"""``claude-multi update`` and the card's U (``claude_multi.release_update``).

Releases are fake release directories signed with the stdlib test signer
(no ssh-keygen needed). Three layers:

- the journey in process (channels, up to date, plan, confirm, apply,
  rollback, refusals and exit codes);
- the https transport and the certificate trust against a local https
  server with a certificate authority made for the test (needs openssl);
- the real command line: an installation's own ``bin/claude-multi`` (the
  bundle carries this checkout's package and a test release key as its
  installed trust) updating, rolling back and putting the incoming Claude
  Code in place through the real acquisition, in a private HOME.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import http.server
import io
import json
import os
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import acquire, gateway_inhibition, install_txn, release_update as ru, self_update as su, tls, trust
import test_install_txn  # module import: no test classes re-exported
from _fake_release import FakeRelease
from _layout import RESOURCES_ROOT
from _release import keyless_trust
from test_trust import SEED_A, SEED_B, signer_line, sshsig

TODAY = datetime.date(2026, 10, 20)


def sign(release: FakeRelease, seed: bytes = SEED_A) -> FakeRelease:
    sums = (release.dir / "SHA256SUMS").read_bytes()
    (release.dir / "SHA256SUMS.sshsig").write_text(sshsig(seed, sums))
    return release


class Answers:
    """Scripted answers to the journey's questions."""

    def __init__(self, *answers: bool | None | type) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []

    def __call__(self, question: str) -> bool | None:
        self.questions.append(question)
        answer = self.answers.pop(0)
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt
        return answer


class JourneyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-journey-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir(mode=0o700)
        self.environ = {"HOME": str(self.home), "CLAUDE_MULTI_CHANNEL": "bundle"}
        self.root = su.install_root(self.environ)
        self.state = self.home / ".local/state/claude-multi"
        self.state.mkdir(parents=True, mode=0o700)
        self.held: list[str] = []

    def release(self, version: str, **kwargs) -> FakeRelease:
        return sign(FakeRelease(self.tmp / f"release-{version}", version, **kwargs))

    def seed(self, release: FakeRelease) -> None:
        versions = self.root / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        with tarfile.open(release.asset()) as tar:
            tar.extractall(versions, filter="data")
        (versions / f"claude-multi-{release.version}-linux-x86_64").rename(versions / release.version)
        current = self.root / "current"
        if current.is_symlink():
            current.unlink()
        current.symlink_to(f"versions/{release.version}")

    def journey(self, **changes) -> ru.Journey:
        base = dict(environ=self.environ, state_root=self.state, install_root=self.root, channel="bundle",
                    running=self.root / "current", hold=lambda: None, protected=lambda: frozenset(),
                    prune_copies=lambda _bundle, _release: [],
                    signers=lambda: trust.parse_allowed_signers(signer_line(SEED_A)), today=lambda: TODAY)
        base.update(changes)
        return ru.Journey(**base)

    def run_journey(self, *, mode: str = "update", ask=None, journey=None, **kwargs) -> tuple[int, str]:
        out = io.StringIO()
        code = ru.run(journey or self.journey(), mode=mode, ask=ask or Answers(), out=out, **kwargs)
        return code, out.getvalue()

    def current(self) -> str:
        return os.readlink(self.root / "current")


class ChannelTests(JourneyTestCase):
    def test_other_channels_get_instructions_and_nothing_changes(self) -> None:
        for channel, needle in (("nix", "update it through your flake"), ("source", "git pull"),
                                (None, "not installed by the claude-multi installer")):
            for mode in ("update", "check", "rollback"):
                with self.subTest(channel=channel, mode=mode):
                    code, out = self.run_journey(mode=mode, journey=self.journey(channel=channel))
                    self.assertEqual(code, ru.EXIT_REFUSED)
                    self.assertIn(needle, out)
                    self.assertIn("Nothing was changed", out)
        self.assertFalse((self.root / "current").exists())


