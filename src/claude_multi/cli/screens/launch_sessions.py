"""The launch card and the session interaction stack.

The card opens the sessions screen, the sessions screen resumes through
the card, and the needs-choice chooser returns through it: one module
keeps that call cycle together."""

from __future__ import annotations

from claude_multi import catalog
from claude_multi import compiler
from claude_multi import custom
from claude_multi import errors as cli_errors
from claude_multi import launch
from claude_multi import lineup as lineup_mod
from claude_multi import paths as product_paths
from claude_multi import pin as pin_mod
from claude_multi import profile as profile_mod
from claude_multi import quota
from claude_multi import readiness as readiness_mod
from claude_multi import scope as scope_mod
from claude_multi import sessions
from claude_multi import settings as settings_mod
from claude_multi import state
from claude_multi import termtext
from claude_multi import tui
from claude_multi import views
from claude_multi.tui import workflow_guarantee_panel
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Mapping
from typing import Sequence
from typing import TextIO
import argparse
import copy
import dataclasses
import io
import re
import shlex
import sys
import textwrap
import claude_multi.cli.doctor as doctor
import claude_multi.cli.doctor_actions as doctor_actions
import claude_multi.cli.gateway_facts as gateway_facts
import claude_multi.cli.resume_checks as resume_checks
import claude_multi.cli.runtime as runtime_mod
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.direct as screens_direct
import claude_multi.cli.screens.gateway_actions as screens_gateway
import claude_multi.cli.screens.models as screens_models
import claude_multi.cli.screens.profile_editor as screens_profile_editor
import claude_multi.cli.screens.providers as screens_providers
import claude_multi.cli.screens.settings as screens_settings
import claude_multi.cli.selection as selection
import claude_multi.cli.session_actions as session_actions
import claude_multi.cli.session_facts as session_facts
import claude_multi.cli.text as cli_text
import claude_multi.cli.types as cli_types


@dataclasses.dataclass(frozen=True)
class _WayBack:
    """How a screen opened over others leads back to a fresh launch card,
    where G → L signs in and S resumes a session: the Esc presses it takes
    (one per screen), and whether they reach that card (``card``) or end
    claude-multi (then it is run again)."""

    escapes: int = 1
    card: bool = False

    def deeper(self, screens: int = 1) -> "_WayBack":
        """The way from a screen opened ``screens`` levels above this one."""

        return _WayBack(self.escapes + screens, self.card)

    def text(self) -> str:
        presses = ", ".join(["Esc"] * self.escapes)
        return presses if self.card else f"{presses}, {cli_text.CARD_WAY_REOPEN}"


class _CardHealthActions:
    """The card's doctor and update actions.

    ``_LaunchCardScreen`` is the one class that inherits them.  They
    read only ``runtime``, ``palette``, ``update_hint``, ``gateway_problem``,
    ``gateway_checked``, ``gateway_check``, ``hint_detector``, ``tty_in``,
    ``tty_out``, ``way_back`` and ``self._draw``.
    """

    runtime: "Runtime"
    palette: tui.Palette
    update_hint: tuple[str, str] | None
    gateway_problem: str | None
    gateway_checked: bool
    gateway_check: Any | None
    hint_detector: Any
    tty_in: Any
    tty_out: Any
    way_back: "_WayBack | None"

    def _release_hint(self, _contract: Any) -> tuple[str, str] | None:
        from claude_multi import release_update

        try:
            return release_update.card_hint(self.runtime)
        except Exception:  # never let an update hint break the card
            return None

    def _gateway_status(self) -> str | None:
        """Loopback gateway problem line, or None when healthy."""

        if self.gateway_check is not None:
            return self.gateway_check()
        try:
            self.runtime.check_readiness()
        except launch.LaunchError as exc:
            return launch.gateway_problem_text(exc, home=self.runtime.home)
        except Exception as exc:  # never let a health hint break the card
            return f"gateway check failed: {exc} — {launch.gateway_unit_remedy(self.runtime.home)}"
        return None

    def _refresh_health(self) -> None:
        """Recompute pin/gateway state (after an in-TUI update)."""

        self.runtime.reload_catalog()
        self.update_hint = self.hint_detector(
            self.runtime.catalog.docs["native-contract"]
        )
        self.gateway_problem = self._gateway_status()
        self.gateway_checked = True

    # -- health / update actions ----------------------------------------------

    def _pause_for_lines(self, win: Any, header: str) -> None:
        with tui.suspended_curses(win):
            self.tty_out.write(header + "\n")

    def _resume_note(self, win: Any) -> None:
        with tui.suspended_curses(win):
            self.tty_out.write("\nPress Enter to return to claude-multi.")
            self.tty_out.flush()
            try:
                self.tty_in.readline()
            except KeyboardInterrupt:
                pass

    def _run_update(self, win: Any) -> None:
        """U: the ``claude-multi update`` journey (an installed release checks,
        plans, asks and applies; the Nix package says to update the flake).
        Rollback stays a command-line action (``claude-multi update --rollback``)."""

        from claude_multi import release_update

        journey = release_update.journey_for(self.runtime)
        if journey.channel != "bundle":
            lines = [*release_update.channel_lines(journey.channel),
                     "To roll back an installed release: claude-multi update --rollback"]
            tui.TextView("Update claude-multi", lines, palette=self.palette).run(win)
            return
        with tui.suspended_curses(win):
            self.tty_out.write("Update claude-multi\n")
            ask = release_update.terminal_ask(self.tty_in, self.tty_out, interactive=True)
            go = ask("Check the release server for a newer claude-multi now?")
            if go:
                release_update.run(journey, mode="update", ask=ask, out=self.tty_out)
            else:
                self.tty_out.write("Nothing was checked.\n")
        self._resume_note(win)
        self._refresh_health()

    def _run_health(self, win: Any) -> None:
        """H: doctor's report as ``claude-multi doctor`` prints it (detail
        lines folded behind ``doctor -v``, the same closing line; a key a
        finding says to save is named by the card's route, G → K, as the
        card's other remedies are), a bulk repair offered only when a
        finding names a session repair, then the gateway's own actions."""

        self._pause_for_lines(win, "claude-multi doctor")
        problems, info_lines, attention = doctor._collect_doctor_reports(self.runtime)
        status = "blocked" if problems else ("attention" if attention else "ready")
        shown = [line for line in info_lines if not doctor.detail_line(line)]

        def said(line: str) -> str:
            # The card's way to save a key (G → K), never the command.
            return termtext.visible_text(_card_doctor_line(self.runtime, line, self.way_back))

        with tui.suspended_curses(win):
            if problems:
                self.tty_out.write("BLOCKED\n")
                for line in problems:
                    self.tty_out.write(f"  - {said(line)}\n")
            if attention:
                self.tty_out.write("Attention\n")
                for line in attention:
                    self.tty_out.write(f"  - {said(line)}\n")
            if status == "ready":
                self.tty_out.write("Ready\n")
            for line in shown:
                self.tty_out.write(f"{said(line)}\n")
            hidden = len(info_lines) - len(shown)
            if hidden:
                self.tty_out.write(cli_text.CARD_DOCTOR_HIDDEN.format(n=hidden, s="s" if hidden != 1 else "") + "\n")
            self.tty_out.write(doctor.DOCTOR_SUMMARY[status])
            repairs = doctor.repair_offer(problems, attention)
            if repairs:
                self.tty_out.write(cli_text.CARD_DOCTOR_REPAIR.format(n=repairs))
                self.tty_out.flush()
                try:
                    answer = self.tty_in.readline().strip().lower()
                except KeyboardInterrupt:
                    answer = ""
                if answer in ("y", "yes"):
                    code = doctor_actions._doctor_repair_all(self.runtime, self.tty_out)
                    self.tty_out.write(f"(repair-all exit {code})\n")
        # The gateway's own actions: start (stopped), restart (proven
            # yours) and the log tail, which opens once curses is back.
            follow = screens_gateway.health_prompt(self.runtime, self.tty_in, self.tty_out)
        self._refresh_health()
        if follow == "log":
            title, lines = screens_gateway.log_view(screens_gateway.observe(self.runtime))
            tui.TextView(title, lines, palette=self.palette).run(win)


def _resume_card_result(
    runtime: runtime_mod.Runtime,
    win: Any,
    prepared: cli_types.PreparedLaunch,
    *,
    palette: tui.Palette,
    passthrough: Sequence[str],
    decision: str | None,
    tty_in: Any,
    tty_out: Any,
    now: datetime | None,
    always: bool = False,
    way_back: _WayBack,
) -> tuple | None:
    """A prepared resume: the resume card (§4.1) when it has a diff, notices or secret problems.

    Returns ``("perform", prepared, decision)`` or None (the card's Esc).
    ``always`` shows the card even with nothing to state (the chooser, §4.7.3);
    ``way_back`` is the card's way back to a launch card (its remedies say it).
    """

    if always or prepared.diff or prepared.notices or prepared.secret_problems:
        card = _LaunchCardScreen(
            runtime,
            prepared.target,
            action=selection._card_action(prepared),
            session_id=sessions.managed_id(prepared.record),
            prepared=prepared,
            passthrough=passthrough,
            palette=palette,
            tty_in=tty_in,
            tty_out=tty_out,
            resume_decision=decision,
            now=now,
            way_back=way_back,
        )
        return card.run(win)
    return ("perform", prepared, decision)


class _NeedsChoiceChooser:
    """The needs-a-choice chooser: a new lineup for sessions whose lead was removed.

    With several records it first lists them (``sessions that need a
    choice``).  For one record it lists the profiles (MRU) and ``direct —
    choose a model…`` (Direct choose mode); the choice becomes a
    ``LaunchTarget("relaunch", …)``, ``prepare``
    resumes onto it and the resume card shows the diff.  Enter there returns
    ``("perform", prepared, None)``; a prepare failure is a message on the
    list, which stays.  Nothing is written until the caller performs.
    """

    what = "the needs-a-choice chooser"

    def __init__(
        self,
        runtime: runtime_mod.Runtime,
        records: Sequence[dict[str, Any]],
        *,
        palette: tui.Palette,
        passthrough: Sequence[str] = (),
        notices: Mapping[str, str] | None = None,
        tty_in: Any | None = None,
        tty_out: Any | None = None,
        now: datetime | None = None,
        way_back: _WayBack | None = None,
    ):
        self.runtime = runtime
        self.records = list(records)
        # From the chooser's first list: Esc leaves it (claude-multi -r: ends it).
        self.way_back = way_back or _WayBack()
        self.palette = palette
        self.passthrough = list(passthrough)
        self.notices = dict(notices or {})
        self.tty_in = tty_in
        self.tty_out = tty_out
        self.now = now

    def _notice(self, record: dict[str, Any]) -> str:
        mid = sessions.managed_id(record)
        if mid in self.notices:
            return self.notices[mid]
        if record.get("version") == sessions.RECORD_VERSION:
            return sessions.lead_choice_notice(record, self.runtime.lineup_catalog()) or ""
        summary = sessions.record_summary(record)
        return f"lead {summary.lead_key} has no live line"

    def _for_record(self, win: Any, record: dict[str, Any]) -> tuple | None:
        runtime = self.runtime
        mid = sessions.managed_id(record)
        m8 = mid[:8]
        labels, choices = selection._profile_choice_items(runtime, current=record.get("profile"))
        title = cli_text.NEEDS_CHOICE_TITLE.format(m8=m8, notice=self._notice(record))
        message = ""
        selected = 0
        while True:
            index = screens_common._run_choice_list(
                win, self.palette, title, labels,
                help_title="needs a choice — help", help_text=cli_text.NEEDS_CHOICE_HELP,
                selected=selected, message=message,
            )
            if index is None:
                return None
            selected = index
            kind, name = choices[index]
            if kind == "profile":
                target = cli_types.LaunchTarget("relaunch", None, name, True, f"Relaunch {m8}")
            else:
                picked = screens_direct.run_direct_screen(runtime, win, self.palette, purpose="needs a choice")
                if picked is None:
                    message = ""
                    continue
                key, effort = picked
                target = cli_types.LaunchTarget(
                    "relaunch",
                    profile_mod.ad_hoc_direct(key, effort or profile_mod.ULTRACODE),
                    None,
                    False,
                    f"Relaunch {m8}",
                )
            try:
                prepared = runtime.prepare(
                    target, action="resume", passthrough=list(self.passthrough), session_id=mid
                )
            except cli_types.LaunchPlanError as exc:
                message = "; ".join(termtext.visible_text(problem) for problem in exc.problems)
                continue
            except _CARD_PLAN_ERRORS as exc:
                message = termtext.visible_message(exc).replace("\n", " ")
                continue
            result = _resume_card_result(
                runtime, win, prepared, palette=self.palette, passthrough=self.passthrough,
                decision=None, tty_in=self.tty_in, tty_out=self.tty_out, now=self.now, always=True,
                # The card, then this record's list, then (several records) the list of records.
                way_back=self.way_back.deeper(1 if len(self.records) == 1 else 2),
            )
            if result is not None:
                return result
            message = ""

    def run(self, win: Any) -> tuple | None:
        """``("perform", prepared, None)`` or None (Esc)."""

        tui.hide_cursor()
        if len(self.records) == 1:
            return self._for_record(win, self.records[0])
        labels = []
        for record in self.records:
            summary = sessions.record_summary(record)
            labels.append(f"{summary.managed_id[:8]}  {summary.profile_label}  lead {summary.lead_key} removed")
        selected = 0
        while True:
            index = screens_common._run_choice_list(
                win, self.palette, cli_text.NEEDS_CHOICE_LIST_TITLE, labels,
                help_title="needs a choice — help", help_text=cli_text.NEEDS_CHOICE_HELP, selected=selected,
            )
            if index is None:
                return None
            selected = index
            result = self._for_record(win, self.records[index])
            if result is not None:
                return result


