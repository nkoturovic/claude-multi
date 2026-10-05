#!/usr/bin/env python3
"""Generate the documentation's reference tables from the product's own definitions.

    python3 tools/docs_gen.py          rewrite every generated region
    python3 tools/docs_gen.py --check  name each stale region and exit 1

The sources are the definitions the product itself reads, never a second
list: the launcher's argument parser (``cli.parser.build_parser`` with its
command groups and examples), the gateway tool's command table
(``proxy.PROXY_COMMANDS``), the ``/cm`` verb table
(``lineup_files.CM_VERBS``), the operation inventory (``surface_matrix``),
the interface's key table (``cli.screens.actions``) and its help texts, the
release catalog (lines, providers and shipped profiles) and the presets.

A page holds each generated region between two marker lines and nothing
else on the page is touched::

    <!-- generated: NAME (tools/docs_gen.py) -->
    <!-- end of generated: NAME -->

The output is deterministic: it names no path of this computer, reads no
environment, and writes the parser from its actions rather than through
``argparse``'s formatter, whose layout depends on the terminal and the
Python version. Commands and operations for an earlier format, for the
lifecycle hooks or for the gateway service are classified before anything
is written (:func:`classification`): the ordinary reference names an
earlier format's command with its summary only and leaves the hidden and
internal ones out, and the coverage test accounts for every one of them.
"""

from __future__ import annotations

import argparse
import functools
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

BEGIN = "<!-- generated: {name} (tools/docs_gen.py) -->"
END = "<!-- end of generated: {name} -->"
REGION = re.compile(r"<!-- generated: ([a-z0-9-]+) \(tools/docs_gen\.py\) -->")

# ------------------------------------------------------------------ values

# How each value-taking argument is written: the typed placeholder of the
# documentation's span checker (``tests/_docs_vocab.py`` ``PLACEHOLDERS``),
# by command path and destination; ``""`` writes the argument's choices
# instead. A visible argument missing here stops the generator, so a new
# argument is classified before any page changes.
VALUES: dict[tuple[str, str], str] = {
    ("", "profile"): "name", ("", "profile_file"): "path", ("", "resume"): "id",
    ("direct", "direct_model"): "model", ("direct", "direct_resume"): "id",
    ("lineup", "session"): "runtime-id", ("lineup", "lineup_request"): "request",
    ("profile show", "name"): "name", ("profile new", "name"): "name", ("profile new", "from_profile"): "profile",
    ("profile edit", "name"): "name", ("profile rm", "name"): "name", ("profile rename", "source"): "name",
    ("profile rename", "target"): "new", ("profile duplicate", "source"): "name",
    ("profile duplicate", "target"): "new", ("profile reseed", "name"): "name", ("profile default", "name"): "name",
    ("profile starter", "starter_name"): "name",
    ("sessions show", "uuid"): "id", ("sessions forget", "uuid"): "id", ("sessions link", "uuid"): "runtime-id",
    ("sessions link", "link_profile"): "name", ("sessions link", "link_direct"): "model",
    ("sessions link", "link_cwd"): "dir", ("sessions relink-runtime", "uuid"): "id",
    ("sessions relink-runtime", "runtime_uuid"): "runtime-id", ("sessions relink-runtime", "repair_cwd"): "dir",
    ("sessions resolve-fork", "uuid"): "id", ("sessions resolve-fork", "fork_uuid"): "fork-id",
    ("sessions stop", "uuid"): "id", ("sessions mark-ended", "uuid"): "id",
    ("models add", "provider"): "provider", ("models add", "wire"): "wire", ("models add", "key"): "new-id",
    ("models add", "context"): "n", ("models add", "source"): "", ("models add", "source_ref"): "text",
    ("models add", "effort"): "effort", ("models add", "default_effort"): "effort", ("models add", "display"): "text",
    ("models admit", "key"): "line", ("models revoke", "key"): "line", ("models edit", "key"): "line",
    ("models rm", "key"): "line", ("models rm", "successor"): "line", ("models show", "key"): "line",
    ("models qualify", "key"): "line", ("models qualify", "tool_choice"): "", ("models qualify", "context"): "n",
    ("providers show", "provider_id"): "provider", ("providers validate", "file"): "file",
    ("providers template", "kind"): "", ("providers add", "provider_id"): "provider",
    ("providers add", "preset"): "preset", ("providers add", "as_id"): "provider",
    ("providers add", "secret_file"): "file", ("providers add", "kind"): "", ("providers add", "base_url"): "url",
    ("providers add", "auth"): "", ("providers add", "header"): "header", ("providers add", "secret_ref"): "secret-ref",
    ("providers add", "family"): "family", ("providers add", "display"): "text",
    ("providers add", "contracts"): "contracts", ("providers add", "listing_url"): "url",
    ("providers add", "listing_auth"): "", ("providers add", "listing_shape"): "",
    ("providers edit", "provider_id"): "provider", ("providers approve", "provider_id"): "provider",
    ("providers set-key", "provider_id"): "provider", ("providers set-key", "secret_file"): "file",
    ("providers remove-key", "provider_id"): "provider", ("providers remove-key", "name"): "key-name",
    ("providers sign-in", "provider_id"): "", ("providers sign-out", "provider_id"): "",
    ("providers test", "provider_ids"): "provider", ("providers enable", "provider_id"): "provider",
    ("providers disable", "provider_id"): "provider", ("providers rm", "provider_id"): "provider",
    ("providers transport", "provider_id"): "provider", ("providers transport", "transport_choice"): "",
    ("providers transport", "secret_file"): "file",
    ("discover", "provider"): "provider", ("discover", "discover_add"): "wire", ("discover", "discover_as"): "new-id",
    ("discover", "discover_context"): "n", ("discover", "over_listed"): "text",
    ("update", "update_from_dir"): "dir", ("update", "update_base_url"): "url",
    ("setup", "step"): "", ("setup", "answers"): "file", ("setup", "keys_file"): "path",
    ("setup", "claude_from"): "path", ("setup", "proxy"): "url",
    ("window-ceiling", "ceiling_value"): "ceiling",
    ("explain", "session"): "id", ("explain", "agent"): "agent",
    ("usage", "since"): "when", ("usage", "session"): "id",
    ("plan", "assets"): "path", ("export", "export_out"): "file", ("import", "import_file"): "file",
    ("gateway logs", "lines"): "n", ("gateway logs", "instance"): "nonce",
    ("gateway ensure", "max_wait"): "n", ("gateway ensure", "base_url"): "url",
    ("gateway service install", "name"): "unit",
    ("doctor", "doctor_repair"): "id", ("doctor", "doctor_prune_aliases"): "alias",
}


