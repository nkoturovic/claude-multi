"""The Providers screen's credential and provider actions: set or replace
an API key (Cancel is the default on a replace; a key other providers share
names them first), the missing key of a selected API-key transport, a
reviewed preset from N (its name, its address, which key, the route and the
key), remove a key or a provider you added (with its blockers), apply only
when it is offered, an account provider's account or API key (Anthropic and
OpenAI), the account modal and sign-out (both read from the account-pool
data), the consented test, and keys in either letter case. A fixture
gateway on a temp home with a verified reload; no provider request is
sent."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import test_cli
import test_setup_signin
from test_tui import BACKSPACE, DOWN, END, HOME, RIGHT, FakeWindow
from claude_multi import secret_store, tui, views
from claude_multi.setup import model, signin, texts
import claude_multi.cli.consent as consent
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.providers as providers_screen
import claude_multi.cli.screens.signin as screens_signin
import claude_multi.cli.text as cli_text

ESC = "\x1b"
ENTER = "\n"


def pools_with(name: str, **fields):
    """The packaged pool table with pool ``name`` changed (top-level fields,
    and ``flow`` inside its sign-in), patched in for the sign-in layer."""

    import copy

    from claude_multi import account_pools, strict_json
    from _layout import RESOURCES_ROOT

    document = copy.deepcopy(strict_json.load(RESOURCES_ROOT / account_pools.FILE))
    entry = document["pools"][name]
    if "flow" in fields:
        entry["sign_in"]["flow"] = fields.pop("flow")
    entry.update(fields)
    table = account_pools.parse(document, strict_json.load(RESOURCES_ROOT / account_pools.SCHEMA))
    return mock.patch.object(account_pools, "load", lambda root=None: table)


class ProvidersCase(test_cli.OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        for name in consent.SESSION_MARKERS:
            self.runtime.environ.pop(name, None)
        guard = mock.patch.object(consent, "stdio_ttys", return_value=True)
        guard.start()
        self.addCleanup(guard.stop)

    def store(self) -> secret_store.FileSecretStore:
        return secret_store.default_store(self.runtime.gateway_environ())

    def secret_name(self, provider_id: str) -> str:
        provider = self.runtime.catalog.providers[provider_id]
        return provider["transport"]["auth"]["secret_ref"].removeprefix("env:")

    def keyed(self, *, present: bool) -> str:
        """A shipped keyed provider whose key is (or is not) set."""

        for provider_id, provider in sorted(self.runtime.catalog.providers.items()):
            auth = provider["transport"].get("auth")
            if provider["transport"]["kind"] == "direct" and isinstance(auth, dict) and auth.get("secret_ref"):
                if bool(self.store().get(self.secret_name(provider_id))) == present:
                    return provider_id
        raise AssertionError("the fixture has a keyed provider in that state")

    def screen(self, select: str | None = None) -> providers_screen._ProvidersScreen:
        screen = providers_screen._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
        if select is not None:
            screen.selected = [row.id for row in screen.rows].index(select)
        return screen

    def run_screen(self, screen, keys, *, height=24, width=80) -> FakeWindow:
        win = FakeWindow([*keys, ESC], height=height, width=width)
        screen.run(win)
        return win

    def frame_with(self, win: FakeWindow, needle: str) -> str:
        return next(frame for frame in reversed(win.frames) if needle in frame)


class KeyTests(ProvidersCase):
    def test_k_replaces_only_on_an_explicit_choice(self) -> None:
        pid = self.keyed(present=True)
        name = self.secret_name(pid)
        before = self.store().get(name)
        screen = self.screen(pid)
        # Enter in the field lands on Cancel, the default of a replace.
        win = self.run_screen(screen, ["K", *"replacement-dummy", ENTER, ENTER])
        shown = self.frame_with(win, cli_text.KEY_MODAL_REPLACE.split("(")[0])
        self.assertIn(f"A key is set ({len(before)} chars)", shown)
        self.assertEqual(self.store().get(name), before)
        self.assertIn("API key kept — nothing changed", screen.message)
        # The lower-case letter works too; Replace is chosen explicitly.
        win = self.run_screen(screen, ["k", *"replacement-dummy", ENTER, RIGHT, ENTER])
        self.assertEqual(self.store().get(name), "replacement-dummy")
        self.assertIn("API key saved (17 chars)", screen.message)
        self.assertFalse(any("replacement-dummy" in frame for frame in win.frames), "the key is typed masked")

    def test_enter_on_a_missing_key_saves_it(self) -> None:
        pid = self.keyed(present=False)
        screen = self.screen(pid)
        self.assertEqual(screen._primary(screen._fact()), "set-key")
        # A provider without models continues into adding them (Esc: later).
        modelless = not any(entry.get("provider") == pid for entry in self.runtime.lineup_catalog().lines.values())
        self.run_screen(screen, [ENTER, *"fresh-dummy-key", ENTER, ENTER, *([ESC] if modelless else [])])
        self.assertEqual(self.store().get(self.secret_name(pid)), "fresh-dummy-key")
        self.assertIn("API key saved (15 chars)", screen.message)

    def test_x_removes_a_shipped_providers_key_after_a_confirmation(self) -> None:
        pid = self.keyed(present=True)
        name = self.secret_name(pid)
        before = self.store().get(name)
        screen = self.screen(pid)
        win = self.run_screen(screen, ["X", ENTER])
        self.assertTrue(any("Remove the " in frame and "API key?" in frame for frame in win.frames))
        self.assertEqual(self.store().get(name), before)
        self.assertEqual(screen.message, model.NOTHING_CHANGED)
        self.run_screen(screen, ["x", RIGHT, ENTER])
        self.assertIsNone(self.store().get(name))

    def test_a_refused_key_shows_and_writes_nothing(self) -> None:
        screen = self.screen("llm-local")
        before = self.state_bytes()
        with mock.patch.object(tui, "Modal", side_effect=AssertionError("no key modal for a keyless server")):
            self.run_screen(screen, ["K"])
        self.assertIn(" needs no key", screen.message)
        self.assertEqual(self.state_bytes(), before)


class SelectedTransportKeyTests(ProvidersCase):
    """Anthropic's approved API-key transport with its key gone: the
    readiness a screen shows names the Providers route (G → K), never a
    switch to the transport already in use, and K on Anthropic saves the key."""

    def test_k_saves_the_missing_key_of_the_selected_transport(self) -> None:
        from claude_multi import operator as operator_mod, profile, readiness, state
        from claude_multi.cli import doctor

        name = operator_mod.transport_alternative("anthropic", operator_mod.TRANSPORT_API_KEY).secret_name
        key = self.root / "platform.key"
        state.atomic_write(key, b"anthropic-dummy-key\n")
        code, _out, err = self.op(["providers", "transport", "anthropic", "api-key", "--secret-file", str(key)],
                                  "y\n")
        self.assertEqual(code, 0, err)
        self.store().delete(name)
        lcat = self.runtime.lineup_catalog()
        lead = next(k for k, entry in sorted(lcat.lines.items())
                    if entry["provider"] == "anthropic" and entry.get("lead") is not None)
        evaluation = profile.evaluate(profile.ad_hoc_direct(lead), lcat, bindings={},
                                      effective=self.runtime.current_effective(), ad_hoc=True)
        rows = self.runtime.lineup_readiness(
            evaluation.lineup, readiness.Observations(served=None, oauth_records={}, lan={}, login={}))
        first = rows[0].first
        self.assertEqual((first.code, first.authority), ("credential", True))
        self.assertIn(name, first.text)
        self.assertIn("G providers → K", first.remedy)
        self.assertNotIn("providers transport", rows[0].text())
        screen = self.screen("anthropic")
        win = self.run_screen(screen, ["K", *"anthropic-tui-dummy", ENTER, ENTER])
        self.assertTrue(any(cli_text.KEY_MODAL_TITLE.format(display="Anthropic") in frame for frame in win.frames))
        self.assertEqual(self.store().get(name), "anthropic-tui-dummy")
        self.assertIn("API key saved (19 chars)", screen.message)
        problems, _info, _attention = doctor._collect_doctor_reports(self.runtime)
        self.assertFalse([line for line in problems if "provider anthropic transport" in line], problems)


class PresetTests(ProvidersCase):
    """N on Providers: the reviewed presets with an API key in the
    new-provider picker, through the flow Get started uses (its name and
    address, which key when another provider uses the preset's, the route
    approval, the key); an OpenAI-compatible preset stays closed while that
    route is. The address is the preset's by default and is checked like
    ``providers add --preset --base-url``. The shipped presets (the fixture
    assets carry none)."""

    def setUp(self) -> None:
        super().setUp()
        import test_presets

        patcher = test_presets.shipped_presets()
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        self.presets = test_presets.shipped()
        self.first = test_presets.first
        self.keyed_route = test_presets.keyed_route

    def to_entry(self, entry_id: str) -> list:
        """From the new-provider picker's first entry down to ``entry_id``."""

        import claude_multi.cli.screens.get_started as get_started

        picker = get_started._ProviderPicker(self.runtime, tui.MONO_PALETTE, only="other")
        entries = [row.entry.id for row in picker.rows if row.kind == "entry" and row.entry is not None]
        return [HOME, *([DOWN] * entries.index(entry_id))]

    def existing_instance(self, name: str, provider_id: str, key: str) -> None:
        from claude_multi.setup import providers as layer

        plan = layer.plan_add_preset(self.runtime, name, provider_id, None)
        layer.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan), model.Secret(key))

    def test_n_adds_a_preset_with_its_route_and_key(self) -> None:
        name = self.first("anthropic-compatible")
        info = self.presets[name]
        screen = self.screen()
        win = self.run_screen(screen, ["n", *self.to_entry(f"preset:{name}"), ENTER,
                                       ENTER,  # the preset's own name
                                       ENTER,  # its own address
                                       ENTER,  # declare
                                       RIGHT, ENTER,  # Approve
                                       *"providers-n-dummy", ENTER, ENTER,
                                       ESC,  # add models later
                                       ESC])  # the picker
        frames = "\n".join(win.frames)
        self.assertIn(cli_text.PICKER_TITLE_OTHER, frames)
        self.assertIn(cli_text.PRESET_FORM_TITLE.format(display=info.display), frames)
        self.assertIn(views.PRESET_ADDRESS_LABEL, frames)
        self.assertIn(info.base_url, frames)
        self.assertIn(cli_text.APPROVE_TITLE.format(id=name), frames)
        self.assertNotIn("providers-n-dummy", frames)
        declared = self.runtime.lineup_catalog().providers[name]
        self.assertEqual(declared["transport"]["base_url"], info.base_url)
        self.assertEqual(self.store().get(info.secret_name), "providers-n-dummy")
        self.assertIn(name, self.ledger().routes)
        self.assertTrue(screen.message.startswith(f"Added {name}"), screen.message)
        self.assertIn(name, [row.id for row in screen.rows])

    def test_n_gives_one_more_instance_its_own_key(self) -> None:
        from claude_multi.setup import texts as setup_texts

        name = self.first("anthropic-compatible")
        shared = self.presets[name].secret_name
        self.existing_instance(name, "first", "first-n-dummy")
        screen = self.screen()
        win = self.run_screen(screen, ["n", *self.to_entry(f"preset:{name}"), ENTER,
                                       *([BACKSPACE] * len(name)), *"second", ENTER,  # its name
                                       ENTER,  # its own address
                                       ENTER,  # which key: its own (the default)
                                       ENTER,  # declare
                                       RIGHT, ENTER,  # Approve
                                       *"second-n-dummy", ENTER, ENTER,
                                       ESC,  # add models later
                                       ESC])  # the picker
        frames = "\n".join(win.frames)
        self.assertIn(cli_text.PRESET_KEY_TITLE.format(id="second"), frames)
        self.assertIn(setup_texts.PRESET_KEY_SHARED.format(name=shared, providers="first"), frames)
        self.assertNotIn("n-dummy", frames)
        self.assertEqual(self.store().get(shared), "first-n-dummy")
        self.assertEqual(self.store().get(f"{shared}_SECOND"), "second-n-dummy")
        self.assertTrue(screen.message.startswith("Added second"), screen.message)

    def test_n_keeps_an_openai_compatible_preset_closed(self) -> None:
        from claude_multi import operator as operator_mod
        from claude_multi.setup import texts as setup_texts

        name = self.first(operator_mod.KEYED_KIND)
        before = self.state_bytes()
        with self.keyed_route(False):
            screen = self.screen()
            win = self.run_screen(screen, ["n", *self.to_entry(f"preset:{name}"), ENTER, ESC])
        self.assertTrue(any(setup_texts.OPENAI_COMPAT_CLOSED_NOTE in frame for frame in win.frames))
        form = cli_text.PRESET_FORM_TITLE.format(display=self.presets[name].display)
        self.assertFalse(any(form in frame for frame in win.frames))
        self.assertEqual(self.state_bytes(), before)

    def address(self, name: str, typed: str) -> list:
        """N → the preset → its own name → its address replaced by ``typed``."""

        return ["n", *self.to_entry(f"preset:{name}"), ENTER, ENTER,
                *([BACKSPACE] * len(self.presets[name].base_url)), *typed, ENTER]

    def test_the_address_is_checked_like_the_command_lines_base_url(self) -> None:
        name = self.first("anthropic-compatible")
        for typed, rule in (("http://workspace.example.com/anthropic", "a secret is sent only over https"),
                            ("https://workspace.example.com/anthropic?region=intl", "no query or fragment")):
            with self.subTest(address=typed):
                before = self.state_bytes()
                screen = self.screen()
                self.run_screen(screen, [*self.address(name, typed), ESC])
                self.assertIn(rule, screen.message)
                self.assertEqual(self.state_bytes(), before)
                code, out, err = self.op(["providers", "add", "--preset", name, "--base-url", typed], "n\n")
                self.assertEqual(code, 1, out + err)
                problem = screen.message.split("not declared:", 1)[1].strip()
                self.assertIn(problem, err)
                self.assertEqual(self.state_bytes(), before)

    def test_another_documented_address_is_declared_and_approved(self) -> None:
        from claude_multi import operator as operator_mod

        for kind, typed in (("anthropic-compatible", "https://workspace.example.com/apps/anthropic"),
                            (operator_mod.KEYED_KIND, "https://eu.example.com/openai/v1")):
            name = self.first(kind)
            info = self.presets[name]
            with self.subTest(kind=kind), self.keyed_route(True):
                screen = self.screen()
                win = self.run_screen(screen, [*self.address(name, typed),
                                               ENTER,  # declare
                                               RIGHT, ENTER,  # Approve
                                               *"address-n-dummy", ENTER, ENTER,
                                               ESC,  # add models later
                                               ESC])  # the picker
                self.assertTrue(any(typed in frame for frame in win.frames if cli_text.ENDPOINT_PREVIEW_TITLE in frame))
                provider = json.loads((self.pdir / f"{name}.json").read_text())["provider"]
                self.assertEqual(provider["base_url"], typed)
                if info.listing_url is not None:
                    # The listing moves with the address: the key goes to that origin only.
                    self.assertTrue(provider["listing"]["url"].startswith(typed.rstrip("/") + "/"), provider)
                self.assertEqual(self.ledger().routes[name]["origin"],
                                 operator_mod.normalize_endpoint(typed, keyed=True, lan=False)[0])
                self.assertEqual(self.store().get(info.secret_name), "address-n-dummy")
                self.assertTrue(screen.message.startswith(f"Added {name}"), screen.message)


