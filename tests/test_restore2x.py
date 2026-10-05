"""``claude-multi restore-2x``.

The restore half plus the liveness usability
(``sessions mark-ended``, the ``/proc`` liveness source, ``--assume-dead``
and the list-and-confirm path). The races are driven here without
``perform_launch`` (a test-held ``launcher_write_guard`` and hand-written
records); the real-perform variants follow below. Every thread join has a
10 s timeout so a lock-order bug fails instead of hanging.
"""

from __future__ import annotations

import errno
import contextlib
import io
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _tripwire
from claude_multi import cli, launch, migrate, proxy, service, sessions, state, strict_json, validate
from _v4 import V4Case
from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT
from _golden import assertGolden
from test_migrate import (
    CREATED,
    FORK,
    GOLDENS as MIGRATE_GOLDENS,
    LCAT,
    M1,
    M2,
    M3,
    M4,
    M5,
    M6,
    NOW,
    RUNTIME,
    SCHEMA,
    _cli_environ,
    _write_pointer,
    _write_raw,
    restore_golden_files,
    v1,
    v2,
    v3_managed,
    v3_ordinary,
)
import claude_multi.cli.session_actions
import claude_multi.sessions
import claude_multi.hooks

_tripwire.install()

SCHEMA_226 = strict_json.load(REPO_ROOT / "tests" / "fixtures" / "session.schema.2.26.json")
RESTORE_GOLDENS = MIGRATE_GOLDENS.parent / "restore"
JOIN = 10.0


def _v4_born(managed_id: str, *, cwd: str = "/project/new", last_event_source: str | None = "end") -> dict:
    """A 3.0-born record (fresh launch / sessions link): v4 without ``migration``."""

    record = migrate.convert_record(v3_managed(managed_id, cwd=cwd), cat=LCAT, now=NOW).record
    record.pop("migration")
    record["migrated_from_version"] = None
    record["last_event_source"] = last_event_source
    return record


class RestoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-restore-"))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.environ = _cli_environ(self.tmp)
        self.root = sessions.state_root(self.environ)
        self.store = sessions.SessionStore(self.root, SCHEMA)
        self.hook = "/nix/store/fixture/bin/claude-multi"
        from claude_multi import scope

        self.shim = scope.ensure_hook_shim(self.root, self.hook)
        patcher = mock.patch.object(sessions, "live_background_prefixes", return_value=frozenset())
        self.prefixes = patcher.start()
        self.addCleanup(patcher.stop)
        # Destructive guards read the known-aware daemon form; its prefixes come from self.prefixes.
        patcher = mock.patch.object(sessions, "background_liveness", side_effect=lambda *a, **kw:
                                    sessions.BackgroundLiveness(True, self.prefixes(*a, **kw)))
        self.daemon = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(sessions, "proc_session_ids", return_value=frozenset())
        self.proc = patcher.start()
        self.addCleanup(patcher.stop)
        # Destructive guards read the scan (known + ids); its ids come from self.proc.
        patcher = mock.patch.object(sessions, "proc_session_scan", side_effect=lambda *a, **kw:
                                    sessions.ProcessScan(True, self.proc(*a, **kw)))
        self.scan = patcher.start()
        self.addCleanup(patcher.stop)

    def migrate(self, *records: dict) -> None:
        for raw in records:
            _write_raw(self.store, raw)
        report = migrate.run(self.store, LCAT, hook_command=self.hook, hook_shim_path=self.shim,
                             now=NOW)
        self.assertTrue(report.ok)

    def restore(self, argv: list[str] = (), *, stdin: str | None = None) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.root.parent),
                                          "XDG_CONFIG_HOME": str(self.root.parent / "config"),
                                          "XDG_STATE_HOME": str(self.root.parent / "state"),
                                          "XDG_DATA_HOME": str(self.root.parent / "data")}), \
                mock.patch.object(claude_multi.sessions, "state_root", return_value=self.root):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = cli.main(
                    ["restore-2x", *argv],
                    output_stream=output,
                    input_stream=None if stdin is None else io.StringIO(stdin),
                    interactive=stdin is not None,
                )
        # A refusal reports on stderr: the result, then the refusal.
        return code, output.getvalue() + errors.getvalue()

    def tree(self) -> dict:
        return {
            str(path.relative_to(self.root)): (
                None if path.is_dir() else path.read_bytes(), path.lstat().st_mtime_ns
            )
            for path in sorted(self.root.rglob("*"))
            if not path.name.endswith(".lock")
        }

    def marker(self) -> bool:
        return os.path.lexists(self.root / sessions.STATE_MARKER)

    def assert_226_readable(self, managed: str) -> dict:
        record = strict_json.loads((self.store.sessions_dir / f"{managed}.json").read_bytes())
        self.assertEqual(validate.validate(record, SCHEMA_226, "$"), [])
        sessions._validate_record_invariants(record, "2.26")
        self.assertEqual(record["version"], 3)
        return record


class MarkerGateTests(RestoreTestCase):
    def test_newer_malformed_or_symlinked_marker_refuses_and_touches_nothing(self) -> None:
        self.migrate(v3_managed(M1))
        marker = self.root / sessions.STATE_MARKER
        for body in (b"5\n", b"garbage", None):
            with self.subTest(body=body):
                state.remove_private(marker)
                if body is None:
                    target = self.tmp / "elsewhere"
                    target.write_bytes(b"4\n")
                    os.symlink(target, marker)
                else:
                    state.atomic_write(marker, body)
                before = self.tree()
                code, output = self.restore()
                self.assertEqual(code, 1)
                self.assertTrue(output.startswith("claude-multi: state belongs to a newer"), output)
                self.assertEqual(self.tree(), before)
                if body is None:
                    os.unlink(marker)

    def test_marker_3_has_nothing_to_restore(self) -> None:
        _write_raw(self.store, v3_managed(M1))
        code, output = self.restore()
        self.assertEqual(code, 0)
        self.assertEqual(output, "No current-format state exists here; nothing to restore.\n")


