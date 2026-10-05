"""The OpenAI API-key route: the account provider's key transport rendered
into the gateway's Responses (codex) key channel.

Covers the reviewed model list in the catalog (``key_route``), the
channel-aware render (never the Claude key channel, never an empty model
list, never a fallback to saved ChatGPT accounts), the served identity,
doctor, the key service and its verbs, the answers document, and key
material never echoed in a diagnostic: fixture catalog and gateway seams.
The pinned-gateway classes run the real render in the isolated gateway
harness against a fake Responses upstream (no provider request), including
one that quotes the key in its failures: the client gets fixed local text,
and the request carries no account client identity. One client turn is
one Responses request (no retry when it succeeds), the client's output cap
is not passed on, and no hosted image tool is added while the key is
selected (the rendered image-generation setting, compared with a render
without it).
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, operator, render, secret_store, served_plan, strict_json
from claude_multi import validate as schema_validate
import _gateway_harness as harness
from claude_multi.setup import model, providers as setup_providers, texts
import claude_multi.cli.consent as consent
import claude_multi.proxy
import test_setup_cli
from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog

KEY = ("openai", operator.TRANSPORT_API_KEY)
SECRET = operator.transport_alternative(*KEY).secret_name
DUMMY = "openai-dummy-platform-key-value"
OPERATOR_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "operator"
STAMP = "2026-10-03T00:00:00Z"


def opened():
    """The route offered, as this build's descriptor states it (kept as a
    patch so a test also holds when the descriptor is closed again)."""

    alternative = operator.TRANSPORT_ALTERNATIVES[KEY]
    return mock.patch.dict(operator.TRANSPORT_ALTERNATIVES,
                           {KEY: dataclasses.replace(alternative, available=True, closed_reason=None)})


def closed(reason: str = "the OpenAI API-key transport is closed in this build"):
    """The descriptor as a build that does not offer the route states it."""

    alternative = operator.TRANSPORT_ALTERNATIVES[KEY]
    return mock.patch.dict(operator.TRANSPORT_ALTERNATIVES,
                           {KEY: dataclasses.replace(alternative, available=False, closed_reason=reason)})


def openai_lines(docs) -> list[str]:
    return sorted(key for key, entry in docs["models-v2"]["models"].items() if entry["provider"] == "openai")


def reviewable(docs) -> str:
    """The fixture's lead-capable OpenAI line with per-effort selectors."""

    return next(key for key in openai_lines(docs)
                if "lead" in docs["models-v2"]["models"][key]["capabilities"]
                and isinstance(docs["models-v2"]["models"][key]["efforts"], dict))


def route_block(entry, efforts) -> dict:
    bound = entry["context"]["provider_tokens"]
    return {"efforts": list(efforts), "context_tokens": bound + 50_000, "input_tokens": bound,
            "output_tokens": 64_000, "source": "docs", "source_ref": "https://docs.example.invalid/models/line",
            "checked": "2026-09-29"}


def reviewed_docs() -> tuple[dict, str, tuple[str, ...]]:
    """Fixture docs whose lead-capable OpenAI line is reviewed for the key
    route at its first effort only (the others stay unreviewed)."""

    docs = copy.deepcopy(catalog.load_catalog(FIXTURE_ROOT).docs)
    key = reviewable(docs)
    entry = docs["models-v2"]["models"][key]
    efforts = (sorted(entry["efforts"])[0],)
    entry["key_route"] = route_block(entry, efforts)
    return docs, key, efforts


def ledger(**members) -> operator.OperatorLedger:
    document = json.loads((OPERATOR_FIXTURES / "ledger-empty.json").read_text())
    document.update(members)
    return operator.parse_ledger(strict_json.pretty_file_bytes(document), operator.load_schemas(FIXTURE_ROOT).ledger)


def selected(*, approved: bool = True) -> operator.OperatorLedger:
    route = operator.transport_route_record(operator.transport_alternative(*KEY), at=STAMP)
    if not approved:
        route["rd"] = "0" * 64
    return ledger(transport_choices={"openai": operator.TRANSPORT_API_KEY}, routes={"openai": route})


def build(plan, *, resolve=None, continuity=None):
    return render.build_config_document(
        plan.docs["gateway"], plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"],
        home=Path("/fixture/home"), gateway_tokens=("t" * 64,),
        resolve_secret=resolve or (lambda name: DUMMY if name == SECRET else f"dummy-{name.lower()}"),
        continuity=continuity or {}, captures=plan.captures, provider_headers=plan.headers,
        oauth_overlay=plan.overlay)


def selectors_at(entry, efforts) -> set[str]:
    return {operator._selector_base(selector) for level, selector, _c in catalog.line_selectors(entry)
            if level in efforts}


