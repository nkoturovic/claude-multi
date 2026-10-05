"""The ``lineup`` command and ``/cm``."""

from __future__ import annotations

from claude_multi import assets
from claude_multi import catalog
from claude_multi import compiler
from claude_multi import custom
from claude_multi import errors as cli_errors
from claude_multi import installs
from claude_multi import launch
from claude_multi import lineup as lineup_mod
from claude_multi import lineup_files
from claude_multi import migrate as migrate_mod
from claude_multi import profile as profile_mod
from claude_multi import scope as scope_mod
from claude_multi import sessions
from claude_multi import settings as settings_mod
from claude_multi import state
from claude_multi import termtext
from typing import Any
from typing import TextIO
import os
import sys
import claude_multi.cli.launch_flow as launch_flow
import claude_multi.cli.runtime as runtime_mod
import claude_multi.cli.streams as streams
import claude_multi.cli.types as cli_types


_LINEUP_ERRORS = (
    lineup_mod.LineupRefusal,
    cli_errors.CLIError,
    sessions.SessionError,
    state.StateError,
    catalog.CatalogError,
    compiler.CompilerError,
    launch.LaunchError,
    custom.CustomModelsError,
    migrate_mod.MigrationError,
    profile_mod.ProfileError,
    profile_mod.BindingError,
    settings_mod.SettingsError,
    scope_mod.ScopeError,
    OSError,
)


# The options of ``claude-multi lineup`` (the request follows them).
LINEUP_OPTIONS = (
    ("--session ID", "the session's runtime id (/cm passes it; claude-multi sessions show SESSION names it)"),
    ("--relaunch", "relaunch the session with the change instead of a live apply"),
    ("--its-exited", "the session has exited: relaunch it without asking"),
    ("--preview", "with fallback: show the plan and change nothing"),
    ("-h, --help", "show this help and exit"),
)


def lineup_help() -> str:
    """``claude-multi lineup --help``: the request grammar from the one /cm
    verb table, written before any Runtime, session or state is read."""

    width = max(len(verb.spelled()) for verb in lineup_files.CM_VERBS) + 2
    lines = [
        "usage: claude-multi lineup [--session ID] [--relaunch] [--its-exited] [REQUEST ...]",
        "",
        "Show or change a managed session's lineup: what /cm runs inside the session. Inside the",
        "session the session id comes from its environment; from another terminal pass --session.",
        "",
        "requests:",
        *(f"  {verb.spelled():<{width}}{verb.summary}" for verb in lineup_files.CM_VERBS),
        "",
        "options:",
        *(f"  {option:<{width}}{text}" for option, text in LINEUP_OPTIONS),
        "",
        "Exit status: 0 done; 1 refused or unavailable; 2 an invalid request. Through /cm every answer",
        "is printed with status 0.",
    ]
    return "\n".join(lines) + "\n"


def _help_requested(tail: list[str]) -> bool:
    """``-h``/``--help`` among the leading options of a terminal invocation
    (never in skill mode, never after ``--`` or inside the request)."""

    if lineup_mod.skill_mode_of(tail):
        return False
    for token in tail:
        if token in ("-h", "--help"):
            return True
        if token == "--" or not token.startswith("-") or token == "-":
            return False
    return False