class SharedKeyTests(ProvidersCase):
    """K on a provider whose saved key another provider shares (one more
    provider from a preset that shares the saved key): a confirmation names
    every provider using it, Cancel focused first; Cancel keeps the key of
    both, Replace then takes the new key for both. The shipped presets."""

    def setUp(self) -> None:
        super().setUp()
        import test_presets
        from claude_multi.setup import providers as layer

        patcher = test_presets.shipped_presets()
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        name = test_presets.first("anthropic-compatible")
        self.info = test_presets.shipped()[name]
        plan = layer.plan_add_preset(self.runtime, name, "first", None)
        layer.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan), model.Secret("shared-tui-key"))
        plan = layer.plan_add_preset(self.runtime, name, "second", None, key=layer.KEY_REUSE)
        layer.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan), None)

    def test_k_names_every_provider_and_cancel_keeps_the_key_of_both(self) -> None:
        shared = self.info.secret_name
        title = cli_text.SHARED_KEY_REPLACE_TITLE.format(providers="first, second")
        before = self.state_bytes()
        screen = self.screen("first")
        win = self.run_screen(screen, ["K", ENTER])  # Cancel is focused first
        shown = self.frame_with(win, title)
        self.assertIn(shared, shown)
        self.assertFalse(any(cli_text.KEY_MODAL_BODY.split("\n", 1)[0] in frame for frame in win.frames),
                         "no key is asked for")
        self.assertEqual(self.store().get(shared), "shared-tui-key")
        self.assertEqual(screen.message, texts.KEY_KEPT.format(display=self.info.display))
        self.assertEqual(self.state_bytes(), before)
        # Replace, then the key (Replace again in the key modal): both use it.
        screen = self.screen("second")
        win = self.run_screen(screen, ["k", RIGHT, ENTER, *"shared-new-tui-key", ENTER, RIGHT, ENTER,
                                       ESC])  # add models later
        self.assertTrue(any(title in frame for frame in win.frames))
        self.assertEqual(self.store().get(shared), "shared-new-tui-key")
        self.assertNotIn("shared-new-tui-key", "\n".join(win.frames))
        self.assertIn("API key saved (18 chars)", screen.message)

    def test_the_confirmation_fits_80x24(self) -> None:
        screen = self.screen("first")
        win = self.run_screen(screen, ["K", ESC], height=24, width=80)
        shown = self.frame_with(win, cli_text.SHARED_KEY_REPLACE_TITLE.format(providers="first, second"))
        for line in shown.splitlines():
            self.assertLessEqual(len(line.rstrip()), 80)
        self.assertIn("Cancel keeps it.", " ".join(line.strip(" |") for line in shown.splitlines()))


