"""Discriminating patch rows, against disposable network namespaces only."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from claude_multi import probe, state
from _catalog import SHIPPED_ROOT, uses_shipped_catalog
import _gateway_harness as harness

MODULE = "tests.test_gateway_patches"


def tearDownModule():
    harness.EVIDENCE.finalize_module(MODULE)


def record(row, **fields):
    harness.EVIDENCE.record(MODULE, "patches", {"row": row, **fields})


def gateway_binary():
    binary, reason = harness.find_gateway_binary()
    if binary is None:
        harness.boundary("BOUNDARY: " + reason)
    reason = probe.network_isolation_available()
    if reason:
        harness.boundary("BOUNDARY: " + reason)
    return binary


class LoopbackOAuthListenerTests(unittest.TestCase):
    def check_login(self, provider, row, method="B"):
        binary = gateway_binary()
        with tempfile.TemporaryDirectory(prefix="gwtest-login-") as directory:
            root = Path(directory).resolve()
            auth = root / "auth"
            state.ensure_private_dir(auth)
            port, _ = harness.ports()
            config = root / "config.yaml"
            state.atomic_write(config, (f'host: 127.0.0.1\nauth-dir: "{auth}"\n'
                'discovery: {enabled: false}\nremote-management: {disable-control-panel: true}\n').encode())
            argv = [binary, "--config", str(config), "--" + provider + "-login", "--no-browser",
                    "--oauth-callback-port", str(port)]
            if method == "A":
                argv = [os.path.realpath(sys.executable), "-I", "-S", "-c",
                        harness.listener_probe_source(), str(port), *argv]
            log_path = root / "login.log"
            with log_path.open("wb") as log:
                process = probe.start_isolated_process(argv, cwd=root,
                    env={"HOME": str(root), "PATH": "/usr/bin:/bin"}, stdout=log, stderr=subprocess.STDOUT)
                try:
                    if method == "A":
                        process.wait(timeout=15)
                        self.assertEqual(process.returncode, 0, "isolated listener probe failed")
                        match = re.search(r"^LISTEN=(.+)$", log_path.read_text(), re.M)
                        self.assertIsNotNone(match, "listener probe omitted its result")
                        rows = [tuple(row) for row in json.loads(match.group(1))]
                    else:
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            self.assertIsNone(process.poll(), "OAuth login exited before callback listen")
                            try:
                                rows = harness.listener_rows(process.pid, binary)
                                if any(p == port for _, p in rows):
                                    break
                            except AssertionError as exc:
                                if "descendant not found" not in str(exc):
                                    raise
                            time.sleep(0.02)
                        else:
                            self.fail("OAuth callback listener deadline exceeded")
                    self.assertTrue(any(p == port for _, p in rows))
                    self.assertTrue(all(harness.loopback_listener(addr) for addr, _ in rows), rows)
                    record(row, method=method, addresses=[addr for addr, _ in rows], ports=[p for _, p in rows])
                finally:
                    probe.stop_isolated_process(process)

    def test_claude_login_callback_listens_on_loopback_only(self):
        self.check_login("claude", "LO1")

    def test_codex_login_callback_listens_on_loopback_only(self):
        self.check_login("codex", "LO2")

    def test_antigravity_login_callback_listens_on_loopback_only(self):
        # An installed gateway may predate this patch: never fall back to it.
        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: LO4 needs " + harness.BINARY_ENV + " set to the manifest build")
        self.check_login("antigravity", "LO4")

    def test_fallback_listener_probe_inside_namespace(self):
        self.check_login("claude", "LO1-A", "A")


class GatewayListenerTests(unittest.TestCase):
    def test_tcp_and_udp_listeners_are_private(self):
        gateway = harness.main_harness()
        rows = harness.listener_rows(gateway.process.pid, gateway.binary)
        self.assertEqual(rows, sorted([("127.0.0.1", gateway.port), ("127.0.0.1", gateway.egress)]))
        udp = harness.listener_rows(gateway.process.pid, gateway.binary, udp=True)
        self.assertTrue(all(harness.loopback_listener(addr) for addr, _ in udp), udp)
        record("LO3", tcp_count=len(rows), udp_count=len(udp))


class ManagementInertWithoutKeyTests(unittest.TestCase):
    def test_management_is_inert_without_key(self):
        gateway = harness.main_harness()
        reply = harness.unix_request(gateway.socket, "GET", "/v0/management/auth-files",
            headers={**gateway.headers(), "X-Management-Key": "dummy-gwtest-mgmt"})
        self.assertEqual(reply.status, 404)
        record("M0", status=reply.status)


class ManagementAllowlistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = harness.GatewayHarness(management=True)
        cls.addClassCleanup(cls.gateway.close)

    def request(self, method, path, *, key=True, **headers):
        # Do not exercise bad keys on allowlisted routes (five-strike ban).
        headers = {"Host": f"127.0.0.1:{self.gateway.port}", **headers}
        if key:
            headers["X-Management-Key"] = "dummy-gwtest-mgmt"
        return harness.unix_request(self.gateway.socket, method, path, headers=headers)

    def test_management_positive_control(self):
        reply = self.request("GET", "/v0/management/auth-files")
        self.assertEqual(reply.status, 200)
        record("M1", status=reply.status)

    def test_refused_route_is_404_with_valid_key(self):
        reply = self.request("GET", "/v0/management/config.yaml")
        self.assertEqual((reply.status, len(reply.raw)), (404, 0))
        record("M2", status=reply.status, body_length=len(reply.raw))

    def test_oauth_callback_is_404(self):
        reply = self.request("POST", "/v0/management/oauth-callback", key=False)
        self.assertEqual(reply.status, 404)
        record("M3", status=reply.status)

    def test_browser_origin_is_404_on_allowlisted_route(self):
        reply = self.request("GET", "/v0/management/auth-files", Origin="http://gwtest.invalid")
        self.assertEqual(reply.status, 404)
        record("M4", status=reply.status)


class ManagementEnvOnlyTests(unittest.TestCase):
    def test_config_secret_cannot_enable_management(self):
        # The installed wrapper may select a different gateway build. Patch
        # evidence must select the manifest build, not infer it from version.
        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: ME1 needs " + harness.BINARY_ENV + " set to the manifest build")
        gateway = harness.GatewayHarness(config_secret=True)
        self.addCleanup(gateway.close)
        reply = harness.unix_request(gateway.socket, "GET", "/v0/management/auth-files",
            headers={"Host": f"127.0.0.1:{gateway.port}", "X-Management-Key": "dummy-gwtest-config-secret"})
        self.assertEqual(reply.status, 404)
        record("ME1", status=reply.status)


class StartupFileAuthTests(unittest.TestCase):
    def test_first_model_route_waits_for_file_auth(self):
        gateway = gateway_binary()  # ordinary main check retains the gateway BOUNDARY
        try:
            binary = harness.selected_diagnostic(gateway)
        except harness.DiagnosticMissing as exc:
            harness.boundary("BOUNDARY: set " + harness.STARTUP_PROBE_ENV + " to the matching diagnostic (" + str(exc) + ")")
        with tempfile.TemporaryDirectory(prefix="gwtest-startup-") as directory:
            root = Path(directory).resolve()
            log_path = root / "probe.log"
            with log_path.open("wb") as log:
                process = probe.start_isolated_process(
                    [str(binary), "-test.v", "-test.run=^TestGatewayStartupFileAuth$", "-test.timeout=25s"],
                    cwd=root, env={"HOME": str(root), "TMPDIR": str(root), "PATH": "/usr/bin:/bin"},
                    stdout=log, stderr=subprocess.STDOUT)
                try:
                    process.wait(timeout=30)
                finally:
                    probe.stop_isolated_process(process)
            # Never print auth/config/log payloads, even on a failed mutant.
            output = log_path.read_text()
            self.assertEqual(process.returncode, 0, "isolated startup file-auth probe failed")
            self.assertIn("--- PASS: TestGatewayStartupFileAuth", output)
            record("S1", status="passed", diagnostic=str(binary), synthetic_file_auth=True)


class PatchOmissionProbeTests(unittest.TestCase):
    """Independent API probes, built against the selected variant.

    P1 and every admitted patch row require an explicitly selected gateway
    build before creating a harness.
    Race probes are runtime discriminators, not strings/build-presence checks.
    """
    def check_probe(self, package, name, row):
        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: " + row + " needs " + harness.BINARY_ENV + " set to the manifest build")
        gateway = gateway_binary()
        # A store output or a content-bound build of the selected gateway;
        # anything else fails (the selected build needs its own diagnostic).
        binary = harness.selected_diagnostic(gateway, None if package == "service"
                                             else "gwtest-" + package + "-probe")
        with tempfile.TemporaryDirectory(prefix="gwtest-omission-") as directory:
            root = Path(directory).resolve()
            with (root / "probe.log").open("wb") as log:
                process = probe.start_isolated_process(
                    [str(binary), "-test.v", "-test.run=^" + name + "$", "-test.timeout=20s"],
                    cwd=root, env={"HOME": str(root), "TMPDIR": str(root), "PATH": "/usr/bin:/bin"},
                    stdout=log, stderr=subprocess.STDOUT)
                try:
                    process.wait(timeout=25)
                finally:
                    probe.stop_isolated_process(process)
            self.assertEqual(process.returncode, 0, "isolated " + row + " omission probe failed")
            self.assertIn("--- PASS: " + name, (root / "probe.log").read_text())
        record(row, status="passed", diagnostic=str(binary), test=name)

    def test_compat_retention(self):
        self.check_probe("executor", "TestOmissionCompatRetention", "P1")

    def test_credential_receipt(self):
        self.check_probe("service", "TestOmissionCredentialReceipt", "CS1")

    def test_credentialed_redirect(self):
        self.check_probe("executor", "TestOmissionCredentialedRedirect", "CR1")

    def test_keyed_safety(self):
        self.check_probe("executor", "TestOmissionKeyedSafety", "KS1")

    def test_auth_snapshot(self):
        self.check_probe("service", "TestOmissionAuthSnapshot", "AS1")

    def test_config_snapshot(self):
        self.check_probe("handlers", "TestOmissionConfigSnapshot", "SC1")

    def test_plugin_host_lock(self):
        self.check_probe("handlers", "TestOmissionPluginHostLock", "PH1")

    def test_metadata_lock(self):
        self.check_probe("executor", "TestOmissionMetadataLock", "ML1")

    def test_overlay(self):
        self.check_probe("service", "TestOmissionOverlay", "OM1")

    def test_refresh_shutdown(self):
        self.check_probe("service", "TestOmissionRefreshShutdown", "RS1")



class CodexIdentityTests(unittest.TestCase):
    def test_codex_identity(self):
        binary = harness.run_codex_diagnostic("TestOmissionCodexIdentity")
        record("CI1", status="passed", diagnostic=binary,
               test="TestOmissionCodexIdentity")

    def test_final_header_precedence_matrix(self):
        # Five explicit, nonempty tests on the diagnostic of the selected build.
        names = ("HTTPFinalHeaders", "WebsocketFinalHeaders", "VersionDefault",
                 "CloakingPrecedence", "StaticOverrideException")
        passed = {}
        for name in names:
            with self.subTest(name=name):
                passed[name] = harness.run_codex_diagnostic("TestCM055CodexIdentity" + name, package="executor")
        # A failed (or boundary-skipped) subTest does not stop the loop: the
        # evidence row is recorded only after all five diagnostics succeeded.
        if len(passed) != len(names):
            if passed:
                self.fail("final-header matrix incomplete: " + ", ".join(n for n in names if n not in passed))
            return
        self.assertEqual(len(set(passed.values())), 1, "the five diagnostics ran on different builds")
        record("CI2", status="passed", diagnostic=passed[names[0]], tests=len(names),
               static_exception="gpt-5.6-luna")


class CodexApiKeySafetyTests(unittest.TestCase):
    """The OpenAI API-key route as rendered (a codex API key with cloaking
    off) against a fake Responses upstream that quotes the key it received:
    the client gets fixed local failure text, and the request carries no
    codex client Version default and no session header."""

    def test_key_route_failures_and_identity(self):
        # An installed gateway may predate this patch: never fall back to it.
        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: CK1 needs " + harness.BINARY_ENV + " set to the manifest build")
        import test_openai_key_route as route
        gateway = harness.GatewayHarness(
            document_factory=route.gateway_document,
            upstream_factory=lambda path: harness.FakeUpstream(path, respond=route.reflecting_upstream))
        try:
            alias = sorted(gateway.info["reviewed"])[0]
            secret = route.DUMMY.encode()
            cases = 0
            for case, text in (("reflect-status-failure", b"upstream rejected the credential"),
                               ("reflect-stream-failure", b"upstream server error")):
                for stream in (False, True):
                    with self.subTest(case=case, stream=stream):
                        reply = gateway.request(alias, stream=stream,
                                                body={"messages": [{"role": "user", "content": case}]})
                        self.assertEqual(len(reply.hits), 1, "the failure never reached the fake upstream")
                        self.assertNotIn(secret, reply.raw)
                        self.assertIn(text, reply.raw)
                        if not stream:
                            self.assertGreaterEqual(reply.status, 400)
                        cases += 1
            reply = gateway.request(alias)
            self.assertEqual(reply.status, 200, reply.raw[:300])
            sent = set(reply.hits[0].header_names)
            self.assertFalse({"version", "session-id", "session_id", "originator", "chatgpt-account-id"} & sent)
            self.assertEqual(cases, 4)
            record("CK1", status="passed", failure_cases=cases, fixed_text=True, version_header=False,
                   session_header=False)
        finally:
            gateway.close()


@uses_shipped_catalog
class ContentChunkRouteInventoryTests(unittest.TestCase):
    def test_shipped_protocol_shapes_have_controls(self):
        # Read declarations only: never the operator's configured providers.
        providers = json.loads((SHIPPED_ROOT / "catalog/providers.json").read_text())["providers"]
        self.assertEqual({name: value["adapter"] for name, value in providers.items()}, {
            "anthropic": "cliproxy-oauth-claude-v1", "openai": harness.CODEX,
            **{name: harness.CLAUDE for name in ("openrouter", "deepseek", "kimi", "qwen", "meta")},
        })
        presets = {path.stem: json.loads(path.read_text())["provider"]
                   for path in (SHIPPED_ROOT / "presets").glob("*.json")}
        self.assertEqual({name for name, value in presets.items() if value["kind"] == "openai-compatible"},
                         {"cerebras", "gemini", "groq", "mistral", "xai"})
        self.assertEqual({name for name, value in presets.items() if value["kind"] == "openai-compatible-lan"},
                         {"lan-openai-compatible", "llama-cpp", "lm-studio", "ollama", "vllm"})
        self.assertEqual({name for name, value in presets.items() if value["kind"] == "anthropic-compatible"},
                         {"model-studio", "moonshot", "novita", "vercel", "zai"})
        self.assertEqual(presets["mistral"]["auth"]["kind"], "bearer")
        self.assertEqual(presets["zai"]["kind"], "anthropic-compatible")
        self.assertEqual(set(harness.CONTENT_CHUNK_SHAPES),
                         {"openai-codex", "openrouter", "deepseek", "kimi", "qwen", "meta", "local-llm", "keyed"})

    def test_goldens_are_independent_omission_controls(self):
        import check_gateway_patch_revert as tool
        patch = "cli-proxy-api-openai-content-chunks.patch"
        self.assertEqual(len(harness.PATCH_ROWS[patch]), 2)
        self.assertEqual(len(harness.PATCH_CONTROL_ROWS[patch]), 2)
        for control in harness.PATCH_CONTROL_ROWS[patch]:
            self.assertIn(control, tool.test_ids())
            self.assertNotIn(control, harness.PATCH_ROWS[patch])
        for route in ("keyless", "keyed"):
            for case in harness.CONTENT_CHUNK_LEGACY_MESSAGES:
                for stream in (False, True):
                    self.assertTrue(harness.CONTENT_CHUNK_HTTP_GOLDENS[f"{route}:{case}:{stream}"])


class _ContentChunkFixture:
    @classmethod
    def setUpClass(cls):
        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: content-chunk proof needs an explicitly selected manifest build")
        from test_gateway_keyed_compat import KeyedTLS
        import test_openai_compat_keyed as keyed
        cls.keyless = harness.GatewayHarness(upstream_factory=lambda path: harness.FakeUpstream(
            path, respond=harness.content_chunk_reply))
        cls.addClassCleanup(cls.keyless.close)
        cls.keyed = KeyedTLS(respond=harness.content_chunk_reply)
        cls.addClassCleanup(cls.keyed.close)
        cls.routes = (("keyless", cls.keyless, "gwtest-compat-plain"),
                      ("keyed", cls.keyed.gateway, keyed.KEYED_KEY))


class MistralContentChunkTests(_ContentChunkFixture, unittest.TestCase):
    def check_chunks(self, stream, row):
        from test_gateway_contracts import reconstructed_blocks
        for route, gateway, alias in self.routes:
            with self.subTest(route=route):
                reply = gateway.request(alias, stream=stream, mode="mistral")
                self.assertEqual(reply.status, 200)
                self.assertEqual(len(reply.hits), 1)
                self.assertTrue(reply.hits[0].path.endswith("/chat/completions"))
                blocks = reconstructed_blocks(reply) if stream else reply.json()["content"]
                self.assertEqual(blocks, [{"type": "thinking", "thinking": "first second"},
                                          {"type": "text", "text": "answer"}])
                if stream:
                    events = reply.events()
                    self.assertEqual(sum(e["type"] == "message_stop" for e in events), 1)
                    self.assertEqual(sum(e["type"] == "content_block_stop" for e in events), 2)
                    delta = next(e for e in events if e["type"] == "message_delta")
                    self.assertEqual(delta["delta"]["stop_reason"], "end_turn")
                    self.assertEqual(delta["usage"], {"input_tokens": 11, "output_tokens": 4})
                else:
                    self.assertEqual(reply.json()["stop_reason"], "end_turn")
                    self.assertEqual(reply.json()["usage"], {"input_tokens": 11, "output_tokens": 4})
        if self._outcome.success:
            record(row, status="passed", routes=2, unsigned_thinking=True, repeated_closed_ignored=True)

    def test_streaming_content_chunks(self):
        self.check_chunks(True, "MC1")

    def test_non_streaming_content_chunks(self):
        self.check_chunks(False, "MC2")


class ContentChunkGoldenTests(_ContentChunkFixture, unittest.TestCase):
    def check_golden(self, gateway, alias, route, case, stream):
        reply = harness.content_chunk_request(gateway, alias, stream=stream, mode=case)
        self.assertEqual(reply.status, 200)
        self.assertEqual(len(reply.hits), 1)
        self.assertEqual(reply.raw, harness.CONTENT_CHUNK_HTTP_GOLDENS[f"{route}:{case}:{stream}"].encode(),
                         f"legacy bytes changed: {route}/{case}/{stream}")

    def test_legacy_chat_bytes(self):
        fields = ("string", "reasoning_content", "reasoning", "reasoning_details")
        cases = 0
        for route, gateway, alias in self.routes:
            # Every compatible shape is tested against every reasoning spelling,
            # not inferred from a provider name or a successful text-only turn.
            for shape in harness.CONTENT_CHUNK_SHAPES:
                for case in fields:
                    with self.subTest(route=route, shape=shape, field=case):
                        self.check_golden(gateway, alias, route, case, True)
                        cases += 1
            for case in harness.CONTENT_CHUNK_LEGACY_MESSAGES:
                for stream in (False, True):
                    with self.subTest(route=route, case=case, stream=stream):
                        self.check_golden(gateway, alias, route, case, stream)
                        cases += 1
        if self._outcome.success:
            record("MG1", status="passed", cases=cases, byte_identical=True,
                   shapes=list(harness.CONTENT_CHUNK_SHAPES), unknown_chunks="legacy-per-path")

    def test_native_route_bytes(self):
        for alias in ("gwtest-cc-think-a", "gwtest-ccb-x", "gwtest-codex-e-none"):
            for stream in (False, True):
                with self.subTest(alias=alias, stream=stream):
                    reply = harness.content_chunk_request(self.keyless, alias, stream=stream)
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(len(reply.hits), 1)
                    self.assertTrue(reply.hits[0].path.endswith(
                        "/responses" if "codex" in alias else "/v1/messages"))
                    self.assertEqual(reply.raw, harness.CONTENT_CHUNK_HTTP_GOLDENS[
                        f"native:{alias}:{stream}"].encode())
        if self._outcome.success:
            record("MG2", status="passed", cases=6, messages_and_responses_untouched=True)


if __name__ == "__main__":
    unittest.main()
