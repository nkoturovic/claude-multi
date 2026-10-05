"""Generated payload contracts and offline composition observations.

Only the Claude HTTP retention rows discriminate the retention patch here. Tokens,
Codex/WS, Meta and xAI retention call sites remain Go postCheck coverage.
B081/B088 are observations, not evidence of a repaired product. In particular,
a local count says nothing about provider-token accuracy or the adequacy of
compaction margins; this harness cannot settle that uncertainty.
"""
import copy
import json
import re
import time
import unittest
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from claude_multi import catalog, operator, render, state
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog
import _gateway_harness as harness

MODULE = "tests.test_gateway_contracts"
TOOL = {"name": "gwtest_tool", "description": "fixture tool",
        "input_schema": {"type": "object", "properties": {"k": {"type": "string"}}}}


def tearDownModule():
    harness.EVIDENCE.finalize_module(MODULE)


def dotted(body, path):
    for key in path.split("."):
        if not isinstance(body, dict) or key not in body:
            return None
        body = body[key]
    return body


def override_cases(aliases):
    """The aliases actually exercised by A, not a second contract inventory."""
    return [(alias, info, render.ADAPTER_PAYLOAD_CONTRACTS[info.adapter][info.contract])
            for alias, info in sorted(aliases.items())
            if info.origin == "catalog" and info.route in ("gwtest-cc", "gwtest-codex")
            and info.contract and alias == info.route + "-" + info.contract]


def conflicting(contract):
    value = next(iter(contract["params"].values()))
    other = "high" if value == "low" else "low"
    if "reasoning_effort" in contract["params"]:
        return {"reasoning_effort": other}
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": other}}


def streamed_row(row, stream):
    return row + ("-stream" if stream else "-json")


def reconstructed_blocks(reply):
    """Reassemble actual client SSE blocks, including unsigned thinking."""
    blocks = {}
    for event in reply.events():
        if event["type"] == "content_block_start":
            blocks[event["index"]] = copy.deepcopy(event["content_block"])
        elif event["type"] == "content_block_delta":
            block, delta = blocks[event["index"]], event["delta"]
            for kind, key in (("text_delta", "text"), ("thinking_delta", "thinking"),
                              ("signature_delta", "signature"), ("input_json_delta", "partial_json")):
                if delta["type"] == kind:
                    block[key] = block.get(key, "") + delta[key]
    return [blocks[index] for index in sorted(blocks)]


