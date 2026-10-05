"""``claude-multi window-ceiling [VALUE | --reset]``: the context window ceiling.

One ceiling governs the lead and every agent: a session's window is the
smaller of it and the lead set's smallest provider bound. It lives in
``choices.json`` (never ``settings.json`` or a record), and a change applies
at the next launch or resume; running sessions keep the window they
launched with.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, TextIO

from claude_multi import choices, profile, termtext
import claude_multi.cli.text as cli_text

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod

EXIT_USAGE = 2


def _refuse(message: str, remedy: str | None = None, *, code: int = 1) -> int:
    sys.stderr.write(f"claude-multi: {termtext.visible_text(message)}\n")
    if remedy:
        sys.stderr.write(f"  fix: {termtext.visible_text(remedy)}\n")
    return code


def command(runtime: runtime_mod.Runtime, args: argparse.Namespace, output_stream: TextIO) -> int:
    value = getattr(args, "ceiling_value", None)
    reset = bool(getattr(args, "ceiling_reset", False))
    current = runtime.window_ceiling()
    if value is None and not reset:
        if current.problem is not None:
            return _refuse(f"cannot read the window ceiling: {current.problem}", current.remedy)
        output_stream.write(cli_text.CEILING_SHOW.format(
            value=profile.format_tokens(current.value), tokens=current.value, source=current.source,
            range=choices.window_ceiling_range()) + "\n")
        output_stream.write(cli_text.CEILING_RULE + "\n")
        output_stream.write(cli_text.CEILING_HOW + "\n")
        return 0
    if reset:
        target = choices.WINDOW_CEILING_DEFAULT
    else:
        try:
            target = choices.parse_window_ceiling(str(value))
        except choices.ChoicesError as exc:
            return _refuse(str(exc), code=EXIT_USAGE)
    # A reset of an explicitly stored default still removes the key, so the
    # ceiling reads as the default again (source "default", like Settings).
    if current.problem is None and current.value == target and not (reset and current.is_set):
        output_stream.write(cli_text.CEILING_UNCHANGED.format(
            value=profile.format_tokens(target), source=current.source) + "\n")
        return 0
    try:
        choices.update(runtime.environ, **{choices.WINDOW_CEILING_KEY: target})
    except choices.ChoicesError as exc:
        return _refuse(f"cannot change the window ceiling: {exc}", exc.remedy)
    except OSError as exc:
        return _refuse(f"cannot change the window ceiling: {exc}")
    template = cli_text.CEILING_RESET if reset else cli_text.CEILING_SET
    output_stream.write(template.format(value=profile.format_tokens(target), tokens=target,
                                        applies=cli_text._APPLIES_NEXT) + "\n")
    if reset and current.problem is None and current.value != target:
        # The way back: a reset asks nothing because one command undoes it
        # (the exact value, so the command restores it to the token).
        was = f"{current.value // 1000}K" if current.value % 1000 == 0 else str(current.value)
        output_stream.write(cli_text.CEILING_RESET_WAS.format(value=was) + "\n")
    return 0
