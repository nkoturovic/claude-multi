"""Tests for the trusted catalog seed, references, and bundle hash."""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from claude_multi import catalog, pin, strict_json, validate
from claude_multi.catalog import CatalogError
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog
from _layout import ASSET_ENTRIES, PATCH_DIR, UPSTREAM_JSON


# Split: shape/evidence tests and the upgrade-synced literals assert the
# SHIPPED catalog (CATALOG_ROOT); validator/mutation behaviour runs on the
# frozen FIXTURE_ROOT so a shipped model change never moves those tests.
CATALOG_ROOT = SHIPPED_ROOT


def assert_gpt6_probe_context(case: unittest.TestCase, context: dict) -> None:
    """A GPT-6 line's context carries the
    approved 2026-09-26 acceptance probe. Relations only, never the numbers
    (the fixture rule): the 1M client class stays, the provider window sits below it
    and equals the stated limit, the accepted size is validated and cited,
    and the operating ceiling still binds (operating capacity unchanged)."""

    from claude_multi import composition

    client = context["client_tokens"]
    provider = context["provider_tokens"]
    validated = context["validated_tokens"]
    case.assertGreaterEqual(client, 1_000_000)
    case.assertLess(provider, client)
    case.assertEqual(context["provider_stated_limit_tokens"], provider)
    case.assertLessEqual(validated, provider)
    case.assertEqual(
        composition.operating_window(provider), composition.operating_window(client)
    )
    case.assertEqual(context["ordinary_profile"], "large")
    case.assertIsNone(context["scalar_tokens"])
    case.assertNotIn("user_reported_tokens", context)
    qualification = context["qualification"]
    case.assertLessEqual(len(qualification), 768)
    case.assertIn("2026-09-26", qualification)
    case.assertIn(f"{validated:,}", qualification)
    case.assertIn("rejected on luna", qualification)
    case.assertIn("Near-limit retrieval is unreliable", qualification)

# Selector shape by transport (an invariant, replacing the exact shipped
# selector inventory): codex-pool lanes are ``gpt-multi-*``; every other
# lane is ``claude-multi-*`` or rides its own provider's canonical
# passthrough route. The launcher's stale-selector filter keys on exactly
# these prefixes, so a new model must follow them.
# The frozen catalog-32 (2.x) selector bases that live sessions, records and
# the running gateway depend on, each with its catalog-32
# (provider, wire, proxy contract). Catalog 33 serves every one of them: as
# a live line selector, an Anthropic passthrough route, or a continuity seed
# alias from retired.json (FrozenSelectorCoverageTests). Additions stay
# free, but dropping one of these fails loudly.
FROZEN_2X_SELECTORS = {
    "claude-fable-5": ("anthropic", "claude-fable-5", None),
    "claude-fable-5-1": ("anthropic", "claude-fable-5-1", None),
    "claude-opus-5": ("anthropic", "claude-opus-5", None),
    "claude-opus-5-5": ("anthropic", "claude-opus-5-5", None),
    "claude-opus-4-8": ("anthropic", "claude-opus-4-8", None),
    "claude-multi-opus-5-5": ("anthropic", "claude-opus-5-5", None),
    "claude-multi-opus-5": ("anthropic", "claude-opus-5", None),
    "claude-multi-opus-4-8": ("anthropic", "claude-opus-4-8", None),
    "claude-multi-kimi-k3": ("kimi", "k3", "output-config-max"),
    "gpt-multi-sol-high": ("openai", "gpt-5.6-sol", "reasoning-effort-high"),
    "gpt-multi-sol-xhigh": ("openai", "gpt-5.6-sol", "reasoning-effort-xhigh"),
    "gpt-multi-gpt55-high": ("openai", "gpt-5.5", "reasoning-effort-high"),
    "gpt-multi-astra-high": ("openai", "gpt-6-astra", "reasoning-effort-high"),
    "gpt-multi-astra-xhigh": ("openai", "gpt-6-astra", "reasoning-effort-xhigh"),
    "claude-multi-qwen38-max": ("qwen", "qwen3.8-max", "reasoning-effort-xhigh"),
    "claude-multi-glm52-max": ("qwen", "glm-5.2", "reasoning-effort-max"),
    "claude-multi-deepseek-flash-high": ("deepseek", "deepseek-flash", "output-config-high"),
    "claude-multi-deepseek-flash-max": ("deepseek", "deepseek-flash", "output-config-max"),
    "claude-multi-deepseek-pro-high": ("deepseek", "deepseek-v4-pro", "output-config-high"),
    "claude-multi-deepseek-pro-max": ("deepseek", "deepseek-v4-pro", "output-config-max"),
    "claude-multi-grok46-high": ("openrouter", "x-ai/grok-4.6", "output-config-high"),
    "claude-multi-grok46-xhigh": ("openrouter", "x-ai/grok-4.6", "output-config-xhigh"),
    "claude-multi-muse-spark-high": ("meta", "muse-spark-1.3", "output-config-high"),
    "claude-multi-muse-spark-xhigh": ("meta", "muse-spark-1.3", "output-config-xhigh"),
    "claude-multi-muse-spark-contributor-high": (
        "meta", "muse-spark-1.3-contributor", "output-config-high"),
    "claude-multi-muse-spark-contributor-xhigh": (
        "meta", "muse-spark-1.3-contributor", "output-config-xhigh"),
    "claude-multi-qwen-flash-next": ("llm-local", "qwen3.8-flash-next", None),
}
RETAINED_SELECTOR_BASES = frozenset(FROZEN_2X_SELECTORS)
CODEX_SELECTOR_PREFIX = "gpt-multi-"
CLAUDE_SELECTOR_PREFIX = "claude-multi-"
REMOVED_PATTERNS = (
    "claude-multi-fable-5",
    "claude-multi-sol-",
    "claude-multi-gpt55",
    "conserve-",
)


def _raw(root: Path = CATALOG_ROOT) -> dict:
    return catalog.load_raw(root)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _mutate(mutator, root: Path = FIXTURE_ROOT) -> list[str]:
    raw = _raw(root)
    mutator(raw)
    return catalog.validate_catalog(raw)


def _copy_tree(case: unittest.TestCase, root: Path = FIXTURE_ROOT) -> Path:
    temporary = Path(tempfile.mkdtemp(prefix="claude-multi-catalog-"))
    case.addCleanup(lambda: shutil.rmtree(temporary, ignore_errors=True))
    # The shipped root is the repository root; copy only its asset
    # entries (the fixture root holds exactly these).
    (temporary / "claude-multi").mkdir()
    for name in ASSET_ENTRIES:
        source, target = root / name, temporary / "claude-multi" / name
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            shutil.copyfile(source, target)
    # Copies may originate from a read-only store path; make them writable.
    for root_dir, dirs, files in os.walk(temporary / "claude-multi"):
        os.chmod(root_dir, 0o755)
        for name in files:
            os.chmod(Path(root_dir) / name, 0o644)
    return temporary / "claude-multi"