@functools.lru_cache(maxsize=1)
def _parser() -> argparse.ArgumentParser:
    """The launcher's parser, built once per run (reading it changes nothing)."""

    from claude_multi.cli.parser import build_parser

    return build_parser()


def _matrix():
    from claude_multi import surface_matrix

    return surface_matrix


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:  # noqa: SLF001
    return next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)  # noqa: SLF001


def _node(parser: argparse.ArgumentParser, path: Sequence[str]) -> argparse.ArgumentParser:
    for word in path:
        sub = _subparsers(parser)
        if sub is None or word not in sub.choices:
            raise SystemExit(f"docs_gen: no command {' '.join(path)!r}")
        parser = sub.choices[word]
    return parser


def _shown(parser: argparse.ArgumentParser) -> list[tuple[str, argparse.ArgumentParser, str]]:
    """The sub-commands a parser's help lists: ``(name, parser, summary)``."""

    sub = _subparsers(parser)
    if sub is None:
        return []
    return [(pseudo.dest, sub.choices[pseudo.dest], pseudo.help or "") for pseudo in sub._choices_actions  # noqa: SLF001
            if pseudo.help is not argparse.SUPPRESS]


def _arguments(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    """The arguments a command's help shows, in their order."""

    return [action for action in parser._actions  # noqa: SLF001
            if action.help is not argparse.SUPPRESS
            and not isinstance(action, (argparse._HelpAction, argparse._SubParsersAction))]  # noqa: SLF001


def _value(path: str, action: argparse.Action) -> str:
    key = (path, action.dest)
    if key not in VALUES:
        raise SystemExit(f"docs_gen: {('claude-multi ' + path).strip()} {action.dest}: no placeholder "
                         "(add it to VALUES in tools/docs_gen.py)")
    if VALUES[key]:
        return f"<{VALUES[key]}>"
    if not action.choices:
        raise SystemExit(f"docs_gen: {path} {action.dest}: written as its choices, but it has none")
    return "|".join(str(choice) for choice in action.choices)


def _spell(path: str, action: argparse.Action, option: str | None = None) -> str:
    """One argument as a usage line writes it (an optional value in
    ``[…]``, a repeated one as ``<x>…``)."""

    if not action.option_strings:
        value = _value(path, action)
        if action.nargs == "?":
            return f"[{value}]"
        if action.nargs == "+":
            return f"{value}…"
        if action.nargs == "*":
            return f"[{value}…]"
        return value
    option = option or action.option_strings[0]
    if action.nargs == 0:
        return option
    value = _value(path, action)
    if action.nargs == "?":
        return f"{option} [{value}]"
    if action.nargs == "+":
        return f"{option} {value}…"
    if action.nargs == "*":
        return f"{option} [{value}…]"
    return f"{option} {value}"


def usage(path: Sequence[str], parser: argparse.ArgumentParser, *, passthrough: bool = False) -> str:
    """A command's usage line with typed placeholders: optional parts in
    ``[…]``, a choice among options in ``[a | b]`` (``(a | b)`` when one is
    required), the sub-command as ``<command>``."""

    label = " ".join(path)
    groups = {id(member): group for group in parser._mutually_exclusive_groups  # noqa: SLF001
              for member in group._group_actions}  # noqa: SLF001
    done: set[int] = set()
    parts: list[str] = []
    for action in _arguments(parser):
        group = groups.get(id(action))
        if group is None:
            token = _spell(label, action)
            parts.append(f"[{token}]" if action.option_strings and not action.required else token)
            continue
        if id(group) in done:
            continue
        done.add(id(group))
        members = [_spell(label, member) for member in group._group_actions  # noqa: SLF001
                   if member.help is not argparse.SUPPRESS]
        members = [member[1:-1] if not member.startswith("-") and member.startswith("[") else member
                   for member in members]
        if len(members) == 1:
            parts.append(members[0] if group.required else f"[{members[0]}]")
        else:
            parts.append(("({})" if group.required else "[{}]").format(" | ".join(members)))
    sub = _subparsers(parser)
    if sub is not None and _shown(parser) and path:
        parts.append("<command>" if sub.required else "[<command>]")
    if passthrough:
        parts.append("[-- <claude-args>]")
    return " ".join(["claude-multi", *path, *parts])


_MARKDOWN_TEXT = re.compile(r"([<*])")


def _prose(text: str) -> str:
    """A help text as one table cell: whitespace folded, ``|`` escaped, and
    outside its code spans ``<`` and ``*`` escaped."""

    pieces = re.split(r"(`[^`]*`)", " ".join(text.split()))
    out = [piece if piece.startswith("`") else _MARKDOWN_TEXT.sub(r"\\\1", piece) for piece in pieces]
    return "".join(out).replace("|", "\\|")


def _code(text: str) -> str:
    return "`" + text.replace("|", "\\|") + "`"


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    return (text[:1].upper() + text[1:]).rstrip(".") + "."


def argument_rows(path: Sequence[str], parser: argparse.ArgumentParser) -> list[tuple[str, str]]:
    """``(spellings, what it does)`` for every argument a command's help shows."""

    label = " ".join(path)
    rows = []
    for action in _arguments(parser):
        if action.option_strings:
            spelled = ", ".join(_code(_spell(label, action, option)) for option in action.option_strings)
        else:
            spelled = _code(_spell(label, action))
        what = " ".join(str(action.help or "").split())
        default = action.default
        if (action.option_strings and action.nargs != 0 and default not in (None, False, [], "", argparse.SUPPRESS)
                and "default" not in what):
            what += f" (default {default})"
        rows.append((spelled, _prose(what)))
    return rows


# --------------------------------------------------------- classification

HIDDEN_AUDIENCES = frozenset({"compatibility", "internal"})
PROXY_LISTED = frozenset({"service"})


def path_audiences() -> dict[tuple[str, ...], set[str]]:
    """Command path -> the audiences of the operations spelled at or below it."""

    sm = _matrix()
    audiences: dict[tuple[str, ...], set[str]] = {}
    for op in sm.OPERATIONS:
        for spelling in op.cli:
            path, _flags = sm.split_endpoint(spelling)
            for depth in range(1, len(path) + 1):
                audiences.setdefault(path[:depth], set()).add(op.audience)
    return audiences


def command_kind(path: tuple[str, ...], audiences: Mapping[tuple[str, ...], set[str]]) -> str:
    """How the reference shows a command its parent's help lists: ``listed``
    (its full section) or ``name-only`` (an earlier format's command: its
    summary)."""

    kinds = audiences.get(path, set())
    return "name-only" if path and kinds and kinds <= HIDDEN_AUDIENCES else "listed"


def task_listed(op) -> bool:
    """Whether the task reference has a row for ``op``."""

    return op.audience not in HIDDEN_AUDIENCES | {"service"} and bool(op.cli or op.tui or op.cm)


def task_omission(op) -> str:
    if op.audience in HIDDEN_AUDIENCES:
        return f"{op.audience} operation"
    if op.audience == "service":
        return "operation the gateway service and the launcher run"
    return "maintenance operation of the gateway tool alone"


def classification() -> dict[str, str]:
    """Every command of the parser, every command of the gateway tool and
    every operation, with how the reference treats it: ``listed``,
    ``name-only``, or why it is left out (``hidden: …``, ``counted: …``)."""

    out: dict[str, str] = {}
    audiences = path_audiences()

    def walk(parser: argparse.ArgumentParser, path: tuple[str, ...], shown: bool) -> None:
        label = " ".join(("claude-multi", *path))
        out[label] = command_kind(path, audiences) if shown else "hidden: accepted, not listed by its parent's help"
        sub = _subparsers(parser)
        if sub is None:
            return
        listed = {name for name, _child, _summary in _shown(parser)}
        for name, child in sub.choices.items():
            walk(child, (*path, name), shown and out[label] == "listed" and name in listed)

    walk(_parser(), (), True)
    from claude_multi import proxy

    for command in proxy.PROXY_COMMANDS:
        out[f"claude-multi-proxy {command.name}"] = (
            "listed" if command.audience in PROXY_LISTED
            else f"counted: the gateway tool's {command.audience} command")
    for op in _matrix().OPERATIONS:
        out[f"operation {op.id}"] = "listed" if task_listed(op) else f"counted: {task_omission(op)}"
    return out


# --------------------------------------------------------------- examples

# Operations whose command needs more than its path, its flags and its
# required arguments to read as the task (the flag acts on a resume, the
# step is named, an option the task needs): ``(operation, spelling)`` ->
# the words after ``claude-multi``; each spells its path and its flags.
EXAMPLES: dict[tuple[str, str], str] = {
    ("launch.direct", "direct"): "direct [--model <model>]",
    ("launch.force-resume", "--force"): "-r <id> --force",
    ("launch.force-resume", "direct --force"): "direct -r <id> --force",
    ("setup.redo", "setup --step"): "setup --step <step>",
    ("setup.preflight", "setup --step"): "setup --step preflight",
    ("setup.claude", "setup --step"): "setup --step claude",
    ("setup.claude-from", "setup --claude-from"): "setup --step claude --claude-from <path>",
    ("setup.gateway", "setup --step"): "setup --step gateway",
    ("setup.proxy", "setup --proxy"): "setup --step gateway --proxy <url>",
    ("setup.proxy", "setup --no-proxy"): "setup --step gateway --no-proxy",
    ("setup.providers", "setup --step"): "setup --step providers",
    ("setup.test", "setup --step"): "setup --step test",
    ("setup.profile", "setup --step"): "setup --step profile",
    ("setup.check", "setup --step"): "setup --step check",
    ("providers.add", "providers add"): "providers add <provider> --base-url <url> --secret-ref <secret-ref>",
    ("providers.add", "providers add --preset"): "providers add --preset <preset> [--as <provider>]",
    ("credentials.key-file-input", "providers add --secret-file"):
        "providers add --preset <preset> --secret-file <file>",
    ("credentials.key-file-input", "providers transport --secret-file"):
        "providers transport <provider> api-key --secret-file <file>",
    ("credentials.preset-shared-key", "providers add --reuse-key"): "providers add --preset <preset> --reuse-key",
    ("credentials.preset-shared-key", "providers add --replace-key"):
        "providers add --preset <preset> --replace-key --secret-file <file>",
    ("credentials.sign-in-address", "providers sign-in --no-browser"): "providers sign-in anthropic --no-browser",
    ("profiles.new", "profile new"): "profile new <name> [--from <profile>]",
    ("profiles.new", "profile new --keep-fallback"): "profile new <name> --from <profile> --keep-fallback",
    ("profiles.default", "profile default"): "profile default [<name>]",
    ("settings.ceiling-set", "window-ceiling"): "window-ceiling <ceiling>",
    ("sessions.link", "sessions link"): "sessions link <runtime-id> [--profile <name> | --direct <model>]",
    ("sessions.fork-adopt", "sessions link"): "sessions link <fork-id> --profile <name>",
    ("discovery.listing", "discover"): "discover <provider>",
    ("discovery.declare", "discover --add"): "discover <provider> --add <wire>… [--as <new-id>]",
    ("models.declare-candidate", "discover --add"): "discover <provider> --add <wire> [--as <new-id>]",
    ("discovery.context-override", "discover --over-listed"):
        "discover <provider> --add <wire> --context <n> --over-listed <text>",
    ("lineup.relaunch", "lineup --relaunch"): "lineup --session <runtime-id> --relaunch <request>",
    ("lineup.relaunch", "lineup --its-exited"): "lineup --session <runtime-id> --relaunch --its-exited <request>",
    ("diagnostics.explain", "explain"): "explain [<id>]",
}


def _required(path: tuple[str, ...], flags: Sequence[str]) -> list[str]:
    """The arguments a command takes besides ``flags``: its positionals
    (an optional one as ``[<x>]``, unless it is one side of a choice), the
    first of a required choice none of ``flags`` makes, and its required
    options."""

    parser = _node(_parser(), path)
    label = " ".join(path)
    owners = {option: action for action in parser._actions for option in action.option_strings}  # noqa: SLF001
    spelled = {id(owners[flag]) for flag in flags if flag in owners}
    groups = {id(member): group for group in parser._mutually_exclusive_groups  # noqa: SLF001
              for member in group._group_actions}  # noqa: SLF001
    words: list[str] = []
    for action in _arguments(parser):
        group = groups.get(id(action))
        if group is not None:
            members = group._group_actions  # noqa: SLF001
            if group.required and action is members[0] and not any(id(member) in spelled for member in members):
                words.append(_spell(label, action).strip("[]"))
        elif not action.option_strings:
            words.append(_spell(label, action))
        elif action.option_strings and action.required and id(action) not in spelled:
            words.append(_spell(label, action))
    return words


def example(op, spelling: str) -> str:
    """The command a matrix spelling stands for, as a reader runs it."""

    sm = _matrix()
    path, flags = sm.split_endpoint(spelling)
    if (op.id, spelling) in EXAMPLES:
        text = EXAMPLES[(op.id, spelling)]
        words = text.split()
        if tuple(words[:len(path)]) != path or not set(flags) <= set(words):
            raise SystemExit(f"docs_gen: the example {text!r} does not spell {spelling!r}")
        return f"claude-multi {text}"
    if path == ("lineup",):
        return " ".join(["claude-multi lineup --session <runtime-id>", *flags, "<request>"])
    parser = _node(_parser(), path)
    owners = {option: action for action in parser._actions for option in action.option_strings}  # noqa: SLF001
    label = " ".join(path)
    words = ["claude-multi", *path, *_required(path, flags)]
    for flag in flags:
        words += ["--", "<claude-args>"] if flag == sm.PASSTHROUGH else [_spell(label, owners[flag], flag)]
    return " ".join(words)


def cm_spelled(name: str) -> str:
    from claude_multi import lineup_files

    verb = next(verb for verb in lineup_files.CM_VERBS if verb.name == name)
    return f"/cm {verb.spelled()}".rstrip()


def _lineup_request(name: str) -> str:
    return f"claude-multi lineup --session <runtime-id> {cm_spelled(name).removeprefix('/cm ')}"


def cli_commands(op, spellings: Sequence[str] | None = None, *, verbs: int | None = None) -> list[str]:
    """The commands of an operation (of ``spellings`` only, when given): a
    bare lineup spelling once per ``/cm`` verb (the first ``verbs`` of
    them), every other spelling through :func:`example`."""

    sm = _matrix()
    commands: list[str] = []
    for spelling in op.cli if spellings is None else spellings:
        path, flags = sm.split_endpoint(spelling)
        if path == ("lineup",) and not flags and op.cm:
            commands += [_lineup_request(verb) for verb in op.cm[:verbs]]
        else:
            commands.append(example(op, spelling))
    return list(dict.fromkeys(commands))


# ------------------------------------------------------------- task cells

NOT_IN_SESSION = "not a `/cm` request"
TASK_HEADER = ("| Task | TUI | CLI | In-session | Notes |", "| --- | --- | --- | --- | --- |")


def tui_cell(op) -> str:
    if not op.tui:
        return "not in the launcher: " + _prose(op.absent.get("tui", "no screen offers it"))
    paths: list[str] = []
    for mapping in op.tui:
        text = mapping.path if mapping.available == "always" else f"{mapping.path}: {mapping.available}"
        if text not in paths:
            paths.append(text)
    return _prose("; ".join(paths))


def cli_cell(op) -> str:
    if not op.cli:
        return "no command: " + _prose(op.absent.get("cli", "no command performs it"))
    return "; ".join(_code(command) for command in cli_commands(op))


def cm_cell(op) -> str:
    return "; ".join(_code(cm_spelled(verb)) for verb in op.cm) if op.cm else NOT_IN_SESSION


def notes_cell(op) -> str:
    notes = []
    if op.audience == "scripting":
        notes.append("for scripts")
    elif op.audience == "maintenance":
        notes.append("maintenance")
    notes.append(f"asks first: {op.confirm}" if op.confirm else "asks nothing")
    return _prose("; ".join(notes))


def task_rows(ops: Iterable) -> list[str]:
    return [f"| {_prose(op.does)} | {tui_cell(op)} | {cli_cell(op)} | {cm_cell(op)} | {notes_cell(op)} |"
            for op in ops]


def short_paths(paths: Sequence[str]) -> str:
    """TUI paths from the card, shortened and merged: ``card → P → N`` and
    ``card → P → C`` read ``P → N, C``; a path that continues another one
    listed is left to it."""

    paths = [path.removeprefix("card → ") for path in paths]
    paths = [path for path in paths
             if not any(path != other and path.startswith((other + " ", other + " → ")) for other in paths)]
    order: list[str] = []
    tails: dict[str, list[str]] = {}
    for path in paths:
        head, _sep, tail = path.rpartition(" → ")
        key = head or path
        if key not in tails:
            order.append(key)
            tails[key] = []
        if head and tail not in tails[key]:
            tails[key].append(tail)
    return "; ".join(f"{key} → {', '.join(tails[key])}" if tails[key] else key for key in order)


# ------------------------------------------------- the command-line reference

def region_cli_reference() -> str:
    from claude_multi.cli import parser as cli_parser

    parser = _parser()
    audiences = path_audiences()
    lines = ["## Launch", "", "```text", usage((), parser, passthrough=True), "claude-multi <command>", "```", "",
             "| Argument | What it does |", "| --- | --- |"]
    lines += [f"| {spelled} | {what} |" for spelled, what in argument_rows((), parser)]
    lines += ["", "Examples:", "", "| Command | What it does |", "| --- | --- |"]
    typed = {"NAME": "<name>", "SESSION": "<id>", "MODEL": "<model>", "ARGS": "<claude-args>"}
    for command, what in cli_parser.HELP_EXAMPLES:
        words = command.split()
        what = " ".join(_code(typed[word]) if word in typed and word in words else word for word in what.split())
        lines.append(f"| {_code(' '.join(typed.get(word, word) for word in words))} | {_prose(what)} |")
    for title, rows in cli_parser.COMMAND_GROUPS:
        if title != "Launch":
            lines += ["", f"## {title}"]
        for name, summary in rows:
            lines += _command_section((name,), _node(parser, (name,)), summary, audiences)
    lines += ["", "## The gateway tool", "", region_proxy(), "", "## Inside a session", "", region_cm()]
    return "\n".join(lines)


def _command_section(path: tuple[str, ...], parser: argparse.ArgumentParser, summary: str,
                     audiences: Mapping[tuple[str, ...], set[str]]) -> list[str]:
    label = " ".join(path)
    about = parser.description if parser.description and command_kind(path, audiences) == "listed" else summary
    lines = ["", f"{'#' * min(len(path) + 2, 6)} claude-multi {label}", "", _prose(_sentence(about))]
    if command_kind(path, audiences) == "name-only":
        lines += ["", "It reads or converts an earlier format; its own `--help` lists its options."]
        return lines
    lines += ["", "```text", usage(path, parser, passthrough=path == ("direct",)), "```"]
    rows = argument_rows(path, parser)
    if rows:
        lines += ["", "| Argument | What it does |", "| --- | --- |"]
        lines += [f"| {spelled} | {what} |" for spelled, what in rows]
    if path == ("lineup",):
        lines += ["", "The requests are `/cm`'s ([inside a session](#inside-a-session)); inside a session the "
                  "runtime id comes from the environment."]
    for name, child, child_summary in _shown(parser):
        lines += _command_section((*path, name), child, child_summary, audiences)
    return lines


_PROXY_WORDS = {"/abs": "<root>", "NONCE": "<nonce>", "NAME": "<name>"}


def region_proxy() -> str:
    from claude_multi import proxy

    listed = [command for command in proxy.PROXY_COMMANDS if command.audience in PROXY_LISTED]
    counted = [command for command in proxy.PROXY_COMMANDS if command.audience not in PROXY_LISTED]
    lines = ["`claude-multi-proxy` is the local gateway's own tool. The launcher and the gateway service run "
             "it; a doctor finding names it when it is the fix.", "",
             "| Command | Arguments | What it does |", "| --- | --- | --- |"]
    for command in listed:
        synopsis = " ".join(_PROXY_WORDS.get(word, word) for word in command.synopsis.split())
        lines.append(f"| {_code('claude-multi-proxy ' + command.name)} | {_code(synopsis) if synopsis else 'none'} "
                     f"| {_prose(command.description.splitlines()[0])} |")
    lines += ["", f"`claude-multi-proxy --help` lists its {len(counted)} maintenance commands too, and the exit "
              "statuses of every command."]
    return "\n".join(lines)


def region_cm() -> str:
    from claude_multi import lineup_files

    lines = ["`/cm` shows or changes the session's lineup. A change applies after you type `/reload-plugins`, "
             "or at the next resume when it needs a relaunch.", "",
             "| Request | What it does | Changes the lineup |", "| --- | --- | --- |"]
    lines += [f"| {_code(cm_spelled(verb.name))} | {_prose(verb.summary)} | {'yes' if verb.writes else 'no'} |"
              for verb in lineup_files.CM_VERBS]
    lines += ["", "`/cm` passes everything after it as one request; "
              "`claude-multi lineup --session <runtime-id> <request>` runs the same request from a terminal."]
    return "\n".join(lines)


# ------------------------------------------------------------- task tables

# The page that explains each object's tasks in full, from reference/.
OBJECT_PAGES: dict[str, tuple[str, str]] = {
    "launch": ("Launch", "../quickstart.md"), "setup": ("Setup", "../quickstart.md"),
    "install": ("Install, update and uninstall", "../update.md"),
    "gateway": ("The local gateway", "../guides/gateway.md"),
    "gateway-service": ("The gateway service", "../guides/gateway.md"),
    "providers": ("Providers", "../providers/api-keys.md"),
    "credentials": ("Keys and sign-ins", "../providers/api-keys.md"),
    "models": ("Models", "../guides/models.md"), "discovery": ("Model listings", "../guides/models.md"),
    "profiles": ("Profiles", "../guides/profiles.md"), "sessions": ("Sessions", "../guides/sessions.md"),
    "lineup": ("Lineups and pending changes", "../guides/lineup.md"),
    "diagnostics": ("Diagnostics", "../troubleshooting.md"), "settings": ("Settings", "settings.md"),
    "bindings": ("Named bindings", "../guides/profiles.md"),
    "portability": ("Another computer", "../guides/move-machines.md"),
    "maintenance": ("Maintenance", "../troubleshooting.md"), "compatibility": ("Earlier formats", "cli.md"),
    "review": ("Reviews", "../guides/lineup.md"), "interface": ("The interface", "cli.md"),
}


def region_tasks() -> str:
    sm = _matrix()
    from claude_multi.cli.screens import actions

    lines = ["## The launcher's screens", "", "| Key on the card | What it does |", "| --- | --- |"]
    lines += [f"| {_prose(row.key)} | {_prose(row.does)} |" for row in actions.ACTIONS["card"]]
    for obj in sm.OBJECTS:
        ops = [op for op in sm.OPERATIONS if op.object == obj and task_listed(op)]
        if not ops:
            continue
        if obj not in OBJECT_PAGES:
            raise SystemExit(f"docs_gen: the object {obj!r} has no page in OBJECT_PAGES")
        title, page = OBJECT_PAGES[obj]
        lines += ["", f"## {title}", "", f"In full: [{page.removeprefix('../')}]({page}).", "",
                  *TASK_HEADER, *task_rows(ops)]
    left: dict[str, int] = {}
    for op in sm.OPERATIONS:
        if not task_listed(op):
            left[task_omission(op)] = left.get(task_omission(op), 0) + 1
    lines += ["", "Not listed here: " + "; ".join(
        f"{count} {reason.replace('operation', 'operations', 1) if count != 1 else reason}"
        for reason, count in sorted(left.items())) + "."]
    return "\n".join(lines)


def region_lineup_tasks() -> str:
    sm = _matrix()
    return "\n".join([*TASK_HEADER, *task_rows(op for op in sm.OPERATIONS
                                               if op.object == "lineup" and task_listed(op))])


@dataclass(frozen=True)
class CheatRow:
    """One cheatsheet row: the task as a reader says it, its operations, the
    matrix spellings to show (default: every one), its ``/cm`` verbs
    (default: every one) and a note."""

    task: str
    ops: tuple[str, ...]
    note: str
    cli: tuple[str, ...] | None = None
    cm: tuple[str, ...] | None = None


CHEAT_ROWS: tuple[CheatRow, ...] = (
    CheatRow("get started", ("setup.status", "setup.run"), "the steps not done yet, in order"),
    CheatRow("launch", ("launch.fresh", "launch.choose-profile"), "the card shows the profile it launches"),
    CheatRow("launch one model as the lead", ("launch.direct",), "no `cm-*` agents"),
    CheatRow("continue, resume", ("launch.continue", "launch.resume"), "resume keeps the lineup", ("-c", "-r")),
    CheatRow("see a launch without launching", ("launch.print-launch",), "the gateway key is never shown"),
    CheatRow("connect a provider", ("credentials.set-key", "credentials.sign-in"),
             "[providers](providers/api-keys.md)", ("providers set-key", "providers sign-in")),
    CheatRow("test a provider", ("credentials.test",), "one request each, after you agree; may be billed"),
    CheatRow("profiles", ("profiles.list", "profiles.show", "lineup.profiles"),
             "P is the Profiles screen, not provider fallback", ("profile list", "profile show")),
    CheatRow("fallback profiles for an outage", ("profiles.fallback-filter", "lineup.fallback"), "one provider each"),
    CheatRow("make a profile the default", ("profiles.default", "profiles.default-clear"),
             "otherwise chosen automatically from what is connected"),
    CheatRow("a starter from what is connected", ("profiles.starter",), "previews before saving"),
    CheatRow("create, copy, edit, rename", ("profiles.new", "profiles.copy", "profiles.edit", "profiles.rename"),
             "saving offers the change to following sessions",
             ("profile new", "profile duplicate", "profile edit", "profile rename")),
    CheatRow("remove, restore a shipped profile", ("profiles.remove", "profiles.reseed"),
             "your version is kept as a backup", ("profile rm", "profile reseed")),
    CheatRow("named bindings", ("bindings.list",), "a binding names a model and effort profiles share"),
    CheatRow("change the lineup", ("lineup.show", "lineup.set", "lineup.profile"), "then `/reload-plugins`"),
    CheatRow("follow or pin", ("lineup.pin", "lineup.follow"), "a following session takes its profile's edits"),
    CheatRow("models", ("models.list", "models.show"), "lines, selectors, retired keys", ("models", "models show")),
    CheatRow("add a model of your own", ("discovery.listing", "models.admit", "models.qualify"),
             "[guides/models.md](guides/models.md)", ("discover", "models admit", "models qualify --agents")),
    CheatRow("context window ceiling", ("settings.ceiling-show", "settings.ceiling-set", "settings.ceiling-reset",
                                        "settings.role-windows"), "applies at the next launch or resume"),
    CheatRow("settings", ("settings.inspect",), "[reference/settings.md](reference/settings.md)"),
    CheatRow("sessions", ("sessions.list", "sessions.show"), "records, never transcripts",
             ("sessions list", "sessions show")),
    CheatRow("stop, mark ended, forget", ("sessions.stop", "sessions.mark-ended", "sessions.forget"),
             "transcripts are never touched", ("sessions stop", "sessions mark-ended", "sessions forget")),
    CheatRow("forks and adoption", ("sessions.fork-adopt", "sessions.fork-discard"),
             "the fork transcript is kept either way", ("sessions link", "sessions resolve-fork")),
    CheatRow("health", ("diagnostics.doctor", "diagnostics.first-run", "sessions.repair"),
             "Ready / Attention / BLOCKED", ("doctor", "doctor --first-run", "doctor --repair")),
    CheatRow("quota", ("diagnostics.quota",), "an observation, not a balance", ("quota",)),
    CheatRow("review by another model family", ("review.request",), "report only"),
    CheatRow("the gateway", ("gateway.status", "gateway.start", "gateway.restart", "gateway.logs"),
             "[guides/gateway.md](guides/gateway.md)"),
    CheatRow("the gateway as a user service (Linux)", ("gateway-service.install", "gateway-service.status"),
             "unit `claude-multi-gateway`"),
    CheatRow("update", ("install.update-check", "install.update"), "shows its plan, then asks",
             ("update --check", "update")),
    CheatRow("roll back, uninstall", ("install.rollback", "install.uninstall-preview"),
             "[update.md](update.md), [uninstall.md](uninstall.md)"),
    CheatRow("export, import", ("portability.export", "portability.import-preview"), "no keys travel",
             ("export --out", "import")),
)


def cheat_problems(rows: Sequence[CheatRow] = CHEAT_ROWS) -> list[str]:
    """Rows naming an operation the inventory lacks, or a spelling or verb
    none of the row's operations has."""

    ops = _matrix().rows()
    problems = []
    for row in rows:
        missing = [op_id for op_id in row.ops if op_id not in ops]
        problems += [f"{row.task}: no operation {op_id}" for op_id in missing]
        if missing:
            continue
        spellings = {spelling for op_id in row.ops for spelling in ops[op_id].cli}
        problems += [f"{row.task}: {spelling!r} is not a spelling of its operations"
                     for spelling in row.cli or () if spelling not in spellings]
        verbs = {verb for op_id in row.ops for verb in ops[op_id].cm}
        problems += [f"{row.task}: /cm {verb} is not a verb of its operations"
                     for verb in row.cm or () if verb not in verbs]
    return problems


def _cheat_cli(row: CheatRow, ops: Sequence) -> str:
    commands: list[str] = []
    for spelling in row.cli or ():
        op = next(op for op in ops if spelling in op.cli)
        commands += cli_commands(op, [spelling], verbs=1)
    if row.cli is None:
        for op in ops:
            commands += cli_commands(op, verbs=1)
    commands = list(dict.fromkeys(commands))
    if commands:
        return "; ".join(_code(command) for command in commands)
    reasons = [op.absent["cli"] for op in ops if "cli" in op.absent]
    return "no command: " + _prose(reasons[0] if reasons else "no command performs it")


def region_cheatsheet_tasks() -> str:
    problems = cheat_problems()
    if problems:
        raise SystemExit("docs_gen: " + "; ".join(problems))
    rows = _matrix().rows()
    lines = ["| Task | TUI (from the card) | CLI | In-session | Notes |", "| --- | --- | --- | --- | --- |"]
    for row in CHEAT_ROWS:
        ops = [rows[op_id] for op_id in row.ops]
        paths = [op.tui[0].path for op in ops if op.tui]
        if paths:
            tui = _prose(short_paths(paths))
        else:
            reasons = [op.absent["tui"] for op in ops if "tui" in op.absent]
            tui = "not in the launcher: " + _prose(reasons[0] if reasons else "no screen offers it")
        verbs = row.cm if row.cm is not None else tuple(dict.fromkeys(verb for op in ops for verb in op.cm))
        cm = "; ".join(_code(cm_spelled(verb)) for verb in verbs) if verbs else NOT_IN_SESSION
        lines.append(f"| {row.task} | {tui} | {_cheat_cli(row, ops)} | {cm} | {row.note} |")
    return "\n".join(lines)


SCREEN_TITLES: dict[str, str] = {
    "card": "launch card (`claude-multi`)", "resume-card": "resume card (`claude-multi -r <id>`)",
    "sessions": "sessions (S)", "lineup-dialog": "lineup dialog (S → T)", "direct": "direct (D)",
    "providers": "providers (G)", "models": "models (M)", "settings": "settings (O)", "profiles": "profiles (P)",
    "get-started": "get started (W)", "editor": "profile editor (E)",
    "binding-picker": "binding picker (editor → Enter on an agent)", "named-bindings": "named bindings (P → B)",
}


def region_cheatsheet_keys() -> str:
    from claude_multi.cli import text
    from claude_multi.cli.screens import actions

    lines = ["| Screen | Keys |", "| --- | --- |"]
    for screen, rows in actions.ACTIONS.items():
        if screen not in SCREEN_TITLES:
            raise SystemExit(f"docs_gen: the screen {screen!r} has no title in SCREEN_TITLES")
        keys = " · ".join(f"**{_prose(row.key)}** {_prose(row.does)}" for row in rows)
        lines.append(f"| {SCREEN_TITLES[screen]} | {keys} |")
    marks = " ".join(text.SESSIONS_HELP.split("Marks: ", 1)[1].split("\n")[:2]).strip()
    override = text.SETTINGS_TITLE.split("(", 1)[1].rstrip(")")
    lines += ["", f"Marks on the sessions screen: {_prose(marks)} On the settings screen: {_prose(override)}."]
    return "\n".join(lines)


# --------------------------------------------------------------- the catalog

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")


def _catalog():
    import claude_multi
    from claude_multi import catalog

    return catalog.load_catalog(claude_multi.resources_root())


def _tokens(n: int) -> str:
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000}M"
    if n % 1000 == 0:
        return f"{n // 1000}K"
    return f"{n:,}"


