"""The reviewed presets: vendors with a documented Anthropic-compatible or
OpenAI-compatible endpoint, and servers on your computer or network.

Each preset is reviewed data over the generic routes (display name, kind,
base URL, the key's name, the model list) plus its review block: the
support label ``preset`` and the documentation it was read from. Covered
here: the schema and lint of every shipped preset (HTTPS for a key, no
secret, no duplicate id, display or key name), the provider pickers (Get
started's and Providers N's) listing every keyed preset with the
OpenAI-compatible ones closed until that route opens (both states), the
setup layer's plans, the setup menu, replacing a key two providers share
(named first, default No), and one preset alone reaching a usable profile.
Preset ids are derived from the shipped files, never pinned; temp home,
fixture gateway, no provider request."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import json
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import test_cli
import test_setup_cli
from _catalog import SHIPPED_ROOT
from claude_multi import catalog, operator as operator_mod, profile, secret_store, served_plan, state, strict_json
from claude_multi import validate as schema_validate
from claude_multi.setup import model, providers as setup_providers, texts
import claude_multi.cli.commands.providers as providers_cmd
import claude_multi.cli.consent as consent

KEYED_KINDS = ("anthropic-compatible", operator_mod.KEYED_KIND)


def shipped() -> dict[str, operator_mod.PresetInfo]:
    return operator_mod.presets(SHIPPED_ROOT)


def shipped_raw(name: str) -> bytes:
    return operator_mod.load_preset(name, SHIPPED_ROOT)


def first(kind: str) -> str:
    """The first shipped preset of ``kind`` (by name; never pinned)."""

    names = sorted(name for name, info in shipped().items() if info.kind == kind and not info.generic)
    if not names:
        raise AssertionError(f"no shipped preset of kind {kind}")
    return names[0]


@contextlib.contextmanager
def shipped_presets():
    """The fixture asset root ships no presets: read the shipped reviewed ones."""

    paths, load = operator_mod.preset_paths, operator_mod.load_preset
    with mock.patch.object(operator_mod, "preset_paths", lambda _root=None: paths(SHIPPED_ROOT)), \
            mock.patch.object(operator_mod, "load_preset", lambda name, _root=None: load(name, SHIPPED_ROOT)):
        yield


def keyed_route(open_: bool):
    return mock.patch.object(catalog, "keyed_compat_audited", return_value=open_)


class PresetLintTests(unittest.TestCase):
    """Every shipped preset is well-formed reviewed data."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = catalog.load_catalog(SHIPPED_ROOT)
        cls.schemas = operator_mod.load_schemas(SHIPPED_ROOT)
        cls.schema = operator_mod.load_preset_schema(SHIPPED_ROOT)
        cls.names = sorted(operator_mod.preset_paths(SHIPPED_ROOT))

    def test_every_preset_carries_its_review(self) -> None:
        self.assertTrue(self.names)
        self.assertEqual(sorted(shipped()), self.names, "every preset reads, none is left out of the picker")
        # A check date is never ahead of today anywhere (it is the reviewer's
        # local date; the suite may run in UTC).
        latest = datetime.date.today() + datetime.timedelta(days=1)
        for name in self.names:
            with self.subTest(preset=name):
                raw = shipped_raw(name)
                self.assertEqual(raw, strict_json.pretty_file_bytes(strict_json.loads(raw)), "canonical bytes")
                document = strict_json.loads(raw)
                review = document[operator_mod.PRESET_BLOCK]
                self.assertEqual(schema_validate.validate(review, self.schema), [])
                self.assertEqual(review["support"], operator_mod.PRESET_SUPPORT)  # never "verified"
                if review["source"] == "docs":
                    self.assertTrue(review["source_ref"].startswith("https://"))
                    self.assertLessEqual(datetime.date.fromisoformat(review["checked"]), latest)
                else:
                    self.assertEqual(document["provider"]["kind"], operator_mod.LAN_KIND)
                self.assertEqual(document["lines"], {}, "a preset declares no model; models are added after")
                self.assertNotIn(operator_mod.PRESET_BLOCK, operator_mod.preset_document(raw))

    def test_the_schema_refuses_a_stronger_label_or_a_missing_source(self) -> None:
        good = {"support": "preset", "source": "docs", "source_ref": "https://vendor.example/docs",
                "checked": "2026-10-04"}
        self.assertEqual(schema_validate.validate(good, self.schema), [])
        for bad in ({**good, "support": "verified"}, {k: v for k, v in good.items() if k != "source_ref"},
                    {**good, "source_ref": "http://vendor.example/docs"}, {**good, "checked": "yesterday"},
                    {"support": "preset", "source": "generic", "source_ref": "https://vendor.example/docs"},
                    {**good, "extra": "x"}):
            with self.subTest(review=bad):
                self.assertTrue(schema_validate.validate(bad, self.schema))
        raw = strict_json.pretty_file_bytes({"version": 1, "lines": {}, "provider": {"kind": "openai-compatible-lan"}})
        with self.assertRaisesRegex(operator_mod.OperatorError, "no preset block"):
            operator_mod.preset_info("unreviewed", raw, schema=self.schema)

    def test_ids_displays_and_key_names_are_unique_and_never_a_shipped_provider(self) -> None:
        infos = shipped()
        providers = self.bundle.docs["providers"]["providers"]
        reserved = {str(((p.get("transport") or {}).get("auth") or {}).get("secret_ref", "")).removeprefix("env:")
                    for p in providers.values()}
        reserved |= {alternative.secret_name for alternative in operator_mod.TRANSPORT_ALTERNATIVES.values()}
        samples = set(operator_mod.sample_paths(SHIPPED_ROOT)) - set(self.names)
        displays = [info.display.lower() for info in infos.values()]
        self.assertEqual(len(displays), len(set(displays)), "display names repeat")
        keys = [info.secret_name for info in infos.values() if info.keyed]
        self.assertEqual(len(keys), len(set(keys)), "two presets share a key name")
        for name, info in sorted(infos.items()):
            with self.subTest(preset=name):
                self.assertRegex(name + ".json", operator_mod.FILE_NAME)
                self.assertNotIn(name, providers)
                self.assertNotIn(name, samples)
                if info.keyed:
                    self.assertIn(info.kind, KEYED_KINDS)
                    self.assertIsNone(secret_store.secret_name_problem(info.secret_name))
                    self.assertNotIn(info.secret_name, reserved)
                else:
                    self.assertEqual(info.kind, operator_mod.LAN_KIND)

    def test_endpoints_keys_and_listings_follow_the_route_policy(self) -> None:
        for name, info in sorted(shipped().items()):
            with self.subTest(preset=name):
                document = strict_json.loads(shipped_raw(name))
                self.assertEqual(operator_mod._secret_hits(document, "$", ()), [], "no secret-shaped text")
                origin, _url = operator_mod.normalize_endpoint(info.base_url, keyed=info.keyed,
                                                               lan=not info.keyed)
                if info.keyed:
                    self.assertTrue(origin.startswith("https://"), "a key is sent only over HTTPS")
                    self.assertIsNone(catalog.platform_host_problem(origin))
                    self.assertEqual(document["provider"]["auth"]["secret_ref"], f"env:{info.secret_name}")
                if info.listing_url is not None:
                    listing = document["provider"]["listing"]
                    self.assertEqual(listing["auth"], "provider" if info.keyed else "none")
                    listing_origin, _ = operator_mod.normalize_endpoint(info.listing_url, keyed=info.keyed,
                                                                        lan=not info.keyed)
                    self.assertEqual(listing_origin, origin, "the key goes only to its own origin")
                    self.assertTrue(urllib.parse.urlsplit(info.listing_url).path.endswith("/models"))

    def test_each_declaration_validates_and_grants_nothing(self) -> None:
        for name, info in sorted(shipped().items()):
            with self.subTest(preset=name):
                declared = operator_mod.document_bytes(operator_mod.preset_document(shipped_raw(name)))
                with keyed_route(True):
                    layer = operator_mod.resolve_provider_document(self.bundle.docs, name, declared,
                                                                   schemas=self.schemas)
                self.assertEqual(layer.problems, ())
                self.assertEqual(layer.route_status[name], "unapproved" if info.keyed else "keyless")
                if info.kind == operator_mod.KEYED_KIND:
                    # Closed route: refused on its metadata alone, before any key is read.
                    with keyed_route(False):
                        closed = operator_mod.resolve_provider_document(self.bundle.docs, name, declared,
                                                                        schemas=self.schemas)
                    self.assertEqual([problem.code for problem in closed.problems], ["kind-gated"])
                    self.assertNotIn(name, closed.providers)

    def test_a_base_url_override_retargets_the_listing(self) -> None:
        name = first(operator_mod.LAN_KIND)
        document = operator_mod.preset_document(shipped_raw(name), base_url="http://box.lan:9000/v1")
        self.assertEqual(document["provider"]["base_url"], "http://box.lan:9000/v1")
        if "listing" in document["provider"]:
            self.assertTrue(document["provider"]["listing"]["url"].startswith("http://box.lan:9000/v1/"))


