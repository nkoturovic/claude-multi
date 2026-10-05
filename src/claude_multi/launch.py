"""Launch execution for claude-multi v2.

Consumes a pure CompileResult, verifies claude-multi's own copy of the pinned
Claude Code against the native contract, performs loopback-only readiness
checks after explicit confirmation, persists the session snapshot atomically,
and replaces the process via an injectable ``os.execve`` boundary. No resident
wrapper survives the exec.

Hashing contract: ``resolve_claude`` rehashes the full owned copy (≈240 MB,
roughly 0.3–1 s) on every launch as the trust anchor, before any readiness
check or state write. This is deliberate; any cache must be separately
reviewed.

Trust model: only ``<data root>/claude/<version>/claude`` (``claude.exe`` on
Windows) is executed, and only when it is a regular, non-symlinked,
executable file whose size and full SHA-256 equal the contract's record for
this platform. A missing copy is set up by ``claude-multi setup --step
claude``; a launch only copies an existing hash-identical retained copy
(``pinned-clients/<version>``) into place. The user's own Claude Code install
is never executed, moved, linked or modified. The version's use lock is
taken before the hash check and inherited across exec (:class:`PinnedCopy`),
so the running Claude Code keeps its copy from being pruned.

Accepted limitation: verification is path-based, so a determined local
attacker could swap the artifact between verification and execve (TOCTOU).
An fd/memfd-based redesign is deliberately out of scope.

Cleanup contract: if anything fails after this launch committed
state, or ``os.execve`` raises ``OSError``, cleanup is action-aware and
ownership-guarded (CAS-by-own-write on the mutation token and the exact
committed bytes). A fresh launch forgets its record, compare-and-clears the
per-CWD pointer and removes only its live scope (a pre-existing ``.prev``
survives). A resume (every relaunch is one) restores the exact pre-launch
record bytes (or overlays the hooks' lifecycle onto them when a hook wrote
meanwhile), puts the swapped-out scope back from ``.prev`` byte-exactly and
restores the prior pointer; when a newer attempt has committed, it owns the
record and the scope and cleanup touches neither. ``SystemExit``/signal
identity passes through untouched.
"""

from __future__ import annotations

from claude_multi import endpoint

import errno
import hashlib
import http.client
import os
import re
import stat
import sys
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from . import errors, paths, pin, release_manifest, scope, service, sessions, state, strict_json
from .platform import posix_fs
from .compiler import CompileResult
from .sessions import SessionStore

class LaunchError(errors.ClaudeMultiError, RuntimeError):
    """Raised when launch cannot proceed (fail closed, no effects leaked)."""


class NotSetUpError(LaunchError):
    """The owned copy of the pin is missing (and no retained copy was copied)."""


class GatewayKeyError(LaunchError):
    """The gateway key needs a file remedy, not a service restart."""


def gateway_unit_remedy(home: Path | None = None) -> str:
    """How to bring the gateway back, for the backend ``home`` records."""

    backend = service.backend_of(home)
    if backend.name == service.SYSTEMD:
        return (
            f"start it with {service.hint_code('start', backend=backend)}; "
            f"if it failed (start limit hit): {service.hint_code('recover', backend=backend)}; "
            f"why it stopped: {service.hint_code('why', backend=backend)}"
        )
    return (f"start it with {service.hint_code('start', backend=backend)}; "
            f"why it stopped: {service.hint_code('why', backend=backend)}")


def gateway_problem_text(exc: LaunchError, *, home: Path | None = None) -> str:
    return f"{exc} — {exc.remedy or gateway_unit_remedy(home)}"


_TOKEN_SHAPE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class BinaryStatus:
    """Immutable trust status of the verified owned copy."""

    inspected_path: Path
    validated_version: str
    sha256: str
    platform: str
    migrated: bool = False  # copied from the retained copy by this call


def resolve_claude(
    native_contract: dict[str, Any], *, environ: Mapping[str, str] | None = None,
    retained_root: Path | None = None, platform: str | None = None,
) -> BinaryStatus:
    """Verify claude-multi's own copy of the pinned Claude Code.

    The owned copy must be a regular, non-symlinked, executable file whose
    size and full SHA-256 match the contract's record for this platform; any
    deviation fails closed before any readiness check or state write. When
    the copy is missing and ``retained_root`` is given, a hash-identical
    retained copy (``<retained_root>/<version>``) is copied (never moved)
    into place first. ``retained_root=None`` never writes.
    """

    env = os.environ if environ is None else environ
    try:
        platform = pin.host_platform() if platform is None else platform
        version = pin.version(native_contract)
    except release_manifest.ReleaseManifestError as exc:
        raise LaunchError(f"Claude Code cannot run here: {exc}") from exc
    migrated = False
    try:
        owned = pin.verify_owned(native_contract, env, platform=platform)
    except FileNotFoundError:
        copied = None
        if retained_root is not None:
            from . import acquire  # local: the launch path rarely migrates

            try:
                copied = acquire.migrate_retained(native_contract, env, retained_root, platform=platform)
            except acquire.AcquireError as exc:
                raise LaunchError(f"{pin.not_set_up_text(version)} ({exc})") from exc
        if copied is None:
            raise NotSetUpError(pin.not_set_up_text(version)) from None
        migrated = True
        try:
            owned = pin.verify_owned(native_contract, env, platform=platform)
        except (FileNotFoundError, pin.PinError) as exc:
            raise LaunchError(f"{pin.not_set_up_text(version)} ({exc})") from exc
    except pin.PinError as exc:
        raise LaunchError(
            f"the claude-multi copy of Claude Code {version} cannot be used: {exc} — "
            f"run `{pin.SETUP_COMMAND}` to replace it"
        ) from exc
    except OSError as exc:
        raise LaunchError(
            f"the claude-multi copy of Claude Code {version} cannot be checked: {exc} — "
            f"run `{pin.SETUP_COMMAND}`"
        ) from exc
    return BinaryStatus(
        inspected_path=owned.path,
        validated_version=owned.version,
        sha256=owned.sha256,
        platform=owned.platform,
        migrated=migrated,
    )


