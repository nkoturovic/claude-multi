"""Managed policy and readable settings layers, entirely fixture based."""
import tempfile
import unittest
from pathlib import Path

from claude_multi import managed, strict_json


class ManagedSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, path, doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(strict_json.canonical_file_bytes(doc))

    def test_missing_malformed_and_nonobject_layers(self):
        self.assertEqual(managed.read_locks(self.root), [])
        path = self.root / "managed-settings.json"
        for raw in (b"{bad", b"[]", b'"bad"', b"\xff", b" " * ((1 << 20) + 1)):
            path.write_bytes(raw)
            self.assertEqual(managed.read_locks(self.root), [])

    def test_locks_are_ordered_names_only_and_notes_are_separate(self):
        self.write(self.root / "managed-settings.json", {
            "disableAllHooks": True, "model": "DO-NOT-REPORT-VALUE",
            "env": {"ANTHROPIC_BASE_URL": "DO-NOT-REPORT-VALUE",
                    "ANTHROPIC_DEFAULT_TEST_MODEL": "secret", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "secret",
                    "UNRELATED": "secret"},
            "allowManagedPermissionRulesOnly": True, "apiKeyHelper": "secret",
            "forceLoginMethod": "secret", "forceLoginOrgUUID": "secret",
        })
        self.write(self.root / "managed-settings.d/b.json", {"availableModels": []})
        self.write(self.root / "managed-settings.d/a.json", {"allowManagedHooksOnly": True})
        locks = managed.read_locks(self.root)
        self.assertEqual([lock.path.name for lock in locks][-2:], ["a.json", "b.json"])
        self.assertEqual(sum(lock.kind == "note" for lock in locks), 2)
        self.assertEqual(sum(lock.kind == "credential" for lock in locks), 2)
        self.assertEqual(sum(lock.kind == "hooks" for lock in locks), 2)
        self.assertEqual(sum(lock.kind == "model" for lock in locks), 5)
        self.assertNotIn("DO-NOT-REPORT-VALUE", repr(locks))
        self.assertNotIn("secret", repr(locks))
        self.assertNotIn("UNRELATED", repr(locks))

    def test_false_hook_flags_are_not_locks(self):
        self.write(self.root / "managed-settings.json", {
            "disableAllHooks": False, "allowManagedHooksOnly": False,
            "allowManagedPermissionRulesOnly": False,
        })
        self.assertEqual(managed.read_locks(self.root), [])

    def test_all_env_layers_only_read_env_maps(self):
        home, project, policy = self.root / "home", self.root / "project", self.root / "policy"
        paths = [home / ".claude/settings.json", project / ".claude/settings.json",
                 project / ".claude/settings.local.json", policy / "managed-settings.json",
                 policy / "managed-settings.d/01.json"]
        for n, path in enumerate(paths):
            self.write(path, {"HTTPS_PROXY": "ignored", "env": {"no_proxy": str(n), "bad": False}})
        layers = managed.env_layers({"HOME": str(home)}, project, policy)
        self.assertEqual(layers, [(p, {"no_proxy": str(n)}) for n, p in enumerate(paths)])

    def test_user_layers_are_home_relative_and_policy_comes_last_only_for_the_mode(self):
        # The managed client runs without CLAUDE_CONFIG_DIR, so a value in the
        # launcher's environment never selects the user layer.
        home, project, policy = self.root / "home", self.root / "project", self.root / "policy"
        self.write(policy / "managed-settings.json", {})
        environ = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(self.root / "elsewhere")}
        user = [home / ".claude/settings.json", project / ".claude/settings.json",
                project / ".claude/settings.local.json"]
        self.assertEqual(managed.user_settings_layers(environ, project), user)
        self.assertEqual(managed.permission_mode_layers(environ, project, policy),
                         [*user, policy / "managed-settings.json"])
        self.assertEqual([path for path, _env in managed.env_layers(environ, project, policy)],
                         [*user, policy / "managed-settings.json"])


class ManagedRootTests(unittest.TestCase):
    def test_one_policy_root_per_operating_system(self):
        self.assertEqual(str(managed.managed_root_for("linux")), "/etc/claude-code")
        self.assertEqual(str(managed.managed_root_for("darwin")), "/Library/Application Support/ClaudeCode")
        self.assertEqual(str(managed.managed_root_for("win32")), "C:\\Program Files\\ClaudeCode")
        # WSL and other POSIX systems read the Linux root.
        self.assertEqual(str(managed.managed_root_for("freebsd14")), "/etc/claude-code")


