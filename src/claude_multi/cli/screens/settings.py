"""The Settings screen."""

from __future__ import annotations

from claude_multi import choices as choices_mod
from claude_multi import compiler
from claude_multi import custom
from claude_multi import errors as cli_errors
from claude_multi import profile as profile_mod
from claude_multi import proxy as proxy_mod
from claude_multi import scope as scope_mod
from claude_multi import sessions
from claude_multi import settings as settings_mod
from claude_multi import state
from claude_multi import termtext
from claude_multi import tui
from claude_multi import views
from datetime import datetime
from typing import Any
from typing import Callable
from typing import Mapping
from typing import TextIO
import dataclasses
import textwrap
import claude_multi.cli.doctor as doctor
import claude_multi.cli.doctor_actions as doctor_actions
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.models as screens_models
import claude_multi.cli.screens.providers as screens_providers
import claude_multi.cli.text as cli_text
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


# The settings.json fields edited here -> (default, bounds or None).
_SETTINGS_FIELDS: Mapping[str, tuple[Any, tuple[int, int] | None]] = {
    settings_mod.COMPACTION_PERCENT_KEY: (
        settings_mod.COMPACTION_PERCENT_DEFAULT,
        (settings_mod.COMPACTION_PERCENT_MIN, settings_mod.COMPACTION_PERCENT_MAX),
    ),
    settings_mod.EXPLORE_INHERIT_CAP_DISABLED_KEY: (
        settings_mod.EXPLORE_INHERIT_CAP_DISABLED_DEFAULT, None
    ),
    settings_mod.WORKFLOW_DEFAULT_BINDING_KEY: (None, None),
    settings_mod.REVIEW_ROUND_CAP_KEY: (
        settings_mod.REVIEW_ROUND_CAP_DEFAULT,
        (settings_mod.REVIEW_ROUND_CAP_MIN, settings_mod.REVIEW_ROUND_CAP_MAX),
    ),
}


# Host preferences edited here (preferences.json, not settings.json).
_PREFERENCE_FIELDS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    settings_mod.FEEDBACK_DRAFTS_KEY: (settings_mod.FEEDBACK_DRAFTS_DEFAULT, settings_mod.FEEDBACK_DRAFTS_VALUES),
}


