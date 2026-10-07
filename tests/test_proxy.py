"""Offline disposable-proxy and proxy-control contract tests.

Loopback only: the proxy loads the v2-rendered config and routes to a fake
upstream; no external provider is ever contacted. Proxy-control tests use
isolated homes and injected exec; the real binary is required only for the
disposable rig, which skips with an explicit boundary when unavailable.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from claude_multi import catalog, custom, endpoint, probe, proxy, render, sessions, state
from claude_multi.proxy import ProxyError
from _catalog import FIXTURE_ROOT
from _gateway_harness import boundary


CATALOG_ROOT = FIXTURE_ROOT


# run/login gained a chdir seam that defaults to "no chdir";
# the direct cmd_run/cmd_login/main(["run"]) call sites here pass none, so
# the test process's working directory must never move.
_MODULE_CWD: str | None = None


def setUpModule() -> None:
    global _MODULE_CWD
    _MODULE_CWD = os.getcwd()


def tearDownModule() -> None:
    moved = os.getcwd()
    if _MODULE_CWD is not None and moved != _MODULE_CWD:
        os.chdir(_MODULE_CWD)
        raise AssertionError(f"test_proxy moved the process cwd: {_MODULE_CWD} -> {moved}")
GATEWAY_TOKEN = "a" * 64
DUMMY_KIMI = "dummy-kimi-key"
DUMMY_OPENROUTER = "dummy-openrouter-key"


# Point the disposable rig at a not-yet-installed gateway build (e.g.
# the 7.3.15 store path before activation). Narrow on purpose: an absolute
# /nix/store path only; anything else is refused, never silently ignored.
TEST_BINARY_ENV = "CLAUDE_MULTI_TEST_CLI_PROXY_API"


def _find_binary() -> str | None:
    override = os.environ.get(TEST_BINARY_ENV)
    if override:
        real = os.path.realpath(override)
        if not real.startswith("/nix/store/") or not Path(real).is_file():
            boundary(
                f"boundary: {TEST_BINARY_ENV}={override} is not a /nix/store "
                "cli-proxy-api file; disposable proxy contract skipped"
            )
        return real
    from _gateway import _wrapper_gateway

    for candidate in (
        _wrapper_gateway(),
        shutil.which("cli-proxy-api"),
        str(Path.home() / ".nix-profile" / "bin" / "cli-proxy-api"),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _UpstreamCapture(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        type(self).requests.append(
            {
                "path": self.path,
                "x-api-key": self.headers.get("x-api-key"),
                "authorization": self.headers.get("authorization"),
                "body": body,
            }
        )
        if '"trigger-error-400"' in body:
            payload = {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "fake upstream rejection",
                },
            }
            data = json.dumps(payload).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if '"stream": true' in body or '"stream":true' in body:
            chunks = [
                {"type": "message_start", "message": {"model": "k3"}},
                {"type": "content_block_delta", "delta": {"text": "FAKE"}},
                {"type": "content_block_delta", "delta": {"text": "_OK"}},
                {"type": "message_stop"},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        payload = {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": "k3",
            "content": [{"type": "text", "text": "FAKE_OK"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _wait_ready(base_url: str, opener, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(
                base_url + "/healthz",
                headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"},
            )
            with opener.open(request, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("disposable proxy did not become ready")


class DisposableProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binary = _find_binary()
        if self.binary is None:
            boundary(
                "boundary: pinned cli-proxy-api binary not available; "
                "disposable proxy contract skipped"
            )
        reason = probe.network_isolation_available()
        if reason is not None:
            boundary("BOUNDARY: " + reason)
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-proxy-"))
        self.addCleanup(self._cleanup)
        self.process: subprocess.Popen | None = None
        self.upstream: probe.UnixHTTPServer | None = None
        self.base_url: str | None = None

    def _cleanup(self) -> None:
        if self.process is not None:
            probe.stop_isolated_process(self.process)
        if self.upstream is not None:
            self.upstream.shutdown()
            self.upstream.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _start_proxy(self) -> None:
        _UpstreamCapture.requests = []
        upstream_port = _free_port()
        proxy_port = _free_port()
        upstream_socket = self.root / "upstream.sock"
        gateway_socket = self.root / "gateway.sock"
        self.upstream = probe.UnixHTTPServer(str(upstream_socket), _UpstreamCapture)
        threading.Thread(
            target=self.upstream.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        ).start()

        bundle = catalog.load_catalog(CATALOG_ROOT)
        import copy

        gateway = copy.deepcopy(bundle.docs["gateway"])
        gateway["gateway"]["base_url"] = f"http://127.0.0.1:{proxy_port}"
        providers = copy.deepcopy(bundle.docs["providers"]["providers"])
        providers["kimi"]["transport"]["base_url"] = (
            f"http://127.0.0.1:{upstream_port}/coding"
        )
        providers["openrouter"]["transport"]["base_url"] = (
            f"http://127.0.0.1:{upstream_port}/api"
        )
        result = render.render_config(
            gateway,
            providers,
            bundle.lines,
            continuity={},
            home=self.root,
            gateway_token=GATEWAY_TOKEN,
            resolve_secret=lambda name: (
                DUMMY_OPENROUTER
                if name == "OPENROUTER_CLAUDE_API_KEY"
                else DUMMY_KIMI
            ),
        )
        config_path = self.root / "config.yaml"
        state.atomic_write(config_path, result.yaml.encode("utf-8"))

        env = {
            "HOME": str(self.root),
            "PATH": "/usr/bin:/bin",
        }
        self.process = probe.start_isolated_process(
            [os.path.realpath(self.binary), "--config", str(config_path), "--local-model"],
            bridges=[probe.PortBridge(upstream_port, upstream_socket),
                     probe.PortBridge(proxy_port, gateway_socket, "ingress")],
            cwd=self.root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.base_url = f"http://127.0.0.1:{proxy_port}"
        class GatewayHandler(urllib.request.HTTPHandler):
            def http_open(self, request):
                return self.do_open(
                    lambda host, **kw: probe.UnixHTTPConnection(gateway_socket, **kw), request,
                )
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), GatewayHandler())
        _wait_ready(self.base_url, self.opener)

    def _request(self, base_url: str, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            base_url + path,
            data=data,
            headers={
                "Authorization": f"Bearer {GATEWAY_TOKEN}",
                "Anthropic-Version": "2023-06-01",
                "Content-Type": "application/json",
                # CLIProxyAPI enriches model metadata only for Claude CLI clients.
                "User-Agent": "claude-cli/2.1.216 (external, cli)",
            },
            method="POST" if payload is not None else "GET",
        )
        with self.opener.open(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_rendered_config_contracts_with_fake_upstream(self) -> None:
        self._start_proxy()
        base_url = self.base_url

        models = self._request(base_url, "/v1/models")
        entries = {item.get("id"): item for item in models.get("data", [])}
        self.assertIn("claude-multi-kimi-k3", entries)
        self.assertEqual(entries["claude-multi-kimi-k3"].get("owned_by"), "moonshot")
        self.assertEqual(
            entries["claude-multi-kimi-k3"].get("max_input_tokens"), 1000000
        )

        message = self._request(
            base_url,
            "/v1/messages",
            {
                "model": "claude-multi-kimi-k3",
                "max_tokens": 64,
                "thinking": {"type": "enabled", "budget_tokens": 1024},
                "messages": [{"role": "user", "content": "fake test"}],
            },
        )
        self.assertTrue(message.get("content"), "response mapped")

        kimi_hits = [
            request
            for request in _UpstreamCapture.requests
            if urllib.parse.urlsplit(request["path"]).path == "/coding/v1/messages"
        ]
        self.assertTrue(kimi_hits, "upstream observed the kimi route")
        self.assertEqual(kimi_hits[0]["x-api-key"], DUMMY_KIMI)
        body = json.loads(kimi_hits[0]["body"])
        self.assertNotIn("thinking", body, "payload filter strips top-level thinking")
        self.assertEqual(body.get("model"), "k3", "wire model mapping")
        self.assertEqual(body.get("output_config", {}).get("effort"), "max")

        # A second x-api-key direct route with its own secret, effort contract
        # and (slash-bearing) wire. The omitted-tool_choice invariant was first
        # pinned for DeepSeek Pro (thinking mode rejects a forced choice); the
        # gateway's claude-compatible path carries no model-name conditions,
        # so the fixture's openrouter line guards the same request shape.
        self._request(
            base_url,
            "/v1/messages",
            {
                "model": "claude-multi-grok46-xhigh",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "use the check tool"}],
                "tools": [
                    {
                        "name": "catalog19_check",
                        "description": "Record a local request-shape check.",
                        "input_schema": {
                            "type": "object",
                            "properties": {"status": {"type": "string"}},
                            "required": ["status"],
                        },
                    }
                ],
            },
        )
        second_hits = [
            request
            for request in _UpstreamCapture.requests
            if urllib.parse.urlsplit(request["path"]).path == "/api/v1/messages"
        ]
        self.assertTrue(second_hits, "upstream observed the second direct route")
        self.assertEqual(second_hits[0]["x-api-key"], DUMMY_OPENROUTER)
        body = json.loads(second_hits[0]["body"])
        self.assertEqual(body.get("model"), "x-ai/grok-4.6")
        self.assertEqual(body.get("output_config", {}).get("effort"), "xhigh")
        self.assertEqual(body.get("tools", [])[0].get("name"), "catalog19_check")
        self.assertNotIn(
            "tool_choice",
            body,
            "the gateway must not invent the forced choice rejected by thinking mode",
        )

        self.process.terminate()
        self.process.wait(timeout=5)
        self.assertIsNotNone(self.process.poll(), "no orphan proxy process")

    def test_sse_streaming_passthrough(self) -> None:
        self._start_proxy()
        request = urllib.request.Request(
            self.base_url + "/v1/messages",
            data=json.dumps(
                {
                    "model": "claude-multi-kimi-k3",
                    "max_tokens": 64,
                    "stream": True,
                    "messages": [{"role": "user", "content": "stream test"}],
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {GATEWAY_TOKEN}",
                "Anthropic-Version": "2023-06-01",
                "Content-Type": "application/json",
                "User-Agent": "claude-cli/2.1.216 (external, cli)",
            },
            method="POST",
        )
        with self.opener.open(request, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "")
            body = response.read().decode("utf-8")
        self.assertIn("text/event-stream", content_type)
        self.assertIn('"FAKE"', body)
        self.assertIn('"_OK"', body)
        # The Anthropic stream's terminal event reaches the client. (The fake
        # upstream also sends a non-Anthropic `data: [DONE]` trailer: 7.2.80
        # forwarded it, 7.3.15 ends the Claude passthrough at message_stop
        # and drops it. The protocol has no [DONE]; assert the terminal.)
        self.assertIn('"type": "message_stop"', body)

    def test_upstream_error_mapping(self) -> None:
        self._start_proxy()
        request = urllib.request.Request(
            self.base_url + "/v1/messages",
            data=json.dumps(
                {
                    "model": "claude-multi-kimi-k3",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "trigger-error-400"}],
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {GATEWAY_TOKEN}",
                "Anthropic-Version": "2023-06-01",
                "Content-Type": "application/json",
                "User-Agent": "claude-cli/2.1.216 (external, cli)",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.opener.open(request, timeout=10)
        self.assertEqual(raised.exception.code, 400)
        error_body = raised.exception.read().decode("utf-8")
        raised.exception.close()
        self.assertIn("invalid_request_error", error_body)
        self.assertIn("fake upstream rejection", error_body)


class InstanceListenerTests(unittest.TestCase):
    """A gateway this state root started and proves reloads without the
    ownership Attention: the reload check asks the root's own proof."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-instance-listener-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.state_root = self.root / "state" / "claude-multi"
        self.gateway = {"gateway": {"base_url": "http://127.0.0.1:18399", "health_path": "/healthz"}}

    def _fake(self, verdict, base_url="http://127.0.0.1:18399"):
        from claude_multi.platform import observation

        class Own:
            def __init__(self, **kwargs):
                self.base_url = base_url
                self.kwargs = kwargs

            def observe(self):
                return mock.Mock(listener=verdict)

        return Own

    def test_the_own_gateway_verdict_comes_from_the_state_roots_proof(self) -> None:
        from claude_multi import gateway_lifecycle, service

        ours = service.OwnerVerdict("ours", "the claude-multi gateway service")
        with mock.patch.object(gateway_lifecycle, "Gateway", self._fake(ours)), \
                mock.patch.object(service, "listener_owner", side_effect=AssertionError("manager-only verdict")):
            check = proxy.instance_listener(self.home, self.state_root, {"HOME": str(self.home)}, self.gateway)
            self.assertIs(check("http://127.0.0.1:18399"), ours)
        plain = service.OwnerVerdict("unknown", "another process of yours (unit ownership unconfirmed)")
        with mock.patch.object(gateway_lifecycle, "Gateway", self._fake(ours, "http://127.0.0.1:1")), \
                mock.patch.object(service, "listener_owner", return_value=plain):
            check = proxy.instance_listener(self.home, self.state_root, {"HOME": str(self.home)}, self.gateway)
            self.assertIs(check("http://127.0.0.1:18399"), plain)

    def test_a_proven_gateway_reload_prints_no_ownership_attention(self) -> None:
        from claude_multi import launch, service

        seen = []

        def models_get(base_url, token, timeout=1.5, *, owner_check=None, attention=None, require_proof=False):
            seen.append(owner_check)
            launch.check_listener(base_url, owner_check=owner_check, attention=attention)
            return 200, {"claude-multi-render-0123abcd"}

        for kind, attention_expected in (("ours", False), ("unknown", True)):
            seen.clear()
            verdict = service.OwnerVerdict(kind, "fixture")
            err = io.StringIO()
            with self.subTest(kind=kind), mock.patch.object(launch, "_default_models_get", models_get), \
                    redirect_stderr(err):
                outcome = proxy.await_sentinel(self.gateway, "t" * 64, "claude-multi-render-0123abcd",
                                               owner_check=lambda _base: verdict)
                self.assertEqual(outcome.status, "reloaded")
                self.assertEqual(len(seen), 1)
                self.assertEqual("could not be confirmed" in err.getvalue(), attention_expected)


