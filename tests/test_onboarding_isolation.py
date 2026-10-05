"""Fixture-only onboarding acceptance and boundary regressions.

Helpers also serve the PTY child. All runtime transports are injected; no
provider, live gateway, operator home, or native transcript is an input.
"""
from __future__ import annotations

import copy
import io
import json
import unittest
from unittest import mock

import test_cli
import test_qualify
import test_openai_compat_keyed as keyed
from claude_multi import catalog, operator, profile, qualify, state, strict_json, tui, views
import claude_multi.cli.consent as consent
import claude_multi.cli.onboarding as onboarding
import claude_multi.cli.types as types
import claude_multi.cli.screens.models as models_screen
import claude_multi.cli.screens.common as common
from test_tui import FakeWindow


class JourneyFixture(test_cli.OperatorCommandCase):
    def setUp(self):
        super().setUp()
        self.runtime.environ["TERM"] = "xterm-256color"
        names = {p["transport"].get("auth", {}).get("secret_ref", "").removeprefix("env:")
                 for p in self.runtime.catalog.docs["providers"]["providers"].values()}
        state.atomic_write(self.secret_file, "".join(f"{name}=fixture-only-dummy\n" for name in sorted(names) if name).encode())
        self.confirmations = []
        self.listing_calls = []
        self.runtime.listing_transport = self.listing
        self.runtime.exact_client_runner = lambda request: qualify.ExactClientOutcome("pass", "ok")
        self.patch = mock.patch.object(self.runtime, "contract_identity", return_value=test_qualify.CONTRACTS)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def listing(self, url, headers, **caps):
        self.listing_calls.append((url, headers, caps))
        return json.dumps({"data": [{"id": "vendor/fixture-reviewer", "name": "Fixture Reviewer",
                                    "context_length": 200000}]}).encode()

    def confirm(self, text):
        self.confirmations.append(text)
        return True

    def invoke(self, argv):
        output = io.StringIO()
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            code = onboarding.invoke(self.runtime, argv, confirm=self.confirm, output=output)
        return code, output.getvalue()

    def line(self, pool=False, family="openai"):
        result = {"wire_model": "claude-fixture-reviewer-9" if pool else "vendor/fixture-reviewer",
                  "display": "Fixture Reviewer", "efforts": ["high"] if pool else {"high": "output-config-high"},
                  "default_effort": "high", "context": {"declared_tokens": 1000000 if pool else 200000,
                                                         "source": "operator"},
                  "capabilities": ["lead", "agents"], "roles": ["cm-reviewer"]}
        if pool:
            result["output"] = {"declared_tokens": 64000, "source": "operator"}
        else:
            result["family"] = family
        return result

    def declare(self, pool=False, family="openai"):
        key = "custom-pool-reviewer" if pool else "custom-fixture-reviewer"
        output = io.StringIO()
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            code = onboarding.declare(self.runtime, "anthropic" if pool else "openrouter", key,
                                      self.line(pool, family), output=output)
        self.assertEqual(code, 0, output.getvalue())
        self.serve_current()
        return key

    def evaluation(self, key):
        document = copy.deepcopy(self.runtime.profiles.load("direct"))
        document["name"] = "fixture-onboarding"
        document.pop("seed", None)
        document["agents"] = {"cm-reviewer": {"model": key, "effort": "high"}}
        evaluated = profile.evaluate(document, self.runtime.lineup_catalog(),
                                     effective=self.runtime.current_effective())
        return document, evaluated


