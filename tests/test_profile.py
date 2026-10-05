"""Profiles v2: parse, evaluate/resolve, warnings, notices, routing.

Every case runs on the frozen fixture catalog with test-local profiles
(fixture ids may be pinned; shipped ids never are). Catalog mutations (a
test-local retired entry, a ``status: new`` line, a custom synthetic line,
a Meta line) are applied to a deep copy of ``load_raw(FIXTURE_ROOT)`` docs.
"""

from __future__ import annotations

import builtins
import copy
import os
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, custom, profile, settings, strict_json

from _catalog import FIXTURE_ROOT


_RAW = catalog.load_raw(FIXTURE_ROOT)
_BUNDLE = catalog.load_catalog(FIXTURE_ROOT)
_UNSET = object()


def _docs() -> dict:
    return copy.deepcopy(_RAW["docs"])


def _lcat(docs: dict | None = None) -> profile.LineupCatalog:
    return profile.LineupCatalog.from_docs(_docs() if docs is None else docs)


def b(model: str, effort: str) -> dict:
    return {"effort": effort, "model": model}


def doc(*, agents_over: dict | None = None, drop: tuple[str, ...] = (), **over) -> dict:
    """The base profile: lead opus55·ultracode; explorer/analyst/implementer
    sol·high; reviewer sol·xhigh; reviewer-strong opus55·xhigh; explore replace."""

    document = {
        "version": 2,
        "name": "t",
        "lead": b("opus55", "ultracode"),
        "agents": {
            "cm-explorer": b("sol", "high"),
            "cm-analyst": b("sol", "high"),
            "cm-implementer": b("sol", "high"),
            "cm-reviewer": b("sol", "xhigh"),
            "cm-reviewer-strong": b("opus55", "xhigh"),
        },
        "native_agents": {"explore": "replace", "general_purpose": "off", "plan": "native"},
    }
    for rid in drop:
        document["agents"].pop(rid)
    for rid, binding in (agents_over or {}).items():
        document["agents"][rid] = binding
    for key, value in over.items():
        if value is _UNSET:
            document.pop(key, None)
        else:
            document[key] = value
    return document


def direct(lead: dict, **over) -> dict:
    return doc(
        lead=lead,
        agents={},
        native_agents={"explore": "native", "general_purpose": "on", "plan": "native"},
        **over,
    )


def balanced_shaped() -> dict:
    """The fixture balanced seed's bindings."""

    return doc(
        agents={
            "cm-explorer": b("sol", "high"),
            "cm-analyst": b("sol", "high"),
            "cm-analyst-strong": b("opus55", "xhigh"),
            "cm-implementer-light": b("sol", "high"),
            "cm-implementer": b("sol", "high"),
            "cm-implementer-strong": b("sol", "xhigh"),
            "cm-reviewer": b("sol", "xhigh"),
            "cm-reviewer-strong": b("opus55", "xhigh"),
        }
    )


def claude_shaped() -> dict:
    """The fixture claude seed's bindings."""

    return doc(
        primary_provider="anthropic",
        agents={
            "cm-explorer": b("opus5", "xhigh"),
            "cm-analyst": b("opus5", "xhigh"),
            "cm-analyst-strong": b("opus55", "xhigh"),
            "cm-implementer-light": b("opus5", "xhigh"),
            "cm-implementer": b("opus5", "xhigh"),
            "cm-implementer-strong": b("opus55", "xhigh"),
            "cm-reviewer": b("opus5", "xhigh"),
            "cm-reviewer-strong": b("fable", "max"),
        },
    )


def _retired_x_old(docs: dict, successor: str | None = "sol") -> dict:
    entry = copy.deepcopy(docs["retired"]["retired"]["muse-spark"])
    entry.update(successor=successor, since_catalog=31, provider="openai")
    docs["retired"]["retired"]["x-old"] = entry
    return docs


def _with_meta_line(docs: dict) -> dict:
    entry = copy.deepcopy(docs["models"]["models"]["grok46"])
    entry.update(provider="meta", display="Meta Test Line")
    entry["efforts"] = {
        "high": {"proxy_contract": "output-config-high", "selector": "claude-multi-mx-high[1m]"}
    }
    entry["default_effort"] = "high"
    docs["models"]["models"]["mx"] = entry
    return docs


class ProfileTestCase(unittest.TestCase):
    maxDiff = None

    def errors(self, document, cat=None, **kw) -> list[str]:
        result = profile.evaluate(document, _lcat() if cat is None else cat, **kw)
        self.assertIsNone(result.lineup) if result.errors else None
        return list(result.errors)

    def assertInvalid(self, document, expected: list[str], cat=None, **kw) -> None:
        self.assertEqual(self.errors(document, cat, **kw), expected)

    def assertValid(self, document, cat=None, **kw) -> profile.ResolvedLineup:
        result = profile.evaluate(document, _lcat() if cat is None else cat, **kw)
        self.assertEqual(result.errors, ())
        self.assertIsNotNone(result.lineup)
        return result.lineup

    def codes(self, lineup) -> tuple[str, ...]:
        return tuple(finding.code for finding in lineup.warnings)


# ----------------------------------------------------------------- parse


