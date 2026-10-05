"""Observation cannot promote staged bindings or candidate attribution."""
from dataclasses import replace
import io
import json
import unittest
from unittest import mock

from claude_multi import lineup_log, routing, strict_json
import claude_multi.cli.entry as entry
import claude_multi.cli.gateway_facts as gateway_facts
import claude_multi.cli.session_facts as session_facts
from _v4 import V4Case
from tests.test_observations import NOW, MID, collect, final, inventory, log, source


class RoutingTests(unittest.TestCase):
    def report(self, events=None, bindings=None, *, selector="fixture-alias", key="fixture"):
        return routing.report(record={"managed_id": MID, "lineup_generation": 2, "pending": None},
               role="cm-analyst", binding={"key": key, "generation": "1", "effort": "high", "selector": selector},
               available=(selector,), lead_selectors=(), integrity="matched",
               bindings=bindings or lineup_log.BindingLog(), events=events or collect(),
               route=("provider", "fixture-wire", None, "catalog"), route_confirmed=True, now=NOW)

    def test_explain_record_fence_log_journal_are_distinct(self):
        report = self.report(collect(log("model=fixture-alias provider=claude")), lineup_log.BindingLog((
            lineup_log.BindingObservation(NOW, "subagent-start", 2, "cm-analyst", "agent-1", "fixture-alias"),), "partial"))
        text = routing.text(report)
        positions = [text.index(h) for h in ("Intent", "Fence", "Binding log", "Gateway route now", "Observed execution", "Conclusion")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("record", {f.source for f in report.facts})
        self.assertIn("lineup-log", {f.source for f in report.facts})
        self.assertIn("current-route", {f.source for f in report.facts})
        self.assertIn("journal", {f.source for f in report.facts})
        # The explained binding is named in both
        # views, and the layers use their labels.
        self.assertTrue(text.startswith(f"Routing explanation — session {MID[:8]} · cm-analyst\n"), text)
        self.assertEqual(json.loads(report.json())["facts"][0]["value"], "cm-analyst")
        self.assertIn("cm-analyst", report.json())
        for label in ("  recorded: fixture · effort high · selector fixture-alias · generation 1",
                      "  pending: none", "  selector: allowed as agent", "  scope integrity: matched",
                      "  process reload: unconfirmed", "  fixture-alias → provider / fixture-wire",
                      "  source: catalog", "    attribution: candidate-only"):
            self.assertIn(label, text)
        self.assertNotIn("process-reload: unknown", text)
        self.assertNotIn("intent-pending", text)
        agent = routing.report(record={"managed_id": MID, "lineup_generation": 2, "pending": None},
                               role="agent-1", explained_role="agent-1 → cm-analyst",
                               binding={"key": "fixture", "effort": "high", "selector": "fixture-alias"},
                               available=("fixture-alias",), lead_selectors=(), integrity="matched",
                               bindings=lineup_log.BindingLog(), events=collect(),
                               route=("provider", "fixture-wire", None, "catalog"), route_confirmed=True, now=NOW)
        self.assertIn("· agent-1 → cm-analyst\n", routing.text(agent))

    def test_binding_generation_does_not_confirm_reload(self):
        report = self.report(bindings=lineup_log.BindingLog((lineup_log.BindingObservation(
            NOW, "subagent-start", 2, "cm-analyst", "agent-1", "fixture-alias"),), "partial"))
        self.assertIsNone(next(f.value for f in report.facts if f.code == "process-reload"))
        self.assertIn("binding at gen 2 (reload unconfirmed)", routing.text(report))

    def test_time_window_match_is_candidate_only(self):
        report = self.report(collect(log("model=fixture-alias provider=claude")))
        self.assertIn(routing.CANDIDATE, routing.text(report))
        self.assertEqual([f.value for f in report.facts if f.code == "attribution"], ["candidate-only"])

    def test_context_suffix_matches_base_alias_journal_candidates_and_status(self):
        report = self.report(collect(log("session-affinity: provider=claude model=fixture-alias"), final()),
                             selector="fixture-alias[1m]")
        values = {f.code: f.value for f in report.facts}
        self.assertEqual(values["intent-selector"], "fixture-alias[1m]")
        self.assertTrue(values["fence-allowed"])
        self.assertEqual(values["requested-selector"], "fixture-alias")
        self.assertEqual(values["request-id"], "a1b2c3d4")
        self.assertEqual(values["attribution"], "candidate-only")
        self.assertEqual(values["http-status"], 200)
        self.assertEqual(values["conclusion"], routing.CANDIDATE)

    def test_record_key_uses_session_schema_including_retired_generation(self):
        for key, expected in (("fixture@4-7", "fixture@4-7"), ("bad/key", None), ("X", None), ("a" * 65, None)):
            with self.subTest(key=key):
                fact = next(f for f in self.report(key=key).facts if f.code == "intent-key")
                self.assertEqual(fact.value, expected)

    def test_truncated_session_id_never_becomes_exact(self):
        report = self.report(collect(log(f"model=fixture-alias provider=claude session={MID[:8]}")))
        self.assertEqual([f.value for f in report.facts if f.code == "attribution"], ["candidate-only"])
        self.assertNotIn("session=", report.json())

    def test_substitution_absence_is_not_served_model_proof(self):
        report = self.report(collect(log("model=fixture-alias provider=claude")))
        self.assertEqual([f.value for f in report.facts if f.code == "upstream-reported-model"], [None])
        substitution = self.report(collect(log('claude executor: upstream served model "different" for requested model "fixture-wire" (auth_index=private)')))
        self.assertIn("The gateway reported substitution: fixture-wire → different.", routing.text(substitution))
        values = {f.code: f.value for f in substitution.facts}
        self.assertEqual(values["conclusion"], "The gateway reported substitution: fixture-wire → different.")
        self.assertEqual(values["attribution"], "candidate-only")
        self.assertNotIn("private", substitution.json())


class ExplainCommandTests(V4Case):
    def test_cli_selection_prefix_and_agent_are_read_only(self):
        record = self.launch_fresh()
        mid = record["managed_id"]
        with mock.patch.object(gateway_facts, "_read_gateway_journal", return_value=source()):
            output = io.StringIO()
            status = entry.main(["explain", mid[:8], "--json"], runtime=self.runtime,
                                output_stream=output, interactive=False)
        self.assertEqual(status, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["report"], "explain")
        self.assertEqual(self.runtime.session_store.load(mid), record)
        with self.assertRaisesRegex(Exception, "agent must be"):
            session_facts.explanation(self.runtime, mid, "not-a-role")

    def test_null_selector_generation_zero_text_and_json_are_read_only(self):
        record = self.launch_fresh()
        mid = record["managed_id"]
        record["applied"]["lead"]["selector"] = None
        record["lineup_generation"] = 0
        record.pop("launch_fence", None)
        record.pop("scope_lead", None)
        record["applied_hash"] = strict_json.bundle_digest(record["applied"])
        self.store.save(record)
        runtime = self.make_runtime(allow_state_writes=False, refresh_shims=False, initialize_session_store=False)
        before = inventory(self.root)
        for selection in ([mid], []):
            for flags in ([], ["--json"]):
                with self.subTest(selection=selection, flags=flags), \
                        mock.patch.object(gateway_facts, "_read_gateway_journal", return_value=source()):
                    out = io.StringIO()
                    status = entry.main(["explain", *selection, *flags], runtime=runtime,
                                        output_stream=out, interactive=False)
                    self.assertEqual(status, 0)
                    if flags:
                        report = json.loads(out.getvalue())
                        values = {f["code"]: f["value"] for f in report["facts"]}
                        self.assertIsNone(values["intent-selector"])
                        self.assertIsNone(values["route-provider"])
                        self.assertIsNone(values["route-wire"])
                        self.assertFalse(values["route-confirmed"])
                    else:
                        self.assertIn("· selector unknown ·", out.getvalue())
                        self.assertIn("  unknown → unknown / unknown", out.getvalue())
                        self.assertIn(" · lead\n", out.getvalue())
        self.assertEqual(inventory(self.root), before)

    def test_base_alias_journal_matches_recorded_context_selector(self):
        record = self.launch_fresh()
        selector = record["applied"]["lead"]["selector"]
        self.assertTrue(selector.endswith("[1m]"))
        runtime = self.make_runtime(allow_state_writes=False, refresh_shims=False, initialize_session_store=False)
        rows = source(log(f"session-affinity: provider=claude model={selector.removesuffix('[1m]')}"), final())
        with mock.patch.object(gateway_facts, "_read_gateway_journal", return_value=rows):
            out = io.StringIO()
            self.assertEqual(entry.main(["explain", record["managed_id"], "--json"], runtime=runtime,
                                        output_stream=out, interactive=False), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["coverage"]["lineup_log"], "absent")
        values = {f["code"]: f["value"] for f in report["facts"]}
        self.assertEqual(values["http-status"], 200)
        self.assertEqual(values["attribution"], "candidate-only")
        self.assertEqual(values["conclusion"], routing.CANDIDATE)

    def test_no_selection_refuses_without_picker(self):
        self.runtime.environ.pop("CLAUDE_MULTI_MANAGED_ID", None)
        with self.assertRaisesRegex(Exception, "no managed session selected"):
            session_facts.report_session(self.runtime)
