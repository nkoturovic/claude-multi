"""The Direct screen: a single-lead lineup chooser."""

from __future__ import annotations

from claude_multi import catalog
from claude_multi import compiler
from claude_multi import custom
from claude_multi import errors as cli_errors
from claude_multi import launch
from claude_multi import profile as profile_mod
from claude_multi import scope as scope_mod
from claude_multi import sessions
from claude_multi import settings as settings_mod
from claude_multi import state
from claude_multi import termtext
from claude_multi import tui
from claude_multi import views
from typing import Any
from typing import Callable
from typing import Mapping
from typing import Sequence
import dataclasses
import textwrap
import claude_multi.cli.gateway_facts as gateway_facts
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.gateway_actions as screens_gateway
import claude_multi.cli.screens.providers as screens_providers
import claude_multi.cli.session_facts as session_facts
import claude_multi.cli.text as cli_text
import claude_multi.cli.types as cli_types
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def _direct_marks(
    runtime: runtime_mod.Runtime,
    key: str,
    *,
    unavailable: Mapping[str, str],
    oauth_records: Mapping[str, int],
    served: set[str] | frozenset[str] | None,
    lcat: profile_mod.LineupCatalog | None = None,
) -> str | None:
    """The Direct row mark on the merged v2 lines (§4.9, v2-checks code 9).

    ``no secret`` (the provider is in ``_ordinary_unavailable``), ``sign in
    needed`` (an OAuth pool with no credential record), ``not served`` (the
    line's selectors, ``[1m]`` stripped, are not all in the running
    gateway's served set; an unknown served set, i.e. the gateway down, marks
    nothing), else None.  Reads ``lcat.lines[key]`` and ``lcat.providers``
    only, so an admitted New line marks like any other (the legacy predicates
    read the v1 view, which omits New lines).
    """

    lcat = lcat if lcat is not None else runtime.lineup_catalog()
    entry = lcat.lines[key]
    provider_id = entry["provider"]
    if provider_id in unavailable:
        return cli_text._DIRECT_MARK_NO_SECRET
    transport = (lcat.providers.get(provider_id) or {}).get("transport") or {}
    if transport.get("kind") == "oauth-pool" and oauth_records.get(transport.get("pool"), 0) <= 0:
        return cli_text._DIRECT_MARK_SIGNIN
    if served is None:
        return None
    selectors = {selector.removesuffix("[1m]") for _e, selector, _c in catalog.line_selectors(entry)}
    if selectors and not selectors <= set(served):
        return cli_text._DIRECT_MARK_UNSERVED
    return None


def _direct_reason(
    runtime: runtime_mod.Runtime,
    key: str,
    mark: str | None,
    *,
    unavailable: Mapping[str, str],
    lcat: profile_mod.LineupCatalog,
) -> str | None:
    """The detail text of a marked row (the earlier launcher's wording)."""

    if mark is None:
        return None
    entry = lcat.lines[key]
    provider = lcat.providers.get(entry["provider"]) or {}
    if mark == cli_text._DIRECT_MARK_NO_SECRET:
        reason = unavailable.get(entry["provider"], "")
        template = cli_text.DIRECT_REASON_KEY if reason.startswith("missing ") else cli_text.DIRECT_REASON_KEY_INVALID
        return template.format(display=provider.get("display", entry["provider"]))
    if mark == cli_text._DIRECT_MARK_SIGNIN:
        return cli_text.DIRECT_REASON_SIGNIN.format(kind=_account_kind(provider["transport"]["pool"]))
    return cli_text.DIRECT_REASON_UNSERVED


def _account_kind(pool: str) -> str:
    from claude_multi.setup import texts

    return texts.ACCOUNT_KINDS.get(pool, f"{pool} account")


def _direct_selector_line(entry: Mapping[str, Any]) -> str:
    return "in-session: " + " · ".join(f"/model {s}" for s in gateway_facts._line_selectors(entry))


