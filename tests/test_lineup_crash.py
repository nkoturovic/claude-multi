"""Crash injection at every live-apply write step.

The live apply writes agent files (new → changed → removed), ``lineup.md``,
``lineup.gen``, the lineup-log line and the record last, with no rollback
beyond the atomic per-file writes. After a failure at any step:

- the output requests repair and the record's ``launch_epoch`` is unchanged;
- ``doctor --repair`` (``transition.converge``) restores scope ==
  compile(record); when ``lineup.gen`` was already published the generation
  is fenced above it, so the lead never sees it go backwards;
- a resume prepared before the failed apply launches iff neither
  ``lineup.gen`` nor the record was written, refuses a moved generation when only
  ``lineup.gen`` moved and fails its CAS when the record was saved.
"""

from __future__ import annotations

import unittest
from unittest import mock

from claude_multi import hooks, launch, lineup, scope, sessions, state, strict_json, transition
from test_lineup import LineupCase

# (checkpoint, detail filter): raise when the live apply reaches it.
STEPS = (
    ("9.1", "first-new"),
    ("9.1-done", None),
    ("9.2", None),
    ("9.3", None),
    ("9.4", None),
    ("9.5", None),
    ("9.6", None),
    ("9.7-before", None),
    ("9.7-during", None),
)
GEN_PUBLISHED = {"9.5", "9.6", "9.7-before", "9.7-during"}
RECORD_SAVED = {"9.7-during"}


class LiveApplyCrashTests(LineupCase):
    def setUp(self) -> None:
        super().setUp()
        # One request with a new, a changed and a removed agent file.
        self.save_profile("mix", lambda d: (
            d["agents"].pop("cm-analyst-strong"),
            d["agents"].update({"cm-designer": {"model": "sol", "effort": "high"},
                                "cm-implementer": {"model": "opus55", "effort": "xhigh"}}),
        ))

    def crash(self, step: str):
        """Context: the live apply fails at ``step``."""

        if step == "9.7-during":
            real = sessions.SessionStore.save

            def save(store, record):
                real(store, record)
                raise state.CommittedStateError(5, "simulated directory fsync failure")

            return mock.patch.object(sessions.SessionStore, "save", save)

        def checkpoint(name, detail=None):
            if name == step:
                raise OSError(5, f"injected crash at {step}")

        return mock.patch.object(lineup, "_checkpoint", checkpoint)

    def test_every_step_reports_r17_and_converge_restores_record_authority(self) -> None:
        for step, _detail in STEPS:
            with self.subTest(step=step):
                record = self.launch_fresh(self.profile_target("balanced"))
                self.mid, self.rid = record["managed_id"], record["runtime_session_id"]
                with self.crash(step):
                    code, out, _ = self.request("profile mix")
                self.assertEqual(code, 0)
                self.assertTrue(
                    out.startswith("claude-multi: the lineup change failed after partially "
                                   "updating the scope ("), out,
                )
                self.assertIn(
                    f"the record still holds lineup generation 1; run claude-multi doctor "
                    f"--repair {self.mid}, then retry", out,
                )
                after = self.store.load(self.mid)
                self.assertEqual(after["launch_epoch"], record["launch_epoch"])
                published = step in GEN_PUBLISHED
                report = self.converge_report()
                current = self.store.load(self.mid)
                if step in RECORD_SAVED:
                    # the record took generation 2 itself: nothing to fence
                    self.assertEqual(current["lineup_generation"], 2, report)
                elif published:
                    # Fenced above the published generation 2.
                    self.assertGreater(current["lineup_generation"], 2, report)
                    gen = hooks.read_gen(self.live(self.mid))
                    self.assertGreaterEqual(int(gen.split()[0]), 2)
                else:
                    self.assertEqual(current["lineup_generation"], 1, report)
                ep = transition.expected_plan(
                    current,
                    docs=self.runtime.ordinary_docs,
                    prompt_bodies=self.runtime.catalog.prompt_bodies,
                    state_root=self.store.root,
                    hook_command=self.runtime.hook3_command,
                    token_helper_command=self.runtime.token_helper_command,
                    environ=self.runtime.environ,
                    managed_root=self.runtime.managed_root,
                    live=self.live(self.mid),
                )
                self.assertEqual(scope.live_drift(self.live(self.mid), ep.plan), [])
                self.assertEqual(current["launch_epoch"], record["launch_epoch"])

    def test_a_resume_prepared_before_the_failed_apply(self) -> None:
        for step, _detail in STEPS:
            with self.subTest(step=step):
                record = self.launch_fresh(self.profile_target("balanced"))
                self.mid, self.rid = record["managed_id"], record["runtime_session_id"]
                prepared = self.prepare_resume(self.mid)
                with self.crash(step):
                    self.request("profile mix")
                if step in RECORD_SAVED:
                    with self.assertRaisesRegex(launch.LaunchError, "prepare the launch again"):
                        self.runtime.perform(prepared)
                elif step in GEN_PUBLISHED:
                    with self.assertRaisesRegex(
                        launch.LaunchError,
                        rf"session {self.mid} lineup generation on disk changed after "
                        r"preparation \(1 → 2\)",
                    ):
                        self.runtime.perform(prepared)
                else:
                    self.runtime.perform(prepared)
                    resumed = self.store.load(self.mid)
                    self.assertEqual(resumed["launch_epoch"], record["launch_epoch"] + 1)
                    self.assertIn("live scope matches the record-authoritative compile",
                                  self.converge_report())

    def test_a_crash_after_the_first_new_file_leaves_other_files_untouched(self) -> None:
        live = self.live(self.mid)
        before = self.tree(live)
        with self.crash("9.1"):
            self.request("profile mix")
        after = self.tree(live)
        self.assertIn(".claude/agents/cm-designer.md", after)
        self.assertEqual(
            {k: v for k, v in after.items() if k != ".claude/agents/cm-designer.md"}, before
        )
        self.assertEqual(strict_json.loads(self.record_bytes(self.mid))["lineup_generation"], 1)


if __name__ == "__main__":
    unittest.main()
