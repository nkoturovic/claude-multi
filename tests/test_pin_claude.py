"""``tools/pin_claude.py``: the next native contract from a verified manifest pair.

Offline only: the signed 2.1.281 manifest fixture and, where GnuPG is
present, a real signature check with the vendored key in a throwaway keyring.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from claude_multi import catalog, pin, release_manifest, strict_json, upgrade, validate
from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT, RESOURCES_RELATIVE, TOOLS_DIR, fake_checkout

MANIFESTS = REPO_ROOT / "tests" / "fixtures" / "release-manifest"


def _tool():
    spec = importlib.util.spec_from_file_location("pin_claude_tool", TOOLS_DIR / "pin_claude.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _tool()
        self.manifest, self.signature = release_manifest.load_local(MANIFESTS, "2.1.281")
        self.current = strict_json.load(FIXTURE_ROOT / "catalog" / "native-contract.json")
        self.schema = strict_json.load(FIXTURE_ROOT / "schemas" / "native-contract.schema.json")

    def build(self, **kwargs):
        params = dict(version="2.1.281", manifest=self.manifest, signature=self.signature,
                      fingerprint=release_manifest.KEY_FINGERPRINT, battery_platform="linux-x64",
                      receipt_sha256="e" * 64, today="2026-10-02")
        params.update(kwargs)
        return self.tool.build_contract(copy.deepcopy(self.current), **params)

    def test_every_signed_build_and_the_provenance(self) -> None:
        document = self.build()
        entry = document["verified"][0]
        manifest = json.loads(self.manifest)
        self.assertEqual(entry["platforms"], {name: {"sha256": item["checksum"], "size": item["size"]}
                                              for name, item in manifest["platforms"].items()})
        self.assertEqual(set(entry["platforms"]), set(pin.PLATFORMS))
        self.assertEqual(entry["manifest_sha256"], hashlib.sha256(self.manifest).hexdigest())
        self.assertEqual(entry["signature_sha256"], hashlib.sha256(self.signature).hexdigest())
        self.assertEqual(entry["key_fingerprint"], release_manifest.KEY_FINGERPRINT)
        self.assertEqual(entry["evidence"], {"linux-x64": "battery", "others": "identity+smoke",
                                             "receipt_sha256": "e" * 64})
        self.assertEqual(entry["verified_at"], "2026-10-02")
        self.assertEqual(self.tool.check_contract(document, self.schema), [])
        # The behaviour sections carry over unchanged.
        for key in ("acceptance", "agent_efforts", "lead_efforts", "capabilities", "nested_subagents"):
            self.assertEqual(document[key], self.current[key])

    def test_written_contract_loads_as_the_packaged_one(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="cm-pin-claude-"))
        self.addCleanup(shutil.rmtree, root, True)
        shutil.copytree(FIXTURE_ROOT, root / "assets")
        document = self.build(version="2.1.281")
        (root / "assets" / "catalog" / "native-contract.json").write_text(json.dumps(document))
        loaded = catalog.load_catalog(root / "assets")
        self.assertEqual(pin.version(loaded.docs["native-contract"]), "2.1.281")
        self.assertEqual(pin.evidence_class(loaded.docs["native-contract"], "darwin-arm64"), "identity+smoke")

    def test_refusals(self) -> None:
        with self.assertRaisesRegex(release_manifest.ReleaseManifestMismatch, "no plan9-x64 build"):
            self.build(battery_platform="plan9-x64")
        with self.assertRaisesRegex(release_manifest.ReleaseManifestMismatch, "manifest names version 2.1.281"):
            self.build(version="2.1.282")
        broken = self.build()
        broken["verified"][0]["platforms"]["linux-x64"]["size"] = 0
        self.assertTrue(self.tool.check_contract(broken, self.schema))
        self.assertEqual(validate.validate(self.current, self.schema, "$"), [])


class SettingsKeysOptionTests(BuildContractTests):
    def test_build_records_given_keys(self) -> None:
        document = self.build(settings_keys=["permissions", "env"])
        self.assertEqual(document["verified"][0]["settings_keys"], ["env", "permissions"])
        self.assertEqual(self.tool.check_contract(document, self.schema), [])

    def test_a_new_version_needs_its_settings_keys(self) -> None:
        older = copy.deepcopy(self.current)
        older["verified"][0]["version"] = "2.1.280"
        older["verified"][0]["settings_keys"] = ["apiKeyHelper", "env", "hooks", "model", "permissions"]
        params = dict(version="2.1.281", manifest=self.manifest, signature=self.signature,
                      fingerprint=release_manifest.KEY_FINGERPRINT, battery_platform="linux-x64",
                      receipt_sha256="e" * 64, today="2026-10-02")
        with self.assertRaisesRegex(upgrade.UpgradeError, "--settings-keys FILE"):
            self.tool.build_contract(copy.deepcopy(older), **params)
        document = self.tool.build_contract(copy.deepcopy(older), settings_keys=["env", "hooks"], **params)
        self.assertEqual(document["verified"][0]["settings_keys"], ["env", "hooks"])

    def test_a_file_that_is_not_the_signed_build_is_refused(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="cm-pin-claude-keys-"))
        self.addCleanup(shutil.rmtree, root, True)
        stray = root / "claude"
        stray.write_bytes(b"not the build")
        with self.assertRaisesRegex(release_manifest.ReleaseManifestMismatch, "not the signed 2.1.281 linux-x64"):
            self.tool.settings_keys_from(stray, self.manifest, version="2.1.281", platform="linux-x64")


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _tool()
        self.root = Path(tempfile.mkdtemp(prefix="cm-pin-claude-cmd-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = fake_checkout(self.root / "repo", FIXTURE_ROOT, ("catalog", "schemas"))
        self.contract = self.repo / RESOURCES_RELATIVE / "catalog/native-contract.json"
        self.receipt = self.root / "receipt.json"
        self.receipt.write_text('{"fixture": "battery receipt"}\n')

    def run_tool(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.tool.main(["2.1.281", "--manifest-dir", str(MANIFESTS), "--receipt", str(self.receipt),
                                   "--repo", str(self.repo), "--date", "2026-10-02", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_signed_fixture_writes_the_next_contract(self) -> None:
        if shutil.which("gpg") is None:
            self.skipTest("BOUNDARY: gpg absent; the release check verifies with GnuPG")
        before = self.contract.read_bytes()
        code, out, err = self.run_tool("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["verified"][0]["evidence"]["receipt_sha256"],
                         hashlib.sha256(self.receipt.read_bytes()).hexdigest())
        self.assertEqual(self.contract.read_bytes(), before)
        code, out, err = self.run_tool()
        self.assertEqual(code, 0, err)
        self.assertIn("8 platform builds", out)
        written = json.loads(self.contract.read_bytes())
        self.assertEqual(len(written["verified"][0]["platforms"]), 8)

    def test_a_changed_manifest_is_refused(self) -> None:
        if shutil.which("gpg") is None:
            self.skipTest("BOUNDARY: gpg absent; the release check verifies with GnuPG")
        tampered = self.root / "manifests" / "2.1.281"
        tampered.mkdir(parents=True)
        source = MANIFESTS / "2.1.281"
        (tampered / "manifest.json").write_bytes((source / "manifest.json").read_bytes() + b" ")
        (tampered / "manifest.json.sig").write_bytes((source / "manifest.json.sig").read_bytes())
        before = self.contract.read_bytes()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.tool.main(["2.1.281", "--manifest-dir", str(self.root / "manifests"), "--receipt",
                                   str(self.receipt), "--repo", str(self.repo)])
        self.assertEqual(code, 2)
        self.assertIn("bad signature", err.getvalue())
        self.assertEqual(self.contract.read_bytes(), before)

    def test_a_reviewed_key_list_is_read_and_checked(self) -> None:
        if shutil.which("gpg") is None:
            self.skipTest("BOUNDARY: gpg absent; the release check verifies with GnuPG")
        keys = self.root / "keys.json"
        keys.write_text('["permissions", "model", "hooks", "env", "apiKeyHelper", "reviewed"]')
        code, out, err = self.run_tool("--dry-run", "--settings-keys", str(keys))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["verified"][0]["settings_keys"],
                         ["apiKeyHelper", "env", "hooks", "model", "permissions", "reviewed"])
        keys.write_text('["env"]')
        code, _out, err = self.run_tool("--dry-run", "--settings-keys", str(keys))
        self.assertEqual(code, 2)
        self.assertIn("lacks apiKeyHelper", err)

    def test_writes_only_into_a_verified_checkout(self) -> None:
        # Not a checkout: refused before anything is read or written.
        stray = self.root / "stray"
        shutil.copytree(self.repo / RESOURCES_RELATIVE, stray / "catalog-tree")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.tool.main(["2.1.281", "--manifest-dir", str(MANIFESTS), "--receipt", str(self.receipt),
                                   "--repo", str(stray)])
        self.assertEqual(code, 2)
        self.assertIn("is not a source checkout", err.getvalue())

    def test_a_symlinked_resource_directory_is_refused(self) -> None:
        # A symlinked data/ (or catalog/) would redirect the write outside the
        # checkout: refused before the manifest is read.
        outside = self.root / "outside"
        shutil.move(str(self.repo / RESOURCES_RELATIVE), str(outside))
        (self.repo / RESOURCES_RELATIVE).symlink_to(outside, target_is_directory=True)
        before = (outside / "catalog/native-contract.json").read_bytes()
        code, _out, err = self.run_tool()
        self.assertEqual(code, 2)
        self.assertIn("not a real directory inside the checkout", err)
        self.assertEqual((outside / "catalog/native-contract.json").read_bytes(), before)

    def test_missing_pair_is_refused_without_a_fetch(self) -> None:
        code, _out, err = self.run_tool("--manifest-dir", str(self.root / "empty"))
        self.assertEqual(code, 2)
        self.assertIn("offline files", err)


if __name__ == "__main__":
    unittest.main()
