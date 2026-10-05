"""``claude-multi migrate`` and ``claude-multi restore-2x``.

Records only: the migration run and ``profile migrate`` live elsewhere;
this module owns the v3 -> v4 record conversion, the
``sessions/<id>.v3.json`` backups, the per-cwd pointer merge, the state
marker write and the reverse (``restore_2x``). Every writer here holds the global migration lock
exclusively; ``plan`` (the dry run) takes no lock and writes nothing.

Import rule: ``catalog``, ``profile``, ``settings``, ``sessions``,
``state``, ``strict_json``, ``migration_map`` only; never ``cli``, ``launch``,
``transition``, ``compiler``, ``scope`` or ``hooks``. The caller passes what
those would give (the resolved hook command and the legacy shim path, the
transcript check, the liveness check).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import os
import shlex
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import catalog, errors, migration_map, profile, sessions, settings, state, strict_json

# The agent efforts; an agent lane outside them falls back to "high".
AGENT_EFFORTS = profile.EFFORT_ORDER
LEAD_EFFORTS = (*profile.EFFORT_ORDER, profile.ULTRACODE)
MECHANICAL_ROLES = ("cm-analyst", "cm-implementer", "cm-reviewer")
OUTCOMES = ("needs-choice", "successor", "renamed", "unchanged")
DEFAULT_CLEANUP_PERIOD_DAYS = 30  # Claude's default when cleanupPeriodDays is unset
PROFILE_MIGRATE_COMMAND = "claude-multi profile migrate --apply"
ORDINARY_NATIVE = {"explore": "native", "general_purpose": "on", "plan": "native"}


class MigrationError(errors.ClaudeMultiError, ValueError):
    """``migrate``/``restore-2x`` refused or failed (exit 2)."""


class RestoreRefused(MigrationError):
    """restore-2x refused before writing anything (§5.1 step 3)."""

    def __init__(self, lines: Sequence[str]):
        self.lines = tuple(lines)
        super().__init__("\n".join(self.lines))


class RestoreStopped(MigrationError):
    """restore-2x stopped part-way (a failed re-check or rescan); marker kept."""

    def __init__(self, message: str, done: "RestoreReport"):
        self.done = done
        super().__init__(message)


@dataclass(frozen=True)
class RecordOutcome:
    managed_id: str
    status: str  # "migrate" | "already-v4" | "unreadable" | "failed" | "blocked"
    from_version: int | None
    session_type: str | None  # v3 type (legacy files: managed)
    outcome: str | None  # needs-choice | successor | renamed | unchanged
    v3_lead: str | None
    v4_lead: str | None
    successor: str | None
    composition_name: str | None
    profile: str | None
    profile_reason: str  # seed | convert | dropped | missing | default | ordinary | ""
    agents: tuple[str, ...]
    notes: tuple[str, ...]
    record: dict | None
    error: str | None
    last_event_source: str | None = None
    last_seen_at: str | None = None


@dataclass(frozen=True)
class PointerOutcome:
    digest: str
    cwd: str | None
    action: str  # "merge" | "rename-ordinary" | "keep" | "already-merged" | "unreadable"
    kept: str | None
    dropped: str | None


@dataclass(frozen=True)
class TranscriptNote:
    """A record whose transcript cannot resume it."""

    managed_id: str
    status: str  # "missing" | "stale"
    age_days: int | None


@dataclass(frozen=True)
class MigrationReport:
    state_root: str
    marker_before: int
    dry_run: bool
    records: tuple[RecordOutcome, ...]
    pointers: tuple[PointerOutcome, ...]
    backups_written: int
    pointer_backups_written: int
    transcripts: tuple[TranscriptNote, ...] | None = None  # None = not checked
    cleanup_days: int = DEFAULT_CLEANUP_PERIOD_DAYS
    pointer_files: int = 0

    @property
    def ok(self) -> bool:
        """Exit 0: every record migrated or already v4 and no pointer unreadable."""

        return all(item.status in ("migrate", "already-v4") for item in self.records) and not any(
            item.action == "unreadable" for item in self.pointers
        )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------- key rules


def _selector_base(selector: str) -> str:
    suffix = "[1m]"
    return selector[: -len(suffix)] if selector.endswith(suffix) else selector


def resolve_retired_selector(
    retired: Mapping[str, Any], selector: str
) -> tuple[str, dict[str, Any]] | None:
    """A retired selector (one ``[1m]`` stripped) -> ``(retired key, entry)``.

    The pure form of ``Catalog.resolve_selector`` over a retired map, so a
    ``profile.LineupCatalog`` (which has no such method) works too (§4.1
    deviation: no ``catalog_obj`` keyword is needed).
    """

    wanted = _selector_base(selector)
    for key in sorted(retired):
        entry = retired[key]
        for candidate in sorted(entry.get("selectors", {})):
            if _selector_base(candidate) == wanted:
                return key, entry
    return None


def translate_key(key: str, selector: str | None, catalog_version: int, cat: Any) -> str:
    """The current key naming the generation a legacy key meant (public).

    ``cat`` is anything with a ``retired`` map (``profile.LineupCatalog`` or
    ``catalog.Catalog``). A selector that names a retired line wins; else a
    same-key generation move ``K@g`` retired first after ``catalog_version``;
    else the key itself (live, a retired rename, or unknown). Two ``K@…``
    entries sharing that first ``since_catalog`` raise :class:`MigrationError`.
    Callable with ``selector=None`` (composition keys).
    """

    retired = cat.retired
    if selector:
        hit = resolve_retired_selector(retired, selector)
        if hit is not None:
            return hit[0]
    moved = [
        (entry["since_catalog"], retired_key)
        for retired_key, entry in retired.items()
        if "@" in retired_key
        and retired_key.split("@", 1)[0] == key
        and entry["since_catalog"] > catalog_version
    ]
    if moved:
        first = min(since for since, _key in moved)
        at_first = sorted(retired_key for since, retired_key in moved if since == first)
        if len(at_first) == 1:
            return at_first[0]
        raise MigrationError(
            f"lead {key}: retired keys {', '.join(at_first)} share since_catalog "
            f"{first}; the catalog is ambiguous for a catalog-{catalog_version} record"
        )
    return key


def _resolve(cat: Any, key: str) -> catalog.KeyResolution | None:
    """``cat.resolve_key(key)``; a key unknown to the catalog -> None."""

    try:
        return cat.resolve_key(key)
    except catalog.CatalogError:
        return None


def _generation(cat: Any, key: str) -> str | None:
    if key in cat.lines:
        value = cat.lines[key].get("generation")
    elif key in cat.retired:
        value = cat.retired[key].get("generation")
    else:
        value = None
    return value if isinstance(value, str) and value else None


def _provider(cat: Any, key: str, resolution: catalog.KeyResolution | None) -> str | None:
    if key in cat.lines:
        return cat.lines[key]["provider"]
    if key in cat.retired:
        return cat.retired[key].get("provider")
    if resolution is not None and resolution.key is not None:
        return cat.lines[resolution.key]["provider"]
    return None


def _family(cat: Any, key: str, resolution: catalog.KeyResolution | None) -> str | None:
    """Live line family, the retired entry's provider family, or None when removed."""

    if resolution is None or resolution.key is None:
        return None
    if key in cat.lines:
        return catalog.line_family(cat.lines[key], cat.providers)
    provider_id = cat.retired.get(key, {}).get("provider")
    provider = cat.providers.get(provider_id)
    if provider is None:
        return provider_id
    return provider["independence_family"]


def _outcome(cat: Any, v4_lead: str, resolution: catalog.KeyResolution | None) -> str:
    """§4.6 (the M records table)."""

    if resolution is None or resolution.key is None:
        return "needs-choice"
    if resolution.key == v4_lead:
        return "unchanged"
    first = cat.retired.get(resolution.chain[0]) if resolution.chain else None
    if first is not None and first.get("last_wire") == cat.lines[resolution.key]["wire_model"]:
        return "renamed"
    return "successor"


def default_settings_snapshot(cat: Any) -> dict[str, Any]:
    """The default Settings snapshot a migrated record carries."""

    eff = settings.effective(
        {"version": settings.SETTINGS_DATA_VERSION},
        provider_ids=cat.providers,
        line_keys=cat.lines,
    )
    return settings.snapshot(eff)


