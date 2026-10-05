"""API keys, the shipped providers' on/off, removing a provider you added
and the consented connection test: the setup layer's plans and applies and
the ``providers`` verbs over them (fixture gateway; no provider request)."""

from __future__ import annotations

import errno
import io
import shlex
from pathlib import Path
from unittest import mock

import claude_multi.proxy
import test_cli
from claude_multi import custom, errors, operator as operator_mod, paths, secret_store, state, strict_json
from claude_multi.setup import model, providers as setup_providers, texts
import claude_multi.cli.commands.providers as providers_cmd
import claude_multi.cli.consent as consent

KIMI = "KIMI_CLAUDE_API_KEY"
ANTHROPIC_KEY = operator_mod.transport_alternative("anthropic", operator_mod.TRANSPORT_API_KEY).secret_name


class CredentialCase(test_cli.OperatorCommandCase):
    def store(self) -> secret_store.FileSecretStore:
        return secret_store.default_store(self.runtime.gateway_environ())

    def human(self):
        return mock.patch.object(consent, "stdio_ttys", return_value=True)


class SetKeyLayerTests(CredentialCase):
    def test_plan_names_the_key_and_never_its_value(self) -> None:
        with self.human():
            plan = setup_providers.plan_set_key(self.runtime, "kimi")
        self.assertEqual((plan.secret_name, plan.replaces, plan.current_length), (KIMI, True, len("cli-test-dummy")))
        self.assertTrue(plan.guarded)
        self.assertNotIn("cli-test-dummy", repr(plan))
        self.assertEqual(plan.path_display, str(self.secret_file))

    def test_apply_saves_and_reloads(self) -> None:
        with self.human():
            plan = setup_providers.plan_set_key(self.runtime, "kimi")
            applied = setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                                    model.Secret("kimi-new-dummy"))
        self.assertEqual(self.store().get(KIMI), "kimi-new-dummy")
        self.assertEqual(applied.reload, "reloaded")
        self.assertTrue(applied.lines[0].startswith("Kimi API key saved (14 chars) — the gateway reloaded"),
                        applied.lines)
        self.assertNotIn("kimi-new-dummy", "".join(applied.lines))

    def test_the_apply_contract(self) -> None:
        with self.human():
            plan = setup_providers.plan_set_key(self.runtime, "kimi")
            other = model.Confirmation("sha256:other", plan.consent, "2026-10-03T00:00:00Z")
            with self.assertRaisesRegex(model.Refused, model.CONFIRMATION_MISMATCH):
                setup_providers.apply_set_key(self.runtime, plan, other, model.Secret("x-dummy"))
            for bad in ("", "has space"):
                with self.subTest(value=bad), self.assertRaises(model.Refused):
                    setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                                  model.Secret(bad))
            # The key moved after the plan: stale, nothing written.
            self.store().set(KIMI, "changed-meanwhile")
            with self.assertRaises(model.Stale):
                setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                              model.Secret("kimi-new-dummy"))
        self.assertEqual(self.store().get(KIMI), "changed-meanwhile")
        # Outside a terminal the guarded apply refuses.
        with mock.patch.object(consent, "stdio_ttys", return_value=False), self.assertRaises(errors.ClaudeMultiError):
            setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan), model.Secret("v-dummy"))
        self.runtime.allow_state_writes = False
        with self.human(), self.assertRaisesRegex(model.Refused, "read-only"):
            setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan), model.Secret("v-dummy"))

    def test_a_refused_render_restores_the_previous_key(self) -> None:
        with self.human():
            plan = setup_providers.plan_set_key(self.runtime, "kimi")
            with mock.patch.object(self.runtime, "render_gateway",
                                   side_effect=errors.ClaudeMultiError("fixture render refusal")):
                with self.assertRaises(model.Refused) as caught:
                    setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                                  model.Secret("kimi-new-dummy"))
        self.assertIn("Nothing changed — the gateway configuration was refused", str(caught.exception))
        self.assertEqual(self.store().get(KIMI), "cli-test-dummy")

    def test_an_interrupt_during_the_render_restores_the_previous_key(self) -> None:
        with self.human():
            plan = setup_providers.plan_set_key(self.runtime, "kimi")
            with mock.patch.object(self.runtime, "render_gateway", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                                  model.Secret("kimi-new-dummy"))
        self.assertEqual(self.store().get(KIMI), "cli-test-dummy")

    def test_account_providers_and_unknown_ids_refuse_with_both_fix_forms(self) -> None:
        cases = {
            "anthropic": (texts.KEY_ANTHROPIC_TRANSPORT, texts.KEY_ANTHROPIC_TRANSPORT_LINE),
            # Offered by this build: the key is chosen as the provider's transport.
            "openai": (texts.KEY_ACCOUNT_TRANSPORT.format(display="OpenAI", account="ChatGPT account"),
                       texts.KEY_ACCOUNT_TRANSPORT_LINE.format(display="OpenAI", account="ChatGPT account",
                                                               id="openai")),
            "nobody": (texts.KEY_UNKNOWN_PROVIDER.format(id="nobody"),) * 2,
            "llm-local": (None, None),
        }
        for provider_id, (tui_text, line_text) in cases.items():
            with self.subTest(provider=provider_id), self.assertRaises(model.Refused) as caught:
                setup_providers.plan_set_key(self.runtime, provider_id)
            if provider_id == "llm-local":
                self.assertIn("needs no key", str(caught.exception))
                continue
            self.assertEqual((str(caught.exception), caught.exception.line_text), (tui_text, line_text))