def _direct_detail_reserve(runtime: runtime_mod.Runtime, width: int, keys: Sequence[str] | None = None) -> int:
    """Worst-case detail lines at ``width`` (the earlier ``_detail_reserve``, on v2 lines).

    Reason candidates come from ``lcat.providers`` (every keyed provider's
    missing- and invalid-key texts, every OAuth pool's sign-in text, the
    not-served text) plus the class line; selector-line
    candidates are ``in-session: /model …`` over ``_line_selectors`` of each
    listed row (``keys``; every lead-capable line when None).  The floor so
    never flaps while browsing.
    """

    lcat = runtime.lineup_catalog()
    providers = lcat.providers
    candidates = [
        template.format(display=p.get("display", pid))
        for pid, p in providers.items()
        if (p.get("transport", {}).get("kind") == "direct" or catalog.is_keyed_compat(p))
        and isinstance(p["transport"].get("auth"), Mapping)
        and p["transport"]["auth"].get("secret_ref")
        for template in (cli_text.DIRECT_REASON_KEY, cli_text.DIRECT_REASON_KEY_INVALID)
    ]
    candidates.extend(
        cli_text.DIRECT_REASON_SIGNIN.format(kind=_account_kind(p["transport"]["pool"]))
        for p in providers.values()
        if p.get("transport", {}).get("kind") == "oauth-pool"
    )
    candidates.append(cli_text.DIRECT_REASON_UNSERVED)
    wrap_width = max(20, width - 4)
    reason_lines = max(len(textwrap.wrap(reason, wrap_width)) for reason in candidates)
    listed = keys if keys is not None else [
        k for k, e in lcat.lines.items() if "lead" in e.get("capabilities", ())
    ]
    selector_lines = max(
        (len(textwrap.wrap(_direct_selector_line(lcat.lines[k]), wrap_width)) for k in listed if k in lcat.lines),
        default=0,
    )
    return reason_lines + selector_lines


