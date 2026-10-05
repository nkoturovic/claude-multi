"""Doctor's gateway, local-file and platform facts, the TUI gateway actions
and the /cm gateway line.

Gateway observations come from the fake world of ``test_gateway_lifecycle``
(injected seams); filesystem types and WSL markers are fixture tables and
temporary ``/proc`` trees. Nothing reads the host's process table, mount
table, service manager or gateway.
"""

from __future__ import annotations

import datetime
import io
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from _v4 import V4Case
from claude_multi import cli, endpoint, gateway_hold, gateway_lifecycle as gl, lineup, quota, service, sessions, state, tui
from claude_multi.cli import gateway_facts
import claude_multi.cli.runtime as runtime_mod
import claude_multi.cli.screens.gateway_actions as gateway_actions
from claude_multi.platform import mounts
from test_gateway_lifecycle import NOW, SENTINEL, World, save_line
from test_tui import ESC, FakeWindow


class _GatewayDoctor(V4Case):
    """A V4 runtime whose gateway is the fake world (and no live host reads)."""

    def setUp(self) -> None:
        super().setUp()
        bindir = self.root / "wrapper-bin"
        bindir.mkdir()
        for name in ("claude-multi", "claude-multi-proxy"):
            (bindir / name).write_text("#!/bin/sh\nexit 1\n")
            (bindir / name).chmod(0o755)
        self.env["CLAUDE_MULTI_HOOK_COMMAND"] = str(bindir / "claude-multi")
        patcher = mock.patch.object(gl, "published_sentinel", return_value=SENTINEL)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state_root = Path(self.env["XDG_STATE_HOME"]) / "claude-multi"
        self.workdir = service.ensure_gateway_workdir(self.state_root)
        self.case = mock.Mock(root=self.root, workdir=self.workdir)
        self.world = World(self.case)
        self.runtime = self.make_runtime(gateway_seams=self.world.seams())
        patcher = mock.patch.object(runtime_mod.Runtime, "state_filesystem", return_value="ext4")
        self.fstype = patcher.start()
        self.addCleanup(patcher.stop)

    def ready(self, nonce: str) -> None:
        """A running instance of an installed gateway binary."""

        self.world.become_ready(nonce)
        binary = self.root / "cli-proxy-api"
        binary.write_text("")
        service.write_exec_stamp(self.workdir, service.ExecStamp(
            "1.0.0", "signature", self.world.pid, str(binary), NOW.isoformat(), "pid:[101]", instance=nonce))

    def facts(self) -> tuple[list[str], list[str]]:
        return gateway_facts.gateway_runtime_report(self.runtime)

    def log(self, nonce: str, lines: list[str]) -> Path:
        from claude_multi.platform import file_log

        descriptor, path = file_log.create_instance_log(self.workdir / service.GATEWAY_LOGS, NOW, nonce)
        os.write(descriptor, "".join(line + "\n" for line in lines).encode())
        os.close(descriptor)
        return path