# ---------------------------------------------------------------- convert


def _agent_binding(cat: Any, variant: Mapping[str, Any], key: str) -> tuple[dict[str, Any], str | None]:
    lane = variant["lane"]
    effort = lane if lane in AGENT_EFFORTS else "high"
    binding = {
        "key": key,
        "generation": _generation(cat, key),
        "selector": variant["client_selector"],
        "effort": effort,
    }
    return binding, (None if lane in AGENT_EFFORTS else lane)


def _mechanical_agents(
    cat: Any,
    snapshot: Mapping[str, Any],
    catalog_version: int,
    lead_family: str | None,
    native: dict[str, str],
    notes: list[str],
) -> dict[str, dict[str, Any]]:
    """§4.5: the M rule over the record's own variants (no other id bound)."""

    variants = list(snapshot.get("variants", []))
    facts = []
    for variant in variants:
        key = translate_key(
            variant["model"], variant.get("client_selector"), catalog_version, cat
        )
        resolution = _resolve(cat, key)
        facts.append((variant, key, resolution, _family(cat, key, resolution)))
    agents: dict[str, dict[str, Any]] = {}
    picked: dict[str, int] = {}
    for role in MECHANICAL_ROLES:
        of_role = [index for index, (variant, *_rest) in enumerate(facts) if variant["role"] == role]
        ordered = [i for i in of_role if facts[i][0].get("preferred")] + [
            i for i in of_role if not facts[i][0].get("preferred")
        ]
        if not ordered:
            continue
        preferred = facts[ordered[0]]
        choice = next((i for i in ordered if facts[i][2] is not None and facts[i][2].key is not None), None)
        if choice is None:
            notes.append(
                f"{profile.label(role)}: {preferred[0]['model']!r} was removed — unbound"
            )
            continue
        variant, key, _resolution, _fam = facts[choice]
        binding, bad_lane = _agent_binding(cat, variant, key)
        agents[role] = binding
        picked[role] = choice
        if bad_lane is not None:
            notes.append(f"{role}: lane {bad_lane!r} → effort high")
        if choice != ordered[0]:
            notes.append(
                f"{profile.label(role)}: preferred {preferred[0]['model']} removed → "
                f"{key}·{binding['effort']}"
            )
    # cm-reviewer-strong: the first other-family live reviewer variant.
    reviewer_family = (
        facts[picked["cm-reviewer"]][3] if "cm-reviewer" in picked else lead_family
    )
    for index, (variant, key, resolution, family) in enumerate(facts):
        if variant["role"] != "cm-reviewer" or index == picked.get("cm-reviewer"):
            continue
        if resolution is None or resolution.key is None or family == reviewer_family:
            continue
        binding, bad_lane = _agent_binding(cat, variant, key)
        agents["cm-reviewer-strong"] = binding
        if bad_lane is not None:
            notes.append(f"cm-reviewer-strong: lane {bad_lane!r} → effort high")
        break
    if native.get("explore") == "replace":
        if "cm-analyst" in agents:
            agents["cm-explorer"] = copy.deepcopy(agents["cm-analyst"])
        else:
            native["explore"] = "native"
            notes.append("Explore: replace → native (no analyst left to back cm-explorer)")
    return {role: agents[role] for role in catalog.AGENT_ROLE_IDS if role in agents}


def _lead_providers(
    cat: Any,
    snapshot: Mapping[str, Any],
    lead_provider: str | None,
    settings_snapshot: Mapping[str, Any],
    notes: list[str],
) -> list[str] | None:
    """Providers whose availability includes lead; None = every enabled."""

    availability = snapshot.get("availability", {}).get("providers", {})
    chosen = []
    for provider_id in sorted(availability):
        if availability[provider_id] not in ("lead", "lead+agents"):
            continue
        if provider_id not in cat.providers:
            notes.append(
                f"lead_providers: {provider_id} is not a provider in catalog "
                f"{cat.catalog_version} — dropped"
            )
            continue
        chosen.append(provider_id)
    if lead_provider is not None and lead_provider in cat.providers and lead_provider not in chosen:
        chosen.append(lead_provider)
        chosen.sort()
        notes.append(f"lead_providers: added {lead_provider} (the lead's provider, E18)")
    enabled = sorted(
        provider_id
        for provider_id, on in settings_snapshot["providers_enabled"].items()
        if on
    )
    if not chosen or chosen == enabled:
        return None
    return chosen


def convert_record(
    raw: dict[str, Any],
    *,
    cat: Any,
    now: str,
    settings_snapshot: dict[str, Any] | None = None,
) -> RecordOutcome:
    """The v4 document of one v1-3 record (pure).

    ``cat`` is the merged ``profile.LineupCatalog`` (custom entries
    included). The record keeps the translated legacy keys and the v3
    selectors: resolution through the retired map happens at the next launch.
    Raises :class:`MigrationError` (ambiguous catalog) or KeyError/TypeError
    on a malformed record; ``run`` turns those into ``status="failed"``.
    """

    from_version = raw["version"]
    if from_version not in sessions.LEGACY_RECORD_VERSIONS:
        raise MigrationError(f"record version {from_version!r} is not a legacy record")
    view = sessions._normalize_legacy_record(copy.deepcopy(raw))
    session_type = view["session_type"]
    snapshot_ = settings_snapshot if settings_snapshot is not None else default_settings_snapshot(cat)
    catalog_version = view["catalog_version"]
    notes: list[str] = []
    ordinary = session_type == sessions.SESSION_TYPE_ORDINARY
    if ordinary:
        v3_lead = view["ordinary_model"]
        selector = None
        composition_name = None
    else:
        lead_doc = view["snapshot"]["lead"]
        v3_lead = lead_doc["model"]
        selector = lead_doc["client_selector"]
        composition_name = view["composition_name"]
    v4_lead = translate_key(v3_lead, selector, catalog_version, cat)
    resolution = _resolve(cat, v4_lead)
    if resolution is None:
        notes.append(
            f"lead {v4_lead}: unknown to catalog {cat.catalog_version} (not a custom model either)"
        )
    outcome = _outcome(cat, v4_lead, resolution)
    successor = (
        resolution.key
        if resolution is not None and resolution.key is not None and resolution.key != v4_lead
        else None
    )
    lead_provider = _provider(cat, v4_lead, resolution)
    if ordinary:
        lead = {
            "key": v4_lead,
            "generation": _generation(cat, v4_lead),
            "selector": None,
            "effort": profile.ULTRACODE,
            "env": {},
            "compaction": {"window": None, "trigger": None, "percent": None, "scalar": None},
        }
        agents: dict[str, dict[str, Any]] = {}
        native = dict(ORDINARY_NATIVE)
        workflows = "native"
        lead_providers = None
        no_subagents = bool(view.get("no_subagents", False))
    else:
        snap = view["snapshot"]
        effort = lead_doc["effort"]
        if effort not in LEAD_EFFORTS:
            notes.append(f"lead: effort {effort!r} → ultracode")
            effort = profile.ULTRACODE
        compaction: dict[str, Any] = {
            "window": snap.get("auto_compact_window_tokens") or None,
            "trigger": lead_doc.get("auto_compact_tokens") or None,
            "percent": None,
            "scalar": snap.get("scalar_context_tokens"),
        }
        if lead_doc.get("client_context_tokens"):
            compaction["client_tokens"] = lead_doc["client_context_tokens"]
        if lead_doc.get("provider_context_tokens"):
            compaction["provider_tokens"] = lead_doc["provider_context_tokens"]
        lead = {
            "key": v4_lead,
            "generation": _generation(cat, v4_lead),
            "selector": selector,
            "effort": effort,
            "env": dict(lead_doc.get("env", {})),
            "compaction": compaction,
        }
        native = dict(snap["native_agents"])
        agents = _mechanical_agents(
            cat, snap, catalog_version, _family(cat, v4_lead, resolution), native, notes
        )
        workflows = snap.get("workflows") or view.get("workflows") or "native"
        lead_providers = _lead_providers(cat, snap, lead_provider, snapshot_, notes)
        no_subagents = False
    lead_class = None
    if resolution is not None and resolution.key is not None:
        lead_class = cat.lines[resolution.key].get("context", {}).get("ordinary_profile")
    applied = {
        "lead": lead,
        "agents": agents,
        "native_agents": native,
        "workflows": workflows,
        "lead_providers": lead_providers,
        "settings_overrides": {},
        "no_subagents": no_subagents,
        "settings": copy.deepcopy(snapshot_),
    }
    target, reason = migration_map.record_profile(composition_name)
    record: dict[str, Any] = {
        "version": sessions.RECORD_VERSION,
        "managed_id": view["managed_id"],
        "runtime_session_id": view["runtime_session_id"],
        "runtime_aliases": copy.deepcopy(view["runtime_aliases"]),
    }
    if "mutation_token" in view:
        record["mutation_token"] = view["mutation_token"]
    record["launch_epoch"] = view.get("launch_epoch", 0)
    for key in (
        "identity_state",
        "last_event_source",
        "last_seen_at",
        "last_end_reason",
        "observed_cwd",
        "observed_model",
        "pending_forks",
        "cwd",
    ):
        if key in view:
            record[key] = copy.deepcopy(view[key])
    record.update(
        {
            "profile": target,
            "follow": False,
            "applied": applied,
            "applied_hash": strict_json.bundle_digest(applied),
            "lineup_generation": 0,
            "lead_class": lead_class,
            "migration": {
                "from_version": from_version,
                "migrated_at": now,
                "outcome": outcome,
                "composition_name": composition_name,
                "session_type": session_type,
            },
            "catalog_version": view["catalog_version"],
            "catalog_hash": view["catalog_hash"],
            "launcher_version": view["launcher_version"],
            "created_at": view["created_at"],
            "forked_from": view.get("forked_from"),
            "migrated_from_version": from_version,
        }
    )
    return RecordOutcome(
        managed_id=view["managed_id"],
        status="migrate",
        from_version=from_version,
        session_type=session_type,
        outcome=outcome,
        v3_lead=v3_lead,
        v4_lead=v4_lead,
        successor=successor,
        composition_name=composition_name,
        profile=target,
        profile_reason=reason,
        agents=tuple(agents),
        notes=tuple(notes),
        record=record,
        error=None,
        last_event_source=view.get("last_event_source"),
        last_seen_at=view.get("last_seen_at"),
    )