class _DirectScreen:
    """Direct: a lead-only session on any offered lead-capable line.

    Rows come from the merged view (``views.direct_rows`` over
    ``views.line_rows``): the offered lead-capable catalog lines of
    every class, then the custom lead lines once.  Enter rechecks the marks
    (the ``DIRECT_*`` modals), then prepares an ad-hoc direct launch and
    returns ``("perform", prepared)``; the caller performs after curses
    teardown.  Tab saves the choice as a lead-only profile.  With
    ``purpose`` (needs-a-choice, link, the lineup dialog's direct target) the
    screen is a chooser: Enter returns ``(key, effort)``, and ``(key, None)``
    for purpose ``link`` (no effort field).
    """

    what = "the direct screen"

    def __init__(
        self,
        runtime: runtime_mod.Runtime,
        *,
        palette: tui.Palette,
        passthrough: Sequence[str] = (),
        purpose: str | None = None,
        journal: Callable[[], str | None] | None = None,
        no_subagents: bool | None = None,
    ):
        self.runtime = runtime
        self.palette = palette
        self.passthrough = list(passthrough)
        # ``direct --no-subagents``: the launch this screen prepares carries it.
        self.no_subagents = no_subagents
        self.purpose = purpose
        self.journal = journal
        self.message = ""
        self.message_role = "accent"
        self.efforts: dict[str, str] = {}
        self.offset = 0
        self._credential_warning_accepted = False
        self._load()
        self.selected = self._preselect()

    # -- data --------------------------------------------------------------

    def _load(self) -> None:
        runtime = self.runtime
        try:
            eff = runtime.current_effective()
        except cli_errors.CLIError:
            eff = screens_common._default_effective(runtime)
        self.lcat = lcat = runtime.lineup_catalog()
        registry = custom.load_registry(runtime.environ)
        line_rows = views.line_rows(lcat, eff, custom_ids=frozenset(registry["models"]))
        self.unavailable = gateway_facts._ordinary_unavailable(runtime)
        self.oauth_records = gateway_facts._oauth_credential_records(runtime)
        try:
            self.served, _status = runtime.served_models(runtime.gateway_token())
        except launch.LaunchError:
            self.served = None
        self.message = (gateway_facts.gateway_down_banner(runtime) + " (W: start it or read its log)"
                        if self.served is None else "")
        if runtime.gateway_attention:
            self.message += ("; " if self.message else "") + "Attention: " + "; ".join(runtime.gateway_attention)
        marks = {
            row.key: _direct_marks(
                runtime, row.key, unavailable=self.unavailable,
                oauth_records=self.oauth_records, served=self.served, lcat=lcat,
            )
            for row in line_rows
        }
        self.rows = views.direct_rows(line_rows, marks=marks)

    def _preselect(self) -> int:
        """§4.9: the newest direct v4 record of this cwd, else the default seed's lead, else 0."""

        keys = [row.key for row in self.rows]
        best: tuple[str, str] | None = None
        for record in session_facts._session_records(self.runtime):
            if record.get("version") != sessions.RECORD_VERSION or record.get("cwd") != self.runtime.cwd:
                continue
            applied = record.get("applied")
            if not isinstance(applied, Mapping) or applied.get("agents") != {}:
                continue
            key = (applied.get("lead") or {}).get("key")
            if key not in keys:
                continue
            try:
                stamp = session_facts._record_last_seen(record)
            except KeyError:
                continue
            if best is None or stamp > best[0]:
                best = (stamp, key)
        if best is not None:
            return keys.index(best[1])
        try:
            lead = self.runtime.profiles.load(catalog.DEFAULT_SEED).get("lead") or {}
        except profile_mod.ProfileError:
            lead = {}
        model = lead.get("model") if isinstance(lead, Mapping) else None
        if isinstance(model, str):
            try:
                resolved = self.lcat.resolve_key(model).key
            except catalog.CatalogError:
                resolved = None
            if resolved in keys:
                return keys.index(resolved)
        return 0

    def effort_of(self, row: views.DirectRow) -> str | None:
        """The row's launch effort: ``ultracode`` on client rows, the cycled one on gateway rows."""

        if row.mode != "gateway":
            return profile_mod.ULTRACODE
        return self.efforts.get(row.key, row.default_effort)

    def _shows_effort(self) -> bool:
        return self.purpose != "link"

    def _row_text(self, row: views.DirectRow) -> str:
        if row.mode == "gateway" and not self._shows_effort():
            return dataclasses.replace(row, mode="client").text()
        return row.text(self.efforts.get(row.key))

    # -- layout ------------------------------------------------------------

    def _keybar(self) -> tui.KeyBar:
        return tui.KeyBar(self._bindings())

    def _bindings(self, *, longest: bool = False) -> tuple[tuple[str, str], ...]:
        """The bar for the selected row: ← → when its effort is chosen here,
        W while the gateway is not reachable (``longest``: both, for the floor)."""

        base = cli_text.DIRECT_CHOOSE_KEYBAR if self.purpose else cli_text.DIRECT_KEYBAR
        extra: list[tuple[str, str]] = []
        row = self.rows[self.selected] if self.rows and 0 <= getattr(self, "selected", -1) < len(self.rows) else None
        if longest or (row is not None and row.mode == "gateway" and self._shows_effort() and row.efforts):
            extra.append(cli_text.DIRECT_EFFORT_BINDING)
        if longest or self.served is None:
            extra.append(cli_text.DIRECT_GATEWAY_BINDING)
        return (*base[:-2], *extra, *base[-2:])

    def _title(self) -> str:
        return cli_text.DIRECT_CHOOSE_TITLE.format(purpose=self.purpose) if self.purpose else cli_text.DIRECT_TITLE

    def _detail_reserve(self, width: int) -> int:
        wrap_width = max(20, width - 4)
        return max(_direct_detail_reserve(self.runtime, width, [r.key for r in self.rows]),
                   max((len(textwrap.wrap(views.direct_detail(row, self.rows), wrap_width))
                        + len(textwrap.wrap(_direct_selector_line(self.lcat.lines[row.key]), wrap_width))
                        for row in self.rows), default=0))

    def min_size(self, width: int) -> tuple[int, int]:
        """(rows, cols) floor (§3.4): top, title, rule, class note, message + list + detail + bar."""

        return views.ScreenFloor.compute(
            chrome=5,
            list_rows=len(self.rows),
            detail_reserve=self._detail_reserve(width),
            bar_rows=tui.KeyBar(self._bindings(longest=True)).rows(width),
            cols=cli_text.DIRECT_MIN_COLS,
        ).as_tuple()

    def _detail(self, width: int) -> list[tuple[str, str]]:
        if not self.rows:
            return []
        row = self.rows[self.selected]
        wrap_width = max(20, width - 4)
        lines: list[tuple[str, str]] = []
        reason = _direct_reason(
            self.runtime, row.key, row.mark, unavailable=self.unavailable, lcat=self.lcat
        )
        text = views.direct_detail(row, self.rows, reason=reason)
        lines.extend((piece, "warn" if reason else "dim") for piece in textwrap.wrap(text, wrap_width))
        entry = self.lcat.lines.get(row.key)
        if entry is not None and gateway_facts._line_selectors(entry):
            lines.extend((piece, "dim") for piece in textwrap.wrap(_direct_selector_line(entry), wrap_width))
        return lines

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows_needed, cols_needed = self.min_size(width)
        if height < rows_needed or width < cols_needed:
            screens_common._screen_too_small(win, palette, self.what, rows_needed, cols_needed)
            return
        win.erase()
        keybar = self._keybar()
        bar_rows = keybar.rows(width)
        message_row = height - bar_rows - 1
        tui.safe_add(win, 1, 2, views.clip(self._title(), width - 3), palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        detail = self._detail(width)
        reserve = self._detail_reserve(width)
        list_top = 3
        visible = max(1, message_row - reserve - 1 - list_top)
        if self.selected < self.offset:
            self.offset = self.selected
        elif self.selected >= self.offset + visible:
            self.offset = self.selected - visible + 1
        self.offset = max(0, min(self.offset, max(0, len(self.rows) - visible)))
        row_y = list_top
        if not self.rows:
            tui.safe_add(win, row_y, 4, views.clip(cli_text.DIRECT_EMPTY, width - 5), palette.attr("dim"))
            row_y += 1
        for index in range(self.offset, min(len(self.rows), self.offset + visible)):
            row = self.rows[index]
            focused = index == self.selected
            role = "dim" if row.mark else "normal"
            if focused:
                tui.safe_add(win, row_y, 2, "›", palette.attr("accent"))
            attr = palette.attr(role) | (tui.curses.A_REVERSE if focused else 0)
            tui.safe_add(win, row_y, 4, views.clip(self._row_text(row), width - 5), attr)
            row_y += 1
        tui.safe_add(win, row_y, 2, views.clip(cli_text.DIRECT_CLASS_NOTE, width - 3), palette.attr("dim"))
        row_y += 1
        for offset, (line, role) in enumerate(detail[:reserve]):
            tui.safe_add(win, row_y + offset, 2, views.clip(line, width - 3), palette.attr(role))
        if self.message:
            tui.safe_add(win, message_row, 2, views.clip(self.message, width - 3), palette.attr(self.message_role))
        keybar.draw(win, height - 1, palette)
        win.refresh()

    # -- actions -----------------------------------------------------------

    def _say(self, text: str, role: str = "accent") -> None:
        self.message = text
        self.message_role = role

    def _cycle_effort(self, direction: int) -> None:
        row = self.rows[self.selected]
        if row.mode != "gateway" or not self._shows_effort() or not row.efforts:
            return
        current = self.efforts.get(row.key, row.default_effort)
        index = row.efforts.index(current) if current in row.efforts else 0
        index = max(0, min(len(row.efforts) - 1, index + direction))
        self.efforts[row.key] = row.efforts[index]

    def _confirm_marked(self, win: Any, row: views.DirectRow) -> bool:
        """Enter's recheck: refresh the marks; a marked row asks first."""

        eff = self.runtime.current_effective()
        entry = self.lcat.lines[row.key]
        reason = eff.unavailable_lines.get(row.key, "")
        if not settings_mod.provider_enabled(eff, entry["provider"]):
            reason = "provider off — G → Space enables it"
        elif not isinstance(entry.get("lead"), Mapping) or not (entry.get("context") or {}).get("ordinary_profile"):
            reason = "missing lead/context fields — edit the model definition"
        if reason:
            tui.TextView("model unavailable", [reason], palette=self.palette).run(win)
            return False
        self.unavailable = gateway_facts._ordinary_unavailable(self.runtime)
        self.oauth_records = gateway_facts._oauth_credential_records(self.runtime)
        mark = _direct_marks(
            self.runtime, row.key, unavailable=self.unavailable,
            oauth_records=self.oauth_records, served=self.served, lcat=self.lcat,
        )
        if mark is None:
            return True
        entry = self.lcat.lines[row.key]
        provider = self.lcat.providers.get(entry["provider"]) or {}
        lines = [f"{row.key} — {entry.get('display', row.key)} (provider {entry['provider']})"]
        if mark == cli_text._DIRECT_MARK_NO_SECRET:
            lines += screens_common.modal_lines(
                cli_text.DIRECT_KEY_MISSING_BODY.format(display=provider.get("display", entry["provider"])), win)
            confirmed = tui.Modal(cli_text.DIRECT_KEY_MISSING_TITLE, lines,
                                  buttons=cli_text.DIRECT_KEY_MISSING_BUTTONS).run(
                                      win, self.palette, background=self._draw)
            self._credential_warning_accepted = bool(confirmed)
            return bool(confirmed)
        if mark == cli_text._DIRECT_MARK_SIGNIN:
            kind = _account_kind(provider["transport"]["pool"])
            lines += screens_common.modal_lines(cli_text.DIRECT_SIGNIN_BODY.format(kind=kind), win)
            buttons = cli_text.DIRECT_SIGNIN_CHOOSE_BUTTONS if self.purpose else cli_text.DIRECT_SIGNIN_BUTTONS
            choice = tui.Modal(cli_text.DIRECT_SIGNIN_TITLE, lines, buttons=buttons).run(
                win, self.palette, background=self._draw)
            if choice == "signin":
                import claude_multi.cli.screens.signin as screens_signin

                screens_signin.run_sign_in_flow(self.runtime, win, self.palette, entry["provider"],
                                                say=self._say, background=self._draw)
                selected = row.key
                message, role = self.message, self.message_role
                self._load()
                keys = [item.key for item in self.rows]
                self.selected = keys.index(selected) if selected in keys else 0
                self.message, self.message_role = message, role
                return False
            return choice == "launch"
        lines += screens_common.modal_lines(cli_text.DIRECT_UNSERVED_BODY, win)
        anyway = "Choose anyway" if self.purpose else "Launch anyway"
        confirmed = tui.Modal(cli_text.DIRECT_UNSERVED_TITLE, lines, buttons=(("Cancel", False), (anyway, True))).run(
            win, self.palette, background=self._draw
        )
        return bool(confirmed)

    def _launch(self, win: Any) -> tuple | None:
        self._credential_warning_accepted = False
        row = self.rows[self.selected]
        if not self._confirm_marked(win, row):
            return None
        effort = self.effort_of(row) or profile_mod.ULTRACODE
        if self.purpose:
            return (row.key, None if self.purpose == "link" else effort)
        target = cli_types.LaunchTarget(
            "ad-hoc", profile_mod.ad_hoc_direct(row.key, effort), None, False, f"Direct {row.key}"
        )
        try:
            prepared = self.runtime.prepare(target, action="fresh", passthrough=list(self.passthrough),
                                            no_subagents=self.no_subagents)
        except (cli_errors.CLIError, launch.LaunchError, scope_mod.ScopeError, compiler.CompilerError,
                profile_mod.ProfileError, profile_mod.BindingError, settings_mod.SettingsError) as exc:
            # A transient message: the screen stays and Enter can be retried.
            self._say(termtext.visible_message(exc).replace("\n", " "), "error")
            return None
        if prepared.secret_problems:
            # Direct may launch without a credential, as on the command line.
            # Route approval, enablement and transport safety were checked separately.
            if not self._credential_warning_accepted:
                wrap = max(20, min(60, win.getmaxyx()[1] - 8))
                lines = [piece for problem in prepared.secret_problems for piece in textwrap.wrap(problem, wrap)]
                lines += textwrap.wrap("Requests may fail. G → K sets the provider key. Launch anyway?", wrap)
                if not tui.Modal(cli_text.DIRECT_KEY_MISSING_TITLE, lines,
                                 buttons=cli_text.DIRECT_KEY_MISSING_BUTTONS).run(
                                     win, self.palette, background=self._draw):
                    self._say(cli_text.DIRECT_NOT_LAUNCHED, "warn")
                    return None
            prepared = dataclasses.replace(prepared, secret_problems=())
        return ("perform", prepared)

    def _save_as_profile(self, win: Any) -> None:
        row = self.rows[self.selected]
        if not self.runtime.allow_state_writes:
            self._say(cli_text.SETTINGS_READ_ONLY.replace("settings", "profiles"), "warn")
            return
        field_ = tui.TextInput(f"direct-{row.key}", max_length=64)
        modal = tui.Modal(
            cli_text.DIRECT_SAVE_TITLE,
            [f"lead {row.short} · effort {self.effort_of(row)}"],
            buttons=(("Save", True), ("Cancel", False)),
            input=field_,
        )
        if not modal.run(win, self.palette, background=self._draw):
            return
        name = field_.value.strip()
        document = {**profile_mod.ad_hoc_direct(row.key, self.effort_of(row) or profile_mod.ULTRACODE), "name": name}
        document.pop("seed", None)  # sc[10]: never a seed marker
        try:
            self.runtime.profiles.new(document)
        except state.CommittedStateError as exc:
            self._say(f"Committed, durability unconfirmed: {screens_common._state_error_text(exc)}", "warn")
            return
        except (profile_mod.ProfileError, sessions.StateMarkerError, state.StateError, OSError) as exc:
            self._say(screens_common._state_error_text(exc), "warn")
            return
        self._say(cli_text.DIRECT_SAVED.format(name=name))

    def run(self, win: Any) -> tuple | None:
        """``("perform", prepared)`` (launch), ``(key, effort)`` (choose mode) or None (Esc)."""

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
                screens_common._help_modal(win, self.palette, "direct — help", cli_text.DIRECT_HELP, self._draw)
            elif key.kind == "char" and key.ch in ("g", "G") and not self.purpose:
                screens_providers.run_providers_screen(self.runtime, win, self.palette, journal=self.journal)
                current = self.rows[self.selected].key if self.rows else None
                self._load()
                keys = [row.key for row in self.rows]
                self.selected = keys.index(current) if current in keys else 0
            elif key.kind == "char" and key.ch in ("w", "W") and self.served is None:
                note = screens_gateway.dialog(self.runtime, win, self.palette, background=self._draw)
                current = self.rows[self.selected].key if self.rows else None
                self._load()
                keys = [row.key for row in self.rows]
                self.selected = keys.index(current) if current in keys else 0
                if note:
                    self.message = note
            elif not self.rows:
                continue
            elif key.kind == "up" or (key.kind == "char" and key.ch == "k"):
                self.selected = max(0, self.selected - 1)
            elif key.kind == "down" or (key.kind == "char" and key.ch == "j"):
                self.selected = min(len(self.rows) - 1, self.selected + 1)
            elif key.kind == "home":
                self.selected = 0
            elif key.kind == "end":
                self.selected = len(self.rows) - 1
            elif key.kind in ("left", "right"):
                self._cycle_effort(-1 if key.kind == "left" else 1)
            elif key.kind == "tab" and not self.purpose:
                self._save_as_profile(win)
            elif key.kind == "enter":
                outcome = self._launch(win)
                if outcome is not None:
                    return outcome


def run_direct_screen(
    runtime: runtime_mod.Runtime,
    win: Any,
    palette: tui.Palette,
    *,
    passthrough: Sequence[str] = (),
    purpose: str | None = None,
    journal: Callable[[], str | None] | None = None,
    no_subagents: bool | None = None,
) -> tuple | None:
    """Open Direct (T §5) on ``win``: ``("perform", prepared)``, ``(key, effort)`` or None.
    ``no_subagents`` (``direct --no-subagents``) is carried into the launch it prepares."""

    return _DirectScreen(
        runtime, palette=palette, passthrough=passthrough, purpose=purpose, journal=journal,
        no_subagents=no_subagents,
    ).run(win)
