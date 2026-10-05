"""Roles v2, the one-prompt-per-function prompts and the legacy roles view.

Supersedes the 2.x four-role contract: ten role
ids (``cm-lead`` plus nine agent ids) carrying ``function``/``grade``, one
prompt file per function, and a legacy (v1) roles view for the 2.x readers
that remain test inputs.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from claude_multi import catalog, strict_json
from claude_multi.catalog import CatalogError

from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog


PROMPT_FILES = (
    "cm-analyst.md",
    "cm-designer.md",
    "cm-explorer.md",
    "cm-implementer.md",
    "cm-lead.md",
    "cm-reviewer.md",
)


def _raw() -> dict:
    return catalog.load_raw(FIXTURE_ROOT)


def _roles(raw: dict) -> dict:
    return raw["docs"]["roles"]["roles"]


def _copy_tree(case: unittest.TestCase) -> Path:
    temporary = Path(tempfile.mkdtemp(prefix="claude-multi-roles-"))
    case.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
    root = temporary / "assets"
    shutil.copytree(FIXTURE_ROOT, root)
    for root_dir, _dirs, files in os.walk(root):
        os.chmod(root_dir, 0o755)
        for name in files:
            os.chmod(Path(root_dir) / name, 0o644)
    return root


class RolesV2ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = catalog.load_catalog(FIXTURE_ROOT)
        cls.roles = cls.bundle.roles_v2

    def test_id_set(self) -> None:
        self.assertEqual(set(self.roles), set(catalog.ROLE_IDS))
        self.assertEqual(len(catalog.ROLE_IDS), 10)
        self.assertNotIn("cm-reviewer-light", self.roles)

    def test_per_id_rules(self) -> None:
        implementers = {"cm-implementer-light", "cm-implementer", "cm-implementer-strong"}
        read_only = {"cm-explorer", "cm-reviewer", "cm-reviewer-strong"}
        for role_id, role in self.roles.items():
            with self.subTest(role=role_id):
                function, grade = role["function"], role["grade"]
                spelled = f"cm-{function}" + ("" if grade == "plain" else f"-{grade}")
                self.assertEqual(role_id, spelled)
                self.assertEqual(
                    role["requires"],
                    [f"cm-{function}"] if grade in ("light", "strong") else [],
                )
                self.assertEqual(
                    role["isolation"], "worktree" if role_id in implementers else None
                )
                self.assertEqual(
                    role["disallowed_tools"],
                    list(catalog.READ_ONLY_TOOLS) if role_id in read_only else [],
                )
                self.assertEqual(role["prompt_file"], f"prompts/cm-{function}.md")

    def test_read_only_tools_order(self) -> None:
        self.assertEqual(
            catalog.READ_ONLY_TOOLS, ("Edit", "Write", "NotebookEdit", "Agent", "Skill")
        )

    def test_read_only_roles_deny_the_skill_vector(self) -> None:
        # A skill can fork an agent outside the lineup, and a
        # frontmatter `Skill(<name>)` entry removes the whole tool at the
        # pinned client (spike S13), so every read-only role denies `Skill`
        # and no other role does. S13 proves the compiled behaviour.
        for role_id, role in self.roles.items():
            with self.subTest(role=role_id):
                self.assertEqual(
                    "Skill" in role["disallowed_tools"],
                    role["function"] in catalog.READ_ONLY_FUNCTIONS,
                )
                self.assertFalse(
                    any("(" in tool for tool in role["disallowed_tools"])
                )

    def test_cost_signals(self) -> None:
        self.assertIn(
            "exact spec and a gating check; cheapest",
            self.roles["cm-implementer-light"]["description"],
        )
        for role_id in ("cm-analyst-strong", "cm-implementer-strong", "cm-reviewer-strong"):
            with self.subTest(role=role_id):
                self.assertIn("several times the cost", self.roles[role_id]["description"])


class RolesV2MutationTests(unittest.TestCase):
    """One failing mutation per rule (exact message) plus the passing case."""

    def _errors(self, mutate) -> list[str]:
        raw = _raw()
        mutate(raw)
        return catalog.validate_catalog(raw)

    def test_unmutated_fixture_passes(self) -> None:
        self.assertEqual(catalog.validate_catalog(_raw()), [])

    def test_role_set(self) -> None:
        def mutate(raw: dict) -> None:
            roles = _roles(raw)
            del roles["cm-designer"]
            light = copy.deepcopy(roles["cm-reviewer"])
            light.update(grade="light", requires=["cm-reviewer"])
            roles["cm-reviewer-light"] = light
            raw["prompt_bodies"]["cm-reviewer-light"] = raw["prompt_bodies"]["cm-reviewer"]

        self.assertEqual(
            self._errors(mutate),
            [
                f"roles: the role set must be exactly {sorted(catalog.ROLE_IDS)}; "
                "missing ['cm-designer'], unexpected ['cm-reviewer-light']"
            ],
        )

    def test_role_missing_ids_never_raise(self) -> None:
        # A dropped agent id (legacy or not) is an
        # error list, never a KeyError from the legacy view.
        for role_id in ("cm-explorer", "cm-analyst", "cm-lead"):
            with self.subTest(dropped=role_id):
                errors = self._errors(lambda raw: _roles(raw).pop(role_id))
                self.assertIn(
                    f"roles: the role set must be exactly {sorted(catalog.ROLE_IDS)}; "
                    f"missing [{role_id!r}], unexpected []",
                    errors,
                )

    def test_role_spelling(self) -> None:
        self.assertEqual(
            self._errors(lambda raw: _roles(raw)["cm-analyst-strong"].update(grade="light")),
            ["roles.cm-analyst-strong: function/grade spell cm-analyst-light, not 'cm-analyst-strong'"],
        )

    def test_role_requires(self) -> None:
        self.assertEqual(
            self._errors(lambda raw: _roles(raw)["cm-reviewer-strong"].update(requires=[])),
            ["roles.cm-reviewer-strong.requires: must be ['cm-reviewer']"],
        )

    def test_role_isolation(self) -> None:
        self.assertEqual(
            self._errors(lambda raw: _roles(raw)["cm-implementer-light"].update(isolation=None)),
            ["roles.cm-implementer-light.isolation: must be 'worktree'"],
        )

    def test_role_tools(self) -> None:
        self.assertEqual(
            self._errors(lambda raw: _roles(raw)["cm-reviewer"].update(disallowed_tools=[])),
            [
                "roles.cm-reviewer.disallowed_tools: must be "
                "['Edit', 'Write', 'NotebookEdit', 'Agent', 'Skill']"
            ],
        )
        self.assertEqual(
            self._errors(
                lambda raw: _roles(raw)["cm-explorer"].update(
                    disallowed_tools=["Edit", "Write", "NotebookEdit", "Agent"]
                )
            ),
            [
                "roles.cm-explorer.disallowed_tools: must be "
                "['Edit', 'Write', 'NotebookEdit', 'Agent', 'Skill']"
            ],
        )
        self.assertEqual(
            self._errors(
                lambda raw: _roles(raw)["cm-analyst"].update(
                    disallowed_tools=list(catalog.READ_ONLY_TOOLS)
                )
            ),
            ["roles.cm-analyst.disallowed_tools: must be []"],
        )

    def test_role_prompt_file(self) -> None:
        # A wrong but schema-valid function file,
        # mutated in memory (the schema pattern admits no hyphen after cm-).
        message = (
            "roles.cm-analyst-strong.prompt_file: must be 'prompts/cm-analyst.md' "
            "(one prompt per function)"
        )
        self.assertEqual(
            self._errors(
                lambda raw: _roles(raw)["cm-analyst-strong"].update(
                    prompt_file="prompts/cm-reviewer.md"
                )
            ),
            [message],
        )

        # As the loader would read it: the body is the reviewer's, so shared-prompt validation
        # fires as well.
        def as_loaded(raw: dict) -> None:
            _roles(raw)["cm-analyst-strong"]["prompt_file"] = "prompts/cm-reviewer.md"
            raw["prompt_bodies"]["cm-analyst-strong"] = raw["prompt_bodies"]["cm-reviewer"]

        errors = self._errors(as_loaded)
        self.assertIn(message, errors)
        self.assertIn(
            "roles.cm-analyst-strong: prompt body differs from 'cm-analyst' "
            "(one prompt per function)",
            errors,
        )

    def test_role_schema_rejects_hyphenated_prompt_file(self) -> None:
        root = _copy_tree(self)
        path = root / "catalog" / "roles.json"
        document = strict_json.load(path)
        document["roles"]["cm-analyst-strong"]["prompt_file"] = "prompts/cm-analyst-strong.md"
        path.write_bytes(strict_json.pretty_file_bytes(document))
        with self.assertRaisesRegex(CatalogError, r"catalog/roles.json: .*prompt_file"):
            catalog.load_raw(root)

    def test_role_identity_names(self) -> None:
        self.assertEqual(
            self._errors(
                lambda raw: _roles(raw)["cm-explorer"].update(description="Use the sol line.")
            ),
            ["roles.cm-explorer.description: names a model, provider or family ('sol')"],
        )
        # Word-bounded and case-insensitive; the generic exclusions pass.
        self.assertEqual(
            self._errors(
                lambda raw: _roles(raw)["cm-explorer"].update(
                    description="Resolve console meta data; local and custom checks."
                )
            ),
            [],
        )
        self.assertEqual(
            self._errors(
                lambda raw: _roles(raw)["cm-explorer"].update(description="Ask ANTHROPIC.")
            ),
            ["roles.cm-explorer.description: names a model, provider or family ('anthropic')"],
        )

    def test_role_id_pattern(self) -> None:
        def mutate(raw: dict) -> None:
            _roles(raw)["CM_Bad"] = copy.deepcopy(_roles(raw)["cm-analyst"])
            raw["prompt_bodies"]["CM_Bad"] = raw["prompt_bodies"]["cm-analyst"]

        errors = self._errors(mutate)
        self.assertIn("roles: invalid role ID 'CM_Bad'", errors)
        self.assertIn(
            f"roles: the role set must be exactly {sorted(catalog.ROLE_IDS)}; "
            "missing [], unexpected ['CM_Bad']",
            errors,
        )


class RolesDataVersionTests(unittest.TestCase):
    def test_v1_roles_refused_with_the_version_message(self) -> None:
        root = _copy_tree(self)
        path = root / "catalog" / "roles.json"
        document = strict_json.load(path)
        document["version"] = 1
        path.write_bytes(strict_json.pretty_file_bytes(document))
        with self.assertRaises(CatalogError) as caught:
            catalog.load_raw(root)
        self.assertEqual(
            str(caught.exception),
            "catalog/roles.json: unsupported data version 1 "
            "(this launcher reads roles v2, catalog 33+)",
        )

    def test_v2_roles_load(self) -> None:
        self.assertEqual(_raw()["docs"]["roles"]["version"], catalog.ROLES_DATA_VERSION)


class LegacyRolesViewTests(unittest.TestCase):
    """There is no legacy roles view: ``docs["roles"]`` is the raw roles v2
    document, aliased as ``roles-v2``."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = catalog.load_catalog(FIXTURE_ROOT)

    def test_raw_v2_is_the_hashed_document(self) -> None:
        self.assertIs(self.bundle.docs["roles"], self.bundle.docs["roles-v2"])
        self.assertIs(self.bundle.docs["roles-v2"], self.bundle.bundle["roles"])
        self.assertEqual(self.bundle.bundle["roles"], _raw()["docs"]["roles"])
        self.assertIs(self.bundle.roles, self.bundle.roles_v2)


class PromptIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = catalog.load_catalog(FIXTURE_ROOT)

    def test_prompt_bodies_cover_every_role(self) -> None:
        self.assertEqual(set(self.bundle.prompt_bodies), set(catalog.ROLE_IDS))

    def test_one_prompt_per_function(self) -> None:
        by_function: dict[str, set[bytes]] = {}
        for role_id, role in self.bundle.roles_v2.items():
            by_function.setdefault(role["function"], set()).add(
                self.bundle.prompt_bodies[role_id]
            )
        self.assertEqual(set(by_function), set(catalog.ROLE_FUNCTIONS))
        for function, bodies in by_function.items():
            with self.subTest(function=function):
                self.assertEqual(len(bodies), 1)
        plain = [self.bundle.prompt_bodies[f"cm-{f}"] for f in catalog.ROLE_FUNCTIONS]
        self.assertEqual(len(set(plain)), len(plain))

    def test_bodies_are_the_prompt_files(self) -> None:
        for role_id, role in self.bundle.roles_v2.items():
            with self.subTest(role=role_id):
                body = self.bundle.prompt_bodies[role_id]
                self.assertEqual(
                    body, (FIXTURE_ROOT / "catalog" / role["prompt_file"]).read_bytes()
                )
                self.assertTrue(body.strip())
                self.assertTrue(body.endswith(b"\n"))
                self.assertEqual(
                    self.bundle.bundle["prompts"][role_id],
                    "sha256:" + strict_json.sha256_hex(body),
                )

    def test_prompt_files_are_exactly_six(self) -> None:
        self.assertEqual(
            tuple(sorted(p.name for p in (FIXTURE_ROOT / "catalog" / "prompts").iterdir())),
            PROMPT_FILES,
        )


