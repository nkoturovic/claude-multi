"""The release identity line: every component named, unavailable ones too."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_multi
from claude_multi import identity
from _layout import RESOURCES_ROOT


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-identity-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def _resources(self, *, version=True, gateway=True, native=True) -> Path:
        data = self.root / "data"
        (data / "catalog").mkdir(parents=True)
        if version:
            shutil.copy(RESOURCES_ROOT / "version.json", data / "version.json")
        if gateway:
            shutil.copy(RESOURCES_ROOT / "gateway-contract.json", data / "gateway-contract.json")
        if native:
            shutil.copy(RESOURCES_ROOT / "catalog" / "native-contract.json", data / "catalog" / "native-contract.json")
        return data

    def test_the_packaged_identity_names_every_component(self) -> None:
        found = identity.collect()
        line = identity.version_line()
        version = json.loads((RESOURCES_ROOT / "version.json").read_text())
        native = json.loads((RESOURCES_ROOT / "catalog" / "native-contract.json").read_text())["verified"][0]
        self.assertTrue(line.startswith(f"claude-multi {claude_multi.__version__} ("))
        self.assertIn(f"catalog {version['catalog_version']}", line)
        self.assertIn(f"Claude Code {native['version']}, manifest {native['manifest_sha256'][:8]}", line)
        self.assertNotIn(identity.UNAVAILABLE, line)
        self.assertEqual(found.document()["claude_code"], {"version": native["version"],
                                                           "manifest": native["manifest_sha256"][:8]})
        self.assertEqual(set(found.document()), {"launcher", "catalog", "gateway", "claude_code"})

    def test_missing_components_say_unavailable(self) -> None:
        cases = {
            "version": ("catalog unavailable",),
            "gateway": ("CLIProxyAPI unavailable",),
            "native": ("Claude Code unavailable",),
        }
        for missing, needles in cases.items():
            with self.subTest(missing=missing):
                data = self._resources(**{missing: False})
                with mock.patch.object(claude_multi, "resources_root", return_value=data):
                    line = identity.version_line("claude-multi-dev")
                self.assertTrue(line.startswith("claude-multi-dev "))
                for needle in needles:
                    self.assertIn(needle, line)
                shutil.rmtree(data)

    def test_nothing_is_run_or_contacted(self) -> None:
        with mock.patch("subprocess.run", side_effect=AssertionError("run")), \
                mock.patch("socket.socket", side_effect=AssertionError("network")):
            identity.version_line()


if __name__ == "__main__":
    unittest.main()
