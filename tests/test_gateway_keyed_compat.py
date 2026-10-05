"""Keyed chat evidence: validated declarations -> renderer -> isolated TLS.

The key values, bodies and reasoning are transient fixture data. Required host
proofs fail on missing prerequisites; the non-gateway Nix lane selects units,
never skips an unavailable proof. No live endpoints or provider state.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _gateway_harness as harness
import test_openai_compat_keyed as keyed
from claude_multi import catalog, operator, probe, qualify, render, state


KEYS = {keyed.KEYED_SECRET: "keyedtest-key-first-dummy", keyed.OTHER_SECRET: "keyedtest-key-second-dummy"}
ALL_LEVELS = ("low", "medium", "high", "xhigh", "max")
MODULE = "tests.test_gateway_keyed_compat"
EVIDENCE = harness.Evidence()
SELECTED = set()


def record(test, table, row, **fields):
    if not test._outcome.success:
        raise AssertionError("failed test cannot record passed evidence")
    EVIDENCE.binary = dict(harness.EVIDENCE.binary)
    EVIDENCE.record(MODULE, table, {"row": row, "status": "passed", **fields})


def tearDownModule():
    harness.finalize_keyed(EVIDENCE, MODULE, SELECTED)


def load_tests(loader, tests, pattern):
    if harness.keyed_lane() == "unit":
        return unittest.TestSuite()
    SELECTED.update(("core", "audit"))
    return tests



def prerequisites():
    """A required proof must fail, never turn missing isolation into a skip."""
    if not os.environ.get(harness.BINARY_ENV):
        raise AssertionError("explicit candidate gateway is required")
    binary, reason = harness.find_gateway_binary()
    if binary is None:
        raise AssertionError("candidate gateway unavailable: " + reason)
    reason = probe.network_isolation_available()
    if reason:
        raise AssertionError("network isolation unavailable: " + reason)
    openssl = shutil.which("openssl")
    if openssl is None:
        raise AssertionError("TLS certificate tool unavailable")
    return openssl


class TLSUpstream(harness.FakeUpstream):
    """Raw TCP bridge terminates TLS, without a CONNECT/proxy escape hatch."""

    def __init__(self, path, cert, key, respond=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        super().__init__(path, respond=respond)
        accept = self.server.get_request

        def get_request():
            connection, address = accept()
            try:
                return context.wrap_socket(connection, server_side=True), address
            except BaseException:
                connection.close()
                raise

        self.server.get_request = get_request


class KeyedTLS:
    """Shared E2b/E2c fixture; model metadata is never patched after render."""

    def __init__(self, *, files=None, values=None, captures=None, management=False, respond=None):
        openssl = prerequisites()
        self.root = Path(tempfile.mkdtemp(prefix="keyedtest-tls-")).resolve()
        state.ensure_private_dir(self.root)
        self.gateway = None
        self.values = dict(KEYS if values is None else values)
        self.files = files
        self.holder = {}
        try:
            cert, key = self.root / "ca.pem", self.root / "key.pem"
            result = subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                                     "-subj", "/CN=keyedtest.invalid", "-addext", "subjectAltName=IP:127.0.0.1",
                                     "-keyout", str(key), "-out", str(cert)],
                                    capture_output=True, timeout=30)
            if result.returncode:
                raise AssertionError("TLS certificate generation failed")
            original = probe.start_isolated_process

            def start(argv, **kwargs):
                if "trust" in self.holder:
                    kwargs["env"] = {**kwargs["env"], "SSL_CERT_FILE": self.holder["trust"]}
                return original(argv, **kwargs)

            def document(bundle, egress, port, home):
                docs = keyed.fixture_docs(audited=True)
                docs["gateway"]["gateway"]["base_url"] = f"http://127.0.0.1:{port}"
                # Keep the audited asset vocabulary, but no unrelated fixture routes.
                docs["providers"]["providers"] = {}
                docs["models-v2"]["models"] = {}
                docs["models"] = docs["models-v2"]
                docs["retired"]["models"] = {}
                base = f"https://127.0.0.1:{egress}/keyedtest/v1"
                files = self.files or {
                    keyed.KEYED_ID: keyed.keyed_document(base_url=base, efforts=ALL_LEVELS),
                    "otherchat": keyed.keyed_document(base_url=base + "/second", secret=keyed.OTHER_SECRET,
                        key="custom-second-chat", wire="keyedtest-second-wire", efforts=("high", "max")),
                }
                if self.files is None:
                    first = files[keyed.KEYED_ID]["lines"]
                    first["custom-lowhigh-chat"] = keyed.keyed_document(key="custom-lowhigh-chat",
                        wire="keyedtest-lowhigh-wire", efforts=("low", "high"))["lines"]["custom-lowhigh-chat"]
                # The namespace URL is assigned BEFORE validation and route approval.
                files = copy.deepcopy(files)
                for index, declaration in enumerate(files.values()):
                    declaration["provider"]["base_url"] = base + (f"/route{index}" if index else "")
                self.plan, self.ledger = keyed.keyed_plan(files, docs=docs, captures=captures)
                if self.plan.layer.problems:
                    raise AssertionError("fixture declaration failed validation")
                self.files_resolved = files
                self.docs = docs
                result = self.render(home)
                trust = home / ".config/claude-multi/keyedtest-ca.pem"
                state.atomic_write(trust, cert.read_bytes())
                self.holder["trust"] = str(trust)
                return result, {}, {}

            with mock.patch.object(probe, "start_isolated_process", start):
                self.gateway = harness.GatewayHarness(document_factory=document, management=management,
                    upstream_factory=lambda path: TLSUpstream(path, cert, key, respond=respond))
        except BaseException:
            self.close()
            raise

    def render(self, home):
        document, _available, unavailable, _info = render.build_config_document(self.plan.docs["gateway"], self.plan.docs["providers"]["providers"],
            self.plan.docs["models-v2"]["models"], home=home, gateway_token=harness.FIXTURE_GATEWAY_TOKEN,
            resolve_secret=self.values.get, continuity={}, captures=self.plan.captures,
            provider_headers=self.plan.headers, oauth_overlay=self.plan.overlay)
        if unavailable:
            raise AssertionError("unexpected unavailable rendered provider")
        return document

    def close(self):
        if self.gateway is not None:
            self.gateway.close()
            self.gateway = None
        shutil.rmtree(self.root)


class KeyedGatewayCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SELECTED.add("core")
        cls.started = time.monotonic()
        cls.fixture = KeyedTLS()
        cls.gateway = cls.fixture.gateway

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()
        print(f"keyed core elapsed: {time.monotonic() - cls.started:.3f}s")

    def test_validated_approved_renderer_loads_through_tls(self):
        gateway = self.gateway
        self.assertTrue(all(status == "approved" for status in self.fixture.plan.layer.route_status.values()))
        self.assertTrue(all(line.core_entry["status"] == "new" for line in self.fixture.plan.layer.lines.values()))
        reply = gateway.request(keyed.KEYED_KEY)
        self.assertEqual(reply.status, 200)
        self.assertEqual(len(reply.hits), 1)
        self.assertEqual(reply.hits[0].path, "/keyedtest/v1/chat/completions")
        self.assertTrue(all(p.base_url.startswith("https://127.0.0.1:") for p in self.fixture.plan.layer.providers.values()))
        self.assertEqual(reply.json()["model"], keyed.KEYED_KEY)
        self.assertFalse(bool(gateway.document.get("proxy-url")))
        record(self, "core", "K1-render", tls=True, upstream_hits=1)

    def test_two_provider_auth_wire_pairing_and_force_mapping(self):
        for alias, wire, name in ((keyed.KEYED_KEY, keyed.KEYED_WIRE, keyed.KEYED_SECRET),
                                  ("custom-second-chat", "keyedtest-second-wire", keyed.OTHER_SECRET)):
            for stream in (False, True):
                with self.subTest(alias=alias, stream=stream):
                    reply = self.gateway.request(alias, stream=stream)
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(len(reply.hits), 1)
                    hit = reply.hits[0]
                    self.assertEqual(hit.body["model"], wire)
                    self.assertTrue(hit.header_values.get("authorization") == "Bearer " + self.fixture.values[name])
                    downstream = reply.events()[0]["message"] if stream else reply.json()
                    self.assertEqual(downstream["model"], alias)
        record(self, "core", "K1-pairing", cases=4, credential_match=True)

    def test_keyed_thinking_forms_suffix_defaults_and_clamp(self):
        # Frozen K12 predictions: unlike capability sets have distinct wires.
        for alias, levels in ((keyed.KEYED_KEY, ALL_LEVELS), ("custom-second-chat", ("high", "max")),
                              ("custom-lowhigh-chat", ("low", "high"))):
            cells = [("absent", {}, None), ("adaptive-default", {"thinking": {"type": "adaptive"}},
                      "xhigh" if len(levels) == 5 else "max" if "max" in levels else "high"),
                     ("disabled", {"thinking": {"type": "disabled"}}, levels[0]),
                     ("zero", {"thinking": {"type": "enabled", "budget_tokens": 0}}, levels[0]),
                     ("auto", {"thinking": {"type": "enabled", "budget_tokens": -1}},
                      "medium" if len(levels) == 5 else levels[0])]
            for level in ALL_LEVELS:
                expected = (level if len(levels) == 5 else ("max" if level in ("xhigh", "max") else "high")
                            if "max" in levels else "low" if level in ("low", "medium") else "high")
                cells.append((level, {"thinking": {"type": "adaptive"}, "output_config": {"effort": level}}, expected))
            for budget, level in ((1, "low"), (512, "low"), (513, "low"), (1024, "low"),
                                  (1025, "medium"), (8192, "medium"), (8193, "high"), (24576, "high"),
                                  (24577, "xhigh"), (32768, "xhigh")):
                expected = (level if len(levels) == 5 else "high" if "max" in levels
                            else "low" if level in ("low", "medium") else "high")
                cells.append(("budget-" + str(budget), {"thinking": {"type": "enabled", "budget_tokens": budget}}, expected))
            for form in ({"thinking": {"type": "enabled"}},
                         {"thinking": {"type": "adaptive"}, "output_config": {"effort": "auto"}}):
                cells.append(("auto-form", form, "medium" if len(levels) == 5 else levels[0]))
            for stream in (False, True):
                for label, body, expected in cells:
                    with self.subTest(alias=alias, stream=stream, cell=label):
                        reply = self.gateway.request(alias, stream=stream, body=body)
                        self.assertEqual(reply.status, 200)
                        self.assertEqual(len(reply.hits), 1)
                        observed = reply.hits[0].body
                        if expected is None:
                            self.assertNotIn("reasoning_effort", observed)
                        else:
                            self.assertEqual(observed.get("reasoning_effort"), expected)
                for suffix, expected in (("high", "high"), ("none", levels[0]), ("0", levels[0]),
                                         ("auto", "medium" if len(levels) == 5 else levels[0]),
                                         ("-1", "medium" if len(levels) == 5 else levels[0])):
                    with self.subTest(alias=alias, stream=stream, suffix=suffix):
                        reply = self.gateway.request(alias + "(" + suffix + ")", stream=stream,
                            body={"thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}})
                        self.assertEqual(reply.status, 200)
                        self.assertEqual(len(reply.hits), 1)
                        self.assertEqual(reply.hits[0].body.get("reasoning_effort"), expected)
                with self.subTest(alias=alias, stream=stream, cell="unknown-level"):
                    reply = self.gateway.request(alias, stream=stream,
                        body={"thinking": {"type": "adaptive"}, "output_config": {"effort": "ultra"}})
                    self.assertEqual(reply.status, 400)
                    self.assertEqual(len(reply.hits), 0)
        record(self, "core", "K12", capability_sets=3, streaming_modes=2, suffix_wins=True,
               suffix_cells=30, unknown_level_cells=6, unknown_level_hits=0)

    def test_keyed_stream_terminal_usage_and_local_count_tokens(self):
        reply = self.gateway.request(keyed.KEYED_KEY, stream=True)
        self.assertEqual(reply.status, 200)
        self.assertEqual(qualify.classify_stream(qualify.HttpResult(reply.status, reply.raw)), ("pass", "ok"))
        self.assertEqual(reply.events()[-1]["type"], "message_stop")
        count = self.gateway.request(keyed.KEYED_KEY, path="/v1/messages/count_tokens")
        self.assertEqual(count.status, 200)
        self.assertEqual(len(count.hits), 0)
        self.assertIsInstance(count.json()["input_tokens"], int)
        scenario = {}

        def respond(path, body, raw, headers):
            base = {"id": "keyedtest-stream", "model": body["model"], "object": "chat.completion.chunk"}
            def frame(delta, finish=None):
                return b"data: " + json.dumps({**base, "choices": [{"index": 0, "delta": delta,
                    "finish_reason": finish}]}).encode() + b"\n\n"
            content = frame({"role": "assistant", "content": "fixture"})
            finished = frame({}, scenario["finish"])
            usage = b"data: " + json.dumps({**base, "choices": [], "usage": {
                "prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22}}).encode() + b"\n\n"
            mode = scenario["mode"]
            chunks = [(0, content + finished)]
            if mode == "delayed-usage":
                chunks.append((0.03, usage))
            if mode in ("delayed-usage", "missing-usage"):
                chunks.append((0, b"data: [DONE]\n\n"))
            elif mode == "malformed":
                chunks.append((0, b"data: {broken\n\n"))
            elif mode == "premature":
                chunks = [(0, content)]
            return 200, "text/event-stream", harness.ChunkedBody(tuple(chunks),
                missing_bytes=1 if mode == "premature" else 0)

        with_fixture = KeyedTLS(respond=respond)
        self.addCleanup(with_fixture.close)
        cases = 0
        for mode in ("delayed-usage", "missing-usage", "clean-missing-done", "premature", "malformed"):
            for finish, reason in (("stop", "end_turn"), ("length", "max_tokens")):
                with self.subTest(mode=mode, finish=finish):
                    scenario.update(mode=mode, finish=finish)
                    reply = with_fixture.gateway.request(keyed.KEYED_KEY, stream=True)
                    self.assertEqual(len(reply.hits), 1)
                    verdict = qualify.classify_stream(qualify.HttpResult(reply.status, reply.raw))
                    if mode in ("premature", "malformed"):
                        self.assertEqual(verdict, ("failed", "stream-error"))
                        self.assertNotIn("message_stop", [e["type"] for e in reply.events()])
                    else:
                        self.assertEqual(verdict, ("pass", "ok"))
                        events = reply.events()
                        self.assertEqual(events[-1]["type"], "message_stop")
                        self.assertEqual(sum(e["type"] == "message_stop" for e in events), 1)
                        delta, = [e for e in events if e["type"] == "message_delta"]
                        self.assertEqual(delta["delta"]["stop_reason"], reason)
                        # The gateway uses a local input estimate; only output
                        # usage is asserted against the fake provider's count.
                        self.assertEqual(delta["usage"]["output_tokens"], 5 if mode == "delayed-usage" else 0)
                    cases += 1
        record(self, "core", "S1", generation_hits=1, count_tokens_hits=0, terminal_cases=cases,
               delayed_usage=True, missing_usage_output_zero=True, clean_missing_done_accepted=True,
               premature_disconnect_failed=True, malformed_frame_failed=True)

    def test_keyed_key_reload_changes_selected_credential(self):
        gateway = self.gateway
        before = render.document_sentinel(gateway.document)
        old = self.fixture.values[keyed.KEYED_SECRET]
        try:
            self.fixture.values[keyed.KEYED_SECRET] = "keyedtest-key-replacement-dummy"
            result = self.fixture.render(gateway.home)
            self.assertNotEqual(render.document_sentinel(result), before)
            gateway.document = result
            state.atomic_write(gateway.config, render.emit_yaml(result).encode())
            gateway.await_ready()
            reply = gateway.request(keyed.KEYED_KEY)
            self.assertEqual(reply.status, 200)
            self.assertTrue(reply.hits[0].header_values.get("authorization") == "Bearer " + self.fixture.values[keyed.KEYED_SECRET])
            self.assertFalse(reply.hits[0].header_values.get("authorization") == "Bearer " + old)
            record(self, "core", "A1-reload", sentinel_changed=True, credential_version_match=True)
        finally:
            self.fixture.values[keyed.KEYED_SECRET] = old
            result = self.fixture.render(gateway.home)
            gateway.document = result
            state.atomic_write(gateway.config, render.emit_yaml(result).encode())
            gateway.await_ready()


class KeyedGatewayAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SELECTED.add("audit")
        prerequisites()

    def fixture(self, **kwargs):
        fixture = KeyedTLS(**kwargs)
        self.addCleanup(fixture.close)
        return fixture

    def test_keyed_missing_blank_keys_and_captured_only_render(self):
        # Missing and unusable keys omit every alias, never a keyless fallback.
        plan, _ = keyed.keyed_plan({keyed.KEYED_ID: keyed.keyed_document()})
        for value in (None, "", " ", 42, "bad\nkey"):
            result = keyed.render_plan_config(plan, lambda _name: value)
            self.assertNotIn(KEYS[keyed.KEYED_SECRET], result.yaml)
            self.assertNotIn(keyed.KEYED_KEY, result.yaml)
        for same_wire in (False, True):
            files = {keyed.KEYED_ID: keyed.keyed_document()}
            captures = {"custom-captured-chat": {"pid": keyed.KEYED_ID,
                "wire": keyed.KEYED_WIRE if same_wire else "keyedtest-captured-wire", "levels": ("low", "high")}}
            fixture = self.fixture(files=files, captures=captures)
            for alias in (keyed.KEYED_KEY, "custom-captured-chat"):
                reply = fixture.gateway.request(alias)
                self.assertEqual(reply.status, 200)
                self.assertEqual(len(reply.hits), 1)
        # Zero current models, the approved capture alone still serves.
        files[keyed.KEYED_ID]["lines"] = {}
        fixture = self.fixture(files=files, captures=captures)
        self.assertEqual(fixture.gateway.request("custom-captured-chat").status, 200)
        record(self, "audit", "K1-omissions", unusable_keys=5, captured_only=True, shared_wire_sibling=True)

    def test_final_p1_cr1_ks1_discriminators_match_candidate(self):
        for name in ("TestOmissionCompatRetention", "TestOmissionCredentialedRedirect",
                     "TestOmissionKeyedSafety"):
            harness.run_codex_diagnostic(name, package="executor")
        record(self, "audit", "P1-CR1-KS1", matching_final_discriminators=3)

    def test_client_headers_cannot_replace_configured_route(self):
        fixture = self.fixture()
        for stream in (False, True):
            reply = fixture.gateway.request(keyed.KEYED_KEY, stream=stream, headers={
                "Host": "unrelated.invalid", "X-Api-Key": "keyedtest-key-unselected-client",
                "Proxy-Authorization": "Bearer keyedtest-key-proxy-attempt", "X-Forwarded-Host": "unrelated.invalid",
                "X-Forwarded-Proto": "http"})
            self.assertEqual(reply.status, 200)
            self.assertEqual(len(reply.hits), 1)
            self.assertTrue(reply.hits[0].header_values.get("authorization") == "Bearer " + KEYS[keyed.KEYED_SECRET])
            self.assertEqual(reply.hits[0].path, "/keyedtest/v1/chat/completions")
        # A wrong gateway Authorization is refused before any provider hit.
        reply = fixture.gateway.request(keyed.KEYED_KEY, headers={"Authorization": "Bearer keyedtest-key-wrong-client"})
        self.assertEqual(reply.status, 401)
        self.assertEqual(len(reply.hits), 0)
        record(self, "audit", "K1-headers", credential_match=True, refused_gateway_auth_hits=0)

    def test_keyed_effort_qualification_reaches_upstream_and_strict_refusal_fails(self):
        strict = {"enabled": False}
        def respond(path, body, raw, headers):
            if strict["enabled"] and body.get("reasoning_effort") == "medium":
                return 400, "application/json", b'{"error":{"message":"unsupported effort"}}'
            return harness.fake_reply(path, body, raw)
        fixture = self.fixture(respond=respond)
        line = fixture.plan.layer.lines[keyed.KEYED_KEY]
        plan = qualify.build_plan(keyed.KEYED_KEY, line.core_entry, digest=line.definition_digest,
            provider_id=keyed.KEYED_ID, provider=fixture.plan.layer.providers[keyed.KEYED_ID].entry,
            checks=("smoke", "efforts"), keyed_replay=True)
        def post(call, body):
            reply = harness.unix_request(fixture.gateway.socket, "POST", "/v1/messages",
                headers=fixture.gateway.headers(), body=body, timeout=5)
            return qualify.HttpResult(reply.status, reply.raw)
        for rejection in (False, True):
            strict["enabled"] = rejection
            before = len(fixture.gateway.upstream.hits)
            results = qualify.run_battery(plan, post)
            hits = fixture.gateway.upstream.hits[before:]
            self.assertEqual(len(hits), 6)
            self.assertEqual([hit.body.get("reasoning_effort") for hit in hits], ["high", *ALL_LEVELS])
            self.assertEqual([r.item for r in results if r.result == "failed"], ["medium"] if rejection else [])
        record(self, "audit", "K12-qualification", requests=12, transmitted_levels=list(ALL_LEVELS), strict_no_retry=True)

    def test_redirect_status_origin_cross_product_no_destination_hits(self):
        scenario = {}
        def respond(path, body, raw, headers):
            return scenario['status'], 'text/plain', b'private redirect body', {'Location': scenario['location']}
        fixture = self.fixture(respond=respond)
        gateway = fixture.gateway
        destinations = [f'https://127.0.0.1:{gateway.egress}/redirect-target',
                        f'https://127.0.0.1:{gateway.egress + 1}/redirect-target',
                        'https://sub.localhost/redirect-target', 'https://unrelated.invalid/redirect-target',
                        f'http://127.0.0.1:{gateway.egress}/redirect-target']
        for status in (301, 302, 303, 307, 308):
            for location in destinations:
                for stream in (False, True):
                    with self.subTest(status=status, target=destinations.index(location), stream=stream):
                        scenario.update(status=status, location=location)
                        before = len(gateway.upstream.hits)
                        reply = gateway.request(keyed.KEYED_KEY, stream=stream)
                        self.assertEqual(reply.status, 502)
                        self.assertEqual(len(gateway.upstream.hits) - before, 1)
                        self.assertTrue(b'upstream redirect refused' in reply.raw)
                        self.assertFalse(b'private redirect body' in reply.raw)
                        self.assertFalse(b'redirect-target' in reply.raw)
                        self.assertFalse('location' in reply.headers)
        self.assertFalse('redirect-target' in gateway.log_path.read_text())
        record(self, 'audit', 'CR1', cases=50, destination_hits=0, fixed_error=True)

    def test_safe_error_boundaries_short_long_keys_and_logging(self):
        scenario = {}
        def respond(path, body, raw, headers):
            return scenario['status'], scenario['type'], scenario['data'], scenario.get('headers', {})
        cases = 0
        for value in ('q7v9', 'keyedtest-key-long-dummy-' + 'q' * 80):
            fixture = self.fixture(values={**KEYS, keyed.KEYED_SECRET: value}, respond=respond)
            gateway = fixture.gateway
            for debug in (False, True):
                document = copy.deepcopy(gateway.document)
                document['debug'] = debug
                document['request-log'] = debug  # non-authorable diagnostic control
                document['openai-compatibility'] = [s for s in document['openai-compatibility']
                                                   if s['name'] != 'claude-multi-render']
                gateway.document = render.finalize_document(document)
                state.atomic_write(gateway.config, render.emit_yaml(gateway.document).encode())
                gateway.await_ready()
                scenarios = [(400, 'application/json', json.dumps({'error': {'message': value}}).encode()),
                    (401, 'text/plain', value.encode()), (429, 'text/html', ('<p>' + value + '</p>').encode()),
                    (500, 'application/json', json.dumps({'error': value.encode().hex()}).encode()),
                    (200, 'application/json', json.dumps({'error': {'message': value}}).encode()),
                    (400, 'text/plain', value.encode() + b'x' * 65537),
                    (200, 'text/event-stream', b'data: {"error":{"message":"' + value.encode() + b'"}}\n\n'),
                    (200, 'text/event-stream', b'data: malformed-' + value.encode() + b'\n\n')]
                for status, content_type, data in scenarios:
                    for stream in (False, True):
                        scenario.update(status=status, type=content_type, data=data,
                                        headers={'X-Private-Metadata': value})
                        reply = gateway.request(keyed.KEYED_KEY, stream=stream)
                        self.assertEqual(len(reply.hits), 1)
                        self.assertFalse(value.encode() in reply.raw, 'selected credential in client error')
                        self.assertFalse(value in json.dumps(reply.headers), 'selected credential in client headers')
                        self.assertLess(len(reply.raw), 65536)
                        cases += 1
                # Read a bounded disposable log only; never retain its contents.
                self.assertLess(gateway.log_path.stat().st_size, 4 * 1024 * 1024)
                self.assertFalse(value in gateway.log_path.read_text(), 'selected credential in gateway log')
        # The matching compiled patch matrix adds read/close/transport faults,
        # midstream frames, encoded echoes and both disposable logging modes.
        names = ('TestCM053KeyedCompatHTTPErrorSafe', 'TestCM053KeyedCompatHTTP200ErrorEnvelopeSafe',
                 'TestCM053KeyedCompatSSEClassifiedBeforeCapture', 'TestCM053KeyedCompatAmbiguousPayloadSafe',
                 'TestCM053KeyedCompatStreamFrameBounds', 'TestCM053KeyedCompatHeadersSafe',
                 'TestCM053KeyedCompatIOErrorsSafe', 'TestCM053KeyedCompatLoggingMatrix',
                 'TestCM053KeylessCompatErrorBehaviorPreserved')
        for name in names:
            harness.run_codex_diagnostic(name, package='executor')
        record(self, 'audit', 'KS1', cases=cases, diagnostics=len(names), credential_echo=False, debug_checked=True)

    def test_management_canary_on_off_allowed_and_denied_surfaces(self):
        for enabled in (False, True):
            fixture = self.fixture(management=enabled)
            gateway = fixture.gateway
            self.assertEqual(gateway.request(keyed.KEYED_KEY).status, 200)
            headers = {**gateway.headers(), 'Authorization': 'Bearer dummy-gwtest-mgmt'}
            for route in ('auth-files', 'auth-files/models?name=fixture', 'model-definitions/openai-compatibility'):
                reply = harness.unix_request(gateway.socket, 'GET', '/v0/management/' + route, headers=headers)
                self.assertIn(reply.status, (200, 400, 404))
                for value in KEYS.values():
                    self.assertFalse(value.encode() in reply.raw)
                self.assertFalse(b'api-key-entries' in reply.raw)
                if not enabled:
                    self.assertEqual(reply.status, 404)
            for route in ('config', 'config.yaml', 'auth-files/download?name=fixture', 'api-call',
                          'openai-compatibility', 'claude-api-key'):
                reply = harness.unix_request(gateway.socket, 'GET', '/v0/management/' + route, headers=headers)
                self.assertEqual(reply.status, 404)
                self.assertTrue(reply.raw in (b'', b'404 page not found'))
        record(self, 'audit', 'M1', management_modes=2, canary_absent=True)

    def test_late_duplicate_stream_and_compact_controls(self):
        names = ('TestCM053CompatChatRetentionFinalBoundary', 'TestCM053CompatChatRetentionLateOverride',
                 'TestCM053CompatChatRetentionDuplicateKeys', 'TestCM053CompatChatRetentionFailureSendsNothing',
                 'TestCM053CompatStreamAltStillSanitized')
        for name in names:
            harness.run_codex_diagnostic(name, package='executor')
        # These are raw executor controls, not additional authorable options.
        fixture = self.fixture()
        for stream in (False, True):
            reply = fixture.gateway.request(keyed.KEYED_KEY, stream=stream,
                                             body={'prompt_cache_retention': '24h'})
            self.assertEqual(reply.status, 200)
            self.assertNotIn('prompt_cache_retention', reply.hits[0].top_level_keys)
        record(self, 'audit', 'P1-controls', diagnostics=len(names), raw_non_authorable=True, managed_chat_hits=2)

    def test_returned_reasoning_parallel_tools_and_split_arguments(self):
        import re
        from test_gateway_contracts import reconstructed_blocks
        def respond(path, body, raw, headers):
            messages = body.get('messages', [])
            last = messages[-1].get('content', '') if messages else ''
            tool_results = [message for message in messages if message.get('role') == 'tool']
            if tool_results:
                message = {'role': 'assistant', 'content': tool_results[-1]['content']}
                finish = 'stop'
            elif body.get('tools') and isinstance(last, str) and 'nonce "' in last:
                nonce = re.search(r'nonce "([^"]+)"', last).group(1)
                message = {'role': 'assistant', 'content': None, 'reasoning_content': 'keyedtest-transient-reasoning',
                    'tool_calls': [{'id': 'call_keyedtest_replay', 'type': 'function', 'function': {
                        'name': qualify.TOOL_NAME, 'arguments': json.dumps({'nonce': nonce})}}]}
                finish = 'tool_calls'
            else:
                return harness.fake_reply(path, body, raw)
            return 200, 'application/json', json.dumps({'id': 'keyedtest-completion', 'object': 'chat.completion',
                'model': body['model'], 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}}).encode()
        fixture = self.fixture(respond=respond)
        line = fixture.plan.layer.lines[keyed.KEYED_KEY]
        returned, sent = [], []
        def post(call, body):
            sent.append(json.loads(body))
            reply = harness.unix_request(fixture.gateway.socket, 'POST', '/v1/messages',
                                         headers=fixture.gateway.headers(), body=body)
            returned.append(reply.json()['content'])
            return qualify.HttpResult(reply.status, reply.raw)
        for variant in qualify.TOOL_VARIANTS:
            returned.clear()
            sent.clear()
            plan = qualify.build_plan(keyed.KEYED_KEY, line.core_entry, digest=line.definition_digest,
                provider_id=keyed.KEYED_ID, provider=fixture.plan.layer.providers[keyed.KEYED_ID].entry,
                checks=('tools',), tools_variant=variant, keyed_replay=True)
            (result,) = qualify.run_battery(plan, post)
            self.assertEqual(result.result, 'pass')
            self.assertEqual(len(sent), 2)
            self.assertEqual(sent[1]['messages'][1]['content'], returned[0])
            self.assertTrue(any(block['type'] == 'thinking' for block in returned[0]))
            self.assertTrue(any(message.get('role') == 'tool' and message.get('tool_call_id') == 'call_keyedtest_replay'
                                for message in fixture.gateway.upstream.hits[-1].body['messages']))
        reply = fixture.gateway.request(keyed.KEYED_KEY, stream=True, mode='tool_calls_split')
        tools = [block for block in reconstructed_blocks(reply) if block['type'] == 'tool_use']
        self.assertEqual(len(tools), 2)
        for tool, suffix in zip(tools, ('a', 'b')):
            self.assertEqual(tool['id'], 'call_gwtest_' + suffix)
            self.assertEqual(tool['name'], 'gwtest_tool_' + suffix)
            self.assertEqual(json.loads(tool['partial_json']), {'k': 'v'})
        record(self, 'audit', 'F1', returned_history_equal=True, tool_variants=2, parallel_split_calls=2)

    def test_strict_provider_refusals_and_default_is_compat_losses(self):
        from test_gateway_contracts import reconstructed_blocks
        fixture = self.fixture()
        gateway = fixture.gateway
        for stream in (False, True):
            reply = gateway.request(keyed.KEYED_KEY, stream=stream, mode='reasoning')
            blocks = reconstructed_blocks(reply) if stream else reply.json()['content']
            thinking = [block for block in blocks if block['type'] == 'thinking']
            self.assertEqual(len(thinking), 1)
            self.assertFalse(thinking[0].get('signature'))
            for compat in (False, True):
                document = copy.deepcopy(gateway.document)
                document['openai-compatibility'] = [s for s in document['openai-compatibility']
                                                   if s['name'] != 'claude-multi-render']
                for section in document['openai-compatibility']:
                    for model in section['models']:
                        if compat:
                            model['is-compat'] = True  # non-authorable contrast, never a production default
                        else:
                            model.pop('is-compat', None)
                gateway.document = render.finalize_document(document)
                state.atomic_write(gateway.config, render.emit_yaml(gateway.document).encode())
                gateway.await_ready()
                replay = gateway.request(keyed.KEYED_KEY, stream=stream, body={'messages': [
                    {'role': 'assistant', 'content': thinking}, {'role': 'user', 'content': 'continue'}]})
                self.assertEqual(replay.status, 200)
                reasoning = [m['reasoning_content'] for m in replay.hits[0].body['messages'] if 'reasoning_content' in m]
                self.assertEqual(reasoning, [thinking[0]['thinking']] if compat else [])
        # Authored schema normalization and the lossy fields are observed on
        # the default renderer, never inferred from the raw is-compat control.
        losses = self.fixture()
        tool = {"name": "keyedtest_schema", "description": "fixture", "strict": True,
                "input_schema": {"type": "object", "properties": {
                    "child": {"type": "object"}, "text": {"type": "string", "pattern": r"\p{L}+"}},
                    "required": ["child"], "additionalProperties": False}}
        histories = []
        for stream in (False, True):
            reply = losses.gateway.request(keyed.KEYED_KEY, stream=stream, body={"tools": [tool], "max_tokens": 128})
            self.assertEqual(reply.status, 200)
            sent = reply.hits[0].body
            function = sent["tools"][0]["function"]
            self.assertNotIn("strict", function)
            self.assertEqual(function["parameters"], {"type": "object", "properties": {
                "child": {"type": "object", "properties": {}}, "text": {"type": "string"}},
                "required": ["child"], "additionalProperties": False})
            self.assertEqual(sent["max_tokens"], 128)
            self.assertNotIn("max_completion_tokens", sent)
            for is_error in (False, True):
                reply = losses.gateway.request(keyed.KEYED_KEY, stream=stream, body={"messages": [
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "call_keyedtest_error",
                        "name": tool["name"], "input": {"child": {}}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_keyedtest_error",
                        "content": "fixture result", "is_error": is_error}]}]})
                self.assertEqual(reply.status, 200)
                histories.append([m for m in reply.hits[0].body["messages"] if m["role"] in ("assistant", "tool")])
                result, = [m for m in histories[-1] if m["role"] == "tool"]
                self.assertEqual(result["tool_call_id"], "call_keyedtest_error")
                self.assertEqual(result["name"], tool["name"])
                self.assertEqual(result["content"], "fixture result")
                self.assertNotIn("is_error", result)
            self.assertEqual(histories[-2], histories[-1])

        # Each strict fake accepts the ordinary control and rejects a specific
        # translated feature. Run through the real qualifier: no weaker retry,
        # no synthetic pass, and failed HTTP status is evidence, not a grant.
        scenario = {"reject": None}
        predicates = {
            "usage": lambda b: b.get("stream_options", {}).get("include_usage") is True,
            "effort": lambda b: "reasoning_effort" in b,
            "schema": lambda b: any(t["function"]["parameters"].get("additionalProperties") is False
                                    for t in b.get("tools", [])),
            "strict": lambda b: any("strict" not in t["function"] for t in b.get("tools", [])),
            "history": lambda b: any(m["role"] == "tool" for m in b["messages"]),
            "max-completion": lambda b: "max_completion_tokens" not in b,
        }

        def respond(path, body, raw, headers):
            selected = scenario["reject"]
            if selected and predicates[selected](body):
                return 400, "application/json", b'{"error":{"message":"unsupported fixture feature"}}'
            if body.get("tools"):
                import re
                results = [m for m in body["messages"] if m["role"] == "tool"]
                if results:
                    message, finish = {"role": "assistant", "content": results[-1]["content"]}, "stop"
                else:
                    nonce = re.search(r'nonce "([^"]+)"', body["messages"][-1]["content"]).group(1)
                    message, finish = {"role": "assistant", "content": None, "reasoning_content": "fixture reasoning",
                        "tool_calls": [{"id": "call_keyedtest_strict", "type": "function", "function": {
                            "name": qualify.TOOL_NAME, "arguments": json.dumps({"nonce": nonce})}}]}, "tool_calls"
                return 200, "application/json", json.dumps({"id": "keyedtest-strict", "object": "chat.completion",
                    "model": body["model"], "choices": [{"index": 0, "message": message, "finish_reason": finish}]}).encode()
            return harness.fake_reply(path, body, raw)

        strict = self.fixture(respond=respond)
        line = strict.plan.layer.lines[keyed.KEYED_KEY]
        for feature, check, variant, expected_hits in (
                ("usage", "stream", "forced", 1), ("effort", "smoke", "forced", 1),
                ("schema", "tools", "forced", 1), ("strict", "tools", "auto", 1),
                ("history", "tools", "forced", 2), ("max-completion", "smoke", "forced", 1)):
            plan = qualify.build_plan(keyed.KEYED_KEY, line.core_entry, digest=line.definition_digest,
                provider_id=keyed.KEYED_ID, provider=strict.plan.layer.providers[keyed.KEYED_ID].entry,
                checks=(check,), tools_variant=variant, keyed_replay=True)
            def post(call, body):
                reply = harness.unix_request(strict.gateway.socket, "POST", "/v1/messages",
                    headers=strict.gateway.headers(), body=body)
                return qualify.HttpResult(reply.status, reply.raw)
            for rejected in (False, True):
                with self.subTest(feature=feature, rejected=rejected):
                    scenario["reject"] = feature if rejected else None
                    before = len(strict.gateway.upstream.hits)
                    result, = qualify.run_battery(plan, post)
                    self.assertEqual((result.result, result.reason),
                                     ("failed", "http-status") if rejected else ("pass", "ok"))
                    self.assertEqual(len(strict.gateway.upstream.hits) - before,
                                     expected_hits if rejected else 2 if check == "tools" else 1)
        # is_error has no standard chat equivalent; a provider requiring it
        # refuses the translated history rather than receiving fabricated data.
        required_error = self.fixture(respond=lambda _p, body, *_args: (
            (400, "application/json", b'{"error":{"message":"error marker required"}}')
            if any(m["role"] == "tool" and "is_error" not in m for m in body["messages"])
            else harness.fake_reply(_p, body, _args[0])))
        reply = required_error.gateway.request(keyed.KEYED_KEY, body={"messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_keyedtest_error",
                "name": tool["name"], "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_keyedtest_error",
                "content": "fixture result", "is_error": True}]}]})
        self.assertEqual(qualify.classify_message(qualify.HttpResult(reply.status, reply.raw)), ("failed", "http-status"))
        self.assertEqual(len(reply.hits), 1)
        record(self, 'audit', 'F1-losses', unsigned_reasoning=True, default_preserves_reasoning=False,
               raw_compat_control_preserves=True, strict_no_retry=True, schema_normalized=True,
               strict_flag_lost=True, is_error_lost=True, max_completion_tokens_absent=True,
               strict_refusal_features=sorted(predicates), is_error_refusal_hits=1)

    def test_keyed_authority_reload_and_stale_sentinel_races(self):
        # Reuse command-owner injection seams for prompt/I/O/commit races and
        # real state durability. TLS key replacement is independently K1/A1.
        result = unittest.TestResult()
        suite = unittest.TestSuite([
            keyed.KeyedAuthorityTests('test_keyed_admission_race_rejects_stale_render_evidence'),
            keyed.KeyedAuthorityTests('test_keyed_admission_preflight_fails_before_secret_or_network'),
            keyed.KeyedRenderTests('test_keyed_capture_failure_keeps_previous_config'),
        ])
        suite.run(result)
        self.assertTrue(result.wasSuccessful(), 'keyed command authority/capture regression failed')
        self.assertFalse(result.skipped)
        record(self, 'audit', 'A1-authority', alias_and_sentinel_required=True, capture_before_publish=True,
               audit_is_not_admission=True)
