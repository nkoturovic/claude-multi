"""#12 thinking validation/override ordering, and E7 wire census.

E0 is a synthetic branch fact, not a statement about every production wire.
E7 uses shipped wires only as evidence inputs to a fixture-local route.
The forced-tool-choice exception (A7, both forms/streams) is owned by
PayloadContractTests and its generated contract_exceptions evidence table.
"""
import copy
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from claude_multi import catalog, render
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog
import _gateway_harness as harness

MODULE = "tests.test_gateway_clamp"


def tearDownModule():
    harness.EVIDENCE.finalize_module(MODULE)


def thinking_body(form):
    if form == "absent":
        return {}
    if form == "adaptive":
        return {"thinking": {"type": "adaptive"}}
    if form.startswith("adaptive-"):
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": form.removeprefix("adaptive-")}}
    if form.startswith("enabled-"):
        budget = int(form.removeprefix("enabled-"))
        return {"thinking": {"type": "enabled", "budget_tokens": budget}, "max_tokens": budget + 1024}
    if form == "disabled":
        return {"thinking": {"type": "disabled"}}
    raise ValueError("unknown clamp thinking form")


def dotted(body, path):
    for key in path.split("."):
        if not isinstance(body, dict):
            return None
        body = body.get(key)
    return body


def outcome(reply, family):
    body = reply.hits[0].body if len(reply.hits) == 1 else {}
    path = {"claude": "output_config.effort", "codex": "reasoning.effort", "compat": "reasoning_effort"}[family]
    return {"status": reply.status, "hits": len(reply.hits), "thinking_present": "thinking" in body,
            "thinking_type": dotted(body, "thinking.type"),
            "budget_tokens_present": isinstance(body.get("thinking"), dict) and "budget_tokens" in body["thinking"],
            "effort_path": path, "effort": dotted(body, path),
            "top_level_reasoning_effort": body.get("reasoning_effort")}


class ClampShapeTests(unittest.TestCase):
    def setUp(self):
        self.bundle = catalog.load_catalog(FIXTURE_ROOT)

    def test_matrix_axes_forms_and_required_cells(self):
        _, aliases, _ = harness.build_document(self.bundle, 50121, 50122, Path("/fixture/home"))
        required = harness.REQUIRED_EVIDENCE[MODULE]["E"]
        self.assertEqual(len(required), 200)
        self.assertEqual(len(harness.CLAMP_FORMS), 10)
        for family, variants in harness.CLAMP_TARGETS.items():
            for variant, alias in variants.items():
                self.assertEqual(aliases[alias].family, family)
                for form in harness.CLAMP_FORMS:
                    body = thinking_body(form)
                    for stream in (False, True):
                        self.assertIn(f"{family}:{variant}:{form}:{stream}", required)
                    if form.startswith("enabled-"):
                        self.assertEqual(body["max_tokens"], body["thinking"]["budget_tokens"] + 1024)
        self.assertNotIn("output_config", thinking_body("adaptive"))
        self.assertEqual(thinking_body("absent"), {})
        self.assertEqual(thinking_body("disabled"), {"thinking": {"type": "disabled"}})
        with self.assertRaises(ValueError):
            thinking_body("unknown")

    def test_census_builder_is_pure_local_and_does_not_declare_levels(self):
        lines = {"gwtest-new": {"wire_model": "gwtest-wire-new", "status": "new"},
                 "gwtest-active": {"wire_model": "gwtest-wire-active", "status": "active"}}
        before, original = copy.deepcopy(self.bundle.docs), copy.deepcopy(lines)
        document, aliases, info = harness.build_census_document(self.bundle, 50121, 50122, Path("/fixture/home"), lines)
        self.assertEqual(self.bundle.docs, before)
        self.assertEqual(lines, original)
        self.assertEqual(set(info["census_lines"].values()), set(lines))
        self.assertEqual(render.rendered_selectors(document), frozenset(aliases))
        self.assertEqual(document["host"], "127.0.0.1")
        self.assertEqual(document["port"], 50122)
        self.assertEqual(len(document["claude-api-key"]), 1)
        self.assertNotIn("codex-api-key", document)
        for section in harness.model_sections(document):
            if section.get("name") == render.SENTINEL_NAME:
                self.assertEqual(section["base-url"], render.SENTINEL_BASE_URL)
                continue
            self.assertEqual(section["base-url"], "http://127.0.0.1:50121/gwtest-census")
            self.assertEqual(urlsplit(section["base-url"]).hostname, "127.0.0.1")
            for model in section["models"]:
                self.assertNotIn("thinking", model)
                self.assertTrue(model["force-mapping"])
                self.assertEqual(model["name"], lines[info["census_lines"][model["alias"]]]["wire_model"])
        self.assertEqual(len(document["payload"]["override"]), 1)
        self.assertEqual(document["payload"]["override"][0]["params"], {"output_config.effort": "high"})
        self.assertTrue(all(info.route == "gwtest-census" and not info.levels for info in aliases.values()))
        with self.assertRaisesRegex(AssertionError, "at least one"):
            harness.build_census_document(self.bundle, 50121, 50122, Path("/fixture/home"), {})

    def test_missing_stream_or_thinking_cell_refuses_evidence(self):
        tables = {table: [{"row": row} for row in required]
                  for table, required in harness.REQUIRED_EVIDENCE[MODULE].items()}
        document = {"schema": "gwtest-evidence-v1", "module": MODULE,
                    "binary": {"realpath": "/nix/store/fixture/bin/cli-proxy-api", "version": "7.3.15"},
                    "tables": tables}
        tables["census"] = [{"row": "gwtest-census-0", "line": "gwtest-new"},
                            {"row": "coverage", "lines": ["gwtest-new"], "count": 1, "complete": True}]
        harness.validate_evidence_document(document, MODULE)
        for coverage in ({"row": "coverage"},
                         {"row": "coverage", "lines": [], "count": 0, "complete": True},
                         {"row": "coverage", "lines": "gwtest-new", "count": 9, "complete": True},
                         {"row": "coverage", "lines": ["gwtest-new", "gwtest-new"], "count": 2, "complete": True}):
            invalid = copy.deepcopy(document)
            invalid["tables"]["census"] = [coverage]
            with self.subTest(coverage=coverage), self.assertRaisesRegex(AssertionError, "missing required census line evidence"):
                harness.validate_evidence_document(invalid, MODULE)
        missing = copy.deepcopy(document)
        missing["tables"]["census"].pop(0)
        with self.assertRaisesRegex(AssertionError, "missing required census line evidence"):
            harness.validate_evidence_document(missing, MODULE)
        for table in tables:
            changed = copy.deepcopy(document)
            changed["tables"][table].pop()
            with self.subTest(table=table), self.assertRaisesRegex(AssertionError, "missing required evidence rows"):
                harness.validate_evidence_document(changed, MODULE)


class ClampMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = harness.main_harness()

    def test_e_generated_cross_product_and_pinned_cells(self):
        observed = {}
        for family, variants in harness.CLAMP_TARGETS.items():
            for variant, alias in variants.items():
                for form in harness.CLAMP_FORMS:
                    for stream in (False, True):
                        with self.subTest(family=family, variant=variant, form=form, stream=stream):
                            reply = self.gateway.request(alias, stream=stream, body=thinking_body(form))
                            facts = outcome(reply, family)
                            observed[(family, variant, form, stream)] = facts
                            # Record first: a changed prediction remains evidence, never a silently weakened assertion.
                            harness.EVIDENCE.record(MODULE, "E", {
                                "row": f"{family}:{variant}:{form}:{stream}", "family": family,
                                "variant": variant, "form": form, "stream": stream, **facts})
                            self.assertEqual(len(reply.hits), 1 if reply.status == 200 else 0)
                            if reply.hits:
                                self.assertEqual(reply.hits[0].route, self.gateway.aliases[alias].route)
                                self.assertEqual(reply.hits[0].body["model"], self.gateway.aliases[alias].wire)
                            if stream:
                                json_facts = observed[(family, variant, form, False)]
                                self.assertEqual(facts["status"] // 100, json_facts["status"] // 100)
                                for key in ("effort", "top_level_reasoning_effort", "thinking_present"):
                                    self.assertEqual(facts[key], json_facts[key], key)
                            self.assert_pinned(family, variant, form, facts)
        self.assertEqual(len(observed), 200)

    def assert_pinned(self, family, variant, form, facts):
        key = (family, variant, form)
        if key == ("claude", "declared", "adaptive-max"):  # E1x: cannot rescue strict-validation failure
            self.assertNotEqual(facts["status"], 200)
            self.assertEqual(facts["hits"], 0)
            return
        effort = {
            ("claude", "none", "adaptive-low"): "high",    # E0
            ("claude", "none", "adaptive-max"): "high",
            ("claude", "declared", "adaptive-high"): "high",  # E1
            ("claude", "declared", "enabled-32000"): "high",  # E1b
            ("claude", "superset", "adaptive-max"): "low",  # E2
            ("compat", "plain", "adaptive-max"): "high",  # E3
            ("compat", "override", "adaptive-max"): "low",  # E4
            ("compat", "superset", "adaptive-max"): "max",  # E5
            ("codex", "none", "adaptive-max"): "low",  # E6
        }.get(key)
        if effort is None:
            return
        self.assertEqual(facts["status"], 200)
        self.assertEqual(facts["effort"], effort)
        if family == "claude" and variant == "none":
            self.assertFalse(facts["thinking_present"])
        if key == ("claude", "declared", "adaptive-high"):
            self.assertTrue(facts["thinking_present"])
        if key == ("claude", "declared", "enabled-32000"):
            self.assertEqual(facts["thinking_type"], "adaptive")
            self.assertFalse(facts["budget_tokens_present"])


@uses_shipped_catalog
class ShippedWireBranchCensusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shipped = catalog.load_catalog(SHIPPED_ROOT)
        cls.lines = {key: line for key, line in shipped.lines.items()
                     if shipped.providers[line["provider"]]["adapter"] == harness.CLAUDE}
        cls.gateway = harness.GatewayHarness(census_lines=cls.lines)
        cls.addClassCleanup(cls.gateway.close)

    def test_e7_each_shipped_direct_wire_classified(self):
        classified = set()
        for alias, key in self.gateway.info["census_lines"].items():
            with self.subTest(line=key):
                reply = self.gateway.request(alias, body=thinking_body("adaptive-max"))
                facts = outcome(reply, "claude")
                branch = "error" if reply.status != 200 else "kept" if facts["thinking_present"] else "strip"
                harness.EVIDENCE.record(MODULE, "census", {
                    "row": alias, "line": key, "wire": self.lines[key]["wire_model"], "branch": branch,
                    "classification": "branch per shipped wire (registry lookup outcome)", **facts})
                self.assertEqual(len(reply.hits), 1 if reply.status == 200 else 0)
                if reply.hits:
                    self.assertEqual(reply.hits[0].body["model"], self.lines[key]["wire_model"])
                classified.add(key)
        # This required row is emitted ONLY after every dynamic input was observed.
        self.assertEqual(classified, set(self.lines))
        self.assertEqual(len(self.gateway.aliases), len(self.lines))
        harness.EVIDENCE.record(MODULE, "census", {"row": "coverage", "lines": sorted(classified),
                                                  "count": len(classified), "complete": True})


if __name__ == "__main__":
    unittest.main()
