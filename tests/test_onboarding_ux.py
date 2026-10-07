"""Draft cursor, model naming and explicit family regressions; fixture-only."""
from __future__ import annotations

import curses
import json
import unicodedata
import unittest
from unittest import mock

from claude_multi import discovery, tui, views
from claude_multi.cli import consent
from claude_multi.cli.screens import common
import test_discovery
import test_onboarding_isolation as journeys
from test_tui import DELETE, END, ENTER, ESC, HOME, LEFT, FakeWindow


class CellWindow(FakeWindow):
    """Model curses' narrow, wide and combining cells for input assertions."""

    def addstr(self, y, x, text, attr=0):
        previous = None
        for char in text:
            if unicodedata.category(char) in ("Mn", "Me"):
                if previous is None:
                    raise AssertionError("unattached combining mark")
                base, _ = self.grid[(y, previous)]
                self.grid[(y, previous)] = (base + char, attr)
                continue
            cells = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            super().addstr(y, x, char + " " * (cells - 1), attr)
            previous, x = x, x + cells


class CursorTests(unittest.TestCase):
    def form(self, value="abcd", **kwargs):
        return tui.OnboardingForm("draft", (("text", "Text", value, ()),), **kwargs)

    def draw(self, form, *, width=50):
        win = FakeWindow(height=24, width=width)
        with mock.patch.object(tui, "show_cursor") as shown:
            form._draw(win)
        shown.assert_called_once()
        self.assertIsNotNone(win.cursor_pos)
        y, x = win.cursor_pos
        self.assertTrue(win.attr_at(y, x) & curses.A_REVERSE)
        return win

    def test_movement_and_edits_follow_the_highlight_and_hardware_cursor(self):
        form = self.form()
        field = form.inputs["text"]
        for key, value, cursor in (("left", "abcd", 3), ("home", "abcd", 0),
                                   ("right", "abcd", 1), ("delete", "acd", 1),
                                   ("backspace", "cd", 0), ("end", "cd", 2)):
            with self.subTest(key=key):
                field.handle(tui.Key(key))
                win = self.draw(form)
                self.assertEqual(field.value, value)
                self.assertEqual(win.cursor_pos, (6, 2 + cursor))
                self.assertEqual(win.grid[win.cursor_pos][0], value[cursor:cursor + 1] or "_")
                highlighted = [x for (y, x), (_, attr) in win.grid.items()
                               if y == 6 and attr & curses.A_REVERSE]
                self.assertEqual(highlighted, [2 + cursor])
        field.handle(tui.Key("home"))
        field.handle(tui.Key("char", "X"))
        win = self.draw(form)
        self.assertEqual(field.value, "Xcd")
        self.assertEqual(win.cursor_pos, (6, 3))
        self.assertEqual(win.grid[win.cursor_pos][0], "c")

    def test_full_and_long_fields_have_a_real_end_insertion_cell(self):
        for value in ("x" * 46, "prefix/" + "0123456789" * 12):
            with self.subTest(length=len(value)):
                form = self.form(value)
                win = self.draw(form)
                field = form.inputs["text"]
                self.assertGreater(field.offset, 0)
                self.assertLess(win.cursor_pos[1], 48)
                self.assertEqual(win.grid[win.cursor_pos][0], "_")
                self.assertNotIn("…", win.line(6))
                field.handle(tui.Key("char", "Z"))
                win = self.draw(form)
                self.assertIn("Z_", win.line(6))
                field.handle(tui.Key("home"))
                win = self.draw(form)
                self.assertEqual(field.offset, 0)
                self.assertEqual(win.cursor_pos, (6, 2))
                self.assertTrue(win.line(6).startswith("  " + value[:10]))
                field.handle(tui.Key("end"))
                win = self.draw(form, width=80)
                self.assertEqual(win.grid[win.cursor_pos][0], "_")

    def test_text_input_end_cursor_points_at_blank_not_last_character(self):
        field = tui.TextInput("x" * 20)
        win = FakeWindow()
        position = field.draw(win, 0, 0, 12, tui.MONO_PALETTE, focused=True)
        self.assertEqual(position, (0, 10))
        self.assertEqual(win.grid[position][0], " ")
        self.assertEqual(win.line(0)[11], "]")

    def test_unicode_cell_positions_and_combining_edits(self):
        form = self.form("é界éZ")
        field = form.inputs["text"]
        # Narrow, wide and combining display cells, not len(value).
        with mock.patch.object(tui, "show_cursor"):
            win = CellWindow()
            form._draw(win)
        self.assertEqual(win.cursor_pos, (6, 7))
        self.assertEqual(win.grid[win.cursor_pos][0], "_")
        for key, index, column, glyph in (("left", 4, 6, "Z"), ("left", 2, 5, "é"),
                                           ("left", 1, 3, "界"), ("right", 2, 5, "é")):
            field.handle(tui.Key(key))
            with mock.patch.object(tui, "show_cursor"):
                form._draw(win)
            self.assertEqual((field.cursor, win.cursor_pos), (index, (6, column)))
            self.assertEqual(win.grid[win.cursor_pos][0], glyph)
            self.assertTrue(win.attr_at(*win.cursor_pos) & curses.A_REVERSE)
        field.handle(tui.Key("delete"))
        self.assertEqual(field.value, "é界Z")
        field.handle(tui.Key("backspace"))
        self.assertEqual(field.value, "éZ")
        # No half-wide glyph at either edge; reserve an end insertion cell.
        field = tui.TextInput("界" * 30)
        self.assertEqual(field._visible(6), "界" * 2)
        self.assertEqual(field.offset, 28)
        field.handle(tui.Key("home"))
        self.assertEqual(field._visible(6), "界" * 3)
        self.assertEqual(field.offset, 0)
        for value, column in (("界" * 30, 46), ("́A", 4), ("é" * 60, 47)):
            with self.subTest(value=value), mock.patch.object(tui, "show_cursor"):
                win = CellWindow(width=50)
                self.form(value)._draw(win)
                self.assertEqual(win.cursor_pos, (6, column))
                self.assertEqual(win.grid[win.cursor_pos][0], "_")

    def test_highlight_survives_an_unsupported_hardware_cursor(self):
        win = FakeWindow()
        with mock.patch.object(tui.curses, "curs_set", side_effect=tui.CursesError):
            self.form()._draw(win)
        self.assertEqual(win.cursor_pos, (6, 6))
        self.assertEqual(win.grid[win.cursor_pos][0], "_")
        self.assertTrue(win.attr_at(*win.cursor_pos) & curses.A_REVERSE)

    def test_choice_help_never_shows_the_edit_cursor(self):
        form = tui.OnboardingForm("choice", (("mode", "Mode", "one", (("one", "One"),)),),
                                  help_text="Choice help")
        win = FakeWindow(["?", ESC, ENTER])
        with mock.patch.object(tui.curses, "curs_set") as cursor:
            self.assertEqual(form.run(win), {"mode": "one"})
        self.assertIn("Choice help", "\n".join(win.frames))
        self.assertNotIn(mock.call(1), cursor.call_args_list)

    def test_choice_hidden_and_too_small_steps_hide_the_cursor(self):
        choice = tui.OnboardingForm("choice", (("mode", "Mode", "one", (("one", "One"),)),))
        hidden = self.form("fixture-pasted-value", shown={"text": lambda _: False})
        for form, width in ((choice, 50), (hidden, 50), (self.form(), 30)):
            with self.subTest(width=width, title=form.title), \
                    mock.patch.object(tui, "show_cursor") as shown, \
                    mock.patch.object(tui, "hide_cursor") as hide:
                win = FakeWindow(width=width)
                form._draw(win)
                shown.assert_not_called()
                hide.assert_called()
                self.assertIsNone(win.cursor_pos)
                self.assertNotIn("fixture-pasted-value", win.text())

    def test_help_resize_and_every_exit_leave_no_cursor(self):
        for keys, expected in (([ENTER], {"text": "abcd"}), ([ESC], None), (["\x03"], None),
                               ([curses.KEY_F1, ESC, LEFT, ENTER], {"text": "abcd"})):
            with self.subTest(keys=keys), mock.patch.object(tui.curses, "curs_set") as cursor:
                form = self.form(help_text="Help with drafting")
                self.assertEqual(form.run(FakeWindow(keys)), expected)
                self.assertEqual(cursor.call_args.args, (0,))
                if curses.KEY_F1 in keys:
                    self.assertIn(mock.call(0), cursor.call_args_list[2:-1])
        form = self.form()
        win = FakeWindow([curses.KEY_RESIZE, ENTER, curses.KEY_RESIZE, ENTER])
        read = win.get_wch
        sizes = iter((5, 5, 24, 24))
        def resize():
            win.height = next(sizes)
            return read()
        with mock.patch.object(win, "get_wch", side_effect=resize), \
                mock.patch.object(tui.curses, "curs_set") as cursor:
            self.assertEqual(form.run(win), {"text": "abcd"})
            self.assertIn("Terminal too small", "\n".join(win.frames))
            self.assertEqual(cursor.call_args.args, (0,))
        with mock.patch.object(tui, "read_key", side_effect=RuntimeError("input failed")), \
                mock.patch.object(tui, "hide_cursor") as hide:
            with self.assertRaises(RuntimeError):
                self.form().run(FakeWindow())
            hide.assert_called()

    def test_refused_hidden_value_is_cleared_without_rendering_it(self):
        form = self.form("fixture-pasted-value", checks={"text": lambda text: "Bad name" if text else None},
                         shown={"text": lambda text: not text})
        win = FakeWindow([ENTER, ENTER])
        with mock.patch.object(tui.curses, "curs_set") as cursor:
            self.assertEqual(form.run(win), {"text": ""})
        self.assertNotIn("fixture-pasted-value", "\n".join(win.frames))
        self.assertIn("Bad name", "\n".join(win.frames))
        self.assertEqual(cursor.call_args.args, (0,))


