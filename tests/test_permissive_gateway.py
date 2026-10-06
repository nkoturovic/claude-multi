"""Compiled, unattested LAN agent selectors reach the keyless fake upstream.

The real manifest gateway runs in the existing private network namespace and
uses unix-socket bridges. These are gateway dispatch/retention observations,
not a real-client Agent-tool probe or a retention-patch omission discriminator.
"""

from __future__ import annotations

import copy
import unittest

import _gateway_harness as harness
from _catalog import FIXTURE_ROOT
from claude_multi import operator, profile, render, scope, settings, strict_json


PROVIDER = "permissive-lan"
KEY = "custom-permissive-lan"
WIRE = "fixture-lan-agent-wire"
ROLE = "cm-reviewer"


class PermissiveLanGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = harness.GatewayHarness(document_factory=self._document)
        self.addCleanup(self.gateway.close)

    def _document(self, bundle, egress, port, home):
        docs = bundle.docs
        declaration = {
            "version": 1,
            "provider": {
                "display": "Fixture LAN server",
                "kind": "openai-compatible-lan",
                "base_url": f"http://127.0.0.1:{egress}/{PROVIDER}/v1",
                "auth": {"kind": "none"},
                "independence_family": "Fixture LAN family",
            },
            "lines": {
                KEY: {
                    "wire_model": WIRE,
                    "display": "Fixture LAN agent",
                    "efforts": ["high"],
                    "default_effort": "high",
                    "context": {"declared_tokens": 128000, "source": "operator"},
                },
            },
        }
        layer = operator.validate_layer(
            docs, {PROVIDER: strict_json.pretty_file_bytes(declaration)},
            schemas=operator.load_schemas(FIXTURE_ROOT), ledger=None, evidence=None,
        )
        self.assertEqual(layer.problems, ())
        self.assertEqual(layer.route_status[PROVIDER], "keyless")
        entry = layer.lines[KEY].core_entry
        self.assertEqual(entry["capabilities"], ["lead"])
        self.assertEqual(entry["roles"], [])
        fields = operator.agent_fact_fields(
            KEY, layer=layer, ledger=None, evidence=None, admitted_lines=(),
            provider_enabled=True, contracts={}, docs=docs, trusted_docs=docs,
        )
        self.assertIsNotNone(fields)
        self.assertFalse(fields["admitted"])
        self.assertEqual(fields["evidence"], operator.EVIDENCE_MISSING)
        cat = profile.LineupCatalog.from_docs(operator.merge_docs(docs, layer)).with_gate(
            profile.AgentGate(profile.AGENT_MODE_CURRENT, {KEY: profile.AgentFacts(**fields)}),
        )
        eff = settings.effective({"version": 1}, provider_ids=cat.providers, line_keys=cat.lines)
        self.assertFalse(eff.admitted_lines)
        document = profile.ad_hoc_direct(bundle.seed_profiles["direct"]["lead"]["model"])
        document["agents"] = {ROLE: {"model": KEY, "effort": "high"}}
        evaluation = profile.evaluate(document, cat, effective=eff)
        self.assertEqual(evaluation.errors, ())
        lineup = evaluation.lineup
        self.assertIsNotNone(lineup)
        warnings = {finding.code for finding in lineup.warnings if finding.slot == ROLE}
        self.assertTrue({"admission", "qualification", "capability-recommendation",
                         "role-recommendation", "context-risk"} <= warnings, warnings)
        binding = lineup.agents[ROLE].binding
        self.assertEqual(binding.client_context_tokens, 200000)
        compiled = scope.compile_lineup_scope(
            lineup, cat, eff, bundle.prompt_bodies, scope.catalog_meta_v2(docs), lineup_generation=1,
        )
        frontmatter = compiled.agent_files[f".claude/agents/{ROLE}.md"].decode().split("---", 2)[1]
        fields = dict(line.split(": ", 1) for line in frontmatter.splitlines() if ": " in line)
        self.selector = fields["model"]
        self.assertEqual(self.selector, binding.selector)
        self.assertEqual(fields["effort"], "high")
        self.assertIn(self.selector, compiled.settings["availableModels"])
        self.assertNotEqual(self.selector, lineup.lead.binding.selector)

        plan = operator.render_plan(docs, layer, None)
        gateway_doc = copy.deepcopy(docs["gateway"])
        gateway_doc["gateway"]["base_url"] = f"http://127.0.0.1:{port}"

        def no_secret(_name):
            self.fail("the keyless LAN render requested a credential")

        config, _available, _unavailable, info = render.build_config_document(
            gateway_doc, {PROVIDER: plan.docs["providers"]["providers"][PROVIDER]},
            {KEY: plan.docs["models"]["models"][KEY]}, home=home,
            gateway_token=harness.FIXTURE_GATEWAY_TOKEN, resolve_secret=no_secret,
            continuity={}, captures=plan.captures, provider_headers=plan.headers, oauth_overlay=plan.overlay,
        )
        route = next(row for row in config["openai-compatibility"] if row["name"] == PROVIDER)
        self.assertEqual(route["base-url"], declaration["provider"]["base_url"])
        self.assertNotIn("api-key-entries", route)
        self.assertEqual([(row["name"], row["alias"], row["force-mapping"]) for row in route["models"]],
                         [(WIRE, self.selector, True)])
        aliases = {self.selector: harness.AliasInfo(PROVIDER, PROVIDER, harness.COMPAT,
                                                  "compat", WIRE, None, origin="operator")}
        return config, aliases, info

    def _dispatch(self, *, stream: bool) -> None:
        reply = self.gateway.request(self.selector, stream=stream, body={"prompt_cache_retention": "24h"})
        self.assertEqual(reply.status, 200, self.gateway.diagnostic("LAN agent request failed"))
        self.assertEqual(len(reply.hits), 1)
        hit = reply.hits[0]
        self.assertEqual(hit.route, PROVIDER)
        self.assertEqual(hit.path, f"/{PROVIDER}/v1/chat/completions")
        self.assertEqual(hit.method, "POST")
        self.assertEqual(hit.body["model"], WIRE)
        self.assertIs(hit.body["stream"], stream)
        self.assertNotIn("authorization", hit.header_names)
        self.assertNotIn("x-api-key", hit.header_names)
        self.assertNotIn("prompt_cache_retention", hit.top_level_keys)
        self.assertNotIn(harness.FIXTURE_GATEWAY_TOKEN.encode(), hit.raw)
        if stream:
            events = reply.events()
            self.assertEqual(events[0]["type"], "message_start")
            self.assertEqual(events[-1]["type"], "message_stop")
            self.assertIn("FAKE_OK", "".join(event.get("delta", {}).get("text", "") for event in events))
        else:
            self.assertEqual(reply.json()["stop_reason"], "end_turn")
            self.assertIn("FAKE_OK", "".join(block.get("text", "") for block in reply.json()["content"]))

    def test_unattested_lan_agent_json_dispatch_and_retention(self) -> None:
        self._dispatch(stream=False)

    def test_unattested_lan_agent_stream_dispatch_and_retention(self) -> None:
        self._dispatch(stream=True)
