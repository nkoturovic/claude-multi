"""Seed profiles.

``FixtureSeedTests`` resolve the frozen fixture seeds (fixture ids may
be pinned; the tables below name agent ids only). ``ShippedSeedContractTests``
assert invariants of the shipped seeds and never pin a shipped model id.
"""

from __future__ import annotations

import unittest

from claude_multi import catalog, profile, strict_json

from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog


R, RS = "cm-reviewer", "cm-reviewer-strong"
LIGHT, IMPL, STRONG, LEAD = (
    "cm-implementer-light",
    "cm-implementer",
    "cm-implementer-strong",
    "cm-lead",
)

# Expected routing, as (author, normal cell, high cell); a cell is
# (reviewer, same_family, only_other_family, reason).
_ITS_CHECK = (None, False, False, "its check")
_NO_REVIEWER = (None, False, False, "no reviewer bound")
_CROSS_WRITERS = (
    (LIGHT, _ITS_CHECK, (RS, False, False, None)),
    (IMPL, (RS, False, True, None), (RS, False, False, None)),
    (STRONG, (RS, False, False, None), (RS, False, False, None)),
    (LEAD, (R, False, True, None), (R, False, True, None)),
)
_QUALITY = (
    (LIGHT, _ITS_CHECK, (RS, False, False, None)),
    (IMPL, (RS, False, True, None), (RS, False, False, None)),
    (STRONG, (R, False, True, None), (R, False, True, None)),
    (LEAD, (R, False, True, None), (R, False, True, None)),
)
_SINGLE_FAMILY = (
    (LIGHT, _ITS_CHECK, (RS, True, False, None)),
    (IMPL, (R, True, False, None), (RS, True, False, None)),
    (STRONG, (RS, True, False, None), (RS, True, False, None)),
    (LEAD, (RS, True, False, None), (RS, True, False, None)),
)
# The aggregator seed: one provider, five families, every review cross-family.
_AGGREGATOR = (
    (LIGHT, _ITS_CHECK, (RS, False, False, None)),
    (IMPL, (R, False, False, None), (RS, False, False, None)),
    (STRONG, (RS, False, False, None), (RS, False, False, None)),
    (LEAD, (RS, False, False, None), (RS, False, False, None)),
)
EXPECTED_ROUTING = {
    "balanced": _CROSS_WRITERS,
    "quality": _QUALITY,
    "max": _CROSS_WRITERS,
    "economy": _CROSS_WRITERS,
    "claude": _SINGLE_FAMILY,
    "openai": _SINGLE_FAMILY,
    "openrouter": _AGGREGATOR,
    "direct": ((LEAD, _NO_REVIEWER, _NO_REVIEWER),),
}
EXPECTED_WARNINGS = {
    "balanced": ("fan-out-quota",),
    "quality": (),
    "max": ("fan-out-quota",),
    "economy": ("fan-out-quota",),
    "claude": ("same-family-review",),
    "openai": ("same-family-review",),
    # Its strong reviewer's own window is below the lead's class (allowed).
    "openrouter": ("context-below-lead-class",),
    "direct": (),
}
SINGLE_FAMILY_W1 = "review same-family (reduced independence)"


def routing_table(lineup: profile.ResolvedLineup) -> tuple:
    def cell(c: profile.RouteCell) -> tuple:
        return (c.reviewer, c.same_family, c.only_other_family, c.reason)

    return tuple((row.author, cell(row.normal), cell(row.high)) for row in lineup.routing.rows)


def seed_documents(root) -> dict[str, dict]:
    return {
        name: strict_json.load(root / "catalog" / "profiles" / f"{name}.json")
        for name in catalog.SEED_PROFILE_NAMES
    }


class FixtureSeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = catalog.load_catalog(FIXTURE_ROOT)
        cls.lineups = {
            name: profile.resolve(document, cls.bundle)
            for name, document in cls.bundle.seed_profiles.items()
        }

    def test_every_seed_resolves(self) -> None:
        self.assertEqual(tuple(self.lineups), catalog.SEED_PROFILE_NAMES)
        for name, lineup in self.lineups.items():
            with self.subTest(seed=name):
                self.assertEqual(lineup.name, name)
                self.assertEqual(lineup.notices, ())

    def test_routing_tables(self) -> None:
        for name, lineup in self.lineups.items():
            with self.subTest(seed=name):
                self.assertEqual(routing_table(lineup), EXPECTED_ROUTING[name])

    def test_warning_codes(self) -> None:
        for name, lineup in self.lineups.items():
            with self.subTest(seed=name):
                self.assertEqual(
                    tuple(w.code for w in lineup.warnings), EXPECTED_WARNINGS[name]
                )

    def test_balanced_exact_texts(self) -> None:
        lineup = self.lineups["balanced"]
        (warning,) = lineup.warnings
        self.assertEqual(
            warning.message, "explorer and every implementer are on openai (codex quota shared)"
        )
        self.assertEqual(warning.compact, "explorer+implementers share openai")
        self.assertEqual(lineup.spread_text(), "openai 6 · anthropic 2 (+lead)")
        self.assertEqual(lineup.routing.authors, 4)
        self.assertEqual(len(lineup.routing.same_family_authors), 0)

    def test_single_provider_seeds_one_single_family_w1(self) -> None:
        for name in ("claude", "openai"):
            with self.subTest(seed=name):
                lineup = self.lineups[name]
                w1 = [w for w in lineup.warnings if w.code == "same-family-review"]
                self.assertEqual(len(w1), 1)
                self.assertEqual(w1[0].message, SINGLE_FAMILY_W1)
                self.assertEqual(w1[0].compact, "review same-family")
                self.assertTrue(lineup.routing.single_family)
                self.assertEqual(len(lineup.routing.same_family_authors), 4)

    def test_direct(self) -> None:
        lineup = self.lineups["direct"]
        self.assertTrue(lineup.is_direct)
        self.assertEqual(lineup.lead_providers, ("anthropic",))
        self.assertEqual(lineup.native_agents["explore"], "native")
        self.assertEqual(lineup.native_agents["general_purpose"], "on")
        self.assertEqual(lineup.warnings, ())
        self.assertEqual(dict(lineup.agents), {})
        (row,) = lineup.routing.rows
        self.assertEqual(row.author, LEAD)
        self.assertEqual(row.normal.reason, "no reviewer bound")
        self.assertEqual(row.high.reason, "no reviewer bound")


