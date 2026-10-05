"""Grouped by provider: the binding picker, the provider cell of the card
and the profile editor (``provider·family`` for a model an aggregator
serves), Direct's order, and the provider picker of Get started. Every id
and display comes from the loaded fixture catalog."""

from __future__ import annotations

import contextlib
import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _tui_fixture as fx
import test_cli
from test_tui import DOWN, END, HOME, UP, FakeWindow
from claude_multi import catalog, cli, profile, tui, views
from claude_multi.setup import providers as setup_providers
import claude_multi.cli.consent as consent
import claude_multi.cli.screens.get_started as get_started
import claude_multi.cli.text as cli_text
import claude_multi.cli.types as types

ESC = "\x1b"
ENTER = "\n"


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="cm-picker-by-provider-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.runtime = fx.fixture_runtime(tmp / "root")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(fx.hermetic(self.runtime))
        self.lcat = self.runtime.lineup_catalog()
        self.eff = self.runtime.current_effective()
        self.rows = views.line_rows(self.lcat, self.eff, custom_ids=frozenset())

    def aggregator_row(self) -> views.LineRow:
        """A line whose model family is not its provider's own family."""

        for row in self.rows:
            own = self.lcat.providers[row.provider].get("independence_family")
            if row.family and own is not None and row.family != own:
                return row
        raise AssertionError("the fixture has a line an aggregator serves")

    def own_family_row(self) -> views.LineRow:
        return next(row for row in self.rows
                    if row.family == self.lcat.providers[row.provider].get("independence_family"))


class BindingPickerTests(FixtureCase):
    def test_groups_follow_the_provider_display_name(self) -> None:
        for slot in (catalog.LEAD_ROLE, next(iter(catalog.AGENT_ROLE_IDS))):
            with self.subTest(slot=slot):
                model = views.picker_rows(self.rows, slot=slot, bindings={}, lcat=self.lcat, eff=self.eff,
                                          current=None)
                lines = [item for item in model.items if item.kind == "line"]
                self.assertTrue(lines)
                order = [self.lcat.lines[item.key]["provider"] for item in lines]
                groups = list(dict.fromkeys(order))
                self.assertEqual(groups, sorted(groups, key=lambda pid: (
                    str(self.lcat.providers[pid].get("display", pid)).lower(), pid)))
                self.assertEqual(order, sorted(order, key=groups.index), "each provider's models are together")
                for index, item in enumerate(lines):
                    first = index == 0 or order[index - 1] != order[index]
                    self.assertEqual(item.text[:11].strip(), order[index] if first else "", item.text)

    def test_the_family_column_follows_the_model(self) -> None:
        row = self.aggregator_row()
        model = views.picker_rows(self.rows, slot=catalog.LEAD_ROLE if row.lead_capable else row.key,
                                  bindings={}, lcat=self.lcat, eff=self.eff, current=None)
        item = next((i for i in model.items if i.key == row.key), None)
        if item is None:  # not offered for the lead: any slot that admits it
            slot = next(rid for rid in catalog.AGENT_ROLE_IDS if row.admits(rid))
            model = views.picker_rows(self.rows, slot=slot, bindings={}, lcat=self.lcat, eff=self.eff, current=None)
            item = next(i for i in model.items if i.key == row.key)
        self.assertIn(f" {row.family:<9} ", item.text)


class ProviderCellTests(FixtureCase):
    def test_an_aggregator_names_the_model_family(self) -> None:
        row = self.aggregator_row()
        self.assertEqual(views.provider_cell(row.provider, row.family, self.lcat.providers),
                         f"{row.provider}·{row.family}")
        own = self.own_family_row()
        self.assertEqual(views.provider_cell(own.provider, own.family, self.lcat.providers), own.provider)
        self.assertEqual(views.provider_cell("not-a-provider", "x", self.lcat.providers), "not-a-provider")
        self.assertEqual(views.provider_cell(own.provider, "", self.lcat.providers), own.provider)

    def test_the_card_and_the_editor_show_the_provider_cell(self) -> None:
        row = self.aggregator_row()
        slot = next(rid for rid in catalog.AGENT_ROLE_IDS if row.admits(rid))
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document["name"] = "aggregated"
        document.pop("seed", None)
        document["agents"][slot] = {"model": row.key, "effort": row.default_effort}
        evaluated = profile.evaluate(document, self.lcat, effective=self.eff)
        self.assertIsNotNone(evaluated.lineup, evaluated.errors)
        card = views.card_model(target=types.LaunchTarget("profile", document, "aggregated", True, "aggregated"),
                                lineup=evaluated.lineup, providers=self.lcat.providers)
        header = next(r for r in card.rows if r.kind == "header").cells
        self.assertIn(header, (views.CARD_TABLE_COLUMNS, views.CARD_WINDOW_TABLE_COLUMNS))
        self.assertEqual(header[3], "provider")
        cells = {r.cells[0]: r.cells for r in card.rows if r.kind == "agent"}
        self.assertEqual(cells[profile.label(slot)][3], f"{row.provider}·{row.family}")
        self.runtime.profiles.new(document)
        state = cli._profile_editor_state(self.runtime, "aggregated", "edit")
        texts = [r.text for r in views.editor_rows(state)]
        self.assertTrue(any(f"{row.provider}·{row.family}" in text for text in texts), texts)


