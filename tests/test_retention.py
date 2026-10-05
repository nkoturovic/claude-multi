"""Owned and retained Claude Code copies: migration on launch, the use lock, report, prune rule."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from claude_multi import acquire, cli, launch, paths, pin, probe, retention, service, state
from claude_multi.platform import darwin_process
from _layout import SRC_DIR
from _v4 import V4Case
import claude_multi.tui


def _contract(version: str, platform: str, data: bytes) -> dict:
    return {"verified": [{
        "version": version,
        "platforms": {platform: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}},
        "manifest_sha256": "1" * 64, "signature_sha256": None,
        "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE", "verified_at": "2026-10-01",
        "evidence": {platform: "battery", "receipt_sha256": "2" * 64},
    }]}


FAKE = b"#!/bin/fake\n"


class PruneCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="cm-retention-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / "state")}
        self.platform = pin.host_platform()
        self.contract = _contract("2.1.300", self.platform, b"#!/bin/current\n")
        for version in ("2.1.200", "2.1.250", "2.1.280", "2.1.300"):
            self.owned(version)

    def owned(self, version: str, data: bytes = FAKE) -> Path:
        path = pin.owned_path(self.env, version, self.platform)
        state.ensure_private_dir(path.parent)
        path.write_bytes(data)
        path.chmod(0o755)
        return path

    def plan(self, **kwargs):
        kwargs.setdefault("launcher_version", "1.0.2")
        kwargs.setdefault("running", set())
        kwargs.setdefault("gateway_launcher", None)
        return retention.prune_plan(self.contract, self.env, **kwargs)

    def prune_copies(self, **kwargs) -> retention.PruneOutcome:
        kwargs.setdefault("launcher_version", "1.0.2")
        kwargs.setdefault("running", set())
        kwargs.setdefault("gateway_launcher", None)
        return retention.prune_copies(self.contract, self.env, **kwargs)

    def exclusive(self, version: str) -> pin.UseLock | None:
        """What a prune gets: the version's use lock, exclusively, without waiting."""

        return pin.take_use_lock(self.env, version, shared=False, blocking=False)

    def assert_in_use(self, version: str) -> None:
        self.assertIsNone(self.exclusive(version), f"{version} is not locked")

    def assert_free(self, version: str) -> None:
        lock = self.exclusive(version)
        self.assertIsNotNone(lock, f"{version} is still locked")
        lock.release()


class UseLockTests(PruneCase):
    """The per-version use lock file: ``<owned root>/<version>/.in-use``."""

    def test_shared_holders_coexist_and_keep_the_exclusive_holder_out(self):
        first = pin.take_use_lock(self.env, "2.1.200", shared=True)
        second = pin.take_use_lock(self.env, "2.1.200", shared=True)
        path = pin.owned_root(self.env) / "2.1.200" / pin.IN_USE
        self.assertEqual(first.path, path)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        self.assert_in_use("2.1.200")
        first.release()
        self.assert_in_use("2.1.200")
        second.release()
        second.release()  # idempotent
        self.assert_free("2.1.200")
        # Only the launch's descriptor survives exec.
        launch_lock = pin.take_use_lock(self.env, "2.1.200", shared=True, inheritable=True)
        prune_lock = self.exclusive("2.1.250")
        try:
            self.assertTrue(os.get_inheritable(launch_lock.descriptor))
            self.assertFalse(os.get_inheritable(prune_lock.descriptor))
        finally:
            launch_lock.release()
            prune_lock.release()

    def test_a_missing_version_directory_is_not_set_up(self):
        with self.assertRaises(FileNotFoundError):
            pin.take_use_lock(self.env, "2.1.400", shared=True)
        self.assertFalse((pin.owned_root(self.env) / "2.1.400").exists())
        with self.assertRaises(pin.PinError):
            pin.take_use_lock(self.env, "../2.1.200", shared=True)

    def test_a_symlinked_lock_file_refuses_and_its_target_is_untouched(self):
        target = self.home / "elsewhere"
        target.write_bytes(b"keep")
        target.chmod(0o644)
        path = pin.owned_root(self.env) / "2.1.200" / pin.IN_USE
        path.symlink_to(target)
        with self.assertRaisesRegex(pin.PinError, r"is a symbolic link.*rm .*\.in-use.*"
                                                  r"`claude-multi setup --step claude`"):
            pin.take_use_lock(self.env, "2.1.200", shared=True)
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        # The launch refuses with the same remedy.
        contract = _contract("2.1.200", self.platform, FAKE)
        with self.assertRaisesRegex(launch.LaunchError, r"Claude Code 2\.1\.200 cannot be used: .*symbolic link"
                                                        r".*`claude-multi setup --step claude`"):
            launch.PinnedCopy(contract, self.env).verify()
        # Pruning keeps it (its lock cannot be checked) and never follows it.
        plan = self.plan()
        self.assertEqual(plan.keep["2.1.200"], retention.LOCK_UNKNOWN)
        self.assertEqual(self.prune_copies().removed, ("2.1.250",))
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_bytes(), b"keep")

    def test_a_foreign_or_linked_lock_file_refuses(self):
        path = pin.owned_root(self.env) / "2.1.200" / pin.IN_USE
        path.write_bytes(b"")
        other = os.geteuid() + 1
        with mock.patch.object(pin, "check_private_directory"), \
                mock.patch.object(pin.os, "geteuid", return_value=other), \
                self.assertRaisesRegex(pin.PinError, "belongs to another user.*rm "):
            pin.take_use_lock(self.env, "2.1.200", shared=True)
        os.link(path, self.home / "second-name")
        with self.assertRaisesRegex(pin.PinError, "hard links"):
            pin.take_use_lock(self.env, "2.1.200", shared=True)
        (self.home / "second-name").unlink()
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(pin.PinError, "not a regular file"):
            pin.take_use_lock(self.env, "2.1.200", shared=True)
        self.assertEqual(self.plan().keep["2.1.200"], retention.LOCK_UNKNOWN)

    def test_an_own_lock_file_is_made_private(self):
        path = pin.owned_root(self.env) / "2.1.200" / pin.IN_USE
        path.write_bytes(b"")
        path.chmod(0o644)
        pin.take_use_lock(self.env, "2.1.200", shared=True).release()
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)

    def test_the_version_directory_must_be_private_and_real(self):
        directory = pin.owned_root(self.env) / "2.1.200"
        directory.chmod(0o755)
        with self.assertRaisesRegex(pin.PinError, r"open to other users.*chmod 700"):
            pin.take_use_lock(self.env, "2.1.200", shared=True)
        self.assertFalse((directory / pin.IN_USE).exists())
        self.assertEqual(self.plan().keep["2.1.200"], retention.LOCK_UNKNOWN)
        directory.chmod(0o700)
        real = self.home / "real"
        state.ensure_private_dir(real)
        (pin.owned_root(self.env) / "2.1.400").symlink_to(real)
        with self.assertRaisesRegex(pin.PinError, "is a symbolic link"):
            pin.take_use_lock(self.env, "2.1.400", shared=True)
        self.assertEqual(list(real.iterdir()), [])  # nothing created through the link