@uses_shipped_catalog
class ShippedSeedContractTests(unittest.TestCase):
    """Invariants of the shipped seeds (no model ids)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = catalog.load_catalog(SHIPPED_ROOT)
        cls.documents = seed_documents(SHIPPED_ROOT)
        cls.evaluations = {
            name: profile.evaluate(document, cls.bundle)
            for name, document in cls.documents.items()
        }

    def lineup(self, name: str) -> profile.ResolvedLineup:
        evaluation = self.evaluations[name]
        self.assertEqual(evaluation.errors, (), name)
        assert evaluation.lineup is not None
        return evaluation.lineup

    def test_names_and_default(self) -> None:
        self.assertEqual(
            catalog.SEED_PROFILE_NAMES,
            ("balanced", "quality", "max", "economy", "claude", "openai", "openrouter", "direct"),
        )
        self.assertEqual(catalog.DEFAULT_SEED, "balanced")
        self.assertEqual(
            sorted(p.stem for p in (SHIPPED_ROOT / "catalog" / "profiles").glob("*.json")),
            sorted(catalog.SEED_PROFILE_NAMES),
        )
        self.assertEqual(self.bundle.seed_profiles, self.documents)

    def test_each_seed_is_v2_and_named_by_stem(self) -> None:
        for name, document in self.documents.items():
            with self.subTest(seed=name):
                self.assertEqual(document["version"], catalog.PROFILE_DATA_VERSION)
                self.assertEqual(profile.parse(document), document)
                self.assertEqual(document["name"], name)
                self.assertEqual(document["seed"]["id"], name)
                self.assertIsInstance(document["seed"]["version"], int)

    def test_each_seed_evaluates_clean(self) -> None:
        for name in self.documents:
            with self.subTest(seed=name):
                lineup = self.lineup(name)
                self.assertEqual(lineup.notices, ())
                self.assertEqual(lineup.outsiders, ())

    def test_no_named_bindings_and_ultracode_leads(self) -> None:
        for name, document in self.documents.items():
            with self.subTest(seed=name):
                for binding in [document["lead"], *document["agents"].values()]:
                    self.assertNotIn("use", binding)
                self.assertEqual(document["lead"]["effort"], profile.ULTRACODE)

    def test_every_bound_line_is_active(self) -> None:
        for name, document in self.documents.items():
            for binding in [document["lead"], *document["agents"].values()]:
                with self.subTest(seed=name, line=binding["model"]):
                    self.assertEqual(self.bundle.lines[binding["model"]]["status"], "active")

    def test_bound_agent_sets(self) -> None:
        for name, document in self.documents.items():
            with self.subTest(seed=name):
                if name == "direct":
                    self.assertEqual(document["agents"], {})
                    self.assertEqual(document["native_agents"]["explore"], "native")
                    self.assertEqual(document["native_agents"]["general_purpose"], "on")
                else:
                    self.assertEqual(
                        set(document["agents"]), set(catalog.AGENT_ROLE_IDS) - {"cm-designer"}
                    )

    def test_multi_provider_seeds(self) -> None:
        # Multi-provider = not direct and no primary_provider.
        checked = 0
        for name in self.documents:
            lineup = self.lineup(name)
            if lineup.is_direct or lineup.primary_provider is not None:
                continue
            checked += 1
            with self.subTest(seed=name):
                strong = {
                    agent.binding.provider
                    for rid, agent in lineup.agents.items()
                    if rid.endswith("-strong")
                }
                self.assertGreaterEqual(len(strong), 2)
                for row in lineup.routing.rows:
                    self.assertFalse(row.normal.same_family, row.author)
                    self.assertFalse(row.high.same_family, row.author)
                self.assertNotIn(
                    "lead-strong-concentration", {w.code for w in lineup.warnings}
                )
        self.assertEqual(checked, 4)

    def test_single_provider_seeds(self) -> None:
        checked = aggregators = 0
        for name in self.documents:
            lineup = self.lineup(name)
            primary = lineup.primary_provider
            if primary is None:
                continue
            checked += 1
            aggregator = primary in catalog.AGGREGATOR_PROVIDERS
            aggregators += aggregator
            with self.subTest(seed=name):
                self.assertEqual(lineup.lead.binding.provider, primary)
                for agent in lineup.agents.values():
                    self.assertEqual(agent.binding.provider, primary)
                # Every cell with a reviewer is same-family
                # (the light writer's normal "its check" cell has none); an
                # aggregator seed's lines declare families and every review
                # is cross-family instead.
                for row in lineup.routing.rows:
                    for cell in (row.normal, row.high):
                        if cell.reviewer is not None:
                            self.assertEqual(cell.same_family, not aggregator, row.author)
                w1 = [w for w in lineup.warnings if w.code == "same-family-review"]
                self.assertEqual([w.message for w in w1], [] if aggregator else [SINGLE_FAMILY_W1])
        self.assertEqual((checked, aggregators), (3, 1))

    def test_no_w3_and_w5_only_on_the_aggregator_seed(self) -> None:
        for name in self.documents:
            with self.subTest(seed=name):
                lineup = self.lineup(name)
                codes = {w.code for w in lineup.warnings}
                self.assertNotIn("strong-not-stronger", codes)
                if lineup.primary_provider not in catalog.AGGREGATOR_PROVIDERS:
                    self.assertNotIn("context-below-lead-class", codes)

    def test_routing_and_warning_tables(self) -> None:
        for name in self.documents:
            with self.subTest(seed=name):
                lineup = self.lineup(name)
                self.assertEqual(routing_table(lineup), EXPECTED_ROUTING[name])
                self.assertEqual(
                    tuple(w.code for w in lineup.warnings), EXPECTED_WARNINGS[name]
                )


@uses_shipped_catalog
class SeedRowTests(unittest.TestCase):
    # The approved seed table, not a generic effort-increment policy. Stable line
    # keys only; wire ids and generation selectors are derived from the catalog.
    # The quality row's strong writer and the claude seed rely on the
    # quality seed-version bump.
    ROWS = {
        "quality": (2, "sol:high sol:xhigh astra:max sol:xhigh sol:xhigh sonnet:xhigh astra:max opus:xhigh"),
        # Sonnet writers on the claude seed; strong writer stays Opus.
        "claude": (2, "sonnet:medium opus:high opus:xhigh sonnet:high sonnet:high opus:xhigh opus:xhigh fable:xhigh"),
        "balanced": (3, "sol:high sol:xhigh opus:xhigh luna:max sol:xhigh sol:max sol:max opus:xhigh"),
        "max": (2, "sol:high sol:xhigh fable:xhigh sol:xhigh sol:xhigh astra:max astra:max opus:xhigh"),
        "economy": (3, "luna:xhigh sol:xhigh astra:xhigh luna:max sol:xhigh astra:xhigh sol:xhigh opus:high"),
        "openai": (3, "sol:high sol:xhigh astra:max sol:max sol:xhigh astra:xhigh sol:max astra:max"),
    }
    ROLES = ("cm-explorer", "cm-analyst", "cm-analyst-strong", LIGHT, IMPL, STRONG, R, RS)

    def test_exact_approved_rows_and_versions(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        for name, (version, rows) in self.ROWS.items():
            document = bundle.seed_profiles[name]
            self.assertEqual(document["seed"]["version"], version, name)
            expected = {rid: dict(zip(("model", "effort"), binding.split(":")))
                        for rid, binding in zip(self.ROLES, rows.split())}
            self.assertEqual(document["agents"], expected, name)
            self.assertEqual(profile.evaluate(document, bundle).errors, (), name)
        self.assertEqual(bundle.seed_profiles["direct"]["seed"]["version"], 1)
        self.assertEqual(bundle.seed_profiles["claude"]["primary_provider"], "anthropic")
        self.assertEqual(bundle.seed_profiles["openai"]["primary_provider"], "openai")
        self.assertEqual(bundle.seed_profiles["claude"]["agents"][STRONG], {"model": "opus", "effort": "xhigh"})

    def test_actual_cross_family_review_routes(self) -> None:
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        for name, document in bundle.seed_profiles.items():
            lineup = profile.resolve(document, bundle)
            self.assertEqual(routing_table(lineup), EXPECTED_ROUTING[name], name)
            for row in lineup.routing.rows:
                for cell in (row.normal, row.high):
                    if cell.reviewer is None:
                        continue
                    author = lineup.lead if row.author == LEAD else lineup.agents[row.author]
                    reviewer = lineup.agents[cell.reviewer]
                    self.assertEqual(author.binding.family == reviewer.binding.family,
                                     name in ("claude", "openai"), (name, row.author, cell.reviewer))

    def test_installed_seed_adoption_is_explicit(self) -> None:
        import copy
        import tempfile

        bundle = catalog.load_catalog(SHIPPED_ROOT)
        with tempfile.TemporaryDirectory() as home:
            store = profile.ProfileStore(environ={"HOME": home}, seeds=bundle.seed_profiles)
            store.install_seeds()
            older = copy.deepcopy(store.load("balanced"))
            older["seed"]["version"] -= 1
            older["agents"][IMPL]["effort"] = "high"
            store.save(older)
            store.install_seeds()
            self.assertEqual(store.load("balanced"), older)
            self.assertTrue(store.seed_updates())
            store.reseed("balanced")
            self.assertEqual(store.load("balanced"), bundle.seed_profiles["balanced"])


if __name__ == "__main__":
    unittest.main()
