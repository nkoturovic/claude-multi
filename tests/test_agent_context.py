"""Agents at full context: one rule decides each role's context class and
effective window from the session's compiled window/percent policy.

The session window is min(ceiling, the lead set's smallest provider bound).
An agent on a 1M-class line whose provider bound is at least that window
keeps ``[1m]`` and runs at the window like the lead; one whose bound is below
it uses the same alias without ``[1m]`` (the client's 200K class), so it never
overflows its route. Lines are chosen by shape from the frozen fixture (never
a shipped id); a few shipped-catalog checks derive everything from the loaded
catalog.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import shutil
import unittest
from pathlib import Path
from typing import Any

import _v4
import bless
import test_screens_catalog
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog
from claude_multi import catalog, choices, compiler, custom, profile, readiness, scope, sessions, settings, state, strict_json
from claude_multi import views

CEILING = settings.WINDOW_CEILING_DEFAULT
WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
PERCENT_ENV = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
BELOW = 500_000  # a provider bound below the default session window


# ------------------------------------------------------------------ fixture shapes


def _bundle():
    return catalog.load_catalog(FIXTURE_ROOT)


def _lcat(docs) -> profile.LineupCatalog:
    return profile.LineupCatalog.from_docs(docs)


def _eff(lcat, doc=None, **fields):
    eff = settings.effective(doc or {"version": 1}, provider_ids=lcat.providers, line_keys=lcat.lines)
    return dataclasses.replace(eff, **fields) if fields else eff


def _gateway_agent_line(docs, adapter: str) -> str:
    """A 1M-class, large, lead- and agents-capable gateway-effort line on a
    provider with ``adapter`` that admits every role."""

    lines = docs["models"]["models"]
    providers = docs["providers"]["providers"]
    for key in sorted(lines):
        entry = lines[key]
        if (providers[entry["provider"]]["adapter"] == adapter and isinstance(entry["efforts"], dict)
                and {"lead", "agents"} <= set(entry["capabilities"]) and entry["roles"] == "all"
                and entry["context"]["client_tokens"] == 1_000_000
                and entry["context"]["ordinary_profile"] == "large"):
            return key
    raise AssertionError(f"the fixture has no 1M gateway-effort line on {adapter}")


def codex_line(docs) -> str:
    return _gateway_agent_line(docs, "cliproxy-oauth-codex-v1")


def openrouter_line(docs) -> str:
    """The aggregator (declared-family) gateway-effort line of that shape."""

    lines = docs["models"]["models"]
    for key in sorted(lines):
        entry = lines[key]
        if "family" in entry and isinstance(entry["efforts"], dict) and entry["roles"] == "all" \
                and entry["context"]["client_tokens"] == 1_000_000 \
                and entry["context"]["ordinary_profile"] == "large" and "agents" in entry["capabilities"]:
            return key
    raise AssertionError("the fixture has no 1M aggregator line")


def seed_lead(bundle) -> tuple[str, str]:
    """The balanced seed's lead (key, provider)."""

    key = bundle.seed_profiles["balanced"]["lead"]["model"]
    return key, bundle.docs["models"]["models"][key]["provider"]


def lan_lead(docs) -> str:
    """A lead-capable line on a keyless LAN route."""

    lines = docs["models"]["models"]
    providers = docs["providers"]["providers"]
    for key in sorted(lines):
        entry = lines[key]
        if "lead" in entry["capabilities"] and readiness.is_lan(providers[entry["provider"]]):
            return key
    raise AssertionError("the fixture has no LAN lead line")


def sub_1m_agent_line(docs) -> str:
    """An agents-capable gateway-effort line below the 1M client class."""

    lines = docs["models"]["models"]
    for key in sorted(lines):
        entry = lines[key]
        if "agents" in entry["capabilities"] and isinstance(entry["efforts"], dict) \
                and entry["context"]["client_tokens"] < 1_000_000:
            return key
    raise AssertionError("the fixture has no sub-1M agent line")


def bounded(docs, key: str, provider_tokens: int):
    """A docs copy whose line ``key`` has ``provider_tokens`` as its bound."""

    docs = copy.deepcopy(docs)
    context = docs["models"]["models"][key]["context"]
    context["provider_tokens"] = provider_tokens
    context["validated_tokens"] = min(context["validated_tokens"], provider_tokens)
    return docs


def write_bounded_assets(dest: Path, key_of, provider_tokens: int) -> tuple[Path, str]:
    """A private copy of the fixture asset root whose line ``key_of(docs)``
    has ``provider_tokens`` as its bound (Runtime-level tests load it)."""

    shutil.copytree(FIXTURE_ROOT, dest)
    for directory, _dirs, files in os.walk(dest):  # a store copy is read-only
        os.chmod(directory, 0o755)
        for name in files:
            os.chmod(Path(directory) / name, 0o644)
    path = dest / "catalog" / "models.json"
    document = strict_json.loads(path.read_bytes())
    key = key_of({"models": document, "providers": strict_json.loads(
        (dest / "catalog" / "providers.json").read_bytes())})
    wrapped = bounded({"models": document}, key, provider_tokens)
    path.write_bytes(strict_json.pretty_file_bytes(wrapped["models"]))
    catalog.load_catalog(dest)  # still a valid catalog
    return dest, key


def narrowed_balanced(bundle) -> dict[str, Any]:
    """The balanced seed with the lead set narrowed to the lead's provider,
    so its agent lines on other providers are outside the lead set."""

    document = copy.deepcopy(bundle.seed_profiles["balanced"])
    document.pop("seed", None)
    document["name"] = "narrow"
    document["lead_providers"] = [seed_lead(bundle)[1]]
    return document


def agents_on(document, key: str, docs, *slots: str) -> dict[str, Any]:
    document = copy.deepcopy(document)
    effort = docs["models"]["models"][key]["default_effort"]
    for slot in slots:
        document["agents"][slot] = {"model": key, "effort": effort}
    return document


def scope_plan(bundle, docs, lcat, eff, lineup):
    return scope.compile_lineup_scope(lineup, lcat, eff, bundle.prompt_bodies, scope.catalog_meta_v2(docs),
                                      lineup_generation=1)


def launch(bundle, docs, eff, lineup):
    return compiler.compile_lineup_launch(
        docs=docs, prompt_bodies=bundle.prompt_bodies, lineup=lineup, effective=eff,
        session_action=compiler.build_fresh(bless.FIXED_SESSION), lineup_generation=1,
        state_root=bless.V2_STATE_ROOT, scope_dir=bless.SCOPE_DIR, hook_command=bless.V2_HOOK_COMMAND,
        token_helper_command=bless.V2_TOKEN_HELPER)


def frontmatter_model(plan, rid: str) -> str:
    text = plan.agent_files[f".claude/agents/{rid}.md"].decode("utf-8")
    return next(line.removeprefix("model: ") for line in text.splitlines() if line.startswith("model: "))


def windows(lineup) -> dict[str, profile.RoleWindow]:
    return dict(profile.lineup_windows(lineup))