class PolicyPreflightTests(unittest.TestCase):
    """The managed-policy preflight: BLOCK or Attention, never a policy value."""

    BASE = "http://127.0.0.1:18317"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, doc, name="managed-settings.json"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(strict_json.canonical_file_bytes(doc))
        return path

    def findings(self, selectors=(), pin="2.1.286"):
        return managed.policy_findings(self.root, pin_version=pin, base_url=self.BASE, selectors=selectors)

    def kinds(self, **kwargs):
        return sorted((lock.key, lock.kind, lock.blocks) for lock in self.findings(**kwargs))

    def test_version_bounds_that_exclude_the_pin_block(self):
        self.write({"requiredMinimumVersion": "2.1.300"})
        self.assertEqual(self.kinds(), [("requiredMinimumVersion", "version", True)])
        self.write({"requiredMaximumVersion": "2.1.200"})
        self.assertEqual(self.kinds(), [("requiredMaximumVersion", "version", True)])
        self.write({"requiredMinimumVersion": "2.1.286", "requiredMaximumVersion": "2.2"})
        self.assertEqual(self.kinds(), [])
        self.write({"requiredMinimumVersion": "latest"})
        self.assertEqual(self.kinds(), [("requiredMinimumVersion", "version-unknown", False)])
        text = managed.finding_text(managed.policy_findings(self.root, pin_version="2.1.286")[0])
        self.assertIn("Claude Code 2.1.286", text)
        self.assertNotIn("latest", text)

    def test_allowed_providers_needs_a_pinned_custom_endpoint(self):
        self.write({"allowedProviders": ["anthropic", "bedrock"]})
        self.assertEqual(self.kinds(), [("allowedProviders", "provider", True)])
        self.write({"allowedProviders": ["customEndpoint"], "env": {"ANTHROPIC_BASE_URL": "https://gw.example"}})
        self.assertEqual(self.kinds(), [("allowedProviders", "provider", True), ("env.ANTHROPIC_BASE_URL", "model", False)])
        self.assertNotIn("gw.example", repr(self.findings()))
        self.write({"allowedProviders": ["customEndpoint"], "env": {"ANTHROPIC_BASE_URL": self.BASE + "/"}})
        self.assertEqual(self.kinds(), [("env.ANTHROPIC_BASE_URL", "model", False)])
        # Not a list: the client ignores it (every provider allowed).
        self.write({"allowedProviders": "customEndpoint"})
        self.assertEqual(self.kinds(), [])

    def test_credential_and_hook_locks_block(self):
        self.write({"apiKeyHelper": "SECRET-VALUE", "forceLoginOrgUUID": "SECRET-VALUE",
                    "allowManagedHooksOnly": True})
        self.assertEqual(self.kinds(), [("allowManagedHooksOnly", "hooks", True),
                                        ("apiKeyHelper", "credential", True),
                                        ("forceLoginOrgUUID", "credential", True)])
        self.assertNotIn("SECRET-VALUE", repr(self.findings()))
        self.assertNotIn("SECRET-VALUE", " ".join(managed.finding_text(lock) for lock in self.findings()))

    def test_denied_models_block_only_a_bound_line(self):
        self.write({"deniedModels": ["opus", "claude-sonnet-5", "best"]})
        self.assertEqual(self.kinds(selectors=["claude-multi-sol-high[1m]"]),
                         [("deniedModels", "models", False)])
        for selector in ("claude-opus-5-5[1m]", "claude-sonnet-5-5", "claude-sonnet-5-20260101"):
            with self.subTest(selector=selector):
                found = self.findings(selectors=["claude-multi-sol-high[1m]", selector])
                self.assertEqual([(lock.kind, lock.blocks) for lock in found], [("denied", True)])
                self.assertIn(selector, found[0].effect)

    def test_available_models_match_blocks_a_line_the_policy_does_not_admit(self):
        self.write({"availableModelsMatch": "exact", "availableModels": ["claude-opus-5"]})
        locks = self.findings(selectors=["claude-opus-5-5[1m]"])
        self.assertIn(("availableModelsMatch", "denied", True), [(l.key, l.kind, l.blocks) for l in locks])
        locks = self.findings(selectors=["claude-opus-5-20260101"])
        self.assertIn(("availableModelsMatch", "models", False), [(l.key, l.kind, l.blocks) for l in locks])
        # Without a policy list the session's own exact fence admits its lines.
        self.write({"availableModelsMatch": "exact"})
        self.assertEqual(self.kinds(selectors=["claude-opus-5-5[1m]"]), [("availableModelsMatch", "models", False)])

    def test_matching_rules(self):
        self.assertTrue(managed.denied_by("us.anthropic.claude-opus-4-1-20250805-v1:0", ["claude-opus-4-1"]))
        self.assertFalse(managed.denied_by("claude-opus-5", ["claude-opus-5-5"]))
        self.assertFalse(managed.denied_by("claude-opus-50", ["claude-opus-5"]))
        self.assertTrue(managed.allowed_by("claude-opus-5-5", ["claude-opus-5"], "prefix"))
        self.assertFalse(managed.allowed_by("claude-opus-5-5", ["claude-opus-5"], "exact"))
        self.assertTrue(managed.allowed_by("claude-opus-5-fast", ["claude-opus-5"], "exact"))
        self.assertTrue(managed.allowed_by("claude-fable-5-1[1m]", ["fable"], "exact"))
        self.assertTrue(managed.allowed_by("anything", None))

    def test_split_policy_files_are_judged_as_one_merged_source(self):
        # The client merges the base file and the drop-ins (name order) into
        # one managed source before it admits a provider.
        providers = self.write({"allowedProviders": ["customEndpoint"]}, "managed-settings.d/10-providers.json")
        endpoint = self.write({"env": {"ANTHROPIC_BASE_URL": self.BASE}}, "managed-settings.d/20-endpoint.json")
        self.assertEqual(self.kinds(), [("env.ANTHROPIC_BASE_URL", "model", False)])
        self.assertEqual([lock.path for lock in self.findings()], [endpoint])
        # A genuinely different endpoint still blocks, named by the file
        # that set allowedProviders.
        self.write({"env": {"ANTHROPIC_BASE_URL": "https://gw.example"}}, "managed-settings.d/20-endpoint.json")
        found = [lock for lock in self.findings() if lock.key == "allowedProviders"]
        self.assertEqual([(lock.kind, lock.blocks, lock.path) for lock in found], [("provider", True, providers)])
        self.assertNotIn("gw.example", repr(self.findings()))
        # A later file's scalar wins: the base pins the gateway, a drop-in
        # moves the endpoint away.
        endpoint.unlink()
        self.write({"env": {"ANTHROPIC_BASE_URL": self.BASE}})
        self.write({"env": {"ANTHROPIC_BASE_URL": "https://other.example"}}, "managed-settings.d/30-move.json")
        self.assertIn(("allowedProviders", "provider", True), self.kinds())

    def test_drop_in_model_lists_join_and_later_bounds_win(self):
        self.write({"availableModels": ["claude-opus-5"], "availableModelsMatch": "exact"})
        self.write({"availableModels": ["claude-opus-5-5"]}, "managed-settings.d/10.json")
        # The joined list admits the drop-in's model under the base's mode.
        self.assertIn(("availableModelsMatch", "models", False), self.kinds(selectors=["claude-opus-5-5[1m]"]))
        self.write({"deniedModels": ["claude-sonnet-5"]}, "managed-settings.d/20.json")
        self.write({"deniedModels": ["claude-opus-5-5"]}, "managed-settings.d/30.json")
        found = self.findings(selectors=["claude-sonnet-5-20260101"])
        self.assertIn(("deniedModels", "denied", True), [(lock.key, lock.kind, lock.blocks) for lock in found])
        self.write({"requiredMinimumVersion": "2.1.200"}, "managed-settings.d/40.json")
        self.write({"requiredMinimumVersion": "2.1.300"}, "managed-settings.d/50.json")
        version = [lock for lock in self.findings() if lock.key == "requiredMinimumVersion"]
        self.assertEqual([(lock.kind, lock.path.name) for lock in version], [("version", "50.json")])

    def test_duplicate_keys_are_read_like_the_client(self):
        # JSON.parse keeps a duplicate key's last value.
        path = self.root / "managed-settings.json"
        path.write_bytes(b'{"allowManagedHooksOnly": false, "allowManagedHooksOnly": true}')
        self.assertEqual(self.kinds(), [("allowManagedHooksOnly", "hooks", True)])
        path.write_bytes(b'{"requiredMaximumVersion": "9.9.9", "requiredMaximumVersion": "2.1.200"}')
        self.assertEqual(self.kinds(), [("requiredMaximumVersion", "version", True)])
        path.write_bytes(b'{"allowManagedHooksOnly": true, "allowManagedHooksOnly": false}')
        self.assertEqual(self.kinds(), [])

    def test_a_policy_file_that_cannot_be_used_is_reported(self):
        path = self.root / "managed-settings.json"
        for raw, reason in ((b"{bad", "is not valid JSON"), (b"[]", "is not a JSON object"),
                            (b'{"a": NaN}', "is not valid JSON")):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                found = self.findings()
                self.assertEqual([(lock.kind, lock.blocks) for lock in found], [("unreadable", False)])
                self.assertIn(reason, managed.finding_text(found[0]))
        path.write_bytes(b'{"model": "' + b"x" * (1 << 20) + b'"}')
        found = self.findings()
        self.assertEqual([(lock.kind, lock.blocks) for lock in found], [("unchecked", True)])
        self.assertIn("larger than 1 MiB", managed.finding_text(found[0]))
        path.unlink()
        self.assertEqual(self.findings(), [])  # absent: nothing to report
        path.mkdir()
        self.assertEqual([lock.kind for lock in self.findings()], ["unreadable"])