class ParseTests(ProfileTestCase):
    def parse_errors(self, document) -> list[str]:
        with self.assertRaises(profile.ProfileValidationError) as ctx:
            profile.parse(document)
        return list(ctx.exception.errors)

    def test_version_one_is_e1_before_the_schema(self) -> None:
        self.assertEqual(
            self.parse_errors({"version": 1, "slots": []}),
            [
                "version: unsupported profile version 1 (profiles are v2; legacy compositions "
                "migrate with 'claude-multi profile migrate')"
            ],
        )

    def test_lead_under_agents_is_unexpected(self) -> None:
        self.assertEqual(
            self.parse_errors(doc(agents_over={"cm-lead": b("sol", "high")})),
            ["$.agents: unexpected key 'cm-lead'"],
        )

    def test_unknown_agent_id_is_unexpected(self) -> None:
        self.assertEqual(
            self.parse_errors(doc(agents_over={"cm-foo": b("sol", "high")})),
            ["$.agents: unexpected key 'cm-foo'"],
        )

    def test_missing_lead(self) -> None:
        self.assertEqual(
            self.parse_errors(doc(lead=_UNSET)), ["$: missing required key 'lead'"]
        )

    def test_binding_shapes_e2(self) -> None:
        self.assertEqual(
            self.parse_errors(doc(agents_over={"cm-analyst": {"model": "sol"}})),
            ['agents.cm-analyst: a binding is {"model", "effort"} or {"use"}'],
        )
        self.assertEqual(
            self.parse_errors(
                doc(lead={"model": "sol", "effort": "high", "use": "x"})
            ),
            ['lead: a binding is {"model", "effort"} or {"use"}'],
        )

    def test_workflows_enum(self) -> None:
        self.assertEqual(
            self.parse_errors(doc(workflows="semi")),
            ["$.workflows: value 'semi' is not in the allowed set"],
        )

    def test_non_object(self) -> None:
        self.assertEqual(self.parse_errors(["x"]), ["profile: expected an object"])

    def test_passing_v2_documents_parse_clean(self) -> None:
        # E1/E2 passing counterparts.
        base = doc()
        parsed = profile.parse(base)
        self.assertEqual(parsed, base)
        self.assertIsNot(parsed, base)  # a deep copy
        named = doc(lead={"use": "x"}, agents_over={"cm-analyst": {"use": "y"}})
        self.assertEqual(profile.parse(named)["lead"], {"use": "x"})

    def test_parse_never_mutates_its_input(self) -> None:
        base = doc()
        before = copy.deepcopy(base)
        profile.parse(base)["agents"].clear()
        self.assertEqual(base, before)

    def test_schema_is_the_package_copy_and_equals_the_catalog_schema(self) -> None:
        self.assertEqual(profile.profile_schema_path().name, "profile.schema.json")
        self.assertIn("profile", catalog.SCHEMA_NAMES)
        self.assertEqual(profile._PROFILE_SCHEMA, _RAW["schemas"]["profile"])

    def test_load_profile_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_bytes(strict_json.pretty_file_bytes(doc()))
            self.assertEqual(profile.load_profile_file(path), doc())
            path.write_text('{"version": 2, "version": 2}')
            with self.assertRaises(profile.ProfileError):
                profile.load_profile_file(path)
            path.write_bytes(strict_json.pretty_file_bytes({"version": 1}))
            with self.assertRaises(profile.ProfileValidationError):
                profile.load_profile_file(path)

    def test_referenced_bindings(self) -> None:
        document = doc(lead={"use": "lead-b"}, agents_over={"cm-analyst": {"use": "fast"}})
        self.assertEqual(
            profile.referenced_bindings(document),
            {"lead": "lead-b", "agents.cm-analyst": "fast"},
        )
        self.assertEqual(profile.referenced_bindings(doc()), {})


# ------------------------------------------------------- validation rules