class _SettingsScreen:
    """The Settings screen.

    ``compaction_percent``, "Explore follows lead model"
    (``explore_inherit_cap_disabled``), ``workflow_default_binding`` and
    ``review_round_cap`` are edited through ``SettingsStore.update``;
    provider enablement and New admission are summary rows that open the
    Providers and Models screens; the token row is the ``doctor
    --rotate-token`` entry (``tty_in``/``tty_out`` are the card's streams).
    The kept environment and the default profile are choices of their own
    (``choices.json``).  The ``→ effective`` row is always ``views.effective_context``
    over ``card_lineup`` with a fresh ``Effective``, recomputed after every
    successful write; it never reads a prepared record.
    """

    what = "the settings screen"

    def __init__(
        self,
        runtime: runtime_mod.Runtime,
        palette: tui.Palette,
        *,
        card_lineup: profile_mod.ResolvedLineup | None = None,
        tty_in: TextIO,
        tty_out: TextIO,
        now: datetime | None = None,
    ):
        self.runtime = runtime
        self.palette = palette
        self.card_lineup = card_lineup
        self.tty_in = tty_in
        self.tty_out = tty_out
        self.now = now
        self.message = ""
        self.message_role = "accent"
        self.offset = 0
        self._load()
        self.index = next(i for i, entry in enumerate(self.entries) if self._selectable(entry))

    # -- data --------------------------------------------------------------

    def _load(self) -> None:
        runtime = self.runtime
        self.banner: str | None = None
        store = runtime.settings_store
        doc: dict[str, Any] | None
        try:
            doc = store.load()
            eff = runtime.settings_effective()
        except (settings_mod.SettingsError, cli_errors.CLIError) as exc:
            cause = exc.__cause__ if isinstance(exc, cli_errors.CLIError) and exc.__cause__ else exc
            self.banner = f"settings.json is invalid: {cause} — fix or remove {store.path}"
            doc = None
            eff = screens_common._default_effective(runtime)
        # The ceiling lives in choices.json: shown (and edited) on its own row.
        self.ceiling = runtime.window_ceiling()
        eff = dataclasses.replace(eff, window_ceiling=self.ceiling.value)
        self.doc = doc
        self.eff = eff
        overrides: dict[str, int] = {}
        profiles = runtime.profiles
        for name in profiles.names():
            try:
                document = profiles.load(name)
            except profile_mod.ProfileError:
                continue
            values = document.get("settings_overrides")
            if isinstance(values, Mapping) and settings_mod.COMPACTION_PERCENT_KEY in values:
                overrides[name] = values[settings_mod.COMPACTION_PERCENT_KEY]
        self.overrides = overrides
        context = None
        self.context_error: str | None = None
        lineup = self.card_lineup
        if lineup is not None:
            try:
                resolved = views.effective_context(runtime, lineup, eff)
            except (scope_mod.ScopeError, settings_mod.SettingsError, compiler.CompilerError) as exc:
                self.context_error = str(exc)
            else:
                context = (
                    lineup.name or "direct",
                    views.short_label(lineup.lead.binding.display),
                    resolved,
                )
        self.context = context
        try:
            mtime: float | None = (proxy_mod.config_dir(runtime.home) / "api-key").lstat().st_mtime
        except OSError:
            mtime = None
        rotating = doctor._token_rotation_attention(runtime) is not None
        self.feedback = settings_mod.feedback_drafts_preference(runtime.environ)
        from claude_multi.setup import defaults

        self.rows = views.settings_rows(
            doc, eff, overrides=overrides, context=context, token_state=(mtime, rotating),
            lcat=runtime.lineup_catalog(), feedback_drafts=self.feedback,
            default_profile=(defaults.chosen_default(runtime),),
            window_ceiling=(self.ceiling.value, self.ceiling.is_set, self.ceiling.problem),
        )
        self.rows += (self._env_keep_row(),)
        entries: list[tuple[str, Any]] = []
        for section in views.SETTINGS_SECTIONS:
            members = [row for row in self.rows if row.section == section]
            if members:
                entries.append(("section", section))
                entries.extend(("row", row) for row in members)
        self.entries = entries

    @staticmethod
    def _selectable(entry: tuple[str, Any]) -> bool:
        return entry[0] == "row" and entry[1].key != "effective"

    @property
    def selected_row(self) -> views.SettingsRow | None:
        if 0 <= self.index < len(self.entries) and self._selectable(self.entries[self.index]):
            return self.entries[self.index][1]
        return None

    def _row(self, key: str) -> views.SettingsRow:
        return next(row for row in self.rows if row.key == key)

    # -- layout ------------------------------------------------------------

    def _banner_lines(self, width: int) -> list[str]:
        return screens_common._wrapped(self.banner, width - 4) if self.banner else []

    def _detail_lines(self, width: int) -> list[str]:
        row = self.selected_row
        if row is None or not row.detail:
            return []
        lines = textwrap.wrap(row.detail, max(20, width - 4))
        if len(lines) > cli_text.SETTINGS_DETAIL_LINES:
            rest = " ".join(lines[cli_text.SETTINGS_DETAIL_LINES - 1 :])
            lines = lines[: cli_text.SETTINGS_DETAIL_LINES - 1] + [views.clip(rest, max(20, width - 4))]
        return lines

    def min_size(self, width: int) -> tuple[int, int]:
        """(rows, cols) floor (§3.4): top/title/rule, banner, sections, effective, detail, message."""

        sections = sum(1 for kind, _ in self.entries if kind == "section")
        chrome = 3 + len(self._banner_lines(width)) + sections + 1 + cli_text.SETTINGS_DETAIL_LINES + 1
        list_rows = sum(1 for entry in self.entries if self._selectable(entry))
        floor = views.ScreenFloor.compute(
            chrome=chrome,
            list_rows=list_rows,
            detail_reserve=0,
            bar_rows=tui.KeyBar(cli_text.SETTINGS_KEYBAR).rows(width),
            cols=cli_text.SETTINGS_MIN_COLS,
        )
        return floor.as_tuple()

    # -- actions -------------------------------------------------------

    def _can_edit(self, row: views.SettingsRow) -> bool:
        """Pure: the edit/reset predicate ``_writable`` enforces (no message)."""

        return (
            _choice_row(row)
            and row.editable
            and bool(self.runtime.allow_state_writes)
        )

    def keybar_bindings(self) -> tuple[tuple[str, str], ...]:
        """The footer for the selected row: its supported actions only."""

        row = self.selected_row
        tail = (("?", "help"), ("Esc", "back"))
        if row is None:
            return tail
        if row.edit in views.SETTINGS_NAVIGATION_EDITS:
            return cli_text.SETTINGS_KEYBAR_OPEN
        if row.key == views.SETTINGS_ENV_KEEP_KEY:
            return self._env_keep_keybar(row)
        if row.edit == "token":
            if self.runtime.allow_state_writes:
                return cli_text.SETTINGS_KEYBAR_ROTATE
            return ((cli_text.SETTINGS_READ_ONLY_KEY, cli_text.SETTINGS_READ_ONLY_COMMAND), *tail)
        if self._can_edit(row):
            return cli_text.SETTINGS_KEYBAR
        if row.key == views.SETTINGS_CEILING_KEY and self.runtime.allow_state_writes:
            reason = cli_text.SETTINGS_READ_ONLY_CHOICES
        elif not row.edit:
            reason = cli_text.SETTINGS_READ_ONLY_EVIDENCE
        elif not self.runtime.allow_state_writes:
            reason = cli_text.SETTINGS_READ_ONLY_COMMAND
        else:
            reason = cli_text.SETTINGS_READ_ONLY_INVALID
        return ((cli_text.SETTINGS_READ_ONLY_KEY, reason), *tail)

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows_needed, cols_needed = self.min_size(width)
        if height < rows_needed or width < cols_needed:
            screens_common._screen_too_small(win, palette, self.what, rows_needed, cols_needed)
            return
        win.erase()
        keybar = tui.KeyBar(self.keybar_bindings())
        bar_rows = keybar.rows(width)
        tui.safe_add(win, 1, 2, cli_text.SETTINGS_TITLE, palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        row = 3
        for line in self._banner_lines(width):
            tui.safe_add(win, row, 2, line, palette.attr("warn"))
            row += 1
        message_lines = (textwrap.wrap(self.message, max(1, width - 4))
                         if self.message_role == "warn" else [views.clip(self.message, width - 3)])
        message_row = height - bar_rows - max(1, len(message_lines))
        detail_top = message_row - cli_text.SETTINGS_DETAIL_LINES
        visible = max(1, detail_top - row)
        if self.index < self.offset:
            self.offset = self.index
        elif self.index >= self.offset + visible:
            self.offset = self.index - visible + 1
        self.offset = max(0, min(self.offset, max(0, len(self.entries) - visible)))
        shown = range(self.offset, min(len(self.entries), self.offset + visible))
        for index in shown:
            kind, value = self.entries[index]
            if kind == "section":
                tui.safe_add(win, row, 2, value, palette.attr("dim") | tui.curses.A_BOLD)
            else:
                focused = index == self.index
                role = "normal" if value.editable or value.key == "effective" else "dim"
                text = value.text(width)
                attr = palette.attr(role) | (tui.curses.A_REVERSE if focused else 0)
                tui.safe_add(win, row, 2, text[2:], attr)
            row += 1
        for offset, line in enumerate(self._detail_lines(width)):
            tui.safe_add(win, row + offset, 2, line, palette.attr("dim"))
        for offset, line in enumerate(message_lines):
            tui.safe_add(win, message_row + offset, 2, line, palette.attr(self.message_role))
        keybar.draw(win, height - 1, palette)
        win.refresh()

    # -- writes ------------------------------------------------------------

    def _say(self, text: str, role: str = "accent") -> None:
        self.message = text
        self.message_role = role

    def _failure(self, exc: BaseException) -> str:
        """Keep validation text; filesystem failures carry the shared remedy."""

        cause = exc
        while cause is not None:
            text = state.failure_text(cause, self.runtime.environ)
            if text is not None:
                return text
            cause = cause.__cause__
        return str(exc)

    def _update(self, mutate: Callable[[dict[str, Any]], None]) -> str | None:
        """One ``SettingsStore.update``; the error text, or None after a reload."""

        try:
            self.runtime.settings_store.update(
                mutate,
                # The merged provider/line sets (operator ids included).
                catalog=self.runtime.lineup_catalog(),
                custom_registry=custom.load_registry(self.runtime.environ),
            )
        except (
            settings_mod.SettingsError,
            sessions.StateMarkerError,
            state.StateError,
            custom.CustomModelsError,
            OSError,
        ) as exc:
            return self._failure(exc)
        self._load()
        return None

    def _saved(self, label: str, value: str) -> None:
        self._say(
            f"saved {label} = {value} — {cli_text._APPLIES_NEXT}; running sessions show "
            '"settings changed"'
        )

    def _set(self, row: views.SettingsRow, value: Any, shown: str) -> None:
        error = self._update(lambda document: document.__setitem__(row.key, value))
        if error is not None:
            self._say(error, "warn")
            return
        self._saved(row.label, shown)

    def _writable(self, row: views.SettingsRow) -> bool:
        if not _choice_row(row) or not row.editable:
            self._say(f"{row.label} is not editable here", "warn")
            return False
        if not self.runtime.allow_state_writes:
            self._say(cli_text.SETTINGS_READ_ONLY, "warn")
            return False
        return True

    def _update_preference(self, mutate: Callable[[dict[str, Any]], None]) -> str | None:
        """One ``PreferencesStore.update`` (its own leaf lock); the error or None."""

        try:
            self.runtime.preferences_store.update(mutate)
        except (settings_mod.SettingsError, sessions.StateMarkerError, state.StateError, OSError) as exc:
            return self._failure(exc)
        self._load()
        return None

    def _cycle_preference(self, row: views.SettingsRow) -> None:
        _default, values = _PREFERENCE_FIELDS[row.key]
        current = row.value if row.value in values else _default
        value = values[(values.index(current) + 1) % len(values)]
        error = self._update_preference(lambda document: document.__setitem__(row.key, value))
        if error is not None:
            self._say(error, "warn")
            return
        self._say(
            f"saved {row.label} = {value} — {cli_text._APPLIES_NEXT}; running sessions keep "
            "their launch value (doctor: managed-session preference changed)"
        )

    def _edit_int(self, win: Any, row: views.SettingsRow) -> None:
        default, bounds = _SETTINGS_FIELDS[row.key]
        assert bounds is not None
        low, high = bounds
        current = getattr(self.eff, row.key)
        error: str | None = None
        while True:
            lines = [f"current {current} · default {default} · range {low}–{high}"]
            if error:
                lines.extend(textwrap.wrap(error, max(20, min(60, win.getmaxyx()[1] - 8))))
            modal = tui.Modal(
                row.label, lines, buttons=(("Save", True), ("Cancel", False)),
                input=tui.TextInput(max_length=6),
            )
            confirmed = modal.run(win, self.palette, background=self._draw)
            text = modal.input.value.strip()
            if not confirmed or not text:
                return
            try:
                value = int(text, 10)
            except ValueError:
                error = f"{row.label}: {text!r} is not a whole number"
                continue
            error = self._update(lambda document: document.__setitem__(row.key, value))
            if error is None:
                self._saved(row.label, str(value))
                return

    def _edit_ceiling(self, win: Any, row: views.SettingsRow) -> None:
        """The context window ceiling (``choices.json``): a token count or
        thousands (``400K``); out of range is refused with the range."""

        current = self.ceiling.value
        error: str | None = None
        while True:
            lines = [cli_text.SETTINGS_CEILING_PROMPT.format(
                current=profile_mod.format_tokens(current),
                default=profile_mod.format_tokens(choices_mod.WINDOW_CEILING_DEFAULT),
                range=views.SETTINGS_CEILING_RANGE)]
            if error:
                lines.extend(textwrap.wrap(error, max(20, min(60, win.getmaxyx()[1] - 8))))
            modal = tui.Modal(
                row.label, lines, buttons=(("Save", True), ("Cancel", False)),
                input=tui.TextInput(max_length=9),
            )
            confirmed = modal.run(win, self.palette, background=self._draw)
            text = modal.input.value.strip()
            if not confirmed or not text:
                return
            try:
                value = choices_mod.parse_window_ceiling(text)
            except choices_mod.ChoicesError as exc:
                error = str(exc)
                continue
            error = self._update_ceiling(value)
            if error is None:
                self._say(cli_text.SETTINGS_CEILING_SAVED.format(
                    label=row.label, value=profile_mod.format_tokens(value), applies=cli_text._APPLIES_NEXT))
                return

    def _update_ceiling(self, value: int) -> str | None:
        """One ``choices.update`` of the ceiling (its own leaf lock); the error or None."""

        try:
            choices_mod.update(self.runtime.environ, **{choices_mod.WINDOW_CEILING_KEY: value})
        except (choices_mod.ChoicesError, state.StateError, OSError) as exc:
            return self._failure(exc)
        self._load()
        return None

    def _step_int(self, row: views.SettingsRow, delta: int) -> None:
        _default, bounds = _SETTINGS_FIELDS[row.key]
        assert bounds is not None
        current = getattr(self.eff, row.key)
        value = min(bounds[1], max(bounds[0], current + delta))
        if value != current:
            self._set(row, value, str(value))

    def _edit_workflow(self, win: Any) -> None:
        runtime = self.runtime
        lcat = runtime.lineup_catalog()
        rows = views.line_rows(
            lcat, self.eff, custom_ids=frozenset(custom.load_registry(runtime.environ)["models"])
        )
        picker = views.picker_rows(
            rows, slot="workflow", bindings={}, lcat=lcat, eff=self.eff,
            current=self.eff.workflow_default_binding,
        )
        # The binding picker, slot "workflow".
        chosen = tui.BindingPicker(picker, palette=self.palette).run(win)
        if chosen is None:
            return
        label = self._row(settings_mod.WORKFLOW_DEFAULT_BINDING_KEY).label
        if chosen[0] == "off":
            error = self._update(
                lambda document: document.pop(settings_mod.WORKFLOW_DEFAULT_BINDING_KEY, None)
            )
            if error is not None:
                self._say(error, "warn")
            else:
                self._saved(label, "off")
            return
        _kind, key, effort = chosen
        binding = {"model": key, "effort": effort}
        error = self._update(
            lambda document: document.__setitem__(settings_mod.WORKFLOW_DEFAULT_BINDING_KEY, binding)
        )
        if error is not None:
            self._say(error, "warn")
            return
        short = next((r.short for r in rows if r.key == key), key)
        self._saved(label, f"{short} · {effort}")

    def _choose_default(self, win: Any) -> None:
        """The default profile: automatic first, then every profile in the
        ready-first order, marked connected or with the fix it needs."""

        from claude_multi.setup import defaults

        runtime = self.runtime
        store = runtime.profiles
        loadable, _failed = defaults.split_loadable(store, store.names())
        verdicts = defaults.profile_verdicts(runtime, loadable)
        choices = [choice for choice in defaults.profile_choices(runtime, verdicts=verdicts) if choice.loadable]
        chosen = defaults.chosen_default(runtime)
        items = [tui.SelectItem(views.SETTINGS_DEFAULT_AUTOMATIC)]
        names: list[str | None] = [None]
        for choice in choices:
            if choice.ready:
                note = cli_text.SETTINGS_DEFAULT_CONNECTED
            else:
                note = defaults.summarize_for(runtime, verdicts.get(choice.name, ())).state_text
            items.append(tui.SelectItem(choice.name, note=note))
            names.append(choice.name)
        picker = tui.SelectList(cli_text.SETTINGS_DEFAULT_TITLE, items, footer=cli_text.CHOICE_KEYBAR,
                                selected=names.index(chosen) if chosen in names else 0)
        index = picker.run(win, self.palette, help_text=views.SETTINGS_DEFAULT_DETAIL)
        if not isinstance(index, int):
            return
        self._set_default(names[index])

    def _set_default(self, name: str | None) -> None:
        from claude_multi.setup import defaults, model

        try:
            plan = defaults.set_default_plan(self.runtime, name)
            defaults.apply_default(self.runtime, plan, model.Confirmation.given(plan))
        except (cli_errors.ClaudeMultiError, OSError) as exc:
            self._say(self._failure(exc), "warn")
            return
        self._load()
        self._say(defaults.DEFAULT_CLEARED if name is None else cli_text.DEFAULT_SET.format(name=name))

    def _reset(self, win: Any, row: views.SettingsRow) -> None:
        if row.key == views.SETTINGS_ENV_KEEP_KEY:
            self._reset_env_keep(win, row)
            return
        if not self._writable(row):
            return
        confirmed = tui.Modal(
            f"Reset {row.label} to its default?", [], buttons=(("Reset", True), ("Cancel", False)), default=False,
        ).run(win, self.palette, background=self._draw)
        if not confirmed:
            return
        if row.key == views.SETTINGS_DEFAULT_KEY:
            self._set_default(None)
            return
        if row.key == views.SETTINGS_CEILING_KEY:
            error = self._update_ceiling(choices_mod.WINDOW_CEILING_DEFAULT)
            if error is not None:
                self._say(error, "warn")
                return
            self._say(f"reset {row.label} to its default — {cli_text._APPLIES_NEXT}")
            return
        if row.key in _PREFERENCE_FIELDS:
            error = self._update_preference(lambda document: document.pop(row.key, None))
            if error is not None:
                self._say(error, "warn")
                return
            self._say(f"reset {row.label} to its default — {cli_text._APPLIES_NEXT}")
            return
        error = self._update(lambda document: document.pop(row.key, None))
        if error is not None:
            self._say(error, "warn")
            return
        self._say(
            f"reset {row.label} to its default — {cli_text._APPLIES_NEXT}; running sessions show "
            '"settings changed"'
        )

    def _rotate(self, win: Any) -> None:
        if not self.runtime.allow_state_writes:
            self._say(cli_text.SETTINGS_READ_ONLY, "warn")
            return
        wrap = max(20, min(60, win.getmaxyx()[1] - 8))
        confirmed = tui.Modal(
            cli_text.SETTINGS_TOKEN_TITLE, textwrap.wrap(cli_text.SETTINGS_TOKEN_BODY, wrap),
            buttons=(("Rotate", True), ("Cancel", False)), default=False,
        ).run(win, self.palette, background=self._draw)
        if not confirmed:
            return
        with tui.suspended_curses(win):
            try:
                code = doctor_actions._doctor_rotate_token(
                    self.runtime,
                    input_stream=self.tty_in,
                    output_stream=self.tty_out,
                    interactive=True,
                )
            except (cli_errors.CLIError, proxy_mod.ProxyError, OSError) as exc:
                self.tty_out.write(f"claude-multi: {termtext.visible_message(self._failure(exc))}\n")
                code = 1
            self.tty_out.write("Press Enter to return")
            self.tty_out.flush()
            self.tty_in.readline()
        self._load()
        if code == 0:
            self._say("token rotation finished")
        else:
            self._say(
                f"token rotation did not finish (exit {code}): finish with "
                "`claude-multi doctor --rotate-token`",
                "warn",
            )

    def _open(self, win: Any, target: str) -> None:
        if target == "providers":
            screens_providers._ProvidersScreen(self.runtime, palette=self.palette).run(win)
        else:
            screens_models._ModelsScreen(self.runtime, palette=self.palette).run(win)
        self._load()

    def _enter(self, win: Any) -> None:
        row = self.selected_row
        if row is None:
            return
        if row.edit in ("providers", "models"):
            self._open(win, row.edit)
            return
        if row.key == views.SETTINGS_ENV_KEEP_KEY:
            self._edit_env_keep(win, row)
            return
        if row.edit == "token":
            self._rotate(win)
            return
        if not self._writable(row):
            return
        if row.edit == "int":
            self._edit_int(win, row)
        elif row.edit == "toggle":
            value = not bool(getattr(self.eff, row.key))
            self._set(row, value, "on" if value else "off")
        elif row.edit == "choice":
            self._cycle_preference(row)
        elif row.edit == "picker":
            self._edit_workflow(win)
        elif row.edit == "default-profile":
            self._choose_default(win)
        elif row.edit == "ceiling":
            self._edit_ceiling(win, row)

    # -- the kept environment (choices.json) ---------------------------------

    def _credential_names(self) -> frozenset[str]:
        try:
            return compiler.credential_env_names(doctor._provider_secret_names(self.runtime))
        except (KeyError, TypeError, cli_errors.ClaudeMultiError):
            return frozenset()

    def _env_keep_row(self) -> views.SettingsRow:
        from claude_multi import choices as choices_mod

        try:
            names = list(choices_mod.read(self.runtime.environ).get(views.SETTINGS_ENV_KEEP_KEY))
        except choices_mod.ChoicesError as exc:
            return views.settings_env_keep_row((), problem=str(exc))
        from claude_multi import secret_store

        refused = choices_mod.env_keep_problems(names, credential_names=self._credential_names())
        # A hand-edited entry that looks like a key value is never drawn.
        return views.settings_env_keep_row([secret_store.shown_env_name(name) for name in names], refused=refused)

    def _env_keep_keybar(self, row: views.SettingsRow) -> tuple[tuple[str, str], ...]:
        if not row.editable:
            return ((cli_text.SETTINGS_READ_ONLY_KEY, cli_text.SETTINGS_ENV_KEEP_INVALID), ("?", "help"),
                    ("Esc", "back"))
        if not self.runtime.allow_state_writes:
            return ((cli_text.SETTINGS_READ_ONLY_KEY, cli_text.SETTINGS_READ_ONLY_COMMAND), ("?", "help"),
                    ("Esc", "back"))
        return cli_text.SETTINGS_KEYBAR

    def _env_keep_writable(self, row: views.SettingsRow) -> bool:
        if not row.editable:
            self._say(f"{row.label} is not editable here: {cli_text.SETTINGS_ENV_KEEP_INVALID}", "warn")
            return False
        if not self.runtime.allow_state_writes:
            self._say(cli_text.SETTINGS_READ_ONLY, "warn")
            return False
        return True

    def _save_env_keep(self, names: list[str]) -> str | None:
        from claude_multi import choices as choices_mod

        try:
            choices_mod.set_session_env_keep(self.runtime.environ, names, credential_names=self._credential_names())
        except choices_mod.ChoicesError as exc:
            return cli_text.SETTINGS_ENV_KEEP_REFUSED.format(
                reasons=str(exc).removeprefix("choice session_env_keep refuses "))
        except (state.StateError, OSError) as exc:
            return self._failure(exc)
        self._load()
        return None

    def _edit_env_keep(self, win: Any, row: views.SettingsRow) -> None:
        """Enter: the kept names, separated by spaces or commas; the whole
        list is checked and refused with every reason, names only."""

        if not self._env_keep_writable(row):
            return
        from claude_multi import choices as choices_mod, secret_store

        try:
            listed = list(choices_mod.read(self.runtime.environ).get(views.SETTINGS_ENV_KEEP_KEY))
        except choices_mod.ChoicesError:
            listed = []
        # Only plain names are prefilled: an entry that looks like a key
        # value is never drawn, so saving the list drops it.
        current = " ".join(name for name in listed if secret_store.shown_env_name(name) == name)
        error: str | None = None
        while True:
            wrap = max(20, min(60, win.getmaxyx()[1] - 8))
            lines = textwrap.wrap(cli_text.SETTINGS_ENV_KEEP_PROMPT, wrap)
            if error:
                lines.extend(textwrap.wrap(error, wrap))
            modal = tui.Modal(row.label, lines, buttons=(("Save", True), ("Cancel", False)),
                              input=tui.TextInput(current, max_length=1024))
            confirmed = modal.run(win, self.palette, background=self._draw)
            text = modal.input.value.strip()
            if not confirmed or not text:
                return
            names = [name for name in text.replace(",", " ").split() if name]
            error = self._save_env_keep(names)
            if error is None:
                self._say(cli_text.SETTINGS_ENV_KEEP_SAVED.format(names=", ".join(names),
                                                                  applies=cli_text._APPLIES_NEXT))
                return
            current = text

    def _reset_env_keep(self, win: Any, row: views.SettingsRow) -> None:
        if not self._env_keep_writable(row):
            return
        confirmed = tui.Modal(
            f"Reset {row.label} to its default?", [cli_text.SETTINGS_ENV_KEEP_RESET_BODY],
            buttons=(("Reset", True), ("Cancel", False)), default=False,
        ).run(win, self.palette, background=self._draw)
        if not confirmed:
            return
        error = self._save_env_keep([])
        if error is not None:
            self._say(error, "warn")
            return
        self._say(f"reset {row.label} to its default (none) — {cli_text._APPLIES_NEXT}")

    def _move(self, direction: int) -> None:
        index = self.index
        while 0 <= index + direction < len(self.entries):
            index += direction
            if self._selectable(self.entries[index]):
                self.index = index
                return

    def run(self, win: Any) -> None:
        tui.hide_cursor()
        while True:
            self._draw(win)
            key = tui.read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "ctrl" and key.ch == "c":
                raise KeyboardInterrupt
            if key.kind == "esc":
                return
            height, width = win.getmaxyx()
            rows_needed, cols_needed = self.min_size(width)
            if height < rows_needed or width < cols_needed:
                continue
            self.message = ""
            row = self.selected_row
            if key.kind == "up" or (key.kind == "char" and key.ch == "k"):
                self._move(-1)
            elif key.kind == "down" or (key.kind == "char" and key.ch == "j"):
                self._move(1)
            elif key.kind == "home":
                self.index = -1
                self._move(1)
            elif key.kind == "end":
                self.index = len(self.entries)
                self._move(-1)
            elif key.kind == "char" and key.ch == "?":
                screens_common._help_modal(win, self.palette, "settings — help", cli_text.SETTINGS_HELP, self._draw)
            elif key.kind == "enter":
                self._enter(win)
            elif key.kind == "char" and key.ch in ("r", "R") and row is not None:
                self._reset(win, row)
            elif key.kind == "char" and key.ch in ("g", "G"):
                self._open(win, "providers")
            elif key.kind == "char" and key.ch in ("m", "M"):
                self._open(win, "models")
            elif (
                key.kind in ("left", "right")
                and row is not None
                and row.key == settings_mod.REVIEW_ROUND_CAP_KEY
                and self._writable(row)
            ):
                self._step_int(row, -1 if key.kind == "left" else 1)


def _choice_row(row: views.SettingsRow) -> bool:
    """A row this screen edits: a Settings field, a host preference, the
    default profile or the window ceiling (the last two in ``choices.json``)."""

    return (row.key in _SETTINGS_FIELDS or row.key in _PREFERENCE_FIELDS
            or row.key in (views.SETTINGS_DEFAULT_KEY, views.SETTINGS_CEILING_KEY))


def run_settings_screen(
    runtime: runtime_mod.Runtime,
    win: Any,
    palette: tui.Palette,
    *,
    card_lineup: profile_mod.ResolvedLineup | None = None,
    tty_in: TextIO,
    tty_out: TextIO,
) -> None:
    """Open the Settings screen (T §6.5) on ``win``; returns when it is closed."""

    _SettingsScreen(
        runtime, palette, card_lineup=card_lineup, tty_in=tty_in, tty_out=tty_out
    ).run(win)