class CheckAndUpdateTests(JourneyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed(self.release("1.0.0"))

    def test_an_equal_version_is_up_to_date_and_never_planned(self) -> None:
        same = self.release("1.0.0")
        with mock.patch.object(su, "plan", side_effect=AssertionError("never planned")), \
                mock.patch.object(su, "apply", side_effect=AssertionError("never applied")):
            for mode in ("update", "check"):
                code, out = self.run_journey(mode=mode, from_dir=str(same.dir), assume_yes=True)
                self.assertEqual(code, ru.EXIT_OK, out)
                self.assertIn("claude-multi 1.0.0 is up to date (released 2026-10-15)", out)
        self.assertEqual(self.current(), "versions/1.0.0")
        older = self.release("0.9.0")
        code, out = self.run_journey(from_dir=str(older.dir))
        self.assertEqual(code, ru.EXIT_OK)
        self.assertIn("newer than the latest release 0.9.0", out)

    def test_check_only_reports(self) -> None:
        newer = self.release("1.1.0")
        code, out = self.run_journey(mode="check", from_dir=str(newer.dir))
        self.assertEqual(code, ru.EXIT_OK, out)
        self.assertIn("claude-multi 1.1.0 is available (released 2026-10-15); 1.0.0 is installed", out)
        self.assertEqual(self.current(), "versions/1.0.0")

    def test_confirmation_and_exit_codes(self) -> None:
        newer = self.release("1.1.0")
        for answer, code_expected, text in ((None, ru.EXIT_REFUSED, "confirm on a terminal, or pass --yes"),
                                            (False, ru.EXIT_DECLINED, "Nothing was changed."),
                                            (KeyboardInterrupt, ru.EXIT_CANCELLED, "Cancelled")):
            with self.subTest(answer=answer):
                ask = Answers(answer)
                code, out = self.run_journey(from_dir=str(newer.dir), ask=ask)
                self.assertEqual(code, code_expected, out)
                self.assertIn(text, out)
                self.assertEqual(ask.questions, ["Update claude-multi to 1.1.0 now?"])
                self.assertIn("Update plan: 1.0.0 -> 1.1.0", out)
                self.assertEqual(self.current(), "versions/1.0.0")
        code, out = self.run_journey(from_dir=str(newer.dir), ask=Answers(True))
        self.assertEqual(code, ru.EXIT_OK, out)
        self.assertIn("Updated claude-multi to 1.1.0; 1.0.0 is kept for 'claude-multi update --rollback'", out)
        self.assertIn("Only the launcher changed", out)
        self.assertEqual(self.current(), "versions/1.1.0")

    def test_refusals_before_anything_is_fetched(self) -> None:
        newer = self.release("1.1.0")
        cases = {
            "inside a session": (dict(environ={**self.environ, "CLAUDECODE": "1"}), "inside a Claude Code session"),
            "another launcher": (dict(running=self.tmp / "elsewhere"), "not from the installed release's current"),
            "no release key": (dict(signers=lambda: trust.release_signers(keyless_trust(self.tmp))),
                               "carries no release signing key"),
            "nothing installed": (dict(install_root=self.tmp / "none"), "no installed release"),
        }
        for label, (changes, needle) in cases.items():
            with self.subTest(label):
                code, out = self.run_journey(journey=self.journey(**changes), from_dir=str(newer.dir), assume_yes=True)
                self.assertEqual(code, ru.EXIT_REFUSED, out)
                self.assertIn(needle, out)
        self.assertEqual(self.current(), "versions/1.0.0")
        fd = os.open(self.state / "channel", os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"nix\n")
        os.close(fd)
        code, out = self.run_journey(from_dir=str(newer.dir), assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED)
        self.assertIn("belongs to the nix installation", out)

    def test_a_signature_by_another_key_is_refused(self) -> None:
        rogue = sign(FakeRelease(self.tmp / "rogue", "1.1.0"), SEED_B)
        code, out = self.run_journey(from_dir=str(rogue.dir), assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED)
        self.assertIn("is not trusted", out)
        self.assertEqual(self.current(), "versions/1.0.0")

    def test_no_download_location(self) -> None:
        code, out = self.run_journey(assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED)
        self.assertIn("names no download location", out)
        self.assertIn("claude-multi update --from-dir DIR", out)

    def test_the_gateway_hold_and_the_running_gateway(self) -> None:
        newer = self.release("1.1.0", gateway="b" * 64)
        held = self.journey(hold=lambda: "the gateway's persistence hold is active: save failed")
        code, out = self.run_journey(journey=held, from_dir=str(newer.dir), assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED)
        self.assertIn("persistence hold is active", out)
        self.assertEqual(self.current(), "versions/1.0.0")
        self.run_journey(from_dir=str(self.release("1.0.5").dir), assume_yes=True)
        running = self.journey(protected=lambda: frozenset({"1.0.0"}))
        code, out = self.run_journey(journey=running, from_dir=str(newer.dir), assume_yes=True)
        self.assertEqual(code, ru.EXIT_OK, out)
        self.assertIn("Kept 1.0.0: the running gateway executes them", out)
        self.assertIn("The gateway binary changed", out)

    def test_the_plan_names_a_state_migration_that_blocks_rollback(self) -> None:
        migrating = self.release("1.1.0", state_format=5)
        code, out = self.run_journey(from_dir=str(migrating.dir), ask=Answers(False))
        self.assertEqual(code, ru.EXIT_DECLINED)
        self.assertIn("rolling back to 1.0.0 is refused", out)
        self.assertIn("'claude-multi update --rollback' to 1.0.0 is refused", out)


class RollbackTests(JourneyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed(self.release("1.0.0"))

    def test_rollback_journey(self) -> None:
        code, out = self.run_journey(mode="rollback", assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED)
        self.assertIn("no previous version", out)
        self.run_journey(from_dir=str(self.release("1.1.0", state_format=5).dir), assume_yes=True)
        code, out = self.run_journey(mode="rollback", ask=Answers(False))
        self.assertEqual(code, ru.EXIT_DECLINED)
        code, out = self.run_journey(mode="rollback", ask=Answers(True))
        self.assertEqual(code, ru.EXIT_OK, out)
        self.assertIn("Rolled back to claude-multi 1.0.0; 1.1.0 is now the previous version", out)
        self.assertEqual((self.current(), os.readlink(self.root / "previous")), ("versions/1.0.0", "versions/1.1.0"))
        self.run_journey(mode="rollback", assume_yes=True)
        fd = os.open(self.state / "state-version", os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"5\n")
        os.close(fd)
        code, out = self.run_journey(mode="rollback", assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED)
        self.assertIn("cannot roll back to 1.0.0", out)
        self.assertIn("state is only ever migrated forward", out)
        self.assertEqual(self.current(), "versions/1.1.0")


class TransactionTests(JourneyTestCase):
    """The update's gateway inhibition: recorded before the first change,
    ended on success or when nothing changed, kept by an interruption after
    the installation started changing and finished by the next run."""

    def setUp(self) -> None:
        super().setUp()
        self.seed(self.release("1.0.0"))
        self.newer = self.release("1.1.0", claude="9.9.9")

    def record(self):
        return gateway_inhibition.read(self.state)

    def fail_rename_into(self, name: str):
        real = os.rename

        def rename(src, dst):
            if Path(dst).name == name and ".staging." in str(src):
                raise OSError(28, "No space left on device")
            return real(src, dst)

        return mock.patch.object(su.os, "rename", side_effect=rename)

    def test_a_successful_update_records_and_ends_its_inhibition(self) -> None:
        seen = []

        def prune(bundle: Path, release: su.Release) -> list[str]:
            record = self.record()
            seen.append((record.owner, record.phase, record.purpose))
            return ["Kept Claude Code copies in use: 2.1.100 — once no session runs them, `x` removes them."]

        code, out = self.run_journey(journey=self.journey(prune_copies=prune, acquire_pin=lambda *_a: None),
                                     from_dir=str(self.newer.dir), assume_yes=True)
        self.assertEqual(code, ru.EXIT_OK, out)
        self.assertEqual(seen, [("updater", "prune", "update claude-multi 1.0.0 to 1.1.0")])
        self.assertIn("Kept Claude Code copies in use: 2.1.100", out)
        self.assertIsNone(self.record())

    def test_a_failure_before_the_installation_changes_ends_it(self) -> None:
        def unavailable(_bundle, _release):
            raise su.UpdateError("Claude Code 9.9.9 is not in place; nothing was changed")

        cases = {
            "download": dict(),  # a tampered asset: the download step refuses
            "pin": dict(acquire_pin=unavailable),
        }
        for phase, changes in cases.items():
            with self.subTest(phase):
                release = self.newer
                if phase == "download":
                    release = self.release("1.1.0")
                    release.asset().write_bytes(b"tampered")
                code, out = self.run_journey(journey=self.journey(**changes), from_dir=str(release.dir),
                                             assume_yes=True)
                self.assertEqual(code, ru.EXIT_REFUSED, out)
                self.assertIsNone(self.record())
                self.assertEqual(self.current(), "versions/1.0.0")
                self.assertNotIn("stopped in its", out)

    def test_an_interruption_at_each_later_step_keeps_it_until_the_next_run_finishes(self) -> None:
        def flip_fails(*_args):
            raise OSError(5, "Input/output error")

        def prune_fails(_bundle, _release):
            raise KeyboardInterrupt

        cases = {
            "replace": (self.fail_rename_into("1.1.0"), ru.EXIT_REFUSED, "versions/1.0.0"),
            "switch": (mock.patch.object(su, "_flip", side_effect=flip_fails), ru.EXIT_REFUSED, "versions/1.0.0"),
            "prune": (contextlib.nullcontext(), ru.EXIT_CANCELLED, "versions/1.1.0"),
        }
        for phase, (failure, exit_code, current) in cases.items():
            with self.subTest(phase):
                journey = self.journey(acquire_pin=lambda *_a: None,
                                       **({"prune_copies": prune_fails} if phase == "prune" else {}))
                with failure:
                    code, out = self.run_journey(journey=journey, from_dir=str(self.newer.dir), assume_yes=True)
                self.assertEqual(code, exit_code, out)
                self.assertIn(f"update claude-multi 1.0.0 to 1.1.0 stopped in its {phase} step", out)
                self.assertIn("run claude-multi update again", out)
                self.assertEqual(self.current(), current)
                record = self.record()
                self.assertEqual((record.owner, record.phase), ("updater", phase))
                # Another owner is refused meanwhile.
                with self.assertRaises(install_txn.TransactionError):
                    install_txn.begin(self.state, owner="installer", purpose="install")
                # The next run finishes it first, then carries on.
                code, out = self.run_journey(journey=self.journey(acquire_pin=lambda *_a: None),
                                             from_dir=str(self.newer.dir), assume_yes=True)
                self.assertEqual(code, ru.EXIT_OK, out)
                self.assertIn(f"Finishing an earlier update that stopped in its {phase} step.", out)
                self.assertIn("gateway changes are no longer paused", out)
                self.assertIsNone(self.record())
                self.assertEqual(self.current(), "versions/1.1.0")
                self.assertFalse([p for p in (self.root / "versions").iterdir() if p.name.startswith(".")])
                # Back to 1.0.0 for the next case.
                self.assertEqual(self.run_journey(mode="rollback", assume_yes=True)[0], ru.EXIT_OK)
                shutil.rmtree(self.root / "versions/1.1.0")
                (self.root / "previous").unlink()

    def test_an_interrupted_rollback_is_finished_by_the_next_one(self) -> None:
        self.assertEqual(self.run_journey(journey=self.journey(acquire_pin=lambda *_a: None),
                                          from_dir=str(self.newer.dir), assume_yes=True)[0], ru.EXIT_OK)
        with mock.patch.object(su, "_flip", side_effect=OSError(5, "Input/output error")):
            code, out = self.run_journey(mode="rollback", assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED, out)
        self.assertIn("roll back claude-multi to 1.0.0 stopped in its switch step", out)
        self.assertEqual(self.record().phase, "switch")
        code, out = self.run_journey(mode="rollback", assume_yes=True)
        self.assertEqual(code, ru.EXIT_OK, out)
        self.assertIn("Finishing an earlier update that stopped in its switch step.", out)
        self.assertEqual(self.current(), "versions/1.0.0")
        self.assertIsNone(self.record())

    def links(self) -> tuple[str, str]:
        return self.current(), os.readlink(self.root / "previous")

    def test_a_switch_stopped_between_its_links_is_put_back_by_the_next_run(self) -> None:
        """The rollback dies after ``previous`` changed and before ``current``
        does (both links on 1.1.0): the next run puts the recorded pair back,
        then does what it was asked, and the rollback target is not lost."""

        self.assertEqual(self.run_journey(journey=self.journey(acquire_pin=lambda *_a: None),
                                          from_dir=str(self.newer.dir), assume_yes=True)[0], ru.EXIT_OK)
        self.assertEqual(self.links(), ("versions/1.1.0", "versions/1.0.0"))
        real = su._flip

        def flip(root, name, version):
            if name == su.CURRENT:
                raise OSError(5, "Input/output error")
            real(root, name, version)

        with mock.patch.object(su, "_flip", side_effect=flip):
            code, out = self.run_journey(mode="rollback", assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED, out)
        self.assertIn("roll back claude-multi to 1.0.0 stopped in its switch step", out)
        self.assertEqual(self.links(), ("versions/1.1.0", "versions/1.1.0"))
        self.assertEqual((self.record().owner, self.record().phase), ("updater", "switch"))
        code, out = self.run_journey(mode="rollback", assume_yes=True)
        self.assertEqual(code, ru.EXIT_OK, out)
        self.assertIn("Put the installed versions back as they were before an interrupted switch "
                      "(current 1.1.0, previous 1.0.0).", out)
        self.assertIn("gateway changes are no longer paused", out)
        self.assertIn("Rolled back to claude-multi 1.0.0; 1.1.0 is now the previous version.", out)
        self.assertEqual(self.links(), ("versions/1.0.0", "versions/1.1.0"))
        self.assertIsNone(self.record())
        self.assertFalse((self.root / su.SWITCH_RECORD).exists())
        # An update stopped the same way: put back, then updated again.
        self.assertEqual(self.run_journey(mode="rollback", assume_yes=True)[0], ru.EXIT_OK)  # 1.1.0 current
        newest = self.release("1.2.0")
        with mock.patch.object(su, "_flip", side_effect=flip):
            code, out = self.run_journey(journey=self.journey(acquire_pin=lambda *_a: None),
                                         from_dir=str(newest.dir), assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED, out)
        self.assertEqual(self.links(), ("versions/1.1.0", "versions/1.1.0"))
        code, out = self.run_journey(journey=self.journey(acquire_pin=lambda *_a: None),
                                     from_dir=str(newest.dir), assume_yes=True)
        self.assertEqual(code, ru.EXIT_OK, out)
        self.assertIn("(current 1.1.0, previous 1.0.0)", out)
        self.assertEqual(self.links(), ("versions/1.2.0", "versions/1.1.0"))

    def test_a_rollback_switches_only_to_the_version_confirmed(self) -> None:
        """Another update lands while the rollback waits for its answer: the
        confirmed rollback (to 1.0.0) is refused rather than turned into a
        rollback to 1.1.0."""

        self.assertEqual(self.run_journey(journey=self.journey(acquire_pin=lambda *_a: None),
                                          from_dir=str(self.newer.dir), assume_yes=True)[0], ru.EXIT_OK)
        newest = self.release("1.2.0", claude="9.9.9")

        def ask(question: str) -> bool:
            self.assertEqual(question, "Roll back claude-multi to 1.0.0 now?")
            code, out = self.run_journey(journey=self.journey(acquire_pin=lambda *_a: None),
                                         from_dir=str(newest.dir), assume_yes=True)
            self.assertEqual(code, ru.EXIT_OK, out)
            return True

        code, out = self.run_journey(mode="rollback", ask=ask)
        self.assertEqual(code, ru.EXIT_REFUSED, out)
        self.assertIn("the installation changed since the rollback was confirmed (1.1.0 -> 1.0.0 was confirmed; "
                      "it is now 1.2.0 -> 1.1.0); nothing was changed", out)
        self.assertNotIn("Rolled back", out)
        self.assertEqual(self.links(), ("versions/1.2.0", "versions/1.1.0"))
        self.assertIsNone(self.record())  # nothing changed: the rollback's inhibition ended

    def test_another_owners_inhibition_and_an_earlier_hand_off_record_refuse(self) -> None:
        held = install_txn.begin(self.state, owner="installer", purpose="install claude-multi 1.0.0",
                                 pid=os.getppid())
        code, out = self.run_journey(from_dir=str(self.newer.dir), assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED, out)
        self.assertIn("the gateway is inhibited by installer (install claude-multi 1.0.0; phase prepare", out)
        self.assertIn("sh install.sh --repair", out)
        self.assertEqual(self.current(), "versions/1.0.0")
        held.end()
        test_install_txn.legacy_handoff(self.state, pid=test_install_txn.DEAD_PID)
        code, out = self.run_journey(from_dir=str(self.newer.dir), assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED, out)
        self.assertIn("inhibited by service (gateway service install (claude-multi-gateway)", out)
        self.assertIn("claude-multi gateway service install", out)
        self.assertEqual(self.current(), "versions/1.0.0")


class FactsTests(JourneyTestCase):
    def test_release_age_lines(self) -> None:
        runtime = mock.Mock(environ=self.environ, home=self.home)
        self.assertEqual(ru.card_hint(runtime, today=TODAY), (mock.ANY, "installed release unreadable — sh install.sh --repair"))
        self.seed(self.release("1.0.0", release_date="2026-10-01"))
        self.assertEqual(ru.card_hint(runtime, today=TODAY), ("1.0.0", "released 2026-10-01"))
        old = ru.card_hint(runtime, today=datetime.date(2026, 12, 1))
        self.assertEqual(old, ("1.0.0", "this release is 61 days old — claude-multi update --check"))
        lines = ru.doctor_lines(runtime, today=TODAY)
        self.assertIn("Installed release: claude-multi 1.0.0 was released 2026-10-01, 19 days ago — check for a "
                      "newer one: claude-multi update --check", lines)
        self.assertTrue(lines[0].startswith("HTTPS trust (update, Claude Code download): "))
        nix = mock.Mock(environ={**self.environ, "CLAUDE_MULTI_CHANNEL": "nix"}, home=self.home)
        self.assertEqual(ru.card_hint(nix)[1], "updated through your Nix flake")
        self.assertIsNone(ru.card_hint(mock.Mock(environ={"HOME": str(self.home)}, home=self.home)))
        self.assertEqual(len(ru.doctor_lines(nix)), 1)


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict[str, str], bytes]] = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        status, headers, body = self.routes.get(self.path, (404, {}, b"missing"))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return None