# ------------------------------------------------------------------ the rule


class RoleWindowRuleTests(unittest.TestCase):
    POLICY = profile.WindowPolicy(window=800_000, percent=90)

    def role(self, selector="x-high[1m]", bound=1_000_000, *, client=1_000_000, decide=True, policy=None):
        return profile.role_window(selector, client_tokens=client, provider_tokens=bound,
                                   policy=policy or self.POLICY, decide=decide)

    def test_a_bound_below_equal_to_and_above_the_session_window(self) -> None:
        below = self.role(bound=799_999)
        self.assertEqual((below.selector, below.client_class, below.window, below.narrowed),
                         ("x-high", 200_000, 200_000, True))
        self.assertEqual(below.trigger, compiler.reactive_trigger(200_000, 90))
        for bound in (800_000, 872_000, 1_000_000):
            with self.subTest(bound=bound):
                role = self.role(bound=bound)
                self.assertEqual((role.selector, role.client_class, role.window, role.narrowed),
                                 ("x-high[1m]", 1_000_000, 800_000, False))
                self.assertEqual(role.trigger, 702_000)
                self.assertLessEqual(role.trigger, bound)

    def test_a_selector_as_bound_is_never_narrowed(self) -> None:
        lead = self.role(bound=300_000, decide=False)
        self.assertEqual((lead.selector, lead.window, lead.narrowed), ("x-high[1m]", 800_000, False))
        kept = self.role("x-high", bound=1_000_000, client=200_000, decide=False)
        self.assertEqual((kept.selector, kept.client_class, kept.window), ("x-high", 200_000, 200_000))

    def test_suffixless_agents_use_the_actual_200k_class_not_declared_capacity(self) -> None:
        for declared in (128_000, 258_400, 500_000, 1_000_000):
            with self.subTest(declared=declared):
                role = self.role("y-high", bound=declared, client=declared)
                self.assertEqual((role.selector, role.client_class, role.window, role.narrowed),
                                 ("y-high", 200_000, 200_000, False))

    def test_an_exported_scalar_is_the_class_of_a_selector_without_1m(self) -> None:
        policy = profile.WindowPolicy(window=320_032, percent=90, scalar=320_032)
        narrowed = self.role(bound=300_000, policy=policy)
        self.assertEqual((narrowed.selector, narrowed.client_class, narrowed.narrowed), ("x-high", 320_032, True))
        wide = self.role(bound=900_000, policy=policy)
        self.assertEqual((wide.selector, wide.window), ("x-high[1m]", 320_032))

    def test_window_policy_over_the_lead_set(self) -> None:
        rows = [(1_000_000, 1_000_000, None), (872_000, 1_000_000, None)]
        self.assertEqual(profile.window_policy(rows, 90), profile.WindowPolicy(800_000, 90, None))
        self.assertEqual(profile.window_policy(rows, 70, ceiling=500_000), profile.WindowPolicy(500_000, 70, None))
        self.assertEqual(profile.window_policy(rows + [(600_000, 1_000_000, None)], 90).window, 600_000)
        sub = [(320_032, 320_032, None), (258_400, 258_400, 258_400)]
        self.assertEqual(profile.window_policy(sub, 90), profile.WindowPolicy(258_400, 90, 258_400))
        self.assertEqual(profile.window_policy([], 90, ceiling=400_000), profile.WindowPolicy(400_000, 90, None))

    def test_the_compiler_and_the_rule_share_the_policy(self) -> None:
        rows = [(1_000_000, 1_000_000, None), (900_000, 1_000_000, None)]
        fence = type("F", (), {"lead_class": "large", "lead_set": tuple(
            type("R", (), {"provider_tokens": p, "client_tokens": c, "scalar_tokens": s,
                           "source": "catalog", "validated_tokens": p})() for p, c, s in rows)})()
        for ceiling in (200_000, 450_000, CEILING):
            with self.subTest(ceiling=ceiling):
                context = compiler.lead_set_context(fence, 85, ceiling=ceiling)
                self.assertEqual(context.policy, profile.window_policy(rows, 85, ceiling=ceiling))
                self.assertEqual(context.window, min(ceiling, 900_000))
                self.assertEqual(context.trigger, compiler.reactive_trigger(context.window, 85))

    def test_the_window_condition_of_the_agent_gate(self) -> None:
        # A [1m] line whose bound is at least the window, or below it (the
        # 200K class), compacts within its bound; only a bound below the
        # 200K-class trigger is a problem.
        self.assertIsNone(profile.agent_window_problem("x[1m]", 872_000))
        self.assertIsNone(profile.agent_window_problem("x[1m]", BELOW))
        self.assertIsNone(profile.agent_window_problem("x[1m]", 167_000))
        problem = profile.agent_window_problem("x[1m]", 150_000)
        self.assertEqual(problem, "its agent class 200K (200000 tokens), shared process window 800000, "
                                  "effective window 200000, compacts at 167000 tokens (95%), "
                                  "above its provider bound 150000")
        at_90 = profile.WindowPolicy(window=800_000, percent=90)
        self.assertIsNone(profile.agent_window_problem("x[1m]", 165_000, policy=at_90))
        self.assertIsNotNone(profile.agent_window_problem("x[1m]", 165_000))


# ------------------------------------------------------------------ mixed bounds


