"""Gateway actions in the TUI: start or restart now, the log tail, the service.

Offered from H (the card's health report) and from the gateway-down banners
of the Providers and Direct screens (key W). Start is offered for a stopped
gateway and restart only for a gateway proven yours; anything else on the
port is shown, never acted on, and a fixture runtime (injected loopback
seams) is never observed or started. The supervised service's state is
shown, and installing (or refreshing) it is offered where it can run.
While a transaction owner holds the gateway's inhibition the view says so,
with its remedy, and offers no start, restart or service install.
Stopping and removing the service stay CLI verbs (:data:`CLI_ONLY`). The log
tail opens in a ``TextView``. After an action the runtime's catalog view is
reloaded (:func:`refresh_endpoint`), so a port the action recorded is what
the calling screen re-queries and what the next launch compiles.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable, TextIO

from claude_multi import errors, gateway_inhibition, gateway_lifecycle, termtext, tui
import claude_multi.cli.screens.common as screens_common

# Gateway operations the TUI deliberately leaves to the CLI, with the reason.
CLI_ONLY = {
    "gateway stop": "a launch never needs it and it is refused while the persistence hold is active; "
                    "run claude-multi gateway stop in a terminal",
    "gateway clear-hold": "it needs a typed confirmation in a terminal outside Claude Code: "
                          "claude-multi gateway clear-hold",
    "gateway service uninstall": "it reverses a deliberate choice and changes the service manager; run "
                                 "claude-multi gateway service uninstall in a terminal",
}
LOG_TAIL_LINES = 200
TITLE = "Local gateway"


@dataclass(frozen=True)
class GatewayView:
    gateway: Any | None
    seen: gateway_lifecycle.Observation | None  # None: not observed (fixture or error)
    error: str | None = None
    service: Any | None = None  # gateway_service.GatewayService (live views only)
    service_status: Any | None = None
    runtime: Any | None = None  # the runtime the view was observed for (refreshed after an action)
    inhibition: str | None = None  # the inhibited text (with its remedy) while one is recorded

    @property
    def can_install_service(self) -> bool:
        status = self.service_status
        return (self.seen is not None and status is not None and status.supported is None and self.inhibition is None
                and not status.foreign and (not status.installed or bool(status.stale)))

    def service_line(self) -> str | None:
        status = self.service_status
        if status is None:
            return None
        if status.installed:
            return f"service: installed ({status.name})" + (" — stale, install refreshes it" if status.stale else "")
        if status.supported is not None:
            return "service: not available here (on-demand only)"
        return "service: not installed (the gateway starts on demand)"

    @property
    def can_start(self) -> bool:
        return self.seen is not None and self.seen.state == gateway_lifecycle.STOPPED and self.inhibition is None

    @property
    def can_restart(self) -> bool:
        return self.seen is not None and self.seen.state == gateway_lifecycle.OURS and self.inhibition is None

    def headline(self) -> str:
        if self.error is not None:
            return f"Gateway: {self.error}"
        if self.seen is None:
            return "Gateway: not observed here"
        return f"Gateway: {self.seen.state} — {self.seen.detail}"


def observe(runtime: Any) -> GatewayView:
    """One credential-free observation of the runtime's gateway."""

    try:
        gateway = runtime.gateway()
    except (errors.ClaudeMultiError, OSError) as exc:
        return GatewayView(None, None, termtext.visible_message(exc), runtime=runtime)
    live = runtime.gateway_seams is not None or (
        runtime.health_get is None and runtime.served_models_callback is None)
    if not live:
        return GatewayView(gateway, None, runtime=runtime)
    manager = status = None
    try:
        manager = runtime.gateway_service()
        status = manager.status()
    except (errors.ClaudeMultiError, OSError):
        manager = status = None
    return GatewayView(gateway, gateway.observe(), service=manager, service_status=status, runtime=runtime,
                       inhibition=_inhibition_text(gateway))


def _inhibition_text(gateway: Any) -> str | None:
    record = gateway_inhibition.read(gateway.state_root)
    if record is None:
        return None
    message, remedy = gateway_inhibition.refusal(record, "start, restart and service changes wait for it")
    return f"{message} — fix: {remedy}"


def refresh_endpoint(runtime: Any | None) -> None:
    """Reload the runtime's catalog view after a gateway action (a start may
    have recorded a new install's port, a service install its endpoint)."""

    if runtime is None:
        return
    try:
        runtime.reload_catalog()
    except (errors.ClaudeMultiError, OSError):
        pass  # the previous view stays; the next screen load reports the problem


def act(view: GatewayView, action: str) -> gateway_lifecycle.Outcome:
    """Run ``start`` (stopped only), ``restart`` (proven ours only) or the
    service install, then refresh the runtime's endpoint view."""

    runtime = view.runtime
    if runtime is not None and not getattr(runtime, "allow_state_writes", True):
        return gateway_lifecycle.Outcome("refused", "this run is read-only; nothing was done",
                                         "run claude-multi from a terminal to act on the gateway")
    if action == "start" and view.can_start:
        outcome = view.gateway.ensure(explicit=True, choose_port=True)
    elif action == "restart" and view.can_restart:
        outcome = view.gateway.restart()
    elif action == "service" and view.can_install_service:
        outcome = view.service.install()
    else:
        return gateway_lifecycle.Outcome("refused", f"{view.headline()}; nothing was done",
                                         "check it with: claude-multi gateway status")
    refresh_endpoint(runtime)
    return outcome