class PresetPickerTests(test_cli.OperatorCommandCase):
    def entries(self, **kwargs):
        with shipped_presets():
            return setup_providers.picker_entries(self.runtime, **kwargs)

    def test_shipped_build_offers_keyed_endpoint_and_presets(self) -> None:
        gateway = catalog.load_catalog(SHIPPED_ROOT).docs["gateway"]
        self.assertTrue(catalog.keyed_compat_audited(gateway))
        self.runtime.catalog.docs["gateway"] = gateway
        presets = {f"preset:{name}" for name, info in shipped().items()
                   if info.kind == operator_mod.KEYED_KIND}
        self.assertTrue(presets)
        for only in (None, "other"):
            with self.subTest(picker=only):
                entries = {entry.id: entry for entry in self.entries(only=only)}
                for entry_id in presets | {"other:openai-compatible"}:
                    entry = entries[entry_id]
                    self.assertTrue(entry.available, entry_id)
                    self.assertEqual(entry.note, "")
                    if entry_id in presets:
                        self.assertEqual(entry.state_text, texts.PRESET_STATE)

    def test_the_picker_lists_every_preset(self) -> None:
        infos = shipped()
        for open_ in (False, True):
            with self.subTest(openai_compatible_open=open_), keyed_route(open_):
                entries = self.entries()
                by_id = {entry.id: entry for entry in entries}
                for name, info in sorted(infos.items()):
                    entry = by_id[f"preset:{name}" if info.keyed else f"other:lan:{name}"]
                    self.assertEqual(entry.kind, "preset" if info.keyed else "lan")
                    if not info.generic:
                        self.assertIn(info.display, entry.label)
                    if entry.available and not info.generic:
                        self.assertEqual(entry.state_text, texts.PRESET_STATE)
                # Shipped providers first, then the presets, then "Other" (LAN servers last).
                kinds = [entry.kind for entry in entries]
                presets = [index for index, kind in enumerate(kinds) if kind == "preset"]
                self.assertLess(max(i for i, kind in enumerate(kinds) if kind in ("account", "api-key")),
                                min(presets))
                self.assertLess(max(presets), min(i for i, entry in enumerate(entries) if entry.group == "other"))
                # Anthropic-compatible presets come before OpenAI-compatible ones.
                order = [infos[entries[i].id.split(":", 1)[1]].kind for i in presets]
                self.assertEqual(order, sorted(order, key=KEYED_KINDS.index))

    def test_openai_compatible_presets_follow_the_keyed_route(self) -> None:
        infos = shipped()
        openai_compatible = {f"preset:{name}" for name, info in infos.items() if info.kind == operator_mod.KEYED_KIND}
        anthropic = {f"preset:{name}" for name, info in infos.items() if info.kind == "anthropic-compatible"}
        self.assertTrue(openai_compatible and anthropic)
        with keyed_route(False):
            by_id = {entry.id: entry for entry in self.entries()}
        for entry_id in sorted(openai_compatible):
            with self.subTest(closed=entry_id):
                self.assertFalse(by_id[entry_id].available)
                self.assertEqual(by_id[entry_id].note, texts.OPENAI_COMPAT_CLOSED_NOTE)
                self.assertFalse(by_id["other:openai-compatible"].available)
        for entry_id in sorted(anthropic):
            self.assertTrue(by_id[entry_id].available)
        # Reopened: every OpenAI-compatible preset is offered with no other change.
        with keyed_route(True):
            by_id = {entry.id: entry for entry in self.entries()}
        for entry_id in sorted(openai_compatible | anthropic):
            with self.subTest(open=entry_id):
                self.assertTrue(by_id[entry_id].available)
                self.assertEqual((by_id[entry_id].note, by_id[entry_id].state_text), ("", texts.PRESET_STATE))

    def test_adding_your_own_lists_the_keyed_presets_then_other(self) -> None:
        # Providers N: the keyed presets exactly as Get started lists them
        # (OpenAI-compatible ones closed while that route is), then "Other";
        # no shipped provider and none of yours.
        infos = shipped()
        keyed = {f"preset:{name}" for name, info in infos.items() if info.keyed}
        for open_ in (False, True):
            with self.subTest(openai_compatible_open=open_), keyed_route(open_):
                other = list(self.entries(only="other"))
                full = [entry for entry in self.entries() if entry.kind == "preset"]
                presets = [entry for entry in other if entry.kind == "preset"]
                self.assertEqual(presets, full)
                self.assertEqual({entry.id for entry in presets}, keyed)
                for entry in presets:
                    info = infos[entry.id.split(":", 1)[1]]
                    if info.kind == operator_mod.KEYED_KIND:
                        self.assertIs(entry.available, open_, entry.id)
                        self.assertEqual(entry.note, "" if open_ else texts.OPENAI_COMPAT_CLOSED_NOTE)
                    else:
                        self.assertTrue(entry.available, entry.id)
                rest = [entry for entry in other if entry.kind != "preset"]
                self.assertTrue(rest and all(entry.group == "other" for entry in rest))
                self.assertFalse([entry for entry in other if entry.kind in ("account", "api-key", "own")])
                self.assertLess(max(other.index(entry) for entry in presets),
                                min(other.index(entry) for entry in rest))
        other = [entry for entry in self.entries(only="other") if entry.kind != "preset"]
        lan = [name for name, info in shipped().items() if not info.keyed]
        self.assertEqual(sorted(entry.id.split(":", 2)[2] for entry in other if entry.kind == "lan"), sorted(lan))
        generic = [entry for entry in other if entry.kind == "lan" and entry.label == texts.PICKER_LABELS["other:lan"]]
        self.assertEqual(len(generic), 1)
        self.assertEqual(generic[0], next(entry for entry in other if entry.kind == "lan"), "the generic one first")


