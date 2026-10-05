"""Tests for ``transition.py``: converge and the expected plan only.

The 2.x transition engine (``prepare``/``execute``/``restore_exec_failure``,
``build_diff``, the print-only outcome) is deleted: a relaunch is a resume
through ``launch.perform_launch`` (covered by ``test_launch_v4`` and
the ``sessions transition`` alias tests in ``test_cli``). The converted 2.x
cases below run on v4 records from a real launch; the converge cases
live in ``test_converge_v4``.
"""

from __future__ import annotations

import os
import stat
import unittest
from unittest import mock

from claude_multi import compiler, profile, scope, sessions, state, transition
from claude_multi.transition import TransitionError
from _v4 import V4Case

FIXED_ID = "11111111-1111-4111-8111-111111111111"


class _Case(V4Case):
    def setUp(self) -> None:
        super().setUp()
        self.record = self.launch_fresh()
        self.mid = self.record["managed_id"]

    def converge(self, mid: str | None = None) -> list[str]:
        return transition.converge(
            self.store.root,
            self.store,
            mid or self.mid,
            runtime_parts=self.runtime.converge_parts(),
        )


class StateMarkerTransitionTests(_Case):
    def test_converge_refuses_under_a_newer_marker_and_writes_nothing(self) -> None:
        state.atomic_write(self.store.root / sessions.STATE_MARKER, b"5\n")

        def authority_bytes():
            return {
                p: p.read_bytes()
                for p in self.store.root.rglob("*")
                if p.is_file() and "bin" not in p.relative_to(self.store.root).parts
            }

        before = authority_bytes()
        with mock.patch.object(scope, "ensure_hook_shim_v3") as shim:
            with self.assertRaises(sessions.StateMarkerError):
                self.converge()
        shim.assert_not_called()
        self.assertEqual(authority_bytes(), before)


class ConvergeTests(_Case):
    def test_window_before_first_rename_prunes_staging(self) -> None:
        staging = self.store.root / "scopes" / f".{self.mid}.new"
        state.ensure_private_dir(staging)
        state.atomic_write(staging / "settings.json", b"{}\n")
        report = self.converge()
        self.assertFalse(staging.exists())
        self.assertIn("live scope matches the record-authoritative compile", report)

    def test_live_missing_without_prev_recompiles(self) -> None:
        scope._remove_tree(self.live(self.mid))
        report = self.converge()
        self.assertIn(
            "live scope was missing; recompiled from the record and the installed catalog", report
        )
        self.assertIn("live scope matches the record-authoritative compile", self.converge())

    def test_unexpected_extra_file_is_drift(self) -> None:
        state.atomic_write(self.live(self.mid) / "extra.txt", b"x")
        report = self.converge()
        self.assertTrue(any("unexpected file extra.txt" in line for line in report), report)
        self.assertFalse((self.live(self.mid) / "extra.txt").exists())

    def test_unexpected_directory_is_drift(self) -> None:
        state.ensure_private_dir(self.live(self.mid) / "stray")
        report = self.converge()
        self.assertTrue(any("unexpected directory stray" in line for line in report), report)
        self.assertFalse((self.live(self.mid) / "stray").exists())

    def test_symlinked_internal_directory_is_drift(self) -> None:
        agents = self.live(self.mid) / ".claude" / "agents"
        target = self.root / "elsewhere"
        target.mkdir()
        scope._remove_tree(agents)
        os.symlink(target, agents)
        report = self.converge()
        self.assertTrue(any("is a symlink" in line for line in report), report)
        self.assertFalse(agents.is_symlink())

    def test_private_mode_drift_is_repaired(self) -> None:
        settings_path = self.live(self.mid) / "settings.json"
        os.chmod(settings_path, 0o644)
        report = self.converge()
        self.assertTrue(any("settings.json mode is not 0600" in line for line in report), report)
        self.assertEqual(stat.S_IMODE(os.lstat(settings_path).st_mode), 0o600)

    def test_converge_rejects_non_uuid(self) -> None:
        with self.assertRaises(TransitionError):
            self.converge("not-a-uuid")

    def test_converge_missing_record_fails_closed(self) -> None:
        with self.assertRaises(sessions.SessionError):
            self.converge(FIXED_ID)

    def test_converge_state_root_mismatch_refused(self) -> None:
        with self.assertRaises(TransitionError):
            transition.converge(
                self.root / "other",
                self.store,
                self.mid,
                runtime_parts=self.runtime.converge_parts(),
            )

    def test_converge_symlinked_live_scope_fails_closed(self) -> None:
        live = self.live(self.mid)
        moved = self.root / "moved-scope"
        os.rename(live, moved)
        os.symlink(moved, live)
        with self.assertRaises(TransitionError):
            self.converge()
        self.assertTrue(live.is_symlink())

    def test_converge_loads_the_record_under_the_lifecycle_lock(self) -> None:
        acquisitions: list[bool] = []
        real_load = self.store.load

        def spying_load(session_id: str) -> dict:
            probe = self.store.lifecycle_lock(session_id)
            acquired = probe.acquire(blocking=False)
            if acquired:
                probe.release()
            acquisitions.append(acquired)
            return real_load(session_id)

        with mock.patch.object(self.store, "load", spying_load):
            self.converge()
        self.assertEqual(acquisitions[:1], [False])