class GatewayFactsTests(_GatewayDoctor):
    def test_on_demand_facts(self) -> None:
        self.ready("aaaaaaaaaaaaaaaa")
        self.log("aaaaaaaaaaaaaaaa", ["started"])
        attention, info = self.facts()
        self.assertEqual(attention, [])
        text = "\n".join(info)
        self.assertIn("Gateway: backend on-demand · supervision none · confinement none (on-demand)", text)
        self.assertIn("Gateway instance lock: held — instance aaaaaaaaaaaaaaaa, pid 4242 (ours:", text)
        self.assertIn("Gateway starts in the last 10 minutes: 0 of 3", text)
        self.assertIn("Gateway logs: 1 instance log(s), 8 bytes", text)
        self.assertIn("Gateway persistence hold: none (instance logs evaluated completely)", text)

    def test_crash_pause_hold_and_drift_are_attention(self) -> None:
        state.atomic_write(self.workdir / gl.START_HISTORY, (
            '{"version": 1, "starts": [%s]}' % ", ".join(
                f'"{(NOW - datetime.timedelta(minutes=m)).isoformat()}"' for m in (1, 2, 3))).encode())
        self.world.become_ready("bbbbbbbbbbbbbbbb")
        self.log("bbbbbbbbbbbbbbbb", [save_line("failed")])
        attention, _info = self.facts()
        text = "\n".join(attention)
        self.assertIn("automatic gateway starts are paused (3 starts in the last 10 minutes)", text)
        self.assertIn("gateway persistence hold: HELD — stop, restart, update and uninstall are refused", text)
        self.assertIn("gateway restart pending: the running gateway's binary is no longer installed", text)

    def test_the_supervised_backend_names_its_unit_and_confinement(self) -> None:
        endpoint.write_config(self.runtime.home, endpoint.EndpointConfig(port=18317, backend=endpoint.SYSTEMD))
        runtime = self.make_runtime(gateway_seams=self.world.seams(journal=lambda _unit, _since: gateway_hold.observation.LogWindow((), "bounded")))
        with mock.patch.object(endpoint.sys, "platform", "linux"):
            _attention, info = gateway_facts.gateway_runtime_report(runtime)
        text = "\n".join(info)
        self.assertIn(f"backend systemd ({endpoint.DEFAULT_UNIT}) · supervision systemd (restarts on failure) · "
                      "confinement tmpfs+TierB (systemd)", text)
        self.assertIn(f"Gateway logs: the {endpoint.DEFAULT_UNIT} journal (24 h window)", text)

    def test_a_home_without_a_gateway_is_not_observed(self) -> None:
        with mock.patch.object(endpoint, "set_up", return_value=False), \
                mock.patch.object(gl.Gateway, "observe", side_effect=AssertionError("live observation")):
            _attention, info = self.facts()
        self.assertIn(gateway_facts.GATEWAY_NOT_SET_UP, info)
        self.assertFalse([line for line in info if line.startswith("Gateway instance lock")])

    def test_a_fixture_runtime_is_never_observed_live(self) -> None:
        runtime = self.make_runtime()  # injected health, no gateway seams
        with mock.patch.object(gl.Gateway, "observe", side_effect=AssertionError("live observation")):
            _attention, info = gateway_facts.gateway_runtime_report(runtime)
        self.assertFalse([line for line in info if line.startswith("Gateway instance lock")])

    def test_doctor_shows_the_facts(self) -> None:
        self.runtime.doctor_callback = None
        self.runtime.doctor_served_callback = mock.Mock(return_value=([], []))
        with mock.patch('claude_multi.cli.gateway_facts._read_gateway_journal', return_value=None):
            _problems, info, _attention = cli._collect_doctor_reports(self.runtime)
        self.assertTrue(any(line.startswith("Gateway: backend on-demand") for line in info), info)
        # Management follows the release channel, not the on-demand backend.
        self.assertIn(quota.state_text(quota.PoolStatus("unavailable"),
                                       restart_hint=cli.gateway_service_hint("restart", self.runtime),
                                       backend=endpoint.ON_DEMAND), info)