class KeyedEndpointOfferingTests(test_cli.OperatorCommandCase):
    def test_generic_platform_refusal_points_to_the_dedicated_route(self) -> None:
        for url in ("https://api.openai.com/v1", "https://API.OPENAI.COM:443/anything"):
            with self.subTest(url=url):
                origin, _ = operator_mod.normalize_endpoint(url, keyed=True, lan=False)
                problem = catalog.platform_host_problem(origin)
                self.assertIn("not a generic openai-compatible route", problem)
                self.assertIn("use the OpenAI Platform API-key route", problem)
                self.assertNotIn("stays closed", problem)

    def test_keyed_endpoint_forms_offer_only_bearer(self) -> None:
        from claude_multi import views

        create = views.endpoint_form_fields(operator_mod.KEYED_KIND)
        edit = views.provider_edit_form_fields({"kind": operator_mod.KEYED_KIND,
                                               "auth": {"kind": "bearer"}})
        for fields in (create, edit):
            auth, = [field for field in fields if field[0] == "auth"]
            self.assertEqual(auth[2], "bearer")
            self.assertEqual([choice[0] for choice in auth[3]], ["bearer"])
        anthropic = views.endpoint_form_fields("anthropic-compatible")
        auth, = [field for field in anthropic if field[0] == "auth"]
        self.assertEqual(auth[2], "header")
        self.assertEqual([choice[0] for choice in auth[3]], ["header", "bearer"])

    def test_keyed_cli_endpoint_never_offers_header_auth(self) -> None:
        from types import SimpleNamespace
        from claude_multi.cli.commands import setup as setup_cmd

        for kind in (operator_mod.KEYED_KIND, "anthropic-compatible"):
            prompts = []
            def ask(prompt, _stream, default=""):
                prompts.append(prompt)
                return default
            plan = SimpleNamespace(lines=())
            with self.subTest(kind=kind), mock.patch.object(consent, "require_human"), \
                    mock.patch.object(setup_cmd, "_ask", side_effect=ask), \
                    mock.patch.object(setup_providers, "plan_add_endpoint", return_value=plan) as planned, \
                    mock.patch.object(consent, "confirm", return_value=False), self.assertRaises(model.Declined):
                setup_cmd._connect_endpoint(self.runtime, kind, input_stream=None, write=lambda _line: None)
            values = planned.call_args.args[1]
            self.assertEqual(values["auth"], "bearer" if kind == operator_mod.KEYED_KIND else "header")
            auth_prompts = [prompt for prompt in prompts if "How the key is sent" in prompt]
            self.assertEqual(auth_prompts, [] if kind == operator_mod.KEYED_KIND
                             else ["How the key is sent (header or bearer)"])