@unittest.skipUnless(shutil.which("openssl"), "BOUNDARY: openssl is not installed (it makes the test CA)")
class HttpsTests(unittest.TestCase):
    """The transport, the redirect lock and the trust source, over real TLS."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-tls-"))
        run = lambda *args: subprocess.run(["openssl", *args], cwd=cls.tmp, check=True, capture_output=True,  # noqa: E731
                                           timeout=120, stdin=subprocess.DEVNULL)
        run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
            "-subj", "/CN=claude-multi test CA", "-keyout", "ca.key", "-out", "ca.pem",
            "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign")
        run("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-subj", "/CN=localhost",
            "-keyout", "server.key", "-out", "server.csr")
        run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
            "-subj", "/CN=another CA", "-keyout", "other.key", "-out", "other.pem",
            "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign")
        (cls.tmp / "ext").write_text("subjectAltName=DNS:localhost\nextendedKeyUsage=serverAuth\n")
        run("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
            "-days", "2", "-extfile", "ext", "-out", "server.pem")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.tmp / "server.pem", cls.tmp / "server.key")
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]
        cls.trusted = {"SSL_CERT_FILE": str(cls.tmp / "ca.pem")}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def url(self, path: str = "", host: str = "localhost") -> str:
        return f"https://{host}:{self.port}{path}"

    def test_fetch_download_and_caps(self) -> None:
        _Handler.routes = {"/v1.0.0/MANIFEST.json": (200, {}, b"{}"), "/latest/MANIFEST.json": (200, {}, b"{\"a\":1}"),
                           "/v1.0.0/big.tar.gz": (200, {}, b"x" * 100)}
        transport = ru.HttpsTransport(self.url("/v{version}"), self.url("/latest"), environ=self.trusted)
        self.assertEqual(transport.fetch("MANIFEST.json", "1.0.0", 10), b"{}")
        self.assertEqual(transport.fetch("MANIFEST.json", None, 10), b"{\"a\":1}")
        with self.assertRaisesRegex(su.UpdateError, "larger than 3 bytes"):
            transport.fetch("MANIFEST.json", None, 3)
        destination = Path(self.tmp) / "out"
        with self.assertRaisesRegex(su.UpdateError, "larger than the 50 bytes"):
            transport.download("big.tar.gz", "1.0.0", destination, 50)
        transport.download("big.tar.gz", "1.0.0", destination, 100)
        self.assertEqual(destination.read_bytes(), b"x" * 100)
        with self.assertRaisesRegex(su.UpdateError, "answered 404"):
            transport.fetch("SHA256SUMS", "1.0.0", 10)
        with self.assertRaisesRegex(su.UpdateError, "not a release file name"):
            transport.fetch("../x", "1.0.0", 10)

    def test_untrusted_certificates_and_plain_http_are_refused(self) -> None:
        _Handler.routes = {"/MANIFEST.json": (200, {}, b"{}")}
        with mock.patch.object(tls, "SYSTEM_BUNDLES", ()):
            untrusted = ru.HttpsTransport(self.url(), environ={"SSL_CERT_FILE": str(self.tmp / "other.pem")})
            with self.assertRaisesRegex(su.UpdateError, "cannot reach the release server"):
                untrusted.fetch("MANIFEST.json", None, 10)
        for bad in ("http://localhost/x", "https://user:pw@localhost/x", "ftp://x"):
            with self.subTest(bad), self.assertRaises(su.UpdateError):
                ru.HttpsTransport(bad)

    def test_redirects_stay_on_the_release_host(self) -> None:
        _Handler.routes = {"/MANIFEST.json": (302, {"Location": self.url("/other", host="127.0.0.1")}, b""),
                           "/hop": (302, {"Location": "/MANIFEST2.json"}, b""),
                           "/MANIFEST2.json": (200, {}, b"ok")}
        transport = ru.HttpsTransport(self.url(), environ=self.trusted)
        with self.assertRaisesRegex(su.UpdateError, "not a release host"):
            transport.fetch("MANIFEST.json", None, 10)
        _Handler.routes["/SHA256SUMS"] = (302, {"Location": self.url("/MANIFEST2.json")}, b"")
        self.assertEqual(transport.fetch("SHA256SUMS", None, 10), b"ok")
        self.assertIn("objects.githubusercontent.com",
                      ru.HttpsTransport("https://github.com/o/r/releases/download/v{version}").hosts)

    def test_the_trust_source_and_the_other_transports(self) -> None:
        self.assertEqual(tls.source(self.trusted)[2], f"SSL_CERT_FILE={self.tmp / 'ca.pem'}")
        self.assertEqual(tls.source({"SSL_CERT_DIR": "/certs"})[:2], (None, "/certs"))
        # A configured location that is missing (the Nix build sandbox sets
        # one) or unloadable trusts nothing, and never breaks building the
        # context: plain http through the same opener keeps working.
        missing = str(self.tmp / "missing.pem")
        self.assertEqual(tls.source({"SSL_CERT_FILE": missing})[2], f"SSL_CERT_FILE={missing} (missing)")
        garbage = self.tmp / "garbage.pem"
        garbage.write_text("not a certificate\n")
        for configured in ({"SSL_CERT_FILE": missing}, {"SSL_CERT_FILE": str(garbage)}):
            with self.subTest(configured=configured):
                context = tls.context(configured)
                self.assertEqual((context.verify_mode, context.check_hostname), (ssl.CERT_REQUIRED, True))
                self.assertEqual(context.get_ca_certs(), [])
                with self.assertRaisesRegex(su.UpdateError, "cannot reach the release server"):
                    ru.HttpsTransport(self.url(), environ=configured).fetch("MANIFEST.json", None, 10)
        _Handler.routes = {"/claude": (200, {}, b"binary")}
        with mock.patch.dict(os.environ, self.trusted):
            with contextlib.closing(acquire._open(self.url("/claude"), headers={}, timeout=10)) as response:
                self.assertEqual(response.read(), b"binary")
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(self.tmp / "other.pem")}), \
                mock.patch.object(tls, "SYSTEM_BUNDLES", ()), self.assertRaises(OSError):
            acquire._open(self.url("/claude"), headers={}, timeout=10)


class CommandLineTests(JourneyTestCase):
    """The installed release's own claude-multi, in a subprocess with a private HOME."""

    def release(self, version: str, **kwargs) -> FakeRelease:
        kwargs.setdefault("trust", signer_line(SEED_A))
        return super().release(version, real_launchers=True, **kwargs)

    def cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LC_ALL": "C", "TMPDIR": str(self.tmp)}
        return subprocess.run([str(self.root / "current/bin/claude-multi"), *args], env=env, cwd=cwd or self.tmp,
                              capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)

    def test_update_and_rollback_through_the_installed_command(self) -> None:
        self.seed(self.release("1.0.0"))
        result = self.cli("update", "--from-dir", str(self.release("1.0.0").dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("claude-multi 1.0.0 is up to date", result.stdout)
        newer = self.release("1.1.0")
        result = self.cli("update", "--check", "--from-dir", str(newer.dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("claude-multi 1.1.0 is available", result.stdout)
        result = self.cli("update", "--from-dir", str(newer.dir))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("confirm on a terminal, or pass --yes", result.stdout)
        result = self.cli("update")
        self.assertEqual(result.returncode, 1)
        self.assertIn("names no download location", result.stdout)
        result = self.cli("update", "--from-dir", str(newer.dir), "--yes", cwd=Path("/"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Updated claude-multi to 1.1.0", result.stdout)
        self.assertEqual(self.current(), "versions/1.1.0")
        version = self.cli("--version")
        self.assertTrue(version.stdout.startswith("claude-multi 1.1.0"), version.stdout + version.stderr)
        result = self.cli("update", "--rollback", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.current(), "versions/1.0.0")
        self.assertEqual(self.cli("update", "--rollback", "--check").returncode, 2)  # one mode at a time

    def test_update_check_leaves_the_shared_shims_and_the_channel_marker_alone(self) -> None:
        """A machine move checks the installation's identity with update
        --check while the shared shims still name the earlier launcher: the
        check reports, and repoints, records or creates nothing."""

        from claude_multi import installs, scope

        self.seed(self.release("1.0.0"))
        earlier = self.tmp / "earlier" / "bin" / "claude-multi"
        earlier.parent.mkdir(parents=True)
        earlier.write_text("#!/bin/sh\n")
        environ = {"HOME": str(self.home)}
        shims = (scope.ensure_hook_shim(self.state, str(earlier)),
                 scope.ensure_hook_shim_v3(self.state, str(earlier)),
                 scope.ensure_gateway_token_shim(self.state, scope.resolve_gateway_token_path(environ),
                                                 str(earlier)))
        self.assertEqual(installs.claim_marker(self.state, "bundle", environ), ("bundle", True))
        watched = (*shims, installs.marker_path(self.state))
        for path in watched:
            os.utime(path, ns=(1_000_000_000, 1_000_000_000))

        def snapshot() -> dict[str, tuple[bytes, int]]:
            return {str(path): (path.read_bytes(), os.stat(path).st_mtime_ns) for path in watched}

        def tree() -> list[str]:
            return sorted(str(path.relative_to(self.state)) for path in self.state.rglob("*"))

        before, entries = snapshot(), tree()
        result = self.cli("update", "--check", "--from-dir", str(self.release("1.0.0").dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("claude-multi 1.0.0 is up to date (released 2026-10-15)", result.stdout)
        result = self.cli("update", "--check", "--from-dir", str(self.release("1.1.0").dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("claude-multi 1.1.0 is available (released 2026-10-15); 1.0.0 is installed", result.stdout)
        self.assertIn("Run 'claude-multi update' to see what it changes and install it.", result.stdout)
        result = self.cli("update", "--check")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("names no download location", result.stdout)
        self.assertEqual(snapshot(), before)
        self.assertEqual(tree(), entries)
        self.assertEqual(self.current(), "versions/1.0.0")
        self.assertIn(str(earlier), shims[0].read_text())

    def test_the_incoming_claude_code_is_put_in_place_and_recorded_for_the_incoming_release(self) -> None:
        from claude_multi import pin

        self.seed(self.release("1.0.0"))
        platform = pin.host_platform()
        client = b"#!/bin/sh\necho 'fake claude 2.1.999'\n"
        record = {"sha256": hashlib.sha256(client).hexdigest(), "size": len(client)}
        contract = json.loads((RESOURCES_ROOT / "catalog/native-contract.json").read_text())
        contract["verified"][0]["version"] = "2.1.999"
        contract["verified"][0]["platforms"][platform] = record
        native = self.home / ".local/share/claude/versions"
        native.mkdir(parents=True)
        (native / "2.1.999").write_bytes(client)
        os.chmod(native / "2.1.999", 0o755)
        newer = self.release("1.2.0", claude="2.1.999", claude_platforms={platform: record},
                             overrides={"claude_multi/data/catalog/native-contract.json":
                                        (json.dumps(contract, indent=2) + "\n").encode()})
        result = self.cli("update", "--from-dir", str(newer.dir), "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("runs Claude Code 2.1.999", result.stdout)
        owned = pin.owned_path({"HOME": str(self.home)}, "2.1.999", platform)
        self.assertEqual(owned.read_bytes(), client)
        self.assertEqual(acquire.read_index({"HOME": str(self.home)}).get("1.2.0"), "2.1.999")
        pins = su.installed_pins({"HOME": str(self.home)})
        self.assertEqual(set(pins), {"2.1.999", "0.0.0-test"})

    @unittest.skipUnless(Path("/usr/bin/sleep").is_file() and Path("/proc/self/exe").exists(),
                         "BOUNDARY: needs /usr/bin/sleep and /proc (a process executing an installed gateway file)")
    def test_an_unresolved_credential_save_failure_vetoes_gateway_changing_switches(self) -> None:
        """ENOSPC at a credential save, its log pruned at a later start (the
        failure persists as a hold), then the installed update and rollback:
        both refuse to switch to another gateway binary, a launcher-only
        update proceeds, and the running gateway's version stays."""

        import fcntl
        import socket

        from claude_multi import endpoint, gateway_hold, service
        from claude_multi.platform import file_log
        from test_gateway_lifecycle import save_line

        sleep = Path("/usr/bin/sleep").read_bytes()
        running_gateway = hashlib.sha256(sleep).hexdigest()
        self.seed(self.release("1.0.0", gateway=running_gateway, gateway_binary=sleep))
        with socket.socket() as probe:  # a free port, so the observation never looks at another gateway
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        endpoint.write_config(self.home, endpoint.parse_config({"version": 1, "host": "127.0.0.1", "port": port}))
        # The gateway runs 1.0.0's binary, holds its instance lock and has a start record.
        workdir = service.ensure_gateway_workdir(self.state)
        lock = os.open(service.instance_lock_path(self.state), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        binary = self.root / "versions/1.0.0/libexec/claude-multi/cli-proxy-api"
        process = subprocess.Popen([str(binary), "300"], pass_fds=(lock,), stdin=subprocess.DEVNULL)
        os.close(lock)  # the gateway process keeps the lock
        self.addCleanup(lambda: (process.kill(), process.wait()))
        service.write_exec_stamp(workdir, service.ExecStamp(
            "1.0.0", "signature", process.pid, os.path.realpath(binary), service.utc_now(),
            pid_namespace=os.readlink("/proc/self/ns/pid"), instance="2222222222222222"))
        # Before the failure: a gateway-changing update goes through (1.0.0 stays: the gateway runs it).
        result = self.cli("update", "--from-dir", str(self.release("1.0.5", gateway="b" * 64).dir), "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # An older instance's log: a credential save failed with ENOSPC (errno 28), then the log is pruned.
        started = datetime.datetime(2026, 10, 2, 11, 0, tzinfo=datetime.timezone.utc)
        descriptor, _path = file_log.create_instance_log(workdir / service.GATEWAY_LOGS, started, "1111111111111111")
        os.write(descriptor, (save_line("failed") + "\n").encode())
        os.close(descriptor)
        pruned = gateway_hold.prune_at_start(self.state, ("claude",), keep={"2222222222222222"}, budget=0)
        self.assertEqual((pruned.removed, pruned.persisted), (("1111111111111111",), ("1111111111111111",)))
        result = self.cli("update", "--from-dir", str(self.release("1.1.0", gateway="c" * 64).dir), "--yes")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("persistence hold is active", result.stdout)
        self.assertIn("instance 1111111111111111", result.stdout)
        result = self.cli("update", "--rollback", "--yes")  # back to 1.0.0's gateway: another binary
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("persistence hold is active", result.stdout)
        self.assertEqual(self.current(), "versions/1.0.5")
        launcher_only = self.release("1.0.6", gateway="b" * 64)
        result = self.cli("update", "--from-dir", str(launcher_only.dir), "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Kept 1.0.0: the running gateway executes them", result.stdout)
        self.assertEqual(sorted(p.name for p in (self.root / "versions").iterdir()), ["1.0.0", "1.0.5", "1.0.6"])
        self.assertIsNone(process.poll())  # the gateway was never stopped

    def test_an_unavailable_incoming_claude_code_changes_nothing(self) -> None:
        from claude_multi import pin

        self.seed(self.release("1.0.0"))
        platform = pin.host_platform()
        contract = json.loads((RESOURCES_ROOT / "catalog/native-contract.json").read_text())
        contract["verified"][0]["version"] = "2.1.998"
        contract["verified"][0]["platforms"][platform] = {"sha256": "e" * 64, "size": 12}
        newer = self.release("1.2.0", claude="2.1.998", overrides={
            "claude_multi/data/catalog/native-contract.json": (json.dumps(contract) + "\n").encode()})
        with mock.patch.object(acquire, "_open", side_effect=AssertionError("no network in tests")):
            journey = self.journey(acquire_pin=ru.pin_acquirer(
                {"HOME": str(self.home)}, platform=platform,
                opener=lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))))
            code, out = self.run_journey(journey=journey, from_dir=str(newer.dir), assume_yes=True)
        self.assertEqual(code, ru.EXIT_REFUSED, out)
        self.assertIn("Claude Code 2.1.998 could not be put in place", out)
        self.assertEqual(self.current(), "versions/1.0.0")
        self.assertEqual(sorted(p.name for p in (self.root / "versions").iterdir()), ["1.0.0"])


if __name__ == "__main__":
    unittest.main()
