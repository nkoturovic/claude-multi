"""The ``doctor`` command: action selection and report rendering.

The report's headline is the one health reduction of the JSON report
(``observations.status_of``): BLOCKED, else Attention, else Ready. Blocking
reasons and attention items are always shown; routine detail lines (one per
session, the gateway's counters, the evidence notes) only with ``-v``.
Exit statuses: 0 ready or attention, 1 blocked, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from typing import Any, TextIO

from claude_multi import observations
from claude_multi import sessions
from claude_multi import strict_json
from claude_multi import termtext
import claude_multi.cli.doctor as doctor
import claude_multi.cli.doctor_actions as doctor_actions
import claude_multi.cli.session_facts as session_facts
import claude_multi.cli.types as cli_types
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def _doctor_command(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
    no_color: bool,
    terminal_stream: TextIO | None = None,
) -> int:
    """The ``doctor`` command: an action, the first-run checks, or the report.
    Token rotation asks on ``terminal_stream`` (the terminal; default
    ``output_stream``) and reports its result to ``output_stream`` and a
    pause to stderr."""

    if getattr(args, "doctor_first_run", False):
        others = [flag for flag, on in (("--repair", args.doctor_repair is not None),
                                        ("--repair-all", args.doctor_repair_all),
                                        ("--prune", args.doctor_prune),
                                        ("--rotate-token", args.doctor_rotate_token),
                                        ("--prune-aliases", args.doctor_prune_aliases is not None),
                                        ("--preview", getattr(args, "preview", False))) if on]
        if others:
            sys.stderr.write(f"claude-multi doctor: --first-run cannot accompany {', '.join(others)}\n")
            return 2
        return _first_run(runtime, json_output=bool(getattr(args, "json", False)), output_stream=output_stream)
    if getattr(args, "preview", False):
        if not args.doctor_prune:
            raise cli_types.UsageError("--preview is valid only with --prune")
        return doctor_actions._doctor_prune_preview(runtime, output_stream)
    if args.doctor_rotate_token:
        return doctor_actions._doctor_rotate_token(
            runtime,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
            terminal_stream=terminal_stream,
            error_stream=sys.stderr,
        )
    if args.doctor_repair is not None:
        target = sessions.managed_id(session_facts.resolve_session(runtime, args.doctor_repair))
        return doctor_actions._doctor_repair(runtime, target, output_stream)
    if getattr(args, "doctor_include_live", False) and not args.doctor_repair_all:
        raise cli_types.UsageError("--include-live is valid only with --repair-all")
    if args.doctor_repair_all:
        return doctor_actions._doctor_repair_all(
            runtime,
            output_stream,
            include_live=bool(getattr(args, "doctor_include_live", False)),
        )
    if args.doctor_prune:
        return doctor_actions._doctor_prune(runtime, output_stream)
    if args.doctor_prune_aliases is not None:
        return doctor_actions._doctor_prune_aliases(
            runtime, args.doctor_prune_aliases, output_stream
        )
    with runtime.report_snapshot():
        result = doctor._collect_doctor_reports(runtime)
        report = doctor.doctor_report(runtime, result)
    if getattr(args, "json", False):
        document = report.document()
        document["environment"] = environment_block(runtime)
        output_stream.write(json.dumps(document, ensure_ascii=True, allow_nan=False, indent=2) + "\n")
        return 1 if report.status == "blocked" else 0
    problems, info_lines, scope_attention = result
    verbose = bool(getattr(args, "doctor_verbose", False))
    # Badge styling only when color is active (a tty, not NO_COLOR, not
    # --no-color); the line contract itself never changes.
    from claude_multi.cli.screens.common import _output_palette

    palette = _output_palette(input_stream, output_stream, no_color)
    status = observations.status_of(["block"] * bool(problems) + ["attention"] * bool(scope_attention))
    shown = info_lines if verbose else [line for line in info_lines if not doctor.detail_line(line)]
    if problems:
        output_stream.write(palette.ansi("BLOCKED", "error") + "\n")
        for problem in problems:
            output_stream.write(palette.ansi(f"  - {termtext.visible_text(problem)}", "error") + "\n")
    if scope_attention:
        output_stream.write(palette.ansi("Attention", "warn") + "\n")
        for line in scope_attention:
            output_stream.write(palette.ansi(f"  - {termtext.visible_text(line)}", "warn") + "\n")
    if status == "ready":
        output_stream.write(palette.ansi("Ready", "ok") + "\n")
    for line in shown:
        output_stream.write(f"{termtext.visible_text(line)}\n")
    hidden = len(info_lines) - len(shown)
    if hidden:
        output_stream.write(f"({hidden} more detail line{'s' if hidden != 1 else ''}: claude-multi doctor -v)\n")
    output_stream.write(doctor.DOCTOR_SUMMARY[status])
    return 1 if status == "blocked" else 0


# Terminal types named as they are; any other value is "other", so a report
# never carries an arbitrary environment value.
_KNOWN_TERMS = frozenset({
    "dumb", "linux", "vt100", "xterm", "xterm-256color", "xterm-kitty", "screen", "screen-256color", "tmux",
    "tmux-256color", "alacritty", "foot", "rxvt-unicode", "rxvt-unicode-256color", "wezterm", "ghostty",
    "xterm-ghostty",
})


def environment_block(runtime: runtime_mod.Runtime) -> dict[str, Any]:
    """The ``environment`` block of ``doctor --json``: allowlisted facts only
    (platform, Python, terminal mode, install channel, release identity,
    whether the roots exist). No path, user name, host, proxy setting or
    other environment value is included."""

    from claude_multi import endpoint, identity, paths
    from claude_multi.platform import mounts

    environ = runtime.environ
    term = environ.get("TERM")

    def tty(stream: Any) -> bool:
        try:
            return bool(stream is not None and stream.isatty())
        except (AttributeError, ValueError, OSError):
            return False

    def present(path: Any) -> bool:
        try:
            return os.path.lexists(path)
        except (OSError, ValueError):
            return False

    try:
        wsl = mounts.wsl(environ, proc_root=runtime.proc_root) is not None
    except OSError:
        wsl = None
    return {
        "schema": 1,
        "platform": sys.platform,
        "machine": platform.machine() or None,
        "wsl": wsl,
        "python": platform.python_version(),
        "terminal": {
            "stdin": tty(sys.stdin),
            "stdout": tty(sys.stdout),
            "term": None if not term else term if term in _KNOWN_TERMS else "other",
            "no_color": bool(environ.get("NO_COLOR")),
        },
        "channel": endpoint.channel(environ),
        "release": identity.collect().document(),
        "roots": {
            "config": present(sessions.config_root(environ)),
            "state": present(runtime.session_store.root),
            "claude_settings": present(paths.claude_settings_dir(environ) / "settings.json"),
            "gateway_set_up": endpoint.set_up(runtime.home),
        },
    }


def _first_run(runtime: runtime_mod.Runtime, *, json_output: bool, output_stream: TextIO) -> int:
    """``doctor --first-run [--json]``: exit 0 ready, 1 not ready."""

    from claude_multi.setup import firstrun
    from claude_multi.setup import texts as setup_texts

    items = firstrun.checks(runtime)
    info = firstrun.info_lines(runtime)
    if json_output:
        output_stream.write(strict_json.pretty_file_bytes(firstrun.to_json(items, info)).decode("utf-8"))
    else:
        lines = [setup_texts.FIRSTRUN_HEADER, *firstrun.render_lines(items, surface="cli"), *info,
                 firstrun.summary(items)]
        output_stream.write("".join(termtext.visible_text(line) + "\n" for line in lines))
    return 1 if firstrun.failing(items) else 0