def _blocked(outcome: RecordOutcome, profile_exists: Callable[[str], bool] | None) -> RecordOutcome:
    """A convert target whose profile file is not on disk yet."""

    if (
        profile_exists is None
        or outcome.status != "migrate"
        or outcome.profile_reason != migration_map.CONVERT
        or outcome.profile is None
        or profile_exists(outcome.profile)
    ):
        return outcome
    return dataclasses.replace(
        outcome,
        status="blocked",
        error=(
            f"profile {outcome.profile!r} (convert target of legacy composition "
            f"{outcome.composition_name!r}) does not exist yet; run "
            f"{PROFILE_MIGRATE_COMMAND} first"
        ),
    )


def _blocked_refusal(blocked: Sequence[RecordOutcome]) -> MigrationError:
    listed = ", ".join(f"{item.managed_id[:8]} → {item.profile}" for item in blocked)
    return MigrationError(
        f"migrate refused: {len(blocked)} record(s) map to profiles that do not "
        f"exist yet ({listed}); run {PROFILE_MIGRATE_COMMAND} first, then rerun "
        "claude-multi migrate. Nothing was changed."
    )


def _unreadable(managed_id: str, exc: BaseException) -> RecordOutcome:
    return RecordOutcome(
        managed_id=managed_id, status="unreadable", from_version=None, session_type=None,
        outcome=None, v3_lead=None, v4_lead=None, successor=None, composition_name=None,
        profile=None, profile_reason="", agents=(), notes=(), record=None, error=str(exc),
    )


def _failed(managed_id: str, raw: Mapping[str, Any], exc: BaseException) -> RecordOutcome:
    detail = str(exc) or type(exc).__name__
    if isinstance(exc, (KeyError, TypeError)):
        detail = f"malformed legacy record ({type(exc).__name__}: {exc})"
    return RecordOutcome(
        managed_id=managed_id, status="failed", from_version=raw.get("version"),
        session_type=raw.get("session_type"), outcome=None, v3_lead=None, v4_lead=None,
        successor=None, composition_name=raw.get("composition_name"), profile=None,
        profile_reason="", agents=(), notes=(), record=None, error=detail,
        last_event_source=raw.get("last_event_source"), last_seen_at=raw.get("last_seen_at"),
    )


def _already_v4(raw: Mapping[str, Any]) -> RecordOutcome:
    return RecordOutcome(
        managed_id=raw["managed_id"], status="already-v4", from_version=4,
        session_type=None, outcome=None, v3_lead=None, v4_lead=None, successor=None,
        composition_name=None, profile=raw.get("profile"), profile_reason="",
        agents=tuple(raw["applied"]["agents"]), notes=(), record=None, error=None,
        last_event_source=raw.get("last_event_source"), last_seen_at=raw.get("last_seen_at"),
    )


def _convert_or_fail(
    managed_id: str, raw: dict[str, Any], cat: Any, now: str, snapshot_: dict[str, Any]
) -> RecordOutcome:
    try:
        return convert_record(raw, cat=cat, now=now, settings_snapshot=snapshot_)
    except (MigrationError, KeyError, TypeError, ValueError, AttributeError) as exc:
        return _failed(managed_id, raw, exc)


# ---------------------------------------------------------------- pointers


def _pointer_digests(pointers_dir: Path) -> list[str]:
    digests = set()
    for path in pointers_dir.glob("*.json"):
        digest = path.name.split(".", 1)[0]
        if len(digest) == 32 and all(ch in "0123456789abcdef" for ch in digest):
            digests.add(digest)
    return sorted(digests)


def _read_pointer(path: Path) -> dict[str, Any] | None:
    """The payload of a pointer file; None when missing; raises ValueError when unreadable."""

    if not os.path.lexists(path):
        return None
    try:
        payload = strict_json.loads(state.read_private(path))
    except (state.StateError, strict_json.StrictJSONError, OSError) as exc:
        raise ValueError(str(exc)) from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("session_id"), str)
        or not sessions.UUID4.fullmatch(payload["session_id"])
    ):
        raise ValueError("not a pointer payload")
    return payload


def _last_seen(store: sessions.SessionStore, session_id: str) -> str | None:
    try:
        return store.load(session_id).get("last_seen_at")
    except (sessions.SessionError, state.StateError, OSError):
        return None


@dataclass(frozen=True)
class _PointerPlan:
    outcome: PointerOutcome
    write: dict[str, Any] | None  # the <d>.json payload to write
    remove_ordinary: bool
    backups: tuple[Path, ...]  # existing files that need a <name>.v3 backup


def _plan_pointer(store: sessions.SessionStore, digest: str) -> _PointerPlan:
    primary = store.pointers_dir / f"{digest}.json"
    legacy = store.pointers_dir / f"{digest}.ordinary.json"
    try:
        primary_payload = _read_pointer(primary)
        legacy_payload = _read_pointer(legacy)
    except ValueError:
        return _PointerPlan(PointerOutcome(digest, None, "unreadable", None, None), None, False, ())
    backups = tuple(
        path
        for path in (primary, legacy)
        if os.path.lexists(path)
        and not os.path.lexists(path.with_name(path.name + sessions.POINTER_BACKUP_SUFFIX))
    )
    if primary_payload is not None and legacy_payload is not None:
        first, second = primary_payload["session_id"], legacy_payload["session_id"]
        winner_payload = primary_payload
        loser = second
        if first != second:
            seen_first, seen_second = _last_seen(store, first), _last_seen(store, second)
            if seen_second is not None and (seen_first is None or seen_second > seen_first):
                winner_payload, loser = legacy_payload, first
        payload = {"cwd": winner_payload.get("cwd"), "session_id": winner_payload["session_id"]}
        return _PointerPlan(
            PointerOutcome(digest, payload["cwd"], "merge", payload["session_id"], loser),
            payload, True, backups,
        )
    if legacy_payload is not None:
        payload = {"cwd": legacy_payload.get("cwd"), "session_id": legacy_payload["session_id"]}
        return _PointerPlan(
            PointerOutcome(digest, payload["cwd"], "rename-ordinary", payload["session_id"], None),
            payload, True, backups,
        )
    if primary_payload is not None and "session_type" in primary_payload:
        payload = {"cwd": primary_payload.get("cwd"), "session_id": primary_payload["session_id"]}
        return _PointerPlan(
            PointerOutcome(digest, payload["cwd"], "keep", payload["session_id"], None),
            payload, False, backups,
        )
    cwd = primary_payload.get("cwd") if primary_payload else None
    kept = primary_payload["session_id"] if primary_payload else None
    return _PointerPlan(PointerOutcome(digest, cwd, "already-merged", kept, None), None, False, ())


