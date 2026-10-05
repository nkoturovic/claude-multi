"""The final native client on the real rendered keyed TLS gateway.

Only fixture-local metadata leaves memory; no native transcript is read.
"""
from __future__ import annotations

import copy
import gzip
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import _gateway_harness as harness
import test_gateway_keyed_compat as keyed_gateway
import test_openai_compat_keyed as keyed
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT
from test_gateway_hint_client import _GatewayRelay, _RelayHandler
from claude_multi import catalog, compiler, probe, profile, render, scope, settings, state, strict_json

MODULE = "tests.test_keyed_compat_client"
EVIDENCE = harness.Evidence()
SELECTED = set()
LEAD = "custom-keyedtest-lead"
AGENT = "custom-keyedtest-agent"
WORKFLOW = "custom-keyedtest-workflow"
FORBIDDEN = "custom-keyedtest-forbidden"
WIRES = {FORBIDDEN: "keyedtest-forbidden-wire", LEAD: "keyedtest-lead-wire", AGENT: "keyedtest-agent-wire", WORKFLOW: "keyedtest-workflow-wire"}


# Generation reaches the gateway only as Messages ingress.
GENERATION_INGRESS = frozenset({"/v1/messages", "/v1/messages/count_tokens"})
METADATA_TRANSPORT_METHODS = frozenset({"GET", "HEAD"})


def ingress_violations(rows, transport_rows):
    """Every non-Messages generation attempt, even one refused locally.

    ``rows`` are relayed Messages ingress; ``transport_rows`` are everything
    else. Only metadata-only GET/HEAD transport that never reached upstream is
    permitted, so a compact/image/SDK POST that 404s at the gateway before a
    successful Messages fallback is still a violation.
    """
    violations = ["ingress " + row["path"] for row in rows if row["path"] not in GENERATION_INGRESS]
    violations += [row["method"] + " " + row["route"] for row in transport_rows
                   if row["method"] not in METADATA_TRANSPORT_METHODS or row["upstream_hits"]]
    return violations


def load_tests(loader, tests, pattern):
    if harness.keyed_lane() == "unit":
        return unittest.TestSuite()
    SELECTED.add("client")
    return tests


def tearDownModule():
    harness.finalize_keyed(EVIDENCE, MODULE, SELECTED)


class RelayHandler(_RelayHandler):
    def do_POST(self):
        if self.path.split("?", 1)[0] not in ("/v1/messages", "/v1/messages/count_tokens"):
            return self.relay_transport()
        # Metadata parsing must not become a local fake answer. The auto-mode
        # client can send compressed JSON; forward the ORIGINAL bytes/headers.
        length = int(self.headers.get("Content-Length", 0))
        if not 0 <= length <= 16 * 1024 * 1024:
            raise AssertionError("native ingress exceeds fixture cap")
        raw = self.rfile.read(length)
        view = gzip.decompress(raw) if self.headers.get("Content-Encoding") == "gzip" else raw
        if len(view) > 16 * 1024 * 1024:
            raise AssertionError("decompressed native ingress exceeds fixture cap")
        try:
            document = json.loads(view)
        except (ValueError, UnicodeError):
            document = {}
        if not isinstance(document, dict):
            document = {}
        provider = self.server.provider
        context = provider._record(self.command, self.path, self.headers, raw, document)
        status, payload = provider._respond(document, self.path, context)
        if isinstance(payload, probe.SseResponse):
            self._send_sse(status, payload)
        else:
            self._send_json(status, payload)


class Relay(_GatewayRelay):
    def __init__(self, gateway):
        super().__init__(gateway)
        self.ingress = []

    def start(self):
        super().start()
        self._server.RequestHandlerClass = RelayHandler
        return self

    def unix_socket(self, directory):
        path = super().unix_socket(directory)
        self._unix_servers[path][0].RequestHandlerClass = RelayHandler
        return path

    def _observe(self, path, headers, hits, status, raw, document):
        self.ingress.append((path.split("?", 1)[0], copy.deepcopy(document)))
        self.rows.append({"path": path.split("?", 1)[0], "status": status, "upstream_hits": len(hits),
                          "thinking_type": (document.get("thinking") or {}).get("type"),
                          "effort": (document.get("output_config") or {}).get("effort"),
                          "model": document.get("model"), "stream": bool(document.get("stream"))})


