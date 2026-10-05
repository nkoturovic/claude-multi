"""Guards for the frozen test asset root.

The fixture under ``tests/fixtures/assets`` is a constrained, frozen copy of
the shipped asset root. These guards keep it a faithful stand-in: schemas
must stay byte-identical to the shipped ones (a schema change must be
mirrored deliberately), and so must the byte-copied prompts, roles and
settings; the 2.x default composition is frozen at its catalog-32 bytes
(``tests/fixtures/v3/``, 2.x test input only); the fixture must validate
cleanly, its anthropic routes must cover every fixture Anthropic wire and
both client refusal-fallback targets, and the contract must never point at
a real binary. The seven fixture seed profiles are frozen
literals (not byte copies) that must mirror the shipped seeds' structure.
"""

from __future__ import annotations

import hashlib
import json
import unittest

from claude_multi import catalog, composition, profile, strict_json, validate

from _catalog import FIXTURE_ROOT, SHIPPED_ROOT
from _layout import REPO_ROOT


class FixtureAssetGuardTests(unittest.TestCase):
    def test_roots_are_distinct_and_fixture_lives_in_the_package(self) -> None:
        self.assertNotEqual(FIXTURE_ROOT, SHIPPED_ROOT)
        self.assertEqual(FIXTURE_ROOT.relative_to(REPO_ROOT).parts[:2], ("tests", "fixtures"))

    def test_fixture_schemas_byte_equal_shipped(self) -> None:
        shipped = sorted(p.name for p in (SHIPPED_ROOT / "schemas").iterdir() if p.is_file())
        fixture = sorted(p.name for p in (FIXTURE_ROOT / "schemas").iterdir() if p.is_file())
        self.assertEqual(fixture, shipped)
        for name in shipped:
            with self.subTest(schema=name):
                self.assertEqual(
                    (FIXTURE_ROOT / "schemas" / name).read_bytes(),
                    (SHIPPED_ROOT / "schemas" / name).read_bytes(),
                    f"fixture schemas/{name} drifted from shipped; mirror the change",
                )

    def test_fixture_byte_copies_equal_shipped(self) -> None:
        # The non-model files are byte-copies (role prompts,
        # roles, settings). Tests of role contracts run on the fixture, so a
        # shipped change must be mirrored here deliberately (then re-bless)
        # or this guard fails. The 2.x default composition is frozen
        # instead (test_fixture_default_composition_frozen).
        prompt_names = sorted(p.name for p in (SHIPPED_ROOT / "catalog" / "prompts").iterdir())
        # One prompt per function (six functions, ten role ids).
        self.assertEqual(
            prompt_names,
            [f"cm-{function}.md" for function in sorted(catalog.ROLE_FUNCTIONS)],
        )
        self.assertEqual(
            sorted(p.name for p in (FIXTURE_ROOT / "catalog" / "prompts").iterdir()),
            prompt_names,
        )
        relative = [
            *(f"catalog/prompts/{name}" for name in prompt_names),
            "catalog/roles.json",
            "settings.json",
        ]
        for rel in relative:
            with self.subTest(path=rel):
                self.assertEqual(
                    (FIXTURE_ROOT / rel).read_bytes(),
                    (SHIPPED_ROOT / rel).read_bytes(),
                    f"fixture {rel} drifted from shipped; mirror the change and re-bless",
                )

    def test_fixture_validates_clean(self) -> None:
        self.assertEqual(catalog.validate_catalog(catalog.load_raw(FIXTURE_ROOT)), [])
        catalog.load_catalog(FIXTURE_ROOT)  # full load (prompts, bundle hash)

    def test_fixture_default_composition_frozen(self) -> None:
        # Frozen at its catalog-32 bytes (goldens depend on it), in
        # tests/fixtures/v3/ (2.x test input), not the asset tree; the catalog
        # loader never reads it, so it is checked with the 2.x reader that
        # profile migrate keeps (composition.validate_document).
        data = (REPO_ROOT / "tests" / "fixtures" / "v3" / "default-composition.json").read_bytes()
        self.assertEqual(
            hashlib.sha256(data).hexdigest(),
            "3a07e2a4500702b500333dd746f3df03cdbd1466d1f4d21035448c8d7be60f11",
        )
        self.assertFalse((FIXTURE_ROOT / "catalog" / "compositions").exists())
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "composition.schema.json")
        self.assertEqual(validate.validate(strict_json.loads(data), schema, "$"), [])
        document = strict_json.loads(data)
        self.assertIs(composition.validate_document(document, schema), document)
        self.assertNotIn("compositions/default", catalog.load_catalog(FIXTURE_ROOT).docs)

    def test_fixture_routes_cover_anthropic_wires_and_refusal_targets(self) -> None:
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        routes = bundle.providers["anthropic"]["passthrough_routes"]
        names = {route["name"] for route in routes}
        self.assertLessEqual(set(catalog.REFUSAL_FALLBACK_WIRES), names)
        self.assertLessEqual(
            {
                line["wire_model"]
                for line in bundle.lines.values()
                if line["provider"] == "anthropic"
            },
            names,
        )
        self.assertTrue(all(route["fork"] for route in routes))

    def test_fixture_retired_validates(self) -> None:
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        schema = strict_json.load(FIXTURE_ROOT / "schemas" / "retired.schema.json")
        self.assertEqual(validate.validate(bundle.docs["retired"], schema, "$"), [])
        self.assertEqual(set(bundle.retired), {"muse-spark"})
        entry = bundle.retired["muse-spark"]
        self.assertIsNone(entry["successor"])
        self.assertEqual(entry["provider"], "meta")
        self.assertEqual(entry["since_catalog"], 31)

    def test_every_fixture_line_active(self) -> None:
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        self.assertEqual(len(bundle.lines), 15)
        for key, line in bundle.lines.items():
            with self.subTest(line=key):
                self.assertEqual(line["status"], "active")
                self.assertIsNone(line["registry_overlay"])
        # docs["models"] is the raw v2 document (the v1 view is gone).
        self.assertIs(bundle.docs["models"], bundle.docs["models-v2"])

    def test_fixture_contract_is_path_free_and_signed_manifest_shaped(self) -> None:
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        contract = bundle.docs["native-contract"]
        self.assertNotIn("claude", contract)
        self.assertNotIn("/", json.dumps(contract["verified"]).replace("identity+smoke", ""))
        entry = contract["verified"][0]
        manifest = json.loads((REPO_ROOT / "tests/fixtures/release-manifest/2.1.281/manifest.json").read_bytes())
        self.assertEqual(entry["version"], manifest["version"])
        self.assertEqual(entry["platforms"], {name: {"sha256": item["checksum"], "size": item["size"]}
                                              for name, item in manifest["platforms"].items()})

    def test_fixture_gateway_carries_no_patch_manifest(self) -> None:
        # Patch parity with gateway/UPSTREAM.json is a shipped-catalog test.
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        self.assertEqual(bundle.docs["gateway"]["gateway"]["patches"], [])


