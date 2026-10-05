"""The state root's gateway inhibition: its record, its owners and every path it fences.

The record and its API (begin, advance, check, end, recover; the command for
shell owners), the stable inhibited outcome on every mutation path with the
read-only paths kept available, the owner's continuation, the doctor and
status reports, the entry points outside the command dispatcher called
directly (``claude-multi-proxy``, the ``/cm`` intercept, the hooks), and
a home whose gateway was never set up. Everything runs in temporary homes
against the lifecycle's fake gateway world, a stub service manager and
injected loopback seams: nothing reaches a real gateway, port or manager.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import socket
import stat
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT
from claude_multi import cli, endpoint, gateway_inhibition as gi, gateway_lifecycle as gl, launch, proxy, state
from claude_multi.cli import gateway_facts
from claude_multi.cli.screens import gateway_actions
from test_gateway_lifecycle import NOW
import test_cli
import test_continuity
import test_gateway_service
import test_operator as operator_fixtures  # module import: no test classes re-exported

OWNER = dict(owner="installer", purpose="update 1.0.0 to 1.0.1", phase="download", remedy="sh install.sh --recover")


def stale_after() -> datetime.timedelta:
    return datetime.timedelta(seconds=gi.DEFAULT_STALE_AFTER)


class RecordTests(unittest.TestCase):
    """The record format and the published API."""

    def setUp(self) -> None:
        import shutil
        import tempfile

        self.root = Path(tempfile.mkdtemp(prefix="cm-inhibition-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state_root = self.root / "state" / "claude-multi"

    def test_begin_writes_one_closed_private_record(self) -> None:
        token = gi.begin(self.state_root, **OWNER, expiry=gi.owner_process(os.getpid(), 120), now=NOW)
        path = gi.record_path(self.state_root)
        self.assertEqual(path, self.state_root / "gateway" / "inhibition.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        document = json.loads(path.read_text())
        self.assertEqual(set(document), {"version", "owner", "purpose", "phase", "created", "updated", "expiry",
                                         "remedy", "token"})
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["expiry"], {"policy": "owner-process", "pid": os.getpid(), "seconds": 120})
        self.assertEqual((document["created"], document["updated"]), (NOW.isoformat(), NOW.isoformat()))
        self.assertEqual(document["token"], token)
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        record = gi.read(self.state_root)
        self.assertEqual((record.owner, record.purpose, record.phase, record.remedy),
                         ("installer", "update 1.0.0 to 1.0.1", "download", "sh install.sh --recover"))
        self.assertNotIn(token, record.describe())
        # A deadline owner names no process.
        gi.end(self.state_root, token)
        gi.begin(self.state_root, **OWNER, expiry=gi.deadline(900), now=NOW)
        self.assertEqual(json.loads(path.read_text())["expiry"], {"policy": "deadline", "seconds": 900})

    def test_one_owner_at_a_time_and_only_the_owner_continues(self) -> None:
        token = gi.begin(self.state_root, **OWNER, now=NOW)
        with self.assertRaises(gi.InhibitionError) as raised:
            gi.begin(self.state_root, **{**OWNER, "owner": "cutover"}, now=NOW)
        self.assertIn("inhibited by installer (update 1.0.0 to 1.0.1; phase download", str(raised.exception))
        self.assertIsNone(gi.blocking(self.state_root, token=token))
        self.assertIsNotNone(gi.blocking(self.state_root))
        self.assertIsNotNone(gi.blocking(self.state_root, token="0" * 32))
        with self.assertRaises(gi.Inhibited) as raised:
            gi.guard(self.state_root, what="nothing was updated")
        self.assertTrue(str(raised.exception).endswith("; nothing was updated"))
        self.assertIn("sh install.sh --recover", raised.exception.remedy)
        gi.guard(self.state_root, token=token)  # the owner passes
        later = NOW + datetime.timedelta(seconds=30)
        advanced = gi.advance(self.state_root, token, "replace", now=later)
        self.assertEqual((advanced.phase, advanced.created, advanced.updated), ("replace", NOW, later))
        for wrong in ("0" * 32, "not-a-token"):
            with self.assertRaises(gi.InhibitionError):
                gi.advance(self.state_root, wrong, "replace")
            with self.assertRaises(gi.InhibitionError):
                gi.end(self.state_root, wrong)
        self.assertTrue(gi.end(self.state_root, token))
        self.assertFalse(gi.end(self.state_root, token))  # ending twice is fine
        self.assertIsNone(gi.read(self.state_root))
        gi.guard(self.state_root)

    def test_stale_policies(self) -> None:
        record = gi.Inhibition("installer", "p", "x", NOW, NOW, gi.owner_process(99999, 60), "r", "a" * 32)
        alive, gone = (lambda _pid: True), (lambda _pid: False)
        self.assertFalse(record.stale(now=NOW, exists=alive))
        self.assertTrue(record.stale(now=NOW, exists=gone))  # the owner process is gone
        self.assertTrue(record.stale(now=NOW + datetime.timedelta(seconds=61), exists=alive))  # no progress
        self.assertFalse(record.stale(now=NOW, exists=lambda _pid: None))  # unknown is not gone
        mine = gi.Inhibition("installer", "p", "x", NOW, NOW, gi.owner_process(os.getpid(), 60), "r", "a" * 32)
        self.assertTrue(mine.stale(now=NOW, exists=alive))  # left behind: its owner never asks
        timed = gi.Inhibition("cutover", "p", "x", NOW, NOW, gi.deadline(60), "r", "a" * 32)
        self.assertFalse(timed.stale(now=NOW + datetime.timedelta(seconds=60)))
        self.assertTrue(timed.stale(now=NOW + datetime.timedelta(seconds=61)))
        # A stale record still refuses: never ignored, reported with its remedy.
        token = gi.begin(self.state_root, **OWNER, expiry=gi.deadline(60), now=NOW)
        message, remedy = gi.refusal(gi.read(self.state_root), "nothing was started",
                                     now=NOW + datetime.timedelta(hours=1))
        self.assertIn("its owner did not finish (stale)", message)
        self.assertIn("sh install.sh --recover", remedy)
        self.assertIsNotNone(gi.blocking(self.state_root))
        self.assertTrue(gi.end(self.state_root, token))

    def test_recovery_is_explicit_and_idempotent(self) -> None:
        self.assertIsNone(gi.recover(self.state_root, "installer"))  # nothing to recover
        token = gi.begin(self.state_root, **OWNER, expiry=gi.owner_process(424242, 600), now=NOW)
        with self.assertRaises(gi.InhibitionError):  # its owner still runs
            gi.recover(self.state_root, "installer", now=NOW, exists=lambda _pid: True)
        with self.assertRaises(gi.InhibitionError):  # another owner's record
            gi.recover(self.state_root, "cutover", now=NOW, exists=lambda _pid: False)
        for _ in range(2):
            record = gi.recover(self.state_root, "installer", now=NOW, exists=lambda _pid: False)
            self.assertEqual((record.token, record.phase), (token, "download"))
        gi.advance(self.state_root, record.token, "rollback")
        self.assertTrue(gi.end(self.state_root, record.token))
        self.assertIsNone(gi.recover(self.state_root, "installer"))

    def test_a_stale_record_of_its_own_kind_is_taken_over_never_another(self) -> None:
        gi.begin(self.state_root, **OWNER, expiry=gi.deadline(60), now=NOW)
        later = NOW + datetime.timedelta(seconds=120)
        with self.assertRaises(gi.InhibitionError):
            gi.begin(self.state_root, **{**OWNER, "owner": "updater"}, now=later, take_over_stale=True)
        with self.assertRaises(gi.InhibitionError):  # not stale yet
            gi.begin(self.state_root, **OWNER, now=NOW, take_over_stale=True)
        token = gi.begin(self.state_root, **OWNER, now=later, take_over_stale=True)
        self.assertEqual(gi.read(self.state_root).token, token)

    def test_an_unreadable_record_inhibits_and_is_never_taken_over(self) -> None:
        gi.begin(self.state_root, **OWNER, now=NOW)
        path = gi.record_path(self.state_root)
        for raw in (b"{", json.dumps({"version": 2}).encode(), path.read_bytes().replace(b'"version":1', b'"version":9')):
            with self.subTest(raw=raw[:20]):
                state.atomic_write(path, raw)
                record = gi.read(self.state_root)
                self.assertFalse(record.readable)
                self.assertIsNotNone(gi.blocking(self.state_root, token="a" * 32))
                message, remedy = gi.refusal(record, "nothing was changed")
                self.assertIn("unreadable record", message)
                self.assertIn("inhibition.json", remedy)
                with self.assertRaises(gi.InhibitionError):
                    gi.begin(self.state_root, **OWNER, now=NOW + datetime.timedelta(days=9), take_over_stale=True)
                with self.assertRaises(gi.InhibitionError):
                    gi.recover(self.state_root, "installer")
        path.chmod(0o644)  # unsafe mode: unreadable too, never "none"
        self.assertFalse(gi.read(self.state_root).readable)

    def test_input_is_checked(self) -> None:
        cases = {"owner": "Installer", "phase": "two words", "purpose": "a\nb", "remedy": ""}
        for field, value in cases.items():
            with self.subTest(field=field), self.assertRaises(gi.InhibitionError):
                gi.begin(self.state_root, **{**OWNER, field: value})
        for expiry in (gi.Expiry("forever", 10), gi.Expiry(gi.DEADLINE, 0), gi.Expiry(gi.OWNER_PROCESS, 10, None)):
            with self.subTest(expiry=expiry), self.assertRaises(gi.InhibitionError):
                gi.begin(self.state_root, **OWNER, expiry=expiry)
        self.assertIsNone(gi.read(self.state_root))

    def test_begin_waits_for_the_start_lock(self) -> None:
        lock = state.FileLock(gi.service.ensure_gateway_workdir(self.state_root) / "gateway-start")
        self.assertTrue(lock.acquire(blocking=False))
        try:
            with self.assertRaises(gi.InhibitionError) as raised:
                gi.begin(self.state_root, **OWNER, lock_wait=0.1)
            self.assertIn("start lock", str(raised.exception))
            self.assertIsNone(gi.read(self.state_root))
            # A caller that holds the lock itself records under it.
            gi.begin(self.state_root, **OWNER, held_lock=True)
        finally:
            lock.release()

    def command(self, *argv: str, token: str | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        environ = {"HOME": str(self.root / "home")}
        if token is not None:
            environ[gi.TOKEN_ENV] = token
        code = gi.main(["--state-root", str(self.state_root), *argv], environ=environ, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_the_command_for_shell_owners(self) -> None:
        self.assertEqual(self.command("status")[0], 0)
        self.assertEqual(self.command("check")[0], 0)
        self.assertEqual(self.command("begin", "--owner", "cutover", "--purpose", "move", "--phase", "freeze",
                                      "--remedy", "sh cutover.sh --recover")[0], 1)  # no expiry given
        code, out, _ = self.command("begin", "--owner", "cutover", "--purpose", "move to the bundle",
                                    "--phase", "freeze", "--remedy", "sh cutover.sh --recover",
                                    "--stale-after", "3600")
        self.assertEqual(code, 0)
        token = out.strip()
        self.assertEqual(gi.read(self.state_root).token, token)
        self.assertEqual(gi.read(self.state_root).expiry, gi.deadline(3600))
        code, out, _ = self.command("status")
        self.assertEqual(code, 1)
        self.assertIn("inhibited by cutover (move to the bundle; phase freeze", out)
        self.assertNotIn(token, out)
        code, _out, err = self.command("check")
        self.assertEqual(code, 1)
        self.assertIn("fix: wait until it has finished", err)
        self.assertEqual(self.command("check", token=token)[0], 0)
        self.assertEqual(self.command("advance", "--phase", "switch")[0], 1)  # no token
        self.assertEqual(self.command("advance", "--phase", "switch", token=token)[0], 0)
        self.assertEqual(gi.read(self.state_root).phase, "switch")
        code, _out, err = self.command("recover", "--owner", "cutover")
        self.assertEqual(code, 1)  # in progress (its deadline has not passed)
        self.assertEqual(self.command("end", token="0" * 32)[0], 1)
        self.assertEqual(self.command("end", token=token)[0], 0)
        self.assertEqual(self.command("end", token=token)[:2], (0, ""))  # idempotent
        self.assertEqual(self.command("recover", "--owner", "cutover")[:2], (0, ""))  # nothing to recover
        code, out, _ = self.command("begin", "--owner", "installer", "--purpose", "first install", "--phase", "unpack",
                                    "--remedy", "sh install.sh --recover", "--pid", "1")
        self.assertEqual(code, 0)
        self.assertEqual(gi.read(self.state_root).expiry, gi.owner_process(1, gi.DEFAULT_STALE_AFTER))
        self.assertEqual(self.command("--bogus")[0], 2)
        self.assertEqual(gi.main(["--state-root", "relative", "status"], environ={}, stdout=io.StringIO(),
                                 stderr=io.StringIO()), 2)


class _Inhibited(test_gateway_service._ServiceCase):
    """The service harness (stub manager, fake gateway world) with an inhibition helper."""

    def fake_binary(self) -> str:
        """A gateway executable that is never run (exec goes through a recorder)."""

        binary = self.root / "fake-gateway" / "cli-proxy-api"
        if not binary.exists():
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\nexit 1\n")
            binary.chmod(0o755)
        return str(binary)

    def inhibit(self, owner: str = "cutover", *, pid: int | None = None, at: datetime.datetime | None = None) -> str:
        expiry = gi.owner_process(pid if pid is not None else os.getppid()) if pid != 0 else gi.deadline(3600)
        return gi.begin(self.state_root, owner=owner, purpose="move to the bundle", phase="freeze",
                        remedy="sh cutover.sh --recover", expiry=expiry, now=at or self.world.now)

    def runtime(self, **environ: str) -> cli.Runtime:
        launcher = self.home / ".local/share/claude-multi/install/current/bin/claude-multi"
        return cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.environ(CLAUDE_MULTI_HOOK_COMMAND=str(launcher),
                                                                         **environ),
                           cwd=self.root, managed_root=self.root / "managed", gateway_seams=self.seams(),
                           gateway_service_seams=test_gateway_service.gs.ServiceSeams(
                               runner=self.manager, which=lambda *_a, **_k: None))

    def run_cli(self, *argv: str, **environ: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(list(argv), runtime=self.runtime(**environ), output_stream=out, interactive=False)
        return code, out.getvalue(), err.getvalue()


class MutationPathTests(_Inhibited):
    """Every mutation path refuses with the one inhibited outcome before it dispatches anything."""

    def manager_changes(self) -> list[str]:
        """The service manager requests other than reads (``show``, ``is-active``)."""

        return [verb for verb in self.manager.verbs() if verb not in ("show", "is-active")]

    def assert_inhibited(self, outcome: gl.Outcome) -> None:
        self.assertEqual(outcome.status, "inhibited", outcome.lines())
        self.assertEqual(outcome.exit_code, 1)
        self.assertIn("the gateway is inhibited by cutover (move to the bundle; phase freeze", outcome.message)
        self.assertIn("sh cutover.sh --recover", outcome.remedy)

    def test_starts_stops_and_service_changes_refuse_before_any_effect(self) -> None:
        self.inhibit()
        before = endpoint.read_config(self.home)
        self.assert_inhibited(self.gateway().ensure(max_wait=1))  # the token helper's automatic start
        self.assert_inhibited(self.gateway().ensure(max_wait=1, explicit=True, choose_port=True))
        self.assert_inhibited(self.service().install())
        self.running()
        self.assert_inhibited(self.gateway().stop())
        self.assert_inhibited(self.gateway().restart())
        self.assertEqual(self.world.spawned, [])
        self.assertEqual(self.world.terminated, [])
        self.assertEqual(self.manager_changes(), [])
        self.assertFalse(self.unit_dir.exists())
        self.assertEqual(endpoint.read_config(self.home), before)
        self.assertFalse((self.workdir / gl.START_HISTORY).exists())

    def test_a_new_install_records_no_port_under_an_inhibition(self) -> None:
        endpoint.endpoint_path(self.home).unlink()
        (self.home / ".config/claude-multi/api-key").unlink()
        self.inhibit()
        self.assert_inhibited(self.gateway().ensure(max_wait=1, explicit=True, choose_port=True))
        self.assert_inhibited(self.service().install())
        self.assertFalse(endpoint.endpoint_path(self.home).exists())
        self.assertEqual(self.world.spawned, [])

    def test_the_systemd_backend_asks_the_manager_nothing(self) -> None:
        self.assertTrue(self.service().install().ok)
        self.manager.calls.clear()
        self.manager.active.clear()
        self.world.lock, self.world.alive, self.world.listener = False, False, "none"
        self.inhibit()
        self.assert_inhibited(self.gateway().ensure(max_wait=1))
        self.assert_inhibited(self.service().uninstall())
        self.assert_inhibited(self.service().install())  # a refresh too
        self.assertEqual(self.manager_changes(), [])

    def test_reading_stays_available_and_a_ready_gateway_is_still_delivered(self) -> None:
        self.running()
        self.inhibit()
        outcome = self.gateway().ensure(max_wait=1)
        self.assertEqual(outcome.status, "ready", outcome.lines())  # proven ours and ready: the token is delivered
        code, out, err = self.run_cli("gateway", "ensure", "--quiet", "--max-wait", "1")
        self.assertEqual((code, out, err), (0, "", ""))
        code, out, _ = self.run_cli("gateway", "status")
        self.assertEqual(code, 0)
        self.assertIn("gateway: ours", out)
        self.assertIn("  inhibition: the gateway is inhibited by cutover (move to the bundle; phase freeze", out)
        self.assertIn("    fix: wait until it has finished", out)
        code, out, err = self.run_cli("gateway", "stop")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("claude-multi: the gateway is inhibited by cutover", err)
        self.assertIn("  fix: wait until it has finished; if it was interrupted, its owner finishes or releases it: "
                      "sh cutover.sh --recover", err)
        self.assertEqual(self.world.terminated, [])
        self.assertEqual(self.world.spawned, [])
        # A gateway that is not proven ours is never delivered, and nothing is started.
        self.world.listener = "unknown"
        self.assert_inhibited(self.gateway().ensure(max_wait=1))
        self.world.lock, self.world.alive, self.world.listener = False, False, "none"
        code, out, err = self.run_cli("gateway", "ensure", "--quiet", "--max-wait", "1")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("inhibited", err)
        self.assertEqual(self.world.spawned, [])

    def test_the_setup_step_refuses_before_it_records_anything(self) -> None:
        self.inhibit()
        before = endpoint.endpoint_path(self.home).read_bytes()
        code, out, err = self.run_cli("setup", "--step", "gateway", "--proxy", "http://proxy.example.invalid:3128")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("inhibited by cutover", err)
        self.assertEqual(endpoint.endpoint_path(self.home).read_bytes(), before)

    def test_the_owner_acts_through_its_token_and_the_helper_never_does(self) -> None:
        token = self.inhibit()
        owner = {gi.TOKEN_ENV: token}
        # The token helper's ensure never acts for the owner, even with its token around.
        code, _out, err = self.run_cli("gateway", "ensure", "--quiet", "--max-wait", "1", **owner)
        self.assertEqual(code, 1, err)
        self.assertEqual(self.world.spawned, [])
        # The owner's explicit start runs, and hands the token to the gateway it starts.
        code, out, err = self.run_cli("gateway", "start", **owner)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.world.spawned), 1)
        self.assertEqual(self.world.spawned[0]["env"][gi.TOKEN_ENV], token)
        code, _out, err = self.run_cli("gateway", "restart", **owner)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.world.spawned), 2)
        # A non-owner start never forwards an inherited token.
        gi.end(self.state_root, token)
        self.world.lock, self.world.alive, self.world.listener = False, False, "none"
        code, _out, err = self.run_cli("gateway", "start", **{gi.TOKEN_ENV: "f" * 32})
        self.assertEqual(code, 0, err)
        self.assertNotIn(gi.TOKEN_ENV, self.world.spawned[-1]["env"])
        # The gateway program itself never carries it.
        env = proxy._gateway_exec_env({"HOME": str(self.home), gi.TOKEN_ENV: token, "LANG": "C"})
        self.assertEqual(env, {"HOME": str(self.home), "LANG": "C"})

    def test_the_owner_runs_the_service_verbs_under_its_own_record(self) -> None:
        token = self.inhibit()
        code, out, err = self.run_cli("gateway", "service", "install", **{gi.TOKEN_ENV: token})
        self.assertEqual(code, 0, err)
        self.assertIn("installed and running", out)
        record = gi.read(self.state_root)
        self.assertEqual((record.owner, record.token), ("cutover", token))  # its record, untouched
        code, out, err = self.run_cli("gateway", "service", "uninstall", **{gi.TOKEN_ENV: token})
        self.assertEqual(code, 0, err)
        self.assertEqual(gi.read(self.state_root).token, token)
        code, _out, err = self.run_cli("gateway", "service", "install")
        self.assertEqual(code, 1)
        self.assertIn("inhibited by cutover", err)

    def test_a_launch_starts_nothing_and_records_no_port(self) -> None:
        self.inhibit()
        runtime = self.runtime()
        with self.assertRaises(launch.LaunchError) as raised:
            runtime.ensure_gateway()
        self.assertIn("local gateway: the gateway is inhibited by cutover", str(raised.exception))
        self.assertIn("sh cutover.sh --recover", raised.exception.remedy)
        self.assertEqual(self.world.spawned, [])
        # A new install records nothing while inhibited; the launch reports the inhibition.
        endpoint.endpoint_path(self.home).unlink()
        (self.home / ".config/claude-multi/api-key").unlink()
        runtime = self.runtime()
        self.assertFalse(runtime.provision_endpoint())
        with self.assertRaises(launch.LaunchError):
            runtime.ensure_gateway()
        self.assertFalse(endpoint.endpoint_path(self.home).exists())
        # A gateway proven ours and ready serves the launch (read-only).
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.port))
        self.running()
        self.runtime().ensure_gateway()
        self.assertEqual(self.world.spawned, [])

    def test_an_interrupted_owner_is_reported_by_doctor_and_status(self) -> None:
        runtime = self.runtime()
        self.assertEqual(gateway_facts.inhibition_report(runtime), ([], [], []))
        self.inhibit(at=datetime.datetime.now(datetime.timezone.utc))  # doctor reads the real clock
        problems, attention, info = gateway_facts.inhibition_report(runtime)
        self.assertEqual((problems, info), ([], []))
        self.assertEqual(len(attention), 1)
        self.assertIn("inhibited by cutover (move to the bundle; phase freeze", attention[0])
        self.assertIn("fix: wait until it has finished", attention[0])
        gi.record_path(self.state_root).unlink()
        self.inhibit(pid=0, at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2))
        problems, attention, _ = gateway_facts.inhibition_report(runtime)
        self.assertEqual(attention, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("its owner did not finish (stale)", problems[0])
        self.assertIn("sh cutover.sh --recover", problems[0])
        state.atomic_write(gi.record_path(self.state_root), b"{")
        problems, _attention, _ = gateway_facts.inhibition_report(runtime)
        self.assertIn("unreadable record", problems[0])
        self.assertIn("inhibition.json", problems[0])

    def test_the_tui_view_says_why_and_offers_no_change(self) -> None:
        self.inhibit()
        runtime = self.runtime()
        view = gateway_actions.observe(runtime)
        self.assertTrue(view.seen is not None and view.seen.state == gl.STOPPED)
        self.assertIn("inhibited by cutover", view.inhibition)
        self.assertFalse(view.can_start or view.can_restart or view.can_install_service)
        out = io.StringIO()
        gateway_actions.health_prompt(runtime, io.StringIO("s\n"), out)
        self.assertIn("inhibited by cutover", out.getvalue())
        self.assertNotIn("s  start it now", out.getvalue())
        self.assertEqual(self.world.spawned, [])


class StaleHandOffTests(_Inhibited):
    def test_only_the_service_verbs_finish_an_interrupted_hand_off(self) -> None:
        self.assertTrue(self.service().install().ok)
        self.manager.active.clear()
        self.world.lock, self.world.alive, self.world.listener = False, False, "none"
        # A hand-off of another (dead) process, long ago.
        gi.begin(self.state_root, owner=gl.SERVICE_OWNER, purpose="gateway service install (claude-multi-gateway)",
                 phase="install", remedy=gl.SERVICE_REMEDY, expiry=gi.owner_process(os.getppid()),
                 now=self.world.now - stale_after() * 2)
        self.assertEqual(self.gateway().ensure(max_wait=0).status, "inhibited")
        # Another owner's stale record is never taken over by the service verbs.
        gi.record_path(self.state_root).unlink()
        self.inhibit(pid=0, at=self.world.now - datetime.timedelta(days=2))
        self.assertEqual(self.service().install().status, "inhibited")


class _Entry(_Inhibited):
    """Direct calls of the paths outside the command dispatcher."""

    def proxy(self, *argv: str, **environ: str) -> tuple[int, str, str, list]:
        execs: list = []
        out, err = io.StringIO(), io.StringIO()
        env = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / ".local/state"),
               "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT), "CLAUDE_MULTI_PROXY_BIN": self.fake_binary(), **environ}
        with redirect_stdout(out), redirect_stderr(err):
            code = proxy.main(list(argv), environ=env, execve=lambda *a: execs.append(a),
                              models_get=lambda *_a: (200, set()), health_get=lambda *_a: 200,
                              listener_observer=lambda _b: proxy.service.OwnerVerdict("none", "fixture"),
                              pid_get=lambda **_k: (None, False))
        return code, out.getvalue(), err.getvalue(), execs

    def config_files(self) -> dict[str, bytes]:
        directory = self.home / ".config/claude-multi"
        return {path.name: path.read_bytes() for path in sorted(directory.iterdir()) if path.is_file()}

    def shims(self) -> dict[str, bytes]:
        directory = self.state_root / "bin"
        return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}

    @contextlib.contextmanager
    def process_environ(self, launcher: str, **extra: str):
        environ = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / ".local/state"),
                   "XDG_CONFIG_HOME": str(self.home / ".config"), "TERM": "dumb",
                   "CLAUDE_MULTI_HOOK_COMMAND": launcher, **extra}
        with mock.patch.dict(os.environ, environ):
            for name in (gi.TOKEN_ENV, "CLAUDE_MULTI_CHANNEL"):
                if name not in extra:
                    os.environ.pop(name, None)
            yield


class EntryPointFencingTests(_Entry):
    """The paths outside the command dispatcher, called directly."""

    def test_the_proxy_refuses_every_change_while_inhibited(self) -> None:
        token = self.inhibit()
        before = self.config_files()
        for argv in (["init"], ["init", "--start-check"], ["run"], ["run", "--prepare-and-exec"],
                     ["claude-login"], ["codex-device-login"], ["rotate-management-key"],
                     ["disable-management-key"], ["snapshot-auth", "--restore", "auth.pre-7.3.20260101"]):
            with self.subTest(argv=argv):
                code, out, err, execs = self.proxy(*argv)
                self.assertEqual((code, out, execs), (1, "", []))
                self.assertIn("claude-multi-proxy: the gateway is inhibited by cutover", err)
                self.assertIn("fix: wait until it has finished", err)
        self.assertEqual(self.config_files(), before)
        self.assertFalse(os.path.lexists(self.workdir / proxy.service.EXEC_STAMP))
        # Reading stays available; the owner passes.
        self.assertEqual(self.proxy("status")[0], 0)
        code, _out, err, execs = self.proxy("run", "--prepare-and-exec", **{gi.TOKEN_ENV: token})
        self.assertEqual(code, None, err)
        self.assertEqual(len(execs), 1)
        self.assertNotIn(gi.TOKEN_ENV, execs[0][2])

    def test_the_units_directives_are_the_reviewed_exception(self) -> None:
        self.assertEqual(self.proxy("init", "--prepare-start")[0], 0)
        self.inhibit()
        # The service manager runs them; every claude-multi path that asks it to is fenced.
        self.assertEqual(self.proxy("init", "--prepare-start")[0], 0)
        code, _out, err, execs = self.proxy("run", "--prepared")
        self.assertEqual((code, len(execs)), (None, 1), err)
        self.assertNotIn("inhibited", self.proxy("init", "--reload-check")[2])

    def test_the_proxy_refuses_another_channels_state_root(self) -> None:
        from claude_multi import installs

        installs.write_marker(self.state_root, "nix")
        for argv in (["init"], ["run", "--prepare-and-exec"], ["claude-login"]):
            with self.subTest(argv=argv):
                code, _out, err, execs = self.proxy(*argv, CLAUDE_MULTI_CHANNEL="bundle")
                self.assertEqual((code, execs), (1, []))
                self.assertIn("belongs to the nix installation, and this is the bundle one", err)
        self.assertEqual(self.proxy("init", CLAUDE_MULTI_CHANNEL="nix")[0], 0)
        self.assertEqual(self.proxy("init")[0], 0)  # an unknown channel neither claims nor refuses
        self.assertEqual(installs.read_marker(self.state_root), "nix")

    def test_the_hooks_and_the_cm_intercept_leave_the_shims_to_the_owner(self) -> None:
        import claude_multi.cli.commands.lineup as commands_lineup

        old, new = str(self.root / "old" / "claude-multi"), str(self.root / "new" / "claude-multi")
        with self.process_environ(old):
            cli.Runtime(asset_root=FIXTURE_ROOT)
        before = self.shims()
        self.assertTrue(any(old.encode() in raw for raw in before.values()))
        token = self.inhibit()
        runtime_id = "0b5e7f4e-1d2c-4b3a-8a9b-0c1d2e3f4a5b"
        with self.process_environ(new), redirect_stderr(io.StringIO()):
            cli.main(["session-event", "end", "--managed-id", runtime_id], input_stream=io.StringIO("{}"),
                     output_stream=io.StringIO(), interactive=False)
            commands_lineup._lineup_main(["--session", runtime_id, "show"], runtime=None, input_stream=None,
                                         output_stream=io.StringIO(), interactive=False)
            self.assertTrue(cli.Runtime(asset_root=FIXTURE_ROOT).inhibited_shims)
        self.assertEqual(self.shims(), before)
        # The owner's own run refreshes them; without an inhibition every run does.
        with self.process_environ(new, **{gi.TOKEN_ENV: token}):
            self.assertFalse(cli.Runtime(asset_root=FIXTURE_ROOT).inhibited_shims)
        self.assertTrue(any(new.encode() in raw for raw in self.shims().values()))
        gi.end(self.state_root, token)
        with self.process_environ(old):
            cli.Runtime(asset_root=FIXTURE_ROOT)
        self.assertEqual(self.shims(), before)


class StoppedProofTests(_Inhibited):
    """The unit's start preparation never trusts the manager's MainPID alone."""

    def setUp(self) -> None:
        super().setUp()
        from claude_multi import management

        self.management = management
        self.env = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / ".local/state"),
                    "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT), "CLAUDE_MULTI_PROXY_BIN": self.fake_binary(),
                    management.PATCHES_ENV: management.ALLOWLIST_PATCH,
                    management.CHANNEL_ENV: management.MANAGEMENT_CHANNEL}

    def prepare(self, owner: str = "none") -> str:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            # The manager says stopped: MainPID 0 and no recorded process.
            code = proxy.cmd_init(["--prepare-start"], environ=self.env, models_get=lambda *_a: (200, set()),
                                  listener_observer=lambda _b: proxy.service.OwnerVerdict(owner, "fixture"),
                                  pid_get=lambda **_k: (None, False))
        self.assertEqual(code, 0)
        return err.getvalue()

    def test_a_held_instance_lock_withdraws_the_stopped_proof(self) -> None:
        self.prepare()
        key = self.management.read_active(self.home)
        self.assertIsNotNone(key)
        self.management.stage_rotation(self.home)
        from claude_multi.platform import posix_fs

        held = posix_fs.hold_exclusive(self.workdir / proxy.service.INSTANCE_LOCK)  # an instance still owns it
        try:
            for owner in ("none", "ours", "unknown"):
                with self.subTest(listener=owner):
                    self.assertIn("stopped listener and PID not confirmed", self.prepare(owner))
                    self.assertIsNone(self.management.read_active(self.home))  # nothing selected
                    self.assertTrue(self.management.key_state(self.home).staged)  # nothing promoted
        finally:
            os.close(held)
        # A free lock with a live or unknown listener is no proof either.
        for owner in ("ours", "foreign", "unknown"):
            with self.subTest(free_lock_listener=owner):
                self.assertIn("not confirmed", self.prepare(owner))
                self.assertTrue(self.management.key_state(self.home).staged)
        # The lock is free (this preparation holds it) and nothing listens: promoted.
        self.assertIn("staged management key promoted", self.prepare())
        self.assertFalse(self.management.key_state(self.home).staged)
        self.assertNotEqual(self.management.read_active(self.home), key)
        # The preparation released the lock: the gateway's own run can take it.
        from claude_multi.platform import posix_fs as fs

        self.assertFalse(fs.lock_held(self.workdir / proxy.service.INSTANCE_LOCK))


class ConfigurationTests(test_cli.OperatorCommandCase):
    """Only the owner publishes a gateway configuration while it holds the inhibition."""

    def test_provider_writes_rotation_and_prune_wait_for_the_owner(self) -> None:
        root = self.runtime.session_store.root
        token = gi.begin(root, owner="updater", purpose="update 1.0.0 to 1.0.1", phase="replace",
                         remedy="claude-multi update --rollback")
        config = proxy.config_dir(self.runtime.home) / "config.yaml"
        before = config.read_bytes() if config.exists() else None
        code, _out, err = self.op(test_cli.ACME_ADD, "y\n")
        self.assertEqual(code, 1)
        self.assertIn("the gateway is inhibited by updater (update 1.0.0 to 1.0.1; phase replace", err)
        self.assertFalse((self.pdir / "acme.json").exists())  # undone
        self.assertEqual(config.read_bytes() if config.exists() else None, before)
        outcome = proxy.prune_aliases(self.runtime.home, root, None, environ=self.runtime.gateway_environ())
        self.assertEqual(outcome.code, 1)
        self.assertIn("inhibited by updater", outcome.lines[0])
        with self.assertRaises(gi.Inhibited):
            proxy.rotate_token(self.runtime.home, live_sessions=lambda: [], environ=self.runtime.gateway_environ(),
                               state_root=root)
        # The owner's own write publishes.
        code, _out, err = self.op(test_cli.ACME_ADD, "y\n", env={gi.TOKEN_ENV: token})
        self.assertEqual(code, 0, err)
        self.assertTrue((self.pdir / "acme.json").exists())


class FreshHomeTests(test_cli.OperatorCommandCase):
    """A home with no recorded endpoint and no earlier gateway is not set up:
    nothing reloads, applies or ensures a gateway on the packaged port."""

    def setUp(self) -> None:
        super().setUp()
        (self.runtime.home / ".config/claude-multi/api-key").unlink()  # nothing was ever rendered here
        self.assertFalse(endpoint.set_up(self.runtime.home))

    @contextlib.contextmanager
    def no_connection(self):
        attempts: list = []

        def refuse(_sock, address, *_a):
            attempts.append(address)
            raise AssertionError(f"a connection was attempted to {address}")

        with mock.patch.object(socket.socket, "connect", refuse), \
                mock.patch.object(socket.socket, "connect_ex", refuse):
            yield attempts
        self.assertEqual(attempts, [])

    def gateway_files(self) -> list[str]:
        directory = self.runtime.home / ".config/claude-multi"
        return sorted(name for name in ("config.yaml", "api-key", "endpoint.json") if (directory / name).exists())

    def test_models_add_sends_nothing_to_any_port(self) -> None:
        state.ensure_private_dir(self.pdir)
        state.atomic_write(self.pdir / "kimi.json",
                           json.dumps(operator_fixtures._fixture_files()["kimi"]).encode())
        before = (self.pdir / "kimi.json").read_bytes()
        with self.no_connection(), mock.patch.object(proxy, "await_sentinel",
                                                     side_effect=AssertionError("a reload was requested")):
            code, out, err = self.op(["models", "add", "kimi", "kimi-fixture-extra", "--as", "custom-kimi-extra",
                                      "--context", "65536", "--source", "operator",
                                      "--effort", "max=output-config-max"])
        self.assertEqual(code, 1, out)
        self.assertIn("the local gateway is not set up yet: run claude-multi setup --step gateway first", err)
        self.assertEqual((self.pdir / "kimi.json").read_bytes(), before)  # undone
        self.assertEqual(self.gateway_files(), [])  # no config, key or port: still a new install

    def test_apply_reload_and_the_helper_refuse_on_a_fresh_home(self) -> None:
        with self.no_connection():
            code, _out, err = self.op(["providers", "apply"])
            self.assertEqual(code, 1)
            self.assertIn("not set up", err)
            with self.assertRaises(endpoint.NotSetUpError):  # (the case's fixture patches the instance's)
                cli.Runtime.verify_reload(self.runtime, "claude-multi-render-0123abcd")
            gateway = gl.Gateway(home=self.runtime.home, state_root=self.runtime.session_store.root,
                                 environ=self.runtime.environ, gateway_document=self.runtime.catalog.docs["gateway"],
                                 installation=REPO_ROOT, seams=gl.Seams(
                                     spawn=mock.Mock(side_effect=AssertionError("spawned")),
                                     lock_held=lambda _p: False,
                                     listener=mock.Mock(side_effect=AssertionError("observed the packaged port"))))
            outcome = gateway.ensure(max_wait=1)
            self.assertEqual(outcome.status, "refused")
            self.assertIn("not set up", outcome.message)
            self.assertEqual(outcome.remedy, "set it up: claude-multi setup --step gateway")
            outcome = proxy.prune_aliases(self.runtime.home, self.runtime.session_store.root, None,
                                          environ=self.runtime.gateway_environ())
            self.assertEqual(outcome.code, 1)
            self.assertIn("not set up", outcome.lines[0])
            with self.assertRaises(endpoint.NotSetUpError):
                proxy.rotate_token(self.runtime.home, live_sessions=lambda: [])
            for argv in (["init"], ["run"], ["claude-login"]):
                with self.subTest(argv=argv):
                    err = io.StringIO()
                    with redirect_stderr(err), redirect_stdout(io.StringIO()):
                        code = proxy.main(argv, environ=self.runtime.gateway_environ(),
                                          execve=mock.Mock(side_effect=AssertionError("exec")))
                    self.assertEqual(code, 1)
                    self.assertIn("not set up", err.getvalue())
                    self.assertIn("fix: claude-multi setup --step gateway", err.getvalue())
        self.assertEqual(self.gateway_files(), [])
        status = io.StringIO()
        gateway.seams.listener = lambda _b, _p: proxy.service.OwnerVerdict("none", "fixture")
        status.write("\n".join(gateway.status().lines(self.runtime.environ)))
        self.assertIn("(not set up: claude-multi setup --step gateway)", status.getvalue())

    def test_an_existing_install_keeps_the_packaged_port(self) -> None:
        state.atomic_write(self.runtime.home / ".config/claude-multi/api-key", b"a" * 64 + b"\n")
        self.assertTrue(endpoint.set_up(self.runtime.home))
        observed: list[str] = []
        gateway = gl.Gateway(home=self.runtime.home, state_root=self.runtime.session_store.root,
                             environ=self.runtime.environ, gateway_document=self.runtime.catalog.docs["gateway"],
                             installation=REPO_ROOT, seams=gl.Seams(
                                 lock_held=lambda _p: False, health_get=lambda *_a: None,
                                 listener=lambda base, _pid: observed.append(base) or proxy.service.OwnerVerdict(
                                     "foreign", "another program", uid=1234)))
        outcome = gateway.ensure(max_wait=1)
        self.assertEqual(outcome.status, "refused")  # it observed the packaged port it keeps
        self.assertEqual(observed, [self.runtime.catalog.docs["gateway"]["gateway"]["base_url"]])
        self.assertFalse(endpoint.endpoint_path(self.runtime.home).exists())


def legacy_record(state_root: Path, *, pid: int, started: datetime.datetime, operation: str = "install",
                  raw: bytes | None = None) -> str:
    """An earlier hand-off record (service-handoff.json); returns its token."""

    token = "c" * 32
    document = {"version": 1, "operation": operation, "unit": "claude-multi-gateway", "pid": pid,
                "started_at": started.isoformat(), "token": token}
    state.atomic_write(gi.legacy_path(state_root), raw if raw is not None else json.dumps(document).encode())
    return token


class LegacyRecordTests(_Inhibited):
    """An earlier hand-off record keeps inhibiting until the service verbs finish it."""

    def test_an_interrupted_earlier_record_still_refuses_every_start(self) -> None:
        token = legacy_record(self.state_root, pid=os.getppid(), started=self.world.now - datetime.timedelta(days=1))
        record = gi.read(self.state_root)
        self.assertEqual((record.owner, record.phase, record.token), (gl.SERVICE_OWNER, "install", token))
        self.assertIn("gateway service install (claude-multi-gateway)", record.describe())
        outcome = self.gateway().ensure(max_wait=1)
        self.assertEqual(outcome.status, "inhibited", outcome.lines())
        self.assertIn("its owner did not finish (stale)", outcome.message)
        self.assertIn(gl.SERVICE_REMEDY, outcome.remedy)
        self.assertEqual(self.world.spawned, [])
        self.assertEqual(self.gateway().stop().status, "inhibited")
        # A recent one whose process runs is in progress (not stale), and refuses too.
        legacy_record(self.state_root, pid=os.getppid(), started=self.world.now)
        outcome = self.gateway().ensure(max_wait=1)
        self.assertEqual(outcome.status, "inhibited")
        self.assertNotIn("stale", outcome.message)
        self.assertEqual(self.world.spawned, [])

    def test_unreadable_and_conflicting_records_refuse_and_are_never_taken_over(self) -> None:
        legacy_record(self.state_root, pid=1, started=NOW, raw=b"{")
        self.assertFalse(gi.read(self.state_root).readable)
        self.assertEqual(self.gateway().ensure(max_wait=1).status, "inhibited")
        self.assertEqual(self.service().install().status, "inhibited")
        self.assertFalse(self.unit_dir.exists())
        # Both records at once are unknown: refused, never migrated or taken over.
        gi.legacy_path(self.state_root).unlink()
        self.inhibit(pid=0, at=self.world.now - datetime.timedelta(days=1))
        legacy_record(self.state_root, pid=os.getppid(), started=self.world.now - datetime.timedelta(days=1))
        record = gi.read(self.state_root)
        self.assertFalse(record.readable)
        self.assertIn("both inhibition.json and the earlier service-handoff.json", record.describe())
        self.assertEqual(self.service().install().status, "inhibited")
        with self.assertRaises(gi.InhibitionError):
            gi.begin(self.state_root, **OWNER, take_over_stale=True)
        self.assertTrue(gi.legacy_path(self.state_root).exists())

    def test_the_service_verbs_migrate_and_finish_it(self) -> None:
        self.assertTrue(self.service().install().ok)
        self.manager.active.clear()
        self.world.lock, self.world.alive, self.world.listener = False, False, "none"
        token = legacy_record(self.state_root, pid=os.getppid(), started=self.world.now - datetime.timedelta(days=1))
        # Recovery hands its token over in this format (migrated under the start lock).
        recovered = gi.recover(self.state_root, gl.SERVICE_OWNER, now=self.world.now)
        self.assertEqual(recovered.token, token)
        self.assertFalse(gi.legacy_path(self.state_root).exists())
        self.assertEqual(gi.read(self.state_root).token, token)
        outcome = self.service().install()
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertIn("an interrupted hand-off was finished", outcome.message)
        self.assertIsNone(gi.read(self.state_root))
        # An earlier record still in its own file is finished the same way.
        legacy_record(self.state_root, pid=os.getppid(), started=self.world.now - datetime.timedelta(days=1))
        self.assertTrue(self.service().install().ok)
        self.assertIsNone(gi.read(self.state_root))
        self.assertFalse(gi.legacy_path(self.state_root).exists())


class SerializationTests(_Inhibited):
    """A writer that passed its inhibition check finishes before a begin can record."""

    def test_setup_changes_the_endpoint_under_the_start_lock(self) -> None:
        real_swap_proxy = endpoint.swap_proxy
        attempts: list[str] = []

        def swap_proxy_racing_a_begin(*args, **kwargs):
            try:
                gi.begin(self.state_root, owner="cutover", purpose="move", phase="freeze",
                         remedy="sh cutover.sh --recover", lock_wait=0.2)
            except gi.InhibitionError as exc:
                attempts.append(str(exc))
            return real_swap_proxy(*args, **kwargs)

        with mock.patch.object(endpoint, "swap_proxy", swap_proxy_racing_a_begin):
            code, out, err = self.run_cli("setup", "--step", "gateway", "--proxy", "http://proxy.example.invalid:3128")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(attempts), 1)
        self.assertIn("start lock", attempts[0])  # the begin waited for setup, and gave up
        self.assertIsNone(gi.read(self.state_root))
        self.assertEqual(endpoint.read_config(self.home).proxy_url, "http://proxy.example.invalid:3128")
        # A begin that lands after the preparation but before the lock: setup
        # sees it under the lock and changes nothing.
        before = endpoint.endpoint_path(self.home).read_bytes()
        real_lock = gl.Gateway.lock_for_change

        def begin_first(gateway, *args, **kwargs):
            self.inhibit()
            return real_lock(gateway, *args, **kwargs)

        with mock.patch.object(gl.Gateway, "lock_for_change", begin_first):
            code, out, err = self.run_cli("setup", "--step", "gateway", "--no-proxy")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("inhibited by cutover", err)
        self.assertEqual(endpoint.endpoint_path(self.home).read_bytes(), before)

    def test_a_proxy_render_paused_after_its_check_holds_off_a_begin(self) -> None:
        import threading

        api_key_lock = state.FileLock(proxy.config_dir(self.home) / "api-key")
        self.assertTrue(api_key_lock.acquire(blocking=False))
        result: list = []
        env = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / ".local/state"),
               "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT), "CLAUDE_MULTI_PROXY_BIN": self.fake_binary()}

        seams = dict(models_get=lambda *_a: (200, set()), health_get=lambda *_a: 200,
                     listener_observer=lambda _b: proxy.service.OwnerVerdict("none", "fixture"),
                     pid_get=lambda **_k: (None, False))

        def init() -> None:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result.append(proxy.main(["init"], environ=env, **seams))

        worker = threading.Thread(target=init)
        released = False
        try:
            worker.start()
            # Wait until the render passed its check and waits for the api-key lock
            # (its writer fence is held: the exclusive side cannot be taken).
            fence = state.FileLock(self.state_root / gi.FENCE)
            for _ in range(500):
                if not fence.acquire(blocking=False):
                    break
                fence.release()
                worker.join(0.01)
            else:
                self.fail("the render never held its writer fence")
            with self.assertRaises(gi.InhibitionError) as raised:
                gi.begin(self.state_root, owner="cutover", purpose="move", phase="freeze",
                         remedy="sh cutover.sh --recover", lock_wait=0.3)
            self.assertIn("configuration or shims is in progress", str(raised.exception))
            self.assertIsNone(gi.read(self.state_root))
            api_key_lock.release()
            released = True
            worker.join(30)
        finally:
            if not released:
                api_key_lock.release()
            worker.join(30)
        self.assertEqual(result, [0])
        # Once it finished, the owner records, and the next render refuses.
        token = gi.begin(self.state_root, owner="cutover", purpose="move", phase="freeze",
                         remedy="sh cutover.sh --recover")
        before = (proxy.config_dir(self.home) / "config.yaml").read_bytes()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(proxy.main(["init"], environ=env, **seams), 1)
        self.assertEqual((proxy.config_dir(self.home) / "config.yaml").read_bytes(), before)
        gi.end(self.state_root, token)

    def test_shims_and_configuration_are_written_inside_the_fence(self) -> None:
        from claude_multi import scope

        attempts: list[str] = []
        real_shim = scope.ensure_hook_shim

        def shim_racing_a_begin(*args, **kwargs):
            try:
                gi.begin(self.state_root, owner="cutover", purpose="move", phase="freeze",
                         remedy="sh cutover.sh --recover", lock_wait=0.2)
            except gi.InhibitionError as exc:
                attempts.append(str(exc))
            return real_shim(*args, **kwargs)

        with mock.patch.object(scope, "ensure_hook_shim", shim_racing_a_begin):
            runtime = self.runtime()
        self.assertFalse(runtime.inhibited_shims)
        self.assertEqual(len(attempts), 1)
        self.assertIn("configuration or shims is in progress", attempts[0])
        self.assertIsNone(gi.read(self.state_root))
        real_render = proxy.render_runtime_config

        def render_racing_a_begin(*args, **kwargs):
            try:
                gi.begin(self.state_root, owner="cutover", purpose="move", phase="freeze",
                         remedy="sh cutover.sh --recover", lock_wait=0.2)
            except gi.InhibitionError as exc:
                attempts.append(str(exc))
            return real_render(*args, **kwargs)

        with mock.patch.object(proxy, "render_runtime_config", render_racing_a_begin):
            runtime.render_gateway()
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(gi.read(self.state_root))


class ReadOnlyWithoutTheStartLockTests(_Inhibited):
    """A transaction owner holding the start lock never turns reads into "busy"."""

    def test_a_ready_gateway_is_delivered_and_changes_report_the_owner_at_once(self) -> None:
        self.running()
        lock = state.FileLock(self.workdir / "gateway-start")
        self.assertTrue(lock.acquire(blocking=False))
        try:
            gi.begin(self.state_root, owner=gl.SERVICE_OWNER, purpose="gateway service refresh (claude-multi-gateway)",
                     phase="refresh", remedy=gl.SERVICE_REMEDY, expiry=gi.owner_process(os.getppid()),
                     now=self.world.now, held_lock=True)
            started = self.world.clock()
            outcome = self.gateway().ensure(max_wait=1)
            self.assertEqual(outcome.status, "ready", outcome.lines())
            code, out, err = self.run_cli("gateway", "ensure", "--quiet", "--max-wait", "1")
            self.assertEqual((code, out, err), (0, "", ""))
            # A session compiled for another endpoint still gets nothing.
            outcome = self.gateway().ensure(max_wait=1, destination="http://127.0.0.1:1")
            self.assertEqual(outcome.status, "refused")
            for change in (self.gateway().stop(), self.gateway().restart(),
                           self.gateway().ensure(max_wait=1, explicit=True, choose_port=True),
                           self.service().install()):
                self.assertEqual(change.status, "inhibited", change.lines())
                self.assertIn("inhibited by service (gateway service refresh", change.message)
            self.assertLess(self.world.clock() - started, gl.START_LOCK_WAIT)  # nothing waited for the lock
            # A gateway that is not proven ours is never delivered.
            self.world.listener = "unknown"
            self.assertEqual(self.gateway().ensure(max_wait=1).status, "inhibited")
        finally:
            lock.release()
        self.assertEqual(self.world.spawned, [])
        self.assertEqual(self.world.terminated, [])


class RefreshTransactionTests(_Inhibited):
    """A service refresh is a durable hand-off of its own."""

    def test_an_interrupted_refresh_keeps_its_inhibition_until_it_is_finished(self) -> None:
        self.assertTrue(self.service().install().ok)
        self.notifier = "/usr/bin/notify-send"  # the refresh adds the failure notice unit
        seen: list = []
        real_write = test_gateway_service.gs.GatewayService._write_files

        def write_then_interrupt(manager, files, undo):
            seen.append(gi.read(self.state_root))
            real_write(manager, files, undo)
            raise KeyboardInterrupt

        before = self.unit_text()
        with mock.patch.object(test_gateway_service.gs.GatewayService, "_write_files", write_then_interrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.service().install()
        self.assertEqual((seen[0].owner, seen[0].phase), (gl.SERVICE_OWNER, "refresh"))  # recorded before the write
        self.assertNotEqual(self.unit_text(), before)
        record = gi.read(self.state_root)
        self.assertEqual((record.owner, record.phase), (gl.SERVICE_OWNER, "refresh"))  # kept
        self.manager.active.clear()
        self.world.lock, self.world.alive, self.world.listener = False, False, "none"
        self.assertEqual(self.gateway().ensure(max_wait=1).status, "inhibited")
        self.assertEqual(self.world.spawned, [])
        # The next install finishes it (the manager reloads what was written).
        self.manager.calls.clear()
        outcome = self.service().install()
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertIn("an interrupted refresh was finished", outcome.message)
        self.assertIn("daemon-reload", self.manager.verbs())
        self.assertIsNone(gi.read(self.state_root))

    def test_a_restoration_the_manager_did_not_reload_keeps_it(self) -> None:
        self.assertTrue(self.service().install().ok)
        self.notifier = "/usr/bin/notify-send"
        self.manager.reload_ok = False  # the refresh fails, and so does the restoring reload
        before = self.unit_text()
        self.manager.calls.clear()
        outcome = self.service().install()
        self.assertEqual(outcome.status, "failed", outcome.lines())
        self.assertEqual(self.manager.verbs().count("daemon-reload"), 2)
        self.assertEqual(self.unit_text(), before)  # the files are back, but the manager never reloaded them
        self.assertIn("its restoration is not finished", outcome.message)
        self.assertIn("the service manager did not reload the restored unit files", "\n".join(outcome.notes))
        self.assertEqual(outcome.remedy, "run claude-multi gateway service install again")
        record = gi.read(self.state_root)
        self.assertEqual((record.owner, record.phase), (gl.SERVICE_OWNER, "refresh"))  # kept
        self.manager.active.clear()
        self.world.lock, self.world.alive, self.world.listener = False, False, "none"
        self.assertEqual(self.gateway().ensure(max_wait=1).status, "inhibited")
        self.assertEqual(self.world.spawned, [])
        # The next install finishes it once the manager reloads.
        self.manager.reload_ok = True
        outcome = self.service().install()
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertIn("an interrupted refresh was finished", outcome.message)
        self.assertIsNone(gi.read(self.state_root))

    def test_a_completed_or_completely_restored_refresh_removes_it(self) -> None:
        self.assertTrue(self.service().install().ok)
        self.notifier = "/usr/bin/notify-send"
        self.manager.reload_outcomes = [False, True]  # the refresh fails; its restoration is reloaded
        before = self.unit_text()
        self.manager.calls.clear()
        outcome = self.service().install()
        self.assertEqual(outcome.status, "failed", outcome.lines())
        self.assertEqual(self.manager.verbs().count("daemon-reload"), 2)
        self.assertEqual(self.unit_text(), before)
        self.assertNotIn("not finished", outcome.message)
        self.assertIsNone(gi.read(self.state_root))
        outcome = self.service().install()
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertIn("refreshed", outcome.message)
        self.assertIsNone(gi.read(self.state_root))
        # Run by an outer owner, the refresh works under the owner's record and leaves it.
        token = self.inhibit()
        self.notifier = None
        code, _out, err = self.run_cli("gateway", "service", "install", **{gi.TOKEN_ENV: token})
        self.assertEqual(code, 0, err)
        self.assertEqual(gi.read(self.state_root).token, token)


class InterruptedUninstallTests(_Inhibited):
    def test_rerunning_uninstall_finishes_one_interrupted_after_the_backend_switch(self) -> None:
        self.assertTrue(self.service().install().ok)
        self.assertTrue(self.service().uninstall().ok)
        self.assertEqual(self.recorded().backend, endpoint.ON_DEMAND)
        # As a process killed right after it recorded the on-demand backend
        # leaves it: the service gone, its record behind.
        gi.begin(self.state_root, owner=gl.SERVICE_OWNER, purpose="gateway service uninstall (claude-multi-gateway)",
                 phase="uninstall", remedy=gl.SERVICE_REMEDY, expiry=gi.owner_process(os.getppid()),
                 now=self.world.now - stale_after() * 2)
        self.assertEqual(self.gateway().stop().status, "inhibited")
        outcome = self.service().uninstall()
        self.assertEqual(outcome.status, "stopped", outcome.lines())
        self.assertIn("an interrupted uninstall was finished", outcome.message)
        self.assertIsNone(gi.read(self.state_root))
        self.assertNotEqual(self.gateway().stop().status, "inhibited")
        # Another owner's record, or a live hand-off, is left alone.
        token = self.inhibit(pid=0, at=self.world.now - datetime.timedelta(days=2))
        self.assertEqual(self.service().uninstall().message, "the gateway service is not installed; nothing was changed")
        self.assertEqual(gi.read(self.state_root).token, token)
        gi.end(self.state_root, token)
        live = gi.begin(self.state_root, owner=gl.SERVICE_OWNER, purpose="gateway service uninstall (claude-multi-gateway)",
                        phase="uninstall", remedy=gl.SERVICE_REMEDY, expiry=gi.owner_process(os.getppid()),
                        now=self.world.now)
        self.assertEqual(self.service().uninstall().status, "inhibited")
        self.assertEqual(gi.read(self.state_root).token, live)

    def test_a_restored_unit_file_is_removed_and_a_running_unit_refuses(self) -> None:
        self.assertTrue(self.service().install().ok)
        text = self.unit_text()
        self.assertTrue(self.service().uninstall().ok)
        (self.unit_dir / "claude-multi-gateway.service").write_text(text)  # a restoration interrupted midway
        gi.begin(self.state_root, owner=gl.SERVICE_OWNER, purpose="gateway service uninstall (claude-multi-gateway)",
                 phase="uninstall", remedy=gl.SERVICE_REMEDY, expiry=gi.owner_process(os.getppid()),
                 now=self.world.now - stale_after() * 2)
        self.manager.active.add("claude-multi-gateway")
        outcome = self.service().uninstall()
        self.assertEqual(outcome.status, "refused", outcome.lines())
        self.assertIn("still runs", outcome.message)
        self.assertIsNotNone(gi.read(self.state_root))
        self.manager.active.clear()
        outcome = self.service().uninstall()
        self.assertEqual(outcome.status, "stopped", outcome.lines())
        self.assertFalse((self.unit_dir / "claude-multi-gateway.service").exists())
        self.assertIsNone(gi.read(self.state_root))


    def test_a_failed_reload_keeps_it_until_a_later_reload_succeeds(self) -> None:
        unit = "claude-multi-gateway"
        self.assertTrue(self.service().install().ok)
        text = self.unit_text()
        self.assertTrue(self.service().uninstall().ok)
        # Interrupted after it recorded the on-demand backend: its unit file
        # still there and loaded by the manager.
        (self.unit_dir / f"{unit}.service").write_text(text)
        self.manager.loaded.add(unit)
        gi.begin(self.state_root, owner=gl.SERVICE_OWNER, purpose=f"gateway service uninstall ({unit})",
                 phase="uninstall", remedy=gl.SERVICE_REMEDY, expiry=gi.owner_process(os.getppid()),
                 now=self.world.now - stale_after() * 2)
        self.manager.reload_ok = False
        self.manager.calls.clear()
        first = self.service().uninstall()
        self.assertEqual(first.status, "failed", first.lines())
        self.assertFalse((self.unit_dir / f"{unit}.service").exists())  # the files are gone
        self.assertEqual(self.manager.verbs().count("daemon-reload"), 1)
        self.assertIn(unit, self.manager.loaded)  # the manager still holds the old unit
        self.assertEqual(gi.read(self.state_root).phase, "uninstall")  # kept
        # A retry with no file left to remove still reloads the manager, and
        # keeps the record while that fails.
        self.manager.calls.clear()
        second = self.service().uninstall()
        self.assertEqual(second.status, "failed", second.lines())
        self.assertIn("the interrupted uninstall is not finished", second.message)
        self.assertEqual(self.manager.verbs().count("daemon-reload"), 1)
        self.assertEqual(gi.read(self.state_root).phase, "uninstall")
        self.assertEqual(self.gateway().stop().status, "inhibited")
        # The record ends only once the manager reloaded.
        self.manager.reload_ok = True
        self.manager.calls.clear()
        third = self.service().uninstall()
        self.assertEqual(third.status, "stopped", third.lines())
        self.assertIn("an interrupted uninstall was finished", third.message)
        self.assertEqual(self.manager.verbs().count("daemon-reload"), 1)
        self.assertNotIn(unit, self.manager.loaded)
        self.assertIsNone(gi.read(self.state_root))


class TwoRootsTests(_Entry):
    """One home, two state roots: the managed root's owner fences both."""

    def test_another_state_root_cannot_change_the_managed_roots_gateway(self) -> None:
        from claude_multi import continuity, installs

        self.assertEqual(self.proxy("init")[0], 0)
        self.assertEqual(continuity.read(self.home)["state_root"], str(self.state_root))
        token = self.inhibit()
        before = self.config_files()
        other = self.root / "other" / "claude-multi"
        state.ensure_private_dir(other)
        for argv, extra in ((["init", "--state-root", str(other)], {}),
                            (["init"], {"XDG_STATE_HOME": str(self.root / "other")}),
                            (["init", "--state-root", str(other), "--adopt-root"], {}),
                            (["run", "--prepare-and-exec", "--state-root", str(other)], {}),
                            (["claude-login", "--state-root", str(other)], {})):
            with self.subTest(argv=argv, extra=extra):
                code, _out, err, execs = self.proxy(*argv, **extra)
                self.assertEqual((code, execs), (1, []), err)
                self.assertIn("inhibited by cutover", err)
        self.assertEqual(self.config_files(), before)
        self.assertEqual(continuity.read(self.home)["state_root"], str(self.state_root))
        self.assertIsNone(gi.read(other))
        # The owner passes from either root.
        self.assertEqual(self.proxy("init", "--state-root", str(other), **{gi.TOKEN_ENV: token})[0], 0)
        gi.end(self.state_root, token)
        # The managed root's channel is checked too.
        installs.write_marker(self.state_root, "nix")
        code, _out, err, _execs = self.proxy("init", "--state-root", str(other), CLAUDE_MULTI_CHANNEL="bundle")
        self.assertEqual(code, 1)
        self.assertIn("belongs to the nix installation", err)

    def test_a_writer_paused_across_a_root_adoption_writes_nothing(self) -> None:
        from claude_multi import continuity
        from claude_multi.cli import consent

        self.assertEqual(self.proxy("init")[0], 0)
        self.assertEqual(continuity.read(self.home)["state_root"], str(self.state_root))
        adopted = self.root / "adopted" / "claude-multi"
        state.ensure_private_dir(adopted)
        seen: dict = {}
        real_summary = proxy.pending_served_summary

        def pause(*args, **kwargs):
            # The writer for the managed root passed its entry check (its fence
            # is held) and pauses before it renders: meanwhile the home is
            # adopted to another root, whose transaction owner then begins.
            if not seen:
                seen["paused"] = True
                with mock.patch.object(consent, "require_human"), mock.patch("sys.stdin", io.StringIO("y\n")):
                    seen["adoption"] = self.proxy("init", "--state-root", str(adopted), "--adopt-root")
                seen["token"] = gi.begin(adopted, owner="cutover", purpose="move to the bundle", phase="freeze",
                                         remedy="sh cutover.sh --recover", expiry=gi.deadline(3600))
                seen["files"] = self.config_files()
            return real_summary(*args, **kwargs)

        with mock.patch.object(proxy, "pending_served_summary", pause):
            code, _out, err, _execs = self.proxy("init")
        self.assertEqual(seen["adoption"][0], 0, seen["adoption"][2])
        self.assertEqual(continuity.read(self.home)["state_root"], str(adopted))
        self.assertEqual(code, 1, err)
        self.assertIn(gi.AUTHORITY_CHANGED, err)
        self.assertEqual(self.config_files(), seen["files"])  # config.yaml (and every key) unchanged
        self.assertEqual(gi.read(adopted).token, seen["token"])  # the new root's inhibition stays
        # A writer that starts now fences the adopted root and is refused by its owner.
        code, _out, err, _execs = self.proxy("init")
        self.assertEqual(code, 1)
        self.assertIn("inhibited by cutover", err)
        self.assertEqual(self.config_files(), seen["files"])

    def test_the_authority_is_read_again_under_the_fence_and_at_the_write(self) -> None:
        from claude_multi import continuity

        self.assertEqual(self.proxy("init")[0], 0)
        roots = gi.home_roots(self.home, self.state_root)
        self.assertEqual(roots, (self.state_root,))
        adopted = self.root / "adopted" / "claude-multi"
        document = continuity.read(self.home)
        document["state_root"] = str(adopted)
        continuity.write(self.home, document)  # adopted after the roots were resolved
        with self.assertRaises(gi.AuthorityChanged) as raised, gi.fenced(roots, home=self.home):
            self.fail("a writer entered with a stale authority")
        self.assertEqual(str(raised.exception), f"{gi.AUTHORITY_CHANGED} (nothing was changed)")
        self.assertIn("run the command again", raised.exception.remedy)  # every surface shows a fix line
        with gi.fenced(gi.home_roots(self.home, self.state_root), home=self.home):
            gi.revalidate(self.home)  # both roots fenced
            with gi.fenced(roots):
                gi.revalidate(self.home)  # an inner fence keeps the outer roots
        with gi.fenced(roots):
            with self.assertRaises(gi.AuthorityChanged):
                gi.revalidate(self.home)  # the write-time check
        gi.revalidate(self.home)  # a flow holding no fence is not checked

    def test_an_unreadable_authority_refuses_every_change_and_reading_stays(self) -> None:
        from claude_multi import continuity

        self.assertEqual(self.proxy("init")[0], 0)
        self.inhibit()  # the managed root's transaction owner
        other = self.root / "other" / "claude-multi"
        state.ensure_private_dir(other)
        state.atomic_write(continuity.path(self.home), b'{"version": 1, "state_root": ')
        before = self.config_files()
        for argv, extra in ((["init", "--state-root", str(other)], {}),
                            (["init"], {"XDG_STATE_HOME": str(self.root / "other")}),
                            (["run", "--prepare-and-exec", "--state-root", str(other)], {}),
                            (["claude-login", "--state-root", str(other)], {}),
                            (["rotate-management-key"], {"XDG_STATE_HOME": str(self.root / "other")})):
            with self.subTest(argv=argv, extra=extra):
                code, _out, err, execs = self.proxy(*argv, **extra)
                self.assertEqual((code, execs), (1, []), err)
                self.assertIn("gateway continuity set unreadable (continuity.json:", err)
                self.assertIn("repair or remove ~/.config/claude-multi/continuity.json", err)
        self.assertEqual(self.config_files(), before)
        # The launcher's configuration and credential writes from the other
        # root refuse the same way.
        runtime = self.runtime(XDG_STATE_HOME=str(self.root / "other"))
        self.assertEqual(runtime.session_store.root, other)
        self.assertEqual(runtime.shims_withheld, "authority")  # the shims are left alone
        self.assertFalse((other / "bin").exists())
        with self.assertRaises(gi.AuthorityUnreadable):
            runtime.render_gateway()
        with self.assertRaises(gi.AuthorityUnreadable):
            proxy.rotate_token(self.home, environ=self.environ(), live_sessions=lambda: (), state_root=other)
        self.assertEqual(self.config_files(), before)
        # Reading stays available, and a gateway proven ours and ready is still delivered.
        self.assertEqual(self.proxy("status")[0], 0)
        code, out, err, _execs = self.proxy("snapshot-auth", XDG_STATE_HOME=str(self.root / "other"))
        self.assertEqual(code, 0, err)
        self.assertIn("snapshot:", out)
        self.running()
        self.assertEqual(self.gateway().ensure(max_wait=1).status, "ready")
        self.assertEqual(self.world.spawned, [])


