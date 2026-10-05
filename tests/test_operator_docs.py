"""The public documentation: its tree, its gates and its anchors.

Every public page (the five root pages and every page under ``docs/``,
discovered from the tree) is checked with the one gate module
``tests/_docs_vocab.py``: command spans parse, “…” fragments exist in the
launcher source, links resolve (heading anchors included; an installed
page reaches the root pages only by their public URL), and no failing rule
hit. On top of that: the page tree and its installed set, the drafting
markers and their ratchet, the CHEATSHEET's structure and its coverage of
every doctor BLOCK family, the public gateway-log surface, the ``--help``
pointers, and the facts pages share with the launcher's own text. Budgets
and the in-tree vocabulary gate are ``test_docs_vocabulary``'s.
"""

from __future__ import annotations

import ast
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _docs_vocab as vocab
from _layout import DOCS_DIR, REPO_ROOT, RESOURCES_ROOT, doc_path, load_tool
from claude_multi import cli, lineup, tui
import argparse

DOCS = vocab.PKG_DOCS  # every public page, by label
docs_gen = load_tool("docs_gen")
SRC = REPO_ROOT / "src" / "claude_multi"

# The proposed tree: five root pages and these 32 pages under docs/.
DOCS_TREE = (
    "CHEATSHEET.md", "STANDALONE.md", "USAGE.md",
    "guides/gateway.md", "guides/lineup.md", "guides/models.md", "guides/move-machines.md",
    "guides/networking.md", "guides/profiles.md", "guides/sessions.md",
    "install/linux.md", "install/macos.md", "install/nix.md", "install/windows-wsl2.md",
    "privacy.md",
    "providers/anthropic-compatible.md", "providers/api-keys.md", "providers/chatgpt-account.md",
    "providers/claude-account.md", "providers/lan.md", "providers/openai-compatible.md",
    "providers/openrouter.md",
    "quickstart.md",
    "reference/cli.md", "reference/compatibility.md", "reference/licenses.md", "reference/settings.md",
    "reference/tasks.md",
    "security.md", "troubleshooting.md", "uninstall.md", "update.md",
)

# The drafting markers each page may still carry (``<!-- docs-todo(OWNER):
# … -->``): facts that land with later work. A ratchet: a page may only go
# down; a page that reaches zero leaves the table; a page not listed carries
# none. Release preflight, not the candidate version, requires zero markers.
DOCS_TODO_CEILINGS: dict[str, int] = {}


def _text(name: str) -> str:
    """A public page by label, else a repository path."""

    path = doc_path(name) if name in vocab.PKG_DOCS else REPO_ROOT / name
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    """The body of the heading line ``heading`` (any level) up to the next heading of that level or higher."""

    lines = text.split("\n")
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if match and match[2] == heading:
            level = len(match[1])
            body = []
            for rest in lines[index + 1 :]:
                other = re.match(r"^(#{1,6})\s", rest)
                if other and len(other[1]) <= level:
                    break
                body.append(rest)
            return "\n".join(body)
    raise AssertionError(f"no heading {heading!r}")


def _rows(text: str) -> list[str]:
    """Markdown table body rows (header and separator rows dropped)."""

    rows = [line for line in text.split("\n") if line.startswith("| ")]
    return [row for row in rows if not re.match(
        r"^\|\s*(You see|Task|Screen|Path|Key|Class|Symptom|Option|Field|Want|Page|Route|Target|Provider|"
        r"Process|Component|From|What|Mac|Architecture|Flake output|Kind|Function|In the export|"
        r"Group|You use|Key on the card|Level|Command|Doc)\s*\|", row)]


def _links(name: str) -> set[str]:
    """The pages (labels) a public page links to, relative links only."""

    path = doc_path(name)
    found = set()
    for target in re.findall(r"(?<!!)\[[^\]\n]*\]\(([^)\s#]+)(?:#[^)\s]*)?\)", path.read_text(encoding="utf-8")):
        if re.match(r"[a-z][a-z0-9+.-]*:", target):
            continue
        resolved = Path(os.path.normpath(path.parent / target))
        if DOCS_DIR in resolved.parents:
            found.add(resolved.relative_to(DOCS_DIR).as_posix())
        elif resolved.parent == REPO_ROOT:
            found.add(resolved.name)
    return found


