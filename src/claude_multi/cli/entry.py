"""The claude-multi entry point.

``main`` parses the argv and routes it.  Its module-level imports are the
lightweight hook path only (pinned by ``test_cli_split``): every session-event
hook is dispatched before a command, screen or doctor module is imported;
scope-only events never build Runtime or load the catalog.  Everything else is
imported inside the branch that needs it.

Ordinary commands share one exit table: 0 success, 1 a failure, a refusal or
an unavailable result, 2 an invalid command line, 3 a change the person
declined, 130 an interrupt. Human errors go to stderr; results to stdout.
The hooks, ``/cm`` (skill mode) and the gateway tool keep their own protocol
statuses.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any, TextIO

from claude_multi import assets
from claude_multi import errors
from claude_multi import errors as cli_errors
from claude_multi import hooks
from claude_multi import lineup_files
from claude_multi import sessions
from claude_multi import termtext
import claude_multi.cli.parser as parser_mod
import claude_multi.cli.streams as streams
import claude_multi.cli.text as cli_text

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def main(
    argv: list[str] | None = None,
    *,
    runtime: runtime_mod.Runtime | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    interactive: bool | None = None,
) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # The presentation switches may precede any command, lineup included.
    lead = 0
    while lead < len(raw) and raw[lead] in lineup_files.PRESENTATION_FLAGS:
        lead += 1
    if raw[lead:lead + 1] == ["lineup"]:
        # Intercepted on the raw argv, before split_passthrough (which would
        # cut at `--`) and before argparse (which would reject a request
        # starting with `-`).
        import claude_multi.cli.commands.lineup as commands_lineup

        try:
            return commands_lineup._lineup_main(
                raw[lead + 1:],
                runtime=runtime,
                input_stream=input_stream,
                output_stream=output_stream,
                interactive=interactive,
            )
        except KeyboardInterrupt:
            return _interrupted(None)
    launcher_args, passthrough = parser_mod.split_passthrough(raw)
    parser = parser_mod.build_parser()
    try:
        args = parser.parse_args(launcher_args)
    except SystemExit as exc:
        # A protocol-3 hook (or a scope-only event) must never block the
        # client on an argument it cannot parse; a legacy start/end keeps
        # argparse's exit 2.
        if exc.code not in (0, None) and hooks.is_nonblocking_hook_argv(launcher_args):
            sys.stderr.write("claude-multi: hook arguments rejected; event ignored\n")
            if launcher_args[1:2] == ["premodel"]:
                # A model switch the launcher cannot check is refused (fail
                # closed), never silently allowed.
                stream = output_stream or sys.stdout
                stream.write(
                    hooks.premodel_fail_closed_response(
                        "claude-multi premodel failed: hook arguments rejected "
                        "— run claude-multi doctor"
                    )
                )
                stream.flush()
            return 0
        raise

    output = output_stream or sys.stdout
    # Refuse the retired flag before constructing Runtime (which refreshes
    # shims and creates store directories), including picker/print launches.
    if args.legacy:
        sys.stderr.write(f"claude-multi: {cli_text.LEGACY_REFUSAL}\n")
        sys.stderr.flush()
        return 2
    owned_tty: tuple[TextIO, TextIO] | None = None
    tty_in: TextIO | None = None
    tty_out: TextIO | None = None
    try:
        state_path = (
            runtime.session_store.root if runtime is not None else sessions.state_root()
        )
        if args.command == "restore-2x":
            if passthrough:
                _refuse_passthrough()
            restore_interactive = interactive
            if restore_interactive is None:
                restore_interactive = streams._stdio_streams_are_ttys()
            import claude_multi.cli.commands.migration as commands_migration

            return commands_migration._restore_2x(
                state_path,
                output,
                args,
                input_stream=input_stream,
                interactive=bool(restore_interactive),
                environ=runtime.environ if runtime is not None else None,
            )
        marker_newer = False
        try:
            sessions.check_state_marker(state_path)
        except sessions.StateMarkerError:
            if parser_mod._writes_versioned_state(args):
                raise
            marker_newer = True
            if args.resume == "" or (
                args.command == "sessions" and args.sessions_command == "list"
            ):
                # Keep the listing available, without the picker's mutation
                # actions or its launch path under incompatible state.
                interactive = False
        if args.command == "session-event":
            # Protocol-3 hooks run before Runtime is constructed. The four
            # scope-only events never load the catalog, write a shim, create
            # a store directory or take a record lock; the v3 SessionStart
            # builds its notice from scope files first and only then builds
            # Runtime for the reconcile.
            hook_input = input_stream or sys.stdin
            if args.event in hooks.SCOPE_ONLY_EVENTS:
                return hooks.dispatch(
                    args,
                    state_root=state_path,
                    environ=runtime.environ if runtime is not None else os.environ,
                    input_stream=hook_input,
                    output_stream=output,
                    error_stream=sys.stderr,
                )
            if args.event == "start" and args.hook_protocol == lineup_files.HOOK_PROTOCOL:
                import claude_multi.cli.runtime as runtime_mod
                import claude_multi.cli.session_events as session_events

                injected = runtime
                return session_events._session_start_v3(
                    args,
                    state_path=state_path,
                    runtime_factory=lambda: injected
                    or runtime_mod.Runtime(asset_root=assets.default_asset_root(), allow_state_writes=False),
                    input_stream=hook_input,
                    output_stream=output,
                    error_stream=sys.stderr,
                    environ=runtime.environ if runtime is not None else os.environ,
                )
            # Every other session event (legacy start/end, protocol-3 end) is
            # routed here, before handle_command. Runtime is built exactly as
            # the shared construction below builds it for session-event (no
            # state writes, no contract notice), then passthrough is refused,
            # then the event is handled; no command, screen or doctor module
            # is imported on this path.
            import claude_multi.cli.runtime as runtime_mod
            import claude_multi.cli.session_events as session_events

            runtime = runtime or runtime_mod.Runtime(
                asset_root=assets.default_asset_root(),
                allow_state_writes=False,
                refresh_shims=not bool(getattr(args, "print_launch", False)),
                initialize_session_store=True,
            )
            if passthrough:
                raise cli_errors.CLIError("passthrough arguments are accepted only for launch")
            return session_events._handle_session_event(
                runtime,
                args,
                input_stream=hook_input,
                output_stream=output,
            )
        # Commands, screens and launch flows load only past the hooks. A
        # passthrough tail is checked first, before any Runtime: launches and
        # direct take one, and never a launcher-owned flag.
        if passthrough:
            if args.command not in (None, "direct"):
                _refuse_passthrough()
            _check_passthrough(passthrough)
        import claude_multi.cli.commands.sessions as commands_sessions
        import claude_multi.cli.dispatch as dispatch
        import claude_multi.cli.launch_flow as launch_flow
        import claude_multi.cli.runtime as runtime_mod
        import claude_multi.cli.screens.common as screens_common
        import claude_multi.cli.screens.launch_sessions as launch_sessions
        import claude_multi.cli.selection as selection
        from claude_multi import tui

        # Whether a person is at a terminal is decided before anything is
        # recorded or built: a command that needs one is refused first.
        if (
            getattr(args, "composition_file", None) == "-"
            or getattr(args, "profile_file", None) == "-"
        ) and interactive is not False:
            # stdin carries the document, so it cannot also feed key input:
            # '-' forces noninteractive (the --help promise), unconditionally.
            interactive = False
        if interactive is None:
            if input_stream is not None:
                interactive = True
            else:
                try:
                    tty_in, tty_out = streams._open_tty_streams()
                    if tty_in is not sys.stdin:
                        owned_tty = (tty_in, tty_out)
                    interactive = True
                except cli_errors.CLIError:
                    interactive = False
        if args.command == "direct":
            # Bare direct needs a terminal for its picker (and --print-launch a
            # model): refused before the channel is recorded or a Runtime that
            # may write is built.
            launch_flow._direct_preflight(args, interactive=bool(interactive))
        if args.command == "gateway" and args.gateway_command == "start":
            # An already-inhibited start refuses before channel recording,
            # config cleanup or session-store initialization. The lifecycle
            # still rechecks under its start lock before dispatching.
            from claude_multi import gateway_inhibition

            environ = runtime.environ if runtime is not None else os.environ
            gateway_inhibition.guard(state_path, token=environ.get(gateway_inhibition.TOKEN_ENV) or None,
                                     what="nothing was started")
        # The single Runtime construction for every command. Hooks
        # (session-event) and a newer state marker never clean config files
        # (the legacy-override removal); every other command may. Read-only
        # commands write nothing: no shim refresh (refresh_shims=False takes
        # the paths-only branch) and no legacy-override removal. They are
        # `quota`, `usage`, `explain`, `plan` (a candidate binary's plan never
        # repoints running sessions' hooks), the lists and shows (root `show`
        # included), `update --check` (with or without --from-dir), the
        # `migrate` and `profile migrate` dry runs (an unactivated build never
        # repoints the legacy shim), stdout `export` and the `import` preview
        # (`export --out` records a receipt and `import --apply` writes), the
        # JSON, preview and first-run doctor reports (they check the helpers
        # as they are) and `--print-launch`.
        migrate_dry = args.command == "migrate" and bool(getattr(args, "dry_run", False))
        profile_dry = parser_mod._profile_migrate_read_only(args)
        quota_read = args.command in {"quota", "usage", "explain", "plan"} or parser_mod._read_only_listing(args) or (
            args.command == "doctor" and (bool(getattr(args, "json", False)) or bool(getattr(args, "preview", False))
                                          or bool(getattr(args, "doctor_first_run", False)))) or (
            args.command == "export" and getattr(args, "export_out", None) is None) or (
            args.command == "import" and not getattr(args, "import_apply", False))
        read_only = quota_read or migrate_dry or profile_dry or bool(getattr(args, "print_launch", False))
        # One state root serves one release channel (installs.check): another
        # channel's launcher refuses, except a plain doctor run, which then
        # writes nothing and reports both installations.
        from claude_multi import installs

        channel_check = installs.check(
            state_path, runtime.environ if runtime is not None else os.environ, record=not read_only)
        if not channel_check.ok:
            if args.command != "doctor" or parser_mod._writes_versioned_state(args):
                raise cli_errors.CLIError(str(channel_check.problem), remedy=channel_check.remedy)
            read_only = quota_read = True
        plain_doctor = args.command == "doctor" and not parser_mod._writes_versioned_state(args)
        if runtime is None:
            try:
                runtime = runtime_mod.Runtime(
                    asset_root=assets.default_asset_root(),
                    allow_state_writes=(
                        args.command != "session-event"
                        and not marker_newer
                        and not read_only
                    ),
                    refresh_shims=not read_only,
                    initialize_session_store=not (read_only or plain_doctor),
                )
            except (errors.ClaudeMultiError, OSError) as exc:
                if not plain_doctor or not isinstance(exc, OSError):
                    raise
                # A doctor report never needs the preparation that failed:
                # it reports the failure from a read-only runtime instead of
                # retrying it.
                runtime = runtime_mod.Runtime(
                    asset_root=assets.default_asset_root(), allow_state_writes=False,
                    refresh_shims=False, initialize_session_store=False,
                )
                runtime.init_problem = _failure_text(exc, runtime.environ)
        if args.command != "session-event":
            runtime.show_contract_notice(sys.stderr)
        if args.command is None:
            selection._normalize_profile_args(runtime, args)
        inp = input_stream or tty_in or sys.stdin
        tty_output = output_stream or tty_out or output

        if args.command is not None:
            return dispatch.handle_command(
                runtime,
                args,
                input_stream=inp,
                output_stream=streams._report_output_stream(args, tty_output, output_stream or output),
                interactive=bool(interactive),
                no_color=args.no_color,
                passthrough=passthrough,
                terminal_stream=tty_output,
            )

        report = output_stream or output
        force = bool(args.force)
        if args.resume == "":
            # Bare -r: the sessions screen on a curses-capable terminal;
            # --line, a dumb or pipe terminal and --print-launch keep the line
            # chooser; the text listing when not interactive.
            if interactive:
                if not args.print_launch and screens_common._curses_ok(bool(args.line), inp, tty_output):
                    try:
                        result = launch_sessions._sessions_list_tui(
                            runtime,
                            input_stream=inp,
                            output_stream=tty_output,
                            no_color=args.no_color,
                            passthrough=passthrough,
                            resume_decision="force" if force else None,
                        )
                    except (tui.CursesError, OSError):
                        result = screens_common._LINE_FALLBACK  # curses could not start
                    if result is not screens_common._LINE_FALLBACK:
                        if result is None:
                            return 0
                        return launch_flow._perform_card_result(runtime, result, force=force)
                chosen = launch_flow._choose_session_line(runtime, inp, tty_output)
                if chosen is None:
                    return 0
                return launch_flow._resume_flow(
                    runtime,
                    chosen,
                    passthrough=passthrough,
                    interactive=True,
                    confirm=True,
                    input_stream=inp,
                    output_stream=tty_output,
                    report_stream=report,
                    force=force,
                    print_launch=bool(args.print_launch),
                    line=bool(args.line),
                    no_color=args.no_color,
                )
            commands_sessions._print_sessions_listing(runtime, report)
            return 0

        if args.resume or args.continue_last:
            if args.resume:
                session_id = selection._resolve_resume_target(runtime, args.resume)
            else:
                session_id = runtime.session_store.last(runtime.cwd)
                if session_id is None:
                    raise cli_errors.CLIError(
                        "no managed session is recorded for this directory",
                        remedy="claude-multi -r SESSION (no value opens the picker), or start fresh: claude-multi",
                    )
            record = runtime.session_store.resolve(session_id)
            selection._refuse_resume_override(args.profile, record, profile_file=args.profile_file)
            return launch_flow._resume_flow(
                runtime,
                session_id,
                passthrough=passthrough,
                interactive=bool(interactive),
                confirm=True,
                input_stream=inp,
                output_stream=tty_output,
                report_stream=report,
                force=force,
                print_launch=bool(args.print_launch),
                line=bool(args.line),
                no_color=args.no_color,
            )

        # Only a FRESH launch can need an explicit profile: resume and
        # continue take the recorded lineup, so they never demand one.
        if (
            not interactive
            and not args.print_launch
            and args.profile is None
            and args.profile_file is None
        ):
            import claude_multi.cli.types as cli_types
            from claude_multi.setup import texts as setup_texts

            # Nothing connected yet: the fix is setting up, not a profile name.
            fix = setup_texts.LINE_SETUP_FIX if launch_flow._nothing_connected(runtime) else None
            raise cli_types.UsageError(cli_text.NONINTERACTIVE_PROFILE_REQUIRED, remedy=fix)
        target = selection._fresh_target(runtime, args, inp, read_only=bool(args.print_launch))
        if interactive and not args.print_launch and screens_common._curses_ok(bool(args.line), inp, tty_output):
            # The card prepares inside _plan(), so a BLOCKED lineup shows the
            # BLOCKED card; the result is performed after curses teardown.
            try:
                result = launch_sessions.launch_card(
                    runtime,
                    target,
                    action="fresh",
                    passthrough=passthrough,
                    input_stream=inp,
                    output_stream=tty_output,
                    no_color=args.no_color,
                    # A profile chosen by default may open Get started first.
                    explicit=args.profile is not None or args.profile_file is not None,
                )
            except (tui.CursesError, OSError):
                pass  # curses could not start: the line branch below
            else:
                if result is None:
                    tty_output.write("Nothing was launched.\n")
                    tty_output.flush()
                    return 0
                return launch_flow._perform_card_result(runtime, result, force=force)
        # The line branch prepares first (a BLOCKED lineup exits 1 before any
        # prompt), then asks the line confirm. With nothing connected yet,
        # setting up is offered before both.
        if interactive and not args.print_launch and launch_flow._offer_setup_line(runtime, inp, tty_output):
            if args.profile is None and args.profile_file is None:
                runtime.selection_notices = []
                target = selection._fresh_target(runtime, args, inp, read_only=False)
        if args.print_launch:
            return launch_flow._print_launch(runtime, target, report, passthrough=passthrough)
        prepared = runtime.prepare(target, action="fresh", passthrough=passthrough)
        if not interactive:
            return runtime.perform(prepared, resume_decision="force" if force else None)
        if not launch_flow._confirm_launch_line(prepared, inp, tty_output, runtime=runtime):
            return 0
        return runtime.perform(prepared, resume_decision="force" if force else None)
    except KeyboardInterrupt:
        if args.command == "session-event":
            raise
        return _interrupted(args)
    except sessions.StateMarkerError as exc:
        if _json_requested(args):
            return _json_error(args, exc, output)
        if args.command == "session-event":
            sys.stderr.write(f"claude-multi: {exc}\n")
            if getattr(args, "event", None) == "premodel":
                # A model switch over state this release cannot read is
                # refused (fail closed), never silently allowed.
                message = termtext.visible_message(exc)
                if exc.remedy:
                    message += " — " + termtext.visible_text(exc.remedy)
                output.write(hooks.premodel_fail_closed_response(f"claude-multi premodel failed: {message}"))
                output.flush()
            return 0
        return _report_error(exc)
    except errors.ClaudeMultiError as exc:
        if _json_requested(args):
            return _json_error(args, exc, output)
        if args.command == "session-event":
            # Claude shows a SessionStart/End hook's stderr, never its stdout,
            # and exit 2 there is no blocking signal: stderr, exit 1, nothing
            # on stdout.
            sys.stderr.write(f"claude-multi: {_error_text(exc)}\n")
            return 1
        return _report_error(exc)
    except OSError as exc:
        if args.command == "session-event" or _filesystem(exc) is None:
            raise
        if _json_requested(args):
            return _json_error(args, cli_errors.CLIError(_failure_text(exc, os.environ)), output)
        sys.stderr.write(f"claude-multi: {_failure_text(exc, os.environ)}\n")
        sys.stderr.flush()
        return 1
    finally:
        if owned_tty is not None:
            for handle in owned_tty:
                handle.close()


# Every JSON mode prints exactly one document, a failure before (or
# during) the report included: the reports (``--json``) their error form,
# the lists (``... list --json``) their own failure document.
_JSON_REPORTS = frozenset({"doctor", "quota", "explain", "usage", "plan"})
# (command, the sub-command attribute, its value) -> the list's name.
_JSON_LISTS = {
    ("sessions", "sessions_command", "list"): "sessions",
    ("profile", "profile_command", "list"): "profiles",
    ("models", "models_command", "list"): "models",
    ("providers", "providers_command", "list"): "providers",
}
# The machine documents of the earlier-format tools (``--json``).
_JSON_TOOL_REPORTS = frozenset({"migrate", "restore-2x"})
LIST_UNAVAILABLE = "The list could not be collected; run without --json for the diagnostic and remedy."


def _json_list(args: Any) -> str | None:
    """The list a ``... list --json`` names, or None."""

    if not getattr(args, "json_output", False):
        return None
    command = getattr(args, "command", None)
    for (name, attr, value), listed in _JSON_LISTS.items():
        if command == name and getattr(args, attr, None) == value:
            return listed
    return None


def _json_requested(args: Any) -> bool:
    command = getattr(args, "command", None)
    if bool(getattr(args, "json", False)) and command in _JSON_REPORTS:
        return True
    return _json_list(args) is not None or (
        bool(getattr(args, "json_output", False)) and command in _JSON_TOOL_REPORTS)


def list_error_document(name: str) -> dict[str, Any]:
    """The failure document of a JSON list: the list's own schema version
    and name, ``status`` unavailable and a fixed text (no exception body)."""

    return {"schema_version": _list_schema(name), "list": name, "status": "unavailable",
            "error": LIST_UNAVAILABLE}


def _list_schema(name: str) -> int:
    if name == "sessions":
        from claude_multi.cli.commands.sessions import SESSIONS_LIST_SCHEMA as schema
    elif name == "profiles":
        from claude_multi.cli.commands.profile import PROFILE_LIST_SCHEMA as schema
    elif name == "models":
        from claude_multi.cli.commands.models import MODELS_LIST_SCHEMA as schema
    else:
        from claude_multi.cli.commands.providers import PROVIDERS_LIST_SCHEMA as schema
    return int(schema)


def _error_text(exc: errors.ClaudeMultiError) -> str:
    """The error's text and its one fix, safe for a terminal. A filesystem
    failure without a remedy of its own gets the standard one."""

    message = termtext.visible_message(exc)
    remedy = exc.remedy
    if not remedy:
        found = _filesystem(exc)
        remedy = found[1] if found is not None else None
    if remedy:
        message += "\n  fix: " + termtext.visible_text(remedy)
    return message


def _report_error(exc: errors.ClaudeMultiError) -> int:
    """An ordinary command's failure or refusal on stderr: exit 1, or the
    error type's own status (2 for an invalid command line)."""

    sys.stderr.write(f"claude-multi: {_error_text(exc)}\n")
    sys.stderr.flush()
    return getattr(type(exc), "exit_status", 1)


