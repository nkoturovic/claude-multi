"""Account sign-in and sign-out: the typed personal-use acknowledgement,
the gateway program's login gated on it, the sign-in run in this terminal
(a stand-in program, no network), names-only account listing and the
sign-out into a kept backup with its exact undo."""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import test_cli
from _layout import FIXTURES_ROOT
from claude_multi import choices, proxy
from claude_multi.setup import external, model, signin, texts
import claude_multi.cli.consent as consent

FAKE_LOGIN = FIXTURES_ROOT / "fake_gateway_login.py"
CLAUDE_ACK_ID, CLAUDE_ACK_TEXT = signin.ack_text("claude")
_CHATGPT_ACK_ID, CHATGPT_ACK_TEXT = signin.ack_text("codex")
# The headers as they read before the pools became data, byte for byte.
CLAUDE_BROWSER_HEADER = (
    "claude-multi — Claude account sign-in (personal use)\n"
    "A browser page opens at claude.ai. Approve the sign-in there; it finishes here.\n"
    "If no page opens, open the address printed below. If the browser ends on a page that cannot\n"
    "load, copy that page's full address and paste it here when asked.\n"
    "Ctrl-C cancels; nothing changes unless the sign-in completes.\n")
CLAUDE_ADDRESS_HEADER = (
    "claude-multi — Claude account sign-in (personal use)\n"
    "Open the address printed below in a browser on any device and approve the sign-in. The browser\n"
    "then ends on a page that cannot load: copy that page's full address and paste it here when asked.\n"
    "Ctrl-C cancels; nothing changes unless the sign-in completes.\n")
CHATGPT_HEADER = (
    "claude-multi — ChatGPT account sign-in (personal use)\n"
    "An address and a one-time code are printed below. Open the address on any device, enter the code\n"
    "and approve; the sign-in finishes here.\n"
    "Ctrl-C cancels; nothing changes unless the sign-in completes.\n")


@contextlib.contextmanager
def quiet_stdio():
    """Silence what an inheriting child writes to this process's stdout/stderr."""

    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    sink = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(sink, 1)
        os.dup2(sink, 2)
        yield
    finally:
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in (*saved, sink):
            os.close(fd)


