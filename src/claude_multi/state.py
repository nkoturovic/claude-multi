"""Owner-controlled, symlink-safe state primitives for claude-multi v2.

All launcher writes go through this module: parent directories must be owned
and group/other-inaccessible, targets must be regular files (never symlinks),
writes use a same-directory temporary file with mode 0600, fsync of file and
directory, and an atomic replace. Advisory sibling locks coordinate concurrent
launchers.

Crash-window contract: an OS or process hard crash after the temporary file
is fsynced but before ``os.replace`` may leave a same-directory
``.<name>.*.tmp`` stale file (mode 0600, owner-only). The previous target
content remains intact and no partial target is ever visible; the stale temp
file is harmless and may be removed by the owner. No automatic reaper is
provided. Exceptions before replacement clean up the temporary file and leave
the previous target intact. A directory-fsync failure after replacement raises
``CommittedStateError`` so callers know the new bytes may already be visible
and can perform transaction-specific recovery.

The POSIX primitives (directory fsync, the single ``os.replace``, ``flock``,
ownership and mode bits) live in ``platform.posix_fs``; the checks,
error types, messages and transaction order stay here.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Mapping

from . import errors
from .platform import posix_fs


class StateError(errors.ClaudeMultiError, OSError):
    """Raised when a state path or write violates safety requirements."""


class CommittedStateError(StateError):
    """The target was replaced, but directory durability was not confirmed."""


# The filesystem failures an ordinary command reports with a remedy
# instead of a traceback: what each means and the one thing to do.
FILESYSTEM_FAILURES = {
    errno.ENOSPC: ("no space is left on the device", "free some space, then run the command again"),
    errno.EDQUOT: ("the disk quota is exhausted", "free some space, then run the command again"),
    errno.EACCES: ("permission was denied", "check that you own the folder and can write to it"),
    errno.EPERM: ("the operation is not permitted", "check that you own the folder and can write to it"),
    errno.EROFS: ("the file system is read-only", "run claude-multi where its folders are writable"),
}


def filesystem_failure(exc: BaseException) -> tuple[str, str] | None:
    """``(what happened, remedy)`` for an ``OSError`` an ordinary command
    reports plainly (no space, a quota, no permission, a read-only file
    system), or None. A :class:`CommittedStateError` also says the change
    may already be visible: it never claims that nothing changed."""

    if not isinstance(exc, OSError) or exc.errno not in FILESYSTEM_FAILURES:
        return None
    what, remedy = FILESYSTEM_FAILURES[exc.errno]
    if isinstance(exc, CommittedStateError):
        what += " (the change was written but may not be durable yet)"
    return what, remedy


def failure_location(exc: BaseException, environ: Mapping[str, str]) -> str:
    """`` at <path>`` (home-relative) for an error that names a file, else ``""``."""

    from . import paths

    filename = getattr(exc, "filename", None)
    return f" at {paths.display(filename, environ)}" if isinstance(filename, (str, os.PathLike)) else ""


def failure_text(exc: BaseException, environ: Mapping[str, str], *, own_message: bool = True) -> str | None:
    """The text every command shows for a filesystem failure
    (:func:`filesystem_failure`), as ``<what>\\n  fix: <remedy>``, or None
    for any other error. One of claude-multi's own errors keeps its message
    and its own remedy (``own_message``); any other one says what happened
    and where, home-relative. Never a traceback, never an errno."""

    found = filesystem_failure(exc)
    if found is None:
        return None
    what, remedy = found
    if own_message and isinstance(exc, errors.ClaudeMultiError):
        return f"{exc}\n  fix: {exc.remedy or remedy}"
    return f"{what}{failure_location(exc, environ)}\n  fix: {remedy}"


SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def check_name(name: str) -> str:
    """Validate a state file name (composition, session, draft); no traversal."""

    if not SAFE_NAME.fullmatch(name):
        raise StateError(
            errno.EINVAL,
            f"unsafe state name {name!r}: must match {SAFE_NAME.pattern}",
        )
    return name


def _check_directory(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise StateError(errno.ENOENT, f"missing state directory {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise StateError(errno.ELOOP, f"state directory {path} is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise StateError(errno.ENOTDIR, f"state directory {path} is not a directory")
    if not posix_fs.owned_by_caller(info):
        raise StateError(errno.EPERM, f"state directory {path} is not owner-controlled")
    if posix_fs.shared_mode_bits(info):
        raise StateError(
            errno.EPERM,
            f"state directory {path} must not be accessible by group/other",
        )


def _check_regular_file(path: Path) -> None:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise StateError(errno.ELOOP, f"state file {path} is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise StateError(errno.EINVAL, f"state file {path} is not a regular file")
    if not posix_fs.owned_by_caller(info):
        raise StateError(errno.EPERM, f"state file {path} is not owner-controlled")


def ensure_private_dir(path: Path | str) -> Path:
    """Create (if missing) and validate a private 0700 state directory.

    Every directory newly created by this call — the leaf and any missing
    parents — is hardened to mode 0700, so a child-first creation can never
    leave a group/other-accessible parent behind.
    """

    candidate = Path(path)
    if candidate.exists() or candidate.is_symlink():
        _check_directory(candidate)
        return candidate
    missing: list[Path] = []
    probe = candidate
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    # parents=True uses 0777 for intermediate directories. Create each one
    # explicitly private, including while it is being created (default ACLs
    # can override umask). Never chmod a directory won by another creator.
    for created in reversed(missing):
        try:
            created.mkdir(mode=0o700)
        except FileExistsError:
            _check_directory(created)
        else:
            os.chmod(created, 0o700)
    _check_directory(candidate)
    return candidate


# The raising variant; callers here and in retention look it up by this name.
_fsync_directory = posix_fs.fsync_directory


def atomic_write(path: Path | str, data: bytes) -> None:
    """Write data atomically: same-dir temp, mode 0600, fsync, replace.

    On ordinary exceptions the temporary file is removed and the previous
    target (if any) is preserved. See the module docstring for the documented
    hard-crash window that may leave a harmless stale temp file.
    """

    target = Path(path)
    if target.name in ("", ".", ".."):
        raise StateError(errno.EINVAL, f"unsafe state path {target}")
    parent = target.parent
    _check_directory(parent)
    if os.path.lexists(target):
        _check_regular_file(target)

    descriptor, temporary = tempfile.mkstemp(
        dir=parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        posix_fs.replace_file(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    try:
        _fsync_directory(parent)
    except OSError as exc:
        raise CommittedStateError(
            exc.errno or errno.EIO,
            f"state file {target} was replaced but directory fsync failed: {exc}",
        ) from exc


def read_private(path: Path | str) -> bytes:
    """Read a state file after regular-file, ownership, and mode checks."""

    target = Path(path)
    try:
        _check_regular_file(target)
    except FileNotFoundError as exc:
        raise StateError(errno.ENOENT, f"missing state file {target}") from exc
    info = os.lstat(target)
    if posix_fs.shared_mode_bits(info):
        raise StateError(
            errno.EPERM,
            f"state file {target} must not be accessible by group/other",
        )
    return target.read_bytes()


def remove_private(path: Path | str) -> bool:
    """Remove a private regular state file and durably fsync its directory."""

    target = Path(path)
    _check_directory(target.parent)
    if not os.path.lexists(target):
        return False
    _check_regular_file(target)
    os.unlink(target)
    _fsync_directory(target.parent)
    return True


class FileLock:
    """Advisory lock on a sibling ``<target>.lock`` file.

    Exclusive (``LOCK_EX``) by default; ``shared=True`` takes ``LOCK_SH``
    instead (every launcher writer holds the global migration lock
    shared, ``migrate``/``restore-2x`` hold it exclusively). A lock is not
    re-entrant: a second ``FileLock`` on the same target opens a second fd,
    which conflicts with the first exactly like another process would.
    """

    def __init__(self, target: Path | str, *, shared: bool = False):
        target = Path(target)
        self._lock_path = target.with_name(target.name + ".lock")
        self._descriptor: int | None = None
        self._shared = bool(shared)

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def shared(self) -> bool:
        return self._shared

    def acquire(self, blocking: bool = True) -> bool:
        if self._descriptor is not None:
            raise StateError(errno.EINVAL, "lock already held")
        _check_directory(self._lock_path.parent)
        if os.path.lexists(self._lock_path):
            _check_regular_file(self._lock_path)
        descriptor = os.open(
            self._lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600
        )
        try:
            os.fchmod(descriptor, 0o600)
            posix_fs.lock_descriptor(descriptor, shared=self._shared, blocking=blocking)
        except BlockingIOError:
            os.close(descriptor)
            return False
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return True

    def release(self) -> None:
        if self._descriptor is None:
            return
        try:
            posix_fs.unlock_descriptor(self._descriptor)
        finally:
            os.close(self._descriptor)
            self._descriptor = None

    def identity(self) -> tuple[int, int] | None:
        """``(st_dev, st_ino)`` of the lock file this lock holds (None when
        it is not held): the file, whatever its name points to now."""

        if self._descriptor is None:
            return None
        info = os.fstat(self._descriptor)
        return info.st_dev, info.st_ino

    def __enter__(self) -> "FileLock":
        self.acquire(blocking=True)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


# ------------------------------------------------------ served-change barrier
# One gateway-scoped mutation
# barrier, ``<HOME>/.config/claude-multi/served-change.lock`` (HOME-relative
# like the api-key leaf, so every XDG state root shares it). Lock order:
# migration guard -> barrier -> token rotation (non-blocking) -> store and
# lifecycle locks -> Settings -> api-key leaf. One acquisition per commit
# phase: inner services take the held :class:`BarrierToken` and assert it
# (:func:`require_barrier`); they never acquire. A second acquisition by the
# thread that holds it is refused at once (it would deadlock on its own fd:
# FileLock is not re-entrant); another thread waits like another process
# (flock locks belong to the open file description). Hooks, unit/init and
# rotation renders never take it.

SERVED_BARRIER_NAME = "served-change"
SERVED_BARRIER_TIMEOUT = 10.0
BARRIER_BUSY_TEXT = "another gateway operation is in progress"


class BarrierBusyError(StateError):
    """The barrier stayed held elsewhere for the whole bounded wait."""

    def __init__(self, message: str = BARRIER_BUSY_TEXT):
        super().__init__(errno.EAGAIN, message)


class BarrierTokenError(StateError):
    """An inner service ran without the held barrier token (or re-acquired it)."""

    def __init__(self, message: str):
        super().__init__(errno.EINVAL, message)


_HELD_LOCK = threading.Lock()
_HELD: dict[tuple[str, int], "BarrierToken"] = {}


class BarrierToken:
    """The held served-change barrier: a capability, never re-acquired.

    ``guard`` is the migration lock the outer phase paired with it (shared
    for launcher writers, exclusive for ``migrate``/``restore-2x``) and
    ``guard_root`` its state root; inner services that would otherwise take
    the migration guard themselves skip it when they receive the token.
    """

    def __init__(self, lock: FileLock, *, guard: FileLock | None = None,
                 guard_root: Path | None = None):
        self._lock = lock
        self._key = (str(lock.lock_path), threading.get_ident())
        self.guard = guard
        self.guard_root = None if guard_root is None else Path(guard_root)
        self._released = False

    @property
    def lock_path(self) -> Path:
        return self._lock.lock_path

    @property
    def held(self) -> bool:
        return not self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with _HELD_LOCK:
            if _HELD.get(self._key) is self:
                del _HELD[self._key]
        self._lock.release()

    def __enter__(self) -> "BarrierToken":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def served_barrier_target(config_dir: Path | str) -> Path:
    """The barrier's FileLock target; the lock file is ``served-change.lock``."""

    return Path(config_dir) / SERVED_BARRIER_NAME


