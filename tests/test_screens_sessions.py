"""The 3.0 sessions screen, lineup dialog, chooser and entries.

Every test runs on a hermetic ``_tui_fixture.fixture_runtime`` inside
``hermetic(runtime)`` with doctor's clock at ``FIXED_NOW``: liveness comes
from the Runtime seams (``live_prefixes``/``proc_ids``), the kept direct
``_live_background_prefixes()`` callers are routed to the fixture, and no
test reads ``/proc``, the journal or the daemon socket directory.  Profiles,
keys, lead classes and providers derive from the loaded fixture catalog;
no fixture id is a literal here.  Screens are driven on
``test_tui.FakeWindow`` in ``MONO_PALETTE``; every script ends with Esc or a
returning key.  Records are ``_tui_fixture.v4_record`` (v4) or the
``test_migrate`` v3 builders written through ``_v3.save_v3``.
"""

from __future__ import annotations

import contextlib
import copy
import io
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from claude_multi import catalog, cli, custom, lineup, profile, scope, sessions, tui, views

import _tui_fixture as fx
import _v3
from test_tui import BTAB, DOWN, END, HOME, LEFT, RIGHT, UP, FakeWindow
import claude_multi.launch
import claude_multi.lineup
import claude_multi.tui
import claude_multi.cli.text as cli_text

SPEC = "the sessions screen contract"
ESC = "\x1b"
ENTER = "\n"
TAB = "\t"
SPACE = " "


def _flat(frame: str) -> str:
    """A frame's text with the Modal borders stripped and lines joined (wrapped text)."""

    return " ".join(line.strip(" |") for line in frame.splitlines())


