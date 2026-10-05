"""Acquire claude-multi's own copy of the pinned Claude Code.

The owned copy (``pin.owned_path``) is put in place by copying a file whose
size and SHA-256 equal the contract's record for this platform from, in
order: an explicit ``--claude-from PATH``, the retained
``pinned-clients/<version>`` copy, the native Claude Code versions
directories, and the ``claude`` on PATH (resolved). Only when none matches
is a download from ``downloads.claude.ai`` offered, with its exact request
plan put to the user first (nothing is sent without a yes).

Writes go to a 0700 directory through a same-directory temporary file that is
verified, made 0755, fsynced and renamed into place. Anthropic's installer is
never run, and the user's own install is never moved, linked or modified:
every local source is only read. One acquisition per version runs at a time
on a host (an advisory lock next to the version directory).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import errors, paths, pin, release_manifest, state, strict_json
from .platform import posix_fs


DOWNLOAD_URL = "https://downloads.claude.ai/claude-code-releases/{version}/{platform}/{binary}"
DOWNLOAD_TRIES = 3
DOWNLOAD_TIMEOUT_SECONDS = 60.0
DOWNLOAD_QUESTION = "Download it now? [y/N] "
CHUNK = 1 << 20
INDEX_NAME = "pins.json"
INDEX_LIMIT = 32


class AcquireError(errors.ClaudeMultiError, RuntimeError):
    """The owned copy could not be put in place (nothing half-written stays)."""


class AcquireBusy(AcquireError):
    """Another acquisition of the same version holds the host lock."""


class _WrongBytes(AcquireError):
    """The downloaded bytes are not the pinned build (never retried)."""


@dataclass(frozen=True)
class Candidate:
    """One local file that may be the pinned build."""

    kind: str  # "claude-from" | "retained" | "native" | "path"
    path: Path

    def label(self, environ: Mapping[str, str]) -> str:
        names = {
            "claude-from": "the file given with --claude-from",
            "retained": "the retained copy",
            "native": "your Claude Code versions directory",
            "path": "the claude on your PATH",
        }
        return f"{names[self.kind]} ({paths.display(self.path, environ)})"


def native_versions_dirs(environ: Mapping[str, str]) -> list[Path]:
    """Where a native Claude Code install keeps its versions (read only).

    ``$XDG_DATA_HOME/claude/versions`` when set, ``~/.local/share/claude/
    versions``, and on Windows ``%USERPROFILE%\\.local\\share\\claude\\versions``.
    """

    found: list[Path] = []
    xdg = environ.get("XDG_DATA_HOME")
    if xdg:
        found.append(Path(xdg) / "claude" / "versions")
    found.append(paths.upstream_versions_dir(environ))
    profile = environ.get("USERPROFILE")
    if profile:
        found.append(Path(profile) / ".local" / "share" / "claude" / "versions")
    unique: list[Path] = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique


def local_candidates(
    version: str, platform: str, environ: Mapping[str, str], *,
    claude_from: Path | None = None, retained_root: Path | None = None,
) -> list[Candidate]:
    """Existing local files to compare with the pin, in the documented order."""

    ordered: list[Candidate] = []
    if claude_from is not None:
        ordered.append(Candidate("claude-from", Path(os.path.realpath(claude_from))))
    root = paths.retained_root(environ) if retained_root is None else retained_root
    ordered.append(Candidate("retained", root / version))
    names = (version, f"{version}.exe") if platform.startswith("win32-") else (version,)
    for directory in native_versions_dirs(environ):
        for name in names:
            ordered.append(Candidate("native", directory / name))
    on_path = shutil.which("claude", path=environ.get("PATH", os.defpath))
    if on_path:
        ordered.append(Candidate("path", Path(os.path.realpath(on_path))))
    unique: list[Candidate] = []
    seen: set[Path] = set()
    for item in ordered:
        if item.path in seen or not os.path.lexists(item.path):
            continue
        seen.add(item.path)
        unique.append(item)
    return unique


def matches(path: Path, record: Mapping[str, Any]) -> bool:
    """Size first (cheap), then the full SHA-256 of a regular file."""

    try:
        resolved = Path(os.path.realpath(path))
        if not resolved.is_file() or resolved.stat().st_size != record["size"]:
            return False
        return pin.file_sha256(resolved) == record["sha256"]
    except OSError:
        return False


def _record(contract: Mapping[str, Any], platform: str) -> Mapping[str, Any]:
    record = pin.platform_record(contract, platform)
    if record is None:
        raise AcquireError(
            f"this claude-multi release has no verified Claude Code {pin.version(contract)} "
            f"build for {platform}"
        )
    return record


def retained_source(
    contract: Mapping[str, Any], environ: Mapping[str, str], retained_root: Path, *,
    platform: str | None = None,
) -> Path | None:
    """The hash-identical retained copy of the pin, if any (read only)."""

    platform = pin.host_platform() if platform is None else platform
    record = pin.platform_record(contract, platform)
    if record is None:
        return None
    candidate = Path(retained_root) / pin.version(contract)
    if os.path.islink(candidate) or not candidate.is_file():
        return None
    return candidate if matches(candidate, record) else None


def version_lock(environ: Mapping[str, str], version: str) -> state.FileLock:
    """The host lock one acquisition of ``version`` holds (pruning takes it
    too, so it never removes a version being put in place)."""

    root = state.ensure_private_dir(pin.owned_root(environ))
    return state.FileLock(root / f".acquire-{version}")


_lock = version_lock


def _free_bytes(path: Path) -> int | None:
    try:
        usage = os.statvfs(path)
    except (OSError, AttributeError):
        return None
    return usage.f_bavail * usage.f_frsize


def partial_path(destination: Path) -> Path:
    """Where a download keeps its bytes until they verify (resumable)."""

    return destination.parent / f".{destination.name}.part"


def _resumable_bytes(destination: Path, expected: int) -> int:
    """The size of a partial download a resume continues (0 when there is
    none, it is not a regular file, or it is larger than the build: then it
    is discarded and the download starts over)."""

    try:
        info = os.lstat(partial_path(destination))
    except OSError:
        return 0
    if not stat.S_ISREG(info.st_mode) or info.st_size > expected:
        return 0
    return info.st_size


def _check_version_dir(destination: Path, environ: Mapping[str, str]) -> None:
    """Refuse a version directory that is a link or not a directory (it is
    never followed, changed or removed)."""

    directory = destination.parent
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AcquireError(f"cannot check {paths.display(directory, environ)}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        problem = "is a symbolic link"
    elif not stat.S_ISDIR(info.st_mode):
        problem = "is not a directory"
    else:
        return
    shown = paths.display(directory, environ)
    raise AcquireError(
        f"{shown} {problem}; claude-multi keeps its copy only in a real directory there — "
        f"remove it (rm {shown}; a link's target is not touched), then rerun `{pin.SETUP_COMMAND}`"
    )


def _prepare_destination(environ: Mapping[str, str], version: str, platform: str,
                         record: Mapping[str, Any], *, resume: bool = False) -> Path:
    """The verified-ready destination; refuses when the filesystem lacks room
    for the build (a resumed download: for the bytes it still needs)."""

    destination = pin.owned_path(environ, version, platform)
    try:
        state.ensure_private_dir(destination.parent)
    except OSError as exc:
        raise AcquireError(f"cannot prepare {paths.display(destination.parent, environ)}: {exc}") from exc
    needed = record["size"] - (_resumable_bytes(destination, record["size"]) if resume else 0)
    free = _free_bytes(destination.parent)
    if free is not None and free < needed:
        raise AcquireError(
            f"not enough free space for Claude Code {version}: it needs "
            f"{_megabytes(needed)} in {paths.display(destination.parent, environ)}"
        )
    return destination


def _publish(temporary: Path, destination: Path) -> None:
    os.chmod(temporary, 0o755)
    os.replace(temporary, destination)
    posix_fs.fsync_directory(destination.parent)


def copy_verified(source: Path, record: Mapping[str, Any], destination: Path) -> None:
    """Copy ``source`` to ``destination`` only if the copied bytes verify.

    The digest is computed over the bytes written, so a source that changes
    during the copy never lands. The source is only read.
    """

    fd, name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as out, Path(source).open("rb") as reader:
            while chunk := reader.read(CHUNK):
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                if size > record["size"]:
                    break
            out.flush()
            os.fsync(out.fileno())
        if size != record["size"] or digest.hexdigest() != record["sha256"]:
            raise AcquireError(f"{source} changed while it was copied; nothing was installed")
        _publish(temporary, destination)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _index_path(environ: Mapping[str, str]) -> Path:
    return pin.owned_root(environ) / INDEX_NAME


def read_index(environ: Mapping[str, str]) -> dict[str, str]:
    """``{launcher version: Claude Code version}`` of the launchers that set
    up an owned copy here (unreadable: empty)."""

    try:
        document = strict_json.loads(state.read_private(_index_path(environ)))
    except (OSError, ValueError, errors.ClaudeMultiError):
        return {}
    launchers = document.get("launchers") if isinstance(document, dict) else None
    if not isinstance(launchers, dict):
        return {}
    return {key: value for key, value in launchers.items()
            if isinstance(key, str) and isinstance(value, str) and pin.version_key(value)}


def record_launcher(environ: Mapping[str, str], launcher_version: str, version: str) -> None:
    """Remember which Claude Code version this launcher release runs
    (the prune rule keeps the previous release's pin). Best effort."""

    try:
        launchers = read_index(environ)
        if launchers.get(launcher_version) == version:
            return
        launchers[launcher_version] = version
        if len(launchers) > INDEX_LIMIT:
            ordered = sorted(launchers, key=_launcher_key)
            for stale in ordered[: len(launchers) - INDEX_LIMIT]:
                del launchers[stale]
        state.atomic_write(_index_path(environ), (json.dumps(
            {"version": 1, "launchers": dict(sorted(launchers.items()))}, indent=2) + "\n").encode())
    except OSError:
        pass


def _launcher_key(value: str) -> tuple[int, ...]:
    return pin.version_key(value.removesuffix("-dev")) or ()


def migrate_retained(
    contract: Mapping[str, Any], environ: Mapping[str, str], retained_root: Path, *,
    platform: str,
) -> Path | None:
    """Copy (never move) a hash-identical retained copy into the owned layout.

    Returns the owned path, or None when there is nothing to copy or another
    acquisition holds the lock. Raises :class:`AcquireError` when the copy
    fails.
    """

    source = retained_source(contract, environ, retained_root, platform=platform)
    if source is None:
        return None
    version = pin.version(contract)
    record = _record(contract, platform)
    try:
        lock = _lock(environ, version)
        if not lock.acquire(blocking=False):
            return None
    except OSError as exc:
        raise AcquireError(f"cannot prepare {paths.display(pin.owned_root(environ), environ)}: {exc}") from exc
    try:
        destination = _prepare_destination(environ, version, platform, record)
        if not _owned_ok(destination, record):
            copy_verified(source, record, destination)
        _record_running_launcher(environ, version)
        return destination
    except OSError as exc:
        raise AcquireError(f"copying {paths.display(source, environ)} failed: {exc}") from exc
    finally:
        lock.release()


def _owned_ok(destination: Path, record: Mapping[str, Any]) -> bool:
    """The full owned-copy check a launch makes: real directories, then the
    file (regular, size, executable, sha256)."""

    try:
        pin.check_owned_dirs(destination)
        pin.check_file(destination, record)
        return True
    except (OSError, pin.PinError):
        return False


def _record_running_launcher(environ: Mapping[str, str], version: str, launcher_version: str | None = None) -> None:
    from . import __version__

    record_launcher(environ, launcher_version or __version__, version)


# ------------------------------------------------------------- download


def download_url(version: str, platform: str) -> str:
    if pin.version_key(version) is None or platform not in pin.PLATFORMS:
        raise AcquireError("invalid Claude Code version or platform")
    return DOWNLOAD_URL.format(version=version, platform=platform, binary=pin.binary_name(platform))


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.0f} MiB"


