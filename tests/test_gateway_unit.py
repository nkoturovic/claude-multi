"""The supervised unit's spec and the pinned CLIProxyAPI 7.3.15 filesystem inventory.

Write-site audit of /nix/store/qmzgbddl997jjf2q8fnf81y30hm5lp22-source:
* auth/: sdk/auth/filestore.go:104-150; internal/auth/{claude,codex,kimi,
  meta,xai,vertex} token writers. Includes truncate, temp+rename and delete.
* gateway/logs/: internal/logging/global_logger.go:143-217 (even with file
  logging off, .perm_test is created); request_logger_{writer,streaming,
  body_source}.go hold error logs, multipart spools and temp bodies there.
  ResolveLogDirectory falls back to auth/logs, also within the RW bind.
* traces/: reserved existing gateway trace root (no current Go writer).
* static/management.html beside config: managementasset/updater.go:113-121,
  228,410-433 and api/server_management.go: control panel AND updater disabled.
* config.yaml: config/config_load.go:114-125 hashes a plaintext management
  key; empty secret-key avoids this. management/{config_basic,handler,plugins,
  plugin_store}.go writes are unreachable (management allowlist, no management key).
* plugins/: pluginstore/install.go:521-563, pluginhost/auth_provider.go:517-537,
  auth_callbacks.go:299, loader_windows.go:133-200 and homeplugins/sync.go:467:
  plugins.enabled=false; Home/remote stores are excluded by the env scrub.
* Home cert/key: home/certificate.go:146,246, cmd/server/main.go:338-345:
  HOME_JWT scrubbed and no --home-jwt; no Home config rendered.
* postgres/git/object caches and example config: internal/store/*.go and
  misc/copy-example-config.go:22-26: PGSTORE_/GITSTORE_/OBJECTSTORE_ scrubbed.
* cooldown *.cds: sdk/cliproxy/auth/cooldown_state.go:166-257:
  save-cooldown-status=false. discovery instance_id: discovery/id.go:75-84:
  discovery.enabled=false. Remote catalogs are memory-only, additionally
  disabled by --local-model (cmd/server/main.go:833-850).
* WRITABLE_PATH/MANAGEMENT_STATIC_PATH redirected roots are scrubbed; cwd
  .env is refused before exec, so Go cannot restore those environment values.

No new writable Go path is granted for a disabled feature. The whole config
root is read-only, including filenames absent at startup. Tier 'none' is the
explicit unprotected fallback, never described as a sandbox.
"""
import json
import unittest
from pathlib import PurePosixPath

from claude_multi import catalog, endpoint, proxy, service
from claude_multi.platform import systemd_unit
from _catalog import SHIPPED_ROOT, FIXTURE_ROOT
from _layout import SERVICE_SPEC


class GatewayUnitTests(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads(SERVICE_SPEC.read_text())

    def test_paths_and_parity(self):
        s = self.spec
        self.assertEqual(set(s), systemd_unit.SPEC_KEYS)
        self.assertEqual(systemd_unit.validate_spec(s), s)
        # The one service spec describes the supervised backend; on-demand needs no unit.
        self.assertEqual(s["backend"], endpoint.SYSTEMD)
        self.assertEqual(s["unit"], endpoint.DEFAULT_UNIT)
        self.assertEqual(s["state_root"], ".local/state/claude-multi")
        self.assertEqual(s["working_directory"], s["state_root"] + "/" + service.GATEWAY_WORKDIR)
        self.assertEqual(s["exec_links"], {"bundle": ".local/share/claude-multi/install/current",
                                           "nix": ".local/share/claude-multi/nix/current"})
        for key in ("private_dirs", "bind_rw", "bind_ro"):
            for value in s[key]:
                path = PurePosixPath(value)
                self.assertFalse(path.is_absolute())
                self.assertNotIn("..", path.parts)
            self.assertEqual(len(s[key]), len(set(s[key])))
        for i, path in enumerate(s["private_dirs"]):
            self.assertFalse(any(PurePosixPath(path).is_relative_to(parent)
                                 for parent in s["private_dirs"][i + 1:]))
        for path in s["bind_rw"] + s["bind_ro"]:
            self.assertIn(path, s["private_dirs"])
        self.assertEqual(set(s["bind_rw"]), {".local/share/claude-multi/auth",
                                           ".local/share/claude-multi/traces", s["working_directory"]})
        self.assertIn(".config/claude-multi", s["bind_ro"])
        self.assertNotIn(".config/secrets/claude.env", str(s))
        self.assertIn(s["hardening"]["home"], ("tmpfs", "read-only", "none"))
        # Both exec links sit under the read-only data bind: no extra bind needed.
        for link in s["exec_links"].values():
            self.assertEqual(systemd_unit.bind_sets(s, link), (sorted(s["bind_rw"]), sorted(s["bind_ro"])))
        self.assertEqual(len(systemd_unit.TIER_B), 17)
        self.assertLessEqual(set(s["hardening"]["tier_b"]), set(systemd_unit.TIER_B))

    def test_notice_and_disabled_write_paths(self):
        title, body = service.failure_notice("claude-multi-gateway")
        self.assertEqual(title, "claude-multi gateway failed")
        self.assertIn("reset-failed claude-multi-gateway", body)
        self.assertIn("claude-multi gateway service install recreates them", body)
        self.assertNotIn("tmpfiles", body)
        for root in (SHIPPED_ROOT, FIXTURE_ROOT):
            static = catalog.load_catalog(root).docs["gateway"]["gateway"]["cliproxy_static"]
            self.assertTrue(static["remote-management"]["disable-control-panel"])
            self.assertTrue(static["remote-management"]["disable-auto-update-panel"])
            self.assertEqual(static["remote-management"]["secret-key"], "")
            for key in ("plugins", "discovery"):
                self.assertFalse(static[key]["enabled"])
            self.assertFalse(static["save-cooldown-status"])
            self.assertFalse(static["logging-to-file"])
        for key in ("HOME_JWT", "WRITABLE_PATH", "MANAGEMENT_STATIC_PATH", "PGSTORE_DSN",
                    "GITSTORE_GIT_URL", "OBJECTSTORE_ENDPOINT"):
            self.assertTrue(proxy.gateway_env_denied(key))
        self.assertIn("--local-model", proxy.EXEC_ARGV_FLAGS)
