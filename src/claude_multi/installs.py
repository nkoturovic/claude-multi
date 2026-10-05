"""Installations, their release channels and the state root's channel marker.

Each release channel installs under a root of its own, so two installations
never overwrite each other's files:

- ``bundle``: ``~/.local/share/claude-multi/install/{versions/<v>,current,previous}``
  with relative links; the installer and the self-update own it (this module
  only reads it).
- ``nix``: the Nix store. Its stable exec link
  ``~/.local/share/claude-multi/nix/current`` exists only once
  ``claude-multi gateway service install`` selected an installation for the
  supervised service; it is registered as an indirect garbage-collector root
  so the store path it names stays.
- ``source``: a checkout, run in place.

One state root serves one channel. ``<state root>/channel`` (the state root
is ``$XDG_STATE_HOME/claude-multi`` or ``~/.local/state/claude-multi``) is one
ASCII line, ``bundle``, ``nix`` or ``source``, newline-terminated, mode
0600. The first state-writing run of a launcher whose channel is known
claims it under an exclusive lock (``channel.lock``), so of two first runs
of different channels exactly one owns the state root; a launcher of another
channel then refuses with a one-line hint, and ``claude-multi doctor`` shows
every installation it finds. Any other marker content fails closed, and a
claim that cannot be written refuses the state-writing run.
"""

from __future__ import annotations

import errno
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from claude_multi import endpoint, errors, layout, paths, state

MARKER = "channel"
CHANNELS = endpoint.CHANNELS
BUNDLE_DIR = "install"
NIX_LINK = Path("nix") / "current"
# An exec link some Nix setups keep outside the data root.
NIX_RELEASE_LINK = Path(".local") / "share" / "claude-multi-release" / "current"
PROXY_ENTRY = Path("bin") / "claude-multi-proxy"
STORE_PREFIX = "/nix/store/"
GC_ROOT_TIMEOUT = 60


class ChannelError(errors.ClaudeMultiError, ValueError):
    """The channel marker is unreadable or names no known channel."""


def marker_path(state_root: Path | str) -> Path:
    return Path(state_root) / MARKER


def _marker_remedy(state_root: Path | str, environ: Mapping[str, str]) -> str:
    return (f"write the channel of the installation you use (bundle, nix or source) as the only line of "
            f"{paths.display(marker_path(state_root), environ)}, then retry")


def read_marker(state_root: Path | str, environ: Mapping[str, str] | None = None) -> str | None:
    """The recorded channel, None when no marker exists; anything else raises."""

    env = os.environ if environ is None else environ
    target = marker_path(state_root)
    if not os.path.lexists(target):
        return None
    try:
        raw = state.read_private(target)
    except state.StateError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise ChannelError(f"the channel marker cannot be read: {exc}",
                           remedy=_marker_remedy(state_root, env)) from exc
    except OSError as exc:
        raise ChannelError(f"the channel marker cannot be read: {exc.strerror or exc}",
                           remedy=_marker_remedy(state_root, env)) from exc
    for channel in CHANNELS:
        if raw == f"{channel}\n".encode("ascii"):
            return channel
    raise ChannelError("the channel marker names no known channel",
                       remedy=_marker_remedy(state_root, env))


def write_marker(state_root: Path | str, channel: str) -> None:
    if channel not in CHANNELS:
        raise ChannelError(f"unknown channel {channel!r}")
    root = state.ensure_private_dir(state_root)
    state.atomic_write(marker_path(root), f"{channel}\n".encode("ascii"))


def claim_marker(state_root: Path | str, channel: str,
                 environ: Mapping[str, str] | None = None) -> tuple[str, bool]:
    """Record ``channel`` unless a marker exists, atomically with respect to
    other claims: ``(owner, recorded)``. The marker is re-read under the
    exclusive claim lock, so a concurrent first run of another channel sees
    this one's marker (and is refused), never overwrites it."""

    if channel not in CHANNELS:
        raise ChannelError(f"unknown channel {channel!r}")
    root = state.ensure_private_dir(state_root)
    with state.FileLock(marker_path(root)):
        owner = read_marker(root, environ)
        if owner is not None:
            return owner, False
        write_marker(root, channel)
        return channel, True


_SWITCH = {
    ("nix", "bundle"): "run the installer with --migrate-from-nix to move this account to it",
    ("bundle", "nix"): "keep using the installed release; to switch this account to Nix, remove that release "
                       "first, then remove {marker}",
    ("source", "bundle"): "run the installer with --migrate-from-nix to move this account to it",
}


@dataclass(frozen=True)
class ChannelCheck:
    """One launcher run against the marker: ``problem`` set means refuse."""

    running: str | None
    owner: str | None
    recorded: bool = False
    problem: str | None = None
    remedy: str | None = None

    @property
    def ok(self) -> bool:
        return self.problem is None


