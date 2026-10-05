"""Service/path boundary: fixture observations only, no live manager."""

from __future__ import annotations

import datetime
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, paths, proxy, service
from _catalog import FIXTURE_ROOT


class ServiceTests(unittest.TestCase):
    def test_unit_matches_catalog(self) -> None:
        self.assertEqual(service.UNIT, catalog.load_catalog(FIXTURE_ROOT).docs["gateway"]["gateway"]["service"])

    def test_hints_and_journal(self) -> None:
        # Control verbs name the guarded product verbs on every backend.
        systemd = service.Backend(service.SYSTEMD, "fixture")
        for verb in ("start", "stop", "restart", "status", "logs"):
            self.assertEqual(service.hint(verb), f"claude-multi gateway {verb}")
            self.assertEqual(service.hint(verb, backend=systemd), f"claude-multi gateway {verb}")
            self.assertEqual(service.hint_code(verb), f"`{service.hint(verb)}`")
        # The on-demand backend never names the service manager.
        self.assertEqual(service.hint("why"), "claude-multi gateway logs")
        self.assertEqual(service.hint("recover"), "claude-multi gateway start")
        self.assertEqual(service.hint("reset-failed"), "claude-multi gateway start")
        self.assertEqual(service.hint("reload"), "claude-multi-proxy init")
        # Manager text only where the unit exists.
        self.assertEqual(service.hint("recover", backend=systemd),
                         "systemctl --user reset-failed fixture && claude-multi gateway start")
        self.assertEqual(service.hint("why", backend=systemd),
                         "systemctl --user show -p ActiveState,Result,ExecMainStatus fixture")
        self.assertEqual(service.hint("reload", backend=systemd), "systemctl --user reload fixture")
        with self.assertRaises(ValueError):
            service.hint("enable")
        self.assertEqual(service.journal_argv(),
                         ("journalctl", "--user", "-u", "cli-proxy-api", "--since", "-24h", "-o", "cat"))
        self.assertIn("fixture", service.journal_argv(unit="fixture", since="-1h"))
        title, body = service.failure_notice()
        self.assertEqual(title, "claude-multi gateway failed")
        self.assertIn("systemctl --user show -p ActiveState,Result,ExecMainStatus cli-proxy-api", body)
        self.assertIn("systemctl --user reset-failed cli-proxy-api && systemctl --user start cli-proxy-api", body)

    def test_the_backend_comes_from_the_endpoint_document(self) -> None:
        from claude_multi import endpoint

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(service.backend_of(home), service.Backend())
            self.assertEqual(service.hint("why", home=home), "claude-multi gateway logs")
            endpoint.write_config(home, endpoint.EndpointConfig(port=18317, backend=endpoint.SYSTEMD))
            with mock.patch.object(endpoint.sys, "platform", "linux"):
                self.assertEqual(service.backend_of(home), service.Backend(service.SYSTEMD, endpoint.DEFAULT_UNIT))
                self.assertIn(endpoint.DEFAULT_UNIT, service.hint("why", home=home))
            with mock.patch.object(endpoint.sys, "platform", "darwin"):  # unsupported: text only
                self.assertEqual(service.backend_of(home), service.Backend())
            (home / ".config/claude-multi/endpoint.json").write_text("{broken")
            self.assertEqual(service.hint("why", home=home), "claude-multi gateway logs")

    def test_service_probe_fails_closed(self) -> None:
        # Migrated from test_gateway_auth_tools: the manager belongs here.
        for name, running in (("active", True), ("activating", True), ("deactivating", True),
                              ("reloading", True), ("inactive", False), ("failed", False)):
            with self.subTest(state=name):
                runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout=f"{name}\n"))
                self.assertIs(service.service_active(runner=runner), running)
                self.assertEqual(runner.call_args.args[0],
                                 ["systemctl", "--user", "show", "--property=ActiveState", "--value", service.UNIT])
        for result in (mock.Mock(returncode=1, stdout=""), mock.Mock(returncode=0, stdout="")):
            runner = mock.Mock(return_value=result)
            self.assertIsNone(service.manager_state(runner=runner))
            with self.assertRaisesRegex(service.ServiceError, "cannot tell"):
                service.service_active(runner=runner)
        for error in (OSError(), subprocess.TimeoutExpired("fixture", 10)):
            with self.assertRaises(service.ServiceError):
                service.service_active(runner=mock.Mock(side_effect=error))

    def test_workdir_aliases_are_identical_and_dotenv_is_never_read(self) -> None:
        for name in ("gateway_workdir", "ensure_gateway_workdir", "gateway_dotenv_present"):
            self.assertIs(getattr(proxy, name), getattr(service, name))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = service.ensure_gateway_workdir(root)
            self.assertEqual(workdir, root / "gateway")
            self.assertEqual(workdir.stat().st_mode & 0o777, 0o700)
            self.assertTrue((workdir / "logs").is_dir())
            self.assertFalse(service.gateway_dotenv_present(workdir))
            (workdir / ".env").symlink_to(root / "absent")
            self.assertTrue(service.gateway_dotenv_present(workdir))

    def test_status_matrix_uses_health_and_pid_not_manager_alone(self) -> None:
        for health, alive, manager, expected in (
            (200, False, "inactive", "running"),
            (ConnectionRefusedError(), True, "failed", "running"),
            (ConnectionRefusedError(), False, "inactive", "stopped"),
            (ConnectionRefusedError(), False, "failed", "stopped"),
            (ConnectionRefusedError(), False, None, "stopped"),
            (ConnectionRefusedError(), False, "activating", "unknown"),
            (ConnectionRefusedError(), None, "inactive", "unknown"),
            (TimeoutError(), False, "inactive", "unknown"),
            (503, False, "inactive", "unknown"),
        ):
            with self.subTest(health=health, alive=alive, manager=manager):
                getter = mock.Mock(side_effect=health) if isinstance(health, Exception) else mock.Mock(return_value=health)
                result = service.gateway_status(
                    base_url="http://127.0.0.1:9", health_path="/healthz", state_root=Path("/fixture"),
                    health_get=getter, pid_get=lambda **_: (42 if alive else None, alive),
                    manager=lambda: manager,
                )
                self.assertEqual(result.state, expected)
                self.assertEqual(result.manager_state, manager)

    def test_pid_candidate_is_checked_in_fixture_proc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout="42"))
            kwargs = dict(state_root=root, proc_root=root / "proc", runner=runner)
            self.assertEqual(service.gateway_pid(**kwargs), (42, None))
            (root / "proc" / "42").mkdir(parents=True)
            self.assertEqual(service.gateway_pid(**kwargs), (42, True))
            runner.return_value.stdout = "0"
            self.assertEqual(service.gateway_pid(**kwargs), (None, False))
            runner.return_value.stdout = "malformed"
            self.assertEqual(service.gateway_pid(**kwargs), (None, None))
            # MainPID=0 without an observable /proc is not a first start.
            runner.return_value.stdout = "0"
            self.assertEqual(service.gateway_pid(**{**kwargs, "proc_root": root / "no-proc"}), (None, None))