def _efforts(entry: Mapping) -> list[str]:
    return sorted(entry["efforts"], key=lambda effort: (EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER
                                                         else len(EFFORT_ORDER), effort))


def _selectors(entry: Mapping) -> str:
    """A line's typed ``/model`` selectors: one (the effort is chosen in
    Claude Code), or one per effort written as their common pattern."""

    from claude_multi import catalog

    found = catalog.line_selectors(dict(entry))
    if len(found) == 1:
        return f"{_code(found[0][1])} (the effort is chosen in Claude Code)"
    patterns = set()
    for effort, selector, _contract in found:
        head, sep, tail = selector.rpartition(f"-{effort}")
        patterns.add(f"{head}-<effort>{tail}" if sep else selector)
    if len(patterns) == 1:
        return _code(patterns.pop())
    return ", ".join(_code(selector) for _effort, selector, _contract in found)


def _window(entry: Mapping) -> str:
    context = entry["context"]
    client = context.get("client_tokens") or 0
    provider = context.get("provider_tokens") or context.get("declared_tokens") or 0
    kind = "1M class" if client >= 1_000_000 else "200K class"
    return f"{kind}, provider window {_tokens(provider)}" if provider else kind


def _active(lines: Mapping, provider: str | None = None) -> list[str]:
    return sorted(key for key, entry in lines.items() if entry.get("status") == "active"
                  and (provider is None or entry["provider"] == provider))


