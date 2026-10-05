"""The bundled-runtime tool (``tools/_build/python_runtime.py``), offline.

Synthetic archives stand in for the pinned python-build-standalone files:
fetch by hash through an injected opener, checked extraction, delete-only
pruning, and host bytecode compilation (the running interpreter stands in
for the pinned host interpreter).
"""

from __future__ import annotations

import hashlib
import io
import json
import marshal
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from _release import PYTHON_RUNTIME, RUNTIMES_JSON, load_tool, requires_release_tree


def _tar(entries: list[tuple[str, str, object]]) -> bytes:
    """``(name, kind, payload)`` with kind file/dir/symlink/hardlink/fifo."""

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            info.mtime = 0
            if kind == "file":
                data = payload if isinstance(payload, bytes) else str(payload).encode()
                info.size, info.mode = len(data), 0o755 if "/bin/" in name else 0o644
                tar.addfile(info, io.BytesIO(data))
                continue
            if kind == "dir":
                info.type, info.mode = tarfile.DIRTYPE, 0o755
            elif kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, str(payload)
            elif kind == "hardlink":
                info.type, info.linkname = tarfile.LNKTYPE, str(payload)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
            tar.addfile(info)
    return buffer.getvalue()


SYNTHETIC = [
    ("python", "dir", None),
    ("python/bin", "dir", None),
    ("python/bin/python3.14", "file", b"#!/bin/sh\necho interpreter\n"),
    ("python/bin/python3", "symlink", "python3.14"),
    ("python/bin/pip3.14", "file", b"pip"),
    ("python/bin/pip3", "symlink", "pip3.14"),
    ("python/bin/idle3.14", "file", b"idle"),
    ("python/bin/idle3", "symlink", "idle3.14"),
    ("python/bin/pydoc3.14", "file", b"pydoc"),
    ("python/bin/pydoc3", "symlink", "pydoc3.14"),
    ("python/include/python3.14/Python.h", "file", b"/* header */"),
    ("python/lib/libpython3.14.so.1.0", "file", b"\x7fELF shared"),
    ("python/lib/libpython3.so", "symlink", "libpython3.14.so.1.0"),
    ("python/lib/libtcl9.0.so", "file", b"tcl"),
    ("python/lib/libtk9.0.so", "file", b"tk"),
    ("python/lib/tcl9.0/init.tcl", "file", b"tcl script"),
    ("python/lib/tk9.0/tk.tcl", "file", b"tk script"),
    ("python/lib/itcl4.3.8/itcl.tcl", "file", b"itcl"),
    ("python/lib/thread3.0.6/thread.tcl", "file", b"thread"),
    ("python/lib/python3.14/os.py", "file", b"# os\n"),
    ("python/lib/python3.14/pydoc.py", "file", b"# pydoc module stays\n"),
    ("python/lib/python3.14/_testcapi_feature.py", "file", b"# test helper\n"),
    ("python/lib/python3.14/ensurepip/__init__.py", "file", b""),
    ("python/lib/python3.14/idlelib/idle.py", "file", b""),
    ("python/lib/python3.14/tkinter/__init__.py", "file", b""),
    ("python/lib/python3.14/turtledemo/demo.py", "file", b""),
    ("python/lib/python3.14/turtle.py", "file", b"# kept\n"),
    ("python/lib/python3.14/lib-dynload/_json.cpython-314-x86_64-linux-gnu.so", "file", b"json"),
    ("python/lib/python3.14/lib-dynload/_tkinter.cpython-314-x86_64-linux-gnu.so", "file", b"tk"),
    ("python/lib/python3.14/lib-dynload/_testcapi.cpython-314-x86_64-linux-gnu.so", "file", b"t"),
    ("python/lib/python3.14/site-packages/README.txt", "file", b"kept"),
    ("python/lib/python3.14/site-packages/pip/__init__.py", "file", b""),
    ("python/lib/python3.14/site-packages/pip-25.2.dist-info/METADATA", "file", b""),
    ("python/lib/python3.14/site-packages/pipx_like/__init__.py", "file", b"# not pip\n"),
    ("python/share/man/man1/python3.1", "file", b"man"),
    ("python/share/terminfo/x/xterm-256color", "file", b"terminfo"),
    ("python/share/terminfo/x/xterm-hardlink", "hardlink", "python/share/terminfo/x/xterm-256color"),
]
PRUNED = {
    "bin/idle3", "bin/idle3.14", "bin/pip3", "bin/pip3.14", "bin/pydoc3", "bin/pydoc3.14",
    "include", "lib/itcl4.3.8", "lib/libtcl9.0.so", "lib/libtk9.0.so", "lib/tcl9.0", "lib/thread3.0.6", "lib/tk9.0",
    "lib/python3.14/_testcapi_feature.py", "lib/python3.14/ensurepip", "lib/python3.14/idlelib",
    "lib/python3.14/tkinter", "lib/python3.14/turtledemo",
    "lib/python3.14/lib-dynload/_tkinter.cpython-314-x86_64-linux-gnu.so",
    "lib/python3.14/lib-dynload/_testcapi.cpython-314-x86_64-linux-gnu.so",
    "lib/python3.14/site-packages/pip", "lib/python3.14/site-packages/pip-25.2.dist-info",
    "share/man",
}


