"""Stale-vocabulary gate over the public pages and --help.

The stale terms (``composition``, ``variant``, ``cm-*-<model>-<lane>``,
``--legacy``, "does not hot-reload", "isolation contracts") appear in the
in-tree set only as intentional mentions of legacy formats, and each file
stays within its budget (``_docs_vocab.BUDGETS``). Every rule lives in
``tests/_docs_vocab.py``; this module runs it over the in-tree set (every
public page, every rendered ``claude-multi`` ``--help``, the proxy/dev
help texts, the two phrases in ``src``), plus the secret-read (R7),
backend-command (R8) and public-page rules (R13–R15), the allowlist
hygiene, the rule unit cases (``self_test``) and the checker's refusal to
read transcripts or secrets.

The intentional hits are printed once (stderr) so a reviewer sees what the
budgets admit; ``python3 tests/check_docs_vocabulary.py -v`` prints the
same list.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _docs_vocab as vocab
import check_docs_vocabulary as checker
from _layout import REPO_ROOT, doc_path
from claude_multi import cli

_SCAN: tuple[list[vocab.Target], list[vocab.Hit], list[str]] | None = None


def _scan() -> tuple[list[vocab.Target], list[vocab.Hit], list[str]]:
    """The in-tree set, scanned once for the module (help pages included)."""

    global _SCAN
    if _SCAN is None:
        targets = vocab.in_tree_targets()
        hits, hygiene = vocab.run_all(targets)
        _SCAN = (targets, hits, hygiene)
    return _SCAN


def _hits_for(label: str) -> list[vocab.Hit]:
    return [h for h in _scan()[1] if h.path == label]


def _failing(hits: list[vocab.Hit]) -> list[str]:
    return [h.format() for h in hits if h.failing]


class InTreeVocabularyTests(unittest.TestCase):
    """One subTest per in-tree file; budgets."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.targets, cls.hits, cls.hygiene = _scan()

    def test_the_in_tree_set_is_complete(self) -> None:
        labels = {t.label for t in self.targets}
        for name in vocab.PKG_DOCS:
            self.assertIn(name, labels)
        self.assertIn("<help: claude-multi>", labels)
        self.assertIn("<doc: claude-multi-dev --help>", labels)
        self.assertIn("src/claude_multi/tui.py", labels)

    def test_no_failing_hit_in_any_in_tree_file(self) -> None:
        for target in self.targets:
            with self.subTest(target.label):
                self.assertEqual(_failing(_hits_for(target.label)), [])

    def test_intentional_hits_stay_within_budget(self) -> None:
        counts = vocab.intentional_counts(self.hits)
        listing = "\n".join(
            f"  {h.format()}" for h in self.hits if h.intentional and h.rule in vocab.TERMS
        )
        print(
            f"\n[docs vocabulary gate] {sum(counts.values())} intentional hit(s), "
            f"budgets {dict(sorted(vocab.BUDGETS.items()))}:\n{listing}",
            file=sys.stderr,
        )
        self.assertEqual(vocab.budget_failures(self.hits), [])
        # A label with intentional hits must have a budget (absent = 0).
        self.assertLessEqual(set(counts), set(vocab.BUDGETS))

    def test_budgets_track_the_observed_count(self) -> None:
        # "rounded up by at most 2": a budget far above its file's count
        # would let 2.x narrative creep back unnoticed.
        counts = vocab.intentional_counts(self.hits)
        for label, budget in vocab.BUDGETS.items():
            with self.subTest(label):
                observed = counts.get(label, 0)
                self.assertGreaterEqual(budget, observed)
                self.assertLessEqual(budget - observed, vocab.BUDGET_HEADROOM)

    def test_budget_failure_is_reported(self) -> None:
        hit = vocab.Hit("USAGE.md", 1, "composition", "x", intentional="marker: 2.x")
        self.assertEqual(
            vocab.budget_failures([hit] * 3, {"USAGE.md": 2}),
            ["USAGE.md: 3 intentional hit(s), budget 2"],
        )
        # Absent label = budget 0; report-only and non-term hits never count.
        self.assertEqual(vocab.budget_failures([hit], {}), ["USAGE.md: 1 intentional hit(s), budget 0"])
        other = vocab.Hit("X.md", 1, "secret-read", "x", intentional="n/a")
        report = vocab.Hit("X.md", 1, "composition", "x", intentional="marker: 2.x", report_only="interim")
        self.assertEqual(vocab.budget_failures([other, report], {}), [])

    def test_the_checker_agrees(self) -> None:
        # The gate-step checker (in-tree mode) enforces the same rules,
        # allowlist hygiene and budgets as this module.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checker.main([])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn(" 0 failing, ", out.getvalue())