def barrier_held_here(config_dir: Path | str) -> bool:
    """The calling thread holds the barrier of ``config_dir`` (test/diagnostic seam)."""

    lock_path = served_barrier_target(config_dir).with_name(SERVED_BARRIER_NAME + ".lock")
    with _HELD_LOCK:
        token = _HELD.get((str(lock_path), threading.get_ident()))
    return token is not None and token.held


def acquire_served_barrier(
    config_dir: Path | str,
    *,
    timeout: float | None = SERVED_BARRIER_TIMEOUT,
    interval: float = 0.05,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    guard: FileLock | None = None,
    guard_root: Path | None = None,
) -> BarrierToken:
    """Take the barrier with a bounded wait (``timeout=None``: one attempt).

    Still held elsewhere at the deadline -> :class:`BarrierBusyError` with
    the neutral text. Held already by the calling thread ->
    :class:`BarrierTokenError` immediately (never a self-deadlock).
    """

    directory = ensure_private_dir(Path(config_dir))
    lock = FileLock(served_barrier_target(directory))
    key = (str(lock.lock_path), threading.get_ident())
    with _HELD_LOCK:
        existing = _HELD.get(key)
        if existing is not None and existing.held:
            raise BarrierTokenError(
                "served-change barrier already held by this thread — an inner service must "
                "use the held token, never re-acquire it"
            )
    deadline = None if timeout is None else clock() + max(0.0, float(timeout))
    while True:
        if lock.acquire(blocking=False):
            break
        if deadline is None or clock() >= deadline:
            raise BarrierBusyError()
        sleep(interval)
    token = BarrierToken(lock, guard=guard, guard_root=guard_root)
    with _HELD_LOCK:
        _HELD[key] = token
    return token


