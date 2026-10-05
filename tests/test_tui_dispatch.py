"""TUI dispatch: each key the operation matrix names reaches its action.

Every test drives a real screen (or the gateway dialog) through a
FakeWindow key script and observes the action that ran: the update view
and doctor on the resume card; Start now, Restart now, Show log and the
service install in the gateway dialog; Q, N, E and A on Providers (E opens
the prefilled form and saves through the providers edit transaction, never
an editor); a key for a provider with no models continuing into adding and
admitting one, after which a starter can lead with it; saved accounts
listed after a switch to the API key, with the key actions on the row;
Enter, V, Declare…, E and Q on Models. Temp homes, fixture gateways; no
provider request is sent and no editor runs.
"""

from __future__ import annotations

import dataclasses
import io
import json
import types
import unittest
from pathlib import Path
from unittest import mock

import claude_multi
import test_cli
import test_gateway_doctor
import test_screens_card
from test_tui import DOWN, RIGHT, FakeWindow
from claude_multi import gateway_lifecycle, operator as operator_mod, secret_store, state, tui
from claude_multi.setup import signin, status as setup_status
import claude_multi.cli.consent as consent
import claude_multi.cli.onboarding as onboarding
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.gateway_actions as gateway_actions
import claude_multi.cli.screens.models as models_screen
import claude_multi.cli.screens.providers as providers_screen
import claude_multi.cli.text as cli_text

ESC = "\x1b"
ENTER = "\n"


def _recording_views(shown: list) -> type:
    real = tui.TextView

    class RecordingView(real):
        def __init__(self, title, lines, **kwargs):
            shown.append((title, list(lines)))
            super().__init__(title, lines, **kwargs)

    return RecordingView


# ---------------------------------------------------------------- the resume card


class ResumeCardDispatchTests(test_screens_card._CardCase):
    def test_u_on_the_resume_card_shows_the_update(self) -> None:
        from claude_multi import upgrade

        _record, prepared = self.resume_prepared()
        shown: list = []
        with mock.patch.object(claude_multi.tui, "TextView", _recording_views(shown)), \
                mock.patch.object(upgrade, "run_repin", side_effect=AssertionError("never the re-pin flow")):
            self.run_card(self.resume_card(prepared), ["u", ESC])
            self.assertEqual(shown, [], "no badge, no update view")
            result, _win = self.run_card(self.resume_card(prepared, update_hint=("1.0.0", "1.0.1")),
                                         ["u", ESC, ESC])
        self.assertIsNone(result)
        self.assertEqual(shown[0][0], "Update claude-multi")
        self.assertIn("Nothing was changed", " ".join(shown[0][1]))
        self.assertEqual(self.runtime.launches, [])

    def test_v_shows_every_row_on_the_resume_card(self) -> None:
        _record, prepared = self.resume_prepared()
        shown: list = []
        with mock.patch.object(claude_multi.tui, "TextView", _recording_views(shown)):
            result, _win = self.run_card(self.resume_card(prepared), ["v", ESC, ESC])
        self.assertIsNone(result)
        self.assertEqual(len(shown), 1)
        text = "\n".join(shown[0][1])
        self.assertIn(prepared.lineup.lead.binding.display, text)
        self.assertIn(cli_text.CARD_DETAILS_ENV_TITLE, text)
        self.assertEqual(self.runtime.launches, [])

    def test_h_runs_doctor_on_the_resume_card(self) -> None:
        from claude_multi.cli import doctor as doctor_mod

        _record, prepared = self.resume_prepared()
        out = io.StringIO()
        screen = self.resume_card(prepared, tty_in=io.StringIO("\n\n\n"), tty_out=out)
        with mock.patch.object(doctor_mod, "_collect_doctor_reports",
                               return_value=([], ["fixture info line"], [])) as collect:
            result, _win = self.run_card(screen, ["h", ESC])
        self.assertIsNone(result)
        collect.assert_called_once()
        self.assertIn("claude-multi doctor\n", out.getvalue())
        self.assertIn(doctor_mod.DOCTOR_SUMMARY["ready"], out.getvalue())
        self.assertEqual(self.runtime.launches, [])


