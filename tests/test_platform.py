"""The Linux/POSIX platform seams (primitives, backends, observations).

Every live read is a fixture: a temporary ``/proc`` tree, an injected manager
runner, injected listener/PID observers. Nothing here reads the real service
manager, the real gateway or real process tables.
"""

from __future__ import annotations

import ast
import errno
import functools
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _layout import REPO_ROOT
from claude_multi import management, proxy, service, sessions, state
from claude_multi.platform import linux_process, linux_service, observation, posix_fs

SRC = REPO_ROOT / "src" / "claude_multi"
PLATFORM = SRC / "platform"
GATEWAY_URL = "http://127.0.0.1:8333"


def _unavailable_manager(*_args, **_kwargs):
    raise FileNotFoundError("no service manager in this fixture")


class _Private(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="cm-platform-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        os.chmod(self.root, 0o700)


class PackageBoundaryTests(unittest.TestCase):
    def test_backends_never_import_their_facades(self) -> None:
        for path in sorted(PLATFORM.glob("*.py")):
            imported = set()
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, path.name)
                    imported.add(node.module)
            product = {name for name in imported if name.startswith("claude_multi")}
            with self.subTest(module=path.name):
                self.assertLessEqual(product, {"claude_multi.platform"}, product)
        self.assertEqual(ast.parse((PLATFORM / "__init__.py").read_text()).body[1:], [])

    def test_package_import_loads_no_backend(self) -> None:
        code = ("import sys, claude_multi.platform, claude_multi.state, claude_multi.sessions\n"
                "print(sorted(m for m in sys.modules if m.startswith('claude_multi.platform')))\n")
        env = {**os.environ, "PYTHONPATH": str(SRC.parent)}
        out = subprocess.run([sys.executable, "-B", "-c", code], env=env, text=True,
                             capture_output=True, check=True, timeout=30).stdout
        self.assertEqual(out.strip(), "['claude_multi.platform', 'claude_multi.platform.observation', "
                                      "'claude_multi.platform.posix_fs']")

    def test_compatibility_aliases_keep_identity(self) -> None:
        self.assertIs(service.OwnerVerdict, observation.OwnerVerdict)
        self.assertIs(service.ExecReloadResult, observation.ExecReloadResult)
        self.assertIs(sessions.BackgroundLiveness, observation.BackgroundLiveness)
        self.assertIs(state._fsync_directory, posix_fs.fsync_directory)
        self.assertIs(service._manager_property, linux_service.manager_property)
        self.assertEqual(service._VERBS, linux_service.VERBS)

    def test_facade_hint_text_is_the_backend_text(self) -> None:
        # Where the unit exists, diagnostics are the backend's own text;
        # control verbs stay the guarded product verbs on every backend.
        systemd = service.Backend(service.SYSTEMD, service.UNIT)
        for verb in ("reload", "reset-failed", "why"):
            with self.subTest(verb=verb):
                self.assertEqual(service.hint(verb, backend=systemd), linux_service.hint(verb, unit=service.UNIT))
        for verb in ("start", "stop", "restart", "status"):
            with self.subTest(verb=verb):
                self.assertNotIn("systemctl", service.hint(verb, backend=systemd))
        self.assertEqual(service.journal_argv(), linux_service.journal_argv(since="-24h", unit=service.UNIT))
        with self.assertRaises(ValueError):
            service.hint("enable")

    def test_gnu_only_remedy_text_is_unchanged(self) -> None:
        error = management.ManagementKeyError("not a regular file")
        path = '"$HOME/.config/claude-multi/management-key"'
        backup = '"$HOME/.config/claude-multi/management-key.rejected"'
        self.assertEqual(error.remedy, f"test ! -e {backup} && test ! -L {backup} && mv -T -- {path} {backup}"
                                       " && claude-multi-proxy rotate-management-key")


