"""The macOS process backend through its seams: lsof, ps and the connect probe.

No test runs a real lsof or ps or reads the host process table: every call
goes through a stub runner, and the connect probe only touches a
test-allocated loopback port. The journeys run forget, stop and mark-ended
with the macOS backend selected.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _catalog import FIXTURE_ROOT
from claude_multi import catalog, cli, endpoint, gateway_lifecycle as gl, migrate, service, sessions
from claude_multi.platform import darwin_process, observation
import claude_multi.cli.session_actions as session_actions
from test_gateway_lifecycle import World, _Lifecycle
from test_migrate import LCAT, M1, RUNTIME, SCHEMA, _cli_environ, _write_raw, v3_managed

UID = os.geteuid()


def completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout.encode(), b"")


class Runner:
    """A stub for the backend's tools: argv[0] basename -> result (or exception)."""

    def __init__(self, **results) -> None:
        self.results = results
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert kwargs["timeout"] <= darwin_process.TIMEOUT
        result = self.results[Path(argv[0]).name]
        if isinstance(result, BaseException):
            raise result
        return result(argv) if callable(result) else result


class ListenerTests(unittest.TestCase):
    BASE = "http://127.0.0.1:18399"

    def owner(self, runner, *, pid=4242, connect=lambda _port: False):
        return darwin_process.listener_owner(self.BASE, pid=pid, euid=UID, runner=runner,
                                             connect=connect, lsof="/usr/sbin/lsof")

    def test_lsof_argv_and_field_parsing(self) -> None:
        runner = Runner(lsof=completed(f"p4242\nu{UID}\nf7\n"))
        self.assertEqual(self.owner(runner).kind, "ours")
        self.assertEqual(runner.calls, [["/usr/sbin/lsof", "-nP", "-iTCP:18399", "-sTCP:LISTEN", "-Fpu"]])
        self.assertEqual(darwin_process.parse_lsof("p1\nu5\nf3\np2\nu6\n"), [(1, 5), (2, 6)])

    def test_verdicts(self) -> None:
        cases = {
            "foreign": completed(f"p77\nu{UID + 1}\n"),
            "unknown": completed(f"p77\nu{UID}\n"),          # another process of ours
        }
        for kind, result in cases.items():
            with self.subTest(kind=kind):
                verdict = self.owner(Runner(lsof=result))
                self.assertEqual(verdict.kind, kind)
        foreign = self.owner(Runner(lsof=completed(f"p4242\nu{UID}\np77\nu{UID + 1}\n")))
        self.assertEqual((foreign.kind, foreign.uid), ("foreign", UID + 1))  # a foreign row always wins
        self.assertEqual(self.owner(Runner(lsof=completed(f"p4242\nu{UID}\n")), pid=None).kind, "unknown")

    def test_nothing_visible_asks_the_connect_probe(self) -> None:
        empty = Runner(lsof=completed("", 1))  # lsof exits 1 when nothing matches
        self.assertEqual(self.owner(empty, connect=lambda _port: False).kind, "none")
        invisible = self.owner(empty, connect=lambda _port: True)
        self.assertEqual(invisible.kind, "unknown")  # another user's socket lsof cannot see
        self.assertIn("lsof cannot see", invisible.detail)
        self.assertEqual(self.owner(empty, connect=lambda _port: None).kind, "unknown")

    def test_an_unavailable_lsof_is_unknown_never_none(self) -> None:
        for result in (FileNotFoundError("lsof"), subprocess.TimeoutExpired("lsof", 5), completed("", 2),
                       completed("p1\n" * (darwin_process.OUTPUT_LIMIT // 3 + 1))):
            with self.subTest(result=type(result).__name__):
                self.assertEqual(self.owner(Runner(lsof=result), connect=lambda _port: False).kind, "unknown")
        with mock.patch.object(darwin_process, "_tool", return_value=None):
            verdict = darwin_process.listener_owner(self.BASE, pid=1, runner=Runner(), connect=lambda _p: False)
        self.assertEqual(verdict.kind, "unknown")

    def test_the_connect_probe_on_a_test_port(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            self.assertTrue(darwin_process.connect_probe(port))
        finally:
            server.close()
        self.assertFalse(darwin_process.connect_probe(port))


class GatewayPidTests(unittest.TestCase):
    def stamp(self, binary="/opt/claude-multi/bin/cli-proxy-api"):
        return service.ExecStamp("1.0.0", "sig", 4242, binary, "2026-10-02T12:00:00+00:00", None, "a" * 16)

    def test_identity(self) -> None:
        runner = Runner(ps=completed("/opt/claude-multi/bin/cli-proxy-api\n"))
        self.assertEqual(darwin_process.gateway_pid(self.stamp(), exists=lambda _p: True, runner=runner,
                                                    ps="/bin/ps"), (4242, True))
        self.assertEqual(runner.calls, [["/bin/ps", "-p", "4242", "-o", "comm="]])
        self.assertEqual(darwin_process.gateway_pid(self.stamp(), exists=lambda _p: False, runner=Runner(),
                                                    ps="/bin/ps"), (4242, False))
        reused = Runner(ps=completed("/usr/bin/python3\n"))
        self.assertEqual(darwin_process.gateway_pid(self.stamp(), exists=lambda _p: True, runner=reused,
                                                    ps="/bin/ps"), (None, None))
        self.assertEqual(darwin_process.gateway_pid(self.stamp(), exists=lambda _p: None, runner=Runner(),
                                                    ps="/bin/ps"), (None, None))
        failing = Runner(ps=completed("", 1))
        self.assertEqual(darwin_process.gateway_pid(self.stamp(), exists=lambda _p: True, runner=failing,
                                                    ps="/bin/ps"), (None, None))
        self.assertEqual(darwin_process.gateway_pid(None), (None, None))

    def test_a_symlinked_install_path_is_the_same_program(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "cli-proxy-api"
            real.write_text("")
            link = Path(tmp) / "current"
            link.symlink_to(real)
            runner = Runner(ps=completed(f"{real}\n"))
            self.assertEqual(darwin_process.gateway_pid(self.stamp(str(link)), exists=lambda _p: True,
                                                        runner=runner, ps="/bin/ps"), (4242, True))


class SessionScanTests(unittest.TestCase):
    TABLE = (f"  101 /Users/u/.local/bin/claude --session-id {RUNTIME} --model x\n"
             f"  102 claude --resume={M1}\n"
             "  103 /bin/zsh -l\n"
             "  104 claude --resume not-a-uuid\n")

    def scan(self, runner, **kwargs):
        return darwin_process.session_scan(accept=sessions.UUID4.fullmatch, runner=runner, ps="/bin/ps", **kwargs)

    def test_ids_from_one_bounded_table(self) -> None:
        runner = Runner(ps=completed(self.TABLE))
        result = self.scan(runner)
        self.assertEqual(result, observation.ProcessScan(True, frozenset({RUNTIME, M1})))
        self.assertEqual(runner.calls, [["/bin/ps", "-axww", "-o", "pid=,args="]])
        self.assertEqual(self.scan(Runner(ps=completed(self.TABLE)), exclude_pids=(101,)).ids, frozenset({M1}))

    def test_an_unreadable_table_is_unknown(self) -> None:
        for result, reason in ((completed("", 1), "ps exit 1"), (FileNotFoundError("ps"), "ps did not answer"),
                               (completed("1 x\n" * (darwin_process.OUTPUT_LIMIT // 3)), "exceeds the read bound")):
            with self.subTest(reason=reason):
                scan = self.scan(Runner(ps=result))
                self.assertFalse(scan.known)
                self.assertIn(reason, scan.reason)

    def test_ancestors_from_one_parent_table(self) -> None:
        runner = Runner(ps=completed("   1     0\n  50     1\n  60    50\n  70    60\n  99     1\n"))
        self.assertEqual(darwin_process.ancestor_pids(70, runner=runner, ps="/bin/ps"), frozenset({70, 60, 50, 1}))
        self.assertEqual(darwin_process.ancestor_pids(7, runner=Runner(ps=completed("", 1)), ps="/bin/ps"),
                         frozenset({7}))

    def test_the_daemon_root_reader_is_the_posix_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cc-daemon"
            (root / "0123abcd" / "pty").mkdir(parents=True, mode=0o700)
            (root / "0123abcd" / "pty" / "b1111111.sock").write_text("")
            os.chmod(root, 0o700)
            verdict = darwin_process.background_liveness(root)
        self.assertEqual(verdict, observation.BackgroundLiveness(True, frozenset({"b1111111"})))


class DarwinGatewayTests(_Lifecycle):
    """The lifecycle on macOS: ownership through lsof, ps and kill(0)."""

    def macos_seams(self, lsof: str, *, exists=True, comm="/fixture/gateway") -> gl.Seams:
        runner = Runner(lsof=completed(lsof, 0 if lsof else 1), ps=completed(comm + "\n"))
        return self.world.seams(listener=None, process=None, runner=runner)

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(darwin_process, "_tool", side_effect=lambda name: f"/usr/sbin/{name}")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(darwin_process.posix_process, "exists", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ownership_is_proven_through_lsof_and_ps(self) -> None:
        self.world.write_stamp("a" * 16)
        self.world.lock = True
        gateway = self.gateway(platform="darwin", seams=self.macos_seams(f"p{self.world.pid}\nu{UID}\n"))
        self.assertEqual(gateway.observe().state, gl.OURS)
        gateway = self.gateway(platform="darwin", seams=self.macos_seams(f"p{self.world.pid}\nu{UID}\n",
                                                                         comm="/usr/bin/python3"))
        self.assertEqual(gateway.observe().state, gl.UNKNOWN)  # the PID runs another program

    def test_an_invisible_listener_is_refused_before_any_start_or_token(self) -> None:
        with mock.patch.object(darwin_process, "connect_probe", return_value=True):
            gateway = self.gateway(platform="darwin", seams=self.macos_seams(""))
            outcome = gateway.ensure(max_wait=1)
        self.assertEqual(outcome.status, "refused")
        self.assertIn("lsof cannot see", outcome.message)
        self.assertEqual(self.world.spawned, [])

    def test_a_free_lock_and_no_listener_is_stopped_and_starts(self) -> None:
        with mock.patch.object(darwin_process, "connect_probe", return_value=False):
            gateway = self.gateway(platform="darwin", seams=self.macos_seams(""))
            self.assertEqual(gateway.observe().state, gl.STOPPED)


class MacOSSessionJourneys(unittest.TestCase):
    """forget, stop and mark-ended with the macOS backend selected."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-darwin-journeys-"))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.environ = _cli_environ(self.tmp)
        self.store = sessions.SessionStore(sessions.state_root(self.environ), SCHEMA)
        hook = "/fixture/bin/claude-multi"
        from claude_multi import scope

        shim = scope.ensure_hook_shim(self.store.root, hook)
        _write_raw(self.store, v3_managed(M1, last_event_source="resume"))
        self.assertTrue(migrate.run(self.store, LCAT, hook_command=hook, hook_shim_path=shim,
                                    now="2026-09-25T00:00:00Z").ok)
        self.daemon_root = self.tmp / "cc-daemon"
        self.daemon_root.mkdir(mode=0o700)
        self.table = ""
        self.ps_result: BaseException | None = None
        for target, value in ((sessions, "_platform"), (darwin_process, "_tool")):
            patcher = mock.patch.object(target, value, return_value="darwin" if target is sessions else "/bin/ps")
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(darwin_process, "_run", side_effect=self._ps)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ps(self, _runner, argv):
        self.assertEqual(argv[:2], ["/bin/ps", "-axww"])
        if self.ps_result is not None:
            return None  # what the runner wrapper returns for a tool that did not answer
        return completed(self.table)

    def runtime(self) -> cli.Runtime:
        return cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.environ, cwd=self.tmp,
                           background_liveness=lambda: darwin_process.background_liveness(self.daemon_root))

    def run_cli(self, argv):
        """``(exit, stdout then stderr)``."""

        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = cli.main(argv, runtime=self.runtime(), output_stream=output, interactive=False)
        return code, output.getvalue() + errors.getvalue()

    def live_in_daemon(self) -> None:
        pty = self.daemon_root / "0123abcd" / "pty"
        pty.mkdir(parents=True, mode=0o700)
        (pty / f"{M1[:8]}.sock").write_text("")

    def test_mark_ended_reads_the_ps_table(self) -> None:
        self.table = f"  555 /Users/u/.local/share/claude/2.1/claude --resume {M1}\n"
        code, output = self.run_cli(["sessions", "mark-ended", M1])
        self.assertEqual(code, 1, output)
        self.assertIn("refused: session", output)
        self.ps_result = FileNotFoundError("ps")
        code, output = self.run_cli(["sessions", "mark-ended", M1])
        self.assertEqual(code, 1, output)
        self.assertIn("the process table is unreadable: ps did not answer", output)
        self.ps_result, self.table = None, "  1 /sbin/launchd\n"
        code, output = self.run_cli(["sessions", "mark-ended", M1])
        self.assertEqual((code, output), (0, f"{M1}  marked ended\n"))
        self.assertEqual(self.store.load(M1)["last_event_source"], "end")

    def test_forget_and_stop_read_the_daemon_root(self) -> None:
        self.live_in_daemon()
        record = self.store.load(M1)
        self.assertIsNone(session_actions._stop_precheck(self.runtime(), record))  # live: stop may proceed
        code, output = self.run_cli(["sessions", "forget", M1, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("is live in the background", output)
        shutil.rmtree(self.daemon_root / "0123abcd")
        self.assertIn("not live in the background", session_actions._stop_precheck(self.runtime(), record))
        code, output = self.run_cli(["sessions", "forget", M1, "--yes"])
        self.assertEqual(code, 0, output)
        self.assertFalse((self.store.sessions_dir / f"{M1}.json").exists())

    def test_an_unknown_daemon_root_refuses_the_destructive_verbs(self) -> None:
        os.chmod(self.daemon_root, 0o755)
        os.symlink(self.tmp, self.daemon_root / "link")
        record = self.store.load(M1)
        self.assertIn("cannot tell whether", session_actions._stop_precheck(self.runtime(), record))
        code, output = self.run_cli(["sessions", "forget", M1])
        self.assertNotEqual(code, 0)
        self.assertTrue((self.store.sessions_dir / f"{M1}.json").exists())


if __name__ == "__main__":
    unittest.main()
