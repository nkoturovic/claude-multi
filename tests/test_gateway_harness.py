"""Gateway harness self-tests (always run) and isolated, real-gateway smoke."""
import copy
import io
import json
import os
import stat
import tempfile
import unittest
from collections import Counter
from functools import lru_cache
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from claude_multi import catalog, render, state
from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT, served_selectors, uses_shipped_catalog
from _layout import REPO_ROOT, RESOURCES_ROOT
import _gateway_harness as harness
from _layout import NIX_DIR
from _tripwire import PORTS
import check_gateway_races

MODULE = "tests.test_gateway_harness"


def tearDownModule():
    # Not atexit: unittest propagates write/validation failures as test errors.
    harness.EVIDENCE.finalize_module(MODULE)


@uses_shipped_catalog
class PatchCoverageTests(unittest.TestCase):
    def test_every_manifest_patch_has_resolvable_rows(self):
        manifest = json.loads((RESOURCES_ROOT / "catalog/gateway.json").read_text())["gateway"]["patches"]
        self.assertEqual(set(manifest), set(harness.PATCH_ROWS))
        self.assertEqual(len(manifest), len(set(manifest)))
        import check_gateway_patch_revert as tool
        for patch in manifest:
            self.assertIn(patch, tool.omission_closure(patch, manifest, harness.PATCH_DEPENDENCIES))
        for patch, names in harness.PATCH_ROWS.items():
            self.assertTrue(names, patch)
            self.assertEqual(len(names), len(set(names)), patch)
            for name in names:
                loader = unittest.TestLoader()
                suite = loader.loadTestsFromName(name)
                self.assertFalse(loader.errors, name)
                self.assertEqual(suite.countTestCases(), 1, name)


class AdmittedPatchSelectionTests(unittest.TestCase):
    def test_every_new_row_requires_explicit_selection_before_gateway_creation(self):
        from tests.test_gateway_patches import PatchOmissionProbeTests
        for name in unittest.defaultTestLoader.getTestCaseNames(PatchOmissionProbeTests):
            for required in (False, True):
                with self.subTest(row=name, required=required), mock.patch.dict(os.environ, {}, clear=True), \
                        mock.patch("tests.test_gateway_patches.gateway_binary") as create:
                    if required:
                        os.environ[harness.REQUIRE_ENV] = "1"
                    with self.assertRaises(AssertionError if required else unittest.SkipTest):
                        getattr(PatchOmissionProbeTests(name), name)()
                    create.assert_not_called()


class ListenerHelperTests(unittest.TestCase):
    def test_listener_address_decoding_and_loopback_scope(self):
        for address in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(harness.loopback_listener(address))
        for address in ("0.0.0.0", "::", "127.0.0.2", "192.0.2.1"):
            self.assertFalse(harness.loopback_listener(address))
        table = "header\n0: 0100007F:C001 00000000:0000 0A\n1: 00000000:C002 00000000:0000 07\n"
        self.assertEqual(harness.socket_rows(table), [("127.0.0.1", 49153)])
        self.assertEqual(len(harness.socket_rows(table, listening=False)), 2)
        namespace = {}
        source = harness.listener_probe_source().split("port = int(sys.argv[1])")[0]
        exec(source, namespace)
        self.assertEqual(namespace["socket_rows"](table), harness.socket_rows(table))