class ProfileValidationTests(ProfileTestCase):
    def test_base_doc_is_valid(self) -> None:
        lineup = self.assertValid(doc())
        self.assertEqual(lineup.name, "t")

    def test_e3_unknown_model(self) -> None:
        self.assertInvalid(
            doc(agents_over={"cm-analyst": b("nope", "high")}),
            ["agents.cm-analyst.model: unknown model 'nope'"],
        )
        self.assertInvalid(doc(lead=b("nope", "ultracode")), ["lead.model: unknown model 'nope'"])

    def test_e4_retired_lead_without_successor(self) -> None:
        self.assertInvalid(
            doc(lead=b("muse-spark", "ultracode")),
            [
                "lead.model: muse-spark was retired in catalog 31 (no successor): "
                "needs a model choice"
            ],
        )

    def test_e4_passing_retired_lead_with_successor_resolves_with_notice(self) -> None:
        cat = _lcat(_retired_x_old(_docs()))
        lineup = self.assertValid(doc(lead=b("x-old", "ultracode")), cat)
        self.assertEqual(lineup.lead.binding.key, "sol")
        self.assertEqual(lineup.lead.binding.requested, "x-old")
        self.assertEqual(lineup.lead.binding.chain, ("x-old",))
        self.assertEqual(
            [(n.code, n.slot, n.message) for n in lineup.notices],
            [("retired-successor", "cm-lead", "lead: x-old was retired in catalog 31 -> sol")],
        )

    def test_e5_agent_effort(self) -> None:
        self.assertInvalid(
            doc(agents_over={"cm-analyst": b("sol", "medium")}),
            ["agents.cm-analyst.effort: 'medium' is not declared by 'sol' (declared: high, xhigh)"],
        )
        self.assertValid(doc(agents_over={"cm-analyst": b("sol", "high")}))

    def test_e5_lead_effort(self) -> None:
        self.assertInvalid(
            doc(lead=b("opus55", "high")),
            ["lead.effort: 'high' is not declared by 'opus55' (declared: xhigh, ultracode)"],
        )
        self.assertValid(doc(lead=b("opus55", "xhigh")))
        self.assertValid(doc(lead=b("opus55", "ultracode")))

    def test_e6_ultracode_is_lead_only(self) -> None:
        self.assertInvalid(
            doc(agents_over={"cm-reviewer": b("sol", "ultracode")}),
            ["agents.cm-reviewer.effort: 'ultracode' is lead-only"],
        )
        # Passing: an agent at xhigh under an ultracode lead.
        lineup = self.assertValid(doc(agents_over={"cm-reviewer": b("sol", "xhigh")}))
        self.assertEqual(lineup.lead.binding.effort, "ultracode")

    def _custom_docs(self) -> dict:
        registry = {
            "version": 1,
            "providers": {},
            "models": {
                "c1": {
                    "wire_model": "some-wire",
                    "provider": "kimi",
                    "context_tokens": 262144,
                    "created_via": "manual",
                }
            },
        }
        return custom.merge_docs(_BUNDLE.docs, registry)

    def test_e7_custom_model_in_a_saved_profile(self) -> None:
        cat = profile.LineupCatalog.from_docs(self._custom_docs())
        self.assertInvalid(
            direct(b("c1", "ultracode")),
            ["lead.model: custom model 'c1' cannot be bound in a profile (use ad-hoc direct)"],
            cat,
        )
        # Passing: the same slot on a non-custom line.
        self.assertValid(direct(b("kimi-k3", "ultracode")), cat)

    def test_e7_ad_hoc_direct_custom_lead_resolves(self) -> None:
        # A merged docs map (models-v2/roles-v2 win) builds a
        # correct LineupCatalog and an ad-hoc custom lead resolves.
        cat = profile.LineupCatalog.from_docs(self._custom_docs())
        self.assertIn("c1", cat.lines)
        self.assertIn("cm-explorer", cat.roles)
        lineup = self.assertValid(profile.ad_hoc_direct("c1"), cat, ad_hoc=True)
        self.assertEqual(lineup.lead.binding.family, "custom")
        self.assertEqual(lineup.lead.binding.mode, "client")
        self.assertEqual(lineup.lead.binding.selector, "custom-c1")
        self.assertIsNone(lineup.name)

    def test_e8_new_line_until_admitted(self) -> None:
        docs = _docs()
        docs["models"]["models"]["grok46"]["status"] = "new"
        cat = _lcat(docs)
        document = doc(agents_over={"cm-analyst": b("grok46", "high")})
        self.assertInvalid(
            document,
            ["agents.cm-analyst.model: model 'grok46' is New · Off (status new) until admitted"],
            cat,
        )
        eff = settings.effective(
            {"version": 1, "admitted_lines": ["grok46"]},
            provider_ids=cat.providers,
            line_keys=cat.lines,
        )
        self.assertValid(document, cat, effective=eff)

    def test_e9_disabled_provider(self) -> None:
        cat = _lcat()
        document = doc(agents_over={"cm-analyst": b("kimi-k3", "max")})
        eff = settings.effective(
            {"version": 1, "providers": {"kimi": {"enabled": False}}},
            provider_ids=cat.providers,
            line_keys=cat.lines,
        )
        self.assertInvalid(
            document, ["agents.cm-analyst.model: provider 'kimi' is disabled in Settings"],
            cat, effective=eff,
        )
        self.assertValid(document, cat, effective=None)

    def test_e10_lead_capability(self) -> None:
        self.assertInvalid(doc(lead=b("gpt55", "high")), ["lead.model: 'gpt55' lacks the lead capability"])
        # Passing: a lead-capable gateway line.
        self.assertValid(doc(lead=b("sol", "ultracode")))

    def test_e11_agents_capability_ends_the_slot_checks(self) -> None:
        # Exactly E11 (no E12/E5 for the same slot).
        self.assertInvalid(
            doc(agents_over={"cm-analyst": b("qwen-flash-next", "high")}),
            ["agents.cm-analyst.model: 'qwen-flash-next' lacks the agents capability"],
        )
        self.assertInvalid(
            doc(agents_over={"cm-analyst": b("qwen-flash-next", "max")}),
            ["agents.cm-analyst.model: 'qwen-flash-next' lacks the agents capability"],
        )
        # Passing.
        self.assertValid(doc(agents_over={"cm-analyst": b("sol", "high")}))

    def test_e12_admission_is_per_id(self) -> None:
        self.assertInvalid(
            doc(agents_over={"cm-explorer": b("opus", "xhigh")}),
            ["agents.cm-explorer.model: 'opus' does not admit cm-explorer"],
        )
        self.assertInvalid(
            doc(agents_over={"cm-reviewer-strong": b("gpt55", "high")}),
            ["agents.cm-reviewer-strong.model: 'gpt55' does not admit cm-reviewer-strong"],
        )
        self.assertValid(doc(agents_over={"cm-reviewer": b("gpt55", "high")}))
        self.assertValid(doc(agents_over={"cm-reviewer": b("opus", "xhigh")}))

    def test_e13_requires(self) -> None:
        self.assertInvalid(
            doc(agents_over={"cm-analyst-strong": b("sol", "xhigh")}, drop=("cm-analyst",)),
            ["agents.cm-analyst-strong: requires cm-analyst"],
        )
        self.assertInvalid(
            doc(agents_over={"cm-implementer-light": b("sol", "high")}, drop=("cm-implementer",)),
            ["agents.cm-implementer-light: requires cm-implementer"],
        )
        self.assertInvalid(
            doc(drop=("cm-reviewer",)),
            ["agents.cm-reviewer-strong: requires cm-reviewer"],
        )

    def test_e13_hint_names_the_unbound_dependency(self) -> None:
        self.assertInvalid(
            doc(
                agents_over={
                    "cm-analyst": b("muse-spark", "high"),
                    "cm-analyst-strong": b("sol", "xhigh"),
                }
            ),
            [
                "agents.cm-analyst-strong: requires cm-analyst "
                "(cm-analyst was unbound: 'muse-spark' was removed)"
            ],
        )

    def test_e13_passing_retired_agent_alone_is_unbound_with_notice(self) -> None:
        lineup = self.assertValid(doc(agents_over={"cm-analyst": b("muse-spark", "high")}))
        self.assertIn("cm-analyst", lineup.unbound)
        self.assertNotIn("cm-analyst", lineup.agents)
        self.assertEqual(
            [(n.code, n.slot, n.message, n.compact) for n in lineup.notices],
            [
                (
                    "retired-unbound",
                    "cm-analyst",
                    "analyst: 'muse-spark' was removed — unbound",
                    "analyst: 'muse-spark' was removed — unbound",
                )
            ],
        )

    def test_e16_explore_replace_requires_explorer(self) -> None:
        self.assertInvalid(
            doc(drop=("cm-explorer",)),
            ["native_agents.explore: 'replace' requires cm-explorer"],
        )
        self.assertInvalid(
            doc(agents_over={"cm-explorer": b("muse-spark", "high")}),
            [
                "native_agents.explore: 'replace' requires cm-explorer "
                "(cm-explorer was unbound: 'muse-spark' was removed)"
            ],
        )
        lineup = self.assertValid(direct(b("opus55", "ultracode")))
        self.assertTrue(lineup.is_direct)

    def test_e14_e15_named_bindings(self) -> None:
        self.assertInvalid(
            doc(agents_over={"cm-analyst": {"use": "frontier"}}),
            ["agents.cm-analyst.use: unknown named binding 'frontier'"],
        )
        fast = {"fast": b("sol", "ultracode")}
        self.assertInvalid(
            doc(agents_over={"cm-analyst": {"use": "fast"}}),
            ["agents.cm-analyst.use: named binding 'fast' carries lead-only effort 'ultracode'"],
            bindings=fast,
        )
        lineup = self.assertValid(doc(lead={"use": "fast"}), bindings=fast)
        self.assertEqual(lineup.lead.binding.named, "fast")
        self.assertEqual(lineup.lead.binding.key, "sol")
        self.assertEqual(lineup.named_bindings, ("fast",))
        # A present, valid named binding for an agent resolves.
        lineup = self.assertValid(
            doc(agents_over={"cm-analyst": {"use": "ok"}}), bindings={"ok": b("sol", "xhigh")}
        )
        self.assertEqual(lineup.agents["cm-analyst"].binding.named, "ok")

    def test_named_binding_error_form(self) -> None:
        self.assertInvalid(
            doc(lead={"use": "x"}),
            ["lead.use: named binding 'x': 'gpt55' lacks the lead capability"],
            bindings={"x": b("gpt55", "high")},
        )
        self.assertInvalid(
            doc(agents_over={"cm-analyst": {"use": "fast"}}),
            [
                "agents.cm-analyst.use: named binding 'fast': 'medium' is not declared "
                "by 'sol' (declared: high, xhigh)"
            ],
            bindings={"fast": b("sol", "medium")},
        )
        self.assertInvalid(
            doc(lead={"use": "gone"}),
            [
                "lead.use: named binding 'gone': muse-spark was retired in catalog 31 "
                "(no successor): needs a model choice"
            ],
            bindings={"gone": b("muse-spark", "ultracode")},
        )
        # Passing.
        lineup = self.assertValid(doc(lead={"use": "x"}), bindings={"x": b("opus55", "ultracode")})
        self.assertEqual(lineup.lead.binding.key, "opus55")

    def test_e17_e18_lead_providers(self) -> None:
        self.assertInvalid(
            doc(lead_providers=["nope"]),
            [
                "lead_providers: unknown provider 'nope'",
                "lead_providers: must include the lead's provider 'anthropic'",
            ],
        )
        self.assertInvalid(
            doc(lead_providers=["openai"]),
            ["lead_providers: must include the lead's provider 'anthropic'"],
        )
        lineup = self.assertValid(doc(lead_providers=["anthropic"]))
        self.assertEqual(lineup.lead_providers, ("anthropic",))

    def test_e19_primary_provider(self) -> None:
        self.assertInvalid(doc(primary_provider="nope"), ["primary_provider: unknown provider 'nope'"])
        self.assertEqual(self.assertValid(doc(primary_provider="anthropic")).primary_provider, "anthropic")

    def test_e20_e22_settings_overrides(self) -> None:
        self.assertInvalid(
            doc(settings_overrides={"window": 1}),
            ["settings_overrides.window: not overridable per profile"],
        )
        self.assertInvalid(
            doc(settings_overrides={"compaction_percent": True}),
            ["settings_overrides.compaction_percent: must be an integer"],
        )
        self.assertInvalid(
            doc(settings_overrides={"compaction_percent": "80"}),
            ["settings_overrides.compaction_percent: must be an integer"],
        )
        self.assertInvalid(
            doc(settings_overrides={"compaction_percent": 50}),
            ["settings_overrides.compaction_percent: 50 is outside 60–95"],
        )
        self.assertInvalid(
            doc(settings_overrides={"compaction_percent": 96}),
            ["settings_overrides.compaction_percent: 96 is outside 60–95"],
        )
        self.assertValid(doc(settings_overrides={"compaction_percent": 95}))
        lineup = self.assertValid(doc(settings_overrides={"compaction_percent": 60}))
        self.assertEqual(dict(lineup.settings_overrides), {"compaction_percent": 60})

    def test_overridable_settings_share_the_settings_constants(self) -> None:
        # One source for the key and bounds.
        self.assertEqual(
            dict(profile.OVERRIDABLE_SETTINGS),
            {
                settings.COMPACTION_PERCENT_KEY: (
                    settings.COMPACTION_PERCENT_MIN,
                    settings.COMPACTION_PERCENT_MAX,
                )
            },
        )

    def test_errors_accumulate_in_order(self) -> None:
        self.assertInvalid(
            doc(agents_over={"cm-analyst": b("sol", "medium")}, lead_providers=["nope", "anthropic"]),
            [
                "agents.cm-analyst.effort: 'medium' is not declared by 'sol' (declared: high, xhigh)",
                "lead_providers: unknown provider 'nope'",
            ],
        )

    def test_invalid_catalog_returns_errors_never_raises(self) -> None:
        # Unknown provider, malformed entry, missing role.
        docs = _docs()
        docs["models"]["models"]["sol"]["provider"] = "nope"
        self.assertInvalid(
            doc(agents={"cm-analyst": b("sol", "high")}, native_agents={
                "explore": "native", "general_purpose": "off", "plan": "native"}),
            ["agents.cm-analyst.model: line 'sol' names unknown provider 'nope'"],
            _lcat(docs),
        )
        docs = _docs()
        del docs["models"]["models"]["sol"]["efforts"]["high"]["selector"]
        self.assertInvalid(
            doc(agents={"cm-analyst": b("sol", "high")}, native_agents={
                "explore": "native", "general_purpose": "off", "plan": "native"}),
            [
                "agents.cm-analyst.model: line 'sol' is malformed "
                "(efforts.high lacks selector/proxy_contract)"
            ],
            _lcat(docs),
        )
        docs = _docs()
        docs["models"]["models"]["opus5"]["efforts"] = {"xhigh": {}}  # client line, map shape
        self.assertInvalid(
            direct(b("opus5", "ultracode")),
            ["lead.model: line 'opus5' is malformed (client-effort line without an efforts list)"],
            _lcat(docs),
        )
        docs = _docs()
        del docs["roles"]["roles"]["cm-explorer"]
        self.assertInvalid(
            doc(),
            ["agents.cm-explorer: role 'cm-explorer' is not in the catalog roles"],
            _lcat(docs),
        )


