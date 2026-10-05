"""The local gateway's lifecycle: ensure, stop, restart, status and logs.

Backends. On-demand is the default on every release channel and platform: the
launcher starts the gateway when a session needs it and leaves it running (it
is never reference-counted to sessions). The systemd backend applies only
when the endpoint document records an installed service; then these verbs
delegate to the service manager, behind the same ownership and hold checks.

Ownership ("proven ours"): the single-instance lock is held, the exec stamp
names the process, that PID runs the stamped binary, and the listening socket
on the configured port belongs to that PID. A free lock with no listener is
the stopped proof. Anything else on the port is not ours, and unknown counts
as not ours: nothing is started, signalled or sent a token.

Every start, stop and service hand-off takes ``gateway-start.lock`` before it
looks at the backend, and re-reads the endpoint document under it (a hand-off
changes the backend while holding it). Under that lock each of them first
checks the state root's inhibition (:mod:`gateway_inhibition`): while a
transaction owner — a service hand-off, the installer, the updater, a
machine move — holds one, nothing is started, stopped or handed over, and
the outcome is ``inhibited``; ``ensure`` still delivers a gateway already
proven ours (it waits for its readiness, never starts one). A caller that
finds the start lock taken while such an inhibition is recorded does not
wait for it: a change reports the inhibition at once, and ``ensure`` checks
the destination, ownership and readiness without the lock (read-only; the
owner may hold the lock for its whole transaction). A home with no
recorded endpoint and no earlier gateway (no ``config.yaml`` or key) is not
set up: an automatic ``ensure`` refuses there instead of using the packaged
port, and only an explicit start (``choose_port``) records the new
install's port first.

On-demand ``ensure`` serializes starters with ``gateway-start.lock``; reuses a
gateway proven ours whose /healthz answers; otherwise spawns one detached
``claude-multi-proxy run --prepare-and-exec`` (new session, umask 077, cwd the
gateway directory, an allowlisted environment, stdout and stderr to a fresh
per-instance log) and waits, bounded, for its start readiness: ours, healthy,
and serving the render sentinel of the configuration it prepared. It reports
what is missing and never retries a start. One deadline bounds the whole
ensure: the start lock, every service-manager call and spawn, every
ownership, health and models observation (each blocking call gets at most
the time left) and the readiness wait; once it passes the result is a
timeout (a gateway still starting is left alone), never a late "ready". The systemd backend observes
ownership first, exactly like on-demand, and only a stopped proof lets it
enqueue a unit start. Three starts within ten minutes
block automatic starts (with the log tail) until the user starts it
explicitly.

``stop`` and ``restart`` signal only a PID proven ours, and only after the
persistence hold (:mod:`gateway_hold`) found nothing unresolved. A spawn
first prunes previous instances' logs beyond a byte budget, after their
unresolved failures became recorded holds; a running instance's log is never
touched. On macOS the ownership proof reads ``lsof`` and ``ps`` through
``platform.darwin_process``; under WSL a state root on a Windows drive is
refused.
"""

from __future__ import annotations

import datetime
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from claude_multi import (endpoint, errors, gateway_hold, gateway_inhibition, installs, layout, paths, secret_store,
                          service, state, strict_json, termtext)
from claude_multi.platform import file_log, linux_process, mounts, observation, posix_fs, posix_process

# Start readiness: the gateway's own start-up gate holds model routes for at
# most 30 s after a start; the budget adds a margin.
START_READINESS_TIMEOUT = 45.0
START_LOCK_WAIT = 10.0
STOP_TIMEOUT = 30.0
POLL = 0.1
# The shortest bound a blocking call within an ensure gets (a call with less
# time left than this is not made; the ensure reports its timeout instead).
MIN_CALL = 0.5
# Service-manager states in which a start is already under way or done.
MANAGER_RUNNING = frozenset({"active", "activating", "reloading"})
CRASH_WINDOW = datetime.timedelta(minutes=10)
CRASH_LIMIT = 3
START_HISTORY = "start-history.json"
LOG_TAIL_LINES = 20
# What a failure's log tail is read with: its lines and the context above
# them, redacted together before the tail is cut to LOG_TAIL_LINES.
LOG_TAIL_READ = LOG_TAIL_LINES + secret_store.TAIL_CONTEXT_LINES
BUSY_EXIT = 75  # run's status while another instance holds the lock