class MixedBoundsTests(unittest.TestCase):
    """Agent lines outside the lead set (the lead set is narrowed to the
    lead's provider), at a bound below, equal to and above the window."""

    SLOTS = ("cm-analyst", "cm-reviewer")

    def setUp(self) -> None:
        self.bundle = _bundle()
        self.docs = self.bundle.docs
        self.codex = codex_line(self.docs)
        self.openrouter = openrouter_line(self.docs)

    def resolve(self, docs, document, **fields):
        lcat = _lcat(docs)
        eff = _eff(lcat, **fields)
        return lcat, eff, profile.resolve(document, lcat, effective=eff)

    def case(self, key: str, bound: int, **fields):
        docs = bounded(self.docs, key, bound)
        document = agents_on(narrowed_balanced(self.bundle), key, docs, *self.SLOTS)
        lcat, eff, lineup = self.resolve(docs, document, **fields)
        return docs, lcat, eff, lineup

    def test_catalog_and_openrouter_lines_below_equal_and_above_the_window(self) -> None:
        for key in (self.codex, self.openrouter):
            for bound, narrowed in ((BELOW, True), (CEILING, False), (872_000, False)):
                with self.subTest(line=key, bound=bound):
                    docs, lcat, eff, lineup = self.case(key, bound)
                    self.assertEqual(lineup.policy.window, CEILING)
                    entry = docs["models"]["models"][key]
                    line_selector = entry["efforts"][entry["default_effort"]]["selector"]
                    self.assertTrue(line_selector.endswith("[1m]"))
                    plan = scope_plan(self.bundle, docs, lcat, eff, lineup)
                    for rid in self.SLOTS:
                        binding = lineup.agents[rid].binding
                        role = windows(lineup)[rid]
                        expected = line_selector.removesuffix("[1m]") if narrowed else line_selector
                        self.assertEqual(binding.selector, expected)
                        self.assertEqual(frontmatter_model(plan, rid), expected)
                        self.assertIn(expected, plan.settings["availableModels"])
                        self.assertEqual(binding.provider_context_tokens, bound)
                        self.assertEqual(binding.proxy_contract, entry["efforts"][entry["default_effort"]]["proxy_contract"])
                        self.assertEqual(role.window, 200_000 if narrowed else CEILING)
                        self.assertEqual(binding.client_context_tokens, 200_000 if narrowed else 1_000_000)
                        self.assertLess(role.trigger, bound)
                    # The lead runs at the session window either way.
                    self.assertEqual(windows(lineup)["cm-lead"].window, CEILING)
                    self.assertEqual(plan.settings["env"][WINDOW_ENV], str(CEILING))
                    self.assertEqual(compiler.agent_context_gaps(lineup, CEILING), ())

    def test_a_narrowed_agent_needs_no_route_change(self) -> None:
        from claude_multi import render

        docs, _lcat_, _eff_, lineup = self.case(self.codex, BELOW)
        entry = docs["models"]["models"][self.codex]
        provider = docs["providers"]["providers"][entry["provider"]]
        served = render.provider_selectors(entry["provider"], provider, docs["models"]["models"], continuity={},
                                           providers=docs["providers"]["providers"])
        self.assertIn(lineup.agents["cm-analyst"].binding.selector, served)

    def test_the_lead_set_bound_sets_one_window_for_lead_and_agents(self) -> None:
        # A lead-set line with a lower bound lowers the session window for
        # the lead and every agent; an agent line at least that high keeps
        # [1m], a lower one is narrowed.
        lead_key, lead_provider = seed_lead(self.bundle)
        member = self.openrouter
        docs = bounded(bounded(self.docs, member, 600_000), self.codex, 550_000)
        document = narrowed_balanced(self.bundle)
        document["lead_providers"] = sorted({lead_provider, docs["models"]["models"][member]["provider"]})
        document = agents_on(document, self.codex, docs, "cm-analyst")
        lcat, eff, lineup = self.resolve(docs, document)
        self.assertEqual(lineup.policy.window, 600_000)
        rows = windows(lineup)
        self.assertEqual(rows["cm-lead"].window, 600_000)
        self.assertFalse(lineup.agents["cm-analyst"].binding.selector.endswith("[1m]"))
        self.assertEqual(rows["cm-analyst"].window, 200_000)
        on_lead_line = [rid for rid, agent in lineup.agents.items() if agent.binding.key == lead_key]
        self.assertTrue(on_lead_line)
        for rid in on_lead_line:
            self.assertTrue(lineup.agents[rid].binding.selector.endswith("[1m]"))
            self.assertEqual(rows[rid].window, 600_000)
        result = launch(self.bundle, docs, eff, lineup)
        self.assertEqual(result.env_set[WINDOW_ENV], "600000")
        self.assertEqual(result.scope_plan.settings["env"][WINDOW_ENV], "600000")

    def test_a_lowered_ceiling_lowers_the_lead_and_agent_windows_alike(self) -> None:
        docs, lcat, eff, lineup = self.case(self.codex, BELOW, window_ceiling=400_000)
        self.assertEqual(lineup.policy.window, 400_000)
        for slot, role in windows(lineup).items():
            with self.subTest(slot=slot):
                self.assertEqual(role.window, 400_000)
                self.assertTrue(role.selector.endswith("[1m]"))
        result = launch(self.bundle, docs, eff, lineup)
        self.assertEqual((result.env_set[WINDOW_ENV], result.env_set[PERCENT_ENV]), ("400000", "90"))
        env = result.scope_plan.settings["env"]
        self.assertEqual((env[WINDOW_ENV], env[PERCENT_ENV]), ("400000", "90"))
        context = strict_json.loads(result.scope_plan.other_files["lead-set.json"])["context"]
        self.assertEqual((context["window"], context["trigger"]), (400_000, compiler.reactive_trigger(400_000, 90)))

    def test_a_lineup_resolved_for_another_window_never_compiles(self) -> None:
        docs, _lcat_, _eff_, lineup = self.case(self.codex, BELOW, window_ceiling=400_000)
        eff = _eff(_lcat(docs))
        with self.assertRaisesRegex(compiler.CompilerError, "resolved for a session window of 400000 tokens"):
            launch(self.bundle, docs, eff, lineup)

    def test_the_workflow_default_follows_the_window(self) -> None:
        for bound, narrowed in ((BELOW, True), (872_000, False)):
            with self.subTest(bound=bound):
                docs = bounded(self.docs, self.codex, bound)
                entry = docs["models"]["models"][self.codex]
                document = narrowed_balanced(self.bundle)
                lcat, eff, lineup = self.resolve(
                    docs, document, workflow_default_binding={"model": self.codex, "effort": entry["default_effort"]})
                result = launch(self.bundle, docs, eff, lineup)
                line_selector = entry["efforts"][entry["default_effort"]]["selector"]
                expected = line_selector.removesuffix("[1m]") if narrowed else line_selector
                settings_doc = result.scope_plan.settings
                self.assertEqual(settings_doc["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], expected)
                self.assertIn(expected, settings_doc["availableModels"])
                self.assertIn(f"run on `{expected}`", result.lead_prompt)
                role = scope.workflow_default_window(lcat, eff, policy=lineup.policy)
                self.assertEqual((role.selector, role.window), (expected, 200_000 if narrowed else CEILING))
                rows = views.role_window_rows(lineup, role)
                self.assertEqual(rows[-1], views.RoleWindowRow(
                    "workflow default", expected, "200K" if narrowed else "1M", role.window, role.trigger))
                self.assertEqual(rows[0].role, "lead")

    def test_a_lan_lead_sets_the_window_for_1m_agents(self) -> None:
        lead = lan_lead(self.docs)
        small = sub_1m_agent_line(self.docs)
        document = profile.ad_hoc_direct(lead, self.docs["models"]["models"][lead]["default_effort"])
        document["name"] = "lan"
        document = agents_on(document, self.codex, self.docs, "cm-analyst")
        small_entry = self.docs["models"]["models"][small]
        slot = next(rid for rid in catalog.AGENT_ROLE_IDS if small_entry["roles"] == "all" or rid in small_entry["roles"])
        document["agents"][slot] = {"model": small, "effort": small_entry["default_effort"]}
        lcat, eff, lineup = self.resolve(self.docs, document)
        bound = self.docs["models"]["models"][lead]["context"]["provider_tokens"]
        self.assertLess(bound, CEILING)
        self.assertEqual(lineup.policy.window, bound)
        rows = windows(lineup)
        self.assertEqual(rows["cm-lead"].window, bound)
        self.assertTrue(lineup.agents["cm-analyst"].binding.selector.endswith("[1m]"))
        self.assertEqual(rows["cm-analyst"].window, bound)
        # A suffixless agent without a process scalar uses the client's 200K class.
        self.assertEqual(lineup.agents[slot].binding.selector,
                         small_entry["efforts"][small_entry["default_effort"]]["selector"])
        self.assertEqual(rows[slot].window, min(profile.CLASS_STANDARD, bound))
        result = launch(self.bundle, self.docs, eff, lineup)
        self.assertEqual(result.env_set[WINDOW_ENV], str(bound))

    def test_risky_agent_and_workflow_use_actual_shared_window_without_refusal(self) -> None:
        docs = bounded(self.docs, self.codex, 128_000)
        entry = docs["models"]["models"][self.codex]
        document = agents_on(narrowed_balanced(self.bundle), self.codex, docs, "cm-analyst")
        lcat, eff, lineup = self.resolve(
            docs, document, workflow_default_binding={"model": self.codex, "effort": entry["default_effort"]})
        role = windows(lineup)["cm-analyst"]
        self.assertEqual((role.client_class, role.window, role.trigger), (200_000, 200_000, 162_000))
        risk = next(w for w in lineup.warnings if w.code == "context-risk" and w.slot == "cm-analyst")
        for actual in ("200000", "800000", "162000", "128000"):
            self.assertIn(actual, risk.message)
        result = launch(self.bundle, docs, eff, lineup)
        self.assertIn(role.selector, result.scope_plan.settings["availableModels"])
        workflow = scope.workflow_default_window(lcat, eff, policy=lineup.policy)
        self.assertEqual(workflow.window, 200_000)
        warnings = scope.workflow_default_warnings(lcat, eff, policy=lineup.policy)
        self.assertTrue(any(w.code == "context-risk" and "128000" in w.message for w in warnings))
        self.assertNotIn(b"context overflow", result.scope_plan.other_files["lineup.md"])

    def test_legacy_suffixless_agent_never_invents_a_1m_class(self) -> None:
        registry = {"version": 1, "providers": {}, "models": {
            "legacy-wide": {"provider": "llm-local", "wire_model": "legacy-wide-wire", "context_tokens": 1_000_000}}}
        docs = custom.merge_docs(self.docs, registry)
        document = narrowed_balanced(self.bundle)
        document["agents"]["cm-analyst"] = {"model": "legacy-wide", "effort": "high"}
        lcat, eff, lineup = self.resolve(docs, document)
        binding = lineup.agents["cm-analyst"].binding
        self.assertFalse(binding.selector.endswith("[1m]"))
        self.assertEqual(binding.provider_context_tokens, 1_000_000)
        self.assertEqual(binding.client_context_tokens, 200_000)
        self.assertEqual(windows(lineup)["cm-analyst"].window, 200_000)
        result = launch(self.bundle, docs, eff, lineup)
        self.assertIn(binding.selector, result.scope_plan.settings["availableModels"])

    def test_nonpositive_compaction_triggers_remain_hard(self) -> None:
        for bound in (0, 10_000, 20_000, 30_000):
            with self.subTest(bound=bound), self.assertRaises(compiler.CompilerError):
                compiler.reactive_trigger(bound, 90)

    def test_a_custom_lead_sets_the_window_for_the_workflow_default(self) -> None:
        registry = {
            "version": 1,
            "providers": {"my-gw": {"base_url": "https://llm.example.invalid/v1", "auth_kind": "bearer",
                                    "secret_env": "MY_GW_API_KEY"}},
            "models": {"my-model": {"provider": "my-gw", "wire_model": "my-wire", "context_tokens": 262_144}},
        }
        docs = custom.merge_docs(self.docs, registry)
        lcat = _lcat(docs)
        codex = docs["models-v2"]["models"][self.codex]
        keep = {"my-gw", codex["provider"]}
        eff = _eff(lcat, {"version": 1, "providers": {pid: {"enabled": False} for pid in lcat.providers
                                                      if pid not in keep}},
                   workflow_default_binding={"model": self.codex, "effort": codex["default_effort"]})
        lineup = profile.resolve(profile.ad_hoc_direct("my-model", "high"), lcat, effective=eff, ad_hoc=True)
        self.assertEqual(lineup.policy.window, 262_144)
        result = launch(self.bundle, docs, eff, lineup)
        selector = codex["efforts"][codex["default_effort"]]["selector"]
        self.assertTrue(selector.endswith("[1m]"))
        self.assertEqual(result.scope_plan.settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], selector)
        self.assertEqual(result.env_set[WINDOW_ENV], "262144")
        role = scope.workflow_default_window(lcat, eff, policy=lineup.policy)
        self.assertEqual(role.window, 262_144)


# ------------------------------------------------------------------ compiled env


class CompiledEnvTests(unittest.TestCase):
    """The process env and the compiled settings ``env`` carry the session
    window and percent with 1M-class agents."""

    def test_1m_lead_set_with_872k_agent_lines(self) -> None:
        bundle = _bundle()
        key = codex_line(bundle.docs)
        docs = bounded(bundle.docs, key, 872_000)
        lcat = _lcat(docs)
        eff = _eff(lcat)
        lineup = profile.resolve(bundle.seed_profiles["balanced"], lcat, effective=eff)
        on_line = [rid for rid, agent in lineup.agents.items() if agent.binding.key == key]
        self.assertTrue(on_line)
        self.assertTrue(all(agent.binding.selector.endswith("[1m]") for agent in lineup.agents.values()))
        result = launch(bundle, docs, eff, lineup)
        for env in (result.env_set, result.scope_plan.settings["env"]):
            self.assertEqual((env[WINDOW_ENV], env[PERCENT_ENV]), ("800000", "90"))
        for rid in on_line:
            self.assertTrue(frontmatter_model(result.scope_plan, rid).endswith("[1m]"))
            self.assertEqual(windows(lineup)[rid].trigger, 702_000)

    def test_a_profile_percent_reaches_both_envs_and_every_role(self) -> None:
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        eff = _eff(lcat)
        document = copy.deepcopy(bundle.seed_profiles["balanced"])
        document["settings_overrides"] = {"compaction_percent": 70}
        lineup = profile.resolve(document, lcat, effective=eff)
        self.assertEqual(lineup.policy, profile.WindowPolicy(800_000, 70, None))
        result = launch(bundle, bundle.docs, eff, lineup)
        for env in (result.env_set, result.scope_plan.settings["env"]):
            self.assertEqual((env[WINDOW_ENV], env[PERCENT_ENV]), ("800000", "70"))
        self.assertEqual({role.trigger for role in windows(lineup).values()},
                         {compiler.reactive_trigger(800_000, 70)})


# ------------------------------------------------------------------ records


class RecordWindowTests(unittest.TestCase):
    def test_the_ceiling_never_enters_settings_or_a_snapshot(self) -> None:
        lcat = _lcat(_bundle().docs)
        lowered = _eff(lcat, window_ceiling=300_000)
        self.assertEqual(settings.snapshot(lowered), settings.snapshot(_eff(lcat)))
        self.assertNotIn("window_ceiling", settings.snapshot(lowered))
        restored = settings.effective_from_snapshot(settings.snapshot(lowered))
        self.assertEqual(restored.window_ceiling, CEILING)
        self.assertEqual(settings.drift(settings.snapshot(_eff(lcat)), lowered), [])
        self.assertEqual(settings.effective_from_snapshot(settings.snapshot(lowered),
                                                          window_ceiling=300_000).window_ceiling, 300_000)

    def test_recorded_window(self) -> None:
        def record(window):
            return {"applied": {"lead": {"compaction": {"window": window, "trigger": None,
                                                        "percent": None, "scalar": None}}}}

        self.assertEqual(sessions.recorded_window(record(800_000)), 800_000)
        for value in (None, 0, True, "800000"):
            with self.subTest(value=value):
                self.assertIsNone(sessions.recorded_window(record(value)))
        self.assertIsNone(sessions.recorded_window({}))


class RecordAuthorityWindowTests(_v4.V4Case):
    """A record-authority compile decides each agent's class against the
    window the session launched with; a different ceiling applies at the
    next resume, with an explicit class row."""

    def test_the_recorded_window_governs_until_resume(self) -> None:
        from claude_multi import cli, transition
        from claude_multi.cli import doctor

        root, key = write_bounded_assets(self.root / "assets", codex_line, BELOW)
        self.runtime = self.make_runtime(asset_root=root)
        document = narrowed_balanced(self.runtime.catalog)
        self.runtime.profiles.new(document)
        choices.update(self.env, window_ceiling=400_000)
        record = self.launch_fresh(cli.LaunchTarget("profile", document, document["name"], False, "Narrow"))
        choices.update(self.env, window_ceiling=choices.WINDOW_CEILING_DEFAULT)  # back to the default
        mid = record["managed_id"]
        self.assertEqual(sessions.recorded_window(record), 400_000)
        self.assertEqual(self.execs[-1][2][WINDOW_ENV], "400000")
        slots = sorted(rid for rid, b in record["applied"]["agents"].items() if b["key"] == key)
        self.assertTrue(slots)
        self.assertTrue(all(record["applied"]["agents"][rid]["selector"].endswith("[1m]") for rid in slots))
        # Doctor, under the default ceiling, reads the session's own window.
        self.runtime = self.make_runtime(asset_root=root)
        parts = self.runtime.converge_parts()
        ep = transition.expected_plan(
            self.store.load(mid), docs=parts.docs, prompt_bodies=parts.prompt_bodies, state_root=self.store.root,
            hook_command=parts.hook_command, token_helper_command=parts.token_helper_command,
            live=self.live(mid), environ=parts.environ, managed_root=parts.managed_root)
        self.assertIsNotNone(ep.plan, ep.reason)
        self.assertEqual(ep.agent_class_kept, ())
        self.assertEqual(scope.live_drift(self.live(mid), ep.plan), [])
        problems, attention, _info = doctor._check_scope_integrity(self.runtime, self.store.load(mid))
        self.assertEqual(problems, [])
        self.assertFalse([line for line in attention if "agent context class" in line], attention)
        # The resume compiles the current ceiling: the line's bound is now
        # below the window, so its agents move to the 200K class, visibly.
        prepared = self.prepare_resume(mid)
        rows = [line for line in prepared.diff if "agent class" in line]
        self.assertEqual(len(rows), len(slots), prepared.diff)
        self.assertTrue(all(line.rstrip().endswith("1M → 200K") for line in rows))
        self.assertIn("  context window       400K → 800K", prepared.diff)
        self.runtime.perform(prepared)
        resumed = self.store.load(mid)
        self.assertEqual(sessions.recorded_window(resumed), CEILING)
        for rid in slots:
            self.assertFalse(resumed["applied"]["agents"][rid]["selector"].endswith("[1m]"))


# ------------------------------------------------------------------ the ceiling


def _private_env(root: Path) -> dict[str, str]:
    home = root / "home"
    home.mkdir(mode=0o700)
    return {"HOME": str(home)}


class WindowCeilingChoiceTests(unittest.TestCase):
    """The shared ceiling is a ``choices.json`` field: 200K..800K, default
    800K, absent reads as the default, refused outside the range."""

    def setUp(self) -> None:
        import tempfile

        self.root = Path(tempfile.mkdtemp(prefix="cm-ceiling-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.env = _private_env(self.root)

    def test_the_field_defaults_and_round_trips(self) -> None:
        self.assertEqual(choices.window_ceiling(self.env), choices.WindowCeiling(CEILING, False))
        self.assertEqual((choices.WINDOW_CEILING_MIN, choices.WINDOW_CEILING_MAX, choices.WINDOW_CEILING_DEFAULT),
                         (200_000, CEILING, CEILING))
        choices.update(self.env, window_ceiling=400_000)
        path = choices.path(self.env)
        self.assertEqual(strict_json.loads(path.read_bytes()), {"version": 1, "window_ceiling": 400_000})
        self.assertEqual(choices.window_ceiling(self.env), choices.WindowCeiling(400_000, True))
        self.assertEqual(choices.window_ceiling(self.env).source, "set")
        choices.update(self.env, window_ceiling=CEILING)
        self.assertEqual(strict_json.loads(path.read_bytes()), {"version": 1})
        self.assertEqual(choices.window_ceiling(self.env).source, "default")
        # Never a settings.json key (older releases read it under a closed schema).
        self.assertFalse(settings.settings_path(self.env).exists())

    def test_values_outside_the_range_are_refused_with_the_range(self) -> None:
        for value in (199_999, CEILING + 1, 0, True, "400000", 400_000.0, None):
            with self.subTest(value=value), self.assertRaises(choices.ChoicesError) as caught:
                choices.update(self.env, window_ceiling=value)
            self.assertIn("200K–800K", str(caught.exception))
        self.assertFalse(choices.path(self.env).exists())
        for text, value in (("400000", 400_000), ("400K", 400_000), ("400k", 400_000), (" 200_000 ", 200_000),
                            ("800K", CEILING)):
            with self.subTest(text=text):
                self.assertEqual(choices.parse_window_ceiling(text), value)
        for text in ("900K", "199999", "1M", "abc", "", "-400K", "4e5"):
            with self.subTest(text=text), self.assertRaises(choices.ChoicesError) as caught:
                choices.parse_window_ceiling(text)
            self.assertIn("200K–800K", str(caught.exception))

    def test_an_unreadable_document_reports_its_problem(self) -> None:
        path = choices.path(self.env)
        state.ensure_private_dir(path.parent)
        for data in (b"{not json", b'{"version": 1, "window_ceiling": 900000}'):
            state.atomic_write(path, data)
            with self.subTest(data=data):
                ceiling = choices.window_ceiling(self.env)
                self.assertEqual((ceiling.value, ceiling.is_set), (CEILING, False))
                self.assertTrue(ceiling.problem)
                self.assertIn("choices.json", ceiling.remedy)


class WindowCeilingRuntimeTests(_v4.V4Case):
    """Every compile and evaluation on current state reads the ceiling; a
    change applies at the next launch or resume."""

    def test_the_choice_reaches_evaluation_compile_and_record(self) -> None:
        choices.update(self.env, window_ceiling=400_000)
        self.assertEqual(self.runtime.current_effective().window_ceiling, 400_000)
        self.assertEqual(self.runtime.settings_effective().window_ceiling, CEILING)
        record = self.launch_fresh()
        self.assertEqual(sessions.recorded_window(record), 400_000)
        self.assertEqual((self.execs[-1][2][WINDOW_ENV], self.execs[-1][2][PERCENT_ENV]), ("400000", "90"))
        live = strict_json.loads((self.live(record["managed_id"]) / "settings.json").read_bytes())
        self.assertEqual(live["env"][WINDOW_ENV], "400000")
        # 1M-class agents keep [1m] and run at the lowered window like the lead.
        agents = record["applied"]["agents"]
        self.assertTrue(agents)
        lineup = self.runtime._evaluate(self.runtime.profiles.load("balanced"), profile_name="balanced",
                                        effective=self.runtime.current_effective(),
                                        lcat=self.runtime.lineup_catalog())
        self.assertEqual({role.window for role in windows(lineup).values()}, {400_000})
        # The ceiling is neither a settings.json key nor in the record's snapshot.
        self.assertNotIn("window_ceiling", record["applied"]["settings"])
        self.assertFalse(settings.settings_path(self.env).exists())

    def test_an_unreadable_choice_refuses_launch_and_doctor_blocks(self) -> None:
        from claude_multi import cli
        from claude_multi.cli import doctor

        path = choices.path(self.env)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, b'{"version": 1, "window_ceiling": 100000}')
        with self.assertRaises(cli.CLIError) as caught:
            self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        self.assertIn("cannot read the window ceiling", str(caught.exception))
        self.assertIn("choices.json", str(caught.exception))
        problems, _attention = doctor._doctor_profile_report(self.runtime)
        self.assertTrue(any("cannot read the window ceiling" in line for line in problems), problems)
        self.assertEqual(self.execs, [])

    def test_print_launch_lists_each_role_window(self) -> None:
        import io
        from claude_multi.cli import launch_flow

        key = codex_line(self.runtime.catalog.docs)
        effort = self.runtime.catalog.docs["models"]["models"][key]["default_effort"]
        self.runtime.settings_store.update(
            lambda doc: doc.__setitem__("workflow_default_binding", {"model": key, "effort": effort}),
            catalog=self.runtime.lineup_catalog())
        choices.update(self.env, window_ceiling=500_000)
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[], read_only=True)
        buffer = io.StringIO()
        launch_flow._print_launch_plan(prepared, buffer)
        text = buffer.getvalue()
        self.assertIn("context windows (ceiling 500000, set):\n", text)
        rows = [line for line in text.splitlines() if " class · window " in line]
        roles = [line.split()[0] for line in rows]
        self.assertEqual(roles[0], "lead")
        self.assertEqual(roles[1:-1], [profile.label(rid) for rid in prepared.lineup.agents])
        self.assertTrue(rows[-1].lstrip().startswith("workflow default"))
        trigger = compiler.reactive_trigger(500_000, 90)
        self.assertTrue(all(line.endswith(f"window 500000 · compacts at {trigger}") for line in rows), rows)
        self.assertTrue(all("[1m] · 1M class" in line for line in rows), rows)
        self.assertIn(f"compaction env (process and scope settings): {WINDOW_ENV}=500000 {PERCENT_ENV}=90\n",
                      text)
        self.assertNotIn(_v4.FIXTURE_GATEWAY_TOKEN, text)

    def test_the_command_shows_sets_and_resets(self) -> None:
        import contextlib
        import io
        from claude_multi import cli

        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.main(["window-ceiling", *argv], runtime=self.runtime, output_stream=out,
                                interactive=False)
            return code, out.getvalue(), err.getvalue()

        code, out, _err = run()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("window ceiling: 800K (800000 tokens, default) · range 200K–800K"), out)
        self.assertIn("agent lines whose bound is below it keep the 200K class", out)
        self.assertEqual(run("400K")[:2], (0, "window ceiling set to 400K (400000 tokens) — applies at the next "
                                           "launch or resume; running sessions keep their window\n"))
        self.assertEqual(choices.window_ceiling(self.env), choices.WindowCeiling(400_000, True))
        self.assertTrue(run()[1].startswith("window ceiling: 400K (400000 tokens, set)"))
        for value in ("900K", "150000", "lots"):
            with self.subTest(value=value):
                code, out, err = run(value)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("200K–800K (200000–800000 tokens)", err)
                self.assertEqual(choices.window_ceiling(self.env).value, 400_000)
        # The reset asks nothing: it names the ceiling it replaced and the
        # command that sets it again.
        self.assertEqual(run("--reset")[:2], (0, "window ceiling reset to the default 800K (800000 tokens) — "
                                                 "applies at the next launch or resume\n"
                                                 "  it was 400K: claude-multi window-ceiling 400K sets it again\n"))
        self.assertEqual(choices.window_ceiling(self.env), choices.WindowCeiling(CEILING, False))
        self.assertIn("nothing changed", run("--reset")[1])
        # A ceiling that is no whole thousand comes back to the token.
        self.assertEqual(run("258400")[0], 0)
        shown = run("--reset")[1]
        self.assertIn("it was 258400: claude-multi window-ceiling 258400 sets it again", shown)
        self.assertEqual(run("258400")[0], 0)
        self.assertEqual(choices.window_ceiling(self.env).value, 258_400)
        run("--reset")
        from claude_multi.cli import parser as parser_mod

        parser = cli.build_parser()
        for argv, writes in ((["window-ceiling"], False), (["window-ceiling", "400K"], True),
                             (["window-ceiling", "--reset"], True)):
            with self.subTest(argv=argv):
                self.assertIs(parser_mod._writes_versioned_state(parser.parse_args(argv)), writes)

    def test_reset_removes_an_explicitly_stored_default(self) -> None:
        import contextlib
        import io
        from claude_multi import cli

        path = choices.path(self.env)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, b'{"version": 1, "window_ceiling": 800000}')
        self.assertEqual(choices.window_ceiling(self.env), choices.WindowCeiling(CEILING, True))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["window-ceiling", "--reset"], runtime=self.runtime, output_stream=out,
                            interactive=False)
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(out.getvalue(), "window ceiling reset to the default 800K (800000 tokens) — "
                                         "applies at the next launch or resume\n")
        self.assertEqual(strict_json.loads(path.read_bytes()), {"version": 1})
        self.assertEqual(choices.window_ceiling(self.env).source, "default")

    def test_a_ceiling_changed_after_preparation_never_launches(self) -> None:
        from claude_multi import launch

        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        # The plan states the one ceiling it was compiled with.
        self.assertEqual(prepared.window_ceiling, choices.WindowCeiling(CEILING, False))
        self.assertEqual(prepared.result.env_set[WINDOW_ENV], str(CEILING))
        choices.update(self.env, window_ceiling=200_000)
        with self.assertRaises(launch.LaunchError) as caught:
            self.runtime.perform(prepared)
        self.assertIn("the window ceiling changed since this launch was prepared (800K → 200K) — nothing "
                      "launched; run the command again", str(caught.exception))
        # An unreadable choices.json refuses the same plan (fail closed).
        path = choices.path(self.env)
        state.atomic_write(path, b"{not json")
        with self.assertRaises(launch.LaunchError) as caught:
            self.runtime.perform(prepared)
        self.assertIn("cannot read the window ceiling", str(caught.exception))
        self.assertIn("choices.json", str(caught.exception))
        self.assertEqual(self.execs, [])
        self.assertEqual(self.store.scan_uuid_records(), [])
        # With the ceiling it was prepared with, the same plan launches.
        state.atomic_write(path, b'{"version": 1}')
        self.runtime.perform(prepared)
        self.assertEqual(len(self.execs), 1)
        self.assertEqual(self.execs[0][2][WINDOW_ENV], str(CEILING))

    def test_the_commit_barrier_checks_the_ceiling_again(self) -> None:
        from unittest import mock
        from claude_multi import launch

        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        real_acquire = state.acquire_served_barrier

        def acquire(*args, **kwargs):
            # The first check (before the barrier) passed; the ceiling
            # changes before the commit.
            token = real_acquire(*args, **kwargs)
            choices.update(self.env, window_ceiling=400_000)
            return token

        with mock.patch.object(state, "acquire_served_barrier", side_effect=acquire), \
                self.assertRaises(launch.LaunchError) as caught:
            self.runtime.perform(prepared)
        self.assertIn("(800K → 400K)", str(caught.exception))
        self.assertEqual(self.execs, [])
        self.assertEqual(self.store.scan_uuid_records(), [])

    def test_print_launch_states_the_ceiling_the_plan_was_compiled_with(self) -> None:
        import io
        from unittest import mock
        from claude_multi.cli import launch_flow

        choices.update(self.env, window_ceiling=500_000)
        reads = [choices.window_ceiling(self.env), choices.WindowCeiling(200_000, True)]
        # A second read during the preparation would see another value: the
        # plan never reads the ceiling twice.
        with mock.patch.object(type(self.runtime), "window_ceiling", side_effect=lambda: reads.pop(0)):
            prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[],
                                            read_only=True)
        self.assertEqual(len(reads), 1)
        buffer = io.StringIO()
        launch_flow._print_launch_plan(prepared, buffer)
        self.assertIn("context windows (ceiling 500000, set):\n", buffer.getvalue())
        self.assertEqual(prepared.result.env_set[WINDOW_ENV], "500000")