@requires_release_tree
class RuntimeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = load_tool(PYTHON_RUNTIME, "python_runtime")
        cls.shipped = json.loads(RUNTIMES_JSON.read_text())

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-runtime-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def manifest(self, archive: bytes | None = None, **changes) -> Path:
        document = json.loads(json.dumps(self.shipped))
        if archive is not None:
            entry = document["targets"]["linux-x86_64"]
            entry["size"], entry["sha256"] = len(archive), hashlib.sha256(archive).hexdigest()
        document.update(changes)
        path = self.tmp / f"runtimes-{len(list(self.tmp.iterdir()))}.json"
        path.write_text(json.dumps(document))
        return path

    def opener(self, payload: bytes, calls: list[str] | None = None):
        def open_url(url: str):
            if calls is not None:
                calls.append(url)
            return io.BytesIO(payload)
        return open_url


class ManifestTests(RuntimeTestCase):
    def test_shipped_manifest_is_consistent(self) -> None:
        runtimes = self.tool.load(RUNTIMES_JSON)
        self.assertEqual(sorted(runtimes.targets), sorted(self.tool.TRIPLES))
        self.assertTrue(runtimes.python.startswith("3.14."))
        for target, runtime in runtimes.targets.items():
            with self.subTest(target):
                self.assertIn(runtime.triple, runtime.filename)
                self.assertTrue(runtime.url.startswith("https://github.com/astral-sh/python-build-standalone/releases/download/"))
                self.assertIn("%2B", runtime.url)
                self.assertNotIn("+", runtime.url)
                self.assertTrue(runtime.url.endswith(runtime.filename.replace("+", "%2B")))
        self.assertEqual(list(runtimes.prune), sorted(set(runtimes.prune)))
        for component in ("bin/pip*", "lib/python3.14/ensurepip", "lib/python3.14/tkinter", "include", "share/man",
                          "lib/itcl*", "lib/thread*"):
            self.assertIn(component, runtimes.prune)
        self.assertFalse(any(p.startswith("share/terminfo") for p in runtimes.prune))

    def test_manifest_refusals(self) -> None:
        cases = {
            "format": {"format": 2},
            "flavor": {"flavor": "full"},
            "http url": {"url": "http://example.invalid/{release}/{filename}"},
            "short release": {"release": "2026"},
            "prune escape": {"prune": ["../etc"]},
            "prune absolute": {"prune": ["/usr"]},
            "prune unsorted": {"prune": ["share/man", "include"]},
        }
        for label, change in cases.items():
            with self.subTest(label):
                with self.assertRaises(self.tool.BuildError):
                    self.tool.load(self.manifest(**change))
        document = json.loads(json.dumps(self.shipped))
        document["targets"]["linux-x86_64"]["filename"] = "cpython-other.tar.gz"
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps(document))
        with self.assertRaisesRegex(self.tool.BuildError, "must name"):
            self.tool.load(bad)
        del document["targets"]["darwin-x86_64"]
        bad.write_text(json.dumps(document))
        with self.assertRaisesRegex(self.tool.BuildError, "targets must be exactly"):
            self.tool.load(bad)

    def test_host_target(self) -> None:
        cases = {("Linux", "x86_64"): "linux-x86_64", ("Linux", "aarch64"): "linux-aarch64",
                 ("Linux", "arm64"): "linux-aarch64", ("Darwin", "arm64"): "darwin-arm64",
                 ("Darwin", "x86_64"): "darwin-x86_64"}
        for (system, machine), target in cases.items():
            self.assertEqual(self.tool.host_target(system, machine), target)
        for system, machine in (("FreeBSD", "amd64"), ("Linux", "riscv64"), ("Windows", "AMD64")):
            with self.assertRaises(self.tool.BuildError):
                self.tool.host_target(system, machine)


