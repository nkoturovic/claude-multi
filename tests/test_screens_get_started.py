"""Get started in the full-screen UI: the step list read from real state,
the focus, the step views that write only after a confirmation, the
refusals (a resume card, a read-only build, a Claude Code session) and how
it ends. A fixture gateway on a temp home; no provider request is sent."""

from __future__ import annotations

import hashlib
import io
import unittest
from pathlib import Path
from unittest import mock

import test_cli
from test_tui import BACKSPACE, DOWN, END, HOME, RIGHT, UP, FakeWindow
from claude_multi import tui
from claude_multi.setup import external, model, status
import claude_multi.cli.consent as consent
import claude_multi.cli.screens.get_started as get_started
import claude_multi.cli.text as cli_text

ESC = "\x1b"
ENTER = "\n"
CTRL_C = "\x03"


class GetStartedCase(test_cli.OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        for name in consent.SESSION_MARKERS:
            self.runtime.environ.pop(name, None)
        patches = (
            mock.patch.object(consent, "stdio_ttys", return_value=True),
            # A verified owned Claude Code copy: the step list never probes the host.
            mock.patch.object(external, "claude_status", return_value=external.ClaudeStatus(
                "2.1.0", "verified", "verified copy", None)),
            mock.patch.object(external, "claude_verified", return_value=external.ClaudeStatus(
                "2.1.0", "verified", "verified copy", None)),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def screen(self, **kwargs) -> get_started._GetStartedScreen:
        return get_started._GetStartedScreen(self.runtime, tui.MONO_PALETTE, **kwargs)

    def run_screen(self, screen, keys, *, height=24, width=80):
        win = FakeWindow(list(keys), height=height, width=width)
        return screen.run(win), win

    def ids(self) -> list[str]:
        return [step.id for step in status.snapshot(self.runtime).steps]

    def steps_to(self, screen, step_id: str) -> list[str]:
        return [HOME] + [DOWN] * self.ids().index(step_id)


class StepListTests(GetStartedCase):
    def test_rows_marks_and_the_first_unfinished_focus(self) -> None:
        state = status.snapshot(self.runtime)
        rows = get_started.step_rows(state)
        self.assertEqual([sid for sid, _text in rows], [step.id for step in state.steps])
        for number, ((_sid, text), step) in enumerate(zip(rows, state.steps), start=1):
            self.assertTrue(text.startswith(f"{number} {get_started.step_title(step, state)}"), text)
            self.assertIn(f" {cli_text.GS_MARKS[step.state]} ", text + " ")
        result, win = self.run_screen(self.screen(), [ESC])
        self.assertEqual(result, get_started.GetStartedResult("card"))
        frame = win.frames[0]
        focused = state.first_unfinished
        index = [step.id for step in state.steps].index(focused)
        self.assertIn(f"› {rows[index][1]}", frame)
        self.assertIn(cli_text.GS_HEADER, frame)
        self.assertIn(cli_text.GS_CONNECTED.format(list=get_started.connected_text(state.connections)), frame)
        self.assertIn(" · ".join(f"{k} {v}".strip() for k, v in cli_text.GS_KEYBAR), frame)

    def test_enter_on_this_computer_opens_the_preflight(self) -> None:
        screen = self.screen()
        before = self.state_bytes()
        _result, win = self.run_screen(screen, [*self.steps_to(screen, "preflight"), ENTER, ESC, ESC])
        self.assertTrue(any(cli_text.PREFLIGHT_TITLE in frame for frame in win.frames))
        self.assertEqual(self.state_bytes(), before, "the preflight only reads")

    def test_focus_names_the_step_and_arrows_move(self) -> None:
        state = status.snapshot(self.runtime)
        rows = dict(get_started.step_rows(state))
        _result, win = self.run_screen(self.screen(focus="providers"), [UP, END, ESC])
        self.assertIn(f"› {rows['providers']}", win.frames[0])
        self.assertIn(f"› {rows['gateway']}", win.frames[1])
        self.assertIn(f"› {rows['check']}", win.frames[2])

    def test_moving_never_writes_and_ctrl_c_interrupts(self) -> None:
        before = self.state_bytes()
        _result, _win = self.run_screen(self.screen(), [DOWN, UP, END, HOME, "?", ESC, ESC])
        self.assertEqual(self.state_bytes(), before)
        with self.assertRaises(KeyboardInterrupt):
            self.run_screen(self.screen(), [CTRL_C])

    def test_the_floor(self) -> None:
        _result, win = self.run_screen(self.screen(), [ESC], height=cli_text.GS_MIN_ROWS - 1)
        self.assertNotIn(cli_text.GS_HEADER, win.frames[0])
        _result, win = self.run_screen(self.screen(), [ESC], height=cli_text.GS_MIN_ROWS)
        self.assertIn(cli_text.GS_HEADER, win.frames[0])


class RefusalTests(GetStartedCase):
    def test_a_resume_card_opens_it_read_only(self) -> None:
        screen = self.screen(resume=True)
        before = self.state_bytes()
        self.run_screen(screen, [*self.steps_to(screen, "profile"), ENTER, ESC])
        self.assertEqual(screen.message, cli_text.GS_RESUME_READONLY)
        self.run_screen(screen, ["a", ESC])
        self.assertEqual(screen.message, cli_text.GS_RESUME_READONLY)
        # The read-only steps still open.
        _result, win = self.run_screen(screen, [*self.steps_to(screen, "preflight"), ENTER, ESC, ESC])
        self.assertTrue(any(cli_text.PREFLIGHT_TITLE in frame for frame in win.frames))
        self.assertEqual(self.state_bytes(), before)

    def test_inside_a_claude_code_session_writes_are_refused(self) -> None:
        screen = self.screen()
        with mock.patch.dict(self.runtime.environ, {"CLAUDECODE": "1"}):
            self.run_screen(screen, [*self.steps_to(screen, "profile"), ENTER, ESC])
            self.assertEqual(screen.message, cli_text.GS_IN_SESSION)
            self.run_screen(screen, ["A", ESC])
            self.assertEqual(screen.message, cli_text.GS_IN_SESSION)

    def test_a_read_only_build_refuses_every_writing_step(self) -> None:
        screen = self.screen()
        with mock.patch.object(self.runtime, "allow_state_writes", False):
            for step in ("claude", "gateway", "providers", "test", "profile"):
                with self.subTest(step=step):
                    self.run_screen(screen, [*self.steps_to(screen, step), ENTER, ESC])
                    self.assertEqual(screen.message, cli_text.GS_READONLY)


class StepViewTests(GetStartedCase):
    def test_profile_step_builds_the_starter_and_makes_it_the_default(self) -> None:
        from claude_multi import choices

        screen = self.screen()
        self.assertEqual(status.snapshot(self.runtime).step("profile").state, "todo")
        _result, win = self.run_screen(screen, [*self.steps_to(screen, "profile"), ENTER, ENTER, ESC])
        shown = next(frame for frame in win.frames if cli_text.PROFILE_STEP_TITLE in frame)
        self.assertIn(f"[ {cli_text.PROFILE_STEP_STARTER_BUTTONS[0][0]} ]", shown)
        self.assertEqual(screen.message, cli_text.PROFILE_STEP_SAVED.format(name="starter"))
        self.assertIn("starter", self.runtime.profiles.names())
        self.assertEqual(choices.read(self.runtime.environ).get("default_profile"), "starter")
        self.assertEqual(status.snapshot(self.runtime).step("profile").state, "done")

    def test_not_now_on_the_profile_step_writes_nothing(self) -> None:
        screen = self.screen()
        before = self.state_bytes()
        keys = [*self.steps_to(screen, "profile"), ENTER, "\t", "\t", ENTER, ESC]
        self.run_screen(screen, keys)
        self.assertEqual(self.state_bytes(), before)

    def test_the_test_step_asks_once_and_a_no_sends_nothing(self) -> None:
        self.apply_and_serve()
        screen = self.screen()
        self.run_screen(screen, [*self.steps_to(screen, "test"), ENTER, ENTER, "n", ESC])
        self.assertEqual(self.calls, [])
        self.assertEqual(screen.message, cli_text.TEST_DECLINED)
        _result, win = self.run_screen(screen, [*self.steps_to(screen, "test"), ENTER, ENTER, "y", ESC, ESC])
        linked = [c for c in status.connected(self.runtime) if c.served.partition("/")[0] not in ("0", "—")]
        self.assertEqual(len(self.calls), len(linked))
        self.assertTrue(any(cli_text.TEST_CONSENT_TITLE in frame for frame in win.frames))
        self.assertTrue(any(cli_text.TEST_RESULTS_TITLE in frame for frame in win.frames))
        self.assertTrue(screen.results)
        test_row = dict(get_started.step_rows(screen.state, screen.results))["test"]
        self.assertIn(cli_text.GS_MARKS["done"], test_row)

    def test_the_check_step_opens_the_launch_card(self) -> None:
        screen = self.screen()
        result, win = self.run_screen(screen, [*self.steps_to(screen, "check"), ENTER, ENTER])
        self.assertEqual(result, get_started.GetStartedResult("card"))
        self.assertTrue(any(cli_text.CHECK_STEP_TITLE in frame for frame in win.frames))

    def test_profiles_go_through_the_callback(self) -> None:
        screen = self.screen()
        self.run_screen(screen, ["p", ESC])
        self.assertEqual(screen.message, cli_text.GS_PROFILES_FROM_CARD)
        opened = []
        chosen = self.screen(open_profiles=lambda win: opened.append(win) or "balanced")
        result, _win = self.run_screen(chosen, ["P"])
        self.assertEqual(result, get_started.GetStartedResult("use", "balanced"))
        self.assertEqual(len(opened), 1)
        nothing = self.screen(open_profiles=lambda win: None)
        result, _win = self.run_screen(nothing, ["P", ESC])
        self.assertEqual(result, get_started.GetStartedResult("card"))

    def test_the_gateway_step_reports_and_offers_the_log(self) -> None:
        screen = self.screen()
        with mock.patch.object(external, "ensure_gateway", return_value=None), \
                mock.patch.object(external, "gateway_log_tail", return_value=("fixture log line",)):
            _result, win = self.run_screen(screen, [*self.steps_to(screen, "gateway"), ENTER,
                                                    RIGHT, RIGHT, ENTER, ESC, ENTER, ESC])
        self.assertTrue(any("fixture log line" in frame for frame in win.frames))
        self.assertEqual(screen.message, cli_text.GATEWAY_STEP_RUNNING)


class ProvidersStepTests(GetStartedCase):
    """Enter on the providers step and A from any step open the provider
    picker on this screen; what the picker connects is written there."""

    def to_entry(self, entry_id: str) -> list:
        """From the picker's first entry down to ``entry_id`` (headers are skipped)."""

        picker = get_started._ProviderPicker(self.runtime, tui.MONO_PALETTE)
        entries = [row.entry.id for row in picker.rows if row.kind == "entry" and row.entry is not None]
        return [HOME, *([DOWN] * entries.index(entry_id))]

    def secret(self, name: str) -> str | None:
        from claude_multi import secret_store

        return secret_store.default_store(self.runtime.gateway_environ()).get(name)

    def test_enter_on_the_providers_step_connects_a_key_in_the_picker(self) -> None:
        provider = self.runtime.catalog.providers["openrouter"]
        name = provider["transport"]["auth"]["secret_ref"].removeprefix("env:")
        self.assertIsNone(self.secret(name))
        screen = self.screen()
        _result, win = self.run_screen(screen, [*self.steps_to(screen, "providers"), ENTER,
                                                *self.to_entry("openrouter:api-key"), ENTER,
                                                *"sk-or-dummy-key", ENTER, ENTER, ESC, ESC])
        self.assertTrue(any(cli_text.PICKER_TITLE in frame for frame in win.frames))
        self.assertTrue(any(cli_text.KEY_MODAL_TITLE.format(display="OpenRouter") in frame for frame in win.frames))
        self.assertEqual(self.secret(name), "sk-or-dummy-key")
        self.assertIn("API key saved (15 chars)", screen.message)
        self.assertEqual(status.snapshot(self.runtime).step("providers").state, "done")

    def test_a_adds_your_own_endpoint_from_any_step(self) -> None:
        screen = self.screen()
        self.assertNotEqual(screen.state.steps[screen.selected].id, "providers")
        keys = ["a", *self.to_entry("other:anthropic-compatible"), ENTER,
                *"vendorx", ENTER, *"https://api.vendorx.example/anthropic", ENTER, ENTER,
                *([BACKSPACE] * len("unknown")), *"vendorx", ENTER, ENTER,
                ENTER,  # declare
                RIGHT, ENTER,  # Approve
                *"vendorx-dummy-key", ENTER, ENTER,
                ESC,  # add models later
                ESC, ESC]
        _result, win = self.run_screen(screen, keys)
        frames = "\n".join(win.frames)
        self.assertIn(cli_text.PICKER_TITLE, frames)
        self.assertIn(cli_text.ENDPOINT_FORM_TITLE["anthropic-compatible"], frames)
        self.assertIn(cli_text.APPROVE_TITLE.format(id="vendorx"), frames)
        declared = self.runtime.lineup_catalog().providers["vendorx"]
        self.assertEqual(self.secret(declared["transport"]["auth"]["secret_ref"].removeprefix("env:")),
                         "vendorx-dummy-key")
        self.assertTrue(screen.message.startswith("Added vendorx"), screen.message)

    def test_a_over_ssh_signs_in_to_an_account_by_its_address(self) -> None:
        from claude_multi.setup import signin, texts
        import claude_multi.cli.screens.signin as screens_signin

        # A display is present, so only the SSH connection preselects the address.
        self.runtime.environ.update(DISPLAY=":0", SSH_CONNECTION="192.0.2.1 50000 192.0.2.2 22")
        # The gateway program the sign-in names; the runner seam runs instead of it.
        program = self.root / "fake-gateway"
        program.write_text("#!/bin/sh\nexit 0\n")
        program.chmod(0o755)
        self.runtime.environ["CLAUDE_MULTI_PROXY_BIN"] = str(program)
        runs: list = []
        self.runtime.signin_runner = lambda invocation: runs.append(invocation) or 0
        screen = self.screen()
        with mock.patch.object(signin, "pool_offered", return_value=True), \
                mock.patch.object(screens_signin, "_wait_for_enter"), mock.patch("sys.stdout", io.StringIO()):
            _result, win = self.run_screen(screen, ["a", *self.to_entry("anthropic:account"), ENTER,
                                                    *"personal", ENTER, ENTER,
                                                    ENTER,  # the preselected method
                                                    ESC, ESC, ESC])
        title = cli_text.SIGNIN_METHOD_TITLE.format(kind=texts.ACCOUNT_KINDS[signin.pool_of("anthropic")])
        chooser = next(frame for frame in win.frames if title in frame)
        address = dict(cli_text.SIGNIN_METHODS)["address"]
        self.assertTrue(next(line for line in chooser.splitlines() if address in line).lstrip().startswith(">"))
        self.assertEqual(len(runs), 1)
        self.assertIn("--no-browser", runs[0].argv)


class PresetPickerTests(GetStartedCase):
    """The reviewed presets in the provider picker: Enter on a preset with
    an API key names it, previews where its key goes, approves that route
    and takes the key, filled in from the preset; an OpenAI-compatible
    preset stays unavailable with the closed note while that route is
    closed. The shipped presets (the fixture assets carry none)."""

    def setUp(self) -> None:
        super().setUp()
        import test_presets

        patcher = test_presets.shipped_presets()
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        self.presets = test_presets.shipped()
        self.first = test_presets.first
        self.keyed_route = test_presets.keyed_route

    to_entry = ProvidersStepTests.to_entry
    secret = ProvidersStepTests.secret

    def test_enter_on_a_preset_adds_it_with_its_route_and_key(self) -> None:
        from claude_multi.setup import texts

        name = self.first("anthropic-compatible")
        info = self.presets[name]
        screen = self.screen()
        keys = ["a", *self.to_entry(f"preset:{name}"), ENTER,
                ENTER,  # the preset's own name
                ENTER,  # the preset's own address
                ENTER,  # declare
                RIGHT, ENTER,  # Approve
                *"preset-tui-dummy-key", ENTER, ENTER,
                ESC,  # add models later
                ESC, ESC]
        _result, win = self.run_screen(screen, keys)
        frames = "\n".join(win.frames)
        self.assertIn(cli_text.PICKER_DETAIL["preset"].format(display=info.display)[:40], frames)
        self.assertIn(cli_text.PRESET_FORM_TITLE.format(display=info.display), frames)
        self.assertIn(texts.PRESET_PREVIEW_HEAD.format(display=info.display)[:40], frames)
        self.assertIn(cli_text.APPROVE_TITLE.format(id=name), frames)
        self.assertIn(cli_text.KEY_MODAL_TITLE.format(display=info.display), frames)
        self.assertNotIn("preset-tui-dummy-key", frames)
        declared = self.runtime.lineup_catalog().providers[name]
        self.assertEqual(declared["transport"]["base_url"].rstrip("/"), info.base_url.rstrip("/"))
        self.assertEqual(self.secret(info.secret_name), "preset-tui-dummy-key")
        self.assertIn(name, self.ledger().routes)
        self.assertTrue(screen.message.startswith(f"Added {name}"), screen.message)

    def existing_instance(self, name: str, provider_id: str, key: str) -> None:
        """An existing, active provider from preset ``name`` (declared, approved, its key saved)."""

        from claude_multi.setup import providers as layer

        plan = layer.plan_add_preset(self.runtime, name, provider_id, None)
        layer.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan), model.Secret(key))
        self.assertIn(provider_id, [conn.provider_id for conn in status.connected(self.runtime)])

    def named(self, name: str, provider_id: str) -> list:
        """From the picker through the preset's form: its default name
        replaced by ``provider_id``, its own address kept."""

        return ["a", *self.to_entry(f"preset:{name}"), ENTER, *([BACKSPACE] * len(name)), *provider_id, ENTER,
                ENTER]

    def test_one_more_instance_of_a_preset_gets_its_own_key(self) -> None:
        from claude_multi.setup import texts

        name = self.first("anthropic-compatible")
        shared = self.presets[name].secret_name
        own = f"{shared}_SECOND"
        self.existing_instance(name, "first", "first-tui-key")
        screen = self.screen()
        keys = [*self.named(name, "second"),
                ENTER,  # which key: its own (the default)
                ENTER,  # declare
                RIGHT, ENTER,  # Approve
                *"second-tui-key", ENTER, ENTER,
                ESC,  # add models later
                ESC, ESC]
        _result, win = self.run_screen(screen, keys)
        frames = "\n".join(win.frames)
        self.assertEqual(self.secret(shared), "first-tui-key")
        self.assertEqual(self.secret(own), "second-tui-key")
        self.assertIn(cli_text.PRESET_KEY_TITLE.format(id="second"), frames)
        self.assertIn(texts.PRESET_KEY_SHARED.format(name=shared, providers="first"), frames)
        self.assertNotIn("tui-key", frames)
        declared = self.runtime.lineup_catalog().providers["second"]
        self.assertEqual(declared["transport"]["auth"]["secret_ref"], f"env:{own}")
        self.assertTrue(screen.message.startswith("Added second"), screen.message)
        connected = [conn.provider_id for conn in status.connected(self.runtime)]
        self.assertIn("first", connected)
        self.assertIn("second", connected)

    def test_the_key_chooser_fits_80x24_with_the_full_key_name(self) -> None:
        # Short selectable labels; the generated key name, however long,
        # is in the wrapped text above them, never clipped off a label.
        name = self.first("anthropic-compatible")
        shared = self.presets[name].secret_name
        provider_id = "second-team-workspace"
        own = f"{shared}_{provider_id.upper().replace('-', '_')}"
        self.assertLessEqual(len(own), 64)
        self.existing_instance(name, "first", "first-tui-key")
        for height, width in ((24, 80), (40, 120)):
            with self.subTest(size=f"{width}x{height}"):
                before = self.state_bytes()
                screen = self.screen()
                _result, win = self.run_screen(screen, [*self.named(name, provider_id), ESC, ESC, ESC],
                                               height=height, width=width)
                title = cli_text.PRESET_KEY_TITLE.format(id=provider_id)
                chooser = next(frame for frame in reversed(win.frames) if title in frame)
                self.assertIn(own, chooser)
                rows = chooser.splitlines()
                for label in cli_text.PRESET_KEY_ITEMS:
                    self.assertTrue(any(label in row and len(row.rstrip()) < width for row in rows), label)
                self.assertTrue(all(len(label) <= 40 for label in cli_text.PRESET_KEY_ITEMS))
                self.assertEqual(self.state_bytes(), before)

    def test_replacing_a_shared_key_is_confirmed_by_name_and_cancel_keeps_it(self) -> None:
        name = self.first("anthropic-compatible")
        shared = self.presets[name].secret_name
        self.existing_instance(name, "first", "first-tui-key")
        before = self.state_bytes()
        screen = self.screen()
        keys = [*self.named(name, "second"),
                DOWN, DOWN, ENTER,  # which key: replace the saved one
                ENTER,  # declare
                RIGHT, ENTER,  # Approve
                ENTER,  # the replacement's confirmation: Cancel is the default
                ESC, ESC]
        _result, win = self.run_screen(screen, keys)
        frames = "\n".join(win.frames)
        self.assertIn(cli_text.SHARED_KEY_REPLACE_TITLE.format(providers="first"), frames)
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.secret(shared), "first-tui-key")
        self.assertEqual(screen.message, model.NOTHING_CHANGED)

    def test_sharing_the_saved_key_types_none(self) -> None:
        from claude_multi.setup import texts

        name = self.first("anthropic-compatible")
        shared = self.presets[name].secret_name
        self.existing_instance(name, "first", "first-tui-key")
        screen = self.screen()
        keys = [*self.named(name, "second"),
                DOWN, ENTER,  # which key: the saved one, shared
                ENTER,  # declare
                RIGHT, ENTER,  # Approve
                ESC,  # add models later
                ESC, ESC]
        _result, win = self.run_screen(screen, keys)
        frames = "\n".join(win.frames)
        self.assertEqual(self.secret(shared), "first-tui-key")
        declared = self.runtime.lineup_catalog().providers.get("second")
        self.assertIsNotNone(declared, "the second instance is declared")
        self.assertEqual(declared["transport"]["auth"]["secret_ref"], f"env:{shared}")
        self.assertNotIn(cli_text.KEY_MODAL_BODY.split("\n", 1)[0], frames)  # no key modal
        self.assertIn(texts.PRESET_KEY_REUSE.format(id="second", name=shared, providers="first")[:60], frames)
        self.assertTrue(screen.message.startswith("Added second"), screen.message)

    def test_an_openai_compatible_preset_waits_for_its_route(self) -> None:
        from claude_multi import catalog, operator as operator_mod
        from claude_multi.setup import texts

        name = self.first(operator_mod.KEYED_KIND)
        before = self.state_bytes()
        with self.keyed_route(False):
            self.assertFalse(catalog.keyed_compat_audited(self.runtime.catalog.docs.get("gateway")))
            screen = self.screen()
            _result, win = self.run_screen(screen, ["a", *self.to_entry(f"preset:{name}"), ENTER, ESC, ESC])
        self.assertTrue(any(texts.OPENAI_COMPAT_CLOSED_NOTE in frame for frame in win.frames))
        form = cli_text.PRESET_FORM_TITLE.format(display=self.presets[name].display)
        self.assertFalse(any(form in frame for frame in win.frames))
        self.assertEqual(self.state_bytes(), before)