class ForeignChannelShimTests(_Entry):
    """Hooks and other entry points of another channel never repoint the shared shims."""

    def test_hooks_of_another_channel_leave_every_shim_alone(self) -> None:
        from claude_multi import installs

        old, new = str(self.root / "old" / "claude-multi"), str(self.root / "new" / "claude-multi")
        installs.write_marker(self.state_root, "nix")
        with self.process_environ(old, CLAUDE_MULTI_CHANNEL="nix"):
            self.assertIsNone(cli.Runtime(asset_root=FIXTURE_ROOT).shims_withheld)
        before = self.shims()
        self.assertEqual(len(before), 3)
        runtime_id = "0b5e7f4e-1d2c-4b3a-8a9b-0c1d2e3f4a5b"
        for marker in (b"nix\n", b"garbage\n"):
            state.atomic_write(installs.marker_path(self.state_root), marker)
            for channel in ("bundle", "nix") if marker != b"nix\n" else ("bundle",):
                with self.subTest(marker=marker, channel=channel), \
                        self.process_environ(new, CLAUDE_MULTI_CHANNEL=channel), redirect_stderr(io.StringIO()):
                    cli.main(["session-event", "end", "--managed-id", runtime_id], input_stream=io.StringIO("{}"),
                             output_stream=io.StringIO(), interactive=False)
                    self.assertEqual(cli.Runtime(asset_root=FIXTURE_ROOT).shims_withheld, "channel")
                    self.assertEqual(self.shims(), before)
        # The owning channel refreshes them.
        state.atomic_write(installs.marker_path(self.state_root), b"nix\n")
        with self.process_environ(new, CLAUDE_MULTI_CHANNEL="nix"):
            self.assertIsNone(cli.Runtime(asset_root=FIXTURE_ROOT).shims_withheld)
        self.assertTrue(any(new.encode() in raw for raw in self.shims().values()))


