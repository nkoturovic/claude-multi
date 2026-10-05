"""Launcher-owned session state for claude-multi.

Stable-managed-ID records under XDG state, per-CWD last-session pointers with
advisory locking, and adoption (`link`) records. All persistence uses the
atomic, symlink-safe, mode-0600 primitives of ``state``. No private Claude
files are ever read.

Record version 4 (the ``applied`` lineup, ``profile``/``follow``,
``lineup_generation``) lives next to the legacy versions 1-3, which keep
loading and keep their own version on every lifecycle write; the state
marker ``<state>/state-version`` (missing = 3, this launcher writes 4); the
global migration lock ``<state>/migration.lock`` (exclusive for
migrate/restore, shared for every launcher write, probed by hooks); unified
per-cwd pointers with a legacy ``.ordinary`` read.

Import rule: this module imports only stdlib + ``state``, ``strict_json`` and
``validate`` at import time (``hooks`` pins its transitive module set);
``catalog``, ``profile``, ``hooks`` and ``lineup_log`` are imported inside the
functions that need them.
"""

from __future__ import annotations

from . import assets, paths

import contextlib
import copy
import json
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, NamedTuple, Sequence

from . import errors, state, strict_json, validate as schema_validate
from .platform.observation import BackgroundLiveness, ProcessScan


class SessionError(errors.ClaudeMultiError, RuntimeError):
    """Raised on session state failures, including corrupt/stale records."""


class MigrationBusyError(SessionError):
    """The global migration lock is held exclusively (migrate / restore-2x)."""


class LegacyRecordError(SessionError):
    """A v4-only operation met a legacy (version 1-3) record."""


class AmbiguousSessionError(SessionError):
    """A name or prefix a person gave matches several sessions; nothing is
    chosen. ``candidates`` are the matching managed ids (bounded in the
    message)."""

    def __init__(self, value: str, candidates: Sequence[str]) -> None:
        self.value = value
        self.candidates = tuple(candidates)
        shown = list(self.candidates[:HUMAN_CANDIDATES_SHOWN])
        more = len(self.candidates) - len(shown)
        listing = ", ".join(shown) + (f" and {more} more" if more > 0 else "")
        super().__init__(f"{value!r} matches {len(self.candidates)} sessions ({listing}); nothing was chosen",
                         remedy="give the full session id, for example: " + self.candidates[0])


class PendingForkError(SessionError):
    """``resolve_runtime`` met an id that is some record's pending fork."""

    def __init__(self, record: dict[str, Any]):
        self.record = record
        super().__init__(pending_fork_message(record))


UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_COLLISION_RETRIES = 8


class _JSONPairs(list):
    """Object pairs preserved for conservative invalid-record inspection."""


def state_root(environ: dict[str, str] | None = None) -> Path:
    return paths.state_root(environ)


def config_root(environ: dict[str, str] | None = None) -> Path:
    return paths.config_root(environ)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# A person may name a session by a UUID prefix this long or longer.
HUMAN_PREFIX_MIN = 8
HUMAN_CANDIDATES_SHOWN = 8
UUID_PREFIX = re.compile(r"^[0-9a-f][0-9a-f-]{0,35}$")

RECORD_VERSION = 4
V3_RECORD_VERSION = 3
# Every legacy record version. ``_PRE_V3_VERSIONS`` are the legacy files
# that load through ``_normalize_legacy_record`` into a v3 view.
LEGACY_RECORD_VERSIONS = frozenset({1, 2, 3})
_PRE_V3_VERSIONS = frozenset({1, 2})
STATE_MARKER = "state-version"
SUPPORTED_STATE_VERSION = 4
UNMARKED_STATE_VERSION = 3  # a missing marker = legacy state
MIGRATION_LOCK_TARGET = "migration"  # == hooks.MIGRATION_LOCK_TARGET
MIGRATION_BUSY_TEXT = "migration or restore in progress; retry in a moment"
BACKUP_SUFFIX = ".v3.json"  # sessions/<id>.v3.json
POINTER_BACKUP_SUFFIX = ".v3"  # last-session-by-cwd/<name>.v3
NOTICE_DIR = "notice"  # == hooks.NOTICE_DIR
SEEN_SUFFIX = ".seen"  # == hooks.SEEN_SUFFIX
# Launches that can only reach the gateway through the scope's apiKeyHelper
# (no credential in the process environment): every launch whose catalog is
# at least HELPER_ONLY_CATALOG, and, for records of older launches, every
# launcher at least HELPER_ONLY_LAUNCHER_VERSION.
HELPER_ONLY_CATALOG = 37
HELPER_ONLY_LAUNCHER_VERSION = (2, 26)
SESSION_TYPE_MANAGED = "managed-composition"
SESSION_TYPE_ORDINARY = "ordinary-gateway"
IDENTITY_AUTHORITATIVE = "authoritative"
IDENTITY_UNVERIFIED = "unverified"
IDENTITY_REPAIR_NEEDED = "repair-needed"
IDENTITY_PENDING_FORK = "pending-fork"
_MAX_RUNTIME_ALIASES = 16
# ``last_end_reason`` of a synthetic end (``sessions mark-ended``,
# ``restore-2x --assume-dead``).
MARKED_ENDED_REASON = "marked-ended"


def mark_ended_remedy(ids: Iterable[str] = ()) -> str:
    """The remedy a refusal on live-looking session records names.

    Record liveness is "no recorded end", so a launch that exited before
    SessionStart (or a session that died without SessionEnd) still counts
    as live. ``sessions mark-ended`` refuses a session that is still
    running (process table and daemon checks), so naming it is safe. One id
    is named in full (the command takes the full id); several are listed.
    """

    ordered = sorted(set(ids))
    target = ordered[0] if len(ordered) == 1 else "<id>"
    text = ("a record without a recorded end counts as live, including a launch that exited "
            "before SessionStart; if that session is not running, record its end: "
            f"claude-multi sessions mark-ended {target}")
    if len(ordered) > 1:
        text += f" (ids: {', '.join(ordered)})"
    return text
_LIFECYCLE_FIELDS = (
    "runtime_session_id",
    "runtime_aliases",
    "identity_state",
    "last_event_source",
    "last_seen_at",
    "pending_forks",
    "observed_cwd",
    "observed_model",
    "last_end_reason",
)

# v1 records load with these v2 defaults before their in-memory v3 migration.
_V2_DEFAULTS = {"mode": "legacy", "scope_generation": 0, "workflows": "native"}


class StateMarkerError(SessionError):
    """The state root is not safe for this launcher's mutation protocol."""

    # Older code over newer state: state only ever migrates forward, so the
    # fix is the newer release, never a downgrade of the state.
    NEWER_REMEDY = ("use the newer claude-multi that wrote it (an installed release updates with "
                    "claude-multi update); state is only ever migrated forward")

    def __init__(self, version: int | None = None):
        self.version = version
        label = str(version) if version is not None else "unknown"
        message = (f"state belongs to a newer claude-multi (state version {label}); "
                   f"this launcher understands state version {SUPPORTED_STATE_VERSION} or older")
        # The fix rides in the message (not ``remedy``): every surface that
        # shows this error (hooks, screens, doctor) prints only the message.
        super().__init__(f"{message}. Fix: {self.NEWER_REMEDY}" if version is not None else message)


def check_state_marker(state_root: Path) -> int:
    """Read the migration marker without writes; missing means state v3.

    Only ASCII decimal digits and one optional trailing newline are valid.
    Unsafe/unreadable markers refuse just like newer versions. A missing
    marker is legacy state (``UNMARKED_STATE_VERSION``); ``4`` is written by
    ``ensure_state_v4``/``write_state_marker`` before the first v4 record
    Read-only callers may catch StateMarkerError.
    """

    marker = Path(state_root) / STATE_MARKER
    try:
        marker.lstat()
    except FileNotFoundError:
        return UNMARKED_STATE_VERSION
    except OSError as exc:
        raise StateMarkerError() from exc
    try:
        raw = state.read_private(marker)
        if not re.fullmatch(rb"[0-9]+\n?", raw):
            raise StateMarkerError()
        version = int(raw)
    except (OSError, ValueError) as exc:
        raise StateMarkerError() from exc
    if version > SUPPORTED_STATE_VERSION:
        raise StateMarkerError(version)
    return version


def write_state_marker(root: Path | str) -> None:
    """Write ``4\\n`` (0600, atomic). Callers hold the migration lock exclusively."""

    state.atomic_write(
        Path(root) / STATE_MARKER, f"{SUPPORTED_STATE_VERSION}\n".encode("ascii")
    )


def remove_state_marker(root: Path | str) -> bool:
    """Remove the marker (``restore-2x`` does this last); False when absent."""

    return state.remove_private(Path(root) / STATE_MARKER)


def migration_lock(root: Path | str, *, shared: bool = False) -> state.FileLock:
    """The global migration lock ``<state>/migration.lock``; the caller acquires."""

    return state.FileLock(Path(root) / MIGRATION_LOCK_TARGET, shared=shared)


@contextlib.contextmanager
def launcher_write_guard(root: Path | str) -> Iterator[state.FileLock]:
    """Hold the migration lock shared (non-blocking) for one launcher write.

    Every launcher writer enters it BEFORE its lifecycle lock, so
    ``migrate``/``restore-2x`` (exclusive) never interleave with a launch,
    live apply, link or title write. A held exclusive lock raises
    :class:`MigrationBusyError`. ``perform_launch`` keeps the lock through
    ``execve`` (the fd is ``O_CLOEXEC``, so exec drops it). Never nested:
    ``FileLock`` is not re-entrant.
    """

    lock = migration_lock(root, shared=True)
    if not lock.acquire(blocking=False):
        raise MigrationBusyError(MIGRATION_BUSY_TEXT)
    try:
        yield lock
    finally:
        lock.release()


def barrier_dir(home: Path | str) -> Path:
    """The gateway-scoped served-change barrier directory (HOME-relative,
    like the api-key leaf; never ``XDG_CONFIG_HOME``)."""

    return paths.gateway_config_dir({"HOME": str(home)})


