"""Operator schemas, the providers.d loader, digests,
collisions, the credential/endpoint policy, the migration preflight and
``operator.displaced_lines``.

Every provider, wire and credential name here is fixture-local (invented);
no shipped model id is pinned. Credential values are dummies.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from claude_multi import catalog, compiler, operator, proxy, secret_store, state, strict_json
from claude_multi import validate as schema_validate
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT

OPERATOR_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "operator"
PROVIDERS_FIXTURE = OPERATOR_FIXTURES / "providers.d"
OPERATOR_SCHEMAS = ("operator-provider", "operator-ledger", "operator-evidence")


def _docs() -> dict:
    return copy.deepcopy(catalog.load_catalog(FIXTURE_ROOT).docs)


def _schemas() -> operator.OperatorSchemas:
    return operator.load_schemas(FIXTURE_ROOT)


def _fixture_files() -> dict[str, dict]:
    return {path.stem: json.loads(path.read_text()) for path in sorted(PROVIDERS_FIXTURE.glob("*.json"))}


def _raw(files: dict[str, dict]) -> dict[str, bytes]:
    return {name: strict_json.pretty_file_bytes(doc) for name, doc in files.items()}


def _layer(files: dict[str, dict] | None = None, **kwargs) -> operator.OperatorLayer:
    docs = kwargs.pop("docs", None) or _docs()
    return operator.validate_layer(docs, _raw(_fixture_files() if files is None else files),
                                   schemas=kwargs.pop("schemas", None) or _schemas(), **kwargs)


def _texts(layer: operator.OperatorLayer) -> list[str]:
    return [problem.text() for problem in layer.problems]


def _ledger(**members) -> operator.OperatorLedger:
    document = json.loads((OPERATOR_FIXTURES / "ledger-empty.json").read_text())
    document.update(members)
    return operator.parse_ledger(strict_json.pretty_file_bytes(document), _schemas().ledger)


def _route(provider: operator.ResolvedProvider) -> dict:
    return {"rd": provider.route_digest, "secret_ref": provider.entry["transport"]["auth"].get("secret_ref"),
            "auth": provider.auth_kind, "header": provider.header, "origin": provider.origin,
            "listing_origin": provider.listing_origin, "approved_at": "2026-09-30T00:00:00Z"}


def _acme_line(**overrides) -> dict:
    line = {"wire_model": "acme-extra-1", "display": "Acme Extra", "efforts": ["high"], "default_effort": "high",
            "context": {"declared_tokens": 65536, "source": "operator"}}
    line.update(overrides)
    return line


# ------------------------------------------------------------------ schemas
class OperatorSchemaTests(unittest.TestCase):
    def test_schemas_are_valid_closed_and_mirrored_byte_exact(self) -> None:
        for name in OPERATOR_SCHEMAS:
            with self.subTest(schema=name):
                shipped = (SHIPPED_ROOT / "schemas" / f"{name}.schema.json").read_bytes()
                fixture = (FIXTURE_ROOT / "schemas" / f"{name}.schema.json").read_bytes()
                self.assertEqual(shipped, fixture)
                document = strict_json.loads(shipped)
                schema_validate.check_schema(document)
                self.assertEqual(document["version"], operator.SCHEMA_META_VERSION)
                self.assertIs(document["additionalProperties"], False)

    def test_fixture_documents_validate(self) -> None:
        schemas = _schemas()
        for path in sorted(PROVIDERS_FIXTURE.glob("*.json")):
            with self.subTest(file=path.name):
                self.assertEqual(schema_validate.validate(json.loads(path.read_text()), schemas.provider), [])
        self.assertEqual(schema_validate.validate(json.loads((OPERATOR_FIXTURES / "ledger-empty.json").read_text()), schemas.ledger), [])
        self.assertEqual(schema_validate.validate(json.loads((OPERATOR_FIXTURES / "evidence-sample.json").read_text()), schemas.evidence), [])

    def test_author_schema_refuses_authority_fields(self) -> None:
        schema = _schemas().provider
        base = _fixture_files()["acme"]
        line_fields = ("status", "lead", "validated_tokens", "qualification", "minimum_tested", "evidence",
                       "verified", "registry_overlay", "passthrough_routes", "overlay", "thinking")
        for name in line_fields:
            with self.subTest(line_field=name):
                document = copy.deepcopy(base)
                document["lines"]["custom-acme-small"][name] = {}
                self.assertTrue(any(f"unexpected key {name!r}" in p for p in schema_validate.validate(document, schema)))
        for name in ("adapter", "transport", "passthrough_routes", "support", "pool"):
            with self.subTest(provider_field=name):
                document = copy.deepcopy(base)
                document["provider"][name] = {}
                self.assertTrue(any(f"unexpected key {name!r}" in p for p in schema_validate.validate(document, schema)))

    def test_booleans_are_never_integers(self) -> None:
        schema = _schemas().provider
        for mutate in (lambda d: d.__setitem__("version", True),
                       lambda d: d["lines"]["custom-acme-small"]["context"].__setitem__("declared_tokens", True)):
            document = copy.deepcopy(_fixture_files()["acme"])
            mutate(document)
            self.assertTrue(schema_validate.validate(document, schema))

    def test_ledger_and_evidence_are_closed(self) -> None:
        schemas = _schemas()
        ledger = json.loads((OPERATOR_FIXTURES / "ledger-empty.json").read_text())
        for extra in ("preferences", "claude_feedback_drafts", "evidence"):
            with self.subTest(ledger_extra=extra):
                self.assertTrue(schema_validate.validate({**ledger, extra: {}}, schemas.ledger))
        evidence = json.loads((OPERATOR_FIXTURES / "evidence-sample.json").read_text())
        for extra in ("prompt", "body", "headers", "response"):
            with self.subTest(evidence_extra=extra):
                bad = copy.deepcopy(evidence)
                bad["lines"]["custom-acme-small"]["checks"]["smoke"][extra] = "x"
                self.assertTrue(schema_validate.validate(bad, schemas.evidence))
        bad = copy.deepcopy(evidence)
        bad["lines"]["custom-acme-small"]["checks"]["context"] = {}
        self.assertTrue(schema_validate.validate(bad, schemas.evidence))

    def test_models_registry_overlay_is_null_or_the_channel_marker(self) -> None:
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "models.schema.json")
        document = strict_json.load(FIXTURE_ROOT / "catalog" / "models.json")
        cases = ((None, True), ({"channel": "codex"}, True), ({"channel": "claude"}, True),
                 ({"channel": "other"}, False), ({}, False), ({"channel": "codex", "name": "x"}, False),
                 ({"channel": "codex", "thinking": {}}, False))
        for value, ok in cases:
            with self.subTest(overlay=value):
                candidate = copy.deepcopy(document)
                candidate["models"]["sol"]["registry_overlay"] = value
                self.assertEqual(not schema_validate.validate(candidate, schema), ok)

    def test_retired_registry_overlay_is_optional_and_closed(self) -> None:
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "retired.schema.json")
        document = strict_json.load(FIXTURE_ROOT / "catalog" / "retired.json")
        self.assertNotIn("registry_overlay", document["retired"]["muse-spark"])  # legacy absence stays valid
        self.assertEqual(schema_validate.validate(document, schema), [])
        for value, ok in ((None, True), ({"channel": "codex"}, True), ({"channel": "codex", "x": 1}, False), ("codex", False)):
            with self.subTest(overlay=value):
                candidate = copy.deepcopy(document)
                candidate["retired"]["muse-spark"]["registry_overlay"] = value
                self.assertEqual(not schema_validate.validate(candidate, schema), ok)


# ------------------------------------------------------------------ operator origins
class OperatorOriginValidateLineTests(unittest.TestCase):
    def setUp(self) -> None:
        docs = _docs()
        self.providers = docs["providers"]["providers"]
        self.native = docs["native-contract"]

    def _entry(self, provider: str, **overrides) -> dict:
        entry = operator.derive_core_entry(
            "custom-x", _acme_line(wire_model="fixture-x"), provider, self.providers[provider],
            migrated=False, gateway_baseline="7.2.80", file="providers.d/x.json")
        entry.update(overrides)
        return entry

    def _check(self, entry: dict, origin: str = "operator", key: str = "custom-x") -> list[str]:
        return catalog.validate_line(key, entry, providers=self.providers, native_contract=self.native,
                                     taken_selectors={}, origin=origin)

    def test_unsupported_origin_raises(self) -> None:
        for origin in ("legacy-custom", "custom", ""):
            with self.assertRaises(ValueError):
                self._check(self._entry("kimi"), origin=origin)

    def test_no_contract_list_is_allowed_on_a_claude_compatible_provider(self) -> None:
        self.assertEqual(self._check(self._entry("kimi")), [])

    def test_list_refused_on_a_gateway_effort_pool(self) -> None:
        self.assertTrue(any("gateway-effort" in e for e in self._check(self._entry("openai"))))

    def test_migrated_exception_is_narrow(self) -> None:
        migrated = self._entry("kimi", selector="custom-x")
        self.assertEqual(self._check(migrated, origin="operator-migrated"), [])
        for bad, needle in (
            ({"efforts": ["high", "max"]}, 'exactly ["high"]'),
            ({"selector": "custom-y"}, "exact selector"),
            ({"provider": "openai"}, "direct provider"),
        ):
            with self.subTest(bad=bad):
                self.assertTrue(any(needle in e for e in self._check({**migrated, **bad}, origin="operator-migrated")), bad)

    def test_key_grammar_per_origin(self) -> None:
        entry = self._entry("kimi")
        self.assertTrue(self._check(entry, key="custom-9x"))
        self.assertTrue(self._check(entry, key="x"))
        migrated = {**entry, "selector": "custom-9x"}
        self.assertFalse([e for e in self._check(migrated, origin="operator-migrated", key="custom-9x") if "key" in e])

    def test_operator_lead_env_overlay_and_selector_namespace(self) -> None:
        entry = self._entry("kimi")
        self.assertTrue(any("no lead env" in e for e in self._check({**entry, "lead": {"effort": "high", "env": {"CLAUDE_CODE_X": "1"}}})))
        self.assertTrue(any("default effort" in e for e in self._check({**entry, "lead": {"effort": "ultracode", "env": {}}})))
        self.assertTrue(any("registry_overlay" in e for e in self._check({**entry, "registry_overlay": {"channel": "claude"}})))
        self.assertTrue(any("namespace" in e for e in self._check({**entry, "selector": "other-x"})))

    def test_anthropic_pool_line_uses_its_canonical_wire_and_needs_no_route(self) -> None:
        entry = operator.derive_core_entry(
            "custom-x", _acme_line(wire_model="claude-fixture-9", context={"declared_tokens": 1000000, "source": "operator"}),
            "anthropic", self.providers["anthropic"], migrated=False, gateway_baseline="7.2.80", file="f")
        self.assertEqual(entry["selector"], "claude-fixture-9[1m]")
        self.assertEqual(self._check(entry), [])
        self.assertTrue(any("canonical" in e for e in self._check({**entry, "selector": "custom-x[1m]"})))


# ------------------------------------------------------------------ loader resolution
class LoaderResolutionTests(unittest.TestCase):
    def test_fixture_layer_resolves_every_shape(self) -> None:
        layer = _layer()
        self.assertEqual(_texts(layer), [])
        modes = {key: line.selector_mode for key, line in layer.lines.items()}
        self.assertEqual(modes, {
            "custom-acme-large": "ordinary_map", "custom-acme-small": "ordinary_list",
            "custom-beta-one": "ordinary_list", "custom-claude-fixture": "client_effort_existing_pool",
            "custom-gpt-fixture": "gateway_effort_existing_pool", "custom-kimi-next": "ordinary_map",
            "custom-lan-model": "ordinary_list",
        })
        self.assertEqual(set(layer.providers), {"acme", "beta", "lanbox"})
        self.assertEqual(layer.route_status, {"acme": "unapproved", "beta": "unapproved", "lanbox": "keyless"})
        schemas = _schemas()
        for key, line in layer.lines.items():
            with self.subTest(line=key):
                self.assertEqual(schema_validate.validate(line.core_entry, schemas.entry), [])
                self.assertNotIn("family", line.core_entry)
                self.assertEqual(line.core_entry["status"], "new")
                self.assertIsNone(line.core_entry["registry_overlay"])
                self.assertEqual(line.core_entry["lead"], {"effort": line.core_entry["default_effort"], "env": {}})
                self.assertEqual(line.core_entry["capabilities"], ["lead"])
                self.assertEqual(line.core_entry["roles"], [])
                context = line.core_entry["context"]
                self.assertEqual(context["validated_tokens"], min(context["declared_tokens"], catalog.CUSTOM_VALIDATED_CAP))
                self.assertEqual(context["ordinary_profile"], f"custom-{context['declared_tokens']}")
                self.assertIn("not benchmark-verified", context["qualification"])
                self.assertEqual(layer.metadata[key]["origin"], "operator")

    def test_explicit_origin_and_family_metadata(self) -> None:
        layer = _layer()
        self.assertEqual(layer.metadata["custom-kimi-next"]["provider_origin"], "catalog")
        self.assertEqual(layer.metadata["custom-acme-small"]["provider_origin"], "operator")
        self.assertEqual(layer.lines["custom-claude-fixture"].family, "anthropic")
        self.assertEqual(layer.providers["acme"].entry["origin"], "operator")

    def test_channels_come_from_the_pool_not_provider_ids(self) -> None:
        providers = _docs()["providers"]["providers"]
        self.assertEqual(operator.overlay_channel(providers["anthropic"]), "claude")
        self.assertEqual(operator.overlay_channel(providers["openai"]), "codex")
        self.assertIsNone(operator.overlay_channel(providers["kimi"]))
        for name in ("claude", "codex"):
            files = {name: {"version": 1, "lines": {}}}
            self.assertTrue(any("provider block" in t for t in _texts(_layer(files))))

    def test_line_failure_isolates_to_the_line(self) -> None:
        files = _fixture_files()
        files["acme"]["lines"]["custom-acme-bad"] = _acme_line(efforts=["ultracode"], default_effort="ultracode")
        layer = _layer(files)
        self.assertIn("custom-acme-small", layer.lines)
        self.assertNotIn("custom-acme-bad", layer.lines)
        self.assertTrue(any("ultracode" in t for t in _texts(layer)))

    def test_provider_failure_isolates_to_the_file(self) -> None:
        files = _fixture_files()
        files["acme"]["provider"]["base_url"] = "https://user@api.acme.example/anthropic"
        layer = _layer(files)
        self.assertNotIn("acme", layer.providers)
        self.assertFalse([k for k in layer.lines if k.startswith("custom-acme")])
        self.assertIn("custom-beta-one", layer.lines)

    def test_agent_requests_are_staged_and_only_static_problems_refuse(self) -> None:
        """Requested agents/roles load inert; a role without
        the capability, an unknown role and agents on an unaudited route
        (the LAN kind, E07) still refuse to load."""

        files = {"acme": _fixture_files()["acme"], "lanbox": _fixture_files()["lanbox"]}
        for extra in ({"capabilities": ["lead", "agents"], "roles": ["cm-reviewer"]},
                      {"capabilities": ["lead", "agents"], "roles": ["cm-explorer"]},
                      {"capabilities": ["lead", "agents"], "roles": "all"}):
            with self.subTest(extra=extra):
                files["acme"]["lines"]["custom-acme-small"] = _acme_line(**extra)
                layer = _layer(files)
                self.assertIn("custom-acme-small", layer.lines)
                entry = layer.lines["custom-acme-small"].core_entry
                self.assertEqual(entry["capabilities"], extra["capabilities"])
                self.assertEqual(entry["roles"], extra.get("roles", []))
        for extra, text in (({"roles": ["cm-explorer"]}, 'roles need the "agents" capability'),
                            ({"capabilities": ["lead", "agents"], "roles": ["cm-nobody"]},
                             "unknown agent role(s) cm-nobody"),
                            ({"capabilities": ["lead", "agents"]}, "requires at least one role")):
            with self.subTest(extra=extra):
                files["acme"]["lines"]["custom-acme-small"] = _acme_line(**extra)
                layer = _layer(files)
                self.assertNotIn("custom-acme-small", layer.lines)
                self.assertTrue(any(text in t for t in _texts(layer)), _texts(layer))
        files["acme"]["lines"]["custom-acme-small"] = _acme_line()
        files["lanbox"]["lines"]["custom-lan-model"]["capabilities"] = ["lead", "agents"]
        layer = _layer(files)
        self.assertNotIn("custom-lan-model", layer.lines)
        self.assertIn('providers.d/lanbox.json: lines.custom-lan-model.capabilities: "agents" needs a '
                      "retention-audited route; openai-compatible-lan is lead-only until the compat audit", _texts(layer))

    def test_provenance_needs_a_source_ref(self) -> None:
        files = {"acme": _fixture_files()["acme"]}
        files["acme"]["lines"]["custom-acme-small"]["context"] = {"declared_tokens": 65536, "source": "docs"}
        self.assertTrue(any("source_ref" in t for t in _texts(_layer(files))))

    def test_keyed_generic_compat_is_gated(self) -> None:
        files = {"acme": _fixture_files()["acme"]}
        files["acme"]["provider"]["kind"] = "openai-compatible"
        docs = _docs()
        docs["gateway"]["audits"] = {"openai_compat_keyed": False}
        layer = _layer(files, docs=docs)
        self.assertNotIn("acme", layer.providers)
        self.assertTrue(any(catalog.KEYED_AUDIT_CLOSED in t for t in _texts(layer)))

    def test_conservative_scrub_names_include_unapproved_and_invalid(self) -> None:
        files = _fixture_files()
        files["zed"] = copy.deepcopy(files["acme"])
        files["zed"]["provider"]["auth"]["secret_ref"] = "env:ZED_API_KEY"
        files["zed"]["provider"]["base_url"] = "http://zed.example"  # invalid: keyed http
        files["zed"]["lines"] = {}
        layer = _layer(files)
        self.assertNotIn("zed", layer.providers)
        self.assertEqual(layer.secret_names, frozenset({"ACME_API_KEY", "BETA_API_KEY", "ZED_API_KEY"}))


class TrustVocabularyTests(unittest.TestCase):
    """Operator declarations select trusted vocabulary and obey pool constraints."""

    def test_operator_selects_but_cannot_extend_t0_vocabulary(self) -> None:
        cases = {
            "unknown contract": lambda f: f["acme"]["provider"].__setitem__("payload_contracts", ["made-up-contract"]),
            "contract of another adapter": lambda f: f["acme"]["provider"].__setitem__("payload_contracts", ["reasoning-effort-high"]),
            "authored adapter": lambda f: f["acme"]["provider"].__setitem__("adapter", "cliproxy-custom-v1"),
            "authored lead": lambda f: f["acme"]["lines"]["custom-acme-small"].__setitem__("lead", {"effort": "high", "env": {"CLAUDE_CODE_X": "1"}}),
            "ultracode": lambda f: f["acme"]["lines"]["custom-acme-small"].update(efforts=["ultracode"], default_effort="ultracode"),
            "unused contract on line": lambda f: f["acme"]["lines"]["custom-acme-large"].__setitem__("efforts", {"high": "output-config-max"}),
            "pool family change": lambda f: f["anthropic"]["lines"]["custom-claude-fixture"].__setitem__("family", "other"),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                files = _fixture_files()
                mutate(files)
                self.assertTrue(_texts(_layer(files)), name)

    def test_operator_adds_line_on_existing_pool_but_not_a_pool(self) -> None:
        layer = _layer()
        claude = layer.lines["custom-claude-fixture"].core_entry
        self.assertEqual(claude["selector"], "claude-fixture-9[1m]")
        codex = layer.lines["custom-gpt-fixture"].core_entry
        self.assertEqual({lvl: spec["selector"] for lvl, spec in codex["efforts"].items()},
                         {"high": "custom-gpt-fixture-high[1m]", "xhigh": "custom-gpt-fixture-xhigh[1m]"})
        refusals = {
            "pool provider block": lambda f: f["openai"].__setitem__("provider", copy.deepcopy(f["acme"]["provider"])),
            "list on codex": lambda f: f["openai"]["lines"]["custom-gpt-fixture"].update(efforts=["high"]),
            "map on claude": lambda f: f["anthropic"]["lines"]["custom-claude-fixture"].update(efforts={"high": "x"}),
            "opaque claude wire": lambda f: f["anthropic"]["lines"]["custom-claude-fixture"].__setitem__("wire_model", "fixture-opaque"),
            "codex contract outside the pool": lambda f: f["openai"]["lines"]["custom-gpt-fixture"].__setitem__("efforts", {"max": "reasoning-effort-max"}),
        }
        for name, mutate in refusals.items():
            with self.subTest(case=name):
                files = _fixture_files()
                mutate(files)
                self.assertTrue(_texts(_layer(files)), name)


class OverlayInputTests(unittest.TestCase):
    """Pool lines derive a closed overlay projection."""

    def test_projection_of_pool_lines(self) -> None:
        layer = _layer()
        providers = _docs()["providers"]["providers"]
        claude = layer.lines["custom-claude-fixture"]
        self.assertEqual(operator.overlay_projection(claude, providers["anthropic"]), {
            "channel": "claude", "max_context_length": 1000000, "max_completion_tokens": 64000,
            "thinking_levels": ["high", "max"]})
        codex = layer.lines["custom-gpt-fixture"]
        self.assertEqual(operator.overlay_projection(codex, providers["openai"]),
                         {"channel": "codex", "max_context_length": 400000, "thinking_levels": ["high", "xhigh"]})
        self.assertIsNone(operator.overlay_projection(layer.lines["custom-kimi-next"], providers["kimi"]))
        schema = _schemas().ledger["properties"]["aliases"]["additionalProperties"]["properties"]["overlay"]
        for line, pid in ((claude, "anthropic"), (codex, "openai")):
            self.assertEqual(schema_validate.validate(operator.overlay_projection(line, providers[pid]), schema), [])

    def test_loader_rules_refuse_bad_inputs(self) -> None:
        self.assertIsNotNone(operator.overlay_problem({"channel": "codex", "max_context_length": 1}, "vendor/model"))
        self.assertIsNotNone(operator.overlay_problem({"channel": "codex", "max_context_length": 2**63}, "m"))
        self.assertIsNotNone(operator.overlay_problem({"channel": "codex", "max_context_length": True}, "m"))
        self.assertIsNotNone(operator.overlay_problem({"channel": "claude", "max_context_length": 1}, "claude-x"))
        self.assertIsNotNone(operator.overlay_problem({"channel": "claude", "max_context_length": 1,
                                                       "thinking_levels": ["ultra"]}, "claude-x"))
        self.assertIsNone(operator.overlay_problem({"channel": "claude", "max_context_length": 1,
                                                    "thinking_levels": ["low", "max"]}, "claude-x"))
        files = _fixture_files()
        files["openai"]["lines"]["custom-gpt-fixture"]["wire_model"] = "vendor/gpt-fixture"
        layer = _layer(files)
        self.assertNotIn("custom-gpt-fixture", layer.lines)
        self.assertTrue(any("overlay model name" in t for t in _texts(layer)))


class CollisionTests(unittest.TestCase):
    """Namespace and collision scope; the smallest valid unit survives."""

    def test_selector_collisions_with_trusted_names_refuse_the_t2_line(self) -> None:
        def claude_wire(files: dict, wire: str) -> None:
            files["anthropic"]["lines"]["custom-claude-fixture"]["wire_model"] = wire

        docs = _docs()
        docs["retired"]["retired"]["fixture-old"] = {
            "successor": None, "reason": "fixture", "since_catalog": 1, "provider": "anthropic",
            "last_wire": "claude-fixture-old", "display": "Old", "context_tokens": 1000000,
            "capabilities": ["lead"], "roles": [], "selectors": {"claude-fixture-old[1m]": None},
        }
        cases = {
            "passthrough route without a line": ("claude-fable-5-1", "passthrough route of anthropic"),
            "retired selector": ("claude-fixture-old", "retired key fixture-old"),
            "render sentinel": ("claude-multi-render-x", "sentinel"),
        }
        for name, (wire, needle) in cases.items():
            with self.subTest(case=name):
                files = _fixture_files()
                claude_wire(files, wire)
                layer = _layer(files, docs=docs)
                self.assertNotIn("custom-claude-fixture", layer.lines)
                self.assertIn("custom-gpt-fixture", layer.lines)  # the rest of the layer survives
                self.assertTrue(any(needle in t for t in _texts(layer)), _texts(layer))
        files = _fixture_files()
        legacy = json.loads((OPERATOR_FIXTURES / "custom.json").read_text())
        files["kimi"]["lines"]["custom-zeta-pro"] = _acme_line(wire_model="fixture-other")
        layer = _layer(files, legacy=legacy)
        self.assertNotIn("custom-zeta-pro", layer.lines)
        self.assertIn("custom-kimi-next", layer.lines)
        self.assertTrue(any("legacy custom model zeta-pro" in t for t in _texts(layer)))
        files = _fixture_files()
        files["kimi"]["lines"]["custom-kimi-legacy-wire"] = _acme_line(wire_model="kimi-fixture-side")
        self.assertNotIn("custom-kimi-legacy-wire", _layer(files, legacy=legacy).lines)

    def test_two_t2_declarations_of_one_selector_or_route_both_refused(self) -> None:
        files = _fixture_files()
        files["acme"]["lines"]["custom-dup"] = _acme_line(wire_model="dup-1")
        files["beta"]["lines"]["custom-dup"] = _acme_line(wire_model="dup-2")
        layer = _layer(files)
        self.assertNotIn("custom-dup", layer.lines)
        self.assertIn("custom-acme-small", layer.lines)
        self.assertIn("custom-beta-one", layer.lines)
        files = _fixture_files()
        files["acme"]["lines"]["custom-acme-twin"] = _acme_line(wire_model="acme-small-1")
        layer = _layer(files)
        self.assertNotIn("custom-acme-twin", layer.lines)
        self.assertNotIn("custom-acme-small", layer.lines)
        self.assertIn("custom-acme-large", layer.lines)

    def test_three_and_four_declarations_of_one_key_all_refused(self) -> None:
        """A third (or fourth) declaration never reinstates a
        collided key; every owner is diagnosed."""

        lan = _fixture_files()["lanbox"]
        for count in (3, 4):
            files = {}
            for index, pid in enumerate(("aaa", "bbb", "ccc", "ddd")[:count]):
                document = copy.deepcopy(lan)
                document["provider"]["base_url"] = f"http://box{index}.lan:8010/v1"
                files[pid] = document
            with self.subTest(count=count):
                layer = _layer(files)
                self.assertNotIn("custom-lan-model", layer.lines)
                self.assertNotIn("custom-lan-model", layer.metadata)
                owners = {p.file for p in layer.problems if p.code == "collision"}
                self.assertEqual(owners, {f"providers.d/{pid}.json" for pid in files})
        # An owner whose declaration failed later checks still counts as one.
        files = {pid: copy.deepcopy(lan) for pid in ("aaa", "bbb")}
        files["bbb"]["provider"]["base_url"] = "http://box1.lan:8010/v1"
        files["aaa"]["lines"]["custom-lan-model"]["display"] = "LAN a"
        self.assertNotIn("custom-lan-model", _layer(files).lines)

    def test_catalog_route_displaces_the_t2_line(self) -> None:
        docs = _docs()
        files = {"kimi": _fixture_files()["kimi"]}
        kimi_wire = next(e["wire_model"] for e in docs["models"]["models"].values() if e["provider"] == "kimi")
        files["kimi"]["lines"]["custom-kimi-next"]["wire_model"] = kimi_wire
        layer = _layer(files, docs=docs)
        self.assertIn("custom-kimi-next", layer.displaced)
        self.assertNotIn("custom-kimi-next", layer.lines)
        self.assertTrue(any("admit or bind that line instead" in t for t in _texts(layer)))


class SecretOriginTests(unittest.TestCase):
    """One normalized origin per secret name (T1 > approved T2 > legacy)."""

    def test_t1_wins_over_a_conflicting_t2(self) -> None:
        files = _fixture_files()
        files["acme"]["provider"]["auth"]["secret_ref"] = "env:KIMI_CLAUDE_API_KEY"
        layer = _layer(files)
        self.assertNotIn("acme", layer.providers)
        self.assertTrue(any("catalog provider kimi" in t for t in _texts(layer)))
        files["acme"]["provider"]["base_url"] = "https://api.kimi.com/other-path"
        self.assertIn("acme", _layer(files).providers)

    def test_conflicting_t2_files_both_fail(self) -> None:
        files = _fixture_files()
        files["beta"]["provider"]["auth"] = {"kind": "bearer", "secret_ref": "env:ACME_API_KEY"}
        layer = _layer(files)
        self.assertNotIn("acme", layer.providers)
        self.assertNotIn("beta", layer.providers)
        self.assertIn("lanbox", layer.providers)

    def test_secret_bearing_foreign_listing_fails(self) -> None:
        files = _fixture_files()
        files["acme"]["provider"]["listing"] = {"url": "https://elsewhere.example/v1/models", "shape": "openai", "auth": "bearer"}
        self.assertNotIn("acme", _layer(files).providers)
        files["acme"]["provider"]["listing"] = {"url": "https://elsewhere.example/v1/models", "shape": "openai", "auth": "none"}
        self.assertIn("acme", _layer(files).providers)

    def test_legacy_routes_join_the_index(self) -> None:
        legacy = json.loads((OPERATOR_FIXTURES / "custom.json").read_text())
        base = _layer({}, legacy=legacy)
        self.assertEqual(base.legacy_dropped, {})
        steal_t1 = copy.deepcopy(legacy)
        steal_t1["providers"]["zeta"]["secret_env"] = "KIMI_CLAUDE_API_KEY"
        self.assertIn("zeta", _layer({}, legacy=steal_t1).legacy_dropped)
        reserved = copy.deepcopy(legacy)
        # Every keep-list name is also under a reserved prefix (the list
        # carries no personal MCP key).
        reserved["providers"]["zeta"]["secret_env"] = "CLAUDE_CODE_MESSAGING_TOKEN"
        self.assertIn("reserved prefix", _layer({}, legacy=reserved).legacy_dropped["zeta"])
        files = {"acme": _fixture_files()["acme"]}
        files["acme"]["provider"]["auth"]["secret_ref"] = "env:ZETA_API_KEY"
        unapproved = _layer(files, legacy=legacy)
        self.assertNotIn("acme", unapproved.providers)
        self.assertEqual(unapproved.legacy_dropped, {})
        approved_route = _layer(files).providers["acme"]
        ledger = _ledger(routes={"acme": _route(approved_route)})
        approved = _layer(files, legacy=legacy, ledger=ledger)
        self.assertIn("acme", approved.providers)
        self.assertIn("zeta", approved.legacy_dropped)
        merged = operator.merge_docs(_docs(), approved, legacy=legacy)
        self.assertNotIn("zeta", merged["providers"]["providers"])
        self.assertNotIn("zeta-pro", merged["models"]["models"])
        self.assertIn("idle", merged["providers"]["providers"])


class RedactionAndHeaderTests(unittest.TestCase):
    """Secret literals never appear in diagnostics; header policy."""

    def test_secret_literals_refuse_the_file_without_echo(self) -> None:
        literal = "sk-fake" + "0123456789abcdefghij"
        files = _fixture_files()
        files["acme"]["notes"] = f"key is {literal}"
        layer = _layer(files)
        self.assertNotIn("acme", layer.providers)
        self.assertTrue(any("a secret value is not allowed" in t for t in _texts(layer)))
        self.assertFalse(any(literal in t or "0123456789" in t for t in _texts(layer)))
        raw = {"acme": b'{"version": 1, "notes": "' + literal.encode() + b'", broken'}
        layer = operator.validate_layer(_docs(), raw, schemas=_schemas())
        self.assertTrue(any("a secret value is not allowed" in t for t in _texts(layer)))
        self.assertFalse(any(literal in t for t in _texts(layer)))

    def test_stored_values_are_compared_in_memory_only(self) -> None:
        stored = "dummy-stored-credential-value"
        files = _fixture_files()
        files["beta"]["lines"]["custom-beta-one"]["display"] = f"x{stored}"[:96]
        layer = _layer(files, secret_values=frozenset({stored}))
        self.assertNotIn("beta", layer.providers)
        self.assertFalse(any(stored in t for t in _texts(layer)))

    def test_secret_bearing_keys_duplicates_and_prefixes_never_echo(self) -> None:
        literal = "sk-fake" + "0123456789abcdefghij"
        stored = "dummy-stored-credential-value"
        cases = {}
        files = _fixture_files()
        files["acme"][literal] = "x"  # token-shaped extra key (was: `unexpected key` diagnostic)
        cases["token key"] = (_raw(files), frozenset(), "acme")
        files = _fixture_files()
        files["beta"]["lines"]["custom-beta-one"]["notes"] = {stored: stored}  # stored value as key and value
        cases["stored key+value"] = (_raw(files), frozenset({stored}), "beta")
        files = _fixture_files()
        files["acme"]["provider"]["headers"] = {"X-Trace": "sha256:" + literal}  # no trusted-data exemption
        cases["sha256 prefix"] = (_raw(files), frozenset(), "acme")
        dup = b'{"version": 1, "' + stored.encode() + b'": 1, "' + stored.encode() + b'": 2}'
        cases["duplicate stored key"] = ({"acme": dup}, frozenset({stored}), "acme")
        cases["duplicate plain key"] = ({"acme": b'{"version": 1, "k": 1, "k": 2}'}, frozenset(), "acme")
        cases["token file name"] = ({"sk-fakeabcdefgh1234": _raw(_fixture_files())["acme"]}, frozenset(), "sk-fakeabcdefgh1234")
        for name, (raw, secret_values, refused) in cases.items():
            with self.subTest(case=name):
                layer = operator.validate_layer(_docs(), raw, schemas=_schemas(), secret_values=secret_values)
                texts = _texts(layer)
                self.assertTrue(texts)
                for text in texts:
                    for needle in (literal, "0123456789", stored, "sk-fakeabcdefgh1234"):
                        self.assertNotIn(needle, text)
                self.assertNotIn(refused, layer.providers)
                self.assertTrue(any(p.code in {"secret-literal", "json"} for p in layer.problems))
        layer = operator.validate_layer(_docs(), cases["duplicate plain key"][0], schemas=_schemas())
        self.assertEqual([p.subject for p in layer.problems], ['duplicate key "k"'])

    def test_reader_problems_are_scrubbed(self) -> None:
        problem = operator._problem("file-name", "providers.d/sk-fakeabcdefgh1234.JSON", "", "ignored")
        self.assertNotIn("sk-fake", operator._scrub(problem).text())
        stored = "dummy-stored-credential-value"
        layer = operator.validate_layer(_docs(), {}, schemas=_schemas(), secret_values=frozenset({stored}),
                                        read_problems=[operator._problem("unsafe-file", f"providers.d/{stored}", "", "x")])
        self.assertFalse(any(stored in t for t in _texts(layer)))

    def test_header_policy(self) -> None:
        refused = {"Authorization": "x", "Host": "evil.example", "X-Auth-Token": "x", "X-Title": "a\r\nInjected: 1",
                   "X-Tag": "${ACME_API_KEY}", "Bad Header": "x", "Cookie": "a=b"}
        for name, value in refused.items():
            with self.subTest(header=name):
                self.assertIsNotNone(operator.header_problem(name, value))
                files = _fixture_files()
                files["acme"]["provider"]["headers"] = {name: value}
                self.assertNotIn("acme", _layer(files).providers)
        self.assertIsNone(operator.header_problem("X-Title", "claude-multi fixture"))
        files = _fixture_files()
        files["lanbox"]["provider"]["headers"] = {"X-Title": "fixture"}
        self.assertNotIn("lanbox", _layer(files).providers)
        files = _fixture_files()
        files["acme"]["provider"]["headers"] = {f"X-H{i}": "v" for i in range(9)}
        self.assertNotIn("acme", _layer(files).providers)

    def test_reserved_secret_names_refused(self) -> None:
        for name in ("ANTHROPIC_KEY", "CLAUDE_KEY", "X_FILE_DESCRIPTOR", "GITHUB_TOKEN", "PGSTORE_X",
                     "CLAUDE_CODE_MESSAGING_TOKEN", "DISABLE_AUTO_COMPACT"):
            with self.subTest(name=name):
                files = _fixture_files()
                files["acme"]["provider"]["auth"]["secret_ref"] = f"env:{name}"
                self.assertNotIn("acme", _layer(files).providers)

    def test_deny_constants_are_shared_by_reference(self) -> None:
        self.assertIs(proxy.GATEWAY_ENV_DENY_NAMES, secret_store.GATEWAY_ENV_DENY_NAMES)
        self.assertIs(proxy.GATEWAY_ENV_DENY_PREFIXES, secret_store.GATEWAY_ENV_DENY_PREFIXES)
        self.assertIs(compiler.ENV_UNSET_KEEP, secret_store.ENV_UNSET_KEEP)


class EndpointPolicyTests(unittest.TestCase):
    """Endpoint policy is lexical only: no DNS or redirect claim."""

    def test_keyed_routes(self) -> None:
        ok = {
            "https://API.Example.com/anthropic/": ("https://api.example.com", "https://api.example.com/anthropic"),
            "https://api.example.com:443/v1": ("https://api.example.com", "https://api.example.com/v1"),
            "https://api.example.com:8443": ("https://api.example.com:8443", "https://api.example.com:8443"),
            "https://[2001:db8::1]/v1": ("https://[2001:db8::1]", "https://[2001:db8::1]/v1"),
        }
        for url, expected in ok.items():
            with self.subTest(url=url):
                self.assertEqual(operator.normalize_endpoint(url, keyed=True, lan=False), expected)
        refused = ("http://api.example.com", "https://user:pw@api.example.com", "https://api.example.com/#x",
                   "https://api.example.com/?a=1", "https://api.example.com:99999", "https://api.example.com:0",
                   "ftp://api.example.com", "https://127.1", "https://2130706433", "https://0x7f.0.0.1",
                   "https://api.example.com.", "https://169.254.169.254", "https://[fe80::1]", "https://0.0.0.0",
                   "https://metadata.google.internal", "https://localhost:8317", "https://127.0.0.1:8316",
                   "https://exämple.com", "https://api example.com", "https:///v1", "https://100.100.100.200")
        for url in refused:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    operator.normalize_endpoint(url, keyed=True, lan=False)

    def test_keyless_lan_hosts(self) -> None:
        for url in ("http://box.lan:8010/v1", "http://192.168.1.20:8080", "http://localhost:11434", "http://gpu:9000",
                    "http://10.0.0.5", "http://nas.home.arpa"):
            with self.subTest(url=url):
                operator.normalize_endpoint(url, keyed=False, lan=True)
        for url in ("http://api.example.com", "http://8.8.8.8", "http://127.0.0.1:8317", "http://169.254.1.1"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    operator.normalize_endpoint(url, keyed=False, lan=True)

    def test_gateway_port_comes_from_the_catalog_too(self) -> None:
        docs = _docs()
        docs["gateway"]["gateway"]["base_url"] = "http://127.0.0.1:18317"
        self.assertIn(18317, operator.gateway_ports(docs))

    def test_auth_kind_rules(self) -> None:
        cases = {
            "none on keyed kind": {"kind": "none"},
            "header without x-api-key": {"kind": "header", "secret_ref": "env:ACME_API_KEY"},
            "bearer with header": {"kind": "bearer", "secret_ref": "env:ACME_API_KEY", "header": "x-api-key"},
            "bearer without ref": {"kind": "bearer"},
        }
        for name, auth in cases.items():
            with self.subTest(case=name):
                files = _fixture_files()
                files["acme"]["provider"]["auth"] = auth
                self.assertNotIn("acme", _layer(files).providers)
        files = _fixture_files()
        files["lanbox"]["provider"]["auth"] = {"kind": "bearer", "secret_ref": "env:LAN_API_KEY"}
        self.assertNotIn("lanbox", _layer(files).providers)


class RouteApprovalTests(unittest.TestCase):
    """A route edit disables rendering until re-approved."""

    def _approved(self, files: dict) -> operator.OperatorLedger:
        layer = _layer(files)
        return _ledger(routes={pid: _route(p) for pid, p in layer.providers.items() if p.auth_kind != "none"})

    def test_origin_edit_lapses_and_path_edit_keeps_a_bearer_route(self) -> None:
        files = _fixture_files()
        ledger = self._approved(files)
        self.assertEqual(_layer(files, ledger=ledger).route_status["acme"], "approved")
        moved = copy.deepcopy(files)
        moved["acme"]["provider"]["base_url"] = "https://api.acme-other.example/anthropic"
        layer = _layer(moved, ledger=ledger)
        self.assertEqual(layer.route_status["acme"], "changed")
        self.assertEqual(layer.route_changes["acme"], ("https://api.acme.example", "https://api.acme-other.example"))
        path_only = copy.deepcopy(files)
        path_only["acme"]["provider"]["base_url"] = "https://api.acme.example/v2/anthropic"
        self.assertEqual(_layer(path_only, ledger=ledger).route_status["acme"], "approved")

    def test_header_auth_route_binds_the_full_base_url(self) -> None:
        files = _fixture_files()
        ledger = self._approved(files)
        self.assertEqual(_layer(files, ledger=ledger).route_status["beta"], "approved")
        files["beta"]["provider"]["base_url"] = "https://beta.example/v2"
        self.assertEqual(_layer(files, ledger=ledger).route_status["beta"], "changed")

    def test_secret_rename_lapses_approval(self) -> None:
        files = _fixture_files()
        ledger = self._approved(files)
        files["acme"]["provider"]["auth"]["secret_ref"] = "env:ACME_OTHER_KEY"
        self.assertEqual(_layer(files, ledger=ledger).route_status["acme"], "changed")


class DigestTests(unittest.TestCase):
    def _digest(self, files: dict, key: str) -> str:
        return _layer(files).lines[key].definition_digest

    def test_admission_tracks_definition_not_cosmetics(self) -> None:
        files = _fixture_files()
        before = self._digest(files, "custom-acme-large")
        cosmetic = [
            lambda l: l.__setitem__("display", "Renamed"), lambda l: l.__setitem__("notes", "n"),
            lambda l: l.__setitem__("routing_note", "r"), lambda l: l.__setitem__("generation", "9"),
            lambda l: l["context"].__setitem__("source_ref", "https://docs.acme.example/other"),
        ]
        for index, mutate in enumerate(cosmetic):
            with self.subTest(cosmetic=index):
                changed = copy.deepcopy(files)
                mutate(changed["acme"]["lines"]["custom-acme-large"])
                self.assertEqual(self._digest(changed, "custom-acme-large"), before)
        routing = [
            lambda f: f["acme"]["lines"]["custom-acme-large"].__setitem__("wire_model", "acme-large-2"),
            lambda f: f["acme"]["lines"]["custom-acme-large"]["context"].__setitem__("declared_tokens", 131072),
            lambda f: f["acme"]["provider"].__setitem__("base_url", "https://api.acme.example/v2"),
            lambda f: f["acme"]["lines"]["custom-acme-large"].__setitem__("output", {"declared_tokens": 8192, "source": "operator"}),
        ]
        for index, mutate in enumerate(routing):
            with self.subTest(routing=index):
                changed = copy.deepcopy(files)
                mutate(changed)
                self.assertNotEqual(self._digest(changed, "custom-acme-large"), before)

    def test_catalog_contract_additions_do_not_lapse_a_lines_only_line(self) -> None:
        files = _fixture_files()
        docs = _docs()
        before = _layer(files, docs=docs).lines["custom-kimi-next"].definition_digest
        docs["providers"]["providers"]["kimi"]["payload_contracts"].append("output-config-high")
        self.assertEqual(_layer(files, docs=docs).lines["custom-kimi-next"].definition_digest, before)

    def test_field_hashes_name_what_changed(self) -> None:
        files = _fixture_files()
        before = _layer(files).metadata["custom-acme-large"]["field_hashes"]
        files["acme"]["lines"]["custom-acme-large"]["wire_model"] = "acme-large-2"
        after = _layer(files).metadata["custom-acme-large"]["field_hashes"]
        self.assertEqual(sorted(name for name in before if before[name] != after[name]), ["wire"])


class SchemaPropagationTests(unittest.TestCase):
    def test_composed_entry_schema_tracks_fixture_schema(self) -> None:
        temporary = Path(tempfile.mkdtemp(prefix="claude-multi-operator-schema-"))
        self.addCleanup(shutil.rmtree, temporary, True)
        shutil.copytree(FIXTURE_ROOT / "schemas", temporary / "schemas")
        path = temporary / "schemas" / "models.schema.json"
        os.chmod(path, 0o644)
        document = json.loads(path.read_text())
        entry = document["properties"]["models"]["additionalProperties"]
        entry["required"].append("propagation_probe")
        entry["properties"]["propagation_probe"] = {"type": "string"}
        path.write_text(json.dumps(document))
        schemas = operator.load_schemas(temporary)
        self.assertIn("propagation_probe", schemas.entry["required"])
        layer = _layer(schemas=schemas)
        self.assertEqual(layer.lines, {})
        self.assertTrue(any("propagation_probe" in t for t in _texts(layer)))
        profile = schemas.entry["properties"]["context"]["properties"]["ordinary_profile"]["oneOf"]
        self.assertEqual(profile[-1], {"type": "string", "pattern": "^custom-[0-9]{4,7}$"})


class ProvidersDirTests(unittest.TestCase):
    """Symlink safety: readable symlinks, never replaced; bounded reads."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-providers-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = self.root / "store"
        self.store.mkdir(mode=0o755)
        os.chmod(self.store, 0o755)
        self.directory = self.root / "providers.d"

    def _stored(self, name: str, document: dict, mode: int = 0o444) -> Path:
        path = self.store / name
        path.write_bytes(strict_json.pretty_file_bytes(document))
        os.chmod(path, mode)
        return path

    def test_hm_symlink_is_readable_but_never_replaced(self) -> None:
        state.ensure_private_dir(self.directory)
        acme = _fixture_files()["acme"]
        target = self._stored("acme.json", acme)
        (self.directory / "acme.json").symlink_to(target)
        read = operator.read_providers_dir(self.directory)
        self.assertEqual(read.problems, ())
        self.assertIn("acme", read.files)
        layer = operator.validate_layer(_docs(), read.files, schemas=_schemas())
        self.assertIn("acme", layer.providers)
        fragment = {"lines": {"custom-acme-new": _acme_line()}}
        with self.assertRaises(operator.OperatorError) as caught:
            operator.check_writable_target(self.directory, "acme", fragment, lambda: frozenset())
        message = str(caught.exception)
        self.assertTrue(message.startswith("providers.d/acme.json is read-only here (managed elsewhere) — add this to its source:\n"))
        self.assertIn('"custom-acme-new"', message)
        # The fragment is shown only after the literal screen.
        token = "sk-" + "reviewdummy" * 2
        stored = "stored-dummy-value-7"
        for shown, values, needle in (
                ({"lines": {"custom-acme-new": _acme_line(display=token)}}, lambda: frozenset(),
                 "it contains a secret-like or stored credential literal"),
                ({"lines": {"custom-acme-new": _acme_line(display=stored)}}, lambda: frozenset({stored}),
                 "it contains a secret-like or stored credential literal"),
                ({"lines": {"custom-acme-new": _acme_line()}}, None, "stored credentials could not be screened"),
                ({"lines": {"custom-acme-new": _acme_line()}}, lambda: None, "stored credentials could not be screened"),
                (None, lambda: frozenset(), "change it at its source")):
            with self.subTest(needle=needle), self.assertRaises(operator.OperatorError) as caught:
                operator.check_writable_target(self.directory, "acme", shown, values)
            message = str(caught.exception)
            self.assertIn(needle, message)
            self.assertNotIn(token, message)
            self.assertNotIn(stored, message)
            self.assertNotIn("custom-acme-new", message)
        self.assertTrue((self.directory / "acme.json").is_symlink())
        self.assertEqual(target.read_bytes(), strict_json.pretty_file_bytes(acme))
        self.assertEqual(operator.check_writable_target(self.directory, "fresh", fragment), self.directory / "fresh.json")

    def test_symlinked_directory_is_read_only(self) -> None:
        self._stored("beta.json", _fixture_files()["beta"])
        os.chmod(self.store, 0o555)
        self.addCleanup(os.chmod, self.store, 0o755)
        self.directory.symlink_to(self.store)
        read = operator.read_providers_dir(self.directory)
        self.assertTrue(read.symlinked)
        self.assertIn("beta", read.files)
        with self.assertRaises(operator.OperatorError):
            operator.check_writable_target(self.directory, "gamma", {"lines": {}})

    def test_unsafe_entries_isolate_to_their_file(self) -> None:
        state.ensure_private_dir(self.directory)
        (self.directory / "good.json").write_bytes(strict_json.pretty_file_bytes(_fixture_files()["acme"]))
        (self.directory / "open.json").write_bytes(b"{}")
        os.chmod(self.directory / "open.json", 0o666)
        (self.directory / "big.json").write_bytes(b" " * (operator.READ_LIMIT_BYTES + 1))
        (self.directory / "gone.json").symlink_to(self.root / "missing.json")
        os.mkfifo(self.directory / "pipe.json", 0o600)
        (self.directory / "Bad_Name.json").write_bytes(b"{}")
        (self.directory / operator.MIGRATION_MARKER).write_bytes(b"{}")
        read = operator.read_providers_dir(self.directory)
        self.assertEqual(set(read.files), {"good"})
        self.assertTrue(read.marker)
        codes = sorted(p.code for p in read.problems)
        self.assertEqual(codes, ["file-name", "too-large", "unreadable", "unsafe-file", "unsafe-file"])
        self.assertNotIn(operator.MIGRATION_MARKER.removesuffix(".json"), read.files)

    def test_writer_refuses_foreign_or_open_targets(self) -> None:
        state.ensure_private_dir(self.directory)
        (self.directory / "open.json").write_bytes(b"{}")
        os.chmod(self.directory / "open.json", 0o644)
        with self.assertRaises(operator.OperatorError):
            operator.check_writable_target(self.directory, "open", {"lines": {}})
        state.atomic_write(self.directory / "mine.json", b"{}")
        self.assertEqual(operator.check_writable_target(self.directory, "mine", {}), self.directory / "mine.json")
        with self.assertRaises(operator.OperatorError):
            operator.check_writable_target(self.directory, "../escape", {})

    def test_absent_directory_is_an_empty_layer(self) -> None:
        read = operator.read_providers_dir(self.root / "absent")
        self.assertEqual((read.exists, read.files, read.problems), (False, {}, ()))
        layer = operator.validate_layer(_docs(), read.files, schemas=_schemas())
        self.assertEqual((layer.providers, layer.lines, layer.problems), ({}, {}, ()))
        docs = _docs()
        merged = operator.merge_docs(docs, layer)
        self.assertEqual(merged, docs)

    def test_paths_are_home_relative(self) -> None:
        environ = {"HOME": "/h", "XDG_CONFIG_HOME": "/x", "XDG_STATE_HOME": "/s"}
        self.assertEqual(operator.providers_dir(environ), Path("/h/.config/claude-multi/providers.d"))
        self.assertEqual(operator.ledger_path(environ), Path("/h/.config/claude-multi/operator-ledger.json"))
        self.assertEqual(operator.evidence_path(environ), Path("/s/claude-multi/operator-evidence.json"))


