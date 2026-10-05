"""The installer and updater safety transaction (``claude_multi.install_txn``)
and the installer's receipt (``claude_multi.install_receipt``)."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import gateway_inhibition, install_receipt, install_txn, installs, service, state
import test_inhibition  # module import: no test classes re-exported


class TxnTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-txn-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir(mode=0o700)
        self.environ = {"HOME": str(self.home)}
        self.state = self.home / ".local/state/claude-multi"
        self.state.mkdir(parents=True, mode=0o700)
        self.install = self.home / ".local/share/claude-multi/install"
        (self.install / "versions").mkdir(parents=True, mode=0o700)
        os.chmod(self.install, 0o700)

    def marker(self, data: bytes, mode: int = 0o600) -> None:
        target = self.state / "channel"
        if target.exists() or target.is_symlink():
            target.unlink()
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        os.write(fd, data)
        os.close(fd)
        os.chmod(target, mode)


class OwnershipTests(TxnTestCase):
    def test_owner_rules(self) -> None:
        self.assertIsNone(install_txn.check_ownership(self.state, self.environ))
        self.marker(b"bundle\n")
        self.assertEqual(install_txn.check_ownership(self.state, self.environ), "bundle")
        self.marker(b"nix\n")
        with self.assertRaises(install_txn.TransactionError) as caught:
            install_txn.check_ownership(self.state, self.environ)
        self.assertIn("belongs to the nix installation; nothing was changed", str(caught.exception))
        self.assertIn("--migrate-from-nix", caught.exception.remedy)
        self.assertEqual(install_txn.check_ownership(self.state, self.environ, migrate=True), "nix")

    def test_malformed_markers_refuse_even_a_migration(self) -> None:
        for data, mode in ((b"", 0o600), (b"bundle", 0o600), (b"bundle\nnix\n", 0o600), (b"other\n", 0o600),
                           (b"bundle\n", 0o644), (b"bundle\n", 0o660)):
            with self.subTest(data=data, mode=oct(mode)):
                self.marker(data, mode)
                for migrate in (False, True):
                    with self.assertRaisesRegex(install_txn.TransactionError, "nothing was changed"):
                        install_txn.check_ownership(self.state, self.environ, migrate=migrate)
                    with self.assertRaises(install_txn.TransactionError):
                        install_txn.claim(self.state, self.environ, migrate=migrate)
                self.assertEqual((self.state / "channel").read_bytes(), data)
        (self.state / "channel").unlink()
        (self.state / "channel").symlink_to(self.tmp / "target")
        (self.tmp / "target").write_bytes(b"bundle\n")
        with self.assertRaisesRegex(install_txn.TransactionError, "symlink"):
            install_txn.check_ownership(self.state, self.environ, migrate=True)

    def test_a_foreign_owned_marker_refuses(self) -> None:
        self.marker(b"bundle\n")
        with mock.patch("claude_multi.platform.posix_fs.owned_by_caller", return_value=False):
            with self.assertRaisesRegex(install_txn.TransactionError, "owner"):
                install_txn.check_ownership(self.state, self.environ)

    def test_claim_writes_the_runtime_format(self) -> None:
        self.assertIsNone(install_txn.claim(self.state, self.environ))
        self.assertEqual((self.state / "channel").read_bytes(), b"bundle\n")
        self.assertEqual(os.stat(self.state / "channel").st_mode & 0o777, 0o600)
        self.assertEqual(installs.read_marker(self.state, self.environ), "bundle")
        self.marker(b"source\n")
        with self.assertRaises(install_txn.TransactionError):
            install_txn.claim(self.state, self.environ)
        self.assertEqual(install_txn.claim(self.state, self.environ, migrate=True), "source")
        self.assertEqual(installs.read_marker(self.state, self.environ), "bundle")

    def test_state_format(self) -> None:
        self.assertEqual(install_txn.check_state(self.state, 4, "1.0.0"), 3)
        fd = os.open(self.state / "state-version", os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"5\n")
        os.close(fd)
        with self.assertRaisesRegex(install_txn.TransactionError, "1.0.0 cannot read your state"):
            install_txn.check_state(self.state, 4, "1.0.0")
        with self.assertRaises(install_txn.TransactionError) as caught:
            install_txn.check_state(self.state, 4, "1.0.0", rollback_from="1.1.0")
        self.assertIn("cannot roll back to 1.0.0", str(caught.exception))
        self.assertIn("stay on 1.1.0", caught.exception.remedy)


DEAD_PID = 2 ** 22 + 1  # no such process: an interrupted owner


def legacy_handoff(state_root: Path, *, pid: int) -> None:
    """An earlier release's service hand-off record (``service-handoff.json``)."""

    service.ensure_gateway_workdir(state_root)
    document = {"version": 1, "operation": "install", "unit": "claude-multi-gateway", "pid": pid,
                "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "token": "c" * 32}
    state.atomic_write(gateway_inhibition.legacy_path(state_root), json.dumps(document).encode())


class InhibitionTests(TxnTestCase):
    """The installer's and the updater's inhibition is the runtime's one record."""

    def test_begin_advance_and_end(self) -> None:
        held = install_txn.begin(self.state, owner="installer", purpose="install claude-multi 1.0.1")
        record = gateway_inhibition.read(self.state)
        self.assertEqual((record.owner, record.phase, record.remedy), ("installer", "prepare", "sh install.sh --repair"))
        self.assertEqual((record.expiry.policy, record.expiry.pid), ("owner-process", os.getpid()))
        self.assertEqual(record.token, held.token)
        with self.assertRaises(install_txn.TransactionError) as caught:
            install_txn.guard(self.state)
        self.assertIn("the gateway is inhibited by installer (install claude-multi 1.0.1; phase prepare",
                      str(caught.exception))
        self.assertEqual(caught.exception.remedy.split(": ")[-1], "sh install.sh --repair")
        install_txn.guard(self.state, token=held.token)
        self.assertTrue(held.restorable)
        for phase in ("download", "pin"):
            held.advance(phase)
            self.assertTrue(held.restorable)
        held.advance("replace")
        self.assertFalse(held.restorable)  # from here on an interruption keeps the record
        self.assertEqual(gateway_inhibition.read(self.state).phase, "replace")
        held.end()
        self.assertIsNone(gateway_inhibition.read(self.state))
        held.end()  # ending twice is fine
        with self.assertRaisesRegex(install_txn.TransactionError, "no longer holds"):
            held.advance("switch")

    def test_one_owner_at_a_time_and_recovery_is_explicit(self) -> None:
        # Owned by a live process (the test runner's parent stands in for it).
        held = install_txn.begin(self.state, owner="updater", purpose="update claude-multi 1.0.0 to 1.0.1",
                                 pid=os.getppid())
        self.assertIn("claude-multi update", gateway_inhibition.read(self.state).remedy)
        with self.assertRaisesRegex(install_txn.TransactionError, "inhibited by updater"):
            install_txn.begin(self.state, owner="installer", purpose="install")
        with self.assertRaisesRegex(install_txn.TransactionError, "still in progress"):
            install_txn.recover(self.state, "updater")  # its owner still runs it
        held.end()
        # An interrupted run (its owner process gone) still refuses a begin of
        # the same owner: only an explicit recovery takes it back.
        install_txn.begin(self.state, owner="installer", purpose="install claude-multi 1.0.1", pid=DEAD_PID)
        gateway_inhibition.advance(self.state, gateway_inhibition.read(self.state).token, "switch")
        with self.assertRaisesRegex(install_txn.TransactionError, "did not finish"):
            install_txn.begin(self.state, owner="installer", purpose="install claude-multi 1.0.2")
        with self.assertRaisesRegex(install_txn.TransactionError, "did not finish"):
            install_txn.guard(self.state)
        install_txn.guard(self.state, recoverable="installer")  # the repair's check before its lock
        with self.assertRaises(install_txn.TransactionError):
            install_txn.guard(self.state, recoverable="updater")
        with self.assertRaisesRegex(install_txn.TransactionError, "not updater's"):
            install_txn.recover(self.state, "updater")
        recovered = install_txn.recover(self.state, "installer")
        self.assertEqual((recovered.phase, recovered.recovered, recovered.restorable), ("switch", True, False))
        install_txn.guard(self.state, token=recovered.token)
        recovered.advance("launchers")
        recovered.end()
        self.assertIsNone(gateway_inhibition.read(self.state))
        self.assertIsNone(install_txn.recover(self.state, "installer"))  # idempotent: nothing to recover

    def test_an_earlier_releases_hand_off_record_still_blocks(self) -> None:
        for pid, label in ((os.getppid(), "in progress"), (DEAD_PID, "interrupted")):
            with self.subTest(label):
                legacy_handoff(self.state, pid=pid)
                with self.assertRaises(install_txn.TransactionError) as caught:
                    install_txn.guard(self.state)
                self.assertIn("the gateway is inhibited by service (gateway service install (claude-multi-gateway)",
                              str(caught.exception))
                self.assertIn("claude-multi gateway service install", caught.exception.remedy)
                with self.assertRaises(install_txn.TransactionError):
                    install_txn.guard(self.state, recoverable="installer")
                for owner in ("installer", "updater"):
                    with self.assertRaisesRegex(install_txn.TransactionError, "inhibited by service"):
                        install_txn.begin(self.state, owner=owner, purpose="install")
                with self.assertRaisesRegex(install_txn.TransactionError, "inhibited by service"):
                    install_txn.claim(self.state, self.environ)
                self.assertFalse((self.state / "channel").exists())
                # A begin migrates it into the current format (the runtime's
                # rule for every record writer); it keeps refusing as before.
                self.assertEqual(gateway_inhibition.read(self.state).owner, "service")
                gateway_inhibition.record_path(self.state).unlink(missing_ok=True)
                gateway_inhibition.legacy_path(self.state).unlink(missing_ok=True)
        raw = gateway_inhibition.legacy_path(self.state)
        raw.write_text("{")  # unreadable: unknown is never free
        with self.assertRaisesRegex(install_txn.TransactionError, "unreadable record"):
            install_txn.guard(self.state)

    def test_the_marker_claim_is_fenced_for_the_owner(self) -> None:
        held = install_txn.begin(self.state, owner="installer", purpose="install claude-multi 1.0.1")
        with self.assertRaisesRegex(install_txn.TransactionError, "inhibited by installer"):
            install_txn.claim(self.state, self.environ)
        self.assertFalse((self.state / "channel").exists())
        self.assertIsNone(install_txn.claim(self.state, self.environ, token=held.token))
        self.assertEqual((self.state / "channel").read_bytes(), b"bundle\n")
        with held.fenced():
            pass
        other = install_txn.Inhibition(self.state, "installer", "0" * 32)
        with self.assertRaisesRegex(install_txn.TransactionError, "inhibited by installer"), other.fenced():
            pass  # pragma: no cover
        held.end()


class _Competing(test_inhibition._Inhibited):
    """The service harness (stub manager, fake gateway world)."""

    def assert_inhibited(self, outcome, owner: str, remedy: str) -> None:
        self.assertEqual(outcome.status, "inhibited", outcome.lines())
        self.assertEqual(outcome.exit_code, 1)
        self.assertIn(f"the gateway is inhibited by {owner} (", outcome.message)
        self.assertIn(remedy, outcome.remedy)


class CompetingChangeTests(_Competing):
    """While an installer or updater holds its inhibition, a competing
    automatic or explicit start and a service install get the inhibited
    outcome before any effect; an interrupted run keeps refusing them."""

    def test_competing_starts_and_service_installs_are_inhibited(self) -> None:
        for owner, remedy in (("installer", "sh install.sh --repair"), ("updater", "claude-multi update")):
            with self.subTest(owner):
                held = install_txn.begin(self.state_root, owner=owner, purpose=f"{owner} run")
                self.assert_inhibited(self.gateway().ensure(max_wait=1), owner, remedy)  # the token helper
                self.assert_inhibited(self.gateway().ensure(max_wait=1, explicit=True, choose_port=True), owner,
                                      remedy)
                self.assert_inhibited(self.service().install(), owner, remedy)
                self.assertEqual(self.world.spawned, [])
                self.assertFalse(self.unit_dir.exists())
                held.advance("switch")
                held.end()
        # An interrupted installer (its shell gone) still refuses them.
        install_txn.begin(self.state_root, owner="installer", purpose="install claude-multi 1.0.1", pid=DEAD_PID)
        outcome = self.gateway().ensure(max_wait=1)
        self.assert_inhibited(outcome, "installer", "sh install.sh --repair")
        self.assertIn("its owner did not finish (stale)", outcome.message)
        self.assert_inhibited(self.service().install(), "installer", "sh install.sh --repair")
        self.assertEqual(self.world.spawned, [])
        # The owner's own commands pass with its token (the explicit gateway
        # verbs take it from CLAUDE_MULTI_INHIBITION_TOKEN).
        recovered = install_txn.recover(self.state_root, "installer")
        gateway = self.gateway()
        gateway.inhibition_token = recovered.token
        outcome = gateway.ensure(max_wait=1, explicit=True)
        self.assertNotEqual(outcome.status, "inhibited", outcome.lines())
        recovered.end()


class ProtectedTests(TxnTestCase):
    def stamp(self, pid: int, binary: Path) -> None:
        workdir = service.ensure_gateway_workdir(self.state)
        service.write_exec_stamp(workdir, service.ExecStamp(
            "1.0.0", "sig", pid, os.path.realpath(binary), service.utc_now(),
            pid_namespace=os.readlink("/proc/self/ns/pid") if os.path.exists("/proc/self/ns/pid") else None,
            instance="0123456789abcdef"))

    def test_nothing_running(self) -> None:
        self.assertEqual(install_txn.protected_versions(self.install, self.state), frozenset())

    def test_a_held_instance_lock_without_a_start_record_is_unknown(self) -> None:
        lock = service.instance_lock_path(self.state)
        service.ensure_gateway_workdir(self.state)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], pass_fds=(fd,))
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        os.close(os.dup(fd))
        self.assertIsNone(install_txn.protected_versions(self.install, self.state))

    @unittest.skipUnless(Path("/proc/self/exe").exists() and Path("/usr/bin/sleep").is_file(),
                         "BOUNDARY: needs /proc and /usr/bin/sleep")
    def test_a_running_gateway_protects_its_version(self) -> None:
        binary = self.install / "versions/1.0.0/libexec/claude-multi/cli-proxy-api"
        binary.parent.mkdir(parents=True)
        shutil.copy2("/usr/bin/sleep", binary)
        process = subprocess.Popen([str(binary), "30"])
        self.addCleanup(lambda: (process.kill(), process.wait()))
        self.stamp(process.pid, binary)
        self.assertEqual(install_txn.protected_versions(self.install, self.state), frozenset({"1.0.0"}))
        self.assertEqual(install_txn.version_of(binary, self.install), "1.0.0")
        process.kill()
        process.wait()
        self.assertEqual(install_txn.protected_versions(self.install, self.state), frozenset())

    def test_a_gateway_running_from_elsewhere_protects_nothing_here(self) -> None:
        self.stamp(os.getpid(), Path(sys.executable))
        self.assertEqual(install_txn.protected_versions(self.install, self.state), frozenset())
        self.assertIsNone(install_txn.version_of(sys.executable, self.install))

    def test_an_unreadable_start_record_is_unknown(self) -> None:
        workdir = service.ensure_gateway_workdir(self.state)
        fd = os.open(workdir / service.EXEC_STAMP, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"not json")
        os.close(fd)
        self.assertIsNone(install_txn.protected_versions(self.install, self.state))
        out, err = io.StringIO(), io.StringIO()
        code = install_txn.main(["protected", "--state-root", str(self.state), "--install-root", str(self.install)],
                                environ=self.environ, stdout=out, stderr=err)
        self.assertEqual(code, install_txn.EXIT_UNKNOWN)
        self.assertIn("every installed version is kept", err.getvalue())