class OfflineJourneys(JourneyFixture):
    def test_openrouter_listing_declare_admit_qualify_bind_launch(self):
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            call, result = onboarding.listing(self.runtime, "openrouter", confirm=self.confirm)
        self.assertEqual(len(self.listing_calls), 1)
        self.assertNotIn("Authorization", self.listing_calls[0][1])
        draft = onboarding.line_draft(self.runtime, "openrouter", result.entries[0], call)
        self.assertEqual(draft["context"]["source"], "listing")
        key = self.declare()
        self.assertNotEqual(self.line()["family"], self.runtime.catalog.docs["providers"]["providers"]["openrouter"]["independence_family"])
        self.assertTrue(self.evaluation(key)[1].errors)
        self.assertEqual(self.invoke(["models", "admit", key])[0], 0)
        code, out = self.invoke(["models", "qualify", key, "--agents", "--tool-choice", "auto"])
        self.assertEqual(code, 0, out)
        document, evaluated = self.evaluation(key)
        self.assertFalse(evaluated.errors, evaluated.errors)
        self.assertEqual(evaluated.lineup.agents["cm-reviewer"].binding.family, "openai")
        rows = views.line_rows(self.runtime.lineup_catalog(), self.runtime.current_effective(), custom_ids=frozenset())
        self.assertEqual(next(row.family for row in rows if row.key == key), "openai")
        self.runtime.profiles.new(document)
        target = types.LaunchTarget("profile", document, document["name"], True, "Profile fixture-onboarding")
        prepared = self.runtime.prepare(target, action="fresh", passthrough=[])
        self.assertEqual(self.runtime.perform(prepared), 0)
        self.assertEqual(len(self.launches), 1)

    def test_pool_exact_client_both_outcomes_and_definition_reedit(self):
        key = self.declare(pool=True)
        self.assertEqual(self.invoke(["models", "admit", key])[0], 0)
        self.runtime.exact_client_runner = lambda request: qualify.ExactClientOutcome("inconclusive", "unavailable")
        code, out = self.invoke(["models", "qualify", key, "--agents"])
        self.assertEqual(code, 1, out)
        self.assertIn("exact-client proof unavailable on this platform", out)
        self.assertTrue(self.evaluation(key)[1].errors)
        self.runtime.exact_client_runner = lambda request: qualify.ExactClientOutcome("pass", "ok")
        code, out = self.invoke(["models", "qualify", key, "--agents"])
        self.assertEqual(code, 0, out)
        self.assertFalse(self.evaluation(key)[1].errors)
        self.assertTrue(any("exact-client check" in text and "offline" in text for text in self.confirmations))
        document = self.line(pool=True)
        document["roles"] = "all"
        output = io.StringIO()
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            self.assertEqual(onboarding.edit_line(self.runtime, key, document, confirm=self.confirm, output=output), 0)
        self.assertEqual(onboarding.statuses(self.runtime)[key], "changed — re-admit")
        self.assertTrue(self.evaluation(key)[1].errors)

    def test_unknown_aggregator_family_is_not_independent(self):
        key = self.declare(family="unknown")
        self.assertEqual(self.invoke(["models", "admit", key])[0], 0)
        self.assertEqual(self.invoke(["models", "qualify", key, "--agents"])[0], 0)
        _, evaluated = self.evaluation(key)
        self.assertFalse(evaluated.errors, evaluated.errors)
        self.assertEqual(evaluated.lineup.agents["cm-reviewer"].binding.family, "unknown")
        self.assertTrue(all("cross-family" not in " ".join(row) for row in views.routing_rows(evaluated.lineup)))

    def test_guard_precedes_callback_and_form_cancellation_writes_nothing(self):
        before = self.state_bytes()
        self.runtime.environ["CLAUDECODE"] = ""
        with self.assertRaises(consent.ConsentRefused):
            onboarding.listing(self.runtime, "openrouter", confirm=lambda text: self.fail("consent reached"))
        self.assertEqual(self.listing_calls, [])
        self.assertEqual(self.state_bytes(), before)
        form = tui.OnboardingForm("new provider", views.provider_form_fields())
        with mock.patch.object(tui, "hide_cursor"):
            self.assertIsNone(form.run(FakeWindow(["\x1b"])))
        self.assertEqual(self.state_bytes(), before)

    def test_default_and_explicit_provider_kinds_preserve_presets(self):
        self.assertEqual(dict((name, default) for name, _, default, _ in views.provider_form_fields())["kind"],
                         "anthropic-compatible")
        self.assertIn("recommended", views.PROVIDER_KIND_CHOICES[0][1])
        self.assertEqual(views.provider_form_fields(kind="openai-compatible-lan")[1][2], "openai-compatible-lan")
        args = list(test_cli.ACME_ADD)
        index = args.index("--kind")
        del args[index:index + 2]
        code, out = self.invoke(args)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads((self.pdir / "acme.json").read_text())["provider"]["kind"], "anthropic-compatible")
        preset = {"version": 1, "lines": {}, "provider": {
            "kind": "openai-compatible-lan", "display": "Fixture LAN", "auth": {"kind": "none"},
            "base_url": "http://fixture.lan:8000/v1", "independence_family": "local"}}
        with mock.patch.object(operator, "load_preset", return_value=strict_json.pretty_file_bytes(preset)):
            code, out = self.invoke(["providers", "add", "--preset", "fixture-lan", "--as", "fixture-lan"])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads((self.pdir / "fixture-lan.json").read_text())["provider"]["kind"], "openai-compatible-lan")

    def test_ineligible_named_picker_is_not_a_bypass(self):
        key = self.declare()
        self.assertEqual(self.invoke(["models", "admit", key])[0], 0)
        rows = views.line_rows(self.runtime.lineup_catalog(), self.runtime.current_effective(), custom_ids=frozenset())
        picker = views.picker_rows(rows, slot="cm-reviewer", bindings={"fixture": {"model": key, "effort": "high"}},
                                   lcat=self.runtime.lineup_catalog(), eff=self.runtime.current_effective(), current=None)
        line = next(row for row in picker.items if row.key == key)
        self.assertFalse(line.selectable)
        self.assertIn("◇", line.text)
        self.assertTrue(line.note)
        self.assertFalse(any(row.kind == "named" and row.selectable and row.key == "fixture" for row in picker.items))