class PosixFaultInjectionTests(_Private):
    def test_directory_fsync_failure_raises_and_closes(self) -> None:
        opened = []
        real_open = os.open

        def tracking_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        with mock.patch.object(os, "open", tracking_open), \
                mock.patch.object(os, "fsync", side_effect=OSError(errno.EIO, "injected")):
            with self.assertRaises(OSError):
                posix_fs.fsync_directory(self.root)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])  # closed despite the failure

    def test_state_write_reports_committed_state_through_the_primitive(self) -> None:
        directory = state.ensure_private_dir(self.root / "state")
        target = directory / "record.json"
        state.atomic_write(target, b"old")
        with mock.patch.object(os, "fsync", side_effect=[None, OSError(errno.EIO, "dir fsync")]):
            with self.assertRaisesRegex(state.CommittedStateError, "directory fsync failed"):
                state.atomic_write(target, b"new")
        self.assertEqual(target.read_bytes(), b"new")

    def test_replace_is_one_call_and_never_retried(self) -> None:
        directory = state.ensure_private_dir(self.root / "state")
        target = directory / "record.json"
        state.atomic_write(target, b"old")
        for number in (errno.EBUSY, errno.EACCES, errno.EXDEV):
            with self.subTest(errno=number), \
                    mock.patch.object(os, "replace", side_effect=OSError(number, "injected")) as replace, \
                    mock.patch("time.sleep", side_effect=AssertionError("no delay")):
                with self.assertRaises(OSError) as caught:
                    state.atomic_write(target, b"new")
                self.assertEqual(caught.exception.errno, number)
                self.assertEqual(replace.call_count, 1)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual([p.name for p in directory.iterdir()], ["record.json"])

    def test_lock_failure_closes_the_descriptor_and_holds_nothing(self) -> None:
        directory = state.ensure_private_dir(self.root / "locks")
        lock = state.FileLock(directory / "target")
        closed = []
        real_close = os.close
        with mock.patch.object(posix_fs, "lock_descriptor", side_effect=OSError(errno.ENOLCK, "injected")), \
                mock.patch.object(os, "close", side_effect=lambda fd: (closed.append(fd), real_close(fd))):
            with self.assertRaises(OSError):
                lock.acquire()
        self.assertEqual(len(closed), 1)
        other = state.FileLock(directory / "target")
        self.assertTrue(other.acquire(blocking=False))
        other.release()
        with lock:
            pass

    def test_primitive_shared_exclusive_and_non_reentrant(self) -> None:
        path = self.root / "lockfile"
        path.write_bytes(b"")
        first = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        second = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        third = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        self.addCleanup(os.close, first)
        self.addCleanup(os.close, second)
        self.addCleanup(os.close, third)
        posix_fs.lock_descriptor(first, shared=True, blocking=False)
        posix_fs.lock_descriptor(second, shared=True, blocking=False)
        with self.assertRaises(BlockingIOError):
            posix_fs.lock_descriptor(third, shared=False, blocking=False)
        posix_fs.unlock_descriptor(first)
        posix_fs.unlock_descriptor(second)
        posix_fs.lock_descriptor(first, shared=False, blocking=False)
        # Same process, same file, another descriptor: not re-entrant.
        for shared in (False, True):
            with self.assertRaises(BlockingIOError):
                posix_fs.lock_descriptor(second, shared=shared, blocking=False)
        posix_fs.unlock_descriptor(first)
        posix_fs.lock_descriptor(second, shared=False, blocking=False)
        posix_fs.unlock_descriptor(second)

    def test_ownership_and_mode_predicates(self) -> None:
        path = self.root / "file"
        path.write_bytes(b"")
        os.chmod(path, 0o600)
        info = os.lstat(path)
        self.assertTrue(posix_fs.owned_by_caller(info))
        self.assertEqual(posix_fs.shared_mode_bits(info), 0)
        os.chmod(path, 0o640)
        self.assertEqual(posix_fs.shared_mode_bits(os.lstat(path)), 0o040)
        with mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            self.assertFalse(posix_fs.owned_by_caller(info))
            with self.assertRaisesRegex(state.StateError, "owner-controlled"):
                state.read_private(path)

    def test_exec_replace_resolves_execve_at_call_time(self) -> None:
        with mock.patch.object(os, "execve", return_value="replaced") as execve:
            self.assertEqual(posix_fs.exec_replace("/fixture/bin", ["/fixture/bin"], {}), "replaced")
        execve.assert_called_once_with("/fixture/bin", ["/fixture/bin"], {})