class LegacyKeySharerTests(CredentialCase):
    """A provider of custom.json still in effect that uses a shipped
    provider's key name shares that key: set-key names it and asks first.
    Once custom.json is migrated, or when the operator layer drops the
    provider (the key belongs to another origin), it no longer counts."""

    SAME_ORIGIN = {"base_url": "https://api.kimi.com/anthropic", "auth_kind": "header", "secret_env": KIMI}

    def legacy(self, providers: dict, models: dict | None = None) -> None:
        """custom.json as both readers find it (the shell's and the service's copy)."""

        raw = strict_json.pretty_file_bytes({"version": 1, "providers": providers, "models": models or {}})
        for path in (custom.registry_path(self.runtime.environ),
                     claude_multi.proxy.config_dir(self.runtime.home) / "custom.json"):
            state.ensure_private_dir(path.parent)
            state.atomic_write(path, raw)

    def question(self, providers: str) -> str:
        return texts.KEY_SHARED_REPLACE_PROMPT.format(name=KIMI, n=len("cli-test-dummy"), providers=providers)

    def plan(self) -> setup_providers.KeyPlan:
        with self.human():
            return setup_providers.plan_set_key(self.runtime, "kimi")

    def test_a_legacy_provider_in_effect_shares_the_key(self) -> None:
        self.legacy({"shared-legacy": self.SAME_ORIGIN})
        plan = self.plan()
        self.assertEqual((plan.shared_with, plan.key_users, plan.replaces_shared),
                         (("shared-legacy",), "kimi, shared-legacy", True))
        self.assertEqual(plan.lines, (self.question("kimi, shared-legacy").rstrip(),))
        # The verb asks with that question (default No) and keeps the key.
        code, out, err = self.op(["providers", "set-key", "kimi"], "\nnever-typed-key\n")
        self.assertEqual(code, 3, out + err)
        self.assertIn(self.question("kimi, shared-legacy"), err)
        self.assertEqual(self.store().get(KIMI), "cli-test-dummy")

    def test_a_legacy_provider_the_layer_drops_is_no_sharer(self) -> None:
        self.legacy({"elsewhere": {**self.SAME_ORIGIN, "base_url": "https://elsewhere.example/anthropic"}})
        self.assertIn("elsewhere", providers_cmd.operator_context(self.runtime).layer.legacy_dropped)
        plan = self.plan()
        self.assertEqual((plan.shared_with, plan.replaces_shared), ((), False))
        self.assertEqual(plan.lines, (texts.KEY_REPLACE_PROMPT.format(display="Kimi", n=len("cli-test-dummy"))
                                      .rstrip(),))

    def test_after_migration_custom_json_no_longer_counts(self) -> None:
        state.atomic_write(self.secret_file, b"KIMI_CLAUDE_API_KEY=cli-test-dummy\nZETA_API_KEY=zeta-dummy-value\n")
        self.legacy({"shared-legacy": self.SAME_ORIGIN,
                     "zeta": {"base_url": "https://api.zeta.example/anthropic", "auth_kind": "header",
                              "secret_env": "ZETA_API_KEY"}},
                    {"zeta-pro": {"wire_model": "zeta-pro-2", "provider": "zeta", "context_tokens": 300000,
                                  "created_via": "manual"}})
        self.assertEqual(self.plan().shared_with, ("shared-legacy",))
        code, out, err = self.op(["providers", "migrate-custom", "--apply"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(providers_cmd.operator_context(self.runtime).layer.marker)
        # The provider without models stays behind in custom.json, which is no longer read.
        self.assertFalse((self.pdir / "shared-legacy.json").exists())
        self.assertEqual(self.plan().shared_with, ())


class UnconfirmedWriteTests(CredentialCase):
    """A write whose bytes were replaced but whose folder sync failed (or
    that was interrupted) is put back; the original error still reaches the
    caller."""

    def fail_sync_once(self, directory: Path):
        real = state._fsync_directory
        failed: list[Path] = []

        def flaky(path) -> None:
            if Path(path) == directory and not failed:
                failed.append(Path(path))
                raise OSError(errno.EIO, "fixture sync failure")
            real(path)

        return mock.patch.object(state, "_fsync_directory", side_effect=flaky)

    def test_a_replaced_key_whose_sync_failed_is_put_back(self) -> None:
        with self.human():
            plan = setup_providers.plan_set_key(self.runtime, "kimi")
            with self.fail_sync_once(self.secret_file.parent), self.assertRaises(state.CommittedStateError):
                setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                              model.Secret("kimi-new-dummy"))
        self.assertEqual(self.store().get(KIMI), "cli-test-dummy")

    def test_a_removed_key_whose_sync_failed_is_put_back(self) -> None:
        with self.human():
            plan = setup_providers.plan_remove_key(self.runtime, "kimi")
            with self.fail_sync_once(self.secret_file.parent), self.assertRaises(state.CommittedStateError):
                setup_providers.apply_remove_key(self.runtime, plan, model.Confirmation.given(plan))
        self.assertEqual(self.store().get(KIMI), "cli-test-dummy")

    def test_a_transport_switch_whose_ledger_sync_failed_is_put_back(self) -> None:
        ledger_dir = operator_mod.ledger_path(self.env).parent
        with self.human():
            plan = setup_providers.plan_transport(self.runtime, "anthropic", "api-key")
            with self.fail_sync_once(ledger_dir), self.assertRaises(state.CommittedStateError):
                setup_providers.apply_transport(self.runtime, plan, model.Confirmation.given(plan),
                                                model.Secret("anthropic-dummy-key"))
        self.assertEqual(setup_providers.current_transport(self.runtime, "anthropic"), "oauth-pool")
        self.assertIsNone(self.store().get(ANTHROPIC_KEY))

    def test_a_failure_before_the_write_landed_touches_nothing(self) -> None:
        with self.human():
            plan = setup_providers.plan_set_key(self.runtime, "kimi")
            with mock.patch.object(state, "atomic_write", side_effect=OSError(errno.ENOSPC, "fixture full disk")):
                with self.assertRaises(OSError) as caught:
                    setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                                  model.Secret("kimi-new-dummy"))
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(self.store().get(KIMI), "cli-test-dummy")