class ModelNamingTests(journeys.JourneyFixture):
    def action(self):
        action = common.OnboardingActions(self.runtime, FakeWindow([]), tui.MONO_PALETTE)
        action.show = mock.Mock()
        action.preview = mock.Mock(return_value=True)
        action.confirm = mock.Mock(return_value=True)
        return action

    def values(self, line, key=""):
        return {name: str(default) for name, _, default, _ in views.model_form_fields(line, key=key)}

    def run_form(self, action, line, values, key="", *, edit=False):
        with mock.patch.object(tui.OnboardingForm, "run", return_value=values), \
                mock.patch.object(consent, "stdio_ttys", return_value=True):
            action.model_form("openrouter", line, key, edit=edit)

    def test_model_stepper_edits_the_display_and_cancels_without_writes(self):
        line = self.line()
        fields = views.model_form_fields(line)
        action = self.action()
        action.win = FakeWindow([ENTER, HOME, DELETE, "M", END, "!", ENTER, *([ENTER] * (len(fields) - 2))])
        with mock.patch.object(tui.curses, "curs_set") as cursor, \
                mock.patch.object(consent, "stdio_ttys", return_value=True):
            action.model_form("openrouter", line)
        self.assertIn("declared custom-", str(action.show.call_args))
        raw = json.loads((self.pdir / "openrouter.json").read_text())["lines"]
        self.assertEqual(next(iter(raw.values()))["display"], "Mixture Reviewer!")
        self.assertEqual(cursor.call_args.args, (0,))
        before = self.state_bytes()
        action.preview.reset_mock()
        action.win = FakeWindow([ENTER, HOME, "X", ESC])
        action.model_form("openrouter", line)
        action.preview.assert_not_called()
        self.assertEqual(self.state_bytes(), before)

    def test_derivation_avoids_retired_generation_base_keys(self):
        line = self.line()
        base = discovery.derived_key(line["wire_model"], ())
        lcat = self.runtime.lineup_catalog()
        view = mock.Mock(providers=lcat.providers, lines=lcat.lines, retired={base + "@1": {}})
        action = self.action()
        action.preview.return_value = False
        before = self.state_bytes()
        with mock.patch.object(self.runtime, "lineup_catalog", return_value=view):
            self.run_form(action, line, self.values(line))
        self.assertIn(base + "-2", action.preview.call_args.args[0])
        self.assertEqual(self.state_bytes(), before)

    def test_display_defaults_to_listing_name_or_wire(self):
        for line, expected in ((self.line(), "Fixture Reviewer"), ({"wire_model": "vendor/model"}, "vendor/model"),
                               ({}, "")):
            self.assertEqual(self.values(line)["display"], expected)

    def test_display_persists_and_blank_key_is_derived_and_previewed(self):
        line = self.line()
        values = self.values(line)
        values["display"] = "My chosen display"
        action = self.action()
        self.run_form(action, line, values)
        key = discovery.derived_key(line["wire_model"], ())
        declared = json.loads((self.pdir / "openrouter.json").read_text())["lines"]
        self.assertEqual(list(declared), [key])
        self.assertEqual(declared[key]["display"], "My chosen display")
        self.assertIn('"' + key + '"', action.preview.call_args.args[0])
        self.assertIn('"display": "My chosen display"', action.preview.call_args.args[0])
        self.assertEqual(self.listing_calls, [])

    def test_blank_display_defaults_to_the_entered_wire(self):
        line = self.line()
        values = self.values(line)
        values["display"] = ""
        action = self.action()
        self.run_form(action, line, values)
        declaration = json.loads((self.pdir / "openrouter.json").read_text())["lines"]
        self.assertEqual(next(iter(declaration.values()))["display"], line["wire_model"])

    def test_derived_key_collision_suffix_and_explicit_collision_refusal(self):
        line = self.line()
        base = discovery.derived_key(line["wire_model"], ())
        self.run_form(self.action(), line, self.values(line, base))
        second = dict(line, wire_model="vendor.fixture-reviewer")
        action = self.action()
        self.run_form(action, second, self.values(second))
        declared = json.loads((self.pdir / "openrouter.json").read_text())["lines"]
        self.assertEqual(set(declared), {base, base + "-2"})
        self.assertIn(base + "-2", action.preview.call_args.args[0])
        before = self.state_bytes()
        self.run_form(action, dict(line, wire_model="another-wire"), self.values(line, base))
        self.assertEqual(self.state_bytes(), before)
        self.assertIn("already declared", str(action.show.call_args))

    def test_invalid_derivation_and_cancel_write_nothing(self):
        before = self.state_bytes()
        action = self.action()
        line = dict(self.line(), wire_model="///")
        self.run_form(action, line, self.values(line))
        action.preview.assert_not_called()
        self.assertIn("no valid key derives", str(action.show.call_args))
        self.run_form(action, self.line(), None)
        self.assertEqual(self.state_bytes(), before)
        action.preview.return_value = False
        self.run_form(action, self.line(), self.values(self.line()))
        self.assertEqual(self.state_bytes(), before)

    def test_edit_has_no_key_field_and_persists_display_without_renaming(self):
        key = self.declare()
        line = self.line()
        fields = views.model_form_fields(line, key=key, edit=True)
        self.assertNotIn("key", [name for name, *_ in fields])
        values = {name: str(default) for name, _, default, _ in fields}
        values["display"] = "Renamed display"
        action = self.action()
        self.run_form(action, line, values, key, edit=True)
        declared = json.loads((self.pdir / "openrouter.json").read_text())["lines"]
        self.assertEqual(set(declared), {key})
        self.assertEqual(declared[key]["display"], "Renamed display")
        self.assertIn(key, action.preview.call_args.args[0])
        before = self.state_bytes()
        values["key"] = "custom-other"
        self.run_form(action, line, values, key, edit=True)
        self.assertEqual(self.state_bytes(), before)
        self.assertIn("Edit keeps the local key", str(action.show.call_args))