class ServiceBackendTests(unittest.TestCase):
    def test_missing_manager_is_unknown_never_stopped(self) -> None:
        self.assertIsNone(service.manager_state(runner=_unavailable_manager))
        self.assertIsNone(linux_service.manager_property("MainPID", "unit", _unavailable_manager))
        self.assertIsNone(service.last_reload_result(runner=_unavailable_manager))
        with self.assertRaises(service.ServiceError):
            service.service_active(runner=_unavailable_manager)
        failed = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "no bus"))
        self.assertIsNone(service.manager_state(runner=failed))
        with self.assertRaises(service.ServiceError):
            service.service_active(runner=failed)

    def test_manager_reads_are_single_and_pinned(self) -> None:
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "inactive\n", ""))
        self.assertEqual(service.manager_state(runner=runner), "inactive")
        runner.assert_called_once()
        argv = runner.call_args.args[0]
        self.assertEqual(argv[-1], service.UNIT)
        self.assertIn("--property=ActiveState", argv)
        self.assertEqual(runner.call_args.kwargs["env"]["LC_ALL"], "C")

    def test_journal_records_end_at_a_line_feed_only(self) -> None:
        # A carriage return, a vertical tab, a next-line character or a
        # Unicode line or paragraph separator stays inside its record.
        records = ["one\rtwo", "three\x0bfour", "five\u0085six", "seven eight", "nine ten", "last"]
        raw = ("\n".join(records) + "\n").encode()
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, raw, b""))
        self.assertEqual(linux_service.journal_tail(unit="unit", lines=10, runner=runner), records)
        self.assertEqual(linux_service.journal_tail(unit="unit", lines=2, runner=runner), records[-2:])
        self.assertEqual(runner.call_args.args[0][:5], ["journalctl", "--user", "-u", "unit", "-n"])
        # A bounded read drops the partial record it starts inside.
        self.assertEqual(linux_service.journal_tail(unit="unit", lines=10, runner=runner,
                                                    max_bytes=len(raw) - 2), records[1:])
        runner.return_value = subprocess.CompletedProcess([], 1, b"", b"no journal")
        self.assertIsNone(linux_service.journal_tail(unit="unit", lines=10, runner=runner))


class _Gateway(_Private):
    """A fixture gateway: private workdir, exec stamps, a /proc tree and binaries."""

    NS = "pid:[101]"

    def setUp(self) -> None:
        super().setUp()
        self.state_root = self.root / "state"
        self.workdir = service.ensure_gateway_workdir(self.state_root)
        self.proc = self.root / "proc"
        (self.proc / "self/ns").mkdir(parents=True)
        (self.proc / "self/ns/pid").symlink_to(self.NS)
        (self.proc / "net").mkdir()
        self.tables()
        self.binary = self.root / "gateway-binary"
        self.binary.write_text("fixture gateway")
        self.other = self.root / "unrelated-binary"
        self.other.write_text("fixture other program")
        self.runner = mock.Mock(side_effect=_unavailable_manager)

    def tables(self, rows: str = "") -> None:
        for name in ("tcp", "tcp6"):
            (self.proc / "net" / name).write_text("sl local_address rem_address st ...\n" + rows)

    @staticmethod
    def row(uid: int, inode: str = "42") -> str:
        return f" 0: 0100007F:208D 00000000:0000 0A 0:0 00:0 0 {uid} 0 {inode}\n"

    def stamp(self, pid: int, started_at: str, namespace: str | None = NS) -> None:
        service.write_exec_stamp(self.workdir, service.ExecStamp(
            "3.1.0-dev", "signature", pid, str(self.binary), started_at, namespace))

    def process(self, pid: int, exe: Path) -> None:
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / "exe").symlink_to(exe)

    def pid_get(self, **kwargs):
        return service.gateway_pid(**kwargs, proc_root=self.proc, runner=self.runner)

    def listener(self, base_url: str):
        return service.listener_owner(base_url, proc_root=self.proc, euid=os.geteuid(), runner=self.runner)

    def observe(self) -> observation.GatewayObservation:
        return service.gateway_observation(GATEWAY_URL, state_root=self.state_root,
                                           listener=self.listener, pid_get=self.pid_get)