class RefusalTests(RestoreTestCase):
    def test_a_record_without_an_end_refuses(self) -> None:
        self.migrate(v3_managed(M1, last_event_source="resume"), v3_managed(M5))
        before = self.tree()
        code, output = self.restore()
        self.assertEqual(code, 1)
        self.assertIn(
            f"claude-multi: restore-2x refused: 1 session(s) may still be running (live or "
            f"unknown): {M1[:8]} (last event resume). Stop them (claude-multi sessions stop "
            "<id>); for a session you know has exited without a SessionEnd, rerun with "
            "--not-running <id>. Nothing was changed.",
            output,
        )
        self.assertIn("claude-multi sessions mark-ended <id>", output)
        self.assertEqual(self.tree(), before)
        self.assertTrue(self.marker())
        code, output = self.restore(["--not-running", M1])
        self.assertEqual(code, 0, output)
        self.assertFalse(self.marker())
        # --not-running keeps the lifecycle as it was
        self.assertEqual(self.assert_226_readable(M1)["last_event_source"], "resume")

    def test_null_last_event_counts_as_live(self) -> None:
        # the state a v4 resume commit leaves, hand-written
        self.migrate(v3_managed(M1))
        raw, _data = self.store.load_raw(M1)
        raw["last_event_source"] = None
        _write_raw(self.store, raw)
        code, output = self.restore()
        self.assertEqual(code, 1)
        self.assertIn(f"{M1[:8]} (last event none)", output)

    def test_unreadable_record_refuses_and_not_running_cannot_override(self) -> None:
        self.migrate(v3_managed(M1))
        state.atomic_write(self.store.sessions_dir / f"{M5}.json", b"{broken")
        for argv in ([], ["--not-running", M5]):
            with self.subTest(argv=argv):
                before = self.tree()
                code, output = self.restore(argv)
                self.assertEqual(code, 1)
                self.assertIn(
                    f"{M5[:8]}: record unreadable (", output
                )
                self.assertIn(
                    f"fix it or run claude-multi sessions forget {M5} (this drops its legacy backup)",
                    output,
                )
                self.assertEqual(self.tree(), before)

    def test_daemon_live_prefix_refuses_whatever_the_flags(self) -> None:
        self.migrate(v3_managed(M1, last_event_source="resume"))
        self.prefixes.return_value = frozenset({M1[:8]})
        code, output = self.restore(["--not-running", M1])
        self.assertEqual(code, 1)
        self.assertIn(
            f"claude-multi: {M1[:8]} is live in the Claude background daemon; --not-running "
            "cannot override that",
            output,
        )
        code, output = self.restore(["--assume-dead", M1], stdin="y\n")
        self.assertEqual(code, 1)
        self.assertTrue(self.marker())

    def test_proc_cmdline_liveness_is_a_second_source(self) -> None:
        # A process whose argv names the runtime id.
        self.migrate(v3_managed(M1, last_event_source="end"))
        self.proc.return_value = frozenset({M1})
        code, output = self.restore()
        self.assertEqual(code, 1)
        self.assertIn(f"{M1[:8]} (last event end)", output)

    def test_unreadable_process_table_refuses_and_changes_nothing(self) -> None:
        # A missing /proc capability is unknown, never "no process".
        self.migrate(v3_managed(M1, last_event_source="end"))
        self.scan.side_effect = None
        self.scan.return_value = _UNREADABLE
        for argv in ([], ["--not-running", M1], ["--assume-dead", M1]):
            with self.subTest(argv=argv):
                before = self.tree()
                code, output = self.restore(argv)
                self.assertEqual(code, 1, output)
                self.assertIn("restore-2x: cannot tell which sessions are running (the process "
                              "table is unreadable: PermissionError); nothing restored", output)
                self.assertEqual(self.tree(), before)
                self.assertTrue(self.marker())

    def test_unobservable_daemon_liveness_refuses_and_changes_nothing(self) -> None:
        # The daemon half of the liveness check fails closed too.
        self.migrate(v3_managed(M1, last_event_source="end"))
        root = _daemon_fixture(self.tmp, M1, symlink=False)
        self.daemon.side_effect = lambda *_a, **_kw: _REAL_BACKGROUND_LIVENESS(root)
        before = self.tree()
        code, output = self.restore(["--not-running", M1])
        self.assertEqual(code, 1, output)
        self.assertIn(f"{M1[:8]} is live in the Claude background daemon; --not-running "
                      "cannot override that", output)
        self.assertEqual(self.tree(), before)
        (root / "link").symlink_to(root)
        self.assertFalse(_REAL_BACKGROUND_LIVENESS(root).known)
        for argv in ([], ["--not-running", M1], ["--assume-dead", M1]):
            with self.subTest(argv=argv):
                code, output = self.restore(argv)
                self.assertEqual(code, 1, output)
                self.assertIn("claude-multi: restore-2x: cannot tell which sessions are live in the "
                              "background (symlink in daemon root); nothing restored", output)
                self.assertEqual(self.tree(), before)
                self.assertTrue(self.marker())
                self.assertEqual(self.store.load(M1)["version"], 4)

    def test_an_unreadable_process_argv_refuses_and_changes_nothing(self) -> None:
        # A listed process whose argv cannot be read is unknown.
        self.migrate(v3_managed(M1, last_event_source="end"))
        proc_root = _argv_unreadable_proc(self.tmp)
        self.scan.side_effect = lambda *_a, **kw: _REAL_PROC_SESSION_SCAN(proc_root, **kw)
        for argv in ([], ["--not-running", M1], ["--assume-dead", M1]):
            with self.subTest(argv=argv):
                before = self.tree()
                code, output = self.restore(argv)
                self.assertEqual(code, 1, output)
                self.assertIn(f"cannot tell which sessions are running ({_ARGV_UNREADABLE}); "
                              "nothing restored", output)
                self.assertEqual(self.tree(), before)
                self.assertTrue(self.marker())

    def test_migrated_record_without_its_backup_refuses(self) -> None:
        self.migrate(v3_managed(M1))
        state.remove_private(self.store.backup_path(M1))
        before = self.tree()
        code, output = self.restore()
        self.assertEqual(code, 1)
        self.assertIn(
            f"{M1[:8]}: migrated record without its legacy backup (sessions/{M1}.v3.json "
            f"missing); restore it from a copy or run claude-multi sessions forget {M1}",
            output,
        )
        self.assertEqual(self.tree(), before)

    def test_bad_id_argument(self) -> None:
        self.migrate(v3_managed(M1))
        code, output = self.restore(["--not-running", "not-a-uuid"])
        self.assertEqual(code, 2)
        self.assertIn("is not a managed session UUID", output)


class AssumeDeadTests(RestoreTestCase):
    def test_assume_dead_records_the_end(self) -> None:
        self.migrate(v3_managed(M1, last_event_source="compact"), v3_ordinary(M2, last_event_source=None))
        code, output = self.restore(["--assume-dead", M1, M2])
        self.assertEqual(code, 0, output)
        for managed in (M1, M2):
            record = self.assert_226_readable(managed)
            self.assertEqual(record["last_event_source"], "end")
            self.assertEqual(record["last_end_reason"], sessions.MARKED_ENDED_REASON)
        self.assertIn(", marked ended)", output)

    def test_list_and_confirm(self) -> None:
        self.migrate(v3_managed(M1, last_event_source="resume"))
        code, output = self.restore(stdin="n\n")
        self.assertEqual(code, 3)
        self.assertIn(f"  {M1}  last event resume", output)
        self.assertIn("record their end, then restore? [y/N]", output)
        self.assertTrue(self.marker())
        code, output = self.restore(stdin="y\n")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.assert_226_readable(M1)["last_event_source"], "end")


_REAL_PROC_SESSION_IDS = sessions.proc_session_ids
_REAL_PROC_SESSION_SCAN = sessions.proc_session_scan
_UNREADABLE = sessions.ProcessScan(False, frozenset(), "PermissionError")
_REAL_BACKGROUND_LIVENESS = sessions.background_liveness
_ARGV_UNREADABLE = "the process table is unreadable: IsADirectoryError on process 4242"