class GateRuleTests(unittest.TestCase):
    """Every public page: the shared rules (no failing hit), spans, fragments, links."""

    def test_no_failing_gate_hit(self) -> None:
        targets = [t for t in vocab.in_tree_targets(include_help=False) if t.label in DOCS]
        self.assertEqual(sorted(t.label for t in targets), sorted(DOCS))
        for target in targets:
            with self.subTest(target.label):
                self.assertEqual([h.format() for h in vocab.scan_target(target) if h.failing], [])

    def test_user_pages_pass_the_user_page_rules(self) -> None:
        # R8 (a service-manager command names its backend, never the earlier
        # unit), R13 (no "this machine" remedy), R14 (no operator skill), R15
        # (no release lineage), the extended machine paths, R11 (journal
        # output filtered) and R12 (lineup --session takes <runtime-id>).
        self.assertIn("README.md", vocab.USER_DOCS)
        self.assertIn("install/windows-wsl2.md", vocab.USER_DOCS)
        self.assertNotIn("AGENTS.md", vocab.USER_DOCS)
        for name in vocab.USER_DOCS:
            with self.subTest(name):
                hits = vocab._fixture_hits(_text(name), vocab.RULES_USER_DOC, label=name)
                self.assertEqual([h.format() for h in hits if h.failing], [])

    def test_contributor_pages_carry_no_machine_remedy_or_operator_skill(self) -> None:
        for name in vocab.CONTRIBUTOR_PAGES:
            with self.subTest(name):
                hits = vocab._fixture_hits(_text(name), {"backend-command", "this-machine", "operator-skill",
                                                         "machine-path"}, label=name)
                self.assertEqual([h.format() for h in hits], [])

    def test_every_command_span_parses(self) -> None:
        # Every form of every span on every public page (contributor pages
        # included) parses with the parser that owns it; a line that says
        # "refused" exempts nothing (negative examples are registered with
        # the refusal they must meet).
        for name in DOCS:
            with self.subTest(name):
                problems = [
                    f"{line}: {span}: {problem}"
                    for line, span, physical in vocab.command_spans(_text(name))
                    if (problem := vocab.page_span_problem(name, span, physical))
                ]
                self.assertEqual(problems, [])

    def test_span_coverage_is_counted(self) -> None:
        # Extraction found commands of every kind: zero extracted spans, or
        # a kind that silently stopped being found, cannot pass unnoticed.
        coverage = vocab.span_coverage({name: _text(name) for name in DOCS})
        self.assertEqual(coverage["problems"], 0, coverage)
        self.assertGreater(coverage["spans"], 300, coverage)
        self.assertGreater(coverage["complete"], 300, coverage)
        self.assertGreater(coverage["fragment"], 0, coverage)
        self.assertGreater(coverage["name"], 0, coverage)
        self.assertGreaterEqual(coverage["forms"], coverage["spans"], coverage)
        self.assertGreaterEqual(coverage["pages"], 20, coverage)
        for name in ("AGENTS.md", "CONTRIBUTING.md", "CHEATSHEET.md", "reference/cli.md"):
            with self.subTest(name):
                self.assertTrue(vocab.command_spans(_text(name)), name)

    def test_negative_examples_need_their_refusal(self) -> None:
        with mock.patch.dict(vocab.NEGATIVE_EXAMPLES, {("x.md", "claude-multi profile new"): "required: name",
                                                       ("x.md", "claude-multi profile list"): "anything",
                                                       ("x.md", "claude-multi profile show NAME"): "required"}):
            self.assertEqual(vocab.page_span_problem("x.md", "claude-multi profile new"), "")
            self.assertIn("a negative example that parses", vocab.page_span_problem("x.md", "claude-multi profile list"))
            self.assertIn("refused for another reason",
                          vocab.page_span_problem("x.md", "claude-multi profile show NAME"))
        for (page, span), expected in vocab.NEGATIVE_EXAMPLES.items():
            with self.subTest(span):
                self.assertIn(page, DOCS)
                self.assertIn(span, [found for _line, found, _physical in vocab.command_spans(_text(page))])
                self.assertTrue(expected)

    def test_the_placeholder_registry_is_typed_and_finite(self) -> None:
        for name, placeholder in vocab.PLACEHOLDERS.items():
            with self.subTest(name):
                self.assertRegex(name, r"^[a-z][a-z0-9-]*$")
                self.assertTrue(placeholder.kind and placeholder.sample)
                self.assertNotIn(name, vocab.FRAGMENTS)
        self.assertEqual(vocab.span_problem("claude-multi sessions show <session>"), "unknown placeholder <session>")

    def test_no_page_names_the_removed_launcher(self) -> None:
        for name in DOCS:
            with self.subTest(name):
                self.assertIsNone(vocab.REMOVED_LAUNCHER.search(_text(name)))

    def test_quoted_launcher_text_exists(self) -> None:
        for name in DOCS:
            with self.subTest(name):
                self.assertEqual(vocab.fragment_problems(_text(name)), [])

    def test_relative_links_resolve(self) -> None:
        # Every relative link resolves to a file of this repository, here
        # and in the sandbox, with its heading anchor; a link that leaves
        # the repository fails even when its target exists elsewhere.
        for path in vocab.link_targets():
            with self.subTest(path.relative_to(REPO_ROOT).as_posix()):
                self.assertEqual(vocab.link_problems(path), [])

    def test_no_reference_to_material_outside_the_repository(self) -> None:
        for path in vocab.link_targets():
            with self.subTest(path.relative_to(REPO_ROOT).as_posix()):
                self.assertEqual(vocab.private_references(path.read_text(encoding="utf-8")), [])

    def test_the_link_gates_read_every_public_document(self) -> None:
        # One inventory, discovered from the tree, for the discovered tests
        # and the standalone checker.
        self.assertEqual([path for path in vocab.link_targets()], [doc_path(name) for name in vocab.public_pages()])
        self.assertEqual(vocab.PUBLIC_DOCS, vocab.public_pages())
        for name in ("CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md", "AGENTS.md", "README.md",
                     "install/linux.md", "reference/licenses.md"):
            self.assertIn(name, vocab.PUBLIC_DOCS)

    def test_the_standalone_link_check_fails_on_a_contributor_document(self) -> None:
        import contextlib
        import io

        import check_docs_vocabulary as checker

        contributing = doc_path("CONTRIBUTING.md")
        original = Path.read_text

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            text = original(path, *args, **kwargs)
            if path == contributing:
                text += "\nSee the [development plan](../private-plan.md) and maintainer/notes.md.\n"
            return text

        out = io.StringIO()
        with mock.patch.object(Path, "read_text", read_text), contextlib.redirect_stdout(out):
            code = checker.main(["--links"])
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("CONTRIBUTING.md", out.getvalue())
        self.assertIn("../private-plan.md: outside the repository", out.getvalue())
        self.assertIn("outside reference: maintainer/", out.getvalue())

    def test_the_link_gate_refuses_outside_targets_and_private_references(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cm-links-") as tmp:
            root = Path(tmp) / "repo"
            (root / "docs" / "guides").mkdir(parents=True)
            (Path(tmp) / "elsewhere.md").write_text("outside\n")
            (root / "README.md").write_text("# Read me\n")
            (root / "docs" / "here.md").write_text("# Here\n\n## A section\n")
            doc = root / "docs" / "guide.md"
            doc.write_text("# Guide\n\n[ok](here.md) [gone](absent.md) [out](../../elsewhere.md)\n"
                           "[root](../README.md) [anchor](here.md#a-section) [bad anchor](here.md#nowhere)\n"
                           "[self](#guide) [self bad](#missing) [local](/home/user/x.md)\n"
                           f"[public]({vocab.PUBLIC_REPOSITORY}/blob/main/README.md)\n"
                           f"[public bad]({vocab.PUBLIC_REPOSITORY}/blob/main/ABSENT.md)\n")
            problems = [(target, problem.split(":", 1)[0].split(" (")[0]) for _line, target, problem in
                        vocab.link_problems(doc, root=root)]
            nested = root / "docs" / "guides" / "deep.md"
            nested.write_text("# Deep\n\n[up](../here.md#a-section) [top](../../README.md#read-me)\n")
            nested_problems = [target for _line, target, _problem in vocab.link_problems(nested, root=root)]
        self.assertEqual(problems, [
            ("absent.md", "missing"), ("../../elsewhere.md", "outside the repository"),
            ("../README.md", "an installed page reaches a root page only by its public URL"),
            ("here.md#nowhere", "no heading #nowhere in here.md"),
            ("#missing", "no heading #missing in this page"),
            ("/home/user/x.md", "a machine-local path"),
            (f"{vocab.PUBLIC_REPOSITORY}/blob/main/ABSENT.md", "missing in the repository"),
        ])
        self.assertEqual(nested_problems, ["../../README.md#read-me"])
        text = "see `docs/design/README.md` and [x](../maintainer/run.md)\n"
        self.assertEqual([found for _line, found in vocab.private_references(text)],
                         ["docs/design/", "../maintainer/"])
        self.assertEqual(vocab.private_references("src/claude_multi/maintainer_tools.py, docs/designer.md\n"), [])

    def test_an_installed_tree_links_only_inside_itself(self) -> None:
        # An installed document tree has no root pages and no repository
        # around it: links resolve inside it (nested directories and
        # anchors included), and a root page is reached by its public URL.
        with tempfile.TemporaryDirectory(prefix="cm-installed-") as tmp:
            share = Path(tmp) / "share" / "claude-multi"
            (share / "guides" / "deep").mkdir(parents=True)
            (share / "USAGE.md").write_text("# Usage\n\n## Start\n\n[deep](guides/deep/page.md#deep)\n")
            (share / "guides" / "deep" / "page.md").write_text(
                "# Deep\n\n[up](../../USAGE.md#start) [missing anchor](../../USAGE.md#nowhere)\n"
                "[root](../../../../README.md) [gone](../absent.md) [self](#deep)\n"
                f"[public]({vocab.PUBLIC_REPOSITORY}/blob/main/CONTRIBUTING.md)\n"
                f"[public bad]({vocab.PUBLIC_REPOSITORY}/blob/main/ABSENT.md)\n")
            (Path(tmp) / "README.md").write_text("# Read me\n")
            problems = [(page, target, problem.split(":", 1)[0]) for page, _line, target, problem
                        in vocab.installed_link_problems(share)]
        self.assertEqual(problems, [
            ("guides/deep/page.md", "../../USAGE.md#nowhere", "no heading #nowhere in USAGE.md"),
            ("guides/deep/page.md", "../../../../README.md", "outside the repository"),
            ("guides/deep/page.md", "../absent.md", "missing"),
            ("guides/deep/page.md", f"{vocab.PUBLIC_REPOSITORY}/blob/main/ABSENT.md", "missing in the repository"),
        ])

    def test_heading_anchors(self) -> None:
        for heading, slug in (("3. Where the key is kept", "3-where-the-key-is-kept"),
                              ("The sessions screen (`claude-multi -r` or **S**)",
                               "the-sessions-screen-claude-multi--r-or-s"),
                              ("`/model` and `/cm`", "model-and-cm"),
                              ("Windows and WSL are separate", "windows-and-wsl-are-separate")):
            with self.subTest(heading):
                self.assertEqual(vocab.heading_slug(heading), slug)

    def test_lineup_session_names_the_runtime_id(self) -> None:
        # Never `lineup --session <id>`/`ID`.
        for name in DOCS:
            for unit in vocab.parse_markdown(_text(name)):
                self.assertEqual(vocab.lineup_session_hits(unit, label=name), [], name)

    def test_lineup_help_is_never_called_refused(self) -> None:
        # `claude-multi lineup --help` lists the request grammar.
        for name in DOCS:
            for unit in vocab.parse_markdown(_text(name)):
                if "claude-multi lineup --help" in unit.text:
                    self.assertNotRegex(unit.text, r"lineup --help`? is refused", f"{name}:{unit.start}")

    def test_migration_material_is_not_documented_here(self) -> None:
        # No profile-migrate flags, no runbook, no migration chapter, no
        # earlier-release journey.
        for name in DOCS:
            text = _text(name)
            with self.subTest(name):
                self.assertIsNone(re.search(r"profile\s+migrate\s+--", text))
                self.assertNotIn("RUNBOOK", text)
                self.assertNotRegex(text, r"(?i)runbook")
                self.assertIsNone(re.search(r"\]\([^)]*MIGRATION\.md", text))
                self.assertNotRegex(text, r"(?m)^#+ .*(?i:coming from|rollback to \d)")

    def test_lengths(self) -> None:
        # The map stays a map; the landing page stays one screen of reading.
        self.assertLessEqual(len(_text("USAGE.md").splitlines()), 120)
        self.assertLessEqual(len(_text("README.md").splitlines()), 160)


class TreeTests(unittest.TestCase):
    """The page set, its installed form and its navigation."""

    def test_the_page_tree(self) -> None:
        self.assertEqual(vocab.docs_pages(), tuple(sorted(DOCS_TREE)))
        self.assertEqual(len(DOCS_TREE), 32)
        for name in vocab.ROOT_PAGES:
            self.assertTrue((REPO_ROOT / name).is_file(), name)
        # One source of notices: no root notices file.
        self.assertFalse((REPO_ROOT / "THIRD_PARTY_NOTICES.md").exists())

    def test_every_page_under_docs_is_installed_at_its_relative_path(self) -> None:
        from _layout import pyproject

        table = pyproject()["tool"]["setuptools"]["data-files"]
        installed = {}
        for key, files in table.items():
            for source in files:
                installed[source] = f"{key}/{Path(source).name}"
        self.assertEqual(sorted(installed), sorted(f"docs/{name}" for name in DOCS_TREE))
        for source, target in installed.items():
            with self.subTest(source):
                self.assertEqual(Path(target).relative_to("share/claude-multi"), Path(source).relative_to("docs"))
        manifest = (REPO_ROOT / "MANIFEST.in").read_text()
        for source in installed:
            self.assertIn(source, manifest.split())

    def test_every_page_is_reachable_from_the_map(self) -> None:
        # USAGE.md is the installed entry point: every page under docs/ is
        # reachable from it through links between installed pages.
        seen, todo = {"USAGE.md"}, ["USAGE.md"]
        while todo:
            for target in _links(todo.pop()):
                if target in DOCS_TREE and target not in seen:
                    seen.add(target)
                    todo.append(target)
        self.assertEqual(sorted(set(DOCS_TREE) - seen), [])

    def test_the_first_session_journey_is_linked(self) -> None:
        # README → quickstart → a platform install page → a provider page →
        # the first-session pages.
        self.assertIn("quickstart.md", {name.removeprefix("docs/") for name in _links("README.md")} | _links("README.md"))
        quickstart = _links("quickstart.md")
        for target in ("install/linux.md", "install/macos.md", "install/windows-wsl2.md", "install/nix.md",
                       "providers/api-keys.md", "providers/openrouter.md", "guides/profiles.md",
                       "guides/lineup.md", "guides/sessions.md", "troubleshooting.md"):
            self.assertIn(target, quickstart)
        for install in ("install/linux.md", "install/macos.md", "install/windows-wsl2.md"):
            self.assertIn("quickstart.md", _links(install), install)

    def test_readme_links_the_licences_page(self) -> None:
        for name in ("README.md", "SECURITY.md", "install/linux.md", "install/macos.md"):
            with self.subTest(name):
                self.assertTrue(any(target.endswith("reference/licenses.md") for target in _links(name))
                                or "docs/reference/licenses.md" in _text(name), name)


class DraftingMarkerTests(unittest.TestCase):
    """Facts that land with later work are drafted as tracked markers, never
    as visible placeholders."""

    def test_markers_are_well_formed_and_nothing_else_is_a_placeholder(self) -> None:
        for name in DOCS:
            with self.subTest(name):
                self.assertEqual(vocab.placeholder_problems(_text(name)), [])

    def test_the_markers_stay_within_the_ratchet(self) -> None:
        found = {name: len(vocab.todo_markers(_text(name))) for name in DOCS}
        found = {name: count for name, count in found.items() if count}
        unlisted = sorted(f"{name}: x{count}" for name, count in found.items() if name not in DOCS_TODO_CEILINGS)
        above = sorted(f"{name}: x{found[name]}, ceiling x{ceiling}"
                       for name, ceiling in DOCS_TODO_CEILINGS.items() if found.get(name, 0) > ceiling)
        gone = sorted(name for name in DOCS_TODO_CEILINGS if name not in found)
        self.assertEqual(
            {"new pages": unlisted, "above the ceiling": above, "pages now complete": gone},
            {"new pages": [], "above the ceiling": [], "pages now complete": []},
            "a drafted fact is a tracked marker: a completed page leaves DOCS_TODO_CEILINGS, a page "
            "may lower its ceiling, and no page gains one",
        )

    def test_release_preflight_refuses_drafting_markers(self) -> None:
        release = load_tool("release")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            pages = [Path(name) if name in vocab.ROOT_PAGES else Path("docs") / name for name in DOCS]
            pages.append(Path("docs/guides/new-page.md"))
            for relative in pages:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# Complete\n", encoding="utf-8")
            self.assertTrue(release.check_drafting_markers(repo).ok)
            for relative in pages:
                path = repo / relative
                path.write_text("# Draft\n\n<!-- docs-todo(release): the release facts -->\n", encoding="utf-8")
                check = release.check_drafting_markers(repo)
                self.assertEqual(check.name, "drafting-markers")
                self.assertFalse(check.ok)
                self.assertIn(f"{relative.as_posix()}:3", check.detail)
            check = release.check_drafting_markers(repo)
            for relative in pages:
                self.assertIn(f"{relative.as_posix()}:3", check.detail)
                (repo / relative).write_text("# Complete\n", encoding="utf-8")
            self.assertTrue(release.check_drafting_markers(repo).ok)

    def test_the_marker_rules(self) -> None:
        good = "Text.\n\n<!-- docs-todo(release): the fingerprint -->\n\nMore.\n"
        self.assertEqual(vocab.todo_markers(good), [(3, "release", "the fingerprint")])
        self.assertEqual(vocab.placeholder_problems(good), [])
        for bad in ("<!-- docs-todo(someday): x -->\n", "<!-- docs-todo(release):  -->\n",
                    "Text <!-- docs-todo(release): inline -->\n", "TODO: write this\n", "Fingerprint: TBD\n",
                    "<!-- docs-todo: no owner -->\n"):
            with self.subTest(bad):
                self.assertNotEqual(vocab.placeholder_problems(bad), [])


class GatewayServiceDocumentationTests(unittest.TestCase):
    def test_service_requirements_and_on_demand_alternative_are_explicit(self) -> None:
        for name, heading in (("guides/gateway.md", "The supervised service (Linux)"),
                              ("install/linux.md", "The gateway")):
            with self.subTest(name):
                text = _flat(_section(_text(name), heading))
                for anchor in ("unprivileged user namespaces", "Ubuntu 23.10 and later", "AppArmor",
                               "gateway exited during its start", "keeps the on-demand gateway",
                               "system-wide security trade-off"):
                    self.assertIn(anchor, text)
        guide = _flat(_section(_text("guides/gateway.md"), "The supervised service (Linux)"))
        for anchor in ("sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0", "/etc/sysctl.d/",
                       "The on-demand gateway does not need unprivileged user namespaces"):
            self.assertIn(anchor, guide)

    def test_service_limit_is_in_the_release_notes_and_troubleshooting(self) -> None:
        limits = _flat(_section(_text("CHANGELOG.md"), "Compatibility and limits"))
        for anchor in ("unprivileged user namespaces", "AppArmor", "Ubuntu 23.10 and later",
                       "on-demand gateway does not"):
            self.assertIn(anchor, limits)
        row = next(row for row in _rows(_section(_text("troubleshooting.md"), "The gateway"))
                   if "gateway exited during its start" in row)
        for anchor in ("claude-multi gateway service install", "on-demand gateway", "security trade-off",
                       "guides/gateway.md#the-supervised-service-linux", "claude-multi gateway service status"):
            self.assertIn(anchor, row)


class QuotaDocumentationTests(unittest.TestCase):
    def test_quota_is_documented_as_an_observation_with_its_limits(self) -> None:
        text = _flat(_section(_text("guides/gateway.md"), "Quota readings"))
        for anchor in ("claude-multi quota", "/cm quota", "no provider call", "not zero use",
                       "exits 1 when no reading was made", "only the Nix package enables",
                       "unavailable in this build", "not a balance", "not billed tokens",
                       "not on whether the gateway is on demand or supervised",
                       "Stale or exhausted readings still exit 0",
                       "`/cm quota` reports the same condition with status 0"):
            self.assertIn(anchor, text)

    def test_user_pages_teach_no_management_key_administration(self) -> None:
        for name in vocab.USER_DOCS:
            with self.subTest(name):
                self.assertNotRegex(_text(name), r"rotate-management-key|disable-management-key|init --prepare-start")


class CheatsheetStructureTests(unittest.TestCase):
    """A one-page reference first, then Troubleshooting."""

    def setUp(self) -> None:
        self.text = _text("CHEATSHEET.md")

    def test_sections_in_order(self) -> None:
        headings = [line for line in self.text.splitlines() if line.startswith("#")]
        order = ["## Commands at a glance", "### Keys", "### Files", "## Troubleshooting (symptom → command)"]
        positions = [headings.index(h) for h in order]
        self.assertEqual(positions, sorted(positions))

    def test_cm_grammar_is_lineup_help_line(self) -> None:
        self.assertIn(lineup.HELP_LINE, self.text.replace("\\|", "|"))

    def test_the_task_table_has_every_surface_column(self) -> None:
        table = _section(self.text, "Commands at a glance").split("### Keys", 1)[0]
        self.assertIn("| Task | TUI (from the card) | CLI | In-session | Notes |", table)
        rows = _rows(table)
        self.assertGreaterEqual(len(rows), 25)
        for row in rows:
            cells = [row[start:stop].strip() for start, stop in vocab._cells(row)][1:-1]
            with self.subTest(row[:40]):
                self.assertEqual(len(cells), 5, row)
                # Never an unexplained blank or dash.
                self.assertTrue(all(cell and cell not in ("-", "—") for cell in cells), row)

    def test_one_line_per_topic(self) -> None:
        # The task table is generated (tests/test_docs_generated.py): one
        # row per cheatsheet topic; the prose around it is written here.
        reference = self.text.split("## Troubleshooting", 1)[0]
        table = _section(self.text, "Commands at a glance").split("### Keys", 1)[0]
        tasks = [row.split("|")[1].strip() for row in _rows(table) if not row.startswith("| ---")]
        self.assertEqual(tasks, [row.task for row in docs_gen.CHEAT_ROWS])
        for anchor in (
            "| get started | W |", "P is the Profiles screen, not provider fallback", "P → F",
            "`claude-multi profile default [<name>]`", "`claude-multi profile starter`",
            "your version is kept as a backup", "`claude-multi profile reseed <name>`",
            "`claude-multi --profile <name>`", "`claude-multi direct [--model <model>]`",
            "`claude-multi -c`", "`claude-multi -r [<id>]`", "/cm set <agent>=<model>[:<effort>]",
            "`claude-multi lineup --session <runtime-id> show`", "`claude-multi profile list`",
            "`claude-multi models`", "`claude-multi discover <provider>`",
            "`claude-multi sessions list`", "`claude-multi sessions link <fork-id> --profile <name>`",
            "`claude-multi doctor --first-run`", "`claude-multi update`", "`claude-multi gateway status`",
            "`claude-multi providers set-key <provider>`", "`claude-multi window-ceiling --reset`",
            "`claude-multi export --out <file>`", "`claude-multi uninstall --dry-run`",
            "LIVE", "RELAUNCH", "`/reload-plugins`",
            "`choices.json`", "`~/.config/claude-multi/`", "`~/.claude/projects/`",
        ):
            self.assertIn(anchor, reference)

    def test_card_and_sessions_keys_match_the_landed_help(self) -> None:
        # The key rows are generated from the interface's action table;
        # they also carry every key the card's and the sessions screen's
        # own help name.
        keys = _section(self.text, "Keys")
        card = next(row for row in _rows(keys) if row.startswith("| launch card"))
        for key in ("Enter", "W", "E", "Tab", "P", "D", "S", "G", "M", "O", "V", "H", "U", "?", "Esc"):
            self.assertIn(f"**{key}** ", card)
        for line in cli.QUICK_HELP.splitlines():
            match = re.match(r"^([A-Z]) — ", line)
            if match:
                self.assertIn(f"**{match[1]}** ", card, line)
        sessions = next(row for row in _rows(keys) if row.startswith("| sessions"))
        for key, _word in cli.SESSIONS_KEYBAR:
            self.assertIn(f"**{key}** ", sessions)
        for key in ("E", "M", "P", "L"):
            self.assertIn(f"**{key}** ", sessions)
        marks = _flat(cli.SESSIONS_HELP.replace(tui.CHEATSHEET_HINT, "")).split("Marks: ", 1)[1]
        for mark in re.findall(r"[●◐○↻⚠!]", marks):
            self.assertIn(mark, self.text)

    def test_placeholders_say_where_full_ids_come_from(self) -> None:
        head = _flat(self.text.split("## Commands at a glance", 1)[0])
        for anchor in ("`<id>` is the full managed session id", "$CLAUDE_MULTI_MANAGED_ID",
                       "`Session <id>` line per session, with `→ runtime <runtime-id>` when the two differ",
                       "shows only the first 8 characters",
                       "`runtime_session_id` in `claude-multi sessions show <id>`"):
            self.assertIn(anchor, head)

    def test_troubleshooting_uses_the_public_gateway_commands(self) -> None:
        trouble = self.text.split("## Troubleshooting (symptom → command)", 1)[1]
        rows = _rows(trouble)
        self.assertGreaterEqual(len(rows), 40)
        for row in rows:
            with self.subTest(row[:50]):
                self.assertNotRegex(row, r"systemctl|journalctl|launchctl|cli-proxy-api|this machine")
        self.assertIn("`claude-multi gateway restart`", trouble)
        self.assertIn("`claude-multi gateway start`", trouble)

    def test_the_persistence_hold_comes_before_any_restart(self) -> None:
        trouble = self.text.split("## Troubleshooting (symptom → command)", 1)[1]
        hold = trouble.index("persistence hold")
        self.assertLess(hold, trouble.index("`claude-multi gateway restart`"))
        self.assertLess(hold, trouble.index("### Gateway"))

    def test_symptom_rows(self) -> None:
        trouble = _flat(self.text.split("## Troubleshooting (symptom → command)", 1)[1])
        for anchor in (
            "`claude-multi` must be on the session's PATH",
            "“may shadow the session's /cm skill”",
            "“is not set up for claude-multi”", "“does not match the verified sha256”",
            "one provider's agents stall", "`/cm fallback <provider>`",
            "“the on-disk gateway config differs from a fresh render of the installed catalog”",
            "“gateway key slots unusable”", "“gateway continuity set unreadable”", "“local gateway: <error> — <fix>”",
            "“scope(s) diverged from record authority”", "“scope differs from the record-authoritative compile”",
            "/cm: “next: type /reload-plugins”",
            "relaunch with a profile whose lead is that model: `claude-multi lineup --session <runtime-id> --relaunch profile <name>`",
            "“keyed generic openai-compatible routes are unavailable in this build: their compatibility audit gate is closed”",
            "add its name in O → kept environment",
            "`sh install.sh --repair`",
        ):
            self.assertIn(anchor, trouble)

    def test_gateway_logs_point_to_the_public_redacted_surface(self) -> None:
        # Raw gateway log lines can name account files and credential
        # indexes: the CHEATSHEET runs no journal or log-reading shell
        # command of its own, names no operator skill, and points to the
        # public commands — the log to read locally, doctor's summary
        # (counts per model and code, no account details) to share.
        self.assertNotRegex(self.text, r"journalctl|systemctl")
        self.assertEqual([hit for hit in vocab._fixture_hits(self.text, {"operator-skill"})], [])
        trouble = self.text.split("## Troubleshooting (symptom → command)", 1)[1]
        start = next(row for row in _rows(trouble) if "“local gateway: <error> — <fix>”" in row)
        self.assertIn("`claude-multi gateway logs`", start)
        why = next(row for row in _rows(trouble) if row.startswith("| why did the gateway stop"))
        for anchor in ("`claude-multi gateway logs`", "redacted", "never paste them publicly", "`claude-multi doctor`",
                       "without account details"):
            self.assertIn(anchor, why)
        for name in ("guides/gateway.md", "privacy.md", "troubleshooting.md"):
            with self.subTest(name):
                self.assertNotRegex(_text(name), r"journalctl|systemctl")


# ------------------------------------------------ doctor BLOCK coverage

# Every doctor BLOCK message family
# — the strings the doctor report functions put into their problems list —
# has a CHEATSHEET “…” fragment or a reasoned exclusion. ``(module, function,
# index of the problems list in its return tuple)``.
DOCTOR_BLOCK_FUNCTIONS = (
    # The report snapshot wraps these bodies.
    ("claude_multi.cli.doctor", "_collect_doctor_report_lines", 0),
    ("claude_multi.cli.doctor", "_doctor_profile_report", 0),
    ("claude_multi.cli.doctor", "_doctor_hook_targets", 0),
    ("claude_multi.cli.doctor", "_doctor_proxy_report", 0),
    ("claude_multi.cli.doctor", "_doctor_managed_report", 0),
    ("claude_multi.cli.doctor", "_doctor_served_checks", 0),
    ("claude_multi.cli.doctor", "_check_scope_integrity", 0),
    ("claude_multi.cli.doctor", "_doctor_scope_report", 1),
    ("claude_multi.cli.session_facts", "_read_session_records", 1),
    ("claude_multi.cli.doctor", "_doctor_collision_report", 1),
    ("claude_multi.launch", "doctor_binary_report", 0),
)
# Texts the problems lists receive through a helper call (every return).
DOCTOR_BLOCK_HELPERS = (
    ("claude_multi.cli.session_facts", "_collision_error"),
    ("claude_multi.sessions", "relink_message"),
    ("claude_multi.sessions", "pending_fork_message"),
)
# signature (constants joined, "{}" per formatted value) substring -> reason.
DOCTOR_BLOCK_EXCLUSIONS = {
    "profile {}: {}": "profile load/evaluation errors: the text is the field and its fix; "
                      "CHEATSHEET row doctor `profile <name>: <field>: <error>`",
    "profiles: {}": "the profiles directory is unreadable; the text names the path "
                    "(no fixed chunk of 10+ characters to quote)",
    "settings: {}": "the text after it is Runtime.current_effective's CLIError: "
                    "CHEATSHEET row “cannot read operator settings: <error>; fix or remove”",
    "session {} identity is {}; {}": "pending-fork only (_doctor_scope_report.identity_checks): the "
                                     "text continues with pending_fork_message, its own family "
                                     "(“has an unresolved native fork”, the fork row)",
}


def _module_constant(name: str) -> str | None:
    """A unique top-level string constant ``name`` across the source tree
    (``served_plan.ROOT_BLOCK.format(...)`` is a family)."""

    found = []
    for path in sorted(SRC.rglob("*.py")):
        for statement in ast.parse(path.read_text(encoding="utf-8")).body:
            if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                    and isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == name
                    and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str)):
                found.append(statement.value.value)
    return found[0] if len(found) == 1 else None


