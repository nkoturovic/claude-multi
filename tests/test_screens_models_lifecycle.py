"""Models actions keep optional badges separate from provider/route usability.
Enter admits/revokes without inference or apply, Q is an explicit diagnostic,
and X removes a model with replacement where needed. Hermetic fixtures only.
"""

from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

import test_cli
from test_tui import RIGHT, FakeWindow
from claude_multi import tui
from claude_multi.setup import model
import claude_multi.cli.consent as consent
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.models as models_screen
import claude_multi.cli.screens.providers as providers_screen
import claude_multi.cli.text as cli_text

ESC = "\x1b"
ENTER = "\n"
KEY = "custom-acme-small"


class ModelsCase(test_cli.OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        for name in consent.SESSION_MARKERS:
            self.runtime.environ.pop(name, None)
        guard = mock.patch.object(consent, "stdio_ttys", return_value=True)
        guard.start()
        self.addCleanup(guard.stop)

    def screen(self, key: str | None = KEY) -> models_screen._ModelsScreen:
        screen = models_screen._ModelsScreen(self.runtime, palette=tui.MONO_PALETTE)
        if key is not None:
            screen.index = screen.keys.index(key)
        return screen

    def run_screen(self, screen, keys, *, height=24, width=80) -> FakeWindow:
        win = FakeWindow([*keys, ESC], height=height, width=width)
        screen.run(win)
        return win

    def declaration(self) -> dict:
        return json.loads((self.pdir / "acme.json").read_text())

    def reference(self, name: str = "uses-acme") -> None:
        """A profile of yours whose reviewer is the model you added."""

        document = copy.deepcopy(self.runtime.profiles.load("direct"))
        document["name"] = name
        document.pop("seed", None)
        document["agents"] = {"cm-reviewer": {"model": KEY, "effort": "high"}}
        self.runtime.profiles.new(document)


class RemoveTests(ModelsCase):
    def setUp(self) -> None:
        super().setUp()
        self.declare_small()
        self.serve_current()

    def test_a_shipped_model_is_not_removed(self) -> None:
        shipped = next(iter(self.runtime.catalog.docs["models-v2"]["models"]))
        screen = self.screen(shipped)
        before = self.state_bytes()
        self.run_screen(screen, ["x"])
        self.assertEqual(screen.message, cli_text.MODELS_X_CATALOG)
        self.assertEqual(self.state_bytes(), before)

    def test_an_unused_model_is_removed_after_a_confirmation(self) -> None:
        screen = self.screen()
        win = self.run_screen(screen, ["X", ENTER])
        self.assertTrue(any(cli_text.REMOVE_LINE_TITLE.format(key=KEY) in frame for frame in win.frames))
        self.assertEqual(screen.message, model.NOTHING_CHANGED)
        self.assertIn(KEY, self.declaration()["lines"])
        self.run_screen(screen, ["x", RIGHT, ENTER])
        self.assertNotIn(KEY, self.declaration()["lines"])
        self.assertIn(KEY, self.ledger().removed)

    def test_where_profiles_use_it_a_replacement_is_chosen_first(self) -> None:
        from claude_multi.setup import lines as setup_lines

        self.reference()
        plan = setup_lines.plan_line_remove(self.runtime, KEY)
        self.assertEqual(len(plan.references), 1)
        self.assertTrue(plan.candidates)
        successor = plan.candidates[0]
        screen = self.screen()
        # Cancel on the replacement list writes nothing.
        before = self.state_bytes()
        win = self.run_screen(screen, ["X", ESC])
        self.assertTrue(any(cli_text.SUCCESSOR_TITLE.format(key=KEY, n=1) in frame for frame in win.frames))
        self.assertEqual(self.state_bytes(), before)
        # A no at the review writes nothing either.
        self.run_screen(screen, ["X", ENTER, "n"])
        self.assertEqual(screen.message, model.NOTHING_CHANGED)
        self.assertEqual(self.state_bytes(), before)
        win = self.run_screen(screen, ["X", ENTER, "y"])
        self.assertTrue(any(cli_text.SUCCESSOR_CONFIRM_TITLE.format(key=KEY) in frame for frame in win.frames))
        self.assertNotIn(KEY, self.declaration()["lines"])
        rewritten = self.runtime.profiles.load("uses-acme")
        self.assertEqual(rewritten["agents"]["cm-reviewer"]["model"], successor)


class ApproveTests(ModelsCase):
    def setUp(self) -> None:
        super().setUp()
        code, _out, err = self.op([*test_cli.ACME_ADD, "--declare-only"])
        self.assertEqual(code, 0, err)
        code, _out, err = self.op(test_cli.SMALL_ADD)
        self.assertEqual(code, 0, err)

    def test_enter_on_an_unapproved_route_still_changes_only_the_badge(self) -> None:
        screen = self.screen()
        self.assertEqual(screen._row_kind(KEY, screen.model(80)), "new")
        with mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("inference")), \
                mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("inference")), \
                mock.patch.object(providers_screen.ConnectActions, "approve", side_effect=AssertionError("route approval")), \
                mock.patch.object(screens_common.OnboardingActions, "show"):
            win = self.run_screen(screen, [ENTER, "y"])
        self.assertIn("Enter admit badge", win.frames[0])
        self.assertIn("Providers", " ".join(screen.model(80).details[KEY]))
        self.assertNotIn("acme", getattr(self.ledger(), "routes", {}))
        self.assertIn(KEY, self.ledger().admissions)
        self.assertEqual(screen._row_kind(KEY, screen.model(80)), "admitted")
        self.assertFalse(next(row.offered for row in screen.line_rows if row.key == KEY))
        with mock.patch.object(screens_common.OnboardingActions, "show"):
            self.run_screen(screen, [ENTER, "y"])
        self.assertNotIn(KEY, self.ledger().admissions)
        self.assertNotIn("acme", self.ledger().routes)
        self.assertEqual(self.http_calls, [])


