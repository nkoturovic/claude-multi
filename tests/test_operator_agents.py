"""Operator (T2) agent authority — staged requests, the one
six-condition gate (``profile.agent_eligibility``) in its ``current`` and
``record`` modes, the bounded T2 fence, the in-session relaunch-class
binding, readiness vs authority, the session window, the aggregator
family rule and the doctor gate side (A06, I01).

Fixture-local operator state only (temp HOME/XDG); every contract digest is
injected; no shipped model id is pinned.
"""

from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

import test_cli  # module import: no test classes re-exported
import test_lineup  # module import: no test classes re-exported
import test_operator as operator_fixtures  # module import: no test classes re-exported
from _catalog import FIXTURE_ROOT
from claude_multi import compiler, operator as operator_mod, profile as profile_mod, qualify, scope, sessions
from claude_multi import settings as settings_mod, state, strict_json, transition
from claude_multi.cli import types as cli_types

CONTRACTS = qualify.ContractIdentity("c" * 64, "d" * 64)
OTHER_CONTRACTS = qualify.ContractIdentity("c" * 64, "e" * 64)
AT = "2026-09-30T00:00:00Z"
AGENT_KEY = "custom-acme-agent"
AGENT_SELECTOR = "custom-acme-agent-high"
AGENT_LINE = {"wire_model": "acme-agent-1", "display": "Acme Agent", "efforts": {"high": "output-config-high"},
              "default_effort": "high", "context": {"declared_tokens": 200000, "source": "operator"},
              "capabilities": ["lead", "agents"], "roles": ["cm-reviewer", "cm-analyst"], "family": "unknown"}
VERSIONS = {"launcher": "3.1.0", "client": "2.1.281", "gateway": "7.3.15"}


def agent_document(slot: str = "cm-reviewer", key: str = AGENT_KEY, effort: str = "high", *, lead: str = "opus",
                   name: str = "t2") -> dict:
    document = profile_mod.ad_hoc_direct(lead)
    document["name"] = name
    document["agents"] = {slot: {"model": key, "effort": effort}}
    return document


def record_passes(environ, key: str, digest: str, *, contracts=CONTRACTS, levels=("high",), variant="forced",
                  skip=(), fail=(), extra=()) -> None:
    results = []
    paths = [("smoke",), *(("efforts", level) for level in levels), ("tools", variant), ("stream",), *extra]
    for path in paths:
        if path[0] in skip:
            continue
        outcome = "failed" if path[0] in fail else "pass"
        results.append((path, {"result": outcome, "http": 200 if outcome == "pass" else 400, "at": AT,
                               "reason": "ok" if outcome == "pass" else "http-status",
                               "contracts": contracts.as_document()}))
    operator_mod.record_checks(environ, operator_mod.load_schemas(FIXTURE_ROOT), key, digest=digest,
                               host="api.acme.example", versions=VERSIONS, results=results)


def t1_agent(slot: str = "cm-analyst") -> tuple[str, str]:
    """A fixture catalog line (key, effort) that admits ``slot`` (derived, never pinned)."""

    lines = __import__("claude_multi").catalog.load_catalog(FIXTURE_ROOT).lines
    for key in sorted(lines):
        entry = lines[key]
        roles = entry.get("roles")
        if "agents" in entry["capabilities"] and (roles == "all" or slot in roles) \
                and entry.get("status", "active") == "active":
            return key, profile_mod.declared_efforts(entry)[0]
    raise AssertionError(f"no fixture line admits {slot}")


class OperatorState:
    """Writes fixture-local providers.d, ledger grants and Settings admission."""

    def install(self, runtime, files: dict, *, keys=(), routes=("acme",)) -> operator_mod.OperatorLayer:
        home = {"HOME": str(runtime.home)}
        pdir = operator_mod.providers_dir(home)
        state.ensure_private_dir(pdir)
        for name, document in files.items():
            state.atomic_write(pdir / f"{name}.json", strict_json.pretty_file_bytes(document))
        layer = operator_mod.validate_layer(
            runtime.catalog.docs, {k: strict_json.pretty_file_bytes(v) for k, v in files.items()},
            schemas=operator_mod.load_schemas(FIXTURE_ROOT))
        self.assertEqual([p.text() for p in layer.problems], [])
        document = operator_mod.ledger_document(None)
        document["routes"] = {pid: operator_fixtures._route(layer.providers[pid]) for pid in routes}
        for key in keys:
            line = layer.lines[key]
            document["admissions"][key] = {
                "digest": line.definition_digest, "at": AT, "via": "admit",
                "diagnostic": {"wire": line.core_entry["wire_model"], "fields": layer.metadata[key]["field_hashes"]}}
        state.atomic_write(operator_mod.ledger_path(home), strict_json.canonical_file_bytes(document))
        if keys:
            runtime.settings_store.update(
                lambda doc: doc.__setitem__("admitted_lines", sorted(set(doc.get("admitted_lines", [])) | set(keys))),
                catalog=runtime.lineup_catalog())
        return layer

    @staticmethod
    def acme_files(**line_overrides) -> dict:
        files = {"acme": copy.deepcopy(operator_fixtures._fixture_files()["acme"])}
        line = copy.deepcopy(AGENT_LINE)
        line.update(line_overrides)
        files["acme"]["lines"] = {AGENT_KEY: line}
        return files


