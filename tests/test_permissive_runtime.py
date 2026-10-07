"""Optional model attestations never authorize routes or mutate compiled scopes."""

from __future__ import annotations

import copy
import dataclasses
import json
import unittest
from unittest import mock

from claude_multi import launch, profile, scope, settings, state, strict_json
from claude_multi.cli import doctor, runtime, types
import test_cli as fixtures
import test_scope_v2 as scope_fixtures


class PermissiveRuntimeTests(fixtures.OperatorCommandCase):
    def setUp(self):
        super().setUp()
        self.declare_small()
        self.key = "custom-acme-small"
        self.no_smoke = mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("automatic smoke"))
        self.no_qualify = mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("automatic qualify"))
        self.no_smoke.start()
        self.no_qualify.start()
        self.addCleanup(self.no_smoke.stop)
        self.addCleanup(self.no_qualify.stop)

    def prepare(self, *, lead="opus", agent=True, workflow=False):
        document = profile.ad_hoc_direct(lead, "high")
        if agent:
            document["agents"] = {"cm-reviewer": {"model": self.key, "effort": "high"}}
        if workflow:
            self.runtime.settings_store.update(
                lambda doc: doc.update(workflow_default_binding={"model": self.key, "effort": "high"}),
                catalog=self.runtime.lineup_catalog())
        target = types.LaunchTarget("ad-hoc", document, None, False, "fixture")
        return self.runtime.prepare(target, action="fresh", passthrough=[])

    def change_definition(self):
        path = self.pdir / "acme.json"
        document = json.loads(path.read_text())
        document["lines"][self.key]["context"]["declared_tokens"] = 120000
        state.atomic_write(path, strict_json.pretty_file_bytes(document))

    def test_unadmitted_unqualified_lead_and_agent_launch_with_warnings(self):
        for lead, agent in ((self.key, False), ("opus", True)):
            with self.subTest(lead=lead, agent=agent):
                prepared = self.prepare(lead=lead, agent=agent)
                self.assertFalse(self.runtime.current_effective().admitted_lines)
                codes = {finding.code for finding in prepared.lineup.warnings}
                self.assertTrue({"admission", "qualification", "family-unknown"} <= codes)
                self.assertIn("not qualified", "\n".join(prepared.notices))
                self.runtime.perform(prepared)
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.http_calls, [])

    def test_workflow_default_is_fenced_warns_and_its_definition_revalidates(self):
        prepared = self.prepare(agent=False, workflow=True)
        selected = prepared.result.scope_plan.settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"]
        self.assertIn(selected, prepared.result.fence.available_models)
        self.assertNotIn(selected, prepared.result.fence.lead_selectors)
        self.assertIn("workflow default:", "\n".join(prepared.notices))
        self.assertIn("forced named-tool", "\n".join(prepared.notices))
        self.assertIn(self.key, runtime.prepared_line_keys(prepared, self.runtime.lineup_catalog()))
        self.change_definition()
        for check in (self.runtime.revalidate_operator, self.runtime._revalidate_in_barrier):
            with self.subTest(check=check.__name__), self.assertRaisesRegex(launch.LaunchError, "changed"):
                check(prepared)
        self.assertEqual(self.launches, [])

    def test_badge_changes_do_not_change_any_scope_bytes_or_revalidation(self):
        with mock.patch.object(type(self.runtime.session_store), "new_id", return_value=fixtures.FIXED_ID):
            before = self.prepare(workflow=True)
            code, out, err = self.op(["models", "admit", self.key], "y\n")
            self.assertEqual(code, 0, out + err)
            admitted = self.prepare(workflow=True)
            code, out, err = self.op(["models", "revoke", self.key], "y\n")
            self.assertEqual(code, 0, out + err)
            revoked = self.prepare(workflow=True)
        for after in (admitted, revoked):
            self.assertEqual(after.result.scope_plan, before.result.scope_plan)
            self.assertEqual(after.record["launch_fence"], before.record["launch_fence"])
            self.runtime.revalidate_operator(after)
        self.runtime.revalidate_operator(before)
        self.assertEqual(before.record["version"], 4)
        self.assertNotIn("unavailable_lines", before.record["applied"]["settings"])
        self.assertNotIn("not qualified", before.result.scope_plan.other_files[scope.LINEUP_MD].decode())
        self.assertIn("not qualified", "\n".join(before.notices))

    def test_copied_launch_card_still_revalidates_its_original_definition(self):
        prepared = self.prepare(lead=self.key, agent=False)
        shown = dataclasses.replace(prepared, notices=("presentation changed",))
        self.change_definition()
        with self.assertRaisesRegex(launch.LaunchError, "changed"):
            self.runtime.perform(shown)
        self.assertEqual(self.launches, [])

    def test_unadmitted_unapproved_route_refuses_lead_agent_and_workflow(self):
        state.atomic_write(self.ledger_file, strict_json.canonical_file_bytes(
            fixtures.operator_mod.ledger_document(None)))
        eff = self.runtime.current_effective()
        self.assertIn(self.key, eff.unavailable_lines)
        self.assertIn("providers approve acme", eff.unavailable_lines[self.key])
        for lead, agent, workflow in ((self.key, False, False), ("opus", True, False), ("opus", False, True)):
            with self.subTest(lead=lead, agent=agent, workflow=workflow):
                with self.assertRaises((types.LaunchPlanError, scope.ScopeError)):
                    self.prepare(lead=lead, agent=agent, workflow=workflow)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.http_calls, [])

    def test_doctor_reports_evidence_as_attention_and_usable_separately(self):
        doc = profile.ad_hoc_direct("opus", "high")
        doc["name"] = "local-test"
        doc["agents"] = {"cm-reviewer": {"model": self.key, "effort": "high"}}
        self.runtime.profiles.new(doc)
        problems, attention = doctor._doctor_profile_report(self.runtime)
        self.assertFalse(any("local-test" in problem for problem in problems))
        self.assertTrue(any("local-test" in line and "not qualified" in line for line in attention))
        blocks, attention, info = doctor._doctor_operator_report(self.runtime, None)
        self.assertEqual(blocks, [])
        self.assertTrue(any("0 qualified" in line and "1 usable for agents" in line for line in info))
        self.assertTrue(any("not qualified" in line for line in attention))