class PromptContracts(unittest.TestCase):
    def test_final_text_is_the_report_deliverable(self):
        from _catalog import SHIPPED_ROOT, FIXTURE_ROOT
        for name in ("lead", "analyst", "designer"):
            rel = f"catalog/prompts/cm-{name}.md"
            text = (SHIPPED_ROOT / rel).read_text()
            self.assertEqual(text, (FIXTURE_ROOT / rel).read_text())
            self.assertNotIn("may create an explicitly requested", text)
            self.assertIn("report deliverable", text)
        roles = json.loads((SHIPPED_ROOT / "catalog/roles.json").read_text())["roles"]
        for name in ("cm-analyst", "cm-analyst-strong", "cm-designer"):
            self.assertNotIn("artifact only", roles[name]["description"])
            self.assertIn("no file edits", roles[name]["description"])
        self.assertIn("Do not ask a report-only agent to create a report file", (SHIPPED_ROOT / "catalog/prompts/cm-lead.md").read_text())


class ActionRefusalOwners(JourneyFixture):
    def test_E11_listing_without_context(self):
        import claude_multi.cli.commands.discover as discover
        self.assertEqual(discover.missing_context_text("fixture", "model-9"),
                         'discover fixture --add model-9: the listing states no context — claude-multi models add fixture model-9 --context N --source docs --source-ref "<URL, date>"')

    def test_E12_admit_unserved(self):
        import claude_multi.cli.commands.models as models
        key = self.declare()
        line = self.runtime.operator_snapshot().layer.lines[key]
        self.assertEqual(f"admit {key}: " + models._served_check(key, line, frozenset()),
                         f"admit {key}: 0/1 aliases served — claude-multi providers apply")

    def test_E13_ineligible_profile_slot(self):
        self.assertEqual(profile.AGENT_REFUSAL.format(prefix="profile fixture: ", slot="cm-reviewer",
                         key="custom-fixture", reason="no current tools evidence",
                         remedy="claude-multi models qualify custom-fixture --agents"),
                         "profile fixture: agents.cm-reviewer: custom-fixture is not agent-eligible (no current tools evidence) — claude-multi models qualify custom-fixture --agents")

    def test_E16_changed_route(self):
        self.assertEqual(operator.route_status_text("fixture", "changed", old="https://old.example", new="https://new.example"),
                         "provider fixture: route changed (origin https://old.example → https://new.example) — claude-multi providers approve fixture")

    def test_E19_session_guard(self):
        with self.assertRaises(consent.ConsentRefused) as raised:
            consent.require_human("models admit", {"CLAUDE_MULTI_MANAGED_ID": ""})
        self.assertEqual(str(raised.exception),
                         "models admit: needs a terminal outside Claude Code sessions (CLAUDE_MULTI_MANAGED_ID set) — run it in a separate shell")


