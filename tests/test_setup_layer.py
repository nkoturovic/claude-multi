"""The setup layer below every surface: connections and the setup steps,
the provider picker, your own endpoints and servers on your network, the
Anthropic transport, removing a model line, and what moves to another
computer (names only). Fixture gateway; no provider request."""

from __future__ import annotations

import json
from unittest import mock

import test_cli
from _catalog import SHIPPED_ROOT, uses_shipped_catalog
from claude_multi import operator as operator_mod, secret_store, sessions
from claude_multi.setup import lines as setup_lines, model, providers as setup_providers, signin, status, texts
import claude_multi.cli.consent as consent


class LayerCase(test_cli.OperatorCommandCase):
    def human(self):
        return mock.patch.object(consent, "stdio_ttys", return_value=True)

    def store(self) -> secret_store.FileSecretStore:
        return secret_store.default_store(self.runtime.gateway_environ())

    def record(self, name: str) -> None:
        directory = signin.auth_dir(self.runtime)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (directory / name).write_bytes(b"{}")


class ConnectionTests(LayerCase):
    def test_states_come_from_real_state(self) -> None:
        found = {item.provider_id: item for item in status.connections(self.runtime)}
        self.assertEqual((found["kimi"].state, found["kimi"].credential, found["kimi"].label),
                         ("connected", "KIMI_CLAUDE_API_KEY", "Kimi (API key)"))
        self.assertEqual(found["deepseek"].state, "key-missing")
        self.assertIn(found["anthropic"].state, ("not-signed-in", "sign-in-unknown"))
        self.assertEqual((found["anthropic"].kind, found["anthropic"].transport), ("account", "account"))
        self.assertEqual(found["anthropic"].label, "Claude account")
        self.record("claude-a@example.com.json")
        found = {item.provider_id: item for item in status.connections(self.runtime)}
        self.assertEqual(found["anthropic"].accounts, ("a@example.com",))
        code, _out, err = self.op(["providers", "disable", "kimi"])
        self.assertEqual(code, 0, err)
        found = {item.provider_id: item for item in status.connections(self.runtime)}
        self.assertEqual(found["kimi"].state, "off")

    def test_a_snapshot_never_writes(self) -> None:
        before = sorted(str(path) for path in self.root.rglob("*"))
        status.snapshot(self.runtime)
        self.assertEqual(sorted(str(path) for path in self.root.rglob("*")), before)


class PickerTests(LayerCase):
    def test_order_groups_and_closed_entries(self) -> None:
        entries = setup_providers.picker_entries(self.runtime)
        ids = [entry.id for entry in entries]
        self.assertEqual(ids[:4], ["anthropic:account", "anthropic:api-key", "openai:account", "openai:api-key"])
        self.assertEqual(ids[4], "openrouter:api-key")
        self.assertEqual(entries[-1].group, "other")
        by_id = {entry.id: entry for entry in entries}
        # Offered by this build; the fixture reviews no OpenAI line for it.
        self.assertFalse(by_id["openai:api-key"].available)
        self.assertEqual(by_id["openai:api-key"].note, texts.KEY_ROUTE_NO_MODELS_NOTE)
        self.assertEqual(by_id["anthropic:account"].label, texts.PICKER_LABELS["anthropic:account"])
        self.assertNotIn("llm-local:api-key", by_id)  # a keyless shipped server needs no connection
        self.assertEqual(by_id["kimi:api-key"].state_text, texts.STATE_TEXT["key-set"])
        only = setup_providers.picker_entries(self.runtime, only="other")
        self.assertTrue(only and all(entry.group == "other" for entry in only))

    def test_a_strict_build_closes_the_claude_account(self) -> None:
        with mock.patch.object(signin, "signin_policy", return_value="public-strict"):
            entry = next(e for e in setup_providers.picker_entries(self.runtime) if e.id == "anthropic:account")
        self.assertFalse(entry.available)
        self.assertEqual(entry.note, texts.SIGNIN_CLOSED_NOTE)


