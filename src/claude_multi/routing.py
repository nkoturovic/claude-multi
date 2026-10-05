"""Pure routing explanation: intent, compilation and execution stay distinct."""
from __future__ import annotations

from datetime import datetime
import re
from claude_multi import catalog, gateway_events, lineup_log, observations

AGREE = "Recorded and compiled bindings agree; actual upstream execution is unknown."
CANDIDATE = "A matching selector was observed, but it cannot be attributed to this session."
RELOAD = "Binding changed on disk; reload remains unconfirmed. Start a fresh agent after /reload-plugins."


def current_route(selector: str | None, docs: dict, aliases: dict) -> tuple[str | None, str | None, str | None, str | None]:
    base = selector.removesuffix("[1m]") if selector else None
    for key, entry in docs["models"]["models"].items():
        contract = None
        matches = isinstance(entry.get("selector"), str) and entry["selector"].removesuffix("[1m]") == base
        efforts = entry.get("efforts")
        if isinstance(efforts, dict):
            for value in efforts.values():
                if isinstance(value.get("selector"), str) and value["selector"].removesuffix("[1m]") == base:
                    matches, contract = True, value.get("proxy_contract")
        if matches:
            legacy = catalog.is_legacy_custom_entry(entry)
            origin = docs.get("operator-lines", {}).get(key, {}).get("origin", "legacy-custom" if legacy else "catalog")
            return entry.get("provider"), entry.get("wire_model"), contract, origin
    base = selector.removesuffix("[1m]") if selector else None
    entry = aliases.get(base, {})
    if entry:
        return entry.get("provider"), entry.get("wire"), entry.get("proxy_contract"), entry.get("route_origin", "continuity")
    return None, None, None, None


def report(*, record: dict, role: str, binding: dict, available: tuple[str, ...] | None,
           lead_selectors: tuple[str, ...], integrity: str, bindings: lineup_log.BindingLog,
           events: gateway_events.GatewayEvents, route: tuple, route_confirmed: bool,
           now: datetime, current_eligible: bool | None = None,
           explained_role: str | None = None) -> observations.Report:
    """``explained_role``: the binding this
    report explains — ``lead``, the cm-* role, or ``<agent-id> → <role>``."""

    mid = record["managed_id"]
    facts = []
    def add(code, value, source, *, kind="session", subject=mid, observed=None, reason=None):
        facts.append(observations.Fact(code, kind, subject, "known" if value is not None else "unknown", value,
                     observed_at=observed, source=source, reason=reason,
                     coverage="partial" if source in {"journal", "lineup-log"} else "complete"))
    selector = observations.identifier(binding.get("selector"))
    base = selector.removesuffix("[1m]") if selector else None
    # Record keys use the session schema, including retired key@generation.
    key = binding.get("key")
    add("explained-role", explained_role or role, "local")
    add("intent-key", key if isinstance(key, str) and re.fullmatch(r"[a-z0-9][a-z0-9@._-]{0,63}", key) else None, "record")
    for name in ("effort", "selector", "generation"):
        add("intent-" + name, observations.identifier(binding.get(name)), "record")
    add("intent-lineup-generation", record.get("lineup_generation"), "record")
    add("intent-pending", bool(record.get("pending")), "record")
    add("eligibility-record", True, "record", reason="recorded-grant")
    add("eligibility-current", current_eligible, "current-catalog")
    allowed = selector in available if available is not None and selector else None
    category = None
    if allowed:
        category = ("lead" if selector in lead_selectors else "fallback-only" if
                    selector.removesuffix("[1m]") in catalog.REFUSAL_FALLBACK_WIRES else "agent")
    add("fence-allowed", allowed, "scope")
    add("fence-category", category, "scope")
    add("fence-integrity", integrity, "scope")
    add("process-reload", None, "scope", reason="reload-unconfirmed")
    matching = [b for b in bindings.events if role == "lead" and b.event == "apply" or b.role == role or b.agent_id == role]
    for index, event in enumerate(matching):
        subject = f"binding-{index + 1}"
        for code, value in (("binding-label", event.label), ("binding-role", event.role),
                            ("binding-agent", event.agent_id), ("binding-selector", event.selector)):
            add(code, value, "lineup-log", kind="binding", subject=subject, observed=event.timestamp)
    for code, value in zip(("route-provider", "route-wire", "route-contract", "route-origin"), route):
        add(code, observations.identifier(value), "current-route", reason="current-not-historical")
    add("route-confirmed", route_confirmed, "served-models", reason="render-sentinel" if route_confirmed else "declared-route-only")
    add("route-limitations", "This is the current route, not proof of an earlier request.", "current-route")
    candidates = [e for e in events.routes if selector and (e.selector == base or route[1] and e.requested == route[1])]
    for index, event in enumerate(candidates[-256:]):
        subject = f"candidate-{index + 1}"
        for code, value in (("request-id", event.request_id), ("requested-selector", event.selector),
                            ("upstream-reported-model", event.served), ("attribution", "candidate-only")):
            add(code, value, "journal", kind="request", subject=subject, observed=event.timestamp)
        if event.requested and event.served:
            add("substitution", f"The gateway reported substitution: {event.requested} → {event.served}.",
                "journal", kind="request", subject=subject, observed=event.timestamp)
    for index, request in enumerate(events.requests):
        if selector and request.selector == base:
            add("http-status", request.status, "journal", kind="request", subject=f"http-{index + 1}", observed=request.timestamp,
                reason="initial-status-candidate-only")
    conclusion = CANDIDATE if candidates else AGREE if allowed and integrity == "matched" else "Compiled bindings could not be confirmed; actual upstream execution is unknown."
    substitutions = [f.value for f in facts if f.code == "substitution"]
    if substitutions:
        # The reported substitution is the conclusion; its
        # attribution stays candidate-only (the facts above).
        conclusion = substitutions[-1]
    if any(b.selector and b.selector != selector for b in matching):
        conclusion = RELOAD
    add("conclusion", conclusion, "local")
    return observations.Report("explain", now,
             {"records": "complete", "scope": "complete" if integrity == "matched" else "partial",
              "lineup_log": bindings.coverage, "journal": events.coverage}, tuple(facts))


