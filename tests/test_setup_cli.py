"""``claude-multi setup`` in line mode: --status, the modes and their
usage errors, the steps, --keys-file and --answers, with their exit
statuses (0 ok, 1 refused or not done, 2 usage, 3 declined, 130 cancelled)."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
from unittest import mock

import test_cli
from claude_multi import acquire, paths, pin, proxy, secret_store, state
from claude_multi.cli import consent as consent_mod
import claude_multi.cli.commands.providers as providers_cmd
import claude_multi.cli.commands.setup as setup_cmd
from claude_multi.setup import external, providers as setup_providers, status, texts

KIMI = "KIMI_CLAUDE_API_KEY"
DEEPSEEK = "DEEPSEEK_CLAUDE_API_KEY"
VERIFIED = external.ClaudeStatus("2.1.0", "verified", "verified copy", None)
MISSING = external.ClaudeStatus("2.1.0", "missing", "not installed", external.CLAUDE_FIX)
CLAUDE_BUILD = b"#!/bin/sh\n# a fixture build, never run\n" + bytes(range(256)) * 4


class SetupCase(test_cli.OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        self.claude_copy(VERIFIED)  # a verified owned Claude Code copy unless a test says otherwise

    def claude_copy(self, found: external.ClaudeStatus) -> None:
        """The owned Claude Code copy as the step list and the checks see it."""

        for name in ("claude_status", "claude_verified"):
            patcher = mock.patch.object(external, name, return_value=found)
            patcher.start()
            self.addCleanup(patcher.stop)

    def menu_number(self, entry_id: str) -> int:
        entries = [entry for entry in setup_providers.picker_entries(self.runtime) if entry.available]
        return 1 + next(index for index, entry in enumerate(entries) if entry.id == entry_id)

    def private_file(self, relative: str, data: bytes) -> Path:
        target = Path(self.runtime.environ["HOME"]) / relative
        state.ensure_private_dir(target.parent)
        state.atomic_write(target, data)
        return target

    def answers(self, document: object) -> Path:
        return self.private_file("answers/setup.json", json.dumps(document).encode())


class StatusTests(SetupCase):
    def test_status_lists_every_step_and_exits_by_readiness(self) -> None:
        code, out, err = self.op(["setup", "--status"], tty=False)
        rows = out.splitlines()
        self.assertEqual([row.split()[1] for row in rows], list(status.STEP_ORDER), out)
        self.assertIn("Claude Code 2.1.0, verified copy", rows[1])
        self.assertIn("Kimi (API key)", rows[3])
        self.assertEqual(code, 0 if status.snapshot(self.runtime).ready else 1, out + err)

    def test_status_with_the_claude_copy_missing_is_not_ready(self) -> None:
        self.claude_copy(MISSING)
        code, out, _err = self.op(["setup", "--status"], tty=False)
        self.assertEqual(code, 1)
        self.assertIn("Claude Code 2.1.0 not installed — claude-multi setup --step claude", out)

    def test_nothing_connected(self) -> None:
        state.atomic_write(self.secret_file, b"")
        code, _out, err = self.op(["providers", "disable", "llm-local"])  # the fixture's keyless server
        self.assertEqual(code, 0, err)
        self.served = set()
        code, out, _err = self.op(["setup", "--status"], tty=False)
        self.assertEqual(code, 1)
        self.assertIn("nothing connected yet — claude-multi setup --step providers", out)
        self.assertIn("after a provider is connected", out)
        self.assertEqual(status.should_open(self.runtime, explicit=False), "providers")
        self.assertIsNone(status.should_open(self.runtime, explicit=True))
        self.assertIsNone(status.should_open(self.runtime, explicit=False, resume=True))
        with mock.patch.dict(self.runtime.environ, {"CLAUDECODE": "1"}):
            self.assertIsNone(status.should_open(self.runtime, explicit=False))


class ModeTests(SetupCase):
    def test_usage_errors_exit_2(self) -> None:
        answers = self.answers({"version": 1})
        cases = [
            ["setup", "--status", "--answers", str(answers)],
            ["setup", "--keys-file", str(answers), "--step", "providers"],
            ["setup", "--step", "providers", "--proxy", "http://proxy.invalid:3128"],
            ["setup", "--claude-from", "/nowhere", "--proxy", "http://proxy.invalid:3128"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, _out, err = self.op(argv)
                self.assertEqual(code, 2, err)

    def test_off_a_terminal_a_bare_run_names_the_alternatives(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _out = self.run_cli(["setup"], interactive=False)
        self.assertEqual(code, 1)  # unavailable here, not an invalid command line
        self.assertIn(texts.SETUP_NEEDS_TERMINAL, err.getvalue())

    def test_a_cancel_keeps_finished_steps(self) -> None:
        with mock.patch.object(status, "snapshot", side_effect=KeyboardInterrupt):
            code, out, _err = self.op(["setup"])
        self.assertEqual(code, 130)
        self.assertIn(texts.SETUP_CANCELLED, out)


class StepTests(SetupCase):
    def test_providers_menu_connects_through_the_command_forms(self) -> None:
        number = self.menu_number("kimi:api-key")
        code, out, err = self.op(["setup", "--step", "providers"], f"{number}\ny\nkimi-new-dummy\n\n")
        self.assertEqual(code, 0, err)
        self.assertIn("Kimi API key saved (14 chars)", out)
        self.assertEqual(secret_store.default_store(self.runtime.gateway_environ()).get(KIMI), "kimi-new-dummy")
        self.assertNotIn("kimi-new-dummy", out + err)
        self.assertIn("Claude account — sign in (personal use)", out)
        code, out, err = self.op(["setup", "--step", "providers"], "99\nq\n")
        self.assertEqual(code, 0)
        self.assertIn(texts.SETUP_WRONG_CHOICE, out)

    def test_leaving_the_menu_after_saying_no_exits_3(self) -> None:
        number = self.menu_number("kimi:api-key")
        code, out, err = self.op(["setup", "--step", "providers"], f"{number}\nn\n\n")
        self.assertEqual(code, 3, out + err)
        self.assertEqual(secret_store.default_store(self.runtime.gateway_environ()).get(KIMI), "cli-test-dummy")
        # A later choice that works is what counts when leaving.
        code, out, err = self.op(["setup", "--step", "providers"], f"{number}\nn\n{number}\ny\nkimi-new-dummy\n\n")
        self.assertEqual(code, 0, out + err)

    def test_a_cancelled_sign_in_stops_setup_with_130(self) -> None:
        number = self.menu_number("openai:account")
        with mock.patch.object(providers_cmd, "_providers_sign_in", return_value=130) as sign_in:
            code, out, err = self.op(["setup", "--step", "providers"], f"{number}\n\n")
        self.assertEqual(code, 130, out + err)
        self.assertEqual(sign_in.call_count, 1)
        self.assertIn(texts.SETUP_CANCELLED, out)

    def test_a_bare_run_stops_at_a_cancelled_step(self) -> None:
        ran: list[str] = []

        def run_step(_runtime, step, **_kwargs):
            ran.append(step)
            return 130 if step == "test" else 0

        with mock.patch.object(setup_cmd, "_run_step", side_effect=run_step):
            code, out, _err = self.op(["setup", "--redo"])
        self.assertEqual(code, 130)
        self.assertEqual(ran[-1], "test")
        self.assertNotIn(texts.SETUP_DONE, out)

    def test_steps_that_need_a_person_refuse_inside_a_session(self) -> None:
        for step in ("providers", "test"):
            with self.subTest(step=step):
                code, _out, err = self.op(["setup", "--step", step], "1\n", env={"CLAUDECODE": "1"})
                self.assertEqual(code, 1)
                self.assertIn("CLAUDECODE set", err)

    def test_test_step_asks_once_and_a_no_is_not_a_failure(self) -> None:
        self.apply_and_serve()
        code, out, err = self.op(["setup", "--step", "test"], "n\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, [])
        linked = status.connected(self.runtime)
        self.assertIn("kimi", [item.provider_id for item in linked])
        code, out, err = self.op(["setup", "--step", "test"], "y\ny\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.calls), len(linked))
        self.assertEqual(err.count("Proceed? [y/N]"), 1)

    def test_check_step_prints_the_first_run_checks(self) -> None:
        code, out, _err = self.op(["setup", "--step", "check"])
        self.assertIn(texts.FIRSTRUN_HEADER, out)
        self.assertIn(texts.FIRSTRUN_INFO, out)
        self.assertIn(code, (0, 1))


class GatewayProxyStepTests(SetupCase):
    """``setup --step gateway --proxy URL | --no-proxy`` through the setup
    layer's one transaction: checked first, the exact undo registered before
    ``endpoint.json`` is written, and for a running gateway one served change
    (the render and its verified reload) that puts the document back exactly
    when the render is refused or the change is interrupted."""

    OLD = "http://old.proxy.invalid:3128"
    NEW = "http://new.proxy.invalid:3128"

    def setUp(self) -> None:
        super().setUp()
        from claude_multi import endpoint

        packaged = endpoint.port_of(self.runtime.catalog.docs["gateway"]["gateway"]["base_url"])
        endpoint.set_proxy(self.runtime.home, self.OLD, packaged_port=packaged)
        self.runtime.reload_catalog()
        self.path = endpoint.endpoint_path(self.runtime.home)
        self.before = self.path.read_bytes()

    def step(self, *argv: str, running: bool, **patches):
        from claude_multi import gateway_lifecycle as gl, service

        seen = gl.Observation(gl.OURS if running else gl.STOPPED, "fixture", None,
                              service.OwnerVerdict("ours" if running else "none", "fixture"), running)
        ready = mock.Mock(ok=True, exit_code=0, lines=lambda: ["gateway ready (fixture)"])
        with mock.patch.object(gl.Gateway, "observe", return_value=seen), \
                mock.patch.object(gl.Gateway, "ensure_locked", return_value=ready) as ensure:
            code, out, err = self.op(["setup", "--step", "gateway", *argv], tty=False)
        return code, out, err, ensure

    def proxy_now(self) -> str | None:
        from claude_multi import endpoint

        return endpoint.read_config(self.runtime.home).proxy_url

    def test_a_running_gateway_reloads_in_one_served_change(self) -> None:
        code, out, err, ensure = self.step("--proxy", self.NEW, running=True)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.proxy_now(), self.NEW)
        self.assertIn(f"gateway outbound proxy: {self.NEW}", out)
        self.assertIn("Applied — the gateway reloaded", out)
        self.runtime.verify_reload.assert_called_once()
        ensure.assert_not_called()  # it runs already: nothing is started

    def test_the_running_gateways_served_change_holds_no_start_lock(self) -> None:
        from claude_multi import service
        from claude_multi.platform import posix_fs

        held: list[bool | None] = []
        real = self.runtime.render_gateway

        def render(**kwargs):
            held.append(posix_fs.lock_held(service.start_lock_path(self.runtime.session_store.root)))
            return real(**kwargs)

        with mock.patch.object(self.runtime, "render_gateway", side_effect=render):
            code, out, err, _ensure = self.step("--proxy", self.NEW, running=True)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(held, [False])  # a launch needing the start lock meanwhile is not held up

    def test_a_stopped_gateway_records_the_proxy_then_starts(self) -> None:
        code, out, err, ensure = self.step("--no-proxy", running=False)
        self.assertEqual(code, 0, out + err)
        self.assertIsNone(self.proxy_now())
        self.assertIn("gateway outbound proxy: none (the gateway connects directly)", out)
        self.assertIn("gateway ready (fixture)", out)
        ensure.assert_called_once()
        self.runtime.verify_reload.assert_not_called()

    def test_a_refused_render_puts_the_document_back(self) -> None:
        with mock.patch.object(self.runtime, "render_gateway", side_effect=proxy.ProxyError("fixture render refused")):
            code, out, err, ensure = self.step("--proxy", self.NEW, running=True)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertIn("the gateway configuration was refused: fixture render refused", err)
        self.assertNotIn("gateway outbound proxy", out)
        self.runtime.verify_reload.assert_not_called()
        ensure.assert_not_called()

    def test_the_served_preflight_refuses_before_anything_is_written(self) -> None:
        from claude_multi.setup import model

        with mock.patch.object(setup_providers, "preflight", side_effect=model.Refused("fixture: live impact unknown")):
            code, out, err, _ensure = self.step("--proxy", self.NEW, running=True)
        self.assertEqual(code, 1, out + err)
        self.assertIn("fixture: live impact unknown", err)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_an_interrupt_after_the_bytes_were_replaced_is_put_back(self) -> None:
        # The undo is registered before the write: an interrupt that lands
        # right after the new document replaced the old one restores it.
        from claude_multi import endpoint

        real = endpoint.write_config

        def write_then_interrupt(home, config):
            real(home, config)
            raise KeyboardInterrupt

        for running in (False, True):
            with self.subTest(running=running), mock.patch.object(endpoint, "write_config", write_then_interrupt):
                code, _out, err, ensure = self.step("--proxy", self.NEW, running=running)
                self.assertEqual(code, 130, err)
                self.assertEqual(self.path.read_bytes(), self.before)
                ensure.assert_not_called()

    def sync_fails_once(self, failure: BaseException):
        """The folder sync right after the new ``endpoint.json`` replaced the
        old one fails once (a failing disk, or an interrupt landing there)."""

        real = state._fsync_directory
        failed: list[bool] = []

        def flaky(path) -> None:
            if Path(path) == self.path.parent and not failed:
                failed.append(True)
                raise failure
            real(path)

        return failed, mock.patch.object(state, "_fsync_directory", side_effect=flaky)

    def test_a_failure_or_interrupt_at_the_folder_sync_puts_the_document_back(self) -> None:
        cases = ((OSError(5, "fixture sync failure"), 1), (KeyboardInterrupt(), 130))
        for failure, expected in cases:
            for running in (False, True):
                with self.subTest(failure=type(failure).__name__, running=running):
                    self.runtime.verify_reload.reset_mock()
                    failed, patcher = self.sync_fails_once(failure)
                    with patcher:
                        code, out, err, ensure = self.step("--proxy", self.NEW, running=running)
                    self.assertEqual(failed, [True])  # it failed after the replacement, as intended
                    self.assertEqual(code, expected, out + err)
                    self.assertEqual(self.path.read_bytes(), self.before)
                    # The runtime reads the document as it was put back.
                    gateway = self.runtime.catalog.docs["gateway"]["gateway"]
                    self.assertEqual(gateway["cliproxy_static"]["proxy-url"], self.OLD)
                    self.assertNotIn("gateway outbound proxy", out)
                    self.runtime.verify_reload.assert_not_called()
                    ensure.assert_not_called()


class KeysFileTests(SetupCase):
    def setUp(self) -> None:
        super().setUp()
        self.runtime.environ.pop("CLAUDE_MULTI_SECRET_ENV")
        self.keys = self.private_file(".config/secrets/claude.env", b"KIMI_CLAUDE_API_KEY=pointed-dummy\n")

    def test_select_an_existing_key_file(self) -> None:
        code, out, err = self.op(["setup", "--keys-file", str(self.keys)], "n\n")
        self.assertEqual(code, 3, err)
        self.assertFalse(paths.secret_pointer_path(self.runtime.environ).exists())
        code, out, err = self.op(["setup", "--keys-file", str(self.keys)], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("API keys now come from ~/.config/secrets/claude.env (1 key name(s) found).", out)
        self.assertEqual(secret_store.secret_env_path(self.runtime.gateway_environ()), self.keys)
        self.assertNotIn("pointed-dummy", out + err)

    def test_refusals(self) -> None:
        code, _out, err = self.op(["setup", "--keys-file", str(self.keys)], "y\n", tty=False)
        self.assertEqual(code, 1)
        loose = self.private_file("keys.env", b"A_KEY=dummy\n")
        code, _out, err = self.op(["setup", "--keys-file", str(loose)], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("not your home folder itself", err)
        os.chmod(self.keys, 0o644)
        code, _out, err = self.op(["setup", "--keys-file", str(self.keys)], "y\n")
        self.assertEqual(code, 1)
        self.assertFalse(paths.secret_pointer_path(self.runtime.environ).exists())

    def test_an_environment_override_is_named(self) -> None:
        code, out, err = self.op(["setup", "--keys-file", str(self.keys)], "y\n",
                                 env={"CLAUDE_MULTI_SECRET_ENV": str(self.secret_file)})
        self.assertEqual(code, 0, err)
        self.assertIn(texts.KEYS_FILE_OVERRIDDEN, out)

    def select(self, how: str, **kwargs) -> tuple[int, str, str]:
        if how == "keys-file":
            return self.op(["setup", "--keys-file", str(self.keys)], "y\n", **kwargs)
        answers = self.private_file("answers/keys.json", json.dumps({"version": 1, "keys_file": str(self.keys)})
                                    .encode())
        return self.op(["setup", "--answers", str(answers)], "y\n", **kwargs)

    def unselect(self) -> None:
        pointer = paths.secret_pointer_path(self.runtime.environ)
        if pointer.exists():
            pointer.unlink()

    def test_an_answers_key_file_is_selected_like_keys_file(self) -> None:
        shown = "~/.config/secrets/claude.env"
        for how in ("keys-file", "answers"):
            with self.subTest(how=how), mock.patch.object(external, "service_installed", return_value=True):
                self.unselect()
                reloads = self.runtime.verify_reload.call_count
                code, out, err = self.select(how)
                self.assertEqual(code, 0, out + err)
                # The gateway was re-rendered with the file and its reload verified.
                self.assertEqual(self.runtime.verify_reload.call_count, reloads + 1)
                self.assertEqual(secret_store.secret_env_path(self.runtime.gateway_environ()), self.keys)
                self.assertIn(texts.KEYS_FILE_SET.format(path=shown, n=1), out)
                self.assertIn(texts.KEYS_FILE_SERVICE.format(path=shown), out)
                self.assertIn("Applied — the gateway reloaded", out)
                self.assertNotIn("pointed-dummy", out + err)
        for how in ("keys-file", "answers"):
            with self.subTest(how=how, override=True):
                self.unselect()
                code, out, err = self.select(how, env={"CLAUDE_MULTI_SECRET_ENV": str(self.secret_file)})
                self.assertEqual(code, 0, out + err)
                self.assertIn(texts.KEYS_FILE_OVERRIDDEN, out)

    def test_a_key_file_the_gateway_rejects_the_local_key_for_exits_1(self) -> None:
        rejected = proxy.ReloadResult("token_mismatch", "gateway: token mismatch")
        for how in ("keys-file", "answers"):
            with self.subTest(how=how), mock.patch.object(self.runtime, "verify_reload", return_value=rejected):
                self.unselect()
                code, out, err = self.select(how)
                self.assertEqual(code, 1, out + err)
                self.assertIn(texts.RELOAD_TEXT["token_mismatch"], out)
                # The selection is kept: the gateway, not the file, needs the fix.
                self.assertEqual(secret_store.secret_env_path(self.runtime.gateway_environ()), self.keys)


class AnswersTests(SetupCase):
    def test_refused_documents_exit_2_and_never_echo_a_value(self) -> None:
        token = "sk-" + "q" * 16
        cases = {
            "secret": ({"version": 1, "providers": [{"id": "kimi", "api_key_file": token}]}, "looks like a key"),
            "sign-in": ({"version": 1, "providers": [{"id": "openai", "sign_in": True}]},
                        "claude-multi providers sign-in openai"),
            "test": ({"version": 1, "providers": [{"id": "kimi", "test": True}]}, "claude-multi providers test kimi"),
            "inline key": ({"version": 1, "providers": [{"id": "kimi", "api_key": "abc"}]}, "use api_key_file"),
            "relative": ({"version": 1, "providers": [{"id": "kimi", "api_key_file": "k.txt"}]}, "absolute path"),
            "schema": ({"version": 2}, "invalid"),
        }
        for label, (document, expected) in cases.items():
            with self.subTest(case=label):
                code, _out, err = self.op(["setup", "--answers", str(self.answers(document))], "y\n")
                self.assertEqual(code, 2, err)
                self.assertIn(expected, err)
                self.assertNotIn(token, err)

    def document(self) -> dict:
        key = self.private_file("keys/deepseek.txt", b"deepseek-dummy-value\n")
        return {"version": 1, "providers": [{"id": "kimi", "enabled": False},
                                            {"id": "deepseek", "api_key_file": str(key)}]}

    def test_off_a_terminal_guarded_items_are_skipped_and_named(self) -> None:
        code, out, err = self.op(["setup", "--answers", str(self.answers(self.document()))], tty=False)
        self.assertEqual(code, 1, err)
        self.assertIn("kimi disabled", out)
        self.assertIn("skipped deepseek: needs a terminal", out)
        self.assertIn("applied 1, skipped 1, failed 0", out)
        self.assertIsNone(secret_store.default_store(self.runtime.gateway_environ()).get("DEEPSEEK_CLAUDE_API_KEY"))

    def test_on_a_terminal_one_confirmation_covers_the_plan(self) -> None:
        path = self.answers(self.document())
        code, out, err = self.op(["setup", "--answers", str(path)], "n\n")
        self.assertEqual(code, 3)
        self.assertIn(texts.ANSWERS_PLAN_HEAD, out)
        self.assertIn("deepseek: import the API key DEEPSEEK_CLAUDE_API_KEY from", out)
        code, out, err = self.op(["setup", "--answers", str(path)], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("applied 2, skipped 0, failed 0", out)
        store = secret_store.default_store(self.runtime.gateway_environ())
        self.assertEqual(store.get("DEEPSEEK_CLAUDE_API_KEY"), "deepseek-dummy-value")
        self.assertNotIn("deepseek-dummy-value", out + err)

    def test_a_document_that_does_not_parse_never_shows_what_it_holds(self) -> None:
        token = "sk-" + "q" * 24
        cases = {
            "duplicate key": b'{"version": 1, "%s": 1, "%s": 2}' % (token.encode(), token.encode()),
            "malformed": b'{"version": 1, "%s": }' % token.encode(),
            "trailing data": b'{"version": 1} "%s"' % token.encode(),
        }
        for label, raw in cases.items():
            with self.subTest(case=label):
                path = self.private_file("answers/broken.json", raw)
                code, out, err = self.op(["setup", "--answers", str(path)], "y\n")
                self.assertEqual(code, 2, err)
                self.assertIn("the answers file is not valid JSON", err)
                self.assertNotIn(token, out + err)
                self.assertNotIn("q" * 24, out + err)

    def test_an_unusable_api_key_file_refuses_the_whole_file_before_anything_runs(self) -> None:
        loose = self.private_file("keys/loose.txt", b"deepseek-dummy-value\n")
        os.chmod(loose, 0o644)
        cases = {
            "missing": str(Path(self.runtime.environ["HOME"]) / "keys" / "nowhere.txt"),
            "shared": str(loose),
            "two lines": str(self.private_file("keys/two.txt", b"one-dummy\ntwo-dummy\n")),
            "not a key": str(self.private_file("keys/spaces.txt", b"not a key value\n")),
        }
        before = self.state_bytes()
        for label, key_file in cases.items():
            for tty in (True, False):
                with self.subTest(case=label, tty=tty):
                    document = {"version": 1, "providers": [{"id": "kimi", "enabled": False},
                                                            {"id": "deepseek", "api_key_file": key_file}]}
                    code, out, err = self.op(["setup", "--answers", str(self.answers(document))], "y\n", tty=tty)
                    self.assertEqual(code, 2, out + err)
                    self.assertIn("(deepseek)", err)
                    self.assertNotIn(texts.ANSWERS_PLAN_HEAD, out)
                    self.assertNotIn("kimi disabled", out)
                    self.assertNotIn("dummy", out + err)
                    after = self.state_bytes()
                    after.pop(str(self.answers(document)), None)
                    before.pop(str(self.answers(document)), None)
                    self.assertEqual(after, before)

    def newco(self, **extra) -> dict:
        key = self.private_file("keys/newco.txt", b"newco-dummy-value\n")
        return {"id": "newco", "endpoint": {"kind": "anthropic-compatible",
                                            "base_url": "https://api.newco.example/anthropic", "auth": "header",
                                            "family": "newco", "listing": "https://api.newco.example/v1/models"},
                "api_key_file": str(key), **extra}

    def small_claude_pin(self) -> dict:
        platform = pin.host_platform()
        contract = {**self.runtime.catalog.docs["native-contract"], "verified": [{
            "version": "2.1.400",
            "platforms": {platform: {"sha256": hashlib.sha256(CLAUDE_BUILD).hexdigest(), "size": len(CLAUDE_BUILD)}},
            "manifest_sha256": "1" * 64, "signature_sha256": None,
            "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE", "verified_at": "2026-10-01",
            "evidence": {platform: "battery", "receipt_sha256": "2" * 64}}]}
        patcher = mock.patch.dict(self.runtime.catalog.docs, {"native-contract": contract})
        patcher.start()
        self.addCleanup(patcher.stop)
        return contract

    def fake_download(self, *, size_change: int = 0):
        """The acquisition, offline: it asks for consent with the download
        plan (its size changed by ``size_change``) and records a download."""

        downloads: list = []

        def acquire_claude(runtime, *, consent, progress, claude_from=None):
            plan = acquire.download_plan(runtime.catalog.docs["native-contract"], runtime.environ,
                                         pin.host_platform())
            plan = dataclasses.replace(plan, size=plan.size + size_change)
            if not consent(plan):
                return acquire.AcquireOutcome("declined", plan.version, plan.platform, None)
            downloads.append(plan)
            return acquire.AcquireOutcome("downloaded", plan.version, plan.platform, plan.destination, plan.url)

        return downloads, mock.patch.object(external, "acquire_claude", side_effect=acquire_claude)

    def test_the_plan_shows_route_approvals_and_the_download_size_before_the_question(self) -> None:
        self.small_claude_pin()
        document = {"version": 1, "claude": {"source": "auto", "accept_download": True},
                    "providers": [self.newco()]}
        before = self.state_bytes()
        downloads, patcher = self.fake_download()
        with patcher:
            code, out, err = self.op(["setup", "--answers", str(self.answers(document))], "n\n")
        self.assertEqual(code, 3, out + err)
        self.assertEqual(downloads, [])
        plan = out.split(texts.ANSWERS_PLAN_HEAD, 1)[1]
        for expected in (f"Size: {len(CLAUDE_BUILD)} bytes", "GET https://downloads.claude.ai/",
                         "Your API key NEWCO_API_KEY (saved) is sent only to:", "https://api.newco.example",
                         "as header x-api-key", "Model list: https://api.newco.example/v1/models",
                         "newco: import the API key NEWCO_API_KEY from",
                         "writes: providers.d/newco.json, operator-ledger.json"):
            self.assertIn(expected, plan)
        after = self.state_bytes()
        after.pop(str(self.answers(document)), None)
        before.pop(str(self.answers(document)), None)
        self.assertEqual(after, before)
        downloads, patcher = self.fake_download()
        with patcher:
            code, out, err = self.op(["setup", "--answers", str(self.answers(document))], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(downloads), 1)
        self.assertIn("applied 2, skipped 0, failed 0", out)
        self.assertEqual(secret_store.default_store(self.runtime.gateway_environ()).get("NEWCO_API_KEY"),
                         "newco-dummy-value")

    def test_a_download_that_differs_from_the_plan_is_refused(self) -> None:
        self.small_claude_pin()
        document = {"version": 1, "claude": {"source": "download", "accept_download": True}}
        downloads, patcher = self.fake_download(size_change=1)
        with patcher:
            code, out, err = self.op(["setup", "--answers", str(self.answers(document))], "y\n")
        self.assertEqual(code, 1, out + err)
        self.assertEqual(downloads, [])
        self.assertIn("changed while you were deciding", err)

    def test_a_key_saved_after_the_confirmation_is_never_overwritten(self) -> None:
        path = self.answers(self.document())
        store = secret_store.default_store(self.runtime.gateway_environ())

        def confirm(_text, *, input_stream):
            store.set(DEEPSEEK, "meanwhile-dummy")
            return True

        with mock.patch.object(consent_mod, "confirm", side_effect=confirm):
            code, out, err = self.op(["setup", "--answers", str(path)])
        self.assertEqual(code, 1, out + err)
        self.assertIn("what the plan showed for deepseek changed while you were deciding", err)
        self.assertEqual(secret_store.default_store(self.runtime.gateway_environ()).get(DEEPSEEK), "meanwhile-dummy")
        self.assertIn("kimi disabled", out)
        self.assertIn("applied 1, skipped 0, failed 1", out)

    def test_keys_planned_with_the_key_file_the_answers_select(self) -> None:
        self.runtime.environ.pop("CLAUDE_MULTI_SECRET_ENV")
        keys = self.private_file(".config/secrets/claude.env", b"KIMI_CLAUDE_API_KEY=pointed-dummy\n")
        key = self.private_file("keys/deepseek.txt", b"deepseek-dummy-value\n")
        document = {"version": 1, "keys_file": str(keys),
                    "providers": [{"id": "deepseek", "api_key_file": str(key)}]}
        code, out, err = self.op(["setup", "--answers", str(self.answers(document))], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"from {key} into ~/.config/secrets/claude.env", out)
        self.assertIn("applied 2, skipped 0, failed 0", out)
        self.assertEqual(secret_store.FileSecretStore(keys, environ=self.runtime.gateway_environ()).get(DEEPSEEK),
                         "deepseek-dummy-value")

    def test_a_starter_is_planned_against_the_transport_the_file_selects(self) -> None:
        from claude_multi.setup import answers

        # Nothing else connected: no saved key and the keyless server off.
        state.atomic_write(self.secret_file, b"")
        self.op(["providers", "disable", "llm-local"])
        key = self.private_file("keys/anthropic.txt", b"anthropic-dummy-value\n")
        document = {"version": 1, "providers": [{"id": "anthropic", "transport": "api-key",
                                                 "api_key_file": str(key)}],
                    "profile": "starter"}
        # Planned against the current account transport (nobody signed in),
        # the starter has no ready lead; against the selected key it has one.
        chain = answers._ChoicesChain(None)
        keyed = answers._keyed_providers(self.runtime, document)
        current, _ = answers._profile_item(self.runtime, "starter", chain, keyed)
        self.assertIsNotNone(current.problem, current.lines)
        before = self.state_bytes()
        projected, planned = answers._profile_item(self.runtime, "starter", chain, keyed,
                                                   answers._projected_transports(self.runtime, document))
        self.assertIsNone(projected.problem, projected.problem)
        self.assertTrue(planned, projected.lines)
        self.assertIn(texts.ANSWERS_STARTER_PLAN.format(name="starter"), projected.lines)
        self.assertEqual(self.state_bytes(), before, "planning writes nothing")
        # Applied in one run: the transport, then the starter it planned.
        code, out, err = self.op(["setup", "--answers", str(self.answers(document))], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("applied 2, skipped 0, failed 0", out)
        self.assertIn("starter", self.runtime.profiles.names())
        self.assertNotIn("anthropic-dummy-value", out + err)

    def test_a_top_level_keys_file_keys_the_selected_transport(self) -> None:
        from claude_multi import operator as operator_mod
        from claude_multi.setup import answers

        # Nothing else connected: no saved key and the keyless server off.
        state.atomic_write(self.secret_file, b"")
        self.op(["providers", "disable", "llm-local"])
        name = operator_mod.transport_alternative("anthropic", "api-key").secret_name
        keys = self.private_file(".config/secrets/claude.env", f"{name}=anthropic-dummy-value\n".encode())
        document = {"version": 1, "keys_file": str(keys),
                    "providers": [{"id": "anthropic", "transport": "api-key"}], "profile": "starter"}
        before = self.state_bytes()
        item, planned = answers._profile_item(self.runtime, "starter", answers._ChoicesChain(None),
                                              answers._keyed_providers(self.runtime, document),
                                              answers._projected_transports(self.runtime, document))
        # The key the file holds is the selected transport's: the starter has a ready lead.
        self.assertIsNone(item.problem, item.lines)
        self.assertTrue(planned)
        self.assertIn("anthropic", answers._keyed_providers(self.runtime, document))
        self.assertEqual(self.state_bytes(), before, "planning writes nothing")
        # Applied in one run: the key file, the transport, then the starter it planned.
        self.runtime.environ.pop("CLAUDE_MULTI_SECRET_ENV", None)
        code, out, err = self.op(["setup", "--answers", str(self.answers(document))], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("applied 3, skipped 0, failed 0", out)
        self.assertIn("starter", self.runtime.profiles.names())
        self.assertNotIn("anthropic-dummy-value", out + err)

    def test_a_key_for_an_endpoint_declared_earlier_in_the_file_refuses_the_file(self) -> None:
        endpoint = self.newco()
        key_file = endpoint.pop("api_key_file")
        document = {"version": 1, "providers": [endpoint, {"id": "newco", "api_key_file": key_file}]}
        path = self.answers(document)
        before = self.state_bytes()
        for tty in (True, False):
            with self.subTest(tty=tty):
                code, out, err = self.op(["setup", "--answers", str(path)], "y\n", tty=tty)
                self.assertEqual(code, 2, out + err)
                self.assertIn("providers[1] (newco):", err)
                self.assertIn("put api_key_file in the 'newco' endpoint entry", err)
                self.assertNotIn(texts.ANSWERS_PLAN_HEAD, out)
                after = self.state_bytes()
                self.assertEqual(after, before)  # nothing applied: the declaration neither
        # The same key inside the endpoint entry is one item, and it applies.
        code, out, err = self.op(["setup", "--answers", str(self.answers({"version": 1,
                                                                           "providers": [self.newco()]}))], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("applied 1, skipped 0, failed 0", out)
        self.assertEqual(secret_store.default_store(self.runtime.gateway_environ()).get("NEWCO_API_KEY"),
                         "newco-dummy-value")

    def test_other_items_for_a_provider_the_file_declares_refuse_the_file(self) -> None:
        cases = {
            "toggle in its own entry": ([self.newco(enabled=False)], "providers[0] (newco): enabled is applied"),
            "toggle later": ([self.newco(), {"id": "newco", "enabled": False}], "set this in a second setup run"),
            "declared twice": ([self.newco(), self.newco()], "providers[1] (newco): newco is declared only by"),
        }
        for label, (entries, expected) in cases.items():
            with self.subTest(case=label):
                code, out, err = self.op(["setup", "--answers", str(self.answers({"version": 1,
                                                                                   "providers": entries}))], "y\n")
                self.assertEqual(code, 2, out + err)
                self.assertIn(expected, err)
                self.assertNotIn(texts.ANSWERS_PLAN_HEAD, out)

    def test_a_default_chosen_during_the_confirmation_is_kept_and_reported(self) -> None:
        from claude_multi import choices

        path = self.answers({"version": 1, "default_profile": None})
        chosen = sorted(self.runtime.profiles.names())[0]

        def confirm(_text, *, input_stream):
            # Another window picks a default while this plan waits for its yes.
            choices.update(self.runtime.environ, default_profile=chosen)
            return True

        with mock.patch.object(consent_mod, "confirm", side_effect=confirm):
            code, out, err = self.op(["setup", "--answers", str(path)])
        self.assertEqual(code, 1, out + err)
        self.assertEqual(choices.read(self.runtime.environ).get("default_profile"), chosen)
        self.assertIn("what the plan showed for default_profile changed while you were deciding", err)
        self.assertIn("applied 0, skipped 0, failed 1", out)
        # The preview named the file it writes.
        shown = paths.display(choices.path(self.runtime.environ), self.runtime.environ)
        self.assertIn(f"writes: {shown}", out)

    def test_the_preview_shows_the_starter_and_the_files_it_writes(self) -> None:
        from claude_multi import choices

        path = self.answers({"version": 1, "profile": "starter"})
        before = self.state_bytes()
        code, out, err = self.op(["setup", "--answers", str(path)], "n\n")
        self.assertEqual(code, 3, out + err)
        after = self.state_bytes()
        after.pop(str(path), None)
        before.pop(str(path), None)
        self.assertEqual(after, before)
        plan = out.split(texts.ANSWERS_PLAN_HEAD, 1)[1]
        profile_file = paths.display(self.runtime.profiles.root / "starter.json", self.runtime.environ)
        choices_file = paths.display(choices.path(self.runtime.environ), self.runtime.environ)
        self.assertIn(f"writes: {profile_file}, {choices_file}", plan)
        self.assertIn("lead: ", plan)  # one row per slot
        self.assertIn(texts.ANSWERS_STARTER_PLAN.format(name="starter"), plan)
        code, out, err = self.op(["setup", "--answers", str(path)], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("starter", self.runtime.profiles.names())
        self.assertEqual(choices.read(self.runtime.environ).get("default_profile"), "starter")

    def test_the_starter_preview_counts_the_providers_this_file_gives_a_key(self) -> None:
        from claude_multi import choices

        store = secret_store.default_store(self.runtime.gateway_environ())
        store.delete(KIMI)  # nothing keyed is connected now
        key = self.private_file("keys/kimi.txt", b"kimi-dummy-value\n")
        path = self.answers({"version": 1, "providers": [{"id": "kimi", "api_key_file": str(key)}],
                             "profile": "starter"})
        code, out, err = self.op(["setup", "--answers", str(path)], "y\n")
        self.assertEqual(code, 0, out + err)
        plan = out.split(texts.ANSWERS_PLAN_HEAD, 1)[1].split("applied", 1)[0]
        self.assertIn(texts.ANSWERS_STARTER_PLAN.format(name="starter"), plan)
        self.assertNotIn("cannot run", plan)
        self.assertIn("applied 2, skipped 0, failed 0", out)
        self.assertEqual(choices.read(self.runtime.environ).get("default_profile"), "starter")
        self.assertEqual(store.get(KIMI), "kimi-dummy-value")

    def test_a_starter_that_would_differ_from_the_preview_is_not_written(self) -> None:
        from claude_multi import choices

        path = self.answers({"version": 1, "profile": "starter"})
        store = secret_store.default_store(self.runtime.gateway_environ())

        def confirm(_text, *, input_stream):
            store.delete(KIMI)  # a provider the preview counted on is gone by the yes
            return True

        with mock.patch.object(consent_mod, "confirm", side_effect=confirm):
            code, out, err = self.op(["setup", "--answers", str(path)])
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("starter", self.runtime.profiles.names())
        self.assertIsNone(choices.read(self.runtime.environ).get("default_profile"))
        self.assertIn("what the plan showed for profile changed while you were deciding", err)
        self.assertIn("applied 0, skipped 0, failed 1", out)

    def test_an_unreadable_answers_file(self) -> None:
        code, _out, err = self.op(["setup", "--answers", "/nonexistent/answers.json"])
        self.assertEqual(code, 2)
        self.assertIn("cannot be read", err)


if __name__ == "__main__":
    import unittest

    unittest.main()