class HoldTests(TxnTestCase):
    class Gateway:
        def __init__(self, state: str, held: bool = False, error: Exception | None = None) -> None:
            self.state, self.held, self.error = state, held, error

        def observe(self):
            if self.error is not None:
                raise self.error
            return types.SimpleNamespace(state=self.state)

        def persistence_hold(self, seen):
            return types.SimpleNamespace(held=self.held, reasons=("a credential save failed (ENOSPC)",))

    def test_hold_reasons(self) -> None:
        self.assertIsNone(install_txn.hold_reason(self.state, self.environ, gateway=self.Gateway("stopped", True)))
        self.assertIsNone(install_txn.hold_reason(self.state, self.environ, gateway=self.Gateway("ours")))
        reason = install_txn.hold_reason(self.state, self.environ, gateway=self.Gateway("ours", True))
        self.assertIn("persistence hold is active: a credential save failed (ENOSPC)", reason)
        self.assertIsNotNone(install_txn.hold_reason(self.state, self.environ, gateway=self.Gateway("unknown", True)))
        failing = self.Gateway("ours", error=OSError("no /proc"))
        self.assertIn("cannot be evaluated", install_txn.hold_reason(self.state, self.environ, gateway=failing))

    def test_the_packaged_gateway_reads_the_packaged_catalog(self) -> None:
        gateway = install_txn._packaged_gateway(self.state, self.environ)
        self.assertEqual(gateway.state_root, self.state)
        self.assertTrue(gateway.providers())


