"""The gateway endpoint document, port selection, backend choice and its consumers.

Every HOME is temporary; no test connects anywhere (a port probe binds, and the
listener here is test-allocated on an ephemeral port).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _tripwire
from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT
from _v4 import V4Case
from claude_multi import (catalog, cli, endpoint, launch, operator, proxy, scope, service, sessions, state,
                          strict_json)


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-endpoint-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        patcher = mock.patch.object(Path, "home", return_value=self.root / "not-the-runtime-home")
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, document) -> Path:
        path = endpoint.endpoint_path(self.home)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, json.dumps(document).encode())
        return path


class DocumentTests(_Home):
    def test_closed_document_rules(self) -> None:
        good = {"version": 1, "host": "127.0.0.1", "port": 18317}
        self.assertEqual(endpoint.parse_config(good), endpoint.EndpointConfig(port=18317))
        refused = {
            "unknown key": {**good, "extra": 1},
            "missing port": {"version": 1, "host": "127.0.0.1"},
            "version": {**good, "version": 2},
            "boolean version": {**good, "version": True},
            "non-loopback host": {**good, "host": "0.0.0.0"},
            "boolean port": {**good, "port": True},
            "privileged port": {**good, "port": 80},
            "port too large": {**good, "port": 70000},
            "backend": {**good, "backend": "launchd"},
            "unit without the service backend": {**good, "unit": "claude-multi-gateway"},
            "unit shape": {**good, "backend": "systemd", "unit": "../evil"},
            "not an object": [good],
        }
        for label, document in refused.items():
            with self.subTest(label), self.assertRaises(endpoint.EndpointError):
                endpoint.parse_config(document)
        service_doc = endpoint.parse_config({**good, "backend": "systemd", "unit": "my-gateway"})
        self.assertEqual((service_doc.backend, service_doc.service_unit), ("systemd", "my-gateway"))
        self.assertEqual(endpoint.parse_config({**good, "backend": "systemd"}).service_unit,
                         endpoint.DEFAULT_UNIT)
        self.assertIsNone(endpoint.parse_config(good).service_unit)

    def test_read_write_round_trip_is_private(self) -> None:
        self.assertIsNone(endpoint.read_config(self.home))
        config = endpoint.EndpointConfig(port=18320)
        path = endpoint.write_config(self.home, config)
        self.assertEqual(path, self.home / ".config/claude-multi/endpoint.json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(endpoint.read_config(self.home), config)
        self.assertEqual(json.loads(path.read_text()), {"version": 1, "host": "127.0.0.1", "port": 18320})

    def test_unreadable_documents_refuse_with_a_remedy(self) -> None:
        path = self.write({"version": 1, "host": "127.0.0.1", "port": 1})
        with self.assertRaises(endpoint.EndpointError) as ctx:
            endpoint.read_config(self.home)
        self.assertIn("endpoint.json", ctx.exception.remedy)
        path.write_bytes(b"{not json")
        with self.assertRaises(endpoint.EndpointError):
            endpoint.read_config(self.home)
        path.chmod(0o644)
        with self.assertRaises(endpoint.EndpointError):
            endpoint.read_config(self.home)
        path.unlink()
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps({"version": 1, "host": "127.0.0.1", "port": 18317}))
        path.symlink_to(target)
        with self.assertRaises(endpoint.EndpointError):
            endpoint.read_config(self.home)

    def test_accessor_keeps_catalog_fields_and_prefers_the_document(self) -> None:
        gateway = {"gateway": {"base_url": "http://127.0.0.1:8317", "health_path": "/healthz", "other": 1}}
        self.assertEqual(endpoint.gateway_endpoint(gateway).base_url, "http://127.0.0.1:8317")
        self.assertEqual(endpoint.gateway_endpoint(gateway, home=self.home).base_url, "http://127.0.0.1:8317")
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=18318))
        effective = endpoint.gateway_endpoint(gateway, home=self.home)
        self.assertEqual(effective.base_url, "http://127.0.0.1:18318")
        self.assertEqual(effective.health_path, "/healthz")
        self.assertEqual(effective.document["gateway"]["other"], 1)
        self.assertEqual(gateway["gateway"]["base_url"], "http://127.0.0.1:8317")  # never mutated

    def test_catalog_application_keeps_the_trusted_bundle(self) -> None:
        loaded = catalog.load_catalog(FIXTURE_ROOT)
        self.assertIs(endpoint.apply_to_catalog(loaded, self.home), loaded)
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=18319))
        applied = endpoint.apply_to_catalog(loaded, self.home)
        self.assertEqual(applied.bundle_sha256, loaded.bundle_sha256)
        self.assertEqual(applied.docs["gateway"]["gateway"]["base_url"], "http://127.0.0.1:18319")
        self.assertIs(applied.docs["models"], loaded.docs["models"])
        self.assertEqual(loaded.docs["gateway"]["gateway"]["base_url"],
                         catalog.load_catalog(FIXTURE_ROOT).docs["gateway"]["gateway"]["base_url"])


class PortSelectionTests(_Home):
    def test_first_free_port_of_the_new_install_range(self) -> None:
        self.assertEqual(endpoint.NEW_INSTALL_PORTS[0], 18317)
        self.assertEqual(len(endpoint.NEW_INSTALL_PORTS), 20)
        taken = {18317, 18318}
        self.assertEqual(endpoint.select_port(lambda port: port not in taken), 18319)
        with self.assertRaises(endpoint.EndpointError) as ctx:
            endpoint.select_port(lambda _port: False)
        self.assertIn("18317-18336", str(ctx.exception))

    def test_probe_binds_and_never_connects(self) -> None:
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("connect")):
            self.assertFalse(endpoint.port_free(port))
            listener.close()
            self.assertTrue(endpoint.port_free(port))

    def test_a_new_install_records_its_port_and_an_existing_one_keeps_its_own(self) -> None:
        seen = []

        def probe(port):
            seen.append(port)
            return port == 18321

        config = endpoint.ensure_config(self.home, probe=probe)
        self.assertEqual(config, endpoint.EndpointConfig(port=18321))
        self.assertEqual(endpoint.read_config(self.home), config)
        self.assertEqual(seen, list(range(18317, 18322)))
        # The recorded document wins from then on (no new probe).
        self.assertEqual(endpoint.ensure_config(self.home, probe=lambda _p: self.fail("probe")), config)
        other = self.root / "other-home"
        other.mkdir(mode=0o700)
        state.ensure_private_dir(other / ".config" / "claude-multi")
        state.atomic_write(other / ".config/claude-multi/api-key", b"x" * 64 + b"\n")
        self.assertTrue(endpoint.existing_install(other))
        self.assertIsNone(endpoint.ensure_config(other, probe=lambda _p: self.fail("probe")))
        self.assertFalse(endpoint.endpoint_path(other).exists())


class BackendTests(unittest.TestCase):
    def test_on_demand_on_every_channel_and_platform_without_a_service(self) -> None:
        # A fresh Linux Nix install without a unit, Nix on macOS, a bundle, a checkout.
        for channel in ("nix", "bundle", "source", None):
            for platform in ("linux", "darwin"):
                env = {} if channel is None else {endpoint.CHANNEL_ENV: channel}
                with self.subTest(channel=channel, platform=platform):
                    self.assertEqual(endpoint.channel(env), channel)
                    self.assertEqual(endpoint.resolve_backend(None, platform=platform), endpoint.ON_DEMAND)
                    plain = endpoint.EndpointConfig(port=18317)
                    self.assertEqual(endpoint.resolve_backend(plain, platform=platform), endpoint.ON_DEMAND)

    def test_only_an_installed_service_selects_systemd_and_unsupported_is_refused(self) -> None:
        installed = endpoint.EndpointConfig(port=18317, backend="systemd")
        self.assertEqual(endpoint.resolve_backend(installed, platform="linux"), endpoint.SYSTEMD)
        with self.assertRaises(endpoint.EndpointError) as ctx:
            endpoint.resolve_backend(installed, platform="darwin")
        self.assertIn("on-demand", ctx.exception.remedy)

    def test_the_channel_is_reported_never_invented(self) -> None:
        self.assertIsNone(endpoint.channel({endpoint.CHANNEL_ENV: "homebrew"}))
        self.assertIsNone(endpoint.channel({}))


class ConsumerTests(_Home):
    """The configured port reaches every consumer: scopes, readiness, operator rules, render."""

    def setUp(self) -> None:
        super().setUp()
        self.env = {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"), "TERM": "dumb",
            "CLAUDE_MULTI_SECRET_ENV": str(self.root / "secrets" / "claude.env"),
            "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT),
        }
        (self.root / "project").mkdir()
        token_dir = state.ensure_private_dir(self.home / ".config" / "claude-multi")
        state.atomic_write(token_dir / "api-key", (FIXTURE_GATEWAY_TOKEN + "\n").encode("ascii"))

    def runtime(self, **kwargs) -> cli.Runtime:
        return cli.Runtime(
            listener_owner=lambda _base: service.OwnerVerdict("ours", "fixture gateway"),
            managed_root=self.root / "managed", asset_root=FIXTURE_ROOT, environ=self.env,
            cwd=self.root / "project", **kwargs,
        )

    def test_runtime_scopes_readiness_and_operator_ports_follow_the_document(self) -> None:
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=18325))
        probes = []
        runtime = self.runtime(health_get=lambda base, path: probes.append(base) or 200)
        url = "http://127.0.0.1:18325"
        self.assertEqual(runtime.catalog.docs["gateway"]["gateway"]["base_url"], url)
        self.assertEqual(scope.catalog_meta_v2(runtime.ordinary_docs).gateway_base_url, url)
        self.assertEqual(runtime.check_readiness(), FIXTURE_GATEWAY_TOKEN)
        self.assertEqual(probes, [url])
        self.assertIn(18325, operator.gateway_ports(runtime.ordinary_docs))
        with self.assertRaisesRegex(ValueError, "gateway port"):
            operator.normalize_endpoint("http://127.0.0.1:18325/v1", keyed=False, lan=True,
                                        gateway_ports=operator.gateway_ports(runtime.ordinary_docs))
        runtime.reload_catalog()
        self.assertEqual(runtime.catalog.docs["gateway"]["gateway"]["base_url"], url)

    def test_without_a_document_the_packaged_port_stays(self) -> None:
        runtime = self.runtime(health_get=lambda _b, _p: 200)
        packaged = catalog.load_catalog(FIXTURE_ROOT).docs["gateway"]["gateway"]["base_url"]
        self.assertEqual(runtime.catalog.docs["gateway"]["gateway"]["base_url"], packaged)
        self.assertIsNone(runtime.endpoint_error)

    def test_an_invalid_document_refuses_launch_paths_but_not_construction(self) -> None:
        self.write({"version": 1, "host": "127.0.0.1", "port": "18317"})
        runtime = self.runtime(health_get=lambda _b, _p: self.fail("health"))
        self.assertIsNotNone(runtime.endpoint_error)
        with self.assertRaises(launch.LaunchError) as ctx:
            runtime.check_readiness()
        self.assertIn("endpoint.json", ctx.exception.remedy)
        with self.assertRaises(launch.LaunchError):
            runtime.ensure_gateway()
        with self.assertRaises(endpoint.EndpointError):
            runtime.gateway()

    def test_the_render_listens_on_the_configured_port_and_preparation_binds_it(self) -> None:
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=18330))
        env = dict(self.env)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            target, _result, _report = proxy.render_runtime_config(self.home, environ=env)
            self.assertIn("\nport: 18330\n", "\n" + target.read_text())
            proxy.cmd_init(["--prepare-start"], environ=env, models_get=lambda *_a: (200, set()),
                           listener_observer=lambda _b: service.OwnerVerdict("none", "fixture"),
                           pid_get=lambda **_k: (None, False))
        self.assertTrue(proxy._prepared_config(self.home, Path(self.env["XDG_STATE_HOME"]) / "claude-multi", env))
        # Moving the port after the preparation makes it stale for run --prepared.
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=18331))
        with self.assertRaisesRegex(proxy.ProxyError, "preparation is absent or stale"):
            proxy._prepared_config(self.home, Path(self.env["XDG_STATE_HOME"]) / "claude-multi", env)
        receipt = strict_json.loads(state.read_private(proxy.config_dir(self.home) / proxy.PREPARED_STAMP))
        self.assertIn(endpoint.ENDPOINT_FILE, receipt["files"])


class OutboundProxyTests(ConsumerTests):
    """The gateway's outbound proxy: endpoint.json, the render, the setup step, doctor."""

    URL = "http://proxy.corp.invalid:3128"

    def test_the_document_validates_the_proxy_and_never_echoes_a_refused_url(self) -> None:
        good = {"version": 1, "host": "127.0.0.1", "port": 18317}
        self.assertEqual(endpoint.parse_config({**good, "proxy_url": self.URL}).proxy_url, self.URL)
        for url in ("http://user:secret-SENTINEL@proxy:3128", "ftp://proxy", "http://proxy/path", "", 7):
            with self.subTest(url=url), self.assertRaises(endpoint.EndpointError) as ctx:
                endpoint.parse_config({**good, "proxy_url": url})
            self.assertNotIn("SENTINEL", str(ctx.exception))
        config = endpoint.EndpointConfig(port=18317, proxy_url=self.URL)
        self.assertEqual(config.document()["proxy_url"], self.URL)
        self.assertNotIn("proxy_url", endpoint.EndpointConfig(port=18317).document())

    def test_the_render_carries_the_proxy_and_the_spawn_env_carries_no_proxy_variable(self) -> None:
        from claude_multi import gateway_lifecycle

        endpoint.write_config(self.home, endpoint.EndpointConfig(port=18330, proxy_url=self.URL))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            target, _result, _report = proxy.render_runtime_config(self.home, environ=dict(self.env))
        self.assertIn(f'\nproxy-url: "{self.URL}"\n', "\n" + target.read_text())
        runtime = self.runtime(health_get=lambda _b, _p: 200)
        static = runtime.catalog.docs["gateway"]["gateway"]["cliproxy_static"]
        self.assertEqual(static["proxy-url"], self.URL)
        env = gateway_lifecycle.spawn_environment(
            {"HOME": str(self.home), "HTTPS_PROXY": self.URL, "http_proxy": self.URL, "ALL_PROXY": self.URL},
            self.home)
        self.assertFalse({name for name in env if name.lower().endswith("_proxy")})

    def setup_step(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        runtime = self.runtime(health_get=lambda _b, _p: 200)
        from claude_multi import gateway_lifecycle

        stopped = gateway_lifecycle.Observation(gateway_lifecycle.STOPPED, "fixture: not running", None,
                                                service.OwnerVerdict("none", "fixture"), False)
        # The setup step starts the gateway under the start lock it took for
        # the endpoint change (ensure_locked).
        with mock.patch("claude_multi.gateway_lifecycle.Gateway.ensure_locked",
                        return_value=mock.Mock(ok=True, exit_code=0, lines=lambda: ["gateway ready (fixture)"])), \
                mock.patch("claude_multi.gateway_lifecycle.Gateway.observe", return_value=stopped), \
                redirect_stderr(err):
            code = cli.main(["setup", "--step", "gateway", *argv], runtime=runtime, output_stream=out,
                            interactive=False)
        return code, out.getvalue(), err.getvalue()

    def test_the_setup_step_records_and_clears_the_proxy(self) -> None:
        code, out, _err = self.setup_step("--proxy", self.URL)
        self.assertEqual(code, 0, out)
        self.assertIn(f"gateway outbound proxy: {self.URL}", out)
        self.assertIn("gateway ready (fixture)", out)
        config = endpoint.read_config(self.home)
        packaged = endpoint.port_of(catalog.load_catalog(FIXTURE_ROOT).docs["gateway"]["gateway"]["base_url"])
        self.assertEqual((config.port, config.proxy_url), (packaged, self.URL))  # an existing install keeps its port
        code, out, _err = self.setup_step("--no-proxy")
        self.assertEqual(code, 0)
        self.assertIn("gateway outbound proxy: none", out)
        self.assertIsNone(endpoint.read_config(self.home).proxy_url)
        code, out, err = self.setup_step("--proxy", "http://u:pw-SENTINEL@proxy:1")
        self.assertNotEqual(code, 0)
        self.assertNotIn("SENTINEL", out + err)
        self.assertIn("credentials are never allowed", out + err)

    def test_a_new_install_takes_a_new_port_with_its_proxy(self) -> None:
        (self.home / ".config/claude-multi/api-key").unlink()
        config = endpoint.set_proxy(self.home, self.URL, probe=lambda port: port == 18320)
        self.assertEqual((config.port, config.proxy_url), (18320, self.URL))

    def test_doctor_names_the_proxy_and_an_ignored_shell_variable(self) -> None:
        from claude_multi.cli import gateway_facts

        runtime = self.runtime(health_get=lambda _b, _p: 200, served_models_callback=lambda *_a: (set(), 200))
        _attention, info = gateway_facts.gateway_runtime_report(runtime)
        self.assertFalse([line for line in info if "proxy" in line.lower()])
        self.env["HTTPS_PROXY"] = self.URL
        _attention, info = gateway_facts.gateway_runtime_report(self.runtime(health_get=lambda _b, _p: 200))
        self.assertTrue(any("never inherits it" in line for line in info), info)
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=18330, proxy_url=self.URL))
        _attention, info = gateway_facts.gateway_runtime_report(self.runtime(health_get=lambda _b, _p: 200))
        self.assertIn(f"Gateway outbound proxy: {self.URL} (endpoint.json)", info)


