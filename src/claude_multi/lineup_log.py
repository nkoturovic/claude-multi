"""The per-session lineup log.

``<state>/lineup-log/<managed_id>.log`` records which scope binding a
``cm-*`` spawn saw (the ``subagent`` hook) and every live lineup apply. It
is observability, never authority: it lives OUTSIDE the scope, so it is
never plan content, never hashed, never drift-checked and never carried
across a scope rewrite (an append can never race a scope swap's removal).

File discipline:

- one JSON object per line, ``strict_json.canonical_bytes(obj) + b"\\n"``,
  at most :data:`LOG_LINE_MAX` bytes;
- appended under an exclusive ``flock`` on an
  ``O_WRONLY|O_APPEND|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK`` fd that must be a
  regular file owned by the euid with mode 0600 (a file this call creates is
  ``fchmod``-ed to 0600); a symlink, directory, FIFO or foreign file is
  refused and never followed;
- bounded: when a line would push the file past
  ``lineup_files.LINEUP_LOG_MAX_BYTES`` it is rotated once to ``.log.1``
  (the previous ``.1`` is dropped) and the line starts a fresh log;
- the ``lineup-log/`` directory is a private 0700 state directory.

Entries that name a binding carry ``"label": binding_label(N)`` ("binding at
gen N (reload unconfirmed)"): the scope binding at that generation, not proof
of the served model (before ``/reload-plugins`` a session keeps its previous
bindings).

This module imports only stdlib, ``state``, ``strict_json``,
``lineup_files`` and ``platform.posix_fs`` (the flock primitive: no module
outside ``platform/`` imports ``fcntl``), so ``hooks.py`` can use it without
the compiler chain. ``forget`` and ``doctor --prune`` use :func:`remove`
(orphans via :func:`log_ids`).
"""

from __future__ import annotations

import errno
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Mapping

from . import errors, state, strict_json
from .platform import posix_fs
from .lineup_files import (
    LINEUP_LOG_DIR,
    LINEUP_LOG_MAX_BYTES,
    LINEUP_LOG_ROTATED_SUFFIX,
    LINEUP_LOG_SUFFIX,
    lineup_log_path,
    lineup_log_rotated_path,
)

LOG_LINE_MAX = 4096
LOG_MODE = 0o600

_BINDING_LABEL = "binding at gen {generation} (reload unconfirmed)"
# A concurrent rotation moves the file between our open and our lock; each
# retry reopens the new file. Bounded so a pathological loop cannot hang a hook.
_MAX_ATTEMPTS = 8


class LineupLogError(errors.ClaudeMultiError, ValueError):
    """A lineup-log input is invalid (managed id, record shape, line size)."""


