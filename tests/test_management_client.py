"""One bounded read, mandatory injectable ownership, no raw response retention."""

import ast
import http.client
import http.server
import inspect
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import management, probe, quota, service, state
from _layout import REPO_ROOT
from _tier import fast_tier_boundary
from tests import _tripwire
from tests.test_quota import FIXTURES, NOW, SENTINELS

BODY = (FIXTURES / "auth-files-mixed.json").read_bytes()
OURS = service.OwnerVerdict("ours", "fixture gateway")


class FakeManagementServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        outer = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.requestline, list(self.headers.items())))
                if outer.delay:
                    time.sleep(outer.delay)
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                try:
                    self.wfile.write(outer.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass
        super().__init__(("127.0.0.1", 0), Handler)
        self.status, self.body, self.delay = 200, BODY, 0
        self.requests, self.accepts = [], 0
        self.base = f"http://127.0.0.1:{self.server_port}"
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def get_request(self):
        result = super().get_request()
        self.accepts += 1
        return result


class ManagementClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.server = FakeManagementServer()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.gateway = {"gateway": {"base_url": self.server.base}}
        management.prepare_start(self.home, stopped=True)
        self.owner = mock.Mock(return_value=OURS)
        self.attention = mock.Mock()
        self.addCleanup(mock.patch.stopall)
        # An accidental fallback to the host observer is a failure, not evidence.
        mock.patch.object(service, "listener_owner", side_effect=AssertionError("host owner observation")).start()
        mock.patch.object(quota, "_now", return_value=NOW).start()

    def read(self, **kwargs):
        kwargs.setdefault("environ", {management.CHANNEL_ENV: management.MANAGEMENT_CHANNEL})
        return management.pool_status(self.home, self.gateway, owner_check=self.owner,
                                      attention=self.attention, **kwargs)

    def test_exact_request_status_map_no_retries_and_no_proxy_env(self):
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1"}):
            for code, expected in ((200, "ok"), (401, "mismatch"), (403, "refused"), (404, "management-off"), (500, "error")):
                self.server.status = code
                before = self.server.accepts
                result = self.read()
                self.assertEqual(result.state, expected)
                self.assertEqual(result.http_status, code)
                self.assertEqual(result.read_at, NOW)
                self.assertEqual(self.server.accepts, before + 1)
        self.assertEqual(len(self.server.requests), 5)
        for line, headers in self.server.requests:
            self.assertEqual(line, "GET /v0/management/auth-files HTTP/1.1")
            self.assertEqual({k for k, _v in headers}, {"Host", "Accept-Encoding", "X-Management-Key"})
            self.assertEqual(dict(headers)["Host"], f"127.0.0.1:{self.server.server_port}")
            self.assertEqual(dict(headers)["X-Management-Key"], management.read_active(self.home))

    def test_body_cap_and_no_error_body_read(self):
        self.server.body = b"x" * (quota.MAX_BODY_BYTES + 1)
        self.assertEqual(self.read().state, "malformed")
        for code in (200, 401, 403, 404, 500):
            conn = mock.Mock()
            response = conn.getresponse.return_value
            response.status, response.read.return_value = code, b"{}"
            management.send_request(conn, "dummy")
            if code == 200:
                response.read.assert_called_once_with(quota.MAX_BODY_BYTES + 1)
            else:
                response.read.assert_not_called()
            conn.request.assert_called_once_with("GET", quota.AUTH_FILES_PATH, headers={"X-Management-Key": "dummy"})

    def test_timeout_and_closed_port(self):
        self.server.delay = 0.2
        started = time.monotonic()
        result = self.read(get=lambda base, key: management.default_get(base, key, timeout=0.05))
        self.assertEqual(result.state, "error")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.server.accepts, 1)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.gateway["gateway"]["base_url"] = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self.assertEqual(self.read().state, "down")

    def test_invalid_urls_never_connect_or_observe_owner(self):
        for url in ("http://localhost:1234", "https://127.0.0.1:1234", "http://127.0.0.1:1234/x",
                    "http://10.0.0.1:1234", "http://127.0.0.1:0", "http://127.0.0.1:65536",
                    "http://127.0.0.1:1234?x", "http://key@127.0.0.1:1234"):
            self.gateway["gateway"]["base_url"] = url
            self.assertEqual(self.read().state, "error")
        self.assertEqual(self.server.accepts, 0)
        self.owner.assert_not_called()

    def test_slot_states_and_reenable_cannot_accumulate_strikes(self):
        getter = mock.Mock(return_value=(200, BODY))
        active = management.key_dir(self.home) / management.KEY_FILE
        active.unlink()
        self.assertEqual(self.read(get=getter).state, "no-key")
        management.ensure(self.home)
        self.assertEqual(self.read(get=getter).state, "pending")
        management.prepare_start(self.home, stopped=True)
        state.atomic_write(active, b"bad-shape")
        result = self.read(get=getter)
        self.assertEqual(result.state, "key-unusable")
        self.assertIn("disable-management-key", result.remedy)
        management.disable(self.home)
        self.assertEqual(self.read(get=getter).state, "disabled")
        management.stage_rotation(self.home)
        for _ in range(8):
            self.assertEqual(self.read(get=getter).state, "pending")
        getter.assert_not_called()
        self.owner.assert_not_called()
        self.assertEqual(self.server.accepts, 0)

    def test_unsafe_path_remedies_are_retained_not_secret_values(self):
        active = management.key_dir(self.home) / management.KEY_FILE
        for shape, expected in (("mode", "disable-management-key"), ("symlink", "unlink --"), ("directory", "mv -T --")):
            if active.is_dir():
                active.rmdir()
            else:
                active.unlink(missing_ok=True)
            if shape == "mode":
                active.write_text("SENTINEL")
                active.chmod(0o644)
            elif shape == "symlink":
                active.symlink_to(self.home / "absent-SENTINEL")
            else:
                active.mkdir()
            result = self.read()
            self.assertEqual(result.state, "key-unusable")
            self.assertIn(expected, quota.state_text(result, restart_hint="RESTART"))
            self.assertNotIn("SENTINEL", repr(result))
        self.assertEqual(self.server.accepts, 0)

    def test_owner_policy_applies_to_injected_transport_and_warns(self):
        for kind, expected, calls in (("foreign", "not-ours", 0), ("unknown", "ok", 1),
                                      ("ours", "ok", 1), ("none", "ok", 1)):
            getter = mock.Mock(return_value=(200, BODY))
            self.owner.return_value = service.OwnerVerdict(kind, "fixture owner")
            self.attention.reset_mock()
            before = len(_tripwire.HITS)
            self.assertEqual(self.read(get=getter).state, expected)
            self.assertEqual(getter.call_count, calls)
            self.assertEqual(self.attention.call_count, int(kind == "unknown"))
            self.assertEqual(len(_tripwire.HITS), before)

    def test_secret_transport_exceptions_discarded(self):
        key = management.read_active(self.home)
        for error in (RuntimeError(key), ConnectionRefusedError(key)):
            getter = mock.Mock(side_effect=error)
            result = self.read(get=getter)
            self.assertNotIn(key, repr(result))
            self.assertNotIn(key, quota.state_text(result, restart_hint="RESTART"))
            getter.assert_called_once()