class DirectOrderTests(FixtureCase):
    def test_rows_sort_by_provider_then_model_with_a_provider_column(self) -> None:
        rows = views.direct_rows(self.rows, marks={})
        catalog_rows = [r for r in rows if r.source == "catalog"]
        self.assertTrue(catalog_rows)
        displays = {r.key: r.provider_display for r in self.rows}
        names = {r.key: r.display for r in self.rows}
        keys = [(displays[r.key].lower(), names[r.key].lower(), r.key) for r in catalog_rows]
        self.assertEqual(keys, sorted(keys))
        width = max(len(r.provider) for r in catalog_rows)
        for row in catalog_rows:
            self.assertEqual(row.provider_width, width)
            self.assertTrue(row.text().startswith(f"{row.provider:<{width}} {row.short}"), row.text())


class ProviderPickerTests(test_cli.OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        for name in consent.SESSION_MARKERS:
            self.runtime.environ.pop(name, None)
        guard = mock.patch.object(consent, "stdio_ttys", return_value=True)
        guard.start()
        self.addCleanup(guard.stop)

    def picker(self, only=None) -> get_started._ProviderPicker:
        return get_started._ProviderPicker(self.runtime, tui.MONO_PALETTE, only=only)

    def test_groups_headers_and_family(self) -> None:
        entries = setup_providers.picker_entries(self.runtime)
        picker = self.picker()
        headers = [row for row in picker.rows if row.kind == "header"]
        self.assertEqual([row.text for row in headers], list(dict.fromkeys(e.group_title for e in entries)))
        first = entries[0]
        if first.family:
            self.assertEqual(headers[0].right, cli_text.PICKER_FAMILY.format(family=first.family))
        self.assertEqual(picker.rows[picker.selected].entry, first)
        win = FakeWindow([ESC], height=24, width=80)
        picker.run(win)
        frame = win.frames[0]
        self.assertIn(cli_text.PICKER_TITLE, frame)
        self.assertIn("›", next(line for line in frame.splitlines() if first.label in line))
        self.assertIn(" · ".join(f"{k} {v}" for k, v in cli_text.PICKER_KEYBAR), frame)

    def test_arrows_skip_headers_and_end_reaches_the_last_entry(self) -> None:
        picker = self.picker()
        picker.run(FakeWindow([DOWN, DOWN, UP, END, ESC], height=24, width=80))
        self.assertEqual(picker.rows[picker.selected].kind, "entry")
        self.assertEqual(picker.selected, max(i for i, row in enumerate(picker.rows) if row.kind == "entry"))
        picker.run(FakeWindow([HOME, ESC], height=24, width=80))
        self.assertEqual(picker.selected, min(i for i, row in enumerate(picker.rows) if row.kind == "entry"))

    def test_an_unavailable_entry_says_why_and_writes_nothing(self) -> None:
        picker = self.picker()
        index = next((i for i, row in enumerate(picker.rows) if row.entry is not None and not row.entry.available),
                     None)
        if index is None:
            self.skipTest("every fixture entry is available")
        picker.selected = index
        before = self.state_bytes()
        picker.run(FakeWindow([ENTER, ESC], height=24, width=80))
        self.assertEqual(picker.message, picker.rows[index].entry.note or picker.rows[index].entry.state_text)
        self.assertEqual(self.state_bytes(), before)

    def test_your_providers_and_the_manual_form(self) -> None:
        code, _out, err = self.op(test_cli.ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        picker = self.picker()
        self.assertIn(cli_text.PICKER_YOURS, [row.text for row in picker.rows if row.kind == "block"])
        own = next(row for row in picker.rows if row.entry is not None and row.entry.kind == "own")
        self.assertEqual(own.entry.provider_id, "acme")
        other = self.picker(only="other")
        self.assertTrue(all(row.entry.group == "other" for row in other.rows if row.entry is not None))
        self.assertEqual(other.rows[-1].entry.kind, "manual")
        self.assertEqual(other.rows[-1].text, cli_text.ADVANCED_MANUAL)

    def test_enter_on_a_key_entry_opens_the_key_modal(self) -> None:
        picker = self.picker()
        index = next(i for i, row in enumerate(picker.rows)
                     if row.entry is not None and row.entry.kind == "api-key" and row.entry.available
                     and row.entry.provider_id not in ("anthropic", "openai"))
        picker.selected = index
        win = FakeWindow([ENTER, ESC, ESC], height=24, width=80)
        result = picker.run(win)
        display = picker.rows[index].entry.group_title
        self.assertTrue(any(cli_text.KEY_MODAL_TITLE.format(display=display) in frame for frame in win.frames))
        self.assertIsNotNone(result)
        self.assertEqual(picker.rows[picker.selected].entry.id, picker.rows[index].entry.id)


def picker_golden(root: Path) -> str:
    """The provider picker of Get started at 80x24 (tests/goldens/tui)."""

    import _tui_render

    case = ProviderPickerTests()
    case.setUp()
    try:
        win = FakeWindow([ESC], height=_tui_render.HEIGHT, width=_tui_render.WIDTH)
        case.picker().run(win)
        return _tui_render.frame_text(win.frames[0])
    finally:
        case.doCleanups()


if __name__ == "__main__":
    unittest.main()
