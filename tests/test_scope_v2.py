"""The 3.0 (v2) scope plan — fence, agent files, settings, lineup files.

Behavioural tests run on the frozen fixture (``tests/_catalog.FIXTURE_ROOT``);
every expected id comes from the loaded catalog or a test-local copy, never a
shipped model id. Goldens (``tests/goldens/v2/``) are written by
``tests/bless.py`` from the fixture seeds ``balanced`` and ``direct``.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from claude_multi import catalog, compiler, custom, lineup_files, profile, scope, settings, state, strict_json
from claude_multi import lineup as lineup_mod
from claude_multi.scope import ScopeError
from _catalog import FIXTURE_ROOT, uses_shipped_catalog
from _golden import assertGolden

import bless

FIXED_SESSION = bless.FIXED_SESSION
HOOK3 = bless.V2_HOOK_COMMAND
HELPER = bless.V2_TOKEN_HELPER
NEVER_DENIED = (
    "Plan",
    "fork",
    "claude-code-guide",
    "statusline-setup",
    "web-fetch",
    "workflow-subagent",
)


def _bundle():
    return catalog.load_catalog(FIXTURE_ROOT)


def _lcat(docs: dict[str, Any]) -> profile.LineupCatalog:
    return profile.LineupCatalog.from_docs(docs)


def _eff(lcat: profile.LineupCatalog, doc: dict[str, Any] | None = None, **fields: Any):
    eff = settings.effective(
        doc or {"version": 1}, provider_ids=lcat.providers, line_keys=lcat.lines
    )
    return dataclasses.replace(eff, **fields) if fields else eff


def _disabled(lcat: profile.LineupCatalog, *provider_ids: str, **fields: Any):
    doc = {"version": 1, "providers": {pid: {"enabled": False} for pid in provider_ids}}
    return _eff(lcat, doc, **fields)


def _compile(
    lineup,
    lcat,
    eff,
    bundle,
    *,
    lifecycle: bool = True,
    generation: int = 1,
    **kw: Any,
):
    extra: dict[str, Any] = {}
    if lifecycle:
        extra = {
            "managed_id": FIXED_SESSION,
            "hook_command": HOOK3,
            "token_helper_command": HELPER,
        }
    extra.update(kw)
    return scope.compile_lineup_scope(
        lineup,
        lcat,
        eff,
        bundle.prompt_bodies,
        scope.catalog_meta_v2(bundle.docs),
        lineup_generation=generation,
        **extra,
    )


def _seed(name: str, *, eff=None, lcat=None, bundle=None):
    bundle = bundle or _bundle()
    lcat = lcat or _lcat(bundle.docs)
    eff = eff or _eff(lcat)
    lineup = profile.resolve(bundle.seed_profiles[name], lcat, effective=eff)
    return bundle, lcat, eff, lineup


def _plan(name: str = "balanced", **kw: Any):
    bundle, lcat, eff, lineup = _seed(name)
    return lineup, _compile(lineup, lcat, eff, bundle, **kw)


def _frontmatter(data: bytes) -> list[tuple[str, str]]:
    lines = data.decode("utf-8").splitlines()
    assert lines[0] == "---"
    fields = []
    for line in lines[1:]:
        if line == "---":
            return fields
        key, _, value = line.partition(": ")
        fields.append((key, value))
    raise AssertionError("unterminated frontmatter")


def _lead_set(plan) -> dict[str, Any]:
    return strict_json.loads(plan.other_files[lineup_files.LEAD_SET_JSON])


def _md(plan) -> str:
    return plan.other_files[lineup_files.LINEUP_MD].decode("utf-8")


def _selectors(entry: dict[str, Any]) -> list[tuple[str | None, str]]:
    """Test oracle: (effort | None, selector) of a line, from the raw entry only."""

    efforts = entry["efforts"]
    if isinstance(efforts, list):
        return [(None, entry["selector"])]
    order = profile.EFFORT_ORDER
    return [(level, efforts[level]["selector"]) for level in sorted(efforts, key=order.index)]


def _offered(lcat, eff, key, entry) -> bool:
    status_ok = entry.get("status", "active") == "active" or key in eff.admitted_lines
    return status_ok and eff.providers_enabled.get(entry["provider"], True)


def _oracle_lead_rows(lcat, eff, lead_class, lead_providers):
    rows = []
    for key, entry in lcat.lines.items():
        if not _offered(lcat, eff, key, entry):
            continue
        if "lead" not in entry["capabilities"] or entry.get("lead") is None:
            continue
        if entry["context"].get("ordinary_profile") != lead_class:
            continue
        if lead_providers is not None and entry["provider"] not in lead_providers:
            continue
        for effort, selector in _selectors(entry):
            rows.append((key, effort, selector))
    return rows


def _oracle_agent_set(lcat, eff):
    return sorted(
        {
            selector
            for key, entry in lcat.lines.items()
            if _offered(lcat, eff, key, entry) and "agents" in entry["capabilities"]
            for _effort, selector in _selectors(entry)
        }
    )


# ------------------------------------------------------------------ goldens


class V2ScopeGoldenTests(unittest.TestCase):
    def test_every_file_matches_its_golden(self) -> None:
        for kind in bless.V2_SEEDS:
            root = bless.V2_GOLDENS / kind / "scope"
            files = bless.v2_scope_files(bless.v2_scope_plan(kind))
            for relpath, data in files.items():
                with self.subTest(kind=kind, relpath=relpath):
                    assertGolden(self, root.joinpath(*relpath.split("/")), data)

    def test_golden_trees_have_no_extra_files(self) -> None:
        for kind in bless.V2_SEEDS:
            root = bless.V2_GOLDENS / kind / "scope"
            on_disk = {
                str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
            }
            with self.subTest(kind=kind):
                self.assertEqual(
                    on_disk, set(bless.v2_scope_files(bless.v2_scope_plan(kind)))
                )

    def test_no_golden_carries_a_log_timestamp_or_store_path(self) -> None:
        for path in bless.V2_GOLDENS.rglob("*"):
            if not path.is_file():
                continue
            with self.subTest(path=str(path)):
                self.assertNotIn("lineup.log", path.name)
                text = path.read_text()
                self.assertNotIn("/nix/store", text)
                self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:", text))

    def test_direct_golden_has_no_agents(self) -> None:
        plan = bless.v2_scope_plan("direct")
        self.assertEqual(plan.agent_files, {})
        self.assertEqual(set(plan.other_files), set(lineup_files.LINEUP_FILES))


# -------------------------------------------------------------- agent files


class AgentFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle, self.lcat, self.eff, self.lineup = _seed("balanced")
        self.plan = _compile(self.lineup, self.lcat, self.eff, self.bundle)

    def test_one_file_per_bound_id_and_agent_names(self) -> None:
        self.assertEqual(
            set(self.plan.agent_files),
            {f".claude/agents/{rid}.md" for rid in self.lineup.agents},
        )
        self.assertEqual(self.plan.agent_names, frozenset(self.lineup.agents))
        # lineup/skill files never reach the collision gate.
        self.assertFalse({"SKILL", "lineup", "lead-set"} & self.plan.agent_names)

    def test_frontmatter_order_values_and_tool_keys(self) -> None:
        for rid, agent in self.lineup.agents.items():
            with self.subTest(rid=rid):
                fields = _frontmatter(self.plan.agent_files[f".claude/agents/{rid}.md"])
                keys = [key for key, _ in fields]
                expected = ["name", "description", "model", "effort"]
                if agent.role.isolation is not None:
                    expected.append("isolation")
                if agent.role.disallowed_tools:
                    expected.append("disallowedTools")
                self.assertEqual(keys, expected)
                values = dict(fields)
                self.assertEqual(values["name"], rid)
                self.assertEqual(values["model"], agent.binding.selector)
                self.assertEqual(values["effort"], agent.binding.effort)
                if agent.role.function in ("explorer", "reviewer"):
                    self.assertEqual(
                        values["disallowedTools"], "Edit, Write, NotebookEdit, Agent, Skill"
                    )
                else:
                    self.assertNotIn("disallowedTools", values)
                if agent.role.function == "implementer":
                    self.assertEqual(values["isolation"], "worktree")
                else:
                    self.assertNotIn("isolation", values)

    def test_description_is_role_text_plus_sentinel_and_model_free(self) -> None:
        tokens = catalog.identity_tokens(self.bundle.docs)
        strings = set(tokens)
        for key, entry in self.lcat.lines.items():
            strings |= {key.lower(), entry["display"].lower()}
            strings |= {selector.lower() for _e, selector in _selectors(entry)}
        for rid, agent in self.lineup.agents.items():
            with self.subTest(rid=rid):
                description = dict(
                    _frontmatter(self.plan.agent_files[f".claude/agents/{rid}.md"])
                )["description"]
                self.assertEqual(
                    description,
                    scope._yaml_double_quoted(
                        agent.role.description + " " + scope.SENTINEL_SUFFIX
                    ),
                )
                lowered = description.lower()
                for token in strings:
                    self.assertIsNone(
                        re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", lowered),
                        token,
                    )

    def test_body_is_prompt_bytes_identical_within_a_function(self) -> None:
        bodies: dict[str, bytes] = {}
        for rid, agent in self.lineup.agents.items():
            data = self.plan.agent_files[f".claude/agents/{rid}.md"]
            body = data.split(b"\n---\n\n", 1)[1]
            self.assertEqual(body, self.bundle.prompt_bodies[rid])
            previous = bodies.setdefault(agent.role.function, body)
            self.assertEqual(previous, body)

    def test_agent_ultracode_fails_closed(self) -> None:
        agent = self.lineup.agents["cm-analyst"]
        bad = dataclasses.replace(
            agent, binding=dataclasses.replace(agent.binding, effort="ultracode")
        )
        lineup = dataclasses.replace(
            self.lineup, agents={**self.lineup.agents, "cm-analyst": bad}
        )
        with self.assertRaisesRegex(ScopeError, "ultracode"):
            _compile(lineup, self.lcat, self.eff, self.bundle)


# --------------------------------------------------------------------- fence


class FenceTests(unittest.TestCase):
    def test_lead_set_matches_the_oracle_and_holds_the_lead(self) -> None:
        for name in catalog.SEED_PROFILE_NAMES:
            bundle, lcat, eff, lineup = _seed(name)
            plan = _compile(lineup, lcat, eff, bundle)
            doc = _lead_set(plan)
            with self.subTest(seed=name):
                self.assertEqual(
                    [(row["key"], row["effort"], row["selector"]) for row in doc["rows"]],
                    _oracle_lead_rows(lcat, eff, lineup.lead_class, lineup.lead_providers),
                )
                self.assertIn(
                    lineup.lead.binding.selector, [row["selector"] for row in doc["rows"]]
                )
                self.assertEqual(doc["lead"]["selector"], lineup.lead.binding.selector)
                self.assertEqual(doc["lead_class"], lineup.lead_class)

    def test_agent_only_and_other_class_selectors_stay_out_of_the_lead_set(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        plan = _compile(lineup, lcat, eff, bundle)
        rows = {row["selector"] for row in _lead_set(plan)["rows"]}
        for key, entry in lcat.lines.items():
            other = (
                "lead" not in entry["capabilities"]
                or entry["context"].get("ordinary_profile") != lineup.lead_class
            )
            if other:
                for _effort, selector in _selectors(entry):
                    with self.subTest(key=key):
                        self.assertNotIn(selector, rows)
        self.assertEqual(
            plan.settings["availableModels"],
            sorted(rows | set(_oracle_agent_set(lcat, eff)) | set(scope.fallback_only(
                scope.lead_set_rows(lineup, lcat, eff)))),
        )

    def test_fallback_only_for_anthropic_lead_sets(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        plan = _compile(lineup, lcat, eff, bundle)
        expected = {wire + "[1m]" for wire in catalog.REFUSAL_FALLBACK_WIRES}
        rows = {row["selector"] for row in _lead_set(plan)["rows"]}
        fallback = expected - rows
        self.assertTrue(fallback)
        self.assertLessEqual(fallback, set(plan.settings["availableModels"]))
        picker = {option["model"] for option in plan.settings["modelPicker"]["options"]}
        self.assertFalse(fallback & rows)
        self.assertFalse(fallback & picker)

    def test_non_anthropic_lead_class_has_no_fallback(self) -> None:
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        eff = _eff(lcat)
        # A lead-capable line in a class without an Anthropic line.
        key = next(
            key
            for key, entry in lcat.lines.items()
            if "lead" in entry["capabilities"]
            and entry["context"].get("ordinary_profile") is not None
            and all(
                catalog.line_family(other, lcat.providers) != "anthropic"
                for other in lcat.lines.values()
                if other["context"].get("ordinary_profile")
                == entry["context"]["ordinary_profile"]
            )
        )
        lineup = profile.resolve(profile.ad_hoc_direct(key), lcat, effective=eff, ad_hoc=True)
        fence = scope.compile_fence(lineup, lcat, eff)
        self.assertEqual(fence.fallback_only, ())
        for wire in catalog.REFUSAL_FALLBACK_WIRES:
            self.assertNotIn(wire + "[1m]", fence.available_models)
        plan = _compile(lineup, lcat, eff, bundle)
        # The picker's Default row names the Opus family default, refused.
        default = plan.settings["env"].get("ANTHROPIC_DEFAULT_OPUS_MODEL")
        self.assertIsNotNone(default)
        self.assertIn(f"/model's built-in Default row is `{default}`", _md(plan))

    def test_disabled_provider_drops_its_lines_from_both_sets(self) -> None:
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        eff = _disabled(lcat, "openai")
        _, _, _, lineup = _seed("direct", eff=eff, lcat=lcat, bundle=bundle)
        fence = scope.compile_fence(lineup, lcat, eff)
        openai = {
            selector
            for entry in lcat.lines.values()
            if entry["provider"] == "openai"
            for _e, selector in _selectors(entry)
        }
        self.assertTrue(openai)
        self.assertFalse(openai & set(fence.available_models))

    def test_lead_providers_narrows_only_the_lead_set(self) -> None:
        bundle, lcat, eff, lineup = _seed("direct")
        self.assertIsNotNone(lineup.lead_providers)
        fence = scope.compile_fence(lineup, lcat, eff)
        self.assertEqual({row.provider for row in fence.lead_set}, set(lineup.lead_providers))
        self.assertEqual(list(fence.agent_set), _oracle_agent_set(lcat, eff))
        unnarrowed = dataclasses.replace(lineup, lead_providers=None)
        wide = scope.compile_fence(unnarrowed, lcat, eff)
        self.assertLess(len(fence.lead_set), len(wide.lead_set))

    def test_new_line_absent_until_admitted(self) -> None:
        bundle = _bundle()
        docs = copy.deepcopy(bundle.docs)
        # A lead- and agents-capable line of the balanced lead class that
        # balanced does not bind.
        _, lcat0, _, lineup0 = _seed("balanced")
        bound = {lineup0.lead.binding.key} | {a.binding.key for a in lineup0.agents.values()}
        key = next(
            key
            for key, entry in lcat0.lines.items()
            if key not in bound
            and "agents" in entry["capabilities"]
            and entry["context"].get("ordinary_profile") == lineup0.lead_class
        )
        docs["models-v2"]["models"][key]["status"] = "new"
        lcat = _lcat(docs)
        selectors = {s for _e, s in _selectors(lcat.lines[key])}
        eff = _eff(lcat)
        lineup = profile.resolve(bundle.seed_profiles["balanced"], lcat, effective=eff)
        fence = scope.compile_fence(lineup, lcat, eff)
        self.assertFalse(selectors & set(fence.available_models))
        admitted = _eff(lcat, {"version": 1, "admitted_lines": [key]})
        fence = scope.compile_fence(lineup, lcat, admitted)
        self.assertLessEqual(selectors, set(fence.available_models))
        self.assertLessEqual(selectors, fence.lead_selectors)

    def test_retired_selectors_never_in_the_fence(self) -> None:
        for name in catalog.SEED_PROFILE_NAMES:
            bundle, lcat, eff, lineup = _seed(name)
            fence = scope.compile_fence(lineup, lcat, eff)
            for retired in lcat.retired.values():
                for selector in retired["selectors"]:
                    with self.subTest(seed=name, selector=selector):
                        self.assertNotIn(selector, fence.available_models)

    def test_empty_lead_set_and_lead_outside_it_fail_closed(self) -> None:
        bundle, lcat, eff, lineup = _seed("direct")
        restrictive = _disabled(lcat, "anthropic")
        with self.assertRaisesRegex(ScopeError, "lead set for class .* is empty"):
            scope.compile_fence(lineup, lcat, restrictive)
        # Lead's provider narrowed away while other rows stay.
        narrowed = dataclasses.replace(lineup, lead_providers=("openai",))
        with self.assertRaisesRegex(ScopeError, "not in the lead set"):
            scope.compile_fence(narrowed, lcat, eff)

    def test_fence_gaps_is_exact_membership(self) -> None:
        self.assertEqual(
            scope.fence_gaps(["a-b[1m]", "c"], ["a-b", "a-b[1m]", "c", "c-d", "z"]),
            ("a-b", "c-d", "z"),
        )
        self.assertEqual(scope.fence_gaps(["x"], []), ())

    def test_duplicate_labels_get_the_key(self) -> None:
        bundle = _bundle()
        docs = copy.deepcopy(bundle.docs)
        lines = docs["models-v2"]["models"]
        anthropic = [
            key
            for key, entry in lines.items()
            if entry["provider"] == "anthropic" and "lead" in entry["capabilities"]
        ]
        first, second = anthropic[:2]
        lines[second]["display"] = lines[first]["display"]
        lcat = _lcat(docs)
        eff = _eff(lcat)
        lineup = profile.resolve(bundle.seed_profiles["direct"], lcat, effective=eff)
        rows = scope.compile_fence(lineup, lcat, eff).lead_set
        labels = [row.label for row in rows]
        self.assertEqual(len(labels), len(set(labels)))
        dup = [row for row in rows if row.key in (first, second)]
        for row in dup:
            self.assertTrue(row.label.endswith(f" · {row.key}"))

    def test_row_labels_carry_effort_for_gateway_rows(self) -> None:
        _, plan = _plan("balanced")
        for row in _lead_set(plan)["rows"]:
            with self.subTest(selector=row["selector"]):
                parts = row["label"].split(" · ")
                if row["effort"] is not None:
                    self.assertIn(row["effort"], parts)
                self.assertIn(row["family"], parts)


class CustomLeadTests(unittest.TestCase):
    """An ad-hoc direct custom lead over merged docs."""

    def _merged(self):
        bundle = _bundle()
        registry = {
            "version": 1,
            "providers": {
                "my-gw": {
                    "base_url": "https://llm.example.invalid/v1",
                    "auth_kind": "bearer",
                    "secret_env": "MY_GW_API_KEY",
                }
            },
            "models": {
                "my-model": {
                    "provider": "my-gw",
                    "wire_model": "my-wire",
                    "context_tokens": 262144,
                }
            },
        }
        return bundle, custom.merge_docs(bundle.docs, registry)

    def test_custom_lead_compiles_with_a_custom_only_lead_set(self) -> None:
        bundle, docs = self._merged()
        lcat = _lcat(docs)
        eff = _eff(lcat)
        entry = lcat.lines["my-model"]
        self.assertIsInstance(entry["efforts"], list)
        lineup = profile.resolve(
            profile.ad_hoc_direct("my-model", "high"), lcat, effective=eff, ad_hoc=True
        )
        plan = _compile(lineup, lcat, eff, bundle)
        doc = _lead_set(plan)
        self.assertEqual(
            [(row["key"], row["family"], row["source"], row["effort"]) for row in doc["rows"]],
            [("my-model", "custom", "custom", None)],
        )
        self.assertEqual(plan.settings["model"], entry["selector"])
        self.assertEqual(scope.compile_fence(lineup, lcat, eff).fallback_only, ())
        self.assertIn("ad-hoc direct", _md(plan))
        # Family defaults follow the offered lines (here Anthropic is
        # offered, so they are compiled; see the next test for none).
        self.assertIn("MY_GW_API_KEY", scope.line_view(lcat, eff).secret_env_names)

    def test_custom_lead_without_anthropic_has_no_family_default_or_fallback(self) -> None:
        bundle, docs = self._merged()
        lcat = _lcat(docs)
        others = [pid for pid in lcat.providers if pid != "my-gw"]
        eff = _disabled(lcat, *others)
        lineup = profile.resolve(
            profile.ad_hoc_direct("my-model", "high"), lcat, effective=eff, ad_hoc=True
        )
        plan = _compile(lineup, lcat, eff, bundle)
        env = plan.settings["env"]
        self.assertFalse([key for key in env if key.startswith("ANTHROPIC_DEFAULT_")])
        self.assertEqual(plan.settings["availableModels"], [lcat.lines["my-model"]["selector"]])
        self.assertNotIn("Default row", _md(plan))


class LineViewTests(unittest.TestCase):
    def test_sources_and_secret_names(self) -> None:
        bundle = _bundle()
        lcat = _lcat(bundle.docs)
        view = scope.line_view(lcat, _eff(lcat))
        self.assertEqual({line.source for line in view.lines}, {"catalog"})
        self.assertEqual([line.key for line in view.lines], list(lcat.lines))
        expected = sorted(
            {
                provider["transport"]["auth"]["secret_ref"].removeprefix("env:")
                for provider in lcat.providers.values()
                if isinstance(provider["transport"].get("auth"), dict)
                and "secret_ref" in provider["transport"]["auth"]
            }
        )
        self.assertEqual(list(view.secret_env_names), expected)


# ---------------------------------------------------------------- assertions


class FenceAssertionTests(unittest.TestCase):
    def test_agent_on_a_disabled_provider_fails_closed(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        provider = lineup.agents["cm-explorer"].binding.provider
        self.assertNotEqual(provider, lineup.lead.binding.provider)
        restrictive = _disabled(lcat, provider)
        with self.assertRaises(ScopeError) as caught:
            _compile(lineup, lcat, restrictive, bundle)
        message = str(caught.exception)
        self.assertIn("cm-explorer", message)
        self.assertIn(lineup.agents["cm-explorer"].binding.selector, message)

    def _agents_line(self, lcat, *, client: bool | None = None):
        for key, entry in lcat.lines.items():
            if "agents" not in entry["capabilities"]:
                continue
            if client is None or isinstance(entry["efforts"], list) == client:
                return key, entry
        raise AssertionError("no such line")

    def test_workflow_default_binding_compiles_to_the_subagent_model(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        key, entry = self._agents_line(lcat, client=False)
        effort = profile.declared_efforts(entry)[0]
        eff = dataclasses.replace(eff, workflow_default_binding={"model": key, "effort": effort})
        plan = _compile(lineup, lcat, eff, bundle)
        selector = entry["efforts"][effort]["selector"]
        self.assertEqual(plan.settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], selector)
        self.assertIn(selector, plan.settings["availableModels"])
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", plan.settings["env"])
        # lineup.md names the workflow default with the probed reach
        # (SettingsEnvPlacementProbe case iii).
        self.assertIn(
            compiler.workflow_default_line(selector, lineup.native_agents), _md(plan)
        )
        self.assertIn(
            f"- Workflow agents without a cm-* agentType run on `{selector}` "
            "(Settings workflow_default_binding); native Explore and Plan are not "
            "affected.",
            _md(plan),
        )

    def test_workflow_default_refusals_name_the_setting(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        lead_only = next(
            key for key, entry in lcat.lines.items() if "agents" not in entry["capabilities"]
        )
        gateway_key, gateway = self._agents_line(lcat, client=False)
        undeclared = next(e for e in profile.EFFORT_ORDER if e not in gateway["efforts"])
        retired_null = next(k for k, v in lcat.retired.items() if v["successor"] is None)
        disabled_provider = gateway["provider"]
        cases = [
            ({"model": lead_only, "effort": "high"}, eff, ".model"),
            ({"model": gateway_key, "effort": undeclared}, eff, ".effort"),
            ({"model": gateway_key, "effort": "ultracode"}, eff, ".effort"),
            ({"model": retired_null, "effort": "high"}, eff, ".model"),
            ({"model": "no-such-line", "effort": "high"}, eff, ".model"),
            (
                {"model": gateway_key, "effort": profile.declared_efforts(gateway)[0]},
                _disabled(lcat, disabled_provider),
                ".model",
            ),
        ]
        for binding, base, field in cases:
            with self.subTest(binding=binding):
                with self.assertRaisesRegex(
                    ScopeError, re.escape("Settings workflow_default_binding" + field)
                ):
                    scope.workflow_default_selector(
                        lcat, dataclasses.replace(base, workflow_default_binding=binding)
                    )

    def test_client_effort_workflow_default_only_at_its_default_effort(self) -> None:
        # A test-local client-effort line with two efforts.
        bundle = _bundle()
        docs = copy.deepcopy(bundle.docs)
        lcat0 = _lcat(docs)
        key, entry = self._agents_line(lcat0, client=True)
        line = docs["models-v2"]["models"][key]
        other = next(e for e in profile.EFFORT_ORDER if e != line["default_effort"])
        line["efforts"] = sorted({*line["efforts"], other}, key=profile.EFFORT_ORDER.index)
        lcat = _lcat(docs)
        eff = _eff(lcat)
        ok = dataclasses.replace(
            eff, workflow_default_binding={"model": key, "effort": line["default_effort"]}
        )
        self.assertEqual(scope.workflow_default_selector(lcat, ok), line["selector"])
        bad = dataclasses.replace(eff, workflow_default_binding={"model": key, "effort": other})
        with self.assertRaisesRegex(ScopeError, re.escape("workflow_default_binding.effort")):
            scope.workflow_default_selector(lcat, bad)

    def test_off_by_default(self) -> None:
        _, plan = _plan("balanced")
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", plan.settings["env"])
        self.assertNotIn("Workflow agents without", _md(plan))


# ------------------------------------------------------------------ settings


class SettingsTests(unittest.TestCase):
    def test_key_set_with_and_without_lifecycle(self) -> None:
        _, plan = _plan("balanced")
        self.assertEqual(
            set(plan.settings),
            {
                "disableWorkflows",
                "workflowSizeGuideline",
                "workflowKeywordTriggerEnabled",
                "permissions",
                "availableModels",
                "model",
                "modelPicker",
                "autoCompactEnabled",
                "disableAllHooks",
                "worktree",
                "env",
                "hooks",
                "apiKeyHelper",
            },
        )
        self.assertLessEqual(set(plan.settings), scope.COMPILED_SETTINGS_KEYS)
        _, bare = _plan("balanced", lifecycle=False)
        self.assertNotIn("hooks", bare.settings)
        self.assertNotIn("apiKeyHelper", bare.settings)
        env = bare.settings["env"]
        for key in (
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_RETRY_WATCHDOG",
            "CLAUDE_MULTI_MANAGED_ID",
            "CLAUDE_MULTI_SESSION_ID",
            "CLAUDE_MULTI_LAUNCH_EPOCH",
        ):
            self.assertNotIn(key, env)

    def test_lifecycle_inputs_come_together(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        with self.assertRaisesRegex(ScopeError, "together"):
            _compile(lineup, lcat, eff, bundle, lifecycle=False, managed_id=FIXED_SESSION)
        with self.assertRaisesRegex(ScopeError, "together"):
            _compile(lineup, lcat, eff, bundle, lifecycle=False, hook_command=HOOK3)

    def test_model_picker_is_the_lead_set(self) -> None:
        lineup, plan = _plan("balanced")
        picker = plan.settings["modelPicker"]
        rows = _lead_set(plan)["rows"]
        self.assertIs(picker["replaceBuiltInOptions"], True)
        self.assertEqual([o["model"] for o in picker["options"]], [r["selector"] for r in rows])
        self.assertEqual([o["label"] for o in picker["options"]], [r["label"] for r in rows])
        self.assertEqual(
            [o["description"] for o in picker["options"]],
            [f"claude-multi · {r['key']}" for r in rows],
        )
        labels = [o["label"] for o in picker["options"]]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(plan.settings["model"], lineup.lead.binding.selector)

    def test_worktree_compaction_and_watchdog_for_every_seed(self) -> None:
        for name in catalog.SEED_PROFILE_NAMES:
            _, plan = _plan(name)
            with self.subTest(seed=name):
                self.assertEqual(plan.settings["worktree"], {"baseRef": "head"})
                self.assertIs(plan.settings["autoCompactEnabled"], True)
                self.assertIs(plan.settings["disableAllHooks"], False)
                self.assertEqual(plan.settings["env"]["CLAUDE_CODE_RETRY_WATCHDOG"], "1")

    def test_compaction_flag_env_matches_process_env_for_each_context_class(self) -> None:
        bundle = _bundle()
        for client_tokens, scalar in ((200000, None), (200000, 150000), (1000000, None), (1000000, 150000)):
            for percent in (70, 90):
                with self.subTest(client_tokens=client_tokens, scalar=scalar, percent=percent):
                    docs = copy.deepcopy(bundle.docs)
                    # One synthetic lead class, built from fixture data only.
                    key = "kimi-k3"
                    entry = docs["models-v2"]["models"][key]
                    entry["context"].update(client_tokens=client_tokens, provider_tokens=client_tokens,
                                            scalar_tokens=scalar, ordinary_profile="probe-context")
                    lcat = _lcat(docs)
                    eff = _eff(lcat)
                    document = profile.ad_hoc_direct(key)
                    lineup = profile.resolve(document, lcat, effective=eff, ad_hoc=True)
                    lineup = dataclasses.replace(lineup, settings_overrides={"compaction_percent": percent})
                    result = compiler.compile_lineup_launch(
                        docs=docs, prompt_bodies=bundle.prompt_bodies, lineup=lineup, effective=eff,
                        session_action=compiler.build_fresh(FIXED_SESSION), lineup_generation=1,
                        state_root=Path("/fixture/state"), scope_dir=Path("/fixture/scope"), hook_command=HOOK3,
                        token_helper_command=HELPER, worktree_available=True,
                    )
                    plan = _compile(lineup, lcat, eff, bundle)
                    keys = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
                            "CLAUDE_CODE_MAX_CONTEXT_TOKENS")
                    self.assertEqual({key: plan.settings["env"][key] for key in keys if key in plan.settings["env"]},
                                     {key: result.env_set[key] for key in keys if key in result.env_set})
                    self.assertEqual(plan.settings["env"][keys[1]], str(percent))
                    self.assertEqual(keys[2] in plan.settings["env"], scalar is not None and client_tokens < 1000000)

    def test_explore_cap_setting(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        on = _compile(lineup, lcat, eff, bundle)
        self.assertEqual(on.settings["env"]["CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP"], "1")
        off = _compile(
            lineup, lcat, dataclasses.replace(eff, explore_inherit_cap_disabled=False), bundle
        )
        self.assertNotIn("CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP", off.settings["env"])

    def test_family_defaults_follow_offered_lines(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        plan = _compile(lineup, lcat, eff, bundle)
        offered = {k: e for k, e in lcat.lines.items() if _offered(lcat, eff, k, e)}
        expected = compiler.family_default_env(offered, dict(lcat.providers))
        env = plan.settings["env"]
        self.assertEqual({k: env[k] for k in env if k.startswith("ANTHROPIC_DEFAULT_")}, expected)
        self.assertTrue(expected)
        # A New·Off family line is not offered: its variable is omitted.
        docs = copy.deepcopy(bundle.docs)
        family_key = next(key for key, _var in compiler.FAMILY_DEFAULT_LINES if key in lcat.lines)
        variable = dict(compiler.FAMILY_DEFAULT_LINES)[family_key]
        docs["models-v2"]["models"][family_key]["status"] = "new"
        lcat2 = _lcat(docs)
        eff2 = _eff(lcat2)
        lineup2 = profile.resolve(bundle.seed_profiles["openai"], lcat2, effective=eff2)
        plan2 = _compile(lineup2, lcat2, eff2, bundle)
        self.assertNotIn(variable, plan2.settings["env"])

    def test_no_subagents_turns_workflows_off(self) -> None:
        for name in catalog.SEED_PROFILE_NAMES:
            _, plan = _plan(name, no_subagents=True)
            with self.subTest(seed=name):
                self.assertIs(plan.settings["disableWorkflows"], True)
                self.assertEqual(
                    plan.settings["permissions"]["deny"],
                    ["Agent", *scope.MANAGED_SKILL_DENIES, *scope.SECRET_PATH_DENIES],
                )
                self.assertIn("delegation is disabled for this session", _md(plan))

    def test_workflows_off_profile(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        off = dataclasses.replace(lineup, workflows="off")
        self.assertIs(_compile(off, lcat, eff, bundle).settings["disableWorkflows"], True)
        self.assertIs(_compile(lineup, lcat, eff, bundle).settings["disableWorkflows"], False)


class HookTests(unittest.TestCase):
    def test_six_protocol_3_hooks_without_matcher(self) -> None:
        _, plan = _plan("balanced", launch_epoch=7)
        hooks = plan.settings["hooks"]
        self.assertEqual(set(hooks), {name for name, _ in scope.HOOK_EVENTS_V3})
        for name, event in scope.HOOK_EVENTS_V3:
            with self.subTest(event=name):
                self.assertEqual(len(hooks[name]), 1)
                group = hooks[name][0]
                self.assertEqual(set(group), {"hooks"})
                (hook,) = group["hooks"]
                self.assertEqual(
                    hook,
                    {
                        "type": "command",
                        "command": f"{HOOK3} session-event {event} --managed-id "
                        f"{FIXED_SESSION} --launch-epoch 7 --hook-protocol 3",
                        "timeout": 5,
                    },
                )
        self.assertEqual(plan.settings["env"]["CLAUDE_MULTI_LAUNCH_EPOCH"], "7")
        self.assertNotIn("transcript", strict_json.canonical_bytes(plan.settings).decode())

    def test_the_2x_shim_is_refused_and_epoch_validated(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        with self.assertRaisesRegex(ScopeError, "claude-multi-hook-3"):
            _compile(lineup, lcat, eff, bundle, hook_command="/state/bin/claude-multi-hook")
        for epoch in (-1, True, "1"):
            with self.subTest(epoch=epoch):
                with self.assertRaisesRegex(ScopeError, "launch_epoch"):
                    _compile(lineup, lcat, eff, bundle, launch_epoch=epoch)
        with self.assertRaisesRegex(ScopeError, "UUIDv4"):
            _compile(lineup, lcat, eff, bundle, managed_id="not-a-uuid")

    def test_hook_command_is_shell_quoted(self) -> None:
        _, plan = _plan("balanced", hook_command="/st ate/bin/claude-multi-hook-3")
        command = plan.settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertTrue(command.startswith("'/st ate/bin/claude-multi-hook-3' session-event"))


class DenyTests(unittest.TestCase):
    def test_seed_denies(self) -> None:
        for name in catalog.SEED_PROFILE_NAMES:
            lineup, plan = _plan(name)
            deny = plan.settings["permissions"]["deny"]
            with self.subTest(seed=name):
                self.assertEqual(deny[-len(scope.SECRET_PATH_DENIES):], list(scope.SECRET_PATH_DENIES))
                # The named skill denies sit between the agent and secret denies.
                policy = deny[-len(scope.SECRET_PATH_DENIES) - len(scope.MANAGED_SKILL_DENIES):
                              -len(scope.SECRET_PATH_DENIES)]
                self.assertEqual(policy, list(scope.MANAGED_SKILL_DENIES))
                agent_denies = deny[: -len(scope.SECRET_PATH_DENIES) - len(scope.MANAGED_SKILL_DENIES)]
                if lineup.is_direct:
                    self.assertEqual(agent_denies, ["Agent(claude)"])
                else:
                    self.assertEqual(
                        agent_denies,
                        ["Agent(Explore)", "Agent(general-purpose)", "Agent(claude)"],
                    )
                for native in NEVER_DENIED:
                    self.assertNotIn(f"Agent({native})", deny)
                for rid in catalog.ROLE_IDS:
                    self.assertNotIn(f"Agent({rid})", deny)
                self.assertFalse([item for item in deny if item.startswith("Agent(cm-")])

    def test_secret_denies_are_home_relative(self) -> None:
        for item in scope.SECRET_PATH_DENIES:
            self.assertRegex(item, r"^(Read|Edit)\(~/")
        self.assertEqual(
            list(scope.SECRET_PATH_DENIES),
            [
                "Read(~/.config/claude-multi/**)",
                "Read(~/.local/share/claude-multi/**)",
                "Read(~/.config/anthropic/**)",
                "Read(~/.claude/.credentials.json)",
                "Edit(~/.local/state/claude-multi/**)",
                "Edit(~/.config/claude-multi/**)",
            ],
        )


# -------------------------------------------------------------- lineup files


class LineupFileTests(unittest.TestCase):
    def test_lineup_md_contents(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        eff = dataclasses.replace(eff, review_round_cap=3)
        plan = _compile(lineup, lcat, eff, bundle, generation=42)
        md = _md(plan)
        self.assertTrue(md.startswith("# claude-multi lineup (lineup_generation 42)\n"))
        self.assertTrue(md.endswith("\n") and not md.endswith("\n\n"))
        lead = lineup.lead.binding
        self.assertIn(
            f"- lead: {lead.display} · {lead.effort} · {lead.family} · `{lead.selector}`", md
        )
        for rid, agent in lineup.agents.items():
            b = agent.binding
            with self.subTest(rid=rid):
                self.assertIn(f"- `{rid}`: {b.display} · {b.effort} · {b.family} · `{b.selector}`", md)
        for row in lineup.routing.rows:
            author = "lead" if row.author == profile.LEAD_ROLE else row.author
            self.assertIn(f"| {author} ({row.family}) |", md)
        self.assertIn("Review rounds: at most 3 per change set", md)
        for needle in ("/reload-plugins", "SendMessage-continue", "press `s`", "generation 42."):
            self.assertIn(needle, md)
        self.assertIn("- not bound: " + ", ".join(f"`{r}`" for r in lineup.unbound), md)
        for finding in lineup.warnings:
            self.assertIn(f"- ! {finding.message}", md)
        self.assertIn("## Provider exhaustion", md)

    def test_direct_lines(self) -> None:
        lineup, plan = _plan("direct")
        md = _md(plan)
        self.assertIn("- none: this session has no cm-* agents", md)
        self.assertNotIn("not bound:", md)
        self.assertNotIn("## Provider exhaustion", md)
        self.assertIn(f"({len(_lead_set(plan)['rows'])} entries)", md)

    def test_round_two_rule_follows_the_current_routing_table(self) -> None:
        # The reviewer's family changes between lineups.
        bundle, lcat, eff, lineup = _seed("balanced")
        md1 = _md(_compile(lineup, lcat, eff, bundle))
        self.assertIn("first re-read the routing table of the current lineup_generation", md1)
        doc = copy.deepcopy(bundle.seed_profiles["balanced"])
        doc["agents"]["cm-reviewer"] = dict(doc["agents"]["cm-reviewer-strong"])
        lineup2 = profile.resolve(doc, lcat, effective=eff)
        md2 = _md(_compile(lineup2, lcat, eff, bundle, generation=2))

        def rows(md: str) -> list[str]:
            return [line for line in md.splitlines() if line.startswith("| ") and "(" in line]

        self.assertNotEqual(rows(md1), rows(md2))

    def test_writer_grades_unavailable_outside_a_git_work_tree(self) -> None:
        # Worktree-isolated writers are marked unavailable outside a work tree.
        bundle, lcat, eff, lineup = _seed("balanced")
        md = _md(_compile(lineup, lcat, eff, bundle, worktree_available=False))
        for rid, agent in lineup.agents.items():
            line = next(l for l in md.splitlines() if l.startswith(f"- `{rid}`:"))
            with self.subTest(rid=rid):
                if agent.role.isolation == "worktree":
                    self.assertIn(scope.WRITER_UNAVAILABLE_NOTE, line)
                else:
                    self.assertNotIn("unavailable here", line)
        self.assertNotIn("unavailable here", _md(_compile(lineup, lcat, eff, bundle)))

    def test_strict_tool_schema_warning_and_notices_reach_checks(self) -> None:
        bundle = _bundle()
        docs = copy.deepcopy(bundle.docs)
        lines = docs["models-v2"]["models"]
        template_key = next(
            key
            for key, entry in lines.items()
            if isinstance(entry["efforts"], dict)
            and entry["roles"] == "all"
            and lcat_provider_adapter(docs, entry) == "cliproxy-claude-compatible-v1"
        )
        meta_line = copy.deepcopy(lines[template_key])
        meta_line["provider"] = "meta"
        meta_line["efforts"] = {
            level: {"selector": f"claude-multi-strictline-{level}", "proxy_contract": spec["proxy_contract"]}
            for level, spec in meta_line["efforts"].items()
        }
        lines["strictline"] = meta_line
        # A retired key with a live successor (a notice).
        live = next(iter(lines))
        docs["retired"]["retired"]["gone-line"] = {
            **copy.deepcopy(next(iter(docs["retired"]["retired"].values()))),
            "successor": live,
            "roles": "all",
        }
        lcat = _lcat(docs)
        eff = _eff(lcat)
        doc = copy.deepcopy(bundle.seed_profiles["balanced"])
        doc["agents"]["cm-designer"] = {
            "model": "strictline",
            "effort": profile.declared_efforts(meta_line)[0],
        }
        lineup = profile.resolve(doc, lcat, effective=eff)
        codes = {finding.code for finding in lineup.warnings}
        self.assertIn("strict-tool-schema", codes)
        md = _md(_compile(lineup, lcat, eff, bundle))
        strict = next(f for f in lineup.warnings if f.code == "strict-tool-schema")
        self.assertIn(f"- ! {strict.message}", md)
        # Notices (retired successor) are listed without the "!" marker.
        doc2 = copy.deepcopy(bundle.seed_profiles["direct"])
        if "lead" in lines[live]["capabilities"]:
            doc2["lead"] = {"model": "gone-line", "effort": "ultracode"}
            doc2.pop("lead_providers", None)
            lineup2 = profile.resolve(doc2, lcat, effective=eff)
            self.assertTrue(lineup2.notices)
            md2 = _md(_compile(lineup2, lcat, eff, bundle))
            for finding in lineup2.notices:
                self.assertIn(f"- {finding.message}", md2)

    def test_checks_omitted_without_findings(self) -> None:
        lineup, plan = _plan("direct")
        self.assertFalse(lineup.warnings or lineup.notices)
        self.assertNotIn("## Checks", _md(plan))

    def test_size_limit_fails_closed(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        saved = scope.LINEUP_MD_MAX_BYTES
        scope.LINEUP_MD_MAX_BYTES = 200
        try:
            with self.assertRaisesRegex(ScopeError, "over the 200-byte limit"):
                _compile(lineup, lcat, eff, bundle)
        finally:
            scope.LINEUP_MD_MAX_BYTES = saved

    def test_gen_line(self) -> None:
        _, plan = _plan("balanced", generation=4242)
        gen = plan.other_files[lineup_files.LINEUP_GEN].decode()
        md = plan.other_files[lineup_files.LINEUP_MD]
        self.assertEqual(gen, f"4242 {strict_json.sha256_hex(md)[:12]}\n")
        self.assertRegex(gen.rstrip("\n"), lineup_files.GEN_LINE)
        bundle, lcat, eff, lineup = _seed("balanced")
        for bad in (0, -1, True, lineup_files.GENERATION_MAX + 1, "1"):
            with self.subTest(generation=bad):
                with self.assertRaisesRegex(ScopeError, "lineup_generation"):
                    _compile(lineup, lcat, eff, bundle, generation=bad)

    def test_lead_set_json_shape(self) -> None:
        lineup, plan = _plan("direct")
        doc = _lead_set(plan)
        self.assertEqual(
            set(doc), {"context", "lead", "lead_class", "lead_providers", "rows", "version"}
        )
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["lead_providers"], sorted(lineup.lead_providers))
        self.assertEqual(set(doc["context"]), {"percent", "scalar", "trigger", "window"})
        for row in doc["rows"]:
            self.assertEqual(
                set(row),
                {"display", "effort", "family", "key", "label", "provider", "selector", "source"},
            )
        self.assertEqual(
            plan.other_files[lineup_files.LEAD_SET_JSON], strict_json.canonical_file_bytes(doc)
        )

    def test_lead_set_context_uses_the_profile_formula_and_override(self) -> None:
        bundle, lcat, eff, lineup = _seed("balanced")
        doc = _lead_set(_compile(lineup, lcat, eff, bundle))
        rows = [lcat.lines[row["key"]]["context"] for row in doc["rows"]]
        window = min(min(c["provider_tokens"] for c in rows), 800_000)
        self.assertEqual(doc["context"]["window"], window)
        self.assertEqual(doc["context"]["percent"], eff.compaction_percent)
        self.assertEqual(doc["context"]["trigger"], compiler.reactive_trigger(window, eff.compaction_percent))
        overridden = dataclasses.replace(lineup, settings_overrides={"compaction_percent": 70})
        doc70 = _lead_set(_compile(overridden, lcat, eff, bundle))
        self.assertEqual(doc70["context"]["percent"], 70)
        self.assertEqual(doc70["context"]["trigger"], compiler.reactive_trigger(window, 70))


def lcat_provider_adapter(docs: dict[str, Any], entry: dict[str, Any]) -> str:
    return docs["providers"]["providers"][entry["provider"]]["adapter"]


# ----------------------------------------------------------------------- skill


class SkillTests(unittest.TestCase):
    EXPECTED = (
        "---\n"
        "name: cm\n"
        "description: Show or change this claude-multi session's agent lineup "
        "(/cm, /cm profiles, /cm profile NAME, /cm set AGENT=MODEL[:EFFORT], /cm unset AGENT, "
        "/cm direct [MODEL[:EFFORT]], /cm pin, /cm follow, /cm fallback PROVIDER [--preview], "
        "/cm review [high-stakes] [RANGE], /cm quota)\n"
        'argument-hint: "profiles · profile NAME · set AGENT=MODEL[:EFFORT] · unset AGENT · '
        'direct [MODEL[:EFFORT]] · pin · follow · fallback PROVIDER [--preview] · '
        'review [high-stakes] [RANGE] · quota"\n'
        "disable-model-invocation: true\n"
        "allowed-tools: Bash(claude-multi lineup:*)\n"
        "---\n"
        "!`claude-multi lineup --session ${CLAUDE_SESSION_ID} '$ARGUMENTS'`\n"
    ).encode("utf-8")
    # The bytes earlier releases compiled; a live scope may still carry one
    # until its next resume or repair.
    PREVIOUS = (
        (
            b"---\n"
            b"name: cm\n"
            b"description: Show or change this claude-multi session's agent lineup "
            b"(/cm, /cm set AGENT=MODEL[:EFFORT], /cm unset AGENT, /cm profile NAME, "
            b"/cm direct, /cm pin, /cm follow)\n"
            b"disable-model-invocation: true\n"
            b"allowed-tools: Bash(claude-multi lineup:*)\n"
            b"---\n"
            b"!`claude-multi lineup --session ${CLAUDE_SESSION_ID} '$ARGUMENTS'`\n"
        ),
        (
            "---\n"
            "name: cm\n"
            "description: Show or change this claude-multi session's agent lineup "
            "(/cm, /cm profiles, /cm profile NAME, /cm set AGENT=MODEL[:EFFORT], /cm unset AGENT, "
            "/cm direct, /cm pin, /cm follow, /cm fallback PROVIDER, /cm review, /cm quota)\n"
            'argument-hint: "profile NAME · set AGENT=MODEL[:EFFORT] · unset AGENT · direct · pin · '
            'follow · profiles · fallback PROVIDER · review [high-stakes] [RANGE] · quota"\n'
            "disable-model-invocation: true\n"
            "allowed-tools: Bash(claude-multi lineup:*)\n"
            "---\n"
            "!`claude-multi lineup --session ${CLAUDE_SESSION_ID} '$ARGUMENTS'`\n"
        ).encode("utf-8"),
    )

    def test_compiled_cm_argument_hint(self) -> None:
        """Every compiled scope's /cm skill carries the hint
        listing the subcommands (the real client shows it: S14's /cm control)."""

        self.assertEqual(scope.PREVIOUS_SKILL_MD_BYTES, self.PREVIOUS)
        for name in catalog.SEED_PROFILE_NAMES:
            _, plan = _plan(name)
            text = plan.other_files[lineup_files.SKILL_RELPATH].decode("utf-8")
            frontmatter = dict(line.split(": ", 1) for line in text.split("---\n")[1].splitlines())
            with self.subTest(seed=name):
                hint = frontmatter["argument-hint"]
                self.assertTrue(hint.startswith('"') and hint.endswith('"'), hint)
                self.assertNotIn('"', hint[1:-1])
                for sub in ("profile NAME", "set AGENT=MODEL[:EFFORT]", "unset AGENT", "direct", "pin",
                            "follow", "profiles", "fallback PROVIDER", "review [high-stakes] [RANGE]",
                            "quota"):
                    self.assertIn(sub, hint)
                self.assertEqual(frontmatter["disable-model-invocation"], "true")
                # Every hinted verb parses (the grammar and the hint agree).
                for words in (["profiles"], ["profile", "balanced"], ["set", "implementer=sol:high"],
                              ["unset", "implementer"], ["direct"], ["pin"], ["follow"],
                              ["fallback", "openai"], ["review"], ["review", "high-stakes", "HEAD~1..HEAD"],
                              ["quota"]):
                    self.assertEqual(lineup_mod.parse_request(words).verb, words[0])
                hinted = [part.split()[0] for part in hint.strip('"').split(" · ")]
                self.assertEqual(hinted, [name for name in lineup_files.CM_VERB_NAMES if name != "show"])

    def test_a_scope_with_earlier_skill_bytes_is_lazy_state(self) -> None:
        _, plan = _plan("balanced")
        drifted = [f"{lineup_files.SKILL_RELPATH} content differs"]
        with tempfile.TemporaryDirectory(prefix="cm-skill-lazy-") as tmp:
            live = Path(tmp)
            target = live / lineup_files.SKILL_RELPATH
            target.parent.mkdir(parents=True, mode=0o700)
            for previous in self.PREVIOUS:
                with self.subTest(previous=previous[:80]):
                    state.atomic_write(target, previous)
                    self.assertTrue(scope.skill_policy_drift(live, plan, drifted))
            state.atomic_write(target, b"---\nname: cm\nsomething else\n")
            self.assertFalse(scope.skill_policy_drift(live, plan, drifted))

    def test_bytes_identical_for_every_session(self) -> None:
        self.assertEqual(scope.SKILL_MD_BYTES, self.EXPECTED)
        for name in catalog.SEED_PROFILE_NAMES:
            _, plan = _plan(name)
            with self.subTest(seed=name):
                self.assertEqual(plan.other_files[lineup_files.SKILL_RELPATH], self.EXPECTED)
        text = self.EXPECTED.decode()
        self.assertEqual(text.count("'$ARGUMENTS'"), 1)
        self.assertEqual(text.count("$ARGUMENTS"), 1)
        description = text.splitlines()[2]
        self.assertNotIn(": ", description.removeprefix("description: "))
        bundle = _bundle()
        for key, entry in bundle.lines.items():
            self.assertNotIn(entry["display"], text)
            for _effort, selector in _selectors(entry):
                self.assertNotIn(selector, text)


@unittest.skipUnless(shutil.which("sh"), "needs /bin/sh")
class SkillExpansionTests(unittest.TestCase):
    """Hermetic E C34 emulation: raw ``$ARGUMENTS`` substitution, then ``sh -c``.

    The one-argument refusal itself is the ``lineup`` parser's test.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-skill-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        stub = bindir / "claude-multi"
        stub.write_text(
            "#!/bin/sh\n"
            ': > "$CM_OUT"\n'
            'for arg in "$@"; do printf \'%s\\0\' "$arg" >> "$CM_OUT"; done\n'
        )
        stub.chmod(0o700)
        self.bindir = bindir
        line = SkillTests.EXPECTED.decode().splitlines()[-1]
        self.template = line.removeprefix("!`").removesuffix("`")

    def _run(self, arguments: str) -> tuple[int, list[str], str]:
        command = self.template.replace("${CLAUDE_SESSION_ID}", FIXED_SESSION).replace(
            "$ARGUMENTS", arguments
        )
        out = self.tmp / "argv"
        if out.exists():
            out.unlink()
        work = self.tmp / "cwd"
        work.mkdir(exist_ok=True)
        result = subprocess.run(
            ["sh", "-c", command],
            cwd=work,
            env={"PATH": f"{self.bindir}:/usr/bin:/bin", "CM_OUT": str(out)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        argv = out.read_bytes().split(b"\0")[:-1] if out.exists() else []
        return result.returncode, [a.decode() for a in argv], result.stderr

    def test_one_exact_argument_without_side_effects(self) -> None:
        for raw in ("set cm-explorer=opus[1m]", "a;b", "$(touch pwned)", "x `touch pwned2` y"):
            with self.subTest(raw=raw):
                code, argv, _ = self._run(raw)
                self.assertEqual(code, 0)
                self.assertEqual(argv, ["lineup", "--session", FIXED_SESSION, raw])
        self.assertEqual(sorted(p.name for p in (self.tmp / "cwd").iterdir()), [])

    def test_word_leading_bang_arrives_escaped(self) -> None:
        # The client rewrites a word-leading "!" to "\!" before substitution
        # (E C34); inside the single quotes it stays literal for the parser
        # (which normalises or refuses it).
        code, argv, _ = self._run("\\!profile claude")
        self.assertEqual(code, 0)
        self.assertEqual(argv[-1], "\\!profile claude")

    def test_balanced_apostrophes_split_and_odd_ones_fail(self) -> None:
        code, argv, _ = self._run("set a' 'b")
        self.assertEqual(code, 0)
        self.assertGreater(len(argv) - 3, 1)  # the launcher must refuse this
        code, argv, _ = self._run("it's")
        self.assertNotEqual(code, 0)
        self.assertEqual(argv, [])


# ----------------------------------------------------------------- plan hash


class PlanHashV2Tests(unittest.TestCase):
    def test_deterministic_and_content_sensitive(self) -> None:
        _, plan = _plan("balanced")
        _, again = _plan("balanced")
        self.assertEqual(scope.plan_hash(plan), scope.plan_hash(again))
        tampered = scope.ScopePlan(
            agent_files=plan.agent_files,
            settings=plan.settings,
            other_files={**plan.other_files, lineup_files.LINEUP_MD: b"x\n"},
        )
        self.assertNotEqual(scope.plan_hash(plan), scope.plan_hash(tampered))


class LeadContextTests(unittest.TestCase):
    """``compiler.reactive_trigger``/``lead_set_context`` (for
    ``lead-set.json``'s ``context``)."""

    def test_reactive_trigger_matches_the_2x_formula_at_90(self) -> None:
        from claude_multi import composition

        for window in (40_000, 200_000, 258_400, 500_000, 800_000, 983_616):
            with self.subTest(window=window):
                self.assertEqual(
                    compiler.reactive_trigger(window, 90),
                    composition.auto_compact_trigger(window),
                )
        for percent in (0, 101, True, "90"):
            with self.assertRaises(compiler.CompilerError):
                compiler.reactive_trigger(800_000, percent)
        with self.assertRaises(compiler.CompilerError):
            compiler.reactive_trigger(33_000, 90)

    def test_scalar_only_below_1m(self) -> None:
        bundle = _bundle()
        docs = copy.deepcopy(bundle.docs)
        lines = docs["models-v2"]["models"]
        key = next(
            k
            for k, e in lines.items()
            if "lead" in e["capabilities"]
            and e["context"]["client_tokens"] < 1_000_000
            and e["context"].get("ordinary_profile")
        )
        lines[key]["context"]["scalar_tokens"] = lines[key]["context"]["provider_tokens"] - 1000
        lcat = _lcat(docs)
        eff = _eff(lcat)
        lineup = profile.resolve(profile.ad_hoc_direct(key), lcat, effective=eff, ad_hoc=True)
        fence = scope.compile_fence(lineup, lcat, eff)
        context = compiler.lead_set_context(fence, 90)
        self.assertEqual(
            context.scalar, min(800_000, lines[key]["context"]["provider_tokens"] - 1000)
        )
        # A 1M lead class never exports a scalar (the fixture large class
        # has a scalar line).
        _, lcat2, eff2, lineup2 = _seed("balanced")
        fence2 = scope.compile_fence(lineup2, lcat2, eff2)
        self.assertTrue(any(row.scalar_tokens is not None for row in fence2.lead_set))
        self.assertIsNone(compiler.lead_set_context(fence2, 90).scalar)


class CatalogMetaV2Tests(unittest.TestCase):
    def test_reads_v2_only(self) -> None:
        bundle = _bundle()
        docs = dict(bundle.docs)
        docs["models"] = {"models": {}}  # the v1 view must not matter
        meta = scope.catalog_meta_v2(docs)
        expected = sorted(
            {s for entry in bundle.lines.values() for _e, s, _c in catalog.line_selectors(entry)}
        )
        self.assertEqual(list(meta.client_selectors), expected)
        self.assertEqual(meta.base_settings, bundle.docs["settings"])

    def test_no_v1_views_needed(self) -> None:
        # ``docs.get("models-v2", docs["models"])`` would evaluate the
        # default eagerly, so a map without the v1 views would raise.
        bundle = _bundle()
        docs = dict(bundle.docs)
        self.assertIn("models-v2", docs)
        self.assertIn("roles-v2", docs)
        docs.pop("models", None)
        docs.pop("roles", None)
        meta = scope.catalog_meta_v2(docs)
        self.assertEqual(meta, scope.catalog_meta_v2(bundle.docs))
        lcat = profile.LineupCatalog.from_docs(docs)
        self.assertEqual(lcat.lines, profile.LineupCatalog.from_docs(bundle.docs).lines)
        self.assertEqual(custom._v2_lines(docs), bundle.docs["models-v2"]["models"])


class SkillCollisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-skillcol-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _skill(self, base: Path, name: str = "cm") -> Path:
        path = base / ".claude" / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: x\n---\n")
        return path

    def test_cwd_ancestors_to_git_root_add_dirs_and_user_dir(self) -> None:
        repo = self.tmp / "repo"
        sub = repo / "a" / "b"
        sub.mkdir(parents=True)
        (repo / ".git").mkdir()
        inside = self._skill(repo / "a")
        self._skill(self.tmp)  # above the git root: not scanned
        extra = self.tmp / "extra"
        extra.mkdir()
        added = self._skill(extra)
        other = self._skill(extra, "other")
        user = self.tmp / "user-skills"
        (user / "cm").mkdir(parents=True)
        (user / "cm" / "SKILL.md").write_text("x")
        found = scope.find_skill_collisions(sub, [extra], user_skills_dir=user)
        self.assertEqual(
            sorted(str(path) for path, _ in found),
            sorted([str(inside), str(added), str(user / "cm" / "SKILL.md")]),
        )
        self.assertNotIn(str(other), [str(p) for p, _ in found])

    def test_unreadable_directories_are_tolerated(self) -> None:
        locked = self.tmp / "locked"
        locked.mkdir()
        self._skill(locked)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o700)
        self.assertIsInstance(scope.find_skill_collisions(self.tmp, [locked]), list)


# ------------------------------------------------------- scope effects


class _StateRootCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-effects-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)

    def _write(self, plan) -> Path:
        return scope.write_scope(self.root, FIXED_SESSION, plan)


