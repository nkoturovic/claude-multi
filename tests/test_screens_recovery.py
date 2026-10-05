"""The sessions screen's details and recovery actions.

V shows a session's identity, directory, runtime id, applied lineup, the
pending change and why it waits, drift, routing and the observed usage
(unavailable, never zero, when the gateway log cannot be read). M marks an
exited session ended and P repairs its scope through the existing session
and doctor operations; every confirmation focuses Cancel first. When the
background liveness cannot be observed, forget and stop need the full typed
session id, and the under-lock guards still refuse the current session.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import _tui_fixture as fx
from claude_multi import cli, lineup, sessions, views
from claude_multi.cli import session_actions
import claude_multi.cli.text as cli_text
from test_screens_sessions import ENTER, ESC, _SessionsCase, _flat
from test_tui import DOWN, RIGHT

TAB = "\t"


class SessionsDetailsTests(_SessionsCase):
    def test_v_shows_identity_lineup_pending_and_unavailable_usage(self) -> None:
        record = self.record(pending=True)
        from test_tui import END

        result, win = self.run_screen(self.screen(), ["v", END, ESC, ESC], height=40, width=120)
        self.assertIsNone(result)
        text = _flat("\n".join(win.frames))
        self.assertIn(cli_text.SESSIONS_DETAILS_TITLE, text)
        self.assertIn(f"session {fx.FIXED_ID}", text)
        self.assertIn(f"runtime id: {sessions.runtime_session_id(record)}", text)
        self.assertIn(f"directory: {record['cwd']}", text)
        self.assertIn("lineup: gen", text)
        self.assertIn("pending: profile", text)
        self.assertIn(f"waits because {lineup.REASON_FORCED}", text)
        self.assertIn("settings drift: none", text)
        self.assertIn(views.SESSION_USAGE_UNAVAILABLE[:40], text)
        self.assertNotIn("Observed requests: 0", text)

    def test_the_details_builder_marks_missing_sources_unavailable(self) -> None:
        record = self.record()
        summary = sessions.record_summary(record, self.lcat)
        lines = views.session_details(summary, record, state="ended", lead="x", explain=None, usage=None)
        self.assertIn("routing: unavailable for this record", lines)
        self.assertIn(views.SESSION_USAGE_UNAVAILABLE, lines)
        self.assertIn("state: ○ ended", lines)
        self.assertIn("pending: none", lines)

    def test_v_on_a_legacy_record_says_how_to_migrate(self) -> None:
        self.v3_record()
        _result, win = self.run_screen(self.screen(), ["v", ESC, ESC], height=40, width=120)
        self.assertIn("legacy record: the next resume (Enter) migrates it", _flat("\n".join(win.frames)))


class SessionsMarkEndedTests(_SessionsCase):
    def test_m_marks_an_exited_session_ended(self) -> None:
        self.record()
        _result, win = self.run_screen(self.screen(), ["m", RIGHT, ENTER, ESC], width=120)
        self.assertTrue(any("Mark 11111111…  ended?".replace("  ", " ") in f for f in win.frames))
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["last_event_source"], "end")
        self.assertIn("11111111…: marked ended", win.frames[-1])

    def test_enter_cancels_by_default(self) -> None:
        record = self.record()
        _result, win = self.run_screen(self.screen(), ["m", ENTER, ESC])
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["last_event_source"],
                         record["last_event_source"])
        self.assertIn("nothing changed", win.frames[-1])

    def test_a_running_session_is_not_marked(self) -> None:
        record = self.record()
        self.runtime.live_prefixes = frozenset({sessions.runtime_session_id(record)[:8]})
        _result, win = self.run_screen(self.screen(), ["m", ESC], width=120)
        self.assertFalse(any("Mark 11111111" in f for f in win.frames))
        self.assertIn("is running — E stops it", win.frames[-1])

    def test_an_ended_session_has_nothing_to_mark(self) -> None:
        self.record(ended=True)
        screen = self.screen()
        self.assertNotIn(cli_text.SESSIONS_MARK_BINDING, screen._row_actions())
        _result, win = self.run_screen(screen, ["m", ESC], width=120)
        self.assertIn("already has an end event", win.frames[-1])


class SessionsRepairTests(_SessionsCase):
    def test_p_repairs_through_the_doctor_operation(self) -> None:
        self.record()
        with mock.patch("claude_multi.cli.doctor_actions._doctor_repair",
                        side_effect=lambda runtime, mid, out: (out.write(f"repaired {mid[:8]}\n"), 0)[1]) as repair:
            _result, win = self.run_screen(self.screen(), ["p", RIGHT, ENTER, ESC, ESC], width=120)
        repair.assert_called_once()
        self.assertEqual(repair.call_args.args[1], fx.FIXED_ID)
        self.assertTrue(any("repaired 11111111" in f for f in win.frames))
        self.assertIn("repair of 11111111… done", win.frames[-1])

    def test_cancel_runs_nothing(self) -> None:
        self.record()
        with mock.patch("claude_multi.cli.doctor_actions._doctor_repair") as repair:
            self.run_screen(self.screen(), ["p", ENTER, ESC])
        repair.assert_not_called()

    def test_a_legacy_record_is_refused(self) -> None:
        self.v3_record()
        with mock.patch("claude_multi.cli.doctor_actions._doctor_repair") as repair:
            _result, win = self.run_screen(self.screen(), ["p", ESC], width=120)
        repair.assert_not_called()
        self.assertIn(cli_text.REPAIR_LEGACY, win.frames[-1])


class UnknownLivenessTests(_SessionsCase):
    """Background liveness that cannot be observed: forget and stop need the
    typed session id; a wrong id or Cancel changes nothing."""

    def unknown(self):
        return mock.patch.object(self.runtime, "background_liveness",
                                 return_value=sessions.BackgroundLiveness(False, frozenset(), "fixture: unreadable"))

    def test_forget_needs_the_typed_id(self) -> None:
        self.record()
        with self.unknown():
            # The typed id, → Forget, Enter; Esc closes the result, Esc leaves.
            _result, win = self.run_screen(self.screen(), ["x", *fx.FIXED_ID, ENTER, RIGHT, ENTER, ESC, ESC],
                                           width=120)
        text = _flat("\n".join(win.frames))
        self.assertIn("Forget 11111111… without a liveness check?", text)
        self.assertIn("fixture: unreadable", text)
        self.assertFalse(self.runtime.session_store.exists(fx.FIXED_ID))

    def test_a_wrong_id_forgets_nothing(self) -> None:
        self.record()
        with self.unknown():
            _result, win = self.run_screen(self.screen(), ["x", *"11111111", ENTER, RIGHT, ENTER, ESC], width=120)
        self.assertTrue(self.runtime.session_store.exists(fx.FIXED_ID))
        self.assertIn("does not match", win.frames[-1])

    def test_cancel_is_focused_after_the_field(self) -> None:
        self.record()
        with self.unknown():
            _result, win = self.run_screen(self.screen(), ["x", *fx.FIXED_ID, ENTER, ENTER, ESC], width=120)
        self.assertTrue(self.runtime.session_store.exists(fx.FIXED_ID))
        self.assertIn("Forget cancelled.", win.frames[-1])

    def test_the_current_session_is_never_forgotten(self) -> None:
        self.record()
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = fx.FIXED_ID
        with self.unknown():
            _result, win = self.run_screen(self.screen(), ["x", *fx.FIXED_ID, ENTER, RIGHT, ENTER, ESC], width=120)
        self.assertTrue(self.runtime.session_store.exists(fx.FIXED_ID))
        self.assertIn("refusing to forget the session you are running inside", win.frames[-1])

    def test_stop_needs_the_typed_id_and_sends_claude_stop(self) -> None:
        record = self.record()
        with self.unknown(), mock.patch.object(session_actions, "_stop_runtime", return_value=None) as stop:
            _result, win = self.run_screen(self.screen(), ["e", *fx.FIXED_ID, ENTER, RIGHT, ENTER, ESC],
                                           width=120)
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[1], sessions.runtime_session_id(record))
        self.assertTrue(any("Stop 11111111… without a liveness check?" in f for f in win.frames))

    def test_stop_cancel_sends_nothing(self) -> None:
        self.record()
        with self.unknown(), mock.patch.object(session_actions, "_stop_runtime", return_value=None) as stop:
            self.run_screen(self.screen(), ["e", ESC, ESC], width=120)
        stop.assert_not_called()


class LineupTargetsTests(_SessionsCase):
    """T opens on the session's own profile; keep the current lineup (pin
    semantics) and a fallback provider preview first and apply re-decided
    under the lock, refusing a stale preview."""

    def dialog(self, record=None) -> cli._LineupDialog:
        record = record or self.runtime.session_store.load(fx.FIXED_ID)
        return cli._LineupDialog(self.runtime, record, palette=cli_tui().MONO_PALETTE, liveness="unknown")

    def test_t_opens_on_the_current_profile(self) -> None:
        record = self.record()
        dialog = self.dialog()
        self.assertEqual(dialog.profile_name, record["profile"])
        self.assertEqual(dialog.diff, [])

    def to_keep(self, dialog) -> None:
        dialog.target = "keep"
        dialog._preview()

    def test_keep_previews_follow_off_and_the_discarded_change(self) -> None:
        record = self.record(pending=True)
        dialog = self.dialog()
        self.to_keep(dialog)
        text = "\n".join(text for _label, text, _role in dialog.model().lines)
        self.assertIn(lineup.DISCARD_PENDING, text)
        self.assertIn(f"follow: on (profile {record['profile']}) → off", text)
        self.assertIn("): dropped", text)
        self.assertIn(lineup.EFFECT_RECORD, text)
        self.assertIn("pending", self.runtime.session_store.load(fx.FIXED_ID))  # a preview writes nothing

    def test_keep_applies_pin_semantics(self) -> None:
        record = self.record(pending=True)
        dialog = self.dialog()
        self.to_keep(dialog)
        from test_tui import FakeWindow

        self.assertTrue(dialog._apply(FakeWindow([ENTER], height=30, width=100)))
        after = self.runtime.session_store.load(fx.FIXED_ID)
        self.assertNotIn("pending", after)
        self.assertFalse(after["follow"])
        self.assertEqual(after["lineup_generation"], record["lineup_generation"])
        self.assertEqual(after["applied_hash"], record["applied_hash"])

    def test_a_stale_preview_is_refused_and_refreshed(self) -> None:
        self.record(pending=True)
        dialog = self.dialog()
        self.to_keep(dialog)
        # Another writer drops the pending change after the preview.
        current = self.runtime.session_store.load(fx.FIXED_ID)
        current.pop("pending")
        self.runtime.session_store.save(current)
        from test_tui import FakeWindow

        self.assertFalse(dialog._apply(FakeWindow([], height=30, width=100)))
        self.assertEqual(dialog.message, cli_text.LINEUP_PREVIEW_STALE)
        self.assertTrue(self.runtime.session_store.load(fx.FIXED_ID)["follow"])

    def test_fallback_cycles_the_lineups_providers_and_previews(self) -> None:
        self.record()
        dialog = self.dialog()
        self.assertTrue(dialog.providers)
        dialog.target = "fallback"
        dialog._preview()
        first = dialog.provider_name
        text = "\n".join(text for _label, text, _role in dialog.model().lines)
        self.assertIn(f"fallback → [{first} ▾]", text)
        self.assertTrue(dialog.preview_lines or dialog.message)
        if len(dialog.providers) > 1:
            dialog._cycle(1)
            self.assertNotEqual(dialog.provider_name, first)

    def test_ended_fallback_preview_names_next_resume_and_enter_applies(self) -> None:
        from claude_multi.cli.screens import common
        from test_tui import FakeWindow

        for height, width in ((24, 80), (40, 120)):
            with self.subTest(size=f"{width}x{height}"):
                record = self.record(ended=True)
                dialog = self.dialog(record)
                dialog.liveness = "ended"
                dialog.target = "fallback"
                for provider in dialog.providers:
                    dialog.provider_name = provider
                    dialog._preview()
                    if dialog.shown and "Effect: LIVE" in dialog.shown.text:
                        break
                self.assertIsNotNone(dialog.shown)
                self.assertIn("Effect: LIVE", dialog.shown.text)
                # Only presentation changes; locked apply keeps its original preview.
                before = self.runtime.session_store.load(fx.FIXED_ID)
                text = "\n".join(text for _label, text, _role in dialog.model().lines)
                self.assertIn("scope changes now; used at the next resume", text)
                self.assertIn("Enter applies", text)
                self.assertNotIn("Effect: LIVE", text)
                self.assertNotIn(lineup.FALLBACK_EXISTING, text)
                self.assertNotIn("Apply it with /cm fallback", text)
                self.assertEqual(before, self.runtime.session_store.load(fx.FIXED_ID))
                win = FakeWindow([ENTER, *([DOWN] * 40), ENTER], height=height, width=width)
                with mock.patch.object(lineup, "apply_preview", wraps=lineup.apply_preview) as apply, \
                        mock.patch.object(common, "_ScrollModal", wraps=common._ScrollModal) as modal:
                    self.assertTrue(dialog.run(win))
                self.assertEqual(apply.call_args.args[1], dialog.shown)
                after = self.runtime.session_store.load(fx.FIXED_ID)
                self.assertFalse(after["follow"])
                self.assertNotIn("pending", after)
                self.assertGreaterEqual(after["lineup_generation"], before["lineup_generation"])
                report = _flat(" ".join(modal.call_args.args[1]))
                self.assertIn("scope changed now; used at the next resume", report)
                self.assertIn(f"Next: resume with claude-multi -r {fx.FIXED_ID}", report)
                frames = [frame for frame in win.frames
                          if cli_text.LINEUP_REPORT_TITLE.format(m8=fx.FIXED_ID[:8]) in frame]
                self.assertTrue(frames, "the post-apply result modal was not rendered")
                rendered = "\n".join(frames)
                self.assertIn("scope changed now; used at the next resume", rendered)
                self.assertIn("Next: resume with claude-multi -r", rendered)
                for forbidden in ("Effect: LIVE", "Existing agents", "fresh agents", "/reload-plugins"):
                    self.assertNotIn(forbidden, report)
                    self.assertNotIn(forbidden, rendered)

    def test_live_fallback_keeps_live_effect_but_names_the_tui_apply_key(self) -> None:
        self.record()
        dialog = self.dialog()
        dialog.liveness = "live"
        dialog.target = "fallback"
        for provider in dialog.providers:
            dialog.provider_name = provider
            dialog._preview()
            if dialog.shown and "Effect: LIVE" in dialog.shown.text:
                break
        self.assertIsNotNone(dialog.shown)
        text = "\n".join(text for _label, text, _role in dialog.model().lines)
        self.assertIn("Effect: LIVE", text)
        self.assertIn(lineup.FALLBACK_EXISTING, text)
        self.assertIn("Enter applies", text)
        self.assertNotIn("Apply it with /cm fallback", text)
        from claude_multi.cli.screens import common
        from test_tui import FakeWindow

        with mock.patch.object(common, "_ScrollModal", wraps=common._ScrollModal) as modal:
            self.assertTrue(dialog._apply(FakeWindow([ENTER], height=40, width=120)))
        report = _flat(" ".join(modal.call_args.args[1]))
        self.assertIn("Effect: LIVE", report)
        self.assertIn(lineup.FALLBACK_EXISTING, report)
        self.assertIn("/reload-plugins", report)
        self.assertNotIn("Next: resume with", report)

    def test_tab_to_direct_and_space_picks_the_lead(self) -> None:
        from claude_multi.cli.screens import direct as screens_direct
        from test_tui import FakeWindow

        record = self.record()
        lead = record["applied"]["lead"]
        dialog = self.dialog()
        with mock.patch.object(screens_direct, "run_direct_screen",
                               return_value=(lead["key"], lead["effort"])) as picker:
            self.assertIsNone(dialog.run(FakeWindow(["\t", "\t", " ", ESC], height=30, width=110)))
        picker.assert_called_once()
        self.assertEqual(picker.call_args.kwargs.get("purpose"), "lineup")
        self.assertEqual(dialog.target, "direct")
        self.assertEqual(dialog.direct_words, ["direct", f"{lead['key']}:{lead['effort']}"])
        self.assertEqual(dialog.request.verb, "direct")

    def test_tab_to_keep_and_enter_applies_pin_semantics(self) -> None:
        from test_tui import FakeWindow

        record = self.record(pending=True)
        dialog = self.dialog()
        win = FakeWindow(["\t", "\t", "\t", ENTER, ENTER], height=30, width=110)
        self.assertTrue(dialog.run(win))
        self.assertEqual(dialog.target, "keep")
        after = self.runtime.session_store.load(fx.FIXED_ID)
        self.assertNotIn("pending", after)
        self.assertFalse(after["follow"])
        self.assertEqual(after["applied_hash"], record["applied_hash"])

    def test_space_never_opens_the_direct_picker_on_keep_or_fallback(self) -> None:
        from claude_multi.cli.screens import direct as screens_direct
        from test_tui import FakeWindow

        self.record()
        with mock.patch.object(screens_direct, "run_direct_screen",
                               side_effect=AssertionError("the Direct picker opened")):
            dialog = self.dialog()
            # Tab to keep (profile → per-agent → direct → keep), Space, Esc.
            result = dialog.run(FakeWindow(["\t", "\t", "\t", " ", ESC], height=30, width=110))
            self.assertIsNone(result)
            self.assertEqual(dialog.target, "keep")
            self.assertEqual(dialog.message, cli_text.LINEUP_KEEP_NOTHING_TO_PICK)
            # On fallback, Space lists the lineup's providers; Enter on the
            # second one selects it (Esc leaves the dialog).
            dialog = self.dialog()
            providers = list(dialog.providers)
            self.assertTrue(providers)
            keys = ["\t"] * 4 + [" "] + ([DOWN] if len(providers) > 1 else []) + [ENTER, ESC]
            win = FakeWindow(keys, height=30, width=110)
            self.assertIsNone(dialog.run(win))
        self.assertEqual(dialog.target, "fallback")
        self.assertEqual(dialog.provider_name, providers[1 if len(providers) > 1 else 0])
        self.assertTrue(any(cli_text.LINEUP_PICK_PROVIDER in frame for frame in win.frames))


    def single_provider_seed(self) -> tuple[str, str]:
        """(seed, provider): a seed whose lead and agents all run on one
        provider, and that provider (derived from the fixture catalog)."""

        for name in sorted(self.runtime.catalog.seed_profiles):
            lineup_ = self._evaluate(name)
            if lineup_ is None:
                continue
            used = {lineup_.lead.binding.provider, *(a.binding.provider for a in lineup_.agents.values())}
            if len(used) == 1:
                return name, used.pop()
        raise AssertionError("the fixture has a seed on a single provider")

    def test_a_single_provider_lineup_falls_back_to_another_provider(self) -> None:
        from test_tui import FakeWindow

        seed, used = self.single_provider_seed()
        record = self.record(seed)
        dialog = self.dialog()
        # Every provider with a fallback profile is offered, not only the one
        # the lineup runs on; the dialog's preview is /cm fallback's own.
        recipes = {self.runtime.profiles.load(name).get("primary_provider") for name in self.runtime.profiles.names()}
        others = []
        for provider in sorted(provider for provider in recipes if isinstance(provider, str)):
            if provider == used:
                continue
            try:
                lineup.preview(self.runtime, record, lineup.parse_request(["fallback", provider]))
            except lineup.LineupRefusal:
                continue
            others.append(provider)
        self.assertTrue(others, "the fixture has another provider with a complete fallback profile")
        target = others[0]
        self.assertIn(used, dialog.providers)
        self.assertIn(target, dialog.providers)
        self.assertEqual(sorted(provider for provider in recipes if isinstance(provider, str)),
                         lineup.fallback_providers(self.runtime))
        # Tab to fallback, Space lists the providers, Down to the other one,
        # Enter chooses it, Enter applies the preview, Enter closes the report.
        keys = ["\t"] * 4 + [" "] + [DOWN] * dialog.providers.index(target) + [ENTER, ENTER, ENTER]
        win = FakeWindow(keys, height=30, width=110)
        self.assertTrue(dialog.run(win))
        self.assertEqual(dialog.target, "fallback")
        self.assertEqual(dialog.provider_name, target)
        self.assertTrue(any(f"fallback [{target} ▾]" in frame for frame in win.frames))
        self.assertTrue(any("Preview only; nothing changed. Enter applies." in _flat(frame) for frame in win.frames))
        after = self.runtime.session_store.load(fx.FIXED_ID)
        stated = (after.get("pending") or {}).get("document") or sessions.applied_document(after)
        self.assertEqual(stated["primary_provider"], target)
        self.assertFalse(after["follow"])

    def test_a_provider_without_a_fallback_profile_shows_the_cm_refusal(self) -> None:
        self.record()
        dialog = self.dialog()
        dialog.target = "fallback"
        dialog.provider_name = "no-such-provider"
        dialog._preview()
        self.assertIsNone(dialog.request)
        self.assertEqual(dialog.message,
                         lineup.FALLBACK_NONE.format(provider="no-such-provider").replace("\n", " "))


def cli_tui():
    from claude_multi import tui

    return tui


class RelinkGateTests(_SessionsCase):
    """The moved-directory gates offer Relink: the store's relink, then the
    gate again; a transcript still filed under the old directory gets the
    exact quoted move command, never a move."""

    def run_gate(self, record, keys):
        from claude_multi import tui
        from claude_multi.cli import resume_checks
        from claude_multi.cli.screens import launch_sessions
        from test_tui import FakeWindow

        gate = resume_checks._evaluate_resume_gate(self.runtime, record, live_prefixes=frozenset())
        win = FakeWindow(list(keys), height=30, width=110)
        return gate, launch_sessions._run_resume_gate_modal(self.runtime, record, gate, win, tui.MONO_PALETTE), win

    def test_a_transcript_in_another_existing_project_relinks_and_resumes(self) -> None:
        record = self.record()
        elsewhere = Path(self.runtime.cwd).parent / "elsewhere"
        elsewhere.mkdir()
        transcript = self.transcript(record)
        target = transcript.parent.parent / cli._native_project_slug(str(elsewhere)) / transcript.name
        target.parent.mkdir(parents=True)
        transcript.rename(target)
        gate, result, _win = self.run_gate(record, [ENTER])
        self.assertEqual(gate.kind, "transcript-elsewhere")
        self.assertEqual(gate.relink, str(elsewhere))
        self.assertEqual(result[0], "resume")
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["cwd"], str(elsewhere))
        self.assertTrue(target.is_file())

    def test_a_gone_directory_relinks_and_names_the_move_command(self) -> None:
        import shlex

        record = self.record()
        transcript = self.transcript(record)
        old_cwd = Path(self.runtime.cwd)
        moved = old_cwd.parent / "renamed project"
        old_cwd.rename(moved)
        self.runtime.cwd = str(moved)
        # Relink to… → the field (prefilled with this directory) → Relink → Close.
        gate, result, win = self.run_gate(record, [ENTER, ENTER, ENTER, ESC])
        self.assertEqual(gate.kind, "cwd-missing")
        self.assertIsNone(result)
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["cwd"], str(moved))
        text = _flat("\n".join(win.frames))
        self.assertIn(cli_text.RELINK_MOVE_TITLE, text)
        self.assertIn("mv -n ", text)
        from claude_multi.cli.screens import launch_sessions

        command = launch_sessions._transcript_move_command(self.runtime, self.runtime.session_store.load(fx.FIXED_ID))
        mkdir, move = command.splitlines()
        self.assertEqual(shlex.split(move)[:3], ["mv", "-n", str(transcript)])
        self.assertEqual(Path(shlex.split(move)[3]).parent, Path(shlex.split(mkdir)[2]))
        self.assertEqual(Path(shlex.split(move)[3]).parent.name, cli._native_project_slug(str(moved)))
        self.assertTrue(transcript.is_file(), "the transcript is never moved")

    def test_enter_on_a_session_whose_directory_moved_relinks_it_here(self) -> None:
        import shlex

        record = self.record()
        transcript = self.transcript(record)
        old_cwd = Path(self.runtime.cwd)
        moved = old_cwd.parent / "renamed project"
        old_cwd.rename(moved)
        self.runtime.cwd = str(moved)
        # C: all directories (the record names the old one) → Enter: resume →
        # Relink to… → the field (prefilled with this directory) → Relink →
        # the move command → Esc → Esc leaves the screen.
        result, win = self.run_screen(self.screen(), ["c", ENTER, ENTER, ENTER, ENTER, ESC, ESC],
                                      height=30, width=110)
        self.assertIsNone(result)
        text = _flat("\n".join(win.frames))
        self.assertIn(cli_text.RELINK_INPUT_TITLE.format(short=f"{fx.FIXED_ID[:8]}…"), text)
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["cwd"], str(moved))
        self.assertIn(cli_text.RELINK_MOVE_TITLE, text)
        from claude_multi.cli.screens import launch_sessions

        command = launch_sessions._transcript_move_command(self.runtime, self.runtime.session_store.load(fx.FIXED_ID))
        self.assertEqual(shlex.split(command.splitlines()[1])[:3], ["mv", "-n", str(transcript)])
        self.assertTrue(transcript.is_file(), "the transcript is never moved")
        self.assertEqual(self.runtime.launches, [])

    def file_under(self, transcript: Path, directory: Path, *, copy: bool = False) -> Path:
        """The transcript filed under ``directory``'s project slug (moved, or a copy)."""

        target = transcript.parent.parent / cli._native_project_slug(str(directory)) / transcript.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if copy:
            target.write_bytes(transcript.read_bytes())
        else:
            transcript.rename(target)
        return target

    def test_an_undecodable_transcript_gate_offers_the_directory_chooser(self) -> None:
        import shlex

        record = self.record()
        # Filed under a project directory that does not exist (any more): the
        # slug cannot be decoded to a directory.
        filed = self.file_under(self.transcript(record), Path(self.runtime.cwd).parent / "never existed")
        here = Path(self.runtime.cwd).parent / "here"
        here.mkdir()
        self.runtime.cwd = str(here)
        # Relink to… → the field (prefilled with this directory) → Relink →
        # the gate again: the one known source gives its move command → Esc.
        gate, result, win = self.run_gate(record, [ENTER, ENTER, ENTER, ESC])
        self.assertEqual(gate.kind, "transcript-elsewhere")
        self.assertIsNone(gate.relink)
        self.assertEqual(gate.actions, (("relink-choose", cli_text.RELINK_CHOOSE),))
        self.assertIsNone(result)
        text = _flat("\n".join(win.frames))
        self.assertIn(cli_text.RELINK_INPUT_TITLE.format(short=f"{fx.FIXED_ID[:8]}…"), text)
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["cwd"], str(here))
        self.assertIn(cli_text.RELINK_MOVE_TITLE, text)
        from claude_multi.cli.screens import launch_sessions

        command = launch_sessions._transcript_move_command(self.runtime, self.runtime.session_store.load(fx.FIXED_ID))
        self.assertEqual(shlex.split(command.splitlines()[1])[:3], ["mv", "-n", str(filed)])
        self.assertTrue(filed.is_file(), "the transcript is never moved")

    def test_an_ambiguous_transcript_relinks_but_names_no_move_command(self) -> None:
        record = self.record()
        transcript = self.transcript(record)
        first, second = (Path(self.runtime.cwd).parent / name for name in ("one", "two"))
        first.mkdir()
        second.mkdir()
        copies = [self.file_under(transcript, first, copy=True), self.file_under(transcript, second)]
        here = Path(self.runtime.cwd).parent / "here"
        here.mkdir()
        self.runtime.cwd = str(here)
        # Relink to… → the field → Relink → the gate again (two sources: no
        # command) → Close.
        gate, result, win = self.run_gate(record, [ENTER, ENTER, ENTER, ENTER])
        self.assertEqual(gate.kind, "transcript-elsewhere")
        self.assertEqual(gate.actions, (("relink-choose", cli_text.RELINK_CHOOSE),))
        self.assertIsNone(result)
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["cwd"], str(here))
        text = _flat("\n".join(win.frames))
        self.assertNotIn(cli_text.RELINK_MOVE_TITLE, text)
        self.assertNotIn("mv -n", text)
        self.assertIn("Transcript found in a different project", text)
        self.assertTrue(all(path.is_file() for path in copies))

    def test_enter_on_an_undecodable_transcript_relinks_it_here(self) -> None:
        record = self.record()
        filed = self.file_under(self.transcript(record), Path(self.runtime.cwd).parent / "never existed")
        here = Path(self.runtime.cwd).parent / "here"
        here.mkdir()
        self.runtime.cwd = str(here)
        # C: all directories → Enter: resume → Relink to… → the field → Relink
        # → the move command → Esc → Esc leaves the screen.
        result, win = self.run_screen(self.screen(), ["c", ENTER, ENTER, ENTER, ENTER, ESC, ESC],
                                      height=30, width=110)
        self.assertIsNone(result)
        text = _flat("\n".join(win.frames))
        self.assertIn(cli_text.RELINK_INPUT_TITLE.format(short=f"{fx.FIXED_ID[:8]}…"), text)
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["cwd"], str(here))
        self.assertIn(cli_text.RELINK_MOVE_TITLE, text)
        self.assertTrue(filed.is_file())
        self.assertEqual(self.runtime.launches, [])

    def test_a_relink_to_a_missing_directory_is_refused(self) -> None:
        from claude_multi import errors

        record = self.record()
        old_cwd = Path(self.runtime.cwd)
        old_cwd.rename(old_cwd.parent / "gone")
        self.runtime.cwd = str(old_cwd)
        with self.assertRaises(errors.CLIError):
            self.run_gate(record, [ENTER, ENTER, ENTER])
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["cwd"], str(old_cwd))


class NativePickerTests(_SessionsCase):
    def test_every_native_session_is_reachable(self) -> None:
        self.record()
        ids = [f"77777777-7777-4777-8{index:03d}-777777777777" for index in range(25)]
        self.native(ids)
        screen = self.screen()
        # L, End (the oldest row of 25), Esc: the picker lists all of them.
        from test_tui import END

        _result, win = self.run_screen(screen, ["l", END, ESC, ESC], height=24, width=100)
        oldest = sorted(screen.native, key=lambda item: item["mtime"])[0]["session_id"][:12]
        self.assertTrue(any(oldest in frame for frame in win.frames))


if __name__ == "__main__":
    unittest.main()