# ------------------------------------------------------------ the reviewed list
class KeyRouteCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.docs = copy.deepcopy(catalog.load_catalog(FIXTURE_ROOT).docs)
        self.key = reviewable(self.docs)

    def check(self, key: str, entry: dict) -> list[str]:
        docs = self.docs
        return catalog.validate_line(key, entry, providers=docs["providers"]["providers"],
                                     native_contract=docs["native-contract"], taken_selectors={})

    @uses_shipped_catalog
    def test_shipped_key_routes_fit_their_lines_and_no_route_is_empty(self) -> None:
        docs = catalog.load_catalog(SHIPPED_ROOT).docs
        providers = docs["providers"]["providers"]
        lines = docs["models-v2"]["models"]
        explicit = {alternative.provider for alternative in operator.TRANSPORT_ALTERNATIVES.values()
                    if alternative.explicit_models}
        for provider_id in sorted(explicit):
            with self.subTest(provider=provider_id):
                self.assertTrue(operator.key_route_lines(docs, provider_id))
        for key, entry in sorted(lines.items()):
            route = catalog.key_route(entry)
            if route is None:
                continue
            with self.subTest(line=key):
                transport = providers[entry["provider"]]["transport"]
                self.assertEqual((transport["kind"], transport["pool"] in catalog.KEY_ROUTE_POOLS),
                                 ("oauth-pool", True))
                self.assertLessEqual(set(route["efforts"]), set(entry["efforts"]))
                self.assertLessEqual(entry["context"]["provider_tokens"], route["input_tokens"])
                self.assertLessEqual(route["input_tokens"], route["context_tokens"])
                self.assertLessEqual(route.get("output_tokens", 0), route["context_tokens"])
                self.assertEqual(route["source"], "docs")
                self.assertTrue(route["source_ref"].startswith("https://"))
                # Never ahead of today anywhere (a local check date, a UTC run).
                self.assertLessEqual(datetime.date.fromisoformat(route["checked"]),
                                     datetime.date.today() + datetime.timedelta(days=1))
        # Every active line of a provider with an explicit key route is
        # reviewed for it, so a profile on that provider keeps every slot
        # served when the key is selected.
        for provider_id in sorted(explicit):
            for key, entry in sorted(lines.items()):
                if entry["provider"] == provider_id and entry["status"] == "active":
                    with self.subTest(reviewed=key):
                        self.assertIsNotNone(catalog.key_route(entry))

    def test_the_validator_refuses_an_unsound_route(self) -> None:
        entry = self.docs["models-v2"]["models"][self.key]
        sound = route_block(entry, sorted(entry["efforts"]))
        self.assertEqual(self.check(self.key, {**entry, "key_route": sound}), [])
        other = next(key for key, line in self.docs["models-v2"]["models"].items()
                     if self.docs["providers"]["providers"][line["provider"]]["transport"]["kind"] == "direct")
        other_entry = self.docs["models-v2"]["models"][other]
        cases = {
            "another provider": (other, {**other_entry, "key_route": route_block(other_entry, ["high"])},
                                 "only a line on an OAuth pool"),
            "unknown effort": (self.key, {**entry, "key_route": route_block(entry, ["minimal"])},
                               "is not an effort of the line"),
            "bound above the input limit": (
                self.key, {**entry, "key_route": {**sound, "input_tokens": entry["context"]["provider_tokens"] - 1}},
                "exceeds the route's documented input limit"),
            "input above the window": (self.key, {**entry, "key_route": {**sound, "context_tokens": 1}},
                                       "exceeds the route's context_tokens"),
        }
        for label, (key, candidate, needle) in cases.items():
            with self.subTest(case=label):
                found = self.check(key, candidate)
                self.assertTrue(any(needle in text for text in found), found)
        provider = self.docs["providers"]["providers"]["openai"]
        self.assertEqual(catalog._check_key_route("models.custom-x", {**entry, "key_route": {}}, provider,
                                                  operator=True),
                         ["models.custom-x.key_route: only a reviewed catalog line lists an API-key route"])

    def test_the_schema_is_closed_and_complete(self) -> None:
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "models.schema.json")
        entry = self.docs["models-v2"]["models"][self.key]
        good = {**entry, "key_route": route_block(entry, ["high"])}
        self.assertEqual(schema_validate.validate({"version": 2, "models": {self.key: good}}, schema), [])
        for label, change in (("unknown field", {"thinking": ["high"]}), ("other source", {"source": "operator"}),
                              ("no efforts", {"efforts": []})):
            with self.subTest(case=label):
                bad = {**entry, "key_route": {**route_block(entry, ["high"]), **change}}
                self.assertTrue(schema_validate.validate({"version": 2, "models": {self.key: bad}}, schema))
        missing = {**entry, "key_route": {k: v for k, v in route_block(entry, ["high"]).items() if k != "source_ref"}}
        self.assertTrue(schema_validate.validate({"version": 2, "models": {self.key: missing}}, schema))

    def test_levels_are_canonical(self) -> None:
        entry = {"key_route": {"efforts": ["max", "low", "high"]}}
        self.assertEqual(catalog.key_route_levels(entry), ("low", "high", "max"))
        self.assertEqual(catalog.key_route_levels({}), ())


# ------------------------------------------------------------ the transport descriptor
class DescriptorTests(unittest.TestCase):
    def test_the_official_base_carries_v1_and_bearer_auth(self) -> None:
        alternative = operator.transport_alternative(*KEY)
        self.assertEqual(alternative.base_url, "https://api.openai.com/v1")
        self.assertEqual((alternative.auth_kind, alternative.header, alternative.origin),
                         ("bearer", None, "https://api.openai.com"))
        self.assertEqual((alternative.channel, alternative.explicit_models), (render.CODEX_KEY_SECTION, True))
        record = operator.transport_route_record(alternative, at=STAMP)
        self.assertEqual((record["auth"], record["header"], record["secret_ref"]),
                         ("bearer", None, f"env:{SECRET}"))
        anthropic = operator.transport_alternative("anthropic", operator.TRANSPORT_API_KEY)
        self.assertEqual((anthropic.channel, anthropic.explicit_models), (render.CLAUDE_KEY_SECTION, False))
        self.assertNotEqual(alternative.route_digest, anthropic.route_digest)

    def test_this_build_offers_it_and_a_closed_build_says_why(self) -> None:
        docs, _key, _efforts = reviewed_docs()
        alternative = operator.transport_alternative(*KEY)
        self.assertEqual((alternative.available, alternative.closed_reason), (True, None))
        self.assertIsNone(operator.transport_problem(docs, *KEY))
        with closed("closed for this check"):
            self.assertEqual(operator.transport_problem(docs, *KEY), "closed for this check")

    def test_selection_needs_a_reviewed_line(self) -> None:
        docs = copy.deepcopy(catalog.load_catalog(FIXTURE_ROOT).docs)
        with opened():
            problem = operator.transport_problem(docs, *KEY)
            self.assertIn("no OpenAI model is reviewed for its api-key transport", problem)
            docs, _key, _efforts = reviewed_docs()
            self.assertIsNone(operator.transport_problem(docs, *KEY))

    def test_the_key_name_is_reserved_even_while_not_offered(self) -> None:
        files = {"acme": {"version": 1, "provider": {
            "display": "Acme", "kind": "anthropic-compatible", "base_url": "https://api.acme.example/anthropic",
            "auth": {"kind": "bearer", "secret_ref": f"env:{SECRET}"}, "independence_family": "acme"},
            "lines": {}}}
        layer = operator.validate_layer(copy.deepcopy(catalog.load_catalog(FIXTURE_ROOT).docs),
                                        {name: strict_json.pretty_file_bytes(doc) for name, doc in files.items()},
                                        schemas=operator.load_schemas(FIXTURE_ROOT))
        self.assertNotIn("acme", layer.providers)
        self.assertTrue(any("one secret has one origin" in problem.text() for problem in layer.problems))