def _apply_pointer(store: sessions.SessionStore, digest: str) -> tuple[PointerOutcome, int]:
    """§4.9 under FileLock(<d>.json) then FileLock(<d>.ordinary.json)."""

    primary = store.pointers_dir / f"{digest}.json"
    legacy = store.pointers_dir / f"{digest}.ordinary.json"
    locks = [state.FileLock(path) for path in sorted((primary, legacy), key=str)]
    for lock in locks:
        lock.acquire(blocking=True)
    try:
        planned = _plan_pointer(store, digest)
        written = 0
        for path in planned.backups:
            backup = path.with_name(path.name + sessions.POINTER_BACKUP_SUFFIX)
            if not os.path.lexists(backup):
                state.atomic_write(backup, state.read_private(path))
                written += 1
        if planned.write is not None:
            state.atomic_write(primary, strict_json.canonical_file_bytes(planned.write))
        if planned.remove_ordinary:
            state.remove_private(legacy)
        return planned.outcome, written
    finally:
        for lock in reversed(locks):
            lock.release()


# -------------------------------------------------------------- transcripts


def cleanup_period_days(user_settings: Any) -> int:
    """``cleanupPeriodDays`` of the user's Claude settings (default 30)."""

    if isinstance(user_settings, Mapping):
        value = user_settings.get("cleanupPeriodDays")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return DEFAULT_CLEANUP_PERIOD_DAYS


