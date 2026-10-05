"""Session facts."""

from __future__ import annotations

from claude_multi import paths

from claude_multi import errors as cli_errors

from typing import Any
from pathlib import Path
from claude_multi import compiler
from datetime import datetime
from claude_multi import errors
from dataclasses import field
import os
from claude_multi import sessions
from claude_multi import termtext
from datetime import timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def _project_agent_files(cwd: Path | str) -> list[Path]:
    """Project agent files visible from ``cwd`` (native precedence).

    Mirrors the ``find_cm_collisions`` walk read-only: every
    ``.claude/agents/`` directory from ``cwd`` up to and including the git
    root (first ancestor containing ``.git``) or the filesystem root,
    deduplicated by resolved path. Unreadable directories are skipped.
    """

    files: list[Path] = []
    seen: set[Path] = set()
    start = Path(cwd).resolve()
    for ancestor in (start, *start.parents):
        candidate = paths.project_agents(ancestor)
        if candidate.is_dir():
            try:
                key = candidate.resolve()
            except OSError:
                key = candidate
            if key not in seen:
                seen.add(key)
                try:
                    entries = sorted(candidate.iterdir())
                except OSError:
                    entries = []
                for entry in entries:
                    try:
                        if entry.is_file() and entry.suffix == ".md":
                            files.append(entry)
                    except OSError:
                        continue
        if (ancestor / ".git").exists():
            break
    return files


def _collision_error(path: Path, name: str, cwd: str) -> str:
    """UX §10 collision language, verbatim."""

    return (
        f"project agent {name!r} at {os.path.relpath(path, cwd)} collides with "
        "the managed cm-* namespace. Rename or remove it, or launch from a "
        "different directory."
    )


def pending_fork_message(record: dict[str, Any]) -> str:
    """Alias kept next to the card/guard call sites (canonical: sessions)."""

    return sessions.pending_fork_message(record)


def _session_record_scan(
    runtime: runtime_mod.Runtime,
) -> tuple[list[dict[str, Any]], list[str], set[str]]:
    """Readable records, load problems, and every UUID-shaped record key."""

    return runtime.report_value("records", lambda: _read_session_records(runtime))


def _read_session_records(runtime):
    records: list[dict[str, Any]] = []
    problems: list[str] = []
    record_ids: set[str] = set()
    try:
        entries = sorted(runtime.session_store.sessions_dir.iterdir())
    except FileNotFoundError:
        entries = []
    except OSError:
        return [], ["session record directory is unavailable; records are unknown"], set()
    for path in entries:
        if path.suffix != ".json":
            continue
        session_id = path.stem
        if not sessions.UUID4.fullmatch(session_id):
            continue
        record_ids.add(session_id)
        try:
            records.append(runtime.session_store.load(session_id))
        except sessions.SessionError as exc:
            problems.append(f"session record {session_id} is unreadable: {exc}")
    return records, problems, record_ids


def _session_records(runtime: runtime_mod.Runtime) -> list[dict[str, Any]]:
    records, _problems, _record_ids = _session_record_scan(runtime)
    return records