class ProxyCommandTableTests(unittest.TestCase):
    """One command table: dispatch, its own help, the overall usage and
    option checks that run before anything is initialized or observed."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-proxy-table-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"HOME": str(self.root / "home"), "XDG_STATE_HOME": str(self.root / "state")}

    def _effects_forbidden(self):
        boom = AssertionError("an effect before the option check")
        return [mock.patch.object(proxy, name, side_effect=boom)
                for name in ("_entry_refusal", "load_bundle", "cmd_init", "cmd_status", "cmd_run", "cmd_login",
                             "cmd_snapshot_auth", "cmd_rotate_management_key", "cmd_disable_management_key")]

    def test_every_command_has_its_own_help_without_effects(self) -> None:
        import contextlib

        for command in proxy.PROXY_COMMANDS:
            for flag in proxy.HELP_ARGUMENTS:
                with self.subTest(command=command.name, flag=flag), contextlib.ExitStack() as stack:
                    for patch in self._effects_forbidden():
                        stack.enter_context(patch)
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(proxy.main([command.name, flag], environ=self.environ), 0)
                    self.assertTrue(out.getvalue().startswith(f"usage: claude-multi-proxy {command.name}"))
                    self.assertIn(command.description.splitlines()[0], out.getvalue())
        self.assertFalse((self.root / "home").exists())

    def test_unknown_options_are_refused_before_anything_runs(self) -> None:
        import contextlib

        cases = {"init": ["--bogus"], "status": ["--bogus"], "rotate-management-key": ["x"],
                 "disable-management-key": ["--yes"], "run": ["--bogus"], "claude-login": ["--bogus"],
                 "codex-device-login": ["--state-root", "relative"], "snapshot-auth": ["--bogus"]}
        self.assertEqual(set(cases), set(proxy.PROXY_COMMAND_NAMES))
        for name, args in cases.items():
            with self.subTest(command=name), contextlib.ExitStack() as stack:
                for patch in self._effects_forbidden():
                    stack.enter_context(patch)
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(proxy.main([name, *args], environ=self.environ), 1)
                self.assertIn(f"usage: claude-multi-proxy {name}", err.getvalue())
        self.assertFalse((self.root / "home").exists())

    def test_the_usage_lists_exactly_the_table_and_every_entry_dispatches(self) -> None:
        listed = [line.split()[0] for line in proxy.PROXY_USAGE.split("commands:\n", 1)[1].split("\n\n", 1)[0]
                  .splitlines() if line.startswith("  ") and not line.startswith("   ")]
        self.assertEqual(listed, [*proxy.PROXY_COMMAND_NAMES, "-v,"])
        valid = {"init": [], "status": [], "rotate-management-key": [], "disable-management-key": [],
                 "run": [], "claude-login": [], "codex-device-login": [], "snapshot-auth": []}
        handlers = {"init": "cmd_init", "status": "cmd_status", "rotate-management-key": "cmd_rotate_management_key",
                    "disable-management-key": "cmd_disable_management_key", "run": "cmd_run",
                    "claude-login": "cmd_login", "codex-device-login": "cmd_login",
                    "snapshot-auth": "cmd_snapshot_auth"}
        for name in proxy.PROXY_COMMAND_NAMES:
            with self.subTest(command=name), mock.patch.object(proxy, "_entry_refusal", return_value=None), \
                    mock.patch.object(proxy, handlers[name], return_value=42) as handler:
                self.assertEqual(proxy.main([name, *valid[name]], environ=self.environ), 42)
                handler.assert_called_once()
        for command in proxy.PROXY_COMMANDS:
            self.assertIn(command.audience, ("user", "service", "maintenance"))
            self.assertTrue(command.operations)


if __name__ == "__main__":
    unittest.main()


class ProxyControlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-proxyctl-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.home = self.root / "home"
        self.home.mkdir()
        os.chmod(self.home, 0o700)
        self.secrets = self.root / "secrets"
        state.ensure_private_dir(self.secrets)
        self.secret_file = self.secrets / "claude.env"
        self.environ = {
            "HOME": str(self.home),
            "CLAUDE_MULTI_SECRET_ENV": str(self.secret_file),
            "CLAUDE_MULTI_ASSETS": str(CATALOG_ROOT),
        }
        self.binary = self.root / "bin" / "cli-proxy-api"
        self.binary.parent.mkdir()
        self.binary.write_bytes(b"#!/bin/fake\n")
        self.binary.chmod(0o755)
        self.environ["CLAUDE_MULTI_PROXY_BIN"] = str(self.binary)

    def _write_secret(self, content: bytes = b"KIMI_CLAUDE_API_KEY=test-dummy-value-123\n"):
        state.atomic_write(self.secret_file, content)

    def set_up_gateway(self, home: Path | None = None) -> None:
        """Record the gateway's port in this home (the packaged one): the
        ``claude-multi-proxy`` command renders, runs and signs in only in a
        home whose gateway is set up."""

        base = catalog.load_catalog(CATALOG_ROOT).docs["gateway"]["gateway"]["base_url"]
        endpoint.write_config(home or self.home, endpoint.EndpointConfig(port=endpoint.port_of(base)))

    def _models_get(self, _base, _token):
        import re

        yaml = (proxy.config_dir(self.home) / "config.yaml").read_text()
        return 200, set(re.findall(r'alias: "(claude-multi-render-[0-9a-f]+)"', yaml))

    def _init(self) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = proxy.cmd_init([], environ=self.environ, models_get=self._models_get)
        self.assertEqual(code, 0)
        return buffer.getvalue()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class InterruptTests(ProxyControlTestCase):
    """Ctrl-C in the gateway tool: one cancellation line, exit 130, and the
    state root's writer fence released, never a traceback."""

    def setUp(self) -> None:
        super().setUp()
        self.set_up_gateway()
        self.state_root = self.home / ".local" / "state" / "claude-multi"

    def fence_free(self) -> bool:
        from claude_multi import gateway_inhibition

        lock = gateway_inhibition._fence_lock(self.state_root, shared=False, create=False)
        if lock is None or not lock.acquire(blocking=False):
            return False
        lock.release()
        return True

    def run_interrupted(self, argv, **seams) -> tuple[int, str]:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = proxy.main(argv, environ=self.environ, **seams)
        return code, err.getvalue()

    def test_an_interrupted_start_check_exits_130_and_releases_the_fence(self) -> None:
        held: list[bool] = []

        def not_ready_yet(_base, _token):
            held.append(not self.fence_free())  # the command holds the fence while it runs
            return 200, set()

        def interrupted(_seconds):
            raise KeyboardInterrupt

        clock = FakeClock()
        code, err = self.run_interrupted(["init", "--start-check"], models_get=not_ready_yet, clock=clock,
                                         sleep=interrupted)
        self.assertEqual(code, 130, err)
        self.assertEqual(held, [True])
        self.assertEqual(err.strip(), "claude-multi-proxy: init cancelled")
        self.assertTrue(self.fence_free())

    def test_an_interrupted_render_exits_130_and_releases_the_fence(self) -> None:
        with mock.patch.object(proxy, "_publish_prepared_render", side_effect=KeyboardInterrupt):
            code, err = self.run_interrupted(["init"])
        self.assertEqual(code, 130, err)
        self.assertEqual(err.strip(), "claude-multi-proxy: init cancelled")
        self.assertNotIn("Traceback", err)
        self.assertTrue(self.fence_free())


class ReloadTests(ProxyControlTestCase):
    def test_init_reloaded(self):
        self.assertIn("gateway: reloaded (sentinel claude-multi-render-", self._init())

    def test_init_wait_outcomes_and_deadline(self):
        self.set_up_gateway()
        for status, expected in ((200, "restart required"), (401, "token mismatch")):
            clock = FakeClock()
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = proxy.main(
                    ["init"], environ=self.environ,
                    models_get=lambda _base, _token: (status, set()),
                    clock=clock, sleep=clock.sleep,
                )
            self.assertEqual(code, 0)
            self.assertIn(expected, err.getvalue())
            self.assertEqual(clock.now, 2.0)

    def test_gateway_down_is_informational_and_exception_redacted(self):
        def down(_base, token):
            raise ConnectionRefusedError(token)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(proxy.cmd_init([], environ=self.environ, models_get=down), 0)
        self.assertIn("not running", out.getvalue())
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn(proxy.gateway_api_keys(self.home)[0], out.getvalue())

    def test_non_refusal_query_errors_are_unverified_and_redacted(self):
        for error_type in (TimeoutError, ConnectionResetError, ValueError):
            with self.subTest(error_type=error_type):
                clock = FakeClock()
                getter = mock.Mock(side_effect=error_type("secret-query-detail"))
                result = proxy.await_sentinel(
                    {"gateway": {"base_url": "unused"}}, "dummy", "sentinel",
                    models_get=getter, clock=clock, sleep=clock.sleep,
                )
                self.assertEqual(result.status, "restart_required")
                self.assertIn("models check failed", result.message)
                self.assertNotIn("secret-query-detail", result.message)
                self.assertNotIn("not running", result.message)
                self.assertGreater(getter.call_count, 1)
                self.assertEqual(clock.now, 2.0)

    def test_transient_query_error_can_recover_before_deadline(self):
        clock = FakeClock()
        getter = mock.Mock(side_effect=[TimeoutError("private"), (200, {"sentinel"})])
        result = proxy.await_sentinel(
            {"gateway": {"base_url": "unused"}}, "dummy", "sentinel",
            models_get=getter, clock=clock, sleep=clock.sleep,
        )
        self.assertEqual(result.status, "reloaded")
        self.assertEqual(clock.now, 0.1)

    def test_no_listener_poll_is_down_without_attention(self):
        err = io.StringIO()
        with mock.patch.object(proxy.service, "listener_owner",
                               return_value=proxy.service.OwnerVerdict("none", "no listener")), \
                mock.patch.object(proxy.launch_mod.http.client, "HTTPConnection",
                                  side_effect=ConnectionRefusedError), redirect_stderr(err):
            result = proxy.await_sentinel(
                {"gateway": {"base_url": "http://127.0.0.1:18317"}}, "dummy", "sentinel")
        self.assertEqual(result.status, "down")
        self.assertEqual(err.getvalue(), "")

    def test_default_polling_reports_unknown_owner_once_but_rechecks(self):
        for kind in ("unknown", "none"):
            with self.subTest(kind=kind):
                clock = FakeClock()
                connection = mock.MagicMock()
                response = connection.getresponse.return_value
                response.status = 200
                response.read.return_value = b'{"data": []}'
                owner = mock.Mock(return_value=proxy.service.OwnerVerdict(kind, "fixture owner"))
                err = io.StringIO()
                with mock.patch.object(proxy.service, "listener_owner", owner), \
                        mock.patch.object(proxy.launch_mod.http.client, "HTTPConnection", return_value=connection), \
                        redirect_stderr(err):
                    result = proxy.await_sentinel(
                        {"gateway": {"base_url": "http://127.0.0.1:8317"}}, "fixture-token", "sentinel",
                        clock=clock, sleep=clock.sleep,
                    )
                self.assertEqual(result.status, "restart_required")
                self.assertEqual(owner.call_count, connection.request.call_count)
                self.assertGreater(owner.call_count, 1)
                self.assertEqual(err.getvalue().count("Attention:"), 1 if kind == "unknown" else 0)

    def test_listener_change_during_polling_never_sends_to_foreign_owner(self):
        clock = FakeClock()
        connection = mock.MagicMock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'{"data": []}'
        verdicts = iter([proxy.service.OwnerVerdict("none", "no listener")])
        owner = lambda _: next(verdicts, proxy.service.OwnerVerdict("foreign", "another user"))
        with mock.patch.object(proxy.service, "listener_owner", owner), \
                mock.patch.object(proxy.launch_mod.http.client, "HTTPConnection", return_value=connection):
            result = proxy.await_sentinel(
                {"gateway": {"base_url": "http://127.0.0.1:8317"}}, "fixture-token", "sentinel",
                clock=clock, sleep=clock.sleep,
            )
        self.assertEqual(result.status, "restart_required")
        connection.request.assert_called_once()

    def test_401_during_reload_is_retried(self):
        clock = FakeClock()
        getter = mock.Mock(side_effect=[(401, set()), (200, {"sentinel"})])
        result = proxy.await_sentinel(
            {"gateway": {"base_url": "unused"}}, "dummy", "sentinel",
            models_get=getter, clock=clock, sleep=clock.sleep,
        )
        self.assertEqual(result.status, "reloaded")
        self.assertEqual(clock.now, 0.1)


class StartReadinessTests(ProxyControlTestCase):
    """Explicit (re)start readiness, fake clock only.

    After a (re)start the render sentinel is registered before the listener
    while the OAuth pools register later, so start mode also needs one
    rendered OAuth alias; hot reloads keep the sentinel-only 2 s check.
    """

    GATEWAY = {"gateway": {"base_url": "unused"}}
    SENTINEL = "claude-multi-render-0123abcd"
    ALIASES = frozenset({"fixture-oauth-a", "fixture-oauth-b"})

    def _wait(self, responses, clock, **kwargs):
        def getter(_base, _token):
            item = responses(clock.now)
            if isinstance(item, BaseException):
                raise item
            return item
        return proxy.await_sentinel(self.GATEWAY, "dummy", self.SENTINEL, models_get=getter,
                                    clock=clock, sleep=clock.sleep, **kwargs)

    def test_prolonged_refusal_is_retried_within_the_start_budget(self):
        clock = FakeClock()
        # Refused for 30 s (the pre-listen window of a slow start), then ready.
        responses = lambda now: (ConnectionRefusedError("private") if now < 30
                                 else (200, {self.SENTINEL, "fixture-oauth-b"}))
        result = self._wait(responses, clock, mode="start", oauth_aliases=self.ALIASES)
        self.assertEqual(result.status, "ready")
        self.assertIn("fixture-oauth-b", result.message)
        self.assertGreaterEqual(clock.now, 30)
        self.assertLess(clock.now, proxy.START_READINESS_TIMEOUT)
        # Refused throughout: an explicit failure naming the listener, at the budget.
        clock = FakeClock()
        result = self._wait(lambda _now: ConnectionRefusedError("private"), clock,
                            mode="start", oauth_aliases=self.ALIASES)
        self.assertEqual(result.status, "not_ready")
        self.assertAlmostEqual(clock.now, proxy.START_READINESS_TIMEOUT, places=6)
        self.assertIn("still refused connections", result.message)
        self.assertNotIn("private", result.message)

    def test_sentinel_before_oauth_waits_for_an_oauth_alias(self):
        clock = FakeClock()
        # The sentinel is config-synthesized (served at once); the OAuth pool
        # registers 12 s later.
        responses = lambda now: (200, {self.SENTINEL} | ({"fixture-oauth-a"} if now >= 12 else set()))
        result = self._wait(responses, clock, mode="start", oauth_aliases=self.ALIASES)
        self.assertEqual(result.status, "ready")
        self.assertIn("OAuth alias fixture-oauth-a served", result.message)
        self.assertGreaterEqual(clock.now, 12)
        # The same sequence in reload mode is satisfied by the sentinel alone.
        clock = FakeClock()
        self.assertEqual(self._wait(responses, clock).status, "reloaded")
        self.assertEqual(clock.now, 0)

    def test_render_without_oauth_aliases_needs_only_the_sentinel(self):
        for aliases in ((), frozenset({self.SENTINEL})):
            with self.subTest(aliases=aliases):
                clock = FakeClock()
                responses = lambda now: (200, {self.SENTINEL} if now >= 3 else set())
                result = self._wait(responses, clock, mode="start", oauth_aliases=aliases)
                self.assertEqual(result.status, "ready")
                self.assertIn("the render has no OAuth alias", result.message)
                self.assertGreaterEqual(clock.now, 3)

    def test_deadline_expiry_names_the_missing_piece(self):
        cases = (
            ((200, {"claude-multi-render-old"}), "render sentinel claude-multi-render-0123abcd is not served"),
            ((200, {self.SENTINEL}), "none of the 2 rendered OAuth aliases is served"),
            ((401, set()), "rejects the current key"),
            ((503, set()), "answered HTTP 503"),
            (TimeoutError("private"), "models check kept failing"),
        )
        for response, missing in cases:
            with self.subTest(missing=missing):
                clock = FakeClock()
                result = self._wait(lambda _now: response, clock, mode="start",
                                    oauth_aliases=self.ALIASES, timeout=35)
                self.assertEqual(result.status, "not_ready")
                # No 2 s clamp in start mode: the whole explicit budget is spent.
                self.assertAlmostEqual(clock.now, 35, places=6)
                self.assertIn("not ready 35 s after a (re)start", result.message)
                self.assertIn(missing, result.message)
                self.assertNotIn("private", result.message)
        clock = FakeClock()
        result = self._wait(lambda _now: (200, {self.SENTINEL}), clock, mode="start",
                            oauth_aliases=self.ALIASES)
        self.assertAlmostEqual(clock.now, proxy.START_READINESS_TIMEOUT, places=6)
        self.assertEqual(proxy.START_READINESS_TIMEOUT, 45.0)

    def test_hot_reload_behaviour_is_unchanged(self):
        # The 2 s clamp holds whatever timeout says; refusal is informational.
        clock = FakeClock()
        result = self._wait(lambda _now: (200, set()), clock, timeout=35)
        self.assertEqual(result.status, "restart_required")
        self.assertEqual(clock.now, 2.0)
        clock = FakeClock()
        calls = []
        def refused(now):
            calls.append(now)
            return ConnectionRefusedError()
        self.assertEqual(self._wait(refused, clock, timeout=35).status, "down")
        self.assertEqual((clock.now, len(calls)), (0, 1))
        clock = FakeClock()
        result = self._wait(lambda _now: (200, {self.SENTINEL}), clock, oauth_aliases=self.ALIASES)
        self.assertEqual(result.status, "reloaded")
        with self.assertRaisesRegex(ValueError, "readiness mode"):
            self._wait(lambda _now: (200, set()), FakeClock(), mode="restart")

    def test_start_mode_default_polling_waits_on_the_gate(self):
        # A gated /v1/models request may be held up to 30 s by the gateway;
        # start mode gives one request up to 5 s instead of hot reload's 0.25 s.
        for mode, expected in (("start", proxy.START_REQUEST_TIMEOUT), ("reload", 0.25)):
            with self.subTest(mode=mode):
                getter = mock.Mock(return_value=(200, {self.SENTINEL}))
                with mock.patch.object(proxy.launch_mod, "_default_models_get", getter):
                    proxy.await_sentinel(self.GATEWAY, "dummy", self.SENTINEL, mode=mode,
                                         clock=FakeClock(), sleep=lambda _s: None)
                self.assertEqual(getter.call_args.kwargs["timeout"], expected)

    def test_render_result_carries_the_rendered_oauth_aliases(self):
        self._init()
        _, result, _ = proxy.render_runtime_config(self.home, environ=self.environ)
        yaml = (proxy.config_dir(self.home) / "config.yaml").read_text()
        self.assertTrue(result.oauth_aliases, "the fixture renders OAuth pools")
        self.assertEqual(list(result.oauth_aliases), sorted(result.oauth_aliases))
        self.assertNotIn(result.sentinel, result.oauth_aliases)
        for alias in result.oauth_aliases:
            self.assertIn(f'alias: "{alias}"', yaml)
        self.assertEqual(render.rendered_oauth_aliases({"oauth-model-alias": {
            "pool": [{"alias": "b"}, {"alias": "a"}, {"alias": render.SENTINEL_PREFIX + "x"}]}}), ("a", "b"))

    def _start_check(self, getter, *extra):
        clock = FakeClock()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = proxy.main(["init", *extra, "--start-check"], environ=self.environ,
                              models_get=getter, clock=clock, sleep=clock.sleep)
        return code, out.getvalue(), err.getvalue(), clock.now

    def test_init_start_check_exit_status_stamps_and_render_aliases(self):
        self.set_up_gateway()
        workdir = proxy.gateway_workdir(sessions.state_root(self.environ))
        seen = []
        def getter(_base, token):
            yaml = (proxy.config_dir(self.home) / "config.yaml").read_text()
            import re
            sentinel = set(re.findall(r'alias: "(claude-multi-render-[0-9a-f]+)"', yaml))
            seen.append(token)
            return 200, sentinel
        code, _out, err, now = self._start_check(getter)
        self.assertEqual(code, proxy.START_CHECK_FAILED)
        self.assertEqual(now, proxy.START_READINESS_TIMEOUT)
        self.assertIn("gateway: not ready 45 s after a (re)start: none of the", err)
        self.assertIn("rendered OAuth aliases is served", err)
        self.assertNotIn(seen[0], err)
        # A failed start check records no reload status (it is not a reload).
        self.assertIsNone(proxy.service.read_reload_stamp(workdir))
        self.assertFalse((workdir / proxy.service.RELOAD_ATTEMPT).exists())

        captured = {}
        real = proxy.await_sentinel
        def spy(*args, **kwargs):
            captured.update(kwargs)
            return real(*args, **kwargs)
        def ready(_base, _token):
            status, ids = getter(_base, _token)
            return status, ids | {captured["oauth_aliases"][0]}
        with mock.patch.object(proxy, "await_sentinel", side_effect=spy):
            code, out, err, now = self._start_check(ready, "--state-root", str(sessions.state_root(self.environ)))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(now, 0)
        self.assertIn("gateway: ready (sentinel claude-multi-render-", out)
        self.assertEqual(captured["mode"], "start")
        _, result, _ = proxy.render_runtime_config(self.home, environ=self.environ)
        self.assertEqual(captured["oauth_aliases"], result.oauth_aliases)
        self.assertEqual(proxy.service.read_reload_stamp(workdir)["status"], "reloaded")

    def test_init_without_start_check_keeps_informational_down(self):
        self.set_up_gateway()
        # The unit's ExecStartPre runs a config-only init before the gateway
        # exists: refused stays an immediate, informational "down", exit 0.
        clock = FakeClock()
        def refused(_base, _token):
            raise ConnectionRefusedError()
        with redirect_stdout(io.StringIO()) as out:
            code = proxy.main(["init"], environ=self.environ, models_get=refused,
                              clock=clock, sleep=clock.sleep)
        self.assertEqual((code, clock.now), (0, 0))
        self.assertIn("not running", out.getvalue())

    def test_init_mode_flags_are_exclusive(self):
        for args in (["--start-check", "--reload-check"], ["--start-check", "--start-check"],
                     ["--start-check", "--state-root", "relative"]):
            with self.subTest(args=args), self.assertRaisesRegex(
                    proxy.ProxyError, r"init \[--reload-check \| --start-check \| --prepare-start\]"):
                proxy.cmd_init(args, environ=self.environ)
        self.assertIn("--start-check", proxy.PROXY_USAGE)
        self.assertIn("6 not ready", proxy.PROXY_USAGE)