class SeedLoadTests(unittest.TestCase):
    def test_seed_loads_clean(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        self.assertEqual(
            # llm-local left the catalog (externalized-provider retirement).
            set(bundle.providers), {"anthropic", "deepseek", "kimi", "meta", "openai", "openrouter", "qwen"}
        )
        # Invariants, not the exact shipped model inventory — adding a
        # well-formed model must not move this test. Every model rides a
        # loaded provider. On the v2 lines (the v1 view, the 2.x
        # default projection and the legacy roles view are deleted).
        self.assertTrue(bundle.lines)
        for model_id, model in bundle.lines.items():
            with self.subTest(model=model_id):
                self.assertIn(model["provider"], bundle.providers)
        self.assertNotIn("compositions/default", bundle.docs)
        self.assertIs(bundle.roles, bundle.roles_v2)
        self.assertEqual(set(bundle.roles), set(catalog.ROLE_IDS))

    def test_validate_catalog_accepts_seed(self) -> None:
        self.assertEqual(catalog.validate_catalog(_raw()), [])

    def test_retained_alias_split_exact(self) -> None:
        # The exact shipped selector inventory became invariants, so a
        # well-formed model addition keeps this green while the alias split
        # (passthrough names vs line selectors, prefix per transport, no
        # collisions) stays enforced. Iterate every v2 line selector
        # (New lines included) and pin the catalog-33 shape rules the
        # validator leaves to shipped tests: canonical Anthropic
        # selectors, gateway-effort selector = key + effort, and
        # contract level == effort.
        bundle = catalog.load_catalog(CATALOG_ROOT)
        passthrough: dict[str, str] = {}
        for provider_id, provider in bundle.providers.items():
            for route in provider["passthrough_routes"]:
                self.assertNotIn(route["name"], passthrough)
                passthrough[route["name"]] = provider_id
        seen: dict[str, str] = {}
        for model_id, model in bundle.lines.items():
            provider = bundle.providers[model["provider"]]
            transport = provider["transport"]
            one_m = model["context"]["client_tokens"] >= 1_000_000
            suffix = "[1m]" if one_m else ""
            if provider["adapter"] == "cliproxy-oauth-claude-v1":
                with self.subTest(line=model_id, rule="canonical selector"):
                    self.assertEqual(model["selector"], model["wire_model"] + suffix)
            for level, selector, contract in catalog.line_selectors(model):
                base = selector.removesuffix("[1m]")
                where = f"{model_id}.{level}"
                with self.subTest(selector=where):
                    self.assertNotIn(base, seen, f"duplicates {seen.get(base)}")
                    seen[base] = where
                    # The [1m] suffix is exactly the 1M client classification.
                    self.assertEqual(selector.endswith("[1m]"), one_m)
                    if base in passthrough:
                        # A selector may only ride its own provider's route.
                        self.assertEqual(passthrough[base], model["provider"])
                    elif transport["kind"] == "oauth-pool" and transport.get("pool") == "codex":
                        self.assertTrue(base.startswith(CODEX_SELECTOR_PREFIX), base)
                    else:
                        self.assertTrue(base.startswith(CLAUDE_SELECTOR_PREFIX), base)
                    if isinstance(model["efforts"], dict):
                        prefix = (
                            CODEX_SELECTOR_PREFIX
                            if transport.get("pool") == "codex"
                            else CLAUDE_SELECTOR_PREFIX
                        )
                        generation = ""
                        if model_id == "sol":  # the reviewed, separable generation rename
                            generation = model["generation"].replace(".", "-") + "-"
                        self.assertEqual(selector, f"{prefix}{model_id}-{generation}{level}{suffix}")
                        self.assertTrue(contract.endswith("-" + level), contract)
                    # Removed aliases stay removed (also pinned bundle-wide
                    # by test_removed_aliases_absent_from_bundle).
                    for pattern in REMOVED_PATTERNS:
                        self.assertNotIn(pattern, base)

    def test_retained_selectors_never_renamed_or_removed(self) -> None:
        # The shipped selector bases must stay a SUPERSET of
        # the retained set (running sessions and 2.x records reference them).
        # A selector leaves the live lines only through a retired
        # entry, whose selectors the continuity set keeps serving.
        bundle = catalog.load_catalog(CATALOG_ROOT)
        bases = {
            route["name"]
            for provider in bundle.providers.values()
            for route in provider["passthrough_routes"]
        }
        bases |= {
            selector.removesuffix("[1m]")
            for model in bundle.lines.values()
            for _level, selector, _contract in catalog.line_selectors(model)
        }
        bases |= set(bundle.retired_selector_index())
        self.assertEqual(sorted(RETAINED_SELECTOR_BASES - bases), [])

    def test_removed_aliases_absent_from_bundle(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        blob = strict_json.canonical_bytes(bundle.bundle).decode("utf-8")
        for pattern in REMOVED_PATTERNS:
            self.assertNotIn(pattern, blob)

    def test_fork_rule_only_on_canonical_routes(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        forked = [
            route["name"]
            for provider in bundle.providers.values()
            for route in provider["passthrough_routes"]
            if route["fork"]
        ]
        self.assertEqual(
            sorted(forked),
            [
                "claude-fable-5",
                "claude-fable-5-1",
                "claude-opus-4-8",
                "claude-opus-5",
                "claude-opus-5-5",
                "claude-sonnet-5",
                "claude-sonnet-5-5",
            ],
        )

    def test_every_shipped_route_is_justified(self) -> None:
        # A route exists for a live Anthropic line wire, a retired
        # Anthropic last_wire (continuity), or a client refusal-fallback
        # target (U23) — nothing else.
        bundle = catalog.load_catalog(CATALOG_ROOT)
        live = {
            line["wire_model"]
            for line in bundle.lines.values()
            if line["provider"] == "anthropic"
        }
        retired = {
            entry["last_wire"]
            for entry in bundle.retired.values()
            if entry["provider"] == "anthropic"
        }
        for provider_id, provider in bundle.providers.items():
            for route in provider["passthrough_routes"]:
                with self.subTest(route=route["name"]):
                    self.assertEqual(provider_id, "anthropic")
                    self.assertIn(
                        route["name"],
                        live | retired | set(catalog.REFUSAL_FALLBACK_WIRES),
                    )
        self.assertLessEqual(set(catalog.REFUSAL_FALLBACK_WIRES), {
            route["name"] for route in bundle.providers["anthropic"]["passthrough_routes"]
        })

    def test_context_evidence_separated(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        sol = bundle.lines["sol"]["context"]
        self.assertGreater(sol["declared_tokens"], sol["client_tokens"])
        self.assertNotIn("agent_client_tokens", sol)
        self.assertNotIn("provider_stated_limit_tokens", sol)
        self.assertIn("inherited GPT-6 route bound, unverified for 6.1", sol["qualification"])
        for historical in ("343,541", "868,925", "Near-limit retrieval", "G5-route"):
            self.assertNotIn(historical, sol["qualification"])

    def test_settings_carry_no_worktree_keys(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        self.assertNotIn("worktree", bundle.docs["settings"])
        self.assertEqual(
            set(bundle.docs["settings"]),
            {"disableWorkflows", "workflowSizeGuideline", "workflowKeywordTriggerEnabled"},
        )

    def test_version_json_is_the_development_release_and_catalog(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        self.assertEqual(bundle.docs["version"]["launcher_version"], "1.1.0-dev")
        self.assertEqual(bundle.docs["version"]["catalog_version"], 37)

    def test_removed_lines_are_retired_without_successor(self) -> None:
        # Catalog 33: out-of-favour lines leave; their providers stay.
        bundle = catalog.load_catalog(CATALOG_ROOT)
        for key in ("qwen38", "glm52", "kimi-k3", "grok46", "deepseek-pro"):
            with self.subTest(key=key):
                self.assertNotIn(key, bundle.lines)
                self.assertIsNone(bundle.retired[key]["successor"])
                self.assertIn(bundle.retired[key]["provider"], bundle.providers)
                resolution = bundle.resolve_key(key)
                self.assertIsNone(resolution.key)
                self.assertIn("needs a model choice", resolution.notice)
        # qwen38's max selector records its 2.x rule (xhigh), not max.
        self.assertEqual(
            bundle.retired["qwen38"]["selectors"],
            {"claude-multi-qwen38-max[1m]": "reasoning-effort-xhigh"},
        )

    def test_opus_line_serves_every_role(self) -> None:
        # The opus line serves every role, lead-capable.
        bundle = catalog.load_catalog(CATALOG_ROOT)
        opus = bundle.lines["opus"]
        self.assertEqual(opus["roles"], "all")
        self.assertEqual(opus["capabilities"], ["lead", "agents"])
        for key in ("opus55", "opus5", "opus@4.8"):
            with self.subTest(key=key):
                self.assertEqual(bundle.resolve_key(key).key, "opus")
        self.assertEqual(bundle.retired["opus@4.8"]["roles"], ["cm-reviewer"])


class FrozenSelectorCoverageTests(unittest.TestCase):
    """Every frozen catalog-32 selector stays served by catalog 33.

    Each base must satisfy at least one of (not "exactly one": that is
    false for the data itself):
      (i)   a live line selector base, same provider and contract (the wire
            may differ only for a gateway-effort codex line: sol retargets);
      (ii)  an Anthropic passthrough route;
      (iii) a continuity seed alias (a retired selector) with the identical
            (provider, wire, contract).
    Wherever several hold, (provider, wire, contract) agree across them.
    """

    EXPECTED = {
        "claude-fable-5": {"ii", "iii"},
        "claude-fable-5-1": {"i", "ii", "iii"},
        "claude-opus-5": {"ii"},
        "claude-opus-5-5": {"i", "ii"},
        "claude-opus-4-8": {"ii"},
        "gpt-multi-sol-high": {"iii"},
        "gpt-multi-sol-xhigh": {"iii"},
        "gpt-multi-astra-high": {"i"},
        "gpt-multi-astra-xhigh": {"i"},
        "claude-multi-deepseek-flash-high": {"i"},
        "claude-multi-deepseek-flash-max": {"i"},
    }

    def test_no_trusted_wire_has_static_header_override(self) -> None:
        # Complete config.override_header census of the
        # pinned pre-patch registry/models/models.json (sha256
        # ca457e5269f67068fcd13de77a4eb55da603917cd77309e61e4661ef4327c85b).
        # All four entries (codex-free/team/plus/pro) name this same wire.
        # Keep this small, audited projection hermetic: a source-pin change
        # fails closed and requires re-cutting the census, not a store lookup
        # or a skip in standalone/sandbox tests. It is not a T1 route claim.
        pin = json.loads(UPSTREAM_JSON.read_text())
        self.assertEqual(pin["upstream"]["version"], "7.3.15")
        self.assertEqual(pin["source"]["nix_hash"], "sha256-s6Vdpcl7Hr85azsZ2DpkEQyod9eE9UQizKiYAJtguwY=")
        override_wires = {"gpt-5.6-luna"}
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        trusted = {line["wire_model"] for line in bundle.lines.values()}
        trusted.update(entry["last_wire"] for entry in bundle.retired.values())
        trusted.update(route["name"] for provider in bundle.providers.values()
                       for route in provider.get("passthrough_routes", []))
        self.assertFalse(trusted & override_wires,
                         f"T1/retired wires have static header overrides: {sorted(trusted & override_wires)}")

    def test_every_frozen_selector_is_served(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        live: dict[str, tuple[str, str, str | None, bool]] = {}
        for line in bundle.lines.values():
            codex = bundle.providers[line["provider"]]["transport"].get("pool") == "codex"
            for _level, selector, contract in catalog.line_selectors(line):
                live[selector.removesuffix("[1m]")] = (
                    line["provider"], line["wire_model"], contract, codex
                )
        routes = {
            route["name"]
            for route in bundle.providers["anthropic"]["passthrough_routes"]
        }
        seed = {
            base: (entry["provider"], entry["last_wire"], contract)
            for base, (_key, entry, contract) in bundle.retired_selector_index().items()
        }
        self.assertEqual(len(FROZEN_2X_SELECTORS), 27)
        for base, (provider, wire, contract) in sorted(FROZEN_2X_SELECTORS.items()):
            with self.subTest(base=base):
                # The frozen GPT-5.6 fixture is unchanged. Catalog 33 already
                # retargeted these two bases to GPT-6; the catalog retains that exact
                # historical target under sol@6, not the new Sol wire.
                if base in ("gpt-multi-sol-high", "gpt-multi-sol-xhigh"):
                    wire = bundle.retired["sol@6"]["last_wire"]
                holds: set[str] = set()
                facts: list[tuple[str, str, str | None]] = []
                if base in live:
                    l_provider, l_wire, l_contract, codex = live[base]
                    self.assertEqual((l_provider, l_contract), (provider, contract))
                    if not codex:
                        self.assertEqual(l_wire, wire)
                    holds.add("i")
                    if l_wire == wire:
                        facts.append((l_provider, l_wire, l_contract))
                if base in routes:
                    holds.add("ii")
                    facts.append(("anthropic", base, None))
                if base in seed:
                    self.assertEqual(seed[base], (provider, wire, contract))
                    holds.add("iii")
                    facts.append(seed[base])
                self.assertTrue(holds, f"{base} is no longer served")
                self.assertEqual(holds, self.EXPECTED.get(base, {"iii"}))
                if facts:  # sol's retargeted live wire is the only exception
                    self.assertEqual(set(facts), {(provider, wire, contract)})

    def test_rules_named_in_the_spec(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        index = bundle.retired_selector_index()
        self.assertEqual(index["claude-multi-qwen38-max"][2], "reasoning-effort-xhigh")
        self.assertEqual(index["claude-multi-kimi-k3"][2], "output-config-max")
        self.assertIn("filter-thinking", bundle.providers["kimi"]["payload_contracts"])
        # Historical GPT-5.6 -> GPT-6 retarget, now owned by sol@6 continuity.
        self.assertNotEqual(bundle.lines["sol"]["wire_model"], bundle.retired["sol@6"]["last_wire"])
        for base in ("gpt-multi-sol-high", "gpt-multi-sol-xhigh"):
            self.assertEqual(index[base][0], "sol@6")
        self.assertNotIn("sol@5.6", bundle.retired)

    def test_render_serves_every_frozen_selector_with_its_rule(self) -> None:
        # The shipped render (v2 lines + the continuity seed, every
        # secret dummy-resolved) serves all 27 frozen bases, with each
        # retired selector's recorded effort rule and kimi's filter.
        from claude_multi import continuity, render

        from claude_multi import operator

        bundle = catalog.load_catalog(CATALOG_ROOT)
        externalized = catalog.externalized_providers(bundle.docs)
        external_bases = {
            base for base, (_provider, _wire, _contract) in FROZEN_2X_SELECTORS.items()
            if _provider in externalized
        }

        def build(docs):
            return render.build_config_document(
                docs["gateway"], docs["providers"]["providers"], docs["models-v2"]["models"],
                home=Path("/home/test"), gateway_token="a" * 64,
                resolve_secret=lambda _name: "dummy-secret",
                continuity=continuity.seed_only(bundle)["aliases"],
                quiet_providers=frozenset(catalog.externalized_providers(docs)),
            )

        # An externalized provider's frozen selectors stay continuity
        # data; they are served exactly while its sample is declared.
        document, _available, unavailable, info = build(bundle.docs)
        self.assertEqual(unavailable, ())
        self.assertEqual(info["notices"], ())
        self.assertEqual(set(FROZEN_2X_SELECTORS) - render.rendered_selectors(document), external_bases)
        schemas = operator.load_schemas(CATALOG_ROOT)
        for provider_id, sample in sorted(externalized.items()):
            raw = (CATALOG_ROOT / catalog.EXAMPLES_PROVIDERS_DIR / sample).read_bytes()
            layer = operator.resolve_provider_document(bundle.docs, provider_id, raw, schemas=schemas)
            self.assertEqual(layer.problems, (), provider_id)
            docs = operator.merge_docs(bundle.docs, layer)
        document, _available, unavailable, info = build(docs)
        self.assertEqual(unavailable, ())
        self.assertEqual(info["notices"], ())
        served = render.rendered_selectors(document)
        self.assertEqual(sorted(set(FROZEN_2X_SELECTORS) - served), [])

        def aliases(kind: str, params) -> set[str]:
            return {
                model["name"]
                for entry in document["payload"][kind]
                if entry["params"] == params
                for model in entry["models"]
            }

        self.assertIn("claude-multi-qwen38-max", aliases("override", {"reasoning_effort": "xhigh"}))
        self.assertNotIn("claude-multi-qwen38-max", aliases("override", {"reasoning_effort": "max"}))
        self.assertIn("claude-multi-kimi-k3", aliases("override", {"output_config.effort": "max"}))
        self.assertIn("claude-multi-kimi-k3", aliases("filter", ["thinking"]))


class ReferenceViolationTests(unittest.TestCase):
    def test_unknown_provider_reference(self) -> None:
        errors = _mutate(
            lambda raw: raw["docs"]["models"]["models"]["sol"].update(provider="nope")
        )
        self.assertTrue(any("unknown provider" in error for error in errors))

    def test_unknown_role_reference(self) -> None:
        errors = _mutate(
            lambda raw: raw["docs"]["models"]["models"]["sol"].update(roles=["cm-nope"])
        )
        self.assertTrue(any("unknown role" in error for error in errors))

    def test_default_effort_not_in_efforts(self) -> None:
        errors = _mutate(
            lambda raw: raw["docs"]["models"]["models"]["sol"].update(default_effort="turbo")
        )
        self.assertTrue(any("default_effort" in error for error in errors), errors)

    @staticmethod
    def _add_effort(raw, level: str) -> None:
        raw["docs"]["models"]["models"]["sol"]["efforts"][level] = {
            "selector": f"gpt-multi-sol-{level}[1m]",
            "proxy_contract": "reasoning-effort-high",
        }

    def test_lane_effort_outside_vocabulary(self) -> None:
        errors = _mutate(lambda raw: self._add_effort(raw, "ludicrous"))
        self.assertTrue(any("agent efforts" in error for error in errors))

    def test_lane_agent_ultracode_rejected(self) -> None:
        # Ultracode is a lead-only effort (the pinned client drops
        # agent ultracode silently); the validator names the
        # agent effort list.
        errors = _mutate(lambda raw: self._add_effort(raw, "ultracode"))
        self.assertTrue(
            any("'ultracode' is outside the trusted agent efforts" in error for error in errors),
            errors,
        )

    def test_lead_effort_low_accepted(self) -> None:
        def mutate(raw):
            leads = [
                model
                for _, model in sorted(raw["docs"]["models"]["models"].items())
                if model["lead"] is not None
            ]
            self.assertTrue(leads)
            leads[0]["lead"]["effort"] = "low"

        self.assertEqual(_mutate(mutate), [])

    def test_lead_effort_outside_lead_efforts_rejected(self) -> None:
        def mutate(raw):
            for _, model in sorted(raw["docs"]["models"]["models"].items()):
                if model["lead"] is not None:
                    model["lead"]["effort"] = "ludicrous"
                    return

        errors = _mutate(mutate)
        self.assertTrue(any("lead efforts" in error for error in errors), errors)

    def test_packaged_effort_contract_semantics_checked(self) -> None:
        def mutate(raw):
            raw["docs"]["native-contract"]["lead_efforts"]["values"].remove("max")

        errors = _mutate(mutate)
        self.assertTrue(
            any("must contain every agent effort" in error for error in errors), errors
        )

    def test_lane_contract_undeclared_by_provider(self) -> None:
        errors = _mutate(
            lambda raw: raw["docs"]["models"]["models"]["sol"]["efforts"]["high"].update(
                proxy_contract="output-config-max"
            )
        )
        self.assertTrue(any("proxy effort contract" in error for error in errors))

    def test_duplicate_lane_selector_conflict(self) -> None:
        def mutate(raw):
            raw["docs"]["models"]["models"]["gpt55"]["efforts"]["high"][
                "selector"
            ] = "gpt-multi-sol-high[1m]"

        errors = _mutate(mutate)
        self.assertTrue(any("duplicates" in error for error in errors))

    def _route_schema_refusal(self, mutate) -> None:
        # The canonical route shape and fork:true are schema rules
        # (providers.schema.json), so they fail at load_raw on a copied tree.
        root = _copy_tree(self)
        path = root / "catalog" / "providers.json"
        document = json.loads(path.read_text())
        mutate(document["providers"]["anthropic"]["passthrough_routes"])
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(CatalogError, "passthrough_routes"):
            catalog.load_raw(root)

    def test_fork_rejected_on_noncanonical_route(self) -> None:
        self._route_schema_refusal(
            lambda routes: routes.append({"name": "claude-multi-fable-5", "fork": True})
        )

    def test_passthrough_without_fork_rejected(self) -> None:
        def mutate(routes):
            routes[0]["fork"] = False

        self._route_schema_refusal(mutate)

    def test_routes_only_on_the_anthropic_pool(self) -> None:
        errors = _mutate(
            lambda raw: raw["docs"]["providers"]["providers"]["kimi"][
                "passthrough_routes"
            ].append({"name": "claude-kimi-3", "fork": True})
        )
        self.assertTrue(
            any("passthrough routes are only allowed on the Anthropic OAuth pool" in e for e in errors),
            errors,
        )

    def test_selector_riding_own_route_must_name_its_wire(self) -> None:
        def mutate(raw):
            raw["docs"]["models"]["models"]["opus55"]["selector"] = "claude-opus-4-8[1m]"

        errors = _mutate(mutate)
        self.assertTrue(
            any("selector rides route claude-opus-4-8 but names wire claude-opus-5-5" in e for e in errors),
            errors,
        )

    def test_secret_like_value_rejected(self) -> None:
        errors = _mutate(
            lambda raw: raw["docs"]["models"]["models"]["sol"].update(
                routing_note="temporary note sk-live123456789"
            )
        )
        self.assertTrue(any("secret-like value" in error for error in errors))

    def test_env_secret_reference_allowed(self) -> None:
        self.assertEqual(
            catalog.validate_catalog(_raw()),
            [],
            "env: secret references must remain acceptable",
        )

    def test_prompt_traversal_rejected(self) -> None:
        root = _copy_tree(self)
        roles_path = root / "catalog" / "roles.json"
        roles_doc = json.loads(roles_path.read_text())
        roles_doc["roles"]["cm-analyst"]["prompt_file"] = "../escape.md"
        roles_path.write_text(json.dumps(roles_doc))
        with self.assertRaises(CatalogError):
            catalog.load_raw(root)

    def test_prompt_symlink_escape_rejected(self) -> None:
        root = _copy_tree(self)
        prompt = root / "catalog" / "prompts" / "cm-analyst.md"
        prompt.unlink()
        prompt.symlink_to(Path("/etc/hostname"))
        with self.assertRaises(CatalogError):
            catalog.load_raw(root)

    def test_prompt_symlink_inside_catalog_rejected(self) -> None:
        root = _copy_tree(self)
        prompt = root / "catalog" / "prompts" / "cm-analyst.md"
        prompt.unlink()
        prompt.symlink_to(root / "catalog" / "prompts" / "cm-lead.md")
        with self.assertRaises(CatalogError):
            catalog.load_raw(root)

    def test_render_sentinel_prefix_is_reserved(self) -> None:
        for target in ("model", "lane", "route"):
            with self.subTest(target=target):
                raw = _raw(FIXTURE_ROOT)
                models = raw["docs"]["models"]["models"]
                reserved = "claude-multi-render-deadbeef"
                if target == "model":
                    # a client-effort line's selector
                    models["qwen-flash-next"]["selector"] = reserved
                elif target == "lane":
                    # a gateway-effort line's per-effort selector
                    models["sol"]["efforts"]["high"]["selector"] = reserved + "[1m]"
                else:
                    raw["docs"]["providers"]["providers"]["anthropic"][
                        "passthrough_routes"
                    ][0]["name"] = reserved
                self.assertTrue(any(
                    "reserved render sentinel prefix" in error
                    for error in catalog.validate_catalog(raw)
                ))

    def test_lane_selector_riding_cross_provider_route(self) -> None:
        def mutate(raw):
            raw["docs"]["models"]["models"]["kimi-k3"]["efforts"]["max"][
                "selector"
            ] = "claude-opus-4-8[1m]"

        errors = _mutate(mutate)
        self.assertTrue(
            any("rides another provider's passthrough route" in error for error in errors),
            f"errors: {errors}",
        )

    def test_unverified_settings_key_rejected(self) -> None:
        errors = _mutate(
            lambda raw: raw["docs"]["settings"].update(worktree={"baseRef": "head"})
        )
        self.assertTrue(any("worktree" in error for error in errors))


# The compiler-owned keys the v2 compile moves into flag settings
# plus the policy-defeating keys it unsets.
LEAD_ENV_KEYS_RESERVED_BY_THE_COMPILER = (
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "DISABLE_AUTO_COMPACT",
    "DISABLE_COMPACT",
    "MAX_THINKING_TOKENS",
    "CLAUDE_CODE_DISABLE_THINKING",
    "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
    "CLAUDE_CODE_COORDINATOR_FORCE_WORKER_INHERIT_MODEL",
)


class ReservedLeadEnvKeyTests(unittest.TestCase):
    def test_compiler_keys_are_reserved(self) -> None:
        self.assertLessEqual(
            frozenset(LEAD_ENV_KEYS_RESERVED_BY_THE_COMPILER), catalog.RESERVED_LEAD_ENV_KEYS
        )
        # The credential and legacy members stay reserved.
        self.assertLessEqual(
            frozenset(catalog.CREDENTIAL_ENV_KEYS), catalog.RESERVED_LEAD_ENV_KEYS
        )


class CatalogMutationMatrixTests(unittest.TestCase):
    def test_enforced_checks(self) -> None:
        cases: list[tuple[str, object, str]] = []

        def case(name: str, mutator, needle: str) -> None:
            cases.append((name, mutator, needle))

        case(
            "empty prompt body",
            lambda raw: raw["prompt_bodies"].__setitem__("cm-analyst", b""),
            "prompt body is empty",
        )
        case(
            "whitespace-only prompt body",
            lambda raw: raw["prompt_bodies"].__setitem__("cm-analyst", b"  \n"),
            "prompt body is empty",
        )
        case(
            "duplicate prompt bodies across roles",
            lambda raw: raw["prompt_bodies"].__setitem__(
                "cm-reviewer", raw["prompt_bodies"]["cm-analyst"]
            ),
            "prompt body duplicates",
        )
        case(
            "prompt body differs within a function",
            lambda raw: raw["prompt_bodies"].__setitem__("cm-analyst-strong", b"other\n"),
            "prompt body differs from 'cm-analyst'",
        )
        case(
            "missing version.json launcher_version",
            lambda raw: raw["docs"]["version"].pop("launcher_version"),
            "missing 'launcher_version'",
        )
        case(
            "missing version.json catalog_version",
            lambda raw: raw["docs"]["version"].pop("catalog_version"),
            "missing 'catalog_version'",
        )
        case(
            "non CLAUDE_CODE_ lead env key",
            lambda raw: raw["docs"]["models"]["models"]["kimi-k3"]["lead"]["env"].__setitem__(
                "HOME", "1"
            ),
            "unexpected variable",
        )
        case(
            "duplicate passthrough route names",
            lambda raw: raw["docs"]["providers"]["providers"]["anthropic"][
                "passthrough_routes"
            ].append({"name": "claude-fable-5", "fork": True}),
            "duplicate passthrough route",
        )
        case(
            "client-effort line with an efforts map",
            lambda raw: raw["docs"]["models"]["models"]["fable"].__setitem__(
                "efforts", {"max": {"selector": "claude-fable-5[1m]", "proxy_contract": "x"}}
            ),
            "declares an efforts list, not a map",
        )
        case(
            "gateway-effort line with a line selector",
            lambda raw: raw["docs"]["models"]["models"]["sol"].__setitem__(
                "selector", "gpt-multi-sol-high[1m]"
            ),
            "never a line selector",
        )
        case(
            "settings version key forbidden",
            lambda raw: raw["docs"]["settings"].__setitem__("version", 1),
            "must not contain",
        )
        for reserved in (
            *catalog.CREDENTIAL_ENV_KEYS,
            "CLAUDE_CODE_SUBAGENT_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
            "CLAUDE_CODE_DISABLE_WORKFLOWS",
            *LEAD_ENV_KEYS_RESERVED_BY_THE_COMPILER,
        ):
            case(
                f"reserved lead env key {reserved}",
                lambda raw, key=reserved: raw["docs"]["models"]["models"]["kimi-k3"][
                    "lead"
                ]["env"].__setitem__(key, "1"),
                "compiler-owned and reserved",
            )
        case(
            "zero lead env value",
            lambda raw: raw["docs"]["models"]["models"]["kimi-k3"]["lead"]["env"].__setitem__(
                "CLAUDE_CODE_EXAMPLE_LIMIT", "0"
            ),
            "must be a positive integer",
        )
        case(
            "provider context exceeds client",
            lambda raw: raw["docs"]["models"]["models"]["sol"]["context"].__setitem__(
                "provider_tokens", 2000000
            ),
            "exceeds client_tokens",
        )
        case(
            "scalar context exceeds provider",
            lambda raw: raw["docs"]["models"]["models"]["sol"]["context"].__setitem__(
                "scalar_tokens", 2000000
            ),
            "exceeds provider_tokens",
        )
        case(
            "validated context exceeds declaration",
            lambda raw: raw["docs"]["models"]["models"]["sol"]["context"].__setitem__(
                "validated_tokens", 2000000
            ),
            "exceeds declared_tokens",
        )
        case(
            "unvalidated provider context lacks matching attestation",
            lambda raw: raw["docs"]["models"]["models"]["kimi-k3"]["context"].__setitem__(
                "user_reported_tokens", 900000
            ),
            "must match user_reported_tokens",
        )
        case(
            "selector and client context disagree",
            lambda raw: raw["docs"]["models"]["models"]["sol"]["context"].__setitem__(
                "client_tokens", 500000
            ),
            "selector classification",
        )
        case(
            "lead missing ordinary profile",
            lambda raw: raw["docs"]["models"]["models"]["sol"]["context"].__setitem__(
                "ordinary_profile", None
            ),
            "lead-capable models must belong",
        )
        # Routes are data; the semantic rules are "every
        # Anthropic line wire is a route" and "every client refusal-fallback
        # target is a route" (these mutations are schema-valid, so they
        # surface from validate_catalog).
        case(
            "empty anthropic passthrough routes",
            lambda raw: raw["docs"]["providers"]["providers"]["anthropic"].__setitem__(
                "passthrough_routes", []
            ),
            "missing refusal-fallback route claude-opus-4-8",
        )
        case(
            "missing fable line route",
            lambda raw: raw["docs"]["providers"]["providers"]["anthropic"][
                "passthrough_routes"
            ].pop(0),
            "Anthropic line wire claude-fable-5 must be a passthrough route",
        )
        def drop_route(name):
            def mutate(raw):
                routes = raw["docs"]["providers"]["providers"]["anthropic"][
                    "passthrough_routes"
                ]
                routes[:] = [route for route in routes if route["name"] != name]

            return mutate

        case(
            "missing refusal-fallback opus route",
            drop_route("claude-opus-4-8"),
            "missing refusal-fallback route claude-opus-4-8",
        )
        # The Sonnet 5.5 cyber/frontier_llm target.
        case(
            "missing refusal-fallback sonnet route",
            drop_route("claude-sonnet-5"),
            "missing refusal-fallback route claude-sonnet-5",
        )
        for name, mutator, needle in cases:
            with self.subTest(case=name):
                errors = _mutate(mutator)
                if needle == "__NO_ERROR_EXPECTED__":
                    self.assertEqual(errors, [], f"{name}: unexpected errors {errors}")
                else:
                    self.assertTrue(
                        any(needle in error for error in errors),
                        f"{name}: expected {needle!r} in {errors}",
                    )

    def test_empty_routes_reports_both_refusal_targets_and_line_wires(self) -> None:
        errors = _mutate(
            lambda raw: raw["docs"]["providers"]["providers"]["anthropic"].__setitem__(
                "passthrough_routes", []
            )
        )
        for wire in catalog.REFUSAL_FALLBACK_WIRES:
            self.assertTrue(
                any(f"missing refusal-fallback route {wire}" in e for e in errors), errors
            )
        self.assertTrue(
            any("Anthropic line wire claude-fable-5 must be a passthrough route" in e for e in errors),
            errors,
        )


class CompositionViolationTests(unittest.TestCase):
    """The 2.x composition validator (``catalog.validate_composition``)
    and its tests are deleted (2.x presets convert via
    profile migrate); the composition schema stays."""

    @staticmethod
    def _composition() -> dict:
        import _v3

        return _v3.default_composition()

    def test_composition_schema_accepts_optional_workflows(self) -> None:
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "composition.schema.json")
        document = self._composition()
        self.assertEqual(validate.validate(document, schema, "$"), [])
        document["workflows"] = "off"
        self.assertEqual(validate.validate(document, schema, "$"), [])
        document["workflows"] = "sometimes"
        self.assertTrue(validate.validate(document, schema, "$"))


class BundleHashTests(unittest.TestCase):
    def test_bundle_hash_deterministic(self) -> None:
        first = catalog.load_catalog(FIXTURE_ROOT)
        second = catalog.load_catalog(FIXTURE_ROOT)
        self.assertEqual(first.bundle_sha256, second.bundle_sha256)
        self.assertTrue(first.bundle_sha256.startswith("sha256:"))

    def test_bundle_hash_matches_canonical_bytes(self) -> None:
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        expected = "sha256:" + strict_json.sha256_hex(
            strict_json.canonical_bytes(bundle.bundle)
        )
        self.assertEqual(bundle.bundle_sha256, expected)

    def test_bundle_hash_changes_with_content(self) -> None:
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        mutated = copy.deepcopy(bundle.bundle)
        mutated["models"]["models"]["sol"]["display"] = "changed"
        self.assertNotEqual(
            strict_json.bundle_digest(mutated), bundle.bundle_sha256
        )

    def test_bundle_records_prompt_identity_hashes(self) -> None:
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        for role_id, body in bundle.prompt_bodies.items():
            expected = "sha256:" + strict_json.sha256_hex(body)
            self.assertEqual(bundle.bundle["prompts"][role_id], expected)

    # Seed profiles ------------------------------------------------------
    def test_bundle_hash_covers_seed_profiles(self) -> None:
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        self.assertEqual(tuple(bundle.bundle["profiles"]), catalog.SEED_PROFILE_NAMES)
        self.assertEqual(bundle.bundle["profiles"], bundle.seed_profiles)
        mutated = copy.deepcopy(bundle.bundle)
        mutated["profiles"]["balanced"]["description"] = "changed"
        self.assertNotEqual(strict_json.bundle_digest(mutated), bundle.bundle_sha256)

    def test_bundle_roles_are_raw_v2(self) -> None:
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        self.assertEqual(bundle.bundle["roles"], bundle.docs["roles-v2"])
        self.assertEqual(set(bundle.bundle["roles"]["roles"]), set(catalog.ROLE_IDS))
        self.assertEqual(bundle.bundle["roles"]["version"], catalog.ROLES_DATA_VERSION)

    def test_user_bindings_and_profiles_never_change_the_bundle(self) -> None:
        before = catalog.load_catalog(FIXTURE_ROOT).bundle_sha256
        config = Path(tempfile.mkdtemp(prefix="claude-multi-config-"))
        self.addCleanup(lambda: shutil.rmtree(config, ignore_errors=True))
        root = config / "claude-multi"
        (root / "profiles").mkdir(parents=True)
        (root / "bindings.json").write_bytes(
            strict_json.pretty_file_bytes(
                {"bindings": {"frontier": {"effort": "xhigh", "model": "opus55"}}, "version": 1}
            )
        )
        user = copy.deepcopy(catalog.load_raw(FIXTURE_ROOT)["docs"]["profiles/balanced"])
        user["description"] = "operator edit"
        (root / "profiles" / "balanced.json").write_bytes(strict_json.pretty_file_bytes(user))
        with unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}):
            after = catalog.load_catalog(FIXTURE_ROOT).bundle_sha256
        self.assertEqual(after, before)


def _write_seed(root: Path, name: str, mutate) -> None:
    path = root / "catalog" / "profiles" / f"{name}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_bytes(strict_json.pretty_file_bytes(document))


class SeedProfileLoaderTests(unittest.TestCase):
    """Seeds are required documents; unknown stems and v1 refuse."""

    def test_fixture_and_shipped_load_the_seven_seeds(self) -> None:
        for root in (FIXTURE_ROOT, SHIPPED_ROOT):
            with self.subTest(root=root.name):
                docs = catalog.load_raw(root)["docs"]
                self.assertEqual(
                    sorted(k for k in docs if k.startswith("profiles/")),
                    sorted(f"profiles/{n}" for n in catalog.SEED_PROFILE_NAMES),
                )

    def test_missing_seed_names_the_path(self) -> None:
        root = _copy_tree(self)
        (root / "catalog" / "profiles" / "quality.json").unlink()
        with self.assertRaisesRegex(CatalogError, r"^catalog/profiles/quality\.json: "):
            catalog.load_raw(root)

    def test_extra_seed_file_is_refused(self) -> None:
        root = _copy_tree(self)
        shutil.copy(
            root / "catalog" / "profiles" / "balanced.json",
            root / "catalog" / "profiles" / "extra.json",
        )
        with self.assertRaises(CatalogError) as ctx:
            catalog.load_raw(root)
        self.assertEqual(
            str(ctx.exception),
            "catalog/profiles/extra.json: unknown seed profile (seeds are balanced, "
            "quality, max, economy, claude, openai, openrouter, direct)",
        )

    def test_v1_seed_gets_the_version_message(self) -> None:
        root = _copy_tree(self)
        _write_seed(root, "balanced", lambda d: d.update(version=1))
        with self.assertRaises(CatalogError) as ctx:
            catalog.load_raw(root)
        self.assertEqual(
            str(ctx.exception),
            "catalog/profiles/balanced.json: unsupported data version 1 (seed profiles are v2)",
        )

    def test_schema_violation_names_the_seed(self) -> None:
        root = _copy_tree(self)
        _write_seed(root, "max", lambda d: d.update(workflows="semi"))
        with self.assertRaisesRegex(CatalogError, r"^catalog/profiles/max\.json: \$\.workflows"):
            catalog.load_raw(root)


class SeedProfileRuleTests(unittest.TestCase):
    """Seed-profile validation on raw-doc mutations (exact messages), plus five
    robustness rows: an invalid catalog returns errors and never raises."""

    @staticmethod
    def _balanced(mutate):
        def mutator(raw):
            mutate(raw["docs"]["profiles/balanced"])

        return mutator

    def test_unmutated_fixture_and_shipped_are_clean(self) -> None:
        # The passing case of every R-S row.
        self.assertEqual(_mutate(lambda raw: None), [])
        self.assertEqual(_mutate(lambda raw: None, SHIPPED_ROOT), [])

    def test_seed_name_must_equal_stem(self) -> None:
        errors = _mutate(self._balanced(lambda d: d.update(name="other")))
        self.assertEqual(errors, ["profiles/balanced: name must equal the file stem"])

    def test_seed_marker_must_match_the_profile(self) -> None:
        errors = _mutate(self._balanced(lambda d: d.pop("seed")))
        self.assertEqual(
            errors, ['profiles/balanced.seed: must be {"id": "balanced", "version": <integer>}']
        )
        errors = _mutate(self._balanced(lambda d: d.update(seed={"id": "quality", "version": 1})))
        self.assertEqual(
            errors, ['profiles/balanced.seed: must be {"id": "balanced", "version": <integer>}']
        )

    def test_seed_no_named_bindings(self) -> None:
        errors = _mutate(
            self._balanced(lambda d: d["agents"].update({"cm-analyst": {"use": "x"}}))
        )
        self.assertEqual(errors, ["profiles/balanced: seeds must not use named bindings"])

    def test_seed_retired_unbound_notice_is_an_error(self) -> None:
        def mutate(d):
            d["agents"].pop("cm-analyst-strong")
            d["agents"]["cm-analyst"] = {"effort": "high", "model": "muse-spark"}

        self.assertEqual(
            _mutate(self._balanced(mutate)),
            [
                "profiles/balanced: analyst: 'muse-spark' was removed — unbound; "
                "seeds name live lines only"
            ],
        )

    def test_seed_retired_dependency_fails_closed(self) -> None:
        errors = _mutate(
            self._balanced(
                lambda d: d["agents"].update(
                    {"cm-analyst": {"effort": "high", "model": "muse-spark"}}
                )
            )
        )
        self.assertEqual(
            errors,
            [
                "profiles/balanced: agents.cm-analyst-strong: requires cm-analyst "
                "(cm-analyst was unbound: 'muse-spark' was removed)"
            ],
        )

    def test_seed_undeclared_effort(self) -> None:
        errors = _mutate(
            self._balanced(
                lambda d: d["agents"].update({"cm-analyst": {"effort": "medium", "model": "sol"}})
            )
        )
        self.assertEqual(
            errors,
            [
                "profiles/balanced: agents.cm-analyst.effort: 'medium' is not declared by "
                "'sol' (declared: high, xhigh)"
            ],
        )

    def test_seed_new_line_is_new_off(self) -> None:
        def mutate(raw):
            raw["docs"]["models"]["models"]["opus5"]["status"] = "new"

        errors = _mutate(mutate)
        self.assertIn(
            "profiles/quality: agents.cm-implementer-strong.model: model 'opus5' is "
            "New · Off (status new) until admitted",
            errors,
        )
        self.assertIn(
            "profiles/claude: agents.cm-explorer.model: model 'opus5' is New · Off "
            "(status new) until admitted",
            errors,
        )

    def test_seed_secret_scan(self) -> None:
        errors = _mutate(
            self._balanced(
                lambda d: d.update(description="sk-ant-api03-" + "a" * 40)
            )
        )
        self.assertEqual(
            errors,
            ["$.profiles/balanced.description: secret-like value is forbidden in trusted data"],
        )

    def test_invalid_catalogs_return_errors_and_never_raise(self) -> None:
        def unknown_provider(raw):
            raw["docs"]["models"]["models"]["sol"]["provider"] = "nope"

        def explorer_dropped(raw):
            raw["docs"]["roles"]["roles"].pop("cm-explorer")

        def lead_uses(raw):
            raw["docs"]["profiles/balanced"]["lead"] = {"use": "x"}

        def effortless_agent(raw):
            raw["docs"]["profiles/balanced"]["agents"]["cm-analyst"] = {"model": "sol"}

        cases = {
            unknown_provider: "profiles/balanced: agents.cm-explorer.model: line 'sol' "
            "names unknown provider 'nope'",
            explorer_dropped: "profiles/balanced: agents.cm-explorer: role 'cm-explorer' "
            "is not in the catalog roles",
            lead_uses: "profiles/balanced: seeds must not use named bindings",
            effortless_agent: 'profiles/balanced: agents.cm-analyst: a binding is '
            '{"model", "effort"} or {"use"}',
        }
        # The 2.x projection (and its drop-default variant) is gone.
        for mutate, needle in cases.items():
            with self.subTest(case=mutate.__name__):
                self.assertIn(needle, _mutate(mutate))

    def test_evaluation_crash_is_reported_not_raised(self) -> None:
        from claude_multi import profile

        with unittest.mock.patch.object(profile, "evaluate", side_effect=KeyError("boom")):
            errors = _mutate(lambda raw: None)
        self.assertIn(
            "profiles/balanced: not evaluated against an invalid catalog (KeyError('boom'))",
            errors,
        )


class SupportNoteTests(unittest.TestCase):
    def test_anthropic_transport_support_is_honest(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        anthropic = bundle.providers["anthropic"]
        self.assertEqual(anthropic["support"], "anthropic-supported")
        self.assertIn("not officially supported by Anthropic", anthropic["support_note"])
        for provider_id in ("kimi", "openai"):
            provider = bundle.providers[provider_id]
            self.assertEqual(provider["support"], "locally-validated-experimental")
            self.assertIn("not officially supported", provider["support_note"])

    def test_missing_support_note_rejected(self) -> None:
        root = _copy_tree(self)
        providers_path = root / "catalog" / "providers.json"
        document = json.loads(providers_path.read_text())
        del document["providers"]["kimi"]["support_note"]
        providers_path.write_text(json.dumps(document))
        with self.assertRaisesRegex(CatalogError, "support_note"):
            catalog.load_raw(root)


class ReviewSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = strict_json.load(FIXTURE_ROOT / "schemas" / "review.schema.json")
        validate.check_schema(cls.schema)

    def _review(self, path: str) -> dict:
        return {
            "version": 2,
            "draft": "d1",
            "draft_hash": "sha256:" + "1" * 64,
            "repo": {"path": "/repo", "revision": "deadbeef"},
            "files": [
                {
                    "path": path,
                    "pre_image_hash": None,
                    "post_image_hash": "sha256:" + "0" * 64,
                }
            ],
            "results": {
                "bundle_valid": True,
                "render_sha256": "sha256:" + "2" * 64,
                "unavailable_providers": [],
                "diff": "",
            },
            "created_at": "2026-07-21T00:00:00Z",
        }

    def test_results_shape_enforced(self) -> None:
        document = self._review("a")
        self.assertEqual(validate.validate(document, self.schema), [])
        document["results"]["secret"] = "x"
        self.assertTrue(validate.validate(document, self.schema))
        document = self._review("a")
        del document["results"]["diff"]
        self.assertTrue(validate.validate(document, self.schema))

    def test_accepted_paths(self) -> None:
        for path in (
            "a",
            "src/claude_multi/data/catalog/models.json",
            "x/y_z-1.2/q.md",
        ):
            with self.subTest(path=path):
                self.assertEqual(validate.validate(self._review(path), self.schema), [])

    def test_rejected_paths(self) -> None:
        for path in (
            "",
            "/abs",
            "a/../b",
            "a/./b",
            ".hidden/x",
            "a//b",
            "a/.x",
            "../x",
            "a/",
        ):
            with self.subTest(path=path):
                self.assertTrue(
                    validate.validate(self._review(path), self.schema),
                    f"path {path!r} must be rejected",
                )


class GatewayStaticSchemaTests(unittest.TestCase):
    def test_static_blob_typo_rejected(self) -> None:
        def mutate(raw):
            raw["docs"]["gateway"]["gateway"]["cliproxy_static"]["request-retry-x"] = 1

        raw = _raw(FIXTURE_ROOT)
        mutate(raw)
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "gateway.schema.json")
        problems = validate.validate(raw["docs"]["gateway"], schema, "$")
        self.assertTrue(any("unexpected key" in problem for problem in problems))

    def test_static_blob_seed_valid(self) -> None:
        raw = _raw()
        schema = strict_json.load(CATALOG_ROOT / "schemas" / "gateway.schema.json")
        self.assertEqual(validate.validate(raw["docs"]["gateway"], schema, "$"), [])


class NativeContractTests(unittest.TestCase):
    def test_record_is_offline_and_unverified_where_required(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        record = bundle.docs["native-contract"]
        self.assertEqual(record["version"], 2)
        self.assertEqual(pin.version(record), "2.1.286")
        self.assertNotIn("claude", record)  # path-free: no local install paths
        acceptance = record["acceptance"]
        # The real-client probe verdicts S1-S12 fold in as U11-U22 and S7c
        # (the refusal fallback) as U23.
        self.assertEqual(
            set(acceptance),
            {"U1", "U2", "U5", "U6", "U10", *(f"U{n}" for n in range(11, 24))},
        )
        for name, entry in acceptance.items():
            with self.subTest(acceptance=name):
                if name == "U13":
                    # S3 FAIL: truthful status, with the adopted fallback.
                    self.assertEqual(entry["status"], "failed")
                elif name in ("U1", "U2", "U6"):
                    self.assertEqual(entry["status"], "unverified")
                else:
                    # The fence (U5) and SubagentStop (U10) are
                    # probe-verified; U11-U23 are the real-client probe verdicts.
                    self.assertEqual(entry["status"], "verified")
                if entry["status"] == "failed":
                    self.assertIn("fallback", entry["note"])
                if name in {f"U{n}" for n in range(11, 24)}:
                    self.assertIn("client 2.1.286", entry["note"])
                    self.assertIn("Radar: tests/test_client_", entry["note"])
        # The same-launch delivery record is deleted: contingency is the only
        # lead delivery, so no delivery record remains to drift.
        self.assertNotIn("lead_delivery", record)

    def test_promoted_executable_facts_locked(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        entry = bundle.docs["native-contract"]["verified"][0]
        self.assertEqual(entry["version"], "2.1.286")
        self.assertEqual(
            entry["platforms"]["linux-x64"]["sha256"],
            "fe503f65c6289d59c23e5b21ae44f03583f997dd33a2cbfc75ab4f96fb8fc73f",
        )
        self.assertEqual(entry["platforms"]["linux-x64"]["size"], 241667256)
        self.assertEqual(entry["key_fingerprint"], "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE")
        self.assertEqual(entry["evidence"]["linux-x64"], "battery")
        self.assertEqual(entry["verified_at"], "2026-10-01")
        self.assertEqual(bundle.docs["native-contract"]["recorded_at"], "2026-09-25")
        self.assertEqual(bundle.docs["native-contract"]["evidence_version"], 3)
        # Every listed platform has an evidence class (battery or identity+smoke).
        for platform in entry["platforms"]:
            with self.subTest(platform=platform):
                self.assertIn(pin.evidence_class(bundle.docs["native-contract"], platform),
                              pin.EVIDENCE_CLASSES)

    def test_the_packaged_pin_records_its_settings_keys(self) -> None:
        # A releasable contract carries the settings keys of its build: the
        # launch's settings-skew safeguard is off without them.
        from claude_multi import upgrade

        bundle = catalog.load_catalog(CATALOG_ROOT)
        keys = pin.settings_keys(bundle.docs["native-contract"])
        self.assertIsNotNone(keys)
        self.assertLessEqual(upgrade.SETTINGS_KEYS_REQUIRED, keys)

    def test_models_minimum_tested_floor_locked_separately(self) -> None:
        # No provider/model requests are allowed in this automation, so model
        # compatibility at the inspected 2.1.217 binary is untested; 2.1.216
        # remains the truthful minimum-tested floor, decoupled from the native
        # artifact version.
        bundle = catalog.load_catalog(CATALOG_ROOT)
        validated = pin.version(bundle.docs["native-contract"])
        self.assertEqual(validated, "2.1.286")
        floors = {"opus": "2.1.280", "fable": "2.1.257", "sonnet": "2.1.286"}
        # The OpenRouter lines record the pin their evidence ran on.
        floors.update({key: "2.1.286" for key, line in bundle.lines.items()
                       if line["provider"] in catalog.AGGREGATOR_PROVIDERS})
        # A model outside the locked floors map sits exactly
        # at the 2.1.216 baseline (a raised floor must be locked deliberately
        # in `floors`, AGENTS §5); a model added at the baseline stays green.
        for model_id, model in bundle.lines.items():
            with self.subTest(model=model_id):
                tested = model["minimum_tested"]["claude_code"]
                self.assertEqual(tested, floors.get(model_id, "2.1.216"))
                self.assertLessEqual(_version_key(tested), _version_key(validated))

    def test_generic_agent_aliases_recorded_for_policy(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        aliases = bundle.docs["native-contract"]["generic_agent_aliases"]
        self.assertEqual(aliases["values"], ["claude"])
        self.assertEqual(aliases["status"], "provisionally-trusted")
        self.assertIn("not functional verification", aliases["evidence"])

    def test_capabilities_all_pending(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        capabilities = bundle.docs["native-contract"]["capabilities"]
        self.assertEqual(
            set(capabilities),
            {
                "root_agent_discovery",
                "persistent_denies",
                "onboarding_trust",
                "transcript_location_exact_resume",
                "selectors_effort_tools_worktree",
                "background_supervisor_lifecycle",
                "root_memory_reinjection",
                "project_agent_collision",
            },
        )
        for name, entry in capabilities.items():
            with self.subTest(capability=name):
                self.assertEqual(entry["status"], "pending")

    def test_nested_subagent_decision_pending(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        nested = bundle.docs["native-contract"]["nested_subagents"]
        self.assertEqual(nested["decision"], "pending")
        self.assertEqual(
            nested["depth_key"],
            {"name": "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", "status": "unverified"},
        )
        self.assertEqual(
            nested["concurrency_key"],
            {"name": "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", "status": "unverified"},
        )
        self.assertIn("not functional verification", nested["evidence"])

    def test_lifecycle_evidence_identifier_matches_inspected_version(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        record = bundle.docs["native-contract"]
        lifecycle = record["lifecycle_evidence"]
        self.assertEqual(lifecycle["inspected_version"], pin.version(record))
        self.assertIn("phase0", lifecycle["evidence_id"])

    def test_effort_split_evidenced(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        record = bundle.docs["native-contract"]
        self.assertNotIn("effort_vocabulary", record)
        agent = record["agent_efforts"]
        lead = record["lead_efforts"]
        self.assertEqual(agent["values"], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(
            lead["values"], ["low", "medium", "high", "xhigh", "max", "ultracode"]
        )
        self.assertEqual(agent["status"], "verified")
        self.assertEqual(lead["status"], "provisionally-trusted")
        self.assertNotIn("ultracode", agent["values"])
        self.assertEqual(bundle.agent_efforts, tuple(agent["values"]))
        self.assertEqual(bundle.lead_efforts, tuple(lead["values"]))
        self.assertIn("Client check S8", agent["evidence"])


class NativeContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = strict_json.load(
            FIXTURE_ROOT / "schemas" / "native-contract.schema.json"
        )
        validate.check_schema(cls.schema)

    def _document(self, root: Path = FIXTURE_ROOT) -> dict:
        return copy.deepcopy(_raw(root)["docs"]["native-contract"])

    def test_seed_document_valid(self) -> None:
        self.assertEqual(
            validate.validate(self._document(CATALOG_ROOT), self.schema, "$"), []
        )

    def test_unknown_top_level_key_rejected(self) -> None:
        document = self._document()
        document["surprise"] = 1
        problems = validate.validate(document, self.schema, "$")
        self.assertTrue(any("unexpected key" in problem for problem in problems))

    def test_missing_new_required_fields_rejected(self) -> None:
        for field in (
            "agent_efforts",
            "lead_efforts",
            "generic_agent_aliases",
            "capabilities",
            "nested_subagents",
            "acceptance",
            "lifecycle_evidence",
        ):
            with self.subTest(field=field):
                document = self._document()
                del document[field]
                self.assertTrue(validate.validate(document, self.schema, "$"))

    def test_agent_ultracode_rejected_by_schema(self) -> None:
        document = self._document()
        document["agent_efforts"]["values"].append("ultracode")
        self.assertTrue(validate.validate(document, self.schema, "$"))

    def test_lead_effort_outside_enum_rejected_by_schema(self) -> None:
        document = self._document()
        document["lead_efforts"]["values"].append("ludicrous")
        self.assertTrue(validate.validate(document, self.schema, "$"))

    def test_legacy_effort_vocabulary_rejected_by_schema(self) -> None:
        # A 2.x override shape (legacy schema): the old key is unknown and the split
        # keys are missing.
        document = self._document()
        document["effort_vocabulary"] = document.pop("lead_efforts")
        del document["agent_efforts"]
        problems = validate.validate(document, self.schema, "$")
        self.assertTrue(any("unexpected key" in problem for problem in problems))
        self.assertTrue(catalog.is_legacy_contract_override(document))
        self.assertFalse(catalog.is_legacy_contract_override(self._document()))

    def test_effort_contract_semantics(self) -> None:
        document = self._document()
        self.assertEqual(catalog._check_effort_contract(document, "$"), [])
        missing_agent = copy.deepcopy(document)
        missing_agent["lead_efforts"]["values"].remove("low")
        self.assertTrue(
            any(
                "must contain every agent effort" in error
                for error in catalog._check_effort_contract(missing_agent, "$")
            )
        )
        # Schema-valid, but a lead list without ultracode is refused too.
        no_ultracode = copy.deepcopy(document)
        no_ultracode["lead_efforts"]["values"].remove("ultracode")
        self.assertEqual(validate.validate(no_ultracode, self.schema, "$"), [])
        self.assertTrue(
            any(
                "'ultracode'" in error
                for error in catalog._check_effort_contract(no_ultracode, "$")
            )
        )

    def test_capability_entry_is_per_entry_closed(self) -> None:
        document = self._document()
        document["capabilities"]["onboarding_trust"]["probe"] = "g1-p3"
        problems = validate.validate(document, self.schema, "$")
        self.assertTrue(any("unexpected key" in problem for problem in problems))

    def test_capability_status_outside_enum_rejected(self) -> None:
        document = self._document()
        document["capabilities"]["onboarding_trust"]["status"] = "passed"
        self.assertTrue(validate.validate(document, self.schema, "$"))

    def test_acceptance_entry_shape_enforced(self) -> None:
        document = self._document()
        document["acceptance"]["U1"]["status"] = "passed"
        self.assertTrue(validate.validate(document, self.schema, "$"))
        document = self._document()
        document["acceptance"]["U1"]["probe"] = "phase2"
        problems = validate.validate(document, self.schema, "$")
        self.assertTrue(any("unexpected key" in problem for problem in problems))

    def test_nested_decision_outside_enum_rejected(self) -> None:
        document = self._document()
        document["nested_subagents"]["decision"] = "assumed"
        self.assertTrue(validate.validate(document, self.schema, "$"))

    def test_nested_key_name_is_const_locked(self) -> None:
        document = self._document()
        document["nested_subagents"]["depth_key"]["name"] = "CLAUDE_CODE_OTHER"
        self.assertTrue(validate.validate(document, self.schema, "$"))

    def test_verified_entry_is_closed_and_path_free(self) -> None:
        document = self._document()
        entry = document["verified"][0]
        for mutate, where in (
            (lambda d: d["verified"][0].__setitem__("resolved_path", "/x"), "unexpected key"),
            (lambda d: d["verified"][0]["platforms"].__setitem__("plan9-x64", {"sha256": "a" * 64, "size": 1}),
             "unexpected key"),
            (lambda d: d["verified"][0]["platforms"]["linux-x64"].__setitem__("size", 0), "minimum"),
            (lambda d: d["verified"][0]["platforms"]["linux-x64"].__setitem__("sha256", "A" * 64), "pattern"),
            (lambda d: d["verified"][0]["evidence"].__setitem__("linux-x64", "vibes"), ""),
            (lambda d: d["verified"].append(copy.deepcopy(entry)), ""),
            (lambda d: d.__setitem__("verified", []), ""),
            (lambda d: d.__setitem__("version", 1), ""),
        ):
            with self.subTest(where=where):
                candidate = copy.deepcopy(document)
                mutate(candidate)
                problems = validate.validate(candidate, self.schema, "$")
                self.assertTrue(problems)
                self.assertTrue(any(where in problem for problem in problems), problems)
        unsigned = copy.deepcopy(document)
        unsigned["verified"][0]["signature_sha256"] = None
        self.assertEqual(validate.validate(unsigned, self.schema, "$"), [])
        empty = copy.deepcopy(document)
        empty["verified"][0]["platforms"] = {}
        self.assertEqual(validate.validate(empty, self.schema, "$"), [])
        self.assertTrue(catalog._check_verified_pins(empty, "$"))

    def test_v1_packaged_contract_is_refused(self) -> None:
        root = _copy_tree(self, FIXTURE_ROOT)
        path = root / "catalog" / "native-contract.json"
        document = json.loads(path.read_bytes())
        document["version"] = 1
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(catalog.CatalogError, "unsupported data version 1 .*native contract v2"):
            catalog.load_raw(root)

    def test_lifecycle_evidence_shape_enforced(self) -> None:
        document = self._document()
        document["lifecycle_evidence"]["surprise"] = "x"
        problems = validate.validate(document, self.schema, "$")
        self.assertTrue(any("unexpected key" in problem for problem in problems))
        document = self._document()
        document["lifecycle_evidence"]["inspected_version"] = "2.1"
        self.assertTrue(validate.validate(document, self.schema, "$"))




class ContractOverrideTests(unittest.TestCase):
    """An operator contract override is never applied; the old-format file
    is classified against the packaged pin (doctor reports it)."""

    def _override_file(self, case_root: Path, document) -> Path:
        from claude_multi import state as state_mod

        target = case_root / "config" / "native-contract.json"
        state_mod.ensure_private_dir(target.parent)
        state_mod.atomic_write(target, strict_json.canonical_file_bytes(document))
        return target

    def _v1_doc(self, version: str) -> dict:
        document = copy.deepcopy(_raw()["docs"]["native-contract"])
        del document["verified"]
        document["version"] = 1
        document["claude"] = {"validated_version": version, "executable": {
            "configured_path": "/home/user/.local/bin/claude",
            "resolved_path": f"/home/user/.local/share/claude/versions/{version}",
            "sha256": "a" * 64, "inspection": "fixture", "inspected_at": "2026-01-01"}}
        return document

    def test_newer_old_format_override_is_never_applied(self) -> None:
        root = _copy_tree(self, CATALOG_ROOT)
        override = self._override_file(root, self._v1_doc("2.1.300"))
        bundle = catalog.load_catalog(root, contract_override=override)
        self.assertEqual(pin.version(bundle.docs["native-contract"]), "2.1.286")
        self.assertEqual(bundle.contract_source, "override-ignored-newer")
        self.assertEqual(bundle.contract_override_detail, "2.1.300")
        packaged = catalog.load_catalog(root)
        self.assertEqual(bundle.bundle_sha256, packaged.bundle_sha256)
        self.assertEqual(packaged.contract_source, "packaged")

    def test_stale_old_format_override_is_ignored(self) -> None:
        root = _copy_tree(self, CATALOG_ROOT)
        for version in ("2.1.100", "2.1.286"):
            with self.subTest(version=version):
                override = self._override_file(root, self._v1_doc(version))
                bundle = catalog.load_catalog(root, contract_override=override)
                self.assertEqual(bundle.contract_source, "override-ignored-stale")
                self.assertEqual(bundle.contract_override_detail, version)
                self.assertEqual(pin.version(bundle.docs["native-contract"]), "2.1.286")

    def test_other_overrides_are_ignored_as_unusable(self) -> None:
        root = _copy_tree(self, CATALOG_ROOT)
        for document in ({"claude": {"bogus": True}}, _raw()["docs"]["native-contract"], []):
            with self.subTest(document=type(document).__name__):
                target = self._override_file(root, document)
                bundle = catalog.load_catalog(root, contract_override=target)
                self.assertEqual(bundle.contract_source, "override-ignored-invalid")
                self.assertEqual(pin.version(bundle.docs["native-contract"]), "2.1.286")

    def test_legacy_override_still_raises_for_its_removal(self) -> None:
        root = _copy_tree(self, CATALOG_ROOT)
        document = self._v1_doc("2.1.300")
        document["effort_vocabulary"] = document.pop("lead_efforts")
        target = self._override_file(root, document)
        with self.assertRaisesRegex(catalog.CatalogError, "an older-format contract"):
            catalog.load_catalog(root, contract_override=target)


class GatewayManifestConsistencyTests(unittest.TestCase):
    """The machine-readable patch manifest matches the build's patches."""

    def test_manifest_covers_every_patch_the_recipe_applies(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        # Exact ordered lists, not sets: the build applies the recipe's
        # series in this order, and the retention patch's context hunks are
        # pinned against the sequentially patched source. Any reorder or
        # entry drift must fail loudly.
        manifest = list(bundle.docs["gateway"]["gateway"]["patches"])
        recipe = json.loads(UPSTREAM_JSON.read_text(encoding="utf-8"))
        applied = [entry["basename"] for entry in recipe["series"] if entry["admitted"]]
        self.assertTrue(applied, "no admitted patches in gateway/UPSTREAM.json?")
        self.assertEqual(manifest, applied)
        self.assertEqual(
            manifest,
            [
                "cli-proxy-api-loopback-oauth.patch",
                "cli-proxy-api-kimi-claude-compat.patch",
                "cli-proxy-api-non-claude-cache-retention.patch",
                "cli-proxy-api-management-readonly-allowlist.patch",
                "cli-proxy-api-watcher-parentdir.patch",
                "cli-proxy-api-serve-after-initial-auth-load.patch",
                "cli-proxy-api-management-env-only.patch",
                "cli-proxy-api-credential-save-report.patch",
                "cli-proxy-api-credentialed-redirects.patch",
                "cli-proxy-api-openai-compat-keyed-safety.patch",
                "cli-proxy-api-auth-snapshot-locking.patch",
                "cli-proxy-api-server-config-snapshot.patch",
                "cli-proxy-api-plugin-host-locking.patch",
                "cli-proxy-api-claude-metadata-locking.patch",
                "cli-proxy-api-oauth-model-overlay.patch",
                "cli-proxy-api-no-antigravity-egress.patch",
                "cli-proxy-api-codex-client-identity.patch",
                "cli-proxy-api-antigravity-loopback-callback.patch",
                "cli-proxy-api-codex-api-key-safety.patch",
                "cli-proxy-api-refresh-shutdown-join.patch",
                "cli-proxy-api-openai-content-chunks.patch",
            ],
        )

    def test_manifest_entries_exist_as_files(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        for name in bundle.docs["gateway"]["gateway"]["patches"]:
            with self.subTest(patch=name):
                self.assertTrue(
                    (PATCH_DIR / name).is_file(),
                    f"{name} in the manifest but not in gateway/patches/",
                )


class WireSlashPatternTests(unittest.TestCase):
    """wire_model accepts exactly one author/model slash segment
    (OpenRouter shape) and rejects more."""

    def _wire_problems(self, wire: str) -> list[str]:
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "models.schema.json")
        document = strict_json.load(FIXTURE_ROOT / "catalog" / "models.json")
        document["models"]["sol"]["wire_model"] = wire
        return validate.validate(document, schema, "$")

    def test_single_slash_segment_accepted(self) -> None:
        self.assertEqual(self._wire_problems("x-ai/grok-4.6"), [])

    def test_double_slash_rejected(self) -> None:
        problems = self._wire_problems("a/b/c")
        self.assertTrue(any("wire_model" in p for p in problems))

    def test_leading_slash_rejected(self) -> None:
        problems = self._wire_problems("/grok-4.6")
        self.assertTrue(any("wire_model" in p for p in problems))

    def test_duplicate_route_wire_rejected_same_provider_allowed_cross(self) -> None:
        # Same (provider, wire) twice is ambiguous; same wire across
        # DIFFERENT providers is the multi-route convention.
        def dup_same(raw):
            models = raw["docs"]["models"]["models"]
            import copy as _copy

            clone = _copy.deepcopy(models["grok46"])
            clone["efforts"]["high"]["selector"] = "claude-multi-grok46-high-alt"
            clone["efforts"]["xhigh"]["selector"] = "claude-multi-grok46-xhigh-alt"
            models["grok46-alt"] = clone

        errors = _mutate(dup_same)
        self.assertTrue(any("duplicate route wire" in e for e in errors))

        def same_wire_other_provider(raw):
            models = raw["docs"]["models"]["models"]
            providers = raw["docs"]["providers"]["providers"]
            import copy as _copy

            clone = _copy.deepcopy(models["grok46"])
            clone["provider"] = "deepseek"
            clone["wire_model"] = "x-ai/grok-4.6"  # same wire, different provider
            clone["efforts"]["high"]["selector"] = "claude-multi-grok46-high-ds"
            clone["efforts"]["high"]["proxy_contract"] = "output-config-high"
            clone["efforts"]["xhigh"]["selector"] = "claude-multi-grok46-xhigh-ds"
            clone["efforts"]["xhigh"]["proxy_contract"] = "output-config-max"
            clone["efforts"]["max"] = clone["efforts"].pop("xhigh")
            clone["default_effort"] = "high"
            clone["context"]["ordinary_profile"] = "grok"
            models["grok46-ds"] = clone

        errors = _mutate(same_wire_other_provider)
        self.assertFalse(any("duplicate route wire" in e for e in errors))

    def test_codex_route_context_bounds(self) -> None:
        # Catalog 33: the GPT-6 lines keep the 1M client class (S12); the
        # provider window and validated size come from the approved
        # 2026-09-26 acceptance probe. gpt55 is retired (its 2.x fence
        # survives only as the retired entry's context_tokens).
        bundle = catalog.load_catalog(CATALOG_ROOT)
        for key in ("luna",):
            with self.subTest(line=key):
                line = bundle.lines[key]
                self.assertEqual(line["wire_model"], f"gpt-6-{key}")
                context = line["context"]
                assert_gpt6_probe_context(self, context)
                # Never "operator-attested" for GPT-6 lines.
                self.assertNotIn("operator-attested", context["qualification"])
                self.assertIn("route acceptance verified 2026-09-25", context["qualification"])
                self.assertIn("0.155.0", context["qualification"])
        self.assertNotIn("gpt55", bundle.lines)
        gpt55 = bundle.retired["gpt55"]
        self.assertEqual(gpt55["context_tokens"], 258400)
        self.assertEqual(gpt55["successor"], "sol")
        # Continuity is kept past the upstream date.
        self.assertIn("2026-10-14", gpt55["reason"])
        self.assertEqual(gpt55["selectors"], {"gpt-multi-gpt55-high": "reasoning-effort-high"})


class DeepSeekProductionModelsTests(unittest.TestCase):
    """Stable aliases resolve the current production versions.

    2026-09-10 (V4.1-Flash): the canonical id is literally `deepseek-flash`;
    the retired `deepseek-v4-flash` string is a compatibility redirect and
    `deepseek-v4-pro` reroutes to V4.1-Flash from 2026-09-14 04:00 UTC.
    """

    def test_flash_uses_the_canonical_v41_id(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        flash = bundle.lines["deepseek-flash"]
        self.assertEqual(flash["wire_model"], "deepseek-flash")
        self.assertEqual(flash["display"], "DeepSeek V4.1 Flash")
        self.assertIn("V4.1-Flash", flash["routing_note"])
        self.assertIn("canonical id since 2026-09-10", flash["routing_note"])
        # Docs-only evidence keeps the conservative floor (the 1M context is
        # provider-documented, not measured); a docs claim is not a probe.
        self.assertEqual(flash["context"]["validated_tokens"], 200000)
        self.assertIn("conservative 200,000 docs floor", flash["context"]["qualification"])
        # Catalog 33 removed deepseek-pro: no stale sibling reference.
        self.assertNotIn("Pro sibling", flash["context"]["qualification"])
        # No dated callable id is documented by DeepSeek.
        self.assertNotIn("deepseek-flash-0910", json.dumps(flash))
        self.assertNotIn("deepseek-v4.1-flash", json.dumps(flash))

    def test_pro_is_retired_without_successor(self) -> None:
        # Since 2026-09-14 deepseek-v4-pro serves V4.1-Flash, which the
        # deepseek-flash line carries; the Pro selectors stay continuity.
        bundle = catalog.load_catalog(CATALOG_ROOT)
        self.assertNotIn("deepseek-pro", bundle.lines)
        pro = bundle.retired["deepseek-pro"]
        self.assertIsNone(pro["successor"])
        self.assertEqual(pro["last_wire"], "deepseek-v4-pro")
        self.assertEqual(pro["display"], "DeepSeek V4 Pro")
        self.assertIn("2026-09-14", pro["reason"])
        self.assertEqual(
            pro["selectors"],
            {
                "claude-multi-deepseek-pro-high[1m]": "output-config-high",
                "claude-multi-deepseek-pro-max[1m]": "output-config-max",
            },
        )
        self.assertIn(
            "named forced tool_choice returns 400",
            bundle.providers["deepseek"]["support_note"],
        )

    def test_deepseek_wires_and_selectors_are_unique(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        deepseek = {
            model_id: model
            for model_id, model in bundle.lines.items()
            if model["provider"] == "deepseek"
        }
        # Subset + uniqueness, not the exact provider inventory, so a
        # further well-formed DeepSeek model keeps this green.
        wires = [model["wire_model"] for model in deepseek.values()]
        self.assertLessEqual({"deepseek-flash"}, set(wires))
        self.assertEqual(len(wires), len(set(wires)))
        selectors = [
            selector
            for model in deepseek.values()
            for _level, selector, _contract in catalog.line_selectors(model)
        ]
        # One selector per declared effort (derived, never a literal count).
        self.assertEqual(
            len(selectors), sum(len(model["efforts"]) for model in deepseek.values())
        )
        self.assertEqual(len(selectors), len(set(selectors)))


class RetiredDirectLinesTests(unittest.TestCase):
    """Catalog 33: grok46, kimi-k3 and glm52/qwen38 leave the lines;
    their providers stay configured and their 2.x selectors stay continuity
    (the grok46 shape class is replaced by these retired-map pins)."""

    def test_grok46_retired_with_its_concrete_slug(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        self.assertNotIn("grok46", bundle.lines)
        self.assertNotIn("grok45", bundle.retired)  # pre-32, deliberately absent
        grok = bundle.retired["grok46"]
        self.assertEqual(grok["provider"], "openrouter")
        self.assertEqual(grok["last_wire"], "x-ai/grok-4.6")
        self.assertEqual(grok["context_tokens"], 500000)
        self.assertEqual(
            grok["selectors"],
            {
                "claude-multi-grok46-high": "output-config-high",
                "claude-multi-grok46-xhigh": "output-config-xhigh",
            },
        )

    def test_kimi_k3_retired_keeps_its_max_rule(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        kimi = bundle.retired["kimi-k3"]
        self.assertEqual(kimi["selectors"], {"claude-multi-kimi-k3[1m]": "output-config-max"})
        self.assertIn("kimi", bundle.providers)
        self.assertNotIn("kimi-k3", bundle.lines)
        # A current Kimi line never takes over the retired selector.
        current = {
            catalog._selector_base(selector)
            for line in bundle.lines.values() if line["provider"] == "kimi"
            for _level, selector, _contract in catalog.line_selectors(line)
        }
        self.assertNotIn("claude-multi-kimi-k3", current)

    def test_every_keyed_provider_ships_a_line_that_can_lead(self) -> None:
        # A key alone is a usable start: each shipped provider reached with an
        # API key carries an active lead-capable line (reviewed from its
        # vendor's documentation at least, with the conservative floor).
        bundle = catalog.load_catalog(CATALOG_ROOT)
        keyed = sorted(pid for pid, provider in bundle.providers.items()
                       if ((provider.get("transport") or {}).get("auth") or {}).get("secret_ref"))
        self.assertTrue(keyed)
        for pid in keyed:
            with self.subTest(provider=pid):
                leads = [line for line in bundle.lines.values()
                         if line["provider"] == pid and line["status"] == "active" and "lead" in line["capabilities"]]
                self.assertTrue(leads)
                for line in leads:
                    self.assertLessEqual(line["context"]["validated_tokens"], line["context"]["provider_tokens"])

    def test_openrouter_declares_high_and_xhigh_output_contracts(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        self.assertEqual(
            set(bundle.providers["openrouter"]["payload_contracts"]),
            {"output-config-high", "output-config-xhigh"},
        )


class AstraAndMuseModelTests(unittest.TestCase):
    """The gpt-6-astra line (codex route) and the muse-spark pair (Meta API)."""

    def test_astra_lead_capable_1m_shape(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        astra = bundle.lines["astra"]
        self.assertEqual(astra["provider"], "openai")
        self.assertEqual(astra["wire_model"], "gpt-6-astra")
        self.assertEqual(astra["display"], "GPT-6 Astra")
        # Lead-capable since catalog22 (route acceptance verified 2026-09-05).
        self.assertEqual(astra["capabilities"], ["lead", "agents"])
        self.assertEqual(astra["lead"], {"effort": "ultracode", "env": {}})
        self.assertEqual(astra["roles"], "all")
        self.assertEqual(astra["default_effort"], "high")
        # Catalog 34: GPT-6 efforts low..max (the
        # codex max contract exists since catalog 34).
        levels = ["low", "medium", "high", "xhigh", "max"]
        self.assertEqual(sorted(astra["efforts"]), sorted(levels))
        for level in levels:
            with self.subTest(level=level):
                self.assertEqual(
                    astra["efforts"][level],
                    {
                        "selector": f"gpt-multi-astra-{level}[1m]",
                        "proxy_contract": f"reasoning-effort-{level}",
                    },
                )
        context = astra["context"]
        self.assertEqual(context["declared_tokens"], 1050000)
        # Lead-capable since catalog22 -> joins the large ordinary profile;
        # The 2026-09-26 probe sets the provider window and the
        # validated size (relations only, never the numbers).
        assert_gpt6_probe_context(self, context)
        self.assertIn("Route acceptance verified 2026-09-05", context["qualification"])
        self.assertIn("272,000", context["qualification"])
        self.assertNotIn("qwen38", context["qualification"])

    def test_muse_pair_shape(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        muse = bundle.lines["muse"]
        contrib = bundle.lines["muse-contributor"]
        self.assertEqual(muse["wire_model"], "muse-spark-1.3")
        self.assertEqual(contrib["wire_model"], "muse-spark-1.3-contributor")
        levels = ["low", "medium", "high", "xhigh"]  # Meta documents no max
        for key, model in (("muse", muse), ("muse-contributor", contrib)):
            self.assertEqual(model["provider"], "meta")
            self.assertEqual(model["capabilities"], ["lead", "agents"])
            self.assertEqual(model["context"]["client_tokens"], 1000000)
            self.assertEqual(model["context"]["provider_tokens"], 1000000)
            self.assertEqual(model["context"]["declared_tokens"], 1048576)
            self.assertEqual(model["context"]["validated_tokens"], 200000)
            self.assertEqual(model["context"]["ordinary_profile"], "large")
            self.assertEqual(sorted(model["efforts"]), sorted(levels))
            for level in levels:
                self.assertEqual(
                    model["efforts"][level],
                    {
                        "selector": f"claude-multi-{key}-{level}[1m]",
                        "proxy_contract": f"output-config-{level}",
                    },
                )
        self.assertEqual(muse["default_effort"], "xhigh")
        self.assertEqual(contrib["default_effort"], "high")
        for old, new in (("muse-spark", "muse"), ("muse-spark-contributor", "muse-contributor")):
            self.assertEqual(bundle.retired[old]["successor"], new)
        # Contributor carries the privacy caveat in both routing surfaces.
        self.assertIn("improve Meta products", contrib["context"]["qualification"])
        self.assertIn("train Meta", contrib["routing_note"])
        self.assertIn("100 RPM", contrib["routing_note"])
        self.assertIn("muse tier", contrib["routing_note"])
        self.assertNotIn("muse-spark tier", contrib["routing_note"])

    def test_meta_provider_contract_and_bearer_transport(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        meta = bundle.providers["meta"]
        self.assertEqual(meta["adapter"], "cliproxy-claude-compatible-v1")
        self.assertEqual(meta["independence_family"], "meta")
        self.assertEqual(
            set(meta["payload_contracts"]),
            {
                "output-config-low",
                "output-config-medium",
                "output-config-high",
                "output-config-xhigh",
            },
        )
        self.assertEqual(meta["transport"]["base_url"], "https://api.meta.ai")
        self.assertEqual(meta["transport"]["auth"]["kind"], "bearer")
        self.assertEqual(
            meta["transport"]["auth"]["secret_ref"], "env:META_CLAUDE_API_KEY"
        )
        self.assertIn("NO max", meta["support_note"])
        self.assertIn("thinking always on", meta["support_note"])


def _retired_entry(**overrides) -> dict:
    entry = {
        "successor": None,
        "reason": "Test-local retired line.",
        "since_catalog": 31,
        "provider": "meta",
        "last_wire": "muse-test-1",
        "display": "Muse Test",
        "context_tokens": 1000000,
        "capabilities": ["agents"],
        "roles": ["cm-reviewer"],
        "selectors": {"claude-multi-muse-test-high[1m]": "output-config-high"},
    }
    entry.update(overrides)
    return entry


class CatalogV2Tests(unittest.TestCase):
    """Models v2, the retired map, New · Off (``docs["models"]`` is raw v2)."""

    def _write_models(self, root: Path, mutate) -> None:
        path = root / "catalog" / "models.json"
        document = json.loads(path.read_text())
        mutate(document)
        path.write_bytes(strict_json.pretty_file_bytes(document))

    # 2. docs aliases ----------------------------------
    def test_docs_carry_view_and_raw_v2(self) -> None:
        # The view is gone; docs["models"]/docs["roles"] are the raw
        # v2 documents and models-v2/roles-v2 alias the same objects.
        raw = _raw(FIXTURE_ROOT)
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        self.assertEqual(bundle.docs["models-v2"], raw["docs"]["models"])
        self.assertIs(bundle.lines, bundle.docs["models-v2"]["models"])
        self.assertIs(bundle.docs["models"], bundle.docs["models-v2"])
        self.assertIs(bundle.docs["roles"], bundle.docs["roles-v2"])
        self.assertFalse(hasattr(bundle, "models"))
        # The hash covers raw v2 + retired.
        self.assertEqual(bundle.bundle["models"], raw["docs"]["models"])
        self.assertEqual(bundle.bundle["retired"], raw["docs"]["retired"])

    def test_retired_edit_changes_bundle_hash(self) -> None:
        before = catalog.load_catalog(FIXTURE_ROOT).bundle_sha256
        root = _copy_tree(self)
        path = root / "catalog" / "retired.json"
        document = json.loads(path.read_text())
        document["retired"]["muse-spark"]["reason"] = "Changed reason."
        path.write_bytes(strict_json.pretty_file_bytes(document))
        self.assertNotEqual(catalog.load_catalog(root).bundle_sha256, before)

    # 3. versions -------------------------------------------------------
    def test_models_v1_document_gets_the_friendly_version_error(self) -> None:
        root = _copy_tree(self)
        self._write_models(root, lambda document: document.update(version=1))
        with self.assertRaisesRegex(CatalogError, "models v2"):
            catalog.load_raw(root)
        # A catalog-32 models file (v1 entries) also fails on the version,
        # before the schema's field noise.
        shutil.copy(FIXTURE_ROOT.parent / "catalog32-models.json", root / "catalog" / "models.json")
        with self.assertRaisesRegex(
            CatalogError,
            r"unsupported data version 1 \(this launcher reads models v2, catalog 33\+\)",
        ):
            catalog.load_raw(root)

    def test_retired_and_composition_documents_stay_version_1(self) -> None:
        self.assertEqual(catalog.SUPPORTED_DATA_VERSION, 1)
        self.assertEqual(catalog.MODELS_DATA_VERSION, 2)
        root = _copy_tree(self)
        path = root / "catalog" / "retired.json"
        document = json.loads(path.read_text())
        self.assertEqual(document["version"], 1)
        document["version"] = 2
        path.write_bytes(strict_json.pretty_file_bytes(document))
        with self.assertRaisesRegex(CatalogError, "unsupported data version 2"):
            catalog.load_raw(root)
        # The catalog carries no composition; the kept 2.x reader
        # still gates compositions on SUPPORTED_DATA_VERSION.
        import _v3
        from claude_multi import composition

        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "composition.schema.json")
        document = _v3.default_composition()
        self.assertEqual(document["version"], 1)
        document["version"] = 2
        with self.assertRaises(composition.CompositionError):
            composition.validate_document(document, schema)

    # 4. shape ---------------------------------------------------------
    def test_shape_rules(self) -> None:
        def lines(raw):
            return raw["docs"]["models"]["models"]

        cases = [
            ("client-effort map", lambda raw: lines(raw)["fable"].update(
                efforts={"max": {"selector": "claude-fable-5[1m]", "proxy_contract": "x"}}),
             "declares an efforts list"),
            ("gateway selector", lambda raw: lines(raw)["sol"].update(selector="gpt-multi-sol-high[1m]"),
             "never a line selector"),
            ("default not an effort", lambda raw: lines(raw)["fable"].update(default_effort="high"),
             "default_effort"),
            ("client ultracode", lambda raw: lines(raw)["fable"]["efforts"].append("ultracode"),
             "'ultracode' is outside the trusted agent efforts"),
            ("roles lists cm-lead", lambda raw: lines(raw)["sol"].update(roles=["cm-lead"]),
             "implied by the lead capability"),
            ("agents without roles", lambda raw: lines(raw)["sol"].update(roles=[]),
             "agents capability requires at least one role"),
            ("lead-only null lead", lambda raw: lines(raw)["qwen-flash-next"].update(lead=None),
             "lead capability requires a lead block"),
            ("agents-only with lead", lambda raw: lines(raw)["gpt55"].update(
                lead={"effort": "ultracode", "env": {}}),
             "agents-only line must have lead: null"),
            ("route selector names another wire", lambda raw: lines(raw)["fable"].update(
                selector="claude-opus-5[1m]"),
             "selector rides route claude-opus-5 but names wire claude-fable-5"),
            ("@ in a line key", lambda raw: lines(raw).__setitem__(
                "sol@2", copy.deepcopy(lines(raw)["qwen-flash-next"])),
             "invalid line key"),
        ]
        for name, mutator, needle in cases:
            with self.subTest(case=name):
                errors = _mutate(mutator)
                self.assertTrue(any(needle in error for error in errors), f"{needle!r} not in {errors}")

    def test_schema_refuses_overlay_and_unknown_status(self) -> None:
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "models.schema.json")
        for field, value in (("registry_overlay", {}), ("status", "beta")):
            with self.subTest(field=field):
                document = strict_json.load(FIXTURE_ROOT / "catalog" / "models.json")
                document["models"]["sol"][field] = value
                problems = validate.validate(document, schema, "$")
                self.assertTrue(any(field in problem for problem in problems), problems)
        document = strict_json.load(FIXTURE_ROOT / "catalog" / "models.json")
        for removed in ("lanes", "default_lane", "client_selector", "compatible_roles", "role_hints"):
            with self.subTest(removed=removed):
                broken = copy.deepcopy(document)
                broken["models"]["sol"][removed] = {}
                self.assertTrue(validate.validate(broken, schema, "$"))

    # 5. retired -------------------------------------------------------
    def _retired_errors(self, entries: dict, mutate=None) -> list[str]:
        def mutator(raw):
            raw["docs"]["retired"]["retired"].update(copy.deepcopy(entries))
            if mutate is not None:
                mutate(raw)

        return _mutate(mutator)

    def test_retired_rules(self) -> None:
        sol_selectors = lambda n: {f"claude-multi-muse-t{n}-high[1m]": "output-config-high"}  # noqa: E731
        failing = [
            ("cycle", {"a": _retired_entry(successor="b", selectors=sol_selectors(1)),
                       "b": _retired_entry(successor="a", selectors=sol_selectors(2))}, None,
             "cycle a -> b -> a"),
            ("new successor", {"x": _retired_entry(successor="sol")},
             lambda raw: raw["docs"]["models"]["models"]["sol"].update(status="new"),
             "not an active line"),
            ("plain key is live", {"sol": _retired_entry()}, None, "retired key sol is also a live line"),
            ("@ base not live", {"nope@1": _retired_entry(generation="1")}, None, "'nope' is not a live line"),
            ("@ successor mismatch", {"sol@5.5": _retired_entry(generation="5.5", successor=None)}, None,
             "must be the live line 'sol'"),
            ("@ current generation", {"sol@5.6": _retired_entry(generation="5.6", successor="sol")}, None,
             "is the live line's current generation"),
            ("successor lacks roles", {"x": _retired_entry(successor="opus", roles="all",
                                                              capabilities=["lead", "agents"])}, None,
             "successor opus does not admit roles/capabilities"),
            ("undeclared contract", {"x": _retired_entry(
                selectors={"claude-multi-muse-test-max[1m]": "output-config-max"})}, None,
             "is not declared by provider 'meta'"),
            ("live selector on another wire", {"x": _retired_entry(
                provider="openai", last_wire="gpt-5.5",
                selectors={"gpt-multi-sol-high[1m]": "reasoning-effort-high"})}, None,
             "is live on models.sol with a different provider or wire"),
            ("duplicate retired base", {"x": _retired_entry(
                selectors={"claude-multi-muse-spark-high": "output-config-high"})}, None,
             "duplicates retired.muse-spark"),
            ("route on another wire", {"x": _retired_entry(
                provider="anthropic", last_wire="claude-opus-5", capabilities=["agents"],
                selectors={"claude-opus-4-8[1m]": None})}, None,
             "equal last_wire"),
            ("since after catalog", {"x": _retired_entry(since_catalog=99)}, None, "newer than catalog_version"),
            ("unknown provider", {"x": _retired_entry(provider="nope", selectors={"claude-multi-x-high": None})},
             None, "unknown provider 'nope'"),
            ("empty selectors", {"x": _retired_entry(selectors={})}, None, "at least one legacy selector"),
            ("secret reason", {"x": _retired_entry(reason="leaked sk-live1234567890abc")}, None,
             "secret-like value"),
        ]
        for name, entries, mutate, needle in failing:
            with self.subTest(case=name):
                errors = self._retired_errors(entries, mutate)
                self.assertTrue(any(needle in error for error in errors), f"{needle!r} not in {errors}")

        passing = [
            ("chain to a live line", {"x": _retired_entry(successor="y", selectors=sol_selectors(1)),
                                      "y": _retired_entry(successor="sol", selectors=sol_selectors(2))}),
            ("gpt55-shaped agents reviewer -> sol (all)", {"x": _retired_entry(
                provider="openai", last_wire="gpt-5.4", successor="sol",
                selectors={"gpt-multi-gpt54-high": "reasoning-effort-high"})}),
            ("same wire as the live line (fable51 case)", {"fable-old": _retired_entry(
                provider="anthropic", last_wire="claude-fable-5", successor="fable",
                capabilities=["lead", "agents"], roles="all",
                selectors={"claude-fable-5[1m]": None})}),
            ("@ generation of a live line", {"opus@4.7": _retired_entry(
                provider="anthropic", last_wire="claude-opus-4-7", successor="opus",
                generation="4.7", selectors={"claude-multi-opus-4-7[1m]": None})}),
        ]
        for name, entries in passing:
            with self.subTest(case=name):
                self.assertEqual(self._retired_errors(entries), [])

    # 6. resolve --------------------------------------------------------
    def test_resolve_key_and_selector(self) -> None:
        import dataclasses

        base = catalog.load_catalog(FIXTURE_ROOT)
        docs = copy.deepcopy(base.docs)
        docs["retired"]["retired"]["old-sol"] = _retired_entry(
            provider="openai", last_wire="gpt-5.4", successor="sol", since_catalog=30,
            selectors={"gpt-multi-old-sol-high": "reasoning-effort-high"},
        )
        bundle = dataclasses.replace(base, docs=docs)
        live = bundle.resolve_key("sol")
        self.assertEqual((live.key, live.chain, live.notice), ("sol", (), None))
        renamed = bundle.resolve_key("old-sol")
        self.assertEqual(renamed.key, "sol")
        self.assertEqual(renamed.chain, ("old-sol",))
        self.assertEqual(renamed.notice, "old-sol was retired in catalog 30 -> sol")
        gone = bundle.resolve_key("muse-spark")
        self.assertIsNone(gone.key)
        self.assertEqual(
            gone.notice, "muse-spark was retired in catalog 31 (no successor): needs a model choice"
        )
        with self.assertRaisesRegex(CatalogError, "unknown model key 'nope'"):
            bundle.resolve_key("nope")
        key, entry = bundle.resolve_selector("claude-multi-muse-spark-high[1m]")
        self.assertEqual(key, "muse-spark")
        self.assertEqual(entry["provider"], "meta")
        self.assertEqual(bundle.resolve_selector("claude-multi-muse-spark-xhigh")[0], "muse-spark")
        self.assertIsNone(bundle.resolve_selector("gpt-multi-sol-high[1m]"))
        self.assertEqual(
            bundle.retired_selector_index()["claude-multi-muse-spark-high"][2],
            "output-config-high",
        )

    # 7. New · Off -----------------------------------------------------
    def test_new_line_is_off_everywhere_but_the_lines(self) -> None:
        # Rewritten in place onto the 3.0 readers.  The
        # New line is absent from the merged offered view (scope.line_view,
        # the merged-view seam) and from every BindingPicker slot's rows, yet still a
        # line, still served and still a live key.  The 2.x editor state it
        # used to check is deleted; the ordinary_* compiler helpers are gone too.
        from claude_multi import profile, scope, settings, tui, views

        from _catalog import served_selectors

        # An agents-capable line that no seed binds (the catalog has no
        # legacy default composition).
        base = catalog.load_catalog(FIXTURE_ROOT)
        bound: set[str] = set()
        for document in base.seed_profiles.values():
            slots = [document.get("lead"), *document.get("agents", {}).values()]
            bound |= {s["model"] for s in slots if isinstance(s, dict) and "model" in s}
        key = next(
            (k for k, e in base.lines.items() if "agents" in e["capabilities"] and k not in bound),
            None,
        )
        self.assertIsNotNone(key, "the fixture has an unbound agents-capable line")
        # Not vacuous: while active, the line is offered and a picker lists it.
        base_lcat = profile.LineupCatalog.from_docs(base.docs)
        base_eff = settings.effective(
            {"version": settings.SETTINGS_DATA_VERSION}, provider_ids=base.providers, line_keys=base.lines
        )
        self.assertIn(key, {line.key for line in scope.line_view(base_lcat, base_eff).lines})
        base_rows = views.line_rows(base_lcat, base_eff, custom_ids=frozenset())
        self.assertIn(key, {
            item.key
            for item in views.picker_rows(
                base_rows, slot="binding", bindings={}, lcat=base_lcat, eff=base_eff, current=None
            ).items
        })
        root = _copy_tree(self)
        self._write_models(root, lambda d: d["models"][key].update(status="new"))
        bundle = catalog.load_catalog(root)
        self.assertIn(key, bundle.lines)
        lcat = profile.LineupCatalog.from_docs(bundle.docs)
        eff = settings.effective(
            {"version": settings.SETTINGS_DATA_VERSION},
            provider_ids=bundle.providers,
            line_keys=bundle.lines,
        )
        self.assertNotIn(key, {line.key for line in scope.line_view(lcat, eff).lines})
        rows = views.line_rows(lcat, eff, custom_ids=frozenset())
        self.assertFalse(next(row for row in rows if row.key == key).offered)
        for slot in (catalog.LEAD_ROLE, *catalog.AGENT_ROLE_IDS, "binding", "workflow"):
            with self.subTest(slot=slot):
                picker = tui.BindingPicker(
                    views.picker_rows(rows, slot=slot, bindings={}, lcat=lcat, eff=eff, current=None)
                )
                self.assertNotIn(key, {item.key for item in picker.model.items if item.kind == "line"})
        selector = catalog.line_selectors(bundle.lines[key])[0][1]
        self.assertIn(selector.removesuffix("[1m]"), served_selectors(root))
        self.assertEqual(bundle.resolve_key(key).key, key)

    # 9. custom (v2 synthetic entries, collision sets) ------------------
    def _registry(self, model_id: str = "my-model") -> dict:
        return {
            "version": 1,
            "providers": {},
            "models": {
                model_id: {
                    "wire_model": "some-wire",
                    "provider": "kimi",
                    "context_tokens": 262144,
                    "created_via": "manual",
                }
            },
        }

    def test_custom_entries_are_v2_and_merge_into_both_views(self) -> None:
        from claude_multi import custom

        bundle = catalog.load_catalog(FIXTURE_ROOT)
        snapshot = copy.deepcopy(bundle.docs)
        baseline = bundle.docs["gateway"]["gateway"]["cliproxyapi_baseline"]
        entry = custom.synthetic_entries(self._registry(), cliproxyapi=baseline)["my-model"]
        self.assertEqual(entry["efforts"], ["high"])
        self.assertEqual(entry["default_effort"], "high")
        self.assertEqual(entry["family"], "custom")
        self.assertEqual(entry["roles"], [])
        self.assertEqual(entry["selector"], "custom-my-model")
        self.assertEqual(catalog.line_family(entry, bundle.providers), "custom")
        merged = custom.merge_docs(bundle.docs, self._registry())
        self.assertEqual(merged["models-v2"]["models"]["my-model"], entry)
        # One merged v2 line map under both keys (the view is gone).
        self.assertIs(merged["models"], merged["models-v2"])
        # The Catalog's docs are never mutated by the merge.
        self.assertEqual(bundle.docs, snapshot)
        self.assertIs(bundle.docs["models"], bundle.docs["models-v2"])
        self.assertNotIn("my-model", bundle.lines)

    def test_custom_collisions_cover_new_lines_and_retired_keys(self) -> None:
        # The collision set is every v2 line (New
        # included), every retired key and every @ base - never the view.
        from claude_multi import custom

        root = _copy_tree(self)
        self._write_models(root, lambda d: d["models"]["qwen38"].update(status="new"))
        bundle = catalog.load_catalog(root)
        docs = copy.deepcopy(bundle.docs)
        docs["retired"]["retired"]["sol@5.5"] = _retired_entry(
            provider="openai", last_wire="gpt-5.5", successor="sol", generation="5.5",
            selectors={"gpt-multi-old-high": "reasoning-effort-high"},
        )
        for model_id, expected in (
            ("qwen38", "model qwen38"),
            ("muse-spark", "model muse-spark (retired catalog key)"),
        ):
            with self.subTest(model=model_id):
                registry = self._registry(model_id)
                self.assertEqual(custom.merge_conflicts(docs, registry), [expected])
                merged = custom.merge_docs(docs, registry)
                self.assertEqual(
                    merged["models-v2"]["models"].get(model_id),
                    docs["models-v2"]["models"].get(model_id),
                )
                self.assertEqual(
                    merged["models"]["models"].get(model_id),
                    docs["models"]["models"].get(model_id),
                )
        self.assertIn("qwen38", custom.catalog_line_ids(docs))
        self.assertLessEqual({"muse-spark", "sol@5.5", "sol"}, custom.retired_model_ids(docs))
        environ = {
            "HOME": tempfile.mkdtemp(prefix="claude-multi-custom-"),
        }
        self.addCleanup(lambda: shutil.rmtree(environ["HOME"], ignore_errors=True))
        environ["XDG_CONFIG_HOME"] = environ["HOME"] + "/.config"
        environ["XDG_STATE_HOME"] = environ["HOME"] + "/.local/state"
        for model_id, needle in (
            ("qwen38", "exists in the trusted catalog"),
            ("muse-spark", "model id muse-spark is a retired catalog key"),
        ):
            with self.subTest(add=model_id):
                with self.assertRaisesRegex(custom.CustomModelsError, needle):
                    custom.add_model(
                        environ,
                        model_id,
                        wire_model="w",
                        provider="kimi",
                        context_tokens=262144,
                        created_via="manual",
                        catalog_providers=bundle.providers,
                        catalog_models=custom.catalog_line_ids(docs),
                        retired_models=custom.retired_model_ids(docs),
                    )
        self.assertFalse((Path(environ["XDG_CONFIG_HOME"]) / "claude-multi" / "custom.json").exists())

    def test_custom_v1_registry_still_loads(self) -> None:
        from claude_multi import custom, state

        home = Path(tempfile.mkdtemp(prefix="claude-multi-custom-"))
        self.addCleanup(lambda: shutil.rmtree(home, ignore_errors=True))
        environ = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
        }
        path = custom.registry_path(environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, strict_json.pretty_file_bytes(self._registry()))
        self.assertEqual(custom.load_registry(environ)["models"]["my-model"]["provider"], "kimi")


class ShippedLineDefaultsTests(unittest.TestCase):
    """Catalog 33 default efforts."""

    def test_default_efforts(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        expected = {
            "opus": "high", "fable": "high", "sonnet": "high",
            "astra": "high", "sol": "high", "luna": "max",
            "muse": "xhigh", "muse-contributor": "high",
            "deepseek-flash": "high",
        }
        for key, effort in expected.items():
            with self.subTest(line=key):
                self.assertEqual(bundle.lines[key]["default_effort"], effort)
        # A client-effort line has one selector, at the default effort
        # (on catalog.line_selectors).
        self.assertEqual(
            [effort for effort, _s, _c in catalog.line_selectors(bundle.lines["opus"])], ["high"]
        )
        for key in ("opus", "fable", "sonnet"):
            self.assertEqual(
                bundle.lines[key]["efforts"], ["low", "medium", "high", "xhigh", "max"]
            )
        # The known release inventory is active; a
        # candidate checkout may also carry a promoted New · Off line (the
        # developer pipeline's output), which NewLineGateTests constrains.
        for key in expected:
            with self.subTest(line=key):
                self.assertEqual(bundle.lines[key]["status"], "active")
        self.assertTrue(all(line["status"] in ("active", "new") for line in bundle.lines.values()))
        for line in bundle.lines.values():
            if line["registry_overlay"] is not None:
                self.assertEqual(line["registry_overlay"], {"channel": bundle.providers[line["provider"]]["transport"]["pool"]})


class NewLineGateTests(unittest.TestCase):
    """New · Off lines are absent from seeds and
    fences until admitted — checked on every New line the shipped catalog
    carries and on a disposable New · Off draft promoted into a temporary
    copy, so the installed-registry presence path runs without weakening
    any promotion gate. Lines are selected by shape, never by model id."""

    def _new_lines_absent_until_admitted(self, bundle: catalog.Catalog, keys: list[str]) -> None:
        from claude_multi import profile, scope, settings

        cat = profile.LineupCatalog.from_catalog(bundle)
        providers = bundle.docs["providers"]["providers"]
        default = settings.effective({"version": 1}, provider_ids=providers, line_keys=bundle.lines)
        admitted = settings.effective({"version": 1, "admitted_lines": sorted(keys)},
                                      provider_ids=providers, line_keys=bundle.lines)
        for name, seed in bundle.seed_profiles.items():
            bound = {seed["lead"]["model"], *(slot["model"] for slot in seed["agents"].values())}
            self.assertFalse(bound & set(keys), name)
        for key in keys:
            with self.subTest(line=key):
                entry = bundle.lines[key]
                self.assertEqual(entry["status"], "new")
                self.assertFalse(settings.line_offered(key, entry, default))
                self.assertTrue(settings.line_offered(key, entry, admitted))
                selectors = {selector for _l, selector, _c in catalog.line_selectors(entry)}
                self.assertNotIn(key, {line.key for line in scope.line_view(cat, default).lines})
                if "agents" in entry["capabilities"]:
                    self.assertFalse(selectors & set(scope.agent_set(cat, default)))
                    self.assertTrue(selectors <= set(scope.agent_set(cat, admitted)))

    def test_shipped_new_lines_are_off_until_admitted(self) -> None:
        bundle = catalog.load_catalog(CATALOG_ROOT)
        keys = sorted(key for key, line in bundle.lines.items() if line["status"] == "new")
        self._new_lines_absent_until_admitted(bundle, keys)

    def test_disposable_new_off_draft_passes_the_gates_and_stays_off(self) -> None:
        from claude_multi import dev

        root = Path(tempfile.mkdtemp(prefix="claude-multi-newline-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        tree = root / "assets"
        for part in ("catalog", "schemas"):
            shutil.copytree(CATALOG_ROOT / part, tree / part)
        for name in ("version.json", "settings.json"):
            shutil.copyfile(CATALOG_ROOT / name, tree / name)
        for directory, _dirs, files in os.walk(tree):
            os.chmod(directory, 0o755)
            for name in files:
                os.chmod(Path(directory) / name, 0o644)
        raw = catalog.load_raw(tree)["docs"]
        providers = raw["providers"]["providers"]
        like_key = next(key for key, line in sorted(raw["models"]["models"].items())
                        if isinstance(line["efforts"], dict)
                        and providers[line["provider"]]["transport"].get("pool") == "codex"
                        and "agents" in line["capabilities"])
        entry = dev._scaffold_model_entry(raw, like_id=like_key, new_id="disposable-new",
                                          wire_model="gpt-disposable-new")
        entry.update(generation="0", display="Disposable New", routing_note="Test-only New · Off line.")
        entry["context"]["qualification"] = "Test-only; not benchmark-verified."
        draft = dev.make_model_draft(name="disposable", provider=entry["provider"], entry=entry,
                                     now="2026-09-30T00:00:00Z")
        dev._reject_qualify_markers(draft)
        images = dev.build_post_images(raw, draft)
        candidate = dev._apply_images_to_docs(raw, images)
        dev._check_new_entry_policy(candidate, draft)
        self.assertEqual(candidate["models"]["models"]["disposable-new"]["status"], "new")
        # The installed-registry presence path runs on the candidate and
        # reports the disposable pool wire (a report, never a refusal).
        registry = dev.registry_presence(candidate, {catalog.REGISTRY_DIR_ENV: str(
            Path(__file__).resolve().parent / "fixtures" / "registry")})
        self.assertTrue([line for line in registry if "'disposable-new' wire 'gpt-disposable-new'" in line])
        (tree / "catalog" / "models.json").write_bytes(images[next(iter(images))])
        bundle = catalog.load_catalog(tree)
        self._new_lines_absent_until_admitted(bundle, ["disposable-new"])
        # An explicit non-new status stays refused (no gate weakened).
        active = dev.make_model_draft(name="d", provider=entry["provider"],
                                      entry={**entry, "status": "active"}, now="2026-09-30T00:00:00Z")
        with self.assertRaisesRegex(dev.DevError, "New · Off"):
            dev.build_post_images(raw, active)



class ValidateLineExtractionTests(unittest.TestCase):
    """``catalog.validate_line`` is the catalog model loop, extracted.

    The catalog verdicts and their order are unchanged (a 6000-case
    differential against the earlier inline loop confirmed them); here: the
    loop delegates line by line, and the operator additions (the ``custom-``
    namespace and the overlay marker) only append.
    """

    def _line_errors(self, raw: dict) -> list[str]:
        docs = raw["docs"]
        roles = [r for r in docs["roles"]["roles"] if r != catalog.LEAD_ROLE]
        taken: dict[str, str] = {}
        routes: dict[str, str] = {}
        found: list[str] = []
        for key in sorted(docs["models"]["models"]):
            found += catalog.validate_line(
                key, docs["models"]["models"][key], providers=docs["providers"]["providers"],
                native_contract=docs["native-contract"], taken_selectors=taken, origin="catalog",
                taken_routes=routes, role_ids=roles,
            )
        return found

    def test_catalog_loop_is_validate_line_in_key_order(self) -> None:
        mutations = [
            lambda raw: raw["docs"]["models"]["models"]["sol"].update(default_effort="ultracode"),
            lambda raw: raw["docs"]["models"]["models"].__setitem__(
                "sol-dup", copy.deepcopy(raw["docs"]["models"]["models"]["sol"])),
            lambda raw: raw["docs"]["models"]["models"]["opus"].update(selector="claude-multi-render-x"),
            lambda raw: raw["docs"]["models"]["models"]["kimi-k3"].update(provider="nope"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                raw = _raw(FIXTURE_ROOT)
                mutate(raw)
                line_errors = self._line_errors(copy.deepcopy(raw))
                self.assertTrue(line_errors)
                errors = catalog.validate_catalog(raw)
                start = errors.index(line_errors[0])
                self.assertEqual(errors[start:start + len(line_errors)], line_errors)
        self.assertEqual(self._line_errors(_raw(FIXTURE_ROOT)), [])
        self.assertEqual(self._line_errors(_raw(SHIPPED_ROOT)), [])

    def test_custom_namespace_is_reserved_in_the_catalog(self) -> None:
        def key(raw):
            raw["docs"]["models"]["models"]["custom-x"] = copy.deepcopy(raw["docs"]["models"]["models"]["sol"])
            for level, spec in raw["docs"]["models"]["models"]["custom-x"]["efforts"].items():
                spec["selector"] = f"other-{level}[1m]"

        def selector(raw):
            for level, spec in raw["docs"]["models"]["models"]["sol"]["efforts"].items():
                spec["selector"] = f"custom-sol-{level}[1m]"

        def retired(raw):
            raw["docs"]["retired"]["retired"]["custom-old"] = copy.deepcopy(raw["docs"]["retired"]["retired"]["muse-spark"])
            raw["docs"]["retired"]["retired"]["custom-old"]["selectors"] = {"other-old": None}

        for name, mutate in (("key", key), ("selector", selector), ("retired", retired)):
            with self.subTest(case=name):
                self.assertTrue(any("reserved" in e and "custom-" in e for e in _mutate(mutate)))

    def test_overlay_marker_must_match_the_providers_pool(self) -> None:
        def overlay(key, value):
            return lambda raw: raw["docs"]["models"]["models"][key].__setitem__("registry_overlay", value)

        self.assertEqual(_mutate(overlay("sol", {"channel": "codex"})), [])
        self.assertTrue(any("channel 'claude'" in e for e in _mutate(overlay("sol", {"channel": "claude"}))))
        self.assertTrue(any("registry_overlay" in e for e in _mutate(overlay("kimi-k3", {"channel": "claude"}))))

        def retired(value, provider="openai", wire="gpt-fixture-old"):
            def mutate(raw):
                entry = raw["docs"]["retired"]["retired"]["muse-spark"]
                entry.update(provider=provider, last_wire=wire, registry_overlay=value,
                             selectors={"gpt-multi-muse-spark-high[1m]": "reasoning-effort-high"})
            return mutate

        self.assertEqual(_mutate(retired({"channel": "codex"})), [])
        self.assertTrue(any("registry_overlay" in e for e in _mutate(retired({"channel": "claude"}))))

    def test_overlay_wire_must_be_a_valid_overlay_name(self) -> None:
        self.assertIsNone(catalog.OVERLAY_NAME.fullmatch("vendor/model"))
        self.assertIsNotNone(catalog.OVERLAY_NAME.fullmatch("gpt-fixture-9.1"))
        self.assertIsNone(catalog.OVERLAY_NAME.fullmatch("a" * 129))
        self.assertEqual(catalog.OVERLAY_CHANNELS, ("claude", "codex"))


if __name__ == "__main__":
    unittest.main()


class AgentClassFieldTests(unittest.TestCase):
    """No line carries an agent context class of its own: an agent's class
    follows the session window (``profile.role_window``), so the catalog
    schema and the operator schema have no such field."""

    def _fixture_line(self, raw) -> str:
        # By shape: an agents-capable 1M line of the frozen fixture.
        lines = raw["docs"]["models"]["models"]
        return next(key for key, entry in lines.items()
                    if "agents" in entry["capabilities"] and entry["context"]["client_tokens"] == 1_000_000)

    def test_the_catalog_schema_refuses_an_agent_class_field(self) -> None:
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "models.schema.json")
        raw = _raw(FIXTURE_ROOT)
        key = self._fixture_line(raw)
        raw["docs"]["models"]["models"][key]["context"]["agent_client_tokens"] = 200_000
        problems = validate.validate(raw["docs"]["models"], schema, "$")
        self.assertTrue(any("agent_client_tokens" in problem for problem in problems), problems)

    def test_operator_authoring_has_no_agent_class_field(self) -> None:
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "operator-provider.schema.json")
        context = schema["properties"]["lines"]["additionalProperties"]["properties"]["context"]
        self.assertFalse(context.get("additionalProperties", True))
        self.assertNotIn("agent_client_tokens", context["properties"])


class T1OutputTests(unittest.TestCase):
    def test_output_limit_schema_and_validator_parity(self) -> None:
        t2 = strict_json.load(FIXTURE_ROOT / "schemas/operator-provider.schema.json")
        expected = t2["properties"]["lines"]["additionalProperties"]["properties"]["output"]
        valid = [
            {"declared_tokens": n, "source": source, "source_ref": "fixture reference"}
            for n in (1, 128000, 2097152) for source in ("listing", "docs", "operator", "registry")
        ]
        invalid = [
            {"declared_tokens": n, "source": "docs"} for n in (True, False, None, 0, -1, 2097153, 1.5, "128000")
        ] + [None, {}, {"declared_tokens": 1}, {"declared_tokens": 1, "source": "guessed"},
             {"declared_tokens": 1, "source": "docs", "source_ref": ""},
             {"declared_tokens": 1, "source": "docs", "source_ref": "x" * 257},
             {"declared_tokens": 1, "source": "docs", "extra": 1}]
        for name in ("models", "retired"):
            schema = strict_json.load(FIXTURE_ROOT / f"schemas/{name}.schema.json")
            self.assertEqual(schema["properties"][name]["additionalProperties"]["properties"]["output"], expected)
            for value in valid + invalid:
                with self.subTest(document=name, output=value):
                    raw = catalog.load_raw(FIXTURE_ROOT)
                    entries = raw["docs"][name][name]
                    entries[next(iter(entries))]["output"] = value
                    errors = validate.validate(raw["docs"][name], schema, "$") + catalog.validate_catalog(raw)
                    self.assertEqual(not errors, value in valid, errors)
        # Optional means absent, never a made-up default on existing models.
        self.assertEqual(catalog.validate_catalog(catalog.load_raw(FIXTURE_ROOT)), [])


@uses_shipped_catalog
class SolGenerationTests(unittest.TestCase):
    def test_aliases_context_and_successors(self) -> None:
        from claude_multi import continuity, operator, render

        bundle = catalog.load_catalog(SHIPPED_ROOT)
        key = "sol"
        line = bundle.lines[key]
        retired_key = f"{key}@6"
        old = bundle.retired[retired_key]
        self.assertEqual(old["last_wire"], f"gpt-{old['generation']}-{key}")
        self.assertEqual(line["wire_model"], f"gpt-{line['generation']}-{key}")
        self.assertEqual(old["selectors"], {
            f"gpt-multi-{key}-{level}[1m]": f"reasoning-effort-{level}"
            for level in line["efforts"]
        })
        self.assertEqual(line["generation"], "6.1")
        self.assertEqual(line["context"]["declared_tokens"], 1050000)
        self.assertEqual(line["context"]["client_tokens"], 1000000)
        self.assertNotIn("agent_client_tokens", line["context"])
        self.assertEqual(line["context"]["validated_tokens"], 200000)
        self.assertLessEqual(line["context"]["provider_tokens"], old["context_tokens"])
        self.assertEqual(line["output"]["declared_tokens"], 128000)
        self.assertEqual(line["output"]["source"], "docs")
        self.assertTrue(line["output"]["source_ref"].endswith(line["wire_model"]))
        self.assertIn("not Codex-route measured", line["routing_note"])
        self.assertEqual(line["registry_overlay"], {"channel": "codex"})
        self.assertEqual(set(line["efforts"]), {"low", "medium", "high", "xhigh", "max"})
        aliases = continuity.seed_entries(bundle)
        plan = operator.render_plan(bundle.docs, operator.validate_layer(bundle.docs, {}, schemas=operator.load_schemas(SHIPPED_ROOT)), None)
        doc, *_ = render.build_config_document(
            plan.docs["gateway"], bundle.providers, bundle.lines, home=Path("/fixture/home"),
            gateway_tokens=("fixture-token",), resolve_secret=lambda _n: "dummy-secret",
            continuity=aliases, oauth_overlay=plan.overlay)
        routes = {row["alias"]: row["name"] for row in doc["oauth-model-alias"]["codex"]}
        old_bases = set()
        for selector in old["selectors"]:
            base = selector.removesuffix("[1m]")
            old_bases.add(base)
            self.assertEqual(routes[base], old["last_wire"])
            for spelling in (base, base + "[1m]"):
                self.assertEqual(bundle.resolve_selector(spelling)[0], retired_key)
        for level, spec in line["efforts"].items():
            base = spec["selector"].removesuffix("[1m]")
            self.assertNotIn(base, old_bases)
            self.assertEqual(routes[base], line["wire_model"])
            self.assertTrue(spec["selector"].endswith("[1m]"))
        rows = [r for r in doc[render.OVERLAY_KEY]["codex"] if r["name"] == line["wire_model"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["max-context-length"], line["context"]["provider_tokens"])
        self.assertEqual(rows[0]["max-completion-tokens"], line["output"]["declared_tokens"])
        self.assertEqual(rows[0]["thinking"]["levels"], ["low", "medium", "high", "xhigh", "max"])
        self.assertIsNone(render.overlay_entry_problem(rows[0]))
        self.assertEqual(bundle.resolve_key(retired_key).key, key)
        self.assertEqual(bundle.resolve_key("gpt55").key, key)
        self.assertIn("2026-10-14T19:00:00Z", bundle.retired["gpt55"]["reason"])
        self.assertNotIn(retired_key, bundle.lines)
