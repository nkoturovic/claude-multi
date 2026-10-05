"""Retained and owned Claude Code copies: verification, report and prune rule.

Two kinds of copies live under the data root:

- ``pinned-clients/<version>``: copies retained by earlier releases. They are
  rollback material and are never pruned; a launch only reads one, to copy a
  hash-identical pin into the owned layout.
- ``claude/<version>/claude``: claude-multi's own copies (``pin.py``). The
  prune rule keeps the current pin, the previous release's pin, every
  version a claude-multi launch holds the use lock of, every version a
  readable process runs, and the version the running gateway's launcher
  release pins; it never decides when the process table or the gateway's
  start record cannot be read.

The use lock is the authority for what claude-multi runs: every launch holds
``<version>/.in-use`` shared from before its hash check for as long as that
Claude Code runs (``launch.PinnedCopy``; the descriptor is inherited across
exec). The process scan only adds evidence: a process whose executable link
(``/proc``) or path (macOS ``ps``) names an owned copy keeps it, and a
process whose executable cannot be read is no evidence either way. A copy
started outside claude-multi holds no lock, so it is protected only while
its process can be inspected (the documented residual).

Pruning coordinates with the writers and the users of the owned layout
through two locks per version: an acquisition holds ``.acquire-<version>``
next to the version directories (``acquire.py``) and a launch holds
``<version>/.in-use``. ``prune_copies`` takes both exclusively, without
waiting, keeps every version it cannot lock (reported as in use), reads the
protection facts again while it holds them, and deletes only what that fresh
reading still allows, with the locks held through the deletion and the
use lock file removed last (no launch verifies a copy that is about to go).
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from . import paths, pin

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
# A new version lands beside the current one while both may be in use, and
# a download keeps a partial file next to the destination: ask for room for
# two builds of the pinned size before calling the disk low.
LOW_DISK_BUILDS = 2
_PROC_SCAN_LIMIT = 65536
# Keep reasons.
IN_USE = "in use"
RUNNING = "a running session uses it"
LOCK_UNKNOWN = "its use lock cannot be checked"


class UnknownFact(Exception):
    """A fact the prune rule needs cannot be established (nothing is removed)."""


# ``prune_plan(gateway_launcher=UNKNOWN)``: a gateway start is recorded, but
# its record cannot be read.
UNKNOWN = object()


def retained_path(root: Path, version: str) -> Path | None:
    return root / version if isinstance(version, str) and _VERSION.fullmatch(version) else None


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify_retained(root: Path, version: str, sha256: str) -> Path | None:
    """Full verification of a retained copy (hooks must use same_file)."""

    from . import state

    target = retained_path(root, version)
    if target is None:
        return None
    try:
        state._check_directory(root)
        state._check_regular_file(target)
        if os.access(target, os.X_OK) and _digest(target) == sha256:
            return target
    except OSError:
        pass
    return None


def same_file(a: Path, b: Path) -> bool:
    """Metadata-only identity (no hash and no binary reads)."""

    try:
        left, right = a.stat(), b.stat()
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    except OSError:
        return False


def free_bytes(path: Path) -> int | None:
    try:
        usage = os.statvfs(path)
    except (OSError, AttributeError):
        return None
    return usage.f_bavail * usage.f_frsize


# ------------------------------------------------------------- prune rule


@dataclass(frozen=True)
class PrunePlan:
    """Owned copies to keep (with the reason) and to remove.

    ``blocked`` names why nothing may be removed now (a fact the rule needs
    is unknown); ``remove`` is then empty.
    """

    keep: Mapping[str, str]
    remove: tuple[tuple[str, int], ...] = ()
    blocked: str | None = None


def owned_versions(environ: Mapping[str, str]) -> list[str]:
    """Version directories under the owned root (symlinks never count)."""

    root = pin.owned_root(environ)
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    found = [entry.name for entry in entries
             if _VERSION.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)]
    return sorted(found, key=lambda name: pin.version_key(name) or ())


@dataclass(frozen=True)
class ProcessScan:
    """The owned versions live processes are seen to execute."""

    versions: frozenset[str] = frozenset()


def _platform() -> str:
    """The process backend's platform (a seam: tests select macOS here)."""

    return sys.platform


