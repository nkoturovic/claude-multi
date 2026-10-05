"""The consented connection test: one small request per chosen provider,
through the local gateway, after one consent that names every request.

The consent covers what it showed: each target's model, its route and
credential (the transport in effect, the key by name) and the gateway
serving the current setup. All of that is checked again right before each
request; anything that moved refuses that request and the ones after it,
with nothing sent.

Results are not persisted. Each request goes through ``runtime.smoke`` (the
one bounded smoke request; never retried).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from claude_multi import catalog, custom, errors, operator as operator_mod, secret_store
from claude_multi.setup import model, texts


@dataclass(frozen=True)
class TestTarget:
    provider_id: str
    display: str
    alias: str
    upstream: str
    auth_text: str
    account: bool = False
    authority: str = ""  # what the consent covers for this target (see :func:`target_authority`)


@dataclass(frozen=True)
class TestResult:
    provider_id: str
    display: str
    outcome: str  # pass | 401 | 403 | 402 | 404 | 429 | 5xx | timeout | down | other
    ms: int | None
    text: str


@dataclass(frozen=True)
class TestPlan(model.Plan):
    targets: tuple[TestTarget, ...]
    excluded: tuple[str, ...] = field(default=())
    sentinel: str | None = None  # the render the gateway was verified to serve


def render_identity(runtime: Any) -> tuple[str | None, frozenset[str] | None, str]:
    """(sentinel, served ids, problem) of the gateway: the current setup's
    render must be the one on disk and served (the admission smoke's check;
    ``problem`` is empty then)."""

    from claude_multi.cli.commands import models as models_cmd

    return models_cmd.render_identity(runtime)


def _view(runtime: Any) -> tuple[dict[str, Any], dict[str, Any], Any]:
    """(effective docs, transport selections, operator layer): the catalog,
    your providers and the selected transports, as the gateway renders them."""

    snapshot = runtime.operator_snapshot()
    docs = operator_mod.merge_docs(runtime.catalog.docs, snapshot.layer, legacy=custom.load_registry(runtime.environ))
    selections = operator_mod.transport_selections(docs, snapshot.ledger)
    return operator_mod.apply_transports(docs, selections), selections, snapshot.layer


def target_authority(runtime: Any, provider_id: str, alias: str,
                     view: tuple[dict[str, Any], dict[str, Any], Any] | None = None) -> str:
    """A digest of what a request to ``alias`` relies on: the model line, the
    provider's effective route and auth (the transport in effect and whether
    it is approved), its route approval, the key's name, whether it is saved
    and the key file it is read from. Names only; no value is read into it."""

    docs, selections, layer = view if view is not None else _view(runtime)
    provider = docs["providers"]["providers"].get(provider_id)
    line = next((entry for entry in docs["models-v2"]["models"].values()
                 if entry.get("provider") == provider_id and _alias(entry) == alias), None)
    selection = selections.get(provider_id)
    auth = ((provider or {}).get("transport") or {}).get("auth") or {}
    ref = auth.get("secret_ref")
    name = ref.removeprefix("env:") if isinstance(ref, str) else None
    env = runtime.gateway_environ()
    try:
        location = secret_store.key_file_location(env)
        key_file: Any = [location.source, os.path.realpath(location.path)]
        present = None if name is None else secret_store.FileSecretStore(location.path, environ=env).is_set(name)
    except secret_store.SecretStoreError as exc:
        key_file, present = ["unreadable", str(exc)], None
    resolved = layer.providers.get(provider_id)
    facts = {
        "provider": provider,
        "line": line,
        "transport": None if selection is None else [selection.choice, selection.approved, selection.problem],
        "route": [layer.route_status.get(provider_id), resolved.route_digest if resolved is not None else None],
        "secret": name,
        "present": present,
        "key_file": key_file,
    }
    raw = json.dumps(facts, sort_keys=True, default=repr).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _lead_key(runtime: Any) -> str | None:
    from claude_multi.setup import defaults

    try:
        choice = defaults.resolve_default(runtime)
        document = runtime.profiles.load(choice.name)
    except (errors.ClaudeMultiError, OSError, ValueError):
        return None
    lead = document.get("lead")
    return lead.get("model") if isinstance(lead, Mapping) else None


def _alias(entry: Mapping[str, Any]) -> str:
    selectors = catalog.line_selectors(dict(entry))
    chosen = next((item for item in selectors if item[0] == entry.get("default_effort")), selectors[0])
    return operator_mod._selector_base(chosen[1])


def _upstream(provider: Mapping[str, Any]) -> tuple[str, str, bool]:
    """(upstream origin, auth text, account) of an effective provider."""

    transport = provider.get("transport") or {}
    if transport.get("kind") == "oauth-pool":
        pool = str(transport.get("pool"))
        return (texts.SIGNIN_HOSTS.get(pool, pool),
                texts.TEST_AUTH_ACCOUNT.format(kind=texts.ACCOUNT_KINDS.get(pool, f"{pool} account")), True)
    parts = urllib.parse.urlsplit(str(transport.get("base_url", "")))
    origin = f"{parts.scheme}://{parts.netloc}" if parts.netloc else "your server"
    auth = transport.get("auth") or {}
    ref = auth.get("secret_ref")
    if auth.get("kind") == "none" or not isinstance(ref, str):
        return origin, texts.TEST_AUTH_KEYLESS, False
    return origin, texts.TEST_AUTH_KEY.format(name=ref.removeprefix("env:")), False


def plan_test(runtime: Any, provider_ids: Sequence[str]) -> TestPlan:
    """One target per provider: the line the default profile's lead binds,
    else its first served line (key order), at its default effort."""

    view = _view(runtime)
    docs = view[0]
    lines = docs["models-v2"]["models"]
    providers = docs["providers"]["providers"]
    sentinel, served_now, problem = render_identity(runtime)
    served = served_now or frozenset()
    lead = _lead_key(runtime)
    targets: list[TestTarget] = []
    excluded: list[str] = []
    for pid in provider_ids:
        provider = providers.get(pid)
        if provider is None:
            raise model.Refused(texts.KEY_UNKNOWN_PROVIDER.format(id=pid))
        display = str(provider.get("display") or pid)
        candidates = [(key, entry) for key, entry in sorted(lines.items()) if entry.get("provider") == pid]
        chosen = None
        for key, entry in candidates:
            if key == lead and _alias(entry) in served:
                chosen = entry
        if chosen is None:
            chosen = next((entry for _key, entry in candidates if _alias(entry) in served), None)
        if chosen is None:
            excluded.append(texts.TEST_NO_SERVED.format(display=display))
            continue
        upstream, auth_text, account = _upstream(provider)
        alias = _alias(chosen)
        targets.append(TestTarget(pid, display, alias, upstream, auth_text, account,
                                  target_authority(runtime, pid, alias, view)))
    if targets and problem:
        # The gateway does not serve what the consent would describe.
        raise model.Refused(texts.TEST_NOT_CURRENT.format(reason=problem),
                            line=texts.TEST_NOT_CURRENT_LINE.format(reason=problem),
                            fix=model.Fix("P", "claude-multi providers apply"))
    lines_out = [texts.TEST_CONSENT_HEAD.format(n=len(targets))]
    for index, target in enumerate(targets, start=1):
        lines_out += texts.TEST_CONSENT_ITEM.format(i=index, display=target.display, alias=target.alias,
                                                    upstream=target.upstream, auth_text=target.auth_text
                                                    ).rstrip("\n").split("\n")
    lines_out += texts.TEST_CONSENT_TAIL.split("\n")
    digest = model.digest_of("test", [(t.provider_id, t.alias, t.upstream, t.authority) for t in targets],
                             sentinel if targets else None)
    return TestPlan("test", ",".join(provider_ids), tuple(lines_out), "network", True, digest, (),
                    tuple(targets), tuple(excluded), sentinel if targets else None)


def consent_text(plan: TestPlan) -> str:
    return "\n".join(plan.lines)


def classify(target: TestTarget, outcome: Any, ms: int | None) -> TestResult:
    status = getattr(outcome, "http", None)
    reason = str(getattr(outcome, "reason", ""))
    if getattr(outcome, "result", None) == "pass":
        key: Any = "pass"
    elif status in (401, 403):
        key = "account-refused" if target.account else status
    elif status in (402, 404, 429):
        key = status
    elif isinstance(status, int) and 500 <= status < 600:
        key = "5xx"
    elif "wall-clock" in reason or "timed out" in reason.lower() or "timeout" in reason.lower():
        key = "timeout"
    elif status is None and ("refused" in reason.lower() or "connection" in reason.lower()
                             or "no HTTP response" in reason):
        key = "down"
    else:
        key = "other"
    text = texts.TEST_RESULT[key].format(display=target.display, ms=ms, reason=reason)
    return TestResult(target.provider_id, target.display, "account-refused" if key == "account-refused" else str(key),
                      ms, text)


def run_test(runtime: Any, plan: TestPlan, confirmation: model.Confirmation, *,
             on_result: Callable[[TestResult], None] | None = None,
             clock: Callable[[], float] = time.monotonic) -> tuple[TestResult, ...]:
    """Send the planned requests, one per target, no retries."""

    if confirmation.digest != plan.digest or confirmation.consent != plan.consent:
        raise model.Refused(model.CONFIRMATION_MISMATCH)
    from claude_multi.cli import consent

    consent.require_human("providers test", runtime.gateway_environ())
    results: list[TestResult] = []
    for target in plan.targets:
        _still_consented(runtime, plan, target)
        started = clock()
        try:
            outcome = runtime.smoke(target.alias)
        except (errors.ClaudeMultiError, OSError) as exc:
            outcome = operator_mod.SmokeOutcome("fail", None, f"connection: {type(exc).__name__}")
        result = classify(target, outcome, int((clock() - started) * 1000))
        results.append(result)
        if on_result is not None:
            on_result(result)
    return tuple(results)


def _still_consented(runtime: Any, plan: TestPlan, target: TestTarget) -> None:
    """Right before a request: the gateway still serves the render the
    consent saw, the alias is served, and the target's route and credential
    are what the consent named. Otherwise nothing more is sent."""

    sentinel, served, problem = render_identity(runtime)
    if (problem or sentinel != plan.sentinel or target.alias not in (served or ())
            or target_authority(runtime, target.provider_id, target.alias) != target.authority):
        raise model.Refused(texts.TEST_STALE.format(display=target.display))
