"""Adapters to Claude Code acquisition, the local gateway and the
installation: the one place the setup layer names those modules' functions.

Every live observation goes through the runtime's seams; a runtime that
injects loopback seams describes a fixture gateway that is already running,
so nothing here starts, stops or probes a real one for it.
"""

from __future__ import annotations

import os
import platform as platform_mod
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from claude_multi import endpoint, errors, paths
from claude_multi.setup import model


@dataclass(frozen=True)
class ClaudeStatus:
    version: str
    state: str  # verified | missing | mismatch | unavailable
    detail: str
    fix: model.Fix | None


CLAUDE_FIX = model.Fix("Enter on step 2", "claude-multi setup --step claude",
                       ("claude-multi", "setup", "--step", "claude"))


def claude_status(runtime: Any) -> ClaudeStatus:
    """The owned copy of the pinned Claude Code (metadata only; launches hash it)."""

    from claude_multi import pin

    contract = runtime.catalog.docs["native-contract"]
    try:
        version = pin.version(contract)
    except (KeyError, TypeError, ValueError):
        return ClaudeStatus("?", "unavailable", "the packaged Claude Code pin is unreadable", None)
    state, text = pin.copy_state(contract, runtime.environ, retained_root=paths.retained_root(runtime.environ))
    if state in ("ready", "pending"):
        return ClaudeStatus(version, "verified", "verified copy", None)
    if state == "not set up":
        return ClaudeStatus(version, "missing", text or "not installed", CLAUDE_FIX)
    if state == "damaged":
        return ClaudeStatus(version, "mismatch", text or "damaged", CLAUDE_FIX)
    return ClaudeStatus(version, "unavailable", text or state, None)


SUPPORTED_FIX = model.Fix(None, "see Supported systems in the claude-multi guide")


def claude_verified(runtime: Any) -> ClaudeStatus:
    """The owned copy of the pinned Claude Code, checked as a launch checks
    it: a regular executable file of the pinned size and sha256 (read-only).

    Only that copy counts: a missing one is ``missing`` even while a kept
    copy waits to be put in place, and any difference is ``mismatch``; both
    name the setup step that puts the right copy there."""

    from claude_multi import pin

    contract = runtime.catalog.docs["native-contract"]
    try:
        version = pin.version(contract)
    except (KeyError, TypeError, ValueError):
        return ClaudeStatus("?", "unavailable", "the packaged Claude Code pin is unreadable", None)
    if pin.platform_record(contract, pin.host_platform()) is None:
        return ClaudeStatus(version, "unavailable", f"this release has no verified Claude Code {version} build for "
                            f"{pin.host_platform()}", SUPPORTED_FIX)
    try:
        pin.verify_owned(contract, runtime.environ)
    except FileNotFoundError:
        return ClaudeStatus(version, "missing", "not set up for claude-multi", CLAUDE_FIX)
    except pin.PinError as exc:
        return ClaudeStatus(version, "mismatch", f"the claude-multi copy cannot be used: {exc}", CLAUDE_FIX)
    except OSError as exc:
        return ClaudeStatus(version, "mismatch",
                            f"the claude-multi copy cannot be read ({exc.strerror or type(exc).__name__})",
                            CLAUDE_FIX)
    return ClaudeStatus(version, "verified", "verified copy", None)


def acquire_claude(runtime: Any, *, consent: Callable[[Any], bool], progress: Callable[[str], None],
                   claude_from: Path | None = None) -> Any:
    """Copy (or, after ``consent`` says yes to the shown plan, download) the
    pinned Claude Code; the acquisition's own outcome is returned."""

    from claude_multi import acquire

    contract = runtime.catalog.docs["native-contract"]
    return acquire.acquire(contract, runtime.environ, claude_from=claude_from, consent=consent, progress=progress)


@dataclass(frozen=True)
class GatewayState:
    state: str  # running | todo | setup (no port recorded yet) | blocked
    backend: str
    port: int | None
    detail: str
    fix: model.Fix | None


GATEWAY_FIX = model.Fix("Enter on step 3", "claude-multi setup --step gateway",
                        ("claude-multi", "setup", "--step", "gateway"))