def log_view(view: GatewayView) -> tuple[str, list[str]]:
    """``(title, lines)`` of the newest log tail (or why there is none)."""

    if view.gateway is None:
        return "gateway log", [view.headline()]
    try:
        lines = view.gateway.logs(lines=LOG_TAIL_LINES)
    except (gateway_lifecycle.LifecycleError, errors.ClaudeMultiError, OSError) as exc:
        return "gateway log", [termtext.visible_message(exc)]
    return f"gateway log — last {LOG_TAIL_LINES} lines", [termtext.visible_text(line) for line in lines] or ["(empty)"]


_ACTION_TEXT = {"start": "Starting the gateway", "restart": "Restarting the gateway",
                "service": "Installing the gateway service"}


def _run_suspended(win: Any, view: GatewayView, action: str, tty_in: TextIO, tty_out: TextIO) -> str:
    with tui.suspended_curses(win):
        tty_out.write(f"{_ACTION_TEXT[action]} (at most {gateway_lifecycle.START_READINESS_TIMEOUT:g} s)…\n")
        tty_out.flush()
        outcome = act(view, action)
        for line in outcome.lines():
            tty_out.write(termtext.visible_text(line) + "\n")
        tty_out.write("\nPress Enter to return to claude-multi.")
        tty_out.flush()
        try:
            tty_in.readline()
        except KeyboardInterrupt:
            pass
    return outcome.message


def dialog(runtime: Any, win: Any, palette: tui.Palette, *, background: Callable[[Any], None] | None = None,
           tty_in: TextIO | None = None, tty_out: TextIO | None = None) -> str:
    """The gateway dialog; returns a one-line status for the calling screen."""

    view = observe(runtime)
    buttons: list[tuple[str, str | None]] = []
    if view.can_start:
        buttons.append(("Start now", "start"))
    if view.can_restart:
        buttons.append(("Restart now", "restart"))
    if view.can_install_service:
        buttons.append(("Install service", "service"))
    buttons += [("Show log", "log"), ("Close", None)]
    lines = [view.headline()]
    if view.gateway is not None:
        lines.append(f"endpoint {view.gateway.base_url} · backend {view.gateway.backend}")
    if view.service_line() is not None:
        lines.append(view.service_line())
    if view.inhibition is not None:
        lines.append(view.inhibition)
    lines.append("Stop it from a terminal: claude-multi gateway stop.")
    # Wrapped to the box, so no remedy is cut off (the inhibition's above all).
    body = [piece for line in lines for piece in screens_common.modal_lines(line, win)]
    choice = tui.Modal(TITLE, body, buttons=tuple(buttons)).run(win, palette, background=background)
    if choice == "log":
        title, text = log_view(view)
        tui.TextView(title, text, palette=palette).run(win)
        return ""
    if choice in ("start", "restart", "service"):
        return _run_suspended(win, view, choice, tty_in or sys.stdin, tty_out or sys.stdout)
    return ""


def health_prompt(runtime: Any, tty_in: TextIO, tty_out: TextIO) -> str | None:
    """H's gateway line (inside the suspended region): the state, the
    offered keys and the return prompt. Returns ``"log"`` when the log tail
    should open once curses is back, else None."""

    view = observe(runtime)
    tty_out.write("\n" + termtext.visible_text(view.headline()) + "\n")
    if view.inhibition is not None:
        tty_out.write(termtext.visible_text(view.inhibition) + "\n")
    offers = []
    if view.can_start:
        offers.append("s  start it now")
    if view.can_restart:
        offers.append("r  restart it now")
    if view.can_install_service:
        offers.append("i  install the gateway service")
    offers.append("l  show its log")
    tty_out.write("  " + " · ".join(offers) + "\n")
    tty_out.write("Press Enter to return to claude-multi.")
    tty_out.flush()
    try:
        answer = tty_in.readline().strip().lower()
    except KeyboardInterrupt:
        return None
    if answer == "l":
        return "log"
    action = {"s": "start", "r": "restart", "i": "service"}.get(answer)
    if action is None or (action == "start" and not view.can_start) or (
            action == "restart" and not view.can_restart) or (
            action == "service" and not view.can_install_service):
        return None
    tty_out.write(f"\n{_ACTION_TEXT[action]} (at most {gateway_lifecycle.START_READINESS_TIMEOUT:g} s)…\n")
    tty_out.flush()
    for line in act(view, action).lines():
        tty_out.write(termtext.visible_text(line) + "\n")
    tty_out.write("\nPress Enter to return to claude-multi.")
    tty_out.flush()
    try:
        tty_in.readline()
    except KeyboardInterrupt:
        pass
    return None
