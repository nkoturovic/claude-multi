"""The launch card's way into setup and profiles: Get started opening on its
own (and every case in which it never does), W, P, H while nothing is
connected, the bars, the default mark, the description, spend and
not-connected rows, a default profile that cannot be loaded, Tab skipping
it, the default-profile notice across replans, and the Settings row of the
default profile. A hermetic fixture runtime; nothing is sent upstream."""

from __future__ import annotations

import contextlib
import copy
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, choices, cli, profile, state, tui, views
from claude_multi.setup import defaults, model, status

import _tui_fixture as fx
from _tui_render import golden_runtime, render
from test_tui import BTAB, DOWN, END, HOME, LEFT, RIGHT, FakeWindow
import claude_multi.cli.screens.get_started as screens_get_started
import claude_multi.cli.screens.launch_sessions as launch_sessions
import claude_multi.cli.screens.profiles as screens_profiles
import claude_multi.cli.text as cli_text
import claude_multi.cli.types as cli_types

ESC = "\x1b"
ENTER = "\n"
TAB = "\t"


def disconnect(runtime) -> None:
    """Nothing connected: the runtime holds no key, and every provider that
    needs none (a server on the network) is turned off."""

    keyless = [conn.provider_id for conn in status.connections(runtime) if conn.state == status.CONNECTED]
    runtime.settings_store.update(
        lambda doc: doc.setdefault("providers", {}).update({pid: {"enabled": False} for pid in keyless}),
        catalog=runtime.lineup_catalog(),
    )


class CardOnboardingCase(unittest.TestCase):
    runtime_kwargs: dict = {}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-card-onboarding-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runtime = fx.fixture_runtime(self.tmp / "root", **self.runtime_kwargs)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(fx.hermetic(self.runtime))
        stack.enter_context(mock.patch("claude_multi.cli.gateway_facts._doctor_now", lambda: fx.FIXED_NOW))
        for name in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            self.runtime.environ.pop(name, None)
        self.runtime.profiles.install_seeds()

    def target(self, name: str = catalog.DEFAULT_SEED) -> cli.LaunchTarget:
        return cli.LaunchTarget("profile", self.runtime.profiles.load(name), name, True, f"Profile {name}")

    def card(self, target=None, **kwargs) -> launch_sessions._LaunchCardScreen:
        kwargs.setdefault("gateway_checked", True)
        kwargs.setdefault("hint_detector", lambda _contract: None)
        kwargs.setdefault("tty_in", io.StringIO())
        kwargs.setdefault("tty_out", io.StringIO())
        kwargs.setdefault("passthrough", [])
        return launch_sessions._LaunchCardScreen(self.runtime, target or self.target(), palette=tui.MONO_PALETTE,
                                                 **kwargs)

    @staticmethod
    def run_card(screen, keys, *, height=24, width=80):
        win = FakeWindow(list(keys), height=height, width=width)
        return screen.run(win), win

    def set_default(self, name: str | None) -> None:
        plan = defaults.set_default_plan(self.runtime, name)
        defaults.apply_default(self.runtime, plan, model.Confirmation.given(plan))

    @contextlib.contextmanager
    def get_started(self, result=None):
        calls: list[dict] = []

        def fake(runtime, win, palette, **kwargs):
            calls.append(kwargs)
            return result or screens_get_started.GetStartedResult("card")

        with mock.patch.object(screens_get_started, "run_get_started", fake):
            yield calls

    @contextlib.contextmanager
    def profiles(self, result=None):
        calls: list[dict] = []

        def fake(runtime, win, palette, **kwargs):
            calls.append(kwargs)
            return result or screens_profiles.ProfilesResult("back")

        with mock.patch.object(screens_profiles, "run_profiles_screen", fake):
            yield calls

    def broken(self, name: str = "broken") -> Path:
        path = self.runtime.profiles.root / f"{name}.json"
        state.atomic_write(path, b'{\n  "name": "broken",\n  oops\n}\n')
        return path

    def unloadable_card(self, name: str = "broken", **kwargs):
        self.broken(name)
        try:
            self.runtime.profiles.load(name)
        except profile.ProfileLoadError as exc:
            self.runtime.selection_load_error = exc
        target = cli.LaunchTarget("unloadable", None, name, False, f"Profile {name}")
        return self.card(target, **kwargs)


# ================================================================ opening on its own