BRANDS = (
    "opus fable sonnet haiku gpt codex chatgpt kimi qwen glm grok deepseek muse astra sol "
    "luna moonshot alibaba anthropic openai xai claude gemini llama"
).split()
# "`/cm profile claude`" names the shipped seed profile (a catalog profile
# name, not a binding): the lead prompt's exhaustion escape quotes the exact
# operator command.
ALLOWLISTED_PHRASES = ("claude-multi", "Claude Code", "`/cm profile claude`")


@uses_shipped_catalog
class PromptNoIdentityNamesTests(unittest.TestCase):
    """No model, provider, family or product name in prompts or role text."""

    @staticmethod
    def _tokens() -> set[str]:
        tokens = set(BRANDS)
        for root in (FIXTURE_ROOT, SHIPPED_ROOT):
            tokens |= set(catalog.identity_tokens(catalog.load_raw(root)["docs"]))
        return tokens - set(catalog.IDENTITY_TOKEN_EXCLUSIONS)

    def _assert_clean(self, where: str, text: str, tokens: set[str]) -> None:
        for phrase in ALLOWLISTED_PHRASES:
            text = text.replace(phrase, "")
        for token in sorted(tokens):
            match = re.search(rf"\b{re.escape(token)}\b", text, re.IGNORECASE)
            self.assertIsNone(match, f"{where} names {token!r}: {match!r}")

    def test_prompts_and_descriptions_name_no_identity(self) -> None:
        tokens = self._tokens()
        for root in (SHIPPED_ROOT, FIXTURE_ROOT):
            for name in PROMPT_FILES:
                path = root / "catalog" / "prompts" / name
                with self.subTest(path=str(path)):
                    self._assert_clean(str(path), path.read_text(encoding="utf-8"), tokens)
            roles = strict_json.load(root / "catalog" / "roles.json")["roles"]
            for role_id, role in roles.items():
                with self.subTest(root=str(root), role=role_id):
                    self._assert_clean(f"{root} roles.{role_id}", role["description"], tokens)