def _value(value: object) -> str:
    return "unknown" if value is None else str(value)


def text(report: observations.Report) -> str:
    """The routing layers with their labels (the JSON keeps the fact codes)."""

    session = {f.code: f.value for f in report.facts if f.subject_kind == "session"}
    mid = next((f.subject_id for f in report.facts if f.subject_kind == "session"), "unknown")
    lines = [f"Routing explanation — session {mid[:8]} · {_value(session.get('explained-role'))}"]

    def grouped(kind: str) -> list[tuple[str, dict[str, observations.Fact]]]:
        groups: dict[str, dict[str, observations.Fact]] = {}
        for fact in report.facts:
            if fact.subject_kind == kind:
                groups.setdefault(str(fact.subject_id), {})[fact.code] = fact
        return list(groups.items())

    def stamp(fact: observations.Fact | None) -> str:
        return f"{observations.instant(fact.observed_at)}  " if fact is not None and fact.observed_at else ""

    current = session.get("eligibility-current")
    lines += ["", "Intent",
              f"  recorded: {_value(session.get('intent-key'))} · effort {_value(session.get('intent-effort'))} · "
              f"selector {_value(session.get('intent-selector'))} · generation {_value(session.get('intent-generation'))}",
              f"  lineup generation: {_value(session.get('intent-lineup-generation'))}",
              f"  pending: {'yes (applies at the next resume)' if session.get('intent-pending') else 'none'}",
              "  eligibility: recorded grant · current "
              + ("unknown" if current is None else "eligible" if current else "not eligible")]
    allowed = session.get("fence-allowed")
    lines += ["", "Fence",
              "  selector: " + ("unknown" if allowed is None else
                                f"allowed as {_value(session.get('fence-category'))}" if allowed else "not allowed"),
              f"  scope integrity: {_value(session.get('fence-integrity'))}",
              "  process reload: unconfirmed"]
    lines += ["", "Binding log"]
    bindings = grouped("binding")
    for _subject, facts in bindings:
        label = facts.get("binding-label")
        who = facts.get("binding-agent") or facts.get("binding-role")
        lines.append(f"  {stamp(label)}{_value(who.value if who else None)}  "
                     f"{_value(label.value if label else None)}")
    if not bindings:
        lines.append("  unknown — no retained observation")
    confirmed = session.get("route-confirmed")
    contract = session.get("route-contract")
    lines += ["", "Gateway route now",
              f"  {_value(session.get('intent-selector'))} → {_value(session.get('route-provider'))} / "
              f"{_value(session.get('route-wire'))}" + (f" · contract {contract}" if contract else ""),
              f"  source: {_value(session.get('route-origin'))}",
              "  served: " + ("confirmed by the render sentinel" if confirmed else "declared route only"),
              f"  {_value(session.get('route-limitations'))}"]
    lines += ["", "Observed execution"]
    observed = [(subject, facts) for subject, facts in grouped("request")]
    for _subject, facts in observed:
        if "http-status" in facts:
            fact = facts["http-status"]
            lines.append(f"  {stamp(fact)}initial HTTP status: {_value(fact.value)} (candidate-only)")
            continue
        first = facts.get("request-id")
        lines.append(f"  {stamp(first)}request: {_value(first.value if first else None)}")
        for code, label in (("requested-selector", "requested selector"),
                            ("upstream-reported-model", "upstream-reported model"),
                            ("attribution", "attribution")):
            fact = facts.get(code)
            lines.append(f"    {label}: {_value(fact.value if fact else None)}")
        if "substitution" in facts:
            lines.append(f"    {facts['substitution'].value}")
    if not observed:
        lines.append("  unknown — no retained observation")
    lines += ["", "Conclusion", f"  {_value(session.get('conclusion'))}"]
    lines.append("Coverage: " + ", ".join(f"{k}={v}" for k, v in report.coverage.items()))
    return "\n".join(lines) + "\n"