class ObservationAcceptance(JourneyFixture):
    def test_B29_served_extras_are_advisory_and_do_not_write(self):
        import test_discovery
        from claude_multi import launch
        registry = test_discovery._registry_dir(self, {"claude": [{"id": "claude-fixture-unlisted", "context_length": 200000}]})
        self.runtime.environ[catalog.REGISTRY_DIR_ENV] = str(registry)
        self.runtime.served_models_callback = lambda gateway, token: (
            (launch.ServedModel("claude-fixture-unlisted", "anthropic", None),
             launch.ServedModel("unattributed-fixture", "someone", None)), 200)
        before = self.state_bytes()
        screen = models_screen._ModelsScreen(self.runtime, palette=tui.MONO_PALETTE)
        self.assertEqual([row.wire for row in screen.candidates.rows], ["claude-fixture-unlisted"])
        self.assertEqual([row.wire for row in screen.candidates.unattributed], ["unattributed-fixture"])
        self.assertIn("__candidates__", screen.keys)
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.listing_calls, [])
        self.assertEqual(self.http_calls, [])

    def test_selected_transport_display_and_D28_need_the_selected_key(self):
        import claude_multi.cli.gateway_facts as facts
        state.atomic_write(self.secret_file, self.secret_file.read_bytes() + b"PLATFORM_ANTHROPIC_API_KEY=fixture-key-only\n")
        self.assertEqual(self.invoke(["providers", "transport", "anthropic", "api-key"])[0], 0)
        self.serve_current()
        row = next(f for f in facts._provider_facts(self.runtime).facts if f["id"] == "anthropic")
        self.assertEqual(row["kind"], "direct")
        self.assertEqual(row["transport_label"], "transport: api-key")
        doc = self.runtime.profiles.load("direct")
        lineup = profile.resolve(doc, self.runtime.lineup_catalog(), effective=self.runtime.current_effective())
        self.assertEqual(self.runtime.lineup_secret_problems(lineup), [])
        state.atomic_write(self.secret_file, b"KIMI_CLAUDE_API_KEY=fixture-only\n")
        self.assertTrue(any("PLATFORM_ANTHROPIC_API_KEY" in line for line in self.runtime.lineup_secret_problems(lineup)))

    def test_catalog_admission_TUI_guard_refuses_before_prompt(self):
        import test_screens_catalog as fixtures
        from _catalog import FIXTURE_ROOT
        key = fixtures.new_line_key(catalog.load_catalog(FIXTURE_ROOT))
        # Existing fixture has active lines; use a view-only New marker for the
        # selected row. The guard must fire before Settings is asked to write.
        screen = models_screen._ModelsScreen(self.runtime, palette=tui.MONO_PALETTE)
        screen.index = screen.keys.index(key)
        screen._row_kind = lambda *args: "new"
        self.runtime.environ["CLAUDECODE"] = ""
        before = self.state_bytes()
        with mock.patch.object(tui.Modal, "run", side_effect=AssertionError("must not prompt")):
            screen._toggle_admission(FakeWindow([]))
        self.assertIn("needs a terminal outside Claude Code sessions", screen.message)
        self.assertEqual(self.state_bytes(), before)

    def test_tiny_consent_never_accepts_hidden_authority(self):
        with mock.patch.object(tui, "hide_cursor"):
            self.assertFalse(tui.TextView("approve", ["full plan"], confirm=True).run(
                FakeWindow(["y", "\x1b"], height=5, width=20)))
            self.assertFalse(tui.TextView("declare", ["full declaration"], confirm="declare").run(
                FakeWindow(["\n", "\x1b"], height=5, width=20)))


class FormProvenanceTests(JourneyFixture):
    def test_listing_wire_cannot_change_under_the_old_provenance(self):
        draft = self.line()
        draft["context"] = {"declared_tokens": 200000, "source": "listing", "source_ref": "https://fixture.example/models 2026-09-30"}
        values = {name: str(default) for name, _, default, _ in views.model_form_fields(draft, key="custom-fixture")}
        values["wire"] = "another-model"
        action = common.OnboardingActions(self.runtime, FakeWindow([]), tui.MONO_PALETTE)
        with mock.patch.object(tui.OnboardingForm, "run", return_value=values), \
                mock.patch.object(action, "show") as shown, mock.patch.object(onboarding, "declare") as declare:
            action.model_form("openrouter", draft, "custom-fixture")
        declare.assert_not_called()
        self.assertIn("prefill belongs to the selected wire", str(shown.call_args))

    def test_manually_entered_output_never_inherits_listing_provenance(self):
        draft = self.line()
        draft["context"] = {"declared_tokens": 200000, "source": "listing", "source_ref": "https://fixture.example/models 2026-09-30"}
        values = {name: str(default) for name, _, default, _ in views.model_form_fields(draft, key="custom-fixture")}
        values["output"] = "64000"
        action = common.OnboardingActions(self.runtime, FakeWindow([]), tui.MONO_PALETTE)
        with mock.patch.object(tui.OnboardingForm, "run", return_value=values), \
                mock.patch.object(action, "show"), mock.patch.object(action, "preview", return_value=True), \
                mock.patch.object(onboarding, "declare", return_value=0) as declare:
            action.model_form("openrouter", draft, "custom-fixture")
        self.assertEqual(declare.call_args.args[3]["output"], {"declared_tokens": 64000, "source": "operator"})


class ListingCheckboxTests(JourneyFixture):
    def test_space_shows_the_toggle_and_enter_declares_exactly_the_checked_models(self):
        """The production listing picker never toggles
        invisibly: Space shows [x], a second Space [ ]."""
        win = FakeWindow(["\n", " ", " ", " ", "\n"], height=24, width=80)
        action = common.OnboardingActions(self.runtime, win, tui.MONO_PALETTE)
        action.confirm = lambda text: True
        declared = []
        with mock.patch.object(consent, "stdio_ttys", return_value=True), \
                mock.patch.object(action, "model_form", side_effect=lambda pid, draft, key: declared.append(key)):
            action.add_models("openrouter")
        picker = [frame for frame in win.frames if "advertised models" in frame]
        rows = [next(line for line in frame.splitlines() if "vendor/fixture-reviewer" in line) for frame in picker]
        self.assertEqual([row.strip() for row in rows],
                         ["> [ ] vendor/fixture-reviewer", "> [x] vendor/fixture-reviewer",
                          "> [ ] vendor/fixture-reviewer", "> [x] vendor/fixture-reviewer"])
        self.assertEqual(declared, ["custom-vendor-fixture-reviewer"])

    def test_an_empty_selection_says_nothing_was_declared(self):
        win = FakeWindow(["\n", "\n", "\x1b"], height=24, width=80)
        action = common.OnboardingActions(self.runtime, win, tui.MONO_PALETTE)
        action.confirm = lambda text: True
        with mock.patch.object(consent, "stdio_ttys", return_value=True), \
                mock.patch.object(action, "model_form", side_effect=AssertionError("declared")):
            action.add_models("openrouter")
        self.assertIn("nothing was declared", win.frames[-1])


