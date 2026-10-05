"""``claude-multi gateway``: the local gateway's lifecycle verbs.

``start`` (also records a new install's port), ``ensure`` (for scripts and the
token helper: exit 0 only when the gateway is ours and ready), ``stop`` and
``restart`` (only a gateway proven ours, never under the persistence hold),
``status`` and ``logs`` (read-only, no token sent), ``clear-hold`` (the
user's typed confirmation that the held credentials were verified) and
``service install|uninstall|status`` (the opt-in supervised systemd user
unit; see ``gateway_service``). Exit statuses: 0 ok; 1 refused, not ready or
failed (the message names which); 2 a usage error; 3 a declined
confirmation; 130 cancelled.

``claude-multi setup --step gateway [--proxy URL | --no-proxy]`` is the
gateway's setup step: it records (or clears) the gateway's outbound proxy in
``endpoint.json``, then starts the gateway, or re-renders a running one so
the proxy applies.

While the state root's inhibition is recorded (see ``gateway_inhibition``),
start, stop, restart, the service verbs and the setup step refuse with the
inhibited outcome (exit 1); ``ensure`` still answers for a gateway already
proven ours. The transaction owner's own explicit verbs pass by carrying its
token in ``CLAUDE_MULTI_INHIBITION_TOKEN``; ``ensure`` (the token helper)
never acts for the owner.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, TextIO

from claude_multi import endpoint, errors, gateway_inhibition, gateway_lifecycle, termtext
import claude_multi.cli.consent as consent

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod

CLEAR_WORD = "clear-hold"
LOG_LINES_MAX = 5000
EXIT_FAILED, EXIT_DECLINED, EXIT_CANCELLED = 1, 3, 130


def _write(stream: TextIO, lines: list[str]) -> None:
    for line in lines:
        stream.write(termtext.visible_text(line) + "\n")
    stream.flush()


def _report(outcome: gateway_lifecycle.Outcome, output_stream: TextIO) -> int:
    lines = outcome.lines()
    if outcome.ok:
        _write(output_stream, lines)
    else:
        _write(sys.stderr, ["claude-multi: " + lines[0], *lines[1:]])
    return outcome.exit_code


def _clear_hold(gateway: gateway_lifecycle.Gateway, runtime: "runtime_mod.Runtime",
                input_stream: TextIO, output_stream: TextIO) -> int:
    consent.require_human("claude-multi gateway clear-hold", runtime.environ)
    report = gateway.persistence_hold()
    if not report.held:
        _write(output_stream, ["persistence hold: none — nothing to clear"])
        return 0
    prompt = consent.prompt_stream()
    _write(prompt, [
        "persistence hold:",
        *(f"  {reason}" for reason in report.reasons),
        "Clear it only after you verified the affected credentials (a persisted save, or a new sign-in).",
        "Stop and restart are allowed again until the next unresolved save.",
    ])
    prompt.write(f"Type {CLEAR_WORD} to confirm: ")
    prompt.flush()
    answer = (consent._answer_stream(input_stream).readline() or "").strip()
    if answer != CLEAR_WORD:
        _write(output_stream, ["not cleared — nothing written"])
        return EXIT_DECLINED
    newer = gateway.clear_hold(report)
    if newer:
        _write(output_stream, [
            "persistence hold cleared for what was shown; evidence recorded after it still holds:",
            *(f"  instance {item['instance']}: {item['reason']}" for item in newer),
            f"Verify those credentials too, then run claude-multi gateway {CLEAR_WORD} again.",
        ])
        return 1
    _write(output_stream, ["persistence hold cleared"])
    return 0


def _require_writes(runtime: "runtime_mod.Runtime", what: str) -> None:
    """The launcher write guard at the mutation boundary: a read-only Runtime
    and state of a newer release are refused before anything is started,
    stopped or recorded."""

    from claude_multi import sessions

    sessions.check_state_marker(runtime.session_store.root)
    if not runtime.allow_state_writes:
        raise errors.CLIError(f"{what} is not available in a read-only run; nothing was changed")


def _public(run) -> int:
    """Run one verb with the public exit statuses: a refusal (read-only run,
    unreadable endpoint, guard) is 1 like every other not-ok outcome, an
    interrupt 130; state of a newer release keeps the launcher-wide exit."""

    from claude_multi import sessions

    try:
        return run()
    except KeyboardInterrupt:
        _write(sys.stderr, ["claude-multi: cancelled"])
        return EXIT_CANCELLED
    except sessions.StateMarkerError:
        raise
    except errors.ClaudeMultiError as exc:
        lines = ["claude-multi: " + termtext.visible_message(exc)]
        if exc.remedy:
            lines.append(f"  fix: {exc.remedy}")
        _write(sys.stderr, lines)
        return EXIT_FAILED


def _as_owner(gateway: gateway_lifecycle.Gateway, runtime: "runtime_mod.Runtime") -> gateway_lifecycle.Gateway:
    """An explicit verb run by the inhibition's owner (its token in the
    environment) acts under that inhibition; everyone else is refused by it."""

    gateway.inhibition_token = runtime.environ.get(gateway_inhibition.TOKEN_ENV) or None
    return gateway


def command(runtime: "runtime_mod.Runtime", args: argparse.Namespace, *,
            input_stream: TextIO, output_stream: TextIO) -> int:
    return _public(lambda: _command(runtime, args, input_stream=input_stream, output_stream=output_stream))


def _command(runtime: "runtime_mod.Runtime", args: argparse.Namespace, *,
             input_stream: TextIO, output_stream: TextIO) -> int:
    import claude_multi.cli.parser as parser_mod

    verb = args.gateway_command
    if parser_mod._gateway_command_writes(args):
        _require_writes(runtime, f"claude-multi gateway {verb}")
    try:
        gateway = runtime.gateway()
    except endpoint.EndpointError as exc:
        raise errors.CLIError(str(exc), remedy=exc.remedy) from exc
    try:
        if verb == "status":
            _write(output_stream, gateway.status().lines(runtime.environ))
            return 0
        if verb == "logs":
            count = max(1, min(int(args.lines), LOG_LINES_MAX))
            _write(output_stream, gateway.logs(lines=count, instance=args.instance))
            return 0
        if verb == "clear-hold":
            return _clear_hold(gateway, runtime, input_stream, output_stream)
        if verb == "start":
            return _report(_as_owner(gateway, runtime).ensure(explicit=True, choose_port=True), output_stream)
        if verb == "ensure":
            outcome = gateway.ensure(max_wait=max(0.0, float(args.max_wait)), destination=args.base_url)
            if outcome.ok and args.quiet:
                return 0
            return _report(outcome, output_stream)
        if verb == "stop":
            return _report(_as_owner(gateway, runtime).stop(), output_stream)
        if verb == "restart":
            return _report(_as_owner(gateway, runtime).restart(), output_stream)
        if verb == "service":
            return _service(runtime, args, output_stream)
    except gateway_lifecycle.LifecycleError as exc:
        raise errors.CLIError(str(exc), remedy=exc.remedy) from exc
    raise errors.CLIError(f"unsupported gateway command {verb!r}")


def _service(runtime: "runtime_mod.Runtime", args: argparse.Namespace, output_stream: TextIO) -> int:
    from claude_multi import gateway_service

    try:
        manager = runtime.gateway_service()
        verb = args.gateway_service_command
        if verb == "status":
            _write(output_stream, manager.status().lines(runtime.environ))
            return 0
        if verb == "install":
            _as_owner(manager.gateway, runtime)
            return _report(manager.install(args.name), output_stream)
        if verb == "uninstall":
            _as_owner(manager.gateway, runtime)
            return _report(manager.uninstall(), output_stream)
    except gateway_service.ServiceSetupError as exc:
        raise errors.CLIError(str(exc), remedy=exc.remedy) from exc
    raise errors.CLIError(f"unsupported gateway service command {args.gateway_service_command!r}")


def setup_command(runtime: "runtime_mod.Runtime", args: argparse.Namespace, *,
                  input_stream: TextIO, output_stream: TextIO) -> int:
    """``claude-multi setup --step gateway [--proxy URL | --no-proxy]``."""

    return _public(lambda: _setup(runtime, args, output_stream=output_stream))


def _change_proxy(runtime: "runtime_mod.Runtime", args: argparse.Namespace, *, running: bool) -> object:
    """Record (or clear) the outbound proxy through the setup layer's one
    transaction: checked first, written inside the writer fence of every
    root that may own this home's gateway with its exact undo registered
    before the write, and for a running gateway rendered and its reload
    verified in the same served change (a refused render puts
    ``endpoint.json`` back as it was)."""

    from claude_multi.setup import model as setup_model, providers as setup_providers

    plan = setup_providers.plan_proxy(runtime, None if args.no_proxy else args.proxy, running=running)
    return setup_providers.apply_proxy(runtime, plan, setup_model.Confirmation.given(plan))


def _proxy_lines(args: argparse.Namespace, applied: object) -> list[str]:
    proxy_url = None if args.no_proxy else args.proxy
    return [f"gateway outbound proxy: {proxy_url}" if proxy_url else
            "gateway outbound proxy: none (the gateway connects directly)", *getattr(applied, "lines", ())]


def _setup(runtime: "runtime_mod.Runtime", args: argparse.Namespace, *, output_stream: TextIO) -> int:
    if args.step != "gateway":
        raise errors.CLIError(f"unsupported setup step {args.step!r}")
    _require_writes(runtime, "claude-multi setup")
    if runtime.endpoint_error is not None:
        raise errors.CLIError(str(runtime.endpoint_error), remedy=runtime.endpoint_error.remedy)
    try:
        gateway = _as_owner(runtime.gateway(), runtime)
        unusable = gateway.unusable_state_root()
        if unusable is not None:
            return _report(unusable, output_stream)
        # The endpoint change and the start after it happen under the start
        # lock, with the inhibition checked under it: a transaction owner's
        # begin never lands between that check and the change, and an
        # inhibition already recorded is reported at once.
        lock, refused = gateway.lock_for_change("nothing was changed")
        if lock is None:
            assert refused is not None
            return _report(refused, output_stream)
        try:
            inhibited = gateway.inhibition_refusal("nothing was changed")
            if inhibited is not None:
                return _report(inhibited, output_stream)
            running = False
            if args.proxy is not None or args.no_proxy:
                # Observed only in a home whose gateway is set up: a new
                # home's packaged port is never looked at.
                running = endpoint.set_up(runtime.home) and gateway.observe().state == gateway_lifecycle.OURS
                if not running:
                    # Recorded under the start lock, then started with it.
                    _write(output_stream, _proxy_lines(args, _change_proxy(runtime, args, running=False)))
                    gateway = _as_owner(runtime.gateway(), runtime)
            if not running:
                wait = gateway_lifecycle.START_READINESS_TIMEOUT
                return _report(gateway.ensure_locked(gateway.seams.clock() + wait, wait, explicit=True,
                                                     choose_port=True), output_stream)
        finally:
            lock.release()
        # A running gateway re-renders with the proxy in one served change,
        # with no start lock held (a launch may need it meanwhile); the
        # endpoint write checks the inhibition again inside its own fence.
        from claude_multi.cli.commands import providers as commands_providers

        applied = _change_proxy(runtime, args, running=True)
        _write(output_stream, _proxy_lines(args, applied))
        return commands_providers.applied_exit(applied)
    except gateway_lifecycle.LifecycleError as exc:
        raise errors.CLIError(str(exc), remedy=exc.remedy) from exc