# ---------------------------------------------------------------- the gateway dialog


class _Service:
    """A user service manager with no service installed yet."""

    def __init__(self) -> None:
        self.installed: list[str] = []

    def status(self):
        return types.SimpleNamespace(supported=None, name="claude-multi-gateway.service", installed=False,
                                     foreign=False, stale=None)

    def install(self):
        self.installed.append("install")
        return gateway_lifecycle.Outcome("ready", "gateway service installed")


class GatewayDialogTests(test_gateway_doctor._GatewayDoctor):
    def dialog(self, keys, *, tty_in: str = "\n"):
        out = io.StringIO()
        win = FakeWindow([*keys], height=24, width=80)
        with mock.patch.object(tui, "suspended_curses", mock.MagicMock()):
            message = gateway_actions.dialog(self.runtime, win, tui.MONO_PALETTE,
                                             tty_in=io.StringIO(tty_in), tty_out=out)
        return message, win, out.getvalue()

    def test_start_now_starts_a_stopped_gateway(self) -> None:
        message, win, printed = self.dialog([ENTER])  # Start now is the first button
        self.assertIn("[ Start now ]", win.frames[0])
        self.assertEqual(len(self.world.spawned), 1)
        self.assertIn("Starting the gateway", printed)
        self.assertTrue(message.startswith("gateway ready"), message)

    def test_restart_now_restarts_a_proven_gateway(self) -> None:
        self.world.become_ready("ffffffffffffffff")
        message, win, printed = self.dialog([ENTER])  # Restart now is the first button
        self.assertIn("[ Restart now ]", win.frames[0])
        self.assertNotIn("Start now", win.frames[0])
        self.assertEqual(self.world.terminated, [4242])
        self.assertIn("Restarting the gateway", printed)
        self.assertTrue(message.startswith("gateway ready"), message)

    def test_show_log_opens_the_tail(self) -> None:
        self.world.become_ready("abababababababab")
        self.log("abababababababab", ["first fixture line", "last fixture line"])
        message, win, _printed = self.dialog(["\t", ENTER, ESC])  # Restart now → Show log
        self.assertEqual(message, "")
        text = "\n".join(win.frames)
        self.assertIn("gateway log — last 200 lines", text)
        self.assertIn("last fixture line", text)
        self.assertEqual(self.world.terminated, [])

    def test_the_dialog_names_the_service(self) -> None:
        service = _Service()
        with mock.patch.object(type(self.runtime), "gateway_service", return_value=service):
            _message, win, _printed = self.dialog([ESC])
        self.assertIn("service: not installed (the gateway starts on demand)", win.frames[0])
        self.assertIn("claude-multi gateway stop", win.frames[0])
        self.assertEqual(service.installed, [])

    def test_install_service_runs_the_service_install(self) -> None:
        service = _Service()
        with mock.patch.object(type(self.runtime), "gateway_service", return_value=service):
            message, win, printed = self.dialog(["\t", ENTER])  # Start now → Install service
        self.assertIn("[ Install service ]", win.frames[0])
        self.assertEqual(service.installed, ["install"])
        self.assertIn("Installing the gateway service", printed)
        self.assertEqual(message, "gateway service installed")
        self.assertEqual(self.world.spawned, [])


# ---------------------------------------------------------------- Providers