class AdmitTests(ModelsCase):
    def setUp(self) -> None:
        super().setUp()
        self.declare_small()
        self.shown: list[tuple[str, list[str]]] = []
        patches = (
            mock.patch.object(screens_common.OnboardingActions, "confirm", lambda _self, text: True),
            mock.patch.object(screens_common.OnboardingActions, "show",
                              lambda _self, title, lines: self.shown.append((title, list(lines)))),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def admitted(self) -> bool:
        from claude_multi.cli.commands import providers as provider_commands

        return KEY in provider_commands.admitted_keys(self.runtime)

    def test_an_unserved_admission_is_metadata_only_without_apply_or_retry(self) -> None:
        screen = self.screen()
        with mock.patch.object(providers_screen.ConnectActions, "apply", side_effect=AssertionError("apply")), \
                mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("inference")), \
                mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("inference")):
            self.run_screen(screen, [ENTER])
            self.assertTrue(self.admitted())
            self.run_screen(screen, [ENTER])
        self.assertFalse(self.admitted())
        self.assertEqual(self.http_calls, [])
        self.assertEqual([title for title, _lines in self.shown], ["Action result", "Action result"])

    def test_an_admission_failure_never_offers_apply_and_retry(self) -> None:
        import claude_multi.cli.onboarding as onboarding

        def refused(_runtime, _argv, *, confirm, output):
            output.write("0/1 aliases served — claude-multi providers apply")
            return 1

        with mock.patch.object(onboarding, "invoke", side_effect=refused) as invoke, \
                mock.patch.object(providers_screen.ConnectActions, "apply", side_effect=AssertionError("apply")):
            self.run_screen(self.screen(), [ENTER])
        self.assertEqual(invoke.call_count, 1)
        self.assertFalse(self.admitted())

    def test_a_served_admission_needs_no_apply(self) -> None:
        self.serve_current()
        with mock.patch.object(providers_screen.ConnectActions, "apply",
                               side_effect=AssertionError("nothing to apply")):
            self.run_screen(self.screen(), [ENTER])
        self.assertTrue(self.admitted())


class ContinueTests(ModelsCase):
    def test_skip_admit_after_manual_declaration_names_ordinary_profile_selection(self) -> None:
        from test_tui import DOWN
        from claude_multi import profile

        code, _out, err = self.op(test_cli.ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        keys = [DOWN, ENTER, *"acme-small-1", ENTER, *KEY, ENTER, *"131072", ENTER,
                ENTER, *"fixture docs, date", ENTER, *([ENTER] * 6), ENTER, ESC, ENTER]
        win = FakeWindow(keys, height=30, width=90)
        actions = providers_screen.ConnectActions(self.runtime, win, tui.MONO_PALETTE)
        with mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("inference")), \
                mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("inference")):
            summary = actions.continue_to_models("acme")
        self.assertIn("not admitted", summary)
        self.assertIn("Profiles", summary)
        self.assertIn("Optional diagnostics", summary)
        self.assertNotIn(KEY, self.ledger().admissions)
        self.assertIn(KEY, self.runtime.lineup_catalog().lines)
        self.assertEqual(self.http_calls, [])
        result = profile.evaluate(profile.ad_hoc_direct(KEY), self.runtime.lineup_catalog(),
                                  effective=self.runtime.current_effective(), ad_hoc=True)
        self.assertFalse(result.errors, result.errors)


