"""macOS process backend: listener ownership, gateway PID identity and session liveness.

macOS has no ``/proc``. This backend observes through the system tools with
bounded, read-only calls; every call takes an injected ``runner`` (and the
connect probe its own seam), so tests never touch the host:

* listener owner: ``lsof -nP -iTCP:<port> -sTCP:LISTEN -Fpu`` names the
  listening processes and their uids. lsof cannot see another user's
  sockets, so when it shows nothing a loopback connect probe decides: a
  refused connect is no listener, an accepted one is a listener lsof cannot
  see (unknown, so the caller refuses it);
* gateway PID identity: ``kill(pid, 0)`` plus ``ps -p <pid> -o comm=`` against
  the recorded binary (a PID reused by another program is not the gateway);
* session liveness: one bounded ``ps -axww -o pid=,args=`` scan for the
  ``--session-id``/``--resume`` ids, and the parent walk from one
  ``ps -axo pid=,ppid=`` table;
* owned-copy evidence: one bounded ``ps -axww -o comm=`` read of every
  process's executable path (the copy pruning keeps);
* background (daemon) liveness: the pinned client derives its daemon root
  from a fixed ``/tmp/cc-daemon-<uid>`` on every POSIX system (only Termux
  differs), so the same layout reader applies.

A missing tool, a timeout, a nonzero exit or output beyond the bound is
unknown, never stopped. Native macOS behaviour is claimed only with native
CI evidence; until then these readers are exercised through their seams.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import urllib.parse
from pathlib import Path
from typing import Callable, Iterable

from claude_multi.platform import linux_process, observation, posix_process

SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
TIMEOUT = 5.0
OUTPUT_LIMIT = 16 << 20  # one process-table read; more is unknown, never cut
SESSION_FLAGS = ("--session-id", "--resume")
CONNECT_TIMEOUT = 0.5


def _tool(name: str) -> str | None:
    return shutil.which(name, path=SYSTEM_PATH)


def _run(runner: Callable, argv: list[str], timeout: float = TIMEOUT) -> subprocess.CompletedProcess | None:
    if timeout <= 0:
        return None
    try:
        return runner(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=min(TIMEOUT, timeout),
                      env={"PATH": SYSTEM_PATH, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None


def _text(completed: subprocess.CompletedProcess) -> str | None:
    raw = completed.stdout
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not isinstance(raw, bytes) or len(raw) > OUTPUT_LIMIT:
        return None
    return raw.decode("utf-8", "replace")


def connect_probe(port: int, *, host: str = "127.0.0.1", timeout: float = CONNECT_TIMEOUT) -> bool | None:
    """True when something accepts on ``host:port``, False when refused, None otherwise."""

    if timeout <= 0:
        return None
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(min(CONNECT_TIMEOUT, timeout))
    try:
        probe.connect((host, port))
    except ConnectionRefusedError:
        return False
    except OSError:
        return None
    finally:
        probe.close()
    return True


def parse_lsof(text: str) -> list[tuple[int, int | None]]:
    """``(pid, uid)`` per process from ``lsof -F`` field output."""

    found: list[tuple[int, int | None]] = []
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdecimal():
            found.append((int(line[1:]), None))
        elif line.startswith("u") and line[1:].isdecimal() and found:
            found[-1] = (found[-1][0], int(line[1:]))
    return found


def listener_owner(
    base_url: str, *, pid: int | None, euid: int | None = None, runner: Callable = subprocess.run,
    connect: Callable[[int], bool | None] | None = None, lsof: str | None = None,
    budget: Callable[[], float] | None = None,
) -> observation.OwnerVerdict:
    """Who listens on the base URL's port: ours (only the recorded PID, our
    uid), foreign (another uid), none (nothing accepts) or unknown.

    ``budget`` (the time left, asked before each call) bounds the ``lsof``
    and connect calls together; without it each has its own bound."""

    try:
        port = urllib.parse.urlsplit(base_url).port
        uid = os.geteuid() if euid is None else euid
    except (ValueError, TypeError, AttributeError):
        return observation.OwnerVerdict("unknown", "listener ownership is unavailable")
    if port is None:
        return observation.OwnerVerdict("unknown", "listener ownership is unavailable")
    tool = lsof or _tool("lsof")
    if tool is None:
        return observation.OwnerVerdict("unknown", "listener ownership is unavailable (no lsof)")
    left = budget or (lambda: TIMEOUT)
    completed = _run(runner, [tool, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpu"], left())
    text = _text(completed) if completed is not None else None
    if text is None:
        return observation.OwnerVerdict("unknown", "listener ownership is unavailable (lsof failed)")
    rows = parse_lsof(text)
    if completed.returncode not in (0, 1) or (completed.returncode == 0 and not rows):
        return observation.OwnerVerdict("unknown", "listener ownership is unavailable (lsof failed)")
    for owner_pid, owner in rows:
        if owner is not None and owner != uid:
            return observation.OwnerVerdict("foreign", f"another user (uid {owner})", uid=owner, pid=owner_pid)
    if not rows:
        accepted = connect(port) if connect is not None else connect_probe(
            port, timeout=min(CONNECT_TIMEOUT, left()))
        if accepted is False:
            return observation.OwnerVerdict("none", "nothing listens")
        return observation.OwnerVerdict("unknown", "a listener that lsof cannot see (another user's, "
                                        "or the table is incomplete)")
    if any(owner is None for _pid, owner in rows):
        return observation.OwnerVerdict("unknown", "listener ownership is incomplete")
    if pid is not None and all(owner_pid == pid for owner_pid, _owner in rows):
        return observation.OwnerVerdict("ours", "the claude-multi gateway", uid, pid)
    return observation.OwnerVerdict("unknown", "another process of yours (gateway ownership unconfirmed)",
                                    uid, rows[0][0])


def _same_program(command: str, binary: str) -> bool:
    if command == binary:
        return True
    try:
        return os.path.realpath(command) == os.path.realpath(binary)
    except (OSError, ValueError):
        return False


def gateway_pid(stamp, *, exists: Callable[[int], bool | None] | None = None,
                runner: Callable = subprocess.run, ps: str | None = None,
                timeout: float = TIMEOUT) -> tuple[int | None, bool | None]:
    """``(pid, alive)`` for a recorded start (``pid``, ``binary``).

    Alive only while that PID exists and runs the recorded binary; absent is
    stopped (macOS has one PID namespace); a PID now running another program,
    or an unreadable ``ps`` (or one past ``timeout``), is unknown.
    """

    if stamp is None:
        return None, None
    exists = exists or posix_process.exists
    present = exists(stamp.pid)
    if present is False:
        return stamp.pid, False
    if present is None:
        return None, None
    tool = ps or _tool("ps")
    if tool is None:
        return None, None
    completed = _run(runner, [tool, "-p", str(stamp.pid), "-o", "comm="], timeout)
    text = _text(completed) if completed is not None else None
    if text is None or completed.returncode != 0:
        # ps exits 1 for a PID that vanished meanwhile.
        return (stamp.pid, False) if exists(stamp.pid) is False else (None, None)
    command = text.strip()
    if command and _same_program(command, stamp.binary):
        return stamp.pid, True
    return None, None


def _table(runner: Callable, argv_tail: list[str], ps: str | None) -> tuple[str | None, str | None]:
    tool = ps or _tool("ps")
    if tool is None:
        return None, "no ps"
    completed = _run(runner, [tool, *argv_tail])
    if completed is None:
        return None, "ps did not answer"
    if completed.returncode != 0:
        return None, f"ps exit {completed.returncode}"
    text = _text(completed)
    if text is None:
        return None, "the process table exceeds the read bound"
    return text, None


def session_scan(*, exclude_pids: Iterable[int] = (), accept: Callable[[str], object],
                 runner: Callable = subprocess.run, ps: str | None = None) -> observation.ProcessScan:
    """Ids named after ``--session-id``/``--resume`` in every process's arguments.

    ``ps`` joins argv with spaces, so an argument that itself contains such a
    flag also counts (live): the safe direction for the destructive guards.
    """

    text, reason = _table(runner, ["-axww", "-o", "pid=,args="], ps)
    if text is None:
        return observation.ProcessScan(False, frozenset(), reason)
    excluded = frozenset(exclude_pids)
    ids: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if not fields or not fields[0].isdecimal():
            continue
        if int(fields[0]) in excluded:
            continue
        words = fields[1:]
        for index, word in enumerate(words):
            candidate = None
            if word in SESSION_FLAGS and index + 1 < len(words):
                candidate = words[index + 1]
            else:
                for flag in SESSION_FLAGS:
                    if word.startswith(flag + "="):
                        candidate = word[len(flag) + 1:]
            if candidate is not None and accept(candidate):
                ids.add(candidate)
    return observation.ProcessScan(True, frozenset(ids))


def executable_paths(*, runner: Callable = subprocess.run, ps: str | None = None) -> list[str] | None:
    """The executable path of every process ``ps`` names (``comm``: the path
    the program was executed from); None when the table cannot be read.

    A process whose path ``ps`` cannot show (another user's shows only its
    short name) is skipped: the result is evidence of what runs, never proof
    that something does not."""

    text, _reason = _table(runner, ["-axww", "-o", "comm="], ps)
    if text is None:
        return None
    return [line.strip() for line in text.splitlines() if line.strip().startswith("/")]


def ancestor_pids(pid: int | None = None, *, runner: Callable = subprocess.run,
                  ps: str | None = None) -> frozenset[int]:
    """``pid`` (default: this process) and its ancestors from one ``ps`` table (bounded walk)."""

    current = os.getpid() if pid is None else pid
    text, _reason = _table(runner, ["-axo", "pid=,ppid="], ps)
    parents: dict[int, int] = {}
    for line in (text or "").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdecimal() and fields[1].isdecimal():
            parents[int(fields[0])] = int(fields[1])
    seen: set[int] = set()
    for _step in range(linux_process.ANCESTOR_WALK_LIMIT):
        if current <= 0 or current in seen:
            break
        seen.add(current)
        if current not in parents:
            break
        current = parents[current]
    return frozenset(seen)


def background_liveness(root: Path | None = None) -> observation.BackgroundLiveness:
    """The daemon-root reader (same ``/tmp/cc-daemon-<uid>`` layout as Linux)."""

    return linux_process.background_liveness(root)
