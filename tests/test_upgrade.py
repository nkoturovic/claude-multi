"""Tests for the evidence-gated Claude Code re-pin (claude-multi-dev repin)."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, paths, pin, release_manifest, state, upgrade, validate
from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT, RESOURCES_RELATIVE, RESOURCES_ROOT
from claude_multi.upgrade import UpgradeError


FAKE_CLAUDE = """#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "VERSION_NAME (Claude Code)"
    exit 0
fi
if [ "$1" = "--help" ]; then
    echo "Usage: claude [options] - Claude Code"
    exit 0
fi
exit 0
# settings schema: {$schema:()=>o(),apiKeyHelper:()=>o().optional().describe("Path to a script that outputs authentication values"),env:()=>o(),hooks:()=>o(),model:()=>o(),permissions:()=>o()}
"""
FAKE_SETTINGS_KEYS = ["$schema", "apiKeyHelper", "env", "hooks", "model", "permissions"]

PLATFORM = pin.host_platform()
OTHER_PLATFORM = "darwin-arm64" if PLATFORM != "darwin-arm64" else "linux-x64"
OLD_CONTRACT = {
    "version": 2,
    "verified": [{
        "version": "2.1.217",
        "platforms": {PLATFORM: {"sha256": "a" * 64, "size": 10}},
        "manifest_sha256": "1" * 64,
        "signature_sha256": "2" * 64,
        "key_fingerprint": release_manifest.KEY_FINGERPRINT,
        "verified_at": "2026-01-01",
        "evidence": {PLATFORM: "battery", "receipt_sha256": "3" * 64},
    }],
    "lifecycle_evidence": {
        "inspected_version": "2.1.217",
        "evidence_id": "deepwork-thing-2.1.217",
    },
}


REAL_RELEASE_VERIFIER = upgrade._release_verifier
FIXTURE_SCHEMA = b'{"fixture": "native-contract"}\n'
FIXTURE_RELEASE = upgrade.ReleaseIdentity("2.3.1", 3, 1, hashlib.sha256(FIXTURE_SCHEMA).hexdigest())


def fake_release(version, digest, size):
    return release_manifest.ReleaseProof(
        version, PLATFORM, digest, size, "b" * 64, "c" * 64, release_manifest.KEY_FINGERPRINT,
        {PLATFORM: {"sha256": digest, "size": size}, OTHER_PLATFORM: {"sha256": "d" * 64, "size": 7}})


def _pinned(contract) -> str:
    return pin.version(contract)


class UpgradeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for patcher in (
            mock.patch.object(upgrade, "running_release", return_value=FIXTURE_RELEASE),
            mock.patch.object(upgrade, "_release_verifier", return_value=fake_release),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-upgrade-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.env = {"HOME": str(self.root / "home"), "PATH": str(self.root / "bin"),
                    "XDG_STATE_HOME": str(self.root / "state")}
        self.versions = paths.upstream_versions_dir(self.env)
        self.versions.mkdir(parents=True)
        self.old = self._write_version("2.1.217")
        self.new = self._write_version("2.1.218")
        self.link = self.root / "bin" / "claude"
        self.link.parent.mkdir()
        self.link.symlink_to(self.old)
        self.contract = json.loads(json.dumps(OLD_CONTRACT))

    def repin(self, **kwargs):
        kwargs.setdefault("env", self.env)
        return upgrade.run_repin(**kwargs)

    def _write_version(self, name: str) -> Path:
        path = self.versions / name
        path.write_text(FAKE_CLAUDE.replace("VERSION_NAME", name), encoding="utf-8")
        path.chmod(0o755)
        return path

    def _product_tree(self, repo_name: str = "repo") -> Path:
        product = self.root / repo_name
        (product / RESOURCES_RELATIVE / "catalog").mkdir(parents=True)
        (product / "tests").mkdir()
        (product / RESOURCES_RELATIVE / "schemas").mkdir()
        (product / RESOURCES_RELATIVE / "schemas/native-contract.schema.json").write_bytes(FIXTURE_SCHEMA)
        (product / RESOURCES_RELATIVE / "catalog" / "native-contract.json").write_text(
            json.dumps(self.contract, indent=2) + "\n", encoding="utf-8"
        )
        (product / RESOURCES_RELATIVE / "version.json").write_text(
            json.dumps({"version": 1, "launcher_version": "2.3.1", "catalog_version": 3}) + "\n",
            encoding="utf-8",
        )
        return product

    def _successful_evidence_output(self) -> str:
        records = [
            prefix + f"record-{index}"
            for index, prefix in enumerate(
                upgrade.ESSENTIAL_EVIDENCE_PREFIXES, start=1
            )
        ]
        return "\n".join([*records, "Ran 42 tests"]) + "\n"

    def _runner(self, returncode: int, out: str | None = None):
        output = self._successful_evidence_output() if out is None else out

        def run(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], returncode, output, "")
        return run


class InspectCandidateTests(UpgradeTestCase):
    def test_inspects_a_valid_artifact(self) -> None:
        result = upgrade.inspect_candidate(self.new)
        self.assertEqual(result.version, "2.1.218")
        self.assertEqual(len(result.sha256), 64)

    def test_rejects_symlink_missing_shape_and_mode(self) -> None:
        with self.assertRaisesRegex(UpgradeError, "symlink"):
            upgrade.inspect_candidate(self.link)
        with self.assertRaisesRegex(UpgradeError, "missing"):
            upgrade.inspect_candidate(self.versions / "2.1.999")
        odd = self.versions / "claude-dev"
        odd.write_text("#!/bin/sh\n", encoding="utf-8")
        odd.chmod(0o755)
        with self.assertRaisesRegex(UpgradeError, "X.Y.Z"):
            upgrade.inspect_candidate(odd)
        noexec = self._write_version("2.1.300")
        noexec.chmod(0o644)
        with self.assertRaisesRegex(UpgradeError, "not executable"):
            upgrade.inspect_candidate(noexec)


class FindCandidateTests(UpgradeTestCase):
    def test_newer_path_target_wins(self) -> None:
        newest = self._write_version("2.1.300")
        self.link.unlink()
        self.link.symlink_to(self.new)
        self.assertEqual(upgrade.find_candidates(self.contract, self.env), [self.new, newest])

    def test_newest_native_version_when_path_claude_is_current(self) -> None:
        self.assertEqual(upgrade.find_candidate(self.contract, self.env), self.new)

    def test_xdg_data_home_versions_are_candidates(self) -> None:
        xdg = self.root / "xdg"
        (xdg / "claude/versions").mkdir(parents=True)
        newer = xdg / "claude/versions/2.1.219"
        shutil.copy2(self.new, newer)
        self.assertEqual(upgrade.find_candidate(self.contract, {**self.env, "XDG_DATA_HOME": str(xdg)}), newer)

    def test_none_when_nothing_newer(self) -> None:
        self.new.unlink()
        self.assertIsNone(upgrade.find_candidate(self.contract, self.env))

    def test_rejects_malformed_pin(self) -> None:
        self.contract["verified"][0]["version"] = "dev"
        with self.assertRaisesRegex(UpgradeError, "not X.Y.Z"):
            upgrade.find_candidate(self.contract, self.env)


class RenderContractTests(UpgradeTestCase):
    def test_promotes_every_signed_build_and_provenance(self) -> None:
        inspection = upgrade.inspect_candidate(self.new, verify=fake_release)
        promoted = upgrade.render_contract(self.contract, inspection, today="2026-07-24",
                                           receipt_sha256="e" * 64, settings_keys=FAKE_SETTINGS_KEYS)
        self.assertEqual(promoted["version"], 2)
        self.assertNotIn("claude", promoted)
        entry = promoted["verified"][0]
        self.assertEqual(entry["version"], "2.1.218")
        self.assertEqual(entry["platforms"][PLATFORM], {"sha256": inspection.sha256,
                                                        "size": self.new.stat().st_size})
        self.assertEqual(entry["platforms"][OTHER_PLATFORM], {"sha256": "d" * 64, "size": 7})
        self.assertEqual((entry["manifest_sha256"], entry["signature_sha256"], entry["key_fingerprint"]),
                         ("b" * 64, "c" * 64, release_manifest.KEY_FINGERPRINT))
        self.assertEqual(entry["verified_at"], "2026-07-24")
        self.assertEqual(entry["evidence"], {PLATFORM: "battery", "others": "identity+smoke",
                                             "receipt_sha256": "e" * 64})
        self.assertEqual(promoted["lifecycle_evidence"]["inspected_version"], "2.1.218")
        self.assertEqual(promoted["lifecycle_evidence"]["evidence_id"], "deepwork-thing-2.1.218")
        # The prior contract is not mutated.
        self.assertEqual(_pinned(self.contract), "2.1.217")

    def test_unsigned_candidate_is_refused(self) -> None:
        inspection = upgrade.inspect_candidate(self.new)
        with self.assertRaisesRegex(UpgradeError, "no verified signed-manifest builds"):
            upgrade.render_contract(self.contract, inspection, today="2026-07-24")

    def test_rendered_contract_loads_with_the_real_schema(self):
        prior = json.loads((FIXTURE_ROOT / "catalog/native-contract.json").read_bytes())
        schema = json.loads((RESOURCES_ROOT / "schemas/native-contract.schema.json").read_bytes())
        version = "10.100.1000"
        proof = release_manifest.ReleaseProof(
            version, "linux-arm64-musl", "a" * 64, 123, "b" * 64, "c" * 64,
            release_manifest.KEY_FINGERPRINT, {name: {"sha256": "a" * 64, "size": 123} for name in pin.PLATFORMS},
        )
        assets = self.root / "assets"
        shutil.copytree(FIXTURE_ROOT, assets)
        inspection = upgrade.CandidateInspection(path=self.versions / version, version=version,
                                                 sha256=proof.sha256, release=proof)
        promoted = upgrade.render_contract(prior, inspection, today="2026-09-27",
                                           settings_keys=FAKE_SETTINGS_KEYS)
        self.assertEqual(validate.validate(promoted, schema), [])
        (assets / "catalog/native-contract.json").write_text(json.dumps(promoted) + "\n")
        loaded = catalog.load_catalog(assets)
        self.assertEqual(pin.version(loaded.docs["native-contract"]), version)
        self.assertEqual(len(loaded.docs["native-contract"]["verified"][0]["platforms"]), 8)


class SettingsKeysTests(UpgradeTestCase):
    """The settings keys a build knows, read from its bytes (never run)."""

    SCHEMA = (
        b'junk{"apiKeyHelper": 1};function s(){let r=1;return{$schema:()=>o().optional(),'
        b'apiKeyHelper:()=>o().optional().describe("Path to a script that outputs authentication values"),'
        b'env:()=>u({a:o()}),hooks:()=>s("x,y:{},z"),model:()=>o(),permissions:()=>u({deny:A(o()),b:1}),'
        b'...flag&&{xaaIdp:()=>o()},...!1,theme:()=>U(["a","b"]),"quoted":()=>o(),...qr(e,(i)=>i)}}tail'
    )

    def build(self, data: bytes) -> Path:
        path = self.root / "build"
        path.write_bytes(b"\x7fELF..." + data + b"...")
        return path

    def test_keys_of_the_schema_object_sorted(self) -> None:
        self.assertEqual(upgrade.settings_keys_from_binary(self.build(self.SCHEMA)),
                         ["$schema", "apiKeyHelper", "env", "hooks", "model", "permissions", "quoted",
                          "theme", "xaaIdp"])
        # Older builds define the entries without the thunk.
        older = self.SCHEMA.replace(b"apiKeyHelper:()=>o()", b"apiKeyHelper:o()")
        self.assertIn("permissions", upgrade.settings_keys_from_binary(self.build(older)))

    def test_ambiguous_missing_or_incomplete_schemas_are_refused(self) -> None:
        with self.assertRaisesRegex(UpgradeError, "no single settings schema"):
            upgrade.settings_keys_from_binary(self.build(b"nothing here"))
        with self.assertRaisesRegex(UpgradeError, "no single settings schema"):
            upgrade.settings_keys_from_binary(self.build(self.SCHEMA + self.SCHEMA))
        with self.assertRaisesRegex(UpgradeError, "lacks hooks"):
            upgrade.settings_keys_from_binary(self.build(self.SCHEMA.replace(b"hooks:", b"hookz:")))

    def test_render_records_carries_or_refuses_the_keys(self) -> None:
        inspection = upgrade.inspect_candidate(self.new, verify=fake_release)
        promoted = upgrade.render_contract(self.contract, inspection, today="2026-07-24",
                                           settings_keys=["model", "env", "model"])
        self.assertEqual(promoted["verified"][0]["settings_keys"], ["env", "model"])
        # A new version without keys is refused: the launch's settings-skew
        # safeguard needs them (the prior version's keys never carry over).
        prior = copy.deepcopy(self.contract)
        prior["verified"][0]["settings_keys"] = ["env"]
        for keys in (None, []):
            with self.subTest(keys=keys), self.assertRaisesRegex(UpgradeError, "--settings-keys FILE"):
                upgrade.render_contract(prior, inspection, today="2026-07-24", settings_keys=keys)
        # Re-recording the pinned version keeps its keys.
        same = upgrade.CandidateInspection(self.new, _pinned(prior), inspection.sha256, inspection.release)
        self.assertEqual(upgrade.render_contract(prior, same, today="2026-07-24")["verified"][0]["settings_keys"],
                         ["env"])


class SettingsKeysInventoryTests(UpgradeTestCase):
    """A new pin needs the settings keys its build knows: read from the
    candidate, or a reviewed list; never promoted without them."""

    def write_keys(self, content: str) -> Path:
        path = self.root / "keys.json"
        path.write_text(content)
        return path

    def test_reviewed_lists(self) -> None:
        keys = upgrade.read_settings_keys_file(self.write_keys(json.dumps(
            ["permissions", "model", "hooks", "env", "apiKeyHelper", "newKey"])))
        self.assertEqual(keys, ["apiKeyHelper", "env", "hooks", "model", "newKey", "permissions"])
        for content, message in (
            ("{bad", "cannot read"),
            ('{"keys": []}', "JSON array of settings key names"),
            ("[]", "JSON array of settings key names"),
            ('["env", "not a key"]', "JSON array of settings key names"),
            ('["apiKeyHelper", "env", "hooks", "model", "permissions", "env"]', "more than once"),
            ('["apiKeyHelper", "env", "model", "permissions"]', "lacks hooks"),
        ):
            with self.subTest(content=content), self.assertRaisesRegex(UpgradeError, message):
                upgrade.read_settings_keys_file(self.write_keys(content))

    def test_a_candidate_whose_keys_cannot_be_read_is_refused_before_any_write(self) -> None:
        self.new.write_text(FAKE_CLAUDE.replace("VERSION_NAME", "2.1.218").replace("apiKeyHelper:", "x:"))
        product = self._product_tree()
        before = {path: path.read_bytes() for path in product.rglob("*") if path.is_file()}
        runner = mock.Mock(side_effect=AssertionError("no evidence suite"))
        with self.assertRaisesRegex(UpgradeError, "no single settings schema.*--settings-keys FILE.*nothing was changed"):
            self.repin(checkout_root=product, native_contract=self.contract, today="2026-07-24", runner=runner)
        self.assertEqual({path: path.read_bytes() for path in product.rglob("*") if path.is_file()}, before)
        runner.assert_not_called()

    def test_a_reviewed_list_is_recorded(self) -> None:
        self.new.write_text(FAKE_CLAUDE.replace("VERSION_NAME", "2.1.218").replace("apiKeyHelper:", "x:"))
        product = self._product_tree()
        reviewed = ["apiKeyHelper", "env", "hooks", "model", "permissions", "reviewedKey"]
        outcome = self.repin(checkout_root=product, native_contract=self.contract, today="2026-07-24",
                             runner=self._runner(0), settings_keys=reviewed)
        self.assertEqual(outcome.kind, "prepared")
        self.assertIn("settings keys: 6 recorded from the reviewed list", outcome.messages)
        written = json.loads((product / RESOURCES_RELATIVE / "catalog/native-contract.json").read_bytes())
        self.assertEqual(written["verified"][0]["settings_keys"], reviewed)

    def test_keys_read_from_the_candidate_are_recorded(self) -> None:
        product = self._product_tree()
        outcome = self.repin(checkout_root=product, native_contract=self.contract, today="2026-07-24",
                             runner=self._runner(0))
        self.assertEqual(outcome.kind, "prepared")
        written = json.loads((product / RESOURCES_RELATIVE / "catalog/native-contract.json").read_bytes())
        self.assertEqual(written["verified"][0]["settings_keys"], FAKE_SETTINGS_KEYS)

    def test_the_dev_command_reads_the_reviewed_list(self) -> None:
        from claude_multi.cli.commands import repin as repin_cmd

        path = self.write_keys(json.dumps(["apiKeyHelper", "env", "hooks", "model", "permissions"]))
        product = self._product_tree()
        with mock.patch.object(upgrade, "run_repin", return_value=upgrade.UpgradeOutcome(
                kind="current", inspection=None, messages=())) as run, \
                mock.patch("claude_multi.dev.verify_repo", return_value=product), \
                mock.patch.object(catalog, "load_raw", return_value={"docs": {"native-contract": self.contract}}):
            self.assertEqual(repin_cmd.repin({"settings-keys": str(path)}, environ=self.env,
                                             output_stream=io.StringIO()), 0)
        self.assertEqual(run.call_args.kwargs["settings_keys"], ["apiKeyHelper", "env", "hooks", "model", "permissions"])
        with self.assertRaisesRegex(Exception, "lacks hooks"):
            repin_cmd.repin({"settings-keys": str(self.write_keys('["apiKeyHelper", "env", "model", "permissions"]'))},
                            environ=self.env, output_stream=io.StringIO())


class CandidateHandoffTests(UpgradeTestCase):
    """The evidence suite's probes find the verified candidate wherever the
    re-pin found it."""

    def test_a_path_candidate_outside_the_versions_dirs_reaches_the_probes(self) -> None:
        from claude_multi import probe

        custom = self.root / "custom-releases" / "2.1.218"
        custom.parent.mkdir()
        custom.write_text(FAKE_CLAUDE.replace("VERSION_NAME", "2.1.218"))
        custom.chmod(0o755)
        self.new.unlink()
        self.link.unlink()
        self.link.symlink_to(custom)
        product = self._product_tree()
        found = []

        def runner(argv, **kwargs):
            if list(argv[:2]) != ["nix", "build"]:
                promoted = json.loads((product / RESOURCES_RELATIVE / "catalog/native-contract.json").read_bytes())
                found.append(probe.trusted_from_contract(promoted, kwargs["env"]).resolved_path)
                self.assertEqual(kwargs["env"][probe.REPIN_CANDIDATE_ENV], str(custom))
            return self._runner(0)(argv, **kwargs)

        outcome = self.repin(checkout_root=product, native_contract=self.contract, today="2026-07-24",
                             runner=runner)
        self.assertEqual(outcome.kind, "prepared")
        self.assertEqual(found, [custom])


class EssentialEvidencePrefixTests(unittest.TestCase):
    def test_prefix_indices(self) -> None:
        """The 2.x four keep indices 0-3 (the
        RealPinnedBinaryTests print through them), the per-seed probe is 4,
        then the S1/S4/S5/S6/S7/S9 spike PASS lines (their exact prints)."""

        self.assertEqual(
            upgrade.ESSENTIAL_EVIDENCE_PREFIXES,
            (
                "real delegation outcome: ran ",
                "fence negative control: subagent silently ran on the lead ",
                "real manual compaction outcome: ran ",
                "real auto compaction outcome: ran ",
                "per-seed delegation outcome: ran ",
                "client check S1: PASS ",
                "client check S4: PASS ",
                "client check S5: PASS ",
                "client check S6: PASS ",
                "client check S7: PASS ",
                "client check S9: PASS ",
                "client probe CE: PASS class=CE-A ",
                "client probe DAH: PASS class=DAH-override ",
                "client probe FD: PASS class=FD-env-off ",
                "client probe CC: PASS class=CC-bash-child ",
                "client probe XC: PASS class=XC-exact ",
                "client probe S14: PASS class=S14-model-deny ",
                "client probe S14-agents: PASS class=S14-subagent-deny ",
                "client probe S14-matrix: PASS class=S14-mode-allow-matrix ",
                "client probe S14-alias: PASS class=S14-alias-resolution ",
                "client probe S14-user-only: PASS class=S14-upstream-classified ",
                "client probe S14-precedence: PASS class=S14-fresh-resume ",
                "client probe S14-controls: PASS class=S14-positive-controls ",
                "client probe S14-typed: PASS class=S14-typed-run ",
                "client probe FM: PASS class=FM-prefetch-off ",
                "client probe FM-layers: PASS class=FM-flag-settings ",
            ),
        )
        # A fail-closed or BOUNDARY print never matches a prefix.
        for line in (
            "per-seed delegation outcome: fail-closed: live daemon domain touched",
            "client check S5: INCONCLUSIVE unobserved=fork",
            "client check S6: FAIL picker=deny:allow",
        ):
            self.assertTrue(upgrade._missing_evidence_prefixes(line))
            self.assertEqual(
                len(upgrade._missing_evidence_prefixes(line)),
                len(upgrade.ESSENTIAL_EVIDENCE_PREFIXES),
            )

    def test_client_boundary_class_is_the_only_alternative(self) -> None:
        """FD (feedback drafts) is satisfied by the full proof or by the
        explicit subagent-withheld boundary class, never by another class."""

        d95 = "client probe FD: PASS class=FD-env-off "
        self.assertEqual(
            upgrade.ESSENTIAL_EVIDENCE_ALTERNATIVES,
            {d95: ("client probe FD: PASS class=FD-env-off-subagent-withheld ",)},
        )
        others = "\n".join(
            prefix + "fixture" for prefix in upgrade.ESSENTIAL_EVIDENCE_PREFIXES if prefix != d95
        )
        for line, missing in (
            ("client probe FD: PASS class=FD-env-off off lead=False", ()),
            ("client probe FD: PASS class=FD-env-off-subagent-withheld off lead=False", ()),
            ("client probe FD: FAIL class=FD-env-ignored class pin expects FD-env-off", (d95,)),
            ("client probe FD: PASS class=FD-env-offish detail", (d95,)),
            ("client probe FD: INCONCLUSIVE class=unavailable control", (d95,)),
        ):
            with self.subTest(line=line):
                self.assertEqual(upgrade._missing_evidence_prefixes(others + "\n" + line + "\n"), missing)


class RunRepinTests(UpgradeTestCase):

    def test_success_message_counts_every_essential_probe(self) -> None:
        product = self._product_tree()
        outcome = self.repin(checkout_root=product, native_contract=self.contract,
                             today="2026-07-24", runner=self._runner(0))
        count = len(upgrade.ESSENTIAL_EVIDENCE_PREFIXES)
        self.assertIn(
            f"all {count} essential native probes completed positively",
            "\n".join(outcome.messages),
        )

    def test_checkout_promoted_receipt_recorded_and_no_override(self) -> None:
        product = self._product_tree()
        outcome = self.repin(checkout_root=product, native_contract=self.contract,
                             today="2026-07-24", runner=self._runner(0))
        self.assertEqual(outcome.kind, "prepared")
        repo_contract = json.loads((product / RESOURCES_RELATIVE / "catalog" / "native-contract.json").read_bytes())
        self.assertEqual(_pinned(repo_contract), "2.1.218")
        receipt = hashlib.sha256(self._successful_evidence_output().encode()).hexdigest()
        self.assertEqual(repo_contract["verified"][0]["evidence"]["receipt_sha256"], receipt)
        self.assertEqual(json.loads((product / RESOURCES_RELATIVE / "version.json").read_bytes())["catalog_version"], 4)
        # No operator override anywhere: a build runs only its own pin.
        self.assertFalse((self.root / "config").exists())
        self.assertFalse(any("override" in line for line in outcome.messages))
        self.assertTrue(any("claude-multi setup --step claude" in line for line in outcome.messages))

    def test_diagnostic_observations_promote_with_notes_only_for_changes(self) -> None:
        for index, (probe_id, observed, recorded, detail) in enumerate((
            ("U1", "U1-none", "U1-proc", "changed recorded=U1-proc"),
            ("X5", "unavailable", "X5-silent-hours", "changed recorded=X5-silent-hours"),
            ("R19", "unavailable", "unavailable", "probe unavailable: ProbeError"),
            ("SC", "SC-future", "SC-caps", "changed recorded=SC-caps"),
            ("SX", "SX-closed", "SX-open-continue-dies", "changed recorded=SX-open-continue-dies"),
            ("SF", "SF-lead-light", "SF-lead-heavy", "changed recorded=SF-lead-heavy"),
            ("RET", "unavailable", None, "baseline=unrecorded"),
        )):
            with self.subTest(probe=probe_id, observed=observed):
                product = self._product_tree(f"diagnostic-{index}")
                status = "INCONCLUSIVE" if observed == "unavailable" else "PASS"
                verdict = f"client probe {probe_id}: {status} class={observed} {detail}\n"
                # Duplicate captured lines still yield one note per diagnostic.
                output = self._successful_evidence_output() + verdict * 2
                outcome = self.repin(checkout_root=product, native_contract=self.contract,
                                     today="2026-09-28", runner=self._runner(0, output))
                note = (f"note: probe {probe_id} observed {observed} on Claude 2.1.218 "
                        f"(recorded: {recorded}); the doctor and lead-prompt text that depends on it "
                        "may be inaccurate until a claude-multi release re-records it")
                notes = [line for line in outcome.messages if line.startswith("note: probe ")]
                self.assertEqual(notes, [note] if "changed recorded=" in detail else [])
                self.assertEqual(outcome.kind, "prepared")
                self.assertEqual(_pinned(json.loads((product / RESOURCES_RELATIVE / "catalog/native-contract.json").read_bytes())),
                                 "2.1.218")
                self.assertEqual(json.loads((product / RESOURCES_RELATIVE / "version.json").read_bytes())["catalog_version"], 4)

    def test_unchanged_diagnostic_or_non_diagnostic_has_no_note(self) -> None:
        self.assertEqual(upgrade._diagnostic_notes(
            "client probe U1: PASS class=U1-proc observation\n"
            "client probe R19: INCONCLUSIVE class=unavailable observation\n"
            "client probe SF: PASS class=SF-lead-heavy baseline=unrecorded\n"
            "client probe RET: INCONCLUSIVE class=unavailable baseline=unrecorded\n"
            "client probe CE: PASS class=CE-B changed recorded=CE-A\n"
            "client probe DAH: PASS class=unavailable observation\n", "2.1.218"), ())

    def test_promotion_syncs_pinned_test_literals(self) -> None:
        product = self._product_tree()
        pinned = (
            'validated = "2.1.217"\n'
            f'sha = "{"a" * 64}"\n'
            'verified_at = "2026-01-01"\n'
            'floors = {"opus5": "2.1.217"}  # decoupled from the artifact version\n'
            'self.assertEqual(bundle.docs["version"]["catalog_version"], 3)\n'
        )
        (product / "tests" / "test_catalog.py").write_text(pinned, encoding="utf-8")
        outcome = self.repin(checkout_root=product, native_contract=self.contract,
                             today="2026-07-24", runner=self._runner(0))
        text = (product / "tests" / "test_catalog.py").read_text(encoding="utf-8")
        inspection = upgrade.inspect_candidate(self.new)
        self.assertIn('validated = "2.1.218"', text)
        self.assertIn(f'sha = "{inspection.sha256}"', text)
        self.assertIn('verified_at = "2026-07-24"', text)
        self.assertIn('"catalog_version"], 4)', text)
        # The decoupled per-model floor line is never touched.
        self.assertIn('floors = {"opus5": "2.1.217"}', text)
        self.assertTrue(
            any("synced version-pinned test literals" in line for line in outcome.messages)
        )

    def test_evidence_failure_restores_synced_test_files(self) -> None:
        product = self._product_tree()
        pinned = 'validated = "2.1.217"\n'
        target = product / "tests" / "test_native_contract.py"
        target.write_text(pinned, encoding="utf-8")
        with self.assertRaisesRegex(UpgradeError, "evidence suite failed"):
            self.repin(checkout_root=product, native_contract=self.contract,
                       today="2026-07-24", runner=self._runner(1, "FAILED"))
        self.assertEqual(target.read_text(encoding="utf-8"), pinned)

    def test_evidence_failure_restores_the_checkout(self) -> None:
        product = self._product_tree()
        before_contract = (product / RESOURCES_RELATIVE / "catalog" / "native-contract.json").read_bytes()
        before_version = (product / RESOURCES_RELATIVE / "version.json").read_bytes()
        with self.assertRaisesRegex(UpgradeError, "evidence suite failed.*restored unchanged"):
            self.repin(checkout_root=product, native_contract=self.contract,
                       today="2026-07-24", runner=self._runner(1, "FAILED"))
        self.assertEqual((product / RESOURCES_RELATIVE / "catalog" / "native-contract.json").read_bytes(), before_contract)
        self.assertEqual((product / RESOURCES_RELATIVE / "version.json").read_bytes(), before_version)

    def test_failure_names_survive_a_long_traceback_tail(self) -> None:
        product = self._product_tree()
        failures = "ERROR: test_first (fixture.First)\nFAIL: test_second (fixture.Second)\n"
        output = failures + "traceback line\n" * 30 + "FAILED\n"
        with self.assertRaises(UpgradeError) as caught:
            self.repin(checkout_root=product, native_contract=self.contract,
                       today="2026-07-24", runner=self._runner(1, output))
        self.assertIn("ERROR: test_first (fixture.First)", str(caught.exception))
        self.assertIn("FAIL: test_second (fixture.Second)", str(caught.exception))

    def test_zero_exit_missing_each_completion_record_restores_everything(self) -> None:
        for index, missing_prefix in enumerate(upgrade.ESSENTIAL_EVIDENCE_PREFIXES, start=1):
            with self.subTest(missing_prefix=missing_prefix):
                product = self._product_tree(f"repo-missing-{index}")
                pinned = product / "tests" / "test_native_contract.py"
                pinned.write_text('validated = "2.1.217"\n', encoding="utf-8")
                before = {path: path.read_bytes() for path in (
                    product / RESOURCES_RELATIVE / "catalog" / "native-contract.json", product / RESOURCES_RELATIVE / "version.json", pinned)}
                output = "\n".join(
                    prefix + "ok" for prefix in upgrade.ESSENTIAL_EVIDENCE_PREFIXES if prefix != missing_prefix
                ) + "\nRan 42 tests\n"
                with self.assertRaisesRegex(UpgradeError, "completion evidence is missing") as raised:
                    self.repin(checkout_root=product, native_contract=self.contract,
                               today="2026-07-24", runner=self._runner(0, output))
                self.assertIn(repr(missing_prefix), str(raised.exception))
                for path, data in before.items():
                    self.assertEqual(path.read_bytes(), data, path)

    def test_zero_exit_skip_or_early_return_text_is_not_positive_evidence(self) -> None:
        product = self._product_tree()
        before = (product / RESOURCES_RELATIVE / "catalog" / "native-contract.json").read_bytes()
        output = "\n".join(
            "SKIPPED before " + prefix for prefix in upgrade.ESSENTIAL_EVIDENCE_PREFIXES
        ) + "\nRan 42 tests\nOK (skipped=4)\n"
        with self.assertRaisesRegex(UpgradeError, "completion evidence is missing"):
            self.repin(checkout_root=product, native_contract=self.contract,
                       today="2026-07-24", runner=self._runner(0, output))
        self.assertEqual((product / RESOURCES_RELATIVE / "catalog" / "native-contract.json").read_bytes(), before)

    def test_skill_policy_case_skip_refuses_promotion(self) -> None:
        """The S14 lead proof cannot stand in for a mandatory S14 case that
        skipped as INCONCLUSIVE (unittest still exits zero); every case's own
        completion record is required."""

        s14 = [p for p in upgrade.ESSENTIAL_EVIDENCE_PREFIXES if p.startswith("client probe S14")]
        self.assertEqual(len(s14), 8)
        for index, skipped in enumerate(s14):
            if skipped.startswith("client probe S14: "):
                continue
            with self.subTest(skipped=skipped):
                product = self._product_tree(f"repo-s14-{index}")
                probe_id = skipped.split(":")[0].removeprefix("client probe ")
                output = "\n".join(
                    [prefix + "ok" for prefix in upgrade.ESSENTIAL_EVIDENCE_PREFIXES if prefix != skipped]
                    + [f"client probe {probe_id}: INCONCLUSIVE live daemon domain touched bin=fixture",
                       "Ran 42 tests", "OK (skipped=1)"]
                ) + "\n"
                with self.assertRaisesRegex(UpgradeError, "completion evidence is missing") as raised:
                    self.repin(checkout_root=product, native_contract=self.contract,
                               today="2026-07-24", runner=self._runner(0, output))
                self.assertIn(repr(skipped), str(raised.exception))

    def test_nonzero_exit_rejects_even_with_all_completion_records(self) -> None:
        product = self._product_tree()
        with self.assertRaisesRegex(UpgradeError, "evidence suite failed"):
            self.repin(checkout_root=product, native_contract=self.contract,
                       today="2026-07-24", runner=self._runner(1))

    def test_completion_records_must_start_at_line_boundary(self) -> None:
        product = self._product_tree()
        output = "\n".join(
            "prefix-noise " + prefix + "not-anchored" for prefix in upgrade.ESSENTIAL_EVIDENCE_PREFIXES
        ) + "\nRan 42 tests\n"
        with self.assertRaisesRegex(UpgradeError, "completion evidence is missing"):
            self.repin(checkout_root=product, native_contract=self.contract,
                       today="2026-07-24", runner=self._runner(0, output))

    def test_missing_checkout_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            UpgradeError, "version.json is unreadable or incomplete.*pass --repo with a 2.3.1 checkout"
        ):
            self.repin(checkout_root=self.root / "elsewhere", native_contract=self.contract,
                       today="2026-07-24", runner=self._runner(0))

    def test_already_current(self) -> None:
        self.new.unlink()
        product = self._product_tree()
        outcome = self.repin(checkout_root=product, native_contract=self.contract,
                             today="2026-07-24", runner=self._runner(0))
        self.assertEqual(outcome.kind, "current")
        self.assertIn("nothing to re-pin", outcome.messages[0])


class NoBuildOrInstallTests(UpgradeTestCase):
    """A re-pin promotes into the checkout and runs its evidence suite;
    it never builds or installs anything (the pin ships with a release)."""

    def recording_runner(self, calls):
        def run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, self._successful_evidence_output(), "")
        return run

    def test_only_the_evidence_suite_runs(self) -> None:
        product = self._product_tree()
        calls: list[list[str]] = []
        outcome = self.repin(checkout_root=product, native_contract=self.contract, today="2026-07-24",
                             runner=self.recording_runner(calls))
        self.assertEqual(outcome.kind, "prepared")
        self.assertEqual([call[1:5] for call in calls], [["-m", "unittest", "discover", "-s"]])
        hint = next(m for m in outcome.messages if m.startswith("install:"))
        self.assertIn("next release built from this checkout", hint)
        self.assertIn("builds and installs nothing", hint)
        self.assertFalse(any("activat" in message for message in outcome.messages))

    def test_the_activation_options_are_gone(self) -> None:
        for retired in ("activate", "flake_target", "activation_plan", "activation_ask", "activation_commit"):
            with self.subTest(retired), self.assertRaises(TypeError):
                upgrade.run_repin(checkout_root=self.root, native_contract=self.contract, today="2026-07-24",
                                  env=self.env, **{retired: True})
        self.assertFalse(hasattr(upgrade, "RELEASE_LINK"))


class RepinLockWaitNoteTests(UpgradeTestCase):
    """A contended repin says so instead of sitting silent."""

    def test_waiting_note_emitted_when_lock_held(self) -> None:
        import threading

        self.new.unlink()
        product = self._product_tree()
        blocker = state.FileLock(upgrade._lock_path(self.env))
        blocker.acquire(blocking=True)
        notes: list[str] = []
        outcome_holder: list[object] = []

        def _run() -> None:
            outcome_holder.append(self.repin(checkout_root=product, native_contract=self.contract,
                                             today="2026-07-27", runner=_unused_runner,
                                             progress=notes.append))

        worker = threading.Thread(target=_run)
        worker.start()
        try:
            deadline = time.time() + 5
            while not notes and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(any("waiting" in note for note in notes))
        finally:
            blocker.release()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome_holder[0].kind, "current")
        self.assertEqual(upgrade._lock_path(self.env).parent, self.root / "state/claude-multi/locks")


def _unused_runner(*args, **kwargs):  # pragma: no cover - never reached
    raise AssertionError("runner must not run for the current path")


class HeartbeatTests(unittest.TestCase):
    def test_beats_and_stops_cleanly(self) -> None:
        notes: list[str] = []
        with upgrade._Heartbeat(notes.append, "  [3/4] evidence suite still running", interval=0.02):
            time.sleep(0.07)
        self.assertTrue(notes)
        self.assertIn("evidence suite still running", notes[0])
        self.assertIn("elapsed", notes[0])
        count_at_stop = len(notes)
        time.sleep(0.06)
        # The thread is stopped on exit: no further beats.
        self.assertEqual(len(notes), count_at_stop)

    def test_noop_without_progress(self) -> None:
        with upgrade._Heartbeat(None, "phase", interval=0.01):
            time.sleep(0.03)


class SyncAnchoringTests(UpgradeTestCase):
    """Prefix-colliding pins must never mangle decoupled literals."""

    def test_prefix_collision_does_not_mangle_other_versions(self) -> None:
        product = self._product_tree()
        pinned = (
            'validated = "2.1.2"\n'
            'default_floor = floors.get(model_id, "2.1.216")\n'
            '# 2.1.216 remains the truthful minimum floor\n'
            'fixture = "2.1.300"\n'
        )
        target = product / "tests" / "test_catalog.py"
        target.write_text(pinned, encoding="utf-8")
        backups: dict = {}
        synced = upgrade._sync_pinned_test_literals(
            product,
            old_version="2.1.2",
            new_version="2.1.20",
            old_sha256=None,
            new_sha256="b" * 64,
            old_inspected_at=None,
            new_inspected_at="2026-07-27",
            old_catalog_version=3,
            final_catalog_version=4,
            backups=backups,
        )
        text = target.read_text(encoding="utf-8")
        self.assertEqual(synced, ["tests/test_catalog.py"])
        self.assertIn('validated = "2.1.20"', text)
        self.assertIn('"2.1.216"', text)          # never mangled to 2.1.2016
        self.assertIn('"2.1.300"', text)          # never mangled to 2.1.2000
        self.assertIn("2.1.216 remains", text)    # comments anchored too
        self.assertNotIn("2.1.2016", text)
        self.assertNotIn("2.1.200", text.replace("2.1.300", ""))

    def test_floors_get_lines_are_never_synced(self) -> None:
        product = self._product_tree()
        pinned = (
            'validated = "2.1.217"\n'
            'floors = {"opus5": "2.1.217"}\n'
            'x = floors.get(model_id, "2.1.217")\n'
        )
        target = product / "tests" / "test_catalog.py"
        target.write_text(pinned, encoding="utf-8")
        upgrade._sync_pinned_test_literals(
            product,
            old_version="2.1.217",
            new_version="2.1.218",
            old_sha256=None,
            new_sha256="b" * 64,
            old_inspected_at=None,
            new_inspected_at="2026-07-27",
            old_catalog_version=3,
            final_catalog_version=4,
            backups={},
        )
        text = target.read_text(encoding="utf-8")
        self.assertIn('validated = "2.1.218"', text)
        self.assertIn('floors = {"opus5": "2.1.217"}', text)
        self.assertIn('floors.get(model_id, "2.1.217")', text)


class CatalogBumpTests(UpgradeTestCase):
    """``update`` bumps ``catalog_version`` only when the
    checkout equals the running release's; the literal sync takes the old and
    final versions explicitly (never ``old = final - 1``); a failed evidence
    run restores every byte; the per-model minimum floors never move."""

    PINNED = (
        'validated = "2.1.217"\n'
        'floors = {"opus5": "2.1.217"}  # decoupled minimum\n'
        'x = floors.get(model_id, "2.1.217")\n'
        'self.assertEqual(bundle.docs["version"]["catalog_version"], {cv})\n'
    )

    def _tree(self, name: str, catalog_version: int) -> tuple[Path, Path]:
        product = self._product_tree(name)
        version = product / RESOURCES_RELATIVE / "version.json"
        doc = json.loads(version.read_bytes())
        doc["catalog_version"] = catalog_version
        version.write_text(json.dumps(doc) + "\n", encoding="utf-8")
        target = product / "tests" / "test_catalog.py"
        target.write_text(self.PINNED.replace("{cv}", str(catalog_version)), encoding="utf-8")
        return product, target

    def test_equal_bumps_ahead_does_not(self) -> None:
        running = FIXTURE_RELEASE.catalog_version
        for label, checkout, final in (("equal", running, running + 1), ("ahead", running + 1, running + 1)):
            with self.subTest(label):
                product, target = self._tree(label, checkout)
                outcome = self.repin(
                    checkout_root=product, native_contract=self.contract,
                    today="2026-10-01", runner=self._runner(0),
                )
                self.assertEqual(outcome.kind, "prepared")
                self.assertEqual(json.loads((product / RESOURCES_RELATIVE / "version.json").read_bytes())["catalog_version"], final)
                text = target.read_text(encoding="utf-8")
                self.assertIn(f'"catalog_version"], {final})', text)
                self.assertNotIn(f'"catalog_version"], {final + 1})', text)
                self.assertIn('validated = "2.1.218"', text)
                self.assertIn('floors = {"opus5": "2.1.217"}', text)
                self.assertIn('floors.get(model_id, "2.1.217")', text)
                self.assertIn(f"(catalog_version {final})", "\n".join(outcome.messages))
        self.assertEqual(upgrade.promoted_catalog_version(36, 35), 36)
        self.assertEqual(upgrade.promoted_catalog_version(35, 35), 36)
        with self.assertRaisesRegex(UpgradeError, "older than the running release"):
            upgrade.promoted_catalog_version(34, 35)

    def test_explicit_old_final_literal_sync(self) -> None:
        product = self._product_tree()
        target = product / "tests" / "test_catalog.py"
        for old, final, expected in ((36, 36, 36), (36, 37, 37), (35, 37, 37)):
            with self.subTest(old=old, final=final):
                text = (f'self.assertEqual(bundle.docs["version"]["catalog_version"], {old})\n'
                        f'self.assertEqual(other["catalog_version"], {old - 1})\n')
                target.write_text(text, encoding="utf-8")
                upgrade._sync_pinned_test_literals(
                    product, old_version="2.1.217", new_version="2.1.218", old_sha256=None,
                    new_sha256="b" * 64, old_inspected_at=None, new_inspected_at="2026-10-01",
                    old_catalog_version=old, final_catalog_version=final, backups={},
                )
                after = target.read_text(encoding="utf-8")
                self.assertIn(f'"catalog_version"], {expected})', after)
                # final - 1 is never inferred to be the old literal.
                self.assertIn(f'other["catalog_version"], {old - 1})', after)

    def test_failed_evidence_restores_all_bytes(self) -> None:
        running = FIXTURE_RELEASE.catalog_version
        for label, checkout in (("equal", running), ("ahead", running + 1)):
            for runner in (self._runner(1, "FAILED"), self._runner(0, "Ran 1 tests\n")):
                with self.subTest(label, runner=runner):
                    product, target = self._tree(f"{label}-{id(runner)}", checkout)
                    before = {path: path.read_bytes() for path in (
                        product / RESOURCES_RELATIVE / "catalog" / "native-contract.json", product / RESOURCES_RELATIVE / "version.json", target)}
                    with self.assertRaises(UpgradeError):
                        self.repin(
                            checkout_root=product, native_contract=self.contract, today="2026-10-01", runner=runner,
                        )
                    for path, data in before.items():
                        self.assertEqual(path.read_bytes(), data, path)


class CandidateFallbackTests(UpgradeTestCase):
    """An invalid highest-named artifact is skipped, not fatal."""

    def test_invalid_highest_entry_falls_back_with_a_note(self) -> None:
        bogus = self._write_version("2.1.300")
        bogus.chmod(0o644)  # not executable -> fails inspection
        product = self._product_tree()
        outcome = self.repin(
            checkout_root=product,
            native_contract=self.contract,
            today="2026-07-27",
            runner=self._runner(0),
        )
        self.assertEqual(outcome.kind, "prepared")
        self.assertEqual(outcome.inspection.version, "2.1.218")
        self.assertTrue(any("skipped unusable" in line for line in outcome.messages))
        self.assertTrue(any("2.1.300" in line for line in outcome.messages))

    def test_all_candidates_invalid_reports_skips(self) -> None:
        self.new.chmod(0o644)
        product = self._product_tree()
        outcome = self.repin(
            checkout_root=product,
            native_contract=self.contract,
            today="2026-07-27",
            runner=self._runner(0),
        )
        self.assertEqual(outcome.kind, "current")
        self.assertTrue(any("skipped unusable" in line for line in outcome.messages))


class RepoFileWriteTests(unittest.TestCase):
    """Promotion writes are crash-atomic and preserve repo modes."""

    def test_write_repo_file_preserves_mode_and_content(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="claude-multi-repofile-"))
        self.addCleanup(shutil.rmtree, root, True)
        target = root / "file.json"
        target.write_text("old\n", encoding="utf-8")
        target.chmod(0o644)
        upgrade._write_repo_file(target, b"new\n")
        self.assertEqual(target.read_bytes(), b"new\n")
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o644)
        self.assertEqual(list(root.glob("*.tmp-*")), [])

    def test_write_repo_file_new_file_gets_644(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="claude-multi-repofile-"))
        self.addCleanup(shutil.rmtree, root, True)
        target = root / "created.json"
        upgrade._write_repo_file(target, b"x\n")
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o644)


class CandidateOrderingTests(UpgradeTestCase):
    """Retained versions order by version key, never lexically."""

    def test_narrow_digit_versions_order_numerically(self) -> None:
        self.contract["verified"][0]["version"] = "2.1.50"
        # Neutralize the symlink: it must resolve to the pin (not a candidate).
        pinned = self._write_version("2.1.50")
        self.link.unlink()
        self.link.symlink_to(pinned)
        old = self._write_version("2.1.99")
        newer = self._write_version("2.1.218")
        ordered = upgrade.find_candidates(self.contract, self.env)
        # Retained versions are newest-first BY VERSION KEY: 2.1.218 > 2.1.217
        # > 2.1.99 — lexically, 2.1.99 would incorrectly sort first.
        self.assertEqual(ordered, [newer, self.old, old])

    def test_restore_writes_are_mode_preserving_atomic(self) -> None:
        product = self._product_tree()
        target = product / RESOURCES_RELATIVE / "catalog" / "native-contract.json"
        target.chmod(0o644)
        before_mode = stat.S_IMODE(os.lstat(target).st_mode)
        with self.assertRaisesRegex(UpgradeError, "evidence suite failed"):
            self.repin(
                checkout_root=product,
                native_contract=self.contract,
                today="2026-07-27",
                runner=self._runner(1, "FAILED"),
            )
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), before_mode)
        self.assertEqual(
            target.read_bytes(), json.dumps(self.contract, indent=2).encode() + b"\n"
        )
        self.assertEqual(list(product.glob("**/*.tmp-*")), [])


class CheckoutContainmentTests(UpgradeTestCase):
    """Repin writes only into the verified checkout's own resources: a
    symlinked resource directory never redirects a write."""

    def test_a_symlinked_resource_directory_refuses_before_any_write(self) -> None:
        product = self._product_tree()
        outside = self.root / "outside-data"
        shutil.move(str(product / RESOURCES_RELATIVE), str(outside))
        (product / RESOURCES_RELATIVE).symlink_to(outside, target_is_directory=True)
        before = {path: path.read_bytes() for path in sorted(outside.rglob("*")) if path.is_file()}
        runner = mock.Mock()
        with self.assertRaisesRegex(UpgradeError, "not a real directory inside the checkout"):
            self.repin(checkout_root=product, native_contract=self.contract, today="2026-07-24", runner=runner)
        runner.assert_not_called()  # no evidence suite
        self.assertEqual({path: path.read_bytes() for path in sorted(outside.rglob("*")) if path.is_file()}, before)

    def test_a_symlinked_catalog_directory_refuses_too(self) -> None:
        product = self._product_tree()
        catalog_dir = product / RESOURCES_RELATIVE / "catalog"
        outside = self.root / "outside-catalog"
        shutil.move(str(catalog_dir), str(outside))
        catalog_dir.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(UpgradeError, "not a real directory inside the checkout"):
            self.repin(checkout_root=product, native_contract=self.contract, today="2026-07-24",
                       runner=self._runner(0))
        self.assertEqual(_pinned(json.loads((outside / "native-contract.json").read_bytes())), "2.1.217")

    def test_writes_outside_the_checkout_are_refused(self) -> None:
        product = self._product_tree()
        for path in (self.root / "elsewhere.json", product / ".." / "escape.json"):
            with self.subTest(path=path), self.assertRaisesRegex(UpgradeError, "refused"):
                upgrade._write_checkout_file(product, path, b"x")
        self.assertFalse((self.root / "elsewhere.json").exists())

    def test_the_fingerprint_reads_the_checkout_resources_under_stable_labels(self) -> None:
        product = self._product_tree()
        before = upgrade._checkout_fingerprint(product)
        # A stray repository-level copy is not the checkout's resource.
        (product / "version.json").write_text("{}\n")
        self.assertEqual(upgrade._checkout_fingerprint(product), before)
        (product / RESOURCES_RELATIVE / "version.json").write_text("{}\n")
        self.assertNotEqual(upgrade._checkout_fingerprint(product), before)

    def test_the_default_checkout_needs_an_installation_tree(self) -> None:
        self.assertEqual(upgrade.default_checkout(), REPO_ROOT)
        with mock.patch("claude_multi.layout.installation", return_value=None), \
                self.assertRaisesRegex(UpgradeError, "pass --repo PATH"):
            upgrade.default_checkout()


class RunningReleaseTests(unittest.TestCase):
    def test_a_resource_override_never_redefines_the_running_release(self) -> None:
        import claude_multi

        packaged = upgrade.ReleaseIdentity.from_root(claude_multi.resources_root())
        with mock.patch.dict(os.environ, {"CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT)}):
            self.assertEqual(upgrade.running_release(), packaged)
        self.assertEqual(packaged.launcher_version, claude_multi.__version__)


class RepoFileSymlinkTests(unittest.TestCase):
    def test_write_repo_file_refuses_symlinked_target(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="claude-multi-symlink-"))
        self.addCleanup(shutil.rmtree, root, True)
        real = root / "real"
        real.write_text("x\n", encoding="utf-8")
        link = root / "link"
        link.symlink_to(real)
        with self.assertRaisesRegex(UpgradeError, "symlinked"):
            upgrade._write_repo_file(link, b"y\n")
        self.assertEqual(real.read_text(encoding="utf-8"), "x\n")


class RealSyncedLiteralTests(unittest.TestCase):
    """The sync still finds every literal it must move.

    Reads the REAL ``tests/test_catalog.py`` / ``tests/test_native_contract.py``
    (the files ``_SYNC_TEST_FILES`` names) and the shipped contract and
    version.json, then runs ``_sync_pinned_test_literals`` on temp COPIES:
    every synced literal kind must be present and must be rewritten, the
    decoupled ``floors`` lines must stay byte-identical, and the real files
    are never written. A future edit that hides a pin from the sync (or moves
    a test off the shipped literals) fails here instead of at the next
    ``claude-multi-dev repin``. Values are derived, never pinned in this file.
    """

    def test_sync_finds_and_rewrites_every_real_pinned_literal(self) -> None:
        from _catalog import SHIPPED_ROOT
        from _layout import REPO_ROOT, RESOURCES_ROOT

        contract = json.loads(
            (SHIPPED_ROOT / "catalog" / "native-contract.json").read_text(encoding="utf-8")
        )
        version_doc = json.loads((SHIPPED_ROOT / "version.json").read_text(encoding="utf-8"))
        old_version = pin.version(contract)
        old_sha = contract["verified"][0]["platforms"]["linux-x64"]["sha256"]
        old_size = contract["verified"][0]["platforms"]["linux-x64"]["size"]
        old_inspected = contract["verified"][0]["verified_at"]
        old_catalog = int(version_doc["catalog_version"])
        new_version = "99.0.0"
        new_sha = "f" * 64
        new_size = old_size + 1
        new_inspected = "2099-01-01"
        old_platforms = contract["verified"][0]["platforms"]
        new_platforms = {name: {"sha256": f"{index:x}" * 64, "size": record["size"] + 1}
                         for index, (name, record) in enumerate(sorted(old_platforms.items()), start=1)}
        new_platforms["linux-x64"] = {"sha256": new_sha, "size": new_size}

        root = Path(tempfile.mkdtemp(prefix="claude-multi-sync-real-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        product = root / "claude-multi"
        (product / "tests").mkdir(parents=True)
        originals: dict[str, bytes] = {}
        for relative in upgrade._SYNC_TEST_FILES:
            data = (REPO_ROOT / relative).read_bytes()
            originals[relative] = data
            (product / relative).write_bytes(data)

        version_literal = f'"{old_version}"'
        catalog_literal = f'"catalog_version"], {old_catalog})'
        catalog_text = originals["tests/test_catalog.py"].decode("utf-8")
        contract_text = originals["tests/test_native_contract.py"].decode("utf-8")
        for name, text, needle in (
            ("test_catalog version", catalog_text, version_literal),
            ("test_catalog sha256", catalog_text, old_sha),
            ("test_catalog verified_at", catalog_text, f'"{old_inspected}"'),
            ("test_catalog catalog_version", catalog_text, catalog_literal),
            ("test_catalog linux-x64 size", catalog_text, f'["linux-x64"]["size"], {old_size})'),
            ("test_native_contract validated_version", contract_text, version_literal),
        ):
            with self.subTest(literal=name):
                self.assertIn(needle, text, f"{name}: synced literal not found")

        backups: dict = {}
        synced = upgrade._sync_pinned_test_literals(
            product,
            old_version=old_version,
            new_version=new_version,
            old_sha256=old_sha,
            new_sha256=new_sha,
            old_inspected_at=old_inspected,
            new_inspected_at=new_inspected,
            old_catalog_version=old_catalog,
            final_catalog_version=old_catalog + 1,
            backups=backups,
            old_platforms=old_platforms,
            new_platforms=new_platforms,
        )
        self.assertEqual(synced, list(upgrade._SYNC_TEST_FILES))
        self.assertEqual(
            set(backups), {product / relative for relative in upgrade._SYNC_TEST_FILES}
        )

        rewritten = {
            relative: (product / relative).read_text(encoding="utf-8")
            for relative in upgrade._SYNC_TEST_FILES
        }
        catalog_after = rewritten["tests/test_catalog.py"]
        contract_after = rewritten["tests/test_native_contract.py"]
        # Nothing pinned to the old contract survives outside floors lines.
        for relative, text in rewritten.items():
            for line in text.splitlines():
                if "floors" in line:
                    continue
                with self.subTest(file=relative, line=line.strip()):
                    self.assertNotIn(version_literal, line)
                    self.assertNotIn(old_sha, line)
                    self.assertNotIn(f'"{old_inspected}"', line)
                    self.assertNotIn(catalog_literal, line)
        self.assertIn(f'"{new_version}"', catalog_after)
        self.assertIn(new_sha, catalog_after)
        self.assertIn(f'"{new_inspected}"', catalog_after)
        self.assertIn(f'"catalog_version"], {old_catalog + 1})', catalog_after)
        self.assertIn(f'["linux-x64"]["size"], {new_size})', catalog_after)
        self.assertNotIn(f'["linux-x64"]["size"], {old_size})', catalog_after)
        self.assertIn(f'"{new_version}"', contract_after)
        # Decoupled per-model floors are never synced.
        for relative in upgrade._SYNC_TEST_FILES:
            before = [
                line
                for line in originals[relative].decode("utf-8").splitlines()
                if "floors" in line
            ]
            after = [line for line in rewritten[relative].splitlines() if "floors" in line]
            self.assertEqual(before, after)
        # The synced shipped-facts test passes against a candidate contract
        # whose hash and size both moved; the unsynced one fails on it.
        candidate = copy.deepcopy(contract)
        candidate["verified"][0].update(version=new_version, verified_at=new_inspected,
                                        platforms=copy.deepcopy(new_platforms))
        self.assertTrue(self._shipped_facts_pass(product / "tests/test_catalog.py", candidate))
        self.assertFalse(self._shipped_facts_pass(REPO_ROOT / "tests/test_catalog.py", candidate))
        self.assertTrue(self._shipped_facts_pass(REPO_ROOT / "tests/test_catalog.py", contract))
        # The real files were only read.
        for relative, data in originals.items():
            self.assertEqual((REPO_ROOT / relative).read_bytes(), data)

    def _shipped_facts_pass(self, test_file: Path, native_contract: dict) -> bool:
        """Run ``test_catalog``'s shipped-facts test from ``test_file``
        against ``native_contract`` as the shipped one."""

        import importlib.util
        import types

        spec = importlib.util.spec_from_file_location(f"_synced_catalog_{id(native_contract)}_{len(str(test_file))}",
                                                      test_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        shipped = types.SimpleNamespace(docs={"native-contract": native_contract})
        result = unittest.TestResult()
        with mock.patch.object(catalog, "load_catalog", return_value=shipped):
            module.NativeContractTests("test_promoted_executable_facts_locked").run(result)
        return result.wasSuccessful() and result.testsRun == 1


class CheckoutGuardTests(UpgradeTestCase):
    def rewrite_version(self, product, **changes):
        path = product / RESOURCES_RELATIVE / "version.json"
        doc = json.loads(path.read_bytes())
        doc.update(changes)
        path.write_text(json.dumps(doc))

    def test_guard_matrix(self):
        for i, (changes, message) in enumerate((
            ({}, None),
            ({"launcher_version": "2.3.2"}, "exactly the running release"),
            ({"launcher_version": "2.3.0"}, "own release"),
            ({"launcher_version": "2.4.0"}, "own release"),
            ({"launcher_version": "3.3.1"}, "own release"),
            ({"launcher_version": "2.3.1-dev"}, "exactly the running release"),
            ({"version": 2}, "uses data version 2"),
            ({"catalog_version": 2}, "older than this launcher's 3"),
            ({"catalog_version": 4}, None),
        )):
            with self.subTest(changes=changes):
                product = self._product_tree(f"repo-{i}")
                self.rewrite_version(product, **changes)
                if message is None:
                    upgrade.checkout_guard(product, FIXTURE_RELEASE, writes=True)
                else:
                    with self.assertRaisesRegex(UpgradeError, message):
                        upgrade.checkout_guard(product, FIXTURE_RELEASE, writes=True)

    def test_dev_writes_allowed(self):
        product = self._product_tree()
        self.rewrite_version(product, launcher_version="2.3.1-dev")
        running = upgrade.ReleaseIdentity.from_root(product / RESOURCES_RELATIVE)
        upgrade.checkout_guard(product, running, writes=True)

    def test_w0_malformed_missing_unreadable_and_ill_typed(self):
        product = self._product_tree()
        path = product / RESOURCES_RELATIVE / "version.json"
        valid = json.loads(path.read_bytes())
        cases = [b'[]', b'{', b'null']
        for key in valid:
            doc = dict(valid)
            del doc[key]
            cases.append(json.dumps(doc).encode())
        for key, value in (("launcher_version", "2.3.1-beta"), ("launcher_version", 231),
                           ("catalog_version", True), ("version", 1.0)):
            cases.append(json.dumps({**valid, key: value}).encode())
        for raw in cases:
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaisesRegex(UpgradeError, "unreadable or incomplete"):
                    upgrade.checkout_guard(product, FIXTURE_RELEASE, writes=True)
        path.unlink()
        with self.assertRaisesRegex(UpgradeError, "unreadable or incomplete"):
            upgrade.ReleaseIdentity.from_root(product)

    def test_missing_version_document_names_matching_checkout_remedy(self):
        product = self._product_tree()
        (product / RESOURCES_RELATIVE / "version.json").unlink()
        with self.assertRaisesRegex(UpgradeError, "pass --repo with a 2.3.1 checkout"):
            upgrade.checkout_guard(product, FIXTURE_RELEASE, writes=True)

    def test_schema_only_required_for_writes(self):
        product = self._product_tree()
        path = product / RESOURCES_RELATIVE / "schemas/native-contract.schema.json"
        path.write_bytes(b"different")
        with self.assertRaisesRegex(UpgradeError, "schema differs"):
            upgrade.checkout_guard(product, FIXTURE_RELEASE, writes=True)
        path.unlink()
        with self.assertRaisesRegex(UpgradeError, "schema.json is missing"):
            upgrade.checkout_guard(product, FIXTURE_RELEASE, writes=True)
        upgrade.checkout_guard(product, FIXTURE_RELEASE, writes=False)

    def test_from_the_packaged_resources(self):
        from _layout import REPO_ROOT, RESOURCES_ROOT
        identity = upgrade.ReleaseIdentity.from_root(RESOURCES_ROOT)
        self.assertEqual(identity.launcher_version, json.loads((RESOURCES_ROOT / "version.json").read_bytes())["launcher_version"])
        self.assertEqual(len(identity.contract_schema_sha256), 64)

    def test_refusal_preserves_checkout_and_never_executes_candidate(self):
        product = self._product_tree()
        self.rewrite_version(product, launcher_version="2.3.1-dev")
        marker = self.root / "executed"
        self.new.write_text(f'#!/bin/sh\n: > "{marker}"\n')
        before = {p: p.read_bytes() for p in product.rglob("*") if p.is_file()}
        runner, verifier = mock.Mock(), mock.Mock()
        notes = []
        with self.assertRaisesRegex(UpgradeError, "exactly the running release"):
            self.repin(checkout_root=product, native_contract=self.contract,
                                today="2026-09-27", runner=runner,
                                release_verifier=verifier, progress=notes.append)
        self.assertEqual(before, {p: p.read_bytes() for p in product.rglob("*") if p.is_file()})
        self.assertFalse(marker.exists())
        self.assertFalse(notes)
        runner.assert_not_called()
        verifier.assert_not_called()

    def test_current_is_unguarded(self):
        self.new.unlink()
        product = self._product_tree()
        self.rewrite_version(product, launcher_version="2.3.2")
        runner = mock.Mock(side_effect=AssertionError("nothing runs"))
        kwargs = dict(checkout_root=product, native_contract=self.contract,
                      today="2026-09-27", runner=runner)
        self.assertEqual(self.repin(**kwargs).kind, "current")
        runner.assert_not_called()


class ReleaseEvidenceTests(UpgradeTestCase):
    def test_mismatch_skips_without_executing_and_falls_back(self):
        bad = self._write_version("2.1.999")
        marker = self.root / "executed"
        bad.write_text(f'#!/bin/sh\n: > "{marker}"\n')
        def verify(version, digest, size):
            if version == bad.name:
                raise release_manifest.ReleaseManifestMismatch("sha256 differs")
            return fake_release(version, digest, size)
        outcome = self.repin(checkout_root=self._product_tree(), native_contract=self.contract,
                                      today="2026-09-27",
                                      runner=self._runner(0), release_verifier=verify)
        self.assertFalse(marker.exists())
        self.assertEqual(outcome.inspection.version, self.new.name)
        self.assertIn("release manifest mismatch (sha256 differs)", "\n".join(outcome.messages))

    def test_unavailable_aborts_before_execution_or_mutation(self):
        product = self._product_tree()
        before = {p: p.read_bytes() for p in product.rglob("*") if p.is_file()}
        marker = self.root / "executed"
        self.new.write_text(f'#!/bin/sh\n: > "{marker}"\n')
        verifier = mock.Mock(side_effect=release_manifest.ReleaseManifestUnavailable("network: timeout"))
        runner = mock.Mock()
        with self.assertRaisesRegex(UpgradeError, "nothing was changed.*claude-multi-dev repin --manifest-dir DIR") as caught:
            self.repin(checkout_root=product, native_contract=self.contract,
                                today="2026-09-27", runner=runner, release_verifier=verifier)
        self.assertIn("repin stopped: cannot verify", str(caught.exception))
        self.assertFalse(marker.exists())
        self.assertEqual(before, {p: p.read_bytes() for p in product.rglob("*") if p.is_file()})
        runner.assert_not_called()

    def test_provenance_and_release_line_once(self):
        product = self._product_tree()
        outcome = self.repin(checkout_root=product, native_contract=self.contract,
                                      today="2026-09-27", runner=self._runner(0))
        entry = json.loads((product / RESOURCES_RELATIVE / "catalog/native-contract.json").read_bytes())["verified"][0]
        self.assertEqual((entry["manifest_sha256"], entry["key_fingerprint"]), ("b" * 64, release_manifest.KEY_FINGERPRINT))
        lines = [m for m in outcome.messages if m.startswith("release manifest:")]
        self.assertEqual(len(lines), 1)
        self.assertTrue(outcome.messages[outcome.messages.index(lines[0]) - 1].startswith("inspected offline:"))

    def test_default_verifier_selects_offline_or_online_source(self):
        # Call the real factory despite the test-case's default injection.
        with mock.patch.object(release_manifest, "fetch", return_value=(b"m", b"s")) as fetch, \
             mock.patch.object(release_manifest, "load_local", return_value=(b"m", b"s")) as local, \
             mock.patch.object(release_manifest, "gpg_executable", return_value="fixture-gpg") as gpg, \
             mock.patch.object(release_manifest, "verify_release") as verify:
            verify.side_effect = lambda v, h, s, **kw: (kw["source"](v), fake_release(v, h, s))[1]
            # The network source needs the candidate's own consent.
            plans = []
            REAL_RELEASE_VERIFIER(None, {"PATH": "/fixture"}, consent=lambda plan: plans.append(plan) or True)(
                "1.2.3", "a" * 64, 10)
            fetch.assert_called_once_with("1.2.3")
            local.assert_not_called()
            self.assertEqual(plans, [release_manifest.request_plan("1.2.3")])
            fetch.reset_mock()
            REAL_RELEASE_VERIFIER(self.root, {"PATH": "/fixture"})("1.2.3", "a" * 64, 10)
            local.assert_called_once_with(self.root, "1.2.3")
            fetch.assert_not_called()
            gpg.assert_called_with({"PATH": "/fixture"})
            with self.assertRaises(release_manifest.ReleaseFetchDeclined):
                REAL_RELEASE_VERIFIER(None, {"PATH": "/fixture"})("1.2.3", "a" * 64, 10)
            fetch.assert_not_called()


REAL_FETCH = release_manifest.fetch


class RepinConsentTests(UpgradeTestCase):
    """Per-candidate request plans, lock-free fetch/verify, revalidation
    inside the repin transaction."""

    def setUp(self) -> None:
        super().setUp()
        self.product = self._product_tree()
        self.lock_target = upgrade._lock_path(self.env)
        self.events: list[tuple[str, str]] = []
        self.bad: set[str] = set()
        for patcher in (
            mock.patch.object(upgrade, "_release_verifier", REAL_RELEASE_VERIFIER),
            mock.patch.object(release_manifest, "gpg_executable", return_value="fixture-gpg"),
            mock.patch.object(release_manifest, "verify_signature", side_effect=self._signature),
            mock.patch.object(release_manifest, "fetch",
                              side_effect=lambda version: REAL_FETCH(version, opener=self._opener)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _signature(self, manifest, signature, **kwargs):
        self.events.append(("verify", json.loads(manifest)["version"]))
        return release_manifest.KEY_FINGERPRINT

    def _manifest(self, version: str) -> bytes:
        path = self.versions / version
        data = path.read_bytes()
        checksum = hashlib.sha256(data).hexdigest() if version not in self.bad else "0" * 64
        return json.dumps({"version": version, "platforms": {release_manifest.platform_key(): {
            "checksum": checksum, "size": len(data)}}}).encode()

    def _opener(self, url, **kwargs):
        self.check_unlocked(f"GET {url}")
        version = url.split("/")[-2]
        self.events.append(("GET", url))
        stream = __import__("io").BytesIO(self._manifest(version) if url.endswith(".json") else b"signature")
        stream.geturl = lambda: url
        return stream

    def check_unlocked(self, where: str) -> None:
        """No mutation lock is held: the repin transaction's lock is free."""

        if getattr(self, "contended", False):
            return  # another repin holds it by design
        probe = state.FileLock(self.lock_target)
        self.assertTrue(probe.acquire(blocking=False), f"repin lock held during {where}")
        probe.release()

    def consent(self, *answers: bool):
        replies = list(answers)

        def ask(plan):
            self.check_unlocked(f"the consent for {plan.version}")
            self.events.append(("ask", plan.version))
            self.assertEqual(plan, release_manifest.request_plan(plan.version))
            return replies.pop(0)
        return ask

    def run_update(self, **kwargs):
        kwargs.setdefault("runner", self._runner(0))
        return self.repin(checkout_root=self.product, native_contract=self.contract,
                                   today="2026-10-01", **kwargs)

    def snapshot(self):
        return {p: p.read_bytes() for p in self.product.rglob("*") if p.is_file()}

    def test_manifest_fetch_decline_sends_zero_requests(self):
        marker = self.root / "executed"
        self.new.write_text(f'#!/bin/sh\n: > "{marker}"\n')
        before = self.snapshot()
        runner = mock.Mock()
        outcome = self.run_update(fetch_consent=self.consent(False), runner=runner)
        self.assertEqual(outcome.kind, "fetch-declined")
        self.assertEqual(self.events, [("ask", "2.1.218")])  # zero GETs, nothing verified
        self.assertTrue(outcome.messages[0].startswith(upgrade.FETCH_DECLINED))
        self.assertIn("--manifest-dir DIR", outcome.messages[0])
        self.assertFalse(marker.exists())
        runner.assert_not_called()
        self.assertEqual(before, self.snapshot())
        # No consent callback at all: still nothing is sent.
        self.events.clear()
        self.assertEqual(self.run_update(runner=runner).kind, "fetch-declined")
        self.assertEqual(self.events, [])

    def test_next_candidate_requires_new_call_plan(self):
        bad = self._write_version("2.1.999")
        self.bad.add(bad.name)
        outcome = self.run_update(fetch_consent=self.consent(True, True))
        bad_url = release_manifest.MANIFEST_URL.format(version="2.1.999")
        good_url = release_manifest.MANIFEST_URL.format(version="2.1.218")
        self.assertEqual(self.events, [
            ("ask", "2.1.999"), ("GET", bad_url), ("GET", bad_url + ".sig"), ("verify", "2.1.999"),
            ("ask", "2.1.218"), ("GET", good_url), ("GET", good_url + ".sig"), ("verify", "2.1.218"),
        ])
        self.assertEqual(outcome.inspection.version, "2.1.218")
        self.assertIn("release manifest mismatch (sha256 differs)", "\n".join(outcome.messages))
        # The earlier yes never covers the fallback: a "no" for it stops there.
        self.events.clear()
        product = self._product_tree("repo-2")
        self.product = product
        outcome = self.run_update(fetch_consent=self.consent(True, False))
        self.assertEqual(outcome.kind, "fetch-declined")
        self.assertEqual([e for e in self.events if e[0] == "GET"], [("GET", bad_url), ("GET", bad_url + ".sig")])
        self.assertEqual(self.events[-1], ("ask", "2.1.218"))

    def test_offline_manifest_never_falls_back_to_network(self):
        offline = self.root / "offline"
        (offline / "2.1.218").mkdir(parents=True)
        consent = mock.Mock(side_effect=AssertionError("offline needs no network consent"))
        before = self.snapshot()
        with self.assertRaisesRegex(UpgradeError, "offline files: FileNotFoundError.*nothing was changed"):
            self.run_update(manifest_dir=offline, fetch_consent=consent)
        self.assertEqual(self.events, [])
        consent.assert_not_called()
        self.assertEqual(before, self.snapshot())
        # A complete offline pair verifies without any request.
        (offline / "2.1.218" / "manifest.json").write_bytes(self._manifest("2.1.218"))
        (offline / "2.1.218" / "manifest.json.sig").write_bytes(b"signature")
        outcome = self.run_update(manifest_dir=offline, fetch_consent=consent)
        self.assertEqual(outcome.kind, "prepared")
        self.assertEqual(self.events, [("verify", "2.1.218")])
        consent.assert_not_called()

    def test_candidate_not_executed_before_signature_verification(self):
        marker = self.root / "executed"
        script = self.new.read_text().replace("#!/bin/sh\n", f'#!/bin/sh\n: > "{marker}"\n', 1)
        self.new.write_text(script)
        seen = []
        original = self._signature

        def signature(manifest, sig, **kwargs):
            seen.append(marker.exists())
            return original(manifest, sig, **kwargs)

        release_manifest.verify_signature.side_effect = signature
        outcome = self.run_update(fetch_consent=self.consent(True))
        self.assertEqual(seen, [False])  # not executed while (or before) verifying
        self.assertTrue(marker.exists())  # --version/--help ran only after it
        self.assertEqual(outcome.kind, "prepared")
        # A signature failure never executes the candidate at all.
        marker.unlink()
        self.product = self._product_tree("repo-bad")
        release_manifest.verify_signature.side_effect = release_manifest.ReleaseManifestMismatch("bad signature")
        outcome = self.run_update(fetch_consent=self.consent(True))
        self.assertEqual(outcome.kind, "current")
        self.assertFalse(marker.exists())
        self.assertIn("release manifest mismatch (bad signature)", "\n".join(outcome.messages))

    def test_repin_fetch_and_prompt_hold_no_mutation_lock(self):
        outcome = self.run_update(fetch_consent=self.consent(True))
        self.assertEqual(outcome.kind, "prepared")
        self.assertIn(("ask", "2.1.218"), self.events)
        self.assertEqual(len([e for e in self.events if e[0] == "GET"]), 2)
        # Contended: another repin holds the transaction. The plan is still
        # shown and fetched lock-free; only the commit waits for the lock.
        self.product = self._product_tree("repo-contended")
        blocker = state.FileLock(self.lock_target)
        blocker.acquire(blocking=True)
        self.contended = True
        notes: list[str] = []
        asked = []

        def ask(plan):
            asked.append(plan.version)
            return True

        import threading
        result: list = []
        worker = threading.Thread(target=lambda: result.append(self.run_update(fetch_consent=ask, progress=notes.append)))
        worker.start()
        try:
            deadline = time.time() + 10
            while not any("waiting" in note for note in notes) and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(asked, ["2.1.218"])  # asked and fetched before waiting
            self.assertTrue(any("waiting" in note for note in notes))
        finally:
            blocker.release()
            worker.join(timeout=20)
        self.assertEqual(result[0].kind, "prepared")

    def test_repin_revalidates_artifact_and_checkout_before_promotion(self):
        real_inspect = upgrade.inspect_candidate
        for what, change in (
            ("the candidate artifact", lambda: self.new.write_text(self.new.read_text() + "# changed\n")),
            ("the source checkout", lambda: (self.product / RESOURCES_RELATIVE / "catalog/native-contract.json").write_text(
                (self.product / RESOURCES_RELATIVE / "catalog/native-contract.json").read_text() + "\n")),
        ):
            with self.subTest(what=what):
                self.product = self._product_tree(f"repo-{what.split()[-1]}")
                self.new.write_text(FAKE_CLAUDE.replace("VERSION_NAME", "2.1.218"))
                runner = mock.Mock()

                def inspect(path, verify=None, change=change):
                    result = real_inspect(path, verify=verify)
                    change()  # after verification, before the transaction
                    return result

                before = self.snapshot() if what != "the source checkout" else None
                with mock.patch.object(upgrade, "inspect_candidate", side_effect=inspect), \
                        self.assertRaisesRegex(UpgradeError, f"{what}.*changed while the candidate was being "
                                                             "verified — nothing was changed"):
                    self.run_update(fetch_consent=self.consent(True), runner=runner)
                runner.assert_not_called()  # no evidence suite, no promotion
                if before is not None:
                    self.assertEqual(before, self.snapshot())