class PathTests(unittest.TestCase):
    def test_home_display_and_xdg_policy(self) -> None:
        env = {"HOME": "/fixture/home", "XDG_CONFIG_HOME": "/fixture/config"}
        self.assertEqual(paths.home(env), Path("/fixture/home"))
        self.assertEqual(paths.display("/fixture/home/a", env), "~/a")
        self.assertEqual(paths.display("/fixture/home", env), "~")
        self.assertEqual(paths.display("/fixture/home-other/a", env), "/fixture/home-other/a")
        self.assertEqual(paths.config_root(env), Path("/fixture/config/claude-multi"))
        self.assertEqual(paths.gateway_config_dir(env), Path("/fixture/home/.config/claude-multi"))
        self.assertEqual(paths.secret_env_default(env),
                         Path("/fixture/home/.config/claude-multi/secrets/provider-keys.env"))


class ListenerOwnerTests(unittest.TestCase):
    """No real proc or manager observation; all socket/PID facts are fixtures."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)
        (self.proc / "net").mkdir()
        self.tables()

    def tables(self, tcp="", tcp6=""):
        for name, rows in (("tcp", tcp), ("tcp6", tcp6)):
            (self.proc / "net" / name).write_text("sl local_address rem_address st ...\n" + rows)

    def row(self, uid=1000, address="0100007F", state="0A", port="208D", inode="42"):
        return f" 0: {address}:{port} 00000000:0000 {state} 0:0 00:0 0 {uid} 0 {inode}\n"

    def owner(self, **kwargs):
        return service.listener_owner("http://127.0.0.1:8333", proc_root=self.proc,
                                      euid=1000, stamp={"pid": 123}, **kwargs)

    def test_none_foreign_own_unconfirmed_and_ours(self):
        self.assertEqual(self.owner().kind, "none")
        self.tables(self.row(uid=999))
        self.assertEqual(self.owner().kind, "foreign")
        self.assertEqual(self.owner().uid, 999)
        self.tables(self.row())
        self.assertEqual(self.owner().kind, "unknown")
        fd = self.proc / "123/fd/8"
        fd.parent.mkdir(parents=True)
        fd.symlink_to("socket:[42]")
        self.assertEqual(self.owner().kind, "ours")
        self.tables(self.row() + self.row(uid=999))
        self.assertEqual(self.owner().kind, "foreign")

    def test_wildcard_ipv6_loopback_and_nonlisteners(self):
        for address in ("00000000", "0100007F"):
            self.tables(self.row(uid=999, address=address))
            self.assertEqual(self.owner().kind, "foreign")
        self.tables(self.row(uid=999, state="01") + self.row(uid=999, port="0009") + self.row(uid=999, address="0200007F"))
        self.assertEqual(self.owner().kind, "none")
        self.tables(tcp6=self.row(uid=999, address="0" * 32))
        self.assertEqual(self.owner().kind, "foreign")
        self.tables(tcp6=self.row(uid=999, address="00000000000000000000000001000000"))
        self.assertEqual(service.listener_owner("http://[::1]:8333", proc_root=self.proc,
                                               euid=1000, stamp={}).kind, "foreign")
        self.assertEqual(self.owner().kind, "none")

    def test_unreadable_unknown_and_foreign_still_refuses(self):
        (self.proc / "net/tcp6").unlink()
        self.assertEqual(self.owner().kind, "unknown")
        (self.proc / "net/tcp").write_text("header\n" + self.row(uid=999))
        self.assertEqual(self.owner().kind, "foreign")
        with mock.patch.object(service.sys, "platform", "darwin"):  # lsof answers there; none here
            self.assertEqual(self.owner(runner=mock.Mock(side_effect=FileNotFoundError("lsof"))).kind, "unknown")

    def test_unrelated_fd_disappearing_does_not_invalidate_socket_proof(self):
        self.tables(self.row())
        directory = self.proc / "123/fd"
        directory.mkdir(parents=True)
        (directory / "8").symlink_to("socket:[42]")
        (directory / "9").symlink_to("/fixture/unrelated")
        readlink = service.os.readlink

        def racing_readlink(fd):
            if fd.name == "9":
                raise FileNotFoundError("closed concurrently")
            return readlink(fd)

        with mock.patch.object(service.os, "readlink", side_effect=racing_readlink):
            self.assertEqual(self.owner().kind, "ours")
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError):
            self.assertEqual(self.owner().kind, "unknown")

    def test_mainpid_comes_only_from_injected_manager(self):
        self.tables(self.row())
        fd = self.proc / "123/fd/1"
        fd.parent.mkdir(parents=True)
        fd.symlink_to("socket:[42]")
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "123\n", ""))
        self.assertEqual(service.listener_owner("http://127.0.0.1:8333", proc_root=self.proc,
                                               euid=1000, runner=runner).kind, "ours")
        self.assertIn("--property=MainPID", runner.call_args.args[0])


class GatewayStampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = service.ensure_gateway_workdir(Path(self.tmp.name))
        self.old = "2026-09-27T01:00:00+00:00"
        self.now = "2026-09-27T02:00:00+00:00"
        self.new = "2026-09-27T03:00:00+00:00"

    def pending(self, **kwargs):
        return service.pending_restart_lines(self.workdir, signature="s", launcher_version="3.0.2-dev",
                                             reload_failed=lambda: False, **kwargs)

    def start(self, at=None, signature="s", pid_namespace="pid:[101]"):
        service.write_exec_stamp(self.workdir, service.ExecStamp(
            "3.0.2-dev", signature, 42, "/fixture/gw", at or self.now, pid_namespace))

    @staticmethod
    def reload_record(start="02:00:00", stop="02:00:01", code="exited", status=203):
        return ("{ path=/fixture/proxy ; argv[]=/fixture/proxy init --reload-check ; ignore_errors=no ; "
                f"start_time=[Sun 2026-09-27 {start} UTC] ; stop_time=[Sun 2026-09-27 {stop} UTC] ; "
                f"pid=42 ; code={code} ; status={status} }}")

    def manager_result(self, text):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, text, ""))
        result = service.last_reload_result(runner=runner)
        self.assertIn("--property=ExecReload", runner.call_args.args[0])
        self.assertIn("--timestamp=us+utc", runner.call_args.args[0])
        self.assertEqual(runner.call_args.kwargs["env"]["TZ"], "UTC")
        self.assertEqual(runner.call_args.kwargs["env"]["LC_ALL"], "C")
        return result

    def test_preexec_failure_newer_than_success_is_pending_and_new_success_clears(self):
        self.start(at=self.old)
        service.write_reload_attempt(self.workdir, self.old, "v")
        service.write_reload_stamp(self.workdir, "reloaded", self.old, "v")
        result = self.manager_result(self.reload_record())
        self.assertEqual(result.started_at, datetime.datetime.fromisoformat(self.now))
        def pending():
            return service.pending_restart_lines(self.workdir, signature="s", launcher_version="v",
                                                 reload_failed=lambda: result)
        self.assertIn("ExecReload failed", pending()[0])
        # No failed-attempt writer ran; only the manager knows about 203/EXEC.
        self.assertEqual(service.read_reload_stamp(self.workdir)["status"], "reloaded")
        service.write_reload_stamp(self.workdir, "reloaded", self.new, "v")
        self.assertEqual(pending(), [])
        # Production, non-injected path uses the dated observation of the
        # recorded unit (the default or a custom name), and asks no manager
        # on demand.
        for unit in ("claude-multi-gateway", "my-gateway"):
            with self.subTest(unit=unit), \
                    mock.patch.object(service, "last_reload_result", return_value=result) as reader:
                self.assertEqual(service.pending_restart_lines(
                    self.workdir, signature="s", launcher_version="v",
                    backend=service.Backend(service.SYSTEMD, unit)), [])
                reader.assert_called_once_with(unit=unit)
        service.write_reload_stamp(self.workdir, "reloaded", self.old, "v")
        for backend in (None, service.Backend()):
            with self.subTest(backend=backend), \
                    mock.patch.object(service, "last_reload_result", side_effect=AssertionError("manager")):
                self.assertEqual(service.pending_restart_lines(
                    self.workdir, signature="s", launcher_version="v", backend=backend), [])

    def test_latest_manager_record_success_and_restart_supersede_failure(self):
        failed = self.reload_record(start="01:00:00", stop="01:00:01")
        succeeded = self.reload_record(status=0)
        self.assertFalse(self.manager_result(failed + " ; " + succeeded).failed)
        self.assertFalse(self.manager_result(succeeded + " ; " + failed).failed)
        self.start(at=self.new)
        service.write_reload_stamp(self.workdir, "reloaded", self.new, "v")
        self.assertEqual(service.pending_restart_lines(
            self.workdir, signature="s", launcher_version="v",
            reload_failed=lambda: self.manager_result(failed)), [])

    def test_unavailable_or_malformed_manager_record_keeps_stamp_behavior(self):
        self.start(at=self.old)
        service.write_reload_stamp(self.workdir, "reloaded", self.old, "v")
        records = ("", "garbage", "{ code=exited ; status=203 }",
                   self.reload_record(start="n/a"), self.reload_record(stop="01:00:00"),
                   self.reload_record(code="(null)"), self.reload_record(status="bad"),
                   self.reload_record().replace(" UTC", " BROKEN"),
                   self.reload_record()[:-1], self.reload_record(status="9" * 5000))
        for raw in records:
            with self.subTest(raw=raw[:160]):
                result = self.manager_result(raw)
                self.assertIsNone(result)
                self.assertEqual(service.pending_restart_lines(
                    self.workdir, signature="s", launcher_version="v", reload_failed=lambda: result), [])
        for runner in (mock.Mock(side_effect=FileNotFoundError),
                       mock.Mock(side_effect=subprocess.TimeoutExpired("fixture", 10)),
                       mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))):
            self.assertIsNone(service.last_reload_result(runner=runner))
        service.write_reload_attempt(self.workdir, self.new, "v")
        self.assertEqual(len(service.pending_restart_lines(
            self.workdir, signature="s", launcher_version="v", reload_failed=lambda: None)), 1)

    def test_manager_timestamp_subseconds_and_signal_failure(self):
        for code, status in (("killed", "9/KILL"), ("killed", "15/TERM"), ("dumped", "6/ABRT")):
            with self.subTest(code=code, status=status):
                result = self.manager_result(self.reload_record(
                    start="02:00:00.123456", stop="02:00:00.234567", code=code, status=status))
                self.assertIsNotNone(result)
                self.assertTrue(result.failed)
                self.assertEqual(result.started_at.microsecond, 123456)
                service.write_reload_stamp(self.workdir, "reloaded", "2026-09-27T02:00:00.100000+00:00", "v")
                def pending():
                    return service.pending_restart_lines(self.workdir, signature="s", launcher_version="v",
                                                         reload_failed=lambda: result)
                self.assertEqual(len(pending()), 1)
                self.assertIn("ExecReload failed", pending()[0])
                service.write_reload_stamp(self.workdir, "reloaded", "2026-09-27T02:00:00.300000+00:00", "v")
                self.assertEqual(pending(), [])

    def test_stamps_private_and_invalid_ignored(self):
        self.assertEqual(self.pending(), [])
        self.start()
        self.assertEqual(service.read_exec_stamp(self.workdir).pid, 42)
        self.assertEqual((self.workdir / service.EXEC_STAMP).stat().st_mode & 0o777, 0o600)
        (self.workdir / service.EXEC_STAMP).write_text('{"pid": "bad"}')
        self.assertIsNone(service.read_exec_stamp(self.workdir))
        service.write_reload_stamp(self.workdir, "reloaded", self.now, "v")
        self.assertEqual(service.read_reload_stamp(self.workdir)["status"], "reloaded")

    def test_pending_and_restart_clear(self):
        self.start()
        service.write_reload_stamp(self.workdir, "restart_required", self.old, "v")
        self.assertEqual(self.pending(), [])
        service.write_reload_attempt(self.workdir, self.new, "v")
        self.assertEqual(len(self.pending()), 1)
        service.write_reload_stamp(self.workdir, "error", self.new, "v")
        self.assertIn("error", self.pending()[0])
        service.write_reload_stamp(self.workdir, "reloaded", self.new, "v")
        self.assertEqual(self.pending(), [])
        service.write_reload_attempt(self.workdir, "2026-09-27T03:00:00.000001+00:00", "v")
        self.assertEqual(len(self.pending()), 1)
        self.start(at="2026-09-27T04:00:00+00:00", signature="different")
        self.assertEqual(len(self.pending()), 1)
        self.assertIn("start arguments or environment filter", self.pending()[0])

    def test_exec_reload_real_format_and_stamp_failure_fallback(self):
        for text, failed in (
            ("", False),
            ("{ path=/fixture/proxy ; argv[]=/fixture/proxy init ; ignore_errors=no ; start_time=[Sun 2026-09-27 01:00:00 UTC] ; stop_time=[Sun 2026-09-27 01:00:01 UTC] ; pid=42 ; code=exited ; status=1 }", True),
            (self.reload_record(status=0), False),
            (self.reload_record(code="killed", status="9/KILL"), True),
            (self.reload_record(code="dumped", status="6/ABRT"), True),
            ("{ code=(null) ; status=0 }", False),
        ):
            with self.subTest(text=text):
                runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout=text))
                result = service.last_reload_failed(runner=runner)
                self.assertIs(result, failed)
                self.assertIn("--property=ExecReload", runner.call_args.args[0])
                lines = service.pending_restart_lines(self.workdir, signature="s", launcher_version="v",
                                                      reload_failed=lambda: result)
                self.assertEqual(bool(lines), failed)

    def test_pid_uses_stamp_without_manager(self):
        binary = Path(self.tmp.name) / "gateway-binary"
        binary.write_text("fixture")
        service.write_exec_stamp(self.workdir, service.ExecStamp("v", "s", 42, str(binary), self.now))
        proc = Path(self.tmp.name) / "proc"
        (proc / "42").mkdir(parents=True)
        (proc / "42/exe").symlink_to(binary)
        runner = mock.Mock(side_effect=AssertionError("manager read"))
        self.assertEqual(service.gateway_pid(state_root=Path(self.tmp.name), proc_root=proc, runner=runner), (42, True))
        runner.assert_not_called()

    def test_absent_stamp_pid_remains_evidence_without_manager(self):
        self.start()
        proc = Path(self.tmp.name) / "proc"
        (proc / "self/ns").mkdir(parents=True)
        (proc / "self/ns/pid").symlink_to("pid:[101]")
        runner = mock.Mock(side_effect=FileNotFoundError("no systemctl"))
        def refused(*_):
            raise ConnectionRefusedError()
        def observe():
            return service.gateway_status(
                base_url="unused", health_path="/healthz", state_root=Path(self.tmp.name),
                proc_root=proc, pid_get=lambda **kw: service.gateway_pid(**kw, runner=runner),
                health_get=refused, manager=lambda: None)
        status = observe()
        self.assertEqual((status.state, status.pid), ("stopped", 42))
        # A manager-proven replacement still wins over the old absent PID.
        (proc / "43").mkdir()
        runner.side_effect = None
        runner.return_value = mock.Mock(returncode=0, stdout="43")
        status = observe()
        self.assertEqual((status.state, status.pid), ("running", 43))
        # A missing exe is not the same as a missing process directory.
        (proc / "42").mkdir()
        runner.side_effect = FileNotFoundError("no user bus")
        self.assertEqual(observe().state, "unknown")

    def test_invalidate_reload_stamps_keeps_attempt_and_refuses_symlinks(self):
        service.write_reload_attempt(self.workdir, self.now, "v")
        service.write_reload_stamp(self.workdir, "reloaded", self.old, "v")
        service.invalidate_reload_stamps(self.workdir)
        self.assertTrue((self.workdir / service.RELOAD_ATTEMPT).is_file())
        self.assertFalse((self.workdir / service.RELOAD_STAMP).exists())
        self.assertFalse((self.workdir / service.RELOAD_SUCCESS).exists())
        service.invalidate_reload_stamps(self.workdir)  # missing files are harmless
        target = Path(self.tmp.name) / "untouched"
        target.write_text("fixture")
        (self.workdir / service.RELOAD_STAMP).symlink_to(target)
        service.invalidate_reload_stamps(self.workdir)
        self.assertTrue((self.workdir / service.RELOAD_STAMP).is_symlink())
        self.assertEqual(target.read_text(), "fixture")
        outside = service.ensure_gateway_workdir(Path(self.tmp.name) / "other")
        service.write_reload_stamp(outside, "reloaded", self.old, "v")
        link = Path(self.tmp.name) / "unsafe-workdir"
        link.symlink_to(outside, target_is_directory=True)
        service.invalidate_reload_stamps(link)
        self.assertEqual(service.read_reload_stamp(outside)["status"], "reloaded")

    def test_stale_stamp_reused_pid_falls_back_to_manager(self):
        binary = Path(self.tmp.name) / "gateway-binary"
        binary.write_text("gateway fixture")
        # MainPID=0 needs independent absence evidence: the
        # recorded PID is in this namespace and provably runs another program.
        service.write_exec_stamp(self.workdir, service.ExecStamp("v", "s", 42, str(binary), self.old, "pid:[101]"))
        proc = Path(self.tmp.name) / "proc"
        (proc / "self/ns").mkdir(parents=True)
        (proc / "self/ns/pid").symlink_to("pid:[101]")
        (proc / "42").mkdir(parents=True)
        unrelated = Path(self.tmp.name) / "unrelated"
        unrelated.write_text("fixture")
        (proc / "42/exe").symlink_to(unrelated)
        runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout="0"))
        pid_get = lambda **kw: service.gateway_pid(**kw, runner=runner)
        def refused(*_):
            raise ConnectionRefusedError()
        status = service.gateway_status(base_url="unused", health_path="/healthz",
            state_root=Path(self.tmp.name), proc_root=proc, pid_get=pid_get,
            health_get=refused, manager=lambda: "inactive")
        self.assertEqual(status.state, "stopped")
        self.assertIsNone(status.pid)
        self.assertIn("--property=MainPID", runner.call_args.args[0])
        (proc / "43").mkdir()
        runner.return_value.stdout = "43"
        self.assertEqual(pid_get(state_root=Path(self.tmp.name), proc_root=proc), (43, True))
        runner.return_value.returncode = 1
        self.assertEqual(pid_get(state_root=Path(self.tmp.name), proc_root=proc), (None, None))
        # A stamp without a PID namespace cannot prove the reused PID is ours to judge.
        runner.return_value.returncode, runner.return_value.stdout = 0, "0"
        self.assertEqual(pid_get(state_root=Path(self.tmp.name), proc_root=proc), (None, False))
        service.write_exec_stamp(self.workdir, service.ExecStamp("v", "s", 42, str(binary), self.old))
        self.assertEqual(pid_get(state_root=Path(self.tmp.name), proc_root=proc), (None, None))

    def test_success_clears_failed_manager_hint_but_new_attempt_is_pending(self):
        self.start()
        manager = mock.Mock(return_value=True)
        def pending():
            return service.pending_restart_lines(self.workdir, signature="s", launcher_version="v",
                                                 reload_failed=manager)
        service.write_reload_attempt(self.workdir, self.now, "v")
        service.write_reload_stamp(self.workdir, "error", self.now, "v")
        self.assertEqual(len(pending()), 1)
        service.write_reload_stamp(self.workdir, "reloaded", self.new, "v")
        self.assertEqual(pending(), [])
        self.assertEqual(manager.call_count, 2)
        service.write_reload_attempt(self.workdir, "2026-09-27T04:00:00+00:00", "v")
        self.assertEqual(len(pending()), 1)
        self.assertEqual(manager.call_count, 3)
        with mock.patch.object(service.os, "access", return_value=False):
            self.assertEqual(len(pending()), 1)
        self.assertEqual(manager.call_count, 4)

    def test_unreadable_reload_stamp_uses_manager_fallback(self):
        for name in (service.RELOAD_STAMP, service.RELOAD_ATTEMPT, service.RELOAD_SUCCESS):
            with self.subTest(name=name):
                service.write_reload_attempt(self.workdir, self.old, "v")
                service.write_reload_stamp(self.workdir, "reloaded", self.new, "v")
                (self.workdir / name).write_text("invalid")
                self.assertEqual(len(service.pending_restart_lines(
                    self.workdir, signature="s", launcher_version="v", reload_failed=lambda: True)), 1)

    def test_absent_pid_in_private_namespace_or_old_stamp_is_unknown(self):
        proc = Path(self.tmp.name) / "proc"
        proc.mkdir()
        runner = mock.Mock(side_effect=FileNotFoundError("no user bus in sandbox"))
        for stamped, current, expected in (("pid:[101]", "pid:[101]", "stopped"),
                                           ("pid:[101]", "pid:[202]", "unknown"),
                                           ("pid:[101]", PermissionError(), "unknown"),
                                           (None, "pid:[101]", "unknown")):
            with self.subTest(stamped=stamped, current=current):
                self.start(pid_namespace=stamped)
                if stamped is None:
                    path = self.workdir / service.EXEC_STAMP
                    raw = json.loads(path.read_text())
                    del raw["pid_namespace"]  # an absent namespace field, not a null field
                    path.write_text(json.dumps(raw))
                reader = (mock.Mock(side_effect=current) if isinstance(current, Exception)
                          else mock.Mock(return_value=current))
                status = service.gateway_status(
                    base_url="unused", health_path="/healthz", state_root=Path(self.tmp.name),
                    proc_root=proc, health_get=mock.Mock(side_effect=ConnectionRefusedError()),
                    manager=lambda: None,
                    pid_get=lambda **kw: service.gateway_pid(**kw, runner=runner, readlink=reader))
                self.assertEqual(status.state, expected)
                reader.assert_called_once_with(proc / "self/ns/pid")
                # A nonzero manager PID absent from this /proc cannot establish
                # death either (the manager may live outside the caller's PID ns).
                manager_pid = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "42", ""))
                self.assertEqual(service.gateway_pid(state_root=Path(self.tmp.name), proc_root=proc,
                    runner=manager_pid, readlink=reader), (42, False if expected == "stopped" else None))
                # Nor can a manager MainPID of 0.
                manager_zero = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "0", ""))
                self.assertEqual(service.gateway_pid(state_root=Path(self.tmp.name), proc_root=proc,
                    runner=manager_zero, readlink=reader), (None, False if expected == "stopped" else None))

    def test_invalid_namespace_stamp_is_unavailable(self):
        for namespace in (False, 101, "", "pid:101", "mnt:[101]"):
            with self.subTest(namespace=namespace):
                self.start(pid_namespace=namespace)
                self.assertIsNone(service.read_exec_stamp(self.workdir))


class OwnerPolicyTests(unittest.TestCase):
    def test_owner_policy_truth_table(self):
        for kind, policy in (("ours", "proceed"), ("none", "proceed"),
                             ("foreign", "refuse"), ("unknown", "attention")):
            self.assertEqual(service.owner_policy(service.OwnerVerdict(kind, "fixture")), policy)