class RevertToolTests(unittest.TestCase):
    def test_outcomes_include_subtest_and_fixture_failures(self):
        import check_gateway_patch_revert as tool

        class Rows(unittest.TestCase):
            def test_pass(self):
                pass

            def test_subtest(self):
                with self.subTest(cell="synthetic"):
                    self.fail("deliberate")

        class SetupFails(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise AssertionError("deliberate setup error")

            def test_row(self):
                pass

        # The inner suite must not run this module's real evidence finalizer.
        Rows.__module__ = SetupFails.__module__ = "gwtest_revert_synthetic"
        cases = [Rows("test_pass"), Rows("test_subtest"), SetupFails("test_row")]
        names = [case.id() for case in cases]
        with mock.patch.dict(harness.PATCH_ROWS, {"synthetic.patch": tuple(names)}, clear=True), \
                mock.patch.object(harness.EVIDENCE, "finalize_module", side_effect=AssertionError("premature finalization")) as finalize:
            result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=tool.RowResult).run(unittest.TestSuite(cases))
            finalize.assert_not_called()
            self.assertEqual(result.outcomes, dict(zip(names, ("ok", "FAIL", "ERROR"))))
            summary = tool.verdict(result.outcomes, [names[1]])
            self.assertEqual(summary["own_red"], [names[1]])
            self.assertEqual(summary["cross_red"], [names[2]])
            self.assertFalse(summary["incomplete"])
            result.outcomes[names[0]] = "skipped"
            self.assertEqual(tool.verdict(result.outcomes, names)["incomplete"], [names[0]])

    def test_single_omission_expression_preserves_the_other_patches(self):
        import check_gateway_patch_revert as tool
        expression = tool.build_expression(Path("/fixture/worktree"), ("synthetic.patch",))
        self.assertIn('[ "synthetic.patch" ])) old.patches', expression)
        self.assertIn("doCheck = false", expression)
        # A partial series installs with record alone; the replaced text must
        # be the shipped install step, or the replacement does nothing.
        self.assertIn(tool.DIAGNOSTIC_INSTALL_PHASE, expression)
        self.assertIn(tool.SHIPPED_INSTALL_STEP + " --target", (NIX_DIR / "gateway.nix").read_text())
        self.assertIn("gateway-startup-probe.nix", expression)
        self.assertNotIn("go ", expression)
        # An omission may move later hunks by the omitted lines (the
        # replacement is asserted in Nix); the full-series control never.
        self.assertIn(tool.DIAGNOSTIC_PATCH_PHASE, expression)
        self.assertIn("--omission-offsets", tool.DIAGNOSTIC_PATCH_PHASE)
        self.assertIn("assert phase != old.patchPhase", tool.DIAGNOSTIC_PATCH_PHASE)
        self.assertIn(tool.SHIPPED_APPLY_STEP + '"', (NIX_DIR / "gateway.nix").read_text())
        control = tool.build_expression(Path("/fixture"), ())
        self.assertIn('[  ])) old.patches', control)
        self.assertNotIn("omission-offsets", control)

    def test_moved_hunks_come_from_the_diagnostic_build_record(self):
        import check_gateway_patch_revert as tool
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "share/cli-proxy-api/BUILD.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"admitted_series": True, "series": []}))
            self.assertEqual(tool.moved_hunks(directory), {})
            moved = {"b.patch": ["Hunk #2 succeeded at 252 (offset -20 lines)."]}
            record.write_text(json.dumps({"admitted_series": False, "moved_hunks": moved}))
            self.assertEqual(tool.moved_hunks(directory), moved)

    def test_provenance_labels_clean_and_dirty_trees(self):
        import check_gateway_patch_revert as tool
        for status in ("", " M tracked.py\nA  staged.py\n?? untracked.py\n"):
            with self.subTest(status=status), mock.patch.object(tool.subprocess, "check_output",
                    side_effect=["fixture-head\n", status]) as git:
                facts = tool.git_provenance(Path("/fixture"))
                self.assertEqual(facts, {"head": "fixture-head", "dirty": bool(status),
                                        "status_porcelain": status.splitlines()})
                self.assertEqual(git.call_args_list, [
                    mock.call(["git", "-C", "/fixture", "rev-parse", "HEAD"], text=True),
                    mock.call(["git", "-C", "/fixture", "status", "--porcelain", "--untracked-files=all"], text=True),
                ])

    def test_dependency_closure_is_declared_transitive_and_acyclic(self):
        import check_gateway_patch_revert as tool
        manifest = ("a", "b", "c", "unrelated")
        dependencies = {"b": {"a": {}}, "c": {"b": {}}}
        self.assertEqual(tool.omission_closure("a", manifest, dependencies), ("a", "b", "c"))
        self.assertEqual(tool.omission_closure("b", manifest, dependencies), ("b", "c"))
        self.assertEqual(tool.omission_closure("unrelated", manifest, dependencies), ("unrelated",))
        with self.assertRaisesRegex(ValueError, "outside the manifest"):
            tool.omission_closure("a", manifest, {"b": {"absent": {}}})
        with self.assertRaisesRegex(ValueError, "cyclic"):
            tool.omission_closure("a", manifest, {"b": {"a": {}}, "a": {"b": {}}})

    def test_dependency_cross_red_is_never_own_evidence(self):
        import check_gateway_patch_revert as tool
        with mock.patch.dict(harness.PATCH_ROWS, {"a": ("row.a",), "b": ("row.b",), "c": ("row.c",)}, clear=True):
            variant = {"omitted": ["a", "b"], "outcomes": {"row.a": "FAIL", "row.b": "FAIL", "row.c": "FAIL"}}
            control = {"outcomes": {"row.a": "ok", "row.b": "FAIL", "row.c": "ok"}}
            result = tool.attribute(variant, "a", control)
            self.assertEqual(result["own_red"], ["row.a"])
            self.assertEqual(result["expected_dependency_cross_red"], ["row.b"])
            self.assertEqual(result["cross_red"], ["row.c"])
            control["outcomes"]["row.a"] = "FAIL"
            result = tool.attribute(variant, "a", control)
            self.assertFalse(result["own_red"])
            self.assertEqual(result["unattributed_own_red"], ["row.a"])

    def test_globally_red_variant_never_proves_coverage(self):
        # An omission whose every row went red (broken build,
        # harness trouble) is not discriminating, even with own rows red.
        import check_gateway_patch_revert as tool
        rows = {"a": ("row.a",), "b": ("row.b",), "c": ("row.c",)}
        with mock.patch.dict(harness.PATCH_ROWS, rows, clear=True):
            red = tool.attribute({"omitted": ["a"], "outcomes": dict.fromkeys(("row.a", "row.b", "row.c"), "FAIL")},
                                 "a", None)
            self.assertEqual(red["own_red"], ["row.a"])
            self.assertEqual(red["cross_red"], ["row.b", "row.c"])
            self.assertFalse(tool.proof_passed(red, 1))
            clean = tool.attribute({"omitted": ["a"], "outcomes": {"row.a": "FAIL", "row.b": "ok", "row.c": "ok"}},
                                   "a", None)
            self.assertTrue(tool.proof_passed(clean, 1))
            self.assertFalse(tool.proof_passed(clean, 0))
            # A declared dependent's rows stay allowed cross-red.
            dependent = tool.attribute({"omitted": ["a", "b"], "outcomes": {"row.a": "FAIL", "row.b": "FAIL",
                                                                            "row.c": "ok"}},
                                       "a", {"outcomes": {"row.a": "ok", "row.b": "FAIL", "row.c": "ok"}})
            self.assertTrue(tool.proof_passed(dependent, 1))

    def test_required_gateway_boundary_is_blocked_not_red(self):
        import check_gateway_patch_revert as tool

        class Rows(unittest.TestCase):
            def test_boundary(self):
                with mock.patch.dict(os.environ, {harness.REQUIRE_ENV: "1"}):
                    harness.boundary("BOUNDARY: synthetic isolation unavailable")

            def test_subtest_boundary(self):
                with self.subTest(cell="synthetic"), mock.patch.dict(os.environ, {harness.REQUIRE_ENV: "1"}):
                    harness.boundary("BOUNDARY: synthetic disk full")

            def test_real_failure(self):
                self.fail("deliberate")

        class SetupBoundary(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise AssertionError(harness.REQUIRE_ENV + "=1: BOUNDARY: synthetic startup probe")

            def test_row(self):
                pass

        Rows.__module__ = SetupBoundary.__module__ = "gwtest_revert_synthetic"
        cases = [Rows("test_boundary"), Rows("test_subtest_boundary"), Rows("test_real_failure"),
                 SetupBoundary("test_row")]
        names = [case.id() for case in cases]
        with mock.patch.dict(harness.PATCH_ROWS, {"synthetic.patch": tuple(names)}, clear=True), \
                mock.patch.object(harness.EVIDENCE, "finalize_module"):
            result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=tool.RowResult).run(unittest.TestSuite(cases))
            self.assertEqual(result.outcomes, dict(zip(names, ("blocked", "blocked", "FAIL", "blocked"))))
            summary = tool.verdict(result.outcomes, names)
            self.assertEqual(summary["own_red"], [names[2]])
            self.assertEqual(sorted(summary["incomplete"]), sorted([names[0], names[1], names[3]]))