class ChangedPathsTests(UpgradeTestCase):
    def test_reports_status_exact_commit_and_how_the_pin_ships(self):
        product = self._product_tree()
        (product / "tests/test_catalog.py").write_text('pin = "2.1.217"\n')
        git = mock.Mock(return_value=subprocess.CompletedProcess([], 0, " M version.json\n", ""))
        result = self.repin(checkout_root=product, native_contract=self.contract,
                            today="2026-09-27", runner=self._runner(0), git=git)
        repo = product
        # Checkout-relative paths: the resources sit under src/claude_multi/data.
        paths = ["src/claude_multi/data/catalog/native-contract.json", "src/claude_multi/data/version.json",
                 "tests/test_catalog.py"]
        self.assertIn(f"changed in {repo}:", result.messages)
        self.assertIn("   M version.json", result.messages)
        self.assertIn(f"commit: git -C {repo} commit -m 'build(pin): pin Claude Code 2.1.218' -- "
                      + " ".join(paths), result.messages)
        hint = next(m for m in result.messages if m.startswith("install:"))
        self.assertIn("tools/build.py release", hint)
        self.assertNotRegex("\n".join(result.messages), r"(?i)home[- ]manager")
        self.assertNotIn("#", hint)
        self.assertEqual(git.call_args.args[0], ["git", "-C", str(repo), "status", "--short", "--", *paths])
        self.assertEqual(git.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")

    def test_commit_suggestion_quotes_shell_sensitive_repo_paths(self):
        product = self._product_tree("repo with 'quote'; $value")
        result = self.repin(
            checkout_root=product, native_contract=self.contract, today="2026-09-27",
            runner=self._runner(0), git=mock.Mock(side_effect=OSError("no git")),
        )
        command = next(m for m in result.messages if m.startswith("commit: "))
        self.assertEqual(shlex.split(command.removeprefix("commit: "))[:3], ["git", "-C", str(product)])

    def test_git_failure_nonfatal(self):
        for i, git in enumerate((mock.Mock(side_effect=OSError("no git")),
                                 mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "")))):
            with self.subTest(i=i):
                result = self.repin(checkout_root=self._product_tree(f"repo-{i}"), native_contract=self.contract,
                                    today="2026-09-27", runner=self._runner(0), git=git)
                self.assertEqual(result.kind, "prepared")
                self.assertTrue(any("git status unavailable" in m and "expected:" in m for m in result.messages))