class PortChangeTests(V4Case):
    def test_sessions_take_a_new_port_at_their_next_resume(self) -> None:
        packaged = catalog.load_catalog(FIXTURE_ROOT).docs["gateway"]["gateway"]["base_url"]
        record = self.launch_fresh()
        mid = sessions.managed_id(record)

        def compiled() -> tuple[str, str]:
            settings = json.loads((self.live(mid) / "settings.json").read_text())
            return settings["env"]["ANTHROPIC_BASE_URL"], self.execs[-1][2]["ANTHROPIC_BASE_URL"]

        self.assertEqual(compiled(), (packaged, packaged))
        endpoint.write_config(Path(self.env["HOME"]), endpoint.EndpointConfig(port=18329))
        self.runtime = self.make_runtime()
        self.resume(mid)
        self.assertEqual(compiled(), ("http://127.0.0.1:18329",) * 2)


class TripwireTests(_Home):
    def test_the_configured_port_joins_the_live_ports(self) -> None:
        self.assertEqual(_tripwire.configured_ports({"HOME": str(self.home)}), frozenset())
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=18333))
        self.assertEqual(_tripwire.configured_ports({"HOME": str(self.home)}), frozenset({18333}))
        self.assertLessEqual({8316, 8317}, _tripwire.PORTS)
        self.assertEqual(_tripwire.PORTS, frozenset({8316, 8317}) | _tripwire.configured_ports())


if __name__ == "__main__":
    unittest.main()