class LocalFilesTests(_GatewayDoctor):
    """Roots and secret files are checked before the gateway key (no misleading key remedy)."""

    def test_unsafe_roots_and_secret_files_have_their_own_fix_first(self) -> None:
        config = self.runtime.home / ".config" / "claude-multi"
        os.chmod(config, 0o755)
        self.runtime.doctor_callback = None
        problems, _info, _attention = cli._collect_doctor_reports(self.runtime)
        self.assertIn(f"~/.config/claude-multi has mode 0755 (group/other access): chmod 700 ~/.config/claude-multi",
                      problems[0])
        self.assertFalse([line for line in problems if line.startswith("local gateway key file:")])
        os.chmod(config, 0o700)
        os.chmod(config / "api-key", 0o644)
        problems, _info, _attention = cli._collect_doctor_reports(self.runtime)
        self.assertIn("~/.config/claude-multi/api-key has mode 0644 (group/other access): "
                      "chmod 600 ~/.config/claude-multi/api-key", problems)
        os.chmod(config / "api-key", 0o600)
        target = self.root / "elsewhere.yaml"
        target.write_text("x")
        os.symlink(target, config / "config.yaml")
        problems, _flagged = gateway_facts.local_files_report(self.runtime)
        self.assertIn("is a symlink — claude-multi refuses symlinks", problems[0])

    def test_a_malformed_key_is_reported_by_shape_never_by_value(self) -> None:
        key = self.runtime.home / ".config" / "claude-multi" / "api-key"
        state.atomic_write(key, b"not-a-token-SENTINEL\n")
        problems, flagged = gateway_facts.local_files_report(self.runtime)
        self.assertIn(key, flagged)
        self.assertIn("invalid token shape", problems[0])
        self.assertNotIn("SENTINEL", repr(problems))

    def test_a_foreign_owner_is_named(self) -> None:
        lstat = os.lstat
        secret = Path(self.env["CLAUDE_MULTI_SECRET_ENV"])

        def fake(path, *args, **kwargs):
            info = lstat(path, *args, **kwargs)
            if os.fspath(path) == str(secret):
                values = list(info)
                values[stat.ST_UID] = info.st_uid + 1
                return os.stat_result(values)
            return info

        with mock.patch.object(gateway_facts.os, "lstat", side_effect=fake):
            problems, _flagged = gateway_facts.local_files_report(self.runtime)
        self.assertTrue(any("not by you" in line for line in problems), problems)


class PlatformTests(_GatewayDoctor):
    def wsl_runtime(self, *, cwd=None):
        proc = self.root / "proc"
        (proc / "sys/kernel").mkdir(parents=True, exist_ok=True)
        (proc / "sys/kernel/osrelease").write_text("5.15.153.1-microsoft-standard-WSL2\n")
        return self.make_runtime(proc_root=proc, cwd=cwd or self.project)

    def test_wsl_is_info_and_a_windows_drive_state_root_blocks(self) -> None:
        runtime = self.wsl_runtime()
        problems, info = gateway_facts.platform_report(runtime)
        self.assertEqual(problems, [])
        self.assertTrue(info[0].startswith("WSL: running under WSL; the gateway stops when the WSL VM shuts down"))
        self.assertIn("WSL: the Windows-side and WSL-side Claude configurations are separate", info[1])
        self.fstype.return_value = "9p"
        problems, _info = gateway_facts.platform_report(runtime)
        self.assertIn("is on a Windows drive (9p) under WSL", problems[0])

    def test_outside_wsl_there_is_no_wsl_line(self) -> None:
        proc = self.root / "proc-linux"
        (proc / "sys/kernel").mkdir(parents=True, exist_ok=True)
        (proc / "sys/kernel/osrelease").write_text("6.8.0-generic\n")
        runtime = self.make_runtime(proc_root=proc, cwd=self.project)
        runtime.environ.pop("WSL_DISTRO_NAME", None)
        _problems, info = gateway_facts.platform_report(runtime)
        self.assertFalse(any(line.startswith("WSL") for line in info), info)

    def test_a_windows_drive_cwd_is_info(self) -> None:
        runtime = self.wsl_runtime()
        runtime.cwd = Path("/mnt/c/Users/me/project")
        _problems, info = gateway_facts.platform_report(runtime)
        self.assertTrue(any("is on a Windows drive: file access is slow" in line for line in info), info)

    def test_a_network_state_root_makes_liveness_unknown(self) -> None:
        self.fstype.return_value = "nfs4"
        runtime = self.make_runtime(background_liveness=None)
        _problems, info = gateway_facts.platform_report(runtime)
        self.assertTrue(any("network filesystem (nfs4)" in line for line in info), info)
        with mock.patch.object(sessions, "background_liveness",
                               return_value=sessions.BackgroundLiveness(True, frozenset({"abc"}))):
            verdict = cli.Runtime._background_liveness(runtime)
        self.assertFalse(verdict.known)
        self.assertIn("sessions on other hosts are invisible", verdict.reason)
        self.fstype.return_value = "ext4"
        with mock.patch.object(sessions, "background_liveness",
                               return_value=sessions.BackgroundLiveness(True, frozenset({"abc"}))):
            self.assertTrue(cli.Runtime._background_liveness(runtime).known)


