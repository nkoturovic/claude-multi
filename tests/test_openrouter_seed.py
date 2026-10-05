"""The OpenRouter seed and aggregator lines: per-line families, the seed
rules, the shipped profiles kept byte-identical, the seed order and the
credential-to-profile table.

Behaviour runs on the fixture (its aggregator lines mirror the shipped
seed's slot families); the shipped-catalog classes assert invariants and
never pin a shipped model id.
"""

from __future__ import annotations

import copy
import hashlib
import unittest
from pathlib import Path
from unittest import mock

import test_cli  # module import: no test classes re-exported
from claude_multi import catalog, profile, readiness, routing, strict_json, validate, views
from claude_multi.cli import runtime as runtime_mod
from claude_multi.setup import defaults

from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog


def _raw(root: Path = FIXTURE_ROOT) -> dict:
    return catalog.load_raw(root)


def _aggregator_lines(lines: dict) -> dict:
    return {key: entry for key, entry in lines.items() if entry["provider"] in catalog.AGGREGATOR_PROVIDERS}


class FamilySchemaTests(unittest.TestCase):
    def test_family_is_an_optional_lowercase_name(self) -> None:
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "models.schema.json")
        document = strict_json.load(FIXTURE_ROOT / "catalog" / "models.json")
        key = next(iter(_aggregator_lines(document["models"])))
        for value, ok in (("x-ai", True), ("google", True), ("X-AI", False), ("-x", False), (3, False)):
            candidate = copy.deepcopy(document)
            candidate["models"][key]["family"] = value
            with self.subTest(family=value):
                self.assertEqual(validate.validate(candidate, schema, "$") == [], ok)
        candidate = copy.deepcopy(document)
        plain = next(k for k, e in candidate["models"].items() if e["provider"] not in catalog.AGGREGATOR_PROVIDERS)
        self.assertNotIn("family", candidate["models"][plain])
        self.assertEqual(validate.validate(candidate, schema, "$"), [])


class AggregatorValidationTests(unittest.TestCase):
    def _errors(self, mutate) -> list[str]:
        raw = _raw()
        mutate(raw["docs"]["models"]["models"])
        return catalog.validate_catalog(raw)

    def test_the_fixture_aggregator_lines_validate(self) -> None:
        self.assertEqual(catalog.validate_catalog(_raw()), [])
        lines = _aggregator_lines(_raw()["docs"]["models"]["models"])
        self.assertGreaterEqual(len(lines), 5)
        self.assertTrue(all(isinstance(entry.get("family"), str) for entry in lines.values()))

    def test_an_aggregator_line_declares_its_family(self) -> None:
        key = next(iter(_aggregator_lines(_raw()["docs"]["models"]["models"])))
        errors = self._errors(lambda lines: lines[key].pop("family"))
        self.assertIn(f"models.{key}.family: a line of aggregator provider 'openrouter' declares its family",
                      errors)

    def test_unknown_and_custom_are_never_declared(self) -> None:
        key = next(iter(_aggregator_lines(_raw()["docs"]["models"]["models"])))
        for family in ("unknown", "custom"):
            with self.subTest(family=family):
                errors = self._errors(lambda lines: lines[key].update(family=family))
                self.assertIn(f"models.{key}.family: {family!r} is not a declared family", errors)

    def test_only_an_aggregator_line_declares_a_family(self) -> None:
        lines = _raw()["docs"]["models"]["models"]
        key = next(k for k, e in lines.items() if e["provider"] not in catalog.AGGREGATOR_PROVIDERS)
        errors = self._errors(lambda models: models[key].update(family="anthropic"))
        self.assertIn(f"models.{key}.family: only a line of an aggregator provider (openrouter) declares a family",
                      errors)

    def _retired_errors(self, provider: str, family: str | None) -> list[str]:
        """A retired entry like the fixture's, moved onto ``provider``."""

        raw = _raw()
        retired = raw["docs"]["retired"]["retired"]
        entry = copy.deepcopy(next(iter(retired.values())))
        entry.update(provider=provider, last_wire="vendor/fixture-retired",
                     selectors={"claude-multi-fixture-retired-high": "output-config-high"})
        if family is not None:
            entry["family"] = family
        retired["fixture-retired"] = entry
        return [error for error in catalog.validate_catalog(raw) if error.startswith("retired.fixture-retired")]

    def test_a_retired_aggregator_entry_declares_its_family(self) -> None:
        self.assertEqual(self._retired_errors("openrouter", "vendor"), [])
        self.assertEqual(self._retired_errors("openrouter", None),
                         ["retired.fixture-retired.family: a line of aggregator provider 'openrouter' declares its "
                          "family"])
        self.assertEqual(self._retired_errors("openrouter", "unknown"),
                         ["retired.fixture-retired.family: 'unknown' is not a declared family"])
        other = next(pid for pid in _raw()["docs"]["providers"]["providers"]
                     if pid not in catalog.AGGREGATOR_PROVIDERS)
        self.assertIn(f"retired.fixture-retired.family: only a line of an aggregator provider (openrouter) "
                      "declares a family", self._retired_errors(other, "vendor"))

    def test_aggregator_providers_are_one_object(self) -> None:
        from claude_multi import operator

        self.assertIs(operator.AGGREGATOR_PROVIDERS, catalog.AGGREGATOR_PROVIDERS)
        self.assertEqual(catalog.AGGREGATOR_PROVIDERS, frozenset({"openrouter"}))

    def test_declared_families_are_catalog_families(self) -> None:
        from claude_multi import operator

        docs = _raw()["docs"]
        families = operator.t1_families(docs)
        for entry in _aggregator_lines(docs["models"]["models"]).values():
            self.assertIn(entry["family"], families)
        self.assertNotIn("unknown", families)
        self.assertIn("google", catalog.identity_tokens(docs))
        self.assertNotIn("unknown", catalog.identity_tokens(docs))