class ContractCompletenessTests(unittest.TestCase):
    """Contract completeness always runs, even when the live classes are
    boundary-skipped."""

    def setUp(self):
        self.document, self.aliases, _ = harness.build_document(
            catalog.load_catalog(FIXTURE_ROOT), 50121, 50122, Path("/fixture/home"))

    def test_a6_every_contract_kind_and_override_is_exercised(self):
        expected = set()
        filters = set()
        for adapter, contracts in render.ADAPTER_PAYLOAD_CONTRACTS.items():
            for cid, contract in contracts.items():
                self.assertIn(contract["kind"], ("override", "filter"), (adapter, cid))
                (expected if contract["kind"] == "override" else filters).add((adapter, cid))
        cases = override_cases(self.aliases)
        actual = [(info.adapter, info.contract) for _, info, _ in cases]
        self.assertEqual(Counter(actual), Counter({key: 1 for key in expected}))
        self.assertEqual(filters, {(harness.CLAUDE, "filter-thinking")})
        filtered = {model["name"] for rule in self.document["payload"]["filter"] for model in rule["models"]}
        self.assertEqual(filtered, {alias for alias, info in self.aliases.items() if info.route == "gwtest-ccf"})
        for adapter, cid in expected:
            for stream in (False, True):
                self.assertIn(harness.contract_row(adapter, cid, stream),
                              harness.REQUIRED_EVIDENCE[MODULE]["contracts"])
        # The count is derived, including the Codex max override.
        self.assertEqual(len(harness.REQUIRED_EVIDENCE[MODULE]["contracts"]), 2 * len(expected))

    def test_nested_route_is_local_and_rendered_with_tools_effort_contract(self):
        sections = [section for section in self.document["claude-api-key"]
                    if urlsplit(section["base-url"]).path == "/gwtest-nested/anthropic"]
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["base-url"], "http://127.0.0.1:50121/gwtest-nested/anthropic")
        self.assertEqual(sections[0]["auth-header"], "x-api-key")
        aliases = {alias for alias, info in self.aliases.items() if info.route == "gwtest-nested"}
        self.assertEqual(aliases, {model["alias"] for model in sections[0]["models"]})
        self.assertEqual(len(aliases), 1)
        self.assertTrue(all(self.aliases[alias].contract == "output-config-high" for alias in aliases))

    def test_missing_matrix_evidence_is_rejected(self):
        evidence = harness.Evidence()
        evidence.binary = {"realpath": "/nix/store/fixture/bin/cli-proxy-api", "version": "7.3.15"}
        for table, rows in harness.REQUIRED_EVIDENCE[MODULE].items():
            for row in rows:
                evidence.record(MODULE, table, {"row": row})
        document = {"schema": "gwtest-evidence-v1", "module": MODULE,
                    "binary": evidence.binary, "tables": evidence.tables[MODULE]}
        harness.validate_evidence_document(document, MODULE)
        for table in document["tables"]:
            changed = copy.deepcopy(document)
            changed["tables"][table].pop()
            with self.subTest(table=table), self.assertRaisesRegex(AssertionError, "missing required evidence rows"):
                harness.validate_evidence_document(changed, MODULE)


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = harness.main_harness()

    def family_aliases(self):
        return (self.alias("gwtest-cc", "output-config-high", levels=()),
                self.alias("gwtest-codex", "reasoning-effort-high", levels=()), "gwtest-compat-plain")

    def alias(self, route, contract=None, origin="catalog", levels=None):
        matches = [alias for alias, info in self.gateway.aliases.items()
                   if info.route == route and info.origin == origin
                   and (contract is None or info.contract == contract)
                   and (levels is None or info.levels == levels)]
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def record(self, table, row, reply, **facts):
        hit = reply.hits[0] if len(reply.hits) == 1 else None
        body = hit.body if hit else {}
        harness.EVIDENCE.record(MODULE, table, {
            "row": row, "classification": "contract", "status": reply.status,
            "hits": len(reply.hits), "path": hit.path if hit else None,
            "thinking_type": dotted(body, "thinking.type"),
            "output_config_effort": dotted(body, "output_config.effort"),
            "reasoning_effort": dotted(body, "reasoning.effort") or body.get("reasoning_effort"),
            **facts,
        })

    def hit(self, reply, alias):
        # Avoid dumping raw captures (including dummy credentials) on failure.
        self.assertEqual(reply.status, 200)
        self.assertEqual(len(reply.hits), 1)
        hit = reply.hits[0]
        info = self.gateway.aliases[alias]
        suffix = {"claude": "/v1/messages", "codex": "/responses", "compat": "/chat/completions"}[info.family]
        base = next(section["base-url"] for section in harness.model_sections(self.gateway.document)
                    if any(model["alias"] == alias for model in section["models"]))
        self.assertEqual(hit.path, urlsplit(base).path + suffix)
        self.assertEqual(hit.route, info.route)
        if info.family == "claude":
            self.assertEqual(hit.query, "beta=true")
        self.assertEqual(hit.body["model"], info.wire)
        return hit

    def assert_client_alias(self, reply, alias, stream):
        if stream:
            starts = [event["message"] for event in reply.events() if event["type"] == "message_start"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["model"], alias)
        else:
            self.assertEqual(reply.json()["model"], alias)

    def assert_contract(self, reply, alias, contract):
        hit = self.hit(reply, alias)
        for path, value in contract["params"].items():
            self.assertEqual(dotted(hit.body, path), value, (alias, path))
        if self.gateway.aliases[alias].family == "codex":
            self.assertIs(hit.body["stream"], True)
        elif "output_config.effort" in contract["params"]:
            self.assertNotIn("reasoning_effort", hit.body)


class PayloadContractTests(GatewayTests):
    def test_a1_a4_generated_overrides_and_shared_wire_siblings(self):
        observed = {}
        for alias, info, contract in override_cases(self.gateway.aliases):
            for stream in (False, True):
                with self.subTest(adapter=info.adapter, contract=info.contract, stream=stream):
                    reply = self.gateway.request(alias, stream=stream, body=conflicting(contract))
                    self.record("contracts", harness.contract_row(info.adapter, info.contract, stream), reply,
                                adapter=info.adapter, contract=info.contract, stream=stream)
                    self.assert_contract(reply, alias, contract)
                    observed[(alias, stream)] = reply.hits[0].body
        groups = {}
        for alias, info, contract in override_cases(self.gateway.aliases):
            groups.setdefault((info.route, info.wire), []).append((alias, contract))
        self.assertTrue(any(len(group) > 1 for group in groups.values()))
        for group in groups.values():
            for alias, contract in group:
                for stream in (False, True):
                    for path, value in contract["params"].items():
                        self.assertEqual(dotted(observed[(alias, stream)], path), value)

    def test_a5_keyless_compat_has_no_payload_contract(self):
        alias = "gwtest-compat-plain"
        reply = self.gateway.request(alias)
        self.record("composition", "A5", reply)
        hit = self.hit(reply, alias)
        for rules in self.gateway.document["payload"].values():
            self.assertFalse(any(model["name"] == alias for rule in rules for model in rule["models"]))
        for contracts in render.ADAPTER_PAYLOAD_CONTRACTS.values():
            for contract in contracts.values():
                for path in contract["params"]:
                    self.assertIsNone(dotted(hit.body, path), path)

    def test_a7_forced_tool_choice_contract_exceptions(self):
        for alias, info, contract in override_cases(self.gateway.aliases):
            if info.adapter != harness.CLAUDE:
                continue
            for choice in ({"type": "any"}, {"type": "tool", "name": TOOL["name"]}):
                for stream in (False, True):
                    with self.subTest(contract=info.contract, choice=choice["type"], stream=stream):
                        reply = self.gateway.request(alias, stream=stream, body={
                            **conflicting(contract), "tools": [TOOL], "tool_choice": choice})
                        self.record("contract_exceptions", harness.contract_row(info.adapter, info.contract, stream, choice["type"]),
                                    reply, classification="observation", contract=info.contract,
                                    choice=choice["type"], stream=stream)
                        hit = self.hit(reply, alias)
                        if "output_config.effort" in contract["params"]:
                            self.assertIsNone(dotted(hit.body, "output_config.effort"))
                            self.assertNotIn("thinking", hit.body)
                        else:
                            self.assertEqual(hit.body["reasoning_effort"], contract["params"]["reasoning_effort"])

    def test_a8_continuity_overrides(self):
        for alias, info in self.gateway.aliases.items():
            if info.origin != "continuity" or info.route not in ("gwtest-cc", "gwtest-ccb"):
                continue
            contract = render.ADAPTER_PAYLOAD_CONTRACTS[info.adapter][info.contract]
            for stream in (False, True):
                with self.subTest(alias=alias, stream=stream):
                    reply = self.gateway.request(alias, stream=stream, body=conflicting(contract))
                    self.record("composition", streamed_row("A8-" + info.route, stream), reply)
                    self.assert_contract(reply, alias, contract)


