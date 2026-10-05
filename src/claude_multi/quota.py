"""Minimized, passive quota observations and pure presentation."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Iterable

from . import errors, strict_json

AUTH_FILES_PATH = "/v0/management/auth-files"
MAX_BODY_BYTES = 1 << 20
MAX_FILES = 256
HIGH_PERCENT = 90.0
CACHE_TTL_SECONDS = 60.0
LIMITS = strict_json.JSONLimits(max_bytes=MAX_BODY_BYTES, max_depth=16,
                                max_collection=4096, max_string=65536)
REASONS = {
    "unauthorized": "unauthorized", "invalid_grant": "invalid_grant",
    "token expired": "token_expired", "payment_required": "payment_required",
    "quota exhausted": "quota_exhausted", "not_found": "not_found",
    "transient upstream error": "transient", "request failed": "request_failed",
    "cloudflare challenge": "cloudflare",
}
STATES = frozenset({"ok", "no-key", "disabled", "key-unusable", "pending",
                    "management-off", "mismatch", "refused", "malformed",
                    "error", "down", "not-ours", "seam", "unavailable"})
# By-design states with nothing to fix: information, never a remedy.
INFO_STATES = frozenset({"no-key", "disabled", "unavailable"})


class QuotaParseError(errors.ClaudeMultiError, ValueError):
    """A structural error, never containing response values or JSON paths."""


@dataclass(frozen=True)
class Window:
    label: str
    used_percent: float
    resets_at: datetime | None
    length: timedelta | None


PLAN_TYPES = frozenset({"free", "go", "plus", "pro", "team", "business", "enterprise", "edu", "k12"})
PROVIDERS = frozenset({"claude", "codex", "gemini", "gemini-cli", "antigravity", "qwen", "iflow", "kimi"})


@dataclass(frozen=True)
class QuotaMetadata:
    has_credits: bool | None = None
    unlimited: bool | None = None
    balance: str | None = None  # exact bounded decimal, provider credits (not money)
    plan_type: str | None = None
    retry_after: datetime | None = None  # HTTP timing, not scheduler cooldown


@dataclass(frozen=True)
class ModelQuota:
    model: str
    observed_at: datetime | None
    windows: tuple[Window, ...]
    metadata: QuotaMetadata


@dataclass(frozen=True)
class Credential:
    handle: str
    provider: str
    status: str
    reason: str | None
    disabled: bool | None
    unavailable: bool | None
    next_retry_after: datetime | None
    success: int | None
    failed: int | None
    observed_at: datetime | None
    windows: tuple[Window, ...]
    metadata: QuotaMetadata = QuotaMetadata()
    model_quotas: tuple[ModelQuota, ...] = ()
    plan_type: str | None = None
    plan_source: str | None = None


@dataclass(frozen=True)
class PoolStatus:
    state: str
    http_status: int | None = None
    read_at: datetime | None = None
    credentials: tuple[Credential, ...] = ()
    key_problem: str | None = None
    remedy: str | None = None
    key_file: str | None = None


@dataclass(frozen=True)
class Finding:
    level: str
    credential: Credential
    window: Window | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        value = re.sub(r"(\.\d{6})\d+", r"\1", value)
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _number(value: object, pattern: str, maximum: float) -> float | None:
    if not isinstance(value, str) or not re.fullmatch(pattern, value.strip(), re.ASCII):
        return None
    number = float(value.strip())
    return number if number <= maximum else None


def _epoch(value: object) -> datetime | None:
    seconds = _number(value, r"\d{9,11}", 99999999999)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _windows(provider: str, raw: object, observed: datetime | None) -> tuple[Window, ...]:
    if not isinstance(raw, dict):
        return ()
    signals = {key.lower(): value.strip() for key, value in raw.items()
               if isinstance(value, str)}
    windows = []
    if provider == "claude":
        for label, length in (("5h", timedelta(hours=5)), ("7d", timedelta(days=7))):
            prefix = f"anthropic-ratelimit-unified-{label}-"
            fraction = signals.get(prefix + "utilization")
            if _number(fraction, r"\d{1,2}(\.\d{1,6})?", 10) is not None:
                reset = signals.get(prefix + "reset")
                # Exact decimal scaling: binary 0.29 * 100 would floor to 28%.
                percent = float(Decimal(fraction) * 100)
                windows.append(Window(label, percent, _epoch(reset) or parse_time(reset), length))
    elif provider == "codex":
        for name in ("primary", "secondary"):
            prefix = f"x-codex-{name}-"
            used = _number(signals.get(prefix + "used-percent"), r"\d{1,4}(\.\d{1,3})?", 1000)
            if used is None:
                continue
            minutes = _number(signals.get(prefix + "window-minutes"), r"\d{1,7}", 9999999)
            label, length = name, None
            if minutes:
                count = int(minutes)
                label = (f"{count // 1440}d" if count % 1440 == 0 else
                         f"{count // 60}h" if count % 60 == 0 else f"{count}m")
                length = timedelta(minutes=count)
            reset = _epoch(signals.get(prefix + "reset-at"))
            after = _number(signals.get(prefix + "reset-after-seconds"), r"\d{1,9}", 999999999)
            if reset is None and after is not None and observed is not None:
                try:
                    reset = observed + timedelta(seconds=after)
                except OverflowError:
                    pass
            windows.append(Window(label, used, reset, length))
    return tuple(windows)


def retry_after(value: object, observed: datetime | None) -> datetime | None:
    if not isinstance(value, str) or len(value) > 128 or observed is None:
        return None
    try:
        if re.fullmatch(r"[0-9]{1,10}", value):
            seconds = int(value)
            return observed + timedelta(seconds=seconds) if seconds <= 2**31 - 1 else None
        # HTTP-date, never ISO timestamps, naive dates, negatives or free text.
        if not re.fullmatch(r"[A-Z][a-z]{2}, [0-9]{2} [A-Z][a-z]{2} [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT", value):
            return None
        result = parsedate_to_datetime(value)
        return result.astimezone(timezone.utc) if result.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _metadata(provider: str, raw: object, observed: datetime | None) -> QuotaMetadata:
    if not isinstance(raw, dict) or provider not in {"claude", "codex"}:
        return QuotaMetadata()
    signals = {key.lower(): value for key, value in raw.items() if isinstance(key, str)}
    def boolean(name):
        value = signals.get("x-codex-credits-" + name)
        return {"true": True, "false": False}.get(value) if isinstance(value, str) else None
    balance = signals.get("x-codex-credits-balance")
    if not isinstance(balance, str) or not re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,6})?", balance) or Decimal(balance) > 10**15:
        balance = None
    plan = signals.get("x-codex-plan-type")
    plan = plan if isinstance(plan, str) and plan in PLAN_TYPES else None
    timing = retry_after(signals.get("retry-after"), observed)
    return QuotaMetadata(boolean("has-credits"), boolean("unlimited"), balance, plan, timing) if provider == "codex" else QuotaMetadata(retry_after=timing)


def _model_quotas(provider: str, value: object) -> tuple[ModelQuota, ...]:
    if not isinstance(value, dict) or len(value) > 256:
        return ()
    result = []
    for model, raw in value.items():
        # IDs may contain no path, email or raw account-bearing text. Empty
        # unrecognized entries do not retain an advisory identifier at all.
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,255}", model) or not isinstance(raw, dict):
            continue
        observed = parse_time(raw.get("observed_at"))
        windows = _windows(provider, raw.get("signals"), observed)
        metadata = _metadata(provider, raw.get("signals"), observed)
        if observed is not None or windows or metadata != QuotaMetadata():
            result.append(ModelQuota(model, observed, windows, metadata))
    return tuple(result)


def parse_auth_files(body: bytes) -> tuple[Credential, ...]:
    try:
        document = strict_json.loads(body, limits=LIMITS)
    except (ValueError, UnicodeError, RecursionError):
        raise QuotaParseError("invalid auth-files JSON") from None
    if not isinstance(document, dict):
        raise QuotaParseError("auth-files must be an object")
    files = document.get("files")
    if not isinstance(files, list):
        raise QuotaParseError("auth-files files must be a list")
    if len(files) > MAX_FILES:
        raise QuotaParseError("too many auth-files entries")
    credentials, counts = [], {}
    for entry in files:
        if not isinstance(entry, dict):
            raise QuotaParseError("auth-files entry must be an object")
        provider = entry.get("provider")
        provider = provider.lower() if isinstance(provider, str) else "other"
        if provider not in PROVIDERS:
            provider = "other"
        counts[provider] = counts.get(provider, 0) + 1
        status = entry.get("status")
        if not isinstance(status, str) or status not in {
            "active", "pending", "refreshing", "error", "disabled", "unknown"
        }:
            status = "other"
        message = entry.get("status_message")
        message = message.strip().casefold() if isinstance(message, str) else ""
        reason = REASONS.get(message, "other") if message else None
        raw_quota = entry.get("quota")
        raw_quota = raw_quota if isinstance(raw_quota, dict) else {}
        observed = parse_time(raw_quota.get("observed_at"))
        def counter(name):
            value = entry.get(name)
            return value if type(value) is int and 0 <= value < 2**53 else None
        metadata = _metadata(provider, raw_quota.get("signals"), observed)
        claim = entry.get("id_token")
        claim = claim.get("plan_type") if provider == "codex" and isinstance(claim, dict) else None
        claim = claim if isinstance(claim, str) and claim in PLAN_TYPES else None
        credentials.append(Credential(
            f"{provider}#{counts[provider]}", provider, status, reason,
            entry.get("disabled") if type(entry.get("disabled")) is bool else None,
            entry.get("unavailable") if type(entry.get("unavailable")) is bool else None,
            parse_time(entry.get("next_retry_after")), counter("success"), counter("failed"),
            observed, _windows(provider, raw_quota.get("signals"), observed),
            metadata, _model_quotas(provider, entry.get("model_quotas")),
            metadata.plan_type or claim,
            "reported header" if metadata.plan_type else "reported claim" if claim else None,
        ))
    return tuple(credentials)


def window_current(window: Window, observed_at: datetime | None, now: datetime) -> bool:
    if observed_at is None or observed_at > now or now - observed_at >= (window.length or timedelta(hours=5)):
        return False
    if window.resets_at is not None:
        return window.resets_at > now
    return now - observed_at < (window.length or timedelta(hours=5))


def classify(credential: Credential, now: datetime) -> Finding:
    c = credential
    current = [w for w in c.windows if window_current(w, c.observed_at, now)]
    window = max(current, key=lambda w: w.used_percent, default=None)
    if c.disabled or c.status == "disabled":
        level = "disabled"
    elif c.reason in ("unauthorized", "invalid_grant"):
        level = "login"
    elif c.reason == "token_expired" or (c.status == "error" and c.unavailable
                                           and c.reason in (None, "other")):
        level = "unusable"
    elif c.reason == "payment_required":
        level = "payment"
    elif (c.reason == "quota_exhausted" and c.observed_at is not None and
          0 <= (now - c.observed_at).total_seconds() < 5 * 3600 and
          (window is not None or not c.windows)):
        level = "exhausted"
    elif window is not None and window.used_percent >= HIGH_PERCENT:
        level = "high"
    else:
        level = "ok" if window is not None else "no-data"
    return Finding(level, c, window)


def worst(credentials: Iterable[Credential], now: datetime) -> Finding | None:
    levels = ("disabled", "no-data", "ok", "high", "exhausted", "payment", "unusable", "login")
    findings = [classify(c, now) for c in credentials]
    # Stable sort: equal severities/percentages keep response (handle) order.
    return max(findings, key=lambda f: (levels.index(f.level),
               f.window.used_percent if f.window and f.level in ("ok", "high") else 0), default=None)


def format_age(delta: timedelta, *, compact: bool = False) -> str:
    seconds = max(0, delta.total_seconds())
    if seconds < 60:
        return "now" if compact else "just now"
    number, unit = (int(seconds // 60), "m") if seconds < 3600 else (
        (int(seconds // 3600), "h") if seconds < 172800 else (int(seconds // 86400), "d"))
    return f"{number}{unit}" if compact else f"{number} {'min' if unit == 'm' else unit} ago"


def format_reset(when: datetime, now: datetime, tz: tzinfo | None = None) -> str:
    local, today = when.astimezone(tz), now.astimezone(tz)
    days = (local.date() - today.date()).days
    if days == 0:
        return local.strftime("%H:%M")
    if 0 < days <= 6:
        return ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[local.weekday()] + local.strftime(" %H:%M")
    return local.strftime("%Y-%m-%d %H:%M")


def observation_age(credential: Credential, now: datetime, *, compact: bool = False) -> str:
    if credential.observed_at is None:
        return "age unknown"
    return format_age(now - credential.observed_at, compact=compact)


def window_text(window: Window, credential: Credential, now: datetime, tz: tzinfo | None = None) -> str:
    if not window_current(window, credential.observed_at, now):
        return f"{window.label} reset since the observation" if credential.observed_at else f"{window.label} observation age unknown"
    percent = math.floor(window.used_percent)
    mark = "!" if window.used_percent >= HIGH_PERCENT else ""
    reset = f"resets {format_reset(window.resets_at, now, tz)}" if window.resets_at else "reset unknown"
    return f"{window.label} {percent}%{mark} ({reset})"


def credential_text(credential: Credential, *, login_command: str | None, now: datetime,
                    tz: tzinfo | None = None, provider: str | None = None, surface: str = "providers") -> str:
    """One credential's line; its quota remedy is :func:`guidance` for the
    ``provider`` on the ``surface`` it is shown on (never "P moves
    agents" in a running session, never another provider's remedy)."""
    c = credential
    remedy = guidance(provider or c.provider, surface)
    finding = classify(c, now)
    login = login_command or "the provider's login command"
    age = observation_age(c, now)
    if finding.level == "disabled":
        return "disabled"
    if finding.level == "login":
        return f"credential rejected ({c.reason}) — sign in again: {login}"
    if finding.level == "unusable":
        return f"unusable (access token expired and not refreshed) — if it persists, sign in again: {login}"
    if finding.level == "payment":
        return f"payment required or forbidden — {remedy}"
    if finding.level == "exhausted":
        return f"quota exhausted · observed {age} — {remedy}"
    text = " · ".join(window_text(w, c, now, tz) for w in c.windows) or "no quota headers yet"
    text += f" · observed {age}"
    if finding.level == "high":
        text += f" · 90%+: {remedy}"
    if c.success or c.failed:
        text += f" · {c.success if c.success is not None else 'unknown'} ok / {c.failed if c.failed is not None else 'unknown'} failed"
    return text


# Where doctor reads credential health while quota reads are off, by the
# gateway backend (``endpoint.backend_of``): the supervised service's
# journal, or the on-demand instance logs.
HEALTH_SOURCES = {
    "systemd": "the gateway service's journal",
    "on-demand": "the gateway's instance logs (claude-multi gateway logs)",
}
HEALTH_SOURCE_UNKNOWN = ("the gateway's log (the service journal when supervised, "
                         "the instance logs on demand)")


def health_source(backend: str | None) -> str:
    """Where credential health is read for ``backend`` (None: either)."""

    return HEALTH_SOURCES.get(backend or "", HEALTH_SOURCE_UNKNOWN)


# The typed reason a quota read is unavailable, by pool state: management
# that is off or not reachable with the key (a gateway restart or the key
# fixes it, never another sign-in), an observation that cannot be made,
# or another program on the gateway port.
MANAGEMENT_OFF_STATES = frozenset({"no-key", "disabled", "pending", "key-unusable", "management-off",
                                   "mismatch", "refused"})
UNAVAILABLE_STATES = frozenset({"unavailable", "malformed", "error", "down", "seam"})


def unavailable_reason(pool: PoolStatus) -> str | None:
    """``management-disabled``, ``unavailable`` or ``not-ours`` for a pool
    without a usable read, else None. The exhausted and stale conditions of
    a readable pool are :func:`condition`'s."""

    if pool.state in MANAGEMENT_OFF_STATES:
        return "management-disabled"
    if pool.state in UNAVAILABLE_STATES:
        return "unavailable"
    if pool.state == "not-ours":
        return "not-ours"
    return None


def condition(pool: PoolStatus, now: datetime, *, pools: Iterable[str] | None = None) -> str:
    """One typed condition of a quota observation: ``available``,
    ``management-disabled``, ``unavailable``, ``not-ours``, ``stale`` (a
    readable pool whose credentials carry no current window observation)
    or ``exhausted`` (every usable credential observed exhausted). Missing
    observations alone are never damage: they are unavailable or stale."""

    reason = unavailable_reason(pool)
    if reason is not None:
        return reason
    credentials = [c for c in pool.credentials if pools is None or c.provider in set(pools)]
    if credentials and pool_exhausted(credentials, now):
        return "exhausted"
    current = any(window_current(w, c.observed_at, now) for c in credentials for w in c.windows)
    if credentials and not current:
        return "stale"
    return "available"


# What each typed condition means; the command line, the Providers details
# and /cm quota print the same line.
CONDITION_TEXT = {
    "available": "the accounts' quota was read",
    "stale": "read, but no account carries a current observation yet (traffic updates it)",
    "exhausted": "every usable account was observed exhausted (advisory)",
    "management-disabled": "the read is off or the gateway refused its key (a restart or the key fixes it)",
    "unavailable": "no reading can be made here now",
    "not-ours": "another program holds the gateway port; the management key was not sent",
}
# ``claude-multi quota`` exits 1 exactly when no reading was made.
CONDITION_EXIT = {"available": 0, "stale": 0, "exhausted": 0,
                  "management-disabled": 1, "unavailable": 1, "not-ours": 1}


def condition_line(name: str) -> str:
    """The one line every surface prints for a typed condition."""

    return f"Quota condition: {name} — {CONDITION_TEXT[name]}"


def state_text(pool: PoolStatus, *, restart_hint: str, backend: str | None = None) -> str | None:
    r = restart_hint
    source = health_source(backend)
    return {
        "no-key": f"Quota: off — no management key; credential health comes from {source} "
                  f"(enable: claude-multi-proxy init, then restart the gateway: {r})",
        "disabled": "Quota: disabled by the operator (claude-multi-proxy rotate-management-key re-enables)",
        "unavailable": f"Quota: unavailable in this build; credential health comes from {source}",
        "pending": f"quota: management key awaits start preparation; reads stay off — restart the gateway: {r}",
        "key-unusable": f"quota: {pool.key_file or 'management-key'} is unusable "
                        f"({pool.key_problem or 'unsafe file'}) — "
                        f"{pool.remedy or 'inspect the management-key path without following symlinks'}; "
                        f"then restart the gateway: {r}",
        "management-off": "quota: the running gateway has management off (it started before the key existed, "
                          f"or its binary lacks the read-only allowlist) — restart it: {r}",
        "mismatch": "quota: management key mismatch — the running gateway holds another key; "
                    f"restart it to load management-key: {r}",
        "refused": "quota: management refused (loopback banned for 30 min after five failed key attempts "
                   f"on allowlisted routes) — restarting the gateway clears the ban: {r}",
        "malformed": "quota: the gateway's auth-files answer was not understood (ignored)",
        "error": f"quota: the gateway answered HTTP {pool.http_status}" if pool.http_status is not None
                 else "quota: the management read failed",
        "not-ours": "quota: the process on the gateway port is not claude-multi's gateway; the management key was not sent",
        "down": "quota: the local gateway is down",
    }.get(pool.state)


def doctor_report(pool: PoolStatus, *, provider_by_pool, login_commands, restart_hint: str,
                  now: datetime, tz: tzinfo | None = None, backend: str | None = None,
                  ) -> tuple[list[str], list[str], frozenset[str]]:
    """Attention, Info, and pools with an explicit invalid_grant warning.

    Only the same refresh failure is equivalent to the journal's warning.
    Healthy data (even a valid access token) cannot prove refresh health;
    an unauthorized access token is not evidence of a dead refresh token.
    """
    if pool.state != "ok":
        if pool.state in {"down", "seam"}:
            return [], [], frozenset()
        text = state_text(pool, restart_hint=restart_hint, backend=backend)
        if text is None:
            return [], [], frozenset()
        return ([], [text], frozenset()) if pool.state in INFO_STATES else (
            [text], [], frozenset())
    attention, summary, refresh_failures = [], [], set()
    other = 0
    for c in pool.credentials:
        provider = provider_by_pool.get(c.provider)
        if provider is None:
            other += 1
            continue
        finding = classify(c, now)
        login_command = login_commands.get(c.provider)
        login = login_command or "the provider's login command"
        prefix = f"quota: {c.handle} ({provider})"
        fallback = guidance(provider, "doctor")
        age = observation_age(c, now)
        if finding.level == "login":
            attention.append(f"{prefix} credential rejected by the provider ({c.reason}) — sign in again: {login}")
            if c.reason == "invalid_grant":
                refresh_failures.add(c.provider)
        elif finding.level == "unusable":
            attention.append(f"{prefix} is unusable (access token expired and not refreshed) — "
                             f"if it persists, sign in again: {login}")
        elif finding.level == "payment":
            attention.append(f"{prefix} refused by the provider (payment required or forbidden) — {fallback}")
        elif finding.level == "exhausted":
            attention.append(f"{prefix} quota exhausted (observed {age}) — {fallback}")
        elif finding.level == "high":
            window = finding.window
            reset = f"resets {format_reset(window.resets_at, now, tz)}" if window.resets_at else "reset unknown"
            attention.append(f"{prefix} at {math.floor(window.used_percent)}% of its {window.label} window "
                             f"({reset}; observed {age}; passive) — {fallback}")
        if finding.level == "disabled":
            summary.append(f"{c.handle} disabled")
        elif c.windows:
            windows = " · ".join(window_text(w, c, now, tz) for w in c.windows)
            summary.append(f"{c.handle} {windows} ({age})")
        else:
            summary.append(f"{c.handle} no quota headers yet")
    if other:
        summary.append(f"other credentials: {other}")
    info = ["Quota (local gateway, passive): " + ("; ".join(summary) or "no credentials")]
    return attention, info, frozenset(refresh_failures)


def provider_cell(credentials: Iterable[Credential], now: datetime, tz: tzinfo | None = None,
                  *, width: int | None = None) -> str:
    """Worst credential; compact progressively, never truncate the observation age.

    At narrow widths drop the handle, then the other windows, then the window
    label. The detail pane retains the handle and reset. No percent is invented.
    """
    credentials = tuple(credentials)
    finding = worst(credentials, now)
    if finding is None:
        return "no data"
    c = finding.credential
    age = observation_age(c, now, compact=True)
    current = [w for w in c.windows if window_current(w, c.observed_at, now)]
    def window(w):
        return f"{w.label} {math.floor(w.used_percent)}%{'!' if w.used_percent >= HIGH_PERCENT else ''}"
    level = {"login": "sign-in!", "unusable": "unusable!", "payment": "payment!",
             "exhausted": "exhausted!", "disabled": "disabled"}.get(finding.level)
    windows = " · ".join(window(w) for w in current)
    text = level or windows or ("reset" if c.windows and c.observed_at else "no data")
    prefix = c.handle + " " if len(credentials) > 1 else ""
    candidates = [f"{prefix}{text} · {age}", f"{text} · {age}"]
    if finding.window is not None and level is None:
        candidates += [f"{window(finding.window)} · {age}",
                       f"{math.floor(finding.window.used_percent)}%{'!' if finding.level == 'high' else ''} · {age}"]
    candidates += [age]
    if width is None:
        return candidates[0]
    return next((text for text in candidates if len(text) <= width),
                "age ?" if c.observed_at is None else age)


def provider_details(credentials: Iterable[Credential], now: datetime, tz: tzinfo | None = None,
                     *, login_command: str | None = None) -> tuple[str, ...]:
    """Worst first, one observation plus an explicit route to the remaining rows.

    Age precedes windows so detail clipping cannot turn an old observation into
    an apparently fresh one. The screen prioritizes remedies separately.
    """
    credentials = tuple(credentials)
    finding = worst(credentials, now)
    if finding is None:
        return ("no quota headers yet",)
    c = finding.credential
    text = f"{c.handle} · observed {observation_age(c, now)}"
    if finding.level == "exhausted" and finding.window is None:
        text += " · quota exhausted (no window)"
    else:
        # The worst window first makes a high secondary window visible too.
        windows = sorted(c.windows, key=lambda w: w != finding.window)
        text += " · " + (" · ".join(window_text(w, c, now, tz) for w in windows) or "no quota headers yet")
    more = (f"+{len(credentials) - 1} more (claude-multi quota)",) if len(credentials) > 1 else ()
    extra = metadata_lines(c, now, tz) if c.metadata != QuotaMetadata() or c.model_quotas or c.plan_type else ()
    return (text, *more, *extra)


def card_summary(pool: PoolStatus, *, pools: Iterable[str], login_commands,
                 now: datetime, tz: tzinfo | None = None, resume: bool = False,
                 width: int = 67) -> tuple[str, str] | None:
    """Advisory only; the actual lineup's pools, not the whole gateway.

    At 80 columns preserve age and the available remedy before reset detail.
    Missing windows/reset times are explicit rather than fabricated.
    """
    if pool.state != "ok":
        return None
    pools = frozenset(pools)
    finding = worst((c for c in pool.credentials if c.provider in pools), now)
    if finding is None or finding.level in {"disabled", "no-data", "payment"}:
        return None
    c, w = finding.credential, finding.window
    age = observation_age(c, now, compact=True)
    age = age if c.observed_at is None else f"{age} old"
    if finding.level in {"login", "unusable"}:
        command = login_commands.get(c.provider) or "the provider's login command"
        text = f"{c.handle} sign in: {command} · {age}"
        if len(text) > width:
            text = f"{c.handle} · {age} — {command}"
        return text, "warn"
    if finding.level == "ok":
        return f"worst {c.handle} {w.label} {math.floor(w.used_percent)}% · observed {observation_age(c, now)}", "dim"
    remedy = "S Sessions → T" if resume else "P provider fallback"
    value = (f"{w.label} {math.floor(w.used_percent)}%!" if w is not None else "exhausted (no window)")
    if finding.level == "exhausted" and w is not None:
        value = "exhausted " + value
    reset = (f"resets {format_reset(w.resets_at, now, tz)}" if w and w.resets_at else "reset unknown")
    base = f"{c.handle} {value} · {age}"
    text = f"{base} · {reset} — {remedy}"
    if len(text) > width and w is not None and w.resets_at is None:
        # Unknown reset is a fact, not disposable detail. Shorten the fresh
        # remedy first, then the redundant handle rather than invent a time.
        remedy = "S Sessions → T" if resume else "P fallback"
        text = f"{base} · {reset} — {remedy}"
        if len(text) > width:
            text = f"{value} · {age} · {reset} — {remedy}"
    elif len(text) > width:
        # A known reset remains in Providers/claude-multi quota; never lose
        # the observation age or suggest the unavailable P action on resume.
        text = f"{base} — {remedy}"
    return text, "warn"


def pool_exhausted(credentials: Iterable[Credential], now: datetime) -> bool:
    """All usable credentials must have current exhaustion evidence; advisory."""
    usable = [c for c in credentials if c.disabled is not True and c.status != "disabled"]
    return bool(usable) and all(
        c.disabled is False and c.unavailable is False and c.status == "active" and
        (classify(c, now).level == "exhausted" or any(
            w.used_percent >= 100 and window_current(w, c.observed_at, now) for w in c.windows))
        for c in usable)


# Wherever a reported Retry-After is shown.
RETRY_AFTER_REPORTED = "The provider reported Retry-After {duration}."
RETRY_AFTER_SILENT = "The client may wait silently; this is not proof that an agent has stopped."


def retry_after_duration(c: Credential) -> str | None:
    """The reported Retry-After as a duration from its observation (None:
    no reported value or no observation time)."""

    after = c.metadata.retry_after
    if after is None or c.observed_at is None:
        return None
    seconds = max(0, math.ceil((after - c.observed_at).total_seconds()))
    return f"{seconds}s" if seconds < 60 else format_age(after - c.observed_at, compact=True)


def metadata_lines(c: Credential, now: datetime, tz: tzinfo | None = None) -> tuple[str, ...]:
    lines = []
    if c.provider == "codex":
        lines.append(f"plan {c.plan_type or 'unknown'} ({c.plan_source or 'source unknown'})")
        def flag(value):
            return "unknown" if value is None else str(value).lower()
        lines.append(f"credits: {c.metadata.balance or 'unknown'} provider credits · "
                     f"has-credits {flag(c.metadata.has_credits)} · unlimited {flag(c.metadata.unlimited)}")
    if c.provider in {"codex", "claude"}:
        after = c.metadata.retry_after
        wait = "unknown"
        if after is not None:
            seconds = (after - now).total_seconds()
            wait = "elapsed" if seconds <= 0 else (
                f"in {math.ceil(seconds)}s" if seconds < 60 else "in " + format_age(after - now, compact=True))
        lines.append(f"retry-after: {wait} · observed {observation_age(c, now)}")
        duration = retry_after_duration(c)
        if duration is not None:
            lines += [RETRY_AFTER_REPORTED.format(duration=duration), RETRY_AFTER_SILENT]
    for model in c.model_quotas:
        from dataclasses import replace
        scoped = replace(c, observed_at=model.observed_at, windows=model.windows, metadata=model.metadata)
        windows = " · ".join(window_text(w, scoped, now, tz) for w in model.windows) or "quota unknown"
        lines.append(f"model {model.model}: {windows} · observed {observation_age(scoped, now)}")
        if model.metadata != QuotaMetadata():
            nested = replace(scoped, model_quotas=(), plan_type=model.metadata.plan_type,
                             plan_source="reported header" if model.metadata.plan_type else None)
            lines.extend("  " + line for line in metadata_lines(nested, now, tz))
    return tuple(lines)


def command_lines(pool: PoolStatus, *, provider_by_pool, login_commands, restart_hint: str,
                  now: datetime, tz: tzinfo | None = None, surface: str = "providers",
                  backend: str | None = None) -> tuple[int, list[str]]:
    typed = condition(pool, now, pools=tuple(provider_by_pool))
    if pool.state != "ok":
        return CONDITION_EXIT[typed], [state_text(pool, restart_hint=restart_hint, backend=backend)
                                       or "quota: management read unavailable", condition_line(typed)]
    read = pool.read_at.astimezone(tz).strftime("%H:%M") if pool.read_at else "unknown"
    lines = [f"Quota — local gateway, read {read} (passive: updated only by traffic; nothing stored)",
             condition_line(typed)]
    known = [c for c in pool.credentials if c.provider in provider_by_pool]
    width = max((len(c.handle) for c in known), default=0)
    for name, provider in provider_by_pool.items():
        credentials = [c for c in known if c.provider == name]
        if not credentials:
            continue
        lines.append(f"{provider} ({name} pool)")
        for c in credentials:
            text = credential_text(c, login_command=login_commands.get(name), now=now, tz=tz,
                                   provider=provider, surface=surface)
            lines.append(f"  {c.handle:<{width}}  {c.status:<8}  {text}")
            lines.extend("    " + line for line in metadata_lines(c, now, tz))
        lines.append("  Pool headroom: unknown" if not pool_exhausted(credentials, now) else
                     "  All usable credentials observed exhausted (advisory)")
    other = len(pool.credentials) - len(known)
    if other:
        lines.append(f"other credentials (not a claude-multi provider): {other}")
    lines.append("Management safety: five failed logins on allowlisted routes can ban loopback for 30 min; "
                 "refused routes and guard-refused browser/Host requests do not count.")
    lines.append("Quota is passive metadata from gateway traffic; no provider request was made.")
    lines.append("Management authentication failures on allowed paths can trigger the gateway's five-strike lockout; this command does not retry them.")
    return CONDITION_EXIT[typed], lines


def guidance_action(surface: str) -> str:
    """The provider-agnostic action of :func:`guidance` (no provider named)."""
    if surface == "fresh":
        return "P chooses another profile for this new launch."
    if surface in {"resume", "session"}:
        return "change the lineup with /cm or Sessions -> T."
    return ("choose a profile that does not depend on this provider. "
            "For an existing session, use /cm or Sessions -> T.")


def guidance(provider: str, surface: str) -> str:
    """Provider-specific action guidance; P never changes an existing session."""
    return f"{provider}: quota pressure; {guidance_action(surface)}"


def report(pool: PoolStatus, now: datetime):
    """The same minimized snapshot as human quota, never the management body."""
    from .observations import Fact, Report, instant
    facts = [Fact("management-state", "source", "management", "known", pool.state,
                  source="management", classification="info" if pool.state in {"ok", "seam", *INFO_STATES} else "attention")]
    def add(code, subject, value, observed=None, freshness="unknown"):
        facts.append(Fact(code, "credential", subject, "known" if value is not None else "unknown", value,
                          observed_at=observed, source="management", freshness=freshness,
                          coverage="partial", reason="passive-metadata"))
    for c in pool.credentials:
        finding = classify(c, now)
        facts.append(Fact("credential-health", "credential", c.handle, "known", finding.level,
                          source="management", observed_at=c.observed_at,
                          classification="attention" if finding.level in {"login", "unusable", "payment", "exhausted", "high"} else "info"))
        for code in ("provider", "status", "reason", "disabled", "unavailable", "success", "failed", "plan_type", "plan_source"):
            add(code.replace("_", "-"), c.handle, getattr(c, code))
        add("scheduler-next-retry", c.handle, instant(c.next_retry_after))
        def scoped(subject, observed, windows, metadata):
            freshness = "unknown" if observed is None else "current" if 0 <= (now - observed).total_seconds() < 5 * 3600 else "stale"
            for name in ("has_credits", "unlimited", "balance", "plan_type", "retry_after"):
                value = getattr(metadata, name)
                if name == "retry_after":
                    if value is not None:
                        # A reported wait is never proof an agent stopped.
                        facts.append(Fact("retry-after-honesty", "credential", subject, "known",
                                          RETRY_AFTER_SILENT, observed_at=observed, source="management",
                                          freshness=freshness, coverage="partial",
                                          reason="client-may-wait-silently"))
                    value = instant(value)
                # The untimed reported claim and the timestamped quota header
                # are distinct facts, even when the claim is the chosen label.
                code = "quota-plan-type" if name == "plan_type" else name.replace("_", "-")
                add(code, subject, value, observed, freshness)
            for index, window in enumerate(windows):
                for name, value in (("label", window.label), ("used-percent", window.used_percent),
                                    ("resets-at", instant(window.resets_at))):
                    add(f"window-{index + 1}-{name}", subject, value, observed,
                        "current" if window_current(window, observed, now) else "stale" if observed else "unknown")
        scoped(c.handle, c.observed_at, c.windows, c.metadata)
        for index, model in enumerate(c.model_quotas):
            subject = c.handle.replace("#", "-") + f"-model-{index + 1}"
            add("model", subject, model.model, model.observed_at)
            add("credential", subject, c.handle)
            scoped(subject, model.observed_at, model.windows, model.metadata)
    return Report("quota", now, {"management": pool.state}, tuple(facts))
