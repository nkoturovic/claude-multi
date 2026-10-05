"""Gateway service boundary: systemd hints, status observations and unit control.

Health and process observations are injectable; a service-manager state alone
never proves the gateway stopped. The only calls that start, stop or restart a
unit are :func:`unit_control` (the systemd backend's verbs, which the gateway
lifecycle guards with ownership and the persistence hold); every other helper
reads. This is the facade over the Linux backends: the manager's argv, hint
text and property reads live in ``platform.linux_service``, the ``/proc``
listener and PID observations in ``platform.linux_process``; gateway status
policy, exec/reload stamps and restart decisions stay here.
"""

from __future__ import annotations

import datetime
import re
import http.client
import os
import sys  # the listener backend reads sys.platform; tests patch it here
import subprocess
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import errors, state, strict_json
from .platform import linux_process, linux_service, observation

UNIT = "cli-proxy-api"
GATEWAY_BINARY = "cli-proxy-api"  # the gateway executable's name on PATH
GATEWAY_WORKDIR, GATEWAY_LOGS, GATEWAY_DOTENV = "gateway", "logs", ".env"
# Held by the running gateway process itself (taken before exec, kept across
# it): free together with an absent listener proves no instance runs.
INSTANCE_LOCK = "gateway.lock"
# Serializes starters (launches, explicit starts, stops) of one state root.
START_LOCK = "gateway-start.lock"
_VERBS = linux_service.VERBS


class ServiceError(errors.ClaudeMultiError, RuntimeError):
    """An unreadable service state must not authorize destructive recovery."""


ON_DEMAND, SYSTEMD = "on-demand", "systemd"
# The product's own gateway verbs work on both backends (on the systemd
# backend they delegate to the service manager behind the ownership and
# persistence-hold checks), so every control remedy names them; a raw manager
# restart would skip the hold.
_PRODUCT_VERBS = {
    "start": "claude-multi gateway start",
    "stop": "claude-multi gateway stop",
    "restart": "claude-multi gateway restart",
    "status": "claude-multi gateway status",
    "logs": "claude-multi gateway logs",
}
HINT_VERBS = (*_PRODUCT_VERBS, "reload", "reset-failed", "recover", "why")


@dataclass(frozen=True)
class Backend:
    """Which backend runs the gateway, and its unit (systemd only)."""

    name: str = ON_DEMAND
    unit: str | None = None


def backend_of(home: Path | str | None) -> Backend:
    """The backend the user's endpoint document records (on-demand without one).

    A document that cannot be read or names a backend this platform cannot
    run reads as on-demand here: remedies are text, and the lifecycle refuses
    such a document itself with its own remedy.
    """

    if home is None:
        return Backend()
    from claude_multi import endpoint  # endpoint never imports this module

    try:
        config = endpoint.read_config(home)
        name = endpoint.resolve_backend(config)
    except endpoint.EndpointError:
        return Backend()
    if name != SYSTEMD or config is None:
        return Backend()
    return Backend(SYSTEMD, config.service_unit)


def hint(verb: str, *, home: Path | str | None = None, backend: Backend | None = None) -> str:
    """The semantic remedy ``verb`` as command text for the user's backend.

    The one remedy seam: service-manager text appears only where the
    supervised unit exists (the systemd backend); otherwise the product's own
    verbs. Control verbs always name the guarded product verbs.
    """

    chosen = backend if backend is not None else backend_of(home)
    if verb in _PRODUCT_VERBS:
        return _PRODUCT_VERBS[verb]
    if verb not in HINT_VERBS:
        raise ValueError(f"unsupported gateway service verb: {verb}")
    if chosen.name == SYSTEMD and chosen.unit:
        if verb == "recover":
            return f"{linux_service.hint('reset-failed', unit=chosen.unit)} && {_PRODUCT_VERBS['start']}"
        return linux_service.hint(verb, unit=chosen.unit)
    if verb == "reload":
        return "claude-multi-proxy init"
    if verb == "why":
        return _PRODUCT_VERBS["logs"]
    return _PRODUCT_VERBS["start"]  # recover, reset-failed: an explicit start clears the pause


def hint_code(verb: str, *, home: Path | str | None = None, backend: Backend | None = None) -> str:
    return f"`{hint(verb, home=home, backend=backend)}`"


def journal_argv(*, since: str = "-24h", unit: str = UNIT) -> tuple[str, ...]:
    return linux_service.journal_argv(since=since, unit=unit)


