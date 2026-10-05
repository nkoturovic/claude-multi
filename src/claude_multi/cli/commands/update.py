"""The ``update`` command: updating claude-multi itself.

``claude-multi update`` checks for a newer release, shows its plan, asks and
installs it; ``--check`` only checks, ``--rollback`` returns to the previous
version. The journey (and its refusals and exit codes) is
:mod:`claude_multi.release_update`, shared with the card's U.
"""

from __future__ import annotations

import argparse
from typing import TextIO

from claude_multi import release_update
import claude_multi.cli.runtime as runtime_mod


def _interactive(input_stream: TextIO | None, output_stream: TextIO) -> bool:
    try:
        return bool(input_stream is not None and input_stream.isatty() and output_stream.isatty())
    except (AttributeError, ValueError):
        return False


def _update_command(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    output_stream: TextIO,
    input_stream: TextIO | None = None,
) -> int:
    """Check, plan, confirm and apply an update, or roll back (exit codes in
    :mod:`claude_multi.release_update`)."""

    mode = "rollback" if getattr(args, "update_rollback", False) else (
        "check" if getattr(args, "update_check", False) else "update")
    journey = release_update.journey_for(runtime)
    ask = release_update.terminal_ask(input_stream, output_stream,
                                      interactive=_interactive(input_stream, output_stream))
    return release_update.run(journey, mode=mode, assume_yes=bool(getattr(args, "update_yes", False)), ask=ask,
                              out=output_stream, from_dir=getattr(args, "update_from_dir", None),
                              base_url=getattr(args, "update_base_url", None))
