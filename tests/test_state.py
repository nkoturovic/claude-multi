"""Tests for owner-controlled symlink-safe state primitives."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import state
from claude_multi.state import StateError


class StateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-state-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                os.chmod(path, 0o700)
                path.rmdir()
        self.root.rmdir()

    def _dir(self, name: str = "state") -> Path:
        return state.ensure_private_dir(self.root / name)


class NameValidationTests(StateTestCase):
    def test_safe_names_accepted(self) -> None:
        for name in ("default", "my-composition", "a.b_c", "x" * 64):
            self.assertEqual(state.check_name(name), name)

    def test_unsafe_names_rejected(self) -> None:
        for name in ("", "../x", "a/b", "Absolute", "-lead", ".hidden", "x" * 65):
            with self.assertRaises(StateError, msg=f"name {name!r} must be rejected"):
                state.check_name(name)


class AtomicWriteTests(StateTestCase):
    def test_roundtrip_mode_0600(self) -> None:
        directory = self._dir()
        target = directory / "session.json"
        state.atomic_write(target, b'{"ok": true}\n')
        self.assertEqual(state.read_private(target), b'{"ok": true}\n')
        mode = stat.S_IMODE(os.lstat(target).st_mode)
        self.assertEqual(mode, 0o600)

    def test_overwrite_replaces_content(self) -> None:
        directory = self._dir()
        target = directory / "session.json"
        state.atomic_write(target, b"first")
        state.atomic_write(target, b"second")
        self.assertEqual(state.read_private(target), b"second")

    def test_symlink_target_rejected(self) -> None:
        directory = self._dir()
        real = directory / "real.json"
        state.atomic_write(real, b"data")
        link = directory / "link.json"
        link.symlink_to(real)
        with self.assertRaises(StateError):
            state.atomic_write(link, b"nope")
        with self.assertRaises(StateError):
            state.read_private(link)
        self.assertEqual(state.read_private(real), b"data")

    def test_symlink_parent_rejected(self) -> None:
        directory = self._dir("real-state")
        link = self.root / "link-state"
        link.symlink_to(directory)
        with self.assertRaises(StateError):
            state.atomic_write(link / "session.json", b"nope")

    def test_non_regular_target_rejected(self) -> None:
        directory = self._dir()
        (directory / "subdir").mkdir()
        with self.assertRaises(StateError):
            state.atomic_write(directory / "subdir", b"nope")

    def test_group_accessible_parent_rejected(self) -> None:
        directory = self._dir()
        os.chmod(directory, 0o750)
        with self.assertRaises(StateError):
            state.atomic_write(directory / "session.json", b"nope")

    def test_group_accessible_file_rejected_on_read(self) -> None:
        directory = self._dir()
        target = directory / "session.json"
        state.atomic_write(target, b"data")
        os.chmod(target, 0o640)
        with self.assertRaises(StateError):
            state.read_private(target)

    def test_missing_parent_rejected(self) -> None:
        with self.assertRaises(StateError):
            state.atomic_write(self.root / "missing" / "session.json", b"nope")

    def test_failed_write_leaves_no_temp_files(self) -> None:
        directory = self._dir()
        os.chmod(directory, 0o500)
        with self.assertRaises(OSError):
            state.atomic_write(directory / "session.json", b"nope")
        os.chmod(directory, 0o700)
        leftovers = [p for p in directory.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_injected_post_temp_failure_cleans_up_and_preserves_target(self) -> None:
        directory = self._dir()
        target = directory / "session.json"
        state.atomic_write(target, b"original")
        with mock.patch.object(state.os, "replace", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                state.atomic_write(target, b"new")
        self.assertEqual(state.read_private(target), b"original")
        leftovers = [p for p in directory.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_post_replace_fsync_failure_reports_committed_state(self) -> None:
        directory = self._dir()
        target = directory / "session.json"
        state.atomic_write(target, b"original")
        with mock.patch.object(
            state, "_fsync_directory", side_effect=OSError("injected fsync")
        ):
            with self.assertRaisesRegex(
                state.CommittedStateError, "was replaced but directory fsync failed"
            ):
                state.atomic_write(target, b"new")
        self.assertEqual(state.read_private(target), b"new")
        leftovers = [p for p in directory.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])


class DirectoryTests(StateTestCase):
    def test_ensure_private_dir_creates_nested_0700(self) -> None:
        created = state.ensure_private_dir(self.root / "a" / "b")
        self.assertTrue(created.is_dir())
        self.assertEqual(stat.S_IMODE(os.lstat(created).st_mode) & 0o077, 0)
        self.assertEqual(
            stat.S_IMODE(os.lstat(self.root / "a").st_mode) & 0o077, 0
        )

    def test_child_first_creation_hardens_intermediate_parents(self) -> None:
        state.ensure_private_dir(self.root / "x" / "y" / "z")
        for part in ("x", "x/y", "x/y/z"):
            mode = stat.S_IMODE(os.lstat(self.root / part).st_mode)
            self.assertEqual(mode & 0o077, 0, f"{part} not hardened")

    def test_every_missing_ancestor_is_private_from_creation(self) -> None:
        mkdir = os.mkdir
        observed = []

        def record(path, mode=0o777, **kwargs):
            mkdir(path, mode, **kwargs)
            observed.append((Path(path).name, mode, os.stat(path).st_mode & 0o777))

        previous = os.umask(0o002)
        try:
            with mock.patch.object(os, "mkdir", side_effect=record):
                state.ensure_private_dir(self.root / "a" / "b" / "c")
        finally:
            os.umask(previous)
        self.assertEqual(observed, [(name, 0o700, 0o700) for name in ("a", "b", "c")])

    def test_ensure_private_dir_rejects_symlink(self) -> None:
        directory = self._dir("real")
        link = self.root / "link"
        link.symlink_to(directory)
        with self.assertRaises(StateError):
            state.ensure_private_dir(link)


class FileLockTests(StateTestCase):
    def test_lock_exclusion_and_release(self) -> None:
        directory = self._dir()
        target = directory / "last-session.json"
        first = state.FileLock(target)
        second = state.FileLock(target)
        self.assertTrue(first.acquire(blocking=False))
        self.assertFalse(second.acquire(blocking=False))
        first.release()
        self.assertTrue(second.acquire(blocking=False))
        second.release()

    def test_lock_context_manager(self) -> None:
        directory = self._dir()
        target = directory / "last-session.json"
        with state.FileLock(target):
            rival = state.FileLock(target)
            self.assertFalse(rival.acquire(blocking=False))
        self.assertTrue(state.FileLock(target).acquire(blocking=False))

    def test_lock_file_is_mode_0600_regular(self) -> None:
        directory = self._dir()
        target = directory / "last-session.json"
        lock = state.FileLock(target)
        lock.acquire(blocking=False)
        info = os.lstat(lock.lock_path)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        lock.release()

    def test_lock_rejects_symlinked_lock_file(self) -> None:
        directory = self._dir()
        target = directory / "last-session.json"
        real = directory / "elsewhere"
        state.atomic_write(real, b"x")
        (directory / "last-session.json.lock").symlink_to(real)
        with self.assertRaises(StateError):
            state.FileLock(target).acquire(blocking=False)


class SharedFileLockTests(StateTestCase):
    """``FileLock(shared=True)`` takes LOCK_SH on the same lock file."""

    def test_shared_holders_coexist_and_exclude_an_exclusive_one(self) -> None:
        directory = self._dir()
        target = directory / "migration"
        first = state.FileLock(target, shared=True)
        second = state.FileLock(target, shared=True)
        exclusive = state.FileLock(target)
        self.assertTrue(first.shared)
        self.assertFalse(exclusive.shared)
        self.assertEqual(first.lock_path, exclusive.lock_path)
        self.assertTrue(first.acquire(blocking=False))
        self.assertTrue(second.acquire(blocking=False))
        self.assertFalse(exclusive.acquire(blocking=False))
        first.release()
        self.assertFalse(exclusive.acquire(blocking=False))
        second.release()
        self.assertTrue(exclusive.acquire(blocking=False))
        exclusive.release()

    def test_an_exclusive_holder_refuses_a_non_blocking_shared_acquire(self) -> None:
        directory = self._dir()
        target = directory / "migration"
        exclusive = state.FileLock(target)
        self.assertTrue(exclusive.acquire(blocking=False))
        try:
            self.assertFalse(state.FileLock(target, shared=True).acquire(blocking=False))
        finally:
            exclusive.release()
        shared = state.FileLock(target, shared=True)
        self.assertTrue(shared.acquire(blocking=False))
        info = os.lstat(shared.lock_path)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        shared.release()

    def test_shared_lock_is_not_reentrant(self) -> None:
        lock = state.FileLock(self._dir() / "migration", shared=True)
        self.assertTrue(lock.acquire(blocking=False))
        try:
            with self.assertRaises(StateError):
                lock.acquire(blocking=False)
        finally:
            lock.release()



class OwnedReadableTests(StateTestCase):
    """descriptor-checked, bounded reads of operator-authored files."""

    def _plain(self, name: str, data: bytes = b"{}", mode: int = 0o644) -> Path:
        path = self.root / name
        path.write_bytes(data)
        os.chmod(path, mode)
        return path

    def test_default_owners_are_the_caller_and_root(self) -> None:
        self.assertEqual(state.readable_owner_uids(), frozenset({os.geteuid(), 0}))

    def test_shared_readable_file_and_symlink_to_it_are_read(self) -> None:
        target = self._plain("target.json", b'{"version": 1}', 0o644)
        link = self.root / "link.json"
        link.symlink_to(target)
        self.assertEqual(state.read_owned_readable(target, max_bytes=100), b'{"version": 1}')
        self.assertEqual(state.read_owned_readable(link, max_bytes=100), b'{"version": 1}')

    def test_group_or_other_writable_target_refused(self) -> None:
        for mode in (0o664, 0o646):
            with self.subTest(mode=oct(mode)):
                target = self._plain(f"w{mode:o}.json", mode=mode)
                link = self.root / f"l{mode:o}.json"
                link.symlink_to(target)
                for path in (target, link):
                    with self.assertRaisesRegex(StateError, "writable by group/other"):
                        state.read_owned_readable(path, max_bytes=100)

    def test_untrusted_owner_refused(self) -> None:
        target = self._plain("owned.json")
        with self.assertRaisesRegex(StateError, "not owned by you or root"):
            state.read_owned_readable(target, max_bytes=100, trusted_uids=frozenset({os.geteuid() + 1}))

    def test_oversize_refused(self) -> None:
        target = self._plain("big.json", b"x" * 101)
        self.assertEqual(len(state.read_owned_readable(target, max_bytes=101)), 101)
        with self.assertRaises(StateError) as caught:
            state.read_owned_readable(target, max_bytes=100)
        self.assertEqual(caught.exception.errno, errno.EFBIG)

    def test_fifo_and_directory_refused_without_blocking(self) -> None:
        fifo = self.root / "fifo.json"
        os.mkfifo(fifo, 0o600)
        self.addCleanup(lambda: fifo.unlink() if os.path.lexists(fifo) else None)
        with self.assertRaisesRegex(StateError, "not a regular file"):
            state.read_owned_readable(fifo, max_bytes=100)
        (self.root / "sub").mkdir(mode=0o700)
        with self.assertRaises((StateError, IsADirectoryError)):
            state.read_owned_readable(self.root / "sub", max_bytes=100)

    def test_dangling_link_is_absent(self) -> None:
        (self.root / "dangling.json").symlink_to(self.root / "missing.json")
        with self.assertRaises(FileNotFoundError):
            state.read_owned_readable(self.root / "dangling.json", max_bytes=100)

    def test_directory_descriptor_reads_and_symlinked_directory_is_reported(self) -> None:
        real = self.root / "real"
        real.mkdir(mode=0o755)
        os.chmod(real, 0o755)
        (real / "a.json").write_bytes(b"{}")
        os.chmod(real / "a.json", 0o444)
        link = self.root / "linked"
        link.symlink_to(real)
        for path, expected in ((real, False), (link, True)):
            descriptor, symlinked = state.open_owned_readable_dir(path)
            try:
                self.assertIs(symlinked, expected)
                self.assertEqual(state.read_owned_readable("a.json", max_bytes=10, dir_fd=descriptor), b"{}")
            finally:
                os.close(descriptor)

    def test_writable_directory_refused_and_absent_raises_not_found(self) -> None:
        shared = self.root / "shared"
        shared.mkdir()
        os.chmod(shared, 0o777)
        with self.assertRaisesRegex(StateError, "writable by group/other"):
            state.open_owned_readable_dir(shared)
        with self.assertRaises(FileNotFoundError):
            state.open_owned_readable_dir(self.root / "absent")
        self._plain("file.json")
        with self.assertRaises(StateError):
            state.open_owned_readable_dir(self.root / "file.json")

    def test_reads_never_write_or_change_modes(self) -> None:
        target = self._plain("keep.json", b"{}", 0o444)
        before = os.stat(target)
        state.read_owned_readable(target, max_bytes=10)
        after = os.stat(target)
        self.assertEqual((before.st_mode, before.st_mtime_ns, before.st_ino), (after.st_mode, after.st_mtime_ns, after.st_ino))




class FilesystemFailureTests(unittest.TestCase):
    """The filesystem failures a command reports with a remedy."""

    def test_each_errno_has_a_meaning_and_a_remedy(self) -> None:
        import errno as errno_mod

        for code in (errno_mod.ENOSPC, errno_mod.EDQUOT, errno_mod.EACCES, errno_mod.EPERM, errno_mod.EROFS):
            with self.subTest(errno=errno_mod.errorcode[code]):
                found = state.filesystem_failure(OSError(code, "x", "/some/path"))
                self.assertIsNotNone(found)
                what, remedy = found
                self.assertTrue(what and remedy)
                self.assertNotIn("/some/path", what + remedy)
        self.assertIsNone(state.filesystem_failure(OSError(errno_mod.ENOENT, "missing")))
        self.assertIsNone(state.filesystem_failure(ValueError("not an OSError")))

    def test_a_committed_write_never_claims_nothing_changed(self) -> None:
        import errno as errno_mod

        what, _remedy = state.filesystem_failure(state.CommittedStateError(errno_mod.ENOSPC, "dir fsync"))
        self.assertIn("was written", what)
        self.assertNotIn("nothing", what)


if __name__ == "__main__":
    unittest.main()