def read_gateway_journal(*, max_bytes: int, since: str = "-24h", unit: str = UNIT):
    return linux_service.read_gateway_journal(unit=unit, max_bytes=max_bytes, since=since)


def unit_control(verb: str, unit: str, *, runner: Callable = subprocess.run, timeout: float | None = None,
                 no_block: bool = False) -> bool:
    """Run one start/stop/restart of ``unit``; True when the manager accepted it.

    Callers own the policy (ownership proof, persistence hold); an unavailable
    manager is False, never an exception that could be mistaken for success.
    ``timeout`` bounds the call (default: the manager's own bound);
    ``no_block`` only enqueues the job.
    """

    try:
        completed = linux_service.control(
            verb, unit, runner, timeout=linux_service.CONTROL_TIMEOUT if timeout is None else timeout,
            no_block=no_block)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def journal_tail(unit: str, *, lines: int, runner: Callable = subprocess.run) -> list[str] | None:
    """The unit's last ``lines`` journal messages, or None when unreadable."""

    return linux_service.journal_tail(unit=unit, lines=lines, runner=runner)


_manager_query = linux_service.manager_query
_manager_property = linux_service.manager_property


def manager_state(unit: str = UNIT, *, runner: Callable = subprocess.run, timeout: float | None = None) -> str | None:
    return _manager_property("ActiveState", unit, runner,
                             timeout=linux_service.QUERY_TIMEOUT if timeout is None else timeout)


def service_active(unit: str = UNIT, *, runner: Callable = subprocess.run) -> bool:
    try:
        completed = _manager_query("ActiveState", unit, runner)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceError(
            f"cannot tell whether {unit} is running ({type(exc).__name__}); "
            f"stop it and retry: {hint_code('stop')}"
        ) from exc
    active = completed.stdout.strip() if completed.returncode == 0 else ""
    if active in ("inactive", "failed"):
        return False
    if active:
        return True
    raise ServiceError(
        f"cannot tell whether {unit} is running (service manager exit "
        f"{completed.returncode}); stop it and retry: {hint_code('stop')}"
    )


def gateway_workdir(state_root: Path) -> Path:
    return Path(state_root) / GATEWAY_WORKDIR


def ensure_gateway_workdir(state_root: Path) -> Path:
    workdir = gateway_workdir(state_root)
    state.ensure_private_dir(workdir)
    state.ensure_private_dir(workdir / GATEWAY_LOGS)
    return workdir


def instance_lock_path(state_root: Path) -> Path:
    return gateway_workdir(state_root) / INSTANCE_LOCK


def start_lock_path(state_root: Path) -> Path:
    return gateway_workdir(state_root) / START_LOCK


def gateway_dotenv_present(workdir: Path) -> bool:
    """Existence only, including dangling symlinks; never read a dotenv file."""
    return os.path.lexists(Path(workdir) / GATEWAY_DOTENV)


def failure_notice(unit: str = UNIT) -> tuple[str, str]:
    """The supervised unit's failure notice (``OnFailure``): systemd text by nature."""

    return (
        "claude-multi gateway failed",
        "The gateway service stopped after repeated failures. "
        f"Why: {linux_service.hint('why', unit=unit)} · recover: {linux_service.hint('recover', unit=unit)} · "
        "status 226/NAMESPACE (a missing directory): claude-multi gateway service install recreates them",
    )


@dataclass(frozen=True)
class GatewayStatus:
    state: str  # running | stopped | unknown
    pid: int | None
    manager_state: str | None
    detail: str


HEALTH_TIMEOUT = 1.5


def _health_get(base_url: str, health_path: str, timeout: float = HEALTH_TIMEOUT) -> int:
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1":
        raise ServiceError("gateway health address is not loopback http")
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        connection.request("GET", health_path)
        return connection.getresponse().status
    finally:
        connection.close()


def gateway_pid(
    *, state_root: Path, proc_root: Path = Path("/proc"), unit: str = UNIT,
    runner: Callable = subprocess.run, readlink: Callable = os.readlink,
) -> tuple[int | None, bool | None]:
    """Check the exec-stamp PID, with a manager fallback for pre-stamp releases.

    A persisted PID may have been reused. Trust it only while its executable
    is the stamped gateway; otherwise use the manager's current MainPID hint.
    Absence is stopped evidence only in the stamped PID namespace. A private
    /proc view, an old stamp without that namespace, or an unreadable namespace
    cannot prove the gateway is gone. An unreadable/reused PID stays unknown.
    """
    stamp = read_exec_stamp(gateway_workdir(state_root))
    return linux_process.gateway_pid(
        stamp, proc_root=proc_root, readlink=readlink,
        main_pid=lambda: _manager_property("MainPID", unit, runner),
    )