class WriteScopeV2Tests(_StateRootCase):
    def test_files_0600_dirs_0700_and_nothing_else(self) -> None:
        for kind in bless.V2_SEEDS:
            with self.subTest(kind=kind):
                plan = bless.v2_scope_plan(kind)
                live = self._write(plan)
                expected = bless.v2_scope_files(plan)
                on_disk = {
                    path.relative_to(live).as_posix()
                    for path in live.rglob("*")
                    if not path.is_dir()
                }
                self.assertEqual(on_disk, set(expected))
                for relpath, data in expected.items():
                    target = live.joinpath(*relpath.split("/"))
                    self.assertEqual(target.read_bytes(), data)
                    self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o600)
                dirs = [live, *(p for p in live.rglob("*") if p.is_dir())]
                self.assertIn(live / ".claude" / "skills" / "cm", dirs)
                for directory in dirs:
                    self.assertEqual(
                        stat.S_IMODE(os.lstat(directory).st_mode), 0o700, str(directory)
                    )
                self.assertFalse((live / "lineup.log").exists())
                # The lineup log lives outside the scope and a write never
                # creates it.
                self.assertFalse((self.root / lineup_files.LINEUP_LOG_DIR).exists())
                self.assertEqual(scope.live_drift(live, plan), [])

    def test_rewrite_leaves_an_out_of_scope_log_untouched(self) -> None:
        from claude_multi import lineup_log

        plan = bless.v2_scope_plan("managed")
        self._write(plan)
        log = lineup_log.append(self.root, FIXED_SESSION, {"event": "x"})
        before = (log.read_bytes(), os.lstat(log).st_ino)
        self._write(bless.v2_scope_plan("direct"))
        self.assertEqual((log.read_bytes(), os.lstat(log).st_ino), before)


