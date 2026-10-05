"""The Runtime loopback-gateway seam.

Production defaults must stay the live loopback calls; the token must come
from the RUNTIME HOME (never the process HOME when the runtime environ
carries one); every cli call site must route through the seam so tests can
be hermetic. No test here opens a socket.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import cli, state

from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT, served_selectors
import claude_multi.launch
import claude_multi.service
import claude_multi.tui


class GatewaySeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-seam-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "TERM": "dumb",
            "CLAUDE_MULTI_SECRET_ENV": str(self.root / "secrets" / "claude.env"),
        }
        (self.root / "project").mkdir()
        token_dir = state.ensure_private_dir(self.home / ".config" / "claude-multi")
        state.atomic_write(
            token_dir / "api-key", (FIXTURE_GATEWAY_TOKEN + "\n").encode("ascii")
        )
        # A wrong implementation that consults the process HOME must fail
        # here, never read the operator's real token file.
        patcher = mock.patch.object(
            Path, "home", return_value=self.root / "not-the-runtime-home"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _runtime(self, **kwargs) -> cli.Runtime:
        return cli.Runtime(
            listener_owner=lambda _base: claude_multi.service.OwnerVerdict("ours", "fixture gateway"),
            managed_root=self.root / "managed",
            asset_root=FIXTURE_ROOT,
            environ=self.env,
            cwd=self.root / "project",
            **kwargs,
        )

    def test_defaults_are_the_live_loopback_calls(self) -> None:
        runtime = self._runtime()
        self.assertIsNone(runtime.served_models_callback)
        self.assertIsNone(runtime.health_get)
        with mock.patch.object(
            claude_multi.launch, "served_snapshot",
            return_value=((claude_multi.launch.ServedModel("x"),), 200),
        ) as served:
            self.assertEqual(runtime.served_models("t" * 64), ({"x"}, 200))
        served.assert_called_once_with(runtime.catalog.docs["gateway"], "t" * 64,
                                       owner_check=runtime.listener_owner,
                                       attention=runtime.gateway_attention.append)

    def test_gateway_token_reads_the_runtime_home(self) -> None:
        runtime = self._runtime()
        self.assertEqual(runtime.home, self.home)
        self.assertEqual(runtime.gateway_token(), FIXTURE_GATEWAY_TOKEN)

    def test_injected_served_callback_is_used(self) -> None:
        calls = []

        def served(gateway, token):
            calls.append((gateway["gateway"]["base_url"], token))
            return set(served_selectors()), 200

        runtime = self._runtime(served_models_callback=served)
        ids, status = runtime.served_models(runtime.gateway_token())
        self.assertEqual(status, 200)
        self.assertEqual(ids, set(served_selectors()))
        self.assertEqual(calls, [("http://127.0.0.1:8317", FIXTURE_GATEWAY_TOKEN)])

    def test_readiness_uses_runtime_home_and_injected_health_getter(self) -> None:
        probes = []

        def health(base_url, path):
            probes.append((base_url, path))
            return 200

        runtime = self._runtime(health_get=health)
        self.assertEqual(runtime.check_readiness(), FIXTURE_GATEWAY_TOKEN)
        self.assertEqual(probes, [("http://127.0.0.1:8317", "/healthz")])

    def test_direct_screen_and_provider_facts_route_through_the_seam(self) -> None:
        # re-pointed from the 2.x ordinary picker
        # at the 3.0 Direct screen; the _provider_facts half is unchanged.
        seen = []

        def served(_gateway, token):
            seen.append(token)
            return set(served_selectors()), 200

        runtime = self._runtime(served_models_callback=served)
        screen = cli._DirectScreen(runtime, palette=claude_multi.tui.MONO_PALETTE)
        self.assertEqual(screen.served, set(served_selectors()))
        facts = cli._provider_facts(runtime)
        self.assertFalse(facts.gateway_down)
        self.assertEqual(seen, [FIXTURE_GATEWAY_TOKEN, FIXTURE_GATEWAY_TOKEN])

    def test_perform_threads_runtime_home_and_health_getter(self) -> None:
        def health(_base_url, _path):
            return 200

        runtime = self._runtime(health_get=health)
        prepared = runtime.prepare(
            cli.LaunchTarget(
                "profile", runtime.profiles.load("balanced"), "balanced", True, "Profile"
            ),
            action="fresh",
            passthrough=[],
        )
        with mock.patch.object(claude_multi.launch, "perform_launch", return_value=0) as perform:
            self.assertEqual(runtime.perform(prepared), 0)
        kwargs = perform.call_args.kwargs
        self.assertEqual(kwargs["home"], self.home)
        self.assertIs(kwargs["health_get"], health)


if __name__ == "__main__":
    unittest.main()
