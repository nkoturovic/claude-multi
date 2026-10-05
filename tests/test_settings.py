"""Operator settings store and resolver.

Every test runs on a temp HOME / XDG_CONFIG_HOME / XDG_STATE_HOME; the
catalog is the frozen fixture or a private copy of it with test-local
``status: new`` lines. Nothing reads the operator's real config.
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import shutil
import stat
import tempfile
import types
import unittest
from pathlib import Path

from claude_multi import (
    catalog, composition, profile, proxy, sessions, settings, state, strict_json,
)
from claude_multi import validate as schema_validate
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT


def _copy_with_new_lines(case: unittest.TestCase, *keys: str) -> Path:
    temporary = Path(tempfile.mkdtemp(prefix="cm-settings-catalog-"))
    case.addCleanup(shutil.rmtree, temporary, True)
    root = temporary / "claude-multi"
    shutil.copytree(FIXTURE_ROOT, root)
    for root_dir, _dirs, files in os.walk(root):
        os.chmod(root_dir, 0o755)
        for name in files:
            os.chmod(Path(root_dir) / name, 0o644)
    path = root / "catalog" / "models.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for key in keys:
        document["models"][key]["status"] = "new"
    path.write_bytes(strict_json.pretty_file_bytes(document))
    return root


class SettingsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-settings-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
        }
        self.store = settings.SettingsStore(self.environ)
        # grok46 and qwen38 are not named by the frozen fixture composition,
        # so a copy may mark them New.
        self.bundle = catalog.load_catalog(_copy_with_new_lines(self, "grok46", "qwen38"))

    def write_raw(self, data: bytes, *, mode: int = 0o600) -> Path:
        state.ensure_private_dir(self.store.path.parent)
        state.atomic_write(self.store.path, data)
        os.chmod(self.store.path, mode)
        return self.store.path


class SettingsStoreTests(SettingsCase):
    def test_path_is_the_xdg_config_root(self) -> None:
        self.assertEqual(self.store.path, self.root / "config" / "claude-multi" / "settings.json")
        self.assertEqual(settings.settings_path(self.environ), self.store.path)
        self.assertEqual(settings.schema_path().name, "settings.schema.json")

    def test_a_relative_xdg_config_home_is_ignored(self) -> None:
        from claude_multi import choices, paths, sessions

        home = self.root / "home"
        for value in ("relative/config", "./config", ".config"):
            with self.subTest(value=value):
                environ = {"HOME": str(home), "XDG_CONFIG_HOME": value}
                self.assertEqual(paths.config_root(environ), home / ".config" / "claude-multi")
                self.assertEqual(sessions.config_root(environ), home / ".config" / "claude-multi")
                self.assertEqual(settings.settings_path(environ), home / ".config/claude-multi/settings.json")
                self.assertEqual(choices.path(environ), home / ".config/claude-multi/choices.json")
        absolute = {"HOME": str(home), "XDG_CONFIG_HOME": str(self.root / "xdg")}
        self.assertEqual(paths.config_root(absolute), self.root / "xdg" / "claude-multi")
        self.assertEqual(paths.config_root({"HOME": str(home), "XDG_CONFIG_HOME": ""}),
                         home / ".config" / "claude-multi")

    def test_absent_is_default_and_creates_nothing(self) -> None:
        self.assertEqual(self.store.load(), {"version": 1})
        self.assertFalse(self.store.path.parent.exists())
        self.assertFalse((self.root / "config").exists())

    def test_save_writes_0600_pretty_bytes_in_a_0700_dir(self) -> None:
        document = {
            "version": 1,
            "providers": {"kimi": {"enabled": False}},
            "admitted_lines": ["qwen38", "grok46"],
        }
        written = self.store.save(document, catalog=self.bundle)
        self.assertEqual(written["admitted_lines"], ["grok46", "qwen38"])
        self.assertEqual(document["admitted_lines"], ["qwen38", "grok46"])  # caller's doc untouched
        self.assertEqual(stat.S_IMODE(os.lstat(self.store.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(self.store.path.parent).st_mode), 0o700)
        self.assertEqual(self.store.path.read_bytes(), strict_json.pretty_file_bytes(written))
        self.assertEqual(self.store.load(), written)

    def test_unknown_provider_refused_on_save_tolerated_on_load(self) -> None:
        with self.assertRaisesRegex(settings.SettingsError, "unknown provider zeta"):
            self.store.save(
                {"version": 1, "providers": {"zeta": {"enabled": False}}}, catalog=self.bundle
            )
        with self.assertRaisesRegex(settings.SettingsError, "unknown line nope"):
            self.store.save({"version": 1, "admitted_lines": ["nope"]}, catalog=self.bundle)
        self.assertFalse(self.store.path.exists())
        # A custom provider is known when the registry is passed.
        registry = {"version": 1, "providers": {"zeta": {}}, "models": {}}
        self.store.save(
            {"version": 1, "providers": {"zeta": {"enabled": False}}},
            catalog=self.bundle, custom_registry=registry,
        )
        # Written by an older catalog/registry: tolerated on load, reported.
        self.write_raw(
            strict_json.pretty_file_bytes(
                {"version": 1, "providers": {"zeta": {"enabled": False}}, "admitted_lines": ["gone"]}
            )
        )
        document = self.store.load()
        eff = settings.effective(
            document, provider_ids=self.bundle.providers, line_keys=self.bundle.lines
        )
        self.assertEqual(eff.unknown, ("provider zeta", "line gone"))
        self.assertNotIn("zeta", eff.providers_enabled)
        self.assertEqual(eff.admitted_lines, frozenset())

    def test_stale_entries_do_not_block_later_updates(self) -> None:
        self.write_raw(
            strict_json.pretty_file_bytes(
                {"version": 1, "providers": {"zeta": {"enabled": False}}, "admitted_lines": ["gone"]}
            )
        )
        written = self.store.set_provider_enabled("kimi", False, catalog=self.bundle)
        self.assertEqual(written["providers"]["zeta"], {"enabled": False})
        self.assertEqual(written["providers"]["kimi"], {"enabled": False})
        written = self.store.revoke_line("gone", catalog=self.bundle)
        self.assertEqual(written["admitted_lines"], [])
        # A NEW unknown entry is still refused through update.
        with self.assertRaisesRegex(settings.SettingsError, "unknown line other"):
            self.store.update(
                lambda doc: doc["admitted_lines"].append("other"), catalog=self.bundle
            )

    def test_corrupt_or_unsafe_files_raise(self) -> None:
        cases = {
            "malformed": (b"{oops", 0o600),
            "duplicate keys": (b'{"version": 1, "version": 1}', 0o600),
            "not an object": (b"[1]", 0o600),
            "schema": (b'{"version": 2}', 0o600),
            "extra key": (b'{"version": 1, "lead_model": "sol"}', 0o600),
            "out-of-bounds policy field": (b'{"version": 1, "compaction_percent": 96}', 0o600),
            "bad provider id": (b'{"version": 1, "providers": {"../x": {}}}', 0o600),
            "group readable": (b'{"version": 1}', 0o644),
        }
        for label, (data, mode) in cases.items():
            with self.subTest(case=label):
                self.write_raw(data, mode=mode)
                with self.assertRaises(settings.SettingsError):
                    self.store.load()
        os.unlink(self.store.path)
        target = self.root / "elsewhere.json"
        target.write_bytes(b'{"version": 1}')
        os.chmod(target, 0o600)
        os.symlink(target, self.store.path)
        with self.assertRaises(settings.SettingsError):
            self.store.load()

    def test_admit_line_only_for_new_lines_and_sorted(self) -> None:
        with self.assertRaisesRegex(settings.SettingsError, "not New · Off"):
            self.store.admit_line("sol", catalog=self.bundle)
        with self.assertRaisesRegex(settings.SettingsError, "unknown catalog line"):
            self.store.admit_line("nope", catalog=self.bundle)
        self.assertFalse(self.store.path.exists())
        self.store.admit_line("qwen38", catalog=self.bundle)
        written = self.store.admit_line("grok46", catalog=self.bundle)
        self.assertEqual(written["admitted_lines"], ["grok46", "qwen38"])
        self.assertEqual(self.store.load()["admitted_lines"], ["grok46", "qwen38"])
        written = self.store.revoke_line("qwen38", catalog=self.bundle)
        self.assertEqual(written["admitted_lines"], ["grok46"])

    def test_set_provider_enabled(self) -> None:
        with self.assertRaisesRegex(settings.SettingsError, "unknown provider"):
            self.store.set_provider_enabled("zeta", False, catalog=self.bundle)
        written = self.store.set_provider_enabled("kimi", False, catalog=self.bundle)
        self.assertEqual(written, {"version": 1, "providers": {"kimi": {"enabled": False}}})
        written = self.store.set_provider_enabled("kimi", True, catalog=self.bundle)
        self.assertEqual(written["providers"]["kimi"], {"enabled": True})

    def test_writes_refused_under_a_newer_state_marker(self) -> None:
        state_root = state.ensure_private_dir(sessions.state_root(self.environ))
        state.atomic_write(state_root / sessions.STATE_MARKER, b"5\n")
        with self.assertRaises(sessions.StateMarkerError):
            self.store.save({"version": 1}, catalog=self.bundle)
        with self.assertRaises(sessions.StateMarkerError):
            self.store.admit_line("qwen38", catalog=self.bundle)
        self.assertFalse(self.store.path.parent.exists())
        self.assertEqual(self.store.load(), {"version": 1})  # reads stay available


class ResolverTests(SettingsCase):
    def _eff(self, document: dict) -> settings.Effective:
        return settings.effective(
            document, provider_ids=self.bundle.providers, line_keys=self.bundle.lines
        )

    def test_effective_defaults(self) -> None:
        eff = self._eff({"version": 1})
        self.assertEqual(set(eff.providers_enabled), set(self.bundle.providers))
        self.assertTrue(all(eff.providers_enabled.values()))
        self.assertEqual(eff.admitted_lines, frozenset())
        self.assertEqual(eff.unknown, ())
        self.assertTrue(settings.provider_enabled(eff, "kimi"))
        self.assertTrue(settings.provider_enabled(eff, "never-heard-of"))
        # Effective is Settings-level; profile overrides are
        # never folded in (compaction_percent_for applies them at compile).
        self.assertNotIn(
            "profile_overrides", inspect.signature(settings.effective).parameters
        )

    def test_line_offered_truth_table(self) -> None:
        active = {"provider": "kimi", "status": "active"}
        new = {"provider": "kimi", "status": "new"}
        for enabled in (True, False):
            for admitted in (False, True):
                document = {
                    "version": 1,
                    "providers": {"kimi": {"enabled": enabled}},
                    "admitted_lines": ["x"] if admitted else [],
                }
                eff = settings.effective(document, provider_ids=["kimi"], line_keys=["x"])
                with self.subTest(enabled=enabled, admitted=admitted):
                    self.assertEqual(settings.line_offered("x", active, eff), enabled)
                    self.assertEqual(settings.line_offered("x", new, eff), enabled and admitted)
                    # admission of ANOTHER key never offers this New line
                    self.assertFalse(settings.line_offered("y", new, eff))

    def test_snapshot_is_canonical(self) -> None:
        eff = self._eff(
            {
                "version": 1,
                "providers": {"qwen": {"enabled": False}, "kimi": {"enabled": False}},
                "admitted_lines": ["qwen38", "grok46"],
            }
        )
        snap = settings.snapshot(eff)
        self.assertEqual(snap["version"], 1)
        self.assertEqual(list(snap["providers_enabled"]), sorted(self.bundle.providers))
        self.assertFalse(snap["providers_enabled"]["kimi"])
        self.assertTrue(snap["providers_enabled"]["openai"])
        self.assertEqual(snap["admitted_lines"], ["grok46", "qwen38"])
        self.assertEqual(
            strict_json.canonical_bytes(snap),
            strict_json.canonical_bytes(settings.snapshot(self._eff(
                {"version": 1, "admitted_lines": ["grok46", "qwen38"],
                 "providers": {"kimi": {"enabled": False}, "qwen": {"enabled": False}}}
            ))),
        )
        self.assertEqual(settings.drift(snap, eff), [])

    def test_drift_annotations(self) -> None:
        before = settings.snapshot(
            self._eff({"version": 1, "providers": {"kimi": {"enabled": False}}, "admitted_lines": ["qwen38"]})
        )
        now = self._eff(
            {"version": 1, "providers": {"qwen": {"enabled": False}}, "admitted_lines": ["grok46"]}
        )
        self.assertEqual(
            settings.drift(before, now),
            [
                "provider kimi enabled since launch",
                "provider qwen disabled since launch",
                "line grok46 admitted since launch",
                "line qwen38 revoked since launch",
            ],
        )
        # A snapshot missing a provider counts it enabled; junk never raises.
        self.assertEqual(settings.drift({}, now), [
            "provider qwen disabled since launch", "line grok46 admitted since launch",
        ])
        self.assertEqual(
            settings.drift({"providers_enabled": [], "admitted_lines": [{"x": 1}]}, now),
            ["provider qwen disabled since launch", "line grok46 admitted since launch"],
        )

    def test_compaction_percent_constants(self) -> None:
        self.assertEqual(settings.COMPACTION_PERCENT_KEY, "compaction_percent")
        self.assertEqual(settings.COMPACTION_PERCENT_DEFAULT, 90)
        self.assertEqual(
            (settings.COMPACTION_PERCENT_MIN, settings.COMPACTION_PERCENT_MAX), (60, 95)
        )


_POLICY_DEFAULTS = {
    "compaction_percent": 90,
    "explore_inherit_cap_disabled": True,
    "review_round_cap": 2,
    "workflow_default_binding": None,
}


class PolicyFieldSchemaTests(SettingsCase):
    """The four lead-context fields in the settings schema."""

    def _problems(self, document: dict) -> list[str]:
        return schema_validate.validate(
            document, strict_json.load(settings.schema_path()), "$"
        )

    def test_schema_accepts_each_field_at_its_bounds(self) -> None:
        accepted = [
            {"compaction_percent": 60},
            {"compaction_percent": 95},
            {"review_round_cap": 1},
            {"review_round_cap": 3},
            {"explore_inherit_cap_disabled": True},
            {"explore_inherit_cap_disabled": False},
            {"workflow_default_binding": None},
            {"workflow_default_binding": {"model": "sol", "effort": "xhigh"}},
            {"workflow_default_binding": {"model": "opus@4.8", "effort": "low"}},
            {**_POLICY_DEFAULTS},
        ]
        for fields in accepted:
            with self.subTest(fields=fields):
                self.assertEqual(self._problems({"version": 1, **fields}), [])

    def test_schema_rejects_out_of_range_and_malformed(self) -> None:
        rejected = {
            "compaction 59": {"compaction_percent": 59},
            "compaction 96": {"compaction_percent": 96},
            "compaction bool": {"compaction_percent": True},
            "compaction float": {"compaction_percent": 90.0},
            "round cap 0": {"review_round_cap": 0},
            "round cap 4": {"review_round_cap": 4},
            "cap non-bool": {"explore_inherit_cap_disabled": 1},
            "binding ultracode": {"workflow_default_binding": {"model": "sol", "effort": "ultracode"}},
            "binding extra key": {
                "workflow_default_binding": {"model": "sol", "effort": "high", "use": "x"}
            },
            "binding missing effort": {"workflow_default_binding": {"model": "sol"}},
            "binding bad model": {"workflow_default_binding": {"model": "Sol", "effort": "high"}},
            "binding string": {"workflow_default_binding": "sol"},
        }
        for label, fields in rejected.items():
            with self.subTest(case=label):
                self.assertTrue(self._problems({"version": 1, **fields}))

    def test_schema_mirrors_the_code_constants(self) -> None:
        properties = strict_json.load(settings.schema_path())["properties"]
        self.assertEqual(
            (properties["compaction_percent"]["minimum"], properties["compaction_percent"]["maximum"]),
            (settings.COMPACTION_PERCENT_MIN, settings.COMPACTION_PERCENT_MAX),
        )
        self.assertEqual(
            (properties["review_round_cap"]["minimum"], properties["review_round_cap"]["maximum"]),
            (settings.REVIEW_ROUND_CAP_MIN, settings.REVIEW_ROUND_CAP_MAX),
        )
        binding = properties["workflow_default_binding"]["oneOf"][1]["properties"]
        self.assertEqual(tuple(binding["effort"]["enum"]), settings.WORKFLOW_BINDING_EFFORTS)
        self.assertNotIn(profile.ULTRACODE, settings.WORKFLOW_BINDING_EFFORTS)
        self.assertEqual(binding["model"]["pattern"], settings._BINDING_MODEL.pattern)
        self.assertEqual(set(settings.POLICY_KEYS), set(_POLICY_DEFAULTS))
        self.assertLessEqual(set(settings.POLICY_KEYS), set(properties))

    def test_store_round_trips_the_policy_fields(self) -> None:
        document = {
            "version": 1,
            "compaction_percent": 75,
            "explore_inherit_cap_disabled": False,
            "review_round_cap": 3,
            "workflow_default_binding": {"model": "sol", "effort": "xhigh"},
        }
        written = self.store.save(document, catalog=self.bundle)
        self.assertEqual(written, document)
        self.assertEqual(self.store.load(), document)


class PolicyResolverTests(SettingsCase):
    """Effective policy fields, the effective-percent override rule, snapshot and drift."""

    def _eff(self, document: dict) -> settings.Effective:
        return settings.effective(
            document, provider_ids=self.bundle.providers, line_keys=self.bundle.lines
        )

    def _policy(self, eff: settings.Effective) -> dict:
        return {key: getattr(eff, key) for key in _POLICY_DEFAULTS}

    def test_defaults(self) -> None:
        eff = self._eff({"version": 1})
        self.assertEqual(self._policy(eff), _POLICY_DEFAULTS)
        # Call sites that build an Effective directly stay valid.
        direct = settings.Effective(providers_enabled={}, admitted_lines=frozenset(), unknown=())
        self.assertEqual(self._policy(direct), _POLICY_DEFAULTS)

    def test_document_values(self) -> None:
        eff = self._eff({
            "version": 1,
            "compaction_percent": 70,
            "explore_inherit_cap_disabled": False,
            "review_round_cap": 1,
            "workflow_default_binding": {"model": "sol", "effort": "high"},
        })
        self.assertEqual(self._policy(eff), {
            "compaction_percent": 70,
            "explore_inherit_cap_disabled": False,
            "review_round_cap": 1,
            "workflow_default_binding": {"model": "sol", "effort": "high"},
        })

    def test_invalid_document_values_raise_naming_the_key(self) -> None:
        cases = {
            "compaction_percent": 59,
            "review_round_cap": 4,
            "explore_inherit_cap_disabled": "no",
            "workflow_default_binding": {"model": "sol", "effort": "ultracode"},
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                with self.assertRaisesRegex(settings.SettingsError, key):
                    self._eff({"version": 1, key: value})

    def test_compaction_percent_for_applies_the_profile_override(self) -> None:
        eff = self._eff({"version": 1, "compaction_percent": 80})
        self.assertEqual(settings.compaction_percent_for(eff), 80)
        self.assertEqual(settings.compaction_percent_for(eff, {}), 80)
        self.assertEqual(
            settings.compaction_percent_for(eff, {"compaction_percent": 70}), 70
        )
        # The override never folds into Effective.
        self.assertEqual(eff.compaction_percent, 80)
        self.assertEqual(settings.snapshot(eff)["compaction_percent"], 80)
        refused = {
            "review_round_cap": {"review_round_cap": 2},
            "compaction_percent": {"compaction_percent": True},
            "compaction_percent 59": {"compaction_percent": 59},
            "compaction_percent 96": {"compaction_percent": 96},
            "compaction_percent str": {"compaction_percent": "70"},
        }
        for label, overrides in refused.items():
            with self.subTest(case=label):
                with self.assertRaisesRegex(settings.SettingsError, label.split()[0]):
                    settings.compaction_percent_for(eff, overrides)

    def test_resolved_lineup_override_feeds_compaction_percent_for(self) -> None:
        # The profile path end to end: a test-local profile override of
        # 70 reaches the compile rule; the Settings-level value is untouched.
        cat = profile.LineupCatalog.from_docs(self.bundle.docs)
        seed = copy.deepcopy(self.bundle.seed_profiles["direct"])
        seed.pop("seed", None)
        seed["name"] = "direct-70"
        seed["settings_overrides"] = {"compaction_percent": 70}
        eff = self._eff({"version": 1})
        lineup = profile.resolve(seed, cat, effective=eff)
        self.assertEqual(dict(lineup.settings_overrides), {"compaction_percent": 70})
        self.assertEqual(settings.compaction_percent_for(eff, lineup.settings_overrides), 70)
        self.assertEqual(eff.compaction_percent, settings.COMPACTION_PERCENT_DEFAULT)

    def test_snapshot_round_trip(self) -> None:
        documents = [
            {"version": 1},
            {
                "version": 1,
                "providers": {"kimi": {"enabled": False}},
                "admitted_lines": ["grok46"],
                "compaction_percent": 65,
                "explore_inherit_cap_disabled": False,
                "review_round_cap": 3,
                "workflow_default_binding": {"model": "sol", "effort": "xhigh"},
            },
        ]
        for document in documents:
            with self.subTest(document=document):
                eff = self._eff(document)
                snap = settings.snapshot(eff)
                self.assertLessEqual(set(_POLICY_DEFAULTS), set(snap))
                back = settings.effective_from_snapshot(snap)
                self.assertEqual(back, eff)
                self.assertEqual(
                    strict_json.canonical_bytes(settings.snapshot(back)),
                    strict_json.canonical_bytes(snap),
                )
                # Through canonical bytes (what a record stores).
                reread = strict_json.loads(strict_json.canonical_bytes(snap))
                self.assertEqual(settings.effective_from_snapshot(reread), eff)
        self.assertIsNone(settings.snapshot(self._eff({"version": 1}))["workflow_default_binding"])

    def test_effective_from_snapshot_is_strict(self) -> None:
        good = settings.snapshot(self._eff({"version": 1}))
        # A snapshot without the policy fields reads them as defaults.
        legacy = {key: good[key] for key in ("version", "providers_enabled", "admitted_lines")}
        self.assertEqual(
            self._policy(settings.effective_from_snapshot(legacy)), _POLICY_DEFAULTS
        )
        bad = {
            "not an object": [],
            "unknown key": {**good, "lead": "sol"},
            "version": {**good, "version": 2},
            "version bool": {**good, "version": True},
            "providers_enabled": {**good, "providers_enabled": {"kimi": "yes"}},
            "admitted_lines": {**good, "admitted_lines": "grok46"},
            "missing providers_enabled": {k: v for k, v in good.items() if k != "providers_enabled"},
            "compaction_percent": {**good, "compaction_percent": 100},
            "review_round_cap": {**good, "review_round_cap": 0},
            "explore_inherit_cap_disabled": {**good, "explore_inherit_cap_disabled": None},
            "workflow_default_binding": {**good, "workflow_default_binding": {"model": "sol"}},
        }
        for label, document in bad.items():
            with self.subTest(case=label):
                with self.assertRaises(settings.SettingsError):
                    settings.effective_from_snapshot(document)

    def test_drift_lines_for_each_policy_field(self) -> None:
        before = settings.snapshot(self._eff({"version": 1}))
        now = self._eff({
            "version": 1,
            "compaction_percent": 70,
            "explore_inherit_cap_disabled": False,
            "review_round_cap": 3,
            "workflow_default_binding": {"model": "sol", "effort": "xhigh"},
        })
        self.assertEqual(settings.drift(before, now), [
            "compaction percent 70 since launch (was 90)",
            "explore inherit cap enabled since launch",
            "review round cap 3 since launch (was 2)",
            "workflow default binding sol:xhigh since launch (was off)",
        ])
        self.assertEqual(settings.drift(settings.snapshot(now), self._eff({"version": 1})), [
            "compaction percent 90 since launch (was 70)",
            "explore inherit cap disabled since launch",
            "review round cap 2 since launch (was 3)",
            "workflow default binding off since launch (was sol:xhigh)",
        ])
        self.assertEqual(settings.drift(settings.snapshot(now), now), [])
        # A 035-shape snapshot (no policy keys) against defaults: no drift;
        # junk policy values read as the default and never raise.
        legacy = {key: before[key] for key in ("version", "providers_enabled", "admitted_lines")}
        self.assertEqual(settings.drift(legacy, self._eff({"version": 1})), [])
        junk = {**before, "compaction_percent": "x", "review_round_cap": None,
                "explore_inherit_cap_disabled": 3, "workflow_default_binding": [1]}
        self.assertEqual(settings.drift(junk, self._eff({"version": 1})), [])
        self.assertEqual(settings.drift([], self._eff({"version": 1})), [])


class WorkflowBindingSaveTests(SettingsCase):
    """The save side (client-effort lines)."""

    def _save(self, binding: dict, *, catalog_obj=None) -> dict:
        return self.store.save(
            {"version": 1, "workflow_default_binding": binding},
            catalog=self.bundle if catalog_obj is None else catalog_obj,
        )

    def _line(self, predicate) -> str:
        for key in sorted(self.bundle.lines):
            if predicate(self.bundle.lines[key]):
                return key
        self.skipTest("fixture has no such line")

    def test_gateway_effort_line_accepts_any_declared_effort(self) -> None:
        key = self._line(
            lambda e: isinstance(e["efforts"], dict)
            and "agents" in e["capabilities"]
            and len(e["efforts"]) > 1
        )
        entry = self.bundle.lines[key]
        for effort in sorted(entry["efforts"]):
            with self.subTest(effort=effort):
                written = self._save({"model": key, "effort": effort})
                self.assertEqual(written["workflow_default_binding"], {"model": key, "effort": effort})
        undeclared = next(e for e in settings.WORKFLOW_BINDING_EFFORTS if e not in entry["efforts"])
        with self.assertRaisesRegex(
            settings.SettingsError, r"Settings workflow_default_binding\.effort: .*not declared"
        ):
            self._save({"model": key, "effort": undeclared})

    def test_client_effort_line_requires_its_default_effort(self) -> None:
        # The fixture's client-effort lines declare one effort each; a
        # test-local copy declares a second (the shipped Anthropic shape).
        key = self._line(
            lambda e: isinstance(e["efforts"], list) and "agents" in e["capabilities"]
        )
        entry = copy.deepcopy(self.bundle.lines[key])
        default = entry["default_effort"]
        other = next(e for e in settings.WORKFLOW_BINDING_EFFORTS if e != default)
        entry["efforts"] = [other, default]
        lines = {**self.bundle.lines, key: entry}
        catalog_obj = types.SimpleNamespace(
            providers=self.bundle.providers, lines=lines, retired=self.bundle.retired
        )
        written = self._save({"model": key, "effort": default}, catalog_obj=catalog_obj)
        self.assertEqual(written["workflow_default_binding"]["effort"], default)
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(
            settings.SettingsError, r"Settings workflow_default_binding\.effort: .*client-effort"
        ):
            self._save({"model": key, "effort": other}, catalog_obj=catalog_obj)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_refusals_name_the_field_and_write_nothing(self) -> None:
        lead_only = self._line(lambda e: "agents" not in e["capabilities"])
        null_retired = next(
            key for key, entry in sorted(self.bundle.retired.items()) if entry["successor"] is None
        )
        cases = {
            "unknown key": ({"model": "no-such-line", "effort": "high"}, r"\.model: unknown model key"),
            "lead-only line": (
                {"model": lead_only, "effort": self.bundle.lines[lead_only]["default_effort"]},
                r"\.model: .*not agents-capable",
            ),
            "retired, null successor": (
                {"model": null_retired, "effort": "high"}, r"\.model: .*needs a model choice"
            ),
        }
        for label, (binding, needle) in cases.items():
            with self.subTest(case=label):
                with self.assertRaisesRegex(
                    settings.SettingsError, "Settings workflow_default_binding" + needle
                ):
                    self._save(binding)
                self.assertFalse(self.store.path.exists())

    def test_retired_key_with_a_live_successor_resolves(self) -> None:
        key = self._line(
            lambda e: isinstance(e["efforts"], dict) and "agents" in e["capabilities"]
        )
        retired = {
            **self.bundle.retired,
            "old-line": {"successor": key, "since_catalog": 30},
        }
        catalog_obj = types.SimpleNamespace(
            providers=self.bundle.providers, lines=self.bundle.lines, retired=retired
        )
        effort = self.bundle.lines[key]["default_effort"]
        written = self._save({"model": "old-line", "effort": effort}, catalog_obj=catalog_obj)
        self.assertEqual(written["workflow_default_binding"]["model"], "old-line")

    def test_stale_binding_on_disk_does_not_block_updates(self) -> None:
        self.write_raw(strict_json.pretty_file_bytes(
            {"version": 1, "workflow_default_binding": {"model": "gone-line", "effort": "high"}}
        ))
        written = self.store.set_provider_enabled("kimi", False, catalog=self.bundle)
        self.assertEqual(
            written["workflow_default_binding"], {"model": "gone-line", "effort": "high"}
        )
        # A newly introduced bad binding is still refused through update.
        with self.assertRaisesRegex(settings.SettingsError, "workflow_default_binding"):
            self.store.update(
                lambda doc: doc.__setitem__(
                    "workflow_default_binding", {"model": "other-gone", "effort": "high"}
                ),
                catalog=self.bundle,
            )


class SharedConstantTests(unittest.TestCase):
    def test_profile_overridable_settings_is_the_settings_mapping(self) -> None:
        self.assertIs(profile.OVERRIDABLE_SETTINGS, settings.OVERRIDABLE)
        self.assertEqual(
            dict(settings.OVERRIDABLE),
            {"compaction_percent": (settings.COMPACTION_PERCENT_MIN, settings.COMPACTION_PERCENT_MAX)},
        )

    def test_default_compaction_percent_is_the_2x_constant(self) -> None:
        self.assertEqual(settings.COMPACTION_PERCENT_DEFAULT, composition.AUTO_COMPACT_PERCENT)

    def test_policy_defaults(self) -> None:
        self.assertIs(settings.EXPLORE_INHERIT_CAP_DISABLED_DEFAULT, True)
        self.assertEqual(settings.REVIEW_ROUND_CAP_DEFAULT, 2)
        self.assertEqual(
            (settings.REVIEW_ROUND_CAP_MIN, settings.REVIEW_ROUND_CAP_MAX), (1, 3)
        )


class SettingsIsNotACatalogDocumentTests(SettingsCase):
    def test_schema_is_not_a_catalog_schema(self) -> None:
        self.assertNotIn("settings", catalog.SCHEMA_NAMES)
        schema = strict_json.load(settings.schema_path())
        schema_validate.check_schema(schema)
        self.assertEqual(schema, strict_json.load(SHIPPED_ROOT / "schemas" / "settings.schema.json"))
        # The package-root settings.json (Claude Code settings) is a catalog
        # doc that this schema would refuse, and load_raw still accepts it.
        raw = catalog.load_raw(FIXTURE_ROOT)
        self.assertTrue(schema_validate.validate(raw["docs"]["settings"], schema, "$"))

    def test_load_raw_and_bundle_hash_ignore_operator_settings(self) -> None:
        before = catalog.load_catalog(FIXTURE_ROOT).bundle_sha256
        self.store.save(
            {"version": 1, "providers": {"kimi": {"enabled": False}}}, catalog=self.bundle
        )
        self.assertTrue(self.store.path.exists())
        after = catalog.load_catalog(FIXTURE_ROOT)
        self.assertEqual(after.bundle_sha256, before)
        self.assertEqual(catalog.load_raw(FIXTURE_ROOT)["docs"]["settings"],
                         strict_json.load(FIXTURE_ROOT / "settings.json"))

    def test_render_ignores_provider_enabled_false(self) -> None:
        # The real runtime render (proxy, temp HOME, fixture copy with New
        # lines, dummy secrets): disabling every provider changes nothing.
        home = state.ensure_private_dir(Path(self.environ["HOME"]))
        secrets = state.ensure_private_dir(self.root / "secrets")
        state.atomic_write(
            secrets / "claude.env",
            b"KIMI_CLAUDE_API_KEY=kimi-dummy-value\nQWEN_CLAUDE_API_KEY=qwen-dummy-value\n",
        )
        environ = {
            **self.environ,
            "CLAUDE_MULTI_SECRET_ENV": str(secrets / "claude.env"),
            "CLAUDE_MULTI_ASSETS": str(self.bundle.root),
        }
        config = proxy.config_dir(home) / "config.yaml"
        proxy.render_runtime_config(home, environ=environ, state_root=None)
        baseline = config.read_bytes()
        self.store.save(
            {"version": 1, "providers": {pid: {"enabled": False} for pid in self.bundle.providers}},
            catalog=self.bundle,
        )
        proxy.render_runtime_config(home, environ=environ, state_root=None)
        self.assertEqual(config.read_bytes(), baseline)
        self.assertIn(b"claude-multi-kimi-k3", baseline)
        self.assertIn(b"claude-multi-qwen38-max", baseline)  # a New line still renders


if __name__ == "__main__":
    unittest.main()


class PreferencesStoreTests(SettingsCase):
    """preferences.json is its own closed document."""

    def setUp(self) -> None:
        super().setUp()
        self.prefs = settings.PreferencesStore(self.environ)

    def test_absent_means_off_and_no_file_is_created(self) -> None:
        self.assertEqual(self.prefs.load(), {})
        self.assertEqual(self.prefs.feedback_drafts(), "off")
        self.assertEqual(settings.feedback_drafts_preference(self.environ), ("off", None))
        self.assertFalse(self.prefs.path.exists())
        self.assertEqual(self.prefs.path, self.store.path.parent / "preferences.json")

    def test_update_is_private_closed_and_leaves_settings_untouched(self) -> None:
        self.store.save({"version": 1}, catalog=self.bundle)
        settings_bytes = self.store.path.read_bytes()
        written = self.prefs.update(lambda document: document.__setitem__("claude_feedback_drafts", "notify"))
        self.assertEqual(written, {"claude_feedback_drafts": "notify"})
        self.assertEqual(stat.S_IMODE(self.prefs.path.stat().st_mode), 0o600)
        self.assertEqual(self.prefs.feedback_drafts(), "notify")
        self.assertEqual(self.store.path.read_bytes(), settings_bytes)
        # Never in the settings document or a record's Settings snapshot, and
        # the launch-time key set stays the 3.0 four (record hashes unchanged).
        self.assertNotIn("claude_feedback_drafts", json.loads(settings_bytes))
        self.assertNotIn("claude_feedback_drafts", settings.snapshot(settings.effective({"version": 1},
                         provider_ids=self.bundle.providers, line_keys=self.bundle.lines)))
        self.assertEqual(sessions.LAUNCH_TIME_SETTINGS_KEYS, ("availableModels", "modelPicker", "model", "env"))
        for bad in ({"claude_feedback_drafts": "quiet"}, {"version": 1}, {"other": True}):
            with self.subTest(bad=bad), self.assertRaises(settings.SettingsError):
                self.prefs.update(lambda document, bad=bad: document.update(bad))
        self.assertEqual(self.prefs.feedback_drafts(), "notify")

    def test_unreadable_preferences_compile_the_restrictive_default(self) -> None:
        state.ensure_private_dir(self.prefs.path.parent)
        state.atomic_write(self.prefs.path, b"{not json")
        value, problem = settings.feedback_drafts_preference(self.environ)
        self.assertEqual(value, "off")
        self.assertIn("corrupt", problem)
        state.atomic_write(self.prefs.path, b'{"claude_feedback_drafts": "notify"}\n')
        os.chmod(self.prefs.path, 0o644)
        value, problem = settings.feedback_drafts_preference(self.environ)
        self.assertEqual(value, "off")
        self.assertIsNotNone(problem)

    def test_schema_is_closed_and_mirrored(self) -> None:
        shipped = (SHIPPED_ROOT / "schemas" / "preferences.schema.json").read_bytes()
        self.assertEqual(shipped, (FIXTURE_ROOT / "schemas" / "preferences.schema.json").read_bytes())
        schema = json.loads(shipped)
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(set(schema["properties"]), {"claude_feedback_drafts"})
        self.assertEqual(schema["properties"]["claude_feedback_drafts"]["enum"], ["off", "notify"])