def check_managed_id(managed_id: str) -> str:
    """Require the canonical lowercase UUIDv4 used by state path names."""

    try:
        parsed = uuid.UUID(managed_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise LineupLogError(f"managed id {managed_id!r} is not a UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != managed_id:
        raise LineupLogError(f"managed id {managed_id!r} is not a UUIDv4")
    return managed_id


def binding_label(generation: int) -> str:
    """``"binding at gen N (reload unconfirmed)"`` for lineup generation ``N``."""

    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise LineupLogError(f"lineup generation must be a positive integer, got {generation!r}")
    return _BINDING_LABEL.format(generation=generation)


def encode_line(record: Mapping[str, Any]) -> bytes:
    """One canonical JSON line (with its newline); ``LineupLogError`` if too large."""

    if not isinstance(record, Mapping):
        raise LineupLogError("a lineup-log record must be a JSON object")
    try:
        line = strict_json.canonical_bytes(dict(record)) + b"\n"
    except (TypeError, ValueError) as exc:
        raise LineupLogError(f"lineup-log record is not JSON-serialisable: {exc}") from exc
    if len(line) > LOG_LINE_MAX:
        raise LineupLogError(
            f"lineup-log line is {len(line)} bytes; the limit is {LOG_LINE_MAX}"
        )
    return line


def _open_log(path: Path) -> int:
    """Open (creating 0600 if absent) a bounded log for append; refuse unsafe files."""

    flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    created = False
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, LOG_MODE)
        created = True
    except FileExistsError:
        # O_NOFOLLOW: a symlink fails with ELOOP; a directory with EISDIR;
        # O_NONBLOCK: a FIFO without a reader fails with ENXIO, never blocks.
        descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise state.StateError(errno.EINVAL, f"log {path} is not a regular file")
        if info.st_uid != os.geteuid():
            raise state.StateError(errno.EPERM, f"log {path} is not owner-controlled")
        if created:
            os.fchmod(descriptor, LOG_MODE)
        elif stat.S_IMODE(info.st_mode) != LOG_MODE:
            raise state.StateError(errno.EPERM, f"log {path} mode is not 0600")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def append_bounded(path: Path, rotated: Path, line: bytes, *, max_bytes: int) -> Path:
    """Append one already-encoded line to a private bounded log; returns ``path``.

    The shared file discipline of the lineup log and the hook error log
    (``<state>/hook-errors.log``): the caller has made the
    parent a private directory; the append runs under an exclusive ``flock``
    on a 0600 regular owner file (symlink, directory, FIFO or foreign file
    refused, never followed); a line that would push the file past
    ``max_bytes`` first rotates it once to ``rotated`` (the previous rotation
    is dropped). Concurrent appenders are serialised; a rotation by another
    appender is detected (the path no longer names the locked inode) and the
    line goes to the fresh log.
    """

    for _attempt in range(_MAX_ATTEMPTS):
        try:
            descriptor = _open_log(path)
        except FileNotFoundError:
            continue  # removed between the O_EXCL attempt and the reopen
        try:
            posix_fs.lock_descriptor(descriptor, shared=False, blocking=True)
            info = os.fstat(descriptor)
            try:
                current = os.lstat(path)
            except FileNotFoundError:
                continue
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                continue  # rotated (or replaced) while we waited for the lock
            if info.st_size and info.st_size + len(line) > max_bytes:
                os.replace(path, rotated)
                _fsync_dir(path.parent)
                continue
            _write_all(descriptor, line)
            return path
        finally:
            os.close(descriptor)
    raise state.StateError(errno.EAGAIN, f"log {path} kept rotating; line not written")


def append(
    state_root: Path | str,
    managed_id: str,
    record: Mapping[str, Any],
    *,
    max_bytes: int = LINEUP_LOG_MAX_BYTES,
) -> Path:
    """Append one record line to the session's lineup log; returns the log path.

    Raises ``LineupLogError`` for invalid input and ``state.StateError`` /
    ``OSError`` for an unsafe or unwritable log; the caller (a hook) decides
    whether that is only a note (:func:`append_bounded` has the discipline).
    """

    check_managed_id(managed_id)
    line = encode_line(record)
    state.ensure_private_dir(Path(state_root) / LINEUP_LOG_DIR)
    return append_bounded(
        lineup_log_path(Path(state_root), managed_id),
        lineup_log_rotated_path(Path(state_root), managed_id),
        line,
        max_bytes=max_bytes,
    )


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def remove(state_root: Path | str, managed_id: str) -> bool:
    """Remove a session's log and its rotation (``forget``/``doctor --prune``).

    Idempotent: False when neither exists. Refuses (``state.StateError``) a
    symlinked or foreign ``lineup-log/`` directory or log file rather than
    following it; never creates the directory.
    """

    check_managed_id(managed_id)
    log_dir = Path(state_root) / LINEUP_LOG_DIR
    if not os.path.lexists(log_dir):
        return False
    removed = False
    for path in (
        lineup_log_path(Path(state_root), managed_id),
        lineup_log_rotated_path(Path(state_root), managed_id),
    ):
        if state.remove_private(path):
            removed = True
    return removed


def log_ids(state_root: Path | str) -> list[str]:
    """Sorted managed ids that have a log or a rotated log (for orphan pruning).

    Read-only; a missing directory is an empty list; names that are not a
    canonical UUIDv4 plus a log suffix are ignored (never removed here).
    """

    log_dir = Path(state_root) / LINEUP_LOG_DIR
    try:
        info = os.lstat(log_dir)
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode):
        raise state.StateError(errno.ENOTDIR, f"lineup-log path {log_dir} is not a directory")
    found: set[str] = set()
    for name in os.listdir(log_dir):
        for suffix in (LINEUP_LOG_ROTATED_SUFFIX, LINEUP_LOG_SUFFIX):
            if name.endswith(suffix):
                stem = name[: -len(suffix)]
                try:
                    found.add(check_managed_id(stem))
                except LineupLogError:
                    pass
                break
    return sorted(found)


__all__ = [
    "LOG_LINE_MAX",
    "LOG_MODE",
    "LineupLogError",
    "append",
    "append_bounded",
    "binding_label",
    "check_managed_id",
    "encode_line",
    "log_ids",
    "remove",
]


# Read side: never mkdir/chmod/lock, and never open a special file or follow a
# final symlink. Reading a raced inode refuses instead of inventing continuity.
def read_regular(path: Path, *, max_bytes: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise OSError(errno.EINVAL, "unsafe or oversized observation file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes or
                (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)):
            raise OSError(errno.EINVAL, "observation file changed")
        chunks, count = [], 0
        while count <= max_bytes:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            chunks.append(chunk)
        if count > max_bytes:
            raise OSError(errno.EFBIG, "oversized observation file")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


from dataclasses import dataclass
from datetime import datetime, timezone
import re


@dataclass(frozen=True)
class BindingObservation:
    timestamp: datetime
    event: str
    generation: int | None
    role: str | None
    agent_id: str | None
    selector: str | None

    @property
    def label(self) -> str:
        return binding_label(self.generation) if self.generation else "binding generation unknown (reload unconfirmed)"


@dataclass(frozen=True)
class BindingLog:
    events: tuple[BindingObservation, ...] = ()
    coverage: str = "unavailable"


def read(state_root: Path | str, managed_id: str) -> BindingLog:
    """Read current + one rotation; partial tails never become bindings."""
    check_managed_id(managed_id)
    events = []
    found = False
    for path in (lineup_log_rotated_path(state_root, managed_id), lineup_log_path(state_root, managed_id)):
        try:
            raw = read_regular(path, max_bytes=LINEUP_LOG_MAX_BYTES)
        except FileNotFoundError:
            continue
        except OSError:
            return BindingLog(tuple(events), "unavailable")
        found = True
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b"\n") or len(line) > LOG_LINE_MAX:
                continue
            try:
                entry = strict_json.loads(line)
                if not isinstance(entry, dict) or entry.get("event") not in {"apply", "subagent-start"}:
                    continue
                timestamp = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
                if timestamp.utcoffset() is None:
                    continue
                gen = re.fullmatch(r"([1-9][0-9]{0,9}) [a-f0-9]{12}", str(entry.get("lineup_gen", "")))
                def field(name):
                    value = entry.get(name)
                    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,255}", value) else None
                events.append(BindingObservation(timestamp.astimezone(timezone.utc), entry["event"],
                              int(gen[1]) if gen else None, field("agent_type"), field("agent_id"), field("scope_selector")))
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                continue
    return BindingLog(tuple(events[-256:]), "partial" if found else "absent")