def require_barrier(token: object, *, root: Path | str | None = None) -> BarrierToken:
    """Assert ``token`` is a held barrier (paired with ``root``'s migration
    guard when ``root`` is given). Inner services call this, never acquire."""

    if not isinstance(token, BarrierToken) or not token.held:
        raise BarrierTokenError(
            "served-change barrier token required: this inner service runs only inside an "
            "outer served-change phase"
        )
    if root is not None and token.guard_root is not None and Path(root) != token.guard_root:
        raise BarrierTokenError(
            f"served-change barrier token was taken for state root {token.guard_root}, not {root}"
        )
    return token


# ------------------------------------------------------ owned-readable reads
# User-authored declarations (``providers.d``) may be symlinks into a
# read-only store (a system configuration manager's). They are *readable* when the final target
# is a regular file owned by the caller or root and not group/other
# WRITABLE (shared read access is fine); a directory is readable under the
# same rule. Every check runs on the open descriptor (``fstat`` after
# ``open``), never check-then-open, and reads are bounded. These readers
# never write: tool writers keep :func:`atomic_write`, which refuses
# symlinks, so a store-managed file is never replaced.

WRITABLE_BY_OTHERS = 0o022


def readable_owner_uids() -> frozenset[int]:
    """Owners whose files an owned-readable read accepts: the caller and root."""

    return frozenset({os.geteuid(), 0})


