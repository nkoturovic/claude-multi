"""OpenRouter public/account/stealth discovery, entirely fixture-driven."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from claude_multi import discovery, proxy, state, secret_store, tui
from claude_multi.cli import consent, onboarding
from claude_multi.cli.commands import discover
from claude_multi.cli.screens import common
from _layout import FIXTURES_ROOT
from _golden import assertGolden
from _catalog import GOLDENS_ROOT
from test_discovery import DiscoveryCLICase

PUBLIC = discovery.OPENROUTER_MODELS_URL
ACCOUNT = PUBLIC + "/user"
WIRE = "stealth/space-bunny-alpha"
MODEL = PUBLIC + "/" + WIRE + "/endpoints"
KEY = "openrouter-fixture-dummy"


def body(name):
    return (FIXTURES_ROOT / "discovery" / f"openrouter-{name}.json").read_bytes()


def entries(name):
    return proxy.parse_listing(body(name), "openrouter-model" if name == "model" else "openrouter").entries


def listing_golden():
    rows = discovery.merge_listings(entries("public"), entries("account"))
    return "\n".join(discovery.entry_text(discovery.MarkedEntry(row, "candidate")) for row in rows) + "\n"


class ParserTests(unittest.TestCase):
    def test_every_regular_public_row_survives_unchanged(self):
        # Compare with the old OpenAI-shape parser: added facts/marks only.
        previous = proxy.parse_listing(body("public"), "openai").entries
        public = entries("public")
        merged = discovery.merge_listings(public, entries("account"))
        self.assertEqual(tuple(merged[:len(public)]), public)
        self.assertEqual([row["id"] for row in public], [row["id"] for row in previous])
        for old, new in zip(previous, public):
            self.assertEqual({key: new[key] for key in old}, old)
        self.assertEqual(len(merged), len(public) + 2)
        self.assertTrue(all(row["account_only"] for row in merged[len(public):]))

    def test_marks_facts_and_variant_warning(self):
        rows = discovery.merge_listings(entries("public"), entries("account"))
        text = {row["id"]: discovery.entry_text(discovery.MarkedEntry(row, "candidate")) for row in rows}
        self.assertIn("[router]", text["openrouter/auto"])
        self.assertNotIn("stealth:", text["openrouter/auto"])
        self.assertIn(discovery.STEALTH_CAUTION, text[WIRE])
        self.assertIn("family=unknown", text[WIRE])
        self.assertIn("[account-only]", text[WIRE])
        self.assertIn("price=free", text[WIRE])
        self.assertIn("tools=yes", text[WIRE])
        self.assertIn("context=262144", text[WIRE])
        self.assertIn("not addable in this release", text["vendor/free:free"])
        self.assertIn("tools=no", text["vendor/free:free"])
        self.assertNotIn("price=free", text["vendor/bare"])
        self.assertIn("context=unknown", text["vendor/malformed"])
        self.assertIn("tools=unknown", text["vendor/malformed"])

    def test_malformed_and_missing_facts_are_unknown_not_hidden(self):
        for facts in ({}, {"pricing": []}, {"context_length": -4},
                      {"pricing": {"prompt": True, "completion": "Infinity"}},
                      {"pricing": {"prompt": "garbage", "completion": "1" * 65}},
                      {"supported_parameters": ["tools", None]},
                      {"architecture": {"modality": "text\n->text"}}):
            with self.subTest(facts=facts):
                result = proxy.parse_listing(json.dumps({"data": [{"id": WIRE, **facts}]}).encode(), "openrouter")
                self.assertEqual(len(result.entries), 1)
                row = result.entries[0]
                self.assertIsNone(row["context_length"])
                self.assertEqual(row["pricing"], {})
                self.assertIsNone(row["tools"])
                self.assertIsNone(row["modality"])

    def test_per_model_shape_is_minimized_and_never_complete_inventory(self):
        result = proxy.parse_listing(body("model"), "openrouter-model")
        self.assertFalse(result.complete)
        self.assertEqual(result.entries[0]["display_name"], "Space Bunny Alpha")
        self.assertEqual(result.entries[0]["modality"], "text+image+video->text")
        self.assertNotIn("endpoints", result.entries[0])
        for raw in (b'{}', b'{"data": []}', b'{"data": {}}', b'{"data": null}'):
            with self.assertRaises(proxy.ListingShapeError):
                proxy.parse_listing(raw, "openrouter-model")

    def test_disagreeing_or_missing_endpoint_facts_stay_unknown(self):
        for endpoints in (
                [{"context_length": 200000, "supported_parameters": ["tools"]},
                 {"context_length": 300000, "supported_parameters": []}],
                [{"context_length": 200000, "supported_parameters": ["tools"]}, {}],
                [{"context_length": True, "supported_parameters": "tools"}],
                [None], []):
            with self.subTest(endpoints=endpoints):
                raw = json.dumps({"data": {"id": WIRE, "endpoints": endpoints}}).encode()
                row = proxy.parse_listing(raw, "openrouter-model").entries[0]
                self.assertIsNone(row["context_length"])
                self.assertIsNone(row["tools"])
                self.assertEqual(row["pricing"], {})
        row = proxy.parse_listing(json.dumps({"data": {"id": WIRE, "endpoints": [
            {"context_length": 200000, "supported_parameters": ["tools"],
             "pricing": {"prompt": "0", "completion": "0"}},
            {"context_length": 200000, "supported_parameters": ["tools", "temperature"],
             "pricing": {"prompt": "0.0", "completion": "0"}}
        ]}}).encode(), "openrouter-model").entries[0]
        self.assertEqual(row["context_length"], 200000)
        self.assertIs(row["tools"], True)
        self.assertEqual(row["pricing"], {"prompt": "0", "completion": "0"})

    def test_wire_validation_never_turns_an_id_into_a_path_or_query(self):
        self.assertTrue(discovery.stealth_id(WIRE))
        for wire in (WIRE + ":free", "stealth/../secret", "stealth/x?key=y", "stealth/x#fragment", "stealth/"):
            self.assertFalse(discovery.stealth_id(wire))

    def test_listing_golden(self):
        assertGolden(self, GOLDENS_ROOT / "discovery/openrouter-listing.txt", listing_golden().encode())


class OpenRouterTests(DiscoveryCLICase):
    def setUp(self):
        super().setUp()
        self.bodies.update({PUBLIC: body("public"), ACCOUNT: body("account"), MODEL: body("model")})

    def set_key(self):
        state.atomic_write(self.secret_file, f"OPENROUTER_CLAUDE_API_KEY={KEY}\n".encode())

    def test_no_key_fetches_public_only_without_mutation(self):
        before = self.state_bytes()
        code, out, err = self.op(["discover", "openrouter"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent, [(PUBLIC, {})])
        self.assertIn("public listing only (no key configured)", out)
        self.assertIn("vendor/bare\tcandidate", out)
        self.assertEqual(self.state_bytes(), before)

    def test_malformed_account_store_preserves_public_rows_without_mutation(self):
        state.atomic_write(self.secret_file, f"OPENROUTER_CLAUDE_API_KEY={KEY}\nmalformed {KEY}\n".encode())
        before = self.state_bytes()
        code, out, err = self.op(["discover", "openrouter"], "y\ny\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent, [(PUBLIC, {})])
        self.assertEqual(err.count("Proceed with these listed requests?"), 1)
        self.assertIn("account-filtered listing skipped (credential unavailable); keeping the public listing", out)
        for row in entries("public"):
            self.assertIn(row["id"] + "\t", out)
        self.assertNotIn(KEY, out + err)
        self.assertNotIn(str(self.secret_file), out + err)
        self.assertEqual(self.state_bytes(), before)

    def test_unavailable_account_store_preserves_public_rows_without_disclosure(self):
        before = self.state_bytes()
        with mock.patch.object(secret_store, "default_store", side_effect=secret_store.SecretStoreError(
                f"fixture credential unavailable: {self.secret_file} {KEY}")):
            code, out, err = self.op(["discover", "openrouter"], "y\ny\n")
        self.assertEqual(code, 0, "credential availability must not discard the public listing")
        self.assertEqual(self.sent, [(PUBLIC, {})])
        self.assertEqual(err.count("Proceed with these listed requests?"), 1)
        self.assertIn("account-filtered listing skipped (credential unavailable); keeping the public listing", out)
        for row in entries("public"):
            self.assertIn(row["id"] + "\t", out)
        self.assertNotIn(KEY, out + err)
        self.assertNotIn(str(self.secret_file), out + err)
        self.assertEqual(self.state_bytes(), before)

    def test_key_removed_after_account_consent_preserves_public_rows(self):
        self.set_key()
        original = consent.confirm
        after_removal = {}

        def remove_key_after_consent(text, *, input_stream):
            answer = original(text, input_stream=input_stream)
            if ACCOUNT in text and answer:
                secret_store.default_store(self.runtime.gateway_environ()).delete("OPENROUTER_CLAUDE_API_KEY")
                after_removal.update(self.state_bytes())
            return answer

        with mock.patch.object(consent, "confirm", side_effect=remove_key_after_consent):
            code, out, err = self.op(["discover", "openrouter"], "y\ny\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("Proceed with these listed requests?"), 2)
        self.assertEqual(self.sent, [(PUBLIC, {})])
        self.assertIn("account-filtered listing skipped (credential unavailable); keeping the public listing", out)
        for row in entries("public"):
            self.assertIn(row["id"] + "\t", out)
        self.assertNotIn(WIRE + "\t", out)
        self.assertNotIn(KEY, out + err)
        self.assertNotIn(str(self.secret_file), out + err)
        self.assertTrue(after_removal)
        self.assertEqual(self.state_bytes(), after_removal)

    def test_account_store_malformed_after_consent_preserves_public_rows(self):
        self.set_key()
        original = consent.confirm
        after_change = {}

        def corrupt_store_after_consent(text, *, input_stream):
            answer = original(text, input_stream=input_stream)
            if ACCOUNT in text and answer:
                state.atomic_write(self.secret_file, f"malformed {KEY}\n".encode())
                after_change.update(self.state_bytes())
            return answer

        with mock.patch.object(consent, "confirm", side_effect=corrupt_store_after_consent):
            code, out, err = self.op(["discover", "openrouter"], "y\ny\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("Proceed with these listed requests?"), 2)
        self.assertEqual(self.sent, [(PUBLIC, {})])
        self.assertIn("account-filtered listing skipped (credential unavailable); keeping the public listing", out)
        for row in entries("public"):
            self.assertIn(row["id"] + "\t", out)
        self.assertNotIn(KEY, out + err)
        self.assertNotIn(str(self.secret_file), out + err)
        self.assertTrue(after_change)
        self.assertEqual(self.state_bytes(), after_change)

    def test_keyed_second_call_has_separate_consent_and_header_only_secret(self):
        self.set_key()
        before = self.state_bytes()
        code, out, err = self.op(["discover", "openrouter"], "y\ny\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent, [(PUBLIC, {}), (ACCOUNT, {"Authorization": "Bearer " + KEY})])
        self.assertEqual(err.count("Proceed with these listed requests?"), 2)
        self.assertIn("auth: bearer from OPENROUTER_CLAUDE_API_KEY (value not shown)", err)
        self.assertNotIn(KEY, out + err)
        self.assertIn("[account-only]", out)
        self.assertIn(WIRE, out)
        self.assertEqual(self.state_bytes(), before)

    def test_declining_account_call_preserves_public_models(self):
        self.set_key()
        code, out, err = self.op(["discover", "openrouter"], "y\nn\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent, [(PUBLIC, {})])
        self.assertIn("account-filtered listing declined", out)
        self.assertNotIn(WIRE + "\t", out)
        for row in entries("public"):
            self.assertIn(row["id"] + "\t", out)

    def test_declining_public_sends_nothing_and_never_offers_keyed_call(self):
        self.set_key()
        code, out, err = self.op(["discover", "openrouter"], "n\ny\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent, [])
        self.assertEqual(out, "")
        self.assertNotIn(ACCOUNT, err)

    def test_failed_or_malformed_account_result_preserves_public_rows(self):
        self.set_key()
        for raw in (None, b'{"data": {}}'):
            with self.subTest(raw=raw):
                self.bodies[ACCOUNT] = raw
                code, out, err = self.op(["discover", "openrouter"], "y\ny\n")
                self.assertEqual(code, 0, err)
                self.assertIn("account-filtered listing failed", out)
                for row in entries("public"):
                    self.assertIn(row["id"] + "\t", out)

    def test_key_cannot_escape_in_transport_errors_or_response(self):
        self.set_key()
        ordinary = self.transport
        for echo in (False, True):
            def transport(url, headers, **caps):
                if url == ACCOUNT:
                    if echo:
                        return json.dumps({"data": [{"id": KEY}]}).encode()
                    raise OSError("request headers: " + KEY)
                return ordinary(url, headers, **caps)
            self.runtime.listing_transport = transport
            code, out, err = self.op(["discover", "openrouter"], "y\ny\n")
            self.assertEqual(code, 0, err)
            self.assertNotIn(KEY, out + err)
            self.assertIn("account-filtered listing failed", out)

    def test_guard_runs_before_any_key_read_for_listing_and_lookup(self):
        for argv in (["discover", "openrouter"], ["discover", "openrouter", "--add", WIRE]):
            with mock.patch.object(secret_store, "default_store", side_effect=AssertionError("unguarded secret")):
                code, out, err = self.op(argv, "y\ny\n", env={"CLAUDECODE": ""})
                self.assertEqual(code, 1, out + err)
        self.assertEqual(self.sent, [])

    def test_account_plan_is_revalidated_before_secret_and_send(self):
        self.set_key()
        call = discover._plan_one(self.runtime, "openrouter", account=True)
        changed = dict(self.runtime.catalog.docs["providers"]["providers"]["openrouter"])
        changed["display"] = "Changed provider"
        with mock.patch.dict(self.runtime.catalog.docs["providers"]["providers"], {"openrouter": changed}), \
                mock.patch.object(discover, "_secret", side_effect=AssertionError("stale secret read")):
            with self.assertRaisesRegex(discover.providers_cmd.OperatorCommandError, "configuration changed"):
                discover._execute(self.runtime, call)
        self.assertEqual(self.sent, [])

    def test_account_fallback_does_not_hide_state_or_route_refusals(self):
        self.set_key()
        original = discover._revalidate
        before = self.state_bytes()
        for refusal in (
                state.StateError("fixture state integrity refused"),
                discover.providers_cmd.OperatorCommandError(discovery.STALE_TEXT.format(provider="openrouter")),
                discover.providers_cmd.OperatorCommandError(discovery.ROUTE_TEXT.format(provider="openrouter"))):
            with self.subTest(refusal=type(refusal).__name__):
                self.sent.clear()

                def revalidate(runtime, call):
                    if call.account:
                        raise refusal
                    return original(runtime, call)

                with mock.patch.object(discover, "_revalidate", side_effect=revalidate), \
                        mock.patch.object(secret_store.FileSecretStore, "get",
                                          side_effect=AssertionError("secret read after authority refusal")):
                    code, out, err = self.op(["discover", "openrouter"], "y\ny\n")
                self.assertEqual(code, 1, out + err)
                self.assertIn(str(refusal), err)
                self.assertNotIn("keeping the public listing", out)
                self.assertEqual(self.sent, [(PUBLIC, {})])
                self.assertNotIn(KEY, out + err)
                self.assertEqual(self.state_bytes(), before)

    def test_direct_stealth_add_fills_facts_without_admission_or_binding(self):
        code, out, err = self.op(["discover", "openrouter", "--add", WIRE], "y\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent, [(MODEL, {})])
        line = json.loads((self.pdir / "openrouter.json").read_text())["lines"]["custom-stealth-space-bunny-alpha"]
        self.assertEqual(line["wire_model"], WIRE)
        self.assertEqual(line["display"], "Space Bunny Alpha")
        self.assertEqual(line["family"], "unknown")
        self.assertEqual(line["context"]["declared_tokens"], 262144)
        self.assertEqual(line["context"]["source"], "listing")
        self.assertIn(MODEL, line["context"]["source_ref"])
        self.assertIn("text+image+video->text", line["notes"])
        self.assertIn("tools: yes", line["notes"])
        self.assertFalse(self.ledger() and self.ledger().admissions)
        self.assertFalse(self.http_calls)
        self.assertFalse(self.calls)

    def test_missing_metadata_requires_explicit_context_not_a_guess(self):
        self.bodies[MODEL] = json.dumps({"data": {"id": WIRE}}).encode()
        code, out, err = self.op(["discover", "openrouter", "--add", WIRE], "y\n")
        self.assertEqual(code, 1, err)
        self.assertIn("the listing states no context", err)
        self.assertFalse((self.pdir / "openrouter.json").exists())
        code, out, err = self.op(["discover", "openrouter", "--add", WIRE, "--context", "200000"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("context=unknown", out)

    def test_mismatched_or_failed_model_lookup_leaves_facts_unknown(self):
        for raw in (None, b'{"data": {"id": "vendor/other", "context_length": 200000}}'):
            self.bodies[MODEL] = raw
            code, out, err = self.op(["discover", "openrouter", "--add", WIRE], "y\n")
            self.assertEqual(code, 1, err)
            self.assertIn("facts unknown", out)
            self.assertFalse((self.pdir / "openrouter.json").exists())

    def test_free_variant_is_visible_but_not_declared(self):
        code, out, err = self.op(["discover", "openrouter", "--add", "vendor/free:free"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("not addable in this release", out)
        self.assertIn("not addable in this release", err)
        self.assertFalse((self.pdir / "openrouter.json").exists())

    def test_manual_models_add_remains_no_lookup(self):
        code, out, err = self.op(["models", "add", "openrouter", WIRE, "--context", "200000", "--source", "docs",
                                 "--source-ref", "https://example.com/model"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.ledger() and self.ledger().admissions)

    def test_tui_adapter_uses_same_two_consents_and_same_rows(self):
        self.set_key()
        consents = []
        before = self.state_bytes()
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            call, result = onboarding.listing(self.runtime, "openrouter",
                                             confirm=lambda text: consents.append(text) or True)
        self.assertEqual(len(consents), 2)
        self.assertNotIn("auth: bearer", consents[0])
        self.assertIn(ACCOUNT, consents[1])
        self.assertEqual(result.entries, discovery.merge_listings(entries("public"), entries("account")))
        self.assertEqual(self.state_bytes(), before)

    def test_tui_malformed_account_store_keeps_public_rows_and_notice(self):
        state.atomic_write(self.secret_file, f"malformed {KEY}\n".encode())
        before = self.state_bytes()
        consents, notices = [], []
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            call, result = onboarding.listing(
                self.runtime, "openrouter", confirm=lambda text: consents.append(text) or True,
                notice=notices.append)
        self.assertEqual(call.url, PUBLIC)
        self.assertEqual(result.entries, entries("public"))
        self.assertTrue(result.complete)
        self.assertEqual(len(consents), 1)
        self.assertEqual(notices, ["OpenRouter: account-filtered listing skipped (credential unavailable); "
                                   "keeping the public listing."])
        self.assertEqual(self.sent, [(PUBLIC, {})])
        self.assertNotIn(KEY, "\n".join(consents + notices))
        self.assertEqual(self.state_bytes(), before)

    def test_tui_picker_shares_facts_and_refuses_variant_selection(self):
        self.set_key()
        action = common.OnboardingActions(self.runtime, None, tui.MONO_PALETTE)
        action.confirm = lambda text: True
        action.show = mock.Mock()
        action.model_form = mock.Mock()
        captured = []
        def choose(picker, *args, **kwargs):
            captured.extend(item.label for item in picker.items)
            return [2]
        with mock.patch.object(consent, "stdio_ttys", return_value=True), \
                mock.patch.object(tui.SelectList, "run", choose):
            action.add_models("openrouter", mode="listing")
        expected = [discovery.entry_text(discovery.MarkedEntry(row, "candidate")).replace("\t", "  ")
                    for row in discovery.merge_listings(entries("public"), entries("account"))]
        self.assertEqual(captured, expected)
        action.model_form.assert_not_called()
        self.assertIn("not addable in this release", str(action.show.call_args))

    def test_tui_keeps_the_cli_catalog_classification(self):
        key, line = next((key, line) for key, line in self.runtime.catalog.lines.items()
                         if line["provider"] == "openrouter")
        self.listing(PUBLIC, [{"id": line["wire_model"]}])
        action = common.OnboardingActions(self.runtime, None, tui.MONO_PALETTE)
        action.confirm = lambda text: True
        action.show = mock.Mock()
        captured = []
        def choose(picker, *args, **kwargs):
            captured.extend(item.label for item in picker.items)
            return None
        with mock.patch.object(consent, "stdio_ttys", return_value=True), \
                mock.patch.object(tui.SelectList, "run", choose):
            action.add_models("openrouter", mode="listing")
        code, out, err = self.op(["discover", "openrouter"], "y\n")
        self.assertEqual(code, 0, err)
        cli_row = next(row for row in out.splitlines() if row.startswith(line["wire_model"] + "\t"))
        self.assertEqual(captured, [cli_row.replace("\t", "  ")])
        self.assertIn(f"cataloged as {key}", captured[0])

    def test_tui_keeps_the_cli_unadmitted_operator_classification(self):
        wire, key = "vendor/bare", "custom-router-bare"
        code, out, err = self.op(["models", "add", "openrouter", wire, "--as", key,
                                  "--context", "131072", "--source", "operator"])
        self.assertEqual(code, 0, err)
        self.listing(PUBLIC, [{"id": wire, "context_length": 131072}])
        action = common.OnboardingActions(self.runtime, None, tui.MONO_PALETTE)
        action.confirm = lambda text: True
        action.show = mock.Mock()
        action.model_form = mock.Mock()
        captured = []

        def choose(picker, *args, **kwargs):
            captured.extend(item.label for item in picker.items)
            return None

        before = self.state_bytes()
        with mock.patch.object(consent, "stdio_ttys", return_value=True), \
                mock.patch.object(tui.SelectList, "run", choose):
            action.add_models("openrouter", mode="listing")
        code, out, err = self.op(["discover", "openrouter"], "y\n")
        self.assertEqual(code, 0, err)
        cli_row = next(row for row in out.splitlines() if row.startswith(wire + "\t"))
        self.assertEqual(captured, [cli_row.replace("\t", "  ")])
        self.assertIn(f"operator {key} (not admitted)", captured[0])
        action.model_form.assert_not_called()
        self.assertEqual(self.state_bytes(), before)

    def test_tui_add_by_id_prefills_without_declaration(self):
        action = common.OnboardingActions(self.runtime, None, tui.MONO_PALETTE)
        action.confirm = lambda text: True
        action.show = mock.Mock()
        action.model_form = mock.Mock()
        before = self.state_bytes()
        with mock.patch.object(consent, "stdio_ttys", return_value=True), \
                mock.patch.object(tui.SelectList, "run", return_value=2), \
                mock.patch.object(tui.OnboardingForm, "run", return_value={"wire": WIRE}):
            action.add_models("openrouter")
        self.assertEqual(self.sent, [(MODEL, {})])
        provider, draft, key = action.model_form.call_args.args
        self.assertEqual(provider, "openrouter")
        self.assertEqual(draft["display"], "Space Bunny Alpha")
        self.assertEqual(draft["family"], "unknown")
        self.assertEqual(draft["context"]["declared_tokens"], 262144)
        self.assertEqual(self.state_bytes(), before)
