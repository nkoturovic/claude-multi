"""Custom context evidence, touched-entry auth rules and XDG parity."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, custom, paths, state, strict_json
from _catalog import FIXTURE_ROOT


def registry_fixture():
    return {
        "version": 1,
        "providers": {
            "acme": {
                "base_url": "https://acme.example.invalid/v1",
                "auth_kind": "header",
                "header": "Authorization",
                "secret_env": "ACME_API_KEY",
            },
            "clean": {
                "base_url": "https://clean.example.invalid/v1",
                "auth_kind": "bearer",
                "secret_env": "CLEAN_API_KEY",
            },
        },
        "models": {
            "acme-one": {
                "wire_model": "acme-wire", "provider": "acme",
                "context_tokens": 1_000_000, "created_via": "manual",
            },
            "clean-one": {
                "wire_model": "clean-wire", "provider": "clean",
                "context_tokens": 128_000, "created_via": "manual",
            },
        },
    }


class CustomContextTests(unittest.TestCase):
    def test_operator_bounds_are_not_claimed_as_benchmarks(self):
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        for bound in (128_000, 1_000_000):
            with self.subTest(bound=bound):
                registry = registry_fixture()
                registry["models"]["acme-one"]["context_tokens"] = bound
                entry = custom.synthetic_entries(
                    registry, cliproxyapi=bundle.docs["gateway"]["gateway"]["cliproxyapi_baseline"]
                )["acme-one"]
                self.assertEqual(entry["context"], {
                    "client_tokens": bound, "declared_tokens": bound,
                    "provider_tokens": bound, "scalar_tokens": None,
                    "ordinary_profile": custom.profile_for(bound),
                    "qualification": "operator-declared (custom.json); not benchmark-verified",
                    "validated_tokens": min(bound, 200_000),
                    "user_reported_tokens": bound,
                })
                errors = []
                catalog._check_context("custom", entry, errors)
                self.assertEqual(errors, [])
        self.assertEqual(catalog.CUSTOM_VALIDATED_CAP, 200_000)


class CustomRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.env = {"HOME": str(self.home)}
        self.path = custom.registry_path(self.env)
        self.registry = registry_fixture()
        state.ensure_private_dir(self.path.parent)
        state.atomic_write(self.path, strict_json.pretty_file_bytes(self.registry))

    def add_model(self, name="new-model", provider="acme"):
        custom.add_model(
            self.env, name, provider=provider, wire_model="new-wire",
            context_tokens=128_000, created_via="manual",
        )

    def add_provider(self, name="acme", **changes):
        spec = {**self.registry["providers"][name], **changes}
        custom.add_provider(self.env, name, **spec)

    def test_predicate_is_sorted_case_insensitive_and_defaults_to_x_api_key(self):
        registry = registry_fixture()
        registry["providers"]["zeta"] = {**registry["providers"]["acme"], "header": "X-Token"}
        self.assertEqual(custom.header_rule_violations(registry), [
            ("acme", "Authorization"), ("zeta", "X-Token"),
        ])
        for header in (None, "x-api-key", "X-API-Key"):
            with self.subTest(header=header):
                spec = registry["providers"]["acme"]
                spec.pop("header", None)
                if header is not None:
                    spec["header"] = header
                self.assertEqual(custom.header_rule_violations(registry), [("zeta", "X-Token")])
        registry["providers"]["zeta"]["auth_kind"] = "bearer"
        self.assertEqual(custom.header_rule_violations(registry), [])
        self.assertEqual(custom.header_rule_violations({}), [])

    def test_load_tolerates_header_violation_but_merge_drops_provider_and_all_its_models(self):
        registry = custom.load_registry(self.env)
        self.assertEqual(registry, self.registry)
        registry["models"]["acme-two"] = copy.deepcopy(registry["models"]["acme-one"])
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        before_docs = copy.deepcopy(bundle.docs)
        before_registry = copy.deepcopy(registry)
        merged = custom.merge_docs(bundle.docs, registry)
        self.assertNotIn("acme", merged["providers"]["providers"])
        self.assertIn("clean", merged["providers"]["providers"])
        for key in ("models", "models-v2"):
            self.assertNotIn("acme-one", merged[key]["models"])
            self.assertNotIn("acme-two", merged[key]["models"])
            self.assertIn("clean-one", merged[key]["models"])
        self.assertIs(merged["models"], merged["models-v2"])
        self.assertEqual(bundle.docs, before_docs)
        self.assertEqual(registry, before_registry)

    def test_case_insensitive_and_default_header_providers_load_and_merge(self):
        for header in (None, "X-API-Key"):
            with self.subTest(header=header):
                spec = self.registry["providers"]["acme"]
                spec.pop("header", None)
                if header is not None:
                    spec["header"] = header
                custom.save_registry(self.env, self.registry)
                loaded = custom.load_registry(self.env)
                self.assertEqual(custom.header_rule_violations(loaded), [])
                merged = custom.merge_docs(catalog.load_catalog(FIXTURE_ROOT).docs, loaded)
                self.assertIn("acme", merged["providers"]["providers"])
                self.assertIn("acme-one", merged["models"]["models"])

    def test_save_registry_has_no_blanket_header_refusal(self):
        custom.save_registry(self.env, self.registry)
        self.assertEqual(custom.load_registry(self.env), self.registry)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_add_and_edit_model_on_violating_provider_refused_without_writing(self):
        for name in ("new-model", "acme-one", "clean-one"):
            with self.subTest(name=name):
                before = self.path.read_bytes()
                with self.assertRaises(custom.CustomModelsError) as raised:
                    self.add_model(name)
                self.assertEqual(str(raised.exception),
                    "refusing to save the custom registry: provider acme uses header auth "
                    "with 'Authorization', and this gateway build honors only the x-api-key "
                    'header — set "header": "x-api-key" for it in '
                    '~/.config/claude-multi/custom.json, or remove it: claude-multi custom '
                    'remove-provider acme (its models go with it)')
                self.assertEqual(self.path.read_bytes(), before)

    def test_add_and_edit_violating_provider_refused_without_writing(self):
        for name in ("acme", "new-provider", "clean"):
            with self.subTest(name=name):
                before = self.path.read_bytes()
                with self.assertRaisesRegex(custom.CustomModelsError, "honors only the x-api-key"):
                    custom.add_provider(self.env, name, **self.registry["providers"]["acme"])
                self.assertEqual(self.path.read_bytes(), before)

    def test_clean_adds_and_edits_do_not_get_blocked_by_unrelated_violation(self):
        self.add_model("new-model", "clean")
        self.add_model("clean-one", "clean")
        self.add_provider("clean", display="Renamed clean provider")
        custom.add_provider(self.env, "new-provider", **self.registry["providers"]["clean"])
        after = custom.load_registry(self.env)
        self.assertEqual(after["providers"]["acme"], self.registry["providers"]["acme"])
        self.assertEqual(after["models"]["acme-one"], self.registry["models"]["acme-one"])
        self.assertIn("new-model", after["models"])
        self.assertEqual(after["models"]["clean-one"]["wire_model"], "new-wire")
        self.assertIn("new-provider", after["providers"])
        self.assertEqual(after["providers"]["clean"]["display"], "Renamed clean provider")

    def test_provider_can_be_repaired_and_model_moved_off_violating_provider(self):
        self.add_model("acme-one", "clean")
        self.assertEqual(custom.load_registry(self.env)["models"]["acme-one"]["provider"], "clean")
        for auth_kind, header in (("header", "X-API-Key"), ("bearer", "Authorization")):
            with self.subTest(auth_kind=auth_kind):
                self.add_provider(auth_kind=auth_kind, header=header)
                self.add_model()
                self.assertEqual(custom.header_rule_violations(custom.load_registry(self.env)), [])

    def test_remove_model_always_allowed_even_with_other_violations(self):
        for name in ("clean-one", "acme-one", "missing"):
            with self.subTest(name=name):
                self.assertEqual(custom.remove_model(self.env, name), name != "missing")
                after = custom.load_registry(self.env)
                self.assertNotIn(name, after["models"])
                self.assertEqual(after["providers"], self.registry["providers"])

    def test_remove_violating_provider_cascades_only_its_models_in_sorted_order(self):
        self.registry["models"]["acme-zero"] = copy.deepcopy(self.registry["models"]["acme-one"])
        custom.save_registry(self.env, self.registry)
        self.assertEqual(custom.remove_provider(self.env, "acme"), (True, ("acme-one", "acme-zero")))
        after = custom.load_registry(self.env)
        self.assertEqual(after["models"], {"clean-one": self.registry["models"]["clean-one"]})
        self.assertEqual(after["providers"], {"clean": self.registry["providers"]["clean"]})
        self.assertEqual(custom.remove_provider(self.env, "acme"), (False, ()))

    def test_remove_clean_provider_with_models_still_refused(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(custom.CustomModelsError, "remove them first"):
            custom.remove_provider(self.env, "clean")
        self.assertEqual(self.path.read_bytes(), before)

    def test_remove_empty_providers_and_missing_provider_with_violations_present(self):
        self.assertEqual(custom.remove_provider(self.env, "missing"), (False, ()))
        for name in ("clean", "acme"):
            with self.subTest(name=name):
                custom.remove_model(self.env, f"{name}-one")
                self.assertEqual(custom.remove_provider(self.env, name), (True, ()))


class RegistryParityTests(unittest.TestCase):
    def test_xdg_path_matrix_reports_only_paths_and_state_without_writes(self):
        for presence in ("neither", "shell", "service", "different", "equal", "unset"):
            with self.subTest(presence=presence), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                env = {"HOME": tmp, "XDG_CONFIG_HOME": str(home / "xdg")}
                a = custom.registry_path(env)
                b = home / ".config/claude-multi/custom.json"
                if presence in ("shell", "different", "equal"):
                    state.ensure_private_dir(a.parent)
                    state.atomic_write(a, b"fixture-shell-content")
                if presence in ("service", "different", "equal", "unset"):
                    state.ensure_private_dir(b.parent)
                    state.atomic_write(b, b"fixture-shell-content" if presence == "equal" else b"fixture-service-content")
                if presence == "unset":
                    env.pop("XDG_CONFIG_HOME")
                before = {str(p): p.read_bytes() for p in (a, b) if p.exists()}
                result = custom.registry_parity(env)
                if presence in ("neither", "equal", "unset"):
                    self.assertIsNone(result)
                else:
                    a_text, b_text = paths.display(a, env), paths.display(b, env)
                    mismatch = "the two files differ" if presence == "different" else (
                        f"only {a_text if presence == 'shell' else b_text} exists"
                    )
                    self.assertEqual(result,
                        f"custom.json mismatch: this shell reads {a_text} (XDG_CONFIG_HOME) "
                        f"but the gateway service reads {b_text} (it runs without "
                        f"XDG_CONFIG_HOME); {mismatch} — custom models can launch but are "
                        f"not served. Keep one registry at {b_text} and run claude-multi "
                        "without XDG_CONFIG_HOME, then claude-multi-proxy init")
                    self.assertNotIn("fixture-", result)
                self.assertEqual({str(p): p.read_bytes() for p in (a, b) if p.exists()}, before)
                if presence == "neither":
                    self.assertEqual(list(home.iterdir()), [])

    def test_io_errors_are_attention_by_path_never_raised(self):
        # An unreadable service-side registry, or one that
        # disappears between listing and reading, must not crash doctor.
        real_read = Path.read_bytes
        for case in ("unreadable-service", "unreadable-shell", "vanished-service"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                env = {"HOME": tmp, "XDG_CONFIG_HOME": str(home / "xdg")}
                a = custom.registry_path(env)
                b = home / ".config/claude-multi/custom.json"
                for path in (a, b):
                    state.ensure_private_dir(path.parent)
                    state.atomic_write(path, b"fixture-registry-content")
                target = a if case == "unreadable-shell" else b
                error = FileNotFoundError(2, "No such file or directory") if case.startswith("vanished") \
                    else PermissionError(13, "Permission denied")

                def read_bytes(path, _target=target, _error=error):
                    if path == _target:
                        raise _error
                    return real_read(path)

                with mock.patch.object(Path, "read_bytes", read_bytes):
                    result = custom.registry_parity(env)
                a_text, b_text = paths.display(a, env), paths.display(b, env)
                if case == "vanished-service":
                    self.assertIn(f"only {a_text} exists", result)
                else:
                    shown = paths.display(target, env)
                    self.assertTrue(result.startswith(
                        f"custom.json unreadable: {shown} cannot be read (Permission denied), "))
                    self.assertIn(f"keep one registry at {b_text}", result)
                self.assertNotIn("fixture-", result)

    def test_xdg_equal_to_default_path_needs_no_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "XDG_CONFIG_HOME": str(Path(tmp) / ".config")}
            self.assertIsNone(custom.registry_parity(env))