class ThinkingFilterTests(GatewayTests):
    def test_b_filter_and_unfiltered_discriminators(self):
        targets = [(alias, info) for alias, info in self.gateway.aliases.items()
                   if info.route == "gwtest-ccf" or (info.route == "gwtest-cc" and info.levels)]
        self.assertEqual(len(targets), 5)
        for alias, info in targets:
            filtered = info.route == "gwtest-ccf"
            for stream in (False, True):
                with self.subTest(alias=alias, stream=stream):
                    reply = self.gateway.request(alias, stream=stream, body={
                        "thinking": {"type": "adaptive"}, "output_config": {"effort": info.levels[0]}})
                    self.record("filter", streamed_row(alias, stream), reply, filtered=filtered)
                    hit = self.hit(reply, alias)
                    self.assertEqual(dotted(hit.body, "output_config.effort"), info.levels[0])
                    self.assertEqual("thinking" in hit.body, not filtered,
                                     "B2 discriminator broken: unfiltered control must preserve thinking")


class CompositionTests(GatewayTests):
    def test_c1_c3_force_mapping(self):
        for alias in self.family_aliases():
            family = self.gateway.aliases[alias].family
            for stream in (False, True):
                with self.subTest(family=family, stream=stream):
                    reply = self.gateway.request(alias, stream=stream)
                    self.record("composition", streamed_row("C-" + family, stream), reply,
                                emulated=family == "codex")
                    self.hit(reply, alias)
                    self.assert_client_alias(reply, alias, stream)

    def test_c4_served_set_is_exact(self):
        headers = self.gateway.headers()
        headers.pop("anthropic-version")
        reply = harness.unix_request(self.gateway.socket, "GET", "/v1/models", headers=headers)
        self.assertEqual(reply.status, 200)
        ids = {row["id"] for row in reply.json()["data"]}
        expected = render.rendered_selectors(self.gateway.document) | {render.document_sentinel(self.gateway.document)}
        harness.EVIDENCE.record(MODULE, "composition", {"row": "C4", "classification": "contract",
                                                        "status": reply.status, "count": len(ids), "expected_count": len(expected)})
        self.assertEqual(ids, expected)
        self.assertFalse(any(name.startswith("gwtest-wire-") for name in ids))

    def test_c5_suffix_is_not_a_gateway_alias(self):
        reply = self.gateway.request(self.family_aliases()[0] + "[1m]")
        self.record("composition", "C5", reply)
        self.assertNotEqual(reply.status, 200)
        self.assertEqual(len(reply.hits), 0)

    def test_c6_substitution_is_hidden_but_logged(self):
        # One substitution per credential/model pair: the gateway throttles
        # this warning, so repeating the same pair in stream mode is not an
        # independent log discriminator. C1-C3 cover both response forms.
        for alias in self.family_aliases():
            info = self.gateway.aliases[alias]
            with self.subTest(family=info.family):
                offset = self.gateway.log_path.stat().st_size
                reply = self.gateway.request(alias, mode="substitute")
                pattern = re.compile(r'executor: upstream served model \\?"gwtest-served-other\\?" for requested model \\?"'
                                     + re.escape(info.wire) + r'\\?"')
                deadline = time.monotonic() + 2
                while True:
                    with self.gateway.log_path.open("rb") as log:
                        log.seek(offset)
                        matches = pattern.findall(log.read().decode(errors="replace"))
                    if matches or time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
                self.record("composition", "C6-" + info.family, reply, log_matches=len(matches))
                self.hit(reply, alias)
                self.assert_client_alias(reply, alias, False)
                self.assertEqual(len(matches), 1)