class LedgerEvidenceTests(unittest.TestCase):
    def test_parse_and_fail_closed(self) -> None:
        schemas = _schemas()
        raw = (OPERATOR_FIXTURES / "ledger-empty.json").read_bytes()
        ledger = operator.parse_ledger(raw, schemas.ledger)
        self.assertEqual(ledger.admissions, {})
        for bad in (b"{", b'{"version": 1}', raw.replace(b'"routes": {}', b'"routes": {"Bad": {}}'),
                    raw.replace(b'"admissions": {}', b'"admissions": {"notcustom": {}}')):
            with self.subTest(bad=bad[:30]):
                with self.assertRaises(operator.OperatorError):
                    operator.parse_ledger(bad, schemas.ledger)
        evidence = operator.parse_evidence((OPERATOR_FIXTURES / "evidence-sample.json").read_bytes(), schemas.evidence)
        self.assertEqual(evidence.lines["custom-acme-small"]["checks"]["smoke"]["result"], "pass")

    def test_load_is_private_and_absent_is_none(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="claude-multi-ledger-"))
        os.chmod(home, 0o700)
        self.addCleanup(shutil.rmtree, home, True)
        environ = {"HOME": str(home), "XDG_STATE_HOME": str(home / "state")}
        schemas = _schemas()
        self.assertIsNone(operator.load_ledger(environ, schemas))
        self.assertIsNone(operator.load_evidence(environ, schemas))
        path = operator.ledger_path(environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, (OPERATOR_FIXTURES / "ledger-empty.json").read_bytes())
        self.assertIsNotNone(operator.load_ledger(environ, schemas))
        os.chmod(path, 0o644)
        with self.assertRaises(operator.OperatorError):
            operator.load_ledger(environ, schemas)
        os.chmod(path, 0o600)
        path.rename(path.with_name("real.json"))
        path.symlink_to(path.with_name("real.json"))
        with self.assertRaises(operator.OperatorError):
            operator.load_ledger(environ, schemas)