def gateway_status(
    *, base_url: str, health_path: str, state_root: Path,
    health_get: Callable | None = None, manager: Callable | None = None,
    pid_get: Callable | None = None, proc_root: Path = Path("/proc"),
) -> GatewayStatus:
    """Health + PID, with the manager only a hint. Unknown never means stopped."""
    try:
        status = (health_get or _health_get)(base_url, health_path)
        health = True if status == 200 else None
    except ConnectionRefusedError:
        health = False
    except Exception:
        health = None
    try:
        pid, alive = (pid_get or gateway_pid)(state_root=state_root, proc_root=proc_root)
    except Exception:
        pid, alive = None, None
    try:
        active = (manager or manager_state)()
    except Exception:
        active = None
    if health is True or alive is True:
        return GatewayStatus("running", pid, active, "health answered 200 or gateway PID is alive")
    if health is False and alive is False and active in (None, "inactive", "failed"):
        return GatewayStatus("stopped", pid, active, "health refused and gateway PID is absent")
    return GatewayStatus("unknown", pid, active, "gateway health or PID is uncertain")


OwnerVerdict = observation.OwnerVerdict  # ours | foreign | unknown | none


def owner_policy(verdict: OwnerVerdict) -> str:
    """Only a foreign uid refuses; unconfirmed ownership is Attention (a
    served read that needs proof refuses it too: ``launch.check_listener``)."""
    if verdict.kind == "foreign":
        return "refuse"
    return "proceed" if verdict.kind in ("ours", "none") else "attention"


def owner_attention(base_url: str, verdict: OwnerVerdict) -> str:
    port = urllib.parse.urlsplit(base_url).port or 80
    return (f"port {port}: {verdict.detail}; the claude-multi gateway service "
            f"could not be confirmed — proceeding; check with: {hint_code('status')}")


def listener_owner(
    base_url: str, *, proc_root: Path = Path("/proc"), euid: int | None = None,
    stamp: dict | None = None, runner: Callable = subprocess.run,
) -> OwnerVerdict:
    """Observe the listening socket uid, then prove MainPID holds its inode.

    No process environments or command lines are read. Unknown is not proof of
    a foreign uid; a foreign matching listener always wins, even if another
    table or the service manager is unreadable. On macOS the bounded lsof
    reader (plus a connect probe) answers instead of ``/proc``.
    """
    if sys.platform == "darwin":
        from .platform import darwin_process

        pid = stamp.get("pid") if stamp is not None else None
        return darwin_process.listener_owner(base_url, pid=pid if isinstance(pid, int) else None,
                                             euid=euid, runner=runner)
    return linux_process.listener_owner(
        base_url, proc_root=proc_root, euid=euid, stamp=stamp,
        main_pid=lambda: _manager_property("MainPID", UNIT, runner),
    )


EXEC_STAMP = "exec.json"
INSTANCE_NONCE = re.compile(r"^[0-9a-f]{16}$")


def start_marker_prefix(instance: str) -> str:
    """The start of the line ``run`` writes to stdout right before the exec."""

    return f"claude-multi-proxy: gateway instance {instance} starting"
RELOAD_STAMP = "last-reload.json"
RELOAD_ATTEMPT = "reload-attempt.json"
RELOAD_SUCCESS = "reload-success.json"
_RELOAD_STATUSES = frozenset({"reloaded", "restart_required", "token_mismatch", "down", "error"})


@dataclass(frozen=True)
class ExecStamp:
    launcher_version: str
    signature: str
    pid: int
    binary: str
    started_at: str
    pid_namespace: str | None = None  # a stamp without it cannot prove PID absence
    # A random per-start identity; it names the instance's log file and its
    # cursors. Older stamps carry none.
    instance: str | None = None


def utc_now() -> str:
    # Subsecond precision distinguishes an attempt from the preceding success.
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def _stamp_time(value: str) -> datetime.datetime:
    result = datetime.datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("stamp timestamp needs a timezone")
    return result


def _read_stamp(path: Path) -> dict | None:
    try:
        value = strict_json.loads(state.read_private(path))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, errors.ClaudeMultiError):
        return None


def write_exec_stamp(workdir: Path, stamp: ExecStamp) -> None:
    state.atomic_write(workdir / EXEC_STAMP, strict_json.canonical_bytes(asdict(stamp)))