class CommandTests(TxnTestCase):
    def run_main(self, *argv: str, **environ: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = install_txn.main(list(argv), environ={**self.environ, **environ}, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_preflight_and_claim(self) -> None:
        roots = ("--state-root", str(self.state), "--install-root", str(self.install))
        self.assertEqual(self.run_main("preflight", *roots)[0], 0)
        self.marker(b"nix\n")
        code, _out, err = self.run_main("preflight", *roots)
        self.assertEqual(code, 1)
        self.assertIn("claude-multi installer: this account's claude-multi state", err)
        self.assertIn("  fix: ", err)
        self.assertEqual(self.run_main("preflight", *roots, "--migrate")[0], 0)
        code, out, _err = self.run_main("claim", "--state-root", str(self.state), "--migrate")
        self.assertEqual((code, out), (0, "nix\n"))
        self.assertEqual(self.run_main("preflight", "--state-root", "relative", "--install-root", "x")[0], 1)
        self.assertEqual(self.run_main("bogus")[0], 2)

    def test_preflight_lets_only_the_recovering_owner_pass(self) -> None:
        roots = ("--state-root", str(self.state), "--install-root", str(self.install))
        install_txn.begin(self.state, owner="installer", purpose="install claude-multi 1.0.1", pid=DEAD_PID)
        code, _out, err = self.run_main("preflight", *roots)
        self.assertEqual(code, 1)
        self.assertIn("inhibited by installer", err)
        self.assertIn("  fix: ", err)
        self.assertEqual(self.run_main("preflight", *roots, "--recoverable", "installer")[0], 0)
        self.assertEqual(self.run_main("preflight", *roots, "--recoverable", "updater")[0], 1)
        held = install_txn.recover(self.state, "installer")
        self.assertEqual(self.run_main("preflight", *roots, "--token-env",
                                       CLAUDE_MULTI_INHIBITION_TOKEN=held.token)[0], 0)
        code, out, _err = self.run_main("claim", "--state-root", str(self.state),
                                        CLAUDE_MULTI_INHIBITION_TOKEN=held.token)
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.run_main("begin", "--state-root", str(self.state))[0], 2)  # the inhibition's own command
        held.end()

    def test_receipt(self) -> None:
        launcher = self.home / ".local/bin/claude-multi"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n# claude-multi installer launcher\nexec x\n")
        profile = self.home / ".profile"
        line = install_receipt.PATH_LINE
        profile.write_text(f"{line}\n")
        code, _out, err = self.run_main("receipt", "--install-root", str(self.install), "--launcher", str(launcher),
                                        "--path-line", str(profile), line)
        self.assertEqual(code, 0, err)
        receipt = install_receipt.read(self.install)
        self.assertEqual(receipt.launchers[0].path, str(launcher))
        self.assertTrue(receipt.launchers[0].owned())
        self.assertEqual(receipt.path_lines[0].line, line)


class ReceiptTests(TxnTestCase):
    def document(self, **changes: object) -> dict:
        launcher = self.home / ".local/bin/claude-multi"
        base = {"format": 2,
                "launchers": [{"path": str(launcher), "sha256": "a" * 64}],
                "path_lines": [{"file": str(self.home / ".bashrc"), "marker": install_receipt.PATH_MARK,
                                "line": install_receipt.PATH_LINE}]}
        base.update(changes)
        return base

    def test_strict_schema(self) -> None:
        self.assertEqual(install_receipt.parse(self.document()).document()["format"], 2)
        launcher = self.document()["launchers"][0]
        line = self.document()["path_lines"][0]
        bad = {
            "legacy hashless format": self.document(format=1),
            "legacy launcher list": self.document(launchers=[launcher["path"]]),
            "unknown key": {**self.document(), "extra": 1},
            "relative launcher": self.document(launchers=[{"path": "bin/claude-multi", "sha256": "a" * 64}]),
            "unnormalized launcher": self.document(launchers=[{"path": "/home/../x", "sha256": "a" * 64}]),
            "short digest": self.document(launchers=[{"path": launcher["path"], "sha256": "a" * 63}]),
            "upper-case digest": self.document(launchers=[{"path": launcher["path"], "sha256": "A" * 64}]),
            "duplicate launcher": self.document(launchers=[launcher, launcher]),
            "line without marker": self.document(path_lines=[{**line, "line": "export PATH=x"}]),
            "two lines": self.document(path_lines=[{**line, "line": "a\n" + line["line"]}]),
            "marker not a comment": self.document(path_lines=[{**line, "marker": "added"}]),
            "missing line": self.document(path_lines=[{"file": line["file"], "marker": line["marker"]}]),
            "boolean format": self.document(format=True),
        }
        for label, document in bad.items():
            with self.subTest(label), self.assertRaises(install_receipt.ReceiptError):
                install_receipt.parse(document)
        with self.assertRaisesRegex(install_receipt.ReceiptError, "unreadable"):
            install_receipt.read(self.install)

    def test_ownership_evidence(self) -> None:
        launcher = self.home / ".local/bin/claude-multi"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"#!/bin/sh\nexec x\n")
        receipt = install_receipt.record(self.install, [launcher])
        entry = receipt.launchers[0]
        self.assertTrue(entry.owned())
        launcher.write_bytes(b"#!/bin/sh\nexec mine\n")
        self.assertFalse(entry.owned())
        launcher.unlink()
        launcher.symlink_to(self.tmp / "elsewhere")
        (self.tmp / "elsewhere").write_bytes(b"#!/bin/sh\nexec x\n")
        self.assertFalse(entry.owned())
        bashrc = self.home / ".bashrc"
        bashrc.write_text(f"# {install_receipt.PATH_MARK} mentioned\n")
        recorded = install_receipt.PathLine(str(bashrc), install_receipt.PATH_MARK, install_receipt.PATH_LINE)
        self.assertFalse(recorded.present())
        bashrc.write_text(f"x\n{install_receipt.PATH_LINE}\n")
        self.assertTrue(recorded.present())

    def test_a_legacy_receipt_vouches_for_nothing(self) -> None:
        legacy = {"format": 1, "launchers": ["/x"], "path_lines": [{"file": "/y", "marker": "# m"}]}
        fd = os.open(self.install / "installer.json", os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, json.dumps(legacy).encode())
        os.close(fd)
        launcher = self.home / "claude-multi"
        launcher.write_bytes(b"x")
        receipt = install_receipt.record(self.install, [launcher])
        self.assertEqual(receipt.path_lines, ())
        self.assertEqual(install_receipt.read(self.install), receipt)


if __name__ == "__main__":
    unittest.main()
