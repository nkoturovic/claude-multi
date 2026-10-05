"""Live apply races with real FileLocks and threads.

Each race parks one writer inside the record's lifecycle lock (a hook in the
live apply's compile step, or the ``execve`` seam of a real
``perform_launch``) and runs the other writer on a thread; every wait is
bounded (10 s) so a lock-order bug fails instead of hanging.
"""

from __future__ import annotations

import io
import threading
import unittest
from unittest import mock

from claude_multi import lineup, profile, settings, sessions, strict_json
from test_lineup import LineupCase

TIMEOUT = 10


class _Park:
    """Hold the first live-apply compile inside the lock until released."""

    def __init__(self) -> None:
        self.inside = threading.Event()
        self.go = threading.Event()
        self.real = lineup._compile_live
        self.calls = 0

    def __call__(self, runtime, record, live_dir):
        self.calls += 1
        if self.calls == 1:
            self.inside.set()
            assert self.go.wait(TIMEOUT), "parked apply was never released"
        return self.real(runtime, record, live_dir)


class LiveApplyRaceTests(LineupCase):
    def thread(self, target) -> tuple[threading.Thread, threading.Event, list]:
        done = threading.Event()
        box: list = []

        def run() -> None:
            try:
                box.append(target())
            except BaseException as exc:  # pragma: no cover - reported by the test
                box.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker, done, box

    def test_a_profile_save_inside_an_in_flight_apply_propagates_after_it(self) -> None:
        # S does not follow X yet; `/cm profile X` reads X, the editor
        # saves X' and propagates before S's record is saved. Membership is
        # decided under S's lock, so X' reaches S after the apply finishes.
        self.save_profile("x", lambda d: None)
        saved = threading.Event()
        park = _Park()

        def editor() -> str:
            self.runtime.profiles.update("x", lambda d: d["agents"].update(
                {"cm-implementer": {"model": "opus55", "effort": "xhigh"}}))
            saved.set()
            out = io.StringIO()
            lineup.on_saved(self.runtime, ["x"], apply_live=True, out=out)
            return out.getvalue()

        def compile_hook(runtime, record, live_dir):
            if park.calls == 0:
                park.calls = 1
                thread_box.append(self.thread(editor))
                assert saved.wait(TIMEOUT), "the editor save never completed"
            return park.real(runtime, record, live_dir)

        thread_box: list = []
        with mock.patch.object(lineup, "_compile_live", compile_hook):
            code, out, _ = self.request("profile x")
        self.assertIn("session now follows profile x", out)
        worker, done, box = thread_box[0]
        self.assertTrue(done.wait(TIMEOUT), "propagation never finished")
        self.assertIn("applied live", box[0])
        record = self.store.load(self.mid)
        self.assertEqual((record["profile"], record["follow"]), ("x", True))
        final = profile.evaluate(
            self.runtime.profiles.load("x"), self.runtime.lineup_catalog(), bindings={},
            effective=settings.effective_from_snapshot(record["applied"]["settings"]),
        ).lineup
        self.assertEqual(
            {rid: (b["key"], b["effort"]) for rid, b in record["applied"]["agents"].items()},
            {rid: (a.binding.key, a.binding.effort) for rid, a in final.agents.items()},
        )
        self.assertIn("live scope matches the record-authoritative compile", self.converge_report())

    def test_two_live_applies_serialize(self) -> None:
        park = _Park()
        with mock.patch.object(lineup, "_compile_live", park):
            first, first_done, first_box = self.thread(
                lambda: self.request("set implementer=opus55:xhigh"))
            self.assertTrue(park.inside.wait(TIMEOUT))
            second, second_done, second_box = self.thread(
                lambda: self.request("set reviewer=opus55:xhigh"))
            self.assertFalse(second_done.wait(0.3), "the second apply ran inside the first's lock")
            park.go.set()
            self.assertTrue(first_done.wait(TIMEOUT) and second_done.wait(TIMEOUT))
        self.assertIn("lineup gen 2:", first_box[0][1])
        self.assertIn("lineup gen 3:", second_box[0][1])
        agents = self.store.load(self.mid)["applied"]["agents"]
        self.assertEqual(agents["cm-implementer"]["key"], "opus55")
        self.assertEqual(agents["cm-reviewer"]["key"], "opus55")

    def test_an_apply_waits_for_a_launch_holding_the_lock_through_exec(self) -> None:
        self.write_transcript(self.rid)
        in_exec, go = threading.Event(), threading.Event()
        real_execve = self._execve

        def blocking_execve(path, argv, env):
            in_exec.set()
            assert go.wait(TIMEOUT)
            return real_execve(path, argv, env)

        self.runtime.execve = blocking_execve
        launch, launch_done, launch_box = self.thread(lambda: self.resume(self.mid))
        self.assertTrue(in_exec.wait(TIMEOUT))
        apply, apply_done, apply_box = self.thread(
            lambda: self.request("set implementer=opus55:xhigh"))
        self.assertFalse(apply_done.wait(0.3), "the apply ran while the launch held the lock")
        go.set()
        self.assertTrue(launch_done.wait(TIMEOUT) and apply_done.wait(TIMEOUT))
        self.assertIn("lineup gen", apply_box[0][1])
        record = self.store.load(self.mid)
        self.assertEqual(record["launch_epoch"], 2)
        self.assertEqual(record["applied"]["agents"]["cm-implementer"]["key"], "opus55")

    def test_session_start_and_postmodel_during_an_apply_keep_both_effects(self) -> None:
        lead_set = strict_json.loads((self.live(self.mid) / "lead-set.json").read_bytes())
        row = next(r for r in lead_set["rows"] if r["key"] == "fable")
        park = _Park()
        with mock.patch.object(lineup, "_compile_live", park):
            apply, apply_done, apply_box = self.thread(
                lambda: self.request("set implementer=opus55:xhigh"))
            self.assertTrue(park.inside.wait(TIMEOUT))
            start, start_done, _ = self.thread(lambda: self.store.reconcile_runtime(
                self.mid, observed_runtime_id=self.rid, source="startup",
                cwd=self.runtime.cwd, launch_epoch=1))
            switch, switch_done, switch_box = self.thread(lambda: self.store.record_lead_switch(
                self.mid, observed_runtime_id=self.rid, launch_epoch=1, row=row))
            self.assertFalse(start_done.wait(0.3))
            park.go.set()
            self.assertTrue(apply_done.wait(TIMEOUT) and start_done.wait(TIMEOUT)
                            and switch_done.wait(TIMEOUT))
        record = self.store.load(self.mid)
        self.assertEqual(record["applied"]["agents"]["cm-implementer"]["key"], "opus55")
        self.assertEqual(record["last_event_source"], "startup")
        self.assertEqual(record["applied"]["lead"]["key"], "fable")
        self.assertIn(switch_box[0], ("switched", "pinned"))


if __name__ == "__main__":
    unittest.main()