class ExpectedPlanTests(_Case):
    def test_expected_plan_is_the_launch_compile(self) -> None:
        parts = self.runtime.converge_parts()
        ep = transition.expected_plan(
            self.store.load(self.mid),
            docs=parts.docs,
            prompt_bodies=parts.prompt_bodies,
            state_root=self.store.root,
            hook_command=parts.hook_command,
            token_helper_command=parts.token_helper_command,
            environ=parts.environ, managed_root=parts.managed_root,
            live=self.live(self.mid),
        )
        self.assertTrue(ep.launch_files_kept)
        self.assertFalse(ep.catalog_launch_differs)
        self.assertFalse(ep.lead_switched)
        disk = scope.read_disk_plan(self.live(self.mid), ep.plan)
        self.assertEqual(scope.plan_hash(disk), scope.plan_hash(ep.plan))

    def test_expected_plan_needs_a_compiled_v4_record(self) -> None:
        record = dict(self.store.load(self.mid))
        record["lineup_generation"] = 0
        parts = self.runtime.converge_parts()
        with self.assertRaises(TransitionError):
            transition.expected_plan(
                record,
                docs=parts.docs,
                prompt_bodies=parts.prompt_bodies,
                state_root=self.store.root,
                hook_command=parts.hook_command,
                token_helper_command=parts.token_helper_command,
                environ=parts.environ, managed_root=parts.managed_root,
                live=None,
            )


class SessionSentinelEnvTests(V4Case):
    def test_env_set_carries_the_session_sentinel(self) -> None:
        lcat = self.runtime.lineup_catalog()
        eff = self.runtime.current_effective()
        lineup = profile.resolve(self.runtime.profiles.load("balanced"), lcat, effective=eff)
        for action in (compiler.build_fresh(FIXED_ID), compiler.build_resume(FIXED_ID)):
            with self.subTest(action=action.kind):
                result = compiler.compile_lineup_launch(
                    docs=self.runtime.ordinary_docs,
                    prompt_bodies=self.runtime.catalog.prompt_bodies,
                    lineup=lineup,
                    effective=eff,
                    session_action=action,
                    lineup_generation=1,
                    state_root=self.store.root,
                    scope_dir=self.store.root / "scopes" / FIXED_ID,
                    hook_command=self.runtime.hook3_command,
                    token_helper_command=self.runtime.token_helper_command,
                    launch_environ=self.runtime.environ,
                    worktree_available=True,
                )
                self.assertEqual(result.env_set[transition.SESSION_ENV_VAR], FIXED_ID)
                self.assertEqual(result.env_set[transition.LEGACY_SESSION_ENV_VAR], FIXED_ID)