class _ProvidersCase(test_cli.OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        for name in consent.SESSION_MARKERS:
            self.runtime.environ.pop(name, None)
        guard = mock.patch.object(consent, "stdio_ttys", return_value=True)
        guard.start()
        self.addCleanup(guard.stop)
        self.shown: list[tuple[str, list[str]]] = []
        for patcher in (
            mock.patch.object(screens_common.OnboardingActions, "confirm", lambda _self, text: True),
            mock.patch.object(screens_common.OnboardingActions, "show",
                              lambda _self, title, lines: self.shown.append((title, list(lines)))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def screen(self, select: str | None = None) -> providers_screen._ProvidersScreen:
        screen = providers_screen._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
        if select is not None:
            screen.selected = [row.id for row in screen.rows].index(select)
        return screen

    def run_screen(self, screen, keys, *, height=24, width=80) -> FakeWindow:
        win = FakeWindow([*keys, ESC], height=height, width=width)
        screen.run(win)
        return win

    def store(self) -> secret_store.FileSecretStore:
        return secret_store.default_store(self.runtime.gateway_environ())

    def modelless_keyed(self) -> str:
        """A shipped keyed provider without model lines (derived, never pinned)."""

        lines = self.runtime.lineup_catalog().lines
        for pid, provider in sorted(self.runtime.catalog.providers.items()):
            auth = provider["transport"].get("auth")
            if (provider["transport"]["kind"] == "direct" and isinstance(auth, dict) and auth.get("secret_ref")
                    and not any(entry.get("provider") == pid for entry in lines.values())):
                return pid
        raise AssertionError("the fixture has a keyed provider without models")


class ProvidersDispatchTests(_ProvidersCase):
    def test_q_shows_the_details(self) -> None:
        shown: list = []
        with mock.patch.object(claude_multi.tui, "TextView", _recording_views(shown)):
            self.run_screen(self.screen("kimi"), ["q", ESC])
        self.assertEqual(shown[0][0], cli_text.DETAILS_TITLE)
        self.assertTrue(shown[0][1])

    def test_n_opens_the_provider_picker(self) -> None:
        import claude_multi.cli.screens.get_started as screens_get_started

        with mock.patch.object(screens_get_started, "run_picker", return_value=None) as picker:
            self.run_screen(self.screen(), ["n"])
        picker.assert_called_once()
        self.assertEqual(picker.call_args.kwargs, {"only": "other"})

    def test_a_pasted_key_in_the_secret_name_never_reaches_a_preview(self) -> None:
        from test_tui import END

        synthetic = "sk-ant-api03-" + "a" * 40
        values = {"id": "acme", "kind": "anthropic-compatible", "url": "https://api.acme.example/anthropic",
                  "auth": "bearer", "secret": synthetic, "family": "acme", "contracts": "",
                  "listing": "", "shape": "", "listing_auth": ""}
        directory = operator_mod.providers_dir(self.runtime.gateway_environ())
        before = sorted(path.name for path in directory.glob("*")) if directory.is_dir() else []
        # N opens the picker of your own providers; End is the manual form;
        # Enter opens it (its answers come from the patched form).
        with mock.patch.object(tui.OnboardingForm, "run", return_value=values), \
                mock.patch.object(screens_common.OnboardingActions, "preview",
                                  side_effect=AssertionError("the declaration preview was reached")), \
                mock.patch.object(onboarding, "invoke", side_effect=AssertionError("a command ran")):
            win = self.run_screen(self.screen(), ["n", END, ENTER, ESC], height=30, width=100)
        self.assertTrue(any(cli_text.ADVANCED_MANUAL in frame for frame in win.frames))
        self.assertEqual(self.shown, [(cli_text.PROVIDER_SECRET_NAME_TITLE,
                                       [secret_store.KEY_NOT_NAME, "", cli_text.PROVIDER_SECRET_NAME_HINT])])
        everything = repr(self.shown) + "\n".join(win.frames)
        self.assertNotIn(synthetic, everything)
        self.assertNotIn("a" * 40, everything)
        self.assertNotIn("--secret-ref", everything)
        after = sorted(path.name for path in directory.glob("*")) if directory.is_dir() else []
        self.assertEqual(after, before, "nothing was declared")

    def test_a_pasted_key_typed_into_the_secret_name_field_is_never_drawn(self) -> None:
        # The real form, key by key: the key-shaped text is never drawn, not
        # even in part; Enter refuses it at the field (value-free) and clears
        # it; a variable name is drawn and moves on.
        from test_tui import END

        synthetic = "sk-ant-api03-" + "Dummy0" * 7
        before_secret = ["n", END, ENTER, *"acme", ENTER, ENTER, *"https://api.acme.example/anthropic", ENTER, ENTER]
        with mock.patch.object(screens_common.OnboardingActions, "preview",
                               side_effect=AssertionError("the declaration preview was reached")), \
                mock.patch.object(onboarding, "invoke", side_effect=AssertionError("a command ran")):
            win = self.run_screen(self.screen(), [*before_secret, *synthetic, ENTER, ESC, ESC], height=30, width=100)
        frames = "\n".join(win.frames)
        self.assertIn("Logical secret name", frames)
        for piece in (synthetic, "sk-", "Dummy0", "api03"):
            self.assertNotIn(piece, frames)
        self.assertNotIn("Independence family", frames, "the form stayed on the key-name step")
        self.assertIn(tui.FIELD_HIDDEN, frames)
        self.assertIn("looks like an API key", frames)
        self.assertEqual(self.shown, [])
        # A variable name is drawn as typed and the form moves on.
        with mock.patch.object(screens_common.OnboardingActions, "preview", return_value=False):
            win = self.run_screen(self.screen(), [*before_secret, *"ACME_API_KEY", ENTER, ESC, ESC], height=30,
                                  width=100)
        frames = "\n".join(win.frames)
        self.assertIn("ACME_API_KEY_", frames)
        self.assertIn("Independence family", frames)

    def test_an_invalid_secret_name_is_refused_by_name_only(self) -> None:
        from test_tui import END

        values = {"id": "acme", "kind": "anthropic-compatible", "url": "https://api.acme.example/anthropic",
                  "auth": "bearer", "secret": "ANTHROPIC_API_KEY", "family": "acme", "contracts": "",
                  "listing": "", "shape": "", "listing_auth": ""}
        with mock.patch.object(tui.OnboardingForm, "run", return_value=values), \
                mock.patch.object(screens_common.OnboardingActions, "preview",
                                  side_effect=AssertionError("the declaration preview was reached")):
            self.run_screen(self.screen(), ["n", END, ENTER, ESC], height=30, width=100)
        self.assertEqual(self.shown[0][0], cli_text.PROVIDER_SECRET_NAME_TITLE)
        self.assertEqual(self.shown[0][1][0], secret_store.secret_name_problem("ANTHROPIC_API_KEY"))

    def test_a_offers_the_add_models_flow(self) -> None:
        with mock.patch.object(screens_common.OnboardingActions, "add_models") as add:
            self.run_screen(self.screen("kimi"), ["a"])
        add.assert_called_once_with("kimi")

    def test_e_edits_a_provider_in_the_form(self) -> None:
        import claude_multi.cli.commands.providers as provider_commands

        code, _out, err = self.op(test_cli.ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        path = operator_mod.providers_dir(self.runtime.gateway_environ()) / "acme.json"
        before = json.loads(path.read_text())["provider"]
        seen: dict = {}

        def answer(form, _win):
            seen["fields"] = {name: default for name, _label, default, _choices in form.fields}
            return {**seen["fields"], "family": "acme-labs", "display": "Acme Labs"}

        with mock.patch.object(tui.OnboardingForm, "run", autospec=True, side_effect=answer), \
                mock.patch.object(provider_commands, "run_editor",
                                  side_effect=AssertionError("the form never opens an editor")):
            screen = self.screen("acme")
            self.run_screen(screen, ["e"])
        # The form starts from the declaration; only what it changed changes.
        self.assertEqual(seen["fields"]["url"], before["base_url"])
        self.assertEqual(seen["fields"]["auth"], before["auth"]["kind"])
        self.assertEqual(seen["fields"]["family"], before["independence_family"])
        after = json.loads(path.read_text())["provider"]
        self.assertEqual(after["independence_family"], "acme-labs")
        self.assertEqual(after["display"], "Acme Labs")
        self.assertEqual({k: v for k, v in after.items() if k not in ("independence_family", "display")},
                         {k: v for k, v in before.items() if k not in ("independence_family", "display")})
        self.assertEqual(screen.message, cli_text.EDIT_DONE.format(id="acme"))
        self.assertIn("wrote providers.d/acme.json", "\n".join(self.shown[-1][1]))
        # A shipped provider is not edited here.
        screen = self.screen("kimi")
        with mock.patch.object(tui.OnboardingForm, "run", side_effect=AssertionError("no form")):
            self.run_screen(screen, ["E"])
        self.assertIn("E edits providers you added", screen.message)


class ProvidersGatewayDispatchTests(_ProvidersCase):
    """W on Providers opens the gateway's actions."""

    def test_w_start_now_starts_a_stopped_gateway(self) -> None:
        import _tui_fixture as fx

        view = fx.GatewayViewStub(start=True)
        screen = self.screen("kimi")
        with fx.gateway_actions_seam(view) as acted:
            win = self.run_screen(screen, ["w", ENTER])  # Start now is the first button
        self.assertEqual(acted, [(view, "start")])
        self.assertTrue(any("[ Start now ]" in frame for frame in win.frames))
        self.assertEqual(screen.message, "gateway start: fixture done")

    def test_w_show_log_opens_the_tail(self) -> None:
        import _tui_fixture as fx

        view = fx.GatewayViewStub()
        with fx.gateway_actions_seam(view) as acted, \
                mock.patch.object(gateway_actions, "log_view",
                                  return_value=("gateway log — fixture", ["last fixture line"])) as log_view:
            win = self.run_screen(self.screen("kimi"), ["w", ENTER, ESC])  # Show log is the first button
        log_view.assert_called_once_with(view)
        self.assertEqual(acted, [])
        self.assertTrue(any("last fixture line" in frame for frame in win.frames))

    def test_w_names_the_service(self) -> None:
        import _tui_fixture as fx

        line = "service: not installed (the gateway starts on demand)"
        view = fx.GatewayViewStub(service_line=line)
        with fx.gateway_actions_seam(view) as acted:
            win = self.run_screen(self.screen("kimi"), ["w", ESC])
        self.assertEqual(acted, [])
        dialog = next(frame for frame in win.frames if gateway_actions.TITLE in frame)
        self.assertIn(line, dialog)
        self.assertIn("claude-multi gateway stop", dialog)


class ProviderEditFormTests(_ProvidersCase):
    """The edit form keeps its opening snapshot through the
    locked comparison and never draws a secret-bearing value."""

    def acme(self) -> Path:
        code, _out, err = self.op(test_cli.ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        return operator_mod.providers_dir(self.runtime.gateway_environ()) / "acme.json"

    def test_a_declaration_changed_while_the_form_is_open_is_not_reverted(self) -> None:
        path = self.acme()
        moved = "https://api.acme.example/v2/anthropic"

        def answer(form, _win):
            defaults = {name: default for name, _label, default, _choices in form.fields}
            # Another window changes the URL while the form is open.
            document = json.loads(path.read_text())
            document["provider"]["base_url"] = moved
            state.atomic_write(path, operator_mod.document_bytes(document))
            return {**defaults, "family": "acme-labs"}

        screen = self.screen("acme")
        with mock.patch.object(tui.OnboardingForm, "run", autospec=True, side_effect=answer):
            self.run_screen(screen, ["e"])
        after = json.loads(path.read_text())["provider"]
        self.assertEqual(after["base_url"], moved)
        self.assertNotEqual(after.get("independence_family"), "acme-labs")
        self.assertEqual(screen.message, cli_text.EDIT_NOT_SAVED.format(id="acme"))
        self.assertIn("changed while you edited it", "\n".join(self.shown[-1][1]))
        # A change during the confirmation meets the locked comparison with
        # the same opening snapshot.
        again = "https://api.acme.example/v3/anthropic"

        def confirm_meanwhile(_self, _text):
            document = json.loads(path.read_text())
            document["provider"]["base_url"] = again
            state.atomic_write(path, operator_mod.document_bytes(document))
            return True

        screen = self.screen("acme")
        with mock.patch.object(tui.OnboardingForm, "run", autospec=True,
                               side_effect=lambda form, _win: {**{n: d for n, _l, d, _c in form.fields},
                                                               "family": "acme-labs"}), \
                mock.patch.object(screens_common.OnboardingActions, "confirm", confirm_meanwhile):
            self.run_screen(screen, ["e"])
        after = json.loads(path.read_text())["provider"]
        self.assertEqual(after["base_url"], again)
        self.assertNotEqual(after.get("independence_family"), "acme-labs")
        self.assertEqual(screen.message, cli_text.EDIT_NOT_SAVED.format(id="acme"))

    def test_a_secret_bearing_declaration_is_refused_before_any_default_is_drawn(self) -> None:
        path = self.acme()
        secret = "sk-" + "fixture" + "0123456789abcdef"
        screen = self.screen("acme")
        # A hand edit after the screen loaded: a credential inside the URL.
        document = json.loads(path.read_text())
        document["provider"]["base_url"] = f"https://api.acme.example/anthropic?key={secret}"
        state.atomic_write(path, operator_mod.document_bytes(document))
        with mock.patch.object(tui.OnboardingForm, "run", side_effect=AssertionError("the form opened")):
            win = self.run_screen(screen, ["e"])
        self.assertIn("looks like a secret", screen.message)
        self.assertIn("$.provider.base_url", screen.message)
        for text in (screen.message, *win.frames):
            self.assertNotIn(secret, text)


class ZeroModelProviderTests(_ProvidersCase):
    def test_a_key_continues_into_adding_and_admitting_a_model(self) -> None:
        # Nothing else is connected: no saved key and the keyless server off.
        state.atomic_write(self.secret_file, b"")
        self.op(["providers", "disable", "llm-local"])
        pid = self.modelless_keyed()
        key = f"custom-{pid}-x1"
        declared: list[str] = []

        def by_hand(actions, provider_id, line=None, key_name="", *, edit=False):
            output = io.StringIO()
            onboarding.declare(self.runtime, provider_id, key, {
                "wire_model": f"{pid}-x1", "display": f"{pid} x1", "efforts": ["high"], "default_effort": "high",
                "context": {"declared_tokens": 131072, "source": "operator"}, "capabilities": ["lead"],
                "roles": []}, output=output)
            declared.append(provider_id)
            self.serve_current()

        screen = self.screen(pid)
        with mock.patch.object(screens_common.OnboardingActions, "model_form", autospec=True,
                               side_effect=by_hand), \
                mock.patch.object(operator_mod, "smoke_current", return_value=True):
            # K, the key, Save; then "Enter a model by hand"; then Admit.
            win = self.run_screen(screen, ["K", *"fresh-dummy-key", ENTER, ENTER, DOWN, ENTER, ENTER])
        self.assertTrue(any(cli_text.ADD_MODELS_TITLE.format(id=pid) in frame for frame in win.frames))
        self.assertTrue(any(cli_text.ADMIT_NOW_TITLE.format(key=key) in frame for frame in win.frames))
        self.assertEqual(declared, [pid])
        self.assertIn(cli_text.NO_MODELS_ADMITTED.format(id=pid, keys=key), screen.message)
        self.assertNotIn("fresh-dummy-key", "\n".join(win.frames))
        from claude_multi.cli.commands import providers as provider_commands

        self.assertIn(key, provider_commands.admitted_keys(self.runtime))
        # The provider alone now gives a usable starter.
        plan, _warnings = self.runtime.starter_plan("starter")
        self.assertIsNotNone(plan.document, plan.refusal)
        self.assertEqual(plan.document["lead"]["model"], key)

    def test_later_names_the_next_step_and_writes_no_model(self) -> None:
        pid = self.modelless_keyed()
        screen = self.screen(pid)
        self.run_screen(screen, ["K", *"fresh-dummy-key", ENTER, ENTER, ESC])
        self.assertEqual(self.store().get(self.runtime.catalog.providers[pid]["transport"]["auth"]["secret_ref"]
                                          .removeprefix("env:")), "fresh-dummy-key")
        self.assertIn(cli_text.NO_MODELS_LATER.format(id=pid), screen.message)
        self.assertFalse(setup_status.without_models(self.runtime, ("kimi",)))
        self.assertEqual(setup_status.without_models(self.runtime, (pid,)), (pid,))

    def test_the_command_line_names_the_next_step(self) -> None:
        pid = self.modelless_keyed()
        key_file = self.root / "modelless.key"
        key_file.write_text("cli-dummy-key\n")
        key_file.chmod(0o600)
        code, out, err = self.op(["providers", "set-key", pid, "--secret-file", str(key_file)])
        self.assertEqual(code, 0, err)
        self.assertIn(f"next: {pid} has no models yet", out)
        self.assertIn(f"claude-multi discover {pid} --add WIRE", out)


class AccountInventoryTests(_ProvidersCase):
    def test_a_saved_account_stays_listed_after_a_key_switch(self) -> None:
        directory = signin.auth_dir(self.runtime)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (directory / "claude-a@example.com.json").write_bytes(b"{}")
        key_file = self.root / "platform.key"
        key_file.write_text("platform-dummy-key\n")
        key_file.chmod(0o600)
        code, _out, err = self.op(["providers", "transport", "anthropic", "api-key", "--secret-file",
                                   str(key_file)], "y\n")
        self.assertEqual(code, 0, err)
        connection = next(item for item in setup_status.connections(self.runtime) if item.provider_id == "anthropic")
        self.assertEqual(connection.transport, "api-key")
        self.assertEqual(connection.accounts, ("a@example.com",))
        screen = self.screen("anthropic")
        bar = dict(screen._keybar_bindings())
        self.assertEqual(bar["K"], cli_text.PROVIDERS_KEY_LABELS["replace"])
        self.assertEqual(bar["X"], "remove key")
        self.assertEqual(bar["L"], "accounts")
        shown: list = []
        with mock.patch.object(claude_multi.tui, "TextView", _recording_views(shown)):
            self.run_screen(screen, ["q", ESC])
        self.assertIn(cli_text.DETAILS_SAVED_ACCOUNTS.format(accounts="a@example.com"), shown[0][1])
        # L still opens the account modal with the saved account.
        win = self.run_screen(screen, ["l", ESC])
        self.assertTrue(any("a@example.com" in frame for frame in win.frames))
        # The picker's account entry says the account is signed in, not in use.
        from claude_multi.setup import providers as setup_providers, texts

        entry = next(e for e in setup_providers.picker_entries(self.runtime) if e.id == "anthropic:account")
        self.assertTrue(entry.state_text.startswith(texts.STATE_TEXT["signed-in"]), entry.state_text)
        self.assertNotIn(texts.IN_USE, entry.state_text)


# ---------------------------------------------------------------- Models


class ModelsDispatchTests(_ProvidersCase):
    KEY = "custom-acme-small"

    def setUp(self) -> None:
        super().setUp()
        self.declare_small()
        self.serve_current()

    def models(self, key: str | None) -> models_screen._ModelsScreen:
        screen = models_screen._ModelsScreen(self.runtime, palette=tui.MONO_PALETTE)
        if key is not None:
            screen.index = screen.keys.index(key)
        return screen

    def test_enter_inspects_a_catalog_line(self) -> None:
        shipped = next(key for key in self.runtime.catalog.docs["models-v2"]["models"]
                       if key in models_screen._ModelsScreen(self.runtime, palette=tui.MONO_PALETTE).keys)
        shown: list = []
        with mock.patch.object(claude_multi.tui, "TextView", _recording_views(shown)):
            self.run_screen(self.models(shipped), [ENTER, ESC])
        self.assertEqual(shown[0][0], "model — full details")
        self.assertTrue(shown[0][1])

    def test_v_shows_the_full_details(self) -> None:
        shown: list = []
        with mock.patch.object(claude_multi.tui, "TextView", _recording_views(shown)):
            self.run_screen(self.models(self.KEY), ["v", ESC])
        self.assertEqual(shown[0][0], "model — full details")
        self.assertIn(self.KEY, "\n".join(shown[0][1]))

    def test_a_candidate_opens_the_prefilled_declare_form(self) -> None:
        from claude_multi import discovery

        screen = self.models("__candidates__")
        row = discovery.Candidate("acme", "acme", "acme-large-2", frozenset({"served"}), None, None, None)
        screen.candidates = dataclasses.replace(screen.candidates, rows=(row,), unattributed=())
        with mock.patch.object(models_screen._ModelsScreen, "_load"), \
                mock.patch.object(screens_common.OnboardingActions, "model_form") as form:
            self.run_screen(screen, [ENTER, ENTER, RIGHT, ENTER], width=100)
        form.assert_called_once()
        provider_id, draft, key = form.call_args.args
        self.assertEqual(provider_id, "acme")
        self.assertEqual(draft["wire_model"], "acme-large-2")
        self.assertTrue(key)

    def test_a_registry_candidate_keeps_its_registry_provenance(self) -> None:
        from claude_multi import catalog as catalog_mod, discovery

        screen = self.models("__candidates__")
        meta = catalog_mod.RegistryModel(display_name="Acme Large 2", context_length=131072)
        row = discovery.Candidate("acme-section", "acme", "acme-large-2", frozenset({"registry"}), meta, None, None)
        screen.candidates = dataclasses.replace(screen.candidates, rows=(row,), unattributed=())
        defaults: list[dict] = []

        class RecordingForm:
            def __init__(self, title, fields, **_kwargs):
                defaults.append({name: default for name, _label, default, _choices in fields})

            def run(self, _win):
                return None  # the person cancels: nothing is declared

        # Enter on candidates, Enter on the row, → to Declare…, Enter: the real form.
        with mock.patch.object(models_screen._ModelsScreen, "_load"), \
                mock.patch.object(claude_multi.tui, "OnboardingForm", RecordingForm):
            self.run_screen(screen, [ENTER, ENTER, RIGHT, ENTER], width=100)
        self.assertEqual(len(defaults), 1)
        form = defaults[0]
        self.assertEqual(form["wire"], "acme-large-2")
        self.assertEqual(form["context"], "131072")
        self.assertEqual(form["source"], "registry")
        self.assertTrue(form["ref"].startswith("pinned registry acme-section "), form["ref"])
        self.assertNotIn("listing", (form["source"], form["ref"]))

    def test_e_edits_a_declared_model(self) -> None:
        with mock.patch.object(screens_common.OnboardingActions, "model_form") as form:
            self.run_screen(self.models(self.KEY), ["e"])
        form.assert_called_once()
        provider_file, line, key = form.call_args.args
        self.assertEqual(key, self.KEY)
        self.assertEqual(line["wire_model"], "acme-small-1")
        self.assertEqual(form.call_args.kwargs, {"edit": True})

    def test_q_opens_the_qualification_form(self) -> None:
        with mock.patch.object(screens_common.OnboardingActions, "qualify") as qualify:
            self.run_screen(self.models(self.KEY), ["q"])
        qualify.assert_called_once_with(self.KEY)


if __name__ == "__main__":
    unittest.main()