NOT_SET_UP = "not set up yet"


def _fixture_gateway(runtime: Any) -> bool:
    return not runtime._live_gateway()


def _port(runtime: Any) -> int | None:
    try:
        return endpoint.port_of(endpoint.gateway_endpoint(runtime.catalog.docs["gateway"]).base_url)
    except (errors.ClaudeMultiError, KeyError, ValueError):
        return None


def gateway_status(runtime: Any) -> GatewayState:
    """Whether the local gateway runs and is ours (no token is sent)."""

    from claude_multi import gateway_lifecycle as lifecycle

    if runtime.endpoint_error is not None:
        return GatewayState("blocked", "unknown", None, str(runtime.endpoint_error),
                            model.Fix(None, runtime.endpoint_error.remedy))
    if _fixture_gateway(runtime):
        gateway = runtime.catalog.docs["gateway"]
        try:
            target = endpoint.gateway_endpoint(gateway)
            healthy = runtime.health_get is None or runtime.health_get(target.base_url, target.health_path) == 200
        except (OSError, errors.ClaudeMultiError, ValueError):
            healthy = False
        return GatewayState("running" if healthy else "todo", "on-demand", _port(runtime),
                            "running" if healthy else "not running", None if healthy else GATEWAY_FIX)
    if not endpoint.set_up(runtime.home):
        # A new home has no port yet: nothing of ours can run there, and the
        # packaged port (another program's, perhaps) is never looked at.
        return GatewayState("setup", "on-demand", None, NOT_SET_UP, GATEWAY_FIX)
    try:
        gateway = runtime.gateway()
        seen = gateway.observe()
    except (errors.ClaudeMultiError, OSError) as exc:
        return GatewayState("blocked", "unknown", _port(runtime), str(exc), GATEWAY_FIX)
    backend = getattr(gateway, "backend", "on-demand")
    if seen.state in (lifecycle.OURS, lifecycle.STARTING):
        return GatewayState("running", backend, _port(runtime), seen.detail, None)
    if seen.state == lifecycle.STOPPED:
        problem = start_problem(runtime, gateway)
        if problem is not None:
            return GatewayState("blocked", backend, _port(runtime), problem[0], problem[1])
        return GatewayState("todo", backend, _port(runtime), seen.detail, GATEWAY_FIX)
    return GatewayState("blocked", backend, _port(runtime), seen.detail,
                        model.Fix(None, "claude-multi gateway status"))


REINSTALL_FIX = model.Fix(None, "reinstall claude-multi")
PAUSED_FIX = model.Fix(None, "read the log (claude-multi gateway logs), fix the cause, then start it yourself: "
                             "claude-multi gateway start")