class RecordAuthorityOperatorTests(unittest.TestCase):
    """record-authority compiles use the record snapshot and never
    the ledger; a migrated line matching the applied key or selector counts
    as admitted (the legacy line it replaced was active)."""

    def _lcat(self) -> profile.LineupCatalog:
        from claude_multi import catalog as catalog_mod
        from _catalog import FIXTURE_ROOT
        docs = catalog_mod.load_catalog(FIXTURE_ROOT).docs
        lcat = profile.LineupCatalog.from_docs(docs)
        entry = {"provider": "kimi", "selector": "custom-oldbox", "efforts": ["high"], "status": "new"}
        lines = dict(lcat.lines, **{"custom-oldbox": entry, "custom-fresh": dict(entry, selector="custom-fresh")})
        return profile.LineupCatalog(
            lines=lines, retired=lcat.retired, providers=lcat.providers, roles=lcat.roles,
            agent_efforts=lcat.agent_efforts, lead_efforts=lcat.lead_efforts, catalog_version=lcat.catalog_version,
            operator={"custom-oldbox": {"origin": "operator-migrated", "legacy_key": "oldbox"},
                      "custom-fresh": {"origin": "operator", "legacy_key": None}},
        )

    def test_migrated_line_matching_the_record_counts_as_admitted(self) -> None:
        from claude_multi import settings as settings_mod
        eff = settings_mod.Effective(providers_enabled={}, admitted_lines=frozenset(), unknown=())
        lcat = self._lcat()
        for applied_lead in ({"key": "oldbox", "selector": "x"}, {"key": "other", "selector": "custom-oldbox"},
                             {"key": "custom-oldbox", "selector": "y"}):
            with self.subTest(lead=applied_lead):
                record = {"applied": {"lead": applied_lead, "agents": {}}}
                out = transition.recorded_operator_admissions(record, lcat, eff)
                self.assertEqual(out.admitted_lines, frozenset({"custom-oldbox"}))
        record = {"applied": {"lead": {"key": "custom-fresh", "selector": "custom-fresh"}, "agents": {}}}
        # A plain operator line rides the snapshot only: no inference.
        self.assertEqual(transition.recorded_operator_admissions(record, lcat, eff).admitted_lines, frozenset())
        unrelated = {"applied": {"lead": {"key": "opus", "selector": "claude-multi-opus-4-8[1m]"}, "agents": {}}}
        self.assertIs(transition.recorded_operator_admissions(unrelated, lcat, eff), eff)

    def test_expected_plan_takes_no_ledger_input(self) -> None:
        import inspect
        params = inspect.signature(transition.expected_plan).parameters
        self.assertFalse({"ledger", "layer", "snapshot"} & set(params))


if __name__ == "__main__":
    unittest.main()


class FeedbackPreferencePlanTests(_Case):
    """The preference is an explicit compile input,
    restaged from proven launch files (no drift), and a preference-only
    launch-item difference is reported as its own class."""

    def _plan(self, value: str):
        parts = self.runtime.converge_parts()
        return transition.expected_plan(
            self.store.load(self.mid), docs=parts.docs, prompt_bodies=parts.prompt_bodies,
            state_root=self.store.root, hook_command=parts.hook_command,
            token_helper_command=parts.token_helper_command, environ=parts.environ,
            managed_root=parts.managed_root, live=self.live(self.mid), feedback_drafts=value,
        )

    def test_launch_compile_carries_the_default_off(self) -> None:
        live = scope.read_disk_plan(self.live(self.mid), self._plan("off").plan)
        self.assertEqual(live.settings["env"][scope.FEEDBACK_ENV], "false")
        self.assertEqual(self.runtime.converge_parts().feedback_drafts, "off")

    def test_toggle_restages_the_launch_files_and_is_preference_only(self) -> None:
        same = self._plan("off")
        self.assertFalse(same.catalog_launch_differs or same.preference_launch_differs)
        toggled = self._plan("notify")
        self.assertTrue(toggled.launch_files_kept)
        self.assertTrue(toggled.catalog_launch_differs)
        self.assertTrue(toggled.preference_launch_differs)
        self.assertNotIn(scope.FEEDBACK_ENV, toggled.catalog_plan.settings["env"])
        disk = scope.read_disk_plan(self.live(self.mid), toggled.plan)
        self.assertEqual(scope.plan_hash(disk), scope.plan_hash(toggled.plan))

    def test_invalid_preference_value_fails_closed(self) -> None:
        with self.assertRaises(scope.ScopeError):
            self._plan("quiet")
        self.assertEqual(scope.feedback_drafts_env("off"), {"CLAUDE_CODE_SEND_FEEDBACK": "false"})
        self.assertEqual(scope.feedback_drafts_env("notify"), {})