# ------------------------------------------------------------ the render
class KeyRouteRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = opened()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.docs, self.key, self.efforts = reviewed_docs()
        self.entry = self.docs["models-v2"]["models"][self.key]

    def render(self, ledger_=None, **kwargs):
        plan = operator.render_plan(self.docs, operator.empty_layer(), selected() if ledger_ is None else ledger_)
        return (plan, *build(plan, **kwargs))

    def test_the_section_lists_exactly_the_reviewed_models(self) -> None:
        plan, document, available, unavailable, info = self.render()
        self.assertIn("openai", available)
        self.assertEqual(unavailable, ())
        self.assertEqual(plan.transports["openai"].problem, None)
        sections = document[render.CODEX_KEY_SECTION]
        self.assertEqual(len(sections), 1)
        section = sections[0]
        self.assertEqual(list(section), ["api-key", "base-url", "disable-codex-cloaking", "request-retry", "models"])
        self.assertEqual((section["api-key"], section["base-url"], section["disable-codex-cloaking"],
                          section["request-retry"]), (DUMMY, "https://api.openai.com/v1", True, 0))
        route = self.entry["key_route"]
        wanted = selectors_at(self.entry, self.efforts)
        self.assertEqual({item["alias"] for item in section["models"]}, wanted)
        for item in section["models"]:
            self.assertEqual(item, {"name": self.entry["wire_model"], "alias": item["alias"],
                                    "display-name": self.entry["display"],
                                    "max-context-length": route["context_tokens"],
                                    "thinking": {"levels": list(self.efforts)}, "force-mapping": True})
        # Never the Claude key channel, never the account pool, no fallback.
        claude_aliases = {item["alias"] for section in document["claude-api-key"] for item in section["models"]}
        self.assertFalse(claude_aliases & selectors_at(self.entry, self.entry["efforts"]))
        self.assertNotIn("codex", document["oauth-model-alias"])
        self.assertEqual(document[render.EXCLUDED_MODELS_KEY], {"codex": ["*"]})
        # The served-set authorities agree with the section.
        providers = plan.docs["providers"]["providers"]
        self.assertEqual(render.provider_selectors("openai", providers["openai"], plan.docs["models-v2"]["models"],
                                                   continuity={}, providers=providers), wanted)
        self.assertEqual(operator.key_route_selectors(self.docs, "openai"), wanted)
        self.assertLessEqual(wanted, render.rendered_selectors(document))
        # Effort contracts: the codex protocol, only for the reviewed aliases.
        overrides = {model["name"]: entry["params"] for entry in document["payload"]["override"]
                     for model in entry["models"] if model["name"] in selectors_at(self.entry, self.entry["efforts"])}
        self.assertEqual(set(overrides), wanted)
        for alias, params in overrides.items():
            self.assertEqual(params, {"reasoning.effort": alias.rsplit("-", 1)[1]})
        self.assertTrue(all(model["protocol"] == "codex" for entry in document["payload"]["override"]
                            for model in entry["models"] if model["name"] in wanted))

    def test_unreviewed_lines_efforts_and_retained_aliases_are_not_served(self) -> None:
        continuity = {"gpt-multi-retired-high": {"provider": "openai", "wire": "gpt-retired", "display": "Old",
                                                 "context_tokens": 100_000, "proxy_contract": "reasoning-effort-high"},
                      "gpt-multi-retired-low": {"provider": "openai", "wire": "gpt-retired", "display": "Old",
                                                "context_tokens": 100_000, "proxy_contract": "reasoning-effort-low"}}
        _plan, document, _a, _u, info = self.render(continuity=continuity)
        served = render.rendered_selectors(document)
        for key in openai_lines(self.docs):
            entry = self.docs["models-v2"]["models"][key]
            reviewed = set(catalog.key_route_levels(entry))
            for level, selector, _contract in catalog.line_selectors(entry):
                with self.subTest(line=key, effort=level):
                    self.assertEqual(operator._selector_base(selector) in served, level in reviewed)
        self.assertFalse(set(continuity) & served)
        self.assertIn("2 continuity alias(es) of openai: not served on its API-key route (only reviewed lines are)",
                      info["notices"])

    def test_no_fallback_to_the_account_without_a_usable_key(self) -> None:
        for label, value in (("missing", None), ("blank", ""), ("invalid", "has space")):
            with self.subTest(key=label):
                _plan, document, available, unavailable, _info = self.render(
                    resolve=lambda name, value=value: value if name == SECRET else "d")
                self.assertNotIn("openai", available)
                self.assertNotIn(render.CODEX_KEY_SECTION, document)
                self.assertNotIn("codex", document["oauth-model-alias"])
                self.assertEqual(document[render.EXCLUDED_MODELS_KEY], {"codex": ["*"]})
                reason = next(item["reason"] for item in unavailable if item["provider"] == "openai")
                self.assertIn(f"env:{SECRET}", reason)
        # The availability rule the pickers use agrees with the render.
        plan = operator.render_plan(self.docs, operator.empty_layer(), selected())
        providers = plan.docs["providers"]["providers"]
        for value, expected in ((None, f"missing required secret env:{SECRET}"),
                                ("", f"unusable required secret env:{SECRET} (blank or invalid; value not shown)"),
                                (DUMMY, None)):
            with self.subTest(parity=value):
                self.assertEqual(render.provider_secret_reason(providers["openai"], lambda _n, v=value: v), expected)

    def test_an_unapproved_selection_is_withheld(self) -> None:
        plan, document, available, unavailable, _info = self.render(selected(approved=False))
        self.assertIn("not approved", plan.transports["openai"].problem)
        self.assertNotIn("openai", available)
        self.assertNotIn(render.CODEX_KEY_SECTION, document)
        self.assertNotIn("codex", document["oauth-model-alias"])
        self.assertIn({"provider": "openai", "reason": "transport api-key is not approved"}, unavailable)

    def test_an_empty_reviewed_list_is_never_rendered(self) -> None:
        del self.docs["models-v2"]["models"][self.key]["key_route"]
        plan, document, _available, _unavailable, _info = self.render()
        self.assertNotIn(render.CODEX_KEY_SECTION, document)  # never `models: []`
        self.assertNotIn("codex", document["oauth-model-alias"])
        self.assertEqual(document[render.EXCLUDED_MODELS_KEY], {"codex": ["*"]})
        self.assertNotIn("models: []", render.emit_yaml(document))
        self.assertEqual(render.provider_selectors("openai", plan.docs["providers"]["providers"]["openai"],
                                                   plan.docs["models-v2"]["models"], continuity={}), frozenset())

    def test_image_generation_passthrough_follows_the_openai_key_transport(self) -> None:
        line = f'{render.IMAGE_GENERATION_KEY}: "{render.IMAGE_GENERATION_PASSTHROUGH}"'
        # The OpenAI key transport selected: rendered, keyed or not, approved or not.
        for label, ledger_, resolve in (
                ("keyed", selected(), None),
                ("no key", selected(), lambda name: None if name == SECRET else "d"),
                ("unapproved", selected(approved=False), None)):
            with self.subTest(selected=label):
                _plan, document, _a, _u, _i = self.render(ledger_, resolve=resolve)
                self.assertEqual(document[render.IMAGE_GENERATION_KEY], render.IMAGE_GENERATION_PASSTHROUGH)
                self.assertIn(line, render.emit_yaml(document).splitlines())
        # Never otherwise: the account pool, or the Anthropic key transport.
        anthropic = operator.transport_route_record(
            operator.transport_alternative("anthropic", operator.TRANSPORT_API_KEY), at=STAMP)
        for label, ledger_ in (("account", ledger()),
                               ("anthropic key", ledger(transport_choices={"anthropic": operator.TRANSPORT_API_KEY},
                                                        routes={"anthropic": anthropic}))):
            with self.subTest(selected=label):
                _plan, document, _a, _u, _i = self.render(ledger_)
                self.assertNotIn(render.IMAGE_GENERATION_KEY, document)
                self.assertNotIn(render.IMAGE_GENERATION_KEY, render.emit_yaml(document))

    def test_the_trusted_docs_never_change_and_the_pool_is_the_default(self) -> None:
        before = copy.deepcopy(self.docs)
        _plan, document, _a, _u, _i = self.render()
        self.assertEqual(self.docs, before)
        _plan, pool_document, available, _u, _i = self.render(ledger())
        self.assertNotIn(render.CODEX_KEY_SECTION, pool_document)
        self.assertNotIn(render.EXCLUDED_MODELS_KEY, pool_document)
        self.assertIn("codex", pool_document["oauth-model-alias"])
        self.assertIn("openai", available)