def _lineup_main(
    tail: list[str],
    *,
    runtime: runtime_mod.Runtime | None,
    input_stream: TextIO | None,
    output_stream: TextIO | None,
    interactive: bool | None,
) -> int:
    """``claude-multi lineup`` (the raw-argv intercept of ``main``).

    Skill mode (``--session`` present): never opens ``/dev/tty``, every
    line (refusals and internal errors included) goes to stdout as
    ``claude-multi: …`` and the exit status is 0 (the /cm transport). Outside
    skill mode success goes to stdout (exit 0) and a refusal to stderr: exit
    2 for a request or option the parser rejects, 1 for a refused or failed
    request. ``--help`` prints the grammar before anything else. An injected
    ``runtime`` is honoured (tests, probes); otherwise the Runtime is built
    with ``main``'s formula (``show``, ``profiles``, ``review``, ``quota``
    and a ``fallback --preview`` read; every other request writes).
    """

    out = output_stream or sys.stdout
    skill = lineup_mod.skill_mode_of(tail)
    if _help_requested(tail):
        out.write(lineup_help())
        out.flush()
        return 0

    def refuse(message: str, code: int = 1) -> int:
        text = "\n".join(
            termtext.visible_text(line) for line in str(message).splitlines()
        ) or termtext.visible_text(str(message))
        if skill:
            out.write(f"claude-multi: {text}\n")
            out.flush()
            return 0
        sys.stderr.write(f"claude-multi: {text}\n")
        sys.stderr.flush()
        return code

    environ = runtime.environ if runtime is not None else dict(os.environ)
    try:
        # A request or option the grammar rejects is a usage error.
        args = lineup_mod.parse_cli(tail, environ)
    except lineup_mod.LineupRefusal as exc:
        return refuse(str(exc), 2)
    try:
        # One state root serves one release channel: the same guard as every
        # other command, before a Runtime refreshes shims or anything writes.
        state_path = runtime.session_store.root if runtime is not None else sessions.state_root()
        try:
            channel_check = installs.guard(state_path, environ, writes=args.writes)
        except installs.ChannelError as exc:
            raise cli_errors.CLIError(f"{exc}\n  fix: {exc.remedy}") from exc
        foreign_owner = not channel_check.ok  # a read-only request; it writes nothing
        if runtime is None:
            marker_newer = False
            try:
                sessions.check_state_marker(state_path)
            except sessions.StateMarkerError:
                if args.writes:
                    raise
                marker_newer = True
            # A request that writes nothing (show, profiles, review, quota, a
            # fallback --preview) builds the read-only Runtime: no shim
            # refresh, no legacy-file removal, no store directories.
            writes = bool(args.writes) and not (marker_newer or foreign_owner)
            runtime = runtime_mod.Runtime(
                asset_root=assets.default_asset_root(),
                allow_state_writes=writes,
                refresh_shims=writes,
                initialize_session_store=bool(args.writes),
            )
        elif args.writes:
            sessions.check_state_marker(runtime.session_store.root)
        if skill:
            is_interactive = False
        elif interactive is not None:
            is_interactive = bool(interactive)
        else:
            is_interactive = input_stream is not None or streams._stdio_streams_are_ttys()
        inp = input_stream or sys.stdin
        bound = runtime

        def relaunch_exec(mid: str, target: Any, sample: tuple[Any, Any, Any, Any]) -> int:
            m8 = mid[:8]
            # §7.7: a `profile N` target is loaded by prepare (doc None).
            document = None if target.kind == "profile" else target.document
            launch_target = cli_types.LaunchTarget(
                "relaunch", document, target.profile, target.follow, f"Relaunch {m8}"
            )
            return launch_flow._relaunch_exec(
                bound,
                mid,
                launch_target,
                its_exited=args.its_exited,
                interactive=is_interactive,
                sample=sample,
                input_stream=inp,
                output_stream=out,
            )

        def quota_text() -> tuple[int, str]:
            from claude_multi.cli.commands import quota as quota_cmd

            return quota_cmd.session_quota_text(bound)

        def provider_errors(record: Any) -> list[str]:
            # Doctor's own bounded read of the gateway log (the last 24 h,
            # one report snapshot); a read that fails is unavailable, never zero.
            from datetime import timedelta

            import claude_multi.cli.gateway_facts as gateway_facts

            end = gateway_facts._doctor_now()
            start = end - timedelta(hours=lineup_mod.PROVIDER_ERRORS_HOURS)
            try:
                with bound.report_snapshot():
                    events = gateway_facts.report_events(bound, since=f"-{lineup_mod.PROVIDER_ERRORS_HOURS}h")
            except (cli_errors.ClaudeMultiError, OSError, ValueError):
                events = None
            return lineup_mod.provider_error_lines(bound, record, events, start=start, end=end)

        result = lineup_mod.apply(
            runtime, args, interactive=is_interactive, relaunch_exec=relaunch_exec,
            quota_text=quota_text, provider_errors=provider_errors,
        )
    except _LINEUP_ERRORS as exc:
        if isinstance(exc, lineup_mod.LineupRefusal):
            return refuse(str(exc))
        # No space, a quota, no permission or a read-only file system: the
        # text and fix every other command shows (/cm keeps its status 0).
        return refuse(state.failure_text(exc, environ) or termtext.visible_message(exc))
    except Exception as exc:  # noqa: BLE001 - skill mode never shows a traceback
        if not skill:
            raise
        return refuse(f"lineup failed: {type(exc).__name__}: {termtext.visible_message(exc)}")
    if result.text:
        for line in result.text.splitlines():
            out.write(termtext.visible_text(line) + "\n")
        out.flush()
    return result.exit_code