class VendorInputTests(unittest.TestCase):
    """Vendor pinning is judged on what a diagnostic derivation consumes."""
    PINNED = harness.PINNED_GO_MODULES

    def show(self, env, inputs, fod_outputs):
        document = {"version": 4, "derivations": {"abc-gwtest-probe.drv": {
            "env": env, "inputs": {"drvs": {name: {} for name in inputs}, "srcs": []}}}}
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[:3] == ["nix", "derivation", "show"]:
                return mock.Mock(returncode=0, stdout=json.dumps(document))
            return mock.Mock(returncode=0, stdout="\n".join(fod_outputs[name] for name in argv[3:]) + "\n")
        return run, calls

    def test_consumed_vendor_is_pinned_only_when_env_and_inputs_agree(self):
        pinned_drv = "f20-cli-proxy-api-7.3.15-go-modules.drv"
        forked_drv = "fx5-gwtest-probe-7.3.15-go-modules.drv"
        outputs = {"/nix/store/" + pinned_drv: self.PINNED,
                   "/nix/store/" + forked_drv: "/nix/store/ljg-gwtest-probe-7.3.15-go-modules"}
        run, calls = self.show({"goModules": self.PINNED}, [pinned_drv, "gw-cli-proxy-api-7.3.15.drv"], outputs)
        consumed = harness.consumed_vendor("/nix/store/out-gwtest-probe", run=run)
        self.assertTrue(harness.vendor_pinned(consumed))
        self.assertEqual(calls[0], ["nix", "derivation", "show", "--offline", "/nix/store/out-gwtest-probe"])
        self.assertEqual(calls[1], ["nix-store", "--query", "--outputs", "/nix/store/" + pinned_drv])
        # The attribute can echo the pin while the build fetches its own FOD.
        run, _ = self.show({"goModules": self.PINNED}, [pinned_drv, forked_drv], outputs)
        self.assertFalse(harness.vendor_pinned(harness.consumed_vendor("x", run=run)))
        run, _ = self.show({"goModules": outputs["/nix/store/" + forked_drv]}, [forked_drv], outputs)
        self.assertFalse(harness.vendor_pinned(harness.consumed_vendor("x", run=run)))
        run, _ = self.show({}, [], outputs)
        self.assertFalse(harness.vendor_pinned(harness.consumed_vendor("x", run=run)))

    def test_every_diagnostic_expression_asserts_consumed_vendor(self):
        tests = REPO_ROOT / "tests"
        for name, helper in (("gateway-startup-probe.nix", "./vendor-inputs.nix"),
                             ("gateway-race.nix", "./vendor-inputs.nix"),
                             ("gateway-retry-after.nix", "./vendor-inputs.nix"),
                             ("gateway_go/lifecycle.nix", "../vendor-inputs.nix")):
            text = (tests / name).read_text()
            self.assertIn(f"assert import {helper} diagnostic == [ cliProxyApi.goModules.drvPath ];", text, name)
            self.assertNotIn("diagnostic.goModules", text, name)