class PruneRuleTests(PruneCase):
    def test_keeps_current_and_previous_release_pin_from_the_index(self):
        acquire.record_launcher(self.env, "1.0.0", "2.1.250")
        acquire.record_launcher(self.env, "1.0.1", "2.1.280")
        acquire.record_launcher(self.env, "1.0.2", "2.1.300")
        plan = self.plan()
        self.assertEqual(plan.keep, {"2.1.300": "the current pin", "2.1.280": "the previous release's pin"})
        self.assertEqual([name for name, _size in plan.remove], ["2.1.200", "2.1.250"])
        self.assertIsNone(plan.blocked)

    def test_without_an_index_the_newest_older_copy_is_kept(self):
        plan = self.plan()
        self.assertEqual(plan.keep["2.1.280"], "the previous release's pin")
        self.assertEqual([name for name, _size in plan.remove], ["2.1.200", "2.1.250"])

    def test_live_sessions_and_the_gateway_release_pin_are_kept(self):
        acquire.record_launcher(self.env, "0.9.0", "2.1.200")
        acquire.record_launcher(self.env, "1.0.1", "2.1.280")
        plan = self.plan(running={"2.1.250"}, gateway_launcher="0.9.0")
        self.assertEqual(plan.keep["2.1.250"], retention.RUNNING)
        self.assertIn("running gateway", plan.keep["2.1.200"])
        self.assertEqual(plan.remove, ())

    def test_versions_whose_use_lock_is_held_are_kept_in_use(self):
        lock = pin.take_use_lock(self.env, "2.1.200", shared=True)
        try:
            plan = self.plan()
            self.assertEqual(plan.keep["2.1.200"], retention.IN_USE)
            self.assertEqual([name for name, _size in plan.remove], ["2.1.250"])
            self.assertEqual(self.prune_copies(), retention.PruneOutcome(("2.1.250",), ("2.1.200",)))
        finally:
            lock.release()
        self.assertEqual(self.prune_copies(), retention.PruneOutcome(("2.1.200",), ()))
        self.assertFalse((pin.owned_root(self.env) / "2.1.200").exists())

    def test_unknown_facts_block_every_removal(self):
        self.assertIn("cannot be checked", self.plan(running=None).blocked)
        self.assertEqual(self.plan(running=None).remove, ())
        self.assertEqual(self.prune_copies(running=None), retention.PruneOutcome())
        blocked = self.plan(gateway_launcher="0.8.0")
        self.assertIn("claude-multi 0.8.0", blocked.blocked)
        self.assertEqual(blocked.remove, ())
        # The gateway of this very release pins the current version.
        self.assertIsNone(self.plan(gateway_launcher="1.0.2").blocked)

    def test_gateway_stamp_is_read_from_the_state_root(self):
        workdir = service.ensure_gateway_workdir(paths.state_root(self.env))
        service.write_exec_stamp(workdir, service.ExecStamp("0.7.0", "sig", 4242, "/gw", "2026-10-01T00:00:00+00:00"))
        plan = retention.prune_plan(self.contract, self.env, launcher_version="1.0.2", running=set())
        self.assertIn("claude-multi 0.7.0", plan.blocked)

    def test_running_versions_come_from_process_exe_links(self):
        proc = self.home / "proc"
        for pid, target in (("10", pin.owned_path(self.env, "2.1.250", self.platform)),
                            ("11", self.home / "elsewhere" / "2.1.200" / "claude"),
                            ("self", pin.owned_path(self.env, "2.1.200", self.platform))):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "exe").symlink_to(target)
        scan = retention.scan_processes(pin.owned_root(self.env), proc)
        self.assertEqual(scan, retention.ProcessScan(frozenset({"2.1.250"})))
        self.assertIsNone(retention.scan_processes(pin.owned_root(self.env), self.home / "no-proc"))

    def test_prune_removes_regular_files_only_and_never_follows_links(self):
        outside = self.home / "outside"
        outside.write_bytes(b"keep me")
        (pin.owned_path(self.env, "2.1.200", self.platform).parent / "link").symlink_to(outside)
        removed = retention.prune(self.contract, self.env, launcher_version="1.0.2", running=set(),
                                  gateway_launcher=None)
        self.assertEqual(removed, ["2.1.250"])  # 2.1.200 holds a link: left in place
        self.assertEqual(outside.read_bytes(), b"keep me")
        self.assertTrue(pin.owned_path(self.env, "2.1.300", self.platform).exists())
        self.assertTrue(pin.owned_path(self.env, "2.1.280", self.platform).exists())
        self.assertFalse((pin.owned_root(self.env) / "2.1.250").exists())

    def fake_process(self, proc: Path, pid: str, *, exe: Path | None = None, name: str = "claude",
                     uid: int | None = None) -> Path:
        """A fake process entry: an ``exe`` link, or an unreadable one (a
        regular file: reading it as a link fails) with a ``status``."""

        directory = proc / pid
        directory.mkdir(parents=True)
        if exe is not None:
            (directory / "exe").symlink_to(exe)
        else:
            (directory / "exe").write_bytes(b"")
        owner = os.geteuid() if uid is None else uid
        (directory / "status").write_text(f"Name:\t{name}\nUid:\t{owner}\t{owner}\t{owner}\t{owner}\n")
        return directory

    def test_a_process_whose_executable_cannot_be_read_never_blocks_pruning(self):
        proc = self.home / "proc"
        self.fake_process(proc, "1", exe=Path("/usr/bin/true"))
        self.fake_process(proc, "3", uid=os.geteuid() + 1)  # another user's
        self.fake_process(proc, "4")  # this user's, unreadable (non-dumpable)
        self.fake_process(proc, "12", name="systemd")
        root = pin.owned_root(self.env)
        self.assertEqual(retention.scan_processes(root, proc), retention.ProcessScan())
        plan = self.plan(running=..., proc_root=proc)
        self.assertIsNone(plan.blocked)
        self.assertEqual([name for name, _size in plan.remove], ["2.1.200", "2.1.250"])
        self.assertEqual(self.prune_copies(running=..., proc_root=proc).removed, ("2.1.200", "2.1.250"))

    def test_doctor_shows_copies_in_use_and_never_a_process_line(self):
        proc = self.home / "proc"
        for pid in ("31", "32", "33", "34", "35", "36", "37"):
            self.fake_process(proc, pid, name="worker")
        scan = retention.scan_processes

        def report():
            with mock.patch.object(retention, "scan_processes", side_effect=lambda root, *_a, **_k: scan(root, proc)):
                return retention.retention_report(self.contract, None, self.env)

        lock = pin.take_use_lock(self.env, "2.1.200", shared=True)
        try:
            attention, info = report()
        finally:
            lock.release()
        self.assertEqual(info, ["Claude Code copies kept by claude-multi: 2.1.300 (the current pin), "
                                "2.1.280 (the previous release's pin), 2.1.200 (in use)."])
        self.assertEqual(attention, ["Claude Code copies no release here needs: 2.1.250 (0 MiB) — "
                                     "`claude-multi setup --step claude` removes them"])
        attention, info = report()
        self.assertNotIn("in use", info[0])
        self.assertIn("2.1.200 (0 MiB), 2.1.250 (0 MiB)", attention[0])
        self.assertFalse(any("inspect" in line for line in [*attention, *info]))

    def test_a_pin_in_use_keeps_its_pin_reason_and_doctor_adds_the_use(self):
        # Releases older than this one (and the running one, 1.0.0).
        acquire.record_launcher(self.env, "0.8.0", "2.1.200")
        acquire.record_launcher(self.env, "0.9.0", "2.1.280")
        locks = [pin.take_use_lock(self.env, version, shared=True)
                 for version in ("2.1.300", "2.1.280", "2.1.250", "2.1.200")]
        try:
            plan = self.plan(running={"2.1.250", "2.1.280"}, gateway_launcher="0.8.0")
            self.assertEqual(plan.keep, {
                "2.1.300": "the current pin", "2.1.280": "the previous release's pin",
                "2.1.200": "the running gateway's release (0.8.0) pins it",
                "2.1.250": retention.IN_USE,  # the lock rules over the process evidence
            })
            # Setup's "once no session runs them" covers no pin.
            outcome = self.prune_copies(running={"2.1.250", "2.1.280"}, gateway_launcher="0.8.0")
            self.assertEqual(outcome, retention.PruneOutcome((), ("2.1.250",)))
            with mock.patch.object(retention, "scan_processes",
                                   return_value=retention.ProcessScan(frozenset({"2.1.250", "2.1.280"}))), \
                    mock.patch.object(retention, "_gateway_launcher", return_value="0.8.0"):
                attention, info = retention.retention_report(self.contract, None, self.env)
        finally:
            for lock in locks:
                lock.release()
        self.assertEqual(info, ["Claude Code copies kept by claude-multi: 2.1.300 (the current pin, in use), "
                                "2.1.280 (the previous release's pin, in use), "
                                "2.1.200 (the running gateway's release (0.8.0) pins it, in use), "
                                "2.1.250 (in use)."])
        self.assertEqual(attention, [])
        # Once the sessions are gone the pins stay, without the use.
        with mock.patch.object(retention, "scan_processes", return_value=retention.ProcessScan()), \
                mock.patch.object(retention, "_gateway_launcher", return_value="0.8.0"):
            _attention, info = retention.retention_report(self.contract, None, self.env)
        self.assertNotIn("in use", info[0])

    def test_an_unreadable_or_invalid_gateway_record_blocks_pruning(self):
        workdir = service.ensure_gateway_workdir(paths.state_root(self.env))
        service.write_exec_stamp(workdir, service.ExecStamp("1.0.2", "sig", 4242, "/gw", "2026-10-01T00:00:00+00:00"))
        stamp = workdir / service.EXEC_STAMP
        protected = retention.prune_plan(self.contract, self.env, launcher_version="1.0.2", running=set())
        self.assertIsNone(protected.blocked)
        stamp.chmod(0o644)  # not private: the reader refuses it
        plan = retention.prune_plan(self.contract, self.env, launcher_version="1.0.2", running=set())
        self.assertIn("start record cannot be read", plan.blocked)
        self.assertEqual(plan.remove, ())
        stamp.chmod(0o600)
        stamp.write_bytes(b'{"launcher_version": 7}')
        self.assertIn("start record cannot be read",
                      retention.prune_plan(self.contract, self.env, launcher_version="1.0.2", running=set()).blocked)
        self.assertIn("cannot be read", self.plan(gateway_launcher=retention.UNKNOWN).blocked)
        stamp.unlink()  # no recorded start: nothing to protect
        self.assertIsNone(retention.prune_plan(self.contract, self.env, launcher_version="1.0.2",
                                               running=set()).blocked)

    def test_symlinked_version_directories_never_count(self):
        (pin.owned_root(self.env) / "2.1.100").symlink_to(self.home)
        self.assertNotIn("2.1.100", retention.owned_versions(self.env))
        self.assertNotIn("2.1.100", [name for name, _ in self.plan().remove])

    def test_report_lists_kept_copies_and_names_the_step(self):
        with mock.patch.object(retention, "scan_processes", return_value=retention.ProcessScan()):
            attention, info = retention.retention_report(self.contract, None, self.env)
        self.assertIn("2.1.300 (the current pin)", info[0])
        self.assertIn("2.1.200", attention[0])
        self.assertIn("`claude-multi setup --step claude` removes them", attention[0])

    def test_report_low_disk_line_compares_free_bytes_with_the_build_size(self):
        size = pin.platform_record(self.contract, pin.host_platform())["size"]
        with mock.patch.object(retention, "scan_processes", return_value=None):
            with mock.patch.object(retention, "free_bytes", return_value=2 * size):
                attention, _info = retention.retention_report(self.contract, None, self.env)
            self.assertFalse(any(line.startswith("disk space:") for line in attention))
            with mock.patch.object(retention, "free_bytes", return_value=2 * size - 1):
                attention, _info = retention.retention_report(self.contract, None, self.env)
            self.assertTrue(any(line.startswith("disk space:") and "free on the filesystem of" in line
                                for line in attention))
            with mock.patch.object(retention, "free_bytes", return_value=None):
                attention, _info = retention.retention_report(self.contract, None, self.env)
            self.assertFalse(any(line.startswith("disk space:") for line in attention))