def _proc_executables(proc_root: Path) -> list[Path] | None:
    """Every readable ``/proc/<pid>/exe`` target; None when the table cannot
    be listed in full. A process that exited meanwhile, has no executable (a
    kernel thread) or whose link cannot be read (another user's, or a
    non-dumpable one) is skipped: the use lock, not this scan, protects what
    claude-multi runs."""

    try:
        entries = [entry for entry in os.scandir(proc_root) if entry.name.isdecimal()]
    except OSError:
        return None
    if not entries or len(entries) > _PROC_SCAN_LIMIT:
        return None
    targets = []
    for entry in entries:
        try:
            targets.append(Path(os.readlink(Path(entry.path) / "exe")))
        except OSError:
            continue
    return targets


def _darwin_executables(runner: Callable | None) -> list[Path] | None:
    """The executable path of every process ``ps`` names (macOS has no
    ``/proc``), symlinks resolved; None when the table cannot be read."""

    from .platform import darwin_process

    found = (darwin_process.executable_paths() if runner is None
             else darwin_process.executable_paths(runner=runner))
    if found is None:
        return None
    targets = []
    for path in found:
        if Path(path).name in ("claude", "claude.exe"):
            try:
                targets.append(Path(os.path.realpath(path)))
            except (OSError, ValueError):
                continue
    return targets


def scan_processes(
    owned_root: Path, proc_root: Path | str = "/proc", *, runner: Callable | None = None,
) -> ProcessScan | None:
    """Which owned versions live processes are seen to run (supplementary
    evidence: the use lock is the authority); None when the process table
    cannot be read or listed in full.

    Linux reads ``/proc/<pid>/exe`` links, macOS one bounded ``ps`` read
    (``runner`` is its test seam); no hashing, no binary reads."""

    if _platform() == "darwin":
        targets = _darwin_executables(runner)
    else:
        targets = _proc_executables(Path(proc_root))
    if targets is None:
        return None
    owned = Path(os.path.realpath(owned_root))
    return ProcessScan(frozenset(
        target.parent.name for target in targets
        if target.parent.parent == owned and _VERSION.fullmatch(target.parent.name)
    ))


def use_state(environ: Mapping[str, str], name: str) -> bool | None:
    """Whether a claude-multi launch or session holds version ``name``'s use
    lock: a momentary non-blocking probe that creates nothing. None when the
    lock file or its directory is unsafe (:func:`pin.take_use_lock` would
    refuse it; the version is then kept)."""

    from .platform import posix_fs

    directory = pin.owned_root(environ) / name
    path = directory / pin.IN_USE
    try:
        pin.check_private_directory(directory.parent)
        pin.check_private_directory(directory)
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except (OSError, pin.PinError):
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        return None
    return posix_fs.lock_held(path)


def _gateway_launcher(environ: Mapping[str, str]) -> str | None:
    """The launcher release that started the gateway, or None when no start
    is recorded. Raises :class:`UnknownFact` when a start record exists but
    cannot be read or is not valid."""

    from . import service

    workdir = service.gateway_workdir(paths.state_root(dict(environ)))
    try:
        os.lstat(workdir / service.EXEC_STAMP)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UnknownFact(f"the gateway's start record cannot be checked ({exc.strerror})") from exc
    stamp = service.read_exec_stamp(workdir)
    if stamp is None:
        raise UnknownFact("the gateway's start record cannot be read")
    return stamp.launcher_version


def _launcher_key(value: str) -> tuple[int, ...]:
    return pin.version_key(value.removesuffix("-dev")) or ()


def _installed_release_pins(environ: Mapping[str, str], keep: dict[str, str]) -> str | None:
    """Add the installed release's pins to ``keep`` (``current``, ``previous``
    and the running gateway's version, as the install links say, never by
    version order); the reason no copy may go when they cannot be read."""

    from . import install_txn, self_update

    try:
        pins = self_update.installed_pins(
            environ, protected=lambda root: install_txn.protected_versions(root, paths.state_root(dict(environ))))
    except (OSError, self_update.UpdateError) as exc:
        return f"the installed release's pins cannot be read ({exc})"
    for name, why in pins.items():
        keep.setdefault(name, why)
    return None