def _prompt(name: str) -> str:
    return (FIXTURE_ROOT / "catalog" / "prompts" / name).read_text(encoding="utf-8")


class _PromptChecks:
    """Mixin: the required and forbidden substrings of one prompt."""

    NAME = ""
    REQUIRED: tuple[str, ...] = ()
    FORBIDDEN: tuple[str, ...] = ()

    def test_required_and_forbidden_substrings(self) -> None:
        text = _prompt(self.NAME)
        for needle in self.REQUIRED:
            with self.subTest(required=needle):
                self.assertIn(needle, text)
        for needle in self.FORBIDDEN:
            with self.subTest(forbidden=needle):
                self.assertNotIn(needle, text)


class LeadPromptTests(_PromptChecks, unittest.TestCase):
    NAME = "cm-lead.md"
    REQUIRED = (
        "# cm-lead",
        "Spawn only `cm-*` agent types.",
        "Never pass a per-invocation model override",
        "Trust the lineup notice with the highest `lineup_generation`",
        "Every workflow `agent()` call names a `cm-*` `agentType`.",
        "`isolation: 'worktree'`",
        "never SendMessage-continue an agent spawned under an earlier `lineup_generation`",
        "A `-strong` agent bound to your own model at the same or lower effort adds a "
        "clean context and parallelism, not capability: do such work yourself or use "
        "the plain grade.",
        "For this comparison an `ultracode` lead counts at its model's default effort.",
        "use the same function's other grade",
        # The exhaustion and reroute rules.
        "is an\n  announced substitution",
        "exhausted binding, and announce the reroute.",
        "This applies only when that\n  grade is bound to a different provider; otherwise follow the provider rule\n  under Failure handling.",
        "recovery rule below and the grade reroute above",
        "When agents on one provider fail on quota or rate limits (429)",
        "`/cm profile claude`",
        "`/cm set <agent>=<model>:<effort>`",
        "`/reload-plugins`",
        "this overrides the\n  recovery rule below",
        "An agent spawned under an earlier\n  `lineup_generation` is never continued",
        "with one reviewer per change",
        "both reviewers in parallel on the same diff in the same",
        "Stop after two review rounds",
        "A normal `cm-implementer-light` change gets no review",
        "unless they press `s` (this session only)",
        "Follow the review routing table",
        "Pick the function first",
        "Then pick the grade by how precisely you can specify the task",
        # The general skill rule and the fallback escape.
        "Do not invoke a skill that launches agents outside the bound `cm-*`\n  lineup",
        "A denied or unavailable role\n  is not permission to substitute a generic agent.",
        "`/cm review`",
        "`/cm fallback <provider>`",
    )
    FORBIDDEN = (
        "inisher",
        "preferred variant",
        "exact generated IDs",
        "routing hint",
        "variant",
    )

    def test_failure_handling_probe_text(self) -> None:
        text = _prompt(self.NAME).split("## Failure handling", 1)[1]
        for line in (
            'An agent that died with "Prompt is too long" is not continued: a continuation dies the same way; dispatch a fresh agent with a narrower scope and the facts it already found.',
            '- A "Concurrent subagent limit" tool error is a harness cap, not an infrastructure death: never continue it under the recovery rule; wait for running agents to finish or do the step yourself. Delegation nests at most three levels below you; agents at the third level have no Agent tool.',
            "- A foreground agent on a rate-limited provider can block you silently for as long as the provider's Retry-After (hours); dispatch quota-prone providers in the background and check on them.",
        ):
            self.assertEqual(text.count(line), 1)
        self.assertNotIn("one-shot finalize", text)
        self.assertNotIn("When the failure IS the context", text)

    def test_workflow_agents_are_never_continued(self) -> None:
        # The exclusion lives in the recovery bullet.
        text = _prompt(self.NAME).split("## Failure handling", 1)[1]
        recovery = next(
            bullet for bullet in text.split("\n- ")
            if bullet.startswith("You own delegated-agent recovery.")
        )
        flat = " ".join(recovery.split())
        for sentence in (
            "An agent a workflow runs is never continued either: the workflow owns "
            "its retries, and a message to one that has ended revives it outside "
            "the workflow, next to the workflow's own retry.",
            "Message a workflow agent only while it runs; when one is blocked on "
            "something outside its scope, either fix the condition from outside and "
            "leave the retry to the workflow, or stop the workflow first and take "
            "the step over — never both.",
        ):
            self.assertEqual(flat.count(sentence), 1)
        self.assertIn(
            "continue the agent (current lineup generation only, never a workflow agent)",
            " ".join(text.split()),
        )

    def test_round_two_after_a_workflow_review(self) -> None:
        # A workflow reviewer's transcript is not addressable.
        review = _prompt(self.NAME).split("## Review", 1)[1].split("\n## ", 1)[0]
        self.assertIn(
            "A reviewer that ran inside a workflow cannot be continued (its "
            "transcript is not addressable): its round 2 is a fresh spawn of the "
            "same agent type, given the round-1 findings.",
            " ".join(review.split()),
        )

    def test_first_line(self) -> None:
        self.assertEqual(_prompt(self.NAME).split("\n", 1)[0], "# cm-lead — integration owner")