class FetchTests(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.archive = _tar(SYNTHETIC)
        self.runtimes = self.tool.load(self.manifest(self.archive))
        self.runtime = self.runtimes.runtime("linux-x86_64")
        self.cache = self.tmp / "cache"

    def test_fetch_verifies_then_caches(self) -> None:
        calls: list[str] = []
        path = self.tool.fetch(self.runtime, self.cache, opener=self.opener(self.archive, calls))
        self.assertEqual(path, self.cache / self.runtime.sha256 / self.runtime.filename)
        self.assertEqual(path.read_bytes(), self.archive)
        self.assertEqual(calls, [self.runtime.url])
        self.assertEqual(self.tool.fetch(self.runtime, self.cache, opener=self.opener(b"", calls)), path)
        self.assertEqual(len(calls), 1)  # the cache hit was re-hashed, not re-downloaded
        self.assertEqual(self.tool.fetch(self.runtime, self.cache, offline=True), path)

    def test_mismatches_leave_nothing_behind(self) -> None:
        middle = len(self.archive) // 2
        altered = self.archive[:middle] + bytes([self.archive[middle] ^ 0xFF]) + self.archive[middle + 1:]
        for label, payload in {"other bytes": altered,
                               "short": self.archive[:-10], "long": self.archive + b"x"}.items():
            with self.subTest(label):
                with self.assertRaises(self.tool.BuildError):
                    self.tool.fetch(self.runtime, self.cache, opener=self.opener(payload))
                leftovers = list(self.cache.rglob("*")) if self.cache.exists() else []
                self.assertEqual([p for p in leftovers if p.is_file()], [])

    def test_offline_needs_the_cache_and_a_tampered_cache_refuses(self) -> None:
        with self.assertRaisesRegex(self.tool.BuildError, "offline"):
            self.tool.fetch(self.runtime, self.cache, offline=True)
        path = self.tool.fetch(self.runtime, self.cache, opener=self.opener(self.archive))
        path.write_bytes(self.archive[:-1] + b"\x01")
        with self.assertRaises(self.tool.BuildError):
            self.tool.fetch(self.runtime, self.cache, offline=True)

    def test_default_opener_refuses_plain_http(self) -> None:
        with self.assertRaises(self.tool.BuildError):
            self.tool._https_opener("http://example.invalid/x.tar.gz")


class ExtractPruneTests(RuntimeTestCase):
    def _hashes(self, root: Path) -> dict[str, str]:
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}

    def test_prepare_prunes_exactly_the_listed_entries(self) -> None:
        archive = _tar(SYNTHETIC)
        runtimes = self.tool.load(self.manifest(archive))
        source = self.tool.extract(self._archive_file(archive), self.tmp / "pristine")
        before = self._hashes(source)
        result = self.tool.prepare(runtimes, "linux-x86_64", self.tmp / "bundle", cache=self.tmp / "cache",
                                   opener=self.opener(archive))
        root = Path(result["root"])
        self.assertEqual(set(result["removed"]), PRUNED)
        after = self._hashes(root)
        # Delete-only: every remaining file is byte-identical to the archive's.
        self.assertTrue(set(after) <= set(before))
        self.assertEqual({k: before[k] for k in after}, after)
        self.assertTrue((root / "share/terminfo/x/xterm-256color").is_file())
        self.assertTrue((root / "lib/python3.14/pydoc.py").is_file())
        self.assertTrue((root / "lib/python3.14/site-packages/pipx_like/__init__.py").is_file())
        self.assertEqual(os.readlink(root / "bin/python3"), "python3.14")
        self.assertEqual(self.tool.dangling_links(root), [])

    def test_a_prune_gap_is_reported(self) -> None:
        archive = _tar(SYNTHETIC + [("python/bin/pip", "symlink", "pip3.14")])
        runtimes = self.tool.load(self.manifest(archive, prune=["bin/pip3*"]))
        with self.assertRaisesRegex(self.tool.BuildError, "dangling links: bin/pip"):
            self.tool.prepare(runtimes, "linux-x86_64", self.tmp / "b", cache=self.tmp / "c",
                              opener=self.opener(archive))

    def test_a_store_path_reference_is_refused(self) -> None:
        store = b"/nix/store/" + b"0" * 31 + b"a-glibc-2.40/lib/ld-linux-x86-64.so.2"
        archive = _tar(SYNTHETIC + [("python/lib/python3.14/lib-dynload/_ssl.so", "file", b"\x7fELF" + store)])
        runtimes = self.tool.load(self.manifest(archive))
        with self.assertRaisesRegex(self.tool.BuildError, "names /nix/store paths: lib/python3.14/lib-dynload/_ssl.so"):
            self.tool.prepare(runtimes, "linux-x86_64", self.tmp / "b", cache=self.tmp / "c",
                              opener=self.opener(archive))
        # The bare prefix in path-detection text is not a concrete store path.
        detection = self.tmp / "detection"
        detection.mkdir()
        (detection / "detect.py").write_text("STORE = '/nix/store/'\n")
        self.assertEqual(self.tool.store_references(detection), [])

    def test_prune_never_follows_a_symlinked_directory(self) -> None:
        outside = self.tmp / "outside"
        (outside / "include").mkdir(parents=True)
        root = self.tmp / "root"
        root.mkdir()
        (root / "lib").symlink_to(outside)
        self.assertEqual(self.tool.prune(root, ["lib/include"]), [])
        self.assertTrue((outside / "include").is_dir())

    def _archive_file(self, data: bytes) -> Path:
        path = self.tmp / f"archive-{hashlib.sha256(data).hexdigest()[:8]}.tar.gz"
        path.write_bytes(data)
        return path

    def test_extraction_refusals(self) -> None:
        base = [("python", "dir", None), ("python/bin/python3", "file", b"x")]
        cases = {
            "absolute": base + [("/etc/passwd", "file", b"x")],
            "dot-dot": base + [("python/../evil", "file", b"x")],
            "outside root": base + [("other/file", "file", b"x")],
            "escaping symlink": base + [("python/lib", "symlink", "../../etc")],
            "absolute symlink": base + [("python/lib", "symlink", "/usr/lib")],
            "dangling hard link": base + [("python/h", "hardlink", "python/nowhere")],
            "fifo": base + [("python/pipe", "fifo", None)],
            "no interpreter": [("python", "dir", None), ("python/lib/x", "file", b"x")],
        }
        for label, entries in cases.items():
            with self.subTest(label):
                dest = self.tmp / f"x-{label.replace(' ', '-')}"
                with self.assertRaises(self.tool.BuildError):
                    self.tool.extract(self._archive_file(_tar(entries)), dest)
                self.assertFalse((self.tmp / "evil").exists())
        occupied = self.tmp / "occupied"
        occupied.mkdir()
        (occupied / "x").write_text("")
        with self.assertRaisesRegex(self.tool.BuildError, "not empty"):
            self.tool.extract(self._archive_file(_tar(base)), occupied)