class AgentCase(OperatorState, test_cli.CLITestCase):
    def setUp(self) -> None:
        super().setUp()
        state.atomic_write(self.secret_file, b"KIMI_CLAUDE_API_KEY=cli-test-dummy\nACME_API_KEY=acme-dummy-value\n"
                                             b"OPENROUTER_CLAUDE_API_KEY=or-dummy-value\n")
        patcher = mock.patch.object(self.runtime, "contract_identity", return_value=CONTRACTS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = self.runtime.gateway_environ()

    def digest(self, key: str = AGENT_KEY) -> str:
        return self.runtime.operator_snapshot().layer.lines[key].definition_digest

    def prepare(self, document: dict):
        target = cli_types.LaunchTarget("profile", document, document["name"], False, "fixture")
        return self.runtime.prepare(target, action="fresh", passthrough=[])

    def evaluate(self, document: dict, *, gate=None):
        lcat = self.runtime.lineup_catalog()
        if gate is not None:
            lcat = lcat.with_gate(gate)
        return profile_mod.evaluate(document, lcat, bindings={}, effective=self.runtime.current_effective())


# ------------------------------------------------------------ staging
class StagingTests(AgentCase):
    def test_roles_edit_admit_qualify_has_no_digest_deadlock(self) -> None:
        # 1. A lead-only line, admitted.
        files = self.acme_files(capabilities=["lead"], roles=[])
        files["acme"]["lines"][AGENT_KEY].pop("family")
        self.install(self.runtime, files, keys=(AGENT_KEY,))
        lead_only = self.digest()
        self.assertIn(AGENT_KEY, self.runtime.current_effective().admitted_lines)
        # 2. Request agents and roles: the digest moves, admission lapses,
        # but the line still loads and keeps its render aliases (inert request).
        requested = self.acme_files()
        home = {"HOME": str(self.runtime.home)}
        state.atomic_write(operator_mod.providers_dir(home) / "acme.json", strict_json.pretty_file_bytes(requested["acme"]))
        layer = self.runtime.operator_snapshot().layer
        self.assertIn(AGENT_KEY, layer.lines)
        self.assertNotEqual(self.digest(), lead_only)
        self.assertEqual(operator_mod.line_aliases(layer.lines[AGENT_KEY].core_entry), (AGENT_SELECTOR,))
        self.assertNotIn(AGENT_KEY, self.runtime.current_effective().admitted_lines)
        # Qualification and admission are independent optional attestations.
        record_passes(self.env, AGENT_KEY, self.digest())
        evaluation = self.evaluate(agent_document())
        self.assertEqual(evaluation.errors, ())
        self.assertIn("admission", {w.code for w in evaluation.lineup.warnings})
        # 4. Re-admit the changed definition: eligible, no deadlock.
        self.install(self.runtime, requested, keys=(AGENT_KEY,))
        evaluation = self.evaluate(agent_document())
        self.assertEqual(evaluation.errors, ())
        self.assertEqual(evaluation.lineup.agents["cm-reviewer"].binding.selector, AGENT_SELECTOR)
        # Qualification never edited the declaration or the ledger.
        self.assertEqual(self.digest(), self.runtime.operator_snapshot().layer.lines[AGENT_KEY].definition_digest)


# ------------------------------------------------------------ the gate (authority, window and qualification)
class GateTests(unittest.TestCase):
    ENTRY = {"capabilities": ["lead", "agents"], "roles": ["cm-reviewer"], "default_effort": "high",
             "efforts": {"high": {"selector": "custom-x-high", "proxy_contract": "c"}},
             "lead": {"effort": "high", "env": {}},
             "context": {"client_tokens": 200000, "provider_tokens": 200000}}
    FACTS = profile_mod.AgentFacts(key="custom-x", provider="acme", admitted=True, route="approved", d60=True,
                                   route_kind="", family="unknown", t1_families=frozenset({"anthropic", "openai"}),
                                   evidence="current", tools_variants=frozenset({"forced"}))
    EFFORTS = ("low", "medium", "high", "xhigh", "max")

    _DEFAULT = object()

    def verdict(self, entry=None, facts=_DEFAULT, **kw):
        kw.setdefault("slot", "cm-reviewer")
        kw.setdefault("effort", "high")
        return profile_mod.agent_eligibility(entry or self.ENTRY, key="custom-x",
                                             facts=self.FACTS if facts is self._DEFAULT else facts,
                                             agent_efforts=self.EFFORTS, **kw)

    def test_recommendations_and_evidence_warn_but_route_and_effort_stay_hard(self) -> None:
        self.assertTrue(self.verdict().eligible)
        replace = __import__("dataclasses").replace
        entry_cases = {
            "capability-recommendation": dict(self.ENTRY, capabilities=["lead"]),
            "role-recommendation": dict(self.ENTRY, roles=["cm-explorer"]),
            "lead-env-ignored": dict(self.ENTRY, lead={"effort": "high", "env": {"X": "1"}}),
            "context-risk": dict(self.ENTRY, context={"client_tokens": 200000, "provider_tokens": 131072}),
        }
        for code, entry in entry_cases.items():
            with self.subTest(code=code):
                verdict = self.verdict(entry=entry)
                self.assertTrue(verdict.eligible, verdict.reasons)
                self.assertIn(code, {w.code for w in verdict.warnings})
        fact_cases = (
            ("admission", "not admitted", replace(self.FACTS, admitted=False)),
            ("family-unknown", "independence unknown", replace(self.FACTS, family="acme")),
            ("qualification", "no current tools evidence", replace(self.FACTS, evidence="missing", evidence_gaps=("tools",))),
            ("qualification", "failed stream", replace(self.FACTS, evidence="failed", evidence_gaps=("stream",))),
            ("qualification", "stale for the current definition", replace(self.FACTS, evidence="definition-stale")),
            ("exact-client", "unavailable", replace(self.FACTS, pool=True, exact_client="unavailable")),
            ("exact-client", "missing", replace(self.FACTS, pool=True, exact_client="missing")),
            ("exact-client", "failed", replace(self.FACTS, pool=True, exact_client="failed")),
        )
        for code, text, facts in fact_cases:
            with self.subTest(code=code, text=text):
                verdict = self.verdict(facts=facts)
                self.assertTrue(verdict.eligible, verdict.reasons)
                self.assertEqual(verdict.evidence, facts.evidence)
                self.assertTrue(any(w.code == code and text in w.message for w in verdict.warnings))
        lan = self.verdict(facts=replace(self.FACTS, d60=False, route="keyless", route_kind="openai-compatible-lan"))
        self.assertTrue(lan.eligible)
        self.assertFalse(lan.reasons)
        for route in ("changed", "unapproved"):
            verdict = self.verdict(facts=replace(self.FACTS, route=route, admitted=False))
            self.assertFalse(verdict.eligible)
            self.assertIn(route, verdict.reasons[0])
            self.assertEqual(verdict.remedy, "claude-multi providers approve acme")
        for effort in ("ultracode", "max", "impossible"):
            self.assertFalse(self.verdict(effort=effort).eligible)
        missing = self.verdict(facts=None)
        self.assertTrue(missing.eligible)
        self.assertEqual(missing.evidence, "missing")
        self.assertTrue(any("not qualified" in w.message for w in missing.warnings))

    def test_contract_stale_is_eligible_with_attention_and_workflow_needs_forced(self) -> None:
        replace = __import__("dataclasses").replace
        verdict = self.verdict(facts=replace(self.FACTS, evidence="contract-stale"))
        self.assertTrue(verdict.eligible)
        self.assertTrue(verdict.attention)
        self.assertTrue(any(w.code == "qualification" and "predates the current pins" in w.message
                            for w in verdict.warnings))
        auto_only = replace(self.FACTS, tools_variants=frozenset({"auto"}))
        self.assertTrue(self.verdict(facts=auto_only).eligible)  # an agent role: auto suffices
        workflow = self.verdict(facts=auto_only, slot=None, use=profile_mod.WORKFLOW_USE)
        self.assertTrue(workflow.eligible, workflow.reasons)
        self.assertIn("tool-evidence", {w.code for w in workflow.warnings})

    def test_record_mode_keeps_a_proven_grant_and_never_grants_a_changed_slot(self) -> None:
        replace = __import__("dataclasses").replace
        stale = replace(self.FACTS, evidence="missing", admitted=False)
        kept = self.verdict(facts=stale, mode=profile_mod.AGENT_MODE_RECORD, recorded=True)
        self.assertTrue(kept.eligible)
        self.assertEqual(kept.warnings, self.verdict(facts=stale).warnings)
        self.assertTrue(self.verdict(facts=stale, mode=profile_mod.AGENT_MODE_RECORD, recorded=False).eligible)
        with self.assertRaises(ValueError):
            self.verdict(mode="whatever")

    def test_agent_compaction_helper_matches_the_compiler_rule(self) -> None:
        for window in (200000, 262144, 800000, 1000000):
            for percent in (60, 90, 95):
                self.assertEqual(profile_mod.agent_compaction_trigger(window, percent),
                                 compiler.reactive_trigger(window, percent))
        self.assertEqual(profile_mod.agent_class_window("x[1m]"), 1000000)
        self.assertEqual(profile_mod.agent_class_window("x"), 200000)
        self.assertEqual(profile_mod.agent_class_window("x", 150000), 150000)
        self.assertEqual(profile_mod.agent_class_window("x[1m]", ceiling=800000), 800000)
        self.assertIsNone(profile_mod.agent_window_problem("x", 167000))
        self.assertIsNotNone(profile_mod.agent_window_problem("x", 166999))
        # A 1M-class line runs at the session window when its bound is at
        # least the window, and in the 200K class below it.
        self.assertIsNone(profile_mod.agent_window_problem("x[1m]", 872000))
        self.assertIsNone(profile_mod.agent_window_problem("x[1m]", 500000))
        self.assertIsNotNone(profile_mod.agent_window_problem("x[1m]", 166999))
        self.assertIsNone(profile_mod.agent_window_problem("x[1m]", 1000000))


class GateWiringTests(AgentCase):
    def setUp(self) -> None:
        super().setUp()
        self.install(self.runtime, self.acme_files(), keys=(AGENT_KEY,))

    @test_lineup._fixed_id()
    def test_unqualified_launch_keeps_exact_selector_in_fence(self) -> None:
        prepared = self.prepare(agent_document())
        self.assertTrue(any(w.code == "qualification" and "not qualified" in w.message
                            for w in prepared.lineup.warnings))
        self.assertEqual(self.launches, [])
        self.assertIn(AGENT_SELECTOR, prepared.result.fence.agent_set)
        self.assertIn(AGENT_SELECTOR, prepared.result.scope_plan.settings["availableModels"])
        self.assertEqual(prepared.lineup.agents["cm-reviewer"].binding.family, "unknown")
        before = prepared.result.scope_plan
        record_passes(self.env, AGENT_KEY, self.digest())
        after = self.prepare(agent_document()).result.scope_plan
        self.assertEqual(before.agent_files, after.agent_files)
        self.assertEqual(before.settings, after.settings)
        self.assertEqual(before.other_files, after.other_files)

    def test_contract_stale_is_attention_definition_change_fails_closed(self) -> None:
        record_passes(self.env, AGENT_KEY, self.digest(), contracts=OTHER_CONTRACTS)
        prepared = self.prepare(agent_document())
        warnings = {finding.code: finding.message for finding in prepared.lineup.warnings}
        self.assertIn("predates the current pins", warnings["qualification"])
        # A definition change invalidates evidence, not the usable route.
        files = self.acme_files()
        files["acme"]["lines"][AGENT_KEY]["context"]["declared_tokens"] = 190000
        self.install(self.runtime, files, keys=(AGENT_KEY,))
        prepared = self.prepare(agent_document())
        self.assertTrue(any(w.code == "qualification" and "stale for the current definition" in w.message
                            for w in prepared.lineup.warnings))

    def test_workflow_default_on_an_operator_line_needs_the_forced_variant(self) -> None:
        record_passes(self.env, AGENT_KEY, self.digest(), variant="auto")
        self.runtime.settings_store.update(
            lambda doc: doc.__setitem__("workflow_default_binding", {"model": AGENT_KEY, "effort": "high"}),
            catalog=self.runtime.lineup_catalog())
        prepared = self.prepare(agent_document("cm-analyst", *t1_agent()))
        self.assertTrue(any("forced named-tool" in notice for notice in prepared.notices), prepared.notices)
        before = prepared.result.scope_plan
        record_passes(self.env, AGENT_KEY, self.digest(), variant="forced")
        prepared = self.prepare(agent_document("cm-analyst", *t1_agent()))
        self.assertIn(AGENT_SELECTOR, prepared.result.scope_plan.settings["availableModels"])
        self.assertEqual(prepared.result.scope_plan.settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], AGENT_SELECTOR)
        self.assertEqual(before.other_files, prepared.result.scope_plan.other_files)

    def test_picker_facts_come_from_the_gate(self) -> None:
        from claude_multi import views

        def item():
            lcat = self.runtime.lineup_catalog()
            eff = self.runtime.current_effective()
            rows = views.line_rows(lcat, eff, custom_ids=frozenset())
            picker = views.picker_rows(rows, slot="cm-reviewer", bindings={}, lcat=lcat, eff=eff, current=None)
            return next(entry for entry in picker.items if entry.kind == "line" and entry.key == AGENT_KEY)

        unqualified = item()
        self.assertTrue(unqualified.selectable)
        record_passes(self.env, AGENT_KEY, self.digest())
        self.assertTrue(item().selectable)

    def test_in_session_relaunch_reason_names_the_operator_binding(self) -> None:
        self.assertIn("operator agent binding is relaunch-class", test_lineup.lineup.REASON_T2_AGENT)