class PinnedCopy:
    """The pin's use lock for one run of the owned copy.

    :meth:`verify` takes the shared lock on ``<owned root>/<version>/.in-use``
    (:func:`pin.take_use_lock`) and only then checks the copy. The
    descriptor is inheritable, so an exec hands the lock to Claude Code,
    which holds it for its whole lifetime (its children inherit it too, which
    only keeps a copy longer); pruning takes the lock exclusively without
    waiting and keeps every version it cannot lock. :meth:`release` closes it
    on every path that does not exec. Waits only while a prune removes the
    version (the check then finds it gone).
    """

    def __init__(self, native_contract: Mapping[str, Any], environ: Mapping[str, str] | None = None):
        self._contract = native_contract
        self._environ = os.environ if environ is None else environ
        self.lock: pin.UseLock | None = None

    def _take(self) -> bool:
        """Hold the lock; False when there is no version directory to lock
        (or no readable pin: the check reports that)."""

        if self.lock is not None:
            return True
        try:
            version = pin.version(self._contract)
        except (KeyError, TypeError, ValueError, AttributeError, errors.ClaudeMultiError):
            return False
        if pin.version_key(version) is None:
            return False
        try:
            self.lock = pin.take_use_lock(self._environ, version, shared=True, inheritable=True)
        except FileNotFoundError:
            return False
        except pin.PinError as exc:
            raise LaunchError(
                f"the claude-multi copy of Claude Code {version} cannot be used: {exc}"
            ) from exc
        except OSError as exc:
            raise LaunchError(
                f"the claude-multi copy of Claude Code {version} cannot be checked: {exc} — "
                f"run `{pin.SETUP_COMMAND}`"
            ) from exc
        return True

    def verify(self, *, retained_root: Path | None = None, platform: str | None = None) -> BinaryStatus:
        """:func:`resolve_claude` under the lock."""

        self._take()
        status = resolve_claude(self._contract, environ=self._environ,
                                retained_root=retained_root, platform=platform)
        if self.lock is None:
            # The version directory appeared during the check (a retained
            # copy was copied into place, or a setup ran meanwhile): lock it
            # and check the copy again under the lock.
            if not self._take():
                raise NotSetUpError(pin.not_set_up_text(status.validated_version))
            again = resolve_claude(self._contract, environ=self._environ, platform=platform)
            status = replace(again, migrated=status.migrated)
        return status

    def release(self) -> None:
        lock, self.lock = self.lock, None
        if lock is not None:
            lock.release()


def doctor_binary_report(
    native_contract: dict[str, Any], *, environ: Mapping[str, str] | None = None,
    retained_root: Path | None = None,
) -> tuple[list[str], list[str]]:
    """(problems, info) for the owned copy via the same check as launch.

    Doctor parity: a failure here is exactly the failure launch would hit,
    except that a missing copy which the next launch copies from a
    hash-identical retained copy is reported as set up on that launch (the
    launch migrates only a missing copy: a damaged, unsafe or unreadable one
    stays a problem whatever is retained). Doctor never writes.
    """

    env = os.environ if environ is None else environ
    try:
        status = resolve_claude(native_contract, environ=env)
    except LaunchError as exc:
        if isinstance(exc, NotSetUpError) and retained_root is not None:
            from . import acquire

            try:
                pending = acquire.retained_source(native_contract, env, retained_root)
            except (OSError, errors.ClaudeMultiError):
                pending = None
            if pending is not None:
                return [], [
                    f"Claude Code {pin.version(native_contract)}: the next launch copies the "
                    f"verified retained copy {paths.display(pending, env)} into claude-multi's "
                    "own location."
                ]
        return [f"managed Claude binary: {exc}"], []
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        # Direct-API callers may pass a malformed record; catalog loading
        # still enforces the closed schema on the normal path.
        return [f"managed Claude binary: malformed native-contract record ({exc!r})"], []
    return [], [
        f"Managed Claude {status.validated_version} verified "
        f"(sha256 {status.sha256[:12]}..., {paths.display(status.inspected_path, env)})."
    ]