class PresetPlanTests(test_cli.OperatorCommandCase):
    def human(self):
        return mock.patch.object(consent, "stdio_ttys", return_value=True)

    def store(self) -> secret_store.FileSecretStore:
        return secret_store.default_store(self.runtime.gateway_environ())

    def test_a_keyed_preset_is_your_own_endpoint_with_its_reviewed_facts(self) -> None:
        name = first("anthropic-compatible")
        info = shipped()[name]
        with shipped_presets(), self.human():
            plan = setup_providers.plan_add_preset(self.runtime, name, "mine", None)
            self.assertIsInstance(plan, setup_providers.EndpointPlan)
            self.assertEqual((plan.consent, plan.guarded, plan.preset), ("approve-route", True, name))
            self.assertEqual((plan.kind, plan.secret_name), (info.kind, info.secret_name))
            self.assertEqual(plan.lines[0], texts.PRESET_PREVIEW_HEAD.format(display=info.display))
            self.assertIn(f"The key {info.secret_name} is sent as", "\n".join(plan.lines))
            applied = setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan),
                                                       model.Secret("preset-dummy-key"))
        self.assertTrue(applied.lines[0].startswith("Added mine"), applied.lines)
        document = json.loads((self.pdir / "mine.json").read_text())
        self.assertNotIn(operator_mod.PRESET_BLOCK, document)
        self.assertEqual(document["provider"]["base_url"], info.base_url)
        self.assertEqual(document["provider"]["display"], info.display)
        self.assertEqual(self.ledger().routes["mine"]["origin"],
                         operator_mod.normalize_endpoint(info.base_url, keyed=True, lan=False)[0])
        self.assertEqual(self.store().get(info.secret_name), "preset-dummy-key")
        with shipped_presets(), self.assertRaisesRegex(model.Refused, "exists — choose another name"):
            setup_providers.plan_add_preset(self.runtime, name, "mine", None)
        with shipped_presets(), self.assertRaisesRegex(model.Refused, "ships with claude-multi"):
            setup_providers.plan_add_preset(self.runtime, name, "kimi", None)

    def test_an_openai_compatible_preset_waits_for_its_route(self) -> None:
        name = first(operator_mod.KEYED_KIND)
        info = shipped()[name]
        before = self.state_bytes()
        with shipped_presets(), self.human(), keyed_route(False):
            with self.assertRaises(model.Refused) as caught:
                setup_providers.plan_add_preset(self.runtime, name, name, None)
        self.assertEqual(str(caught.exception), texts.OPENAI_COMPAT_CLOSED)
        self.assertEqual(self.state_bytes(), before)
        with shipped_presets(), self.human(), keyed_route(True):
            plan = setup_providers.plan_add_preset(self.runtime, name, name, None)
            self.assertEqual((plan.kind, plan.auth, plan.secret_name, plan.listing),
                             (operator_mod.KEYED_KIND, "bearer", info.secret_name, info.listing_url))
            setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan),
                                             model.Secret("preset-dummy-key"))
        self.assertEqual(json.loads((self.pdir / f"{name}.json").read_text())["provider"]["kind"],
                         operator_mod.KEYED_KIND)
        self.assertIn(name, self.ledger().routes)

    def connected(self, provider_id: str) -> bool:
        from claude_multi.setup import status

        return any(conn.provider_id == provider_id for conn in status.connected(self.runtime))

    def first_instance(self, name: str, provider_id: str, key: str) -> None:
        """An existing, active provider from preset ``name``: declared, its route approved, its key saved."""

        plan = setup_providers.plan_add_preset(self.runtime, name, provider_id, None)
        setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan), model.Secret(key))
        self.assertTrue(self.connected(provider_id))

    def test_two_instances_of_one_preset_keep_the_first_credential(self) -> None:
        name = first("anthropic-compatible")
        shared = shipped()[name].secret_name
        with shipped_presets(), self.human():
            for provider_id, key in (("first", "first-instance-key"), ("second", "second-instance-key")):
                plan = setup_providers.plan_add_preset(self.runtime, name, provider_id, None)
                setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan),
                                                 model.Secret(key))
        self.assertEqual(self.store().get(shared), "first-instance-key")
        second = json.loads((self.pdir / "second.json").read_text())["provider"]["auth"]["secret_ref"]
        self.assertNotEqual(second, f"env:{shared}")
        self.assertEqual(self.store().get(second.removeprefix("env:")), "second-instance-key")

    def test_one_more_instance_of_a_preset_never_replaces_the_key_in_use(self) -> None:
        name = first("anthropic-compatible")
        shared = shipped()[name].secret_name
        own = f"{shared}_SECOND"
        with shipped_presets(), self.human():
            self.first_instance(name, "first", "first-instance-key")
            plan = setup_providers.plan_add_preset(self.runtime, name, "second", None)
            # The default: a key of its own, under a name of its own; the saved one is untouched.
            self.assertEqual((plan.secret_name, plan.key_use, plan.shared_with), (own, setup_providers.KEY_OWN, ()))
            self.assertEqual((plan.preset_secret, plan.preset_sharers), (shared, ("first",)))
            self.assertEqual((plan.key_present, plan.key_length), (False, None))
            self.assertIn(texts.PRESET_KEY_OWN.format(id="second", name=own, preset_name=shared, providers="first"),
                          plan.lines)
            setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan),
                                             model.Secret("second-instance-key"))
        self.assertEqual(self.store().get(shared), "first-instance-key")
        self.assertEqual(self.store().get(own), "second-instance-key")
        declared = json.loads((self.pdir / "second.json").read_text())
        self.assertEqual(declared["provider"]["auth"]["secret_ref"], f"env:{own}")
        self.assertTrue(self.connected("first") and self.connected("second"))

    def test_sharing_or_replacing_the_saved_key_is_explicit(self) -> None:
        name = first("anthropic-compatible")
        shared = shipped()[name].secret_name
        with shipped_presets(), self.human():
            self.first_instance(name, "first", "first-instance-key")
            # Sharing the saved key: no key is typed, and none is accepted.
            plan = setup_providers.plan_add_preset(self.runtime, name, "second", None, key=setup_providers.KEY_REUSE)
            self.assertEqual((plan.secret_name, plan.shared_with), (shared, ("first",)))
            self.assertEqual((plan.key_present, plan.key_length), (True, len("first-instance-key")))
            with self.assertRaisesRegex(model.Refused, "takes no new key"):
                setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan),
                                                 model.Secret("not-taken-key"))
            setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan), None)
            self.assertEqual(self.store().get(shared), "first-instance-key")
            self.assertTrue(self.connected("second"))
            # Replacing it names every provider using it and needs the new key.
            plan = setup_providers.plan_add_preset(self.runtime, name, "third", None,
                                                   key=setup_providers.KEY_REPLACE)
            self.assertEqual(plan.shared_with, ("first", "second"))
            self.assertIn(texts.PRESET_KEY_REPLACE.format(name=shared, providers="first, second"), plan.lines)
            with self.assertRaisesRegex(model.Refused, "needs the new key"):
                setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan), None)
            # A key changed after the plan was shown refuses the replacement as stale.
            changed = setup_providers.plan_set_key(self.runtime, "first")
            setup_providers.apply_set_key(self.runtime, changed, model.Confirmation.given(changed),
                                          model.Secret("first-changed-key"))
            with self.assertRaises(model.Stale):
                setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan),
                                                 model.Secret("replacement-key"))
            self.assertEqual(self.store().get(shared), "first-changed-key")
            plan = setup_providers.plan_add_preset(self.runtime, name, "third", None,
                                                   key=setup_providers.KEY_REPLACE)
            setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan),
                                             model.Secret("replacement-key"))
        self.assertEqual(self.store().get(shared), "replacement-key")

    def test_sharing_needs_a_key_another_provider_uses(self) -> None:
        name = first("anthropic-compatible")
        shared = shipped()[name].secret_name
        with shipped_presets(), self.human():
            for key in (setup_providers.KEY_REUSE, setup_providers.KEY_REPLACE):
                with self.subTest(key=key), self.assertRaises(model.Refused) as caught:
                    setup_providers.plan_add_preset(self.runtime, name, "alone", None, key=key)
                self.assertEqual(str(caught.exception), texts.PRESET_KEY_NOT_SHARED.format(name=shared, id="alone"))
            self.first_instance(name, "first", "first-instance-key")
            # A name too long to carry its own key name is refused, never shared silently.
            long_id = "x" + "-long" * 12
            with self.assertRaisesRegex(model.Refused, "needs its own API key name"):
                setup_providers.plan_add_preset(self.runtime, name, long_id, None)
        self.assertEqual(self.store().get(shared), "first-instance-key")

    def test_a_custom_json_provider_using_the_preset_key_is_a_sharer(self) -> None:
        """custom.json still in effect counts like any provider: the new one
        gets a key of its own by default, and sharing names the legacy one."""

        from claude_multi import custom

        name = first("anthropic-compatible")
        info = shipped()[name]
        shared = info.secret_name
        registry = custom.registry_path(self.runtime.environ)
        state.ensure_private_dir(registry.parent)
        state.atomic_write(registry, strict_json.pretty_file_bytes({
            "version": 1, "models": {},
            "providers": {"legacy-p": {"base_url": info.base_url, "auth_kind": "header", "secret_env": shared}}}))
        with shipped_presets(), self.human():
            plan = setup_providers.plan_add_preset(self.runtime, name, "studio", None)
            self.assertEqual((plan.preset_secret, plan.preset_sharers), (shared, ("legacy-p",)))
            self.assertEqual((plan.secret_name, plan.key_use), (f"{shared}_STUDIO", setup_providers.KEY_OWN))
            replace = setup_providers.plan_add_preset(self.runtime, name, "studio", None,
                                                      key=setup_providers.KEY_REPLACE)
        self.assertEqual((replace.secret_name, replace.shared_with), (shared, ("legacy-p",)))
        self.assertIn(texts.PRESET_KEY_REPLACE.format(name=shared, providers="legacy-p"), replace.lines)

    def test_a_server_preset_declares_without_a_key(self) -> None:
        name = first(operator_mod.LAN_KIND)
        with shipped_presets():
            plan = setup_providers.plan_add_preset(self.runtime, name, "homebox", None)
            self.assertIsInstance(plan, setup_providers.PresetPlan)
            setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan))
        document = json.loads((self.pdir / "homebox.json").read_text())
        self.assertEqual(document["provider"]["auth"], {"kind": "none"})
        self.assertNotIn(operator_mod.PRESET_BLOCK, document)


