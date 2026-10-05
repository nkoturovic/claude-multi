"""Tests for native built-in agent policy compilation."""

from __future__ import annotations

import itertools
import unittest
from pathlib import Path

from claude_multi import catalog, compiler, strict_json
from _catalog import FIXTURE_ROOT


CATALOG_ROOT = FIXTURE_ROOT


class PolicyMatrixTests(unittest.TestCase):
    def test_full_matrix(self) -> None:
        explores = ("replace", "native", "off")
        plans = ("native", "off")
        general = ("on", "off")
        for explore, plan, gp in itertools.product(explores, plans, general):
            with self.subTest(explore=explore, plan=plan, general_purpose=gp):
                env, denies = compiler.compile_native_policy(
                    {"explore": explore, "plan": plan, "general_purpose": gp},
                    ["claude"],
                )
                both_non_native = explore != "native" and plan != "native"
                if both_non_native:
                    self.assertEqual(
                        env, {"CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS": "1"}
                    )
                    self.assertNotIn("Agent(Explore)", denies)
                    self.assertNotIn("Agent(Plan)", denies)
                else:
                    self.assertEqual(env, {})
                    if explore != "native":
                        self.assertIn("Agent(Explore)", denies)
                    else:
                        self.assertNotIn("Agent(Explore)", denies)
                    if plan != "native":
                        self.assertIn("Agent(Plan)", denies)
                    else:
                        self.assertNotIn("Agent(Plan)", denies)
                if gp == "off":
                    self.assertIn("Agent(general-purpose)", denies)
                else:
                    self.assertNotIn("Agent(general-purpose)", denies)
                # stable deny order: Explore, Plan, general-purpose, aliases
                expected: list[str] = []
                if not both_non_native and explore != "native":
                    expected.append("Agent(Explore)")
                if not both_non_native and plan != "native":
                    expected.append("Agent(Plan)")
                if gp == "off":
                    expected.append("Agent(general-purpose)")
                expected.append("Agent(claude)")
                self.assertEqual(denies, expected)

    def test_generic_aliases_sorted_and_deduplicated(self) -> None:
        _, denies = compiler.compile_native_policy(
            {"explore": "native", "plan": "native", "general_purpose": "on"},
            ["claude", "beta", "claude"],
        )
        self.assertEqual(denies, ["Agent(beta)", "Agent(claude)"])

    def test_generic_alias_never_duplicates_builtin_deny(self) -> None:
        _, denies = compiler.compile_native_policy(
            {"explore": "replace", "plan": "native", "general_purpose": "off"},
            ["general-purpose", "Explore"],
        )
        self.assertEqual(denies, ["Agent(Explore)", "Agent(general-purpose)"])

    def test_no_aliases_preserves_builtin_only_order(self) -> None:
        env, denies = compiler.compile_native_policy(
            {"explore": "replace", "plan": "native", "general_purpose": "off"}
        )
        self.assertEqual(env, {})
        self.assertEqual(denies, ["Agent(Explore)", "Agent(general-purpose)"])

    def test_default_seed_policy(self) -> None:
        env, denies = compiler.compile_native_policy(
            {"explore": "replace", "plan": "native", "general_purpose": "off"},
            ["claude"],
        )
        self.assertEqual(env, {})
        self.assertEqual(
            denies, ["Agent(Explore)", "Agent(general-purpose)", "Agent(claude)"]
        )

    def test_both_disabled_uses_env_var(self) -> None:
        env, denies = compiler.compile_native_policy(
            {"explore": "off", "plan": "off", "general_purpose": "on"}
        )
        self.assertEqual(env, {"CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS": "1"})
        self.assertEqual(denies, [])

    def test_no_same_name_shadowing(self) -> None:
        # The generated agent ids of every fixture seed
        # (3.0 agent files are named by role id), not a 2.x resolution.
        from claude_multi import profile

        bundle = catalog.load_catalog(CATALOG_ROOT)
        lcat = profile.LineupCatalog.from_docs(bundle.docs)
        for name, seed in bundle.seed_profiles.items():
            evaluation = profile.evaluate(seed, lcat)
            self.assertIsNotNone(evaluation.lineup, (name, evaluation.errors))
            generated_ids = set(evaluation.lineup.agents) | {"cm-lead"}
            for builtin in ("Explore", "Plan", "general-purpose"):
                with self.subTest(seed=name, builtin=builtin):
                    self.assertNotIn(builtin, generated_ids)


if __name__ == "__main__":
    unittest.main()