class OperatorAgentRollbackTests(JourneyFixture):
    """A 3.0.2 reader has
    no operator layer, so a record or profile binding a T2 agent (a starter
    saved from operator lines is such a profile) refuses with the
    unknown-model evaluation error; the runbook remedy is Sessions → T →
    a catalog-only profile."""

    def test_a_t2_agent_binding_is_an_unknown_model_under_the_302_view(self):
        from claude_multi import cli, sessions
        sessions.ensure_state_v4(self.runtime.session_store.root)
        key = self.declare()
        self.assertEqual(self.invoke(["models", "admit", key])[0], 0)
        self.assertEqual(self.invoke(["models", "qualify", key, "--agents"])[0], 0)
        document, evaluated = self.evaluation(key)
        self.assertEqual(evaluated.errors, ())
        self.runtime.profiles.new(document)
        target = types.LaunchTarget("profile", document, document["name"], True, "Profile fixture-onboarding")
        prepared = self.runtime.prepare(target, action="fresh", passthrough=[])
        self.assertEqual(self.runtime.perform(prepared), 0)
        unknown = f"agents.cm-reviewer.model: unknown model {key!r}"
        # The 3.0.2 view: the reviewed catalog alone (no providers.d, ledger or evidence).
        old = profile.LineupCatalog.from_docs(self.runtime.catalog.docs)
        self.assertEqual(profile.evaluate(document, old, effective=self.runtime.current_effective()).errors,
                         (unknown,))
        for follow in (True, False):
            with self.subTest(follow=follow):
                record = dict(prepared.record, last_event_source="end", follow=follow)
                self.runtime.session_store.save(record)
                with mock.patch.object(type(self.runtime), "lineup_catalog", lambda _self: old):
                    with self.assertRaises(types.LaunchPlanError) as caught:
                        self.runtime.prepare(cli._record_resume_target(record), action="resume", passthrough=[],
                                             session_id=record["managed_id"])
                self.assertIn(unknown, str(caught.exception))
        # The remedy: another (catalog-only) profile evaluates under 3.0.2.
        self.assertEqual(profile.evaluate(self.runtime.profiles.load("direct"), old,
                                          effective=self.runtime.current_effective()).errors, ())


class CardOperatorLeadTests(JourneyFixture):
    def test_an_operator_lead_is_tagged_op_beside_its_class(self):
        """An operator lead is tagged op beside its class."""
        import claude_multi.cli.screens.launch_sessions as launch_sessions
        key = self.declare()
        self.assertEqual(self.invoke(["models", "admit", key])[0], 0)
        lcat = self.runtime.lineup_catalog()
        catalog_key = next(k for k, entry in sorted(lcat.lines.items())
                           if lcat.origin(k) == "catalog" and entry.get("lead") is not None)
        for lead, tagged in ((key, True), (catalog_key, False)):
            with self.subTest(lead=lead):
                document = profile.ad_hoc_direct(lead)
                evaluated = profile.evaluate(document, lcat, effective=self.runtime.current_effective(), ad_hoc=True)
                self.assertFalse(evaluated.errors, evaluated.errors)
                card = views.card_model(
                    target=types.LaunchTarget("ad-hoc", document, None, False, f"Direct {lead}"),
                    lineup=evaluated.lineup, lead_origin=launch_sessions._lead_origin(self.runtime, evaluated.lineup))
                row = next(row for row in card.rows if row.kind == "lead")
                self.assertEqual(row.text.endswith(" class op"), tagged, row.text)