class RepinCommandTests(UpgradeTestCase):
    """``claude-multi-dev repin``: flags, the default checkout, no defaults."""

    def test_flags_are_checked_before_anything_runs(self):
        from claude_multi.cli.commands import repin as repin_cmd
        from claude_multi import errors

        for flags, message in (({"activate": True}, "unknown repin option --activate"),
                               ({"flake-target": ".#x"}, "unknown repin option --flake-target"),
                               ({"manifest-dir": True}, "--manifest-dir needs a value"),
                               ({"bogus": "x"}, "unknown repin option --bogus")):
            with self.subTest(flags=flags), mock.patch.object(upgrade, "run_repin") as run:
                with self.assertRaisesRegex(errors.ClaudeMultiError, message):
                    repin_cmd.repin(flags, environ=self.env, output_stream=io.StringIO())
                run.assert_not_called()

    def test_default_checkout_is_the_running_source_tree(self):
        from claude_multi.cli.commands import repin as repin_cmd

        self.assertEqual(upgrade.default_checkout(), REPO_ROOT)
        product = self._product_tree()
        (product / "flake.nix").write_text("{}\n")
        (product / "src/claude_multi/__init__.py").write_text("")
        seen = {}

        def fake_run(**kwargs):
            seen.update(kwargs)
            return upgrade.UpgradeOutcome("current", None, ("nothing to re-pin",))
        out = io.StringIO()
        with mock.patch.object(upgrade, "run_repin", side_effect=fake_run), \
                mock.patch("claude_multi.catalog.load_raw", return_value={"docs": {"native-contract": self.contract}}), \
                mock.patch.object(upgrade, "default_checkout", return_value=product):
            self.assertEqual(repin_cmd.repin({}, environ=self.env, output_stream=out), 0)
        self.assertEqual(seen["checkout_root"], product.resolve())
        self.assertNotIn("activate", seen)
        self.assertIn("nothing to re-pin", out.getvalue())

    def test_dev_entry_routes_repin(self):
        from claude_multi import dev

        with mock.patch("claude_multi.cli.commands.repin.repin", return_value=0) as repin:
            self.assertEqual(dev.main(["repin", "--repo", "/x", "--manifest-dir", "/m"]), 0)
        repin.assert_called_once_with({"repo": "/x", "manifest-dir": "/m"})
        self.assertIn("claude-multi-dev repin", dev.DEV_HELP)
        self.assertNotRegex(dev.DEV_HELP, r"#\w")  # no flake target anywhere
        self.assertNotIn("--activate", dev.DEV_HELP)