class SignInCase(test_cli.OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        self.argv_log = self.root / "fake-login-argv.jsonl"
        self.launcher = self.write_launcher("fake-gateway", {"FAKE_LOGIN_ARGV": str(self.argv_log)})
        self.runtime.environ["CLAUDE_MULTI_PROXY_BIN"] = str(self.launcher)
        self.auth = signin.auth_dir(self.runtime)

    def write_launcher(self, name: str, env: dict[str, str]) -> Path:
        """A /bin/sh launcher with an absolute interpreter (no /usr/bin/env)."""

        target = self.root / name
        exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        target.write_text(f"#!/bin/sh\n{exports} exec {shlex.quote(sys.executable)} "
                          f"{shlex.quote(str(FAKE_LOGIN))} \"$@\"\n")
        target.chmod(0o755)
        return target

    def human(self):
        return mock.patch.object(consent, "stdio_ttys", return_value=True)

    def acknowledge(self, pool: str) -> signin.Ack:
        return signin.record_ack(self.runtime, pool, "personal",
                                 now=datetime.datetime(2026, 10, 3, tzinfo=datetime.timezone.utc))

    def record(self, name: str, data: bytes = b"{}") -> Path:
        self.auth.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.auth / name
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def argv_runs(self) -> list[list[str]]:
        if not self.argv_log.exists():
            return []
        return [json.loads(line) for line in self.argv_log.read_text().splitlines()]


class AcknowledgementTests(SignInCase):
    def test_the_typed_word_is_recorded_with_the_text_hash(self) -> None:
        self.assertIsNone(signin.current_ack(self.runtime.environ, "claude"))
        for typed in ("", "yes", "Personal use"):
            with self.subTest(typed=typed), self.assertRaisesRegex(model.Refused, "type personal"):
                signin.record_ack(self.runtime, "claude", typed)
        ack = self.acknowledge("claude")
        record = choices.read(self.runtime.environ).get("acknowledgements")["claude"]
        self.assertEqual(record, {"text_id": CLAUDE_ACK_ID,
                                  "text_sha256": signin.text_sha256(CLAUDE_ACK_TEXT),
                                  "acknowledged_at": "2026-10-03T00:00:00Z", "typed": "personal"})
        self.assertEqual(signin.current_ack(self.runtime.environ, "claude"), ack)
        # The other pool needs its own acknowledgement.
        self.assertIsNone(signin.current_ack(self.runtime.environ, "codex"))

    def test_a_changed_text_needs_a_fresh_acknowledgement(self) -> None:
        self.acknowledge("claude")
        changed = {**signin._ACK, "claude": (CLAUDE_ACK_ID, CLAUDE_ACK_TEXT + " (revised)")}
        with mock.patch.dict(signin._ACK, changed):
            self.assertIsNone(signin.current_ack(self.runtime.environ, "claude"))
        with mock.patch.dict(signin._ACK, {"claude": ("claude-account-personal-use/2", CLAUDE_ACK_TEXT)}):
            self.assertIsNone(signin.current_ack(self.runtime.environ, "claude"))
        self.assertIsNotNone(signin.current_ack(self.runtime.environ, "claude"))

    def test_a_read_only_runtime_records_nothing(self) -> None:
        self.runtime.allow_state_writes = False
        with self.assertRaisesRegex(model.Refused, "read-only"):
            signin.record_ack(self.runtime, "codex", "personal")

    def test_the_texts_state_independence_and_the_claude_terms(self) -> None:
        self.assertIn("not affiliated with Anthropic", CLAUDE_ACK_TEXT)
        self.assertIn("An Anthropic API key is the\nsupported alternative.", CLAUDE_ACK_TEXT)
        self.assertIn("separate from your Claude Code login", CLAUDE_ACK_TEXT)
        self.assertIn("not affiliated with OpenAI", CHATGPT_ACK_TEXT)


class LoginGateTests(SignInCase):
    def test_the_gateway_login_commands_refuse_without_a_current_acknowledgement(self) -> None:
        env = self.runtime.gateway_environ()
        executed = []
        for command, provider in (("claude-login", "anthropic"), ("codex-device-login", "openai")):
            with self.subTest(command=command):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = proxy.cmd_login(command, [], environ=env, execve=lambda *a: executed.append(a))
                self.assertEqual(code, 1)
                self.assertIn(f"sign in with claude-multi providers sign-in {provider}", err.getvalue())
        self.assertEqual(executed, [])
        self.assertFalse(self.auth.exists() and any(self.auth.iterdir()))
        self.acknowledge("codex")
        with contextlib.redirect_stderr(io.StringIO()):
            outcome = proxy.cmd_login("codex-device-login", [], environ=env, execve=lambda *a: "EXECUTED")
        self.assertEqual(outcome, "EXECUTED")

    def test_a_strict_build_never_offers_the_claude_sign_in(self) -> None:
        self.acknowledge("claude")
        with mock.patch.object(signin, "signin_policy", return_value="public-strict"):
            self.assertFalse(signin.pool_offered("claude"))
            self.assertTrue(signin.pool_offered("codex"))
            self.assertIn("not offered by this build", signin.login_refusal("claude-login", self.runtime.environ))
            self.acknowledge("codex")
            self.assertIsNone(signin.login_refusal("codex-device-login", self.runtime.environ))
            with self.assertRaisesRegex(model.Refused, "not offered by this build"):
                signin.plan_sign_in(self.runtime, "anthropic")
            code, _out, err = self.op(["providers", "sign-in", "anthropic"], "personal\n")
            self.assertEqual(code, 1)
            self.assertIn(texts.SIGNIN_STRICT, err)

    def test_the_policy_comes_from_the_release_identity(self) -> None:
        self.assertIn(signin.signin_policy(), signin.SIGNIN_POLICIES)


class MethodTests(SignInCase):
    def test_defaults(self) -> None:
        none = lambda _name: None  # noqa: E731
        self.assertEqual(signin.method_default("codex", {"DISPLAY": ":0"}), "device")
        self.assertEqual(signin.method_default("claude", {"SSH_CONNECTION": "x", "DISPLAY": ":0"}), "address")
        self.assertEqual(signin.method_default("claude", {}, platform="linux"), "address")
        self.assertEqual(signin.method_default("claude", {"WAYLAND_DISPLAY": "w"}, platform="linux"), "browser")
        self.assertEqual(signin.method_default("claude", {}, platform="darwin"), "browser")
        self.assertEqual(signin.method_default("claude", {}, wsl=True, which=none), "address")
        self.assertEqual(signin.method_default("claude", {}, wsl=True, which=lambda n: f"/usr/bin/{n}"), "browser")
        self.assertEqual(signin.opener_argv({}, which=lambda n: "/x/explorer.exe" if n == "explorer.exe" else None),
                         ["/x/explorer.exe"])

    def test_first_address(self) -> None:
        self.assertIsNone(signin.first_address("no address yet https://claude.ai/oau"))
        self.assertEqual(signin.first_address("Visit:\nhttps://claude.ai/oauth?x=1\nPaste"), "https://claude.ai/oauth?x=1")

    def test_account_providers_only(self) -> None:
        with self.assertRaisesRegex(model.Refused, "claude-multi providers set-key kimi"):
            signin.plan_sign_in(self.runtime, "kimi")
        with self.assertRaisesRegex(model.Refused, "no 'device' method"):
            signin.plan_sign_in(self.runtime, "anthropic", method="device")


class RunSignInTests(SignInCase):
    def plan(self, provider_id: str, **kwargs) -> signin.SignInPlan:
        with contextlib.redirect_stderr(io.StringIO()):
            return signin.plan_sign_in(self.runtime, provider_id, **kwargs)

    def test_the_plan_runs_the_pinned_program_without_a_credential_in_its_argv(self) -> None:
        plan = self.plan("anthropic", method="address")
        self.assertEqual(plan.invocation.argv[0], str(self.launcher))
        self.assertEqual(plan.invocation.argv[-2:], ("--claude-login", "--no-browser"))
        self.assertNotIn(str(self.launcher), repr(plan.invocation))
        self.assertNotIn("cli-test-dummy", " ".join(plan.invocation.argv))
        self.assertNotIn("cli-test-dummy", json.dumps(dict(plan.invocation.env)))
        self.assertEqual(signin.header(plan), CLAUDE_ADDRESS_HEADER)
        self.assertEqual(signin.header(self.plan("anthropic", method="browser")), CLAUDE_BROWSER_HEADER)
        self.assertEqual(signin.header(self.plan("openai")), CHATGPT_HEADER)

    def test_a_completed_claude_sign_in_is_judged_by_the_new_record(self) -> None:
        ack = self.acknowledge("claude")
        plan = self.plan("anthropic", method="address")

        def runner(invocation) -> int:
            # Ctrl-C does nothing here while the sign-in runs, yet it is
            # handled, not ignored: an ignored interrupt would be inherited.
            self.assertIs(signal.getsignal(signal.SIGINT), signin._interrupt_here)
            done = subprocess.run(list(invocation.argv), env=dict(invocation.env), cwd=invocation.cwd,
                                  input="http://localhost:54545/callback?code=fake&state=s\n", text=True,
                                  capture_output=True, check=False)
            self.assertIn("https://claude.ai/oauth/authorize", done.stdout)
            return done.returncode

        before = signal.getsignal(signal.SIGINT)
        with self.human():
            outcome = signin.run_sign_in(self.runtime, plan, ack=ack, runner=runner)
        self.assertIs(signal.getsignal(signal.SIGINT), before)
        self.assertEqual((outcome.status, outcome.added, outcome.accounts),
                         ("signed-in", ("user@example.com",), ("user@example.com",)))
        record = self.auth / "claude-user@example.com.json"
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        self.assertEqual(signin.outcome_lines(plan, outcome)[0],
                         "Signed in: user@example.com (Claude account). Its models are available within a few seconds.")
        self.assertIn("--no-browser", self.argv_runs()[-1])

    def test_exit_zero_without_a_record_is_not_a_sign_in(self) -> None:
        ack = self.acknowledge("claude")
        plan = self.plan("anthropic", method="address")

        def runner(invocation) -> int:
            return subprocess.run(list(invocation.argv), env=dict(invocation.env), cwd=invocation.cwd,
                                  input="\n", text=True, capture_output=True, check=False).returncode

        with self.human():
            outcome = signin.run_sign_in(self.runtime, plan, ack=ack, runner=runner)
        self.assertEqual((outcome.status, outcome.exit_code), ("unchanged", 0))
        self.assertEqual(signin.outcome_lines(plan, outcome, line=True),
                         (texts.SIGNIN_UNCHANGED_LINE.format(id="anthropic"),))

    def test_the_device_sign_in_runs_in_this_terminal(self) -> None:
        ack = self.acknowledge("codex")
        plan = self.plan("openai")
        with self.human(), quiet_stdio():
            outcome = signin.run_sign_in(self.runtime, plan, ack=ack)
        self.assertEqual(outcome.status, "signed-in")
        self.assertEqual(signin.accounts(self.runtime, "openai"), ("user@example.com",))

    def test_cancel_port_busy_and_a_missing_program(self) -> None:
        ack = self.acknowledge("codex")
        plan = self.plan("openai")
        with self.human():
            self.assertEqual(signin.run_sign_in(self.runtime, plan, ack=ack, runner=lambda _i: 130).status, "cancelled")
            self.assertEqual(signin.run_sign_in(self.runtime, plan, ack=ack,
                                                runner=lambda _i: -signal.SIGINT).status, "cancelled")
            busy = self.write_launcher("busy-gateway", {"FAKE_LOGIN_PORT_BUSY": "1"})
            self.runtime.environ["CLAUDE_MULTI_PROXY_BIN"] = str(busy)
            busy_plan = self.plan("openai")
            with quiet_stdio():
                outcome = signin.run_sign_in(self.runtime, busy_plan, ack=ack)
            self.assertEqual((outcome.status, outcome.exit_code), ("port-busy", signin.PORT_IN_USE_EXIT))

            def missing(_invocation) -> int:
                raise FileNotFoundError(2, "No such file or directory")

            outcome = signin.run_sign_in(self.runtime, plan, ack=ack, runner=missing)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(signin.outcome_lines(plan, outcome), ("Not signed in — No such file or directory",))
        self.assertEqual(signin.accounts(self.runtime, "openai"), ())

    def test_ctrl_c_stops_a_sign_in_program_that_handles_no_interrupt(self) -> None:
        ack = self.acknowledge("codex")
        seen = self.root / "child-disposition"
        plain = self.write_launcher("plain-gateway", {"FAKE_LOGIN_PLAIN": "1", "FAKE_LOGIN_SELF_INTERRUPT": "1",
                                                      "FAKE_LOGIN_DISPOSITION": str(seen)})
        self.runtime.environ["CLAUDE_MULTI_PROXY_BIN"] = str(plain)
        plan = self.plan("openai")
        before = signal.getsignal(signal.SIGINT)
        with self.human(), quiet_stdio():
            outcome = signin.run_sign_in(self.runtime, plan, ack=ack)
        self.assertEqual(seen.read_text(), "default")
        self.assertEqual((outcome.status, outcome.exit_code), ("cancelled", -signal.SIGINT))
        self.assertEqual(signin.accounts(self.runtime, "openai"), ())
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_a_busy_port_is_not_a_sign_in_even_when_a_record_is_refreshed_meanwhile(self) -> None:
        ack = self.acknowledge("codex")
        self.plan("openai")  # the gateway's private folders exist first
        existing = self.record("codex-c@example.com.json")
        plan = self.plan("openai")

        def runner(_invocation) -> int:
            # The running gateway refreshes the record while the sign-in
            # finds its port taken (size and time change; never read).
            with open(existing, "ab") as handle:
                handle.write(b" ")
            stamp = existing.stat().st_mtime_ns + 5_000_000_000
            os.utime(existing, ns=(stamp, stamp))
            return signin.PORT_IN_USE_EXIT

        with self.human():
            outcome = signin.run_sign_in(self.runtime, plan, ack=ack, runner=runner)
        self.assertEqual((outcome.status, outcome.added, outcome.exit_code),
                         ("port-busy", (), signin.PORT_IN_USE_EXIT))
        self.assertEqual(signin.outcome_lines(plan, outcome), (texts.SIGNIN_PORT,))
        self.runtime.signin_runner = runner
        code, out, _err = self.op(["providers", "sign-in", "openai"])
        self.assertEqual(code, 1)
        self.assertIn(texts.SIGNIN_PORT, out)

    def test_a_stale_or_missing_acknowledgement_refuses_before_running(self) -> None:
        plan = self.plan("openai")
        ran = []
        with self.human(), self.assertRaisesRegex(model.Refused, "providers sign-in openai"):
            signin.run_sign_in(self.runtime, plan, ack=None, runner=ran.append)
        ack = self.acknowledge("codex")
        choices.set_acknowledgement(self.runtime.environ, "codex", None)
        with self.human(), self.assertRaises(model.Refused):
            signin.run_sign_in(self.runtime, plan, ack=ack, runner=ran.append)
        self.assertEqual(ran, [])
        self.acknowledge("codex")
        with mock.patch.object(consent, "stdio_ttys", return_value=False), self.assertRaises(consent.ConsentRefused):
            signin.run_sign_in(self.runtime, plan, ack=signin.current_ack(self.runtime.environ, "codex"),
                               runner=ran.append)
        self.assertEqual(ran, [])

    def test_the_windows_opener_gets_the_first_address_as_one_argument(self) -> None:
        self.acknowledge("codex")
        plan = self.plan("openai")
        opened = self.root / "opened.jsonl"
        opener = self.root / "opener"
        opener.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c "
                          f"'import json,sys; open(sys.argv[1],\"a\").write(json.dumps(sys.argv[2:])+\"\\n\")' "
                          f"{shlex.quote(str(opened))} \"$@\"\n")
        opener.chmod(0o755)
        shown: list[str] = []
        code = signin._run_child(plan.invocation, (str(opener),), write=shown.append)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(opened.read_text()), ["https://auth.openai.com/codex/device"])
        self.assertIn(texts.SIGNIN_WSL_OPEN, "".join(shown))
        self.assertIn("FAKE-CODE", "".join(shown))