class EndpointTests(LayerCase):
    VALUES = {"id": "vendorx", "kind": "anthropic-compatible", "url": "https://api.vendorx.example/anthropic",
              "auth": "header", "family": "vendorx", "listing": ""}

    def test_preview_approval_and_key_in_one_transaction(self) -> None:
        with self.human():
            plan = setup_providers.plan_add_endpoint(self.runtime, self.VALUES)
            self.assertEqual(plan.secret_name, "VENDORX_API_KEY")
            self.assertEqual(plan.consent, "approve-route")
            self.assertIn("The key VENDORX_API_KEY is sent as header x-api-key only to "
                          "https://api.vendorx.example.", "\n".join(plan.lines))
            applied = setup_providers.apply_add_endpoint(self.runtime, plan, model.Confirmation.given(plan),
                                                         model.Secret("vendorx-dummy-key"))
        self.assertTrue(applied.lines[0].startswith("Added vendorx — the gateway reloaded"), applied.lines)
        self.assertTrue((self.pdir / "vendorx.json").exists())
        self.assertEqual(self.ledger().routes["vendorx"]["origin"], "https://api.vendorx.example")
        self.assertEqual(self.store().get("VENDORX_API_KEY"), "vendorx-dummy-key")
        with self.assertRaisesRegex(model.Refused, "exists — choose another name"):
            setup_providers.plan_add_endpoint(self.runtime, self.VALUES)
        with self.assertRaisesRegex(model.Refused, "ships with claude-multi"):
            setup_providers.plan_add_endpoint(self.runtime, {**self.VALUES, "id": "kimi"})

    def test_a_refused_render_leaves_nothing_behind(self) -> None:
        with self.human():
            plan = setup_providers.plan_add_endpoint(self.runtime, self.VALUES)
            with mock.patch.object(self.runtime, "render_gateway",
                                   side_effect=setup_providers.errors.ClaudeMultiError("fixture refusal")):
                with self.assertRaises(model.Refused):
                    setup_providers.apply_add_endpoint(self.runtime, plan, model.Confirmation.given(plan),
                                                       model.Secret("vendorx-dummy-key"))
        self.assertFalse((self.pdir / "vendorx.json").exists())
        self.assertNotIn("vendorx", self.ledger().routes)
        self.assertIsNone(self.store().get("VENDORX_API_KEY"))

    def test_derived_names_avoid_reserved_prefixes(self) -> None:
        self.assertEqual(setup_providers.derived_secret_name("my-vendor"), "MY_VENDOR_API_KEY")
        self.assertEqual(setup_providers.derived_secret_name("claude-proxy"), "USER_CLAUDE_PROXY_API_KEY")

    def test_the_closed_openai_compatible_kind(self) -> None:
        from claude_multi import catalog

        with mock.patch.object(catalog, "keyed_compat_audited", return_value=False):
            with self.assertRaisesRegex(model.Refused, "not available in this release"):
                setup_providers.plan_add_endpoint(self.runtime, {**self.VALUES, "kind": "openai-compatible"})
            entry = next(e for e in setup_providers.picker_entries(self.runtime) if e.id == "other:openai-compatible")
        self.assertFalse(entry.available)


@uses_shipped_catalog
class LanServerTests(LayerCase):
    """The reviewed server presets ship as examples (the fixture root has none)."""

    def test_a_server_on_your_network(self) -> None:
        shipped = operator_mod.load_preset
        with mock.patch.object(operator_mod, "load_preset", lambda name, _root: shipped(name, SHIPPED_ROOT)):
            plan = setup_providers.plan_add_preset(self.runtime, "llm-local", "homelab",
                                                   "http://192.168.1.20:8000/v1")
        self.assertEqual(plan.lines, (texts.LAN_PREVIEW.format(id="homelab", base_url="http://192.168.1.20:8000/v1"),))
        self.assertFalse(plan.guarded)
        applied = setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan))
        self.assertTrue(applied.lines[0].startswith("Added homelab"))
        document = json.loads((self.pdir / "homelab.json").read_text())
        self.assertEqual(document["provider"]["base_url"], "http://192.168.1.20:8000/v1")
        self.assertEqual(document["provider"]["auth"], {"kind": "none"})