# ------------------------------------------------------------ the served identity
class KeyRouteServedPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = opened()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.docs, self.key, self.efforts = reviewed_docs()
        self.entry = self.docs["models-v2"]["models"][self.key]

    def document(self, ledger_):
        plan = operator.render_plan(self.docs, operator.empty_layer(), ledger_)
        return build(plan)[0]

    def test_switching_retargets_the_reviewed_aliases_and_removes_the_rest(self) -> None:
        pool, keyed = self.document(ledger()), self.document(selected())
        text = render.emit_yaml(keyed)
        self.assertEqual(served_plan.parse_restricted_yaml(text), keyed)  # a byte-exact inverse
        before = served_plan.routes_from_document(pool)
        after = served_plan.published_routes(text)
        identity = json.dumps({selector: route.as_document() for selector, route in after.items()})
        self.assertNotIn(DUMMY, identity)
        reviewed = selectors_at(self.entry, self.efforts)
        for alias in reviewed:
            route = after[alias]
            self.assertEqual((route.section, route.route, route.wire, route.auth_header),
                             (served_plan.SECTION_CODEX_KEY, "https://api.openai.com/v1", self.entry["wire_model"],
                              "bearer"))
            self.assertIn("key route https://api.openai.com/v1 (responses, bearer)", route.label())
            self.assertTrue(route.contract)
        plan = served_plan.build_plan(before, after, references=served_plan.References({}, frozenset()),
                                      state_root="/state", gateway="g")
        self.assertEqual({selector for selector, _o, _n in plan.retargeted}, reviewed)
        pool_openai = {alias for alias, route in before.items()
                       if route.section == served_plan.SECTION_OAUTH and route.route == "codex"}
        self.assertEqual({selector for selector, _route in plan.removed}, pool_openai - reviewed)
        self.assertNotIn(DUMMY, plan.text())

    def test_a_section_switch_or_a_level_change_is_a_retarget(self) -> None:
        keyed = self.document(selected())
        before = served_plan.routes_from_document(keyed)
        for label, change in (("retry", lambda s: s.update({"request-retry": 1})),
                              ("cloaking", lambda s: s.update({"disable-codex-cloaking": False})),
                              ("levels", lambda s: s["models"][0]["thinking"].update({"levels": ["low"]}))):
            with self.subTest(change=label):
                changed = copy.deepcopy(keyed)
                change(changed[render.CODEX_KEY_SECTION][0])
                plan = served_plan.build_plan(before, served_plan.routes_from_document(changed),
                                              references=served_plan.References({}, frozenset()),
                                              state_root="/state", gateway="g")
                self.assertTrue(plan.retargeted)


# ------------------------------------------------------------ doctor
class KeyRouteDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = opened()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.docs, self.key, _efforts = reviewed_docs()

    def findings(self, ledger_, **kwargs):
        schemas = operator.load_schemas(FIXTURE_ROOT)
        layer = operator.validate_layer(self.docs, {}, schemas=schemas, ledger=ledger_)
        read = operator.ProvidersRead(Path("/nonexistent/providers.d"), True, False, {}, False, ())
        snapshot = operator.OperatorSnapshot(layer, ledger_, None, True, read, schemas)
        plan = operator.render_plan(self.docs, layer, ledger_)
        return operator.doctor_findings(self.docs, snapshot, plan=plan, admitted=(), refs={}, live=frozenset(),
                                        **kwargs)

    def test_the_selected_route_is_info_and_names_what_it_serves(self) -> None:
        found = self.findings(selected())
        self.assertEqual(found.blocks, ())
        self.assertIn(f"provider openai transport: api-key (bearer env:{SECRET} → https://api.openai.com); the codex "
                      "OAuth pool serves nothing while it is selected", found.info)
        others = [key for key in openai_lines(self.docs) if key != self.key]
        self.assertIn(f"provider openai transport api-key: 1 of {len(others) + 1} openai model lines are reviewed for "
                      f"this route ({self.key}); not served while it is selected: {', '.join(others)}", found.info)

    def test_a_missing_key_an_unapproved_choice_or_no_reviewed_line_blocks(self) -> None:
        found = self.findings(selected(), unset_secrets=(SECRET,))
        # The transport is chosen and approved: the fix saves the key (a
        # second switch to the same transport would change nothing).
        self.assertTrue(any(f"{SECRET} is not set — claude-multi providers set-key openai" in line
                            and "back to the pool: claude-multi providers transport openai oauth-pool" in line
                            for line in found.blocks), found.blocks)
        self.assertFalse(any("providers transport openai api-key" in line for line in found.blocks), found.blocks)
        found = self.findings(selected(approved=False))
        self.assertTrue(any("is not approved for the current reviewed descriptor" in line for line in found.blocks))
        del self.docs["models-v2"]["models"][self.key]["key_route"]
        found = self.findings(selected())
        self.assertTrue(any("no model is reviewed for this route, so nothing of openai is served" in line
                            for line in found.blocks), found.blocks)