class KeyedProviderKeyTests(JourneyFixture):
    """A keyed N offers the key modal and K stores it through the setup
    layer; the value never passes argv or any shown text."""

    def test_manual_auth_choices_follow_kind_changes_and_backtracking(self):
        from test_tui import RIGHT, UP

        enter, back = "\n", "\x7f"
        for steps, kind, auth in ((0, "anthropic-compatible", "header"),
                                  (1, "openai-compatible", "bearer"),
                                  (2, "openai-compatible-lan", "none")):
            with self.subTest(kind=kind):
                # Select header on the initial Anthropic form, then go back to
                # its kind and change it before revisiting authentication.
                keys = [*"acme", enter, enter, *"https://api.acme.example/v1", enter, RIGHT,
                        back, UP, *([RIGHT] * steps), enter, enter,
                        enter, *([] if auth == "none" else list("ACME_API_KEY")),
                        enter, enter, enter, enter, enter, enter]
                win = FakeWindow(keys, height=30, width=100)
                action = common.OnboardingActions(self.runtime, win, tui.MONO_PALETTE)
                with mock.patch.object(catalog, "keyed_compat_audited", return_value=True), \
                        mock.patch.object(action, "preview", return_value=True), \
                        mock.patch.object(action, "invoke", return_value=0) as invoke, \
                        mock.patch.object(action, "offer_key"):
                    action.new_provider(manual_only=True)
                argv = invoke.call_args.args[0]
                self.assertEqual(argv[argv.index("--kind") + 1], kind)
                self.assertEqual(argv[argv.index("--auth") + 1], auth)
                frame = next(frame for frame in reversed(win.frames) if "— Authentication" in frame)
                if auth != "header":
                    self.assertNotIn("x-api-key", frame)
                if auth != "none":
                    self.assertNotIn("none (LAN)", frame)
                    self.assertIn("--secret-ref", argv)
                else:
                    self.assertNotIn("Bearer", frame)
                    self.assertNotIn("--secret-ref", argv)
                self.assertEqual("--header" in argv, auth == "header")

    def test_keyed_new_provider_offers_the_key_modal(self):
        import claude_multi.cli.screens.providers as providers_screen
        import claude_multi.cli.text as cli_text
        from claude_multi import secret_store

        values = {"id": "acme", "kind": "anthropic-compatible", "url": "https://api.acme.example/anthropic",
                  "auth": "bearer", "secret": "ACME_API_KEY", "family": "acme", "contracts": "output-config-high",
                  "listing": "", "shape": "", "listing_auth": ""}
        shown, asked = [], []
        action = common.OnboardingActions(self.runtime, FakeWindow([]), tui.MONO_PALETTE)
        action.show = lambda title, lines: shown.append((title, list(lines)))
        action.confirm = lambda text: True
        with mock.patch.object(tui.SelectList, "run", return_value=0), \
                mock.patch.object(tui.OnboardingForm, "run", return_value=values), \
                mock.patch.object(action, "preview", return_value=True), \
                mock.patch.object(consent, "stdio_ttys", return_value=True), \
                mock.patch.object(providers_screen.ConnectActions, "_ask_key",
                                  side_effect=lambda *a: asked.append(a) or "acme-dummy-value"), \
                mock.patch.object(providers_screen.ConnectActions, "continue_to_models",
                                  return_value=None) as models_next:
            action.new_provider()
        # The new provider has no models: the key continues into adding them.
        models_next.assert_called_once_with("acme")
        self.assertEqual(len(asked), 1, "the key modal opens once after a keyed N")
        self.assertIsNone(asked[0][2], "a new key is not a replacement")
        self.assertEqual(shown[-1][0], cli_text.KEY_MODAL_TITLE.format(display="acme"))
        self.assertTrue(any("API key saved (" in line for line in shown[-1][1]), shown[-1])
        self.assertNotIn("acme-dummy-value", repr(shown))
        self.assertEqual(secret_store.default_store(self.runtime.environ).get("ACME_API_KEY"), "acme-dummy-value")
        # A set key is not offered again; K on a set key replaces only on an explicit choice.
        asked.clear()
        action.offer_key("acme")
        self.assertEqual(asked, [])
        with mock.patch.object(consent, "stdio_ttys", return_value=True), \
                mock.patch.object(providers_screen.ConnectActions, "_ask_key",
                                  side_effect=lambda *a: asked.append(a) or None):
            outcome = providers_screen.ConnectActions(self.runtime, FakeWindow([]), tui.MONO_PALETTE).set_key("acme")
        self.assertEqual(asked[0][2], len("acme-dummy-value"), "the replace modal names the current length")
        self.assertIn("API key kept — nothing changed", outcome.text)
        self.assertEqual(secret_store.default_store(self.runtime.environ).get("ACME_API_KEY"), "acme-dummy-value")


class FormEditRecoveryTests(JourneyFixture):
    def test_invalid_form_does_not_claim_a_saved_editor_file(self):
        key = self.declare()
        draft = self.line()
        draft["roles"] = ["cm-no-such-role"]
        path = self.pdir / "openrouter.json"
        before = path.read_bytes()
        output = io.StringIO()
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            code = onboarding.edit_line(self.runtime, key, draft, confirm=self.confirm, output=output)
        self.assertEqual(code, 1)
        self.assertEqual(before, path.read_bytes())
        self.assertIn("form draft was not saved", output.getvalue())
        self.assertNotIn("kept at None", output.getvalue())
        self.assertEqual(self.http_calls, [])