@dataclass(frozen=True)
class DaemonStatus:
    """Best-effort shared-daemon status: existence/pid inspection only."""

    state: str  # "present" | "absent" | "unsupported"
    summary: str
    pid: int | None = None
    version: str | None = None


def _metadata_pid(path: Path) -> int | None:
    """Trivially read a daemon pid: plain JSON, informational-only.

    Any failure (missing, unreadable, unparseable, wrong shape) yields None;
    this line never gates anything, so no defensive parsing is warranted.
    """

    try:
        document = strict_json.loads(path.read_bytes())
    except Exception:
        return None
    pid = document.get("pid") if isinstance(document, dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return None
    return pid


def inspect_shared_daemon(
    *,
    domain_dir: Path | None = None,
    uid: int | None = None,
) -> DaemonStatus:
    """Best-effort shared-daemon status: existence plus pid when exposed.

    Never sends a request to the daemon and never reads transcripts. The
    domain must be a real directory owned by the effective uid to count as
    present; anything else reports absent. Informational only.
    """

    if uid is None:
        uid_getter = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
        if uid_getter is None:
            return DaemonStatus(
                state="unsupported",
                summary="daemon status inspection is unsupported on this platform",
            )
        uid = uid_getter()
    base = domain_dir if domain_dir is not None else Path(f"/tmp/cc-daemon-{uid}")
    try:
        base_stat = os.lstat(base)
    except OSError:
        return DaemonStatus(
            state="absent",
            summary=f"no shared-daemon domain at {base}; status not exposed",
        )
    if not stat.S_ISDIR(base_stat.st_mode) or base_stat.st_uid != uid:
        return DaemonStatus(
            state="absent",
            summary=f"no uid-owned shared-daemon domain at {base}; status not exposed",
        )
    pid = _metadata_pid(base / "metadata.json")
    detail = f"pid {pid}" if pid is not None else "pid not exposed"
    return DaemonStatus(
        state="present",
        summary=f"shared-daemon domain present at {base} ({detail})",
        pid=pid,
    )


def _default_health_get(base_url: str, health_path: str, timeout: float = 1.5) -> int:
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1":
        raise LaunchError("gateway base_url is not loopback http",
                          remedy=gateway_unit_remedy())
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        connection.request("GET", health_path)
        return connection.getresponse().status
    finally:
        connection.close()


MODELS_PATH = "/v1/models"


class GatewayOwnerError(LaunchError):
    """A different user's listener must never receive the gateway token."""


def check_listener(base_url: str, *, owner_check=None, attention=None,
                   require_proof: bool = False) -> service.OwnerVerdict:
    """The listener verdict before a token is sent (``service.owner_policy``:
    another user's listener refuses, an unconfirmed one of yours is
    Attention). ``require_proof`` refuses an unconfirmed listener too: the
    served reads of a launch, a resume and doctor send the gateway token only
    to a listener proven to be this state root's gateway, as the gateway
    start and ensure do."""

    verdict = (owner_check or service.listener_owner)(base_url)
    policy = service.owner_policy(verdict)
    if policy == "proceed":
        return verdict
    port = urllib.parse.urlsplit(base_url).port or 80
    if policy == "refuse":
        raise GatewayOwnerError(
            f"BLOCK: gateway listener on port {port} is held by {verdict.detail}; "
            "the gateway token was not sent",
            remedy=f"check the listener before launching: {service.hint_code('status')}",
        )
    if require_proof:
        raise GatewayOwnerError(
            f"BLOCK: the listener on port {port} is not proven to be the claude-multi gateway "
            f"({verdict.detail}); the gateway token was not sent",
            remedy=f"check the listener before launching: {service.hint_code('status')}",
        )
    message = service.owner_attention(base_url, verdict)
    if attention is not None:
        attention(message)
    else:
        print(f"claude-multi: Attention: {message}", file=sys.stderr)
    return verdict


def gateway_authorization(base_url: str, token: str, *, owner_check=None, attention=None,
                          require_proof: bool = False) -> dict[str, str]:
    """The single authenticated-request boundary, including injected transports."""
    check_listener(base_url, owner_check=owner_check, attention=attention, require_proof=require_proof)
    return {"Authorization": f"Bearer {token}"}


# One bounded served snapshot per refresh. Each entry
# keeps a validated id, a sanitized ``owned_by`` and a bounded ``created``;
# ``served_models`` stays the id-set projection for existing callers.
SERVED_ID = re.compile(r"^[^\s\x00-\x1f\x7f]{1,256}$")
_OWNED_BY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
SERVED_CREATED_MAX = 253402300799


@dataclass(frozen=True)
class ServedModel:
    """One ``/v1/models`` entry: local registration only, never upstream proof."""

    id: str
    owned_by: str | None = None
    created: int | None = None


def served_model(item: Any) -> ServedModel | None:
    """A validated entry, or None (a malformed id is dropped, never shown)."""

    if not isinstance(item, dict):
        return None
    model_id = item.get("id")
    if not isinstance(model_id, str) or not SERVED_ID.fullmatch(model_id):
        return None
    owned = item.get("owned_by")
    created = item.get("created")
    return ServedModel(
        id=model_id,
        owned_by=owned if isinstance(owned, str) and _OWNED_BY.fullmatch(owned) else None,
        created=created if isinstance(created, int) and not isinstance(created, bool)
        and 0 <= created <= SERVED_CREATED_MAX else None,
    )


def served_snapshot_from(result: Any) -> tuple[ServedModel, ...]:
    """Normalize a models getter's payload: an id collection (the earlier
    injected callbacks, metadata absent) or ServedModel entries."""

    models: dict[str, ServedModel] = {}
    for item in result or ():
        model = item if isinstance(item, ServedModel) else (
            ServedModel(item) if isinstance(item, str) else None)
        if model is not None and model.id not in models:
            models[model.id] = model
    return tuple(models[key] for key in sorted(models))


def _default_models_snapshot_get(
    base_url: str, token: str, timeout: float = 1.5, *, owner_check=None, attention=None,
    require_proof: bool = False,
) -> tuple[int, tuple[ServedModel, ...]]:
    """Loopback GET /v1/models; (status, served entries with metadata).

    No claude-cli User-Agent is sent, so the codex cloak cannot cosmetically
    hide the codex-pool aliases. Bearer auth; the gateway's access provider
    also accepts x-api-key.
    """

    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1":
        raise LaunchError("gateway base_url is not loopback http",
                          remedy=gateway_unit_remedy())
    headers = gateway_authorization(base_url, token, owner_check=owner_check, attention=attention,
                                    require_proof=require_proof)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        connection.request("GET", MODELS_PATH, headers=headers)
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            return response.status, ()
        payload = strict_json.loads(body.decode("utf-8"))
        entries = [served_model(item) for item in payload.get("data", [])]
        return 200, served_snapshot_from(entry for entry in entries if entry is not None)
    finally:
        connection.close()


def _default_models_get(
    base_url: str, token: str, timeout: float = 1.5, *, owner_check=None, attention=None,
    require_proof: bool = False,
) -> tuple[int, set[str]]:
    """Loopback GET /v1/models; (status, served selector ids)."""

    status, models = _default_models_snapshot_get(
        base_url, token, timeout, owner_check=owner_check, attention=attention, require_proof=require_proof)
    return status, {model.id for model in models}


def served_snapshot(
    gateway: dict[str, Any],
    token: str,
    *,
    models_get: Callable[[str, str], tuple[int, Any]] | None = None,
    owner_check=None, attention=None,
) -> tuple[tuple[ServedModel, ...] | None, int | None]:
    """(served entries, HTTP status) the running gateway serves now.

    The one ``/v1/models`` read of a refresh; :func:`served_models` is its
    id projection. An injected ``models_get`` may return an id set (absent
    metadata) or ServedModel entries. Same error contract as
    :func:`served_models`, and the same proof: the token goes only to a
    listener proven to be the gateway (``check_listener(require_proof=True)``).
    """

    try:
        base_url = endpoint.gateway_endpoint(gateway).base_url
        if models_get is None:
            status, models = _default_models_snapshot_get(
                base_url, token, owner_check=owner_check, attention=attention, require_proof=True)
        else:
            gateway_authorization(base_url, token, owner_check=owner_check, attention=attention,
                                  require_proof=True)
            status, raw = models_get(base_url, token)
            models = served_snapshot_from(raw)
    except LaunchError as exc:
        if exc.remedy is None:
            exc.remedy = gateway_unit_remedy()
        raise
    except AssertionError:
        raise  # programming errors and test isolation trips are not gateway outages
    except Exception as exc:  # connection refused, timeout, etc.
        raise LaunchError(f"local gateway models check failed: {exc}",
                          remedy=gateway_unit_remedy()) from exc
    if status != 200:
        return None, status
    return models, status


def served_models(
    gateway: dict[str, Any],
    token: str,
    *,
    models_get: Callable[[str, str], tuple[int, set[str]]] | None = None,
    owner_check=None, attention=None,
) -> tuple[set[str] | None, int | None]:
    """(selector ids, HTTP status) the running gateway serves now.

    Loopback-local registry state — never an upstream provider call. The
    ids are None on a non-200 response (the status tells the caller why —
    a 401 means the running daemon holds a different token than the
    rendered config); connection failures raise LaunchError like the
    readiness probe. A listener not proven to be the gateway raises
    :class:`GatewayOwnerError` before the token is sent.
    """

    gateway["gateway"]  # a document without the gateway block is a programming error
    try:
        base_url = endpoint.gateway_endpoint(gateway).base_url
        if models_get is None:
            status, ids = _default_models_get(base_url, token, owner_check=owner_check, attention=attention,
                                              require_proof=True)
        else:
            gateway_authorization(base_url, token, owner_check=owner_check, attention=attention,
                                  require_proof=True)
            status, ids = models_get(base_url, token)
            ids = {model.id for model in served_snapshot_from(ids)}
    except LaunchError as exc:
        if exc.remedy is None:
            exc.remedy = gateway_unit_remedy()
        raise
    except AssertionError:
        raise  # programming errors and test isolation trips are not gateway outages
    except Exception as exc:  # connection refused, timeout, etc.
        raise LaunchError(f"local gateway models check failed: {exc}",
                          remedy=gateway_unit_remedy()) from exc
    if status != 200:
        return None, status
    return ids, status


def read_gateway_token(gateway: dict[str, Any], *, home: Path | None = None) -> str:
    """Read + shape-check the loopback gateway token (never upstream)."""

    gateway_info = gateway["gateway"]
    base = home if home is not None else Path.home()
    token_path = base / gateway_info["token_file"]
    shown = paths.display(token_path, {"HOME": str(base)})
    try:
        raw = state.read_private(token_path)
    except state.StateError as exc:
        remedy = (
            "run claude-multi-proxy init (it creates the key and re-renders the gateway config)"
            if exc.errno == errno.ENOENT else
            f"make {shown} a regular file you own with mode 0600 (chmod 600 {shown}), then retry"
        )
        raise GatewayKeyError(f"gateway key file unavailable or unsafe: {exc}", remedy=remedy) from exc
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        token = ""
    if not _TOKEN_SHAPE.fullmatch(token):
        raise GatewayKeyError(
            "gateway key file has an invalid token shape",
            remedy=f"move it aside and create a fresh key: mv {shown} {shown}.invalid && claude-multi-proxy init",
        )
    return token


def check_readiness(
    gateway: dict[str, Any],
    *,
    home: Path | None = None,
    health_get: Callable[[str, str], int] | None = None,
) -> str:
    """Loopback-only readiness: gateway token file + /healthz. Never upstream."""

    gateway_info = gateway["gateway"]
    token = read_gateway_token(gateway, home=home)
    getter = health_get or _default_health_get
    try:
        status = getter(endpoint.gateway_endpoint(gateway).base_url, endpoint.gateway_endpoint(gateway).health_path)
    except LaunchError as exc:
        if exc.remedy is None:
            exc.remedy = gateway_unit_remedy(home)
        raise
    except Exception as exc:  # connection refused, timeout, etc.
        raise LaunchError(f"local gateway health check failed: {exc}",
                          remedy=gateway_unit_remedy(home)) from exc
    if status != 200:
        raise LaunchError(f"local gateway health check returned status {status}",
                          remedy=gateway_unit_remedy(home))
    return token


@dataclass
class _CwdLease:
    """Open directory fds make resume-CWD validation race resistant."""

    original_fd: int
    target_fd: int
    original_path: str
    target_path: str
    closed: bool = False

    @classmethod
    def prepare(cls, target: Any) -> "_CwdLease":
        if not isinstance(target, str) or not target.startswith("/"):
            raise LaunchError(
                f"recorded project directory {target!r} is not an absolute path"
            )
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        original_path = os.getcwd()
        original_fd = os.open(".", flags)
        try:
            target_fd = os.open(target, flags)
        except OSError as exc:
            os.close(original_fd)
            raise LaunchError(
                f"cannot open the session's original project directory {target}: {exc}; "
                "if it was renamed/moved, repair the record with `claude-multi "
                "sessions relink-runtime <managed-id> <runtime-id> --cwd <new "
                "project directory>` (or rename it back)"
            ) from exc
        lease = cls(original_fd, target_fd, original_path, target)
        try:
            # Prove the directory is enterable before any launch state commit.
            os.fchdir(target_fd)
            os.fchdir(original_fd)
        except OSError as exc:
            lease.close()
            raise LaunchError(
                f"cannot enter the session's original project directory {target}: {exc}; "
                "repair the recorded CWD before resuming"
            ) from exc
        return lease

    def enter(self) -> None:
        os.fchdir(self.target_fd)

    def restore(self) -> None:
        if not self.closed:
            os.fchdir(self.original_fd)

    def close(self) -> None:
        if self.closed:
            return
        os.close(self.target_fd)
        os.close(self.original_fd)
        self.closed = True




MIGRATION_BUSY_LAUNCH = (
    "migration or restore in progress; retry in a moment (nothing was launched)"
)
BARRIER_BUSY_LAUNCH = "nothing was launched; retry in a moment"


def disk_generation(scope_dir: Path | str) -> int:
    """The first field of a scope's ``lineup.gen`` (absent or invalid -> 0)."""

    from . import hooks  # local: hooks is a leaf the launch path reads once

    line = hooks.read_gen(scope_dir)
    if line is None:
        return 0
    head = line.split(" ", 1)[0]
    return int(head) if head.isdigit() else 0


def _restore_scope(
    store: SessionStore, session_id: str, had_live: bool, dir_fsync: Any
) -> None:
    """Put the pre-launch scope back: ``.prev`` when one was swapped out, else none."""

    if had_live:
        scope.restore_prev_scope(store.root, session_id, dir_fsync=dir_fsync)
    else:
        scope.remove_live_scope(store.root, session_id)


def perform_launch(
    result: CompileResult,
    *,
    native_contract: dict[str, Any],
    environ: dict[str, str] | None = None,
    retain_root: Path | None = None,
    **kwargs: Any,
) -> Any:
    """Execute a v4 launch (:func:`_perform_launch`) under the pin's use lock
    (:class:`PinnedCopy`): taken before the owned copy's hash check and
    inherited by the exec'd Claude Code, so pruning never removes a copy a
    launch verified or a session runs. Released here on every path that does
    not exec."""

    pinned = PinnedCopy(native_contract, environ)
    try:
        return _perform_launch(result, pinned=pinned, native_contract=native_contract,
                               environ=environ, retain_root=retain_root, **kwargs)
    finally:
        pinned.release()


def _perform_launch(
    result: CompileResult,
    *,
    record: dict[str, Any],
    store: SessionStore,
    native_contract: dict[str, Any],
    gateway: dict[str, Any],
    readiness: Callable[..., str] = check_readiness,
    execve: Callable[[str, list[str], dict[str, str]], Any] = posix_fs.exec_replace,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
    health_get: Callable[[str, str], int] | None = None,
    allow_model_relaunch: bool = False,
    expected_launch_epoch: int | None = None,
    expected_mutation_token: str | None = None,
    expected_applied_hash: str | None = None,
    expected_lineup_generation: int | None = None,
    expected_disk_generation: int | None = None,
    dir_fsync: Callable[[Path], None] | None = None,
    retain_root: Path | None = None,
    barrier_timeout: float | None = state.SERVED_BARRIER_TIMEOUT,
    revalidate: Callable[[], None] | None = None,
    pinned: PinnedCopy,
) -> Any:
    """Execute a v4 launch: verify -> readiness -> state -> execve.

    ``retain_root`` names the retained-copy root a missing owned copy may be
    copied from (a launch that may write); None verifies without writing.
    ``pinned`` verifies the copy under its use lock; the caller releases it.

    After the card and every probe, the shared migration
    hold is followed by the gateway served-change barrier, taken with a
    bounded wait (a timeout refuses with the neutral text, nothing written)
    and never held across a card or a prompt. Inside it ``revalidate`` re-checks the
    root authority and operator eligibility a second time; then the
    lifecycle lock. The barrier fd is ``O_CLOEXEC`` like the others: exec
    drops it, and a failure-owned rollback completes before it is released.

    One path for fresh launches, resumes and relaunches (a relaunch is a
    resume with a target lineup). The binary and loopback readiness are
    checked before any lock; the global migration lock is then held
    **shared** (non-blocking: a running ``migrate``/``restore-2x`` refuses
    with L-4, nothing written) and the per-session lifecycle lock blocking,
    both through ``execve`` (their fds are ``O_CLOEXEC``). Inside the lock a
    resume re-validates the record it was prepared against (epoch, token,
    ``applied_hash``, ``lineup_generation`` and the on-disk ``lineup.gen``),
    then commits: token, lead prompt, the ``.prev``/``.new`` scope swap, the
    record, the pointer. Every pre-exec failure and an exec ``OSError``
    restore the exact prior record bytes (only while the record is ours:
    CAS-by-own-write), ``.prev`` byte-exactly and the prior pointer; a
    fresh launch forgets its record and removes only its live scope.
    """

    sessions.check_state_marker(store.root)
    if not result.durable:
        raise LaunchError("--legacy was retired; every launch must be durable")
    if result.scope_plan is None or not result.scope_plan.settings.get("apiKeyHelper"):
        raise LaunchError("every launch requires the scope apiKeyHelper")
    status = pinned.verify(retained_root=retain_root)
    executable = status.inspected_path
    readiness(gateway, home=home, health_get=health_get)

    action_kind = result.session_action.kind
    try:
        stable_id = sessions.managed_id(record)
        runtime_id = sessions.runtime_session_id(record)
    except sessions.SessionError as exc:
        raise LaunchError(str(exc)) from exc
    if action_kind not in {"fresh", "resume"}:
        raise LaunchError(f"unsupported session action {action_kind!r}")
    if record.get("version") != sessions.RECORD_VERSION:
        raise LaunchError(
            f"session {stable_id}: only version-{sessions.RECORD_VERSION} records launch"
        )
    if result.session_action.managed_id != stable_id:
        raise LaunchError(
            f"compiled session action targets managed session "
            f"{result.session_action.managed_id!r}, but the record belongs to "
            f"{stable_id!r}"
        )
    if result.session_action.runtime_session_id != runtime_id:
        raise LaunchError(
            f"compiled action targets runtime session "
            f"{result.session_action.runtime_session_id!r}, but the record targets "
            f"{runtime_id!r}"
        )
    if result.scope_dir is None:
        raise LaunchError("a launch requires a compiled scope directory")
    expected_scope = scope.scope_dir(store.root, stable_id)
    if result.scope_dir != expected_scope:
        raise LaunchError(
            f"compiled scope directory {result.scope_dir} does not match the "
            f"session store scope {expected_scope}"
        )
    missing = scope.missing_hook_targets(result.scope_plan.settings)
    if missing:
        raise LaunchError(
            f"compiled hook target {missing[0]} is missing or not executable; the session's "
            "/model fence and lineup hooks would silently not run — run claude-multi doctor "
            "(a normal claude-multi command restores the hook shims), then launch again"
        )
    collisions = scope.find_cm_collisions(
        record["cwd"],
        result.passthrough_add_dirs,
        result.scope_plan.agent_names,
    )
    if collisions:
        formatted = "; ".join(
            f"{path} (agent name {name!r})" for path, name in collisions
        )
        raise LaunchError(
            "exact cm-* agent name collision outside the managed scope: "
            f"{formatted}; rename the colliding project agent — it would "
            "silently shadow a guaranteed managed definition"
        )

    # Validate and enter the authoritative original CWD before committing any
    # state; the open fds keep the exec-side chdir valid across a rename.
    cwd_lease = _CwdLease.prepare(record.get("cwd"))

    # The migration lock SHARED before the lifecycle lock, non-blocking.
    # `migrate`/`restore-2x` hold it exclusively; a launch never waits for
    # them and never nests a second hold (FileLock is not re-entrant).
    guard = sessions.migration_lock(store.root, shared=True)
    try:
        acquired = guard.acquire(blocking=False)
    except BaseException:
        cwd_lease.close()
        raise
    if not acquired:
        cwd_lease.close()
        raise LaunchError(MIGRATION_BUSY_LAUNCH)
    barrier_home = Path(home) if home is not None else paths.home(environ if environ is not None else os.environ)
    try:
        barrier = state.acquire_served_barrier(
            sessions.barrier_dir(barrier_home), timeout=barrier_timeout,
            guard=guard, guard_root=Path(store.root),
        )
    except state.BarrierBusyError as exc:
        cwd_lease.close()
        guard.release()
        raise LaunchError(f"{BARRIER_BUSY_LAUNCH} ({exc.strerror})") from exc
    except BaseException:
        cwd_lease.close()
        guard.release()
        raise
    if revalidate is not None:
        try:
            revalidate()
        except BaseException:
            cwd_lease.close()
            barrier.release()
            guard.release()
            raise

    scope_written = False
    had_live = False
    committed_bytes: bytes | None = None
    committed_token: str | None = None
    pre_existing: bytes | None = None
    prior_pointer: bytes | None = None
    lock = store.lifecycle_lock(stable_id)
    try:
        lock.acquire(blocking=True)
    except BaseException:
        cwd_lease.close()
        barrier.release()
        guard.release()
        raise

    def release_locks() -> None:
        try:
            lock.release()
        finally:
            try:
                barrier.release()
            finally:
                guard.release()

    try:
        pre_existing = store.read_record_bytes(stable_id)
        committed_record = record
        if action_kind == "fresh":
            if pre_existing is not None:
                raise LaunchError(f"fresh session {stable_id} already has a record")
        else:
            if pre_existing is None:
                raise LaunchError(f"resume session {stable_id} has no managed record")
            try:
                current = store.load(stable_id)
            except sessions.SessionError as exc:
                raise LaunchError(str(exc)) from exc
            if current.get("version") != sessions.RECORD_VERSION:
                raise LaunchError(
                    f"session {stable_id} was changed to a legacy record concurrently "
                    "(restore-2x?); prepare the launch again"
                )
            if sessions.managed_id(current) != stable_id:
                raise LaunchError(f"session {stable_id} changed identity concurrently")
            if expected_launch_epoch is not None:
                current_epoch = current.get("launch_epoch", 0)
                if (
                    current_epoch != expected_launch_epoch
                    or current.get("mutation_token") != expected_mutation_token
                ):
                    raise LaunchError(
                        f"session {stable_id} authority changed after preparation; "
                        "prepare the launch again"
                    )
                if record.get("launch_epoch", 0) != current_epoch + 1:
                    raise LaunchError(
                        f"session {stable_id} launch epoch is stale; prepare it again"
                    )
            if (
                expected_applied_hash is not None
                and current.get("applied_hash") != expected_applied_hash
            ) or (
                expected_lineup_generation is not None
                and current.get("lineup_generation") != expected_lineup_generation
            ):
                raise LaunchError(
                    f"session {stable_id} lineup changed after preparation "
                    "(live apply or /model); prepare the launch again"
                )
            if expected_disk_generation is not None:
                disk_n = disk_generation(expected_scope)
                if disk_n != expected_disk_generation:
                    raise LaunchError(
                        f"session {stable_id} lineup generation on disk changed after "
                        f"preparation ({expected_disk_generation} → {disk_n}); prepare "
                        "the launch again"
                    )
            current_runtime = sessions.runtime_session_id(current)
            if current_runtime != result.session_action.runtime_session_id:
                raise LaunchError(
                    f"session {stable_id} now targets runtime {current_runtime}; "
                    "this launch was compiled for "
                    f"{result.session_action.runtime_session_id} — prepare it again"
                )
            if current["cwd"] != record["cwd"]:
                raise LaunchError(
                    f"session {stable_id} CWD changed concurrently from "
                    f"{record['cwd']!r} to {current['cwd']!r}; prepare it again"
                )
            identity_state = current.get("identity_state", sessions.IDENTITY_UNVERIFIED)
            if current.get("pending_forks"):
                raise LaunchError(sessions.pending_fork_message(current))
            if identity_state == sessions.IDENTITY_REPAIR_NEEDED and not (
                allow_model_relaunch
                and "observed_model" in current
                and "observed_cwd" not in current
            ):
                raise LaunchError(sessions.relink_message(current))

            committed_record = sessions.carry_lifecycle_state(record, current)
            if allow_model_relaunch:
                committed_record.pop("observed_model", None)
                committed_record["identity_state"] = sessions.IDENTITY_UNVERIFIED
            # set_title rotates no token, so a rename between
            # prepare and perform passes every CAS; keep it.
            if current.get("title") is not None:
                committed_record["title"] = current["title"]
            else:
                committed_record.pop("title", None)
            # cf[2]: "launched, no hook yet" (None counts live everywhere), so
            # a resume whose SessionStart is delayed never looks ended.
            committed_record["last_event_source"] = None
            committed_record.pop("last_end_reason", None)
            if sessions.runtime_session_id(committed_record) != runtime_id:
                raise LaunchError(
                    f"session {stable_id} runtime identity changed during preparation"
                )
            prior_pointer = store.read_pointer_bytes(committed_record["cwd"])

        committed_token = sessions.new_mutation_token()
        committed_record = {**committed_record, "mutation_token": committed_token}

        if result.write_lead_prompt:
            state.atomic_write(result.lead_prompt_path, result.lead_prompt.encode("utf-8"))
        had_live = scope.swap_scope(
            store.root, stable_id, result.scope_plan, dir_fsync=dir_fsync
        )
        scope_written = True
        store.save(committed_record)
        committed_bytes = store.read_record_bytes(stable_id)
        store.update_last(committed_record["cwd"], stable_id, blocking=True)
    except BaseException:
        try:
            if committed_token is None:
                raise
            if action_kind == "resume" and pre_existing is not None:
                current_bytes = store.read_record_bytes(stable_id)
                try:
                    current = store.load(stable_id)
                except sessions.SessionError:
                    current = None
                if current is not None and current.get("mutation_token") == committed_token:
                    store.restore_record_bytes(stable_id, pre_existing)
                elif current_bytes != pre_existing:
                    # Unknown authority: never guess at rollback ownership.
                    raise
                if scope_written:
                    try:
                        _restore_scope(store, stable_id, had_live, dir_fsync)
                    except Exception:
                        pass  # never mask the original failure
                try:
                    store.restore_pointer_bytes(record["cwd"], stable_id, prior_pointer)
                except Exception:
                    pass
            elif action_kind == "fresh":
                try:
                    current = store.load(stable_id)
                except sessions.SessionError:
                    current = None
                if current is not None and current.get("mutation_token") == committed_token:
                    store._forget_unlocked(stable_id)
                try:
                    store.clear_last(record["cwd"], stable_id, blocking=True)
                except Exception:
                    pass
                if scope_written:
                    try:
                        scope.remove_live_scope(store.root, stable_id)
                    except Exception:
                        pass
        finally:
            cwd_lease.close()
            release_locks()
        raise

    base_environ = dict(os.environ if environ is None else environ)
    for key in result.env_unset:
        base_environ.pop(key, None)
    final_env = {**base_environ, **result.env_set}
    argv = [str(executable), *result.argv]

    # Claude locates transcripts under the session's ORIGINAL project
    # directory; the fd lease was validated before the commit. A returning
    # injected exec boundary is restored to the caller's CWD; a real exec
    # never returns and O_CLOEXEC closes the lease and both lock fds (the
    # pin's use lock is inheritable: Claude Code keeps it).
    try:
        cwd_lease.enter()
        outcome = execve(str(executable), argv, final_env)
        cwd_lease.restore()
        cwd_lease.close()
        release_locks()
        return outcome
    except OSError:
        try:
            try:
                cwd_lease.restore()
            except OSError:
                pass
        finally:
            cwd_lease.close()
        try:
            try:
                current = store.load(stable_id)
            except sessions.SessionError:
                current = None
            owns_mutation = (
                current is not None
                and committed_token is not None
                and current.get("mutation_token") == committed_token
            )
            if action_kind == "resume" and owns_mutation:
                record_restored = False
                if store.read_record_bytes(stable_id) == committed_bytes:
                    store.restore_record_bytes(stable_id, pre_existing)
                    record_restored = True
                else:
                    try:
                        prior = strict_json.loads(pre_existing)
                        if not isinstance(prior, dict):
                            raise ValueError("prior record is not an object")
                        store.save(sessions.carry_lifecycle_state(prior, current))
                        record_restored = True
                    except Exception:
                        # Never replace the exec error with a cleanup failure;
                        # doctor/converge own an unknown state.
                        pass
                if record_restored:
                    try:
                        _restore_scope(store, stable_id, had_live, dir_fsync)
                    except Exception:
                        pass
                    try:
                        store.restore_pointer_bytes(record["cwd"], stable_id, prior_pointer)
                    except Exception:
                        pass
            elif action_kind == "fresh" and owns_mutation:
                store._forget_unlocked(stable_id)
                try:
                    store.clear_last(record["cwd"], stable_id, blocking=True)
                except Exception:
                    pass
                try:
                    scope.remove_live_scope(store.root, stable_id)
                except Exception:
                    pass
        finally:
            release_locks()
        raise
    except BaseException:
        try:
            cwd_lease.restore()
        finally:
            cwd_lease.close()
            release_locks()
        raise