class WindowDisplayTests(unittest.TestCase):
    """The card, the lineup summary and the Settings row show each role's
    effective window (data from ``profile.lineup_windows``)."""

    def lineups(self):
        bundle = _bundle()
        key = codex_line(bundle.docs)
        lcat = _lcat(bundle.docs)
        uniform = profile.resolve(bundle.seed_profiles["balanced"], lcat, effective=_eff(lcat))
        docs = bounded(bundle.docs, key, BELOW)
        narrow_lcat = _lcat(docs)
        mixed = profile.resolve(narrowed_balanced(bundle), narrow_lcat, effective=_eff(narrow_lcat))
        direct = profile.resolve(bundle.seed_profiles["direct"], lcat, effective=_eff(lcat))
        return key, uniform, mixed, direct

    def context(self, lineup) -> dict:
        role = windows(lineup)[profile.LEAD_ROLE]
        return {"window": role.window, "trigger": role.trigger, "percent": lineup.policy.percent, "scalar": None}

    def card(self, lineup, **kwargs):
        from claude_multi.cli import types

        target = types.LaunchTarget("profile", {}, lineup.name, True, "x")
        return views.card_model(target=target, lineup=lineup, context=self.context(lineup), **kwargs)

    def test_the_card_names_every_agent_or_adds_a_window_column(self) -> None:
        key, uniform, mixed, direct = self.lineups()
        card = self.card(uniform)
        context = next(row for row in card.rows if row.kind == "context")
        self.assertEqual(context.text, "window 800K (lead and agents) · compacts at ~702K (90 %)")
        self.assertEqual(next(row for row in card.rows if row.kind == "header").cells, views.CARD_TABLE_COLUMNS)
        card = self.card(mixed)
        context = next(row for row in card.rows if row.kind == "context")
        self.assertEqual(context.text, "window 800K (lead) · compacts at ~702K (90 %)")
        self.assertEqual(next(row for row in card.rows if row.kind == "header").cells,
                         views.CARD_WINDOW_TABLE_COLUMNS)
        cells = {row.cells[0]: row.cells for row in card.rows if row.kind == "agent"}
        for rid, agent in mixed.agents.items():
            with self.subTest(agent=rid):
                expected = "200K" if agent.binding.key == key else "800K"
                self.assertEqual(cells[profile.label(rid)][4], expected)
        explorer = next(line for line in views.card_text(card, 80).splitlines() if line.startswith("  explorer"))
        self.assertIn(" 200K ", explorer + " ")
        self.assertEqual(views.card_widths(card, 80)[4], len("window"))
        card = self.card(direct)
        self.assertEqual(next(row for row in card.rows if row.kind == "context").text,
                         "window 800K · compacts at ~702K (90 %)")
        role = windows(uniform)[profile.LEAD_ROLE]
        card = self.card(uniform, workflow_default=("Model X · high", role))
        workflow = next(row for row in card.rows if row.kind == "workflow")
        self.assertEqual(workflow.text, "Model X · high · window 800K (agents without a cm-* type)")

    def test_the_lineup_summary_groups_roles_by_window(self) -> None:
        from claude_multi import lineup as lineup_mod

        key, uniform, mixed, direct = self.lineups()
        self.assertEqual(lineup_mod.window_summary(uniform),
                         "context window 800K for the lead and every agent · compacts at ~702K (90 %)")
        role = windows(uniform)[profile.LEAD_ROLE]
        self.assertEqual(lineup_mod.window_summary(uniform, role),
                         "context window 800K for the lead, every agent and the workflow default · "
                         "compacts at ~702K (90 %)")
        self.assertEqual(lineup_mod.window_summary(direct),
                         "context window 800K for the lead · compacts at ~702K (90 %)")
        narrow = [profile.label(rid) for rid, agent in mixed.agents.items() if agent.binding.key == key]
        wide = [profile.label(rid) for rid, agent in mixed.agents.items() if agent.binding.key != key]
        self.assertTrue(narrow and wide)
        self.assertEqual(lineup_mod.window_summary(mixed),
                         f"context windows (90 %): 800K (compacts at ~702K) {', '.join(['lead', *wide])} · "
                         f"200K (compacts at ~162K) {', '.join(narrow)}")
        text = lineup_mod.render_text(mixed, header="profile narrow", profile_view=True)
        self.assertIn(lineup_mod.window_summary(mixed) + "\n", text)

    def test_the_settings_row_shows_value_and_source(self) -> None:
        lcat = _lcat(_bundle().docs)
        eff = _eff(lcat)

        def row(ceiling):
            rows = views.settings_rows({"version": 1}, eff, overrides={}, context=None, token_state=(None, False),
                                       window_ceiling=ceiling)
            return next(item for item in rows if item.key == views.SETTINGS_CEILING_KEY)

        default = row(None)
        self.assertEqual((default.value, default.note, default.when, default.edit, default.editable),
                         ("800K", "default · range 200K–800K", "next", "ceiling", True))
        self.assertEqual(default.detail, "The effective window is the smaller of this ceiling and the lead set's "
                                         "smallest provider bound; agent lines below it keep the 200K class.")
        self.assertEqual(row((400_000, True, None)).value, "400K")
        self.assertEqual(row((400_000, True, None)).note, "set · range 200K–800K")
        broken = row((CEILING, False, "choices are not valid JSON"))
        self.assertFalse(broken.editable)
        self.assertIn("choices.json cannot be read", broken.detail)
        from test_hygiene import origin_counts

        for item in (default, broken):
            self.assertEqual(origin_counts((item.label + item.note + item.detail).encode()), 0)
            self.assertNotIn("3.1", item.detail)