class ImportBoundaryTests(unittest.TestCase):
    def test_pure_parser_and_leaf_client(self):
        for module in (management, quota):
            tree = ast.parse(inspect.getsource(module))
            names = {alias.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for alias in n.names}
            names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            self.assertFalse(names & {"urllib", "urllib.request", "proxy", "cli", "launch"})
            if module is quota:
                relative = {alias.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level for alias in n.names}
                self.assertEqual(relative, {"errors", "strict_json", "Fact", "Report", "instant"})
        source = REPO_ROOT / "src" / "claude_multi"
        modules = {"claude_multi." + ".".join(p.relative_to(source).with_suffix("").parts).removesuffix(".__init__"): p
                   for p in source.rglob("*.py")}
        self.assertTrue(any(name.startswith("claude_multi.cli.") for name in modules))
        self.assertEqual(sorted(name for name, p in modules.items() if "/v0/management" in p.read_text()),
                         ["claude_multi.quota"])
        # The gateway secret name only in the scrub/injection and the session strip.
        # The scrub's deny list moved to secret_store (the shared credential
        # policy; proxy re-exports the same object), proxy keeps the injection.
        self.assertEqual(sorted(name for name, p in modules.items() if "MANAGEMENT_PASSWORD" in p.read_text()),
                         ["claude_multi.compiler", "claude_multi.proxy", "claude_multi.secret_store"])


