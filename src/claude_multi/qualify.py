"""The qualification battery of user-defined model lines.

``claude-multi models qualify KEY [--smoke] [--efforts] [--tools]
[--stream] [--context N] [--agents]`` runs a consented, bounded set of
requests through the loopback gateway on the line's own aliases, so the
check covers the rendered route (auth, payload rules, force-mapping). This
module is the one engine: the plan and its exact consent text, the
deterministic synthetic payloads, the verdicts, the bounded loopback
transport and the contract identities evidence is bound to. It never
prompts, reads secrets or writes state: the command (``cli.commands.models``)
owns consent and the evidence commit, ``operator`` owns the store.

Rules:

- ``--agents`` = smoke + every declared effort + tools + stream, never
  context. Flags compose; a duplicate flag never duplicates a check.
- The tools variant (forced named tool, or strict automatic tool) is chosen
  before consent and disclosed in the plan. There is never a retry with a
  weaker variant; the second tools turn runs only when the first returned
  the expected tool call.
- Each request: 120 s total wall-clock and 256 KiB received (context
  300 s); a synthetic request above 16 MiB is refused before sending. No
  retries, no route fallback, no model substitution. ``max_tokens`` is sent
  but is not a cost bound, and cancelling cannot stop upstream billing.
- Outcomes are ``pass``, ``failed`` (a definite rejection or wrong
  behaviour) and ``inconclusive`` (429, 5xx, connection errors, timeouts,
  a malformed HTTP exchange). An inconclusive attempt never replaces an
  earlier pass (``operator.merge_checks``).
- Evidence is bound to the definition digest and to two opaque contract
  digests: the client (validated version, executable sha256, agent
  efforts) and the gateway (upstream version plus the ordered
  ``basename@sha256`` patch identities of the packaged
  ``gateway-contract.json``, a checked-in resource; absent only from
  resources that carry none).
- The exact-client check (first-party pool lines) is a separate, offline
  run of the pinned client against a local fake (zero provider requests);
  its evidence is recorded apart from the HTTP checks.
- The loopback transport is ``http.client`` behind
  ``launch.gateway_authorization``: environment proxies are never
  consulted, redirects are never followed.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import strict_json

# ------------------------------------------------------------ constants
REQUEST_DEADLINE_SECONDS = 120.0
CONTEXT_DEADLINE_SECONDS = 300.0
RESPONSE_MAX_BYTES = 256 * 1024
REQUEST_BUILD_MAX_BYTES = 16 * 1024 * 1024
SMOKE_INPUT_TOKENS = 40
SMOKE_PROMPT = "Reply with the single word: ok"
SMOKE_MAX_TOKENS = 16
TOOLS_MAX_TOKENS = 256
CONTEXT_MAX_TOKENS = 64
CONTEXT_MIN_TOKENS = 8192
APPROX_CHARS_PER_TOKEN = 4

CHECK_SMOKE = "smoke"
CHECK_EFFORTS = "efforts"
CHECK_TOOLS = "tools"
CHECK_STREAM = "stream"
CHECK_CONTEXT = "context"
CHECK_EXACT = "exact_client"
CHECK_ORDER = (CHECK_SMOKE, CHECK_EFFORTS, CHECK_TOOLS, CHECK_STREAM, CHECK_CONTEXT)
AGENT_CHECKS = (CHECK_SMOKE, CHECK_EFFORTS, CHECK_TOOLS, CHECK_STREAM)

TOOLS_FORCED = "forced"
TOOLS_AUTO = "auto"
TOOL_VARIANTS = (TOOLS_FORCED, TOOLS_AUTO)
TOOL_VARIANT_TEXT = {
    TOOLS_FORCED: "forced named tool (tool_choice {type: tool}, strict object schema)",
    TOOLS_AUTO: "strict automatic tool (tool_choice auto, strict: true, strict object schema)",
}
TOOL_NAME = "cm_qualify_record"

PASS = "pass"
FAILED = "failed"
INCONCLUSIVE = "inconclusive"
RESULTS = (PASS, FAILED, INCONCLUSIVE)
# The bounded failure-code enum (schemas/operator-evidence.schema.json).
REASONS = (
    "ok", "http-status", "not-a-message", "no-tool-call", "wrong-tool-call", "no-acknowledgement",
    "stream-order", "stream-error", "retrieval-wrong", "timeout", "oversize", "connection", "protocol",
    "rate-limited", "upstream-error", "unavailable", "model-rewritten", "effort-dropped",
    "thinking-stripped", "output-exceeds", "window-mismatch", "no-run",
)
REASON_TEXT = {
    "ok": "ok",
    "http-status": "rejected",
    "not-a-message": "the response is not a message object",
    "no-tool-call": "no tool call was returned",
    "wrong-tool-call": "the tool call did not match the expected name and arguments",
    "no-acknowledgement": "the final answer did not acknowledge the tool result",
    "stream-order": "the event stream was out of order or did not end with message_stop",
    "stream-error": "the event stream carried an error event",
    "retrieval-wrong": "retrieval failed",
    "timeout": "the wall-clock cap was reached",
    "oversize": "the response exceeded the byte cap",
    "connection": "the connection failed",
    "protocol": "malformed HTTP exchange",
    "rate-limited": "rate limited (HTTP 429)",
    "upstream-error": "upstream error",
    "unavailable": "exact-client proof unavailable on this platform",
    "model-rewritten": "the client rewrote the model",
    "effort-dropped": "the client dropped or rewrote the effort",
    "thinking-stripped": "the client sent no thinking block",
    "output-exceeds": "the client's output limit exceeds the declaration",
    "window-mismatch": "the client's context window differs from the compiled class",
    "no-run": "the client never reached the fake provider",
}
EXACT_CLIENT_UNAVAILABLE = "exact-client proof unavailable on this platform"
STALE_TEXT = "qualify {key}: configuration changed during qualification — evidence not recorded; retry"
STALE_CONFIRM_TEXT = "qualify {key}: configuration changed while awaiting confirmation — nothing sent; retry"
STALE_SEND_TEXT = ("qualify {key}: configuration changed during qualification — remaining requests not "
                   "sent; evidence not recorded; retry")

GATEWAY_CONTRACT_NAME = "gateway-contract.json"
GATEWAY_CONTRACT_VERSION = 1
_PATCH_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\.patch@[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class QualifyError(ValueError):
    """A plan cannot be built (usage or declaration problem); nothing sent."""


# ------------------------------------------------------------ contract identity
@dataclass(frozen=True)
class ContractIdentity:
    """Opaque digests evidence is bound to (``gateway`` None: no manifest)."""

    client: str
    gateway: str | None

    def as_document(self) -> dict[str, Any]:
        return {"client": self.client, "gateway": self.gateway}


def _digest(kind: str, document: Any) -> str:
    return hashlib.sha256(kind.encode("ascii") + b"\0" + strict_json.canonical_bytes(document)).hexdigest()


def client_contract_digest(native_contract: Mapping[str, Any], *, platform: str | None = None) -> str:
    """Exactly the pinned version, the sha256 of the build this
    machine executes and the agent efforts of the effective contract; prose,
    dates, other platforms and unused fields never move it."""

    from . import errors as errors_mod, pin

    try:
        platform = pin.host_platform() if platform is None else platform
        record = pin.platform_record(native_contract, platform)
    except errors_mod.ClaudeMultiError:
        record = None
    return _digest("cm-client-contract", {
        "validated_version": pin.version(native_contract),
        "executable_sha256": record["sha256"] if record is not None else "unsupported-platform",
        "agent_efforts": list(native_contract["agent_efforts"]["values"]),
    })


def parse_gateway_contract(raw: bytes) -> dict[str, Any]:
    """The packaged ``gateway-contract.json``: strict, closed, ordered."""

    try:
        document = strict_json.loads(raw, strict_json.JSONLimits(max_bytes=65536))
    except strict_json.StrictJSONError as exc:
        raise QualifyError(f"{GATEWAY_CONTRACT_NAME}: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"version", "upstream_version", "patches"}:
        raise QualifyError(f"{GATEWAY_CONTRACT_NAME}: expected exactly version, upstream_version and patches")
    if document["version"] != GATEWAY_CONTRACT_VERSION:
        raise QualifyError(f"{GATEWAY_CONTRACT_NAME}: unsupported version")
    if not isinstance(document["upstream_version"], str) or not _VERSION.fullmatch(document["upstream_version"]):
        raise QualifyError(f"{GATEWAY_CONTRACT_NAME}: upstream_version is not a version")
    patches = document["patches"]
    if not isinstance(patches, list) or len(patches) > 64 or not all(
            isinstance(item, str) and _PATCH_IDENTITY.fullmatch(item) for item in patches):
        raise QualifyError(f"{GATEWAY_CONTRACT_NAME}: patches must be ordered basename@sha256 identities")
    if len({item.split("@", 1)[0] for item in patches}) != len(patches):
        raise QualifyError(f"{GATEWAY_CONTRACT_NAME}: a patch basename appears twice")
    return document


def gateway_contract_digest(document: Mapping[str, Any]) -> str:
    """Upstream version + the ordered patch content identities (never sorted)."""

    return _digest("cm-gateway-contract", {
        "upstream_version": document["upstream_version"], "patches": list(document["patches"]),
    })


def load_gateway_contract(asset_root: Path | str) -> dict[str, Any] | None:
    """The packaged manifest under the asset root, or None (standalone build
    or unreadable/invalid: evidence then counts at most as contract-stale)."""

    path = Path(asset_root) / GATEWAY_CONTRACT_NAME
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        return parse_gateway_contract(raw)
    except QualifyError:
        return None


def contract_identity(native_contract: Mapping[str, Any], asset_root: Path | str) -> ContractIdentity:
    manifest = load_gateway_contract(asset_root)
    return ContractIdentity(
        client=client_contract_digest(native_contract),
        gateway=gateway_contract_digest(manifest) if manifest is not None else None,
    )


# ------------------------------------------------------------ plan
@dataclass(frozen=True)
class PlannedCall:
    """One possible request (disclosed before consent)."""

    check: str  # smoke | efforts | tools | stream | context
    item: str | None  # effort level, tools variant/step, or None
    alias: str
    effort: str | None  # client effort sent as output_config.effort (list-shaped lines)
    approx_input_tokens: int
    deadline: float
    stream: bool = False
    conditional: bool = False

    def label(self) -> str:
        if self.check == CHECK_EFFORTS:
            return f"effort {self.item}"
        if self.check == CHECK_TOOLS:
            variant, step = str(self.item).split(":")
            return f"tools ({variant}) step {step}"
        if self.check == CHECK_CONTEXT:
            return f"context ≈{self.approx_input_tokens}"
        return self.check


@dataclass(frozen=True)
class QualificationPlan:
    key: str
    digest: str
    provider_id: str
    host: str
    route_text: str
    checks: tuple[str, ...]
    tools_variant: str
    context_tokens: int | None
    calls: tuple[PlannedCall, ...]
    levels: tuple[str, ...]
    exact_client: bool  # a first-party pool line: the offline exact-client check runs too
    declared_tokens: int
    keyed_replay: bool = False  # returned history + final-client effort form, keyed chat only

    def calls_for(self, check: str) -> tuple[PlannedCall, ...]:
        return tuple(call for call in self.calls if call.check == check)


def requested_checks(*, smoke: bool, efforts: bool, tools: bool, stream: bool, context: int | None,
                     agents: bool) -> tuple[str, ...]:
    """No flag = smoke. ``--agents`` = smoke + efforts + tools + stream."""

    wanted = set()
    if agents:
        wanted |= set(AGENT_CHECKS)
    if smoke:
        wanted.add(CHECK_SMOKE)
    if efforts:
        wanted.add(CHECK_EFFORTS)
    if tools:
        wanted.add(CHECK_TOOLS)
    if stream:
        wanted.add(CHECK_STREAM)
    if context is not None:
        wanted.add(CHECK_CONTEXT)
    if not wanted:
        wanted.add(CHECK_SMOKE)
    return tuple(check for check in CHECK_ORDER if check in wanted)


def _base(selector: str) -> str:
    return selector[: -len("[1m]")] if selector.endswith("[1m]") else selector


def line_levels(entry: Mapping[str, Any]) -> tuple[str, ...]:
    efforts = entry["efforts"]
    declared = list(efforts) if isinstance(efforts, list) else list(efforts)
    order = {level: rank for rank, level in enumerate(("low", "medium", "high", "xhigh", "max"))}
    return tuple(sorted(declared, key=lambda level: (order.get(level, 99), level)))


def effort_alias(entry: Mapping[str, Any], level: str) -> tuple[str, str | None]:
    """(alias, client effort) one effort request uses: map-shaped lines use
    the level's own alias (the gateway force-maps its contract); list-shaped
    lines encode the client effort as ``output_config.effort``."""

    efforts = entry["efforts"]
    if isinstance(efforts, list):
        return _base(entry["selector"]), level
    return _base(efforts[level]["selector"]), None


def is_pool_line(provider: Mapping[str, Any]) -> bool:
    """A first-party OAuth-pool line (the exact-client gate applies)."""

    return provider.get("transport", {}).get("kind") == "oauth-pool"


def route_text(provider_id: str, provider: Mapping[str, Any], *, t2: Any = None,
               transport: Any = None) -> tuple[str, str]:
    """(route line, host) of the consent plan: the effective transport
    (a selected reviewed alternative replaces the pool), never a value."""

    if transport is not None and getattr(transport, "alternative", None) is not None:
        alternative = transport.alternative
        host = urllib.parse.urlsplit(alternative.origin).hostname or "unknown"
        return (f"{alternative.origin}; auth {alternative.auth_kind} from {alternative.secret_name} "
                f"(value not shown)", host.lower())
    if t2 is not None:
        host = urllib.parse.urlsplit(t2.origin).hostname or "unknown"
        if t2.auth_kind == "none":
            return f"{t2.origin}; auth none (keyless)", host.lower()
        return f"{t2.origin}; auth {t2.auth_kind} from {t2.secret_name} (value not shown)", host.lower()
    spec = provider["transport"]
    if spec["kind"] == "oauth-pool":
        return (f"the {spec['pool']} OAuth pool; auth an OAuth pool credential held by the gateway (not shown)",
                spec["pool"])
    parts = urllib.parse.urlsplit(spec["base_url"])
    host = (parts.hostname or "unknown").lower()
    auth = spec.get("auth") or {}
    if auth.get("kind") == "none":
        return f"{parts.scheme}://{parts.netloc}; auth none (keyless)", host
    name = str(auth.get("secret_ref", "")).removeprefix("env:")
    return f"{parts.scheme}://{parts.netloc}; auth {auth.get('kind')} from {name} (value not shown)", host


def build_plan(
    key: str, entry: Mapping[str, Any], *, digest: str, provider_id: str, provider: Mapping[str, Any],
    checks: Iterable[str], tools_variant: str = TOOLS_FORCED, context_tokens: int | None = None,
    t2: Any = None, transport: Any = None, keyed_replay: bool = False,
) -> QualificationPlan:
    """The immutable plan of one battery (pure; every possible request)."""

    checks = tuple(check for check in CHECK_ORDER if check in set(checks))
    if tools_variant not in TOOL_VARIANTS:
        raise QualifyError(f"unknown tools variant {tools_variant!r} (choose {' or '.join(TOOL_VARIANTS)})")
    declared = int(entry["context"]["declared_tokens"])
    if CHECK_CONTEXT in checks:
        if context_tokens is None or isinstance(context_tokens, bool) or not isinstance(context_tokens, int):
            raise QualifyError("--context needs an integer token count")
        if not CONTEXT_MIN_TOKENS <= context_tokens <= declared:
            raise QualifyError(f"--context {context_tokens} is outside {CONTEXT_MIN_TOKENS}..{declared} "
                               "(the declared context)")
    levels = line_levels(entry)
    default = entry["default_effort"]
    smoke_alias, smoke_effort = effort_alias(entry, default)
    calls: list[PlannedCall] = []
    for check in checks:
        if check == CHECK_SMOKE:
            calls.append(PlannedCall(CHECK_SMOKE, None, smoke_alias, smoke_effort if keyed_replay else None, SMOKE_INPUT_TOKENS,
                                     REQUEST_DEADLINE_SECONDS))
        elif check == CHECK_EFFORTS:
            for level in levels:
                alias, effort = effort_alias(entry, level)
                calls.append(PlannedCall(CHECK_EFFORTS, level, alias, effort, SMOKE_INPUT_TOKENS,
                                         REQUEST_DEADLINE_SECONDS))
        elif check == CHECK_TOOLS:
            calls.append(PlannedCall(CHECK_TOOLS, f"{tools_variant}:1", smoke_alias, None, 150,
                                     REQUEST_DEADLINE_SECONDS))
            calls.append(PlannedCall(CHECK_TOOLS, f"{tools_variant}:2", smoke_alias, None, 250,
                                     REQUEST_DEADLINE_SECONDS, conditional=True))
        elif check == CHECK_STREAM:
            calls.append(PlannedCall(CHECK_STREAM, None, smoke_alias, None, SMOKE_INPUT_TOKENS,
                                     REQUEST_DEADLINE_SECONDS, stream=True))
        elif check == CHECK_CONTEXT:
            assert context_tokens is not None
            calls.append(PlannedCall(CHECK_CONTEXT, None, smoke_alias, None, context_tokens,
                                     CONTEXT_DEADLINE_SECONDS))
    route, host = route_text(provider_id, provider, t2=t2, transport=transport)
    agentic = bool(set(checks) & {CHECK_TOOLS, CHECK_STREAM, CHECK_EFFORTS})
    return QualificationPlan(
        key=key, digest=digest, provider_id=provider_id, host=host, route_text=route, checks=checks,
        tools_variant=tools_variant, context_tokens=context_tokens if CHECK_CONTEXT in checks else None,
        calls=tuple(calls), levels=levels, exact_client=is_pool_line(provider) and agentic,
        declared_tokens=declared, keyed_replay=keyed_replay,
    )


def consent_text(plan: QualificationPlan) -> str:
    """The exact consent, with its tool-choice variant and disclosures."""

    lines = [
        f"Qualification plan for {plan.key}:",
        f"  definition: {plan.digest}",
        f"  route: {plan.route_text}",
    ]
    for number, call in enumerate(plan.calls, 1):
        lines.append(f"  {number}. {call.label()} — alias {call.alias}, approximately "
                     f"{call.approx_input_tokens} input tokens")
    if CHECK_TOOLS in plan.checks:
        lines.append(f"  tools variant: {TOOL_VARIANT_TEXT[plan.tools_variant]}; no other variant is tried.")
        lines.append("  tools step 2 runs only if step 1 returns the expected tool call.")
    if plan.exact_client:
        lines.append("  exact-client check: the pinned Claude Code client runs offline against a local fake "
                     "(no provider request); recorded as separate evidence.")
    lines += [
        "Caps: 120 s and 256 KiB per request; context 300 s and 256 KiB.",
        "No retries. max_tokens is sent but is not a reliable cost bound.",
        "Qualification records evidence only; it does not admit or enable agents.",
        "Proceed with these listed requests? [y/N] ",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------ payloads (deterministic, synthetic)
def _messages_body(alias: str, messages: list[Any], *, max_tokens: int, effort: str | None = None,
                   stream: bool = False, tools: list[Any] | None = None,
                   tool_choice: Mapping[str, Any] | None = None, keyed_replay: bool = False) -> bytes:
    body: dict[str, Any] = {"model": alias, "max_tokens": max_tokens, "stream": stream, "messages": messages}
    if effort is not None:
        body["output_config"] = {"effort": effort}
        if keyed_replay:
            body["thinking"] = {"type": "adaptive"}
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = dict(tool_choice)
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > REQUEST_BUILD_MAX_BYTES:
        raise QualifyError(f"the synthetic request exceeds {REQUEST_BUILD_MAX_BYTES} bytes; nothing sent")
    return raw


def smoke_body(call: PlannedCall, *, keyed_replay: bool = False) -> bytes:
    return _messages_body(call.alias, [{"role": "user", "content": SMOKE_PROMPT}],
                          max_tokens=SMOKE_MAX_TOKENS, effort=call.effort, stream=call.stream,
                          keyed_replay=keyed_replay)


def tool_definition(variant: str) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": TOOL_NAME,
        "description": "Record the verification nonce the user gives.",
        "input_schema": {
            "type": "object",
            "properties": {"nonce": {"type": "string", "description": "the verification nonce"}},
            "required": ["nonce"],
            "additionalProperties": False,
        },
    }
    if variant == TOOLS_AUTO:
        tool["strict"] = True
    return tool


def tools_step1(call: PlannedCall, variant: str, nonce: str) -> bytes:
    choice = {"type": "tool", "name": TOOL_NAME} if variant == TOOLS_FORCED else {"type": "auto"}
    prompt = f'Call the tool {TOOL_NAME} exactly once with nonce "{nonce}". Do not answer in text.'
    return _messages_body(call.alias, [{"role": "user", "content": prompt}], max_tokens=TOOLS_MAX_TOKENS,
                          tools=[tool_definition(variant)], tool_choice=choice)


def tools_step2(call: PlannedCall, variant: str, nonce: str, tool_use: Mapping[str, Any], ack: str, *,
                assistant_content: list[dict[str, Any]] | None = None) -> bytes:
    prompt = f'Call the tool {TOOL_NAME} exactly once with nonce "{nonce}". Do not answer in text.'
    messages = [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": assistant_content if assistant_content is not None else [
            {"type": "tool_use", "id": tool_use["id"], "name": TOOL_NAME, "input": {"nonce": nonce}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use["id"], "content": f"ACK {ack}"},
            {"type": "text", "text": "Reply with the tool result text exactly."},
        ]},
    ]
    return _messages_body(call.alias, messages, max_tokens=TOOLS_MAX_TOKENS, tools=[tool_definition(variant)])


def context_corpus(tokens: int, nonce: str) -> tuple[str, str]:
    """(corpus, target entry) of approximately ``tokens`` input tokens: a
    synthetic ledger with one answer-bearing record among distractors and a
    per-run nonce (the old research probe's method, never its script)."""

    target_chars = max(0, tokens - 64) * APPROX_CHARS_PER_TOKEN
    rows: list[str] = []
    size = 0
    index = 0
    while size < target_chars:
        row = (f"Entry {index:07d}: account ACCT-{(index * 7919) % 100000:05d} settled "
               f"{(index * 104729) % 1000000 / 100:.2f} units on day {index % 365 + 1}.")
        rows.append(row)
        size += len(row) + 1
        index += 1
    target = max(0, len(rows) // 2)
    label = f"{target:07d}"
    answer = f"Entry {label}: the verification code is {nonce}."
    if rows:
        rows[target] = answer
    else:
        rows.append(answer)
    question = (f"Which verification code appears in entry {label}? Reply with the code only.")
    return "\n".join(rows) + "\n\n" + question, label


def context_body(call: PlannedCall, nonce: str) -> bytes:
    corpus, _label = context_corpus(call.approx_input_tokens, nonce)
    return _messages_body(call.alias, [{"role": "user", "content": corpus}], max_tokens=CONTEXT_MAX_TOKENS)


# ------------------------------------------------------------ transport
@dataclass(frozen=True)
class HttpResult:
    """One bounded exchange: the body stays in memory and is never stored."""

    status: int | None
    body: bytes
    failure: str | None = None  # timeout | oversize | connection | protocol


Post = Callable[[PlannedCall, bytes], HttpResult]


def loopback_post(
    base_url: str, token: str, body: bytes, *, deadline: float, max_bytes: int = RESPONSE_MAX_BYTES,
    owner_check: Callable | None = None, connect: Callable[[float], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> HttpResult:
    """One ``POST /v1/messages`` to the loopback gateway.

    ``http.client`` only (environment proxies never apply), after the
    listener-ownership check. A total wall-clock deadline (a timer shuts the
    socket down) and a cumulative byte cap; no redirect is followed and
    nothing is retried. ``connect`` (tests) supplies the connection.
    """

    import http.client

    from . import launch

    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None:
        raise QualifyError("qualify: the gateway base_url is not loopback http")
    headers = launch.gateway_authorization(base_url, token, owner_check=owner_check)
    start = clock()

    def left() -> float:
        return deadline - (clock() - start)

    status: int | None = None
    connection = (connect(max(0.001, deadline)) if connect is not None
                  else http.client.HTTPConnection(parts.hostname, parts.port, timeout=max(0.001, deadline)))
    timer: threading.Timer | None = None
    cut_done = threading.Event()
    # The request's socket, kept even after http.client hands it to a
    # will-close response (``connection.sock`` is then None): the deadline
    # must still be able to cut a body that dribbles or stalls.
    held: list[Any] = []

    def cut() -> None:
        cut_done.set()
        for sock in (*held, getattr(connection, "sock", None)):
            if sock is None:
                continue
            try:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass

    def bound() -> None:
        for sock in held:
            try:
                sock.settimeout(max(0.001, left()))
            except OSError:
                pass

    try:
        timer = threading.Timer(max(0.0, left()), cut)
        timer.daemon = True
        timer.start()
        connection.request("POST", "/v1/messages", body=body, headers={
            **headers, "content-type": "application/json", "anthropic-version": "2023-06-01",
        })
        if connection.sock is not None:
            held.append(connection.sock)
        if cut_done.is_set():
            cut()
        bound()
        response = connection.getresponse()
        status = response.status
        chunks: list[bytes] = []
        total = 0
        while True:
            if left() <= 0 or cut_done.is_set():
                return HttpResult(status, b"", "timeout")
            bound()
            chunk = response.read1(min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                return HttpResult(status, b"", "oversize")
        if left() <= 0 or cut_done.is_set():
            return HttpResult(status, b"", "timeout")
        return HttpResult(status, b"".join(chunks))
    except (socket.timeout, TimeoutError):
        return HttpResult(status, b"", "timeout")
    except (http.client.HTTPException, ValueError):
        return HttpResult(status, b"", "timeout" if left() <= 0 or cut_done.is_set() else "protocol")
    except OSError:
        return HttpResult(status, b"", "timeout" if left() <= 0 or cut_done.is_set() else "connection")
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()


# ------------------------------------------------------------ verdicts
@dataclass(frozen=True)
class CheckResult:
    """One recorded verdict (never a prompt, answer, argument or header)."""

    check: str
    item: str | None  # effort level or tools variant
    result: str
    http: int | None
    reason: str
    requested: int | None = None
    measured: int | None = None
    measured_source: str | None = None
    retrieval: str | None = None

    def record(self, at: str, contracts: ContractIdentity) -> dict[str, Any]:
        document: dict[str, Any] = {"result": self.result, "http": self.http, "at": at, "reason": self.reason,
                                    "contracts": contracts.as_document()}
        if self.check == CHECK_CONTEXT:
            document.update(requested=self.requested, measured=self.measured,
                            measured_source=self.measured_source, retrieval=self.retrieval)
        return document

    def text(self) -> str:
        reason = REASON_TEXT.get(self.reason, self.reason)
        if self.check == CHECK_EXACT:  # offline: no HTTP exchange
            return self.result if self.result == PASS else f"{self.result} ({reason})"
        http = f"HTTP {self.http}" if self.http is not None else "no HTTP status"
        detail = "" if self.result == PASS else f": {reason}"
        return f"{self.result} ({http}{detail})"


def _transport_verdict(response: HttpResult) -> tuple[str, str] | None:
    """(result, reason) for failures independent of the body, else None."""

    if response.failure == "timeout":
        return INCONCLUSIVE, "timeout"
    if response.failure == "connection":
        return INCONCLUSIVE, "connection"
    if response.failure == "protocol":
        return INCONCLUSIVE, "protocol"
    if response.failure == "oversize":
        return FAILED, "oversize"
    if response.status is None:
        return INCONCLUSIVE, "connection"
    if response.status == 429:
        return INCONCLUSIVE, "rate-limited"
    if 500 <= response.status <= 599:
        return INCONCLUSIVE, "upstream-error"
    if response.status != 200:
        return FAILED, "http-status"
    return None


def _message(body: bytes) -> dict[str, Any] | None:
    try:
        document = strict_json.loads(body, strict_json.JSONLimits(max_bytes=RESPONSE_MAX_BYTES))
    except (strict_json.StrictJSONError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict) or document.get("type") != "message" \
            or not isinstance(document.get("content"), list):
        return None
    return document


def _text_of(document: Mapping[str, Any]) -> str:
    return "".join(block.get("text", "") for block in document.get("content", [])
                   if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))


def classify_message(response: HttpResult) -> tuple[str, str]:
    verdict = _transport_verdict(response)
    if verdict is not None:
        return verdict
    return (PASS, "ok") if _message(response.body) is not None else (FAILED, "not-a-message")


def classify_tool_call(response: HttpResult, nonce: str) -> tuple[str, str, dict[str, Any] | None]:
    verdict = _transport_verdict(response)
    if verdict is not None:
        return verdict[0], verdict[1], None
    document = _message(response.body)
    if document is None:
        return FAILED, "not-a-message", None
    calls = [block for block in document["content"] if isinstance(block, dict) and block.get("type") == "tool_use"]
    if not calls:
        return FAILED, "no-tool-call", None
    call = calls[0]
    if len(calls) != 1 or call.get("name") != TOOL_NAME or call.get("input") != {"nonce": nonce} \
            or not isinstance(call.get("id"), str) or not call["id"]:
        return FAILED, "wrong-tool-call", None
    return PASS, "ok", {"id": call["id"]}


def returned_assistant_content(response: HttpResult, nonce: str) -> list[dict[str, Any]] | None:
    """Validate bounded Messages history for keyed replay, without reconstructing
    it. Content (including reasoning/signatures) is transient, never evidence.
    The single expected call is checked separately from the closed block shapes.
    """

    result, _reason, call = classify_tool_call(response, nonce)
    if result != PASS or call is None:
        return None
    document = _message(response.body)
    if document is None or not 1 <= len(document["content"]) <= 64:
        return None
    content = document["content"]
    for block in content:
        if not isinstance(block, dict):
            return None
        kind = block.get("type")
        if not isinstance(kind, str):
            return None
        fields = {"text": {"type", "text"}, "thinking": {"type", "thinking", "signature"},
                  "redacted_thinking": {"type", "data"},
                  "tool_use": {"type", "id", "name", "input"}}.get(kind)
        required = fields - {"signature"} if kind == "thinking" else fields
        if fields is None or not required <= set(block) <= fields:
            return None
        if kind == "tool_use":
            if (not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", block["id"])
                    or block["name"] != TOOL_NAME or block["input"] != {"nonce": nonce}):
                return None
        else:
            for field in set(block) - {"type"}:
                value = block[field]
                if not isinstance(value, str):
                    return None
                try:
                    if len(value.encode("utf-8")) > 65536:
                        return None
                except UnicodeEncodeError:
                    return None
    return content


def classify_acknowledgement(response: HttpResult, ack: str) -> tuple[str, str]:
    verdict = _transport_verdict(response)
    if verdict is not None:
        return verdict
    document = _message(response.body)
    if document is None:
        return FAILED, "not-a-message"
    return (PASS, "ok") if ack in _text_of(document) else (FAILED, "no-acknowledgement")


def _block_index(index: Any) -> int | None:
    """A content-block index: a non-negative int (never a bool), else None."""

    return index if isinstance(index, int) and not isinstance(index, bool) and index >= 0 else None


def classify_stream(response: HttpResult) -> tuple[str, str]:
    """Valid event ordering ending in ``message_stop``; no error event.

    An explicit state machine: ``message_start`` first; then content blocks
    (``content_block_start`` → ``content_block_delta``* →
    ``content_block_stop`` per valid index); then the terminal
    ``message_delta`` phase, entered only with no block open, after which
    only ``message_delta`` and the final ``message_stop`` may follow. Any
    other order, a malformed index or trailing events is ``stream-order``."""

    verdict = _transport_verdict(response)
    if verdict is not None:
        return verdict
    try:
        text = response.body.decode("utf-8")
    except UnicodeDecodeError:
        return FAILED, "stream-order"
    kinds: list[tuple[str, Any]] = []
    for frame in re.split(r"\r?\n\r?\n", text):
        data = [line[5:].strip() for line in frame.splitlines() if line.startswith("data:")]
        if not data:
            continue
        payload = "\n".join(data)
        if payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except ValueError:
            return FAILED, "stream-order"
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return FAILED, "stream-order"
        if event["type"] == "error":
            return FAILED, "stream-error"
        if event["type"] != "ping":
            kinds.append((event["type"], event.get("index")))
    if not kinds or kinds[0][0] != "message_start" or kinds[-1][0] != "message_stop":
        return FAILED, "stream-order"
    open_blocks: set[int] = set()
    terminal = False  # inside the message_delta phase
    for kind, raw_index in kinds[1:-1]:
        if kind in ("content_block_start", "content_block_delta", "content_block_stop"):
            index = _block_index(raw_index)
            if terminal or index is None:
                return FAILED, "stream-order"
            if kind == "content_block_start":
                if index in open_blocks:
                    return FAILED, "stream-order"
                open_blocks.add(index)
            elif index not in open_blocks:
                return FAILED, "stream-order"
            elif kind == "content_block_stop":
                open_blocks.discard(index)
        elif kind == "message_delta":
            if open_blocks:
                return FAILED, "stream-order"
            terminal = True
        elif kind in ("message_start", "message_stop"):
            return FAILED, "stream-order"
    if open_blocks:
        return FAILED, "stream-order"
    return PASS, "ok"


def _usage_tokens(document: Mapping[str, Any]) -> int | None:
    usage = document.get("usage")
    if not isinstance(usage, dict):
        return None
    total = 0
    found = False
    for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**31 - 1:
            return None
        total += value
        found = found or name == "input_tokens"
    return total if found and total > 0 else None


def classify_context(response: HttpResult, nonce: str, requested: int) -> CheckResult:
    verdict = _transport_verdict(response)
    if verdict is not None:
        return CheckResult(CHECK_CONTEXT, None, verdict[0], response.status, verdict[1], requested=requested)
    document = _message(response.body)
    if document is None:
        return CheckResult(CHECK_CONTEXT, None, FAILED, response.status, "not-a-message", requested=requested)
    measured = _usage_tokens(document)
    correct = nonce in _text_of(document)
    return CheckResult(
        CHECK_CONTEXT, None, PASS if correct else FAILED, response.status, "ok" if correct else "retrieval-wrong",
        requested=requested, measured=measured, measured_source="usage.input_tokens" if measured else None,
        retrieval="correct" if correct else "wrong",
    )


# ------------------------------------------------------------ execution
def run_battery(plan: QualificationPlan, post: Post, *,
                nonce: Callable[[], str] = lambda: secrets.token_hex(6),
                on_result: Callable[[CheckResult], None] | None = None,
                before_send: Callable[[int], None] | None = None) -> tuple[CheckResult, ...]:
    """Send the plan's requests in order, never more than planned and never
    retried. The second tools turn runs only after the expected call.

    ``before_send(sent)`` runs immediately before every request (``sent`` =
    the requests already sent); it revalidates the consented authorization
    and raises to stop the remaining requests."""

    results: list[CheckResult] = []
    sent = 0

    def send(call: PlannedCall, body: bytes) -> HttpResult:
        nonlocal sent
        if before_send is not None:
            before_send(sent)
        sent += 1
        return post(call, body)

    def done(result: CheckResult) -> None:
        results.append(result)
        if on_result is not None:
            on_result(result)

    for check in plan.checks:
        calls = plan.calls_for(check)
        if check in (CHECK_SMOKE, CHECK_EFFORTS, CHECK_STREAM):
            for call in calls:
                response = send(call, smoke_body(call, keyed_replay=plan.keyed_replay))
                result, reason = classify_stream(response) if call.stream else classify_message(response)
                done(CheckResult(check, call.item if check == CHECK_EFFORTS else None, result,
                                 response.status, reason))
        elif check == CHECK_TOOLS:
            first, second = calls
            variant = plan.tools_variant
            value, ack = nonce(), nonce()
            response = send(first, tools_step1(first, variant, value))
            result, reason, tool_use = classify_tool_call(response, value)
            if result != PASS or tool_use is None:
                done(CheckResult(CHECK_TOOLS, variant, result, response.status, reason))
                continue
            content = returned_assistant_content(response, value) if plan.keyed_replay else None
            if plan.keyed_replay and content is None:
                done(CheckResult(CHECK_TOOLS, variant, FAILED, response.status, "not-a-message"))
                continue
            response = send(second, tools_step2(second, variant, value, tool_use, ack,
                                                 assistant_content=content))
            result, reason = classify_acknowledgement(response, ack)
            done(CheckResult(CHECK_TOOLS, variant, result, response.status, reason))
        elif check == CHECK_CONTEXT:
            (call,) = calls
            value = nonce()
            response = send(call, context_body(call, value))
            done(classify_context(response, value, call.approx_input_tokens))
    return tuple(results)


def context_floor(declared: int, current_floor: int, result: CheckResult) -> int:
    """The validated floor after one context result: only an accepted,
    correct, measured result raises it, to the measured count capped at
    the declaration; never below the current floor."""

    if result.result != PASS or result.retrieval != "correct" or result.measured is None:
        return current_floor
    return max(current_floor, min(result.measured, declared))


def context_text(key: str, result: CheckResult, floor_before: int, floor_after: int) -> str:
    if result.result == PASS and result.retrieval == "correct" and result.measured is not None:
        head = f"context {key}: accepted {result.measured} input tokens; retrieval correct"
    elif result.retrieval == "wrong" and result.measured is not None:
        head = f"context {key}: accepted {result.measured} input tokens; retrieval failed"
    elif result.retrieval == "correct":
        head = f"context {key}: accepted; retrieval correct; token count unavailable"
    elif result.retrieval == "wrong":
        head = f"context {key}: accepted; retrieval failed; token count unavailable"
    else:
        head = f"context {key}: {result.text()}"
    tail = (f"validated floor: {floor_after}" if floor_after != floor_before
            else f"validated floor unchanged: {floor_after}")
    return f"{head}\n{tail}"


# ------------------------------------------------------------ exact-client check
@dataclass(frozen=True)
class ExactClientInput:
    """What the offline exact-client run needs (the production compile of
    the line: its generated selector and compiled scope settings)."""

    key: str
    selector: str  # the generated selector the client is given (--model, agent frontmatter)
    wire: str  # the model id the client must send (the selector without [1m])
    efforts: tuple[str, ...]
    client_effort: bool  # list-shaped: the client sends output_config.effort
    output_limit: int | None
    expected_window: Mapping[str, int]  # {"client": class window, "compiled": operating window}
    settings: Mapping[str, Any]  # the compiled scope settings
    plan: Any = None  # the compiled scope.ScopePlan (written into each disposable fixture)


@dataclass(frozen=True)
class ExactClientOutcome:
    result: str  # pass | failed | inconclusive
    reason: str  # ok | model-rewritten | ... | unavailable | no-run

    def text(self) -> str:
        return "pass" if self.result == PASS else f"{self.result} ({REASON_TEXT.get(self.reason, self.reason)})"


def exact_client_class(observations: Mapping[str, Any], *, wire: str, efforts: Iterable[str],
                       client_effort: bool, output_limit: int | None,
                       window: Mapping[str, Any]) -> ExactClientOutcome:
    """Pure verdict over the recorded request metadata (lead and agent runs):
    the wire model unchanged, every declared effort unchanged with a thinking
    block (client-effort lines), the output limit within the declaration and
    the /context window equal to the compiled class."""

    runs = [*observations.get("lead", {}).values(), *observations.get("agent", {}).values()]
    if not runs or any(not run.get("rows") for run in runs):
        return ExactClientOutcome(INCONCLUSIVE, "no-run")
    for run in runs:
        for row in run["rows"]:
            if row.get("model") != wire:
                return ExactClientOutcome(FAILED, "model-rewritten")
            if client_effort:
                if row.get("effort") != run.get("effort"):
                    return ExactClientOutcome(FAILED, "effort-dropped")
                if row.get("thinking") is None:
                    return ExactClientOutcome(FAILED, "thinking-stripped")
            if output_limit is not None and (row.get("max_tokens") is None or row["max_tokens"] > output_limit):
                return ExactClientOutcome(FAILED, "output-exceeds")
    if observations.get("window") != window:
        return ExactClientOutcome(FAILED, "window-mismatch")
    return ExactClientOutcome(PASS, "ok")


def exact_client_result(outcome: ExactClientOutcome) -> CheckResult:
    return CheckResult(CHECK_EXACT, None, outcome.result, None, outcome.reason)


def pool_efforts(entry: Mapping[str, Any]) -> tuple[str, ...]:
    return line_levels(entry)


def is_client_effort(entry: Mapping[str, Any]) -> bool:
    return isinstance(entry.get("efforts"), list)