def prune_plan(
    contract: Mapping[str, object], environ: Mapping[str, str], *,
    launcher_version: str | None = None, proc_root: Path | str = "/proc",
    gateway_launcher: str | None | object = ...,
    running: Iterable[str] | None | object = ...,
    held: Iterable[str] = (),
) -> PrunePlan:
    """Which owned copies may go (read only).

    Keeps the current pin; the previous release's pin (the version the
    newest older launcher release recorded, else the newest owned version
    below the current pin); the pin of the launcher release that started
    the gateway; every version whose use lock a claude-multi launch holds
    (:func:`use_state`; an unsafe lock file keeps its version too); and
    every version a live process is seen to run. A pin keeps its pin reason
    while it is in use too (doctor adds the use), so only the use-based
    reasons (:data:`IN_USE`, :data:`RUNNING`) end when the sessions do.
    When the process table or the gateway's pin cannot be known, nothing is
    removed. ``held``: the versions whose use locks the caller holds itself
    (pruning), never probed. ``running`` (tests): the versions live
    processes run, or None for an unknown answer; ``gateway_launcher``
    (tests): a launcher version, None for no recorded start, or
    :data:`UNKNOWN` for an unreadable one.
    """

    from . import __version__, acquire

    launcher = __version__ if launcher_version is None else launcher_version
    current = pin.version(contract)
    keep: dict[str, str] = {current: "the current pin"}
    present = owned_versions(environ)
    index = acquire.read_index(environ)
    older = sorted((name for name in index if _launcher_key(name) < _launcher_key(launcher)),
                   key=_launcher_key)
    previous = index[older[-1]] if older else None
    if previous is None:
        below = [name for name in present if (pin.version_key(name) or ()) < (pin.version_key(current) or ())]
        previous = below[-1] if below else None
    if previous is not None:
        keep.setdefault(previous, "the previous release's pin")
    installed_blocked = _installed_release_pins(environ, keep)
    gateway_blocked: str | None = None
    stamped = gateway_launcher
    if gateway_launcher is ...:
        try:
            stamped = _gateway_launcher(environ)
        except UnknownFact as exc:
            gateway_blocked = str(exc)
            stamped = None
    elif gateway_launcher is UNKNOWN:
        gateway_blocked = "the gateway's start record cannot be read"
        stamped = None
    if isinstance(stamped, str) and stamped != launcher:
        stamped_pin = index.get(stamped)
        if stamped_pin is None:
            gateway_blocked = (f"the running gateway was started by claude-multi {stamped}, "
                               "whose Claude Code version is not recorded here")
        else:
            keep.setdefault(stamped_pin, f"the running gateway's release ({stamped}) pins it")
    mine = frozenset(held)
    for name in present:
        if name not in keep and name not in mine:
            lock_state = use_state(environ, name)
            if lock_state is not False:
                keep[name] = IN_USE if lock_state else LOCK_UNKNOWN
    blocked: str | None = None
    if running is ...:
        scan = scan_processes(pin.owned_root(environ), proc_root)
        live = None if scan is None else scan.versions
    else:
        live = running
    if live is None:
        blocked = "the processes running Claude Code cannot be checked on this system"
    else:
        for name in sorted(live):
            keep.setdefault(name, RUNNING)
    blocked = blocked or gateway_blocked or installed_blocked
    if blocked is not None:
        return PrunePlan(keep, (), blocked)
    return PrunePlan(keep, tuple((name, _tree_size(pin.owned_root(environ) / name))
                                 for name in present if name not in keep), None)


def _tree_size(directory: Path) -> int:
    total = 0
    try:
        for entry in os.scandir(directory):
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    except OSError:
        pass
    return total