def _signature(node: ast.AST) -> str | None:
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format"
            and isinstance(node.func.value, (ast.Attribute, ast.Name))):
        target = node.func.value
        constant = _module_constant(target.attr if isinstance(target, ast.Attribute) else target.id)
        if constant is not None:
            return re.sub(r"\{[^{}]*\}", "{}", constant)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
        )
    return None


def _first_signature(node: ast.AST) -> str | None:
    for child in ast.walk(node):
        signature = _signature(child)
        if signature is not None:
            return signature
    return None


def _function(module: str, name: str) -> ast.FunctionDef:
    relative = module.removeprefix("claude_multi.").replace(".", "/")
    path = SRC / (relative + ("/__init__.py" if module == "claude_multi.cli" else ".py"))
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(found) != 1:
        raise AssertionError(f"{module}.{name} resolves {len(found)} times")
    return found[0]


def doctor_block_families() -> list[tuple[str, str]]:
    """``(where, signature)`` for every string a doctor BLOCK list receives."""

    families: list[tuple[str, str]] = []
    for module, name, index in DOCTOR_BLOCK_FUNCTIONS:
        where = f"{module}:{name}"
        for node in ast.walk(_function(module, name)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("append", "extend")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "problems"
                and node.args
            ):
                signature = _first_signature(node.args[0])
                if signature is not None:
                    families.append((where, signature))
            elif isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
                element = node.value.elts[index]
                lists = [element] if isinstance(element, ast.List) else [
                    side for side in (getattr(element, "left", None), getattr(element, "right", None))
                    if isinstance(side, ast.List)
                ]
                for item in (e for lst in lists for e in lst.elts):
                    signature = _first_signature(item)
                    if signature is not None:
                        families.append((where, signature))
    for module, name in DOCTOR_BLOCK_HELPERS:
        for node in ast.walk(_function(module, name)):
            if isinstance(node, ast.Return) and node.value is not None:
                signature = _first_signature(node.value)
                if signature is not None:
                    families.append((f"{module}:{name}", signature))
    return families