@contextlib.contextmanager
def served_change_phase(
    root: Path | str,
    home: Path | str,
    *,
    timeout: float | None = state.SERVED_BARRIER_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[state.BarrierToken]:
    """One served-change commit phase.

    The migration lock SHARED (non-blocking, :class:`MigrationBusyError`),
    then the gateway barrier with a bounded wait
    (:class:`state.BarrierBusyError`, the neutral text). The yielded token
    pairs both; inner services consume it instead of taking either lock.
    Prompts, cards, consent and provider calls never run inside a phase.
    """

    with launcher_write_guard(root) as guard:
        token = state.acquire_served_barrier(
            barrier_dir(home), timeout=timeout, clock=clock, sleep=sleep,
            guard=guard, guard_root=Path(root),
        )
        try:
            yield token
        finally:
            token.release()


@contextlib.contextmanager
def phase_or_guard(root: Path | str, barrier: state.BarrierToken | None) -> Iterator[Any]:
    """An inner service's migration hold: with a held
    barrier token the outer phase already holds the migration guard, so the
    token is asserted and nothing is acquired; without one the service takes
    its own shared guard (a standalone call)."""

    if barrier is not None:
        state.require_barrier(barrier, root=root)
        yield barrier.guard
        return
    with launcher_write_guard(root) as guard:
        yield guard


def acquire_exclusive_bounded(
    root: Path | str,
    *,
    timeout: float = 3.0,
    interval: float = 0.1,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> state.FileLock:
    """Take the migration lock exclusively with a bounded wait.

    Non-blocking attempts every ``interval`` until ``timeout``; still held
    elsewhere -> :class:`MigrationBusyError`. Used by ``ensure_state_v4`` and
    launch-time migration only: the launch/link path never hangs for a whole
    ``migrate`` record loop. The caller releases the returned lock.
    """

    lock = migration_lock(root)
    deadline = clock() + timeout
    while True:
        if lock.acquire(blocking=False):
            return lock
        if clock() >= deadline:
            raise MigrationBusyError(MIGRATION_BUSY_TEXT)
        sleep(interval)


def ensure_state_v4(
    root: Path | str,
    *,
    timeout: float = 3.0,
    interval: float = 0.1,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Make the state root a v4 root before its first v4 record.

    Fast path: the marker already reads 4 -> False, no lock. Otherwise the
    migration lock is taken exclusively (bounded, ``MigrationBusyError``),
    the marker re-checked and ``4\\n`` written -> True. Callers (fresh launch,
    ``sessions link``) run this BEFORE ``launcher_write_guard``; ``migrate``
    and launch-time migration write the marker themselves under their own
    exclusive hold (never nest). Precondition: this Runtime
    refreshed the legacy hook shim (a legacy hook then still finds a launcher
    that reads the new marker).
    """

    root = Path(root)
    if check_state_marker(root) == SUPPORTED_STATE_VERSION:
        return False
    lock = acquire_exclusive_bounded(
        root, timeout=timeout, interval=interval, clock=clock, sleep=sleep
    )
    try:
        if check_state_marker(root) == SUPPORTED_STATE_VERSION:
            return False
        write_state_marker(root)
        return True
    finally:
        lock.release()


def migration_window_wait(
    root: Path | str,
    *,
    timeout: float = 3.0,
    interval: float = 0.1,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """The hooks' bounded wait for a running migration.

    Polls the non-creating probe (``hooks.migration_lock_held``: LOCK_SH |
    LOCK_NB on an O_RDONLY fd) until the lock is free -> True, or until
    ``timeout`` -> False (the caller skips its record write). Shared holders
    (launcher writers) never make it wait.
    """

    from . import hooks  # local: hooks imports sessions

    deadline = clock() + timeout
    while True:
        if not hooks.migration_lock_held(root):
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


_DEFAULT_SCHEMA: dict[str, Any] | None = None


def default_schema() -> dict[str, Any]:
    """The packaged ``schemas/session.schema.json`` (read once per process)."""

    global _DEFAULT_SCHEMA
    if _DEFAULT_SCHEMA is None:
        _DEFAULT_SCHEMA = strict_json.load(
            assets.root() / "schemas" / "session.schema.json"
        )
    return copy.deepcopy(_DEFAULT_SCHEMA)


def release_tuple(value: str) -> tuple[int, int, int]:
    """The numeric core of a release or development launcher version."""

    major, minor, patch = value.removesuffix("-dev").split(".")
    return int(major), int(minor), int(patch)


def helper_only_launch(record: dict[str, Any]) -> bool:
    """True when the record's launch passed the gateway credential only
    through the scope's apiKeyHelper, never in the process environment.

    A capability boundary on the launch, not a release order: a launch whose
    catalog is at least :data:`HELPER_ONLY_CATALOG` is helper-only whatever
    its launcher version says; an older record keeps its launcher-version
    comparison (:data:`HELPER_ONLY_LAUNCHER_VERSION`). A migrated record keeps
    the catalog and launcher of the launch that wrote it, so a conversion
    never makes an older process helper-only.
    """

    catalog_version = record.get("catalog_version")
    if (isinstance(catalog_version, int) and not isinstance(catalog_version, bool)
            and catalog_version >= HELPER_ONLY_CATALOG):
        return True
    return release_tuple(record["launcher_version"])[:2] >= HELPER_ONLY_LAUNCHER_VERSION


def may_hold_env_token(record: dict[str, Any]) -> bool:
    """A session that may still hold a gateway credential in its environment:
    a launch that was not helper-only (:func:`helper_only_launch`) and has no
    SessionEnd.

    ``None`` (launched, no hook seen yet) counts as live. Liveness is
    heuristic: SessionEnd may never fire, so this errs toward "live".
    """

    return not helper_only_launch(record) and record.get("last_event_source") != "end"


# The daemon-location and bounded process-table reads live in the platform
# backends (``platform.linux_process``: ``/proc``; ``platform.darwin_process``:
# bounded ``ps`` reads), imported lazily so hooks never load them; the
# session-id shape and the liveness policy stay here.


def _platform() -> str:
    """The process backend's platform (a seam: tests select macOS here)."""

    import sys

    return sys.platform


def background_liveness(root: Path | None = None) -> BackgroundLiveness:
    """Metadata-only liveness with explicit uncertainty for destructive callers.

    Every destructive guard (forget, stop, mark-ended, restore-2x, the
    rotation dead-confirm, ``doctor --repair-all``) reads this form and
    refuses (or treats the session as live) when ``known`` is False.
    """
    from .platform import linux_process

    # The pinned client roots its daemon at /tmp/cc-daemon-<uid> on every
    # POSIX platform, so one reader serves Linux and macOS.
    return linux_process.background_liveness(root)


def live_background_prefixes(root: Path | None = None) -> frozenset[str]:
    """Resume/pickers/displays only: permissive (empty) when daemon metadata is unknown."""
    return background_liveness(root).prefixes


def ancestor_pids(proc_root: Path | str = "/proc", pid: int | None = None) -> frozenset[int]:
    """``pid`` (default: this process) and its ancestors, from ``/proc/<pid>/stat``.

    The processes ``restore-2x --assume-dead <own id>``
    may disregard when it checks the caller's own record against ``/proc``.
    Walks field 4 (ppid) of each ``stat`` line, parsed after the last ``)``
    so a ``comm`` with spaces or parentheses cannot shift it. Bounded to 64
    steps; any error, a pid <= 0 or a pid seen before ends the walk.
    Read-only; ``proc_root`` is the test seam.
    """
    if _platform() == "darwin":
        from .platform import darwin_process

        return darwin_process.ancestor_pids(pid)
    from .platform import linux_process

    return linux_process.ancestor_pids(proc_root, pid)


def proc_session_scan(
    proc_root: Path | str = "/proc", *, exclude_pids: Iterable[int] = ()
) -> ProcessScan:
    """The process-table argv scan with explicit uncertainty (``known`` False:
    unlistable). On macOS one bounded ``ps`` read replaces ``/proc``."""
    if _platform() == "darwin":
        from .platform import darwin_process

        return darwin_process.session_scan(exclude_pids=exclude_pids, accept=UUID4.fullmatch)
    from .platform import linux_process

    return linux_process.session_scan(proc_root, exclude_pids=exclude_pids, accept=UUID4.fullmatch)


def proc_session_ids(
    proc_root: Path | str = "/proc", *, exclude_pids: Iterable[int] = ()
) -> frozenset[str]:
    """Runtime ids named after ``--session-id``/``--resume`` in ``/proc/*/cmdline``.

    The second, metadata-only liveness source: a process
    whose argv carries a record's runtime id or alias is that session,
    running. Read-only and bounded (at most 1 MiB per cmdline); any error
    degrades to "nothing found" for that table or process (by design: an
    unreadable table or argv is reported only by :func:`proc_session_scan`,
    which every destructive guard uses; this form is for displays and
    pickers). ``proc_root`` is the test
    seam; ``exclude_pids`` skips those processes (the caller's
    ancestors, :func:`ancestor_pids`; empty by default).
    """
    if _platform() == "darwin":
        from .platform import darwin_process

        return darwin_process.session_scan(exclude_pids=exclude_pids, accept=UUID4.fullmatch).ids
    from .platform import linux_process

    return frozenset(rid for rid, _path in linux_process.session_argv_paths(
        proc_root, exclude_pids=exclude_pids, accept=UUID4.fullmatch))


def _proc_session_paths(
    proc_root: Path | str, *, exclude_pids: Iterable[int] = ()
) -> Iterator[tuple[str, Path]]:
    """Shared bounded argv scan; executable metadata is read only on request."""
    from .platform import linux_process

    return linux_process.session_argv_paths(proc_root, exclude_pids=exclude_pids, accept=UUID4.fullmatch)


def proc_client_mismatches(
    session_ids: Iterable[str], pinned: Path, *, retained: Path | None = None,
    proc_root: Path | str = "/proc",
) -> dict[str, str]:
    """Known non-pinned clients by runtime id, never hash or exec.

    Missing/unreadable pins or exe links (including deleted executables)
    mean unknown, not mismatch, as do non-version executable basenames.
    Accept either existing pin reference, including a retained copy after
    reinstalling the original. Hard links compare equal by device and inode.
    """

    from . import retention

    wanted = frozenset(session_ids)
    if not wanted:
        return {}
    expected: set[tuple[int, int]] = set()
    for reference in (pinned, retained):
        if reference is None:
            continue
        try:
            info = reference.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return {}
        expected.add((info.st_dev, info.st_ino))
    if not expected:
        return {}
    found: dict[str, str] = {}
    for rid, process in _proc_session_paths(proc_root):
        if rid not in wanted:
            continue
        try:
            target = Path(os.readlink(process / "exe"))
            # A native install names the file after its version; an owned
            # copy is <version>/claude.
            client = target.parent.name if target.name in ("claude", "claude.exe") else target.name
            if not retention._VERSION.fullmatch(client):
                continue  # deleted exes and pre-exec launchers are unknown
            actual = (process / "exe").stat()
        except OSError:
            continue
        if (actual.st_dev, actual.st_ino) not in expected:
            found[rid] = client
    return found


def record_session_ids(record: dict[str, Any]) -> frozenset[str]:
    """The managed id, runtime id and every runtime alias of a record view."""

    ids = {record.get("managed_id") or record.get("session_id")}
    ids.add(record.get("runtime_session_id"))
    ids.update(item.get("session_id") for item in record.get("runtime_aliases", []))
    return frozenset(item for item in ids if isinstance(item, str))


def record_is_proc_live(record: dict[str, Any], proc_ids: frozenset[str]) -> bool:
    """A process argv names this record's runtime id, alias or managed id."""

    return bool(proc_ids & record_session_ids(record))


def managed_id(record: dict[str, Any]) -> str:
    """Stable claude-multi identity for a normalized or legacy record."""

    value = record.get("managed_id", record.get("session_id"))
    if not isinstance(value, str) or not UUID4.fullmatch(value):
        raise SessionError(f"record managed_id {value!r} is not a UUIDv4")
    return value


def runtime_session_id(record: dict[str, Any]) -> str:
    """Current native Claude runtime UUID for a normalized record."""

    value = record.get("runtime_session_id", record.get("session_id"))
    if not isinstance(value, str) or not UUID4.fullmatch(value):
        raise SessionError(f"record runtime_session_id {value!r} is not a UUIDv4")
    return value


def new_mutation_token() -> str:
    """Unique ownership token for one record/scope mutation attempt."""

    return str(uuid.uuid4())


def carry_lifecycle_state(
    target: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Overlay the latest hook-owned fields onto a prepared record mutation."""

    merged = copy.deepcopy(target)
    for key in _LIFECYCLE_FIELDS:
        merged.pop(key, None)
        if key in current:
            merged[key] = copy.deepcopy(current[key])
    return merged


def _derived_identity_state(record: dict[str, Any]) -> str:
    """Derive state from unresolved lifecycle evidence after a start event."""

    if "observed_cwd" in record or "observed_model" in record:
        return IDENTITY_REPAIR_NEEDED
    if record.get("pending_forks"):
        return IDENTITY_PENDING_FORK
    return IDENTITY_AUTHORITATIVE


def drop_resolved_pending_forks(record: dict[str, Any]) -> dict[str, Any] | None:
    """Copy of ``record`` with authority-holding pending forks removed.

    A pending fork that IS the current runtime authority is already resolved
    by reality — the live runtime is that fork, so there is nothing left to
    adopt or discard. Returns ``None`` when nothing changed.
    """

    pending = record.get("pending_forks", [])
    if not pending:
        return None
    runtime_id = record.get("runtime_session_id")
    kept = [dict(item) for item in pending if item.get("session_id") != runtime_id]
    if len(kept) == len(pending):
        return None
    updated = {**record, "pending_forks": kept}
    if record.get("identity_state") != IDENTITY_REPAIR_NEEDED:
        updated["identity_state"] = _derived_identity_state(updated)
    return updated


def pending_fork_message(record: dict[str, Any]) -> str:
    """Actionable fork-blocked language: names the fork(s) and the remedies.

    Every resume/transition guard and the card uses this so the operator
    never sees an unnamed "adopt the fork UUID" dead end.
    """

    stable_id = managed_id(record)
    pending = record.get("pending_forks", [])
    fork_ids = [item.get("session_id", "?") for item in pending]
    fork_list = ", ".join(fork_ids)
    if record.get("version") == RECORD_VERSION:
        # One adopt-command source for v4 records. The legacy branches below
        # keep their own text.
        remedy = (
            f"adopt it with `{fork_adopt_command(record, fork_ids[0])}`, or "
            f"discard the marker with `claude-multi sessions resolve-fork "
            f"{stable_id} {fork_ids[0]}`"
        )
        if len(fork_ids) > 1:
            remedy = (
                "adopt or discard each with `claude-multi sessions link "
                "<fork-uuid> --profile NAME|--direct MODEL` / `claude-multi "
                f"sessions resolve-fork {stable_id} <fork-uuid>`"
            )
        return (
            f"session {stable_id} has an unresolved native fork (runtime "
            f"{fork_list}); {remedy} — the fork transcript is kept either way"
        )
    if record["session_type"] == SESSION_TYPE_ORDINARY:
        adopt = f"claude-multi sessions link {fork_ids[0]} --model MODEL"
    else:
        adopt = (
            f"claude-multi sessions link {fork_ids[0]} --composition "
            f"{record.get('composition_name', 'NAME')}"
        )
    remedy = (
        f"adopt it with `{adopt}`, or discard the marker with `claude-multi "
        f"sessions resolve-fork {stable_id} {fork_ids[0]}`"
    )
    if len(fork_ids) > 1:
        link_flags = (
            "--model MODEL"
            if record["session_type"] == SESSION_TYPE_ORDINARY
            else "--composition NAME"
        )
        remedy = (
            f"adopt or discard each with `claude-multi sessions link <fork-uuid> "
            f"{link_flags}` / `claude-multi sessions resolve-fork "
            f"{stable_id} <fork-uuid>`"
        )
    return (
        f"session {stable_id} has an unresolved native fork (runtime "
        f"{fork_list}); {remedy} — the fork transcript is kept either way"
    )


def fork_resolution_problem(
    record: Mapping[str, Any], managed_session_id: str, fork_runtime_id: str
) -> str | None:
    """Why ``resolve-fork`` cannot discard this fork marker, or None.

    One rule for the command (checked before it asks) and the store
    (checked again under the lifecycle lock): the id must be a UUIDv4 the
    record lists as a pending fork that does not hold its resume authority.
    """

    if not UUID4.fullmatch(fork_runtime_id):
        return f"fork runtime id {fork_runtime_id!r} is not a UUIDv4"
    pending = record.get("pending_forks") or []
    if not any(item.get("session_id") == fork_runtime_id for item in pending):
        return f"session {managed_session_id} has no pending fork {fork_runtime_id}"
    if record.get("runtime_session_id") == fork_runtime_id:
        return (
            f"fork {fork_runtime_id} holds the resume authority of "
            f"session {managed_session_id}; it is already resolved "
            "(run `claude-multi doctor --repair-all` to clear the marker)"
        )
    return None


def relink_message(record: dict[str, Any]) -> str:
    """Actionable repair-needed language: the exact relink command.

    Every resume/transition guard, the card, the sessions list, and doctor
    use this so the operator never sees a bare command name that cannot
    run as printed.
    """

    stable_id = managed_id(record)
    runtime_id = runtime_session_id(record)
    base = f"claude-multi sessions relink-runtime {stable_id} {runtime_id}"
    observed_cwd = record.get("observed_cwd")
    if not observed_cwd:
        observed_model = record.get("observed_model")
        if observed_model:
            # One model-only text (shared with the hook texts) for
            # every record version; the recorded lead's selector, else key.
            try:
                key, selector = lead_identity(record)
                recorded = selector or key
            except (KeyError, TypeError):
                recorded = "the recorded lead"
            return (
                f"session {stable_id} identity is repair-needed (observed "
                f"model {observed_model} differs from the recorded "
                f"{recorded}); resume through the launcher to "
                f"reconcile the recorded lead (`claude-multi -r {stable_id}`), "
                f"or relink only if the runtime UUID itself changed: `{base}`"
            )
        return (
            f"session {stable_id} identity is repair-needed (runtime/model "
            f"evidence conflicts with the record); repair it with `{base}`"
        )
    recorded_cwd = record.get("cwd", "?")
    return (
        f"session {stable_id} identity is repair-needed (a resume was "
        f"observed from {observed_cwd}, conflicting with the recorded "
        f"project dir {recorded_cwd}); repair it with `{base} --cwd "
        f"{shlex.quote(recorded_cwd)}` to keep the recorded dir (the "
        f"common case), or `{base} --cwd {shlex.quote(observed_cwd)}` "
        f"only if the session was intentionally re-homed there"
    )


def fork_adopt_command(record: dict[str, Any], fork_id: str) -> str:
    """The one adopt command for a fork of ``record``.

    Shared by ``pending_fork_message``, the hook fork text and the TUI modal.
    """

    base = f"claude-multi sessions link {fork_id}"
    if record.get("version") == RECORD_VERSION:
        if record.get("profile"):
            return f"{base} --profile {record['profile']}"
        if not record["applied"]["agents"]:
            return f"{base} --direct {record['applied']['lead']['key']}"
        return f"{base} --profile NAME"
    view = _normalize_legacy_record(record)
    if view.get("session_type") == SESSION_TYPE_ORDINARY:
        return f"{base} --direct {view.get('ordinary_model', 'MODEL')}"
    return f"{base} --profile NAME"


# ------------------------------------------------------------ v4 readers


def lead_identity(record: dict[str, Any]) -> tuple[str, str | None]:
    """``(lead key, lead selector)`` of any record version.

    v4 ``applied.lead``; v3 managed ``snapshot.lead`` (model, client
    selector); v3 ordinary ``(ordinary_model, None)``; v1/v2 through their v3
    view. Used by the hook region and by doctor's catalog-free legacy line.
    """

    if record.get("version") == RECORD_VERSION:
        lead = record["applied"]["lead"]
        return lead["key"], lead["selector"]
    view = _normalize_legacy_record(record)
    if view.get("session_type") == SESSION_TYPE_ORDINARY:
        return view["ordinary_model"], None
    lead = view["snapshot"]["lead"]
    return lead["model"], lead.get("client_selector")


def _require_v4(record: dict[str, Any]) -> None:
    if record.get("version") != RECORD_VERSION:
        stable = record.get("managed_id", record.get("session_id", "?"))
        raise LegacyRecordError(
            f"session {stable} is a legacy record (version {record.get('version')}); "
            f"resume it (claude-multi -r {stable}) or run claude-multi migrate"
        )


def applied_document(record: dict[str, Any]) -> dict[str, Any]:
    """The profile v2 document a v4 record's ``applied`` restates (pure)."""

    _require_v4(record)
    applied = record["applied"]
    document: dict[str, Any] = {
        "version": 2,
        "name": record["profile"] or "ad-hoc",
        "description": "",
        "lead": {"model": applied["lead"]["key"], "effort": applied["lead"]["effort"]},
        "agents": {
            role_id: {"model": binding["key"], "effort": binding["effort"]}
            for role_id, binding in applied["agents"].items()
        },
        "native_agents": dict(applied["native_agents"]),
        "workflows": applied["workflows"],
    }
    if applied["lead_providers"]:
        document["lead_providers"] = list(applied["lead_providers"])
    if applied["settings_overrides"]:
        document["settings_overrides"] = dict(applied["settings_overrides"])
    if applied.get("primary_provider") is not None:
        document["primary_provider"] = applied["primary_provider"]
    return document


def _catalog_version_of(cat: Any) -> Any:
    version = getattr(cat, "catalog_version", None)
    if version is None:
        docs = getattr(cat, "docs", None)
        if isinstance(docs, Mapping):
            version = docs.get("version", {}).get("catalog_version")
    return "?" if version is None else version


def lead_key_needs_choice(key: str, cat: Any) -> bool:
    """True when ``key`` has no live line in ``cat`` (pure; never raises).

    ``cat`` is a ``catalog.Catalog`` or a (merged) ``profile.LineupCatalog``:
    anything with ``resolve_key``. A retired chain ending in null and a key
    unknown to the catalog (``CatalogError``: a migrated unregistered custom
    model, a custom model removed after launch) both need a choice (sc[11]).
    """

    from . import catalog as catalog_mod  # local: keep the hooks import set

    try:
        return cat.resolve_key(key).key is None
    except catalog_mod.CatalogError:
        return True


def lead_needs_choice(record: dict[str, Any], cat: Any) -> bool:
    """The one needs-a-choice predicate for a v4 record's lead."""

    _require_v4(record)
    return lead_key_needs_choice(record["applied"]["lead"]["key"], cat)


def lead_choice_notice(record: dict[str, Any], cat: Any) -> str | None:
    """The resolution notice for a v4 record's lead (None for a live key)."""

    from . import catalog as catalog_mod  # local: keep the hooks import set

    _require_v4(record)
    key = record["applied"]["lead"]["key"]
    try:
        return cat.resolve_key(key).notice
    except catalog_mod.CatalogError:
        return (
            f"lead {key}: unknown to catalog {_catalog_version_of(cat)} "
            "(not a custom model either)"
        )


@dataclass(frozen=True)
class RecordSummary:
    """The one record reader of the TUI and the text listings."""

    managed_id: str
    runtime_id: str
    version: int
    profile_label: str
    follow: bool | None  # None for legacy records
    lead_key: str
    agent_count: int
    lineup_generation: int | None  # None for legacy records
    needs_choice: bool | None  # None without a catalog or for legacy records
    pending: bool
    lead_target: bool
    title: str | None
    last_event_source: str | None
    last_seen_at: str
    identity_state: str
    cwd: str


def record_summary(record: dict[str, Any], cat: Any = None) -> RecordSummary:
    """Summarise a loaded record (v3 view or v4). Pure; never raises for a valid record."""

    if record.get("version") == RECORD_VERSION:
        applied = record["applied"]
        return RecordSummary(
            managed_id=record["managed_id"],
            runtime_id=record["runtime_session_id"],
            version=RECORD_VERSION,
            profile_label=record["profile"] or "(ad-hoc)",
            follow=bool(record["follow"]),
            lead_key=applied["lead"]["key"],
            agent_count=len(applied["agents"]),
            lineup_generation=record["lineup_generation"],
            needs_choice=None if cat is None else lead_needs_choice(record, cat),
            pending="pending" in record,
            lead_target="lead_target" in record,
            title=record.get("title"),
            last_event_source=record.get("last_event_source"),
            last_seen_at=record["last_seen_at"],
            identity_state=record["identity_state"],
            cwd=record["cwd"],
        )
    view = _normalize_legacy_record(record)
    ordinary = view.get("session_type") == SESSION_TYPE_ORDINARY
    key, _selector = lead_identity(view)
    return RecordSummary(
        managed_id=view["managed_id"],
        runtime_id=view["runtime_session_id"],
        version=view["version"],
        profile_label=(
            f"gateway:{view['ordinary_model']}"
            if ordinary
            else f"cm:{view['composition_name']}"
        ),
        follow=None,
        lead_key=key,
        agent_count=0 if ordinary else len(view["snapshot"].get("variants", [])),
        lineup_generation=None,
        needs_choice=None,
        pending=False,
        lead_target=False,
        title=None,
        last_event_source=view.get("last_event_source"),
        last_seen_at=view["last_seen_at"],
        identity_state=view["identity_state"],
        cwd=view["cwd"],
    )


# ------------------------------------------------------------ v4 builders

LAUNCH_TIME_SETTINGS_KEYS = ("availableModels", "modelPicker", "model", "env")


def launch_digest(settings: Mapping[str, Any], lead_set_bytes: bytes) -> str:
    """Digest of every launch-time item a live scope pins.

    The four ``settings.json`` keys the client reads once at start plus the
    exact ``lead-set.json`` bytes (the premodel gate). Key order and
    formatting of ``settings.json`` do not matter (parsed values are hashed
    canonically); other settings keys are ignored.
    """

    # The additive, sticky loopback bypass is reconstructed from files,
    # not launch intent. Repair may strengthen it without widening model fences.
    items = {key: settings[key] for key in LAUNCH_TIME_SETTINGS_KEYS if key in settings}
    if isinstance(items.get("env"), Mapping):
        items["env"] = {k: v for k, v in items["env"].items() if k not in ("NO_PROXY", "no_proxy")}
    return strict_json.bundle_digest(
        {
            "settings": items,
            "lead_set_sha256": strict_json.sha256_hex(bytes(lead_set_bytes)),
        }
    )


def lead_ref(lead: Mapping[str, Any]) -> dict[str, Any]:
    """``{key, effort, selector}`` of an ``applied.lead`` (``scope_lead``/``lead_target``)."""

    return {"key": lead["key"], "effort": lead["effort"], "selector": lead["selector"]}


def recorded_window(record: Mapping[str, Any]) -> int | None:
    """The compaction window a v4 record's session launched with
    (``applied.lead.compaction.window``), or None when the record states
    none (a migrated or linked record). Record-authority compiles decide
    each role's context class against it."""

    applied = record.get("applied") if isinstance(record, Mapping) else None
    lead = applied.get("lead") if isinstance(applied, Mapping) else None
    compaction = lead.get("compaction") if isinstance(lead, Mapping) else None
    window = compaction.get("window") if isinstance(compaction, Mapping) else None
    if isinstance(window, int) and not isinstance(window, bool) and window > 0:
        return window
    return None


def with_applied(record: dict[str, Any], applied: dict[str, Any]) -> dict[str, Any]:
    """Copy of a v4 record with ``applied`` replaced and ``applied_hash`` recomputed."""

    _require_v4(record)
    updated = copy.deepcopy(record)
    updated["applied"] = copy.deepcopy(applied)
    updated["applied_hash"] = strict_json.bundle_digest(updated["applied"])
    return updated


def applied_from_lineup(
    lineup: Any,
    *,
    context: Any,
    settings_snapshot: dict[str, Any],
    no_subagents: bool,
) -> dict[str, Any]:
    """The v4 ``applied`` object of a resolved lineup (pure).

    ``lineup`` is a ``profile.ResolvedLineup``, ``context`` the compile's
    ``compiler.LeadContext`` (override-aware percent) and
    ``settings_snapshot`` the Settings-level ``settings.snapshot``.
    """

    bindings = lineup.applied_bindings()
    lead_binding = lineup.lead.binding
    lead = {
        **bindings["lead"],
        "env": dict(lineup.lead.env),
        "compaction": {
            "window": context.window,
            "trigger": context.trigger,
            "percent": context.percent,
            "scalar": context.scalar,
            "client_tokens": lead_binding.client_context_tokens,
            "provider_tokens": lead_binding.provider_context_tokens,
        },
    }
    applied = {
        "lead": lead,
        "agents": copy.deepcopy(bindings["agents"]),
        "native_agents": dict(lineup.native_agents),
        "workflows": lineup.workflows,
        "lead_providers": list(lineup.lead_providers) if lineup.lead_providers else None,
        "settings_overrides": copy.deepcopy(dict(lineup.settings_overrides)),
        "no_subagents": bool(no_subagents),
        "settings": copy.deepcopy(settings_snapshot),
    }
    # Optional; the profile's warning suppression, so the record restates
    # the lineup.md checks its launch compiled.
    primary = getattr(lineup, "primary_provider", None)
    if primary is not None:
        applied["primary_provider"] = primary
    return applied


def make_v4_record(
    *,
    managed_id: str,
    cwd: str,
    profile: str | None,
    follow: bool,
    applied: dict[str, Any],
    lineup_generation: int,
    lead_class: str | None,
    catalog_version: int,
    catalog_hash: str,
    launcher_version: str,
    launch_epoch: int,
    launch_fence: str | None = None,
    scope_lead: dict[str, Any] | None = None,
    mutation_token: str | None = None,
    forked_from: str | None = None,
    identity_state: str = IDENTITY_UNVERIFIED,
    now: str | None = None,
) -> dict[str, Any]:
    """A fresh v4 record (a fresh launch, or ``sessions link`` at gen 0)."""

    timestamp = now or _now()
    record: dict[str, Any] = {
        "version": RECORD_VERSION,
        "managed_id": managed_id,
        "runtime_session_id": managed_id,
        "runtime_aliases": [],
        "identity_state": identity_state,
        "last_event_source": None,
        "last_seen_at": timestamp,
        "pending_forks": [],
        "launch_epoch": launch_epoch,
        "cwd": cwd,
        "profile": profile,
        "follow": bool(follow),
        "applied": copy.deepcopy(applied),
        "applied_hash": strict_json.bundle_digest(applied),
        "lineup_generation": lineup_generation,
        "lead_class": lead_class,
        "catalog_version": catalog_version,
        "catalog_hash": catalog_hash,
        "launcher_version": launcher_version,
        "created_at": timestamp,
        "forked_from": forked_from,
        "migrated_from_version": None,
    }
    if mutation_token is not None:
        record["mutation_token"] = mutation_token
    if launch_fence is not None:
        record["launch_fence"] = launch_fence
    if scope_lead is not None:
        record["scope_lead"] = copy.deepcopy(dict(scope_lead))
    return record


def next_v4_record(
    current: dict[str, Any],
    *,
    profile: str | None,
    follow: bool,
    applied: dict[str, Any],
    lineup_generation: int,
    lead_class: str | None,
    catalog_version: int,
    catalog_hash: str,
    launcher_version: str,
    launch_epoch: int,
    launch_fence: str | None,
    scope_lead: dict[str, Any] | None,
) -> dict[str, Any]:
    """The record a launch commits over ``current`` (resume / relaunch).

    Identity, lifecycle, migration, title, ``created_at`` and ``forked_from``
    are kept (perform re-copies title from its locked read);
    ``pending`` and ``lead_target`` are dropped (a launch applies both).
    """

    _require_v4(current)
    updated = copy.deepcopy(current)
    updated.update(
        {
            "profile": profile,
            "follow": bool(follow),
            "applied": copy.deepcopy(applied),
            "applied_hash": strict_json.bundle_digest(applied),
            "lineup_generation": lineup_generation,
            "lead_class": lead_class,
            "catalog_version": catalog_version,
            "catalog_hash": catalog_hash,
            "launcher_version": launcher_version,
            "launch_epoch": launch_epoch,
        }
    )
    for key, value in (("launch_fence", launch_fence), ("scope_lead", scope_lead)):
        if value is None:
            updated.pop(key, None)
        else:
            updated[key] = copy.deepcopy(value)
    updated.pop("pending", None)
    updated.pop("lead_target", None)
    return updated


def restore_overlay(backup_raw: dict[str, Any], current_v4: dict[str, Any]) -> dict[str, Any]:
    """restore-2x: the backup's v3 shape with the current lifecycle (pure).

    ``carry_lifecycle_state`` (``_LIFECYCLE_FIELDS``, unchanged) + the higher
    ``launch_epoch`` (keeps a v4 link/resolve-fork revocation) + the
    current ``cwd`` (a v4 relink) and ``launcher_version`` + a new mutation
    token. Every v4-only key is dropped: the result is a v3 record.
    """

    _require_v4(current_v4)
    b3 = copy.deepcopy(_normalize_legacy_record(backup_raw))
    merged = carry_lifecycle_state(b3, current_v4)
    merged["launch_epoch"] = max(
        b3.get("launch_epoch", 0), current_v4.get("launch_epoch", 0)
    )
    merged["cwd"] = current_v4["cwd"]
    merged["launcher_version"] = current_v4["launcher_version"]
    merged["mutation_token"] = new_mutation_token()
    return merged


def _normalize_legacy_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return an in-memory v3 view of a v1/v2 managed record.

    Loading is side-effect free: the original bytes remain on disk until a
    later explicit launch, transition, hook reconciliation, or repair saves the
    normalized record.
    """

    version = record.get("version")
    if version == V3_RECORD_VERSION:
        normalized = dict(record)
        normalized.setdefault("launch_epoch", 0)
        return normalized
    if version not in _PRE_V3_VERSIONS:
        return record
    old = dict(record)
    if version == 1:
        old.update({key: old.get(key, default) for key, default in _V2_DEFAULTS.items()})
    stable = old.get("session_id")
    timestamp = old.get("created_at")
    return {
        "version": V3_RECORD_VERSION,
        "managed_id": stable,
        "runtime_session_id": stable,
        "runtime_aliases": [],
        "session_type": SESSION_TYPE_MANAGED,
        "identity_state": IDENTITY_UNVERIFIED,
        "last_event_source": None,
        "last_seen_at": timestamp,
        "pending_forks": [],
        "launch_epoch": 0,
        "cwd": old.get("cwd"),
        "composition_name": old.get("composition_name"),
        "composition_hash": old.get("composition_hash"),
        "snapshot": old.get("snapshot"),
        "mode": old.get("mode"),
        "scope_generation": old.get("scope_generation"),
        "workflows": old.get("workflows"),
        "catalog_version": old.get("catalog_version"),
        "catalog_hash": old.get("catalog_hash"),
        "launcher_version": old.get("launcher_version"),
        "created_at": timestamp,
        "forked_from": old.get("forked_from"),
        "migrated_from_version": version,
    }


_IDENTITY_KEYS = (
    "managed_id",
    "runtime_session_id",
    "runtime_aliases",
    "identity_state",
    "last_event_source",
    "last_seen_at",
    "pending_forks",
    "migrated_from_version",
)


def _validate_identity(record: dict[str, Any], origin: str) -> str:
    """The identity/lifecycle checks shared by v3 and v4 (S:612-659 verbatim)."""

    stable = managed_id(record)
    runtime_session_id(record)
    token = record.get("mutation_token")
    if token is not None and (not isinstance(token, str) or not UUID4.fullmatch(token)):
        raise SessionError(f"{origin}: mutation_token {token!r} is not a UUIDv4")
    epoch = record.get("launch_epoch", 0)
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise SessionError(f"{origin}: launch_epoch {epoch!r} is invalid")
    aliases = record.get("runtime_aliases")
    if not isinstance(aliases, list) or len(aliases) > _MAX_RUNTIME_ALIASES:
        raise SessionError(f"{origin}: runtime_aliases is invalid")
    alias_ids: set[str] = set()
    for alias in aliases:
        if not isinstance(alias, dict):
            raise SessionError(f"{origin}: runtime alias is not an object")
        alias_id = alias.get("session_id")
        if not isinstance(alias_id, str) or not UUID4.fullmatch(alias_id):
            raise SessionError(f"{origin}: runtime alias {alias_id!r} is not a UUIDv4")
        if alias_id in alias_ids or alias_id == record["runtime_session_id"]:
            raise SessionError(f"{origin}: duplicate/current runtime alias {alias_id}")
        alias_ids.add(alias_id)
    if stable == record.get("forked_from"):
        raise SessionError(f"{origin}: session cannot be forked from itself")
    return stable


_V4_REQUIRED = (
    *_IDENTITY_KEYS,
    "cwd",
    "profile",
    "follow",
    "applied",
    "applied_hash",
    "lineup_generation",
    "lead_class",
)
_AGENT_ID = re.compile(r"^cm-[a-z][a-z0-9-]*$")


def _validate_v4_invariants(record: dict[str, Any], origin: str) -> None:
    """Code invariants of a version-4 record, after the schema."""

    for key in _V4_REQUIRED:
        if key not in record:
            raise SessionError(f"{origin}: version 4 record is missing {key!r}")
    _validate_identity(record, origin)
    from . import catalog as catalog_mod  # local: catalog never imports sessions

    applied = record["applied"]
    if not isinstance(applied, dict):
        raise SessionError(f"{origin}: applied is not an object")
    agents = applied.get("agents")
    if not isinstance(agents, dict):
        raise SessionError(f"{origin}: applied.agents is not an object")
    for role_id in agents:
        if (
            not isinstance(role_id, str)
            or not _AGENT_ID.fullmatch(role_id)
            or role_id == catalog_mod.LEAD_ROLE
            or role_id not in catalog_mod.AGENT_ROLE_IDS
        ):
            raise SessionError(f"{origin}: applied.agents key {role_id!r} is not an agent role")
    if record["applied_hash"] != strict_json.bundle_digest(applied):
        raise SessionError(
            f"{origin}: applied_hash does not match applied (hand-edited record?)"
        )
    if record["follow"] is True and record["profile"] is None:
        raise SessionError(f"{origin}: follow requires a profile")
    generation = record["lineup_generation"]
    lead = applied.get("lead") if isinstance(applied.get("lead"), dict) else {}
    if lead.get("selector") is None and generation != 0:
        raise SessionError(
            f"{origin}: a lead without a selector needs lineup_generation 0"
        )
    pending = record.get("pending")
    if pending is not None:
        if pending.get("kind") == "profile":
            if pending.get("profile") is None or pending.get("document") is not None:
                raise SessionError(
                    f"{origin}: a pending profile change names a profile and no document"
                )
        else:
            from . import profile as profile_mod  # local: profile imports sessions

            if not isinstance(pending.get("document"), dict):
                raise SessionError(f"{origin}: a pending lineup change needs a document")
            try:
                profile_mod.parse(pending["document"])
            except profile_mod.ProfileValidationError as exc:
                raise SessionError(f"{origin}: pending document is invalid: {exc}") from exc
    migration = record.get("migration")
    if migration is not None and record["migrated_from_version"] != migration.get("from_version"):
        raise SessionError(
            f"{origin}: migrated_from_version disagrees with migration.from_version"
        )
    title = record.get("title")
    if title is not None and (
        not isinstance(title, str) or not title.isprintable() or "\n" in title
    ):
        raise SessionError(f"{origin}: title must be one printable line")
    compiled = isinstance(generation, int) and generation >= 1
    for key in ("launch_fence", "scope_lead"):
        if (key in record) != compiled:
            raise SessionError(
                f"{origin}: {key} is required exactly when lineup_generation >= 1"
            )
    target = record.get("lead_target")
    if target is not None and (target.get("key"), target.get("effort")) == (
        lead.get("key"),
        lead.get("effort"),
    ):
        raise SessionError(f"{origin}: lead_target equals the applied lead")


def _validate_record_invariants(record: dict[str, Any], origin: str) -> None:
    version = record.get("version")
    if version in _PRE_V3_VERSIONS:
        value = record.get("session_id")
        if not isinstance(value, str) or not UUID4.fullmatch(value):
            raise SessionError(f"{origin}: legacy session_id {value!r} is not a UUIDv4")
        if version == 2:
            for key in _V2_DEFAULTS:
                if key not in record:
                    raise SessionError(
                        f"{origin}: version 2 record is missing {key!r}"
                    )
        return
    if version == RECORD_VERSION:
        _validate_v4_invariants(record, origin)
        return
    if version != V3_RECORD_VERSION:
        raise SessionError(f"{origin}: unsupported session record version {version!r}")
    for key in (*_IDENTITY_KEYS[:4], "session_type", *_IDENTITY_KEYS[4:]):
        if key not in record:
            raise SessionError(f"{origin}: version 3 record is missing {key!r}")
    if record.get("session_type") not in {SESSION_TYPE_MANAGED, SESSION_TYPE_ORDINARY}:
        raise SessionError(f"{origin}: invalid session_type {record.get('session_type')!r}")
    _validate_identity(record, origin)
    if record["session_type"] == SESSION_TYPE_MANAGED:
        for key in (
            "composition_name",
            "composition_hash",
            "snapshot",
            "mode",
            "scope_generation",
            "workflows",
        ):
            if key not in record:
                raise SessionError(f"{origin}: managed record is missing {key!r}")
    else:
        for key in ("ordinary_model", "context_profile"):
            if key not in record:
                raise SessionError(f"{origin}: ordinary record is missing {key!r}")
        if any(key in record for key in ("composition_name", "snapshot", "workflows")):
            raise SessionError(f"{origin}: ordinary record carries composition fields")


class _Sentinel:
    __slots__ = ("_name",)

    def __init__(self, name: str):
        self._name = name

    def __repr__(self) -> str:
        return self._name


# v4 model evidence: the observed model is a form of
# the recorded lead. ``None`` means "no admissible evidence" (observed_model
# untouched); a string is the raw observed model that is not such a form.
MODEL_EQUIVALENT = _Sentinel("MODEL_EQUIVALENT")


class Evidence(NamedTuple):
    """What a hook's ``normalize`` callback returns under the record locks."""

    model: object  # MODEL_EQUIVALENT | str | None (v4); str | None (v3)
    model_profile: str | None
    observed_model: str | None
    cwd: str | None


def reconcile_runtime_record(
    record: dict[str, Any],
    *,
    observed_runtime_id: str,
    source: str,
    cwd: str | None = None,
    model: Any = None,
    model_profile: str | None = None,
    observed_model: str | None = None,
    launch_epoch: int | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Return an idempotently reconciled record view from SessionStart metadata.

    v1-3 input -> the reconciled v3 view (the legacy rules byte-for-byte); v4 input
    -> the reconciled v4 record, whose model evidence is tri-state
    (``MODEL_EQUIVALENT`` / ``None`` / raw str) and never re-pins the lead.
    """

    if not UUID4.fullmatch(observed_runtime_id):
        raise SessionError(f"runtime session_id {observed_runtime_id!r} is not a UUIDv4")
    current = _normalize_legacy_record(record)
    _validate_record_invariants(current, "reconcile")
    if model is MODEL_EQUIVALENT and current.get("version") != RECORD_VERSION:
        raise SessionError("MODEL_EQUIVALENT is v4 model evidence; a legacy record takes a model string")
    timestamp = now or _now()
    current_epoch = current.get("launch_epoch", 0)
    observed_epoch = 0 if launch_epoch is None else launch_epoch
    if (
        not isinstance(observed_epoch, int)
        or isinstance(observed_epoch, bool)
        or observed_epoch < 0
    ):
        raise SessionError(f"launch_epoch {observed_epoch!r} is invalid")
    if source != "repair" and observed_epoch < current_epoch:
        return dict(current)
    alias_ids = {
        item.get("session_id") for item in current.get("runtime_aliases", [])
    }
    if (
        source not in {"fork", "repair"}
        and observed_epoch <= current_epoch
        and observed_runtime_id in alias_ids
    ):
        # A delayed hook from an older runtime must never retarget authority or
        # contribute model/CWD evidence to the newer launch.
        return dict(current)
    updated = dict(current)
    updated["launch_epoch"] = observed_epoch
    updated["last_event_source"] = source
    updated["last_seen_at"] = timestamp
    if cwd is not None and cwd != current["cwd"]:
        updated["observed_cwd"] = cwd
    if source == "fork":
        pending = list(current.get("pending_forks", []))
        if not any(item.get("session_id") == observed_runtime_id for item in pending):
            if len(pending) >= _MAX_RUNTIME_ALIASES:
                # Never evict a genuine marker silently: the
                # operator was told about every pending fork by name, so
                # dropping the oldest would orphan a fork they were asked to
                # resolve. Fail the hook visibly instead.
                raise SessionError(
                    f"session {managed_id(current)} already tracks "
                    f"{_MAX_RUNTIME_ALIASES} unresolved native forks; resolve "
                    "some (adopt or resolve-fork) before more can be tracked"
                )
            pending.append({"session_id": observed_runtime_id, "observed_at": timestamp})
        updated["pending_forks"] = pending[-_MAX_RUNTIME_ALIASES:]
        if current.get("identity_state") == IDENTITY_REPAIR_NEEDED:
            updated["identity_state"] = IDENTITY_REPAIR_NEEDED
        else:
            updated["identity_state"] = _derived_identity_state(updated)
        return updated

    prior_runtime = current["runtime_session_id"]
    aliases = [
        dict(item)
        for item in current.get("runtime_aliases", [])
        if item.get("session_id") not in {observed_runtime_id, prior_runtime}
    ]
    if prior_runtime != observed_runtime_id:
        aliases.append(
            {
                "session_id": prior_runtime,
                "source": current.get("last_event_source") or "previous",
                "observed_at": timestamp,
            }
        )
    updated["runtime_session_id"] = observed_runtime_id
    updated["runtime_aliases"] = aliases[-_MAX_RUNTIME_ALIASES:]
    pending = [
        dict(item)
        for item in current.get("pending_forks", [])
        if item.get("session_id") != observed_runtime_id
    ]
    if len(pending) != len(current.get("pending_forks", [])):
        # Authority landing on a pending fork resolves it: the live runtime
        # IS that fork, so there is nothing left to adopt or discard.
        updated["pending_forks"] = pending
    if current.get("version") == RECORD_VERSION:
        if model is MODEL_EQUIVALENT:
            updated.pop("observed_model", None)
        elif model is not None:
            updated["observed_model"] = model
        # model None: observed_model untouched (legacy parity, `elif model:`),
        # so earlier repair evidence survives compacts, resumes and relinks.
        # No re-pin: the lead changes only via a user-source PostModelSwitch.
    elif current["session_type"] == SESSION_TYPE_ORDINARY:
        if observed_model and model is None:
            updated["observed_model"] = observed_model
        elif model:
            if model_profile != current["context_profile"] or (
                # Null-profile (single-model) records share no fence: same
                # profile-None does NOT imply same model, so a different
                # reconciled model must stay an observation, never a re-pin
                # (the live scope still fences the recorded model).
                current["context_profile"] is None
                and model != current["ordinary_model"]
            ):
                updated["observed_model"] = observed_model or model
            else:
                updated["ordinary_model"] = model
                updated.pop("observed_model", None)
    elif model:
        if model != current["snapshot"]["lead"]["client_selector"]:
            updated["observed_model"] = observed_model or model
        else:
            updated.pop("observed_model", None)
    updated["identity_state"] = _derived_identity_state(updated)
    return updated


# Lifecycle writers patch the raw on-disk document: every writer
# loads with ``load_raw`` inside its lock, computes on ``_lifecycle_view(raw)``
# and saves ``_patch_lifecycle(raw, updated, extra)``. A v3/v4 record keeps
# its own version and every non-lifecycle byte; a v1/v2 record is saved as its
# whole v3 view (the legacy persistence, still readable by earlier launchers). No lifecycle
# writer ever produces version 4 from 1-3.
LIFECYCLE_PATCH_KEYS = (*_LIFECYCLE_FIELDS, "launch_epoch")


def _lifecycle_view(raw: dict[str, Any]) -> dict[str, Any]:
    """v4 -> deep copy (+ ``launch_epoch`` default 0); v1-3 -> the v3 view."""

    if raw.get("version") == RECORD_VERSION:
        view = copy.deepcopy(raw)
        view.setdefault("launch_epoch", 0)
        return view
    return copy.deepcopy(_normalize_legacy_record(raw))


def _patch_lifecycle(
    raw: dict[str, Any],
    updated_view: dict[str, Any],
    extra_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The document a lifecycle writer saves (version preserved)."""

    if raw.get("version") in _PRE_V3_VERSIONS:
        return copy.deepcopy(updated_view)
    document = copy.deepcopy(raw)
    for key in (*LIFECYCLE_PATCH_KEYS, *extra_keys):
        if key in updated_view:
            document[key] = copy.deepcopy(updated_view[key])
        else:
            document.pop(key, None)
    return document


def _is_v3_ordinary(raw: dict[str, Any]) -> bool:
    return (
        raw.get("version") == V3_RECORD_VERSION
        and raw.get("session_type") == SESSION_TYPE_ORDINARY
    )


class SessionStore:
    """UUID-keyed session records plus per-CWD last-session pointers."""

    def __init__(self, root: Path | str, schema: dict[str, Any], *, read_only: bool = False):
        self.read_only = read_only
        directory = Path if read_only else state.ensure_private_dir
        self.root = directory(Path(root))
        self.schema = schema
        self.sessions_dir = directory(self.root / "sessions")
        self.pointers_dir = directory(self.root / "last-session-by-cwd")

    def _require_writable(self) -> None:
        if self.read_only:
            raise SessionError("session store is read-only")

    def _record_path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not UUID4.fullmatch(session_id):
            raise SessionError(f"session_id {session_id!r} is not a UUIDv4")
        return self.sessions_dir / f"{session_id}.json"

    def backup_path(self, managed_session_id: str) -> Path:
        """``sessions/<id>.v3.json``: the legacy backup ``migrate`` keeps (restore-2x)."""

        if not isinstance(managed_session_id, str) or not UUID4.fullmatch(managed_session_id):
            raise SessionError(f"session_id {managed_session_id!r} is not a UUIDv4")
        return self.sessions_dir / f"{managed_session_id}{BACKUP_SUFFIX}"

    def scan_uuid_records(self) -> list[Path]:
        """Sorted ``sessions/*.json`` whose stem is a UUIDv4 (never a backup)."""

        return sorted(
            path for path in self.sessions_dir.glob("*.json") if UUID4.fullmatch(path.stem)
        )

    def new_id(self) -> str:
        """Mint a UUIDv4, retrying on the (astronomically unlikely) collision."""

        for _ in range(_COLLISION_RETRIES):
            candidate = str(uuid.uuid4())
            if not os.path.lexists(self._record_path(candidate)):
                return candidate
        raise SessionError("could not mint a collision-free session UUID")

    def _validate(self, record: dict[str, Any], origin: str) -> dict[str, Any]:
        problems = schema_validate.validate(record, self.schema, "$")
        if problems:
            raise SessionError(f"{origin}: invalid session record: {'; '.join(problems)}")
        _validate_record_invariants(record, origin)
        return record

    def _write_record(self, record: dict[str, Any], origin: str) -> Path:
        self._require_writable()
        self._validate(record, origin)
        path = self._record_path(managed_id(record))
        state.atomic_write(path, strict_json.canonical_file_bytes(record))
        return path

    def save(self, record: dict[str, Any]) -> Path:
        """Validate and write a whole version-4 record (the only launcher write).

        No launcher path writes a v1-3 record: lifecycle writers patch the
        raw document in its own version through ``_save_lifecycle``, and
        ``restore-2x`` writes the restored v3 bytes the same way. A v4
        record needs the state marker at 4: ``ensure_state_v4`` or
        ``migrate`` writes it before the first v4 record.
        """
        self._require_writable()

        if record.get("version") != RECORD_VERSION:
            raise SessionError(
                f"save: only version {RECORD_VERSION} records are written by this "
                f"launcher (got version {record.get('version')!r})"
            )
        marker = check_state_marker(self.root)
        if marker < SUPPORTED_STATE_VERSION:
            raise SessionError(
                "state marker missing: v4 records need state version 4 "
                "(run claude-multi migrate)"
            )
        return self._write_record(record, "save")

    def _save_lifecycle(self, document: dict[str, Any]) -> Path:
        """Write a lifecycle writer's patched document in its own version."""
        self._require_writable()

        check_state_marker(self.root)
        return self._write_record(document, "save")

    def _read_validated(self, managed_session_id: str) -> tuple[dict[str, Any], bytes]:
        if not isinstance(managed_session_id, str) or not UUID4.fullmatch(managed_session_id):
            raise SessionError(f"managed_id {managed_session_id!r} is not a UUIDv4")
        path = self._record_path(managed_session_id)
        try:
            raw = state.read_private(path)
        except state.StateError as exc:
            raise SessionError(
                f"cannot read session record {managed_session_id}: {exc}"
            ) from exc
        try:
            record = strict_json.loads(raw)
        except strict_json.StrictJSONError as exc:
            raise SessionError(
                f"corrupt session record {managed_session_id}: {exc}; "
                "use `claude-multi sessions forget` to remove it"
            ) from exc
        if not isinstance(record, dict):
            raise SessionError(
                f"corrupt session record {managed_session_id}: not an object"
            )
        record = self._validate(record, f"session {managed_session_id}")
        embedded = managed_id(record)
        if embedded != managed_session_id:
            raise SessionError(
                f"corrupt session record {managed_session_id}: embedded managed_id "
                f"{embedded!r} does not match its record path"
            )
        return record, raw

    def load_raw(self, managed_session_id: str) -> tuple[dict[str, Any], bytes]:
        """The validated on-disk document (no normalisation) and its exact bytes."""

        return self._read_validated(managed_session_id)

    def load(self, managed_session_id: str) -> dict[str, Any]:
        """A v4 record as is (``launch_epoch`` defaulted), or the v3 view of 1-3."""

        record, _raw = self._read_validated(managed_session_id)
        if record["version"] == RECORD_VERSION:
            record.setdefault("launch_epoch", 0)
            return record
        normalized = _normalize_legacy_record(record)
        self._validate(normalized, f"session {managed_session_id} (normalized)")
        return normalized

    def load_v4(self, managed_session_id: str) -> dict[str, Any]:
        """``load`` for callers that need v4 semantics; 1-3 -> LegacyRecordError."""

        record, _raw = self._read_validated(managed_session_id)
        if record["version"] != RECORD_VERSION:
            raise LegacyRecordError(
                f"session {managed_session_id} is a legacy record (version "
                f"{record['version']}); resume it (claude-multi -r "
                f"{managed_session_id}) or run claude-multi migrate"
            )
        record.setdefault("launch_epoch", 0)
        return record

    def _unreadable_record_claims_runtime(
        self, path: Path, identifier: str
    ) -> bool:
        """Conservatively detect runtime ownership in an invalid record."""

        try:
            raw = state.read_private(path)
        except state.StateError as exc:
            raise SessionError(
                f"cannot determine runtime ownership while session record "
                f"{path.stem} is unreadable: {exc}"
            ) from exc
        try:
            document = strict_json.loads(raw)
        except strict_json.StrictJSONError as strict_exc:
            # Strict JSON rejects duplicate keys. Decode once with preserved
            # object pairs so escaped UUIDs and every duplicate ownership field
            # remain visible. If even that fails, ownership is unknowable and
            # all new runtime assignment must fail closed.
            try:
                diagnostic = json.loads(raw, object_pairs_hook=_JSONPairs)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise SessionError(
                    f"cannot determine runtime ownership while session record "
                    f"{path.stem} is malformed: {strict_exc}"
                ) from exc
            if isinstance(diagnostic, _JSONPairs):
                aliases: list[Any] = []
                for key, value in diagnostic:
                    if key in {"runtime_session_id", "session_id"} and value == identifier:
                        return True
                    if key == "runtime_aliases" and isinstance(value, list):
                        aliases.extend(value)
                for alias in aliases:
                    if isinstance(alias, _JSONPairs):
                        if any(
                            key == "session_id" and value == identifier
                            for key, value in alias
                        ):
                            return True
                    elif alias == identifier:
                        return True
            return False
        if not isinstance(document, dict):
            return False
        if document.get("runtime_session_id") == identifier:
            return True
        if document.get("session_id") == identifier:
            return True
        aliases = document.get("runtime_aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                if isinstance(alias, dict) and alias.get("session_id") == identifier:
                    return True
                if alias == identifier:
                    return True
        return False

    def _scan_runtime(
        self, identifier: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None]:
        """(records whose runtime id/alias is ``identifier``, a record listing it
        as a pending fork, the record whose managed id it is); fail closed on an
        unreadable record that may claim it."""

        matches: list[dict[str, Any]] = []
        fork_owner: dict[str, Any] | None = None
        stable_owner: dict[str, Any] | None = None
        for path in self.scan_uuid_records():
            try:
                record = self.load(path.stem)
            except SessionError as exc:
                if self._unreadable_record_claims_runtime(path, identifier):
                    raise SessionError(
                        f"unreadable session record {path.stem} may claim runtime "
                        f"session {identifier}: {exc}; repair or forget that record "
                        "before assigning this runtime UUID"
                    ) from exc
                continue
            ids = {record["runtime_session_id"]} | {
                item["session_id"] for item in record.get("runtime_aliases", [])
            }
            if identifier in ids:
                matches.append(record)
            elif fork_owner is None and any(
                item.get("session_id") == identifier
                for item in record.get("pending_forks", [])
            ):
                fork_owner = record
            if record["managed_id"] == identifier:
                stable_owner = record
        if len(matches) > 1:
            choices = ", ".join(sorted(managed_id(item) for item in matches))
            raise SessionError(
                f"runtime session id {identifier} maps to multiple managed records: "
                f"{choices}; repair the duplicate mapping"
            )
        return matches, fork_owner, stable_owner

    def resolve(self, identifier: str) -> dict[str, Any]:
        """Resolve a unique stable ID, runtime ID, or historical runtime alias."""

        if not UUID4.fullmatch(identifier):
            raise SessionError(f"session identifier {identifier!r} is not a UUIDv4")
        if self.exists(identifier):
            return self.load(identifier)
        matches, _fork, _stable = self._scan_runtime(identifier)
        if not matches:
            raise SessionError(f"no managed session matches {identifier}")
        return matches[0]

    def resolve_human(
        self, value: str, *, names: Callable[[dict[str, Any]], Iterable[str]] | None = None,
    ) -> dict[str, Any]:
        """The session a person named (``-r``, ``sessions show|forget|stop``,
        ``explain``, ``usage --session``): an exact UUID (a managed id first,
        then a runtime id or alias through :meth:`resolve`'s ownership
        checks), an unambiguous UUID prefix of at least
        :data:`HUMAN_PREFIX_MIN` characters over managed and runtime ids, or
        an exact name (``names(record)``: the names a record answers to).
        Matches are deduplicated by managed id; several refuse with
        :class:`AmbiguousSessionError` (never the newest), none with
        :class:`SessionError`. An unreadable record that may match refuses
        too (fail closed). Protocol positions (``/cm --session``, hooks)
        stay runtime-UUID-only: they use :meth:`resolve_runtime`.
        """

        if not isinstance(value, str) or not value:
            raise SessionError("no session given")
        # Ids are case-insensitive for a person: an uppercase id or prefix is
        # the same session, checked exactly as its lowercase spelling.
        lowered = value.lower()
        if UUID4.fullmatch(lowered):
            return self.resolve(lowered)
        prefix = lowered if UUID_PREFIX.fullmatch(lowered) and len(value) >= HUMAN_PREFIX_MIN else None
        if prefix is None and names is None:
            raise SessionError(f"session {value!r} is neither a session id nor a prefix of at least "
                               f"{HUMAN_PREFIX_MIN} characters of one",
                               remedy="list the sessions: claude-multi sessions list")
        found: dict[str, dict[str, Any]] = {}
        unreadable: list[str] = []
        for path in self.scan_uuid_records():
            try:
                record = self.load(path.stem)
            except SessionError:
                if prefix is not None and path.stem.startswith(prefix):
                    unreadable.append(path.stem)
                elif self._unreadable_record_mentions(path, value, prefix=prefix):
                    unreadable.append(path.stem)
                continue
            mid = managed_id(record)
            ids = {mid, record.get("runtime_session_id", ""),
                   *(item.get("session_id", "") for item in record.get("runtime_aliases", []) or [])}
            if prefix is not None and any(isinstance(item, str) and item.startswith(prefix) for item in ids):
                found[mid] = record
            elif names is not None and value in set(names(record)):
                found[mid] = record
        if unreadable:
            raise SessionError(
                f"session record {unreadable[0]} is unreadable and may be {value!r}; nothing was chosen",
                remedy=f"repair or forget it first: claude-multi sessions forget {unreadable[0]}",
            )
        if len(found) > 1:
            raise AmbiguousSessionError(value, sorted(found))
        if not found:
            raise SessionError(f"no managed session matches {value!r}",
                               remedy="list the sessions: claude-multi sessions list")
        return next(iter(found.values()))

    def _unreadable_record_mentions(self, path: Path, value: str, *, prefix: str | None = None) -> bool:
        """Whether an unreadable record may be the one a person named
        (fail closed): its file cannot be read or decoded, or a string in it
        (keys and values, JSON escapes decoded, duplicate keys kept) carries
        ``value`` or, case-insensitively, starts with the id ``prefix``. The
        raw bytes are checked too, without regard to case."""

        try:
            raw = state.read_private(path)
        except state.StateError:
            return True
        needles = {value.lower()} | ({prefix} if prefix else set())
        if any(needle.encode("utf-8", "replace") in raw.lower() for needle in needles):
            return True
        try:
            document = json.loads(raw, object_pairs_hook=_JSONPairs)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
            return True  # ownership is unknowable: it may be the one meant

        def strings(node: Any) -> Iterator[str]:
            if isinstance(node, _JSONPairs):
                for key, item in node:
                    yield key
                    yield from strings(item)
            elif isinstance(node, list):
                for item in node:
                    yield from strings(item)
            elif isinstance(node, str):
                yield node

        for text in strings(document):
            folded = text.lower()
            if value.lower() in folded or (prefix is not None and folded.startswith(prefix)):
                return True
        return False

    def resolve_runtime(self, identifier: str) -> dict[str, Any]:
        """Runtime ids and runtime aliases only (``lineup --session``).

        Never the managed-id file shortcut of :meth:`resolve`: a managed id
        that is not also a runtime id is refused with the runtime id to use;
        a pending fork raises :class:`PendingForkError`. Unreadable records
        that may claim the id fail closed, as in ``resolve``.
        """

        if not isinstance(identifier, str) or not UUID4.fullmatch(identifier):
            raise SessionError(f"session identifier {identifier!r} is not a UUIDv4")
        matches, fork_owner, stable_owner = self._scan_runtime(identifier)
        if matches:
            return matches[0]
        if fork_owner is not None:
            raise PendingForkError(fork_owner)
        if stable_owner is not None:
            raise SessionError(
                f"{identifier} is a stable session id; lineup --session takes the "
                f"runtime id ({stable_owner['runtime_session_id']}) — /cm passes it "
                "automatically"
            )
        raise SessionError(f"no claude-multi session has runtime id {identifier}")

    def reconcile_runtime(
        self,
        stable_id: str,
        *,
        observed_runtime_id: str,
        source: str,
        cwd: str | None = None,
        model: Any = None,
        model_profile: str | None = None,
        observed_model: str | None = None,
        launch_epoch: int | None = None,
        now: str | None = None,
        normalize: Callable[[dict[str, Any], str | None, str | None], Evidence] | None = None,
    ) -> dict[str, Any]:
        """Atomically apply one metadata-only lifecycle observation.

        ``normalize``: when given, it runs under the
        runtime-index and lifecycle locks on the view of the record actually
        loaded, with the raw observed model (``observed_model``, else
        ``model``) and the raw payload ``cwd``; its :class:`Evidence` replaces
        ``model``, ``model_profile``, ``observed_model`` and ``cwd``. So a
        record converted between the hook's lock-free read and the lock is
        judged on its locked version. The raw document keeps its version;
        a v3 ordinary re-pin reaches disk (``ordinary_model``, sc[1]).
        """
        self._require_writable()

        index_lock = self.runtime_index_lock()
        lock = self.lifecycle_lock(stable_id)
        index_lock.acquire(blocking=True)
        lock.acquire(blocking=True)
        try:
            raw, _raw_bytes = self.load_raw(stable_id)
            current = _lifecycle_view(raw)
            if normalize is not None:
                raw_observed = observed_model if observed_model is not None else model
                evidence = normalize(copy.deepcopy(current), raw_observed, cwd)
                model = evidence.model
                model_profile = evidence.model_profile
                observed_model = evidence.observed_model
                cwd = evidence.cwd
            try:
                owner = self.resolve(observed_runtime_id)
            except SessionError as exc:
                if "no managed session matches" not in str(exc):
                    raise
                owner = None
            if owner is not None and managed_id(owner) != stable_id:
                if source != "fork":
                    raise SessionError(
                        f"runtime session {observed_runtime_id} is already owned "
                        f"by managed session {managed_id(owner)}"
                    )
                # A delayed/retried fork hook after adoption is already
                # resolved. Remove any stale pending entry instead of
                # re-blocking the parent.
                pending = current.get("pending_forks", [])
                filtered = [
                    dict(item)
                    for item in pending
                    if item.get("session_id") != observed_runtime_id
                ]
                if len(filtered) != len(pending):
                    updated = {**current, "pending_forks": filtered}
                    if current.get("identity_state") != IDENTITY_REPAIR_NEEDED:
                        updated["identity_state"] = _derived_identity_state(updated)
                    self._save_lifecycle(_patch_lifecycle(raw, updated))
                    return updated
                return current
            if (
                source == "fork"
                and owner is not None
                and managed_id(owner) == stable_id
            ):
                return current
            updated = reconcile_runtime_record(
                current,
                observed_runtime_id=observed_runtime_id,
                source=source,
                cwd=cwd,
                model=model,
                model_profile=model_profile,
                observed_model=observed_model,
                launch_epoch=launch_epoch,
                now=now,
            )
            extra = ("ordinary_model",) if _is_v3_ordinary(raw) else ()
            self._save_lifecycle(_patch_lifecycle(raw, updated, extra))
            return updated
        finally:
            lock.release()
            index_lock.release()

    def relink_runtime(
        self,
        stable_id: str,
        *,
        observed_runtime_id: str,
        cwd: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Atomically repair runtime ownership and optional authoritative CWD.

        A launcher write: after the state-marker check, the shared
        migration hold comes first, then the runtime-index lock, the lifecycle lock and the pointer locks; a
        running ``migrate``/``restore-2x`` refuses with
        :class:`MigrationBusyError` before anything is written. The hold is
        released on return, so a caller's follow-up converge
        (``_doctor_repair``) takes its own (``FileLock`` is not re-entrant).
        """
        self._require_writable()

        if not UUID4.fullmatch(observed_runtime_id):
            raise SessionError(
                f"runtime session_id {observed_runtime_id!r} is not a UUIDv4"
            )
        # An unknown marker refuses before the hold creates migration.lock.
        check_state_marker(self.root)
        with launcher_write_guard(self.root):
            return self._relink_runtime_locked(
                stable_id,
                observed_runtime_id=observed_runtime_id,
                cwd=cwd,
                now=now,
            )

    def _relink_runtime_locked(
        self,
        stable_id: str,
        *,
        observed_runtime_id: str,
        cwd: str | None,
        now: str | None,
    ) -> dict[str, Any]:
        index_lock = self.runtime_index_lock()
        lock = self.lifecycle_lock(stable_id)
        index_lock.acquire(blocking=True)
        lock.acquire(blocking=True)
        try:
            raw, _raw_bytes = self.load_raw(stable_id)
            current = _lifecycle_view(raw)
            try:
                owner = self.resolve(observed_runtime_id)
            except SessionError as exc:
                if "no managed session matches" not in str(exc):
                    raise
            else:
                if managed_id(owner) != stable_id:
                    raise SessionError(
                        f"runtime session {observed_runtime_id} is already owned "
                        f"by managed session {managed_id(owner)}"
                    )
            repaired = {
                **current,
                "launch_epoch": current.get("launch_epoch", 0) + 1,
                "mutation_token": new_mutation_token(),
            }
            # A relink always re-asserts the runtime's home directory: an
            # explicit --cwd re-homes, a bare relink re-asserts the recorded
            # cwd — either way the stale observed_cwd evidence is resolved
            # by the operator's assertion. A wrong bare assert
            # degrades to a native "No conversation found", which the
            # resume gate pre-detects; observed_model is untouched (no model
            # is passed: the v4 tri-state keeps it, sc[0]).
            if cwd is not None:
                repaired["cwd"] = cwd
            repaired.pop("observed_cwd", None)
            updated = reconcile_runtime_record(
                repaired,
                observed_runtime_id=observed_runtime_id,
                source="repair",
                cwd=repaired["cwd"],
                launch_epoch=repaired["launch_epoch"],
                now=now,
            )
            document = _patch_lifecycle(raw, updated, ("cwd", "mutation_token"))
            if updated["cwd"] == current["cwd"]:
                prior_record = self.read_record_bytes(stable_id)
                try:
                    self._save_lifecycle(document)
                except state.CommittedStateError:
                    if prior_record is not None:
                        self.restore_record_bytes(stable_id, prior_record)
                    raise
                return updated

            # Each pointer file is gated on its own payload (cf[11]): the
            # merged last() could name this session through <d>.ordinary.json
            # while <d>.json names another one.
            old_pointers = tuple(
                path
                for path in (
                    self._pointer_path(current["cwd"]),
                    self._legacy_pointer_path(current["cwd"]),
                )
                if path == self._pointer_path(current["cwd"]) or os.path.lexists(path)
            )
            new_pointer = self._pointer_path(updated["cwd"])
            touched = sorted({*old_pointers, new_pointer}, key=str)
            pointer_locks = [state.FileLock(path) for path in touched]
            for pointer_lock in pointer_locks:
                pointer_lock.acquire(blocking=True)
            prior_record = self.read_record_bytes(stable_id)
            prior_pointers: dict[Path, bytes | None] = {}
            try:
                prior_pointers = {
                    path: state.read_private(path) if os.path.lexists(path) else None
                    for path in touched
                }
                self._save_lifecycle(document)
                for pointer in old_pointers:
                    if self._pointer_names(pointer, stable_id):
                        state.remove_private(pointer)
                payload = {"cwd": updated["cwd"], "session_id": stable_id}
                state.atomic_write(
                    new_pointer, strict_json.canonical_file_bytes(payload)
                )
                return updated
            except BaseException:
                if prior_record is not None:
                    self.restore_record_bytes(stable_id, prior_record)
                for pointer, data in prior_pointers.items():
                    if data is None:
                        state.remove_private(pointer)
                    else:
                        state.atomic_write(pointer, data)
                raise
            finally:
                for pointer_lock in reversed(pointer_locks):
                    pointer_lock.release()
        finally:
            lock.release()
            index_lock.release()

    def record_session_end(
        self,
        stable_id: str,
        *,
        observed_runtime_id: str,
        reason: str,
        launch_epoch: int | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Record advisory SessionEnd metadata without changing resume identity.

        A no-op unless the epoch equals the record's and the observed runtime
        id is the record's current runtime: a delayed end
        from a historical runtime or a fork that shares the scope and epoch
        never marks the authority ended.
        """
        self._require_writable()

        if not UUID4.fullmatch(observed_runtime_id):
            raise SessionError(
                f"runtime session_id {observed_runtime_id!r} is not a UUIDv4"
            )
        lock = self.lifecycle_lock(stable_id)
        lock.acquire(blocking=True)
        try:
            raw, _raw_bytes = self.load_raw(stable_id)
            current = _lifecycle_view(raw)
            observed_epoch = 0 if launch_epoch is None else launch_epoch
            if observed_epoch != current.get("launch_epoch", 0):
                return current
            if observed_runtime_id != current["runtime_session_id"]:
                return current
            updated = {
                **current,
                "last_event_source": "end",
                "last_end_reason": reason,
                "last_seen_at": now or _now(),
            }
            self._save_lifecycle(_patch_lifecycle(raw, updated))
            return updated
        finally:
            lock.release()

    def mark_ended(
        self,
        stable_id: str,
        *,
        is_live: Callable[[dict[str, Any]], bool] | None = None,
        reason: str = MARKED_ENDED_REASON,
        now: str | None = None,
    ) -> bool:
        """Write a synthetic ``end`` for a session the operator knows has exited.

        For ``sessions mark-ended`` and the rotation's dead-confirm path. A
        launcher write: the shared migration hold, then the lifecycle lock;
        the record keeps its version and its mutation
        token (a lifecycle patch, like a real SessionEnd). ``is_live(view)``
        runs under the lock: a session live in the daemon or in ``/proc``
        refuses. False when the last event already is ``end``.
        """
        self._require_writable()

        with launcher_write_guard(self.root):
            lock = self.lifecycle_lock(stable_id)
            lock.acquire(blocking=True)
            try:
                raw, _raw_bytes = self.load_raw(stable_id)
                view = _lifecycle_view(raw)
                if view.get("last_event_source") == "end":
                    return False
                if is_live is not None and is_live(view):
                    raise SessionError(
                        f"session {stable_id} is live (Claude background daemon or a "
                        "running process); stop it first (claude-multi sessions stop "
                        f"{stable_id})"
                    )
                updated = {
                    **view,
                    "last_event_source": "end",
                    "last_end_reason": reason,
                    "last_seen_at": now or _now(),
                }
                self._save_lifecycle(_patch_lifecycle(raw, updated))
                return True
            finally:
                lock.release()

    def exists(self, session_id: str) -> bool:
        return os.path.lexists(self._record_path(session_id))

    def _forget_unlocked(self, session_id: str) -> bool:
        self._require_writable()
        check_state_marker(self.root)
        if not UUID4.fullmatch(session_id):
            raise SessionError(f"session_id {session_id!r} is not a UUIDv4")
        return state.remove_private(self._record_path(session_id))

    def converge_pending_forks(self, managed_session_id: str) -> bool:
        """Drop pending forks that hold resume authority; True when changed."""
        self._require_writable()

        lock = self.lifecycle_lock(managed_session_id)
        lock.acquire(blocking=True)
        try:
            raw, _raw_bytes = self.load_raw(managed_session_id)
            updated = drop_resolved_pending_forks(_lifecycle_view(raw))
            if updated is None:
                return False
            self._save_lifecycle(_patch_lifecycle(raw, updated))
            return True
        finally:
            lock.release()

    def resolve_fork(
        self, managed_session_id: str, fork_runtime_id: str
    ) -> dict[str, Any]:
        """Discard one pending fork marker (metadata-only; transcript stays).

        The fork's transcript remains on disk as a native session and can be
        adopted later with `sessions link`. Refuses when the fork holds the
        resume authority (that case converges via `converge_pending_forks`)
        or when the id is not actually pending.
        """
        self._require_writable()

        if not UUID4.fullmatch(fork_runtime_id):
            raise SessionError(
                f"fork runtime id {fork_runtime_id!r} is not a UUIDv4"
            )
        lock = self.lifecycle_lock(managed_session_id)
        lock.acquire(blocking=True)
        try:
            raw, _raw_bytes = self.load_raw(managed_session_id)
            current = _lifecycle_view(raw)
            pending = current.get("pending_forks", [])
            problem = fork_resolution_problem(current, managed_session_id, fork_runtime_id)
            if problem is not None:
                raise SessionError(problem)
            kept = [
                dict(item)
                for item in pending
                if item.get("session_id") != fork_runtime_id
            ]
            updated = {
                **current,
                "pending_forks": kept,
                # Same revocation as adoption: the discarded fork's baked
                # epoch goes stale, so its later hooks cannot claim the
                # parent's authority. A still-running parent app reconciles
                # on its next launcher resume.
                "launch_epoch": current.get("launch_epoch", 0) + 1,
            }
            if current.get("identity_state") != IDENTITY_REPAIR_NEEDED:
                updated["identity_state"] = _derived_identity_state(updated)
            self._save_lifecycle(_patch_lifecycle(raw, updated))
            return updated
        finally:
            lock.release()

    def forget(self, session_id: str) -> bool:
        """Remove one record under its lifecycle lock."""
        self._require_writable()

        lock = self.lifecycle_lock(session_id)
        lock.acquire(blocking=True)
        try:
            return self._forget_unlocked(session_id)
        finally:
            lock.release()

    def forget_session(
        self,
        stable_id: str,
        *,
        pre_delete_check: Callable[[dict[str, Any] | None], str | None] | None = None,
    ) -> tuple[bool, bool]:
        """Serialize generated-scope, record, and pointer removal.

        ``pre_delete_check`` runs under the lifecycle lock, after the fresh
        load and before anything is removed: it receives the freshly loaded
        record (``None`` when corrupt) and returns a refusal message or
        ``None``. Liveness decided outside the lock would be stale by the
        time deletion happens.

        Cleanup order: scope -> both cwd pointers -> the legacy backup
        ``<id>.v3.json`` -> pointer backups naming the id ->
        ``notice/<rid>.seen`` for the runtime id and every alias -> the
        lineup log -> the record, last.
        """
        self._require_writable()

        from . import lineup_log, scope

        lock = self.lifecycle_lock(stable_id)
        lock.acquire(blocking=True)
        try:
            if not self.exists(stable_id):
                return False, False
            try:
                current = self.load(stable_id)
            except SessionError:
                current = None
            if pre_delete_check is not None:
                refusal = pre_delete_check(current)
                if refusal is not None:
                    raise SessionError(refusal)
            if current is None:
                # Corrupt record: the operator's remedy must
                # not dead-end on the same load that failed. Skip the
                # load-dependent cleanup and sweep pointers by id instead.
                scope_removed = scope.remove_scope(self.root, stable_id)
                self._sweep_pointers_for(stable_id)
                state.remove_private(self.backup_path(stable_id))
                self._sweep_pointer_backups_for(stable_id)
                lineup_log.remove(self.root, stable_id)
                return self._forget_unlocked(stable_id), scope_removed
            # Validate/remove generated state first. The record is the final
            # irreversible delete so a scope-safety failure remains repairable.
            scope_removed = scope.remove_scope(self.root, stable_id)
            self.clear_last(current["cwd"], stable_id, blocking=True)
            state.remove_private(self.backup_path(stable_id))
            self._sweep_pointer_backups_for(stable_id)
            self._remove_seen_markers(
                [current["runtime_session_id"]]
                + [item["session_id"] for item in current.get("runtime_aliases", [])]
            )
            lineup_log.remove(self.root, stable_id)
            removed = self._forget_unlocked(stable_id)
            return removed, scope_removed
        finally:
            lock.release()

    def _remove_seen_markers(self, runtime_ids: list[str]) -> None:
        notice_dir = self.root / NOTICE_DIR
        if not os.path.lexists(notice_dir):
            return
        for runtime_id in runtime_ids:
            if UUID4.fullmatch(runtime_id):
                state.remove_private(notice_dir / f"{runtime_id}{SEEN_SUFFIX}")

    def _sweep_pointer_backups_for(self, session_id: str) -> int:
        """Remove every ``*.json.v3`` pointer backup whose payload names session_id.

        Each is re-read under the FileLock of the pointer it backs up
        (the backup name without ``.v3``) before it is removed.
        """

        swept = 0
        for backup in sorted(self.pointers_dir.glob(f"*.json{POINTER_BACKUP_SUFFIX}")):
            if not self._pointer_names(backup, session_id):
                continue
            pointer = backup.with_name(backup.name[: -len(POINTER_BACKUP_SUFFIX)])
            lock = state.FileLock(pointer)
            lock.acquire(blocking=True)
            try:
                if self._pointer_names(backup, session_id):
                    state.remove_private(backup)
                    swept += 1
            finally:
                lock.release()
        return swept

    def _sweep_pointers_for(self, session_id: str) -> int:
        """Remove every last-session pointer naming session_id (load-free)."""

        swept = 0
        for pointer in self.pointers_dir.glob("*.json"):
            if pointer.is_symlink() or not pointer.is_file():
                continue
            try:
                payload = strict_json.loads(state.read_private(pointer))
            except (state.StateError, ValueError):
                continue
            if payload.get("session_id") != session_id:
                continue
            lock = state.FileLock(pointer)
            lock.acquire(blocking=True)
            try:
                # Re-read under the lock: only delete when it still matches.
                try:
                    current = strict_json.loads(state.read_private(pointer))
                except (state.StateError, ValueError):
                    continue
                if current.get("session_id") == session_id:
                    # Durable delete (review): the record removal fsyncs a
                    # DIFFERENT directory — without remove_private the
                    # pointer unlink can be lost while the record stays gone.
                    state.remove_private(pointer)
                    swept += 1
            finally:
                lock.release()
        return swept

    def link(self, record: dict[str, Any]) -> Path:
        """Failure-atomically adopt a native runtime and resolve its parent.

        ``record`` (the adopted session) may be v3 or v4; each parent that
        lists its runtime as a pending fork is patched in its own version
        with the epoch bump that revokes the fork.
        """
        self._require_writable()

        stable = managed_id(record)
        runtime_id = runtime_session_id(record)
        index_lock = self.runtime_index_lock()
        new_lock = self.lifecycle_lock(stable)
        parent_locks: list[state.FileLock] = []
        parent_updates: list[tuple[str, bytes, dict[str, Any]]] = []
        applied_parents: list[tuple[str, bytes]] = []
        pointer = self._pointer_path(record["cwd"])
        pointer_lock = state.FileLock(pointer)
        prior_pointer: bytes | None = None
        pointer_locked = False
        linked = False
        index_lock.acquire(blocking=True)
        try:
            new_lock.acquire(blocking=True)
            if self.exists(stable):
                raise SessionError(f"session {stable} is already managed")
            try:
                existing = self.resolve(runtime_id)
            except SessionError as exc:
                if "no managed session matches" not in str(exc):
                    raise
            else:
                raise SessionError(
                    f"runtime session {runtime_id} is already managed as "
                    f"{managed_id(existing)}"
                )

            # Collect every matching parent while holding its lifecycle lock.
            # Unrelated corrupt records are ignored rather than making adoption
            # partially succeed after the new owner is written.
            for path in self.scan_uuid_records():
                parent_id = path.stem
                if parent_id == stable:
                    continue
                parent_lock = self.lifecycle_lock(parent_id)
                parent_lock.acquire(blocking=True)
                try:
                    try:
                        raw, prior_bytes = self.load_raw(parent_id)
                    except SessionError:
                        parent_lock.release()
                        continue
                    current = _lifecycle_view(raw)
                    pending = current.get("pending_forks", [])
                    filtered = [
                        dict(item)
                        for item in pending
                        if item.get("session_id") != runtime_id
                    ]
                    if len(filtered) == len(pending):
                        parent_lock.release()
                        continue
                    updated = {
                        **current,
                        "pending_forks": filtered,
                        # Revoke the adopted fork's baked credential: its scope
                        # carries the parent's managed-id + this epoch, so its
                        # later hooks could otherwise migrate the parent's
                        # authority into the fork's lineage. A
                        # still-running parent app's hooks go stale until its
                        # next launcher resume — the relink-runtime trade-off.
                        "launch_epoch": current.get("launch_epoch", 0) + 1,
                    }
                    if current.get("identity_state") != IDENTITY_REPAIR_NEEDED:
                        updated["identity_state"] = _derived_identity_state(updated)
                    parent_locks.append(parent_lock)
                    parent_updates.append(
                        (parent_id, prior_bytes, _patch_lifecycle(raw, updated))
                    )
                except BaseException:
                    parent_lock.release()
                    raise

            pointer_lock.acquire(blocking=True)
            pointer_locked = True
            prior_pointer = (
                state.read_private(pointer) if os.path.lexists(pointer) else None
            )
            try:
                for parent_id, prior_bytes, document in parent_updates:
                    try:
                        self._save_lifecycle(document)
                    except state.CommittedStateError:
                        if self.read_record_bytes(parent_id) == strict_json.canonical_file_bytes(
                            document
                        ):
                            applied_parents.append((parent_id, prior_bytes))
                        raise
                    else:
                        applied_parents.append((parent_id, prior_bytes))
                try:
                    linked_path = self.save(record)
                except state.CommittedStateError:
                    linked = self.read_record_bytes(stable) == strict_json.canonical_file_bytes(
                        record
                    )
                    raise
                linked = True
                payload = {"cwd": record["cwd"], "session_id": stable}
                state.atomic_write(pointer, strict_json.canonical_file_bytes(payload))
                return linked_path
            except BaseException:
                if linked:
                    self._forget_unlocked(stable)
                for parent_id, prior_bytes in reversed(applied_parents):
                    self.restore_record_bytes(parent_id, prior_bytes)
                try:
                    if prior_pointer is None:
                        state.remove_private(pointer)
                    else:
                        state.atomic_write(pointer, prior_pointer)
                except OSError:
                    pass
                raise
        finally:
            if pointer_locked:
                pointer_lock.release()
            for parent_lock in reversed(parent_locks):
                parent_lock.release()
            new_lock.release()
            index_lock.release()

    # v4-only record writes ----------------------------------------------

    def set_title(self, managed_session_id: str, title: str | None) -> dict[str, Any]:
        """Set or clear a v4 record's display title (the TUI rename).

        A launcher write (shared migration hold, then the lifecycle lock).
        The mutation token and ``applied_hash`` stay unchanged, so a title
        write never invalidates a prepared launch; ``perform_launch`` copies
        the title from its locked read instead.
        """
        self._require_writable()

        if title is not None:
            if not isinstance(title, str):
                raise SessionError("session title must be a string")
            title = title.strip()
            if not 1 <= len(title) <= 80 or not title.isprintable():
                raise SessionError(
                    "session title must be 1-80 printable characters on one line"
                )
        with launcher_write_guard(self.root):
            lock = self.lifecycle_lock(managed_session_id)
            lock.acquire(blocking=True)
            try:
                raw, _raw_bytes = self.load_raw(managed_session_id)
                _require_v4(raw)
                document = copy.deepcopy(raw)
                if title is None:
                    document.pop("title", None)
                else:
                    document["title"] = title
                self.save(document)
                return document
            finally:
                lock.release()

    def record_lead_switch(
        self,
        managed_session_id: str,
        *,
        observed_runtime_id: str,
        launch_epoch: int | None,
        row: Mapping[str, Any],
    ) -> str:
        """Record a user-source ``/model`` switch onto a lead-set row (§10.3).

        Returns ``"none"`` (nothing recorded), ``"switched"`` or ``"pinned"``
        (the record followed its profile and now keeps this lead). A
        hook write: lifecycle lock only (no runtime-index lock, no migration
        hold), the mutation token unchanged; ``applied_hash`` changes,
        so a resume prepared before the switch fails its CAS. ``lead-set.json``,
        ``launch_fence`` and ``scope_lead`` are never touched.
        """
        self._require_writable()

        lock = self.lifecycle_lock(managed_session_id)
        lock.acquire(blocking=True)
        try:
            raw, _raw_bytes = self.load_raw(managed_session_id)
            if raw.get("version") != RECORD_VERSION or raw["lineup_generation"] == 0:
                return "none"
            view = _lifecycle_view(raw)
            observed_epoch = 0 if launch_epoch is None else launch_epoch
            if observed_epoch != view["launch_epoch"]:
                return "none"  # a revoked fork or a stale process never moves the lead
            if observed_runtime_id != view["runtime_session_id"]:
                return "none"  # a fork shares scope and epoch; only the authority moves it
            lead = view["applied"]["lead"]
            new_lead = {
                **lead,
                "key": row["key"],
                "selector": row["selector"],
                "effort": row.get("effort") or lead["effort"],
                "generation": lead["generation"] if row["key"] == lead["key"] else None,
            }
            if new_lead == lead:
                return "none"
            updated = with_applied(view, {**view["applied"], "lead": new_lead})
            if updated.get("observed_model") == new_lead["selector"]:
                updated.pop("observed_model")
                updated["identity_state"] = _derived_identity_state(updated)
            target = updated.pop("lead_target", None)
            outcome = "switched"
            if updated["follow"]:
                if target is None or (target["key"], target["effort"]) != (
                    new_lead["key"],
                    new_lead["effort"],
                ):
                    # The lead is now a pinned edit: the next resume must
                    # not re-apply the profile's lead over it.
                    updated["follow"] = False
                    outcome = "pinned"
            self._save_lifecycle(
                _patch_lifecycle(
                    raw, updated, ("applied", "applied_hash", "follow", "lead_target")
                )
            )
            return outcome
        finally:
            lock.release()

    # Exact-byte pre-read/restore (action-aware execve cleanup) -----------

    def runtime_index_lock(self) -> state.FileLock:
        """Serialize mutations that can claim a native runtime UUID."""
        self._require_writable()

        check_state_marker(self.root)
        locks_dir = state.ensure_private_dir(self.root / "locks")
        return state.FileLock(locks_dir / "runtime-index")

    def lifecycle_lock(self, session_id: str) -> state.FileLock:
        """Per-session lifecycle lock serializing record/scope mutation.

        Held by launch (scope write + record save + pointer) and transition
        (staging/swap/save) so concurrent launchers on the same UUID cannot
        interleave mutations. Launch keeps it through ``execve``: the fd is
        ``O_CLOEXEC``, so exec drops it; cleanup paths re-acquire and use
        compare-and-restore so a newer launch always wins.
        """
        self._require_writable()

        if not UUID4.fullmatch(session_id):
            raise SessionError(f"session_id {session_id!r} is not a UUIDv4")
        check_state_marker(self.root)
        locks_dir = state.ensure_private_dir(self.root / "locks")
        return state.FileLock(locks_dir / f"{session_id}.lifecycle")

    def read_record_bytes(self, session_id: str) -> bytes | None:
        """Exact on-disk record bytes; None when no record exists.

        Used by the launch path to capture the pre-launch record so an
        execve failure can restore it byte-for-byte (never regenerated).
        """

        path = self._record_path(session_id)
        if not os.path.lexists(path):
            return None
        return state.read_private(path)

    def restore_record_bytes(self, session_id: str, data: bytes) -> None:
        """Restore exact pre-read record bytes (never regenerated content)."""
        self._require_writable()

        check_state_marker(self.root)
        state.atomic_write(self._record_path(session_id), data)

    # Per-CWD last-session pointers ----------------------------------------
    #
    # One ``<d>.json`` per cwd, payload ``{"cwd", "session_id"}``. The
    # launcher also reads the legacy ``<d>.ordinary.json`` (merged by
    # ``last()``) and writes only ``<d>.json``. Every single-file write or
    # delete gates on that file's own payload (``_pointer_names``); the merged ``last()``
    # is for ``-c``, the remembered profile and listings only.

    def _pointer_path(self, cwd: str) -> Path:
        digest = strict_json.sha256_hex(cwd.encode("utf-8"))[:32]
        return self.pointers_dir / f"{digest}.json"

    def _legacy_pointer_path(self, cwd: str) -> Path:
        digest = strict_json.sha256_hex(cwd.encode("utf-8"))[:32]
        return self.pointers_dir / f"{digest}.ordinary.json"

    @staticmethod
    def _pointer_session(path: Path) -> str | None:
        """The session id a pointer file names; None when missing/unreadable/invalid."""

        if not os.path.lexists(path):
            return None
        try:
            payload = strict_json.loads(state.read_private(path))
        except (state.StateError, strict_json.StrictJSONError, OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not UUID4.fullmatch(session_id):
            return None
        return session_id

    def _pointer_names(self, path: Path, session_id: str) -> bool:
        """Per-file payload check: this pointer file names ``session_id``."""

        return self._pointer_session(Path(path)) == session_id

    def read_pointer_bytes(self, cwd: str) -> bytes | None:
        """Exact on-disk ``<d>.json`` bytes; None when no pointer exists."""

        pointer = self._pointer_path(cwd)
        if not os.path.lexists(pointer):
            return None
        return state.read_private(pointer)

    def restore_pointer_bytes(
        self, cwd: str, session_id: str, data: bytes | None
    ) -> bool:
        """Compare-and-restore ``<d>.json`` after a failed launch.

        Restores only while the file still names ``session_id`` (this
        launch's own write), gated on that file's payload, never on the
        merged ``last()``.
        """
        self._require_writable()

        check_state_marker(self.root)
        if not UUID4.fullmatch(session_id):
            raise SessionError(f"session_id {session_id!r} is not a UUIDv4")
        pointer = self._pointer_path(cwd)
        lock = state.FileLock(pointer)
        if not lock.acquire(blocking=False):
            return False
        try:
            if not self._pointer_names(pointer, session_id):
                return False
            if data is None:
                return state.remove_private(pointer)
            state.atomic_write(pointer, data)
            return True
        finally:
            lock.release()

    def update_last(self, cwd: str, session_id: str, *, blocking: bool = False) -> bool:
        """Record the per-CWD last session (``<d>.json`` only, no session_type)."""
        self._require_writable()

        check_state_marker(self.root)
        if not UUID4.fullmatch(session_id):
            raise SessionError(f"session_id {session_id!r} is not a UUIDv4")
        pointer = self._pointer_path(cwd)
        lock = state.FileLock(pointer)
        if not lock.acquire(blocking=blocking):
            return False
        try:
            payload = {"cwd": cwd, "session_id": session_id}
            state.atomic_write(pointer, strict_json.canonical_file_bytes(payload))
            return True
        finally:
            lock.release()

    def last(self, cwd: str) -> str | None:
        """The remembered session of ``cwd`` (merged read).

        Both ``<d>.json`` and the legacy ``<d>.ordinary.json`` are read
        (missing, unreadable, non-object or invalid ids are ignored; a legacy
        payload ``session_type`` is ignored). Two different candidates: the
        one whose record has the newer ``last_seen_at`` wins (a record that
        fails to load loses); a tie goes to ``<d>.json``.
        """

        primary = self._pointer_session(self._pointer_path(cwd))
        legacy = self._pointer_session(self._legacy_pointer_path(cwd))
        if primary is None or legacy is None or primary == legacy:
            return primary or legacy

        def seen(session_id: str) -> str | None:
            try:
                return self.load(session_id).get("last_seen_at")
            except (SessionError, state.StateError, OSError):
                return None

        primary_seen, legacy_seen = seen(primary), seen(legacy)
        if legacy_seen is not None and (primary_seen is None or legacy_seen > primary_seen):
            return legacy
        return primary

    def clear_last(self, cwd: str, session_id: str, *, blocking: bool = False) -> bool:
        """Clear each of ``<d>.json``/``<d>.ordinary.json`` that names session_id.

        Each file is checked and removed under its own FileLock; True when
        at least one pointer was removed.
        """
        self._require_writable()

        check_state_marker(self.root)
        if not UUID4.fullmatch(session_id):
            raise SessionError(f"session_id {session_id!r} is not a UUIDv4")
        removed = False
        legacy = self._legacy_pointer_path(cwd)
        pointers = [self._pointer_path(cwd)]
        if os.path.lexists(legacy):  # never create a lock for a legacy name the launcher never writes
            pointers.append(legacy)
        for pointer in pointers:
            lock = state.FileLock(pointer)
            if not lock.acquire(blocking=blocking):
                continue
            try:
                if self._pointer_names(pointer, session_id) and state.remove_private(pointer):
                    removed = True
            finally:
                lock.release()
        return removed