class WindowCeilingSettingsScreenTests(test_screens_catalog._ScreenCase):
    """The Settings row edits and resets the ceiling in ``choices.json``."""

    def screen(self):
        import io
        from claude_multi import cli, tui

        return cli._SettingsScreen(self.runtime, tui.MONO_PALETTE, tty_in=io.StringIO(), tty_out=io.StringIO())

    def test_edit_reset_and_refusal(self) -> None:
        ENTER, ESC = test_screens_catalog.ENTER, test_screens_catalog.ESC
        screen = self.screen()
        self.assertEqual(screen.selected_row.key, views.SETTINGS_CEILING_KEY)  # the first row
        self.assertEqual(screen.keybar_bindings()[0], ("Enter", "edit"))
        win = self.run_screen(screen, [ENTER, *"900K", ENTER, ENTER, ESC, ESC])
        self.assertTrue(any("current 800K · default 800K · range 200K–800K" in frame for frame in win.frames))
        self.assertTrue(any("outside the allowed range 200K–800K" in frame for frame in win.frames))
        self.assertFalse(choices.path(self.runtime.environ).exists())
        screen = self.screen()
        self.run_screen(screen, [ENTER, *"400K", ENTER, ENTER, ESC])
        self.assertEqual(choices.window_ceiling(self.runtime.environ), choices.WindowCeiling(400_000, True))
        self.assertEqual(screen.message, "saved context window ceiling = 400K — applies at the next launch or "
                                         "resume; running sessions keep their window")
        row = screen._row(views.SETTINGS_CEILING_KEY)
        self.assertEqual((row.value, row.note), ("400K", "set · range 200K–800K"))
        self.assertEqual(self.runtime.current_effective().window_ceiling, 400_000)
        self.assertFalse(settings.settings_path(self.runtime.environ).exists())
        # R focuses Cancel first: Enter there keeps the ceiling; Left, then Enter resets it.
        screen = self.screen()
        win = self.run_screen(screen, ["R", ENTER, ESC])
        self.assertTrue(any("Reset context window ceiling to its default?" in frame for frame in win.frames))
        self.assertEqual(choices.window_ceiling(self.runtime.environ), choices.WindowCeiling(400_000, True))
        screen = self.screen()
        self.run_screen(screen, ["R", test_screens_catalog.LEFT, ENTER, ESC])
        self.assertEqual(choices.window_ceiling(self.runtime.environ), choices.WindowCeiling(CEILING, False))
        self.assertTrue(screen.message.startswith("reset context window ceiling to its default"))

    def test_an_unreadable_choices_file_makes_the_row_read_only(self) -> None:
        path = choices.path(self.runtime.environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, b"{not json")
        screen = self.screen()
        self.assertIsNone(screen.banner)  # settings.json itself is fine
        row = screen._row(views.SETTINGS_CEILING_KEY)
        self.assertFalse(row.editable)
        self.assertIn("choices.json cannot be read", row.detail)
        self.assertEqual(screen.keybar_bindings()[0], ("read-only:", "fix choices.json to edit"))


