"""Command dispatch: one delegation per command."""

from __future__ import annotations

from claude_multi import errors as cli_errors
from typing import TextIO
import argparse
import claude_multi.cli.commands.ceiling as commands_ceiling
import claude_multi.cli.commands.custom as commands_custom
import claude_multi.cli.commands.discover as commands_discover
import claude_multi.cli.commands.doctor as commands_doctor
import claude_multi.cli.commands.migration as commands_migration
import claude_multi.cli.commands.models as commands_models
import claude_multi.cli.commands.profile as commands_profile
import claude_multi.cli.commands.providers as commands_providers
import claude_multi.cli.commands.quota as commands_quota
import claude_multi.cli.commands.explain as commands_explain
import claude_multi.cli.commands.gateway as commands_gateway
import claude_multi.cli.commands.usage as commands_usage
import claude_multi.cli.commands.plan as commands_plan
import claude_multi.cli.commands.portability as commands_portability
import claude_multi.cli.commands.sessions as commands_sessions
import claude_multi.cli.commands.update as commands_update
import claude_multi.cli.commands.setup as commands_setup
import claude_multi.cli.commands.uninstall as commands_uninstall
import claude_multi.cli.launch_flow as launch_flow
import claude_multi.cli.session_events as session_events
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def handle_command(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
    no_color: bool = False,
    passthrough: list[str] | None = None,
    terminal_stream: TextIO | None = None,
) -> int:
    if args.command == "quota":
        return commands_quota._cmd_quota(runtime, output_stream, json_output=bool(getattr(args, "json", False)))
    if args.command == "explain":
        return commands_explain.command(runtime, args, output_stream)
    if args.command == "usage":
        return commands_usage.command(runtime, args, output_stream)
    if args.command == "plan":
        return commands_plan.command(runtime, args, output_stream)
    if args.command == "window-ceiling":
        return commands_ceiling.command(runtime, args, output_stream)
    if args.command in ("export", "import"):
        return commands_portability.command(runtime, args, input_stream=input_stream, output_stream=output_stream)

    if args.command == "restore-2x":
        return commands_migration._restore_2x(
            runtime.session_store.root,
            output_stream,
            args,
            input_stream=input_stream,
            interactive=interactive,
            environ=runtime.environ,
        )

    if args.command == "migrate":
        return commands_migration._migrate_command(runtime, args, output_stream)

    if args.command == "session-event":
        return session_events._handle_session_event(
            runtime,
            args,
            input_stream=input_stream,
            output_stream=output_stream,
        )

    if args.command == "direct":
        return launch_flow._direct_command(
            runtime,
            args,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
            passthrough=list(passthrough or []),
        )

    if args.command == "custom":
        code = commands_custom._custom_command(
            runtime,
            args,
            output_stream=output_stream,
        )
        if code is not None:
            return code

    if args.command == "profile":
        return commands_profile._profile_command(
            runtime,
            args,
            args.profile_command,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
        )

    if args.command == "compose":
        return commands_profile._compose_command(
            runtime,
            args,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
        )

    if args.command == "sessions":
        code = commands_sessions._sessions_command(
            runtime,
            args,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
            no_color=no_color,
        )
        if code is not None:
            return code

    if args.command == "update":
        return commands_update._update_command(
            runtime,
            args,
            output_stream=output_stream,
            input_stream=input_stream,
        )

    if args.command == "setup":
        return commands_setup._setup_command(
            runtime, args, input_stream=input_stream, output_stream=output_stream, interactive=interactive,
        )

    if args.command == "uninstall":
        return commands_uninstall.command(
            runtime, args, input_stream=input_stream, output_stream=output_stream, interactive=interactive,
        )

    if args.command == "discover":
        return commands_discover._discover_command(
            runtime,
            args,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
        )

    if args.command == "models":
        return commands_models._models_command(
            runtime,
            args,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
        )

    if args.command == "providers":
        return commands_providers._providers_command(
            runtime,
            args,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
        )

    if args.command == "show":
        return commands_profile._show_command(
            runtime,
            args,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
        )

    if args.command == "gateway":
        return commands_gateway.command(runtime, args, input_stream=input_stream, output_stream=output_stream)

    if args.command == "doctor":
        return commands_doctor._doctor_command(
            runtime,
            args,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
            no_color=no_color,
            terminal_stream=terminal_stream,
        )

    raise cli_errors.CLIError(f"unsupported command {args.command!r}")
