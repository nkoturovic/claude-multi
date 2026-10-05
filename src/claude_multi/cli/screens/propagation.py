"""The propagation screen after a profile save."""

from __future__ import annotations

from claude_multi import catalog
from claude_multi import lineup as lineup_mod
from claude_multi import sessions
from claude_multi import tui
from claude_multi import views
from typing import Any
import io
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.session_facts as session_facts
import claude_multi.cli.text as cli_text
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


class _PropagationScreen:
    """The propagation prompt after a save.

    The preview rows come from ``lineup.followers`` (lock-free) with the
    liveness of each follower's record (``_liveness`` over the Runtime seams,
    taken once).  ``A`` calls ``lineup.on_saved(…, apply_live=True)``, ``L``
    the same with ``apply_live=False``; each shows the lines ``on_saved``
    wrote on the report screen, verbatim.  ``Esc`` calls nothing.
    ``on_saved`` decides membership under each record's lock; this preview
    never decides.
    """

    what = "the propagation prompt"

    def __init__(
        self,
        runtime: runtime_mod.Runtime,
        palette: tui.Palette,
        names: list[str],
        *,
        binding: str | None = None,
        preface: list[str] | None = None,
    ):
        self.runtime = runtime
        self.palette = palette
        self.names = list(names)
        self.binding = binding
        self.preface = list(preface or [])
        self.offset = 0
        self._load()

    def _load(self) -> None:
        runtime = self.runtime
        self.followers, self.unreadable = lineup_mod.followers(runtime, self.names)
        prefixes = runtime._live_prefixes()
        proc_ids = runtime._proc_ids()
        lcat = runtime.lineup_catalog()
        self.states: dict[str, str] = {}
        self.titles: dict[str, str] = {}
        for follower in self.followers:
            mid = follower.managed_id
            try:
                record = runtime.session_store.load(mid)
            except sessions.SessionError:
                self.states[mid], self.titles[mid] = "unknown", "—"
                continue
            self.states[mid] = session_facts._liveness(runtime, record, prefixes, proc_ids)
            try:
                title = sessions.record_summary(record, lcat).title
            except (sessions.SessionError, catalog.CatalogError, KeyError, TypeError):
                title = None
            self.titles[mid] = title or "—"

    @property
    def offers_apply(self) -> bool:
        """``A`` is offered only for a live/unknown follower with a lineup-enabled scope and no pending change."""

        return any(
            self.states.get(f.managed_id) in ("live", "unknown")
            and f.generation >= 1
            and not f.has_pending
            for f in self.followers
        )

    def keybar(self) -> tuple[tuple[str, str], ...]:
        return cli_text.PROPAGATION_KEYBAR if self.offers_apply else cli_text.PROPAGATION_KEYBAR_LATER

    def heading(self) -> str:
        if self.binding is not None:
            return f"saved binding {self.binding} (used by {', '.join(self.names)})"
        return f"saved {', '.join(self.names)}"

    def count_line(self) -> str:
        count = len(self.followers)
        noun = "session follows" if count == 1 else "sessions follow"
        what = "this profile" if len(self.names) == 1 else "these profiles"
        return f"{count} {noun} {what}:"

    def min_size(self, width: int) -> tuple[int, int]:
        return views.ScreenFloor.compute(
            chrome=5, list_rows=len(self.followers), detail_reserve=0,
            bar_rows=tui.KeyBar(cli_text.PROPAGATION_KEYBAR).rows(width), cols=cli_text.PROPAGATION_MIN_COLS,
        ).as_tuple()

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows, cols = self.min_size(width)
        if height < rows or width < cols:
            screens_common._screen_too_small(win, palette, self.what, rows, cols)
            return
        win.erase()
        bar = tui.KeyBar(self.keybar())
        bar_rows = bar.rows(width)
        tui.safe_add(win, 1, 2, views.clip(self.heading(), width - 3), palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.clip(self.count_line(), width - 3))
        lines = views.propagation_rows(self.followers, self.states, self.titles, width=width)
        limit = height - bar_rows - 1 - (1 if self.unreadable else 0)
        visible = max(1, limit - 3)
        if len(lines) > visible:
            # More followers than rows: scroll with ↑/↓ (j/k); the last row says where.
            visible = max(1, visible - 1)
        self.offset = max(0, min(self.offset, len(lines) - visible))
        shown = lines[self.offset:self.offset + visible]
        for y, line in enumerate(shown, 3):
            tui.safe_add(win, y, 0, line)
        if len(shown) < len(lines):
            first, last = self.offset + 1, self.offset + len(shown)
            tui.safe_add(
                win, 3 + visible, 2,
                views.clip(f"{first}-{last} of {len(lines)} (↑/↓ scroll)", width - 3), palette.attr("dim"),
            )
        if self.unreadable:
            tui.safe_add(
                win, height - bar_rows - 1, 2,
                f"? {len(self.unreadable)} session record(s) unreadable; skipped", palette.attr("warn"),
            )
        bar.draw(win, height - 1, palette)
        win.refresh()

    def _apply(self, win: Any, apply_live: bool) -> str:
        buffer = io.StringIO()
        lineup_mod.on_saved(self.runtime, self.names, apply_live=apply_live, out=buffer)
        lines = [*self.preface, *buffer.getvalue().splitlines()]
        title = f"propagation — {self.binding or ', '.join(self.names)}"
        screens_common._TextReportScreen(title, lines, self.palette).run(win)
        return "applied" if apply_live else "later"

    def run(self, win: Any) -> str | None:
        tui.hide_cursor()
        while True:
            self._draw(win)
            key = tui.read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "esc" or (key.kind == "ctrl" and key.ch == "c"):
                return None
            height, width = win.getmaxyx()
            rows, cols = self.min_size(width)
            if height < rows or width < cols:
                continue
            if key.kind == "up" or (key.kind == "char" and key.ch == "k"):
                self.offset = max(0, self.offset - 1)
            elif key.kind == "down" or (key.kind == "char" and key.ch == "j"):
                self.offset += 1  # clamped by _draw
            if key.kind == "char" and key.ch in ("a", "A") and self.offers_apply:
                return self._apply(win, True)
            if key.kind == "char" and key.ch in ("l", "L"):
                return self._apply(win, False)
            if key.kind == "char" and key.ch == "?":
                screens_common._help_modal(win, self.palette, "propagation — help", cli_text.PROPAGATION_HELP, self._draw)


def run_propagation_screen(
    runtime: runtime_mod.Runtime,
    win: Any,
    palette: tui.Palette,
    names: list[str],
    *,
    binding: str | None = None,
    preface: list[str] | None = None,
) -> str | None:
    """The §4.6 prompt for ``names``; ``"none"`` when no session follows them (no screen).

    ``preface`` lines (a rename's ``on_removed`` report) lead the report; with
    no follower they are shown on their own report screen.
    """

    screen = _PropagationScreen(runtime, palette, names, binding=binding, preface=preface)
    if not screen.followers:
        if preface:
            title = f"propagation — {binding or ', '.join(names)}"
            screens_common._TextReportScreen(title, list(preface), palette).run(win)
        return "none"
    return screen.run(win)
