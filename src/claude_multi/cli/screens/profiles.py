"""The Profiles screen: every profile with its state, and what can be done
with one — use it for this launch, create one (from a shipped profile, from
the profile on the card, or built from the connected providers), edit,
copy, rename, delete (a copy is kept), restore a shipped version and make
one the default.

Moving through the list writes nothing. Every write follows a
confirmation and goes through the profile operations of the setup layer,
which re-check that nothing changed since the plan was shown. A profile
that cannot be loaded is listed with its file and line: E edits the file
as it is stored, X deletes it (a copy is kept).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from claude_multi import errors as cli_errors
from claude_multi import lineup as lineup_mod
from claude_multi import profile as profile_mod
from claude_multi import readiness
from claude_multi import state as state_mod
from claude_multi import tui
from claude_multi import views
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.profile_editor as screens_profile_editor
import claude_multi.cli.screens.propagation as screens_propagation
import claude_multi.cli.text as cli_text

MIN_ROWS = 18
MIN_COLS = 60
# Rows that are not the list: top, title, rule, header, separator, two
# detail lines, the message.
CHROME = 8
EDITED_LABELS = ("edited", "edited-stale", "unknown")


@dataclass(frozen=True)
class ProfilesResult:
    """How Profiles ended: ``use`` a profile, ``back``, or ``get-started``
    (W). ``renamed`` pairs ``(old, new)`` and ``removed`` names tell the
    caller what moved while the screen was open."""

    kind: str  # use | back | get-started
    name: str | None = None
    renamed: tuple[tuple[str, str], ...] = ()
    removed: tuple[str, ...] = ()


def origin_text(row: Any) -> str:
    """``yours``, ``seed`` or ``seed·edited`` (a shipped profile you changed)."""

    if row.origin != "seed":
        return row.origin
    return "seed·edited" if row.seed_state in EDITED_LABELS else "seed"


def used_text(row: Any, now: datetime) -> str:
    return views.ages(row.last_used, now=now, compact=True)


def column_widths(rows: Any, now: datetime) -> tuple[int, int, int, int]:
    """``(profile, origin, state, used)`` widths; notes take the rest."""

    names = max([len("profile"), *(len(row.name) for row in rows)])
    origins = max([len("origin"), *(len(origin_text(row)) for row in rows)])
    states = max([len("state"), *(len(row.state_text) for row in rows)])
    used = max([len("used"), *(len(used_text(row, now)) for row in rows)])
    return min(16, names), min(11, origins), min(22, states), min(10, used)


def row_text(row: Any, widths: tuple[int, int, int, int], now: datetime) -> str:
    name_w, origin_w, state_w, used_w = widths
    cells = (views.clip(row.name, name_w), views.clip(origin_text(row), origin_w),
             views.clip(row.state_text, state_w), views.clip(used_text(row, now), used_w))
    text = "  ".join(f"{cell:<{width}}" for cell, width in zip(cells, widths))
    return f"{text}  {' · '.join(row.notes)}".rstrip()


def header_text(widths: tuple[int, int, int, int]) -> str:
    cells = ("profile", "origin", "state", "used")
    return ("  ".join(f"{cell:<{width}}" for cell, width in zip(cells, widths)) + "  notes").rstrip()


def keybar(row: Any, *, fallback_only: bool) -> tuple[tuple[str, str], ...]:
    """The bar for the selected row (yours, a shipped profile, or a file
    that cannot be loaded); F reads ``all profiles`` while the filter is on."""

    if row is None:
        bindings = [("N", "new"), ("F", "fallback only"), ("B", "bindings"), ("W", "get started"), ("?", "help"),
                    ("Esc", "back")]
    elif not row.loadable:
        bindings = list(cli_text.PROFILES_KEYBAR_UNLOADABLE)
        if row.origin == "seed":
            bindings = [cli_text.PROFILES_RESTORE_BINDING if key == "X" else (key, label)
                        for key, label in bindings]
    elif row.origin == "seed":
        bindings = [(key, label) for key, label in cli_text.PROFILES_KEYBAR_SEED
                    if key != "U" or row.seed_state != "current"]
    else:
        bindings = list(cli_text.PROFILES_KEYBAR_YOURS)
    if fallback_only:
        bindings = [(key, cli_text.PROFILES_FILTER_OFF_LABEL if key == "F" else label) for key, label in bindings]
    return tuple(bindings)


class _ProfilesScreen:
    """The list (see the module docstring)."""

    what = "Profiles"

    def __init__(self, runtime: Any, palette: tui.Palette, *, selected: str | None = None,
                 card_profile: str | None = None, now: datetime | None = None):
        self.runtime = runtime
        self.palette = palette
        self.card_profile = card_profile
        self.now = now
        self.fallback_only = False
        self.message = ""
        self.message_role = "accent"
        self.offset = 0
        self.index = 0
        self.renamed: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.rows: tuple[Any, ...] = ()
        self._reload(select=selected)

    # -- data ----------------------------------------------------------------

    def _clock(self) -> datetime:
        return self.now or datetime.now(timezone.utc)

    def _reload(self, *, select: str | None = None) -> None:
        from claude_multi.setup import profiles as setup_profiles

        current = select if select is not None else (self.selected.name if self.selected is not None else None)
        try:
            self.rows = setup_profiles.rows(self.runtime, fallback_only=self.fallback_only)
        except (cli_errors.ClaudeMultiError, OSError, ValueError) as exc:
            self.rows = ()
            self._say(str(exc), "warn")
        names = [row.name for row in self.rows]
        self.index = names.index(current) if current in names else min(self.index, max(0, len(names) - 1))

    @property
    def selected(self) -> Any:
        return self.rows[self.index] if 0 <= self.index < len(self.rows) else None

    def _result(self, kind: str, name: str | None = None) -> ProfilesResult:
        return ProfilesResult(kind, name, tuple(self.renamed), tuple(self.removed))

    # -- layout --------------------------------------------------------------

    def min_size(self, width: int) -> tuple[int, int]:
        return MIN_ROWS, MIN_COLS

    def _keybar(self) -> tui.KeyBar:
        return tui.KeyBar(keybar(self.selected, fallback_only=self.fallback_only))

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows_needed, cols_needed = self.min_size(width)
        if height < rows_needed or width < cols_needed:
            screens_common._screen_too_small(win, palette, self.what, rows_needed, cols_needed)
            return
        win.erase()
        bar = self._keybar()
        bar_rows = bar.rows(width)
        message_row = height - bar_rows - 1
        separator = message_row - 3
        title = cli_text.PROFILES_TITLE_FALLBACK if self.fallback_only else cli_text.PROFILES_TITLE
        tui.safe_add(win, 1, 2, views.clip(title, width - 3), palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        now = self._clock()
        widths = column_widths(self.rows, now)
        tui.safe_add(win, 3, 4, views.clip(header_text(widths), width - 5), palette.attr("dim") | tui.curses.A_BOLD)
        visible = max(1, separator - 4)
        if self.index < self.offset:
            self.offset = self.index
        elif self.index >= self.offset + visible:
            self.offset = self.index - visible + 1
        self.offset = max(0, min(self.offset, max(0, len(self.rows) - visible)))
        if not self.rows:
            empty = cli_text.PROFILES_NO_FALLBACK if self.fallback_only else cli_text.PROFILES_NONE
            tui.safe_add(win, 4, 4, views.clip(empty, width - 5), palette.attr("dim"))
        for y, index in enumerate(range(self.offset, min(len(self.rows), self.offset + visible)), 4):
            row = self.rows[index]
            focused = index == self.index
            if focused:
                tui.safe_add(win, y, 2, "›", palette.attr("accent"))
            role = "warn" if not row.loadable else "normal"
            tui.safe_add(win, y, 4, views.clip(row_text(row, widths, now), width - 5),
                         palette.attr(role) | (tui.curses.A_REVERSE if focused else 0))
        tui.safe_add(win, separator, 2, "─", palette.attr("dim"))
        row = self.selected
        if row is not None:
            first = row.description if row.loadable else row.description or row.reason_line
            tui.safe_add(win, separator + 1, 2, views.clip(first, width - 3), palette.attr("dim"))
            role = "normal" if row.ready else "warn"
            tui.safe_add(win, separator + 2, 2, views.clip(row.reason_line, width - 3), palette.attr(role))
        if self.message:
            tui.safe_add(win, message_row, 2, views.clip(self.message, width - 3), palette.attr(self.message_role))
        bar.draw(win, height - 1, palette)
        win.refresh()

    def _say(self, text: str, role: str = "accent") -> None:
        self.message = text
        self.message_role = role

    def _fail(self, exc: BaseException) -> None:
        remedy = getattr(exc, "remedy", None)
        text = str(exc)
        self._say(text + (f" — {remedy}" if remedy and remedy not in text else ""), "warn")

    # -- small dialogs ---------------------------------------------------------

    def _free_name(self, base: str) -> str:
        name, n = base, 2
        while self._taken(name):
            name, n = f"{base}-{n}", n + 1
        return name if state_mod.SAFE_NAME.fullmatch(name) else ""

    def _taken(self, name: str) -> bool:
        try:
            return self.runtime.profiles.contains(name)
        except profile_mod.ProfileError:
            return True

    def _ask_name(self, win: Any, default: str) -> str | None:
        """The name modal: a valid name that is not taken, or None (cancelled)."""

        from claude_multi.setup import profiles as setup_profiles

        field = tui.TextInput(default, max_length=64)
        error = ""
        while True:
            lines = [setup_profiles.NAME_RULE]
            if error:
                lines += screens_common.modal_lines(error, win)
            modal = tui.Modal(cli_text.NAME_TITLE, lines, buttons=cli_text.NAME_BUTTONS, input=field)
            if not modal.run(win, self.palette, background=self._draw):
                return None
            name = field.value.strip()
            if not state_mod.SAFE_NAME.fullmatch(name):
                error = setup_profiles.NAME_BAD
            elif self._taken(name):
                error = setup_profiles.NAME_TAKEN.format(name=name)
            else:
                return name

    def _keep_fallback(self, win: Any, document: dict[str, Any]) -> bool | None:
        """Keep a copy as the fallback for its provider? No is the default;
        None: cancelled."""

        provider = document.get("primary_provider")
        if not isinstance(provider, str) or not provider:
            return False
        lines = screens_common.modal_lines(cli_text.KEEP_FALLBACK_BODY.format(provider=provider), win)
        return tui.Modal(cli_text.KEEP_FALLBACK_TITLE.format(provider=provider), lines,
                         buttons=cli_text.KEEP_FALLBACK_BUTTONS).run(win, self.palette, background=self._draw)

    def _confirm(self, win: Any, title: str, body: str, buttons: Any, *, focus: int = 1) -> Any:
        modal = tui.Modal(title, screens_common.modal_lines(body, win) if body else [], buttons=buttons)
        modal.focus = focus
        return modal.run(win, self.palette, background=self._draw)

    def _report(self, win: Any, title: str, lines: list[str]) -> None:
        if lines:
            screens_common._TextReportScreen(title, lines, self.palette).run(win)

    def _editor(self, win: Any, editor_state: tui.ProfileEditorState, *, focus: str | None = None) -> None:
        screen = tui.ProfileEditorScreen(
            editor_state,
            palette=self.palette,
            callbacks=screens_profile_editor._profile_editor_callbacks(
                self.runtime, self.palette, editor_state=editor_state),
            environ=self.runtime.environ,
            initial_focus=focus,
        )
        screen.run(win)

    # -- actions ---------------------------------------------------------------

    def _use(self) -> ProfilesResult | None:
        from claude_multi.setup import profiles as setup_profiles

        row = self.selected
        if row is None:
            return None
        if not row.loadable:
            self._say(setup_profiles.PROFILE_REASON["unloadable"], "warn")
            return None
        return self._result("use", row.name)

    def _new(self, win: Any) -> None:
        store = self.runtime.profiles
        card = self.card_profile
        card_ok = card is not None and not self._unloadable(card)
        items = [
            tui.SelectItem(cli_text.NEW_ITEMS[0]),
            tui.SelectItem(cli_text.NEW_ITEMS[1].format(card=card if card else "—"), enabled=card_ok),
            tui.SelectItem(cli_text.NEW_ITEMS[2]),
        ]
        choice = tui.SelectList(cli_text.NEW_TITLE, items, footer=cli_text.CHOICE_KEYBAR).run(
            win, self.palette, help_text=cli_text.PROFILES_HELP)
        if choice == 0:
            seeds = [name for name in readiness.SEED_ORDER if store.is_seed(name)]
            seeds += sorted(name for name in store.names() if store.is_seed(name) and name not in seeds)
            seed_items = []
            for name in seeds:
                try:
                    note = str(store.load(name).get("description") or "")
                except profile_mod.ProfileError:
                    note = ""
                seed_items.append(tui.SelectItem(name, note=views.clip(note, 50)))
            picked = tui.SelectList(cli_text.NEW_SEED_TITLE, seed_items, footer=cli_text.CHOICE_KEYBAR).run(
                win, self.palette, help_text=cli_text.PROFILES_HELP)
            if isinstance(picked, int):
                self._new_from(win, seeds[picked])
        elif choice == 1 and card_ok and card is not None:
            self._new_from(win, card)
        elif choice == 2:
            self._starter(win)

    def _unloadable(self, name: str) -> bool:
        try:
            self.runtime.profiles.load(name)
        except profile_mod.ProfileError:
            return True
        return False

    def _new_from(self, win: Any, source: str) -> None:
        """Name it, decide about the fallback, then edit the unsaved draft
        (^S saves it as a new profile; Esc discards it)."""

        try:
            document = self.runtime.profiles.load(source)
        except profile_mod.ProfileError as exc:
            self._fail(exc)
            return
        name = self._ask_name(win, self._free_name(f"{source}-mine"))
        if name is None:
            return
        keep = self._keep_fallback(win, document)
        if keep is None:
            return
        draft = {key: value for key, value in document.items() if key != "seed"}
        draft["name"] = name
        if not keep:
            draft.pop("primary_provider", None)
        try:
            editor_state = screens_profile_editor._draft_editor_state(self.runtime, draft)
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._fail(exc)
            return
        self._editor(win, editor_state, focus="general")
        saved = editor_state.saved
        if saved is not None:
            self._say(tui.PROFILE_FORM_SAVED.format(name=saved.name))
            self._reload(select=saved.name)
        else:
            self._reload()

    def _starter(self, win: Any) -> None:
        """Built from the connected providers: name, preview, save, then the
        offer to make it the default."""

        from claude_multi.setup import defaults, model, profiles as setup_profiles

        name = self._ask_name(win, self._free_name("starter"))
        if name is None:
            return
        try:
            plan = setup_profiles.plan_new_starter(self.runtime, name)
        except model.Refused:
            self._say(cli_text.STARTER_NO_LEAD_TUI, "warn")
            return
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._fail(exc)
            return
        if not tui.TextView(cli_text.STARTER_PREVIEW_TITLE, plan.lines, palette=self.palette, confirm=True).run(win):
            self._say(model.NOTHING_CHANGED)
            return
        try:
            setup_profiles.apply_new_starter(self.runtime, plan, model.Confirmation.given(plan))
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._fail(exc)
            return
        self._reload(select=name)
        made = self._confirm(win, cli_text.MAKE_DEFAULT_TITLE.format(name=name), "",
                             cli_text.MAKE_DEFAULT_BUTTONS, focus=2)
        if made is True:
            try:
                chosen = defaults.set_default_plan(self.runtime, name)
                defaults.apply_default(self.runtime, chosen, model.Confirmation.given(chosen))
            except (cli_errors.ClaudeMultiError, OSError) as exc:
                self._fail(exc)
                return
            self._say(cli_text.DEFAULT_SET.format(name=name))
        else:
            self._say(tui.PROFILE_FORM_SAVED.format(name=name))
        self._reload(select=name)

    def _edit(self, win: Any) -> None:
        row = self.selected
        if row is None:
            return
        if not row.loadable:
            self._raw_edit(win, row.name)
            return
        try:
            editor_state = screens_profile_editor._profile_editor_state(self.runtime, row.name, "edit")
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._fail(exc)
            return
        self._editor(win, editor_state)
        saved = editor_state.saved
        # Every rename that committed, even when a later save followed it.
        self.renamed.extend(editor_state.renamed)
        self._reload(select=saved.name if saved is not None else row.name)

    def _raw_edit(self, win: Any, name: str) -> None:
        """The file as it is stored, in $VISUAL or $EDITOR (curses suspended);
        a valid result is saved, an invalid one is kept and its problems shown."""

        import claude_multi.cli.commands.profile as commands_profile

        environ = self.runtime.environ
        if not (environ.get("VISUAL") or environ.get("EDITOR")):
            self._say(cli_text.RAW_EDIT_NO_EDITOR, "warn")
            return
        store = self.runtime.profiles
        try:
            digest = store.digest(name)
            raw = store.raw_bytes(name)
        except profile_mod.ProfileError as exc:
            self._fail(exc)
            return
        output = io.StringIO()
        with tui.suspended_curses(win):
            try:
                saved = commands_profile._edit_profile(self.runtime, None, name=name, verb="edit",
                                                       output_stream=output, raw=raw, expected=digest)
            except (cli_errors.ClaudeMultiError, OSError) as exc:
                saved = False
                output.write(f"{exc}\n")
        lines = output.getvalue().splitlines()
        if saved:
            self._say(lines[-1] if lines else tui.PROFILE_FORM_SAVED.format(name=name))
        else:
            tui.TextView(cli_text.RAW_EDIT_TITLE.format(name=name), lines, palette=self.palette).run(win)
        self._reload(select=name)

    def _copy(self, win: Any) -> None:
        from claude_multi.setup import model, profiles as setup_profiles

        row = self.selected
        if row is None:
            return
        if not row.loadable:
            self._say(setup_profiles.PROFILE_REASON["unloadable"], "warn")
            return
        try:
            document = self.runtime.profiles.load(row.name)
        except profile_mod.ProfileError as exc:
            self._fail(exc)
            return
        name = self._ask_name(win, self._free_name(f"{row.name}-copy"))
        if name is None:
            return
        keep = self._keep_fallback(win, document)
        if keep is None:
            return
        try:
            plan = setup_profiles.plan_copy(self.runtime, row.name, name, keep_fallback=keep)
            setup_profiles.apply_copy(self.runtime, plan, model.Confirmation.given(plan))
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._fail(exc)
            return
        self._say(setup_profiles.COPIED.format(source=row.name, target=name))
        self._reload(select=name)

    def _rename(self, win: Any) -> None:
        from claude_multi.setup import defaults, model, profiles as setup_profiles

        row = self.selected
        if row is None:
            return
        if row.origin == "seed":
            self._say(setup_profiles.SEED_NO_RENAME.format(name=row.name), "warn")
            return
        if not row.loadable:
            self._say(setup_profiles.PROFILE_REASON["unloadable"], "warn")
            return
        found, _unreadable = lineup_mod.followers(self.runtime, [row.name])
        running = sum(1 for item in found if item.live)
        is_default = defaults.chosen_default(self.runtime) == row.name
        if found or is_default:
            body = (setup_profiles.FOLLOWERS_LINE.format(n=len(found), name=row.name, m=running) if found else "")
            body += setup_profiles.DEFAULT_MOVES if is_default else ""
            if not self._confirm(win, cli_text.RENAME_TITLE.format(name=row.name), body.rstrip("\n"),
                                 cli_text.RENAME_BUTTONS):
                return
        name = self._ask_name(win, row.name)
        if name is None:
            return
        try:
            plan = setup_profiles.plan_rename(self.runtime, row.name, name)
            outcome = setup_profiles.apply_rename(self.runtime, plan, model.Confirmation.given(plan))
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._fail(exc)
            return
        self.renamed.append((row.name, name))
        self._say(outcome.lines[0])
        self._report(win, f"propagation — {name}", list(outcome.lines[1:]))
        self._reload(select=name)

    def _delete(self, win: Any) -> None:
        from claude_multi.setup import model, profiles as setup_profiles

        row = self.selected
        if row is None:
            return
        if row.origin == "seed":
            self._say(setup_profiles.SEED_NO_DELETE.format(name=row.name), "warn")
            return
        try:
            plan = setup_profiles.plan_delete(self.runtime, row.name)
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._fail(exc)
            return
        if not self._confirm(win, cli_text.DELETE_TITLE.format(name=row.name), "\n".join(plan.lines),
                             cli_text.DELETE_BUTTONS):
            return
        try:
            outcome = setup_profiles.apply_delete(self.runtime, plan, model.Confirmation.given(plan))
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._fail(exc)
            return
        self.removed.append(row.name)
        self._say(outcome.lines[0])
        self._report(win, f"propagation — {row.name}", list(outcome.lines[1:]))
        self._reload()

    def _reseed(self, win: Any) -> None:
        from claude_multi.setup import model, profiles as setup_profiles

        row = self.selected
        if row is None:
            return
        if row.origin != "seed":
            self._say(setup_profiles.NOT_A_SEED.format(name=row.name), "warn")
            return
        try:
            if row.loadable and row.seed_state == "unedited-stale":
                plan = setup_profiles.plan_refresh_unedited(self.runtime)
                if plan.names:
                    self._refresh(win, plan)
                    return
            plan_one = setup_profiles.plan_reseed(self.runtime, row.name)
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._fail(exc)
            return
        if plan_one.nothing:
            self._say(plan_one.lines[0])
            return
        view = tui.TextView(cli_text.RESEED_VIEW_TITLE.format(name=row.name, v=plan_one.version),
                            plan_one.lines, palette=self.palette, confirm=True)
        if not view.run(win):
            self._say(model.NOTHING_CHANGED)
            return
        try:
            outcome = setup_profiles.apply_reseed(self.runtime, plan_one, model.Confirmation.given(plan_one))
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._fail(exc)
            return
        self._say(outcome.lines[0])
        screens_propagation.run_propagation_screen(self.runtime, win, self.palette, [row.name])
        self._reload(select=row.name)

    def _refresh(self, win: Any, plan: Any) -> None:
        from claude_multi.setup import model, profiles as setup_profiles

        these = "it" if len(plan.names) == 1 else "them"
        body = cli_text.REFRESH_BODY.format(these=these, versions=plan.versions)
        if not self._confirm(win, cli_text.REFRESH_TITLE.format(names=", ".join(plan.names)), body,
                             cli_text.REFRESH_BUTTONS):
            return
        try:
            done = setup_profiles.apply_refresh_unedited(self.runtime, plan, model.Confirmation.given(plan))
        except setup_profiles.RefreshPartial as exc:
            # Some were updated before the failure: their followers still get the offer.
            self._fail(exc)
            screens_propagation.run_propagation_screen(self.runtime, win, self.palette, list(exc.done))
            self._reload()
            return
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._fail(exc)
            return
        self._say(cli_text.PROFILES_REFRESHED.format(names=", ".join(done)))
        if done:
            screens_propagation.run_propagation_screen(self.runtime, win, self.palette, list(done))
        self._reload()

    def _default(self, win: Any) -> None:
        from claude_multi.setup import defaults, model

        row = self.selected
        if row is None:
            return
        name: str | None = None if row.is_default else row.name
        if name is not None and row.loadable and not row.ready:
            why = row.state_text[:1].upper() + row.state_text[1:]
            if not self._confirm(win, cli_text.DEFAULT_NOT_READY_TITLE.format(name=row.name),
                                 cli_text.DEFAULT_NOT_READY_BODY.format(why=why),
                                 cli_text.DEFAULT_NOT_READY_BUTTONS):
                return
        try:
            plan = defaults.set_default_plan(self.runtime, name)
            defaults.apply_default(self.runtime, plan, model.Confirmation.given(plan))
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._fail(exc)
            return
        self._say(defaults.DEFAULT_CLEARED if name is None else cli_text.DEFAULT_SET.format(name=name))
        self._reload(select=row.name)

    def _bindings(self, win: Any) -> None:
        """B: the named bindings, with the editor's store access (a change
        runs the propagation offer for the profiles that use it)."""

        import claude_multi.cli.screens.profile_editor as screens_profile_editor

        screens_profile_editor.run_named_bindings(self.runtime, win, self.palette)
        self._reload(select=self.selected.name if self.selected is not None else None)

    def _help(self, win: Any) -> None:
        wrap = max(20, min(100, win.getmaxyx()[1] - 8))
        screens_common._ScrollModal(cli_text.PROFILES_HELP_TITLE,
                                    screens_common._wrap_help_lines(cli_text.PROFILES_HELP, wrap)).run(
            win, self.palette, background=self._draw)

    # -- the loop ----------------------------------------------------------------

    def run(self, win: Any) -> ProfilesResult:
        tui.hide_cursor()
        actions: dict[str, Callable[[Any], None]] = {
            "N": self._new, "E": self._edit, "C": self._copy, "R": self._rename, "X": self._delete,
            "U": self._reseed, "D": self._default, "B": self._bindings,
        }
        while True:
            self._draw(win)
            key = tui.read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "ctrl" and key.ch == "c":
                raise KeyboardInterrupt
            if key.kind == "esc":
                return self._result("back")
            height, width = win.getmaxyx()
            rows_needed, cols_needed = self.min_size(width)
            if height < rows_needed or width < cols_needed:
                continue
            self.message = ""
            letter = key.ch.upper() if key.kind == "char" and key.ch else ""
            if key.kind == "up":
                self.index = max(0, self.index - 1)
            elif key.kind == "down":
                self.index = min(max(0, len(self.rows) - 1), self.index + 1)
            elif key.kind == "home":
                self.index = 0
            elif key.kind == "end":
                self.index = max(0, len(self.rows) - 1)
            elif key.kind == "enter":
                result = self._use()
                if result is not None:
                    return result
            elif letter == "?":
                self._help(win)
            elif letter == "W":
                return self._result("get-started")
            elif letter == "F":
                self.fallback_only = not self.fallback_only
                self._reload()
            elif letter in actions:
                actions[letter](win)


def run_profiles_screen(runtime: Any, win: Any, palette: tui.Palette, *, selected: str | None = None,
                        card_profile: str | None = None, now: datetime | None = None) -> ProfilesResult:
    """Open Profiles on ``win`` with ``selected`` highlighted; ``card_profile``
    is the profile on the card (a source for N)."""

    return _ProfilesScreen(runtime, palette, selected=selected, card_profile=card_profile, now=now).run(win)