def _discover_native_sessions(
    runtime: runtime_mod.Runtime, *, limit: int = 20, cwd_filter: str | None = None
) -> list[dict[str, Any]]:
    """Metadata-only, UUID-deduplicated discovery of unmanaged sessions."""

    home = Path(runtime.environ.get("HOME") or Path.home())
    projects = paths.native_projects(home, runtime.environ)
    records, _record_problems, record_ids = _session_record_scan(runtime)
    managed_runtime_ids: set[str] = set(record_ids)
    for record in records:
        managed_runtime_ids.add(record["runtime_session_id"])
        managed_runtime_ids.update(
            item["session_id"] for item in record.get("runtime_aliases", [])
        )
    fork_of: dict[str, str] = {}
    for record in records:
        for item in record.get("pending_forks", []):
            fork_id = item.get("session_id")
            if fork_id and fork_id not in managed_runtime_ids:
                fork_of.setdefault(fork_id, sessions.managed_id(record))
    by_id: dict[str, dict[str, Any]] = {}
    try:
        for project_dir in projects.iterdir():
            if not project_dir.is_dir():
                continue
            for entry in project_dir.glob("*.jsonl"):
                session_id = entry.stem
                if (
                    not sessions.UUID4.fullmatch(session_id)
                    or session_id in managed_runtime_ids
                ):
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                item = by_id.setdefault(
                    session_id,
                    {"session_id": session_id, "slugs": set(), "mtime": mtime},
                )
                item["slugs"].add(project_dir.name)
                item["mtime"] = max(item["mtime"], mtime)
    except OSError:
        return []

    found: list[dict[str, Any]] = []
    for item in by_id.values():
        slugs = tuple(sorted(item["slugs"]))
        current_slug = _native_project_slug(runtime.cwd)
        cwd_candidates = {
            path
            for slug in slugs
            for path in _decode_project_slug_candidates(slug)
        }
        if current_slug in slugs:
            cwd = runtime.cwd
        else:
            cwd = str(next(iter(cwd_candidates))) if len(cwd_candidates) == 1 else None
        if cwd_filter is not None and cwd != cwd_filter:
            continue
        found.append(
            {
                "session_id": item["session_id"],
                "slugs": slugs,
                "slug": slugs[0] if len(slugs) == 1 else "(ambiguous)",
                "cwd": cwd,
                "mtime": item["mtime"],
                "fork_of": fork_of.get(item["session_id"]),
            }
        )
    found.sort(key=lambda item: item["mtime"], reverse=True)
    return found[:limit]