class AdmissionPredicateTests(unittest.TestCase):
    def test_offered_needs_every_condition(self) -> None:
        files = _fixture_files()
        first = _layer(files)
        grant = lambda key: {"digest": first.lines[key].definition_digest, "at": "2026-09-30T00:00:00Z", "via": "admit",
                             "diagnostic": {"wire": first.lines[key].core_entry["wire_model"], "fields": {}}}
        ledger = _ledger(routes={"acme": _route(first.providers["acme"])},
                         admissions={k: grant(k) for k in ("custom-acme-small", "custom-kimi-next", "custom-lan-model")})
        layer = _layer(files, ledger=ledger)
        offered = lambda key, **kw: operator.operator_line_offered(
            key, layer=kw.get("layer", layer), ledger=kw.get("ledger", ledger),
            admitted_lines=kw.get("admitted", {key}), provider_enabled=kw.get("enabled", True))
        self.assertTrue(offered("custom-acme-small"))
        self.assertTrue(offered("custom-kimi-next"))
        self.assertTrue(offered("custom-lan-model"))
        self.assertFalse(offered("custom-acme-small", admitted=set()))
        self.assertFalse(offered("custom-acme-small", enabled=False))
        self.assertFalse(offered("custom-acme-small", ledger=None))
        self.assertFalse(offered("custom-acme-large"))  # no grant
        self.assertFalse(offered("custom-beta-one"))  # route unapproved
        edited = copy.deepcopy(files)
        edited["acme"]["lines"]["custom-acme-small"]["wire_model"] = "acme-small-2"
        self.assertFalse(offered("custom-acme-small", layer=_layer(edited, ledger=ledger)))


class DisplacedLinesTests(unittest.TestCase):
    """Catalog displacement is pure and deterministic, with no automatic successor."""

    def _candidate(self, docs: dict, wire: str, provider: str, key: str = "fixture-release") -> dict:
        candidate = copy.deepcopy(docs)
        line = copy.deepcopy(candidate["models"]["models"]["kimi-k3"])
        line.update(provider=provider, wire_model=wire)
        candidate["models"]["models"][key] = line
        return candidate

    def test_rows_name_admitted_collisions_without_successors(self) -> None:
        docs = _docs()
        files = _fixture_files()
        layer = _layer(files, docs=docs)
        ledger = _ledger(admissions={"custom-kimi-next": {
            "digest": layer.lines["custom-kimi-next"].definition_digest, "at": "2026-09-30T00:00:00Z",
            "via": "admit", "diagnostic": {"wire": "kimi-fixture-next", "fields": {}}}})
        candidate = self._candidate(docs, "kimi-fixture-next", "kimi")
        before = (copy.deepcopy(candidate), strict_json.canonical_bytes(ledger.admissions))
        rows = operator.displaced_lines(candidate, layer, ledger)
        self.assertEqual(rows, (operator.DisplacedLine("custom-kimi-next", "kimi", "kimi-fixture-next", ("fixture-release",), True),))
        self.assertEqual(operator.displaced_lines(candidate, layer, ledger), rows)
        self.assertEqual((candidate, strict_json.canonical_bytes(ledger.admissions)), before)
        self.assertFalse(operator.displaced_lines(candidate, layer, None)[0].admitted)
        self.assertEqual(operator.displaced_lines(docs, layer, ledger), ())
        other_provider = self._candidate(docs, "kimi-fixture-next", "deepseek")
        self.assertEqual(operator.displaced_lines(other_provider, layer, ledger), ())
        self.assertFalse(hasattr(rows[0], "successor"))

    def test_already_displaced_lines_are_reported(self) -> None:
        docs = _docs()
        files = {"kimi": _fixture_files()["kimi"]}
        kimi_wire = docs["models"]["models"]["kimi-k3"]["wire_model"]
        files["kimi"]["lines"]["custom-kimi-next"]["wire_model"] = kimi_wire
        layer = _layer(files, docs=docs)
        rows = operator.displaced_lines(docs, layer, None)
        self.assertEqual([(r.key, r.catalog_keys) for r in rows], [("custom-kimi-next", ("kimi-k3",))])

    def test_malformed_inputs_are_diagnosed(self) -> None:
        layer = _layer()
        for bad in ({}, {"models": []}, {"models": {"models": {"x": {}}}}):
            with self.subTest(bad=bad):
                with self.assertRaises(operator.OperatorError):
                    operator.displaced_lines(bad, layer, None)
        with self.assertRaises(operator.OperatorError):
            operator.displaced_lines(_docs(), layer, {"admissions": {}})
        with self.assertRaises(operator.OperatorError):
            operator.displaced_lines(_docs(), "layer", None)


class MergeTests(unittest.TestCase):
    def test_merge_never_mutates_and_keeps_the_alias_contract(self) -> None:
        docs = _docs()
        snapshot = copy.deepcopy(docs)
        layer = _layer(docs=docs)
        merged = operator.merge_docs(docs, layer)
        self.assertEqual(docs, snapshot)
        self.assertIs(merged["models"], merged["models-v2"])
        self.assertEqual(set(merged["models"]["models"]) - set(docs["models"]["models"]), set(layer.lines))
        self.assertEqual(merged["providers"]["providers"]["acme"]["origin"], "operator")
        self.assertNotIn("origin", merged["providers"]["providers"]["kimi"])

    def test_legacy_only_without_the_marker(self) -> None:
        legacy = json.loads((OPERATOR_FIXTURES / "custom.json").read_text())
        without = operator.merge_docs(_docs(), _layer({}, legacy=legacy), legacy=legacy)
        self.assertIn("zeta-pro", without["models"]["models"])
        marked = _layer({}, legacy=legacy, marker=True)
        self.assertNotIn("zeta-pro", operator.merge_docs(_docs(), marked, legacy=legacy)["models"]["models"])


class MigrationPreflightTests(unittest.TestCase):
    """Migration preflight is all-or-nothing, field-specific, zero writes."""

    def _legacy(self) -> dict:
        return json.loads((OPERATOR_FIXTURES / "custom.json").read_text())

    def test_valid_registry_plans_every_target(self) -> None:
        plan = operator.migration_preflight(self._legacy(), _docs(), schemas=_schemas())
        self.assertEqual(plan.problems, ())
        # The unreferenced legacy provider is listed as
        # skipped, never written: unreferenced legacy providers are not migration targets.
        self.assertEqual(set(plan.documents), {"zeta", "kimi"})
        self.assertEqual(plan.zero_model_providers, ("idle",))
        self.assertNotIn("provider", plan.documents["kimi"])
        line = plan.documents["zeta"]["lines"]["custom-zeta-pro"]
        self.assertEqual((line["selector"], line["legacy_key"], line["efforts"]), ("custom-zeta-pro", "zeta-pro", ["high"]))
        self.assertEqual(plan.documents["zeta"]["provider"]["independence_family"], "unknown")

    def test_migrated_lines_load_only_after_activation(self) -> None:
        plan = operator.migration_preflight(self._legacy(), _docs(), schemas=_schemas())
        pending = operator.validate_layer(_docs(), _raw(plan.documents), schemas=_schemas(), marker=False)
        self.assertEqual(pending.pending, ("custom-kimi-side", "custom-zeta-pro"))
        self.assertEqual(pending.lines, {})
        active = operator.validate_layer(_docs(), _raw(plan.documents), schemas=_schemas(), marker=True)
        self.assertEqual(_texts(active), [])
        self.assertEqual(set(active.lines), {"custom-kimi-side", "custom-zeta-pro"})
        for key, line in active.lines.items():
            with self.subTest(line=key):
                self.assertEqual(line.selector_mode, "migrated")
                self.assertEqual(line.core_entry["selector"], key)
                self.assertEqual(active.metadata[key]["origin"], "operator-migrated")

    def test_migrated_list_line_on_catalog_gateway_provider(self) -> None:
        plan = operator.migration_preflight(self._legacy(), _docs(), schemas=_schemas())
        layer = operator.validate_layer(_docs(), _raw({"kimi": plan.documents["kimi"]}), schemas=_schemas(), marker=True)
        entry = layer.lines["custom-kimi-side"].core_entry
        self.assertEqual((entry["efforts"], entry["selector"]), (["high"], "custom-kimi-side"))
        self.assertEqual(entry["context"]["client_tokens"], 262144)
        self.assertEqual(layer.lines["custom-kimi-side"].family, "moonshot")

    def test_offending_fields_are_named_and_nothing_is_planned(self) -> None:
        legacy = self._legacy()
        legacy["models"].update({
            "glm-4.6": {"wire_model": "glm-x", "provider": "zeta", "context_tokens": 131072, "created_via": "manual"},
            "qwen3_coder": {"wire_model": "qc", "provider": "zeta", "context_tokens": 131072, "created_via": "manual"},
            "a" * 60: {"wire_model": "long", "provider": "zeta", "context_tokens": 131072, "created_via": "manual"},
        })
        legacy["providers"].update({
            "z.ai": {"base_url": "https://z.example/anthropic", "auth_kind": "bearer", "secret_env": "ZAI_KEY"},
            "my_api": {"base_url": "https://m.example/anthropic", "auth_kind": "bearer", "secret_env": "MY_KEY"},
            "1min": {"base_url": "https://o.example/anthropic", "auth_kind": "bearer", "secret_env": "ONE_KEY"},
            "nines": {"base_url": "https://n.example/anthropic", "auth_kind": "bearer", "secret_env": "9KEY"},
            "under": {"base_url": "https://u.example/anthropic", "auth_kind": "bearer", "secret_env": "_TOKEN"},
            "short": {"base_url": "https://s.example/anthropic", "auth_kind": "bearer", "secret_env": "AB"},
            "claimed": {"base_url": "https://c.example/anthropic", "auth_kind": "bearer", "secret_env": "ANTHROPIC_X"},
            "hdr": {"base_url": "https://h.example/anthropic", "auth_kind": "header", "secret_env": "HDR_KEY", "header": "authorization"},
            "meta1": {"base_url": "https://169.254.169.254/v1", "auth_kind": "bearer", "secret_env": "META1_KEY"},
            "userinfo": {"base_url": "https://u:p@x.example/v1", "auth_kind": "bearer", "secret_env": "UI_KEY"},
        })
        # Provider predicates refuse the providers migration writes,
        # i.e. those a legacy model references (the migration's skip rule).
        for index, pid in enumerate(("z.ai", "my_api", "1min", "nines", "under", "short", "claimed", "hdr",
                                     "meta1", "userinfo")):
            legacy["models"][f"ref{index}"] = {"wire_model": f"w{index}", "provider": pid,
                                               "context_tokens": 131072, "created_via": "manual"}
        before = sorted(p.name for p in OPERATOR_FIXTURES.rglob("*"))
        plan = operator.migration_preflight(legacy, _docs(), schemas=_schemas())
        self.assertFalse(plan.ok)
        self.assertEqual(plan.documents, {})
        paths_named = {p.json_path for p in plan.problems}
        for expected in ("$.models.glm-4.6", "$.models.qwen3_coder", f"$.models.{'a' * 60}",
                         "$.providers.z.ai", "$.providers.my_api", "$.providers.1min",
                         "$.providers.nines.secret_env", "$.providers.under.secret_env",
                         "$.providers.short.secret_env", "$.providers.claimed.secret_env",
                         "$.providers.hdr.header", "$.providers.meta1.base_url", "$.providers.userinfo.base_url"):
            self.assertIn(expected, paths_named)
        by_code = {p.code for p in plan.problems}
        self.assertTrue({"migrate-model-id", "migrate-provider-id", "migrate-secret-env", "migrate-header",
                         "migrate-base-url"} <= by_code)
        for problem in plan.problems:
            if problem.code == "migrate-provider-id":
                self.assertNotIn("model", problem.remedy)
        self.assertEqual(sorted(p.name for p in OPERATOR_FIXTURES.rglob("*")), before)

    def test_unreferenced_invalid_provider_is_skipped_not_refused(self) -> None:
        legacy = self._legacy()
        legacy["providers"]["short"] = {"base_url": "https://s.example/anthropic", "auth_kind": "bearer",
                                        "secret_env": "AB"}
        plan = operator.migration_preflight(legacy, _docs(), schemas=_schemas())
        self.assertTrue(plan.ok, [p.text() for p in plan.problems])
        self.assertEqual(plan.zero_model_providers, ("idle", "short"))
        self.assertNotIn("short", plan.documents)
        self.assertEqual([p.json_path for p in plan.skipped_problems], ["$.providers.short.secret_env"])

    def test_constants(self) -> None:
        self.assertEqual(operator.LEGACY_MODEL_ID.pattern, "^[a-z0-9][a-z0-9-]*$")
        self.assertEqual(operator.LEGACY_MODEL_ID_MAX, 57)
        self.assertEqual(operator.MIGRATED_KEY.pattern, "^custom-[a-z0-9][a-z0-9-]{0,56}$")
        self.assertEqual(operator.NEW_KEY.pattern, "^custom-[a-z][a-z0-9-]{0,40}$")
        self.assertEqual(operator.MIGRATION_HEADER, "x-api-key")
        self.assertIs(operator.MIGRATION_SECRET_NAME, secret_store.SECRET_NAME)
        self.assertEqual(operator.LINE_ORIGINS, ("catalog", "legacy-custom", "operator", "operator-migrated"))