class PresetMenuTests(test_setup_cli.SetupCase):
    def test_the_setup_menu_adds_a_preset_with_its_key(self) -> None:
        name = first("anthropic-compatible")
        info = shipped()[name]
        with shipped_presets():
            number = self.menu_number(f"preset:{name}")
            code, out, err = self.op(["setup", "--step", "providers"], f"{number}\n\ny\npreset-menu-key\n\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn(texts.PRESET_PREVIEW_HEAD.format(display=info.display), out)
        self.assertIn(f"Added {name}", out)
        self.assertIn(f"claude-multi models add {name} WIRE", out)
        self.assertNotIn("preset-menu-key", out + err)
        self.assertEqual(secret_store.default_store(self.runtime.gateway_environ()).get(info.secret_name),
                         "preset-menu-key")
        self.assertIn(name, self.ledger().routes)

    def store(self) -> secret_store.FileSecretStore:
        return secret_store.default_store(self.runtime.gateway_environ())

    def test_one_more_instance_from_the_menu_gets_its_own_key(self) -> None:
        name = first("anthropic-compatible")
        shared = shipped()[name].secret_name
        own = f"{shared}_SECOND"
        with shipped_presets():
            code, out, err = self.op(["setup", "--step", "providers"],
                                     f"{self.menu_number(f'preset:{name}')}\n\ny\nfirst-menu-key\n\n")
            self.assertEqual(code, 0, out + err)
            number = self.menu_number(f"preset:{name}")
            # The default answer to which key: its own.
            code, out, err = self.op(["setup", "--step", "providers"], f"{number}\nsecond\n\ny\nsecond-menu-key\n\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn(texts.PRESET_KEY_SHARED.format(name=shared, providers=name), out)
        self.assertIn(texts.PRESET_KEY_CHOICES[0].format(id="second", own=own), out)
        self.assertIn(texts.PRESET_KEY_CHOICES[2].format(name=shared, providers=name), out)
        self.assertNotIn("menu-key", out + err)
        self.assertEqual(self.store().get(shared), "first-menu-key")
        self.assertEqual(self.store().get(own), "second-menu-key")
        self.assertEqual(self.runtime.lineup_catalog().providers["second"]["transport"]["auth"]["secret_ref"],
                         f"env:{own}")

    def test_replacing_a_shared_key_from_the_menu_is_confirmed_by_name(self) -> None:
        name = first("anthropic-compatible")
        shared = shipped()[name].secret_name
        with shipped_presets():
            code, out, err = self.op(["setup", "--step", "providers"],
                                     f"{self.menu_number(f'preset:{name}')}\n\ny\nfirst-menu-key\n\n")
            self.assertEqual(code, 0, out + err)
            number = self.menu_number(f"preset:{name}")
            # 3: replace the saved key; approve the route, then no to the replacement.
            code, out, err = self.op(["setup", "--step", "providers"], f"{number}\nthird\n3\ny\nn\n\n")
            self.assertEqual(code, 3, out + err)
            self.assertIn(texts.PRESET_KEY_REPLACE_QUESTION.format(name=shared, providers=name), out + err)
            self.assertNotIn("third", self.runtime.lineup_catalog().providers)
            self.assertEqual(self.store().get(shared), "first-menu-key")
            # 2: share the saved key; no key is asked for.
            code, out, err = self.op(["setup", "--step", "providers"], f"{number}\nfourth\n2\ny\n\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn(texts.PRESET_KEY_REUSE.format(id="fourth", name=shared, providers=name), out)
        self.assertEqual(self.runtime.lineup_catalog().providers["fourth"]["transport"]["auth"]["secret_ref"],
                         f"env:{shared}")
        self.assertEqual(self.store().get(shared), "first-menu-key")

    def test_saying_no_writes_nothing(self) -> None:
        name = first("anthropic-compatible")
        before = self.state_bytes()
        with shipped_presets():
            number = self.menu_number(f"preset:{name}")
            code, out, err = self.op(["setup", "--step", "providers"], f"{number}\n\nn\n\n")
        self.assertEqual(code, 3, out + err)
        self.assertEqual(self.state_bytes(), before)


class PresetCommandKeyTests(test_cli.OperatorCommandCase):
    """``providers add --preset`` when another provider already uses the
    preset's API key: its own key name by default, ``--reuse-key`` shares
    the saved key, ``--replace-key`` replaces it after a y/N naming every
    provider that uses it."""

    def store(self) -> secret_store.FileSecretStore:
        return secret_store.default_store(self.runtime.gateway_environ())

    def key_file(self, label: str, value: str) -> str:
        path = self.root / f"{label}.key"
        state.atomic_write(path, f"{value}\n".encode())
        return str(path)

    def add(self, name: str, provider_id: str, *flags: str, answers: str = "y\n") -> tuple[int, str, str]:
        return self.op(["providers", "add", "--preset", name, "--as", provider_id, *flags], answers)

    def secret_ref(self, provider_id: str) -> str:
        return json.loads((self.pdir / f"{provider_id}.json").read_text())["provider"]["auth"]["secret_ref"]

    def test_one_more_instance_keeps_the_first_key(self) -> None:
        name = first("anthropic-compatible")
        shared = shipped()[name].secret_name
        own = f"{shared}_SECOND"
        with shipped_presets():
            code, out, err = self.add(name, "first", "--secret-file", self.key_file("first", "first-cli-key"))
            self.assertEqual(code, 0, out + err)
            code, out, err = self.add(name, "second", "--secret-file", self.key_file("second", "second-cli-key"))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.store().get(shared), "first-cli-key")
        self.assertEqual((self.secret_ref("first"), self.secret_ref("second")), (f"env:{shared}", f"env:{own}"))
        self.assertEqual(self.store().get(own), "second-cli-key")
        self.assertIn(texts.PRESET_KEY_OWN.format(id="second", name=own, preset_name=shared, providers="first"), out)
        self.assertIn(f"stored {own} (", out)
        self.assertNotIn("cli-key", out + err)

    def test_reuse_and_replace_are_explicit(self) -> None:
        name = first("anthropic-compatible")
        shared = shipped()[name].secret_name
        with shipped_presets():
            code, out, err = self.add(name, "first", "--secret-file", self.key_file("first", "first-cli-key"))
            self.assertEqual(code, 0, out + err)
            code, out, err = self.add(name, "second", "--reuse-key")
            self.assertEqual(code, 0, out + err)
            self.assertIn(texts.PRESET_KEY_REUSE.format(id="second", name=shared, providers="first"), out)
            self.assertEqual(self.secret_ref("second"), f"env:{shared}")
            self.assertEqual(self.store().get(shared), "first-cli-key")
            # Saying no to the replacement (Cancel is the default) writes nothing.
            replacement = self.key_file("third", "replacement-cli-key")
            before = self.state_bytes()
            code, out, err = self.add(name, "third", "--replace-key", "--secret-file", replacement, answers="y\n\n")
            self.assertEqual(code, 1, out + err)
            self.assertIn(texts.PRESET_KEY_REPLACE_QUESTION.format(name=shared, providers="first, second"),
                          out + err)
            self.assertEqual(self.state_bytes(), before)
            self.assertEqual(self.store().get(shared), "first-cli-key")
            code, out, err = self.add(name, "third", "--replace-key", "--secret-file", replacement,
                                      answers="y\ny\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"the key of first, second too", out)
        self.assertNotIn("replacement-cli-key", out + err)
        self.assertEqual(self.store().get(shared), "replacement-cli-key")

    def test_sharing_flags_need_a_shared_key(self) -> None:
        name = first("anthropic-compatible")
        shared = shipped()[name].secret_name
        before = self.state_bytes()
        with shipped_presets():
            code, out, err = self.add(name, "alone", "--reuse-key")
            self.assertEqual(code, 1, out + err)
            self.assertIn(texts.PRESET_KEY_NOT_SHARED.format(name=shared, id="alone"), out + err)
            for flags in (("--reuse-key", "--secret-file", self.key_file("x", "unused-key")),
                          ("--replace-key",), ("--replace-key", "--declare-only")):
                with self.subTest(flags=flags):
                    code, out, err = self.add(name, "alone", *flags)
                    self.assertEqual(code, 2, out + err)
        self.assertEqual(self.state_bytes(), before)


class SharedKeySetKeyTests(test_cli.OperatorCommandCase):
    """``providers set-key`` on a key two providers share (one more provider
    from a preset that shares the saved key): the question names every
    provider using it and its default is No, so a declined replacement keeps
    the key of both; ``--yes`` replaces it without the question, naming them;
    a provider that starts using the key after the plan refuses the apply as
    stale."""

    FIRST_KEY = "shared-first-key"

    def setUp(self) -> None:
        super().setUp()
        patcher = shipped_presets()
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        self.name = first("anthropic-compatible")
        self.info = shipped()[self.name]
        self.shared = self.info.secret_name
        with self.human():
            self.add_instance("first", value=self.FIRST_KEY)
            self.add_instance("second", key=setup_providers.KEY_REUSE)

    def human(self):
        return mock.patch.object(consent, "stdio_ttys", return_value=True)

    def add_instance(self, provider_id: str, *, key: str = setup_providers.KEY_OWN, value: str | None = None) -> None:
        plan = setup_providers.plan_add_preset(self.runtime, self.name, provider_id, None, key=key)
        setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan),
                                         model.Secret(value) if value is not None else None)

    def store(self) -> secret_store.FileSecretStore:
        return secret_store.default_store(self.runtime.gateway_environ())

    def question(self) -> str:
        return texts.KEY_SHARED_REPLACE_PROMPT.format(name=self.shared, n=len(self.FIRST_KEY),
                                                      providers="first, second")

    def test_both_providers_use_one_saved_key(self) -> None:
        for provider_id in ("first", "second"):
            self.assertEqual(self.runtime.lineup_catalog().providers[provider_id]["transport"]["auth"]["secret_ref"],
                             f"env:{self.shared}")
        self.assertEqual(self.store().get(self.shared), self.FIRST_KEY)

    def test_a_declined_replacement_keeps_the_key_of_both(self) -> None:
        before = self.state_bytes()
        for provider_id, answer in (("second", "\n"), ("first", "n\n")):
            with self.subTest(provider=provider_id):
                code, out, err = self.op(["providers", "set-key", provider_id], answer + "never-typed-key\n")
                self.assertEqual(code, 3, out + err)
                self.assertIn(self.question(), err)
                self.assertIn(texts.KEY_KEPT.format(display=self.info.display), out)
                self.assertEqual(self.store().get(self.shared), self.FIRST_KEY)
                self.assertEqual(self.state_bytes(), before)

    def test_yes_replaces_it_for_both_and_names_them(self) -> None:
        code, out, err = self.op(["providers", "set-key", "first", "--yes"], "shared-yes-key\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn(texts.KEY_SHARED_REPLACED_NOTE.format(name=self.shared, providers="first, second"), err)
        self.assertNotIn("[y/N]", err)
        self.assertNotIn("shared-yes-key", out + err)
        self.assertEqual(self.store().get(self.shared), "shared-yes-key")
        # An explicit yes replaces it too.
        code, out, err = self.op(["providers", "set-key", "second"], "y\nshared-new-key\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn(texts.KEY_SHARED_REPLACE_PROMPT.format(name=self.shared, n=len("shared-yes-key"),
                                                             providers="first, second"), err)
        self.assertEqual(self.store().get(self.shared), "shared-new-key")

    def test_the_plan_names_the_others_and_a_new_sharer_is_stale(self) -> None:
        with self.human():
            plan = setup_providers.plan_set_key(self.runtime, "first")
            self.assertEqual((plan.shared_with, plan.key_users, plan.replaces_shared),
                             (("second",), "first, second", True))
            self.assertEqual(plan.lines, (self.question().rstrip(),))
            # The providers using the key differ under the locks from the ones named.
            with mock.patch.object(setup_providers, "key_sharers", return_value=("second", "third")), \
                    self.assertRaises(model.Stale) as caught:
                setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                              model.Secret("shared-stale-key"))
            self.assertEqual(caught.exception.what, texts.KEY_USERS_WHAT.format(name=self.shared))
            # A third provider really sharing it after the plan was shown: refused, nothing written.
            self.add_instance("third", key=setup_providers.KEY_REUSE)
            with self.assertRaises(model.SetupError):
                setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                              model.Secret("shared-stale-key"))
        self.assertEqual(self.store().get(self.shared), self.FIRST_KEY)

    def remove_first(self) -> None:
        """``providers rm first --yes``, through the API that verb runs (the
        key both use stays saved)."""

        removal = setup_providers.plan_remove_provider(self.runtime, "first")
        self.assertEqual(removal.blockers, ())
        setup_providers.apply_remove_provider(self.runtime, removal, model.Confirmation.given(removal))
        self.assertFalse((self.pdir / "first.json").exists())

    def test_a_target_removed_while_the_plan_is_made_refuses_the_apply(self) -> None:
        """``first`` removed after its plan read who uses the key but before
        the plan's served preview: the plan's sample predates the removal, so
        the apply refuses and the key ``second`` uses is kept byte for byte."""

        real = providers_cmd.served_preflight
        raced: list[str] = []

        def racing(runtime, verb, **kwargs):
            if verb == "providers set-key first" and not raced:
                raced.append(verb)
                self.remove_first()
            return real(runtime, verb, **kwargs)

        with self.human():
            with mock.patch.object(providers_cmd, "served_preflight", side_effect=racing):
                plan = setup_providers.plan_set_key(self.runtime, "first")
            self.assertEqual((raced, plan.shared_with), (["providers set-key first"], ("second",)))
            before = self.secret_file.read_bytes()
            with self.assertRaises(model.SetupError) as caught:
                setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                              model.Secret("never-written-key"))
        self.assertIn(served_plan.CHANGED_REFUSAL, str(caught.exception))
        self.assertEqual(self.secret_file.read_bytes(), before)
        self.assertEqual(self.store().get(self.shared), self.FIRST_KEY)

    def test_a_removed_target_is_stale_under_the_locks(self) -> None:
        """The apply resolves its provider again under the transaction's
        locks: ``first`` removed after the plan (the served preview taken
        again after the removal, so only that check can see it) refuses as
        stale, value-free, and the key ``second`` uses is kept byte for
        byte."""

        with self.human():
            plan = setup_providers.plan_set_key(self.runtime, "first")
            self.assertEqual(plan.shared_with, ("second",))
            self.remove_first()
            served = setup_providers.preflight(self.runtime, "providers set-key first",
                                               assume_present=(self.shared,))
            plan = dataclasses.replace(plan, served=served)
            before = self.secret_file.read_bytes()
            with self.assertRaises(model.Stale) as caught:
                setup_providers.apply_set_key(self.runtime, plan, model.Confirmation.given(plan),
                                              model.Secret("never-written-key"))
        self.assertEqual(caught.exception.what, texts.KEY_TARGET_WHAT.format(id="first", name=self.shared))
        for value in (self.FIRST_KEY, "never-written-key"):
            self.assertNotIn(value, str(caught.exception))
        self.assertEqual(self.secret_file.read_bytes(), before)
        self.assertEqual(self.store().get(self.shared), self.FIRST_KEY)
        # The provider left with the key still uses it.
        with self.human():
            self.assertEqual(setup_providers.plan_set_key(self.runtime, "second").shared_with, ())

    def test_a_key_nobody_else_uses_asks_as_before(self) -> None:
        with self.human():
            plan = setup_providers.plan_set_key(self.runtime, "kimi")
        self.assertEqual((plan.shared_with, plan.replaces_shared), ((), False))
        code, out, err = self.op(["providers", "set-key", "kimi"], "n\n")
        self.assertEqual(code, 3, out + err)
        self.assertIn(texts.KEY_REPLACE_PROMPT.format(display=plan.display, n=plan.current_length), err)