class KeyFileLocationTests(CredentialCase):
    """A key plan is for one key file: another file selected while it waits
    refuses with nothing written."""

    def setUp(self) -> None:
        super().setUp()
        self.runtime.environ.pop("CLAUDE_MULTI_SECRET_ENV")
        self.env_now = self.runtime.gateway_environ()
        self.a = self.private(".config/secrets/a.env", b"KIMI_CLAUDE_API_KEY=dummy-key-aaaa\n")
        self.b = self.private(".config/secrets/b.env", b"KIMI_CLAUDE_API_KEY=dummy-key-bbbb\n")
        secret_store.write_pointer(self.env_now, self.a)

    def private(self, relative: str, data: bytes) -> Path:
        target = paths.home(self.runtime.gateway_environ()) / relative
        state.ensure_private_dir(target.parent)
        state.atomic_write(target, data)
        return target

    def test_a_key_file_selected_while_a_set_key_waits_is_never_written(self) -> None:
        with self.human():
            plan = setup_providers.plan_set_key(self.runtime, "kimi")
            secret_store.write_pointer(self.env_now, self.b)
            with self.assertRaises(model.SetupError) as caught:
                setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                              model.Secret("kimi-new-dummy"))
        self.assertIn("nothing", str(caught.exception))
        self.assertEqual(secret_store.read_env_file(self.a), {KIMI: "dummy-key-aaaa"})
        self.assertEqual(secret_store.read_env_file(self.b), {KIMI: "dummy-key-bbbb"})
        self.assertEqual(plan.key_file, ("pointer", str(self.a.resolve())))

    def test_a_key_file_selected_while_a_remove_key_waits_is_never_written(self) -> None:
        with self.human():
            plan = setup_providers.plan_remove_key(self.runtime, "kimi")
            secret_store.write_pointer(self.env_now, self.b)
            with self.assertRaises(model.SetupError):
                setup_providers.apply_remove_key(self.runtime, plan, model.Confirmation.given(plan))
        self.assertEqual(secret_store.read_env_file(self.a), {KIMI: "dummy-key-aaaa"})
        self.assertEqual(secret_store.read_env_file(self.b), {KIMI: "dummy-key-bbbb"})

    def test_the_planned_file_is_checked_again_inside_the_transaction(self) -> None:
        # The shared sample does not see it here; the transaction's own check does.
        with mock.patch.object(providers_cmd, "key_file_identity", return_value=["fixed", "fixed"]):
            plan = setup_providers.plan_set_key(self.runtime, "kimi")
            secret_store.write_pointer(self.env_now, self.b)
            with self.human(), self.assertRaises(model.Stale) as caught:
                setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                              model.Secret("kimi-new-dummy"))
        self.assertEqual(caught.exception.what, texts.KEY_FILE_WHAT)
        self.assertEqual(secret_store.read_env_file(self.b), {KIMI: "dummy-key-bbbb"})

    def test_selecting_a_key_file_is_one_served_change_with_an_exact_undo(self) -> None:
        pointer = paths.secret_pointer_path(self.env_now)
        before = pointer.read_bytes()
        with mock.patch.object(self.runtime, "render_gateway",
                               side_effect=errors.ClaudeMultiError("fixture render refusal")):
            with self.assertRaises(model.Refused):
                setup_providers.apply_key_file(self.runtime, self.b)
        self.assertEqual(pointer.read_bytes(), before)
        applied = setup_providers.apply_key_file(self.runtime, self.b)
        self.assertEqual(applied.reload, "reloaded")
        self.assertEqual(secret_store.secret_env_path(self.env_now), self.b)

    def test_a_selection_that_stops_serving_a_line_with_unknown_live_impact_is_refused(self) -> None:
        # The selected file (A) serves a provider's lines; the new one (B)
        # holds no key, and a session record cannot be read, so whether a
        # running session uses those lines is unknown.
        self.apply_and_serve()
        config = claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml"
        published = config.read_bytes()
        empty = self.private(".config/secrets/empty.env", b"")
        record = Path(self.runtime.session_store.root) / "sessions" / "0b5f3b3e-8f0c-4c3b-9a65-3f0a8c1f2d4e.json"
        state.ensure_private_dir(record.parent)
        state.atomic_write(record, b"{not a record")
        pointer = paths.secret_pointer_path(self.env_now)
        before = pointer.read_bytes()
        reloads = self.runtime.verify_reload.call_count
        code, out, err = self.op(["setup", "--keys-file", str(empty)], "y\n")
        self.assertEqual(code, 1, out + err)
        self.assertIn("Live session impact: unknown", err)
        # Nothing changed: the pointer still selects A and the published
        # configuration still serves what it served.
        self.assertEqual(pointer.read_bytes(), before)
        self.assertEqual(secret_store.secret_env_path(self.env_now), self.a)
        self.assertEqual(config.read_bytes(), published)
        self.assertEqual(self.runtime.verify_reload.call_count, reloads)
        # The same selection is refused at the setup layer before anything is written.
        with self.assertRaises(model.Refused) as caught:
            setup_providers.apply_key_file(self.runtime, empty)
        self.assertIn("Live session impact: unknown", str(caught.exception))
        self.assertEqual(pointer.read_bytes(), before)

    def test_a_selection_is_planned_again_against_the_new_file_under_the_locks(self) -> None:
        # A key added to the new file after the plan changes what it would
        # serve: refused as stale, nothing written.
        self.apply_and_serve()
        target = self.private(".config/secrets/c.env", b"")
        pointer = paths.secret_pointer_path(self.env_now)
        before = pointer.read_bytes()
        real = setup_providers.served_transaction

        def transaction(runtime, **kwargs):
            state.atomic_write(target, b"KIMI_CLAUDE_API_KEY=dummy-key-cccc\n")
            return real(runtime, **kwargs)

        with mock.patch.object(setup_providers, "served_transaction", side_effect=transaction), \
                self.assertRaises(model.SetupError) as caught:
            setup_providers.apply_key_file(self.runtime, target)
        self.assertIn("nothing", str(caught.exception))
        self.assertEqual(pointer.read_bytes(), before)