NEVER_WAIT = "Never end your turn waiting for a reply. To ask the lead something, send\n  the message and keep "
BLOCKED_EXIT = "finish with status `blocked` and the\n  exact need."
SKILL_BAN = (
    "Never invoke a skill that spawns agents (such as `code-review`): its\n"
    "  agents run outside the lineup."
)


class ReviewerPromptTests(_PromptChecks, unittest.TestCase):
    NAME = "cm-reviewer.md"
    REQUIRED = (
        NEVER_WAIT + "reviewing, or " + BLOCKED_EXIT,
        "you have no edit, write, agent or skill tools",
        "BLOCKER, MAJOR, MINOR or NIT",
        "concrete failure scenario",
        "suggested fix",
        "Verify every finding before you report it",
        "APPROVE or REVISE",
        "Only verified BLOCKER or MAJOR findings can produce REVISE; MINOR and NIT "
        "findings never block.",
        "In round 2 check only the fixes",
        "Never call EnterWorktree",
        "git -C",
    )
    FORBIDDEN = (
        "inisher",
        "report every edit",
        "review artifact",
        "delegate",
        "Spawn",
        "must-fix",
        "reject",
    )


class ExplorerPromptTests(_PromptChecks, unittest.TestCase):
    NAME = "cm-explorer.md"
    REQUIRED = (
        "Report facts, not",
        "Cite every fact",
        "You are read-only.",
        "Never call EnterWorktree",
        NEVER_WAIT + "exploring, or " + BLOCKED_EXIT,
        "You cannot spawn agents or invoke skills",
    )
    FORBIDDEN = ("delegate", "Spawn only")