class SharedKeyAnswersTests(test_setup_cli.SetupCase):
    def test_the_answers_preview_names_every_provider_using_the_key(self) -> None:
        name = first("anthropic-compatible")
        shared = shipped()[name].secret_name
        with shipped_presets():
            with mock.patch.object(consent, "stdio_ttys", return_value=True):
                plan = setup_providers.plan_add_preset(self.runtime, name, "first", None)
                setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan),
                                                 model.Secret("shared-answers-first"))
                plan = setup_providers.plan_add_preset(self.runtime, name, "second", None,
                                                       key=setup_providers.KEY_REUSE)
                setup_providers.apply_add_preset(self.runtime, plan, model.Confirmation.given(plan), None)
            key = self.private_file("keys/shared.txt", b"shared-answers-new\n")
            path = self.answers({"version": 1, "providers": [{"id": "second", "api_key_file": str(key)}]})
            code, out, err = self.op(["setup", "--answers", str(path)], "n\n")
        self.assertEqual(code, 3, out + err)
        self.assertIn(texts.ANSWERS_KEY_SHARED.format(name=shared, providers="first, second"), out)
        self.assertNotIn("shared-answers", out + err)
        self.assertEqual(secret_store.default_store(self.runtime.gateway_environ()).get(shared),
                         "shared-answers-first")