class RealGatewayAuthFilesTests(unittest.TestCase):
    def test_authenticated_auth_files_and_refused_route(self):
        boundary = fast_tier_boundary() or probe.network_isolation_available()
        if boundary:
            self.skipTest("BOUNDARY: " + boundary)
        from _gateway import _gateway_binary
        binary, reason = _gateway_binary()
        if binary is None:
            self.skipTest("BOUNDARY: " + reason)
        with tempfile.TemporaryDirectory(prefix="cm-quota-gateway-") as temp:
            root = Path(temp)
            auth = state.ensure_private_dir(root / "auth")
            state.atomic_write(auth / "claude-name-SENTINEL.json", json.dumps({
                "type": "claude", "access_token": "sk-ant-api03-SENTINEL",
                "refresh_token": "dummy-refresh-SENTINEL", "email": "pii-email@example.invalid",
                "expired": "2099-01-01T00:00:00Z", "last_refresh": "2098-01-01T00:00:00Z",
            }).encode())
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            key = "a" * 64
            config = root / "config.yaml"
            config.write_text(f'''host: "127.0.0.1"
port: {port}
auth-dir: "{auth}"
api-keys: ["dummy-gateway-token"]
remote-management:
  allow-remote: false
  secret-key: ""
  disable-control-panel: true
  disable-auto-update-panel: true
usage-statistics-enabled: false
discovery:
  enabled: false
''')
            bridge = root / "gateway.sock"
            process = probe.start_isolated_process(
                [binary, "--config", str(config), "--local-model"],
                bridges=[probe.PortBridge(port, bridge, "ingress")], cwd=root,
                env={"HOME": str(root), "PATH": "/usr/bin:/bin", "MANAGEMENT_PASSWORD": key},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                def connection():
                    conn = probe.UnixHTTPConnection(bridge, timeout=2)
                    # HTTPConnection uses these for its automatic Host header.
                    conn.host, conn.port = "127.0.0.1", port
                    return conn
                # Management bypasses the startup gate: establish auth registration
                # via the local models endpoint BEFORE the one measured auth-files read.
                deadline = time.monotonic() + 20
                ready = False
                while time.monotonic() < deadline:
                    conn = connection()
                    try:
                        conn.request("GET", "/v1/models", headers={"Authorization": "Bearer dummy-gateway-token"})
                        response = conn.getresponse()
                        ready = response.status == 200 and bool(json.loads(response.read()).get("data"))
                        if ready:
                            break
                    except (OSError, http.client.HTTPException, ValueError):
                        pass
                    finally:
                        conn.close()
                    time.sleep(0.05)
                self.assertTrue(ready, "synthetic OAuth auth registration was not ready")
                conn = connection()
                try:
                    conn.request("GET", "/v0/management/config")
                    response = conn.getresponse()
                    refused = (response.status, response.read())
                finally:
                    conn.close()
                self.assertEqual(refused, (404, b""), "current gateway must refuse non-allowlisted routes")
                conn = connection()
                try:
                    code, body = management.send_request(conn, key)
                finally:
                    conn.close()
                self.assertEqual(code, 200, "positive authenticated control; an off API is not proof")
                rows = quota.parse_auth_files(body)
                self.assertEqual([c.handle for c in rows], ["claude#1"])
                code, lines = quota.command_lines(quota.PoolStatus("ok", credentials=rows),
                    provider_by_pool={"claude": "fixture-provider"}, login_commands={},
                    restart_hint="RESTART", now=NOW)
                self.assertEqual(code, 0)
                for sentinel in (*SENTINELS, key):
                    self.assertNotIn(sentinel, repr(rows) + repr(lines))
            finally:
                probe.stop_isolated_process(process)


if __name__ == "__main__":
    unittest.main()
