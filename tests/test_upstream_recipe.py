"""The gateway recipe (gateway/UPSTREAM.json) is the single authority.

The closed schema, the patch files and their sha256, the catalog patch list,
the Nix expression and the build tool all agree with it; no second editable
patch list exists. These tests read the shipped recipe and patches (shape
tests), never a built gateway.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import unittest

from _layout import (
    BUILD_TOOL,
    CANDIDATE_DIR,
    FLAKE_LOCK,
    FLAKE_NIX,
    NIX_DIR,
    PATCH_DIR,
    REPO_ROOT,
    RESOURCES_ROOT,
    UPSTREAM_JSON,
    UPSTREAM_REVIEW,
)
from claude_multi import strict_json, validate

SCHEMA = RESOURCES_ROOT / "schemas" / "gateway-upstream.schema.json"


def load_recipe():
    return strict_json.load(UPSTREAM_JSON)


def load_build_tool():
    spec = importlib.util.spec_from_file_location("gateway_build_tool", BUILD_TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def admitted_names(recipe):
    return [entry["basename"] for entry in recipe["series"] if entry["admitted"]]


def added_lines(patch_text, *, tests=False):
    """Added lines per changed Go file, non-test files unless ``tests``."""

    lines, current = [], None
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            current = line.rsplit(" b/", 1)[-1]
        elif line.startswith("+") and not line.startswith("+++") and current and current.endswith(".go"):
            if current.endswith("_test.go") == tests:
                lines.append(line[1:])
    return lines


class RecipeSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = strict_json.load(SCHEMA)
        self.recipe = load_recipe()

    def test_schema_is_a_closed_supported_schema(self):
        validate.check_schema(self.schema)
        self.assertIs(self.schema["additionalProperties"], False)

    def test_recipe_validates(self):
        self.assertEqual(validate.validate(self.recipe, self.schema), [])

    def test_schema_refuses_unknown_keys_and_malformed_values(self):
        cases = {
            "extra top-level key": lambda doc: doc.update({"patches": []}),
            "extra series key": lambda doc: doc["series"][0].update({"number": 1}),
            "short commit": lambda doc: doc["upstream"].update({"commit": "673131f5"}),
            "candidate admitted": lambda doc: doc["candidates"][0].update({"admitted": True}),
            "series not admitted": lambda doc: doc["series"][0].update({"admitted": False}),
            "cgo build": lambda doc: doc["build"]["env"].update({"CGO_ENABLED": "1"}),
            "missing host archive": lambda doc: doc["toolchain"]["archives"].pop("darwin-arm64"),
            "gate subtest selection": lambda doc: doc["gates"][0].update({"run": "TestA/sub"}),
            "gate without platform": lambda doc: doc["gates"][0].pop("platforms"),
        }
        for label, change in cases.items():
            document = copy.deepcopy(self.recipe)
            change(document)
            with self.subTest(case=label):
                self.assertNotEqual(validate.validate(document, self.schema), [])

    def test_fixture_mirror_of_the_schema(self):
        mirror = REPO_ROOT / "tests" / "fixtures" / "assets" / "schemas" / SCHEMA.name
        self.assertEqual(mirror.read_bytes(), SCHEMA.read_bytes())


class SeriesIdentityTests(unittest.TestCase):
    def setUp(self):
        self.recipe = load_recipe()

    def test_every_patch_carries_its_recorded_sha256(self):
        for entry in self.recipe["series"]:
            with self.subTest(patch=entry["basename"]):
                self.assertEqual(hashlib.sha256((PATCH_DIR / entry["basename"]).read_bytes()).hexdigest(),
                                 entry["sha256"])
        for entry in self.recipe["candidates"]:
            with self.subTest(candidate=entry["basename"]):
                self.assertEqual(hashlib.sha256((CANDIDATE_DIR / entry["basename"]).read_bytes()).hexdigest(),
                                 entry["sha256"])

    def test_patch_directories_hold_exactly_the_recipe(self):
        self.assertEqual(sorted(path.name for path in PATCH_DIR.iterdir()),
                         sorted(entry["basename"] for entry in self.recipe["series"]))
        self.assertEqual(sorted(path.name for path in CANDIDATE_DIR.iterdir()),
                         sorted(entry["basename"] for entry in self.recipe["candidates"]))

    def test_catalog_patch_list_equals_the_admitted_series(self):
        catalog = json.loads((RESOURCES_ROOT / "catalog" / "gateway.json").read_text())["gateway"]
        self.assertEqual(catalog["patches"], admitted_names(self.recipe))
        self.assertEqual(catalog["cliproxyapi_baseline"], self.recipe["upstream"]["version"])

    def test_candidates_are_never_admitted_or_applied(self):
        series = {entry["basename"] for entry in self.recipe["series"]}
        for entry in self.recipe["candidates"]:
            self.assertIs(entry["admitted"], False)
            self.assertNotIn(entry["basename"], series)


class BuildRecordTests(unittest.TestCase):
    def setUp(self):
        self.recipe = load_recipe()

    def test_upstream_identity_is_consistent(self):
        upstream, source = self.recipe["upstream"], self.recipe["source"]
        self.assertEqual(upstream["tag"], "v" + upstream["version"])
        self.assertEqual(source["archive_url"], f"{upstream['repository']}/archive/{upstream['commit']}.tar.gz")
        self.assertIn(upstream["commit"], source["archive_file"])

    def test_ldflags_carry_the_exact_upstream_identity(self):
        upstream = self.recipe["upstream"]
        ldflags = " ".join(self.recipe["build"]["ldflags"])
        # The version reaches provider User-Agents, so it is exactly upstream's.
        self.assertEqual(ldflags, "-s -w -buildid= "
                         f"-X main.Version={upstream['version']} -X main.Commit={upstream['commit']} "
                         "-X main.BuildDate=1970-01-01T00:00:00Z")
        self.assertEqual(self.recipe["build"]["flags"], ["-trimpath", "-buildvcs=false", "-mod=vendor"])
        self.assertEqual(self.recipe["build"]["env"], {"CGO_ENABLED": "0"})

    def test_targets_ship_four_and_compile_a_windows_canary(self):
        targets = {target["name"]: target for target in self.recipe["targets"]}
        self.assertEqual({name for name, target in targets.items() if target["shipped"]},
                         {"linux-amd64", "linux-arm64", "darwin-arm64", "darwin-amd64"})
        self.assertEqual({name for name, target in targets.items() if not target["shipped"]}, {"windows-amd64"})
        for name, target in targets.items():
            self.assertEqual(name, f"{target['goos']}-{target['goarch']}")

    def test_toolchain_archives_name_the_pinned_version(self):
        toolchain = self.recipe["toolchain"]
        for host, archive in toolchain["archives"].items():
            with self.subTest(host=host):
                self.assertEqual(archive["file"], f"go{toolchain['version']}.{host}.tar.gz")
        # The recipe's Go must satisfy the upstream go directive (go 1.26.0).
        self.assertGreaterEqual(tuple(int(part) for part in toolchain["version"].split(".")), (1, 26, 0))


class GateRecordTests(unittest.TestCase):
    def setUp(self):
        self.recipe = load_recipe()
        self.gates = {gate["name"]: gate for gate in self.recipe["gates"]}

    def test_anchored_selections_equal_their_expected_tests(self):
        for gate in self.recipe["gates"]:
            match = re.fullmatch(r"\^\((.*)\)\$", gate.get("run", ""))
            if match:
                with self.subTest(gate=gate["name"]):
                    self.assertEqual(match.group(1).split("|"), gate["expected_tests"])

    def test_every_gate_names_tests_it_can_select(self):
        for gate in self.recipe["gates"]:
            with self.subTest(gate=gate["name"]):
                if gate.get("run"):
                    self.assertTrue(gate["expected_tests"], "a run selection needs its expected tests")
                    run = re.compile(gate["run"])
                    for name in gate["expected_tests"]:
                        self.assertTrue(run.search(name), name)
                if gate.get("skip"):
                    skip = re.compile(gate["skip"])
                    self.assertFalse([name for name in gate["expected_tests"] if skip.search(name)])

    def test_race_gates_are_linux_cgo_gates_with_counts(self):
        for gate in self.recipe["gates"]:
            with self.subTest(gate=gate["name"]):
                if gate["race"]:
                    self.assertEqual(gate["platforms"], "linux")
                    self.assertEqual(gate["count"], 100)
                else:
                    self.assertEqual(gate["platforms"], "portable")
                    self.assertIsNone(gate["count"])

    def test_patch_owned_gate_tests_exist_in_the_admitted_patches(self):
        added = "\n".join("\n".join(added_lines((PATCH_DIR / name).read_text(), tests=True))
                          for name in admitted_names(self.recipe))
        for gate in self.recipe["gates"]:
            for name in gate["expected_tests"]:
                if name.startswith(("TestCM053", "TestCM055", "TestManagementAllowlist_", "TestManagementEnvOnly_")):
                    with self.subTest(gate=gate["name"], test=name):
                        self.assertIn(f"func {name}(", added)

    def test_source_pins_name_patched_or_upstream_files(self):
        pins = {pin["name"]: pin for pin in self.recipe["source_pins"]}
        self.assertEqual(set(pins), {"substitution-log-format", "credential-save-format"})
        patch = (PATCH_DIR / "cli-proxy-api-credential-save-report.patch").read_text()
        self.assertIn(pins["credential-save-format"]["text"], "\n".join(added_lines(patch)))


class MarkerTests(unittest.TestCase):
    def test_each_marker_is_added_by_its_patch_and_not_removed(self):
        recipe = load_recipe()
        admitted = set(admitted_names(recipe))
        for marker in recipe["markers"]:
            with self.subTest(patch=marker["patch"]):
                self.assertIn(marker["patch"], admitted)
                text = (PATCH_DIR / marker["patch"]).read_text()
                self.assertTrue(any(marker["text"] in line for line in added_lines(text)))
                removed = [line for line in text.splitlines() if line.startswith("-") and not line.startswith("---")]
                self.assertFalse(any(marker["text"] in line for line in removed))


class NixDerivesFromRecipeTests(unittest.TestCase):
    def setUp(self):
        self.nix = (NIX_DIR / "gateway.nix").read_text()
        self.code = "\n".join(line for line in self.nix.splitlines() if not line.lstrip().startswith("#"))

    def test_gateway_nix_reads_the_recipe_and_runs_the_build_tool(self):
        self.assertTrue(self.code.lstrip().startswith("{ pkgs }:"))
        self.assertIn("recipe = ../gateway/UPSTREAM.json;", self.code)
        self.assertIn("buildTool = ../tools/build.py;", self.code)
        self.assertIn("upstream = lib.importJSON recipe;", self.code)
        for step in ("gatewayBuild fetch vendor", 'gatewayBuild apply "\'\'${patchArgs[@]}"',
                     "gatewayBuild build --target", "gatewayBuild inspect record --target",
                     "gatewayBuild gates --gates", "gateway repro"):
            self.assertIn(step, self.code)
        self.assertIn('--inputs "$buildRoot/cm-inputs" --offline', self.code)

    def test_no_second_patch_list_or_pin(self):
        # Patch names, hashes, the commit and the gates live only in the recipe.
        self.assertNotIn(".patch", self.code)
        self.assertIsNone(re.search(r"sha256-[A-Za-z0-9+/]{43}=", self.code))
        self.assertIsNone(re.search(r"[0-9a-f]{40}", self.code))
        self.assertNotIn("go test", self.code)
        self.assertNotIn("buildGoModule", self.code)
        self.assertNotIn("overrideAttrs", self.code)

    def test_fixed_output_inputs_come_from_the_recipe(self):
        self.assertIn("rev = upstream.upstream.commit;", self.code)
        self.assertIn("hash = upstream.source.nix_hash;", self.code)
        self.assertIn("outputHash = upstream.vendor.nix_hash;", self.code)
        self.assertIn('outputHashMode = "recursive";', self.code)
        self.assertIn("url = upstream.toolchain.url_prefix + toolchain.file;", self.code)
        self.assertIn("inherit (toolchain) sha256;", self.code)
        # The vendored-modules store name is what the gateway harness pins.
        self.assertIn('name = "cli-proxy-api-${version}-go-modules";', self.code)

    def test_patches_and_contract_derive_from_the_admitted_series(self):
        self.assertIn("admitted = builtins.filter (entry: entry.admitted) upstream.series;", self.code)
        self.assertIn('builtins.hashFile "sha256" path == entry.sha256', self.code)
        self.assertIn("gatewayPatches = map patchFile admitted;", self.code)
        self.assertIn('patches = map (entry: "${entry.basename}@${entry.sha256}") admitted;', self.code)
        self.assertIn("patches = gatewayPatches;", self.code)

    def test_no_external_gateway_base(self):
        if not FLAKE_NIX.is_file():
            self.skipTest("boundary: flake.nix is absent in the sandbox tree")
        flake = FLAKE_NIX.read_text()
        self.assertNotIn("llm-agents", flake)
        lock = json.loads(FLAKE_LOCK.read_text())
        self.assertEqual(lock["nodes"][lock["root"]]["inputs"], {"nixpkgs": "nixpkgs"})
        for literal in ("gateway-gates = cliProxyApi.gates.portable;", "gateway-race = cliProxyApi.gates.race;",
                        "gateway-inspect = cliProxyApi.inspect;", "gateway-repro = cliProxyApi.repro;",
                        "gateway-windows-canary = cliProxyApi.targets.windows-amd64;",
                        'cliProxyApiBin = "${cliProxyApi}/bin/cli-proxy-api";'):
            self.assertIn(literal, flake)


class UpstreamReviewTests(unittest.TestCase):
    """The upstream fixes after the pinned release are reviewed and recorded.

    gateway/upstream-review.json lists every reviewed fix with its disposition:
    "backport" (an admitted patch now), "rebase" (carried by the rebase onto the
    next upstream line) or "not-applicable". A re-pin makes the review stale.
    """

    DISPOSITIONS = {"backport", "rebase", "not-applicable"}
    FIX_KEYS = {"commits", "release", "subject", "disposition", "reason"}

    def setUp(self):
        self.review = strict_json.load(UPSTREAM_REVIEW)
        self.recipe = load_recipe()

    def test_closed_shape(self):
        self.assertEqual(set(self.review), {"format", "upstream", "admission", "rendered_configuration",
                                            "dependencies", "vulncheck", "fixes"})
        self.assertEqual(self.review["format"], 1)
        upstream = self.review["upstream"]
        self.assertEqual(set(upstream), {"repository", "pinned", "reviewed_through", "reviewed_on",
                                         "releases", "commits"})
        self.assertRegex(upstream["reviewed_on"], r"^\d{4}-\d{2}-\d{2}$")
        for point in ("pinned", "reviewed_through"):
            self.assertEqual(set(upstream[point]), {"version", "commit"})
            self.assertRegex(upstream[point]["commit"], r"^[0-9a-f]{40}$")
        self.assertTrue(self.review["admission"].strip())
        self.assertTrue(all(isinstance(fact, str) and fact.strip() for fact in self.review["rendered_configuration"]))
        self.assertEqual(set(self.review["vulncheck"]), {"tool", "mode", "status", "reason", "run"})
        self.assertEqual((self.review["vulncheck"]["tool"], self.review["vulncheck"]["mode"]), ("govulncheck", "binary"))

    def test_review_starts_at_the_pinned_upstream(self):
        upstream, pinned = self.review["upstream"], self.recipe["upstream"]
        self.assertEqual(upstream["repository"], pinned["repository"])
        self.assertEqual(upstream["pinned"], {"version": pinned["version"], "commit": pinned["commit"]})

        def release(version):
            return tuple(int(part) for part in version.split("."))

        self.assertGreater(release(upstream["reviewed_through"]["version"]), release(pinned["version"]))

    def test_every_fix_has_one_disposition_and_a_reason(self):
        admitted = {entry["basename"] for entry in self.recipe["series"] if entry["admitted"]}
        seen = set()
        for fix in self.review["fixes"]:
            with self.subTest(commits=fix.get("commits")):
                self.assertLessEqual(self.FIX_KEYS, set(fix))
                self.assertLessEqual(set(fix) - self.FIX_KEYS, {"patch", "covered_by"})
                self.assertIn(fix["disposition"], self.DISPOSITIONS)
                for field in ("release", "subject", "reason"):
                    self.assertTrue(fix[field].strip())
                self.assertTrue(fix["commits"])
                for commit in fix["commits"]:
                    self.assertRegex(commit, r"^[0-9a-f]{8,40}$")
                    self.assertNotIn(commit, seen)
                    seen.add(commit)
                # A backport is an admitted patch; a covered fix names the local patch.
                if fix["disposition"] == "backport":
                    self.assertIn(fix.get("patch"), admitted)
                else:
                    self.assertNotIn("patch", fix)
                if "covered_by" in fix:
                    self.assertIn(fix["covered_by"], admitted)


class ContractTests(unittest.TestCase):
    def test_contract_document_and_bytes(self):
        tool = load_build_tool()
        recipe = load_recipe()
        admitted = [entry for entry in recipe["series"] if entry["admitted"]]
        contract = tool.contract_document(recipe, admitted)
        self.assertEqual(contract, {
            "version": 1, "upstream_version": recipe["upstream"]["version"],
            "patches": [f"{entry['basename']}@{entry['sha256']}" for entry in admitted]})
        # builtins.toJSON: sorted keys, no whitespace, no trailing newline.
        raw = tool.contract_bytes(contract)
        self.assertTrue(raw.startswith(b'{"patches":["cli-proxy-api-'))
        self.assertTrue(raw.endswith(b'"upstream_version":"' + recipe["upstream"]["version"].encode() + b'","version":1}'))
        self.assertNotIn(b" ", raw)
        self.assertEqual(json.loads(raw), contract)


if __name__ == "__main__":
    unittest.main()