class AutomaticOpenTests(CardOnboardingCase):
    def test_nothing_connected_opens_the_providers_step_once(self) -> None:
        disconnect(self.runtime)
        with self.get_started() as calls:
            self.run_card(self.card(auto_setup=True), [ESC])
        self.assertEqual([call["focus"] for call in calls], ["providers"])

    def test_a_missing_claude_code_copy_opens_the_claude_step(self) -> None:
        self.runtime.pin_report = True
        with self.get_started() as calls, mock.patch.object(launch_sessions, "_claude_card_row",
                                                             return_value=(None, None)):
            self.run_card(self.card(auto_setup=True), [ESC])
        self.assertEqual([call["focus"] for call in calls], ["claude"])

    def test_never_when_connected_explicit_resume_in_a_session_or_read_only(self) -> None:
        cases = {
            "connected": lambda: self.card(auto_setup=True),
            "explicit (no auto open requested)": lambda: self.card(auto_setup=False),
        }
        for label, build in cases.items():
            with self.subTest(label), self.get_started() as calls:
                self.run_card(build(), [ESC])
                self.assertEqual(calls, [])
        disconnect(self.runtime)
        with self.subTest("inside a Claude Code session"), self.get_started() as calls, \
                mock.patch.dict(self.runtime.environ, {"CLAUDECODE": "1"}):
            self.run_card(self.card(auto_setup=True), [ESC])
            self.assertEqual(calls, [])
        with self.subTest("read-only"), self.get_started() as calls, \
                mock.patch.object(self.runtime, "allow_state_writes", False):
            self.run_card(self.card(auto_setup=True), [ESC])
            self.assertEqual(calls, [])
        record = fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        prepared = self.runtime.prepare(cli._record_resume_target(record), action="resume", passthrough=[],
                                        session_id=fx.FIXED_ID)
        with self.subTest("a resume card"), self.get_started() as calls:
            self.run_card(self.card(prepared.target, action="resume", prepared=prepared, auto_setup=True), [ESC])
            self.assertEqual(calls, [])

    def test_launch_card_opens_it_only_for_a_profile_chosen_by_default(self) -> None:
        seen = []

        def fake_card(*args, **kwargs):
            seen.append((kwargs["explicit"], kwargs["auto_setup"]))
            raise tui.CursesError("stop")

        target = self.target()
        for explicit in (False, True):
            with mock.patch.object(launch_sessions, "_LaunchCardScreen", side_effect=fake_card), \
                    mock.patch.object(tui, "run_curses_on_streams"), contextlib.suppress(tui.CursesError):
                launch_sessions.launch_card(self.runtime, target, action="fresh", passthrough=[],
                                            input_stream=io.StringIO(), output_stream=io.StringIO(),
                                            no_color=True, explicit=explicit)
        self.assertEqual(seen, [(False, True), (True, False)])


# ================================================================ W, P, H


