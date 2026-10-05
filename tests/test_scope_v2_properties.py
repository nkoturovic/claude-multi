"""Property tests: the v2 fence and scope plan over random fixture profiles.

A seeded generator (``random.Random(seed)``, 300 seeds) draws profiles over
the loaded fixture ``LineupCatalog``: a lead from the offered lead-capable
lines (declared efforts plus ``ultracode``), each agent id bound with
p=0.6 to an admitting agents-capable line at a declared effort (``requires``
closed), random native-agent policy, workflows, ``lead_providers``,
``compaction_percent`` override, provider enablement (every bound provider
kept enabled), Explore cap, ``--no-subagents`` and ``workflow_default_binding``
(absent, a fence member or a non-member). The expectations are computed by
independent oracles written here (never by calling the compiler): lead set
, agent set, fallback-only set, compaction, denies.

Context is checked on ``lead-set.json`` and on the
``compile_lineup_launch`` process env; binding-independence also covers the
launch half (process env, appendix bytes and ``lead_prompt_path``).
"""

from __future__ import annotations

import copy
import dataclasses
import random
import unittest
from pathlib import Path
from typing import Any

from claude_multi import catalog, compiler, profile, render, scope, settings, strict_json
from claude_multi.scope import ScopeError
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog

SEEDS = range(300)
FIXED_SESSION = "11111111-1111-4111-8111-111111111111"
HOOK3 = "/state/bin/claude-multi-hook-3"
HELPER = "/state/bin/claude-multi-gateway-token"
CEILING = 800_000
OUTPUT_RESERVE = 20_000
HEADROOM = 13_000
FAMILY_VARS = (
    ("fable", "ANTHROPIC_DEFAULT_FABLE_MODEL"),
    ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
    ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
)
SECRET_DENIES = [
    "Read(~/.config/claude-multi/**)",
    "Read(~/.local/share/claude-multi/**)",
    "Read(~/.config/anthropic/**)",
    "Read(~/.claude/.credentials.json)",
    "Edit(~/.local/state/claude-multi/**)",
    "Edit(~/.config/claude-multi/**)",
]

# An independent named inventory of the skill policy (including editing and
# conditional bundled entries). Never derive this oracle from scope's policy
# constants.
SKILL_DENIES = [
    "Skill(batch)",
    "Skill(code-review)",
    "Skill(commit)",
    "Skill(commit-push-pr)",
    "Skill(debug)",
    "Skill(design-sync)",
    "Skill(doctor)",
    "Skill(fewer-permission-prompts)",
    "Skill(init)",
    "Skill(keybindings-help)",
    "Skill(pr)",
    "Skill(run-skill-generator)",
    "Skill(schedule)",
    "Skill(security-review)",
    "Skill(simplify)",
    "Skill(update-config)",
    "Skill(verify)",
]


# ------------------------------------------------------------------ oracles


def selectors_of(entry: dict[str, Any]) -> list[tuple[str | None, str]]:
    efforts = entry["efforts"]
    if isinstance(efforts, list):
        return [(None, entry["selector"])]
    order = ("low", "medium", "high", "xhigh", "max")
    return [(level, efforts[level]["selector"]) for level in sorted(efforts, key=order.index)]


def efforts_of(entry: dict[str, Any]) -> list[str]:
    efforts = entry["efforts"]
    return list(efforts) if isinstance(efforts, list) else list(efforts)


def family_of(entry: dict[str, Any], providers: dict[str, Any]) -> str:
    return entry.get("family") or providers[entry["provider"]]["independence_family"]


def offered(key: str, entry: dict[str, Any], enabled: dict[str, bool], admitted=()) -> bool:
    status = entry.get("status", "active") == "active" or key in admitted
    return status and enabled.get(entry["provider"], True)


def oracle_lead_rows(lines, enabled, lead_class, lead_providers):
    rows = []
    for key, entry in lines.items():
        if not offered(key, entry, enabled):
            continue
        if "lead" not in entry["capabilities"] or entry.get("lead") is None:
            continue
        if entry["context"].get("ordinary_profile") != lead_class:
            continue
        if lead_providers is not None and entry["provider"] not in lead_providers:
            continue
        for effort, selector in selectors_of(entry):
            rows.append((key, effort, selector))
    return rows