class ProxyTests(GetStartedCase):
    """The gateway's outbound proxy from the gateway step: one checked
    change with the running gateway's reload, put back exactly when the
    gateway configuration is refused or the change is interrupted."""

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

    def keys(self) -> list:
        # The field holds the current proxy: replace it, then Save.
        return [*([BACKSPACE] * len(self.OLD)), *self.NEW, ENTER, ENTER]

    def proxy_now(self) -> str | None:
        from claude_multi import endpoint

        return endpoint.read_config(self.runtime.home).proxy_url

    def test_a_refused_configuration_keeps_the_previous_proxy(self) -> None:
        from claude_multi import proxy

        screen = self.screen()
        with mock.patch.object(self.runtime, "render_gateway", side_effect=proxy.ProxyError("fixture render refused")):
            screen._proxy(FakeWindow(self.keys()))
        self.assertEqual(self.proxy_now(), self.OLD)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.runtime.catalog.docs["gateway"]["gateway"]["cliproxy_static"]["proxy-url"], self.OLD)
        self.assertEqual(screen.message_role, "warn")
        self.assertIn("Nothing changed — the gateway configuration was refused: fixture render refused",
                      screen.message)
        self.assertNotIn(self.NEW, screen.message)
        self.runtime.verify_reload.assert_not_called()

    def test_an_interrupted_change_is_put_back(self) -> None:
        screen = self.screen()
        with mock.patch.object(self.runtime, "render_gateway", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            screen._proxy(FakeWindow(self.keys()))
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_a_failure_or_interrupt_at_the_folder_sync_puts_the_document_back(self) -> None:
        from claude_multi import state

        real = state._fsync_directory
        for failure in (OSError(5, "fixture sync failure"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                failed: list[bool] = []

                def flaky(path, failure=failure) -> None:
                    # The new document replaced the old one; its folder sync fails once.
                    if Path(path) == self.path.parent and not failed:
                        failed.append(True)
                        raise failure
                    real(path)

                screen = self.screen()
                with mock.patch.object(state, "_fsync_directory", side_effect=flaky):
                    if isinstance(failure, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            screen._proxy(FakeWindow(self.keys()))
                    else:
                        screen._proxy(FakeWindow(self.keys()))
                        self.assertEqual(screen.message_role, "warn")
                self.assertEqual(failed, [True])
                self.assertEqual(self.path.read_bytes(), self.before)
                self.assertEqual(self.runtime.catalog.docs["gateway"]["gateway"]["cliproxy_static"]["proxy-url"],
                                 self.OLD)
                self.runtime.verify_reload.assert_not_called()

    def test_enter_on_the_gateway_step_opens_the_outbound_proxy_and_saves_it(self) -> None:
        screen = self.screen()
        with mock.patch.object(external, "ensure_gateway", return_value=None):
            # Enter → the gateway step → Outbound proxy… → the field → Save → Esc.
            _result, win = self.run_screen(screen, [*self.steps_to(screen, "gateway"), ENTER, RIGHT, ENTER,
                                                    *self.keys(), ESC])
        self.assertTrue(any(cli_text.PROXY_TITLE in frame for frame in win.frames))
        self.assertEqual(self.proxy_now(), self.NEW)
        self.assertTrue(screen.message.startswith(cli_text.PROXY_SET.format(proxy=self.NEW)), screen.message)
        self.runtime.verify_reload.assert_called_once()

    def test_a_saved_proxy_reloads_the_running_gateway(self) -> None:
        screen = self.screen()
        screen._proxy(FakeWindow(self.keys()))
        self.assertEqual(self.proxy_now(), self.NEW)
        self.assertEqual(screen.message_role, "accent")
        self.assertTrue(screen.message.startswith(cli_text.PROXY_SET.format(proxy=self.NEW) + " Applied — "),
                        screen.message)
        self.runtime.verify_reload.assert_called_once()

    def test_an_invalid_proxy_writes_nothing(self) -> None:
        screen = self.screen()
        screen._proxy(FakeWindow([*([BACKSPACE] * len(self.OLD)), *"http://user:secret@proxy.invalid", ENTER,
                                  ENTER]))
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertIn("proxy refused", screen.message)
        self.assertNotIn("secret", screen.message)


class _Download:
    """One in-memory download response (the acquisition's opener seam)."""

    status = 200
    headers: dict = {}

    def __init__(self, url: str, data: bytes):
        self.url = url
        self.buffer = io.BytesIO(data)

    def geturl(self) -> str:
        return self.url

    def read(self, size: int = -1) -> bytes:
        return self.buffer.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class ClaudeStepTests(GetStartedCase):
    """The Claude Code step runs the real acquisition with an offline
    download seam: a plan is shown first, and only a yes downloads."""

    PAYLOAD = b"#!/bin/sh\n# fixture claude build\n" + bytes(range(256)) * 16
    VERSION = "2.1.400"

    def setUp(self) -> None:
        super().setUp()
        from claude_multi import acquire, pin

        self.platform = pin.host_platform()
        self.contract = {"verified": [{
            "version": self.VERSION,
            "platforms": {self.platform: {"sha256": hashlib.sha256(self.PAYLOAD).hexdigest(),
                                          "size": len(self.PAYLOAD)}},
            "manifest_sha256": "1" * 64, "signature_sha256": None,
            "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE", "verified_at": "2026-10-01",
            "evidence": {self.platform: "battery", "receipt_sha256": "2" * 64},
        }]}
        self.requests: list[str] = []
        empty = Path(self.runtime.environ["HOME"]) / "empty-path"
        empty.mkdir(exist_ok=True)

        def opener(url, *, headers, timeout):
            self.requests.append(url)
            return _Download(url, self.PAYLOAD)

        def acquire_claude(runtime, *, consent, progress, claude_from=None):
            return acquire.acquire(self.contract, runtime.environ, platform=self.platform, consent=consent,
                                   progress=progress, opener=opener)

        patches = (
            mock.patch.dict(self.runtime.environ, {"PATH": str(empty)}),
            mock.patch.object(external, "claude_status", return_value=external.ClaudeStatus(
                self.VERSION, "missing", "not set up", external.CLAUDE_FIX)),
            mock.patch.object(external, "claude_verified", return_value=external.ClaudeStatus(
                self.VERSION, "missing", "not set up", external.CLAUDE_FIX)),
            mock.patch.object(external, "acquire_claude", acquire_claude),
            mock.patch("claude_multi.retention.prune"),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.owned = pin.owned_path(self.runtime.environ, self.VERSION, self.platform)

    def test_a_no_downloads_nothing_and_a_yes_installs_the_verified_copy(self) -> None:
        screen = self.screen(focus="claude")
        _result, win = self.run_screen(screen, [ENTER, "n", ESC])
        consent_frame = next(frame for frame in win.frames
                             if cli_text.CLAUDE_STEP_TITLE.format(version=self.VERSION) in frame)
        self.assertIn(cli_text.CLAUDE_STEP_UNTOUCHED, consent_frame)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.owned.exists())
        self.assertEqual(screen.message, model.NOTHING_CHANGED)
        self.run_screen(screen, [ENTER, "y", ESC])
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.owned.is_file())
        self.assertEqual(self.owned.read_bytes(), self.PAYLOAD)
        self.assertEqual(screen.message, cli_text.CLAUDE_STEP_DONE.format(version=self.VERSION))

    def test_ctrl_c_at_the_plan_cancels_and_a_non_terminal_is_refused(self) -> None:
        screen = self.screen(focus="claude")
        self.run_screen(screen, [ENTER, CTRL_C, ESC])
        self.assertEqual(self.requests, [])
        self.assertFalse(self.owned.exists())
        with mock.patch.object(consent, "stdio_ttys", return_value=False):
            _result, win = self.run_screen(screen, [ENTER, ESC])
        self.assertFalse(any(cli_text.CLAUDE_STEP_UNTOUCHED in frame for frame in win.frames))
        self.assertIn("terminal", screen.message)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.owned.exists())


def get_started_golden(root: Path) -> str:
    """Get started at 80x24 over the operator fixture (tests/goldens/tui)."""

    import _tui_render

    case = GetStartedCase()
    case.setUp()
    try:
        win = FakeWindow([ESC], height=_tui_render.HEIGHT, width=_tui_render.WIDTH)
        case.screen().run(win)
        return _tui_render.frame_text(win.frames[0])
    finally:
        case.doCleanups()


if __name__ == "__main__":
    unittest.main()
