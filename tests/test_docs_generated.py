"""The generated documentation: current, complete and deterministic.

``tools/docs_gen.py`` writes the reference tables from the product's own
definitions (the argument parser, the gateway tool's command table, the
``/cm`` verbs, the operation inventory, the interface's key table, the
release catalog and the presets). These tests run its check, so a change to
any of them cannot merge with stale pages, and account for every command
and operation the reference leaves out.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import unittest
from unittest import mock

import _docs_vocab as vocab
from _layout import DOCS_DIR, REPO_ROOT, doc_path, load_tool
from claude_multi import lineup_files, proxy, surface_matrix
from claude_multi.cli import parser as cli_parser

docs_gen = load_tool("docs_gen")


def _page(label: str) -> str:
    return doc_path(label).read_text(encoding="utf-8")


def _region(label: str, name: str) -> str:
    text = _page(label)
    begin, end = docs_gen.BEGIN.format(name=name), docs_gen.END.format(name=name)
    return text.split(begin, 1)[1].split(end, 1)[0]


def _headings(text: str) -> list[str]:
    return [match[1] for match in re.finditer(r"(?m)^#{1,6} (.+)$", text)]


class CurrentTests(unittest.TestCase):
    """The committed pages equal what the generator writes."""

    def test_every_region_is_current(self) -> None:
        self.assertEqual(docs_gen.stale(), [], "run python3 tools/docs_gen.py")

    def test_the_check_mode(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(docs_gen.main(["--check"]), 0)
        self.assertIn(f"{len(docs_gen.REGIONS)} regions, 0 stale", out.getvalue())
        # A region edited by hand is stale, and the check names it.
        original = docs_gen.Path.read_text
        target = DOCS_DIR / "reference" / "tasks.md"

        def edited(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            return text.replace("| launch the profile shown on the card |", "| launch |") if path == target else text

        out = io.StringIO()
        with mock.patch.object(docs_gen.Path, "read_text", edited), contextlib.redirect_stdout(out):
            self.assertEqual(docs_gen.main(["--check"]), 1)
        self.assertIn("stale: reference/tasks.md: tasks", out.getvalue())

    def test_a_definition_change_makes_its_region_stale(self) -> None:
        # The parser's examples, the inventory and the verb table each feed
        # a region: a change to one of them leaves the committed page stale.
        examples = (*cli_parser.HELP_EXAMPLES, ("claude-multi doctor --json", "a report for scripts"))
        with mock.patch.object(cli_parser, "HELP_EXAMPLES", examples):
            self.assertIn("reference/cli.md: cli-reference", docs_gen.stale())
        first = surface_matrix.OPERATIONS[0]
        renamed = (surface_matrix.Op(**{**first.__dict__, "does": "a task that was renamed"}),
                   *surface_matrix.OPERATIONS[1:])
        with mock.patch.object(surface_matrix, "OPERATIONS", renamed):
            self.assertIn("reference/tasks.md: tasks", docs_gen.stale())
        verbs = tuple(lineup_files.CmVerb(verb.name, verb.args, verb.writes, verb.summary + " (changed)")
                      for verb in lineup_files.CM_VERBS)
        with mock.patch.object(lineup_files, "CM_VERBS", verbs):
            self.assertIn("reference/cli.md: cli-reference", docs_gen.stale())

    def test_an_unclassified_argument_stops_the_generator(self) -> None:
        values = dict(docs_gen.VALUES)
        del values[("doctor", "doctor_repair")]
        with mock.patch.object(docs_gen, "VALUES", values), self.assertRaises(SystemExit) as raised:
            docs_gen.region_cli_reference()
        self.assertIn("doctor_repair: no placeholder", str(raised.exception))

    def test_a_region_without_its_generator_is_stale(self) -> None:
        original = docs_gen.Path.read_text
        target = DOCS_DIR / "uninstall.md"

        def extra(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            return text + docs_gen.BEGIN.format(name="nowhere") + "\n" if path == target else text

        with mock.patch.object(docs_gen.Path, "read_text", extra):
            self.assertIn("uninstall.md: nowhere has no generator", docs_gen.stale())

    def test_the_output_names_nothing_of_this_computer(self) -> None:
        first = docs_gen.generate()
        with mock.patch.dict(os.environ, {"HOME": "/nonexistent-cm-docs", "COLUMNS": "40", "LANG": "C",
                                          "NO_COLOR": "1", "XDG_STATE_HOME": "/nonexistent-cm-state"}):
            second = docs_gen.generate()
        self.assertEqual(first, second)
        for path, text in first.items():
            with self.subTest(path.name):
                self.assertNotIn(str(REPO_ROOT), text)
                self.assertIsNone(re.search(r"/(?:home|Users)/[^/\s`<]+", text))
                self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))


class CoverageTests(unittest.TestCase):
    """Every command and operation is listed, named or counted."""

    def test_every_command_and_operation_is_classified(self) -> None:
        classified = docs_gen.classification()
        kinds = {kind.split(":", 1)[0] for kind in classified.values()}
        self.assertEqual(kinds, {"listed", "name-only", "hidden", "counted"})
        for command in proxy.PROXY_COMMANDS:
            self.assertIn(f"claude-multi-proxy {command.name}", classified)
        for op in surface_matrix.OPERATIONS:
            self.assertIn(f"operation {op.id}", classified)
        # The earlier spellings and the hook command are hidden.
        for label in ("claude-multi compose", "claude-multi show", "claude-multi session-event",
                      "claude-multi sessions transition"):
            self.assertTrue(classified[label].startswith("hidden"), label)
        for label in ("claude-multi custom", "claude-multi migrate", "claude-multi restore-2x",
                      "claude-multi profile migrate", "claude-multi providers migrate-custom"):
            self.assertEqual(classified[label], "name-only", label)

    def test_the_command_reference_follows_the_classification(self) -> None:
        reference = _region("reference/cli.md", "cli-reference")
        headings = set(_headings(reference))
        for label, kind in docs_gen.classification().items():
            if not label.startswith("claude-multi ") or label.startswith("claude-multi-proxy"):
                continue
            with self.subTest(label):
                if kind in ("listed", "name-only"):
                    self.assertIn(label, headings)
                else:
                    self.assertNotIn(label, headings)
                    self.assertNotIn(f"`{label} ", reference)
        # A name-only command gets no usage line.
        for label, kind in docs_gen.classification().items():
            if kind == "name-only":
                self.assertNotIn(f"\n{label} ", reference.split("```text", 1)[1], label)

    def test_every_listed_argument_is_in_the_reference(self) -> None:
        reference = _region("reference/cli.md", "cli-reference")
        parser = cli_parser.build_parser()
        for label, kind in docs_gen.classification().items():
            if kind != "listed" or not label.startswith("claude-multi") or label.startswith("claude-multi-proxy"):
                continue
            path = tuple(label.split()[1:])
            node = docs_gen._node(parser, path)
            for action in docs_gen._arguments(node):
                spelled = action.option_strings[0] if action.option_strings else docs_gen._spell(" ".join(path), action)
                with self.subTest(f"{label} {action.dest}"):
                    self.assertIn(f"`{spelled.replace('|', chr(92) + '|')}", reference)

    def test_the_gateway_tool_and_the_cm_verbs(self) -> None:
        reference = _region("reference/cli.md", "cli-reference")
        listed = [command for command in proxy.PROXY_COMMANDS if command.audience in docs_gen.PROXY_LISTED]
        self.assertTrue(listed)
        for command in proxy.PROXY_COMMANDS:
            with self.subTest(command.name):
                self.assertEqual(f"`claude-multi-proxy {command.name}`" in reference, command in listed)
        counted = len(proxy.PROXY_COMMANDS) - len(listed)
        self.assertIn(f"lists its {counted} maintenance commands", reference)
        for verb in lineup_files.CM_VERBS:
            with self.subTest(verb.name):
                self.assertIn(f"| {docs_gen._code(docs_gen.cm_spelled(verb.name))} |", reference)

    def test_the_task_reference_lists_or_counts_every_operation(self) -> None:
        tasks = _region("reference/tasks.md", "tasks")
        counts: dict[str, int] = {}
        for op in surface_matrix.OPERATIONS:
            with self.subTest(op.id):
                row = f"| {docs_gen._prose(op.does)} |"
                if docs_gen.task_listed(op):
                    self.assertIn(row, tasks)
                else:
                    counts[docs_gen.task_omission(op)] = counts.get(docs_gen.task_omission(op), 0) + 1
        summary = tasks.split("Not listed here: ", 1)[1]
        self.assertEqual(sum(int(n) for n in re.findall(r"(\d+) ", summary)), sum(counts.values()))
        self.assertEqual(sum(counts.values()), len([op for op in surface_matrix.OPERATIONS
                                                    if not docs_gen.task_listed(op)]))
        # Every listed task row has all five cells, none blank or a bare dash.
        for row in tasks.splitlines():
            if row.startswith("| ") and not row.startswith(("| Task |", "| --- |", "| Key on the card |")):
                cells = [row[start:stop].strip() for start, stop in vocab._cells(row)][1:-1]
                if len(cells) == 5:
                    self.assertTrue(all(cell and cell not in ("-", "—") for cell in cells), row)

    def test_every_object_has_its_page(self) -> None:
        listed = {op.object for op in surface_matrix.OPERATIONS if docs_gen.task_listed(op)}
        self.assertLessEqual(listed, set(docs_gen.OBJECT_PAGES))
        tasks = _page("reference/tasks.md")
        for obj in sorted(listed):
            self.assertIn(f"## {docs_gen.OBJECT_PAGES[obj][0]}\n", tasks)

    def test_the_cheatsheet_rows_name_inventory_operations(self) -> None:
        self.assertEqual(docs_gen.cheat_problems(), [])
        bad = docs_gen.CheatRow("x", ("launch.fresh", "no.such-operation"), "n", ("doctor",), ("bogus",))
        self.assertEqual(docs_gen.cheat_problems([bad]), ["x: no operation no.such-operation"])
        bad = docs_gen.CheatRow("y", ("launch.fresh",), "n", ("doctor",), ("bogus",))
        self.assertEqual(docs_gen.cheat_problems([bad]), ["y: 'doctor' is not a spelling of its operations",
                                                          "y: /cm bogus is not a verb of its operations"])

    def test_every_key_of_the_interface_is_in_the_cheatsheet(self) -> None:
        from claude_multi.cli.screens import actions

        keys = _region("CHEATSHEET.md", "cheatsheet-keys")
        for screen, rows in actions.ACTIONS.items():
            row = next(line for line in keys.splitlines() if line.startswith(f"| {docs_gen.SCREEN_TITLES[screen]} |"))
            for action in rows:
                with self.subTest(action.id):
                    self.assertIn(f"**{docs_gen._prose(action.key)}** {docs_gen._prose(action.does)}", row)


class InputTests(unittest.TestCase):
    """What the generator writes is checkable: typed placeholders the span
    checker knows, and examples that spell their operation."""

    def test_every_placeholder_is_typed(self) -> None:
        for (path, dest), name in docs_gen.VALUES.items():
            with self.subTest(f"{path} {dest}"):
                if name:
                    self.assertTrue(name in vocab.PLACEHOLDERS or name in vocab.FRAGMENTS, name)
        for text in docs_gen.EXAMPLES.values():
            for name in re.findall(r"<([a-z][a-z0-9-]*)>", text):
                self.assertTrue(name in vocab.PLACEHOLDERS or name in vocab.FRAGMENTS, (text, name))

    def test_every_example_spells_its_operation(self) -> None:
        rows = surface_matrix.rows()
        for (op_id, spelling), text in docs_gen.EXAMPLES.items():
            with self.subTest(op_id):
                self.assertIn(op_id, rows)
                self.assertIn(spelling, rows[op_id].cli)
                self.assertTrue(docs_gen.example(rows[op_id], spelling).startswith("claude-multi "))
                self.assertEqual(vocab.span_problem("claude-multi " + text), "")
        # An example that drops its spelling's flag stops the generator.
        with mock.patch.dict(docs_gen.EXAMPLES, {("setup.status", "setup --status"): "setup"}), \
                self.assertRaises(SystemExit):
            docs_gen.example(rows["setup.status"], "setup --status")

    def test_every_generated_command_parses(self) -> None:
        counts = {"spans": 0, "forms": 0}
        for name, (label, _build) in docs_gen.REGIONS.items():
            region = _region(label, name)
            spans = vocab.command_spans(region)
            with self.subTest(name):
                for _line, span, _physical in spans:
                    result = vocab.page_check(label, span)
                    self.assertEqual(result.problem, "", span)
                    counts["spans"] += 1
                    counts["forms"] += result.forms
        self.assertGreater(counts["spans"], 500, counts)
        self.assertGreater(counts["forms"], counts["spans"], counts)


if __name__ == "__main__":
    unittest.main()