class RotationTests(ProxyControlTestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.directory = proxy.config_dir(self.home)
        state.ensure_private_dir(self.directory)
        self.old = "a" * 64
        self.key = self.directory / "api-key"
        self.previous = self.directory / "previous-key"
        state.atomic_write(self.key, (self.old + "\n").encode())
        self.config = self.directory / "config.yaml"
        self.seen_keys = []

    def assert_api_lock_free(self):
        lock = state.FileLock(self.key)
        self.assertTrue(lock.acquire(blocking=False), "api-key lock held during wait/getter")
        lock.release()

    def getter(self, _base, token):
        import re

        self.assert_api_lock_free()
        yaml = self.config.read_text()
        block = yaml.split("api-keys:\n", 1)[1].split("debug:", 1)[0]
        keys = re.findall(r'"([0-9a-f]{64})"', block)
        self.seen_keys.append(tuple(keys))
        ids = set(re.findall(r'alias: "(claude-multi-render-[0-9a-f]+)"', yaml))
        return (200, ids) if token in keys else (401, set())

    def sleep(self, seconds):
        self.assert_api_lock_free()
        self.clock.sleep(seconds)

    def rotate(self, **kwargs):
        options = dict(
            environ=self.environ, resolver=lambda _: None,
            models_get=self.getter, clock=self.clock, sleep=self.sleep,
            live_sessions=lambda: (),
        )
        options.update(kwargs)
        return proxy.rotate_token(self.home, **options)

    def test_rotation_publishes_after_dual_then_retires_old_after_ttl(self):
        observed = []
        def sleep(seconds):
            self.assert_api_lock_free()
            observed.append(proxy.gateway_api_keys(self.home))
            self.assertEqual(self.previous.stat().st_mode & 0o777, 0o600)
            self.clock.sleep(seconds)
        result = self.rotate(sleep=sleep, margin=7)
        new = proxy.gateway_api_keys(self.home)[0]
        self.assertEqual(result.status, "reloaded")
        self.assertNotEqual(self.old, new)
        self.assertEqual(self.clock.now, 307)
        # Without a progress reporter the wait stays one sleep.
        self.assertEqual(observed, [(self.old, new)])
        self.assertEqual(self.seen_keys, [(self.old, new), (new,), (new,)])
        self.assertFalse(self.previous.exists())
        self.assertEqual(self.getter("unused", self.old)[0], 401)

    # Honest repeated-wait UX, unchanged algorithm.

    def test_resumed_rotation_explains_full_ttl(self):
        # A published rotation (previous-key != api-key) whose files are an
        # hour old: the wait is never inferred from mtimes, it starts again.
        state.atomic_write(self.previous, (self.old + "\n").encode())
        new = "b" * 64
        state.atomic_write(self.key, (new + "\n").encode())
        hour_ago = time.time() - 3600
        for path in (self.previous, self.key):
            os.utime(path, (hour_ago, hour_ago))
        lines = []
        self.rotate(progress=lines.append)
        self.assertEqual(lines[:3], [
            "Resuming a published token rotation.",
            "The previous wait is not persisted, so the full 305s safety window starts again.",
            "No key has been discarded.",
        ])
        self.assertIn("step 3/4: Waiting 305s for the helper cache window: 300s TTL + 5s safety margin.", lines)
        self.assertIn("Both gateway keys remain accepted during this wait.", lines)
        self.assertEqual(self.clock.now, 305)  # the full window, again
        self.assertEqual(proxy.gateway_api_keys(self.home), (new,))
        # A fresh rotation never claims to resume.
        fresh = []
        self.rotate(progress=fresh.append)
        self.assertNotIn("Resuming a published token rotation.", fresh)

    def test_cancelled_wait_preserves_dual_key_state(self):
        calls = []

        def sleep(seconds):
            calls.append(seconds)
            if len(calls) == 3:
                raise KeyboardInterrupt
            self.clock.sleep(seconds)

        lines = []
        with self.assertRaises(KeyboardInterrupt):
            self.rotate(sleep=sleep, progress=lines.append)
        keys = proxy.gateway_api_keys(self.home)
        self.assertEqual(len(keys), 2)  # both keys still accepted
        self.assertEqual(keys[0], self.old)
        self.assertEqual(self.previous.read_text().strip(), self.old)
        self.assertEqual(self.key.read_text().strip(), keys[1])
        self.assertIn('  - "' + self.old + '"\n  - "' + keys[1] + '"', self.config.read_text())
        self.assertEqual(lines[-1], proxy.ROTATION_WAIT_CANCELLED.format(total="305"))
        # The rotation lock is released and a rerun resumes and completes.
        resumed = []
        self.rotate(progress=resumed.append)
        self.assertEqual(resumed[0], "Resuming a published token rotation.")
        self.assertEqual(proxy.gateway_api_keys(self.home), (keys[1],))
        self.assertFalse(self.previous.exists())

    def test_rotation_progress_uses_monotonic_time(self):
        import inspect

        self.assertIs(inspect.signature(proxy.rotate_token).parameters["clock"].default, time.monotonic)
        lines = []
        with mock.patch.object(proxy.time, "time", side_effect=AssertionError("wall clock read")):
            self.rotate(progress=lines.append)
        remaining = [line for line in lines if line.startswith("  helper cache window: ")]
        self.assertEqual(remaining, [
            proxy.ROTATION_REMAINING.format(remaining=value) for value in (245, 185, 125, 65, 5)
        ])
        done = lines.index(proxy.ROTATION_WAIT_DONE.format(total="305"))
        self.assertTrue(lines[done + 1].startswith("step 4/4"))
        # A wall-clock jump (the injected clock only) never shortens the wait.
        jumps = []

        def sleep(seconds):
            jumps.append(seconds)
            self.clock.sleep(seconds)

        self.rotate(sleep=sleep, progress=lambda _line: None)
        self.assertEqual(sum(jumps), 305)
        self.assertLessEqual(max(jumps), proxy.ROTATION_PROGRESS_SECONDS)

    def test_live_session_refusal_preserves_dual_then_confirmation_resumes(self):
        with self.assertRaisesRegex(ProxyError, "session-one.*rotation paused"):
            self.rotate(live_sessions=lambda: ["session-one"])
        keys = proxy.gateway_api_keys(self.home)
        self.assertEqual(len(keys), 2)
        prompts = []
        self.rotate(live_sessions=lambda: ["session-one"],
                    confirm=lambda message: prompts.append(message) or True)
        self.assertIn("session-one", prompts[0])
        self.assertEqual(proxy.gateway_api_keys(self.home), (keys[-1],))
        self.assertEqual(self.clock.now, 610)

    def test_resume_published_window_renders_same_keys_on_runtime_init(self):
        state.atomic_write(self.previous, (self.old + "\n").encode())
        new = "b" * 64
        state.atomic_write(self.key, (new + "\n").encode())
        _, first, _report = proxy.render_runtime_config(self.home, environ=self.environ, resolver=lambda _: None)
        self.assertIn('  - "' + self.old + '"\n  - "' + new + '"', first.yaml)
        self.rotate()
        self.assertEqual(proxy.gateway_api_keys(self.home), (new,))

    def test_restart_fallback_requires_confirmation_and_positive_retry(self):
        for status in (200, 401):
            with self.subTest(status=status), self.assertRaisesRegex(ProxyError, "restart required"):
                self.rotate(models_get=lambda _base, _token: (status, set()))
            self.assertEqual(proxy.gateway_api_keys(self.home), (self.old,))
        with self.assertRaisesRegex(ProxyError, "still unverified"):
            self.rotate(models_get=lambda _base, _token: (200, set()), confirm=lambda _: True)
        self.assertEqual(proxy.gateway_api_keys(self.home), (self.old,))
        confirmed = []
        def getter(base, token):
            return self.getter(base, token) if confirmed else (200, set())
        self.rotate(models_get=getter, confirm=lambda message: confirmed.append(message) or True)
        self.assertEqual(len(confirmed), 1)

    def test_confirmed_cmd_run_restart_reapplies_each_stage_and_refreshes_proof(self):
        confirmed = set()
        regenerated = []
        secret = [None]

        def stage():
            return "dual" if len(proxy.gateway_api_keys(self.home)) == 1 else "single"

        def getter(base, token):
            if stage() not in confirmed:
                return 200, set()
            return self.getter(base, token)

        def confirm(message):
            self.assertIn("restart required", message)
            self.assert_api_lock_free()
            phase = stage()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                proxy.cmd_run([], environ=self.environ, execve=lambda *_: None)
            # cmd_run overwrote the stage: old-only before publish; dual while
            # retiring. Rotation must restore the intended stage after this.
            self.getter("unused", self.old)
            regenerated.append(len(self.seen_keys[-1]))
            secret[0] = "changed-provider-secret" if phase == "dual" else None
            confirmed.add(phase)
            return True

        self.rotate(models_get=getter, confirm=confirm, resolver=lambda _: secret[0])
        self.assertEqual(regenerated, [1, 2])
        self.assertEqual(confirmed, {"dual", "single"})
        self.assertFalse(self.previous.exists())
        self.assertEqual(self.getter("unused", self.old)[0], 401)

    def test_final_reload_fallback_never_removes_previous_slot(self):
        def getter(base, token):
            if len(proxy.gateway_api_keys(self.home)) == 2:
                return 200, set()
            return self.getter(base, token)
        with self.assertRaisesRegex(ProxyError, "restart required"):
            self.rotate(models_get=getter)
        self.assertTrue(self.previous.exists())
        self.rotate()
        self.assertFalse(self.previous.exists())

    def test_old_token_verification_is_mandatory(self):
        for old_status in (200, 403, 500):
            with self.subTest(old_status=old_status):
                def getter(base, token):
                    status, ids = self.getter(base, token)
                    return (old_status, ids) if status == 401 else (status, ids)
                with self.assertRaisesRegex(ProxyError, "previous token"):
                    self.rotate(models_get=getter)
                self.assertTrue(self.previous.exists())
        self.rotate()

    # Unit, init and rotation renders never take or
    # assert the served-change barrier; rotation never moves the authority.
    @staticmethod
    def _foreign_barrier(home):
        import contextlib
        import fcntl

        @contextlib.contextmanager
        def held():
            directory = state.ensure_private_dir(Path(home) / ".config" / "claude-multi")
            descriptor = os.open(directory / "served-change.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)

        return held()

    def test_render_unit_init_rotation_never_acquire_barrier(self):
        self._write_secret()
        acquire = mock.Mock(side_effect=AssertionError("a render took the served-change barrier"))
        root = str(self.root / "state" / "claude-multi")
        with self._foreign_barrier(self.home), mock.patch.object(state, "acquire_served_barrier", acquire), \
                mock.patch.object(state, "require_barrier", side_effect=AssertionError("asserted a barrier")):
            started = time.monotonic()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(proxy.cmd_init(["--prepare-start", "--state-root", root],
                                                environ=self.environ, models_get=self.getter), 0)
                self.assertEqual(proxy.cmd_init(["--state-root", root], environ=self.environ,
                                                models_get=self.getter), 0)
            proxy.render_runtime_config(self.home, environ=self.environ, state_root=Path(root), policy="start")
            self.rotate(state_root=Path(root))
            self.assertLess(time.monotonic() - started, 30)
        acquire.assert_not_called()
        self.assertTrue(self.previous.exists() or self.key.read_text().strip() != self.old)

    def test_rotation_from_other_root_does_not_move_authority(self):
        self._write_secret()
        from claude_multi import continuity

        managed = self.root / "state" / "claude-multi"
        proxy.render_runtime_config(self.home, environ=self.environ, state_root=managed)
        before = continuity.path(self.home).read_bytes()
        self.assertEqual(continuity.read(self.home)["state_root"], str(managed))
        other = self.root / "shell-xdg-state" / "claude-multi"
        self.rotate(state_root=other)
        self.assertEqual(continuity.read(self.home)["state_root"], str(managed))
        self.assertEqual(continuity.path(self.home).read_bytes(), before)

    def test_rotation_lock_excludes_second_writer(self):
        lock = state.FileLock(self.directory / "token-rotation")
        lock.acquire()
        try:
            # The neutral text — a served change or a prune
            # holds the same lock as a rotation.
            with self.assertRaisesRegex(ProxyError, "another gateway operation is in progress"):
                self.rotate()
        finally:
            lock.release()
        self.assertFalse(self.previous.exists())

    def test_concurrent_init_before_publish_refuses_stale_candidate(self):
        def getter(base, token):
            result = self.getter(base, token)
            proxy.render_runtime_config(self.home, environ=self.environ, resolver=lambda _: None)
            return result
        with self.assertRaisesRegex(ProxyError, "config changed"):
            self.rotate(models_get=getter)
        self.assertEqual(proxy.gateway_api_keys(self.home), (self.old,))

    def test_concurrent_init_before_cleanup_keeps_previous_slot(self):
        def getter(base, token):
            result = self.getter(base, token)
            if result[0] == 401:
                proxy.render_runtime_config(self.home, environ=self.environ, resolver=lambda _: None)
            return result
        with self.assertRaisesRegex(ProxyError, "config changed"):
            self.rotate(models_get=getter)
        self.assertTrue(self.previous.exists())

    def test_crash_at_each_durable_boundary_is_reentrant(self):
        class Crash(BaseException):
            pass
        original_write, original_remove = state.atomic_write, state.remove_private
        for boundary in range(1, 6):
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after):
                    if self.previous.exists():
                        original_remove(self.previous)
                    original_write(self.key, (self.old + "\n").encode())
                    count = 0
                    def operation(fn, *args):
                        nonlocal count
                        count += 1
                        if count == boundary and not after:
                            raise Crash()
                        value = fn(*args)
                        if count == boundary and after:
                            raise Crash()
                        return value
                    with mock.patch.object(state, "atomic_write", side_effect=lambda *a: operation(original_write, *a)), \
                         mock.patch.object(state, "remove_private", side_effect=lambda *a: operation(original_remove, *a)):
                        with self.assertRaises(Crash):
                            self.rotate()
                    self.assert_api_lock_free()
                    self.rotate()
                    self.assertFalse(self.previous.exists())
                    current = proxy.gateway_api_keys(self.home)[0]
                    self.assertEqual(self.getter("unused", current)[0], 200)
                    self.assertEqual(self.getter("unused", self.old)[0], 401)

    def test_owner_read_only_key_slots_are_accepted(self):
        # One slot rule with launch.read_gateway_token —
        # read_private's owner-only check; a hardened 0400 key still works.
        self.key.chmod(0o400)
        state.atomic_write(self.previous, (("b" * 64) + "\n").encode())
        self.previous.chmod(0o400)
        self.assertEqual(proxy.gateway_api_keys(self.home), ("b" * 64, self.old))

    def test_render_refusal_is_one_line_not_a_traceback(self):
        # `run` is the unit's ExecStart; a RenderError must
        # be a one-line exit 1, never a crash-looping traceback.
        err = io.StringIO()
        with mock.patch.object(
            proxy, "render_runtime_config",
            side_effect=proxy.render_mod.RenderError("provider id 'claude-multi-render' is reserved"),
        ), redirect_stderr(err):
            code = proxy.main(["run"], environ=self.environ, execve=lambda *_: None)
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("reserved", err.getvalue())

    def test_key_slots_fail_closed_and_never_echo_values(self):
        self.assertEqual(proxy.gateway_api_keys(environ=self.environ), (self.old,))
        state.atomic_write(self.previous, (self.old + "\n").encode())
        self.assertEqual(proxy.gateway_api_keys(self.home), (self.old,))
        for bad in (b"SECRET-NOT-A-TOKEN", b"\xff" * 64):
            state.atomic_write(self.previous, bad)
            with self.assertRaises(ProxyError) as raised:
                proxy.gateway_api_keys(self.home)
            self.assertNotIn("SECRET-NOT-A-TOKEN", str(raised.exception))
        state.atomic_write(self.previous, ("b" * 64).encode())
        self.previous.chmod(0o640)
        with self.assertRaises(ProxyError):
            proxy.gateway_api_keys(self.home)
        self.previous.unlink()
        self.previous.symlink_to(self.directory / "missing")
        with self.assertRaises(ProxyError):
            proxy.gateway_api_keys(self.home)

    def test_negative_or_nonfinite_margin_refused_before_writes(self):
        for margin in (-1, float("inf"), float("nan")):
            with self.assertRaises(ProxyError):
                self.rotate(margin=margin)
        self.assertFalse(self.previous.exists())


class SecretParsingTests(ProxyControlTestCase):
    def test_valid_assignments(self) -> None:
        self._write_secret(b'KIMI_CLAUDE_API_KEY=abc-123_X.Y\nOTHER="quoted:value"\n')
        values = proxy.parse_secret_env(self.secret_file)
        self.assertEqual(values["KIMI_CLAUDE_API_KEY"], "abc-123_X.Y")
        self.assertEqual(values["OTHER"], "quoted:value")

    def test_export_prefix_and_single_quotes(self) -> None:
        self._write_secret(b"export KIMI_CLAUDE_API_KEY='abc+def/ghi='\n")
        values = proxy.parse_secret_env(self.secret_file)
        self.assertEqual(values["KIMI_CLAUDE_API_KEY"], "abc+def/ghi=")

    def test_duplicate_assignment_rejected(self) -> None:
        self._write_secret(b"A=11111111\nA=22222222\n")
        with self.assertRaisesRegex(ProxyError, "duplicate assignment"):
            proxy.parse_secret_env(self.secret_file)

    def test_malformed_line_rejected(self) -> None:
        self._write_secret(b"this is not an assignment\n")
        with self.assertRaisesRegex(ProxyError, "malformed assignment"):
            proxy.parse_secret_env(self.secret_file)

    def test_unsafe_characters_rejected(self) -> None:
        self._write_secret(b"A=$(rm -rf /)\n")
        with self.assertRaisesRegex(ProxyError, "unsupported characters"):
            proxy.parse_secret_env(self.secret_file)

    def test_symlink_secret_file_rejected(self) -> None:
        self._write_secret()
        link = self.secrets / "link.env"
        link.symlink_to(self.secret_file)
        with self.assertRaisesRegex(ProxyError, "unavailable or unsafe"):
            proxy.parse_secret_env(link)

    def test_group_readable_secret_file_rejected(self) -> None:
        self._write_secret()
        os.chmod(self.secret_file, 0o640)
        with self.assertRaisesRegex(ProxyError, "unavailable or unsafe"):
            proxy.parse_secret_env(self.secret_file)

    def test_missing_secret_returns_none(self) -> None:
        self.assertIsNone(proxy.resolve_secret("KIMI_CLAUDE_API_KEY", environ=self.environ))
        self._write_secret(b"OTHER=value1234\n")
        self.assertIsNone(proxy.resolve_secret("KIMI_CLAUDE_API_KEY", environ=self.environ))


class InitRenderTests(ProxyControlTestCase):
    def test_header_violating_registry_renders_without_the_provider_or_its_models(self) -> None:
        from tests.test_custom_registry import registry_fixture

        registry = registry_fixture()
        custom.save_registry(self.environ, registry)
        before = custom.registry_path(self.environ).read_bytes()
        self._write_secret(b"ACME_API_KEY=fixture-acme-key\nCLEAN_API_KEY=fixture-clean-key\n")
        output = self._init()
        config = (proxy.config_dir(self.home) / "config.yaml").read_text()
        self.assertNotIn("acme", config)
        self.assertNotIn("fixture-acme-key", config)
        self.assertNotIn("acme", output)
        self.assertIn("custom-clean-one", config)
        self.assertIn("https://clean.example.invalid/v1", config)
        self.assertEqual(custom.registry_path(self.environ).read_bytes(), before)

    def test_init_creates_dirs_token_config_idempotent(self) -> None:
        self._write_secret()
        first = self._init()
        token_path = proxy.config_dir(self.home) / "api-key"
        config_path = proxy.config_dir(self.home) / "config.yaml"
        self.assertTrue(token_path.is_file())
        token = token_path.read_text().strip()
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        import stat as stat_mod

        self.assertEqual(stat_mod.S_IMODE(os.lstat(token_path).st_mode), 0o600)
        self.assertEqual(stat_mod.S_IMODE(os.lstat(config_path).st_mode), 0o600)
        first_bytes = config_path.read_bytes()
        second = self._init()
        self.assertEqual(config_path.read_bytes(), first_bytes)
        self.assertEqual(token_path.read_text().strip(), token)
        self.assertIn("providers available", first)
        self.assertIn("kimi", second)

    def test_init_missing_secret_omits_provider_reports(self) -> None:
        # no secret file at all → kimi omitted, OAuth providers remain
        output = self._init()
        self.assertIn("provider unavailable: kimi", output)
        self.assertIn("anthropic", output)
        self.assertIn("openai", output)
        config = (proxy.config_dir(self.home) / "config.yaml").read_text()
        self.assertNotIn("claude-multi-kimi-k3", config)
        self.assertIn("gpt-multi-sol-high", config)

    def test_no_secret_in_init_output_or_config_artifacts_except_resolved(self) -> None:
        self._write_secret(b"KIMI_CLAUDE_API_KEY=supersecret-value-999\n")
        output = self._init()
        self.assertNotIn("supersecret-value-999", output)
        # the resolved secret appears only inside the mode-0600 rendered config
        config_path = proxy.config_dir(self.home) / "config.yaml"
        self.assertIn("supersecret-value-999", config_path.read_text())
        import stat as stat_mod

        self.assertEqual(stat_mod.S_IMODE(os.lstat(config_path).st_mode), 0o600)

    def test_auth_dir_preserved(self) -> None:
        auth = proxy.state_dir(self.home) / "auth"
        state.ensure_private_dir(auth)
        marker = auth / "oauth-record.json"
        state.atomic_write(marker, b"{}")
        self._init()
        self.assertTrue(marker.is_file())


class StatusTests(ProxyControlTestCase):
    def test_status_not_initialized(self) -> None:
        # Injected health getter: tests never reach the running gateway.
        def refused(_base, _path):
            raise OSError("refused")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = proxy.cmd_status([], environ=self.environ, health_get=refused)
        self.assertEqual(code, 0)
        self.assertIn("not initialized", buffer.getvalue())

    def test_status_initialized_and_running(self) -> None:
        self._init()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = proxy.cmd_status(
                [], environ=self.environ, health_get=lambda _b, _h: 200
            )
        self.assertEqual(code, 0)
        self.assertIn("initialized", buffer.getvalue())
        self.assertIn("running", buffer.getvalue())

    def test_status_loopback_only_and_stopped(self) -> None:
        self._init()
        calls = []

        def record_get(base, path):
            calls.append((base, path))
            raise OSError("refused")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            proxy.cmd_status([], environ=self.environ, health_get=record_get)
        self.assertIn("stopped", buffer.getvalue())
        self.assertEqual(calls, [("http://127.0.0.1:8317", "/healthz")])

    def test_status_redacts_secrets(self) -> None:
        self._write_secret(b"KIMI_CLAUDE_API_KEY=supersecret-value-999\n")
        self._init()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            proxy.cmd_status([], environ=self.environ, health_get=lambda _b, _h: 200)
        self.assertNotIn("supersecret-value-999", buffer.getvalue())
        self.assertNotIn("a" * 64, buffer.getvalue())


def _acknowledged_sign_ins(case: unittest.TestCase) -> None:
    """These classes test the login exec itself; the personal-use
    acknowledgement gate in front of it is covered in test_setup_signin."""

    patcher = mock.patch("claude_multi.setup.signin.login_refusal", return_value=None)
    patcher.start()
    case.addCleanup(patcher.stop)


class RunLoginTests(ProxyControlTestCase):
    def setUp(self) -> None:
        super().setUp()
        _acknowledged_sign_ins(self)

    def _capture_exec(self):
        captured = {}

        def fake_execve(executable, argv, env):
            captured["executable"] = executable
            captured["argv"] = argv
            captured["env"] = env
            return "EXECUTED"

        return captured, fake_execve

    def test_run_exact_execve_no_wrapper(self) -> None:
        self._write_secret()
        captured, fake = self._capture_exec()
        outcome = proxy.cmd_run([], environ=self.environ, execve=fake)
        self.assertEqual(outcome, "EXECUTED")
        self.assertEqual(captured["executable"], str(self.binary))
        self.assertEqual(captured["argv"][0], str(self.binary))
        self.assertEqual(captured["argv"][1], "--config")
        self.assertTrue(captured["argv"][2].endswith("config.yaml"))
        self.assertEqual(captured["argv"][3], "--local-model")
        self.assertEqual(len(captured["argv"]), 4)

    def test_login_execve_exact(self) -> None:
        for command, flag in (("claude-login", "--claude-login"), ("codex-device-login", "--codex-device-login")):
            with self.subTest(command=command):
                captured, fake = self._capture_exec()
                outcome = proxy.cmd_login(command, [], environ=self.environ, execve=fake)
                self.assertEqual(outcome, "EXECUTED")
                self.assertEqual(captured["argv"][-1], flag)

    def test_binary_resolution_failure(self) -> None:
        environ = dict(self.environ)
        environ["CLAUDE_MULTI_PROXY_BIN"] = str(self.root / "missing")
        with self.assertRaisesRegex(ProxyError, "not an executable"):
            proxy.resolve_proxy_binary(environ)

    def test_no_secret_in_exec_argv_or_env(self) -> None:
        self._write_secret(b"KIMI_CLAUDE_API_KEY=supersecret-value-999\n")
        captured, fake = self._capture_exec()
        proxy.cmd_run([], environ=self.environ, execve=fake)
        argv_blob = " ".join(captured["argv"])
        self.assertNotIn("supersecret-value-999", argv_blob)


class InstanceLockTests(ProxyControlTestCase):
    """Every run holds the single-instance lock across exec and stamps an instance nonce."""

    NONCE = "0123456789abcdef"

    def setUp(self) -> None:
        super().setUp()
        self.state_root = sessions.state_root(self.environ)
        self.workdir = proxy.gateway_workdir(self.state_root)
        self.lock_path = self.workdir / proxy.service.INSTANCE_LOCK

    def _exec_probe(self, seen: dict):
        from claude_multi.platform import posix_fs

        def execve(executable, argv, env):
            seen["held"] = posix_fs.lock_held(self.lock_path)
            seen["inheritable"] = [fd for fd in range(3, 256) if self._locked_inheritable(fd)]
            seen["stamp"] = proxy.service.read_exec_stamp(self.workdir)
            return "EXECUTED"

        return execve

    def _locked_inheritable(self, fd: int) -> bool:
        try:
            return os.get_inheritable(fd) and os.path.samefile(f"/proc/self/fd/{fd}", self.lock_path)
        except OSError:
            return False

    def test_prepared_run_holds_the_lock_across_exec_and_stamps_the_nonce(self) -> None:
        self._init()
        seen: dict = {}
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            result = proxy.cmd_run(["--prepared", "--instance", self.NONCE], environ=self.environ,
                                   execve=self._exec_probe(seen))
        self.assertEqual(result, "EXECUTED")
        self.assertTrue(seen["held"])
        self.assertEqual(len(seen["inheritable"]), 1)  # the lock fd survives the exec
        self.assertEqual(seen["stamp"].instance, self.NONCE)
        marker = out.getvalue().strip().splitlines()[-1]
        self.assertTrue(marker.startswith(f"claude-multi-proxy: gateway instance {self.NONCE} starting "
                                          f"(pid {os.getpid()}, port "), marker)
        self.assertFalse(proxy.posix_fs.lock_held(self.lock_path))  # a returning exec released it
        # Without --instance a random nonce is stamped.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            proxy.cmd_run(["--prepared"], environ=self.environ, execve=self._exec_probe(seen))
        self.assertRegex(seen["stamp"].instance, r"^[0-9a-f]{16}$")
        self.assertNotEqual(seen["stamp"].instance, self.NONCE)

    def test_a_held_lock_refuses_every_mode_with_its_own_status(self) -> None:
        self._init()
        from claude_multi.platform import posix_fs

        before = proxy.service.read_exec_stamp(self.workdir)
        held = posix_fs.hold_exclusive(self.lock_path)
        try:
            for args in (["run", "--prepared"], ["run", "--prepare-and-exec"], ["run"]):
                err = io.StringIO()
                with self.subTest(args=args), redirect_stdout(io.StringIO()), redirect_stderr(err), \
                        mock.patch.object(proxy, "INSTANCE_LOCK_WAIT", 0.0):
                    code = proxy.main(args, environ=self.environ, execve=lambda *_a: self.fail("exec"))
                    self.assertEqual(code, proxy.INSTANCE_BUSY_EXIT)
                    self.assertIn("another gateway instance is running or starting", err.getvalue())
        finally:
            os.close(held)
        self.assertEqual(proxy.service.read_exec_stamp(self.workdir), before)

    def test_prepare_and_exec_prepares_then_execs_in_one_process(self) -> None:
        self._write_secret()
        seen: dict = {}
        detached = []
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()), \
                mock.patch.object(proxy.service, "listener_owner",
                                  return_value=proxy.service.OwnerVerdict("none", "fixture")) as listener:
            result = proxy.cmd_run(["--prepare-and-exec", "--detach", "--instance", self.NONCE,
                                    "--state-root", str(self.state_root)],
                                   environ=self.environ, execve=self._exec_probe(seen),
                                   detach=lambda: detached.append(True))
        self.assertEqual(result, "EXECUTED")
        self.assertEqual(detached, [True])
        self.assertTrue(seen["held"])
        self.assertEqual(seen["stamp"].instance, self.NONCE)
        self.assertTrue((proxy.config_dir(self.home) / "config.yaml").is_file())
        self.assertTrue((proxy.config_dir(self.home) / proxy.PREPARED_STAMP).is_file())
        text = out.getvalue()
        self.assertLess(text.index("config: "), text.index(f"gateway instance {self.NONCE} starting"))
        self.assertNotIn("gateway: ", text)  # no readiness wait in the starting process
        for call in listener.call_args_list:  # the stopped proof never asks a service manager
            self.assertEqual(call.kwargs.get("stamp"), {"pid": None})
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            proxy.cmd_run(["--prepare-and-exec"], environ=self.environ, execve=self._exec_probe(seen),
                          detach=lambda: detached.append(False))
        self.assertEqual(detached, [True])  # only --detach forks

    def test_a_failed_preparation_releases_the_lock(self) -> None:
        state.ensure_private_dir(self.workdir)
        (self.workdir / proxy.service.GATEWAY_DOTENV).write_text("X=1\n")
        with self.assertRaises(ProxyError):
            proxy.cmd_run(["--prepare-and-exec"], environ=self.environ, execve=lambda *_a: self.fail("exec"))
        (self.workdir / proxy.service.GATEWAY_DOTENV).unlink()
        with mock.patch.object(proxy, "_prepare_start_here", side_effect=ProxyError("render refused")):
            with self.assertRaisesRegex(ProxyError, "render refused"):
                proxy.cmd_run(["--prepare-and-exec"], environ=self.environ, execve=lambda *_a: self.fail("exec"))
        self.assertFalse(proxy.posix_fs.lock_held(self.lock_path))
        with self.assertRaises(OSError), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self._init()
            proxy.cmd_run(["--prepared"], environ=self.environ,
                          execve=mock.Mock(side_effect=OSError(8, "Exec format error")))
        self.assertFalse(proxy.posix_fs.lock_held(self.lock_path))

    def test_run_usage(self) -> None:
        for args in (["--detach"], ["--prepared", "--detach"], ["--instance", self.NONCE],
                     ["--prepared", "--instance", "XYZ"], ["--prepared", "--prepare-and-exec"],
                     ["--prepared", "--instance", self.NONCE, "--instance", self.NONCE],
                     ["--prepare-and-exec", "--detach", "--detach"], ["--prepared", "--state-root", "relative"]):
            with self.subTest(args=args), self.assertRaisesRegex(ProxyError, r"^usage: claude-multi-proxy run \["):
                proxy.cmd_run(args, environ=self.environ, execve=lambda *_a: self.fail("exec"))