class RaceToolSelfTests(check_gateway_races.SelfTests):
    """The race tool's pure verdict/evidence self-tests, in the ordinary suite."""


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.bundle = catalog.load_catalog(FIXTURE_ROOT)
        self.egress, self.port = harness.ports()
        self.document, self.aliases, self.info = harness.build_document(
            self.bundle, self.egress, self.port, Path("/fixture/home"))

    def test_templates_are_structural_and_missing_template_fails(self):
        selected = harness.templates(self.bundle)
        self.assertEqual(set(selected), {"codex", "header", "bearer", "compat"})
        for name in ("header", "bearer"):
            self.assertEqual(selected[name][0]["transport"]["auth"]["kind"], name)
        with mock.patch.dict(self.bundle.docs, {"providers": {"providers": {}}}):
            with self.assertRaisesRegex(AssertionError, "fixture lacks template"):
                harness.templates(self.bundle)

    def test_render_inputs_are_unchanged_and_every_url_is_local(self):
        before = copy.deepcopy(self.bundle.docs)
        harness.build_document(self.bundle, self.egress, self.port, Path("/fixture/home"))
        self.assertEqual(before, self.bundle.docs)
        for section in harness.model_sections(self.document):
            parts = urlsplit(section["base-url"])
            if section.get("name") == render.SENTINEL_NAME:
                self.assertEqual(section["base-url"], render.SENTINEL_BASE_URL)
            else:
                self.assertEqual((parts.scheme, parts.hostname, parts.port), ("http", "127.0.0.1", self.egress))
                self.assertTrue(parts.path.startswith("/gwtest-"))
        self.assertEqual(self.document["port"], self.port)
        self.assertEqual(self.document["host"], "127.0.0.1")
        self.assertFalse({self.egress, self.port} & PORTS)
        self.assertEqual(self.document["request-retry"], 0)
        self.assertEqual(self.document["max-retry-credentials"], 1)
        self.assertTrue(self.document["disable-cooling"])
        for section in self.document["claude-api-key"] + self.document["codex-api-key"]:
            self.assertIn("dummy", section["api-key"])
        self.assertFalse(self.document["passthrough-headers"])

    def test_contract_inventory_aliases_and_continuity(self):
        expected = {(adapter, name) for adapter, contracts in render.ADAPTER_PAYLOAD_CONTRACTS.items()
                    for name, contract in contracts.items() if contract["kind"] == "override"}
        self.assertEqual({(i.adapter, i.contract) for i in self.aliases.values() if i.contract}, expected)
        self.assertEqual(render.rendered_selectors(self.document), frozenset(self.aliases))
        self.assertEqual(set(self.info["continuity_rendered"]), {a for a, i in self.aliases.items() if i.origin == "continuity"})
        self.assertEqual(len(self.info["continuity_rendered"]), 3)
        self.assertFalse(self.info["notices"])
        self.assertFalse(set(self.aliases) & served_selectors(FIXTURE_ROOT, include_continuity=True))
        self.assertTrue(all(not a.startswith(render.SENTINEL_PREFIX) for a in self.aliases))
        self.assertTrue(render.document_sentinel(self.document).startswith(render.SENTINEL_PREFIX))
        # Exactly one override per provider/declared contract with aliases,
        # NOT one per contract globally (several providers share contracts).
        pairs = set()
        seen = Counter()
        for rule in self.document["payload"]["override"][:-1]:
            identities = set()
            for model in rule["models"]:
                alias = model["name"]
                info = self.aliases[alias]
                contract = render.ADAPTER_PAYLOAD_CONTRACTS[info.adapter][info.contract]
                self.assertEqual(model["protocol"], contract["protocol"])
                self.assertEqual(rule["params"], contract["params"])
                identities.add((info.provider, info.contract))
                seen[alias] += 1
            self.assertEqual(len(identities), 1)
            identity = identities.pop()
            self.assertNotIn(identity, pairs)
            pairs.add(identity)
        self.assertEqual(pairs, {(i.provider, i.contract) for i in self.aliases.values() if i.contract})
        self.assertEqual(seen, Counter({a: 1 for a, i in self.aliases.items() if i.contract}))
        filters = self.document["payload"]["filter"]
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0]["params"], ["thinking"])
        self.assertEqual({m["name"] for m in filters[0]["models"]},
                         {a for a, i in self.aliases.items() if i.provider == "gwtest-ccf"})

    def test_all_six_surgeries(self):
        document, aliases = self.document, self.aliases
        fields = ("name", "alias", "force-mapping")
        self.assertEqual([{k: m[k] for k in fields} for m in document["codex-api-key"][0]["models"]],
                         [{k: m[k] for k in fields} for m in document["oauth-model-alias"]["codex"]])
        models = {m["alias"]: m for section in harness.model_sections(document) for m in section["models"]}
        wires = Counter(i.wire for i in aliases.values())
        for alias, info in aliases.items():
            self.assertTrue(models[alias]["force-mapping"])
            if info.levels:
                self.assertEqual(models[alias]["thinking"], {"levels": list(info.levels)})
                self.assertEqual(wires[info.wire], 1)
            else:
                self.assertNotIn("thinking", models[alias])
        self.assertEqual(document["payload"]["override"][-1], {
            "models": [{"name": "gwtest-compat-override", "protocol": "openai"}], "params": {"reasoning_effort": "low"}})
        self.assertTrue(models["gwtest-compat-iscompat"]["is-compat"])
        header_section = next(s for s in document["claude-api-key"] if urlsplit(s["base-url"]).path == "/gwtest-cch")
        self.assertTrue(header_section["headers"]["Authorization"].startswith("Bearer dummy"))
        self.assertNotIn("claude-header-defaults", document)
        variant, variant_aliases, _ = harness.build_document(self.bundle, self.egress, self.port, Path("/fixture/home"), "ua")
        major, minor, _ = map(int, self.bundle.docs["native-contract"]["verified"][0]["version"].split("."))
        self.assertEqual(variant.pop("claude-header-defaults"), {"user-agent": f"claude-cli/{major}.{minor + 1}.0 (external, cli)"})
        self.assertEqual(variant, document)
        self.assertEqual(variant_aliases, aliases)


