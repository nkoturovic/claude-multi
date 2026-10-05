"""The managed skill policy (S14) and the fast-mode tripwire (FM).

S14: the
compiled session settings of every managed scope deny model invocation of
the bundled skills named in ``scope.SKILL_POLICY`` (``permissions.deny``
``Skill(<name>)``). The real pinned client proves, with a discriminating
pre-policy negative control in the same mode for every case:

- the lead's and a cm-* subagent's ``Skill`` call is refused ("blocked by
  permission rules") and nothing forks, while the identical pre-policy call
  forks or loads the skill; a read-only role has no ``Skill`` tool at all;
- the refusal holds in every permission mode (both bypass spellings,
  ``auto`` and the ``manual`` alias included) crossed with no CLI allow,
  ``--allowedTools Skill(code-review)``, ``--allowedTools Skill`` and the
  ``--allowed-tools`` spelling, and for the extra surfaces
  (``--allow-dangerously-skip-permissions``, ``--inherit-permission-mode``,
  ``--setting-sources``, ``--managed-settings``);
- user, project and local allow rules and a CLI allow do not defeat it, on
  a fresh start and on ``--resume``;
- aliases resolve before the permission check (``review``), a subcommand is
  an argument (``code-review ultra``), and an upstream user-only entry
  (``disable-model-invocation``) or an unregistered name is classified as
  such, never counted as a newly denied pass;
- positive controls: a non-spawning bundled skill still loads, the lead
  still delegates to a bound cm reviewer, ``/cm`` dynamic expansion (the
  3.1 skill bytes with the argument hint) still runs, and an operator-typed
  ``/code-review``, ``/review``, ``/simplify`` and ``/batch`` still run.

Not exercised: supervisor takeover (documented as unverified).

FM: with the compiled ``CLAUDE_CODE_DISABLE_FAST_MODE=1``, an
interactive client seeded with ``fastMode: true`` and a cached
``penguinModeOrgEnabled: true`` and a fixed fake helper key sends zero
``GET /api/claude_code_penguin_mode`` through a local non-forwarding
CONNECT/TLS tripwire for ``api.anthropic.com:443`` and no ``speed`` on the
fake messages request; the same run without the disable reaches the
tripwire (the negative control). The compiled flag settings state the same
disable, because user, project and local settings ``env`` override the
process environment: with all three stating ``0`` the flag-settings ``1``
still holds on a fresh start and on ``--resume`` (FM-layers; its control,
without the flag-settings disable, prefetches and sends ``fast``). The
tripwire never forwards, resolves or dials anything; other hosts are refused
with 403.

Every mandatory case prints its own ``PASS class=...`` completion record
(``upgrade.ESSENTIAL_EVIDENCE_PREFIXES``): a case that skips as
INCONCLUSIVE never lets a re-pin pass on the other cases' evidence.

Real-binary runs only (``_tier.real_binary_gate``; fixture HOME and config,
``bwrap --unshare-net``, the fake provider over the fixture unix socket).
Evidence is metadata only: tool results are classified in memory, request
bodies are never kept or printed.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest import mock

import test_compiler_v2  # module import: no test classes re-exported
from _catalog import FIXTURE_ROOT
from _tier import real_binary_gate
from _layout import RESOURCES_ROOT  # the one root source
from claude_multi import catalog, cli, compiler, probe, profile, scope, sessions, state, strict_json
from claude_multi.cli import doctor, doctor_actions, launch_flow, text as cli_text
from _v4 import V4Case

_CONTRACT_PATH = RESOURCES_ROOT / "catalog" / "native-contract.json"
_LIVE_DOMAIN_TOUCHED = "live daemon domain touched"
LEAD_MODEL = "s14-lead"
AGENT_MODEL = "s14-agent"
LEAD_PROMPT = "S14-LEAD-PROMPT"
ARG_RE = re.compile(r"S14-ARG-\d{3}")
AGENT_RE = re.compile(r"S14-AGENT-\d{2}X")
TYPED_RE = re.compile(r"S14-TYPED-(\d{2})")
MAX_BODY = 16 * 1024 * 1024

# Tool-result classes (the result text is classified in memory, never kept).
LOADED, DENIED, USER_ONLY, UNKNOWN, ASK_REFUSED, OTHER_ERROR = (
    "loaded", "denied", "user-only", "unknown", "ask-refused", "other-error")
# Entry classification from a (pre-policy negative, policy) pair.
NEWLY_DENIED, UPSTREAM_REFUSED, UNREGISTERED, DEFEATED, INCONCLUSIVE = (
    "newly-denied", "upstream-refused", "unregistered", "defeated", "inconclusive")
PIN_CLASS = {"model": NEWLY_DENIED, "ask": NEWLY_DENIED, "user": UPSTREAM_REFUSED, "absent": UNREGISTERED}

# The permission-mode matrix: every passthrough permission mode crossed with the
# allow surfaces, plus the extra surfaces; each cell is (label, argv).
MODES = (
    ("default", ("--permission-mode", "default")),
    ("manual", ("--permission-mode", "manual")),
    ("acceptEdits", ("--permission-mode", "acceptEdits")),
    ("auto", ("--permission-mode", "auto")),
    ("bypassPermissions", ("--permission-mode", "bypassPermissions")),
    ("dontAsk", ("--permission-mode", "dontAsk")),
    ("plan", ("--permission-mode", "plan")),
    ("dangerously-skip-permissions", ("--dangerously-skip-permissions",)),
)
ALLOWS = (
    ("no-allow", ()),
    ("allow-code-review", ("--allowedTools", "Skill(code-review)")),
    ("allow-Skill", ("--allowedTools", "Skill")),
)
EXTRA_SURFACES = (
    ("allowed-tools-alias", ("--allowed-tools", "Skill")),
    ("allow-dangerously-skip-permissions", ("--allow-dangerously-skip-permissions",)),
    ("inherit-permission-mode", ("--inherit-permission-mode", "bypassPermissions")),
    ("setting-sources-user", ("--setting-sources", "user")),
)


def result_class(block: dict[str, Any]) -> str:
    """The class of one ``tool_result`` block (its text never leaves memory)."""

    if block.get("is_error") is not True:
        return LOADED
    text = json.dumps(block.get("content"))
    if "blocked by permission rules" in text:
        return DENIED
    if "disable-model-invocation" in text:
        return USER_ONLY
    if "Unknown skill" in text:
        return UNKNOWN
    if "Execute skill" in text:
        return ASK_REFUSED
    return OTHER_ERROR


def classify_entry(negative: str, policy: str, forks: tuple[int, int] = (0, 0)) -> str:
    """Newly denied only when the pre-policy call loaded (or forked) and
    the policy call was refused by the permission rules with no fork; an
    upstream-refused, unregistered or mode-unavailable negative is never a
    pass for the deny."""

    if policy == LOADED or forks[1]:
        return DEFEATED
    if negative == USER_ONLY:
        return UPSTREAM_REFUSED
    if negative == UNKNOWN:
        return UNREGISTERED
    if negative == LOADED and policy == DENIED:
        return NEWLY_DENIED
    return INCONCLUSIVE


def _redacted(path: str) -> str:
    """A request path with id-shaped segments (digits, or ``sdk-…``) replaced by ``<id>``."""

    return "/".join("<id>" if re.search(r"\d|^sdk-", segment) else segment
                    for segment in path.split("/"))


def _route(path: str) -> str:
    return urllib.parse.urlsplit(path).path.rstrip("/")


def _messages_payload(content: list[dict[str, Any]], document: dict[str, Any], ordinal: int) -> Any:
    stop = "tool_use" if content[0].get("type") == "tool_use" else "end_turn"
    payload = probe._message_payload(
        content, model=document.get("model"), stop_reason=stop, message_id=f"msg_s14_{ordinal:04d}"
    )
    return probe._sse_message(payload) if document.get("stream") else payload


def _tool_names(document: dict[str, Any]) -> set[str]:
    return {tool.get("name") for tool in document.get("tools") or [] if isinstance(tool, dict)}


def _tool_results(messages: list[Any]) -> list[dict[str, Any]]:
    found = []
    for message in messages:
        blocks = message.get("content") if isinstance(message, dict) else None
        for block in blocks if isinstance(blocks, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                found.append(block)
    return found


class S14Provider(probe.FakeAnthropicProvider):
    """The loopback fake with a lenient in-memory JSON parse (a loaded bundled
    skill can exceed the strict parser's limits) and, when ``tls`` is given,
    a non-forwarding CONNECT tripwire for exactly ``api.anthropic.com:443``.

    Only counters, paths, booleans and ``speed`` values are kept.
    """

    def __init__(self, *, responder: Any, tls: tuple[Path, Path] | None = None) -> None:
        super().__init__(responder=responder)
        self.lock = threading.Lock()
        self.connects: list[str] = []
        self.inner: list[tuple[str, str, bool]] = []  # (method, path, fixture helper key present)
        self.speeds: list[Any] = []
        self.tls_context = None
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(tls[0]), str(tls[1]))
            self.tls_context = context
        self.helper_key: str | None = None

    def unix_socket(self, directory: Path | str) -> Path:
        path = super().unix_socket(directory)
        self._unix_servers[path][0].RequestHandlerClass = _S14Handler
        return path


class _S14Handler(probe._ProviderHandler):
    inner_tls = False

    @property
    def owner(self) -> S14Provider:
        return self.server.provider  # type: ignore[attr-defined]

    def do_CONNECT(self) -> None:
        owner = self.owner
        with owner.lock:
            owner.connects.append(self.path)
        if self.path != "api.anthropic.com:443" or owner.tls_context is None:
            self.send_error(403)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.flush()
        try:
            with owner.tls_context.wrap_socket(self.connection, server_side=True) as connection:
                handler = type("_Inner", (_S14Handler,), {"inner_tls": True})
                handler(connection, self.client_address, self.server)
        except (OSError, ssl.SSLError):
            pass
        self.close_connection = True

    def _inner(self, method: str) -> None:
        owner = self.owner
        key = self.headers.get("x-api-key")
        with owner.lock:
            owner.inner.append((method, _route(self.path), key is not None and key == owner.helper_key))
        if method == "GET" and _route(self.path) == "/api/claude_code_penguin_mode":
            self._send_json(200, {"enabled": True, "disabled_reason": None})
            return
        self._error(404, "not_found_error", "s14 tripwire: not served")

    def do_GET(self) -> None:
        if self.inner_tls:
            self._inner("GET")
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.inner_tls:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(min(length, MAX_BODY))
            self._inner("POST")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY:
            self._error(413, "invalid_request_error", "s14: body too large")
            return
        body = self.rfile.read(length) if length else b""
        try:
            document = json.loads(body) if body else None
        except ValueError:
            document = None
        if not isinstance(document, dict):
            self._error(400, "invalid_request_error", "s14: body is not a JSON object")
            return
        owner = self.owner
        if _route(self.path).endswith("/messages"):
            with owner.lock:
                owner.speeds.append(document.get("speed"))
        context = owner._record(self.command, self.path, self.headers, b"", document)
        reply = owner._respond(document, self.path, context)
        status, payload = reply
        if isinstance(payload, probe.SseResponse):
            self._send_sse(status, payload)
        else:
            self._send_json(status, payload)


@dataclasses.dataclass
class AgentSeen:
    skill_offered: bool | None = None


class SkillResponder:
    """In-memory script: the lead calls ``calls`` (Skill) and spawns
    ``spawns`` (Agent) in its first request; each spawned agent calls its own
    skills in its first request. A request that is neither the lead's (its
    first message carries the prompt marker) nor an agent's (its first
    message carries the spawn marker) is a fork, attributed through the one
    ``S14-ARG-nnn`` argument marker it carries. Only classes and counters
    are kept."""

    def __init__(self, calls=(), spawns=()) -> None:
        self.lock = threading.Lock()
        self.calls = [(f"toolu_s14_lead_{index:03d}", skill, arg) for index, (skill, arg) in enumerate(calls)]
        self.spawns = {marker: (agent_type, [(f"toolu_s14_{marker[10:12]}_{index:03d}", skill, arg)
                                             for index, (skill, arg) in enumerate(agent_calls)])
                       for marker, (agent_type, agent_calls) in spawns}
        self.lead_sent = False
        self.results: dict[str, str] = {}
        self.forks: dict[str, int] = {}
        self.unattributed = 0
        self.agents = {marker: AgentSeen() for marker in self.spawns}
        self.lead_requests = 0

    def respond(self, document: dict[str, Any], path: str, context: probe.RequestContext) -> tuple[int, Any]:
        route = _route(path)
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return 404, {"type": "error", "error": {"type": "not_found_error", "message": "s14"}}
        messages = document.get("messages") or []
        first = json.dumps(messages[0]) if messages else ""
        tools = _tool_names(document)
        content: list[dict[str, Any]] = [{"type": "text", "text": "PROBE-OK"}]
        with self.lock:
            for block in _tool_results(messages):
                self.results[str(block.get("tool_use_id"))] = result_class(block)
            agent = next((marker for marker in self.spawns if marker in first), None)
            if LEAD_PROMPT in first:
                self.lead_requests += 1
                if not self.lead_sent and ("Skill" in tools or "Agent" in tools):
                    self.lead_sent = True
                    content = [
                        {"type": "tool_use", "id": call_id, "name": "Skill", "input": {"skill": skill, "args": arg}}
                        for call_id, skill, arg in self.calls
                    ] + [
                        {"type": "tool_use", "id": f"toolu_s14_spawn_{marker[10:12]}", "name": "Agent",
                         "input": {"description": "s14", "subagent_type": agent_type,
                                   "prompt": f"Reply {marker}."}}
                        for marker, (agent_type, _calls) in self.spawns.items()
                    ] or content
            elif agent is not None:
                seen = self.agents[agent]
                if seen.skill_offered is None:
                    seen.skill_offered = "Skill" in tools
                    calls = self.spawns[agent][1]
                    if calls:
                        content = [
                            {"type": "tool_use", "id": call_id, "name": "Skill",
                             "input": {"skill": skill, "args": arg}}
                            for call_id, skill, arg in calls
                        ]
            else:
                markers = set(ARG_RE.findall(json.dumps(messages)))
                if len(markers) == 1:
                    marker = next(iter(markers))
                    self.forks[marker] = self.forks.get(marker, 0) + 1
                else:
                    self.unattributed += 1
        return 200, _messages_payload(content, document, context.ordinal)

    def outcome(self, call_id: str) -> str:
        return self.results.get(call_id, "no-result")


class _RealCase(unittest.TestCase):
    """The operator-host boundary (the spike precedent) and one disposable fixture per test."""

    trusted: probe.TrustedExecutable
    boundary_skip: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="the S14/FM probes")
        cls.boundary_skip = gate.boundary
        if gate.trusted is not None:
            cls.trusted = gate.trusted

    def setUp(self) -> None:
        super().setUp()
        if self.boundary_skip is not None:
            self.skipTest(self.boundary_skip)
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-s14-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"HOME": str(self.root / "live" / "home"), "PATH": "/usr/bin:/bin"}
        self.fixture = probe.build_fixture(self.root / "fixture", environ=self.environ)
        self.scope = state.ensure_private_dir(self.fixture.root / "scope")
        probe.seed_fixture_project_trust(self.fixture, self.fixture.project_dir)
        self.runs = 0

    @property
    def binary_label(self) -> str:
        return f"bin={self.trusted.resolved_path.name}/{self.trusted.sha256[:12]}"

    def guard(self, probe_id: str, run: Any) -> Any:
        try:
            return run()
        except probe.ProbeError as exc:
            if _LIVE_DOMAIN_TOUCHED in str(exc):
                print(f"client probe {probe_id}: INCONCLUSIVE live daemon domain touched {self.binary_label}")
                self.skipTest(f"BOUNDARY: {_LIVE_DOMAIN_TOUCHED}; {probe_id} is inconclusive, never a pass")
            raise

    def settings_file(self, document: dict[str, Any]) -> Path:
        self.runs += 1
        path = self.scope / f"settings-{self.runs:03d}.json"
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        return path

    def run_print(self, settings: dict[str, Any], responder: SkillResponder, extra=(), *,
                  probe_id: str = "S14", timeout: float = 180) -> probe.NativeRunResult:
        path = self.settings_file(settings)

        def run() -> probe.NativeRunResult:
            with S14Provider(responder=responder) as provider:
                return probe.run_native(
                    ("-p", LEAD_PROMPT, "--settings", str(path), "--model", LEAD_MODEL,
                     "--add-dir", str(self.scope), *extra),
                    trusted=self.trusted, fixture=self.fixture, provider=provider, timeout=timeout,
                    allow_real=True, environ=self.environ,
                )

        result = self.guard(probe_id, run)
        self.assertFalse(result.timed_out, f"{probe_id}: timed out {extra}")
        self.assertEqual(result.returncode, 0, f"{probe_id}: exit {result.returncode} {extra}")
        return result


def compiled_permissions() -> dict[str, Any]:
    """The production compile's ``permissions`` (fixture ``balanced``) for a
    launch whose settings layers configure no mode (the compiled
    ``defaultMode``; explicit ``--permission-mode`` cases override it)."""

    _lineup, result = test_compiler_v2._seed_launch("balanced", default_mode=scope.PERMISSION_DEFAULT_MODE)
    return copy_json(result.scope_plan.settings["permissions"])


def pre_policy(permissions: dict[str, Any]) -> dict[str, Any]:
    """The same permissions without the named skill denies (the negative control)."""

    named = set(scope.MANAGED_SKILL_DENIES)
    return {**permissions, "deny": [entry for entry in permissions["deny"] if entry not in named]}


def copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _arg(index: int) -> str:
    return f"S14-ARG-{index:03d}"


# ------------------------------------------------------------------ pure


class SkillPolicyInventoryTests(unittest.TestCase):
    """The compiled inventory itself (no binary)."""

    def test_inventory_is_named_classified_and_merged_into_every_scope(self) -> None:
        names = [name for name, _why, _pin in scope.SKILL_POLICY]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), len(set(names)))
        for name, why, pin in scope.SKILL_POLICY:
            self.assertIn(pin, scope.SKILL_POLICY_CLASSES)
            self.assertTrue(why)
            self.assertRegex(name, r"^[a-z][a-z0-9-]*$")
        self.assertEqual(scope.MANAGED_SKILL_DENIES, tuple(f"Skill({name})" for name in names))
        # The three named skills, and never /cm or a wildcard.
        for required in ("Skill(code-review)", "Skill(simplify)", "Skill(batch)"):
            self.assertIn(required, scope.MANAGED_SKILL_DENIES)
        self.assertNotIn("Skill(cm)", scope.MANAGED_SKILL_DENIES)
        self.assertNotIn("Skill", scope.MANAGED_SKILL_DENIES)
        for seed in ("balanced", "direct"):
            _lineup, result = test_compiler_v2._seed_launch(seed)
            deny = result.scope_plan.settings["permissions"]["deny"]
            with self.subTest(seed=seed):
                start = deny.index(scope.MANAGED_SKILL_DENIES[0])
                self.assertEqual(tuple(deny[start:start + len(scope.MANAGED_SKILL_DENIES)]),
                                 scope.MANAGED_SKILL_DENIES)
                # merged, never replacing: native-agent denies before, secret denies after
                self.assertTrue(all(entry.startswith("Agent(") for entry in deny[:start]))
                self.assertTrue(set(deny[start + len(scope.MANAGED_SKILL_DENIES):])
                                >= set(scope.SECRET_PATH_DENIES))
        _lineup, result = test_compiler_v2._seed_launch("balanced", no_subagents=True)
        deny = result.scope_plan.settings["permissions"]["deny"]
        self.assertEqual(deny[0], "Agent")
        self.assertEqual(tuple(deny[1:1 + len(scope.MANAGED_SKILL_DENIES)]), scope.MANAGED_SKILL_DENIES)

    def test_classification_never_counts_an_unavailable_negative(self) -> None:
        self.assertEqual(classify_entry(LOADED, DENIED), NEWLY_DENIED)
        self.assertEqual(classify_entry(USER_ONLY, DENIED), UPSTREAM_REFUSED)
        self.assertEqual(classify_entry(USER_ONLY, USER_ONLY), UPSTREAM_REFUSED)
        self.assertEqual(classify_entry(UNKNOWN, UNKNOWN), UNREGISTERED)
        self.assertEqual(classify_entry(ASK_REFUSED, DENIED), INCONCLUSIVE)
        self.assertEqual(classify_entry(OTHER_ERROR, DENIED), INCONCLUSIVE)
        self.assertEqual(classify_entry(LOADED, LOADED), DEFEATED)
        self.assertEqual(classify_entry(LOADED, DENIED, (1, 1)), DEFEATED)


class LaunchNoticeTests(unittest.TestCase):
    """Only a surface S14 proved to defeat the deny gets a notice."""

    def test_proven_bypass_prints_launch_notice(self) -> None:
        # S14 at the pin proved no defeating surface: the table is empty and
        # no matrix surface prints anything.
        self.assertEqual(compiler.SKILL_POLICY_BYPASS_FLAGS, {})
        for _label, argv in (*MODES, *ALLOWS, *EXTRA_SURFACES):
            self.assertEqual(compiler.skill_policy_bypass_flags(argv), ())
        # The mechanism: a listed (proven) flag prints exactly one stderr
        # line at launch, with or without a value, and is never blocked.
        prepared = mock.Mock()
        prepared.result.argv = ["--session-id", "x", "--defeating-flag=1", "--verbose", "--defeating-flag"]
        with mock.patch.dict(compiler.SKILL_POLICY_BYPASS_FLAGS, {"--defeating-flag": "fixture"}):
            self.assertEqual(compiler.skill_policy_bypass_flags(prepared.result.argv), ("--defeating-flag",))
            stream = io.StringIO()
            launch_flow._skill_policy_notices(prepared, stream)
        lines = stream.getvalue().splitlines()
        self.assertEqual(lines, [cli_text.SKILL_POLICY_BYPASS_NOTICE.format(flag="--defeating-flag")])
        self.assertTrue(lines[0].startswith("claude-multi: notice: --defeating-flag"))
        stream = io.StringIO()
        launch_flow._skill_policy_notices(prepared, stream)
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(compiler.validate_passthrough(["--defeating-flag"]), ["--defeating-flag"])


class SkillPolicyScopeRepairTests(V4Case):
    """A live scope written by the other release
    (3.0.x without the skill denies and with the 3.0 /cm bytes, or 3.1 under a
    rollback compile) is lazy state repaired by resume or ``doctor
    --repair``; any other permissions drift stays BLOCK. No record-schema
    change is involved (the policy is compile output only)."""

    def _integrity(self, mid: str):
        return doctor._check_scope_integrity(self.runtime, self.store.load(mid))

    def _repair(self, mid: str) -> None:
        out = io.StringIO()
        self.assertEqual(doctor_actions._doctor_repair(self.runtime, mid, out), 0, out.getvalue())

    def _files(self, mid: str) -> tuple[bytes, bytes]:
        live = self.live(mid)
        return (live / "settings.json").read_bytes(), (live / scope.SKILL_RELPATH).read_bytes()

    def _write_settings(self, mid: str, mutate) -> None:
        path = self.live(mid) / "settings.json"
        document = strict_json.loads(path.read_bytes())
        mutate(document["permissions"])
        state.atomic_write(path, strict_json.canonical_file_bytes(document))

    def test_skill_policy_scope_repair_both_directions(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        released = self._files(mid)
        self.assertIn("Skill(code-review)", released[0].decode())
        self.assertEqual(released[1], scope.SKILL_MD_BYTES)
        agents_before = {k: v for k, v in self.tree(self.live(mid)).items() if k.startswith(".claude/agents/")}
        drift = cli_text.SKILL_POLICY_DRIFT.format(m8=mid[:8], mid=mid)
        previous = scope.PREVIOUS_SKILL_MD_BYTES[0]

        # Forward (3.0.x scope under the 3.1 launcher): Attention, repair writes 3.1.
        # A 3.0.x scope also has no permissions.defaultMode; repair
        # never adds it to a live scope (the next resume decides).
        named = set(scope.MANAGED_SKILL_DENIES)
        self.assertEqual(strict_json.loads(released[0])["permissions"]["defaultMode"],
                         scope.PERMISSION_DEFAULT_MODE)

        def strip_30(p):
            p["deny"] = [e for e in p["deny"] if e not in named]
            p.pop("defaultMode")

        self._write_settings(mid, strip_30)
        state.atomic_write(self.live(mid) / scope.SKILL_RELPATH, previous)
        problems, attention, _info = self._integrity(mid)
        self.assertEqual(problems, [])
        self.assertIn(drift, attention)
        self.assertNotIn(cli_text.DEFAULT_MODE_DRIFT.format(m8=mid[:8], mid=mid), attention)
        self._repair(mid)
        repaired = strict_json.loads(self._files(mid)[0])
        self.assertNotIn("defaultMode", repaired["permissions"])
        expected = strict_json.loads(released[0])
        expected["permissions"].pop("defaultMode")
        self.assertEqual(repaired, expected)
        released = self._files(mid)  # the 3.1 policy this live scope keeps
        problems, attention, info = self._integrity(mid)
        self.assertEqual((problems, [a for a in attention if "skill" in a]), ([], []))
        self.assertIn(f"Scope: {mid[:8]} OK (lineup generation 1).", info)

        # Rollback (the 3.0.x compile, simulated by its constants): the 3.1
        # scope differs and the repair writes the 3.0 bytes back; the 3.1
        # doctor then reads that scope as lazy state again.
        with mock.patch.object(scope, "MANAGED_SKILL_DENIES", ()), \
                mock.patch.object(scope, "SKILL_MD_BYTES", previous):
            problems, _attention, _info = self._integrity(mid)
            self.assertTrue(any("scope differs from the record-authoritative compile" in line
                                for line in problems), problems)
            self._repair(mid)
            rolled = self._files(mid)
        self.assertNotIn("Skill(", rolled[0].decode())
        self.assertEqual(rolled[1], previous)
        problems, attention, _info = self._integrity(mid)
        self.assertEqual(problems, [])
        self.assertIn(drift, attention)
        self._repair(mid)
        self.assertEqual(self._files(mid), released)
        # Agents never moved in either direction.
        self.assertEqual({k: v for k, v in self.tree(self.live(mid)).items() if k.startswith(".claude/agents/")},
                         agents_before)

        # Unrelated permissions drift, a partial deny set or foreign skill
        # bytes stay BLOCK.
        for label, mutate in (
            ("partial", lambda p: p["deny"].remove("Skill(code-review)")),
            ("foreign deny", lambda p: p["deny"].append("Bash(rm:*)")),
            ("secret deny removed", lambda p: p["deny"].remove(scope.SECRET_PATH_DENIES[0])),
        ):
            with self.subTest(label):
                self._write_settings(mid, mutate)
                problems, attention, _info = self._integrity(mid)
                self.assertTrue(problems, label)
                self.assertNotIn(drift, attention)
                self._repair(mid)
                self.assertEqual(self._files(mid), released)
        state.atomic_write(self.live(mid) / scope.SKILL_RELPATH, b"---\nname: cm\n---\nforeign\n")
        problems, attention, _info = self._integrity(mid)
        self.assertTrue(problems)
        self.assertNotIn(drift, attention)

    def test_malformed_deny_entries_are_block_never_an_exception(self) -> None:
        """A damaged ``permissions.deny`` holding a JSON
        object or array is scope damage (BLOCK), never a TypeError out of the
        skill-policy drift classification."""

        record = self.launch_fresh()
        mid = record["managed_id"]
        drift = cli_text.SKILL_POLICY_DRIFT.format(m8=mid[:8], mid=mid)
        released = self._files(mid)
        for label, entry in (("object", {"malformed": "not-a-rule"}), ("array", ["Skill(code-review)"])):
            for without_skill_denies in (False, True):
                with self.subTest(label, without_skill_denies=without_skill_denies):
                    named = set(scope.MANAGED_SKILL_DENIES)

                    def mutate(p, entry=entry, strip=without_skill_denies):
                        if strip:
                            p["deny"] = [e for e in p["deny"] if e not in named]
                        p["deny"].append(entry)

                    self._write_settings(mid, mutate)
                    problems, attention, _info = self._integrity(mid)
                    self.assertTrue(any("scope differs from the record-authoritative compile" in line
                                        for line in problems), problems)
                    self.assertNotIn(drift, attention)
                    self._repair(mid)
                    self.assertEqual(self._files(mid), released)

    def test_released_30_scope_without_fast_mode_env_is_lazy_state(self) -> None:
        """A running 3.0.x session's proven launch env has no fast-mode
        disable (its process never had one): doctor keeps those launch items
        (Attention, never BLOCK), repair restores the 3.1 skill policy around
        them, and the next resume compiles the disable into the flag settings."""

        record = self.launch_fresh()
        mid = record["managed_id"]
        drift = cli_text.SKILL_POLICY_DRIFT.format(m8=mid[:8], mid=mid)
        live = self.live(mid)
        named = set(scope.MANAGED_SKILL_DENIES)
        path = live / "settings.json"
        document = strict_json.loads(path.read_bytes())
        document["permissions"]["deny"] = [e for e in document["permissions"]["deny"] if e not in named]
        document["permissions"].pop("defaultMode")  # 3.0.x compiled none
        document["env"].pop(scope.FAST_MODE_ENV)
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        state.atomic_write(live / scope.SKILL_RELPATH, scope.PREVIOUS_SKILL_MD_BYTES[0])
        self.mutate(mid, launch_fence=sessions.launch_digest(document, (live / scope.LEAD_SET_JSON).read_bytes()))
        problems, attention, _info = self._integrity(mid)
        self.assertEqual(problems, [])
        self.assertIn(drift, attention)
        self._repair(mid)
        repaired = strict_json.loads(path.read_bytes())
        self.assertNotIn(scope.FAST_MODE_ENV, repaired["env"])
        self.assertIn("Skill(code-review)", repaired["permissions"]["deny"])
        self.assertNotIn("defaultMode", repaired["permissions"])
        self.assertEqual((live / scope.SKILL_RELPATH).read_bytes(), scope.SKILL_MD_BYTES)
        problems, _attention, _info = self._integrity(mid)
        self.assertEqual(problems, [])
        self.resume(mid)
        resumed = strict_json.loads(path.read_bytes())
        self.assertEqual(resumed["env"][scope.FAST_MODE_ENV], "1")
        self.assertEqual(resumed["permissions"]["defaultMode"], scope.PERMISSION_DEFAULT_MODE)

    def test_truthful_302_scope_is_attention_also_after_repair_all_include_live(self) -> None:
        """A truthful 3.0.2-shaped live scope (no skill denies, no
        ``permissions.defaultMode``, the 3.0 /cm bytes) under the current compile
        in a home with no configured layer: Attention, never BLOCK; ``doctor
        --repair-all --include-live`` restores the skill policy and never adds
        the default mode; the next resume decides it from the layers."""

        record = self.launch_fresh()
        mid = record["managed_id"]
        live = self.live(mid)
        path = live / "settings.json"
        named = set(scope.MANAGED_SKILL_DENIES)
        document = strict_json.loads(path.read_bytes())
        document["permissions"] = {"deny": [e for e in document["permissions"]["deny"] if e not in named]}
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        state.atomic_write(live / scope.SKILL_RELPATH, scope.PREVIOUS_SKILL_MD_BYTES[0])
        mode_drift = cli_text.DEFAULT_MODE_DRIFT.format(m8=mid[:8], mid=mid)
        for phase in ("before", "after"):
            with self.subTest(phase):
                problems, attention, _info = self._integrity(mid)
                self.assertEqual(problems, [])
                self.assertNotIn(mode_drift, attention)
                code, output = self._doctor()
                self.assertNotIn("BLOCK", output)
                if phase == "before":
                    self.assertIn(cli_text.SKILL_POLICY_DRIFT.format(m8=mid[:8], mid=mid), attention)
                    out = io.StringIO()
                    with mock.patch("claude_multi.cli.session_facts._live_background_prefixes",
                                    return_value=frozenset()):
                        self.assertEqual(cli.main(["doctor", "--repair-all", "--include-live"],
                                                  runtime=self.runtime, output_stream=out,
                                                  interactive=False), 0, out.getvalue())
                    repaired = strict_json.loads(path.read_bytes())
                    self.assertIn("Skill(code-review)", repaired["permissions"]["deny"])
                    self.assertNotIn("defaultMode", repaired["permissions"])
        self.resume(mid)
        self.assertEqual(strict_json.loads(path.read_bytes())["permissions"]["defaultMode"],
                         scope.PERMISSION_DEFAULT_MODE)

    def _doctor(self) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stderr", io.StringIO()), \
                mock.patch("claude_multi.cli.session_facts._live_background_prefixes", return_value=frozenset()):
            code = cli.main(["doctor"], runtime=self.runtime, output_stream=out, interactive=False)
        return code, out.getvalue()


# ------------------------------------------------------------------ S14


class S14SkillPolicyTests(_RealCase):
    """The compiled deny at the real pinned client (the production compile)."""

    def setUp(self) -> None:
        super().setUp()
        self.permissions = compiled_permissions()
        self.policy = {"model": LEAD_MODEL, "permissions": self.permissions}
        self.negative = {"model": LEAD_MODEL, "permissions": pre_policy(self.permissions)}

    def _pair(self, calls, extra=(), *, probe_id="S14") -> tuple[SkillResponder, SkillResponder]:
        negative = SkillResponder(calls)
        self.run_print(self.negative, negative, extra, probe_id=probe_id)
        policy = SkillResponder(calls)
        self.run_print(self.policy, policy, extra, probe_id=probe_id)
        return negative, policy

    def test_lead_skill_deny_blocks_generic_forks(self) -> None:
        entries = [name for name, _why, _pin in scope.SKILL_POLICY]
        calls = [(name, _arg(index)) for index, name in enumerate(entries)]
        # Ask-gated entries are model-invocable only with an allow: the same
        # CLI allow in both runs makes their negative control discriminating
        # (and shows a CLI allow does not defeat the deny).
        ask = [name for name, _why, pin in scope.SKILL_POLICY if pin == "ask"]
        extra = tuple(item for name in ask for item in ("--allowedTools", f"Skill({name})"))
        negative, policy = self._pair(calls, extra)
        observed = {}
        for index, (name, _arg_marker) in enumerate(calls):
            call_id = f"toolu_s14_lead_{index:03d}"
            forks = (negative.forks.get(_arg(index), 0), policy.forks.get(_arg(index), 0))
            observed[name] = classify_entry(negative.outcome(call_id), policy.outcome(call_id), forks)
            with self.subTest(entry=name):
                self.assertNotEqual(policy.outcome(call_id), LOADED)
                self.assertEqual(policy.forks.get(_arg(index), 0), 0)
        expected = {name: PIN_CLASS[pin] for name, _why, pin in scope.SKILL_POLICY}
        self.assertEqual(observed, expected)
        # The discriminating fork: code-review forks without the policy only.
        self.assertGreaterEqual(negative.forks.get(_arg(entries.index("code-review")), 0), 1)
        self.assertEqual(sum(policy.forks.values()) + policy.unattributed, 0)
        newly = sorted(name for name, value in observed.items() if value == NEWLY_DENIED)
        print(
            "client probe S14: PASS class=S14-model-deny lead: compiled Skill denies refuse the "
            f"model's call ({len(newly)} newly denied: {', '.join(newly)}); upstream user-only "
            f"{sorted(n for n, v in observed.items() if v == UPSTREAM_REFUSED)} and unregistered "
            f"{sorted(n for n, v in observed.items() if v == UNREGISTERED)} classified, never "
            f"counted; pre-policy code-review forks, policy forks 0 {self.binary_label}"
        )

    def test_cm_subagent_skill_deny_blocks_generic_forks(self) -> None:
        agents = state.ensure_private_dir(self.scope / ".claude" / "agents")
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        for role_id in ("cm-analyst", "cm-implementer", "cm-reviewer"):
            role = bundle.roles_v2[role_id]
            spec = profile.RoleSpec(
                id=role_id, function=role["function"], grade=role["grade"],
                prompt_file=role["prompt_file"], isolation=role["isolation"],
                disallowed_tools=tuple(role["disallowed_tools"]), description=role["description"],
                requires=tuple(role["requires"]),
            )
            binding = profile.ResolvedBinding(
                slot=role_id, requested="s14", key="s14", chain=("s14",), named=None, provider="s14",
                family="s14", display="s14", generation="1", mode="client", effort="high",
                selector=AGENT_MODEL, proxy_contract=None, default_effort="high",
                client_context_tokens=200000, provider_context_tokens=200000,
            )
            data = scope._agent_file_v2_bytes(profile.ResolvedAgent(binding, spec, True), bundle.prompt_bodies)
            state.atomic_write(agents / f"{role_id}.md", data)
        # The writer grade needs a git work tree for its worktree isolation.
        subprocess.run(["git", "init", "-q", str(self.fixture.project_dir)], check=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(self.fixture.home)}, timeout=30)
        subprocess.run(["git", "-C", str(self.fixture.project_dir), "-c", "user.email=s14@fixture",
                        "-c", "user.name=s14", "commit", "-q", "--allow-empty", "-m", "s14"],
                       check=True, env={"PATH": "/usr/bin:/bin", "HOME": str(self.fixture.home)}, timeout=30)
        plan = []
        for index, role_id in enumerate(("cm-analyst", "cm-implementer", "cm-reviewer")):
            base = 100 + index * 10
            plan.append((f"S14-AGENT-{index + 1:02d}X",
                         (role_id, [("code-review", _arg(base)), ("simplify", _arg(base + 1))])))

        def run(settings: dict[str, Any]) -> SkillResponder:
            responder = SkillResponder(spawns=plan)
            self.run_print(settings, responder, probe_id="S14-agents", timeout=240)
            for marker in responder.agents:
                self.assertIsNotNone(responder.agents[marker].skill_offered, f"{marker} was not spawned")
            return responder

        negative = run(self.negative)
        policy = run(self.policy)
        by_role = {}
        for index, (marker, (role_id, calls)) in enumerate(plan):
            call_ids = [f"toolu_s14_{marker[10:12]}_{n:03d}" for n in range(len(calls))]
            by_role[role_id] = {
                "skill_offered": (negative.agents[marker].skill_offered, policy.agents[marker].skill_offered),
                "negative": [negative.outcome(c) for c in call_ids],
                "policy": [policy.outcome(c) for c in call_ids],
                "forks": (sum(negative.forks.get(arg, 0) for _s, arg in calls),
                          sum(policy.forks.get(arg, 0) for _s, arg in calls)),
            }
        for role_id in ("cm-analyst", "cm-implementer"):
            with self.subTest(role=role_id):
                row = by_role[role_id]
                self.assertEqual(row["skill_offered"], (True, True))
                self.assertEqual(row["negative"], [LOADED, LOADED])
                self.assertGreaterEqual(row["forks"][0], 1)
                self.assertEqual(row["policy"], [DENIED, DENIED])
                self.assertEqual(row["forks"][1], 0)
        reviewer = by_role["cm-reviewer"]
        # The read-only role has no Skill tool in either run (S13): refused,
        # never forked, and not a proof of the session deny.
        self.assertEqual(reviewer["skill_offered"], (False, False))
        self.assertNotIn(LOADED, reviewer["negative"] + reviewer["policy"])
        self.assertEqual(reviewer["forks"], (0, 0))
        self.assertEqual(policy.unattributed, 0)
        print(
            "client probe S14-agents: PASS class=S14-subagent-deny cm-analyst and cm-implementer keep "
            "Skill; the session "
            "deny refuses their code-review/simplify calls with 0 forks (pre-policy: forks "
            f"{by_role['cm-analyst']['forks'][0]}+{by_role['cm-implementer']['forks'][0]}); "
            f"cm-reviewer has no Skill tool {self.binary_label}"
        )

    def test_permission_mode_allow_matrix_negative_controls(self) -> None:
        cells = [(f"{mode}+{allow}", (*mode_argv, *allow_argv))
                 for mode, mode_argv in MODES for allow, allow_argv in ALLOWS]
        managed = self.scope / "managed-settings.json"
        state.atomic_write(managed, strict_json.canonical_file_bytes(
            {"allowManagedPermissionRulesOnly": True, "permissions": {"allow": ["Skill"]}}))
        cells += list(EXTRA_SURFACES) + [("managed-settings", ("--managed-settings", str(managed)))]
        failures = []
        for label, argv in cells:
            negative, policy = self._pair([("code-review", _arg(0))], argv, probe_id=f"S14-{label}")
            verdict = classify_entry(negative.outcome("toolu_s14_lead_000"), policy.outcome("toolu_s14_lead_000"),
                                     (negative.forks.get(_arg(0), 0), policy.forks.get(_arg(0), 0)))
            if verdict != NEWLY_DENIED or not negative.forks.get(_arg(0)):
                failures.append((label, verdict, negative.forks.get(_arg(0), 0)))
            # A defeating surface must be listed for its launch notice.
            if verdict == DEFEATED:
                self.assertTrue(compiler.skill_policy_bypass_flags(argv), label)
        self.assertEqual(failures, [])
        print(
            f"client probe S14-matrix: PASS class=S14-mode-allow-matrix {len(cells)} cells (8 modes incl. "
            "both bypass spellings, "
            "auto and manual x {no allow, Skill(code-review), Skill}; --allowed-tools, "
            "--allow-dangerously-skip-permissions, --inherit-permission-mode, --setting-sources, "
            "--managed-settings): pre-policy forks in every cell, the deny refuses in every cell; "
            f"no defeating surface {self.binary_label}"
        )

    def test_alias_and_ultra_resolution(self) -> None:
        calls = [("review", _arg(0)), ("code-review", f"ultra {_arg(1)}"), ("ultrareview", _arg(2)),
                 ("checkup", _arg(3)), ("routines", _arg(4))]
        negative, policy = self._pair(calls)
        outcome = [(negative.outcome(f"toolu_s14_lead_{i:03d}"), policy.outcome(f"toolu_s14_lead_{i:03d}"))
                   for i in range(len(calls))]
        # review: an alias of code-review, resolved before the permission check.
        self.assertEqual(outcome[0], (LOADED, DENIED))
        self.assertGreaterEqual(negative.forks.get(_arg(0), 0), 1)
        # ultra: an argument of code-review, covered by the canonical deny.
        self.assertEqual(outcome[1], (LOADED, DENIED))
        # ultrareview is no registered skill name; checkup (doctor) is
        # upstream user-only; routines (schedule) is not registered here.
        self.assertEqual(outcome[2], (UNKNOWN, UNKNOWN))
        self.assertEqual(outcome[3], (USER_ONLY, USER_ONLY))
        self.assertEqual(outcome[4], (UNKNOWN, UNKNOWN))
        self.assertEqual(sum(policy.forks.values()) + policy.unattributed, 0)
        print(f"client probe S14-alias: PASS class=S14-alias-resolution review and code-review ultra "
              f"denied; checkup "
              f"upstream user-only; ultrareview/routines unregistered {self.binary_label}")

    def test_upstream_user_only_is_not_a_negative_control_pass(self) -> None:
        # Default mode, no CLI allow: user-only entries are refused upstream
        # and ask-gated ones by the mode, with or without the deny. Neither
        # is ever classified as a newly denied pass.
        entries = [name for name, _why, pin in scope.SKILL_POLICY if pin in ("user", "ask")]
        calls = [(name, _arg(index)) for index, name in enumerate(entries)]
        negative, policy = self._pair(calls, ("--permission-mode", "default"))
        for index, name in enumerate(entries):
            call_id = f"toolu_s14_lead_{index:03d}"
            verdict = classify_entry(negative.outcome(call_id), policy.outcome(call_id))
            pin = dict((n, p) for n, _w, p in scope.SKILL_POLICY)[name]
            with self.subTest(entry=name):
                self.assertNotEqual(verdict, NEWLY_DENIED)
                self.assertNotEqual(verdict, DEFEATED)
                self.assertEqual(negative.outcome(call_id), USER_ONLY if pin == "user" else ASK_REFUSED)
        self.assertEqual(sum(negative.forks.values()) + sum(policy.forks.values()), 0)
        print(f"client probe S14-user-only: PASS class=S14-upstream-classified {len(entries)} "
              f"user-only/ask-gated entries refused upstream in both runs, never counted as "
              f"newly denied {self.binary_label}")

    def test_user_project_cli_precedence_fresh_resume(self) -> None:
        allow = {"permissions": {"allow": ["Skill(code-review)", "Skill"]}}
        probe.write_fixture_settings(self.fixture, "claude-config/settings.json", allow)
        project = self.fixture.project_dir / ".claude"
        state.ensure_private_dir(project)
        state.atomic_write(project / "settings.json", strict_json.canonical_file_bytes(allow))
        state.atomic_write(project / "settings.local.json", strict_json.canonical_file_bytes(allow))
        session = "5e14c0de-5e14-4c0d-8e14-5e14c0de0001"
        cli_allow = ("--allowedTools", "Skill(code-review)")
        fresh = SkillResponder([("code-review", _arg(0))])
        self.run_print(self.policy, fresh, ("--session-id", session, *cli_allow))
        resumed = SkillResponder([("code-review", _arg(1))])
        self.run_print(self.policy, resumed, ("--resume", session, *cli_allow))
        self.assertEqual(fresh.outcome("toolu_s14_lead_000"), DENIED)
        self.assertEqual(resumed.outcome("toolu_s14_lead_000"), DENIED)
        self.assertEqual(sum(fresh.forks.values()) + sum(resumed.forks.values()), 0)
        # Negative control: the same allow stack without the managed deny forks.
        control = SkillResponder([("code-review", _arg(2))])
        self.run_print(self.negative, control, cli_allow)
        self.assertEqual(control.outcome("toolu_s14_lead_000"), LOADED)
        self.assertGreaterEqual(control.forks.get(_arg(2), 0), 1)
        print("client probe S14-precedence: PASS class=S14-fresh-resume user, project and local "
              "allow rules plus a CLI allow do not defeat the deny on a fresh start or --resume; "
              f"the same allow stack without the deny forks {self.binary_label}")

    def test_nonspawning_cm_and_delegation_positive_controls(self) -> None:
        agents = state.ensure_private_dir(self.scope / ".claude" / "agents")
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        role = bundle.roles_v2["cm-reviewer"]
        spec = profile.RoleSpec(
            id="cm-reviewer", function=role["function"], grade=role["grade"], prompt_file=role["prompt_file"],
            isolation=role["isolation"], disallowed_tools=tuple(role["disallowed_tools"]),
            description=role["description"], requires=tuple(role["requires"]),
        )
        binding = profile.ResolvedBinding(
            slot="cm-reviewer", requested="s14", key="s14", chain=("s14",), named=None, provider="s14",
            family="s14", display="s14", generation="1", mode="client", effort="high", selector=AGENT_MODEL,
            proxy_contract=None, default_effort="high", client_context_tokens=200000,
            provider_context_tokens=200000,
        )
        state.atomic_write(agents / "cm-reviewer.md",
                           scope._agent_file_v2_bytes(profile.ResolvedAgent(binding, spec, True), bundle.prompt_bodies))
        responder = SkillResponder([("dataviz", _arg(0))], spawns=[("S14-AGENT-01X", ("cm-reviewer", []))])
        self.run_print(self.policy, responder)
        # A non-spawning bundled skill still loads; delegation to a bound cm
        # reviewer still spawns.
        self.assertEqual(responder.outcome("toolu_s14_lead_000"), LOADED)
        self.assertIsNotNone(responder.agents["S14-AGENT-01X"].skill_offered)
        self.assertEqual(responder.outcome("toolu_s14_spawn_01"), LOADED)
        # /cm: the 3.1 scope skill bytes (argument hint included) still run
        # their dynamic context at expansion, with the deny in place.
        self._cm_expansion()
        print("client probe S14-controls: PASS class=S14-positive-controls a non-spawning bundled "
              "skill loads, the lead delegates to a bound cm reviewer, /cm expands and runs with "
              f"the deny {self.binary_label}")

    def _cm_expansion(self) -> None:
        skills = state.ensure_private_dir(self.scope / ".claude" / "skills" / "cm")
        state.atomic_write(skills / "SKILL.md", scope.SKILL_MD_BYTES)
        bin_dir = state.ensure_private_dir(self.fixture.root / "bin")
        record = self.fixture.root / "launcher-argv.jsonl"
        body = (
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"open({str(record)!r}, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print('S14-CM-OUT')\n"
        )
        probe.write_fixture_executable(self.fixture, bin_dir, "claude-multi", body)
        path = self.settings_file(self.policy)

        class Ack:
            def respond(self, document, route_path, context):
                if _route(route_path).endswith("/count_tokens"):
                    return 200, {"input_tokens": 1}
                text = "S14-CM-ACK" if "S14-CM-OUT" in json.dumps(document.get("messages")) else "PROBE-OK"
                return 200, _messages_payload([{"type": "text", "text": text}], document, context.ordinal)

        step = probe.PTYInteraction
        steps = (
            step(b"Choose", b"2\r"),
            step(b"Press", b"\r"),
            step("❯".encode("utf-8"), b"/cm "),
            # The client shows the 3.1 argument hint once "/cm " is typed.
            step("profile NAME · set AGENT".encode("utf-8"), b"profiles\r"),
            step(b"S14-CM-ACK", b"/exit\r"),
        )

        def run() -> probe.NativeRunResult:
            with S14Provider(responder=Ack()) as provider:
                return probe.run_native_pty(
                    ("--settings", str(path), "--model", LEAD_MODEL, "--add-dir", str(self.scope)),
                    steps, trusted=self.trusted, fixture=self.fixture, provider=provider, timeout=90,
                    allow_real=True, environ=self.environ, path_dirs=(bin_dir,),
                )

        result = self.guard("S14-cm", run)
        self.assertFalse(result.timed_out)
        calls = [json.loads(line) for line in record.read_text().splitlines() if line.strip()]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["lineup", "--session"])
        self.assertEqual(calls[0][3:], ["profiles"])

    def test_typed_slash_and_plain_client_controls(self) -> None:
        path = self.settings_file(self.policy)

        class Typed:
            """Answer ``S14-TYPED-nn`` requests with ``S14-ACK-nn`` (the PTY barrier)."""

            def __init__(self) -> None:
                self.lock = threading.Lock()
                self.seen: dict[str, set[str]] = {}
                self.tags: set[str] = set()

            def respond(self, document, route_path, context):
                if _route(route_path).endswith("/count_tokens"):
                    return 200, {"input_tokens": 1}
                messages = json.dumps(document.get("messages"))
                found = sorted(set(TYPED_RE.findall(messages)))
                text = f"S14-ACK-{found[-1]}" if found else "PROBE-OK"
                with self.lock:
                    for name in ("code-review", "review", "simplify", "batch"):
                        if f"<command-name>/{name}</command-name>" in messages:
                            self.tags.add(name)
                    for number in found:
                        # The main thread's system prompt carries the appended
                        # marker; a forked skill agent runs on its own.
                        kind = "main" if "S14-LEAD-SYSTEM" in json.dumps(document.get("system")) else "fork"
                        self.seen.setdefault(number, set()).add(kind)
                return 200, _messages_payload([{"type": "text", "text": text}], document, context.ordinal)

        typed = Typed()
        step = probe.PTYInteraction
        steps = (
            step(b"Choose", b"2\r"),
            step(b"Press", b"\r"),
            step("❯".encode("utf-8"), b"/code-review S14-TYPED-01\r"),
            step(b"S14-ACK-01", b"/review S14-TYPED-02\r"),
            step(b"S14-ACK-02", b"/simplify S14-TYPED-03\r"),
            step(b"S14-ACK-03", b"/batch S14-TYPED-04\r"),
            step(b"S14-ACK-04", b"/exit\r"),
        )

        def run() -> probe.NativeRunResult:
            with S14Provider(responder=typed) as provider:
                return probe.run_native_pty(
                    ("--settings", str(path), "--model", LEAD_MODEL, "--add-dir", str(self.scope),
                     "--append-system-prompt", "S14-LEAD-SYSTEM"),
                    steps, trusted=self.trusted, fixture=self.fixture, provider=provider, timeout=150,
                    allow_real=True, environ=self.environ,
                )

        result = self.guard("S14-typed", run)
        self.assertFalse(result.timed_out)
        # Every operator-typed command reached the model with the deny in
        # place; code-review and its alias ran as forks.
        self.assertEqual(sorted(typed.seen), ["01", "02", "03", "04"])
        self.assertIn("fork", typed.seen["01"])
        self.assertIn("fork", typed.seen["02"])
        # /simplify and /batch expanded as skills into the lead's turn.
        self.assertTrue({"simplify", "batch"} <= typed.tags, typed.tags)
        # Plain-client control: without the managed settings no policy is
        # imposed (the model's call forks).
        plain = SkillResponder([("code-review", _arg(0))])
        self.run_print({"model": LEAD_MODEL}, plain)
        self.assertEqual(plain.outcome("toolu_s14_lead_000"), LOADED)
        self.assertGreaterEqual(plain.forks.get(_arg(0), 0), 1)
        print(f"client probe S14-typed: PASS class=S14-typed-run typed /code-review /review /simplify /batch run with "
              f"the deny; plain client unchanged {self.binary_label}")


# ------------------------------------------------------------------ FM


class FastModeTripwireTests(_RealCase):
    """The compiled disable stops the penguin-mode prefetch and fast speed."""

    _runs: dict[bool, dict[str, Any]] = {}

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._runs = {}

    def _tripwire(self) -> tuple[Path, Path, Path, str]:
        """The fixture seeds (fast mode on, a cached org enablement), the TLS
        tripwire certificate and the fixed fake helper key. Once per fixture."""

        if shutil.which("openssl", path="/usr/bin:/bin") is None:
            self.skipTest("BOUNDARY: openssl is needed for the fixture TLS tripwire")
        config = self.fixture.claude_config_dir / ".claude.json"
        document = strict_json.loads(config.read_bytes())
        document["penguinModeOrgEnabled"] = True
        state.atomic_write(config, strict_json.canonical_file_bytes(document))
        tls = state.ensure_private_dir(self.fixture.root / "tls")
        cert, key = tls / "tripwire.pem", tls / "tripwire-key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj",
             "/CN=api.anthropic.com", "-addext", "subjectAltName=DNS:api.anthropic.com",
             "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True, timeout=60, env={"PATH": "/usr/bin:/bin"},
        )
        bin_dir = state.ensure_private_dir(self.fixture.root / "bin")
        helper_key = "s14-fixed-helper-key"
        helper = probe.write_fixture_executable(self.fixture, bin_dir, "s14-helper", f"#!/bin/sh\necho {helper_key}\n")
        return cert, key, helper, helper_key

    def _fm_run(self, seeds: tuple[Path, Path, Path, str], *, process_disable: str | None,
                flag_env: dict[str, str] | None, marker: str, onboarding: bool,
                extra: tuple[str, ...] = (), probe_id: str = "FM") -> dict[str, Any]:
        """One interactive run through the tripwire; metadata only."""

        cert, key, helper, helper_key = seeds
        # A lead model id fast mode applies to (synthetic, never a catalog id).
        model = "s14-opus-5-fixture"
        flag: dict[str, Any] = {"model": model, "apiKeyHelper": str(helper)}
        if flag_env is not None:
            flag["env"] = dict(flag_env)
        settings = self.settings_file(flag)
        ack = f"{marker}-ACK"

        class Reply:
            def respond(self, document, route_path, context):
                if _route(route_path).endswith("/count_tokens"):
                    return 200, {"input_tokens": 1}
                text = ack if marker in json.dumps(document.get("messages")) else "PROBE-OK"
                return 200, _messages_payload([{"type": "text", "text": text}], document, context.ordinal)

        provider = S14Provider(responder=Reply(), tls=(cert, key))
        provider.helper_key = helper_key
        original = probe.ProbeFixture.environ

        def environ(fixture_self, **kwargs):
            env = original(fixture_self, **kwargs)
            # The non-forwarding tripwire is the only route to api.anthropic.com.
            env.update({"HTTPS_PROXY": provider.base_url, "NO_PROXY": "127.0.0.1,localhost",
                        "NODE_EXTRA_CA_CERTS": str(cert)})
            if process_disable is not None:
                env["CLAUDE_CODE_DISABLE_FAST_MODE"] = process_disable
            return env

        step = probe.PTYInteraction
        steps = (
            *((step(b"Choose", b"2\r"), step(b"Press", b"\r")) if onboarding else ()),
            step("❯".encode("utf-8"), marker.encode("ascii") + b"\r"),
            step(ack.encode("ascii"), b"/exit\r"),
        )

        def run() -> probe.NativeRunResult:
            with provider, mock.patch.object(probe.ProbeFixture, "environ", environ):
                return probe.run_native_pty(
                    ("--settings", str(settings), "--model", model, *extra), steps, trusted=self.trusted,
                    fixture=self.fixture, provider=provider, timeout=90, allow_real=True,
                    environ=self.environ, token=None,
                )

        result = self.guard(probe_id, run)
        self.assertFalse(result.timed_out, f"{probe_id}: timed out ({marker})")
        return {
            "penguin": sum(1 for method, path, _key in provider.inner
                           if method == "GET" and path == "/api/claude_code_penguin_mode"),
            "penguin_with_key": sum(1 for method, path, key in provider.inner
                                    if path == "/api/claude_code_penguin_mode" and key),
            # An id-shaped path segment is never printed.
            "keyed_paths": sorted({_redacted(path) for _m, path, key in provider.inner if key}),
            "speeds": list(provider.speeds),
            "connects": sorted(set(provider.connects)),
        }

    def _observe(self, disabled: bool) -> dict[str, Any]:
        if disabled in self._runs:
            return self._runs[disabled]
        _lineup, compiled = test_compiler_v2._seed_launch("balanced")
        value = compiled.env_set.get(scope.FAST_MODE_ENV)
        self.assertEqual(value, "1")
        seeds = self._tripwire()
        probe.write_fixture_settings(self.fixture, "claude-config/settings.json", {"fastMode": True})
        observed = self._fm_run(seeds, process_disable=value if disabled else None, flag_env=None,
                                marker="S14-FM-01", onboarding=True)
        self._runs[disabled] = observed
        return observed

    def test_fast_mode_negative_control_hits_fake_tripwire(self) -> None:
        control = self._observe(False)
        self.assertGreaterEqual(control["penguin"], 1, control)
        self.assertGreaterEqual(control["penguin_with_key"], 1, control)
        self.assertIn("fast", control["speeds"], control)

    def test_fast_mode_fake_messages_omit_speed(self) -> None:
        disabled = self._observe(True)
        self.assertTrue(disabled["speeds"], disabled)
        self.assertEqual(set(disabled["speeds"]), {None}, disabled)

    def test_fast_mode_tls_tripwire_zero_penguin_requests(self) -> None:
        disabled = self._observe(True)
        control = self._observe(False)
        self.assertEqual(disabled["penguin"], 0, disabled)
        self.assertIn("api.anthropic.com:443", disabled["connects"])
        self.assertGreaterEqual(control["penguin"], 1, control)
        # The completion record below states both halves of FM, so it is
        # printed only when this test has itself seen them.
        self.assertTrue(disabled["speeds"], disabled)
        self.assertEqual(set(disabled["speeds"]), {None}, disabled)
        self.assertIn("fast", control["speeds"], control)
        # Residual keyed first-party requests are still reported:
        # the tripwire observes these even with the disable in place.
        residual = [path for path in disabled["keyed_paths"] if path != "/api/claude_code_penguin_mode"]
        print(
            "client probe FM: PASS class=FM-prefetch-off disable=1: penguin-mode GET 0, messages "
            f"speed none; control: penguin-mode GET {control['penguin']} (keyed), speed fast; "
            f"residual keyed first-party paths with the disable: {residual or 'none'} "
            f"{self.binary_label}"
        )

    def _layered_fresh_resume(self, flag_env: dict[str, str] | None, process: str,
                              base: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """Fresh start then ``--resume`` in this test's fixture, with user,
        project and local settings ``env`` stating the fast-mode disable 0."""

        seeds = self._tripwire()
        competing = {"env": {scope.FAST_MODE_ENV: "0"}}
        probe.write_fixture_settings(self.fixture, "claude-config/settings.json",
                                     {"fastMode": True, **competing})
        project = self.fixture.project_dir / ".claude"
        state.ensure_private_dir(project)
        state.atomic_write(project / "settings.json", strict_json.canonical_file_bytes(competing))
        state.atomic_write(project / "settings.local.json", strict_json.canonical_file_bytes(competing))
        session = f"5e14c0de-5e14-4c0d-8e14-5e14c0defa{base:02d}"
        fresh = self._fm_run(seeds, process_disable=process, flag_env=flag_env, marker=f"S14-FM-{base}",
                             onboarding=True, extra=("--session-id", session), probe_id="FM-layers")
        resumed = self._fm_run(seeds, process_disable=process, flag_env=flag_env,
                               marker=f"S14-FM-{base + 1}", onboarding=False, extra=("--resume", session),
                               probe_id="FM-layers")
        return fresh, resumed

    def test_fast_mode_flag_settings_win_over_settings_layers(self) -> None:
        """User, project and local settings ``env`` override
        the process environment, so the compiled flag settings state the
        disable too; it holds on a fresh start and on ``--resume``. The
        control (the same layers and process env, flag settings without the
        disable, in its own fixture) prefetches and sends fast speed, so the
        layers do defeat the process environment alone."""

        _lineup, compiled = test_compiler_v2._seed_launch("balanced")
        process = compiled.env_set.get(scope.FAST_MODE_ENV)
        flag_value = compiled.scope_plan.settings["env"].get(scope.FAST_MODE_ENV)
        self.assertEqual((process, flag_value), ("1", "1"))
        fresh, resumed = self._layered_fresh_resume({scope.FAST_MODE_ENV: flag_value}, process, 10)
        # The control runs in a fresh disposable fixture of its own (the
        # client prefetches at its first start in a config directory).
        self.fixture = probe.build_fixture(self.root / "fixture-control", environ=self.environ)
        self.scope = state.ensure_private_dir(self.fixture.root / "scope")
        probe.seed_fixture_project_trust(self.fixture, self.fixture.project_dir)
        control, control_resumed = self._layered_fresh_resume(None, process, 20)
        for label, observed in (("fresh", fresh), ("resume", resumed)):
            with self.subTest(run=label):
                self.assertEqual(observed["penguin"], 0, observed)
                self.assertIn("api.anthropic.com:443", observed["connects"])
                self.assertTrue(observed["speeds"], observed)
                self.assertEqual(set(observed["speeds"]), {None}, observed)
        self.assertGreaterEqual(control["penguin_with_key"], 1, control)
        self.assertIn("fast", control["speeds"], control)
        self.assertIn("fast", control_resumed["speeds"], control_resumed)
        print(
            "client probe FM-layers: PASS class=FM-flag-settings user/project/local env disable=0 "
            "against flag-settings disable=1: penguin-mode GET 0 and speed none on a fresh start "
            "and on --resume; control without the flag-settings disable: penguin-mode GET "
            f"{control['penguin']} (keyed) fresh, {control_resumed['penguin']} on resume; speed "
            f"fast on both {self.binary_label}"
        )


if __name__ == "__main__":
    unittest.main()