def _daemon_fixture(base: Path, managed: str | None, *, symlink: bool) -> Path:
    """A fixture daemon root: a pty socket for ``managed``, plus a symlink (unknown)."""
    root = base / "cc-daemon"
    (root / "d1" / "pty").mkdir(parents=True)
    if managed is not None:
        (root / "d1" / "pty" / f"{managed[:8]}.sock").write_bytes(b"")
    if symlink:
        (root / "link").symlink_to(root)
    return root


def _argv_unreadable_proc(base: Path) -> Path:
    """A fixture process table whose pid 4242 argv opens but cannot be read (EISDIR)."""
    root = base / "fake-proc-argv"
    (root / "4242" / "cmdline").mkdir(parents=True)
    return root
_REAL_ANCESTOR_PIDS = sessions.ancestor_pids
_ANCESTORS = frozenset({4242, 4241, 1})
_OWN_CLIENT = 4241  # an ancestor of the call: the lead's own client
_OTHER = 5151       # a non-ancestor process (a fork, another terminal)


def _write_stat(root: Path, pid: int, line: bytes) -> None:
    (root / str(pid)).mkdir(parents=True, exist_ok=True)
    (root / str(pid) / "stat").write_bytes(line)


class SelfAssumeDeadTests(RestoreTestCase):
    """Rolling back from
    inside the lead's own managed session. Liveness comes from patched
    ``proc_session_ids`` (keyed on ``exclude_pids``) and ``ancestor_pids``;
    the walk itself runs over a fake ``/proc`` tree only.
    """

    def setUp(self) -> None:
        super().setUp()
        self.proc_root = self.tmp / "fake-proc"
        self.naming: dict[int, frozenset[str]] = {_OWN_CLIENT: frozenset({M1})}
        self.proc_calls: list[tuple[object, frozenset[int]]] = []

        def proc(proc_root="/proc", *, exclude_pids=()):
            excluded = frozenset(exclude_pids)
            self.proc_calls.append((proc_root, excluded))
            return frozenset(
                ident
                for pid, idents in self.naming.items()
                if pid not in excluded
                for ident in idents
            )

        self.proc.side_effect = proc
        patcher = mock.patch.object(sessions, "ancestor_pids", return_value=_ANCESTORS)
        self.ancestors = patcher.start()
        self.addCleanup(patcher.stop)
        self.migrate(v3_managed(M1, last_event_source="resume"), v3_managed(M2))

    def restore_as(self, argv: list[str], environ: dict[str, str]) -> tuple[int, str]:
        output = io.StringIO()
        args = cli.build_parser().parse_args(["restore-2x", *argv])
        # The barrier is HOME-relative; keep it in the fixture tree.
        environ = {"HOME": str(self.root.parent / "home"), **environ}
        code = cli._restore_2x(
            self.root, output, args, interactive=False, error_stream=output,
            proc_root=self.proc_root, environ=environ,
        )
        return code, output.getvalue()

    def assert_refused_hard(self, argv: list[str], environ: dict[str, str]) -> str:
        before = self.tree()
        code, output = self.restore_as(argv, environ)
        self.assertEqual(code, 1, output)
        self.assertIn(f"claude-multi: {M1[:8]} is live", output)
        self.assertEqual(self.tree(), before)
        self.assertTrue(self.marker())
        return output

    def assert_restored_and_ended(self, code: int, output: str) -> None:
        self.assertEqual(code, 0, output)
        self.assertFalse(self.marker())
        record = self.assert_226_readable(M1)
        self.assertEqual(record["last_event_source"], "end")
        self.assertEqual(record["last_end_reason"], sessions.MARKED_ENDED_REASON)
        self.assertIn(", marked ended)", output)

    def test_a_own_id_named_only_by_an_ancestor_is_restored_and_marked_ended(self) -> None:
        code, output = self.restore_as(
            ["--assume-dead", M1], {"CLAUDE_MULTI_MANAGED_ID": M1}
        )
        self.assert_restored_and_ended(code, output)
        # The preview's inventory, then a fresh one inside the commit phase.
        self.assertEqual(self.ancestors.call_args_list, [mock.call(self.proc_root)] * 2)
        self.assertEqual(
            self.proc_calls,
            [(self.proc_root, frozenset()), (self.proc_root, _ANCESTORS)] * 2,
        )

    def test_a_through_main_reads_the_process_environment(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_MULTI_MANAGED_ID": M1}):
            code, output = self.restore(["--assume-dead", M1])
        self.assert_restored_and_ended(code, output)
        self.assertEqual(self.ancestors.call_args_list, [mock.call("/proc")] * 2)

    def test_b_a_non_ancestor_process_naming_it_still_refuses(self) -> None:
        self.naming[_OTHER] = frozenset({M1})
        self.assert_refused_hard(["--assume-dead", M1], {"CLAUDE_MULTI_MANAGED_ID": M1})
        self.ancestors.assert_called_once_with(self.proc_root)

    def test_c_without_a_managed_id_in_the_environment_refuses(self) -> None:
        self.assert_refused_hard(["--assume-dead", M1], {})
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_MULTI_MANAGED_ID", None)
            before = self.tree()
            code, output = self.restore(["--assume-dead", M1])
        self.assertEqual(code, 1, output)
        self.assertEqual(self.tree(), before)
        self.ancestors.assert_not_called()

    def test_an_unreadable_foreign_scan_refuses(self) -> None:
        def scan(proc_root="/proc", *, exclude_pids=()):
            if exclude_pids:
                return _UNREADABLE
            return sessions.ProcessScan(True, frozenset())

        self.scan.side_effect = scan
        before = self.tree()
        code, output = self.restore_as(["--assume-dead", M1], {"CLAUDE_MULTI_MANAGED_ID": M1})
        self.assertEqual(code, 1, output)
        self.assertIn("process table is unreadable", output)
        self.assertEqual(self.tree(), before)
        self.assertTrue(self.marker())

    def test_d_an_environment_naming_another_id_refuses(self) -> None:
        self.assert_refused_hard(["--assume-dead", M1], {"CLAUDE_MULTI_MANAGED_ID": M2})
        self.ancestors.assert_not_called()

    def test_h_the_override_covers_the_own_record_only(self) -> None:
        # the ancestor also names another record: that one stays ●
        self.naming[_OWN_CLIENT] = frozenset({M1, M2})
        before = self.tree()
        code, output = self.restore_as(
            ["--assume-dead", M1, M2], {"CLAUDE_MULTI_MANAGED_ID": M1}
        )
        self.assertEqual(code, 1, output)
        self.assertIn(f"claude-multi: {M2[:8]} is live", output)
        self.assertNotIn(f"claude-multi: {M1[:8]} is live", output)
        self.assertEqual(self.tree(), before)

    def test_e_daemon_liveness_of_the_own_id_is_never_overridden(self) -> None:
        self.prefixes.return_value = frozenset({M1[:8]})
        output = self.assert_refused_hard(
            ["--assume-dead", M1], {"CLAUDE_MULTI_MANAGED_ID": M1}
        )
        self.assertIn("is live in the Claude background daemon", output)

    def test_f_not_running_gets_no_override(self) -> None:
        self.assert_refused_hard(["--not-running", M1], {"CLAUDE_MULTI_MANAGED_ID": M1})
        self.ancestors.assert_not_called()

    def test_g_ancestor_pids_walks_a_fake_proc_tree(self) -> None:
        root = self.proc_root
        # comm may hold spaces and parentheses: field 4 is read after the last ")"
        _write_stat(root, 300, b"300 (claude multi) S 200 300 300 0 -1 4194560\n")
        _write_stat(root, 200, b"200 (cm) S 7 (x) S 100 200 200 0 -1 4194560\n")
        _write_stat(root, 100, b"100 ((bash)) S 1 100 100 0 -1 4194304\n")
        _write_stat(root, 1, b"1 (systemd) S 0 1 1 0 -1 4194560\n")
        _write_stat(root, 7, b"7 (decoy) S 1 7 7 0 -1 0\n")
        self.assertEqual(_REAL_ANCESTOR_PIDS(root, 300), frozenset({300, 200, 100, 1}))
        # a cycle ends the walk
        _write_stat(root, 50, b"50 (a) S 51 0 0 0 -1 0\n")
        _write_stat(root, 51, b"51 (b) S 50 0 0 0 -1 0\n")
        self.assertEqual(_REAL_ANCESTOR_PIDS(root, 50), frozenset({50, 51}))
        _write_stat(root, 52, b"52 (self) S 52 0 0 0 -1 0\n")
        self.assertEqual(_REAL_ANCESTOR_PIDS(root, 52), frozenset({52}))
        # any error ends the walk: a missing parent, a malformed or unreadable line
        _write_stat(root, 400, b"400 (orphan) S 401 0 0 0 -1 0\n")
        self.assertEqual(_REAL_ANCESTOR_PIDS(root, 400), frozenset({400, 401}))
        _write_stat(root, 500, b"500 no-parenthesis S 1\n")
        self.assertEqual(_REAL_ANCESTOR_PIDS(root, 500), frozenset({500}))
        _write_stat(root, 501, b"501 (short)\n")
        self.assertEqual(_REAL_ANCESTOR_PIDS(root, 501), frozenset({501}))
        _write_stat(root, 502, b"502 (nan) S x 0\n")
        self.assertEqual(_REAL_ANCESTOR_PIDS(root, 502), frozenset({502}))
        self.assertEqual(_REAL_ANCESTOR_PIDS(root / "missing", 300), frozenset({300}))
        # bounded to 64 steps
        for pid in range(1000, 1100):
            _write_stat(root, pid, f"{pid} (c) S {pid + 1} 0 0 0 -1 0\n".encode())
        chain = _REAL_ANCESTOR_PIDS(root, 1000)
        self.assertEqual(chain, frozenset(range(1000, 1064)))
        # the default start is this process (the fake tree only)
        _write_stat(root, os.getpid(), f"{os.getpid()} (python3) S 300 0 0 0 -1 0\n".encode())
        self.assertEqual(
            _REAL_ANCESTOR_PIDS(root), frozenset({os.getpid(), 300, 200, 100, 1})
        )

    def test_g_proc_session_ids_skips_excluded_pids(self) -> None:
        root = self.proc_root
        for pid, data in (
            ("101", b"claude\0--resume\0" + M1.encode() + b"\0"),
            ("102", b"claude\0--session-id=" + M2.encode() + b"\0"),
        ):
            (root / pid).mkdir(parents=True)
            (root / pid / "cmdline").write_bytes(data)
        self.assertEqual(_REAL_PROC_SESSION_IDS(root), frozenset({M1, M2}))
        self.assertEqual(_REAL_PROC_SESSION_IDS(root, exclude_pids=()), frozenset({M1, M2}))
        self.assertEqual(_REAL_PROC_SESSION_IDS(root, exclude_pids={101}), frozenset({M2}))
        self.assertEqual(
            _REAL_PROC_SESSION_IDS(root, exclude_pids=frozenset({101, 102})), frozenset()
        )


class RestoreResultTests(RestoreTestCase):
    def test_restored_records_are_2_26_records_and_the_marker_goes_last(self) -> None:
        self.migrate(v3_managed(M1), v3_ordinary(M2), v2(M3), v1(M4))
        _write_raw(self.store, _v4_born(M5, cwd="/project/new"))
        _write_pointer(self.store, "/project/new", M5, session_type=None)
        order: list[str] = []
        real_remove = sessions.remove_state_marker
        real_save = sessions.SessionStore._save_lifecycle

        def remove(root):
            order.append("marker")
            return real_remove(root)

        def save(store, document):
            order.append("save")
            return real_save(store, document)

        with mock.patch.object(sessions, "remove_state_marker", side_effect=remove), \
                mock.patch.object(sessions.SessionStore, "_save_lifecycle", autospec=True,
                                  side_effect=save):
            # legacy v1/v2 files never saw a hook: no recorded end
            code, output = self.restore(["--not-running", M3, M4])
        self.assertEqual(code, 0, output)
        self.assertEqual(order[-1], "marker")
        self.assertEqual(order.count("save"), 4)
        self.assertFalse(self.marker())
        for managed in (M1, M2, M3, M4):
            record = self.assert_226_readable(managed)
            self.assertFalse(self.store.backup_path(managed).exists())
            self.assertEqual(record["mutation_token"] != "", True)
        self.assertEqual(self.assert_226_readable(M3)["migrated_from_version"], 2)
        self.assertEqual(self.assert_226_readable(M4)["migrated_from_version"], 1)
        # 3.0-born: quarantined, pointer cleared
        self.assertFalse((self.store.sessions_dir / f"{M5}.json").exists())
        quarantined = list((self.root / "quarantine-3x").rglob(f"{M5}.json"))
        self.assertEqual(len(quarantined), 1)
        self.assertIsNone(self.store._pointer_session(self.store._pointer_path("/project/new")))
        retired = list((self.root / "restored-2x").rglob("*.v3.json"))
        self.assertEqual(len(retired), 4)
        self.assertEqual(list(self.store.sessions_dir.glob("*.v3.json")), [])
        self.assertIn(f"restored     {M1[:8]}  v4 → v3 (lifecycle, epoch 1", output)
        self.assertIn(f"restored     {M4[:8]}  v4 → v1→3", output)
        self.assertIn(f"quarantined  {M5[:8]}  current-format (no legacy backup)", output)
        self.assertIn("marker removed.\nNext: start the earlier launcher", output)
        # a second restore: marker 3 -> nothing to restore
        self.assertEqual(self.restore()[0], 0)

    def test_overlay_preserves_post_migration_runtime_ids_aliases_epoch_and_cwd(self) -> None:
        self.migrate(v3_managed(M1, cwd="/project/a"))
        # a 3.0 resume that retargets the runtime id (the old id becomes an alias)
        self.store.reconcile_runtime(M1, observed_runtime_id=RUNTIME, source="resume",
                                     launch_epoch=1)
        # a fork, then its discard: the 3.0 epoch bump
        self.store.reconcile_runtime(M1, observed_runtime_id=FORK, source="fork", launch_epoch=1)
        self.store.resolve_fork(M1, FORK)
        (self.tmp / "moved").mkdir()
        self.store.relink_runtime(M1, observed_runtime_id=RUNTIME, cwd=str(self.tmp / "moved"))
        self.store.record_session_end(M1, observed_runtime_id=RUNTIME, reason="exit",
                                      launch_epoch=self.store.load(M1)["launch_epoch"])
        current = self.store.load(M1)
        code, output = self.restore()
        self.assertEqual(code, 0, output)
        record = self.assert_226_readable(M1)
        self.assertEqual(record["runtime_session_id"], RUNTIME)
        self.assertIn(M1, [alias["session_id"] for alias in record["runtime_aliases"]])
        self.assertEqual(record["launch_epoch"], current["launch_epoch"])
        self.assertGreaterEqual(record["launch_epoch"], 3)
        self.assertEqual(record["cwd"], str(self.tmp / "moved"))
        self.assertEqual(record["composition_name"], "opus-sol")

    def test_pointers_restore_with_session_type_and_drop_forgotten_records(self) -> None:
        self.migrate(
            v3_managed(M1, cwd="/project/a", last_seen_at="2026-09-20T00:00:00Z"),
            v3_ordinary(M2, cwd="/project/a", last_seen_at="2026-09-21T00:00:00Z"),
            v3_managed(M6, cwd="/project/f"),
        )
        # (the migrate above ran before any pointer existed; write 2.x pointers,
        # back them up the way migrate does, and merge them)
        _write_pointer(self.store, "/project/a", M1)
        _write_pointer(self.store, "/project/a", M2, sessions.SESSION_TYPE_ORDINARY)
        _write_pointer(self.store, "/project/f", M6)
        report = migrate.run(self.store, LCAT, hook_command=self.hook, hook_shim_path=self.shim,
                             now=NOW)
        self.assertEqual(report.pointer_backups_written, 3)
        # M6 is forgotten under 3.0: its pointer backup is swept by forget and
        # never resurrected.
        self.store.forget_session(M6)
        # a 3.0 resume of M1 moved the current <d>.json to M1 again
        self.store.update_last("/project/a", M1)
        code, output = self.restore()
        self.assertEqual(code, 0, output)
        primary = self.store._pointer_path("/project/a")
        legacy = self.store._legacy_pointer_path("/project/a")
        self.assertEqual(
            strict_json.loads(primary.read_bytes()),
            {"cwd": "/project/a", "session_id": M1, "session_type": sessions.SESSION_TYPE_MANAGED},
        )
        self.assertEqual(
            strict_json.loads(legacy.read_bytes()),
            {"cwd": "/project/a", "session_id": M2,
             "session_type": sessions.SESSION_TYPE_ORDINARY},
        )
        self.assertIsNone(self.store._pointer_session(self.store._pointer_path("/project/f")))
        self.assertEqual(list(self.store.pointers_dir.glob("*.v3")), [])
        self.assertTrue(list((self.root / "restored-2x").rglob("*.json.v3")))

    def test_ordinary_target_conflict_newer_last_seen_wins(self) -> None:
        # A backup <d>.ordinary.json.v3 (M2) and a current <d>.json whose
        # record (M8-like ordinary) restores as ordinary: both target
        # <d>.ordinary.json -> the newer record wins.
        self.migrate(
            v3_ordinary(M2, cwd="/project/o", last_seen_at="2026-09-20T00:00:00Z"),
            v3_ordinary(M5, cwd="/project/o", last_seen_at="2026-09-22T00:00:00Z"),
        )
        _write_pointer(self.store, "/project/o", M2, sessions.SESSION_TYPE_ORDINARY)
        migrate.run(self.store, LCAT, hook_command=self.hook, hook_shim_path=self.shim, now=NOW)
        self.store.update_last("/project/o", M5)
        code, output = self.restore()
        self.assertEqual(code, 0, output)
        legacy = self.store._legacy_pointer_path("/project/o")
        self.assertEqual(strict_json.loads(legacy.read_bytes())["session_id"], M5)
        self.assertFalse(self.store._pointer_path("/project/o").exists())

    def test_goldens(self) -> None:
        for name, data in restore_golden_files().items():
            with self.subTest(name=name):
                assertGolden(self, RESTORE_GOLDENS / name, data)
                self.assertEqual(
                    validate.validate(strict_json.loads(data), SCHEMA_226, "$"), []
                )


class RestoreRaceTests(RestoreTestCase):
    def test_restore_waits_for_a_shared_launcher_hold(self) -> None:
        self.migrate(v3_managed(M1))
        guard = sessions.launcher_write_guard(self.root)
        guard.__enter__()
        result: dict = {}
        worker = threading.Thread(target=lambda: result.update(code=self.restore()))
        worker.start()
        time.sleep(0.3)
        self.assertTrue(worker.is_alive(), "restore did not wait for the shared hold")
        self.assertTrue(self.marker())
        guard.__exit__(None, None, None)
        worker.join(JOIN)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["code"][0], 0, result["code"][1])

    def test_a_launcher_write_during_restore_is_refused(self) -> None:
        self.migrate(v3_managed(M1))
        entered, release = threading.Event(), threading.Event()
        real = cli._record_view_is_live

        def slow_live(view, **kwargs):
            # The preview runs lock-free; the commit phase's
            # revalidation (EX migration lock held) is where restore waits.
            if claude_multi.hooks.migration_lock_held(self.store.root):
                entered.set()
                release.wait(JOIN)
            return real(view, **kwargs)

        result: dict = {}
        with mock.patch('claude_multi.cli.session_facts._record_view_is_live', side_effect=slow_live):
            worker = threading.Thread(target=lambda: result.update(code=self.restore()))
            worker.start()
            self.assertTrue(entered.wait(JOIN))
            with self.assertRaises(sessions.MigrationBusyError):
                with sessions.launcher_write_guard(self.root):
                    pass
            release.set()
            worker.join(JOIN)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["code"][0], 0)

    def test_token_change_between_prepass_and_restore_stops_with_the_marker_kept(self) -> None:
        self.migrate(v3_managed(M1), v3_managed(M5))
        real_lock = sessions.SessionStore.lifecycle_lock
        injected = []

        def lock(store, session_id):
            if session_id == M5 and not injected:
                injected.append(True)
                raw, _data = store.load_raw(M5)
                raw["mutation_token"] = sessions.new_mutation_token()
                _write_raw(store, raw)
            return real_lock(store, session_id)

        with mock.patch.object(sessions.SessionStore, "lifecycle_lock", autospec=True,
                               side_effect=lock):
            code, output = self.restore()
        self.assertEqual(code, 1)
        self.assertIn(
            f"claude-multi: restore-2x stopped: {M5[:8]} changed while restoring", output
        )
        self.assertIn("the marker is still 4 — rerun claude-multi restore-2x", output)
        self.assertTrue(self.marker())
        self.assert_226_readable(M1)  # the first record was restored already
        code, output = self.restore()
        self.assertEqual(code, 0, output)
        self.assert_226_readable(M5)

    def test_a_new_record_before_the_rescan_stops(self) -> None:
        self.migrate(v3_managed(M1))
        real = migrate._restore_pointers

        def pointers(store, *args):
            _write_raw(store, _v4_born(M6))
            return real(store, *args)

        with mock.patch.object(migrate, "_restore_pointers", side_effect=pointers):
            code, output = self.restore()
        self.assertEqual(code, 1)
        self.assertIn(f"new record {M6} appeared", output)
        self.assertTrue(self.marker())

    def test_rerun_after_a_failure_at_record_two_completes(self) -> None:
        self.migrate(v3_managed(M1), v3_managed(M5), v3_managed(M6))
        real = sessions.SessionStore._save_lifecycle
        calls = []

        def save(store, document):
            calls.append(document["managed_id"])
            if len(calls) == 2:
                raise state.StateError(28, "no space left")
            return real(store, document)

        with mock.patch.object(sessions.SessionStore, "_save_lifecycle", autospec=True,
                               side_effect=save):
            code, output = self.restore()
        self.assertEqual(code, 1)
        self.assertIn("restore failed", output)
        self.assertTrue(self.marker())
        code, output = self.restore()
        self.assertEqual(code, 0, output)
        for managed in (M1, M5, M6):
            self.assert_226_readable(managed)
        self.assertIn(f"kept         {M1[:8]}  already legacy", output)