class GatewayEnvScrubTests(ProxyControlTestCase):
    """run/login exec the gateway with a scrubbed env.

    The deny-list covers every variable the pinned gateway reads at startup
    that would change what it is (management password, remote config /
    credential stores, cloud deploy mode, writable path, control-panel path,
    GitHub token, Meta mint URL), in every letter case.
    """
    def setUp(self) -> None:
        super().setUp()
        _acknowledged_sign_ins(self)


    DENIED = (
        "MANAGEMENT_PASSWORD", "management_password",
        "HOME_JWT", "home_jwt",
        "PGSTORE_DSN", "pgstore_dsn", "PGSTORE_SCHEMA", "pgstore_local_path",
        "GITSTORE_GIT_URL", "gitstore_git_token", "GITSTORE_LOCAL_PATH",
        "OBJECTSTORE_ENDPOINT", "objectstore_secret_key", "OBJECTSTORE_BUCKET",
        "DEPLOY", "deploy",
        "WRITABLE_PATH", "writable_path",
        "MANAGEMENT_STATIC_PATH", "management_static_path",
        "GITHUB_TOKEN", "github_token",
        "META_MINT_URL", "meta_mint_url",
    )
    KEPT = ("PATH", "LANG", "XDG_RUNTIME_DIR", "DEPLOYMENT_NAME", "PGSTORE", "MY_HOME_JWT")
    _capture_exec = RunLoginTests._capture_exec

    def _environ(self):
        environ = dict(self.environ)
        for index, name in enumerate(self.DENIED):
            environ[name] = f"deny-value-{index}"
        for name in self.KEPT:
            environ[name] = f"keep-{name}"
        return environ

    def _assert_scrubbed(self, captured, stderr):
        env = captured["env"]
        for name in self.DENIED:
            self.assertNotIn(name, env)
        for name in self.KEPT:
            self.assertEqual(env[name], f"keep-{name}")
        for name in ("HOME", "CLAUDE_MULTI_SECRET_ENV", "CLAUDE_MULTI_ASSETS"):
            self.assertEqual(env[name], self.environ[name])
        notice = [line for line in stderr.splitlines() if line.startswith("gateway env:")]
        self.assertEqual(len(notice), 1)
        for name in self.DENIED:
            self.assertIn(name, notice[0])
        # Names only: no value ever reaches output.
        self.assertNotIn("deny-value-", stderr)

    def test_run_execs_with_the_scrubbed_env(self) -> None:
        self._write_secret()
        captured, fake = self._capture_exec()
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            proxy.cmd_run([], environ=self._environ(), execve=fake)
        self._assert_scrubbed(captured, err.getvalue())

    def test_login_execs_with_the_scrubbed_env(self) -> None:
        for command in proxy.LOGIN_FLAGS:
            with self.subTest(command=command):
                captured, fake = self._capture_exec()
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()):
                    proxy.cmd_login(command, [], environ=self._environ(), execve=fake)
                self._assert_scrubbed(captured, err.getvalue())

    def test_clean_env_passes_through_silently(self) -> None:
        self._write_secret()
        captured, fake = self._capture_exec()
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            proxy.cmd_run([], environ=self.environ, execve=fake)
        self.assertEqual(captured["env"], self.environ)
        self.assertNotIn("gateway env:", err.getvalue())

    def test_denied_names_match_case_insensitively(self) -> None:
        env, dropped = proxy.gateway_environment(
            {"Management_Password": "x", "PgStore_Dsn": "y", "KEEP": "z"}
        )
        self.assertEqual(env, {"KEEP": "z"})
        self.assertEqual(dropped, ["Management_Password", "PgStore_Dsn"])


