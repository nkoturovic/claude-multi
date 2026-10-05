"""Linux process backend: bounded, read-only ``/proc`` and daemon-location reads.

No process environment, no command line beyond the bounded session-flag scan,
and no signals: observations only. Every reader takes ``proc_root`` (and the
other live reads their own seams) so tests use fixture trees. A missing
capability — no ``/proc``, a private PID namespace, no service manager, a
non-Linux kernel — yields unknown (``None``, ``known=False`` or an
``unknown`` verdict), never stopped. It does not import its facades
(``service``, ``sessions``); their policies stay there.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import socket
import stat
import sys
import urllib.parse
from pathlib import Path
from typing import Callable, Iterable, Iterator

from claude_multi.platform import observation

PROC_ROOT = Path("/proc")
SESSION_FLAGS = (b"--session-id", b"--resume")
CMDLINE_LIMIT = 1 << 20
ANCESTOR_WALK_LIMIT = 64
STAT_LIMIT = 4096


def pid_namespace(proc_root: Path | str = PROC_ROOT, *, readlink: Callable | None = None) -> str:
    """This process's PID namespace link (``pid:[N]``); raises ``OSError`` if unreadable."""
    return (readlink or os.readlink)(Path(proc_root) / "self/ns/pid")


def gateway_pid(
    stamp, *, proc_root: Path, readlink: Callable, main_pid: Callable[[], str | None],
) -> tuple[int | None, bool | None]:
    """``(pid, alive)`` from a recorded start (``pid``, ``binary``, ``pid_namespace``).

    A recorded PID counts only while its executable is the recorded binary
    (PID reuse otherwise); ``main_pid`` is the service manager's hint.
    Absence is stopped evidence only in the recorded PID namespace; ``alive``
    is ``None`` whenever that cannot be proven. A manager ``MainPID`` of 0
    is stopped only with independent absence evidence: no recorded start and
    an observable ``/proc`` (the first start), or a recorded start in this
    PID namespace whose process is gone or provably another program.
    """
    raw = None
    absent_pid = None
    same_namespace = False
    recorded_gone = False  # the recorded process is absent or proven reused, in this namespace
    if stamp is not None:
        try:
            same_namespace = stamp.pid_namespace == readlink(proc_root / "self/ns/pid")
        except OSError:
            pass
        try:
            (proc_root / str(stamp.pid)).stat()
        except FileNotFoundError:
            if same_namespace:
                absent_pid = stamp.pid
                recorded_gone = True
        except OSError:
            pass
        try:
            if (proc_root / str(stamp.pid) / "exe").samefile(stamp.binary):
                raw = str(stamp.pid)
            elif same_namespace:
                recorded_gone = True  # PID reused by another program
        except OSError:
            pass
    if raw is None:
        raw = main_pid()
    if raw is None or not raw.isdecimal():
        return (absent_pid, False) if absent_pid is not None else (None, None)
    pid = int(raw)
    if pid == 0:
        if stamp is None:
            try:
                os.listdir(proc_root)  # existence alone does not prove an observable backend
            except OSError:
                return None, None
            return None, False
        return (None, False) if recorded_gone else (None, None)
    try:
        (proc_root / str(pid)).stat()
    except FileNotFoundError:
        return pid, False if same_namespace else None
    except OSError:
        return pid, None
    return pid, True