def check(state_root: Path | str, environ: Mapping[str, str], *, record: bool) -> ChannelCheck:
    """Compare this launcher's channel with the marker; record it when absent.

    A launcher whose channel is unknown (no wrapper named one) neither records
    nor refuses. ``record`` is False for read-only commands.
    """

    running = endpoint.channel(environ)
    try:
        owner = read_marker(state_root, environ)
    except ChannelError as exc:
        return ChannelCheck(running, None, problem=str(exc), remedy=exc.remedy)
    if running is None:
        return ChannelCheck(None, owner)
    recorded = False
    if owner is None:
        if not record:
            return ChannelCheck(running, None)
        try:
            owner, recorded = claim_marker(state_root, running, environ)
        except ChannelError as exc:
            return ChannelCheck(running, None, problem=str(exc), remedy=exc.remedy)
        except (OSError, errors.ClaudeMultiError) as exc:
            return ChannelCheck(
                running, None,
                problem=f"this installation could not record itself as the owner of the claude-multi state "
                        f"({getattr(exc, 'strerror', None) or exc})",
                remedy=f"make {paths.display(state_root, environ)} a private directory you own, then retry")
    if owner == running:
        return ChannelCheck(running, owner, recorded=recorded)
    shown = paths.display(marker_path(state_root), environ)
    hint = _SWITCH.get((owner, running),
                       "use that installation, or remove {marker} once it is uninstalled").format(marker=shown)
    return ChannelCheck(
        running, owner,
        problem=f"this account's claude-multi state belongs to the {owner} installation, and this is the "
                f"{running} one",
        remedy=hint + " (claude-multi doctor lists both)",
    )


def guard(state_root: Path | str, environ: Mapping[str, str], *, writes: bool) -> ChannelCheck:
    """:func:`check` for one command, before anything is constructed or written.

    A state-writing command of another channel (or over an unreadable
    marker) raises :class:`ChannelError` with the hint; a read-only one gets
    the failed check back and must then write nothing (no shim refresh, no
    state write).
    """

    result = check(state_root, environ, record=writes)
    if not result.ok and writes:
        raise ChannelError(str(result.problem), remedy=result.remedy)
    return result


# ------------------------------------------------------------ install roots

def data_root(environ: Mapping[str, str]) -> Path:
    return paths.data_root(environ)


def bundle_root(environ: Mapping[str, str]) -> Path:
    return data_root(environ) / BUNDLE_DIR


def bundle_link(environ: Mapping[str, str]) -> Path:
    return bundle_root(environ) / "current"


def nix_link(environ: Mapping[str, str]) -> Path:
    return data_root(environ) / NIX_LINK


def nix_release_link(environ: Mapping[str, str]) -> Path:
    return paths.home(environ) / NIX_RELEASE_LINK


def install_root(environ: Mapping[str, str], installation: Path | str | None = None) -> Path | None:
    """The installation this launcher runs from: the directory whose
    ``bin/claude-multi-proxy`` belongs to it (None when not found).
    ``installation`` is the launcher tree (default: the running one,
    :func:`claude_multi.layout.installation`); resources never name it."""

    candidates: list[Path] = []
    hook = environ.get("CLAUDE_MULTI_HOOK_COMMAND")
    if hook:
        candidates.append(Path(hook).parent.parent)
    tree = Path(installation) if installation is not None else layout.installation()
    if tree is not None:
        candidates.append(tree)
    for candidate in candidates:
        if (candidate / PROXY_ENTRY).is_file():
            return candidate
    return None


def store_path(path: Path | str) -> str | None:
    """The top-level store path ``path`` resolves into, or None."""

    real = os.path.realpath(path)
    if not real.startswith(STORE_PREFIX):
        return None
    name = real[len(STORE_PREFIX):].split("/", 1)[0]
    return STORE_PREFIX + name if name else None


def link_target(link: Path) -> str | None:
    try:
        return os.readlink(link)
    except OSError:
        return None


@dataclass(frozen=True)
class Installation:
    """One installation found on this account (for doctor and status)."""

    channel: str
    link: Path
    target: str | None  # the link's text
    resolved: str | None  # where it leads now (None when dangling)

    def line(self, environ: Mapping[str, str]) -> str:
        where = paths.display(self.link, environ)
        if self.resolved is None:
            return f"{self.channel}: {where} -> {self.target} (dangling)"
        return f"{self.channel}: {where} -> {self.resolved}"


def discover(environ: Mapping[str, str]) -> list[Installation]:
    """Every installation link on this account (none are followed beyond a resolve)."""

    found = []
    for channel, link in (("bundle", bundle_link(environ)), ("nix", nix_link(environ)),
                          ("nix", nix_release_link(environ))):
        if not os.path.islink(link):
            continue
        target = link_target(link)
        resolved = os.path.realpath(link) if os.path.exists(link) else None
        found.append(Installation(channel, link, target, resolved))
    return found


