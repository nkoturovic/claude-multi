"""Get started: the setup steps, their views and the provider picker.

The step list is derived from real state each time it is drawn
(``setup.status.snapshot``): there is no progress file, so stopping at any
point and coming back shows the same marks. Moving between steps writes
nothing; every write follows a confirmation inside a step (the Claude Code
install, a connection, the starter profile) and goes through the setup
layer. Get started never runs inside a Claude Code session's tools, and a
resume card opens it read-only.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from claude_multi import errors as cli_errors
from claude_multi import tui
from claude_multi import views
import claude_multi.cli.consent as consent
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.providers as screens_providers
import claude_multi.cli.text as cli_text

READ_STEPS = ("preflight", "check")


@dataclass(frozen=True)
class GetStartedResult:
    """How Get started ended: back to the card (``card``), or ``use`` a
    profile chosen in Profiles."""

    kind: str  # card | use
    profile: str | None = None


# ------------------------------------------------------------------ the step rows


def profile_why(default: Any) -> str:
    return cli_text.GS_PROFILE_WHY.get(getattr(default, "reason", ""), cli_text.GS_PROFILE_WHY_AUTOMATIC)


def step_summary(step: Any, state: Any, results: Mapping[str, str] | None = None) -> str:
    """The summary text of one step (``GS_SUMMARY``)."""

    fields: dict[str, Any] = {"n": 0, "problem": "", "connected": "", "name": "", "why": "", "results": "",
                              "version": ""}
    fields.update({key: value for key, value in dict(step.fields).items() if value is not None})
    key = (step.id, step.state)
    if step.id == "test" and results:
        key = ("test", "done")
        fields["results"] = " · ".join(results.values())
    if step.id == "profile" and step.state == "done":
        fields["why"] = profile_why(state.default)
    if step.id == "providers" and step.state == "done":
        fields["connected"] = connected_text(state.connections, empty="")
    template = cli_text.GS_SUMMARY.get(key, "")
    return template.format(**fields)


def step_mark(step: Any, results: Mapping[str, str] | None = None) -> str:
    if step.id == "test" and results:
        return cli_text.GS_MARKS["done"]
    return cli_text.GS_MARKS.get(step.state, "·")


def step_title(step: Any, state: Any) -> str:
    version = ""
    claude = next((item for item in state.steps if item.id == "claude"), None)
    if claude is not None:
        version = str(dict(claude.fields).get("version") or "")
    return cli_text.GS_STEP_TITLES[step.id].format(version=version).rstrip()


def connected_text(connections: Sequence[Any], *, empty: str = cli_text.GS_CONNECTED_NONE) -> str:
    labels = [item.label for item in connections if item.state == "connected"]
    return " · ".join(labels) if labels else empty


def step_rows(state: Any, results: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """``(step id, row text)`` for the step list: ``N Title   mark summary``."""

    rows = []
    for number, step in enumerate(state.steps, start=1):
        text = f"{number} {step_title(step, state):<24}{step_mark(step, results)} {step_summary(step, state, results)}"
        rows.append((step.id, text.rstrip()))
    return rows


# ------------------------------------------------------------------ a step view


class _StepView:
    """A scrolling text view with a row of buttons (Tab or ← → move, Enter
    chooses, Esc goes back); ``shortcuts`` maps a letter to a button value."""

    def __init__(self, title: str, lines: Sequence[str], *, palette: tui.Palette,
                 buttons: Sequence[tuple[str, Any]] = (), shortcuts: Mapping[str, Any] | None = None,
                 roles: Mapping[int, str] | None = None, help_text: str = ""):
        self.title = title
        self.lines = list(lines)
        self.palette = palette
        self.buttons = list(buttons)
        self.shortcuts = dict(shortcuts or {})
        self.roles = dict(roles or {})
        self.help_text = help_text
        self.focus = 0
        self.offset = 0

    def _wrapped(self, width: int) -> list[tuple[str, str]]:
        out = []
        for index, line in enumerate(self.lines):
            role = self.roles.get(index, "warn" if line.startswith("! ") else "normal")
            indent = "  " if line.startswith("! ") else line[: len(line) - len(line.lstrip())]
            for piece in textwrap.wrap(line, max(20, width - 4), subsequent_indent=indent,
                                       break_on_hyphens=False) or [""]:
                out.append((piece, role))
        return out

    def _keybar(self) -> tui.KeyBar:
        bindings = [("Enter", self.buttons[self.focus][0].lower())] if self.buttons else []
        if len(self.buttons) > 1:
            bindings.append(("←→", "choose"))
        return tui.KeyBar([*bindings, ("?", "help"), ("Esc", "back")])

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        if height < 10 or width < 40:
            screens_common._screen_too_small(win, palette, "this step", 10, 40)
            return
        win.erase()
        keybar = self._keybar()
        bar_rows = keybar.rows(width)
        tui.safe_add(win, 1, 2, views.clip(self.title, width - 3), palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        buttons_row = height - bar_rows - 2
        room = max(1, buttons_row - 1 - 3)
        lines = self._wrapped(width)
        self.offset = max(0, min(self.offset, max(0, len(lines) - room)))
        for y, (line, role) in enumerate(lines[self.offset:self.offset + room], 3):
            tui.safe_add(win, y, 2, views.clip(line, width - 3), palette.attr(role))
        col = 2
        for index, (label, _value) in enumerate(self.buttons):
            attr = palette.attr("accent") | (tui.curses.A_REVERSE if index == self.focus else 0)
            text = f"[ {label} ]"
            if col + len(text) >= width - 1:
                break
            tui.safe_add(win, buttons_row, col, text, attr)
            col += len(text) + 2
        keybar.draw(win, height - 1, palette)
        win.refresh()

    def run(self, win: Any) -> Any:
        tui.hide_cursor()
        while True:
            self._draw(win)
            key = tui.read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "ctrl" and key.ch == "c":
                raise KeyboardInterrupt
            if key.kind == "esc":
                return None
            if key.kind == "enter":
                return self.buttons[self.focus][1] if self.buttons else None
            if key.kind in ("tab", "right") and self.buttons:
                self.focus = (self.focus + 1) % len(self.buttons)
            elif key.kind in ("btab", "left") and self.buttons:
                self.focus = (self.focus - 1) % len(self.buttons)
            elif key.kind == "up":
                self.offset = max(0, self.offset - 1)
            elif key.kind == "down":
                self.offset += 1
            elif key.kind == "char" and key.ch == "?":
                screens_common._help_modal(win, self.palette, f"{self.title} — help",
                                           self.help_text or self.title, self._draw)
            elif key.kind == "char" and key.ch and key.ch.upper() in self.shortcuts:
                return self.shortcuts[key.ch.upper()]


# ------------------------------------------------------------------ the provider picker


@dataclass(frozen=True)
class _Row:
    kind: str  # header | entry | block
    text: str
    right: str = ""
    entry: Any = None


class _ProviderPicker:
    """Every way to connect a provider, grouped by provider (``only="other"``:
    adding your own — a reviewed preset with an API key, your own endpoint,
    a server on your network — plus the manual form). Enter connects the selected
    entry through :class:`ConnectActions`; the result stays on the message
    line and the states refresh."""

    what = "the provider picker"

    def __init__(self, runtime: Any, palette: tui.Palette, *, only: str | None = None):
        self.runtime = runtime
        self.palette = palette
        self.only = only
        self.selected = 0
        self.offset = 0
        self.message = ""
        self.message_role = "accent"
        self.last: screens_providers.Outcome | None = None
        self._load()
        self.selected = next((i for i, row in enumerate(self.rows) if row.kind == "entry"), 0)

    def _load(self) -> None:
        from claude_multi.setup import providers as layer

        try:
            entries = list(layer.picker_entries(self.runtime, only=self.only))
        except (cli_errors.ClaudeMultiError, OSError, ValueError) as exc:
            entries = []
            self.message, self.message_role = str(exc), "warn"
        if self.only == "other":
            from claude_multi.setup import texts

            entries.append(layer.PickerEntry("other:manual", "other", texts.PICKER_OTHER, "",
                                             cli_text.ADVANCED_MANUAL, "manual", None, "", "", True, False))
        rows: list[_Row] = []
        group = None
        yours = False
        for entry in entries:
            if entry.group.startswith("own:") and not yours:
                yours = True
                rows.append(_Row("block", cli_text.PICKER_YOURS))
            if entry.group != group:
                group = entry.group
                family = cli_text.PICKER_FAMILY.format(family=entry.family) if entry.family else ""
                rows.append(_Row("header", entry.group_title, family))
            rows.append(_Row("entry", entry.label, entry.state_text or entry.note, entry))
        self.rows = rows
        self.selected = min(self.selected, max(0, len(rows) - 1))

    # -- layout ------------------------------------------------------------

    def _detail(self) -> str:
        if not (0 <= self.selected < len(self.rows)) or self.rows[self.selected].entry is None:
            return ""
        entry = self.rows[self.selected].entry
        if not entry.available:
            return cli_text.PICKER_DETAIL["unavailable"].format(note=entry.note or entry.state_text)
        display = entry.group_title
        if entry.kind == "api-key":
            from claude_multi.setup import texts

            text = cli_text.PICKER_DETAIL["api-key"].format(display=display)
            text += cli_text.PICKER_DETAIL["api-key-paid"].format(display=display)
            if entry.note == texts.NO_MODELS_NOTE:
                text += " " + cli_text.PICKER_DETAIL["no-models"].format(display=display)
            return text
        return cli_text.PICKER_DETAIL.get(entry.kind, "").format(display=display, note=entry.note)

    def min_size(self, width: int) -> tuple[int, int]:
        bar = tui.KeyBar(cli_text.PICKER_KEYBAR).rows(width)
        return max(cli_text.PICKER_MIN_ROWS, 3 + 3 + 3 + 1 + bar + 1), cli_text.PICKER_MIN_COLS

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows_needed, cols_needed = self.min_size(width)
        if height < rows_needed or width < cols_needed:
            screens_common._screen_too_small(win, palette, self.what, rows_needed, cols_needed)
            return
        win.erase()
        keybar = tui.KeyBar(cli_text.PICKER_KEYBAR)
        bar_rows = keybar.rows(width)
        title = cli_text.PICKER_TITLE_OTHER if self.only == "other" else cli_text.PICKER_TITLE
        tui.safe_add(win, 1, 2, title, palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        message_row = height - bar_rows - 1
        detail = screens_common._wrapped(self._detail(), width - 4)[:3]
        detail_top = message_row - len(detail)
        rule_row = detail_top - 1
        visible = max(1, rule_row - 3)
        if self.selected < self.offset:
            self.offset = self.selected
        elif self.selected >= self.offset + visible:
            self.offset = self.selected - visible + 1
        self.offset = max(0, min(self.offset, max(0, len(self.rows) - visible)))
        right_x = max(30, width - 27)
        for y, index in enumerate(range(self.offset, min(len(self.rows), self.offset + visible)), 3):
            row = self.rows[index]
            if row.kind == "block":
                tui.safe_add(win, y, 4, views.clip(row.text, width - 5), palette.attr("dim") | tui.curses.A_BOLD)
                continue
            if row.kind == "header":
                tui.safe_add(win, y, 4, views.clip(row.text, right_x - 5), palette.attr("accent"))
                if row.right:
                    tui.safe_add(win, y, right_x, views.clip(row.right, width - 1 - right_x), palette.attr("dim"))
                continue
            focused = index == self.selected
            role = "normal" if row.entry.available else "dim"
            if focused:
                tui.safe_add(win, y, 2, "›", palette.attr("accent"))
            attr = palette.attr(role) | (tui.curses.A_REVERSE if focused else 0)
            tui.safe_add(win, y, 6, views.clip(row.text, right_x - 7), attr)
            if row.right:
                tui.safe_add(win, y, right_x, views.clip(row.right, width - 1 - right_x), palette.attr("dim"))
        tui.safe_add(win, rule_row, 2, "─", palette.attr("dim"))
        for offset, line in enumerate(detail):
            tui.safe_add(win, detail_top + offset, 2, views.clip(line, width - 3), palette.attr("dim"))
        if self.message:
            tui.safe_add(win, message_row, 2, views.clip(self.message, width - 3), palette.attr(self.message_role))
        keybar.draw(win, height - 1, palette)
        win.refresh()

    # -- keys --------------------------------------------------------------

    def _step(self, direction: int) -> None:
        index = self.selected
        while 0 <= index + direction < len(self.rows):
            index += direction
            if self.rows[index].kind == "entry":
                self.selected = index
                return

    def _edge(self, last: bool) -> None:
        order = range(len(self.rows) - 1, -1, -1) if last else range(len(self.rows))
        self.selected = next((i for i in order if self.rows[i].kind == "entry"), self.selected)

    def _connect(self, win: Any) -> None:
        row = self.rows[self.selected] if 0 <= self.selected < len(self.rows) else None
        if row is None or row.entry is None:
            return
        actions = screens_providers.ConnectActions(self.runtime, win, self.palette, background=self._draw)
        outcome = actions.connect(row.entry)
        current = row.entry.id
        self._load()
        self.selected = next((i for i, r in enumerate(self.rows) if r.entry is not None and r.entry.id == current),
                             self.selected)
        if outcome is not None and outcome.text:
            self.last = outcome
            self.message, self.message_role = outcome.text, outcome.role

    def run(self, win: Any) -> screens_providers.Outcome | None:
        tui.hide_cursor()
        while True:
            self._draw(win)
            key = tui.read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "ctrl" and key.ch == "c":
                raise KeyboardInterrupt
            if key.kind == "esc":
                return self.last
            height, width = win.getmaxyx()
            rows_needed, cols_needed = self.min_size(width)
            if height < rows_needed or width < cols_needed:
                continue
            self.message = ""
            if key.kind == "up":
                self._step(-1)
            elif key.kind == "down":
                self._step(1)
            elif key.kind == "home":
                self._edge(last=False)
            elif key.kind == "end":
                self._edge(last=True)
            elif key.kind == "char" and key.ch == "?":
                screens_common._help_modal(win, self.palette, cli_text.PICKER_HELP_TITLE, cli_text.PICKER_HELP,
                                           self._draw)
            elif key.kind == "enter":
                self._connect(win)


def run_picker(runtime: Any, win: Any, palette: tui.Palette, *, only: str | None = None
               ) -> screens_providers.Outcome | None:
    """The provider picker (``only="other"``: adding your own, the reviewed
    presets with an API key included); the last connection's result, or None."""

    return _ProviderPicker(runtime, palette, only=only).run(win)