@dataclass(frozen=True)
class DownloadPlan:
    """The one request a download sends, shown before the question."""

    version: str
    platform: str
    url: str
    size: int
    destination: Path

    def lines(self, environ: Mapping[str, str]) -> tuple[str, ...]:
        return (
            f"Download plan for Claude Code {self.version} ({self.platform})",
            f"  GET {self.url}",
            "Authentication: none",
            f"Size: {self.size} bytes ({_megabytes(self.size)})",
            f"Saved to: {paths.display(self.destination, environ)}",
            "Used only if its size and sha256 equal what this claude-multi release pins.",
            release_manifest.REDIRECT_PROXY_TEXT,
            f"Up to {DOWNLOAD_TRIES} tries; an interrupted download resumes where it stopped.",
            "Anthropic's installer is not run, and your own Claude Code install is not touched.",
        )

    def text(self, environ: Mapping[str, str]) -> str:
        return "\n".join(self.lines(environ)) + "\n" + DOWNLOAD_QUESTION


def download_plan(contract: Mapping[str, Any], environ: Mapping[str, str], platform: str) -> DownloadPlan:
    version = pin.version(contract)
    record = _record(contract, platform)
    return DownloadPlan(version, platform, download_url(version, platform), record["size"],
                        pin.owned_path(environ, version, platform))


