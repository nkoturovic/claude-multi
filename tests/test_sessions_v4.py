"""Record v4 and the store core.

Nothing here produces a v4 record except these tests. The version
preservation matrix (every lifecycle writer on raw v1, v2, v3 managed, v3
ordinary and v4 records) is permanent: it pins that 2.x records keep their
own version through every hook write.
"""

from __future__ import annotations

import copy
import os
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import (
    catalog,
    compiler,
    continuity,
    hooks,
    lineup_log,
    profile,
    sessions,
    settings,
    state,
    strict_json,
)
from claude_multi.sessions import SessionError
from _catalog import FIXTURE_ROOT
import _v3


MID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
RUNTIME = "33333333-3333-4333-8333-333333333333"
ALIAS = "44444444-4444-4444-8444-444444444444"
FORK = "55555555-5555-4555-8555-555555555555"
THIRD = "66666666-6666-4666-8666-666666666666"
NOW = "2026-09-26T00:00:00Z"
LATER = "2026-09-27T00:00:00Z"
HASH0 = "sha256:" + "0" * 64
FENCE = "sha256:" + "1" * 64
LEAD_SELECTOR = "claude-multi-opus-5-5[1m]"
AGENT_SELECTOR = "gpt-multi-sol-high[1m]"

_BUNDLE = catalog.load_catalog(FIXTURE_ROOT)
_LCAT = profile.LineupCatalog.from_docs(_BUNDLE.docs)


def _schema() -> dict:
    return strict_json.load(FIXTURE_ROOT / "schemas" / "session.schema.json")


def _settings_snapshot() -> dict:
    eff = settings.effective(
        {"version": 1}, provider_ids=_LCAT.providers, line_keys=_LCAT.lines
    )
    return settings.snapshot(eff)


def _applied(*, key: str = "opus55", selector: str | None = LEAD_SELECTOR,
             effort: str = "ultracode", agents: dict | None = None) -> dict:
    return {
        "lead": {
            "key": key,
            "generation": "5.5",
            "selector": selector,
            "effort": effort,
            "env": {},
            "compaction": {"window": 800000, "trigger": 700000, "percent": 90, "scalar": None},
        },
        "agents": (
            {"cm-explorer": {"key": "sol", "generation": "5.6",
                             "selector": AGENT_SELECTOR, "effort": "high"}}
            if agents is None else agents
        ),
        "native_agents": {"explore": "replace", "plan": "native", "general_purpose": "off"},
        "workflows": "native",
        "lead_providers": None,
        "settings_overrides": {},
        "no_subagents": False,
        "settings": _settings_snapshot(),
    }


def _v4(managed_id: str = MID, *, generation: int = 1, profile_name: str | None = "balanced",
        follow: bool = True, applied: dict | None = None, now: str = NOW,
        cwd: str = "/project/path", launch_epoch: int = 1) -> dict:
    applied = applied if applied is not None else _applied()
    compiled = generation >= 1
    return sessions.make_v4_record(
        managed_id=managed_id,
        cwd=cwd,
        profile=profile_name,
        follow=follow,
        applied=applied,
        lineup_generation=generation,
        lead_class="large",
        catalog_version=33,
        catalog_hash=HASH0,
        launcher_version="3.0.0",
        launch_epoch=launch_epoch,
        launch_fence=FENCE if compiled else None,
        scope_lead=sessions.lead_ref(applied["lead"]) if compiled else None,
        mutation_token=sessions.new_mutation_token(),
        now=now,
    )


def _snapshot() -> dict:
    return _v3.snapshot_for(_BUNDLE.docs)


_SNAPSHOT = _snapshot()


def _v3_managed(managed_id: str = MID, now: str = NOW) -> dict:
    record = _v3.make_record(
        managed_id=managed_id, cwd="/project/path", composition_name="default",
        snapshot=_SNAPSHOT, catalog_version=32, catalog_hash=HASH0,
        launcher_version="2.26.0", mode="durable", scope_generation=1, now=now,
        launch_epoch=1,
    )
    record["mutation_token"] = sessions.new_mutation_token()
    return record


def _v3_ordinary(managed_id: str = MID, now: str = NOW) -> dict:
    record = _v3.make_ordinary_record(
        managed_id=managed_id, runtime_session_id=managed_id, cwd="/project/path",
        model="qwen38", context_profile="large", catalog_version=32,
        catalog_hash=HASH0, launcher_version="2.26.0", now=now, launch_epoch=1,
    )
    record["mutation_token"] = sessions.new_mutation_token()
    return record


def _v2(managed_id: str = MID) -> dict:
    return {
        "version": 2, "session_id": managed_id, "cwd": "/project/path",
        "composition_name": "default",
        "composition_hash": strict_json.bundle_digest(_SNAPSHOT),
        "snapshot": _SNAPSHOT, "mode": "durable", "scope_generation": 1,
        "workflows": "native", "catalog_version": 30, "catalog_hash": HASH0,
        "launcher_version": "2.2.0", "created_at": NOW, "forked_from": None,
    }


def _v1(managed_id: str = MID) -> dict:
    record = _v2(managed_id)
    record["version"] = 1
    for key in ("mode", "scope_generation", "workflows"):
        record.pop(key)
    return record


class V4TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-sessions-v4-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = sessions.SessionStore(self.root, _schema())

    def marker4(self) -> None:
        sessions.write_state_marker(self.root)

    def write_raw(self, record: dict, managed_id: str | None = None) -> Path:
        stable = managed_id or record.get("managed_id") or record["session_id"]
        path = self.store.sessions_dir / f"{stable}.json"
        state.atomic_write(path, strict_json.canonical_file_bytes(record))
        return path

    def disk(self, managed_id: str = MID) -> dict:
        return strict_json.loads(self.store.read_record_bytes(managed_id))


# --------------------------------------------------------------------- schema