# --------------------------------------------------------------- warnings


class WarningTests(ProfileTestCase):
    def findings(self, document, code: str, cat=None, **kw) -> list[tuple]:
        lineup = self.assertValid(document, cat, **kw)
        return [
            (f.slot, f.message, f.compact) for f in lineup.warnings if f.code == code
        ]

    def test_w1_single_family(self) -> None:
        lineup = self.assertValid(claude_shaped())
        self.assertEqual(
            [(f.code, f.slot, f.message, f.compact) for f in lineup.warnings],
            [
                (
                    "same-family-review",
                    None,
                    "review same-family (reduced independence)",
                    "review same-family",
                )
            ],
        )

    def test_w1_named_authors(self) -> None:
        self.assertEqual(
            self.findings(
                doc(
                    drop=("cm-reviewer-strong",),
                    agents_over={"cm-implementer-light": b("sol", "high")},
                ),
                "same-family-review",
            ),
            [
                (
                    None,
                    "review same-family (reduced independence) for implementer-light, implementer",
                    "same-family review: implementer-light, implementer",
                )
            ],
        )

    def test_w1_clean(self) -> None:
        self.assertEqual(self.findings(doc(), "same-family-review"), [])

    def _w2_doc(self, **over) -> dict:
        return doc(
            agents_over={
                "cm-analyst-strong": b("opus55", "xhigh"),
                "cm-implementer": b("opus5", "xhigh"),
                "cm-implementer-strong": b("opus5", "xhigh"),
            },
            **over,
        )

    def test_w2_fires(self) -> None:
        self.assertEqual(
            self.findings(self._w2_doc(), "lead-strong-concentration"),
            [(None, "lead and every -strong slot are on anthropic", "lead+strong share anthropic")],
        )

    def test_w2_clean_and_suppressed(self) -> None:
        clean = self._w2_doc()
        clean["agents"]["cm-implementer-strong"] = b("sol", "xhigh")
        self.assertEqual(self.findings(clean, "lead-strong-concentration"), [])
        suppressed = self._w2_doc(primary_provider="anthropic")
        lineup = self.assertValid(suppressed)
        self.assertNotIn("lead-strong-concentration", self.codes(lineup))
        self.assertEqual(
            [n.message for n in lineup.outsiders],
            [
                "outsider: explorer → GPT-5.6 Sol (openai)",
                "outsider: analyst → GPT-5.6 Sol (openai)",
                "outsider: reviewer → GPT-5.6 Sol (openai)",
            ],
        )

    def test_w2_needs_a_strong_slot(self) -> None:
        document = direct(b("opus55", "ultracode"))
        self.assertNotIn("lead-strong-concentration", self.codes(self.assertValid(document)))
        no_strong = doc(
            drop=("cm-reviewer-strong",), agents_over={"cm-reviewer": b("opus5", "xhigh")}
        )
        self.assertNotIn("lead-strong-concentration", self.codes(self.assertValid(no_strong)))

    def test_w3_strong_not_stronger(self) -> None:
        expected = [
            (
                "cm-analyst-strong",
                "analyst-strong: -strong is not stronger (same model as analyst at the same "
                "or lower effort)",
                "analyst-strong: -strong is not stronger",
            )
        ]
        for plain, strong in (("high", "high"), ("xhigh", "high")):
            with self.subTest(plain=plain, strong=strong):
                document = doc(
                    agents_over={
                        "cm-analyst": b("sol", plain),
                        "cm-analyst-strong": b("sol", strong),
                    }
                )
                self.assertEqual(self.findings(document, "strong-not-stronger"), expected)

    def test_w3_clean(self) -> None:
        for strong in (b("sol", "xhigh"), b("opus55", "xhigh")):
            with self.subTest(strong=strong):
                document = doc(
                    agents_over={"cm-analyst": b("sol", "high"), "cm-analyst-strong": strong}
                )
                self.assertEqual(self.findings(document, "strong-not-stronger"), [])

    def test_w4_fan_out_quota(self) -> None:
        self.assertEqual(
            self.findings(doc(), "fan-out-quota"),
            [
                (
                    None,
                    "explorer and every implementer are on openai (codex quota shared)",
                    "explorer+implementers share openai",
                )
            ],
        )
        kimi = doc(
            agents_over={
                "cm-explorer": b("kimi-k3", "max"),
                "cm-implementer": b("kimi-k3", "max"),
            }
        )
        self.assertEqual(
            [f[1] for f in self.findings(kimi, "fan-out-quota")],
            ["explorer and every implementer are on kimi (API key quota shared)"],
        )

    def test_w4_clean_and_suppressed(self) -> None:
        clean = doc(agents_over={"cm-implementer-strong": b("opus55", "xhigh")})
        self.assertEqual(self.findings(clean, "fan-out-quota"), [])
        self.assertEqual(self.findings(doc(primary_provider="openai"), "fan-out-quota"), [])

    def test_w4_metering_helpers(self) -> None:
        providers = _BUNDLE.providers
        for provider_id, provider in providers.items():
            with self.subTest(provider=provider_id):
                self.assertEqual(profile.is_metered(provider), provider_id != "llm-local")
        self.assertEqual(profile.quota_label(providers["anthropic"]), "claude quota shared")
        self.assertEqual(profile.quota_label(providers["openai"]), "codex quota shared")
        self.assertEqual(profile.quota_label(providers["kimi"]), "API key quota shared")

    def test_w5_context_below_lead_class(self) -> None:
        self.assertEqual(
            self.findings(doc(agents_over={"cm-reviewer": b("gpt55", "high")}),
                          "context-below-lead-class"),
            [
                (
                    "cm-reviewer",
                    "reviewer: context 258K is below the lead's 1M class (allowed)",
                    "reviewer: context below lead class",
                )
            ],
        )
        self.assertEqual(
            [f[1] for f in self.findings(doc(agents_over={"cm-analyst": b("grok46", "high")}),
                                         "context-below-lead-class")],
            ["analyst: context 500K is below the lead's 1M class (allowed)"],
        )

    def test_w5_clean(self) -> None:
        self.assertEqual(self.findings(doc(), "context-below-lead-class"), [])
        self.assertEqual(
            self.findings(doc(lead=b("grok46", "ultracode")), "context-below-lead-class"), []
        )

    def test_w6_strict_tool_schema_route_is_a_warning(self) -> None:
        # Route-scoped, never an error.
        cat = _lcat(_with_meta_line(_docs()))
        document = doc(agents_over={"cm-analyst": b("mx", "high")})
        lineup = self.assertValid(document, cat)
        self.assertEqual(
            [(f.slot, f.message, f.compact) for f in lineup.warnings if f.code == "strict-tool-schema"],
            [
                (
                    None,
                    "analyst on meta: the route rejects tool schemas whose 'required' omits "
                    "a property (HTTP 400, e.g. hook evaluator calls)",
                    "meta strict tool schemas: analyst",
                )
            ],
        )
        lead = self.assertValid(doc(lead=b("mx", "ultracode"), agents_over={"cm-analyst": b("mx", "high")}), cat)
        self.assertEqual(
            [f.compact for f in lead.warnings if f.code == "strict-tool-schema"],
            ["meta strict tool schemas: lead, analyst"],
        )
        self.assertNotIn("strict-tool-schema", self.codes(self.assertValid(doc(), cat)))

    def test_warning_order(self) -> None:
        lineup = self.assertValid(
            doc(
                agents_over={
                    "cm-analyst": b("opus55", "xhigh"),
                    "cm-analyst-strong": b("opus55", "xhigh"),
                    "cm-reviewer": b("gpt55", "high"),
                },
                drop=("cm-explorer", "cm-reviewer-strong"),
                native_agents={"explore": "native", "general_purpose": "off", "plan": "native"},
            )
        )
        self.assertEqual(
            self.codes(lineup),
            (
                "same-family-review",
                "lead-strong-concentration",
                "strong-not-stronger",
                "context-below-lead-class",
            ),
        )


