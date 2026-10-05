"""Profile propagation to following sessions.

``lineup.on_saved`` / ``on_removed`` decide membership on each record
re-loaded under its own lifecycle lock; a live follower gets the
phase-A body (LIVE apply, or ``pending`` when relaunch-class — never an
exec); ended followers apply at their next resume; a pending relaunch
change is never dropped; gen-0 followers wait for their first 3.0
resume. Sessions are really launched (``_v4.V4Case``).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import cli, lineup, portability, profile, sessions, state, strict_json
from claude_multi.cli import consent as consent_mod
from test_lineup import LineupCase

FIXED = "11111111-1111-4111-8111-111111111111"


class PropagationCase(LineupCase):
    launch_profile = None

    def setUp(self) -> None:
        super().setUp()
        self.save_profile("mine", lambda d: None)

    def follower(self, name: str = "mine", *, ended: bool = False) -> dict:
        record = self.launch_fresh(self.profile_target(name))
        if ended:
            record = self.mutate(record["managed_id"], last_event_source="end")
        return record

    def edit(self, name: str, mutate) -> None:
        self.runtime.profiles.update(name, mutate)

    def saved(self, names, *, apply_live: bool = True) -> str:
        out = io.StringIO()
        lineup.on_saved(self.runtime, names, apply_live=apply_live, out=out)
        return out.getvalue()

    def removed(self, name: str, renamed_to: str | None = None) -> str:
        out = io.StringIO()
        lineup.on_removed(self.runtime, name, renamed_to=renamed_to, out=out)
        return out.getvalue()

    @staticmethod
    def where(record: dict) -> str:
        return f"{record['managed_id'][:8]} {record['cwd'].rsplit('/', 1)[-1]}"


class SavedTests(PropagationCase):
    def test_live_ended_pinned_and_not_applied(self) -> None:
        live = self.follower()
        ended = self.follower(ended=True)
        pinned = self.follower()
        self.mutate(pinned["managed_id"], follow=False)
        pinned_before = self.record_bytes(pinned["managed_id"])
        ended_before = self.record_bytes(ended["managed_id"])
        self.edit("mine", lambda d: d["agents"].update(
            {"cm-implementer": {"model": "opus55", "effort": "xhigh"}}))
        found, unreadable = lineup.followers(self.runtime, ["mine"])
        self.assertEqual(sorted(f.managed_id for f in found),
                         sorted([live["managed_id"], ended["managed_id"]]))
        self.assertEqual(unreadable, [])
        text = self.saved(["mine"], apply_live=False)
        self.assertIn(
            f"● {self.where(live)}: not applied — apply with: claude-multi lineup --session "
            f"{live['runtime_session_id']} follow", text,
        )
        text = self.saved(["mine"])
        self.assertIn(
            f"● {self.where(live)}: applied live (lineup gen 2) — run /reload-plugins in that "
            "session", text,
        )
        self.assertIn(f"○ {self.where(ended)}: applies at the next resume", text)
        self.assertEqual(self.store.load(live["managed_id"])["applied"]["agents"]
                         ["cm-implementer"]["key"], "opus55")
        self.assertEqual(self.record_bytes(ended["managed_id"]), ended_before)
        self.assertEqual(self.record_bytes(pinned["managed_id"]), pinned_before)
        # the ended follower shows the diff once at its next resume
        prepared = self.prepare_resume(ended["managed_id"])
        self.assertTrue(any("implementer" in line for line in prepared.diff), prepared.diff)

    def test_relaunch_only_change_is_pending_and_pending_is_never_dropped(self) -> None:
        live = self.follower()
        self.edit("mine", lambda d: d["native_agents"].update({"plan": "off"}))
        text = self.saved(["mine"])
        self.assertIn(
            f"● {self.where(live)}: relaunch needed (native agents: plan native → off); "
            f"recorded as pending — exit it, then claude-multi -r {live['managed_id']}", text,
        )
        pending = self.store.load(live["managed_id"])["pending"]
        self.assertEqual((pending["kind"], pending["profile"]), ("profile", "mine"))
        self.edit("mine", lambda d: d["agents"].update(
            {"cm-implementer": {"model": "opus55", "effort": "xhigh"}}))
        text = self.saved(["mine"])
        self.assertIn(f"● {self.where(live)}: has a pending relaunch change; not applied", text)
        self.assertEqual(self.store.load(live["managed_id"])["pending"], pending)
        self.mutate(live["managed_id"], last_event_source="end")
        text = self.saved(["mine"])
        self.assertIn(
            f"○ {self.where(live)}: has a pending relaunch change; it applies at the next resume",
            text,
        )

    def test_a_gen_0_follower_waits_for_its_first_3_0_resume(self) -> None:
        self.write_transcript(FIXED)
        out = io.StringIO()
        with unittest.mock.patch('claude_multi.cli.session_facts._original_cwd_for_adopt', return_value=self.runtime.cwd):
            code = cli.main(["sessions", "link", FIXED, "--profile", "mine"],
                            runtime=self.runtime, output_stream=out, interactive=False)
        self.assertEqual(code, 0, out.getvalue())
        linked = next(r for r in (self.store.load(p.stem) for p in self.store.scan_uuid_records())
                      if r["runtime_session_id"] == FIXED)
        before = self.record_bytes(linked["managed_id"])
        self.edit("mine", lambda d: d["agents"].update(
            {"cm-implementer": {"model": "opus55", "effort": "xhigh"}}))
        text = self.saved(["mine"])
        self.assertIn(f"○ {self.where(linked)}: no lineup-enabled scope yet; applies at the next resume", text)
        self.assertEqual(self.record_bytes(linked["managed_id"]), before)

    def test_unreadable_records_are_reported_and_skipped(self) -> None:
        self.follower()
        state.atomic_write(self.store.sessions_dir / f"{FIXED}.json", b"{not json")
        found, unreadable = lineup.followers(self.runtime, ["mine"])
        self.assertEqual(unreadable, [FIXED])
        text = self.saved(["mine"])
        self.assertIn(f"? {FIXED[:8]}: record unreadable; skipped", text)

    def test_migration_busy_is_reported_per_record(self) -> None:
        # Propagation is one served-change phase, so a running
        # migrate/restore stops the whole pass with one line, nothing written.
        live = self.follower()
        before = self.record_bytes(live["managed_id"])
        lock = sessions.migration_lock(self.store.root)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        text = self.saved(["mine"])
        self.assertIn(
            "? migration or restore in progress; not applied (rerun the save or /cm follow later)", text,
        )
        self.assertEqual(self.record_bytes(live["managed_id"]), before)

    def test_named_binding_edits_reach_the_referencing_profiles_followers(self) -> None:
        lcat = self.runtime.lineup_catalog()
        self.runtime.bindings.set("worker", "sol", "high", cat=lcat)
        self.edit("mine", lambda d: d["agents"].update({"cm-implementer": {"use": "worker"}}))
        live = self.follower()
        self.runtime.bindings.set("worker", "opus55", "xhigh", cat=lcat,
                                  profiles=self.runtime.profiles)
        names = lineup.profiles_for_binding(self.runtime, "worker")
        self.assertEqual(names, ["mine"])
        text = self.saved(names)
        self.assertIn("applied live (lineup gen 2)", text)
        self.assertEqual(self.store.load(live["managed_id"])["applied"]["agents"]
                         ["cm-implementer"]["key"], "opus55")


class DirectProfileSymmetryTests(PropagationCase):
    def test_policy_equal_direct_and_profile_are_live_both_ways(self) -> None:
        native = dict(profile.ad_hoc_direct("opus55")["native_agents"])
        self.edit("mine", lambda d: d["native_agents"].update(native))
        record = self.follower()
        self.mid, self.rid = record["managed_id"], record["runtime_session_id"]
        code, out, _ = self.request("direct")
        self.assertIn(lineup.RELOAD_LINES[0], out)
        self.assertEqual(self.store.load(self.mid)["applied"]["agents"], {})
        code, out, _ = self.request("profile mine")
        self.assertIn(lineup.RELOAD_LINES[0], out)
        self.assertIn("matches", self.converge_report()[-1])

    def test_different_policies_relaunch_both_ways(self) -> None:
        record = self.follower()
        self.mid, self.rid = record["managed_id"], record["runtime_session_id"]
        code, out, _ = self.request("direct")
        self.assertIn("relaunch needed: native agents:", out)
        direct = self.launch_fresh(self.direct_target("opus55"))
        self.mid, self.rid = direct["managed_id"], direct["runtime_session_id"]
        code, out, _ = self.request("profile mine")
        self.assertIn("relaunch needed: native agents:", out)


class RemovedTests(PropagationCase):
    def test_removal_pins_every_follower_and_drops_lead_target(self) -> None:
        live = self.follower()
        ended = self.follower(ended=True)
        self.mid, self.rid = live["managed_id"], live["runtime_session_id"]
        self.edit("mine", lambda d: d["lead"].update({"model": "fable"}))
        self.saved(["mine"])
        self.assertIn("lead_target", self.store.load(self.mid))
        self.runtime.profiles.delete("mine")
        text = self.removed("mine")
        for record in (live, ended):
            self.assertIn(
                f"{record['managed_id'][:8]}: profile 'mine' was removed; the session keeps its "
                "applied lineup and is now pinned", text,
            )
            after = self.store.load(record["managed_id"])
            self.assertEqual((after["profile"], after["follow"]), ("mine", False))
        self.assertNotIn("lead_target", self.store.load(self.mid))

    def test_rename_pins_and_keeps_lead_target(self) -> None:
        # A rename keeps lead_target (the pinned resume applies it).
        live = self.follower()
        self.edit("mine", lambda d: d["lead"].update({"model": "fable"}))
        self.saved(["mine"])
        self.runtime.profiles.rename("mine", "ours")
        text = self.removed("mine", renamed_to="ours")
        self.assertIn("profile 'mine' was renamed to 'ours'", text)
        after = self.store.load(live["managed_id"])
        self.assertFalse(after["follow"])
        self.assertEqual(after["lead_target"]["key"], "fable")

    def test_pending_is_rewritten_on_rename_and_cleared_on_removal(self) -> None:
        a = self.follower()
        b = self.follower()
        self.save_profile("other", lambda d: None)
        # a: a kind-profile pending change naming "other"; b: kind-lineup under "other"
        self.mid, self.rid = a["managed_id"], a["runtime_session_id"]
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = a["managed_id"]
        self.request("profile direct")
        pending = self.store.load(a["managed_id"])["pending"]
        self.mutate(a["managed_id"], pending={**pending, "profile": "other"})
        self.mutate(b["managed_id"], profile="other", follow=False, pending={
            "requested_at": "2026-09-25T00:00:00Z", "kind": "lineup", "profile": "other",
            "follow": True, "document": profile.ad_hoc_direct("opus55"),
            "reasons": ["native agents: explore replace → native"],
        })
        self.runtime.profiles.rename("other", "renamed")
        text = self.removed("other", renamed_to="renamed")
        self.assertIn(f"{a['managed_id'][:8]}: pending relaunch change now uses 'renamed' (pinned)",
                      text)
        got = self.store.load(a["managed_id"])["pending"]
        self.assertEqual((got["profile"], got["follow"]), ("renamed", False))
        self.runtime.profiles.delete("renamed")
        text = self.removed("renamed")
        self.assertIn(
            f"{a['managed_id'][:8]}: pending relaunch change dropped: profile 'renamed' was removed",
            text,
        )
        self.assertNotIn("pending", self.store.load(a["managed_id"]))
        kept = self.store.load(b["managed_id"])["pending"]
        self.assertEqual((kept["kind"], kept["follow"]), ("lineup", False))


if __name__ == "__main__":
    unittest.main()


class ImportPropagationTests(PropagationCase):
    """An import that writes a followed profile
    propagates inside its own served-change phase — the held token, never a
    second barrier acquisition — and never live-applies."""

    def test_import_with_live_followers_does_not_self_deadlock(self) -> None:
        live = self.follower("balanced")
        user_file = profile.profiles_dir(self.runtime.environ) / "balanced.json"
        if user_file.exists():
            user_file.unlink()  # a virtual seed here: the import may shadow it
        self.assertFalse(self.runtime.profiles.has_user("balanced"))
        before = self.store.load(live["managed_id"])
        document = self.runtime.profiles.load("balanced")
        document["agents"]["cm-implementer"] = {"model": "opus55", "effort": "xhigh"}
        document.pop("seed", None)
        bundle = {"format": portability.FORMAT, "version": portability.VERSION,
                  "exported_by": {"launcher_version": "3.1.0-dev", "catalog_version": 36},
                  "profiles": {"balanced": {"document": document}}, "bindings": {}, "settings": {},
                  "providers": {}, "trust_requests": {"routes": [], "admissions": [], "transport_choices": {}}}
        directory = Path(tempfile.mkdtemp(prefix="cm-import-follow-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, True))
        path = directory / "bundle.json"
        path.write_bytes(strict_json.pretty_file_bytes(bundle))
        os.chmod(path, 0o600)
        seen = []
        real_on_saved = lineup.on_saved

        def recording(runtime, names, **kwargs):
            seen.append((sorted(names), kwargs.get("barrier") is not None, kwargs.get("served"), kwargs.get("apply_live")))
            state.require_barrier(kwargs["barrier"], root=runtime.session_store.root)
            return real_on_saved(runtime, names, **kwargs)

        class Answers(io.StringIO):
            def confirm(self, _text):
                return True

        result: dict = {}

        def run() -> None:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err):
                result["code"] = cli.main(["import", str(path), "--apply"], runtime=self.runtime,
                                          input_stream=Answers(), output_stream=out, interactive=True)
            result["out"], result["err"] = out.getvalue(), err.getvalue()

        with mock.patch.object(consent_mod, "stdio_ttys", return_value=True), \
                mock.patch.object(lineup, "on_saved", side_effect=recording):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join(60)
        self.assertFalse(worker.is_alive(), "import --apply deadlocked on its own barrier")
        self.assertEqual(result["code"], 0, result.get("out", "") + result.get("err", ""))
        self.assertEqual(seen, [(["balanced"], True, True, False)])
        self.assertIn("followers: 1 session(s) follow balanced (1 running)", result["out"])
        self.assertTrue(self.runtime.profiles.has_user("balanced"))
        after = self.store.load(live["managed_id"])
        # Never live-applied: the running follower keeps its lineup and gets
        # a pending change for its next resume.
        self.assertEqual(after["applied"], before["applied"])
        self.assertEqual(after["lineup_generation"], before["lineup_generation"])
        self.assertIn("pending", after)
        # The barrier is free again afterwards.
        with sessions.served_change_phase(self.runtime.session_store.root, self.runtime.home, timeout=1.0):
            pass
