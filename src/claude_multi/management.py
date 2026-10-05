"""Restart-bound management keys: private slots and one ownership-checked loopback read.

Truth table (A = active, N = next, D = disabled marker, P = start selection):

  state               A       N       D       client / gateway exec
  absent              -       -       -       off
  active              key     -       -       A, only when P matches A
  staged              key     key     -       A, only when P matches A
  disabled            any     any     present off (marker always wins)
  re-enable-pending   -       key     -       off

An active file without a matching P is also pending, never proof of installation.
P is management-key.prepared: a private SHA-256 digest, not a secret, separate
from the mandatory render receipt. It selects a key only at a proven stopped
boundary; it does not claim the subsequent exec or readiness check succeeded.
A failed start leaves no old listener to receive the selected replacement.

Transitions:
* ensure: absent -> re-enable-pending; all other states preserved.
* rotation: active/staged -> staged (retain A); absent/pending -> pending;
  disabled -> pending (remove A before clearing D, clear D last).
* disable: write D FIRST, then remove N, A and P; interrupted cleanup stays off.
* init --prepare-start ONLY: clear P first; unless stopped is proven, leave
  slots alone and stay off. D wins. Otherwise ensure N if needed, promote N
  by atomic A replacement then N removal, and publish P LAST. A crash before
  the last write stays off; retry uses the same bytes, including A == N.
* ordinary init/reload/start-check and unprepared run may ensure/stage but
  NEVER promote. Prepared run only reads P and A, without locks or chmod.

All writers hold one leaf management-key.lock, never the api-key lock. Readers
are lock-free; they recheck the marker and selection after reading A. Rotation
keeps the selected A available. A concurrent restart can still race one read;
callers must never retry authentication failures. File absence does not prove
that the running process is keyless. These slots are HOME-relative, not XDG.
"""

from __future__ import annotations

from claude_multi import endpoint

import errno
import hashlib
import http.client
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from . import errors, quota, service, state
from .platform import posix_fs

KEY_FILE = "management-key"
STAGED_FILE = "management-key.next"
DISABLED_FILE = "management-key.disabled"
PREPARED_FILE = "management-key.prepared"
ALLOWLIST_PATCH = "cli-proxy-api-management-readonly-allowlist.patch"
PINNED_BIN_ENV = "CLAUDE_MULTI_PROXY_BIN"
PATCHES_ENV = "CLAUDE_MULTI_PROXY_PATCHES"
# The install channel every wrapper sets. Management is a channel policy:
# only the Nix channel, whose wrapper pins the gateway binary and attests its
# patch list, enables it; a bundle, a source checkout or no channel keeps it
# off, whatever binary or patch list the environment names.
CHANNEL_ENV = "CLAUDE_MULTI_CHANNEL"
MANAGEMENT_CHANNEL = "nix"
UNAVAILABLE = "management is unavailable in this build"
_KEY_SHAPE = re.compile(r"^[0-9a-f]{64}$")
# Public, value-free policy input to proxy.exec_signature.
EXEC_POLICY = "047-v1:private-selected-key;stopped-prepare-start-only;allowlist-build;after-scrub"


class ManagementKeyError(errors.ClaudeMultiError, RuntimeError):
    """A redacted key/path failure, with an actionable non-following repair."""

    def __init__(self, reason: str, filename: str = KEY_FILE, *,
                 then: str = "claude-multi-proxy rotate-management-key"):
        self.reason = reason
        self.filename = filename
        path = f'"$HOME/.config/claude-multi/{filename}"'
        if reason in ("symlink", "foreign owner"):
            # The 0700 directory is owner-controlled, so unlinking a foreign
            # file or a link there works; never follow or touch a link target.
            remedy = f"unlink -- {path} && {then}"
        elif reason == "not a regular file":
            # No recursive deletion, and never overwrite an existing backup.
            backup = f'"$HOME/.config/claude-multi/{filename}.rejected"'
            remedy = f"{posix_fs.move_aside_command(path, backup)} && {then}"
        elif reason == "invalid shape" and filename == STAGED_FILE:
            remedy = then  # rotation replaces a readable staged slot; A stays usable
        elif reason == "unsafe directory":
            remedy = 'restore an owner-controlled, non-symlink 0700 "$HOME/.config/claude-multi", then ' + then
        elif reason == "write failed":
            # An I/O failure (e.g. ENOSPC) says nothing about the slot: fix the
            # cause and retry the same command, never a different one.
            remedy = 'free space or fix permissions in "$HOME/.config/claude-multi", then ' + then
        else:
            # remove_private refuses symlinks/foreign ownership (handled above),
            # but removes an owner-controlled regular file with bad shape or mode.
            remedy = f"claude-multi-proxy disable-management-key && {then}"
        message = (f"{filename} could not be written; management reads may still be on"
                   if reason == "write failed" else f"{filename} is unusable ({reason}); quota stays off")
        super().__init__(message, remedy=remedy)