class GetStartedKeyTests(CardOnboardingCase):
    def test_w_opens_get_started_and_the_card_follows_the_default(self) -> None:
        screen = self.card(self.target(catalog.DEFAULT_SEED))
        with self.get_started() as calls:
            self.run_card(screen, ["w", ESC])
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["resume"])
        self.assertIsNotNone(calls[0]["open_profiles"])
        self.assertEqual(screen.target.profile, defaults.resolve_default(self.runtime).name)

    def test_a_chosen_profile_stays_on_the_card(self) -> None:
        other = next(name for name in self.runtime.profiles.names()
                     if name != defaults.resolve_default(self.runtime).name)
        screen = self.card(self.target(other), explicit=True)
        with self.get_started():
            self.run_card(screen, ["W", ESC])
        self.assertEqual(screen.target.profile, other)

    def test_use_from_get_started_retargets(self) -> None:
        other = sorted(self.runtime.profiles.names())[-1]
        screen = self.card()
        with self.get_started(screens_get_started.GetStartedResult("use", other)):
            _result, win = self.run_card(screen, ["w", ESC])
        self.assertEqual(screen.target.profile, other)
        self.assertIn(cli_text.PROFILES_USED.format(name=other)[:60], win.frames[-1])

    def test_a_resume_card_opens_it_read_only(self) -> None:
        record = fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        prepared = self.runtime.prepare(cli._record_resume_target(record), action="resume", passthrough=[],
                                        session_id=fx.FIXED_ID)
        screen = self.card(prepared.target, action="resume", prepared=prepared)
        with self.get_started() as calls:
            self.run_card(screen, ["w", ESC])
        self.assertEqual([(call["resume"], call["open_profiles"]) for call in calls], [(True, None)])

    def test_refused_inside_a_session_and_in_a_read_only_run(self) -> None:
        screen = self.card()
        with self.get_started() as calls, mock.patch.dict(self.runtime.environ, {"CLAUDECODE": "1"}):
            _result, win = self.run_card(screen, ["w", ESC])
        self.assertEqual(calls, [])
        self.assertEqual(screen.message, cli_text.GS_IN_SESSION)
        self.assertIn(cli_text.GS_IN_SESSION[:45], win.frames[-1])
        with self.get_started() as calls, mock.patch.object(self.runtime, "allow_state_writes", False):
            _result, win = self.run_card(screen, ["w", ESC])
        self.assertEqual(calls, [])
        self.assertEqual(screen.message, cli_text.GS_READONLY)
        self.assertIn(cli_text.GS_READONLY[:45], win.frames[-1])

    def test_profiles_from_get_started_returns_the_profile_used(self) -> None:
        other = sorted(self.runtime.profiles.names())[-1]
        screen = self.card()
        with self.profiles(screens_profiles.ProfilesResult("use", other)) as calls:
            self.assertEqual(screen._profiles_for_get_started(FakeWindow([])), other)
        self.assertEqual(calls[0]["selected"], catalog.DEFAULT_SEED)
        with self.profiles(screens_profiles.ProfilesResult("get-started")):
            self.assertIsNone(screen._profiles_for_get_started(FakeWindow([])))


class ProfilesKeyTests(CardOnboardingCase):
    def test_p_opens_profiles_on_the_card_profile(self) -> None:
        screen = self.card()
        with self.profiles() as calls:
            self.run_card(screen, ["p", ESC])
        self.assertEqual(calls[0]["selected"], catalog.DEFAULT_SEED)
        self.assertEqual(calls[0]["card_profile"], catalog.DEFAULT_SEED)

    def test_use_retargets_and_says_so(self) -> None:
        other = sorted(self.runtime.profiles.names())[-1]
        screen = self.card()
        with self.profiles(screens_profiles.ProfilesResult("use", other)):
            _result, win = self.run_card(screen, ["P", ESC])
        self.assertEqual(screen.target.profile, other)
        self.assertTrue(screen.chosen)
        self.assertIn(cli_text.PROFILES_USED.format(name=other)[:60], win.frames[-1])

    def test_the_card_follows_a_rename_and_a_removal(self) -> None:
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = "mine"
        self.runtime.profiles.save(document)
        screen = self.card(self.target("mine"))
        self.runtime.profiles.rename("mine", "ours")
        with self.profiles(screens_profiles.ProfilesResult("back", renamed=(("mine", "ours"),))):
            self.run_card(screen, ["p", ESC])
        self.assertEqual(screen.target.profile, "ours")
        self.runtime.profiles.remove("ours")
        with self.profiles(screens_profiles.ProfilesResult("back", removed=("ours",))):
            self.run_card(screen, ["p", ESC])
        self.assertEqual(screen.target.profile, defaults.resolve_default(self.runtime).name)

    def imported_card(self) -> launch_sessions._LaunchCardScreen:
        """A card opened with --profile-file: the imported document, no stored name."""

        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = "imported"
        target = cli.LaunchTarget("profile-file", document, None, False, "Profile file imported.json")
        return self.card(target, explicit=True)

    def test_use_on_an_imported_document_selects_the_stored_profile(self) -> None:
        other = next(name for name in sorted(self.runtime.profiles.names()) if name != catalog.DEFAULT_SEED)
        screen = self.imported_card()
        with self.profiles(screens_profiles.ProfilesResult("use", other)):
            _result, win = self.run_card(screen, ["P", ESC])
        self.assertEqual((screen.target.kind, screen.target.profile), ("profile", other))
        self.assertEqual(screen.target.document, self.runtime.profiles.load(other))
        self.assertIn(cli_text.PROFILES_USED.format(name=other)[:60], win.frames[-1])
        # The same through Get started.
        screen = self.imported_card()
        with self.get_started(screens_get_started.GetStartedResult("use", other)):
            self.run_card(screen, ["w", ESC])
        self.assertEqual((screen.target.kind, screen.target.profile), ("profile", other))

    def test_browsing_profiles_keeps_an_imported_document(self) -> None:
        screen = self.imported_card()
        with self.profiles(screens_profiles.ProfilesResult("back")):
            self.run_card(screen, ["P", ESC])
        self.assertEqual((screen.target.kind, screen.target.profile), ("profile-file", None))

    def mine(self) -> None:
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = "mine"
        self.runtime.profiles.save(document)

    def drive_editor(self, *, then_save: bool = False, keys: tuple = ()):
        """The editor renames the profile to ``ours`` (``keys`` answer what
        the rename asks), then with ``then_save`` saves it once more."""

        def run(editor, win):
            state = editor.state
            state.set_general(name="ours")
            outcome = tui.ProfileEditorOutcome("rename", "ours", copy.deepcopy(state.document), state.origin,
                                               state.loaded_digest)
            ok, note = editor.callbacks.save(FakeWindow([*keys, ESC, ESC]), outcome)
            self.assertTrue(ok, note)
            state.mark_saved(outcome)
            if then_save:
                state.set_general(description="changed after the rename")
                again = tui.ProfileEditorOutcome("save", "ours", copy.deepcopy(state.document), "ours",
                                                 state.loaded_digest)
                ok, note = editor.callbacks.save(FakeWindow([ESC, ESC]), again)
                self.assertTrue(ok, note)
                state.mark_saved(again)
            return state.saved

        return mock.patch.object(tui.ProfileEditorScreen, "run", run)

    def test_e_renaming_the_default_moves_it_and_the_card_follows(self) -> None:
        self.mine()
        self.set_default("mine")
        screen = self.card(self.target("mine"), explicit=True)
        with self.drive_editor(keys=(RIGHT, ENTER)):
            self.run_card(screen, ["e", ESC])
        self.assertEqual(choices.read(self.runtime.environ).get("default_profile"), "ours")
        self.assertEqual(screen.target.profile, "ours")

    def test_the_card_follows_a_rename_saved_again_in_the_editor(self) -> None:
        self.mine()
        screen = self.card(self.target("mine"), explicit=True)
        with self.drive_editor(then_save=True):
            self.run_card(screen, ["p", "e", ESC, ESC])
        self.assertEqual(screen.target.profile, "ours")
        self.assertEqual(screen.target.document["description"], "changed after the rename")

    def test_w_in_profiles_opens_get_started(self) -> None:
        screen = self.card()
        with self.profiles(screens_profiles.ProfilesResult("get-started")), self.get_started() as calls:
            self.run_card(screen, ["p", ESC])
        self.assertEqual(len(calls), 1)