class _PerAgentScreen:
    """The lineup dialog's per-agent edit (§4.7.1): the nine agents, one change.

    Rows are ``catalog.AGENT_ROLE_IDS`` with the binding the session runs
    (``sessions.applied_document``).  Enter opens the §4.3 binding picker
    for that agent (``views.picker_rows`` against the record's own Settings
    snapshot) and returns the ``set`` request words; U returns ``unset``.
    Esc returns None.
    """

    what = "the per-agent edit"

    def __init__(self, runtime: runtime_mod.Runtime, record: dict[str, Any], *, palette: tui.Palette, eff: settings_mod.Effective):
        self.runtime = runtime
        self.record = record
        self.palette = palette
        self.eff = eff
        self.lcat = runtime.lineup_catalog()
        self.agents = sessions.applied_document(record).get("agents", {})
        self.ids = list(catalog.AGENT_ROLE_IDS)
        self.selected = 0
        self.message = ""

    def _row(self, agent_id: str) -> str:
        binding = self.agents.get(agent_id)
        if not isinstance(binding, Mapping):
            text = lineup_mod.UNBOUND
        else:
            key = str(binding.get("model"))
            text = lineup_mod.binding_label(
                self.lcat.lines.get(key) or self.lcat.retired.get(key), str(binding.get("effort")), key=key
            )
        # The widest agent name sets the column, so every binding lines up.
        name_width = max(len(profile_mod.label(rid)) for rid in catalog.AGENT_ROLE_IDS)
        return f"{profile_mod.label(agent_id):<{name_width}}  {text}"

    def min_size(self, width: int) -> tuple[int, int]:
        return views.ScreenFloor.compute(
            chrome=4, list_rows=len(self.ids), detail_reserve=0,
            bar_rows=tui.KeyBar(cli_text.LINEUP_PER_AGENT_KEYBAR).rows(width), cols=cli_text.LINEUP_PER_AGENT_MIN_COLS,
        ).as_tuple()

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows, cols = self.min_size(width)
        if height < rows or width < cols:
            screens_common._screen_too_small(win, palette, self.what, rows, cols)
            return
        win.erase()
        bar = tui.KeyBar(cli_text.LINEUP_PER_AGENT_KEYBAR)
        m8 = sessions.managed_id(self.record)[:8]
        tui.safe_add(win, 1, 2, views.clip(cli_text.LINEUP_PER_AGENT_TITLE.format(m8=m8), width - 3),
                     palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        message_row = height - bar.rows(width) - 1
        visible = max(1, message_row - 3)
        start = max(0, min(self.selected - visible + 1, len(self.ids) - visible))
        for y, index in enumerate(range(start, min(len(self.ids), start + visible)), 3):
            focused = index == self.selected
            if focused:
                tui.safe_add(win, y, 2, "›", palette.attr("accent"))
            attr = palette.attr("normal") | (tui.curses.A_REVERSE if focused else 0)
            tui.safe_add(win, y, 4, views.clip(self._row(self.ids[index]), width - 5), attr)
        if self.message:
            tui.safe_add(win, message_row, 2, views.clip(self.message, width - 3), palette.attr("warn"))
        bar.draw(win, height - 1, palette)
        win.refresh()

    def _pick(self, win: Any, agent_id: str) -> list[str] | None:
        rows = views.line_rows(
            self.lcat, self.eff, custom_ids=frozenset(custom.load_registry(self.runtime.environ)["models"])
        )
        try:
            bindings = self.runtime.bindings.bindings()
        except profile_mod.BindingError as exc:
            self.message = f"cannot load named bindings: {exc}"
            return None
        model = views.picker_rows(
            rows, slot=agent_id, bindings=bindings, lcat=self.lcat, eff=self.eff,
            current=self.agents.get(agent_id),
        )
        chosen = tui.BindingPicker(model, palette=self.palette).run(win)
        if chosen is None:
            return None
        if chosen[0] == "off":
            return ["unset", agent_id]
        if chosen[0] == "use":
            named = bindings.get(chosen[1]) or {}
            key, effort = named.get("model"), named.get("effort")
        else:
            _kind, key, effort = chosen
        if not isinstance(key, str):
            self.message = f"named binding {chosen[1]!r} has no model"
            return None
        return ["set", f"{agent_id}={key}:{effort}" if effort else f"{agent_id}={key}"]

    def run(self, win: Any) -> list[str] | None:
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
            rows, cols = self.min_size(width)
            if height < rows or width < cols:
                continue
            self.message = ""
            if key.kind == "up" or (key.kind == "char" and key.ch == "k"):
                self.selected = max(0, self.selected - 1)
            elif key.kind == "down" or (key.kind == "char" and key.ch == "j"):
                self.selected = min(len(self.ids) - 1, self.selected + 1)
            elif key.kind == "home":
                self.selected = 0
            elif key.kind == "end":
                self.selected = len(self.ids) - 1
            elif key.kind == "char" and key.ch == "?":
                screens_common._help_modal(win, self.palette, "per-agent edit — help", cli_text.LINEUP_PER_AGENT_HELP, self._draw)
            elif key.kind == "char" and key.ch in ("u", "U"):
                return ["unset", self.ids[self.selected]]
            elif key.kind == "enter":
                words = self._pick(win, self.ids[self.selected])
                if words is not None:
                    return words


def _lineup_providers(lcat: Any, record: Mapping[str, Any]) -> list[str]:
    """The providers a session's applied lineup uses."""

    applied = record.get("applied") or {}
    keys = [applied.get("lead", {}).get("key"),
            *(binding.get("key") for binding in (applied.get("agents") or {}).values() if isinstance(binding, Mapping))]
    found = set()
    for key in keys:
        entry = lcat.lines.get(key) or lcat.retired.get(key) if isinstance(key, str) else None
        if isinstance(entry, Mapping) and entry.get("provider"):
            found.add(str(entry["provider"]))
    return sorted(found)


def _fallback_choices(runtime: Any, record: Mapping[str, Any]) -> list[str]:
    """The fallback dialog's providers: every provider a profile names as
    its primary provider (``/cm fallback``'s recipes), and the ones the
    lineup uses; each preview is ``/cm fallback``'s own, refusals included."""

    return sorted({*lineup_mod.fallback_providers(runtime), *_lineup_providers(runtime.lineup_catalog(), record)})


class _LineupDialog:
    """The lineup dialog (``T`` on a v4 record).

    Five targets (Tab / BTab), opening on the session's own profile: a
    profile (``to``; ← → cycles ``profile_pick_order``), a per-agent edit,
    direct, keep the current lineup (pin semantics: Follow turns off and a
    pending change is discarded; the applied lineup, scope and generation
    stay) and a fallback provider (← → cycles every provider with a
    fallback profile, and the providers the lineup uses).  Each builds a
    ``lineup.parse_request`` request; the preview is
    lock-free (for the first three ``build_target`` → ``profile.evaluate``
    against the record's Settings snapshot → ``classify`` →
    ``render_diff(diff_rows(…))``; keep and fallback show
    ``lineup.preview``).  Enter applies one change: ``lineup.apply``, or
    ``lineup.apply_preview``, which re-decides under the lock and returns a
    refreshed preview when the session changed meanwhile (nothing applied).
    A RELAUNCH is recorded ``pending`` and never execs from here.  ``run``
    returns True after an apply, None on Esc.
    """

    what = "the lineup dialog"
    TARGETS = ("profile", "per-agent", "direct", "keep", "fallback")

    def __init__(
        self,
        runtime: runtime_mod.Runtime,
        record: dict[str, Any],
        *,
        palette: tui.Palette,
        liveness: str,
        now: datetime | None = None,
    ):
        self.runtime = runtime
        self.record = record
        self.mid = sessions.managed_id(record)
        self.summary = sessions.record_summary(record, runtime.lineup_catalog())
        self.palette = palette
        self.liveness = liveness
        self.now = now
        self.order = selection.profile_pick_order(runtime)
        current = record.get("profile")
        # The dialog opens on what the session runs: its own profile.
        self.profile_name = current if current in self.order else (self.order[0] if self.order else current)
        self.providers = _fallback_choices(runtime, record)
        self.provider_name: str | None = self.providers[0] if self.providers else None
        self.shown: lineup_mod.Preview | None = None
        self.preview_lines: list[str] | None = None
        self.target = "profile"
        self.agent_words: list[str] | None = None
        self.direct_words: list[str] = ["direct"]
        self.message = ""
        self.message_role = "warn"
        self._preview()

    # -- the preview (lock-free) -----------------------------------------------

    def _words(self) -> list[str] | None:
        if self.target == "profile":
            return ["profile", self.profile_name] if self.profile_name else None
        if self.target == "per-agent":
            return self.agent_words
        if self.target == "keep":
            return ["pin"]
        if self.target == "fallback":
            return ["fallback", self.provider_name] if self.provider_name else None
        return self.direct_words

    def _preview(self) -> None:
        runtime = self.runtime
        self.request: lineup_mod.Request | None = None
        self.diff: list[str] = []
        self.errors: list[str] = []
        self.mode: str | None = None
        self.reasons: tuple[str, ...] = ()
        self.lead_switch: str | None = None
        self.preview_note = ""
        self.shown = None
        self.preview_lines = None
        words = self._words()
        if words is None:
            self.preview_note = {"per-agent": "Space picks an agent", "fallback": "no provider"}.get(
                self.target, "no profile")
            return
        if self.target in ("keep", "fallback"):
            self._preview_request(words)
            return
        try:
            request = lineup_mod.parse_request(words)
            record = runtime.session_store.load(self.mid)  # a sample; apply re-decides locked
            target = lineup_mod.build_target(record, request, runtime)
            eff = lineup_mod._record_eff(record)  # refuse an unreadable Settings snapshot
            bindings = lineup_mod._bindings_map(runtime)  # refuse invalid named bindings
        except lineup_mod.LineupRefusal as exc:
            self.message = str(exc)
            return
        except (sessions.SessionError, profile_mod.ProfileError) as exc:
            self.message = termtext.visible_message(exc)
            return
        self.request = request
        # As lineup.decide_locked: unchanged agent slots keep the record's
        # grant, new or changed operator slots use the current gate.
        lcat = lineup_mod._record_gate(runtime.lineup_catalog(), record, eff)
        if target.document is None:  # pin: not reachable from T
            return
        evaluation = profile_mod.evaluate(
            target.document, lcat, bindings=bindings, effective=eff, ad_hoc=target.profile is None
        )
        if evaluation.errors:
            self.errors = list(evaluation.errors)
            return
        lineup = evaluation.lineup
        self.diff = lineup_mod.render_diff(
            lineup_mod.diff_rows(record["applied"], lineup, lcat, old_lead_class=record.get("lead_class"))
        )
        scope_dir = scope_mod.scope_dir(runtime.session_store.root, self.mid)
        if int(record["lineup_generation"]) == 0:
            self.mode, self.reasons = "RELAUNCH", (lineup_mod.REASON_GEN0,)
        elif not scope_dir.is_dir():
            self.mode, self.reasons = "RELAUNCH", (lineup_mod.REASON_NO_SCOPE,)
        else:
            cls = lineup_mod.classify(record, lineup, scope_dir, forced=False, lcat=lcat)
            self.mode = "LIVE" if cls.kind == "live" else "RELAUNCH"
            self.reasons = cls.reasons
            if cls.lead_switch is not None:
                self.lead_switch = str(cls.lead_switch.get("display") or cls.lead_switch.get("selector"))

    def _preview_request(self, words: list[str]) -> None:
        """Keep and fallback: the read-only ``lineup.preview`` of the request."""

        try:
            request = lineup_mod.parse_request(words)
            record = self.runtime.session_store.load(self.mid)
            self.shown = lineup_mod.preview(self.runtime, record, request)
        except lineup_mod.LineupRefusal as exc:
            self.message = str(exc).replace("\n", " ")
            return
        except (sessions.SessionError, profile_mod.ProfileError, cli_errors.ClaudeMultiError) as exc:
            self.message = termtext.visible_message(exc)
            return
        self.request = request
        self.preview_lines = []
        for line in self.shown.text.splitlines():
            if not line.strip():
                continue
            if self.target == "fallback":
                if line == lineup_mod.FALLBACK_PREVIEW_ONLY.format(provider=self.provider_name):
                    line = "Preview only; nothing changed. Enter applies."
                elif self.liveness == "ended":
                    if line == lineup_mod.FALLBACK_EXISTING:
                        continue
                    if line == "Effect: LIVE":
                        line = "Effect: scope changes now; used at the next resume"
            self.preview_lines.append(line)

    def model(self) -> views.DialogModel:
        return views.lineup_dialog_model(
            title=self.summary.title,
            managed_id=self.mid,
            state=self.liveness,
            profile_label=self.summary.profile_label,
            generation=int(self.summary.lineup_generation or 0),
            target=self.target,
            target_profile=self.profile_name,
            focus=self.target,
            diff=self.diff,
            mode=self.mode,
            reasons=self.reasons,
            lead_switch=self.lead_switch,
            errors=self.errors,
            preview=self.preview_lines if self.target in ("keep", "fallback") else None,
            target_provider=self.provider_name,
        )

    # -- layout ---------------------------------------------------------------

    def min_size(self, width: int) -> tuple[int, int]:
        return views.ScreenFloor.compute(
            chrome=cli_text.LINEUP_DIALOG_CHROME, list_rows=3, detail_reserve=0,
            bar_rows=tui.KeyBar(cli_text.LINEUP_DIALOG_KEYBAR).rows(width), cols=cli_text.LINEUP_DIALOG_MIN_COLS,
        ).as_tuple()

    def _to_segments(self) -> list[tuple[str, str]]:
        per_agent = "per-agent edit ▸"
        if self.agent_words is not None:
            per_agent += " " + " ".join(self.agent_words)
        direct = "direct (no agents)"
        if len(self.direct_words) > 1:
            direct += " " + self.direct_words[1]
        return [
            ("profile", f"[{self.profile_name or '—'} ▾]"),
            ("per-agent", per_agent),
            ("direct", direct),
            ("keep", "keep current"),
            ("fallback", f"fallback [{self.provider_name or '—'} ▾]"),
        ]

    def _to_window(self, room: int) -> tuple[bool, list[tuple[str, str]]]:
        """The targets drawn on the ``to`` row: from the first, or (when the
        selected one would not fit) from the one that keeps it on screen,
        the skipped ones marked with a leading ``…``."""

        segments = self._to_segments()
        names = [name for name, _segment in segments]
        selected = names.index(self.target) if self.target in names else 0

        def used(part: list[tuple[str, str]], skipped: bool) -> int:
            return sum(len(segment) for _name, segment in part) + 3 * (len(part) - 1) + (3 if skipped else 0)

        start = 0
        while start < selected and used(segments[start:selected + 1], start > 0) > room:
            start += 1
        return start > 0, segments[start:]

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows, cols = self.min_size(width)
        if height < rows or width < cols:
            screens_common._screen_too_small(win, palette, self.what, rows, cols)
            return
        win.erase()
        bar = tui.KeyBar(cli_text.LINEUP_DIALOG_KEYBAR)
        model = self.model()
        tui.safe_add(win, 1, 2, views.clip(model.title, width - 3), palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        message_row = height - bar.rows(width) - 1
        limit = message_row - 1
        y = 3
        text_col = 8
        lines = list(model.lines)
        for index, (label, text, role) in enumerate(lines):
            if y >= limit:
                break
            if y == limit - 1 and index < len(lines) - 1:
                tui.safe_add(win, y, text_col, f"… {len(lines) - index} more lines", palette.attr("dim"))
                y += 1
                break
            if label:
                tui.safe_add(win, y, 2, label, palette.attr("dim") if label != "✗" else palette.attr("error"))
            if label == "to":
                col = text_col
                skipped, segments = self._to_window(width - 1 - text_col)
                if skipped:
                    tui.safe_add(win, y, col, "…", palette.attr("dim"))
                    col += 3
                for name, segment in segments:
                    if col >= width - 2:
                        break
                    clipped = views.clip(segment, width - 1 - col)
                    focused = name == self.target
                    attr = palette.attr("normal") | (tui.curses.A_REVERSE if focused else 0)
                    tui.safe_add(win, y, col, clipped, attr)
                    col += len(clipped) + 3
            else:
                tui.safe_add(win, y, text_col, views.clip(text, width - 1 - text_col), palette.attr(role))
            y += 1
        if self.preview_note and y < limit:
            tui.safe_add(win, y, text_col, self.preview_note, palette.attr("dim"))
        if self.message:
            tui.safe_add(win, message_row, 2, views.clip(self.message.replace("\n", " "), width - 3),
                         palette.attr(self.message_role))
        bar.draw(win, height - 1, palette)
        win.refresh()

    # -- keys -----------------------------------------------------------------

    def _cycle(self, delta: int) -> None:
        if self.target == "fallback":
            if self.providers:
                index = self.providers.index(self.provider_name) if self.provider_name in self.providers else -1
                self.provider_name = self.providers[(index + delta) % len(self.providers)]
            return
        if not self.order:
            return
        index = self.order.index(self.profile_name) if self.profile_name in self.order else -1
        self.profile_name = self.order[(index + delta) % len(self.order)]

    def _choose(self, win: Any) -> None:
        runtime = self.runtime
        if self.target == "profile":
            labels = [f"{name}  (current)" if name == self.record.get("profile") else name for name in self.order]
            selected = self.order.index(self.profile_name) if self.profile_name in self.order else 0
            index = screens_common._run_choice_list(
                win, self.palette, cli_text.LINEUP_PICK_PROFILE, labels,
                help_title="lineup — help", help_text=cli_text.LINEUP_DIALOG_HELP, selected=selected,
            )
            if index is not None:
                self.profile_name = self.order[index]
        elif self.target == "per-agent":
            try:
                eff = lineup_mod._record_eff(self.record)
            except lineup_mod.LineupRefusal as exc:
                self.message = str(exc)
                return
            words = _PerAgentScreen(runtime, self.record, palette=self.palette, eff=eff).run(win)
            if words is not None:
                self.agent_words = words
        elif self.target == "direct":
            picked = screens_direct.run_direct_screen(runtime, win, self.palette, purpose="lineup")
            if picked is not None:
                key, effort = picked
                self.direct_words = ["direct", f"{key}:{effort}" if effort else key]
        elif self.target == "fallback":
            if not self.providers:
                self.message, self.message_role = cli_text.LINEUP_NO_FALLBACK, "accent"
                return
            selected = self.providers.index(self.provider_name) if self.provider_name in self.providers else 0
            index = screens_common._run_choice_list(
                win, self.palette, cli_text.LINEUP_PICK_PROVIDER, list(self.providers),
                help_title="lineup — help", help_text=cli_text.LINEUP_DIALOG_HELP, selected=selected,
            )
            if index is not None:
                self.provider_name = self.providers[index]
        else:
            # Keep the current lineup has nothing to choose.
            self.message, self.message_role = cli_text.LINEUP_KEEP_NOTHING_TO_PICK, "accent"

    def _apply_preview(self, win: Any) -> bool:
        """Keep and fallback: apply what the preview showed, re-decided under
        the lock; a session that changed meanwhile shows the refreshed
        preview here, and nothing is applied."""

        if self.shown is None:
            self.message, self.message_role = cli_text.LINEUP_NOTHING, "accent"
            return False
        try:
            result = lineup_mod.apply_preview(self.runtime, self.shown, interactive=True)
        except (lineup_mod.LineupRefusal, sessions.MigrationBusyError) as exc:
            self.message, self.message_role = str(exc).replace("\n", " "), "warn"
            return False
        except (sessions.SessionError, state.StateError, cli_errors.CLIError, OSError) as exc:
            self.message, self.message_role = termtext.visible_message(exc), "warn"
            return False
        if result.exit_code != 0:
            self._preview()
            self.message, self.message_role = cli_text.LINEUP_PREVIEW_STALE, "warn"
            return False
        text = result.text.rstrip("\n") or "(no change)"
        if self.target == "fallback" and self.liveness == "ended":
            lines = text.splitlines()
            # Shared fallback reports end with one next step. An ended
            # session needs resume, never a running-client reload.
            lines[-1] = f"Next: resume with claude-multi -r {self.mid}"
            replacements = {
                "Effect: LIVE": "Effect: scope changed now; used at the next resume",
                lineup_mod.FALLBACK_UNCHANGED:
                    "Fallback: scope unchanged; the record now states the fallback.",
            }
            running_only = (lineup_mod.FALLBACK_EXISTING, lineup_mod.FALLBACK_LIVE[1],
                            lineup_mod.FALLBACK_PENDING[1])
            text = "\n".join(replacements.get(line, line) for line in lines if line not in running_only)
        wrap = max(20, min(100, win.getmaxyx()[1] - 8))
        screens_common._ScrollModal(cli_text.LINEUP_REPORT_TITLE.format(m8=self.mid[:8]),
                                    screens_common._wrap_help_lines(text, wrap)).run(
            win, self.palette, background=self._draw)
        return True

    def _apply(self, win: Any) -> bool:
        if self.target in ("keep", "fallback"):
            return self._apply_preview(win)
        request = self.request
        same_profile = (
            request is not None
            and request.verb == "profile"
            and request.name == self.record.get("profile")
            and bool(self.record.get("follow"))
        )
        if request is None or self.errors or self.mode is None or (not self.diff and same_profile):
            self.message, self.message_role = cli_text.LINEUP_NOTHING, "accent"
            return False
        args = lineup_mod.LineupArgs(
            skill_mode=False,
            session=self.summary.runtime_id,
            relaunch=False,
            its_exited=False,
            request=request,
        )
        try:
            result = lineup_mod.apply(self.runtime, args, interactive=True, relaunch_exec=None)
        except (lineup_mod.LineupRefusal, sessions.MigrationBusyError) as exc:
            self.message, self.message_role = str(exc), "warn"
            return False
        except (sessions.SessionError, state.StateError, cli_errors.CLIError, OSError) as exc:
            self.message, self.message_role = termtext.visible_message(exc), "warn"
            return False
        text = result.text.rstrip("\n") or "(no change)"
        wrap = max(20, min(100, win.getmaxyx()[1] - 8))
        screens_common._ScrollModal(cli_text.LINEUP_REPORT_TITLE.format(m8=self.mid[:8]), screens_common._wrap_help_lines(text, wrap)).run(
            win, self.palette, background=self._draw
        )
        return True

    def run(self, win: Any) -> bool | None:
        """True after an apply (the caller reloads), None on Esc."""

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
            rows, cols = self.min_size(width)
            if height < rows or width < cols:
                continue
            self.message, self.message_role = "", "warn"
            if key.kind in ("tab", "btab"):
                step = 1 if key.kind == "tab" else -1
                index = self.TARGETS.index(self.target)
                self.target = self.TARGETS[(index + step) % len(self.TARGETS)]
                self._preview()
            elif key.kind in ("left", "right") and self.target in ("profile", "fallback"):
                self._cycle(1 if key.kind == "right" else -1)
                self._preview()
            elif key.kind == "char" and key.ch == " ":
                self._choose(win)
                self._preview()
            elif key.kind == "char" and key.ch == "?":
                screens_common._help_modal(win, self.palette, "lineup — help", cli_text.LINEUP_DIALOG_HELP, self._draw)
            elif key.kind == "enter":
                if self._apply(win):
                    return True


_ROW_ACTIONS = (cli_text.SESSIONS_STOP_BINDING, cli_text.SESSIONS_MARK_BINDING, cli_text.SESSIONS_REPAIR_BINDING)


def _with_row_actions(base: Sequence[tuple[str, str]], actions: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """The sessions bar with the selected row's conditional keys before L."""

    bindings = list(base)
    at = next((index for index, (key, _label) in enumerate(bindings) if key == "L"), len(bindings) - 2)
    bindings[at:at] = list(actions)
    return tuple(bindings)


class _SessionsScreen:
    """The sessions screen: managed records, the banner, native link.

    Rows are ``views.session_rows`` over ``sessions.record_summary`` of every
    record (legacy records included), newest use first; C toggles this
    directory / all directories.  Liveness comes from the Runtime seams
    (``_live_prefixes``/``_proc_ids``), taken once per reload.  Enter resumes
    (the resume gate, then ``Runtime.prepare``, then the resume card when
    there is something to state) and returns ``("perform", prepared,
    decision)``; the caller performs after curses teardown.  V shows the
    details, T opens the lineup dialog, F previews then follows/pins, R
    renames, X forgets (default Cancel), E stops a live session, M marks an
    exited session ended, P repairs its scope, L links a native session.
    Forget and stop with unknown background liveness need the typed session
    id.  The screen holds no lock: every write goes through a store,
    ``lineup`` or session-action API, which re-checks under its locks.
    ``root``: the screen is the first one (bare ``-r``), so Esc quits.
    """

    what = "the sessions screen"

    def __init__(
        self,
        runtime: runtime_mod.Runtime,
        *,
        palette: tui.Palette,
        passthrough: Sequence[str] = (),
        resume_decision: str | None = None,
        now: datetime | None = None,
        tty_in: Any | None = None,
        tty_out: Any | None = None,
        root: bool = False,
        way_back: _WayBack | None = None,
    ):
        self.runtime = runtime
        self.palette = palette
        self.passthrough = list(passthrough)
        self.resume_decision = resume_decision
        self.now = now
        self.tty_in = tty_in
        self.tty_out = tty_out
        self.root = root
        # Esc leaves the screen: back to the launch card that opened it, or
        # (the first screen) out of claude-multi.
        self.way_back = way_back or _WayBack(1, card=not root)
        self.cwd_filter = True
        self.selected = 0
        self.offset = 0
        self.message = ""
        self.message_role = "accent"
        self._reload()

    # -- data -----------------------------------------------------------------

    def _clock(self) -> datetime:
        return self.now or datetime.now(timezone.utc)

    def _reload(self) -> None:
        runtime = self.runtime
        self.lcat = runtime.lineup_catalog()
        try:
            self.eff: settings_mod.Effective | None = runtime.current_effective()
        except cli_errors.CLIError:
            self.eff = None
        # One liveness sample per reload, from the Runtime seams.
        self.prefixes = runtime._live_prefixes()
        self.proc_ids = runtime._proc_ids()
        every = sorted(session_facts._session_records(runtime), key=session_facts._record_sort_key_last_used, reverse=True)
        native_all = session_facts._discover_native_sessions(runtime, limit=None)
        if self.cwd_filter:
            records = [record for record in every if record.get("cwd") == runtime.cwd]
            native = [item for item in native_all if item["cwd"] == runtime.cwd]
            self.empty_elsewhere = not records and not native and (bool(every) or bool(native_all))
        else:
            records, native = every, native_all
            self.empty_elsewhere = False
        # Every eligible native session is listed (the picker scrolls).
        self.native = native
        self.native_overflow = 0
        self.records = records
        # The ``last seen`` cell shows the sort value (``_record_last_seen``:
        # a pre-creation or malformed last_seen_at falls back to created_at).
        self.summaries = [
            dataclasses.replace(
                sessions.record_summary(
                    record, self.lcat if record.get("version") == sessions.RECORD_VERSION else None
                ),
                last_seen_at=session_facts._record_last_seen(record),
            )
            for record in records
        ]
        self.states = {
            summary.managed_id: session_facts._liveness(runtime, record, self.prefixes, self.proc_ids)
            for summary, record in zip(self.summaries, records)
        }
        self.forks = frozenset(
            summary.managed_id for summary, record in zip(self.summaries, records) if record.get("pending_forks")
        )
        self._model_cache: dict[int, views.SessionsModel] = {}
        self.selected = min(self.selected, max(0, self._items() - 1))

    def _model(self, width: int) -> views.SessionsModel:
        model = self._model_cache.get(width)
        if model is None:
            model = views.session_rows(
                self.summaries,
                lcat=self.lcat,
                states=self.states,
                now=self._clock(),
                width=width,
                cwd=self.runtime.cwd if self.cwd_filter else None,
                forks=self.forks,
                native_count=len(self.native) + self.native_overflow,
            )
            self._model_cache[width] = model
        return model

    def _needs(self) -> list[int]:
        """Indexes of the records whose lead needs a choice (the lead mark)."""

        return [
            index for index, summary in enumerate(self.summaries)
            if sessions.lead_key_needs_choice(summary.lead_key, self.lcat)
        ]

    def _items(self) -> int:
        """Selectable items: the records, then the banner (the last item) when shown."""

        return len(self.records) + (1 if self._needs() else 0)

    def _on_banner(self) -> bool:
        return bool(self.records) and self.selected == len(self.records)

    def _record(self) -> dict[str, Any] | None:
        if 0 <= self.selected < len(self.records):
            return self.records[self.selected]
        return None

    # -- layout -----------------------------------------------------------------

    def min_size(self, width: int) -> tuple[int, int]:
        """(rows, cols) floor: chrome + min(rows, 3) + the native line + bar."""

        return views.ScreenFloor.compute(
            chrome=cli_text.SESSIONS_CHROME,
            list_rows=len(self.records),
            detail_reserve=1 if (self.native or self.native_overflow) else 0,
            bar_rows=max(tui.KeyBar(bar).rows(width) for bar in
                         (_with_row_actions(cli_text.SESSIONS_KEYBAR, _ROW_ACTIONS),
                          _with_row_actions(cli_text.SESSIONS_KEYBAR_ALL, _ROW_ACTIONS))),
            cols=cli_text.SESSIONS_MIN_COLS,
        ).as_tuple()

    def _row_actions(self) -> tuple[tuple[str, str], ...]:
        """The selected row's conditional keys: E (running in the
        background), M (no end event and not running), P (a scope to repair)."""

        record = self._record()
        if record is None or self._on_banner():
            return ()
        state_ = self.states.get(sessions.managed_id(record), "unknown")
        actions = []
        if state_ == "live":
            actions.append(cli_text.SESSIONS_STOP_BINDING)
        if self._markable(record, state_):
            actions.append(cli_text.SESSIONS_MARK_BINDING)
        if record.get("version") == sessions.RECORD_VERSION:
            actions.append(cli_text.SESSIONS_REPAIR_BINDING)
        return tuple(actions)

    @staticmethod
    def _markable(record: dict[str, Any], state_: str) -> bool:
        return state_ != "live" and record.get("last_event_source") != "end"

    def _keybar(self) -> tui.KeyBar:
        base = cli_text.SESSIONS_KEYBAR if self.cwd_filter else cli_text.SESSIONS_KEYBAR_ALL
        bindings = list(_with_row_actions(base, self._row_actions()))
        if self.root:
            bindings[-1] = ("Esc", "quit")
        return tui.KeyBar(bindings)

    def _actions(self) -> str:
        if self._on_banner():
            return cli_text.SESSIONS_BANNER_ACTIONS
        record = self._record()
        if record is None:
            return ""
        summary = self.summaries[self.selected]
        v4 = record.get("version") == sessions.RECORD_VERSION
        drift: list[str] = []
        pending_reasons: list[str] = []
        lead_target = None
        needs_notice = None
        if v4:
            if self.eff is not None:
                drift = settings_mod.drift(record["applied"]["settings"], self.eff)
            pending_reasons = list((record.get("pending") or {}).get("reasons") or ())
            target = record.get("lead_target")
            if isinstance(target, Mapping):
                entry = self.lcat.lines.get(target["key"]) or self.lcat.retired.get(target["key"])
                lead_target = lineup_mod.binding_label(entry, target["effort"], key=target["key"])
            needs_notice = sessions.lead_choice_notice(record, self.lcat)
        needs = sessions.lead_key_needs_choice(summary.lead_key, self.lcat)
        repair = record.get("identity_state") == sessions.IDENTITY_REPAIR_NEEDED and (
            "observed_cwd" in record or "observed_model" not in record
        )
        return views.session_actions(
            summary,
            drift=drift,
            pending_reasons=pending_reasons,
            lead_target=lead_target,
            needs_notice=needs_notice,
            needs=needs,
            repair=repair,
        )

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
        actions_row = height - bar_rows - 2
        message_row = height - bar_rows - 1
        model = self._model(width)
        tui.safe_add(win, 1, 2, views.clip(model.title, width - 3), palette.attr("accent") | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        y = 3
        if not self.records:
            empty = cli_text.SESSIONS_EMPTY_FILTERED if self.empty_elsewhere else cli_text.SESSIONS_EMPTY
            tui.safe_add(win, y, 2, views.clip(empty, width - 3), palette.attr("dim"))
            y += 1
        else:
            extra = (1 if model.banner else 0) + (1 if model.native else 0)
            visible = max(1, actions_row - (y + 1) - extra)
            record_index = min(self.selected, len(self.records) - 1)
            if record_index < self.offset:
                self.offset = record_index
            elif record_index >= self.offset + visible:
                self.offset = record_index - visible + 1
            self.offset = max(0, min(self.offset, max(0, len(self.records) - visible)))
            table = tui.Table(
                model.columns,
                [row.cells for row in model.rows],
                selected=self.selected if not self._on_banner() else -1,
                min_widths=model.min_widths,
                gap=2,
                row_roles=[row.role for row in model.rows],
                cursor=True,
            )
            used = table.draw(win, y, 2, width - 1, palette, max_rows=visible, start=self.offset)
            y += used
            if model.banner:
                focused = self._on_banner()
                banner = views.clip(model.banner + "   [Enter]", width - 5)
                if focused:
                    tui.safe_add(win, y, 2, "› ", palette.attr("accent"))
                tui.safe_add(win, y, 4, banner, palette.attr("error") | (tui.curses.A_REVERSE if focused else 0))
                y += 1
        if model.native and y < actions_row:
            tui.safe_add(win, y, 2, views.clip(model.native, width - 3), palette.attr("dim"))
        actions = self._actions()
        if actions:
            tui.safe_add(win, actions_row, 2, views.clip(actions, width - 3), palette.attr("dim"))
        if self.message:
            tui.safe_add(win, message_row, 2, views.clip(self.message.replace("\n", " "), width - 3),
                         palette.attr(self.message_role))
        keybar.draw(win, height - 1, palette)
        win.refresh()

    # -- actions ----------------------------------------------------------------

    def _say(self, text: str, role: str = "accent") -> None:
        self.message = text
        self.message_role = role

    def _chooser(self, win: Any, records: Sequence[dict[str, Any]], notices: Mapping[str, str] | None = None) -> tuple | None:
        return _NeedsChoiceChooser(
            self.runtime, records, palette=self.palette, passthrough=self.passthrough,
            notices=notices, tty_in=self.tty_in, tty_out=self.tty_out, now=self.now,
            way_back=self.way_back.deeper(),
        ).run(win)

    def _resume(self, win: Any, record: dict[str, Any]) -> tuple | None:
        """Enter on a record: gate, prepare, the resume card."""

        runtime = self.runtime
        mid = sessions.managed_id(record)
        if record.get("pending_forks"):
            self._resolve_fork(win, record)
            return None
        if sessions.lead_key_needs_choice(self.summaries[self.selected].lead_key, self.lcat):
            return self._chooser(win, [record])
        gate = resume_checks._evaluate_resume_gate(runtime, record, live_prefixes=self.prefixes)
        decision: str | None = None
        if gate.kind == "needs-choice":
            return self._chooser(win, [record])
        if gate.kind == "daemon-owned" and self.resume_decision == "force":
            decision = "force"
        elif gate.kind != "ok":
            try:
                resolved = _run_resume_gate_modal(runtime, record, gate, win, self.palette, background=self._draw)
            except (cli_errors.CLIError, sessions.SessionError) as exc:
                self._say(termtext.visible_message(exc), "warn")
                return None
            if resolved is None:
                self._say("Resume cancelled.")
                return None
            _, record, decision = resolved
        try:
            prepared = runtime.prepare(
                selection._record_resume_target(record), action="resume", passthrough=list(self.passthrough), session_id=mid
            )
        except cli_types.NeedsChoiceError as exc:
            self._reload()
            return self._chooser(win, [exc.record], {mid: exc.notice})
        except cli_types.LaunchPlanError as exc:
            self._say("not resumed: " + "; ".join(termtext.visible_text(p) for p in exc.problems), "error")
            return None
        except _CARD_PLAN_ERRORS as exc:
            self._say("not resumed: " + termtext.visible_message(exc).replace("\n", " "), "error")
            return None
        result = _resume_card_result(
            runtime, win, prepared, palette=self.palette, passthrough=self.passthrough,
            decision=decision, tty_in=self.tty_in, tty_out=self.tty_out, now=self.now,
            way_back=self.way_back.deeper(),
        )
        if result is None:
            self._say("Resume cancelled.")
            self._reload()
        return result

    def _lineup(self, win: Any, record: dict[str, Any]) -> None:
        if record.get("version") != sessions.RECORD_VERSION:
            self._say(cli_text.SESSIONS_V3_LINEUP, "warn")  # nothing opens
            return
        state_ = self.states.get(sessions.managed_id(record), "unknown")
        applied = _LineupDialog(self.runtime, record, palette=self.palette, liveness=state_, now=self.now).run(win)
        self._reload()
        if applied:
            self._say(f"lineup of {sessions.managed_id(record)[:8]} changed (report above)")

    def _follow(self, win: Any, record: dict[str, Any]) -> None:
        """F: the read-only preview of follow (or pin) — the follow state
        before and after, a pending change it drops, the lineup consequence
        — then y applies it under the session's lock; a session that changed
        meanwhile shows the refreshed preview instead."""

        summary = self.summaries[self.selected]
        if record.get("version") != sessions.RECORD_VERSION:
            self._say(cli_text.SESSIONS_V3_LINEUP, "warn")
            return
        verb = "pin" if summary.follow else "follow"
        title = (cli_text.PIN_PREVIEW_TITLE if verb == "pin" else cli_text.FOLLOW_PREVIEW_TITLE).format(
            short=summary.managed_id[:8])
        try:
            request = lineup_mod.parse_request([verb])
            current = self.runtime.session_store.load(summary.managed_id)
            shown = lineup_mod.preview(self.runtime, current, request)
            while True:
                if not tui.TextView(title, shown.text.splitlines(), palette=self.palette, confirm=True).run(win):
                    self._say("nothing changed")
                    return
                result = lineup_mod.apply_preview(self.runtime, shown, interactive=True)
                if result.exit_code == 0:
                    break
                # The session changed since the preview: show the fresh one.
                shown = lineup_mod.preview(self.runtime, self.runtime.session_store.load(summary.managed_id),
                                           request)
        except (lineup_mod.LineupRefusal, sessions.MigrationBusyError) as exc:
            self._say(str(exc).replace("\n", " "), "warn")
            return
        except (sessions.SessionError, state.StateError, cli_errors.CLIError, OSError) as exc:
            self._say(termtext.visible_message(exc), "warn")
            return
        # The pin/follow next-step line ("recorded; …") adds nothing to the
        # status line here; a longer report still opens the modal.
        lines = [line for line in result.text.splitlines()
                 if line.strip() and line not in (lineup_mod.NEXT_RECORDED, lineup_mod.NEXT_NONE)]
        self._reload()
        if len(lines) > 1:
            wrap = max(20, min(100, win.getmaxyx()[1] - 8))
            screens_common._ScrollModal(cli_text.LINEUP_REPORT_TITLE.format(m8=summary.managed_id[:8]), screens_common._wrap_help_lines("\n".join(lines), wrap)).run(
                win, self.palette, background=self._draw
            )
        self._say(lines[0] if lines else "")

    def _rename(self, win: Any, record: dict[str, Any]) -> None:
        if record.get("version") != sessions.RECORD_VERSION:
            self._say(cli_text.SESSIONS_V3_TITLE, "warn")
            return
        mid = sessions.managed_id(record)
        field_ = tui.TextInput(record.get("title") or "", max_length=80)
        modal = tui.Modal(
            cli_text.SESSIONS_RENAME_TITLE.format(m8=mid[:8]),
            ["1-80 printable characters; empty clears the title"],
            buttons=(("Save", True), ("Cancel", False)),
            input=field_,
        )
        if not modal.run(win, self.palette, background=self._draw):
            return
        text = field_.value.strip()
        try:
            self.runtime.session_store.set_title(mid, text or None)
        except (sessions.SessionError, state.StateError, OSError) as exc:
            self._say(termtext.visible_message(exc), "warn")
            return
        self._reload()
        self._say(f'renamed {mid[:8]}: "{text}"' if text else f"title of {mid[:8]} cleared")

    def _stop_live(self, win: Any, record: dict[str, Any]) -> None:
        stable_id = sessions.managed_id(record)
        runtime_id = sessions.runtime_session_id(record)
        refusal = session_actions._stop_precheck(self.runtime, record)
        force = False
        if refusal is not None and not self.runtime.background_liveness().known:
            # Liveness cannot be observed: only a typed id sends claude stop anyway.
            if not self._typed_confirmation(win, "Stop", record, refusal):
                return
            refusal = session_actions._stop_precheck(self.runtime, record, force=True)
            force = True
        if refusal is not None:
            self._say(refusal + ".", "warn")
            return
        if not force:
            confirmed = tui.Modal(
                cli_text.STOP_MODAL_TITLE.format(short=f"{stable_id[:8]}…"),
                cli_text.STOP_MODAL_BODY.format(runtime_id=runtime_id).splitlines(),
                buttons=cli_text.STOP_BUTTONS, default=False,
            ).run(win, self.palette, background=self._draw)
            if not confirmed:
                self._say("Stop cancelled.")
                return
        problem = session_actions._stop_runtime(self.runtime, runtime_id, record=record)
        if problem is not None:
            self._say(f"stop failed: {problem[:60]}", "warn")
            return
        self._reload()
        self._say(f"stopped {stable_id[:8]}… · conversation kept; Enter resumes when ready")

    def _link_choice(self, win: Any, title: str, *, current: str | None = None) -> tuple[str | None, str | None] | None:
        """A profile (most recent first) or ``direct…`` (choose mode ``link``: no effort field)."""

        labels, choices = selection._profile_choice_items(self.runtime, current=current)
        index = screens_common._run_choice_list(
            win, self.palette, title, labels,
            help_title=cli_text.SESSIONS_HELP_TITLE, help_text=cli_text.SESSIONS_HELP, footer=(("Enter", "link"), ("?", "help"), ("Esc", "back")),
        )
        if index is None:
            return None
        kind, name = choices[index]
        if kind == "profile":
            return (name, None)
        picked = screens_direct.run_direct_screen(self.runtime, win, self.palette, purpose="link")
        if picked is None:
            return None
        return (None, picked[0])

    def _link(self, native_id: str, name: str | None, key: str | None) -> str:
        """``_sessions_link`` with a captured report; the message text."""

        buf = io.StringIO()
        namespace = argparse.Namespace(
            uuid=native_id, link_profile=name, link_direct=key,
            link_composition=None, link_model=None, link_cwd=None,
        )
        try:
            session_actions._sessions_link(self.runtime, namespace, output_stream=buf, interactive=True)
        except (cli_errors.CLIError, cli_types.LaunchPlanError, sessions.SessionError, state.StateError,
                profile_mod.ProfileError, profile_mod.BindingError, settings_mod.SettingsError,
                lineup_mod.LineupRefusal, OSError) as exc:
            if isinstance(exc, cli_types.LaunchPlanError):
                return "not linked: " + "; ".join(exc.problems)
            return "not linked: " + termtext.visible_message(exc).replace("\n", " ")
        return buf.getvalue().strip().replace("\n", " ")

    def _resolve_fork(self, win: Any, record: dict[str, Any]) -> None:
        """Enter on a ⚠ row: adopt or discard the pending fork (default Cancel)."""

        stable_id = sessions.managed_id(record)
        if sessions.drop_resolved_pending_forks(record) is not None:
            try:
                self.runtime.session_store.converge_pending_forks(stable_id)
            except sessions.SessionError as exc:
                self._reload()
                self._say(str(exc), "warn")
                return
            self._reload()
            remaining = len(self.runtime.session_store.load(stable_id).get("pending_forks", []))
            text = "fork marker cleared (runtime already resolved it)"
            text += f" · {remaining} genuine pending (Enter again)" if remaining else "; resume unblocked"
            self._say(text)
            return
        pending = record.get("pending_forks", [])
        fork_id = pending[0]["session_id"]
        wrap = max(20, min(64, win.getmaxyx()[1] - 8))
        lines = textwrap.wrap(sessions.pending_fork_message(record), wrap)
        lines += textwrap.wrap("adopt: " + sessions.fork_adopt_command(record, fork_id), wrap)
        choice = tui.Modal(
            cli_text.FORK_MODAL_TITLE.format(short=f"{stable_id[:8]}…"),
            lines,
            buttons=cli_text.FORK_BUTTONS, default=None,
        ).run(win, self.palette, background=self._draw)
        if choice == "discard":
            try:
                self.runtime.session_store.resolve_fork(stable_id, fork_id)
            except sessions.SessionError as exc:
                self._reload()
                self._say(str(exc), "warn")
                return
            self._reload()
            text = f"fork {fork_id[:12]}… marker discarded; transcript kept"
            if len(pending) > 1:
                text += f" · {len(pending) - 1} more pending (Enter again)"
            self._say(text)
            return
        if choice != "adopt":
            self._say("Resolve fork cancelled.")
            return
        picked = self._link_choice(win, cli_text.SESSIONS_ADOPT_TITLE.format(short=f"{fork_id[:8]}…"),
                                   current=record.get("profile"))
        if picked is None:
            self._say("Adopt cancelled.")
            return
        self._say(self._link(fork_id, *picked))
        self._reload()

    def _link_native(self, win: Any) -> None:
        """L: pick a native (unmanaged) session — every eligible one, the
        list scrolls — then a profile or a direct model."""

        if not self.native:
            self._say(cli_text.SESSIONS_NATIVE_NONE)
            return
        now = self._clock()
        labels = []
        for item in self.native:
            kind = "(fork)" if item.get("fork_of") else "(native)"
            live = "● " if session_facts._native_is_live(item, self.prefixes) else ""
            labels.append(f"{live}{item['session_id'][:12]}…  {kind}  {item['slug']}  {session_facts._mtime_age(item['mtime'], now=now)}")
        title = cli_text.SESSIONS_NATIVE_TITLE
        index = screens_common._run_choice_list(
            win, self.palette, title, labels,
            help_title=cli_text.SESSIONS_HELP_TITLE, help_text=cli_text.SESSIONS_HELP,
            footer=(("Enter", "link"), ("?", "help"), ("Esc", "back")),
        )
        if index is None:
            return
        item = self.native[index]
        picked = self._link_choice(win, cli_text.SESSIONS_LINK_TITLE.format(short=f"{item['session_id'][:8]}…"))
        if picked is None:
            self._say("Link cancelled.")
            return
        self._say(self._link(item["session_id"], *picked))
        self._reload()
        linked = next(
            (i for i, record in enumerate(self.records) if record.get("runtime_session_id") == item["session_id"]),
            None,
        )
        if linked is not None:
            self.selected = linked

    def _forget(self, win: Any, record: dict[str, Any]) -> None:
        session_id = sessions.managed_id(record)
        short = f"{session_id[:8]}…"
        # Live rows never reach the modal: forgetting under a running process
        # orphans it — E stops it in place first.  The under-lock pre-delete
        # check (_forget_liveness_guard) closes the residual race.
        if session_facts._record_view_is_live(record, prefixes=self.prefixes, proc_ids=self.proc_ids):
            self._say(f"session {session_id[:12]}… is live (●) — stop it first (E), then forget", "warn")
            return
        scope_exists = scope_mod.scope_dir(self.runtime.session_store.root, session_id).is_dir()
        scope_note = "" if scope_exists else " (no generated scope exists)"
        runtime_id = sessions.runtime_session_id(record)
        force = False
        verdict = self.runtime.background_liveness()
        if not verdict.known:
            reason = (f"Whether session {session_id} runs in the background cannot be checked "
                      f"({verdict.reason}).")
            if not self._typed_confirmation(win, "Forget", record, reason):
                return
            force = True
        else:
            confirmed = tui.Modal(
                cli_text.FORGET_MODAL_TITLE.format(short=short),
                cli_text.FORGET_MODAL_BODY.format(scope_note=scope_note, runtime_id=runtime_id).splitlines(),
                buttons=cli_text.FORGET_BUTTONS, default=False,
            ).run(win, self.palette, background=self._draw)
            if not confirmed:
                self._say("Forget cancelled.")
                return
        try:
            # The guard runs again under the lifecycle lock right before the delete.
            self.runtime.session_store.forget_session(
                session_id,
                pre_delete_check=session_actions._forget_liveness_guard(self.runtime, session_id, force=force),
            )
        except sessions.SessionError as exc:
            self._reload()
            self._say(str(exc), "warn")
            return
        self._reload()
        # The result stays on screen, wrapped, until it is closed: the full
        # ids and both ways to manage the conversation again.
        tui.TextView(cli_text.FORGET_DONE_TITLE.format(short=short),
                     [line.format(managed_id=session_id, runtime_id=runtime_id) for line in cli_text.FORGET_DONE_BODY],
                     palette=self.palette).run(win)
        self._say(cli_text.FORGET_DONE.format(short=short, runtime_id=runtime_id))

    def _typed_confirmation(self, win: Any, verb: str, record: dict[str, Any], reason: str) -> bool:
        """A forget or stop whose liveness cannot be checked: the person
        types the full session id (Cancel is focused first); False on
        cancel or a mismatch (said so)."""

        mid = sessions.managed_id(record)
        short = f"{mid[:8]}…"
        field_ = tui.TextInput("", max_length=64)
        body = screens_common.modal_lines(cli_text.FORCE_BODY.format(reason=reason, managed_id=mid), win)
        buttons = tuple((label.format(verb=verb), value) for label, value in cli_text.FORCE_BUTTONS)
        modal = tui.Modal(cli_text.FORCE_TITLE.format(verb=verb, short=short), body, buttons=buttons,
                          input=field_)
        confirmed = modal.run(win, self.palette, background=self._draw)
        if not confirmed:
            self._say(f"{verb} cancelled.")
            return False
        if field_.value.strip() != mid:
            self._say(cli_text.FORCE_MISMATCH.format(short=short), "warn")
            return False
        return True

    def _mark_ended(self, win: Any, record: dict[str, Any]) -> None:
        """M: a synthetic end for a session that exited without one (the
        same refusals as ``sessions mark-ended``, decided under its lock)."""

        mid = sessions.managed_id(record)
        short = f"{mid[:8]}…"
        if not self._markable(record, self.states.get(mid, "unknown")):
            self._say(cli_text.MARK_NOT_NEEDED.format(short=short) if record.get("last_event_source") == "end"
                      else f"session {short} is running — E stops it", "warn")
            return
        if not tui.Modal(cli_text.MARK_TITLE.format(short=short), screens_common.modal_lines(cli_text.MARK_BODY, win),
                         buttons=cli_text.MARK_BUTTONS, default=False).run(win, self.palette, background=self._draw):
            self._say("nothing changed")
            return
        try:
            ((_id, outcome),) = session_actions._mark_ended_ids(self.runtime, [mid])
        except (sessions.SessionError, state.StateError, cli_errors.CLIError, OSError) as exc:
            self._say(termtext.visible_message(exc), "warn")
            return
        self._reload()
        self._say(f"{short}: {outcome}", "warn" if outcome.startswith("refused") else "accent")

    def _repair(self, win: Any, record: dict[str, Any]) -> None:
        """P: rebuild the session's scope from its record (doctor's repair)."""

        mid = sessions.managed_id(record)
        short = f"{mid[:8]}…"
        if record.get("version") != sessions.RECORD_VERSION:
            self._say(cli_text.REPAIR_LEGACY, "warn")
            return
        if not tui.Modal(cli_text.REPAIR_TITLE.format(short=short),
                         screens_common.modal_lines(cli_text.REPAIR_BODY, win),
                         buttons=cli_text.REPAIR_BUTTONS, default=False).run(win, self.palette, background=self._draw):
            self._say("nothing changed")
            return
        if not self.runtime.allow_state_writes:
            self._say(cli_text.SETTINGS_READ_ONLY, "warn")
            return
        buf = io.StringIO()
        try:
            code = doctor_actions._doctor_repair(self.runtime, mid, buf)
        except (cli_errors.ClaudeMultiError, sessions.SessionError, state.StateError, OSError) as exc:
            self._say(termtext.visible_message(exc), "warn")
            return
        self._reload()
        tui.TextView(cli_text.REPAIR_RESULT_TITLE.format(short=short), buf.getvalue().splitlines() or ["(no output)"],
                     palette=self.palette).run(win)
        self._say(f"repair of {short} " + ("done" if code == 0 else f"ended with exit {code}"),
                  "accent" if code == 0 else "warn")

    def _details(self, win: Any, record: dict[str, Any]) -> None:
        """V: identity, directory, runtime id, the applied lineup, the
        pending change and why it waits, follow, drift, the routing
        explanation and the observed usage of the last 24 hours (one report
        snapshot; missing observations are unavailable, never zero)."""

        from datetime import timedelta

        from claude_multi import routing
        from claude_multi import usage as usage_mod

        runtime = self.runtime
        summary = self.summaries[self.selected]
        mid = summary.managed_id
        v4 = record.get("version") == sessions.RECORD_VERSION
        lead, agents, drift, pending_target = "", [], [], None
        explain = usage_lines = None
        if v4:
            applied = record["applied"]
            lead = self._binding_text(applied["lead"])
            agents = [f"{profile_mod.label(rid)}: {self._binding_text(binding)}"
                      for rid, binding in applied.get("agents", {}).items()]
            if self.eff is not None:
                drift = settings_mod.drift(applied["settings"], self.eff)
            pending = record.get("pending")
            if isinstance(pending, Mapping):
                pending_target = (f"profile {pending['profile']}" if pending.get("profile")
                                  else "an ad-hoc lineup")
        with runtime.report_snapshot():
            if v4:
                try:
                    explain = routing.text(session_facts.explanation(runtime, mid)).splitlines()
                except (cli_errors.ClaudeMultiError, sessions.SessionError, OSError, ValueError, KeyError):
                    explain = None
            try:
                now = gateway_facts._doctor_now()
                start = now - timedelta(hours=24)
                events = gateway_facts.report_events(runtime, since=start.isoformat())
                if events.coverage != "unavailable":
                    usage_lines = usage_mod.text(usage_mod.report(events, start, now, mid)).splitlines()
            except (cli_errors.ClaudeMultiError, OSError, ValueError):
                usage_lines = None
        lines = views.session_details(
            summary, record, state=self.states.get(mid, "unknown"), lead=lead, agents=agents, drift=drift,
            pending_target=pending_target, explain=explain, usage=usage_lines)
        tui.TextView(cli_text.SESSIONS_DETAILS_TITLE, lines, palette=self.palette).run(win)

    def _binding_text(self, binding: Any) -> str:
        if not isinstance(binding, Mapping):
            return lineup_mod.UNBOUND
        key = str(binding.get("key") or binding.get("model"))
        entry = self.lcat.lines.get(key) or self.lcat.retired.get(key)
        return lineup_mod.binding_label(entry, str(binding.get("effort")), key=key)

    def _help(self, win: Any) -> None:
        wrap = max(20, min(100, win.getmaxyx()[1] - 8))
        screens_common._ScrollModal(cli_text.SESSIONS_HELP_TITLE, screens_common._wrap_help_lines(cli_text.SESSIONS_HELP, wrap)).run(
            win, self.palette, background=self._draw
        )

    def run(self, win: Any) -> tuple | None:
        """``("perform", prepared, decision)`` or None (Esc)."""

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
            ch = key.ch if key.kind == "char" and key.ch else ""
            items = self._items()
            if ch == "?":
                self._help(win)
            elif ch in ("c", "C"):
                self.cwd_filter = not self.cwd_filter
                self.selected = 0
                self.offset = 0
                self._reload()
                self._say("showing only this directory" if self.cwd_filter else "showing all directories")
            elif ch in ("l", "L"):
                self._link_native(win)
            elif not items:
                continue
            elif key.kind == "up" or ch == "k":
                self.selected = max(0, self.selected - 1)
            elif key.kind == "down" or ch == "j":
                self.selected = min(items - 1, self.selected + 1)
            elif key.kind == "pageup":
                self.selected = max(0, self.selected - 10)
            elif key.kind == "pagedown":
                self.selected = min(items - 1, self.selected + 10)
            elif key.kind == "home":
                self.selected = 0
            elif key.kind == "end":
                self.selected = items - 1
            elif self._on_banner():
                if key.kind == "enter":
                    needing = [self.records[i] for i in self._needs()]
                    outcome = self._chooser(win, needing)
                    if outcome is not None:
                        return outcome
                    self._reload()
            else:
                record = self._record()
                if record is None:
                    continue
                if key.kind == "enter":
                    outcome = self._resume(win, record)
                    if outcome is not None:
                        return outcome
                elif ch in ("t", "T"):
                    self._lineup(win, record)
                elif ch in ("f", "F"):
                    self._follow(win, record)
                elif ch in ("r", "R"):
                    self._rename(win, record)
                elif ch in ("x", "X"):
                    self._forget(win, record)
                elif ch in ("e", "E"):
                    self._stop_live(win, record)
                elif ch in ("v", "V"):
                    self._details(win, record)
                elif ch in ("m", "M"):
                    self._mark_ended(win, record)
                elif ch in ("p", "P"):
                    self._repair(win, record)


def _sessions_list_tui(
    runtime: runtime_mod.Runtime,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    no_color: bool,
    passthrough: Sequence[str] = (),
    resume_decision: str | None = None,
) -> tuple | None:
    """The sessions screen in its own curses session (bare ``-r``); Esc quits it.

    Returns ``("perform", prepared, decision)`` after curses teardown, or
    None; the caller performs, never inside curses.  ``passthrough`` follows
    the resume the user picks here, as if they had typed ``-r <uuid>``.
    """

    palette = tui.detect_palette(
        runtime.environ, no_color=no_color, tty_in=input_stream, tty_out=output_stream
    )
    screen = _SessionsScreen(
        runtime,
        palette=palette,
        passthrough=passthrough,
        resume_decision=resume_decision,
        tty_in=input_stream,
        tty_out=output_stream,
        root=True,
    )
    return screens_common._run_screen_curses(
        screen.run, input_stream=input_stream, output_stream=output_stream, palette=palette,
        what="the sessions screen",
    )


def _needs_choice_tui(
    runtime: runtime_mod.Runtime,
    exc: cli_types.NeedsChoiceError,
    *,
    passthrough: Sequence[str],
    input_stream: TextIO,
    output_stream: TextIO,
    no_color: bool,
) -> tuple | None:
    """``_NeedsChoiceChooser`` for the record of ``exc`` (``-r <id>`` needing a choice)."""

    palette = tui.detect_palette(
        runtime.environ, no_color=no_color, tty_in=input_stream, tty_out=output_stream
    )
    record = exc.record
    chooser = _NeedsChoiceChooser(
        runtime,
        [record],
        palette=palette,
        passthrough=passthrough,
        notices={sessions.managed_id(record): exc.notice},
        tty_in=input_stream,
        tty_out=output_stream,
    )
    return screens_common._run_screen_curses(
        chooser.run, input_stream=input_stream, output_stream=output_stream, palette=palette,
        what="the needs-a-choice chooser",
    )


_CARD_CONTEXT_KEYS = ("window", "trigger", "percent", "scalar")


def _card_workflow_window(runtime: Any, eff: Any, lineup: Any) -> Any:
    """The card's workflow-default row data (``views.workflow_window``), or None."""

    if eff is None or lineup is None:
        return None
    return views.workflow_window(runtime.lineup_catalog(), eff, lineup)


_CARD_PLAN_ERRORS = (
    launch.LaunchError,
    cli_errors.CLIError,
    scope_mod.ScopeError,
    compiler.CompilerError,
    profile_mod.ProfileError,
    profile_mod.BindingError,
    settings_mod.SettingsError,
    sessions.SessionError,
)


def _claude_card_row(runtime: Any) -> tuple[str | None, str | None]:
    """The card's Claude Code row and the owned-copy problem, if any.

    The pin and its evidence class for this platform, the owned copy's state
    (metadata only; every launch hashes it) and, when it differs, the user's
    own ``claude`` version. ``(None, None)`` when the runtime's binary report
    is a test seam (``Runtime.pin_report`` off)."""

    if not runtime.pin_report:
        return None, None
    contract = runtime.catalog.docs["native-contract"]
    try:
        platform = pin_mod.host_platform()
        version = pin_mod.version(contract)
        state, problem = pin_mod.copy_state(contract, runtime.environ, platform=platform,
                                            retained_root=product_paths.retained_root(runtime.environ))
    except (cli_errors.ClaudeMultiError, KeyError, TypeError, ValueError, OSError) as exc:
        return None, f"Claude Code cannot run here: {termtext.visible_message(exc)}"
    evidence = pin_mod.evidence_class(contract, platform)
    text = f"Claude Code {version}"
    if evidence:
        text += f" · {evidence} evidence on {platform}"
    if state == "pending":
        text += " · copied into place at launch"
    client = pin_mod.installed_client(runtime.environ)
    if client is not None and client.version and client.version != version:
        text += f" · your claude {client.version}"
    return text, (problem if state not in ("ready", "pending") else None)


def _claude_card_staleness(runtime: Any) -> str | None:
    """Doctor's pin-staleness Attention for the card (``pin.staleness``: the
    pin verified more than 30 days ago, or the user's own claude is newer),
    or None. A fact with no command, so it never loops back to a remedy."""

    if not runtime.pin_report:
        return None
    try:
        client = pin_mod.installed_client(runtime.environ)
        return pin_mod.staleness(runtime.catalog.docs["native-contract"], gateway_facts._doctor_now().date(),
                                 client.version if client is not None else None)
    except (cli_errors.ClaudeMultiError, KeyError, TypeError, ValueError, OSError):
        return None


def _card_new_lines(runtime: Any, eff: Any) -> tuple[str, ...]:
    """Short names of New lines without an optional admission badge."""

    if eff is None:
        return ()
    try:
        registry = custom.load_registry(runtime.environ)
        rows = views.line_rows(runtime.lineup_catalog(), eff, custom_ids=frozenset(registry["models"]))
    except (cli_errors.ClaudeMultiError, KeyError, TypeError, ValueError, OSError):
        return ()
    return tuple(row.short for row in rows
                 if row.source == "catalog" and row.status == "new" and not row.admitted and row.provider_enabled)


def _writers_unavailable(prepared: Any) -> bool:
    """Whether the prepared launch's lineup marks its implementer agents
    unavailable (the compile found no Git work tree to isolate them in)."""

    plan = getattr(getattr(prepared, "result", None), "scope_plan", None)
    if plan is None:
        return False
    note = scope_mod.WRITER_UNAVAILABLE_NOTE.encode()
    return any(path.endswith("lineup.md") and note in body for path, body in plan.other_files.items())


def _card_notices(runtime: Any, eff: Any, prepared: Any) -> tuple[tuple[str, ...], ...]:
    """The card's own notice rows ``(kind, text, *shorter forms)``: an
    ignored ``CLAUDE_CONFIG_DIR``, new model lines still off, and
    implementer agents without a Git repository to isolate them in."""

    notices: list[tuple[str, ...]] = []
    configured = runtime.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        shown = product_paths.display(configured, runtime.environ) if Path(configured).is_absolute() else configured
        notices.append(("config-dir", views.CARD_CONFIG_DIR.format(path=shown),
                        *(text.format(path=shown) for text in views.CARD_CONFIG_DIR_SHORT)))
    names = _card_new_lines(runtime, eff)
    if names:
        listed = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
        notices.append(("new-lines", views.CARD_NEW_LINES.format(n=len(names), names=listed),
                        *(text.format(n=len(names)) for text in views.CARD_NEW_LINES_SHORT)))
    if _writers_unavailable(prepared):
        notices.append(("worktree", views.CARD_WORKTREE, *views.CARD_WORKTREE_SHORT))
    return tuple(notices)


def _card_sign_in_route(way_back: _WayBack | None) -> str:
    """How a card says to sign in: ``G → L`` on a fresh card; on a resume
    card (which has no G) its way back to a launch card first."""

    if way_back is None:
        return cli_text.CARD_SIGN_IN_FRESH
    return cli_text.CARD_SIGN_IN_RESUME.format(way=way_back.text())


# A finding's remedy (after its " — ") that saves a provider's key; a
# command quoted inside other advice (a file to correct) stays as it is.
_SET_KEY_COMMAND = re.compile(r"(?<= — )claude-multi providers set-key ([a-z][a-z0-9-]*)")


def _card_key_route(display: str, way_back: _WayBack | None) -> str:
    """How a card says to save a provider's API key: ``G (providers) → K``
    on that provider; on a resume card (which has no G) its way back to a
    launch card first."""

    if way_back is None:
        return cli_text.CARD_SET_KEY_FRESH.format(display=display)
    return cli_text.CARD_SET_KEY_RESUME.format(way=way_back.text(), display=display)


def _card_doctor_line(runtime: Any, text: str, way_back: _WayBack | None) -> str:
    """A doctor line as the card's H says it: the command that saves a
    provider's key becomes the card's key route (the command line keeps
    the command)."""

    if _SET_KEY_COMMAND.search(text) is None:
        return text
    try:
        providers = runtime.effective_transport_docs()["providers"]["providers"]
    except Exception:  # a provider's name is never worth breaking H over: its id says it too
        providers = {}

    def route(match: "re.Match[str]") -> str:
        provider = providers.get(match.group(1))
        display = provider.get("display") if isinstance(provider, Mapping) else None
        return cli_text.CARD_SET_KEY.format(route=_card_key_route(str(display or match.group(1)), way_back))

    return _SET_KEY_COMMAND.sub(route, text)


def _card_login_routes(route: str) -> dict[str, str]:
    """Pool -> how the card says to sign in: its key route (the command
    line keeps the sign-in commands)."""

    return {pool: route for pool in gateway_facts._OAUTH_LOGIN_COMMANDS}


def _card_remedy(text: str, route: str) -> str:
    """A readiness line as the card says it: a sign-in command becomes the
    card's key route (``sign in: G → L``, or the way back first on a
    resume card)."""

    commands = sorted({gateway_facts.SIGN_IN_ANY, *gateway_facts._OAUTH_LOGIN_COMMANDS.values()},
                      key=len, reverse=True)
    for command in commands:
        text = text.replace(command, "sign in: " + route)
    return text


def _card_slot_groups(lines: Sequence[str]) -> list[str]:
    """Readiness lines (``! <slot>: <reason> — <remedy>``) that differ only
    in their slot become one line each, naming the slots (``lead + 2
    agents`` past two), at the place of the first; V lists every slot."""

    labels = {profile_mod.label(slot) for slot in (catalog.LEAD_ROLE, *catalog.AGENT_ROLE_IDS)}
    slots: dict[str, list[str]] = {}
    order: list[tuple[bool, str]] = []
    for line in lines:
        slot, sep, rest = line[2:].partition(": ") if line.startswith("! ") else ("", "", "")
        if not (sep and slot in labels and " — " in rest):
            order.append((False, line))
            continue
        if rest not in slots:
            slots[rest] = []
            order.append((True, rest))
        slots[rest].append(slot)
    out: list[str] = []
    for grouped, text in order:
        if not grouped:
            out.append(text)
            continue
        named = slots[text]
        if len(named) <= 2:
            who = " and ".join(named)
        else:
            agents = len([slot for slot in named if slot != "lead"])
            who = ("lead + " if "lead" in named else "") + f"{agents} agents"
        out.append(f"! {who}: {text}")
    return out


def _card_session_details(runtime: Any, prepared: Any) -> list[str]:
    """V's session sections: the ``/model`` lead set, the kept environment
    (names only), how a managed session differs from plain claude, and the
    terms and ``/cm`` guidance the help shows."""

    lines: list[str] = []
    result = getattr(prepared, "result", None)
    fence = getattr(result, "fence", None)
    if fence is not None and fence.lead_set:
        lines += ["", cli_text.CARD_DETAILS_MODEL]
        lines += [f"  {row.label} — {row.selector}" for row in fence.lead_set]
    keep = getattr(result, "env_keep", None)
    lines += ["", cli_text.CARD_DETAILS_ENV_TITLE]
    if keep is None or not (keep.kept or keep.stripped or keep.refused):
        lines.append(cli_text.CARD_DETAILS_ENV_NONE)
    else:
        lines.append(cli_text.CARD_DETAILS_ENV_KEPT.format(names=", ".join(keep.kept) or "none"))
        lines.append(cli_text.CARD_DETAILS_ENV_STRIPPED.format(names=", ".join(keep.stripped) or "none"))
        lines += [cli_text.CARD_DETAILS_ENV_REFUSED.format(name=name, reason=reason) for name, reason in keep.refused]
    lines.append(cli_text.CARD_DETAILS_ENV_WHY)
    lines += ["", cli_text.CARD_DETAILS_MANAGED_TITLE, *cli_text.CARD_DETAILS_MANAGED]
    configured = runtime.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        shown = product_paths.display(configured, runtime.environ) if Path(configured).is_absolute() else configured
        lines.append("  " + doctor.CONFIG_DIR_IGNORED.format(path=shown))
    lines += ["", cli_text.CARD_TERMS_TITLE, *cli_text.CARD_TERMS]
    lines += ["", cli_text.CARD_CM_TITLE, *cli_text.CARD_CM_GUIDANCE]
    return lines


def _lead_origin(runtime: Any, lineup: Any) -> str | None:
    """The origin of the lineup's lead line (catalog | operator | ...), or None."""

    if lineup is None:
        return None
    try:
        return runtime.lineup_catalog().origin(lineup.lead.binding.key)
    except (cli_errors.ClaudeMultiError, KeyError, AttributeError):
        return None


class _LaunchCardScreen(_CardHealthActions):
    """The launch card.

    On open, and after every key that changes the target or returns from
    G/M/O/E/H/U/W/P, :meth:`_plan` evaluates and prepares the target (a
    resume card uses the ``prepared`` its caller made) and builds the
    ``views.card_model``.  Enter on a Ready card returns ``("perform",
    prepared[, decision])``; D (Direct) and S (sessions) may return the same
    shape.  Esc returns None.  The card holds no lock and writes nothing
    itself: the editor, Get started, Profiles, Models, Providers and
    Settings write through the setup layer and the store APIs.

    ``explicit`` says the person named the profile (``--profile``); with
    ``auto_setup`` a fresh card opens Get started on its own once, right
    after its first health check, while Claude Code is not set up or no
    provider is connected (never in a Claude Code session or a read-only
    run).  A target that cannot be loaded (``kind == "unloadable"``) shows
    a BLOCKED card naming the file and line; Tab, P and E stay available.
    """

    what = "the launch card"

    def __init__(
        self,
        runtime: runtime_mod.Runtime,
        target: cli_types.LaunchTarget,
        *,
        action: str = "fresh",
        session_id: str | None = None,
        prepared: cli_types.PreparedLaunch | None = None,
        passthrough: Sequence[str],
        palette: tui.Palette,
        update_hint: tuple[str, str] | None = None,
        gateway_problem: str | None = None,
        gateway_checked: bool = False,
        gateway_check: Any | None = None,
        hint_detector: Any | None = None,
        tty_in: Any | None = None,
        tty_out: Any | None = None,
        resume_decision: str | None = None,
        now: datetime | None = None,
        tz=None,
        journal: Callable[[], str | None] | None = None,
        explicit: bool = False,
        auto_setup: bool = False,
        way_back: _WayBack | None = None,
    ):
        if action != "fresh" and prepared is None:
            raise ValueError("a resume card is built with the prepared launch of its caller")
        # A resume card's way back to a launch card (its sign-in and key
        # remedies say it): Esc ends claude-multi -r <id> unless a screen
        # that opened the card says otherwise.
        self.way_back = (way_back or _WayBack()) if action != "fresh" else None
        self.runtime = runtime
        self.target = target
        self.action = action
        self.session_id = session_id
        self.prepared = prepared
        self.passthrough = list(passthrough)
        self.palette = palette
        self.update_hint = update_hint
        self.gateway_problem = gateway_problem
        self.gateway_checked = gateway_checked
        self.gateway_check = gateway_check
        # The U row: an installed release (its version and age; U runs the
        # update) or the Nix package (U says to update the flake); a source
        # checkout shows none.
        self.hint_detector = hint_detector if hint_detector is not None else self._release_hint
        self.tty_in = tty_in if tty_in is not None else sys.stdin
        self.tty_out = tty_out if tty_out is not None else sys.stdout
        self.resume_decision = resume_decision
        self.now = now
        self.tz = tz
        self.pool = quota.PoolStatus("seam")
        self._refresh_pool()
        self.journal = journal
        self.message = ""
        self.message_role = "accent"
        self.eval_errors: tuple[str, ...] = ()
        self.lineup: profile_mod.ResolvedLineup | None = None
        self.card: views.CardModel
        # The person chose the card's profile (on the command line, with Tab
        # or in Profiles): Get started then leaves it on the card.
        self.chosen = explicit
        self.auto_setup = auto_setup and action == "fresh"
        # The file error of a profile that cannot be loaded (its BLOCKED card).
        self.load_error: profile_mod.ProfileError | None = (
            runtime.selection_load_error if target.kind == "unloadable" else None)
        # The default-profile notice is kept for the target it was resolved
        # for, through every replan.
        self.notice_key: tuple[str, str | None] | None = (target.kind, target.profile) if action == "fresh" else None
        self.target_notices: tuple[str, ...] = (
            tuple(f"! {line}" for line in runtime.selection_notices) if action == "fresh" else ())
        self.unloadable_names: set[str] = set()
        self.pick_order: list[str] = []
        if action == "fresh":
            self._refresh_order(target.profile)
        self.not_connected = False
        self._observe_setup()
        self._replan()

    def _refresh_pool(self) -> None:
        # Fetch only on open/health refresh, never on editor/navigation replans.
        self.pool = (self.runtime.pool_status() if self.gateway_checked and self.gateway_problem is None
                     else quota.PoolStatus("down" if self.gateway_problem else "seam"))

    def _refresh_health(self) -> None:
        super()._refresh_health()
        self._refresh_pool()

    def _quota_summary(self, lineup, *, width: int = 67):
        providers = self.runtime.effective_transport_docs()["providers"]["providers"]
        bindings = [lineup.lead.binding, *(a.binding for a in lineup.agents.values())] if lineup else []
        pools = {providers[b.provider]["transport"]["pool"] for b in bindings
                 if providers[b.provider]["transport"]["kind"] == "oauth-pool"}
        return quota.card_summary(self.pool, pools=pools, login_commands=_card_login_routes(self.sign_in_route),
                                  now=self.now or gateway_facts._doctor_now(), tz=self.tz,
                                  resume=self.resume, width=width)

    # -- the plan (§4.1 _plan steps 0-7) -------------------------------------

    @property
    def resume(self) -> bool:
        return self.action != "fresh"

    @property
    def sign_in_route(self) -> str:
        return _card_sign_in_route(self.way_back)

    def _plan(self) -> tuple[cli_types.PreparedLaunch | None, views.CardModel]:
        runtime = self.runtime
        target = self.target
        ev: profile_mod.Evaluation | None = None
        prepared: cli_types.PreparedLaunch | None = None
        lineup: profile_mod.ResolvedLineup | None = None
        context: dict[str, Any] | None = None
        eff: settings_mod.Effective | None = None
        errors: list[str] = []
        eval_errors: tuple[str, ...] = ()
        unloadable = not self.resume and target.kind == "unloadable"
        try:
            eff = runtime.current_effective()
        except cli_errors.CLIError as exc:
            errors.append(str(exc))
        else:
            if unloadable:
                pass  # nothing to evaluate: the card names the file and line
            elif not self.resume:
                try:
                    ev = profile_mod.evaluate(
                        target.document,
                        runtime.lineup_catalog(),
                        bindings=runtime.bindings.bindings(),
                        effective=eff,
                        ad_hoc=target.profile is None,
                    )
                except profile_mod.BindingError as exc:
                    errors.append(f"cannot load named bindings: {exc}")
                else:
                    if ev.errors:
                        eval_errors = tuple(ev.errors)
                        errors.extend(ev.errors)
                    else:
                        try:
                            prepared = runtime.prepare(
                                target, action="fresh", passthrough=list(self.passthrough)
                            )
                        except cli_types.LaunchPlanError as exc:
                            errors.extend(exc.problems)
                        except _CARD_PLAN_ERRORS as exc:
                            errors.append(termtext.visible_message(exc))
            else:
                prepared = self.prepared
        if prepared is not None:
            lineup = prepared.lineup
        elif ev is not None:
            lineup = ev.lineup
        self.eval_errors = eval_errors
        self.lineup = lineup
        if lineup is not None:
            errors.extend(runtime.managed_launch_blocks(lineup))
        claude_row, copy_problem = _claude_card_row(runtime)
        if copy_problem is not None:
            errors.append(copy_problem)
        notices = (_card_slot_groups([_card_remedy(line, self.sign_in_route) for line in prepared.notices])
                   if prepared is not None else [])
        if not self.resume and (target.kind, target.profile) == self.notice_key:
            # The default-profile notice survives replans of its target.
            notices[:0] = [line for line in self.target_notices if line not in notices]
        stale = _claude_card_staleness(runtime)
        if stale is not None:
            notices.append(f"! {stale}")
        collisions: list[str] = []
        project: tuple[int, list[str]] | None = None
        if lineup is not None:
            cwd = str(prepared.record["cwd"]) if (self.resume and prepared is not None) else str(runtime.cwd)
            hits = scope_mod.find_cm_collisions(cwd, [], frozenset(lineup.agents))
            collisions = [session_facts._collision_error(path, name, cwd) for path, name in hits]
            project = (len(session_facts._project_agent_files(cwd)), collisions)
        if prepared is not None:
            compaction = prepared.record["applied"]["lead"]["compaction"]
            context = {key: compaction[key] for key in _CARD_CONTEXT_KEYS}
        elif lineup is not None and eff is not None:
            try:
                context = views.effective_context(runtime, lineup, eff).as_document()
            except (scope_mod.ScopeError, settings_mod.SettingsError, compiler.CompilerError):
                context = None
        catalog_ = runtime.catalog
        health = (
            self.gateway_problem,
            self.gateway_checked,
            pin_mod.version(catalog_.docs["native-contract"]),
            catalog_.contract_source,
            runtime.catalog_version,
        )
        try:
            seed_updates = runtime.profiles.seed_updates()
        except profile_mod.ProfileError:
            seed_updates = []
        radar_hits = sum(1 for line in doctor._doctor_retirement_radar(runtime) if line.startswith("line "))
        description: str | None = None
        is_default = False
        spend: tuple[str, ...] = ()
        if not self.resume:
            from claude_multi.setup import defaults

            description = str((target.document or {}).get("description") or "") or None
            is_default = target.profile is not None and defaults.chosen_default(runtime) == target.profile
            if lineup is not None:
                try:
                    spend = defaults.spend_notes(runtime, lineup)
                except (cli_errors.ClaudeMultiError, KeyError, ValueError):
                    spend = ()
        load_error = self.load_error if unloadable else None
        load_path = None
        if isinstance(load_error, profile_mod.ProfileLoadError):
            load_path = product_paths.display(load_error.path, runtime.environ)
        elif unloadable and load_error is None:
            errors.append(f"cannot load profile {target.profile!r}")
        card = views.card_model(
            target=target,
            action=self.action,
            record=prepared.record if prepared is not None else None,
            lineup=lineup,
            lead_origin=_lead_origin(runtime, lineup),
            context=context,
            eff=eff,
            errors=errors,
            secret_problems=prepared.secret_problems if prepared is not None else (),
            collisions=collisions,
            health=health,
            quota_row=self._quota_summary(lineup),
            start_hint=gateway_facts.gateway_service_hint("start", runtime),
            update_hint=self.update_hint,
            seed_updates=seed_updates,
            providers=runtime.lineup_catalog().providers,
            radar_hits=radar_hits,
            project=project,
            diff=prepared.diff if (self.resume and prepared is not None) else (),
            notices=tuple(notices),
            claude_row=claude_row,
            description=description,
            is_default=is_default,
            spend=spend,
            not_connected=self.not_connected and not self.resume,
            load_error=load_error,
            load_path=load_path,
            workflow_default=_card_workflow_window(runtime, eff, lineup),
            resume_way=self.way_back.text() if self.way_back is not None else views.RESUME_WAY_DIRECT,
        )
        return prepared, views.card_with_notices(card, _card_notices(runtime, eff, prepared))

    def _details(self, win: Any) -> None:
        lines = list(views.card_detail_lines(self.card))
        if self.lineup is not None:
            lines += ["", "Review routing (operator-declared families are labelled)"]
            lines += ["  ".join(row) for row in views.routing_rows(self.lineup)]
            lines += ["", "Per-slot readiness", readiness_mod.LEGEND]
            report = self.runtime.lineup_readiness(self.lineup)
            lines += [_card_remedy(row.text(), self.sign_in_route) for row in report]
        if self.lineup is not None:
            providers = self.runtime.effective_transport_docs()["providers"]["providers"]
            for pid in sorted({self.lineup.lead.binding.provider, *(a.binding.provider for a in self.lineup.agents.values())}):
                transport = providers[pid]["transport"]
                if transport["kind"] == "oauth-pool":
                    finding = quota.worst((c for c in self.pool.credentials if c.provider == transport["pool"]),
                                          self.now or gateway_facts._doctor_now())
                    if finding and finding.level in {"high", "exhausted", "payment"}:
                        lines += [quota.guidance(pid, "resume" if self.resume else "fresh")]
        lines += ["", "Quota (passive observations)"]
        provider_by_pool = {p["transport"]["pool"]: pid for pid, p in self.runtime.lineup_catalog().providers.items()
                            if p["transport"]["kind"] == "oauth-pool"}
        lines += quota.command_lines(self.pool, provider_by_pool=provider_by_pool,
                                     login_commands=_card_login_routes(self.sign_in_route),
                                     restart_hint=gateway_facts.gateway_service_hint("restart", self.runtime),
                                     now=self.now or gateway_facts._doctor_now(), tz=self.tz,
                                     surface="resume" if self.resume else "fresh")[1]
        lines += _card_session_details(self.runtime, self.prepared)
        tui.TextView("lineup — full details", lines, palette=self.palette).run(win)

    def _replan(self) -> None:
        prepared, self.card = self._plan()
        if not self.resume:
            self.prepared = prepared

    # -- layout --------------------------------------------------------------

    def _keybar(self) -> tui.KeyBar:
        return tui.KeyBar(self.card.keys)

    def min_size(self, width: int) -> tuple[int, int]:
        """(rows, cols) floor (§3.4): the chrome plus the longest bar (fresh, with ``U``)."""

        longest = views.card_model(
            target=self.target, lineup=None, update_hint=("0", "0")
        ).keys
        return views.ScreenFloor.compute(
            chrome=cli_text.CARD_CHROME,
            list_rows=0,
            detail_reserve=0,
            bar_rows=tui.KeyBar(longest).rows(width),
            cols=cli_text.CARD_MIN_COLS,
        ).as_tuple()

    def _draw_row(self, win: Any, y: int, row: views.CardRow, width: int, widths: Sequence[int]) -> None:
        palette = self.palette
        limit = width - 1
        if row.kind == "title":
            tui.safe_add(win, y, 2, row.label, palette.attr("accent") | tui.curses.A_BOLD)
            tui.safe_add(win, y, 15, views.clip(row.text, limit - 15), palette.attr(row.role))
            return
        if row.kind == "rule":
            tui.safe_add(win, y, 2, views.rule(width), palette.attr("dim"))
            return
        if row.kind in ("header", "agent"):
            text = views.card_row_text(row, width, widths)[2:]
            attr = palette.attr(row.role) | (tui.curses.A_BOLD if row.kind == "header" else 0)
            tui.safe_add(win, y, 2, views.clip(text, limit - 2), attr)
            return
        if row.kind == "overflow":
            tui.safe_add(win, y, 0, views.clip(row.text, limit), palette.attr("dim"))
            return
        if row.col in (2, 4):
            # Notices and warnings: a shorter form, or the middle cut, so the
            # remedy after " — " stays on the row.
            text = views.fit_text((row.text, *row.short_forms), limit - row.col)
            tui.safe_add(win, y, row.col, text, palette.attr(row.role))
            return
        if row.label:
            tui.safe_add(win, y, 2, row.label, palette.attr(row.label_role))
        col = row.col
        text = row.text
        if row.kind == "retirement" and len(text) > limit - col:
            text = "retirement change at this resume — V details"
        if row.kind == "quota":
            summary = self._quota_summary(self.lineup, width=limit - col)
            text = summary[0] if summary else ""
        text = views.clip(text, max(0, limit - col))
        tui.safe_add(win, y, col, text, palette.attr(row.role))
        col += len(text)
        if row.kind == "lineup":
            col += tui.Badge(row.badge, row.badge_role).draw(win, y, col, palette) if col < limit else 0
            if col < limit:
                tui.safe_add(win, y, col, views.clip(" · " + row.cells[0], limit - col), palette.attr(row.role))
            return
        if row.badge and col + 3 < limit:
            tui.Badge(views.clip(row.badge, limit - col - 3), row.badge_role).draw(win, y, col + 3, palette)

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows_needed, cols_needed = self.min_size(width)
        if height < rows_needed or width < cols_needed:
            screens_common._screen_too_small(win, palette, self.what, rows_needed, cols_needed)
            return
        win.erase()
        model = self.card
        keybar = self._keybar()
        bar_rows = keybar.rows(width)
        bottom = height - bar_rows
        widths = views.card_widths(model, width)
        y = 1
        for row in views.card_fit(model, height=height, bar_rows=bar_rows):
            self._draw_row(win, y, row, width, widths)
            y += 1
        y += 1
        status = views.card_status(model)
        badge = tui.Badge("Status  " + views.CARD_STATUS_TEXT[status], views.CARD_STATUS_ROLE[status])
        used = badge.draw(win, y, 2, palette)
        if self.message:
            col = 2 + used + 3
            tui.safe_add(win, y, col, views.clip(self.message, width - 1 - col), palette.attr(self.message_role))
        y += 1
        wrap_width = max(20, width - 6)
        for error in model.errors:
            for line in textwrap.wrap(error, wrap_width, subsequent_indent="  ",
                                      break_long_words=False, break_on_hyphens=False) or [""]:
                if y >= bottom:
                    break
                tui.safe_add(win, y, 4, views.clip(line, width - 5), palette.attr("error"))
                y += 1
        if model.remedy and y < bottom:
            tui.safe_add(win, y, 4, views.clip(model.remedy, width - 5), palette.attr("accent"))
        keybar.draw(win, height - 1, palette)
        win.refresh()

    # -- keys ----------------------------------------------------------------

    def _say(self, text: str, role: str = "accent") -> None:
        self.message = text
        self.message_role = role

    def _retarget(self, name: str) -> None:
        try:
            document = self.runtime.profiles.load(name)
        except profile_mod.ProfileError as exc:
            self._say(str(exc), "warn")
            return
        self.target = cli_types.LaunchTarget("profile", document, name, True, f"Profile {name}")
        self.load_error = None
        self._replan()

    def _refresh_order(self, current: str | None) -> None:
        """Tab's order: connected profiles first, then the others; those that
        cannot be loaded are listed last and skipped."""

        from claude_multi.setup import defaults

        choices = defaults.profile_choices(self.runtime, current=current)
        self.pick_order = [choice.name for choice in choices]
        self.unloadable_names = {choice.name for choice in choices if not choice.loadable}

    def _cycle(self, delta: int) -> None:
        order = self.pick_order
        current = self.target.profile
        usable = [name for name in order if name not in self.unloadable_names]
        if not usable or (len(usable) == 1 and usable[0] == current):
            self._say(cli_text.CARD_ONE_PROFILE)
            return
        index = order.index(current) if current in order else (-1 if delta > 0 else 0)
        for _attempt in range(len(order)):
            index = (index + delta) % len(order)
            name = order[index]
            if name in self.unloadable_names or name == current:
                continue
            self._retarget(name)
            if self.target.profile == name:
                self.chosen = True
                return
        self._say(cli_text.CARD_ONE_PROFILE)

    def _retarget_default(self) -> None:
        """The profile a launch here uses when none is named, with its notice."""

        from claude_multi.setup import defaults

        choice = defaults.resolve_default(self.runtime)
        try:
            document = self.runtime.profiles.load(choice.name)
        except profile_mod.ProfileError as exc:
            self.target = cli_types.LaunchTarget("unloadable", None, choice.name, False, f"Profile {choice.name}")
            self.load_error = exc
        else:
            self.target = cli_types.LaunchTarget("profile", document, choice.name, True, f"Profile {choice.name}")
            self.load_error = None
        self.notice_key = (self.target.kind, self.target.profile)
        self.target_notices = (choice.notice,) if choice.notice else ()
        self._refresh_order(choice.name)
        self._replan()

    def _refresh_target(self, name: str | None, *, selected: bool = False) -> None:
        """After Profiles or Get started: the card's profile again (it may
        have been edited or renamed), or the default when it is gone.

        ``selected``: the person chose ``name`` there (Enter, ``use``): the
        card moves to that stored profile whatever it showed before (an
        imported document or a direct lineup included); otherwise an ad-hoc
        target stays as it is."""

        if not selected and self.target.kind not in ("profile", "unloadable"):
            self._replan()
            return
        store = self.runtime.profiles
        try:
            exists = name is not None and store.contains(name)
        except profile_mod.ProfileError:
            exists = False
        if not exists or name is None:
            self._retarget_default()
            return
        try:
            document = store.load(name)
        except profile_mod.ProfileError as exc:
            # Still not loadable: the card stays BLOCKED on it, with its error now.
            self.target = cli_types.LaunchTarget("unloadable", None, name, False, f"Profile {name}")
            self.load_error = exc
        else:
            self.target = cli_types.LaunchTarget("profile", document, name, True, f"Profile {name}")
            self.load_error = None
        self._refresh_order(name)
        self._replan()

    def _setup_blocked(self) -> str | None:
        """Why Get started cannot open from this card (None: it can)."""

        import claude_multi.cli.consent as consent

        if consent.session_marker(self.runtime.environ) is not None:
            return cli_text.GS_IN_SESSION
        if not self.resume and not self.runtime.allow_state_writes:
            return cli_text.GS_READONLY
        return None

    def _setup_focus(self) -> str | None:
        """The Get started step this card opens on its own, or None."""

        from claude_multi.setup import status

        try:
            focus = status.should_open(self.runtime, explicit=False, resume=self.resume)
        except (cli_errors.ClaudeMultiError, OSError, ValueError):
            return None
        if focus == "claude" and not self.runtime.pin_report:
            # An injected binary report describes the copy: the card shows
            # no copy state then, so only the providers step can open.
            focus = "providers" if self.not_connected else None
        return focus

    def _observe_setup(self) -> None:
        """Whether nothing is connected yet (one gateway observation)."""

        from claude_multi.setup import status

        if self.resume:
            self.not_connected = False
            return
        try:
            self.not_connected = not status.connected(self.runtime)
        except (cli_errors.ClaudeMultiError, OSError, ValueError, KeyError):
            self.not_connected = False

    def _get_started(self, win: Any, *, focus: str | None = None) -> None:
        """W (and the automatic open): Get started, then the card again,
        on the profile it chose, the default, or the person's own choice."""

        import claude_multi.cli.screens.get_started as screens_get_started

        refusal = self._setup_blocked()
        if refusal is not None:
            self._say(refusal, "warn")
            return
        result = screens_get_started.run_get_started(
            self.runtime, win, self.palette, focus=focus, resume=self.resume,
            open_profiles=None if self.resume else self._profiles_for_get_started,
        )
        if self.resume:
            self._replan()
            return
        self._observe_setup()
        if result.kind == "use" and result.profile:
            self.chosen = True
            self._refresh_target(result.profile, selected=True)
            if self.target.profile == result.profile:
                self._say(cli_text.PROFILES_USED.format(name=result.profile))
        elif self.chosen:
            self._refresh_target(self.target.profile)
        else:
            self._retarget_default()

    def _profiles_for_get_started(self, win: Any) -> str | None:
        """Profiles opened from Get started: the profile chosen with Enter."""

        import claude_multi.cli.screens.profiles as screens_profiles

        result = screens_profiles.run_profiles_screen(
            self.runtime, win, self.palette, selected=self.target.profile, card_profile=self._card_profile(),
            now=self.now)
        self._follow_profile_changes(result)
        return result.name if result.kind == "use" else None

    def _card_profile(self) -> str | None:
        return self.target.profile if self.target.kind == "profile" else None

    def _follow_profile_changes(self, result: Any) -> None:
        """Keep the card on its profile through a rename; a removed one
        gives way to the default."""

        current = self.target.profile
        for old, new in result.renamed:
            if current == old:
                current = new
        if current in result.removed:
            current = None
        self._refresh_target(current)

    def _open_profiles(self, win: Any, *, selected: str | None = None) -> None:
        """P (and Enter on a profile that cannot be loaded): Profiles."""

        import claude_multi.cli.screens.profiles as screens_profiles

        result = screens_profiles.run_profiles_screen(
            self.runtime, win, self.palette, selected=selected or self.target.profile,
            card_profile=self._card_profile(), now=self.now)
        self._observe_setup()
        self._follow_profile_changes(result)
        if result.kind == "use" and result.name:
            self.chosen = True
            self._refresh_target(result.name, selected=True)
            if self.target.profile == result.name:
                self._say(cli_text.PROFILES_USED.format(name=result.name))
        elif result.kind == "get-started":
            self._get_started(win)

    def _first_run(self, win: Any) -> None:
        """H while nothing is connected: the first-run checks, and the way
        to Get started."""

        from claude_multi.setup import firstrun

        checks = firstrun.checks(self.runtime)
        lines = [*firstrun.render_lines(checks, surface="tui"), "", *firstrun.info_lines(self.runtime), "",
                 cli_text.FIRSTRUN_CARD_FOOTER]
        tui.TextView(cli_text.CHECK_STEP_TITLE, lines, palette=self.palette).run(win)

    def _edit(self, win: Any, *, focus_errors: bool = False) -> None:
        """E: the profile editor (§4.2) on the card's profile; a save re-targets the card."""

        if self.resume:
            self._say(cli_text.CARD_RESUME_INERT)
            return
        runtime = self.runtime
        if self.target.kind == "unloadable":
            # The file cannot be loaded: Profiles edits it as it is stored.
            self._open_profiles(win, selected=self.target.profile)
            return
        try:
            if self.target.profile is not None:
                editor_state = screens_profile_editor._profile_editor_state(runtime, self.target.profile, "edit")
                focus = None
            else:
                document = copy.deepcopy(dict(self.target.document or {}))
                document.pop("seed", None)
                editor_state = tui.ProfileEditorState(
                    document,
                    cat=runtime.lineup_catalog(),
                    bindings=selection._editor_bindings(runtime),
                    effective=runtime.current_effective(),
                    origin=None,
                    is_seed=False,
                )
                focus = "general"
        except (cli_errors.CLIError, profile_mod.ProfileError, settings_mod.SettingsError) as exc:
            self._say(termtext.visible_message(exc), "warn")
            return
        if focus_errors and self.eval_errors:
            # Enter on an evaluation-BLOCKED card: the first error's row wins
            # over the E-key default (the name field of an ad-hoc target).
            focus = editor_state.field_row(self.eval_errors[0])
        screen = tui.ProfileEditorScreen(
            editor_state,
            palette=self.palette,
            callbacks=screens_profile_editor._profile_editor_callbacks(runtime, self.palette, editor_state=editor_state),
            environ=runtime.environ,
            initial_focus=focus,
        )
        screen.run(win)
        saved = editor_state.saved
        if saved is not None:
            self.chosen = True
            self._refresh_order(saved.name)
            self._retarget(saved.name)
        else:
            self._replan()

    def _open_direct(self, win: Any) -> tuple | None:
        return screens_direct.run_direct_screen(
            self.runtime, win, self.palette, passthrough=self.passthrough, journal=self.journal
        )

    def _open_providers(self, win: Any) -> None:
        screens_providers.run_providers_screen(self.runtime, win, self.palette, journal=self.journal)
        self._observe_setup()
        self._replan()

    def _open_models(self, win: Any) -> None:
        screens_models.run_models_screen(self.runtime, win, self.palette, profile_hint=self.target.profile, now=self.now)
        self._observe_setup()
        self._replan()

    def _open_settings(self, win: Any) -> None:
        screens_settings.run_settings_screen(
            self.runtime, win, self.palette, card_lineup=self.lineup,
            tty_in=self.tty_in, tty_out=self.tty_out,
        )
        self._observe_setup()
        self._replan()

    def _open_sessions(self, win: Any) -> tuple | None:
        """S: the sessions screen (§4.7) with this card's ``passthrough``.

        Returns its ``("perform", prepared, decision)`` or None; on None the
        card re-plans (a forget, rename or lineup change may have moved the
        MRU order or the records).
        """

        result = _SessionsScreen(
            self.runtime,
            palette=self.palette,
            passthrough=self.passthrough,
            resume_decision=self.resume_decision,
            now=self.now,
            tty_in=self.tty_in,
            tty_out=self.tty_out,
            # Esc leads back to this card: a fresh one, or a resume card and its way.
            way_back=self.way_back.deeper() if self.way_back is not None else _WayBack(1, card=True),
        ).run(win)
        if result is None:
            if not self.resume:
                self._refresh_order(self.target.profile)
            self._replan()
        return result

    def _help(self, win: Any) -> None:
        """?: ``QUICK_HELP`` then the workflow guarantee panel (§4.1.2).

        The lines are wrapped (never clipped) into a scrolling Modal, so the
        ``/model`` rule and the whole panel stay readable at 80x24.
        """

        wrap = max(20, min(100, win.getmaxyx()[1] - 8))
        workflows = (
            self.lineup.workflows
            if self.lineup is not None
            else str((self.target.document or {}).get("workflows", "native"))
        )
        if self.way_back is not None:
            how = cli_text.RESUME_SIGN_IN_HELP_BACK if self.way_back.card else cli_text.RESUME_SIGN_IN_HELP_REOPEN
            quick = cli_text.RESUME_QUICK_HELP.replace("{sign_in}", how.format(route=self.sign_in_route))
        else:
            quick = cli_text.QUICK_HELP
        text = quick + "\n" + workflow_guarantee_panel(workflows)
        screens_common._ScrollModal(cli_text.CARD_HELP_TITLE, screens_common._wrap_help_lines(text, wrap)).run(
            win, self.palette, background=self._draw
        )

    def _secret_modal(self, win: Any) -> None:
        wrap = max(20, min(60, win.getmaxyx()[1] - 8))
        problems = self.prepared.secret_problems if self.prepared is not None else ()
        lines = [piece for problem in problems for piece in textwrap.wrap(problem, wrap)]
        hint = (cli_text.CARD_SECRET_HINT_RESUME.format(way=self.way_back.text()) if self.way_back is not None
                else cli_text.CARD_SECRET_HINT)
        lines += textwrap.wrap(hint, wrap)
        tui.Modal(cli_text.CARD_SECRET_TITLE, lines, buttons=(("Close", True),)).run(
            win, self.palette, background=self._draw
        )

    def _enter(self, win: Any) -> tuple | None:
        card = self.card
        if not self.resume and self.target.kind == "unloadable":
            # Profiles, with the file that cannot be loaded selected.
            self._open_profiles(win, selected=self.target.profile)
            return None
        if card.ready and self.prepared is not None:
            if self.resume:
                return ("perform", self.prepared, self.resume_decision)
            return ("perform", self.prepared)
        if not self.resume and self.eval_errors:
            self._edit(win, focus_errors=True)
            return None
        secret_only = (
            self.prepared is not None
            and bool(self.prepared.secret_problems)
            and tuple(card.errors) == tuple(self.prepared.secret_problems)
        )
        if secret_only:
            self._secret_modal(win)
        return None

    def run(self, win: Any) -> tuple | None:
        """``("perform", prepared[, decision])`` or None (Esc)."""

        tui.hide_cursor()
        if not self.gateway_checked:
            # One loopback check and one update hint per card open (§4.1).
            self.update_hint = self.hint_detector(self.runtime.catalog.docs["native-contract"])
            self.gateway_problem = self._gateway_status()
            self.gateway_checked = True
            self._refresh_pool()
            self._replan()
        if self.auto_setup:
            # Decided once: Claude Code not set up, or nothing connected yet.
            self.auto_setup = False
            focus = self._setup_focus()
            if focus is not None:
                self._get_started(win, focus=focus)
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
            ch = key.ch.lower() if key.kind == "char" and key.ch else ""
            fresh_only = ("e", "p", "d")
            if key.kind == "enter":
                outcome = self._enter(win)
                if outcome is not None:
                    return outcome
            elif ch == "?":
                self._help(win)
            elif ch == "v":
                self._details(win)
            elif ch == "h":
                if self.not_connected and not self.resume:
                    self._first_run(win)
                else:
                    self._run_health(win)
                self._observe_setup()
                self._replan()
            elif ch == "w":
                self._get_started(win)
            elif ch == "u" and self.update_hint is not None:
                self._run_update(win)
                self._replan()
            elif ch == "s":
                outcome = self._open_sessions(win)
                if outcome is not None:
                    return outcome
            elif self.resume and (ch in fresh_only or ch in ("g", "m", "o")
                                  or key.kind in ("tab", "btab", "left", "right")):
                # §4.1.2: a resume card keys Enter/U/S/H/?/Esc only.
                continue
            elif ch == "e":
                self._edit(win)
            elif key.kind in ("tab", "right"):
                self._cycle(1)
            elif key.kind in ("btab", "left"):
                self._cycle(-1)
            elif ch == "p":
                self._open_profiles(win)
            elif ch == "d":
                outcome = self._open_direct(win)
                if outcome is not None:
                    return outcome
                self._observe_setup()
                self._replan()
            elif ch == "g":
                self._open_providers(win)
            elif ch == "m":
                self._open_models(win)
            elif ch == "o":
                self._open_settings(win)


def launch_card(
    runtime: runtime_mod.Runtime,
    target: cli_types.LaunchTarget,
    *,
    action: str,
    session_id: str | None = None,
    prepared: cli_types.PreparedLaunch | None = None,
    resume_decision: str | None = None,
    passthrough: Sequence[str],
    input_stream: TextIO,
    output_stream: TextIO,
    no_color: bool,
    explicit: bool = True,
) -> tuple | None:
    """Run the launch card in its own curses session.

    ``explicit``: the person named the profile; a fresh card for a profile
    chosen by default may open Get started on its own.

    Returns the screen's ``("perform", prepared[, decision])`` after curses
    has been torn down, or None (Esc).  The caller performs, never inside
    curses.  ``tui.CursesError``/``OSError`` raised before curses started
    propagate unchanged (the caller falls back to the line confirm); raised
    after it, they are reported as a ``CLIError`` (never fallen back from:
    the session may already have written through a store API).
    ``KeyboardInterrupt`` propagates.
    """

    palette = tui.detect_palette(
        runtime.environ, no_color=no_color, tty_in=input_stream, tty_out=output_stream
    )
    screen = _LaunchCardScreen(
        runtime,
        target,
        action=action,
        session_id=session_id,
        prepared=prepared,
        passthrough=passthrough,
        palette=palette,
        tty_in=input_stream,
        tty_out=output_stream,
        resume_decision=resume_decision,
        explicit=explicit,
        # Get started opens on its own only for a profile chosen by default.
        auto_setup=action == "fresh" and target.kind == "profile" and not explicit,
    )
    started = False

    def app(win: Any) -> tuple | None:
        nonlocal started
        started = True
        return screen.run(win)

    try:
        return tui.run_curses_on_streams(app, input_stream, output_stream, palette=palette)
    except (tui.CursesError, OSError) as exc:
        if not started:
            raise
        raise cli_errors.CLIError(f"the launch card stopped: {exc}") from exc


def _run_resume_gate_modal(
    runtime: runtime_mod.Runtime,
    record: dict[str, Any],
    gate: resume_checks.ResumeGate,
    win: Any,
    palette: Any,
    *,
    background: Any = None,
) -> tuple[str, dict[str, Any], str | None] | None:
    """Present a non-ok resume gate as a Modal and resolve the choice.

    Returns ("resume", record, decision) when the operator resolved the
    gate (decision is "force" only for the daemon-owned bypass), or None
    when cancelled/failed (the caller stays on its screen). Stop and
    repair go through the existing store/stop machinery — never manual
    record edits.
    """

    buttons = [(label, value) for value, label in gate.actions]
    buttons.append(("Cancel", None))
    # Raw lines are stored unwrapped (text mode joins them verbatim);
    # wrap only for modal presentation.
    lines = [
        wrapped for line in gate.lines for wrapped in textwrap.wrap(line, width=60)
    ]
    choice = tui.Modal(
        gate.title, lines, buttons=tuple(buttons)
    ).run(win, palette, background=background)
    if choice is None:
        return None
    stable_id = sessions.managed_id(record)
    runtime_id = sessions.runtime_session_id(record)
    if choice == "repair-resume":
        runtime.session_store.relink_runtime(
            stable_id, observed_runtime_id=runtime_id
        )
        return ("resume", runtime.session_store.load(stable_id), None)
    if choice == "stop-resume":
        refusal = session_actions._stop_precheck(runtime, record)
        if refusal is not None:
            raise cli_errors.CLIError(refusal)
        error = session_actions._stop_runtime(runtime, runtime_id, record=record)
        if error is not None:
            raise cli_errors.CLIError(f"stop failed: {error}")
        # Rescan before granting the bypass: if the target still shows
        # live, ask for a retry instead of forcing into a fork (review).
        if any(
            runtime_id.startswith(prefix)
            for prefix in session_facts._live_background_prefixes()
        ):
            raise cli_errors.CLIError(
                "stop issued but the session still shows live — give it a "
                "moment and retry"
            )
        # No force exemption: the mandatory gate re-evaluates fresh at the
        # launch boundary — a stop that raced a relaunch is caught there
        # instead of being bypassed by a stale decision.
        return ("resume", record, None)
    if choice == "force":
        return ("resume", record, "force")
    if choice in ("relink", "relink-choose"):
        return _relink_and_recheck(runtime, record, gate, choice, win, palette, background=background)
    return None


def _relink_and_recheck(
    runtime: runtime_mod.Runtime,
    record: dict[str, Any],
    gate: resume_checks.ResumeGate,
    choice: str,
    win: Any,
    palette: Any,
    *,
    background: Any = None,
) -> tuple[str, dict[str, Any], str | None] | None:
    """The moved-directory gates' Relink: point the record at the known
    directory (or one the person names, prefilled with the current one)
    through the store's relink, then evaluate the gate again. When the
    transcript still sits under the old directory, show the exact quoted
    command that moves it and stop — claude-multi never moves, reads or
    deletes a transcript."""

    stable_id = sessions.managed_id(record)
    short = f"{stable_id[:8]}…"
    runtime_id = sessions.runtime_session_id(record)
    target = gate.relink
    if choice == "relink-choose":
        field_ = tui.TextInput(str(runtime.cwd), max_length=4096)
        modal = tui.Modal(cli_text.RELINK_INPUT_TITLE.format(short=short), [],
                          buttons=(("Relink", True), ("Cancel", False)), input=field_)
        if not modal.run(win, palette, background=background):
            return None
        target = field_.value.strip()
    if not target or not Path(target).is_dir():
        raise cli_errors.CLIError(cli_text.RELINK_NOT_DIR.format(path=target or "(empty)"))
    target = str(Path(target).absolute())
    runtime.session_store.relink_runtime(stable_id, observed_runtime_id=runtime_id, cwd=target)
    record = runtime.session_store.load(stable_id)
    again = resume_checks._evaluate_resume_gate(runtime, record)
    if again.kind == "ok":
        return ("resume", record, None)
    command = _transcript_move_command(runtime, record)
    if command is not None:
        tui.TextView(cli_text.RELINK_MOVE_TITLE,
                     cli_text.RELINK_MOVE_BODY.format(cwd=target, command=command).splitlines(),
                     palette=palette).run(win)
        return None
    lines = [wrapped for line in again.lines for wrapped in textwrap.wrap(line, width=60)]
    tui.Modal(again.title, lines, buttons=(("Close", None),)).run(win, palette, background=background)
    return None


def _transcript_move_command(runtime: runtime_mod.Runtime, record: Mapping[str, Any]) -> str | None:
    """The quoted shell command that moves the session's transcript from the
    one project directory it is filed under to the recorded directory's;
    None unless exactly one source is known. Never run here."""

    runtime_id = sessions.runtime_session_id(record)
    slugs = session_facts._slugs_for_session(runtime, runtime_id)
    if len(slugs) != 1:
        return None
    projects = product_paths.native_projects(Path(runtime.environ.get("HOME") or Path.home()), runtime.environ)
    source = projects / slugs[0] / f"{runtime_id}.jsonl"
    destination_dir = projects / session_facts._native_project_slug(str(record["cwd"]))
    if source.parent == destination_dir:
        return None
    destination = destination_dir / source.name
    return (f"mkdir -p {shlex.quote(str(destination_dir))}\n"
            f"mv -n {shlex.quote(str(source))} {shlex.quote(str(destination))}")