class KimiCompatAuthTests(GatewayTests):
    def test_header_auth_sends_x_api_key_only(self):
        alias = self.alias("gwtest-cc", "output-config-high", levels=())
        for stream in (False, True):
            reply = self.gateway.request(alias, stream=stream)
            self.record("auth", streamed_row("K1", stream), reply)
            hit = self.hit(reply, alias)
            self.assertTrue(hit.header_values.get("x-api-key") == "dummy-gwtest-cc")
            self.assertNotIn("authorization", hit.header_names)

    def test_custom_authorization_header_never_displaces_x_api_key(self):
        alias = self.alias("gwtest-cch")
        version = catalog.load_catalog(FIXTURE_ROOT).docs["native-contract"]["verified"][0]["version"]
        for identity in (False, True):
            for stream in (False, True):
                headers, body = {}, {}
                if identity:
                    headers = {"x-app": "cli", "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14",
                               "User-Agent": f"claude-cli/{version} (external, cli)"}
                    body = {"metadata": {"user_id": json.dumps({"device_id": "a" * 64, "account_uuid": "",
                                                                "session_id": str(uuid.uuid4())})}}
                with self.subTest(identity=identity, stream=stream):
                    reply = self.gateway.request(alias, stream=stream, headers=headers, body=body)
                    hit = self.hit(reply, alias)
                    detected = "anthropic-dangerous-direct-browser-access" in hit.header_names
                    self.record("auth", streamed_row("K2-" + str(identity), stream), reply, identity_branch=detected,
                                key_matches=hit.header_values.get("x-api-key") == "dummy-gwtest-cch",
                                authorization_absent="authorization" not in hit.header_names)
                    self.assertEqual(detected, identity, "K2 detector precondition: check fixture version against gateway baseline")
                    self.assertTrue(hit.header_values.get("x-api-key") == "dummy-gwtest-cch")
                    self.assertNotIn("authorization", hit.header_names)

    def test_bearer_auth_and_continuity_keep_authorization_only(self):
        for alias, info in self.gateway.aliases.items():
            if info.route != "gwtest-ccb":
                continue
            for stream in (False, True):
                with self.subTest(alias=alias, stream=stream):
                    reply = self.gateway.request(alias, stream=stream)
                    self.record("auth", streamed_row("K3-" + info.origin, stream), reply)
                    hit = self.hit(reply, alias)
                    self.assertTrue(hit.header_values.get("authorization") == "Bearer dummy-gwtest-ccb")
                    self.assertNotIn("x-api-key", hit.header_names)

    def test_models_listing_carries_owned_by_and_context_length(self):
        reply = self.gateway.get("/v1/models")
        self.assertEqual(reply.status, 200)
        rows = reply.json()["data"]
        groups = {}
        for section in self.gateway.document["claude-api-key"]:
            for model in section["models"]:
                key = (model["display-name"], model["owned-by"], model["context-length"])
                groups.setdefault(key, set()).add(model["alias"])
        for (display, owner, context), aliases in groups.items():
            with self.subTest(display=display):
                matching = [row for row in rows if row["display_name"] == display]
                self.assertEqual(len(matching), len(aliases))
                self.assertTrue(all(row["owned_by"] == owner and row["max_input_tokens"] == context for row in matching))
        harness.EVIDENCE.record(MODULE, "auth", {"row": "K4", "classification": "contract", "groups": len(groups)})


class ResponsePassthroughTests(GatewayTests):
    def test_p_marker_is_set_by_fake_and_not_forwarded(self):
        for alias in self.family_aliases():
            info = self.gateway.aliases[alias]
            for stream in (False, True):
                with self.subTest(family=info.family, stream=stream):
                    reply = self.gateway.request(alias, stream=stream)
                    hit = self.hit(reply, alias)
                    direct = harness.unix_request(self.gateway.run / "upstream.sock", "POST", hit.path,
                                                  headers={"Content-Type": "application/json"}, body=hit.raw)
                    self.record("passthrough", streamed_row(info.family, stream), reply,
                                fake_marker=direct.headers.get(harness.MARKER) == "1")
                    self.assertEqual(direct.headers[harness.MARKER], "1")
                    self.assertNotIn(harness.MARKER, reply.headers)


