"""``claude-multi doctor --first-run``: nine ordered local checks, each
with one fix, the waiting propagation, the JSON form, the exit statuses,
and nothing written."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_cli
from claude_multi import paths, pin, scope, sessions, state
from claude_multi import gateway_lifecycle as gl
from claude_multi.cli import entry
import claude_multi.cli.runtime as runtime_mod
from claude_multi.setup import external, firstrun, model, signin, status, texts
from test_gateway_doctor import _GatewayDoctor
from test_gateway_lifecycle import NOW


def tree(root: Path) -> dict[str, tuple[int, int]]:
    found = {}
    for path in sorted(root.rglob("*")):
        try:
            info = os.lstat(path)
        except OSError:
            continue
        found[str(path.relative_to(root))] = (info.st_size, info.st_mtime_ns)
    return found


def account(state_text: str = "connected") -> status.Connection:
    return status.Connection("anthropic", "Anthropic", "catalog", "account", state_text, "claude",
                             ("a@example.com",), 3, "3/3", False, "account")


class FirstRunCase(test_cli.OperatorCommandCase):
    def checks(self, **kwargs) -> dict[str, firstrun.Check]:
        return {item.id: item for item in firstrun.checks(self.runtime, **kwargs)}

    def doctor(self, *argv: str) -> tuple[int, str, str]:
        return self.op(["doctor", "--first-run", *argv], tty=False)


class OrderTests(FirstRunCase):
    def test_nine_checks_in_order_every_one_runs(self) -> None:
        items = firstrun.checks(self.runtime)
        self.assertEqual([item.id for item in items], list(firstrun.ORDER))
        self.assertEqual(len(items), 9)
        for item in items:
            if item.state in ("fail", "attention"):
                self.assertIsNotNone(item.fix, item)

    def test_nothing_connected_makes_models_and_profile_wait(self) -> None:
        found = self.checks(connections=())
        self.assertEqual((found["providers"].state, found["providers"].fix.cli),
                         ("fail", "claude-multi setup --step providers"))
        self.assertEqual(found["models"].state, "waiting")
        self.assertEqual(found["models"].detail, "waits for a provider")
        self.assertEqual(found["profile"].state, "waiting")
        summary = firstrun.summary(firstrun.checks(self.runtime, connections=()))
        self.assertTrue(summary.startswith("Not ready:"), summary)

    def test_a_signed_in_account_without_a_current_acknowledgement_needs_attention(self) -> None:
        found = self.checks(connections=(account(),))
        self.assertEqual(found["providers"].state, "attention")
        self.assertEqual(found["providers"].fix.cli, "claude-multi providers sign-in anthropic")
        signin.record_ack(self.runtime, "claude", "personal")
        self.assertEqual(self.checks(connections=(account(),))["providers"].state, "ok")

    def test_the_claude_copy(self) -> None:
        with mock.patch.object(external, "claude_verified", return_value=external.ClaudeStatus(
                "2.1.0", "missing", "not installed", external.CLAUDE_FIX)):
            found = self.checks()
        self.assertEqual((found["claude"].title, found["claude"].state), ("Claude Code 2.1.0", "fail"))
        self.assertEqual(found["claude"].fix.argv, ("claude-multi", "setup", "--step", "claude"))

    def test_a_policy_that_blocks(self) -> None:
        blocked = (("block", "allowManagedHooksOnly blocks the session hooks", model.Fix(None, "ask your administrator")),)
        with mock.patch.object(external, "managed_policy_findings", return_value=blocked):
            found = self.checks()
        self.assertEqual((found["policy"].state, found["policy"].fix.cli), ("fail", "ask your administrator"))

    def test_missing_hook_helpers(self) -> None:
        found = self.checks()
        if found["hooks"].state == "fail":
            self.assertEqual(found["hooks"].fix.cli, "claude-multi doctor --repair-all")


class KeyFileTests(FirstRunCase):
    def test_a_shared_key_file_names_chmod(self) -> None:
        os.chmod(self.secret_file, 0o644)
        found = self.checks()["files"]
        self.assertEqual(found.state, "fail")
        self.assertIn("readable by others", found.detail)
        self.assertTrue(found.fix.cli.startswith("chmod 600 "), found.fix)

    def test_a_malformed_line_is_named_without_its_value(self) -> None:
        state.atomic_write(self.secret_file, b"KIMI_CLAUDE_API_KEY=cli-test-dummy\nnot-an-assignment\n")
        found = self.checks()["files"]
        self.assertEqual(found.state, "fail")
        self.assertIn("line 2 is not NAME=value", found.detail)
        self.assertNotIn("cli-test-dummy", found.detail)

    def test_a_bad_pointer_is_a_files_failure(self) -> None:
        env = dict(self.runtime.environ)
        env.pop("CLAUDE_MULTI_SECRET_ENV")
        with mock.patch.dict(self.runtime.environ, env, clear=True):
            from claude_multi import paths

            target = paths.secret_pointer_path(self.runtime.environ)
            state.ensure_private_dir(target.parent)
            state.atomic_write(target, b'{"version": 1, "path": "~/elsewhere.env"}')
            found = self.checks()["files"]
        self.assertEqual(found.state, "fail")
        self.assertIn("key file pointer", found.detail)
        self.assertEqual(found.fix.cli, "fix or delete ~/.config/claude-multi/secret-file.json")


class CommandTests(FirstRunCase):
    def test_text_and_exit_status(self) -> None:
        code, out, err = self.doctor()
        lines = out.splitlines()
        self.assertEqual(lines[0], texts.FIRSTRUN_HEADER)
        self.assertIn(texts.FIRSTRUN_INFO, out)
        self.assertIn("Gateway quota reads:", out)
        expected = 1 if firstrun.failing(firstrun.checks(self.runtime)) else 0
        self.assertEqual(code, expected, err)
        self.assertEqual(lines[-1], firstrun.summary(firstrun.checks(self.runtime)))

    def test_json(self) -> None:
        code, out, _err = self.doctor("--json")
        document = json.loads(out)
        self.assertEqual(document["version"], 1)
        self.assertEqual([item["id"] for item in document["items"]], list(firstrun.ORDER))
        self.assertEqual(document["ready"], code == 0)
        for item in document["items"]:
            self.assertEqual(set(item), {"id", "title", "state", "detail", "fix", "required"})
            if item["fix"] is not None:
                self.assertEqual(set(item["fix"]), {"text", "argv"})
        if not document["ready"]:
            self.assertEqual(document["next"], next(i["id"] for i in document["items"]
                                                    if i["state"] in ("fail", "waiting")))

    def test_it_cannot_accompany_a_repair(self) -> None:
        code, _out, err = self.doctor("--repair-all")
        self.assertEqual(code, 2)
        self.assertIn("--first-run cannot accompany --repair-all", err)

    def test_nothing_is_written(self) -> None:
        self.doctor()  # the runtime's own first use settles
        before = tree(self.root)
        self.doctor()
        self.doctor("--json")
        self.assertEqual(tree(self.root), before)

    def test_tui_lines_name_keys(self) -> None:
        items = firstrun.checks(self.runtime, connections=())
        tui = "\n".join(firstrun.render_lines(items, surface="tui"))
        cli = "\n".join(firstrun.render_lines(items, surface="cli"))
        self.assertIn("fix: A on Get started", tui)
        self.assertIn("fix: claude-multi setup --step providers", cli)


CLAUDE_BUILD = b"#!/bin/sh\n# a fixture build, never run\n" + bytes(range(256)) * 4


class ClaudeCopyTests(FirstRunCase):
    """Only claude-multi's own copy with the pinned size and sha256 passes."""

    VERSION = "2.1.400"

    def setUp(self) -> None:
        super().setUp()
        self.platform = pin.host_platform()
        contract = {**self.runtime.catalog.docs["native-contract"], "verified": [{
            "version": self.VERSION,
            "platforms": {self.platform: {"sha256": hashlib.sha256(CLAUDE_BUILD).hexdigest(),
                                          "size": len(CLAUDE_BUILD)}},
            "manifest_sha256": "1" * 64, "signature_sha256": None,
            "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE", "verified_at": "2026-10-01",
            "evidence": {self.platform: "battery", "receipt_sha256": "2" * 64},
        }]}
        patcher = mock.patch.dict(self.runtime.catalog.docs, {"native-contract": contract})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owned = pin.owned_path(self.runtime.environ, self.VERSION, self.platform)

    def put(self, data: bytes, where: Path, mode: int = 0o755) -> None:
        state.ensure_private_dir(where.parent.parent)
        state.ensure_private_dir(where.parent)
        where.write_bytes(data)
        where.chmod(mode)

    def claude(self) -> firstrun.Check:
        return self.checks(connections=())["claude"]

    def test_the_pinned_copy_passes(self) -> None:
        self.put(CLAUDE_BUILD, self.owned)
        found = self.claude()
        self.assertEqual((found.title, found.state, found.detail),
                         (f"Claude Code {self.VERSION}", "ok", "verified copy"))

    def test_a_copy_of_the_pinned_size_with_another_digest_fails(self) -> None:
        self.put(b"x" * len(CLAUDE_BUILD), self.owned)
        found = self.claude()
        self.assertEqual(found.state, "fail")
        self.assertIn("does not match", found.detail)
        self.assertEqual(found.fix.cli, "claude-multi setup --step claude")
        # Get started and setup --status show the same verdict.
        self.assertEqual(status.snapshot(self.runtime).step("claude").state, "blocked")

    def test_a_kept_copy_waiting_to_be_put_in_place_is_not_a_verified_copy(self) -> None:
        retained = paths.retained_root(self.runtime.environ)
        state.ensure_private_dir(retained.parent)
        state.ensure_private_dir(retained)
        (retained / self.VERSION).write_bytes(CLAUDE_BUILD)
        found = self.claude()
        self.assertEqual((found.state, found.detail), ("fail", "not set up for claude-multi"))
        self.assertEqual(found.fix.argv, ("claude-multi", "setup", "--step", "claude"))
        self.assertFalse(self.owned.exists())  # nothing was put in place