class TransportKeyTests(CredentialCase):
    def test_a_key_removed_while_the_switch_waits_is_never_switched_to(self) -> None:
        self.store().set(ANTHROPIC_KEY, "anthropic-dummy-key")
        # The unused key is not in the render: removing it changes no gateway file.
        self.apply_and_serve()
        sample = providers_cmd._sample(self.runtime)
        with self.human():
            plan = setup_providers.plan_transport(self.runtime, "anthropic", "api-key")
        self.assertTrue(plan.key_present)
        code, _out, err = self.op(["providers", "remove-key", "anthropic", "--yes"])
        self.assertEqual(code, 0, err)
        with self.human(), self.assertRaises(model.SetupError) as caught:
            setup_providers.apply_transport(self.runtime, plan, model.Confirmation.given(plan))
        self.assertIn("nothing", str(caught.exception))
        self.assertEqual(setup_providers.current_transport(self.runtime, "anthropic"), "oauth-pool")
        # The shared sample covers the alternative's key too.
        self.assertNotEqual(providers_cmd._sample(self.runtime), sample)

    def test_the_switch_itself_checks_the_key_again(self) -> None:
        self.store().set(ANTHROPIC_KEY, "anthropic-dummy-key")
        with mock.patch.object(providers_cmd, "_secret_names", return_value=[]):
            with self.human():
                plan = setup_providers.plan_transport(self.runtime, "anthropic", "api-key")
            self.store().delete(ANTHROPIC_KEY)
            with self.human(), self.assertRaises(model.Stale) as caught:
                setup_providers.apply_transport(self.runtime, plan, model.Confirmation.given(plan))
        self.assertEqual(caught.exception.what, "the saved Anthropic API key")
        self.assertEqual(setup_providers.current_transport(self.runtime, "anthropic"), "oauth-pool")


