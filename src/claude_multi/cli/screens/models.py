"""The Models screen."""

from __future__ import annotations

from claude_multi import catalog
from claude_multi import custom
from claude_multi import errors as cli_errors
from claude_multi import operator as operator_mod
from claude_multi import profile as profile_mod
from claude_multi import sessions
from claude_multi import settings as settings_mod
from claude_multi import state
from claude_multi import tui
from claude_multi import views
from datetime import datetime
from typing import Any
from typing import Mapping
import textwrap
import dataclasses
import io
from claude_multi import discovery
import claude_multi.cli.onboarding as onboarding
import claude_multi.cli.consent as consent
import claude_multi.cli.doctor as doctor
import claude_multi.cli.gateway_facts as gateway_facts
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.providers as screens_providers
import claude_multi.cli.text as cli_text
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


class _ModelsScreen:
    """The Models screen: lines, optional badges, retired keys, candidates.

    Enter on a New row records an optional badge; Enter on an admitted
    New row revokes only that badge (default focus Cancel), both through
    ``SettingsStore``; Enter on any other line inspects it
    (``views.line_inspection``), V shows the same details everywhere.  Rows
    come from the merged view (``views.line_rows``). The candidates row
    lists advisory ids; one opens with a prefilled **Declare…** that leads
    to the model form (nothing is declared, approved, admitted or sent
    before that form's own preview).

    ``profile_hint`` (a profile name) preselects that profile's lead line.
    ``now`` is accepted for the runner signature; the retirement radar reads
    doctor's clock (``_doctor_now``).
    """

    what = "the models screen"

    def __init__(
        self,
        runtime: runtime_mod.Runtime,
        *,
        palette: tui.Palette,
        profile_hint: str | None = None,
        now: datetime | None = None,
    ):
        self.runtime = runtime
        self.palette = palette
        self.profile_hint = profile_hint
        self.now = now
        self.message = ""
        self.message_role = "accent"
        self.offset = 0
        self._load()
        self.index = self._first_index()
        hinted = self._hinted_key()
        if hinted is not None and hinted in self.keys:
            self.index = self.keys.index(hinted)

    # -- data --------------------------------------------------------------

    def _load(self) -> None:
        runtime = self.runtime
        self.banner: str | None = None
        try:
            eff = runtime.current_effective()
        except cli_errors.CLIError as exc:
            self.banner = f"settings.json is unreadable: {exc} — admission is unavailable"
            eff = screens_common._default_effective(runtime)
        lcat = runtime.lineup_catalog()
        registry = custom.load_registry(runtime.environ)
        self.lcat = lcat
        self.line_rows = views.line_rows(lcat, eff, custom_ids=frozenset(registry["models"]))
        store = runtime.profiles
        documents: dict[str, Any] = {}
        for name in store.names():
            try:
                documents[name] = store.load(name)
            except profile_mod.ProfileError:
                continue
        try:
            bindings = runtime.bindings.bindings()
        except profile_mod.BindingError:
            bindings = {}
        self.used = views.used_by(documents, bindings, lcat)
        retired: dict[str, str | None] = {}
        for key in lcat.retired:
            try:
                retired[key] = lcat.resolve_key(key).key
            except catalog.CatalogError:
                retired[key] = None
        self.retired = retired
        self.continuity_count = len(gateway_facts._doctor_continuity(runtime)[0])
        # Providers using their API key now: a line reviewed for that route
        # shows the route's own evidence in its details.
        try:
            ledger = runtime.operator_snapshot().ledger
        except (cli_errors.ClaudeMultiError, OSError):
            ledger = None
        choices = ledger.transport_choices if ledger is not None else {}
        self.key_transports = frozenset(pid for pid, choice in choices.items()
                                        if choice == operator_mod.TRANSPORT_API_KEY)
        radar_lines = doctor._doctor_retirement_radar(runtime)
        self.radar = {
            row.key: line
            for line in radar_lines
            for row in self.line_rows
            if line.startswith(f"line {row.key} (")
        }
        try:
            self.lifecycle = onboarding.statuses(runtime)
        except cli_errors.ClaudeMultiError:
            self.lifecycle = {}
        try:
            served, _status = runtime.served_snapshot(runtime.gateway_token())
        except cli_errors.ClaudeMultiError:
            served = None
        self.candidates = discovery.candidates(gateway_facts.pinned_registry(runtime), served,
                                               gateway_facts.discovery_known(runtime))
        model = self.model(80)
        # Table order: the main rows, the New heading (None), the New rows.
        self.keys: list[str | None] = [*model.keys, None, *model.new_keys]

    def model(self, width: int) -> views.ModelsModel:
        model = views.models_model(
            self.line_rows,
            used=self.used,
            retired=self.retired,
            continuity_count=self.continuity_count,
            catalog_version=self.lcat.catalog_version,
            radar=self.radar,
            width=width,
        )

        count_r = sum("registry" in r.sources for r in self.candidates.rows)
        count_s = sum("served" in r.sources for r in self.candidates.rows)
        label = f"{count_r} registry · {count_s} routable uncataloged"
        main = list(model.rows)
        new = list(model.new_rows)
        for cells, keys in ((main, model.keys), (new, model.new_keys)):
            for i, key in enumerate(keys):
                if key in self.lifecycle:
                    row = list(cells[i])
                    row[2] += " op"
                    cells[i] = tuple(row)
        details = dict(model.details)
        for key, status in self.lifecycle.items():
            if status not in ("admitted", "New · not admitted"):
                details[key] = (*details.get(key, ()), f"Attention: {status} — optional admission badge only.")
        for row in self.line_rows:
            if not row.offered:
                details[row.key] = (*details.get(row.key, ()),
                                    f"Use unavailable — Esc → G (Providers) → {row.provider} shows the remedy.")
        details["__candidates__"] = ("Advisory only — Enter inspects; nothing admitted.",)
        return dataclasses.replace(model, details=details, rows=(*main, ("candidates", label, "", "", "", "inspect")),
                                   keys=(*model.keys, "__candidates__"), new_rows=tuple(new))

    def _hinted_key(self) -> str | None:
        if not self.profile_hint:
            return None
        try:
            document = self.runtime.profiles.load(self.profile_hint)
        except profile_mod.ProfileError:
            return None
        lead = document.get("lead")
        model = lead.get("model") if isinstance(lead, Mapping) else None
        if not isinstance(model, str):
            return None
        try:
            return self.lcat.resolve_key(model).key
        except catalog.CatalogError:
            return None

    def _first_index(self) -> int:
        return next((i for i, key in enumerate(self.keys) if key is not None), 0)

    @property
    def selected_key(self) -> str | None:
        if 0 <= self.index < len(self.keys):
            return self.keys[self.index]
        return None

    def _row_kind(self, key: str | None, model: views.ModelsModel) -> str:
        if key is None or key == "__candidates__":
            return "none"
        if key in self.lifecycle:
            return "admitted" if key in model.admitted else "new"
        if key in model.new_keys:
            return "new"
        if key in model.admitted:
            return "admitted"
        return "plain"

    def _keybar(self, model: views.ModelsModel) -> tui.KeyBar:
        kind = self._row_kind(self.selected_key, model)
        if self.banner is None and kind == "new":
            return tui.KeyBar(cli_text.MODELS_KEYBAR_NEW)
        if self.banner is None and kind == "admitted":
            return tui.KeyBar(cli_text.MODELS_KEYBAR_ADMITTED)
        return tui.KeyBar(cli_text.MODELS_KEYBAR)

    # -- layout ------------------------------------------------------------

    def _columns(self, width: int) -> list[int]:
        """Column indexes by width tier: < 70 drops ``used by``, < 64 drops ``provider``."""

        keep = list(range(len(views.MODELS_COLUMNS)))
        if width < 70:
            keep.remove(5)
        if width < 64:
            keep.remove(2)
        return keep

    def _detail_reserve(self, model: views.ModelsModel) -> int:
        return max([1, *(len(lines) for lines in model.details.values())])

    def _banner_lines(self, width: int) -> list[str]:
        return screens_common._wrapped(self.banner, width - 4) if self.banner else []

    def min_size(self, width: int) -> tuple[int, int]:
        """(rows, cols) floor (§3.4): chrome + min(list, 3) + detail reserve + bar."""

        model = self.model(width)
        retired = max(2, len(model.retired.split("\n")))
        chrome = 3 + len(self._banner_lines(width)) + 1 + 1 + retired + 1
        reserve = self._detail_reserve(model) - 1 + 1  # extra detail lines + the message row
        bars = [cli_text.MODELS_KEYBAR, cli_text.MODELS_KEYBAR_NEW, cli_text.MODELS_KEYBAR_ADMITTED]
        bar_rows = max(tui.KeyBar(bar).rows(width) for bar in bars)
        floor = views.ScreenFloor.compute(
            chrome=chrome,
            list_rows=len(model.keys) + len(model.new_keys),
            detail_reserve=reserve,
            bar_rows=bar_rows,
            cols=cli_text.MODELS_MIN_COLS,
        )
        return floor.as_tuple()

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows_needed, cols_needed = self.min_size(width)
        if height < rows_needed or width < cols_needed:
            screens_common._screen_too_small(win, palette, self.what, rows_needed, cols_needed)
            return
        win.erase()
        model = self.model(width)
        keybar = self._keybar(model)
        bar_rows = keybar.rows(width)
        tui.safe_add(win, 1, 2, model.title, palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        row = 3
        for line in self._banner_lines(width):
            tui.safe_add(win, row, 2, line, palette.attr("warn"))
            row += 1
        keep = self._columns(width)
        columns = [views.MODELS_COLUMNS[i] for i in keep]
        min_widths = [cli_text.MODELS_MIN_WIDTHS[i] for i in keep]
        body = [[r[i] for i in keep] for r in (*model.rows, *model.new_rows)]
        # T §4: a narrowed column ends in ``…``.  The widths are tui.Table's
        # own (views.table_widths), so the pre-clipped cells keep them.
        widths = views.table_widths(columns, body, width - 1 - 4, min_widths=min_widths, gap=2)
        body = [[views.clip(cell, widths[i]) for i, cell in enumerate(r)] for r in body]
        cells = body[: len(model.rows)]
        heading_index = len(cells)
        cells.append([""] * len(keep))
        cells.extend(body[len(model.rows):])
        roles: list[str | None] = [None] * len(model.rows) + [None] + ["dim"] * len(model.new_rows)
        table = tui.Table(
            columns,
            cells,
            selected=self.index,
            min_widths=min_widths,
            gap=2,
            row_roles=roles,
            spans={heading_index: (model.new_heading, "dim"),
                   model.keys.index("__candidates__"): (
                       "candidates  " + model.rows[model.keys.index("__candidates__")][1] + " — Enter inspects", "dim")},
            cursor=True,
        )
        retired_lines = model.retired.split("\n")
        reserve = self._detail_reserve(model)
        message_row = height - bar_rows - 1
        visible = max(1, message_row - reserve - len(retired_lines) - (row + 1))
        if self.index < self.offset:
            self.offset = self.index
        elif self.index >= self.offset + visible:
            self.offset = self.index - visible + 1
        self.offset = max(0, min(self.offset, max(0, len(cells) - visible)))
        # Content ends at ``width - 1`` (the rule's right edge).
        used = table.draw(win, row, 2, width - 1, palette, max_rows=visible, start=self.offset)
        row += used
        for line in retired_lines:
            tui.safe_add(win, row, 2, line, palette.attr("dim"))
            row += 1
        key = self.selected_key
        for line in model.details.get(key, ()) if key is not None else ():
            tui.safe_add(win, row, 2, views.fit_text((line,), width - 3), palette.attr("normal"))
            row += 1
        if self.message:
            tui.safe_add(win, message_row, 2, views.clip(self.message, width - 3),
                         palette.attr(self.message_role))
        keybar.draw(win, height - 1, palette)
        win.refresh()

    # -- actions -----------------------------------------------------------

    def _step(self, direction: int) -> None:
        count = len(self.keys)
        for step in range(1, count + 1):
            candidate = self.index + direction * step
            if not 0 <= candidate < count:
                return
            if self.keys[candidate] is not None:
                self.index = candidate
                return

    def _edge(self, last: bool) -> None:
        order = range(len(self.keys) - 1, -1, -1) if last else range(len(self.keys))
        self.index = next((i for i in order if self.keys[i] is not None), self.index)

    def _say(self, text: str, role: str = "accent") -> None:
        self.message = text
        self.message_role = role

    def _toggle_admission(self, win: Any) -> None:
        key = self.selected_key
        model = self.model(win.getmaxyx()[1])
        kind = self._row_kind(key, model)
        if key is None or kind not in ("new", "admitted") or self.banner is not None:
            return
        if not self.runtime.allow_state_writes:
            self._say(cli_text.SETTINGS_READ_ONLY, "warn")
            return
        try:
            consent.require_human(f"models {'revoke' if kind == 'admitted' else 'admit'} {key}", self.runtime.gateway_environ())
        except consent.ConsentRefused as exc:
            self._say(str(exc), "warn")
            return
        row = next(r for r in self.line_rows if r.key == key)
        if key in model.guarded:
            action = screens_common.OnboardingActions(self.runtime, win, self.palette)
            verb = "revoke" if kind == "admitted" else "admit"
            if verb == "admit":
                self._admit_operator(win, key)
            else:
                action.invoke(["models", verb, key])
            self._load()
            self.index = self.keys.index(key) if key in self.keys else self._first_index()
            return
        wrap = max(20, min(60, win.getmaxyx()[1] - 8))
        if kind == "new":
            title = f"Admit {key}?"
            body = (
                f"Record an optional local admission badge for {row.display}. "
                "Use availability is unchanged; no diagnostic request is sent. "
                "Stored in Settings (admitted_lines)."
            )
            buttons = (("Admit", True), ("Cancel", False))
        else:
            title = f"Revoke {key}?"
            body = (
                f"Remove only the optional admission badge for {row.display}. "
                "The line remains usable where its provider/route allows; qualification "
                "evidence is unchanged. Stored in Settings (admitted_lines)."
            )
            buttons = (("Cancel", False), ("Revoke", True))
        # Both optional metadata actions default to Cancel.
        confirmed = tui.Modal(title, textwrap.wrap(body, wrap), buttons=buttons, default=False).run(
            win, self.palette, background=self._draw
        )
        if not confirmed:
            return
        store = self.runtime.settings_store
        try:
            if kind == "new":
                store.admit_line(key, catalog=self.runtime.catalog)
            else:
                store.revoke_line(key, catalog=self.runtime.catalog)
        except (settings_mod.SettingsError, sessions.StateMarkerError, state.StateError, OSError) as exc:
            self._say(str(exc), "warn")
            return
        self._load()
        self.index = self.keys.index(key) if key in self.keys else self._first_index()
        verb = "admitted" if kind == "new" else "revoked"
        self._say(f"{verb} {key} — badge only; use and qualification unchanged")

    def _admit_operator(self, win: Any, key: str) -> None:
        """Record a badge through the shared local-only admission flow."""

        screens_providers.ConnectActions(self.runtime, win, self.palette, background=self._draw).admit(key)

    def _remove(self, win: Any) -> None:
        """X: remove a model you added; where profiles use it, choose its
        replacement first and review the rewrites."""

        from claude_multi.setup import lines as setup_lines, model as setup_model, texts

        key = self.selected_key
        if key is None or key == "__candidates__":
            return
        if key in self.runtime.catalog.docs["models-v2"]["models"]:
            self._say(cli_text.MODELS_X_CATALOG, "warn")
            return
        try:
            plan = setup_lines.plan_line_remove(self.runtime, key)
        except cli_errors.ClaudeMultiError as exc:
            self._say(str(exc), "warn")
            return
        actions = screens_providers.ConnectActions(self.runtime, win, self.palette, background=self._draw)
        if plan.live_users:
            lines = [cli_text.REMOVE_BLOCKED_HEAD] + [
                cli_text.REMOVE_BLOCKED_ITEM.format(fix=texts.BLOCKER["live"][0].format(id8=user[:8]))
                for user in plan.live_users]
            tui.Modal(cli_text.REMOVE_BLOCKED_TITLE.format(id=key), actions._body(lines),
                      buttons=cli_text.CLOSE_BUTTON).run(win, self.palette, background=self._draw)
            return
        if plan.references:
            items = []
            for candidate in plan.candidates:
                entry = self.lcat.lines.get(candidate) or {}
                family = (self.lcat.line_family(candidate, entry) if hasattr(self.lcat, "line_family")
                          else catalog.line_family(entry, self.lcat.providers))
                items.append(cli_text.SUCCESSOR_ITEM.format(display=views.short_label(entry.get("display", candidate)),
                                                            provider=entry.get("provider", ""), family=family))
            items.append(cli_text.SUCCESSOR_CANCEL)
            index = actions._choose(cli_text.SUCCESSOR_TITLE.format(key=key, n=len(plan.references)), items,
                                    help_text=cli_text.MODELS_HELP)
            if index is None or index >= len(plan.candidates):
                return
            try:
                plan = setup_lines.plan_line_remove(self.runtime, key, successor=plan.candidates[index])
            except cli_errors.ClaudeMultiError as exc:
                self._say(str(exc), "warn")
                return
            lines = [*plan.rewrites, texts.REWRITE_QUESTION.format(key=key)]
            if not tui.TextView(cli_text.SUCCESSOR_CONFIRM_TITLE.format(key=key), lines, palette=self.palette,
                                confirm=True).run(win):
                self._say(setup_model.NOTHING_CHANGED, "warn")
                return
        elif not actions._confirm(cli_text.REMOVE_LINE_TITLE.format(key=key), plan.lines, cli_text.REMOVE_BUTTONS):
            self._say(setup_model.NOTHING_CHANGED, "warn")
            return
        try:
            applied = setup_lines.apply_line_remove(self.runtime, plan, setup_model.Confirmation.given(plan))
        except cli_errors.ClaudeMultiError as exc:
            self._say(str(exc), "warn")
            return
        self._load()
        self.index = min(self.index, max(0, len(self.keys) - 1))
        if self.keys and self.keys[self.index] is None:
            self.index = self._first_index()
        self._say(" · ".join(applied.lines))

    def _details(self, win):
        output = io.StringIO()
        key = self.selected_key
        if key == "__candidates__":
            onboarding.candidates(self.runtime, output=output)
            lines = output.getvalue().splitlines()
        elif key in self.lifecycle:
            onboarding.details(self.runtime, key, output=output)
            lines = output.getvalue().splitlines()
        else:
            lines = list(self._inspection(key))
        tui.TextView("model — full details", lines, palette=self.palette).run(win)

    def _inspection(self, key: str | None) -> tuple[str, ...]:
        row = next((row for row in self.line_rows if row.key == key), None)
        if row is None or key is None:
            return ()
        return views.line_inspection(self.lcat.lines[key], row, used=self.used.get(key, 0),
                                     radar=self.radar.get(key), key_route=row.provider in self.key_transports)

    def _candidates(self, win: Any) -> None:
        """The candidates row: the advisory ids; Enter opens one with its
        registry facts and, for an attributed one, Declare… (the model form,
        prefilled). Nothing here contacts a provider or writes."""

        rows = [*self.candidates.rows, *self.candidates.unattributed]
        if not rows:
            tui.TextView("candidates", ["No candidates.", "", *_candidate_footer()], palette=self.palette).run(win)
            return
        labels = [_candidate_label(row) for row in rows]
        selected = 0
        while True:
            index = screens_common._run_choice_list(
                win, self.palette, cli_text.CANDIDATES_TITLE, labels, help_title="candidates — help",
                help_text=cli_text.CANDIDATES_HELP + "\n\n" + discovery.OPENROUTER_HELP,
                footer=cli_text.CANDIDATES_KEYBAR, selected=selected)
            if index is None:
                return
            selected = index
            row = rows[index]
            lines = list(_candidate_lines(row))
            if row.provider_hint is None:
                tui.TextView(cli_text.CANDIDATE_TITLE.format(wire=row.wire),
                             [*lines, "", cli_text.CANDIDATE_UNATTRIBUTED], palette=self.palette).run(win)
                continue
            if not tui.TextView(cli_text.CANDIDATE_TITLE.format(wire=row.wire),
                                [*lines, "", cli_text.CANDIDATE_DECLARE.format(provider=row.provider_hint)],
                                palette=self.palette, confirm="declare").run(win):
                continue
            self._declare_candidate(win, row)
            self._load()
            return

    def _declare_candidate(self, win: Any, row: Any) -> None:
        # A candidate is registry (or served) evidence, never a listing: its
        # context prefill keeps the pinned registry as its source.
        meta = row.metadata
        registry = (meta, row.channel) if meta is not None and row.channel else None
        try:
            draft = onboarding.line_draft(self.runtime, row.provider_hint, {"id": row.wire}, registry=registry)
        except (cli_errors.ClaudeMultiError, ValueError, KeyError) as exc:
            # The form still opens: the person states what the registry did not.
            self._say(f"prefill incomplete: {exc}", "warn")
            draft = {"wire_model": row.wire}
        key = discovery.derived_key(row.wire, set(self.lcat.lines)) or ""
        screens_common.OnboardingActions(self.runtime, win, self.palette).model_form(row.provider_hint, draft, key)

    def run(self, win: Any) -> None:
        """Esc returns None (this screen never returns a jump)."""

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
            height, width = win.getmaxyx()
            rows_needed, cols_needed = self.min_size(width)
            if height < rows_needed or width < cols_needed:
                continue
            self.message = ""
            if key.kind == "char" and key.ch == "?":
                # §4.8: the selected line's radar text is clipped on screen; the
                # full text sits in help.
                radar = self.radar.get(self.selected_key or "")
                text = cli_text.MODELS_HELP + (f"\n\n{radar}" if radar else "")
                screens_common._help_modal(win, self.palette, "models — help", text, self._draw)
            elif key.kind == "up" or (key.kind == "char" and key.ch == "k"):
                self._step(-1)
            elif key.kind == "down" or (key.kind == "char" and key.ch == "j"):
                self._step(1)
            elif key.kind == "home":
                self._edge(last=False)
            elif key.kind == "end":
                self._edge(last=True)
            elif key.kind == "char" and key.ch in ("v", "V"):
                self._details(win)
            elif key.kind == "char" and key.ch in ("q", "Q") and self.selected_key in self.lifecycle:
                screens_common.OnboardingActions(self.runtime, win, self.palette).qualify(self.selected_key)
                self._load()
            elif key.kind == "char" and key.ch in ("q", "Q"):
                self._say("Shipped models carry reviewed evidence; Q qualifies models you added", "warn")
            elif key.kind == "char" and key.ch in ("e", "E"):
                if self.selected_key in self.lifecycle:
                    pid, line = onboarding.declaration(self.runtime, self.selected_key)
                    screens_common.OnboardingActions(self.runtime, win, self.palette).model_form(
                        pid, line, self.selected_key, edit=True)
                    self._load()
                else:
                    self._say("Shipped models are not edited here; E edits models you added", "warn")
            elif key.kind == "char" and key.ch in ("x", "X"):
                self._remove(win)
            elif key.kind == "enter":
                model = self.model(width)
                if self.selected_key == "__candidates__":
                    self._candidates(win)
                elif self.banner is None and self._row_kind(self.selected_key, model) in ("new", "admitted"):
                    self._toggle_admission(win)
                elif self.selected_key is not None:
                    self._details(win)


def run_models_screen(
    runtime: runtime_mod.Runtime,
    win: Any,
    palette: tui.Palette,
    *,
    profile_hint: str | None = None,
    now: datetime | None = None,
) -> None:
    """Open the Models screen on ``win``; returns when it is closed."""

    _ModelsScreen(runtime, palette=palette, profile_hint=profile_hint, now=now).run(win)


def _candidate_label(row: Any) -> str:
    meta = row.metadata
    context = (f"context {profile_mod.format_tokens(meta.context_length)}"
               if meta is not None and meta.context_length else "context unknown")
    return f"{row.channel or 'unattributed'}  {row.wire}  {context}  · {row.mark}"


def _candidate_lines(row: Any) -> tuple[str, ...]:
    """One candidate's advisory facts (registry-stated, never validated)."""

    meta = row.metadata
    def tokens(value: int | None) -> str:
        return str(value) if value else "unknown"
    lines = [f"model id: {row.wire}",
             f"channel: {row.channel or 'unattributed'} · provider: {row.provider_hint or 'unknown'}",
             f"seen in: {', '.join(sorted(row.sources))} · {row.created_text}"]
    if meta is not None:
        if meta.display_name:
            lines.append(f"name: {meta.display_name}")
        lines.append(f"registry-stated context {tokens(meta.context_length)} · max output "
                     f"{tokens(meta.max_completion_tokens)}")
        if meta.thinking_levels:
            lines.append(f"thinking levels: {', '.join(meta.thinking_levels)}")
    if row.visibility:
        lines.append(f"visibility: {row.visibility}")
    if row.owned_by:
        lines.append(f"owned by (advisory): {row.owned_by}")
    lines.append(cli_text.CANDIDATE_ADVISORY)
    return tuple(lines)


def _candidate_footer() -> tuple[str, ...]:
    return (cli_text.CANDIDATE_ADVISORY, *discovery.OPENROUTER_HELP.splitlines())
