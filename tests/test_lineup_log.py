"""The out-of-scope lineup log.

``<state>/lineup-log/<managed_id>.log``: private, bounded with one rotation,
symlink/foreign-file refusing, flock-serialised appends, a removal helper for
the ``forget``/``doctor --prune`` cleanup. Temp state roots only.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from claude_multi import lineup_files, lineup_log, state, strict_json
from claude_multi.lineup_log import LineupLogError
from _layout import REPO_ROOT

MID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-lineuplog-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.log = lineup_files.lineup_log_path(self.root, MID)
        self.rotated = lineup_files.lineup_log_rotated_path(self.root, MID)

    def _lines(self, path: Path) -> list[dict]:
        return [strict_json.loads(line) for line in path.read_bytes().splitlines()]


class AppendTests(_Case):
    def test_creates_private_dir_and_file_with_canonical_lines(self) -> None:
        path = lineup_log.append(self.root, MID, {"event": "subagent-start", "b": 1, "a": "x"})
        self.assertEqual(path, self.root / "lineup-log" / f"{MID}.log")
        self.assertEqual(stat.S_IMODE(os.lstat(path.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        first = path.read_bytes()
        self.assertEqual(
            first, strict_json.canonical_bytes({"a": "x", "b": 1, "event": "subagent-start"}) + b"\n"
        )
        lineup_log.append(self.root, MID, {"event": "apply"})
        data = path.read_bytes()
        self.assertTrue(data.startswith(first))
        self.assertEqual([line["event"] for line in self._lines(path)], ["subagent-start", "apply"])

    def test_created_0600_under_a_permissive_umask(self) -> None:
        old = os.umask(0o000)
        try:
            lineup_log.append(self.root, MID, {"event": "x"})
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.lstat(self.log).st_mode), 0o600)

    def test_invalid_inputs_write_nothing(self) -> None:
        for bad_id in ("../x", "not-a-uuid", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", "", None):
            with self.subTest(managed_id=bad_id):
                with self.assertRaises(LineupLogError):
                    lineup_log.append(self.root, bad_id, {"event": "x"})
        with self.assertRaises(LineupLogError):
            lineup_log.append(self.root, MID, {"event": "x" * lineup_log.LOG_LINE_MAX})
        with self.assertRaises(LineupLogError):
            lineup_log.append(self.root, MID, ["not", "an", "object"])
        with self.assertRaises(LineupLogError):
            lineup_log.append(self.root, MID, {"bad": float("nan")})
        self.assertFalse((self.root / "lineup-log").exists())

    def test_line_limit_is_inclusive(self) -> None:
        overhead = len(strict_json.canonical_bytes({"e": ""})) + 1
        record = {"e": "y" * (lineup_log.LOG_LINE_MAX - overhead)}
        lineup_log.append(self.root, MID, record)
        self.assertEqual(len(self.log.read_bytes()), lineup_log.LOG_LINE_MAX)

    def test_binding_label(self) -> None:
        self.assertEqual(lineup_log.binding_label(7), "binding at gen 7 (reload unconfirmed)")
        for bad in (0, -1, True, "7", None):
            with self.subTest(generation=bad), self.assertRaises(LineupLogError):
                lineup_log.binding_label(bad)


class UnsafeLogTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        state.ensure_private_dir(self.log.parent)

    def test_symlinked_log_is_refused_and_never_followed(self) -> None:
        target = self.root / "target"
        target.write_bytes(b"original\n")
        os.chmod(target, 0o600)
        self.log.symlink_to(target)
        with self.assertRaises(OSError):
            lineup_log.append(self.root, MID, {"event": "x"})
        self.assertEqual(target.read_bytes(), b"original\n")
        dangling = lineup_files.lineup_log_path(self.root, OTHER)
        dangling.symlink_to(self.root / "missing")
        with self.assertRaises(OSError):
            lineup_log.append(self.root, OTHER, {"event": "x"})
        self.assertFalse((self.root / "missing").exists())

    def test_directory_fifo_and_group_readable_logs_are_refused(self) -> None:
        self.log.mkdir()
        with self.assertRaises(OSError):
            lineup_log.append(self.root, MID, {"event": "x"})
        self.log.rmdir()
        os.mkfifo(self.log, 0o600)
        with self.assertRaises(OSError):  # never blocks waiting for a reader
            lineup_log.append(self.root, MID, {"event": "x"})
        self.log.unlink()
        self.log.write_bytes(b"old\n")
        os.chmod(self.log, 0o644)
        with self.assertRaises(state.StateError):
            lineup_log.append(self.root, MID, {"event": "x"})
        self.assertEqual(self.log.read_bytes(), b"old\n")

    def test_symlinked_log_directory_is_refused(self) -> None:
        shutil.rmtree(self.log.parent)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.log.parent.symlink_to(elsewhere)
        with self.assertRaises(state.StateError):
            lineup_log.append(self.root, MID, {"event": "x"})
        self.assertEqual(list(elsewhere.iterdir()), [])


class RotationTests(_Case):
    def test_rotates_once_and_drops_the_older_rotation(self) -> None:
        line = lambda n: {"n": n, "pad": "p" * 40}  # noqa: E731
        size = len(lineup_log.encode_line(line(0)))
        cap = size * 3
        for n in range(3):
            lineup_log.append(self.root, MID, line(n), max_bytes=cap)
        self.assertFalse(self.rotated.exists())
        lineup_log.append(self.root, MID, line(3), max_bytes=cap)
        self.assertEqual([r["n"] for r in self._lines(self.rotated)], [0, 1, 2])
        self.assertEqual([r["n"] for r in self._lines(self.log)], [3])
        self.assertEqual(stat.S_IMODE(os.lstat(self.rotated).st_mode), 0o600)
        for n in range(4, 7):
            lineup_log.append(self.root, MID, line(n), max_bytes=cap)
        self.assertEqual([r["n"] for r in self._lines(self.rotated)], [3, 4, 5])
        self.assertEqual([r["n"] for r in self._lines(self.log)], [6])

    def test_default_bound_is_one_mebibyte(self) -> None:
        self.assertEqual(lineup_files.LINEUP_LOG_MAX_BYTES, 1024 * 1024)


class ConcurrencyTests(_Case):
    def _hammer(self, *, threads: int, lines: int, max_bytes: int) -> None:
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                for n in range(lines):
                    lineup_log.append(
                        self.root, MID, {"t": index, "n": n, "pad": "x" * 64}, max_bytes=max_bytes
                    )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join()
        self.assertEqual(errors, [])

    def test_no_line_is_lost_or_torn(self) -> None:
        self._hammer(threads=20, lines=25, max_bytes=lineup_files.LINEUP_LOG_MAX_BYTES)
        records = self._lines(self.log)
        self.assertEqual(len(records), 500)
        self.assertEqual({(r["t"], r["n"]) for r in records}, {(t, n) for t in range(20) for n in range(25)})

    def test_concurrent_rotation_keeps_every_file_bounded_and_intact(self) -> None:
        cap = 2048
        self._hammer(threads=10, lines=40, max_bytes=cap)
        for path in (self.log, self.rotated):
            data = path.read_bytes()
            self.assertLessEqual(len(data), cap)
            self.assertTrue(data.endswith(b"\n"))
            for record in self._lines(path):
                self.assertEqual(set(record), {"n", "pad", "t"})

    def test_separate_processes_append_intact_lines(self) -> None:
        script = (
            "import sys; from claude_multi import lineup_log\n"
            "for n in range(10):\n"
            "    lineup_log.append(sys.argv[1], sys.argv[2], {'p': int(sys.argv[3]), 'n': n})\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.root), MID, str(p)], env=env
            )
            for p in range(8)
        ]
        self.assertEqual([proc.wait(timeout=60) for proc in procs], [0] * 8)
        records = self._lines(self.log)
        self.assertEqual(len(records), 80)


class RemoveAndListTests(_Case):
    def test_remove_both_files_idempotently(self) -> None:
        self.assertFalse(lineup_log.remove(self.root, MID))
        self.assertFalse((self.root / "lineup-log").exists())  # never created
        lineup_log.append(self.root, MID, {"n": 0}, max_bytes=1)
        lineup_log.append(self.root, MID, {"n": 1}, max_bytes=1)
        lineup_log.append(self.root, OTHER, {"n": 0})
        self.assertTrue(self.log.exists() and self.rotated.exists())
        self.assertEqual(lineup_log.log_ids(self.root), sorted([MID, OTHER]))
        self.assertTrue(lineup_log.remove(self.root, MID))
        self.assertFalse(self.log.exists() or self.rotated.exists())
        self.assertFalse(lineup_log.remove(self.root, MID))
        self.assertEqual(lineup_log.log_ids(self.root), [OTHER])

    def test_remove_refuses_symlinks_and_bad_ids(self) -> None:
        state.ensure_private_dir(self.log.parent)
        target = self.root / "target"
        target.write_bytes(b"keep")
        self.log.symlink_to(target)
        with self.assertRaises(state.StateError):
            lineup_log.remove(self.root, MID)
        self.assertEqual(target.read_bytes(), b"keep")
        with self.assertRaises(LineupLogError):
            lineup_log.remove(self.root, "../etc")

    def test_log_ids_ignores_other_names(self) -> None:
        self.assertEqual(lineup_log.log_ids(self.root), [])
        lineup_log.append(self.root, MID, {"n": 0})
        for junk in ("notes.txt", "x.log", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA.log", f"{OTHER}.log.2"):
            (self.log.parent / junk).write_bytes(b"")
        self.assertEqual(lineup_log.log_ids(self.root), [MID])


class ImportLayeringTests(unittest.TestCase):
    def test_imports_no_compiler_chain(self) -> None:
        # hooks.py must stay free of cli/compiler/catalog/profile/scope;
        # the log module is its write path.
        code = (
            "import sys, claude_multi.lineup_log\n"
            "print(sorted(m for m in sys.modules if m.startswith('claude_multi.')))\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(
            out.strip(),
            str(
                [
                    "claude_multi.errors",
                    "claude_multi.lineup_files",
                    "claude_multi.lineup_log",
                    # Reviewed leaves under state: the empty platform
                    # package and the POSIX primitives, never a backend.
                    "claude_multi.platform",
                    "claude_multi.platform.posix_fs",
                    "claude_multi.state",
                    "claude_multi.strict_json",
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()


class ReadObservationTests(unittest.TestCase):
    def test_lineup_reader_handles_rotation_and_partial_tail(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mid = "11111111-1111-4111-8111-111111111111"
            row = {"event": "subagent-start", "time": datetime.now(timezone.utc).isoformat(),
                   "lineup_gen": "2 abcdef012345", "agent_type": "cm-analyst", "agent_id": "fixture-agent",
                   "scope_selector": "fixture-alias", "raw": "private@example.invalid"}
            path = lineup_log.append(root, mid, row)
            rotated = path.with_name(path.name + ".1")
            path.rename(rotated)
            lineup_log.append(root, mid, row)
            with path.open("ab") as out:
                out.write(b'{"event":')
            result = lineup_log.read(root, mid)
            self.assertEqual(len(result.events), 2)
            self.assertEqual(result.coverage, "partial")
            self.assertIn("reload unconfirmed", result.events[0].label)
            self.assertNotIn("private", repr(result))

    def test_lineup_reader_distinguishes_absent_from_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mid = "11111111-1111-4111-8111-111111111111"
            result = lineup_log.read(root, mid)
            self.assertEqual((result.coverage, result.events), ("absent", ()))
            self.assertEqual(list(root.iterdir()), [])
            folder = root / "lineup-log"
            folder.mkdir()
            for suffix in (".log", ".log.1"):
                path = folder / (mid + suffix)
                path.touch()
                result = lineup_log.read(root, mid)
                self.assertEqual((result.coverage, result.events), ("partial", ()))
                path.unlink()

    def test_lineup_reader_refuses_symlink_fifo_and_oversize(self):
        from claude_multi.lineup_files import LINEUP_LOG_MAX_BYTES
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mid = "11111111-1111-4111-8111-111111111111"
            folder = root / "lineup-log"
            folder.mkdir()
            path = folder / (mid + ".log")
            for kind in ("symlink", "fifo", "oversize"):
                if kind == "symlink":
                    path.symlink_to(root / "absent")
                elif kind == "fifo":
                    os.mkfifo(path)
                else:
                    with path.open("wb") as out:
                        out.truncate(LINEUP_LOG_MAX_BYTES + 1)
                self.assertEqual(lineup_log.read(root, mid).coverage, "unavailable")
                path.unlink()


class LogDurabilityTests(unittest.TestCase):
    def test_lineup_log_best_effort_durability_does_not_fail_hook(self):
        with tempfile.TemporaryDirectory(prefix='cm-log-') as temporary:
            with mock.patch('os.fsync', side_effect=OSError('fixture unsupported sync')):
                lineup_log._fsync_dir(Path(temporary))
            with mock.patch('os.open', side_effect=OSError('fixture unsupported directory open')):
                lineup_log._fsync_dir(Path(temporary))