Opener = Callable[..., Any]


def _open(url: str, *, headers: Mapping[str, str], timeout: float):
    from . import tls

    request = urllib.request.Request(url, headers=dict(headers))
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls.context()),
                                       release_manifest._ReleaseRedirect()).open(request, timeout=timeout)


def _status(response: Any) -> int:
    status = getattr(response, "status", None)
    return int(status if status is not None else response.getcode())


def _hash_existing(path: Path) -> tuple["hashlib._Hash", int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as reader:
        while chunk := reader.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest, size


def download(
    plan: DownloadPlan, record: Mapping[str, Any], *, opener: Opener = _open,
    tries: int = DOWNLOAD_TRIES, timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Fetch ``plan.url`` into the owned layout, verified before it lands.

    A partial file next to the destination survives a failure, so a later
    try (this call's or a later setup's) resumes it with a Range request;
    the hash streams over the bytes as they are written.
    """

    partial = partial_path(plan.destination)
    last = "no attempt"
    for attempt in range(1, tries + 1):
        try:
            _download_once(plan, partial, record, opener=opener, timeout=timeout)
            _publish(partial, plan.destination)
            return plan.destination
        except _WrongBytes:
            raise
        except AcquireError as exc:
            last = str(exc)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError,
                release_manifest.ReleaseManifestError) as exc:
            # Never echo URLs, proxy settings or server bodies.
            last = type(exc).__name__
        if progress is not None and attempt < tries:
            progress(f"download try {attempt} of {tries} failed ({last}); retrying")
    raise AcquireError(f"the download did not complete ({last})")


def _download_once(plan: DownloadPlan, partial: Path, record: Mapping[str, Any], *,
                   opener: Opener, timeout: float) -> None:
    """One request: resume the partial file (Range) or start over; verify."""

    expected = record["size"]
    if os.path.lexists(partial):
        state._check_regular_file(partial)
        digest, have = _hash_existing(partial)
        if have > expected:
            partial.unlink()
            digest, have = hashlib.sha256(), 0
    else:
        digest, have = hashlib.sha256(), 0
    if have < expected:
        headers = {"Range": f"bytes={have}-"} if have else {}
        release_manifest._check_url(plan.url)
        with opener(plan.url, headers=headers, timeout=timeout) as response:
            release_manifest._check_url(response.geturl())
            status = _status(response)
            if status == 206 and have:
                content_range = (getattr(response, "headers", None) or {}).get("Content-Range", "")
                if not str(content_range).startswith(f"bytes {have}-"):
                    partial.unlink()
                    raise AcquireError("the server resumed at another offset; starting over")
                flags = os.O_APPEND
            elif status == 200:
                digest, have, flags = hashlib.sha256(), 0, os.O_TRUNC
            else:
                raise AcquireError(f"the server answered HTTP {status}")
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | flags | os.O_CLOEXEC, 0o600)
            with os.fdopen(descriptor, "wb") as out:
                while have < expected and (chunk := response.read(min(CHUNK, expected - have + 1))):
                    if have + len(chunk) > expected:
                        raise AcquireError("the download is larger than the pinned size")
                    out.write(chunk)
                    digest.update(chunk)
                    have += len(chunk)
                out.flush()
                os.fsync(out.fileno())
    if have != expected:
        raise AcquireError(f"the download stopped at {have} of {expected} bytes")
    if digest.hexdigest() != record["sha256"]:
        partial.unlink()
        raise _WrongBytes("the downloaded file does not match the pinned sha256; it was discarded")


def unavailable_remedy(version: str) -> str:
    return (
        f"To set it up without the download: install Claude Code {version} with Anthropic's "
        f"installer yourself, then rerun `{pin.SETUP_COMMAND}`; or copy a {version} binary here and "
        f"pass `--claude-from PATH`. A newer claude-multi release may pin a version that is "
        "available: update claude-multi."
    )


# ------------------------------------------------------------- acquisition


@dataclass(frozen=True)
class AcquireOutcome:
    """What the acquisition did; ``path`` is the verified owned copy."""

    state: str  # "present" | "copied" | "downloaded" | "declined"
    version: str
    platform: str
    path: Path | None
    source: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def acquire(
    contract: Mapping[str, Any], environ: Mapping[str, str], *,
    platform: str | None = None, claude_from: Path | None = None,
    retained_root: Path | None = None, consent: Callable[[DownloadPlan], bool] | None = None,
    opener: Opener = _open, progress: Callable[[str], None] | None = None,
    launcher_version: str | None = None,
) -> AcquireOutcome:
    """Put the verified owned copy of the pin in place (``setup --step claude``).

    Without ``consent`` (or on a "no") nothing is downloaded and the outcome
    is ``declined`` with the remedies in its notes. ``launcher_version`` is
    the release the pin is recorded for in the prune index (default: the
    running launcher; an update records the incoming release).
    """

    platform = pin.host_platform() if platform is None else platform
    version = pin.version(contract)
    record = _record(contract, platform)
    if claude_from is not None and not Path(claude_from).is_file():
        raise AcquireError(f"--claude-from {claude_from} is not a file")
    try:
        lock = _lock(environ, version)
        locked = lock.acquire(blocking=False)
    except OSError as exc:
        raise AcquireError(f"cannot prepare {paths.display(pin.owned_root(environ), environ)}: {exc}") from exc
    if not locked:
        raise AcquireBusy(f"another claude-multi setup is putting Claude Code {version} in place; "
                          "wait for it to finish, then rerun this step")
    try:
        destination = pin.owned_path(environ, version, platform)
        _check_version_dir(destination, environ)
        if _owned_ok(destination, record) and claude_from is None:
            _record_running_launcher(environ, version, launcher_version)
            return AcquireOutcome("present", version, platform, destination)
        notes: list[str] = []
        for candidate in local_candidates(version, platform, environ, claude_from=claude_from,
                                          retained_root=retained_root):
            if not matches(candidate.path, record):
                notes.append(f"skipped {candidate.label(environ)}: not Claude Code {version} "
                             f"for {platform} (size or sha256 differ)")
                if candidate.kind == "claude-from":
                    raise AcquireError(f"--claude-from {claude_from} is not the Claude Code {version} "
                                       f"build this release pins for {platform} (size or sha256 differ)")
                continue
            destination = _prepare_destination(environ, version, platform, record)
            try:
                copy_verified(candidate.path, record, destination)
            except OSError as exc:
                raise AcquireError(f"copying {candidate.label(environ)} failed: {exc}") from exc
            _record_running_launcher(environ, version, launcher_version)
            return AcquireOutcome("copied", version, platform, destination,
                                  candidate.label(environ), tuple(notes))
        plan = download_plan(contract, environ, platform)
        if consent is None or not consent(plan):
            notes.append(f"nothing was downloaded. {unavailable_remedy(version)}")
            return AcquireOutcome("declined", version, platform, None, None, tuple(notes))
        destination = _prepare_destination(environ, version, platform, record, resume=True)
        try:
            download(plan, record, opener=opener, progress=progress)
        except AcquireError as exc:
            raise AcquireError(f"{exc}. {unavailable_remedy(version)}") from exc
        _record_running_launcher(environ, version, launcher_version)
        return AcquireOutcome("downloaded", version, platform, destination, plan.url, tuple(notes))
    finally:
        lock.release()