# A chunk shorter than this, or one of these generic phrases, names no single
# doctor BLOCK: nearly every remedy names a claude-multi command, and "session
# record" / "is unreadable" recur across families. Such a chunk counts only as
# part of a whole-fragment skeleton match (``_covered``).
SPECIFIC_CHUNK = 15
GENERIC_CHUNKS = frozenset({
    "claude-multi", "claude-multi doctor", "— run claude-multi doctor", "session record",
    "is unreadable", "is missing", "doctor", "restart required",
})
PLACEHOLDER = re.compile(r"<[^<>]+>|…")


def _cheatsheet_fragments(text: str | None = None) -> list[str]:
    source = _text("CHEATSHEET.md") if text is None else text
    return [" ".join(fragment.split()) for fragment in vocab.FRAGMENT.findall(source)]


def _cheatsheet_chunks(text: str | None = None) -> list[str]:
    """The specific constant chunks of the CHEATSHEET's “…” fragments."""

    chunks = []
    for fragment in _cheatsheet_fragments(text):
        for chunk in PLACEHOLDER.split(fragment):
            chunk = chunk.strip()
            if len(chunk) >= SPECIFIC_CHUNK and chunk not in GENERIC_CHUNKS:
                chunks.append(chunk)
    return chunks


def _skeletons(text: str | None = None) -> list[str]:
    """Whole fragments with each placeholder as ``{}`` (at least 10 constant characters)."""

    skeletons = []
    for fragment in _cheatsheet_fragments(text):
        constant = PLACEHOLDER.sub("", fragment).strip()
        if len(constant) >= vocab.FRAGMENT_MIN_CHUNK and constant not in GENERIC_CHUNKS:
            skeletons.append(PLACEHOLDER.sub("{}", fragment).strip())
    return skeletons


def _covered(signature: str, chunks: list[str], skeletons: list[str] = ()) -> bool:
    """A specific chunk lies inside one constant part of the family (or the
    part, itself specific, inside the chunk), or a whole fragment skeleton
    lies inside the family's signature."""

    flat = " ".join(signature.split())
    parts = [" ".join(p.split()) for p in signature.split("{}")]
    return any(
        chunk in part or (len(part) >= SPECIFIC_CHUNK and part not in GENERIC_CHUNKS and part in chunk)
        for part in parts for chunk in chunks
    ) or any(skeleton in flat for skeleton in skeletons)


def _missing(text: str | None = None) -> list[str]:
    chunks, skeletons = _cheatsheet_chunks(text), _skeletons(text)
    return [
        f"{where}: {signature!r}"
        for where, signature in doctor_block_families()
        if not _covered(signature, chunks, skeletons)
        and not any(key in signature for key in DOCTOR_BLOCK_EXCLUSIONS)
    ]