class SingletonPresetTests(test_cli.OperatorCommandCase):
    """One preset alone, with one model added and admitted, is a usable
    profile: no other key, no sign-in, every other provider off."""

    def test_a_preset_alone_reaches_a_usable_profile(self) -> None:
        state.atomic_write(self.secret_file, b"")
        for pid in sorted(self.runtime.catalog.docs["providers"]["providers"]):
            code, _out, err = self.op(["providers", "disable", pid])
            self.assertEqual(code, 0, err)
        plan, _warnings = self.runtime.starter_plan("alone")
        self.assertIsNone(plan.document, "nothing is usable before the preset")
        name = first("anthropic-compatible")
        key_file = self.root / "preset.key"
        state.atomic_write(key_file, b"preset-singleton-key\n")
        with shipped_presets():
            code, out, err = self.op(["providers", "add", "--preset", name, "--as", name, "--secret-file",
                                      str(key_file)], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"declared provider {name} from preset {name}", out)
        self.assertNotIn("preset-singleton-key", out + err)
        key = f"custom-{name}-fixture"
        code, _out, err = self.op(["models", "add", name, "vendor-fixture-1", "--as", key, "--context", "200000",
                                   "--source", "operator", "--effort", "high"])
        self.assertEqual(code, 0, err)
        self.serve_current()
        code, out, err = self.op(["models", "admit", key], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.calls, [key])
        plan, _warnings = self.runtime.starter_plan("alone")
        self.assertIsNotNone(plan.document, plan.refusal)
        self.assertEqual(plan.document["lead"]["model"], key)
        lines = self.runtime.lineup_catalog().lines
        for slot in plan.document.get("agents", {}).values():
            self.assertEqual(lines[slot["model"]]["provider"], name)
        code, out = self.run_cli(["profile", "starter", "--name", "alone", "--apply"], "y\n")
        self.assertEqual(code, 0, out)
        saved = self.runtime.profiles.load("alone")
        evaluation = profile.evaluate(saved, self.runtime.lineup_catalog(), bindings={},
                                      effective=self.runtime.current_effective())
        self.assertEqual(evaluation.errors, ())
        self.assertIsNotNone(evaluation.lineup)


if __name__ == "__main__":
    unittest.main()