def read_exec_stamp(workdir: Path) -> ExecStamp | None:
    raw = _read_stamp(workdir / EXEC_STAMP)
    if raw is None:
        return None
    try:
        stamp = ExecStamp(**raw)
        if type(stamp.pid) is not int or stamp.pid <= 0:
            return None
        if any(not isinstance(raw[k], str) or not raw[k]
               for k in ("launcher_version", "signature", "binary", "started_at")):
            return None
        if stamp.pid_namespace is not None and (
            not isinstance(stamp.pid_namespace, str)
            or re.fullmatch(r"pid:\[[0-9]+\]", stamp.pid_namespace) is None
        ):
            return None
        if stamp.instance is not None and (
            not isinstance(stamp.instance, str) or INSTANCE_NONCE.fullmatch(stamp.instance) is None
        ):
            return None
        _stamp_time(stamp.started_at)
        return stamp
    except (TypeError, ValueError):
        return None


def instance_identity(stamp: ExecStamp | None) -> str | None:
    """The recorded start's identity: PID namespace, PID and start time.

    Every gateway start writes a new stamp, so a replaced or restarted process
    has a different identity. A legacy stamp has no namespace and no identity.
    """
    if stamp is None or stamp.pid_namespace is None:
        return None
    return f"{stamp.pid_namespace}/{stamp.pid}@{stamp.started_at}"


def gateway_observation(
    base_url: str, *, state_root: Path, listener: Callable | None = None,
    pid_get: Callable | None = None,
) -> observation.GatewayObservation:
    """The backend's stopped/instance proof: process state, identity and listener owner.

    Credential-free and read-only (no connect). The listener is observed first;
    if it cannot be observed, the process is not consulted. An unreadable or
    unavailable observation (no /proc, no manager, a private PID namespace) is
    unknown, and only a stopped process with no listener is a stopped proof.
    State, PID and identity form one sample: the exec stamp is read before and
    after the observation, a stamp that changed meanwhile (a replacement
    started) makes the whole sample unknown, and the identity is attached
    only to the stamp's own PID, never to a different manager-reported PID.
    """
    workdir = gateway_workdir(state_root)
    try:
        before = read_exec_stamp(workdir)
        owner = (listener or listener_owner)(base_url)
        pid, alive = (pid_get or gateway_pid)(state_root=state_root)
        after = read_exec_stamp(workdir)
    except (OSError, errors.ClaudeMultiError):
        unknown = observation.ProcessObservation(observation.UNKNOWN, None, None, "gateway was not observable")
        return observation.GatewayObservation(unknown, OwnerVerdict("unknown", "listener ownership is unavailable"))
    if before != after:
        replaced = observation.ProcessObservation(
            observation.UNKNOWN, None, None, "gateway start record changed during the observation")
        return observation.GatewayObservation(replaced, owner)
    state_name = observation.liveness(alive)
    identity = instance_identity(after) if after is not None and pid == after.pid else None
    return observation.GatewayObservation(
        observation.ProcessObservation(state_name, pid, identity, f"gateway process is {state_name}"), owner)


def confirm_stopped(first: observation.GatewayObservation,
                    observe: Callable[[], observation.GatewayObservation]) -> bool:
    """Revalidate a stopped proof at the promotion boundary with a fresh sample.

    True only when ``first`` is a stopped proof and a second observation is a
    stopped proof of the same process sample (state, PID and instance
    identity): a replacement or an inconsistent sample withdraws it.
    """
    if not first.stopped_proof:
        return False
    second = observe()
    return second.stopped_proof and second.process == first.process


def write_reload_attempt(workdir: Path, at: str, launcher_version: str) -> None:
    state.atomic_write(workdir / RELOAD_ATTEMPT, strict_json.canonical_bytes(
        {"at": at, "launcher_version": launcher_version}))


def write_reload_stamp(workdir: Path, status: str, at: str, launcher_version: str) -> None:
    if status not in _RELOAD_STATUSES:
        raise ValueError("invalid reload status")
    raw = strict_json.canonical_bytes(
        {"status": status, "at": at, "launcher_version": launcher_version})
    state.atomic_write(workdir / RELOAD_STAMP, raw)
    if status == "reloaded":
        state.atomic_write(workdir / RELOAD_SUCCESS, raw)