class GatewayWorkdirTests(ProxyControlTestCase):
    """run/login run the gateway in ``<state root>/gateway``.

    The pinned CLIProxyAPI loads ``<cwd>/.env`` after the env scrub, so
    run/login refuse while one exists there (never opened) and chdir into
    the private directory only through the entrypoint's seam.
    """

    def setUp(self) -> None:
        super().setUp()
        _acknowledged_sign_ins(self)
        self._write_secret()
        self.set_up_gateway()
        self.state_root = sessions.state_root(self.environ)
        self.workdir = self.state_root / "gateway"
        self.dotenv = self.workdir / ".env"
        self.config = proxy.config_dir(self.home) / "config.yaml"

    def _recorders(self):
        events: list[tuple[str, str]] = []

        def chdir(path):
            events.append(("chdir", path))

        def execve(executable, argv, env):
            events.append(("execve", executable))
            return "EXECUTED"

        return events, chdir, execve

    def _commands(self):
        yield "run", lambda chdir, execve: proxy.cmd_run(
            [], environ=self.environ, execve=execve, chdir=chdir)
        for command in proxy.LOGIN_FLAGS:
            yield command, lambda chdir, execve, command=command: proxy.cmd_login(
                command, [], environ=self.environ, execve=execve, chdir=chdir)
        for command in ("run", *proxy.LOGIN_FLAGS):
            yield f"main {command}", lambda chdir, execve, command=command: proxy.main(
                [command], environ=self.environ, execve=execve, chdir=chdir)

    def _assert_private_dir(self, path: Path) -> None:
        info = os.lstat(path)
        self.assertTrue(stat.S_ISDIR(info.st_mode), path)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700, path)

    def test_chdir_targets_the_gateway_dir_right_before_execve(self) -> None:
        for label, call in self._commands():
            with self.subTest(label), redirect_stderr(io.StringIO()):
                events, chdir, execve = self._recorders()
                self.assertEqual(call(chdir, execve), "EXECUTED")
                self.assertEqual(
                    events, [("chdir", str(self.workdir)), ("execve", str(self.binary))]
                )
                self._assert_private_dir(self.workdir)
                self._assert_private_dir(self.workdir / "logs")

    def test_an_explicit_state_root_is_honoured(self) -> None:
        other = self.root / "other-state"
        events, chdir, execve = self._recorders()
        with redirect_stderr(io.StringIO()):
            proxy.cmd_run(["--state-root", str(other)], environ=self.environ,
                          execve=execve, chdir=chdir)
        self.assertEqual(events[0], ("chdir", str(other / "gateway")))
        self._assert_private_dir(other / "gateway" / "logs")

    def test_default_seam_never_changes_the_cwd(self) -> None:
        before = os.getcwd()
        calls = []
        with mock.patch.object(os, "chdir", side_effect=AssertionError("chdir called")), \
                redirect_stderr(io.StringIO()):
            proxy.cmd_run([], environ=self.environ, execve=lambda *a: calls.append(a))
            for command in proxy.LOGIN_FLAGS:
                proxy.cmd_login(command, [], environ=self.environ,
                                execve=lambda *a: calls.append(a))
                proxy.main([command], environ=self.environ, execve=lambda *a: calls.append(a))
            proxy.main(["run"], environ=self.environ, execve=lambda *a: calls.append(a))
        self.assertEqual(len(calls), 2 + 2 * len(proxy.LOGIN_FLAGS))
        self.assertEqual(os.getcwd(), before)
        # The directory is still prepared (the unit's WorkingDirectory).
        self._assert_private_dir(self.workdir / "logs")

    def test_init_never_chdirs(self) -> None:
        events, chdir, _execve = self._recorders()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = proxy.main(["init"], environ=self.environ, models_get=self._models_get,
                              chdir=chdir)
        self.assertEqual((code, events), (0, []))

    def test_an_unsafe_gateway_dir_refuses_in_one_line(self) -> None:
        state.ensure_private_dir(self.state_root)
        self.workdir.mkdir(mode=0o700)
        os.chmod(self.workdir, 0o750)
        for label, call in self._commands():
            if not label.startswith("main"):
                continue
            with self.subTest(label):
                events, chdir, execve = self._recorders()
                err = io.StringIO()
                with redirect_stderr(err):
                    code = call(chdir, execve)
                self.assertEqual(code, 1)
                self.assertEqual(events, [])
                lines = err.getvalue().splitlines()
                self.assertEqual(len(lines), 1, lines)
                self.assertIn(f"state directory {self.workdir} must not be accessible", lines[0])
        self.assertFalse(self.config.exists())

    def _plant(self, kind: str) -> None:
        if kind == "regular file":
            self.dotenv.write_bytes(b"HOME_JWT=planted-value\n")
        elif kind == "mode-000 file":
            self.dotenv.write_bytes(b"DEPLOY=cloud\n")
            os.chmod(self.dotenv, 0)
        else:
            os.symlink(self.root / "nowhere" / ".env", self.dotenv)

    def _never_open(self):
        """Patch the open paths so any open of the planted .env fails loudly."""

        target = str(self.dotenv)
        hits: list[str] = []
        real_open, real_io_open, real_os_open = open, io.open, os.open

        def guard(real):
            def opener(file, *args, **kwargs):
                if isinstance(file, (str, bytes, os.PathLike)) and os.fsdecode(file) == target:
                    hits.append(target)
                    raise AssertionError(f"{target} was opened")
                return real(file, *args, **kwargs)
            return opener

        patches = [
            mock.patch("builtins.open", guard(real_open)),
            mock.patch("io.open", guard(real_io_open)),
            mock.patch("os.open", guard(real_os_open)),
        ]
        return hits, patches

    def test_a_dotenv_refuses_before_any_render(self) -> None:
        workdir = proxy.ensure_gateway_workdir(self.state_root)
        self.assertEqual(workdir, self.workdir)
        expected = (
            f"claude-multi-proxy: gateway working directory {self.workdir} holds a .env "
            "file: CLIProxyAPI would load it (HOME_JWT, DEPLOY, WRITABLE_PATH and the "
            "store variables override the gateway's config); move it aside unless you "
            "put it there; the gateway was not started\n"
        )
        continuity_file = proxy.config_dir(self.home) / "continuity.json"
        for kind in ("regular file", "mode-000 file", "dangling symlink"):
            for command in ("run", *proxy.LOGIN_FLAGS):
                with self.subTest(kind=kind, command=command):
                    if os.path.lexists(self.dotenv):
                        os.unlink(self.dotenv)
                    self._plant(kind)
                    events, chdir, execve = self._recorders()
                    hits, patches = self._never_open()
                    err, out = io.StringIO(), io.StringIO()
                    with patches[0], patches[1], patches[2], redirect_stderr(err), \
                            redirect_stdout(out):
                        code = proxy.main([command], environ=self.environ, execve=execve,
                                          chdir=chdir)
                    self.assertEqual(code, 1)
                    self.assertEqual(err.getvalue(), expected)
                    self.assertEqual(out.getvalue(), "")
                    self.assertEqual(events, [])
                    self.assertEqual(hits, [])
                    self.assertFalse(self.config.exists())
                    self.assertFalse(continuity_file.exists())
                    self.assertTrue(os.path.lexists(self.dotenv))  # never moved or removed
                    os.unlink(self.dotenv)
        # Moved aside, the gateway starts again.
        events, chdir, execve = self._recorders()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(proxy.main(["run"], environ=self.environ, execve=execve,
                                        chdir=chdir), "EXECUTED")
        self.assertEqual([kind for kind, _ in events], ["chdir", "execve"])
        self.assertTrue(self.config.exists())

    def test_the_default_seam_refuses_too(self) -> None:
        proxy.ensure_gateway_workdir(self.state_root)
        self._plant("regular file")
        with self.assertRaisesRegex(ProxyError, r"holds a \.env file: .*the gateway was not started$"):
            proxy.cmd_run([], environ=self.environ, execve=lambda *a: self.fail("exec"))
        self.assertFalse(self.config.exists())