# ------------------------------------------------------------------ shipped


@uses_shipped_catalog
class ShippedAgentClassTests(unittest.TestCase):
    """The shipped catalog: no line carries an agent class of its own; every
    seed's agents on 1M-class lines whose bound is at least the session
    window use ``[1m]`` and compact at the window, both envs carry it."""

    def test_no_line_carries_an_agent_class(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        for key, entry in bundle.lines.items():
            with self.subTest(line=key):
                self.assertNotIn("agent_client_tokens", entry["context"])
        schema = strict_json.load(SHIPPED_ROOT / "schemas" / "models.schema.json")
        context = schema["properties"]["models"]["additionalProperties"]["properties"]["context"]
        self.assertNotIn("agent_client_tokens", context["properties"])

    def test_every_seed_runs_its_1m_agents_at_the_session_window(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        lcat = _lcat(bundle.docs)
        eff = _eff(lcat)
        for name in catalog.SEED_PROFILE_NAMES:
            with self.subTest(seed=name):
                lineup = profile.resolve(bundle.seed_profiles[name], lcat, effective=eff)
                result = launch(bundle, bundle.docs, eff, lineup)
                window = int(result.env_set[WINDOW_ENV])
                self.assertEqual(result.scope_plan.settings["env"][WINDOW_ENV], str(window))
                self.assertEqual(window, lineup.policy.window)
                self.assertLessEqual(window, CEILING)
                for rid, agent in lineup.agents.items():
                    entry = bundle.lines[agent.binding.key]
                    if entry["context"]["client_tokens"] < 1_000_000:
                        continue
                    role = windows(lineup)[rid]
                    if entry["context"]["provider_tokens"] >= window:
                        self.assertTrue(agent.binding.selector.endswith("[1m]"), (rid, agent.binding.selector))
                        self.assertEqual(role.window, window)
                    self.assertLessEqual(role.trigger, entry["context"]["provider_tokens"])

    def test_no_agents_capable_line_overflows_at_any_ceiling(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        providers = bundle.docs["providers"]["providers"]
        for ceiling in (200_000, 500_000, CEILING):
            policy = profile.WindowPolicy(window=ceiling, percent=settings.COMPACTION_PERCENT_MAX)
            for key, entry in bundle.lines.items():
                with self.subTest(ceiling=ceiling, line=key):
                    self.assertEqual(profile.line_agent_window_problems(
                        entry, providers[entry["provider"]], policy=policy), ())


if __name__ == "__main__":
    unittest.main()
