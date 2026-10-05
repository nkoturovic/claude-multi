"""Observed client HTTP requests, not tokens, billing or upstream attempts."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import re

from claude_multi import errors, gateway_events, observations, quota

CAVEAT = "This is not token usage, billing, or a count of upstream retry attempts."
# Kept verbatim.
STREAM_CAVEAT = ("Statuses are those sent before the response began; a stream that failed later counts "
                 "as its initial status.")


def parse_since(value: str, now: datetime) -> datetime:
    match = re.fullmatch(r"([1-9][0-9]{0,5})([mhd])", value)
    if match:
        seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
        start = now - timedelta(seconds=seconds)
    elif re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", value):
        start = quota.parse_time(value)
    else:
        start = None
    if start is None or not timedelta(0) <= now - start <= timedelta(days=30):
        raise errors.CLIError("usage: --since needs an RFC 3339 instant or duration within the past 30 days")
    return start


def report(events: gateway_events.GatewayEvents, start: datetime, end: datetime,
           session: str | None = None) -> observations.Report:
    available = events.coverage != "unavailable"
    facts = []
    def add(code, value, *, subject="gateway", kind="gateway", status=None):
        facts.append(observations.Fact(code, kind, subject,
                     status or ("known" if value is not None else "unknown"), value,
                     observed_at=end, source="journal", coverage=events.coverage,
                     reason="observed-only"))
    add("source-state", "timed-out" if events.timed_out else events.source_coverage)
    add("range-start", observations.instant(start))
    add("range-end", observations.instant(end))
    requests = [r for r in events.requests if r.timestamp is not None and start <= r.timestamp <= end]
    inference = [r for r in requests if r.endpoint == "inference"]
    add("requests", len(inference) if available else None, status="known" if available else "unavailable")
    add("incomplete", events.incomplete if available else None)
    for endpoint in ("models", "count_tokens", "websocket", "other"):
        add("excluded-" + endpoint.replace("_", "-"), sum(r.endpoint == endpoint for r in requests) if available else None)
    groups = {}
    for request in inference:
        groups.setdefault((request.provider or "unknown", request.selector or "unknown"), []).append(request)
    for index, ((provider, selector), rows) in enumerate(sorted(groups.items())):
        subject = f"bucket-{index + 1}"
        for code, value in (("provider", provider), ("selector", selector), ("requests", len(rows)),
                            ("attribution", "unknown")):
            add(code, value, subject=subject, kind="usage-bucket")
        counts = Counter(r.status for r in rows)
        for code in (429, 402, 403):
            add(f"http-{code}", counts[code], subject=subject, kind="usage-bucket")
        for category in range(1, 6):
            add(f"http-{category}xx", sum(n for c, n in counts.items() if c // 100 == category),
                subject=subject, kind="usage-bucket")
    if session is not None:
        # No shipped source attests exact session identity. Even one matching
        # local prefix is not a join; global totals must stay separate.
        add("session-requests", None, subject=session, kind="session")
    add("usage-limitations", CAVEAT)
    add("stream-limitations", STREAM_CAVEAT)
    return observations.Report("usage", end, {"journal": events.coverage}, tuple(facts))


def text(report: observations.Report) -> str:
    global_values = {f.code: f.value for f in report.facts if f.subject_kind == "gateway"}
    coverage = report.coverage["journal"]
    lines = ["Usage — observed client HTTP requests",
             f"Range: {global_values['range-start']} to {global_values['range-end']}",
             f"Coverage: {coverage} · source {global_values['source-state']} · no claim is made about requests outside retained journal coverage.",
             CAVEAT, STREAM_CAVEAT]
    count = global_values["requests"]
    lines.append("Requests: unknown — gateway journal unavailable." if count is None else f"Observed requests: {count}")
    for f in report.facts:
        if f.code == "session-requests":
            lines.extend([f"Session {f.subject_id[:8]}: request count unknown — available gateway metadata does not identify this session exactly.",
                          "Gateway-wide totals are shown separately; they are not this session's usage."])
    lines.append("provider  selector  requests  429  402  403  attribution")
    groups = {}
    for f in report.facts:
        if f.subject_kind == "usage-bucket":
            groups.setdefault(f.subject_id, {})[f.code] = f.value
    for row in groups.values():
        lines.append("  ".join(str(row[k]) for k in ("provider", "selector", "requests", "http-429", "http-402", "http-403", "attribution")))
    for key in ("excluded-models", "excluded-count-tokens", "excluded-websocket", "excluded-other", "incomplete"):
        lines.append(f"{key}: {global_values[key] if global_values[key] is not None else 'unknown'}")
    return "\n".join(lines) + "\n"