class MacOSScanTests(PruneCase):
    """The macOS process evidence: one bounded ``ps`` read through its seam."""

    def runner(self, lines: list[str], returncode: int = 0):
        calls: list[list[str]] = []

        def run(argv, **kwargs):
            calls.append(list(argv))
            self.assertLessEqual(kwargs["timeout"], darwin_process.TIMEOUT)
            self.assertEqual(kwargs["env"]["PATH"], darwin_process.SYSTEM_PATH)
            return subprocess.CompletedProcess(argv, returncode, "\n".join(lines).encode(), b"")
        return run, calls

    def test_ps_names_executable_paths_only(self):
        run, calls = self.runner(["/usr/libexec/logd", "(launchd)", "worker", "/Applications/A B.app/a  ", ""])
        self.assertEqual(darwin_process.executable_paths(runner=run, ps="/bin/ps"),
                         ["/usr/libexec/logd", "/Applications/A B.app/a"])
        self.assertEqual(calls, [["/bin/ps", "-axww", "-o", "comm="]])
        failing, _calls = self.runner([], returncode=1)
        self.assertIsNone(darwin_process.executable_paths(runner=failing, ps="/bin/ps"))

    def test_owned_copies_seen_through_ps_are_kept_and_the_lock_still_rules(self):
        link = self.home / "link"
        link.symlink_to(pin.owned_root(self.env))
        run, calls = self.runner([
            str(pin.owned_path(self.env, "2.1.250", self.platform)),
            str(link / "2.1.280" / "claude"),  # through a link: resolved
            str(self.home / "elsewhere" / "2.1.200" / "claude"),
            "/usr/bin/true", "(claude)",
        ])
        real_scan = retention.scan_processes
        with mock.patch.object(retention, "_platform", return_value="darwin"), \
                mock.patch.object(darwin_process, "_tool", return_value="/bin/ps"):
            scan = retention.scan_processes(pin.owned_root(self.env), runner=run)
            self.assertEqual(scan, retention.ProcessScan(frozenset({"2.1.250", "2.1.280"})))
            with mock.patch.object(retention, "scan_processes",
                                   side_effect=lambda root, *_a, **_k: real_scan(root, runner=run)):
                lock = pin.take_use_lock(self.env, "2.1.200", shared=True)
                try:
                    plan = self.plan(running=...)
                    self.assertEqual(plan.keep["2.1.250"], retention.RUNNING)
                    self.assertEqual(plan.keep["2.1.200"], retention.IN_USE)
                    self.assertEqual(plan.remove, ())
                    self.assertEqual(self.prune_copies(running=...),
                                     retention.PruneOutcome((), ("2.1.200", "2.1.250")))
                finally:
                    lock.release()
                self.assertEqual(self.prune_copies(running=...).removed, ("2.1.200",))
            self.assertTrue(all(argv == ["/bin/ps", "-axww", "-o", "comm="] for argv in calls))

    def test_an_unreadable_ps_table_blocks_like_an_unlistable_proc(self):
        failing, _calls = self.runner([], returncode=1)
        with mock.patch.object(retention, "_platform", return_value="darwin"), \
                mock.patch.object(darwin_process, "_tool", return_value="/bin/ps"):
            self.assertIsNone(retention.scan_processes(pin.owned_root(self.env), runner=failing))
        with mock.patch.object(retention, "_platform", return_value="darwin"), \
                mock.patch.object(darwin_process, "_tool", return_value=None):
            plan = self.plan(running=...)
        self.assertIn("cannot be checked on this system", plan.blocked)
        self.assertEqual(plan.remove, ())