class AccountNamesTests(SignInCase):
    def test_names_come_from_file_names_and_contents_are_never_opened(self) -> None:
        first = self.record("claude-a@example.com.json", b'{"token": "never-read"}')
        self.record("codex-b@example.com.json")
        self.record("claude-notes.txt")
        os.symlink(first, self.auth / "claude-link@example.com.json")
        first.chmod(0)
        try:
            self.assertEqual(signin.accounts(self.runtime, "anthropic"), ("a@example.com",))
            self.assertEqual(signin.accounts(self.runtime, "openai"), ("b@example.com",))
        finally:
            first.chmod(0o600)
        with self.assertRaisesRegex(model.Refused, "uses an API key"):
            signin.accounts(self.runtime, "kimi")


class SignOutTests(SignInCase):
    def setUp(self) -> None:
        super().setUp()
        self.record("claude-a@example.com.json")
        self.record("claude-b@example.com.json")
        self.record("codex-c@example.com.json")

    def apply(self, plan, **kwargs):
        with self.human():
            return signin.apply_sign_out(self.runtime, plan, model.Confirmation.given(plan), **kwargs)

    def test_the_plan_names_accounts_backup_and_profiles(self) -> None:
        plan = signin.plan_sign_out(self.runtime, "anthropic")
        self.assertEqual(plan.accounts, ("a@example.com", "b@example.com"))
        self.assertEqual(plan.consent, "destructive")
        self.assertTrue(plan.guarded)
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
        self.assertTrue(plan.backup_display.endswith(f"auth.signed-out.claude.{today}"), plan.backup_display)
        self.assertEqual(plan.lines[0], "Signed in: a@example.com, b@example.com")
        for path in self.auth.glob("claude-*.json"):
            path.unlink()
        with self.assertRaisesRegex(model.Refused, texts.NOT_SIGNED_IN):
            signin.plan_sign_out(self.runtime, "anthropic")

    def test_backup_names_never_collide(self) -> None:
        day = datetime.date(2026, 10, 3)
        first = signin.backup_dir(self.auth, "claude", today=day)
        self.assertEqual(first.name, "auth.signed-out.claude.20261003")
        first.mkdir()
        self.assertEqual(signin.backup_dir(self.auth, "claude", today=day).name, "auth.signed-out.claude.20261003.2")

    def test_sign_out_moves_records_reloads_and_confirms_unlisted(self) -> None:
        plan = signin.plan_sign_out(self.runtime, "anthropic")
        outcome = self.apply(plan, served=lambda: frozenset({"other-model"}))
        self.assertTrue(outcome.complete, outcome)
        self.assertEqual(outcome.moved, ("claude-a@example.com.json", "claude-b@example.com.json"))
        self.assertEqual(signin.accounts(self.runtime, "anthropic"), ())
        self.assertEqual(signin.accounts(self.runtime, "openai"), ("c@example.com",))
        backup = Path(os.path.expanduser(outcome.backup.replace("~", str(self.runtime.home), 1)))
        self.assertEqual(sorted(p.name for p in backup.iterdir()), list(outcome.moved))
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual(outcome.reload, "reloaded")

    def test_still_listed_down_and_a_returning_record(self) -> None:
        plan = signin.plan_sign_out(self.runtime, "anthropic")
        alias = sorted(signin.pool_aliases(self.runtime, "claude"))[0]
        ticks = iter(range(100))

        def reappear(_runtime):
            self.record("claude-a@example.com.json")
            return "reloaded", ()

        outcome = self.apply(plan, served=lambda: frozenset({alias}), clock=lambda: float(next(ticks)),
                             sleep=lambda _s: None, reload=reappear)
        self.assertEqual((outcome.status, outcome.reappeared), ("still-listed", ("claude-a@example.com.json",)))
        self.assertFalse(outcome.complete)
        self.assertIn(texts.SIGNED_OUT_INCOMPLETE, outcome.lines)
        self.assertEqual(signin.accounts(self.runtime, "anthropic"), ())
        backup = self.auth.parent / Path(outcome.backup).name
        self.assertIn("claude-a@example.com.json.again", {p.name for p in backup.iterdir()})
        self.record("claude-z@example.com.json")
        plan = signin.plan_sign_out(self.runtime, "anthropic")
        outcome = self.apply(plan, served=lambda: None, reload=lambda _r: ("down", ()))
        self.assertEqual(outcome.status, "down")
        self.assertIn(texts.SIGNED_OUT_DOWN, outcome.lines)
        self.assertFalse(outcome.complete)

    def test_a_failed_move_is_undone_and_nothing_is_lost(self) -> None:
        plan = signin.plan_sign_out(self.runtime, "anthropic")
        forward = []

        def flaky(source: Path, target: Path) -> None:
            if ".signed-out." in target.parent.name:
                if forward:
                    raise OSError(5, "fixture move failure")
                forward.append(source.name)
            os.rename(source, target)

        with self.assertRaises(OSError):
            self.apply(plan, move=flaky, served=lambda: frozenset())
        self.assertEqual(signin.accounts(self.runtime, "anthropic"), ("a@example.com", "b@example.com"))
        self.assertEqual([p for p in self.auth.parent.iterdir() if ".signed-out." in p.name], [])

    def test_a_record_written_again_during_a_failed_sign_out_is_never_overwritten(self) -> None:
        plan = signin.plan_sign_out(self.runtime, "anthropic")
        first = self.auth / "claude-a@example.com.json"
        original = first.stat().st_ino
        forward: list[str] = []
        refreshed: list[os.stat_result] = []

        def flaky(source: Path, target: Path) -> None:
            if ".signed-out." in target.parent.name:
                if forward:
                    # A refresh writes the first record again, then moving
                    # the second one fails.
                    refreshed.append(self.record("claude-a@example.com.json", b'{"refreshed": true}').stat())
                    raise OSError(5, "fixture move failure")
                forward.append(source.name)
            os.rename(source, target)

        with self.assertRaises(signin.SignOutIncomplete) as caught:
            self.apply(plan, move=flaky, served=lambda: frozenset())
        backups = [p for p in self.auth.parent.iterdir() if ".signed-out." in p.name]
        self.assertEqual(len(backups), 1)
        message = str(caught.exception)
        self.assertIn(backups[0].name, message)
        self.assertIn("a@example.com", message)
        self.assertIn("claude-multi providers sign-out anthropic", message)
        # The newer record keeps its place; the older one stays in the backup.
        now = first.stat()
        self.assertEqual((now.st_ino, now.st_size), (refreshed[0].st_ino, refreshed[0].st_size))
        kept = backups[0] / "claude-a@example.com.json"
        self.assertEqual(kept.stat().st_ino, original)
        self.assertEqual(signin.accounts(self.runtime, "anthropic"), ("a@example.com", "b@example.com"))

    def recheck(self, failure: BaseException):
        """Both records come back during the reload; the second re-check move fails."""

        plan = signin.plan_sign_out(self.runtime, "anthropic")
        phase = {"recheck": False, "moves": 0}

        def reappear(_runtime):
            self.record("claude-a@example.com.json", b'{"again": 1}')
            self.record("claude-b@example.com.json", b'{"again": 2}')
            phase["recheck"] = True
            return "reloaded", ()

        def flaky(source: Path, target: Path) -> None:
            if phase["recheck"]:
                phase["moves"] += 1
                if phase["moves"] == 2:
                    raise failure
            os.rename(source, target)

        try:
            return self.apply(plan, move=flaky, reload=reappear, served=lambda: frozenset())
        except KeyboardInterrupt:  # never let it end the test run
            self.fail("the interrupt escaped the sign-out")

    def test_a_failed_recheck_puts_its_moves_back_and_says_what_stays_signed_in(self) -> None:
        for failure in (OSError(5, "fixture move failure"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                self.record("claude-a@example.com.json")
                self.record("claude-b@example.com.json")
                outcome = self.recheck(failure)
                self.assertFalse(outcome.complete)
                self.assertEqual(outcome.left, ("claude-a@example.com.json", "claude-b@example.com.json"))
                self.assertEqual(signin.accounts(self.runtime, "anthropic"), ("a@example.com", "b@example.com"))
                backup = self.auth.parent / Path(outcome.backup).name
                # The original records stay in the kept backup; nothing else.
                self.assertEqual(sorted(p.name for p in backup.iterdir()),
                                 ["claude-a@example.com.json", "claude-b@example.com.json"])
                self.assertIn(texts.SIGNED_OUT_INCOMPLETE, outcome.lines)
                self.assertTrue(any("stay signed in: a@example.com, b@example.com" in line
                                    and "claude-multi providers sign-out anthropic" in line
                                    for line in outcome.lines), outcome.lines)
                self.assertFalse(any(line.startswith("A record that came back") for line in outcome.lines))

    def no_hard_links(self, *, written_again: bool):
        """A filesystem without hard links (``os.link`` fails with
        EOPNOTSUPP); with ``written_again`` a refresh writes the record
        again right before any rename that would put a moved record back
        into the sign-in folder. Returns the patchers and the stat of every
        record written that way."""

        import errno

        real_rename = os.rename
        written: list[os.stat_result] = []

        def link(*_args, **_kwargs):
            raise OSError(errno.EOPNOTSUPP, "Operation not supported")

        def rename(source, target, *args, **kwargs):
            if written_again and Path(target).parent == self.auth and not os.path.lexists(target):
                written.append(self.record(Path(target).name, b'{"written": "again"}').stat())
            return real_rename(source, target, *args, **kwargs)

        return written, (mock.patch.object(os, "link", side_effect=link),
                         mock.patch.object(os, "rename", side_effect=rename))

    def backup_of(self) -> Path:
        backups = [p for p in self.auth.parent.iterdir() if ".signed-out." in p.name]
        self.assertEqual(len(backups), 1, backups)
        return backups[0]

    def test_a_failed_sign_out_without_hard_links_keeps_the_backup_and_overwrites_nothing(self) -> None:
        real_rename = os.rename
        for written_again in (False, True):
            with self.subTest(written_again=written_again):
                for leftover in self.auth.parent.iterdir():
                    if ".signed-out." in leftover.name:
                        for item in leftover.iterdir():
                            item.unlink()
                        leftover.rmdir()
                self.record("claude-a@example.com.json")
                self.record("claude-b@example.com.json")
                plan = signin.plan_sign_out(self.runtime, "anthropic")
                first = (self.auth / "claude-a@example.com.json").stat()
                forward: list[str] = []

                def flaky(source: Path, target: Path) -> None:
                    if forward:
                        raise OSError(5, "fixture move failure")  # the second record cannot move
                    forward.append(source.name)
                    real_rename(source, target)

                written, patchers = self.no_hard_links(written_again=written_again)
                with patchers[0], patchers[1]:
                    try:
                        self.apply(plan, move=flaky, served=lambda: frozenset())
                    except Exception as exc:  # whatever reaches the caller is the subject
                        failure = exc
                    else:
                        self.fail("the sign-out reported no failure")
                # Nothing was put back: the record stays in the kept backup.
                self.assertFalse(os.path.lexists(self.auth / "claude-a@example.com.json"))
                self.assertIsInstance(failure, signin.SignOutIncomplete)
                backup = self.backup_of()
                message = str(failure)
                self.assertIn(backup.name, message)
                self.assertIn("a@example.com", message)
                # The moved record stays in the kept backup (the same file, by
                # its metadata); nothing was put back over the sign-in folder.
                kept = (backup / "claude-a@example.com.json").stat()
                self.assertEqual((kept.st_ino, kept.st_size), (first.st_ino, first.st_size))
                self.assertEqual(written, [])  # no rename into the sign-in folder was attempted
                self.assertFalse(os.path.lexists(self.auth / "claude-a@example.com.json"))
                self.assertTrue((self.auth / "claude-b@example.com.json").exists())

    def test_a_failed_recheck_without_hard_links_keeps_what_it_moved_and_overwrites_nothing(self) -> None:
        for written_again in (False, True):
            with self.subTest(written_again=written_again):
                for leftover in self.auth.parent.iterdir():
                    if ".signed-out." in leftover.name:
                        for item in leftover.iterdir():
                            item.unlink()
                        leftover.rmdir()
                self.record("claude-a@example.com.json")
                self.record("claude-b@example.com.json")
                plan = signin.plan_sign_out(self.runtime, "anthropic")
                phase = {"recheck": False, "moves": 0}
                came_back: dict[str, os.stat_result] = {}
                real_rename = os.rename

                def reappear(_runtime):
                    for name in ("claude-a@example.com.json", "claude-b@example.com.json"):
                        came_back[name] = self.record(name, b'{"came": "back"}').stat()
                    phase["recheck"] = True
                    return "reloaded", ()

                def flaky(source: Path, target: Path) -> None:
                    if phase["recheck"]:
                        phase["moves"] += 1
                        if phase["moves"] == 2:
                            raise OSError(5, "fixture move failure")
                    real_rename(source, target)

                written, patchers = self.no_hard_links(written_again=written_again)
                with patchers[0], patchers[1]:
                    outcome = self.apply(plan, move=flaky, reload=reappear, served=lambda: frozenset())
                backup = self.backup_of()
                again = backup / "claude-a@example.com.json.again"
                # Nothing was put back over the sign-in folder: the record that
                # came back stays in the backup.
                self.assertFalse(os.path.lexists(self.auth / "claude-a@example.com.json"))
                self.assertTrue(os.path.lexists(again))
                self.assertFalse(outcome.complete)
                self.assertEqual(outcome.kept, ("claude-a@example.com.json",))
                self.assertEqual(outcome.left, ("claude-b@example.com.json",))
                self.assertTrue(any(backup.name in line and "a@example.com" in line and "kept" in line
                                    for line in outcome.lines), outcome.lines)
                self.assertIn(texts.SIGNED_OUT_INCOMPLETE, outcome.lines)
                # The record that came back stays in the backup (by its
                # metadata) and nothing replaced anything in the sign-in folder.
                self.assertEqual((again.stat().st_ino, again.stat().st_size),
                                 (came_back["claude-a@example.com.json"].st_ino,
                                  came_back["claude-a@example.com.json"].st_size))
                self.assertEqual(written, [])
                self.assertFalse(os.path.lexists(self.auth / "claude-a@example.com.json"))
                still = (self.auth / "claude-b@example.com.json").stat()
                self.assertEqual(still.st_ino, came_back["claude-b@example.com.json"].st_ino)

    def test_the_persistence_hold_and_a_stale_plan_refuse(self) -> None:
        plan = signin.plan_sign_out(self.runtime, "anthropic")
        with mock.patch.object(external, "persistence_held", return_value=("a save is in flight",)):
            with self.assertRaisesRegex(model.Refused, "a sign-in save is not confirmed yet"):
                self.apply(plan, served=lambda: frozenset())
        self.record("claude-new@example.com.json")
        with self.assertRaises(model.Stale):
            self.apply(plan, served=lambda: frozenset())
        self.assertEqual(len(signin.accounts(self.runtime, "anthropic")), 3)


class CommandTests(SignInCase):
    def test_sign_in_asks_for_the_word_once_per_text(self) -> None:
        runs = []

        def runner(invocation) -> int:
            runs.append(invocation)
            self.record("codex-c@example.com.json")
            return 0

        self.runtime.signin_runner = runner
        code, out, err = self.op(["providers", "sign-in", "openai"], "sure\n")
        self.assertEqual(code, 3)
        self.assertIn(CHATGPT_ACK_TEXT, err)
        self.assertIn(texts.ACK_WRONG, err)
        self.assertEqual(runs, [])
        code, out, err = self.op(["providers", "sign-in", "openai"], "personal\n")
        self.assertEqual(code, 0, err)
        self.assertIn(CHATGPT_HEADER, out)
        self.assertIn("Signed in: c@example.com (ChatGPT account)", out)
        # Already acknowledged: no text, no word.
        self.runtime.signin_runner = lambda _i: 0
        code, out, err = self.op(["providers", "sign-in", "openai"], "")
        self.assertEqual(code, 1, err)
        self.assertNotIn(CHATGPT_ACK_TEXT, err)
        self.assertIn("Try again: claude-multi providers sign-in openai", out)
        self.runtime.signin_runner = lambda _i: 130
        code, out, _err = self.op(["providers", "sign-in", "openai"])
        self.assertEqual(code, 130)
        self.assertIn(texts.SIGNIN_CANCELLED, out)

    def test_no_browser_and_session_refusal(self) -> None:
        self.acknowledge("claude")
        seen = []
        self.runtime.signin_runner = lambda invocation: seen.append(invocation.argv) or 0
        code, out, err = self.op(["providers", "sign-in", "anthropic", "--no-browser"])
        self.assertEqual(code, 1, err)
        self.assertEqual(seen[-1][-1], "--no-browser")
        self.assertIn(CLAUDE_ADDRESS_HEADER, out)
        code, _out, err = self.op(["providers", "sign-in", "anthropic"], env={"CLAUDECODE": "1"})
        self.assertEqual(code, 1)
        self.assertEqual(len(seen), 1)

    def test_sign_out_command(self) -> None:
        self.record("claude-a@example.com.json")
        code, out, err = self.op(["providers", "sign-out", "anthropic"], "n\n")
        self.assertEqual(code, 3)
        self.assertEqual(signin.accounts(self.runtime, "anthropic"), ("a@example.com",))
        self.served -= signin.pool_aliases(self.runtime, "claude")
        code, out, err = self.op(["providers", "sign-out", "anthropic"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("Signed out of your Claude account; the record(s) are kept in", out)
        self.assertIn("To sign in again: claude-multi providers sign-in anthropic.", out)
        code, _out, err = self.op(["providers", "sign-out", "anthropic", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn(texts.NOT_SIGNED_IN, err)

    def test_sign_out_still_listed_exits_1(self) -> None:
        self.record("claude-a@example.com.json")
        with mock.patch.object(signin, "VERIFY_SECONDS", 0.0):
            code, out, err = self.op(["providers", "sign-out", "anthropic", "--yes"])
        self.assertEqual(code, 1, err)
        self.assertIn("the gateway still lists Claude account models", out)
        self.assertIn(texts.SIGNED_OUT_INCOMPLETE, out)


if __name__ == "__main__":
    import unittest

    unittest.main()