class DoctorBlockCoverageTests(unittest.TestCase):
    def test_every_block_family_has_a_row_or_an_exclusion(self) -> None:
        self.assertGreaterEqual(len(doctor_block_families()), 20)
        self.assertEqual(_missing(), [])

    def test_a_generic_overlap_does_not_cover(self) -> None:
        chunks, skeletons = _cheatsheet_chunks(), _skeletons()
        for signature in (
            "a brand-new gateway failure {} — run `claude-multi doctor --repair`",
            "totally new BLOCK: session record corrupt",
            "session record {} is corrupt: {}",
            "something {} is unreadable: {}",
        ):
            with self.subTest(signature):
                self.assertFalse(_covered(signature, chunks, skeletons))
        self.assertTrue(_covered("session record {} is unreadable: {}", chunks, skeletons))

    def test_deleting_a_row_uncovers_its_family(self) -> None:
        text = _text("CHEATSHEET.md")
        for anchor in (
            "“compiled hook target”", "“leaves out the loopback addresses while a proxy is set”",
            "“ask the policy owner, or use plain claude”",
            "token mismatch”", "“gateway key slots unusable”", "“gateway continuity set unreadable”",
            "“which the compiled fence does not admit”", "“identity is repair-needed”",
            "“scope differs from the record-authoritative compile”",
            "“the contract override is invalid and was IGNORED”", "“session record <id> is unreadable”",
            "“has an unresolved native fork”", "“collides with the managed cm-* namespace”",
            "“managed Claude binary: <error>”", "(sentinel missing) — restart required”",
            "“local gateway: <error> — <fix>”", "“local gateway key file: <error>”",
            "“named bindings:”", "“state marker:”",
            "“the running gateway does not serve rendered selector”",
            "“the on-disk gateway config differs from a fresh render",
            "“gateway managed for root <A>; this command used <B>”",
        ):
            with self.subTest(anchor):
                rows = [line for line in text.splitlines() if anchor in line]
                self.assertEqual(len(rows), 1, anchor)
                self.assertNotEqual(_missing(text.replace(rows[0] + "\n", "")), [])

    def test_exclusions_are_not_stale(self) -> None:
        signatures = [signature for _where, signature in doctor_block_families()]
        for key in DOCTOR_BLOCK_EXCLUSIONS:
            with self.subTest(key):
                self.assertTrue(any(key in signature for signature in signatures))

    def test_the_walk_sees_the_known_families(self) -> None:
        signatures = " ".join(signature for _where, signature in doctor_block_families())
        for anchor in ("sentinel missing", "token mismatch", "does not serve rendered selector",
                       "differs from a fresh render", "gateway key slots unusable",
                       "which the compiled fence does not admit", "collides with the managed cm-* namespace",
                       "has an unresolved native fork", "identity is repair-needed",
                       "managed Claude binary: ", "state marker: ", "the contract override is invalid",
                       "gateway managed for root {}; this command used {}"):
            self.assertIn(anchor, signatures)


# ------------------------------------------------------ help and hint


# ------------------------------------------------------ help and hint


class HelpLinkTests(unittest.TestCase):
    def test_epilog_names_the_shipped_cheatsheet(self) -> None:
        # The packaged resources are the default asset root; the checkout
        # keeps the docs in docs/ (the installation's documents).
        self.assertEqual(cli.default_asset_root(), RESOURCES_ROOT)
        cheatsheet = doc_path("CHEATSHEET.md")
        self.assertTrue(cheatsheet.is_file())
        epilog = cli.build_parser().epilog or ""
        self.assertIn(f"Symptom → command: {cheatsheet}", epilog)
        self.assertIn(f"Documentation map: {doc_path('USAGE.md')}", epilog)
        self.assertIn("/cm shows or changes the lineup (then /reload-plugins)", epilog)

    def test_help_keeps_the_path_unbroken(self) -> None:
        cheatsheet = str(doc_path("CHEATSHEET.md"))
        with mock.patch.dict(os.environ, {"COLUMNS": "60"}):
            text = cli.build_parser().format_help()
        self.assertIn(cheatsheet, text.splitlines()[-2])
        self.assertTrue(text.rstrip("\n").splitlines()[-1].startswith("Documentation map: "))

    def test_pointer_falls_back_to_the_name_by_role(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cm-docs-") as root:
            with mock.patch('claude_multi.layout.installation', return_value=Path(root)):
                epilog = cli.build_parser().epilog or ""
        self.assertIn("Symptom → command: CHEATSHEET.md in the claude-multi docs", epilog)
        self.assertNotIn(root, epilog)

    def test_parser_strings(self) -> None:
        parser = cli.build_parser()
        self.assertEqual(
            parser.description.splitlines()[0],
            "Launch Claude Code with a durable multi-model lineup: a saved profile, "
            "an unsaved profile file, or a direct lead.",
        )
        self.assertIs(parser.formatter_class, argparse.RawDescriptionHelpFormatter)
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))  # noqa: SLF001
        direct = next(c for c in sub._choices_actions if c.dest == "direct")  # noqa: SLF001
        self.assertEqual(
            direct.help,
            "launch one model as the lead, with no cm-* agents (recorded and "
            "resumable like every session)",
        )

    def test_quick_help_hint(self) -> None:
        lines = cli.QUICK_HELP.splitlines()
        question = lines.index("? — this help, then the workflow guarantees below.")
        self.assertEqual(lines[question + 1], tui.CHEATSHEET_HINT)
        # Within the first page down at 80x24: the card's _ScrollModal wraps
        # at 72 columns and shows box_h - 5 = 19 body rows per page (the
        # Get started and Profiles lines come first).
        wrapped = cli._wrap_help_lines(cli.QUICK_HELP, 72)  # noqa: SLF001
        self.assertLess(wrapped.index(tui.CHEATSHEET_HINT), 2 * 19)
        self.assertEqual(tui.CHEATSHEET_HINT, "Symptom → command: CHEATSHEET.md (claude-multi --help prints its path).")

    def test_sessions_help_hint(self) -> None:
        self.assertEqual(cli.SESSIONS_HELP.splitlines()[-1], tui.CHEATSHEET_HINT)
        self.assertEqual(cli.SESSIONS_HELP.count(tui.CHEATSHEET_HINT), 1)


class PackagingTests(unittest.TestCase):
    def test_installed_pages_link_only_to_installed_pages(self) -> None:
        # Every relative link of a page under docs/ resolves inside docs/,
        # which the package installs as share/claude-multi with the same
        # relative paths; the root pages are reached by their public URL.
        from _layout import pyproject

        installed = {source for files in pyproject()["tool"]["setuptools"]["data-files"].values() for source in files}
        for name in DOCS_TREE:
            with self.subTest(name):
                for target in _links(name):
                    self.assertIn(f"docs/{target}", installed, f"{name} links {target}")

    def test_the_help_pointers_name_the_manuals_at_the_top(self) -> None:
        from _layout import pyproject
        from claude_multi import layout

        documents = pyproject()["tool"]["setuptools"]["data-files"]["share/claude-multi"]
        self.assertEqual(layout.DOCUMENTS, ("USAGE.md", "CHEATSHEET.md", "STANDALONE.md"))
        self.assertEqual(documents[:3], [f"docs/{name}" for name in layout.DOCUMENTS])
        text = _text("nix/package.nix")
        self.assertIn("dataFiles = pyproject.tool.setuptools.data-files;", text)
        self.assertIn(
            'description = "claude-multi: durable multi-model launcher for Claude Code, with its '
            'gateway and onboarding tools";',
            text,
        )
        for name in layout.DOCUMENTS:
            self.assertTrue(doc_path(name).is_file(), name)

    def test_sandbox_greps_the_help_for_the_path(self) -> None:
        self.assertIn(
            '"$package/bin/claude-multi" --help | grep -qF "$package/share/claude-multi/CHEATSHEET.md"',
            _text("tests/default.nix"),
        )
        # The Nix package's installed documents: the same closure check as
        # the wheel's and the bundles'.
        self.assertIn('tests/check_docs_vocabulary.py" --installed "$package/share/claude-multi"',
                      _text("tests/default.nix"))


# ------------------------------------------------------ page anchors