class TransportTests(LayerCase):
    def test_switch_to_the_api_key_and_back(self) -> None:
        alternative = operator_mod.transport_alternative("anthropic", operator_mod.TRANSPORT_API_KEY)
        with self.human():
            plan = setup_providers.plan_transport(self.runtime, "anthropic", "api-key")
            self.assertEqual((plan.consent, plan.key_present, plan.secret_name),
                             ("approve-route", False, alternative.secret_name))
            self.assertIn(f"move to {alternative.origin} — same names, never both at once.", plan.lines[0])
            with self.assertRaisesRegex(model.Refused, texts.KEY_EMPTY):
                setup_providers.apply_transport(self.runtime, plan, model.Confirmation.given(plan))
            applied = setup_providers.apply_transport(self.runtime, plan, model.Confirmation.given(plan),
                                                      model.Secret("anthropic-dummy-key"))
            self.assertTrue(applied.lines[0].startswith("Claude models now use your Anthropic API key"))
            self.assertEqual(setup_providers.current_transport(self.runtime, "anthropic"), "api-key")
            with self.assertRaisesRegex(model.Refused, "already uses the api-key transport"):
                setup_providers.plan_transport(self.runtime, "anthropic", "api-key")
            with self.assertRaises(model.Refused) as caught:
                setup_providers.plan_remove_key(self.runtime, "anthropic")
            self.assertEqual(caught.exception.line_text, texts.KEY_ANTHROPIC_IN_USE_LINE)
            back = setup_providers.plan_transport(self.runtime, "anthropic", "oauth-pool")
            setup_providers.apply_transport(self.runtime, back, model.Confirmation.given(back))
        self.assertEqual(setup_providers.current_transport(self.runtime, "anthropic"), "oauth-pool")
        self.assertEqual(self.store().get(alternative.secret_name), "anthropic-dummy-key")

    def test_the_summary_count_and_before_value_match_the_listed_rows(self) -> None:
        self.apply_and_serve()
        from claude_multi.cli.commands import providers as provider_commands

        # The provider's selector bases undercount what moves (the gateway
        # also serves claude-multi-* aliases of the same lines): the summary
        # follows the listed rows, never the bases.
        with self.human(), mock.patch.object(provider_commands, "_transport_selectors",
                                             return_value=frozenset({"claude-opus-5-5"})):
            plan = setup_providers.plan_transport(self.runtime, "anthropic", "api-key")
        served = plan.served.plan
        self.assertIsNone(served.before_unknown)
        moving = len(served.retargeted)
        self.assertGreater(moving, 1)
        self.assertTrue(plan.lines[0].startswith(f"{moving} model selector(s) move to "), plan.lines[0])
        authority = {key: (old, new) for key, old, new in served.authority}
        self.assertEqual(authority["transport anthropic"], ("oauth-pool", "api-key"))
        self.assertIn("  transport anthropic: oauth-pool → api-key", served.lines())

    def test_a_stale_transport_plan_writes_nothing(self) -> None:
        with self.human():
            plan = setup_providers.plan_transport(self.runtime, "anthropic", "api-key")
            first = setup_providers.plan_transport(self.runtime, "anthropic", "api-key")
            setup_providers.apply_transport(self.runtime, first, model.Confirmation.given(first),
                                            model.Secret("anthropic-dummy-key"))
            with self.assertRaises(model.SetupError) as caught:
                setup_providers.apply_transport(self.runtime, plan, model.Confirmation.given(plan),
                                                model.Secret("other-dummy-key"))
        self.assertIn("nothing", str(caught.exception))
        name = operator_mod.transport_alternative("anthropic", operator_mod.TRANSPORT_API_KEY).secret_name
        self.assertEqual(self.store().get(name), "anthropic-dummy-key")