class T2FamilyTests(AgentCase):
    """B check (MAJOR): condition 4's known families come from the unmerged
    trusted catalog only — a qualified, admitted T2 provider never certifies
    its own independence family through the merged docs."""

    def test_a_t2_provider_family_is_not_catalog_declared(self) -> None:
        files = self.acme_files()
        files["acme"]["lines"][AGENT_KEY].pop("family")  # inherits the provider's "acme"
        layer = self.install(self.runtime, files, keys=(AGENT_KEY,))
        self.assertEqual(layer.lines[AGENT_KEY].family, "acme")
        record_passes(self.env, AGENT_KEY, self.digest())
        trusted = operator_mod.t1_families(self.runtime.catalog.docs)
        self.assertNotIn("acme", trusted)
        facts = self.runtime.lineup_catalog().agent_gate.facts[AGENT_KEY]
        self.assertEqual(facts.t1_families, trusted)
        evaluation = self.evaluate(agent_document())
        self.assertEqual(evaluation.errors, ())
        self.assertTrue(any(w.code == "family-unknown" for w in evaluation.lineup.warnings))
        self.assertNotIn("acme", self.runtime.lineup_catalog().known_families)
        self.assertTrue(self.prepare(agent_document()).lineup.routing.independence_unknown)
        # Declared unknown: eligible (never independent); T1-family declarations
        # on an aggregator stay eligible (AggregatorFamilyTests).
        self.install(self.runtime, self.acme_files(), keys=(AGENT_KEY,))
        record_passes(self.env, AGENT_KEY, self.digest())
        self.assertEqual(self.evaluate(agent_document()).errors, ())


