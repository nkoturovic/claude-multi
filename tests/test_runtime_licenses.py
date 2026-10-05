"""The bundled Python runtimes' licences (``packaging/licenses/python/``,
``tools/_build/runtime_licenses.py``).

The checked-in inventory and texts describe exactly the pinned runtimes: each
shipped library has its text, no shipped extension links a strong-copyleft
library (``_dbm`` and Berkeley DB are pruned), and the launcher never imports
what was pruned. The derivation runs over synthetic full archives (no
download).
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from _layout import PACKAGE_DIR
from _release import (RUNTIME_LICENSES, RUNTIME_LICENSES_TOOL, RUNTIMES_JSON, PYTHON_RUNTIME, load_tool,
                      requires_release_tree)

# Python modules that need an extension the runtimes prune.
PRUNED_IMPORTERS = {"_dbm": ("dbm", "shelve", "_dbm"), "_tkinter": ("tkinter", "_tkinter", "turtle", "idlelib")}


def tools():
    return load_tool(RUNTIME_LICENSES_TOOL, "runtime_licenses"), load_tool(PYTHON_RUNTIME, "python_runtime")


@requires_release_tree
class CheckedInTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool, runtime_tool = tools()
        self.runtimes = runtime_tool.load(RUNTIMES_JSON)
        self.inventory = self.tool.load(RUNTIME_LICENSES)

    def test_it_describes_exactly_the_pinned_runtimes(self) -> None:
        self.tool.check_runtimes(self.inventory, self.runtimes, self.runtimes.targets)
        self.assertEqual(sorted(self.inventory.document["targets"]), sorted(self.runtimes.targets))
        for target, entry in self.inventory.document["targets"].items():
            with self.subTest(target):
                runtime = self.runtimes.runtime(target)
                self.assertEqual(entry["install_only"], {"filename": runtime.filename, "sha256": runtime.sha256})
                self.assertEqual(entry["full_archive"]["filename"],
                                 runtime.filename.replace("install_only_stripped.tar.gz", "pgo+lto-full.tar.zst"))
                self.assertRegex(entry["full_archive"]["sha256"], r"^[0-9a-f]{64}$")

    def test_every_shipped_library_has_its_text(self) -> None:
        for target, entry in self.inventory.document["targets"].items():
            for library in entry["libraries"]:
                with self.subTest(target=target, library=library["name"]):
                    self.assertTrue(library["version"])
                    self.assertTrue(library["spdx"] or library.get("public_domain"))
                    self.assertTrue(library["texts"])
                    for name in library["texts"]:
                        data = (RUNTIME_LICENSES / name).read_bytes()
                        self.assertGreater(len(data), 100)
                        self.assertEqual(hashlib.sha256(data).hexdigest(),
                                         self.inventory.document["texts"][name]["sha256"])

    def test_the_copyleft_rule(self) -> None:
        self.assertIn("lib/python3.14/lib-dynload/_dbm*", self.runtimes.prune)
        for target, entry in self.inventory.document["targets"].items():
            with self.subTest(target):
                for library in entry["libraries"]:
                    self.assertFalse([item for item in library["spdx"] if self.tool.strong_copyleft(item)],
                                     library["name"])
                pruned = {item["extension"]: item for item in entry["pruned"]}
                self.assertIn("_dbm", pruned)
                strong = [lib for item in entry["pruned"] for lib in item["libraries"]
                          if any(self.tool.strong_copyleft(spdx) for spdx in lib["spdx"])]
                if target.startswith("linux"):
                    # Berkeley DB (Sleepycat) behind _dbm: the only strong copyleft.
                    self.assertEqual([(lib["name"], lib["spdx"]) for lib in strong], [("bdb", ["Sleepycat"])])
                    self.assertEqual(pruned["_dbm"]["reason"], "strong copyleft; claude-multi never imports it")
                else:
                    self.assertEqual(strong, [])
        for spdx, strong in (("GPL-2.0-only", True), ("AGPL-3.0-or-later", True), ("Sleepycat", True),
                             ("LGPL-2.1-or-later", False), ("MPL-2.0", False), ("BSD-3-Clause", False)):
            self.assertEqual(self.tool.strong_copyleft(spdx), strong, spdx)

    def test_the_launcher_never_imports_a_pruned_extension(self) -> None:
        """What makes pruning safe: no launcher module imports dbm, shelve
        or tkinter (the bundle build's import check then runs every
        launcher module under the pruned runtime)."""

        forbidden = {name for names in PRUNED_IMPORTERS.values() for name in names}
        found = []
        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                found += [f"{path.name}: {name}" for name in names if name.split(".")[0] in forbidden]
        self.assertEqual(found, [])

    def test_install_notices_and_sbom_components(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw)
            written = self.tool.install(self.inventory, "linux-x86_64", destination)
            entry = self.inventory.target("linux-x86_64")
            texts = sorted({name for library in entry["libraries"] for name in library["texts"]})
            self.assertEqual(sorted(written), sorted([*texts, "INVENTORY.json", "THIRD_PARTY_NOTICES.txt"]))
            notices = (destination / "THIRD_PARTY_NOTICES.txt").read_text()
            self.assertIn("openssl-3.5 3.5.9 — Apache-2.0", notices)
            self.assertIn("sqlite 3.53.1 — public domain", notices)
            self.assertIn("_dbm: bdb 6.0.19 (Sleepycat) — strong copyleft; claude-multi never imports it", notices)
            self.assertIn((RUNTIME_LICENSES / "LICENSE.zstd.txt").read_text().splitlines()[0], notices)
        components = self.tool.sbom_components(self.inventory, "darwin-arm64")
        self.assertEqual([item["name"] for item in components],
                         [library["name"] for library in self.inventory.target("darwin-arm64")["libraries"]])
        self.assertNotIn("ncurses", [item["name"] for item in components])  # the system's on macOS


@requires_release_tree
class RefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool, runtime_tool = tools()
        self.runtimes = runtime_tool.load(RUNTIMES_JSON)
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-runtime-licences-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = self.tmp / "licences"
        shutil.copytree(RUNTIME_LICENSES, self.dir)
        self.pristine = (self.dir / "inventory.json").read_text()

    def edit(self, change) -> None:
        document = json.loads(self.pristine)
        change(document)
        (self.dir / "inventory.json").write_text(json.dumps(document))

    def test_load_refuses_an_inconsistent_inventory(self) -> None:
        def first(document, target="linux-x86_64"):
            return document["targets"][target]["libraries"][0]

        cases = {
            "has no licence text": lambda d: first(d).update(texts=[]),
            "is not checked in": lambda d: first(d).update(texts=["LICENSE.other.txt"]),
            "names no licence": lambda d: first(d).update(spdx=[]),
            "strong-copyleft": lambda d: first(d).update(spdx=["Sleepycat"]),
            "both shipped and pruned": lambda d: d["targets"]["linux-x86_64"]["pruned"].append(
                {"extension": first(d)["extensions"][0], "pattern": "x", "libraries": []}),
            "not sorted and unique": lambda d: d["targets"]["linux-x86_64"]["libraries"].append(first(d)),
            "not format 1": lambda d: d.update(format=2),
        }
        for needle, change in cases.items():
            with self.subTest(needle):
                self.edit(change)
                with self.assertRaises(self.tool.LicenseError) as caught:
                    self.tool.load(self.dir)
                self.assertIn(needle, str(caught.exception))
        (self.dir / "inventory.json").write_text(self.pristine)
        self.tool.load(self.dir)
        (self.dir / "LICENSE.extra.txt").write_text("x")
        with self.assertRaisesRegex(self.tool.LicenseError, "not exactly the inventory's texts"):
            self.tool.load(self.dir)
        (self.dir / "LICENSE.extra.txt").unlink()
        (self.dir / "LICENSE.zstd.txt").write_text("altered")
        with self.assertRaisesRegex(self.tool.LicenseError, "sha256 differs"):
            self.tool.load(self.dir)

    def test_another_pin_or_an_unpruned_extension_is_refused(self) -> None:
        self.edit(lambda d: d.update(release="20990101"))
        with self.assertRaisesRegex(self.tool.LicenseError, "regenerate it"):
            self.tool.check_runtimes(self.tool.load(self.dir), self.runtimes, self.runtimes.targets)
        self.edit(lambda d: d["targets"]["linux-aarch64"]["install_only"].update(sha256="0" * 64))
        with self.assertRaisesRegex(self.tool.LicenseError, "linux-aarch64: the runtime licence inventory describes"):
            self.tool.check_runtimes(self.tool.load(self.dir), self.runtimes, self.runtimes.targets)
        self.edit(lambda d: d["targets"]["linux-x86_64"]["pruned"][0].update(pattern="lib/nothing*"))
        with self.assertRaisesRegex(self.tool.LicenseError, "must be pruned"):
            self.tool.check_runtimes(self.tool.load(self.dir), self.runtimes, ["linux-x86_64"])

    def test_the_shipped_runtime_must_match(self) -> None:
        inventory = self.tool.load(self.dir)
        dynload = self.tmp / "lib-dynload"
        dynload.mkdir()
        (dynload / ".empty").write_text("")
        self.tool.check_shipped(inventory, "linux-x86_64", dynload)
        (dynload / "_dbm.cpython-314-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
        with self.assertRaisesRegex(self.tool.LicenseError, "the pruned extension _dbm is still in the runtime"):
            self.tool.check_shipped(inventory, "linux-x86_64", dynload)
        (dynload / "_dbm.cpython-314-x86_64-linux-gnu.so").unlink()
        (dynload / "_gdbm.cpython-314-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
        with self.assertRaisesRegex(self.tool.LicenseError, "the licence inventory does not describe"):
            self.tool.check_shipped(inventory, "linux-x86_64", dynload)


class DeriveTests(unittest.TestCase):
    """The derivation over synthetic full archives (``.tar.gz`` stand-ins)."""

    VERSIONS = {
        "bdb": {"version": "6.0.19", "license_file": "LICENSE.bdb.txt", "licenses": ["Sleepycat"]},
        "openssl-3.5": {"version": "3.5.9", "license_file": "LICENSE.openssl-3.txt", "licenses": ["Apache-2.0"]},
        "sqlite": {"version": "3530100", "license_file": "LICENSE.sqlite.txt", "licenses": []},
        "zstd": {"version": "1.5.7", "license_file": "LICENSE.zstd.txt", "licenses": ["BSD-3-Clause"]},
        "zlib": {"version": "1.3.2", "license_file": "LICENSE.zlib.txt", "licenses": ["Zlib"]},
    }

    def archive(self, path: Path, *, with_zstd_text: bool) -> Path:
        meta = {"licenses": ["Python-2.0", "CNRI-Python"], "build_info": {"extensions": {
            "_dbm": [{"links": [{"name": "db", "path_static": "build/lib/libdb.a"}],
                      "shared_lib": "install/lib/python3.14/lib-dynload/_dbm.cpython-314-x86_64-linux-gnu.so"}],
            "_ssl": [{"links": [{"name": "crypto", "path_static": "x"}, {"name": "ssl", "path_static": "y"}]}],
            "_sqlite3": [{"links": [{"name": "sqlite3", "path_static": "z"}, {"name": "m", "system": True}]}],
            "_zstd": [{"links": [{"name": "zstd", "path_static": "z"}]}],
            "zlib": [{"links": [{"name": "z", "system": True}]}],
            "_ctypes_test": [{"links": [], "shared_lib": "install/lib/python3.14/lib-dynload/_ctypes_test.so"}],
        }}}
        files = {"python/PYTHON.json": json.dumps(meta).encode(),
                 "python/licenses/LICENSE.bdb.txt": b"Sleepycat text\n",
                 "python/licenses/LICENSE.openssl-3.txt": b"Apache text\n",
                 "python/licenses/LICENSE.sqlite.txt": b"public domain text\n",
                 "python/licenses/LICENSE.zlib.txt": b"zlib text\n"}
        if with_zstd_text:
            files["python/licenses/LICENSE.zstd.txt"] = b"zstd text\n"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        path.write_bytes(buffer.getvalue())
        return path

    def test_derive_the_inventory_and_texts(self) -> None:
        tool, _ = tools()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self.archive(root / "full.tar.gz", with_zstd_text=False)
            arguments = dict(install_only={"linux-x86_64": {"filename": "io.tar.gz", "sha256": "1" * 64}},
                             versions=self.VERSIONS, prune=["lib/python3.14/lib-dynload/_dbm*"],
                             release="20261001", python="3.14.8", versions_source="downloads.json")
            with self.assertRaisesRegex(tool.LicenseError, "LICENSE.zstd.txt is not in the archive"):
                tool.derive({"linux-x86_64": archive}, extra_texts={}, **arguments)
            document, texts = tool.derive({"linux-x86_64": archive},
                                          extra_texts={"LICENSE.zstd.txt": (b"zstd text\n", "supplied")}, **arguments)
            entry = document["targets"]["linux-x86_64"]
            self.assertEqual(entry["full_archive"]["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual([(lib["name"], lib["version"], lib["extensions"]) for lib in entry["libraries"]],
                             [("openssl-3.5", "3.5.9", ["_ssl"]), ("sqlite", "3.53.1", ["_sqlite3"]),
                              ("zstd", "1.5.7", ["_zstd"])])
            self.assertTrue(entry["libraries"][1]["public_domain"])
            self.assertEqual(entry["pruned"], [{"extension": "_dbm", "pattern": "lib/python3.14/lib-dynload/_dbm*",
                                                "libraries": [{"name": "bdb", "version": "6.0.19",
                                                               "spdx": ["Sleepycat"]}],
                                                "reason": "strong copyleft; claude-multi never imports it"}])
            self.assertEqual(entry["extensions_without_libraries"], ["_ctypes_test"])
            self.assertEqual(sorted(texts), ["LICENSE.openssl-3.txt", "LICENSE.sqlite.txt", "LICENSE.zstd.txt"])
            self.assertEqual(document["texts"]["LICENSE.zstd.txt"]["source"], "supplied")
            out = root / "out"
            tool.write(out, document, texts)
            loaded = tool.load(out)
            self.assertEqual(loaded.document, json.loads(json.dumps(document)))


if __name__ == "__main__":
    unittest.main()