class HelpVocabularyTests(unittest.TestCase):
    """Every rendered claude-multi --help page."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pages = dict(vocab.help_pages())
        cls.help_hits = [h for h in _scan()[1] if h.path.startswith("<help: ")]

    def test_the_walk_reaches_every_subparser(self) -> None:
        expected = {p.prog for p in vocab._walk(cli.build_parser())}
        self.assertEqual(set(self.pages), expected)
        for prog in ("claude-multi", "claude-multi direct", "claude-multi lineup",
                     "claude-multi profile migrate", "claude-multi sessions mark-ended",
                     "claude-multi doctor", "claude-multi restore-2x"):
            self.assertIn(prog, self.pages)
        self.assertGreaterEqual(len(self.pages), 40)

    def test_only_the_profile_migrate_line_is_an_intentional_hit(self) -> None:
        self.assertEqual(_failing(self.help_hits), [])
        intentional = [(h.path, h.rule) for h in self.help_hits if h.intentional]
        self.assertEqual(intentional, [("<help: claude-multi profile>", "composition")])
        (only,) = [h for h in self.help_hits if h.intentional]
        self.assertIn("convert legacy compositions", only.text)

    def test_the_fixed_parser_strings_are_clean(self) -> None:
        # The two parser strings the walk once found stale stay clean.
        parser = cli.build_parser()
        self.assertNotRegex(parser.description.lower(), r"composition|variant")
        direct_line = next(
            line for line in self.pages["claude-multi"].splitlines() if line.strip().startswith("direct ")
        )
        self.assertNotRegex(direct_line.lower(), r"composition|ordinary gateway")

    def test_kept_alias_flags_are_blanked_not_hidden(self) -> None:
        # The root page no longer shows the earlier alias flags (they parse,
        # hidden); any composition hit left on a page is on a line that
        # carries an alias token, which the walk blanks.
        raw = vocab.Target("<raw>", "text", frozenset(vocab.RULES_TERMS), text=self.pages["claude-multi"])
        raw_hits = vocab.scan_target(raw)
        self.assertFalse(any(h.rule == "composition" for h in raw_hits))
        for hit in raw_hits:
            if hit.rule == "composition" and hit.failing:
                self.assertRegex(hit.text, vocab.HELP_ALIAS_TOKENS)

    def test_epilog_names_the_cheatsheet_and_the_installation_is_masked(self) -> None:
        # The rendered help carries the installation's document path (a
        # dev checkout path); the scan sees <installation> instead,
        # so R9 holds in the dev suite and in the sandbox alike.
        from claude_multi import layout

        self.assertIn("CHEATSHEET.md", vocab.help_epilog())
        top = self.pages["claude-multi"]
        root = layout.installation()
        self.assertNotIn(str(root), top)
        # A source checkout keeps the doc under docs/; the package installs
        # it at the top of its tree.
        document = layout.document("CHEATSHEET.md")
        self.assertIsNotNone(document)
        self.assertIn(f"<installation>/{document.relative_to(root).as_posix()}", top)
        machine = [h.format() for h in self.help_hits if h.rule == "machine-path"]
        self.assertEqual(machine, [])


class DocstringVocabularyTests(unittest.TestCase):
    """claude-multi-proxy --help, bare claude-multi-dev, claude-multi-dev --help."""

    def test_module_help_texts_are_clean(self) -> None:
        labels = [label for label, _text in vocab.module_help_texts()]
        self.assertEqual(
            labels,
            ["<doc: claude-multi-proxy --help>", "<doc: claude-multi-dev (bare)>", "<doc: claude-multi-dev --help>"],
        )
        for label in labels:
            with self.subTest(label):
                hits = _hits_for(label)
                self.assertEqual(_failing(hits), [])
                self.assertEqual([h.format() for h in hits if h.intentional], [])

    def test_dev_help_is_the_printed_text(self) -> None:
        from claude_multi import dev

        self.assertIn("promote", dev.DEV_HELP)
        self.assertTrue(dev.__doc__)


class SourcePhraseTests(unittest.TestCase):
    """The two phrases in ``src`` (raw text, no markers)."""

    def test_no_source_file_says_the_phrases(self) -> None:
        sources = sorted((REPO_ROOT / "src" / "claude_multi").rglob("*.py"))
        self.assertGreater(len(sources), 20)
        self.assertTrue(any("cli" in p.relative_to(REPO_ROOT / "src" / "claude_multi").parts[:-1] for p in sources))
        for path in sources:
            label = path.relative_to(REPO_ROOT).as_posix()
            with self.subTest(label):
                self.assertEqual([h.format() for h in _hits_for(label)], [])

    def test_markers_do_not_excuse_a_source_phrase(self) -> None:
        text = "# 2.x history: the gateway does not hot-reload; isolation contracts\n"
        target = vocab.Target("x.py", "text", frozenset(vocab.SOURCE_PHRASE_TERMS), text=text, markers=False)
        self.assertEqual(sorted(h.rule for h in vocab.scan_target(target) if h.failing),
                         ["isolation-contracts", "no-hot-reload"])
        for variant in ("does **not** hot-reload", "doesn't hot reload", "do not hotreload"):
            with self.subTest(variant):
                self.assertRegex(variant, vocab.TERMS["no-hot-reload"])


class SecretReadTests(unittest.TestCase):
    """R7 (no allowlist) over every in-tree text."""

    def test_no_in_tree_text_reads_a_secret(self) -> None:
        targets, hits, _ = _scan()
        checked = [t.label for t in targets if "secret-read" in t.rules]
        self.assertGreaterEqual(len(checked), 37 + 40)  # the public pages + the help pages
        self.assertEqual([h.format() for h in hits if h.rule == "secret-read"], [])

    def test_management_secret_paths_and_aliases_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cm-docs-management-") as tmp:
            home = Path(tmp)
            for name in ("management-key", "management-key.next", "management-key.disabled",
                         "management-key.prepared", "management-key.lock", "management-key.backup",
                         ".management-key.next.k2j3.tmp", ".api-key.abc123.tmp", ".previous-key.x.tmp"):
                path = home / name  # names alone forbid even a nonexistent file
                with self.subTest(name=name):
                    self.assertEqual(vocab.forbidden_reason(path, {"HOME": tmp}), "a secret file (names only)")
                    alias = home / (name + ".md")
                    alias.symlink_to(path)
                    self.assertTrue(vocab.forbidden_reason(alias, {"HOME": tmp}))
            alias = home / "innocent.md"
            alias.symlink_to(home / "management-key.next")
            self.assertTrue(vocab.forbidden_reason(alias, {"HOME": tmp}))
            self.assertEqual(vocab.forbidden_reason(home / "notes.md", {"HOME": tmp}), "")
            self.assertEqual(vocab.forbidden_reason(home / ".notes.md.tmp", {"HOME": tmp}), "")

    def test_the_rule_unit_cases(self) -> None:
        # Secret, journal, backend, span, marker and forbidden-path cases
        # (the checker's --self-test).
        self.assertEqual(vocab.self_test(), [])


class JournalConfinementTests(unittest.TestCase):
    def test_auth_suffix_and_case_insensitive_auth_extraction_are_not_confined(self) -> None:
        for args in (
            ["-oE", "h=[^ ]+"], ["-oE", "th=[^ ]+"],
            ["-oiE", "AUTH=[^ ]+"], ["--only-matching", "--ignore-case", "AUTH=[^ ]+"],
            ["-oE", "index=[^ ]+"], ["-oE", "file=[^ ]+"],
            ["-oE", "id=[^ ]+"], ["-oE", "_id=[^ ]+"],
            ["-oE", "-e", "model=[^ ]+", "-e", "h=[^ ]+"],
        ):
            with self.subTest(args=args):
                self.assertFalse(vocab._grep_o_confined(args))
        for extraction in ("grep -oE 'h=[^ ]+'", "grep -oiE 'AUTH=[^ ]+'",
                           "grep --only-matching --ignore-case 'AUTH=[^ ]+'",
                           "grep -oE 'file=[^ ]+'", "grep -oE 'id=[^ ]+'",
                           "grep -oE '_id=[^ ]+'"):
            with self.subTest(extraction=extraction):
                self.assertTrue(vocab.journal_problem(
                    "journalctl --user -u cli-proxy-api -o cat | " + extraction + " | sort | uniq -c"
                ))
        for pattern in ("model=[^ ]+", "provider=[^ ]*", "session=[^ )]+", "session_id=[^ ]+"):
            self.assertTrue(vocab._grep_o_confined(["-oE", pattern]), pattern)


class PublicPageRuleTests(unittest.TestCase):
    """R8 and R13–R15 over the public pages; R9 without exceptions."""

    def test_no_public_page_hits_the_public_page_rules(self) -> None:
        for name in vocab.PKG_DOCS:
            with self.subTest(name):
                self.assertEqual([h.format() for h in _hits_for(name) if h.rule in
                                  ("backend-command", "this-machine", "operator-skill", "release-lineage")], [])

    def test_the_rules_are_not_vacuous(self) -> None:
        unlabelled = "The gateway runs as `cli-proxy-api`; `systemctl --user restart cli-proxy-api`.\n"
        self.assertTrue(any(h.rule == "backend-command" for h in vocab._fixture_hits(unlabelled)))
        self.assertTrue(any(h.rule == "this-machine" for h in vocab._fixture_hits("On this machine, restart it.\n")))
        with mock.patch.object(vocab, "operator_skills", lambda: re.compile(r"\bexample-ops-skill\b")):
            self.assertTrue(any(h.rule == "operator-skill" for h in vocab._fixture_hits("skill example-ops-skill §6\n")))
        self.assertTrue(any(h.rule == "release-lineage" for h in vocab._fixture_hits("kept for 3.0.x\n")))

    def test_the_release_line_pattern_is_the_lineage_gate_s(self) -> None:
        import test_hygiene

        self.assertEqual(vocab.RELEASE_LINE.pattern, dict(test_hygiene.LINEAGE_PATTERNS)["release-line"])

    def test_public_pages_carry_no_machine_path(self) -> None:
        # R9 on every public page, the extended set on the user pages; no
        # page has a "this machine" exception any more.
        for target in _scan()[0]:
            if target.label in vocab.PKG_DOCS:
                with self.subTest(target.label):
                    self.assertIn("machine-path", target.rules)
                    if target.label in vocab.USER_DOCS:
                        self.assertIn("machine-path-extended", target.rules)
                    self.assertEqual([h.format() for h in _hits_for(target.label) if h.rule == "machine-path"], [])
        # Any real account's home directory is refused; neutral identities and
        # the public repository's address pass.
        # (Split, so this file carries no site of its own.)
        for text, refused in (("AGENTS keeps /home/" + "somebody/x on this machine.\n", True),
                              ("Paths such as `/Users/" + "jdoe/Library`.\n", True),
                              ("Paths such as `/home/user/.config` and `/Users/me/x`.\n", False),
                              (f"Clone {vocab.PUBLIC_REPOSITORY}.\n", False)):
            with self.subTest(text=text):
                target = vocab.Target("AGENTS.md", "markdown", frozenset(vocab.RULES_MD), text=text)
                self.assertEqual(any(h.rule == "machine-path" for h in vocab.scan_target(target)), refused)


class AllowlistHygieneTests(unittest.TestCase):
    """EXPLICIT ≤ 10 and none stale; BUDGETS keys name in-tree labels."""

    def test_explicit_entries(self) -> None:
        self.assertLessEqual(len(vocab.EXPLICIT), vocab.MAX_EXPLICIT)
        self.assertEqual(_scan()[2], [])
        for glob, substring, reason in vocab.EXPLICIT:
            self.assertTrue(glob and substring and reason)

    def test_a_stale_entry_fails(self) -> None:
        stale = (*vocab.EXPLICIT, ("USAGE.md", "no such text anywhere", "test"))
        _hits, hygiene = vocab.run_all(
            [t for t in vocab.in_tree_targets(include_help=False) if t.label in vocab.PKG_DOCS], explicit=stale
        )
        self.assertEqual(hygiene, ["stale EXPLICIT entry: USAGE.md 'no such text anywhere'"])
        many = tuple(("X.md", f"s{i}", "r") for i in range(vocab.MAX_EXPLICIT + 1))
        self.assertIn(f"EXPLICIT has {len(many)} entries (at most {vocab.MAX_EXPLICIT})",
                      vocab.explicit_problems(many, set(range(len(many)))))

    def test_budget_keys_name_in_tree_labels(self) -> None:
        labels = {vocab.budget_label(t.label) for t in _scan()[0]}
        self.assertLessEqual(set(vocab.BUDGETS), labels)
        self.assertEqual(vocab.BUDGETS["<help>"], 1)


# ------------------------------------------------ path guard


class PathGuardTests(unittest.TestCase):
    """The checker refuses transcripts and secrets before reading anything."""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="cm-docs-guard-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.home = tmp / "home"
        self.claude = self.home / ".claude"
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        env["HOME"] = str(self.home)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _run(self, *argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = checker.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def test_transcripts_and_secrets_are_refused_before_any_read(self) -> None:
        projects = self.claude / "projects" / "p"
        self._write(projects / "notes.md", "never read\n")
        for path in (projects / "notes.md", self.home / "x.jsonl",
                     self.home / ".config" / "claude-multi" / "api-key",
                     self.home / ".config" / "claude-multi" / "config.yaml",
                     *(self.home / ".config" / "claude-multi" / name for name in
                       ("management-key", "management-key.next", "management-key.disabled",
                        "management-key.prepared", "management-key.lock", "management-key.backup"))):
            with self.subTest(str(path)):
                code, out = self._run(str(path))
                self.assertEqual(code, 2, out)
                self.assertIn("refused", out)


if __name__ == "__main__":
    unittest.main()