class SelectedSecretReadinessTests(ProxyControlTestCase):
    """``selected_secret_problems`` over the lineup adapter.

    The resolved input is ``Runtime.lineup_secret_problems``'s
    adapter shape (``cli._SecretAdapter``) built from a profile lineup, not a
    2.x resolved composition; the selected/unselected semantics are unchanged.
    """

    @staticmethod
    def _adapter(lineup):
        from claude_multi import cli

        return cli._SecretAdapter(
            lead=cli._SecretModel(lineup.lead.binding.key),
            variants=tuple(cli._SecretModel(a.binding.key) for a in lineup.agents.values()),
        )

    def _lineup(self, bundle, lead_key, agents):
        import copy

        from claude_multi import profile

        document = copy.deepcopy(bundle.seed_profiles[catalog.DEFAULT_SEED])
        document.pop("seed", None)
        document["name"] = "secret-readiness"
        lead_entry = bundle.lines[lead_key]
        document["lead"] = {"model": lead_key, "effort": lead_entry["lead"]["effort"]}
        document["agents"] = {
            role: {"model": key, "effort": bundle.lines[key]["default_effort"]}
            for role, key in agents.items()
        }
        evaluation = profile.evaluate(document, profile.LineupCatalog.from_docs(bundle.docs))
        self.assertIsNotNone(evaluation.lineup, evaluation.errors)
        return evaluation.lineup

    def _resolved(self):
        # The direct kimi provider is selected (an agent line on it); the
        # seed's OAuth-pool lead and agents stay unchecked.
        bundle = catalog.load_catalog(CATALOG_ROOT)
        seed = bundle.seed_profiles[catalog.DEFAULT_SEED]
        kimi = next(
            key for key, entry in bundle.lines.items()
            if entry["provider"] == "kimi" and "agents" in entry["capabilities"]
        )
        agents = {role: binding["model"] for role, binding in seed["agents"].items()}
        agents["cm-analyst"] = kimi
        return bundle, self._adapter(self._lineup(bundle, seed["lead"]["model"], agents))

    def test_missing_file_blocks_selected_provider(self) -> None:
        bundle, resolved = self._resolved()
        problems = proxy.selected_secret_problems(
            resolved,
            bundle.docs["models"]["models"],
            bundle.docs["providers"]["providers"],
            environ=self.environ,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("Kimi (kimi)", problems[0])
        self.assertIn("env:KIMI_CLAUDE_API_KEY", problems[0])
        self.assertIn("missing", problems[0])

    def test_missing_variable_blocks(self) -> None:
        self._write_secret(b"OTHER=value1234\n")
        bundle, resolved = self._resolved()
        problems = proxy.selected_secret_problems(
            resolved,
            bundle.docs["models"]["models"],
            bundle.docs["providers"]["providers"],
            environ=self.environ,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("KIMI_CLAUDE_API_KEY", problems[0])
        self.assertIn("missing from the secret env file", problems[0])

    def test_unsafe_secret_file_blocks_redacted(self) -> None:
        self._write_secret(b"not-an-assignment\n")
        bundle, resolved = self._resolved()
        problems = proxy.selected_secret_problems(
            resolved,
            bundle.docs["models"]["models"],
            bundle.docs["providers"]["providers"],
            environ=self.environ,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("unsafe or malformed", problems[0])
        self.assertNotIn("not-an-assignment", problems[0])

    def test_symlink_secret_file_blocks(self) -> None:
        self._write_secret()
        link = self.secrets / "link.env"
        link.symlink_to(self.secret_file)
        environ = dict(self.environ)
        environ["CLAUDE_MULTI_SECRET_ENV"] = str(link)
        bundle, resolved = self._resolved()
        problems = proxy.selected_secret_problems(
            resolved,
            bundle.docs["models"]["models"],
            bundle.docs["providers"]["providers"],
            environ=environ,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("unsafe", problems[0])

    def test_present_secret_passes_oauth_unaffected(self) -> None:
        self._write_secret()
        bundle, resolved = self._resolved()
        problems = proxy.selected_secret_problems(
            resolved,
            bundle.docs["models"]["models"],
            bundle.docs["providers"]["providers"],
            environ=self.environ,
        )
        self.assertEqual(problems, [])

    def test_unselected_direct_provider_not_checked(self) -> None:
        # selector-only lineup: no kimi selection → kimi secret irrelevant
        bundle = catalog.load_catalog(CATALOG_ROOT)
        oauth = [
            key for key, entry in bundle.lines.items()
            if bundle.providers[entry["provider"]]["transport"]["kind"] == "oauth-pool"
            and "lead" in entry["capabilities"] and "agents" in entry["capabilities"]
            and entry["roles"] == "all"
        ]
        self.assertGreaterEqual(len(oauth), 2)
        resolved = self._adapter(self._lineup(
            bundle, oauth[0], {"cm-explorer": oauth[1], "cm-analyst": oauth[1]}
        ))
        problems = proxy.selected_secret_problems(
            resolved,
            bundle.docs["models"]["models"],
            bundle.docs["providers"]["providers"],
            environ=self.environ,
        )
        self.assertEqual(problems, [])

    def test_fake_environ_never_touches_real_secret_path(self) -> None:
        # The fake secret file is malformed while the real one parses; any
        # read of the real path would wrongly succeed. The fake must be used.
        real_path = proxy.secret_env_path(None)
        self.assertNotEqual(str(real_path), str(self.secret_file))
        self._write_secret(b"this is malformed\n")
        bundle, resolved = self._resolved()
        with mock.patch.object(proxy.state, "read_private", wraps=proxy.state.read_private) as spy:
            problems = proxy.selected_secret_problems(
                resolved,
                bundle.docs["models"]["models"],
                bundle.docs["providers"]["providers"],
                environ=self.environ,
            )
        self.assertEqual(len(problems), 1)
        self.assertIn("malformed", problems[0])
        touched = {call.args[0] for call in spy.call_args_list}
        self.assertEqual(touched, {self.secret_file})
        self.assertNotIn(real_path, touched)


class TokenStateErrorTests(unittest.TestCase):
    """A wrong-mode token file is a clean one-line error, no traceback."""

    def test_ensure_token_wraps_state_error(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="claude-multi-proxy-"))
        self.addCleanup(shutil.rmtree, home, True)
        token_path = proxy.config_dir(home) / "api-key"
        token_path.parent.mkdir(parents=True)
        token_path.write_text("x" * 64 + "\n")
        token_path.chmod(0o644)  # state.read_private refuses group/other access
        with self.assertRaisesRegex(proxy.ProxyError, "unusable"):
            proxy.ensure_token(home)

    def test_main_reports_state_error_as_one_line(self) -> None:
        import io as _io

        home = Path(tempfile.mkdtemp(prefix="claude-multi-proxy-"))
        self.addCleanup(shutil.rmtree, home, True)
        token_path = proxy.config_dir(home) / "api-key"
        token_path.parent.mkdir(parents=True)
        token_path.write_text("x" * 64 + "\n")
        token_path.chmod(0o644)
        import sys as _sys

        err = _io.StringIO()
        with mock.patch.object(_sys, "stderr", err):
            code = proxy.main(
                ["init"],
                environ={"HOME": str(home), "CLAUDE_MULTI_ASSETS": str(CATALOG_ROOT)},
                models_get=lambda _base, _token: (200, set()),
            )
        self.assertEqual(code, 1)
        self.assertIn("claude-multi-proxy:", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class SetSecretValueTests(unittest.TestCase):
    """proxy.set_secret_value — parse-preserving 0600 secret writes."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-secret-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "claude.env"

    def _write(self, text: str) -> None:
        state.ensure_private_dir(self.root)
        state.atomic_write(self.path, text.encode("utf-8"))

    def test_append_to_new_file(self) -> None:
        length = proxy.set_secret_value(self.path, "QWEN_CLAUDE_API_KEY", "sk-new")
        self.assertEqual(length, 6)
        self.assertEqual(self.path.read_text(), "QWEN_CLAUDE_API_KEY=sk-new\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_replace_preserves_other_lines_byte_identical(self) -> None:
        self._write("# comment\nKIMI_CLAUDE_API_KEY=old-kimi\nexport EXTRA=1\n")
        proxy.set_secret_value(self.path, "KIMI_CLAUDE_API_KEY", "new-kimi")
        self.assertEqual(
            self.path.read_text(),
            "# comment\nKIMI_CLAUDE_API_KEY=new-kimi\nexport EXTRA=1\n",
        )

    def test_replace_keeps_export_prefix(self) -> None:
        self._write("export QWEN_CLAUDE_API_KEY=old\n")
        proxy.set_secret_value(self.path, "QWEN_CLAUDE_API_KEY", "new")
        self.assertEqual(self.path.read_text(), "export QWEN_CLAUDE_API_KEY=new\n")

    def test_invalid_name_and_value_rejected(self) -> None:
        with self.assertRaises(proxy.ProxyError):
            proxy.set_secret_value(self.path, "lower-case", "x")
        with self.assertRaises(proxy.ProxyError):
            proxy.set_secret_value(self.path, "QWEN_CLAUDE_API_KEY", "has spaces")
        with self.assertRaises(proxy.ProxyError):
            proxy.set_secret_value(self.path, "QWEN_CLAUDE_API_KEY", "")
        self.assertFalse(self.path.exists())

    def test_symlink_target_refused(self) -> None:
        target = self.root / "real.env"
        state.atomic_write(target, b"KIMI_CLAUDE_API_KEY=x\n")
        os.symlink(target, self.path)
        with self.assertRaises(proxy.ProxyError):
            proxy.set_secret_value(self.path, "QWEN_CLAUDE_API_KEY", "sk-x")

    def test_duplicate_keys_collapse_to_one_consumable_file(self) -> None:
        self._write("QWEN_CLAUDE_API_KEY=old\nexport QWEN_CLAUDE_API_KEY=older\n")
        proxy.set_secret_value(self.path, "QWEN_CLAUDE_API_KEY", "new")
        # The first occurrence wins (its prefix), the duplicate is dropped.
        self.assertEqual(self.path.read_text(), "QWEN_CLAUDE_API_KEY=new\n")
        # The saved file passes the strict parser (duplicates are rejected).
        self.assertEqual(
            proxy.parse_secret_env(self.path), {"QWEN_CLAUDE_API_KEY": "new"}
        )

    def test_export_tab_prefix_preserved_exactly(self) -> None:
        self._write("export\tQWEN_CLAUDE_API_KEY=old\n")
        proxy.set_secret_value(self.path, "QWEN_CLAUDE_API_KEY", "new")
        self.assertEqual(self.path.read_text(), "export\tQWEN_CLAUDE_API_KEY=new\n")

    def test_preexisting_malformed_other_line_rejects_the_save(self) -> None:
        self._write("garbage line\nKIMI_CLAUDE_API_KEY=x\n")
        with self.assertRaises(proxy.ProxyError):
            proxy.set_secret_value(self.path, "QWEN_CLAUDE_API_KEY", "sk-x")
        # Nothing written: the file still holds the original bytes.
        self.assertEqual(
            self.path.read_text(), "garbage line\nKIMI_CLAUDE_API_KEY=x\n"
        )

    def test_concurrent_saves_serialize_through_the_lock(self) -> None:
        self._write("KIMI_CLAUDE_API_KEY=x\n")
        lock = state.FileLock(self.path)
        lock.acquire(blocking=True)
        outcome: list[str] = []

        def writer() -> None:
            try:
                proxy.set_secret_value(self.path, "QWEN_CLAUDE_API_KEY", "sk-later")
            except proxy.ProxyError as exc:
                outcome.append(str(exc))

        thread = threading.Thread(target=writer)
        thread.start()
        # While the lock is held the writer must still be waiting.
        self.assertTrue(thread.is_alive())
        lock.release()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, [])
        self.assertEqual(
            proxy.parse_secret_env(self.path),
            {"KIMI_CLAUDE_API_KEY": "x", "QWEN_CLAUDE_API_KEY": "sk-later"},
        )

    def test_unrelated_blank_lines_survive_a_duplicate_collapse(self) -> None:
        self._write(
            "# header\n\nQWEN_CLAUDE_API_KEY=old\n\nexport QWEN_CLAUDE_API_KEY=older\n"
        )
        proxy.set_secret_value(self.path, "QWEN_CLAUDE_API_KEY", "new")
        self.assertEqual(
            self.path.read_text(),
            "# header\n\nQWEN_CLAUDE_API_KEY=new\n\n",
        )


class ListProviderModelsTests(unittest.TestCase):
    """2.14.0: the explicit-invocation provider listing (discover)."""

    def _providers(self):
        from claude_multi import catalog

        return catalog.load_catalog(CATALOG_ROOT).docs["providers"]["providers"]

    def test_secret_never_leaks_into_fetch_errors(self) -> None:
        secret = "topsecret-fixture"

        def bad_fetch(url, headers):
            raise RuntimeError(f"headers={headers}")  # carries the secret

        with mock.patch.dict(
            os.environ, {}, clear=False
        ):
            with mock.patch.object(
                proxy, "resolve_secret", return_value=secret
            ):
                with self.assertRaises(proxy.ProxyError) as ctx:
                    proxy.list_provider_models(
                        "kimi", self._providers(), fetch=bad_fetch
                    )
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIn("RuntimeError", str(ctx.exception))

    def test_malformed_payload_entries_are_skipped(self) -> None:
        payload = b'{"data": [{"id": "k3"}, {"no_id": 1}, "junk", 42]}'
        with mock.patch.object(proxy, "resolve_secret", return_value="x"):
            entries = proxy.list_provider_models(
                "kimi", self._providers(), fetch=lambda url, headers: payload
            )
        self.assertEqual([e["id"] for e in entries], ["k3"])

    def test_missing_secret_is_a_clean_error(self) -> None:
        with mock.patch.object(proxy, "resolve_secret", return_value=None):
            with self.assertRaises(proxy.ProxyError) as ctx:
                proxy.list_provider_models("kimi", self._providers())
        self.assertIn("KIMI_CLAUDE_API_KEY", str(ctx.exception))


class ListingRedirectAndErrorShapeTests(unittest.TestCase):
    """Redirects are never followed
    (the credential header would leak to the target); HTTP/connection/parse
    failures become clean redacted ProxyErrors."""

    def _providers(self, base_url):
        return {
            "fixture": {
                "id": "fixture",
                "transport": {
                    "kind": "direct",
                    "base_url": base_url,
                    "auth": {"kind": "bearer", "secret_ref": "env:FIXTURE_KEY"},
                },
            }
        }

    def test_redirect_is_never_followed_and_secret_never_forwarded(self) -> None:
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                if self.path == "/v1/models":
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"data": []}')

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        with mock.patch.object(proxy, "resolve_secret", return_value="fixture-secret"):
            with self.assertRaises(proxy.ProxyError) as ctx:
                proxy.list_provider_models(
                    "fixture", self._providers(base_url), timeout=5.0
                )
        self.assertIn("HTTP 302", str(ctx.exception))
        # The redirect target was never requested — the credential header
        # never left the original request.
        self.assertEqual(hits, ["/v1/models"])

    def test_http_error_is_a_status_only_message(self) -> None:
        def bad_fetch(_url, _headers):
            raise urllib.error.HTTPError(
                "http://x", 418, "teapot detail must not leak", {}, None
            )

        with mock.patch.object(proxy, "resolve_secret", return_value="x"):
            with self.assertRaises(proxy.ProxyError) as ctx:
                proxy.list_provider_models(
                    "fixture", self._providers("http://127.0.0.1:1"), fetch=bad_fetch
                )
        self.assertIn("HTTP 418", str(ctx.exception))
        self.assertNotIn("teapot detail", str(ctx.exception))

    def test_url_error_is_a_reason_only_message(self) -> None:
        def bad_fetch(_url, _headers):
            raise urllib.error.URLError("connection refused")

        with mock.patch.object(proxy, "resolve_secret", return_value="x"):
            with self.assertRaises(proxy.ProxyError) as ctx:
                proxy.list_provider_models(
                    "fixture", self._providers("http://127.0.0.1:1"), fetch=bad_fetch
                )
        self.assertIn("connection error", str(ctx.exception))

    def test_unexpected_payload_shape_is_a_clean_error(self) -> None:
        with mock.patch.object(proxy, "resolve_secret", return_value="x"):
            with self.assertRaises(proxy.ProxyError) as ctx:
                proxy.list_provider_models(
                    "fixture",
                    self._providers("http://127.0.0.1:1"),
                    fetch=lambda _url, _headers: b'{"data": 42}',
                )
        self.assertIn("unexpected shape", str(ctx.exception))

    def test_non_object_payload_is_a_clean_error(self) -> None:
        with mock.patch.object(proxy, "resolve_secret", return_value="x"):
            with self.assertRaises(proxy.ProxyError) as ctx:
                proxy.list_provider_models(
                    "fixture",
                    self._providers("http://127.0.0.1:1"),
                    fetch=lambda _url, _headers: b"[1, 2]",
                )
        self.assertIn("unexpected shape", str(ctx.exception))


class ListingDescriptorTests(unittest.TestCase):
    """per-provider listing descriptors — deepseek attempt,
    openrouter public OpenAI-shape listing."""

    def _providers(self):
        from claude_multi import catalog

        return catalog.load_catalog(CATALOG_ROOT).docs["providers"]["providers"]

    def test_openrouter_listing_needs_no_secret_and_parses_openai_shape(self) -> None:
        payload = (
            b'{"data": ['
            b'{"id": "x-ai/grok-4.6", "name": "Grok 4.6", "context_length": 500000,'
            b' "top_provider": {"max_completion_tokens": 64000},'
            b' "reasoning": {"supported_efforts": ["high", "xhigh"]}},'
            b' "junk", {"no_id": true},'
            b' {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash", "context_length": 1000000}'
            b']}'
        )
        captured = {}

        def fetch(url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return payload

        # resolve_secret raises if called: the public listing must never
        # touch the secret machinery.
        with mock.patch.object(
            proxy, "resolve_secret", side_effect=AssertionError("secret read")
        ):
            entries = proxy.list_provider_models(
                "openrouter", self._providers(), fetch=fetch
            )
        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/models")
        self.assertNotIn("Authorization", captured["headers"])
        self.assertNotIn("x-api-key", captured["headers"])
        self.assertEqual(
            entries,
            [
                {
                    "id": "x-ai/grok-4.6",
                    "display_name": "Grok 4.6",
                    "context_length": 500000,
                    "max_completion_tokens": 64000,
                    "think_efforts": ["high", "xhigh"],
                    "openrouter": True, "pricing": {}, "tools": None, "modality": None,
                },
                {
                    "id": "deepseek/deepseek-v4-flash",
                    "display_name": "DeepSeek V4 Flash",
                    "context_length": 1000000,
                    "openrouter": True, "pricing": {}, "tools": None, "modality": None,
                },
            ],
        )

    def test_deepseek_lists_via_openai_shape_with_bearer(self) -> None:
        # Verified 2026-08-12: the Anthropic path 404s; the documented
        # listing is OpenAI-shape GET /models with Bearer.
        captured = {}

        def fetch(url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return b'{"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]}'

        with mock.patch.object(proxy, "resolve_secret", return_value="ds"):
            entries = proxy.list_provider_models(
                "deepseek", self._providers(), fetch=fetch
            )
        self.assertEqual(captured["url"], "https://api.deepseek.com/models")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer ds")
        self.assertNotIn("x-api-key", captured["headers"])
        # Entries carry ids only — context is asked for in the add flow.
        self.assertEqual(
            entries,
            [
                {"id": "deepseek-flash", "display_name": "", "context_length": None},
                {"id": "deepseek-v4-pro", "display_name": "", "context_length": None},
            ],
        )

    def test_qwen_unsupported_carries_its_note(self) -> None:
        with self.assertRaises(proxy.ProxyError) as ctx:
            proxy.list_provider_models("qwen", self._providers())
        self.assertIn("404", str(ctx.exception))

    def test_unknown_provider_attempts_generic_shape(self) -> None:
        # A custom provider (no descriptor) attempts the Anthropic shape on
        # its own base — the discover path for operator-added providers.
        providers = {
            "lab": {
                "id": "lab",
                "transport": {
                    "kind": "direct",
                    "base_url": "https://lab.example.com/apps/anthropic",
                    "auth": {"kind": "header", "header": "x-api-key", "secret_ref": "env:LAB_KEY"},
                },
            }
        }
        captured = {}

        def fetch(url, headers):
            captured["url"] = url
            return b'{"data": []}'

        with mock.patch.object(proxy, "resolve_secret", return_value="k"):
            entries = proxy.list_provider_models("lab", providers, fetch=fetch)
        self.assertEqual(captured["url"], "https://lab.example.com/apps/anthropic/v1/models")
        self.assertEqual(entries, [])


class DiscoveryListingContractTests(unittest.TestCase):
    """llm-local listing, secret_ref guard, created."""

    def _providers(self):
        return catalog.load_catalog(CATALOG_ROOT).docs["providers"]["providers"]

    def test_llm_local_listing_moved_to_its_sample_declaration(self) -> None:
        # The listing split: the catalog descriptor
        # table no longer carries llm-local; its sample declaration carries
        # the keyless {base}/models listing (never /v1/v1), which the
        # discovery executes for operator providers. A catalog-shaped keyless
        # provider without a descriptor is the clean refusal below, zero calls.
        from _catalog import SHIPPED_ROOT
        from claude_multi import strict_json

        self.assertNotIn("llm-local", proxy._LISTING_SUPPORT)
        self.assertEqual(
            set(proxy._LISTING_SUPPORT) - set(catalog.load_catalog(SHIPPED_ROOT).providers), set()
        )
        sample = strict_json.load(SHIPPED_ROOT / catalog.EXAMPLES_PROVIDERS_DIR / "llm-local.json")
        base = sample["provider"]["base_url"].rstrip("/")
        self.assertTrue(base.endswith("/v1"))
        self.assertEqual(sample["provider"]["listing"],
                         {"url": base + "/models", "shape": "openai", "auth": "none"})
        providers = self._providers()
        calls: list[str] = []
        with mock.patch.object(proxy, "resolve_secret", side_effect=AssertionError("secret read")):
            with self.assertRaises(ProxyError):
                proxy.list_provider_models("llm-local", providers, fetch=lambda url, headers: calls.append(url))
        self.assertEqual(calls, [])

    def test_keyless_transport_without_a_public_descriptor_is_a_clean_error(self) -> None:
        providers = {
            "lan": {
                "transport": {
                    "kind": "direct-openai",
                    "base_url": "http://lan.invalid/v1",
                    "auth": {"kind": "none"},
                }
            }
        }
        fetch = mock.Mock(side_effect=AssertionError("no request"))
        with self.assertRaisesRegex(ProxyError, "no secret_ref"):
            proxy.list_provider_models("lan", providers, fetch=fetch)
        fetch.assert_not_called()
        del providers["lan"]["transport"]["auth"]
        with self.assertRaisesRegex(ProxyError, "auth missing"):
            proxy.list_provider_models("lan", providers, fetch=fetch)

    def test_created_is_kept_from_either_shape(self) -> None:
        payload = (
            b'{"data": ['
            b'{"id": "a", "created_at": "2026-09-01T00:00:00Z"},'
            b'{"id": "b", "created": 1790035200},'
            b'{"id": "c", "created": true},'
            b'{"id": "d", "created_at": "\u001b[31mnot a date"},'
            b'{"id": "e", "created": -5}'
            b"]}"
        )
        with mock.patch.object(proxy, "resolve_secret", return_value="k"):
            entries = proxy.list_provider_models(
                "kimi", self._providers(), fetch=lambda url, headers: payload
            )
        self.assertEqual(
            {entry["id"]: entry.get("created") for entry in entries},
            {"a": "2026-09-01T00:00:00Z", "b": 1790035200, "c": None, "d": None, "e": None},
        )


class ListingParseBoundaryTests(unittest.TestCase):
    """Parse-boundary hardening pins."""

    def _providers(self):
        from claude_multi import catalog

        return catalog.load_catalog(CATALOG_ROOT).docs["providers"]["providers"]

    def test_missing_data_member_is_an_error_not_empty_success(self) -> None:
        # A 200 error body must not read as "the provider has no models".
        with mock.patch.object(proxy, "resolve_secret", return_value="x"):
            with self.assertRaises(proxy.ProxyError) as ctx:
                proxy.list_provider_models(
                    "kimi",
                    self._providers(),
                    fetch=lambda _u, _h: b'{"error": {"message": "fixture"}}',
                )
        self.assertIn("unexpected shape", str(ctx.exception))

    def test_wrongly_typed_fields_drop_to_defaults(self) -> None:
        payload = (
            b'{"data": [{"id": "m1", "name": {"not": "a string"},'
            b' "context_length": "500000"}]}'
        )
        with mock.patch.object(proxy, "resolve_secret", return_value="x"):
            entries = proxy.list_provider_models(
                "kimi", self._providers(), fetch=lambda _u, _h: payload
            )
        self.assertEqual(
            entries, [{"id": "m1", "display_name": "", "context_length": None}]
        )

    def test_deeply_nested_body_is_a_clean_error(self) -> None:
        depth = 60000
        body = b"[" * depth + b"]" * depth
        with mock.patch.object(proxy, "resolve_secret", return_value="x"):
            with self.assertRaises(proxy.ProxyError) as ctx:
                proxy.list_provider_models(
                    "kimi", self._providers(), fetch=lambda _u, _h: body
                )
        self.assertIn("unexpected shape", str(ctx.exception))

    def test_json_booleans_are_not_integers(self) -> None:
        # bool subclasses int: a `true` context must normalize to None,
        # never render as "True ctx".
        payload = (
            b'{"data": [{"id": "m1", "name": "Valid", "context_length": true,'
            b' "top_provider": {"max_completion_tokens": true}}]}'
        )
        with mock.patch.object(proxy, "resolve_secret", return_value="x"):
            entries = proxy.list_provider_models(
                "openrouter", self._providers(), fetch=lambda _u, _h: payload
            )
        self.assertEqual(
            entries,
            [{"id": "m1", "display_name": "Valid", "context_length": None,
              "openrouter": True, "pricing": {}, "tools": None, "modality": None}],
        )

    def test_descriptors_are_immutable(self) -> None:
        # Review: descriptors route credentials — mutation must be impossible.
        with self.assertRaises(TypeError):
            proxy._LISTING_SUPPORT["kimi"] = {"url": "https://evil.example.com"}
        with self.assertRaises(TypeError):
            proxy._LISTING_SUPPORT["kimi"]["url"] = "https://evil.example.com"


class PreparedAndReloadStampTests(ProxyControlTestCase):
    def test_prepared_never_writes_config_or_reads_secrets(self):
        self._write_secret()
        self._init()
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in proxy.config_dir(self.home).iterdir()}
        original = state.atomic_write
        def write(path, *a, **kw):
            self.assertFalse(Path(path).is_relative_to(proxy.config_dir(self.home)))
            return original(path, *a, **kw)
        with mock.patch.object(proxy, "resolve_secret", side_effect=AssertionError("secret read")), \
             mock.patch.object(proxy, "render_runtime_config", side_effect=AssertionError("render")), \
             mock.patch.object(state, "atomic_write", side_effect=write):
            calls = []
            proxy.cmd_run(["--prepared"], environ=self.environ, execve=lambda *args: calls.append(args))
        self.assertEqual(len(calls), 1)
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in proxy.config_dir(self.home).iterdir()}
        self.assertEqual(before, after)
        stamp = proxy.service.read_exec_stamp(proxy.gateway_workdir(sessions.state_root(self.environ)))
        self.assertEqual(stamp.pid, os.getpid())
        self.assertEqual(stamp.pid_namespace, os.readlink("/proc/self/ns/pid"))
        self.assertEqual(stamp.signature, proxy.exec_signature())

    def test_prepared_absent_or_modified_refuses(self):
        with self.assertRaisesRegex(proxy.ProxyError, "preparation is absent"):
            proxy.cmd_run(["--prepared"], environ=self.environ, execve=lambda *_: self.fail("exec"))
        self._init()
        for name in ("config.yaml", "api-key", "continuity.json"):
            path = proxy.config_dir(self.home) / name
            before = path.read_bytes()
            state.atomic_write(path, before + b"\n")
            with self.assertRaisesRegex(proxy.ProxyError, "preparation is absent"):
                proxy.cmd_run(["--prepared"], environ=self.environ, execve=lambda *_: self.fail("exec"))
            state.atomic_write(path, before)

    def test_reload_flag_exit_codes_and_flag_order(self):
        for status, code in (("reloaded", 0), ("restart_required", 3), ("token_mismatch", 4), ("down", 5)):
            for args in (["--reload-check", "--state-root", str(self.root / "state")],
                         ["--state-root", str(self.root / "state"), "--reload-check"], []):
                with self.subTest(status=status, args=args), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                     mock.patch.object(proxy, "await_sentinel", return_value=proxy.ReloadResult(status, status)):
                    self.assertEqual(proxy.cmd_init(args, environ=self.environ), code if args else 0)
        with self.assertRaisesRegex(proxy.ProxyError, "usage:"):
            proxy.cmd_init(["--reload-check", "--reload-check"], environ=self.environ)
        with self.assertRaisesRegex(proxy.ProxyError, "usage:"):
            proxy.cmd_run(["--prepared", "--prepared"], environ=self.environ)

    def test_exception_records_attempt_and_failure(self):
        root = sessions.state_root(self.environ)
        # An existing install (a home whose gateway was never set up is not
        # rendered for, not even by the unit's reload).
        proxy.ensure_directories(self.home)
        proxy.ensure_token(self.home)
        with mock.patch.object(proxy, "render_runtime_config", side_effect=OSError("fixture")), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(proxy.main(["init", "--reload-check"], environ=self.environ), 1)
        workdir = proxy.gateway_workdir(root)
        self.assertTrue((workdir / proxy.service.RELOAD_ATTEMPT).is_file())
        self.assertEqual(proxy.service.read_reload_stamp(workdir)["status"], "error")
        self.assertTrue(proxy.service.pending_restart_lines(workdir, signature=proxy.exec_signature(),
                                                           launcher_version="v", reload_failed=lambda: False))

    def _seed_old_exec_stamp(self):
        self._init()
        root = sessions.state_root(self.environ)
        workdir = proxy.gateway_workdir(root)
        proxy.service.write_exec_stamp(workdir, proxy.service.ExecStamp(
            "old", proxy.exec_signature(), 42, "/fixture/old-gateway",
            "2026-09-27T00:00:00+00:00", "pid:[101]"))
        return workdir

    def _status_without_health_or_manager(self):
        proc = self.root / "proc"
        (proc / "self/ns").mkdir(parents=True)
        (proc / "self/ns/pid").symlink_to("pid:[101]")
        return proxy.service.gateway_status(
            base_url="unused", health_path="/healthz", state_root=sessions.state_root(self.environ),
            proc_root=proc, health_get=mock.Mock(side_effect=ConnectionRefusedError), manager=lambda: None,
            pid_get=lambda **kw: proxy.service.gateway_pid(
                **kw, runner=mock.Mock(side_effect=FileNotFoundError("no user bus"))))

    def test_unreadable_pid_namespace_still_replaces_old_exec_stamp(self):
        workdir = self._seed_old_exec_stamp()
        readlink = os.readlink
        def denied(path, *args, **kwargs):
            if str(path) == "/proc/self/ns/pid":
                raise PermissionError("DO-NOT-PRINT")
            return readlink(path, *args, **kwargs)
        def execve(*_):
            stamp = proxy.service.read_exec_stamp(workdir)
            self.assertEqual(stamp.pid, os.getpid())
            self.assertIsNone(stamp.pid_namespace)
        err = io.StringIO()
        with mock.patch.object(proxy.os, "readlink", side_effect=denied), redirect_stderr(err):
            proxy.cmd_run(["--prepared"], environ=self.environ, execve=execve)
        self.assertNotIn("DO-NOT-PRINT", err.getvalue())
        self.assertEqual(self._status_without_health_or_manager().state, "unknown")

    def test_exec_stamp_failure_is_nonfatal_and_redacted(self):
        workdir = self._seed_old_exec_stamp()
        err = io.StringIO()
        calls = []
        def execve(*args):
            self.assertFalse((workdir / proxy.service.EXEC_STAMP).exists())
            calls.append(args)
        with mock.patch.object(proxy.service, "write_exec_stamp", side_effect=OSError("DO-NOT-PRINT")), redirect_stderr(err):
            proxy.cmd_run(["--prepared"], environ=self.environ, execve=execve)
        self.assertEqual(len(calls), 1)
        self.assertIn("could not record the gateway start (OSError)", err.getvalue())
        self.assertNotIn("DO-NOT-PRINT", err.getvalue())
        self.assertEqual(self._status_without_health_or_manager().state, "unknown")

    def test_exec_stamp_invalidation_failure_is_nonfatal_and_redacted(self):
        self._seed_old_exec_stamp()
        for error in (OSError("DO-NOT-PRINT"), state.StateError("DO-NOT-PRINT")):
            with self.subTest(error=type(error).__name__):
                err = io.StringIO()
                calls = []
                with mock.patch.object(proxy.service, "write_exec_stamp", side_effect=error), \
                     mock.patch.object(state, "remove_private", side_effect=error) as remove, redirect_stderr(err):
                    proxy.cmd_run(["--prepared"], environ=self.environ, execve=lambda *a: calls.append(a))
                remove.assert_called_once_with(proxy.gateway_workdir(sessions.state_root(self.environ)) / proxy.service.EXEC_STAMP)
                self.assertEqual(len(calls), 1)
                self.assertIn("could not record the gateway start", err.getvalue())
                self.assertNotIn("DO-NOT-PRINT", err.getvalue())


class ManagementTestCase(ProxyControlTestCase):
    def setUp(self):
        super().setUp()
        self.environ[proxy.management.PATCHES_ENV] = proxy.management.ALLOWLIST_PATCH
        self.environ[proxy.management.CHANNEL_ENV] = proxy.management.MANAGEMENT_CHANNEL
        self.directory = proxy.management.key_dir(self.home)

    def prepare(self, *, owner="none", alive=False):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return proxy.cmd_init(
                ["--prepare-start"], environ=self.environ, models_get=self._models_get,
                listener_observer=lambda _: proxy.service.OwnerVerdict(owner, "fixture"),
                pid_get=lambda **_: (None, alive))

    def run_capture(self, *, prepared=True, environ=None):
        captured, fake = RunLoginTests._capture_exec(self)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            proxy.cmd_run(["--prepared"] if prepared else [],
                          environ=self.environ if environ is None else environ, execve=fake)
        return captured, out.getvalue() + err.getvalue()


class ManagementChannelPolicyTests(ManagementTestCase):
    """Management is a channel policy: only the Nix channel enables it.

    A bundle-style wrapper names its own channel and the bundled gateway
    (CLAUDE_MULTI_PROXY_BIN) and attests nothing; even a patch list inherited
    from a parent environment, or key files a Nix install left in the home,
    never make it create, inject or send a management key.
    """

    def setUp(self):
        super().setUp()
        self.set_up_gateway()

    def bundle_environ(self, **extra):
        env = {key: value for key, value in self.environ.items() if key != proxy.management.PATCHES_ENV}
        return {**env, proxy.management.CHANNEL_ENV: "bundle", **extra}

    def command(self, args, environ):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = proxy.main(args, environ=environ, models_get=self._models_get, health_get=lambda *_: 200)
        return code, out.getvalue(), err.getvalue()

    def test_spawned_gateway_never_carries_the_attestation_or_an_unselected_key(self):
        # The Nix channel: the selected key is injected, the patch list is not.
        self.prepare()
        captured, _ = self.run_capture()
        self.assertEqual(captured["env"]["MANAGEMENT_PASSWORD"], proxy.management.read_active(self.home))
        self.assertNotIn(proxy.management.PATCHES_ENV, captured["env"])
        # Bundle-style, source-style and channel-less environments, even with
        # a hostile inherited patch list and a selected key in the home.
        hostile = {proxy.management.PATCHES_ENV: proxy.management.ALLOWLIST_PATCH,
                   "MANAGEMENT_PASSWORD": "INHERITED-SECRET"}
        for channel in ("bundle", "source", None, "", "Nix"):
            with self.subTest(channel=channel):
                env = {**self.environ, **hostile}
                if channel is None:
                    env.pop(proxy.management.CHANNEL_ENV)
                else:
                    env[proxy.management.CHANNEL_ENV] = channel
                self.assertFalse(proxy.management.allowlist_build(env))
                for prepared in (True, False):
                    captured, text = self.run_capture(prepared=prepared, environ=env)
                    self.assertNotIn(proxy.management.PATCHES_ENV, captured["env"])
                    self.assertNotIn("MANAGEMENT_PASSWORD", {name.upper() for name in captured["env"]})
                    self.assertNotIn("INHERITED-SECRET", text)
                    self.assertIn("management-key present but management is unavailable in this build", text)

    def test_bundle_environment_creates_no_key_and_offers_no_remedy_loop(self):
        env = self.bundle_environ(**{proxy.management.PATCHES_ENV: proxy.management.ALLOWLIST_PATCH})
        code, out, _ = self.command(["init"], env)
        self.assertEqual(code, 0)
        self.assertNotIn("management key", out)
        self.assertFalse(list(self.directory.glob("management-key*")))
        code, out, _ = self.command(["status"], env)
        self.assertEqual(code, 0)
        self.assertIn("management: unavailable in this build\n", out)
        code, _, err = self.command(["rotate-management-key"], env)
        self.assertEqual(code, 1)
        self.assertIn("management is unavailable in this build; nothing written", err)
        self.assertFalse(list(self.directory.glob("management-key*")))
        captured, _ = self.run_capture(prepared=False, environ=env)
        self.assertNotIn("MANAGEMENT_PASSWORD", captured["env"])
        self.assertNotIn(proxy.management.PATCHES_ENV, captured["env"])
        # Key files from another install stay untouched and unused.
        self.prepare()
        before = {path.name: path.read_bytes() for path in self.directory.glob("management-key*")}
        code, out, _ = self.command(["init"], env)
        self.assertIn("management key: not used (management is unavailable in this build)", out)
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.glob("management-key*")}, before)

    def test_quota_is_unavailable_without_reading_keys_or_the_gateway(self):
        from claude_multi import quota

        self.prepare()
        for environ in (self.bundle_environ(), {}):
            with self.subTest(environ=sorted(environ)), \
                 mock.patch.object(proxy.management, "key_state", side_effect=AssertionError("key read")), \
                 mock.patch.object(proxy.management.service, "listener_owner", side_effect=AssertionError("owner")):
                pool = proxy.management.pool_status(self.home, {"gateway": {"base_url": "http://127.0.0.1:9"}},
                                                    get=mock.Mock(side_effect=AssertionError("get")),
                                                    environ=environ)
            self.assertEqual(pool.state, "unavailable")
            text = quota.state_text(pool, restart_hint="RESTART")
            self.assertEqual(text, "Quota: unavailable in this build; credential health comes from "
                                   + quota.HEALTH_SOURCE_UNKNOWN)
            code, lines = quota.command_lines(pool, provider_by_pool={}, login_commands={}, restart_hint="RESTART",
                                              now=quota._now())
            self.assertEqual((code, lines), (1, [text, quota.condition_line("unavailable")]))
            self.assertEqual(quota.doctor_report(pool, provider_by_pool={}, login_commands={}, restart_hint="RESTART",
                                                 now=quota._now()), ([], [text], frozenset()))
            self.assertNotIn("RESTART", text)
            self.assertNotIn("claude-multi-proxy", text)


class ManagementInjectionTests(ManagementTestCase):
    def setUp(self) -> None:
        super().setUp()
        _acknowledged_sign_ins(self)

    def test_inject_after_scrub_in_both_modes_and_never_login(self):
        self.prepare()
        key = proxy.management.read_active(self.home)
        for prepared in (True, False):
            env = {**self.environ, "MANAGEMENT_PASSWORD": "INHERITED-UPPER",
                   "management_password": "INHERITED-LOWER", "Management_Password": "INHERITED-MIXED"}
            captured, text = self.run_capture(prepared=prepared, environ=env)
            self.assertEqual(captured["env"]["MANAGEMENT_PASSWORD"], key)
            self.assertEqual(captured["argv"], [str(self.binary), "--config",
                                              str(self.directory / "config.yaml"), "--local-model"])
            self.assertNotIn("management_password", captured["env"])
            self.assertNotIn("Management_Password", captured["env"])
            self.assertIn("MANAGEMENT_PASSWORD from management-key", text)
            self.assertIn("Management_Password", text)
            for secret in (key, "INHERITED-UPPER", "INHERITED-LOWER", "INHERITED-MIXED"):
                self.assertNotIn(secret, text)
            for command in proxy.LOGIN_FLAGS:
                captured, fake = RunLoginTests._capture_exec(self)
                with redirect_stderr(io.StringIO()):
                    proxy.cmd_login(command, [], environ=env, execve=fake)
                self.assertFalse(any(name.upper() == "MANAGEMENT_PASSWORD" for name in captured["env"]))
        self.assertNotIn(key, (self.directory / "config.yaml").read_text())
        self.assertNotIn(key, (self.directory / proxy.PREPARED_STAMP).read_text())

    def test_unprepared_run_stages_but_never_promotes(self):
        captured, _ = self.run_capture(prepared=False)
        self.assertNotIn("MANAGEMENT_PASSWORD", captured["env"])
        self.assertTrue((self.directory / proxy.management.STAGED_FILE).exists())
        self.assertFalse((self.directory / proxy.management.KEY_FILE).exists())
        self.prepare()
        a = proxy.management.read_active(self.home)
        proxy.management.stage_rotation(self.home)
        b = (self.directory / proxy.management.STAGED_FILE).read_bytes()
        for prepared in (False, True):
            captured, _ = self.run_capture(prepared=prepared)
            self.assertEqual(captured["env"]["MANAGEMENT_PASSWORD"], a)
            self.assertEqual((self.directory / proxy.management.STAGED_FILE).read_bytes(), b)
        self.prepare()
        captured, _ = self.run_capture()
        self.assertEqual(captured["env"]["MANAGEMENT_PASSWORD"], b.decode().strip())

    def test_prepared_config_immutability_including_locks_and_chmod(self):
        self.prepare()
        key = proxy.management.read_active(self.home)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                  for p in self.directory.iterdir()}
        write = state.atomic_write
        def write_outside_config(path, *args):
            self.assertFalse(Path(path).is_relative_to(self.directory))
            return write(path, *args)
        with mock.patch.object(state.FileLock, "acquire", side_effect=AssertionError("lock")), \
             mock.patch.object(os, "chmod", side_effect=AssertionError("chmod")), \
             mock.patch.object(state, "remove_private", side_effect=AssertionError("remove")), \
             mock.patch.object(state, "atomic_write", side_effect=write_outside_config), \
             mock.patch.object(proxy, "resolve_secret", side_effect=AssertionError("secret")), \
             mock.patch.object(proxy, "render_runtime_config", side_effect=AssertionError("render")):
            captured, _ = self.run_capture()
        self.assertEqual(captured["env"]["MANAGEMENT_PASSWORD"], key)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                                  for p in self.directory.iterdir()})

    def test_start_preparation_requires_both_absent_listener_and_pid(self):
        for owner, alive in (("ours", False), ("foreign", False), ("unknown", False),
                             ("none", True), ("none", None)):
            with self.subTest(owner=owner, alive=alive):
                self.prepare()
                a = (self.directory / proxy.management.KEY_FILE).read_bytes()
                proxy.management.stage_rotation(self.home)
                b = (self.directory / proxy.management.STAGED_FILE).read_bytes()
                self.assertEqual(self.prepare(owner=owner, alive=alive), 0)
                self.assertEqual((self.directory / proxy.management.KEY_FILE).read_bytes(), a)
                self.assertEqual((self.directory / proxy.management.STAGED_FILE).read_bytes(), b)
                captured, _ = self.run_capture()
                self.assertNotIn("MANAGEMENT_PASSWORD", captured["env"])
                self.assertIsNone(proxy.management.read_active(self.home))

    def test_optional_key_trouble_does_not_invalidate_prepared_render(self):
        self.prepare()
        for kind in ("shape", "mode", "missing", "selection"):
            with self.subTest(kind=kind):
                active = self.directory / proxy.management.KEY_FILE
                if kind == "shape":
                    state.atomic_write(active, b"DO-NOT-PRINT")
                elif kind == "mode":
                    active.chmod(0o644)
                elif kind == "missing":
                    active.unlink()
                else:
                    state.atomic_write(active, ("c" * 64 + "\n").encode())
                    (self.directory / proxy.management.PREPARED_FILE).unlink()
                captured, text = self.run_capture()
                self.assertNotIn("MANAGEMENT_PASSWORD", captured["env"])
                self.assertNotIn("DO-NOT-PRINT", text)
        proxy.management.disable(self.home)
        captured, _ = self.run_capture()
        self.assertNotIn("MANAGEMENT_PASSWORD", captured["env"])

    def test_no_gate_never_creates_or_injects_or_observes(self):
        env = {k: v for k, v in self.environ.items() if k != proxy.management.PATCHES_ENV}
        with mock.patch.object(proxy.service, "listener_owner", side_effect=AssertionError("observer")), \
             mock.patch.object(proxy.service, "gateway_pid", side_effect=AssertionError("pid")):
            captured, text = self.run_capture(prepared=False, environ=env)
        self.assertNotIn("MANAGEMENT_PASSWORD", captured["env"])
        self.assertFalse(list(self.directory.glob("management-key*")))
        self.assertNotIn("gateway management:", text)
        self.prepare()
        captured, text = self.run_capture(environ=env)
        self.assertNotIn("MANAGEMENT_PASSWORD", captured["env"])
        self.assertIn("management-key present but management is unavailable in this build", text)

    def test_signature_tracks_policy_but_not_key_values(self):
        before = proxy.exec_signature()
        self.prepare()
        self.assertEqual(proxy.exec_signature(), before)
        proxy.management.stage_rotation(self.home)
        self.prepare()
        self.assertEqual(proxy.exec_signature(), before)
        with mock.patch.object(proxy.management, "EXEC_POLICY", "different-policy"):
            self.assertNotEqual(proxy.exec_signature(), before)

    def test_generation_rollback_keeps_captured_secret_until_process_replacement(self):
        # A rollback with unchanged gateway inputs. The
        # older launcher has no injection gate (no allowlist patch pin) and an
        # older exec signature; its activation reloads, and only a confirmed
        # process replacement drops the environment-held secret.
        self.prepare()
        key = proxy.management.read_active(self.home)
        running, _ = self.run_capture()
        self.assertEqual(running["env"]["MANAGEMENT_PASSWORD"], key)
        config = self.directory / "config.yaml"
        inputs = (running["executable"], running["argv"], config.read_bytes())
        workdir = proxy.gateway_workdir(sessions.state_root(self.environ))
        older = {k: v for k, v in self.environ.items() if k != proxy.management.PATCHES_ENV}
        with mock.patch.object(proxy.management, "EXEC_POLICY", "pre-047"):
            older_signature = proxy.exec_signature()
        self.assertNotEqual(older_signature, proxy.exec_signature())

        def restart_lines():
            return [line for line in proxy.service.pending_restart_lines(
                workdir, signature=older_signature, launcher_version="older",
                reload_failed=lambda: False) if "start arguments or environment filter" in line]

        # Rollback activation: a re-render and hot reload, never an exec.
        with mock.patch.object(os, "execve", side_effect=AssertionError("reload must not exec")), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(proxy.cmd_init([], environ=older, models_get=self._models_get), 0)
        self.assertEqual(config.read_bytes(), inputs[2])  # unchanged gateway inputs
        # The running process keeps its captured environment: management stays
        # reachable with the retained key, and doctor under the older launcher
        # says a restart is pending.
        self.assertEqual(running["env"]["MANAGEMENT_PASSWORD"], key)
        self.assertEqual(proxy.management.read_active(self.home), key)
        self.assertEqual(len(restart_lines()), 1)
        # Confirmed replacement under the older launcher: same binary, argv and
        # render, and no management secret in the new process.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(proxy.cmd_init(
                ["--prepare-start"], environ=older, models_get=self._models_get,
                listener_observer=lambda _: proxy.service.OwnerVerdict("none", "fixture"),
                pid_get=lambda **_: (None, False)), 0)
        with mock.patch.object(proxy.management, "EXEC_POLICY", "pre-047"):
            replaced, text = self.run_capture(environ=older)
        self.assertEqual((replaced["executable"], replaced["argv"], config.read_bytes()), inputs)
        self.assertFalse(any(name.upper() == "MANAGEMENT_PASSWORD" for name in replaced["env"]))
        self.assertNotIn(key, text)
        self.assertEqual(restart_lines(), [])

    def test_failed_exec_after_promotion_never_reverts_or_invents_a_key(self):
        self.prepare()
        proxy.management.stage_rotation(self.home)
        b = (self.directory / proxy.management.STAGED_FILE).read_text().strip()
        self.prepare()  # proven old PID/listener absent
        calls = []
        def fail_exec(_binary, _argv, env):
            calls.append(env["MANAGEMENT_PASSWORD"])
            raise OSError("fixture exec failed")
        with redirect_stderr(io.StringIO()), self.assertRaises(OSError):
            proxy.cmd_run(["--prepared"], environ=self.environ, execve=fail_exec)
        # No old listener exists at this boundary. A subsequent start keeps B.
        for _ in range(8):
            self.assertEqual(proxy.management.read_active(self.home), b)
        self.prepare()
        captured, _ = self.run_capture()
        self.assertEqual(calls, [b])
        self.assertEqual(captured["env"]["MANAGEMENT_PASSWORD"], b)

    def test_management_and_api_key_locks_never_overlap(self):
        held = set()
        acquire, release = state.FileLock.acquire, state.FileLock.release
        def take(lock, *args, **kwargs):
            result = acquire(lock, *args, **kwargs)
            if result:
                held.add(lock.lock_path.name)
                self.assertFalse({"api-key.lock", "management-key.lock"} <= held)
            return result
        def drop(lock):
            held.discard(lock.lock_path.name)
            return release(lock)
        with mock.patch.object(state.FileLock, "acquire", take), mock.patch.object(state.FileLock, "release", drop):
            self.prepare()
            self.run_capture(prepared=False)
        self.assertEqual(held, set())


class ManagementKeyCommandTests(ManagementTestCase):
    # Keep the command tests on the same fully injected fixture home.
    def command(self, args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = proxy.main(args, environ=self.environ, models_get=self._models_get,
                              health_get=lambda *_: 200, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_init_never_promotes_and_status_has_all_states(self):
        self.set_up_gateway()
        self.assertIn("off (no key)", self.command(["status"])[1])
        self.assertIn("quota reads stay off", self.command(["init"])[1])
        self.assertIn("key pending", self.command(["status"])[1])
        self.prepare()
        self.assertIn("management key: present\n", self.command(["init"])[1])
        self.assertIn("management: key active\n", self.command(["status"])[1])
        # Pending without a staged slot (the prepared selection is gone): init
        # never claims a key is staged in management-key.next.
        prepared = self.directory / proxy.management.PREPARED_FILE
        selection = prepared.read_bytes()
        prepared.unlink()
        out = self.command(["init"])[1]
        self.assertFalse((self.directory / proxy.management.STAGED_FILE).exists())
        self.assertIn("management key: present; quota reads stay off until a prepared gateway start", out)
        self.assertNotIn("management-key.next", out)
        state.atomic_write(prepared, selection)
        a = proxy.management.read_active(self.home)
        self.assertEqual(self.command(["rotate-management-key"])[0], 0)
        self.assertIn("rotation staged", self.command(["status"])[1])
        self.assertIn("a rotated key is staged", self.command(["init"])[1])
        self.assertEqual(proxy.management.read_active(self.home), a)
        self.assertEqual(self.command(["disable-management-key"])[0], 0)
        self.assertIn("management: disabled", self.command(["status"])[1])
        self.assertIn("management key: disabled", self.command(["init"])[1])
        self.assertEqual(self.command(["rotate-management-key"])[0], 0)
        for _ in range(6):
            self.assertIn("key pending", self.command(["status"])[1])
            self.assertIsNone(proxy.management.read_active(self.home))

    def test_existing_reload_and_start_results_and_stamps_survive_key_errors(self):
        self.prepare()
        proxy.management.stage_rotation(self.home)
        active = self.directory / proxy.management.KEY_FILE
        a = active.read_bytes()
        for corrupt in (False, True):
            if corrupt:
                state.atomic_write(active, b"PRIVATE-BAD-SHAPE")
            for status, code in proxy.RELOAD_CHECK_EXIT.items():
                with mock.patch.object(proxy, "await_sentinel", return_value=proxy.ReloadResult(status, status)):
                    self.assertEqual(self.command(["init", "--reload-check"])[0], code)
                stamp = proxy.service.read_reload_stamp(proxy.gateway_workdir(sessions.state_root(self.environ)))
                self.assertEqual(stamp["status"], status)
            for status, code in (("ready", 0), ("not_ready", 6)):
                with mock.patch.object(proxy, "await_sentinel", return_value=proxy.ReloadResult(status, status)):
                    self.assertEqual(self.command(["init", "--start-check"])[0], code)
            self.assertEqual(active.read_bytes(), b"PRIVATE-BAD-SHAPE" if corrupt else a)
            self.assertTrue((self.directory / proxy.management.STAGED_FILE).exists())

    def test_command_validation_gate_and_no_leaks(self):
        for command in ("rotate-management-key", "disable-management-key"):
            self.assertEqual(self.command([command, "unexpected"])[0], 1)
        for flag in ("--reload-check", "--start-check", "--prepare-start"):
            self.assertEqual(self.command(["init", "--prepare-start", flag])[0], 1)
        env = self.environ.pop(proxy.management.PATCHES_ENV)
        self.assertEqual(self.command(["rotate-management-key"])[0], 1)
        self.assertFalse(self.directory.exists())
        self.environ[proxy.management.PATCHES_ENV] = env
        self.set_up_gateway()
        self.prepare()
        key = proxy.management.read_active(self.home)
        for args in (["init"], ["status"], ["rotate-management-key"], ["disable-management-key"]):
            code, out, err = self.command(args)
            self.assertEqual(code, 0)
            self.assertNotIn(key, out + err)

    def test_printed_repairs_restore_bad_shape_mode_symlink_and_directory(self):
        for kind in ("shape", "mode", "symlink", "directory"):
            with self.subTest(kind=kind):
                self.prepare()
                active = self.directory / proxy.management.KEY_FILE
                target = self.root / "untouched-target"
                target.write_text("LEAVE-TARGET-ALONE")
                if kind == "shape":
                    state.atomic_write(active, b"DO-NOT-PRINT")
                elif kind == "mode":
                    active.chmod(0o644)
                else:
                    active.unlink()
                    if kind == "symlink":
                        active.symlink_to(target)
                    else:
                        active.mkdir()
                code, out, err = self.command(["init"])
                self.assertEqual(code, 0)
                self.assertIn("quota stays off; fix:", err)
                self.assertNotIn("DO-NOT-PRINT", out + err)
                remedy = err.split("; fix: ", 1)[1].splitlines()[0]
                # Execute the exact displayed procedure. A fixture-only shell
                # function maps the printed proxy command to the entry point
                # copied into an installed-style tree (a source checkout's entry
                # names the source channel, which has no management).
                import sys
                from _layout import REPO_ROOT
                installed = self.home.parent / "installed" / "bin" / "claude-multi-proxy"
                installed.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(REPO_ROOT / "bin/claude-multi-proxy", installed)
                script = 'claude-multi-proxy() { "$PYTHON" "$PROXY" "$@"; }; ' + remedy
                result = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True,
                                        env={**self.environ, "PATH": os.environ.get("PATH", ""),
                                             "PYTHON": sys.executable, "PROXY": str(installed),
                                             "PYTHONPATH": str(REPO_ROOT / "src")})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.prepare()
                self.assertIsNotNone(proxy.management.read_active(self.home))
                self.assertEqual(target.read_text(), "LEAVE-TARGET-ALONE")


class OperatorListingRefusalTests(unittest.TestCase):
    """A providers.d provider block is never listed by the catalog listing."""

    def _providers(self) -> dict:
        return {
            "acme": {
                "display": "Acme", "independence_family": "acme", "support": "operator-declared",
                "support_note": "fixture", "adapter": "cliproxy-claude-compatible-v1",
                "transport": {"kind": "direct", "base_url": "https://api.acme.example/anthropic",
                              "auth": {"kind": "bearer", "secret_ref": "env:ACME_API_KEY"}},
                "passthrough_routes": [], "payload_contracts": [], "origin": "operator",
            },
        }

    def test_operator_provider_listing_refused_with_zero_calls(self) -> None:
        calls: list[str] = []
        for provider_id in ("acme", "llm-local"):
            providers = self._providers()
            if provider_id != "acme":
                providers[provider_id] = providers.pop("acme")
            with self.subTest(provider=provider_id), \
                    mock.patch.object(proxy, "resolve_secret", side_effect=AssertionError("secret read")):
                with self.assertRaises(ProxyError) as caught:
                    proxy.list_provider_models(provider_id, providers, fetch=lambda url, headers: calls.append(url))
                self.assertEqual(
                    str(caught.exception),
                    f"discover {provider_id}: an operator provider is listed through its approved "
                    "listing block (claude-multi discover)",
                )
        self.assertEqual(calls, [])

    def test_catalog_provider_listing_is_unchanged(self) -> None:
        providers = catalog.load_catalog(FIXTURE_ROOT).providers
        seen: list[str] = []

        def fetch(url: str, headers: dict) -> bytes:
            seen.append(url)
            return b'{"data": [{"id": "fixture-model"}]}'

        with mock.patch.object(proxy, "resolve_secret", return_value="dummy-kimi"):
            proxy.list_provider_models("kimi", providers, fetch=fetch)
        self.assertEqual(len(seen), 1)


class CodexListingIdentityTests(unittest.TestCase):
    def test_single_version_drives_url_and_user_agent(self) -> None:
        self.assertEqual(proxy.CODEX_CLIENT_VERSION, "0.159.1")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(proxy.CODEX_MODELS_URL).query)
        self.assertEqual(query, {"client_version": [proxy.CODEX_CLIENT_VERSION]})
        self.assertTrue(proxy.CODEX_USER_AGENT.startswith(f"{proxy.CODEX_ORIGINATOR}/{proxy.CODEX_CLIENT_VERSION} "))
        self.assertEqual(proxy.CODEX_ORIGINATOR, "codex_cli_rs")
        requests = []

        def fetch(url, headers):
            requests.append((url, headers))
            return b'{"models":[{"slug":"gpt-fixture-listed","visibility":"list"}]}'

        with tempfile.TemporaryDirectory() as home, mock.patch.object(
            proxy, "_codex_credential", return_value=("fixture.json", "dummy-token", "dummy-account")
        ) as credential:
            info = catalog.load_catalog(FIXTURE_ROOT).docs["gateway"]["gateway"]
            result = proxy.list_codex_plan_models(info, environ={"HOME": home}, fetch=fetch)
        credential.assert_called_once()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0], proxy.CODEX_MODELS_URL)
        self.assertEqual(requests[0][1]["User-Agent"], proxy.CODEX_USER_AGENT)
        self.assertEqual(requests[0][1]["Originator"], proxy.CODEX_ORIGINATOR)
        self.assertEqual(result[0]["id"], "gpt-fixture-listed")