class DesignerPromptTests(_PromptChecks, unittest.TestCase):
    NAME = "cm-designer.md"
    REQUIRED = (
        "UX guidelines",
        "Report first",
        "final assistant message is the report deliverable",
        "Spawn only `cm-*` agent types",
        NEVER_WAIT + "working, or " + BLOCKED_EXIT,
        SKILL_BAN,
    )


class AnalystPromptTests(_PromptChecks, unittest.TestCase):
    NAME = "cm-analyst.md"
    REQUIRED = (
        "read-mostly",
        "final assistant message is the report deliverable",
        "evidence",
        "may delegate",
        "git -C",
        "Never call EnterWorktree",
        "Spawn only `cm-*` agent types",
        "not valid substitutes",
        "refused by ownership",
        NEVER_WAIT + "working, or " + BLOCKED_EXIT,
        SKILL_BAN,
    )
    FORBIDDEN = ("variant", "reconnaissance", "-light", "-strong")


class ImplementerPromptTests(_PromptChecks, unittest.TestCase):
    NAME = "cm-implementer.md"
    REQUIRED = (
        "bounded",
        "worktree",
        "commit",
        "validate",
        "Descendants share these same boundaries",
        "base ref",
        "uncommitted, or mixed",
        "Leave the worktree in place",
        "git -C <path>",
        "EnterWorktree is not",
        "Spawn only `cm-*` agent types",
        "not valid substitutes",
        "refused by ownership",
        "gating check",
        NEVER_WAIT + "working, or " + BLOCKED_EXIT,
        SKILL_BAN,
    )
    FORBIDDEN = ("variant", "-light", "-strong")


if __name__ == "__main__":
    unittest.main()