class SettingsSkewTests(unittest.TestCase):
    """Settings keys the pin does not know make the user's mode untrusted."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.project = Path(self.temp.name) / "project"
        self.policy = Path(self.temp.name) / "policy"
        self.environ = {"HOME": str(self.home)}

    def write(self, path, doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(strict_json.canonical_file_bytes(doc))

    def test_unknown_keys_are_named_sorted_and_only_for_user_layers(self):
        known = {"permissions", "env", "model"}
        user = self.home / ".claude/settings.json"
        self.write(user, {"permissions": {}, "zNew": 1, "aNew": "SECRET"})
        self.write(self.policy / "managed-settings.json", {"policyOnlyKey": True})
        self.assertEqual(managed.unknown_settings_keys(user, known), ["aNew", "zNew"])
        self.assertEqual(managed.skew_layers(self.environ, self.project, known), [(user, ["aNew", "zNew"])])
        self.assertEqual(managed.skew_layers(self.environ, self.project, None), [])

    def test_a_skewed_layer_never_counts_as_configuring_the_mode(self):
        known = {"permissions"}
        user = self.home / ".claude/settings.json"
        self.write(user, {"permissions": {"defaultMode": "acceptEdits"}})
        self.assertTrue(managed.permission_mode_configured(self.environ, self.project, self.policy, known_keys=known))
        self.write(user, {"permissions": {"defaultMode": "acceptEdits"}, "newerKey": True})
        self.assertTrue(managed.permission_mode_configured(self.environ, self.project, self.policy))
        self.assertFalse(managed.permission_mode_configured(self.environ, self.project, self.policy, known_keys=known))
        # A policy file is never skipped for keys the pin lacks.
        self.write(self.policy / "managed-settings.json", {"permissions": {"defaultMode": "plan"}, "newerKey": 1})
        self.assertTrue(managed.permission_mode_configured(self.environ, self.project, self.policy, known_keys=known))

    def test_a_skewed_user_layer_decides_whatever_another_user_layer_configures(self):
        known = {"permissions"}
        user = self.home / ".claude/settings.json"
        project = self.project / ".claude/settings.json"
        local = self.project / ".claude/settings.local.json"
        self.write(user, {"permissions": {"deny": ["Read(./secret)"]}, "newerKey": True})
        for other in (project, local):
            with self.subTest(layer=other.name):
                self.write(other, {"permissions": {"defaultMode": "auto"}})
                self.assertFalse(managed.permission_mode_configured(
                    self.environ, self.project, self.policy, known_keys=known))
                self.assertTrue(managed.permission_mode_configured(self.environ, self.project, self.policy))
                other.unlink()
        # The skewed layer may be the project's own file.
        self.write(user, {"permissions": {"defaultMode": "acceptEdits"}})
        self.write(local, {"permissions": {"defaultMode": "auto"}, "newerKey": 1})
        self.assertFalse(managed.permission_mode_configured(self.environ, self.project, self.policy, known_keys=known))
        # A managed policy mode outranks the compiled flag settings: it counts.
        self.write(self.policy / "managed-settings.d/10.json", {"permissions": {"defaultMode": "plan"}})
        self.assertTrue(managed.policy_configures_default_mode(self.policy))
        self.assertTrue(managed.permission_mode_configured(self.environ, self.project, self.policy, known_keys=known))