class GatewayObservationTests(_Gateway):
    def test_liveness_words_are_explicit(self) -> None:
        self.assertEqual([observation.liveness(v) for v in (True, False, None, 1, "stopped")],
                         ["running", "stopped", "unknown", "unknown", "unknown"])

    def test_missing_capabilities_are_unknown(self) -> None:
        # No /proc at all, no stamp and no manager.
        missing = service.gateway_observation(
            GATEWAY_URL, state_root=self.root / "absent-state",
            listener=lambda url: service.listener_owner(url, proc_root=self.root / "no-proc", runner=self.runner),
            pid_get=lambda **kw: service.gateway_pid(**kw, proc_root=self.root / "no-proc", runner=self.runner))
        self.assertEqual((missing.process.state, missing.listener.kind), ("unknown", "unknown"))
        self.assertFalse(missing.stopped_proof)
        # A non-Linux kernel has no listener table: unknown, never "none".
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(self.observe().listener.kind, "unknown")
            self.assertFalse(self.observe().stopped_proof)
        # A stamp from another PID namespace cannot prove absence.
        self.stamp(42, "2026-09-29T01:00:00+00:00", namespace="pid:[999]")
        self.assertEqual(self.observe().process.state, "unknown")
        # A stamp without a PID namespace has no identity and no proof.
        self.stamp(42, "2026-09-29T01:00:00+00:00", namespace=None)
        result = self.observe()
        self.assertEqual((result.process.state, result.process.instance), ("unknown", None))

    def test_unobservable_listener_is_unknown_and_skips_the_process(self) -> None:
        pid_get = mock.Mock(side_effect=AssertionError("process read after a failed listener read"))
        for error in (PermissionError("proc"), service.ServiceError("manager")):
            with self.subTest(error=type(error).__name__):
                result = service.gateway_observation(
                    GATEWAY_URL, state_root=self.state_root,
                    listener=mock.Mock(side_effect=error), pid_get=pid_get)
                self.assertEqual((result.process.state, result.listener.kind), ("unknown", "unknown"))
                self.assertFalse(result.stopped_proof)
        pid_get.assert_not_called()

    def test_each_observation_is_read_once(self) -> None:
        listener = mock.Mock(return_value=service.OwnerVerdict("none", "fixture"))
        pid_get = mock.Mock(return_value=(None, False))
        self.assertTrue(service.gateway_observation(GATEWAY_URL, state_root=self.state_root,
                                                    listener=listener, pid_get=pid_get).stopped_proof)
        listener.assert_called_once_with(GATEWAY_URL)
        pid_get.assert_called_once_with(state_root=self.state_root)

    def test_pid_reuse_is_unknown_not_stopped_or_running(self) -> None:
        self.stamp(42, "2026-09-29T01:00:00+00:00")
        self.process(42, self.other)  # the recorded PID now runs another program
        result = self.observe()
        self.assertEqual((result.process.state, result.process.pid), ("unknown", None))
        self.assertFalse(result.stopped_proof)

    def test_absent_recorded_process_with_no_listener_is_the_stopped_proof(self) -> None:
        self.stamp(42, "2026-09-29T01:00:00+00:00")
        result = self.observe()
        self.assertEqual((result.process.state, result.process.pid, result.listener.kind),
                         ("stopped", 42, "none"))
        self.assertEqual(result.process.instance, f"{self.NS}/42@2026-09-29T01:00:00+00:00")
        self.assertTrue(result.stopped_proof)
        # Any listener on the port, even our own uid's, withdraws the proof.
        self.tables(self.row(os.geteuid()))
        self.assertFalse(self.observe().stopped_proof)
        self.tables(self.row(os.geteuid() + 1))
        self.assertEqual(self.observe().listener.kind, "foreign")
        self.assertFalse(self.observe().stopped_proof)

    def test_replacement_changes_the_instance_and_is_running(self) -> None:
        self.stamp(42, "2026-09-29T01:00:00+00:00")
        first = self.observe()
        self.stamp(43, "2026-09-29T02:00:00+00:00")
        self.process(43, self.binary)
        second = self.observe()
        self.assertEqual((second.process.state, second.process.pid), ("running", 43))
        self.assertNotEqual(first.process.instance, second.process.instance)
        self.assertFalse(second.stopped_proof)

    def test_manager_main_pid_zero_needs_independent_absence_evidence(self) -> None:
        zero = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "0", ""))

        def observe(proc_root: Path) -> observation.GatewayObservation:
            return service.gateway_observation(
                GATEWAY_URL, state_root=self.state_root, listener=self.listener,
                pid_get=lambda **kw: service.gateway_pid(**kw, proc_root=proc_root, runner=zero))

        # The legitimate first start: no recorded start and an observable /proc.
        first = observe(self.proc)
        self.assertEqual((first.process.state, first.listener.kind), ("stopped", "none"))
        self.assertTrue(first.stopped_proof)
        # No /proc: MainPID=0 cannot override the missing capability.
        missing = observe(self.root / "no-proc")
        self.assertEqual((missing.process.state, missing.listener.kind), ("unknown", "none"))
        self.assertFalse(missing.stopped_proof)
        # A recorded start in another PID namespace is unobservable here.
        self.stamp(42, "2026-09-29T01:00:00+00:00", namespace="pid:[999]")
        private = observe(self.proc)
        self.assertEqual((private.process.state, private.listener.kind), ("unknown", "none"))
        self.assertFalse(private.stopped_proof)
        # In this namespace, a gone or reused recorded PID is the absence evidence.
        self.stamp(42, "2026-09-29T01:00:00+00:00")
        self.assertTrue(observe(self.proc).stopped_proof)
        self.process(42, self.other)
        self.assertTrue(observe(self.proc).stopped_proof)
        # A readable recorded PID whose executable cannot be checked stays unknown.
        (self.proc / "42/exe").unlink()
        self.assertFalse(observe(self.proc).stopped_proof)

    def test_main_pid_zero_without_proc_never_authorizes_auth_restore(self) -> None:
        zero = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "0", ""))
        home = self.root / "home"
        home.mkdir(mode=0o700)
        environ = {"HOME": str(home), "XDG_STATE_HOME": str(self.root / "xdg-state")}
        gateway_info = {"service": service.UNIT, "base_url": GATEWAY_URL, "health_path": "/healthz"}
        for namespace, proc_root in ((None, self.root / "no-proc"), ("pid:[999]", self.proc)):
            with self.subTest(namespace=namespace):
                state_root = sessions.state_root(environ)
                service.ensure_gateway_workdir(state_root)
                if namespace is not None:
                    service.write_exec_stamp(service.gateway_workdir(state_root), service.ExecStamp(
                        "3.1.0-dev", "signature", 42, str(self.binary), "2026-09-29T01:00:00+00:00", namespace))

                def observe(proc_root=proc_root, **kwargs):
                    return service.gateway_status(
                        **kwargs, proc_root=proc_root,
                        health_get=mock.Mock(side_effect=ConnectionRefusedError()),
                        pid_get=lambda **kw: service.gateway_pid(**kw, runner=zero))

                self.assertEqual(observe(base_url=GATEWAY_URL, health_path="/healthz", state_root=state_root,
                                         manager=lambda: "inactive").state, "unknown")
                with self.assertRaisesRegex(proxy.ProxyError, "refusing to restore while .* is unknown"):
                    proxy.restore_auth_dir(gateway_info, "snapshot", environ=environ,
                                           service_active=lambda _unit: False, gateway_status=observe)

    def test_a_start_record_changed_during_the_observation_is_unknown(self) -> None:
        self.stamp(42, "2026-09-29T01:00:00+00:00")

        def replaced_after_the_pid_read(**kwargs):
            result = self.pid_get(**kwargs)
            self.stamp(43, "2026-09-29T02:00:00+00:00")
            self.process(43, self.binary)
            return result

        result = service.gateway_observation(GATEWAY_URL, state_root=self.state_root,
                                             listener=self.listener, pid_get=replaced_after_the_pid_read)
        self.assertEqual((result.process.state, result.process.pid, result.process.instance),
                         ("unknown", None, None))
        self.assertFalse(result.stopped_proof)

    def test_a_manager_reported_pid_never_takes_the_recorded_identity(self) -> None:
        self.stamp(42, "2026-09-29T01:00:00+00:00")
        self.process(43, self.binary)
        self.runner.side_effect = None
        self.runner.return_value = subprocess.CompletedProcess([], 0, "43", "")
        result = self.observe()
        self.assertEqual((result.process.state, result.process.pid, result.process.instance),
                         ("running", 43, None))