class CompatTranslationTests(GatewayTests):
    def request(self, row, *, alias="gwtest-compat-plain", stream=False, **kwargs):
        reply = self.gateway.request(alias, stream=stream, **kwargs)
        self.record("fidelity", row, reply, classification="observation", stream=stream)
        return reply, self.hit(reply, alias)

    def test_f1_scalar_and_system_translation(self):
        system = "SYS-" + uuid.uuid4().hex
        _, hit = self.request("F1", body={"max_tokens": 77, "temperature": 0.3, "top_p": 0.9,
                                        "stop_sequences": ["STOP1"], "system": system})
        self.assertEqual(hit.body["max_tokens"], 77)
        self.assertEqual(hit.body["temperature"], 0.3)
        self.assertNotIn("top_p", hit.body)
        self.assertEqual(hit.body["stop"], ["STOP1"])
        self.assertIs(hit.body["stream"], False)
        self.assertEqual(hit.body["messages"][0]["role"], "system")
        self.assertIn(system, json.dumps(hit.body["messages"][0]["content"]))

    def test_f2_tool_definitions_and_choices(self):
        for name, choice, expected in (
            ("any", {"type": "any"}, "required"),
            ("tool", {"type": "tool", "name": TOOL["name"]}, {"type": "function", "function": {"name": TOOL["name"]}}),
            ("serial", {"type": "any", "disable_parallel_tool_use": True}, "required"),
        ):
            with self.subTest(choice=name):
                _, hit = self.request("F2-" + name, body={"tools": [TOOL], "tool_choice": choice})
                self.assertEqual(hit.body["tools"][0], {"type": "function", "function": {
                    "name": TOOL["name"], "description": TOOL["description"], "parameters": TOOL["input_schema"]}})
                self.assertEqual(hit.body["tool_choice"], expected)
                if name == "serial":
                    self.assertIs(hit.body["parallel_tool_calls"], False)

    def test_f3_tool_result_precedes_user_text(self):
        _, hit = self.request("F3", body={"messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "gwtest-call", "name": TOOL["name"], "input": {"k": "v"}}]},
            {"role": "user", "content": [{"type": "text", "text": "gwtest-follow-up"},
                                           {"type": "tool_result", "tool_use_id": "gwtest-call", "content": "fixture result"}]},
        ]})
        messages = hit.body["messages"]
        assistant = next(message for message in messages if message["role"] == "assistant")
        call = assistant["tool_calls"][0]
        self.assertEqual({k: call[k] for k in ("id", "type")}, {"id": "gwtest-call", "type": "function"})
        self.assertEqual(call["function"]["name"], TOOL["name"])
        self.assertEqual(json.loads(call["function"]["arguments"]), {"k": "v"})
        tool_index = next(i for i, message in enumerate(messages) if message["role"] == "tool")
        text_index = next(i for i, message in enumerate(messages) if "gwtest-follow-up" in json.dumps(message))
        self.assertLess(tool_index, text_index)
        self.assertEqual(messages[tool_index]["tool_call_id"], "gwtest-call")

    def test_f4_claude_signed_and_redacted_thinking_is_not_forwarded(self):
        _, hit = self.request("F4", body={"messages": [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "fixture reasoning", "signature": "claude-signature-fixture"},
            {"type": "redacted_thinking", "data": "gwtest-redacted"}, {"type": "text", "text": "prior answer"}]}]})
        self.assertNotIn('"reasoning_content"', json.dumps(hit.body))

    def test_f5_text_and_f5c_length_streams(self):
        for mode, row, reason in ((None, "F5", "end_turn"), ("length", "F5c", "max_tokens")):
            with self.subTest(mode=mode):
                reply, hit = self.request(row, stream=True, mode=mode)
                self.assertIs(hit.body["stream"], True)
                self.assertIs(dotted(hit.body, "stream_options.include_usage"), True)
                events = reply.events()
                self.assertEqual(events[0]["type"], "message_start")
                self.assertEqual(events[-1]["type"], "message_stop")
                self.assertIn("FAKE_OK", "".join(block.get("text", "") for block in reconstructed_blocks(reply)))
                self.assertEqual([e["delta"]["stop_reason"] for e in events if e["type"] == "message_delta"], [reason])

    def test_f5b_parallel_split_tool_calls(self):
        reply, _ = self.request("F5b", stream=True, mode="tool_calls_split")
        tools = [block for block in reconstructed_blocks(reply) if block["type"] == "tool_use"]
        self.assertEqual(len(tools), 2)
        for tool, suffix in zip(tools, ("a", "b")):
            self.assertEqual(tool["id"], "call_gwtest_" + suffix)
            self.assertEqual(tool["name"], "gwtest_tool_" + suffix)
            self.assertEqual(json.loads(tool["partial_json"]), {"k": "v"})
        events = reply.events()
        self.assertEqual([e["delta"]["stop_reason"] for e in events if e["type"] == "message_delta"], ["tool_use"])
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_f6_nonstream_stop_reasons(self):
        for mode, reason in ((None, "end_turn"), ("tool_calls", "tool_use"), ("length", "max_tokens")):
            with self.subTest(mode=mode):
                reply, _ = self.request("F6-" + (mode or "text"), mode=mode)
                message = reply.json()
                self.assertEqual(message["stop_reason"], reason)
                if mode == "tool_calls":
                    tools = [block for block in message["content"] if block["type"] == "tool_use"]
                    self.assertEqual(len(tools), 1)
                    self.assertEqual(tools[0]["input"], {"k": "v"})
                    self.assertEqual(tools[0]["name"], TOOL["name"])

    def test_f7_keyless_auth_and_f8_translator_retention_fact(self):
        reply, hit = self.request("F7", body={"prompt_cache_retention": "24h"})
        self.record("fidelity", "F8", reply, classification="translator fact")
        self.assertNotIn("authorization", hit.header_names)
        self.assertEqual(hit.header_values["user-agent"], "cli-proxy-openai-compat")
        self.assertNotIn("prompt_cache_retention", hit.top_level_keys)

    def test_f9_f11_unsigned_reasoning_round_trip(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                reply, _ = self.request(streamed_row("F9", stream), stream=stream, mode="reasoning")
                blocks = reconstructed_blocks(reply) if stream else reply.json()["content"]
                thinking = [block for block in blocks if block["type"] == "thinking"]
                self.assertEqual(len(thinking), 1)
                text = "gwtest-reasoning-" + reply.nonce
                self.assertEqual(thinking[0]["thinking"], text)
                self.assertFalse(thinking[0].get("signature"))
                self.record("reasoning_blocks", streamed_row("F9", stream), reply,
                            classification="observation", keys=sorted(thinking[0]))
                for alias, row, preserved in (("gwtest-compat-plain", "F10", False),
                                               ("gwtest-compat-iscompat", "F11", True)):
                    replay, hit = self.request(streamed_row(row, stream), alias=alias, stream=stream, body={"messages": [
                        {"role": "assistant", "content": thinking}, {"role": "user", "content": "continue"}]})
                    reasoning = [message["reasoning_content"] for message in hit.body["messages"] if "reasoning_content" in message]
                    self.assertEqual(reasoning, [text] if preserved else [])


class RetentionBoundaryTests(GatewayTests):
    def target(self):
        return self.alias("gwtest-cc", "output-config-high", levels=())

    def test_top_level_retention_stripped(self):
        for stream in (False, True):
            reply = self.gateway.request(self.target(), stream=stream, body={"prompt_cache_retention": "24h"})
            self.record("retention", streamed_row("R1", stream), reply, classification="patch discriminator")
            self.assertNotIn("prompt_cache_retention", self.hit(reply, self.target()).top_level_keys)

    def test_duplicate_top_level_retention_stripped(self):
        for stream in (False, True):
            payload = {"model": self.target(), "max_tokens": 64, "stream": stream,
                       "messages": [{"role": "user", "content": "gwtest-case-" + uuid.uuid4().hex[:12]}]}
            raw = ('{"prompt_cache_retention":"24h","prompt_cache_retention":"1h",' + json.dumps(payload)[1:]).encode()
            self.assertEqual(raw.count(b'"prompt_cache_retention"'), 2)
            reply = self.gateway.request(self.target(), raw=raw)
            self.record("retention", streamed_row("R2", stream), reply, classification="patch discriminator")
            self.assertNotIn("prompt_cache_retention", self.hit(reply, self.target()).top_level_keys)

    def test_cache_key_and_nested_retention_preserved(self):
        tool = copy.deepcopy(TOOL)
        nested = {"type": "string", "enum": ["24h", "1h"], "description": "nested fixture field"}
        tool["input_schema"]["properties"]["prompt_cache_retention"] = nested
        for stream in (False, True):
            reply = self.gateway.request(self.target(), stream=stream, body={"prompt_cache_key": "gwtest-pck", "tools": [tool]})
            self.record("retention", streamed_row("R3", stream), reply, classification="guard")
            hit = self.hit(reply, self.target())
            self.assertEqual(hit.body["prompt_cache_key"], "gwtest-pck")
            self.assertEqual(hit.body["tools"][0]["input_schema"]["properties"]["prompt_cache_retention"], nested)
            self.assertIn(json.dumps(nested).encode(), hit.raw)

    def test_codex_and_compat_are_translator_executor_facts(self):
        for alias in (self.alias("gwtest-codex", "reasoning-effort-high", levels=()), "gwtest-compat-plain"):
            for stream in (False, True):
                family = self.gateway.aliases[alias].family
                reply = self.gateway.request(alias, stream=stream, body={"prompt_cache_retention": "24h"})
                self.record("retention", streamed_row("R4-" + family, stream), reply, classification="translator/executor fact")
                self.assertNotIn("prompt_cache_retention", self.hit(reply, alias).top_level_keys)


class CompositionObservationTests(GatewayTests):
    def test_b081_nested_anthropic_base_tools_and_effort(self):
        alias = self.alias("gwtest-nested")
        for stream in (False, True):
            reply = self.gateway.request(alias, stream=stream, body={
                "tools": [TOOL],
                "thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}})
            hit = reply.hits[0] if len(reply.hits) == 1 else None
            tools_preserved = (hit is not None and len(hit.body.get("tools", [])) == 1
                               and all(hit.body["tools"][0].get(key) == value for key, value in TOOL.items()))
            desired = (reply.status == 200 and hit is not None
                       and hit.path == "/gwtest-nested/anthropic/v1/messages"
                       and hit.route == self.gateway.aliases[alias].route
                       and hit.query == "beta=true" and hit.body.get("model") == self.gateway.aliases[alias].wire
                       and hit.header_values.get("x-api-key") == "dummy-gwtest-nested"
                       and tools_preserved and "tool_choice" not in hit.body
                       and dotted(hit.body, "output_config.effort") == "high")
            self.record("observations", streamed_row("B081", stream), reply, classification="observation",
                        scenario_executed=True, desired_behavior_present=desired,
                        known_behavior_reproduced=desired, tools_preserved=tools_preserved)
            # A reproducible observation, not a claim that the harness repaired the gateway.
            hit = self.hit(reply, alias)
            self.assertTrue(hit.header_values.get("x-api-key") == "dummy-gwtest-nested")
            self.assertNotIn("tool_choice", hit.body)
            self.assertTrue(desired)

    def test_o2_b088_count_tokens_is_local_not_accuracy_evidence(self):
        alias = self.alias("gwtest-cc", "output-config-high", levels=())
        before = len(self.gateway.upstream.hits)
        reply = self.gateway.request(alias, path="/v1/messages/count_tokens")
        count = reply.json().get("input_tokens") if reply.status == 200 else None
        self.record("observations", "O2-B088", reply, classification="observation",
                    scenario_executed=True, desired_behavior_present=reply.status == 200 and not reply.hits,
                    known_behavior_reproduced=reply.status == 200 and not reply.hits,
                    input_tokens=count, upstream_delta=len(self.gateway.upstream.hits) - before,
                    provider_accuracy_proven=False, compaction_margin_proven=False)
        self.assertEqual(reply.status, 200)
        self.assertIs(type(count), int)
        self.assertGreater(count, 0)
        self.assertEqual(len(reply.hits), 0)
        self.assertEqual(len(self.gateway.upstream.hits), before)


def assert_static_route_aliases(case, bundle, key):
    """A line without a registry overlay is served by its static route: the
    real render's alias table on its pool lists exactly the line's effort
    selectors for its wire, and no overlay row names that wire (the
    not-applicable proof after the Sol revert)."""
    entry = bundle.lines[key]
    case.assertFalse(entry.get("registry_overlay"), f"{key}: a static route carries no registry overlay")
    docs = copy.deepcopy(bundle.docs)
    plan = operator.render_plan(docs, operator.empty_layer(), None)
    gateway_doc = copy.deepcopy(docs["gateway"])
    gateway_doc["gateway"]["base_url"] = "http://127.0.0.1:1"
    document, _available, _unavailable, _info = render.build_config_document(
        gateway_doc, plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"],
        home=Path("/nonexistent/probe-static-route"), gateway_token=harness.FIXTURE_GATEWAY_TOKEN,
        resolve_secret=lambda _name: "dummy-probe", continuity={}, oauth_overlay=plan.overlay,
        overlay_static=None)
    pool = bundle.providers[entry["provider"]]["transport"]["pool"]
    aliases = {row["alias"] for row in document["oauth-model-alias"].get(pool, ())
               if row["name"] == entry["wire_model"]}
    case.assertEqual(aliases, {item["selector"].removesuffix("[1m]") for item in entry["efforts"].values()},
                     f"{key}: the static route does not list its aliases")
    overlay = (document.get(render.OVERLAY_KEY) or {}).get(pool) or ()
    case.assertNotIn(entry["wire_model"], [row["name"] for row in overlay])


@uses_shipped_catalog
class SolOverlayCapabilityTests(unittest.TestCase):
    """T1 render -> both Go loaders -> OAuth registration/alias manager ->
    real HTTP executor and loopback capture, inside the matching diagnostic.
    The separately started candidate gateway proves its file-loader/listing
    path too. No TLS bypass or real OAuth provider connection is made.
    """
    def setUp(self):
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        self.sol = bundle.lines["sol"]
        self.applicable = self.sol["generation"] != "6"
        if not self.applicable:
            # Reverted: sol is back on GPT-6 and the Sol 6.1 overlay proof
            # does not apply. Never a skip here (any skip fails the
            # gateway check): the explicit not-applicable
            # proof is that the GPT-6 static route still lists its aliases.
            assert_static_route_aliases(self, bundle, "sol")
            return
        self.assertEqual(self.sol["registry_overlay"], {"channel": "codex"})
        self.assertEqual(set(self.sol["efforts"]), {"low", "medium", "high", "xhigh", "max"})

    def document(self, bundle, _egress, port, home, *, levels=True):
        docs = copy.deepcopy(bundle.docs)
        pools = {p["transport"]["pool"]: (key, p) for key, p in docs["providers"]["providers"].items()
                 if p["transport"]["kind"] == "oauth-pool"}
        claude_id, claude_provider = pools["claude"]
        codex_id, codex_provider = pools["codex"]
        sol = copy.deepcopy(self.sol)
        sol["provider"] = codex_id
        # The catalog owns Sonnet's real generation/floor. This synthetic T1 instance
        # exercises its client-effort overlay shape without publishing it.
        sonnet = copy.deepcopy(next(e for e in bundle.lines.values() if e["provider"] == claude_id))
        sonnet.update(wire_model="claude-probe-overlay-fixture", selector="claude-probe-overlay-fixture[1m]",
                      registry_overlay={"channel": "claude"}, efforts=list(sol["efforts"]),
                      output={"declared_tokens": 64000, "source": "operator"})
        claude_provider["passthrough_routes"] = [{"name": sonnet["wire_model"], "fork": True}]
        docs["providers"]["providers"] = {claude_id: claude_provider, codex_id: codex_provider}
        docs["models"] = docs["models-v2"] = {"version": 2, "models": {"sol": sol, "fixture-sonnet": sonnet}}
        docs["retired"] = {"version": 1, "retired": {}}
        plan = operator.render_plan(docs, operator.empty_layer(), None)
        gateway_doc = copy.deepcopy(docs["gateway"])
        gateway_doc["gateway"]["base_url"] = f"http://127.0.0.1:{port}"
        document, available, unavailable, info = render.build_config_document(
            gateway_doc, plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"], home=home,
            gateway_token=harness.FIXTURE_GATEWAY_TOKEN, resolve_secret=lambda _name: "dummy-probe",
            continuity={}, oauth_overlay=plan.overlay, overlay_static=None)
        self.assertFalse(unavailable)
        self.assertEqual(set(available), {claude_id, codex_id})
        for channel, entry in (("codex", sol), ("claude", sonnet)):
            rows = document[render.OVERLAY_KEY][channel]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["name"], entry["wire_model"])
            self.assertEqual(row["max-context-length"], entry["context"]["provider_tokens"])
            self.assertEqual(row["max-completion-tokens"], entry["output"]["declared_tokens"])
            self.assertEqual(row["thinking"]["levels"], [level for level in catalog.OVERLAY_THINKING_LEVELS if level in entry["efforts"]])
        if not levels:
            del document[render.OVERLAY_KEY]["codex"][0]["thinking"]
        document["disable-image-generation"] = True
        return document, {}, info

    def fixture(self, *, levels=True):
        # Paths exist only inside the diagnostic's temp HOME. The file and
        # payload loaders see identical bytes and capabilities.
        document, _, _ = self.document(catalog.load_catalog(FIXTURE_ROOT), 1, 2,
                                       Path("/nonexistent/probe-fixture"), levels=levels)
        aliases = {effort: item["selector"].removesuffix("[1m]") for effort, item in self.sol["efforts"].items()}
        spec = {"wire": self.sol["wire_model"], "aliases": aliases, "levels": levels,
                "channels": document[render.OVERLAY_KEY]}
        return {"config.yaml": render.emit_yaml(document).encode(), "spec.json": json.dumps(spec).encode()}

    def test_all_efforts_http_stream_and_tool_continuation(self):
        if not self.applicable:  # not applicable: asserted in setUp
            return
        binary = harness.run_codex_diagnostic("TestCodexProbeSolEfforts", fixture=self.fixture())
        harness.EVIDENCE.record(MODULE, "sol_overlay", {"row": "SO1", "diagnostic": binary,
            "efforts": 5, "paths": "HTTP,stream,tool-setup,tool-continuation", "summary": True, "encrypted_include": True})

    def test_missing_levels_strips_reasoning_summary(self):
        if not self.applicable:  # not applicable: asserted in setUp
            return
        binary = harness.run_codex_diagnostic("TestCodexProbeSolEfforts", fixture=self.fixture(levels=False))
        harness.EVIDENCE.record(MODULE, "sol_overlay", {"row": "SO2", "diagnostic": binary,
            "efforts": 5, "summary_stripped": True, "encrypted_include": True})

    def test_production_overlay_loader_parity_and_bare_aliases(self):
        if not self.applicable:  # not applicable: asserted in setUp
            return
        binary = harness.run_codex_diagnostic("TestCodexProbeOverlayLoaderParity", fixture=self.fixture())
        def factory(bundle, egress, port, home):
            document, aliases, info = self.document(bundle, egress, port, home)
            auth_dir = Path(document["auth-dir"])
            state.ensure_private_dir(auth_dir)
            for channel in ("claude", "codex"):
                state.atomic_write(auth_dir / (channel + "-probe.json"), json.dumps(
                    {"type": channel, "access_token": "dummy-probe", "plan_type": "pro"}).encode())
            return document, aliases, info
        gateway = harness.GatewayHarness(document_factory=factory)
        self.addCleanup(gateway.close)
        expected = {item["selector"].removesuffix("[1m]") for item in self.sol["efforts"].values()}
        expected.add("claude-probe-overlay-fixture")
        ids = set()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            ids = set()
            for headers in (gateway.headers(), {k: v for k, v in gateway.headers().items() if k != "anthropic-version"}):
                reply = harness.unix_request(gateway.socket, "GET", "/v1/models", headers=headers)
                self.assertEqual(reply.status, 200)
                ids |= {row["id"] for row in reply.json()["data"]}
            if expected <= ids:
                break
            time.sleep(0.05)
        self.assertTrue(expected <= ids, "candidate file loader omitted an overlay alias")
        self.assertEqual(gateway.upstream.hits, [])
        harness.EVIDENCE.record(MODULE, "sol_overlay", {"row": "SO3", "diagnostic": binary,
            "aliases": len(expected), "loader_parity": True, "provider_requests": 0})