def region_model_lines() -> str:
    from claude_multi import catalog

    cat = _catalog()
    lines = ["| Key | Model | Provider | Family | Efforts (default) | Context | Roles | `/model` selectors |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for key in _active(cat.lines):
        entry = cat.lines[key]
        roles = " and ".join(entry.get("capabilities", ())) or "none"
        lines.append(f"| `{key}` | {_prose(entry['display'])} | {cat.providers[entry['provider']]['display']} | "
                     f"{catalog.line_family(dict(entry), cat.providers)} | {', '.join(_efforts(entry))} "
                     f"({entry['default_effort']}) | {_window(entry)} | {roles} | {_selectors(entry)} |")
    lines += ["", f"{len(cat.retired)} retired keys resolve to their successors; `claude-multi models` lists them."]
    return "\n".join(lines)


def _needs(cat, profile: Mapping) -> list[str]:
    models = [profile["lead"]["model"], *(slot["model"] for slot in profile.get("agents", {}).values())]
    providers = dict.fromkeys(cat.lines[model]["provider"] for model in models if model in cat.lines)
    return [cat.providers[pid]["display"] for pid in providers]


def region_shipped_profiles() -> str:
    cat = _catalog()
    lines = ["| Profile | Lead | Agents bound | Needs | What it is |", "| --- | --- | --- | --- | --- |"]
    for name in sorted(cat.seed_profiles):
        profile = cat.seed_profiles[name]
        agents = len(profile.get("agents", {}))
        lines.append(f"| `{name}` | `{profile['lead']['model']}` {profile['lead']['effort']} | {agents or 'none'} | "
                     f"{', '.join(_needs(cat, profile))} | {_prose(profile['description'])} |")
    return "\n".join(lines)


def region_openrouter_lines() -> str:
    from claude_multi import catalog

    cat = _catalog()
    lines = ["| Key | Model | OpenRouter model | Family | Efforts | Context |", "| --- | --- | --- | --- | --- | --- |"]
    for key in _active(cat.lines, "openrouter"):
        entry = cat.lines[key]
        lines.append(f"| `{key}` | {_prose(entry['display'])} | `{entry['wire_model']}` | "
                     f"{catalog.line_family(dict(entry), cat.providers)} | {', '.join(_efforts(entry))} | "
                     f"{_window(entry)} |")
    profile = cat.seed_profiles.get("openrouter")
    if profile is not None:
        lines += ["", f"The shipped `openrouter` profile: {_prose(_sentence(profile['description']))}", "",
                  "| Role | Line | Effort |", "| --- | --- | --- |",
                  f"| lead | `{profile['lead']['model']}` | {profile['lead']['effort']} |"]
        lines += [f"| `{role}` | `{slot['model']}` | {slot['effort']} |"
                  for role, slot in sorted(profile.get("agents", {}).items())]
    return "\n".join(lines)


def region_provider_index() -> str:
    import json

    import claude_multi
    from claude_multi import catalog, operator

    cat = _catalog()
    gateway = json.loads((claude_multi.resources_root() / "catalog" / "gateway.json").read_text(encoding="utf-8"))
    alone = {profile["primary_provider"]: name for name, profile in sorted(cat.seed_profiles.items())
             if profile.get("primary_provider")}

    def by_itself(pid: str) -> str:
        return f"the shipped `{alone[pid]}` profile" if pid in alone else "a starter (`claude-multi profile starter`)"

    rows: list[tuple[str, str]] = []
    for pid, provider in cat.providers.items():
        ref = (provider["transport"].get("auth") or {}).get("secret_ref")
        if ref:
            page = " ([its own page](openrouter.md))" if pid == "openrouter" else ""
            rows.append((provider["display"], f"| {provider['display']} | `{ref.removeprefix('env:')}` | "
                                              f"`{provider['transport']['base_url']}`{page} | {by_itself(pid)} |"))
    for (pid, _choice), alternative in operator.TRANSPORT_ALTERNATIVES.items():
        if alternative.available and pid in cat.providers:
            display = cat.providers[pid]["display"]
            rows.append((display, f"| {display} ([API key](#{pid})) | `{alternative.secret_name}` | "
                                  f"`{alternative.base_url}` | {by_itself(pid)} |"))
    lines = ["| Provider | Key name | Address the key is sent to | For this provider alone |",
             "| --- | --- | --- | --- |", *(row for _name, row in sorted(rows, key=lambda item: item[0].lower()))]
    open_ = catalog.keyed_compat_audited(gateway)
    pages = {"anthropic-compatible": "anthropic-compatible.md", operator.KEYED_KIND: "openai-compatible.md",
             operator.LAN_KIND: "lan.md"}
    lines += ["", "Presets are vendors' documented endpoints, filled in for you "
              "(`claude-multi providers add --preset <preset>`, or **G** → **N** and **W** → **A** in the launcher):", "",
              "| Preset | Route | Key name | Address | Support | Available |",
              "| --- | --- | --- | --- | --- | --- |"]
    presets = operator.presets(claude_multi.resources_root())
    for name, info in sorted(presets.items(), key=lambda item: (list(pages).index(item[1].kind), item[0])):
        available = ("no: the route is closed in this release" if info.kind == operator.KEYED_KIND and not open_
                     else "yes")
        key = f"`{info.secret_name}`" if info.secret_name else "none"
        address = "your server's address" if info.generic else f"`{info.base_url}`"
        lines.append(f"| `{name}` ({_prose(info.display)}) | [{info.kind}]({pages[info.kind]}#presets) | {key} | "
                     f"{address} | `{info.support}` | {available} |")
    return "\n".join(lines)


# ------------------------------------------------------------------- pages

REGIONS: dict[str, tuple[str, Callable[[], str]]] = {
    "cli-reference": ("reference/cli.md", region_cli_reference),
    "tasks": ("reference/tasks.md", region_tasks),
    "cheatsheet-tasks": ("CHEATSHEET.md", region_cheatsheet_tasks),
    "cheatsheet-keys": ("CHEATSHEET.md", region_cheatsheet_keys),
    "lineup-tasks": ("guides/lineup.md", region_lineup_tasks),
    "model-lines": ("guides/models.md", region_model_lines),
    "shipped-profiles": ("guides/profiles.md", region_shipped_profiles),
    "openrouter-lines": ("providers/openrouter.md", region_openrouter_lines),
    "provider-index": ("providers/api-keys.md", region_provider_index),
}


def render(text: str, name: str, body: str) -> str:
    """``text`` with region ``name``'s body replaced; the marker lines stay."""

    begin, end = BEGIN.format(name=name), END.format(name=name)
    if text.count(begin) != 1 or text.count(end) != 1 or text.index(begin) > text.index(end):
        raise SystemExit(f"docs_gen: a page needs exactly one {begin} … {end} pair")
    head, rest = text.split(begin, 1)
    _old, tail = rest.split(end, 1)
    return f"{head}{begin}\n\n{body.rstrip()}\n\n{end}{tail}"


def generate(docs: Path = DOCS) -> dict[Path, str]:
    """Every page that holds a region, as it should read."""

    pages: dict[Path, str] = {}
    for name, (page, build) in REGIONS.items():
        path = docs / page
        pages[path] = render(pages.get(path) or path.read_text(encoding="utf-8"), name, build())
    return pages


def stale(docs: Path = DOCS) -> list[str]:
    """``page: region`` for every region whose committed text differs, and
    for every region marker no generator owns."""

    problems = []
    for name, (page, build) in REGIONS.items():
        text = (docs / page).read_text(encoding="utf-8")
        if render(text, name, build()) != text:
            problems.append(f"{page}: {name}")
    for path in sorted(docs.rglob("*.md")):
        page = path.relative_to(docs).as_posix()
        for name in REGION.findall(path.read_text(encoding="utf-8")):
            if name not in REGIONS or REGIONS[name][0] != page:
                problems.append(f"{page}: {name} has no generator")
    return problems


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docs_gen.py", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when a generated region is stale")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.check:
        problems = stale()
        for problem in problems:
            print(f"stale: {problem} (python3 tools/docs_gen.py rewrites it)")
        print(f"docs_gen: {len(REGIONS)} regions, {len(problems)} stale")
        return 1 if problems else 0
    for path, text in generate().items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(REPO).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