# ---------------------------------------------------------------- notices


class NoticeTests(ProfileTestCase):
    def test_n1_successor_direct_and_named(self) -> None:
        cat = _lcat(_retired_x_old(_docs()))
        lineup = self.assertValid(doc(agents_over={"cm-analyst": b("x-old", "high")}), cat)
        self.assertEqual(
            [(n.code, n.slot, n.message) for n in lineup.notices],
            [("retired-successor", "cm-analyst", "analyst: x-old was retired in catalog 31 -> sol")],
        )
        self.assertEqual(lineup.agents["cm-analyst"].binding.key, "sol")
        lineup = self.assertValid(
            doc(agents_over={"cm-analyst": {"use": "b"}}), cat, bindings={"b": b("x-old", "high")}
        )
        self.assertEqual(
            [n.message for n in lineup.notices],
            ["analyst: x-old was retired in catalog 31 -> sol (named binding 'b')"],
        )

    def test_n2_unbound_named(self) -> None:
        lineup = self.assertValid(
            doc(agents_over={"cm-analyst": {"use": "gone"}}),
            bindings={"gone": b("muse-spark", "high")},
        )
        self.assertEqual(
            [n.message for n in lineup.notices],
            ["analyst: 'muse-spark' was removed — unbound (named binding 'gone')"],
        )

    def test_notice_order_lead_then_agents_then_outsiders(self) -> None:
        cat = _lcat(_retired_x_old(_docs()))
        lineup = self.assertValid(
            doc(
                lead=b("x-old", "ultracode"),
                agents_over={"cm-analyst": b("muse-spark", "high")},
                primary_provider="openai",
            ),
            cat,
        )
        self.assertEqual(
            [n.code for n in lineup.notices],
            ["retired-successor", "retired-unbound", "outsider"],
        )

    def test_n3_outsiders(self) -> None:
        lineup = self.assertValid(
            doc(
                primary_provider="anthropic",
                agents={"cm-explorer": b("kimi-k3", "max")},
                native_agents={"explore": "replace", "general_purpose": "off", "plan": "native"},
            )
        )
        self.assertEqual(
            [(n.slot, n.message, n.compact) for n in lineup.outsiders],
            [
                (
                    "cm-explorer",
                    "outsider: explorer → Kimi K3 · 1M selector (kimi)",
                    "outsider: explorer → Kimi K3 · 1M selector (kimi)",
                )
            ],
        )
        lineup = self.assertValid(direct(b("sol", "ultracode"), primary_provider="anthropic"))
        self.assertEqual(
            [n.message for n in lineup.outsiders], ["outsider: lead → GPT-5.6 Sol (openai)"]
        )

    def test_no_outsiders_without_primary_provider(self) -> None:
        self.assertEqual(self.assertValid(doc()).outsiders, ())