def _check_owned_readable(
    info: os.stat_result, path: Path, what: str, trusted: frozenset[int]
) -> None:
    if info.st_uid not in trusted:
        raise StateError(errno.EPERM, f"{what} {path} is not owned by you or root")
    if stat.S_IMODE(info.st_mode) & WRITABLE_BY_OTHERS:
        raise StateError(errno.EPERM, f"{what} {path} is writable by group/other")


def open_owned_readable_dir(
    path: Path | str, *, trusted_uids: frozenset[int] | None = None
) -> tuple[int, bool]:
    """Open a directory for descriptor-relative reads; ``(fd, is_symlink)``.

    A symlinked directory is allowed (and reported, so writers can treat it
    as read-only). Raises ``FileNotFoundError`` when absent and
    :class:`StateError` when unsafe. The caller closes the descriptor.
    """

    target = Path(path)
    trusted = readable_owner_uids() if trusted_uids is None else frozenset(trusted_uids)
    symlinked = os.path.islink(target)
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except NotADirectoryError as exc:
        raise StateError(errno.ENOTDIR, f"directory {target} is not a directory") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise StateError(errno.ENOTDIR, f"directory {target} is not a directory")
        _check_owned_readable(info, target, "directory", trusted)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, symlinked


def read_owned_readable(
    path: Path | str,
    *,
    max_bytes: int,
    dir_fd: int | None = None,
    trusted_uids: frozenset[int] | None = None,
) -> bytes:
    """Bounded read of a regular file (or a symlink to one) owned by the
    caller or root and not group/other writable, checked on the descriptor.

    ``path`` is relative to ``dir_fd`` when one is given. A FIFO or device
    is refused without blocking (``O_NONBLOCK``); more than ``max_bytes``
    raises :class:`StateError` (``EFBIG``). ``FileNotFoundError`` passes
    through for an absent (or dangling) entry.
    """

    target = Path(path)
    trusted = readable_owner_uids() if trusted_uids is None else frozenset(trusted_uids)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOCTTY | os.O_NONBLOCK
    descriptor = os.open(target, flags, dir_fd=dir_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise StateError(errno.EINVAL, f"file {target} is not a regular file")
        _check_owned_readable(info, target, "file", trusted)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise StateError(errno.EFBIG, f"file {target} exceeds {max_bytes} bytes")
        return data
    finally:
        os.close(descriptor)