class ModelFamilyTests(journeys.JourneyFixture):
    def test_explicit_family_overrides_default_and_omission_inherits(self):
        for provider_id, expected in (("openrouter", "unknown"), ("kimi", "moonshot")):
            for number, explicit in enumerate((None, "Xiaomi GLM", "模型")):
                wire = f"fixture-{provider_id}-{number}"
                argv = ["models", "add", provider_id, wire, "--context", "200000", "--source", "operator"]
                if explicit is not None:
                    argv += ["--family", explicit]
                code, out = self.invoke(argv)
                self.assertEqual(code, 0, out)
                line = next(line for line in self.runtime.operator_snapshot().layer.lines.values()
                            if line.core_entry["wire_model"] == wire)
                self.assertEqual(line.family, explicit or expected)
                raw = json.loads((self.pdir / f"{provider_id}.json").read_text())["lines"][line.key]
                self.assertEqual(raw.get("family"), explicit)

    def test_malformed_family_uses_declaration_validator_and_writes_nothing(self):
        before = self.state_bytes()
        for label in ("", "x" * 65, "two\nlines", "tab\tlabel", "bad\x1bvalue"):
            with self.subTest(label=repr(label)):
                code, _, err = self.op(["models", "add", "openrouter", "fixture-family", "--context", "200000",
                                        "--source", "operator", "--family", label])
                self.assertEqual(code, 1, err)
                self.assertIn("family", err)
                self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.listing_calls, [])