class _SessionsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-screens-sessions-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runtime = self.make_runtime(self.tmp / "root")

    def make_runtime(self, root: Path, **kwargs) -> fx.ScreenRuntime:
        runtime = fx.fixture_runtime(root, **kwargs)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(fx.hermetic(runtime))
        stack.enter_context(mock.patch('claude_multi.cli.gateway_facts._doctor_now', lambda: fx.FIXED_NOW))
        runtime.profiles.install_seeds()
        return runtime

    # -- derivations --------------------------------------------------

    @property
    def lcat(self):
        return self.runtime.lineup_catalog()

    def default_lead(self) -> str:
        return self.lcat.resolve_key(self.runtime.profiles.load(catalog.DEFAULT_SEED)["lead"]["model"]).key

    def live_seed(self) -> str:
        """The first other seed whose relaunch fields and lead equal the default seed's."""

        default = self._evaluate(catalog.DEFAULT_SEED)
        for name in sorted(self.runtime.catalog.seed_profiles):
            if name == catalog.DEFAULT_SEED:
                continue
            other = self._evaluate(name)
            if other is None:
                continue
            if other.relaunch_fields() == default.relaunch_fields() and (
                other.lead.binding.key == default.lead.binding.key
            ):
                return name
        raise AssertionError(f"{SPEC} §7.1: the fixture has a LIVE seed pair")

    def relaunch_seed(self) -> str:
        """The first seed whose native agents differ from the default seed's."""

        default = self.runtime.profiles.load(catalog.DEFAULT_SEED)["native_agents"]
        name = next(
            (n for n in sorted(self.runtime.catalog.seed_profiles)
             if self.runtime.profiles.load(n).get("native_agents") != default),
            None,
        )
        if name is None:
            raise AssertionError(f"{SPEC} §7.1: the fixture has a RELAUNCH seed")
        return name

    def _evaluate(self, name: str):
        evaluation = profile.evaluate(
            self.runtime.profiles.load(name), self.lcat,
            bindings=self.runtime.bindings.bindings(), effective=self.runtime.current_effective(),
        )
        return evaluation.lineup

    def lead_classes(self) -> dict[str, list[str]]:
        """Lead class -> offered lead-capable catalog lines (catalog order)."""

        classes: dict[str, list[str]] = {}
        for key, entry in self.lcat.lines.items():
            if "family" in entry or entry.get("status", "active") == "new":
                continue
            if "lead" not in entry["capabilities"]:
                continue
            cls = entry["context"].get("ordinary_profile")
            if cls is not None:
                classes.setdefault(cls, []).append(key)
        return classes

    # -- builders --------------------------------------------------------------

    def record(self, name=catalog.DEFAULT_SEED, *, managed_id=fx.FIXED_ID, transcript=True, **kwargs) -> dict:
        record = fx.v4_record(self.runtime, name, managed_id=managed_id, **kwargs)
        if transcript:
            self.transcript(record)
        return record

    def direct_record(self, key: str, *, managed_id=fx.FIXED_ID, **kwargs) -> dict:
        return self.record(None, managed_id=managed_id, document=profile.ad_hoc_direct(key), **kwargs)

    def transcript(self, record) -> Path:
        path = (
            Path(self.runtime.environ["HOME"]) / ".claude" / "projects"
            / cli._native_project_slug(record["cwd"]) / f"{sessions.runtime_session_id(record)}.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()  # metadata-only fixture transcript for the resume gate
        return path

    def v3_record(self, *, ordinary: bool = False, managed_id=fx.OTHER_ID, **kwargs) -> dict:
        import test_migrate

        builder = test_migrate.v3_ordinary if ordinary else test_migrate.v3_managed
        record = builder(managed_id, cwd=str(self.runtime.cwd), **kwargs)
        sessions.ensure_state_v4(self.runtime.session_store.root)
        _v3.save_v3(self.runtime.session_store, record)
        return self.runtime.session_store.load(managed_id)

    def native(self, session_ids, *, cwd=None) -> None:
        slug = cli._native_project_slug(cwd or self.runtime.cwd)
        directory = Path(self.runtime.environ["HOME"]) / ".claude" / "projects" / slug
        directory.mkdir(parents=True, exist_ok=True)
        for session_id in session_ids:
            (directory / f"{session_id}.jsonl").touch()

    def screen(self, **kwargs) -> cli._SessionsScreen:
        kwargs.setdefault("now", fx.FIXED_NOW)
        kwargs.setdefault("tty_in", io.StringIO())
        kwargs.setdefault("tty_out", io.StringIO())
        return cli._SessionsScreen(self.runtime, palette=tui.MONO_PALETTE, **kwargs)

    @staticmethod
    def frame(screen, *, height=24, width=80) -> str:
        win = FakeWindow([], height=height, width=width)
        screen._draw(win)
        return win.text()

    @staticmethod
    def run_screen(screen, keys, *, height=24, width=80):
        win = FakeWindow(list(keys), height=height, width=width)
        return screen.run(win), win

    def healthy_card(self):
        """The resume card's one loopback check, fixture-quiet (no update hint by default)."""

        import contextlib

        return contextlib.nullcontext()


# ================================================================ rows


class SessionsRowTests(_SessionsCase):
    def test_columns_states_and_marks(self) -> None:
        self.record(managed_id=fx.FIXED_ID, title="api design")
        self.record(managed_id=fx.OTHER_ID, ended=True, pending=True, last_seen=timedelta(hours=3))
        needs = self.record(managed_id=fx.THIRD_ID, needs_choice=True, last_seen=timedelta(days=2))
        forked = self.record(managed_id=fx.FOURTH_ID, last_seen=timedelta(days=3))
        forked["pending_forks"] = [{"session_id": fx.FIFTH_ID, "observed_at": fx.iso(fx.FIXED_NOW)}]
        self.runtime.session_store.save(forked)
        self.runtime.live_prefixes = frozenset({fx.FIXED_ID[:8]})
        text = self.frame(self.screen(), width=96)
        lines = text.splitlines()
        self.assertIn(f"sessions — project (cwd) · 4 total", lines[1])
        header = next(line for line in lines if "state" in line and "title" in line)
        for column in ("state", "profile", "lead", "agents", "follow", "last seen", "title"):
            self.assertIn(column, header)
        row = {mid: next(line for line in lines if marker in line)
               for mid, marker in ((fx.FIXED_ID, "api design"), (fx.OTHER_ID, "↻ —"),
                                   (fx.THIRD_ID, "! needs a choice"), (fx.FOURTH_ID, "⚠ —"))}
        self.assertIn("● running", row[fx.FIXED_ID])
        self.assertIn("○ ended", row[fx.OTHER_ID])
        self.assertIn("◐ unknown", row[fx.THIRD_ID])
        self.assertIn(f"{needs['applied']['lead']['key']} !", row[fx.THIRD_ID])
        self.assertIn(views.short_label(self.lcat.lines[self.default_lead()]["display"]), row[fx.FIXED_ID])
        self.assertIn("follow", row[fx.FIXED_ID])
        self.assertIn(str(len(needs["applied"]["agents"])), row[fx.FIXED_ID])
        self.assertIn("2 min ago", row[fx.FIXED_ID])
        self.assertIn("3 h ago", row[fx.OTHER_ID])

    def test_proc_liveness_is_running_too(self) -> None:
        record = self.record()
        self.runtime.proc_ids = frozenset({sessions.runtime_session_id(record)})
        self.assertIn("● running", self.frame(self.screen()))

    def test_width_tiers(self) -> None:
        self.record(title="tiers")
        expected = {
            96: ("state", "profile", "lead", "agents", "follow", "last seen", "title"),
            80: ("state", "profile", "lead", "agents", "follow", "last seen", "title"),
            60: ("state", "profile", "lead", "last seen", "title"),
            44: ("state", "profile", "title"),
        }
        for width, columns in expected.items():
            with self.subTest(width=width):
                screen = self.screen()
                rows, _cols = screen.min_size(width)
                text = self.frame(screen, width=width, height=max(rows, 24))
                header = next(line for line in text.splitlines() if " state" in line)
                for column in ("agents", "follow", "last seen", "lead"):
                    self.assertEqual(column in header, column in columns, (width, column, header))
                for line in text.splitlines():
                    self.assertLessEqual(len(line), width - 1, line)
        self.assertIn("2 min ago", self.frame(self.screen(), width=96))
        compact = self.frame(self.screen(), width=80)
        self.assertIn("2 min", compact)
        self.assertNotIn("2 min ago", compact)

    def test_a_v3_row_with_a_retired_null_lead_shows_the_mark_and_never_raises(self) -> None:
        # The lead mark for every record version.
        key = fx.needs_choice_key(self.runtime)
        record = self.v3_record(lead={"model": key})
        summary = sessions.record_summary(record)
        self.assertEqual(summary.lead_key, key)
        text = self.frame(self.screen(), width=96)
        row = next(line for line in text.splitlines() if summary.profile_label in line)
        self.assertIn(f"{key} !", row)
        self.assertIn("! needs a choice", row)
        self.assertIn("1 session(s) need a profile choice", text)

    def test_v3_rows_show_the_2x_labels(self) -> None:
        managed = self.v3_record(managed_id=fx.OTHER_ID)
        ordinary = self.v3_record(ordinary=True, managed_id=fx.THIRD_ID)
        text = self.frame(self.screen(), width=96)
        for record in (managed, ordinary):
            label = sessions.record_summary(record).profile_label
            self.assertTrue(label.startswith(("cm:", "gateway:")), label)
            row = next(line for line in text.splitlines() if label in line)
            self.assertEqual(row.split()[-1], "—", "a 2.x record has no title")

    def test_banner_is_below_the_rows_and_reached_by_down_and_end(self) -> None:
        self.record(managed_id=fx.FIXED_ID)
        self.record(managed_id=fx.OTHER_ID, needs_choice=True, last_seen=timedelta(hours=1))
        screen = self.screen()
        lines = self.frame(screen).splitlines()
        banner = next(i for i, line in enumerate(lines) if "need a profile choice" in line)
        last_row = max(i for i, line in enumerate(lines) if "unknown" in line)
        self.assertEqual(banner, last_row + 1, "the banner sits directly below the last row")
        self.assertEqual(screen._items(), 3)
        # ↓ stops on the banner (the last selectable item); End jumps there too.
        screen.run(FakeWindow([END, ESC]))
        self.assertTrue(screen._on_banner())
        down = self.screen()
        down.run(FakeWindow([DOWN, DOWN, DOWN, ESC]))
        self.assertTrue(down._on_banner())
        self.assertIn(cli.SESSIONS_BANNER_ACTIONS, self.frame(down))
        down.run(FakeWindow([HOME, ESC]))
        self.assertEqual(down.selected, 0)

    def test_empty_and_the_filtered_empty_state(self) -> None:
        self.assertIn(cli.SESSIONS_EMPTY, self.frame(self.screen()))
        record = self.record(transcript=False)
        record["cwd"] = str(self.tmp)
        self.runtime.session_store.save(record)
        screen = self.screen()
        self.assertIn(cli.SESSIONS_EMPTY_FILTERED, self.frame(screen))

    def test_c_toggles_this_directory_and_all_directories(self) -> None:
        self.record(managed_id=fx.FIXED_ID)
        elsewhere = self.record(managed_id=fx.OTHER_ID, transcript=False)
        elsewhere["cwd"] = str(self.tmp)
        self.runtime.session_store.save(elsewhere)
        screen = self.screen()
        self.assertTrue(screen.cwd_filter)
        self.assertEqual([r["managed_id"] for r in screen.records], [fx.FIXED_ID])
        result, win = self.run_screen(screen, ["c", ESC])
        self.assertIsNone(result)
        self.assertFalse(screen.cwd_filter)
        self.assertEqual(len(screen.records), 2)
        self.assertIn("sessions — all directories · 2 total", win.frames[-1])
        self.assertIn("showing all directories", win.frames[-1])
        self.assertIn("C this dir", win.frames[-1])
        self.assertEqual((screen.selected, screen.offset), (0, 0))
        screen.selected, screen.offset = 1, 7
        screen.run(FakeWindow(["C", ESC]))
        self.assertEqual((screen.selected, screen.offset), (0, 0))
        self.assertIn("C all dirs", self.frame(screen))
        self.assertLessEqual(tui.KeyBar(cli.SESSIONS_KEYBAR).rows(80), 2)
        import claude_multi.cli.text as text
        self.assertLessEqual(tui.KeyBar(text.SESSIONS_KEYBAR_ALL).rows(80), 2)
        self.assertTrue(screen.cwd_filter)
        self.assertEqual(screen.message, "showing only this directory")

    def test_sort_is_last_used_and_the_cell_shows_the_sort_value(self) -> None:
        recent_use = self.record(managed_id=fx.FIXED_ID, last_seen=timedelta(hours=1))
        stale = self.record(managed_id=fx.OTHER_ID, last_seen=timedelta(days=3))
        stale["created_at"] = fx.iso(fx.FIXED_NOW - timedelta(minutes=30))  # created after its last_seen
        self.runtime.session_store.save(stale)
        screen = self.screen()
        self.assertEqual([r["managed_id"] for r in screen.records], [fx.OTHER_ID, fx.FIXED_ID])
        self.assertEqual(screen.summaries[0].last_seen_at, cli._record_last_seen(stale))
        self.assertEqual(screen.summaries[1].last_seen_at, recent_use["last_seen_at"])
        self.assertIn("30 min ago", self.frame(screen, width=96))

    def test_the_age_clock_seam(self) -> None:
        self.record(last_seen=timedelta(days=3))
        self.assertIn("3 days ago", self.frame(self.screen(), width=96))
        later = self.screen(now=fx.FIXED_NOW + timedelta(days=90))
        self.assertIn(fx.iso(fx.FIXED_NOW - timedelta(days=3))[:10], self.frame(later, width=96))

    def test_native_line_and_its_overflow(self) -> None:
        self.record()
        ids = [f"77777777-7777-4777-8{index:03d}-777777777777" for index in range(25)]
        self.native(ids[:20])
        text = self.frame(self.screen())
        self.assertIn("native (not managed): 20 — L links one", text)
        self.assertNotIn("more (newest 20 listed)", text)
        self.native(ids[20:])
        screen = self.screen()
        text = self.frame(screen, width=96)
        # Every eligible native session is listed; the picker scrolls.
        self.assertIn("native (not managed): 25 — L links one", text)
        self.assertNotIn("newest 20", text)
        self.assertEqual(len(screen.native), 25)

    def test_floor(self) -> None:
        self.record()
        screen = self.screen()
        rows, cols = screen.min_size(80)
        self.assertEqual(cols, cli.SESSIONS_MIN_COLS)
        small = self.frame(screen, height=rows - 1, width=80)
        self.assertIn(tui.TOO_SMALL.format(what=screen.what), small)
        fits = self.frame(screen, height=rows, width=80)
        self.assertIn("sessions — ", fits)
        self.assertIn("Esc", fits.splitlines()[-1] + fits.splitlines()[-2])
        narrow_rows, _cols = screen.min_size(cols)
        self.assertIn(tui.TOO_SMALL.format(what=screen.what)[: cols - 4],
                      self.frame(screen, height=narrow_rows, width=cols - 1))
        self.assertIn("sessions — ", self.frame(screen, height=narrow_rows, width=cols))
        self.assertIn(tui.TOO_SMALL.format(what=screen.what)[: cols - 4],
                      self.frame(screen, height=narrow_rows - 1, width=cols))
        # Below the floor only Esc acts.
        result, _win = self.run_screen(screen, ["x", ENTER, ESC], height=rows - 1)
        self.assertIsNone(result)
        self.assertTrue(self.runtime.session_store.exists(fx.FIXED_ID))

    def test_keybar_and_help(self) -> None:
        self.assertEqual(
            cli.SESSIONS_KEYBAR,
            (("Enter", "resume"), ("V", "details"), ("T", "lineup"), ("F", "follow/pin"), ("R", "rename"),
             ("X", "forget"), ("L", "link"), ("C", "all dirs"), ("?", "help"), ("Esc", "back")),
        )
        self.record()
        result, win = self.run_screen(self.screen(), ["?", END, ESC, ESC], height=30, width=120)
        self.assertIsNone(result)
        modal = _flat("\n".join(win.frames))
        self.assertIn(cli.SESSIONS_HELP_TITLE, modal)
        self.assertIn("Enter — resume the selected session", modal)
        self.assertIn("↻ relaunch change recorded", modal)
        # The selected row (no end event, not running, a v4 record) adds M and P.
        self.assertEqual(
            self.screen()._keybar().text(),
            "Enter resume · V details · T lineup · F follow/pin · R rename · X forget · M mark ended "
            "· P repair · L link · C all dirs · ? help · Esc back")
        self.assertIn("Enter resume · V details · T lineup", win.frames[0])

    def test_the_root_screen_quits_with_esc(self) -> None:
        self.record()
        screen = cli._SessionsScreen(self.runtime, palette=tui.MONO_PALETTE, now=fx.FIXED_NOW, root=True)
        self.assertEqual(screen._keybar().bindings[-1], ("Esc", "quit"))
        self.assertEqual(self.screen()._keybar().bindings[-1], ("Esc", "back"))


class SessionsActionsRowTests(_SessionsCase):
    def actions(self, record_or_screen=None) -> str:
        screen = self.screen()
        return screen._actions()

    def test_facts_head(self) -> None:
        record = self.record()
        self.assertEqual(
            self.actions(),
            f"lineup gen {record['lineup_generation']} · {len(record['applied']['agents'])} agents · follow",
        )

    def test_settings_drift_note(self) -> None:
        self.record()
        percent = self.runtime.current_effective().compaction_percent - 5
        self.runtime.settings_store.update(
            lambda document: document.__setitem__("compaction_percent", percent),
            catalog=self.runtime.catalog, custom_registry=custom.load_registry(self.runtime.environ),
        )
        text = self.actions()
        self.assertIn("settings changed since launch: ", text)
        self.assertIn("— applies at the next resume", text)

    def test_pending_note(self) -> None:
        self.record(pending=True)
        self.assertIn(f"pending relaunch: {lineup.REASON_FORCED}", self.actions())

    def test_requested_lead_note(self) -> None:
        record = self.record(follow=False)
        other = next(k for k in self.lead_classes()[self.lcat.lines[self.default_lead()]["context"]["ordinary_profile"]]
                     if k != self.default_lead())
        record["lead_target"] = {"key": other, "effort": "high", "selector": "sel"}
        self.runtime.session_store.save(record)
        self.assertIn(
            f"requested lead: {lineup.binding_label(self.lcat.lines[other], 'high', key=other)} — /model in the session",
            self.actions(),
        )

    def test_needs_a_choice_note_v4_and_v3(self) -> None:
        record = self.record(needs_choice=True)
        notice = sessions.lead_choice_notice(record, self.lcat)
        self.assertIn(f"needs a lead choice: {notice}", self.actions())
        self.runtime.session_store.forget_session(fx.FIXED_ID)
        key = fx.needs_choice_key(self.runtime)
        self.v3_record(lead={"model": key})
        self.assertIn(f"needs a lead choice: {key} has no live line", self.actions())

    def test_legacy_record_note(self) -> None:
        self.v3_record()
        self.assertTrue(self.actions().endswith("legacy record — the next resume migrates it"))

    def test_repair_needed_note(self) -> None:
        # Restores ResumeGateTests.test_picker_marker_shows_repair_glyph /
        # test_actions_label_marks_repair_needed_row on the 3.0 row.
        record = self.record()
        record["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        record["observed_cwd"] = "/wrong/project"
        self.runtime.session_store.save(record)
        self.assertIn("identity repair needed — Enter offers Repair & resume", self.actions())


class SessionsInjectionTests(_SessionsCase):
    def test_a_hostile_cwd_name_and_title_are_sanitized_in_the_table(self) -> None:
        # Restores TerminalInjectionTests.test_hostile_cwd_sanitized_in_sessions_tui_table.
        hostile_dir = Path(self.runtime.cwd).parent / "proj\x1b]8;;bad.example\x07link"
        hostile_dir.mkdir()
        self.runtime.cwd = str(hostile_dir)
        record = self.record(transcript=False)
        # A title never carries control bytes: the store refuses them.
        record["title"] = "t\x1b[2Jx"
        with self.assertRaises(sessions.SessionError):
            self.runtime.session_store.save(record)
        text = self.frame(self.screen())
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertIn("proj^[]8;;bad.example^Glink (cwd)", text)


# ================================================================ entries


class SessionsEntryTests(_SessionsCase):
    def curses_script(self, keys, *, width=80, height=24):
        """``run_curses_on_streams`` replaced by a FakeWindow run (the app returns before perform)."""

        calls = []

        def run(app, _inp, _out, *, palette=None):
            win = FakeWindow(list(keys), width=width, height=height)
            calls.append(win)
            return app(win)

        return mock.patch.object(claude_multi.tui, "run_curses_on_streams", side_effect=run), calls

    def main(self, argv, text=""):
        output = io.StringIO()
        with mock.patch.object(claude_multi.tui, "streams_curses_capable", return_value=True):
            code = cli.main(argv, runtime=self.runtime, input_stream=io.StringIO(text),
                            output_stream=output, interactive=True)
        return code, output.getvalue()

    def test_resume_intent_prepares_and_performs_after_teardown(self) -> None:
        self.record()
        patcher, calls = self.curses_script([ENTER])
        with patcher, self.healthy_card():
            code, output = self.main(["-r"])
        self.assertEqual(code, 0, output)
        self.assertEqual(len(calls), 1, "one curses session")
        self.assertEqual(len(self.runtime.launches), 1)
        prepared = self.runtime.launches[0]
        self.assertEqual(prepared.result.session_action.kind, "resume")
        self.assertEqual(prepared.record["managed_id"], fx.FIXED_ID)

    def test_bare_resume_on_a_capable_terminal_opens_the_screen(self) -> None:
        self.record()
        patcher, calls = self.curses_script([ESC])
        with patcher:
            code, output = self.main(["-r"])
        self.assertEqual(code, 0, output)
        self.assertIn("sessions — project (cwd) · 1 total", calls[0].frames[0])
        self.assertEqual(self.runtime.launches, [])

    def test_sessions_list_on_a_capable_terminal_is_a_listing(self) -> None:
        self.record()
        patcher, calls = self.curses_script([ESC])
        with patcher:
            code, output = self.main(["sessions", "list"])
        self.assertEqual(code, 0, output)
        self.assertEqual(calls, [])
        self.assertTrue(output.startswith("sessions\n"), output)

    def test_driver_threads_passthrough_into_the_resume_prepare(self) -> None:
        self.record()
        patcher, _calls = self.curses_script([ENTER])
        with patcher, self.healthy_card():
            code, output = self.main(["-r", "--", "--verbose"])
        self.assertEqual(code, 0, output)
        self.assertIn("--verbose", self.runtime.launches[0].result.argv)

    def test_resume_of_a_v3_ordinary_record_migrates_at_prepare(self) -> None:
        # A 2.x ordinary record resumes through prepare,
        # which migrates it; the resume card states the migration.
        record = self.v3_record(ordinary=True, managed_id=fx.FIXED_ID)
        self.transcript(record)
        screen = self.screen()
        with self.healthy_card(), mock.patch('claude_multi.cli.screens.launch_sessions._LaunchCardScreen._gateway_status', lambda _s: None):
            result, win = self.run_screen(screen, [ENTER, ENTER])
        self.assertEqual(result[0], "perform")
        prepared = result[1]
        self.assertEqual(prepared.record["version"], sessions.RECORD_VERSION)
        self.assertTrue(any(n.startswith("migrated legacy record") for n in prepared.notices))
        self.assertEqual(prepared.record["applied"]["agents"], {})
        self.assertEqual(self.runtime.launches, [])

    def test_resume_of_a_blocked_lineup_shows_the_message_and_never_launches(self) -> None:
        # The followed profile no longer evaluates; the resume never launches.
        name = "blocked-follow"
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = name
        self.runtime.profiles.save(document)
        self.record(name)
        writer = profile.WRITER_IDS[0]
        broken = self.runtime.profiles.load(name)
        broken["agents"][writer] = {"model": "no-such-line", "effort": "high"}
        self.runtime.profiles.save(broken)
        screen = self.screen()
        result, win = self.run_screen(screen, [ENTER, ESC], width=120)
        self.assertIsNone(result)
        self.assertIn("not resumed:", screen.message)
        self.assertEqual(self.runtime.launches, [])
        self.assertTrue(any("not resumed:" in frame for frame in win.frames))

    def test_line_flag_keeps_the_line_flows(self) -> None:
        self.record()
        with mock.patch('claude_multi.cli.screens.launch_sessions._sessions_list_tui') as screen:
            code, output = self.main(["--line", "-r"], "\n")
            self.assertEqual(code, 0, output)
            self.assertIn("resume which session (number, Enter cancels): ", output)
            code, output = self.main(["--line", "sessions", "list"], "1\n")
            self.assertEqual(code, 0, output)
            self.assertNotIn("resume which session", output)
            self.assertIn(fx.FIXED_ID[:8], output)
        screen.assert_not_called()
        self.assertEqual(self.runtime.launches, [])

    def test_resume_needing_a_choice_opens_the_chooser_on_a_capable_terminal(self) -> None:
        # -r <id> whose prepare raises NeedsChoiceError
        # (a pinned record: its source document is the applied lineup).
        self.record(needs_choice=True, follow=False)
        first = cli.profile_pick_order(self.runtime)[0]
        patcher, calls = self.curses_script([ENTER, ENTER])
        with patcher, self.healthy_card():
            code, output = self.main(["-r", fx.FIXED_ID])
        self.assertEqual(code, 0, output)
        self.assertIn("choose a lineup for 11111111", calls[0].frames[0])
        self.assertEqual(len(self.runtime.launches), 1)
        prepared = self.runtime.launches[0]
        self.assertEqual(prepared.target.kind, "relaunch")
        self.assertEqual(prepared.record["profile"], first)
        self.assertFalse(sessions.lead_needs_choice(prepared.record, self.lcat))

    def test_needs_choice_esc_launches_nothing(self) -> None:
        self.record(needs_choice=True, follow=False)
        patcher, _calls = self.curses_script([ESC])
        with patcher:
            code, output = self.main(["-r", fx.FIXED_ID])
        self.assertEqual(code, 0)
        self.assertIn("Nothing was launched.", output)
        self.assertEqual(self.runtime.launches, [])

    def test_curses_start_failure_falls_back_to_the_line_chooser(self) -> None:
        self.record()
        with mock.patch.object(claude_multi.tui, "run_curses_on_streams", side_effect=claude_multi.tui.CursesError("no tty")):
            code, output = self.main(["-r"], "\n")
        self.assertEqual(code, 0, output)
        self.assertIn("resume which session (number, Enter cancels): ", output)

    def test_esc_returns_zero_and_launches_nothing(self) -> None:
        self.record()
        patcher, _calls = self.curses_script([ESC])
        with patcher:
            code, _output = self.main(["-r"])
        self.assertEqual(code, 0)
        self.assertEqual(self.runtime.launches, [])


# ================================================================ Enter (resume gate)


class ResumeGateModalTests(_SessionsCase):
    def test_resume_without_a_diff_returns_the_perform_intent_directly(self) -> None:
        self.record()
        result, _win = self.run_screen(self.screen(), [ENTER])
        self.assertEqual(result[0], "perform")
        self.assertEqual(result[1].record["managed_id"], fx.FIXED_ID)
        self.assertIsNone(result[2])

    def test_enter_continues_this_directorys_newest_session(self) -> None:
        # The list opens on this directory's most recently used session, so
        # Enter continues it (claude-multi -c on the screen).
        self.record(managed_id=fx.OTHER_ID, last_seen=timedelta(hours=3))
        self.record(managed_id=fx.FIXED_ID, last_seen=timedelta(minutes=2))
        result, win = self.run_screen(self.screen(), [ENTER])
        self.assertTrue(any("sessions — " in frame for frame in win.frames))
        self.assertEqual(result[0], "perform")
        self.assertEqual(result[1].record["managed_id"], fx.FIXED_ID)
        self.assertEqual(result[1].result.session_action.kind, "resume")
        self.assertEqual(self.runtime.launches, [], "the screen never performs")

    def test_resume_with_a_diff_opens_the_resume_card(self) -> None:
        self.record()
        writer = profile.WRITER_IDS[0]

        def mutate(document):
            binding = document["agents"][writer]
            efforts = profile.declared_efforts(self.lcat.lines[binding["model"]])
            binding["effort"] = next(e for e in efforts if e != binding["effort"])

        self.runtime.profiles.update(catalog.DEFAULT_SEED, mutate)
        with self.healthy_card(), mock.patch('claude_multi.cli.screens.launch_sessions._LaunchCardScreen._gateway_status', lambda _s: None):
            result, win = self.run_screen(self.screen(), [ENTER, ESC, ESC])
        self.assertIsNone(result)
        self.assertTrue(any("Enter resume" in frame for frame in win.frames), "the resume card")
        self.assertIn("Resume cancelled.", win.frames[-1])

    def test_repair_modal_relinks_and_the_resume_prepares(self) -> None:
        record = self.record()
        record["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        record["observed_cwd"] = "/wrong/project"
        self.runtime.session_store.save(record)
        result, win = self.run_screen(self.screen(), [ENTER, ENTER])
        self.assertTrue(any("Session needs identity repair" in f for f in win.frames))
        self.assertEqual(result[0], "perform")
        self.assertIsNone(result[2])
        repaired = self.runtime.session_store.load(fx.FIXED_ID)
        self.assertNotIn("observed_cwd", repaired)
        self.assertEqual(repaired["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertEqual(result[1].record["managed_id"], fx.FIXED_ID)

    def test_gate_cancel_keeps_the_record_untouched(self) -> None:
        record = self.record()
        record["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        record["observed_cwd"] = "/wrong/project"
        self.runtime.session_store.save(record)
        before = self.runtime.session_store.read_record_bytes(fx.FIXED_ID)
        result, win = self.run_screen(self.screen(), [ENTER, ESC, ESC])
        self.assertIsNone(result)
        self.assertIn("Resume cancelled.", win.frames[-1])
        self.assertEqual(self.runtime.session_store.read_record_bytes(fx.FIXED_ID), before)

    def test_daemon_owned_gate_modal_resume_anyway_threads_force(self) -> None:
        record = self.record()
        self.runtime.live_prefixes = frozenset({sessions.runtime_session_id(record)[:8]})
        # buttons: Stop & resume, Resume anyway, Cancel → → Enter picks "Resume anyway".
        result, win = self.run_screen(self.screen(), [ENTER, RIGHT, ENTER])
        self.assertTrue(any("Session is live in the background" in f for f in win.frames))
        self.assertEqual(result[0], "perform")
        self.assertEqual(result[2], "force")

    def test_force_decision_skips_the_daemon_modal(self) -> None:
        # Restores ResumeTranscriptBoundaryTests.test_picker_force_skips_daemon_modal.
        record = self.record()
        self.runtime.live_prefixes = frozenset({sessions.runtime_session_id(record)})
        result, win = self.run_screen(self.screen(resume_decision="force"), [ENTER])
        self.assertFalse(any("Session is live in the background" in f for f in win.frames))
        self.assertEqual(result[2], "force")

    def test_transcript_missing_gate_guidance_is_shown(self) -> None:
        self.record(transcript=False)
        result, win = self.run_screen(self.screen(), [ENTER, ENTER, ESC])
        self.assertIsNone(result)
        self.assertTrue(any("Transcript not found" in f for f in win.frames))
        self.assertEqual(self.runtime.launches, [])

    def test_needs_choice_gate_opens_the_chooser(self) -> None:
        self.record(needs_choice=True)
        with mock.patch('claude_multi.cli.screens.launch_sessions._run_resume_gate_modal') as gate_modal:
            result, win = self.run_screen(self.screen(), [ENTER, ESC, ESC])
        self.assertIsNone(result)
        gate_modal.assert_not_called()
        self.assertTrue(any("choose a lineup for 11111111" in f for f in win.frames))


class SessionsForkTests(_SessionsCase):
    """Enter on a ⚠ row: adopt or discard the pending fork; Cancel is focused first."""

    def forked(self, *forks: str) -> dict:
        record = self.record()
        record["pending_forks"] = [
            {"session_id": fork, "observed_at": fx.iso(fx.FIXED_NOW)} for fork in (forks or (fx.OTHER_ID,))
        ]
        self.runtime.session_store.save(record)
        return self.runtime.session_store.load(fx.FIXED_ID)

    def test_the_modal_names_the_fork_and_discard_clears_it(self) -> None:
        record = self.forked()
        # buttons: Cancel (focused first), Adopt, Discard.
        result, win = self.run_screen(self.screen(), [ENTER, RIGHT, RIGHT, ENTER, ESC])
        self.assertIsNone(result)
        modal = _flat("\n".join(win.frames))
        self.assertIn("Resolve fork on 11111111…?", modal)
        self.assertIn(sessions.fork_adopt_command(record, fx.OTHER_ID), modal.replace("  ", " "))
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["pending_forks"], [])
        self.assertIn("marker discarded; transcript kept", win.frames[-1])
        self.assertEqual(self.runtime.launches, [])

    def test_discard_reports_the_remaining_forks(self) -> None:
        self.forked(fx.OTHER_ID, fx.THIRD_ID)
        _result, win = self.run_screen(self.screen(), [ENTER, RIGHT, RIGHT, ENTER, ESC], width=120)
        self.assertIn("1 more pending (Enter again)", win.frames[-1])
        remaining = self.runtime.session_store.load(fx.FIXED_ID)["pending_forks"]
        self.assertEqual([item["session_id"] for item in remaining], [fx.THIRD_ID])

    def test_a_concurrent_fork_state_change_is_a_message(self) -> None:
        self.forked()
        with mock.patch.object(
            self.runtime.session_store, "resolve_fork",
            side_effect=sessions.SessionError(f"session {fx.FIXED_ID} has no pending fork {fx.OTHER_ID}"),
        ):
            result, win = self.run_screen(self.screen(), [ENTER, RIGHT, RIGHT, ENTER, ESC])
        self.assertIsNone(result)
        self.assertIn("no pending fork", win.frames[-1])

    def test_enter_on_the_fork_modal_cancels_by_default(self) -> None:
        self.forked()
        result, win = self.run_screen(self.screen(), [ENTER, ENTER, ESC])
        self.assertIsNone(result)
        self.assertEqual(len(self.runtime.session_store.load(fx.FIXED_ID)["pending_forks"]), 1)
        self.assertIn("Resolve fork cancelled.", win.frames[-1])

    def test_an_authority_holding_marker_is_cleared_without_a_modal(self) -> None:
        record = self.forked()
        record["runtime_aliases"] = [
            {"session_id": fx.OTHER_ID, "source": "fork", "observed_at": fx.iso(fx.FIXED_NOW)}
        ]
        # Reality resolved it: the fork id is already an alias of this record.
        with mock.patch.object(sessions, "drop_resolved_pending_forks", return_value={**record, "pending_forks": []}), \
                mock.patch.object(self.runtime.session_store, "converge_pending_forks") as converge:
            result, win = self.run_screen(self.screen(), [ENTER, ESC])
        self.assertIsNone(result)
        converge.assert_called_once_with(fx.FIXED_ID)
        self.assertIn("fork marker cleared (runtime already resolved it)", win.frames[-1])

    def test_adopt_links_the_fork_to_a_profile(self) -> None:
        self.forked()
        self.native([fx.OTHER_ID])
        first = cli.profile_pick_order(self.runtime)[0]
        # Adopt (right of the focused Cancel) → the profile list (MRU first) → Enter.
        result, win = self.run_screen(self.screen(), [ENTER, RIGHT, ENTER, ENTER, ESC])
        self.assertIsNone(result)
        linked = self.runtime.session_store.resolve(fx.OTHER_ID)
        self.assertEqual(linked["profile"], first)
        self.assertEqual(linked["lineup_generation"], 0)
        self.assertIn(f"Linked runtime {fx.OTHER_ID}", _flat("\n".join(win.frames)))

    def test_resume_is_never_prepared_for_a_forked_row(self) -> None:
        self.forked()
        with mock.patch.object(self.runtime, "prepare") as prepare:
            self.run_screen(self.screen(), [ENTER, ESC, ESC])
        prepare.assert_not_called()


# ================================================================ T


class SessionsLineupTests(_SessionsCase):
    def dialog(self, record=None, **kwargs) -> cli._LineupDialog:
        record = record or self.runtime.session_store.load(fx.FIXED_ID)
        kwargs.setdefault("liveness", "unknown")
        return cli._LineupDialog(self.runtime, record, palette=tui.MONO_PALETTE, **kwargs)

    def to(self, dialog, name: str) -> None:
        dialog.profile_name = name
        dialog._preview()

    def test_live_profile_change_applies_and_shows_the_report(self) -> None:
        record = self.record()
        name = self.live_seed()
        dialog = self.dialog()
        self.to(dialog, name)
        self.assertEqual(dialog.mode, "LIVE", dialog.reasons)
        text = self.frame(dialog)
        self.assertIn("mode  LIVE — running agents keep their model; new spawns use the new lineup", text)
        self.assertIn("next  run /reload-plugins in that session", text)
        win = FakeWindow([ENTER, ENTER], height=30, width=100)
        self.assertTrue(dialog.run(win))
        report = _flat("\n".join(win.frames))
        self.assertIn("lineup — 11111111", report)
        self.assertIn("/reload-plugins", report)
        after = self.runtime.session_store.load(fx.FIXED_ID)
        self.assertEqual(after["lineup_generation"], record["lineup_generation"] + 1)
        self.assertEqual(after["profile"], name)

    def test_apply_is_called_with_the_exact_lineup_args(self) -> None:
        self.record()
        name = self.live_seed()
        dialog = self.dialog()
        self.to(dialog, name)
        with mock.patch.object(claude_multi.lineup, "apply", return_value=lineup.ApplyResult("done\n")) as apply:
            self.assertTrue(dialog.run(FakeWindow([ENTER, ENTER])))
        args = apply.call_args.args[1]
        self.assertEqual(args, lineup.LineupArgs(False, sessions.runtime_session_id(dialog.record), False, False,
                                                 lineup.parse_request(["profile", name])))
        self.assertEqual(apply.call_args.kwargs, {"interactive": True, "relaunch_exec": None})

    def test_relaunch_request_is_recorded_pending_via_lineup_apply(self) -> None:
        # The TUI never execs a relaunch.
        record = self.record()
        name = self.relaunch_seed()
        dialog = self.dialog()
        self.to(dialog, name)
        self.assertEqual(dialog.mode, "RELAUNCH")
        text = self.frame(dialog, height=30)
        self.assertIn("mode  RELAUNCH — exit the session; resume applies it", text)
        self.assertIn("why   native agents:", text)
        self.assertIn("next  recorded as a pending change", text)
        win = FakeWindow([ENTER, ENTER], height=30, width=100)
        self.assertTrue(dialog.run(win))
        self.assertIn("Recorded as a pending change", _flat("\n".join(win.frames)))
        after = self.runtime.session_store.load(fx.FIXED_ID)
        self.assertIn("pending", after)
        self.assertEqual(after["pending"]["profile"], name)
        self.assertEqual(after["lineup_generation"], record["lineup_generation"])
        self.assertEqual(self.runtime.launches, [])

    def test_relaunch_reasons_for_gen0_and_no_scope(self) -> None:
        self.record(generation=0)
        dialog = self.dialog()
        self.to(dialog, self.live_seed())
        self.assertEqual((dialog.mode, dialog.reasons), ("RELAUNCH", (lineup.REASON_GEN0,)))
        self.runtime.session_store.forget_session(fx.FIXED_ID)
        self.record(scope=False)
        dialog = self.dialog()
        self.to(dialog, self.live_seed())
        self.assertEqual((dialog.mode, dialog.reasons), ("RELAUNCH", (lineup.REASON_NO_SCOPE,)))

    def test_errors_disable_enter(self) -> None:
        self.record()
        name = "broken-target"
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = name
        document["agents"][profile.WRITER_IDS[0]] = {"model": "no-such-line", "effort": "high"}
        self.runtime.profiles.save(document)
        dialog = self.dialog()
        self.to(dialog, name)
        self.assertTrue(dialog.errors)
        text = self.frame(dialog)
        self.assertIn("✗", text)
        with mock.patch.object(claude_multi.lineup, "apply") as apply:
            self.assertIsNone(dialog.run(FakeWindow([ENTER, ESC])))
        apply.assert_not_called()
        self.assertEqual(dialog.message, cli.LINEUP_NOTHING)

    def test_no_change_is_nothing_to_apply(self) -> None:
        self.record()
        dialog = self.dialog()
        self.to(dialog, catalog.DEFAULT_SEED)
        self.assertEqual(dialog.diff, [])
        self.assertIn("diff  (no change)", self.frame(dialog))
        with mock.patch.object(claude_multi.lineup, "apply") as apply:
            self.assertIsNone(dialog.run(FakeWindow([ENTER, ESC])))
        apply.assert_not_called()

    def test_a_refusal_is_the_message_and_the_dialog_stays(self) -> None:
        self.record()
        dialog = self.dialog()
        self.to(dialog, self.live_seed())
        with mock.patch.object(claude_multi.lineup, "apply", side_effect=lineup.LineupRefusal("R-99 refused")):
            win = FakeWindow([ENTER, ESC])
            self.assertIsNone(dialog.run(win))
        self.assertIn("R-99 refused", win.frames[-1])

    def test_tab_cycles_the_targets_and_left_right_the_profiles(self) -> None:
        self.record()
        dialog = self.dialog()
        first = dialog.profile_name
        dialog.run(FakeWindow([RIGHT, ESC]))
        self.assertNotEqual(dialog.profile_name, first)
        dialog.run(FakeWindow([LEFT, ESC]))
        self.assertEqual(dialog.profile_name, first)
        dialog.run(FakeWindow([TAB, ESC]))
        self.assertEqual(dialog.target, "per-agent")
        self.assertEqual(dialog.preview_note, "Space picks an agent")
        dialog.run(FakeWindow([TAB, ESC]))
        self.assertEqual(dialog.target, "direct")
        self.assertEqual(dialog.request.verb, "direct")
        dialog.run(FakeWindow([BTAB, BTAB, ESC]))
        self.assertEqual(dialog.target, "profile")

    def test_per_agent_unset_builds_one_unset_request(self) -> None:
        record = self.record()
        dialog = self.dialog()
        agent = profile.WRITER_IDS[0]
        self.assertIn(agent, record["applied"]["agents"])
        steps = [DOWN] * list(catalog.AGENT_ROLE_IDS).index(agent)
        dialog.run(FakeWindow([TAB, SPACE, *steps, "u", ESC], height=30))
        self.assertEqual(dialog.agent_words, ["unset", agent])
        self.assertEqual(dialog.request, lineup.parse_request(["unset", agent]))
        self.assertTrue(dialog.diff)

    def test_per_agent_rows_line_up_their_bindings(self) -> None:
        self.record()
        dialog = self.dialog()
        win = FakeWindow([TAB, SPACE, ESC, ESC], height=30, width=80)
        dialog.run(win)
        frame = next(frame for frame in win.frames if "per-agent edit —" in frame)
        rows = [line[4:] for line in frame.splitlines()
                if line[4:].split(" ", 1)[0] in {profile.label(rid) for rid in catalog.AGENT_ROLE_IDS}]
        self.assertEqual(len(rows), len(catalog.AGENT_ROLE_IDS))
        # Each binding starts in one column, after a gap, whatever the name's length.
        starts = {len(row) - len(row[len(row.split(" ", 1)[0]):].lstrip()) for row in rows}
        self.assertEqual(len(starts), 1, rows)
        self.assertTrue(all("  " in row for row in rows))

    def test_per_agent_pick_builds_one_set_request(self) -> None:
        self.record()
        dialog = self.dialog()
        agent = catalog.AGENT_ROLE_IDS[0]
        with mock.patch.object(claude_multi.tui.BindingPicker, "run", return_value=("bind", self.default_lead(), "high")):
            dialog.run(FakeWindow([TAB, SPACE, ENTER, ESC], height=30))
        self.assertEqual(dialog.agent_words, ["set", f"{agent}={self.default_lead()}:high"])
        self.assertEqual(dialog.request.verb, "set")

    def test_the_dialog_title_and_esc_change_nothing(self) -> None:
        self.record(title="api design")
        before = self.runtime.session_store.read_record_bytes(fx.FIXED_ID)
        dialog = self.dialog(liveness="ended")
        text = self.frame(dialog)
        self.assertIn('change lineup — "api design" (ended)', text)
        self.assertIsNone(dialog.run(FakeWindow([ESC])))
        self.assertEqual(self.runtime.session_store.read_record_bytes(fx.FIXED_ID), before)

    def test_t_on_a_v3_row_shows_the_2x_message_and_opens_nothing(self) -> None:
        self.v3_record()
        with mock.patch('claude_multi.cli.screens.launch_sessions._LineupDialog') as dialog:
            result, win = self.run_screen(self.screen(), ["t", ESC])
        self.assertIsNone(result)
        dialog.assert_not_called()
        self.assertIn(cli.SESSIONS_V3_LINEUP, win.frames[-1])

    def test_t_from_the_screen_opens_the_dialog(self) -> None:
        self.record()
        result, win = self.run_screen(self.screen(), ["t", ESC, ESC])
        self.assertIsNone(result)
        self.assertTrue(any("change lineup — 11111111" in f for f in win.frames))

    def test_floor(self) -> None:
        self.record()
        dialog = self.dialog()
        rows, cols = dialog.min_size(80)
        self.assertEqual(cols, cli.LINEUP_DIALOG_MIN_COLS)
        self.assertIn(tui.TOO_SMALL.format(what=dialog.what), self.frame(dialog, height=rows - 1))
        self.assertIn("change lineup — ", self.frame(dialog, height=rows))
        self.assertIn(tui.TOO_SMALL.format(what=dialog.what), self.frame(dialog, height=rows, width=cols - 1))


class LineupDialogDirectTests(_SessionsCase):
    """The direct target: cross-class is RELAUNCH, within the class LIVE with a lead switch."""

    def dialog_for(self, key: str) -> cli._LineupDialog:
        dialog = cli._LineupDialog(
            self.runtime, self.runtime.session_store.load(fx.FIXED_ID), palette=tui.MONO_PALETTE,
            liveness="unknown",
        )
        dialog.target = "direct"
        dialog.direct_words = ["direct", f"{key}:{profile.ULTRACODE}"]
        dialog._preview()
        return dialog

    def lead_class_of(self, key: str) -> str:
        return self.lcat.lines[key]["context"]["ordinary_profile"]

    def test_direct_within_the_lead_class_is_live_with_a_lead_switch(self) -> None:
        lead = self.default_lead()
        self.direct_record(lead)
        other = next(k for k in self.lead_classes()[self.lead_class_of(lead)] if k != lead)
        dialog = self.dialog_for(other)
        self.assertEqual(dialog.mode, "LIVE", dialog.reasons)
        self.assertIsNotNone(dialog.lead_switch)
        text = self.frame(dialog)
        self.assertIn(f"lead  switch with /model → {dialog.lead_switch} (press s: this session only)", text)

    def test_direct_to_a_line_outside_the_lead_class_is_relaunch(self) -> None:
        lead = self.default_lead()
        self.direct_record(lead)
        cls = self.lead_class_of(lead)
        other = next(keys[0] for name, keys in self.lead_classes().items() if name != cls)
        dialog = self.dialog_for(other)
        self.assertEqual(dialog.mode, "RELAUNCH")
        self.assertTrue(any("outside this session's lead set" in reason for reason in dialog.reasons), dialog.reasons)

    def test_direct_from_a_profile_less_class_is_relaunch(self) -> None:
        # 3.0 restatement of the 2.x "single-model" class: a lead class with one
        # line (derived); leaving it for another class is a RELAUNCH.
        single = next(keys[0] for keys in self.lead_classes().values() if len(keys) == 1)
        self.direct_record(single)
        other = next(keys[0] for name, keys in self.lead_classes().items() if name != self.lead_class_of(single))
        dialog = self.dialog_for(other)
        self.assertEqual(dialog.mode, "RELAUNCH")
        self.assertTrue(any("lead class" in reason for reason in dialog.reasons), dialog.reasons)


class LineupDialogInjectionTests(_SessionsCase):
    HOSTILE = "evil\x1b]8;;https://bad.example\x07link\x1b[2J"

    def test_hostile_custom_display_title_and_cwd_are_sanitized(self) -> None:
        # The diff labels come from displays; a custom model's display
        # carries the hostile bytes, as do the title and the cwd name.
        provider = next(
            pid for pid, entry in sorted(self.runtime.catalog.providers.items())
            if entry["transport"]["kind"] == "direct"
        )
        custom.add_model(
            self.runtime.environ, "hostile-custom", wire_model="hostile-custom-wire", provider=provider,
            context_tokens=262144, display=self.HOSTILE, created_via="manual",
            catalog_providers=self.runtime.catalog.providers,
            catalog_models=tuple(self.runtime.catalog.lines), retired_models=tuple(self.lcat.retired),
        )
        record = self.record()
        record["title"] = "title \x07bell"
        with self.assertRaises(sessions.SessionError):  # titles are printable by construction
            self.runtime.session_store.save(record)
        self.runtime.session_store.set_title(fx.FIXED_ID, "a title")
        dialog = cli._LineupDialog(
            self.runtime, self.runtime.session_store.load(fx.FIXED_ID), palette=tui.MONO_PALETTE,
            liveness="unknown",
        )
        dialog.target = "direct"
        dialog.direct_words = ["direct", "hostile-custom:ultracode"]
        dialog._preview()
        text = self.frame(dialog, width=100)
        self.assertTrue(any("evil" in line for line in dialog.diff), dialog.diff)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertIn("^[", text)
        hostile_dir = Path(self.runtime.cwd).parent / "proj\x1b[31m"
        hostile_dir.mkdir()
        self.runtime.cwd = str(hostile_dir)
        screen_text = self.frame(self.screen())
        self.assertNotIn("\x1b", screen_text)
        self.assertIn("proj^[", screen_text)


# ================================================================ F / R / X / E / L


class SessionsFollowTests(_SessionsCase):
    def test_f_pins_a_following_session(self) -> None:
        record = self.record()
        result, win = self.run_screen(self.screen(), ["f", "y", ESC])
        self.assertIsNone(result)
        preview = _flat("\n".join(win.frames))
        self.assertIn("pin — preview for 11111111", preview)
        self.assertIn(f"follow: on (profile {record['profile']}) → off", preview)
        self.assertIn(lineup.PREVIEW_ONLY, preview)
        self.assertIn(
            f"session 11111111 is now pinned (follow off); lineup gen {record['lineup_generation']} unchanged",
            win.frames[-1],
        )
        self.assertFalse(self.runtime.session_store.load(fx.FIXED_ID)["follow"])

    def test_f_follows_a_pinned_session(self) -> None:
        self.record(follow=False)
        _result, win = self.run_screen(self.screen(), ["f", "y", ESC])
        self.assertIn("now follows profile", win.frames[-1])
        self.assertTrue(self.runtime.session_store.load(fx.FIXED_ID)["follow"])

    def test_f_on_an_ad_hoc_record_is_the_r12_refusal(self) -> None:
        self.direct_record(self.default_lead(), follow=False)
        _result, win = self.run_screen(self.screen(), ["f", ESC], width=120)
        self.assertIn(lineup.R12.format(m8=fx.FIXED_ID[:8]), win.frames[-1])

    def test_f_on_a_deleted_profile_is_the_r13_refusal(self) -> None:
        name = "gone-soon"
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = name
        self.runtime.profiles.save(document)
        self.record(name, follow=False)
        self.runtime.profiles.delete(name)
        _result, win = self.run_screen(self.screen(), ["f", ESC], width=140)
        self.assertIn(lineup.R13.format(m8=fx.FIXED_ID[:8], p=name), win.frames[-1])

    def test_f_preview_declined_changes_nothing(self) -> None:
        record = self.record()
        _result, win = self.run_screen(self.screen(), ["f", "n", ESC])
        self.assertTrue(self.runtime.session_store.load(fx.FIXED_ID)["follow"])
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["mutation_token"], record["mutation_token"])
        self.assertIn("nothing changed", win.frames[-1])

    def test_f_with_a_pending_change_names_what_pinning_drops(self) -> None:
        self.record(pending=True)
        _result, win = self.run_screen(self.screen(), ["f", ESC, ESC], width=120)
        preview = _flat("\n".join(win.frames))
        self.assertIn(lineup.DISCARD_PENDING, preview)
        self.assertIn("pending change (", preview)
        self.assertIn("): dropped", preview)
        self.assertIn("pending", self.runtime.session_store.load(fx.FIXED_ID))

    def test_f_on_a_legacy_record_is_refused(self) -> None:
        record = self.v3_record()
        _result, win = self.run_screen(self.screen(), ["f", ESC])
        self.assertIn(cli.SESSIONS_V3_LINEUP, win.frames[-1])
        self.assertEqual(self.runtime.session_store.load(record["managed_id"])["version"], record["version"])


class SessionsRenameTests(_SessionsCase):
    def test_r_sets_and_clears_the_title(self) -> None:
        self.record(title="old")
        result, win = self.run_screen(self.screen(), ["r", *["\x7f"] * 3, *"new title", ENTER, ENTER, ESC])
        self.assertIsNone(result)
        self.assertTrue(any("rename 11111111 — title:" in f for f in win.frames))
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["title"], "new title")
        self.assertIn('renamed 11111111: "new title"', win.frames[-1])
        self.run_screen(self.screen(), ["r", *["\x7f"] * 9, ENTER, ENTER, ESC])
        self.assertNotIn("title", self.runtime.session_store.load(fx.FIXED_ID))

    def test_r_cancel_changes_nothing(self) -> None:
        self.record(title="keep")
        self.run_screen(self.screen(), ["r", "x", ESC, ESC])
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["title"], "keep")

    def test_a_refused_title_is_the_message(self) -> None:
        self.record()
        with mock.patch.object(self.runtime.session_store, "set_title",
                               side_effect=sessions.SessionError("session title must be 1-80 printable characters on one line")):
            _result, win = self.run_screen(self.screen(), ["r", "x", ENTER, ENTER, ESC], width=120)
        self.assertIn("1-80 printable characters", win.frames[-1])

    def test_r_on_a_v3_record_is_refused(self) -> None:
        self.v3_record()
        _result, win = self.run_screen(self.screen(), ["r", ESC])
        self.assertIn(cli.SESSIONS_V3_TITLE, win.frames[-1])


class SessionsForgetTests(_SessionsCase):
    def test_x_forgets_through_the_modal(self) -> None:
        self.record()
        scope_dir = scope.scope_dir(self.runtime.session_store.root, fx.FIXED_ID)
        self.assertTrue(scope_dir.is_dir())
        record = self.runtime.session_store.load(fx.FIXED_ID)
        runtime_id = sessions.runtime_session_id(record)
        # Cancel is focused first: → Forget, Enter; Esc closes the result view.
        result, win = self.run_screen(self.screen(), ["x", RIGHT, ENTER, ESC, ESC], width=120)
        self.assertIsNone(result)
        modal = _flat("\n".join(win.frames))
        self.assertIn("Forget session 11111111…?", modal)
        self.assertIn(f"Runtime id {runtime_id}.", modal)
        self.assertIn("the session record and its generated scope", modal)
        self.assertIn("transcript is never touched", modal)
        self.assertIn(f"claude-multi sessions link {runtime_id}", modal)
        self.assertFalse(self.runtime.session_store.exists(fx.FIXED_ID))
        self.assertFalse(scope_dir.exists())
        # After the forget: the runtime id and how to manage it again.
        self.assertIn(f"forgot 11111111…; its conversation stays as native session {runtime_id}", win.frames[-1])

    def test_the_forget_result_stays_readable_at_80x24(self) -> None:
        record = self.record()
        runtime_id = sessions.runtime_session_id(record)
        # → Forget, Enter: the result view; the frame after the forget, then Esc closes it.
        _result, win = self.run_screen(self.screen(), ["x", RIGHT, ENTER, ESC, ESC], height=24, width=80)
        self.assertFalse(self.runtime.session_store.exists(fx.FIXED_ID))
        modal = max(index for index, frame in enumerate(win.frames) if "Forget session 11111111…?" in frame)
        after = win.frames[modal + 1:]
        # A frame after the confirmation keeps the full runtime id and the command.
        view = next((frame for frame in after if f"claude-multi sessions link {runtime_id}" in _flat(frame)), None)
        self.assertIsNotNone(view, "the result after the forget keeps the full id and the command at 80x24")
        self.assertIn(cli_text.FORGET_DONE_TITLE.format(short="11111111…"), view)
        lines = view.splitlines()
        self.assertTrue(all(len(line) <= 79 for line in lines))
        self.assertNotIn("…", "\n".join(lines[3:]))
        flat = _flat(view)
        self.assertIn(f"Session {fx.FIXED_ID} is forgotten", flat)
        self.assertIn(f"native session {runtime_id};", flat)
        self.assertIn(f"choose {runtime_id}", flat)
        self.assertIn(f"claude-multi sessions link {runtime_id}", flat)
        self.assertIn("L in Sessions", flat)

    def test_x_cancel_keeps_the_record(self) -> None:
        self.record()
        # Enter on the default focus cancels.
        _result, win = self.run_screen(self.screen(), ["x", ENTER, ESC])
        self.assertTrue(self.runtime.session_store.exists(fx.FIXED_ID))
        self.assertIn("Forget cancelled.", win.frames[-1])

    def test_a_live_row_gets_stop_first_guidance_and_no_modal(self) -> None:
        record = self.record()
        self.runtime.live_prefixes = frozenset({sessions.runtime_session_id(record)[:8]})
        result, win = self.run_screen(self.screen(), ["x", ESC])
        self.assertFalse(any("Forget session" in f for f in win.frames))
        self.assertIn("stop it first (E)", win.frames[-1])
        self.assertTrue(self.runtime.session_store.exists(fx.FIXED_ID))

    def test_the_under_lock_refusal_is_the_message(self) -> None:
        record = self.record()
        screen = self.screen()  # sampled while not live
        self.runtime.live_prefixes = frozenset({fx.FIXED_ID[:8]})  # it went live since
        result, win = self.run_screen(screen, ["x", RIGHT, ENTER, ESC])
        self.assertIn("live in the background", win.frames[-1])
        self.assertTrue(self.runtime.session_store.exists(fx.FIXED_ID))


class SessionsStopTests(_SessionsCase):
    def test_e_stops_a_live_row(self) -> None:
        record = self.record()
        self.runtime.live_prefixes = frozenset({sessions.runtime_session_id(record)[:8]})
        with mock.patch('claude_multi.cli.session_actions._stop_runtime', return_value=None) as stop:
            result, win = self.run_screen(self.screen(), ["e", RIGHT, ENTER, ESC])
        self.assertIsNone(result)
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[1], sessions.runtime_session_id(record))
        self.assertTrue(any("Stop live session 11111111…?" in f for f in win.frames))
        self.assertIn("conversation kept; Enter resumes when ready", win.frames[-1])

    def test_enter_on_the_stop_modal_cancels_by_default(self) -> None:
        record = self.record()
        self.runtime.live_prefixes = frozenset({sessions.runtime_session_id(record)[:8]})
        with mock.patch('claude_multi.cli.session_actions._stop_runtime', return_value=None) as stop:
            _result, win = self.run_screen(self.screen(), ["e", ENTER, ESC])
        stop.assert_not_called()
        self.assertIn("Stop cancelled.", win.frames[-1])

    def test_e_on_a_row_not_live_is_a_message(self) -> None:
        self.record()
        with mock.patch('claude_multi.cli.session_actions._stop_runtime') as stop:
            _result, win = self.run_screen(self.screen(), ["e", ESC])
        stop.assert_not_called()
        self.assertIn("nothing to stop", win.frames[-1])


class SessionsLinkTests(_SessionsCase):
    NATIVE = "88888888-8888-4888-8888-888888888888"

    def test_l_links_a_native_session_to_a_profile(self) -> None:
        # Restores NativeDiscoveryTests.test_adopt_moves_native_to_managed.
        self.native([self.NATIVE])
        screen = self.screen()
        self.assertEqual(len(screen.native), 1)
        first = cli.profile_pick_order(self.runtime)[0]
        result, win = self.run_screen(screen, ["l", ENTER, ENTER, ESC])
        self.assertIsNone(result)
        linked = self.runtime.session_store.resolve(self.NATIVE)
        self.assertEqual(linked["profile"], first)
        self.assertEqual(linked["lineup_generation"], 0)
        self.assertEqual(screen.native, [])
        self.assertEqual(screen.records[screen.selected]["runtime_session_id"], self.NATIVE)
        self.assertIn(f"Linked runtime {self.NATIVE}", win.frames[-1])

    def test_l_direct_uses_choose_mode_link_with_no_effort_field(self) -> None:
        self.native([self.NATIVE])
        labels, _choices = cli._profile_choice_items(self.runtime)
        with mock.patch('claude_multi.cli.screens.direct.run_direct_screen', return_value=(self.default_lead(), None)) as direct:
            result, _win = self.run_screen(self.screen(), ["l", ENTER, END, ENTER, ESC])
        self.assertIsNone(result)
        self.assertEqual(direct.call_args.kwargs["purpose"], "link")
        linked = self.runtime.session_store.resolve(self.NATIVE)
        self.assertIsNone(linked["profile"])
        self.assertEqual(linked["applied"]["lead"]["key"], self.default_lead())
        self.assertEqual(labels[-1], cli.SESSIONS_DIRECT_ITEM)

    def test_link_choose_mode_shows_no_effort(self) -> None:
        screen = cli._DirectScreen(self.runtime, palette=tui.MONO_PALETTE, purpose="link")
        text = self.frame(screen)
        self.assertIn("choose a direct model — link", text)
        self.assertNotIn("effort [", text)

    def test_l_without_a_native_session_is_a_message(self) -> None:
        self.record()
        _result, win = self.run_screen(self.screen(), ["l", ESC])
        self.assertIn(cli.SESSIONS_NATIVE_NONE, win.frames[-1])


# ================================================================ the chooser


class NeedsChoiceChooserTests(_SessionsCase):
    def test_banner_then_chooser_prepares_a_relaunch_target(self) -> None:
        self.record(managed_id=fx.FIXED_ID, needs_choice=True)
        first = cli.profile_pick_order(self.runtime)[0]
        with self.healthy_card(), mock.patch('claude_multi.cli.screens.launch_sessions._LaunchCardScreen._gateway_status', lambda _s: None), \
                mock.patch.object(self.runtime, "prepare", wraps=self.runtime.prepare) as prepare:
            result, win = self.run_screen(self.screen(), [END, ENTER, ENTER, ENTER])
        target = prepare.call_args.args[0]
        self.assertEqual(target, cli.LaunchTarget("relaunch", None, first, True, "Relaunch 11111111"))
        self.assertEqual(prepare.call_args.kwargs["action"], "resume")
        self.assertEqual(prepare.call_args.kwargs["session_id"], fx.FIXED_ID)
        self.assertEqual(result[0], "perform")
        self.assertIsNone(result[2])
        self.assertTrue(any("choose a lineup for 11111111 — " in f for f in win.frames))
        self.assertTrue(any("Enter resume" in f for f in win.frames), "the resume card shows the diff")

    def test_several_records_are_listed_first(self) -> None:
        self.record(managed_id=fx.FIXED_ID, needs_choice=True)
        self.record(managed_id=fx.OTHER_ID, needs_choice=True, last_seen=timedelta(hours=1))
        result, win = self.run_screen(self.screen(), [END, ENTER, ESC, ESC])
        self.assertIsNone(result)
        listing = "\n".join(win.frames)
        self.assertIn(cli.NEEDS_CHOICE_LIST_TITLE, listing)
        self.assertIn(f"{fx.FIXED_ID[:8]}  {catalog.DEFAULT_SEED}  lead {fx.needs_choice_key(self.runtime)} removed", listing)

    def test_direct_choice_builds_an_ad_hoc_relaunch_target(self) -> None:
        self.record(needs_choice=True)
        key = self.default_lead()
        with mock.patch('claude_multi.cli.screens.direct.run_direct_screen', return_value=(key, "ultracode")) as direct, \
                mock.patch.object(self.runtime, "prepare", side_effect=cli.LaunchPlanError(["boom"])) as prepare:
            result, win = self.run_screen(self.screen(), [ENTER, END, ENTER, ESC, ESC])
        self.assertIsNone(result)
        self.assertEqual(direct.call_args.kwargs["purpose"], "needs a choice")
        target = prepare.call_args.args[0]
        self.assertEqual(target, cli.LaunchTarget("relaunch", profile.ad_hoc_direct(key, "ultracode"), None, False,
                                                  "Relaunch 11111111"))
        self.assertTrue(any("boom" in f for f in win.frames), "a prepare failure is a message; the list stays")

    def test_the_chooser_help(self) -> None:
        self.record(needs_choice=True)
        _result, win = self.run_screen(self.screen(), [ENTER, "?", ESC, ESC, ESC])
        self.assertIn("needs a choice — help", "\n".join(win.frames))


class TransitionHelpTextTests(unittest.TestCase):
    def test_transition_help_says_lineup(self) -> None:
        # "composition" became "lineup"; the title and the
        # modal texts are unchanged.
        self.assertNotIn("composition", cli.TRANSITION_HELP)
        self.assertIn("changes a session's lineup while keeping its transcript", cli.TRANSITION_HELP)
        self.assertEqual(cli.TRANSITION_MODAL_TITLE, "Confirm transition")


if __name__ == "__main__":
    unittest.main()