def listener_owner(
    base_url: str, *, proc_root: Path, euid: int | None, stamp: dict | None,
    main_pid: Callable[[], str | None],
) -> observation.OwnerVerdict:
    """Observe the listening socket uid, then prove MainPID holds its inode.

    No process environments or command lines are read. Unknown is not proof of
    a foreign uid; a foreign matching listener always wins, even if another
    table or the service manager is unreadable.
    """
    if not sys.platform.startswith("linux"):
        return observation.OwnerVerdict("unknown", "listener ownership is unavailable")
    try:
        parts = urllib.parse.urlsplit(base_url)
        host = ipaddress.ip_address(parts.hostname)
        port = parts.port or 80
        uid = os.geteuid() if euid is None else euid
    except (ValueError, TypeError, AttributeError):
        return observation.OwnerVerdict("unknown", "listener ownership is unavailable")
    rows: list[tuple[int, str]] = []
    unreadable = False
    for name, family in (("tcp", socket.AF_INET), ("tcp6", socket.AF_INET6)):
        try:
            lines = (proc_root / "net" / name).read_text().splitlines()[1:]
            for line in lines:
                fields = line.split()
                if len(fields) < 10 or fields[3] != "0A":
                    continue
                address, raw_port = fields[1].split(":")
                if int(raw_port, 16) != port:
                    continue
                raw = bytes.fromhex(address)
                if sys.byteorder == "little":
                    raw = b"".join(raw[i:i+4][::-1] for i in range(0, len(raw), 4))
                bound = ipaddress.ip_address(socket.inet_ntop(family, raw))
                matched = bound == host or bound.is_unspecified
                if isinstance(bound, ipaddress.IPv6Address):
                    matched = matched or bound.ipv4_mapped == host
                if matched:
                    rows.append((int(fields[7]), fields[9]))
        except (OSError, ValueError, IndexError):
            unreadable = True
    for owner, _inode in rows:
        if owner != uid:
            return observation.OwnerVerdict("foreign", f"another user (uid {owner})", uid=owner)
    if not rows:
        return observation.OwnerVerdict("unknown" if unreadable else "none", "listener ownership is unknown")
    raw_pid = stamp.get("pid") if stamp is not None else main_pid()
    try:
        pid = int(raw_pid)
        if pid <= 0:
            raise ValueError
    except (ValueError, TypeError):
        pid = None
    if pid is not None:
        try:
            inodes = set()
            for fd in (proc_root / str(pid) / "fd").iterdir():
                try:
                    inodes.add(os.readlink(fd))
                except FileNotFoundError:
                    continue  # an unrelated fd may close during the snapshot
            if not unreadable and all(f"socket:[{inode}]" in inodes for _, inode in rows):
                return observation.OwnerVerdict("ours", "the claude-multi gateway service", uid, pid)
        except OSError:
            pass
    return observation.OwnerVerdict("unknown", "another process of yours (unit ownership unconfirmed)", uid, pid)


def background_liveness(root: Path | None = None) -> observation.BackgroundLiveness:
    """Metadata-only liveness with explicit uncertainty for destructive callers."""
    getuid = getattr(os, "getuid", None)
    geteuid = getattr(os, "geteuid", None)
    if getuid is None or geteuid is None:
        return observation.BackgroundLiveness(False, frozenset(), "user id is unavailable")
    root = Path(root) if root is not None else Path(f"/tmp/cc-daemon-{getuid()}")
    try:
        info = root.lstat()
    except FileNotFoundError:
        return observation.BackgroundLiveness(True, frozenset())
    except OSError as exc:
        return observation.BackgroundLiveness(False, frozenset(), type(exc).__name__)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != geteuid():
        return observation.BackgroundLiveness(False, frozenset(), "daemon root is not a uid-owned directory")
    prefixes: set[str] = set()
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    if entry.is_symlink():
                        return observation.BackgroundLiveness(False, frozenset(), "symlink in daemon root")
                    continue
                pty = Path(entry.path) / "pty"
                try:
                    info = pty.lstat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != geteuid():
                        return observation.BackgroundLiveness(False, frozenset(), "unsafe daemon pty directory")
                    with os.scandir(pty) as sockets:
                        prefixes.update(sock.name[:-5] for sock in sockets
                                        if sock.name.endswith(".sock") and sock.name[:-5])
                except FileNotFoundError:
                    continue
    except OSError as exc:
        return observation.BackgroundLiveness(False, frozenset(), type(exc).__name__)
    return observation.BackgroundLiveness(True, frozenset(prefixes))


