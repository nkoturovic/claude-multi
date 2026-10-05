"""The qualification battery through the real pinned gateway, offline.

The built cli-proxy-api (``CLAUDE_MULTI_TEST_CLI_PROXY_API``, a /nix/store
path) runs inside ``bwrap --unshare-net`` with a rendered fixture config;
its only upstream is a unix-socket fake that counts every attempt. The
production transport (``qualify.loopback_post``) reaches the gateway through
the ingress bridge. Proves the claim a design could not assume:
one planned request is one upstream attempt (no hidden gateway retry), for
a success, a rejection, a 429 and a 5xx. Skips with an explicit boundary
when the gateway binary or network isolation is unavailable.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from _catalog import FIXTURE_ROOT
from _gateway_harness import boundary
from claude_multi import catalog, probe, qualify, render, service, state

GATEWAY_TOKEN = "q" * 64
DUMMY_KIMI = "dummy-kimi-key"
TEST_BINARY_ENV = "CLAUDE_MULTI_TEST_CLI_PROXY_API"
OWNER = staticmethod(lambda _base: service.OwnerVerdict("ours", "fixture gateway"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _binary() -> str | None:
    override = os.environ.get(TEST_BINARY_ENV)
    if override:
        real = os.path.realpath(override)
        if not real.startswith("/nix/store/") or not Path(real).is_file():
            boundary(f"boundary: {TEST_BINARY_ENV}={override} is not a /nix/store cli-proxy-api file")
        return real
    from _gateway import _wrapper_gateway

    for candidate in (_wrapper_gateway(), shutil.which("cli-proxy-api")):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


class _Upstream(BaseHTTPRequestHandler):
    """A fake Anthropic-compatible upstream: counts attempts, answers by mode."""

    attempts: list[str] = []
    mode = "ok"

    def log_message(self, *_args) -> None:
        return

    def _send(self, status: int, payload: dict | None = None, *, sse: list | None = None) -> None:
        if sse is not None:
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for frame in sse:
                self.wfile.write(f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n".encode())
                self.wfile.flush()
            return
        data = json.dumps(payload or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        document = json.loads(self.rfile.read(length) or b"{}")
        path = urllib.parse.urlsplit(self.path).path
        if not path.endswith("/v1/messages"):
            self._send(404, {"type": "error"})
            return
        type(self).attempts.append(path)
        mode = type(self).mode
        if mode in ("429", "500", "400"):
            self._send(int(mode), {"type": "error", "error": {"type": "fake", "message": "fake upstream"}})
            return
        message = {"id": "msg_up", "type": "message", "role": "assistant", "model": document.get("model"),
                   "stop_reason": "end_turn", "stop_sequence": None,
                   "usage": {"input_tokens": 7, "output_tokens": 1}, "content": [{"type": "text", "text": "ok"}]}
        if document.get("stream"):
            self._send(200, sse=[
                {"type": "message_start", "message": {**message, "content": []}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                {"type": "message_stop"},
            ])
            return
        last = document["messages"][-1]["content"]
        text = last if isinstance(last, str) else "".join(
            block.get("text", "") for block in last if isinstance(block, dict) and block.get("type") == "text")
        results = [] if isinstance(last, str) else [
            block for block in last if isinstance(block, dict) and block.get("type") == "tool_result"]
        if document.get("tools") and not results and 'nonce "' in text:
            nonce = text.split('nonce "', 1)[1].split('"', 1)[0]
            message["content"] = [{"type": "tool_use", "id": "toolu_up_1", "name": qualify.TOOL_NAME,
                                   "input": {"nonce": nonce}}]
            message["stop_reason"] = "tool_use"
        elif results:
            content = results[0].get("content")
            reply = content if isinstance(content, str) else "".join(
                part.get("text", "") for part in content if isinstance(part, dict))
            message["content"] = [{"type": "text", "text": reply}]
        self._send(200, message)


class AttemptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binary = _binary()
        if self.binary is None:
            boundary("boundary: pinned cli-proxy-api binary not available; gateway attempt count skipped")
        reason = probe.network_isolation_available()
        if reason is not None:
            boundary("BOUNDARY: " + reason)
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-qualify-gw-"))
        os.chmod(self.root, 0o700)
        self.process: subprocess.Popen | None = None
        self.upstream: probe.UnixHTTPServer | None = None
        self.addCleanup(self._cleanup)
        self._start()

    def _cleanup(self) -> None:
        if self.process is not None:
            probe.stop_isolated_process(self.process)
        if self.upstream is not None:
            self.upstream.shutdown()
            self.upstream.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _start(self) -> None:
        _Upstream.attempts = []
        upstream_port, proxy_port = _free_port(), _free_port()
        self.assertNotIn(proxy_port, (8316, 8317))
        upstream_socket = self.root / "upstream.sock"
        self.gateway_socket = self.root / "gateway.sock"
        self.upstream = probe.UnixHTTPServer(str(upstream_socket), _Upstream)
        threading.Thread(target=self.upstream.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        gateway = copy.deepcopy(bundle.docs["gateway"])
        gateway["gateway"]["base_url"] = f"http://127.0.0.1:{proxy_port}"
        providers = copy.deepcopy(bundle.docs["providers"]["providers"])
        providers["kimi"]["transport"]["base_url"] = f"http://127.0.0.1:{upstream_port}/coding"
        self.line = next(entry for entry in bundle.lines.values() if entry["provider"] == "kimi")
        self.provider = providers["kimi"]
        result = render.render_config(gateway, providers, bundle.lines, continuity={}, home=self.root,
                                      gateway_token=GATEWAY_TOKEN, resolve_secret=lambda _name: DUMMY_KIMI)
        config = self.root / "config.yaml"
        state.atomic_write(config, result.yaml.encode("utf-8"))
        self.process = probe.start_isolated_process(
            [os.path.realpath(self.binary), "--config", str(config), "--local-model"],
            bridges=[probe.PortBridge(upstream_port, upstream_socket),
                     probe.PortBridge(proxy_port, self.gateway_socket, "ingress")],
            cwd=self.root, env={"HOME": str(self.root), "PATH": "/usr/bin:/bin"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.base_url = f"http://127.0.0.1:{proxy_port}"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                connection = probe.UnixHTTPConnection(self.gateway_socket, timeout=1)
                connection.request("GET", "/healthz", headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"})
                if connection.getresponse().status == 200:
                    connection.close()
                    return
                connection.close()
            except OSError:
                pass
            time.sleep(0.1)
        self.fail("disposable gateway did not become ready")

    def _post(self, call: qualify.PlannedCall, body: bytes) -> qualify.HttpResult:
        return qualify.loopback_post(
            self.base_url, GATEWAY_TOKEN, body, deadline=call.deadline, owner_check=OWNER,
            connect=lambda timeout: probe.UnixHTTPConnection(self.gateway_socket, timeout=timeout),
        )

    def _plan(self, checks):
        return qualify.build_plan("fixture-kimi", self.line, digest="a" * 64, provider_id="kimi",
                                  provider=self.provider, checks=checks)

    def test_one_planned_request_has_one_upstream_attempt(self) -> None:
        plan = self._plan(qualify.AGENT_CHECKS)
        results = qualify.run_battery(plan, self._post)
        self.assertEqual([(r.check, r.result) for r in results],
                         [("smoke", "pass"), ("efforts", "pass"), ("tools", "pass"), ("stream", "pass")],
                         [(r.check, r.result, r.http, r.reason) for r in results])
        self.assertEqual(len(_Upstream.attempts), len(plan.calls))
        for mode, expected in (("400", ("failed", "http-status")), ("429", ("inconclusive", "rate-limited")),
                               ("500", ("inconclusive", "upstream-error"))):
            with self.subTest(mode=mode):
                _Upstream.mode = mode
                _Upstream.attempts = []
                try:
                    (result,) = qualify.run_battery(self._plan(("smoke",)), self._post)
                finally:
                    _Upstream.mode = "ok"
                self.assertEqual((result.result, result.reason), expected, result)
                self.assertEqual(len(_Upstream.attempts), 1, "a planned request reached the upstream more than once")


if __name__ == "__main__":
    unittest.main()