def _seed_docs(root) -> dict[str, dict]:
    return {
        path.stem: strict_json.load(path)
        for path in sorted((root / "catalog" / "profiles").glob("*.json"))
    }


class FixtureSeedGuardTests(unittest.TestCase):
    """The fixture seeds are frozen literals mirroring the shipped
    seeds' structure (tests/fixtures/build_fixture_assets.py FIXTURE_SEEDS)."""

    def test_fixture_seed_names_equal_shipped(self) -> None:
        fixture = set(_seed_docs(FIXTURE_ROOT))
        shipped = set(_seed_docs(SHIPPED_ROOT))
        self.assertEqual(fixture, shipped)
        self.assertEqual(fixture, set(catalog.SEED_PROFILE_NAMES))

    def test_fixture_seeds_mirror_shipped_structure(self) -> None:
        # Pins no model id: each tree resolves against its own catalog.
        trees = {}
        for label, root in (("fixture", FIXTURE_ROOT), ("shipped", SHIPPED_ROOT)):
            bundle = catalog.load_catalog(root)
            trees[label] = (bundle, _seed_docs(root))

        def facts(label: str, name: str) -> dict:
            bundle, docs = trees[label]
            document = docs[name]
            lineup = profile.resolve(document, bundle)
            families = {"cm-lead": lineup.lead.binding.family} | {
                rid: agent.binding.family for rid, agent in lineup.agents.items()
            }
            return {
                "bound agent ids": set(document["agents"]),
                "slot families": families,
                "native_agents": document["native_agents"],
                "workflows": document.get("workflows"),
                "lead_providers": document.get("lead_providers"),
                "primary_provider": document.get("primary_provider"),
                "seed": document.get("seed"),
                "routing": lineup.routing.rows,
                "warning codes": {w.code for w in lineup.warnings},
            }

        for name in catalog.SEED_PROFILE_NAMES:
            fixture, shipped = facts("fixture", name), facts("shipped", name)
            for aspect in fixture:
                with self.subTest(seed=name, aspect=aspect):
                    self.assertEqual(
                        fixture[aspect],
                        shipped[aspect],
                        f"fixture seed {name} no longer mirrors the shipped seed's "
                        f"{aspect}; mirror the change in "
                        "tests/fixtures/build_fixture_assets.py and re-emit",
                    )

    def test_fixture_seeds_validate_clean(self) -> None:
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        for name, document in _seed_docs(FIXTURE_ROOT).items():
            with self.subTest(seed=name):
                evaluation = profile.evaluate(document, bundle)
                self.assertEqual(evaluation.errors, ())
                self.assertEqual(
                    [
                        n.code
                        for n in evaluation.lineup.notices
                        if n.code in ("retired-successor", "retired-unbound")
                    ],
                    [],
                )


if __name__ == "__main__":
    unittest.main()