class SelectedTransportKeyTests(CredentialCase):
    """An approved API-key transport whose key is gone: doctor's fix saves the
    key (``providers set-key``); switching to the transport already in use
    would change nothing."""

    def select_then_lose_the_key(self) -> None:
        key = self.root / "platform.key"
        state.atomic_write(key, b"anthropic-dummy-key\n")
        code, _out, err = self.op(["providers", "transport", "anthropic", "api-key", "--secret-file", str(key)],
                                  "y\n")
        self.assertEqual(code, 0, err)
        self.store().delete(ANTHROPIC_KEY)

    def transport_blocks(self) -> list[str]:
        from claude_multi.cli import doctor

        problems, _info, _attention = doctor._collect_doctor_reports(self.runtime)
        return [line for line in problems if "provider anthropic transport api-key" in line]

    def test_doctor_names_set_key_and_that_fix_saves_the_key(self) -> None:
        self.select_then_lose_the_key()
        blocks = self.transport_blocks()
        self.assertEqual(len(blocks), 1, blocks)
        self.assertIn(f"{ANTHROPIC_KEY} is not set — claude-multi providers set-key anthropic", blocks[0])
        self.assertNotIn("providers transport anthropic api-key", blocks[0])
        # The transport command is a no-op here: it is already the transport in use.
        code, out, err = self.op(["providers", "transport", "anthropic", "api-key"], "y\n")
        self.assertIn("already uses the api-key transport", out + err)
        self.assertFalse(self.store().is_set(ANTHROPIC_KEY))
        # The fix doctor names saves the key, and the block is gone.
        code, out, err = self.op(["providers", "set-key", "anthropic"], "anthropic-new-dummy\n")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.store().get(ANTHROPIC_KEY), "anthropic-new-dummy")
        self.assertEqual(self.transport_blocks(), [])

    def test_the_cards_h_names_k_and_doctor_keeps_the_command(self) -> None:
        """The same finding on both surfaces: the card's H report names the
        card's route to the key (G → K on the provider), the command line's
        doctor the command."""

        from claude_multi import catalog, cli, tui
        from claude_multi.cli.screens import launch_sessions
        from claude_multi.cli import text as cli_text
        from test_tui import ESC, FakeWindow

        self.select_then_lose_the_key()
        command = "claude-multi providers set-key anthropic"
        out = io.StringIO()
        target = cli.LaunchTarget("profile", self.runtime.profiles.load(catalog.DEFAULT_SEED), catalog.DEFAULT_SEED,
                                  True, f"Profile {catalog.DEFAULT_SEED}")
        card = launch_sessions._LaunchCardScreen(
            self.runtime, target, palette=tui.MONO_PALETTE, gateway_checked=True, hint_detector=lambda _c: None,
            tty_in=io.StringIO("\n\n\n"), tty_out=out, passthrough=[])
        self.assertIsNone(card.run(FakeWindow(["h", ESC], height=24, width=80)))
        report = out.getvalue()
        block = next(line for line in report.splitlines() if "provider anthropic transport api-key" in line)
        self.assertNotIn("providers set-key", report)
        route = cli_text.CARD_SET_KEY.format(route=cli_text.CARD_SET_KEY_FRESH.format(display="Anthropic"))
        self.assertIn(f"{ANTHROPIC_KEY} is not set — {route};", block)
        # The command line keeps the command.
        _code, out_text, err = self.op(["doctor"])
        self.assertIn(f"{ANTHROPIC_KEY} is not set — {command};", out_text + err)
        self.assertNotIn(cli_text.CARD_SET_KEY_FRESH.format(display="Anthropic"), out_text + err)