# ------------------------------------------------------------ fence
class FenceTests(AgentCase):
    def test_unbound_operator_qualification_does_not_change_scope(self) -> None:
        self.install(self.runtime, self.acme_files(), keys=(AGENT_KEY,))
        document = agent_document("cm-analyst", *t1_agent())
        before = self.prepare(document).result.scope_plan.settings["availableModels"]
        record_passes(self.env, AGENT_KEY, self.digest())
        after = self.prepare(document).result.scope_plan.settings["availableModels"]
        self.assertEqual(before, after)
        self.assertNotIn(AGENT_SELECTOR, after)
        lcat = self.runtime.lineup_catalog()
        self.assertNotIn(AGENT_SELECTOR, scope.agent_set(lcat, self.runtime.current_effective()))
        # T1 keeps every offered agents-capable selector.
        t1 = {selector for key, entry in lcat.lines.items() if lcat.origin(key) == "catalog"
              and "agents" in entry["capabilities"]
              for _e, selector, _c in __import__("claude_multi").catalog.line_selectors(entry)}
        self.assertLessEqual(t1 & set(after), set(after))
        bound = self.prepare(agent_document()).result.scope_plan.settings["availableModels"]
        self.assertEqual(set(bound) - set(after), {AGENT_SELECTOR})


class PermissiveDeclarationsTests(AgentCase):
    def test_unadmitted_lead_only_recommendations_allow_every_agent_grade(self) -> None:
        fixtures = operator_fixtures._fixture_files()
        messages = self.acme_files(capabilities=["lead"], roles=[], family="Mistral")
        keyed = copy.deepcopy(messages)
        keyed["acme"]["provider"]["kind"] = "openai-compatible"
        keyed["acme"]["provider"].pop("payload_contracts", None)
        keyed["acme"]["lines"][AGENT_KEY]["efforts"] = ["high"]
        lan = copy.deepcopy(fixtures["lanbox"])
        lan["lines"]["custom-lan-model"]["context"]["declared_tokens"] = 128000
        router = {"version": 1, "lines": {"custom-router-free": {
            "wire_model": "mistral/fixture-model", "display": "Router fixture", "family": "Mistral",
            "efforts": ["high"], "default_effort": "high",
            "context": {"declared_tokens": 200000, "source": "operator"}}}}
        cases = ((messages, AGENT_KEY, ("acme",)), (keyed, AGENT_KEY, ("acme",)),
                 ({"lanbox": lan}, "custom-lan-model", ()),
                 ({"kimi": fixtures["kimi"]}, "custom-kimi-next", ()),
                 ({"anthropic": fixtures["anthropic"]}, "custom-claude-fixture", ()),
                 ({"openrouter": router}, "custom-router-free", ()))
        with mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("automatic smoke")), \
                mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("automatic qualification")):
            for files, key, routes in cases:
                with self.subTest(key=key, kind=files.get("acme", {}).get("provider", {}).get("kind")):
                    self.install(self.runtime, files, routes=routes)
                    entry = self.runtime.lineup_catalog().lines[key]
                    document = agent_document(key=key, effort=entry["default_effort"], lead=key)
                    document["agents"] = {rid: {"model": key, "effort": entry["default_effort"]}
                                          for rid in profile_mod.AGENT_ROLE_IDS}
                    prepared = self.prepare(document)
                    self.assertNotIn(key, self.runtime.current_effective().admitted_lines)
                    self.assertEqual(set(prepared.lineup.agents), set(profile_mod.AGENT_ROLE_IDS))
                    codes = {w.code for w in prepared.lineup.warnings}
                    self.assertTrue({"admission", "qualification", "capability-recommendation", "role-recommendation"}
                                    <= codes, codes)
                    self.assertTrue(any(w.slot == profile_mod.LEAD_ROLE and w.code == "qualification"
                                        for w in prepared.lineup.warnings))
                    for rid, agent in prepared.lineup.agents.items():
                        self.assertIn(agent.binding.selector, prepared.result.scope_plan.settings["availableModels"])
                        body = prepared.result.scope_plan.agent_files[f".claude/agents/{rid}.md"].decode()
                        self.assertIn(f"model: {agent.binding.selector}\n", body)

    def test_keyless_lan_agent_keeps_the_same_rendered_route(self) -> None:
        from claude_multi import render

        lan = copy.deepcopy(operator_fixtures._fixture_files()["lanbox"])
        lan["lines"]["custom-lan-model"]["context"]["declared_tokens"] = 128000
        self.install(self.runtime, {"lanbox": lan}, routes=())
        prepared = self.prepare(agent_document(key="custom-lan-model"))
        agent = prepared.lineup.agents["cm-reviewer"].binding
        self.assertEqual(agent.selector, "custom-lan-model")
        self.assertEqual(agent.client_context_tokens, 200000)
        self.assertIn("context-risk", {w.code for w in prepared.lineup.warnings})
        snapshot = self.runtime.operator_snapshot()
        plan = operator_mod.render_plan(self.runtime.catalog.docs, snapshot.layer, snapshot.ledger)
        config, _available, _unavailable, _info = render.build_config_document(
            plan.docs["gateway"], plan.docs["providers"]["providers"], plan.docs["models"]["models"],
            home=self.runtime.home, gateway_token="fixture", resolve_secret=lambda _ref: None,
            continuity={}, captures=plan.captures, provider_headers=plan.headers, oauth_overlay=plan.overlay)
        route = next(row for row in config["openai-compatibility"] if row["name"] == "lanbox")
        self.assertEqual(route["base-url"], "http://box.lan:8010/v1")
        self.assertNotIn("api-key-entries", route)
        self.assertEqual([(row["name"], row["alias"], row["force-mapping"]) for row in route["models"]],
                         [("lan-model", agent.selector, True)])
        self.assertEqual(snapshot.layer.lines["custom-lan-model"].core_entry["capabilities"], ["lead"])