class DiscoverFamilyTests(test_discovery.DiscoveryCLICase):
    def test_explicit_family_overrides_inherited_default_and_stealth_unknown(self):
        self.approve_acme()
        url = "https://api.acme.example/anthropic/v1/models"
        self.listing(url, [{"id": "acme-family", "context_length": 200000}])
        code, out, err = self.op(["discover", "acme", "--add", "acme-family", "--family", "Xiaomi GLM"], "y\n")
        self.assertEqual(code, 0, out + err)
        line = self.runtime.operator_snapshot().layer.lines["custom-acme-family"]
        self.assertEqual(line.family, "Xiaomi GLM")
        wire = "stealth/fixture-hidden-maker"
        self.listing(discovery.OPENROUTER_MODELS_URL + "/" + wire + "/endpoints", {
            "id": wire, "context_length": 200000})
        # Single-model endpoint failure also permits explicit sourced manual facts.
        code, out, err = self.op(["discover", "openrouter", "--add", wire, "--context", "200000",
                                  "--family", "Declared maker"], "n\n")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.runtime.operator_snapshot().layer.lines["custom-stealth-fixture-hidden-maker"].family,
                         "Declared maker")

    def test_invalid_nonadd_and_multiadd_are_refused_before_guard_or_network(self):
        cases = [( ["discover", "kimi", "--family", "maker"], "need --add"),
                 (["discover", "--all", "--family", "maker"], "need a PROVIDER"),
                 (["discover", "--feed", "--family", "maker"], "need a PROVIDER"),
                 (["discover", "kimi", "--add", "one", "two", "--family", "maker"], "exactly one")]
        cases += [(["discover", "kimi", "--add", "one", "--family", label], "printable")
                  for label in ("", "x" * 65, "two\nlines", "tab\tlabel", "bad\x1bvalue")]
        before = self.state_bytes()
        for argv, reason in cases:
            with self.subTest(argv=argv), mock.patch.object(consent, "require_human", side_effect=AssertionError("guard")):
                code, out, err = self.op(argv, "y\n", tty=False)
                self.assertEqual(code, 2, out + err)
                self.assertIn(reason, err)
                self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.sent, [])

    def test_omitted_family_preserves_inheritance_and_batch_add(self):
        self.approve_acme()
        url = "https://api.acme.example/anthropic/v1/models"
        self.listing(url, [{"id": wire, "context_length": 200000} for wire in ("one", "two")])
        code, out, err = self.op(["discover", "acme", "--add", "one", "two"], "y\n")
        self.assertEqual(code, 0, out + err)
        for key in ("custom-one", "custom-two"):
            self.assertEqual(self.runtime.operator_snapshot().layer.lines[key].family, "acme")
            raw = json.loads((self.pdir / "acme.json").read_text())["lines"][key]
            self.assertNotIn("family", raw)


if __name__ == "__main__":
    unittest.main()