def _parse_time(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _transcript_notes(
    views: Iterable[dict[str, Any]],
    check: Callable[[dict[str, Any]], tuple[str, float | None]],
    cleanup_days: int,
    now: str,
) -> tuple[TranscriptNote, ...]:
    """Missing transcripts and ones older than ``cleanupPeriodDays`` (by path)."""

    notes = []
    reference = _parse_time(now)
    for view in views:
        status, mtime = check(view)
        if status == "missing":
            notes.append(TranscriptNote(view["managed_id"], "missing", None))
        elif status == "present" and mtime is not None:
            age = int((reference - mtime) // 86400)
            if age > cleanup_days:
                notes.append(TranscriptNote(view["managed_id"], "stale", age))
    return tuple(sorted(notes, key=lambda item: item.managed_id))


# --------------------------------------------------------------- plan / run


def _scan(
    store: sessions.SessionStore,
    cat: Any,
    now: str,
    profile_exists: Callable[[str], bool] | None,
) -> tuple[list[RecordOutcome], list[dict[str, Any]]]:
    snapshot_ = default_settings_snapshot(cat)
    outcomes: list[RecordOutcome] = []
    views: list[dict[str, Any]] = []
    for path in store.scan_uuid_records():
        managed = path.stem
        try:
            raw, _raw_bytes = store.load_raw(managed)
        except sessions.SessionError as exc:
            outcomes.append(_unreadable(managed, exc))
            continue
        views.append(sessions._lifecycle_view(raw))
        if raw["version"] == sessions.RECORD_VERSION:
            outcomes.append(_already_v4(raw))
            continue
        outcomes.append(_blocked(_convert_or_fail(managed, raw, cat, now, snapshot_), profile_exists))
    return outcomes, views


def _would_back_up(store: sessions.SessionStore, managed: str) -> bool:
    backup = store.backup_path(managed)
    if not os.path.lexists(backup):
        return True
    try:
        return state.read_private(backup) != store.read_record_bytes(managed)
    except (state.StateError, OSError):
        return True


def plan(
    store: sessions.SessionStore,
    cat: Any,
    *,
    now: str | None = None,
    profile_exists: Callable[[str], bool] | None = None,
    transcript_check: Callable[[dict[str, Any]], tuple[str, float | None]] | None = None,
    cleanup_days: int = DEFAULT_CLEANUP_PERIOD_DAYS,
) -> MigrationReport:
    """``migrate --dry-run``: steps 1 and 5.1-5.3 plus the §4.9 decision.

    No lock is taken (``lifecycle_lock`` would create ``locks/``) and nothing
    is written. ``profile_exists`` enables the ``blocked`` status;
    ``transcript_check`` (by path: ``("present"|"missing"|"elsewhere", mtime)``)
    enables the transcript section.
    """

    now = now or _now()
    marker = sessions.check_state_marker(store.root)
    outcomes, views = _scan(store, cat, now, profile_exists)
    pointers = []
    pointer_backups = 0
    for digest in _pointer_digests(store.pointers_dir):
        planned = _plan_pointer(store, digest)
        pointers.append(planned.outcome)
        pointer_backups += len(planned.backups)
    backups = sum(
        1
        for item in outcomes
        if item.status in ("migrate", "blocked") and _would_back_up(store, item.managed_id)
    )
    transcripts = None
    if transcript_check is not None:
        transcripts = _transcript_notes(views, transcript_check, cleanup_days, now)
    return MigrationReport(
        state_root=str(store.root),
        marker_before=marker,
        dry_run=True,
        records=tuple(outcomes),
        pointers=tuple(pointers),
        backups_written=backups,
        pointer_backups_written=pointer_backups,
        transcripts=transcripts,
        cleanup_days=cleanup_days,
        pointer_files=len(list(store.pointers_dir.glob("*.json"))),
    )


def _check_shim(hook_command: str, hook_shim_path: Path | str) -> None:
    """Running legacy hooks must reach this launcher first."""

    try:
        text = state.read_private(Path(hook_shim_path)).decode("utf-8", "replace")
    except (state.StateError, OSError):
        text = ""
    if shlex.quote(hook_command) not in text:
        raise MigrationError(
            "the legacy hook shim does not point at this launcher; rerun from this launcher"
        )


def _migrate_locked(
    store: sessions.SessionStore,
    managed: str,
    cat: Any,
    now: str,
    snapshot_: dict[str, Any],
    profile_exists: Callable[[str], bool] | None,
) -> tuple[RecordOutcome, int]:
    """§4.2 steps 5.1-5.5 for one record under its lifecycle lock."""

    lock = store.lifecycle_lock(managed)
    lock.acquire(blocking=True)
    try:
        try:
            raw, raw_bytes = store.load_raw(managed)
        except sessions.SessionError as exc:
            return _unreadable(managed, exc), 0
        if raw["version"] == sessions.RECORD_VERSION:
            return _already_v4(raw), 0
        outcome = _blocked(_convert_or_fail(managed, raw, cat, now, snapshot_), profile_exists)
        if outcome.status != "migrate":
            return outcome, 0
        written = 0
        backup = store.backup_path(managed)
        if not os.path.lexists(backup) or state.read_private(backup) != raw_bytes:
            # A crash after the backup plus a later legacy hook write: the
            # on-disk v3 is the truth, so the backup follows it.
            state.atomic_write(backup, raw_bytes)
            written = 1
        document = copy.deepcopy(outcome.record)
        document["mutation_token"] = sessions.new_mutation_token()
        store.save(document)
        return dataclasses.replace(outcome, record=document), written
    finally:
        lock.release()


def run(
    store: sessions.SessionStore,
    cat: Any,
    *,
    hook_command: str,
    hook_shim_path: Path | str,
    now: str | None = None,
    profile_exists: Callable[[str], bool] | None = None,
    home: Path | None = None,
    barrier_timeout: float | None = state.SERVED_BARRIER_TIMEOUT,
) -> MigrationReport:
    """``claude-multi migrate`` (§4.2); never calls ``ensure_state_v4`` (sc[16]).

    With ``home`` (the CLI passes it) the exclusive
    migration lock is followed by the gateway served-change barrier, so a
    migration never interleaves with a served change or a prune.
    """

    now = now or _now()
    marker = sessions.check_state_marker(store.root)
    _check_shim(hook_command, hook_shim_path)
    if profile_exists is not None:
        # Refuse before anything is written.
        preview, _views = _scan(store, cat, now, profile_exists)
        blocked = [item for item in preview if item.status == "blocked"]
        if blocked:
            raise _blocked_refusal(blocked)
    snapshot_ = default_settings_snapshot(cat)
    lock = sessions.migration_lock(store.root)
    lock.acquire(blocking=True)
    barrier: state.BarrierToken | None = None
    try:
        if home is not None:
            barrier = state.acquire_served_barrier(
                sessions.barrier_dir(home), timeout=barrier_timeout, guard=lock, guard_root=store.root,
            )
        if marker < sessions.SUPPORTED_STATE_VERSION:
            sessions.write_state_marker(store.root)
        outcomes: list[RecordOutcome] = []
        backups = 0
        for path in store.scan_uuid_records():
            outcome, written = _migrate_locked(
                store, path.stem, cat, now, snapshot_, profile_exists
            )
            outcomes.append(outcome)
            backups += written
        pointers = []
        pointer_backups = 0
        files = len(list(store.pointers_dir.glob("*.json")))
        for digest in _pointer_digests(store.pointers_dir):
            outcome_, written = _apply_pointer(store, digest)
            pointers.append(outcome_)
            pointer_backups += written
    finally:
        if barrier is not None:
            barrier.release()
        lock.release()
    return MigrationReport(
        state_root=str(store.root),
        marker_before=marker,
        dry_run=False,
        records=tuple(outcomes),
        pointers=tuple(pointers),
        backups_written=backups,
        pointer_backups_written=pointer_backups,
        pointer_files=files,
    )


def migrate_one(
    store: sessions.SessionStore,
    cat: Any,
    managed_id: str,
    *,
    hook_command: str,
    hook_shim_path: Path | str,
    now: str | None = None,
    profile_exists: Callable[[str], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    home: Path | None = None,
    barrier_timeout: float | None = state.SERVED_BARRIER_TIMEOUT,
) -> RecordOutcome:
    """Launch-time migration of one record (§4.7; used by ``Runtime.prepare``).

    With ``home`` its own short EX phase also takes the gateway
    barrier (bounded); the card and the launch's commit phase come later.

    The shim assertion, then the migration lock exclusively with the bounded
    wait (``MigrationBusyError`` -> L-4, nothing written), the marker written
    directly when it reads 3, and steps 5.1-5.5 for this record under its
    lifecycle lock. No pointer merge (``last()`` reads both files).
    A blocked, failed or unreadable record raises.
    """

    now = now or _now()
    _check_shim(hook_command, hook_shim_path)
    raw, _raw_bytes = store.load_raw(managed_id)
    if raw["version"] == sessions.RECORD_VERSION:
        return _already_v4(raw)
    snapshot_ = default_settings_snapshot(cat)
    preview = _blocked(_convert_or_fail(managed_id, raw, cat, now, snapshot_), profile_exists)
    if preview.status != "migrate":
        raise MigrationError(f"session {managed_id} cannot be migrated: {preview.error}")
    lock = sessions.acquire_exclusive_bounded(store.root, clock=clock, sleep=sleep)
    barrier: state.BarrierToken | None = None
    try:
        if home is not None:
            barrier = state.acquire_served_barrier(
                sessions.barrier_dir(home), timeout=barrier_timeout, guard=lock, guard_root=store.root,
                clock=clock, sleep=sleep,
            )
        if sessions.check_state_marker(store.root) < sessions.SUPPORTED_STATE_VERSION:
            sessions.write_state_marker(store.root)
        outcome, _written = _migrate_locked(
            store, managed_id, cat, now, snapshot_, profile_exists
        )
    finally:
        if barrier is not None:
            barrier.release()
        lock.release()
    if outcome.status == "unreadable":
        raise sessions.SessionError(outcome.error or f"session {managed_id} is unreadable")
    if outcome.status not in ("migrate", "already-v4"):
        raise MigrationError(f"session {managed_id} cannot be migrated: {outcome.error}")
    return outcome


# ----------------------------------------------------------------- reports


def _type_label(item: RecordOutcome) -> str:
    if item.from_version in (1, 2):
        return f"L{item.from_version}"
    return "O" if item.session_type == sessions.SESSION_TYPE_ORDINARY else "M"


def _counted(items: Sequence[RecordOutcome]) -> list[RecordOutcome]:
    return [item for item in items if item.status in ("migrate", "blocked") and item.outcome]


def summary(report: MigrationReport) -> dict[str, dict[str, int]]:
    table = {name: {"managed": 0, "ordinary": 0} for name in OUTCOMES}
    for item in _counted(report.records):
        column = "ordinary" if item.session_type == sessions.SESSION_TYPE_ORDINARY else "managed"
        table[item.outcome][column] += 1
    return table


def outcome_line(item: RecordOutcome) -> str:
    """One per-record report line (also the launch-time ``migrated legacy record:`` text)."""

    m8 = item.managed_id[:8]
    if item.status == "already-v4":
        return f"{m8}  -  already-v4"
    if item.status in ("unreadable", "failed"):
        return f"{m8}  -  {item.status}: {item.error}"
    successor = f" ⇒ {item.successor}" if item.successor else ""
    composition = f": {item.composition_name}" if item.composition_name else ""
    notes = f"  notes: {'; '.join(item.notes)}" if item.notes else ""
    line = (
        f"{m8}  {_type_label(item)}  {item.outcome:<12}  lead {item.v3_lead} → "
        f"{item.v4_lead}{successor}  profile {item.profile or 'null'} "
        f"({item.profile_reason}{composition})  agents {len(item.agents)}{notes}"
    )
    if item.status == "blocked":
        line += f"  blocked: {item.error}"
    return line


def _lead_groups(items: Sequence[RecordOutcome], outcome: str) -> str:
    groups: dict[str, list[int]] = {}
    for item in items:
        if item.outcome != outcome:
            continue
        if outcome in ("successor", "renamed"):
            label = f"{item.v4_lead} → {item.successor}"
        else:
            label = str(item.v4_lead)
        counts = groups.setdefault(label, [0, 0])
        counts[1 if item.session_type == sessions.SESSION_TYPE_ORDINARY else 0] += 1
    if not groups:
        return "(none)"
    ordered = sorted(groups.items(), key=lambda pair: (-(pair[1][0] + pair[1][1]), pair[0]))
    return ", ".join(
        f"{label} {counts[0]}" + (f"+{counts[1]}" if counts[1] else "")
        for label, counts in ordered
    )


def render_text(report: MigrationReport) -> str:
    """§4.8 (exact; ``--dry-run`` and the real run share the format)."""

    dry = report.dry_run
    lines = [f"claude-multi migrate{' --dry-run' if dry else ''}: state {report.state_root}"]
    before = (
        "absent (state 3)"
        if report.marker_before < sessions.SUPPORTED_STATE_VERSION
        else str(report.marker_before)
    )
    if report.marker_before < sessions.SUPPORTED_STATE_VERSION:
        after = "4 (would write)" if dry else "4 (written)"
    else:
        after = "4 (unchanged)"
    lines.append(f"marker: {before} → {after}")
    records = report.records
    count = lambda predicate: sum(1 for item in records if predicate(item))  # noqa: E731
    lines.append(
        f"records: {len(records)} ("
        f"{count(lambda i: i.from_version == 3 and i.status != 'already-v4')} v3, "
        f"{count(lambda i: i.from_version in (1, 2))} v1/v2, "
        f"{count(lambda i: i.status == 'already-v4')} already v4, "
        f"{count(lambda i: i.status == 'unreadable')} unreadable, "
        f"{count(lambda i: i.status == 'failed')} failed, "
        f"{count(lambda i: i.status == 'blocked')} blocked)"
    )
    lines.append("")
    table = summary(report)
    lines.append(f"{'outcome':<15}{'managed':>7}  {'ordinary':>8}  {'total':>5}")
    total_m = total_o = 0
    for name in OUTCOMES:
        managed, ordinary = table[name]["managed"], table[name]["ordinary"]
        total_m += managed
        total_o += ordinary
        lines.append(f"{name:<15}{managed:>7}  {ordinary:>8}  {managed + ordinary:>5}")
    lines.append(f"{'total':<15}{total_m:>7}  {total_o:>8}  {total_m + total_o:>5}")
    lines.append("")
    counted = _counted(records)
    lines.append("leads:")
    for name in OUTCOMES:
        lines.append(f"  {name}: {_lead_groups(counted, name)}")
    lines.append("")
    lines.append("profiles:")
    profiles: dict[str, dict[str, int]] = {}
    for item in counted:
        reasons = profiles.setdefault(item.profile or "null", {})
        reasons[item.profile_reason] = reasons.get(item.profile_reason, 0) + 1
    if not profiles:
        lines.append("  (none)")
    for name in sorted(profiles):
        reasons = profiles[name]
        detail = ", ".join(f"{reason} {reasons[reason]}" for reason in sorted(reasons))
        lines.append(f"  {name}: {sum(reasons.values())} ({detail})")
    lines.append("")
    lines.append("records:")
    if not records:
        lines.append("  (none)")
    for item in records:
        lines.append(f"  {outcome_line(item)}")
    lines.append("")
    live = [item for item in records if item.status != "unreadable" and item.last_event_source != "end"]
    lines.append(f"live during migration: {len(live)}")
    if not live:
        lines.append("  (none)")
    for item in live:
        lines.append(
            f"  {item.managed_id[:8]}  last event {item.last_event_source or 'none'} at "
            f"{item.last_seen_at}: verify with claude-multi sessions show {item.managed_id}; "
            "if it was /clear-ed or resumed while this ran, claude-multi sessions "
            f"relink-runtime {item.managed_id} <runtime-id>"
        )
    lines.append("")
    if report.transcripts is None:
        lines.append("transcripts: not checked")
    else:
        lines.append(
            f"transcripts: {len(report.transcripts)} record(s) whose transcript is missing "
            f"or older than cleanupPeriodDays ({report.cleanup_days})"
        )
        if not report.transcripts:
            lines.append("  (none)")
        for note in report.transcripts:
            what = (
                "transcript missing"
                if note.status == "missing"
                else f"transcript older than {report.cleanup_days} days ({note.age_days} days; "
                "Claude deletes it at its next cleanup)"
            )
            lines.append(
                f"  {note.managed_id[:8]}  {what}: claude-multi sessions forget {note.managed_id}"
            )
    lines.append("")
    digests = {item.digest for item in report.pointers}
    lines.append(f"pointers: {report.pointer_files} files, {len(digests)} cwds")
    shown = 0
    for item in report.pointers:
        if item.action == "merge":
            lines.append(
                f"  {item.digest[:8]}  merge: kept {item.kept[:8]} (newer last_seen_at), "
                f"dropped {item.dropped[:8]}"
            )
        elif item.action == "rename-ordinary":
            lines.append(f"  {item.digest[:8]}  rename-ordinary: {item.kept[:8]}")
        elif item.action == "unreadable":
            lines.append(f"  {item.digest[:8]}  unreadable")
        else:
            continue
        shown += 1
    if not shown:
        lines.append("  (none)")
    lines.append("")
    lines.append(
        f"backups: {'would write' if dry else 'wrote'} {report.backups_written} record backups "
        f"(sessions/<id>.v3.json), {report.pointer_backups_written} pointer backups"
    )
    return "\n".join(lines) + "\n"


def render_json(report: MigrationReport) -> bytes:
    """``--json``: canonical JSON (RecordOutcome fields minus ``record``)."""

    def record_doc(item: RecordOutcome) -> dict[str, Any]:
        document = dataclasses.asdict(item)
        document.pop("record")
        document["agents"] = list(item.agents)
        document["notes"] = list(item.notes)
        return document

    document: dict[str, Any] = {
        "marker_before": report.marker_before,
        "dry_run": report.dry_run,
        "records": [record_doc(item) for item in report.records],
        "pointers": [dataclasses.asdict(item) for item in report.pointers],
        "summary": summary(report),
    }
    if report.transcripts is not None:
        document["transcripts"] = [dataclasses.asdict(item) for item in report.transcripts]
    return strict_json.canonical_file_bytes(document)


# ---------------------------------------------------------------- restore-2x


@dataclass
class RestoreReport:
    state_root: str
    stamp: str
    restored: list[tuple[str, int, int, bool]] = field(default_factory=list)  # (id, from, epoch, ended)
    quarantined: list[str] = field(default_factory=list)
    kept: list[tuple[str, bool]] = field(default_factory=list)  # (id, marked ended)
    pointers_restored: int = 0
    pointers_rewritten: int = 0
    pointers_cleared: int = 0
    backups_retired: int = 0
    marker_removed: bool = False


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _retire(source: Path, target: Path) -> None:
    """``os.rename`` into a 0700 retirement tree, with both directories fsynced."""

    state.ensure_private_dir(target.parent)
    os.rename(source, target)
    _fsync_dir(target.parent)
    _fsync_dir(source.parent)


def _stamp(now: str) -> str:
    return now.replace("-", "").replace(":", "")


def _synthetic_end(view: dict[str, Any], now: str) -> dict[str, Any]:
    return {
        **view,
        "last_event_source": "end",
        "last_end_reason": sessions.MARKED_ENDED_REASON,
        "last_seen_at": now,
    }


def restore_live_lines(
    refusals: Sequence[tuple[str, str | None]],
) -> str:
    """The §15.4 refusal sentence for live/unknown records ``(id, last event)``."""

    listed = ", ".join(f"{mid[:8]} (last event {src or 'none'})" for mid, src in refusals)
    return (
        f"claude-multi: restore-2x refused: {len(refusals)} session(s) may still be "
        f"running (live or unknown): {listed}. Stop them (claude-multi sessions stop "
        "<id>); for a session you know has exited without a SessionEnd, rerun with "
        "--not-running <id>. Nothing was changed."
    )


def _restore_one(
    store: sessions.SessionStore,
    managed: str,
    samples: Mapping[str, tuple[str | None, str | None]],
    overrides: set[str],
    dead: set[str],
    now: str,
    retired_root: Path,
    quarantine_root: Path,
    restored_types: dict[str, str],
    report: "RestoreReport",
) -> None:
    """§5.1 step 4 for one record; the caller holds its lifecycle lock."""

    raw, raw_bytes = store.load_raw(managed)
    view = sessions._lifecycle_view(raw)
    source = view.get("last_event_source")
    if (source != "end" and managed not in overrides) or (
        _record_fingerprint(raw_bytes) != samples[managed][1] or source != samples[managed][0]
    ):
        raise RestoreStopped(
            f"restore-2x stopped: {managed[:8]} changed while restoring (a session event "
            "or launcher write reached it); the marker is still 4 — rerun claude-multi "
            "restore-2x",
            report,
        )
    backup = store.backup_path(managed)
    ended = managed in dead and source != "end"
    if raw["version"] == sessions.RECORD_VERSION:
        if os.path.lexists(backup):
            backup_raw = strict_json.loads(state.read_private(backup))
            restored = sessions.restore_overlay(backup_raw, raw)
            if ended:
                restored = _synthetic_end(restored, now)
            store._save_lifecycle(restored)
            _retire(backup, retired_root / "sessions" / backup.name)
            report.backups_retired += 1
            report.restored.append((managed, backup_raw["version"], restored["launch_epoch"], ended))
            restored_types[managed] = restored["session_type"]
        else:
            path = store.sessions_dir / f"{managed}.json"
            _retire(path, quarantine_root / "sessions" / f"{managed}.json")
            store.clear_last(raw["cwd"], managed, blocking=True)
            report.quarantined.append(managed)
        return
    if ended:
        store._save_lifecycle(sessions._patch_lifecycle(raw, _synthetic_end(view, now)))
    if os.path.lexists(backup):
        _retire(backup, retired_root / "sessions" / backup.name)
        report.backups_retired += 1
    report.kept.append((managed, ended))
    restored_types[managed] = view["session_type"]


def _record_fingerprint(raw_bytes: bytes) -> str:
    """The whole on-disk record (runtime identity, epoch, lifecycle, token):
    any SessionStart, resume or launcher write changes it."""

    return hashlib.sha256(raw_bytes).hexdigest()


def _restore_preview(
    store: sessions.SessionStore,
    overrides: set[str],
    live_check: Callable[[dict[str, Any]], bool],
) -> tuple[list[str], dict[str, tuple[str | None, str | None]], list[tuple[str, str | None]], list[str], bool]:
    """Lock-free snapshot of every record for ``restore-2x``:
    ``(ids, samples, live_pairs, refusal_lines, hard)``. Read-only. A
    sample is ``(last event, fingerprint of the complete record bytes)``."""

    ids0 = [path.stem for path in store.scan_uuid_records()]
    samples: dict[str, tuple[str | None, str | None]] = {}
    refusal_lines: list[str] = []
    live_pairs: list[tuple[str, str | None]] = []
    hard = False
    for managed in ids0:
        try:
            raw, raw_bytes = store.load_raw(managed)
        except sessions.SessionError as exc:
            hard = True
            refusal_lines.append(
                f"{managed[:8]}: record unreadable ({exc}); fix it or run claude-multi "
                f"sessions forget {managed} (this drops its legacy backup)"
            )
            continue
        view = sessions._lifecycle_view(raw)
        samples[managed] = (view.get("last_event_source"), _record_fingerprint(raw_bytes))
        if live_check(view):
            hard = True
            if managed in overrides:
                refusal_lines.append(
                    f"claude-multi: {managed[:8]} is live in the Claude background "
                    "daemon; --not-running cannot override that"
                )
            else:
                live_pairs.append((managed, view.get("last_event_source")))
            continue
        if view.get("last_event_source") != "end" and managed not in overrides:
            live_pairs.append((managed, view.get("last_event_source")))
        if (
            raw["version"] == sessions.RECORD_VERSION
            and "migration" in raw
            and not os.path.lexists(store.backup_path(managed))
        ):
            hard = True
            refusal_lines.append(
                f"{managed[:8]}: migrated record without its legacy backup "
                f"(sessions/{managed}.v3.json missing); restore it from a copy or run "
                f"claude-multi sessions forget {managed}"
            )
    return ids0, samples, live_pairs, refusal_lines, hard


RESTORE_CHANGED = (
    "restore-2x: session records changed after confirmation — nothing restored; rerun "
    "claude-multi restore-2x"
)


def restore_2x(
    store: sessions.SessionStore,
    *,
    home: Path,
    not_running: Iterable[str] = (),
    assume_dead: Iterable[str] = (),
    is_live: Callable[[dict[str, Any]], bool] | None = None,
    confirm: Callable[[Sequence[tuple[str, str | None]]], bool] | None = None,
    now: str | None = None,
    barrier_timeout: float | None = state.SERVED_BARRIER_TIMEOUT,
    refresh_live: Callable[[], Callable[[dict[str, Any]], bool]] | None = None,
) -> RestoreReport:
    """``claude-multi restore-2x`` (§5.1) under the exclusive migration lock.

    The caller checked the marker reads 4. ``is_live(view)`` is the ●
    liveness (daemon socket or ``/proc`` cmdline): never overridable.
    ``not_running`` excludes records from the last-event liveness rule;
    ``assume_dead`` does too and also records their end (a synthetic
    ``end``). ``confirm(pairs)`` is the interactive list-and-confirm
    path: yes treats every overridable live record as ``assume_dead``.
    Raises :class:`RestoreRefused` (nothing written) or
    :class:`RestoreStopped` (part-way; the marker is kept, a rerun continues).

    The preview and the confirmation run with no lock
    held; the commit phase then takes the migration lock EXCLUSIVELY, then
    the gateway served-change barrier (``home``-relative; bounded wait),
    and revalidates the whole snapshot — any record added, removed or
    changed since the confirmation (its complete bytes: a SessionStart that
    resumes it under a new runtime UUID included) refuses with nothing
    restored. The revalidation and every restore write run under the
    runtime-index lock (then each lifecycle lock), so no hook can interleave.
    ``refresh_live()`` returns fresh liveness evidence for that commit-phase
    revalidation (the caller's process inventory is re-read; it raises
    :class:`RestoreRefused` when liveness is unknown); without it ``is_live``
    is reused.
    """

    now = now or _now()
    root = store.root
    report = RestoreReport(state_root=str(root), stamp=_stamp(now))
    overrides = set(not_running)
    dead = set(assume_dead)
    overrides |= dead
    live_check = is_live or (lambda _view: False)
    ids0, samples, live_pairs, refusal_lines, hard = _restore_preview(store, overrides, live_check)
    if live_pairs and not hard and confirm is not None and confirm(tuple(live_pairs)):
        for managed, _src in live_pairs:
            overrides.add(managed)
            dead.add(managed)
        live_pairs = []
    if live_pairs or refusal_lines:
        lines = []
        if live_pairs:
            lines.append(restore_live_lines(live_pairs))
            lines.append(
                "  or record their end first: claude-multi sessions mark-ended <id> "
                "(or rerun with --assume-dead <id>)"
            )
        lines.extend(refusal_lines)
        raise RestoreRefused(lines)
    lock = sessions.migration_lock(root)
    lock.acquire(blocking=True)
    try:
        try:
            barrier = state.acquire_served_barrier(
                sessions.barrier_dir(home), timeout=barrier_timeout, guard=lock, guard_root=root,
            )
        except state.BarrierBusyError as exc:
            raise RestoreRefused([f"restore-2x: {exc.strerror} — nothing restored; retry"]) from exc
        try:
            index_lock = store.runtime_index_lock()
            index_lock.acquire(blocking=True)
            try:
                fresh_check = refresh_live() if refresh_live is not None else live_check
                again = _restore_preview(store, overrides, fresh_check)
                if (again[0], again[1]) != (ids0, samples) or again[2] or again[3]:
                    raise RestoreRefused([RESTORE_CHANGED])
                return _restore_locked(store, ids0, samples, overrides, dead, now, report)
            finally:
                index_lock.release()
        finally:
            barrier.release()
    finally:
        lock.release()


def _restore_locked(
    store: sessions.SessionStore,
    ids0: list[str],
    samples: dict[str, tuple[str | None, str | None]],
    overrides: set[str],
    dead: set[str],
    now: str,
    report: RestoreReport,
) -> RestoreReport:
    """The commit phase body (EX migration lock, barrier and runtime-index held)."""

    root = store.root
    retired_root = root / "restored-2x" / report.stamp
    quarantine_root = root / "quarantine-3x" / report.stamp
    restored_types: dict[str, str] = {}
    for managed in sorted(ids0):
        lifecycle = store.lifecycle_lock(managed)
        lifecycle.acquire(blocking=True)
        try:
            _restore_one(
                store, managed, samples, overrides, dead, now, retired_root,
                quarantine_root, restored_types, report,
            )
        except RestoreStopped:
            raise
        except (sessions.SessionError, state.StateError, OSError, ValueError) as exc:
            raise RestoreStopped(
                f"restore-2x stopped: {managed[:8]} changed while restoring (restore "
                f"failed: {exc}); the marker is still 4 — rerun claude-multi restore-2x",
                report,
            ) from exc
        finally:
            lifecycle.release()
    # Backups whose record is gone (forgotten after migration): retired, never resurrected.
    for backup in sorted(store.sessions_dir.glob(f"*{sessions.BACKUP_SUFFIX}")):
        stem = backup.name[: -len(sessions.BACKUP_SUFFIX)]
        if sessions.UUID4.fullmatch(stem) and not os.path.lexists(
            store.sessions_dir / f"{stem}.json"
        ):
            _retire(backup, retired_root / "sessions" / backup.name)
            report.backups_retired += 1
    _restore_pointers(store, restored_types, retired_root, report)
    rescan = {path.stem for path in store.scan_uuid_records()}
    new_ids = sorted(rescan - set(ids0))
    if new_ids:
        raise RestoreStopped(
            f"restore-2x stopped: {new_ids[0][:8]} changed while restoring (new record "
            f"{', '.join(new_ids)} appeared); the marker is still 4 — rerun claude-multi "
            "restore-2x",
            report,
        )
    sessions.remove_state_marker(root)
    report.marker_removed = True
    return report


def _restore_pointers(
    store: sessions.SessionStore,
    restored_types: Mapping[str, str],
    retired_root: Path,
    report: RestoreReport,
) -> None:
    """§5.1 step 5 (sc[14]): only candidates naming a restored/kept record survive."""

    pointers = store.pointers_dir
    digests = set(_pointer_digests(pointers))
    for backup in pointers.glob(f"*.json{sessions.POINTER_BACKUP_SUFFIX}"):
        digest = backup.name.split(".", 1)[0]
        if len(digest) == 32:
            digests.add(digest)
    for digest in sorted(digests):
        primary = pointers / f"{digest}.json"
        legacy = pointers / f"{digest}.ordinary.json"
        locks = [state.FileLock(path) for path in sorted((primary, legacy), key=str)]
        for lock in locks:
            lock.acquire(blocking=True)
        try:
            # candidates: target name -> [(from_backup, record id, bytes)]
            candidates: dict[Path, list[tuple[bool, str, bytes]]] = {}
            backups = []
            for target in (primary, legacy):
                backup = target.with_name(target.name + sessions.POINTER_BACKUP_SUFFIX)
                if not os.path.lexists(backup):
                    continue
                backups.append(backup)
                try:
                    payload = _read_pointer(backup)
                except ValueError:
                    continue
                if payload is not None and payload["session_id"] in restored_types:
                    candidates.setdefault(target, []).append(
                        (True, payload["session_id"], state.read_private(backup))
                    )
            current: dict[Path, dict[str, Any] | None] = {}
            for path in (primary, legacy):
                try:
                    current[path] = _read_pointer(path)
                except ValueError:
                    current[path] = None
                payload = current[path]
                if payload is None or payload["session_id"] not in restored_types:
                    continue
                session_type = restored_types[payload["session_id"]]
                target = legacy if session_type == sessions.SESSION_TYPE_ORDINARY else primary
                rewritten = {
                    "cwd": payload.get("cwd"),
                    "session_id": payload["session_id"],
                    "session_type": session_type,
                }
                candidates.setdefault(target, []).append(
                    (False, payload["session_id"], strict_json.canonical_file_bytes(rewritten))
                )
            winners: dict[Path, tuple[bool, str, bytes]] = {}
            for target, options in candidates.items():
                best = None
                for option in options:
                    if best is None:
                        best = option
                        continue
                    if option[1] == best[1]:
                        best = best if best[0] else option  # same record: the backup wins
                        continue
                    seen_best = _last_seen(store, best[1]) or ""
                    seen_option = _last_seen(store, option[1]) or ""
                    if seen_option > seen_best or (seen_option == seen_best and option[0] and not best[0]):
                        best = option
                winners[target] = best
            for path in (primary, legacy):
                if path in winners:
                    from_backup, _session, data = winners[path]
                    if not os.path.lexists(path) or state.read_private(path) != data:
                        state.atomic_write(path, data)
                    if from_backup:
                        report.pointers_restored += 1
                    else:
                        report.pointers_rewritten += 1
                elif os.path.lexists(path):
                    state.remove_private(path)
                    report.pointers_cleared += 1
            for backup in backups:
                _retire(backup, retired_root / "pointers" / backup.name)
        finally:
            for lock in reversed(locks):
                lock.release()


def render_restore(report: RestoreReport) -> str:
    """§5.3 (stdout)."""

    lines = [f"claude-multi restore-2x: state {report.state_root} (marker 4)"]
    for managed, from_version, epoch, ended in report.restored:
        version = "v3" if from_version == 3 else f"v{from_version}→3"
        suffix = ", marked ended" if ended else ""
        lines.append(
            f"restored     {managed[:8]}  v4 → {version} (lifecycle, epoch {epoch}, cwd from "
            f"the current record{suffix})"
        )
    for managed in report.quarantined:
        lines.append(
            f"quarantined  {managed[:8]}  current-format (no legacy backup) → "
            f"quarantine-3x/{report.stamp}/sessions/{managed}.json"
        )
    for managed, ended in report.kept:
        lines.append(f"kept         {managed[:8]}  already legacy{' (marked ended)' if ended else ''}")
    lines.append(
        f"pointers: restored {report.pointers_restored}, rewritten "
        f"{report.pointers_rewritten}, cleared {report.pointers_cleared}"
    )
    lines.append(f"backups retired to restored-2x/{report.stamp}/")
    if report.marker_removed:
        lines.append("marker removed.")
        lines.append(
            "Next: start the earlier launcher, then run `claude-multi doctor`; "
            "if it reports scope drift for sessions relaunched under this release, run "
            "`claude-multi doctor --repair-all` there. Live lineup changes made under this "
            "release are not carried back."
        )
    return "\n".join(lines) + "\n"


RESTORE_CHECK_CAVEAT = (
    "This checks rollback metadata readiness, not a successful rollback.\n"
    "No transcript was read and no state was changed."
)


def restore_check(root: Path, config_root: Path) -> dict[str, Any]:
    """Read-only retained legacy backup inspection, not a rollback plan.

    Validate record snapshots using the same overlay as restore. Never construct
    a writable store or infer that missing historical config/transcripts are safe.
    Directory enumeration is explicit: permission errors are not empty state.
    """
    checks: list[dict[str, str]] = []
    marker = None
    refused = False
    try:
        try:
            root_info = root.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid():
                raise OSError("unsafe state root")
        marker = sessions.check_state_marker(root)
        store = sessions.SessionStore(root, sessions.default_schema(), read_only=True)
        try:
            info = store.sessions_dir.lstat()
        except FileNotFoundError:
            names = []
        else:
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("unsafe sessions directory")
            names = list(store.sessions_dir.iterdir())
        for path in sorted(names):
            if path.suffix != ".json" or not sessions.UUID4.fullmatch(path.stem):
                continue
            try:
                raw, _ = store.load_raw(path.stem)
                backup = store.backup_path(path.stem)
                try:
                    backup.lstat()
                except FileNotFoundError:
                    if "migration" in raw:
                        raise ValueError("missing backup")
                    checks.append({"path": str(path), "status": "quarantine on restore" if raw["version"] == 4 else "kept"})
                    continue
                saved = strict_json.loads(state.read_private(backup))
                store._validate(saved, "rollback backup")
                if saved["version"] >= sessions.RECORD_VERSION:
                    raise ValueError("not a legacy backup")
                if raw["version"] == sessions.RECORD_VERSION:
                    sessions.restore_overlay(saved, raw)
                checks.append({"path": str(backup), "status": "compatible record snapshot"})
            except (OSError, ValueError, sessions.SessionError, state.StateError):
                refused = True
                checks.append({"path": str(path), "status": "missing, unsafe or incompatible backup"})
        try:
            store.pointers_dir.lstat()
        except FileNotFoundError:
            pass
        else:
            if store.pointers_dir.is_symlink():
                raise OSError("unsafe pointers directory")
            for path in sorted(store.pointers_dir.iterdir()):
                if path.name.endswith(sessions.POINTER_BACKUP_SUFFIX):
                    try:
                        _read_pointer(path)
                        checks.append({"path": str(path), "status": "pointer snapshot readable"})
                    except (OSError, ValueError, state.StateError, MigrationError):
                        refused = True
                        checks.append({"path": str(path), "status": "unsafe or incompatible pointer backup"})
    except (OSError, sessions.SessionError, state.StateError):
        refused = True
        checks.append({"path": str(root), "status": "state metadata unavailable or incompatible"})
    return {"schema_version": 1, "target": "2.x", "state_root": str(root),
            "config_root": str(config_root), "state_marker": marker,
            "status": "refused" if refused else "unknown",
            "checks": checks, "config_backups": "unknown — config is not restored by restore-2x",
            "transcripts": "unknown", "liveness": "not checked", "caveat": RESTORE_CHECK_CAVEAT}
