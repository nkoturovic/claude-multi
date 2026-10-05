"""The Models screen's actions on models you added: Enter on a model whose
provider route is not approved offers Approve now, X removes a model (a
shipped one is refused; where profiles use it a replacement is chosen
first), an admission the running gateway cannot serve offers Apply and
retry, and keys in either letter case. A fixture gateway on a temp home;
no provider request is sent."""

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

    def test_enter_on_an_unapproved_route_offers_approve_now(self) -> None:
        screen = self.screen()
        self.assertEqual(screen.lifecycle[KEY], models_screen.ROUTE_UNAPPROVED)
        win = self.run_screen(screen, [ENTER, ENTER])
        modal = next(frame for frame in win.frames if cli_text.MODELS_APPROVE_TITLE in frame)
        self.assertIn("[ Approve now ]", modal)
        self.assertIn("Enter approve route", win.frames[0])
        self.assertNotIn("acme", getattr(self.ledger(), "routes", {}))
        # Approve now, then the route approval itself.
        win = self.run_screen(screen, [ENTER, RIGHT, ENTER, RIGHT, ENTER])
        self.assertTrue(any(cli_text.APPROVE_TITLE.format(id="acme") in frame for frame in win.frames))
        self.assertIn("acme", self.ledger().routes)
        self.assertNotEqual(screen.lifecycle.get(KEY), models_screen.ROUTE_UNAPPROVED)


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

    def test_an_unserved_admission_offers_apply_and_retry(self) -> None:
        applied: list[str] = []

        def apply(_self, *, not_served=False):
            # The admission saw the model not served: the apply must run.
            applied.append(f"apply not_served={not_served}")
            self.serve_current()
            return providers_screen.Outcome("applied")

        screen = self.screen()
        # Cancel keeps the failure on screen and changes nothing.
        win = self.run_screen(screen, [ENTER, ENTER])
        self.assertTrue(any(cli_text.ADMIT_APPLY_TITLE in frame for frame in win.frames))
        self.assertEqual(self.shown[-1][0], "Action result")
        self.assertFalse(self.admitted())
        with mock.patch.object(providers_screen.ConnectActions, "apply", apply):
            self.run_screen(screen, [ENTER, RIGHT, ENTER])
        self.assertEqual(applied, ["apply not_served=True"])
        self.assertTrue(self.admitted())

    def test_a_served_admission_needs_no_apply(self) -> None:
        self.serve_current()
        with mock.patch.object(providers_screen.ConnectActions, "apply",
                               side_effect=AssertionError("nothing to apply")):
            self.run_screen(self.screen(), [ENTER])
        self.assertTrue(self.admitted())


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