# ---------------------------------------------------------------- routing


def _cells(routing: profile.Routing) -> list[tuple]:
    return [
        (row.author, row.normal.reviewer, row.normal.same_family, row.normal.only_other_family,
         row.normal.reason, row.high.reviewer, row.high.same_family, row.high.only_other_family,
         row.high.reason)
        for row in routing.rows
    ]


class RoutingTests(ProfileTestCase):
    def test_unknown_family_never_counts_cross_family(self) -> None:
        """``unknown`` is never evidence of independence,
        on either side; two known different families stay independent."""

        self.assertFalse(profile.independent_families("unknown", "anthropic"))
        self.assertFalse(profile.independent_families("anthropic", "unknown"))
        self.assertFalse(profile.independent_families("unknown", "unknown"))
        self.assertTrue(profile.independent_families("anthropic", "openai"))
        # The unknown reviewer is not chosen as cross-family while a known
        # other family is bound.
        routing = profile.derive_routing(
            {"cm-lead": "anthropic"}, {"cm-reviewer": "unknown", "cm-reviewer-strong": "openai"},
        )
        lead = routing.rows[0]
        self.assertEqual((lead.normal.reviewer, lead.normal.same_family), ("cm-reviewer-strong", False))
        # Only an unknown reviewer: no independent route (reduced independence).
        routing = profile.derive_routing({"cm-lead": "anthropic"}, {"cm-reviewer": "unknown"})
        self.assertEqual((routing.rows[0].normal.reviewer, routing.rows[0].normal.same_family),
                         ("cm-reviewer", True))
        self.assertEqual(routing.same_family_authors, ("cm-lead",))
        routing = profile.derive_routing({"cm-lead": "unknown"}, {"cm-reviewer": "openai"})
        self.assertTrue(routing.rows[0].normal.same_family)

    def test_single_family(self) -> None:
        routing = profile.derive_routing(
            {"cm-lead": "a", "cm-implementer": "a"},
            {"cm-reviewer": "a", "cm-reviewer-strong": "a"},
        )
        self.assertEqual(
            _cells(routing),
            [
                ("cm-implementer", "cm-reviewer", True, False, None,
                 "cm-reviewer-strong", True, False, None),
                ("cm-lead", "cm-reviewer-strong", True, False, None,
                 "cm-reviewer-strong", True, False, None),
            ],
        )
        self.assertTrue(routing.single_family)
        self.assertEqual(routing.same_family_authors, ("cm-implementer", "cm-lead"))

    def test_two_family_balanced_row(self) -> None:
        routing = profile.derive_routing(
            {
                "cm-lead": "anthropic",
                "cm-implementer-light": "openai",
                "cm-implementer": "openai",
                "cm-implementer-strong": "openai",
            },
            {"cm-reviewer": "openai", "cm-reviewer-strong": "anthropic"},
        )
        self.assertEqual(
            _cells(routing),
            [
                ("cm-implementer-light", None, False, False, "its check",
                 "cm-reviewer-strong", False, False, None),
                ("cm-implementer", "cm-reviewer-strong", False, True, None,
                 "cm-reviewer-strong", False, False, None),
                ("cm-implementer-strong", "cm-reviewer-strong", False, False, None,
                 "cm-reviewer-strong", False, False, None),
                ("cm-lead", "cm-reviewer", False, True, None,
                 "cm-reviewer", False, True, None),
            ],
        )
        self.assertEqual(routing.authors, 4)
        self.assertEqual(routing.same_family_authors, ())
        self.assertFalse(routing.single_family)

    def test_three_family(self) -> None:
        routing = profile.derive_routing(
            {"cm-implementer": "moonshot", "cm-lead": "anthropic"},
            {"cm-reviewer": "openai", "cm-reviewer-strong": "anthropic"},
        )
        row = routing.rows[0]
        self.assertEqual((row.normal.reviewer, row.high.reviewer),
                         ("cm-reviewer", "cm-reviewer-strong"))
        self.assertFalse(row.normal.same_family or row.high.same_family)
        self.assertFalse(row.normal.only_other_family or row.high.only_other_family)

    def test_reviewer_missing(self) -> None:
        routing = profile.derive_routing(
            {"cm-implementer": "openai", "cm-lead": "anthropic"}, {}
        )
        for row in routing.rows:
            for cell in (row.normal, row.high):
                self.assertEqual(cell, profile.RouteCell(None, False, False, "no reviewer bound"))
        self.assertEqual(routing.same_family_authors, ())
        self.assertFalse(routing.single_family)
        lineup = self.assertValid(direct(b("opus55", "ultracode")))
        self.assertEqual([row.author for row in lineup.routing.rows], ["cm-lead"])
        self.assertNotIn("same-family-review", self.codes(lineup))

    def test_only_cm_reviewer_bound(self) -> None:
        routing = profile.derive_routing(
            {"cm-implementer-strong": "openai", "cm-implementer": "openai", "cm-lead": "anthropic"},
            {"cm-reviewer": "anthropic"},
        )
        for row in routing.rows:
            self.assertEqual(row.normal.reviewer, "cm-reviewer")
            self.assertEqual(row.high.reviewer, "cm-reviewer")
            self.assertFalse(row.normal.only_other_family or row.high.only_other_family)

    def test_light_only(self) -> None:
        routing = profile.derive_routing(
            {"cm-implementer-light": "openai", "cm-lead": "anthropic"},
            {"cm-reviewer": "openai", "cm-reviewer-strong": "anthropic"},
        )
        light = routing.rows[0]
        self.assertEqual(light.author, "cm-implementer-light")
        self.assertEqual(light.normal, profile.RouteCell(None, False, False, "its check"))
        self.assertEqual(light.high, profile.RouteCell("cm-reviewer-strong", False, False, None))
        self.assertInvalid(
            doc(agents={"cm-implementer-light": b("sol", "high")},
                native_agents={"explore": "native", "general_purpose": "off", "plan": "native"}),
            ["agents.cm-implementer-light: requires cm-implementer"],
        )

    def test_row_order_and_non_authors(self) -> None:
        lineup = self.assertValid(
            balanced_shaped()
            | {"agents": {**balanced_shaped()["agents"], "cm-designer": b("sol", "high")}}
        )
        self.assertEqual(
            [row.author for row in lineup.routing.rows],
            ["cm-implementer-light", "cm-implementer", "cm-implementer-strong", "cm-lead"],
        )

    def test_balanced_shaped_profile_routing(self) -> None:
        lineup = self.assertValid(balanced_shaped())
        self.assertEqual(lineup.routing.authors, 4)
        self.assertEqual(lineup.routing.same_family_authors, ())
        self.assertEqual(
            [(row.author, row.normal.reviewer, row.high.reviewer) for row in lineup.routing.rows],
            [
                ("cm-implementer-light", None, "cm-reviewer-strong"),
                ("cm-implementer", "cm-reviewer-strong", "cm-reviewer-strong"),
                ("cm-implementer-strong", "cm-reviewer-strong", "cm-reviewer-strong"),
                ("cm-lead", "cm-reviewer", "cm-reviewer"),
            ],
        )


