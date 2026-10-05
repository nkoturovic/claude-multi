"""Bounded, backend-neutral gateway log observations.

Sources supply transient messages, optional cursors, normalized timestamps and
instance identities. Only the closed credential-save metadata survives collection.
A bounded window is never proof of a complete backup-retirement interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Iterable

from claude_multi.platform.observation import LogRecord, LogWindow, LOG_MAX_RECORDS, LOG_MAX_MESSAGE

MAX_RECORDS = LOG_MAX_RECORDS
MAX_EVENTS = 256
MAX_MESSAGE = LOG_MAX_MESSAGE


@dataclass(frozen=True)
class CredentialSave:
    timestamp: datetime | None
    gateway_instance: str | None
    source_cursor: str | None
    operation: str
    result: str
    provider: str
    auth_index: str
    credentials_changed: bool
    stage: str
    errno: int
    category: str
    bytes: int
    size: int
    generation: int
    epoch: int


@dataclass(frozen=True)
class CredentialSaves:
    events: tuple[CredentialSave, ...]
    coverage: str


# Patch 8's grep-pinned credentialSaveEventFormat; fullmatch is intentional.
_EVENT = re.compile(
    r"credential_save_v1 operation=(refresh|prepare|update|result|reconcile|register|reset) "
    r"result=(persisted|failed|unchanged|skipped|unverified) "
    r"provider=([a-z0-9][a-z0-9._-]{0,63}) auth_index=([0-9a-f]{16}|invalid) "
    r"credentials_changed=(true|false) "
    r"stage=(none|validate|path|mkdir|read|marshal|open|write|sync|close|rename|dirsync|delegated|store) "
    r"errno=([0-9]{1,10}) "
    r"category=(none|errno|short_write|error|no_store|config_api_key|runtime_only|plugin_virtual|"
    r"no_metadata|stale_generation|skip_persist|disabled_absent|commit_declined|no_sync|"
    r"delegated|store|dirsync_unsupported|superseded) "
    r"bytes=([0-9]{1,20}) size=([0-9]{1,20}) generation=([0-9]{1,20}) epoch=([0-9]{1,20})"
)
_ENVELOPE = re.compile(
    r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] \[[^\]\r\n]*\] \[[^\]\r\n]*\] "
    r"(?:\[[^\]\r\n]*\] )?(.*)$"
)


def collect_credential_saves(window: LogWindow, *, providers: Iterable[str]) -> CredentialSaves:
    """Keep strict metadata only, in source order; malformed events reduce coverage.

    Provider membership, not just its character shape, prevents filenames and
    account labels masquerading as a provider. Unknown indices are explicit and
    cannot qualify for retirement. Generation/epoch belong to the auth within an
    instance; they must never be compared across gateway restarts.
    """
    allowed = frozenset(providers) | {"invalid"}
    coverage = window.coverage
    events: list[CredentialSave] = []
    seen = set()
    for record in window.records[:MAX_RECORDS]:
        if record.source_cursor is not None:
            if record.source_cursor in seen:
                continue
            seen.add(record.source_cursor)
        if "credential_save_v1" not in record.message:
            continue
        message = record.message
        if len(message) <= MAX_MESSAGE:
            envelope = _ENVELOPE.fullmatch(message)
            match = _EVENT.fullmatch(envelope[1] if envelope else message)
        else:
            match = None
        if match is None or match[3] not in allowed:
            coverage = "incomplete"
            continue
        op, result, provider, index, changed, stage, err, category, count, size, gen, epoch = match.groups()
        if result in {"unchanged", "skipped"} and op != "refresh":
            coverage = "incomplete"
            continue
        if record.timestamp is None or record.gateway_instance is None or index == "invalid" or provider == "invalid":
            coverage = "incomplete"
        events.append(CredentialSave(
            record.timestamp, record.gateway_instance, record.source_cursor,
            op, result, provider, index, changed == "true", stage, int(err), category,
            int(count), int(size), int(gen), int(epoch),
        ))
        if len(events) > MAX_EVENTS:
            del events[0]
            coverage = "truncated"
    if len(window.records) > MAX_RECORDS:
        coverage = "truncated"
    return CredentialSaves(tuple(events), coverage)


# Inference requests, routing candidates and OAuth failures share the
# source pass with credential receipts. Never retain auth=, session prefixes,
# free-form errors, IPs or URLs. A final HTTP log is not an upstream attempt.
@dataclass(frozen=True)
class RouteObservation:
    timestamp: datetime | None
    gateway_instance: str | None
    source_cursor: str | None
    request_id: str | None
    selector: str | None = None
    provider: str | None = None
    requested: str | None = None
    served: str | None = None


@dataclass(frozen=True)
class Request:
    timestamp: datetime | None
    gateway_instance: str | None
    source_cursor: str | None
    request_id: str | None
    endpoint: str  # inference | models | count_tokens | websocket | other | unknown
    status: int
    selector: str | None = None
    provider: str | None = None
    # The installed logger supplies no safe exact session join.
    session_id: str | None = None


@dataclass(frozen=True)
class OAuthFailure:
    timestamp: datetime | None
    gateway_instance: str | None
    source_cursor: str | None
    pool: str | None


@dataclass(frozen=True)
class GatewayEvents:
    requests: tuple[Request, ...]
    routes: tuple[RouteObservation, ...]
    oauth_failures: tuple[OAuthFailure, ...]
    credential_saves: CredentialSaves
    coverage: str
    incomplete: int = 0
    source_coverage: str = "bounded"
    timed_out: bool = False
    oldest_at: datetime | None = None


INFERENCE_PATHS = frozenset({
    "/v1/messages", "/v1/chat/completions", "/v1/completions", "/v1/responses",
    "/v1/responses/compact", "/backend-api/codex/responses", "/backend-api/codex/responses/compact",
})
_REQUEST_ENVELOPE = re.compile(
    r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] \[([^\]\r\n]*)\] \[[^\]\r\n]*\] "
    r"(?:\[[^\]\r\n]*\] )?(.*)$"
)
_HTTP = re.compile(r'^([1-5]\d\d) \|\s*[^|\r\n]{1,32}\|\s*[^|\r\n]{1,64}\|\s*(GET|POST|HEAD|PUT|DELETE|OPTIONS|PATCH)\s+"([^"\r\n]{1,2048})"(?: .*)?$')
_MODEL = re.compile(r'(?:^|\s)model=([^\s]+)')
_PROVIDER = re.compile(r'(?:^|\s)provider=([a-z0-9_-]+)(?:\s|$)')
_SUBSTITUTION = re.compile(r'^[A-Za-z0-9_-]+ executor: upstream served model "([^"\\]*)" for requested model "([^"\\]*)"(?: \(auth_index=[^\r\n]*\))?$')


def collect(window: LogWindow, *, providers: Iterable[str]) -> GatewayEvents:
    """Minimize a backend-neutral window, with conservative request joins.

    A reused request id, missing instance or conflicting selector is never
    resolved by guesswork. Cursor deduplication precedes every collector. Even
    a successful empty bounded read is partial, not retained-history proof.
    """
    from dataclasses import replace
    from claude_multi.observations import identifier

    allowed = frozenset(providers)
    seen = set()
    records = []
    coverage = "unavailable" if window.coverage == "unavailable" else "partial"
    incomplete = 0
    for row in window.records[:MAX_RECORDS]:
        if row.source_cursor is not None:
            if row.source_cursor in seen:
                continue
            seen.add(row.source_cursor)
        records.append(row)
    clean = LogWindow(tuple(records), window.coverage)
    saves = collect_credential_saves(clean, providers=allowed)
    requests, routes, failures = [], [], []
    for row in records:
        if len(row.message) > MAX_MESSAGE:
            incomplete += 1
            continue
        if "credential_save_v1" in row.message:
            continue
        envelope = _REQUEST_ENVELOPE.fullmatch(row.message)
        if not envelope:
            continue
        rid, message = envelope.groups()
        rid = rid if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", rid) else None
        if row.timestamp is None or row.gateway_instance is None:
            incomplete += 1
        common = (row.timestamp, row.gateway_instance, row.source_cursor, rid)
        http = _HTTP.fullmatch(message)
        if http:
            code, method, path = http.groups()
            path = path.split("?", 1)[0]  # never retain arbitrary paths/queries
            endpoint = ("inference" if method == "POST" and path in INFERENCE_PATHS else
                        "models" if method == "GET" and path == "/v1/models" else
                        "count_tokens" if path == "/v1/messages/count_tokens" else
                        "websocket" if method == "GET" and int(code) == 101 else "other")
            requests.append(Request(*common, endpoint, int(code)))
        elif re.match(r"^[1-5]\d\d \|", message) and "upstream execution failed:" not in message:
            # Status-like but not a recognizable final HTTP row, never totalled.
            incomplete += 1
        model = _MODEL.search(message)
        provider = _PROVIDER.search(message)
        selector = identifier(model[1]) if model else None
        pool = provider[1] if provider and provider[1] in allowed else None
        substitution = _SUBSTITUTION.fullmatch(message)
        requested = identifier(substitution[2]) if substitution else None
        served = identifier(substitution[1]) if substitution else None
        if selector or requested or served:
            routes.append(RouteObservation(*common, selector, pool, requested, served))
        if "invalid_grant" in message:
            lowered = message.lower()
            oauth_pool = "codex" if "codex" in lowered else "claude" if ("claude" in lowered or "anthropic" in lowered) else None
            failures.append(OAuthFailure(row.timestamp, row.gateway_instance, row.source_cursor, oauth_pool))
    grouped = {}
    for request in requests:
        key = (request.gateway_instance, request.request_id)
        grouped[key] = grouped.get(key, 0) + 1
    route_groups = {}
    for route in routes:
        route_groups.setdefault((route.gateway_instance, route.request_id), []).append(route)
    joined = []
    for request in requests:
        key = (request.gateway_instance, request.request_id)
        candidates = []
        if all(key) and request.timestamp is not None and grouped[key] == 1:
            candidates = [r for r in route_groups.get(key, ()) if r.timestamp is not None
                          and 0 <= (request.timestamp - r.timestamp).total_seconds() <= 600]
        selectors = {r.selector for r in candidates if r.selector}
        pools = {r.provider for r in candidates if r.provider}
        joined.append(replace(request, selector=next(iter(selectors)) if len(selectors) == 1 else None,
                              provider=next(iter(pools)) if len(pools) == 1 else None))
    oldest = min((row.timestamp for row in records if row.timestamp is not None), default=None)
    return GatewayEvents(tuple(joined), tuple(routes), tuple(failures), saves, coverage, incomplete,
                         window.coverage, window.timed_out, oldest)


def recovery_hold(saves: CredentialSaves) -> bool:
    """Backend-neutral restart veto, never backup-retirement authorization.

    A source adapter must cover the entire switch interval without truncation.
    Only a later, same-instance/identity, non-superseded save can repair a
    failure. ``unverified + changed`` repairs restart safety, NOT durability.
    An operator hold and legacy/stat suspicion remain adapter-owned vetoes.
    """
    if saves.coverage != "bounded":
        return True
    return bool(unrepaired_failures(saves))


def unrepaired_failures(saves: CredentialSaves) -> tuple[CredentialSave, ...]:
    """The failed saves of ``saves`` that no later save repaired (the repair
    rule of :func:`recovery_hold`); a failure without a complete identity is
    never repaired. Coverage is the caller's to judge."""

    unrepaired = []
    for failed in saves.events:
        if failed.result != "failed":
            continue
        if failed.gateway_instance is None or failed.timestamp is None or failed.auth_index == "invalid":
            unrepaired.append(failed)
            continue
        repaired = any(
            later.gateway_instance == failed.gateway_instance
            and later.provider == failed.provider and later.auth_index == failed.auth_index
            and later.timestamp is not None and later.timestamp > failed.timestamp
            and later.epoch >= failed.epoch and later.generation >= failed.generation
            and (later.result == "persisted" or
                 (later.result == "unverified" and later.credentials_changed
                  and later.category != "superseded"))
            for later in saves.events
        )
        if not repaired:
            unrepaired.append(failed)
    return tuple(unrepaired)