def _hold(environ: Mapping[str, str], name: str) -> list | None:
    """The acquisition and use locks of version ``name``, held exclusively
    (the use lock file is created when missing, so no launch can start using
    the version until the locks are released). None when another holder has
    either (never waits): the version is being set up or is in use. Raises
    ``OSError`` or :class:`pin.PinError` when a lock cannot be taken safely."""

    from . import acquire

    acquisition = acquire.version_lock(environ, name)
    if not acquisition.acquire(blocking=False):
        return None
    try:
        use = pin.take_use_lock(environ, name, shared=False, blocking=False)
    except BaseException:
        acquisition.release()
        raise
    if use is None:
        acquisition.release()
        return None
    return [acquisition, use]


# What :func:`_remove_version` did.
_REMOVED = "removed"  # the version directory is gone
_LEFT = "left"  # left in place (perhaps without its copy), its use lock file the caller's
_REOPENED = "reopened"  # every file removed, but a launch made a new use lock file meanwhile


def _remove_version(root: Path, name: str) -> str:
    """Remove one version directory whose acquisition and use locks the
    caller holds: regular files only, never through a link; a directory
    holding anything else is left in place.

    The use lock file goes last. Until no copy is left, a launch that opens
    it finds the file the caller holds locked and waits; one that opens it
    after its removal creates a new one, but finds no copy to verify (it is
    not set up, or it copies a retained copy back and runs that)."""

    directory = root / name
    try:
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode):
            return _LEFT
        entries = list(os.scandir(directory))
        if not all(entry.is_file(follow_symlinks=False) for entry in entries):
            return _LEFT
        for entry in entries:
            if entry.name != pin.IN_USE:
                os.unlink(entry.path)
        try:
            os.unlink(directory / pin.IN_USE)
        except FileNotFoundError:
            pass
    except OSError:
        return _LEFT
    try:
        os.rmdir(directory)
    except OSError:
        return _REOPENED
    return _REMOVED


@dataclass(frozen=True)
class PruneOutcome:
    """What :func:`prune_copies` did: the versions it removed; the ones it
    would have removed but kept because they are in use (a claude-multi
    session or launch holds the use lock, a setup is putting the version in
    place, or a process started meanwhile runs it); and the ones it tried
    to remove but left in place, perhaps in part (the directory holds
    something other than regular files, or a file could not be removed)."""

    removed: tuple[str, ...] = ()
    in_use: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def _version_order(name: str) -> tuple[int, ...]:
    return pin.version_key(name) or ()


def prune_copies(
    contract: Mapping[str, object], environ: Mapping[str, str], *,
    launcher_version: str | None = None, proc_root: Path | str = "/proc",
    gateway_launcher: str | None | object = ...,
    running: Iterable[str] | None | object = ...,
) -> PruneOutcome:
    """Remove the owned copies no release here needs.

    A version whose use lock is held, or that a process is seen to run, is
    kept and reported as in use. Each version the plan would remove is
    locked first (its acquisition and use locks, exclusively and without
    waiting): a version being acquired, or one a launch locked since the
    plan, is kept and reported as in use too. With the locks held the
    protection facts are read again (:func:`prune_plan`), so a process that
    started meanwhile, or a gateway started since, keeps its version; only
    what that fresh plan still removes is deleted, and the locks are held
    until the deletion is done (the use lock file last, see
    :func:`_remove_version`). Nothing is removed (or reported) while the
    plan is blocked.
    """

    facts = dict(launcher_version=launcher_version, proc_root=proc_root,
                 gateway_launcher=gateway_launcher, running=running)
    first = prune_plan(contract, environ, **facts)
    if first.blocked is not None:
        return PruneOutcome()
    present = set(owned_versions(environ))
    in_use = {name for name, reason in first.keep.items() if reason in (RUNNING, IN_USE) and name in present}
    root = pin.owned_root(environ)
    held: dict[str, list] = {}
    removed: list[str] = []
    failed: set[str] = set()

    def ordered(names: Iterable[str]) -> tuple[str, ...]:
        return tuple(sorted(names, key=_version_order))

    try:
        for name, _size in first.remove:
            try:
                locks = _hold(environ, name)
            except (OSError, pin.PinError):
                continue  # a lock cannot be taken safely: the version stays
            if locks is None:
                in_use.add(name)
            else:
                held[name] = locks
        if held:
            fresh = prune_plan(contract, environ, held=held, **facts)
            if fresh.blocked is not None:
                return PruneOutcome((), ordered(in_use))
            allowed = {name for name, _size in fresh.remove}
            for name in held:
                if name in allowed:
                    done = _remove_version(root, name)
                    if done == _REMOVED:
                        removed.append(name)
                    elif done == _REOPENED and use_state(environ, name):
                        in_use.add(name)  # a launch has taken the version up again
                    else:
                        failed.add(name)
                elif fresh.keep.get(name) in (RUNNING, IN_USE):
                    in_use.add(name)
        return PruneOutcome(ordered(removed), ordered(in_use), ordered(failed))
    finally:
        # The acquisition lock first: a launch waiting for the use lock may
        # copy a retained copy back into place as soon as it wakes.
        for locks in held.values():
            for lock in locks:
                lock.release()


