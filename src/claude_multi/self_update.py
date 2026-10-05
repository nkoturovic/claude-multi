"""Bundle self-update: check, plan, apply and roll back an installed release.

The installation layout is the one ``packaging/install.sh`` creates::

    <data root>/install/
      versions/<version>/      one unpacked bundle per version (MANIFEST.json, bin/, …)
      current  -> versions/<v> what every launcher file runs
      previous -> versions/<v> what a rollback returns to (the one older version kept)
      .lock/pid                held while an install, update or rollback runs
      installer.json           launcher files and PATH lines the installer wrote

A release publishes ``MANIFEST.json`` (version, release date, state format,
Claude Code version and one asset record per target), the bundles, and
``SHA256SUMS`` with its ``SHA256SUMS.sshsig`` signature. ``check`` fetches
the small files, verifies the signature and the manifest's checksum;
``plan`` says what an update changes (size, restart class, Claude Code pin,
state format and whether a rollback stays possible); ``apply`` downloads,
verifies, unpacks, puts the incoming release's Claude Code in place, flips
``current`` and keeps one previous version; ``rollback`` flips back unless
the state moved past what that version reads (forward migration only).

``previous`` always names the version that was current before the last
switch, whatever the version numbers: after a rollback it is the newer
version, so a second rollback undoes the first. The two links change
together (:func:`switch`): the links as they were are recorded durably in
``<root>/.switch.json`` before the first one changes and the record goes once
both are in place, so an interruption between them is put back by
:func:`finish_switch` instead of leaving both links on one version. Cleanup
keeps ``current``, ``previous`` and every version a running gateway executes
from; when that cannot be known nothing is removed, and such a version is
never replaced in place.

Network access, signature checks and the safety transaction are injected
(:class:`Transport`, a verifier callable, ``hold``, ``protected``,
``inhibit`` and ``prune_copies``; :mod:`claude_multi.release_update`
supplies the real ones), so this module never opens a connection itself.
They are required: an update that changes the gateway binary is refused
while ``hold`` reports a reason. ``inhibit``'s context records each step as
a phase of the caller's gateway inhibition; an interruption after the
installation started changing leaves that record, and :func:`tidy` (under
the install lock) finishes what the interrupted run left behind.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping, Protocol

from . import paths, state, strict_json, trust
from .errors import ClaudeMultiError
from .platform import posix_fs
from .sessions import STATE_MARKER, UNMARKED_STATE_VERSION

INSTALL_DIR = "install"
VERSIONS = "versions"
CURRENT = "current"
PREVIOUS = "previous"
LOCK_DIR = ".lock"
DOWNLOADS = ".downloads"
SWITCH_RECORD = ".switch.json"
MANIFEST = "MANIFEST.json"
SUMS = trust.SUMS_NAME
SIGNATURE = trust.SUMS_NAME + trust.SIGNATURE_SUFFIX
TARGETS = ("darwin-arm64", "darwin-x86_64", "linux-aarch64", "linux-x86_64")
METADATA_LIMIT = 1024 * 1024

RUNTIME = "runtime/python/bin/python3"
RESTART_LAUNCHER = "launcher"
RESTART_GATEWAY = "gateway"

_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-dev)?")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CHUNK = 1 << 20


class UpdateError(ClaudeMultiError):
    """An update, check or rollback cannot proceed; nothing was changed
    unless the message says so."""


class Interrupted(UpdateError):
    """An update or rollback stopped after the installation started
    changing: its gateway inhibition stays recorded until the next run
    finishes it (``cancelled``: the user interrupted it)."""

    def __init__(self, message: str, *, phase: str, cancelled: bool = False, remedy: str | None = None) -> None:
        super().__init__(message, remedy=remedy)
        self.phase = phase
        self.cancelled = cancelled


# ------------------------------------------------------------------ versions


def version_key(version: str) -> tuple[int, int, int, int]:
    """Order ``X.Y.Z[-dev]``: a development build sorts before its release."""

    match = _VERSION.fullmatch(version)
    if match is None:
        raise UpdateError(f"not a claude-multi version: {version!r}")
    major, minor, patch, dev = match.groups()
    return (int(major), int(minor), int(patch), 0 if dev else 1)


def _release_version(value: object, where: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None or value.endswith("-dev"):
        raise UpdateError(f"{where}: not a release version: {value!r}")
    return value


# ------------------------------------------------------------------ manifests


@dataclass(frozen=True)
class InstalledVersion:
    """One unpacked bundle under ``versions/``, from its ``MANIFEST.json``."""

    version: str
    path: Path
    target: str
    release_date: str
    state_format: int
    claude_code: str
    gateway_sha256: str


@dataclass(frozen=True)
class Installation:
    root: Path
    current: InstalledVersion
    previous: InstalledVersion | None


@dataclass(frozen=True)
class Asset:
    name: str
    target: str
    size: int
    sha256: str
    tar_sha256: str
    gateway_sha256: str


@dataclass(frozen=True)
class Release:
    """A verified release, narrowed to one target's asset. ``claude_platforms``
    is the release's per-platform Claude Code record (``{platform: {sha256,
    size}}``), when the manifest lists it."""

    version: str
    release_date: str
    state_format: int
    claude_code: str
    asset: Asset
    claude_platforms: Mapping[str, Mapping[str, Any]] | None = None

    def claude_size(self, platform: str) -> int | None:
        record = (self.claude_platforms or {}).get(platform)
        size = record.get("size") if isinstance(record, dict) else None
        return size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else None


def _text(document: Mapping[str, object], key: str, where: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise UpdateError(f"{where}: {key} is missing or not a string")
    return value


def _count(document: Mapping[str, object], key: str, where: str) -> int:
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise UpdateError(f"{where}: {key} is missing or not a positive integer")
    return value


def _object(document: Mapping[str, object], key: str, where: str) -> Mapping[str, object]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise UpdateError(f"{where}: {key} is missing or not an object")
    return value


def _common(document: object, where: str) -> tuple[Mapping[str, object], str, str, int, str]:
    """``(document, version, release_date, state_format, claude_code)``."""

    if not isinstance(document, dict):
        raise UpdateError(f"{where}: not a JSON object")
    if document.get("format") != 1 or document.get("name") != "claude-multi":
        raise UpdateError(f"{where}: not a claude-multi manifest (format 1)")
    release_date = _text(document, "release_date", where)
    if not _DATE.fullmatch(release_date):
        raise UpdateError(f"{where}: release_date must be YYYY-MM-DD")
    claude = _text(_object(document, "claude_code", where), "version", f"{where} claude_code")
    return (document, _text(document, "version", where), release_date,
            _count(document, "state_format", where), claude)


def read_bundle_manifest(path: Path, *, version: str | None = None, target: str | None = None) -> InstalledVersion:
    """Validate ``<bundle>/MANIFEST.json`` (extra keys are allowed)."""

    where = str(path / MANIFEST)
    try:
        document = strict_json.load(path / MANIFEST)
    except (OSError, strict_json.StrictJSONError) as exc:
        raise UpdateError(f"{where}: unreadable ({exc})") from exc
    document, found_version, release_date, state_format, claude = _common(document, where)
    if _VERSION.fullmatch(found_version) is None:
        raise UpdateError(f"{where}: invalid version {found_version!r}")
    found_target = document.get("target")
    if found_target not in TARGETS:
        raise UpdateError(f"{where}: unknown target {found_target!r}")
    gateway = document.get("gateway")
    digest = gateway.get("sha256") if isinstance(gateway, dict) else None
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        raise UpdateError(f"{where}: gateway.sha256 is missing")
    if version is not None and found_version != version:
        raise UpdateError(f"{where}: names version {found_version}, expected {version}")
    if target is not None and found_target != target:
        raise UpdateError(f"{where}: names target {found_target}, expected {target}")
    return InstalledVersion(found_version, path, str(found_target), release_date, state_format, claude, digest)


def parse_release(manifest: bytes, sums: Mapping[str, str], target: str) -> Release:
    """The release manifest, checked against the verified ``SHA256SUMS``."""

    if sums.get(MANIFEST) != hashlib.sha256(manifest).hexdigest():
        raise UpdateError(f"the release {MANIFEST} does not match the signed {SUMS}")
    try:
        document = strict_json.loads(manifest)
    except strict_json.StrictJSONError as exc:
        raise UpdateError(f"the release {MANIFEST} is not valid JSON ({exc})") from exc
    document, version, release_date, state_format, claude = _common(document, f"release {MANIFEST}")
    version = _release_version(version, f"release {MANIFEST}")
    assets = _object(document, "assets", f"release {MANIFEST}")
    name = f"claude-multi-{version}-{target}.tar.gz"
    entry = assets.get(name)
    if not isinstance(entry, dict):
        raise UpdateError(f"release {version} has no bundle for {target}")
    where = f"release {MANIFEST} {name}"
    size = _count(entry, "size", where)
    digests = [_text(entry, key, where) for key in ("sha256", "tar_sha256", "gateway_sha256")]
    if entry.get("target") != target or not all(_HEX64.fullmatch(d) for d in digests):
        raise UpdateError(f"{where}: malformed asset record")
    if sums.get(name) != digests[0]:
        raise UpdateError(f"{where}: the signed {SUMS} does not list this bundle with the same checksum")
    platforms = _object(document, "claude_code", f"release {MANIFEST}").get("platforms")
    if platforms is not None and not (isinstance(platforms, dict) and all(
            isinstance(key, str) and isinstance(value, dict) for key, value in platforms.items())):
        raise UpdateError(f"release {MANIFEST}: claude_code.platforms is malformed")
    return Release(version, release_date, state_format, claude, Asset(name, target, size, *digests), platforms)


# ------------------------------------------------------------------ installation


def install_root(environ: Mapping[str, str] | None = None) -> Path:
    return paths.data_root(os.environ if environ is None else environ) / INSTALL_DIR


def _link_version(root: Path, name: str) -> str | None:
    link = root / name
    if not link.is_symlink():
        return None
    target = os.readlink(link)
    head, _, version = target.partition("/")
    if head != VERSIONS or _VERSION.fullmatch(version) is None:
        raise UpdateError(f"{link} points at {target!r}, not at an installed version")
    return version


def read_installation(root: Path) -> Installation | None:
    """The installation under ``root``; None when nothing is installed there."""

    current = _link_version(root, CURRENT)
    if current is None:
        return None
    installed = read_bundle_manifest(root / VERSIONS / current, version=current)
    previous_version = _link_version(root, PREVIOUS)
    previous = None
    if previous_version is not None and previous_version != current:
        try:
            previous = read_bundle_manifest(root / VERSIONS / previous_version, version=previous_version,
                                            target=installed.target)
        except UpdateError:
            previous = None  # a damaged previous version cannot be rolled back to
    return Installation(root, installed, previous)


def installed_pins(environ: Mapping[str, str], *,
                   protected: "Callable[[Path], frozenset[str] | None] | None" = None) -> dict[str, str]:
    """``{Claude Code version: why}`` for the pins the installed release keeps:
    the ``current`` and ``previous`` bundles' pins (as the links say, whatever
    the version order) and the pin of the version a running gateway executes
    from. Empty when no release is installed; :class:`UpdateError` when the
    installation or the running gateway's version cannot be read (then no
    owned copy may be pruned)."""

    root = install_root(environ)
    installation = read_installation(root)
    if installation is None:
        return {}
    keep = {installation.current.claude_code: f"the installed release's pin ({installation.current.version})"}
    if installation.previous is not None:
        keep.setdefault(installation.previous.claude_code,
                        f"the previous installed release's pin ({installation.previous.version})")
    if protected is not None:
        running = protected(root)
        if running is None:
            raise UpdateError("whether a running gateway uses an installed release cannot be checked")
        for version in sorted(running):
            bundle = read_bundle_manifest(root / VERSIONS / version, version=version)
            keep.setdefault(bundle.claude_code, f"the running gateway's release pins it ({version})")
    return keep


def state_version(state_root: Path) -> int:
    """The state format on disk (a missing marker is the unmarked format)."""

    marker = Path(state_root) / STATE_MARKER
    if not os.path.lexists(marker):
        return UNMARKED_STATE_VERSION
    if marker.is_symlink() or not marker.is_file():
        raise UpdateError(f"the state marker {marker} is not a regular file; run 'claude-multi doctor'")
    raw = marker.read_bytes()
    if not re.fullmatch(rb"[0-9]+\n?", raw):
        raise UpdateError(f"the state marker {marker} is damaged; run 'claude-multi doctor'")
    return int(raw)


class _InstallLock:
    """``<root>/.lock`` (a directory with the holder's pid), shared with install.sh."""

    def __init__(self, root: Path) -> None:
        self.path = root / LOCK_DIR

    def __enter__(self) -> "_InstallLock":
        if not self.path.parent.is_dir():
            state.ensure_private_dir(self.path.parent)
        for _attempt in range(2):
            try:
                self.path.mkdir(mode=0o700)
            except FileExistsError:
                holder = self._holder()
                if holder is None:  # a holder between mkdir and writing its pid, or a crash there
                    raise UpdateError(f"another install or update holds {self.path}",
                                      remedy=f"if none is running, remove {self.path}") from None
                if _alive(holder):
                    raise UpdateError(f"another install or update is running (process {holder})") from None
                stale = self.path.with_name(f"{LOCK_DIR}.stale.{os.getpid()}")
                try:
                    os.rename(self.path, stale)
                    shutil.rmtree(stale, ignore_errors=True)
                except OSError:
                    pass
                continue
            (self.path / "pid").write_text(f"{os.getpid()}\n")
            return self
        raise UpdateError("another install or update is running")

    def _holder(self) -> int | None:
        try:
            text = (self.path / "pid").read_text().strip()
        except OSError:
            return None
        return int(text) if text.isdigit() else None

    def __exit__(self, *_exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def install_lock(root: Path) -> "_InstallLock":
    """The install lock (``<root>/.lock``), shared with ``install.sh``."""

    return _InstallLock(root)


def tidy(root: Path) -> tuple[str, ...]:
    """Finish what an interrupted install, update or rollback left in
    ``root`` (the caller holds the install lock): a version moved aside
    (``versions/.replaced.<v>.<pid>``) comes back when ``versions/<v>`` is
    missing, and staging trees, partial downloads and half-made links are
    removed. Returns what was put back. A switch that stopped between its
    two links is :func:`finish_switch`'s."""

    restored = []
    versions = root / VERSIONS
    try:
        entries = sorted(versions.iterdir()) if versions.is_dir() else []
    except OSError as exc:
        raise UpdateError(f"{versions} cannot be read ({exc})") from exc
    for entry in entries:
        name = entry.name
        if name.startswith(".replaced."):
            version = name[len(".replaced."):].rpartition(".")[0]
            if _VERSION.fullmatch(version) and not os.path.lexists(versions / version) and entry.is_dir() \
                    and not entry.is_symlink():
                os.rename(entry, versions / version)
                restored.append(version)
                continue
        if name.startswith((".replaced.", ".staging.")):
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    shutil.rmtree(root / DOWNLOADS, ignore_errors=True)
    for link in (*root.glob(f".{CURRENT}.tmp.*"), *root.glob(f".{PREVIOUS}.tmp.*")):
        if link.is_symlink():
            link.unlink()
    for partial in root.glob(f"{SWITCH_RECORD}.tmp.*"):  # a switch record never put in place
        if partial.is_file() and not partial.is_symlink():
            partial.unlink()
    if os.path.lexists(root):
        _sync(root)
    return tuple(restored)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _flip(root: Path, name: str, version: str) -> None:
    _point(root, name, f"{VERSIONS}/{version}")


def _point(root: Path, name: str, target: str) -> None:
    temporary = root / f".{name}.tmp.{os.getpid()}"
    if os.path.lexists(temporary):
        temporary.unlink()
    os.symlink(target, temporary)
    os.replace(temporary, root / name)


def _sync(root: Path) -> None:
    try:
        posix_fs.fsync_directory(root)
    except OSError as exc:
        raise UpdateError(f"the new links in {root} are in place but could not be made durable: {exc}") from exc


def _link_text(root: Path, name: str) -> str | None:
    """What the link ``name`` holds (None when there is none)."""

    path = root / name
    if not os.path.lexists(path):
        return None
    if not path.is_symlink():
        raise UpdateError(f"{path} is not a link; nothing was changed",
                          remedy="move it away, then run sh install.sh --repair")
    return os.readlink(path)


def _switch_record(root: Path) -> dict[str, str | None]:
    """The links an interrupted :func:`switch` recorded before it changed
    them (refuses an unreadable record: nothing is changed then)."""

    path = root / SWITCH_RECORD
    remedy = f"point {root / CURRENT} at the version you want, then remove {path}"
    try:
        if path.is_symlink() or not path.is_file():
            raise UpdateError(f"{path} is not a regular file")
        document = strict_json.loads(path.read_bytes())
    except (OSError, strict_json.StrictJSONError, UpdateError) as exc:
        raise UpdateError(f"the record of an interrupted switch is unreadable ({exc}); the links were left as they "
                          "are", remedy=remedy) from exc
    before = document.get("before") if isinstance(document, dict) else None
    if (not isinstance(document, dict) or document.get("format") != 1 or not isinstance(before, dict)
            or set(before) != {CURRENT, PREVIOUS}
            or not all(value is None or (isinstance(value, str) and value and "\0" not in value)
                       for value in before.values())):
        raise UpdateError(f"{path} is not a switch record; the links were left as they are", remedy=remedy)
    return {CURRENT: before[CURRENT], PREVIOUS: before[PREVIOUS]}


def _refuse_unfinished_switch(root: Path) -> None:
    record = root / SWITCH_RECORD
    if os.path.lexists(record):
        raise UpdateError(f"an interrupted switch of the installed version is not finished ({record}); "
                          "nothing was changed",
                          remedy="sh install.sh --repair (or, after an interrupted update, claude-multi update "
                                 "again) puts the links back")


def switch(root: Path, *, current: str, previous: str | None = None) -> None:
    """Point ``current`` (and ``previous``, when given; otherwise it stays)
    at installed versions, the two as one step (the caller holds the install
    lock): the links as they are now and the ones wanted are recorded
    durably in ``<root>/.switch.json`` before the first link changes, and
    the record is removed once both are in place and durable. A run that
    stops in between leaves the record, and :func:`finish_switch` puts the
    recorded links back."""

    for version in (current, previous):
        if version is not None and (_VERSION.fullmatch(version) is None or not (root / VERSIONS / version).is_dir()):
            raise UpdateError(f"{version} is not an installed version in {root}; nothing was changed")
    _refuse_unfinished_switch(root)
    record = root / SWITCH_RECORD
    before = {CURRENT: _link_text(root, CURRENT), PREVIOUS: _link_text(root, PREVIOUS)}
    after = {CURRENT: f"{VERSIONS}/{current}",
             PREVIOUS: f"{VERSIONS}/{previous}" if previous is not None else before[PREVIOUS]}
    temporary = root / f"{SWITCH_RECORD}.tmp.{os.getpid()}"
    data = (json.dumps({"format": 1, "before": before, "after": after}, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, record)
    _sync(root)
    if previous is not None:
        _flip(root, PREVIOUS, previous)
    _flip(root, CURRENT, current)
    _sync(root)
    record.unlink()
    _sync(root)


def finish_switch(root: Path) -> str | None:
    """Put back what an interrupted :func:`switch` changed (the caller holds
    the install lock): ``current`` and ``previous`` as its record names them
    from before, then the record goes. Idempotent; None when no switch was
    interrupted. An unreadable record changes nothing and raises."""

    path = root / SWITCH_RECORD
    if not os.path.lexists(path):
        return None
    before = _switch_record(root)
    try:
        for name in (PREVIOUS, CURRENT):
            target = before[name]
            link = root / name
            if target is not None:
                _point(root, name, target)
            elif os.path.lexists(link):
                if not link.is_symlink():
                    raise UpdateError(f"{link} is not a link; the switch record {path} was kept",
                                      remedy="move it away, then run sh install.sh --repair")
                link.unlink()
        _sync(root)
        path.unlink()
        _sync(root)
    except OSError as exc:
        raise UpdateError(f"the links an interrupted switch changed cannot be put back ({exc}); its record "
                          f"{path} was kept") from exc
    shown = ", ".join(f"{name} {(target or 'none').removeprefix(VERSIONS + '/')}"
                      for name, target in ((CURRENT, before[CURRENT]), (PREVIOUS, before[PREVIOUS])))
    return f"put the installed versions back as they were before an interrupted switch ({shown})"


# ------------------------------------------------------------------ transport


class Transport(Protocol):
    """Release file access. ``version=None`` means the latest release."""

    def fetch(self, name: str, version: str | None, limit: int) -> bytes:
        ...

    def download(self, name: str, version: str, destination: Path, size: int) -> None:
        ...


Verifier = Callable[[bytes, bytes], None]
Hold = Callable[[], "str | None"]


class DirectoryTransport:
    """One release laid out in a local directory (``--from-dir`` style)."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, name: str) -> Path:
        if "/" in name or name.startswith("."):
            raise UpdateError(f"not a release file name: {name!r}")
        return self.directory / name

    def fetch(self, name: str, version: str | None, limit: int) -> bytes:
        path = self._path(name)
        try:
            with open(path, "rb") as handle:
                data = handle.read(limit + 1)
        except OSError as exc:
            raise UpdateError(f"the release has no {name} ({exc.strerror})") from exc
        if len(data) > limit:
            raise UpdateError(f"the release {name} is larger than {limit} bytes")
        return data

    def download(self, name: str, version: str, destination: Path, size: int) -> None:
        try:
            with open(self._path(name), "rb") as source, open(destination, "wb") as out:
                copied = 0
                for chunk in iter(lambda: source.read(_CHUNK), b""):
                    copied += len(chunk)
                    if copied > size:
                        raise UpdateError(f"{name} is larger than the {size} bytes the release lists")
                    out.write(chunk)
        except OSError as exc:
            raise UpdateError(f"cannot read {name} from the release ({exc.strerror})") from exc


def signature_verifier(allowed_signers: str, *, now: float | None = None) -> Verifier:
    """The release verifier: ``SHA256SUMS.sshsig`` against ``allowed_signers``."""

    signers = trust.parse_allowed_signers(allowed_signers)

    def verify(sums: bytes, signature: bytes) -> None:
        trust.verify(sums, signature, signers, now=now)

    return verify


# ------------------------------------------------------------------ check / plan


@dataclass(frozen=True)
class CheckResult:
    """``available``: newer than installed; ``current``: the same version;
    ``older``: the installed version is newer than the latest release."""

    status: str
    installed: str
    release: Release


def _verified_sums(transport: Transport, verifier: Verifier, version: str) -> dict[str, str]:
    sums = transport.fetch(SUMS, version, METADATA_LIMIT)
    signature = transport.fetch(SIGNATURE, version, METADATA_LIMIT)
    try:
        verifier(sums, signature)
        return trust.parse_sums(sums)
    except trust.TrustError as exc:
        raise UpdateError(f"the release {SUMS} is not trusted: {exc}") from exc


def check(installation: Installation, transport: Transport, verifier: Verifier) -> CheckResult:
    """Fetch and verify the latest release's metadata (no bundle download)."""

    manifest = transport.fetch(MANIFEST, None, METADATA_LIMIT)
    try:
        latest = strict_json.loads(manifest)
    except strict_json.StrictJSONError as exc:
        raise UpdateError(f"the release {MANIFEST} is not valid JSON ({exc})") from exc
    version = _release_version(latest.get("version") if isinstance(latest, dict) else None, f"release {MANIFEST}")
    release = parse_release(manifest, _verified_sums(transport, verifier, version), installation.current.target)
    order = (version_key(release.version) > version_key(installation.current.version)) - (
        version_key(release.version) < version_key(installation.current.version))
    status = {1: "available", 0: "current", -1: "older"}[order]
    return CheckResult(status, installation.current.version, release)


@dataclass(frozen=True)
class Plan:
    from_version: str
    to_version: str
    asset: str
    size: int
    restart: str
    claude_change: tuple[str, str] | None
    state_change: tuple[int, int] | None
    rollback_blocked: bool
    steps: tuple[str, ...]
    claude_size: int | None = None


def _mib(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MiB"


def plan(installation: Installation, release: Release, *, state: int, platform: str | None = None) -> Plan:
    """What applying ``release`` changes; refuses a release that is not newer
    or cannot read the state on disk. ``platform`` (a Claude Code platform
    key) sizes the download of a changed Claude Code pin."""

    current = installation.current
    if version_key(release.version) <= version_key(current.version):
        raise UpdateError(f"claude-multi {current.version} is installed; {release.version} is not newer")
    if state > release.state_format:
        raise UpdateError(
            f"{release.version} reads state formats up to {release.state_format}, "
            f"but the state on disk is format {state}"
        )
    restart = RESTART_GATEWAY if release.asset.gateway_sha256 != current.gateway_sha256 else RESTART_LAUNCHER
    claude_change = (current.claude_code, release.claude_code) if release.claude_code != current.claude_code else None
    state_change = (current.state_format, release.state_format) if release.state_format > current.state_format else None
    steps = [
        f"download {release.asset.name} ({_mib(release.asset.size)}) and verify it against the signed checksums",
        f"install {release.version} next to {current.version}; {current.version} stays available "
        "for 'claude-multi update --rollback'",
    ]
    claude_size = release.claude_size(platform) if (claude_change is not None and platform) else None
    if claude_change is not None:
        download = f" ({_mib(claude_size)})" if claude_size else ""
        steps.append(f"{release.version} runs Claude Code {claude_change[1]} (now {claude_change[0]}): a matching "
                     f"copy on this computer is used, otherwise it is downloaded{download} from Anthropic's "
                     "release storage and verified, before anything is switched")
    if state_change is not None:
        steps.append(f"the first run of {release.version} upgrades the state format from {state_change[0]} "
                     f"to {state_change[1]}; after that, rolling back to {current.version} is refused")
    if restart == RESTART_GATEWAY:
        steps.append("the gateway changes: restart it when no session needs it (claude-multi gateway restart)")
    else:
        steps.append("only the launcher changes: new sessions use it; the running gateway keeps running")
    return Plan(current.version, release.version, release.asset.name, release.asset.size, restart,
                claude_change, state_change, state_change is not None, tuple(steps), claude_size)


# ------------------------------------------------------------------ apply


def _inside(name: str, top: str) -> bool:
    return name == top or name.startswith(top + "/")


def _check_members(members: list[tarfile.TarInfo], top: str) -> None:
    seen: set[str] = set()
    for member in members:
        normal = posixpath.normpath(member.name)
        if member.name.startswith("/") or normal != member.name.rstrip("/") or not _inside(normal, top):
            raise UpdateError(f"the bundle entry {member.name!r} is outside {top}/")
        if member.issym():
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(normal), member.linkname))
            if member.linkname.startswith("/") or not _inside(resolved, top):
                raise UpdateError(f"the bundle link {member.name!r} points outside {top}/")
        elif member.islnk():
            if posixpath.normpath(member.linkname) not in seen:
                raise UpdateError(f"the bundle hard link {member.name!r} names no earlier entry")
        elif not (member.isfile() or member.isdir()):
            raise UpdateError(f"the bundle entry {member.name!r} is not a file, directory or link")
        seen.add(normal)


def _verify_download(path: Path, asset: Asset) -> None:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    if size != asset.size or digest.hexdigest() != asset.sha256:
        raise UpdateError(f"{asset.name} does not match the signed checksum; nothing was installed")
    inner = hashlib.sha256()
    try:
        with gzip.open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(_CHUNK), b""):
                inner.update(chunk)
    except (OSError, EOFError) as exc:
        raise UpdateError(f"{asset.name} is not a gzip archive ({exc})") from exc
    if inner.hexdigest() != asset.tar_sha256:
        raise UpdateError(f"{asset.name} content does not match the release manifest")


def _unpack(archive: Path, staging: Path, top: str) -> Path:
    staging.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            _check_members(members, top)
            if hasattr(tarfile, "data_filter"):
                tar.extractall(staging, members=members, filter="data")
            else:  # pragma: no cover - interpreters without extraction filters
                tar.extractall(staging, members=members)
    except (OSError, tarfile.TarError) as exc:
        raise UpdateError(f"cannot unpack {archive.name}: {exc}") from exc
    unpacked = staging / top
    if not unpacked.is_dir() or sorted(p.name for p in staging.iterdir()) != [top]:
        raise UpdateError(f"{archive.name} does not hold exactly {top}/")
    return unpacked


def _smoke(bundle: Path) -> None:
    """The bundle's interpreter must run on this host before it becomes current."""

    interpreter = bundle / RUNTIME
    try:
        result = subprocess.run([str(interpreter), "-I", "-c", "print('ok')"], capture_output=True, text=True,
                                timeout=60, stdin=subprocess.DEVNULL, env={"PATH": os.defpath, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"the new version's runtime does not run on this system ({exc}); nothing was changed") from exc
    if result.returncode != 0 or result.stdout.strip() != "ok":
        raise UpdateError("the new version's runtime does not run on this system; nothing was changed")


Protected = Callable[[], "frozenset[str] | None"]
# The caller's gateway inhibition: a context whose value records each phase
# (``advance(phase)``; see :mod:`claude_multi.install_txn`).
Inhibit = Callable[[], ContextManager[Any]]
AcquirePin = Callable[[Path, Release], None]
# The Claude Code copies' cleanup for the release now current (its bundle):
# the lines that say what was removed and what was kept in use.
PruneCopies = Callable[[Path, Release], "list[str]"]


def _advance(held: Any, phase: str) -> None:
    """Record ``phase`` in the caller's inhibition (when its context gives one)."""

    advance = getattr(held, "advance", None)
    if advance is not None:
        advance(phase)


def _prune(root: Path, keep: set[str], protected: frozenset[str] | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Remove versions outside ``keep`` and ``protected``: ``(removed, kept for
    a running gateway)``. ``protected`` None (unknown) removes no version."""

    removed: list[str] = []
    kept: list[str] = []
    versions = root / VERSIONS
    for entry in sorted(versions.iterdir()):
        if entry.name in keep:
            continue
        if not entry.name.startswith("."):
            if protected is None or entry.name in protected:
                kept.append(entry.name)
                continue
        if entry.is_symlink() or not entry.is_dir():
            entry.unlink()
        else:
            shutil.rmtree(entry)
        if not entry.name.startswith("."):
            removed.append(entry.name)
    return tuple(removed), tuple(kept)


@dataclass(frozen=True)
class Applied:
    version: str
    previous: str | None
    restart: str
    removed: tuple[str, ...]
    kept: tuple[str, ...] = ()
    kept_unknown: bool = False
    copies: tuple[str, ...] = ()


def _gateway_refusal(hold: Hold) -> None:
    reason = hold()
    if reason:
        raise UpdateError(f"the gateway cannot be replaced now: {reason}; nothing was changed",
                          remedy="keep the gateway running until claude-multi gateway status shows the hold "
                                 "cleared, then run the update again")


def apply(installation: Installation, release: Release, planned: Plan, transport: Transport, *,
          state_root: Path, hold: Hold, protected: Protected, inhibit: Inhibit,
          prune_copies: PruneCopies, acquire_pin: AcquirePin | None = None) -> Applied:
    """Install ``release`` as ``current``; the old current becomes ``previous``.

    Under the install lock and the caller's inhibition (``inhibit``, which
    also re-checks ownership): the gateway hold when the gateway binary
    changes, the state format, the running gateway's installation
    (``protected``), then the verified download, the incoming Claude Code
    (``acquire_pin``, when the pin changes) and only then the switch; then
    the cleanup of old versions and of Claude Code copies
    (``prune_copies``). Each step is a phase of the inhibition
    (``download``, ``pin``, ``replace``, ``switch``, ``prune``).
    """

    root = installation.root
    if (planned.from_version, planned.to_version) != (installation.current.version, release.version):
        raise UpdateError("the plan does not match this installation and release; check again")
    with _InstallLock(root), inhibit() as held:
        _refuse_unfinished_switch(root)
        fresh = read_installation(root)
        if fresh is None or fresh.current.version != planned.from_version:
            raise UpdateError("the installation changed since the plan was made; check again")
        if planned.restart == RESTART_GATEWAY:
            _gateway_refusal(hold)
        if state_version(state_root) > release.state_format:
            raise UpdateError(f"{release.version} cannot read the state on disk; nothing was changed")
        guarded = protected()
        if os.path.lexists(root / VERSIONS / release.version) and (guarded is None or release.version in guarded):
            raise UpdateError(f"{release.version} is installed and the running gateway executes it (or that cannot "
                              "be checked), so it is not replaced in place; nothing was changed",
                              remedy="restart the gateway (claude-multi gateway restart), then update again")
        old = fresh.current.version
        _advance(held, "download")
        downloads = root / DOWNLOADS
        downloads.mkdir(mode=0o700, exist_ok=True)
        archive = downloads / release.asset.name
        partial = downloads / f".{release.asset.name}.part"
        top = f"claude-multi-{release.version}-{release.asset.target}"
        staging = root / VERSIONS / f".staging.{os.getpid()}"
        try:
            transport.download(release.asset.name, release.version, partial, release.asset.size)
            _verify_download(partial, release.asset)
            os.replace(partial, archive)
            shutil.rmtree(staging, ignore_errors=True)
            unpacked = _unpack(archive, staging, top)
            bundle = read_bundle_manifest(unpacked, version=release.version, target=release.asset.target)
            if (bundle.state_format, bundle.gateway_sha256, bundle.claude_code) != (
                    release.state_format, release.asset.gateway_sha256, release.claude_code):
                raise UpdateError(f"{release.asset.name} does not match the release manifest")
            _smoke(unpacked)
            if acquire_pin is not None and release.claude_code != fresh.current.claude_code:
                _advance(held, "pin")
                acquire_pin(unpacked, release)
            _advance(held, "replace")
            destination = root / VERSIONS / release.version
            if os.path.lexists(destination):
                replaced = root / VERSIONS / f".replaced.{release.version}.{os.getpid()}"
                shutil.rmtree(replaced, ignore_errors=True)
                os.rename(destination, replaced)
            os.rename(unpacked, destination)
            _advance(held, "switch")
            switch(root, current=release.version, previous=old if old != release.version else None)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(downloads, ignore_errors=True)
        _advance(held, "prune")
        guarded = protected()
        removed, kept = _prune(root, {release.version, old}, guarded)
        copies = prune_copies(root / VERSIONS / release.version, release)
    return Applied(release.version, old, planned.restart, removed, kept, guarded is None, tuple(copies))


# ------------------------------------------------------------------ rollback


@dataclass(frozen=True)
class RolledBack:
    version: str
    previous: str
    restart: str


def rollback(root: Path, *, state_root: Path, hold: Hold, inhibit: Inhibit,
             confirmed: Installation | None) -> RolledBack:
    """Make ``previous`` current again (and the old current ``previous``);
    refused when the state moved past it, or when the installation is no
    longer the one ``confirmed`` (what the user agreed to roll back: its
    current and previous versions, compared under the install lock and the
    inhibition). Only links change: no version is removed, so a running
    gateway's installation stays."""

    with _InstallLock(root), inhibit() as held:
        _refuse_unfinished_switch(root)
        installation = read_installation(root)
        if installation is None:
            raise UpdateError(f"nothing is installed in {root}")
        previous = installation.previous
        if previous is None:
            raise UpdateError("there is no previous version to roll back to")
        current = installation.current
        if confirmed is None or (current, previous) != (confirmed.current, confirmed.previous):
            shown = f"{confirmed.current.version} -> {confirmed.previous.version}" if (
                confirmed is not None and confirmed.previous is not None) else "nothing"
            raise UpdateError(
                f"the installation changed since the rollback was confirmed ({shown} was confirmed; it is now "
                f"{current.version} -> {previous.version}); nothing was changed",
                remedy="run claude-multi update --rollback again to see what it would do now")
        on_disk = state_version(state_root)
        if on_disk > previous.state_format:
            raise UpdateError(
                f"cannot roll back to {previous.version}: the state was upgraded to format {on_disk}, "
                f"which {previous.version} cannot read (it reads up to {previous.state_format}); nothing was changed",
                remedy=f"stay on {current.version}; state is only ever migrated forward",
            )
        restart = RESTART_GATEWAY if previous.gateway_sha256 != current.gateway_sha256 else RESTART_LAUNCHER
        if restart == RESTART_GATEWAY:
            _gateway_refusal(hold)
        _advance(held, "switch")
        switch(root, current=previous.version, previous=current.version)
    return RolledBack(previous.version, current.version, restart)
