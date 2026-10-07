"""The launcher's argument parser and its help."""

from __future__ import annotations

from pathlib import Path
import argparse
from claude_multi import layout
import claude_multi.cli.text as cli_text
from claude_multi import lineup_files


def split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split launcher arguments from the exact post-``--`` Claude tail."""

    try:
        separator = argv.index("--")
    except ValueError:
        return list(argv), []
    return list(argv[:separator]), list(argv[separator + 1 :])


class _VersionAction(argparse.Action):
    """``--version``: the release identity line the launchers print
    (version, catalog, gateway and Claude Code pins), read from the
    packaged resources only when asked."""

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: object,
                 option_string: str | None = None) -> None:
        import sys

        from claude_multi import identity

        sys.stdout.write(identity.version_line() + "\n")
        sys.stdout.flush()
        parser.exit()


def _doc_pointer(name: str) -> str:
    """The installed doc (the package's copy, or the checkout's ``docs/``),
    else its name by role. Documents belong to the installation: a resource
    override never moves this pointer."""

    path = layout.document(name)
    return str(path) if path is not None else f"{name} in the claude-multi docs"


# The root help's command groups, in order: (title, ((command, summary), ...)).
# Each summary is also the command's argparse ``help``. Commands absent from
# every group stay accepted but are not shown: the earlier spellings
# (``compose``, ``show``) and the hook command (``session-event``).
COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Launch", (
        ("direct", "launch one model as the lead, with no cm-* agents (recorded and resumable "
                   "like every session)"),
    )),
    ("Inspect (read only)", (
        ("doctor", "check this installation and its sessions: Ready, Attention or BLOCKED, one fix "
                   "for each finding (also --repair, --prune and token rotation)"),
        ("quota", "account quota and credential health from the local gateway (one local read; no "
                  "provider call)"),
        ("explain", "explain a session's recorded bindings and observed routing"),
        ("usage", "observed client requests per model and session (not token usage)"),
        ("plan", "preview what the gateway would serve after pending changes; writes nothing"),
    )),
    ("Manage", (
        ("setup", "set up step by step: this computer, Claude Code, the local gateway, providers and "
                  "a profile (--status shows where you are)"),
        ("profile", "list, show, create, edit, copy, rename, remove and restore profiles; choose the "
                    "default profile"),
        ("sessions", "list, show, stop, forget, link and repair managed sessions"),
        ("providers", "list, add, connect (API key or sign-in), test, turn on or off, apply and remove "
                      "providers"),
        ("models", "list models; declare, admit, qualify, revoke and remove the models you add"),
        ("discover", "list the models a provider offers (a provider call, made only when you ask)"),
        ("lineup", "show or change a session's lineup from a terminal (what /cm runs in a session)"),
        ("window-ceiling", "show, set or reset the context window ceiling of the lead and every agent "
                           "(200K to 800K, default 800K; applies at the next launch or resume)"),
        ("gateway", "the local gateway: status, logs, start, stop, restart and the supervised service"),
        ("update", "update claude-multi itself, or roll back to the previously installed version"),
        ("export", "write your configuration as a portable file (no keys, sessions, logs or evidence)"),
        ("import", "preview an exported configuration on this computer; --apply writes the ready items"),
    )),
    ("Recovery and earlier formats", (
        ("migrate", "convert legacy session records to the current format (backups are kept for "
                    "restore-2x); --dry-run writes nothing"),
        ("restore-2x", "put back the legacy session records a migration backed up, before running an "
                       "earlier launcher"),
        ("custom", "the earlier custom-model registry (move it to providers: claude-multi providers "
                   "migrate-custom)"),
        ("uninstall", "remove claude-multi from this computer (shows the plan first; credentials only "
                      "after a typed confirmation)"),
    )),
)
_SUMMARIES = {name: summary for _title, rows in COMMAND_GROUPS for name, summary in rows}
# The TUI's main screens, by key (root help and the card's help name them alike).
TUI_SCREENS = (
    ("W", "Get started"), ("P", "Profiles"), ("D", "Direct"), ("S", "Sessions"), ("G", "Providers"),
    ("M", "Models"), ("O", "Settings"), ("H", "doctor"),
)
HELP_WIDTH = 100
SESSION_ARGUMENT = "its id, an id prefix of 8 or more characters, or its name"


def _wrap(text: str, width: int, indent: str) -> list[str]:
    """Greedy word wrap that never breaks a word (commands and paths stay whole)."""

    lines: list[str] = []
    current = ""
    for word in text.split():
        if current and len(indent) + len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    lines.append(current)
    return lines


def _help_description() -> str:
    screens = ", ".join(f"{key} {name}" for key, name in TUI_SCREENS)
    lines = [
        "Launch Claude Code with a durable multi-model lineup: a saved profile, an unsaved profile file, "
        "or a direct lead.",
        "",
        *_wrap(f"In a terminal, claude-multi with no command opens the launcher: the launch card (Enter "
               f"launches the profile shown) and its screens {screens}. ? lists the keys; --line uses plain "
               "lines instead.", HELP_WIDTH, ""),
    ]
    column = max(len(name) for _title, rows in COMMAND_GROUPS for name, _summary in rows)
    for title, rows in COMMAND_GROUPS:
        lines += ["", f"{title}:"]
        for name, summary in rows:
            head = f"  {name:<{column}}  "
            pieces = _wrap(summary, HELP_WIDTH, " " * len(head))
            lines.append(head + pieces[0])
            lines += [" " * len(head) + piece for piece in pieces[1:]]
    return "\n".join(lines)


HELP_EXAMPLES = (
    ("claude-multi", "open the launcher"),
    ("claude-multi --profile NAME", "launch a saved profile"),
    ("claude-multi -c", "continue the last session in this directory"),
    ("claude-multi -r SESSION", "resume a session (its id, an 8+ character prefix, or its name)"),
    ("claude-multi direct --model MODEL", "launch one model as the lead"),
    ("claude-multi doctor", "check health, with one fix for each finding"),
    ("claude-multi --profile NAME -- ARGS", "pass ARGS to Claude Code unchanged"),
)


def _help_epilog() -> str:
    return "\n".join([
        "Examples:",
        *(f"  {command:<36}  {what}" for command, what in HELP_EXAMPLES),
        "",
        "Arguments after -- go to Claude Code unchanged (launches and direct only).",
        "--line and --no-color also work after a command.",
        "Inside a session: /cm shows or changes the lineup (then /reload-plugins).",
        "The local gateway's own tool: claude-multi-proxy --help",
        f"Symptom → command: {_doc_pointer(cli_text.CHEATSHEET_NAME)}",
        f"Documentation map: {_doc_pointer(cli_text.USAGE_NAME)}",
    ])


ROOT_USAGE = (
    "claude-multi [--profile NAME | --profile-file PATH|-] [-c | -r [SESSION]] [options] "
    "[-- CLAUDE_ARGS ...]\n       claude-multi COMMAND [options]"
)


def _add_command(commands: "argparse._SubParsersAction", name: str, **kwargs: object) -> argparse.ArgumentParser:
    """A command shown in the root help under its group, with its group summary."""

    return commands.add_parser(name, help=_SUMMARIES[name], **kwargs)  # type: ignore[arg-type]


def _hide_command(commands: "argparse._SubParsersAction", name: str) -> None:
    """Keep a command parsed but out of the command list (an earlier spelling)."""

    commands._choices_actions[:] = [action for action in commands._choices_actions if action.dest != name]


def _add_presentation_flags(parser: argparse.ArgumentParser) -> None:
    """``--line`` and ``--no-color`` after a command too (``claude-multi
    sessions list --line``), on every command parser below the root. They set
    the root's options and say nothing in each command's help."""

    seen: set[int] = set()

    def visit(node: argparse.ArgumentParser) -> None:
        for action in node._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, child in action.choices.items():
                if id(child) in seen or name == "session-event":
                    continue
                seen.add(id(child))
                options = {option for item in child._actions for option in item.option_strings}
                if "--line" not in options:
                    child.add_argument("--line", action="store_true", default=argparse.SUPPRESS,
                                       help=argparse.SUPPRESS)
                if "--no-color" not in options:
                    child.add_argument("--no-color", dest="no_color", action="store_true",
                                       default=argparse.SUPPRESS, help=argparse.SUPPRESS)
                visit(child)

    visit(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-multi",
        usage=ROOT_USAGE,
        description=_help_description(),
        epilog=_help_epilog(),
        # The description and epilog are laid out here: commands and the
        # installed document paths must not be re-wrapped.
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    composition_group = parser.add_mutually_exclusive_group()
    composition_group.add_argument("--profile", metavar="NAME", help="launch a saved profile (the session follows it)")
    composition_group.add_argument("--profile-file", metavar="PATH|-", help="launch one unsaved profile JSON (pinned, nameless); '-' reads stdin and forces noninteractive; resume/continue refuse it; nothing is saved")
    # The earlier spellings of --profile and --profile-file: accepted, not shown.
    composition_group.add_argument("--composition", metavar="NAME", help=argparse.SUPPRESS)
    composition_group.add_argument("--composition-file", metavar="PATH|-", help=argparse.SUPPRESS)
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument("-c", "--continue", dest="continue_last", action="store_true", help="continue the last managed session in this directory")
    session_group.add_argument("-r", "--resume", nargs="?", const="", metavar="SESSION", help="resume a managed session: its id, an id prefix of 8 or more characters, or its name; no value opens the sessions picker")
    parser.add_argument("--force", action="store_true", help="resume despite a background-liveness marker (only bypasses the heuristic ● check; identity/transcript guards still apply)")
    parser.add_argument("--line", action="store_true", help="force the line-based UI (no full-screen curses interface)")
    parser.add_argument("--no-color", action="store_true", help="disable all color output (the NO_COLOR environment variable is also honored)")
    parser.add_argument("--legacy", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--print-launch", action="store_true", help="print the exact Claude argv and env summary instead of launching (the gateway token is never shown); says whether the launch would succeed")
    parser.add_argument("--version", action=_VersionAction, nargs=0, default=argparse.SUPPRESS,
                        help="show the release identity (launcher, catalog, gateway, Claude Code) and exit")

    # The commands are listed by group in the description; the default
    # flat listing is hidden.
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", title="commands", help=argparse.SUPPRESS,
                                     prog="claude-multi")
    direct_parser = _add_command(commands, "direct")
    direct_parser.add_argument("--model", dest="direct_model", help="the model to run as the lead (claude-multi models list); without it a terminal opens the Direct picker")
    direct_parser.add_argument("--no-subagents", action="store_true", default=argparse.SUPPRESS, help="hard-deny subagent delegation for this session (recorded; resume re-applies it, a mismatching explicit flag is rejected)")
    direct_parser.add_argument("--force", action="store_true", default=argparse.SUPPRESS, help="resume despite a background-liveness marker (only bypasses the heuristic ● check)")
    direct_parser.add_argument("--print-launch", action="store_true", help="print the exact Claude argv and env summary instead of launching")
    direct_identity = direct_parser.add_mutually_exclusive_group()
    direct_identity.add_argument(
        "-c", "--continue", dest="direct_continue", action="store_true", help="continue the last managed session in this directory"
    )
    direct_identity.add_argument(
        "-r", "--resume", dest="direct_resume", nargs="?", const="", metavar="SESSION",
        help=f"resume a managed session: {SESSION_ARGUMENT}"
    )

    # `main` hands `lineup` to its own parser on the raw argv (a request may
    # start with `-`); this parser exists for the root help and the parser
    # surface only. `claude-multi lineup --help` describes the request grammar.
    lineup_parser = _add_command(commands, "lineup")
    lineup_parser.add_argument("--session", metavar="ID", help="runtime session id (/cm passes it)")
    lineup_parser.add_argument("--relaunch", action="store_true", help="relaunch instead of a live apply")
    lineup_parser.add_argument("--its-exited", action="store_true", help="the session has exited (no confirmation)")
    lineup_parser.add_argument("lineup_request", nargs=argparse.REMAINDER, metavar="REQUEST",
                               help="lineup request: " + ", ".join(lineup_files.CM_VERB_NAMES))

    profile_parser = _add_command(commands, "profile")
    profile_commands = profile_parser.add_subparsers(dest="profile_command", required=True, help="command to run")
    profile_list = profile_commands.add_parser("list", help="list profiles with origin and last use")
    profile_list.add_argument("--json", action="store_true", dest="json_output",
                              help="one JSON document (stable fields)")
    profile_show = profile_commands.add_parser("show", help="show a profile's evaluated lineup")
    profile_show.add_argument("name", nargs="?", help="profile name (default: balanced)")
    profile_new = profile_commands.add_parser("new", help="create a profile ($EDITOR, or --from SRC)")
    profile_new.add_argument("name", help="profile name")
    profile_new.add_argument("--from", dest="from_profile", metavar="SRC", help="copy SRC (non-interactive)")
    profile_new.add_argument("--keep-fallback", dest="keep_fallback", action="store_true",
                             help="with --from: the copy stays a fallback profile for SRC's provider")
    profile_commands.add_parser("edit", help="edit a profile in $VISUAL/$EDITOR").add_argument(
        "name", help="profile name")
    profile_rm = profile_commands.add_parser("rm", help="remove a profile of yours (a copy is kept)")
    profile_rm.add_argument("name", help="profile name")
    profile_rm.add_argument("--yes", action="store_true", help="skip the y/N confirmation")
    for action in ("rename", "duplicate"):
        item = profile_commands.add_parser(action, help=f"{action} a profile")
        item.add_argument("source", help="source profile name")
        item.add_argument("target", help="target profile name")
        if action == "duplicate":
            item.add_argument("--keep-fallback", dest="keep_fallback", action="store_true",
                              help="the copy stays a fallback profile for the source's provider")
    reseed = profile_commands.add_parser(
        "reseed", help="restore a shipped profile to its shipped version (your version is kept as a copy)")
    reseed.add_argument("name", help="profile name")
    reseed.add_argument("--yes", action="store_true", help="skip the y/N confirmation")
    default = profile_commands.add_parser(
        "default", help="show, set or clear the default profile (used where nothing else chooses one)")
    default.add_argument("name", nargs="?", help="the profile to make the default")
    default.add_argument("--clear", action="store_true", help="go back to choosing automatically")
    starter = profile_commands.add_parser(
        "starter", help="preview a starter profile of locally ready lines (--apply saves it after confirmation)")
    starter.add_argument("--name", dest="starter_name", default="starter", metavar="NAME",
                         help="profile name to save (default: starter; an existing name is refused)")
    starter.add_argument("--apply", dest="starter_apply", action="store_true",
                         help="save the previewed profile after a y/N confirmation")
    profile_migrate = profile_commands.add_parser(
        "migrate", help="convert legacy compositions to profiles (a bare run is a dry run)"
    )
    migrate_mode = profile_migrate.add_mutually_exclusive_group()
    migrate_mode.add_argument("--dry-run", action="store_true", dest="profile_migrate_dry_run", help="report the conversion without writing")
    migrate_mode.add_argument("--apply", action="store_true", dest="profile_migrate_apply", help="apply the profile conversion")

    # The earlier spelling of `profile`: accepted, not shown in the root help.
    compose_parser = commands.add_parser("compose", description="The earlier spelling of claude-multi profile.")
    compose_commands = compose_parser.add_subparsers(dest="compose_command", required=True, help="command to run")
    compose_commands.add_parser("list", help="earlier spelling of `profile list`")

    custom_parser = _add_command(commands, "custom")
    custom_commands = custom_parser.add_subparsers(dest="custom_command", required=True, help="command to run")
    custom_commands.add_parser("list", help="list custom providers and models")
    add_provider = custom_commands.add_parser(
        "add-provider", help="register an Anthropic-compatible endpoint"
    )
    add_provider.add_argument("name", help="provider id (never a catalog id)")
    add_provider.add_argument("--base-url", required=True, help="Anthropic-compatible base URL")
    add_provider.add_argument("--auth", choices=("bearer", "header"), required=True, help="bearer, or header (this gateway build honors only x-api-key)")
    add_provider.add_argument("--header", default=None, help="header name for --auth header; only x-api-key is honored")
    add_provider.add_argument("--secret-env", required=True, help="env var in the secret file")
    add_provider.add_argument("--display", default=None, help="display name")
    for action in ("remove-provider", "remove-model"):
        item = custom_commands.add_parser(action, help=f"{action} by name")
        item.add_argument("name", help="id to remove")
    add_model = custom_commands.add_parser(
        "add-model", help="mark a model into the ordinary registry"
    )
    add_model.add_argument("name", help="model id (never a catalog id)")
    add_model.add_argument("--provider", required=True, help="custom or catalog direct provider")
    add_model.add_argument("--wire", required=True, help="upstream model name")
    add_model.add_argument("--context", required=True, type=int, help="operator-declared context tokens (not benchmark-verified)")
    add_model.add_argument("--display", default=None, help="display name")
    show_comp = compose_commands.add_parser("show", help="earlier spelling of `profile show`")
    show_comp.add_argument("name", help="profile name")
    for action in ("new", "edit", "delete"):
        item = compose_commands.add_parser(action, help=f"earlier spelling of `profile {'rm' if action == 'delete' else action}`")
        item.add_argument("name", help="profile name")
    for action in ("duplicate", "rename"):
        item = compose_commands.add_parser(action, help=f"earlier spelling of `profile {action}`")
        item.add_argument("source", help="source profile name")
        item.add_argument("target", help="target profile name")
    compose_commands.add_parser("restore-default", help="earlier spelling of `profile reseed balanced`")
    template = compose_commands.add_parser("use-as-template", help="earlier spelling of `profile duplicate`")
    template.add_argument("source", help="source profile name")
    template.add_argument("target", help="target profile name")

    sessions_parser = _add_command(commands, "sessions")
    session_commands = sessions_parser.add_subparsers(dest="sessions_command", required=True, help="command to run")
    sessions_list = session_commands.add_parser(
        "list", help="list managed sessions (a report; pick one to resume with claude-multi -r)")
    sessions_list.add_argument("--json", action="store_true", dest="json_output",
                               help="one JSON document (stable fields; no transcript content)")
    show_session = session_commands.add_parser("show", help="show a managed session's record (JSON)")
    show_session.add_argument("uuid", metavar="SESSION", help=SESSION_ARGUMENT)
    forget = session_commands.add_parser(
        "forget", help="forget a managed session: its record and generated scope (the transcript is kept)")
    forget.add_argument("uuid", metavar="SESSION", help=SESSION_ARGUMENT)
    forget.add_argument("--force", action="store_true", help="forget even when background liveness cannot be determined")
    forget.add_argument("--yes", action="store_true", help="skip the y/N confirmation (required without a terminal)")
    link = session_commands.add_parser(
        "link", help="adopt a native session (must exist in local Claude metadata)"
    )
    link.add_argument("uuid", nargs="?", help="native session UUID")
    link_target = link.add_mutually_exclusive_group()
    link_target.add_argument("--profile", dest="link_profile", help="adopt following this profile")
    link_target.add_argument("--direct", dest="link_direct", metavar="MODEL", help="adopt as an ad-hoc direct session")
    # The earlier spellings of --profile and --direct: accepted, not shown.
    link_target.add_argument("--composition", dest="link_composition", help=argparse.SUPPRESS)
    link_target.add_argument("--model", dest="link_model", help=argparse.SUPPRESS)
    link.add_argument(
        "--cwd",
        dest="link_cwd",
        help="authoritative original project directory (validated against metadata)",
    )
    relink = session_commands.add_parser(
        "relink-runtime",
        help="repair a managed record with the authoritative native runtime UUID",
    )
    relink.add_argument("uuid", metavar="SESSION", help=f"the managed session: {SESSION_ARGUMENT}")
    relink.add_argument("runtime_uuid", help="UUID shown by native /status or /resume")
    relink.add_argument(
        "--cwd", dest="repair_cwd", help="also replace the recorded original project CWD"
    )
    resolve_fork = session_commands.add_parser(
        "resolve-fork",
        help="discard a pending native-fork marker (the fork transcript is kept)",
    )
    resolve_fork.add_argument("uuid", metavar="SESSION", help=f"the parent session: {SESSION_ARGUMENT}")
    resolve_fork.add_argument(
        "fork_uuid", help="runtime UUID of the native fork to stop tracking"
    )
    resolve_fork.add_argument("--yes", action="store_true",
                              help="skip the y/N confirmation (required without a terminal)")
    stop = session_commands.add_parser(
        "stop",
        help="stop a live background-owned session (upstream `claude stop`; "
        "the conversation is always kept)",
    )
    stop.add_argument("uuid", metavar="SESSION", help=SESSION_ARGUMENT)
    stop.add_argument("--force", action="store_true", help="send claude stop even when background liveness cannot be determined")
    stop.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation (required non-interactively)",
    )
    mark_ended = session_commands.add_parser(
        "mark-ended",
        help="record a synthetic SessionEnd for a session you know has exited "
        "(refused while it is live in the daemon or a running process)",
    )
    mark_ended_target = mark_ended.add_mutually_exclusive_group(required=True)
    mark_ended_target.add_argument("uuid", nargs="?", metavar="SESSION", help=SESSION_ARGUMENT)
    mark_ended_target.add_argument(
        "--all-dead",
        action="store_true",
        dest="mark_all_dead",
        help="every record whose last event is not `end` and that no daemon "
        "socket or running process names",
    )
    # The earlier spelling of `claude-multi lineup --session ID --relaunch
    # profile NAME`: accepted, not shown in `sessions --help`.
    transition_parser = session_commands.add_parser(
        "transition",
        description="The earlier spelling of claude-multi lineup --session ID --relaunch profile NAME: "
        "diff, exited-confirmation, relaunch through the one launch path.",
    )
    transition_parser.add_argument("uuid", help="session UUID or name")
    transition_parser.add_argument(
        "--composition",
        dest="transition_composition",
        required=True,
        help="target profile name (the earlier flag name)",
    )
    transition_parser.add_argument(
        "--its-exited",
        action="store_true",
        help="confirm the target Claude process has EXITED (not merely idle); "
        "skips the interactive confirmation",
    )

    event_parser = commands.add_parser("session-event")
    # The lifecycle hooks (never shown). The four scope-only events are
    # dispatched to hooks.py before Runtime; start/end keep the legacy record
    # reconcile (with the protocol-3 notice for --hook-protocol 3).
    event_parser.add_argument(
        "event", choices=("start", "end", "prompt", "premodel", "postmodel", "subagent")
    )
    event_parser.add_argument("--managed-id", required=True)
    event_parser.add_argument("--launch-epoch", type=int, default=None)
    event_parser.add_argument(
        "--hook-protocol", type=int, choices=(lineup_files.HOOK_PROTOCOL,), default=None
    )

    migrate_parser = _add_command(commands, "migrate")
    migrate_parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="report what would change; write nothing (no lock, shim or marker)",
    )
    migrate_parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="print the report as canonical JSON",
    )
    restore_parser = _add_command(commands, "restore-2x")
    restore_parser.add_argument("--check", action="store_true", help="check rollback metadata without writes")
    restore_parser.add_argument("--json", action="store_true", dest="json_output", help="JSON metadata check (requires --check)")
    restore_parser.add_argument(
        "--not-running",
        nargs="+",
        action="extend",
        default=[],
        metavar="ID",
        dest="not_running",
        help="a session you know has exited without a SessionEnd (never "
        "overrides a session live in the Claude daemon)",
    )
    restore_parser.add_argument(
        "--assume-dead",
        nargs="+",
        action="extend",
        default=[],
        metavar="ID",
        dest="assume_dead",
        help="like --not-running, and also record the session's end",
    )
    models_parser = _add_command(
        commands, "models",
        description="Without a command (or with list): the models (generation, provider, class, efforts, "
        "status), their typed /model selectors and the retired keys. The other commands manage the models "
        "you add.",
    )
    models_parser.add_argument("--candidates", action="store_true",
                               help="advisory registry and served candidates (nothing declared or admitted)")
    models_parser.add_argument("--all", dest="candidates_all", action="store_true",
                               help="with --candidates: every registry channel and older candidates")
    _add_models_commands(models_parser)
    providers_parser = _add_command(commands, "providers")
    _add_providers_commands(providers_parser)
    discover_parser = _add_command(
        commands, "discover",
        description="List the models a provider advertises (PROVIDER), every enabled direct provider "
        "(--all), or compare the public feed with the pinned registry (--feed). Provider calls run only "
        "in a terminal outside Claude Code sessions, after one y/N naming every request.",
    )
    discover_parser.add_argument("provider", nargs="?", default=None, metavar="PROVIDER",
                                 help="provider id (see `claude-multi models`)")
    discover_modes = discover_parser.add_mutually_exclusive_group()
    discover_modes.add_argument("--all", dest="discover_all", action="store_true",
                                help="list every enabled direct provider with a supported listing "
                                "(observation only; `discover openai` stays separate)")
    discover_modes.add_argument("--feed", dest="discover_feed", action="store_true",
                                help="compare the public model feed with the pinned registry (advisory)")
    discover_parser.add_argument("--add", dest="discover_add", nargs="+", action="extend", default=[],
                                 metavar="WIRE", help="declare listed WIRE(s); admission and diagnostics are optional")
    discover_parser.add_argument("--as", dest="discover_as", default=None, metavar="KEY",
                                 help="line key for exactly one --add WIRE (custom-... reserves local ids against future catalog keys)")
    discover_parser.add_argument("--family", dest="discover_family", default=None, metavar="LABEL",
                                 help="model family label for exactly one --add WIRE (overrides the default; no inference)")
    discover_parser.add_argument("--context", dest="discover_context", type=int, default=None, metavar="N",
                                 help="declared context when the listing states none, or an explicit override")
    discover_parser.add_argument("--over-listed", dest="over_listed", default=None, metavar="REASON",
                                 help="required to declare --context above the listing-stated value")
    discover_parser.add_argument(
        "--yes-i-approve-this-provider-call",
        dest="approve_provider_call",
        action="store_true",
        help=argparse.SUPPRESS,  # retired: parsed only to print its migration error
    )
    update_parser = _add_command(
        commands, "update",
        description="Update claude-multi itself: check, show the plan, ask, install (installed releases), "
        "or switch back to the previously installed version.",
    )
    update_modes = update_parser.add_mutually_exclusive_group()
    update_modes.add_argument("--check", dest="update_check", action="store_true",
                              help="only check whether a newer release exists")
    update_modes.add_argument("--rollback", dest="update_rollback", action="store_true",
                              help="switch back to the previously installed version")
    update_parser.add_argument("--yes", "-y", dest="update_yes", action="store_true",
                               help="apply without asking (needed without a terminal)")
    update_parser.add_argument("--from-dir", dest="update_from_dir", metavar="DIR",
                               help="use the release files in a local directory instead of downloading")
    update_parser.add_argument("--base-url", dest="update_base_url", metavar="URL",
                               help="download from this https location instead of the release's own")
    _add_setup_parser(commands)
    _add_window_ceiling_parser(commands)
    show = commands.add_parser("show", help="earlier spelling of profile show")
    show.add_argument("show_composition", nargs="?", help="profile name (default: balanced)")
    _hide_command(commands, "show")  # the earlier spelling of profile show: accepted, not listed
    quota_parser = _add_command(commands, "quota")
    quota_parser.add_argument("--json", action="store_true", help="one read-only report object")
    explain_parser = _add_command(commands, "explain")
    explain_parser.add_argument("session", nargs="?", metavar="SESSION", help=f"{SESSION_ARGUMENT}; defaults to the current session")
    explain_parser.add_argument("--agent", metavar="A", help="bound cm-* role or exact observed agent id")
    explain_parser.add_argument("--json", action="store_true", help="one read-only report object")
    usage_parser = _add_command(commands, "usage")
    usage_parser.add_argument("--since", default="24h", metavar="WHEN", help="RFC 3339 instant or duration up to 30d; default 24h")
    usage_parser.add_argument("--session", metavar="SESSION", help=f"{SESSION_ARGUMENT}; exact attribution only")
    usage_parser.add_argument("--json", action="store_true", help="one read-only report object")
    plan_parser = _add_command(commands, "plan")
    plan_parser.add_argument("--assets", metavar="PATH",
                             help="plan a candidate package's assets (the running launcher is unchanged)")
    plan_parser.add_argument("--json", action="store_true", help="one read-only plan object")
    # Portability of your configuration between computers.
    export_parser = _add_command(
        commands, "export",
        description="Write your portable configuration (JSON on stdout by default): no secrets, keys, "
        "sessions, logs or evidence; trust travels as inert re-approval requests.")
    export_parser.add_argument("--out", dest="export_out", type=Path, metavar="FILE",
                               help="write FILE atomically (0600) and record a confirmed-export receipt")
    import_parser = _add_command(
        commands, "import",
        description="Preview a portable export against this computer (writes nothing); --apply writes the "
        "ready items; imported trust is never active.")
    import_parser.add_argument("import_file", type=Path, metavar="FILE", help="a `claude-multi export` JSON file")
    import_parser.add_argument("--apply", dest="import_apply", action="store_true",
                               help="confirm and apply the ready items shown (a terminal outside Claude Code)")
    _add_gateway_commands(commands)
    uninstall_parser = _add_command(
        commands, "uninstall",
        description="Remove claude-multi from this computer. The plan is shown first; your credentials go "
        "only after a typed confirmation; kept backups and your Claude Code are never removed.")
    uninstall_parser.add_argument("--dry-run", dest="uninstall_dry_run", action="store_true",
                                  help="show the plan and remove nothing")
    uninstall_parser.add_argument("--keep-setup", dest="keep_setup", action="store_true",
                                  help="keep your setup and session state (profiles, settings, records)")
    uninstall_parser.add_argument("--keep-credentials", dest="keep_credentials", action="store_true",
                                  help="keep your API keys, sign-ins and the local gateway key without asking")
    uninstall_parser.add_argument("--yes", action="store_true", help="skip the y/N confirmation (never the "
                                  "typed one for credentials)")
    uninstall_parser.add_argument("--force", action="store_true",
                                  help="remove even when sessions may still be running (they are named)")
    doctor_parser = _add_command(
        commands, "doctor",
        description="Check this installation and its sessions. The verdict is Ready, Attention or BLOCKED; "
        "each finding names one fix. Plain doctor refreshes the helper shims as routine housekeeping; "
        "use doctor --json for a read-only report. --repair, --prune and --rotate-token act.",
    )
    doctor_actions = doctor_parser.add_mutually_exclusive_group()
    doctor_actions.add_argument("--json", action="store_true", help="read-only diagnostic report (one JSON document)")
    doctor_parser.add_argument("-v", "--verbose", dest="doctor_verbose", action="store_true",
                               help="also show every session's line and the observation details")
    doctor_parser.add_argument("--first-run", dest="doctor_first_run", action="store_true",
                               help="the first-run checks in order, one fix each (with --json: one object)")
    doctor_actions.add_argument(
        "--repair",
        metavar="SESSION",
        dest="doctor_repair",
        help=f"converge a session's scope to its record authority ({SESSION_ARGUMENT})",
    )
    doctor_actions.add_argument(
        "--repair-all",
        action="store_true",
        dest="doctor_repair_all",
        help="converge every durable session's scope (and refresh its record "
        "snapshot) against the installed catalog",
    )
    doctor_parser.add_argument("--preview", action="store_true", help="read-only generated-state prune preview")
    doctor_actions.add_argument(
        "--prune",
        action="store_true",
        dest="doctor_prune",
        help="remove stale staging dirs and scopes whose records are gone",
    )
    doctor_actions.add_argument(
        "--rotate-token",
        action="store_true",
        dest="doctor_rotate_token",
        help="rotate the local gateway token hitlessly (dual-key window, "
        "~5 min helper-TTL wait, reload verified; interactive; rerun resumes)",
    )
    doctor_actions.add_argument(
        "--prune-aliases",
        nargs="*",
        metavar="ALIAS",
        dest="doctor_prune_aliases",
        default=None,
        help="remove gateway continuity aliases no live session references "
        "(all, or the named ones)",
    )
    doctor_parser.add_argument(
        "--include-live",
        action="store_true",
        dest="doctor_include_live",
        help="with --repair-all: also converge sessions whose last event is not "
        "`end` (live or unknown)",
    )
    _add_presentation_flags(parser)
    return parser


# ----------------------------------------------------------- the local gateway

GATEWAY_START_WAIT = 45.0


def _add_gateway_commands(commands: "argparse._SubParsersAction") -> None:
    """``claude-multi gateway start|stop|restart|status|logs|clear-hold|ensure|service``."""

    parser = _add_command(
        commands, "gateway",
        description="Manage the local gateway. It starts on demand when a session needs it; stop "
        "and restart act only on a gateway proven to be yours and never while a credential save "
        "may be unresolved.",
    )
    sub = parser.add_subparsers(dest="gateway_command", required=True, metavar="COMMAND",
                                help="command to run")
    sub.add_parser("start", help="start the gateway if it is not running and wait until it is ready")
    sub.add_parser("stop", help="stop your gateway (refused while the persistence hold is active)")
    sub.add_parser("restart", help="stop, then start your gateway (same checks as stop)")
    sub.add_parser("status", help="backend, endpoint, instance, log and persistence hold (sends no token)")
    logs = sub.add_parser("logs", help="the newest gateway log lines (or one instance's)")
    logs.add_argument("-n", "--lines", type=int, default=50, metavar="N", help="how many lines (default 50)")
    logs.add_argument("--instance", metavar="ID", help="the instance id shown by `claude-multi gateway status`")
    sub.add_parser("clear-hold", help="clear the persistence hold after you verified the credentials "
                   "(a terminal outside Claude Code; typed confirmation; the gateway's own recovery "
                   "for a hold its log reads could not settle)")
    ensure = sub.add_parser("ensure", help="for scripts: exit 0 only when your gateway runs and is ready "
                            "(starts it when stopped)")
    ensure.add_argument("--quiet", action="store_true", help="print nothing on success")
    ensure.add_argument("--max-wait", dest="max_wait", type=float, default=GATEWAY_START_WAIT, metavar="SECONDS",
                        help=f"wait at most this long (default {GATEWAY_START_WAIT:g})")
    ensure.add_argument("--base-url", dest="base_url", metavar="URL",
                        help="refuse unless the gateway's configured endpoint is URL (a session's compiled "
                        "ANTHROPIC_BASE_URL)")
    _add_gateway_service_commands(sub)


def _add_gateway_service_commands(sub: "argparse._SubParsersAction") -> None:
    """``claude-multi gateway service install [--name NAME] | uninstall | status``."""

    parser = sub.add_parser(
        "service", help="the supervised service (Linux with a user service manager): install, uninstall, status",
        description="Hand the gateway to a hardened systemd user unit that restarts it on failure, or back to "
        "the on-demand start. Both directions stop the running gateway only after the persistence hold, and "
        "restore the previous backend when a step fails.",
    )
    service = parser.add_subparsers(dest="gateway_service_command", required=True, metavar="COMMAND",
                                    help="command to run")
    install = service.add_parser("install", help="install (or refresh) the unit and hand the gateway to it")
    install.add_argument("--name", metavar="NAME", help="the unit's name (default: claude-multi-gateway)")
    service.add_parser("uninstall", help="remove the unit and hand the gateway back to the on-demand start")
    service.add_parser("status", help="whether the service is installed, its name, backend and unit state")


# Gateway verbs that only read (no start, stop, hold, endpoint or unit change).
_GATEWAY_REPORT = frozenset({"status", "logs"})


def _gateway_command_writes(args: argparse.Namespace) -> bool:
    """Gateway verbs that may start, stop or record anything (``service status`` reads)."""

    command = getattr(args, "gateway_command", None)
    if command == "service":
        return getattr(args, "gateway_service_command", None) != "status"
    return command not in _GATEWAY_REPORT


# ----------------------------------------------------------- providers and models

PROVIDER_KINDS = ("anthropic-compatible", "openai-compatible-lan", "openai-compatible")
MODEL_SOURCES = ("docs", "operator", "registry")
# The reviewed transports of a shipped account provider (kept literal: the
# parser imports no operator module).
TRANSPORT_CHOICES = ("oauth-pool", "api-key")


def _add_providers_commands(parser: argparse.ArgumentParser) -> None:
    """``claude-multi providers ...``: the shipped providers and yours
    (``providers.d``), presets, keys and sign-ins, and the ``transport`` verb,
    which stays apart from enable/disable."""

    sub = parser.add_subparsers(dest="providers_command", required=True, metavar="COMMAND", help="command to run")
    providers_list = sub.add_parser(
        "list", help="list every provider, shipped and yours: source, on or off, connection and line counts")
    providers_list.add_argument("--json", action="store_true", dest="json_output",
                                help="one JSON document (stable fields; never a key value)")
    show = sub.add_parser("show", help="show one provider declaration")
    show.add_argument("provider_id", metavar="ID", help="provider id (providers.d/<id>.json)")
    show.add_argument("--resolved", action="store_true", help="print the resolved provider and lines as JSON")
    validate = sub.add_parser("validate", help="validate providers.d (or one candidate FILE); writes nothing")
    validate.add_argument("file", nargs="?", metavar="FILE", help="a candidate <id>.json checked with the current files")
    template = sub.add_parser("template", help="print a providers.d template for a reviewed kind")
    template.add_argument("--kind", choices=PROVIDER_KINDS, default="anthropic-compatible",
                          help="reviewed provider kind (default anthropic-compatible)")
    add = sub.add_parser("add", help="declare a new provider, by hand or from a reviewed --preset "
                         "(route approval follows unless --declare-only)")
    add.add_argument("provider_id", nargs="?", metavar="ID", help="new provider id (never a catalog id)")
    add.add_argument("--preset", default=None, metavar="PRESET",
                     help="declare from a reviewed preset or sample (a template, never a grant)")
    add.add_argument("--as", dest="as_id", default=None, metavar="ID", help="provider id for --preset")
    add.add_argument("--secret-file", default=None, metavar="FILE",
                     help="import the key from a private 0600 file into the secret store (terminal only)")
    shared_key = add.add_mutually_exclusive_group()
    shared_key.add_argument("--reuse-key", action="store_true",
                            help="for --preset when another provider already uses its API key: share that saved "
                                 "key (none is imported; by default the new provider gets its own key name)")
    shared_key.add_argument("--replace-key", action="store_true",
                            help="for --preset when another provider already uses its API key: replace that "
                                 "shared key with --secret-file (asks first, naming every provider using it)")
    add.add_argument("--kind", default=None, choices=PROVIDER_KINDS, help="generation protocol (default: anthropic-compatible, recommended for documented Messages endpoints; explicit preset kind wins)")
    add.add_argument("--base-url", default=None, help="provider base URL (https when keyed)")
    add.add_argument("--auth", default=None, choices=("bearer", "header", "none"), help="credential transport")
    add.add_argument("--header", default=None, help="header name for --auth header (x-api-key only)")
    add.add_argument("--secret-ref", default=None, metavar="env:NAME",
                     help="logical credential reference in the secret store (never the value)")
    add.add_argument("--family", default=None, help="family label (1–64 printable single-line characters; unrecognized labels do not establish independence)")
    add.add_argument("--display", default=None, help="display name")
    add.add_argument("--contracts", default=None, metavar="CONTRACT,...", help="reviewed payload contracts it uses")
    add.add_argument("--listing-url", default=None, metavar="URL", help="model listing URL (with --listing-shape)")
    add.add_argument("--listing-auth", choices=("provider", "none"), default=None,
                     help="listing authentication only (default: provider for keyed routes, none for LAN)")
    add.add_argument("--listing-shape", default=None, choices=("anthropic", "openai"),
                     help="model listing shape (with --listing-url)")
    add.add_argument("--declare-only", action="store_true",
                     help="write an inert, unapproved declaration (no approval, no provider call)")
    edit = sub.add_parser("edit", help="edit a declaration in $VISUAL/$EDITOR (validated before it is written)")
    edit.add_argument("provider_id", metavar="ID", help="provider id")
    approve = sub.add_parser("approve", help="approve a declared provider's credential route (terminal, outside "
                             "sessions)")
    approve.add_argument("provider_id", metavar="ID", help="provider id")
    set_key = sub.add_parser("set-key", help="set or replace a provider's API key (shipped providers and yours; "
                             "terminal, outside sessions)")
    set_key.add_argument("provider_id", metavar="ID", help="provider id")
    set_key.add_argument("--secret-file", default=None, metavar="FILE",
                         help="read the key from a private 0600 file instead of a hidden prompt")
    set_key.add_argument("--yes", action="store_true",
                         help="replace a saved key without the y/N question (for every provider that uses it)")
    remove_key = sub.add_parser("remove-key", help="remove a provider's saved API key (terminal, outside sessions)")
    remove_key.add_argument("provider_id", metavar="ID", help="provider id")
    remove_key.add_argument("--yes", action="store_true", help="skip the y/N confirmation")
    remove_key.add_argument("--name", default=None, metavar="NAME",
                            help="the key's name, for a key kept after its provider was removed")
    sign_in = sub.add_parser("sign-in", help="sign in to your Claude or ChatGPT account in this terminal "
                             "(personal use; you confirm that first)")
    sign_in.add_argument("provider_id", metavar="PROVIDER", choices=("anthropic", "openai"),
                         help="anthropic (Claude account) or openai (ChatGPT account)")
    sign_in.add_argument("--no-browser", dest="no_browser", action="store_true",
                         help="print the address instead of opening a browser (Claude account)")
    sign_out = sub.add_parser("sign-out", help="sign out of your Claude or ChatGPT account (the records are kept "
                              "as a backup)")
    sign_out.add_argument("provider_id", metavar="PROVIDER", choices=("anthropic", "openai"),
                          help="anthropic (Claude account) or openai (ChatGPT account)")
    sign_out.add_argument("--yes", action="store_true", help="skip the y/N confirmation")
    test = sub.add_parser("test", help="send one small request to each named provider after one consent "
                          "(may be billed)")
    test.add_argument("provider_ids", metavar="ID", nargs="+", help="provider id")
    for verb, text in (("enable", "turn a provider on for new sessions"),
                       ("disable", "turn a provider off for new sessions (running sessions keep theirs)")):
        sub.add_parser(verb, help=text).add_argument("provider_id", metavar="ID", help="provider id")
    rm = sub.add_parser("rm", help="remove a provider you added (refused while its lines are admitted, bound or "
                        "live)")
    rm.add_argument("provider_id", metavar="ID", help="provider id")
    rm.add_argument("--yes", action="store_true", help="skip the y/N confirmation (the API key is kept)")
    sub.add_parser("apply", help="re-render the gateway config from the catalog and providers.d, verify the reload")
    transport = sub.add_parser(
        "transport", help="show or switch a catalog OAuth-pool provider's reviewed transport "
        "(same selectors; switching is a guarded route approval)")
    transport.add_argument("provider_id", metavar="PROVIDER", help="catalog provider id (e.g. anthropic)")
    transport.add_argument("transport_choice", nargs="?", default=None, choices=TRANSPORT_CHOICES,
                           metavar="{" + ",".join(TRANSPORT_CHOICES) + "}",
                           help="the transport to use (omit to show the current one)")
    transport.add_argument("--secret-file", default=None, metavar="FILE",
                           help="import the key from a private 0600 file (api-key; terminal only)")
    migrate = sub.add_parser("migrate-custom", help="migrate custom.json to providers.d (a bare run is a dry run)")
    migrate.add_argument("--apply", action="store_true", dest="migrate_apply",
                         help="write the files, routes and admissions (terminal, outside sessions)")


def _add_models_commands(parser: argparse.ArgumentParser) -> None:
    """``claude-multi models ...``; bare ``models`` stays the listing."""

    sub = parser.add_subparsers(dest="models_command", metavar="COMMAND", help="command to run")
    models_list = sub.add_parser("list", help="the listing (same as claude-multi models)")
    models_list.add_argument("--json", action="store_true", dest="json_output",
                             help="one JSON document (stable fields)")
    add = sub.add_parser("add", help="declare a model of yours (New · not admitted; allowed anywhere)")
    add.add_argument("provider", metavar="PROVIDER", help="catalog or providers.d provider id")
    add.add_argument("wire", metavar="WIRE", help="upstream model id")
    add.add_argument("--as", dest="key", default=None, metavar="KEY",
                     help="line key (derived from WIRE; custom- reserves local ids against future catalog keys)")
    add.add_argument("--family", default=None, metavar="LABEL",
                     help="model family label (overrides the provider default; no inference)")
    add.add_argument("--context", required=True, type=int, metavar="N", help="declared context tokens")
    add.add_argument("--source", required=True, choices=MODEL_SOURCES, help="where the context figure comes from")
    add.add_argument("--source-ref", default=None, metavar="TEXT", help="URL/date for --source docs|registry")
    add.add_argument("--effort", action="append", default=[], metavar="LEVEL[=CONTRACT]",
                     help="declared effort (repeatable; with contracts a map, else a list)")
    add.add_argument("--default-effort", default=None, metavar="LEVEL", help="default effort (a declared one)")
    add.add_argument("--display", default=None, help="display name (chosen freely; defaults to WIRE)")
    for verb, text in (("admit", "record an optional local admission badge (zero inference; terminal, outside sessions)"),
                       ("revoke", "remove only the admission badge (use and qualification unchanged)"),
                       ("edit", "edit a model of yours in $VISUAL/$EDITOR (shows the admission consequences)")):
        item = sub.add_parser(verb, help=text)
        item.add_argument("key", metavar="KEY", help="line key")
        if verb == "revoke":
            item.add_argument("--yes", action="store_true", help="skip the y/N confirmation (required without "
                              "a terminal)")
    rm = sub.add_parser("rm", help="remove a line you added (captured aliases stay served until pruned)")
    rm.add_argument("key", metavar="KEY", help="line key")
    rm.add_argument("--successor", default=None, metavar="KEY",
                    help="rewrite the profiles and named bindings that name KEY to this offered line")
    rm.add_argument("--yes", action="store_true", help="skip the y/N confirmation")
    show = sub.add_parser("show", help="show a line (status, selectors, class, evidence)")
    show.add_argument("key", metavar="KEY", help="line key")
    show.add_argument("--resolved", action="store_true", help="print the resolved entry as JSON")
    show.add_argument("--evidence", action="store_true", help="print the tool-owned evidence as JSON")
    qualify = sub.add_parser(
        "qualify",
        help="optional diagnostics for a model of yours: consented, bounded checks (evidence only)",
        description="Run the optional, consented qualification battery on a model of yours (its own aliases) through the "
        "loopback gateway. Every request is listed before one y/N; no retries; 120 s and 256 KiB per "
        "request (context 300 s). Evidence only: it never admits a line or edits its declaration. No flag "
        "means --smoke. Your models only (shipped models carry reviewed evidence).",
    )
    qualify.add_argument("key", metavar="KEY", help="the key of a model you added")
    qualify.add_argument("--smoke", action="store_true", help="one minimal generation at the default effort")
    qualify.add_argument("--efforts", action="store_true", help="one minimal generation per declared effort")
    qualify.add_argument("--tools", action="store_true",
                         help="a strict-schema tool round trip (up to 2 requests; the second only after the "
                         "expected tool call)")
    qualify.add_argument("--tool-choice", choices=("forced", "auto"), default="forced",
                         help="--tools with a forced named tool (default) or a strict automatic tool; "
                         "disclosed before consent, never retried the other way")
    qualify.add_argument("--stream", action="store_true", help="one streamed generation ending in message_stop")
    qualify.add_argument("--context", type=int, metavar="N", default=None,
                         help="a synthetic retrieval at about N input tokens (8192..declared; 300 s)")
    qualify.add_argument("--agents", action="store_true",
                         help="--smoke --efforts --tools --stream (not --context); first-party pool lines "
                         "also run the offline exact-client check")


_PROVIDERS_REPORT = frozenset({"list", "show", "validate", "template", "test"})
_MODELS_REPORT = frozenset({"list", "show"})


def _read_only_listing(args: argparse.Namespace) -> bool:
    """The lists, shows and other reports that change nothing, under every
    spelling (the earlier ``compose``, root ``show`` and ``custom list``
    included), ``--print-launch`` and ``update --check``: they read, never
    record the channel, create a directory, refresh a shim, install a seed
    or take a write lock."""

    command = args.command
    if command in (None, "direct") and bool(getattr(args, "print_launch", False)):
        return True
    if command == "show":
        # The earlier spelling of `profile show`: the same report.
        return True
    if command == "update":
        # Only checking, from the release server or --from-dir; installing
        # and rolling back write.
        return bool(getattr(args, "update_check", False))
    if command == "sessions":
        return args.sessions_command in ("list", "show")
    if command == "profile":
        return args.profile_command in ("list", "show", "starter", "default") and not _profile_command_writes(
            args.profile_command, args)
    if command == "compose":
        alias = _COMPOSE_ALIASES.get(args.compose_command)
        return alias in ("list", "show")
    if command == "custom":
        return args.custom_command == "list"
    if command == "models":
        return (getattr(args, "models_command", None) in (None, "list", "show")
                and not getattr(args, "candidates", False))
    if command == "providers":
        return args.providers_command in ("list", "show", "validate", "template") or (
            args.providers_command == "transport" and getattr(args, "transport_choice", None) is None)
    if command == "doctor":
        # Every report (plain, verbose, JSON, preview, first run); a repair,
        # prune, rotation or alias prune writes.
        return not _writes_versioned_state(args)
    if command == "window-ceiling":
        return not _writes_versioned_state(args)
    if command == "gateway":
        return not _gateway_command_writes(args)
    if command == "setup":
        return bool(getattr(args, "setup_status", False))
    return False


def _operator_command_writes(args: argparse.Namespace) -> bool:
    """providers/models verbs that may write operator or launcher state."""

    if args.command == "providers":
        command = args.providers_command
        if command == "migrate-custom":
            return bool(getattr(args, "migrate_apply", False))
        if command == "transport":
            return getattr(args, "transport_choice", None) is not None
        return command not in _PROVIDERS_REPORT
    sub = getattr(args, "models_command", None)
    return sub is not None and sub not in _MODELS_REPORT


# ----------------------------------------------------------- profile


_COMPOSE_ALIASES = {
    "list": "list",
    "show": "show",
    "new": "new",
    "edit": "edit",
    "delete": "rm",
    "duplicate": "duplicate",
    "rename": "rename",
    "use-as-template": "duplicate",
    "restore-default": "reseed",
}


def _profile_command_writes(command: str, args: argparse.Namespace) -> bool:
    """A `profile` command writes unless list/show/migrate without --apply."""

    if command in ("list", "show"):
        return False
    if command == "default":
        return bool(getattr(args, "name", None)) or bool(getattr(args, "clear", False))
    if command == "migrate":
        return bool(getattr(args, "profile_migrate_apply", False))
    if command == "starter":
        return bool(getattr(args, "starter_apply", False))
    return True


def _profile_migrate_read_only(args: argparse.Namespace) -> bool:
    """`profile migrate` without --apply builds a read-only Runtime."""

    return (
        args.command == "profile"
        and getattr(args, "profile_command", None) == "migrate"
        and not getattr(args, "profile_migrate_apply", False)
    )


SETUP_STEPS = ("preflight", "claude", "gateway", "providers", "test", "profile", "check")
# The options each step accepts (``setup`` refuses them on another step).
SETUP_STEP_OPTIONS = {"claude": ("claude_from",), "gateway": ("proxy", "no_proxy")}


def _add_setup_parser(commands: "argparse._SubParsersAction") -> None:
    """``claude-multi setup [--step STEP] [--redo] [--status] [--answers FILE] [--keys-file PATH]``."""

    setup_parser = _add_command(
        commands, "setup",
        description="Run the setup steps that are not done yet, in order: preflight, claude, gateway, "
        "providers, test (optional), profile, check. Nothing is saved until you confirm. --step runs one "
        "step (also when it is done); claude puts claude-multi's own copy of the pinned Claude Code in place; "
        "gateway records the gateway's outbound proxy (an unauthenticated http, https or socks5 URL; the "
        "gateway never inherits proxy variables) and starts the gateway, or re-renders a running one.",
    )
    setup_parser.add_argument("--step", dest="step", choices=SETUP_STEPS, default=None,
                              help="run this step only (also when it is done)")
    setup_parser.add_argument("--redo", action="store_true", help="run the steps that are done too")
    setup_parser.add_argument("--status", dest="setup_status", action="store_true",
                              help="show the steps and exit 0 when every required one is done, else 1")
    setup_parser.add_argument("--answers", dest="answers", type=Path, metavar="FILE",
                              help="a prepared setup (keys by file only; sign-ins and tests need you at the "
                              "terminal)")
    setup_parser.add_argument("--keys-file", dest="keys_file", type=Path, metavar="PATH",
                              help="use an existing private key file (NAME=value lines) for API keys")
    setup_parser.add_argument("--claude-from", dest="claude_from", type=Path, metavar="PATH",
                              help="claude step: copy the pinned Claude Code from this file "
                              "(checked by size and sha256)")
    proxy = setup_parser.add_mutually_exclusive_group()
    proxy.add_argument("--proxy", metavar="URL",
                       help="gateway step: the gateway's outbound proxy (no credentials in it)")
    proxy.add_argument("--no-proxy", dest="no_proxy", action="store_true",
                       help="gateway step: remove the gateway's outbound proxy")


def _add_window_ceiling_parser(commands: "argparse._SubParsersAction") -> None:
    """``claude-multi window-ceiling [VALUE | --reset]`` (show with neither)."""

    parser = _add_command(
        commands, "window-ceiling",
        description="One ceiling governs the lead and every agent: a session's window is the smaller of "
        "it and the lead set's smallest provider bound, and agent lines whose bound is below that window "
        "keep the 200K class. Without arguments it shows the ceiling and whether it is set or the default.",
    )
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("ceiling_value", nargs="?", metavar="VALUE",
                        help="the new ceiling in tokens (400000) or thousands (400K), from 200K to 800K")
    choice.add_argument("--reset", dest="ceiling_reset", action="store_true",
                        help="go back to the default ceiling (800K)")


def _writes_versioned_state(args: argparse.Namespace) -> bool:
    """Commands that may mutate current-format records, scopes or custom state."""

    if args.command in (None, "direct") and bool(getattr(args, "print_launch", False)):
        return False
    if args.command is None:
        return args.resume != ""  # Bare -r can remain a read-only listing.
    if args.command in ("direct", "session-event"):
        return True
    if args.command == "migrate":
        return not args.dry_run
    if args.command == "lineup":
        return True  # intercepted before argparse; `show` is read-only there
    if args.command == "profile":
        return _profile_command_writes(args.profile_command, args)
    if args.command == "compose":
        return _profile_command_writes(_COMPOSE_ALIASES[args.compose_command], args)
    if args.command == "sessions":
        return args.sessions_command not in ("list", "show", "stop")
    if args.command == "custom":
        return args.custom_command != "list"
    if args.command in ("providers", "models"):
        return _operator_command_writes(args)
    if args.command == "discover":
        return bool(getattr(args, "discover_add", None))  # --add declares in a transaction
    if args.command == "export":
        return getattr(args, "export_out", None) is not None  # --out records the receipt
    if args.command == "import":
        return bool(getattr(args, "import_apply", False))
    if args.command == "gateway":
        return _gateway_command_writes(args)
    if args.command == "setup":
        return not getattr(args, "setup_status", False)
    if args.command == "uninstall":
        return not getattr(args, "uninstall_dry_run", False)
    if args.command == "window-ceiling":
        return getattr(args, "ceiling_value", None) is not None or bool(getattr(args, "ceiling_reset", False))
    if args.command == "doctor":
        # Rotation reads every record for environment-credential liveness and re-renders
        # the gateway config; neither is safe against current-format state.
        return bool(
            args.doctor_repair
            or args.doctor_repair_all
            or (args.doctor_prune and not getattr(args, "preview", False))
            or args.doctor_rotate_token
            or args.doctor_prune_aliases is not None
        )
    return False