class LineRemoveTests(LayerCase):
    def setUp(self) -> None:
        super().setUp()
        self.declare_small()
        self.serve_current()

    def test_an_unused_line_is_removed(self) -> None:
        plan = setup_lines.plan_line_remove(self.runtime, "custom-acme-small")
        self.assertEqual((plan.references, plan.live_users, plan.consent), ((), (), "destructive"))
        self.assertEqual(plan.lines, (texts.REMOVE_LINE_BODY.format(file="providers.d/acme.json"),))
        applied = setup_lines.apply_line_remove(self.runtime, plan, model.Confirmation.given(plan))
        self.assertTrue(applied.lines[0].startswith("Removed custom-acme-small — "), applied.lines)
        self.assertIn("custom-acme-small", self.ledger().removed)
        self.assertNotIn("custom-acme-small", json.loads((self.pdir / "acme.json").read_text())["lines"])

    def test_shipped_and_unknown_lines(self) -> None:
        shipped = next(iter(self.runtime.catalog.docs["models-v2"]["models"]))
        with self.assertRaisesRegex(model.Refused, "ships with claude-multi"):
            setup_lines.plan_line_remove(self.runtime, shipped)
        with self.assertRaisesRegex(model.Refused, "no providers.d declaration of nothing-here"):
            setup_lines.plan_line_remove(self.runtime, "nothing-here")

    def test_the_live_refusal_names_the_end_of_session_fix(self) -> None:
        users = ["0123456789abcdef-1", "fedcba9876543210-2"]
        text = setup_lines.live_refusal(users)
        self.assertIn("live sessions use it (01234567, fedcba98)", text)
        self.assertIn(sessions.mark_ended_remedy(users), text)
        self.assertIn("nothing changed", setup_lines.live_refusal(users, changed=True))

    def test_a_confirmation_for_another_plan_is_refused(self) -> None:
        plan = setup_lines.plan_line_remove(self.runtime, "custom-acme-small")
        wrong = model.Confirmation("sha256:" + "0" * 64, plan.consent, "2026-10-03T00:00:00Z")
        with self.assertRaisesRegex(model.Refused, model.CONFIRMATION_MISMATCH):
            setup_lines.apply_line_remove(self.runtime, plan, wrong)
        self.assertIn("custom-acme-small", json.loads((self.pdir / "acme.json").read_text())["lines"])


class ReconnectTests(LayerCase):
    def test_names_only_and_the_key_file_is_never_read(self) -> None:
        code, _out, err = self.op(test_cli.ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        self.record("codex-a@example.com.json")
        with mock.patch.object(secret_store, "default_store", side_effect=AssertionError("store read")), \
                mock.patch.object(secret_store, "read_env_file", side_effect=AssertionError("key file read")):
            facts = setup_providers.reconnect_facts(self.runtime)
        self.assertEqual(facts["sign_ins"], ["openai"])
        self.assertIn("ACME_API_KEY", facts["api_keys"])
        self.assertNotIn("acme-dummy-value", json.dumps(facts))

    def test_secret_values_never_print(self) -> None:
        secret = model.Secret("never-shown-dummy")
        self.assertEqual(str(secret), "Secret(17 chars)")
        self.assertNotIn("never-shown", repr(secret))
        with self.assertRaises(TypeError):
            __import__("pickle").dumps(secret)


if __name__ == "__main__":
    import unittest

    unittest.main()


class GatewayStepTests(LayerCase):
    """The gateway step (bare setup and Get started) starts the gateway
    explicitly, choosing a new home's port first; a home with no recorded
    port shows the step as not set up and is never observed."""

    def live(self) -> mock.Mock:
        from claude_multi.setup import external

        gateway = mock.Mock()
        for patcher in (mock.patch.object(external, "_fixture_gateway", return_value=False),
                        mock.patch.object(self.runtime, "gateway", return_value=gateway),
                        mock.patch.object(self.runtime, "gateway_ensure", None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return gateway

    def test_the_step_starts_explicitly_and_chooses_the_port(self) -> None:
        from claude_multi.setup import external

        gateway = self.live()
        gateway.ensure.return_value = mock.Mock(ok=True)
        self.assertIs(external.ensure_gateway(self.runtime), gateway.ensure.return_value)
        gateway.ensure.assert_called_once_with(explicit=True, choose_port=True)

    def test_a_home_with_no_recorded_port_is_not_set_up_and_never_observed(self) -> None:
        from claude_multi import endpoint
        from claude_multi.setup import external, firstrun

        gateway = self.live()
        gateway.inhibition_refusal.return_value = None
        gateway.persistence_hold.return_value = mock.Mock(held=False)
        with mock.patch.object(endpoint, "set_up", return_value=False):
            found = external.gateway_status(self.runtime)
            self.assertEqual((found.state, found.detail, found.fix), ("setup", "not set up yet", external.GATEWAY_FIX))
            check = {item.id: item for item in firstrun.checks(self.runtime)}["gateway"]
            self.assertEqual((check.state, check.detail, check.fix.cli),
                             ("fail", "not set up yet", "claude-multi setup --step gateway"))
            row = status.status_lines(status.snapshot(self.runtime))[2]
            self.assertIn("not set up yet — claude-multi setup --step gateway", row)
            outcome = external.stop_gateway_for_uninstall(self.runtime)
        self.assertEqual(outcome, ("not-running", "not set up yet"))
        gateway.observe.assert_not_called()