# ------------------------------------------------------------ context class of an operator agent
class OperatorAgentWindowTests(AgentCase):
    """A custom-endpoint (operator) agent line declared above 200K is a
    1M-class line: below the session window it runs in the 200K class (the
    same alias without ``[1m]``), at or above it in the 1M class at the
    window. Its fence entry is the selector it runs on."""

    def _prepared(self, declared: int):
        files = self.acme_files(context={"declared_tokens": declared, "source": "operator"})
        self.install(self.runtime, files, keys=(AGENT_KEY,))
        record_passes(self.env, AGENT_KEY, self.digest())
        entry = self.runtime.lineup_catalog().lines[AGENT_KEY]
        self.assertEqual(entry["efforts"]["high"]["selector"], AGENT_SELECTOR + "[1m]")
        return self.prepare(agent_document())

    def test_a_line_below_the_window_runs_in_the_200k_class(self) -> None:
        prepared = self._prepared(500_000)
        binding = prepared.lineup.agents["cm-reviewer"].binding
        self.assertEqual((binding.selector, binding.provider_context_tokens), (AGENT_SELECTOR, 500_000))
        settings_doc = prepared.result.scope_plan.settings
        self.assertIn(AGENT_SELECTOR, settings_doc["availableModels"])
        self.assertNotIn(AGENT_SELECTOR + "[1m]", settings_doc["availableModels"])
        role = dict(profile_mod.lineup_windows(prepared.lineup))["cm-reviewer"]
        self.assertEqual((role.client_class, role.window), (200_000, 200_000))
        self.assertLess(role.trigger, 500_000)
        self.assertEqual(prepared.result.env_set["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "800000")

    def test_a_line_at_the_window_runs_in_the_1m_class(self) -> None:
        prepared = self._prepared(1_000_000)
        binding = prepared.lineup.agents["cm-reviewer"].binding
        self.assertEqual(binding.selector, AGENT_SELECTOR + "[1m]")
        self.assertIn(binding.selector, prepared.result.scope_plan.settings["availableModels"])
        role = dict(profile_mod.lineup_windows(prepared.lineup))["cm-reviewer"]
        self.assertEqual(role.window, 800_000)


# ------------------------------------------------------------ record authority
class RecordAuthorityTests(AgentCase):
    def setUp(self) -> None:
        super().setUp()
        self.install(self.runtime, self.acme_files(), keys=(AGENT_KEY,))

    def _record(self):
        return {"applied": {"agents": {"cm-reviewer": {"key": AGENT_KEY, "effort": "high"}}}}

    def test_record_authority_keeps_a_recorded_binding_and_never_grants_a_new_one(self) -> None:
        eff = self.runtime.current_effective()
        gate = transition.record_agent_gate(self._record(), eff, self.runtime.lineup_catalog().agent_gate.facts)
        # Missing evidence is a warning in both modes, never mutable authority.
        kept = self.evaluate(agent_document(), gate=gate)
        self.assertEqual(kept.errors, ())
        self.assertIn("qualification", {f.code for f in kept.lineup.warnings})
        self.assertEqual(self.evaluate(agent_document()).errors, ())
        self.assertEqual(self.evaluate(agent_document("cm-analyst"), gate=gate).errors, ())
        empty = transition.record_agent_gate(self._record(), eff, {})
        self.assertEqual(self.evaluate(agent_document("cm-analyst"), gate=empty).errors, ())
        self.assertTrue(self.evaluate(agent_document(effort="max"), gate=empty).errors)


class InSessionTests(OperatorState, test_lineup.LineupCase):
    """An in-session T2 agent binding is relaunch-class and uses the
    current gate (a line admitted before launch, and one admitted after)."""

    launch_profile = None

    def setUp(self) -> None:
        super().setUp()
        state.atomic_write(self.root / "secrets" / "claude.env",
                           b"KIMI_CLAUDE_API_KEY=v4-test-dummy\nQWEN_CLAUDE_API_KEY=v4-test-dummy\n"
                           b"ACME_API_KEY=acme-dummy\n")
        patcher = mock.patch.object(self.runtime, "contract_identity", return_value=CONTRACTS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _launch(self):
        with test_lineup._fixed_id():
            self.record = self.launch_fresh(self.profile_target("balanced"))
        self.mid, self.rid = self.record["managed_id"], self.record["runtime_session_id"]

    def _qualify(self):
        layer = self.runtime.operator_snapshot().layer
        record_passes(self.runtime.gateway_environ(), AGENT_KEY, layer.lines[AGENT_KEY].definition_digest)

    def _scenario(self, admitted_before_launch: bool) -> None:
        if admitted_before_launch:
            self.install(self.runtime, self.acme_files(), keys=(AGENT_KEY,))
            self._qualify()
            self._launch()
        else:
            self._launch()
            self.install(self.runtime, self.acme_files(), keys=(AGENT_KEY,))
            self._qualify()
        evidence = operator_mod.evidence_path(self.runtime.gateway_environ())
        saved = evidence.read_bytes()
        code, out, err = self.request(f"set reviewer={AGENT_KEY}:high")
        self.assertIn("reviewer → custom-acme-agent-high: an operator agent binding is relaunch-class "
                      "(the model fence is fixed at launch); it applies at the next resume", out + err)
        pending = self.store.load(self.mid)["pending"]
        self.assertEqual(pending["document"]["agents"]["cm-reviewer"]["model"], AGENT_KEY)
        # Missing optional evidence never drops the pending binding.
        state.remove_private(evidence)
        prepared = self.prepare_resume(self.mid)
        self.assertFalse(any("pending relaunch change dropped" in n for n in prepared.notices), prepared.notices)
        self.assertTrue(any("not qualified" in n for n in prepared.notices), prepared.notices)
        # With current evidence, resume applies it and the fence carries it.
        state.atomic_write(evidence, saved)
        record = self.resume(self.mid)
        self.assertEqual(record["applied"]["agents"]["cm-reviewer"]["selector"], AGENT_SELECTOR)
        settings = strict_json.loads((self.live(self.mid) / "settings.json").read_bytes())
        self.assertIn(AGENT_SELECTOR, settings["availableModels"])
        # An already-fenced selector can be assigned live without evidence.
        state.remove_private(evidence)
        code, out, err = self.request(f"set analyst={AGENT_KEY}:high")
        self.assertIn("/reload-plugins", out + err)
        self.assertIn("spawn it fresh", out + err)
        self.assertEqual(self.store.load(self.mid)["applied"]["agents"]["cm-analyst"]["key"], AGENT_KEY)
        # The running reviewer is a recorded grant: record authority keeps
        # it with no evidence at all (doctor/converge never revoke).
        plan = transition.expected_plan(
            self.store.load(self.mid), docs=self.runtime.ordinary_docs,
            prompt_bodies=self.runtime.catalog.prompt_bodies, state_root=self.store.root,
            hook_command=self.runtime.hook3_command, token_helper_command=self.runtime.token_helper_command,
            live=self.live(self.mid), environ={**self.runtime.environ, "HOME": str(self.runtime.home)})
        self.assertIsNotNone(plan.plan, plan.reason)
        self.assertIn(AGENT_SELECTOR, plan.plan.settings["availableModels"])

    def test_in_session_t2_binding_is_relaunch_and_uses_current_gate(self) -> None:
        self._scenario(admitted_before_launch=True)

    def test_in_session_t2_binding_admitted_after_launch_uses_current_gate(self) -> None:
        self._scenario(admitted_before_launch=False)

    def test_the_same_t2_selector_in_another_slot_is_relaunch_class(self) -> None:
        """A T2 selector already in the launch fence (bound
        as reviewer) is still relaunch-class when assigned to a new slot."""

        self.install(self.runtime, self.acme_files(), keys=(AGENT_KEY,))
        self._qualify()
        self._launch()
        code, out, err = self.request(f"set reviewer={AGENT_KEY}:high")
        self.assertIn("an operator agent binding is relaunch-class", out + err)
        record = self.resume(self.mid)
        self.assertEqual(record["applied"]["agents"]["cm-reviewer"]["selector"], AGENT_SELECTOR)
        generation = record["lineup_generation"]
        code, out, err = self.request(f"set analyst={AGENT_KEY}:high")
        self.assertIn("/reload-plugins", out + err)
        self.assertNotIn("reviewer → custom-acme-agent-high", out + err)  # the unchanged slot stays
        after = self.store.load(self.mid)
        self.assertEqual(after["lineup_generation"], generation + 1)
        self.assertEqual(after["applied"]["agents"]["cm-analyst"]["key"], AGENT_KEY)
        self.assertIsNone(after.get("pending"))


# ------------------------------------------------------------ readiness vs authority, effective transport
class TransportAuthorityTests(AgentCase):
    def test_d28_uses_the_selected_transport_of_every_bound_slot(self) -> None:
        alternative = operator_mod.TRANSPORT_ALTERNATIVES[("anthropic", "api-key")]
        home = {"HOME": str(self.runtime.home)}
        document = operator_mod.ledger_document(None)
        document["transport_choices"] = {"anthropic": "api-key"}
        document["routes"] = {"anthropic": operator_mod.transport_route_record(alternative, at=AT)}
        state.atomic_write(operator_mod.ledger_path(home), strict_json.canonical_file_bytes(document))
        lineup = profile_mod.resolve(profile_mod.ad_hoc_direct("opus"), self.runtime.lineup_catalog(),
                                     effective=self.runtime.current_effective(), ad_hoc=True)
        problems = self.runtime.lineup_secret_problems(lineup)
        self.assertTrue(any("PLATFORM_ANTHROPIC_API_KEY" in problem for problem in problems), problems)
        document["routes"] = {}
        state.atomic_write(operator_mod.ledger_path(home), strict_json.canonical_file_bytes(document))
        problems = self.runtime.lineup_secret_problems(lineup)
        self.assertTrue(any("not approved" in problem for problem in problems), problems)


# ------------------------------------------------------------ aggregator family
class AggregatorFamilyTests(AgentCase):
    LINES = {
        "custom-or-anthro": {"wire_model": "anthropic/fixture-model-9", "display": "OR anthropic",
                             "efforts": ["high"], "default_effort": "high", "family": "anthropic",
                             "context": {"declared_tokens": 200000, "source": "operator"},
                             "capabilities": ["lead", "agents"], "roles": ["cm-reviewer", "cm-reviewer-strong"]},
        "custom-or-mystery": {"wire_model": "mystery/fixture-model-2", "display": "OR mystery",
                              "efforts": ["high"], "default_effort": "high",
                              "context": {"declared_tokens": 200000, "source": "operator"},
                              "capabilities": ["lead", "agents"], "roles": ["cm-reviewer", "cm-reviewer-strong"]},
    }

    def setUp(self) -> None:
        super().setUp()
        files = {"openrouter": {"version": 1, "lines": copy.deepcopy(self.LINES)}}
        self.install(self.runtime, files, keys=tuple(self.LINES), routes=())
        for key in self.LINES:
            record_passes(self.env, key, self.digest(key))

    def test_declared_family_reaches_routing_and_unknown_is_never_independent(self) -> None:
        layer = self.runtime.operator_snapshot().layer
        self.assertEqual(layer.lines["custom-or-anthro"].family, "anthropic")
        self.assertEqual(layer.lines["custom-or-mystery"].family, "unknown")
        lcat = self.runtime.lineup_catalog()
        self.assertEqual(lcat.operator["custom-or-anthro"]["family"], "anthropic")
        # A T1-family line on the aggregator: the anthropic lead's review is same-family.
        document = agent_document("cm-reviewer", "custom-or-anthro", lead="opus")
        lineup = self.evaluate(document).lineup
        self.assertEqual(lineup.agents["cm-reviewer"].binding.family, "anthropic")
        lead_row = next(row for row in lineup.routing.rows if row.author == "cm-lead")
        self.assertTrue(lead_row.normal.same_family)
        # An unknown line never counts as independent, even beside a known family.
        document = agent_document("cm-reviewer", "custom-or-mystery", lead="opus")
        lineup = self.evaluate(document).lineup
        self.assertEqual(lineup.agents["cm-reviewer"].binding.family, "unknown")
        lead_row = next(row for row in lineup.routing.rows if row.author == "cm-lead")
        self.assertFalse(lead_row.normal.same_family)
        self.assertTrue(lead_row.normal.independence_unknown)
        self.assertIn("independence-unknown", {f.code for f in lineup.warnings})
        # An aggregator's own family is unknown: no family is ever assumed for
        # an undeclared line (its catalog lines declare theirs).
        self.assertEqual(self.runtime.catalog.docs["providers"]["providers"]["openrouter"]["independence_family"],
                         "unknown")

    def test_an_undeclared_aggregator_family_refuses_to_load(self) -> None:
        files = {"openrouter": {"version": 1, "lines": {"custom-or-bad": dict(self.LINES["custom-or-anthro"],
                                                                              family="madeup")}}}
        layer = operator_mod.validate_layer(
            self.runtime.catalog.docs, {k: strict_json.pretty_file_bytes(v) for k, v in files.items()},
            schemas=operator_mod.load_schemas(FIXTURE_ROOT))
        self.assertIn("custom-or-bad", layer.lines)
        self.assertFalse(layer.problems)
        self.assertEqual(layer.lines["custom-or-bad"].family, "madeup")


# ------------------------------------------------------------ doctor gate side (A06, I01)
class DoctorGateTests(AgentCase):
    def test_i01_counts_eligible_lines_and_a06_names_contract_stale_live_bindings(self) -> None:
        self.install(self.runtime, self.acme_files(), keys=(AGENT_KEY,))
        snapshot = self.runtime.operator_snapshot()
        attention, eligible = self.runtime.operator_agent_findings(snapshot, frozenset())
        self.assertEqual(eligible, 1)
        self.assertTrue(any("not qualified" in item for item in attention), attention)
        record_passes(self.env, AGENT_KEY, self.digest(), contracts=OTHER_CONTRACTS)
        attention, eligible = self.runtime.operator_agent_findings(snapshot, frozenset())
        self.assertEqual(eligible, 1)
        self.assertTrue(any("predates the current pins" in item for item in attention), attention)
        with mock.patch.object(self.runtime.session_store, "scan_uuid_records",
                               return_value=[__import__("pathlib").Path("m" * 8 + ".json")]), \
                mock.patch.object(self.runtime.session_store, "load_raw", return_value=(
                    {"version": sessions.RECORD_VERSION,
                     "applied": {"agents": {"cm-reviewer": {"key": AGENT_KEY, "effort": "high"}}}}, b"")):
            attention, _ = self.runtime.operator_agent_findings(snapshot, frozenset({"m" * 8}))
        self.assertTrue(any("predates the current pins" in item for item in attention), attention)
        findings = operator_mod.doctor_findings(self.runtime.catalog.docs, snapshot, plan=None,
                                                admitted=(AGENT_KEY,), refs={}, live=frozenset(), agent_eligible=1)
        self.assertTrue(any("1 usable for agents" in line for line in findings.info), findings.info)


if __name__ == "__main__":
    unittest.main()