class HealthKeyTests(CardOnboardingCase):
    def test_h_shows_the_first_run_checks_while_nothing_is_connected(self) -> None:
        disconnect(self.runtime)
        screen = self.card()
        self.assertTrue(screen.not_connected)
        seen = []
        with mock.patch.object(tui.TextView, "run", autospec=True,
                               side_effect=lambda view, win: seen.append((view.title, list(view.lines)))), \
                mock.patch.object(launch_sessions._LaunchCardScreen, "_run_health") as doctor:
            self.run_card(screen, ["h", ESC])
        doctor.assert_not_called()
        self.assertEqual(seen[0][0], cli_text.CHECK_STEP_TITLE)
        self.assertEqual(seen[0][1][-1], cli_text.FIRSTRUN_CARD_FOOTER)

    def test_h_is_doctor_once_something_is_connected(self) -> None:
        screen = self.card()
        self.assertFalse(screen.not_connected)
        with mock.patch.object(launch_sessions._LaunchCardScreen, "_run_health") as doctor:
            self.run_card(screen, ["h", ESC])
        doctor.assert_called_once()


# ================================================================ rows and bars


class CardRowsTests(CardOnboardingCase):
    def test_the_bars_fit_two_rows_at_80_columns(self) -> None:
        for keys in (views.CARD_KEYS_NOT_CONNECTED, views.CARD_KEYS_FRESH):
            with self.subTest(keys=keys):
                self.assertLessEqual(tui.KeyBar(keys)._rows_needed(80), 2)
        # With the update badge the bar still draws on two rows (compacted).
        update = (views.CARD_KEYS_FRESH[0], ("U", "update"), *views.CARD_KEYS_FRESH[1:])
        self.assertEqual(tui.KeyBar(update).rows(80), 2)

    def test_not_connected_bar_offers_w(self) -> None:
        disconnect(self.runtime)
        screen = self.card()
        self.assertEqual(screen.card.keys, views.CARD_KEYS_NOT_CONNECTED)
        self.assertEqual(self.card(auto_setup=False).card.keys, views.CARD_KEYS_NOT_CONNECTED)

    def test_the_default_mark(self) -> None:
        self.set_default(catalog.DEFAULT_SEED)
        title = self.card().card.rows[0].text
        self.assertIn(f"profile: {catalog.DEFAULT_SEED}{views.CARD_DEFAULT_MARK} ·", title)
        other = next(name for name in self.runtime.profiles.names() if name != catalog.DEFAULT_SEED)
        self.assertNotIn(views.CARD_DEFAULT_MARK, self.card(self.target(other)).card.rows[0].text)

    def test_the_description_row_is_dropped_first(self) -> None:
        card = self.card().card
        description = [row for row in card.rows if row.kind == "description"]
        self.assertEqual([row.text for row in description],
                         [self.runtime.profiles.load(catalog.DEFAULT_SEED)["description"]])
        fitted = views.card_fit(card, height=200, bar_rows=2)
        self.assertIn(description[0], fitted)
        # Shrink until something must go: the description goes first.
        for height in range(60, 10, -1):
            fitted = views.card_fit(card, height=height, bar_rows=2)
            if len(fitted) < len(card.rows):
                self.assertNotIn(description[0], fitted)
                break

    def test_a_pay_per_token_lineup_has_its_spend_row(self) -> None:
        aggregated = next(row.name for row in __import__("claude_multi.setup.profiles", fromlist=["rows"]).rows(
            self.runtime) if row.ready)
        card = self.card(self.target(aggregated)).card
        spend = [row for row in card.rows if row.kind == "spend"]
        self.assertTrue(spend)
        self.assertTrue(all(row.role == "warn" and "bill per token" in row.text for row in spend))

    def test_one_not_connected_notice_replaces_the_readiness_rows(self) -> None:
        disconnect(self.runtime)
        card = self.card().card
        notices = [row for row in card.rows if row.kind == "not-connected"]
        self.assertEqual([row.text for row in notices], [views.CARD_NOT_CONNECTED])
        self.assertEqual([row for row in card.rows if row.kind == "readiness"], [])

    def test_the_default_notice_survives_replans(self) -> None:
        self.runtime.selection_notices = ["your default profile gone no longer exists; using balanced — P → D sets another"]
        screen = self.card()
        texts = [row.text for row in screen.card.rows]
        self.assertTrue(any("no longer exists" in text for text in texts))
        screen._replan()
        screen._replan()
        self.assertEqual(sum("no longer exists" in row.text for row in screen.card.rows), 1)

    def test_the_default_notice_leaves_when_another_profile_is_chosen(self) -> None:
        self.runtime.selection_notices = ["your default profile gone no longer exists; using balanced — P → D sets another"]
        screen = self.card()
        first = screen.target.profile
        self.run_card(screen, ["\t", ESC])
        self.assertNotEqual(screen.target.profile, first)
        self.assertFalse(any("no longer exists" in row.text for row in screen.card.rows))