def chat_reply(body, *, text="PROBE-OK", tool=None, reasoning=None, tokens=7):
    message = {"role": "assistant", "content": text}
    if tool is not None:
        name, arguments = tool
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_keyedtest_native",
            "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    finish = "tool_calls" if tool else "stop"
    usage = {"prompt_tokens": tokens, "completion_tokens": 3, "total_tokens": tokens + 3}
    base = {"id": "chatcmpl_keyedtest_native", "model": body["model"], "created": 1}
    if not body.get("stream"):
        return 200, "application/json", json.dumps({**base, "object": "chat.completion", "usage": usage,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}]}).encode()
    delta = {k: v for k, v in message.items() if k != "tool_calls"}
    if tool:
        delta["tool_calls"] = [{"index": 0, **message["tool_calls"][0]}]
    chunks = [{**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
              {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
              {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage}]
    return 200, "text/event-stream", b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


class Script:
    def __init__(self, mode="text", *, child="cm-reviewer"):
        self.mode, self.child = mode, child
        self.forced = False
        self.main = 0
        self.child_hits = 0

    def __call__(self, path, body, raw, headers):
        if not path.endswith("/chat/completions"):
            return 404, "application/json", b"{}"
        tools = {tool.get("function", {}).get("name") for tool in body.get("tools", [])}
        wire = body.get("model")
        if wire != WIRES[LEAD]:
            self.child_hits += 1
        main_request = wire == WIRES[LEAD] and "Agent" in tools
        if main_request:
            self.main += 1
        tool = None
        if not self.forced and wire == WIRES[LEAD]:
            if self.mode == "read" and "Read" in tools:
                tool = ("Read", {"file_path": self.read_path})
            elif self.mode == "agent" and "Agent" in tools:
                tool = ("Agent", {"description": "keyed child", "subagent_type": self.child,
                                  "prompt": "Return PROBE-OK"})
            elif self.mode == "workflow" and "Workflow" in tools:
                tool = ("Workflow", {"script": "export const meta = {name: 'keyedtest-workflow', description: 'keyed fixture child'}; const r = await agent('Return PROBE-OK', {label: 'keyed-child'}); return r;"})
            elif self.mode == "skill":
                tool = ("Skill", {"skill": "code-review", "args": "fixture"})
            if tool:
                self.forced = True
        tokens = 150000 if self.mode == "auto" and main_request and self.main == 3 else 165000 if self.mode == "auto" and main_request and self.main == 4 else 7
        return chat_reply(body, tool=tool, reasoning="keyedtest-native-transient-reasoning" if tool else None, tokens=tokens)


class KeyedClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SELECTED.add("client")
        keyed_gateway.prerequisites()
        contract = json.loads((SHIPPED_ROOT / "catalog/native-contract.json").read_text())
        cls.trusted = probe.trusted_from_contract(contract)
        if probe._sha256_file(cls.trusted.resolved_path) != cls.trusted.sha256:
            raise AssertionError("final native client missing or drifted")
        cls.contract = contract

    def setup_journey(self, mode="text", *, child="cm-reviewer", permission=None):
        root = Path(tempfile.mkdtemp(prefix="keyedtest-client-")).resolve()
        self.addCleanup(shutil.rmtree, root)
        self.ambient = {"HOME": str(root / "ambient"), "PATH": "/usr/bin:/bin"}
        self.client = probe.build_fixture(root / "client", environ=self.ambient)
        self.script = Script(mode, child=child)
        read_path = self.client.project_dir / "fixture.txt"
        state.atomic_write(read_path, b"PROBE-OK\n")
        self.script.read_path = str(read_path)
        declaration = keyed.keyed_document(key=LEAD, wire=WIRES[LEAD], efforts=keyed_gateway.ALL_LEVELS)
        lead = declaration["lines"][LEAD]
        lead["context"]["declared_tokens"] = 200000
        lead["family"] = "unknown"
        for key in (AGENT, WORKFLOW, FORBIDDEN):
            declaration["lines"][key] = {**copy.deepcopy(lead), "wire_model": WIRES[key],
                "capabilities": ["lead", "agents"], "roles": ["cm-reviewer"]}
        self.tls = keyed_gateway.KeyedTLS(files={keyed.KEYED_ID: declaration}, respond=self.script)
        self.addCleanup(self.tls.close)
        self.relay = Relay(self.tls.gateway).start()
        self.addCleanup(self.relay.stop)
        docs = copy.deepcopy(self.tls.plan.docs)
        docs["gateway"]["gateway"]["base_url"] = self.relay.base_url
        docs["native-contract"] = copy.deepcopy(self.contract)
        facts = {key: profile.AgentFacts(key=key, provider=keyed.KEYED_ID, admitted=True, route="approved",
            d60=True, route_kind="", family="unknown", t1_families=frozenset(), evidence="current",
            tools_variants=frozenset({"forced"})) for key in (AGENT, WORKFLOW)}
        gate = profile.AgentGate(facts=facts)
        cat = profile.LineupCatalog.from_docs(docs, agent_gate=gate)
        effective = settings.effective({"version": 1, "admitted_lines": [LEAD, AGENT, WORKFLOW],
            "workflow_default_binding": {"model": WORKFLOW, "effort": "high"}}, provider_ids=cat.providers,
            line_keys=cat.lines)
        document = {"version": 2, "name": "keyed-probe", "lead": {"model": LEAD, "effort": "high"},
            "agents": {"cm-reviewer": {"model": AGENT, "effort": "max"}}, "workflows": "native",
            "native_agents": {"explore": "native", "plan": "native", "general_purpose": "on"}}
        lineup = profile.resolve(document, cat, effective=effective)
        self.session = str(uuid.uuid4())
        root_state = state.ensure_private_dir(self.client.root / "state")
        helper = root_state / "token-helper"
        state.atomic_write(helper, b"#!/bin/sh\nprintf '%s\\n' probe-dummy-token\n")
        helper.chmod(0o700)
        recorder = state.ensure_private_dir(root_state / "bin") / "claude-multi-hook-3"
        self.events = root_state / "events.jsonl"
        recorder.write_text(f"#!{sys.executable}\nimport json,sys\ne=json.load(sys.stdin)\n"
            f"with open({str(self.events)!r},'a') as f: f.write(json.dumps({{k:e.get(k) for k in "
            "('hook_event_name','session_id','source')})+'\\n')\nprint('{}')\n")
        recorder.chmod(0o700)
        self.launch = compiler.compile_lineup_launch(docs=docs, prompt_bodies=catalog.load_catalog(FIXTURE_ROOT).prompt_bodies,
            lineup=lineup, effective=effective, session_action=compiler.build_fresh(self.session), lineup_generation=1,
            state_root=root_state, scope_dir=root_state / "scopes" / self.session, hook_command=shlex.quote(str(recorder)),
            token_helper_command=shlex.quote(str(helper)), agent_gate=gate, default_mode=scope.PERMISSION_DEFAULT_MODE,
            worktree_available=False, session_cwd=self.client.project_dir)
        scope.write_scope(root_state, self.session, self.launch.scope_plan)
        state.atomic_write(self.launch.lead_prompt_path, self.launch.lead_prompt.encode())
        self.assertIn(self.tls.plan.layer.lines[AGENT].core_entry["selector"], self.launch.fence.available_models)
        probe.seed_fixture_project_trust(self.client, self.client.project_dir)
        probe.seed_fixture_project_trust(self.client, self.launch.scope_dir)
        if permission:
            state.atomic_write(self.client.project_dir / ".claude/settings.local.json",
                               strict_json.canonical_file_bytes({"permissions": {"defaultMode": permission}}))
        return root

    def run_headless(self, *, permission="bypassPermissions"):
        args = [*self.launch.argv, "-p", "keyed isolated test; return PROBE-OK", "--output-format", "json"]
        if permission:
            args += ["--permission-mode", permission]
        result = probe.run_native(args, trusted=self.trusted, fixture=self.client, provider=self.relay,
                                  timeout=120, allow_real=True, environ=self.ambient, token=None)
        self.assertFalse(result.timed_out, "native client timed out")
        self.assertEqual(result.returncode, 0, "native client did not complete")
        self.assertTrue(result.daemon and result.daemon.unchanged)
        self.assertFalse(self.relay.completion_errors(), "native relay accounting incomplete")
        self.assertTrue(any(row["model"] == LEAD and row["status"] == 200 for row in self.relay.rows))
        return result

    def record(self, row, **fields):
        if not self._outcome.success:
            raise AssertionError("failed test cannot record passed evidence")
        EVIDENCE.binary = dict(harness.EVIDENCE.binary)
        EVIDENCE.record(MODULE, "client", {"row": row, "status": "passed", "client_sha256": self.trusted.sha256,
            "thinking_type_present": any(row["thinking_type"] is not None for row in self.relay.rows
                                         if row["model"] in (LEAD, AGENT, WORKFLOW)),
            "requests": len(self.relay.rows), **fields})

    def test_final_client_keyed_lead_tool_stream_completion(self):
        self.setup_journey("read")
        self.run_headless()
        self.assertTrue(self.script.forced)
        self.assertTrue(any(row["stream"] for row in self.relay.rows if row["model"] == LEAD))
        self.assertTrue(any(any(message.get("role") == "tool" for message in hit.body["messages"])
                            for hit in self.tls.gateway.upstream.hits))
        self.record("C1-lead", tool_continuation=True)

    def test_final_client_keyed_returned_history_replay(self):
        self.setup_journey("read")
        self.run_headless()
        self.assertTrue(self.script.forced)
        contents = [block for _path, document in self.relay.ingress for message in document.get("messages", [])
                    if message.get("role") == "assistant" and isinstance(message.get("content"), list)
                    for block in message["content"]]
        self.assertTrue(any(block.get("type") == "tool_use" and block.get("id") == "call_keyedtest_native" for block in contents))
        self.assertTrue(any(block.get("type") == "thinking" for block in contents))
        self.record("C1-replay", returned_tool_history=True, returned_thinking_history=True)

    def test_final_client_keyed_agent_dispatch_never_lead_fallback(self):
        self.setup_journey("agent")
        self.run_headless()
        self.assertTrue(self.script.forced)
        self.assertTrue(any(hit.body.get("model") == WIRES[AGENT] for hit in self.tls.gateway.upstream.hits))
        self.assertTrue(any(row["model"] == AGENT and row["effort"] == "max" for row in self.relay.rows))
        self.assertTrue(all(hit.body.get("reasoning_effort") == "max" for hit in self.tls.gateway.upstream.hits
                            if hit.body.get("model") == WIRES[AGENT]))
        self.record("C1-agent", distinct_wire=True, intended_effort="max")


    def test_final_client_keyed_workflow_child_and_fence(self):
        self.setup_journey("workflow")
        self.run_headless()
        self.assertTrue(self.script.forced)
        self.assertTrue(any(hit.body.get("model") == WIRES[WORKFLOW] for hit in self.tls.gateway.upstream.hits))
        self.assertTrue(any(row["model"] == WORKFLOW and row["effort"] == "high" for row in self.relay.rows))
        self.assertTrue(all(hit.body.get("reasoning_effort") == "high" for hit in self.tls.gateway.upstream.hits
                            if hit.body.get("model") == WIRES[WORKFLOW]))
        self.record("C1-workflow", workflow_distinct_wire=True, intended_effort="high")
        # A user-authored out-of-fence model is substituted with the
        # compiled Workflow default, never the forbidden wire or the lead.
        # Production bound agents cannot compile with such a fence gap.
        for permission in (None, "bypassPermissions"):
            with self.subTest(permission=permission):
                self.setup_journey("agent", child="keyedtest-forbidden-agent")
                self.assertNotIn(FORBIDDEN, self.launch.fence.available_models)
                self.assertIn(FORBIDDEN, render.rendered_selectors(self.tls.gateway.document))
                agents = state.ensure_private_dir(self.launch.scope_dir / ".claude/agents")
                state.atomic_write(agents / "keyedtest-forbidden-agent.md", b"---\nname: keyedtest-forbidden-agent\n"
                    b"description: forbidden fixture child\nmodel: custom-keyedtest-forbidden\n---\n"
                    b"KEYEDTEST-FORBIDDEN-CHILD-SYSTEM. Return PROBE-OK.\n")
                self.run_headless(permission=permission)
                self.assertTrue(self.script.forced)
                self.assertEqual(self.script.child_hits, 1)
                self.assertFalse(any(hit.body.get("model") == WIRES[FORBIDDEN]
                                     for hit in self.tls.gateway.upstream.hits))
                children = [doc for _, doc in self.relay.ingress
                            if "KEYEDTEST-FORBIDDEN-CHILD-SYSTEM" in json.dumps(doc.get("system"))]
                self.assertEqual([doc["model"] for doc in children], [WORKFLOW])
                self.assertEqual([hit.body["model"] for hit in self.tls.gateway.upstream.hits
                                  if hit.body.get("model") != WIRES[LEAD]], [WIRES[WORKFLOW]])
        self.record("C1-fence-fallback", permission_modes=2, forbidden_wire_hits=0,
                    lead_wire_child_hits=0, workflow_default_child_hits=1)

    def test_final_client_keyed_skill_denies_and_fast_mode_policy(self):
        self.setup_journey("skill")
        skill_dir = state.ensure_private_dir(self.client.project_dir / ".claude/skills/code-review")
        state.atomic_write(skill_dir / "SKILL.md", b"---\nname: code-review\ndescription: fixture spawning skill\n"
            b"context: fork\nagent: cm-reviewer\n---\nReturn PROBE-OK.\n")
        self.assertIn("Skill(code-review)", self.launch.scope_plan.settings["permissions"]["deny"])
        self.run_headless()
        self.assertTrue(self.script.forced)
        self.assertEqual(self.script.child_hits, 0)
        self.assertTrue(all(hit.body.get("speed") != "fast" for hit in self.tls.gateway.upstream.hits))
        self.assertEqual(self.launch.scope_plan.settings["env"]["CLAUDE_CODE_DISABLE_FAST_MODE"], "1")
        self.record("C1-policy", spawning_skill_child_hits=0, fast_speed=False)

    def test_final_client_keyed_default_and_explicit_auto_policy(self):
        observed = []
        for permission in (None, "auto"):
            self.setup_journey("agent")
            self.assertEqual(self.launch.scope_plan.settings["permissions"]["defaultMode"], "default")
            self.run_headless(permission=permission)
            self.assertTrue(self.script.forced)
            observed.append(self.script.child_hits)
        self.assertGreater(observed[0], 0)
        self.assertEqual(observed[1], 0)
        self.record("C1-permission", default_child_dispatched=True, explicit_auto_child_hits=0)

    def compact(self, automatic):
        self.setup_journey("auto" if automatic else "text")
        prompt = "❯".encode()
        interactions = [probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
                        probe.PTYInteraction(prompt, b"COMPACTION-SEED-A\r"),
                        probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                        probe.PTYInteraction(prompt, b"COMPACTION-SEED-B\r"),
                        probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True)]
        if automatic:
            large = " ".join(f"probe-token-{i:05d}" for i in range(4000))[:60000].encode()
            interactions += [probe.PTYInteraction(prompt, b"\x1b[200~" + large + b"\x1b[201~\r"),
                             probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                             probe.PTYInteraction(prompt, b"COMPACTION-BRIDGE\r"),
                             probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                             probe.PTYInteraction(prompt, b"COMPACTION-TRIGGER\r"),
                             probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True)]
        else:
            interactions += [probe.PTYInteraction(prompt, b"/compact\r"),
                             probe.PTYInteraction(b"Compacted", b"", preserve_after_wait=True),
                             probe.PTYInteraction(prompt, b"COMPACTION-CONTINUE\r"),
                             probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True)]
        interactions.append(probe.PTYInteraction(prompt, b"/exit\r"))
        result = probe.run_native_pty(self.launch.argv, interactions, trusted=self.trusted, fixture=self.client,
            provider=self.relay, timeout=180, allow_real=True, environ=self.ambient, token=None)
        self.assertFalse(result.timed_out, "actual compaction timed out")
        self.assertEqual(result.returncode, 0, "actual compaction did not complete")
        self.assertTrue(result.daemon and result.daemon.unchanged)
        self.assertFalse(self.relay.completion_errors())
        events = [json.loads(line) for line in self.events.read_text().splitlines()]
        compact = [event for event in events if event.get("source") == "compact"]
        self.assertTrue(compact, "no metadata lifecycle compaction event")
        paths = [hit.path for hit in self.tls.gateway.upstream.hits]
        self.assertTrue(paths)
        violations = ingress_violations(self.relay.rows, self.relay.transport_rows)
        violations += ["upstream " + path for path in paths if not path.endswith("/chat/completions")]
        self.assertEqual(violations, [], "compaction used non-Messages generation ingress")
        self.assertTrue(all(row["upstream_hits"] == 0 for row in self.relay.rows if row["path"].endswith("/count_tokens")))
        self.assertGreaterEqual(self.script.main, 5 if automatic else 3)
        self.record("C2-auto" if automatic else "C2-manual", compact_events=len(compact),
                    genuine_compact_image_sdk_hits=len(violations),
                    count_tokens_ingress=sum(row["path"].endswith("/count_tokens") for row in self.relay.rows),
                    transport_requests=len(self.relay.transport_rows))

    def test_manual_compaction_uses_messages_chat_only(self):
        self.compact(False)

    def test_automatic_compaction_uses_messages_chat_only(self):
        self.compact(True)