class MarkEndedTests(RestoreTestCase):
    def runtime(self) -> cli.Runtime:
        return cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.environ, cwd=self.tmp)

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        """``(exit, stdout then stderr)``."""

        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = cli.main(argv, runtime=self.runtime(), output_stream=output, interactive=False)
        return code, output.getvalue() + errors.getvalue()

    def test_mark_ended_writes_a_synthetic_end_in_the_record_version(self) -> None:
        _write_raw(self.store, v3_managed(M1, last_event_source="resume"))
        before_token = self.store.load(M1)["mutation_token"]
        code, output = self.run_cli(["sessions", "mark-ended", M1])
        self.assertEqual(code, 0, output)
        self.assertEqual(output, f"{M1}  marked ended\n")
        raw, _data = self.store.load_raw(M1)
        self.assertEqual(raw["version"], 3)
        self.assertEqual(raw["last_event_source"], "end")
        self.assertEqual(raw["last_end_reason"], sessions.MARKED_ENDED_REASON)
        self.assertEqual(raw["mutation_token"], before_token)
        code, output = self.run_cli(["sessions", "mark-ended", M1])
        self.assertEqual(output, f"{M1}  already ended\n")

    def test_live_sessions_are_refused(self) -> None:
        _write_raw(self.store, v3_managed(M1, last_event_source="resume"))
        self.proc.return_value = frozenset({M1})
        code, output = self.run_cli(["sessions", "mark-ended", M1])
        self.assertEqual(code, 1)
        self.assertIn("refused: session", output)
        self.assertEqual(self.store.load(M1)["last_event_source"], "resume")

    def test_all_dead_skips_live_and_ended(self) -> None:
        self.migrate(
            v3_managed(M1, last_event_source="resume"),
            v3_managed(M5, last_event_source="end"),
            v3_managed(M6, last_event_source=None),
        )
        self.prefixes.return_value = frozenset({M6[:8]})
        code, output = self.run_cli(["sessions", "mark-ended", "--all-dead"])
        self.assertEqual(code, 0, output)
        self.assertEqual(output, f"{M1}  marked ended\n")
        self.assertEqual(self.store.load(M1)["version"], 4)
        self.assertEqual(self.store.load(M6)["last_event_source"], None)

    def test_unreadable_process_table_refuses_and_writes_nothing(self) -> None:
        _write_raw(self.store, v3_managed(M1, last_event_source="resume"))
        before = self.store.load_raw(M1)[1]
        # The real scan of a missing /proc, through the shared adapter.
        self.scan.side_effect = _REAL_PROC_SESSION_SCAN
        results = claude_multi.cli.session_actions._mark_ended_ids(self.runtime(), [M1], proc_root=self.tmp / "no-proc")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][1].startswith(
            f"refused: cannot tell whether session {M1} is running (the process table is "
            "unreadable: FileNotFoundError); nothing written"), results)
        self.scan.side_effect = None
        self.scan.return_value = _UNREADABLE
        code, output = self.run_cli(["sessions", "mark-ended", M1])
        self.assertEqual(code, 1, output)
        self.assertIn(f"{M1}  refused: cannot tell whether session", output)
        code, output = self.run_cli(["sessions", "mark-ended", "--all-dead"])
        self.assertEqual(code, 1, output)
        self.assertEqual(output, "claude-multi: cannot tell which sessions are running (the process table is "
                                 "unreadable: PermissionError); nothing marked\n")
        self.assertEqual(self.store.load_raw(M1)[1], before)
        self.assertEqual(self.store.load(M1)["last_event_source"], "resume")

    def _rotate_token(self, *, refused: bool) -> str:
        """Run the real rotation with a rendered-config gateway and a virtual clock."""
        from test_proxy import FakeClock

        runtime = self.runtime()
        directory = proxy.config_dir(runtime.home)
        key, previous, config = (directory / name for name in ("api-key", "previous-key", "config.yaml"))
        old = key.read_bytes()
        clock = FakeClock()
        before_retirement = {}
        seen_keys = []

        def served(_gateway, token):
            yaml = config.read_text()
            block = yaml.split("api-keys:\n", 1)[1].split("debug:", 1)[0]
            keys = re.findall(r'"([0-9a-f]{64})"', block)
            seen_keys.append(tuple(keys))
            ids = set(re.findall(r'alias: "(claude-multi-render-[0-9a-f]+)"', yaml))
            return (ids, 200) if token in keys else (set(), 401)

        def sleep(seconds):
            before_retirement.update({path: path.read_bytes() for path in (key, previous, config)})
            clock.sleep(seconds)

        runtime.served_models_callback = served
        runtime.listener_owner = lambda _base: service.OwnerVerdict("ours", "fixture gateway")
        output = io.StringIO()
        code = cli._doctor_rotate_token(
            runtime, input_stream=io.StringIO("y\ny\n"), output_stream=output,
            interactive=True, clock=clock, sleep=sleep,
        )
        text = output.getvalue()
        self.assertEqual(code, 1 if refused else 0, text)
        self.assertEqual(clock.now, proxy.HELPER_TTL_SECONDS + 5)
        self.assertNotEqual(key.read_bytes(), old)
        self.assertEqual(before_retirement[previous], old)
        old_token, new_token = old.decode().strip(), key.read_text().strip()
        self.assertEqual(seen_keys[0], (old_token, new_token))
        if refused:
            self.assertEqual({path: path.read_bytes() for path in (key, previous, config)}, before_retirement)
            self.assertTrue(all(keys == (old_token, new_token) for keys in seen_keys))
            self.assertEqual(proxy.gateway_api_keys(runtime.home), (old_token, new_token))
            self.assertEqual(served(None, old_token)[1], 200)
            self.assertNotIn("step 4/4", text)
            self.assertNotIn("previous key is retired", text)
            self.assertIn("rotation paused", text)
            self.assertIn("resume with `claude-multi doctor --rotate-token`", text)
        else:
            self.assertFalse(previous.exists())
            self.assertEqual(seen_keys[-1], (new_token,))
            self.assertEqual(served(None, old_token)[1], 401)
            self.assertIn("previous key is retired (old=401, new=200)", text)
        self.assertEqual(served(None, new_token)[1], 200)
        self.assertNotIn(old_token, text)
        self.assertNotIn(new_token, text)
        return text

    def test_rotation_dead_confirm_refuses_with_an_unreadable_process_table(self) -> None:
        _write_raw(self.store, v3_managed(M1, last_event_source="resume", launcher_version="2.25.1"))
        before = self.store.load_raw(M1)[1]
        # Keep the real scan and fail it on a fixture-only unavailable backend.
        self.scan.side_effect = lambda *_a, **_kw: _REAL_PROC_SESSION_SCAN(self.tmp / "no-proc")
        output = self._rotate_token(refused=True)
        self.assertIn(f"{M1}: refused: cannot tell whether session", output)
        self.assertIn("process table is unreadable: FileNotFoundError", output)
        self.assertEqual(self.store.load_raw(M1)[1], before)

    def _assert_mark_ended_refused(self, refusal: str, all_dead: str) -> None:
        before = self.store.load_raw(M1)[1]
        self.assertTrue(sessions.may_hold_env_token(self.store.load(M1)))
        results = claude_multi.cli.session_actions._mark_ended_ids(
            self.runtime(), [M1], proc_root=self.tmp / "unused-proc")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][1].startswith(f"refused: {refusal}; nothing written"), results)
        code, output = self.run_cli(["sessions", "mark-ended", M1])
        self.assertEqual(code, 1, output)
        self.assertIn(f"{M1}  refused: {refusal}", output)
        code, output = self.run_cli(["sessions", "mark-ended", "--all-dead"])
        self.assertEqual(code, 1, output)
        self.assertEqual(output, f"claude-multi: {all_dead}; nothing marked\n")
        self.assertEqual(self.store.load_raw(M1)[1], before)
        self.assertTrue(sessions.may_hold_env_token(self.store.load(M1)))

    def test_unobservable_daemon_liveness_refuses_and_writes_nothing(self) -> None:
        # A daemon-live session behind an unobservable daemon
        # root is unknown, never "not live", even with a known-empty process table.
        _write_raw(self.store, v3_managed(M1, last_event_source="resume", launcher_version="2.25.1"))
        proc_root = self.tmp / "proc"
        proc_root.mkdir()
        self.scan.side_effect = lambda *_a, **_kw: _REAL_PROC_SESSION_SCAN(proc_root)
        root = _daemon_fixture(self.tmp, M1, symlink=True)
        for reason, daemon in (
            ("symlink in daemon root", lambda *_a, **_kw: _REAL_BACKGROUND_LIVENESS(root)),
            ("PermissionError", lambda *_a, **_kw: sessions.BackgroundLiveness(False, frozenset(), "PermissionError")),
        ):
            with self.subTest(reason=reason):
                self.daemon.side_effect = daemon
                self._assert_mark_ended_refused(
                    f"cannot tell whether session {M1} is live in the background ({reason})",
                    f"cannot tell which sessions are live in the background ({reason})",
                )

    def test_an_unreadable_process_argv_refuses_and_writes_nothing(self) -> None:
        # An argv that cannot be opened or read is unknown.
        _write_raw(self.store, v3_managed(M1, last_event_source="resume", launcher_version="2.25.1"))
        proc_root = _argv_unreadable_proc(self.tmp)
        self.scan.side_effect = lambda *_a, **_kw: _REAL_PROC_SESSION_SCAN(proc_root)
        refusal = f"cannot tell whether session {M1} is running ({_ARGV_UNREADABLE})"
        self._assert_mark_ended_refused(refusal, f"cannot tell which sessions are running ({_ARGV_UNREADABLE})")
        open_root = self.tmp / "fake-proc-open"
        (open_root / "4242").mkdir(parents=True)
        (open_root / "4242" / "cmdline").write_bytes(b"")
        real_open = os.open

        def refuse_4242(path, *args, **kwargs):
            if str(path).endswith("/4242/cmdline"):
                raise PermissionError(errno.EACCES, "fixture argv refused")
            return real_open(path, *args, **kwargs)

        self.scan.side_effect = lambda *_a, **_kw: _REAL_PROC_SESSION_SCAN(open_root)
        with mock.patch.object(os, "open", side_effect=refuse_4242):
            self._assert_mark_ended_refused(
                f"cannot tell whether session {M1} is running (the process table is unreadable: "
                "PermissionError on process 4242)",
                "cannot tell which sessions are running (the process table is unreadable: "
                "PermissionError on process 4242)",
            )

    def test_rotation_dead_confirm_refuses_with_unobservable_daemon_liveness(self) -> None:
        _write_raw(self.store, v3_managed(M1, last_event_source="resume", launcher_version="2.25.1"))
        before = self.store.load_raw(M1)[1]
        proc_root = self.tmp / "proc"
        proc_root.mkdir()
        self.scan.side_effect = lambda *_a, **_kw: _REAL_PROC_SESSION_SCAN(proc_root)
        root = _daemon_fixture(self.tmp, None, symlink=True)
        self.daemon.side_effect = lambda *_a, **_kw: _REAL_BACKGROUND_LIVENESS(root)
        output = self._rotate_token(refused=True)
        self.assertIn(f"{M1}: refused: cannot tell whether session {M1} is live in the background "
                      "(symlink in daemon root)", output)
        self.assertEqual(self.store.load_raw(M1)[1], before)

    def test_rotation_dead_confirm_refuses_with_an_unreadable_process_argv(self) -> None:
        _write_raw(self.store, v3_managed(M1, last_event_source="resume", launcher_version="2.25.1"))
        before = self.store.load_raw(M1)[1]
        proc_root = _argv_unreadable_proc(self.tmp)
        self.scan.side_effect = lambda *_a, **_kw: _REAL_PROC_SESSION_SCAN(proc_root)
        output = self._rotate_token(refused=True)
        self.assertIn(f"{M1}: refused: cannot tell whether session {M1} is running "
                      f"({_ARGV_UNREADABLE})", output)
        self.assertEqual(self.store.load_raw(M1)[1], before)

    def test_mark_ended_waits_for_no_migration(self) -> None:
        _write_raw(self.store, v3_managed(M1, last_event_source="resume"))
        holder = sessions.migration_lock(self.root)
        self.assertTrue(holder.acquire(blocking=False))
        self.addCleanup(holder.release)
        code, output = self.run_cli(["sessions", "mark-ended", M1])
        self.assertEqual(code, 1)
        self.assertIn(sessions.MIGRATION_BUSY_TEXT, output)

    def test_parser_requires_one_target(self) -> None:
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            cli.build_parser().parse_args(["sessions", "mark-ended"])
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            cli.build_parser().parse_args(["sessions", "mark-ended", M1, "--all-dead"])

    def test_rotation_dead_confirm_marks_the_confirmed_sessions_ended(self) -> None:
        pre = v3_managed(M1, last_event_source="resume", launcher_version="2.25.1")
        _write_raw(self.store, pre)
        proc_root = self.tmp / "proc"
        proc_root.mkdir()
        self.scan.side_effect = lambda *_a, **_kw: _REAL_PROC_SESSION_SCAN(proc_root)
        output = self._rotate_token(refused=False)
        self.assertIn(f"{M1}: marked ended", output)
        self.assertEqual(self.store.load(M1)["last_event_source"], "end")


