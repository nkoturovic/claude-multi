"""Converge, the expected plan and ``doctor --repair[-all]``.

Records and scopes come from real v4 launches (``_v4.V4Case``). "The catalog
gained a line in the lead class after launch" is a custom model added to the
merged view (``custom.json``; every compile reads catalog ∪
custom), so no catalog file is copied.
"""

from __future__ import annotations

import copy
import contextlib
import io
import json
import os
import shutil
import unittest
from unittest import mock

from claude_multi import catalog, cli, custom, profile, scope, sessions, state, strict_json, transition
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog
from _v4 import V4Case
import _v3


def _converge(case: V4Case, mid: str) -> list[str]:
    return transition.converge(
        case.store.root, case.store, mid, runtime_parts=case.runtime.converge_parts()
    )


def _expected(case: V4Case, mid: str) -> transition.ExpectedPlan:
    parts = case.runtime.converge_parts()
    live = case.live(mid)
    return transition.expected_plan(
        case.store.load(mid),
        docs=parts.docs,
        prompt_bodies=parts.prompt_bodies,
        state_root=case.store.root,
        hook_command=parts.hook_command,
        token_helper_command=parts.token_helper_command,
        live=live if live.exists() else None,
    )


class ConvergeV4Tests(V4Case):
    def setUp(self) -> None:
        super().setUp()
        self.record = self.launch_fresh()
        self.mid = self.record["managed_id"]

    def _add_lead_class_line(self) -> None:
        """A test-local catalog copy that gained a line in the lead class (``sol2``)."""

        root = self.root / "assets"
        shutil.copytree(FIXTURE_ROOT, root)
        models_path = root / "catalog" / "models.json"
        document = json.loads(models_path.read_text())
        line = copy.deepcopy(document["models"]["sol"])
        line["display"] = "GPT-5.6 Sol Two"
        line["wire_model"] = "gpt-5.6-sol-two"
        for effort, spec in line["efforts"].items():
            spec["selector"] = f"gpt-multi-sol2-{effort}[1m]"
        document["models"]["sol2"] = line
        models_path.write_text(json.dumps(document, indent=2) + "\n")
        self.runtime = self.make_runtime(asset_root=root)

    def _launch_bytes(self) -> tuple[dict, bytes]:
        live = self.live(self.mid)
        settings_doc = strict_json.loads((live / "settings.json").read_bytes())
        return (
            {k: settings_doc.get(k) for k in sessions.LAUNCH_TIME_SETTINGS_KEYS},
            (live / "lead-set.json").read_bytes(),
        )

    def test_clean_scope_matches(self) -> None:
        self.assertIn("live scope matches the record-authoritative compile", _converge(self, self.mid))

    def test_v3_and_gen0_records_are_reported_not_written(self) -> None:
        record = copy.deepcopy(self.store.load(self.mid))
        record["lineup_generation"] = 0
        record.pop("launch_fence")
        record.pop("scope_lead")
        self.store.save(record)
        before = self.tree(self.live(self.mid))
        self.assertEqual(
            _converge(self, self.mid),
            [f"session {self.mid[:8]} runs a legacy scope; it converges at its next launch with this launcher"],
        )
        self.assertEqual(self.tree(self.live(self.mid)), before)
        other = "55555555-5555-4555-8555-555555555555"
        v3 = _v3.make_record(
            session_id=other,
            cwd=self.runtime.cwd,
            composition_name="default",
            snapshot=_v3.snapshot_for(self.runtime.catalog.docs),
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
            mode="durable",
            scope_generation=1,
        )
        _v3.save_any(self.store, v3)
        self.assertEqual(
            _converge(self, other),
            [f"session {other[:8]} is a legacy record; run claude-multi migrate (or resume it) first"],
        )
        self.assertEqual(self.store.load(other)["version"], 3)

    def test_drift_is_swapped_and_prev_holds_the_drifted_scope(self) -> None:
        agent = self.live(self.mid) / ".claude" / "agents" / "cm-reviewer.md"
        state.atomic_write(agent, b"tampered\n")
        report = _converge(self, self.mid)
        self.assertTrue(any("live scope drifted" in line for line in report), report)
        self.assertNotEqual(agent.read_bytes(), b"tampered\n")
        self.assertEqual(
            (self.prev(self.mid) / ".claude" / "agents" / "cm-reviewer.md").read_bytes(),
            b"tampered\n",
        )
        self.assertIn("live scope matches the record-authoritative compile", _converge(self, self.mid))

    def test_prev_is_renamed_back_in_the_crash_window(self) -> None:
        live = self.live(self.mid)
        os.rename(live, self.prev(self.mid))
        report = _converge(self, self.mid)
        self.assertIn(
            "live scope was missing; restored .prev as the live scope (crash between swap renames)",
            report,
        )
        self.assertIn("live scope matches the record-authoritative compile", report)

    def test_stale_new_is_removed(self) -> None:
        staging = self.store.root / "scopes" / f".{self.mid}.new"
        state.ensure_private_dir(staging)
        report = _converge(self, self.mid)
        self.assertFalse(staging.exists())
        self.assertTrue(any("removed stale staged scope" in line for line in report))

    def test_catalog_moved_binding_refuses_without_a_write(self) -> None:
        record = self.store.load(self.mid)
        applied = copy.deepcopy(record["applied"])
        applied["agents"]["cm-reviewer"]["selector"] = "gpt-multi-sol-medium[1m]"
        self.mutate(self.mid, applied=applied)
        before = self.tree(self.live(self.mid))
        report = _converge(self, self.mid)
        self.assertTrue(
            any(
                line.startswith("the installed catalog resolves reviewer to gpt-multi-sol-xhigh[1m] "
                                "(recorded gpt-multi-sol-medium[1m])")
                for line in report
            ),
            report,
        )
        self.assertEqual(self.tree(self.live(self.mid)), before)

    def test_launch_time_items_survive_a_repair_after_the_catalog_gained_a_line(self) -> None:
        launch_items, lead_set = self._launch_bytes()
        self._add_lead_class_line()
        agent = self.live(self.mid) / ".claude" / "agents" / "cm-reviewer.md"
        state.atomic_write(agent, b"tampered\n")
        ep = _expected(self, self.mid)
        self.assertTrue(ep.launch_files_kept)
        self.assertTrue(ep.catalog_launch_differs)
        out = io.StringIO()
        self.assertEqual(cli.main(["doctor", "--repair", self.mid], runtime=self.runtime,
                                  output_stream=out, interactive=False), 0)
        self.assertNotEqual(agent.read_bytes(), b"tampered\n")
        self.assertEqual(self._launch_bytes(), (launch_items, lead_set))
        _problems, attention, _info = cli._check_scope_integrity(
            self.runtime, self.store.load(self.mid)
        )
        self.assertIn(
            f"session {self.mid[:8]}: fence/picker differ from the installed catalog (catalog "
            "changed since launch); applies at the next resume",
            attention,
        )

    def test_deleted_settings_with_an_unchanged_catalog_rebuilds_the_launch_bytes(self) -> None:
        launch_items, lead_set = self._launch_bytes()
        (self.live(self.mid) / "settings.json").unlink()
        report = _converge(self, self.mid)
        self.assertFalse(any("need a relaunch" in line for line in report), report)
        self.assertEqual(self._launch_bytes(), (launch_items, lead_set))
        record = self.store.load(self.mid)
        settings_doc = strict_json.loads((self.live(self.mid) / "settings.json").read_bytes())
        self.assertEqual(sessions.launch_digest(settings_doc, lead_set), record["launch_fence"])

    def test_deleted_settings_after_the_catalog_gained_a_line_needs_a_relaunch(self) -> None:
        self._add_lead_class_line()
        (self.live(self.mid) / "settings.json").unlink()
        report = _converge(self, self.mid)
        self.assertTrue(
            any(
                line.startswith("launch-time files rebuilt from the installed catalog (")
                and line.endswith(
                    f"live agent changes for {self.mid[:8]} need a relaunch until its next launch"
                )
                for line in report
            ),
            report,
        )

    def test_tampered_launch_time_items_are_rewritten_to_the_launch_bytes(self) -> None:
        launch_items, lead_set = self._launch_bytes()
        live = self.live(self.mid)
        for case in ("lead-set row", "env base url"):
            with self.subTest(case):
                if case == "lead-set row":
                    doc = strict_json.loads((live / "lead-set.json").read_bytes())
                    doc["rows"][0]["label"] = "edited"
                    state.atomic_write(live / "lead-set.json", strict_json.canonical_file_bytes(doc))
                else:
                    doc = strict_json.loads((live / "settings.json").read_bytes())
                    doc["env"]["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9999"
                    state.atomic_write(live / "settings.json", strict_json.canonical_file_bytes(doc))
                ep = _expected(self, self.mid)
                self.assertFalse(ep.launch_files_kept)
                self.assertTrue(ep.rebuilt_fence_matches)
                _converge(self, self.mid)
                self.assertEqual(self._launch_bytes(), (launch_items, lead_set))

    def test_generation_fencing_is_persisted(self) -> None:
        live = self.live(self.mid)
        state.atomic_write(live / "lineup.gen", b"4 000000000000\n")
        token = self.store.load(self.mid)["mutation_token"]
        report = _converge(self, self.mid)
        self.assertIn("lineup generation fenced 1 → 5 (a crashed live apply published 4)", report)
        record = self.store.load(self.mid)
        self.assertEqual(record["lineup_generation"], 5)
        self.assertNotEqual(record["mutation_token"], token)
        self.assertEqual((live / "lineup.gen").read_text().split()[0], "5")

    def test_epoch_bump_after_launch_is_no_catalog_change(self) -> None:
        # sessions link / resolve-fork bump the parent's epoch.
        self.mutate(self.mid, launch_epoch=self.store.load(self.mid)["launch_epoch"] + 1)
        ep = _expected(self, self.mid)
        self.assertTrue(ep.launch_files_kept)
        self.assertFalse(ep.catalog_launch_differs)
        (self.live(self.mid) / "settings.json").unlink()
        ep = _expected(self, self.mid)
        self.assertFalse(ep.launch_files_kept)
        self.assertTrue(ep.rebuilt_fence_matches)

    def test_migration_lock_makes_converge_busy(self) -> None:
        lock = sessions.migration_lock(self.store.root)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        with self.assertRaises(sessions.MigrationBusyError):
            _converge(self, self.mid)
        out = io.StringIO()
        self.assertEqual(cli._doctor_repair(self.runtime, self.mid, out), 1)
        self.assertIn(f"skipped {self.mid[:8]}: migration or restore in progress", out.getvalue())


class PermissionDefaultModeConvergeTests(V4Case):
    """Converge (``doctor --repair``, ``--repair-all
    --include-live``) of a live scope keeps the scope's own on-disk presence
    or absence of ``permissions.defaultMode`` as the decision input; a layer
    edited after launch never re-decides it; only resume does."""

    def _mode_present(self, mid: str) -> bool:
        settings = strict_json.loads((self.live(mid) / "settings.json").read_bytes())
        return "defaultMode" in settings["permissions"]

    def test_converge_keeps_presence_and_resume_decides(self) -> None:
        user = self.root / "home" / ".claude" / "settings.json"
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.assertTrue(self._mode_present(mid))
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_bytes(strict_json.canonical_file_bytes({"permissions": {"defaultMode": "acceptEdits"}}))
        # A layer configured after launch: converge keeps the compiled key.
        self.assertEqual(_expected(self, mid).plan.settings["permissions"]["defaultMode"],
                         scope.PERMISSION_DEFAULT_MODE)
        _converge(self, mid)
        self.assertTrue(self._mode_present(mid))
        # A 3.0-shaped live scope without the key: converge never adds it.
        path = self.live(mid) / "settings.json"
        document = strict_json.loads(path.read_bytes())
        document["permissions"].pop("defaultMode")
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        user.unlink()
        self.assertNotIn("defaultMode", _expected(self, mid).plan.settings["permissions"])
        self.assertIn("live scope matches", "\n".join(_converge(self, mid)))
        out = io.StringIO()
        self.assertEqual(cli.main(["doctor", "--repair-all", "--include-live"], runtime=self.runtime,
                                  output_stream=out, interactive=False), 0)
        self.assertFalse(self._mode_present(mid))
        # Resume decides from the layers (none configured: compiled).
        self.resume(mid)
        self.assertTrue(self._mode_present(mid))


class RepairAllTests(V4Case):
    def test_skips_live_or_unknown_unless_include_live(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        agent = self.live(mid) / ".claude" / "agents" / "cm-reviewer.md"
        state.atomic_write(agent, b"tampered\n")
        out = io.StringIO()
        self.assertEqual(cli.main(["doctor", "--repair-all"], runtime=self.runtime,
                                  output_stream=out, interactive=False), 0)
        self.assertIn(
            f"skipped {mid[:8]}: last event none (live or unknown) — rerun with --include-live",
            out.getvalue(),
        )
        self.assertEqual(agent.read_bytes(), b"tampered\n")
        out = io.StringIO()
        self.assertEqual(cli.main(["doctor", "--repair-all", "--include-live"], runtime=self.runtime,
                                  output_stream=out, interactive=False), 0)
        self.assertNotEqual(agent.read_bytes(), b"tampered\n")

    def test_ended_sessions_converge_and_live_markers_skip(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.mutate(mid, last_event_source="end")
        agent = self.live(mid) / ".claude" / "agents" / "cm-reviewer.md"
        state.atomic_write(agent, b"tampered\n")
        self.runtime.live_prefixes = frozenset({mid[:8]})
        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', lambda root=None: frozenset({mid[:8]})):
            out = io.StringIO()
            cli.main(["doctor", "--repair-all"], runtime=self.runtime, output_stream=out, interactive=False)
        self.assertEqual(agent.read_bytes(), b"tampered\n")
        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', lambda root=None: frozenset()):
            out = io.StringIO()
            cli.main(["doctor", "--repair-all"], runtime=self.runtime, output_stream=out, interactive=False)
        self.assertNotEqual(agent.read_bytes(), b"tampered\n")

    def test_unobservable_daemon_liveness_counts_as_live_or_unknown(self) -> None:
        # Unknown daemon liveness never reads as "not live".
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.mutate(mid, last_event_source="end")
        agent = self.live(mid) / ".claude" / "agents" / "cm-reviewer.md"
        state.atomic_write(agent, b"tampered\n")
        self.runtime.background_liveness = lambda: sessions.BackgroundLiveness(
            False, frozenset(), "symlink in daemon root")
        out = io.StringIO()
        cli.main(["doctor", "--repair-all"], runtime=self.runtime, output_stream=out, interactive=False)
        self.assertEqual(agent.read_bytes(), b"tampered\n")
        self.assertIn(f"skipped {mid[:8]}: last event end (live or unknown) — rerun with --include-live",
                      out.getvalue())
        out = io.StringIO()
        cli.main(["doctor", "--repair-all", "--include-live"], runtime=self.runtime, output_stream=out,
                 interactive=False)
        self.assertNotEqual(agent.read_bytes(), b"tampered\n")

    def test_include_live_without_repair_all_is_a_usage_error(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["doctor", "--include-live"], runtime=self.runtime, output_stream=out, interactive=False)
        self.assertEqual(code, 2)
        self.assertIn("--include-live is valid only with --repair-all", err.getvalue())


class GenerationRenameTests(V4Case):
    def assert_generation_rename(self, key, new_entry, *, old_entry=None, source=FIXTURE_ROOT):
        """Shared convergence seam for Sol and Sonnet release instances.

        Launch/repair/resume use real production services with fake exec/network
        seams, not the native client. Old agent [1m] bytes model 3.0.2.
        """
        root = self.root / f"rename-{key}"
        shutil.copytree(source, root)
        models_path = root / "catalog/models.json"
        models = json.loads(models_path.read_text())
        old = copy.deepcopy(old_entry or models["models"][key])
        models["models"][key] = old
        models_path.write_bytes(strict_json.pretty_file_bytes(models))
        self.runtime = self.make_runtime(asset_root=root)
        level = old["default_effort"]
        self.runtime.settings_store.update(
            lambda d: d.update(workflow_default_binding={"model": key, "effort": level}),
            catalog=self.runtime.lineup_catalog())
        document = profile.ad_hoc_direct(key)
        document["name"] = "rename-fixture"
        document["agents"] = {"cm-reviewer": {"model": key, "effort": level}}
        self.runtime.profiles.new(document)
        records = [
            self.launch_fresh(cli.LaunchTarget("profile", document, document["name"], follow, "Fixture"))
            for follow in (True, False)
        ]
        records.append(self.launch_fresh(self.direct_target(key)))
        before = {r["managed_id"]: self.tree(self.live(r["managed_id"])) for r in records}
        models["models"][key] = copy.deepcopy(new_entry)
        models_path.write_bytes(strict_json.pretty_file_bytes(models))
        retired_path = root / "catalog/retired.json"
        retired = json.loads(retired_path.read_text())
        rkey = f"{key}@{old['generation']}"
        retired["retired"][rkey] = {
            "successor": key, "generation": old["generation"], "reason": "Fixture generation move",
            "since_catalog": json.loads((root / "version.json").read_text())["catalog_version"],
            "provider": old["provider"], "last_wire": old["wire_model"], "display": old["display"],
            "context_tokens": old["context"]["provider_tokens"], "capabilities": old["capabilities"], "roles": old["roles"],
            "selectors": {selector: contract for _, selector, contract in catalog.line_selectors(old)},
        }
        retired_path.write_bytes(strict_json.pretty_file_bytes(retired))
        if isinstance(new_entry["efforts"], list):
            path = root / "catalog/providers.json"
            providers = json.loads(path.read_text())
            routes = providers["providers"][new_entry["provider"]]["passthrough_routes"]
            if not any(row["name"] == new_entry["wire_model"] for row in routes):
                routes.append({"name": new_entry["wire_model"], "fork": True})
            path.write_bytes(strict_json.pretty_file_bytes(providers))
        self.runtime = self.make_runtime(asset_root=root)
        for record in records:
            mid = record["managed_id"]
            expected = _expected(self, mid)
            self.assertIsNone(expected.plan)
            self.assertIn("applies at the next resume", expected.reason)
        out = io.StringIO()
        cli.main(["doctor", "--repair-all", "--include-live"], runtime=self.runtime,
                 output_stream=out, interactive=False)
        self.assertIn("applies at the next resume", out.getvalue())
        problems, _info, attention = cli._collect_doctor_reports(self.runtime)
        self.assertFalse(problems, problems)
        self.assertTrue(any("applies at the next resume" in line for line in attention), attention)
        index = self.runtime.catalog.retired_selector_index()
        for record in records:
            mid = record["managed_id"]
            self.assertEqual(self.tree(self.live(mid)), before[mid])
            live_settings = strict_json.loads(before[mid]["settings.json"])
            for binding in record["applied"]["agents"].values():
                selector = binding["selector"]
                self.assertTrue(selector.endswith("[1m]"))
                self.assertIn(selector, live_settings["availableModels"])
                self.assertEqual(index[selector.removesuffix("[1m]")][1]["last_wire"], old["wire_model"])
            # Next spawn still reads the old frontmatter; resume alone replaces it.
            resumed = self.resume(mid)
            lcat = self.runtime.lineup_catalog()
            provider = lcat.providers[new_entry["provider"]]
            self.assertEqual(resumed["applied"]["lead"]["selector"],
                             profile.binding_selector(new_entry, provider, level, lead=True)[0])
            settings_doc = strict_json.load(self.live(mid) / "settings.json")
            for binding in resumed["applied"]["agents"].values():
                self.assertEqual(binding["selector"], profile.binding_selector(new_entry, provider, level, lead=False)[0])
                self.assertIn(binding["selector"], settings_doc["availableModels"])
            self.assertEqual(settings_doc["env"]["CLAUDE_CODE_SUBAGENT_MODEL"],
                             profile.binding_selector(new_entry, provider, level, lead=False)[0])

    def test_live_repair_preserves_recorded_agent_selectors_until_resume(self) -> None:
        key = "sol"  # frozen fixture, not a shipped model assertion
        entry = copy.deepcopy(self.runtime.catalog.lines[key])
        entry.update(generation="99", wire_model="gpt-fixture-next")
        for level, spec in entry["efforts"].items():
            spec["selector"] = f"gpt-multi-fixture-next-{level}[1m]"
        self.assert_generation_rename(key, entry)

    def test_client_effort_rename_preserves_recorded_fence_until_resume(self) -> None:
        key = "opus"  # frozen fixture; C adds the new Sonnet generation
        entry = copy.deepcopy(self.runtime.catalog.lines[key])
        entry.update(generation="99", wire_model="claude-fixture-99", selector="claude-fixture-99[1m]")
        self.assert_generation_rename(key, entry)

    @uses_shipped_catalog
    def test_sol_generation_live_repair_and_resume(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        key = "sol"
        entry = bundle.lines[key]
        history = bundle.retired[f"{key}@6"]
        old = copy.deepcopy(entry)
        old.update(generation=history["generation"], wire_model=history["last_wire"],
                   display=history["display"], registry_overlay=None)
        old.pop("output", None)
        old["context"]["provider_tokens"] = history["context_tokens"]
        old["efforts"] = {level: {"selector": selector, "proxy_contract": contract}
                          for selector, contract in history["selectors"].items()
                          for level in entry["efforts"] if contract == f"reasoning-effort-{level}"}
        source = self.root / "sol-source"
        shutil.copytree(FIXTURE_ROOT, source)
        path = source / "catalog/providers.json"
        providers = json.loads(path.read_text())
        providers["providers"][entry["provider"]] = bundle.providers[entry["provider"]]
        path.write_bytes(strict_json.pretty_file_bytes(providers))
        self.assert_generation_rename(key, entry, old_entry=old, source=source)

    @uses_shipped_catalog
    def test_sonnet_generation_live_repair_and_resume(self) -> None:
        """The client-effort Sonnet instance. The old
        line is rebuilt from the shipped retired history, never a pinned id."""
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        key = "sonnet"
        entry = bundle.lines[key]
        successors = [rkey for rkey, row in bundle.retired.items()
                      if row.get("successor") == key and rkey.startswith(f"{key}@")]
        self.assertEqual(len(successors), 1, successors)
        history = bundle.retired[successors[0]]
        (selector, contract), = history["selectors"].items()
        self.assertIsNone(contract)
        old = copy.deepcopy(entry)
        old.update(generation=history["generation"], wire_model=history["last_wire"],
                   display=history["display"], selector=selector, registry_overlay=None)
        old.pop("output", None)
        old["context"]["provider_tokens"] = history["context_tokens"]
        source = self.root / "sonnet-source"
        shutil.copytree(FIXTURE_ROOT, source)
        self.assert_generation_rename(key, entry, old_entry=old, source=source)


if __name__ == "__main__":
    unittest.main()
