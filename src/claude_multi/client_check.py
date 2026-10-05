"""Which Claude Code runs a managed session: the hook-side pin check.

The Claude daemon can take over a backgrounded managed session and run it
with the user's own ``claude``, which may have auto-updated past the pinned
version; the session then runs outside the pin's evidence. The SessionStart
and prompt hooks find the client process that runs them, the nearest
ancestor whose command line names the session (``--session-id <id>`` or
``--resume <id>``), and compare its executable with the pin: on Linux the
``/proc/<pid>/exe`` link, on macOS ``ps -o comm=``. A different Claude Code
version gets one message telling the user to exit and resume through
claude-multi. Anything unknown (no such ancestor, an executable whose path
names no version, an unreadable contract) stays silent.

Stdlib plus ``assets``, ``paths``, ``pin`` and ``strict_json``: the scope-only
hooks import this module, so it never loads the catalog. Never raises.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from . import assets, paths, pin, strict_json

ANCESTOR_LIMIT = 8
SESSION_FLAGS = ("--session-id", "--resume")
_STAT_LIMIT = 4096
_CMDLINE_LIMIT = 1 << 20
_CONTRACT_LIMIT = 1 << 20
_PS_TIMEOUT = 2.0


def pinned_version(environ: Mapping[str, str]) -> str | None:
    """The pinned Claude Code version from the packaged native contract."""

    try:
        path = assets.root(environ=environ) / "catalog" / "native-contract.json"
        with path.open("rb") as stream:
            raw = stream.read(_CONTRACT_LIMIT + 1)
        if len(raw) > _CONTRACT_LIMIT:
            return None
        return pin.version(strict_json.loads(raw))
    except Exception:
        return None


def _names_session(argv: list[str], wanted: frozenset[str]) -> bool:
    return any(flag in SESSION_FLAGS and value in wanted for flag, value in zip(argv, argv[1:]))


def _linux_ancestors(start: int, proc_root: Path) -> Iterator[tuple[list[str], Path | None]]:
    pid = start
    seen: set[int] = set()
    for _step in range(ANCESTOR_LIMIT):
        if pid <= 1 or pid in seen:
            return
        seen.add(pid)
        base = proc_root / str(pid)
        try:
            with open(base / "cmdline", "rb") as stream:
                argv = [part.decode("utf-8", "replace")
                        for part in stream.read(_CMDLINE_LIMIT).split(b"\0") if part]
            with open(base / "stat", "rb") as stream:
                raw = stream.read(_STAT_LIMIT)
            parent = int(raw[raw.rindex(b")") + 1:].split()[1])
        except (OSError, ValueError, IndexError):
            return
        try:
            target = os.readlink(base / "exe").removesuffix(" (deleted)")
            exe: Path | None = Path(target)
        except OSError:
            exe = None
        yield argv, exe
        pid = parent


def _ps(run: Callable[..., Any], pid: int, field: str) -> str | None:
    try:
        done = run(["ps", "-ww", "-o", f"{field}=", "-p", str(pid)],
                   capture_output=True, text=True, timeout=_PS_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if getattr(done, "returncode", 1) != 0:
        return None
    text = (getattr(done, "stdout", "") or "").strip()
    return text or None


def _darwin_ancestors(start: int, run: Callable[..., Any]) -> Iterator[tuple[list[str], Path | None]]:
    pid = start
    seen: set[int] = set()
    for _step in range(ANCESTOR_LIMIT):
        if pid <= 1 or pid in seen:
            return
        seen.add(pid)
        args, parent = _ps(run, pid, "args"), _ps(run, pid, "ppid")
        if args is None or parent is None or not parent.isdecimal():
            return
        comm = _ps(run, pid, "comm")
        exe = Path(os.path.realpath(comm)) if comm and comm.startswith("/") else None
        yield args.split(), exe
        pid = int(parent)


def session_client(
    ids: Iterable[str | None], *, pid: int | None = None, proc_root: Path | str = "/proc",
    platform: str = sys.platform, run: Callable[..., Any] = subprocess.run,
) -> Path | None:
    """The executable of the nearest ancestor that runs one of ``ids``.

    Starts at ``pid`` (default: this process's parent) and walks at most
    :data:`ANCESTOR_LIMIT` ancestors. None when no ancestor names a session
    or the platform has no supported process view."""

    wanted = frozenset(item for item in ids if isinstance(item, str) and item)
    if not wanted:
        return None
    start = os.getppid() if pid is None else pid
    if platform.startswith("linux"):
        walk = _linux_ancestors(start, Path(proc_root))
    elif platform == "darwin":
        walk = _darwin_ancestors(start, run)
    else:
        return None
    for argv, exe in walk:
        if _names_session(argv, wanted):
            return exe
    return None


@dataclass(frozen=True)
class ClientMismatch:
    """A managed session that runs a different Claude Code than the pin."""

    actual: Path
    actual_version: str
    pinned: str


def check(
    ids: Iterable[str | None], environ: Mapping[str, str], *, pid: int | None = None,
    proc_root: Path | str = "/proc", platform: str = sys.platform,
    run: Callable[..., Any] = subprocess.run,
) -> ClientMismatch | None:
    """The mismatch between the session's client and the pin, or None (also
    when anything is unknown). Never raises."""

    try:
        pinned = pinned_version(environ)
        if pinned is None:
            return None
        exe = session_client(ids, pid=pid, proc_root=proc_root, platform=platform, run=run)
        if exe is None:
            return None
        actual = pin.version_from_path(exe)
        if actual is None or actual == pinned:
            return None
        return ClientMismatch(exe, actual, pinned)
    except Exception:
        return None


def message(mismatch: ClientMismatch, managed_id: str, environ: Mapping[str, str]) -> str:
    """The user-facing line (a hook ``systemMessage``)."""

    return (
        f"claude-multi: this session runs Claude Code {mismatch.actual_version} "
        f"({paths.display(mismatch.actual, environ)}), not the {mismatch.pinned} claude-multi "
        "verified — the Claude daemon may have moved it onto your own claude after an update. "
        f"Exit it (/exit) and resume it with: claude-multi -r {managed_id}"
    )


__all__ = [
    "ANCESTOR_LIMIT",
    "ClientMismatch",
    "SESSION_FLAGS",
    "check",
    "message",
    "pinned_version",
    "session_client",
]