# ------------------------------------------------------------ key material
class KeyMaterialTests(unittest.TestCase):
    """Every diagnostic of this route is value-free: the key appears once,
    in the rendered section's api-key field, and nowhere else."""

    def test_the_key_appears_only_in_its_field(self) -> None:
        docs, key, _efforts = reviewed_docs()
        with opened():
            plan = operator.render_plan(docs, operator.empty_layer(), selected())
            document, available, unavailable, info = build(plan)
            text = render.emit_yaml(document)
            self.assertEqual(text.count(DUMMY), 1)
            self.assertIn(f'api-key: "{DUMMY}"', text)
            pool = build(operator.render_plan(docs, operator.empty_layer(), ledger()))[0]
            served = served_plan.build_plan(served_plan.routes_from_document(pool),
                                            served_plan.published_routes(text),
                                            references=served_plan.References({}, frozenset()),
                                            state_root="/state", gateway="g")
            for broken in ("", " " + DUMMY, DUMMY + "\n"):
                unusable = build(plan, resolve=lambda name, value=broken: value if name == SECRET else "d")
                self.assertNotIn(DUMMY, json.dumps(unusable[2]) + json.dumps(list(unusable[3]["notices"])))
            schemas = operator.load_schemas(FIXTURE_ROOT)
            layer = operator.validate_layer(docs, {}, schemas=schemas, ledger=selected())
            read = operator.ProvidersRead(Path("/nonexistent/providers.d"), True, False, {}, False, ())
            snapshot = operator.OperatorSnapshot(layer, selected(), None, True, read, schemas)
            findings = operator.doctor_findings(docs, snapshot, plan=plan, admitted=(), refs={}, live=frozenset())
        visible = [served.text(), *findings.blocks, *findings.attention, *findings.info,
                   json.dumps(list(unavailable)), *info["notices"], str(plan.transports["openai"].problem),
                   operator.transport_problem(docs, *KEY) or "", json.dumps(dataclasses.asdict(
                       operator.transport_alternative(*KEY)))]
        for line in visible:
            self.assertNotIn(DUMMY, line)


# ------------------------------------------------------------ the key service and its verbs
def reviewed_asset_root(target: Path) -> tuple[Path, str]:
    """A copy of the fixture asset root whose lead-capable OpenAI line is
    reviewed for the key route at its first effort."""

    root = target / "assets"
    shutil.copytree(FIXTURE_ROOT, root)
    path = root / "catalog" / "models.json"
    document = json.loads(path.read_bytes())
    key = next(k for k, e in sorted(document["models"].items())
               if e["provider"] == "openai" and "lead" in e["capabilities"] and isinstance(e["efforts"], dict))
    entry = document["models"][key]
    entry["key_route"] = route_block(entry, (sorted(entry["efforts"])[0],))
    path.write_bytes(strict_json.pretty_file_bytes(document))
    return root, key