# ================================================================ a profile that cannot be loaded


class UnloadableTargetTests(CardOnboardingCase):
    def test_a_blocked_card_names_the_file_and_line(self) -> None:
        screen = self.unloadable_card()
        card = screen.card
        self.assertFalse(card.ready)
        self.assertTrue(any("broken.json line 3:" in error for error in card.errors), card.errors)
        self.assertEqual(card.remedy, views.CARD_UNLOADABLE_FIX)

    def test_enter_and_e_open_profiles_on_it(self) -> None:
        screen = self.unloadable_card()
        with self.profiles() as calls:
            result, _win = self.run_card(screen, [ENTER, "e", ESC])
        self.assertIsNone(result)
        self.assertEqual([call["selected"] for call in calls], ["broken", "broken"])

    def test_tab_skips_profiles_that_cannot_be_loaded(self) -> None:
        self.broken("aaa-broken")
        screen = self.card()
        self.assertIn("aaa-broken", screen.pick_order)
        self.assertIn("aaa-broken", screen.unloadable_names)
        seen = []
        for _ in range(len(screen.pick_order) + 2):
            screen._cycle(1)
            seen.append(screen.target.profile)
        self.assertNotIn("aaa-broken", seen)
        screen = self.unloadable_card("zzz-broken")
        screen._cycle(1)
        self.assertEqual(screen.target.kind, "profile")
        self.assertIsNone(screen.load_error)

    def test_deleting_it_in_profiles_retargets_to_the_default(self) -> None:
        screen = self.unloadable_card()
        self.runtime.profiles.remove("broken")
        with self.profiles(screens_profiles.ProfilesResult("back", removed=("broken",))):
            self.run_card(screen, ["p", ESC])
        self.assertEqual(screen.target.kind, "profile")
        self.assertEqual(screen.target.profile, defaults.resolve_default(self.runtime).name)