def _record_age_of(record: dict[str, Any], field: str, *, now: datetime | None = None) -> str:
    """Relative age of one record timestamp field ("2h ago"); raw value on parse failure."""

    try:
        stamp = datetime.strptime(record[field], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (KeyError, ValueError):
        return str(record.get(field, "?"))
    current = now or datetime.now(timezone.utc)
    seconds = max(0, int((current - stamp).total_seconds()))
    return _age_from_seconds(seconds)


def _record_age(record: dict[str, Any], *, now: datetime | None = None) -> str:
    """Relative session age for display ("2h ago"); ISO string on parse failure."""

    return _record_age_of(record, "created_at", now=now)


def _record_last_seen(record: dict[str, Any]) -> str:
    """Effective last-activity timestamp for sort and display.

    Creation is itself a use: a missing, malformed, or pre-creation
    `last_seen_at` (clock skew, hand-edits) falls back to `created_at`.
    """

    created = record["created_at"]
    last_seen = record.get("last_seen_at")
    if isinstance(last_seen, str) and last_seen >= created:
        try:
            datetime.strptime(last_seen, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return created
        return last_seen
    return created


def _last_used_age(effective: str, *, now: datetime | None = None) -> str:
    """Relative age of an effective last-used timestamp (ISO string on failure)."""

    try:
        stamp = datetime.strptime(effective, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return effective
    current = now or datetime.now(timezone.utc)
    seconds = max(0, int((current - stamp).total_seconds()))
    return _age_from_seconds(seconds)


def _record_last_used_age(record: dict[str, Any], *, now: datetime | None = None) -> str:
    """Relative age of the effective last-used timestamp (same value as sort)."""

    return _last_used_age(_record_last_seen(record), now=now)


def _record_sort_key_last_used(record: dict[str, Any]) -> str:
    """Descending-sort key: the effective last-used timestamp."""

    return _record_last_seen(record)


def _mtime_age(mtime: float, *, now: datetime | None = None) -> str:
    """Relative age from a filesystem mtime (native session rows)."""

    current = now or datetime.now(timezone.utc)
    seconds = max(0, int(current.timestamp() - mtime))
    return _age_from_seconds(seconds)


def _age_from_seconds(seconds: int) -> str:
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _liveness(
    runtime: runtime_mod.Runtime,
    record: dict[str, Any],
    prefixes: frozenset[str],
    proc_ids: frozenset[str],
) -> str:
    """The three on-screen states: ``live`` / ``ended`` / ``unknown``.

    ``live`` = a daemon pty socket or a ``/proc`` argv names the record;
    otherwise ``ended`` when its last event was ``end``, else ``unknown``
    (treated as running by propagation and the lineup dialog).
    Uses ``_record_view_is_live`` (the doctor/restore-2x ● rule, defined
    later in this module).  Callers take ``prefixes =
    runtime._live_prefixes()`` and ``proc_ids = runtime._proc_ids()`` once
    per screen open or reload; ``runtime`` is the seam owner and is not
    read here.
    """

    del runtime
    if _record_view_is_live(record, prefixes=prefixes, proc_ids=proc_ids):
        return "live"
    return "ended" if record.get("last_event_source") == "end" else "unknown"


def _live_background_prefixes(root: Path | None = None) -> frozenset[str]:
    """Daemon-hosted session-id prefixes (``sessions.live_background_prefixes``)."""

    return sessions.live_background_prefixes(root)


def _record_is_live(record: dict[str, Any], prefixes: frozenset[str]) -> bool:
    if not prefixes:
        return False
    candidates = [sessions.managed_id(record), record["runtime_session_id"]]
    candidates.extend(
        item["session_id"] for item in record.get("runtime_aliases", [])
    )
    return any(
        candidate.startswith(prefix)
        for candidate in candidates
        for prefix in prefixes
    )


def _native_is_live(item: dict[str, Any], prefixes: frozenset[str]) -> bool:
    return any(item["session_id"].startswith(prefix) for prefix in prefixes)


def _record_identity_label(record: dict[str, Any], *, short: bool = False) -> str:
    stable = sessions.managed_id(record)
    runtime_id = record["runtime_session_id"]
    same = stable == runtime_id
    if short:
        stable = stable[:8] + "…"
        runtime_id = runtime_id[:8] + "…"
    if same:
        return stable
    return f"{stable} → runtime {runtime_id}"


def _record_target_label(record: dict[str, Any]) -> str:
    if record.get("version") == sessions.RECORD_VERSION:
        if record["profile"] is not None:
            return f"profile:{record['profile']}"
        return f"direct:{record['applied']['lead']['key']}"
    if record["session_type"] == sessions.SESSION_TYPE_ORDINARY:
        return f"gateway:{record['ordinary_model']}"
    return f"cm:{record['composition_name']}"


def _record_mode_label(record: dict[str, Any]) -> str:
    """The sessions-list mode column: lineup or durable with its generation, or legacy."""

    if record.get("version") == sessions.RECORD_VERSION:
        return f"lineup(g{record['lineup_generation']})"
    if record["mode"] == "durable":
        return f"durable(g{record['scope_generation']})"
    return "legacy"


def _parse_registry_instant(value: str) -> datetime | None:
    try:
        if len(value) == 10:
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _record_view_is_live(
    view: dict[str, Any],
    *,
    prefixes: frozenset[str],
    proc_ids: frozenset[str],
) -> bool:
    """● liveness of a record view: a daemon pty socket or a ``/proc`` argv."""

    try:
        daemon = _record_is_live(view, prefixes)
    except (KeyError, sessions.SessionError):
        daemon = False
    return daemon or sessions.record_is_proc_live(view, proc_ids)


def _record_resume_names(record: dict[str, Any]) -> set[str]:
    """Names ``-r`` accepts for a record besides its UUID."""

    cwd = record.get("cwd")
    names: set[str] = set()

    def add(label: str | None, prefix: str = "cm:") -> None:
        if not label:
            return
        names.add(label)
        names.add(f"{prefix}{label}")
        names.add(compiler.session_display_name(f"{prefix}{label}", cwd))

    if record.get("version") == sessions.RECORD_VERSION:
        if record["profile"] is not None:
            add(record["profile"])
        key = record["applied"]["lead"]["key"]
        names.add(f"cm:direct:{key}")
        names.add(compiler.session_display_name(f"cm:direct:{key}", cwd))
        migration = record.get("migration") or {}
        add(migration.get("composition_name"))
        if migration.get("session_type") == sessions.SESSION_TYPE_ORDINARY:
            names.add(f"cg:{key}")
            names.add(compiler.session_display_name(f"cg:{key}", cwd))
        return names
    if record.get("session_type") == sessions.SESSION_TYPE_MANAGED:
        add(record.get("composition_name"))
    return names


def _js_utf16_units(text: str) -> list[int]:
    raw = text.encode("utf-16-le", errors="surrogatepass")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def _base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = digits[remainder] + result
    return result


def _sanitize_native_slug(text: str) -> tuple[str, list[int]]:
    units = _js_utf16_units(text)
    sanitized = "".join(
        chr(unit)
        if (
            0x30 <= unit <= 0x39
            or 0x41 <= unit <= 0x5A
            or 0x61 <= unit <= 0x7A
        )
        else "-"
        for unit in units
    )
    return sanitized, units


def _native_project_slug(path: Path | str) -> str:
    """Exact Claude 2.1.217 project-directory slug for one path."""

    text = str(path)
    sanitized, units = _sanitize_native_slug(text)
    if len(sanitized) <= 200:
        return sanitized
    hashed = 0
    for unit in units:
        hashed = (hashed * 31 + unit) & 0xFFFFFFFF
    if hashed & 0x80000000:
        hashed -= 0x100000000
    return f"{sanitized[:200]}-{_base36(abs(hashed))}"


def _native_slug_component(name: str) -> str:
    return _sanitize_native_slug(name)[0]


def _slugs_for_session(runtime: runtime_mod.Runtime, session_id: str) -> tuple[str, ...]:
    """All projects-dir slugs containing a native UUID, names only."""

    home = Path(runtime.environ.get("HOME") or Path.home())
    projects = paths.native_projects(home, runtime.environ)
    found: set[str] = set()
    try:
        for project_dir in projects.iterdir():
            if not project_dir.is_dir():
                continue
            if (project_dir / f"{session_id}.jsonl").is_file():
                found.add(project_dir.name)
    except OSError:
        # An unreadable projects root is indistinguishable from "no
        # transcripts" at metadata level — treated as missing (the
        # conservative guidance either way).
        return ()
    return tuple(sorted(found))


def _decode_project_slug_candidates(slug: str) -> tuple[Path, ...]:
    """Return every existing directory represented by a non-injective slug."""

    if not slug.startswith("-"):
        return ()
    candidates: list[tuple[Path, str]] = [(Path("/"), slug[1:])]
    finals: set[Path] = set()
    for _depth in range(24):
        following: list[tuple[Path, str]] = []
        for base, remaining in candidates:
            if remaining == "":
                finals.add(base)
                continue
            try:
                children = [child for child in base.iterdir() if child.is_dir()]
            except OSError:
                continue
            for child in children:
                encoded = _native_slug_component(child.name)
                if remaining == encoded:
                    following.append((child, ""))
                elif remaining.startswith(encoded + "-"):
                    following.append((child, remaining[len(encoded) + 1 :]))
        if not following:
            break
        candidates = list(dict.fromkeys(following))[:64]
    finals.update(path for path, remaining in candidates if remaining == "")
    return tuple(sorted(finals, key=str))


def _decode_project_slug(slug: str) -> Path | None:
    """Compatibility helper: decode only when exactly one directory matches."""

    candidates = _decode_project_slug_candidates(slug)
    return candidates[0] if len(candidates) == 1 else None


def _original_cwd_for_adopt(
    runtime: runtime_mod.Runtime, session_id: str, explicit_cwd: str | None = None
) -> str:
    """Resolve exactly one original CWD or fail closed with repair guidance."""

    slugs = _slugs_for_session(runtime, session_id)
    if not slugs:
        raise cli_errors.CLIError(
            f"cannot locate native session {session_id} in local Claude project "
            "metadata; open it natively on this machine before adoption"
        )
    if explicit_cwd is not None:
        chosen = Path(explicit_cwd).resolve()
        if not chosen.is_dir():
            raise cli_errors.CLIError(
                f"adoption CWD {str(chosen)!r} is not an accessible directory"
            )
        encoded = _native_project_slug(chosen)
        if encoded not in slugs:
            raise cli_errors.CLIError(
                f"adoption CWD {str(chosen)!r} maps to project slug {encoded!r}, "
                f"which does not contain native session {session_id}; observed "
                f"slugs: {slugs!r}"
            )
        return str(chosen)
    current = Path(runtime.cwd)
    if _native_project_slug(current) in slugs:
        return str(current)
    candidates = {
        path for slug in slugs for path in _decode_project_slug_candidates(slug)
    }
    if current in candidates:
        return str(current)
    if len(candidates) == 1:
        return str(next(iter(candidates)))
    if not candidates:
        raise cli_errors.CLIError(
            f"cannot decode the project directory for session slugs {slugs!r}; "
            "cd into the session's original project and adopt again"
        )
    choices = ", ".join(str(path) for path in sorted(candidates, key=str))
    raise cli_errors.CLIError(
        f"session {session_id} has ambiguous project directories: {choices}; "
        "cd into the exact original project and adopt again"
    )


def resolve_session(runtime: runtime_mod.Runtime, value: str) -> dict[str, Any]:
    """The record of the session a person named (``-r``, ``direct -r``,
    ``sessions show|forget|stop|mark-ended|relink-runtime|resolve-fork``,
    ``doctor --repair``, ``explain``, ``usage --session``): an exact id (the
    managed id first, then a runtime id or alias through the store's
    ownership checks), an unambiguous id prefix of at least
    ``sessions.HUMAN_PREFIX_MIN`` characters, or an exact session name (its
    profile, ``cm:<profile>@<project>``, ``cm:direct:<key>@<project>`` and the
    earlier display names). Several matches refuse with a bounded list and a
    copyable retry; nothing is chosen for the person. Protocol positions
    (``/cm --session``, hooks, a new runtime id) never come here."""

    store = runtime.session_store
    try:
        return store.resolve_human(value, names=_record_resume_names)
    except sessions.AmbiguousSessionError as exc:
        lines = [f"{value!r} matches {len(exc.candidates)} managed sessions; nothing was chosen:"]
        for mid in exc.candidates[:sessions.HUMAN_CANDIDATES_SHOWN]:
            try:
                record = store.load(mid)
            except sessions.SessionError:
                lines.append(f"  {mid}")
                continue
            lines.append(
                f"  {mid}  {termtext.visible_text(_record_target_label(record))}  "
                f"{_record_mode_label(record)}  {record.get('created_at', '?')}  "
                f"{termtext.visible_text(str(record.get('cwd', '?')))}"
            )
        more = len(exc.candidates) - sessions.HUMAN_CANDIDATES_SHOWN
        if more > 0:
            lines.append(f"  … and {more} more")
        raise cli_errors.CLIError("\n".join(lines), remedy=exc.remedy) from exc
    except sessions.StateMarkerError:
        raise
    except sessions.SessionError as exc:
        raise cli_errors.CLIError(str(exc), remedy=getattr(exc, "remedy", None)
                                  or "list the sessions: claude-multi sessions list") from exc


def report_session(runtime, identifier: str | None = None):
    """The session a report names (:func:`resolve_session`), else the
    current session (``CLAUDE_MULTI_MANAGED_ID``), else this directory's
    last; never a picker or a runtime guess."""
    if identifier is None:
        marker = runtime.environ.get("CLAUDE_MULTI_MANAGED_ID")
        if isinstance(marker, str) and sessions.UUID4.fullmatch(marker):
            identifier = marker
        else:
            identifier = runtime.session_store.last(runtime.cwd)
    if identifier is None:
        raise cli_errors.CLIError("no managed session selected — pass a session id",
                                  remedy="list the sessions: claude-multi sessions list")
    return resolve_session(runtime, identifier)


def explanation(runtime, identifier=None, agent=None):
    """Collect one report's record/scope/log, served, journal and quota sources."""
    from claude_multi import lineup_log, profile, routing, scope, strict_json, transition, observations
    import claude_multi.cli.gateway_facts as gateway_facts

    record = report_session(runtime, identifier)
    if record.get("version") != sessions.RECORD_VERSION:
        raise cli_errors.CLIError("explain: resume or migrate the legacy record first")
    mid = sessions.managed_id(record)
    bindings = lineup_log.read(runtime.session_store.root, mid)
    role = agent or "lead"
    agents = record["applied"]["agents"]
    if role != "lead" and role not in agents:
        roles = {e.role for e in bindings.events if e.agent_id == role and e.role in agents}
        if len(roles) != 1:
            raise cli_errors.CLIError("explain: agent must be a bound cm-* role or an exact observed agent id")
        binding_role = next(iter(roles))
    else:
        binding_role = role
    binding = record["applied"]["lead"] if binding_role == "lead" else agents[binding_role]
    live = scope.scope_dir(runtime.session_store.root, mid)
    available, lead_selectors, integrity = None, (), "unknown"
    try:
        settings = strict_json.loads(lineup_log.read_regular(live / "settings.json", max_bytes=1 << 20))
        lead_set = strict_json.loads(lineup_log.read_regular(live / "lead-set.json", max_bytes=1 << 20))
        values = settings.get("availableModels") if isinstance(settings, dict) else None
        if isinstance(values, list) and all(observations.identifier(v) for v in values):
            available = tuple(values)
        lead_selectors = tuple(r["selector"] for r in lead_set["rows"] if observations.identifier(r.get("selector")))
        parts = runtime.converge_parts()
        expected = transition.expected_plan(record, docs=parts.docs, prompt_bodies=parts.prompt_bodies,
                   state_root=runtime.session_store.root, hook_command=parts.hook_command,
                   token_helper_command=parts.token_helper_command, live=live, environ=parts.environ,
                   managed_root=parts.managed_root, feedback_drafts=parts.feedback_drafts)
        if expected.plan is not None:
            integrity = "drifted" if scope.live_drift(live, expected.plan) else "matched"
    except (OSError, ValueError, KeyError, TypeError, errors.ClaudeMultiError):
        pass
    lcat = runtime.lineup_catalog()
    key = binding.get("key")
    current = None
    if key in lcat.lines:
        if binding_role != "lead" and key in lcat.operator:
            current = profile.agent_eligibility(lcat.lines[key], key=key, slot=binding_role,
                      effort=binding["effort"], facts=lcat.agent_gate.facts.get(key) if lcat.agent_gate else None,
                      agent_efforts=lcat.agent_efforts).eligible
        else:
            current = True
    aliases, _, _ = gateway_facts._doctor_continuity(runtime)
    route = routing.current_route(binding.get("selector"), runtime.ordinary_docs, aliases)
    confirmed = False
    try:
        snap = gateway_facts._gateway_snapshot(runtime, runtime.gateway_token())
        selector = (binding.get("selector") or "").removesuffix("[1m]")
        if selector:
            route = snap.routes.get(selector, route)
            confirmed = bool(snap.config_drift is False and snap.sentinel and snap.served is not None
                             and snap.sentinel in snap.served and selector in snap.served)
    except errors.ClaudeMultiError:
        pass
    runtime.pool_status()  # existing 60 s failure cache; never a provider call
    return routing.report(record=record, role=role, binding=binding, available=available,
                          lead_selectors=lead_selectors, integrity=integrity, bindings=bindings,
                          events=gateway_facts.report_events(runtime), route=route, route_confirmed=confirmed,
                          current_eligible=current, now=gateway_facts._doctor_now(),
                          explained_role=role if role == binding_role else f"{role} → {binding_role}")