class SetKeyCommandTests(CredentialCase):
    def test_replacing_asks_first_and_no_keeps_it_with_exit_3(self) -> None:
        code, out, err = self.op(["providers", "set-key", "kimi"], "n\n")
        self.assertEqual(code, 3, err)
        self.assertIn("Kimi API key kept — nothing changed", out)
        self.assertEqual(self.store().get(KIMI), "cli-test-dummy")
        code, out, err = self.op(["providers", "set-key", "kimi"], "y\nkimi-new-dummy\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.store().get(KIMI), "kimi-new-dummy")
        self.assertNotIn("kimi-new-dummy", out + err)

    def test_outside_a_terminal_and_inside_a_session_refuse(self) -> None:
        code, _out, err = self.op(["providers", "set-key", "kimi"], "y\nv-dummy\n", tty=False)
        self.assertEqual(code, 1)
        self.assertIn("not a terminal", err)
        code, _out, err = self.op(["providers", "set-key", "kimi"], "y\nv-dummy\n",
                                  env={"CLAUDE_MULTI_MANAGED_ID": "x"})
        self.assertEqual(code, 1)
        self.assertEqual(self.store().get(KIMI), "cli-test-dummy")

    def test_anthropic_names_the_transport_command(self) -> None:
        code, _out, err = self.op(["providers", "set-key", "anthropic"])
        self.assertEqual(code, 1)
        self.assertIn("claude-multi providers transport anthropic api-key", err)


class RemoveKeyTests(CredentialCase):
    def test_remove_key_shows_the_plan_and_no_keeps_it(self) -> None:
        code, out, err = self.op(["providers", "remove-key", "kimi"], "n\n")
        self.assertEqual(code, 3, err)
        self.assertIn(f"Deletes {KIMI} from {self.secret_file}; the gateway reloads without it.", err)
        self.assertIn("If the key may have leaked, also revoke it in your Kimi account.", err)
        self.assertEqual(self.store().get(KIMI), "cli-test-dummy")
        code, out, err = self.op(["providers", "remove-key", "kimi"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("Kimi API key removed — the gateway reloaded", out)
        self.assertIsNone(self.store().get(KIMI))
        code, out, err = self.op(["providers", "remove-key", "kimi", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("Kimi has no saved API key", err)

    def test_the_anthropic_key_in_use_is_refused(self) -> None:
        name = operator_mod.transport_alternative("anthropic", operator_mod.TRANSPORT_API_KEY).secret_name
        self.store().set(name, "anthropic-dummy")
        ctx = mock.Mock()
        ctx.docs = self.runtime.catalog.docs
        ctx.ledger.transport_choices = {"anthropic": operator_mod.TRANSPORT_API_KEY}
        with self.assertRaises(model.Refused) as caught:
            setup_providers._key_target(self.runtime, "anthropic", ctx)
        self.assertEqual(caught.exception.line_text, texts.KEY_ANTHROPIC_IN_USE_LINE)
        # Not selected: the saved Anthropic key can be removed.
        code, out, err = self.op(["providers", "remove-key", "anthropic", "--yes"])
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.store().get(name))

    def test_a_refused_render_puts_the_key_back(self) -> None:
        with self.human():
            plan = setup_providers.plan_remove_key(self.runtime, "kimi")
            with mock.patch.object(self.runtime, "render_gateway",
                                   side_effect=errors.ClaudeMultiError("fixture render refusal")):
                with self.assertRaises(model.Refused):
                    setup_providers.apply_remove_key(self.runtime, plan, model.Confirmation.given(plan))
        self.assertEqual(self.store().get(KIMI), "cli-test-dummy")


class ReloadExitTests(CredentialCase):
    def test_a_key_the_gateway_rejects_at_the_reload_exits_1(self) -> None:
        self.runtime.verify_reload.side_effect = lambda sentinel: claude_multi.proxy.ReloadResult(
            "token_mismatch", "gateway: token mismatch (fixture)")
        code, out, err = self.op(["providers", "set-key", "kimi"], "y\nkimi-new-dummy\n")
        self.assertEqual(code, 1, err)
        self.assertIn(texts.RELOAD_TEXT["token_mismatch"], out)
        code, out, err = self.op(["providers", "remove-key", "kimi", "--yes"])
        self.assertEqual(code, 1, err)
        self.assertIn(texts.RELOAD_TEXT["token_mismatch"], out)
        self.runtime.verify_reload.side_effect = lambda sentinel: claude_multi.proxy.ReloadResult(
            "reloaded", f"gateway: reloaded (sentinel {sentinel})")
        code, _out, err = self.op(["providers", "set-key", "kimi"], "kimi-new-dummy\n")
        self.assertEqual(code, 0, err)


class ToggleTests(CredentialCase):
    def test_disable_then_enable_a_shipped_provider(self) -> None:
        code, out, err = self.op(["providers", "disable", "kimi"])
        self.assertEqual(code, 0, err)
        self.assertIn("kimi disabled — applies at the next launch or resume", out)
        code, out, err = self.op(["providers", "disable", "kimi"])
        self.assertEqual((code, out.strip()), (0, "kimi is already disabled"))
        code, out, err = self.op(["providers", "enable", "kimi"])
        self.assertEqual(code, 0, err)
        self.assertIn("kimi enabled", out)
        code, _out, err = self.op(["providers", "disable", "nobody"])
        self.assertEqual(code, 1)
        self.assertIn("nobody is not a provider here", err)


class RemoveProviderTests(CredentialCase):
    def test_shipped_providers_are_not_removable(self) -> None:
        code, _out, err = self.op(["providers", "rm", "kimi", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn(texts.NOT_YOUR_PROVIDER.format(id="kimi"), err)

    def test_rm_asks_then_offers_the_key(self) -> None:
        code, _out, err = self.op(test_cli.ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        code, out, err = self.op(["providers", "rm", "acme"], "n\n")
        self.assertEqual(code, 3, err)
        self.assertTrue((self.pdir / "acme.json").exists())
        code, out, err = self.op(["providers", "rm", "acme"], "y\nn\n")
        self.assertEqual(code, 0, err)
        self.assertFalse((self.pdir / "acme.json").exists())
        self.assertIn("Removed acme", out)
        self.assertIn("Also remove its API key ACME_API_KEY? [y/N]", err)
        self.assertEqual(self.store().get("ACME_API_KEY"), "acme-dummy-value")

    def test_rm_yes_keeps_the_key_and_names_the_command(self) -> None:
        code, _out, err = self.op(test_cli.ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        code, out, err = self.op(["providers", "rm", "acme", "--yes"])
        self.assertEqual(code, 0, err)
        self.assertIn(texts.KEY_KEPT_AFTER_RM.format(name="ACME_API_KEY", id="acme"), out)
        self.assertEqual(self.store().get("ACME_API_KEY"), "acme-dummy-value")

    def test_the_printed_command_removes_the_kept_key(self) -> None:
        code, _out, err = self.op(test_cli.ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        code, out, err = self.op(["providers", "rm", "acme", "--yes"])
        self.assertEqual(code, 0, err)
        line = next(line for line in out.splitlines() if "was kept" in line)
        command = shlex.split(line.split(" — ", 1)[1])
        self.assertEqual(command[:3], ["claude-multi", "providers", "remove-key"])
        code, out, err = self.op([*command[1:], "--yes"])
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.store().get("ACME_API_KEY"))
        self.assertEqual(self.store().get(KIMI), "cli-test-dummy")

    def test_a_named_key_is_never_another_providers(self) -> None:
        code, _out, err = self.op(["providers", "remove-key", "gone", "--name", KIMI, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn(texts.KEY_NAME_IN_USE.format(name=KIMI, other="kimi"), err)
        code, _out, err = self.op(["providers", "remove-key", "kimi", "--name", "ACME_API_KEY", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn(texts.KEY_NAME_NOT_ITS.format(id="kimi", key=KIMI, name="ACME_API_KEY"), err)
        code, _out, err = self.op(["providers", "remove-key", "gone", "--name", "not a name", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn(texts.KEY_NAME_SHAPE, err)
        self.assertEqual(self.store().get(KIMI), "cli-test-dummy")
        self.assertEqual(self.store().get("ACME_API_KEY"), "acme-dummy-value")

    def test_blockers_name_every_fix(self) -> None:
        self.declare_small()
        self.serve_current()
        code, _out, err = self.op(["models", "admit", "custom-acme-small"], "y\n")
        self.assertEqual(code, 0, err)
        plan = setup_providers.plan_remove_provider(self.runtime, "acme")
        self.assertEqual([(b.kind, b.subject) for b in plan.blockers], [("admitted", "custom-acme-small")])
        self.assertEqual(plan.blockers[0].fix_cli, "claude-multi models revoke custom-acme-small")
        with self.assertRaises(model.Refused):
            setup_providers.apply_remove_provider(self.runtime, plan, model.Confirmation.given(plan))
        code, _out, err = self.op(["providers", "rm", "acme", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("refused — claude-multi models revoke custom-acme-small", err)


class ConnectionTestTests(CredentialCase):
    def test_one_consent_names_every_request_and_no_retries(self) -> None:
        self.apply_and_serve()
        self.outcome = operator_mod.SmokeOutcome("fail", 401, "unauthorized")
        code, out, err = self.op(["providers", "test", "kimi"], "n\n")
        self.assertEqual(code, 3)
        self.assertEqual(self.calls, [])
        self.assertIn("claude-multi will make 1 request(s), one per provider:", err)
        self.assertIn(f"auth: header from {KIMI} (value not shown)", err)
        self.assertIn("Providers may bill these requests.", err)
        code, out, err = self.op(["providers", "test", "kimi"], "y\n")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Kimi: the key was refused — K replaces it", out)
        self.outcome = operator_mod.SmokeOutcome("pass", 200, "ok")
        code, out, err = self.op(["providers", "test", "kimi"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"Kimi: works \(\d+ ms\)")
        self.assertEqual(len(self.calls), 2)

    def test_a_consent_for_the_account_never_sends_through_a_key_switched_to_meanwhile(self) -> None:
        from claude_multi.setup import check

        self.apply_and_serve()
        plan = check.plan_test(self.runtime, ["anthropic"])
        self.assertTrue(plan.targets[0].account)
        with self.human():
            switch = setup_providers.plan_transport(self.runtime, "anthropic", "api-key")
            setup_providers.apply_transport(self.runtime, switch, model.Confirmation.given(switch),
                                            model.Secret("anthropic-dummy-key"))
        self.serve_current()  # the gateway serves the new setup: only the route moved
        with self.human(), self.assertRaises(model.Refused) as caught:
            check.run_test(self.runtime, plan, model.Confirmation.given(plan))
        self.assertEqual(self.calls, [])
        self.assertEqual(str(caught.exception), texts.TEST_STALE.format(display=plan.targets[0].display))

    def test_a_change_between_two_requests_stops_the_rest(self) -> None:
        from claude_multi.setup import check

        self.apply_and_serve()
        plan = check.plan_test(self.runtime, ["kimi", "anthropic"])
        self.assertEqual([t.provider_id for t in plan.targets], ["kimi", "anthropic"])
        alternative = operator_mod.transport_alternative("anthropic", operator_mod.TRANSPORT_API_KEY)

        def switch_anthropic() -> None:
            def select(document):
                document["transport_choices"]["anthropic"] = operator_mod.TRANSPORT_API_KEY
                document["routes"]["anthropic"] = operator_mod.transport_route_record(
                    alternative, at=operator_mod.utc_stamp())

            operator_mod.update_ledger(self.env, operator_mod.load_schemas(self.runtime.asset_root), select)
            self.during_smoke = None

        self.during_smoke = switch_anthropic
        seen = []
        with self.human(), self.assertRaises(model.Refused):
            check.run_test(self.runtime, plan, model.Confirmation.given(plan), on_result=seen.append)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([result.provider_id for result in seen], ["kimi"])

    def test_a_gateway_not_serving_the_current_setup_sends_nothing(self) -> None:
        code, _out, err = self.op(["providers", "test", "kimi"], "y\n")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])
        self.assertIn("Apply it: claude-multi providers apply", err)

    def test_outcomes_are_classified(self) -> None:
        from claude_multi.setup import check

        target = check.TestTarget("kimi", "Kimi", "x", "https://api.kimi.com", "auth")
        account = check.TestTarget("anthropic", "Anthropic", "x", "claude.ai", "auth", True)
        cases = [
            (target, operator_mod.SmokeOutcome("fail", 402, "x"), "402"),
            (target, operator_mod.SmokeOutcome("fail", 404, "x"), "404"),
            (target, operator_mod.SmokeOutcome("fail", 429, "x"), "429"),
            (target, operator_mod.SmokeOutcome("fail", 503, "x"), "5xx"),
            (target, operator_mod.SmokeOutcome("fail", None, "wall-clock cap"), "timeout"),
            (target, operator_mod.SmokeOutcome("fail", None, "connection refused"), "down"),
            (account, operator_mod.SmokeOutcome("fail", 401, "x"), "account-refused"),
            (target, operator_mod.SmokeOutcome("fail", 418, "teapot"), "other"),
        ]
        for item, outcome, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(check.classify(item, outcome, 5).outcome, expected)

    def test_nothing_served_is_excluded_and_needs_no_consent(self) -> None:
        self.served = set()
        code, out, err = self.op(["providers", "test", "kimi"], "y\n")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])
        self.assertIn("Kimi: no served model to test", out)
        self.assertIn(texts.TEST_NOTHING, err)


if __name__ == "__main__":
    import unittest

    unittest.main()