class FormParserBoundaryTests(unittest.TestCase):
    def test_bad_form_fields_cannot_exit_or_print_over_curses(self):
        import contextlib
        from types import SimpleNamespace
        import claude_multi.cli.commands.providers as providers
        runtime = SimpleNamespace(allow_state_writes=True, gateway_environ=lambda: {})
        for value in ("--help", "--not-a-provider-id"):
            stdout, stderr, report = io.StringIO(), io.StringIO(), io.StringIO()
            with self.subTest(value=value), mock.patch.object(consent, "stdio_ttys", return_value=True), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaisesRegex(providers.OperatorUsageError, "invalid onboarding fields"):
                    onboarding.invoke(runtime, ["providers", "add", value],
                                      confirm=lambda _: self.fail("must not reach consent"), output=report)
            self.assertEqual((stdout.getvalue(), stderr.getvalue(), report.getvalue()), ("", "", ""))


class OperatorStateRollbackTests(JourneyFixture):
    def test_rollback_t2_agent_unknown_key_and_catalog_remedy(self):
        # The rollback journey supplies the recorded T2 grant, old
        # catalog-only evaluation and explicit catalog-profile remedy.
        OperatorAgentRollbackTests.test_a_t2_agent_binding_is_an_unknown_model_under_the_302_view(self)

    def test_operator_state_is_safe_under_an_earlier_release_rollback(self):
        from claude_multi import sessions, validate, settings
        key = self.declare()
        self.assertEqual(self.invoke(['models', 'admit', key])[0], 0)
        before = self.state_bytes()
        # The authority artifacts live outside the closed documents of earlier releases.
        # Same settings/session schemas, no new record version or input rewriting.
        doc = self.runtime.settings_store.load()
        self.assertEqual(validate.validate(doc, strict_json.load(self.runtime.asset_root / 'schemas/settings.schema.json'), '$'), [])
        self.assertEqual(sessions.RECORD_VERSION, 4)
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(profile.evaluate(self.runtime.profiles.load('direct'),
                         profile.LineupCatalog.from_docs(self.runtime.catalog.docs),
                         effective=self.runtime.current_effective()).errors, ())

    def test_rollback_t2_workflow_default_fresh_repair_resume(self):
        from claude_multi import cli, sessions, settings, transition
        sessions.ensure_state_v4(self.runtime.session_store.root)
        key = self.declare()
        self.assertEqual(self.invoke(['models', 'admit', key])[0], 0)
        self.assertEqual(self.invoke(['models', 'qualify', key, '--agents'])[0], 0)
        self.runtime.settings_store.update(lambda d: d.update(workflow_default_binding={'model': key, 'effort': 'high'}),
                                           catalog=self.runtime.lineup_catalog())
        target = types.LaunchTarget('profile', self.runtime.profiles.load('direct'), 'direct', True, 'Profile direct')
        prepared = self.runtime.prepare(target, action='fresh', passthrough=[])
        self.runtime.perform(prepared)
        recorded = dict(prepared.record, last_event_source='end')
        self.runtime.session_store.save(recorded)
        self.assertEqual(recorded['applied']['settings']['workflow_default_binding']['model'], key)
        old = profile.LineupCatalog.from_docs(self.runtime.catalog.docs)
        # The current default is cleared under 3.1 before rollback, never by
        # rewriting a recorded snapshot. The latter needs resume, not repair.
        self.runtime.settings_store.update(lambda d: d.update(workflow_default_binding=None), catalog=old)
        parts = self.runtime.converge_parts()
        try:
            expected = transition.expected_plan(recorded, docs=self.runtime.catalog.docs,
                prompt_bodies=parts.prompt_bodies, state_root=self.runtime.session_store.root,
                hook_command=parts.hook_command, token_helper_command=parts.token_helper_command,
                live=None, worktree_available=True, environ=self.runtime.environ, managed_root=self.runtime.managed_root)
        except Exception as exc:
            self.assertIn('workflow_default_binding', str(exc))
        else:
            self.assertIsNone(expected.plan)
            self.assertIn('workflow_default_binding', expected.reason)
        with mock.patch.object(type(self.runtime), 'lineup_catalog', return_value=old):
            fresh = self.runtime.prepare(target, action='fresh', passthrough=[])
            self.assertIsNone(fresh.record['applied']['settings']['workflow_default_binding'])
            resumed = self.runtime.prepare(target, action='resume', passthrough=[], session_id=recorded['managed_id'])
            self.assertIsNone(resumed.record['applied']['settings']['workflow_default_binding'])


