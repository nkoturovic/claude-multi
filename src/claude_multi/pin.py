"""The pinned Claude Code version: contract v2 accessors and the owned copy.

A release pins exactly one Claude Code version. The native contract (v2) is
path-free: its ``verified`` list holds one entry with the version, the
per-platform ``{sha256, size}`` taken from Anthropic's signed release
manifest, the manifest provenance and the evidence class per platform.

claude-multi executes only its own copy of that version,
``<data root>/claude/<version>/claude`` (``claude.exe`` on Windows), after a
full size and SHA-256 check on every launch. The copy is put there by the
acquisition step (``claude-multi setup --step claude``, ``acquire.py``);
the user's own Claude Code install is never executed, moved, linked or
modified by a launch. Each launch holds the version's use lock
(:func:`take_use_lock`) for as long as the copy runs, so pruning never
removes a copy a claude-multi session uses.
"""

from __future__ import annotations

import datetime
import errno
import hashlib
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import errors, paths


# The platforms Anthropic's release manifest lists, in its own spelling.
PLATFORMS = (
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
    "linux-arm64-musl",
    "linux-x64-musl",
    "win32-x64",
    "win32-arm64",
)
EVIDENCE_CLASSES = ("battery", "identity+smoke")
CONTRACT_VERSION = 2
SETUP_COMMAND = "claude-multi setup --step claude"
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class PinError(errors.ClaudeMultiError, RuntimeError):
    """The owned copy of the pinned Claude Code cannot be used (fail closed)."""


def not_set_up_text(version: str) -> str:
    return f"Claude Code {version} is not set up for claude-multi — run `{SETUP_COMMAND}`"


def override_attention(source: str, detail: str | None, pinned: str, override: str) -> str | None:
    """Doctor's line for an operator contract override file (never applied)."""

    if source == "packaged":
        return None
    if source == "override-ignored-newer":
        reason = (f"it pins Claude Code {detail}, newer than the {pinned} this release verified; "
                  "a newer pin comes with a claude-multi release")
    elif source == "override-ignored-stale":
        reason = f"it pins Claude Code {detail}; this release runs its verified {pinned}"
    else:
        reason = f"it cannot be used ({detail})"
    return f"contract override ignored: {reason} — remove it: rm {override}"