class MountTableTests(unittest.TestCase):
    MOUNTINFO = ("22 1 0:21 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
                 "40 22 0:35 / /home rw shared:2 - nfs4 server:/home rw\n"
                 "41 40 0:36 / /home/me/local\\040dir rw - ext4 /dev/sdb1 rw\n"
                 "50 22 0:40 / /mnt/c rw - 9p drvfs rw,aname=drvfs\n")

    def test_linux_longest_mount_point_wins(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "mountinfo"
            table.write_text(self.MOUNTINFO)
            kind = lambda path: mounts.filesystem_type(path, platform="linux", mountinfo=table)  # noqa: E731
            with mock.patch.object(mounts.os.path, "realpath", side_effect=lambda p: os.fspath(p)), \
                    mock.patch.object(Path, "exists", return_value=True):
                self.assertEqual(kind("/home/me/.local/state"), "nfs4")
                self.assertEqual(kind("/home/me/local dir/x"), "ext4")
                self.assertEqual(kind("/mnt/c/Users"), "9p")
                self.assertEqual(kind("/var/tmp"), "ext4")
            self.assertIsNone(mounts.filesystem_type("/x", platform="linux", mountinfo=Path(tmp) / "absent"))

    def test_darwin_mount_output(self) -> None:
        output = ("/dev/disk3s1s1 on / (apfs, sealed, local, read-only)\n"
                  "//me@nas/home on /Users/me (smbfs, nodev, nosuid, mounted by me)\n")
        runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout=output))
        with mock.patch.object(mounts.os.path, "realpath", side_effect=lambda p: os.fspath(p)), \
                mock.patch.object(Path, "exists", return_value=True):
            self.assertEqual(mounts.filesystem_type("/Users/me/.local/state", platform="darwin", runner=runner),
                             "smbfs")
        self.assertIn("smbfs", mounts.NETWORK_TYPES)

    def test_wsl_markers(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            self.assertIsNone(mounts.wsl({}, proc_root=proc))
            self.assertEqual(mounts.wsl({"WSL_DISTRO_NAME": "Ubuntu"}, proc_root=proc), "Ubuntu")
            (proc / "sys/fs/binfmt_misc").mkdir(parents=True)
            (proc / "sys/fs/binfmt_misc/WSLInterop").write_text("")
            self.assertEqual(mounts.wsl({}, proc_root=proc), "WSL")


class CmGatewayLineTests(V4Case):
    def test_answering_and_down_lines(self) -> None:
        self.assertEqual(lineup.gateway_health_line(self.runtime),
                         f"gateway: answering at {self.runtime.catalog.docs['gateway']['gateway']['base_url']}")
        runtime = self.make_runtime(health_get=mock.Mock(side_effect=ConnectionRefusedError("fixture")))
        line = lineup.gateway_health_line(runtime)
        self.assertIn("gateway: not answering at http://127.0.0.1:", line)
        self.assertIn("start it: claude-multi gateway start (details: claude-multi gateway status)", line)
        runtime = self.make_runtime(health_get=lambda _b, _p: 503)
        self.assertIn("answered HTTP 503", lineup.gateway_health_line(runtime))
        self.assertEqual(lineup._with_gateway_line("head\nbody\n", "gw"), "head\ngw\nbody\n")


class GatewayActionsTests(_GatewayDoctor):
    """The TUI paths: H's gateway prompt and the W dialog (start, restart, log tail)."""

    def test_the_view_offers_start_only_when_stopped_and_restart_only_when_ours(self) -> None:
        view = gateway_actions.observe(self.runtime)
        self.assertTrue(view.can_start)
        self.assertFalse(view.can_restart)
        self.world.become_ready("cccccccccccccccc")
        view = gateway_actions.observe(self.runtime)
        self.assertEqual((view.can_start, view.can_restart), (False, True))
        self.world.listener = "foreign"
        view = gateway_actions.observe(self.runtime)
        self.assertEqual((view.can_start, view.can_restart), (False, False))
        self.assertEqual(gateway_actions.act(view, "restart").status, "refused")
        self.assertEqual(self.world.terminated, [])
        fixture = gateway_actions.observe(self.make_runtime())
        self.assertIsNone(fixture.seen)
        self.assertFalse(fixture.can_start or fixture.can_restart)
        self.assertIn("gateway stop", gateway_actions.CLI_ONLY)

    def test_the_health_prompt_starts_a_stopped_gateway(self) -> None:
        out = io.StringIO()
        follow = gateway_actions.health_prompt(self.runtime, io.StringIO("s\n\n"), out)
        self.assertIsNone(follow)
        self.assertEqual(len(self.world.spawned), 1)
        printed = out.getvalue()
        self.assertIn("Gateway: stopped", printed)
        self.assertIn("s  start it now · l  show its log", printed)
        self.assertIn("gateway ready", printed)
        self.assertIn("Press Enter to return to claude-multi.", printed)
        self.assertEqual(gateway_actions.health_prompt(self.runtime, io.StringIO("l\n"), io.StringIO()), "log")
        self.assertIsNone(gateway_actions.health_prompt(self.runtime, io.StringIO("s\n"), io.StringIO()))
        self.assertEqual(len(self.world.spawned), 1)  # already ours: no start is offered or run

    def test_the_dialog_shows_the_log_tail_in_a_text_view(self) -> None:
        self.world.become_ready("dddddddddddddddd")
        self.log("dddddddddddddddd", ["first line", "last line"])
        win = FakeWindow(["\t", "\n", ESC], height=24, width=80)  # Restart now → Show log
        with mock.patch.object(tui.Modal, "run", return_value="log"):
            message = gateway_actions.dialog(self.runtime, win, tui.MONO_PALETTE)
        self.assertEqual(message, "")
        text = "\n".join(win.frames)
        self.assertIn("gateway log — last 200 lines", text)
        self.assertIn("last line", text)

    def test_the_dialog_restarts_only_a_proven_gateway(self) -> None:
        self.world.become_ready("eeeeeeeeeeeeeeee")
        out = io.StringIO()
        captured = {}

        def choose(modal, *_args, **_kwargs):
            captured["buttons"] = [label for label, _value in modal.buttons]
            return "restart"

        with mock.patch.object(tui.Modal, "run", autospec=True, side_effect=choose), \
                mock.patch.object(tui, "suspended_curses", mock.MagicMock()):
            message = gateway_actions.dialog(self.runtime, FakeWindow([]), tui.MONO_PALETTE,
                                             tty_in=io.StringIO("\n"), tty_out=out)
        self.assertEqual(captured["buttons"], ["Restart now", "Show log", "Close"])
        self.assertEqual(self.world.terminated, [4242])
        self.assertTrue(message.startswith("gateway ready"), message)
        self.assertIn("Restarting the gateway", out.getvalue())


class CoverageHoldTests(_GatewayDoctor):
    """A verbose, long-running instance whose log is larger than one read
    evaluates: the hold is incomplete coverage, not a failed save. Doctor,
    ``gateway status``, stop, restart, update and uninstall say so and name
    the command-line clear-hold recovery, which then releases it."""

    NONCE = "cccccccccccccccc"

    def over_budget(self, *, failed_tail: bool = False) -> None:
        self.ready(self.NONCE)
        filler = "[2026-10-02 12:00:00] [--------] [debug] [handler.go:7] verbose request trace " + "x" * 100
        count = gateway_hold.FILE_BUDGET // len(filler) + 64
        tail = [save_line("failed")] if failed_tail else []
        self.log(self.NONCE, ["started", *([filler] * count), *tail])

    def gateway(self) -> gl.Gateway:
        return self.runtime.gateway()

    def test_the_over_budget_log_is_a_coverage_only_hold(self) -> None:
        self.over_budget()
        report = self.gateway().persistence_hold()
        self.assertTrue(report.held)
        self.assertTrue(report.coverage_only, report.reasons)
        self.assertIn("coverage truncated", "; ".join(report.reasons))
        self.assertEqual(gateway_hold.remedy(report), gateway_hold.COVERAGE_REMEDY)
        self.assertTrue(gateway_hold.describe(report).startswith(gateway_hold.COVERAGE_ONLY))

    def test_doctor_names_the_cause_and_the_clear_hold_recovery(self) -> None:
        self.over_budget()
        attention, _info = self.facts()
        line = next(line for line in attention if line.startswith("gateway persistence hold: HELD"))
        self.assertIn("log coverage is incomplete, not because a credential failed", line)
        self.assertIn("claude-multi gateway clear-hold", line)
        self.assertIn("stop, restart, update and uninstall are refused", line)

    def test_stop_and_restart_refuse_with_the_cause_and_the_recovery(self) -> None:
        self.over_budget()
        for outcome in (self.gateway().stop(wait=0.1), self.gateway().restart(max_wait=0.1)):
            with self.subTest(status=outcome.status):
                self.assertEqual(outcome.status, "held")
                self.assertIn(gateway_hold.COVERAGE_ONLY, outcome.message)
                self.assertEqual(outcome.remedy, gateway_hold.COVERAGE_REMEDY)
                self.assertIn("claude-multi gateway clear-hold", "\n".join(outcome.lines()))
        self.assertEqual(self.world.terminated, [])

    def test_status_names_the_cause_and_the_recovery(self) -> None:
        self.over_budget()
        text = "\n".join(self.gateway().status().lines(self.runtime.environ))
        self.assertIn(f"persistence hold: HELD ({gateway_hold.COVERAGE_ONLY})", text)
        self.assertIn(f"fix: {gateway_hold.COVERAGE_REMEDY}", text)

    def test_update_and_uninstall_refusals_name_the_cause_and_the_recovery(self) -> None:
        from claude_multi import install_txn
        from claude_multi.setup import external

        self.over_budget()
        gateway = self.gateway()
        for text in (install_txn.hold_reason(self.state_root, self.runtime.environ, gateway=gateway),
                     external._hold_problem(gateway, gateway.observe())):
            with self.subTest(text=text[:40]):
                self.assertIn(gateway_hold.COVERAGE_ONLY, text)
                self.assertIn("claude-multi gateway clear-hold", text)

    def test_clear_hold_releases_it_and_stop_proceeds(self) -> None:
        self.over_budget()
        gateway = self.gateway()
        self.assertEqual(gateway.clear_hold(gateway.persistence_hold()), ())
        self.assertFalse(gateway.persistence_hold().held)
        self.assertNotEqual(gateway.stop(wait=0.1).status, "held")
        self.assertEqual(self.world.terminated, [4242])

    def test_logs_beyond_the_total_budget_clear_too(self) -> None:
        self.ready(self.NONCE)
        self.log(self.NONCE, ["started"])
        report = gateway_hold.evaluate_files(self.state_root, ("anthropic",), total_budget=0)
        self.assertTrue(report.held)
        self.assertTrue(report.coverage_only, report.reasons)
        self.assertIn("not evaluated (the logs exceed the read budget)", report.reasons[0])
        gateway_hold.clear(self.state_root, report)
        self.assertFalse(gateway_hold.evaluate_files(self.state_root, ("anthropic",), total_budget=0).held)

    def test_a_failed_save_in_the_part_read_is_not_coverage_only(self) -> None:
        self.over_budget(failed_tail=True)
        report = self.gateway().persistence_hold()
        self.assertTrue(report.held)
        self.assertFalse(report.coverage_only)
        self.assertIn("a credential save failed in the part read", "; ".join(report.reasons))
        self.assertEqual(gateway_hold.remedy(report), gateway_hold.REMEDY)
        attention, _info = self.facts()
        line = next(line for line in attention if line.startswith("gateway persistence hold: HELD"))
        self.assertNotIn("log coverage is incomplete", line)
        self.assertIn(gateway_hold.REMEDY, line)
        stopped = self.gateway().stop(wait=0.1)
        self.assertEqual(stopped.remedy, gateway_hold.REMEDY)
        self.assertNotIn(gateway_hold.COVERAGE_ONLY, stopped.message)


if __name__ == "__main__":
    unittest.main()