class GatewayStartTests(_GatewayDoctor):
    """A stopped gateway starts when needed only when it can: its program
    is installed and its automatic starts are not paused. Nothing is
    started or written by the check."""

    def setUp(self) -> None:
        super().setUp()
        empty = self.root / "empty-path"
        empty.mkdir()
        self.runtime.environ.pop("CLAUDE_MULTI_PROXY_BIN", None)
        self.runtime.environ["PATH"] = str(empty)

    def program(self, mode: int = 0o755) -> Path:
        binary = self.root / "gateway-bin" / "cli-proxy-api"
        binary.parent.mkdir(exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 1\n")
        binary.chmod(mode)
        self.runtime.environ["CLAUDE_MULTI_PROXY_BIN"] = str(binary)
        return binary

    def gateway(self) -> firstrun.Check:
        before = sorted(path.name for path in self.workdir.iterdir())
        found = {item.id: item for item in firstrun.checks(self.runtime, connections=())}["gateway"]
        self.assertEqual(self.world.spawned, [])
        self.assertEqual(sorted(path.name for path in self.workdir.iterdir()), before)
        return found

    def test_a_stopped_gateway_with_its_program_starts_when_needed(self) -> None:
        self.program()
        found = self.gateway()
        self.assertEqual((found.state, found.detail), ("ok", "starts when needed"))

    def test_a_missing_program_fails_with_its_fix(self) -> None:
        found = self.gateway()
        self.assertEqual(found.state, "fail")
        self.assertIn("the gateway program (cli-proxy-api) is not installed", found.detail)
        self.assertEqual(found.fix.cli, "reinstall claude-multi")
        self.assertEqual(status.snapshot(self.runtime).step("gateway").state, "blocked")

    def test_a_program_that_cannot_run_fails(self) -> None:
        self.program(0o644)
        found = self.gateway()
        self.assertEqual(found.state, "fail")
        self.assertIn("is not an executable file", found.detail)

    def test_paused_automatic_starts_fail_with_their_fix(self) -> None:
        self.program()
        state.atomic_write(self.workdir / gl.START_HISTORY, (
            '{"version": 1, "starts": [%s]}' % ", ".join(
                f'"{(NOW - datetime.timedelta(minutes=m)).isoformat()}"' for m in (1, 2, 3))).encode())
        found = self.gateway()
        self.assertEqual(found.state, "fail")
        self.assertIn("automatic starts are paused: the gateway was started 3 times in the last 10 minutes",
                      found.detail)
        self.assertIn("claude-multi gateway start", found.fix.cli)
        self.assertIn("claude-multi gateway logs", found.fix.cli)


def _home_tree(root: Path) -> dict[str, tuple[int, int, bytes]]:
    found = {}
    for path in sorted(root.rglob("*")):
        info = os.lstat(path)
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        found[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, data)
    return found


class EntrypointTests(unittest.TestCase):
    """The checks through the real entry point, which builds the runtime
    itself: neither the text nor the JSON form creates, removes or
    refreshes anything (fixture loopback seams; nothing leaves the host)."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-first-run-entry-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "dumb",
                    "LANG": "C.UTF-8"}

    def first_run(self, *argv: str) -> tuple[int, str, list[dict]]:
        built: list[dict] = []
        real = runtime_mod.Runtime

        def factory(**kwargs):
            built.append(kwargs)
            return real(**kwargs, health_get=lambda _base, _path: 503,
                        served_models_callback=lambda _gateway, _token: (None, 503))

        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(runtime_mod, "Runtime", side_effect=factory), \
                mock.patch("socket.socket.connect", side_effect=AssertionError("no connection is made")), \
                contextlib.redirect_stderr(io.StringIO()):
            code = entry.main(["doctor", "--first-run", *argv], output_stream=out, interactive=False)
        return code, out.getvalue(), built

    def test_an_error_before_the_checks_keeps_the_first_run_json_shape(self) -> None:
        from claude_multi import errors

        failure = errors.ClaudeMultiError("fixture: the packaged catalog cannot be read",
                                          remedy="reinstall claude-multi")
        for argv, first_run in ((["doctor", "--first-run", "--json"], True), (["doctor", "--json"], False)):
            with self.subTest(argv=argv):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, self.env, clear=True), \
                        mock.patch.object(runtime_mod, "Runtime", side_effect=failure), \
                        contextlib.redirect_stderr(err):
                    code = entry.main(argv, output_stream=out, interactive=False)
                self.assertEqual(code, 1)
                self.assertIn("claude-multi: fixture: the packaged catalog cannot be read", err.getvalue())
                document = json.loads(out.getvalue())
                if first_run:
                    self.assertEqual(document, {
                        "version": 1, "ready": False, "next": None, "items": [], "info": [texts.FIRSTRUN_INFO],
                        "error": "fixture: the packaged catalog cannot be read\n  fix: reinstall claude-multi"})
                else:  # the ordinary report keeps its own error form
                    self.assertEqual(document["report"], "doctor")
                    self.assertNotIn("items", document)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_an_empty_home_stays_empty(self) -> None:
        for argv in ((), ("--json",)):
            with self.subTest(argv=argv):
                code, out, built = self.first_run(*argv)
                self.assertEqual(code, 1, out)
                self.assertEqual(list(self.home.iterdir()), [])
                self.assertEqual(len(built), 1)
                self.assertFalse(built[0]["allow_state_writes"])
                self.assertFalse(built[0]["refresh_shims"])
                if argv:
                    hooks = next(item for item in json.loads(out)["items"] if item["id"] == "hooks")
                else:
                    self.assertIn(texts.FIRSTRUN_HEADER, out)
                    hooks = None
                if hooks is not None:
                    self.assertEqual((hooks["state"], hooks["fix"]["text"]),
                                     ("fail", "claude-multi doctor --repair-all"))

    def test_the_helpers_of_an_existing_install_are_never_repointed(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=True):
            root = sessions.state_root()
        state.ensure_private_dir(root.parent.parent)
        state.ensure_private_dir(root.parent)
        state.ensure_private_dir(root)
        scope.ensure_hook_shim(root, "/elsewhere/bin/claude-multi")
        scope.ensure_hook_shim_v3(root, "/elsewhere/bin/claude-multi")
        scope.ensure_token_helper_command(root, self.env, "/elsewhere/bin/claude-multi")
        before = _home_tree(self.home)
        for argv in ((), ("--json",)):
            with self.subTest(argv=argv):
                self.first_run(*argv)
                self.assertEqual(_home_tree(self.home), before)


if __name__ == "__main__":
    unittest.main()


class FreshHomeInertTests(unittest.TestCase):
    """A new home set up from nothing, through the real entry point: setup
    (done, declined and interrupted) and doctor (ordinary and first-run,
    text and JSON) create no state of an earlier release's format and give
    no migration or restore advice. Offline: no connection is made and the
    Claude Code download is declined or never offered."""

    # Files and folders only an earlier release's state has.
    OBSOLETE = re.compile(r"(^|/)(compositions|custom\.json|native-contract\.json|state-version)$"
                          r"|\.v3\.json$|\.ordinary$|\.v3$")
    ADVICE = re.compile(r"restore-2x|claude-multi migrate|profile migrate|compositions?\b")
    BUILD = b"#!/bin/sh\n# a fixture build, never run\n" + bytes(range(256)) * 4

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-fresh-home-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "TERM": "dumb", "LANG": "C.UTF-8"}
        self.contract: dict | None = None

    def small_pin(self, runtime: runtime_mod.Runtime) -> None:
        """A pin whose build is :attr:`BUILD` (a copy from a file then works offline)."""

        platform = pin.host_platform()
        runtime.catalog.docs["native-contract"] = {**runtime.catalog.docs["native-contract"], "verified": [{
            "version": "2.1.400",
            "platforms": {platform: {"sha256": hashlib.sha256(self.BUILD).hexdigest(), "size": len(self.BUILD)}},
            "manifest_sha256": "1" * 64, "signature_sha256": None,
            "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE", "verified_at": "2026-10-01",
            "evidence": {platform: "battery", "receipt_sha256": "2" * 64}}]}

    def run_cli(self, *argv: str, answers: io.StringIO | None = None,
                small_pin: bool = False) -> tuple[int, str]:
        from claude_multi import acquire
        from claude_multi.cli import consent

        real = runtime_mod.Runtime

        def factory(**kwargs):
            runtime = real(**kwargs, health_get=lambda _base, _path: 503,
                           served_models_callback=lambda _gateway, _token: (None, 503))
            if small_pin:
                self.small_pin(runtime)
            return runtime

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(runtime_mod, "Runtime", side_effect=factory), \
                mock.patch.object(consent, "stdio_ttys", return_value=True), \
                mock.patch.object(acquire, "_open", side_effect=AssertionError("no download in this test")), \
                mock.patch("socket.socket.connect", side_effect=AssertionError("no connection is made")), \
                contextlib.redirect_stderr(err):
            code = entry.main(list(argv), input_stream=answers or io.StringIO(""), output_stream=out,
                              interactive=True)
        return code, out.getvalue() + err.getvalue()

    def assertInert(self, text: str) -> None:
        leftovers = [str(path.relative_to(self.home)) for path in self.home.rglob("*")
                     if self.OBSOLETE.search(str(path.relative_to(self.home)))]
        self.assertEqual(leftovers, [])
        self.assertIsNone(self.ADVICE.search(text), text)

    def test_doctor_on_a_new_home(self) -> None:
        for argv in (("doctor", "--first-run"), ("doctor", "--first-run", "--json"), ("doctor", "--json"),
                     ("doctor",), ("setup", "--status")):
            with self.subTest(argv=argv):
                _code, text = self.run_cli(*argv)
                self.assertInert(text)

    def test_declined_interrupted_and_finished_setup(self) -> None:
        class Interrupted(io.StringIO):
            def readline(self, *_args) -> str:  # Ctrl-C at the question
                raise KeyboardInterrupt

        code, text = self.run_cli("setup", "--step", "claude", answers=io.StringIO("n\n"))
        self.assertEqual(code, 3, text)  # the download was offered and declined
        self.assertInert(text)
        code, text = self.run_cli("setup", "--step", "claude", answers=Interrupted())
        self.assertEqual(code, 130, text)
        self.assertInert(text)
        build = self.root / "claude"
        build.write_bytes(self.BUILD)
        build.chmod(0o755)
        code, text = self.run_cli("setup", "--step", "claude", "--claude-from", str(build), small_pin=True)
        self.assertEqual(code, 0, text)
        self.assertIn("Claude Code 2.1.400 copied from", text)
        self.assertInert(text)
        for argv in (("doctor", "--first-run"), ("doctor",)):
            with self.subTest(argv=argv):
                _code, text = self.run_cli(*argv)
                self.assertInert(text)