class ProcSessionIdsTests(unittest.TestCase):
    def test_reads_session_flags_from_a_proc_tree(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="claude-multi-proc-"))
        self.addCleanup(shutil.rmtree, root, True)
        cmdlines = {
            "101": b"claude\0--session-id\0" + M1.encode() + b"\0--settings\0x\0",
            "102": b"claude\0--resume=" + M2.encode() + b"\0",
            "103": b"claude\0--resume\0not-a-uuid\0",
            "104": b"vim\0" + M3.encode() + b"\0",
            "self": b"claude\0--resume\0" + M4.encode() + b"\0",
        }
        for pid, data in cmdlines.items():
            (root / pid).mkdir()
            (root / pid / "cmdline").write_bytes(data)
        (root / "105").mkdir()  # no cmdline: ignored
        self.assertEqual(sessions.proc_session_ids(root), frozenset({M1, M2}))
        self.assertEqual(sessions.proc_session_ids(root / "missing"), frozenset())
        record = {"managed_id": M5, "runtime_session_id": M5,
                  "runtime_aliases": [{"session_id": M2}]}
        self.assertTrue(sessions.record_is_proc_live(record, frozenset({M2})))
        self.assertFalse(sessions.record_is_proc_live(record, frozenset({M1})))

    def test_live_background_prefixes_moved_with_a_cli_delegate(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="claude-multi-daemon-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "x" / "pty").mkdir(parents=True)
        (root / "x" / "pty" / "abcdef12.sock").write_bytes(b"")
        self.assertEqual(sessions.live_background_prefixes(root), frozenset({"abcdef12"}))
        self.assertEqual(cli._live_background_prefixes(root), frozenset({"abcdef12"}))


class RealPerformRaceTests(V4Case):
    """restore-2x vs a real perform.

    Real FileLocks and threads: the resume goes through ``Runtime.prepare``
    -> ``Runtime.perform`` -> ``launch.perform_launch`` with the exec seam.
    """

    def setUp(self) -> None:
        super().setUp()
        for name in ("live_background_prefixes", "proc_session_ids"):
            patcher = mock.patch.object(sessions, name, return_value=frozenset())
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(sessions, "proc_session_scan",
                                    return_value=sessions.ProcessScan(True, frozenset()))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(sessions, "background_liveness",
                                    return_value=sessions.BackgroundLiveness(True, frozenset()))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.record = self.launch_fresh()
        self.mid = self.record["managed_id"]
        self.mutate(self.mid, last_event_source="end")

    def restore(self, argv: list[str] = ()) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.store.root.parent),
                                          "XDG_CONFIG_HOME": str(self.store.root.parent / "config"),
                                          "XDG_STATE_HOME": str(self.store.root.parent / "state"),
                                          "XDG_DATA_HOME": str(self.store.root.parent / "data")}), \
                mock.patch.object(claude_multi.sessions, "state_root", return_value=self.store.root):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = cli.main(["restore-2x", *argv], output_stream=output, interactive=False)
        return code, output.getvalue() + errors.getvalue()

    def marker(self) -> bool:
        return os.path.lexists(self.store.root / sessions.STATE_MARKER)

    def test_a_perform_parked_at_exec_makes_restore_wait(self) -> None:
        prepared = self.prepare_resume(self.mid)
        at_exec, release = threading.Event(), threading.Event()

        def parked(path, argv, env):
            at_exec.set()
            release.wait(JOIN)
            return 0

        self.runtime.execve = parked
        launcher = threading.Thread(target=lambda: self.runtime.perform(prepared))
        launcher.start()
        self.assertTrue(at_exec.wait(JOIN))
        result: dict = {}
        worker = threading.Thread(
            target=lambda: result.update(code=self.restore(["--not-running", self.mid]))
        )
        worker.start()
        time.sleep(0.3)
        self.assertTrue(worker.is_alive(), "restore did not wait for perform's shared hold")
        self.assertTrue(self.marker())
        release.set()
        launcher.join(JOIN)
        worker.join(JOIN)
        self.assertFalse(launcher.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["code"][0], 0, result["code"][1])
        self.assertFalse(self.marker())

    def test_a_resume_started_after_restore_took_the_lock_refuses_with_l4(self) -> None:
        prepared = self.prepare_resume(self.mid)
        entered, release = threading.Event(), threading.Event()
        real = cli._record_view_is_live

        def slow_live(view, **kwargs):
            # The preview runs lock-free; the commit phase's
            # revalidation (EX migration lock held) is where restore waits.
            if claude_multi.hooks.migration_lock_held(self.store.root):
                entered.set()
                release.wait(JOIN)
            return real(view, **kwargs)

        result: dict = {}
        before = self.record_bytes(self.mid)
        execs = len(self.execs)
        with mock.patch('claude_multi.cli.session_facts._record_view_is_live', side_effect=slow_live):
            worker = threading.Thread(target=lambda: result.update(code=self.restore()))
            worker.start()
            try:
                self.assertTrue(entered.wait(JOIN))
                with self.assertRaisesRegex(launch.LaunchError, "migration or restore in progress"):
                    self.runtime.perform(prepared)
                self.assertEqual(len(self.execs), execs)
                self.assertEqual(self.record_bytes(self.mid), before)
            finally:
                release.set()
                worker.join(JOIN)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["code"][0], 0, result["code"][1])

    def test_a_resume_committed_before_restore_makes_restore_refuse(self) -> None:
        self.resume(self.mid)
        self.assertIsNone(self.store.load(self.mid)["last_event_source"])
        code, output = self.restore()
        self.assertEqual(code, 1, output)
        self.assertIn("restore-2x refused", output)
        self.assertIn(f"{self.mid[:8]} (last event none)", output)
        self.assertTrue(self.marker())


if __name__ == "__main__":
    unittest.main()


class RestoreMetadataCheckTests(RestoreTestCase):
    def test_restore_2x_check_writes_nothing(self):
        self.migrate(v3_managed(M1))
        before = self.tree()
        with mock.patch.object(sessions.SessionStore, 'lifecycle_lock', side_effect=AssertionError('no lock')):
            code, text = self.restore(['--check', '--json'])
        self.assertEqual(code, 0, text)
        document = strict_json.loads(text)
        self.assertEqual(document['transcripts'], 'unknown')
        self.assertEqual(document['target'], '2.x')
        self.assertEqual(before, self.tree())
        missing = self.tmp / 'empty-home' / 'state'
        migrate.restore_check(missing, self.tmp / 'empty-home/config')
        self.assertFalse(missing.parent.exists())

    def test_restore_2x_check_incomplete_backup_is_unknown_or_refused(self):
        self.migrate(v3_managed(M1))
        self.store.backup_path(M1).unlink()
        before = self.tree()
        code, text = self.restore(['--check', '--json'])
        self.assertEqual(code, 1, text)
        self.assertEqual(strict_json.loads(text)['status'], 'refused')
        self.assertEqual(before, self.tree())