# Additive keyed chat journeys through the existing guarded services.


class KeyedJourneyFixture(keyed.KeyedServedCase, JourneyFixture):
    def declare_keyed(self):
        code, out, err = self.op([
            "providers", "add", keyed.KEYED_ID, "--kind", "openai-compatible",
            "--base-url", keyed.KEYED_BASE, "--auth", "bearer", "--secret-ref", "env:" + keyed.KEYED_SECRET,
            "--family", "unknown", "--declare-only"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.runtime.operator_snapshot().layer.route_status[keyed.KEYED_ID], "unapproved")
        self.assertEqual(self.http_calls, [])
        # A fixture-only key: never the operator store, hidden-input UI or network.
        state.atomic_write(self.secret_file, self.secret_file.read_bytes() +
                           f"{keyed.KEYED_SECRET}={keyed.KEYED_VALUE}\n".encode())
        line = keyed.keyed_document(efforts=("high",))["lines"][keyed.KEYED_KEY]
        line.update(capabilities=["lead", "agents"], roles=["cm-reviewer"], family="unknown")
        line["context"] = {"declared_tokens": 200000, "source": "operator"}
        out = io.StringIO()
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            code = onboarding.declare(self.runtime, keyed.KEYED_ID, keyed.KEYED_KEY, line, output=out)
        self.assertEqual(code, 0, out.getvalue())
        return keyed.KEYED_KEY

    def keyed_picker(self, key):
        cat, eff = self.runtime.lineup_catalog(), self.runtime.current_effective()
        rows = views.line_rows(cat, eff, custom_ids=frozenset())
        picker = views.picker_rows(rows, slot="cm-reviewer", bindings={}, lcat=cat, eff=eff, current=None)
        return next(row for row in picker.items if row.key == key)

    def approve_keyed(self):
        code, out, err = self.op(["providers", "approve", keyed.KEYED_ID], "y\n")
        self.assertEqual(code, 0, out + err)
        self.publish()
        self.serve_current()


class KeyedJourneyTests(KeyedJourneyFixture):
    def test_keyed_cli_declare_approve_apply_admit_qualify_bind(self):
        key = self.declare_keyed()
        self.assertNotIn(key, self.runtime.current_effective().admitted_lines)
        self.assertFalse(self.keyed_picker(key).selectable)
        self.approve_keyed()
        self.assertEqual(onboarding.statuses(self.runtime)[key], "off")
        self.assertEqual(self.http_calls, [])
        code, out, err = self.op(["models", "admit", key], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.http_calls), 1)
        self.assertFalse(self.keyed_picker(key).selectable)
        before = self.ledger_file.read_bytes()
        code, out, err = self.op(["models", "qualify", key, "--agents"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.ledger_file.read_bytes(), before)
        self.assertTrue(self.keyed_picker(key).selectable)
        document, evaluated = self.evaluation(key)
        self.assertFalse(evaluated.errors, evaluated.errors)
        self.runtime.profiles.new(document)
        prepared = self.runtime.prepare(types.LaunchTarget("profile", document, document["name"], True,
                                                           "Profile fixture-onboarding"), action="fresh", passthrough=[])
        self.assertEqual(self.runtime.perform(prepared), 0)
        self.assertEqual(len(self.launches), 1)

    def test_keyed_cli_changes_and_failed_qualification_remain_dimmed(self):
        key = self.declare_keyed()
        self.approve_keyed()
        self.assertEqual(self.op(["models", "admit", key], "y\n")[0], 0)
        self.assertEqual(self.op(["models", "qualify", key, "--agents"], "y\n")[0], 0)
        self.assertTrue(self.keyed_picker(key).selectable)
        _, line = onboarding.declaration(self.runtime, key)
        line["roles"] = "all"
        output = io.StringIO()
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            self.assertEqual(onboarding.edit_line(self.runtime, key, line, confirm=self.confirm, output=output), 0)
        self.assertEqual(onboarding.statuses(self.runtime)[key], "changed — re-admit")
        self.assertFalse(self.keyed_picker(key).selectable)
        self.serve_current()
        self.assertEqual(self.op(["models", "admit", key], "y\n")[0], 0)
        self.assertFalse(self.keyed_picker(key).selectable)
        good = self.runtime.qualify_http
        self.runtime.qualify_http = lambda *args: qualify.HttpResult(400, b'{"error":{"message":"fixture refusal"}}')
        code, out, err = self.op(["models", "qualify", key, "--agents"], "y\n")
        self.assertEqual(code, 1, out + err)
        self.assertFalse(self.keyed_picker(key).selectable)
        self.assertTrue(self.evaluation(key)[1].errors)
        self.runtime.qualify_http = good
        code, out, err = self.op(["models", "qualify", key, "--agents"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.keyed_picker(key).selectable)