class LegacyCustomEntryTests(unittest.TestCase):
    """The family key no longer marks a legacy custom entry; its value does."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = catalog.load_catalog(FIXTURE_ROOT)
        cls.lcat = profile.LineupCatalog.from_catalog(cls.bundle)
        cls.key = next(iter(_aggregator_lines(cls.bundle.lines)))

    def test_the_predicate(self) -> None:
        self.assertTrue(catalog.is_legacy_custom_entry({"family": "custom"}))
        self.assertFalse(catalog.is_legacy_custom_entry({"family": "anthropic"}))
        self.assertFalse(catalog.is_legacy_custom_entry({}))

    def test_an_aggregator_line_is_gateway_effort_from_the_catalog(self) -> None:
        entry = self.bundle.lines[self.key]
        provider = self.bundle.providers[entry["provider"]]
        self.assertEqual(profile._line_mode(entry, provider), "gateway")
        self.assertEqual(profile._line_mode({**entry, "family": "custom"}, provider), "client")
        self.assertEqual(self.lcat.origin(self.key), "catalog")
        self.assertEqual(self.lcat.line_family(self.key, entry), entry["family"])

    def test_routing_and_the_line_rows_say_catalog(self) -> None:
        entry = self.bundle.lines[self.key]
        selector = entry["efforts"][entry["default_effort"]]["selector"]
        self.assertEqual(routing.current_route(selector, self.bundle.docs, {})[3], "catalog")
        docs = copy.deepcopy(self.bundle.docs)
        docs["models"]["models"][self.key]["family"] = "custom"
        self.assertEqual(routing.current_route(selector, docs, {})[3], "legacy-custom")
        from claude_multi import settings

        eff = settings.effective({}, provider_ids=self.bundle.providers, line_keys=self.bundle.lines)
        rows = views.line_rows(self.lcat, eff, custom_ids=frozenset())
        row = next(row for row in rows if row.key == self.key)
        self.assertEqual(row.source, "catalog")


class FixtureSeedRuleTests(unittest.TestCase):
    """The seed rules (name, marker, no named bindings, a clean evaluation, no
    secrets) hold for the aggregator seed too."""

    def test_the_seed_is_checked_like_every_seed(self) -> None:
        cases = {
            "name": (lambda doc: doc.update(name="other"), "profiles/openrouter: name must equal the file stem"),
            "seed": (lambda doc: doc.update(seed={"id": "openrouter"}),
                     'profiles/openrouter.seed: must be {"id": "openrouter", "version": <integer>}'),
            "use": (lambda doc: doc["agents"].update({"cm-explorer": {"use": "fast"}}),
                    "profiles/openrouter: seeds must not use named bindings"),
        }
        for label, (mutate, expected) in cases.items():
            raw = _raw()
            mutate(raw["docs"]["profiles/openrouter"])
            with self.subTest(rule=label):
                self.assertIn(expected, catalog.validate_catalog(raw))
        raw = _raw()
        raw["docs"]["profiles/openrouter"]["lead"]["model"] = "no-such-line"
        self.assertTrue(any(e.startswith("profiles/openrouter: lead.model") for e in catalog.validate_catalog(raw)))

    def test_the_review_sentence_is_cross_family(self) -> None:
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        lineup = profile.resolve(bundle.seed_profiles["openrouter"], bundle)
        self.assertNotIn("≈", views.review_sentence(lineup))
        self.assertIn("all cross-family", views.review_sentence(lineup))


@uses_shipped_catalog
class ShippedSeedTests(unittest.TestCase):
    # The seven earlier seeds stay byte-identical.
    SEED_SHA256 = {
        "balanced": "5735b15ed96484be0df0a56b6c73ed5c066c4c5c50c5cf11837b3a12ac1ae48d",
        "quality": "0c93298476c4b94075f0566909c2c281ec5e5cef05758955842056d0f0912748",
        "max": "eeb02d17e3c13438bcafdfd07cfaf192b86eb9719974e5eb9d93576c4a20c38f",
        "economy": "4aeb24cbe83c796ae63b4ca6aea3066448660b0687ac55b58c05ee766e516ec8",
        "claude": "9f15617bb558f08900a00c0f5b5701ed7da230ac0effd3be6437e831534c79a6",
        "openai": "9e3952e2da2db8d3e10e639f7ece34eccac444034d51a5ec5abd3a9ae8831c61",
        "direct": "a3d5b98af4be5be18613dddce86e223507a2fa5d6029b32ee1bf4ca95fa7dd65",
    }

    def test_the_seven_earlier_seeds_are_byte_identical(self) -> None:
        for name, digest in self.SEED_SHA256.items():
            with self.subTest(seed=name):
                data = (SHIPPED_ROOT / "catalog" / "profiles" / f"{name}.json").read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)

    def test_the_aggregator_seed_and_lines(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        provider = bundle.providers["openrouter"]
        self.assertEqual(provider["independence_family"], "unknown")
        seed = bundle.seed_profiles["openrouter"]
        self.assertEqual(seed["primary_provider"], "openrouter")
        self.assertEqual(seed["seed"], {"id": "openrouter", "version": 1})
        lineup = profile.resolve(seed, bundle)
        bound = [lineup.lead.binding, *(agent.binding for agent in lineup.agents.values())]
        self.assertEqual({binding.provider for binding in bound}, {"openrouter"})
        self.assertGreaterEqual(len({binding.family for binding in bound}), 4)
        self.assertFalse(lineup.routing.same_family_authors)
        self.assertNotIn("≈", views.review_sentence(lineup))
        for key, entry in _aggregator_lines(bundle.lines).items():
            with self.subTest(line=key):
                self.assertEqual(entry["status"], "active")
                self.assertNotIn(entry["family"], catalog.UNDECLARED_FAMILIES)
                # Efforts limited to the provider's reviewed contracts.
                self.assertLessEqual({spec["proxy_contract"] for spec in entry["efforts"].values()},
                                     set(provider["payload_contracts"]))
                self.assertIn(f"Listing context_length {entry['context']['declared_tokens']:,} (OpenRouter models list",
                              entry["context"]["qualification"])
                self.assertIn("admission smoke passed", entry["context"]["qualification"])
                self.assertEqual(entry["context"]["validated_tokens"], 200000)

    def test_an_anthropic_family_line_names_its_tool_check(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        for key, entry in _aggregator_lines(bundle.lines).items():
            variant = "strict automatic tool use" if entry["family"] == "anthropic" else "forced named tool"
            with self.subTest(line=key):
                self.assertIn(variant, entry["context"]["qualification"])


class SeedOrderTests(unittest.TestCase):
    def test_the_order(self) -> None:
        self.assertEqual(readiness.SEED_ORDER,
                         ("balanced", "quality", "max", "economy", "claude", "openrouter", "openai", "direct"))
        self.assertEqual(set(readiness.SEED_ORDER), set(catalog.SEED_PROFILE_NAMES))
        self.assertEqual(catalog.SEED_PROFILE_NAMES[-2:], ("openrouter", "direct"))
        self.assertFalse(hasattr(readiness, "NO_SATISFIABLE_SEED"))


class CredentialTableTests(test_cli.CLITestCase):
    """The default profile follows from what is connected (fixture observations)."""

    def connect(self, *, accounts: tuple[str, ...] = (), keys: tuple[str, ...] = (),
                anthropic_key: bool = False) -> defaults.DefaultChoice:
        runtime = self.runtime
        providers = copy.deepcopy(runtime.lineup_catalog().providers)
        if anthropic_key:
            # The selected API-key transport: a keyed route replaces the account pool.
            providers["anthropic"]["transport"] = {
                "kind": "direct", "base_url": "https://api.anthropic.com",
                "auth": {"kind": "header", "header": "x-api-key", "secret_ref": "env:PLATFORM_ANTHROPIC_API_KEY"}}
        keyed = {pid for pid, provider in providers.items()
                 if provider["transport"]["kind"] != "oauth-pool"
                 and provider["transport"].get("auth", {}).get("kind") not in (None, "none")}
        ready_keys = set(keys) | ({"anthropic"} if anthropic_key else set())
        observations = readiness.Observations(
            served=frozenset(self.served),
            oauth_records={"claude": int("claude" in accounts), "codex": int("codex" in accounts)},
        )
        lines = runtime.lineup_catalog().lines

        def secret_problems(_self, lineup):
            bound = {lines[b.key]["provider"] for b in [lineup.lead.binding,
                                                        *(a.binding for a in lineup.agents.values())]
                     if b.key in lines}
            return [f"provider {pid} ({pid}): no key" for pid in sorted(bound & keyed - ready_keys)]

        # "Nothing connected" is the setup layer's connections, here the fixture's inputs.
        connected = bool(accounts or keys or anthropic_key)
        with mock.patch.object(runtime_mod.Runtime, "readiness_observations", return_value=observations), \
                mock.patch.object(runtime_mod.Runtime, "readiness_providers", return_value=providers), \
                mock.patch.object(runtime_mod.Runtime, "lineup_secret_problems", secret_problems), \
                mock.patch("claude_multi.setup.status.connected", lambda _runtime: connected):
            return defaults.resolve_default(runtime)

    def test_the_six_rows(self) -> None:
        rows = (
            ({"accounts": ("claude",)}, "claude"),
            ({"anthropic_key": True}, "claude"),
            ({"accounts": ("claude", "codex")}, "balanced"),
            ({"accounts": ("codex",)}, "openai"),
            ({"keys": ("openrouter",)}, "openrouter"),
            ({"accounts": ("claude",), "keys": ("openrouter",)}, "claude"),
        )
        for connected, expected in rows:
            with self.subTest(connected=connected):
                choice = self.connect(**connected)
                self.assertEqual((choice.name, choice.reason, choice.ready, choice.notice),
                                 (expected, "seed", True, None))

    def test_anything_else_falls_back_with_the_notice(self) -> None:
        choice = self.connect(keys=("qwen",))
        self.assertEqual((choice.name, choice.reason, choice.ready), (catalog.DEFAULT_SEED, "fallback", False))
        self.assertTrue(choice.notice.startswith("! no profile is connected here (needs "), choice.notice)
        choice = self.connect()
        self.assertEqual(choice.notice, defaults.NONE_CONNECTED)


if __name__ == "__main__":
    unittest.main()