def ancestor_pids(proc_root: Path | str = "/proc", pid: int | None = None) -> frozenset[int]:
    """``pid`` (default: this process) and its ancestors, from ``/proc/<pid>/stat``.

    Walks field 4 (ppid) of each ``stat`` line, parsed after the last ``)``
    so a ``comm`` with spaces or parentheses cannot shift it. Bounded to 64
    steps; any error, a pid <= 0 or a pid seen before ends the walk.
    """

    root = Path(proc_root)
    current = os.getpid() if pid is None else pid
    seen: set[int] = set()
    for _step in range(ANCESTOR_WALK_LIMIT):
        if current <= 0 or current in seen:
            break
        seen.add(current)
        try:
            descriptor = os.open(root / str(current) / "stat", os.O_RDONLY | os.O_CLOEXEC)
            try:
                raw = os.read(descriptor, STAT_LIMIT)
            finally:
                os.close(descriptor)
            current = int(raw[raw.rindex(b")") + 1 :].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return frozenset(seen)


def session_argv_paths(
    proc_root: Path | str, *, exclude_pids: Iterable[int] = (), accept: Callable[[str], object],
) -> Iterator[tuple[str, Path]]:
    """Ids named after ``--session-id``/``--resume`` in ``/proc/*/cmdline``, with the process dir.

    Bounded (at most 1 MiB per cmdline); an unreadable table or process is
    skipped. ``accept`` is the caller's id shape (the facade's UUID check).
    """

    root = Path(proc_root)
    try:
        entries = os.listdir(root)
    except OSError:
        return
    yield from _scan_entries(root, entries, exclude_pids=exclude_pids, accept=accept)


# A process that exited between the enumeration and the read: gone, not unknown.
_VANISHED = frozenset({errno.ENOENT, errno.ESRCH})


def _scan_entries(
    root: Path, entries: Iterable[str], *, exclude_pids: Iterable[int], accept: Callable[[str], object],
    unreadable: list[str] | None = None,
) -> Iterator[tuple[str, Path]]:
    """The bounded argv scan over one enumeration of the process table.

    A process that vanished is skipped. Any other unreadable argv is
    recorded in ``unreadable`` when given (the destructive scan: that
    process may be a client naming the session), else skipped (displays).
    """
    excluded = frozenset(exclude_pids)
    for name in entries:
        if not name.isdigit():
            continue
        if excluded and int(name) in excluded:
            continue
        try:
            descriptor = os.open(root / name / "cmdline", os.O_RDONLY | os.O_CLOEXEC)
        except OSError as exc:
            if unreadable is not None and exc.errno not in _VANISHED:
                unreadable.append(f"{type(exc).__name__} on process {name}")
            continue
        try:
            raw = os.read(descriptor, CMDLINE_LIMIT)
        except OSError as exc:
            if unreadable is not None and exc.errno not in _VANISHED:
                unreadable.append(f"{type(exc).__name__} on process {name}")
            continue
        finally:
            os.close(descriptor)
        args = raw.split(b"\0")
        for index, arg in enumerate(args):
            candidate = None
            if arg in SESSION_FLAGS and index + 1 < len(args):
                candidate = args[index + 1]
            else:
                for flag in SESSION_FLAGS:
                    if arg.startswith(flag + b"="):
                        candidate = arg[len(flag) + 1 :]
            if candidate is None:
                continue
            try:
                text = candidate.decode("ascii")
            except UnicodeDecodeError:
                continue
            if accept(text):
                yield text, root / name


def session_scan(
    proc_root: Path | str, *, exclude_pids: Iterable[int] = (), accept: Callable[[str], object],
) -> observation.ProcessScan:
    """The argv scan with explicit uncertainty: an unlistable table is not "no process".

    The table is enumerated once, and that enumeration is the one scanned, so
    a table that turns unreadable cannot be reported as known and empty. A
    listed process whose argv cannot be opened or read (other than one that
    vanished) may be a client naming the session: the scan is unknown too.
    """
    root = Path(proc_root)
    try:
        entries = os.listdir(root)
    except OSError as exc:
        return observation.ProcessScan(False, frozenset(), type(exc).__name__)
    unreadable: list[str] = []
    ids = frozenset(rid for rid, _path in _scan_entries(
        root, entries, exclude_pids=exclude_pids, accept=accept, unreadable=unreadable))
    if unreadable:
        return observation.ProcessScan(False, frozenset(), unreadable[0])
    return observation.ProcessScan(True, ids)