def start_problem(runtime: Any, gateway: Any) -> tuple[str, model.Fix] | None:
    """Why a stopped gateway would not start when it is needed, with its
    fix, or None. Read-only: nothing is started, written or probed.

    Its program must be an executable file, and an on-demand gateway must
    not have paused its automatic starts after starting too often in a
    short time (the lifecycle's own limit)."""

    from claude_multi import gateway_lifecycle as lifecycle, service

    binary = gateway.installed_binary()
    if binary is None:
        return f"the gateway program ({service.GATEWAY_BINARY}) is not installed", REINSTALL_FIX
    if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        return (f"the gateway program {paths.display(binary, runtime.environ)} is not an executable file",
                REINSTALL_FIX)
    if gateway.backend == endpoint.ON_DEMAND:
        starts = len(gateway.recent_starts())
        if starts >= lifecycle.CRASH_LIMIT:
            minutes = int(lifecycle.CRASH_WINDOW.total_seconds() // 60)
            return (f"automatic starts are paused: the gateway was started {starts} times in the last "
                    f"{minutes} minutes", PAUSED_FIX)
    return None


def ensure_gateway(runtime: Any) -> Any:
    """Start the gateway (an explicit start that also records a new
    install's port first); the lifecycle outcome is returned, or None when
    the runtime's own seam started it or describes one already running."""

    if runtime.gateway_ensure is not None:
        runtime.gateway_ensure(runtime)
        return None
    if _fixture_gateway(runtime):
        return None
    return runtime.gateway().ensure(explicit=True, choose_port=True)


def gateway_log_tail(runtime: Any, lines: int = 40) -> tuple[str, ...]:
    try:
        return tuple(runtime.gateway().logs(lines=lines))
    except (errors.ClaudeMultiError, OSError):
        return ()


def persistence_held(runtime: Any) -> tuple[str, ...]:
    """The reasons a credential save is still unconfirmed (empty: none)."""

    if _fixture_gateway(runtime):
        return ()
    try:
        report = runtime.gateway().persistence_hold()
    except (errors.ClaudeMultiError, OSError) as exc:
        return (f"the hold cannot be evaluated: {exc}",)
    return tuple(report.reasons) if report.held else ()


def _hold_problem(gateway: Any, seen: Any) -> str | None:
    """Why the persistence hold refuses (recorded holds and unresolved
    failures of every instance, a stopped or crashed one included), or None;
    a hold that cannot be evaluated refuses too."""

    try:
        report = gateway.persistence_hold(seen)
    except (errors.ClaudeMultiError, OSError) as exc:
        return f"the hold cannot be evaluated: {exc}"
    if not report.held:
        return None
    from claude_multi import gateway_hold

    return f"{gateway_hold.describe(report)} — {gateway_hold.remedy(report)}"


UNINSTALL_NOT_REMOVED = "nothing was removed"


class InhibitedText(str):
    """A refusal that is the gateway inhibition's own message and fix
    (shown as it is, not as a gateway problem)."""


def _inhibited(outcome: Any) -> InhibitedText:
    """An ``inhibited`` lifecycle outcome as uninstall's refusal: the
    inhibition's own message, then its fix."""

    return InhibitedText(outcome.message + (f"\n  fix: {outcome.remedy}" if outcome.remedy else ""))


def stop_gateway_for_uninstall(runtime: Any) -> tuple[str, str]:
    """Stop a gateway proven ours: ``(outcome, detail)`` where the outcome
    is ``stopped``, ``not-running``, ``hold``, ``not-ours``, ``inhibited``
    (a transaction owner — a service hand-off, an installer, an update, a
    machine move — holds the gateway's inhibition; the detail is its
    message and fix) or ``failed``. The persistence hold is evaluated
    first, also for a gateway that is not running: its logs and hold
    records are what uninstall would remove."""

    from claude_multi import gateway_lifecycle as lifecycle

    if _fixture_gateway(runtime):
        return "not-running", "fixture gateway"
    try:
        gateway = runtime.gateway()
        refusal = gateway.inhibition_refusal(UNINSTALL_NOT_REMOVED)
        if refusal is not None:
            return "inhibited", _inhibited(refusal)
        if not endpoint.set_up(runtime.home):
            # No port was ever recorded here: nothing of ours runs, and the
            # packaged port (another program's, perhaps) is never looked at.
            held = _hold_problem(gateway, None)
            return ("hold", held) if held is not None else ("not-running", NOT_SET_UP)
        seen = gateway.observe()
    except (errors.ClaudeMultiError, OSError) as exc:
        return "failed", str(exc)
    if seen.state == lifecycle.FOREIGN:
        return "not-ours", str(_port(runtime))
    held = _hold_problem(gateway, seen)
    if held is not None:
        return "hold", held
    if seen.state == lifecycle.STOPPED:
        return "not-running", seen.detail
    outcome = gateway.stop()
    if outcome.ok:
        return "stopped", outcome.message
    if outcome.status == "held":
        return "hold", outcome.message
    if outcome.status == "inhibited":
        return "inhibited", _inhibited(outcome)
    return "failed", outcome.message


def hold_gateway_starts(runtime: Any, *, what: str = UNINSTALL_NOT_REMOVED) -> tuple[Any, str | None]:
    """Hold the gateway's start lock so nothing starts it while uninstall
    removes files: ``(lock, None)``; ``(None, None)`` when there is no
    gateway to hold (a fixture, or a state root that never had one); or
    ``(None, refusal)``. Under the lock the gateway must still be stopped,
    clear of every inhibition (a transaction owner records one only while
    it holds this lock) and of the persistence hold."""

    from claude_multi import gateway_lifecycle as lifecycle

    if _fixture_gateway(runtime):
        return None, None
    try:
        gateway = runtime.gateway()
        workdir = Path(gateway.workdir)
        if not workdir.is_dir() or workdir.is_symlink():
            return None, None
        lock = gateway._acquire_start_lock(gateway.seams.clock() + lifecycle.START_LOCK_WAIT)
    except (errors.ClaudeMultiError, OSError) as exc:
        return None, f"the gateway cannot be checked: {exc}"
    if lock is None:
        return None, "a start of the gateway is in progress"
    try:
        refusal = gateway.inhibition_refusal(what)
        if refusal is not None:
            problem: str | None = _inhibited(refusal)
        elif not endpoint.set_up(runtime.home):
            problem = _hold_problem(gateway, None)  # no port recorded: nothing of ours to observe
        else:
            seen = gateway.observe()
            problem = (_hold_problem(gateway, seen) if seen.state == lifecycle.STOPPED
                       else f"the gateway is not stopped: {seen.detail}")
    except (errors.ClaudeMultiError, OSError) as exc:
        problem = f"the gateway cannot be checked: {exc}"
    if problem is not None:
        lock.release()
        return None, problem
    return lock, None


def service_state(runtime: Any) -> tuple[str, str]:
    """The supervised service as uninstall must treat it: ``(state,
    detail)`` where the state is ``absent``; ``recorded`` (endpoint.json
    records it: only the service's own uninstall removes it, and that
    refuses a unit file claude-multi did not write); ``stray`` (a unit file
    claude-multi wrote that is not recorded); or ``unknown`` (it cannot be
    checked)."""

    if _fixture_gateway(runtime):
        return "absent", ""
    try:
        status = runtime.gateway_service().status(live=False)
    except (errors.ClaudeMultiError, OSError) as exc:
        return "unknown", str(exc)
    if status.recorded:
        return "recorded", status.name
    if status.installed:
        return "stray", paths.display(status.unit_path, runtime.environ)
    return "absent", ""


def service_installed(runtime: Any) -> bool:
    if _fixture_gateway(runtime):
        return False
    try:
        return bool(runtime.gateway_service().status(live=False).installed)
    except (errors.ClaudeMultiError, OSError):
        return False


def uninstall_service(runtime: Any) -> Any:
    return runtime.gateway_service().uninstall()


def managed_policy_findings(runtime: Any) -> tuple[tuple[str, str, model.Fix | None], ...]:
    """``(severity, text, fix)`` for each managed-policy setting that
    affects managed sessions (``block`` or ``attention``; names only)."""

    from claude_multi import managed

    try:
        found = runtime.managed_findings()
    except (errors.ClaudeMultiError, OSError) as exc:
        return (("attention", f"the managed policy could not be read: {exc}", None),)
    result = []
    for lock in found:
        fix = model.Fix(None, f"ask your administrator: {lock.key} in {lock.path} blocks claude-multi")
        result.append(("block" if lock.blocks else "attention", managed.finding_text(lock), fix))
    return tuple(result)


def platform_facts(runtime: Any) -> dict[str, str]:
    """The operating system, architecture, WSL distribution and the
    filesystem types of the roots (unknown when not observable)."""

    from claude_multi.platform import mounts

    facts = {"os": sys.platform, "arch": platform_mod.machine() or "unknown"}
    try:
        facts["wsl"] = mounts.wsl(runtime.environ, proc_root=runtime.proc_root) or ""
    except OSError:
        facts["wsl"] = ""
    facts["fs_state"] = runtime.state_filesystem() or "unknown"
    return facts


def free_bytes(path: Path) -> int | None:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def channel(runtime: Any) -> str:
    """``bundle``, ``nix`` or ``checkout``: the installation this launcher runs from."""

    value = endpoint.channel(runtime.environ)
    if value in ("bundle", "nix"):
        return value
    return "checkout"


def owned_claude_dir(environ: Mapping[str, str]) -> Path:
    from claude_multi import pin

    return pin.owned_root(environ)


def is_store_path(path: Path | str) -> bool:
    return os.path.realpath(path).startswith("/nix/store/")