def installer_launchers(environ: Mapping[str, str]) -> list[str]:
    """Doctor's view of the installer's launcher files on PATH: where each
    leads and whether it is still the installer's (``installer.json``)."""

    from claude_multi import install_receipt

    root = bundle_root(environ)
    try:
        receipt = install_receipt.read(root)
    except install_receipt.ReceiptError:
        return []
    lines = []
    for launcher in receipt.launchers:
        shown = paths.display(launcher.path, environ)
        if not os.path.lexists(launcher.path):
            lines.append(f"{shown} (missing; sh install.sh --repair restores it)")
        elif not launcher.owned():
            lines.append(f"{shown} (changed since the installer wrote it; left alone)")
        else:
            lines.append(f"{shown} -> {paths.display(root / 'current' / 'bin' / Path(launcher.path).name, environ)}")
    return lines


# ------------------------------------------------------------ selection

class SelectionError(errors.ClaudeMultiError, RuntimeError):
    """The installation for the supervised service cannot be selected."""


@dataclass(frozen=True)
class Selection:
    """The installation the supervised service executes through its link."""

    channel: str
    link: Path
    root: str  # the installation the link names after the selection
    previous: str | None = None  # the link's text before (nix; None = absent)
    changed: bool = False
    gc_root: bool | None = None  # nix: registered as a garbage-collector root
    note: str | None = None


def plan_selection(channel: str | None, environ: Mapping[str, str],
                   installation: Path | str | None = None) -> Selection:
    """What a service install would execute (no writes)."""

    if channel == "bundle":
        link = bundle_link(environ)
        if not os.path.islink(link) or not (link / PROXY_ENTRY).is_file():
            raise SelectionError(
                f"the installed release has no usable {paths.display(link, environ)} link",
                remedy="repair the installation: sh install.sh --repair")
        return Selection("bundle", link, os.path.realpath(link))
    if channel == "nix":
        root = install_root(environ, installation)
        store = store_path(root) if root is not None else None
        if store is None or not (Path(store) / PROXY_ENTRY).is_file():
            raise SelectionError("this launcher does not run from a Nix store package",
                                 remedy="run claude-multi gateway service install from the installed package")
        link = nix_link(environ)
        if os.path.lexists(link) and not os.path.islink(link):
            raise SelectionError(f"{paths.display(link, environ)} exists and is not a link",
                                 remedy="move it away, then retry")
        previous = link_target(link)
        return Selection("nix", link, store, previous=previous, changed=previous != store)
    raise SelectionError(
        "the supervised gateway service runs only from an installed release or the Nix package"
        + ("" if channel is None else f" (this launcher is a {channel} checkout)"),
        remedy="install a release (or the Nix package) and run its claude-multi gateway service install; "
               "the on-demand gateway keeps working meanwhile")


def _replace_link(link: Path, target: str) -> None:
    temporary = link.with_name(f".{link.name}.{secrets.token_hex(4)}.tmp")
    os.symlink(target, temporary)
    try:
        os.replace(temporary, link)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    try:
        state._fsync_directory(link.parent)
    except OSError:
        pass


def apply_selection(selection: Selection, environ: Mapping[str, str], *,
                    runner: Callable = subprocess.run) -> Selection:
    """Point the nix link at the selected store path and register its
    garbage-collector root (bundles: nothing to write)."""

    if selection.channel != "nix":
        return selection
    state.ensure_private_dir(selection.link.parent)
    if selection.changed or not os.path.islink(selection.link):
        _replace_link(selection.link, selection.root)
    registered = register_gc_root(selection.link, selection.root, environ, runner=runner)
    note = None if registered else (
        "the link is not a garbage-collector root (nix-store failed or is missing): a collection may remove "
        "the package the service runs; run claude-multi gateway service install again after rebuilding")
    return Selection(selection.channel, selection.link, selection.root, selection.previous,
                     selection.changed, registered, note)


def restore_selection(selection: Selection) -> None:
    """Undo :func:`apply_selection` (the link's previous text, or no link)."""

    if selection.channel != "nix" or not selection.changed:
        return
    if selection.previous is None:
        try:
            os.unlink(selection.link)
        except FileNotFoundError:
            pass
    else:
        _replace_link(selection.link, selection.previous)


def remove_nix_link(environ: Mapping[str, str]) -> str | None:
    """Remove the nix exec link when it names a store path; returns its text."""

    link = nix_link(environ)
    target = link_target(link)
    if target is None or not target.startswith(STORE_PREFIX):
        return None
    os.unlink(link)
    return target


def register_gc_root(link: Path, target: str, environ: Mapping[str, str], *,
                     runner: Callable = subprocess.run) -> bool:
    """``nix-store --add-root LINK --indirect --realise TARGET`` (bounded)."""

    tool = shutil.which("nix-store", path=environ.get("PATH") or None)
    if tool is None:
        return False
    try:
        completed = runner([tool, "--add-root", str(link), "--indirect", "--realise", target],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GC_ROOT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and os.path.realpath(link) == os.path.realpath(target)