# ------------------------------------------------------------------ Get started


class _GetStartedScreen:
    """The step list (see the module docstring)."""

    what = "Get started"

    def __init__(self, runtime: Any, palette: tui.Palette, *, focus: str | None = None, resume: bool = False,
                 open_profiles: Callable[[Any], str | None] | None = None):
        self.runtime = runtime
        self.palette = palette
        self.resume = resume
        self.open_profiles = open_profiles
        self.message = ""
        self.message_role = "accent"
        self.results: dict[str, str] = {}
        self._reload()
        ids = [step.id for step in self.state.steps]
        target = focus if focus in ids else self.state.first_unfinished
        self.selected = ids.index(target) if target in ids else 0

    def _reload(self) -> None:
        from claude_multi.setup import status

        self.state = status.snapshot(self.runtime)

    # -- refusals ----------------------------------------------------------

    def _blocked(self, step_id: str) -> str | None:
        """Why a step that writes or sends cannot run here (None: it can)."""

        if step_id in READ_STEPS:
            return None
        if self.resume:
            return cli_text.GS_RESUME_READONLY
        if not getattr(self.runtime, "allow_state_writes", False):
            return cli_text.GS_READONLY
        if step_id != "gateway" and consent.session_marker(self.runtime.environ) is not None:
            return cli_text.GS_IN_SESSION
        return None

    # -- layout ------------------------------------------------------------

    def min_size(self, width: int) -> tuple[int, int]:
        return cli_text.GS_MIN_ROWS, cli_text.GS_MIN_COLS

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows_needed, cols_needed = self.min_size(width)
        if height < rows_needed or width < cols_needed:
            screens_common._screen_too_small(win, palette, self.what, rows_needed, cols_needed)
            return
        win.erase()
        keybar = tui.KeyBar(cli_text.GS_KEYBAR)
        bar_rows = keybar.rows(width)
        message_row = height - bar_rows - 1
        tui.safe_add(win, 1, 2, cli_text.GS_HEADER, palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        step = self.state.steps[self.selected]
        version = str(dict(self.state.step("claude").fields).get("version") or "")
        intro = screens_common._wrapped(cli_text.GS_INTRO, width - 4)
        detail = screens_common._wrapped(cli_text.GS_DETAIL[step.id].format(version=version), width - 4)
        connected = cli_text.GS_CONNECTED.format(list=connected_text(self.state.connections))
        steps = step_rows(self.state, self.results)
        fixed = len(steps) + 1 + len(detail)
        room = message_row - 3
        show_intro = room >= fixed + len(intro) + 3
        blanks = room >= fixed + (len(intro) + 1 if show_intro else 0) + 2
        y = 3
        if show_intro:
            for line in intro:
                tui.safe_add(win, y, 2, line)
                y += 1
            y += 1
        for index, (_sid, text) in enumerate(steps):
            focused = index == self.selected
            if focused:
                tui.safe_add(win, y, 2, "›", palette.attr("accent"))
            state_ = self.state.steps[index].state
            role = "warn" if state_ == "blocked" else "normal"
            tui.safe_add(win, y, 4, views.clip(text, width - 5),
                         palette.attr(role) | (tui.curses.A_REVERSE if focused else 0))
            y += 1
        if blanks:
            y += 1
        tui.safe_add(win, y, 2, views.clip(connected, width - 3))
        y += 2 if blanks else 1
        for line in detail[: max(0, message_row - y)]:
            tui.safe_add(win, y, 2, line, palette.attr("dim"))
            y += 1
        if self.message:
            tui.safe_add(win, message_row, 2, views.clip(self.message, width - 3), palette.attr(self.message_role))
        keybar.draw(win, height - 1, palette)
        win.refresh()

    def _say(self, text: str, role: str = "accent") -> None:
        self.message = text
        self.message_role = role

    def _show(self, outcome: Any) -> None:
        if outcome is not None and getattr(outcome, "text", ""):
            self._say(outcome.text, outcome.role)

    # -- step views ----------------------------------------------------------

    def _preflight(self, win: Any) -> None:
        from claude_multi.setup import firstrun

        checks = self.state.checks or firstrun.checks(self.runtime)
        tui.TextView(cli_text.PREFLIGHT_TITLE, firstrun.render_lines(checks[:3], surface="tui"),
                     palette=self.palette).run(win)

    def _claude(self, win: Any) -> None:
        from claude_multi import acquire, pin, retention
        from claude_multi.setup import external, model

        status_ = external.claude_status(self.runtime)
        if status_.state == "verified":
            tui.TextView(cli_text.CLAUDE_STEP_STATUS_TITLE.format(version=status_.version),
                         [status_.detail, cli_text.GS_DETAIL["claude"].format(version=status_.version)],
                         palette=self.palette).run(win)
            return
        environ = self.runtime.environ
        declined = False
        refused = ""

        def ask(plan: Any) -> bool:
            nonlocal declined, refused
            try:
                consent.require_human(consent.SETUP_DOWNLOAD_VERB, environ)
            except consent.ConsentRefused as exc:
                refused = str(exc)
                return False
            lines = [*plan.text(environ).splitlines(), "", cli_text.CLAUDE_STEP_UNTOUCHED]
            answer = bool(tui.TextView(cli_text.CLAUDE_STEP_TITLE.format(version=status_.version), lines,
                                       palette=self.palette, confirm=True).run(win))
            declined = not answer
            return answer

        def progress(line: str) -> None:
            self._say(cli_text.CLAUDE_STEP_PROGRESS.format(line=line), "dim")
            self._draw(win)

        contract = self.runtime.catalog.docs["native-contract"]
        try:
            outcome = external.acquire_claude(self.runtime, consent=ask, progress=progress)
        except (acquire.AcquireError, pin.PinError, cli_errors.ClaudeMultiError, OSError) as exc:
            remedy = getattr(exc, "remedy", None)
            self._say(str(exc) + (f" — {remedy}" if remedy and remedy not in str(exc) else ""), "warn")
            return
        if outcome.path is None:
            self._say(refused or (model.NOTHING_CHANGED if declined else
                                  cli_text.CLAUDE_STEP_NOT_DONE.format(version=outcome.version)), "warn")
            return
        retention.prune(contract, environ)
        self._say(cli_text.CLAUDE_STEP_DONE.format(version=outcome.version))

    def _gateway(self, win: Any) -> None:
        from claude_multi.setup import external

        before = external.gateway_status(self.runtime)
        try:
            outcome = external.ensure_gateway(self.runtime)
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._say(str(exc), "warn")
            return
        after = external.gateway_status(self.runtime)
        if outcome is None or outcome.ok:
            if before.state == "running":
                lines = [cli_text.GATEWAY_STEP_RUNNING]
            else:
                lines = [cli_text.GATEWAY_STEP_STARTED.format(port=after.port if after.port is not None else "?")]
        else:
            lines = list(outcome.lines())
        self._say(lines[0], "accent" if outcome is None or outcome.ok else "warn")
        while True:
            choice = tui.Modal(cli_text.GATEWAY_STEP_TITLE, [piece for line in lines for piece in
                                                            screens_common.modal_lines(line, win)],
                               buttons=cli_text.GATEWAY_STEP_BUTTONS).run(win, self.palette, background=self._draw)
            if choice == "log":
                tail = list(external.gateway_log_tail(self.runtime)) or [cli_text.GATEWAY_LOG_EMPTY]
                tui.TextView(cli_text.GATEWAY_LOG_TITLE, tail, palette=self.palette).run(win)
                continue
            if choice == "proxy":
                self._proxy(win)
            return

    def _proxy(self, win: Any) -> None:
        """The gateway's outbound proxy: set it (an unauthenticated URL) or
        connect directly; a running gateway reloads with it."""

        from claude_multi import endpoint
        from claude_multi.setup import model, providers as layer

        try:
            current = endpoint.read_config(self.runtime.home)
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._say(str(exc), "warn")
            return
        shown = current.proxy_url if current is not None and current.proxy_url else cli_text.PROXY_NONE
        field = tui.TextInput(current.proxy_url if current is not None and current.proxy_url else "",
                              max_length=2048)
        lines = screens_common.modal_lines(cli_text.PROXY_BODY, win) + [
            "", cli_text.PROXY_CURRENT.format(proxy=shown)]
        choice = tui.Modal(cli_text.PROXY_TITLE, lines, buttons=cli_text.PROXY_BUTTONS, input=field).run(
            win, self.palette, background=self._draw)
        if choice not in ("save", "clear"):
            return
        url = field.value.strip() if choice == "save" else None
        if choice == "save" and not url:
            url = None
        # One setup-layer change: checked first, and a refused render puts
        # the previous proxy back (nothing changed means nothing changed).
        try:
            plan = layer.plan_proxy(self.runtime, url)
            applied = layer.apply_proxy(self.runtime, plan, model.Confirmation.given(plan))
        except (cli_errors.ClaudeMultiError, OSError, KeyError, ValueError) as exc:
            remedy = getattr(exc, "remedy", None)
            self._say(str(exc) + (f" — {remedy}" if remedy and remedy not in str(exc) else ""), "warn")
            return
        text = cli_text.PROXY_SET.format(proxy=plan.proxy_url) if plan.proxy_url else cli_text.PROXY_CLEARED
        if applied.lines:
            text += " " + " · ".join(applied.lines)
        self._say(text)

    def _providers(self, win: Any) -> None:
        self._show(run_picker(self.runtime, win, self.palette))

    def _test(self, win: Any) -> None:
        from claude_multi.setup import status

        linked = []
        for conn in status.connected(self.runtime):
            served, _slash, _expected = conn.served.partition("/")
            if served.isdigit() and int(served) > 0:
                linked.append(conn)
        if not linked:
            from claude_multi.setup import texts

            self._say(texts.TEST_NOTHING, "warn")
            return
        picker = tui.SelectList(cli_text.TEST_PICK_TITLE, [tui.SelectItem(conn.label) for conn in linked],
                                footer=cli_text.TEST_PICK_KEYBAR, multi=True)
        picker.toggled = set(range(len(linked)))
        chosen = picker.run(win, self.palette, help_text=cli_text.TEST_PICK_HELP)
        if not chosen:
            return
        ids = [linked[index].provider_id for index in chosen]
        actions = screens_providers.ConnectActions(self.runtime, win, self.palette, background=self._draw)
        outcome = actions.test(ids, show_results=True)
        if outcome.text:
            for conn in linked:
                hit = next((part for part in outcome.text.split(" · ") if part.startswith(conn.display + ":")), None)
                if hit is not None:
                    self.results[conn.provider_id] = hit
        self._show(outcome)

    def _profile(self, win: Any) -> GetStartedResult | None:
        view = _ProfileStepView(self.runtime, self.palette, state=self.state)
        choice = view.run(win)
        if view.message:
            self._say(view.message, view.message_role)
        if choice == "profiles":
            return self._profiles(win)
        return None

    def _check(self, win: Any) -> GetStartedResult | None:
        from claude_multi.setup import firstrun

        checks = self.state.checks or firstrun.checks(self.runtime)
        failing = firstrun.failing(checks)
        footer = (cli_text.FIRSTRUN_FOOTER_READY if not failing else
                  cli_text.FIRSTRUN_FOOTER_NOT_READY.format(n=len(failing), title=failing[0].title))
        lines = [*firstrun.render_lines(checks, surface="tui"), "", *firstrun.info_lines(self.runtime), "", footer]
        choice = _StepView(cli_text.CHECK_STEP_TITLE, lines, palette=self.palette,
                           buttons=(("Open the launch card", "card"), ("Back", None)),
                           help_text=cli_text.GS_HELP).run(win)
        if choice == "card":
            return GetStartedResult("card")
        return None

    def _profiles(self, win: Any) -> GetStartedResult | None:
        if self.open_profiles is None:
            self._say(cli_text.GS_PROFILES_FROM_CARD)
            return None
        name = self.open_profiles(win)
        if name:
            return GetStartedResult("use", name)
        return None

    def _open(self, win: Any) -> GetStartedResult | None:
        step = self.state.steps[self.selected]
        refusal = self._blocked(step.id)
        if refusal is not None:
            self._say(refusal, "warn")
            return None
        handlers: dict[str, Callable[[Any], Any]] = {
            "preflight": self._preflight, "claude": self._claude, "gateway": self._gateway,
            "providers": self._providers, "test": self._test, "profile": self._profile, "check": self._check,
        }
        return handlers[step.id](win)

    def run(self, win: Any) -> GetStartedResult:
        tui.hide_cursor()
        while True:
            self._draw(win)
            key = tui.read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "ctrl" and key.ch == "c":
                raise KeyboardInterrupt
            if key.kind == "esc":
                return GetStartedResult("card")
            height, width = win.getmaxyx()
            rows_needed, cols_needed = self.min_size(width)
            if height < rows_needed or width < cols_needed:
                continue
            self.message = ""
            letter = key.ch.upper() if key.kind == "char" and key.ch else ""
            result: GetStartedResult | None = None
            if key.kind == "up":
                self.selected = max(0, self.selected - 1)
            elif key.kind == "down":
                self.selected = min(len(self.state.steps) - 1, self.selected + 1)
            elif key.kind == "home":
                self.selected = 0
            elif key.kind == "end":
                self.selected = len(self.state.steps) - 1
            elif letter == "?":
                screens_common._help_modal(win, self.palette, cli_text.GS_HELP_TITLE, cli_text.GS_HELP, self._draw)
            elif key.kind == "enter":
                result = self._open(win)
                self._reload()
            elif letter == "A":
                refusal = self._blocked("providers")
                if refusal is not None:
                    self._say(refusal, "warn")
                else:
                    self._providers(win)
                    self._reload()
            elif letter == "P":
                result = self._profiles(win)
                self._reload()
            if result is not None:
                return result


# ------------------------------------------------------------------ choose your profile


class _ProfileStepView:
    """Choose your profile: keep the one that fits, or build the starter."""

    def __init__(self, runtime: Any, palette: tui.Palette, *, state: Any):
        self.runtime = runtime
        self.palette = palette
        self.state = state
        self.message = ""
        self.message_role = "accent"

    def _connected(self) -> str:
        return cli_text.GS_CONNECTED.format(list=connected_text(self.state.connections))

    def _starter_name(self) -> str:
        taken = set(self.runtime.profiles.names())
        name, n = "starter", 2
        while name in taken:
            name, n = f"starter-{n}", n + 1
        return name

    def _fit_lines(self, default: Any) -> list[str]:
        from claude_multi import profile as profile_mod
        from claude_multi.setup import defaults

        why = cli_text.PROFILE_STEP_WHY.get(default.reason, cli_text.GS_PROFILE_WHY_AUTOMATIC)
        lines = [self._connected(), "", cli_text.PROFILE_STEP_YOURS.format(name=default.name, why=why)]
        try:
            document = self.runtime.profiles.load(default.name)
        except profile_mod.ProfileError:
            return lines
        description = str(document.get("description") or "")
        if description:
            lines.append("  " + description)
        try:
            evaluation = profile_mod.evaluate(document, self.runtime.lineup_catalog(),
                                              bindings=self.runtime.bindings.bindings(),
                                              effective=self.runtime.current_effective(), ad_hoc=False)
        except (cli_errors.ClaudeMultiError, OSError):
            return lines
        if evaluation.lineup is not None:
            notes = defaults.spend_notes(self.runtime, evaluation.lineup)
            if notes:
                lines.append("")
                lines.extend(f"! {note}" for note in notes)
        return lines

    def run(self, win: Any) -> str | None:
        """``profiles`` (open Profiles), ``saved`` or None."""

        from claude_multi.setup import defaults, model, profiles as setup_profiles

        connected = [item for item in self.state.connections if item.state == "connected"]
        if not connected:
            self.message, self.message_role = cli_text.PROFILE_STEP_WAITING, "warn"
            return None
        default = self.state.default
        if default is not None and default.ready:
            choice = _StepView(cli_text.PROFILE_STEP_TITLE, self._fit_lines(default), palette=self.palette,
                               buttons=cli_text.PROFILE_STEP_KEEP_BUTTONS, shortcuts={"P": "profiles"},
                               help_text=cli_text.GS_DETAIL["profile"]).run(win)
            return choice if choice == "profiles" else None
        name = self._starter_name()
        try:
            plan = setup_profiles.plan_new_starter(self.runtime, name)
        except model.Refused:
            _StepView(cli_text.PROFILE_STEP_TITLE, [self._connected(), "", cli_text.STARTER_NO_LEAD_TUI],
                      palette=self.palette, buttons=(("Back", None),)).run(win)
            self.message, self.message_role = cli_text.STARTER_NO_LEAD_TUI, "warn"
            return None
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self.message, self.message_role = str(exc), "warn"
            return None
        names = " · ".join(item.display for item in connected)
        lines = [cli_text.PROFILE_STEP_STARTER_INTRO.format(connected=names), *plan.lines]
        choice = _StepView(cli_text.PROFILE_STEP_TITLE, lines, palette=self.palette,
                           buttons=cli_text.PROFILE_STEP_STARTER_BUTTONS, shortcuts={"P": "profiles"},
                           help_text=cli_text.GS_DETAIL["profile"]).run(win)
        if choice != "save":
            return choice
        try:
            setup_profiles.apply_new_starter(self.runtime, plan, model.Confirmation.given(plan))
            chosen = defaults.set_default_plan(self.runtime, name)
            defaults.apply_default(self.runtime, chosen, model.Confirmation.given(chosen))
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self.message, self.message_role = str(exc), "warn"
            return None
        self.message = cli_text.PROFILE_STEP_SAVED.format(name=name)
        return "saved"


def run_get_started(runtime: Any, win: Any, palette: tui.Palette, *, focus: str | None = None,
                    resume: bool = False, open_profiles: Callable[[Any], str | None] | None = None
                    ) -> GetStartedResult:
    """Open Get started on ``win`` (``focus``: the step to select first;
    ``resume``: read-only; ``open_profiles(win)`` opens Profiles and returns
    the profile chosen with Enter, or None)."""

    return _GetStartedScreen(runtime, palette, focus=focus, resume=resume, open_profiles=open_profiles).run(win)