def _refuse_passthrough() -> None:
    import claude_multi.cli.types as cli_types

    raise cli_types.UsageError("passthrough arguments (after --) are accepted only for a launch or direct",
                               remedy="drop the arguments after --")


def _check_passthrough(passthrough: list[str]) -> None:
    """A launcher-owned flag after ``--`` is an invalid command line."""

    import claude_multi.cli.types as cli_types
    from claude_multi import compiler

    try:
        compiler.validate_passthrough(list(passthrough))
    except compiler.CompilerError as exc:
        raise cli_types.UsageError(str(exc)) from exc


def _filesystem(exc: BaseException) -> tuple[str, str] | None:
    from claude_multi import state

    return state.filesystem_failure(exc)


def _failure_text(exc: BaseException, environ: Any) -> str:
    """A filesystem failure in one line: what happened, where (home-relative)
    and the one fix; never a traceback."""

    from claude_multi import state

    text = state.failure_text(exc, environ, own_message=False)
    if text is not None:
        return text
    return f"the operation failed{state.failure_location(exc, environ)}\n  fix: run claude-multi doctor"


def _interrupted(args: Any) -> int:
    """Ctrl-C: the terminal is restored by the screen that owned it; say
    what happened and exit 130."""

    launching = args is not None and getattr(args, "command", None) in (None, "direct")
    sys.stderr.write("\nclaude-multi: cancelled" + (" — nothing was launched" if launching else "") + "\n")
    sys.stderr.flush()
    return 130


def _json_error(args: Any, exc: errors.ClaudeMultiError, output: TextIO) -> int:
    """A JSON command that failed before it could report: the error on
    stderr and the command's own JSON shape on stdout (exit 1) — a list's
    failure document (:func:`list_error_document`), the first-run checks'
    shape for ``doctor --first-run --json``, else the report's error form.
    stdout carries exactly one document."""

    message = _error_text(exc)
    sys.stderr.write(f"claude-multi: {message}\n")
    listed = _json_list(args)
    if listed is not None:
        from claude_multi import strict_json

        output.write(strict_json.pretty_file_bytes(list_error_document(listed)).decode("utf-8"))
    elif args.command == "doctor" and getattr(args, "doctor_first_run", False):
        from claude_multi import strict_json
        from claude_multi.setup import firstrun

        output.write(strict_json.pretty_file_bytes(firstrun.error_json(message)).decode("utf-8"))
    else:
        from claude_multi.observations import error_report

        output.write(error_report(args.command).json())
    output.flush()
    return getattr(type(exc), "exit_status", 1)