def entry(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    """The pinned entry: the newest of the ``verified`` list (one today)."""

    verified = contract["verified"]
    return max(verified, key=lambda item: version_key(item["version"]) or ())


def version(contract: Mapping[str, Any]) -> str:
    return entry(contract)["version"]


def version_key(value: str) -> tuple[int, ...] | None:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        return None
    return tuple(int(part) for part in value.split("."))


def host_platform() -> str:
    """This machine's platform key (``release_manifest.platform_key``)."""

    from . import release_manifest

    return release_manifest.platform_key()


def platform_record(contract: Mapping[str, Any], platform: str) -> Mapping[str, Any] | None:
    """``{sha256, size}`` of the pinned build for ``platform``, or None."""

    record = entry(contract)["platforms"].get(platform)
    return record if isinstance(record, Mapping) else None


def evidence_class(contract: Mapping[str, Any], platform: str) -> str | None:
    """The evidence class recorded for ``platform`` (None: no build listed)."""

    pinned = entry(contract)
    if platform not in pinned["platforms"]:
        return None
    evidence = pinned["evidence"]
    return evidence.get(platform) or evidence.get("others")


def binary_name(platform: str) -> str:
    return "claude.exe" if platform.startswith("win32-") else "claude"


def owned_root(environ: Mapping[str, str]) -> Path:
    return paths.data_root(environ) / "claude"


def owned_path(environ: Mapping[str, str], version_name: str, platform: str) -> Path:
    if version_key(version_name) is None:
        raise PinError(f"invalid Claude Code version {version_name!r}")
    return owned_root(environ) / version_name / binary_name(platform)


# The use lock of one owned version: ``<owned root>/<version>/.in-use``.
# Every claude-multi launch of the copy holds it shared from before the hash
# check for as long as that Claude Code runs (the descriptor is inherited
# across exec); pruning takes it exclusively without waiting and keeps every
# version it cannot lock.
IN_USE = ".in-use"
# Opening again after the file was removed under a waiting holder (a prune
# deleted the version): a bound, never a loop.
_IN_USE_TRIES = 8


class UseLock:
    """A held ``flock`` on one version's :data:`IN_USE` file."""

    def __init__(self, path: Path, descriptor: int, *, shared: bool):
        self.path = path
        self.shared = shared
        self._descriptor: int | None = descriptor

    @property
    def descriptor(self) -> int | None:
        return self._descriptor

    def release(self) -> None:
        """Close the descriptor. Only closing: a process that inherited the
        same open file (an exec'd client's child) keeps the lock."""

        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            os.close(descriptor)


def use_lock_path(environ: Mapping[str, str], version_name: str) -> Path:
    return _use_lock_path(owned_root(environ), version_name)


def _use_lock_path(root: Path, version_name: str) -> Path:
    if version_key(version_name) is None:
        raise PinError(f"invalid Claude Code version {version_name!r}")
    return Path(root) / version_name / IN_USE


def check_private_directory(directory: Path) -> None:
    """The owned root or a version directory: a real directory of this user,
    closed to everyone else. Raises ``FileNotFoundError`` when missing."""

    info = os.lstat(directory)
    if stat.S_ISLNK(info.st_mode):
        problem = f"is a symbolic link — remove the link (rm {directory}; its target is not touched)"
    elif not stat.S_ISDIR(info.st_mode):
        problem = f"is not a directory — remove it (rm {directory})"
    elif info.st_uid != os.geteuid():
        problem = "belongs to another user — remove it"
    elif stat.S_IMODE(info.st_mode) & 0o077:
        problem = f"is open to other users — make it private (chmod 700 {directory})"
    else:
        return
    raise PinError(f"{directory} {problem}, then run `{SETUP_COMMAND}`")


def _unsafe_lock(path: Path, problem: str) -> PinError:
    return PinError(f"{path} {problem}; claude-multi keeps its use lock there only as its own "
                    f"regular file — remove it (rm {path}), then run `{SETUP_COMMAND}`")


def take_use_lock(
    environ: Mapping[str, str], version_name: str, *, shared: bool, blocking: bool = True,
    inheritable: bool = False,
) -> UseLock | None:
    """Lock ``version_name``'s :data:`IN_USE` file (created 0600 when missing).

    A launch takes it shared and ``inheritable``, so the exec'd Claude Code
    keeps the lock for its whole lifetime (and its children with it); pruning
    takes it exclusively without waiting. Returns None only when
    ``blocking`` is False and another holder conflicts. A file removed while
    this call waited (a prune deleted the version) is opened again. Raises
    ``FileNotFoundError`` when the version directory does not exist and
    :class:`PinError` when it, the owned root or the lock file is unsafe (a
    link, another user's, not private, not a regular file)."""

    return take_use_lock_in(owned_root(environ), version_name, shared=shared, blocking=blocking,
                            inheritable=inheritable)


def take_use_lock_in(
    root: Path, version_name: str, *, shared: bool, blocking: bool = True, inheritable: bool = False,
) -> UseLock | None:
    """:func:`take_use_lock` under the owned root ``root`` (a probe that found
    the owned copy in one environment and runs it in another)."""

    from .platform import posix_fs

    path = _use_lock_path(root, version_name)
    check_private_directory(path.parent.parent)
    check_private_directory(path.parent)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(_IN_USE_TRIES):
        try:
            descriptor = os.open(path, flags, 0o600)
        except IsADirectoryError as exc:
            raise _unsafe_lock(path, "is not a regular file") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _unsafe_lock(path, "is a symbolic link") from exc
            raise
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise _unsafe_lock(path, "is not a regular file")
            if info.st_uid != os.geteuid():
                raise _unsafe_lock(path, "belongs to another user")
            if info.st_nlink > 1:
                raise _unsafe_lock(path, "has other names (hard links)")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(descriptor, 0o600)
            posix_fs.lock_descriptor(descriptor, shared=shared, blocking=blocking)
        except BlockingIOError:
            os.close(descriptor)
            return None
        except BaseException:
            os.close(descriptor)
            raise
        try:
            current = os.lstat(path)
        except FileNotFoundError:
            current = None
        if current is not None and (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
            if inheritable:
                os.set_inheritable(descriptor, True)
            return UseLock(path, descriptor, shared=shared)
        os.close(descriptor)  # removed or replaced while this call waited
    raise PinError(f"{path} keeps being replaced; run `{SETUP_COMMAND}`")


def owned_version_of(path: Path | str, environ: Mapping[str, str]) -> str | None:
    """The version whose owned copy ``path`` is (compared by real path), or None."""

    real = Path(os.path.realpath(path))
    if real.name not in ("claude", "claude.exe") or version_key(real.parent.name) is None:
        return None
    if real.parent.parent != Path(os.path.realpath(owned_root(environ))):
        return None
    return real.parent.name


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def check_file(path: Path, record: Mapping[str, Any]) -> str:
    """Full identity check of one file: regular, size, executable, sha256.

    Returns the digest. Raises ``FileNotFoundError`` when absent and
    :class:`PinError` for any other deviation (a symlink included).
    """

    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise PinError(f"{path} is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise PinError(f"{path} is not a regular file")
    if info.st_size != record["size"]:
        raise PinError(f"{path} has {info.st_size} bytes, not the verified {record['size']}")
    if not os.access(path, os.X_OK):
        raise PinError(f"{path} is not executable")
    digest = file_sha256(path)
    if digest != record["sha256"]:
        raise PinError(f"{path} does not match the verified sha256")
    return digest


def check_owned_dirs(executable: Path) -> None:
    """The owned root and the version directory are real directories.

    Raises ``FileNotFoundError`` when one is missing and :class:`PinError`
    for a symlink or anything that is not a directory."""

    for directory in (executable.parent.parent, executable.parent):
        info = os.lstat(directory)  # FileNotFoundError: not set up
        if stat.S_ISLNK(info.st_mode):
            raise PinError(f"{directory} is a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise PinError(f"{directory} is not a directory")


@dataclass(frozen=True)
class OwnedCopy:
    """A verified owned copy of the pinned Claude Code."""

    path: Path
    version: str
    platform: str
    sha256: str
    size: int


def verify_owned(
    contract: Mapping[str, Any], environ: Mapping[str, str], *, platform: str | None = None,
) -> OwnedCopy:
    """Verify the owned copy of the pin for this platform (every launch).

    Raises ``FileNotFoundError`` when it is not set up and :class:`PinError`
    for a platform the pin has no build for or a damaged copy.
    """

    platform = host_platform() if platform is None else platform
    pinned = version(contract)
    record = platform_record(contract, platform)
    if record is None:
        raise PinError(
            f"this claude-multi release has no verified Claude Code {pinned} build for {platform}"
        )
    executable = owned_path(environ, pinned, platform)
    check_owned_dirs(executable)
    digest = check_file(executable, record)
    return OwnedCopy(executable, pinned, platform, digest, record["size"])


# ---------------------------------------------------------------- pin facts

# A pin older than this many days is reported as stale (doctor Attention).
STALE_AFTER_DAYS = 30


def settings_keys(contract: Mapping[str, Any]) -> frozenset[str] | None:
    """The top-level settings keys the pinned client knows, or None when the
    contract records none (the settings-skew check is then skipped)."""

    try:
        keys = entry(contract).get("settings_keys")
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if not isinstance(keys, list):
        return None
    return frozenset(key for key in keys if isinstance(key, str))


def version_from_path(path: Path | str) -> str | None:
    """The Claude Code version a client path names, without running it.

    A native install names each build after its version
    (``.../claude/versions/2.1.286``); claude-multi's own copy is
    ``.../claude/<version>/claude``. Anything else is unknown (None)."""

    target = Path(path)
    if version_key(target.name) is not None:
        return target.name
    if target.name in ("claude", "claude.exe") and version_key(target.parent.name) is not None:
        return target.parent.name
    return None


@dataclass(frozen=True)
class InstalledClient:
    """The user's own Claude Code: the ``claude`` on PATH, by real path."""

    path: Path  # as found on PATH
    real: Path  # symlinks resolved
    version: str | None  # from the real path; None when it does not name one


def installed_client(environ: Mapping[str, str]) -> InstalledClient | None:
    """Locate the user's ``claude`` on ``environ``'s PATH (never executed)."""

    search = environ.get("PATH")
    if not search:
        return None
    found = shutil.which("claude", path=search)
    if found is None:
        return None
    real = Path(os.path.realpath(found))
    return InstalledClient(Path(found), real, version_from_path(real))


def copy_state(contract: Mapping[str, Any], environ: Mapping[str, str], *,
               platform: str | None = None, retained_root: Path | None = None) -> tuple[str, str | None]:
    """A cheap look at the owned copy (metadata only; the launch hashes it).

    ``("ready", None)``: a regular, executable file of the verified size;
    ``("pending", None)``: missing, and a retained copy of the verified size
    will be copied by the next launch; ``("no build" | "not set up" |
    "damaged", text)`` otherwise, ``text`` naming the remedy."""

    platform = host_platform() if platform is None else platform
    pinned = version(contract)
    record = platform_record(contract, platform)
    if record is None:
        return "no build", (f"this claude-multi release has no verified Claude Code {pinned} "
                            f"build for {platform}")
    executable = owned_path(environ, pinned, platform)
    try:
        check_owned_dirs(executable)
        info = os.lstat(executable)
    except FileNotFoundError:
        if retained_root is not None:
            candidate = Path(retained_root) / pinned
            try:
                if (not os.path.islink(candidate) and candidate.is_file()
                        and candidate.stat().st_size == record["size"]):
                    return "pending", None
            except OSError:
                pass
        return "not set up", not_set_up_text(pinned)
    except (PinError, OSError) as exc:
        return "damaged", (f"the claude-multi copy of Claude Code {pinned} cannot be used: {exc} — "
                           f"run `{SETUP_COMMAND}` to replace it")
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_size != record["size"] or not os.access(executable, os.X_OK)):
        return "damaged", (f"the claude-multi copy of Claude Code {pinned} is damaged — "
                           f"run `{SETUP_COMMAND}` to replace it")
    return "ready", None


def pin_age_days(contract: Mapping[str, Any], today: datetime.date) -> int | None:
    """Days since the pin was verified, or None when the date is unreadable."""

    try:
        verified = datetime.date.fromisoformat(entry(contract)["verified_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return (today - verified).days


def staleness(contract: Mapping[str, Any], today: datetime.date,
              user_version: str | None) -> str | None:
    """Doctor's staleness line, or None.

    Stale when the pin was verified more than :data:`STALE_AFTER_DAYS` days
    ago or the user's own Claude Code is newer. The text states the fact and
    what brings a newer pin (a claude-multi release); it names no command,
    because no command of this release changes the pin."""

    pinned = version(contract)
    reasons = []
    age = pin_age_days(contract, today)
    if age is not None and age > STALE_AFTER_DAYS:
        reasons.append(f"it was verified {age} days ago")
    user_key, pinned_key = version_key(user_version or ""), version_key(pinned)
    if user_key is not None and pinned_key is not None and user_key > pinned_key:
        reasons.append(f"your Claude Code is {user_version}")
    if not reasons:
        return None
    return (f"this claude-multi runs Claude Code {pinned}, and {' and '.join(reasons)}: "
            "a claude-multi release that verifies a newer Claude Code brings it; until then "
            f"managed sessions keep running {pinned} and your own claude is not affected")