def prune_lines(outcome: PruneOutcome, environ: Mapping[str, str]) -> list[str]:
    """What :func:`prune_copies` did, as the lines setup, the installer and
    the updater print."""

    lines = []
    if outcome.removed:
        lines.append("Removed Claude Code copies no release here needs: " + ", ".join(outcome.removed) + ".")
    if outcome.in_use:
        lines.append("Kept Claude Code copies in use: " + ", ".join(outcome.in_use)
                     + f" — once no session runs them, `{pin.SETUP_COMMAND}` removes them.")
    if outcome.failed:
        lines.append("Could not remove Claude Code copies: " + ", ".join(outcome.failed)
                     + f" — check {paths.display(pin.owned_root(environ), environ)}, then run "
                     f"`{pin.SETUP_COMMAND}` again.")
    return lines


def prune(
    contract: Mapping[str, object], environ: Mapping[str, str], **facts: object,
) -> list[str]:
    """The versions :func:`prune_copies` removed."""

    return list(prune_copies(contract, environ, **facts).removed)


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.0f} MiB"


def _sizes(copies: Iterable[tuple[str, int]]) -> str:
    return ", ".join(f"{name} ({_megabytes(size)})" for name, size in copies)


def retention_report(
    effective: Mapping, packaged: Mapping | None, environ: Mapping[str, str]
) -> tuple[list[str], list[str]]:
    """Read-only (attention, info) about owned copies; never hashes."""

    del packaged  # one pin per release: the effective contract is the packaged one
    attention: list[str] = []
    info: list[str] = []
    try:
        plan = prune_plan(effective, environ)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return [f"Claude Code copies cannot be listed ({exc})"], []
    present = set(owned_versions(environ))
    kept = []
    for name, reason in plan.keep.items():
        if name not in present:
            continue
        # A pin keeps its pin reason; a claude-multi session using it shows too.
        if reason not in (IN_USE, RUNNING, LOCK_UNKNOWN) and use_state(environ, name):
            reason = f"{reason}, {IN_USE}"
        kept.append(f"{name} ({reason})")
    if kept:
        info.append("Claude Code copies kept by claude-multi: " + ", ".join(kept) + ".")
    if plan.remove:
        attention.append(
            f"Claude Code copies no release here needs: {_sizes(plan.remove)} — "
            f"`{pin.SETUP_COMMAND}` removes them"
        )
    low = _low_disk_line(effective, environ)
    if low:
        attention.append(low)
    return attention, info


def _low_disk_line(contract: Mapping, environ: Mapping[str, str]) -> str | None:
    record = pin.platform_record(contract, pin.host_platform())
    if record is None:
        return None
    root = pin.owned_root(environ)
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    available = free_bytes(probe)
    if available is None or available >= LOW_DISK_BUILDS * record["size"]:
        return None
    return (f"disk space: {_megabytes(available)} free on the filesystem of "
            f"{paths.display(root, environ)}; a new Claude Code version needs about "
            f"{_megabytes(record['size'])}")