def invalidate_reload_stamps(workdir: Path) -> None:
    """Best-effort invalidation when a reload attempt/result cannot be recorded.

    Older success bytes cannot prove the latest reload succeeded. Removing
    them needs no new file allocation (notably under ENOSPC/EDQUOT) and makes
    doctor consult ExecReload. Keep the attempt as independent pending evidence.
    """
    for name in (RELOAD_STAMP, RELOAD_SUCCESS):
        try:
            state.remove_private(workdir / name)
        except (OSError, errors.ClaudeMultiError):
            pass


def _read_reload(workdir: Path, name: str) -> dict[str, str] | None:
    raw = _read_stamp(workdir / name)
    if raw is None or not all(isinstance(v, str) for v in raw.values()):
        return None
    try:
        _stamp_time(raw["at"])
        if not raw["launcher_version"]:
            return None
        if name != RELOAD_ATTEMPT and raw["status"] not in _RELOAD_STATUSES:
            return None
        return raw
    except (KeyError, ValueError, TypeError):
        return None


def read_reload_stamp(workdir: Path) -> dict[str, str] | None:
    return _read_reload(workdir, RELOAD_STAMP)


ExecReloadResult = observation.ExecReloadResult
_manager_time = linux_service.manager_time


def last_reload_result(*, unit: str = UNIT, runner: Callable = subprocess.run) -> ExecReloadResult | None:
    """Read ``unit``'s last completed ExecReload, including failures before Python ran.

    Unset or malformed manager records are unavailable (``None``); see the
    backend, ``platform.linux_service.last_reload_result``.
    """
    return linux_service.last_reload_result(unit=unit, runner=runner)


def last_reload_failed(*, unit: str = UNIT, runner: Callable = subprocess.run) -> bool:
    """Compatibility boolean observation; restart radars use the dated result."""
    result = last_reload_result(unit=unit, runner=runner)
    return result is not None and result.failed


def pending_restart_lines(
    workdir: Path, *, signature: str, launcher_version: str,
    reload_failed: Callable[[], ExecReloadResult | bool | None] | None = None,
    backend: Backend | None = None,
) -> list[str]:
    """Read-only restart radar. The optional manager seam is injected in tests.

    Without it the service manager is asked only about the unit ``backend``
    records (the systemd backend); the on-demand gateway (the default) has no
    manager record, so its stamps alone decide.
    """
    stamp = read_exec_stamp(workdir)
    result = read_reload_stamp(workdir)
    attempt = _read_reload(workdir, RELOAD_ATTEMPT)
    success = _read_reload(workdir, RELOAD_SUCCESS)
    floor = _stamp_time(stamp.started_at) if stamp else None
    completed = _stamp_time(success["at"]) if success else floor
    if floor is not None and (completed is None or completed < floor):
        completed = floor
    pending = attempt is not None and (completed is None or _stamp_time(attempt["at"]) > completed)
    failed = result is not None and result["status"] != "reloaded" and (
        floor is None or _stamp_time(result["at"]) >= floor)
    # Writers invalidate old successes when they cannot record an attempt/result
    # (ENOSPC included). A pre-exec failure runs no writer at all, so always
    # consult the dated manager record, even with apparently successful stamps.
    stamps_available = (
        result is not None and (result["status"] != "reloaded" or success is not None)
        and (attempt is not None or not os.path.lexists(workdir / RELOAD_ATTEMPT))
        and os.access(workdir, os.W_OK)
    )
    if reload_failed is not None:
        manager_result = reload_failed()
    elif backend is not None and backend.name == SYSTEMD and backend.unit:
        manager_result = last_reload_result(unit=backend.unit)
    else:
        manager_result = None
    if isinstance(manager_result, ExecReloadResult):
        manager_failed = manager_result.failed and (
            not stamps_available or completed is None or manager_result.started_at > completed)
    else:
        # Legacy injected booleans have no chronology: retain their fallback-only
        # semantics, and never fall through to the real manager from a fixture.
        manager_failed = not stamps_available and bool(manager_result)
    lines = []
    if pending or failed or manager_failed:
        at = attempt["at"] if pending else result["at"] if failed else "the last ExecReload"
        status = result["status"] if failed else "ExecReload failed" if manager_failed else "reload pending"
        lines.append(
            f"gateway restart pending: the render of {at} was not applied by the running "
            f"gateway ({status}); restart it between turns: {hint_code('restart', backend=backend)}"
        )
    if stamp is not None and stamp.signature != signature:
        lines.append(
            "gateway restart pending: the running gateway was started by claude-multi "
            f"{stamp.launcher_version} and this release changes its start arguments or "
            "environment filter, which apply only at a restart: "
            f"{hint_code('restart', backend=backend)} (between turns)"
        )
    return lines