class OwnProviderTests(ProvidersCase):
    def test_x_names_the_blockers_first(self) -> None:
        self.declare_small()
        self.serve_current()
        code, _out, err = self.op(["models", "admit", "custom-acme-small"], "y\n")
        self.assertEqual(code, 0, err)
        screen = self.screen("acme")
        win = self.run_screen(screen, ["X", DOWN, ENTER, ENTER])
        chooser = self.frame_with(win, cli_text.REMOVE_CHOICE_TITLE.format(id="acme"))
        for item in (cli_text.REMOVE_CHOICE_KEY, cli_text.REMOVE_CHOICE_PROVIDER, cli_text.REMOVE_CHOICE_CANCEL):
            self.assertIn(item, chooser)
        blocked = self.frame_with(win, cli_text.REMOVE_BLOCKED_HEAD)
        self.assertIn(cli_text.REMOVE_BLOCKED_TITLE.format(id="acme"), blocked)
        self.assertIn("custom-acme-small", blocked)
        self.assertTrue((self.pdir / "acme.json").exists())

    def test_removing_a_provider_offers_its_key_too(self) -> None:
        code, _out, err = self.op(test_cli.ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        screen = self.screen("acme")
        # Remove the provider (confirm), then its key too (confirm both).
        self.run_screen(screen, ["X", DOWN, ENTER, RIGHT, ENTER, RIGHT, ENTER, RIGHT, ENTER])
        self.assertFalse((self.pdir / "acme.json").exists())
        self.assertIsNone(self.store().get("ACME_API_KEY"))
        self.assertNotIn("acme", [row.id for row in screen.rows])

    def test_enter_approves_an_unapproved_route_then_asks_for_nothing_more(self) -> None:
        code, _out, err = self.op([*test_cli.ACME_ADD, "--declare-only"])
        self.assertEqual(code, 0, err)
        screen = self.screen("acme")
        self.assertEqual(screen._primary(screen._fact()), "approve")
        win = self.run_screen(screen, [ENTER, RIGHT, ENTER])
        self.assertTrue(any(cli_text.APPROVE_TITLE.format(id="acme") in frame for frame in win.frames))
        self.assertIn("acme", self.ledger().routes)
        self.assertNotEqual(screen._primary(screen._fact()), "approve")


class ApplyTests(ProvidersCase):
    def test_p_applies_only_when_it_is_offered(self) -> None:
        with mock.patch.object(providers_screen._ProvidersScreen, "_apply_needed", return_value=False), \
                mock.patch.object(providers_screen.ConnectActions, "apply") as apply:
            screen = self.screen()
            win = self.run_screen(screen, ["P"])
        apply.assert_not_called()
        self.assertEqual(screen.message, texts.NOTHING_TO_APPLY)
        self.assertNotIn("P apply", win.frames[0])
        self.assertNotIn(cli_text.PROVIDERS_APPLY_BANNER, win.frames[0])
        with mock.patch.object(providers_screen._ProvidersScreen, "_apply_needed", return_value=True), \
                mock.patch.object(providers_screen.ConnectActions, "apply",
                                  return_value=providers_screen.Outcome("applied")) as apply:
            screen = self.screen()
            win = self.run_screen(screen, ["p"])
        # Offered because the gateway does not serve the setup: the apply runs even without a plan change.
        apply.assert_called_once_with(not_served=True)
        self.assertIn("P apply", win.frames[0])
        self.assertIn(cli_text.PROVIDERS_APPLY_BANNER, win.frames[0])
        self.assertEqual(screen.message, "applied")

    def test_a_gateway_that_missed_the_current_setup_is_applied_and_verified(self) -> None:
        # The configuration on disk is current (a real render), but the
        # running gateway serves none of it: P must apply and verify.
        from claude_multi.setup import providers as layer

        self.runtime.render_gateway(policy="explicit")
        self.assertFalse(layer.plan_apply(self.runtime).changes)
        self.served = set()
        screen = self.screen()
        self.assertTrue(screen.apply_offer)
        win = self.run_screen(screen, ["P", RIGHT, ENTER])
        shown = next(frame for frame in win.frames if cli_text.APPLY_TITLE in frame)
        self.assertIn(texts.APPLY_NOT_SERVED, " ".join(line.strip(" |") for line in shown.splitlines()))
        self.runtime.verify_reload.assert_called_once()
        self.assertNotEqual(screen.message, texts.NOTHING_TO_APPLY)
        self.assertTrue(screen.message.startswith("Applied — "), screen.message)

    def test_apply_previews_and_cancel_writes_nothing(self) -> None:
        import dataclasses
        from claude_multi.setup import providers as layer

        actions = providers_screen.ConnectActions(self.runtime, FakeWindow([ENTER]), tui.MONO_PALETTE)
        current = layer.plan_apply(self.runtime)
        with mock.patch.object(layer, "plan_apply", return_value=dataclasses.replace(current, changes=False)), \
                mock.patch.object(tui, "Modal", side_effect=AssertionError("nothing to confirm")):
            self.assertEqual(actions.apply().text, texts.NOTHING_TO_APPLY)
        plan = dataclasses.replace(current, changes=True)
        before = self.state_bytes()
        with mock.patch.object(layer, "plan_apply", return_value=plan), \
                mock.patch.object(layer, "apply_apply", side_effect=AssertionError("cancelled")):
            outcome = actions.apply()
        self.assertEqual(outcome, providers_screen.Outcome(model.NOTHING_CHANGED, "warn"))
        self.assertEqual(self.state_bytes(), before)


# The Anthropic API-key consent's title, literally.
ANTHROPIC_KEY_TITLE = "Use an Anthropic API key for Claude models?"


class AnthropicTransportTests(ProvidersCase):
    def transport(self) -> str | None:
        from claude_multi.setup import providers as layer

        return layer.current_transport(self.runtime, "anthropic")

    def test_enter_on_anthropic_switches_the_account_to_an_api_key(self) -> None:
        from claude_multi import operator as operator_mod

        self.assertNotEqual(self.transport(), operator_mod.TRANSPORT_API_KEY)
        screen = self.screen("anthropic")
        self.assertEqual(screen._primary(screen._fact()), "connect")
        # Enter → the chooser → the API key → the consent (Cancel first) →
        # Switch → the key → Save.
        win = self.run_screen(screen, [ENTER, DOWN, ENTER, RIGHT, ENTER, *"sk-ant-dummy-123456", ENTER, ENTER])
        frames = "\n".join(win.frames)
        self.assertIn(cli_text.TRANSPORT_CHOOSER_TITLE.format(display="Anthropic"), frames)
        # The title its chooser has always shown, word for word.
        self.assertIn(ANTHROPIC_KEY_TITLE, frames)
        self.assertEqual(cli_text.TRANSPORT_TO_KEY_TITLES["anthropic"], ANTHROPIC_KEY_TITLE)
        self.assertEqual(self.transport(), operator_mod.TRANSPORT_API_KEY)
        alternative = operator_mod.transport_alternative("anthropic", operator_mod.TRANSPORT_API_KEY)
        self.assertEqual(self.store().get(alternative.secret_name), "sk-ant-dummy-123456")
        # Cancel at the consent changes nothing.
        before = self.state_bytes()
        screen = self.screen("anthropic")
        self.run_screen(screen, [ENTER, HOME, ENTER, ENTER])
        self.assertEqual(self.transport(), operator_mod.TRANSPORT_API_KEY)
        self.assertEqual(self.state_bytes(), before)


class OpenAITransportTests(ProvidersCase):
    """OpenAI's account or its API key on the Providers screen: Enter opens
    the chooser (the key route lists the models reviewed for it), the
    switch runs through the setup layer's consent and transaction, K
    replaces the key in use, and switching back offers removing the unused
    key. The fixture assets with one OpenAI line reviewed for the key."""

    def setUp(self) -> None:
        super().setUp()
        import shutil
        import tempfile
        import test_openai_key_route as key_route

        temporary = Path(tempfile.mkdtemp(prefix="cm-openai-transport-"))
        self.addCleanup(shutil.rmtree, temporary, True)
        self.runtime.asset_root, self.reviewed = key_route.reviewed_asset_root(temporary)
        self.runtime.reload_catalog()

    def transport(self) -> str | None:
        from claude_multi.setup import providers as layer

        return layer.current_transport(self.runtime, "openai")

    def secret(self) -> str:
        from claude_multi import operator as operator_mod

        return operator_mod.transport_alternative("openai", operator_mod.TRANSPORT_API_KEY).secret_name

    def config(self) -> str:
        import claude_multi.proxy

        return (claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml").read_text()

    def switch_to_key(self, value: str) -> FakeWindow:
        """Enter → the chooser → the API key → the consent (Cancel first) →
        Switch → the key → Save."""

        screen = self.screen("openai")
        return self.run_screen(screen, [ENTER, DOWN, ENTER, RIGHT, ENTER, *value, ENTER, ENTER])

    def test_enter_on_openai_switches_the_account_to_an_api_key(self) -> None:
        from claude_multi import operator as operator_mod, render

        self.assertNotEqual(self.transport(), operator_mod.TRANSPORT_API_KEY)
        screen = self.screen("openai")
        self.assertEqual(screen._primary(screen._fact()), "connect")
        win = self.run_screen(screen, [ENTER, DOWN, ENTER, RIGHT, ENTER, *"sk-openai-dummy-123456", ENTER, ENTER])
        frames = "\n".join(win.frames)
        self.assertIn(cli_text.TRANSPORT_CHOOSER_TITLE.format(display="OpenAI"), frames)
        self.assertIn(self.reviewed, frames, "the chooser names the models reviewed for the key")
        self.assertIn(cli_text.TRANSPORT_TO_KEY_TITLE.format(display="OpenAI", models="OpenAI models"), frames)
        self.assertNotIn("sk-openai-dummy-123456", frames)
        self.assertEqual(self.transport(), operator_mod.TRANSPORT_API_KEY)
        self.assertEqual(self.store().get(self.secret()), "sk-openai-dummy-123456")
        config = self.config()
        self.assertIn(f"{render.CODEX_KEY_SECTION}:", config)
        self.assertIn(f'{render.IMAGE_GENERATION_KEY}: "{render.IMAGE_GENERATION_PASSTHROUGH}"', config)
        self.assertIn("OpenAI models now use your OpenAI API key", screen.message)
        # The row now offers its key actions; Enter opens the chooser again.
        self.assertEqual(screen._primary(screen._fact()), "connect")

    def test_k_replaces_the_openai_key_in_use(self) -> None:
        self.switch_to_key("sk-openai-dummy-123456")
        screen = self.screen("openai")
        # K on the key in use: the replace modal (Cancel is the default).
        win = self.run_screen(screen, ["K", *"sk-openai-replacement", ENTER, ENTER])
        self.assertIn("A key is set (22 chars)", self.frame_with(win, cli_text.KEY_MODAL_REPLACE.split("(")[0]))
        self.assertEqual(self.store().get(self.secret()), "sk-openai-dummy-123456")
        self.run_screen(screen, ["k", *"sk-openai-replacement", ENTER, RIGHT, ENTER])
        self.assertEqual(self.store().get(self.secret()), "sk-openai-replacement")
        self.assertIn("API key saved (21 chars)", screen.message)

    def test_switching_back_offers_removing_the_unused_key(self) -> None:
        from claude_multi import operator as operator_mod, render

        self.switch_to_key("sk-openai-dummy-123456")
        screen = self.screen("openai")
        # Enter → the account → Switch → "Remove it" → the removal's own
        # confirmation (Cancel first) → Remove → "sign in now?" Later.
        win = self.run_screen(screen, [ENTER, HOME, ENTER, RIGHT, ENTER, RIGHT, ENTER, RIGHT, ENTER, ENTER])
        frames = "\n".join(win.frames)
        self.assertIn(cli_text.TRANSPORT_TO_ACCOUNT_TITLE.format(account="ChatGPT account", models="OpenAI models"),
                      frames)
        self.assertIn(cli_text.KEEP_KEY_TITLE.format(display="OpenAI"), frames)
        self.assertIn(cli_text.SIGN_IN_NOW_TITLE.format(account="ChatGPT account"), frames)
        self.assertEqual(self.transport(), operator_mod.TRANSPORT_POOL)
        self.assertIsNone(self.store().get(self.secret()))
        self.assertNotIn(render.IMAGE_GENERATION_KEY, self.config())

    def test_without_a_reviewed_model_enter_signs_in(self) -> None:
        # The fixture itself reviews no OpenAI line: the account flow only.
        from _catalog import FIXTURE_ROOT

        self.runtime.asset_root = FIXTURE_ROOT
        self.runtime.reload_catalog()
        screen = self.screen("openai")
        self.assertEqual(screen._primary(screen._fact()), "sign-in")


class AccountTests(ProvidersCase):
    def record(self, name: str) -> None:
        directory = signin.auth_dir(self.runtime)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (directory / name).write_bytes(b"{}")

    def test_l_on_a_keyed_row_names_k(self) -> None:
        pid = self.keyed(present=True)
        screen = self.screen(pid)
        self.run_screen(screen, ["L"])
        display = self.runtime.catalog.providers[pid].get("display", pid)
        self.assertEqual(screen.message, cli_text.L_NOT_ACCOUNT.format(display=display))

    def test_l_on_an_account_that_is_not_signed_in(self) -> None:
        screen = self.screen("openai")
        win = self.run_screen(screen, ["l", ENTER])
        shown = self.frame_with(win, cli_text.L_BODY_NOT_SIGNED_IN)
        self.assertIn("[ Close ]", shown)
        self.assertIn("[ Sign in ]", shown)

    def test_the_separate_login_line_follows_the_pools_data(self) -> None:
        # The managed client signs in with a Claude account itself: its
        # modal says the sign-in is separate; a ChatGPT account's does not,
        # unless the pool data says the same of it.
        self.record("claude-a@example.com.json")
        self.record("codex-b@example.com.json")
        for pid, patched, expected in (("anthropic", None, True), ("openai", None, False),
                                       ("openai", pools_with("codex", client_account=True), True)):
            with self.subTest(provider=pid, patched=patched is not None), patched or contextlib.nullcontext():
                win = self.run_screen(self.screen(pid), ["L", ENTER])
                modal = self.frame_with(win, cli_text.L_BUTTONS_SIGNED_IN[1][0])
                # The line wraps in the dialog: its first words identify it.
                self.assertEqual(" ".join(cli_text.L_BODY_SEPARATE.split()[:6]) in modal, expected)

    def test_sign_out_asks_and_cancel_keeps_the_account(self) -> None:
        self.record("claude-a@example.com.json")
        screen = self.screen("anthropic")
        win = self.run_screen(screen, ["L", RIGHT, RIGHT, ENTER, ENTER])
        modal = self.frame_with(win, cli_text.L_BODY_SIGNED_IN.format(accounts="a@example.com"))
        self.assertIn("[ Sign out ]", modal)
        self.assertTrue(any(cli_text.SIGNOUT_TITLE.format(kind=texts.ACCOUNT_KINDS["claude"]) in frame
                            for frame in win.frames))
        self.assertEqual(signin.accounts(self.runtime, "anthropic"), ("a@example.com",))
        self.assertEqual(screen.message, model.NOTHING_CHANGED)

    def test_x_on_a_signed_in_account_signs_out_through_the_layer(self) -> None:
        self.record("codex-a@example.com.json")
        screen = self.screen("openai")
        outcome = signin.SignOutOutcome("stopped", ("codex-a@example.com.json",), (), "backup",
                                        ("Signed out of a@example.com",), "reloaded")
        with mock.patch.object(signin, "apply_sign_out", return_value=outcome) as apply:
            win = self.run_screen(screen, ["X", RIGHT, ENTER, ENTER])
        apply.assert_called_once()
        self.assertTrue(any(cli_text.SIGNOUT_TITLE.format(kind=texts.ACCOUNT_KINDS["codex"]) in frame
                            for frame in win.frames))
        self.assertEqual(screen.message, "Signed out of a@example.com")


class SignInFlowTests(test_setup_signin.SignInCase):
    """Enter on an account that is not signed in: the typed acknowledgement,
    then the gateway program's sign-in in this terminal (a stand-in runner
    here); the record diff decides the result."""

    def setUp(self) -> None:
        super().setUp()
        for name in consent.SESSION_MARKERS:
            self.runtime.environ.pop(name, None)
        self.runs: list[object] = []

        def runner(invocation) -> int:
            self.runs.append(invocation)
            self.record("codex-a@example.com.json")
            return 0

        self.runtime.signin_runner = runner
        self.printed = io.StringIO()
        patches = (
            mock.patch.object(consent, "stdio_ttys", return_value=True),
            mock.patch.object(screens_signin, "_wait_for_enter"),
            mock.patch("sys.stdout", self.printed),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def screen(self) -> providers_screen._ProvidersScreen:
        screen = providers_screen._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
        screen.selected = [row.id for row in screen.rows].index("openai")
        return screen

    def test_a_wrong_word_changes_nothing_and_personal_signs_in(self) -> None:
        screen = self.screen()
        self.assertEqual(screen._primary(screen._fact()), "sign-in")
        win = FakeWindow([ENTER, *"business", ENTER, ENTER, ESC], height=24, width=80)
        screen.run(win)
        self.assertTrue(any(cli_text.ACK_TITLE.format(kind=texts.ACCOUNT_KINDS["codex"]) in frame
                            for frame in win.frames))
        self.assertEqual(screen.message, texts.ACK_WRONG)
        self.assertEqual(self.runs, [])
        self.assertIsNone(signin.current_ack(self.runtime.environ, "codex"))
        win = FakeWindow([ENTER, *"personal", ENTER, ENTER, ENTER, ESC], height=24, width=80)
        screen.run(win)
        self.assertEqual(len(self.runs), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            header = signin.header(signin.plan_sign_in(self.runtime, "openai"))
        self.assertIn(header, self.printed.getvalue())
        self.assertTrue(any(cli_text.SIGNIN_RESULT_TITLE.format(kind=texts.ACCOUNT_KINDS["codex"]) in frame
                            for frame in win.frames))
        self.assertEqual(screen.message, texts.SIGNIN_OK.format(account="a@example.com",
                                                                kind=texts.ACCOUNT_KINDS["codex"]))
        self.assertEqual(signin.accounts(self.runtime, "openai"), ("a@example.com",))
        self.assertEqual(screen._primary(screen._fact()), "account")

    def test_a_cancelled_sign_in_writes_no_record(self) -> None:
        self.runtime.signin_runner = lambda invocation: self.runs.append(invocation) or 130
        screen = self.screen()
        screen.run(FakeWindow([ENTER, *"personal", ENTER, ENTER, ENTER, ESC], height=24, width=80))
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(screen.message, texts.SIGNIN_CANCELLED)
        self.assertEqual(signin.accounts(self.runtime, "openai"), ())

    def test_the_method_picker_follows_the_pools_sign_in_methods(self) -> None:
        # A pool whose sign-in opens one way (a device code) asks nothing
        # more after the word; one that opens two ways (a browser flow)
        # offers both, titled with its account kind.
        kind = texts.ACCOUNT_KINDS["codex"]
        title = cli_text.SIGNIN_METHOD_TITLE.format(kind=kind)
        for flow, picker in (("device", False), ("browser", True)):
            with self.subTest(flow=flow), pools_with("codex", flow=flow), \
                    mock.patch.object(signin, "plan_sign_in", side_effect=model.Refused("stopped after the method")):
                win = FakeWindow([*"personal", ENTER, ENTER, ENTER], height=24, width=80)
                screens_signin.run_sign_in_flow(self.runtime, win, tui.MONO_PALETTE, "openai")
                shown = [frame for frame in win.frames if title in frame]
                self.assertEqual(bool(shown), picker)
                if picker:
                    for _method, label in cli_text.SIGNIN_METHODS:
                        self.assertIn(label, shown[0])
                self.assertEqual(self.runs, [])

    def acknowledge_claude(self, keys: list, *, height: int, width: int) -> FakeWindow:
        """The Claude account sign-in up to its acknowledgement (the sign-in
        itself is stopped right after it, before anything runs)."""

        win = FakeWindow(keys, height=height, width=width)
        with mock.patch.object(signin, "pool_offered", return_value=True), \
                mock.patch.object(signin, "plan_sign_in", side_effect=model.Refused("stopped after the word")):
            screens_signin.run_sign_in_flow(self.runtime, win, tui.MONO_PALETTE, "anthropic")
        return win

    def test_a_small_terminal_shows_the_whole_acknowledgement_before_the_word(self) -> None:
        # 60x18 is the smallest Get started and its provider picker draw at.
        win = self.acknowledge_claude([END, ENTER, *"personal", ENTER, ENTER, ENTER], height=18, width=60)
        text = signin.ack_text("claude")[1]
        lines = [line for line in screens_common.modal_lines(text, FakeWindow([], height=18, width=60))
                 if line.strip()]
        for line in lines:
            with self.subTest(line=line):
                self.assertTrue(any(line in frame for frame in win.frames), "every line of the text can be read")
        typed = [frame for frame in win.frames if "[ Continue ]" in frame and "[ Cancel ]" in frame]
        self.assertTrue(typed)
        self.assertTrue(all("Type personal to confirm" in frame for frame in typed),
                        "the field is shown with the word to type")
        self.assertIsNotNone(signin.current_ack(self.runtime.environ, "claude"))

    def test_esc_on_the_text_records_nothing(self) -> None:
        self.acknowledge_claude([ESC], height=18, width=60)
        self.assertIsNone(signin.current_ack(self.runtime.environ, "claude"))

    def test_a_terminal_that_fits_shows_one_dialog(self) -> None:
        win = self.acknowledge_claude([*"personal", ENTER, ENTER, ENTER], height=24, width=80)
        self.assertIsNotNone(signin.current_ack(self.runtime.environ, "claude"))
        title = cli_text.ACK_TITLE.format(kind=texts.ACCOUNT_KINDS["claude"])
        dialogs = [frame for frame in win.frames if title in frame]
        self.assertTrue(dialogs)
        self.assertTrue(all("[ Cancel ]" in frame and "Type personal to confirm" in frame for frame in dialogs))


class TestAndLetterTests(ProvidersCase):
    def test_t_asks_once_and_a_no_sends_nothing(self) -> None:
        self.apply_and_serve()
        pid = self.keyed(present=True)
        screen = self.screen(pid)
        win = self.run_screen(screen, ["T", "n"])
        self.assertTrue(any(cli_text.TEST_CONSENT_TITLE in frame for frame in win.frames))
        self.assertEqual(self.calls, [])
        self.assertEqual(screen.message, cli_text.TEST_DECLINED)
        self.run_screen(screen, ["t", "y"])
        self.assertEqual(len(self.calls), 1)

    def test_letters_work_in_both_cases_and_j_k_do_not_move(self) -> None:
        screen = self.screen()
        start = screen.selected
        self.run_screen(screen, ["j", "J"])
        self.assertEqual(screen.selected, start)
        for letter in ("q", "Q"):
            with self.subTest(letter=letter):
                win = self.run_screen(self.screen(), [letter, ESC])
                self.assertTrue(any(cli_text.DETAILS_TITLE in frame for frame in win.frames))


def remove_blocked_golden(root: Path) -> str:
    """X on a provider you added whose model is admitted (tests/goldens/tui)."""

    import _tui_render

    case = OwnProviderTests()
    case.setUp()
    try:
        case.declare_small()
        case.serve_current()
        case.op(["models", "admit", "custom-acme-small"], "y\n")
        win = case.run_screen(case.screen("acme"), ["X", DOWN, ENTER, ENTER],
                              height=_tui_render.HEIGHT, width=_tui_render.WIDTH)
        return _tui_render.last_frame(win, cli_text.REMOVE_BLOCKED_HEAD)
    finally:
        case.doCleanups()


def signout_modal_golden(root: Path) -> str:
    """The sign-out confirmation of a Claude account (tests/goldens/tui)."""

    import _tui_render

    import datetime

    case = AccountTests()
    case.setUp()
    dated = signin.backup_dir
    try:
        case.record("claude-a@example.com.json")
        with mock.patch.object(signin, "backup_dir", lambda directory, pool, today=None: dated(
                directory, pool, today=datetime.date(2026, 10, 3))):
            win = case.run_screen(case.screen("anthropic"), ["L", RIGHT, RIGHT, ENTER, ENTER],
                                  height=_tui_render.HEIGHT, width=_tui_render.WIDTH)
        return _tui_render.last_frame(win, cli_text.SIGNOUT_TITLE.format(kind=texts.ACCOUNT_KINDS["claude"]))
    finally:
        case.doCleanups()


if __name__ == "__main__":
    unittest.main()