@dataclass(frozen=True)
class KeyState:
    active: str
    unusable_reason: str | None
    staged: bool
    staged_since: float | None
    disabled: bool
    pending: bool = False


def key_dir(home: Path) -> Path:
    return Path(home) / ".config" / "claude-multi"


def management_channel(environ: Mapping[str, str]) -> bool:
    """True only on the channel that may enable management (exact value)."""
    return environ.get(CHANNEL_ENV) == MANAGEMENT_CHANNEL


def allowlist_build(environ: Mapping[str, str]) -> bool:
    """Management may be enabled: the Nix channel, a pinned gateway binary
    and the read-only allowlist patch in the attested patch list. The patch
    list alone never enables it (a bundle ships the same series)."""
    return management_channel(environ) and bool(environ.get(PINNED_BIN_ENV, "").strip()) and ALLOWLIST_PATCH in {
        name.strip() for name in environ.get(PATCHES_ENV, "").split(",")
    }


def _directory(home: Path, *, create: bool = False) -> Path:
    directory = key_dir(home)
    try:
        if create:
            state.ensure_private_dir(directory)
        elif os.path.lexists(directory):
            state._check_directory(directory)
    except OSError:
        raise ManagementKeyError("unsafe directory") from None
    return directory


def _read(home: Path, filename: str) -> bytes | None:
    path = _directory(home) / filename
    try:
        return state.read_private(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        reason = "unsafe file"
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                reason = "symlink"
            elif not stat.S_ISREG(info.st_mode):
                reason = "not a regular file"
            elif info.st_uid != os.geteuid():
                reason = "foreign owner"
        except OSError:
            pass
        raise ManagementKeyError(reason, filename) from None


def _unsafe_path(path: Path, filename: str, *, then: str) -> None:
    """Classify a slot path without reading any secret bytes."""
    if os.path.lexists(path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ManagementKeyError("symlink", filename, then=then)
        if not stat.S_ISREG(info.st_mode):
            raise ManagementKeyError("not a regular file", filename, then=then)
        if info.st_uid != os.geteuid():
            raise ManagementKeyError("foreign owner", filename, then=then)


def _slot(home: Path, filename: str) -> str | None:
    raw = _read(home, filename)
    if raw is None:
        return None
    try:
        value = raw.decode("ascii").strip()
    except UnicodeError:
        value = ""
    if not _KEY_SHAPE.fullmatch(value):
        raise ManagementKeyError("invalid shape", filename)
    return value


def _disabled(home: Path) -> bool:
    # Even an unsafe marker disables reads. Writers validate it before removal.
    return os.path.lexists(key_dir(home) / DISABLED_FILE)


def _digest(key: str) -> bytes:
    return hashlib.sha256(key.encode("ascii")).hexdigest().encode("ascii") + b"\n"


def read_active(home: Path) -> str | None:
    """Only the start-selected active slot; no locks, writes or chmod."""
    if _disabled(home):
        return None
    key = _slot(home, KEY_FILE)
    if key is None:
        return None
    selected = _read(home, PREPARED_FILE)
    if selected != _digest(key) or _disabled(home):
        return None
    return key


def key_state(home: Path) -> KeyState:
    directory = key_dir(home)
    staged, since = False, None
    try:
        since = (directory / STAGED_FILE).lstat().st_mtime
        staged = True
    except OSError:
        pass
    disabled = _disabled(home)
    reason = None
    try:
        key = _slot(home, KEY_FILE)
        active = "present" if key is not None else "absent"
        pending = not disabled and (staged or key is not None) and read_active(home) is None
    except ManagementKeyError as exc:
        active, reason, pending = "unusable", exc.reason, False
    return KeyState(active, reason, staged, since, disabled, pending)


def _write_key(directory: Path, filename: str) -> None:
    state.atomic_write(directory / filename, (secrets.token_hex(32) + "\n").encode("ascii"))


def _ensure(home: Path, directory: Path) -> str:
    if _disabled(home):
        return "disabled"
    if _slot(home, KEY_FILE) is not None:
        return "present"
    if _slot(home, STAGED_FILE) is None:
        _write_key(directory, STAGED_FILE)
        return "created"
    return "pending"


def ensure(home: Path) -> str:
    directory = _directory(home, create=True)
    try:
        with state.FileLock(directory / KEY_FILE):
            return _ensure(home, directory)
    except OSError:
        raise ManagementKeyError("unsafe file") from None


def stage_rotation(home: Path) -> str:
    directory = _directory(home, create=True)
    try:
        with state.FileLock(directory / KEY_FILE):
            disabled = _disabled(home)
            if disabled:
                # Validate the marker; remove any interrupted-disable active
                # before clearing it. No old active is exposed on re-enable.
                _read(home, DISABLED_FILE)
                state.remove_private(directory / KEY_FILE)
            else:
                _slot(home, KEY_FILE)  # never silently repair unusable input
            _read(home, STAGED_FILE)  # refuse unsafe modes/paths before replacing
            _write_key(directory, STAGED_FILE)
            if disabled:
                state.remove_private(directory / DISABLED_FILE)
            return "staged" if _slot(home, KEY_FILE) is not None else "created"
    except OSError:
        raise ManagementKeyError("unsafe file") from None


def disable(home: Path) -> None:
    directory = _directory(home, create=True)
    # A repair here continues the disable; rotating would re-enable reads.
    again = "claude-multi-proxy disable-management-key"
    try:
        with state.FileLock(directory / KEY_FILE):
            # Invalidate the start selection before touching the marker.
            # Readers are lock-free and need both, so reads stay off even
            # while an unsafe marker's printed repair has removed it.
            _unsafe_path(directory / PREPARED_FILE, PREPARED_FILE, then=again)
            state.atomic_write(directory / PREPARED_FILE, b"")
            _unsafe_path(directory / DISABLED_FILE, DISABLED_FILE, then=again)
            state.atomic_write(directory / DISABLED_FILE, b"")
            for filename in (STAGED_FILE, KEY_FILE, PREPARED_FILE):
                path = directory / filename
                _unsafe_path(path, filename, then=again)
                state.remove_private(path)
    except OSError:
        raise ManagementKeyError("write failed", DISABLED_FILE, then=again) from None


def check_staged(home: Path) -> None:
    """Lock-free validation of the staged slot; raises its category repair."""
    _slot(home, STAGED_FILE)


def prepare_start(home: Path, *, stopped: bool | Callable[[], bool]) -> tuple[str, ...]:
    """Unsandboxed start-only promotion; optional failures never block start.

    The caller proves listener absence AND gateway PID absence, without sending
    credentials: ``stopped`` is the backend's ``GatewayObservation.stopped_proof``
    (``service.gateway_observation``), or a callable evaluated under the key
    lock, at the promotion boundary, that revalidates it
    (``service.confirm_stopped``). Unknown is False. The published selection
    is a preparation receipt, never proof that the following exec or
    readiness check succeeded.
    """
    try:
        directory = _directory(home, create=True)
        with state.FileLock(directory / KEY_FILE):
            # Invalidate selection before doing anything which could change A.
            state.atomic_write(directory / PREPARED_FILE, b"")
            if _disabled(home):
                return ("disabled",)
            if not (stopped() if callable(stopped) else stopped):
                return ("start-unconfirmed",)
            _ensure(home, directory)
            active = _slot(home, KEY_FILE)
            notes: tuple[str, ...] = ()
            try:
                staged = _slot(home, STAGED_FILE)
            except ManagementKeyError:
                staged = None
                notes = ("staged-unusable",)
            if staged is not None:
                state.atomic_write(directory / KEY_FILE, (staged + "\n").encode("ascii"))
                state.remove_private(directory / STAGED_FILE)
                active, notes = staged, ("promoted",)
            if active is not None:
                state.atomic_write(directory / PREPARED_FILE, _digest(active))
            return notes
    except ManagementKeyError as exc:
        return (f"unusable:{exc.reason}",)
    except OSError:
        return ("unusable:unsafe file",)


def prepare_for_exec(home: Path) -> tuple[str | None, tuple[str, ...]]:
    """Read-only selection, including for run --prepared; never promote."""
    try:
        if _disabled(home):
            return None, ("disabled",)
        key = read_active(home)
        return key, (() if key is not None else ("pending",))
    except ManagementKeyError as exc:
        return None, (f"unusable:{exc.reason}",)


TIMEOUT_SECONDS = 1.5
ManagementGetter = Callable[[str, str], tuple[int, bytes]]


def _port(base_url: str) -> int:
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", base_url)
    if match is None or not 0 < int(match[1]) <= 65535:
        raise ValueError("management requires a numeric loopback URL")
    return int(match[1])


def send_request(connection, key: str) -> tuple[int, bytes]:
    """One canonical GET. No retry, redirect, proxy env, or error-body read."""
    connection.request("GET", quota.AUTH_FILES_PATH, headers={"X-Management-Key": key})
    response = connection.getresponse()
    return response.status, response.read(quota.MAX_BODY_BYTES + 1) if response.status == 200 else b""


def default_get(base_url: str, key: str, *, timeout: float = TIMEOUT_SECONDS) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", _port(base_url), timeout=timeout)
    try:
        return send_request(connection, key)
    finally:
        connection.close()


def pool_status(home: Path, gateway: Mapping, *, get: ManagementGetter | None = None,
                owner_check=None, attention=None, environ: Mapping[str, str] | None = None) -> quota.PoolStatus:
    """Read only a selected active key, then make at most one authenticated read.

    Outside the management channel nothing is read or sent: quota is
    unavailable in this build, whatever key files the home holds.
    """
    if not management_channel(os.environ if environ is None else environ):
        return quota.PoolStatus("unavailable")
    info = key_state(home)
    if info.disabled:
        return quota.PoolStatus("disabled")
    if info.active == "unusable":
        # Re-read only to obtain the same category-specific, value-free repair.
        try:
            read_active(home)
        except ManagementKeyError as exc:
            return quota.PoolStatus("key-unusable", key_problem=exc.reason, remedy=exc.remedy,
                                    key_file=exc.filename)
        return quota.PoolStatus("key-unusable")
    if info.pending:
        return quota.PoolStatus("pending")
    if info.active == "absent":
        return quota.PoolStatus("no-key")
    try:
        base_url = endpoint.gateway_endpoint(gateway).base_url
        _port(base_url)
        verdict = (owner_check or service.listener_owner)(base_url)
        policy = service.owner_policy(verdict)
        if policy == "refuse":
            return quota.PoolStatus("not-ours")
        if policy == "attention":
            message = service.owner_attention(base_url, verdict)
            if attention is not None:
                attention(message)
            else:
                print(f"claude-multi: Attention: {message}", file=sys.stderr)
        key = read_active(home)
    except ManagementKeyError as exc:
        return quota.PoolStatus("key-unusable", key_problem=exc.reason, remedy=exc.remedy,
                                key_file=exc.filename)
    except Exception:
        return quota.PoolStatus("error")
    if key is None:
        return quota.PoolStatus("disabled" if _disabled(home) else "pending")
    read_at = quota._now()
    try:
        code, body = (get or default_get)(base_url, key)
        if code == 200:
            try:
                credentials = quota.parse_auth_files(body)
            except quota.QuotaParseError:
                return quota.PoolStatus("malformed", code, read_at)
            return quota.PoolStatus("ok", code, read_at, credentials)
        status = {401: "mismatch", 403: "refused", 404: "management-off"}.get(code, "error")
        return quota.PoolStatus(status, code, read_at)
    except ConnectionRefusedError:
        return quota.PoolStatus("down", read_at=read_at)
    except Exception:
        # Never store or print transport exception text (it may contain a key).
        return quota.PoolStatus("error", read_at=read_at)