# --------------------------------------------------------- resolved lineup


class ResolvedLineupTests(ProfileTestCase):
    def test_lead_class(self) -> None:
        for key, expected in (("opus55", "large"), ("grok46", "grok"), ("qwen-flash-next", "flash431")):
            with self.subTest(key=key):
                self.assertEqual(self.assertValid(direct(b(key, "ultracode"))).lead_class, expected)

    def test_selectors(self) -> None:
        lines = _BUNDLE.lines
        lead = self.assertValid(doc()).lead.binding
        self.assertEqual((lead.mode, lead.selector, lead.proxy_contract),
                         ("client", lines["opus55"]["selector"], None))
        agent = self.assertValid(doc(agents_over={"cm-analyst": b("sol", "xhigh")})).agents["cm-analyst"].binding
        spec = lines["sol"]["efforts"]["xhigh"]
        self.assertEqual((agent.mode, agent.selector, agent.proxy_contract),
                         ("gateway", spec["selector"], spec["proxy_contract"]))
        ultra = self.assertValid(direct(b("sol", "ultracode"))).lead.binding
        default = lines["sol"]["efforts"][lines["sol"]["default_effort"]]
        self.assertEqual((ultra.selector, ultra.proxy_contract),
                         (default["selector"], default["proxy_contract"]))
        xhigh = self.assertValid(direct(b("sol", "xhigh"))).lead.binding
        self.assertEqual(xhigh.selector, lines["sol"]["efforts"]["xhigh"]["selector"])

    def test_binding_selector_helper(self) -> None:
        # Binding selectors use the declared effort, or the default for ultracode.
        lines, providers = _BUNDLE.lines, _BUNDLE.providers
        sol, opus55 = lines["sol"], lines["opus55"]
        self.assertEqual(
            profile.binding_selector(opus55, providers["anthropic"], "xhigh", lead=False),
            (opus55["selector"], None),
        )
        default = sol["efforts"][sol["default_effort"]]
        self.assertEqual(
            profile.binding_selector(sol, providers["openai"], "ultracode", lead=True),
            (default["selector"], default["proxy_contract"]),
        )
        xhigh = sol["efforts"]["xhigh"]
        for lead in (True, False):
            self.assertEqual(
                profile.binding_selector(sol, providers["openai"], "xhigh", lead=lead),
                (xhigh["selector"], xhigh["proxy_contract"]),
            )

    def test_efforts(self) -> None:
        off = self.assertValid(doc(workflows="off")).lead
        self.assertEqual(off.session_effort, "xhigh")
        native = self.assertValid(doc(workflows="native")).lead
        self.assertEqual(native.session_effort, "ultracode")
        self.assertEqual(native.comparison_effort, _BUNDLE.lines["opus55"]["default_effort"])
        self.assertEqual(self.assertValid(doc(lead=b("opus55", "xhigh"))).lead.comparison_effort, "xhigh")

    def test_workflows_default_and_optional_fields(self) -> None:
        # Absent workflows is "native" (2.x parity).
        lineup = self.assertValid(doc())
        self.assertEqual(lineup.workflows, "native")
        self.assertEqual(lineup.lead.session_effort, "ultracode")
        self.assertEqual(lineup.description, "")
        self.assertIsNone(lineup.lead_providers)
        self.assertIsNone(lineup.primary_provider)
        self.assertEqual(dict(lineup.settings_overrides), {})

    def test_lead_step(self) -> None:
        lineup = self.assertValid(doc(agents_over={"cm-analyst": b("opus55", "xhigh")}))
        self.assertFalse(lineup.agents["cm-analyst"].lead_step)
        self.assertTrue(lineup.agents["cm-explorer"].lead_step)

    def test_spread(self) -> None:
        lineup = self.assertValid(balanced_shaped())
        self.assertEqual(lineup.spread, (("openai", 6), ("anthropic", 2)))
        self.assertEqual(lineup.spread_text(), "openai 6 · anthropic 2 (+lead)")
        lineup = self.assertValid(direct(b("opus55", "ultracode")))
        self.assertEqual(lineup.spread, ())
        self.assertEqual(lineup.spread_text(), "anthropic 0 (+lead)")

    def test_applied_bindings_and_relaunch_fields(self) -> None:
        lineup = self.assertValid(
            doc(drop=("cm-explorer", "cm-analyst", "cm-implementer", "cm-reviewer-strong"),
                native_agents={"explore": "off", "general_purpose": "off", "plan": "native"},
                lead_providers=["anthropic"], settings_overrides={"compaction_percent": 80})
        )
        lines = _BUNDLE.lines
        self.assertEqual(
            lineup.applied_bindings(),
            {
                "agents": {
                    "cm-reviewer": {
                        "effort": "xhigh",
                        "generation": lines["sol"]["generation"],
                        "key": "sol",
                        "selector": lines["sol"]["efforts"]["xhigh"]["selector"],
                    }
                },
                "lead": {
                    "effort": "ultracode",
                    "generation": lines["opus55"]["generation"],
                    "key": "opus55",
                    "selector": lines["opus55"]["selector"],
                },
            },
        )
        self.assertEqual(
            lineup.relaunch_fields(),
            {
                "lead_class": "large",
                "lead_providers": ["anthropic"],
                "native_agents": {"explore": "off", "general_purpose": "off", "plan": "native"},
                "settings_overrides": {"compaction_percent": 80},
                "workflows": "native",
            },
        )

    def test_agents_mapping_is_read_only_and_ordered(self) -> None:
        lineup = self.assertValid(balanced_shaped())
        self.assertEqual(
            list(lineup.agents),
            [rid for rid in profile.AGENT_ROLE_IDS if rid != "cm-designer"],
        )
        with self.assertRaises(TypeError):
            lineup.agents["cm-designer"] = None  # type: ignore[index]
        self.assertEqual(lineup.unbound, ("cm-designer",))
        role = lineup.agents["cm-reviewer"].role
        self.assertEqual(role.function, "reviewer")
        self.assertEqual(role.disallowed_tools, catalog.READ_ONLY_TOOLS)
        self.assertEqual(lineup.agents["cm-implementer"].role.isolation, "worktree")

    def test_is_direct_and_ad_hoc(self) -> None:
        lineup = self.assertValid(profile.ad_hoc_direct("opus55"), ad_hoc=True)
        self.assertIsNone(lineup.name)
        self.assertTrue(lineup.is_direct)
        self.assertEqual(lineup.lead.binding.effort, "ultracode")
        self.assertEqual(lineup.native_agents["general_purpose"], "on")
        self.assertFalse(self.assertValid(doc()).is_direct)

    def test_retired_away_agents_are_not_direct(self) -> None:
        lineup = self.assertValid(
            doc(agents={"cm-analyst": b("muse-spark", "high")},
                native_agents={"explore": "native", "general_purpose": "off", "plan": "native"})
        )
        self.assertFalse(lineup.is_direct)
        self.assertEqual(dict(lineup.agents), {})

    def test_determinism(self) -> None:
        first = profile.resolve(balanced_shaped(), _BUNDLE)
        second = profile.resolve(balanced_shaped(), _BUNDLE)
        self.assertEqual(first, second)
        self.assertEqual(
            strict_json.canonical_file_bytes(first.applied_bindings()),
            strict_json.canonical_file_bytes(second.applied_bindings()),
        )

    def test_purity(self) -> None:
        cat = profile.LineupCatalog.from_catalog(_BUNDLE)

        def refuse(*_args, **_kwargs):
            raise AssertionError("filesystem access during resolve")

        with mock.patch.object(builtins, "open", refuse), mock.patch.object(
            os, "open", refuse
        ), mock.patch.object(Path, "read_bytes", refuse), mock.patch.object(Path, "exists", refuse):
            lineup = profile.resolve(balanced_shaped(), cat)
            profile.evaluate(doc(lead=b("nope", "ultracode")), cat)
        self.assertEqual(lineup.name, "t")

    def test_resolve_raises_validation_error(self) -> None:
        with self.assertRaises(profile.ProfileValidationError) as ctx:
            profile.resolve(doc(lead=b("nope", "ultracode")), _BUNDLE)
        self.assertEqual(ctx.exception.errors, ("lead.model: unknown model 'nope'",))

    def test_lineup_catalog_equivalence(self) -> None:
        from_docs = profile.LineupCatalog.from_docs(copy.deepcopy(_RAW["docs"]))
        from_catalog = profile.LineupCatalog.from_catalog(_BUNDLE)
        self.assertEqual(from_docs, from_catalog)
        self.assertEqual(
            profile.resolve(balanced_shaped(), from_docs),
            profile.resolve(balanced_shaped(), from_catalog),
        )
        self.assertEqual(
            profile.resolve(balanced_shaped(), _BUNDLE),
            profile.resolve(balanced_shaped(), from_docs),
        )
        # Loaded docs (v1 views under the plain keys) build the same catalog.
        self.assertEqual(profile.LineupCatalog.from_docs(_BUNDLE.docs), from_catalog)

    def test_key_resolution_matches_catalog(self) -> None:
        for key in ("sol", "muse-spark"):
            with self.subTest(key=key):
                self.assertEqual(
                    catalog.resolve_key_in(_BUNDLE.lines, _BUNDLE.retired, key),
                    _BUNDLE.resolve_key(key),
                )
        with self.assertRaises(catalog.CatalogError) as direct_error:
            catalog.resolve_key_in(_BUNDLE.lines, _BUNDLE.retired, "nope")
        with self.assertRaises(catalog.CatalogError) as method_error:
            _BUNDLE.resolve_key("nope")
        self.assertEqual(str(direct_error.exception), str(method_error.exception))
        self.assertEqual(str(direct_error.exception), "unknown model key 'nope'")