class RotationPhaseTests(test_continuity.ContinuityCase):
    """Each write phase of a token rotation re-checks the inhibition."""

    def test_a_begin_during_the_rotation_pauses_its_next_phase(self) -> None:
        import re

        state.ensure_private_dir(self.config_dir)
        state.atomic_write(self.config_dir / "api-key", (("a" * 64) + "\n").encode())

        def getter(_base, token):
            yaml = self.config.read_text()
            keys = re.findall(r'"([0-9a-f]{64})"', yaml.split("api-keys:\n", 1)[1].split("debug:", 1)[0])
            ids = set(re.findall(r'alias: "(claude-multi-render-[0-9a-f]+)"', yaml))
            return (200, ids) if token in keys else (401, set())

        clock, begun = [0.0], []

        def sleep(seconds: float) -> None:  # the wait between phases: an owner begins
            clock[0] += seconds
            if not begun:
                begun.append(gi.begin(self.state_root, **OWNER))

        with self.assertRaises(gi.Inhibited) as raised:
            proxy.rotate_token(self.home, environ=self.environ, resolver=lambda _: None, models_get=getter,
                               clock=lambda: clock[0], sleep=sleep, live_sessions=lambda: (), state_root=self.state_root)
        self.assertEqual(len(begun), 1)
        self.assertIn("rotation paused; rerun doctor --rotate-token", str(raised.exception))
        self.assertTrue((self.config_dir / "previous-key").exists())  # the dual-key state stays recoverable

    def test_a_root_adopted_during_the_rotation_fences_its_next_phase(self) -> None:
        import re

        from claude_multi import continuity

        state.ensure_private_dir(self.config_dir)
        state.atomic_write(self.config_dir / "api-key", (("a" * 64) + "\n").encode())

        def getter(_base, token):
            yaml = self.config.read_text()
            keys = re.findall(r'"([0-9a-f]{64})"', yaml.split("api-keys:\n", 1)[1].split("debug:", 1)[0])
            ids = set(re.findall(r'alias: "(claude-multi-render-[0-9a-f]+)"', yaml))
            return (200, ids) if token in keys else (401, set())

        adopted = self.root / "adopted" / "claude-multi"
        clock, seen = [0.0], {}

        def sleep(seconds: float) -> None:  # the wait between phases: the home is adopted, its owner begins
            clock[0] += seconds
            if not seen:
                lock = state.FileLock(self.config_dir / "api-key")  # as the adoption writes it
                with lock:
                    document = continuity.read(self.home)
                    self.assertEqual(document["state_root"], str(self.state_root))
                    document["state_root"] = str(adopted)
                    continuity.write(self.home, document)
                seen["token"] = gi.begin(adopted, **OWNER)
                seen["files"] = {name: (self.config_dir / name).read_bytes()
                                 for name in ("config.yaml", "api-key", "previous-key")}

        with self.assertRaises(gi.Inhibited) as raised:
            proxy.rotate_token(self.home, environ=self.environ, resolver=lambda _: None, models_get=getter,
                               clock=lambda: clock[0], sleep=sleep, live_sessions=lambda: (), state_root=self.state_root)
        self.assertIn("rotation paused; rerun doctor --rotate-token", str(raised.exception))
        self.assertIn("inhibited by installer", str(raised.exception))
        self.assertEqual({name: (self.config_dir / name).read_bytes() for name in seen["files"]}, seen["files"])
        self.assertEqual(gi.read(adopted).token, seen["token"])


class FreshHomeUnitDirectiveTests(test_cli.OperatorCommandCase):
    """The unit's own directives get no exception from the fresh-home rule."""

    def test_the_units_directives_never_provision_the_packaged_port(self) -> None:
        (self.runtime.home / ".config/claude-multi/api-key").unlink()
        self.assertFalse(endpoint.set_up(self.runtime.home))
        asked: list = []
        for argv in (["init", "--reload-check"], ["init", "--prepare-start"], ["run", "--prepared"]):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()):
                    code = proxy.main(argv, environ=self.runtime.gateway_environ(),
                                      execve=mock.Mock(side_effect=AssertionError("exec")),
                                      models_get=lambda *a: asked.append(a) or (200, set()),
                                      listener_observer=lambda *a: asked.append(a) or proxy.service.OwnerVerdict(
                                          "none", "fixture"),
                                      pid_get=lambda **_k: (None, False))
                self.assertEqual(code, 1)
                self.assertIn("not set up", err.getvalue())
        self.assertEqual(asked, [])
        directory = self.runtime.home / ".config/claude-multi"
        self.assertEqual([name for name in ("config.yaml", "api-key", "endpoint.json") if (directory / name).exists()],
                         [])


if __name__ == "__main__":
    unittest.main()