class IngressRouteSelfTests(unittest.TestCase):
    """No native client: a refused non-Messages POST cannot pass as C2."""

    compact = KeyedClientTests.compact

    def setup_journey(self, mode):
        from types import SimpleNamespace

        temp = Path(tempfile.mkdtemp(prefix="keyedtest-ingress-selftest-"))
        self.addCleanup(shutil.rmtree, temp, True)
        self.events = temp / "events.jsonl"
        self.events.write_text(json.dumps({"source": "compact"}) + "\n")
        hit = SimpleNamespace(path="/v1/chat/completions")
        self.tls = SimpleNamespace(gateway=SimpleNamespace(upstream=SimpleNamespace(hits=[hit] * 6)))
        self.script = SimpleNamespace(main=6)
        self.launch = SimpleNamespace(argv=["claude"])
        self.trusted = self.client = self.ambient = None
        self.relay = SimpleNamespace(completion_errors=lambda: [], transport_rows=list(self.transport),
            rows=[{"path": "/v1/messages", "status": 200, "upstream_hits": 1} for _ in range(6)])

    def record(self, row, **fields):
        self.recorded = (row, fields)

    def run_compact(self, transport):
        from types import SimpleNamespace
        from unittest import mock

        self.transport, self.recorded = transport, None
        result = SimpleNamespace(timed_out=False, returncode=0, daemon=SimpleNamespace(unchanged=True))
        with mock.patch.object(probe, "run_native_pty", return_value=result):
            self.compact(False)
        return self.recorded

    def test_metadata_transport_is_allowed(self):
        row, fields = self.run_compact([{"method": "GET", "route": "/v1/models", "status": 200, "upstream_hits": 0}])
        self.assertEqual((row, fields["genuine_compact_image_sdk_hits"]), ("C2-manual", 0))

    def test_forbidden_post_before_messages_fallback_fails(self):
        for route in ("/v1/responses/compact", "/v1/images/generations", "/v1/responses", "/v1/complete"):
            refused = {"method": "POST", "route": route, "status": 404, "upstream_hits": 0}
            with self.subTest(route=route), self.assertRaises(AssertionError):
                self.run_compact([refused])
            self.assertIsNone(self.recorded)
        self.assertTrue(ingress_violations([{"path": "/v1/complete"}], []))
        self.assertTrue(ingress_violations([], [{"method": "GET", "route": "/v1/x", "upstream_hits": 1}]))