class HelperTests(unittest.TestCase):
    def test_label(self) -> None:
        self.assertEqual(profile.label("cm-lead"), "lead")
        self.assertEqual(profile.label("cm-analyst-strong"), "analyst-strong")

    def test_effort_rank(self) -> None:
        self.assertLess(profile.effort_rank("high"), profile.effort_rank("xhigh"))
        with self.assertRaises(ValueError):
            profile.effort_rank("ultracode")

    def test_declared_efforts(self) -> None:
        lines = _BUNDLE.lines
        self.assertEqual(profile.declared_efforts(lines["sol"]), ("high", "xhigh"))
        self.assertEqual(profile.declared_efforts(lines["opus55"]), ("xhigh",))
        self.assertEqual(
            profile.declared_efforts({"efforts": ["max", "low", "high"]}), ("low", "high", "max")
        )

    def test_format_tokens(self) -> None:
        self.assertEqual(profile.format_tokens(1_000_000), "1M")
        self.assertEqual(profile.format_tokens(258_400), "258K")
        self.assertEqual(profile.format_tokens(500_000), "500K")

    def test_module_imports(self) -> None:
        source = (Path(profile.__file__)).read_text()
        for forbidden in ("cli", "tui", "compiler", "scope", "launch", "transition", "composition"):
            with self.subTest(module=forbidden):
                self.assertNotRegex(
                    source, rf"(?m)^\s*(from \. import .*\b{forbidden}\b|from \.{forbidden} import|import claude_multi\.{forbidden})"
                )


class OperatorEffortModeTests(unittest.TestCase):
    """The entry's own effort shape decides client vs gateway;
    explicit origin (not ``family``) decides legacy-custom policy."""

    def test_list_shaped_line_on_a_gateway_adapter_is_client_effort(self) -> None:
        docs = _docs()
        kimi = docs["providers"]["providers"]["kimi"]
        entry = copy.deepcopy(docs["models"]["models"]["kimi-k3"])
        entry.pop("efforts")
        entry.update(selector="custom-op-list", efforts=["high"], default_effort="high")
        self.assertEqual(catalog.effort_mode(kimi), "gateway")
        self.assertEqual(profile.binding_selector(entry, kimi, "high", lead=True), ("custom-op-list", None))
        mapped = docs["models"]["models"]["kimi-k3"]
        level = next(iter(mapped["efforts"]))
        self.assertEqual(profile.binding_selector(mapped, kimi, level, lead=False)[0],
                         mapped["efforts"][level]["selector"])

    def test_origin_defaults_and_metadata(self) -> None:
        docs = _docs()
        docs["models"]["models"]["custom-op"] = copy.deepcopy(docs["models"]["models"]["kimi-k3"])
        docs[profile.OPERATOR_LINES_KEY] = {"custom-op": {"origin": "operator"}}
        merged = custom.merge_docs(docs, {"version": 1, "providers": {}, "models": {}})
        lcat = profile.LineupCatalog.from_docs(merged)
        self.assertEqual(lcat.origin("custom-op"), "operator")
        self.assertEqual(lcat.origin("kimi-k3"), "catalog")
        synthetic = dict(docs["models"]["models"]["kimi-k3"], family="custom")
        lines = dict(lcat.lines, legacy=synthetic)
        legacy = profile.LineupCatalog(lines=lines, retired=lcat.retired, providers=lcat.providers, roles=lcat.roles,
                                       agent_efforts=lcat.agent_efforts, lead_efforts=lcat.lead_efforts,
                                       catalog_version=lcat.catalog_version)
        self.assertEqual(legacy.origin("legacy"), "legacy-custom")


if __name__ == "__main__":
    unittest.main()