class StagePlanTests(_StateRootCase):
    def _base(self):
        return bless.v2_scope_plan("managed")

    def _variant(self, **changes):
        plan = self._base()
        return dataclasses.replace(plan, **changes)

    def test_overlapping_keys_refused_before_any_effect(self) -> None:
        plan = self._base()
        agent = next(iter(plan.agent_files))
        cases = {
            "agent-and-other": self._variant(
                other_files={**plan.other_files, agent: b"x"}
            ),
            "settings-json-as-other": self._variant(
                other_files={**plan.other_files, "settings.json": b"{}"}
            ),
            "settings-json-as-agent": self._variant(
                agent_files={**plan.agent_files, "./settings.json": b"x"}
            ),
            "normalised-duplicate": self._variant(
                agent_files={**plan.agent_files, "./" + lineup_files.LINEUP_MD: b"x"}
            ),
            "file-and-directory": self._variant(
                other_files={**plan.other_files, ".claude/skills": b"x"}
            ),
            "traversal": self._variant(other_files={"../escape": b"x"}),
            "absolute": self._variant(other_files={"/etc/passwd": b"x"}),
            "empty": self._variant(other_files={"": b"x"}),
            "not-bytes": self._variant(other_files={"x": "text"}),
        }
        for name, bad in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ScopeError):
                    scope.stage_plan(self.root, FIXED_SESSION, bad)
                with self.assertRaises(ScopeError):
                    scope.write_scope(self.root, FIXED_SESSION, bad)
                self.assertFalse((self.root / "scopes").exists())

    def test_stages_without_touching_the_live_scope(self) -> None:
        managed = self._base()
        live = self._write(managed)
        before = {p: p.read_bytes() for p in live.rglob("*") if p.is_file()}
        direct = bless.v2_scope_plan("direct")
        staging = scope.stage_plan(self.root, FIXED_SESSION, direct)
        self.assertEqual(staging, self.root / "scopes" / f".{FIXED_SESSION}.new")
        self.assertEqual({p: p.read_bytes() for p in live.rglob("*") if p.is_file()}, before)
        self.assertEqual(scope.live_drift(staging, direct), [])
        # A second stage replaces the stale staging tree.
        scope.stage_plan(self.root, FIXED_SESSION, managed)
        self.assertEqual(scope.live_drift(staging, managed), [])

    def test_requires_a_uuid4(self) -> None:
        with self.assertRaisesRegex(ScopeError, "UUIDv4"):
            scope.stage_plan(self.root, "not-a-uuid", self._base())