class GateAndEvidenceTests(unittest.TestCase):
    def test_boundary_require_matrix(self):
        for value, exception in ((None, unittest.SkipTest), ("0", unittest.SkipTest), ("1", AssertionError), ("bad", RuntimeError)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(harness.REQUIRE_ENV, None)
                if value is not None:
                    os.environ[harness.REQUIRE_ENV] = value
                with self.assertRaises(exception):
                    harness.boundary("BOUNDARY: synthetic self-test")

    def test_env_only_patch_row_requires_explicit_manifest_build(self):
        from tests.test_gateway_patches import ManagementEnvOnlyTests
        for require, exception in (("", unittest.SkipTest), ("1", AssertionError)):
            with self.subTest(require=require), mock.patch.dict(os.environ,
                    {harness.BINARY_ENV: "", harness.REQUIRE_ENV: require}), \
                    mock.patch.object(harness, "GatewayHarness") as gateway:
                with self.assertRaisesRegex(exception, "BOUNDARY: ME1 needs .* set to the manifest build"):
                    ManagementEnvOnlyTests("test_config_secret_cannot_enable_management").test_config_secret_cannot_enable_management()
                gateway.assert_not_called()

    def test_ports_and_module_inventory(self):
        for _ in range(20):
            values = harness.ports()
            self.assertEqual(len(set(values)), 2)
            self.assertFalse(set(values) & PORTS)
            self.assertTrue(all(61000 <= p <= 65535 for p in values))
        # Host-only and separately timed lanes are explicit, not an escape
        # hatch for silently dropping future gateway modules from the check.
        self.assertEqual(harness.GATEWAY_CHECK_EXCLUSIONS, {
            "test_gateway_seam", "test_gateway_auth_tools", "test_gateway_hint_client",
            "test_gateway_regressions", "test_gateway_retry_after", "test_gateway_keyed_compat",
            "test_gateway_lifecycle", "test_gateway_doctor", "test_gateway_service",
        })
        modules = {"tests." + p.stem for p in (REPO_ROOT / "tests").glob("test_gateway_*.py")
                   if p.stem not in harness.GATEWAY_CHECK_EXCLUSIONS}
        self.assertEqual(modules | {"tests.test_proxy"}, set(harness.GATEWAY_CHECK_MODULES))
        self.assertEqual(len(harness.GATEWAY_CHECK_MODULES), len(set(harness.GATEWAY_CHECK_MODULES)))

    def test_binary_discovery_cached_once(self):
        # A separate cache: other matrix modules may already have started the
        # singleton. Pure tests must neither seed nor reset that runner cache.
        self.assertEqual(harness.find_gateway_binary.cache_parameters()["maxsize"], 1)
        cached = lru_cache(maxsize=1)(harness.find_gateway_binary.__wrapped__)
        with mock.patch.object(harness, "_gateway_binary", return_value=("/nix/store/fixture/bin/cli-proxy-api", "7.3.15")) as find:
            cached()
            cached()
            find.assert_called_once_with()

    def test_evidence_rejects_sensitive_nested_or_nonfinite_values(self):
        for value in (FIXTURE_GATEWAY_TOKEN, "dummy-gwtest-secret", "gwtesthint-secret", {}, [[1]], b"bytes", float("nan")):
            with self.subTest(kind=type(value).__name__):
                for wrapped in (value, [value]):
                    with self.assertRaises((ValueError, TypeError)):
                        harness.Evidence().record("tests.synthetic", "T", {"value": wrapped})
        evidence = harness.Evidence()
        evidence.record("tests.synthetic", "T", {"values": ["safe", 1, 1.5, True, None]})
        for module in ("../escape", "dummy-gwtest", "gwtesthint-secret"):
            with self.assertRaises(ValueError):
                evidence.record(module, "T", {})

    def evidence(self):
        evidence = harness.Evidence()
        evidence.binary = {"realpath": "/nix/store/fixture/bin/cli-proxy-api", "version": "7.3.15"}
        for module, tables in harness.REQUIRED_EVIDENCE.items():
            for table, rows in tables.items():
                for row in rows:
                    if table == "census" and row == "coverage":
                        evidence.record(module, table, {"row": "fixture-line", "line": "fixture-line"})
                        evidence.record(module, table, {"row": row, "lines": ["fixture-line"], "count": 1, "complete": True})
                    else:
                        evidence.record(module, table, {"row": row, "passed": True})
        return evidence

    def test_evidence_private_atomic_and_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "evidence"
            evidence = self.evidence()
            for module in harness.REQUIRED_EVIDENCE:
                evidence.finalize_module(module, directory)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            path = directory / (MODULE + ".json")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            harness.validate_evidence(directory)
            data = json.loads(path.read_text())
            data["tables"]["smoke"].pop()
            state.atomic_write(path, json.dumps(data).encode())
            with self.assertRaisesRegex(AssertionError, "missing required evidence rows"):
                harness.validate_evidence(directory)
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                harness.validate_evidence(directory)

    def test_published_store_copy_is_a_separate_content_check(self):
        # Nix normalizes the gateway check's output to 0555/0444.
        # That public copy has its own check; the private check never relaxes.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "evidence"
            evidence = self.evidence()
            for module in harness.REQUIRED_EVIDENCE:
                evidence.finalize_module(module, directory)
            with self.assertRaisesRegex(AssertionError, "immutable /nix/store"):
                harness.validate_evidence(directory, published=True)
            files = [directory / (module + ".json") for module in harness.REQUIRED_EVIDENCE]
            try:
                for path in files:
                    path.chmod(0o444)
                directory.chmod(0o555)
                with self.assertRaisesRegex(AssertionError, "must be 0700"):
                    harness.validate_evidence(directory)
                store = Path("/nix/store/gwtest-fixture-gateway-tests/evidence")
                with mock.patch.object(Path, "resolve", return_value=store):
                    harness.validate_evidence(directory, published=True)
                    directory.chmod(0o755)
                    with self.assertRaisesRegex(AssertionError, "read-only"):
                        harness.validate_evidence(directory, published=True)
                    directory.chmod(0o700)
                    files[0].chmod(0o600)
                    data = json.loads(files[0].read_text())
                    data["tables"][next(iter(data["tables"]))].pop()
                    files[0].write_text(json.dumps(data))
                    files[0].chmod(0o444)
                    directory.chmod(0o555)
                    with self.assertRaisesRegex(AssertionError, "missing required evidence rows"):
                        harness.validate_evidence(directory, published=True)
            finally:
                directory.chmod(0o700)

    def test_publication_errors_propagate_and_no_evidence_without_opt_in(self):
        evidence = self.evidence()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(harness.EVIDENCE_ENV, None)
            with mock.patch.object(state, "atomic_write") as write:
                evidence.finalize_module(MODULE)
                write.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "evidence"
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
            with self.assertRaises(state.StateError):
                evidence.finalize_module(MODULE, directory)
            directory.chmod(0o700)
            with mock.patch.object(state, "atomic_write", side_effect=OSError("synthetic write failure")):
                with self.assertRaisesRegex(OSError, "synthetic write failure"):
                    evidence.finalize_module(MODULE, directory)
            with self.assertRaisesRegex(AssertionError, "missing launched binary"):
                harness.Evidence().finalize_module(MODULE, directory)

    def test_request_connection_closed_even_when_response_read_fails(self):
        connection = mock.MagicMock()
        connection.getresponse.return_value.read.side_effect = OSError("synthetic read failure")
        with mock.patch.object(harness.probe, "UnixHTTPConnection", return_value=connection):
            with self.assertRaises(OSError):
                harness.unix_request("unused", "GET", "/healthz")
        connection.close.assert_called_once_with()

    def test_request_preserves_mode_with_custom_messages_and_uses_current_nonce(self):
        gateway = harness.GatewayHarness.__new__(harness.GatewayHarness)
        gateway.socket, gateway.port = Path("/unused"), 55123
        gateway.upstream = mock.Mock(unexpected=0, hits=[])
        gateway.process = mock.Mock()
        gateway.process.poll.return_value = None
        body = {"messages": [{"role": "assistant", "content": "gwtest-reasoning-gwtest-case-000000000000"}]}
        before = copy.deepcopy(body)
        with mock.patch.object(harness, "unix_request", return_value=harness.Reply(200, {}, b"{}")) as send:
            reply = gateway.request("fixture", mode="reasoning", body=body)
        payload = json.loads(send.call_args.kwargs["body"])
        self.assertEqual(body, before)
        self.assertEqual(payload["messages"][:-1], body["messages"])
        self.assertEqual(payload["messages"][-1]["content"], reply.nonce + " gwtest-mode=reasoning")
        old = mock.Mock(nonce="gwtest-case-000000000000")
        current = mock.Mock(nonce="gwtest-case-111111111111")
        gateway.upstream.hits = [old, current]
        raw = b'{"messages":["gwtest-case-000000000000","gwtest-case-111111111111"]}'
        with mock.patch.object(harness, "unix_request", return_value=harness.Reply(200, {}, b"{}")):
            reply = gateway.request("fixture", raw=raw)
        self.assertEqual(reply.nonce, current.nonce)
        self.assertEqual(reply.hits, [current])

    def test_listener_inspection_refuses_callers_namespace(self):
        with mock.patch.object(harness, "descendant_gateway", return_value=123), mock.patch.object(harness.os, "readlink", return_value="same"), mock.patch.object(Path, "read_text") as read:
            with self.assertRaisesRegex(AssertionError, "refusing caller namespace"):
                harness.listener_rows(123, "unused")
            read.assert_not_called()


class FakeUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gwtest-fake-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "upstream.sock"
        self.fake = harness.FakeUpstream(self.path)
        self.addCleanup(self.fake.close)

    def request(self, suffix, stream=False, mode=None, raw=None):
        body = {"model": "gwtest-wire-fake", "stream": stream, "messages": [{"role": "user", "content":
                "gwtest-case-012345abcdef" + (" gwtest-mode=" + mode if mode else "")}]}
        return harness.unix_request(self.path, "POST", "/gwtest-fake/" + suffix,
            headers={"User-Agent": "gwtest-test", "x-claude-code-test": "gwtesthint-fake", "X-Uncaptured": "not-retained"},
            body=raw if raw is not None else json.dumps(body).encode())

    def test_protocols_json_sse_and_marker(self):
        for suffix in ("v1/messages?beta=true", "responses", "chat/completions"):
            for stream in (False, True):
                with self.subTest(suffix=suffix, stream=stream):
                    reply = self.request(suffix, stream)
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(reply.headers[harness.MARKER], "1")
                    self.assertEqual(int(reply.headers["content-length"]), len(reply.raw))
                    if suffix.startswith("v1/messages"):
                        if stream:
                            events = reply.events()
                            message = events[0]["message"]
                            self.assertTrue(message["id"])
                            self.assertEqual(message["model"], "gwtest-wire-fake")
                            self.assertIn("message_delta", [e["type"] for e in events])
                            self.assertEqual(events[-1]["type"], "message_stop")
                        else:
                            self.assertEqual(reply.json()["content"][0]["text"], "FAKE_OK")
                    elif suffix == "responses":
                        self.assertEqual(reply.events()[-1]["type"], "response.completed")
                    elif stream:
                        self.assertTrue(reply.raw.endswith(b"data: [DONE]\n\n"))
                    else:
                        self.assertEqual(reply.json()["choices"][0]["message"]["content"], "FAKE_OK")
        self.assertEqual(len(self.fake.hits), 6)
        hit = self.fake.hits[0]
        self.assertEqual(hit.nonce, "gwtest-case-012345abcdef")
        self.assertEqual(hit.query, "beta=true")
        self.assertEqual(hit.header_names, sorted(hit.header_names))
        self.assertIn("x-uncaptured", hit.header_names)
        self.assertNotIn("x-uncaptured", hit.header_values)
        self.assertEqual(hit.header_values["x-claude-code-test"], "gwtesthint-fake")

    def test_unexpected_methods_and_malformed_bodies_are_counted_with_marker(self):
        cases = [(method, b"{}") for method in ("GET", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "GWTEST", "HEAD")]
        cases += [("POST", raw) for raw in (b"not-json", b"\xff", b"[]", b"null", b"")]
        for index, (method, raw) in enumerate(cases, 1):
            with self.subTest(method=method, raw=raw):
                reply = harness.unix_request(self.path, method, "/gwtest-fake/v1/messages", body=raw)
                self.assertEqual(reply.status, 404)
                self.assertEqual(reply.headers[harness.MARKER], "1")
                self.assertEqual(reply.headers["content-type"], "application/json")
                if method != "HEAD":
                    self.assertEqual(int(reply.headers["content-length"]), len(reply.raw))
                    self.assertIn("error", reply.json())
                self.assertEqual(self.fake.unexpected, index)
                self.assertEqual(len(self.fake.hits), index)
                self.assertEqual(self.fake.hits[-1].method, method)

    def test_duplicate_header_values_are_all_scanned(self):
        connection = harness.probe.UnixHTTPConnection(self.path)
        try:
            connection.putrequest("POST", "/gwtest-fake/v1/messages")
            connection.putheader("X-Repeated", "gwtesthint-first-value")
            connection.putheader("X-Repeated", "last-value")
            connection.putheader("Content-Length", "2")
            connection.endheaders(b"{}")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
        finally:
            connection.close()
        self.assertIn("gwtesthint-first-value", self.fake.hits[-1].hint_markers)

    def test_replayed_reasoning_is_attributed_to_the_final_case(self):
        raw = json.dumps({"messages": [
            {"role": "assistant", "content": "gwtest-reasoning-gwtest-case-000000000000 gwtest-mode=length"},
            {"role": "user", "content": "gwtest-case-111111111111 gwtest-mode=reasoning"}]}).encode()
        reply = self.request("chat/completions", raw=raw)
        self.assertEqual(self.fake.hits[-1].nonce, "gwtest-case-111111111111")
        self.assertEqual(reply.json()["choices"][0]["message"]["reasoning_content"],
                         "gwtest-reasoning-gwtest-case-111111111111")

    def test_modes_and_duplicate_top_level_capture(self):
        for suffix in ("v1/messages", "responses", "chat/completions"):
            for stream in (False, True):
                reply = self.request(suffix, stream, "substitute")
                self.assertIn(b"gwtest-served-other", reply.raw)
                self.assertEqual(self.request(suffix, stream, "error400").status, 400)
        for stream in (False, True):
            reply = self.request("chat/completions", stream, "reasoning")
            self.assertIn(b"reasoning_content", reply.raw)
            reply = self.request("chat/completions", stream, "length")
            choices = reply.events()[-1]["choices"] if stream else reply.json()["choices"]
            self.assertEqual(choices[0]["finish_reason"], "length")
        tool = self.request("chat/completions", mode="tool_calls").json()["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(json.loads(tool["function"]["arguments"]), {"k": "v"})
        events = self.request("chat/completions", True, "tool_calls_split").events()
        calls = {}
        for event in events:
            for call in event["choices"][0]["delta"].get("tool_calls", []):
                item = calls.setdefault(call["index"], {"arguments": "", "chunks": 0})
                item.update({k: call[k] for k in ("id",) if k in call})
                item["arguments"] += call["function"]["arguments"]
                item["chunks"] += 1
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({v["id"] for v in calls.values()}), 2)
        for call in calls.values():
            self.assertGreaterEqual(call["chunks"], 2)
            self.assertEqual(json.loads(call["arguments"]), {"k": "v"})
        raw = b'{"model":"gwtest-wire-fake","duplicate":1,"duplicate":2,"nested":{"x":1},"text":"gwtest-case-012345abcdef"}'
        self.request("v1/messages", raw=raw)
        self.assertEqual(self.fake.hits[-1].top_level_keys.count("duplicate"), 2)
        self.assertEqual(self.fake.hits[-1].body["nested"], {"x": 1})
        self.assertEqual(self.request("unexpected").status, 404)
        self.assertEqual(self.fake.unexpected, 1)


class HarnessSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.harness = harness.main_harness()

    def record(self, row, **fields):
        harness.EVIDENCE.record(MODULE, "smoke", {"row": row, **fields})

    def test_readiness_and_launched_version(self):
        gateway = self.harness
        self.assertIsNone(gateway.process.poll())
        self.assertEqual(gateway.get("/healthz").status, 200)
        self.assertIn(harness.WATCHER_READY, gateway.log_path.read_text())
        self.assertIn(render.document_sentinel(gateway.document), {row["id"] for row in gateway.get("/v1/models").json()["data"]})
        self.record("readiness", alive=True, health=200, watcher=True, sentinel=True)
        self.assertGreaterEqual(tuple(map(int, gateway.version.split("."))), (7, 3, 15))
        self.record("version", version=gateway.version)

    def test_one_request_per_family(self):
        for family in ("claude", "codex", "compat"):
            alias = next(a for a, info in self.harness.aliases.items() if info.family == family and not info.levels)
            with self.subTest(family=family):
                reply = self.harness.request(alias)
                self.assertEqual(reply.status, 200, f"{family} smoke status={reply.status}")
                self.assertEqual(len(reply.hits), 1)
                self.assertIn(b"FAKE_OK", reply.raw)
                self.assertEqual(reply.hits[0].body["model"], self.harness.aliases[alias].wire)
                self.record(family, status=reply.status, hits=len(reply.hits), response_ok=True)

    def test_only_expected_private_namespace_listeners(self):
        gateway = self.harness
        rows = harness.listener_rows(gateway.process.pid, gateway.binary)
        self.assertEqual(rows, sorted([("127.0.0.1", gateway.egress), ("127.0.0.1", gateway.port)]))
        self.record("listeners", addresses=[addr for addr, _ in rows], ports=[port for _, port in rows], method="B")



class CodexIdentityManifestTests(unittest.TestCase):
    def test_missing_diagnostic_refuses_instead_of_skipping(self):
        with mock.patch.dict(os.environ, {harness.BINARY_ENV: "/nix/store/fixture/bin/cli-proxy-api",
                                          harness.STARTUP_PROBE_ENV: ""}), \
                mock.patch.object(harness, "find_gateway_binary", return_value=("/nix/store/fixture/bin/cli-proxy-api", "")), \
                mock.patch.object(harness.probe, "network_isolation_available", return_value=None):
            with self.assertRaisesRegex(AssertionError, "diagnostic missing"):
                harness.run_codex_diagnostic("TestOmissionCodexIdentity")

    def test_patch18_required_row_and_manifest_order(self):
        manifest = json.loads((RESOURCES_ROOT / "catalog/gateway.json").read_text())["gateway"]["patches"]
        self.assertEqual(list(harness.PATCH_ROWS), manifest)
        row = "tests.test_gateway_patches.CodexIdentityTests.test_codex_identity"
        self.assertEqual(harness.PATCH_ROWS["cli-proxy-api-codex-client-identity.patch"], (row,))
        self.assertIn("tests.test_gateway_patches", harness.gateway_check_selections())
        self.assertTrue({"CI1", "CI2"} <= harness.REQUIRED_EVIDENCE["tests.test_gateway_patches"]["patches"])
        self.assertNotIn("cli-proxy-api-codex-client-identity.patch", harness.PATCH_DEPENDENCIES)





class KeyedEvidenceTests(unittest.TestCase):
    def document(self):
        return {"schema": "gwtest-evidence-v1", "module": "tests.test_gateway_keyed_compat",
                "binary": {"realpath": "/nix/store/fixture/bin/cli-proxy-api", "version": "7.3.15"},
                "keyed_identity": {"gateway_sha256": "a" * 64, "sources": {"render.py": "b" * 64}},
                "selected": ["core"], "tables": {"core": [
                    {"row": row, "status": "passed"} for row in sorted(harness.KEYED_INVENTORIES["core"])]}}

    def test_keyed_failed_subtest_produces_no_row(self):
        import test_gateway_keyed_compat as gateway
        import test_keyed_compat_client as client

        for module in (gateway, client):
            with self.subTest(module=module.MODULE):
                evidence = harness.Evidence()

                class FailedProof(unittest.TestCase):
                    def runTest(inner):
                        with inner.subTest(cell="failed"):
                            inner.fail("deliberate failed proof cell")
                        if module is gateway:
                            gateway.record(inner, "core", "K12")
                        else:
                            client.KeyedClientTests.record(inner, "C1-fence-fallback")

                with mock.patch.object(module, "EVIDENCE", evidence):
                    result = unittest.TestResult()
                    FailedProof().run(result)
                self.assertEqual(len(result.failures), 2)  # cell and refused record
                self.assertFalse(result.errors)
                self.assertFalse(evidence.tables)

    def test_keyed_evidence_refuses_dummy_credentials(self):
        evidence = harness.Evidence()
        for value in ("keyedtest-key-private", ["safe", "keyedtest-key-private"], {"nested": "keyedtest-key-private"},
                      [["keyedtest-key-private"]]):
            with self.subTest(kind=type(value).__name__), self.assertRaises((TypeError, ValueError)):
                evidence.record("keyed", "core", {"row": "K1", "value": value})

    def test_keyed_required_rows_missing_or_skipped_fail(self):
        document = self.document()
        identity = document["keyed_identity"]
        harness.validate_keyed_document(document, {"core"}, identity=identity)
        for mutation in ("missing", "skipped", "duplicate"):
            broken = copy.deepcopy(document)
            rows = broken["tables"]["core"]
            if mutation == "missing":
                rows.pop()
            elif mutation == "skipped":
                rows[0]["status"] = "skipped"
            else:
                rows.append(copy.deepcopy(rows[0]))
            with self.assertRaises(AssertionError):
                harness.validate_keyed_document(broken, {"core"}, identity=identity)
        evidence = harness.Evidence()
        evidence.binary = document["binary"]
        evidence.tables[document["module"]] = document["tables"]
        with mock.patch.dict(os.environ, {harness.KEYED_LANE_ENV: "host"}, clear=True),                 mock.patch.object(harness, "keyed_identity", return_value=identity),                 mock.patch.object(state, "atomic_write") as write:
            harness.finalize_keyed(evidence, document["module"], {"core"})
            write.assert_not_called()
            evidence.tables[document["module"]]["core"].pop()
            with self.assertRaises(AssertionError):
                harness.finalize_keyed(evidence, document["module"], {"core"})

    def test_keyed_required_binary_isolation_tls_fail_not_skip(self):
        import test_gateway_keyed_compat as keyed
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(AssertionError):
            keyed.prerequisites()
        with mock.patch.dict(os.environ, {harness.BINARY_ENV: "/candidate"}),                 mock.patch.object(harness, "find_gateway_binary", return_value=(None, "missing")),                 self.assertRaises(AssertionError):
            keyed.prerequisites()
        with mock.patch.dict(os.environ, {harness.BINARY_ENV: "/candidate"}),                 mock.patch.object(harness, "find_gateway_binary", return_value=("/candidate", "")):
            with mock.patch.object(harness.probe, "network_isolation_available", return_value="missing"),                     self.assertRaises(AssertionError):
                keyed.prerequisites()
            with mock.patch.object(harness.probe, "network_isolation_available", return_value=None),                     mock.patch.object(keyed.shutil, "which", return_value=None), self.assertRaises(AssertionError):
                keyed.prerequisites()

    def test_keyed_lane_selection_separates_core_and_host(self):
        from tests import test_gateway_keyed_compat as keyed
        loader = unittest.TestLoader()
        with mock.patch.dict(os.environ, {harness.KEYED_LANE_ENV: "unit"}):
            self.assertEqual(loader.loadTestsFromModule(keyed).countTestCases(), 0)
        with mock.patch.dict(os.environ, {harness.KEYED_LANE_ENV: "host"}), mock.patch.object(keyed, "SELECTED", set()):
            self.assertEqual(loader.loadTestsFromModule(keyed).countTestCases(), 16)
        self.assertNotIn("tests.test_gateway_keyed_compat", harness.GATEWAY_CHECK_MODULES)
        self.assertIn("tests.test_gateway_keyed_compat.KeyedGatewayCoreTests", harness.gateway_check_selections())
        self.assertNotIn("tests.test_keyed_compat_client", harness.GATEWAY_CHECK_MODULES)
        with mock.patch.dict(os.environ, {harness.KEYED_LANE_ENV: "unknown"}), self.assertRaises(AssertionError):
            harness.keyed_lane()

    def test_keyed_evidence_rejects_wrong_source_or_binary_identity(self):
        document = self.document()
        for change in ("gateway_sha256", "sources"):
            identity = copy.deepcopy(document["keyed_identity"])
            identity[change] = "wrong"
            with self.assertRaises(AssertionError):
                harness.validate_keyed_document(document, {"core"}, identity=identity)
        with self.assertRaises(AssertionError):
            harness.validate_keyed_document(document, {"core", "audit"}, identity=document["keyed_identity"])

    def test_keyed_gateway_scope_needs_core_and_audit_but_no_client(self):
        document = self.document()
        document["selected"] = ["audit", "core"]
        document["tables"]["audit"] = [{"row": row, "status": "passed"}
                                       for row in sorted(harness.KEYED_INVENTORIES["audit"])]
        identity = document["keyed_identity"]
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw) / "evidence"
            state.ensure_private_dir(directory)
            state.atomic_write(directory / "tests.test_gateway_keyed_compat.json", json.dumps(document).encode())
            with mock.patch.object(harness, "keyed_identity", return_value=identity):
                harness.validate_keyed_evidence(directory, gateway_only=True)
                # The complete proof also needs the client file; core-only reads core alone.
                with self.assertRaises(FileNotFoundError):
                    harness.validate_keyed_evidence(directory)
                with self.assertRaises(AssertionError):
                    harness.validate_keyed_evidence(directory, core_only=True)
                with self.assertRaises(AssertionError):
                    harness.validate_keyed_evidence(directory, core_only=True, gateway_only=True)
                document["tables"]["audit"].pop()
                state.atomic_write(directory / "tests.test_gateway_keyed_compat.json", json.dumps(document).encode())
                with self.assertRaises(AssertionError):
                    harness.validate_keyed_evidence(directory, gateway_only=True)


if __name__ == "__main__":
    unittest.main()