class PermissiveScopeTests(unittest.TestCase):
    def test_recommendation_override_workflow_is_in_exact_fence(self):
        bundle, lcat, eff, lineup = scope_fixtures._seed("balanced")
        key = next(key for key, entry in lcat.lines.items() if "agents" not in entry["capabilities"])
        eff = dataclasses.replace(eff, workflow_default_binding={"model": key, "effort": lcat.lines[key]["default_effort"]})
        plan = scope_fixtures._compile(lineup, lcat, eff, bundle)
        selected = plan.settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"]
        self.assertIn(selected, plan.settings["availableModels"])
        warnings = scope.workflow_default_warnings(lcat, eff, policy=lineup.policy)
        self.assertIn("capability-recommendation", {warning.code for warning in warnings})
        self.assertEqual(scope.fence_gaps([model for model in plan.settings["availableModels"] if model != selected],
                                         [selected]), (selected,))

    def test_provider_window_mismatch_warns_even_when_the_trigger_fits(self):
        bundle = scope_fixtures._bundle()
        docs = copy.deepcopy(bundle.docs)
        entry = docs["models-v2"]["models"]["gpt55"]
        entry["context"]["provider_tokens"] = 175000
        entry["context"]["validated_tokens"] = min(entry["context"]["validated_tokens"], 175000)
        lcat = profile.LineupCatalog.from_docs(docs)
        eff = settings.effective({"version": 1}, provider_ids=lcat.providers, line_keys=lcat.lines)
        document = profile.ad_hoc_direct("opus", "high")
        document["agents"] = {"cm-reviewer": {"model": "gpt55", "effort": "high"}}
        lineup = profile.resolve(document, lcat, effective=eff)
        role = dict(profile.lineup_windows(lineup))["cm-reviewer"]
        self.assertLess(role.trigger, 175000)
        self.assertGreater(role.window, 175000)
        warning = next(f.message for f in lineup.warnings if f.code == "context-risk")
        for text in ("200000", "175000", "162000", "effective client window exceeds"):
            self.assertIn(text, warning)
        scope_fixtures._compile(lineup, lcat, eff, bundle)
        workflow = dataclasses.replace(eff, workflow_default_binding={"model": "gpt55", "effort": "high"})
        findings = scope.workflow_default_warnings(lcat, workflow, policy=lineup.policy)
        self.assertTrue(any(f.code == "context-risk" and "175000" in f.message for f in findings))
        scope_fixtures._compile(lineup, lcat, workflow, bundle)

    def test_every_mutable_warning_is_excluded_from_scope_identity(self):
        bundle, lcat, eff, lineup = scope_fixtures._seed("balanced")
        before = scope_fixtures._compile(lineup, lcat, eff, bundle)
        for code in profile.TRANSIENT_WARNING_CODES:
            with self.subTest(code=code):
                finding = profile.Finding(code, None, "changed mutable diagnostic", "mutable diagnostic")
                changed = dataclasses.replace(lineup, warnings=(*lineup.warnings, finding))
                self.assertEqual(before, scope_fixtures._compile(changed, lcat, eff, bundle))

    def test_provider_family_label_cannot_create_native_fallback_defaults(self):
        bundle = scope_fixtures._bundle()
        docs = copy.deepcopy(bundle.docs)
        entry = docs["models-v2"]["models"]["grok46"]
        docs[profile.OPERATOR_LINES_KEY] = {"grok46": {"origin": "operator", "family": "anthropic"}}
        lcat = profile.LineupCatalog.from_docs(docs)
        eff = settings.effective({"version": 1}, provider_ids=lcat.providers, line_keys=lcat.lines)
        lineup = profile.resolve(profile.ad_hoc_direct("grok46", entry["default_effort"]), lcat,
                                 effective=eff, ad_hoc=True)
        self.assertEqual(scope.compile_fence(lineup, lcat, eff).fallback_only, ())