class LetterTests(ModelsCase):
    def setUp(self) -> None:
        super().setUp()
        self.declare_small()
        self.serve_current()

    def test_keybars_name_the_actions_of_each_row(self) -> None:
        win = self.run_screen(self.screen(), [])
        bar = " ".join(win.frames[0].splitlines()[-2:])
        for key, label in cli_text.MODELS_KEYBAR_NEW[:-1]:
            self.assertIn(f"{key} {label}", bar)

    def test_e_persists_a_nonaggregator_family_and_empty_role_recommendations(self) -> None:
        self.assertNotIn("family", self.declaration()["lines"][KEY])
        code, _out, err = self.op(["models", "admit", KEY], "y\n")
        self.assertEqual(code, 0, err)
        screen = self.screen()
        keys = ["E", *([ENTER] * 5), *(["\x7f"] * len("acme")), *"Mistral Labs", ENTER,
                ENTER, ENTER, ENTER, RIGHT, ENTER, ENTER, ENTER, "y"]
        with mock.patch.object(screens_common.OnboardingActions, "show"), \
                mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("inference")), \
                mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("inference")):
            self.run_screen(screen, keys, height=30, width=90)
        saved = self.declaration()["lines"][KEY]
        self.assertEqual(saved["family"], "Mistral Labs")
        self.assertEqual(saved["capabilities"], ["lead", "agents"])
        self.assertEqual(saved["roles"], [])
        details = " ".join(screen.model(90).details[KEY])
        self.assertIn("Attention: changed since admission", details)
        self.assertEqual(screen._row_kind(KEY, screen.model(90)), "new")
        self.assertTrue(next(row.offered for row in screen.line_rows if row.key == KEY))
        self.assertEqual(self.http_calls, [])

    def test_q_defaults_to_no_and_deferring_sends_no_inference(self) -> None:
        before = self.state_bytes()
        with mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("inference")), \
                mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("inference")), \
                mock.patch.object(screens_common.OnboardingActions, "show"):
            self.run_screen(self.screen(), ["Q", ESC])
            win = self.run_screen(self.screen(), ["Q", RIGHT, ENTER, ENTER, ENTER, ENTER, "n"])
        self.assertTrue(any("Confirm explicit action" in frame for frame in win.frames))
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.http_calls, [])

    def test_narrow_screen_keeps_badge_and_diagnostic_actions_accessible(self) -> None:
        screen = self.screen()
        height, _cols = screen.min_size(60)
        win = self.run_screen(screen, [], height=max(24, height), width=60)
        self.assertNotIn("too small", win.frames[0])
        self.assertIn("Enter admit badge", win.frames[0])
        self.assertIn("Q diagnostics", win.frames[0])

    def test_both_letter_cases(self) -> None:
        for letter in ("v", "V"):
            with self.subTest(letter=letter):
                with mock.patch.object(models_screen._ModelsScreen, "_details") as details:
                    self.run_screen(self.screen(), [letter])
                details.assert_called_once()
        for letter in ("x", "X"):
            with self.subTest(letter=letter):
                with mock.patch.object(models_screen._ModelsScreen, "_remove") as remove:
                    self.run_screen(self.screen(), [letter])
                remove.assert_called_once()
        for letter in ("e", "E"):
            with self.subTest(letter=letter):
                with mock.patch.object(screens_common.OnboardingActions, "model_form") as form:
                    self.run_screen(self.screen(), [letter])
                form.assert_called_once()
        for letter in ("q", "Q"):
            with self.subTest(letter=letter):
                with mock.patch.object(screens_common.OnboardingActions, "qualify") as qualify:
                    self.run_screen(self.screen(), [letter])
                qualify.assert_called_once_with(KEY)


if __name__ == "__main__":
    unittest.main()
