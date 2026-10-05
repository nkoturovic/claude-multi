"""Which stream a command writes its result to, and the terminal streams."""

from __future__ import annotations

import argparse
import os
import sys
from typing import TextIO

from claude_multi import errors as cli_errors


def _stream_width(stream: TextIO) -> int:
    try:
        return max(32, os.get_terminal_size(stream.fileno()).columns)
    except (AttributeError, OSError):
        return 100


def _stdio_streams_are_ttys() -> bool:
    """Probe stdio TTY-ness, failing closed on missing/closed/broken streams."""

    try:
        return bool(
            sys.stdin is not None
            and sys.stdout is not None
            and sys.stdin.isatty()
            and sys.stdout.isatty()
        )
    except (AttributeError, ValueError, OSError):
        return False


def _open_tty_streams() -> tuple[TextIO, TextIO]:
    """Open the interactive (input, output) stream pair, preferring /dev/tty.

    /dev/tty is non-seekable and cannot be opened in update mode under
    supported Python, so separate read/write handles are opened. When
    /dev/tty cannot be opened (no controlling terminal), fall back to the
    standard streams only when both probe as real TTYs; otherwise fail closed
    for noninteractive handling. Only /dev/tty handles are caller-owned; the
    standard streams are never closed by the caller.
    """

    try:
        tty_in = open("/dev/tty", "r", encoding="utf-8", buffering=1)
        try:
            tty_out = open("/dev/tty", "w", encoding="utf-8", buffering=1)
        except OSError:
            tty_in.close()
            raise
        return tty_in, tty_out
    except OSError as exc:
        if _stdio_streams_are_ttys():
            return sys.stdin, sys.stdout
        raise cli_errors.CLIError(
            "no interactive terminal; use --profile NAME for noninteractive launch"
        ) from exc


# Commands whose output is a result: it goes to stdout, so a pipe or a
# redirect gets it, while questions and progress go to stderr. Commands that
# open a full-screen view or an editor (direct, a bare launch, profile new
# and edit, providers and models edit, sessions transition) keep the
# terminal stream.
_STDOUT_REPORT_COMMANDS = frozenset(
    {
        # doctor --rotate-token included: its questions and the progress of
        # its wait go to the terminal, its result here and a pause to stderr.
        ("doctor", None),
        ("gateway", None),
        ("setup", None),
        ("quota", None),
        ("explain", None),
        ("usage", None),
        ("plan", None),
        ("export", None),
        ("import", None),
        ("discover", None),
        ("uninstall", None),
        ("models", None),
        ("show", None),
        ("update", None),
        ("window-ceiling", None),
        ("restore-2x", None),
        ("session-event", None),
        ("compose", "list"),
        ("compose", "show"),
        ("compose", "delete"),
        ("compose", "duplicate"),
        ("compose", "rename"),
        ("compose", "restore-default"),
        ("compose", "use-as-template"),
        ("custom", "list"),
        ("custom", "add-provider"),
        ("custom", "remove-provider"),
        ("custom", "add-model"),
        ("custom", "remove-model"),
        ("sessions", "list"),
        ("sessions", "show"),
        ("sessions", "forget"),
        ("sessions", "link"),
        ("sessions", "relink-runtime"),
        ("sessions", "mark-ended"),
        ("sessions", "resolve-fork"),
        ("sessions", "stop"),
        ("migrate", None),
        ("lineup", None),
        ("profile", "list"),
        ("profile", "show"),
        ("profile", "rm"),
        ("profile", "rename"),
        ("profile", "duplicate"),
        ("profile", "reseed"),
        ("profile", "migrate"),
        ("profile", "default"),
        ("profile", "starter"),  # a preview; --apply asks on stderr
        # Provider and model verbs ask on stderr and read the process stdin
        # (never /dev/tty); only the editor verbs keep the terminal stream.
        ("providers", "list"),
        ("providers", "show"),
        ("providers", "validate"),
        ("providers", "template"),
        ("providers", "add"),
        ("providers", "approve"),
        ("providers", "set-key"),
        ("providers", "remove-key"),
        ("providers", "sign-in"),
        ("providers", "sign-out"),
        ("providers", "test"),
        ("providers", "enable"),
        ("providers", "disable"),
        ("providers", "transport"),
        ("providers", "rm"),
        ("providers", "apply"),
        ("providers", "migrate-custom"),
        ("models", "list"),
        ("models", "add"),
        ("models", "admit"),
        ("models", "revoke"),
        ("models", "rm"),
        ("models", "show"),
        ("models", "qualify"),
    }
)


def _report_output_stream(args: argparse.Namespace, tty_stream: TextIO, std_stream: TextIO) -> TextIO:
    """stdout for report commands; the tty stream for interactive ones."""

    if args.command == "direct" and getattr(args, "print_launch", False):
        # --print-launch is a pure report even on the interactive direct path.
        return std_stream
    # A discover consent is a y/N on stderr read from the process stdin;
    # the report itself is stdout, like the other operator verbs.
    sub = (
        getattr(args, "compose_command", None)
        or getattr(args, "custom_command", None)
        or getattr(args, "sessions_command", None)
        or getattr(args, "profile_command", None)
        or getattr(args, "providers_command", None)
        or getattr(args, "models_command", None)
    )
    if (args.command, sub) in _STDOUT_REPORT_COMMANDS:
        return std_stream
    return tty_stream