class LiveDriftV2Tests(_StateRootCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan = bless.v2_scope_plan("managed")
        self.live = self._write(self.plan)

    def test_transition_delegates(self) -> None:
        from claude_multi import transition

        self.assertIs(transition._live_drift, scope.live_drift)

    def test_fresh_scope_has_no_drift(self) -> None:
        self.assertEqual(scope.live_drift(self.live, self.plan), [])

    def test_tampered_other_file_content_differs(self) -> None:
        for relpath in lineup_files.LINEUP_FILES:
            with self.subTest(relpath=relpath):
                target = self.live.joinpath(*relpath.split("/"))
                original = target.read_bytes()
                target.write_bytes(original + b"tampered\n")
                self.assertEqual(
                    scope.live_drift(self.live, self.plan), [f"{relpath} content differs"]
                )
                target.write_bytes(original)
                self.assertEqual(scope.live_drift(self.live, self.plan), [])

    def test_missing_and_unexpected_files(self) -> None:
        (self.live / lineup_files.LINEUP_MD).unlink()
        extra = self.live / "stray.txt"
        extra.write_bytes(b"x")
        os.chmod(extra, 0o600)
        self.assertEqual(
            scope.live_drift(self.live, self.plan),
            ["unexpected file stray.txt", f"missing file {lineup_files.LINEUP_MD}"],
        )

    def test_a_lineup_log_inside_the_scope_is_drift(self) -> None:
        log = self.live / "lineup.log"
        log.write_bytes(b"")
        os.chmod(log, 0o600)
        self.assertEqual(scope.live_drift(self.live, self.plan), ["unexpected file lineup.log"])

    def test_unsafe_modes_and_symlinks(self) -> None:
        gen = self.live / lineup_files.LINEUP_GEN
        os.chmod(gen, 0o644)
        self.assertEqual(
            scope.live_drift(self.live, self.plan), [f"{lineup_files.LINEUP_GEN} mode is not 0600"]
        )
        os.chmod(gen, 0o600)
        skill_dir = self.live / ".claude" / "skills" / "cm"
        os.chmod(skill_dir, 0o755)
        self.assertEqual(
            scope.live_drift(self.live, self.plan),
            # An unsafe directory is not descended into (2.26 behaviour).
            [
                "directory .claude/skills/cm mode is not 0700",
                f"missing file {lineup_files.SKILL_RELPATH}",
            ],
        )
        os.chmod(skill_dir, 0o700)
        lead_set = self.live / lineup_files.LEAD_SET_JSON
        copy_path = self.root / "outside.json"
        copy_path.write_bytes(lead_set.read_bytes())
        os.chmod(copy_path, 0o600)
        lead_set.unlink()
        lead_set.symlink_to(copy_path)
        self.assertEqual(
            scope.live_drift(self.live, self.plan),
            [f"{lineup_files.LEAD_SET_JSON} is not a regular file"],
        )


class ReadDiskPlanTests(_StateRootCase):
    def test_fresh_v2_scope_hashes_equal(self) -> None:
        for kind in bless.V2_SEEDS:
            with self.subTest(kind=kind):
                plan = bless.v2_scope_plan(kind)
                live = self._write(plan)
                disk = scope.read_disk_plan(live, plan)
                self.assertIsNotNone(disk)
                self.assertEqual(disk, plan)
                self.assertEqual(scope.plan_hash(disk), scope.plan_hash(plan))

    def test_tampered_or_missing_other_file_changes_the_hash(self) -> None:
        plan = bless.v2_scope_plan("managed")
        live = self._write(plan)
        skill = live.joinpath(*lineup_files.SKILL_RELPATH.split("/"))
        skill.write_bytes(b"tampered")
        disk = scope.read_disk_plan(live, plan)
        self.assertNotEqual(scope.plan_hash(disk), scope.plan_hash(plan))
        skill.unlink()
        disk = scope.read_disk_plan(live, plan)
        self.assertNotIn(lineup_files.SKILL_RELPATH, disk.other_files)
        self.assertNotEqual(scope.plan_hash(disk), scope.plan_hash(plan))

    def test_unreadable_surfaces_give_none(self) -> None:
        plan = bless.v2_scope_plan("managed")
        live = self._write(plan)
        md = live / lineup_files.LINEUP_MD
        os.chmod(md, 0o644)  # group/other bits: read_private refuses
        self.assertIsNone(scope.read_disk_plan(live, plan))
        os.chmod(md, 0o600)
        outside = self.root / "outside.md"
        outside.write_bytes(md.read_bytes())
        os.chmod(outside, 0o600)
        md.unlink()
        md.symlink_to(outside)
        self.assertIsNone(scope.read_disk_plan(live, plan))
        md.unlink()
        md.mkdir(mode=0o700)
        self.assertIsNone(scope.read_disk_plan(live, plan))


def assert_generation_class(case, bundle, key):
    """Parameterized seam: the client-effort generation instance."""
    entry = bundle.lines[key]
    lcat = _lcat(bundle.docs)
    effort = entry["default_effort"]
    eff = _eff(lcat, workflow_default_binding={"model": key, "effort": effort})
    document = profile.ad_hoc_direct(key)
    document["agents"] = {"cm-reviewer": {"model": key, "effort": effort}}
    lineup = profile.resolve(document, lcat, effective=eff)
    plan = _compile(lineup, lcat, eff, bundle)
    agent = lineup.agents["cm-reviewer"].binding
    lead = lineup.lead.binding
    expected = profile.binding_selector(entry, lcat.providers[entry["provider"]], effort, lead=False)[0]
    case.assertEqual(agent.selector, expected)
    case.assertEqual(dict(_frontmatter(plan.agent_files[".claude/agents/cm-reviewer.md"]))["model"], expected)
    case.assertEqual(plan.settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], expected)
    case.assertIn(expected, plan.settings["availableModels"])
    case.assertIn(lead.selector, plan.settings["availableModels"])
    case.assertTrue(lead.selector.endswith("[1m]"))
    case.assertTrue(agent.selector.endswith("[1m]"))
    case.assertEqual(agent.client_context_tokens, 1000000)
    case.assertEqual(profile.agent_window_problem(expected, entry["context"]["provider_tokens"]), None)
    # A recorded class is kept for the SAME alias only.
    recorded = {"cm-reviewer": {"key": key, "effort": effort, "selector": "gpt-multi-fixture-old-high[1m]"}}
    kept = profile.keep_recorded_agent_class(lineup, recorded, lcat)
    case.assertEqual(kept.agents["cm-reviewer"].binding.selector, expected)


class GenerationClassTests(unittest.TestCase):
    def test_parameterized_agent_workflow_class_and_fence(self) -> None:
        for key, moved in (("sol", True), ("opus", False)):
            with self.subTest(line=key):
                bundle = _bundle()
                entry = bundle.lines[key]
                if moved:
                    entry["generation"] = "99"
                    for level, spec in entry["efforts"].items():
                        spec["selector"] = f"gpt-multi-fixture-next-{level}[1m]"
                assert_generation_class(self, bundle, key)

    @uses_shipped_catalog
    def test_sol_agent_workflow_class_and_fence(self) -> None:
        from _catalog import SHIPPED_ROOT

        assert_generation_class(self, catalog.load_catalog(SHIPPED_ROOT), "sol")

    @uses_shipped_catalog
    def test_sonnet_agent_workflow_class_and_fence(self) -> None:
        """The client-effort Sonnet instance keeps [1m] for
        lead and agents."""
        from _catalog import SHIPPED_ROOT

        bundle = catalog.load_catalog(SHIPPED_ROOT)
        self.assertNotIn("agent_client_tokens", bundle.lines["sonnet"]["context"])
        assert_generation_class(self, bundle, "sonnet")


if __name__ == "__main__":
    unittest.main()