SPAWN_PATH = "/usr/bin:/bin"
# Where a package links the gateway executable it ships (relative to its root).
PACKAGED_GATEWAY = Path("libexec") / "claude-multi"
_SPAWN_PASS = frozenset({"LANG", "LANGUAGE", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"})
# Never handed to a spawned gateway: the gateway is not a session (the session
# markers would outlive the session that started it), and a spawned gateway
# never carries a patch attestation.
# An inherited inhibition token is never forwarded either: only the owner's
# own Gateway object hands its token to the gateway it starts.
_SPAWN_WITHHELD = frozenset({
    "CLAUDE_MULTI_PROXY_PATCHES", "CLAUDE_MULTI_MANAGED_ID", "CLAUDE_MULTI_SESSION_ID",
    "CLAUDE_MULTI_LAUNCH_EPOCH", "CLAUDE_MULTI_GATEWAY", gateway_inhibition.TOKEN_ENV,
})

OURS, STARTING, STOPPED, FOREIGN, UNKNOWN = "ours", "starting", "stopped", "foreign", "unknown"
# What confines the gateway process, per backend.
CONFINEMENT = {endpoint.SYSTEMD: "tmpfs+TierB (systemd)", endpoint.ON_DEMAND: "none (on-demand)"}

# Exit statuses of the gateway verbs: 0 for an ok outcome, 1 for every
# refusal, pause, failure, timeout, busy start lock, persistence hold or
# inhibition (the outcome's status and message keep the detail). The token
# helper relies only on zero versus nonzero.
EXIT_CODES = {"ready": 0, "stopped": 0, "refused": 1, "blocked": 1, "failed": 1,
              "timeout": 1, "busy": 1, "held": 1, "inhibited": 1}


class LifecycleError(errors.ClaudeMultiError, RuntimeError):
    """The gateway lifecycle cannot proceed (e.g. no proxy entry point)."""


# A service hand-off (``gateway service install|uninstall``) moves the gateway
# between backends under the start lock, as the inhibition owner "service".
# Its record also refuses every other change if the hand-off dies midway,
# until a later install (or uninstall) finishes it.
SERVICE_OWNER = gateway_inhibition.SERVICE_OWNER
SERVICE_REMEDY = gateway_inhibition.SERVICE_REMEDY
NOT_SET_UP = "the local gateway is not set up yet (no port is recorded for it)"


@dataclass(frozen=True)
class Observation:
    state: str  # ours | starting | stopped | foreign | unknown
    detail: str
    stamp: service.ExecStamp | None
    listener: observation.OwnerVerdict
    lock: bool | None

    @property
    def pid(self) -> int | None:
        return self.stamp.pid if self.stamp is not None else None


@dataclass(frozen=True)
class Outcome:
    status: str  # ready | stopped | refused | blocked | failed | timeout | busy | held | inhibited
    message: str
    remedy: str | None = None
    log_tail: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Gateway log lines reach a person only redacted, whichever backend
        # or failure path read them: the last LOG_TAIL_LINES, with every line
        # read above them as context.
        object.__setattr__(self, "log_tail", tuple(secret_store.redact_tail(self.log_tail, LOG_TAIL_LINES)))

    @property
    def ok(self) -> bool:
        return self.status in ("ready", "stopped")

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def lines(self) -> list[str]:
        result = [self.message, *(f"  {note}" for note in self.notes)]
        if self.remedy:
            result.append(f"  fix: {self.remedy}")
        if self.log_tail:
            result.append("  log tail:")
            result.extend(f"    {termtext.visible_text(line)}" for line in self.log_tail)
        return result


@dataclass
class Seams:
    """Live observations and effects; tests replace them (never the real gateway)."""

    health_get: Callable[[str, str], int] | None = None
    listener: Callable[[str, int | None], observation.OwnerVerdict] | None = None
    process: Callable[[service.ExecStamp], tuple[int | None, bool | None]] | None = None
    lock_held: Callable[[Path], bool | None] = posix_fs.lock_held
    spawn: Callable[..., int | None] = posix_process.spawn_detached
    terminate: Callable[[int], bool] = posix_process.terminate
    models_get: Callable[[str, str, observation.OwnerVerdict], tuple[int, Any]] | None = None
    runner: Callable = subprocess.run
    # The unit journal from an instant on: ``journal(unit, since)``.
    journal: Callable[[str, datetime.datetime], observation.LogWindow] | None = None
    port_probe: Callable[[int], bool] = endpoint.port_free
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime.datetime] = field(
        default=lambda: datetime.datetime.now(datetime.timezone.utc))
    proc_root: Path = Path("/proc")
    # The filesystem type under the state root (None = unknown).
    filesystem: Callable[[Path], str | None] | None = None


def spawn_environment(environ: Mapping[str, str], home: Path | str, *,
                      resolve_binary: bool = False) -> dict[str, str]:
    """The allowlisted environment of a spawned gateway.

    HOME, a minimal PATH, the locale, ``TZ=UTC`` (log timestamps), TMPDIR,
    the TLS trust variables and the launcher's own ``CLAUDE_MULTI_*`` settings
    minus the withheld ones. Proxy variables and every other credential or
    configuration variable stay behind. With ``resolve_binary`` (a source or
    script entry point, which has no wrapper to pin the gateway binary) the
    launcher's PATH resolves the gateway executable once, here.
    """

    env = {"HOME": str(home), "PATH": SPAWN_PATH, "TZ": "UTC"}
    for name, value in environ.items():
        if name in _SPAWN_PASS or name.startswith("LC_"):
            env[name] = value
        elif name.startswith("CLAUDE_MULTI_") and name not in _SPAWN_WITHHELD:
            env[name] = value
    if resolve_binary and "CLAUDE_MULTI_PROXY_BIN" not in env:
        found = shutil.which(service.GATEWAY_BINARY, path=environ.get("PATH") or None)
        if found:
            env["CLAUDE_MULTI_PROXY_BIN"] = found
    return env


def packaged_gateway(root: Path | str | None) -> str | None:
    """The gateway an installation ships (``libexec/claude-multi/<binary>``), or None."""

    if root is None:
        return None
    path = Path(root) / PACKAGED_GATEWAY / service.GATEWAY_BINARY
    return str(path) if path.exists() else None


MODELS_TIMEOUT = 2.0


def _default_models_get(base_url: str, token: str, verdict: observation.OwnerVerdict, *,
                        timeout: float = MODELS_TIMEOUT):
    from claude_multi import launch  # deferred: launch is heavy and only needed here

    return launch._default_models_get(base_url, token, timeout=timeout, owner_check=lambda _base: verdict)


def published_sentinel(home: Path) -> str | None:
    """The render sentinel of the published config (None when unreadable)."""

    from claude_multi import render, served_plan

    try:
        raw = state.read_private(paths.gateway_config_dir({"HOME": str(home)}) / "config.yaml")
        return render.document_sentinel(served_plan.parse_restricted_yaml(raw.decode("utf-8")))
    except (OSError, ValueError, KeyError, IndexError, TypeError, errors.ClaudeMultiError):
        return None


class Gateway:
    """One state root's gateway, with the user's endpoint and backend."""

    def __init__(
        self, *, home: Path | str, state_root: Path | str, environ: Mapping[str, str],
        gateway_document: Mapping[str, Any], installation: Path | str | None = None,
        providers: Iterable[str] | Callable[[], Iterable[str]] = (),
        seams: Seams | None = None, platform: str | None = None,
        proxy_command: list[str] | None = None,
    ) -> None:
        self.home = Path(home)
        self.state_root = Path(state_root)
        self.environ = dict(environ)
        self.catalog_document = gateway_document
        # The installation this launcher runs from (its bin/ entry points and
        # packaged gateway); never a resource directory.
        self.installation = Path(installation) if installation is not None else layout.installation()
        self._providers = providers
        self.seams = seams or Seams()
        self.platform = sys.platform if platform is None else platform
        self._proxy_command = proxy_command
        self.config = endpoint.read_config(self.home)
        self.backend = endpoint.resolve_backend(self.config, platform=self.platform)
        # The inhibition token this object acts under: the one it recorded
        # for a service hand-off, or the transaction owner's (set by the
        # owner's explicit verbs); None for everyone else.
        self.inhibition_token: str | None = None
        self._handoff_recorded = False

    # ------------------------------------------------------------ endpoint
    @property
    def document(self) -> Mapping[str, Any]:
        return endpoint.effective_document(self.catalog_document, self.config)

    @property
    def base_url(self) -> str:
        return endpoint.gateway_endpoint(self.document).base_url

    @property
    def health_path(self) -> str:
        return endpoint.gateway_endpoint(self.document).health_path

    @property
    def port(self) -> int:
        return endpoint.port_of(self.base_url)

    @property
    def unit(self) -> str | None:
        return self.config.service_unit if self.config is not None else None

    @property
    def workdir(self) -> Path:
        return service.gateway_workdir(self.state_root)

    @property
    def logs_dir(self) -> Path:
        return self.workdir / service.GATEWAY_LOGS

    def providers(self) -> frozenset[str]:
        value = self._providers() if callable(self._providers) else self._providers
        return frozenset(value)

    # ------------------------------------------------------------ observation
    def _left(self, deadline: float | None, default: float) -> float:
        """The bound of one blocking call made toward ``deadline``: the time
        left (never more than the call's own ``default``)."""

        if deadline is None:
            return default
        return min(default, deadline - self.seams.clock())

    def _health(self, deadline: float | None = None) -> int | None:
        timeout = self._left(deadline, service.HEALTH_TIMEOUT)
        if timeout <= 0:
            return None
        try:
            if self.seams.health_get is not None:
                return self.seams.health_get(self.base_url, self.health_path)
            return service._health_get(self.base_url, self.health_path, timeout=timeout)
        except AssertionError:
            raise
        except Exception:
            return None

    def _listener(self, pid: int | None, deadline: float | None = None) -> observation.OwnerVerdict:
        if self.seams.listener is not None:
            return self.seams.listener(self.base_url, pid)
        if self.platform == "darwin":
            from claude_multi.platform import darwin_process

            return darwin_process.listener_owner(
                self.base_url, pid=pid, runner=self.seams.runner,
                budget=lambda: self._left(deadline, darwin_process.TIMEOUT))
        return service.listener_owner(self.base_url, proc_root=self.seams.proc_root, stamp={"pid": pid})

    def _process(self, stamp: service.ExecStamp, deadline: float | None = None) -> tuple[int | None, bool | None]:
        if self.seams.process is not None:
            return self.seams.process(stamp)
        if self.platform == "darwin":
            from claude_multi.platform import darwin_process

            return darwin_process.gateway_pid(stamp, runner=self.seams.runner,
                                              timeout=self._left(deadline, darwin_process.TIMEOUT))
        return linux_process.gateway_pid(stamp, proc_root=self.seams.proc_root, readlink=os.readlink,
                                         main_pid=lambda: None)

    @property
    def remedy_backend(self) -> service.Backend:
        return service.Backend(self.backend, self.unit)

    def state_root_refusal(self) -> str | None:
        """Why this state root cannot hold the gateway's locks and logs (WSL:
        a Windows drive has neither POSIX modes nor reliable locks), or None."""

        if not self.platform.startswith("linux"):
            return None
        distro = mounts.wsl(self.environ, proc_root=self.seams.proc_root)
        if distro is None:
            return None
        fstype = self.filesystem_type()
        if fstype in mounts.WINDOWS_DRIVE_TYPES:
            return (f"the state root {paths.display(self.state_root, self.environ)} is on a Windows drive "
                    f"({fstype}) under WSL; locks and private file modes do not work there")
        return None

    def unusable_state_root(self) -> Outcome | None:
        """:meth:`state_root_refusal` as the refused outcome of a start."""

        refusal = self.state_root_refusal()
        if refusal is None:
            return None
        return Outcome("refused", refusal + "; the gateway was not started",
                       "move the state root into the Linux file system (unset XDG_STATE_HOME, "
                       "or point it at a path under your Linux home)")

    def filesystem_type(self) -> str | None:
        if self.seams.filesystem is not None:
            return self.seams.filesystem(self.state_root)
        return mounts.filesystem_type(self.state_root, platform=self.platform)

    def observe(self, *, deadline: float | None = None) -> Observation:
        """One sample of lock, stamp, process and listener (no token). Within
        an ensure, ``deadline`` bounds its blocking calls (macOS ``ps`` and
        ``lsof``) to the time left."""

        stamp = service.read_exec_stamp(self.workdir)
        lock = self.seams.lock_held(service.instance_lock_path(self.state_root))
        alive = self._process(stamp, deadline)[1] if stamp is not None else None
        listener = self._listener(stamp.pid if stamp is not None and alive is True else None, deadline)
        if service.read_exec_stamp(self.workdir) != stamp:
            return Observation(UNKNOWN, "the gateway start record changed during the observation",
                               None, listener, lock)
        port = self.port
        if listener.kind == FOREIGN:
            return Observation(FOREIGN, f"port {port} is held by {listener.detail}", stamp, listener, lock)
        if listener.kind == "ours" and lock is True and alive is True:
            return Observation(OURS, f"the claude-multi gateway runs on port {port}", stamp, listener, lock)
        if listener.kind == "none":
            if lock is False:
                return Observation(STOPPED, "the gateway is not running", stamp, listener, lock)
            if lock is True and alive is True:
                return Observation(STARTING, "the gateway is starting (not listening yet)", stamp, listener, lock)
            if lock is True:
                return Observation(UNKNOWN, "the gateway's instance lock is held but its process "
                                   "cannot be identified (a start may be in progress)", stamp, listener, lock)
            return Observation(UNKNOWN, "the gateway's instance lock cannot be checked", stamp, listener, lock)
        if listener.kind == "ours":
            return Observation(UNKNOWN, f"a gateway of yours listens on port {port} without the "
                               "single-instance lock (another release or a manual run)", stamp, listener, lock)
        return Observation(UNKNOWN, f"port {port}: {listener.detail}", stamp, listener, lock)

    # ------------------------------------------------------------ history
    def _history_path(self) -> Path:
        return self.workdir / START_HISTORY

    def recent_starts(self) -> list[datetime.datetime]:
        """Starts within the crash window (an unreadable record counts as none)."""

        try:
            document = strict_json.loads(state.read_private(self._history_path()))
            starts = [datetime.datetime.fromisoformat(value) for value in document["starts"]]
            if document.get("version") != 1 or any(item.tzinfo is None for item in starts):
                raise ValueError("invalid start history")
        except (OSError, ValueError, KeyError, TypeError, errors.ClaudeMultiError):
            return []
        floor = self.seams.now() - CRASH_WINDOW
        return [item for item in starts if item > floor]

    def _write_history(self, starts: list[datetime.datetime]) -> None:
        state.atomic_write(self._history_path(), strict_json.canonical_bytes(
            {"version": 1, "starts": [item.isoformat() for item in starts[-16:]]}))

    def reset_history(self) -> None:
        try:
            self._write_history([])
        except (OSError, errors.ClaudeMultiError):
            pass

    # ------------------------------------------------------------ helpers
    def _acquire_start_lock(self, deadline: float, *,
                            abort: Callable[[], bool] | None = None) -> state.FileLock | None:
        """``gateway-start.lock`` (bounded); None once ``deadline`` passed or,
        while it is taken, as soon as ``abort()`` is true (the caller then
        reports why, e.g. an inhibition its holder recorded)."""

        lock = state.FileLock(self.workdir / service.START_LOCK.removesuffix(".lock"))
        while True:
            if lock.acquire(blocking=False):
                return lock
            if abort is not None and abort():
                return None
            if self.seams.clock() >= deadline:
                return None
            self.seams.sleep(0.05)

    def _foreign_inhibition(self, *, take_over_stale_service: bool = False) -> bool:
        return self.inhibition_refusal("", take_over_stale_service=take_over_stale_service) is not None

    def lock_for_change(self, what: str, *, take_over_stale_service: bool = False,
                        wait: float = START_LOCK_WAIT) -> tuple[state.FileLock | None, Outcome | None]:
        """The start lock for a change (stop, a service verb, the setup
        step): ``(lock, None)``, or ``(None, outcome)`` — inhibited at once
        while an inhibition this object does not own is recorded and the
        lock is taken, busy once the wait is over. The caller still checks
        :meth:`inhibition_refusal` under the lock."""

        service.ensure_gateway_workdir(self.state_root)
        lock = self._acquire_start_lock(
            self.seams.clock() + wait,
            abort=lambda: self._foreign_inhibition(take_over_stale_service=take_over_stale_service))
        if lock is not None:
            return lock, None
        inhibited = self.inhibition_refusal(what, take_over_stale_service=take_over_stale_service)
        return None, inhibited or Outcome("busy", f"a start of this gateway is in progress; {what}",
                                          "retry in a moment")

    def newest_log(self) -> file_log.InstanceLog | None:
        logs = file_log.instance_logs(self.logs_dir)
        return logs[0] if logs else None

    def _tail(self, path: Path | None = None) -> tuple[str, ...]:
        if path is None:
            log = self.newest_log()
            path = log.path if log is not None else None
        if path is None:
            return ()
        return tuple(file_log.tail_lines(path, lines=LOG_TAIL_READ))

    def _proxy_entry(self) -> tuple[list[str], bool]:
        """``(command, script)``: the ``claude-multi-proxy`` entry point next to
        this launcher; ``script`` when it is the bare Python entry (no wrapper
        pins the gateway binary for it)."""

        if self._proxy_command is not None:
            return list(self._proxy_command), False
        hook = self.environ.get("CLAUDE_MULTI_HOOK_COMMAND")
        if hook:
            sibling = Path(hook).with_name("claude-multi-proxy")
            if sibling.is_file() and os.access(sibling, os.X_OK):
                return [str(sibling)], False
        script = layout.entry_point("claude-multi-proxy", self.installation) if self.installation else None
        if script is not None and script.is_file():
            return [sys.executable, str(script)], True
        raise LifecycleError("claude-multi-proxy was not found next to this launcher",
                             remedy="reinstall claude-multi")

    def spawn_argv(self, instance: str) -> list[str]:
        # The state root is always explicit: the spawn environment carries no
        # XDG variables, and the gateway must scan the launcher's records.
        return [*self._proxy_entry()[0], "run", "--prepare-and-exec", "--detach",
                "--instance", instance, "--state-root", str(self.state_root)]

    def spawn_env(self) -> dict[str, str]:
        env = spawn_environment(self.environ, self.home, resolve_binary=self._proxy_entry()[1])
        if self.owns_inhibition(gateway_inhibition.read(self.state_root)):
            # The owner's own start: the gateway it spawns passes the inhibition.
            env[gateway_inhibition.TOKEN_ENV] = str(self.inhibition_token)
        return env

    def _refused(self, seen: Observation, what: str) -> Outcome:
        if seen.state == FOREIGN:
            remedy = (f"free port {self.port}, or give the gateway another port in "
                      f"{paths.display(endpoint.endpoint_path(self.home), {'HOME': str(self.home)})}")
        else:
            remedy = "check it with: claude-multi gateway status"
        return Outcome("refused", f"{seen.detail}; {what}", remedy)

    # ------------------------------------------------------------ ensure
    def ensure(self, *, max_wait: float = START_READINESS_TIMEOUT, explicit: bool = False,
               choose_port: bool = False, destination: str | None = None) -> Outcome:
        """Make sure the gateway runs and is ready (see the module docstring).

        ``explicit`` (the user's ``gateway start``) clears the crash counter;
        ``choose_port`` lets a new install record its port first;
        ``destination`` (a session's compiled base URL) must be the configured
        endpoint, else nothing is started and the caller gets no token.
        """

        deadline = self.seams.clock() + max(0.0, max_wait)
        unusable = self.unusable_state_root()
        if unusable is not None:
            return unusable
        service.ensure_gateway_workdir(self.state_root)
        # The start lock comes before any backend dispatch (a service
        # hand-off changes the backend while it holds this lock) and before
        # a new install records its port. A transaction owner may hold it
        # for its whole transaction: while its inhibition is recorded,
        # nothing waits for the lock (see _ensure_unlocked).
        lock = self._acquire_start_lock(deadline, abort=self._foreign_inhibition)
        if lock is None:
            unlocked = self._ensure_unlocked(deadline, max_wait, choose_port=choose_port, destination=destination)
            return unlocked or Outcome("busy", "another start of this gateway is still in progress",
                                       "retry in a moment, or check: claude-multi gateway status")
        try:
            return self.ensure_locked(deadline, max_wait, explicit=explicit, destination=destination,
                                      choose_port=choose_port)
        finally:
            lock.release()

    def _ensure_unlocked(self, deadline: float, max_wait: float, *, choose_port: bool,
                         destination: str | None) -> Outcome | None:
        """``ensure`` while another holds the start lock and an inhibition this
        object does not own is recorded: read-only, without the lock (the
        destination, ownership and readiness of a running gateway; never a
        start). None when no such inhibition is recorded (any more)."""

        inhibited = self.inhibition_refusal("nothing was started")
        if inhibited is None:
            return None
        if choose_port:
            return inhibited
        try:
            self.reload_endpoint()
        except endpoint.EndpointError as exc:
            return Outcome("refused", f"{exc}; the gateway was not started", exc.remedy)
        return self._destination_refusal(destination) or self._ensure_inhibited(deadline, max_wait, inhibited)

    def _destination_refusal(self, destination: str | None) -> Outcome | None:
        if destination is None or destination.rstrip("/") == self.base_url.rstrip("/"):
            return None
        return Outcome(
            "refused", f"this session sends to {termtext.visible_text(destination)}, but the gateway's "
            f"endpoint is now {self.base_url}; no token was sent",
            "exit the session and resume it (claude-multi -r <session>): resuming recompiles it for the "
            "current endpoint")

    def reload_endpoint(self) -> None:
        """Re-read the endpoint document (the backend may have changed)."""

        self.config = endpoint.read_config(self.home)
        self.backend = endpoint.resolve_backend(self.config, platform=self.platform)

    # ------------------------------------------------------------ inhibition
    def owns_inhibition(self, record: gateway_inhibition.Inhibition | None) -> bool:
        """Whether this object acts as ``record``'s owner (holds its token)."""

        return (record is not None and record.readable and self.inhibition_token is not None
                and record.token == self.inhibition_token)

    def inhibition_refusal(self, what: str, *, take_over_stale_service: bool = False) -> Outcome | None:
        """The ``inhibited`` outcome while an inhibition this object does not
        own is recorded (None: none is, or this object is its owner). The
        service verbs may take over (``take_over_stale_service``) a stale
        record of an interrupted service hand-off: that is how one is
        finished."""

        record = gateway_inhibition.read(self.state_root)
        if record is None or self.owns_inhibition(record):
            return None
        now = self.seams.now()
        if (take_over_stale_service and record.readable and record.owner == SERVICE_OWNER
                and record.stale(now=now)):
            return None
        message, remedy = gateway_inhibition.refusal(record, what, now=now)
        return Outcome("inhibited", message, remedy)

    def begin_handoff(self, operation: str, unit: str) -> Outcome | None:
        """Record the service hand-off's inhibition (the caller holds the
        start lock and checked :meth:`inhibition_refusal`); an outcome when
        it cannot be recorded (nothing was changed). An owner already
        holding the root's inhibition (a machine move running the service
        verbs) runs the hand-off under its own record instead."""

        if self.owns_inhibition(gateway_inhibition.read(self.state_root)):
            self._handoff_recorded = False
            return None
        try:
            self.inhibition_token = gateway_inhibition.begin(
                self.state_root, owner=SERVICE_OWNER, purpose=f"gateway service {operation} ({unit})",
                phase=operation, remedy=SERVICE_REMEDY, expiry=gateway_inhibition.owner_process(),
                now=self.seams.now(), held_lock=True, take_over_stale=True)
        except gateway_inhibition.InhibitionError as exc:
            return Outcome("busy", f"the gateway service hand-off was not recorded: {exc}; nothing was changed",
                           exc.remedy or "retry in a moment")
        self._handoff_recorded = True
        return None

    def end_handoff(self, *, clear: bool = True) -> None:
        """Finish this object's hand-off: remove its record, or (``clear``
        False: the restoration is incomplete) keep it, so every change stays
        refused until the user finishes the hand-off. A hand-off run under
        another owner's record leaves that record alone."""

        if not self._handoff_recorded:
            return
        if clear and self.inhibition_token is not None:
            try:
                gateway_inhibition.end(self.state_root, self.inhibition_token, held_lock=True)
            except (OSError, errors.ClaudeMultiError):
                pass
        self.inhibition_token = None
        self._handoff_recorded = False

    def not_set_up(self) -> bool:
        """No endpoint is recorded and no gateway was ever prepared here: a
        new install that has not chosen its port yet."""

        return self.config is None and not endpoint.existing_install(self.home)

    def _ensure_inhibited(self, deadline: float, max_wait: float, refusal: Outcome) -> Outcome:
        """``ensure`` during an inhibition: a gateway already proven ours is
        awaited (read-only: its readiness, never a start); anything else
        gets the inhibited refusal."""

        seen = self.observe(deadline=self._call_deadline(deadline, max_wait))
        if seen.state in (OURS, STARTING):
            return self._await_ready(deadline, max_wait)
        return refusal

    def ensure_locked(self, deadline: float, max_wait: float, *, explicit: bool = False,
                      destination: str | None = None, choose_port: bool = False) -> Outcome:
        """:meth:`ensure` for a caller that holds the start lock
        (``choose_port``: a new install records its port first, unless an
        inhibition this object does not own is recorded)."""

        if choose_port:
            inhibited = self.inhibition_refusal("nothing was started")
            if inhibited is not None:
                return inhibited
            endpoint.ensure_config(self.home, probe=self.seams.port_probe)
        try:
            self.reload_endpoint()
        except endpoint.EndpointError as exc:
            return Outcome("refused", f"{exc}; the gateway was not started", exc.remedy)
        refused = self._destination_refusal(destination)
        if refused is not None:
            return refused
        inhibited = self.inhibition_refusal("nothing was started")
        if inhibited is not None:
            return self._ensure_inhibited(deadline, max_wait, inhibited)
        if self.not_set_up():
            return Outcome("refused", f"{NOT_SET_UP}; nothing was started and no token was sent",
                           f"set it up: {endpoint.SETUP_COMMAND}")
        if self.backend == endpoint.SYSTEMD:
            return self._ensure_service(deadline, max_wait)
        seen = self.observe(deadline=self._call_deadline(deadline, max_wait))
        if seen.state in (OURS, STARTING):
            return self._await_ready(deadline, max_wait)
        if seen.state != STOPPED:
            return self._refused(seen, "the gateway was not started and no token was sent")
        if explicit:
            self.reset_history()
        else:
            starts = self.recent_starts()
            if len(starts) >= CRASH_LIMIT:
                return Outcome(
                    "blocked",
                    f"BLOCKED: the gateway was started {len(starts)} times in the last "
                    f"{int(CRASH_WINDOW.total_seconds() // 60)} minutes; automatic starts are paused",
                    "read the log (claude-multi gateway logs), fix the cause, then start it "
                    "yourself: claude-multi gateway start",
                    self._tail(),
                )
        return self._spawn(deadline, max_wait)

    def prune_logs(self) -> gateway_hold.PruneReport:
        """At an instance start: drop the oldest previous logs beyond the
        budget, after persisting what they still prove (never a running
        instance's log: a start happens only once the gateway is stopped)."""

        try:
            return gateway_hold.prune_at_start(self.state_root, self.providers())
        except (OSError, errors.ClaudeMultiError) as exc:
            return gateway_hold.PruneReport(skipped=str(exc))

    def _spawn(self, deadline: float, max_wait: float) -> Outcome:
        if deadline - self.seams.clock() < MIN_CALL:
            return self._out_of_time(max_wait, "no time was left to start it")
        instance = secrets.token_hex(8)
        started = self.seams.now()
        self.prune_logs()
        try:
            argv, env = self.spawn_argv(instance), self.spawn_env()
            self._write_history([*self.recent_starts(), started])
            descriptor, log_path = file_log.create_instance_log(self.logs_dir, started, instance)
        except (OSError, errors.ClaudeMultiError) as exc:
            return Outcome("failed", f"the gateway could not be started: {exc}",
                           getattr(exc, "remedy", None) or "check: claude-multi doctor")
        try:
            remaining = max(MIN_CALL, deadline - self.seams.clock())
            code = self.seams.spawn(argv, cwd=str(self.workdir), env=env, output=descriptor,
                                    timeout=min(posix_process.DETACH_TIMEOUT, remaining))
        except OSError as exc:
            return Outcome("failed", f"the gateway could not be started ({exc.strerror or type(exc).__name__})",
                           "check: claude-multi doctor", self._tail(log_path))
        finally:
            os.close(descriptor)
        if code == BUSY_EXIT:
            seen = self.observe(deadline=deadline)
            if seen.state in (OURS, STARTING):
                return self._await_ready(deadline, max_wait)
            return self._refused(seen, "another gateway instance holds the lock")
        if code != 0:
            what = "did not detach in time" if code is None else f"exited with status {code}"
            return Outcome("failed", f"the gateway starter {what}", "read its log: claude-multi gateway logs",
                           self._tail(log_path))
        return self._await_ready(deadline, max_wait, instance=instance, log_path=log_path, dispatched=True)

    def _out_of_time(self, max_wait: float, what: str, log_path: Path | None = None) -> Outcome:
        return Outcome("timeout", f"the gateway is not ready after {max_wait:g} s: {what}",
                       "check it with: claude-multi gateway status", self._tail(log_path) if log_path else ())

    def await_ready(self, deadline: float, max_wait: float) -> Outcome:
        """Wait (bounded) until the gateway is ours and answers health."""

        return self._await_ready(deadline, max_wait)

    @staticmethod
    def _call_deadline(deadline: float, max_wait: float) -> float:
        """The deadline the blocking calls of one ensure share: the caller's,
        or for a zero-wait check (``--max-wait 0``: a look, no wait) the
        shortest call bound past it."""

        return deadline if max_wait > 0 else deadline + MIN_CALL

    def _await_ready(self, deadline: float, max_wait: float, *, instance: str | None = None,
                     log_path: Path | None = None, dispatched: bool = False) -> Outcome:
        """Wait (bounded) until the gateway is ours and ready; report what is missing.

        One absolute deadline covers every observation: the ownership sample,
        /healthz and the models check each get at most the time left, and
        ready is reported only when the whole check finished before the
        deadline. A zero-wait call that started nothing (``--max-wait 0``)
        makes one check, its calls sharing :data:`MIN_CALL` with the
        ownership sample before it.
        """

        once = max_wait <= 0 and not dispatched
        if once:
            deadline = self._call_deadline(deadline, max_wait)
        missing = "no observation"
        while True:
            if self.seams.clock() > deadline:
                return Outcome("timeout", f"the gateway is not ready after {max_wait:g} s: {missing}",
                               "read its log: claude-multi gateway logs", self._tail(log_path))
            seen = self.observe(deadline=deadline)
            current = seen.stamp.instance if seen.stamp is not None else None
            if instance is not None and current != instance:
                if seen.lock is False:
                    return Outcome("failed", "the gateway exited before it started",
                                   "read its log: claude-multi gateway logs", self._tail(log_path))
                missing = "the gateway is still preparing its configuration"
            elif seen.state == FOREIGN:
                return self._refused(seen, "no token was sent")
            elif seen.state == STOPPED:
                if not self._unit_starting(deadline):
                    return Outcome("failed", "the gateway exited during its start",
                                   "read its log: claude-multi gateway logs", self._tail(log_path))
                missing = "the service is still starting"
            elif seen.state == OURS:
                ready, missing = self._ready(seen, fresh=instance is not None, deadline=deadline)
                if ready and self.seams.clock() <= deadline:
                    pid = seen.pid
                    label = f"instance {current} · " if current else ""
                    return Outcome("ready", f"gateway ready: {label}pid {pid} · {self.base_url}")
                if ready:
                    missing = "the gateway answered only after the deadline"
            elif seen.state == STARTING:
                missing = "the gateway is not listening yet"
            else:
                missing = seen.detail
            remaining = deadline - self.seams.clock()
            if remaining <= 0 or once:
                return Outcome("timeout", f"the gateway is not ready after {max_wait:g} s: {missing}",
                               "read its log: claude-multi gateway logs", self._tail(log_path))
            self.seams.sleep(min(POLL, remaining))

    def _unit_starting(self, deadline: float) -> bool:
        """Whether a supervised unit is still on its way up (its gateway has
        not taken the instance lock yet). Never true on demand."""

        if self.backend != endpoint.SYSTEMD:
            return False
        remaining = deadline - self.seams.clock()
        if remaining < MIN_CALL:
            return True  # no time left to ask: the wait reports its timeout
        state_now = service.manager_state(str(self.unit), runner=self.seams.runner, timeout=remaining)
        return state_now in MANAGER_RUNNING

    def _ready(self, seen: Observation, *, fresh: bool, deadline: float | None = None) -> tuple[bool, str]:
        status = self._health(deadline)
        if status != 200:
            return False, ("/healthz does not answer" if status is None else f"/healthz answered HTTP {status}")
        if not fresh:
            return True, ""
        sentinel = published_sentinel(self.home)
        if sentinel is None:
            return False, "the published configuration cannot be read"
        timeout = self._left(deadline, MODELS_TIMEOUT)
        if timeout <= 0:
            return False, "no time was left for the models check"
        try:
            from claude_multi import launch

            token = launch.read_gateway_token(self.document, home=self.home)
            if self.seams.models_get is not None:
                code, served = self.seams.models_get(self.base_url, token, seen.listener)
            else:
                code, served = _default_models_get(self.base_url, token, seen.listener, timeout=timeout)
        except AssertionError:
            raise
        except Exception:
            return False, "the models check failed"
        ids = {item if isinstance(item, str) else getattr(item, "id", None) for item in (served or ())}
        if code != 200:
            return False, f"the models check answered HTTP {code}"
        if sentinel not in ids:
            return False, f"the render sentinel {sentinel} is not served yet"
        return True, ""

    def _ensure_service(self, deadline: float, max_wait: float) -> Outcome:
        """The systemd backend's ensure: the same ownership classification as
        on-demand before any manager request, then (stopped only) one enqueued
        unit start, all within the deadline."""

        unit = str(self.unit)
        seen = self.observe(deadline=self._call_deadline(deadline, max_wait))
        if seen.state in (OURS, STARTING):
            return self._await_ready(deadline, max_wait)
        if seen.state != STOPPED:
            return self._refused(seen, "the gateway was not started and no token was sent")
        remaining = deadline - self.seams.clock()
        if remaining < MIN_CALL:
            return self._out_of_time(max_wait, "the service was not asked to start")
        active = service.manager_state(unit, runner=self.seams.runner, timeout=remaining)
        if active in MANAGER_RUNNING:
            return self._await_ready(deadline, max_wait, dispatched=True)
        remaining = deadline - self.seams.clock()
        if remaining < MIN_CALL:
            return self._out_of_time(max_wait, "the service was not asked to start")
        if not service.unit_control("start", unit, runner=self.seams.runner, timeout=remaining, no_block=True):
            if self.seams.clock() >= deadline:
                return self._out_of_time(max_wait, f"the service manager did not confirm the start of {unit}")
            return Outcome("failed", f"the service manager did not start {unit}",
                           f"why: {service.hint('why', backend=self.remedy_backend)}")
        return self._await_ready(deadline, max_wait, dispatched=True)

    # ------------------------------------------------------------ hold
    def persistence_hold(self, seen: Observation | None = None) -> gateway_hold.HoldReport:
        """The hold over the backend's log source; ``seen`` is a fresh
        observation (the systemd journal is read from the running instance's
        start)."""

        if self.backend == endpoint.SYSTEMD:
            unit = str(self.unit)
            seen = seen if seen is not None else self.observe()
            running = None
            if seen.stamp is not None and seen.state in (OURS, STARTING):
                try:
                    running = (seen.stamp.instance, datetime.datetime.fromisoformat(seen.stamp.started_at))
                except ValueError:
                    running = None
                if running is not None and running[1].tzinfo is None:
                    running = None

            def reader(since: datetime.datetime) -> observation.LogWindow:
                if self.seams.journal is not None:
                    return self.seams.journal(unit, since)
                return service.read_gateway_journal(max_bytes=gateway_hold.JOURNAL_BUDGET, unit=unit,
                                                    since=f"@{int(since.timestamp())}")

            return gateway_hold.evaluate_journal(self.state_root, self.providers(), reader=reader,
                                                 running=running, now=self._now_text)
        return gateway_hold.evaluate_files(self.state_root, self.providers(), now=self._now_text)

    def _now_text(self) -> str:
        return self.seams.now().astimezone(datetime.timezone.utc).isoformat(timespec="microseconds")

    def clear_hold(self, report: gateway_hold.HoldReport) -> tuple[Mapping[str, str], ...]:
        """Clear what ``report`` showed; returns the holds recorded after it (still held)."""

        service.ensure_gateway_workdir(self.state_root)
        return gateway_hold.clear(self.state_root, report)

    # ------------------------------------------------------------ stop / restart
    def stop(self, *, wait: float = STOP_TIMEOUT) -> Outcome:
        """Stop a gateway proven ours, after the persistence hold."""

        lock, refused = self.lock_for_change("nothing was stopped")
        if lock is None:
            assert refused is not None
            return refused
        try:
            return self.stop_locked(wait=wait)
        finally:
            lock.release()

    def stop_locked(self, *, wait: float = STOP_TIMEOUT) -> Outcome:
        """:meth:`stop` for a caller that holds the start lock."""

        try:
            self.reload_endpoint()
        except endpoint.EndpointError as exc:
            return Outcome("refused", f"{exc}; nothing was stopped", exc.remedy)
        inhibited = self.inhibition_refusal("nothing was stopped")
        if inhibited is not None:
            return inhibited
        if self.backend == endpoint.SYSTEMD:
            return self._stop_service(wait)
        seen = self.observe()
        if seen.state == STOPPED:
            return Outcome("stopped", "the gateway is not running")
        if seen.state not in (OURS, STARTING):
            return self._refused(seen, "nothing was stopped")
        hold = self.persistence_hold(seen)
        if hold.held:
            return Outcome("held", "the gateway was not stopped: " + gateway_hold.describe(hold),
                           gateway_hold.remedy(hold))
        again = self.observe()
        if again.stamp != seen.stamp or again.state not in (OURS, STARTING) or again.pid is None:
            return self._refused(again, "the gateway changed meanwhile; nothing was stopped")
        self.seams.terminate(again.pid)
        return self.await_stopped(wait, again)

    def await_stopped(self, wait: float, target: Observation) -> Outcome:
        deadline = self.seams.clock() + wait
        while True:
            seen = self.observe()
            if seen.state == STOPPED:
                self.reset_history()  # a deliberate stop is not a crash
                label = f"instance {target.stamp.instance} " if target.stamp and target.stamp.instance else ""
                return Outcome("stopped", f"gateway stopped ({label}pid {target.pid})")
            remaining = deadline - self.seams.clock()
            if remaining <= 0:
                return Outcome("timeout", f"the gateway (pid {target.pid}) did not exit within {wait:g} s",
                               "check it with: claude-multi gateway status")
            self.seams.sleep(min(POLL, remaining))

    def _stop_service(self, wait: float) -> Outcome:
        unit = str(self.unit)
        seen = self.observe()
        if seen.state == STOPPED and service.manager_state(unit, runner=self.seams.runner) != "active":
            return Outcome("stopped", "the gateway is not running")
        if seen.state not in (OURS, STARTING, STOPPED):
            return self._refused(seen, "nothing was stopped")
        hold = self.persistence_hold(seen)
        if hold.held:
            return Outcome("held", "the gateway was not stopped: " + gateway_hold.describe(hold),
                           gateway_hold.remedy(hold))
        if not service.unit_control("stop", unit, runner=self.seams.runner):
            return Outcome("failed", f"the service manager did not stop {unit}",
                           f"why: {service.hint('why', backend=self.remedy_backend)}")
        return self.await_stopped(wait, seen)

    def restart(self, *, max_wait: float = START_READINESS_TIMEOUT) -> Outcome:
        """Stop (held, proven ours) then start explicitly."""

        stopped = self.stop()
        if not stopped.ok:
            return stopped
        return self.ensure(max_wait=max_wait, explicit=True)

    # ------------------------------------------------------------ install facts
    def installed_binary(self) -> str | None:
        """The gateway executable this install starts, when the launcher knows it
        (the wrapper's pin, the package's own gateway link, else the gateway on
        the launcher's PATH)."""

        pinned = self.environ.get("CLAUDE_MULTI_PROXY_BIN")
        if pinned:
            return pinned
        packaged = packaged_gateway(installs.install_root(self.environ, self.installation))
        if packaged is not None:
            return packaged
        return shutil.which(service.GATEWAY_BINARY, path=self.environ.get("PATH") or None)

    def running_binary(self) -> str | None:
        """The binary the running gateway's start record names: an install
        cleanup must keep it while that gateway runs."""

        stamp = service.read_exec_stamp(self.workdir)
        return stamp.binary if stamp is not None else None

    def binary_drift(self, seen: Observation) -> str | None:
        """Restart-pending text when the running gateway runs another binary
        than this install provides (or one that is no longer installed)."""

        stamp = seen.stamp
        if stamp is None or seen.state not in (OURS, STARTING):
            return None
        restart = service.hint_code("restart", backend=self.remedy_backend)
        if not os.path.lexists(stamp.binary):
            return (f"gateway restart pending: the running gateway's binary is no longer installed "
                    f"({stamp.binary}); restart it between turns: {restart}")
        installed = self.installed_binary()
        if installed is None:
            return None
        try:
            same = os.path.samefile(installed, stamp.binary)
        except OSError:
            same = os.path.realpath(installed) == os.path.realpath(stamp.binary)
        if same:
            return None
        return (f"gateway restart pending: the running gateway runs {stamp.binary}, this install "
                f"provides {installed}; restart it between turns: {restart}")

    # ------------------------------------------------------------ status / logs
    def status(self) -> "StatusReport":
        seen = self.observe()
        health = self._health() if seen.state in (OURS, STARTING) else None
        try:
            hold = self.persistence_hold(seen)
        except (OSError, errors.ClaudeMultiError) as exc:
            hold = gateway_hold.HoldReport(True, (f"the hold cannot be evaluated: {exc}",), "unknown")
        log = None
        logs: tuple[file_log.InstanceLog, ...] = ()
        if self.backend == endpoint.ON_DEMAND:
            current = seen.stamp.instance if seen.stamp is not None else None
            logs = file_log.instance_logs(self.logs_dir)
            log = (file_log.find_instance(self.logs_dir, current) if current else None) or self.newest_log()
        return StatusReport(
            backend=self.backend, unit=self.unit, channel=endpoint.channel(self.environ),
            base_url=self.base_url, configured=self.config is not None, observation=seen,
            health=health, log=log, starts=len(self.recent_starts()), hold=hold,
            log_files=len(logs), log_bytes=file_log.total_size(logs),
            proxy_url=self.config.proxy_url if self.config is not None else None,
            drift=self.binary_drift(seen),
            inhibition=gateway_inhibition.read(self.state_root),
            set_up=not self.not_set_up(),
            now=self.seams.now(),
        )

    def logs(self, *, lines: int = 50, instance: str | None = None) -> list[str]:
        """The newest ``lines`` gateway log lines (the unit's journal, or an
        instance's log file), each redacted: no key, token, cookie or
        account identifier the gateway logged is shown. The lines above them
        are read too and redacted with them, so a value that starts above
        the first line shown is still recognized."""

        read = max(0, lines) + secret_store.TAIL_CONTEXT_LINES
        if self.backend == endpoint.SYSTEMD:
            result = service.journal_tail(str(self.unit), lines=read, runner=self.seams.runner)
            if result is None:
                raise LifecycleError(f"the {self.unit} journal is not readable")
        else:
            log = file_log.find_instance(self.logs_dir, instance) if instance else self.newest_log()
            if log is None:
                raise LifecycleError("no gateway log yet" if instance is None else f"no log for instance {instance}")
            result = file_log.tail_lines(log.path, lines=read)
        return secret_store.redact_tail(result, lines)


@dataclass(frozen=True)
class StatusReport:
    backend: str
    unit: str | None
    channel: str | None
    base_url: str
    configured: bool
    observation: Observation
    health: int | None
    log: file_log.InstanceLog | None
    starts: int
    hold: gateway_hold.HoldReport
    log_files: int = 0
    log_bytes: int = 0
    proxy_url: str | None = None
    drift: str | None = None
    inhibition: gateway_inhibition.Inhibition | None = None
    set_up: bool = True
    now: datetime.datetime | None = None

    @property
    def supervision(self) -> str:
        return "systemd (restarts on failure)" if self.backend == endpoint.SYSTEMD else "none (on-demand)"

    @property
    def confinement(self) -> str:
        return CONFINEMENT[self.backend]

    def lines(self, environ: Mapping[str, str]) -> list[str]:
        seen = self.observation
        backend = self.backend + (f" ({self.unit})" if self.unit else "")
        source = ("endpoint.json" if self.configured else "packaged default" if self.set_up
                  else f"not set up: {endpoint.SETUP_COMMAND}")
        lines = [
            f"gateway: {seen.state} — {seen.detail}",
            f"  backend: {backend} · channel: {self.channel or 'unknown'}",
            f"  supervision: {self.supervision} · confinement: {self.confinement}",
            f"  endpoint: {self.base_url} ({source})",
        ]
        if self.proxy_url:
            lines.append(f"  outbound proxy: {self.proxy_url}")
        stamp = seen.stamp
        if stamp is not None and seen.state in (OURS, STARTING):
            label = stamp.instance or "unrecorded"
            lines.append(f"  instance: {label} · pid {stamp.pid} · started {stamp.started_at}")
            lines.append("  health: " + ("answering" if self.health == 200 else
                                         "no answer" if self.health is None else f"HTTP {self.health}"))
        if self.log is not None:
            lines.append(f"  log: {paths.display(self.log.path, environ)} ({self.log.size} bytes; "
                         f"{self.log_files} instance log(s), {self.log_bytes} bytes in all)")
        if self.drift:
            lines.append(f"  {self.drift}")
        lines.append(f"  starts in the last {int(CRASH_WINDOW.total_seconds() // 60)} minutes: "
                     f"{self.starts} of {CRASH_LIMIT}")
        if self.hold.held:
            cause = f" ({gateway_hold.COVERAGE_ONLY})" if self.hold.coverage_only else ""
            lines.append(f"  persistence hold: HELD{cause} — stop, restart, update and uninstall are refused")
            lines.extend(f"    {reason}" for reason in self.hold.reasons)
            lines.append(f"    fix: {gateway_hold.remedy(self.hold)}")
        else:
            lines.append(f"  persistence hold: none ({self.hold.source} evaluated)")
        if self.inhibition is None:
            lines.append("  inhibition: none")
        else:
            message, remedy = gateway_inhibition.refusal(
                self.inhibition, "start, stop, restart and service changes are refused", now=self.now)
            lines.append(f"  inhibition: {message}")
            lines.append(f"    fix: {remedy}")
        return lines
