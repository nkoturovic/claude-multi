"""The transition confirmation screen."""

from __future__ import annotations

from claude_multi import tui
from typing import Any
from typing import TextIO
import textwrap
import claude_multi.cli.text as cli_text


class _TransitionScreen:
    """Semantic diff view + exited-confirmation Modal (TRANSITIONS section 3).

    The wording states the target process must have EXITED, not merely idle.
    Returns True only on explicit confirmation; Esc cancels (False).
    """

    KEYBAR = (("Enter", "confirm exited"), ("?", "help"), ("Esc", "cancel"))

    def __init__(
        self,
        diff: list[str],
        *,
        palette: tui.Palette,
        live_note: str | None = None,
    ):
        self.diff = diff
        self.palette = palette
        self.scroll = 0
        self.live_note = live_note

    def _draw(self, win: Any) -> None:
        win.erase()
        palette = self.palette
        height, width = win.getmaxyx()
        keybar = tui.KeyBar(self.KEYBAR)
        bar_rows = keybar.rows(width)
        bottom = height - bar_rows
        tui.safe_add(win, 1, 2, "transition — semantic diff", palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, "─" * min(width - 3, 60), palette.attr("dim"))
        first = 3
        if self.live_note is not None:
            # Wrapped, so its remedy (the stop command) is never cut off.
            for line in textwrap.wrap(self.live_note, max(20, width - 4), break_on_hyphens=False):
                tui.safe_add(win, first, 2, line, palette.attr("warn"))
                first += 1
        visible = max(1, bottom - first - 1)
        self.scroll = max(0, min(self.scroll, max(0, len(self.diff) - visible)))
        # Diff lines start below the separator, clipped at the
        # reserved scroll-indicator/keybar zone.
        for offset, line in enumerate(self.diff[self.scroll : self.scroll + visible]):
            tui.safe_add(win, first + offset, 2, line)
        if len(self.diff) > visible:
            tui.safe_add(
                win,
                bottom - 1,
                2,
                f"{self.scroll + 1}-{min(len(self.diff), self.scroll + visible)} of {len(self.diff)}",
                palette.attr("dim"),
            )
        keybar.draw(win, height - 1, palette)
        win.refresh()

    def run(self, win: Any) -> bool:
        tui.hide_cursor()
        while True:
            self._draw(win)
            key = tui.read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "ctrl" and key.ch == "c":
                raise KeyboardInterrupt
            if key.kind == "char" and key.ch == "?":
                tui.Modal(
                    "transition — help",
                    cli_text.TRANSITION_HELP.splitlines(),
                    buttons=(("Close", True),),
                ).run(win, self.palette, background=self._draw)
                continue
            if key.kind == "esc":
                return False
            if key.kind == "up" or (key.kind == "char" and key.ch == "k"):
                self.scroll -= 1
                continue
            if key.kind == "down" or (key.kind == "char" and key.ch == "j"):
                self.scroll += 1
                continue
            if key.kind == "enter":
                return bool(
                    tui.Modal(
                        cli_text.TRANSITION_MODAL_TITLE,
                        cli_text.TRANSITION_MODAL_BODY.splitlines(),
                        buttons=(("It has exited", True), ("Cancel", False)),
                    ).run(win, self.palette, background=self._draw)
                )


def _transition_confirm(
    diff: list[str],
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    no_color: bool,
    live_note: str | None = None,
) -> bool:
    """Exited-confirmation: curses Modal when capable, else the [y/N] line."""

    if tui.streams_curses_capable(input_stream, output_stream):
        palette = tui.detect_palette(
            no_color=no_color, tty_in=input_stream, tty_out=output_stream
        )
        screen = _TransitionScreen(diff, palette=palette, live_note=live_note)
        try:
            return bool(
                tui.run_curses_on_streams(
                    screen.run, input_stream, output_stream, palette=palette
                )
            )
        except KeyboardInterrupt:
            return False
        except (tui.CursesError, OSError):
            pass  # fall through to the line prompt
    output_stream.write(
        "The target Claude process must have EXITED (not merely idle); "
        "exiting restarts the turn.\n"
        "Has the target process exited? [y/N] "
    )
    output_stream.flush()
    answer = input_stream.readline()
    return answer.strip().lower() == "y"