# ================================================================== render
# Render, captures, overlay emission, failure policy and the prepared proof.
# Every wire, key and credential is fixture-local; no shipped model id.

import io
import re
import time
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from claude_multi import continuity, render, sessions
from claude_multi import custom as custom_mod

ACME_SECRET = "acme-dummy-secret-value"
BETA_SECRET = "beta-dummy-secret-value"
OVERLAY_EXAMPLE_053 = (
    "oauth-extra-models:\n"
    "  claude:\n"
    "    - name: claude-overlay-example-1\n"
    "      display-name: Claude Overlay Example\n"
    "      max-context-length: 1000000\n"
    "      max-completion-tokens: 128000\n"
    "      thinking:\n"
    "        zero-allowed: true\n"
    "        dynamic-allowed: true\n"
    "        levels: [low, medium, high, xhigh, max]\n"
    "  codex:\n"
    "    - name: gpt-overlay-example-1\n"
    "      display-name: GPT Overlay Example\n"
    "      max-context-length: 272000\n"
    "      max-completion-tokens: 128000\n"
    "      thinking:\n"
    "        levels: [low, medium, high, xhigh]\n"
)


def _t1_overlay_docs() -> dict:
    """Fixture docs plus one overlay-marked T1 codex line and one retired
    overlay-served codex entry (fictional wires)."""

    docs = _docs()
    lines = docs["models"]["models"]
    line = copy.deepcopy(lines["sol"])
    line.update(wire_model="gpt-opfix-overlay-1", display="GPT Fixture Overlay", registry_overlay={"channel": "codex"})
    line["efforts"] = {level: {"selector": f"gpt-multi-opfix-t1-{level}", "proxy_contract": f"reasoning-effort-{level}"}
                       for level in ("high", "xhigh")}
    line["context"] = dict(line["context"], provider_tokens=272000)
    lines["opfix-t1"] = line
    docs["retired"]["retired"]["opfix-ret"] = {
        "capabilities": ["lead", "agents"], "context_tokens": 200000, "display": "GPT Fixture Retired",
        "last_wire": "gpt-opfix-retired-1", "provider": "openai", "reason": "fixture", "roles": "all",
        "selectors": {"gpt-multi-opfix-ret-high": "reasoning-effort-high"}, "since_catalog": 31,
        "successor": None, "registry_overlay": {"channel": "codex"},
    }
    return docs


def _build(docs, plan, continuity_map=None, *, static=None, resolve=None):
    return render.build_config_document(
        plan.docs["gateway"], plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"],
        home=Path("/fixture/home"), gateway_tokens=("t" * 64,),
        resolve_secret=resolve or (lambda name: f"dummy-{name.lower()}"),
        continuity=continuity_map or {}, captures=plan.captures, provider_headers=plan.headers,
        oauth_overlay=plan.overlay, overlay_static=static,
    )


class OverlayEmissionTests(unittest.TestCase):
    """One entry per (channel, lower-case wire); precedence; static
    suppression; conflicts; plain literal emission with absent fields omitted."""

    def _capture(self, alias, wire, *, channel="codex", ctx=123456, levels=None, provider="openai", key="custom-old"):
        providers = _docs()["providers"]["providers"]
        overlay = {"channel": channel, "max_context_length": ctx}
        if levels:
            overlay["thinking_levels"] = levels
        return {alias: {"provider": provider, "rd": operator.catalog_route_digest(providers[provider]), "wire": wire,
                        "proxy_contract": "reasoning-effort-high" if channel == "codex" else None, "display": "Old capture",
                        "context_tokens": ctx, "source": f"operator:{key}", "since_catalog": 1, "overlay": overlay}}

    def test_overlay_one_entry_per_wire_across_lines_captures_and_efforts(self) -> None:
        docs = _t1_overlay_docs()
        files = _fixture_files()
        layer = _layer(files, docs=docs)
        # A T2 capture of the T1 wire and of the T2 claude wire (stale capabilities).
        ledger = _ledger(aliases={**self._capture("gpt-opfix-old-high", "gpt-opfix-overlay-1", ctx=99999),
                                  **self._capture("claude-old-alias", "claude-fixture-9", channel="claude",
                                                  ctx=5000, levels=["high"], provider="anthropic")})
        plan = operator.render_plan(docs, layer, ledger)
        document, _a, _u, info = _build(docs, plan)
        overlay = document[render.OVERLAY_KEY]
        self.assertEqual(sorted(overlay), ["claude", "codex"])
        names = {channel: [entry["name"] for entry in entries] for channel, entries in overlay.items()}
        # Multi-effort lines contribute once; T2 codex line and T1 line both present once.
        self.assertEqual(names["codex"], sorted(["gpt-opfix-overlay-1", "gpt-fixture-9"]))
        self.assertEqual(names["claude"], ["claude-fixture-9"])
        t1 = next(e for e in overlay["codex"] if e["name"] == "gpt-opfix-overlay-1")
        self.assertEqual(t1["max-context-length"], 272000)  # the current T1 definition wins its stale capture
        self.assertEqual(t1["thinking"], {"levels": ["high", "xhigh"]})
        claude = overlay["claude"][0]
        self.assertEqual(claude, {"name": "claude-fixture-9", "display-name": "Claude fixture 9",
                                  "max-context-length": 1000000, "max-completion-tokens": 64000,
                                  "thinking": {"levels": ["high", "max"]}})
        conflicts = {(c.channel, c.wire, c.loser) for c in info["overlay_conflicts"]}
        self.assertIn(("codex", "gpt-opfix-overlay-1", "captured alias gpt-opfix-old-high"), conflicts)
        self.assertIn(("claude", "claude-fixture-9", "captured alias claude-old-alias"), conflicts)
        self.assertTrue(all("winning" in c.text() for c in info["overlay_conflicts"]))

    def test_static_registry_wire_gets_no_entry_and_case_normalizes(self) -> None:
        docs = _t1_overlay_docs()
        plan = operator.render_plan(docs, _layer(docs=docs), None)
        static = {"claude": frozenset({"claude-fixture-9"}), "codex": frozenset({"gpt-opfix-overlay-1"})}
        document, *_ = _build(docs, plan, static=static)
        overlay = document[render.OVERLAY_KEY]
        self.assertEqual([e["name"] for e in overlay["codex"]], ["gpt-fixture-9"])
        self.assertNotIn("claude", overlay)
        sources = [render.OverlaySource(tier=0, channel="codex", name="GPT-Mixed", display="A", max_context_length=10,
                                        origin="a"),
                   render.OverlaySource(tier=2, rank=1, channel="codex", name="gpt-mixed", display="B",
                                        max_context_length=20, origin="b", alias="x")]
        planned, conflicts = render.plan_oauth_overlay(sources, rendered_aliases={"codex": frozenset({"x"})})
        self.assertEqual(planned, {"codex": [{"name": "GPT-Mixed", "display-name": "A", "max-context-length": 10}]})
        self.assertEqual(len(conflicts), 1)
        planned, _c = render.plan_oauth_overlay(sources, rendered_aliases={},
                                                static_wires={"codex": frozenset({"gpt-mixed"})})
        self.assertEqual(planned, {})

    def test_precedence_and_equal_tier_rules(self) -> None:
        def src(tier, rank=0, ctx=100, levels=(), origin="o", alias="a", name="w"):
            return render.OverlaySource(tier=tier, rank=rank, channel="claude", name=name, display="D",
                                        max_context_length=ctx, thinking_levels=levels, origin=origin, alias=alias)
        aliases = {"claude": frozenset({"a", "b"})}
        # current T2 beats retained; retained unused alias contributes nothing
        out, _ = render.plan_oauth_overlay([src(1, ctx=5, levels=("high",)), src(2, ctx=9, levels=("high",))],
                                           rendered_aliases=aliases)
        self.assertEqual(out["claude"][0]["max-context-length"], 5)
        out, _ = render.plan_oauth_overlay([src(2, ctx=9, levels=("high",), alias="zz")], rendered_aliases=aliases)
        self.assertEqual(out, {})
        # retired T1 (rank 0) beats a T2 capture (rank 1) in the historical tier
        out, conflicts = render.plan_oauth_overlay(
            [src(2, rank=1, ctx=9, levels=("high",), origin="cap"), src(2, rank=0, ctx=7, origin="ret", alias="b")],
            rendered_aliases=aliases)
        self.assertEqual(out["claude"][0], {"name": "w", "display-name": "D", "max-context-length": 7})
        self.assertEqual([(c.winner, c.loser) for c in conflicts], [("ret", "cap")])
        # equal historical authority: explicit conservative merge, order-free
        merged = [src(2, rank=1, ctx=9, levels=("high", "max"), origin="c1"),
                  src(2, rank=1, ctx=7, levels=("high",), origin="c2", alias="b")]
        out1, c1 = render.plan_oauth_overlay(merged, rendered_aliases=aliases)
        out2, c2 = render.plan_oauth_overlay(list(reversed(merged)), rendered_aliases=aliases)
        self.assertEqual(out1, out2)
        self.assertEqual(out1["claude"][0]["max-context-length"], 7)
        self.assertEqual(out1["claude"][0]["thinking"], {"levels": ["high"]})
        self.assertEqual(len(c1), 2)
        # equal current authority that disagrees refuses the render
        with self.assertRaises(render.RenderError):
            render.plan_oauth_overlay([src(0, ctx=1, origin="x"), src(0, ctx=2, origin="y")], rendered_aliases=aliases)

    def test_loader_prevalidation_refuses_before_publication(self) -> None:
        bad = [
            {"name": "bad name"}, {"name": "x" * 129}, {"name": "m", "max-context-length": True},
            {"name": "m", "max-context-length": 0}, {"name": "m", "max-context-length": 2**63},
            {"name": "m", "thinking": {"levels": []}}, {"name": "m", "thinking": {"levels": ["ultra"]}},
            {"name": "m", "thinking": {"levels": ["high", "high"]}}, {"name": "m", "headers": {}},
            {"name": "m", "display-name": " padded"}, {"name": "m", "display-name": "ctl\x07"},
            {"name": "m", "thinking": {"levels": ["high"], "max": None}},
        ]
        for entry in bad:
            with self.subTest(entry=entry):
                self.assertIsNotNone(render.overlay_entry_problem(entry))
        self.assertIsNone(render.overlay_entry_problem({"name": "A-b.c_1", "max-context-length": 2**63 - 1}))
        with self.assertRaises(render.RenderError):
            render.plan_oauth_overlay([render.OverlaySource(tier=0, channel="codex", name="bad name", display="d",
                                                            max_context_length=1)], rendered_aliases={})
        # an invalid display is omitted (cosmetic), never emitted and never null
        out, _ = render.plan_oauth_overlay([render.OverlaySource(tier=0, channel="codex", name="m", display=" x",
                                                                 max_context_length=1)], rendered_aliases={})
        self.assertEqual(out, {"codex": [{"name": "m", "max-context-length": 1}]})

    def test_emits_one_plain_literal_top_level_key_without_nulls(self) -> None:
        docs = _t1_overlay_docs()
        plan = operator.render_plan(docs, _layer(docs=docs), None)
        document, *_ = _build(docs, plan)
        text = render.emit_yaml(document)
        self.assertEqual(len(re.findall(r"(?m)^oauth-extra-models:$", text)), 1)
        block = text.split("oauth-extra-models:\n", 1)[1].split("\n", 1)[0]
        self.assertTrue(block.startswith("  "))
        for token in ("null", "&", "*", "<<", "!!"):
            section = text[text.index("oauth-extra-models:"):text.index("claude-api-key:")]
            self.assertNotIn(token, section)
        # The gateway's overlay example, encoded by this emitter, is plain literal YAML
        # (double-quoted strings, plain ints/bools, block lists).
        example = {render.OVERLAY_KEY: {
            "claude": [{"name": "claude-overlay-example-1", "display-name": "Claude Overlay Example",
                        "max-context-length": 1000000, "max-completion-tokens": 128000,
                        "thinking": {"zero-allowed": True, "dynamic-allowed": True,
                                     "levels": ["low", "medium", "high", "xhigh", "max"]}}],
            "codex": [{"name": "gpt-overlay-example-1", "display-name": "GPT Overlay Example",
                       "max-context-length": 272000, "max-completion-tokens": 128000,
                       "thinking": {"levels": ["low", "medium", "high", "xhigh"]}}]}}
        self.assertEqual(render.emit_yaml(example), (
            "oauth-extra-models:\n"
            "  claude:\n"
            "    - name: \"claude-overlay-example-1\"\n"
            "      display-name: \"Claude Overlay Example\"\n"
            "      max-context-length: 1000000\n"
            "      max-completion-tokens: 128000\n"
            "      thinking:\n"
            "        zero-allowed: true\n"
            "        dynamic-allowed: true\n"
            "        levels:\n"
            "          - \"low\"\n"
            "          - \"medium\"\n"
            "          - \"high\"\n"
            "          - \"xhigh\"\n"
            "          - \"max\"\n"
            "  codex:\n"
            "    - name: \"gpt-overlay-example-1\"\n"
            "      display-name: \"GPT Overlay Example\"\n"
            "      max-context-length: 272000\n"
            "      max-completion-tokens: 128000\n"
            "      thinking:\n"
            "        levels:\n"
            "          - \"low\"\n"
            "          - \"medium\"\n"
            "          - \"high\"\n"
            "          - \"xhigh\"\n"
        ))
        # Same fields and values as the byte-exact example (flow lists aside).
        for value in ("claude-overlay-example-1", "1000000", "128000", "272000", "zero-allowed: true"):
            self.assertIn(value, OVERLAY_EXAMPLE_053)

    def test_retired_overlay_registration_follows_its_continuity_alias(self) -> None:
        docs = _t1_overlay_docs()
        plan = operator.render_plan(docs, operator.empty_layer(), None)
        alias = "gpt-multi-opfix-ret-high"
        kept = {alias: {"provider": "openai", "wire": "gpt-opfix-retired-1", "proxy_contract": "reasoning-effort-high",
                        "display": "GPT Fixture Retired", "context_tokens": 200000, "source": "seed:opfix-ret",
                        "since_catalog": 31}}
        document, *_ = _build(docs, plan, kept)
        entries = document[render.OVERLAY_KEY]["codex"]
        self.assertIn({"name": "gpt-opfix-retired-1", "display-name": "GPT Fixture Retired",
                       "max-context-length": 200000}, entries)
        self.assertIn(alias, render.rendered_selectors(document))
        pruned, *_ = _build(docs, plan, {})
        self.assertNotIn("gpt-opfix-retired-1", [e["name"] for e in pruned.get(render.OVERLAY_KEY, {}).get("codex", [])])

    def test_pool_line_canonical_selector_is_served_without_a_route(self) -> None:
        plan = operator.render_plan(_docs(), _layer(), None)
        document, *_ = _build(_docs(), plan)
        claude = document["oauth-model-alias"]["claude"]
        self.assertIn({"name": "claude-fixture-9", "alias": "claude-fixture-9", "force-mapping": True, "fork": True},
                      claude)
        self.assertIn("claude-fixture-9", render.rendered_selectors(document))

    def test_render_plan_filters_unapproved_routes_and_passes_headers(self) -> None:
        files = _fixture_files()
        files["beta"]["provider"]["headers"] = {"X-Team": "platform"}
        layer = _layer(files)
        plan = operator.render_plan(_docs(), layer, None)
        self.assertEqual(set(plan.not_rendered), {"acme", "beta"})
        self.assertNotIn("acme", plan.docs["providers"]["providers"])
        self.assertIn("lanbox", plan.docs["providers"]["providers"])
        self.assertIn("custom-kimi-next", plan.docs["models-v2"]["models"])
        route = _route(layer.providers["beta"])
        approved = operator.render_plan(_docs(), _layer(files, ledger=_ledger(routes={"beta": route})),
                                        _ledger(routes={"beta": route}))
        self.assertEqual(approved.headers, {"beta": {"X-Team": "platform"}})
        document, *_ = _build(_docs(), approved)
        section = next(s for s in document["claude-api-key"] if s["base-url"] == "https://beta.example/v1")
        self.assertEqual(section["headers"], {"X-Team": "platform"})
        self.assertEqual(list(section)[:4], ["api-key", "base-url", "auth-header", "headers"])


def _pretty(doc) -> bytes:
    return strict_json.pretty_file_bytes(doc)