class PruneInterleavingTests(PruneCase):
    """Pruning against a launch or an acquisition of the same version."""

    def setUp(self):
        super().setUp()
        self.proc = self.home / "proc"
        (self.proc / "1").mkdir(parents=True)
        (self.proc / "1" / "exe").symlink_to("/usr/bin/true")
        self.old = _contract("2.1.200", self.platform, FAKE)  # an older release's pin

    def prune(self) -> retention.PruneOutcome:
        return self.prune_copies(running=..., proc_root=self.proc)

    def test_a_process_started_after_planning_keeps_its_version(self):
        self.assertEqual([name for name, _ in self.plan(running=..., proc_root=self.proc).remove],
                         ["2.1.200", "2.1.250"])
        original = retention._hold

        def started_meanwhile(environ, name):
            # A copy of 2.1.200 starts after the plan was made, before the
            # version is locked.
            if not (self.proc / "77").exists():
                (self.proc / "77").mkdir()
                (self.proc / "77" / "exe").symlink_to(pin.owned_path(self.env, "2.1.200", self.platform))
            return original(environ, name)

        with mock.patch.object(retention, "_hold", side_effect=started_meanwhile):
            outcome = self.prune()
        self.assertEqual(outcome, retention.PruneOutcome(("2.1.250",), ("2.1.200",)))
        self.assertTrue(pin.owned_path(self.env, "2.1.200", self.platform).exists())

    def test_a_version_being_acquired_is_left_alone(self):
        partial = pin.owned_path(self.env, "2.1.200", self.platform).parent / ".claude.part"
        partial.write_bytes(b"partial")
        lock = acquire.version_lock(self.env, "2.1.200")
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertEqual(self.prune(), retention.PruneOutcome(("2.1.250",), ("2.1.200",)))
        finally:
            lock.release()
        self.assertEqual(partial.read_bytes(), b"partial")
        self.assertTrue(pin.owned_path(self.env, "2.1.200", self.platform).exists())
        self.assertEqual(self.prune().removed, ("2.1.200",))
        self.assertFalse(partial.exists())

    def test_a_launch_between_its_hash_check_and_exec_keeps_its_version(self):
        pinned = launch.PinnedCopy(self.old, self.env)
        status = pinned.verify()
        self.assertEqual(status.inspected_path, pin.owned_path(self.env, "2.1.200", self.platform))
        try:
            self.assertEqual(self.prune(), retention.PruneOutcome(("2.1.250",), ("2.1.200",)))
        finally:
            pinned.release()
        self.assertEqual(self.prune().removed, ("2.1.200",))

    def test_a_launch_that_locks_after_the_plan_keeps_its_version(self):
        original = retention._hold
        launched = []

        def launch_first(environ, name):
            if name == "2.1.200" and not launched:
                pinned = launch.PinnedCopy(self.old, self.env)
                pinned.verify()
                launched.append(pinned)
            return original(environ, name)

        with mock.patch.object(retention, "_hold", side_effect=launch_first):
            outcome = self.prune()
        self.assertEqual(outcome, retention.PruneOutcome(("2.1.250",), ("2.1.200",)))
        self.assertTrue(pin.owned_path(self.env, "2.1.200", self.platform).exists())
        launched[0].release()

    def test_the_launch_holds_the_use_lock_until_exec(self):
        seen = []

        def resolve(*_args, **_kwargs):
            seen.append(self.exclusive("2.1.200"))
            raise launch.LaunchError("stop here")

        with mock.patch.object(launch, "resolve_claude", side_effect=resolve), \
                self.assertRaisesRegex(launch.LaunchError, "stop here"):
            launch.perform_launch(mock.Mock(durable=True), native_contract=self.old, environ=self.env,
                                  record={}, store=mock.Mock(root=self.home / "state"), gateway={})
        self.assertEqual(seen, [None])
        self.assert_free("2.1.200")  # released on the failure path

    def launch_in_thread(self, contract: dict, retained_root: Path | None = None):
        """A launch's verification on another thread: (thread, outcome, pinned)."""

        outcome: list = []
        pinned = launch.PinnedCopy(contract, self.env)

        def run():
            try:
                outcome.append(pinned.verify(retained_root=retained_root))
            except BaseException as exc:  # noqa: BLE001 - handed to the test thread
                outcome.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        return thread, outcome, pinned

    def prune_while_a_launch_waits(self, contract: dict, retained_root: Path | None = None):
        """Prune 2.1.200 while a launch of it waits for the use lock the prune
        holds; returns (prune outcome, launch outcome, pinned)."""

        thread, outcome, pinned = self.launch_in_thread(contract, retained_root)
        self.addCleanup(pinned.release)
        original = retention._remove_version
        waited = []

        def remove(root, name):
            if name == "2.1.200":
                thread.start()
                time.sleep(0.3)
                waited.append(thread.is_alive() and not outcome)  # blocked on the lock
            return original(root, name)

        # The index entry a copy writes would make 2.1.200 this test
        # launcher's previous pin; the rule under test is the lock.
        with mock.patch.object(retention, "_remove_version", side_effect=remove), \
                mock.patch.object(acquire, "_record_running_launcher"):
            pruned = self.prune()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(waited, [True])
        return pruned, outcome[0], pinned

    def test_a_launch_starting_right_after_the_lock_file_goes_finds_no_copy(self):
        directory = pin.owned_root(self.env) / "2.1.200"
        lock_file = directory / pin.IN_USE
        scandir, unlink = os.scandir, os.unlink
        launches: list = []

        def lock_file_first(path=".", *args):
            # A listing order the filesystem may legally give.
            if Path(path) == directory:
                return sorted(scandir(path), key=lambda entry: entry.name != pin.IN_USE)
            return scandir(path, *args)

        def unlinking(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if Path(path) == lock_file and not launches:
                # A new launch starts the moment the lock file is gone: it
                # makes a new one, locks it and checks the copy.
                pinned = launch.PinnedCopy(self.old, self.env)
                self.addCleanup(pinned.release)
                try:
                    launches.append(pinned.verify())
                except launch.LaunchError as exc:
                    launches.append(exc)
                launches.append(pinned)

        with mock.patch.object(retention.os, "scandir", side_effect=lock_file_first), \
                mock.patch.object(retention.os, "unlink", side_effect=unlinking):
            outcome = self.prune()
        result, pinned = launches
        # No copy was left for it to verify: it never holds a copy that is gone.
        self.assertIsInstance(result, launch.NotSetUpError)
        self.assertFalse(pin.owned_path(self.env, "2.1.200", self.platform).exists())
        # It holds the new lock file, so the version is reported as in use.
        self.assertEqual(os.fstat(pinned.lock.descriptor).st_ino, os.lstat(lock_file).st_ino)
        self.assertEqual(outcome, retention.PruneOutcome(("2.1.250",), ("2.1.200",)))
        pinned.release()
        self.assertEqual(self.prune().removed, ("2.1.200",))
        self.assertFalse(directory.exists())

    def test_a_version_that_cannot_be_removed_in_full_is_reported(self):
        directory = pin.owned_root(self.env) / "2.1.200"
        (directory / "extra").mkdir()  # not a regular file: nothing is removed
        self.assertEqual(self.prune(), retention.PruneOutcome(("2.1.250",), (), ("2.1.200",)))
        self.assertTrue(pin.owned_path(self.env, "2.1.200", self.platform).exists())
        (directory / "extra").rmdir()
        unlink = os.unlink

        def refusing(path, *args, **kwargs):
            if Path(path) == directory / ".claude.part":
                raise PermissionError(13, "Permission denied", str(path))
            unlink(path, *args, **kwargs)

        (directory / ".claude.part").write_bytes(b"partial")
        with mock.patch.object(retention.os, "unlink", side_effect=refusing):
            self.assertEqual(self.prune(), retention.PruneOutcome((), (), ("2.1.200",)))
        # The lock file stays (it went last), so the half-removed version can
        # never look free to a launch that is checking a copy.
        self.assertTrue((directory / pin.IN_USE).is_file())
        self.assertEqual(self.prune().removed, ("2.1.200",))

    def test_a_launch_waiting_for_a_prune_never_runs_the_removed_copy(self):
        pruned, result, pinned = self.prune_while_a_launch_waits(self.old)
        self.assertEqual(pruned.removed, ("2.1.200", "2.1.250"))
        self.assertIsInstance(result, launch.NotSetUpError)
        self.assertIn("Claude Code 2.1.200 is not set up for claude-multi", str(result))
        self.assertIsNone(pinned.lock)
        self.assertFalse((pin.owned_root(self.env) / "2.1.200").exists())

    def test_a_waiting_launch_that_copies_the_retained_copy_back_locks_the_new_one(self):
        retained = paths.retained_root(self.env)
        state.ensure_private_dir(retained)
        (retained / "2.1.200").write_bytes(FAKE)
        (retained / "2.1.200").chmod(0o755)
        pruned, result, pinned = self.prune_while_a_launch_waits(self.old, retained)
        self.assertEqual(pruned.removed, ("2.1.200", "2.1.250"))
        self.assertIsInstance(result, launch.BinaryStatus)
        self.assertTrue(result.migrated)
        self.assertEqual(result.inspected_path, pin.owned_path(self.env, "2.1.200", self.platform))
        # The lock it holds is the new version directory's.
        self.assertEqual(os.fstat(pinned.lock.descriptor).st_ino,
                         os.lstat(pin.owned_root(self.env) / "2.1.200" / pin.IN_USE).st_ino)
        self.assert_in_use("2.1.200")
        self.assertEqual(self.prune(), retention.PruneOutcome((), ("2.1.200",)))
        pinned.release()
        self.assertEqual(self.prune().removed, ("2.1.200",))


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "BOUNDARY: renaming a process and making it non-dumpable needs Linux")
class LifetimeLockProcessTests(PruneCase):
    """Real processes: a launch execs the owned copy (a copy of this Python
    interpreter) and the exec'd program holds the use lock for its lifetime."""

    LAUNCHER = (
        "import json, sys\n"
        "from claude_multi import launch\n"
        "from claude_multi.platform import posix_fs\n"
        "contract, environ, child_env = (json.loads(arg) for arg in sys.argv[1:4])\n"
        "status = launch.PinnedCopy(contract, environ).verify()\n"
        "path = str(status.inspected_path)\n"
        "posix_fs.exec_replace(path, [path, '-c', sys.argv[4]], child_env)\n"
    )
    # The "Claude Code" it runs renames itself and makes itself non-dumpable,
    # so neither its name nor its /proc exe link tells what it runs.
    WORKER = (
        "import ctypes, os, sys\n"
        "libc = ctypes.CDLL(None)\n"
        "libc.prctl(15, b'worker', 0, 0, 0)\n"  # PR_SET_NAME
        "libc.prctl(4, 0, 0, 0, 0)\n"  # PR_SET_DUMPABLE
        "print('ready', os.getpid(), flush=True)\n"
        "sys.stdin.read()\n"
    )
    # A program that hands the lock on to a child and exits at once.
    FORKER = (
        "import os, sys\n"
        "if os.fork() == 0:\n"
        "    print('child', os.getpid(), flush=True)\n"
        "    sys.stdin.read()\n"
        "    os._exit(0)\n"
        "os._exit(0)\n"
    )

    def setUp(self):
        super().setUp()
        interpreter = Path(sys.executable).read_bytes()
        self.copy = self.owned("2.1.200", interpreter)
        self.old = _contract("2.1.200", self.platform, interpreter)
        self.child_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONHOME": sys.base_prefix,
                          "PYTHONDONTWRITEBYTECODE": "1"}

    def start(self, program: str) -> subprocess.Popen:
        environ = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SRC_DIR),
                   "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(self.home)}
        process = subprocess.Popen(
            [sys.executable, "-c", self.LAUNCHER, json.dumps(self.old), json.dumps(self.env),
             json.dumps(self.child_env), program],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=environ, cwd=self.home, text=True)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.addCleanup(process.stdout.close)
        self.addCleanup(lambda: process.stdin.closed or process.stdin.close())
        return process

    def wait_free(self, version: str, seconds: float = 10.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            lock = self.exclusive(version)
            if lock is not None:
                lock.release()
                return
            time.sleep(0.05)
        self.fail(f"{version} stayed locked")

    def test_the_exec_d_copy_holds_the_lock_while_it_runs_even_renamed_and_non_dumpable(self):
        process = self.start(self.WORKER)
        line = process.stdout.readline().split()
        self.assertEqual(line[0], "ready", line)
        self.assertEqual(int(line[1]), process.pid)  # exec replaced the launcher
        with open(f"/proc/{process.pid}/status") as status:
            self.assertIn("Name:\tworker\n", status.read())
        self.assert_in_use("2.1.200")
        # The real process table: the non-dumpable process blocks nothing.
        plan = retention.prune_plan(self.contract, self.env, launcher_version="1.0.2", gateway_launcher=None)
        self.assertIsNone(plan.blocked)
        self.assertIn(plan.keep["2.1.200"], (retention.IN_USE, retention.RUNNING))
        self.assertEqual([name for name, _size in plan.remove], ["2.1.250"])
        # The lock alone keeps it.
        self.assertEqual(self.prune_copies(), retention.PruneOutcome(("2.1.250",), ("2.1.200",)))
        with mock.patch.object(retention, "_gateway_launcher", return_value=None), \
                mock.patch.object(retention, "scan_processes", return_value=retention.ProcessScan()):
            attention, info = retention.retention_report(self.contract, None, self.env)
        self.assertIn("2.1.200 (in use)", info[0])
        self.assertEqual(attention, [])
        self.assertIsNone(process.poll())
        process.stdin.close()
        self.assertEqual(process.wait(10), 0)
        self.assert_free("2.1.200")
        self.assertEqual(self.prune_copies().removed, ("2.1.200",))
        self.assertFalse(self.copy.exists())

    def test_a_child_of_the_exec_d_copy_keeps_the_lock_after_the_copy_exits(self):
        process = self.start(self.FORKER)
        line = process.stdout.readline().split()
        self.assertEqual(line[0], "child", line)
        self.assertEqual(process.wait(10), 0)  # the exec'd copy itself has exited
        self.assert_in_use("2.1.200")  # its child inherited the lock
        self.assertEqual(self.prune_copies().in_use, ("2.1.200",))
        process.stdin.close()  # the child's stdin too: it exits
        self.wait_free("2.1.200")
        self.assertEqual(self.prune_copies().removed, ("2.1.200",))

    def test_a_copy_started_outside_claude_multi_is_the_documented_residual(self):
        # Run directly (no launch, no lock) by a process that hides what it
        # runs: pruning cannot know, and the docs say so.
        child = subprocess.Popen([str(self.copy), "-c", self.WORKER], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, env=self.child_env, cwd=self.home, text=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.stdin.close)
        self.assertEqual(child.stdout.readline().split()[0], "ready")
        try:
            os.readlink(f"/proc/{child.pid}/exe")
        except PermissionError:
            pass
        else:
            self.skipTest("BOUNDARY: this process may read a non-dumpable process's executable link")
        self.assert_free("2.1.200")
        self.assertIn("2.1.200", self.prune_copies(running=...).removed)
        self.assertIsNone(child.poll())  # the running program is not disturbed


class RetainedHelperTests(unittest.TestCase):
    def test_verify_retained_and_same_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "pinned-clients"
            state.ensure_private_dir(root)
            target = root / "2.1.9"
            target.write_bytes(b"x")
            target.chmod(0o755)
            digest = hashlib.sha256(b"x").hexdigest()
            self.assertEqual(retention.verify_retained(root, "2.1.9", digest), target)
            self.assertIsNone(retention.verify_retained(root, "2.1.9", "0" * 64))
            self.assertIsNone(retention.verify_retained(root, "../x", digest))
            self.assertTrue(retention.same_file(target, target))
            self.assertFalse(retention.same_file(target, root / "missing"))


class ExactClientHoldTests(PruneCase):
    """The qualification probe that runs claude-multi's own copy holds its use lock."""

    def check(self, trusted: probe.TrustedExecutable, verify) -> tuple[object, list]:
        seen: list = []

        def runs(_request, _trusted, _timeout):
            seen.append(("runs", retention.use_state(self.env, "2.1.200")))
            return "outcome"

        def verified(item):
            seen.append(("verify", retention.use_state(self.env, "2.1.200")))
            return verify(item)

        with mock.patch.object(probe, "network_isolation_available", return_value=None), \
                mock.patch.object(probe, "trusted_from_contract", return_value=trusted), \
                mock.patch.object(probe, "_verify_trusted_executable", side_effect=verified), \
                mock.patch.object(probe, "_exact_client_runs", side_effect=runs):
            outcome = probe.run_exact_client_check(None, native_contract={}, environ=self.env)
        return outcome, seen

    def test_the_owned_copy_is_locked_from_verification_through_the_runs(self):
        owned = pin.owned_path(self.env, "2.1.200", self.platform)
        outcome, seen = self.check(probe.TrustedExecutable(owned, "0" * 64), lambda item: item.resolved_path)
        self.assertEqual(outcome, "outcome")
        self.assertEqual(seen, [("verify", True), ("runs", True)])
        self.assertIs(retention.use_state(self.env, "2.1.200"), False)

    def test_a_failed_verification_releases_it_and_other_files_take_no_lock(self):
        owned = pin.owned_path(self.env, "2.1.200", self.platform)

        def refuse(_item):
            raise probe.ProbeError("drifted")

        outcome, _seen = self.check(probe.TrustedExecutable(owned, "0" * 64), refuse)
        self.assertEqual(outcome.reason, "unavailable")
        self.assert_free("2.1.200")
        native = self.home / ".local/share/claude/versions/2.1.200"
        native.parent.mkdir(parents=True)
        native.write_bytes(FAKE)
        (pin.owned_root(self.env) / "2.1.200" / pin.IN_USE).unlink()
        outcome, seen = self.check(probe.TrustedExecutable(native, "0" * 64), lambda item: item.resolved_path)
        self.assertEqual(outcome, "outcome")
        self.assertEqual(seen, [("verify", False), ("runs", False)])
        self.assertFalse((pin.owned_root(self.env) / "2.1.200" / pin.IN_USE).exists())


class NativeProbeHoldTests(PruneCase):
    """Both native probe runners lock claude-multi's own copy before its hash
    check and hand that lock to the client they start."""

    # The "owned copy": it reports which of its descriptors is its version's
    # use lock, then waits for the test (a sync directory, or the PTY).
    CLIENT = (
        "import os, sys, time\n"
        "lock = os.stat(os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), '.in-use'))\n"
        "held = []\n"
        "for name in os.listdir('/dev/fd'):\n"
        "    try:\n"
        "        info = os.fstat(int(name))\n"
        "    except OSError:\n"
        "        continue\n"
        "    if (info.st_dev, info.st_ino) == (lock.st_dev, lock.st_ino):\n"
        "        held.append(name)\n"
        "print('LOCK_FDS=%d' % len(held), flush=True)\n"
        "if sys.argv[1:] == ['pty']:\n"
        "    print('READY', flush=True)\n"
        "    input()\n"
        "elif sys.argv[1:]:\n"
        "    open(os.path.join(sys.argv[1], 'started'), 'w').close()\n"
        "    deadline = time.monotonic() + 30\n"
        "    while not os.path.exists(os.path.join(sys.argv[1], 'go')) and time.monotonic() < deadline:\n"
        "        time.sleep(0.02)\n"
    )

    def setUp(self):
        super().setUp()
        data = f"#!{sys.executable}\n{self.CLIENT}".encode()
        self.copy = self.owned("2.1.200", data)
        self.old = _contract("2.1.200", self.platform, data)
        self.fixture = probe.build_fixture(self.home / "fixture", environ=self.env)
        found = probe.trusted_from_contract(self.old, self.env)
        self.trusted = replace(found, fake=True)
        # The run's own environment names another owned root: the lock is
        # taken where the copy was found.
        self.elsewhere = {"HOME": str(self.home / "elsewhere")}

    def during_the_check(self) -> tuple[list, object]:
        """A prune between the lock and the hash check: (outcomes, patch)."""

        seen: list = []
        verify = probe._verify_trusted_executable

        def verified(trusted):
            seen.append(self.prune_copies())
            return verify(trusted)
        return seen, mock.patch.object(probe, "_verify_trusted_executable", side_effect=verified)

    def wait_free(self, version: str) -> None:
        deadline = time.monotonic() + 10
        while (lock := self.exclusive(version)) is None:
            self.assertLess(time.monotonic(), deadline, f"{version} stayed locked")
            time.sleep(0.05)
        lock.release()

    def test_the_owned_copy_is_found_with_its_owned_root(self):
        self.assertEqual(self.trusted.owned_root, pin.owned_root(self.env))
        native = self.home / ".local/share/claude/versions/2.1.200"
        native.parent.mkdir(parents=True)
        native.write_bytes(self.copy.read_bytes())
        native.chmod(0o755)
        found = probe.trusted_from_contract(self.old, self.env)
        self.assertEqual(found.resolved_path, native)
        self.assertIsNone(found.owned_root)  # a native install takes no lock

    def test_run_native_hands_the_lock_to_the_client_and_pruning_keeps_the_copy(self):
        sync = state.ensure_private_dir(self.home / "sync")
        seen, verifying = self.during_the_check()
        outcome: list = []

        def run():
            try:
                outcome.append(probe.run_native([str(sync)], trusted=self.trusted, fixture=self.fixture,
                                                timeout=30, environ=self.elsewhere))
            except BaseException as exc:  # noqa: BLE001 - handed to the test thread
                outcome.append(exc)

        with verifying:
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 30
            while not (sync / "started").exists() and thread.is_alive():
                self.assertLess(time.monotonic(), deadline, "the client never started")
                time.sleep(0.02)
            while_running = self.prune_copies()
            (sync / "go").write_bytes(b"")
            thread.join(30)
        self.assertFalse(thread.is_alive())
        result = outcome[0]
        self.assertIsInstance(result, probe.NativeRunResult, result)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LOCK_FDS=1", result.stdout)  # the client holds the lock itself
        self.assertEqual(seen, [retention.PruneOutcome(("2.1.250",), ("2.1.200",))])
        self.assertEqual(while_running, retention.PruneOutcome((), ("2.1.200",)))
        self.assert_free("2.1.200")  # released with the run
        self.assertEqual(self.prune_copies().removed, ("2.1.200",))

    def test_run_native_pty_hands_the_lock_to_the_client_and_pruning_keeps_the_copy(self):
        seen, verifying = self.during_the_check()
        while_running: list = []
        step = probe.PTYInteraction(b"READY", b"done\n",
                                    before_send=lambda: while_running.append(self.prune_copies()))
        with verifying:
            result = probe.run_native_pty(["pty"], [step], trusted=self.trusted, fixture=self.fixture,
                                          timeout=30, environ=self.elsewhere)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("LOCK_FDS=1", result.stdout)
        self.assertEqual(seen, [retention.PruneOutcome(("2.1.250",), ("2.1.200",))])
        self.assertEqual(while_running, [retention.PruneOutcome((), ("2.1.200",))])
        self.assert_free("2.1.200")

    def test_a_refused_run_releases_the_lock_and_a_removed_copy_is_refused(self):
        with self.assertRaisesRegex(probe.ProbeError, "hash"):
            probe.run_native([], trusted=replace(self.trusted, sha256="0" * 64), fixture=self.fixture)
        self.assert_free("2.1.200")
        self.assertEqual(self.prune_copies().removed, ("2.1.200", "2.1.250"))
        for runner in (probe.run_native, lambda *a, **k: probe.run_native_pty(a[0], (), **k)):
            with self.assertRaisesRegex(probe.ProbeError, "does not exist"):
                runner([], trusted=self.trusted, fixture=self.fixture)
        self.assertFalse((pin.owned_root(self.env) / "2.1.200").exists())  # the lock makes no directory
        with self.assertRaisesRegex(probe.ProbeError, "not an owned copy"):
            probe.run_native([], trusted=replace(self.trusted, owned_root=self.home), fixture=self.fixture)

    def test_the_isolated_client_inherits_the_lock_through_the_wrapper(self):
        reason = probe.network_isolation_available()
        if reason is not None:
            self.skipTest(f"BOUNDARY: {reason}")
        seen, verifying = self.during_the_check()
        with verifying:
            result = probe.run_native([], trusted=replace(self.trusted, fake=False), fixture=self.fixture,
                                      allow_real=True, environ=self.elsewhere, timeout=30,
                                      live_daemon_domain=self.home / "no-daemon")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LOCK_FDS=1", result.stdout)  # through bwrap and the namespace wrapper's exec
        self.assertEqual(seen, [retention.PruneOutcome(("2.1.250",), ("2.1.200",))])
        self.wait_free("2.1.200")  # the namespace bridge, a child too, exits with the client


class OwnedCopyLaunchTests(V4Case):
    """The launch path copies a hash-identical retained copy, then runs it."""

    def setUp(self):
        super().setUp()
        self.owned = self.fake_binary
        self.retained_root = paths.retained_root(self.env)
        state.ensure_private_dir(self.retained_root)
        self.retained = self.retained_root / "2.1.281"
        self.retained.write_bytes(self.owned.read_bytes())
        self.retained.chmod(0o755)
        self.owned.unlink()
        os.rmdir(self.owned.parent)

    def exclusive(self) -> pin.UseLock | None:
        return pin.take_use_lock(self.env, "2.1.281", shared=False, blocking=False)

    def test_migration_precedes_readiness_and_the_owned_copy_executes(self):
        def ready(_base, _path):
            self.assertTrue(self.owned.is_file())
            return 200
        self.runtime.health_get = ready
        self.launch_fresh()
        self.assertEqual(self.execs[-1][0], str(self.owned))
        self.assertTrue(self.retained.is_file())  # copied, never moved
        self.assertFalse(retention.same_file(self.owned, self.retained))

    def test_the_exec_inherits_the_use_lock(self):
        seen = []

        def execve(path, argv, env):
            held = [fd for fd in map(int, os.listdir("/dev/fd")) if self.is_use_lock(fd)]
            seen.append((path, [os.get_inheritable(fd) for fd in held], self.exclusive()))
            return 0

        self.runtime.execve = execve
        self.launch_fresh()  # copies the retained copy, then locks it
        self.launch_fresh()  # the copy is in place: locked before its check
        self.assertEqual(seen, [(str(self.owned), [True], None)] * 2)
        lock = self.exclusive()  # released when the exec seam returned
        self.assertIsNotNone(lock)
        lock.release()

    def is_use_lock(self, fd: int) -> bool:
        try:
            info = os.fstat(fd)
            target = os.lstat(self.owned.parent / pin.IN_USE)
        except OSError:
            return False
        return (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino)

    def test_a_failed_exec_releases_the_use_lock(self):
        def execve(_path, _argv, _env):
            self.assertIsNone(self.exclusive())
            raise OSError(8, "Exec format error")

        self.runtime.execve = execve
        with self.assertRaises(OSError):
            self.launch_fresh()
        lock = self.exclusive()
        self.assertIsNotNone(lock)
        lock.release()

    def test_read_only_runtime_never_migrates(self):
        self.runtime._shims_refreshed = False
        with self.assertRaisesRegex(launch.LaunchError, "not set up for claude-multi"):
            self.launch_fresh()
        self.assertFalse(self.owned.exists())
        self.assertFalse(self.owned.parent.exists())  # the lock creates no version directory

    def test_verification_failure_checks_no_gateway(self):
        self.retained.write_bytes(b"tampered")
        with mock.patch.object(self.runtime, "health_get", side_effect=AssertionError("no readiness")), \
             self.assertRaisesRegex(launch.LaunchError, "claude-multi setup --step claude"):
            self.launch_fresh()
        self.assertFalse(self.owned.exists())

    def test_print_launch_never_writes_the_owned_copy(self):
        output = io.StringIO()
        cli.main(["--profile", "balanced", "--print-launch"], runtime=self.runtime,
                 output_stream=output, interactive=False)
        self.assertFalse(self.owned.exists())

    def test_doctor_blocks_a_damaged_copy_even_with_a_retained_one(self):
        # The launch copies a retained copy only when its own is missing.
        data = self.retained.read_bytes()
        state.ensure_private_dir(self.owned.parent)
        self.owned.write_bytes(bytes([data[0] ^ 1]) + data[1:])  # same size, other bytes
        self.owned.chmod(0o755)
        runtime = self.make_runtime(doctor_binary_callback=None)
        problems, info, _attention = cli._collect_doctor_reports(runtime)
        self.assertTrue(any("does not match the verified sha256" in line for line in problems), problems)
        self.assertFalse(any("the next launch copies" in line for line in info))
        with self.assertRaisesRegex(launch.LaunchError, "does not match the verified sha256"):
            launch.resolve_claude(self.runtime.catalog.docs["native-contract"], environ=self.env,
                                  retained_root=self.retained_root)

    def test_doctor_default_callback_reports_the_pending_copy(self):
        runtime = self.make_runtime(doctor_binary_callback=None)
        problems, info, _attention = cli._collect_doctor_reports(runtime)
        self.assertEqual(problems, [])
        self.assertTrue(any("the next launch copies the verified retained copy" in line for line in info))
        self.assertFalse(self.owned.exists())

    def test_cli_stop_runs_the_owned_copy_under_its_use_lock(self):
        self.retained.unlink()
        state.ensure_private_dir(self.owned.parent)
        self.owned.write_bytes(b"#!/bin/fake-claude\n")
        self.owned.chmod(0o755)
        record = self.launch_fresh()
        mid = record["managed_id"]
        output = io.StringIO()
        held = []

        def run(argv, **_kwargs):
            held.append(self.exclusive())
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset({mid[:8]})), \
             mock.patch.object(subprocess, "run", side_effect=run) as stop:
            code = cli.main(["sessions", "stop", mid, "--yes"], runtime=self.runtime,
                            output_stream=output, interactive=False)
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(stop.call_args.args[0], [str(self.owned), "stop", record["runtime_session_id"]])
        self.assertEqual(held, [None])
        lock = self.exclusive()
        self.assertIsNotNone(lock)
        lock.release()

    def test_stop_resume_runs_the_owned_copy(self):
        record = self.launch_fresh()
        mid = record["managed_id"]
        self.runtime.live_prefixes = frozenset({mid[:8]})
        gate = cli._evaluate_resume_gate(self.runtime, record, live_prefixes=self.runtime.live_prefixes)
        self.assertIn("stop-resume", [action for action, _label in gate.actions])

        def stopped(*args, **kwargs):
            self.runtime.live_prefixes = frozenset()
            return subprocess.CompletedProcess(args[0], 0, "", "")
        with mock.patch.object(claude_multi.tui.Modal, "run", return_value="stop-resume"), \
             mock.patch('claude_multi.cli.session_facts._live_background_prefixes', side_effect=[frozenset({mid[:8]}), frozenset()]), \
             mock.patch.object(subprocess, "run", side_effect=stopped) as run:
            outcome = cli._run_resume_gate_modal(self.runtime, record, gate, None, None)
        self.assertEqual(outcome[0], "resume")
        self.assertEqual(run.call_args.args[0][0], str(self.owned))
        self.runtime.perform(self.prepare_resume(mid), resume_decision=outcome[2])
        self.assertEqual(self.execs[-1][0], str(self.owned))
        self.assertEqual(stat.S_IMODE(os.stat(self.owned).st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