class ManagementPreparationTests(_Gateway):
    """Promotion only above the backend's stopped proof."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.environ = {"HOME": str(self.home), "CLAUDE_MULTI_PROXY_BIN": str(self.binary),
                        "CLAUDE_MULTI_PROXY_PATCHES": management.ALLOWLIST_PATCH,
                        management.CHANNEL_ENV: management.MANAGEMENT_CHANNEL}
        self.gateway = {"gateway": {"base_url": GATEWAY_URL}}
        self.selection = management.key_dir(self.home) / management.PREPARED_FILE

    def prepare(self) -> str:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            proxy._init_management(self.home, self.environ, prepare_start=True, gateway=self.gateway,
                                   state_root=self.state_root, listener_observer=self.listener,
                                   pid_get=self.pid_get)
        return err.getvalue()

    def test_unknown_never_promotes(self) -> None:
        management.ensure(self.home)
        staged = management.key_state(self.home)
        self.assertTrue(staged.pending)
        # No stamp, no manager: the process is unknown.
        self.assertIn("not confirmed", self.prepare())
        self.assertEqual(self.selection.read_bytes(), b"")
        self.assertIsNone(management.read_active(self.home))
        self.runner.assert_called()  # the MainPID fallback was consulted, and was unavailable

    def test_pid_reuse_never_promotes(self) -> None:
        management.ensure(self.home)
        self.stamp(42, "2026-09-29T01:00:00+00:00")
        self.process(42, self.other)
        self.assertIn("not confirmed", self.prepare())
        self.assertIsNone(management.read_active(self.home))

    def test_failed_start_repeats_the_same_selection_and_is_not_exec_proof(self) -> None:
        management.ensure(self.home)
        self.stamp(42, "2026-09-29T01:00:00+00:00")
        self.assertIn("promoted", self.prepare())
        key = management.read_active(self.home)
        receipt = self.selection.read_bytes()
        self.assertIsNotNone(key)
        # The exec then fails: no process, no listener. The receipt did not
        # make the gateway running, and the next start selects the same key.
        self.assertEqual(self.observe().process.state, "stopped")
        self.assertEqual(self.prepare(), "")
        self.assertEqual((management.read_active(self.home), self.selection.read_bytes()), (key, receipt))

    def test_process_replacement_withdraws_the_selection(self) -> None:
        management.ensure(self.home)
        self.stamp(42, "2026-09-29T01:00:00+00:00")
        self.prepare()
        key = management.read_active(self.home)
        management.stage_rotation(self.home)
        # Another start replaced the process before this preparation ran.
        self.stamp(43, "2026-09-29T02:00:00+00:00")
        self.process(43, self.binary)
        self.assertIn("not confirmed", self.prepare())
        self.assertEqual(self.selection.read_bytes(), b"")
        self.assertEqual(management.read_active(self.home), None)  # no selection: nothing is read
        self.assertEqual(management.key_state(self.home).staged, True)
        self.assertEqual(management._slot(self.home, management.KEY_FILE), key)  # A never replaced

    def test_replacement_during_the_observation_never_promotes(self) -> None:
        management.ensure(self.home)
        self.stamp(42, "2026-09-29T01:00:00+00:00")
        self.prepare()
        key = management.read_active(self.home)
        management.stage_rotation(self.home)
        real = self.pid_get

        def interleaved(**kwargs):
            result = real(**kwargs)  # PID 42 observed absent ...
            self.stamp(43, "2026-09-29T02:00:00+00:00")  # ... then a replacement starts
            self.process(43, self.binary)
            return result

        self.pid_get = interleaved
        self.assertIn("not confirmed", self.prepare())
        self.assertEqual(self.selection.read_bytes(), b"")
        self.assertTrue(management.key_state(self.home).staged)
        self.assertEqual(management._slot(self.home, management.KEY_FILE), key)

    def test_replacement_before_the_promotion_boundary_withdraws_the_proof(self) -> None:
        management.ensure(self.home)
        self.stamp(42, "2026-09-29T01:00:00+00:00")
        self.prepare()
        key = management.read_active(self.home)
        management.stage_rotation(self.home)
        real_disabled = management._disabled

        def replaced_while_waiting_for_the_lock(home):
            # The first sample was a stopped proof; a replacement starts before
            # the promotion boundary, where the proof is revalidated.
            self.stamp(43, "2026-09-29T02:00:00+00:00")
            self.process(43, self.binary)
            return real_disabled(home)

        with mock.patch.object(management, "_disabled", side_effect=replaced_while_waiting_for_the_lock):
            self.assertIn("not confirmed", self.prepare())
        self.assertEqual(self.selection.read_bytes(), b"")
        self.assertTrue(management.key_state(self.home).staged)
        self.assertEqual(management._slot(self.home, management.KEY_FILE), key)

    def test_the_proof_is_revalidated_under_the_key_lock(self) -> None:
        management.ensure(self.home)
        self.selection.write_bytes(b"stale selection\n")
        # Called after the selection was invalidated, inside the locked section.
        seen = []
        confirm = mock.Mock(side_effect=lambda: seen.append(self.selection.read_bytes()) or False)
        self.assertEqual(management.prepare_start(self.home, stopped=confirm), ("start-unconfirmed",))
        confirm.assert_called_once_with()
        self.assertEqual(seen, [b""])
        self.stamp(42, "2026-09-29T01:00:00+00:00")
        first = self.observe()
        observe = mock.Mock(side_effect=self.observe)
        self.assertTrue(service.confirm_stopped(first, observe))
        observe.assert_called_once_with()
        unknown = mock.Mock(side_effect=AssertionError("no second sample without a first proof"))
        self.stamp(42, "2026-09-29T01:00:00+00:00", namespace="pid:[999]")
        self.assertFalse(service.confirm_stopped(self.observe(), unknown))


class OwnerBeforeCredentialTests(_Private):
    def test_no_authenticated_read_before_or_without_the_owner_check(self) -> None:
        home = self.root / "home"
        home.mkdir(mode=0o700)
        management.ensure(home)
        management.prepare_start(home, stopped=True)
        gateway = {"gateway": {"base_url": GATEWAY_URL}}
        for kind, reads in (("ours", 1), ("none", 1), ("unknown", 1), ("foreign", 0)):
            with self.subTest(kind=kind):
                events = []

                def owner(url, kind=kind):
                    events.append("owner")
                    return service.OwnerVerdict(kind, "fixture")

                def get(url, key):
                    events.append("get")
                    return 404, b""

                management.pool_status(home, gateway, get=get, owner_check=owner, attention=lambda _m: None,
                                       environ={management.CHANNEL_ENV: management.MANAGEMENT_CHANNEL})
                self.assertEqual(events, ["owner"] + ["get"] * reads)


class SessionScanTests(_Private):
    ID = "0f1e2d3c-4b5a-4968-8776-655443322110"

    def test_unlistable_table_is_unknown_and_compat_ids_stay_empty(self) -> None:
        missing = self.root / "no-proc"
        scan = sessions.proc_session_scan(missing)
        self.assertEqual((scan.known, scan.ids), (False, frozenset()))
        self.assertEqual(scan.reason, "FileNotFoundError")
        self.assertEqual(sessions.proc_session_ids(missing), frozenset())

    def test_listed_table_is_known_and_bounded_to_uuid_ids(self) -> None:
        proc = self.root / "proc"
        (proc / "7").mkdir(parents=True)
        (proc / "7/cmdline").write_bytes(b"claude\0--resume\0" + self.ID.encode() + b"\0")
        (proc / "8").mkdir()
        (proc / "8/cmdline").write_bytes(b"claude\0--session-id=not-a-uuid\0")
        scan = sessions.proc_session_scan(proc)
        self.assertEqual((scan.known, scan.ids), (True, frozenset({self.ID})))
        self.assertEqual(sessions.proc_session_scan(proc, exclude_pids={7}).ids, frozenset())
        self.assertEqual([p.name for _rid, p in sessions._proc_session_paths(proc)], ["7"])

    def test_a_scan_is_one_enumeration_and_its_failure_is_unknown(self) -> None:
        proc = self.root / "proc"
        proc.mkdir()
        with mock.patch.object(linux_process.os, "listdir",
                               side_effect=[[], PermissionError("fixture table became unreadable")]) as listdir:
            self.assertEqual(sessions.proc_session_scan(proc), observation.ProcessScan(True, frozenset()))
            scan = sessions.proc_session_scan(proc)
        self.assertEqual((scan.known, scan.ids, scan.reason), (False, frozenset(), "PermissionError"))
        self.assertEqual(listdir.call_count, 2)  # one enumeration per scan, never a second one

    def test_an_unreadable_argv_is_unknown_and_a_vanished_one_is_gone(self) -> None:
        # A listed process whose argv cannot be opened or read
        # may be the client naming the session; displays stay permissive.
        proc = self.root / "proc"
        (proc / "7").mkdir(parents=True)
        (proc / "7/cmdline").write_bytes(b"claude\0--resume\0" + self.ID.encode() + b"\0")
        (proc / "9").mkdir()  # exited before its argv was opened: gone, not unknown
        self.assertEqual(sessions.proc_session_scan(proc), observation.ProcessScan(True, frozenset({self.ID})))
        real_open = os.open

        def refuse_4242(path, *args, **kwargs):
            if str(path).endswith("/4242/cmdline"):
                raise PermissionError(errno.EACCES, "fixture argv refused")
            return real_open(path, *args, **kwargs)

        (proc / "4242").mkdir()
        (proc / "4242/cmdline").write_bytes(b"")
        with mock.patch.object(linux_process.os, "open", side_effect=refuse_4242):
            scan = sessions.proc_session_scan(proc)
            self.assertEqual(sessions.proc_session_ids(proc), frozenset({self.ID}))
        self.assertEqual((scan.known, scan.ids), (False, frozenset()))
        self.assertEqual(scan.reason, "PermissionError on process 4242")
        with mock.patch.object(linux_process.os, "open", side_effect=refuse_4242):
            self.assertTrue(sessions.proc_session_scan(proc, exclude_pids={4242}).known)
        (proc / "4242/cmdline").unlink()
        (proc / "4242/cmdline").mkdir()  # opens, then the read fails (EISDIR)
        scan = sessions.proc_session_scan(proc)
        self.assertEqual((scan.known, scan.reason), (False, "IsADirectoryError on process 4242"))
        self.assertEqual(sessions.proc_session_ids(proc), frozenset({self.ID}))
        with mock.patch.object(linux_process.os, "read",
                               side_effect=ProcessLookupError(errno.ESRCH, "fixture exited")):
            self.assertEqual(sessions.proc_session_scan(proc), observation.ProcessScan(True, frozenset()))

    def test_daemon_liveness_uncertainty_is_explicit(self) -> None:
        with mock.patch.object(os, "getuid", None):
            self.assertFalse(sessions.background_liveness(self.root).known)
        self.assertEqual(sessions.background_liveness(self.root / "absent"),
                         observation.BackgroundLiveness(True, frozenset()))
        os.chmod(self.root, 0o700)
        (self.root / "link").symlink_to(self.root)
        self.assertEqual(sessions.background_liveness(self.root).known, False)

    def test_ancestor_walk_is_bounded(self) -> None:
        proc = self.root / "proc"
        for pid, parent in ((5, 4), (4, 5)):
            (proc / str(pid)).mkdir(parents=True)
            (proc / str(pid) / "stat").write_bytes(f"{pid} (a) b) S {parent} 0 0".encode())
        self.assertEqual(sessions.ancestor_pids(proc, 5), frozenset({4, 5}))
        self.assertEqual(linux_process.ancestor_pids(proc, 99), frozenset({99}))


if __name__ == "__main__":
    unittest.main()


class CurrentNativeMetadataTests(_Private):
    def test_current_claude_config_dir_metadata_only(self):
        from types import SimpleNamespace
        from claude_multi import paths
        from claude_multi.cli import resume_checks, session_facts
        env = {'HOME': str(self.root), 'CLAUDE_CONFIG_DIR': str(self.root / 'native')}
        rid = '11111111-1111-4111-8111-111111111111'
        record = {'session_id': rid, 'runtime_session_id': rid, 'cwd': '/fixture'}
        path = paths.native_projects(self.root, env) / session_facts._native_project_slug('/fixture') / (rid + '.jsonl')
        path.parent.mkdir(parents=True)
        path.touch()
        with mock.patch('builtins.open', side_effect=AssertionError('metadata only')):
            self.assertEqual(resume_checks._resume_transcript_status(SimpleNamespace(environ=env), record),
                             ('present', str(path), True))

    def test_historical_native_root_remains_unknown(self):
        from types import SimpleNamespace
        from claude_multi.cli import resume_checks
        rid = '11111111-1111-4111-8111-111111111111'
        record = {'session_id': rid, 'runtime_session_id': rid, 'cwd': '/fixture'}
        runtime = SimpleNamespace(environ={'HOME': str(self.root), 'CLAUDE_CONFIG_DIR': str(self.root / 'new-native')})
        self.assertEqual(resume_checks._resume_transcript_status(runtime, record)[0], 'unknown')
        self.assertFalse((self.root / 'new-native').exists())


class DurabilityDispositionTests(_Private):
    def test_directory_sync_failure_semantics_per_operation(self):
        from claude_multi import dev, scope, upgrade
        for name, write in [('state', state.atomic_write), ('upgrade', upgrade._write_repo_file),
                            ('dev', dev._repo_atomic_write)]:
            path = self.root / name
            state.atomic_write(path, b'before')
            real_sync = os.fsync
            def sync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError(errno.EIO, 'fixture directory sync failed')
                return real_sync(fd)
            with self.subTest(operation=name), mock.patch('os.fsync', side_effect=sync):
                with self.assertRaises(OSError):
                    write(path, b'after')
            self.assertEqual(path.read_bytes(), b'after', name)
        with mock.patch('os.fsync', side_effect=OSError(errno.EIO, 'fixture')):
            with self.assertRaises(OSError):
                scope._fsync_directory(self.root)
            with self.assertRaises(OSError):
                posix_fs.fsync_directory(self.root)