class PageAnchorTests(unittest.TestCase):
    """Facts the pages share with the launcher's own text."""

    def test_usage_is_the_map(self) -> None:
        flat = _flat(_text("USAGE.md"))
        for anchor in ("[CHEATSHEET.md](CHEATSHEET.md)", "[Quickstart](quickstart.md)",
                       "`claude-multi --help` prints where these pages are installed",
                       "Any one provider is enough for a working setup",
                       f"{vocab.PUBLIC_REPOSITORY}/blob/main/CONTRIBUTING.md"):
            self.assertIn(anchor, flat)

    def test_the_map_defines_the_terms_the_card_help_uses(self) -> None:
        # The concepts page and the card's ? help share one vocabulary.
        terms = cli.QUICK_HELP.split("Terms\n", 1)[1].split("\n\n", 1)[0]
        section = _section(_text("USAGE.md"), "Terms")
        for line in terms.splitlines():
            names = line.strip().split(" — ", 1)[0]
            for name in re.split(r";\s*|\s*/\s*", names):
                name = name.strip()
                with self.subTest(name):
                    self.assertTrue(name)
                    self.assertIn(f"**{name}**", section.replace("**follow / pin**", "**follow** **pin**"))

    def test_the_lineup_guide_carries_the_cm_grammar_and_the_model_rules(self) -> None:
        text = _text("guides/lineup.md")
        self.assertIn(lineup.HELP_LINE, text)
        flat = _flat(text)
        for line in lineup.MODEL_HELP_LINES:
            self.assertIn(_flat(line), flat)
        for anchor in ("`claude-multi` must be on the session's PATH", "one argument", "`--its-exited`",
                       "`--relaunch`", "LIVE", "RELAUNCH", "follow", "pin", "`/cm fallback <provider>`",
                       "`--preview` writes nothing", "`/cm review [high-stakes] [<range>]`"):
            self.assertIn(anchor, flat)

    def test_the_sessions_guide_keys_match_the_landed_help(self) -> None:
        text = _text("guides/sessions.md")
        keys = _section(text, "The sessions screen")
        for key, _word in cli.SESSIONS_KEYBAR:
            if key in ("?", "Esc"):
                continue
            self.assertRegex(keys, rf"\n\| {re.escape(key)} \|")
        for key in ("E", "M", "P"):
            self.assertRegex(keys, rf"\n\| {key} \|")
        marks = _flat(cli.SESSIONS_HELP.replace(tui.CHEATSHEET_HINT, "")).split("Marks: ", 1)[1]
        for mark in re.findall(r"[●◐○↻⚠!]", marks):
            self.assertIn(mark, keys)
        flat = _flat(text)
        for anchor in ("`Session <id>` line per session", "`→ runtime <runtime-id>`", "$CLAUDE_MULTI_MANAGED_ID",
                       "`claude-multi sessions resolve-fork <id> <fork-id>`", "--all-dead",
                       "claude-multi doctor --repair-all", "the transcript stays"):
            self.assertIn(anchor, flat)

    def test_the_profiles_guide_carries_the_workflow_guarantees(self) -> None:
        flat = _flat(_text("guides/profiles.md"))
        bullets = tui.WORKFLOW_GUARANTEE_PANEL.split("\n  · ")[1:]
        self.assertEqual(len(bullets), 3)
        for bullet in bullets:
            self.assertIn(_flat(bullet), flat)
        for anchor in ("`default_profile`", "claude-multi profile default --clear", "**F** shows only fallback",
                       "claude-multi profile starter", ".pre-reseed-", "saving is refused and nothing is written"):
            self.assertIn(anchor, flat)

    def test_readme(self) -> None:
        flat = _flat(_text("README.md"))
        for anchor in ("[docs/quickstart.md](docs/quickstart.md)", "[docs/CHEATSHEET.md](docs/CHEATSHEET.md)",
                       "[CONTRIBUTING.md](CONTRIBUTING.md)", "[SECURITY.md](SECURITY.md)",
                       "[docs/reference/licenses.md](docs/reference/licenses.md)",
                       "A platform is supported only with a completed native install journey",
                       "not affiliated with", "neither a Python installation nor a preinstalled Claude Code",
                       "https://github.com/nkoturovic/claude-multi/releases/latest/download/install.sh",
                       "lead set", "MIT ([LICENSE](LICENSE))",
                       "does not guarantee suppressing Claude Code's own background traffic"):
            self.assertIn(anchor, flat)
        self.assertNotRegex(flat, r"\d,\d{3} (offline )?tests")  # no dated counts

    def test_the_release_urls_are_the_packaged_ones(self) -> None:
        product = json.loads(_text("packaging/product.json"))
        latest = product["release_latest_url"]
        base = product["release_base_url"]
        self.assertTrue(latest.startswith(vocab.PUBLIC_REPOSITORY + "/releases/"))
        self.assertTrue(base.startswith(vocab.PUBLIC_REPOSITORY + "/releases/"))
        for name in ("README.md", "install/linux.md", "install/macos.md"):
            with self.subTest(name):
                urls = re.findall(r"https://github\.com/[^\s`)]+/releases/[^\s`)]+", _text(name))
                self.assertTrue(urls, name)
                for url in urls:
                    self.assertTrue(url == latest.removesuffix("/download")
                                    or url.startswith((latest + "/", base.split("{version}")[0])), url)

    def test_nix_entry_points_and_release_pin(self) -> None:
        text = _text("install/nix.md")
        # source-tree.nix deliberately omits flake.nix to preserve the
        # staged-tree boundary; the public page is still checked everywhere.
        flake = _text("flake.nix") if (REPO_ROOT / "flake.nix").is_file() else None
        for output in ("packages.<system>.default", "packages.<system>.claude-multi",
                       "apps.<system>.default", "apps.<system>.claude-multi",
                       "apps.<system>.claude-multi-proxy", "devShells.<system>.default"):
            self.assertIn(f"`{output}`", text)
        for system in ("x86_64-linux", "aarch64-linux", "aarch64-darwin"):
            if flake is not None:
                self.assertIn(f'"{system}"', flake)
            self.assertIn(f"`{system}`", text)
        if flake is not None:
            for definition in ('default = self.packages.${system}.claude-multi;',
                               'claude-multi = app pkg "claude-multi"',
                               'claude-multi-proxy = app pkg "claude-multi-proxy"'):
                self.assertIn(definition, flake)
        from claude_multi import __version__

        self.assertIn(f"github:nkoturovic/claude-multi/v{__version__}", text)

    def test_nix_entry_point_check_accepts_staging_but_checks_an_existing_flake(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch(f"{__name__}.REPO_ROOT", Path(root)):
            self.test_nix_entry_points_and_release_pin()
            (Path(root) / "flake.nix").write_text("not the documented outputs\n")
            with self.assertRaises(AssertionError):
                self.test_nix_entry_points_and_release_pin()

    def test_nix_notices_live_in_the_gateway_store_output(self) -> None:
        text = _flat(_text("reference/licenses.md"))
        for anchor in ("separate gateway store output", "share/cli-proxy-api/licenses/",
                       "share/cli-proxy-api/sbom.cdx.json"):
            self.assertIn(anchor, text)
        self.assertIn('ln -s "${cliProxyApiBin}" $out/libexec/claude-multi/cli-proxy-api',
                      _text("nix/package.nix"))
        self.assertIn('"$out/share/cli-proxy-api/sbom.cdx.json"', _text("nix/gateway.nix"))

    def test_update_card_is_release_age_not_a_network_check(self) -> None:
        from claude_multi import release_update

        text = _flat(_text("update.md"))
        for anchor in ("not a background check for a newer version",
                       f"{release_update.AGE_NOTICE_DAYS} days",
                       "updated through your Nix flake", "Update plan:",
                       "only when they change"):
            self.assertIn(anchor, text)

    def test_nix_uninstall_quotes_the_kept_program_instruction(self) -> None:
        from claude_multi.setup import texts

        text = _flat(_text("uninstall.md"))
        self.assertIn(texts.UNINSTALL_NIX, text)
        self.assertIn("Nix service link", text)

    def test_changelog_states_current_format_and_accepted_cli_aliases(self) -> None:
        from claude_multi import sessions

        text = _flat(_text("CHANGELOG.md"))
        self.assertIn(f"State format is {sessions.SUPPORTED_STATE_VERSION}", text)
        for old, public in (("--composition", "--profile"),
                            ("--composition-file", "--profile-file"),
                            ("compose delete", "profile rm"),
                            ("compose restore-default", "profile reseed balanced")):
            self.assertIn(f"`{old}`", text)
            self.assertIn(f"`{public}`", text)
        self.assertIn("remain accepted aliases", text)
        self.assertIn("presets are not vendor-tested", text)
        self.assertIn("only in the Nix channel", text)

    def test_publication_status_and_private_reporting(self) -> None:
        from claude_multi import __version__

        readme = _flat(_text("README.md"))
        self.assertIn(f"**First release:** {__version__}", readme)
        self.assertIn("GitHub Releases", readme)
        self.assertNotIn("not released yet", readme)
        security = _flat(_text("SECURITY.md"))
        self.assertIn("private vulnerability reporting", security)
        self.assertIn("acknowledgement within a week", security)

    def test_standalone(self) -> None:
        flat = _flat(_text("STANDALONE.md"))
        self.assertIn(_flat(lineup.MODEL_HELP_LINES[0]), flat)
        for anchor in ("own copy", "never writes `~/.claude/settings.json`", "`CLAUDE_CONFIG_DIR`",
                       "Managed sessions unset it", "does not promise that your installation is current",
                       "`CLAUDE_MULTI_SECRET_ENV` when set (the supervised gateway service ignores it)",
                       "`~/.config/claude-multi/secret-file.json`",
                       "`~/.config/claude-multi/secrets/provider-keys.env`",
                       "[the client skew notes](reference/compatibility.md#client-skew)",
                       "`claude-multi sessions link <id> --profile <name>`"):
            self.assertIn(anchor, flat)
        # Bounded noninterference, not "unaffected".
        self.assertNotRegex(flat, r"(?i)\bunaffected\b|nothing writes")

    def test_windows_bootstrap_documents_url_bound_checksums(self) -> None:
        flat = _flat(_text("install/windows-wsl2.md"))
        for anchor in ("checksum applies only to the exact installer URL embedded in `install.ps1`",
                       "`-InstallerUrl`", "templated URL changed by `-Version`",
                       "supply `-InstallerSha256` from an authenticated release",
                       "refuses to download or run an installer without a checksum"):
            self.assertIn(anchor, flat)

    def test_contributor_guide_documents_test_build_release_refusals(self) -> None:
        flat = _flat(_section(_text("CONTRIBUTING.md"), "Build a release"))
        for anchor in ("`--test-build` records `test_build: true` in the checksummed `MANIFEST.json`",
                       "even when production trust and release URLs are present",
                       "refused by `sign`, production-trust `verify-draft` and `publish`",
                       "`verify-draft --allowed-signers FILE`", "explicit test-key verification"):
            self.assertIn(anchor, flat)

    def test_install_guides_link_the_licences_and_notices(self) -> None:
        guides = sorted(doc_path("install").glob("*.md"))
        self.assertGreaterEqual(len(guides), 4)
        for guide in guides:
            with self.subTest(guide=guide.name):
                self.assertIn("[reference/licenses.md](../reference/licenses.md)", guide.read_text())

    def test_key_storage_names_both_credential_bearing_files(self) -> None:
        text = _flat(_section(_text("providers/api-keys.md"), "3. Where the key is kept"))
        for anchor in ("private key file", "`~/.config/claude-multi/config.yaml`",
                       "Both files contain credentials", "never share either file",
                       "../privacy.md#what-stays-on-your-computer", "../privacy.md#diagnostics-you-share"):
            self.assertIn(anchor, text)
        self.assertNotIn("never in claude-multi's other files", text)

    def test_settings_paths_distinguish_config_and_home_roots(self) -> None:
        text = _text("reference/settings.md")
        flat = _flat(text)
        self.assertIn("Relative `XDG_CONFIG_HOME` and `XDG_STATE_HOME` values are ignored", flat)
        self.assertIn("`$XDG_CONFIG_HOME/claude-multi`", flat)
        self.assertIn("`$XDG_STATE_HOME/claude-multi`", flat)
        for name in ("choices.json", "settings.json", "preferences.json"):
            self.assertRegex(text, rf"`~/.config/claude-multi/{re.escape(name)}`[^\n]*\*\*Config root\*\*")
        rows = _rows(_section(text, "Other configuration"))
        for name in ("providers.d/<id>.json", "operator-ledger.json", "endpoint.json", "continuity.json"):
            with self.subTest(name=name):
                self.assertTrue(any(row.startswith(f"| `{name}` | HOME-relative |") for row in rows))
        for name in ("profiles/<name>.json", "bindings.json"):
            self.assertTrue(any(row.startswith(f"| `{name}` | Config root |") for row in rows))
        self.assertIn("Every row uses the **State root**", _section(text, "State"))

    def test_installers_are_signed_but_not_authenticated_by_running_them(self) -> None:
        for name in ("install/linux.md", "install/macos.md"):
            with self.subTest(name=name):
                flat = _flat(_text(name))
                self.assertIn("release signature covers the installer script", flat)
                self.assertIn("checksum in `SHA256SUMS`", flat)
                self.assertIn("running the script does not authenticate it first", flat)
                self.assertNotIn("not covered by the release signature", flat)

    def test_doctor_discloses_housekeeping_and_the_read_only_mode(self) -> None:
        from claude_multi.cli.parser import build_parser

        parser = build_parser()
        commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        help_text = _flat(commands.choices["doctor"].format_help())
        for name, text in (("help", help_text), ("troubleshooting", _flat(_text("troubleshooting.md"))),
                           ("reference", _flat(_text("reference/cli.md")))):
            with self.subTest(surface=name):
                self.assertIn("refreshes the helper shims", text)
                self.assertIn("doctor --json", text)
                self.assertIn("read-only report", text)
                self.assertNotIn("never changes anything", text)

    def test_the_wsl_guide_shares_doctor_wording(self) -> None:
        from claude_multi import scope
        from claude_multi.cli import gateway_facts

        text = _text("install/windows-wsl2.md")
        for name in ("install/windows-wsl2.md", "troubleshooting.md"):
            with self.subTest(name=name):
                self.assertIn(gateway_facts.WSL_SEPARATE_CONFIG, _text(name))
        for rule in scope.WSL_SECRET_PATH_DENIES:
            path = rule.split("(", 1)[1].rstrip(")")
            self.assertIn(f"`{path}`", text)
        flat = _flat(text)
        for anchor in ("never runs `wsl --unregister`", "native Windows is not supported", "not a sandbox",
                       "drive C mounted at `/mnt/c` only", "on a Windows drive"):
            self.assertIn(anchor, flat.replace("Native Windows", "native Windows"))

    def test_the_compatibility_page_states_the_pin_and_its_limits(self) -> None:
        contract = json.loads((RESOURCES_ROOT / "catalog" / "native-contract.json").read_text())
        pinned = [entry["version"] for entry in contract["verified"]]
        flat = _flat(_text("reference/compatibility.md"))
        for version in pinned:
            self.assertIn(f"Claude Code {version}", flat)
        for anchor in ("checks only the copy's size and metadata", "full hash",
                       "the per-prompt check can stay quiet after an upgrade",
                       "no way to run a client that does not match the pin"):
            self.assertIn(anchor, flat)

    def test_the_privacy_page_states_the_traffic_default(self) -> None:
        flat = _flat(_text("privacy.md"))
        self.assertIn("claude-multi does not guarantee suppressing Claude Code background traffic", flat)
        for anchor in ("`session_env_keep`", "the gateway's local key", "allowlist"):
            self.assertIn(anchor, flat)

    def test_privacy_distinguishes_listing_feed_probe_and_generation_requests(self) -> None:
        rows = _rows(_section(_text("privacy.md"), "Data flows"))
        for destination in ("a provider's model-listing endpoint", "the public third-party model feed",
                            "your configured name resolver"):
            row = next((row for row in rows if destination in row), "")
            self.assertTrue(row.startswith("| the launcher |"), destination)
        probe = next(row for row in rows if "name resolver" in row)
        self.assertIn("no HTTP request, prompts or credentials", probe)
        update = next(row for row in rows if "release location" in row)
        self.assertIn("metadata when you run update or check", update)
        self.assertIn("bundle after confirmation", update)
        self.assertIn("CLAUDE_CODE_DISABLE_FAST_MODE=1", _text("privacy.md"))
        self.assertNotIn("so no fast-mode check is sent", _text("privacy.md"))

    def test_networking_distinguishes_transport_configuration(self) -> None:
        text = _flat(_text("guides/networking.md"))
        for anchor in ("disables environment proxies for these requests",
                       "the environment's proxy settings", "direct DNS resolution and TCP connection",
                       "values from the installing terminal are copied into the unit",
                       "unit adds no certificate override",
                       "LAN reachability check is separate"):
            self.assertIn(anchor, text)
        self.assertIn("urllib.request.ProxyHandler({})", _text("src/claude_multi/release_update.py"))

    def test_contributing_says_what_is_safe_to_attach(self) -> None:
        flat = _flat(_text("CONTRIBUTING.md"))
        for anchor in ("`claude-multi doctor --json`", "`claude-multi --version`", "allowlist",
                       "Never attach", "https://github.com/nkoturovic/claude-multi"):
            self.assertIn(anchor, flat)

    def test_security_states_both_installer_trust_modes(self) -> None:
        flat = _flat(_text("SECURITY.md"))
        for anchor in ("With `ssh-keygen` (OpenSSH 8.1 or newer) installed",
                       "a failed or missing signature stops the install",
                       "Only when `ssh-keygen` is not installed", "checksums built into that installer",
                       "`claude-multi update` always verifies the signature",
                       "never changes the keys the installed updater trusts", "inputs you trust"):
            self.assertIn(anchor, flat)
        installer = _text("packaging/install.sh")
        self.assertIn("a failed or missing signature is never replaced by the", installer)

    def test_the_published_fingerprint_is_the_packaged_release_key(self) -> None:
        # The fingerprint the security pages publish is the SHA-256 of the
        # key blob of the production line the installations carry (base64,
        # unpadded, as ssh-keygen -l prints it), and the line docs/security.md
        # shows is that line.
        import base64
        import hashlib

        from claude_multi import trust

        packaged = (RESOURCES_ROOT / "release-trust" / "allowed_signers").read_text(encoding="utf-8")
        (signer,) = [signer for signer in trust.parse_allowed_signers(packaged)
                     if signer.principals == "release@claude-multi"]
        self.assertEqual(signer.namespaces, "claude-multi-release")
        line = packaged.splitlines()[signer.line - 1]
        blob = base64.b64decode(line.split()[-1])
        self.assertEqual(blob, signer.key_blob)
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        for name in ("SECURITY.md", "security.md"):
            with self.subTest(name):
                published = re.findall(r"SHA256:[A-Za-z0-9+/]{43}", _text(name))
                self.assertEqual(set(published), {fingerprint})
        self.assertIn(f"\n{line}\n", _text("security.md"))

    def test_agents(self) -> None:
        flat = _flat(_text("AGENTS.md"))
        for anchor in ("on the session's PATH", "`CHEATSHEET.md`", "[`CONTRIBUTING.md`](CONTRIBUTING.md)",
                       "`<state>/lineup-log/<id>.log`",
                       "`upstream served model \"<served>\" for requested model \"<requested>\"`",
                       "never a hand edit of the record", "three console entry points"):
            self.assertIn(anchor, flat)
        self.assertNotIn("until then this file wins", flat)


class OpenAICompatibleDocsTests(unittest.TestCase):
    """The keyed OpenAI-compatible page follows the release's audit state."""

    def test_the_example_declaration_is_agent_eligible_after_qualification(self) -> None:
        import shlex

        from claude_multi import operator, profile
        from claude_multi.cli.commands import models

        text = _text("providers/openai-compatible.md")
        commands = re.findall(r"^claude-multi .+$", text, re.MULTILINE)
        command = next(command for command in commands if " models add " in command)
        args = cli.build_parser().parse_args(shlex.split(command)[1:])
        provider = {"transport": {"kind": "direct-openai", "auth": {"kind": "bearer"}}}
        efforts, default = models._efforts(args, provider)
        declaration = {"wire_model": args.wire, "display": args.display or args.wire,
                       "efforts": efforts, "default_effort": default,
                       "context": {"declared_tokens": args.context, "source": args.source,
                                   "source_ref": args.source_ref}}
        edit = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
        if edit is not None:
            declaration.update(json.loads(edit[1]))
        entry = operator.derive_core_entry(
            args.key, declaration, args.provider, provider, migrated=False,
            gateway_baseline="7.3.15", file="example.json")
        # Synthetic successful approval, admission and qualification: this
        # checks the documented declaration, not a vendor or live evidence.
        facts = profile.AgentFacts(
            key=args.key, provider=args.provider, admitted=True, route="approved", d60=True,
            route_kind=operator.KEYED_KIND, family="unknown", t1_families=frozenset(),
            evidence="current", tools_variants=frozenset({"forced"}))
        for slot in ("cm-analyst", "cm-reviewer"):
            with self.subTest(slot=slot):
                verdict = profile.agent_eligibility(
                    entry, key=args.key, slot=slot, effort=default, facts=facts,
                    agent_efforts=profile.declared_efforts(entry))
                self.assertTrue(verdict.eligible, verdict.reasons)
        steps = [f"claude-multi models {verb} {args.key}" for verb in ("edit", "admit", "qualify")]
        for step in (steps[0], steps[1], steps[2] + " --agents"):
            self.assertIn(step, commands)
        self.assertLess(commands.index(command), commands.index(steps[0]))
        self.assertLess(commands.index(steps[0]), commands.index(steps[1]))
        self.assertLess(commands.index(steps[1]), commands.index(steps[2] + " --agents"))
        self.assertIn("never inflate it", _flat(text))

    def test_the_page_matches_the_audit_state(self) -> None:
        from claude_multi import catalog

        gateway = json.loads((RESOURCES_ROOT / "catalog" / "gateway.json").read_text())
        open_ = gateway["audits"][catalog.KEYED_COMPAT_AUDIT]
        text = _text("providers/openai-compatible.md")
        setup = re.search(r"claude-multi providers add \S+ --kind openai-compatible\b", text)
        if open_:
            self.assertIsNotNone(setup, "the audit is open: the page gives the setup journey")
        else:
            self.assertIn(catalog.KEYED_AUDIT_CLOSED, text)
            self.assertIsNone(setup, "while the audit is closed the page gives no unusable setup commands")
            for alternative in ("anthropic-compatible.md", "openrouter.md", "lan.md"):
                self.assertIn(alternative, text)
        for name in ("CHEATSHEET.md", "reference/compatibility.md"):
            self.assertIn("openai-compatible.md", _text(name))


class ProviderRouteDocsTests(unittest.TestCase):
    """The provider pages follow the release's provider data: every shipped
    keyed provider, every preset (with its address, key name and
    availability), the OpenAI API-key route's facts, the route matrix and
    the dated cost references. Derived from the packaged data, never a
    hand-copied list."""

    @staticmethod
    def _catalog_providers() -> dict:
        return json.loads((RESOURCES_ROOT / "catalog" / "providers.json").read_text())["providers"]

    def test_the_provider_table_lists_every_keyed_provider(self) -> None:
        from claude_multi import operator

        table = _section(_text("providers/api-keys.md"), "Providers in this release")
        for pid, provider in self._catalog_providers().items():
            transport = provider["transport"]
            ref = (transport.get("auth") or {}).get("secret_ref")
            if ref is None:
                continue
            with self.subTest(pid):
                row = next((row for row in _rows(table) if f"`{ref.removeprefix('env:')}`" in row), None)
                self.assertIsNotNone(row, pid)
                self.assertIn(f"`{transport['base_url']}`", row)
        for (pid, _choice), alternative in operator.TRANSPORT_ALTERNATIVES.items():
            if not alternative.available:
                continue
            with self.subTest(f"{pid} API key"):
                row = next((row for row in _rows(table) if f"`{alternative.secret_name}`" in row), None)
                self.assertIsNotNone(row, pid)
                self.assertIn(f"`{alternative.base_url}`", row)

    def test_every_preset_is_documented_where_its_route_is(self) -> None:
        from claude_multi import operator

        found = operator.presets(RESOURCES_ROOT)
        self.assertTrue(found)
        pages = {"anthropic-compatible": "providers/anthropic-compatible.md",
                 operator.KEYED_KIND: "providers/openai-compatible.md",
                 operator.LAN_KIND: "providers/lan.md"}
        for name, info in found.items():
            with self.subTest(name):
                self.assertEqual(info.support, "preset")
                table = _section(_text(pages[info.kind]), "Presets")
                row = next((row for row in _rows(table) if row.startswith(f"| `{name}` |")), None)
                self.assertIsNotNone(row, f"{name} has no row in {pages[info.kind]}")
                if info.secret_name is not None:
                    self.assertIn(f"`{info.secret_name}`", row)
                if not info.generic:
                    self.assertIn(f"`{info.base_url}`", row)

    def test_the_presets_of_a_closed_route_read_as_unavailable(self) -> None:
        from claude_multi import catalog
        from claude_multi.setup import texts

        gateway = json.loads((RESOURCES_ROOT / "catalog" / "gateway.json").read_text())
        presets = _flat(_section(_text("providers/openai-compatible.md"), "Presets"))
        quickstart = _flat(_section(_text("quickstart.md"), "4. Connect one provider"))
        if catalog.keyed_compat_audited(gateway):
            self.assertNotIn("not available in this release", presets)
        else:
            self.assertIn(texts.OPENAI_COMPAT_CLOSED_NOTE, presets)
            self.assertIn("not available in this release", presets)
            self.assertIn("listed, not available in this release", quickstart)

    def test_the_openai_key_section_states_the_route_facts(self) -> None:
        from claude_multi import operator

        alternative = operator.TRANSPORT_ALTERNATIVES[("openai", operator.TRANSPORT_API_KEY)]
        section = _flat(_section(_text("providers/api-keys.md"), "OpenAI"))
        for anchor in (f"`{alternative.secret_name}`", f"`{alternative.base_url}`",
                       "claude-multi providers transport openai api-key",
                       "claude-multi providers transport openai oauth-pool",
                       "per-request output cap is not applied", "the model's documented maximum output",
                       "https://developers.openai.com/api/docs/guides/spend-limits",
                       "never both", "fixed local text", "refuses `api.openai.com`"):
            self.assertIn(anchor, section)
        lines = json.loads((RESOURCES_ROOT / "catalog" / "models.json").read_text())["models"]
        reviewed = sorted(key for key, entry in lines.items()
                          if entry.get("provider") == "openai" and entry.get("key_route")
                          and entry.get("status") == "active")
        self.assertTrue(reviewed)
        for key in reviewed:
            self.assertIn(f"(`{key}`)", section)
        self.assertIn("api-keys.md#openai", _text("providers/chatgpt-account.md"))
        # The route matrix and the transports table say it too.
        for name in ("quickstart.md", "reference/compatibility.md"):
            with self.subTest(name):
                self.assertRegex(_flat(_text(name)), r"per-request output cap is not\s+applied")
        self.assertIn("https://developers.openai.com/api/docs/guides/spend-limits", _text("quickstart.md"))

    def test_the_quickstart_matrix_covers_every_route_class(self) -> None:
        text = _section(_text("quickstart.md"), "4. Connect one provider")
        flat = _flat(text)
        self.assertIn("| Route | Credential | In the launcher | From a terminal | Alone gives you |", text)
        self.assertIn("Every route is optional", flat)
        self.assertIn("any single provider gives you a working setup", flat)
        rows = _rows(text)
        routes = [row.split("|")[1].strip() for row in rows]
        for needle in ("account", "Anthropic or OpenAI API key", "DeepSeek, Kimi, Meta, Qwen", "OpenRouter",
                       "Anthropic-compatible endpoint (Z.ai", "OpenAI-compatible endpoint (Groq",
                       "your own Anthropic-compatible endpoint", "your own OpenAI-compatible endpoint",
                       "a server on your network"):
            self.assertTrue(any(needle in route for route in routes), needle)
        # Anthropic-compatible endpoints before OpenAI-compatible ones.
        own = [i for i, route in enumerate(routes) if route.startswith("your own")]
        self.assertIn("Anthropic-compatible", routes[own[0]])
        self.assertIn("OpenAI-compatible", routes[own[1]])
        # One singleton example per route class, each a complete journey.
        examples = _section(text, "One provider, start to finish")
        for anchor in ("claude-multi providers set-key deepseek", "claude-multi providers sign-in anthropic",
                       "claude-multi setup --answers <file>", "claude-multi providers add --preset zai",
                       "claude-multi models admit custom-example", "claude-multi profile starter --apply",
                       "claude-multi doctor --first-run"):
            self.assertIn(anchor, examples)

    def test_cost_references_are_official_and_dated(self) -> None:
        for name, heading in (("providers/api-keys.md", "6. Costs and limits"),
                              ("providers/openrouter.md", "6. Costs and limits"),
                              ("providers/claude-account.md", "6. Limits and quota"),
                              ("providers/chatgpt-account.md", "6. Limits and quota")):
            with self.subTest(name):
                section = _section(_text(name), heading)
                self.assertRegex(section, r"checked on 20\d\d-\d\d-\d\d")
                self.assertRegex(section, r"\]\(https://[^)\s]+\)")
                # Prices are linked, never quoted.
                self.assertNotRegex(section, r"\$\s?\d")
        table = _section(_text("providers/api-keys.md"), "6. Costs and limits")
        # Every shipped provider takes an API key, directly or as its
        # account's alternative, so every one has its cost row.
        for pid, provider in self._catalog_providers().items():
            with self.subTest(pid):
                self.assertTrue(any(row.startswith(f"| {provider['display']} |") for row in _rows(table)), pid)


# ------------------------------------------------------ cleanups


class CleanupTests(unittest.TestCase):
    def test_dead_2x_edit_helpers_are_gone(self) -> None:
        for name in ("dumb_edit_guidance", "DUMB_EDIT_BANNER", "DUMB_EDIT_COMMAND_INTRO"):
            self.assertFalse(hasattr(tui, name), name)

    def test_doctor_footer_names_profiles(self) -> None:
        source = "\n".join(p.read_text(encoding="utf-8") for p in (SRC / "cli").rglob("*.py"))
        self.assertNotIn("Catalog, compositions, and local gateway are valid", source)
        # One closing line, shared by claude-multi doctor and the card's H.
        self.assertEqual(source.count("Catalog, profiles, and local gateway are valid"), 1)


class EarlierRenderCompatibilityTests(unittest.TestCase):
    """The gateway config an earlier release published stays readable: after
    an update, the served-change plan compares the new render with the one
    on disk, which that earlier release wrote. The fixture is such a render
    (synthetic key values, neutral host names)."""

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "render" / "earlier-release-config.yaml"

    def test_the_served_change_plan_reads_an_earlier_render(self):
        from claude_multi import served_plan

        text = self.FIXTURE.read_text()
        document = served_plan.parse_restricted_yaml(text)
        routes = served_plan.routes_from_document(document)
        self.assertTrue(routes)
        self.assertEqual({route.section for route in routes.values()},
                         {served_plan.SECTION_OAUTH, served_plan.SECTION_KEY, "openai-compatibility"})
        self.assertEqual(served_plan.gateway_label(document), "127.0.0.1:8317")
        for value in ("dummy-provider-secret", "dummy-gateway-token"):
            self.assertIn(value, text)
            self.assertNotIn(value, repr(routes))

    def test_the_published_identity_of_an_earlier_render(self):
        from claude_multi import proxy, state

        with tempfile.TemporaryDirectory(prefix="cm-earlier-render-") as tmp:
            home = Path(tmp)
            target = state.ensure_private_dir(proxy.config_dir(home)) / "config.yaml"
            state.atomic_write(target, self.FIXTURE.read_bytes())
            published = proxy.published_identity(home)
        self.assertIsNone(published.unknown)
        self.assertTrue(published.routes)
        self.assertNotIn("dummy-provider-secret", repr(published))


if __name__ == '__main__':
    unittest.main()
