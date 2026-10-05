"""The one v4 launch/resume/relaunch path, end to end.

Every launch here goes through ``Runtime.prepare`` -> ``Runtime.perform`` ->
``launch.perform_launch`` with the injected ``execve`` seam (``_v4.V4Case``):
the marker, the shared migration hold, the ``.prev``/``.new`` scope swap,
the record save and the pointer are real. Replaces the 2.x
``PerformLaunchTests``/``DurablePerformLaunchTests``/``LifecycleCleanupTests``
(``_resume_failure_scope`` and ``precommitted`` are gone).
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import threading
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import cli, launch, lineup, profile, scope, sessions, settings, state, strict_json
from claude_multi.launch import LaunchError
from _catalog import FIXTURE_LAUNCHER_VERSION, FIXTURE_ROOT, GOLDENS_ROOT
from _golden import assertGolden
from _v4 import V4Case
import _v3
import claude_multi.compiler

FIXED = "11111111-1111-4111-8111-111111111111"
NOW = "2026-09-25T00:00:00Z"
V4_GOLDENS = GOLDENS_ROOT / "v4" / "records"


def _fixed_id(value: str = FIXED):
    return mock.patch.object(sessions.SessionStore, "new_id", lambda self: value)


def _fixed_clock():
    return mock.patch.object(sessions, "_now", lambda: NOW)


def fresh_record_golden_files() -> dict[str, bytes]:
    """The v4 record a fresh launch of fixture ``balanced`` / ``direct`` commits.

    Through the launch seam (``launch_callback``): fixed UUID, clock and
    cwd, default Settings; no mutation token (perform adds it).
    """

    import tempfile

    files: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="claude-multi-golden-") as tmp:
        base = Path(tmp)
        env = {
            "HOME": str(base / "home"),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_STATE_HOME": str(base / "state"),
        }
        captured: list[cli.PreparedLaunch] = []
        runtime = cli.Runtime(
            asset_root=FIXTURE_ROOT,
            environ=env,
            cwd="/project",
            launch_callback=lambda prepared: captured.append(prepared) or 0,
            health_get=lambda _b, _p: 200,
            # The golden records keep the fixture's launcher, not the release.
            release_version=FIXTURE_LAUNCHER_VERSION,
        )
        targets = {
            "fresh-balanced.json": cli.LaunchTarget(
                "profile", runtime.profiles.load("balanced"), "balanced", True, "Profile balanced"
            ),
            "fresh-direct.json": cli.LaunchTarget(
                "ad-hoc", profile.ad_hoc_direct("sol"), None, False, "Direct sol"
            ),
        }
        for name, target in targets.items():
            with _fixed_id(), _fixed_clock(), mock.patch.object(
                claude_multi.compiler, "git_work_tree", lambda _cwd: True
            ):
                prepared = runtime.prepare(target, action="fresh", passthrough=[])
            files[name] = strict_json.pretty_file_bytes(prepared.record)
    return files


def resume_follow_diff_golden() -> bytes:
    """The stderr diff of a following session after a local profile edit."""

    case = _GoldenCase()
    case.setUp()
    try:
        return case.follow_diff_stderr()
    finally:
        case.doCleanups()


class _GoldenCase(V4Case):
    def runTest(self) -> None:  # pragma: no cover - helper only
        pass

    def follow_diff_stderr(self) -> bytes:
        self.runtime.profiles.duplicate("balanced", "mine")
        with _fixed_id():
            record = self.launch_fresh(self.profile_target("mine"))
        mid = record["managed_id"]
        self.runtime.profiles.update(
            "mine",
            lambda doc: doc["agents"].update(
                {"cm-analyst": {"model": "opus55", "effort": "xhigh"}}
            ),
        )
        stderr = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(["-r", mid], runtime=self.runtime, output_stream=out, interactive=False)
        assert code == 0, out.getvalue() + stderr.getvalue()
        return stderr.getvalue().encode("utf-8")


class FreshCommitTests(V4Case):
    def test_fresh_commit_record_scope_pointer_prompt_and_marker(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.assertEqual(record["version"], 4)
        self.assertEqual(record["lineup_generation"], 1)
        self.assertEqual((record["profile"], record["follow"]), ("balanced", True))
        self.assertIsNone(record["last_event_source"])
        self.assertEqual(sessions.check_state_marker(self.store.root), 4)
        self.assertEqual(self.store.last(self.runtime.cwd), mid)
        live = self.live(mid)
        settings_doc = strict_json.loads((live / "settings.json").read_bytes())
        lead_set = (live / "lead-set.json").read_bytes()
        self.assertEqual(record["launch_fence"], sessions.launch_digest(settings_doc, lead_set))
        self.assertEqual(record["scope_lead"], sessions.lead_ref(record["applied"]["lead"]))
        self.assertEqual(
            (live / "lineup.gen").read_text().split()[0], str(record["lineup_generation"])
        )
        path, argv, _env = self.execs[-1]
        prompt = Path(argv[argv.index("--append-system-prompt-file") + 1])
        self.assertTrue(prompt.is_file())
        self.assertRegex(prompt.name, rf"^lead-prompt-[0-9a-f]{{16}}-{mid}\.md$")
        self.assertIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--name") + 1], "cm:balanced@project")

    def test_fresh_direct_is_ad_hoc_and_pinned(self) -> None:
        record = self.launch_fresh(self.direct_target("sol"))
        self.assertIsNone(record["profile"])
        self.assertFalse(record["follow"])
        self.assertEqual(record["applied"]["agents"], {})
        _path, argv, _env = self.execs[-1]
        self.assertEqual(argv[argv.index("--name") + 1], "cm:direct:sol@project")

    def test_primary_provider_is_recorded_and_converge_is_clean(self) -> None:
        from claude_multi import transition

        record = self.launch_fresh(self.profile_target("claude"))
        self.assertEqual(record["applied"].get("primary_provider"), "anthropic")
        report = transition.converge(
            self.store.root, self.store, record["managed_id"],
            runtime_parts=self.runtime.converge_parts(),
        )
        self.assertIn("live scope matches the record-authoritative compile", report)

    def test_fresh_goldens(self) -> None:
        for name, data in fresh_record_golden_files().items():
            with self.subTest(name=name):
                assertGolden(self, V4_GOLDENS / name, data)


class ReleaseIdentityTests(V4Case):
    """A resource override selects the catalog inputs, never the release: the
    runtime, the records it writes, its evidence and its exports name the
    packaged launcher (the one ``--version`` reports)."""

    def test_a_resource_override_keeps_the_packaged_launcher(self) -> None:
        import claude_multi
        from claude_multi import upgrade
        from claude_multi.cli.commands import portability as portability_command

        packaged = claude_multi.__version__
        selected = strict_json.loads((FIXTURE_ROOT / "version.json").read_bytes())
        # The selected resources (the frozen fixture) carry another identity.
        self.assertEqual(self.runtime.asset_root, FIXTURE_ROOT)
        self.assertNotEqual(selected["launcher_version"], packaged)
        self.assertEqual(upgrade.running_release().launcher_version, packaged)
        self.assertEqual(self.runtime.launcher_version, packaged)
        self.assertEqual(self.runtime.evidence_versions()["launcher"], packaged)
        # Catalog provenance follows the selected resources.
        self.assertEqual(self.runtime.catalog_version, selected["catalog_version"])
        record = self.launch_fresh()
        self.assertEqual(record["launcher_version"], packaged)
        self.assertEqual(record["catalog_version"], selected["catalog_version"])
        self.assertEqual(record["catalog_hash"], self.runtime.catalog.bundle_sha256)
        exported = portability_command.collect_export(self.runtime)
        self.assertEqual(exported["exported_by"],
                         {"launcher_version": packaged, "catalog_version": selected["catalog_version"]})

    def test_the_release_seam_is_explicit(self) -> None:
        runtime = self.make_runtime(release_version=FIXTURE_LAUNCHER_VERSION)
        self.assertEqual(runtime.launcher_version, FIXTURE_LAUNCHER_VERSION)
        self.assertEqual(runtime.evidence_versions()["launcher"], FIXTURE_LAUNCHER_VERSION)

    def test_fresh_failure_forgets_record_and_keeps_a_pre_existing_prev(self) -> None:
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        mid = sessions.managed_id(prepared.record)
        prev = self.prev(mid)
        state.ensure_private_dir(prev)
        state.atomic_write(prev / "marker", b"keep")

        def failing(*_args):
            raise OSError(8, "exec format error")

        self.runtime.execve = failing
        with self.assertRaises(OSError):
            self.runtime.perform(prepared)
        self.assertFalse(self.store.exists(mid))
        self.assertFalse(self.live(mid).exists())
        self.assertEqual((prev / "marker").read_bytes(), b"keep")
        self.assertIsNone(self.store.last(self.runtime.cwd))


class ResumeGuardTests(V4Case):
    def setUp(self) -> None:
        super().setUp()
        self.record = self.launch_fresh()
        self.mid = self.record["managed_id"]

    def _snapshot(self):
        return (
            self.record_bytes(self.mid),
            self.tree(self.live(self.mid)),
            self.store.read_pointer_bytes(self.runtime.cwd),
        )

    def test_each_cas_mismatch_refuses_and_writes_nothing(self) -> None:
        cases = {
            "epoch": ("expected_launch_epoch", 99, "authority changed after preparation"),
            "token": ("expected_mutation_token", sessions.new_mutation_token(), "authority changed"),
            "hash": ("expected_applied_hash", "sha256:" + "0" * 64, "lineup changed after preparation"),
            "generation": ("expected_lineup_generation", 7, "lineup changed after preparation"),
        }
        for label, (field, value, text) in cases.items():
            with self.subTest(label):
                prepared = self.prepare_resume(self.mid)
                before = self._snapshot()
                import dataclasses

                with self.assertRaisesRegex(LaunchError, text):
                    self.runtime.perform(dataclasses.replace(prepared, **{field: value}))
                self.assertEqual(self._snapshot(), before)

    def test_current_v3_record_is_refused(self) -> None:
        prepared = self.prepare_resume(self.mid)
        raw = strict_json.loads(self.record_bytes(self.mid))
        # A restore-2x ran meanwhile: the on-disk record is a 2.x record.
        state.atomic_write(
            self.store._record_path(self.mid),
            strict_json.canonical_file_bytes(_v3_backup(self, raw)),
        )
        with self.assertRaisesRegex(LaunchError, "was changed to a legacy record concurrently"):
            self.runtime.perform(prepared)

    def test_pre_exec_failure_after_swap_restores_prev_record_and_pointer(self) -> None:
        prepared = self.prepare_resume(self.mid)
        before = self._snapshot()
        with mock.patch.object(
            sessions.SessionStore, "update_last", side_effect=OSError(28, "no space")
        ):
            with self.assertRaises(OSError):
                self.runtime.perform(prepared)
        self.assertEqual(self._snapshot(), before)

    def test_exec_oserror_restores_prev_record_and_pointer(self) -> None:
        prepared = self.prepare_resume(self.mid)
        before = self._snapshot()

        def failing(*_args):
            raise OSError(8, "exec format error")

        self.runtime.execve = failing
        with self.assertRaises(OSError):
            self.runtime.perform(prepared)
        self.assertEqual(self._snapshot(), before)

    def test_exec_failure_after_a_concurrent_hook_overlays_the_lifecycle(self) -> None:
        prepared = self.prepare_resume(self.mid)
        prior = strict_json.loads(self.record_bytes(self.mid))
        live_before = self.tree(self.live(self.mid))

        def hook_then_fail(*_args):
            current = strict_json.loads(self.record_bytes(self.mid))
            current["last_event_source"] = "startup"
            current["last_seen_at"] = "2026-09-26T00:00:00Z"
            state.atomic_write(
                self.store._record_path(self.mid), strict_json.canonical_file_bytes(current)
            )
            raise OSError(8, "exec format error")

        self.runtime.execve = hook_then_fail
        with self.assertRaises(OSError):
            self.runtime.perform(prepared)
        restored = self.store.load(self.mid)
        self.assertEqual(restored["last_event_source"], "startup")
        self.assertEqual(restored["launch_epoch"], prior["launch_epoch"])
        self.assertEqual(restored["applied_hash"], prior["applied_hash"])
        self.assertEqual(self.tree(self.live(self.mid)), live_before)

    def test_swap_rename_failure_is_undone(self) -> None:
        prepared = self.prepare_resume(self.mid)
        before = self._snapshot()
        real_rename = os.rename

        def rename(src, dst):
            if str(src).endswith(".new"):
                raise OSError(5, "io error")
            return real_rename(src, dst)

        with mock.patch.object(scope.os, "rename", side_effect=rename):
            with self.assertRaisesRegex(scope.ScopeError, "the prior scope was restored"):
                self.runtime.perform(prepared)
        self.assertEqual(self._snapshot(), before)

    def test_cas_rejects_a_resume_prepared_before_a_live_apply(self) -> None:
        prepared = self.prepare_resume(self.mid)
        record = self.store.load(self.mid)
        applied = copy.deepcopy(record["applied"])
        applied["agents"].pop("cm-reviewer-strong")
        updated = sessions.with_applied(record, applied)
        updated["lineup_generation"] = record["lineup_generation"] + 1
        updated["mutation_token"] = sessions.new_mutation_token()
        self.store.save(updated)
        with self.assertRaisesRegex(LaunchError, "authority changed after preparation"):
            self.runtime.perform(prepared)

    def test_resume_commit_marks_launched_without_hook(self) -> None:
        self.mutate(self.mid, last_event_source="end")
        record = self.resume(self.mid)
        self.assertIsNone(record["last_event_source"])

    def test_title_set_between_prepare_and_perform_survives(self) -> None:
        prepared = self.prepare_resume(self.mid)
        self.store.set_title(self.mid, "renamed meanwhile")
        self.runtime.perform(prepared)
        self.assertEqual(self.store.load(self.mid)["title"], "renamed meanwhile")

    def test_disk_generation_moved_after_prepare_refuses(self) -> None:
        prepared = self.prepare_resume(self.mid)
        before_record = self.record_bytes(self.mid)
        # A live apply crashed after publishing lineup.gen G+1.
        state.atomic_write(self.live(self.mid) / "lineup.gen", b"2 000000000000\n")
        with self.assertRaisesRegex(LaunchError, r"generation on disk changed after preparation \(1 → 2\)"):
            self.runtime.perform(prepared)
        self.assertEqual(self.record_bytes(self.mid), before_record)

    def test_no_change_resume_keeps_the_generation(self) -> None:
        record = self.resume(self.mid)  # following, unchanged profile
        self.assertEqual(record["lineup_generation"], 1)
        self.mutate(self.mid, follow=False)
        record = self.resume(self.mid)  # pinned
        self.assertEqual(record["lineup_generation"], 1)
        self.assertEqual(record["launch_epoch"], 3)

    def test_resume_prepared_after_a_crashed_publish_plans_past_it(self) -> None:
        state.atomic_write(self.live(self.mid) / "lineup.gen", b"2 000000000000\n")
        record = self.resume(self.mid)
        self.assertEqual(record["lineup_generation"], 3)
        self.assertEqual((self.live(self.mid) / "lineup.gen").read_text().split()[0], "3")

    def test_migration_lock_held_refuses_with_l4(self) -> None:
        prepared = self.prepare_resume(self.mid)
        before = self._snapshot()
        lock = sessions.migration_lock(self.store.root)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        with self.assertRaisesRegex(LaunchError, "migration or restore in progress; retry in a moment \\(nothing was launched\\)"):
            self.runtime.perform(prepared)
        self.assertEqual(self._snapshot(), before)

    def test_pinned_lead_target_launches_the_requested_lead(self) -> None:
        self.mutate(
            self.mid,
            follow=False,
            lead_target={"key": "opus5", "effort": "ultracode", "selector": "x"},
        )
        prepared = self.prepare_resume(self.mid)
        self.assertTrue(any("applying the requested lead" in n for n in prepared.notices))
        self.runtime.perform(prepared)
        record = self.store.load(self.mid)
        self.assertEqual(record["applied"]["lead"]["key"], "opus5")
        self.assertNotIn("lead_target", record)


def _v3_backup(case: V4Case, raw: dict) -> dict:
    """A plausible v3 managed backup for ``restore_overlay`` in a guard test."""

    runtime = case.runtime
    return _v3.make_record(
        managed_id=raw["managed_id"],
        cwd=raw["cwd"],
        composition_name="default",
        snapshot=_v3.snapshot_for(runtime.catalog.docs),
        catalog_version=runtime.catalog_version,
        catalog_hash=runtime.catalog.bundle_sha256,
        launcher_version=runtime.launcher_version,
        mode="durable",
        scope_generation=1,
        now=NOW,
    )


class Gen0AndMigrationTests(V4Case):
    def _v3_record(self, **kwargs) -> dict:
        record = _v3.make_record(
            session_id=FIXED,
            cwd=self.runtime.cwd,
            composition_name="default",
            snapshot=_v3.snapshot_for(self.runtime.catalog.docs),
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
            mode="durable",
            scope_generation=1,
            now=NOW,
            **kwargs,
        )
        _v3.save_any(self.store, record)
        self.store.update_last(self.runtime.cwd, FIXED)
        self.write_transcript(FIXED)
        return record

    def test_resume_of_a_v3_record_migrates_then_launches(self) -> None:
        self._v3_record()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(["-r", FIXED], runtime=self.runtime, output_stream=io.StringIO(), interactive=False)
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertIn("migrated legacy record:", stderr.getvalue())
        self.assertTrue(self.store.backup_path(FIXED).is_file())
        self.assertEqual(sessions.check_state_marker(self.store.root), 4)
        record = self.store.load(FIXED)
        self.assertEqual(record["version"], 4)
        self.assertGreaterEqual(record["lineup_generation"], 1)
        self.assertEqual(record["migration"]["from_version"], 3)

    def test_gen0_resume_exec_failure_puts_the_2x_scope_back(self) -> None:
        self._v3_record()
        from claude_multi import migrate

        migrate.migrate_one(
            self.store, self.runtime.lineup_catalog(), FIXED,
            hook_command=self.runtime.resolved_hook_command,
            hook_shim_path=scope.hook_shim_path(self.store.root),
        )
        self.assertEqual(self.store.load(FIXED)["lineup_generation"], 0)
        legacy = self.live(FIXED)
        state.ensure_private_dir(legacy / ".claude" / "agents")
        state.atomic_write(legacy / "settings.json", b"{\"2.x\": true}\n")
        state.atomic_write(legacy / ".claude" / "agents" / "cm-old.md", b"old\n")
        before = self.tree(legacy)
        record_before = self.record_bytes(FIXED)

        def failing(*_args):
            raise OSError(8, "exec format error")

        self.runtime.execve = failing
        with self.assertRaises(OSError):
            self.resume(FIXED)
        self.assertEqual(self.tree(legacy), before)
        self.assertEqual(self.record_bytes(FIXED), record_before)

    def test_bounded_wait_refuses_a_fresh_launch_while_migrate_holds_the_lock(self) -> None:
        ticks = iter(range(0, 100))
        original = sessions.acquire_exclusive_bounded

        def bounded(root, **_kwargs):
            return original(root, clock=lambda: float(next(ticks)), sleep=lambda _s: None)

        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        lock = sessions.migration_lock(self.store.root)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        with mock.patch.object(sessions, "acquire_exclusive_bounded", bounded):
            with self.assertRaisesRegex(LaunchError, "migration or restore in progress"):
                self.runtime.perform(prepared)
        self.assertFalse(self.store.exists(sessions.managed_id(prepared.record)))
        self.assertEqual(sessions.check_state_marker(self.store.root), 3)

    def test_bounded_wait_refuses_a_resume_that_needs_launch_time_migration(self) -> None:
        self._v3_record()
        before = self.record_bytes(FIXED)
        ticks = iter(range(0, 100))
        original = sessions.acquire_exclusive_bounded

        def bounded(root, **_kwargs):
            return original(root, clock=lambda: float(next(ticks)), sleep=lambda _s: None)

        lock = sessions.migration_lock(self.store.root)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        with mock.patch.object(sessions, "acquire_exclusive_bounded", bounded):
            with self.assertRaises(sessions.MigrationBusyError):
                self.prepare_resume(FIXED)
        self.assertEqual(self.record_bytes(FIXED), before)

    def test_ensure_state_v4_runs_before_the_shared_hold_with_real_locks(self) -> None:
        done = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            try:
                self.launch_fresh()
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(done.wait(10), "fresh launch deadlocked on its own locks")
        self.assertEqual(errors, [])
        self.assertEqual(sessions.check_state_marker(self.store.root), 4)


class SourceDocumentTests(V4Case):
    def test_follow_diff_is_shown_once(self) -> None:
        stderr = _GoldenCase.follow_diff_stderr(self).decode("utf-8")
        self.assertIn("follows profile mine; applying its changes at this resume:", stderr)
        self.assertIn("analyst", stderr)
        mid = self.store.last(self.runtime.cwd)
        again = io.StringIO()
        with contextlib.redirect_stderr(again):
            self.assertEqual(
                cli.main(["-r", mid], runtime=self.runtime, output_stream=io.StringIO(), interactive=False), 0
            )
        self.assertNotIn("applying its changes", again.getvalue())

    def test_follow_diff_golden(self) -> None:
        assertGolden(self, V4_GOLDENS / "resume-follow-diff.txt", resume_follow_diff_golden())

    def test_deleted_followed_profile_pins_with_a_notice(self) -> None:
        self.runtime.profiles.duplicate("balanced", "mine")
        record = self.launch_fresh(self.profile_target("mine"))
        self.runtime.profiles.delete("mine")
        prepared = self.prepare_resume(record["managed_id"])
        self.assertIn(
            f"profile 'mine' no longer exists; session {record['managed_id'][:8]} is now "
            "pinned to its applied lineup",
            prepared.notices,
        )
        self.runtime.perform(prepared)
        self.assertFalse(self.store.load(record["managed_id"])["follow"])

    def test_pending_change_is_consumed_and_cleared(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.mutate(
            mid,
            pending={
                "requested_at": NOW, "kind": "profile", "profile": "quality", "follow": True,
                "document": None, "reasons": ["lead class large → large"],
            },
            mutation_token=sessions.new_mutation_token(),
        )
        record = self.resume(mid)
        self.assertEqual(record["profile"], "quality")
        self.assertNotIn("pending", record)

    def test_stale_pending_profile_and_document_are_dropped(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        broken = profile.ad_hoc_direct("sol")
        broken["lead"] = {"model": "muse-spark", "effort": "high"}
        for pending in (
            {"kind": "profile", "profile": "gone", "follow": True, "document": None},
            {"kind": "lineup", "profile": None, "follow": False, "document": broken},
        ):
            with self.subTest(kind=pending["kind"]):
                self.mutate(
                    mid,
                    pending={"requested_at": NOW, "reasons": ["requested"], **pending},
                    mutation_token=sessions.new_mutation_token(),
                )
                prepared = self.prepare_resume(mid)
                self.assertTrue(
                    any(n.startswith("pending relaunch change dropped:") for n in prepared.notices),
                    prepared.notices,
                )
                self.runtime.perform(prepared)
                after = self.store.load(mid)
                self.assertNotIn("pending", after)
                self.assertEqual(after["profile"], "balanced")

    def test_direct_resume_with_model_is_a_lead_override(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        out = io.StringIO()
        code = cli.main(["direct", "-r", mid, "--model", "opus5"], runtime=self.runtime, output_stream=out, interactive=False)
        self.assertEqual(code, 0, out.getvalue())
        record = self.store.load(mid)
        self.assertEqual(record["applied"]["lead"]["key"], "opus5")
        self.assertIsNone(record["profile"])
        self.assertFalse(record["follow"])

    def test_resume_snapshots_current_settings(self) -> None:
        record = self.launch_fresh()
        settings.SettingsStore(self.runtime.environ).save(
            {"version": 1, "compaction_percent": 80}, catalog=self.runtime.lineup_catalog()
        )
        record = self.resume(record["managed_id"])
        self.assertEqual(record["applied"]["settings"]["compaction_percent"], 80)

    def test_corrupt_settings_refuse_the_launch(self) -> None:
        path = settings.settings_path(self.runtime.environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, b"{not json")
        with self.assertRaisesRegex(cli.CLIError, "cannot read operator settings"):
            self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])


class NeedsChoiceTests(V4Case):
    def _removed_lead(self) -> str:
        record = self.launch_fresh(self.direct_target("sol"))
        mid = record["managed_id"]
        applied = copy.deepcopy(record["applied"])
        applied["lead"]["key"] = "gone-model"
        self.mutate(mid, applied=applied, mutation_token=sessions.new_mutation_token())
        return mid

    def test_unknown_lead_key_raises_needs_choice(self) -> None:
        mid = self._removed_lead()
        with self.assertRaises(cli.NeedsChoiceError) as caught:
            self.prepare_resume(mid)
        self.assertIn("unknown to catalog", str(caught.exception))

    def test_noninteractive_resume_prints_n1(self) -> None:
        mid = self._removed_lead()
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            code = cli.main(["-r", mid], runtime=self.runtime, output_stream=io.StringIO(), interactive=False)
        self.assertEqual(code, 1)
        rid = self.store.load(mid)["runtime_session_id"]
        self.assertIn(
            f"session {mid} needs a profile or model choice (lead gone-model was removed:",
            out.getvalue(),
        )
        self.assertIn(
            f"claude-multi lineup --session {rid} --relaunch --its-exited profile <name> | direct <model>",
            out.getvalue(),
        )

    def test_interactive_chooser_relaunches_with_the_chosen_profile(self) -> None:
        mid = self._removed_lead()
        out = io.StringIO()
        code = cli.main(
            ["-r", mid], runtime=self.runtime, input_stream=io.StringIO("balanced\n\n"),
            output_stream=out, interactive=True,
        )
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("needs a lead choice", out.getvalue())
        record = self.store.load(mid)
        self.assertEqual(record["profile"], "balanced")
        self.assertEqual(record["applied"]["lead"]["key"], "opus55")

    def test_retired_lead_without_successor_needs_a_choice(self) -> None:
        record = self.launch_fresh(self.direct_target("sol"))
        mid = record["managed_id"]
        applied = copy.deepcopy(record["applied"])
        applied["lead"]["key"] = "muse-spark"
        self.mutate(mid, applied=applied, mutation_token=sessions.new_mutation_token())
        with self.assertRaises(cli.NeedsChoiceError):
            self.prepare_resume(mid)


class CredentialTests(V4Case):
    def _kimi_profile(self) -> cli.LaunchTarget:
        document = self.runtime.profiles.load("balanced")
        document["agents"]["cm-analyst"] = {"model": "kimi-k3", "effort": "max"}
        document["name"] = "kimiish"
        return cli.LaunchTarget("profile-file", profile.parse(document), None, False, "Profile file x")

    def _drop_secrets(self) -> None:
        state.atomic_write(self.root / "secrets" / "claude.env", b"")

    def test_fresh_noninteractive_refuses_with_l1(self) -> None:
        self._drop_secrets()
        prepared = self.runtime.prepare(self._kimi_profile(), action="fresh", passthrough=[])
        self.assertTrue(prepared.secret_problems)
        with self.assertRaisesRegex(cli.LaunchPlanError, r"profile is blocked:\n- provider .*\(kimi\)"):
            self.runtime.perform(prepared)
        self.assertEqual(self.execs, [])

    def test_resume_and_relaunch_refuse_with_l1(self) -> None:
        record = self.launch_fresh(self._kimi_profile())
        mid = record["managed_id"]
        self._drop_secrets()
        with self.assertRaises(cli.LaunchPlanError):
            self.runtime.perform(self.prepare_resume(mid))
        relaunch = cli.LaunchTarget(
            "relaunch", self._kimi_profile().document, None, False, "Relaunch"
        )
        with self.assertRaises(cli.LaunchPlanError):
            self.runtime.perform(self.prepare_resume(mid, target=relaunch))

    def test_line_confirm_prints_blocked_and_launches_nothing(self) -> None:
        self._drop_secrets()
        prepared = self.runtime.prepare(self._kimi_profile(), action="fresh", passthrough=[])
        out = io.StringIO()
        self.assertFalse(cli._confirm_launch_line(prepared, io.StringIO("\n"), out))
        self.assertIn("blocked: provider", out.getvalue())
        self.assertIn("q quit: ", out.getvalue())


class DoctorOnV4StateTests(V4Case):
    def test_doctor_on_v4_state_without_a_default_composition(self) -> None:
        self.launch_fresh()
        out = io.StringIO()
        code = cli.main(["doctor", "-v"], runtime=self.runtime, output_stream=out, interactive=False)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("Collisions: none (0 project agents).", out.getvalue())
        self.assertFalse(
            (sessions.config_root(self.runtime.environ) / "compositions" / "default.json").exists()
        )

    def test_collisions_come_from_the_balanced_seed(self) -> None:
        agents = self.project / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "x.md").write_text("---\nname: cm-reviewer-strong\n---\n")
        line, problems = cli._doctor_collision_report(self.runtime)
        self.assertIn("1 blocking", line)
        self.assertTrue(problems)

    def test_v3_ordinary_record_with_a_removed_key_is_catalog_free(self) -> None:
        record = _v3.make_ordinary_record(
            managed_id=FIXED,
            runtime_session_id=FIXED,
            cwd=self.runtime.cwd,
            model="glm52",
            context_profile=None,
            catalog_version=32,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.26.0",
            now=NOW,
        )
        _v3.save_any(self.store, record)
        info, _problems, _attention = cli._doctor_scope_report(self.runtime)
        self.assertTrue(
            any("legacy record · lead glm52 (not checked until migrated)" in line for line in info),
            info,
        )


class PerformPathTests(V4Case):
    """The 2.x perform guarantees, re-proved on the v4 path."""

    def test_exact_argv_env_and_credential_unsets(self) -> None:
        inherited = {
            "PATH": "/usr/bin",
            "CLAUDE_CODE_SUBAGENT_MODEL": "x",
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "999999",
            **{key: "inherited-must-be-unset" for key in launch_catalog().CREDENTIAL_ENV_KEYS},
        }
        self.runtime.environ.update(inherited)
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=["--verbose"])
        self.runtime.perform(prepared)
        path, argv, env = self.execs[-1]
        self.assertEqual(argv[0], path)
        self.assertEqual(argv[1:], prepared.result.argv)
        self.assertEqual(argv[-1], "--verbose")
        for key in launch_catalog().CREDENTIAL_ENV_KEYS:
            self.assertNotIn(key, env)
        for key in ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"):
            self.assertNotIn(key, env)
        self.assertEqual(env["PATH"], "/usr/bin")
        for key, value in prepared.result.env_set.items():
            self.assertEqual(env[key], value)

    def test_readiness_failure_aborts_before_any_state_write(self) -> None:
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        self.runtime.health_get = lambda _b, _p: 503
        with self.assertRaisesRegex(LaunchError, "status 503"):
            self.runtime.perform(prepared)
        self.assertFalse(self.store.exists(sessions.managed_id(prepared.record)))
        self.assertFalse(prepared.result.lead_prompt_path.exists())
        self.assertEqual(self.execs, [])

    def test_binary_failure_aborts_before_readiness_and_state(self) -> None:
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        contract = self.runtime.catalog.docs["native-contract"]
        contract["verified"][0]["platforms"][self.platform]["sha256"] = "0" * 64
        self.runtime.health_get = lambda _b, _p: self.fail("readiness must not run")
        with self.assertRaisesRegex(LaunchError, "does not match the verified sha256"):
            self.runtime.perform(prepared)
        self.assertFalse(self.store.exists(sessions.managed_id(prepared.record)))
        self.assertIsNone(self.store.last(self.runtime.cwd))

    def test_signal_identity_passes_through(self) -> None:
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])

        def raising(*_args):
            raise SystemExit(42)

        self.runtime.execve = raising
        with self.assertRaises(SystemExit) as raised:
            self.runtime.perform(prepared)
        self.assertEqual(raised.exception.code, 42)

    def test_collision_gate_fails_before_any_state_write(self) -> None:
        agents = self.project / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "mine.md").write_text("---\nname: cm-reviewer\n---\n")
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        with self.assertRaisesRegex(LaunchError, "exact cm-\\* agent name collision"):
            self.runtime.perform(prepared)
        self.assertFalse(self.store.exists(sessions.managed_id(prepared.record)))

    def test_hostile_collision_path_is_sanitized_in_the_main_error(self) -> None:
        # Ported from test_cli TerminalInjectionTests (the 2.x card's BLOCKED
        # row): the launch-time collision refusal escapes control bytes.
        agents = self.project / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "cm-shadow\x1b[2J\x07.md").write_text("---\nname: cm-reviewer\n---\n")
        output, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(
                ["--profile", "balanced"],
                runtime=self.runtime,
                input_stream=None,
                output_stream=output,
                interactive=False,
            )
        text = err.getvalue()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(code, 1, text)
        self.assertIn("exact cm-* agent name collision", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertEqual(self.execs, [])

    def test_resume_enters_the_record_cwd(self) -> None:
        # Ported from test_cli ResumeChdirTests: the exec runs in the
        # recorded project dir, whatever the invoking process's cwd.
        original = os.getcwd()
        self.addCleanup(os.chdir, original)
        record = self.launch_fresh()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.chdir(elsewhere)
        seen: list[str] = []

        def capturing(path, argv, env):
            seen.append(os.getcwd())
            return 0

        self.runtime.execve = capturing
        self.resume(record["managed_id"])
        self.assertEqual(seen, [record["cwd"]])

    def test_missing_original_cwd_fails_before_the_commit(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        before = self.record_bytes(mid)
        prepared = self.prepare_resume(mid)
        moved = self.root / "moved"
        os.rename(self.project, moved)
        self.addCleanup(lambda: moved.exists() and os.rename(moved, self.project))
        with self.assertRaisesRegex(LaunchError, "original project directory"):
            launch.perform_launch(
                prepared.result, record=prepared.record, store=self.store,
                native_contract=self.runtime.catalog.docs["native-contract"],
                gateway=self.runtime.catalog.docs["gateway"], execve=self._execve,
                environ=self.runtime.environ, home=self.runtime.home, health_get=self._health,
                expected_launch_epoch=prepared.expected_launch_epoch,
                expected_mutation_token=prepared.expected_mutation_token,
            )
        self.assertEqual(self.record_bytes(mid), before)

    def test_guards_wait_for_the_lifecycle_lock(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        prepared = self.prepare_resume(mid)
        lock = self.store.lifecycle_lock(mid)
        lock.acquire(blocking=True)
        finished = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            try:
                self.runtime.perform(prepared)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertFalse(finished.wait(0.3), "perform must wait for the lifecycle lock")
        lock.release()
        self.assertTrue(finished.wait(10))
        self.assertEqual(errors, [])
        self.assertEqual(self.store.load(mid)["launch_epoch"], 2)

    def test_pending_fork_arriving_after_prepare_blocks_the_launch(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        prepared = self.prepare_resume(mid)
        fork = "33333333-3333-4333-8333-333333333333"
        self.mutate(
            mid,
            pending_forks=[{"session_id": fork, "observed_at": NOW}],
            identity_state=sessions.IDENTITY_PENDING_FORK,
        )
        with self.assertRaisesRegex(LaunchError, fork):
            self.runtime.perform(prepared)

    def test_repair_needed_cwd_refuses_with_the_relink_command(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.mutate(mid, identity_state=sessions.IDENTITY_REPAIR_NEEDED, observed_cwd="/elsewhere")
        with self.assertRaisesRegex(LaunchError, "relink-runtime"):
            self.prepare_resume(mid)

    def test_model_only_repair_relaunches_and_clears_the_evidence(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.mutate(mid, identity_state=sessions.IDENTITY_REPAIR_NEEDED, observed_model="other[1m]")
        prepared = self.prepare_resume(mid)
        self.assertTrue(prepared.model_relaunch)
        self.runtime.perform(prepared)
        after = self.store.load(mid)
        self.assertNotIn("observed_model", after)
        self.assertEqual(after["identity_state"], sessions.IDENTITY_UNVERIFIED)

    def test_runtime_change_after_prepare_aborts(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        prepared = self.prepare_resume(mid)
        other = "44444444-4444-4444-8444-444444444444"
        self.store.relink_runtime(mid, observed_runtime_id=other)
        with self.assertRaises(LaunchError):
            self.runtime.perform(prepared)
        self.assertEqual(self.store.load(mid)["runtime_session_id"], other)


def launch_catalog():
    from claude_multi import catalog

    return catalog


class SessionEnvKeepLaunchTests(V4Case):
    """A managed launch and its resume keep the user's chosen API-key
    variable (names only) and remove every other ``*_API_KEY``."""

    def test_launch_and_resume_keep_the_chosen_variable_by_name_only(self) -> None:
        from claude_multi import choices

        environ = {**self.env, "DOCS_TOOL_API_KEY": "mcp-value-7", "OTHER_TOOL_API_KEY": "other-value-8"}
        choices.update(environ, session_env_keep=["DOCS_TOOL_API_KEY"])
        self.runtime = self.make_runtime(environ=environ)
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.resume(mid)
        self.assertEqual(len(self.execs), 2)
        for _path, _argv, env in self.execs:
            self.assertEqual(env.get("DOCS_TOOL_API_KEY"), "mcp-value-7")
            self.assertNotIn("OTHER_TOOL_API_KEY", env)
        # The value never enters metadata: the record, the scope, the prompt.
        files = {**self.tree(self.live(mid)), "record": self.record_bytes(mid) or b""}
        for name, data in files.items():
            with self.subTest(file=name):
                self.assertNotIn(b"mcp-value-7", data)
        self.assertNotIn("DOCS_TOOL_API_KEY", json.dumps(self.store.load(mid)))

    def test_a_name_refused_at_launch_is_removed_and_named(self) -> None:
        from claude_multi import choices

        environ = {**self.env, "KIMI_CLAUDE_API_KEY": "provider-value-9"}
        # Written as a hand edit: the save path refuses provider names.
        path = choices.path(environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, b'{"version": 1, "session_env_keep": ["KIMI_CLAUDE_API_KEY"]}\n')
        self.runtime = self.make_runtime(environ=environ)
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        self.assertIn("KIMI_CLAUDE_API_KEY", prepared.result.env_unset)
        self.assertTrue(any("KIMI_CLAUDE_API_KEY is removed from this session" in line
                            for line in prepared.notices), prepared.notices)
        self.assertNotIn("provider-value-9", "\n".join(prepared.notices))
        # A launch and its resume both run without the provider key.
        record = self.launch_fresh()
        self.resume(record["managed_id"])
        self.assertEqual(len(self.execs), 2)
        for _path, _argv, env in self.execs:
            self.assertNotIn("KIMI_CLAUDE_API_KEY", env)

    # -- a prepared plan never carries a stale kept name, and
    # a key-shaped entry is never shown.

    def _keep_docs_tool(self) -> None:
        from claude_multi import choices

        environ = {**self.env, "DOCS_TOOL_API_KEY": "mcp-value-7"}
        choices.update(environ, session_env_keep=["DOCS_TOOL_API_KEY"])
        self.runtime = self.make_runtime(environ=environ)

    def _declare_docs_tool(self) -> None:
        """A providers.d declaration naming the kept variable: unadmitted and
        schema-invalid, so only its syntactic secret_ref counts."""

        from claude_multi import operator as operator_mod

        directory = state.ensure_private_dir(operator_mod.providers_dir(self.runtime.gateway_environ()))
        state.atomic_write(directory / "docs.json", strict_json.pretty_file_bytes(
            {"provider": {"auth": {"kind": "bearer", "secret_ref": "env:DOCS_TOOL_API_KEY"}}}))

    def _assert_refused(self, prepared, before: int) -> None:
        with self.assertRaises(LaunchError) as raised:
            self.runtime.perform(prepared)
        self.assertIn("DOCS_TOOL_API_KEY would reach the session", str(raised.exception))
        self.assertIn("nothing launched", str(raised.exception))
        self.assertNotIn("mcp-value-7", str(raised.exception))
        self.assertEqual(len(self.execs), before)

    def test_a_provider_declared_after_prepare_refuses_the_fresh_launch(self) -> None:
        self._keep_docs_tool()
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        self.assertNotIn("DOCS_TOOL_API_KEY", prepared.result.env_unset)
        self._declare_docs_tool()
        self._assert_refused(prepared, 0)
        # Prepared again, the declaration's name is removed and the launch runs.
        record = self.launch_fresh()
        self.assertEqual(len(self.execs), 1)
        self.assertNotIn("DOCS_TOOL_API_KEY", self.execs[0][2])
        self.assertEqual(record["version"], 4)

    def test_a_provider_declared_after_prepare_refuses_the_resume(self) -> None:
        self._keep_docs_tool()
        record = self.launch_fresh()
        self.assertEqual(self.execs[0][2].get("DOCS_TOOL_API_KEY"), "mcp-value-7")
        prepared = self.prepare_resume(record["managed_id"])
        self._declare_docs_tool()
        self._assert_refused(prepared, 1)

    def test_the_commit_barrier_checks_the_kept_names_again(self) -> None:
        self._keep_docs_tool()
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        real = state.acquire_served_barrier

        def declared_meanwhile(*args, **kwargs):
            # After perform's first check, before the barrier is held.
            self._declare_docs_tool()
            return real(*args, **kwargs)

        with mock.patch.object(state, "acquire_served_barrier", side_effect=declared_meanwhile):
            self._assert_refused(prepared, 0)
        self.assertEqual(list(self.store.scan_uuid_records()), [])

    def test_a_name_dropped_from_the_choice_after_prepare_refuses(self) -> None:
        from claude_multi import choices

        self._keep_docs_tool()
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        choices.update(self.runtime.environ, session_env_keep=[])
        self._assert_refused(prepared, 0)

    def test_a_key_shaped_entry_is_never_shown(self) -> None:
        from claude_multi import choices, secret_store, views
        from claude_multi.cli import doctor
        import claude_multi.cli.screens.launch_sessions as launch_sessions

        pasted = "ABCD1234EFGH5678IJKL9012MNOP"
        environ = {**self.env}
        path = choices.path(environ)
        state.ensure_private_dir(path.parent)
        # A hand edit: the save path refuses key-shaped text.
        state.atomic_write(path, b'{"version": 1, "session_env_keep": ["' + pasted.encode() + b'"]}\n')
        self.runtime = self.make_runtime(environ=environ)
        prepared = self.runtime.prepare(self.profile_target(), action="fresh", passthrough=[])
        notices = "\n".join(prepared.notices)
        self.assertNotIn(pasted, notices)
        details = "\n".join(launch_sessions._card_session_details(self.runtime, prepared))
        self.assertNotIn(pasted, details)
        self.assertIn(secret_store.KEY_SHAPED_ENTRY, notices)
        self.assertIn(secret_store.KEY_SHAPED_ENTRY, details)
        attention, info = doctor._doctor_env_keep_report(self.runtime)
        self.assertNotIn(pasted, "\n".join(map(str, attention + info)))
        from claude_multi import tui

        screen = cli._SettingsScreen(self.runtime, tui.MONO_PALETTE, tty_in=io.StringIO(), tty_out=io.StringIO())
        row = screen._env_keep_row()
        self.assertEqual(row.key, views.SETTINGS_ENV_KEEP_KEY)
        self.assertNotIn(pasted, row.note + row.detail)
        self.assertIn(secret_store.KEY_SHAPED_ENTRY, row.note)


if __name__ == "__main__":
    unittest.main()
