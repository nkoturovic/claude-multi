"""Real gateway rename-reload and hitless rotation, network-hermetic.

Only the registry is queried, through a private ingress unix socket. The
binary has a loopback-only namespace with NO egress bridges, no credentials,
a fixture HOME, and every configured upstream on the discard port. Old or
unpatched installed gateways are an explicit BOUNDARY, never false evidence.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from claude_multi import probe, proxy, render, state
from _catalog import FIXTURE_ROOT
from test_proxy import _find_binary
from _gateway_harness import BINARY_ENV, boundary


class GatewayHotReloadTests(unittest.TestCase):
    def setUp(self):
        binary = _find_binary()
        if binary is None:
            boundary("BOUNDARY: gateway binary unavailable")
        reason = probe.network_isolation_available()
        if reason is not None:
            boundary("BOUNDARY: " + reason)
        self.root = Path(tempfile.mkdtemp(prefix="cm-hot-reload-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.assets = self.root / "assets"
        shutil.copytree(FIXTURE_ROOT, self.assets)
        self.home = self.root / "home"
        state.ensure_private_dir(self.home)
        self.environ = {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "CLAUDE_MULTI_ASSETS": str(self.assets),
            "CLAUDE_MULTI_SECRET_ENV": str(self.root / "absent-secrets"),
        }
        self.gateway_path = self.assets / "catalog/gateway.json"
        self.gateway = json.loads(self.gateway_path.read_text())
        # A namespace-local port: never bind or connect to the host's gateway.
        self.port = 19361
        self.gateway["gateway"]["base_url"] = f"http://127.0.0.1:{self.port}"
        self.gateway_path.write_text(json.dumps(self.gateway))
        providers_path = self.assets / "catalog/providers.json"
        providers = json.loads(providers_path.read_text())
        for provider in providers["providers"].values():
            transport = provider["transport"]
            # Credentialed routes are omitted by the None resolver. Only
            # keyless upstreams enter this render; send all of them to discard.
            if transport["kind"] == "direct-openai":
                transport["base_url"] = "http://127.0.0.1:9/v1"
        providers_path.write_text(json.dumps(providers))
        proxy.ensure_directories(self.home)
        self.config, initial, _report = self.render()
        self.old = proxy.gateway_api_keys(self.home)[0]
        self.socket = self.root / "gateway.sock"
        self.log_path = self.root / "gateway.log"
        log = self.log_path.open("wb")
        self.addCleanup(log.close)
        self.process = probe.start_isolated_process(
            [os.path.realpath(binary), "--config", str(self.config), "--local-model"],
            cwd=self.home, env={**self.environ, "PATH": "/usr/bin:/bin"},
            bridges=[probe.PortBridge(self.port, self.socket, "ingress")],
            stdout=log, stderr=subprocess.STDOUT,
        )
        self.addCleanup(probe.stop_isolated_process, self.process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.assertIsNone(self.process.poll(), "isolated gateway exited during startup")
            if "file watcher started for config and auth directory changes" in self.log_path.read_text():
                try:
                    status, ids = self.getter("unused", self.old)
                    if status == 200 and initial.sentinel in ids:
                        break
                except OSError:
                    pass
            time.sleep(0.05)
        else:
            self.fail("isolated gateway did not start its registry and watcher")
        version = re.search(r"Version:\s*v?(\d+)\.(\d+)\.(\d+)", self.log_path.read_text())
        if version and tuple(map(int, version.groups())) < (7, 3, 15):
            boundary("BOUNDARY: gateway version predates 7.3.15")
        # Capability detection is separate from assertions: an unpatched
        # store binary is supported on developer hosts but cannot prove the reload.
        self.change_render(1)
        _, detected, _report = self.render()
        outcome = self.wait(detected.sentinel)
        if outcome.status != "reloaded":
            # An explicitly selected omission is a tested product failure, not
            # an unavailable host capability. Keep ambient installed builds a
            # boundary, but let the fail-closed revert tool count own-red.
            if os.environ.get(BINARY_ENV):
                self.fail("selected gateway lacks rename-reload watcher")
            boundary("BOUNDARY: installed gateway lacks rename-reload watcher")

    def render(self):
        # (target, result, continuity report); no state root:
        # the isolated HOME has no session records to extend from.
        return proxy.render_runtime_config(
            self.home, environ=self.environ, resolver=lambda _: None, state_root=None,
        )

    def change_render(self, number):
        self.gateway["gateway"]["cliproxy_static"]["request-retry"] = number
        self.gateway_path.write_text(json.dumps(self.gateway))

    def getter(self, _base, token):
        connection = probe.UnixHTTPConnection(self.socket, timeout=0.5)
        try:
            headers = {"Authorization": "Bearer " + token} if token else {}
            connection.request("GET", "/v1/models", headers=headers)
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                return response.status, set()
            return response.status, {item["id"] for item in json.loads(body)["data"]}
        finally:
            connection.close()

    def wait(self, sentinel):
        return proxy.await_sentinel(self.gateway, self.old, sentinel, models_get=self.getter)

    def test_repeated_atomic_replaces_with_old_inode_open(self):
        sentinels = set()
        with self.config.open("rb"):
            for number in range(2, 6):
                self.change_render(number)
                _, result, _report = self.render()
                before = time.monotonic()
                outcome = self.wait(result.sentinel)
                self.assertEqual(outcome.status, "reloaded")
                self.assertLessEqual(time.monotonic() - before, 2.1)
                self.assertNotIn(result.sentinel, sentinels)
                sentinels.add(result.sentinel)
                status, ids = self.getter("unused", self.old)
                self.assertEqual(status, 200)
                self.assertEqual(sentinels.intersection(ids), {result.sentinel})
        self.assertIsNone(self.process.poll())

    def test_empty_and_keyless_renders_never_disable_auth(self):
        valid = self.config.read_bytes()
        document, _, _, _ = render.build_config_document(
            self.gateway, {}, {}, home=self.home, gateway_tokens=[self.old],
            resolve_secret=lambda _: None, continuity={},
        )
        del document["api-keys"]
        bad_bodies = (b"", b" \n\t\n", b"# comment only\n", render.emit_yaml(document).encode())
        for bad in bad_bodies:
            start_logs = self.log_path.read_text()
            state.atomic_write(self.config, bad)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                # Authenticate repeatedly throughout the reload interval, not
                # just before/after, so an unauthenticated transient fails.
                self.assertEqual(self.getter("unused", "")[0], 401)
                self.assertEqual(self.getter("unused", self.old)[0], 200)
                new_logs = self.log_path.read_text()[len(start_logs):]
                if "ignoring empty config file" in new_logs or "removes every api-key" in new_logs:
                    break
                time.sleep(0.02)
            else:
                self.fail("watcher did not visibly refuse the invalid render")
        state.atomic_write(self.config, valid)
        self.assertEqual(self.getter("unused", "")[0], 401)

    def test_rotation_library_end_to_end(self):
        offset = 0.0
        observed = []
        def clock():
            return time.monotonic() + offset
        def sleep(seconds):
            nonlocal offset
            if seconds >= proxy.HELPER_TTL_SECONDS:
                keys = proxy.gateway_api_keys(self.home)
                observed.append(len(keys))
                self.assertEqual(len(keys), 2)
                for token in keys:
                    self.assertEqual(self.getter("unused", token)[0], 200)
                # A daemon restart's render during the grace period preserves
                # both keys and the exact sentinel-bearing bytes.
                before = self.config.read_bytes()
                self.render()
                self.assertEqual(before, self.config.read_bytes())
                lock = state.FileLock(proxy.config_dir(self.home) / "api-key")
                self.assertTrue(lock.acquire(blocking=False))
                lock.release()
                offset += seconds
            else:
                time.sleep(seconds)
        result = proxy.rotate_token(
            self.home, environ=self.environ, resolver=lambda _: None,
            models_get=self.getter, clock=clock, sleep=sleep,
            live_sessions=lambda: (), state_root=None,
        )
        self.assertEqual(result.status, "reloaded")
        self.assertEqual(observed, [2])
        self.assertEqual(self.getter("unused", self.old)[0], 401)
        new = proxy.gateway_api_keys(self.home)[0]
        self.assertEqual(self.getter("unused", new)[0], 200)
        self.assertFalse((proxy.config_dir(self.home) / "previous-key").exists())
        self.assertGreater(offset, proxy.HELPER_TTL_SECONDS)
        self.assertAlmostEqual(offset, 305, places=3)


if __name__ == "__main__":
    unittest.main()