def oracle_agent_set(lines, enabled):
    # Every selector of every offered agents-capable line. (An agent line
    # whose provider bound is below the session window would add the same
    # alias without [1m]; no line these properties draw from is one.)
    return sorted(
        {
            selector
            for key, entry in lines.items()
            if offered(key, entry, enabled) and "agents" in entry["capabilities"]
            for _effort, selector in selectors_of(entry)
        }
    )


def oracle_fallback(lines, providers, rows):
    if not any(family_of(lines[key], providers) == "anthropic" for key, _e, _s in rows):
        return []
    lead = {selector for _k, _e, selector in rows}
    return sorted({wire + "[1m]" for wire in catalog.REFUSAL_FALLBACK_WIRES} - lead)


def oracle_context(lines, rows, percent):
    contexts = [lines[key]["context"] for key, _e, _s in rows]
    window = min(CEILING, min(c["provider_tokens"] for c in contexts))
    scalars = [c["scalar_tokens"] for c in contexts if c.get("scalar_tokens") is not None]
    scalar = (
        min(CEILING, min(scalars))
        if scalars and max(c["client_tokens"] for c in contexts) < 1_000_000
        else None
    )
    budget = window - OUTPUT_RESERVE
    trigger = min(budget * percent // 100, budget - HEADROOM)
    return {"percent": percent, "scalar": scalar, "trigger": trigger, "window": window}


def oracle_denies(native: dict[str, str], aliases, no_subagents: bool) -> list[str]:
    if no_subagents:
        return ["Agent", *SKILL_DENIES, *SECRET_DENIES]
    denies = []
    if native["explore"] == "native" or native["plan"] == "native":
        if native["explore"] != "native":
            denies.append("Agent(Explore)")
        if native["plan"] != "native":
            denies.append("Agent(Plan)")
    if native["general_purpose"] == "off":
        denies.append("Agent(general-purpose)")
    for alias in sorted(set(aliases)):
        if f"Agent({alias})" not in denies:
            denies.append(f"Agent({alias})")
    return [*denies, *SKILL_DENIES, *SECRET_DENIES]


def oracle_family_env(lines, providers, enabled):
    env = {}
    for key, variable in FAMILY_VARS:
        entry = lines.get(key)
        if entry is None or not offered(key, entry, enabled):
            continue
        suffix = "[1m]" if entry["context"]["client_tokens"] >= 1_000_000 else ""
        env[variable] = entry["wire_model"] + suffix
    return env


# ---------------------------------------------------------------- generator


@dataclasses.dataclass
class Case:
    doc: dict[str, Any]
    enabled: dict[str, bool]
    cap_disabled: bool
    no_subagents: bool
    workflow: dict[str, str] | None
    workflow_expect: str  # "none" | "member" | "refused"
    workflow_selector: str | None


def _admits(entry: dict[str, Any], rid: str) -> bool:
    return (
        "agents" in entry["capabilities"]
        and (entry["roles"] == "all" or rid in entry["roles"])
    )


def generate(rng: random.Random, lcat: profile.LineupCatalog) -> Case:
    lines = dict(lcat.lines)
    providers = dict(lcat.providers)
    leads = [
        key
        for key, entry in lines.items()
        if "lead" in entry["capabilities"]
        and entry.get("lead") is not None
        and entry["context"].get("ordinary_profile") is not None
        and entry.get("status", "active") == "active"
    ]
    lead_key = rng.choice(leads)
    lead_entry = lines[lead_key]
    lead_effort = rng.choice([*efforts_of(lead_entry), "ultracode"])
    agents: dict[str, dict[str, str]] = {}
    for rid in catalog.AGENT_ROLE_IDS:
        if rng.random() >= 0.6:
            continue
        candidates = [
            key
            for key, entry in lines.items()
            if _admits(entry, rid) and entry.get("status", "active") == "active"
        ]
        if not candidates:
            continue
        key = rng.choice(candidates)
        agents[rid] = {"model": key, "effort": rng.choice(efforts_of(lines[key]))}
    # Close ``requires`` (a non-plain grade needs its plain id).
    changed = True
    while changed:
        changed = False
        for rid in list(agents):
            for required in lcat.roles[rid]["requires"]:
                if required not in agents:
                    del agents[rid]
                    changed = True
                    break
    native = {
        "explore": rng.choice(
            ["native", "off", "replace"] if "cm-explorer" in agents else ["native", "off"]
        ),
        "general_purpose": rng.choice(["on", "off"]),
        "plan": rng.choice(["native", "off"]),
    }
    doc: dict[str, Any] = {
        "version": 2,
        "name": "prop",
        "lead": {"model": lead_key, "effort": lead_effort},
        "agents": agents,
        "native_agents": native,
        "workflows": rng.choice(["native", "off"]),
    }
    if rng.random() < 0.4:
        others = [pid for pid in providers if pid != lead_entry["provider"]]
        doc["lead_providers"] = sorted(
            {lead_entry["provider"], *rng.sample(others, rng.randint(0, min(3, len(others))))}
        )
    if rng.random() < 0.3:
        doc["settings_overrides"] = {"compaction_percent": rng.randint(60, 95)}
    bound = {lead_entry["provider"]} | {lines[b["model"]]["provider"] for b in agents.values()}
    enabled = {pid: (pid in bound or rng.random() < 0.7) for pid in providers}

    workflow: dict[str, str] | None = None
    expect, wf_selector = "none", None
    roll = rng.random()
    if roll < 0.35:
        members = [
            key
            for key, entry in lines.items()
            if "agents" in entry["capabilities"] and offered(key, entry, enabled)
        ]
        key = rng.choice(members)
        entry = lines[key]
        if isinstance(entry["efforts"], list):
            effort = entry["default_effort"]
            wf_selector = entry["selector"]
        else:
            effort = rng.choice(list(entry["efforts"]))
            wf_selector = entry["efforts"][effort]["selector"]
        workflow, expect = {"model": key, "effort": effort}, "member"
    elif roll < 0.5:
        outside = [
            key
            for key, entry in lines.items()
            if "agents" not in entry["capabilities"] or not offered(key, entry, enabled)
        ]
        if outside:
            key = rng.choice(outside)
            workflow = {"model": key, "effort": lines[key]["default_effort"]}
            expect = "refused"
    return Case(
        doc=doc,
        enabled=enabled,
        cap_disabled=rng.random() < 0.7,
        no_subagents=rng.random() < 0.15,
        workflow=workflow,
        workflow_expect=expect,
        workflow_selector=wf_selector,
    )


def effective_for(lcat, case: Case) -> settings.Effective:
    doc = {
        "version": 1,
        "providers": {pid: {"enabled": on} for pid, on in case.enabled.items()},
        "explore_inherit_cap_disabled": case.cap_disabled,
    }
    eff = settings.effective(doc, provider_ids=lcat.providers, line_keys=lcat.lines)
    return dataclasses.replace(eff, workflow_default_binding=case.workflow)


def compile_plan(bundle, lcat, eff, lineup, *, no_subagents=False):
    return scope.compile_lineup_scope(
        lineup,
        lcat,
        eff,
        bundle.prompt_bodies,
        scope.catalog_meta_v2(bundle.docs),
        lineup_generation=1,
        managed_id=FIXED_SESSION,
        hook_command=HOOK3,
        token_helper_command=HELPER,
        no_subagents=no_subagents,
    )


def compile_launch(bundle, lcat, eff, lineup, *, no_subagents=False):
    return compiler.compile_lineup_launch(
        docs=bundle.docs,
        prompt_bodies=bundle.prompt_bodies,
        lineup=lineup,
        effective=eff,
        session_action=compiler.build_fresh(FIXED_SESSION),
        lineup_generation=1,
        state_root=Path("/state"),
        scope_dir=Path("/state/scopes") / FIXED_SESSION,
        hook_command=HOOK3,
        token_helper_command=HELPER,
        no_subagents=no_subagents,
    )


# -------------------------------------------------------------------- tests


class FixturePropertyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = catalog.load_catalog(FIXTURE_ROOT)
        cls.lcat = profile.LineupCatalog.from_docs(cls.bundle.docs)
        cls.lines = dict(cls.lcat.lines)
        cls.providers = dict(cls.lcat.providers)
        cls.aliases = cls.bundle.docs["native-contract"]["generic_agent_aliases"]["values"]

    def _check(self, case: Case, rng: random.Random, coverage: dict[str, set]) -> None:
        lcat, lines, providers = self.lcat, self.lines, self.providers
        eff = effective_for(lcat, case)
        lineup = profile.resolve(case.doc, lcat, effective=eff)
        if case.workflow_expect == "refused":
            with self.assertRaisesRegex(ScopeError, "Settings workflow_default_binding"):
                compile_plan(self.bundle, lcat, eff, lineup, no_subagents=case.no_subagents)
            coverage["workflow"].add("refused")
            return
        plan = compile_plan(self.bundle, lcat, eff, lineup, no_subagents=case.no_subagents)
        lead_set = strict_json.loads(plan.other_files["lead-set.json"])
        settings_doc = plan.settings
        available = settings_doc["availableModels"]
        lead_sel = lineup.lead.binding.selector
        bound = {agent.binding.selector for agent in lineup.agents.values()}

        rows = oracle_lead_rows(lines, case.enabled, lineup.lead_class, lineup.lead_providers)
        agent_set = oracle_agent_set(lines, case.enabled)
        fallback = oracle_fallback(lines, providers, rows)
        lead_rows = {selector for _k, _e, selector in rows}

        # The fence contains the lead and every eligible agent selector.
        self.assertLessEqual(bound | {lead_sel}, set(available))
        self.assertLessEqual(set(agent_set), set(available))
        # Lead rows match the independent oracle.
        self.assertEqual(
            [(r["key"], r["effort"], r["selector"]) for r in lead_set["rows"]], rows
        )
        self.assertIn(lead_sel, lead_rows)
        # The fence is the sorted, unique union of allowed selectors.
        self.assertEqual(available, sorted(lead_rows | set(agent_set) | set(fallback)))
        self.assertEqual(len(available), len(set(available)))
        self.assertEqual(settings_doc["model"], lead_sel)
        # Fallback-only selectors stay out of the lead picker.
        picker = [option["model"] for option in settings_doc["modelPicker"]["options"]]
        self.assertFalse(set(fallback) & (lead_rows | set(picker)))
        # Context follows the effective compaction-percent override.
        percent = lineup.settings_overrides.get("compaction_percent", eff.compaction_percent)
        expected_context = oracle_context(lines, rows, percent)
        self.assertEqual(lead_set["context"], expected_context)
        # The launch process env carries the same context numbers.
        launch = compile_launch(self.bundle, lcat, eff, lineup, no_subagents=case.no_subagents)
        env_set = launch.env_set
        self.assertEqual(env_set["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], str(expected_context["window"]))
        self.assertEqual(env_set["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], str(percent))
        self.assertEqual(
            env_set.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS"),
            None if expected_context["scalar"] is None else str(expected_context["scalar"]),
        )
        self.assertEqual(
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS" in launch.env_unset, expected_context["scalar"] is None
        )
        self.assertIn(
            f"deterministic reactive trigger {expected_context['trigger']} ({percent}% of",
            launch.lead_prompt,
        )
        self.assertEqual(scope.plan_hash(launch.scope_plan), scope.plan_hash(plan))
        # Picker options exactly replace the built-in lead choices.
        self.assertEqual(picker, [r["selector"] for r in lead_set["rows"]])
        self.assertIs(settings_doc["modelPicker"]["replaceBuiltInOptions"], True)
        # Native-agent defaults and workflow selection match declared policy.
        env = settings_doc["env"]
        family = {k: v for k, v in env.items() if k.startswith("ANTHROPIC_DEFAULT_")}
        self.assertEqual(family, oracle_family_env(lines, providers, case.enabled))
        self.assertEqual(
            env.get("CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP"),
            "1" if case.cap_disabled else None,
        )
        self.assertEqual(env.get("CLAUDE_CODE_SUBAGENT_MODEL"), case.workflow_selector)
        if case.workflow_selector is not None:
            self.assertIn(case.workflow_selector, available)
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", env)
        self.assertEqual(env["CLAUDE_CODE_RETRY_WATCHDOG"], "1")
        # Both layers pin fast mode off; settings env wins over
        # user/project settings, and process env covers the launch boundary.
        self.assertEqual(env["CLAUDE_CODE_DISABLE_FAST_MODE"], "1")
        self.assertEqual(env_set["CLAUDE_CODE_DISABLE_FAST_MODE"], "1")
        # Worktree and workflow settings preserve launcher isolation.
        self.assertEqual(settings_doc["worktree"], {"baseRef": "head"})
        self.assertIs(
            settings_doc["disableWorkflows"], lineup.workflows == "off" or case.no_subagents
        )
        # Every bound agent file preserves its model, effort and role policy.
        self.assertEqual(
            set(plan.agent_files), {f".claude/agents/{rid}.md" for rid in lineup.agents}
        )
        for rid, agent in lineup.agents.items():
            text = plan.agent_files[f".claude/agents/{rid}.md"].decode()
            head = text.split("\n---\n", 1)[0]
            self.assertIn(f"\nmodel: {agent.binding.selector}\n", head + "\n")
            self.assertIn(f"\neffort: {agent.binding.effort}", head)
            self.assertNotIn("ultracode", head)
            role = lcat.roles[rid]
            self.assertEqual("disallowedTools:" in head, bool(role["disallowed_tools"]))
            self.assertEqual("isolation: worktree" in head, role["isolation"] == "worktree")
        # Denies match the independent native-agent and skill-policy oracle.
        self.assertEqual(
            settings_doc["permissions"]["deny"],
            oracle_denies(dict(lineup.native_agents), self.aliases, case.no_subagents),
        )
        # Identical inputs produce an identical scope hash.
        again = compile_plan(self.bundle, lcat, eff, lineup, no_subagents=case.no_subagents)
        self.assertEqual(scope.plan_hash(plan), scope.plan_hash(again))
        # Only owned settings and offered selectors enter the scope.
        self.assertLessEqual(set(settings_doc), scope.COMPILED_SETTINGS_KEYS)
        self.assertFalse([s for s in available if s.startswith(render.SENTINEL_PREFIX)])
        offered_selectors = {
            selector
            for key, entry in lines.items()
            if offered(key, entry, case.enabled)
            for _e, selector in selectors_of(entry)
        }
        self.assertLessEqual(set(available), offered_selectors | set(fallback))

        # Binding independence: other random bindings (or none) move no fence byte.
        other = copy.deepcopy(case.doc)
        if rng.random() < 0.3:
            other["agents"] = {}
            if other["native_agents"]["explore"] == "replace":
                other["agents"]["cm-explorer"] = case.doc["agents"]["cm-explorer"]
        else:
            for rid in list(other["agents"]):
                candidates = [
                    key
                    for key, entry in lines.items()
                    if _admits(entry, rid) and offered(key, entry, case.enabled)
                ]
                key = rng.choice(candidates)
                other["agents"][rid] = {
                    "model": key,
                    "effort": rng.choice(efforts_of(lines[key])),
                }
        lineup2 = profile.resolve(other, lcat, effective=eff)
        plan2 = compile_plan(self.bundle, lcat, eff, lineup2, no_subagents=case.no_subagents)
        self.assertEqual(plan2.other_files["lead-set.json"], plan.other_files["lead-set.json"])
        for key in ("availableModels", "modelPicker", "model", "env", "hooks", "worktree"):
            self.assertEqual(plan2.settings[key], settings_doc[key], key)
        launch2 = compile_launch(self.bundle, lcat, eff, lineup2, no_subagents=case.no_subagents)
        self.assertEqual(launch2.env_set, launch.env_set)
        self.assertEqual(launch2.env_unset, launch.env_unset)
        self.assertEqual(launch2.lead_prompt, launch.lead_prompt)
        self.assertEqual(launch2.lead_prompt_path, launch.lead_prompt_path)
        self.assertEqual(launch2.argv, launch.argv)

        coverage["lead_class"].add(lineup.lead_class)
        coverage["modes"].add(lineup.lead.binding.mode)
        for agent in lineup.agents.values():
            coverage["agent_lines"].add(agent.binding.key)
            coverage["modes"].add(agent.binding.mode)
        if lineup.lead_providers is not None and len(rows) < len(
            oracle_lead_rows(lines, case.enabled, lineup.lead_class, None)
        ):
            coverage["narrowing"].add(True)
        if fallback:
            coverage["fallback"].add(True)
        coverage["workflow"].add(case.workflow_expect)

    def test_random_profiles(self) -> None:
        coverage: dict[str, set] = {
            key: set()
            for key in ("lead_class", "modes", "agent_lines", "narrowing", "fallback", "workflow")
        }
        for seed in SEEDS:
            rng = random.Random(seed)
            case = generate(rng, self.lcat)
            with self.subTest(seed=seed):
                self._check(case, rng, coverage)
        classes = {
            entry["context"]["ordinary_profile"]
            for entry in self.lines.values()
            if "lead" in entry["capabilities"] and entry["context"].get("ordinary_profile")
        }
        agent_lines = {k for k, e in self.lines.items() if "agents" in e["capabilities"]}
        self.assertEqual(coverage["lead_class"], classes)
        self.assertEqual(coverage["agent_lines"], agent_lines)
        self.assertEqual(coverage["modes"], {"client", "gateway"})
        self.assertEqual(coverage["narrowing"], {True})
        self.assertEqual(coverage["fallback"], {True})
        self.assertEqual(coverage["workflow"], {"none", "member", "refused"})

    def test_disabled_bound_provider_is_refused(self) -> None:
        # Second sampler: E9 at evaluation, a fence gap at compile.
        hits = 0
        for seed in SEEDS:
            rng = random.Random(10_000 + seed)
            case = generate(rng, self.lcat)
            lead_provider = self.lines[case.doc["lead"]["model"]]["provider"]
            agent_providers = sorted(
                {self.lines[b["model"]]["provider"] for b in case.doc["agents"].values()}
                - {lead_provider}
            )
            if not agent_providers:
                continue
            victim = rng.choice(agent_providers)
            permissive = settings.effective(
                {"version": 1}, provider_ids=self.lcat.providers, line_keys=self.lcat.lines
            )
            restrictive = settings.effective(
                {"version": 1, "providers": {victim: {"enabled": False}}},
                provider_ids=self.lcat.providers,
                line_keys=self.lcat.lines,
            )
            with self.subTest(seed=seed, victim=victim):
                result = profile.evaluate(case.doc, self.lcat, effective=restrictive)
                self.assertTrue(
                    any(f"provider {victim!r} is disabled in Settings" in e for e in result.errors)
                )
                lineup = profile.resolve(case.doc, self.lcat, effective=permissive)
                with self.assertRaisesRegex(ScopeError, "outside the session fence"):
                    compile_plan(self.bundle, self.lcat, restrictive, lineup)
                hits += 1
        self.assertGreater(hits, 50)


@uses_shipped_catalog
class ShippedShapeTests(unittest.TestCase):
    """Shipped seeds on the shipped catalog: no ids named, relations only."""

    def test_every_shipped_seed_compiles_with_the_fence_properties(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        lcat = profile.LineupCatalog.from_docs(bundle.docs)
        lines, providers = dict(lcat.lines), dict(lcat.providers)
        eff = settings.effective({"version": 1}, provider_ids=lcat.providers, line_keys=lcat.lines)
        enabled = {pid: True for pid in providers}
        unnarrowed: list[list[str]] = []
        for name in catalog.SEED_PROFILE_NAMES:
            with self.subTest(seed=name):
                lineup = profile.resolve(bundle.seed_profiles[name], lcat, effective=eff)
                plan = compile_plan(bundle, lcat, eff, lineup)
                available = plan.settings["availableModels"]
                doc = strict_json.loads(plan.other_files["lead-set.json"])
                rows = oracle_lead_rows(lines, enabled, lineup.lead_class, lineup.lead_providers)
                fallback = oracle_fallback(lines, providers, rows)
                lead_rows = {s for _k, _e, s in rows}
                bound = {a.binding.selector for a in lineup.agents.values()}
                self.assertLessEqual(bound | {lineup.lead.binding.selector}, set(available))
                self.assertEqual([(r["key"], r["effort"], r["selector"]) for r in doc["rows"]], rows)
                self.assertEqual(
                    available,
                    sorted(lead_rows | set(oracle_agent_set(lines, enabled)) | set(fallback)),
                )
                picker = {o["model"] for o in plan.settings["modelPicker"]["options"]}
                self.assertFalse(set(fallback) & (lead_rows | picker))
                percent = lineup.settings_overrides.get("compaction_percent", 90)
                self.assertEqual(doc["context"], oracle_context(lines, rows, percent))
                # Each family default whose line is in the lead class is a
                # lead-set selector (the picker's Default row is not refused).
                env = plan.settings["env"]
                for key, variable in FAMILY_VARS:
                    entry = lines.get(key)
                    if (
                        entry is not None
                        and variable in env
                        and entry["context"].get("ordinary_profile") == lineup.lead_class
                        and (lineup.lead_providers is None or entry["provider"] in lineup.lead_providers)
                    ):
                        self.assertIn(env[variable], lead_rows)
                if lineup.lead_providers is None:
                    unnarrowed.append(available)
        self.assertTrue(unnarrowed)
        self.assertEqual(len({tuple(a) for a in unnarrowed}), 1)


if __name__ == "__main__":
    unittest.main()