# ================================================================ Settings: the default profile


class SettingsDefaultRowTests(CardOnboardingCase):
    def settings(self):
        import claude_multi.cli.screens.settings as screens_settings

        screen = screens_settings._SettingsScreen(self.runtime, tui.MONO_PALETTE, tty_in=io.StringIO(),
                                                  tty_out=io.StringIO())
        screen.index = next(index for index, (kind, value) in enumerate(screen.entries)
                            if kind == "row" and value.key == views.SETTINGS_DEFAULT_KEY)
        return screen

    def test_the_row_shows_automatic_then_the_choice(self) -> None:
        screen = self.settings()
        self.assertEqual(screen.selected_row.value, views.SETTINGS_DEFAULT_AUTOMATIC)
        self.assertEqual(screen.keybar_bindings(), cli_text.SETTINGS_KEYBAR)
        self.set_default(catalog.DEFAULT_SEED)
        self.assertEqual(self.settings().selected_row.value, catalog.DEFAULT_SEED)

    def test_enter_lists_automatic_first_then_the_profiles_and_chooses(self) -> None:
        choices_ = [choice for choice in defaults.profile_choices(self.runtime) if choice.loadable]
        target = choices_[1].name
        screen = self.settings()
        win = FakeWindow([ENTER, DOWN, DOWN, ENTER, ESC], height=30, width=90)
        screen.run(win)
        picker = next(f for f in win.frames if f.splitlines()[0].strip() == cli_text.SETTINGS_DEFAULT_TITLE)
        lines = [line for line in picker.splitlines() if line.strip()]
        first = next(i for i, line in enumerate(lines) if views.SETTINGS_DEFAULT_AUTOMATIC in line)
        self.assertIn(choices_[0].name, lines[first + 1])
        ready = next(choice for choice in choices_ if choice.ready)
        self.assertIn(cli_text.SETTINGS_DEFAULT_CONNECTED, next(line for line in lines if f" {ready.name} " in line + " "))
        self.assertEqual(choices.read(self.runtime.environ).get("default_profile"), target)
        self.assertIn(cli_text.DEFAULT_SET.format(name=target)[:50], win.frames[-1])

    def test_r_resets_to_automatic(self) -> None:
        self.set_default(catalog.DEFAULT_SEED)
        screen = self.settings()
        # The reset dialog focuses Cancel first: Left moves to Reset.
        screen.run(FakeWindow(["r", LEFT, ENTER, ESC], height=30, width=90))
        self.assertIsNone(choices.read(self.runtime.environ).get("default_profile"))
        self.assertEqual(screen.selected_row.value, views.SETTINGS_DEFAULT_AUTOMATIC)


# ================================================================ line mode and the entry