class BytecodeTests(RuntimeTestCase):
    DDIR = "lib/python3.14/site-packages"  # the launcher package's place in a bundle

    def _tree(self, name: str) -> Path:
        tree = self.tmp / name / "src"
        (tree / "pkg" / "sub").mkdir(parents=True)
        (tree / "pkg" / "__init__.py").write_text("VALUE = 1\n")
        (tree / "pkg" / "sub" / "__init__.py").write_text("")
        (tree / "pkg" / "sub" / "mod.py").write_text(
            "def f(x):\n    return x in {'alpha', 'beta', 'gamma', 'delta'}\n")
        stale = tree / "pkg" / "__pycache__"
        stale.mkdir()
        (stale / "gone.cpython-314.pyc").write_bytes(b"stale")
        return tree

    def test_compile_is_deterministic_unchecked_hash_with_in_bundle_names(self) -> None:
        version = platform.python_version()
        first, second = self._tree("one"), self._tree("two")
        files = self.tool.compile_tree(sys.executable, first, self.DDIR, 1700000000, expected_version=version)
        self.tool.compile_tree(sys.executable, second, self.DDIR, 1800000000, expected_version=version)
        self.assertEqual(len(files), 3)
        self.assertFalse(any("gone" in f for f in files))
        self.assertFalse((first / "pkg" / "__pycache__" / "gone.cpython-314.pyc").exists())
        self.assertEqual(self.tool.pyc_digest(first), self.tool.pyc_digest(second))
        tag = sys.implementation.cache_tag
        data = (first / "pkg" / "sub" / "__pycache__" / f"mod.{tag}.pyc").read_bytes()
        self.assertEqual(int.from_bytes(data[4:8], "little"), 0b01)  # hash-based, unchecked
        code = marshal.loads(data[16:])
        self.assertEqual(code.co_filename, f"{self.DDIR}/pkg/sub/mod.py")

    def test_version_mismatch_and_absolute_ddir_refuse(self) -> None:
        tree = self._tree("three")
        with self.assertRaisesRegex(self.tool.BuildError, "the bundles pin 3.0.0"):
            self.tool.compile_tree(sys.executable, tree, self.DDIR, 0, expected_version="3.0.0")
        with self.assertRaisesRegex(self.tool.BuildError, "relative"):
            self.tool.compile_tree(sys.executable, tree, "/abs/src", 0)

    def test_command_line_compile_and_digest(self) -> None:
        tree = self._tree("cli")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp / "home"), "LC_ALL": "C"}
        (self.tmp / "home").mkdir()
        run = lambda *argv: subprocess.run([sys.executable, str(PYTHON_RUNTIME), *argv], capture_output=True, stdin=subprocess.DEVNULL,
                                           text=True, env=env, cwd=self.tmp, timeout=120)
        compiled = run("compile", "--python", sys.executable, "--tree", str(tree), "--ddir", self.DDIR, "--epoch", "1")
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        digest = run("digest", "--tree", str(tree))
        self.assertEqual(json.loads(compiled.stdout)["pyc_sha256"], json.loads(digest.stdout)["pyc_sha256"])
        refused = run("prepare", "--target", "linux-x86_64", "--dest", str(self.tmp / "d"),
                      "--cache", str(self.tmp / "empty-cache"), "--offline")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("offline", refused.stderr)


if __name__ == "__main__":
    unittest.main()