class SchemaAndInvariantTests(V4TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.marker4()

    def assert_refused(self, record: dict, text: str | None = None) -> None:
        with self.assertRaises(SessionError) as caught:
            _v3.save_any(self.store, record)
        if text is not None:
            self.assertIn(text, str(caught.exception))

    def test_fresh_v4_record_round_trips(self) -> None:
        record = _v4()
        self.store.save(record)
        self.assertEqual(self.store.load(MID), record)
        self.assertEqual(self.store.load_v4(MID), record)
        raw, data = self.store.load_raw(MID)
        self.assertEqual(raw, record)
        self.assertEqual(data, strict_json.canonical_file_bytes(record))
        self.assertEqual(record["applied_hash"], strict_json.bundle_digest(record["applied"]))
        self.assertEqual(record["version"], 4)
        self.assertIsNone(record["migrated_from_version"])

    def test_development_launcher_version_round_trips(self) -> None:
        record = _v4()
        record["launcher_version"] = "3.1.0-dev"
        self.store.save(record)
        self.assertEqual(self.store.load_v4(MID), record)
        self.assertEqual(sessions.release_tuple("3.1.0-dev"), (3, 1, 0))
        self.assertFalse(sessions.may_hold_env_token(record))

    def test_release_tuple_and_env_token_boundary(self) -> None:
        for version, expected, may_hold in (
            ("2.25.1-dev", (2, 25, 1), True),
            ("2.26.0-dev", (2, 26, 0), False),
            ("3.1.0", (3, 1, 0), False),
        ):
            with self.subTest(version=version):
                self.assertEqual(sessions.release_tuple(version), expected)
                record = {"launcher_version": version, "last_event_source": None}
                self.assertEqual(sessions.may_hold_env_token(record), may_hold)
                record["last_event_source"] = "end"
                self.assertFalse(sessions.may_hold_env_token(record))

    def test_release_versions_round_trip_under_the_record_schema(self) -> None:
        for version in ("1.0.0", "1.0.0-dev", "0.9.0", "1.2.3"):
            with self.subTest(version=version):
                record = _v4()
                record["launcher_version"] = version
                self.store.save(record)
                self.assertEqual(self.store.load_v4(MID), record)

    def test_helper_only_launches_by_catalog_capability(self) -> None:
        # A launch whose catalog carries the helper-only capability holds no
        # environment credential, whatever its launcher number; an older
        # catalog keeps the launcher-version comparison.
        boundary = sessions.HELPER_ONLY_CATALOG
        for launcher, catalog_version, may_hold in (
            ("1.0.0", boundary, False),
            ("1.0.0-dev", boundary, False),
            ("1.0.0", boundary + 5, False),
            ("1.0.0", boundary - 1, True),   # an older catalog: the launcher comparison decides
            ("2.25.1", 31, True),
            ("2.26.0", 32, False),
            ("3.1.0", 36, False),
        ):
            with self.subTest(launcher=launcher, catalog=catalog_version):
                record = {"launcher_version": launcher, "catalog_version": catalog_version,
                          "last_event_source": "startup"}
                self.assertEqual(sessions.helper_only_launch(record), not may_hold)
                self.assertEqual(sessions.may_hold_env_token(record), may_hold)
                record["last_event_source"] = "end"
                self.assertFalse(sessions.may_hold_env_token(record))

    def test_helper_only_ignores_a_catalog_that_is_not_an_integer(self) -> None:
        for value in (True, "37", 37.0, None):
            with self.subTest(value=value):
                record = {"launcher_version": "1.0.0", "catalog_version": value, "last_event_source": None}
                self.assertTrue(sessions.may_hold_env_token(record))

    def test_a_fresh_record_of_this_release_is_helper_only(self) -> None:
        import claude_multi

        record = _v4()
        record["launcher_version"] = claude_multi.__version__
        record["catalog_version"] = sessions.HELPER_ONLY_CATALOG
        record["last_event_source"] = None
        self.store.save(record)
        self.assertFalse(sessions.may_hold_env_token(self.store.load_v4(MID)))

    def test_other_launcher_version_suffixes_are_refused(self) -> None:
        for version in ("3.1.0-rc1", "3.1.0-dev-dev", "3.1-dev"):
            with self.subTest(version=version):
                record = _v4()
                record["launcher_version"] = version
                self.assert_refused(record)

    def test_v4_only_keys_refused_on_a_v3_record(self) -> None:
        extras = {
            "profile": "balanced", "follow": False, "applied": _applied(),
            "applied_hash": HASH0, "lineup_generation": 1, "lead_class": "large",
            "title": "x", "launch_fence": FENCE,
            "lead_target": {"key": "opus", "effort": "xhigh", "selector": "s"},
            "scope_lead": {"key": "opus", "effort": "xhigh", "selector": "s"},
        }
        for key, value in extras.items():
            for base in (_v3_managed(), _v3_ordinary()):
                with self.subTest(key=key, type=base["session_type"]):
                    self.assert_refused({**base, key: value}, "invalid session record")

    def test_removed_v3_keys_refused_on_a_v4_record(self) -> None:
        removed = {
            "session_type": sessions.SESSION_TYPE_MANAGED, "composition_name": "default",
            "composition_hash": HASH0, "snapshot": _SNAPSHOT, "mode": "durable",
            "scope_generation": 1, "workflows": "native", "ordinary_model": "qwen38",
            "context_profile": "large", "no_subagents": False,
        }
        for key, value in removed.items():
            with self.subTest(key=key):
                self.assert_refused({**_v4(), key: value}, "invalid session record")

    def test_applied_is_closed(self) -> None:
        for path in (("extra",), ("lead", "extra"), ("settings", "extra")):
            with self.subTest(path=path):
                applied = _applied()
                target = applied
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = 1
                self.assert_refused(sessions.with_applied(_v4(), applied))

    def test_agent_keys_must_be_agent_roles(self) -> None:
        binding = {"key": "sol", "generation": "5.6", "selector": AGENT_SELECTOR, "effort": "high"}
        for role_id in ("cm-lead", "cm-nope", "explorer"):
            with self.subTest(role=role_id):
                record = sessions.with_applied(_v4(), _applied(agents={role_id: binding}))
                self.assert_refused(record, "not an agent role")

    def test_applied_hash_mismatch_fails_load(self) -> None:
        record = _v4()
        record["applied"]["workflows"] = "off"  # hand edit without the hash
        self.write_raw(record)
        with self.assertRaises(SessionError) as caught:
            self.store.load(MID)
        self.assertIn("applied_hash does not match", str(caught.exception))

    def test_follow_needs_a_profile(self) -> None:
        self.assert_refused(_v4(profile_name=None, follow=True), "follow requires a profile")
        self.store.save(_v4(profile_name=None, follow=False))

    def test_null_selector_needs_generation_zero(self) -> None:
        applied = _applied(selector=None)
        self.store.save(_v4(generation=0, applied=applied, follow=False))
        record = _v4(generation=0, applied=applied, follow=False)
        record["lineup_generation"] = 1
        record["launch_fence"] = FENCE
        record["scope_lead"] = {"key": "opus55", "effort": "ultracode", "selector": "x"}
        self.assert_refused(record, "lineup_generation 0")

    def test_pending_rules(self) -> None:
        good_profile = {"requested_at": NOW, "kind": "profile", "profile": "max",
                        "follow": True, "document": None, "reasons": ["lead class"]}
        self.store.save({**_v4(), "pending": good_profile})
        document = profile.ad_hoc_direct("opus55")
        self.store.save({**_v4(), "pending": {**good_profile, "kind": "lineup",
                                              "profile": None, "follow": False,
                                              "document": document}})
        bad = (
            {**good_profile, "profile": None},
            {**good_profile, "document": document},
            {**good_profile, "kind": "lineup", "document": None},
            {**good_profile, "kind": "lineup", "document": {"version": 1}},
            {**good_profile, "reasons": []},
        )
        for pending in bad:
            with self.subTest(pending=pending):
                self.assert_refused({**_v4(), "pending": pending})

    def test_migration_agrees_with_migrated_from_version(self) -> None:
        migration = {"from_version": 3, "migrated_at": NOW, "outcome": "unchanged",
                     "composition_name": "default", "session_type": "managed-composition"}
        record = {**_v4(generation=0, follow=False), "migration": migration}
        for key in ("launch_fence", "scope_lead"):
            record.pop(key, None)
        self.assert_refused(record, "migration.from_version")
        record["migrated_from_version"] = 3
        self.store.save(record)
        self.assert_refused({**record, "migrated_from_version": 2}, "migration.from_version")

    def test_title_is_one_printable_line(self) -> None:
        self.store.save({**_v4(), "title": "Refactor the parser"})
        for title in ("two\nlines", "tab\there", "", "x" * 81):
            with self.subTest(title=title):
                self.assert_refused({**_v4(), "title": title})

    def test_fence_and_scope_lead_iff_compiled(self) -> None:  # invariant 9
        compiled = _v4(generation=2)
        for key in ("launch_fence", "scope_lead"):
            with self.subTest(missing=key):
                broken = copy.deepcopy(compiled)
                broken.pop(key)
                self.assert_refused(broken, f"{key} is required exactly")
        gen0 = _v4(generation=0, follow=False)
        self.store.save(gen0)
        for key, value in (("launch_fence", FENCE),
                           ("scope_lead", sessions.lead_ref(gen0["applied"]["lead"]))):
            with self.subTest(extra=key):
                self.assert_refused({**gen0, key: value}, f"{key} is required exactly")

    def test_lead_target_differs_from_the_applied_lead(self) -> None:  # invariant 10
        record = _v4()
        lead = record["applied"]["lead"]
        self.assert_refused({**record, "lead_target": sessions.lead_ref(lead)},
                            "lead_target equals the applied lead")
        self.store.save({**record, "lead_target": {**sessions.lead_ref(lead), "effort": "max"}})
        self.store.save({**record, "lead_target": {"key": "opus", "effort": "ultracode",
                                                   "selector": "claude-multi-opus-4-8[1m]"}})

    def test_identity_invariants_are_shared_with_v3(self) -> None:
        record = _v4()
        record["runtime_aliases"] = [{"session_id": MID, "source": "startup", "observed_at": NOW}]
        self.assert_refused(record, "duplicate/current runtime alias")
        self.assert_refused({**_v4(), "forked_from": MID}, "forked from itself")

    def test_applied_from_lineup_builds_a_valid_record(self) -> None:
        eff = settings.effective({"version": 1}, provider_ids=_LCAT.providers, line_keys=_LCAT.lines)
        lineup = profile.resolve(_BUNDLE.seed_profiles["balanced"], _LCAT, effective=eff)
        context = compiler.LeadContext(
            lead_class=lineup.lead_class, window=800000, trigger=700000, percent=90,
            scalar=None, min_provider_tokens=800000, min_client_tokens=1000000, validated=True,
        )
        applied = sessions.applied_from_lineup(
            lineup, context=context, settings_snapshot=settings.snapshot(eff), no_subagents=False
        )
        self.assertEqual(applied["lead"]["key"], lineup.lead.binding.key)
        self.assertEqual(applied["lead"]["selector"], lineup.lead.binding.selector)
        self.assertEqual(
            applied["lead"]["compaction"],
            {"window": 800000, "trigger": 700000, "percent": 90, "scalar": None,
             "client_tokens": lineup.lead.binding.client_context_tokens,
             "provider_tokens": lineup.lead.binding.provider_context_tokens},
        )
        self.assertEqual(sorted(applied["agents"]), sorted(lineup.agents))
        record = sessions.make_v4_record(
            managed_id=MID, cwd="/p", profile="balanced", follow=True, applied=applied,
            lineup_generation=1, lead_class=lineup.lead_class, catalog_version=33,
            catalog_hash=HASH0, launcher_version="3.0.0", launch_epoch=0,
            launch_fence=FENCE, scope_lead=sessions.lead_ref(applied["lead"]), now=NOW,
        )
        self.store.save(record)
        document = sessions.applied_document(record)
        profile.parse(document)
        self.assertEqual(document["lead"], {"model": applied["lead"]["key"],
                                            "effort": applied["lead"]["effort"]})
        self.assertEqual(profile.resolve(document, _LCAT, effective=eff).applied_bindings(),
                         lineup.applied_bindings())

    def test_next_v4_record_drops_pending_and_lead_target(self) -> None:
        current = {**_v4(), "title": "keep", "lead_target": {"key": "opus", "effort": "xhigh",
                                                              "selector": "s"},
                   "pending": {"requested_at": NOW, "kind": "profile", "profile": "max",
                               "follow": True, "document": None, "reasons": ["r"]}}
        applied = _applied(key="opus5", selector="claude-multi-opus-5[1m]")
        nxt = sessions.next_v4_record(
            current, profile="max", follow=True, applied=applied, lineup_generation=2,
            lead_class="large", catalog_version=34, catalog_hash=HASH0,
            launcher_version="3.0.1", launch_epoch=2, launch_fence=FENCE,
            scope_lead=sessions.lead_ref(applied["lead"]),
        )
        self.assertNotIn("pending", nxt)
        self.assertNotIn("lead_target", nxt)
        self.assertEqual(nxt["title"], "keep")
        self.assertEqual(nxt["applied_hash"], strict_json.bundle_digest(applied))
        self.assertEqual(nxt["created_at"], current["created_at"])
        self.store.save(nxt)


# --------------------------------------------------------------------- marker


class MarkerAndLockTests(V4TestCase):
    def test_marker_values(self) -> None:
        marker = self.root / sessions.STATE_MARKER
        self.assertEqual(sessions.check_state_marker(self.root), 3)
        for value, expected in ((b"3\n", 3), (b"4\n", 4), (b"4", 4)):
            state.atomic_write(marker, value)
            self.assertEqual(sessions.check_state_marker(self.root), expected)
        state.atomic_write(marker, b"5")
        with self.assertRaises(sessions.StateMarkerError) as caught:
            sessions.check_state_marker(self.root)
        self.assertEqual(
            str(caught.exception),
            "state belongs to a newer claude-multi (state version 5); "
            "this launcher understands state version 4 or older. Fix: "
            + sessions.StateMarkerError.NEWER_REMEDY,
        )
        state.atomic_write(marker, b"garbage")
        with self.assertRaises(sessions.StateMarkerError) as caught:
            sessions.check_state_marker(self.root)
        self.assertIn("state version unknown", str(caught.exception))
        state.atomic_write(marker, b"4\n")
        marker.chmod(0o644)
        with self.assertRaises(sessions.StateMarkerError):
            sessions.check_state_marker(self.root)
        marker.unlink()
        target = self.root / "target"
        state.atomic_write(target, b"4\n")
        marker.symlink_to(target)
        with self.assertRaises(sessions.StateMarkerError):
            sessions.check_state_marker(self.root)

    def test_ensure_state_v4_writes_once_under_the_exclusive_lock(self) -> None:
        seen: list[bool] = []
        original = sessions.write_state_marker

        def write(root):
            seen.append(hooks.migration_lock_held(root))
            original(root)

        with mock.patch.object(sessions, "write_state_marker", side_effect=write):
            self.assertTrue(sessions.ensure_state_v4(self.root))
        self.assertEqual(seen, [True])
        marker = self.root / sessions.STATE_MARKER
        self.assertEqual(marker.read_bytes(), b"4\n")
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        self.assertFalse(hooks.migration_lock_held(self.root))
        with mock.patch.object(sessions, "acquire_exclusive_bounded") as bounded:
            self.assertFalse(sessions.ensure_state_v4(self.root))  # fast path: no lock
        bounded.assert_not_called()
        self.assertEqual(marker.read_bytes(), b"4\n")

    def test_ensure_state_v4_gives_up_after_the_bounded_wait(self) -> None:
        holder = sessions.migration_lock(self.root)
        self.assertTrue(holder.acquire(blocking=False))
        clock = [0.0]
        sleeps: list[float] = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        try:
            with self.assertRaises(sessions.MigrationBusyError) as caught:
                sessions.ensure_state_v4(self.root, clock=lambda: clock[0], sleep=sleep)
        finally:
            holder.release()
        self.assertEqual(str(caught.exception), sessions.MIGRATION_BUSY_TEXT)
        self.assertGreaterEqual(clock[0], 3.0)
        self.assertLessEqual(len(sleeps), 31)
        self.assertFalse((self.root / sessions.STATE_MARKER).exists())

    def test_v4_save_needs_marker_4_and_v3_saves_under_either(self) -> None:
        with self.assertRaises(SessionError) as caught:
            self.store.save(_v4())
        self.assertIn("state marker missing: v4 records need state version 4", str(caught.exception))
        self.assertFalse(self.store.exists(MID))
        _v3.save_any(self.store, _v3_managed())
        self.marker4()
        _v3.save_any(self.store, _v3_ordinary(OTHER))
        self.store.save(_v4(THIRD))
        self.assertEqual(self.store.load(OTHER)["version"], 3)

    def test_marker_write_and_remove(self) -> None:
        sessions.write_state_marker(self.root)
        self.assertEqual(sessions.check_state_marker(self.root), 4)
        self.assertTrue(sessions.remove_state_marker(self.root))
        self.assertFalse(sessions.remove_state_marker(self.root))
        self.assertEqual(sessions.check_state_marker(self.root), 3)

    def test_launcher_write_guard(self) -> None:
        with sessions.launcher_write_guard(self.root) as held:
            self.assertTrue(held.shared)
            # A second shared holder coexists; the hooks' probe is not blocked.
            with sessions.launcher_write_guard(self.root):
                self.assertFalse(hooks.migration_lock_held(self.root))
            self.assertFalse(sessions.migration_lock(self.root).acquire(blocking=False))
        exclusive = sessions.migration_lock(self.root)
        self.assertTrue(exclusive.acquire(blocking=False))
        try:
            with self.assertRaises(sessions.MigrationBusyError):
                with sessions.launcher_write_guard(self.root):
                    self.fail("entered under an exclusive migration lock")
            self.assertTrue(hooks.migration_lock_held(self.root))
        finally:
            exclusive.release()

    def test_migration_window_wait(self) -> None:
        self.assertTrue(sessions.migration_window_wait(self.root))  # no lock file at all
        self.assertFalse((self.root / "migration.lock").exists())
        holder = sessions.migration_lock(self.root)
        self.assertTrue(holder.acquire(blocking=False))
        clock = [0.0]

        def freeing_sleep(seconds):
            clock[0] += seconds
            if clock[0] >= 1.0:
                holder.release()

        self.assertTrue(sessions.migration_window_wait(
            self.root, clock=lambda: clock[0], sleep=freeing_sleep))
        self.assertLess(clock[0], 1.2)
        self.assertTrue(holder.acquire(blocking=False))
        clock[0] = 0.0
        sleeps: list[float] = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        try:
            self.assertFalse(sessions.migration_window_wait(
                self.root, clock=lambda: clock[0], sleep=sleep))
        finally:
            holder.release()
        self.assertTrue(all(value == 0.1 for value in sleeps))
        self.assertGreaterEqual(clock[0], 3.0)

    def test_default_schema_is_the_packaged_schema(self) -> None:
        packaged = sessions.default_schema()
        self.assertEqual(packaged, _schema())
        packaged["mutated"] = True
        self.assertNotIn("mutated", sessions.default_schema())


# ------------------------------------------------------ lifecycle preservation


_PRE_V3_KINDS = ("v1", "v2")
_KINDS = ("v1", "v2", "v3-managed", "v3-ordinary", "v4")


class LifecycleVersionPreservationTests(V4TestCase):
    """Every lifecycle writer keeps the raw version (never removed)."""

    def seed(self, kind: str, *, pending_fork: str | None = None) -> dict:
        builders = {"v1": _v1, "v2": _v2, "v3-managed": _v3_managed,
                    "v3-ordinary": _v3_ordinary, "v4": _v4}
        record = builders[kind](MID)
        if kind == "v4":
            self.marker4()
        if pending_fork is not None:
            record["pending_forks"] = [{"session_id": pending_fork, "observed_at": NOW}]
            if kind == "v4":
                pass
        self.write_raw(record, MID)
        return record

    @staticmethod
    def expected_rest(raw: dict, extra: tuple[str, ...] = ()) -> bytes:
        view = raw if raw["version"] in (3, 4) else sessions._normalize_legacy_record(raw)
        rest = {k: v for k, v in view.items()
                if k not in sessions.LIFECYCLE_PATCH_KEYS and k not in extra}
        return strict_json.canonical_bytes(rest)

    def assert_preserved(self, kind: str, raw: dict, extra: tuple[str, ...] = ()) -> None:
        saved = self.disk()
        self.assertEqual(saved["version"], 4 if kind == "v4" else 3)
        self.assertEqual(self.expected_rest(saved, extra), self.expected_rest(raw, extra))
        if kind in _PRE_V3_KINDS:
            self.assertEqual(saved["migrated_from_version"], raw["version"])

    def epoch(self, raw: dict) -> int:
        return raw.get("launch_epoch", 0)

    def test_reconcile_every_source(self) -> None:
        for kind in _KINDS:
            for source in ("startup", "resume", "clear", "compact", "fork"):
                with self.subTest(kind=kind, source=source):
                    self.setUp()
                    raw = self.seed(kind)
                    observed = FORK if source == "fork" else RUNTIME
                    self.store.reconcile_runtime(
                        MID, observed_runtime_id=observed, source=source,
                        cwd="/project/path", launch_epoch=self.epoch(raw), now=LATER,
                    )
                    self.assert_preserved(kind, raw)
                    saved = self.disk()
                    if source == "fork":
                        self.assertEqual(saved["pending_forks"][-1]["session_id"], FORK)
                    else:
                        self.assertEqual(saved["runtime_session_id"], RUNTIME)
                        self.assertEqual(saved["last_event_source"], source)

    def test_session_end(self) -> None:
        for kind in _KINDS:
            with self.subTest(kind=kind):
                self.setUp()
                raw = self.seed(kind)
                self.store.record_session_end(
                    MID, observed_runtime_id=MID, reason="exit",
                    launch_epoch=self.epoch(raw), now=LATER,
                )
                self.assert_preserved(kind, raw)
                self.assertEqual(self.disk()["last_event_source"], "end")

    def test_relink(self) -> None:
        for kind in _KINDS:
            for cwd in (None, "/moved/here"):
                with self.subTest(kind=kind, cwd=cwd):
                    self.setUp()
                    raw = self.seed(kind)
                    self.store.update_last("/project/path", MID)
                    self.store.relink_runtime(MID, observed_runtime_id=RUNTIME, cwd=cwd, now=LATER)
                    self.assert_preserved(kind, raw, ("cwd", "mutation_token"))
                    saved = self.disk()
                    self.assertEqual(saved["runtime_session_id"], RUNTIME)
                    self.assertEqual(saved["launch_epoch"], self.epoch(raw) + 1)
                    self.assertEqual(saved["cwd"], cwd or "/project/path")

    def test_fork_writers(self) -> None:
        for kind in ("v3-managed", "v3-ordinary", "v4"):
            with self.subTest(kind=kind, op="resolve_fork"):
                self.setUp()
                raw = self.seed(kind, pending_fork=FORK)
                self.store.resolve_fork(MID, FORK)
                self.assert_preserved(kind, raw)
                self.assertEqual(self.disk()["pending_forks"], [])
            with self.subTest(kind=kind, op="converge_pending_forks"):
                self.setUp()
                raw = self.seed(kind, pending_fork=MID)  # the fork holds authority
                self.assertTrue(self.store.converge_pending_forks(MID))
                self.assert_preserved(kind, raw)
                self.assertEqual(self.disk()["pending_forks"], [])
            with self.subTest(kind=kind, op="link parent"):
                self.setUp()
                raw = self.seed(kind, pending_fork=FORK)
                adopted = _v4(OTHER, generation=0)
                adopted["runtime_session_id"] = FORK
                self.marker4()  # `sessions link` runs ensure_state_v4 first
                self.store.link(adopted)
                self.assert_preserved(kind, raw)
                saved = self.disk()
                self.assertEqual(saved["pending_forks"], [])
                self.assertEqual(saved["launch_epoch"], self.epoch(raw) + 1)

    def test_v3_ordinary_repin_reaches_disk_and_stays_version_3(self) -> None:
        raw = _v3_ordinary()
        raw["observed_model"] = "claude-multi-kimi-k3-max[1m]"
        raw["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        self.write_raw(raw)
        self.store.reconcile_runtime(
            MID, observed_runtime_id=MID, source="startup", cwd="/project/path",
            model="kimi-k3", model_profile="large",
            observed_model="claude-multi-kimi-k3-max[1m]", launch_epoch=1, now=LATER,
        )
        saved = self.disk()
        self.assertEqual(saved["version"], 3)
        self.assertEqual(saved["ordinary_model"], "kimi-k3")
        self.assertNotIn("observed_model", saved)
        self.assertEqual(saved["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertEqual(self.expected_rest(saved, ("ordinary_model",)),
                         self.expected_rest(raw, ("ordinary_model",)))


# ----------------------------------------------------------------- session end


class SessionEndTests(V4TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.marker4()
        self.store.save(_v4(launch_epoch=2))

    def test_only_the_current_runtime_at_the_current_epoch_ends(self) -> None:
        before = self.store.read_record_bytes(MID)
        for runtime, epoch in ((FORK, 2), (MID, 1), (MID, 3)):
            with self.subTest(runtime=runtime, epoch=epoch):
                self.store.record_session_end(MID, observed_runtime_id=runtime,
                                              reason="exit", launch_epoch=epoch)
                self.assertEqual(self.store.read_record_bytes(MID), before)
        ended = self.store.record_session_end(MID, observed_runtime_id=MID, reason="exit",
                                              launch_epoch=2, now=LATER)
        self.assertEqual(ended["last_event_source"], "end")
        self.assertEqual(self.disk()["last_end_reason"], "exit")


# ----------------------------------------------------------- v4 model evidence


class V4ModelEvidenceTests(V4TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.marker4()

    def start(self, model, *, source="startup", observed_model=None, normalize=None):
        return self.store.reconcile_runtime(
            MID, observed_runtime_id=MID, source=source, cwd="/project/path",
            model=model, observed_model=observed_model, launch_epoch=1, now=LATER,
            normalize=normalize,
        )

    def test_tri_state(self) -> None:
        record = _v4()
        self.store.save(record)
        updated = self.start("claude-multi-opus-5[1m]")
        self.assertEqual(updated["observed_model"], "claude-multi-opus-5[1m]")
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)
        self.assertEqual(self.disk()["applied"], record["applied"])  # never a re-pin
        self.start(sessions.MODEL_EQUIVALENT)
        saved = self.disk()
        self.assertNotIn("observed_model", saved)
        self.assertEqual(saved["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertEqual(saved["applied_hash"], record["applied_hash"])

    def test_repair_evidence_survives_compact_resume_and_relink(self) -> None:
        record = {**_v4(), "observed_model": "claude-multi-opus-5[1m]",
                  "identity_state": sessions.IDENTITY_REPAIR_NEEDED}
        self.store.save(record)
        self.start(None, source="compact")
        self.start(None, source="resume")
        for step in ("compact", "resume"):
            with self.subTest(step=step):
                saved = self.disk()
                self.assertEqual(saved["observed_model"], "claude-multi-opus-5[1m]")
                self.assertEqual(saved["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)
        self.store.relink_runtime(MID, observed_runtime_id=MID)
        saved = self.disk()
        self.assertEqual(saved["observed_model"], "claude-multi-opus-5[1m]")
        self.assertEqual(saved["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)

    def test_model_equivalent_on_a_v3_view_raises(self) -> None:
        for record in (_v3_managed(), _v3_ordinary()):
            with self.subTest(type=record["session_type"]):
                with self.assertRaises(SessionError):
                    sessions.reconcile_runtime_record(
                        record, observed_runtime_id=MID, source="startup",
                        model=sessions.MODEL_EQUIVALENT, launch_epoch=1,
                    )

    # A test-local callback that follows the production callback's rules.
    @staticmethod
    def callback(*, source, calls):
        forms = {LEAD_SELECTOR, "claude-opus-5-5"}

        def normalize(view, observed, cwd):
            calls.append(view["version"])
            managed_rule = view["version"] == 4 or view["session_type"] == sessions.SESSION_TYPE_MANAGED
            if source == "compact" and managed_rule:
                return sessions.Evidence(None, None, None, None)
            if observed is None:
                return sessions.Evidence(None, None, None, cwd)
            if view["version"] == 4:
                model = sessions.MODEL_EQUIVALENT if observed in forms else observed
                return sessions.Evidence(model, None, observed, cwd)
            return sessions.Evidence(view.get("ordinary_model"), "other-profile", observed, cwd)
        return normalize

    def race_to_v4(self, v4_record: dict):
        """Patch load_raw: migrate converts the record right before the locked read."""

        original = self.store.load_raw

        def converted(managed_id):
            state.atomic_write(self.store._record_path(managed_id),
                               strict_json.canonical_file_bytes(v4_record))
            return original(managed_id)

        return mock.patch.object(self.store, "load_raw", side_effect=converted)

    def test_normalize_runs_on_the_locked_version(self) -> None:
        _v3.save_any(self.store, _v3_ordinary())
        lock_free = self.store.load(MID)
        self.assertEqual(lock_free["version"], 3)
        calls: list[int] = []
        with self.race_to_v4(_v4()):
            updated = self.start(LEAD_SELECTOR, normalize=self.callback(source="startup", calls=calls))
        self.assertEqual(calls, [4])
        saved = self.disk()
        self.assertEqual(saved["version"], 4)
        self.assertNotIn("observed_model", saved)
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_AUTHORITATIVE)

    def test_compact_admissibility_is_decided_on_the_locked_view(self) -> None:  # Ordinary-only observations must not alter a managed record.
        _v3.save_any(self.store, _v3_ordinary())  # its lock-free rule would keep a compact model
        v4_record = {**_v4(), "observed_model": "claude-multi-opus-5[1m]",
                     "observed_cwd": "/elsewhere",
                     "identity_state": sessions.IDENTITY_REPAIR_NEEDED}
        calls: list[int] = []
        with self.race_to_v4(v4_record):
            self.store.reconcile_runtime(
                MID, observed_runtime_id=MID, source="compact", cwd="/third/place",
                model="gpt-multi-sol-high[1m]", launch_epoch=1, now=LATER,
                normalize=self.callback(source="compact", calls=calls),
            )
        self.assertEqual(calls, [4])
        saved = self.disk()
        self.assertEqual(saved["observed_model"], "claude-multi-opus-5[1m]")
        self.assertEqual(saved["observed_cwd"], "/elsewhere")
        self.assertEqual(saved["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)


# ------------------------------------------------------------------ readers


class ReaderTests(V4TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.marker4()

    def test_needs_choice_for_unknown_and_retired_keys(self) -> None:
        for cat in (_BUNDLE, _LCAT):
            for key, expected in (("opus55", False), ("muse-spark", True), ("custom-gone", True)):
                with self.subTest(cat=type(cat).__name__, key=key):
                    record = sessions.with_applied(_v4(), _applied(key=key))
                    self.assertIs(sessions.lead_needs_choice(record, cat), expected)
                    summary = sessions.record_summary(record, cat)
                    self.assertIs(summary.needs_choice, expected)
        record = sessions.with_applied(_v4(), _applied(key="custom-gone"))
        self.assertEqual(
            sessions.lead_choice_notice(record, _LCAT),
            f"lead custom-gone: unknown to catalog {_LCAT.catalog_version} (not a custom model either)",
        )
        self.assertIn("needs a model choice", sessions.lead_choice_notice(
            sessions.with_applied(_v4(), _applied(key="muse-spark")), _BUNDLE))
        self.assertIsNone(sessions.lead_choice_notice(_v4(), _BUNDLE))
        with self.assertRaises(sessions.LegacyRecordError):
            sessions.lead_needs_choice(_v3_managed(), _BUNDLE)

    def test_record_summary_every_version(self) -> None:
        record = {**_v4(), "title": "T", "lead_target": {"key": "opus", "effort": "xhigh",
                                                         "selector": "s"}}
        summary = sessions.record_summary(record)
        self.assertEqual(
            (summary.version, summary.profile_label, summary.follow, summary.lead_key,
             summary.agent_count, summary.lineup_generation, summary.needs_choice,
             summary.pending, summary.lead_target, summary.title),
            (4, "balanced", True, "opus55", 1, 1, None, False, True, "T"),
        )
        adhoc = sessions.record_summary(_v4(profile_name=None, follow=False))
        self.assertEqual(adhoc.profile_label, "(ad-hoc)")
        managed = sessions.record_summary(_v3_managed())
        self.assertEqual((managed.version, managed.profile_label, managed.follow,
                          managed.lineup_generation), (3, "cm:default", None, None))
        self.assertEqual(managed.agent_count, len(_SNAPSHOT["variants"]))
        ordinary = sessions.record_summary(_v3_ordinary())
        self.assertEqual((ordinary.profile_label, ordinary.lead_key, ordinary.agent_count),
                         ("gateway:qwen38", "qwen38", 0))
        legacy = sessions.record_summary(_v1())
        self.assertEqual((legacy.version, legacy.profile_label), (3, "cm:default"))

    def test_lead_identity(self) -> None:
        self.assertEqual(sessions.lead_identity(_v4()), ("opus55", LEAD_SELECTOR))
        self.assertEqual(sessions.lead_identity(_v3_managed()),
                         (_SNAPSHOT["lead"]["model"], _SNAPSHOT["lead"]["client_selector"]))
        self.assertEqual(sessions.lead_identity(_v3_ordinary()), ("qwen38", None))
        self.assertEqual(sessions.lead_identity(_v2())[0], _SNAPSHOT["lead"]["model"])

    def test_load_v4_refuses_2x_records(self) -> None:
        self.write_raw(_v1())
        with self.assertRaises(sessions.LegacyRecordError) as caught:
            self.store.load_v4(MID)
        self.assertEqual(
            str(caught.exception),
            f"session {MID} is a legacy record (version 1); resume it (claude-multi -r {MID}) "
            "or run claude-multi migrate",
        )
        self.assertEqual(self.store.load(MID)["version"], 3)
        raw, _data = self.store.load_raw(MID)
        self.assertEqual(raw["version"], 1)

    def test_launch_digest(self) -> None:
        settings_doc = {"availableModels": ["a", "b"], "modelPicker": {"x": 1},
                        "model": "a", "env": {"K": "v"}, "hooks": {"h": 1}}
        lead_set = b'{"lead":{}}\n'
        digest = sessions.launch_digest(settings_doc, lead_set)
        reordered = dict(reversed(list(settings_doc.items())))
        reordered["hooks"] = {"other": 2}
        reordered["permissions"] = {"deny": []}
        self.assertEqual(sessions.launch_digest(reordered, lead_set), digest)
        for key in sessions.LAUNCH_TIME_SETTINGS_KEYS:
            with self.subTest(key=key):
                changed = {**settings_doc, key: "changed"}
                self.assertNotEqual(sessions.launch_digest(changed, lead_set), digest)
        self.assertNotEqual(sessions.launch_digest(settings_doc, lead_set + b" "), digest)
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")


# ------------------------------------------------------------ resolve_runtime


class ResolveRuntimeTests(V4TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.marker4()
        record = _v4()
        record["runtime_session_id"] = RUNTIME
        record["runtime_aliases"] = [{"session_id": ALIAS, "source": "clear", "observed_at": NOW}]
        record["pending_forks"] = [{"session_id": FORK, "observed_at": NOW}]
        record["identity_state"] = sessions.IDENTITY_PENDING_FORK
        self.store.save(record)
        self.record = record

    def test_runtime_and_alias_resolve(self) -> None:
        self.assertEqual(self.store.resolve_runtime(RUNTIME), self.record)
        self.assertEqual(self.store.resolve_runtime(ALIAS), self.record)

    def test_managed_id_is_refused_with_the_runtime_id(self) -> None:
        with self.assertRaises(SessionError) as caught:
            self.store.resolve_runtime(MID)
        self.assertEqual(
            str(caught.exception),
            f"{MID} is a stable session id; lineup --session takes the runtime id "
            f"({RUNTIME}) — /cm passes it automatically",
        )
        self.assertEqual(self.store.resolve(MID), self.record)  # the alias path keeps it

    def test_pending_fork_and_unknown(self) -> None:
        with self.assertRaises(sessions.PendingForkError) as caught:
            self.store.resolve_runtime(FORK)
        self.assertEqual(caught.exception.record["managed_id"], MID)
        self.assertEqual(str(caught.exception), sessions.pending_fork_message(self.record))
        with self.assertRaises(SessionError) as caught:
            self.store.resolve_runtime(THIRD)
        self.assertEqual(str(caught.exception), f"no claude-multi session has runtime id {THIRD}")

    def test_unreadable_record_claiming_it_fails_closed(self) -> None:
        state.atomic_write(self.store.sessions_dir / f"{OTHER}.json",
                           strict_json.canonical_file_bytes({"version": 4, "runtime_session_id": THIRD}))
        with self.assertRaises(SessionError) as caught:
            self.store.resolve_runtime(THIRD)
        self.assertIn(f"unreadable session record {OTHER} may claim runtime", str(caught.exception))


# -------------------------------------------------------------------- pointers


class PointerTests(V4TestCase):
    CWD = "/project/path"

    def setUp(self) -> None:
        super().setUp()
        self.marker4()
        self.store.save(_v4(MID, now=NOW))
        self.store.save(_v4(OTHER, now=LATER))

    def legacy(self, session_id: str) -> Path:
        path = self.store._legacy_pointer_path(self.CWD)
        state.atomic_write(path, strict_json.canonical_file_bytes(
            {"cwd": self.CWD, "session_id": session_id, "session_type": "ordinary-gateway"}))
        return path

    def primary(self, session_id: str) -> Path:
        path = self.store._pointer_path(self.CWD)
        state.atomic_write(path, strict_json.canonical_file_bytes(
            {"cwd": self.CWD, "session_id": session_id, "session_type": "managed-composition"}))
        return path

    def test_single_candidates(self) -> None:
        self.assertIsNone(self.store.last(self.CWD))
        self.legacy(MID)
        self.assertEqual(self.store.last(self.CWD), MID)
        self.store._legacy_pointer_path(self.CWD).unlink()
        self.primary(OTHER)
        self.assertEqual(self.store.last(self.CWD), OTHER)

    def test_newer_record_wins_either_way_round(self) -> None:
        self.primary(MID)
        self.legacy(OTHER)
        self.assertEqual(self.store.last(self.CWD), OTHER)
        self.primary(OTHER)
        self.legacy(MID)
        self.assertEqual(self.store.last(self.CWD), OTHER)

    def test_unreadable_record_loses_and_tie_goes_to_the_primary(self) -> None:
        self.primary(THIRD)  # no record
        self.legacy(MID)
        self.assertEqual(self.store.last(self.CWD), MID)
        self.primary(MID)
        self.legacy(THIRD)
        self.assertEqual(self.store.last(self.CWD), MID)
        self.store.save(_v4(THIRD, now=NOW))  # same last_seen_at as MID
        self.primary(THIRD)
        self.legacy(MID)
        self.assertEqual(self.store.last(self.CWD), THIRD)
        state.atomic_write(self.store._legacy_pointer_path(self.CWD), b"not json")
        self.assertEqual(self.store.last(self.CWD), THIRD)

    def test_update_last_writes_no_session_type(self) -> None:
        self.assertTrue(self.store.update_last(self.CWD, MID))
        self.assertEqual(strict_json.loads(self.store._pointer_path(self.CWD).read_bytes()),
                         {"cwd": self.CWD, "session_id": MID})
        self.assertFalse(self.store._legacy_pointer_path(self.CWD).exists())

    def test_clear_last_clears_both_files_that_name_the_id(self) -> None:
        self.store.update_last(self.CWD, MID)
        self.assertTrue(self.store.clear_last(self.CWD, MID))
        # No lock is ever created for a 2.x name that is absent.
        self.assertFalse(Path(str(self.store._legacy_pointer_path(self.CWD)) + ".lock").exists())
        self.primary(MID)
        self.legacy(MID)
        self.assertTrue(self.store.clear_last(self.CWD, MID))
        self.assertIsNone(self.store.last(self.CWD))
        self.primary(OTHER)
        self.legacy(MID)
        self.assertTrue(self.store.clear_last(self.CWD, MID))
        self.assertEqual(self.store.last(self.CWD), OTHER)
        self.assertFalse(self.store.clear_last(self.CWD, MID))

    def test_restore_pointer_bytes_is_compare_and_restore(self) -> None:
        prior = self.primary(OTHER).read_bytes()
        self.store.update_last(self.CWD, MID)
        self.assertTrue(self.store.restore_pointer_bytes(self.CWD, MID, prior))
        self.assertEqual(self.store._pointer_path(self.CWD).read_bytes(), prior)
        self.assertFalse(self.store.restore_pointer_bytes(self.CWD, MID, None))  # names OTHER now
        self.store.update_last(self.CWD, MID)
        self.assertTrue(self.store.restore_pointer_bytes(self.CWD, MID, None))
        self.assertFalse(self.store._pointer_path(self.CWD).exists())

    def test_both_files_present_gate_on_their_own_payload(self) -> None:
        primary = self.primary(OTHER)  # A (newer)
        self.legacy(MID)  # B
        primary_bytes = primary.read_bytes()
        self.assertFalse(self.store.restore_pointer_bytes(self.CWD, MID, b"{}\n"))
        self.assertEqual(primary.read_bytes(), primary_bytes)
        self.store.relink_runtime(MID, observed_runtime_id=RUNTIME, cwd="/moved")
        self.assertEqual(primary.read_bytes(), primary_bytes)
        self.assertFalse(self.store._legacy_pointer_path(self.CWD).exists())
        self.assertEqual(self.store.last("/moved"), MID)
        self.assertEqual(self.store.last(self.CWD), OTHER)


# ---------------------------------------------------------------------- forget


class ForgetTests(V4TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.marker4()

    def pointer_backup(self, name: str, session_id: str) -> Path:
        path = self.store.pointers_dir / f"{name}{sessions.POINTER_BACKUP_SUFFIX}"
        state.atomic_write(path, strict_json.canonical_file_bytes({"cwd": "/x", "session_id": session_id}))
        return path

    def test_forget_removes_backups_seen_markers_and_the_lineup_log(self) -> None:
        record = _v4()
        record["runtime_session_id"] = RUNTIME
        record["runtime_aliases"] = [{"session_id": ALIAS, "source": "clear", "observed_at": NOW}]
        self.store.save(record)
        self.store.update_last(record["cwd"], MID)
        backup = self.store.backup_path(MID)
        state.atomic_write(backup, strict_json.canonical_file_bytes(_v3_managed()))
        mine = [self.pointer_backup("a" * 32 + ".json", MID),
                self.pointer_backup("b" * 32 + ".ordinary.json", MID)]
        theirs = self.pointer_backup("c" * 32 + ".json", OTHER)
        notice = state.ensure_private_dir(self.root / sessions.NOTICE_DIR)
        for runtime_id in (RUNTIME, ALIAS, THIRD):
            state.atomic_write(notice / f"{runtime_id}.seen", b"1 abc\n")
        lineup_log.append(self.root, MID, {"event": "apply", "lineup_gen": 1})
        self.assertEqual(self.store.forget_session(MID), (True, False))
        self.assertFalse(self.store.exists(MID))
        self.assertFalse(backup.exists())
        for path in mine:
            self.assertFalse(path.exists(), path)
        self.assertTrue(theirs.exists())
        self.assertFalse((notice / f"{RUNTIME}.seen").exists())
        self.assertFalse((notice / f"{ALIAS}.seen").exists())
        self.assertTrue((notice / f"{THIRD}.seen").exists())
        self.assertEqual(lineup_log.log_ids(self.root), [])
        self.assertIsNone(self.store.last(record["cwd"]))

    def test_corrupt_record_branch_removes_the_backup(self) -> None:
        state.atomic_write(self.store.sessions_dir / f"{MID}.json", b"{not json")
        backup = self.store.backup_path(MID)
        state.atomic_write(backup, strict_json.canonical_file_bytes(_v3_managed()))
        mine = self.pointer_backup("d" * 32 + ".json", MID)
        self.assertEqual(self.store.forget_session(MID), (True, False))
        self.assertFalse(backup.exists())
        self.assertFalse(mine.exists())

    def test_backups_are_never_records(self) -> None:
        self.store.save(_v4())
        state.atomic_write(self.store.backup_path(MID), strict_json.canonical_file_bytes(_v3_managed()))
        self.assertEqual([p.name for p in self.store.scan_uuid_records()], [f"{MID}.json"])
        self.assertEqual(self.store.backup_path(MID).name, f"{MID}.v3.json")


# -------------------------------------------------------- title and lead switch


class TitleTests(V4TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.marker4()
        self.store.save(_v4())

    def test_set_and_clear(self) -> None:
        before = self.disk()
        self.store.set_title(MID, "  Parser refactor  ")
        saved = self.disk()
        self.assertEqual(saved["title"], "Parser refactor")
        self.assertEqual(saved["mutation_token"], before["mutation_token"])
        self.assertEqual(saved["applied_hash"], before["applied_hash"])
        self.store.set_title(MID, None)
        self.assertEqual(self.disk(), before)

    def test_refusals(self) -> None:
        for title in ("a\nb", "", "   ", "x" * 81):
            with self.subTest(title=title):
                with self.assertRaises(SessionError):
                    self.store.set_title(MID, title)
        _v3.save_any(self.store, _v3_managed(OTHER))
        with self.assertRaises(sessions.LegacyRecordError):
            self.store.set_title(OTHER, "t")
        exclusive = sessions.migration_lock(self.root)
        self.assertTrue(exclusive.acquire(blocking=False))
        try:
            with self.assertRaises(sessions.MigrationBusyError):
                self.store.set_title(MID, "t")
        finally:
            exclusive.release()


class RelinkMigrationGuardTests(V4TestCase):
    def test_relink_refuses_under_an_exclusive_migration_lock_and_writes_nothing(self) -> None:
        self.marker4()
        self.store.save(_v4())
        self.store.update_last("/project/path", MID)
        before = self.store.read_record_bytes(MID)
        pointer = self.store._pointer_path("/project/path")
        pointer_before = pointer.read_bytes()
        exclusive = sessions.migration_lock(self.root)
        self.assertTrue(exclusive.acquire(blocking=False))
        try:
            for cwd in (None, "/moved/here"):
                with self.subTest(cwd=cwd):
                    with self.assertRaises(sessions.MigrationBusyError) as caught:
                        self.store.relink_runtime(MID, observed_runtime_id=RUNTIME, cwd=cwd)
                    self.assertEqual(str(caught.exception), sessions.MIGRATION_BUSY_TEXT)
                    self.assertEqual(self.store.read_record_bytes(MID), before)
                    self.assertEqual(pointer.read_bytes(), pointer_before)
                    self.assertFalse(self.store._pointer_path("/moved/here").exists())
        finally:
            exclusive.release()
        # the hold is released on return: a follow-up guard (doctor --repair's
        # converge) can take its own, and the exclusive lock is free again.
        self.store.relink_runtime(MID, observed_runtime_id=RUNTIME)
        self.assertEqual(self.disk()["runtime_session_id"], RUNTIME)
        with sessions.launcher_write_guard(self.root):
            pass
        again = sessions.migration_lock(self.root)
        self.assertTrue(again.acquire(blocking=False))
        again.release()


class LeadSwitchTests(V4TestCase):
    ROW = {"key": "opus5", "selector": "claude-multi-opus-5[1m]", "effort": None,
           "display": "Opus 5", "family": "anthropic"}

    def setUp(self) -> None:
        super().setUp()
        self.marker4()

    def switch(self, row=None, *, runtime=MID, epoch=1):
        return self.store.record_lead_switch(MID, observed_runtime_id=runtime,
                                             launch_epoch=epoch, row=row or self.ROW)

    def test_none_cases(self) -> None:
        self.store.save(_v4())
        before = self.store.read_record_bytes(MID)
        self.assertEqual(self.switch(epoch=0), "none")
        self.assertEqual(self.switch(runtime=FORK), "none")
        lead = _v4()["applied"]["lead"]
        self.assertEqual(self.switch({"key": lead["key"], "selector": lead["selector"],
                                      "effort": None}), "none")
        self.assertEqual(self.store.read_record_bytes(MID), before)
        self.store.save(_v4(generation=0, follow=False))
        self.assertEqual(self.switch(), "none")
        _v3.save_any(self.store, _v3_managed())
        self.assertEqual(self.switch(), "none")

    def test_pinned_record_switches_and_drops_lead_target(self) -> None:
        record = {**_v4(follow=False), "lead_target": {"key": "fable", "effort": "max",
                                                       "selector": "claude-fable-5[1m]"}}
        self.store.save(record)
        self.assertEqual(self.switch(), "switched")
        saved = self.disk()
        self.assertEqual(saved["applied"]["lead"]["key"], "opus5")
        self.assertEqual(saved["applied"]["lead"]["selector"], "claude-multi-opus-5[1m]")
        self.assertEqual(saved["applied"]["lead"]["effort"], "ultracode")  # null row effort keeps it
        self.assertIsNone(saved["applied"]["lead"]["generation"])
        self.assertEqual(saved["applied"]["lead"]["compaction"], record["applied"]["lead"]["compaction"])
        self.assertNotEqual(saved["applied_hash"], record["applied_hash"])
        self.assertEqual(saved["mutation_token"], record["mutation_token"])
        self.assertEqual(saved["scope_lead"], record["scope_lead"])
        self.assertEqual(saved["launch_fence"], record["launch_fence"])
        self.assertNotIn("lead_target", saved)
        self.assertFalse(saved["follow"])

    def test_following_record_is_pinned_unless_it_lands_on_lead_target(self) -> None:
        self.store.save(_v4())
        self.assertEqual(self.switch(), "pinned")
        saved = self.disk()
        self.assertFalse(saved["follow"])
        self.assertEqual(saved["profile"], "balanced")
        target = {"key": "opus5", "effort": "ultracode", "selector": "claude-multi-opus-5[1m]"}
        self.store.save({**_v4(), "lead_target": target})
        self.assertEqual(self.switch(), "switched")
        saved = self.disk()
        self.assertTrue(saved["follow"])
        self.assertNotIn("lead_target", saved)

    def test_identity_re_derived_when_the_evidence_was_this_model(self) -> None:
        self.store.save({**_v4(), "observed_model": "claude-multi-opus-5[1m]",
                         "identity_state": sessions.IDENTITY_REPAIR_NEEDED})
        self.switch()
        saved = self.disk()
        self.assertNotIn("observed_model", saved)
        self.assertEqual(saved["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.store.save({**_v4(), "observed_model": "claude-multi-opus-5[1m]",
                         "observed_cwd": "/elsewhere",
                         "identity_state": sessions.IDENTITY_REPAIR_NEEDED})
        self.switch()
        saved = self.disk()
        self.assertNotIn("observed_model", saved)
        self.assertEqual(saved["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)


# ------------------------------------------------------------- restore overlay


class RestoreOverlayTests(V4TestCase):
    def test_overlay(self) -> None:
        backup = _v3_managed()
        backup["launch_epoch"] = 5
        current = _v4(launch_epoch=3, cwd="/relinked")
        current["runtime_session_id"] = RUNTIME
        current["runtime_aliases"] = [{"session_id": MID, "source": "startup", "observed_at": NOW}]
        current["title"] = "dropped"
        merged = sessions.restore_overlay(backup, current)
        self.assertEqual(merged["version"], 3)
        self.assertEqual(merged["launch_epoch"], 5)
        self.assertEqual(merged["cwd"], "/relinked")
        self.assertEqual(merged["runtime_session_id"], RUNTIME)
        self.assertEqual(merged["runtime_aliases"], current["runtime_aliases"])
        self.assertEqual(merged["launcher_version"], "3.0.0")
        self.assertNotIn(merged["mutation_token"], (backup["mutation_token"], current["mutation_token"]))
        for key in ("title", "applied", "profile", "launch_fence", "scope_lead"):
            self.assertNotIn(key, merged)
        self.assertEqual(merged["snapshot"], backup["snapshot"])
        self.store._validate(merged, "restore")
        backup["launch_epoch"] = 1
        self.assertEqual(sessions.restore_overlay(backup, current)["launch_epoch"], 3)

    def test_legacy_backups_become_valid_v3(self) -> None:
        for backup in (_v1(), _v2()):
            with self.subTest(version=backup["version"]):
                merged = sessions.restore_overlay(backup, _v4())
                self.assertEqual(merged["version"], 3)
                self.assertEqual(merged["migrated_from_version"], backup["version"])
                self.store._validate(merged, "restore")


# -------------------------------------------------------------------- messages


class MessageTests(unittest.TestCase):
    def test_fork_adopt_command_table(self) -> None:
        base = f"claude-multi sessions link {FORK}"
        rows = (
            (_v4(), f"{base} --profile balanced"),
            (sessions.with_applied({**_v4(profile_name=None, follow=False)},
                                   _applied(agents={})), f"{base} --direct opus55"),
            (_v4(profile_name=None, follow=False), f"{base} --profile NAME"),
            (_v3_managed(), f"{base} --profile NAME"),
            (_v3_ordinary(), f"{base} --direct qwen38"),
        )
        for record, expected in rows:
            with self.subTest(expected=expected):
                self.assertEqual(sessions.fork_adopt_command(record, FORK), expected)

    def test_v4_pending_fork_message(self) -> None:
        record = {**_v4(), "pending_forks": [{"session_id": FORK, "observed_at": NOW}]}
        self.assertEqual(
            sessions.pending_fork_message(record),
            f"session {MID} has an unresolved native fork (runtime {FORK}); adopt it with "
            f"`claude-multi sessions link {FORK} --profile balanced`, or discard the marker "
            f"with `claude-multi sessions resolve-fork {MID} {FORK}` — the fork transcript "
            "is kept either way",
        )
        record["pending_forks"].append({"session_id": THIRD, "observed_at": NOW})
        self.assertIn(
            "adopt or discard each with `claude-multi sessions link <fork-uuid> "
            f"--profile NAME|--direct MODEL` / `claude-multi sessions resolve-fork {MID} "
            "<fork-uuid>`",
            sessions.pending_fork_message(record),
        )

    def test_v3_fork_text_keeps_its_2x_remedy_and_relink_text_is_unified(self) -> None:
        # pending_fork_message keeps the 2.x link flags for v3 until the
        # aliases land; relink_message's model-only branch is one text
        # for every record version (with the hook texts).
        record = {**_v3_managed(), "pending_forks": [{"session_id": FORK, "observed_at": NOW}]}
        self.assertIn("--composition default", sessions.pending_fork_message(record))
        ordinary = {**_v3_ordinary(), "observed_model": "x"}
        message = sessions.relink_message(ordinary)
        self.assertNotIn("claude-gateway -r", message)
        self.assertIn("observed model x differs from the recorded qwen38", message)
        self.assertIn(f"(`claude-multi -r {MID}`)", message)
        managed = {**_v3_managed(), "observed_model": "x"}
        self.assertIn(
            f"differs from the recorded {_SNAPSHOT['lead']['client_selector']}",
            sessions.relink_message(managed),
        )

    def test_v4_relink_message(self) -> None:
        record = {**_v4(), "observed_model": "claude-multi-opus-5[1m]"}
        base = f"claude-multi sessions relink-runtime {MID} {MID}"
        self.assertEqual(
            sessions.relink_message(record),
            f"session {MID} identity is repair-needed (observed model "
            f"claude-multi-opus-5[1m] differs from the recorded {LEAD_SELECTOR}); resume "
            f"through the launcher to reconcile the recorded lead (`claude-multi -r {MID}`), "
            f"or relink only if the runtime UUID itself changed: `{base}`",
        )
        migrated = sessions.with_applied(record, _applied(key="qwen38", selector=None))
        self.assertIn("differs from the recorded qwen38)", sessions.relink_message(migrated))
        self.assertIn("--cwd", sessions.relink_message({**record, "observed_cwd": "/else"}))


# ----------------------------------------------------------------- continuity


class ContinuityScanTests(V4TestCase):
    def test_scan_records_collects_v4_selectors(self) -> None:
        self.marker4()
        record = {**_v4(), "lead_target": {"key": "fable", "effort": "max",
                                           "selector": "claude-fable-5[1m]"}}
        record["scope_lead"] = {"key": "opus", "effort": "xhigh",
                                "selector": "claude-multi-opus-4-8[1m]"}
        self.store.save(record)
        scan = continuity.scan_records(self.root)
        for base in ("claude-multi-opus-5-5", "gpt-multi-sol-high", "claude-fable-5",
                     "claude-multi-opus-4-8"):
            with self.subTest(base=base):
                self.assertEqual(scan.refs.get(base), frozenset({MID}))
        self.assertEqual(scan.live, frozenset({MID}))


class ResolveHumanTests(V4TestCase):
    """The one resolver for a session a person names: an exact UUID, a
    unique prefix of 8+ characters or an exact name; never "the newest"."""

    FIRST = "abcdef01-1111-4111-8111-111111111111"
    SECOND = "abcdef01-2222-4222-8222-222222222222"
    THIRD_ID = "fedcba98-3333-4333-8333-333333333333"

    def setUp(self) -> None:
        super().setUp()
        self.marker4()
        self.records = {}
        for mid, name in ((self.FIRST, "alpha"), (self.SECOND, "beta"), (self.THIRD_ID, "alpha")):
            record = _v4(mid)
            record["runtime_session_id"] = mid if mid != self.THIRD_ID else RUNTIME
            self.store.save(record)
            self.records[mid] = (record, name)

    def names(self, record):
        return {self.records[record["managed_id"]][1]}

    def test_exact_ids_and_unique_prefixes(self) -> None:
        self.assertEqual(self.store.resolve_human(self.FIRST)["managed_id"], self.FIRST)
        self.assertEqual(self.store.resolve_human(RUNTIME)["managed_id"], self.THIRD_ID)
        self.assertEqual(self.store.resolve_human("fedcba98")["managed_id"], self.THIRD_ID)
        self.assertEqual(self.store.resolve_human("abcdef01-1")["managed_id"], self.FIRST)
        # A runtime id prefix resolves to its record, deduplicated by managed id.
        self.assertEqual(self.store.resolve_human(RUNTIME[:9])["managed_id"], self.THIRD_ID)

    def test_short_or_ambiguous_values_refuse(self) -> None:
        with self.assertRaisesRegex(SessionError, "at least 8 characters"):
            self.store.resolve_human("abcdef0")
        with self.assertRaises(sessions.AmbiguousSessionError) as caught:
            self.store.resolve_human("abcdef01")
        self.assertEqual(caught.exception.candidates, (self.FIRST, self.SECOND))
        self.assertIn(self.FIRST, caught.exception.remedy)
        with self.assertRaises(sessions.AmbiguousSessionError):
            self.store.resolve_human("alpha", names=self.names)
        self.assertEqual(self.store.resolve_human("beta", names=self.names)["managed_id"], self.SECOND)
        with self.assertRaisesRegex(SessionError, "no managed session matches"):
            self.store.resolve_human("gamma", names=self.names)

    def test_an_unreadable_record_that_may_match_refuses(self) -> None:
        unreadable = "abcdef02-4444-4444-8444-444444444444"
        state.atomic_write(self.store.sessions_dir / f"{unreadable}.json", b'{"version": 4, "broken": true}')
        with self.assertRaisesRegex(SessionError, f"session record {unreadable} is unreadable"):
            self.store.resolve_human("abcdef02")
        state.atomic_write(self.store.sessions_dir / f"{unreadable}.json", b'{"version": 4, "title": "beta"}')
        with self.assertRaisesRegex(SessionError, "unreadable"):
            self.store.resolve_human("beta", names=self.names)
        # An exact id keeps the existing ownership checks.
        self.assertEqual(self.store.resolve_human(self.FIRST)["managed_id"], self.FIRST)

    def test_case_and_json_escapes_never_bypass_an_unreadable_claim(self) -> None:
        owner = "0a0a0a0a-6666-4666-8666-666666666666"
        runtime_id = "abcabc12-7777-4777-8777-777777777777"
        record = _v4(owner)
        record["runtime_session_id"] = runtime_id
        self.store.save(record)
        # Without an unreadable record, an uppercase id or prefix is the same session.
        for value in ("ABCABC12", runtime_id.upper(), "FEDCBA98"):
            with self.subTest(readable=value):
                self.assertIn(self.store.resolve_human(value)["managed_id"], (owner, self.THIRD_ID))
        unreadable = self.store.sessions_dir / "12345678-5555-4555-8555-555555555555.json"
        escaped = "".join(f"\\u{ord(char):04x}" for char in runtime_id)
        claims = {
            "plain": b'{"version": 4, "runtime_session_id": "' + runtime_id.encode() + b'", "broken": true}',
            "escaped": b'{"version": 4, "runtime_aliases": [{"session_id": "' + escaped.encode() + b'"}]}',
            "escaped-upper": b'{"version": 4, "runtime_aliases": ["' + escaped.encode().upper().replace(b"\\U", b"\\u")
                             + b'"]}',
        }
        for label, raw in claims.items():
            state.atomic_write(unreadable, raw)
            for value in ("abcabc12", "ABCABC12", runtime_id, runtime_id.upper()):
                with self.subTest(claim=label, value=value):
                    with self.assertRaisesRegex(SessionError, "unreadable"):
                        self.store.resolve_human(value)
        # Text that cannot be decoded may be any session: fail closed.
        state.atomic_write(unreadable, b"\xff not json")
        with self.assertRaisesRegex(SessionError, "unreadable"):
            self.store.resolve_human("FEDCBA98")

    def test_the_candidate_list_is_bounded(self) -> None:
        error = sessions.AmbiguousSessionError("x", [f"{index:08x}-0000-4000-8000-000000000000" for index in range(20)])
        self.assertIn("and 12 more", str(error))
        self.assertEqual(len(error.candidates), 20)


if __name__ == "__main__":
    unittest.main()