class LineModeTests(CardOnboardingCase):
    """``--line`` with nothing connected offers ``claude-multi setup`` first."""

    def offer(self, answer: str) -> tuple[bool, str, list]:
        import claude_multi.cli.commands.setup as commands_setup
        import claude_multi.cli.launch_flow as launch_flow

        calls: list = []
        out = io.StringIO()
        with mock.patch.object(commands_setup, "_bare", lambda *args, **kwargs: calls.append(kwargs)):
            ran = launch_flow._offer_setup_line(self.runtime, io.StringIO(answer), out)
        return ran, out.getvalue(), calls

    def test_enter_or_y_runs_setup(self) -> None:
        from claude_multi.setup import texts

        disconnect(self.runtime)
        for answer in ("\n", "y\n", "Yes\n"):
            with self.subTest(answer=answer):
                ran, text, calls = self.offer(answer)
                self.assertTrue(ran)
                self.assertEqual(len(calls), 1)
                self.assertEqual(text, texts.LINE_NOT_CONNECTED + "\n" + texts.LINE_SETUP_NOW)

    def test_n_or_end_of_input_continues_without_setup(self) -> None:
        disconnect(self.runtime)
        for answer in ("n\n", "no\n", ""):
            with self.subTest(answer=answer):
                ran, _text, calls = self.offer(answer)
                self.assertFalse(ran)
                self.assertEqual(calls, [])

    def test_never_offered_when_connected_in_a_session_or_read_only(self) -> None:
        ran, text, _calls = self.offer("y\n")  # the fixture has a connected provider
        self.assertEqual((ran, text), (False, ""))
        disconnect(self.runtime)
        self.runtime.environ["CLAUDECODE"] = "1"
        self.assertEqual(self.offer("y\n")[:2], (False, ""))
        self.runtime.environ.pop("CLAUDECODE")
        with mock.patch.object(self.runtime, "allow_state_writes", False):
            self.assertEqual(self.offer("y\n")[:2], (False, ""))

    def test_an_interrupted_setup_says_so(self) -> None:
        import claude_multi.cli.commands.setup as commands_setup
        import claude_multi.cli.launch_flow as launch_flow
        from claude_multi.setup import texts

        disconnect(self.runtime)
        out = io.StringIO()
        with mock.patch.object(commands_setup, "_bare", side_effect=KeyboardInterrupt):
            self.assertTrue(launch_flow._offer_setup_line(self.runtime, io.StringIO("y\n"), out))
        self.assertIn(texts.SETUP_CANCELLED, out.getvalue())


class EntryTests(CardOnboardingCase):
    def main(self, argv: list[str], stdin: str = "") -> tuple[int, str, str]:
        from claude_multi.cli import entry

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = entry.main(argv, runtime=self.runtime, input_stream=io.StringIO(stdin), output_stream=out,
                              interactive=False)
        return code, out.getvalue(), err.getvalue()

    def test_a_non_interactive_launch_with_nothing_connected_names_setup(self) -> None:
        from claude_multi.setup import texts

        code, out, err = self.main([])
        self.assertEqual(code, 2)
        self.assertIn(cli_text.NONINTERACTIVE_PROFILE_REQUIRED, out + err)
        self.assertNotIn(texts.LINE_SETUP_FIX, out + err)
        disconnect(self.runtime)
        code, out, err = self.main([])
        self.assertEqual(code, 2)
        self.assertIn(texts.LINE_SETUP_FIX, out + err)

    def test_a_model_switch_over_newer_state_is_refused(self) -> None:
        import json

        from claude_multi import hooks

        state.atomic_write(self.runtime.session_store.root / "state-version", b"99\n")
        argv = ["session-event", "premodel", "--managed-id", fx.FIXED_ID, "--launch-epoch", "0",
                "--hook-protocol", "3"]
        code, out, err = self.main(argv, '{"hook_event_name": "PreModelSwitch", "model": "x"}')
        self.assertEqual(code, 0)
        self.assertTrue(err.startswith("claude-multi: "), err)
        document = json.loads(out)
        specific = document["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreModelSwitch")
        self.assertEqual(out, hooks.premodel_fail_closed_response(document["systemMessage"]))
        self.assertTrue(document["systemMessage"].startswith("claude-multi premodel failed: "))
        # Every other event stays silent on stdout.
        code, out, _err = self.main(["session-event", "prompt", "--managed-id", fx.FIXED_ID, "--launch-epoch", "0",
                                     "--hook-protocol", "3"], "{}")
        self.assertEqual((code, out), (0, ""))


# ================================================================ golden


def card_not_connected_golden(root: Path) -> str:
    """A fresh card on the default seed while nothing is connected."""

    with golden_runtime(root, secrets=()) as runtime:
        runtime.profiles.install_seeds()
        disconnect(runtime)
        target = cli.LaunchTarget("profile", runtime.profiles.load(catalog.DEFAULT_SEED), catalog.DEFAULT_SEED,
                                  True, f"Profile {catalog.DEFAULT_SEED}")
        return render(lambda: launch_sessions._LaunchCardScreen(
            runtime, target, passthrough=[], palette=tui.MONO_PALETTE, gateway_checked=True,
            hint_detector=lambda _contract: None, tty_in=io.StringIO(), tty_out=io.StringIO()))


if __name__ == "__main__":
    unittest.main()