class KeyServiceTests(test_setup_cli.SetupCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = Path(tempfile.mkdtemp(prefix="cm-key-route-"))
        self.addCleanup(shutil.rmtree, temporary, True)
        self.assets, self.key = reviewed_asset_root(temporary)
        self.runtime.asset_root = self.assets
        self.runtime.reload_catalog()
        patcher = opened()
        patcher.start()
        self.addCleanup(patcher.stop)

    def human(self):
        return mock.patch.object(consent, "stdio_ttys", return_value=True)

    def store(self) -> secret_store.FileSecretStore:
        return secret_store.default_store(self.runtime.gateway_environ())

    def config(self) -> str:
        return (claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml").read_text()

    def test_the_layer_switches_to_the_key_and_back(self) -> None:
        names = {"display": "OpenAI", "account": "ChatGPT account"}
        total = setup_providers.line_count(self.runtime, "openai")
        with self.human():
            with self.assertRaises(model.Refused) as caught:
                setup_providers.plan_set_key(self.runtime, "openai")
            self.assertEqual(caught.exception.line_text,
                             texts.KEY_ACCOUNT_TRANSPORT_LINE.format(**names, id="openai"))
            plan = setup_providers.plan_transport(self.runtime, "openai", "api-key")
            self.assertEqual((plan.consent, plan.key_present, plan.secret_name, plan.header),
                             ("approve-route", False, SECRET, None))
            body = "\n".join(plan.lines)
            self.assertIn("move to https://api.openai.com — same names, never both at once", body)
            self.assertIn("The key is sent only to https://api.openai.com (header Authorization)", body)
            self.assertIn("Your ChatGPT account sign-in stays on this computer but is not used.", body)
            self.assertIn(texts.TRANSPORT_KEY_REVIEWED.format(display="OpenAI", names=self.key, n=1, total=total),
                          body)
            applied = setup_providers.apply_transport(self.runtime, plan, model.Confirmation.given(plan),
                                                      model.Secret(DUMMY))
            self.assertTrue(applied.lines[0].startswith("OpenAI models now use your OpenAI API key"), applied.lines)
            self.assertNotIn(DUMMY, "\n".join(applied.lines))
            self.assertEqual(self.store().get(SECRET), DUMMY)
            config = self.config()
            self.assertIn("codex-api-key:", config)
            self.assertIn('base-url: "https://api.openai.com/v1"', config)
            self.assertIn('oauth-excluded-models:\n  codex:\n    - "*"', config)
            # The key in use is keyed now: it can be replaced, not removed.
            keyed = setup_providers.plan_set_key(self.runtime, "openai")
            self.assertEqual((keyed.secret_name, keyed.replaces, keyed.display), (SECRET, True, "OpenAI"))
            with self.assertRaises(model.Refused) as caught:
                setup_providers.plan_remove_key(self.runtime, "openai")
            self.assertEqual(caught.exception.line_text, texts.KEY_IN_USE_LINE.format(**names, id="openai"))
            back = setup_providers.plan_transport(self.runtime, "openai", "oauth-pool")
            self.assertIn("move back to your ChatGPT account sign-in", back.lines[0])
            self.assertIn("The OpenAI API key stays saved unless you remove it next.", back.lines)
            applied = setup_providers.apply_transport(self.runtime, back, model.Confirmation.given(back))
            self.assertTrue(applied.lines[0].startswith("OpenAI models use your ChatGPT account sign-in again"))
        self.assertNotIn("codex-api-key", self.config())
        self.assertEqual(self.store().get(SECRET), DUMMY)

    def test_the_verbs_switch_replace_refuse_removal_and_switch_back(self) -> None:
        key = self.private_file("keys/openai.key", (DUMMY + "\n").encode())
        code, out, err = self.op(["providers", "transport", "openai"], tty=False, env={"CLAUDECODE": "1"})
        self.assertEqual(code, 0, err)
        self.assertIn(f"openai\tapi-key\tbearer env:{SECRET} → https://api.openai.com", out)
        code, out, err = self.op(["providers", "transport", "openai", "api-key", "--secret-file", str(key)], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("OpenAI models now use your OpenAI API key — the gateway reloaded", out)
        self.assertIn("Only models reviewed for OpenAI API keys are served on it", err)
        self.assertNotIn(DUMMY, out + err)
        self.assertEqual(self.runtime.operator_snapshot().ledger.transport_choices, {"openai": "api-key"})
        replacement = self.private_file("keys/openai-2.key", b"openai-dummy-platform-key-second\n")
        code, out, err = self.op(["providers", "set-key", "openai", "--secret-file", str(replacement)], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("OpenAI API key saved", out)
        self.assertEqual(self.store().get(SECRET), "openai-dummy-platform-key-second")
        code, _out, err = self.op(["providers", "remove-key", "openai", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("claude-multi providers transport openai oauth-pool", err)
        code, out, err = self.op(["providers", "transport", "openai", "oauth-pool"], "y\ny\n")
        self.assertEqual(code, 0, err)
        self.assertIn("OpenAI models use your ChatGPT account sign-in again", out)
        self.assertIn(f"Also remove the saved OpenAI API key ({SECRET})? [y/N] ", err)
        self.assertIsNone(self.store().get(SECRET))
        self.assertNotIn("openai-dummy-platform-key", out + err)

    def test_the_menu_offers_the_key_route(self) -> None:
        entry = next(item for item in setup_providers.picker_entries(self.runtime) if item.id == "openai:api-key")
        self.assertEqual((entry.available, entry.label, entry.kind), (True, texts.PICKER_LABELS["openai:api-key"],
                                                                      "api-key"))
        number = self.menu_number("openai:api-key")
        code, out, err = self.op(["setup", "--step", "providers"], f"{number}\ny\n{DUMMY}\n\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("OpenAI models now use your OpenAI API key", out)
        self.assertEqual(self.store().get(SECRET), DUMMY)
        self.assertNotIn(DUMMY, out + err)

    def test_an_answers_document_selects_the_key_route(self) -> None:
        key = self.private_file("keys/openai.key", (DUMMY + "\n").encode())
        path = self.answers({"version": 1, "providers": [{"id": "openai", "transport": "api-key",
                                                          "api_key_file": str(key)}]})
        code, out, err = self.op(["setup", "--answers", str(path)], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.runtime.operator_snapshot().ledger.transport_choices, {"openai": "api-key"})
        self.assertEqual(self.store().get(SECRET), DUMMY)
        self.assertIn("codex-api-key:", self.config())
        self.assertNotIn(DUMMY, out + err)

    def details(self, key: str) -> str:
        """The Models screen's full details of ``key`` (V), read afresh."""

        from claude_multi import tui
        import claude_multi.cli.screens.models as models_screen

        return "\n".join(models_screen._ModelsScreen(self.runtime, palette=tui.MONO_PALETTE)._inspection(key))

    def switch(self, to: str) -> None:
        with self.human():
            plan = setup_providers.plan_transport(self.runtime, "openai", to)
            value = model.Secret(DUMMY) if to == operator.TRANSPORT_API_KEY and not plan.key_present else None
            setup_providers.apply_transport(self.runtime, plan, model.Confirmation.given(plan), value)

    def test_models_shows_the_evidence_of_the_route_in_use_both_ways(self) -> None:
        from claude_multi import profile

        entry = self.runtime.catalog.docs["models-v2"]["models"][self.key]
        route = entry["key_route"]
        measured = f"validated {profile.format_tokens(entry['context']['validated_tokens'])}"
        # Docs only, never measured on the key route: the conservative floor.
        floor_tokens = min(route["input_tokens"], catalog.CUSTOM_VALIDATED_CAP)
        floor = f"validated {profile.format_tokens(floor_tokens)}"
        self.assertNotEqual(measured, floor)
        own = (floor, f"provider bound {profile.format_tokens(route['input_tokens'])}",
               f"declared {profile.format_tokens(route['context_tokens'])}",
               f"output {profile.format_tokens(route['output_tokens'])}", route["source_ref"],
               "not measured on this route", "OpenAI API key in use")
        for to in (None, operator.TRANSPORT_API_KEY, operator.TRANSPORT_POOL, operator.TRANSPORT_API_KEY):
            if to is not None:
                self.switch(to)
            with self.subTest(transport=to or "the default"):
                details = self.details(self.key)
                if to == operator.TRANSPORT_API_KEY:
                    for text in own:
                        self.assertIn(text, details)
                    self.assertNotIn(measured, details)
                    self.assertNotIn(entry["context"]["qualification"], details)
                else:
                    self.assertIn(measured, details)
                    self.assertIn(entry["context"]["qualification"], details)
                    self.assertNotIn(floor, details)
                    self.assertNotIn("API key in use", details)
        # A line not reviewed for the key route keeps its own evidence.
        other = next(key for key in openai_lines(self.runtime.catalog.docs) if key != self.key)
        other_entry = self.runtime.catalog.docs["models-v2"]["models"][other]
        self.assertIn(f"validated {profile.format_tokens(other_entry['context']['validated_tokens'])}",
                      self.details(other))
        self.assertEqual(catalog.key_route_floor(route), floor_tokens)

    @uses_shipped_catalog
    def test_the_shipped_key_route_lines_show_their_floor_not_the_account_measurement(self) -> None:
        from claude_multi import profile

        self.runtime.asset_root = SHIPPED_ROOT
        self.runtime.reload_catalog()
        lines = self.runtime.catalog.docs["models-v2"]["models"]
        reviewed = operator.key_route_lines(self.runtime.catalog.docs, "openai")
        self.assertTrue(reviewed)
        self.switch(operator.TRANSPORT_API_KEY)
        for key in reviewed:
            with self.subTest(line=key):
                route, context = lines[key]["key_route"], lines[key]["context"]
                details = self.details(key)
                floor_tokens = min(route["input_tokens"], catalog.CUSTOM_VALIDATED_CAP)
                self.assertIn(f"validated {profile.format_tokens(floor_tokens)}", details)
                if context["validated_tokens"] != floor_tokens:
                    self.assertNotIn(f"validated {profile.format_tokens(context['validated_tokens'])}", details)
                self.assertNotIn(context["qualification"], details)
                self.assertIn(route["source_ref"], details)
                self.assertEqual(catalog.key_route_floor(route), floor_tokens)
        self.switch(operator.TRANSPORT_POOL)
        for key in reviewed:
            with self.subTest(account=key):
                context = lines[key]["context"]
                self.assertIn(f"validated {profile.format_tokens(context['validated_tokens'])}", self.details(key))

    def test_no_reviewed_line_keeps_the_entry_closed(self) -> None:
        path = self.assets / "catalog" / "models.json"
        document = json.loads(path.read_bytes())
        del document["models"][self.key]["key_route"]
        path.write_bytes(strict_json.pretty_file_bytes(document))
        self.runtime.reload_catalog()
        entry = next(item for item in setup_providers.picker_entries(self.runtime) if item.id == "openai:api-key")
        self.assertEqual((entry.available, entry.note), (False, texts.KEY_ROUTE_NO_MODELS_NOTE))
        code, _out, err = self.op(["providers", "transport", "openai", "api-key"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("no OpenAI model is reviewed for its api-key transport", err)


class ClosedBuildTests(test_setup_cli.SetupCase):
    """A build that does not offer the route: it is named, refused with its
    fixes, and a key saved earlier can still be removed."""

    def setUp(self) -> None:
        super().setUp()
        patcher = closed()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_entry_the_refusals_and_a_leftover_key(self) -> None:
        entry = next(item for item in setup_providers.picker_entries(self.runtime) if item.id == "openai:api-key")
        self.assertEqual((entry.available, entry.label, entry.note),
                         (False, texts.PICKER_KEY_CLOSED_LABEL.format(display="OpenAI"), texts.OPENAI_KEY_NOTE))
        code, _out, err = self.op(["providers", "set-key", "openai"], "x\n")
        self.assertEqual(code, 1)
        self.assertIn("claude-multi providers sign-in openai", err)
        secret_store.default_store(self.runtime.gateway_environ()).set(SECRET, DUMMY)
        code, out, err = self.op(["providers", "remove-key", "openai", "--yes"])
        self.assertEqual(code, 0, err)
        self.assertIn("OpenAI API key removed", out)
        self.assertNotIn(DUMMY, out + err)


# ------------------------------------------------------------ the pinned gateway
CODEX_CONTRACTS = tuple(render.ADAPTER_PAYLOAD_CONTRACTS["cliproxy-oauth-codex-v1"])
UPSTREAM_PATH = "/cm-key-route/v1"


def gateway_document(bundle, egress: int, port: int, home: Path):
    """The real render of the selected key route (every codex effort of the
    reviewed line declared and reviewed, so the maximum level is exercised),
    with only the official base swapped for the harness egress: the gateway
    under test appends /responses exactly as it would to the official base."""

    docs = copy.deepcopy(bundle.docs)
    docs["providers"]["providers"]["openai"]["payload_contracts"] = list(CODEX_CONTRACTS)
    key = reviewable(docs)
    entry = docs["models-v2"]["models"][key]
    base = next(iter(entry["efforts"].values()))["selector"].rsplit("-", 1)[0]
    entry["efforts"] = {contract.rsplit("-", 1)[1]: {"selector": f"{base}-{contract.rsplit('-', 1)[1]}",
                                                      "proxy_contract": contract} for contract in CODEX_CONTRACTS}
    entry["key_route"] = route_block(entry, sorted(entry["efforts"]))
    with opened():
        plan = operator.render_plan(docs, operator.empty_layer(), selected())
        gateway = copy.deepcopy(plan.docs["gateway"])
        gateway["gateway"]["base_url"] = f"http://127.0.0.1:{port}"
        document, available, _unavailable, _info = render.build_config_document(
            gateway, plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"], home=home,
            gateway_token=FIXTURE_GATEWAY_TOKEN,
            resolve_secret=lambda name: DUMMY if name == SECRET else f"dummy-{name.lower()}",
            continuity={}, captures=plan.captures, provider_headers=plan.headers, oauth_overlay=plan.overlay)
    if "openai" not in available or len(document[render.CODEX_KEY_SECTION]) != 1:
        raise AssertionError("the key route was not rendered")
    document[render.CODEX_KEY_SECTION][0]["base-url"] = f"http://127.0.0.1:{egress}{UPSTREAM_PATH}"
    for section in document["claude-api-key"]:
        section["base-url"] = f"http://127.0.0.1:{egress}/other-route"
    reviewed = {item["alias"]: item for item in document[render.CODEX_KEY_SECTION][0]["models"]}
    return document, {}, {"line": key, "wire": entry["wire_model"], "reviewed": reviewed,
                          "unreviewed": sorted(selectors_at(docs["models-v2"]["models"][other],
                                                            docs["models-v2"]["models"][other]["efforts"])
                                               for other in openai_lines(docs) if other != key)}


def reflecting_upstream(path, body, raw, headers):
    """A fake Responses upstream whose failures quote the credential it got
    (the shape of a provider error that names the key)."""

    given = headers.get("authorization", "").removeprefix("Bearer ")
    if b"reflect-status-failure" in raw:
        return 401, "application/json", json.dumps({"error": {
            "message": f"Incorrect API key provided: {given}", "type": "invalid_request_error",
            "code": "invalid_api_key"}}).encode()
    if b"reflect-stream-failure" in raw:
        frame = {"type": "response.failed", "response": {"id": "resp_failed", "status": "failed", "error": {
            "code": "server_error", "message": f"request with {given} failed"}}}
        return 200, "text/event-stream", harness.sse_frame("response.failed", frame)
    return harness.fake_reply(path, body, raw)


class PinnedGatewayKeyRouteTests(unittest.TestCase):
    """The rendered section loads in the pinned gateway, the reviewed
    aliases reach <base>/responses with the key and their exact effort, and
    nothing else of the provider is served."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.gateway = harness.GatewayHarness(document_factory=gateway_document)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.gateway.close()

    def test_the_listing_serves_exactly_the_reviewed_aliases(self) -> None:
        # The product's served read sends no anthropic-version (the OpenAI
        # listing), which lists every key-route alias.
        reply = harness.unix_request(self.gateway.socket, "GET", "/v1/models", headers={
            "Authorization": "Bearer " + FIXTURE_GATEWAY_TOKEN, "Host": f"127.0.0.1:{self.gateway.port}"})
        self.assertEqual(reply.status, 200)
        served = {row["id"] for row in reply.json()["data"]}
        self.assertLessEqual(set(self.gateway.info["reviewed"]), served)
        self.assertFalse(served & {alias for aliases in self.gateway.info["unreviewed"] for alias in aliases})

    def test_reviewed_aliases_reach_the_responses_endpoint_with_their_effort(self) -> None:
        for alias in sorted(self.gateway.info["reviewed"]):
            for stream in (False, True):
                with self.subTest(alias=alias, stream=stream):
                    reply = self.gateway.request(alias, stream=stream)
                    self.assertEqual(reply.status, 200, reply.raw[:300])
                    self.assertEqual(len(reply.hits), 1)
                    hit = reply.hits[0]
                    self.assertEqual((hit.method, hit.path), ("POST", UPSTREAM_PATH + "/responses"))
                    self.assertEqual(hit.header_values["authorization"], "Bearer " + DUMMY)
                    self.assertEqual(hit.body["model"], self.gateway.info["wire"])
                    self.assertEqual(hit.body["reasoning"]["effort"], alias.rsplit("-", 1)[1])
                    # The API key route never carries the account's identity.
                    self.assertFalse({"chatgpt-account-id", "originator"} & set(hit.header_names))
                    self.assertIn(b"FAKE_OK", reply.raw)

    def test_tools_reach_the_upstream(self) -> None:
        alias = sorted(self.gateway.info["reviewed"])[0]
        tool = {"name": "cm_lookup", "description": "Look a value up.",
                "input_schema": {"type": "object", "properties": {"k": {"type": "string"}}, "required": ["k"]}}
        reply = self.gateway.request(alias, body={"tools": [tool]})
        self.assertEqual(reply.status, 200, reply.raw[:300])
        names = [item.get("name") for item in reply.hits[0].body.get("tools", [])]
        self.assertIn("cm_lookup", names)

    def test_unreviewed_aliases_never_reach_an_upstream(self) -> None:
        for alias in sorted(alias for aliases in self.gateway.info["unreviewed"] for alias in aliases):
            with self.subTest(alias=alias):
                reply = self.gateway.request(alias)
                self.assertGreaterEqual(reply.status, 400)
                self.assertEqual(reply.hits, [])


def without_image_setting(bundle, egress: int, port: int, home: Path):
    """The key route's render without its image-generation setting (the
    gateway's default behaviour, for comparison)."""

    document, aliases, info = gateway_document(bundle, egress, port, home)
    document.pop(render.IMAGE_GENERATION_KEY)
    return document, aliases, info


# A client turn as Claude Code sends one: its own output cap, a system
# prompt and a tool.
CLIENT_TURN = {"max_tokens": 32000, "system": "You are a careful assistant.",
               "tools": [{"name": "cm_lookup", "description": "Look a value up.",
                          "input_schema": {"type": "object", "properties": {"k": {"type": "string"}}}}]}


def tool_types(body) -> list[str]:
    return [str(tool.get("type")) for tool in body.get("tools", [])]


class PinnedGatewayKeyRouteTurnTests(unittest.TestCase):
    """One client turn on the key route as the pinned gateway sends it: one
    Responses request and no retry when it succeeds, what becomes of the
    client's output cap, and no hosted image tool the client never asked
    for (with the gateway's default, without the rendered setting, adding
    one)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.gateway = harness.GatewayHarness(document_factory=gateway_document)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.gateway.close()

    def turn(self, gateway, *, stream: bool):
        alias = sorted(gateway.info["reviewed"])[0]
        reply = gateway.request(alias, stream=stream, body=CLIENT_TURN)
        self.assertEqual(reply.status, 200, reply.raw[:300])
        return reply

    def test_one_client_turn_is_one_upstream_request(self) -> None:
        self.assertEqual(self.gateway.document[render.CODEX_KEY_SECTION][0]["request-retry"], 0)
        for stream in (False, True):
            with self.subTest(stream=stream):
                reply = self.turn(self.gateway, stream=stream)
                self.assertEqual(len(reply.hits), 1)
                self.assertEqual(reply.hits[0].path, UPSTREAM_PATH + "/responses")
                self.assertIn(b"FAKE_OK", reply.raw)

    def test_the_clients_output_cap_does_not_reach_the_responses_request(self) -> None:
        # The pinned translation from the client's Messages request drops its
        # max_tokens and sets no Responses limit: the model's own output
        # limit applies to every turn on this route.
        for stream in (False, True):
            with self.subTest(stream=stream):
                body = self.turn(self.gateway, stream=stream).hits[0].body
                self.assertFalse({"max_output_tokens", "max_tokens", "max_completion_tokens"} & set(body))

    def test_no_hosted_image_tool_is_added(self) -> None:
        self.assertEqual(self.gateway.document[render.IMAGE_GENERATION_KEY], render.IMAGE_GENERATION_PASSTHROUGH)
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.assertEqual(tool_types(self.turn(self.gateway, stream=stream).hits[0].body), ["function"])
        control = harness.GatewayHarness(document_factory=without_image_setting)
        try:
            self.assertNotIn(render.IMAGE_GENERATION_KEY, control.document)
            self.assertIn("image_generation", tool_types(self.turn(control, stream=False).hits[0].body))
        finally:
            control.close()


class PinnedGatewayKeyRouteSafetyTests(unittest.TestCase):
    """What the gateway guarantees on the key route: provider failures that
    quote the key reach the client as fixed local text, and the request
    carries no account client version or session header."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.gateway = harness.GatewayHarness(
            document_factory=gateway_document,
            upstream_factory=lambda path: harness.FakeUpstream(path, respond=reflecting_upstream))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.gateway.close()

    def test_provider_error_text_never_carries_the_key_to_the_client(self) -> None:
        alias = sorted(self.gateway.info["reviewed"])[0]
        for case in ("reflect-stream-failure", "reflect-status-failure"):
            with self.subTest(case=case):
                reply = self.gateway.request(alias, body={"messages": [{"role": "user", "content": case}]})
                self.assertGreaterEqual(reply.status, 400)
                self.assertEqual(len(reply.hits), 1)
                self.assertNotIn(DUMMY.encode(), reply.raw)
                self.assertIn(b"upstream", reply.raw)

    def test_no_account_client_identity_reaches_the_key_route(self) -> None:
        alias = sorted(self.gateway.info["reviewed"])[-1]
        reply = self.gateway.request(alias)
        self.assertEqual(reply.status, 200, reply.raw[:300])
        self.assertFalse({"version", "session-id", "session_id"} & set(reply.hits[0].header_names))
        # The prompt cache key still travels in the body.
        self.assertTrue(reply.hits[0].body.get("prompt_cache_key"))


if __name__ == "__main__":
    unittest.main()
