"""Tests for launcher-owned session state."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, sessions, state, strict_json
from claude_multi.sessions import SessionError
from _catalog import FIXTURE_ROOT
import _v3


CATALOG_ROOT = FIXTURE_ROOT
FIXED_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"


def _schema() -> dict:
    return strict_json.load(CATALOG_ROOT / "schemas" / "session.schema.json")


def _snapshot() -> dict:
    return _v3.snapshot_for(catalog.load_catalog(CATALOG_ROOT).docs)


def _record(snapshot, session_id=FIXED_ID, forked_from=None) -> dict:
    return _v3.make_record(
        session_id=session_id,
        cwd="/project/path",
        composition_name="default",
        snapshot=snapshot,
        catalog_version=1,
        catalog_hash="sha256:" + "0" * 64,
        launcher_version="2.0.0",
        forked_from=forked_from,
        now="2026-07-21T00:00:00Z",
    )



def _adopted_v4(managed_id: str, runtime_id: str, *, identity_state: str | None = None) -> dict:
    record = _link_record(managed_id)
    record["runtime_session_id"] = runtime_id
    if identity_state is not None:
        record["identity_state"] = identity_state
    return record


def _link_record(session_id: str) -> dict:
    """A v4 generation-0 record, what ``sessions link`` adopts."""

    from test_sessions_v4 import _v4

    return _v4(session_id, generation=0)


class SessionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-sessions-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(self._cleanup)
        self.store = sessions.SessionStore(self.root, _schema())
        self.snapshot = _snapshot()

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)


class StateMarkerTests(SessionTestCase):
    def _tree(self):
        return {
            str(path.relative_to(self.root)): (
                path.lstat().st_mode,
                os.readlink(path) if path.is_symlink() else
                path.read_bytes() if path.is_file() else None,
            )
            for path in self.root.rglob("*")
        }

    def test_missing_and_supported_marker_allow_locks_without_writing_marker(self):
        marker = self.root / sessions.STATE_MARKER
        self.assertEqual(sessions.check_state_marker(self.root), 3)
        self.store.lifecycle_lock(FIXED_ID)
        self.store.runtime_index_lock()
        self.assertFalse(marker.exists())
        # 4 is this launcher's own state version.
        for value in (b"0", b"2\n", b"3", b"3\n", b"4", b"4\n"):
            with self.subTest(value=value):
                state.atomic_write(marker, value)
                before = self._tree()
                self.assertLessEqual(sessions.check_state_marker(self.root), 4)
                self.store.lifecycle_lock(FIXED_ID)
                self.store.runtime_index_lock()
                self.assertEqual(self._tree(), before)

    def test_newer_and_malformed_markers_refuse_before_creating_locks(self):
        marker = self.root / sessions.STATE_MARKER
        for value in (b"5", b"5\n", b"99", b"", b"three", b"3\n\n", b" 3", b"+3", b"-1", b"3.0", b"\xff"):
            with self.subTest(value=value):
                state.atomic_write(marker, value)
                before = self._tree()
                for check in (
                    lambda: sessions.check_state_marker(self.root),
                    lambda: self.store.lifecycle_lock(FIXED_ID),
                    self.store.runtime_index_lock,
                ):
                    with self.assertRaises(sessions.StateMarkerError) as caught:
                        check()
                    self.assertIn("this launcher understands state version 4 or older", str(caught.exception))
                self.assertEqual(self._tree(), before)
                self.assertFalse((self.root / "locks").exists())
        state.atomic_write(marker, b"5\n")
        with self.assertRaises(sessions.StateMarkerError) as caught:
            sessions.check_state_marker(self.root)
        self.assertEqual(str(caught.exception),
                         "state belongs to a newer claude-multi (state version 5); "
                         "this launcher understands state version 4 or older. Fix: "
                         + sessions.StateMarkerError.NEWER_REMEDY)
        self.assertIsNone(caught.exception.remedy)
        self.assertEqual(caught.exception.version, 5)

    def test_symlink_dangling_directory_and_unreadable_markers_fail_closed(self):
        marker = self.root / sessions.STATE_MARKER
        target = self.root / "target"
        state.atomic_write(target, b"3\n")
        for destination in (target, self.root / "missing"):
            marker.symlink_to(destination)
            before = self._tree()
            with self.assertRaises(sessions.StateMarkerError):
                self.store.lifecycle_lock(FIXED_ID)
            self.assertEqual(self._tree(), before)
            marker.unlink()
        marker.mkdir()
        with self.assertRaises(sessions.StateMarkerError):
            sessions.check_state_marker(self.root)
        marker.rmdir()
        state.atomic_write(marker, b"3\n")
        with mock.patch.object(state, "read_private", side_effect=PermissionError("unreadable")):
            with self.assertRaises(sessions.StateMarkerError):
                self.store.runtime_index_lock()
        marker.chmod(0o644)
        with self.assertRaises(sessions.StateMarkerError):
            sessions.check_state_marker(self.root)

    def test_every_session_mutation_refuses_and_readers_still_work(self):
        record = _record(self.snapshot)
        _v3.save_any(self.store, record)
        self.store.update_last(record["cwd"], FIXED_ID)
        state.ensure_private_dir(self.root / "scopes" / FIXED_ID)
        state.atomic_write(self.root / "scopes" / FIXED_ID / "settings.json", b"scope bytes")
        record_bytes = self.store.read_record_bytes(FIXED_ID)
        state.atomic_write(self.root / sessions.STATE_MARKER, b"5\n")
        before = self._tree()
        operations = {
            "save": lambda: self.store.save(_link_record(OTHER_ID)),
            "restore record": lambda: self.store.restore_record_bytes(FIXED_ID, record_bytes),
            "update pointer": lambda: self.store.update_last(record["cwd"], FIXED_ID),
            "clear pointer": lambda: self.store.clear_last(record["cwd"], FIXED_ID),
            "restore pointer": lambda: self.store.restore_pointer_bytes(record["cwd"], FIXED_ID, None),
            "start": lambda: self.store.reconcile_runtime(FIXED_ID, observed_runtime_id=FIXED_ID, source="resume"),
            "end": lambda: self.store.record_session_end(FIXED_ID, observed_runtime_id=FIXED_ID, reason="exit"),
            "relink": lambda: self.store.relink_runtime(FIXED_ID, observed_runtime_id=OTHER_ID),
            "resolve fork": lambda: self.store.resolve_fork(FIXED_ID, OTHER_ID),
            "converge forks": lambda: self.store.converge_pending_forks(FIXED_ID),
            "forget": lambda: self.store.forget_session(FIXED_ID),
            "link": lambda: self.store.link(_link_record(OTHER_ID)),
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                with self.assertRaises(sessions.StateMarkerError):
                    operation()
                self.assertEqual(self._tree(), before)
        self.assertEqual(self.store.load(FIXED_ID), record)
        self.assertEqual(self.store.resolve(FIXED_ID), record)
        self.assertEqual(self.store.last(record["cwd"]), FIXED_ID)
        self.assertEqual(self._tree(), before)


class RecordTests(SessionTestCase):
    def test_save_load_roundtrip(self) -> None:
        record = _record(self.snapshot)
        path = _v3.save_any(self.store, record)
        self.assertEqual(
            stat.S_IMODE(os.lstat(path).st_mode), 0o600
        )
        loaded = self.store.load(FIXED_ID)
        self.assertEqual(loaded["managed_id"], FIXED_ID)
        self.assertEqual(loaded["runtime_session_id"], FIXED_ID)
        self.assertEqual(loaded["snapshot"], self.snapshot)
        self.assertEqual(loaded["forked_from"], None)

    def test_v3_record_fields_and_defaults(self) -> None:
        record = _record(self.snapshot)
        self.assertEqual(record["version"], 3)
        self.assertEqual(record["session_type"], sessions.SESSION_TYPE_MANAGED)
        self.assertEqual(record["identity_state"], sessions.IDENTITY_UNVERIFIED)
        self.assertEqual(record["mode"], "legacy")
        self.assertEqual(record["scope_generation"], 0)
        self.assertEqual(record["workflows"], "native")
        _v3.save_any(self.store, record)
        loaded = self.store.load(FIXED_ID)
        self.assertEqual(loaded["mode"], "legacy")
        self.assertEqual(loaded["scope_generation"], 0)
        self.assertEqual(loaded["workflows"], "native")

    def test_v2_durable_fields_roundtrip(self) -> None:
        record = _v3.make_record(
            session_id=FIXED_ID,
            cwd="/project/path",
            composition_name="default",
            snapshot=self.snapshot,
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.1.0",
            mode="durable",
            scope_generation=1,
            workflows="off",
            now="2026-07-21T00:00:00Z",
        )
        _v3.save_any(self.store, record)
        loaded = self.store.load(FIXED_ID)
        self.assertEqual(loaded["mode"], "durable")
        self.assertEqual(loaded["scope_generation"], 1)
        self.assertEqual(loaded["workflows"], "off")

    def test_v1_record_loads_with_defaults_untouched_on_disk(self) -> None:
        record = _record(self.snapshot)
        record["version"] = 1
        record["session_id"] = record.pop("managed_id")
        for key in (
            "runtime_session_id",
            "runtime_aliases",
            "session_type",
            "identity_state",
            "last_event_source",
            "last_seen_at",
            "pending_forks",
            "launch_epoch",
            "migrated_from_version",
            "mode",
            "scope_generation",
            "workflows",
        ):
            del record[key]
        _v3.save_any(self.store, record)
        on_disk = self.store.read_record_bytes(FIXED_ID)
        loaded = self.store.load(FIXED_ID)
        self.assertEqual(loaded["version"], 3)
        self.assertEqual(loaded["migrated_from_version"], 1)
        self.assertEqual(loaded["mode"], "legacy")
        self.assertEqual(loaded["scope_generation"], 0)
        self.assertEqual(loaded["workflows"], "native")
        # Load never rewrites the record.
        self.assertEqual(self.store.read_record_bytes(FIXED_ID), on_disk)

    def test_v2_record_missing_new_fields_rejected(self) -> None:
        record = _record(self.snapshot)
        record["version"] = 2
        record["session_id"] = record.pop("managed_id")
        for key in (
            "runtime_session_id",
            "runtime_aliases",
            "session_type",
            "identity_state",
            "last_event_source",
            "last_seen_at",
            "pending_forks",
            "launch_epoch",
            "migrated_from_version",
        ):
            del record[key]
        del record["mode"]
        state.atomic_write(
            self.store._record_path(FIXED_ID),
            strict_json.canonical_file_bytes(record),
        )
        with self.assertRaisesRegex(SessionError, "oneOf variant"):
            self.store.load(FIXED_ID)

    def test_record_schema_valid(self) -> None:
        from claude_multi import validate

        record = _record(self.snapshot, forked_from=OTHER_ID)
        self.assertEqual(validate.validate(record, _schema()), [])

    def test_ordinary_record_nullable_profile_and_subagents_flag(self) -> None:
        from claude_multi import validate

        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=OTHER_ID,
            cwd="/project/path",
            model="gpt55",
            context_profile=None,
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.2.0",
            no_subagents=True,
        )
        self.assertIsNone(record["context_profile"])
        self.assertTrue(record["no_subagents"])
        self.assertEqual(validate.validate(record, _schema()), [])
        _v3.save_any(self.store, record)
        self.assertTrue(self.store.load(FIXED_ID)["no_subagents"])

    def test_ordinary_record_emits_subagents_false_by_default(self) -> None:
        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=OTHER_ID,
            cwd="/project/path",
            model="sol",
            context_profile="large",
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.2.0",
        )
        self.assertFalse(record["no_subagents"])

    def test_single_model_reconcile_never_repins_across_models(self) -> None:
        # Null-profile records share no fence: observing a different
        # single-model selector must stay an observation, never overwrite
        # ordinary_model (the live scope still fences the recorded model).
        current = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd="/project/path",
            model="gpt55",
            context_profile=None,
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.2.0",
        )
        updated = sessions.reconcile_runtime_record(
            current,
            observed_runtime_id=OTHER_ID,
            source="resume",
            cwd="/project/path",
            model="other-single",
            model_profile=None,
            observed_model="other-single",
            now="2026-09-06T00:00:00Z",
        )
        self.assertEqual(updated["ordinary_model"], "gpt55")
        self.assertEqual(updated["observed_model"], "other-single")

    def test_single_model_reconcile_adopts_same_model(self) -> None:
        current = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd="/project/path",
            model="gpt55",
            context_profile=None,
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.2.0",
        )
        updated = sessions.reconcile_runtime_record(
            current,
            observed_runtime_id=OTHER_ID,
            source="resume",
            cwd="/project/path",
            model="gpt55",
            model_profile=None,
            observed_model="gpt-multi-gpt55-high",
            now="2026-09-06T00:00:00Z",
        )
        self.assertEqual(updated["ordinary_model"], "gpt55")
        self.assertNotIn("observed_model", updated)

    def test_published_schema_rejects_incomplete_or_cross_type_v3_records(self) -> None:
        from claude_multi import validate

        incomplete = {
            "version": 3,
            "cwd": "/project/path",
            "catalog_version": 1,
            "catalog_hash": "sha256:" + "0" * 64,
            "launcher_version": "2.2.0",
            "created_at": "2026-07-21T00:00:00Z",
            "forked_from": None,
        }
        self.assertTrue(validate.validate(incomplete, _schema()))
        ordinary = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=OTHER_ID,
            cwd="/project/path",
            model="qwen38",
            context_profile="large",
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.2.0",
        )
        ordinary["composition_name"] = "default"
        self.assertTrue(validate.validate(ordinary, _schema()))

    def test_corrupt_record_reported(self) -> None:
        path = self.store._record_path(FIXED_ID)
        state.atomic_write(path, b"{not json")
        with self.assertRaisesRegex(SessionError, "corrupt session record"):
            self.store.load(FIXED_ID)

    def test_stale_record_missing(self) -> None:
        with self.assertRaises(SessionError):
            self.store.load(FIXED_ID)

    def test_embedded_session_id_must_match_record_path(self) -> None:
        path = self.store.sessions_dir / f"{FIXED_ID}.json"
        state.atomic_write(
            path,
            strict_json.canonical_file_bytes(
                _record(self.snapshot, session_id=OTHER_ID)
            ),
        )
        with self.assertRaisesRegex(SessionError, "does not match its record path"):
            self.store.load(FIXED_ID)

    def test_schema_violating_record_rejected_on_save(self) -> None:
        record = _record(self.snapshot)
        record["snapshot"]["scalar_context_tokens"] = "not-a-number"
        with self.assertRaises(SessionError):
            self.store.save(record)

    def test_invalid_mode_rejected_on_save(self) -> None:
        record = _record(self.snapshot)
        record["mode"] = "ephemeral"
        with self.assertRaises(SessionError):
            self.store.save(record)

    def test_fork_relationship_recorded(self) -> None:
        record = _record(self.snapshot, session_id=OTHER_ID, forked_from=FIXED_ID)
        _v3.save_any(self.store, record)
        self.assertEqual(self.store.load(OTHER_ID)["forked_from"], FIXED_ID)

    def test_no_secret_fields_in_record(self) -> None:
        record = _record(self.snapshot)
        blob = strict_json.canonical_bytes(record).decode("utf-8")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", blob)
        self.assertNotIn("api-key", blob)


class RecordBytesTests(SessionTestCase):
    def test_read_record_bytes_absent(self) -> None:
        self.assertIsNone(self.store.read_record_bytes(FIXED_ID))

    def test_read_restore_roundtrip_exact_bytes(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        original = self.store.read_record_bytes(FIXED_ID)
        self.assertIsNotNone(original)
        replacement = _v3.make_record(
            session_id=FIXED_ID,
            cwd="/project/path",
            composition_name="default",
            snapshot=self.snapshot,
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.1.0",
            mode="durable",
            scope_generation=2,
            now="2026-07-22T00:00:00Z",
        )
        _v3.save_any(self.store, replacement)
        self.assertNotEqual(self.store.read_record_bytes(FIXED_ID), original)
        self.store.restore_record_bytes(FIXED_ID, original)
        self.assertEqual(self.store.read_record_bytes(FIXED_ID), original)
        restored = self.store.load(FIXED_ID)
        self.assertEqual(restored["mode"], "legacy")

    def test_record_path_helpers_reject_non_uuid(self) -> None:
        with self.assertRaises(SessionError):
            self.store.exists("../escape")
        with self.assertRaises(SessionError):
            self.store.read_record_bytes("../escape")
        with self.assertRaises(SessionError):
            self.store.restore_record_bytes("../escape", b"{}\n")


class MintTests(SessionTestCase):
    def test_new_id_collision_retry(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        sequence = iter([FIXED_ID, OTHER_ID])
        with mock.patch.object(sessions.uuid, "uuid4", side_effect=lambda: sessions.uuid.UUID(next(sequence))):
            minted = self.store.new_id()
        self.assertEqual(minted, OTHER_ID)

    def test_new_id_is_uuid4(self) -> None:
        minted = self.store.new_id()
        self.assertRegex(minted, sessions.UUID4)


class LinkTests(SessionTestCase):
    """``SessionStore.link`` writes a v4 gen-0 record (save is v4-only)."""

    def setUp(self) -> None:
        super().setUp()
        sessions.write_state_marker(self.root)

    def test_link_adopts_unmanaged_session(self) -> None:
        record = _link_record(OTHER_ID)
        self.store.link(record)
        self.assertEqual(self.store.load(OTHER_ID)["profile"], "balanced")
        self.assertEqual(self.store.load(OTHER_ID)["lineup_generation"], 0)

    def test_link_rejects_existing(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        with self.assertRaisesRegex(SessionError, "already managed"):
            self.store.link(_link_record(FIXED_ID))

    def test_link_rejects_non_uuid(self) -> None:
        record = {**_link_record(OTHER_ID), "managed_id": "not-a-uuid"}
        with self.assertRaisesRegex(SessionError, "not a UUID"):
            self.store.link(record)

    def test_link_rechecks_existence_under_the_lifecycle_lock(self) -> None:
        # The exists-recheck (and therefore the check-then-save pair) runs
        # after the session's lifecycle lock is acquired.
        acquisitions: list[bool] = []
        real_exists = self.store.exists

        def spying_exists(session_id: str) -> bool:
            probe = self.store.lifecycle_lock(session_id)
            acquired = probe.acquire(blocking=False)
            if acquired:
                probe.release()
            acquisitions.append(acquired)
            return real_exists(session_id)

        with mock.patch.object(self.store, "exists", spying_exists):
            self.store.link(_link_record(OTHER_ID))
        # A competing non-blocking acquire on the same lock file had to fail,
        # proving exists ran while link held the lifecycle lock.
        self.assertTrue(acquisitions)
        self.assertTrue(all(acquired is False for acquired in acquisitions))
        self.assertTrue(self.store.exists(OTHER_ID))

    def test_link_waits_for_a_held_lifecycle_lock(self) -> None:
        # A concurrent holder of the lifecycle lock serializes link.
        lock = self.store.lifecycle_lock(OTHER_ID)
        self.assertTrue(lock.acquire(blocking=False))
        done: list[bool] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                self.store.link(_link_record(OTHER_ID))
                done.append(True)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=0.5)
        try:
            self.assertTrue(thread.is_alive())
            self.assertEqual(done, [])
            self.assertFalse(self.store.exists(OTHER_ID))
        finally:
            lock.release()
            thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(done, [True])
        self.assertTrue(self.store.exists(OTHER_ID))

    def test_link_waits_for_pointer_and_commits_record_with_pointer(self) -> None:
        record = _link_record(OTHER_ID)
        pointer = self.store._pointer_path(record["cwd"])
        pointer_lock = state.FileLock(pointer)
        self.assertTrue(pointer_lock.acquire(blocking=False))
        done: list[Path] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                done.append(self.store.link(record))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=0.2)
        try:
            self.assertTrue(thread.is_alive())
            self.assertFalse(self.store.exists(OTHER_ID))
        finally:
            pointer_lock.release()
            thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(len(done), 1)
        self.assertTrue(self.store.exists(OTHER_ID))
        self.assertEqual(self.store.last(record["cwd"]), OTHER_ID)


class IdentityReconciliationTests(SessionTestCase):
    def test_runtime_drift_becomes_authoritative_and_old_id_is_alias(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        updated = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="compact",
            cwd="/project/path",
            now="2026-07-22T00:00:00Z",
        )
        self.assertEqual(updated["managed_id"], FIXED_ID)
        self.assertEqual(updated["runtime_session_id"], OTHER_ID)
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertEqual(
            [item["session_id"] for item in updated["runtime_aliases"]],
            [FIXED_ID],
        )
        self.assertEqual(self.store.resolve(OTHER_ID)["managed_id"], FIXED_ID)
        self.assertEqual(self.store.resolve(FIXED_ID)["managed_id"], FIXED_ID)

    def test_repeated_start_is_idempotent(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        first = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="resume",
            cwd="/project/path",
            now="2026-07-22T00:00:00Z",
        )
        second = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="resume",
            cwd="/project/path",
            now="2026-07-22T00:00:01Z",
        )
        self.assertEqual(first["runtime_aliases"], second["runtime_aliases"])
        self.assertEqual(second["runtime_session_id"], OTHER_ID)

    def test_managed_fork_does_not_replace_parent_runtime(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        updated = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd="/project/path",
            now="2026-07-22T00:00:00Z",
        )
        self.assertEqual(updated["runtime_session_id"], FIXED_ID)
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_PENDING_FORK)
        self.assertEqual(updated["pending_forks"][0]["session_id"], OTHER_ID)

    def test_cwd_mismatch_marks_repair_needed_without_overwriting_origin(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        updated = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=FIXED_ID,
            source="startup",
            cwd="/wrong/project",
            now="2026-07-22T00:00:00Z",
        )
        self.assertEqual(updated["cwd"], "/project/path")
        self.assertEqual(updated["observed_cwd"], "/wrong/project")
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)

    def test_session_end_is_advisory_and_never_retargets_resume(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="resume",
            cwd="/project/path",
            now="2026-07-22T00:00:00Z",
        )
        before = self.store.read_record_bytes(FIXED_ID)
        # An end from a historical runtime (now an
        # alias) is a no-op; only the current runtime's end is recorded.
        stale = self.store.record_session_end(
            FIXED_ID,
            observed_runtime_id=FIXED_ID,
            reason="other",
            now="2026-07-22T00:00:01Z",
        )
        self.assertEqual(self.store.read_record_bytes(FIXED_ID), before)
        self.assertNotIn("last_end_reason", stale)
        ended = self.store.record_session_end(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            reason="other",
            now="2026-07-22T00:00:02Z",
        )
        self.assertEqual(ended["runtime_session_id"], OTHER_ID)
        self.assertEqual(ended["last_end_reason"], "other")
        self.assertEqual(ended["last_event_source"], "end")
        self.assertEqual(ended["identity_state"], sessions.IDENTITY_AUTHORITATIVE)

    def test_delayed_unseen_start_with_older_epoch_cannot_retarget_authority(self) -> None:
        delayed_id = "33333333-3333-4333-8333-333333333333"
        record = _record(self.snapshot)
        record["launch_epoch"] = 1
        _v3.save_any(self.store, record)
        newest = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="resume",
            cwd="/project/path",
            launch_epoch=3,
            now="2026-07-22T00:00:00Z",
        )
        delayed = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=delayed_id,
            source="resume",
            cwd="/project/path",
            launch_epoch=2,
            now="2026-07-22T00:00:01Z",
        )
        self.assertEqual(delayed, newest)
        self.assertEqual(delayed["runtime_session_id"], OTHER_ID)
        self.assertEqual(delayed["launch_epoch"], 3)
        later = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=delayed_id,
            source="resume",
            cwd="/project/path",
            launch_epoch=4,
            now="2026-07-22T00:00:02Z",
        )
        self.assertEqual(later["runtime_session_id"], delayed_id)
        self.assertEqual(later["launch_epoch"], 4)

    def test_delayed_start_from_runtime_alias_cannot_retarget_authority(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        current = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="resume",
            cwd="/project/path",
            now="2026-07-22T00:00:00Z",
        )
        delayed = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=FIXED_ID,
            source="compact",
            cwd="/wrong/project",
            model="gpt-multi-sol-high",
            observed_model="gpt-multi-sol-high",
            now="2026-07-22T00:00:01Z",
        )
        self.assertEqual(delayed, current)
        self.assertEqual(delayed["runtime_session_id"], OTHER_ID)
        self.assertNotIn("observed_cwd", delayed)
        self.assertNotIn("observed_model", delayed)

    def test_carry_lifecycle_state_overlays_current_without_mutation(self) -> None:
        target = _record(self.snapshot)
        target["scope_generation"] = 2
        target["runtime_session_id"] = FIXED_ID
        target["runtime_aliases"] = [{"session_id": OTHER_ID}]
        target["observed_model"] = "stale"
        current = _record(self.snapshot)
        current.update(
            {
                "runtime_session_id": OTHER_ID,
                "runtime_aliases": [
                    {
                        "session_id": FIXED_ID,
                        "source": "compact",
                        "observed_at": "2026-07-22T00:00:00Z",
                    }
                ],
                "identity_state": sessions.IDENTITY_REPAIR_NEEDED,
                "last_event_source": "compact",
                "last_seen_at": "2026-07-22T00:00:00Z",
                "pending_forks": [],
                "observed_cwd": "/wrong/project",
                "last_end_reason": "other",
            }
        )
        target_before = strict_json.canonical_bytes(target)
        current_before = strict_json.canonical_bytes(current)
        merged = sessions.carry_lifecycle_state(target, current)
        self.assertEqual(merged["scope_generation"], 2)
        for key in sessions._LIFECYCLE_FIELDS:
            self.assertEqual(merged.get(key), current.get(key))
        self.assertNotIn("observed_model", merged)
        merged["runtime_aliases"][0]["source"] = "changed"
        self.assertEqual(current["runtime_aliases"][0]["source"], "compact")
        self.assertEqual(strict_json.canonical_bytes(target), target_before)
        self.assertEqual(strict_json.canonical_bytes(current), current_before)

    def test_pending_fork_survives_later_parent_start(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        first = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd="/project/path",
            now="2026-07-22T00:00:00Z",
        )
        second = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=FIXED_ID,
            source="resume",
            cwd="/project/path",
            now="2026-07-22T00:00:01Z",
        )
        self.assertEqual(first["pending_forks"], second["pending_forks"])
        self.assertEqual(second["identity_state"], sessions.IDENTITY_PENDING_FORK)

    def test_fork_does_not_downgrade_existing_repair_state(self) -> None:
        record = _record(self.snapshot)
        record["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        record["observed_cwd"] = "/wrong/project"
        _v3.save_any(self.store, record)
        updated = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd="/wrong/project",
            now="2026-07-22T00:00:00Z",
        )
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)
        self.assertEqual(updated["pending_forks"][0]["session_id"], OTHER_ID)

    def test_linking_fork_clears_parent_pending_state(self) -> None:
        adopted_id = "33333333-3333-4333-8333-333333333333"
        _v3.save_any(self.store, _record(self.snapshot))
        self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd="/project/path",
            now="2026-07-22T00:00:00Z",
        )
        # Link adopts as a v4 generation-0 record (save is v4-only).
        adopted = _adopted_v4(adopted_id, OTHER_ID, identity_state=sessions.IDENTITY_AUTHORITATIVE)
        sessions.write_state_marker(self.root)
        self.store.link(adopted)
        parent = self.store.load(FIXED_ID)
        self.assertEqual(parent["pending_forks"], [])
        self.assertEqual(parent["identity_state"], sessions.IDENTITY_AUTHORITATIVE)

    def test_link_rolls_back_parent_if_adopted_record_save_fails(self) -> None:
        adopted_id = "33333333-3333-4333-8333-333333333333"
        _v3.save_any(self.store, _record(self.snapshot))
        self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd="/project/path",
        )
        parent_before = self.store.read_record_bytes(FIXED_ID)
        # Link adopts as a v4 generation-0 record (save is v4-only).
        adopted = _adopted_v4(adopted_id, OTHER_ID, identity_state=sessions.IDENTITY_AUTHORITATIVE)
        sessions.write_state_marker(self.root)
        real_save = self.store.save

        def fail_adopted(record):
            if sessions.managed_id(record) == adopted_id:
                raise OSError("injected adopted-record failure")
            return real_save(record)

        with mock.patch.object(self.store, "save", side_effect=fail_adopted):
            with self.assertRaisesRegex(OSError, "injected"):
                self.store.link(adopted)
        self.assertFalse(self.store.exists(adopted_id))
        self.assertEqual(self.store.read_record_bytes(FIXED_ID), parent_before)

    def test_stale_fork_event_after_adoption_is_ignored(self) -> None:
        adopted_id = "33333333-3333-4333-8333-333333333333"
        parent = _record(self.snapshot)
        parent["identity_state"] = sessions.IDENTITY_AUTHORITATIVE
        _v3.save_any(self.store, parent)
        # Link adopts as a v4 generation-0 record (save is v4-only).
        adopted = _adopted_v4(adopted_id, OTHER_ID, identity_state=sessions.IDENTITY_AUTHORITATIVE)
        sessions.write_state_marker(self.root)
        self.store.link(adopted)
        replayed = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd="/project/path",
        )
        self.assertEqual(replayed["pending_forks"], [])
        self.assertEqual(replayed["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertEqual(self.store.resolve(OTHER_ID)["managed_id"], adopted_id)

    def test_link_fails_closed_on_unparseable_record_without_partial_adoption(self) -> None:
        adopted_id = "33333333-3333-4333-8333-333333333333"
        corrupt_id = "44444444-4444-4444-8444-444444444444"
        parent = _record(self.snapshot)
        parent["identity_state"] = sessions.IDENTITY_AUTHORITATIVE
        _v3.save_any(self.store, parent)
        self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd="/project/path",
        )
        state.atomic_write(self.store._record_path(corrupt_id), b"{broken")
        # Link adopts as a v4 generation-0 record (save is v4-only).
        adopted = _adopted_v4(adopted_id, OTHER_ID, identity_state=sessions.IDENTITY_AUTHORITATIVE)
        sessions.write_state_marker(self.root)
        with self.assertRaisesRegex(SessionError, "cannot determine runtime ownership"):
            self.store.link(adopted)
        self.assertFalse(self.store.exists(adopted_id))
        self.assertEqual(
            self.store.load(FIXED_ID)["pending_forks"][0]["session_id"], OTHER_ID
        )

    def test_link_refuses_runtime_owned_by_schema_invalid_record(self) -> None:
        adopted_id = "33333333-3333-4333-8333-333333333333"
        owner = _v3.make_record(
            managed_id=FIXED_ID,
            runtime_session_id=OTHER_ID,
            cwd="/project/path",
            composition_name="default",
            snapshot=self.snapshot,
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.2.0",
            identity_state=sessions.IDENTITY_AUTHORITATIVE,
        )
        _v3.save_any(self.store, owner)
        invalid_owner = {**owner, "identity_state": "invalid-state"}
        state.atomic_write(
            self.store._record_path(FIXED_ID),
            strict_json.canonical_file_bytes(invalid_owner),
        )
        # Link adopts as a v4 generation-0 record (save is v4-only).
        adopted = _adopted_v4(adopted_id, OTHER_ID, identity_state=sessions.IDENTITY_AUTHORITATIVE)
        sessions.write_state_marker(self.root)

        with self.assertRaisesRegex(
            SessionError, "unreadable session record .* may claim runtime session"
        ):
            self.store.link(adopted)
        self.assertFalse(self.store.exists(adopted_id))

        state.atomic_write(
            self.store._record_path(FIXED_ID),
            strict_json.canonical_file_bytes(owner),
        )
        self.assertEqual(self.store.resolve(OTHER_ID)["managed_id"], FIXED_ID)

    def test_link_refuses_unicode_escaped_owner_in_duplicate_key_record(self) -> None:
        adopted_id = "33333333-3333-4333-8333-333333333333"
        corrupt_id = "44444444-4444-4444-8444-444444444444"
        owner = _v3.make_record(
            managed_id=corrupt_id,
            runtime_session_id=OTHER_ID,
            cwd="/project/path",
            composition_name="default",
            snapshot=self.snapshot,
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.2.0",
            identity_state=sessions.IDENTITY_AUTHORITATIVE,
        )
        raw = strict_json.canonical_file_bytes(owner).decode("utf-8")
        needle = f'"runtime_session_id":"{OTHER_ID}"'
        escaped = OTHER_ID.replace("2", "\\" + "u0032")
        malformed = raw.replace(
            needle,
            f'"runtime_session_id":"{FIXED_ID}",'
            f'"runtime_session_id":"{escaped}"',
            1,
        ).encode("utf-8")
        self.assertNotIn(OTHER_ID.encode("ascii"), malformed)
        state.atomic_write(self.store._record_path(corrupt_id), malformed)
        # Link adopts as a v4 generation-0 record (save is v4-only).
        adopted = _adopted_v4(adopted_id, OTHER_ID, identity_state=sessions.IDENTITY_AUTHORITATIVE)
        sessions.write_state_marker(self.root)

        with self.assertRaisesRegex(
            SessionError, "unreadable session record .* may claim runtime session"
        ):
            self.store.link(adopted)
        self.assertFalse(self.store.exists(adopted_id))

    def test_pending_fork_end_remains_advisory_and_adoption_heals_parent(self) -> None:
        adopted_id = "33333333-3333-4333-8333-333333333333"
        parent = _record(self.snapshot)
        parent["identity_state"] = sessions.IDENTITY_AUTHORITATIVE
        _v3.save_any(self.store, parent)
        self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd="/project/path",
        )
        ended = self.store.record_session_end(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            reason="other",
        )
        self.assertEqual(ended["identity_state"], sessions.IDENTITY_PENDING_FORK)
        # Link adopts as a v4 generation-0 record (save is v4-only).
        adopted = _adopted_v4(adopted_id, OTHER_ID, identity_state=sessions.IDENTITY_AUTHORITATIVE)
        sessions.write_state_marker(self.root)
        self.store.link(adopted)
        healed = self.store.load(FIXED_ID)
        self.assertEqual(healed["pending_forks"], [])
        self.assertEqual(healed["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertEqual(healed["runtime_session_id"], FIXED_ID)


    def test_forget_session_waits_for_lifecycle_lock(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        lock = self.store.lifecycle_lock(FIXED_ID)
        self.assertTrue(lock.acquire(blocking=False))
        done: list[tuple[bool, bool]] = []
        thread = threading.Thread(
            target=lambda: done.append(self.store.forget_session(FIXED_ID))
        )
        thread.start()
        thread.join(timeout=0.2)
        self.assertTrue(thread.is_alive())
        self.assertTrue(self.store.exists(FIXED_ID))
        lock.release()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(done[0][0], True)
        self.assertFalse(self.store.exists(FIXED_ID))

    def test_forget_scope_failure_preserves_record_authority(self) -> None:
        from claude_multi import scope

        _v3.save_any(self.store, _record(self.snapshot))
        live = scope.scope_dir(self.store.root, FIXED_ID)
        outside = self.root / "outside"
        outside.mkdir()
        live.parent.mkdir(parents=True, exist_ok=True)
        live.symlink_to(outside)
        with self.assertRaises(state.StateError):
            self.store.forget_session(FIXED_ID)
        self.assertTrue(self.store.exists(FIXED_ID))
        self.assertTrue(live.is_symlink())

    def test_forget_waits_for_contended_pointer_cleanup(self) -> None:
        record = _record(self.snapshot)
        _v3.save_any(self.store, record)
        self.store.update_last(record["cwd"], FIXED_ID)
        pointer = self.store._pointer_path(record["cwd"])
        pointer_lock = state.FileLock(pointer)
        self.assertTrue(pointer_lock.acquire(blocking=False))
        done: list[tuple[bool, bool]] = []
        thread = threading.Thread(
            target=lambda: done.append(self.store.forget_session(FIXED_ID))
        )
        thread.start()
        thread.join(timeout=0.2)
        self.assertTrue(thread.is_alive())
        self.assertTrue(self.store.exists(FIXED_ID))
        pointer_lock.release()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(done[0][0], True)
        self.assertIsNone(self.store.last(record["cwd"]))

    def test_forget_removes_transition_previous_scope(self) -> None:
        record = _record(self.snapshot)
        _v3.save_any(self.store, record)
        scopes_root = state.ensure_private_dir(self.store.root / "scopes")
        previous = scopes_root / f".{FIXED_ID}.prev"
        state.ensure_private_dir(previous)
        state.atomic_write(previous / "settings.json", b"{}\n")
        removed, scope_removed = self.store.forget_session(FIXED_ID)
        self.assertTrue(removed)
        self.assertTrue(scope_removed)
        self.assertFalse(previous.exists())
        self.assertFalse(self.store.exists(FIXED_ID))

    def test_failed_link_releases_runtime_index_lock(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        with self.assertRaises(SessionError):
            self.store.link(_record(self.snapshot))
        lock = self.store.runtime_index_lock()
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_ordinary_record_has_no_composition_payload(self) -> None:
        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=OTHER_ID,
            cwd="/project/path",
            model="qwen38",
            context_profile="large",
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.1.0",
            now="2026-07-22T00:00:00Z",
        )
        _v3.save_any(self.store, record)
        loaded = self.store.load(FIXED_ID)
        self.assertEqual(loaded["session_type"], sessions.SESSION_TYPE_ORDINARY)
        self.assertEqual(loaded["runtime_session_id"], OTHER_ID)
        self.assertNotIn("composition_name", loaded)
        self.assertNotIn("snapshot", loaded)


class PendingForkResolutionTests(SessionTestCase):
    """The 2026-07-27 incident lifecycle: a native fork is observed, authority
    later lands ON the fork (supervisor relaunch) — the stale pending marker
    must self-clear; genuinely pending forks keep blocking until resolve-fork.
    """

    THIRD_ID = "33333333-3333-4333-8333-333333333333"

    def _forked_record(self):
        _v3.save_any(self.store, _record(self.snapshot))
        forked = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd="/project/path",
            now="2026-07-22T00:00:00Z",
        )
        self.assertEqual(forked["identity_state"], sessions.IDENTITY_PENDING_FORK)
        self.assertEqual(forked["runtime_session_id"], FIXED_ID)
        return forked

    def _stuck_record(self):
        """The exact pre-fix state: authority on the fork, marker still pending."""

        record = {
            **_record(self.snapshot),
            "runtime_session_id": OTHER_ID,
            "runtime_aliases": [
                {
                    "session_id": FIXED_ID,
                    "source": "fork",
                    "observed_at": "2026-07-22T00:01:00Z",
                }
            ],
            "pending_forks": [
                {"session_id": OTHER_ID, "observed_at": "2026-07-22T00:00:00Z"}
            ],
            "identity_state": sessions.IDENTITY_PENDING_FORK,
        }
        _v3.save_any(self.store, record)
        return record

    def test_authority_landing_on_pending_fork_auto_resolves(self) -> None:
        self._forked_record()
        updated = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="resume",
            cwd="/project/path",
            now="2026-07-22T00:05:00Z",
        )
        self.assertEqual(updated["runtime_session_id"], OTHER_ID)
        self.assertEqual(updated["pending_forks"], [])
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertEqual(
            [item["session_id"] for item in updated["runtime_aliases"]],
            [FIXED_ID],
        )

    def test_authority_elsewhere_keeps_pending_fork_blocking(self) -> None:
        self._forked_record()
        updated = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=self.THIRD_ID,
            source="resume",
            cwd="/project/path",
            now="2026-07-22T00:05:00Z",
        )
        self.assertEqual(updated["runtime_session_id"], self.THIRD_ID)
        self.assertEqual(
            [item["session_id"] for item in updated["pending_forks"]],
            [OTHER_ID],
        )
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_PENDING_FORK)

    def test_converge_pending_forks_clears_authority_holder(self) -> None:
        self._stuck_record()
        self.assertTrue(self.store.converge_pending_forks(FIXED_ID))
        updated = self.store.load(FIXED_ID)
        self.assertEqual(updated["pending_forks"], [])
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertEqual(updated["runtime_session_id"], OTHER_ID)
        self.assertFalse(self.store.converge_pending_forks(FIXED_ID))

    def test_converge_pending_forks_keeps_genuine_pending(self) -> None:
        self._forked_record()
        self.assertFalse(self.store.converge_pending_forks(FIXED_ID))
        self.assertEqual(
            self.store.load(FIXED_ID)["identity_state"],
            sessions.IDENTITY_PENDING_FORK,
        )

    def test_converge_preserves_repair_needed(self) -> None:
        stuck = self._stuck_record()
        stuck = {**stuck, "identity_state": sessions.IDENTITY_REPAIR_NEEDED,
                 "observed_model": "some-model"}
        _v3.save_any(self.store, stuck)
        self.assertTrue(self.store.converge_pending_forks(FIXED_ID))
        updated = self.store.load(FIXED_ID)
        self.assertEqual(updated["pending_forks"], [])
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)

    def test_resolve_fork_discards_marker_and_keeps_parent(self) -> None:
        self._forked_record()
        updated = self.store.resolve_fork(FIXED_ID, OTHER_ID)
        self.assertEqual(updated["pending_forks"], [])
        self.assertEqual(updated["runtime_session_id"], FIXED_ID)
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_AUTHORITATIVE)

    def test_resolve_fork_refuses_authority_holder(self) -> None:
        self._stuck_record()
        with self.assertRaisesRegex(sessions.SessionError, "resume authority"):
            self.store.resolve_fork(FIXED_ID, OTHER_ID)

    def test_resolve_fork_refuses_unknown_and_bad_uuid(self) -> None:
        self._forked_record()
        with self.assertRaisesRegex(sessions.SessionError, "no pending fork"):
            self.store.resolve_fork(FIXED_ID, self.THIRD_ID)
        with self.assertRaisesRegex(sessions.SessionError, "UUIDv4"):
            self.store.resolve_fork(FIXED_ID, "not-a-uuid")

    def test_pending_fork_message_names_fork_and_remedies(self) -> None:
        record = self._forked_record()
        message = sessions.pending_fork_message(record)
        self.assertIn(FIXED_ID, message)
        self.assertIn(OTHER_ID, message)
        self.assertIn("sessions link", message)
        self.assertIn("resolve-fork", message)
        self.assertIn("default", message)  # recorded composition suggested
        self.assertIn("transcript is kept", message)

    def test_pending_fork_message_ordinary_uses_model_flag(self) -> None:
        ordinary = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd="/project/path",
            model="sol",
            context_profile="sol",
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.5.0",
            now="2026-07-21T00:00:00Z",
        )
        ordinary = {
            **ordinary,
            "pending_forks": [
                {"session_id": OTHER_ID, "observed_at": "2026-07-22T00:00:00Z"}
            ],
        }
        message = sessions.pending_fork_message(ordinary)
        self.assertIn("--model", message)
        self.assertIn(OTHER_ID, message)


class PointerTests(SessionTestCase):
    def test_update_and_last_roundtrip(self) -> None:
        self.assertTrue(self.store.update_last("/project/path", FIXED_ID))
        self.assertEqual(self.store.last("/project/path"), FIXED_ID)

    def test_one_pointer_per_cwd(self) -> None:
        # Managed and ordinary sessions share <d>.json; the last
        # write wins and no session_type is stored.
        self.store.update_last("/project/path", FIXED_ID)
        self.store.update_last("/project/path", OTHER_ID)
        self.assertEqual(self.store.last("/project/path"), OTHER_ID)
        pointer = self.store._pointer_path("/project/path")
        self.assertEqual(
            strict_json.loads(pointer.read_bytes()),
            {"cwd": "/project/path", "session_id": OTHER_ID},
        )
        self.assertFalse(self.store._legacy_pointer_path("/project/path").exists())

    def test_last_missing(self) -> None:
        self.assertIsNone(self.store.last("/nowhere"))

    def test_lock_failure_skips_update_without_blocking(self) -> None:
        pointer = self.store._pointer_path("/project/path")
        lock = state.FileLock(pointer)
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertFalse(self.store.update_last("/project/path", FIXED_ID))
        finally:
            lock.release()
        self.assertIsNone(self.store.last("/project/path"))
        self.assertTrue(self.store.update_last("/project/path", OTHER_ID))
        self.assertEqual(self.store.last("/project/path"), OTHER_ID)

    def test_corrupt_pointer_treated_as_missing(self) -> None:
        pointer = self.store._pointer_path("/project/path")
        state.atomic_write(pointer, b"junk{")
        self.assertIsNone(self.store.last("/project/path"))

    def test_pointer_with_non_uuid_session_is_treated_as_missing(self) -> None:
        pointer = self.store._pointer_path("/project/path")
        state.atomic_write(
            pointer,
            strict_json.canonical_file_bytes(
                {"cwd": "/project/path", "session_id": "../escape"}
            ),
        )
        self.assertIsNone(self.store.last("/project/path"))

    def test_pointer_updates_reject_non_uuid(self) -> None:
        with self.assertRaises(SessionError):
            self.store.update_last("/project/path", "../escape")
        with self.assertRaises(SessionError):
            self.store.clear_last("/project/path", "../escape")

    def test_compare_and_restore_pointer_bytes(self) -> None:
        self.store.update_last("/project/path", OTHER_ID)
        prior = self.store.read_pointer_bytes("/project/path")
        self.store.update_last("/project/path", FIXED_ID)
        self.assertTrue(
            self.store.restore_pointer_bytes("/project/path", FIXED_ID, prior)
        )
        self.assertEqual(self.store.last("/project/path"), OTHER_ID)
        self.assertEqual(self.store.read_pointer_bytes("/project/path"), prior)

    def test_clear_last_only_clears_matching_pointer(self) -> None:
        self.store.update_last("/project/path", FIXED_ID)
        self.assertFalse(self.store.clear_last("/project/path", OTHER_ID))
        self.assertEqual(self.store.last("/project/path"), FIXED_ID)
        self.assertTrue(self.store.clear_last("/project/path", FIXED_ID))
        self.assertIsNone(self.store.last("/project/path"))

    def test_clear_last_missing_pointer(self) -> None:
        self.assertFalse(self.store.clear_last("/nowhere", FIXED_ID))


class RootTests(unittest.TestCase):
    def test_xdg_roots(self) -> None:
        env = {
            "XDG_STATE_HOME": "/tmp/state-x",
            "XDG_CONFIG_HOME": "/tmp/config-x",
            "HOME": "/tmp/home-x",
        }
        self.assertEqual(sessions.state_root(env), Path("/tmp/state-x/claude-multi"))
        self.assertEqual(sessions.config_root(env), Path("/tmp/config-x/claude-multi"))
        fallback = {"HOME": "/tmp/home-y"}
        self.assertEqual(
            sessions.state_root(fallback), Path("/tmp/home-y/.local/state/claude-multi")
        )
        self.assertEqual(
            sessions.config_root(fallback), Path("/tmp/home-y/.config/claude-multi")
        )


if __name__ == "__main__":
    unittest.main()


class ForkCredentialRevocationTests(SessionTestCase):
    """Adopt/discard bumps the parent's epoch, staling the fork's hooks."""

    def _forked(self, epoch: int = 3):
        record = {
            **_record(self.snapshot),
            "launch_epoch": epoch,
        }
        _v3.save_any(self.store, record)
        return self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd="/project/path",
            launch_epoch=epoch,
            now="2026-07-22T00:00:00Z",
        )

    def test_resolve_fork_bumps_epoch_and_stales_the_fork_hooks(self) -> None:
        self._forked(epoch=3)
        updated = self.store.resolve_fork(FIXED_ID, OTHER_ID)
        self.assertEqual(updated["launch_epoch"], 4)
        # The fork's next id-changing hook (its baked epoch is 3) is ignored.
        after = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=self.store.new_id(),
            source="compact",
            cwd="/project/path",
            launch_epoch=3,
            now="2026-07-22T00:10:00Z",
        )
        self.assertEqual(after["runtime_session_id"], FIXED_ID)
        self.assertEqual(after["launch_epoch"], 4)

    def test_link_adoption_bumps_parent_epoch(self) -> None:
        forked = self._forked(epoch=2)
        # Link adopts as a v4 generation-0 record (save is v4-only).
        linked = _adopted_v4(self.store.new_id(), OTHER_ID)
        sessions.write_state_marker(self.root)
        self.store.link(linked)
        parent = self.store.load(FIXED_ID)
        self.assertEqual(parent["pending_forks"], [])
        self.assertEqual(parent["launch_epoch"], 3)


class PendingForkCapTests(SessionTestCase):
    """The 17th distinct fork fails the hook visibly; nothing evicted."""

    def test_cap_refuses_new_fork_without_evicting(self) -> None:
        record = _record(self.snapshot)
        record["pending_forks"] = [
            {
                "session_id": f"00000000-0000-4000-8000-{index:012d}",
                "observed_at": "2026-07-22T00:00:00Z",
            }
            for index in range(16)
        ]
        record["identity_state"] = sessions.IDENTITY_PENDING_FORK
        _v3.save_any(self.store, record)
        with self.assertRaisesRegex(sessions.SessionError, "16 unresolved"):
            self.store.reconcile_runtime(
                FIXED_ID,
                observed_runtime_id=OTHER_ID,
                source="fork",
                cwd="/project/path",
                now="2026-07-22T00:05:00Z",
            )
        after = self.store.load(FIXED_ID)
        self.assertEqual(len(after["pending_forks"]), 16)
        # Re-observing an already-tracked fork is still fine (idempotent).
        again = self.store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id="00000000-0000-4000-8000-000000000005",
            source="fork",
            cwd="/project/path",
            now="2026-07-22T00:06:00Z",
        )
        self.assertEqual(len(again["pending_forks"]), 16)

    def test_multi_fork_message_ordinary_uses_model_flag(self) -> None:
        ordinary = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd="/project/path",
            model="sol",
            context_profile="sol",
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.6.1",
            now="2026-07-21T00:00:00Z",
        )
        ordinary["pending_forks"] = [
            {"session_id": OTHER_ID, "observed_at": "2026-07-22T00:00:00Z"},
            {"session_id": "33333333-3333-4333-8333-333333333333",
             "observed_at": "2026-07-22T00:01:00Z"},
        ]
        message = sessions.pending_fork_message(ordinary)
        self.assertIn("--model MODEL", message)
        self.assertNotIn("--composition", message)


class RelinkGuidanceTests(SessionTestCase):
    """Actionable repair-needed guidance + bare-relink semantics."""

    def _repair_needed_record(self) -> dict:
        record = _record(self.snapshot)
        record["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        record["observed_cwd"] = "/wrong/project"
        return record

    def test_bare_relink_clears_observed_cwd_and_restores_authority(self) -> None:
        record = self._repair_needed_record()
        _v3.save_any(self.store, record)
        epoch_before = record.get("launch_epoch", 0)
        updated = self.store.relink_runtime(FIXED_ID, observed_runtime_id=FIXED_ID)
        self.assertNotIn("observed_cwd", updated)
        self.assertEqual(
            updated["identity_state"], sessions.IDENTITY_AUTHORITATIVE
        )
        self.assertEqual(updated["launch_epoch"], epoch_before + 1)
        self.assertEqual(updated["cwd"], "/project/path")

    def test_bare_relink_preserves_observed_model(self) -> None:
        record = self._repair_needed_record()
        record["observed_model"] = "gpt-multi-sol-high"
        _v3.save_any(self.store, record)
        updated = self.store.relink_runtime(FIXED_ID, observed_runtime_id=FIXED_ID)
        self.assertNotIn("observed_cwd", updated)
        self.assertEqual(updated["observed_model"], "gpt-multi-sol-high")
        # Model evidence alone still means repair-needed (the
        # allow-model-relaunch path owns that resolution).
        self.assertEqual(
            updated["identity_state"], sessions.IDENTITY_REPAIR_NEEDED
        )

    def test_relink_message_with_observed_cwd_names_both_commands(self) -> None:
        record = self._repair_needed_record()
        message = sessions.relink_message(record)
        base = f"claude-multi sessions relink-runtime {FIXED_ID} {FIXED_ID}"
        self.assertIn(f"`{base} --cwd /project/path`", message)
        self.assertIn(f"`{base} --cwd /wrong/project`", message)
        self.assertIn("common case", message)
        self.assertIn("re-homed", message)

    def test_relink_message_without_observed_cwd_is_bare_command(self) -> None:
        record = _record(self.snapshot)
        record["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        message = sessions.relink_message(record)
        self.assertIn(
            f"`claude-multi sessions relink-runtime {FIXED_ID} {FIXED_ID}`",
            message,
        )
        self.assertNotIn("--cwd", message)

    def test_relink_message_model_only_points_at_launcher_reconcile(self) -> None:
        record = _record(self.snapshot)
        record["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        record["observed_model"] = "gpt-multi-sol-high"
        message = sessions.relink_message(record)
        self.assertIn("observed model gpt-multi-sol-high", message)
        self.assertIn(f"`claude-multi -r {FIXED_ID}`", message)
        self.assertIn("relink only if the runtime UUID itself changed", message)

    def test_relink_message_shell_quotes_cwds(self) -> None:
        record = self._repair_needed_record()
        record["cwd"] = "/project/with space"
        message = sessions.relink_message(record)
        self.assertIn("--cwd '/project/with space'", message)
        self.assertIn("--cwd /wrong/project", message)


class RelinkMessageOrdinaryTests(SessionTestCase):
    """Ordinary-aware repair guidance."""

    def _ordinary_repair_record(self) -> dict:
        return _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd="/project/path",
            model="qwen38",
            context_profile="large",
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.2.0",
            identity_state=sessions.IDENTITY_REPAIR_NEEDED,
        )

    def test_ordinary_model_only_message_names_the_launcher_resume(self) -> None:
        # One model-only text for every record version; the
        # 2.x `claude-gateway -r … --model` remedy is gone (a 3.0 resume
        # migrates the record and relaunches its recorded lead).
        record = self._ordinary_repair_record()
        record["observed_model"] = "gpt-multi-sol-high"
        message = sessions.relink_message(record)
        self.assertIn("observed model gpt-multi-sol-high", message)
        self.assertIn(f"(`claude-multi -r {FIXED_ID}`)", message)
        self.assertNotIn("claude-gateway -r", message)
        self.assertNotIn("the recorded model", message)

    def test_ordinary_model_only_message_falls_back_without_snapshot(self) -> None:
        record = self._ordinary_repair_record()
        record["observed_model"] = "unexpected-selector"
        message = sessions.relink_message(record)
        self.assertIn("differs from the recorded qwen38", message)


class CorruptForgetStoreTests(SessionTestCase):
    """forget_session survives a corrupt record —
    scope removal + pointer sweep work load-free, by id."""

    def _corrupt(self) -> None:
        state.atomic_write(
            self.store.sessions_dir / f"{FIXED_ID}.json", b"{not json"
        )

    def test_corrupt_record_forgets_scope_and_pointers(self) -> None:
        _v3.save_any(self.store, _record(self.snapshot))
        self.store.update_last("/project/path", FIXED_ID)
        from claude_multi import scope as scope_mod

        live_scope = scope_mod.scope_dir(self.store.root, FIXED_ID)
        state.ensure_private_dir(live_scope)
        pointer = self.store._pointer_path("/project/path")
        self.assertTrue(pointer.exists())
        self._corrupt()
        removed, scope_removed = self.store.forget_session(FIXED_ID)
        self.assertTrue(removed)
        self.assertTrue(scope_removed)
        self.assertFalse(self.store.exists(FIXED_ID))
        self.assertFalse(live_scope.exists())
        self.assertFalse(pointer.exists())

    def test_pointer_sweep_rereads_under_lock(self) -> None:
        # A pointer that stops matching between scan and unlink is kept.
        _v3.save_any(self.store, _record(self.snapshot))
        self.store.update_last("/project/path", FIXED_ID)
        self._corrupt()
        pointer = self.store._pointer_path("/project/path")
        original_loads = strict_json.loads
        calls = {"n": 0}

        def flipping(raw):
            calls["n"] += 1
            payload = original_loads(raw)
            # First read (scan) matches; the under-lock re-read does not.
            if calls["n"] > 1:
                payload = dict(payload, session_id=OTHER_ID)
            return payload

        with mock.patch.object(strict_json, "loads", side_effect=flipping):
            self.store._sweep_pointers_for(FIXED_ID)
        self.assertTrue(pointer.exists())


class MarkEndedRemedyTests(unittest.TestCase):
    """The one remedy text live-record refusals name."""

    def test_one_id_is_named_in_full_and_several_are_listed(self) -> None:
        one = "33333333-3333-4333-8333-333333333333"
        two = "44444444-4444-4444-8444-444444444444"
        text = sessions.mark_ended_remedy([one])
        self.assertIn("a launch that exited before SessionStart", text)
        self.assertTrue(text.endswith(f"claude-multi sessions mark-ended {one}"))
        text = sessions.mark_ended_remedy([two, one, two])
        self.assertIn("claude-multi sessions mark-ended <id> (ids: " + one + ", " + two + ")", text)
        self.assertTrue(sessions.mark_ended_remedy().endswith("claude-multi sessions mark-ended <id>"))


class BackgroundLivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_absent_file_symlink_and_foreign_root(self):
        base = self.root / "daemon"
        self.assertEqual(sessions.background_liveness(base), sessions.BackgroundLiveness(True, frozenset()))
        base.touch()
        self.assertFalse(sessions.background_liveness(base).known)
        base.unlink()
        base.symlink_to(self.root, target_is_directory=True)
        self.assertFalse(sessions.background_liveness(base).known)
        with mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            self.assertFalse(sessions.background_liveness(self.root).known)
        with mock.patch.object(os, "getuid", None):
            self.assertFalse(sessions.background_liveness(self.root).known)

    def test_scan_failures_are_unknown_and_resume_stays_permissive(self):
        with mock.patch.object(os, "scandir", side_effect=PermissionError):
            self.assertFalse(sessions.background_liveness(self.root).known)
            self.assertEqual(sessions.live_background_prefixes(self.root), frozenset())
        pty = self.root / "domain/pty"
        pty.mkdir(parents=True)
        (pty / "abcdef12.sock").touch()
        (self.root / "other").mkdir()
        self.assertEqual(sessions.background_liveness(self.root),
                         sessions.BackgroundLiveness(True, frozenset({"abcdef12"})))
        original = os.scandir
        def scan(path):
            if Path(path) == pty:
                raise PermissionError
            return original(path)
        with mock.patch.object(os, "scandir", side_effect=scan):
            self.assertFalse(sessions.background_liveness(self.root).known)