class OperatorHomeCase(unittest.TestCase):
    """A temporary HOME with the fixture asset root and a secret file."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="opfix-op-")).resolve()
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        state.ensure_private_dir(self.home)
        secrets = state.ensure_private_dir(self.root / "secrets")
        self.secret_file = secrets / "claude.env"
        state.atomic_write(self.secret_file, (f"ACME_API_KEY={ACME_SECRET}\nBETA_API_KEY={BETA_SECRET}\n"
                                              "KIMI_CLAUDE_API_KEY=kimi-dummy-secret-value\n").encode())
        self.environ = {"HOME": str(self.home), "CLAUDE_MULTI_SECRET_ENV": str(self.secret_file),
                        "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT),
                        "CLAUDE_MULTI_REGISTRY_DIR": str(self.root / "no-registry")}
        self.config = proxy.config_dir(self.home)
        self.pdir = operator.providers_dir(self.environ)
        self.ledger_file = operator.ledger_path(self.environ)
        self.state_root = sessions.state_root(self.environ)

    def install(self, files: dict[str, dict]) -> None:
        state.ensure_private_dir(self.pdir)
        for name in list(os.listdir(self.pdir)):
            os.unlink(self.pdir / name)
        for name, doc in files.items():
            state.atomic_write(self.pdir / f"{name}.json", _pretty(doc))

    def write_ledger(self, **members) -> None:
        document = operator.ledger_document(None)
        document.update(members)
        state.ensure_private_dir(self.ledger_file.parent)
        state.atomic_write(self.ledger_file, strict_json.canonical_file_bytes(document))

    def approve(self, files, *pids) -> dict:
        layer = _layer(files)
        return {pid: _route(layer.providers[pid]) for pid in pids}

    def models_get(self, _base, _token):
        text = (self.config / "config.yaml").read_text()
        return 200, set(re.findall(r'alias: "(claude-multi-render-[0-9a-f]+)"', text))

    def init(self, *flags: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = proxy.cmd_init(list(flags), environ=self.environ, models_get=self.models_get)
        return code, out.getvalue(), err.getvalue()

    def reload(self) -> tuple[int, str]:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err), \
                mock.patch.object(proxy, "await_sentinel", return_value=proxy.ReloadResult("reloaded", "ok")):
            code = proxy.main(["init", "--reload-check"], environ=self.environ)
        return code, err.getvalue()

    def config_text(self) -> str:
        return (self.config / "config.yaml").read_text()

    def aliases(self) -> set[str]:
        return set(re.findall(r'alias: "([^"]+)"', self.config_text()))

    def ledger(self) -> operator.OperatorLedger:
        return operator.load_ledger(self.environ, _schemas())

    def live_record(self, *selectors: str, ended: bool = False) -> str:
        sessions_dir = state.ensure_private_dir(self.state_root / "sessions")
        mid = "0b1e2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
        record = {"version": 4, "applied": {"lead": {"key": "x", "selector": selectors[0]}, "agents": {}},
                  "last_event_source": "end" if ended else "start"}
        state.atomic_write(sessions_dir / f"{mid}.json", strict_json.canonical_file_bytes(record))
        return mid


class EmptyLayerRenderTests(OperatorHomeCase):
    def test_empty_operator_layer_preserves_default_render_bytes(self) -> None:
        """Absent providers.d vs an empty directory, both with no ledger,
        render byte-identical configs and create no ledger."""

        self.assertEqual(self.init()[0], 0)
        baseline = (self.config / "config.yaml").read_bytes()
        state.ensure_private_dir(self.pdir)
        self.assertEqual(self.init()[0], 0)
        self.assertEqual((self.config / "config.yaml").read_bytes(), baseline)
        self.assertFalse(os.path.lexists(self.ledger_file))
        self.assertNotIn("oauth-extra-models", baseline.decode())


class CaptureTransactionTests(OperatorHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.files = _fixture_files()
        self.install(self.files)
        self.write_ledger(routes=self.approve(self.files, "acme", "beta"))

    def test_every_emitted_operator_alias_is_captured_before_config(self) -> None:
        order: list[str] = []
        original = state.atomic_write

        def write(path, data):
            order.append(Path(path).name)
            return original(path, data)

        with mock.patch.object(state, "atomic_write", side_effect=write):
            code, _out, err = self.init()
        self.assertEqual(code, 0, err)
        self.assertLess(order.index(operator.LEDGER_NAME), order.index("config.yaml"))
        ledger = self.ledger()
        layer = _layer(self.files, ledger=ledger)
        served = self.aliases()
        expected = {operator._selector_base(sel) for line in layer.lines.values()
                    for _l, sel, _c in catalog.line_selectors(line.core_entry)}
        self.assertTrue(expected <= served, expected - served)
        self.assertEqual(set(ledger.aliases), expected)
        for alias, capture in ledger.aliases.items():
            key = capture["source"].removeprefix("operator:")
            self.assertIn(key, layer.lines)
            self.assertEqual(capture["provider"], layer.lines[key].provider_id)
        pool = ledger.aliases["claude-fixture-9"]
        self.assertEqual(pool["overlay"], {"channel": "claude", "max_context_length": 1000000,
                                           "max_completion_tokens": 64000, "thinking_levels": ["high", "max"]})
        self.assertIn("oauth-extra-models:", self.config_text())
        # a second render changes nothing (idempotent capture)
        before = self.ledger_file.read_bytes()
        self.assertEqual(self.init()[0], 0)
        self.assertEqual(self.ledger_file.read_bytes(), before)

    def test_render_refuses_when_capture_cannot_commit(self) -> None:
        self.install({})
        self.assertEqual(self.init()[0], 0)
        baseline = (self.config / "config.yaml").read_bytes()
        self.install(self.files)
        original = state.atomic_write
        for error in (OSError("disk full"), state.CommittedStateError("dir fsync")):
            with self.subTest(error=type(error).__name__):
                def write(path, data, error=error):
                    if Path(path).name == operator.LEDGER_NAME:
                        raise error
                    return original(path, data)
                with mock.patch.object(state, "atomic_write", side_effect=write), \
                        self.assertRaisesRegex(proxy.ProxyError, "capture could not be committed"):
                    self.init()
                self.assertEqual((self.config / "config.yaml").read_bytes(), baseline)

    def test_removed_line_capture_stays_served_on_the_same_route(self) -> None:
        self.assertEqual(self.init()[0], 0)
        files = copy.deepcopy(self.files)
        del files["acme"]["lines"]["custom-acme-small"]
        self.install(files)
        self.assertEqual(self.init()[0], 0)
        self.assertIn("custom-acme-small", self.aliases())
        self.assertEqual(self.ledger().aliases["custom-acme-small"]["source"], "operator:custom-acme-small")
        # An origin change never retargets the retained alias.
        files["acme"]["provider"]["base_url"] = "https://other.acme.example/anthropic"
        self.install(files)
        code, _out, err = self.init("--prepare-start")
        self.assertEqual(code, 0)
        self.assertNotIn("custom-acme-small", self.aliases())
        self.assertIn("provider acme not rendered (route changed", err)

    def test_route_edit_disables_render_until_reapproved(self) -> None:
        """An origin edit removes the route (and its captures); a live
        reference refuses the explicit render; a bearer path edit keeps it."""

        self.assertEqual(self.init()[0], 0)
        files = copy.deepcopy(self.files)
        files["acme"]["provider"]["base_url"] = "https://api.acme.example/v2/anthropic"  # path only, bearer
        self.install(files)
        self.assertEqual(self.init()[0], 0)
        self.assertIn("custom-acme-small", self.aliases())
        files["acme"]["provider"]["base_url"] = "https://moved.acme.example/anthropic"
        self.install(files)
        self.live_record("custom-acme-small")
        baseline = (self.config / "config.yaml").read_bytes()
        with self.assertRaisesRegex(proxy.ProxyError, r"alias custom-acme-small \(providers.d/acme.json\) used by 0b1e2c3d"):
            self.init()
        self.assertEqual((self.config / "config.yaml").read_bytes(), baseline)
        self.live_record("custom-acme-small", ended=True)
        code, _out, _err = self.init()
        self.assertEqual(code, 0)
        self.assertNotIn("custom-acme-small", self.aliases())
        self.write_ledger(routes=self.approve(files, "acme", "beta"), aliases=dict(self.ledger().aliases))
        self.assertEqual(self.init()[0], 0)
        self.assertIn("custom-acme-small", self.aliases())

    def test_edited_line_keeps_its_non_colliding_captures(self) -> None:
        """A current definition wins only the aliases it emits: a same-key
        wire change and an effort removal keep the older captures (and
        their overlay registration) served on the unchanged route."""

        self.assertEqual(self.init()[0], 0)
        files = copy.deepcopy(self.files)
        files["anthropic"]["lines"]["custom-claude-fixture"]["wire_model"] = "claude-fixture-10"
        del files["openai"]["lines"]["custom-gpt-fixture"]["efforts"]["xhigh"]
        self.install(files)
        self.live_record("claude-fixture-9")
        for flags in ((), ("--prepare-start",)):
            with self.subTest(flags=flags):
                code, _out, err = self.init(*flags)
                self.assertEqual(code, 0, err)
                served = self.aliases()
                self.assertTrue({"claude-fixture-9", "claude-fixture-10", "custom-gpt-fixture-high",
                                 "custom-gpt-fixture-xhigh"} <= served, served)
                overlay = self.config_text().split("oauth-extra-models:", 1)[1]
                self.assertIn("claude-fixture-9", overlay)
                self.assertIn("claude-fixture-10", overlay)
        ledger = self.ledger()
        self.assertEqual(ledger.aliases["claude-fixture-9"]["wire"], "claude-fixture-9")
        self.assertEqual(ledger.aliases["claude-fixture-10"]["source"], "operator:custom-claude-fixture")
        self.assertIsNotNone(ledger.aliases["claude-fixture-9"]["overlay"])
        self.assertEqual(ledger.aliases["custom-gpt-fixture-xhigh"]["source"], "operator:custom-gpt-fixture")
        layer = _layer(files, ledger=ledger)
        plan = operator.render_plan(_docs(), layer, ledger)
        self.assertEqual(set(plan.captures), {"claude-fixture-9", "custom-gpt-fixture-xhigh"})

    def test_records_naming_captures_are_not_unservable(self) -> None:
        """The continuity extension sees retained captures."""

        self.assertEqual(self.init()[0], 0)
        files = copy.deepcopy(self.files)
        del files["acme"]["lines"]["custom-acme-small"]
        self.install(files)
        self.live_record("custom-acme-small")
        code, out, _err = self.init()
        self.assertEqual(code, 0)
        self.assertNotIn("unservable selectors: custom-acme-small", out)


class FailurePolicyTests(OperatorHomeCase):
    """Operator failure policy."""

    def setUp(self) -> None:
        super().setUp()
        self.files = _fixture_files()
        self.install(self.files)
        self.write_ledger(routes=self.approve(self.files, "acme", "beta"))
        self.assertEqual(self.init()[0], 0)
        self.baseline = (self.config / "config.yaml").read_bytes()
        broken = copy.deepcopy(self.files)
        broken["acme"]["lines"]["custom-acme-small"]["efforts"] = ["ultracode"]
        broken["acme"]["provider"]["kind"] = "openai-compatible"  # gated: the whole file fails
        self.install(broken)

    def test_explicit_init_refuses_invalid_input_and_keeps_the_config(self) -> None:
        ledger_before = self.ledger_file.read_bytes()
        with self.assertRaisesRegex(proxy.ProxyError, "operator layer invalid — config not written"):
            self.init()
        self.assertEqual((self.config / "config.yaml").read_bytes(), self.baseline)
        self.assertEqual(self.ledger_file.read_bytes(), ledger_before)

    def test_prepare_start_degrades_with_a_sanitized_notice(self) -> None:
        code, _out, err = self.init("--prepare-start")
        self.assertEqual(code, 0)
        self.assertIn("operator: providers.d/acme.json not rendered (invalid", err)
        self.assertNotIn(ACME_SECRET, err)
        self.assertNotIn("custom-acme-large-high", self.aliases())
        self.assertIn("custom-beta-one", self.aliases())

    def test_reload_check_degrades_unreferenced_and_refuses_a_live_reference(self) -> None:
        code, err = self.reload()
        self.assertEqual(code, 0, err)
        self.assertNotIn("custom-acme-small", self.aliases())
        # restore, then break again with a live session on the dropped alias
        self.install(self.files)
        self.assertEqual(self.init()[0], 0)
        good = (self.config / "config.yaml").read_bytes()
        broken = copy.deepcopy(self.files)
        broken["acme"]["provider"]["kind"] = "openai-compatible"
        self.install(broken)
        self.live_record("custom-acme-small")
        code, err = self.reload()
        self.assertEqual(code, 1)
        self.assertIn("providers.d/acme.json", err)
        self.assertEqual((self.config / "config.yaml").read_bytes(), good)

    def test_corrupt_ledger_refuses_capture_and_degrades_only_at_start(self) -> None:
        self.install(self.files)
        state.atomic_write(self.ledger_file, b"{not json")
        corrupt = self.ledger_file.read_bytes()
        with self.assertRaisesRegex(proxy.ProxyError, "cannot be captured"):
            self.init()
        code, _err = self.reload()
        self.assertEqual(code, 1)
        self.assertEqual((self.config / "config.yaml").read_bytes(), self.baseline)
        code, _out, err = self.init("--prepare-start")
        self.assertEqual(code, 0)
        self.assertIn("operator lines not rendered", err)
        self.assertNotIn("custom-kimi-next", self.aliases())
        self.assertEqual(self.ledger_file.read_bytes(), corrupt)  # never overwritten


class ReloadReferenceSafetyTests(OperatorHomeCase):
    """Absent or corrupt capture history never proves that a
    dropped operator contribution is unreferenced."""

    def setUp(self) -> None:
        super().setUp()
        self.files = _fixture_files()
        self.install(self.files)
        self.write_ledger(routes=self.approve(self.files, "acme", "beta"))
        self.assertEqual(self.init()[0], 0)
        self.good = (self.config / "config.yaml").read_bytes()
        self.assertIn("custom-acme-small", self.aliases())

    def sessions_dir(self) -> Path:
        return self.state_root / "sessions"

    def test_missing_ledger_still_protects_a_live_reference(self) -> None:
        self.live_record("custom-acme-small")
        os.unlink(self.ledger_file)  # the acme route is now unapproved
        code, err = self.reload()
        self.assertEqual(code, 1)
        self.assertIn("alias custom-acme-small (providers.d/acme.json) used by 0b1e2c3d", err)
        self.assertEqual((self.config / "config.yaml").read_bytes(), self.good)
        self.assertFalse(os.path.lexists(self.ledger_file))
        # an invalid declaration is covered too
        broken = copy.deepcopy(self.files)
        broken["acme"]["provider"]["kind"] = "openai-compatible"
        self.install(broken)
        code, err = self.reload()
        self.assertEqual(code, 1)
        self.assertIn("alias custom-acme-small (providers.d/acme.json)", err)
        self.assertEqual((self.config / "config.yaml").read_bytes(), self.good)
        # unreadable records cannot prove it unreferenced either
        self.live_record("custom-acme-small", ended=True)
        state.atomic_write(self.sessions_dir() / "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f.json", b"{not json")
        code, err = self.reload()
        self.assertEqual(code, 1)
        self.assertIn("cannot prove liveness", err)
        self.assertEqual((self.config / "config.yaml").read_bytes(), self.good)
        # ended and readable: reload degrades
        os.unlink(self.sessions_dir() / "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f.json")
        code, err = self.reload()
        self.assertEqual(code, 0, err)
        self.assertNotIn("custom-acme-small", self.aliases())

    def test_corrupt_ledger_refuses_when_references_are_unknown(self) -> None:
        self.install({"acme": self.files["acme"]})  # unapproved under a corrupt ledger: nothing emitted
        state.atomic_write(self.ledger_file, b"{not json")
        corrupt = self.ledger_file.read_bytes()
        record = state.ensure_private_dir(self.sessions_dir()) / "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f.json"
        state.atomic_write(record, b"{not json")
        code, err = self.reload()
        self.assertEqual(code, 1)
        self.assertIn("cannot prove liveness", err)
        self.assertEqual((self.config / "config.yaml").read_bytes(), self.good)
        os.unlink(record)
        os.rmdir(self.sessions_dir())
        state.atomic_write(self.sessions_dir(), b"not a directory")
        code, err = self.reload()
        self.assertEqual(code, 1)
        self.assertIn("cannot prove liveness", err)
        self.assertEqual((self.config / "config.yaml").read_bytes(), self.good)
        os.unlink(self.sessions_dir())
        code, err = self.reload()
        self.assertEqual(code, 0, err)
        self.assertNotIn("custom-acme-small", self.aliases())
        self.assertEqual(self.ledger_file.read_bytes(), corrupt)

    def test_corrupt_ledger_degrades_when_nothing_would_be_emitted(self) -> None:
        """A declared line whose credential is missing emits no T2 alias:
        with no live reference, reload and init degrade (the corrupt ledger
        is never written)."""

        state.atomic_write(self.secret_file, f"ACME_API_KEY={ACME_SECRET}\nBETA_API_KEY={BETA_SECRET}\n".encode())
        self.install({"kimi": self.files["kimi"]})
        state.atomic_write(self.ledger_file, b"{not json")
        corrupt = self.ledger_file.read_bytes()
        code, err = self.reload()
        self.assertEqual(code, 0, err)
        self.assertNotIn("custom-kimi-next", self.aliases())
        self.assertEqual(self.init()[0], 0)
        self.assertEqual(self.ledger_file.read_bytes(), corrupt)


class MultiOwnerReloadTests(OperatorHomeCase):
    """With a capture and a live reference on the first
    owner, two more declarations of the key refuse every declaration; the
    reload keeps the captured route and never retargets it."""

    def test_third_declaration_never_retargets_a_live_capture(self) -> None:
        lan = _fixture_files()["lanbox"]
        files = {"aaa": copy.deepcopy(lan)}
        self.install(files)
        self.assertEqual(self.init()[0], 0)
        self.assertEqual(self.ledger().aliases["custom-lan-model"]["provider"], "aaa")
        good = (self.config / "config.yaml").read_bytes()
        self.live_record("custom-lan-model")
        for index, pid in enumerate(("bbb", "ccc"), start=1):
            files[pid] = copy.deepcopy(lan)
            files[pid]["provider"]["base_url"] = f"http://box{index}.lan:8010/v1"
        self.install(files)
        code, err = self.reload()
        # Every declaration is refused; the retained capture keeps serving
        # the live alias on its captured route (never the third owner).
        self.assertEqual(code, 0, err)
        for pid in ("aaa", "bbb", "ccc"):
            self.assertIn(f"providers.d/{pid}.json not rendered", err)
        self.assertEqual((self.config / "config.yaml").read_bytes(), good)
        self.assertIn("custom-lan-model", self.aliases())
        self.assertNotIn("box2.lan", self.config_text())
        self.assertEqual(self.ledger().aliases["custom-lan-model"]["provider"], "aaa")


class ConfigCommitBoundaryTests(OperatorHomeCase):
    """A directory-fsync failure after config.yaml was
    replaced is a published config, never "nothing changed"."""

    def test_published_but_unconfirmed_config_raises_the_committed_error(self) -> None:
        self.install({"lanbox": _fixture_files()["lanbox"]})
        original = state.atomic_write

        def write(path, data):
            original(path, data)
            if Path(path).name == "config.yaml":
                raise state.CommittedStateError(5, f"state file {path} was replaced but directory fsync failed")

        with mock.patch.object(state, "atomic_write", side_effect=write):
            with self.assertRaises(proxy.ConfigPublishedError) as caught:
                proxy.render_runtime_config(self.home, environ=self.environ, state_root=self.state_root)
        self.assertIsInstance(caught.exception, state.CommittedStateError)
        target, result, _report = caught.exception.rendered
        self.assertEqual(target.read_text(), result.yaml)
        self.assertIn("custom-lan-model", self.aliases())


class UnknownHistoryReferenceTests(OperatorHomeCase):
    """With capture history absent or corrupt and providers.d
    present, a live reference to an unserved ``custom-`` alias refuses
    reload and explicit renders whatever declared_aliases extracted; pure
    3.0.2 legacy state is unchanged."""

    UNKNOWN = "a providers.d file with problems"

    def setUp(self) -> None:
        super().setUp()
        self.files = _fixture_files()
        self.install(self.files)
        self.write_ledger(routes=self.approve(self.files, "acme", "beta"))
        self.assertEqual(self.init()[0], 0)
        self.good = (self.config / "config.yaml").read_bytes()
        self.assertIn("custom-acme-small", self.aliases())
        self.assertIn("custom-acme-small", self.ledger().aliases)

    def sessions_dir(self) -> Path:
        return self.state_root / "sessions"

    def record(self, mid: str, selector: str) -> None:
        sessions_dir = state.ensure_private_dir(self.sessions_dir())
        record = {"version": 4, "applied": {"lead": {"key": "x", "selector": selector}, "agents": {}},
                  "last_event_source": "start"}
        state.atomic_write(sessions_dir / f"{mid}.json", strict_json.canonical_file_bytes(record))

    def break_acme(self, raw: bytes) -> None:
        state.atomic_write(self.pdir / "acme.json", raw)

    def assert_refused(self, err: str, *needles: str) -> None:
        self.assertIn("config not written", err)
        self.assertIn("restore (or approve) the named providers.d file, or end the session(s)", err)
        for needle in needles:
            self.assertIn(needle, err)
        self.assertNotIn(ACME_SECRET, err)
        self.assertNotIn(BETA_SECRET, err)
        self.assertEqual((self.config / "config.yaml").read_bytes(), self.good)

    def test_missing_ledger_malformed_declaration_with_a_live_reference_refuses(self) -> None:
        self.live_record("custom-acme-small")
        os.unlink(self.ledger_file)
        self.break_acme(b'{"version": 1, "provider": {')
        code, err = self.reload()
        self.assertEqual(code, 1)
        self.assert_refused(err, f"alias custom-acme-small ({self.UNKNOWN}) used by 0b1e2c3d")
        self.assertFalse(os.path.lexists(self.ledger_file))
        # the explicit render refuses too (the invalid layer refuses first)
        with self.assertRaisesRegex(proxy.ProxyError, "config not written"):
            self.init()
        self.assertEqual((self.config / "config.yaml").read_bytes(), self.good)
        self.assertFalse(os.path.lexists(self.ledger_file))
        # corrupt capture history, nothing emitted: the same refusal
        self.install({})
        self.break_acme(b"{not json")
        state.atomic_write(self.ledger_file, b"{not json")
        corrupt = self.ledger_file.read_bytes()
        code, err = self.reload()
        self.assertEqual(code, 1)
        self.assert_refused(err, f"alias custom-acme-small ({self.UNKNOWN}) used by 0b1e2c3d")
        self.assertEqual(self.ledger_file.read_bytes(), corrupt)

    def test_missing_ledger_secret_literal_declaration_with_a_live_reference_refuses(self) -> None:
        self.live_record("custom-acme-small")
        # a second live session names a secret-bearing reserved alias
        self.record("1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f", f"custom-{ACME_SECRET}")
        os.unlink(self.ledger_file)
        leaked = copy.deepcopy(self.files)
        leaked["acme"]["lines"]["custom-acme-small"]["display"] = f"Acme {ACME_SECRET}"
        self.install(leaked)
        code, err = self.reload()
        self.assertEqual(code, 1)
        self.assert_refused(err, f"alias custom-acme-small ({self.UNKNOWN}) used by 0b1e2c3d",
                            f"alias <redacted> ({self.UNKNOWN}) used by 1c2d3e4f")
        self.assertNotIn("providers.d/acme.json)", err)
        self.assertFalse(os.path.lexists(self.ledger_file))
        # explicit: refused before any write, value-free
        with self.assertRaises(proxy.ProxyError) as caught:
            self.init()
        self.assertNotIn(ACME_SECRET, str(caught.exception))
        self.assertEqual((self.config / "config.yaml").read_bytes(), self.good)
        self.assertFalse(os.path.lexists(self.ledger_file))

    def test_missing_ledger_served_reference_publishes(self) -> None:
        """Control: the candidate still serves the referenced alias."""

        self.live_record("custom-kimi-next-max")
        os.unlink(self.ledger_file)
        self.break_acme(b"{not json")
        code, err = self.reload()
        self.assertEqual(code, 0, err)
        self.assertIn("custom-kimi-next-max", self.aliases())
        self.assertNotIn("custom-acme-small", self.aliases())

    def test_missing_ledger_without_live_references_publishes(self) -> None:
        """Safe degradation: nothing live names an unserved operator alias."""

        self.live_record("custom-acme-small", ended=True)
        os.unlink(self.ledger_file)
        self.break_acme(b"{not json")
        code, err = self.reload()
        self.assertEqual(code, 0, err)
        self.assertNotIn("custom-acme-small", self.aliases())
        self.assertIn("custom-kimi-next-max", self.aliases())

    def test_pure_legacy_state_is_unchanged(self) -> None:
        """No providers.d and no ledger (3.0.2 state, legacy custom.json):
        an unserved custom- reference neither refuses nor changes bytes."""

        shutil.rmtree(self.pdir)
        os.unlink(self.ledger_file)
        registry = custom_mod.registry_path(self.environ)
        state.ensure_private_dir(registry.parent)
        state.atomic_write(registry, (OPERATOR_FIXTURES / "custom.json").read_bytes())
        code, err = self.reload()
        self.assertEqual(code, 0, err)
        baseline = (self.config / "config.yaml").read_bytes()
        self.live_record("custom-legacy-gone")
        code, err = self.reload()
        self.assertEqual(code, 0, err)
        self.assertEqual((self.config / "config.yaml").read_bytes(), baseline)
        self.assertNotIn("custom-legacy-gone", self.aliases())
        self.assertFalse(os.path.lexists(self.ledger_file))
        self.assertFalse(os.path.lexists(self.pdir))


class PreparedStartTests(OperatorHomeCase):
    """Prepared execution writes no policy or config; the runtime
    exec stamp stays; operator inputs bind the prepared receipt."""

    def setUp(self) -> None:
        super().setUp()
        self.files = _fixture_files()
        self.install(self.files)
        self.write_ledger(routes=self.approve(self.files, "acme"))
        self.binary = self.root / "bin" / "cli-proxy-api"
        self.binary.parent.mkdir()
        self.binary.write_bytes(b"#!/bin/fake\n")
        self.binary.chmod(0o755)
        self.environ["CLAUDE_MULTI_PROXY_BIN"] = str(self.binary)
        self.assertEqual(self.init("--prepare-start")[0], 0)

    def snapshot(self) -> dict:
        return {str(p): p.read_bytes() for p in self.config.rglob("*") if p.is_file()}

    def test_prepared_gateway_never_writes_operator_state(self) -> None:
        before = self.snapshot()
        original = state.atomic_write

        def write(path, *args, **kwargs):
            self.assertFalse(Path(path).is_relative_to(self.config), path)
            return original(path, *args, **kwargs)

        calls = []
        with mock.patch.object(state, "atomic_write", side_effect=write), \
                mock.patch.object(operator, "write_ledger", side_effect=AssertionError("ledger write")), \
                mock.patch.object(proxy, "render_runtime_config", side_effect=AssertionError("render")), \
                redirect_stderr(io.StringIO()):
            proxy.cmd_run(["--prepared"], environ=self.environ, execve=lambda *a: calls.append(a))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.snapshot(), before)
        stamp = proxy.service.read_exec_stamp(proxy.gateway_workdir(self.state_root))
        self.assertEqual(stamp.pid, os.getpid())

    def test_operator_inputs_bind_the_prepared_receipt(self) -> None:
        for mutate in (lambda: state.atomic_write(self.pdir / "acme.json", self.pdir.joinpath("acme.json").read_bytes() + b"\n"),
                       lambda: state.atomic_write(self.ledger_file, self.ledger_file.read_bytes() + b" "),
                       lambda: state.atomic_write(self.pdir / "zeta.json", b"{}")):
            with self.subTest():
                self.assertEqual(self.init("--prepare-start")[0], 0)
                mutate()
                with self.assertRaisesRegex(proxy.ProxyError, "preparation is absent or stale"):
                    proxy.cmd_run(["--prepared"], environ=self.environ, execve=lambda *_: self.fail("exec"))
                if (self.pdir / "zeta.json").exists():
                    os.unlink(self.pdir / "zeta.json")
                self.install(self.files)
                self.write_ledger(routes=self.approve(self.files, "acme"), aliases=dict(self.ledger().aliases)
                                  if self.ledger_file.exists() and self._ledger_ok() else {})

    def _ledger_ok(self) -> bool:
        try:
            self.ledger()
            return True
        except operator.OperatorError:
            return False

    def test_fingerprint_never_raises(self) -> None:
        os.chmod(self.pdir / "acme.json", 0)
        try:
            found = operator.prepared_fingerprint(self.environ)
        finally:
            os.chmod(self.pdir / "acme.json", 0o600)
        if os.geteuid() != 0:
            self.assertTrue(str(found["providers"]["acme.json"]).startswith("unreadable:"))


class DoctorRenderParityTests(OperatorHomeCase):
    """Doctor's expected render is the proxy's own (no false drift)."""

    def test_gateway_snapshot_sees_no_drift_with_an_operator_layer(self) -> None:
        from claude_multi.cli import gateway_facts, runtime as runtime_mod
        files = _fixture_files()
        self.install(files)
        self.write_ledger(routes=self.approve(files, "acme", "beta"))
        self.assertEqual(self.init()[0], 0)
        runtime = runtime_mod.Runtime(asset_root=FIXTURE_ROOT, environ=dict(self.environ),
                                      served_models_callback=lambda _g, _t: (set(self.aliases()), 200),
                                      health_get=lambda *_a: 200, refresh_shims=False,
                                      listener_owner=lambda _base: proxy.service.OwnerVerdict("ours", "fixture"),
                                      initialize_session_store=False)
        token = proxy.gateway_api_keys(self.home)[-1]
        snap = gateway_facts._gateway_snapshot(runtime, token)
        self.assertIsNone(snap.render_error)
        self.assertIs(snap.config_drift, False)
        self.assertIn("claude-fixture-9", snap.expected)


class OverlayLoaderParityTests(unittest.TestCase):
    """test_overlay_multi_source_loader_parity: the emitted multi-source
    config (current T1 marker line, current operator pool lines on both
    channels, a retired overlay-served continuity alias, plus the gateway's
    overlay example encoded by this emitter) loads on the admitted gateway
    in bwrap --unshare-net, and every overlay-served alias is listed. No
    upstream is contacted (listing only; OAuth credentials are dummies)."""

    def test_overlay_multi_source_loader_parity(self) -> None:
        import _gateway_harness as harness
        if not os.environ.get(harness.BINARY_ENV):
            # Patch 16 ships only in the admitted series.
            harness.boundary("BOUNDARY: overlay parity needs " + harness.BINARY_ENV + " set to the manifest build")

        def factory(bundle, _egress, port, home):
            docs = _t1_overlay_docs()
            plan = operator.render_plan(docs, _layer(docs=docs), None)
            gateway_doc = copy.deepcopy(docs["gateway"])
            gateway_doc["gateway"]["base_url"] = f"http://127.0.0.1:{port}"
            retained = {"gpt-multi-opfix-ret-high": {
                "provider": "openai", "wire": "gpt-opfix-retired-1", "proxy_contract": "reasoning-effort-high",
                "display": "GPT Fixture Retired", "context_tokens": 200000, "source": "seed:opfix-ret",
                "since_catalog": 31}}
            document, _a, _u, info = render.build_config_document(
                gateway_doc, plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"], home=home,
                gateway_token=harness.FIXTURE_GATEWAY_TOKEN, resolve_secret=lambda name: "dummy-opfix-" + name.lower(),
                continuity=retained, captures=plan.captures, provider_headers=plan.headers,
                oauth_overlay=plan.overlay, overlay_static=None,
            )
            overlay = document[render.OVERLAY_KEY]
            # The gateway's overlay example (thinking flags included), plain YAML.
            overlay["claude"].append({"name": "claude-overlay-example-1",
                                      "display-name": "Claude Overlay Example",
                                      "max-context-length": 1000000, "max-completion-tokens": 128000,
                                      "thinking": {"zero-allowed": True, "dynamic-allowed": True,
                                                   "levels": ["low", "medium", "high", "xhigh", "max"]}})
            auth_dir = Path(document["auth-dir"])
            state.ensure_private_dir(auth_dir)
            state.atomic_write(auth_dir / "claude-opfix.json", json.dumps(
                {"type": "claude", "access_token": "dummy-opfix-claude", "email": "opfix@example.invalid"}).encode())
            state.atomic_write(auth_dir / "codex-opfix.json", json.dumps(
                {"type": "codex", "access_token": "dummy-opfix-codex", "email": "opfix@example.invalid",
                 "plan_type": "pro"}).encode())
            return document, {}, info

        gateway = harness.GatewayHarness(document_factory=factory)
        try:
            deadline = time.monotonic() + 20
            expected = {"claude-fixture-9", "gpt-multi-opfix-t1-high", "gpt-multi-opfix-t1-xhigh",
                        "custom-gpt-fixture-high", "gpt-multi-opfix-ret-high"}
            ids: set[str] = set()
            # The Anthropic-format listing shows the claude channel; the
            # OpenAI-format listing (no anthropic-version) the codex channel.
            openai_headers = {"Authorization": "Bearer " + harness.FIXTURE_GATEWAY_TOKEN,
                              "Host": f"127.0.0.1:{gateway.port}", "User-Agent": "opfix-harness/1"}
            while time.monotonic() < deadline:
                ids = set()
                for reply in (gateway.get("/v1/models"),
                              harness.unix_request(gateway.socket, "GET", "/v1/models", headers=openai_headers)):
                    if reply.status == 200:
                        ids |= {row["id"] for row in reply.json()["data"]}
                if expected <= ids:
                    break
                time.sleep(0.1)
            self.assertTrue(expected <= ids, gateway.diagnostic(f"missing {sorted(expected - ids)}"))
            self.assertIn("claude-overlay-example-1", ids)
        finally:
            gateway.close()

    def test_operator_line_routes_to_its_declared_wire_through_the_gateway_harness(self) -> None:
        """Gateway harness integration for a rendered operator line: the fake
        upstream sees the declared wire, the approved credential and the
        validated static header (a test-local base-url surgery only, as the
        harness applies to its own providers). The OAuth-overlay upstream
        leg is the overlay patch's Go gate (TestCM053OverlayPassThrough*): the OAuth
        executors target fixed TLS upstreams no offline fake can answer."""

        import _gateway_harness as harness
        files = {"acme": copy.deepcopy(_fixture_files()["acme"])}
        files["acme"]["provider"]["headers"] = {"X-Fixture-Team": "fixture"}

        def factory(_bundle, egress, port, home):
            docs = _docs()
            layer = _layer(files, docs=docs)
            ledger = _ledger(routes={"acme": _route(layer.providers["acme"])})
            layer = _layer(files, docs=docs, ledger=ledger)
            plan = operator.render_plan(docs, layer, ledger)
            gateway_doc = copy.deepcopy(docs["gateway"])
            gateway_doc["gateway"]["base_url"] = f"http://127.0.0.1:{port}"
            document, _a, _u, info = render.build_config_document(
                gateway_doc, plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"], home=home,
                gateway_token=harness.FIXTURE_GATEWAY_TOKEN, resolve_secret=lambda name: "dummy-opfix-" + name.lower(),
                continuity={}, captures=plan.captures, provider_headers=plan.headers, oauth_overlay=plan.overlay,
            )
            section = next(s for s in document["claude-api-key"] if s["base-url"] == files["acme"]["provider"]["base_url"])
            section["base-url"] = f"http://127.0.0.1:{egress}/opfix-acme"  # test-local surgery (loopback fake)
            aliases = {alias: harness.AliasInfo("opfix-acme", "acme", harness.CLAUDE, "claude", wire, contract)
                       for key, line in plan.layer.lines.items() if line.provider_id == "acme"
                       for wire in (line.core_entry["wire_model"],)
                       for _e, alias, contract in catalog.line_selectors(line.core_entry)}
            return document, {operator._selector_base(a): info for a, info in aliases.items()}, info

        gateway = harness.GatewayHarness(document_factory=factory)
        try:
            for alias in ("custom-acme-small", "custom-acme-large-high"):
                with self.subTest(alias=alias):
                    reply = gateway.request(alias)
                    self.assertEqual(reply.status, 200, gateway.diagnostic("request failed"))
                    self.assertEqual(len(reply.hits), 1)
                    hit = reply.hits[0]
                    self.assertEqual(hit.route, "opfix-acme")
                    self.assertEqual(hit.body["model"], gateway.aliases[alias].wire)
                    self.assertEqual(hit.header_values.get("authorization"), "Bearer dummy-opfix-acme_api_key")
                    self.assertIn("x-fixture-team", hit.header_names)
            large = gateway.request("custom-acme-large-high").hits[0]
            self.assertEqual(large.body.get("output_config", {}).get("effort"), "high")
        finally:
            gateway.close()


class ClosedTransportWordingTests(unittest.TestCase):
    """Transport wording follows shipped availability and explicit closed gates."""

    def test_a_closed_key_transport_offers_the_alternatives(self) -> None:
        from claude_multi.setup import texts

        # The shipped OpenAI key transport is offered; a closed descriptor (a
        # build that does not offer one) shares one generic explanation.
        alternative = operator.transport_alternative("openai", "api-key")
        self.assertTrue(alternative.available)
        self.assertIsNone(alternative.closed_reason)
        names = {"display": "OpenAI", "account": "ChatGPT account", "id": "openai"}
        for template in (texts.KEY_TRANSPORT_CLOSED, texts.KEY_TRANSPORT_CLOSED_LINE):
            text = template.format(**names)
            for needle in ("not available in this build", "ChatGPT account", "OpenRouter"):
                self.assertIn(needle, text)
            self.assertNotRegex(text, r"SPEC|§|\b0\d\d\b")

    def test_the_shipped_keyed_route_is_offered_with_available_wording(self) -> None:
        from claude_multi import views

        shipped = catalog.load_catalog(SHIPPED_ROOT)
        audited = catalog.keyed_compat_audited(shipped.docs["gateway"])
        self.assertTrue(audited)
        field = next(field for field in views.provider_form_fields(keyed_audited=audited) if field[0] == "kind")
        choices = dict(field[3])
        self.assertIn(operator.KEYED_KIND, choices)
        self.assertIn("with an API key", choices[operator.KEYED_KIND])
        self.assertNotIn("not available", choices[operator.KEYED_KIND])
        self.assertNotIn(views.PROVIDER_KIND_CLOSED_LABEL, field[1])

    def test_the_keyed_audit_text_states_the_closed_gate_without_a_milestone(self) -> None:
        docs = copy.deepcopy(catalog.load_catalog(SHIPPED_ROOT).docs)
        docs["gateway"][catalog.AUDITS_KEY] = {catalog.KEYED_COMPAT_AUDIT: False}
        self.assertFalse(catalog.keyed_compat_audited(docs["gateway"]))
        raw = strict_json.pretty_file_bytes({"provider": {"kind": operator.KEYED_KIND}})
        problem = operator.keyed_preflight(docs, "example", raw)
        self.assertIsNotNone(problem)
        self.assertEqual(problem.code, "kind-gated")
        text = problem.subject
        self.assertEqual(text, catalog.KEYED_AUDIT_CLOSED)
        self.assertIn("audit gate is closed", text)
        self.assertIn("unavailable", text)
        self.assertNotRegex(text, r"\b0\d\d\b|lands with|not implemented|never")


if __name__ == "__main__":
    unittest.main()


# ====================================================== operator persistence
# migrate-custom (crash matrix, byte parity, no-clobber, reconciliation),
# the smoke transport caps, doctor findings and two-store alias pruning.
# Every provider, wire and credential stays fixture-local.

import http.server
import socketserver
import threading

from claude_multi import profile as profile_mod

ZETA_SECRET = "zeta-dummy-secret-value"
IDLE_SECRET = "idle-dummy-secret-value"


def _alias_routes(config_text: str) -> dict[str, tuple[str, str]]:
    """alias -> (upstream name, base-url) over the claude-api-key sections."""

    found: dict[str, tuple[str, str]] = {}
    base = name = None
    for raw in config_text.splitlines():
        line = raw.strip()
        if line.startswith("base-url:"):
            base = line.split(":", 1)[1].strip().strip('"')
        elif line.startswith("- name:"):
            name = line.split(":", 1)[1].strip().strip('"')
        elif line.startswith("alias:") and name is not None and base is not None:
            found[line.split(":", 1)[1].strip().strip('"')] = (name, base)
    return found


class MigrationApplyTests(OperatorHomeCase):
    """All-or-nothing migrate-custom with a pending manifest and an
    activation marker; custom.json and records are never touched."""

    KEYS = ("custom-kimi-side", "custom-zeta-pro")

    def setUp(self) -> None:
        super().setUp()
        state.atomic_write(self.secret_file, (f"ZETA_API_KEY={ZETA_SECRET}\nIDLE_API_KEY={IDLE_SECRET}\n"
                                              "KIMI_CLAUDE_API_KEY=kimi-dummy-secret-value\n").encode())
        self.registry = custom_mod.registry_path(self.environ)
        state.ensure_private_dir(self.registry.parent)
        self.source = (OPERATOR_FIXTURES / "custom.json").read_bytes()
        state.atomic_write(self.registry, self.source)
        self.admitted: list[tuple[str, ...]] = []

    def plan(self) -> operator.CustomMigrationPlan:
        docs = _docs()
        legacy = custom_mod.load_registry(self.environ)
        snapshot = proxy.operator_snapshot(self.environ, docs, legacy)
        source = state.read_private(self.registry)
        return operator.plan_custom_migration(docs, legacy, source, snapshot.read, schemas=_schemas(),
                                              ledger=snapshot.ledger, marker_doc=operator.read_marker(self.environ))

    def admit(self, keys, _layer) -> None:
        self.admitted.append(tuple(keys))

    def migrate(self) -> operator.CustomMigrationPlan:
        plan = self.plan()
        operator.apply_custom_migration(self.environ, plan, schemas=_schemas(), admit=self.admit)
        return plan

    def merged_view(self) -> tuple[set[str], set[str]]:
        docs = _docs()
        legacy = custom_mod.load_registry(self.environ)
        snapshot = proxy.operator_snapshot(self.environ, docs, legacy)
        merged = operator.merge_docs(docs, snapshot.layer, legacy=legacy)
        return set(snapshot.layer.lines), set(merged["models-v2"]["models"])

    def final_state(self) -> dict:
        ledger = self.ledger()
        marker = operator.read_marker(self.environ)
        return {
            "files": {p.name: p.read_bytes() for p in sorted(self.pdir.glob("*.json"))
                      if operator.FILE_NAME.fullmatch(p.name)},
            "marker": (marker["custom_sha256"], marker["targets"]),
            "routes": sorted(ledger.routes), "admissions": {k: (v["digest"], v["via"]) for k, v in ledger.admissions.items()},
            "migration": ledger.migration,
        }

    def test_dry_plan_writes_nothing(self) -> None:
        plan = self.plan()
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.writes, ("kimi", "zeta"))
        self.assertEqual(plan.routes, ("zeta",))
        self.assertEqual(plan.admissions, self.KEYS)
        self.assertFalse(self.pdir.exists())
        self.assertFalse(os.path.lexists(self.ledger_file))
        text = "\n".join(operator.migration_preview_lines(plan))
        self.assertIn("skipped legacy provider idle: no custom.json model references it (not migrated, never rendered)",
                      text)
        self.assertIn("admit custom-zeta-pro (via migrate; unverified until claude-multi models qualify custom-zeta-pro)",
                      text)

    def test_apply_is_byte_exact_and_selector_parity_holds(self) -> None:
        self.assertEqual(self.init()[0], 0)
        before = _alias_routes(self.config_text())
        self.assertIn("custom-zeta-pro", before)
        plan = self.migrate()
        self.assertEqual(self.registry.read_bytes(), self.source)  # custom.json never touched
        for pid, raw in plan.targets.items():
            self.assertEqual((self.pdir / f"{pid}.json").read_bytes(), raw)
        marker = operator.read_marker(self.environ)
        self.assertEqual(marker["custom_sha256"], strict_json.sha256_hex(self.source))
        ledger = self.ledger()
        self.assertIsNone(ledger.migration)
        self.assertEqual(sorted(ledger.routes), ["zeta"])
        self.assertFalse((self.pdir / "idle.json").exists())
        self.assertEqual({k: v["via"] for k, v in ledger.admissions.items()}, dict.fromkeys(self.KEYS, "migrate"))
        self.assertEqual(self.admitted, [self.KEYS])
        lines, merged = self.merged_view()
        self.assertEqual(set(self.KEYS), lines)
        self.assertNotIn("zeta-pro", merged)  # the legacy synthetic line is gone after activation
        self.assertEqual(self.init()[0], 0)
        after = _alias_routes(self.config_text())
        for key in self.KEYS:
            self.assertEqual(after[key], before[key], key)
        lcat = profile_mod.LineupCatalog.from_docs(operator.merge_docs(
            _docs(), proxy.operator_snapshot(self.environ, _docs(), custom_mod.load_registry(self.environ)).layer))
        resolution = lcat.resolve_key("zeta-pro")
        self.assertEqual(resolution.key, "custom-zeta-pro")  # records keep their legacy key
        self.assertEqual(self.plan().status, "done")

    def test_crash_at_each_commit_boundary_resumes_without_double_merge(self) -> None:
        reference = None
        boundaries = ("manifest", "first-file", "grants", "admit", "marker", "clear")
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                for path in list(self.pdir.glob("*")) if self.pdir.exists() else ():
                    os.unlink(path)
                if os.path.lexists(self.ledger_file):
                    os.unlink(self.ledger_file)
                self.admitted.clear()
                real_update, real_write, real_atomic = operator.update_ledger, operator.write_provider_bytes, state.atomic_write
                counts = {"update": 0, "write": 0}

                def update(*args, **kwargs):
                    counts["update"] += 1
                    if (boundary == "grants" and counts["update"] == 2) or (boundary == "clear" and counts["update"] == 3):
                        raise OSError("crash")
                    return real_update(*args, **kwargs)

                def write(*args, **kwargs):
                    counts["write"] += 1
                    if boundary == "manifest" or (boundary == "first-file" and counts["write"] == 2):
                        raise OSError("crash")
                    return real_write(*args, **kwargs)

                def atomic(path, *args, **kwargs):
                    if boundary == "marker" and Path(path).name == operator.MIGRATION_MARKER:
                        raise OSError("crash")
                    return real_atomic(path, *args, **kwargs)

                def admit(keys, layer):
                    if boundary == "admit":
                        raise OSError("crash")
                    self.admit(keys, layer)

                plan = self.plan()
                with mock.patch.object(operator, "update_ledger", side_effect=update), \
                        mock.patch.object(operator, "write_provider_bytes", side_effect=write), \
                        mock.patch.object(state, "atomic_write", side_effect=atomic), \
                        self.assertRaises(OSError):
                    operator.apply_custom_migration(self.environ, plan, schemas=_schemas(), admit=admit)
                lines, merged = self.merged_view()
                marked = os.path.lexists(operator.marker_path(self.environ))
                self.assertEqual(marked, boundary == "clear")
                if marked:
                    self.assertEqual(lines, set(self.KEYS))
                    self.assertNotIn("zeta-pro", merged)
                else:
                    self.assertEqual(lines & set(self.KEYS), set())  # pending: never loaded beside legacy
                    self.assertIn("zeta-pro", merged)
                self.assertEqual(self.registry.read_bytes(), self.source)
                self.migrate()
                final = self.final_state()
                self.assertIsNone(final["migration"])
                self.assertEqual(final["routes"], ["zeta"])
                if reference is None:
                    reference = final
                self.assertEqual(final["files"], reference["files"])
                self.assertEqual(final["marker"], reference["marker"])
                self.assertEqual({k: v[0] for k, v in final["admissions"].items()},
                                 {k: v[0] for k, v in reference["admissions"].items()})
                self.assertTrue(set(self.KEYS) <= set().union(*self.admitted) or boundary == "clear")

    def test_existing_target_is_never_clobbered(self) -> None:
        state.ensure_private_dir(self.pdir)
        other = copy.deepcopy(_fixture_files()["kimi"])
        state.atomic_write(self.pdir / "kimi.json", _pretty(other))
        plan = self.plan()
        self.assertEqual(plan.status, "refused")
        text = "\n".join(p.text() for p in plan.problems)
        self.assertIn("providers.d/kimi.json: already exists and differs from what migrate-custom writes", text)
        self.assertIn('"custom-kimi-side"', text)  # the exact fragment to merge by hand
        with self.assertRaises(operator.OperatorError):
            operator.apply_custom_migration(self.environ, plan, schemas=_schemas(), admit=self.admit)
        self.assertEqual(sorted(p.name for p in self.pdir.iterdir()), ["kimi.json"])
        self.assertFalse(os.path.lexists(self.ledger_file))

    def test_changed_source_reconciles_owned_files_without_admission(self) -> None:
        self.migrate()
        admitted_digest = self.ledger().admissions["custom-zeta-pro"]["digest"]
        changed = json.loads(self.source)
        changed["models"]["zeta-pro"]["context_tokens"] = 400000
        state.atomic_write(self.registry, strict_json.pretty_file_bytes(changed))
        plan = self.plan()
        self.assertEqual(plan.status, "changed")
        self.assertEqual((plan.routes, plan.admissions), ((), ()))
        self.assertEqual(plan.writes, ("zeta",))
        self.assertIn("custom.json changed since migration", "\n".join(operator.migration_preview_lines(plan)))
        operator.apply_custom_migration(self.environ, plan, schemas=_schemas(), admit=self.admit)
        snapshot = proxy.operator_snapshot(self.environ, _docs(), custom_mod.load_registry(self.environ))
        line = snapshot.layer.lines["custom-zeta-pro"]
        self.assertNotEqual(line.definition_digest, admitted_digest)
        self.assertFalse(operator.operator_line_offered("custom-zeta-pro", layer=snapshot.layer, ledger=snapshot.ledger,
                                                        admitted_lines=self.KEYS, provider_enabled=True))
        self.assertEqual(operator.read_marker(self.environ)["custom_sha256"],
                         strict_json.sha256_hex(self.registry.read_bytes()))
        # A hand-edited target is never overwritten by a later reconciliation.
        changed["models"]["zeta-pro"]["context_tokens"] = 500000
        state.atomic_write(self.registry, strict_json.pretty_file_bytes(changed))
        edited = json.loads((self.pdir / "zeta.json").read_text())
        edited["notes"] = "hand edit"
        state.atomic_write(self.pdir / "zeta.json", _pretty(edited))
        self.assertEqual(self.plan().status, "refused")

    def test_displaced_legacy_line_is_previewed_not_refused(self) -> None:
        legacy = json.loads(self.source)
        legacy["models"]["kimi-dup"] = {"wire_model": "k3", "provider": "kimi", "context_tokens": 262144,
                                        "created_via": "manual"}
        state.atomic_write(self.registry, strict_json.pretty_file_bytes(legacy))
        plan = self.plan()
        self.assertEqual(plan.status, "ready", [p.text() for p in plan.problems])
        self.assertEqual([row.key for row in plan.displaced], ["custom-kimi-dup"])
        self.assertNotIn("custom-kimi-dup", plan.admissions)
        text = "\n".join(operator.migration_preview_lines(plan, key_refs={"custom-kimi-dup": ["profile p lead"]}))
        self.assertIn("displaced custom-kimi-dup: the catalog serves kimi/k3 as kimi-k3; it yields "
                      "(no automatic successor); used by profile p lead", text)


class _SmokeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        server = self.server
        server.requests.append((self.path, self.headers.get("Authorization"),
                                json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        mode = server.mode
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/v1/messages")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "error":
            body = b'{"type":"error"}'
            self.send_response(500)
        elif mode == "huge":
            body = b'{"type":"message","content":[{"text":"' + b"x" * (300 * 1024) + b'"}]}'
            self.send_response(200)
        elif mode == "slow":
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
            return
        else:
            body = b'{"type":"message","role":"assistant","content":[{"type":"text","text":"ok"}]}'
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SmokeTransportTests(unittest.TestCase):
    """One request, no redirect, no retry, a total deadline and a
    cumulative byte cap against a port-0 loopback fake (never 8317/8316)."""

    def serve(self, mode: str):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SmokeHandler)
        server.daemon_threads = True
        server.mode = mode
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def smoke(self, base: str, **kwargs):
        from claude_multi.cli import runtime as runtime_mod
        from claude_multi import service

        return runtime_mod.default_qualify_transport(
            base, "fixture-token", "custom-fixture-alias",
            owner_check=lambda _base: service.OwnerVerdict("ours", "fixture"), **kwargs)

    def test_verdicts_and_single_request(self) -> None:
        for mode, result, http in (("ok", "pass", 200), ("redirect", "fail", 302), ("error", "fail", 500),
                                   ("huge", "degenerate", 200)):
            with self.subTest(mode=mode):
                server, base = self.serve(mode)
                outcome = self.smoke(base)
                self.assertEqual((outcome.result, outcome.http), (result, http))
                self.assertEqual(len(server.requests), 1)  # no redirect follow, no retry
                path, auth, body = server.requests[0]
                self.assertEqual((path, auth, body["model"]), ("/v1/messages", "Bearer fixture-token",
                                                               "custom-fixture-alias"))

    def test_total_deadline_bounds_a_slow_drip(self) -> None:
        server, base = self.serve("slow")
        started = time.monotonic()
        outcome = self.smoke(base, deadline=0.6)
        self.assertEqual(outcome.result, "degenerate")
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(len(server.requests), 1)

    def test_non_loopback_endpoint_is_refused_before_any_request(self) -> None:
        from claude_multi import errors

        with self.assertRaises(errors.ClaudeMultiError):
            self.smoke("https://gateway.example:443")

    def test_classify_smoke_structure(self) -> None:
        self.assertEqual(operator.classify_smoke(200, b"{}").result, "fail")
        self.assertEqual(operator.classify_smoke(200, b"not json").result, "fail")
        self.assertEqual(operator.classify_smoke(None, b"").result, "fail")
        self.assertEqual(operator.classify_smoke(200, b"", timed_out=True).result, "degenerate")


class DoctorFindingsTests(unittest.TestCase):
    """B01-B05 / A01-A08 / I01-I02 severities and the live-reference
    matrix (a reference upgrades Attention to BLOCK, never the reverse)."""

    LIVE = "55555555-5555-4555-8555-555555555555"

    def snapshot(self, files, *, ledger=None, read_problems=(), marker=False):
        raw = _raw(files)
        read = operator.ProvidersRead(Path("/nonexistent/providers.d"), True, False, raw, marker, tuple(read_problems))
        layer = operator.validate_layer(_docs(), raw, schemas=_schemas(), marker=marker, ledger=ledger,
                                        read_problems=read_problems)
        return operator.OperatorSnapshot(layer, ledger, None, ledger is not None, read, _schemas())

    def findings(self, snapshot, *, refs=None, live=(), admitted=(), key_refs=None, served=None, **kwargs):
        plan = operator.render_plan(_docs(), snapshot.layer, snapshot.ledger)
        return operator.doctor_findings(_docs(), snapshot, plan=plan, admitted=admitted, refs=refs or {},
                                        live=frozenset(live), key_refs=key_refs, served=served, **kwargs)

    def acme(self):
        files = {"acme": copy.deepcopy(_fixture_files()["acme"])}
        layer = _layer(files)
        return files, layer

    def test_invalid_file_matrix(self) -> None:
        files, _layer_ok = self.acme()
        files["acme"]["lines"]["custom-acme-small"]["efforts"] = ["ultracode"]
        found = self.findings(self.snapshot(files, ledger=_ledger(routes={})))
        self.assertTrue(any(line.startswith("providers.d/acme.json ignored: $.lines.custom-acme-small")
                            for line in found.attention), found)
        self.assertEqual(found.blocks, ())
        live = self.findings(self.snapshot(files, ledger=_ledger(routes={})),
                             refs={"custom-acme-small": frozenset({self.LIVE})}, live={self.LIVE})
        self.assertTrue(any("sessions/profiles use its lines (session 55555555)" in line
                            and "fix: claude-multi providers edit acme" in line for line in live.blocks), live)
        bound = self.findings(self.snapshot(files, ledger=_ledger(routes={})),
                              key_refs={"custom-acme-small": ["profile p lead"]})
        self.assertTrue(any("(profile p lead)" in line for line in bound.blocks), bound)

    def test_secret_and_unsafe_files_block(self) -> None:
        files, _ = self.acme()
        files["acme"]["notes"] = "sk-ant-api03-" + "A" * 40
        unsafe = operator.OperatorProblem("unsafe-file", "providers.d/beta.json", "", None, None, "not loaded (EPERM)")
        found = self.findings(self.snapshot(files, read_problems=(unsafe,)))
        self.assertTrue(any("a secret value is not allowed" in line for line in found.blocks), found)
        self.assertIn("providers.d/beta.json: not loaded (EPERM)", found.blocks)

    def test_route_and_capture_matrix(self) -> None:
        files, layer = self.acme()
        capture = {"provider": "acme", "rd": "0" * 64, "wire": "acme-old-1", "proxy_contract": None,
                   "display": "Old", "context_tokens": 65536, "source": "operator:custom-acme-old",
                   "since_catalog": 31, "overlay": None}
        ledger = _ledger(routes={}, aliases={"custom-acme-old": capture})
        quiet = self.findings(self.snapshot(files, ledger=ledger))
        self.assertIn("provider acme: route unapproved; not rendered — claude-multi providers approve acme",
                      quiet.attention)
        self.assertIn("alias custom-acme-old (captured for custom-acme-old; provider acme not rendered) has no line — "
                      "prune when unused: claude-multi doctor --prune-aliases", quiet.attention)
        busy = self.findings(self.snapshot(files, ledger=ledger), refs={"custom-acme-small": frozenset({self.LIVE})},
                             live={self.LIVE})
        self.assertIn("provider acme: route unapproved; not rendered — claude-multi providers approve acme "
                      "(used by 55555555)", busy.blocks)
        approved = _ledger(routes={"acme": _route(layer.providers["acme"])}, aliases={"custom-acme-old": capture})
        b05 = self.findings(self.snapshot(files, ledger=approved), refs={"custom-acme-old": frozenset({self.LIVE})},
                            live={self.LIVE})
        self.assertIn("alias custom-acme-old (provider acme not rendered) is used by 55555555 — restore "
                      "providers.d/acme.json or end the session; a record without a recorded end counts as live, "
                      "including a launch that exited before SessionStart; if that session is not running, record "
                      f"its end: claude-multi sessions mark-ended {self.LIVE}", b05.blocks)

    def test_admission_lapse_unserved_and_counts(self) -> None:
        files, layer = self.acme()
        routes = {"acme": _route(layer.providers["acme"])}
        line = layer.lines["custom-acme-small"]
        grant = {"digest": line.definition_digest, "at": "2026-09-30T00:00:00Z", "via": "admit",
                 "diagnostic": {"wire": "acme-small-1", "fields": layer.metadata["custom-acme-small"]["field_hashes"]}}
        ok = self.findings(self.snapshot(files, ledger=_ledger(routes=routes, admissions={"custom-acme-small": grant})),
                           admitted={"custom-acme-small"}, served=frozenset())
        self.assertIn("custom-acme-small: 0/1 aliases served — claude-multi providers apply", ok.attention)
        self.assertIn("operator: 1 providers · 2 lines (1 admitted, 1 New·Off, 0 agent-eligible)", ok.info)
        self.assertFalse(any("candidates" in line for line in ok.info))
        files["acme"]["lines"]["custom-acme-small"]["wire_model"] = "acme-small-2"
        lapsed = self.findings(self.snapshot(files, ledger=_ledger(routes=routes, admissions={"custom-acme-small": grant})),
                               admitted={"custom-acme-small"}, refs={"custom-acme-small": frozenset({self.LIVE})},
                               live={self.LIVE})
        self.assertTrue(any(text.startswith("custom-acme-small changed since admission (wire acme-small-1→acme-small-2")
                            and "re-admit: claude-multi models admit custom-acme-small" in text
                            and "retarget at the next reload: 55555555" in text for text in lapsed.attention), lapsed)

    def test_legacy_displacement_and_overlay_lines(self) -> None:
        legacy = json.loads((OPERATOR_FIXTURES / "custom.json").read_text())
        legacy["providers"]["short"] = {"base_url": "https://s.example/anthropic", "auth_kind": "bearer",
                                        "secret_env": "AB"}
        legacy["models"]["bad_id"] = {"wire_model": "b", "provider": "zeta", "context_tokens": 65536,
                                      "created_via": "manual"}
        found = self.findings(self.snapshot({}), legacy=legacy, overlay_conflicts=("codex/x: kept a over b",))
        self.assertIn("custom.json: 3 providers, 3 models not migrated — preview: claude-multi providers migrate-custom",
                      found.attention)
        self.assertTrue(any(text.startswith("custom.json: migrate-custom would refuse — $.models.bad_id: model id")
                            and "re-add it under a valid id" in text for text in found.attention), found)
        self.assertTrue(any(text.startswith("custom.json: unused legacy provider (skipped by migrate-custom) — "
                                            "$.providers.short.secret_env") for text in found.attention), found)
        self.assertIn("oauth-extra-models: codex/x: kept a over b", found.attention)
        files = {"kimi": {"version": 1, "lines": {"custom-kimi-dup": {
            "wire_model": "k3", "display": "dup", "efforts": ["high"], "default_effort": "high",
            "context": {"declared_tokens": 65536, "source": "operator"}}}}}
        displaced = self.findings(self.snapshot(files), key_refs={"custom-kimi-dup": ["binding b"]})
        self.assertTrue(any(text.startswith("operator line custom-kimi-dup (kimi/k3) is displaced by catalog kimi-k3")
                            and "choose a model for binding b" in text for text in displaced.attention), displaced)

    def test_migrated_million_line_is_info(self) -> None:
        files = {"zeta": {"version": 1, "provider": {
            "display": "Zeta", "kind": "anthropic-compatible", "base_url": "https://api.zeta.example/anthropic",
            "auth": {"kind": "bearer", "secret_ref": "env:ZETA_API_KEY"}, "independence_family": "unknown"},
            "lines": {"custom-zeta-big": {"wire_model": "zeta-big", "display": "Zeta big", "efforts": ["high"],
                                          "default_effort": "high",
                                          "context": {"declared_tokens": 1000000, "source": "operator"},
                                          "selector": "custom-zeta-big", "legacy_key": "zeta-big",
                                          "migrated_from": "custom.json"}}}}
        found = self.findings(self.snapshot(files, marker=True))
        self.assertIn("custom-zeta-big: migrated selector has no [1m]; the client books its default window — "
                      "re-declare to change it", found.info)


class CapturePruneTests(OperatorHomeCase):
    """Doctor --prune-aliases covers the ledger captures with one
    combined live-reference check; tombstones are monotonic."""

    def setUp(self) -> None:
        super().setUp()
        self.files = {"acme": copy.deepcopy(_fixture_files()["acme"])}
        self.install(self.files)
        self.write_ledger(routes=self.approve(self.files, "acme"))
        self.assertEqual(self.init()[0], 0)
        self.assertIn("custom-acme-small", self.ledger().aliases)
        del self.files["acme"]["lines"]["custom-acme-small"]
        self.install(self.files)
        self.assertEqual(self.init()[0], 0)
        self.assertIn("custom-acme-small", self.aliases())  # retained capture still served

    def prune(self, *names):
        with redirect_stderr(io.StringIO()):
            return proxy.prune_aliases(self.home, self.state_root, list(names) or None, environ=self.environ,
                                       models_get=self.models_get)

    def test_unreferenced_capture_is_pruned_and_tombstoned(self) -> None:
        outcome = self.prune()
        self.assertEqual(outcome.code, 0, outcome.lines)
        self.assertTrue(any(line.startswith("pruned: custom-acme-small (acme acme-small-1; captured for custom-acme-small)")
                            for line in outcome.lines), outcome.lines)
        self.assertTrue(any(line.startswith("kept: custom-acme-large-high — current operator line custom-acme-large")
                            for line in outcome.lines), outcome.lines)
        ledger = self.ledger()
        self.assertNotIn("custom-acme-small", ledger.aliases)
        self.assertIn("custom-acme-small", ledger.pruned)
        self.assertNotIn("custom-acme-small", self.aliases())
        self.assertEqual(self.init()[0], 0)
        self.assertNotIn("custom-acme-small", self.aliases())  # no automatic resurrection

    def test_live_reference_keeps_and_refuses_named(self) -> None:
        mid = self.live_record("custom-acme-small")
        outcome = self.prune()
        self.assertEqual(outcome.code, 0)
        self.assertTrue(any(line.startswith("kept: custom-acme-small — live session") for line in outcome.lines))
        refused = self.prune("custom-acme-small")
        self.assertEqual(refused.code, 1)
        self.assertIn("referenced by live session records", refused.lines[0])
        # The refusal names the remedy for a start that never ended.
        self.assertTrue(refused.lines[0].endswith(
            f"; nothing pruned; a record without a recorded end counts as live, including a launch that exited "
            f"before SessionStart; if that session is not running, record its end: "
            f"claude-multi sessions mark-ended {mid}"), refused.lines[0])
        self.assertIn("custom-acme-small", self.ledger().aliases)

    def test_unreadable_record_refuses(self) -> None:
        sessions_dir = state.ensure_private_dir(self.state_root / "sessions")
        state.atomic_write(sessions_dir / "66666666-6666-4666-8666-666666666666.json", b"{broken")
        before = self.ledger_file.read_bytes()
        outcome = self.prune()
        self.assertEqual(outcome.code, 1)
        self.assertIn("cannot prove liveness", outcome.lines[0])
        self.assertEqual(self.ledger_file.read_bytes(), before)

    def test_unknown_named_alias_refuses(self) -> None:
        outcome = self.prune("custom-nope")
        self.assertEqual(outcome.code, 1)
        self.assertIn("unknown continuity or captured alias(es): custom-nope", outcome.lines[0])


# ------------------------------------------------------------------ provider retirement
def _sample_raw() -> bytes:
    return (SHIPPED_ROOT / catalog.EXAMPLES_PROVIDERS_DIR / "llm-local.json").read_bytes()


class ExternalizedRetirementTests(unittest.TestCase):
    """The explicit externalized-provider retirement."""

    def _raw_with(self, mutate) -> dict:
        raw = catalog.load_raw(FIXTURE_ROOT)
        mutate(raw["docs"])
        return raw

    @staticmethod
    def _externalize(docs: dict, **fields) -> None:
        # Fixture analogue of llm-local: move a keyless LAN provider out of the
        # catalog, retire its line with the explicit representation.
        lines = docs["models"]["models"]
        key = next(k for k, v in sorted(lines.items())
                   if docs["providers"]["providers"][v["provider"]]["transport"]["kind"] == "direct-openai")
        line = lines.pop(key)
        provider = line["provider"]
        docs["providers"]["providers"].pop(provider)
        for rkey in [k for k, v in docs["retired"]["retired"].items() if v["provider"] == provider]:
            docs["retired"]["retired"][rkey].update(successor=None, externalized={"sample": f"{provider}.json"})
        entry = {"successor": None, "reason": "fixture externalization", "since_catalog": 1,
                 "provider": provider, "last_wire": line["wire_model"], "display": line["display"],
                 "context_tokens": line["context"]["client_tokens"], "capabilities": line["capabilities"],
                 "roles": line["roles"], "selectors": {line["selector"]: None},
                 "externalized": {"sample": f"{provider}.json"}}
        entry.update(fields)
        docs["retired"]["retired"][key] = entry
        docs["_externalized"] = (key, provider)

    def test_externalized_provider_retirement_is_valid(self) -> None:
        raw = self._raw_with(self._externalize)
        key, provider = raw["docs"].pop("_externalized")
        self.assertEqual(catalog.validate_catalog(raw), [])
        docs = raw["docs"]
        self.assertEqual(catalog.externalized_providers(docs), {provider: f"{provider}.json"})
        resolved = catalog.resolve_key_in(docs["models"]["models"], docs["retired"]["retired"], key)
        self.assertIsNone(resolved.key)
        self.assertIn("needs a model choice", resolved.notice)

    def test_externalized_marker_rules(self) -> None:
        cases = {
            "still a catalog provider": lambda d: (self._externalize(d), d["providers"]["providers"].update(
                {d["_externalized"][1]: copy.deepcopy(catalog.load_raw(FIXTURE_ROOT)["docs"]["providers"]
                                                      ["providers"][d["_externalized"][1]])})),
            "successor must be null": lambda d: self._externalize(d, successor="sol"),
            "no proxy contract": lambda d: self._externalize(d, selectors={"claude-multi-opfix-ext": "x-high"}),
            "no OAuth overlay": lambda d: self._externalize(d, registry_overlay={"channel": "claude"}),
        }
        for needle, mutate in cases.items():
            with self.subTest(case=needle):
                raw = self._raw_with(mutate)
                raw["docs"].pop("_externalized", None)
                errors = catalog.validate_catalog(raw)
                self.assertTrue(any(needle in error for error in errors), errors)
        # Without the marker an unknown provider stays an error.
        raw = self._raw_with(self._externalize)
        key, _provider = raw["docs"].pop("_externalized")
        raw["docs"]["retired"]["retired"][key].pop("externalized")
        self.assertTrue(any("unknown provider" in error for error in catalog.validate_catalog(raw)))

    def test_shipped_llm_local_is_externalized_with_its_sample(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        externalized = catalog.externalized_providers(bundle.docs)
        self.assertIn("llm-local", externalized)
        for provider_id, sample in externalized.items():
            with self.subTest(provider=provider_id):
                self.assertNotIn(provider_id, bundle.providers)
                raw = (SHIPPED_ROOT / catalog.EXAMPLES_PROVIDERS_DIR / sample).read_bytes()
                layer = operator.resolve_provider_document(bundle.docs, provider_id, raw,
                                                           schemas=operator.load_schemas(SHIPPED_ROOT))
                self.assertEqual(layer.problems, ())
                self.assertIn(provider_id, layer.providers)
                # Keyless: no route approval step; lead-only New · Off lines.
                self.assertEqual(layer.route_status[provider_id], "keyless")
                for line in layer.lines.values():
                    self.assertEqual(line.core_entry["status"], "new")
                    self.assertNotIn("agents", line.core_entry["capabilities"])
                # No automatic successor for any retirement of that provider.
                for entry in bundle.retired.values():
                    if entry["provider"] == provider_id:
                        self.assertIsNone(entry["successor"])

    def test_render_is_quiet_until_the_sample_is_declared(self) -> None:
        from claude_multi import continuity
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        seed = continuity.seed_only(bundle)["aliases"]
        external = {alias for alias, entry in seed.items() if entry["provider"] == "llm-local"}
        self.assertTrue(external)
        plan = operator.render_plan(bundle.docs, operator.empty_layer(), None)
        self.assertEqual(plan.quiet_providers, frozenset({"llm-local"}))
        result = render.render_config(
            plan.docs["gateway"], plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"],
            home=Path("/fixture/home"), gateway_tokens=("t" * 64,), resolve_secret=lambda name: "dummy",
            continuity=seed, quiet_providers=plan.quiet_providers)
        self.assertEqual(result.notices, ())
        self.assertFalse(external & result.served)
        layer = operator.resolve_provider_document(bundle.docs, "llm-local", _sample_raw(),
                                                   schemas=operator.load_schemas(SHIPPED_ROOT))
        plan = operator.render_plan(bundle.docs, layer, None)
        self.assertEqual(plan.quiet_providers, frozenset())
        result = render.render_config(
            plan.docs["gateway"], plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"],
            home=Path("/fixture/home"), gateway_tokens=("t" * 64,), resolve_secret=lambda name: "dummy",
            continuity=seed, quiet_providers=plan.quiet_providers)
        # The historical selectors serve again on the declared (keyless) provider.
        self.assertLessEqual(external, result.served)
        for line in layer.lines.values():
            self.assertLessEqual(set(operator.line_aliases(line.core_entry)), result.served)


class PresetTests(unittest.TestCase):
    """Reviewed presets and samples are templates, never grants."""

    def test_presets_and_samples_validate_and_never_shadow_catalog_providers(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        schemas = operator.load_schemas(SHIPPED_ROOT)
        # The picker offers presets only; an example stays loadable by name.
        self.assertNotIn("llm-local", operator.preset_paths(SHIPPED_ROOT))
        self.assertTrue(all(path.parent.name == operator.PRESETS_DIRNAME
                            for path in operator.preset_paths(SHIPPED_ROOT).values()))
        presets = operator.sample_paths(SHIPPED_ROOT)
        self.assertIn("llm-local", presets)
        self.assertTrue(any(path.parent.name == operator.PRESETS_DIRNAME for path in presets.values()))
        for name, path in sorted(presets.items()):
            with self.subTest(preset=name):
                raw = operator.load_preset(name, SHIPPED_ROOT)
                self.assertEqual(raw, path.read_bytes())
                # The declaration a preset becomes (its review block dropped);
                # a keyed OpenAI-compatible preset needs that route open.
                declared = operator.document_bytes(operator.preset_document(raw))
                with mock.patch.object(catalog, "keyed_compat_audited", return_value=True):
                    layer = operator.resolve_provider_document(bundle.docs, name, declared, schemas=schemas)
                self.assertEqual(layer.problems, ())
                document = strict_json.loads(declared)
                if "provider" in document:
                    self.assertNotIn(name, bundle.providers)
                    # Off by default: keyless LAN or unapproved; nothing grants.
                    if document["provider"]["auth"] != {"kind": "none"}:
                        self.assertEqual(layer.route_status[name], "unapproved")
                for line in layer.lines.values():
                    self.assertEqual(line.core_entry["status"], "new")
        with self.assertRaises(operator.OperatorError) as caught:
            operator.load_preset("no-such-preset", SHIPPED_ROOT)
        self.assertIn("reviewed presets:", str(caught.exception))

    def test_preset_document_retargets_the_base_url_and_listing(self) -> None:
        document = operator.preset_document(_sample_raw(), base_url="http://box.lan:9000/v1")
        self.assertEqual(document["provider"]["base_url"], "http://box.lan:9000/v1")
        self.assertEqual(document["provider"]["listing"]["url"], "http://box.lan:9000/v1/models")
        self.assertEqual(operator.preset_document(_sample_raw()), strict_json.loads(_sample_raw()))
        with self.assertRaises(operator.OperatorError):
            operator.preset_document(b'{"version": 1, "lines": {}}', base_url="http://x.lan/v1")

    def test_secret_file_import_rules(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="opfix-secretfile-"))
        self.addCleanup(shutil.rmtree, root, True)
        good = root / "key"
        state.atomic_write(good, b"k-value-123\n")
        self.assertEqual(operator.read_secret_file(good), "k-value-123")
        for name, data, mode in (("shared", b"k\n", 0o644), ("empty", b"\n", 0o600), ("two", b"a\nb\n", 0o600)):
            path = root / name
            path.write_bytes(data)
            os.chmod(path, mode)
            with self.subTest(case=name), self.assertRaises(operator.OperatorError) as caught:
                operator.read_secret_file(path)
            self.assertNotIn("k-value", str(caught.exception))
        link = root / "link"
        link.symlink_to(good)
        with self.assertRaises(operator.OperatorError):
            operator.read_secret_file(link)


class TransportAlternativeTests(unittest.TestCase):
    """Reviewed transports keep every selector."""

    def _docs(self) -> dict:
        return _docs()

    def _ledger(self, choice: str | None, *, approved: bool = True) -> operator.OperatorLedger:
        if choice is None:
            return _ledger()
        if choice == "oauth-pool":
            return _ledger(transport_choices={"anthropic": choice})
        alternative = operator.transport_alternative("anthropic", choice)
        route = operator.transport_route_record(alternative, at="2026-09-30T00:00:00Z")
        if not approved:
            route["rd"] = "0" * 64
        return _ledger(transport_choices={"anthropic": choice}, routes={"anthropic": route})

    def _render(self, ledger, resolve=None):
        docs = self._docs()
        plan = operator.render_plan(docs, operator.empty_layer(), ledger)
        document, available, unavailable, _info = _build(docs, plan, resolve=resolve)
        return plan, document, available, unavailable

    def test_approved_transport_alternative_preserves_selectors(self) -> None:
        _plan, pool_doc, _a, _u = self._render(None)
        pool_selectors = render.rendered_selectors(pool_doc)
        claude_pool = {entry["alias"] for entry in pool_doc["oauth-model-alias"]["claude"]}
        self.assertTrue(claude_pool)
        self.assertNotIn(render.EXCLUDED_MODELS_KEY, pool_doc)
        plan, key_doc, available, unavailable = self._render(self._ledger("api-key"))
        self.assertEqual(render.rendered_selectors(key_doc), pool_selectors)
        self.assertNotIn("claude", key_doc["oauth-model-alias"])
        self.assertEqual(key_doc[render.EXCLUDED_MODELS_KEY], {"claude": ["*"]})
        sections = [section for section in key_doc["claude-api-key"]
                    if section["base-url"] == "https://api.anthropic.com"]
        self.assertEqual(len(sections), 1)
        section = sections[0]
        self.assertEqual(section["auth-header"], "x-api-key")
        self.assertEqual(section["cloak"], {"mode": "never"})
        self.assertEqual(section["api-key"], "dummy-platform_anthropic_api_key")
        self.assertEqual({entry["alias"] for entry in section["models"]}, claude_pool)
        self.assertIn("anthropic", available)
        self.assertEqual(plan.transports["anthropic"].problem, None)
        # Selection is a render-copy marker only: the trusted docs never change.
        self.assertEqual(self._docs()["providers"]["providers"]["anthropic"]["transport"]["kind"], "oauth-pool")

    def test_no_fallback_to_the_pool(self) -> None:
        # Missing key: the key route is unavailable and the pool serves nothing.
        _plan, document, available, unavailable = self._render(
            self._ledger("api-key"), resolve=lambda name: None if name == "PLATFORM_ANTHROPIC_API_KEY" else "d")
        self.assertNotIn("anthropic", available)
        self.assertNotIn("claude", document["oauth-model-alias"])
        self.assertEqual(document[render.EXCLUDED_MODELS_KEY], {"claude": ["*"]})
        # An approval for another descriptor withholds the key (never the pool).
        plan, document, available, _u = self._render(self._ledger("api-key", approved=False))
        self.assertIn("not approved", plan.transports["anthropic"].problem)
        self.assertNotIn("anthropic", available)
        self.assertNotIn("claude", document["oauth-model-alias"])

    def test_openai_platform_needs_a_reviewed_line_and_pool_is_the_default(self) -> None:
        docs = self._docs()
        # The fixture reviews no OpenAI line for the key route: offered, not selectable.
        self.assertTrue(operator.transport_alternative("openai", "api-key").available)
        self.assertIn("no OpenAI model is reviewed for its api-key transport",
                      operator.transport_problem(docs, "openai", "api-key"))
        self.assertIsNone(operator.transport_problem(docs, "anthropic", "api-key"))
        self.assertIsNone(operator.transport_problem(docs, "anthropic", "oauth-pool"))
        self.assertIn("not an OAuth-pool", operator.transport_problem(docs, "kimi", "api-key"))
        plan, document, _a, _u = self._render(self._ledger("oauth-pool"))
        self.assertEqual(plan.transports, {})
        _plan, baseline, _a, _u = self._render(None)
        self.assertEqual(render.emit_yaml(document), render.emit_yaml(baseline))
        alternative = operator.transport_alternative("openai", "api-key")
        ledger = _ledger(transport_choices={"openai": "api-key"},
                         routes={"openai": operator.transport_route_record(alternative, at="2026-09-30T00:00:00Z")})
        # Recorded by hand with no reviewed line: nothing of the route is
        # rendered and the account pool stays excluded (no fallback).
        plan, document, _available, _u = self._render(ledger)
        self.assertIsNone(plan.transports["openai"].problem)
        self.assertNotIn("codex", document["oauth-model-alias"])
        self.assertNotIn(render.CODEX_KEY_SECTION, document)
        self.assertEqual(document[render.EXCLUDED_MODELS_KEY], {"codex": ["*"]})
        # A closed alternative recorded by hand never renders either.
        reason = "closed for this check"
        with mock.patch.dict(operator.TRANSPORT_ALTERNATIVES, {("openai", "api-key"): dataclasses.replace(
                alternative, available=False, closed_reason=reason)}):
            self.assertEqual(operator.transport_problem(docs, "openai", "api-key"), reason)
            plan, document, available, _u = self._render(ledger)
        self.assertEqual(plan.transports["openai"].problem, reason)
        self.assertNotIn("openai", available)
        self.assertNotIn("codex", document["oauth-model-alias"])
        self.assertNotIn(render.CODEX_KEY_SECTION, document)

    def test_transport_secret_is_t1_owned(self) -> None:
        files = {"acme": {"version": 1, "provider": {
            "display": "Acme", "kind": "anthropic-compatible", "base_url": "https://api.acme.example/anthropic",
            "auth": {"kind": "bearer", "secret_ref": "env:PLATFORM_ANTHROPIC_API_KEY"},
            "independence_family": "acme"}, "lines": {}}}
        layer = _layer(files)
        self.assertNotIn("acme", layer.providers)
        self.assertTrue(any("one secret has one origin" in text for text in _texts(layer)))

    def test_captures_on_the_pool_route_never_retarget_onto_the_key(self) -> None:
        docs = self._docs()
        capture = {"provider": "anthropic", "rd": operator.catalog_route_digest(docs["providers"]["providers"]["anthropic"]),
                   "wire": "claude-opfix-cap-1", "proxy_contract": None, "display": "Cap", "context_tokens": 200000,
                   "source": "operator:custom-opfix-cap", "since_catalog": 1, "overlay": None}
        pool = operator.render_plan(docs, operator.empty_layer(), _ledger(aliases={"custom-opfix-cap": capture}))
        self.assertIn("custom-opfix-cap", pool.captures)
        ledger = self._ledger("api-key")
        ledger = dataclasses.replace(ledger, aliases={"custom-opfix-cap": capture})
        keyed = operator.render_plan(docs, operator.empty_layer(), ledger)
        self.assertNotIn("custom-opfix-cap", keyed.captures)


class TransportDoctorTests(unittest.TestCase):
    """The OAuth doctor rule: the selected transport is Info; an
    unapproved choice or a missing key is BLOCK (never a pool fallback)."""

    snapshot = DoctorFindingsTests.snapshot
    findings = DoctorFindingsTests.findings

    def _snap(self, *, approved=True):
        alternative = operator.transport_alternative("anthropic", "api-key")
        route = operator.transport_route_record(alternative, at="2026-09-30T00:00:00Z")
        if not approved:
            route["rd"] = "1" * 64
        return self.snapshot({}, ledger=_ledger(transport_choices={"anthropic": "api-key"},
                                                routes={"anthropic": route}))

    def test_transport_findings(self) -> None:
        found = self.findings(self._snap())
        self.assertEqual(found.blocks, ())
        self.assertTrue(any("provider anthropic transport: api-key (header env:PLATFORM_ANTHROPIC_API_KEY"
                            " → https://api.anthropic.com); the claude OAuth pool serves nothing" in line
                            for line in found.info), found.info)
        found = self.findings(self._snap(), unset_secrets=("PLATFORM_ANTHROPIC_API_KEY",))
        self.assertTrue(any("PLATFORM_ANTHROPIC_API_KEY is not set" in line for line in found.blocks), found.blocks)
        found = self.findings(self._snap(approved=False))
        self.assertTrue(any("is not approved for the current reviewed descriptor" in line
                            and "claude-multi providers transport anthropic oauth-pool" in line
                            for line in found.blocks), found.blocks)


class OnboardingRefusalCatalogueTests(unittest.TestCase):
    """Exact-text owners; conditions, not a one-code/one-E-id table."""

    def problem(self, document, eid, *, pid="acme"):
        found = [p for p in _layer({pid: document}).problems if p.error_id == eid]
        self.assertTrue(found, _texts(_layer({pid: document})))
        return found[0]

    def test_E01_duplicate_key(self):
        layer = operator.validate_layer(_docs(), {"acme": b'{"version":1,"lines":{},"lines":{}}'}, schemas=_schemas())
        self.assertEqual(layer.problems[0].error_id, "E01")
        self.assertEqual(layer.problems[0].text(), 'providers.d/acme.json: duplicate key "lines" — each line key appears once')

    def test_E02_context_schema(self):
        doc = _fixture_files()["acme"]
        doc["lines"]["custom-acme-small"]["context"]["declared_tokens"] = 200000.0
        self.assertEqual(self.problem(doc, "E02").text(),
                         'providers.d/acme.json: lines.custom-acme-small.context.declared_tokens: must be an integer 8192..2097152 (got 200000)')

    def test_E03_catalog_provider(self):
        pid = next(k for k,p in _docs()["providers"]["providers"].items() if p["transport"]["kind"] == "direct")
        self.assertEqual(self.problem(_fixture_files()["acme"], "E03", pid=pid).text(),
                         f'providers.d/{pid}.json: provider: "{pid}" is a catalog provider — remove the "provider" block; lines only')

    def test_E04_authored_evidence(self):
        doc = _fixture_files()["acme"]
        doc["lines"]["custom-acme-small"]["validated_tokens"] = 200000
        self.assertEqual(self.problem(doc, "E04").text(),
                         'providers.d/acme.json: lines.custom-acme-small.validated_tokens: evidence is tool-owned — run claude-multi models qualify custom-acme-small --context N')

    def test_E05_namespace(self):
        doc = _fixture_files()["acme"]
        doc["lines"] = {"badkey": _acme_line()}
        self.assertEqual(self.problem(doc, "E05").text(),
                         'providers.d/acme.json: lines.badkey: operator keys start with "custom-" — rename to custom-badkey')

    def test_E06_catalog_wire(self):
        docs = _docs()
        key, line = next((k, v) for k,v in docs["models-v2"]["models"].items()
                         if docs["providers"]["providers"][v["provider"]]["transport"]["kind"] == "direct")
        doc = {"version": 1, "lines": {"custom-duplicate": _acme_line(wire_model=line["wire_model"])}}
        pid = line["provider"]
        self.assertEqual(self.problem(doc, "E06", pid=pid).text(),
                         f'providers.d/{pid}.json: lines.custom-duplicate: {pid}/{line["wire_model"]} is the catalog line "{key}" — admit or bind that line instead')

    def test_E07_unaudited_agent_route(self):
        doc = _fixture_files()["lanbox"]
        key = next(iter(doc["lines"]))
        doc["lines"][key].update(capabilities=["lead", "agents"], roles=["cm-reviewer"])
        self.assertEqual(self.problem(doc, "E07", pid="lanbox").text(),
                         f'providers.d/lanbox.json: lines.{key}.capabilities: "agents" needs a retention-audited route; openai-compatible-lan is lead-only until the compat audit')

    def test_E08_secret_value_never_echoed(self):
        doc = _fixture_files()["acme"]
        doc["notes"] = "sk-" + "x" * 50
        self.assertEqual(self.problem(doc, "E08").text(),
                         'providers.d/acme.json: notes: a secret value is not allowed — store it with claude-multi providers set-key acme; write "env:NAME"')

    def test_E09_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            env = {"HOME": root}
            pdir = state.ensure_private_dir(operator.providers_dir(env))
            target = pdir / "acme.json"
            target.write_bytes(b'{}')
            target.chmod(0o664)
            problem = operator.read_providers_dir(pdir).problems[0]
            self.assertEqual(problem.error_id, "E09")
            self.assertEqual(problem.text(), f'providers.d/acme.json: is writable by group/others (0664) — chmod 600 {target}')

    def test_E10_listing_origin(self):
        doc = _fixture_files()["acme"]
        doc["provider"]["listing"] = {"url": "https://other.example/models", "shape": "openai", "auth": "provider"}
        origin, _ = operator.normalize_endpoint(doc["provider"]["base_url"], keyed=True, lan=False)
        self.assertEqual(self.problem(doc, "E10").text(),
                         f'providers.d/acme.json: provider.listing.url: origin https://other.example ≠ base_url origin {origin}: the key is sent only to its own origin (auth "none" for a public listing)')

    def test_E14_unauthorized_pool_override_not_supported_lines(self):
        pid = next(k for k,p in _docs()["providers"]["providers"].items() if p["transport"]["kind"] == "oauth-pool")
        self.assertEqual(self.problem(_fixture_files()["acme"], "E14", pid=pid).text(),
                         f"providers.d/{pid}.json: provider: openai/anthropic lines ship with claude-multi (pool admission is the catalog's) — claude-multi models --candidates")
        doc = {"version": 1, "lines": {"custom-pool": _acme_line(wire_model="claude-fixture-9")}}
        layer = _layer({"anthropic": doc})
        self.assertFalse(any(p.error_id == "E14" for p in layer.problems))
        doc["lines"]["custom-pool"]["wire_model"] = "bad-pool-wire"
        shape = self.problem(doc, "E02", pid="anthropic")
        self.assertEqual(shape.code, "pool")

    def test_E15_header_auth(self):
        doc = _fixture_files()["acme"]
        doc["provider"]["auth"].update(kind="header", header="authorization")
        self.assertEqual(self.problem(doc, "E15").text(),
                         'providers.d/acme.json: provider.auth.header: only x-api-key is honoured for header auth — use kind "bearer"')

    def test_E17_static_header_credentials(self):
        doc = _fixture_files()["acme"]
        doc["provider"]["headers"] = {"X-Api-Key": "not-a-real-secret"}
        self.assertEqual(self.problem(doc, "E17").text(),
                         'providers.d/acme.json: provider.headers.X-Api-Key: credential-carrying header names are refused')

    def test_E18_keyed_cleartext(self):
        doc = _fixture_files()["acme"]
        doc["provider"]["base_url"] = "http://acme.example"
        self.assertEqual(self.problem(doc, "E18").text(),
                         'providers.d/acme.json: provider.base_url: base_url refused: a secret is sent only over https')


class T1OverlayTests(unittest.TestCase):
    _capture = OverlayEmissionTests._capture

    def test_context_output_and_reviewed_levels(self) -> None:
        docs = _t1_overlay_docs()
        line = docs["models"]["models"]["opfix-t1"]
        line["output"] = {"declared_tokens": 128000, "source": "docs", "source_ref": "fixture docs"}
        line["efforts"] = {
            level: {"selector": f"gpt-multi-fixture-next-{level}[1m]", "proxy_contract": f"reasoning-effort-{level}"}
            for level in ("low", "medium", "high", "xhigh", "max")
        }
        for channel in ("codex", "claude"):
            with self.subTest(channel=channel):
                candidate = copy.deepcopy(docs)
                entry = candidate["models"]["models"]["opfix-t1"]
                if channel == "claude":
                    entry.update(provider="anthropic", wire_model="claude-fixture-99", selector="claude-fixture-99[1m]",
                                 efforts=list(line["efforts"]), registry_overlay={"channel": channel})
                    candidate["providers"]["providers"]["anthropic"]["passthrough_routes"].append(
                        {"name": entry["wire_model"], "fork": True})
                plan = operator.render_plan(candidate, _layer({}, docs=candidate), None)
                document, *_ = _build(candidate, plan)
                overlay = document[render.OVERLAY_KEY][channel]
                self.assertEqual(len(overlay), 1)
                row = overlay[0]
                self.assertEqual(row["max-context-length"], entry["context"]["provider_tokens"])
                self.assertEqual(row["max-completion-tokens"], 128000)
                self.assertEqual(row["thinking"], {"levels": list(line["efforts"])})
                self.assertIsNone(render.overlay_entry_problem(row))
                self.assertNotIn("source", row)
                self.assertNotIn("source_ref", row)
                del entry["output"]
                without = operator.render_plan(candidate, _layer({}, docs=candidate), None)
                document, *_ = _build(candidate, without)
                self.assertNotIn("max-completion-tokens", document[render.OVERLAY_KEY][channel][0])

    def test_retired_and_capture_output_provenance(self) -> None:
        from claude_multi import continuity

        docs = _t1_overlay_docs()
        retired = docs["retired"]["retired"]["opfix-ret"]
        retired["output"] = {"declared_tokens": 64000, "source": "registry", "source_ref": "fixture registry"}
        bundle = dataclasses.replace(catalog.load_catalog(FIXTURE_ROOT), docs=docs)
        aliases = continuity.seed_entries(bundle)
        capture = self._capture("gpt-fixture-captured", retired["last_wire"])
        capture["gpt-fixture-captured"]["overlay"]["max_completion_tokens"] = 32000
        ledger = _ledger(aliases=capture)
        plan = operator.render_plan(docs, _layer({}, docs=docs), ledger)
        document, *_ = _build(docs, plan, aliases)
        rows = [r for r in document[render.OVERLAY_KEY]["codex"] if r["name"] == retired["last_wire"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["max-completion-tokens"], 64000)  # retired T1 outranks capture
        self.assertNotIn("thinking", rows[0])  # no retired effort list; never guess
        del docs["retired"]["retired"]["opfix-ret"]
        plan = operator.render_plan(docs, _layer({}, docs=docs), ledger)
        document, *_ = _build(docs, plan)
        rows = [r for r in document[render.OVERLAY_KEY]["codex"] if r["name"] == retired["last_wire"]]
        self.assertEqual(rows[0]["max-completion-tokens"], 32000)
