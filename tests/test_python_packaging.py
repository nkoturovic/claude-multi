"""The Python package: its metadata, its three console entry points, and
the wheel and source distribution built from it.

The metadata and entry-point tests need no build backend. The build tests
use the pinned setuptools (``[build-system] requires``) from the running
interpreter's import path, offline; without that exact version they skip
with a BOUNDARY reason. A wheel built here is installed into a fresh
virtual environment outside the checkout and every entry point runs there
from a foreign directory with an empty HOME.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import claude_multi
from claude_multi import entrypoints, identity, layout
from _layout import PACKAGE_DIR, REPO_ROOT, RESOURCES_ROOT, installed_paths, pyproject

SCRIPTS = pyproject()["project"]["scripts"]
SOURCE_INPUTS = ("pyproject.toml", "MANIFEST.in", "README.md", "LICENSE")
DATA_FILES = pyproject()["tool"]["setuptools"]["data-files"]


def documents() -> list[tuple[str, str]]:
    """``(source, installed path)`` of every document, by the one layout
    rule (``tools/build.py``'s, which the bundles use)."""

    from _release import load_tool
    from _layout import BUILD_TOOL

    return [(str(source), str(target)) for source, target in load_tool(BUILD_TOOL, "build").document_layout(DATA_FILES)]


def _package_files(root: Path) -> list[str]:
    """Every file of the ``claude_multi`` package under ``root`` (relative,
    bytecode excluded)."""

    return sorted(path.relative_to(root.parent).as_posix() for path in root.rglob("*")
                  if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")


class VersionMappingTests(unittest.TestCase):
    def test_launcher_versions_map_to_pep440(self) -> None:
        for launcher, expected in (("1.0.0", "1.0.0"), ("1.0.0-dev", "1.0.0.dev0"), ("0.9.12", "0.9.12"),
                                   ("12.3.4-dev", "12.3.4.dev0")):
            with self.subTest(launcher=launcher):
                self.assertEqual(claude_multi.python_version(launcher), expected)

    def test_other_strings_are_refused(self) -> None:
        for bad in ("1.0", "1.0.0-rc1", "1.0.0.dev0", "01.0.0", "1.0.0-dev-dev", "", "v1.0.0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                claude_multi.python_version(bad)

    def test_the_package_version_follows_the_version_document(self) -> None:
        import json

        launcher = json.loads((RESOURCES_ROOT / "version.json").read_text())["launcher_version"]
        self.assertEqual(claude_multi.__version__, launcher)
        self.assertEqual(claude_multi.PACKAGE_VERSION, claude_multi.python_version(launcher))
        dynamic = pyproject()["tool"]["setuptools"]["dynamic"]["version"]
        self.assertEqual(dynamic, {"attr": "claude_multi.PACKAGE_VERSION"})
        self.assertEqual(pyproject()["project"]["dynamic"], ["version"])


class MetadataTests(unittest.TestCase):
    def test_build_backend_is_a_pinned_setuptools(self) -> None:
        system = pyproject()["build-system"]
        self.assertEqual(system["build-backend"], "setuptools.build_meta")
        self.assertEqual(len(system["requires"]), 1)
        self.assertRegex(system["requires"][0], r"^setuptools==\d+\.\d+\.\d+$")

    def test_project_metadata(self) -> None:
        project = pyproject()["project"]
        self.assertEqual(project["name"], "claude-multi")
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(project["license"], "MIT")
        self.assertTrue((REPO_ROOT / project["readme"]).is_file())

    def test_three_console_entry_points(self) -> None:
        # The product's two commands and the contributor's; `claude-multi
        # direct` starts a one-model session.
        self.assertEqual(sorted(SCRIPTS), ["claude-multi", "claude-multi-dev", "claude-multi-proxy"])
        self.assertTrue(set(SCRIPTS) <= set(entrypoints.NAMES))
        for name, target in SCRIPTS.items():
            with self.subTest(name=name):
                module, function = target.split(":")
                self.assertEqual(module, "claude_multi.entrypoints")
                self.assertEqual(function, name.replace("-", "_"))
                self.assertTrue(callable(getattr(entrypoints, function)))
        self.assertEqual(sorted(entrypoints.PROGRAMS), sorted(entrypoints.NAMES))

    def test_package_data_and_documents(self) -> None:
        setuptools = pyproject()["tool"]["setuptools"]
        self.assertEqual(setuptools["package-dir"], {"": "src"})
        self.assertEqual(setuptools["package-data"], {"claude_multi": ["data/**/*"]})
        # Every key is share/claude-multi or a directory under it, every file
        # lies under docs/ in that directory (the build tool refuses others).
        placed = documents()
        for source, target in placed:
            self.assertTrue((REPO_ROOT / source).is_file(), source)
            self.assertEqual(Path(target).relative_to("share/claude-multi"), Path(source).relative_to("docs"))
        # The help's documents stay at the top.
        for name in layout.DOCUMENTS:
            self.assertIn((f"docs/{name}", f"share/claude-multi/{name}"), placed)
        self.assertIn(Path("share") / "claude-multi", layout.DOCUMENT_DIRS)
        self.assertEqual(installed_paths(), ["src", *(source for source, _target in placed)])

    def test_a_nested_document_resolves_in_an_installation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cm-nested-doc-") as tmp:
            prefix = Path(tmp)
            nested = prefix / "share" / "claude-multi" / "reference" / "licenses.md"
            nested.parent.mkdir(parents=True)
            nested.write_text("# Licences\n")
            self.assertEqual(layout.document("reference/licenses.md", prefix), nested)
            self.assertIsNone(layout.document("reference/absent.md", prefix))

    def test_manifest_carries_every_build_input(self) -> None:
        manifest = (REPO_ROOT / "MANIFEST.in").read_text().splitlines()
        rules = [line.split() for line in manifest if line.strip() and not line.startswith("#")]
        included = {word for rule in rules if rule[0] == "include" for word in rule[1:]}
        self.assertTrue(set(SOURCE_INPUTS) <= included, included)
        self.assertTrue({source for source, _target in documents()} <= included)
        self.assertIn(["graft", "src/claude_multi"], rules)
        self.assertIn(["global-exclude", "__pycache__", "*.py[cod]"], rules)


class EntryPointTests(unittest.TestCase):
    """The launch environment each way of starting a command states."""

    INHERITED = {
        "CLAUDE_MULTI_CHANNEL": "nix",
        "CLAUDE_MULTI_ASSETS": "/elsewhere/assets",
        "CLAUDE_MULTI_HOOK_COMMAND": "/elsewhere/bin/claude-multi",
        "CLAUDE_MULTI_PROXY_BIN": "/elsewhere/gateway",
        "UNRELATED": "kept",
    }

    def test_a_console_script_takes_no_wrapper_variable_from_its_caller(self) -> None:
        env = dict(self.INHERITED)
        with mock.patch.object(layout, "installation", return_value=Path("/prefix")):
            entrypoints.prepare_console(env)
        # The channel stays unknown, the resources and the hook command are
        # the installation's own; a gateway binary the caller names is kept
        # (it attests nothing without the Nix channel).
        self.assertEqual(env, {"CLAUDE_MULTI_PROXY_BIN": "/elsewhere/gateway", "UNRELATED": "kept"})

    def test_an_editable_install_is_a_source_launch(self) -> None:
        env = dict(self.INHERITED)
        with mock.patch.object(layout, "installation", return_value=REPO_ROOT):
            entrypoints.prepare_console(env)
        self.assertEqual(env["CLAUDE_MULTI_CHANNEL"], "source")
        self.assertNotIn("CLAUDE_MULTI_ASSETS", env)

    def test_a_source_launcher_outside_a_source_tree_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cm-not-source-") as tmp:
            env = dict(self.INHERITED)
            self.assertFalse(entrypoints.prepare_source(tmp, env))
            self.assertEqual(env, self.INHERITED)
            self.assertTrue(entrypoints.prepare_source(REPO_ROOT, env))
            self.assertEqual(env["CLAUDE_MULTI_CHANNEL"], "source")
            self.assertNotIn("CLAUDE_MULTI_ASSETS", env)
            self.assertEqual(env["CLAUDE_MULTI_HOOK_COMMAND"], "/elsewhere/bin/claude-multi")

    def test_the_source_markers_are_present_here_and_never_installed(self) -> None:
        installed = installed_paths()
        for marker in entrypoints.SOURCE_MARKERS:
            with self.subTest(marker=marker):
                self.assertTrue((REPO_ROOT / marker).is_file())
        # The package recipe is never part of what an installation carries.
        self.assertFalse(any("nix/package.nix" == item or "nix/package.nix".startswith(item + "/")
                             for item in installed))

    def test_the_programs(self) -> None:
        calls = []

        def fake_cli(argv=None, **_kwargs):
            calls.append(("cli", argv))
            return 0

        def fake_proxy(argv=None, **kwargs):
            calls.append(("proxy", argv, kwargs))
            return 0

        with mock.patch("claude_multi.cli.entry.main", fake_cli), mock.patch("claude_multi.proxy.main", fake_proxy), \
                redirect_stdout(io.StringIO()) as shown:
            entrypoints.run("claude-multi", ["doctor"])
            entrypoints.run("claude-multi", ["--version"])
            entrypoints.run("claude-multi-proxy", ["status"])
        self.assertEqual(calls, [
            ("cli", ["doctor"]),
            ("proxy", ["status"], {"chdir": os.chdir}),
        ])
        self.assertEqual(shown.getvalue(), identity.version_line() + "\n")
        # The one-model session is `claude-multi direct`: no other program.
        self.assertEqual(entrypoints.NAMES, ("claude-multi", "claude-multi-proxy", "claude-multi-dev"))
        self.assertEqual(set(entrypoints.PROGRAMS), set(entrypoints.NAMES))
        with self.assertRaises(KeyError):
            entrypoints.run("claude-gateway", [])

    def test_the_module_form_needs_a_command(self) -> None:
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoints.main([]), 2)
            self.assertEqual(entrypoints.main(["claude-other"]), 2)
        self.assertIn("python3 -P -m claude_multi.entrypoints", err.getvalue())

    def test_the_source_launchers_run_the_same_entry_points(self) -> None:
        for name in entrypoints.NAMES:
            with self.subTest(name=name):
                tree = ast.parse((REPO_ROOT / "bin" / name).read_text())
                imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
                modules = sorted({alias.name for node in imports if isinstance(node, ast.Import)
                                  for alias in node.names}
                                 | {node.module for node in imports if isinstance(node, ast.ImportFrom)})
                self.assertEqual(modules, ["claude_multi.entrypoints", "os", "sys"])
                calls = {ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}
                self.assertIn("prepare_source(_ROOT)", calls)
                self.assertIn(f"run({name!r})", calls)

    def test_the_module_form_runs_each_command(self) -> None:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(REPO_ROOT / "src"),
               "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8"}
        with tempfile.TemporaryDirectory(prefix="cm-module-form-") as tmp:
            env["HOME"] = tmp
            for name in entrypoints.NAMES:
                with self.subTest(name=name):
                    result = subprocess.run([sys.executable, "-P", "-m", "claude_multi.entrypoints", name, "--version"],
                                            env=env, cwd=tmp, text=True, capture_output=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), version_output(name))
            self.assertEqual(os.listdir(tmp), [])


def version_output(name: str) -> str:
    """What ``NAME --version`` prints: the release identity line for
    claude-multi and claude-multi-dev, the plain version for the gateway
    tool."""

    if name == "claude-multi":
        return identity.version_line()
    if name == "claude-multi-dev":
        return identity.version_line(name)
    return f"{name} {claude_multi.__version__}"


def _console_script(target: str) -> str:
    """The console-script launcher an installer writes for ``module:function``."""

    module, function = target.split(":")
    return (f"#!{sys.executable}\n"
            "import sys\n"
            f"from {module} import {function}\n"
            "if __name__ == '__main__':\n"
            f"    sys.exit({function}())\n")


INSTALLED_PROBE = """
import json, os, sys
from pathlib import Path
prefix = Path(sys.argv[1])
# What a console script does first: the inherited wrapper variables go.
os.environ["CLAUDE_MULTI_HOOK_COMMAND"] = "/elsewhere/bin/claude-multi"
from claude_multi import entrypoints
entrypoints.prepare_console()
assert not {"CLAUDE_MULTI_CHANNEL", "CLAUDE_MULTI_ASSETS", "CLAUDE_MULTI_HOOK_COMMAND"} & set(os.environ)
import claude_multi
from claude_multi import assets, catalog, gateway_service, layout, qualify, scope
package = Path(claude_multi.__file__).resolve().parent
resources = package / "data"
assert claude_multi.resources_root() == resources, claude_multi.resources_root()
assert package.parent.name == "site-packages", package
assert layout.installation() == prefix, layout.installation()
assert assets.root() == resources
assert scope.resolve_hook_command() == str(prefix / "bin" / "claude-multi"), scope.resolve_hook_command()
for name in layout.DOCUMENTS:
    assert layout.document(name) == prefix / "share" / "claude-multi" / name, name
bundle = catalog.load_catalog(assets.root())
assert catalog.registry_dir() == resources / "registry"
assert qualify.contract_identity(bundle.docs["native-contract"], assets.root()).gateway is not None
assert gateway_service.load_spec()["unit"]
assert layout.installation_resources(prefix) == resources
print(json.dumps({"version": claude_multi.__version__}))
"""


class InstalledPrefixTests(unittest.TestCase):
    """The package as an installer lays it out (a prefix with the package in
    site-packages, the documents in share/claude-multi and the console
    scripts in bin/), outside the checkout, run from a foreign directory
    with an empty HOME and no inherited wrapper environment. Needs no build
    backend, so it also runs in the Nix sandbox."""

    def assert_installed(self, prefix: Path, python: str, env: dict[str, str], elsewhere: Path, home: Path) -> None:
        for name in SCRIPTS:
            with self.subTest(entry=name):
                result = subprocess.run([str(prefix / "bin" / name), "--version"], env=env, cwd=elsewhere,
                                        text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), version_output(name))
        help_text = subprocess.run([str(prefix / "bin" / "claude-multi"), "--help"], env=env, cwd=elsewhere,
                                   text=True, capture_output=True, timeout=60)
        self.assertEqual(help_text.returncode, 0, help_text.stderr)
        self.assertIn(f"Symptom → command: {prefix / 'share' / 'claude-multi' / 'CHEATSHEET.md'}", help_text.stdout)
        probe = subprocess.run([python, "-c", INSTALLED_PROBE, str(prefix)], env=env, cwd=elsewhere,
                               text=True, capture_output=True, timeout=60)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn(claude_multi.__version__, probe.stdout)
        # Nothing was written into the home.
        self.assertEqual(sorted(path.name for path in home.iterdir()), [])

    def test_an_installed_prefix_runs_outside_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cm-installed-prefix-") as tmp:
            root = Path(tmp).resolve()
            prefix = root / "prefix"
            version = f"python{sys.version_info.major}.{sys.version_info.minor}"
            site = prefix / "lib" / version / "site-packages"
            site.mkdir(parents=True)
            shutil.copytree(PACKAGE_DIR, site / "claude_multi", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for directory, files in pyproject()["tool"]["setuptools"]["data-files"].items():
                (prefix / directory).mkdir(parents=True)
                for item in files:
                    shutil.copy2(REPO_ROOT / item, prefix / directory)
            (prefix / "bin").mkdir()
            for name, target in SCRIPTS.items():
                script = prefix / "bin" / name
                script.write_text(_console_script(target))
                script.chmod(0o755)
            self.assertEqual(_package_files(site / "claude_multi"), _package_files(PACKAGE_DIR))
            home, elsewhere = root / "home", root / "elsewhere"
            home.mkdir()
            elsewhere.mkdir()
            env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(site),
                   # Inherited from another installation: never taken.
                   "CLAUDE_MULTI_CHANNEL": "nix", "CLAUDE_MULTI_ASSETS": str(root / "absent")}
            self.assert_installed(prefix, sys.executable, env, elsewhere, home)


def _pinned_setuptools() -> tuple[Path | None, str]:
    """The import root of the pinned setuptools, or None with the reason."""

    required = pyproject()["build-system"]["requires"][0].split("==", 1)[1]
    spec = importlib.util.find_spec("setuptools")
    if spec is None or spec.origin is None:
        return None, f"BOUNDARY: no setuptools {required} on this interpreter's path (the pinned build backend)"
    root = Path(spec.origin).resolve().parent.parent
    found = None
    for info in root.glob("setuptools-*.dist-info"):
        found = info.name.removeprefix("setuptools-").removesuffix(".dist-info")
    if found != required:
        return None, f"BOUNDARY: setuptools {found or 'of unknown version'} at {root}; the build pins {required}"
    return root, ""


def _backend_path(root: Path, scratch: Path) -> Path:
    """A directory holding only the build backend (no other site packages)."""

    backend = scratch / "backend"
    backend.mkdir()
    # The packages and the distribution metadata (its commands are entry points).
    names = ["setuptools", "_distutils_hack", "pkg_resources",
             *(info.name for info in root.glob("setuptools-*.dist-info"))]
    for name in names:
        if (root / name).exists():
            (backend / name).symlink_to(root / name, target_is_directory=True)
    return backend


def _build(source: Path, out: Path, backend: Path, kinds: tuple[str, ...]) -> list[Path]:
    """Each distribution ``kind`` built in a process of its own (the backend
    keeps state between builds in one process)."""

    env = {"PATH": "/usr/bin:/bin", "HOME": str(out.parent / "build-home"), "LC_ALL": "C.UTF-8",
           "PYTHONPATH": str(backend), "PYTHONDONTWRITEBYTECODE": "1", "SOURCE_DATE_EPOCH": "315532800"}
    Path(env["HOME"]).mkdir(exist_ok=True)
    built = []
    for kind in kinds:
        code = f"import setuptools.build_meta as backend\nprint('built:' + backend.build_{kind}({str(out)!r}))\n"
        result = subprocess.run([sys.executable, "-s", "-c", code], cwd=source, env=env,
                                text=True, capture_output=True, timeout=300)
        if result.returncode != 0:
            raise AssertionError(f"{kind} build failed:\n{result.stdout[-2000:]}\n{result.stderr[-4000:]}")
        names = [line.removeprefix("built:") for line in result.stdout.splitlines() if line.startswith("built:")]
        if len(names) != 1 or not (out / names[0]).is_file():
            raise AssertionError(f"the {kind} build named {names}, not a file in {out}")
        built.append(out / names[0])
    return built


def _wheel_index(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as wheel:
        return {name: hashlib.sha256(wheel.read(name)).hexdigest() for name in wheel.namelist()
                if not name.endswith("/")}


class DistributionTests(unittest.TestCase):
    """An offline wheel and source distribution, built with the pinned
    backend; the wheel installed into a virtual environment outside the
    checkout runs every entry point."""

    @classmethod
    def setUpClass(cls) -> None:
        root, reason = _pinned_setuptools()
        if root is None:
            raise unittest.SkipTest(reason)
        cls.scratch = Path(tempfile.mkdtemp(prefix="cm-dist-")).resolve()
        source = cls.scratch / "source"
        source.mkdir()
        for name in SOURCE_INPUTS:
            shutil.copy2(REPO_ROOT / name, source / name)
        for item, _target in documents():
            (source / item).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / item, source / item)
        shutil.copytree(PACKAGE_DIR, source / "src" / "claude_multi",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        cls.backend = _backend_path(root, cls.scratch)
        out = cls.scratch / "dist"
        out.mkdir()
        cls.wheel, cls.sdist = _build(source, out, cls.backend, ("wheel", "sdist"))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.scratch, True)

    def test_the_wheel_carries_the_package_documents_and_entry_points(self) -> None:
        version = claude_multi.PACKAGE_VERSION
        self.assertEqual(self.wheel.name, f"claude_multi-{version}-py3-none-any.whl")
        index = _wheel_index(self.wheel)
        package = sorted(name for name in index if name.startswith("claude_multi/"))
        self.assertEqual(package, _package_files(PACKAGE_DIR))
        for name in package:
            self.assertEqual(index[name], hashlib.sha256((REPO_ROOT / "src" / name).read_bytes()).hexdigest(), name)
        data = f"claude_multi-{version}.data/data"
        self.assertEqual(sorted(name for name in index if name.startswith(f"claude_multi-{version}.data/")),
                         sorted(f"{data}/{target}" for _source, target in documents()))
        with zipfile.ZipFile(self.wheel) as wheel:
            metadata = wheel.read(f"claude_multi-{version}.dist-info/METADATA").decode()
            entry_points = wheel.read(f"claude_multi-{version}.dist-info/entry_points.txt").decode()
        self.assertIn(f"\nVersion: {version}\n", metadata)
        self.assertIn("\nRequires-Python: >=3.11\n", metadata)
        self.assertNotIn("Requires-Dist:", metadata)
        for name, target in SCRIPTS.items():
            self.assertIn(f"{name} = {target}", entry_points)

    def test_the_installed_documents_link_only_inside_the_installation(self) -> None:
        # The wheel's documents unpacked where an installation puts them
        # (share/claude-multi, nested directories included): every relative
        # link and heading anchor resolves inside that tree, a root page is
        # reached only by its public URL, and the help's document lookup
        # finds the installed pages.
        import _docs_vocab as vocab

        data = f"claude_multi-{claude_multi.PACKAGE_VERSION}.data/data/"
        prefix = self.scratch / "installed-documents"
        with zipfile.ZipFile(self.wheel) as wheel:
            for name in wheel.namelist():
                if name.startswith(data + "share/claude-multi/") and not name.endswith("/"):
                    target = prefix / name.removeprefix(data)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(wheel.read(name))
        share = prefix / "share" / "claude-multi"
        pages = sorted(path.relative_to(share).as_posix() for path in share.rglob("*.md"))
        self.assertEqual(pages, sorted(Path(target).relative_to("share/claude-multi").as_posix()
                                       for _source, target in documents()))
        self.assertTrue(any("/" in page for page in pages))
        self.assertEqual(vocab.installed_link_problems(share), [])
        for name in layout.DOCUMENTS:
            self.assertEqual(layout.document(name, prefix), share / name)

    def test_the_source_distribution_rebuilds_the_same_wheel(self) -> None:
        with tarfile.open(self.sdist) as archive:
            names = archive.getnames()
            top = f"claude_multi-{claude_multi.PACKAGE_VERSION}"
            for name in SOURCE_INPUTS:
                self.assertIn(f"{top}/{name}", names)
            self.assertFalse([name for name in names if "__pycache__" in name or name.endswith(".pyc")])
            unpacked = self.scratch / "unpacked"
            archive.extractall(unpacked, filter="data")
        out = self.scratch / "rebuilt"
        out.mkdir()
        (wheel,) = _build(unpacked / top, out, self.backend, ("wheel",))
        first = {name: digest for name, digest in _wheel_index(self.wheel).items() if ".dist-info/" not in name}
        second = {name: digest for name, digest in _wheel_index(wheel).items() if ".dist-info/" not in name}
        self.assertEqual(first, second)

    def test_an_editable_install_runs_the_source_tree(self) -> None:
        # The contributor's install: the console scripts run the checkout's
        # source, name the source channel, and the suite runs through it.
        checkout = self.scratch / "checkout"
        shutil.copytree(REPO_ROOT, checkout, ignore=shutil.ignore_patterns(
            ".git", ".claude", "__pycache__", "*.pyc", "result", "result-*", "build", "dist", "*.egg-info"),
            symlinks=True)
        venv = self.scratch / "editable"
        created = subprocess.run([sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True,
                                 timeout=300, env={"PATH": "/usr/bin:/bin", "HOME": str(self.scratch)})
        if created.returncode != 0:
            self.skipTest(f"BOUNDARY: no virtual environment with pip on this interpreter: {created.stderr[-300:]}")
        python = venv / "bin" / "python"
        installed = subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps",
                                    "--no-build-isolation", "--disable-pip-version-check", "--quiet", "-e",
                                    str(checkout)], capture_output=True, text=True, timeout=300,
                                   env={"PATH": "/usr/bin:/bin", "HOME": str(self.scratch), "PIP_NO_INPUT": "1",
                                        "PYTHONPATH": str(self.backend)})
        self.assertEqual(installed.returncode, 0, installed.stderr)
        home, elsewhere = self.scratch / "editable-home", self.scratch / "editable-cwd"
        home.mkdir()
        elsewhere.mkdir()
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
               "CLAUDE_MULTI_CHANNEL": "nix"}
        for name in SCRIPTS:
            with self.subTest(entry=name):
                result = subprocess.run([str(venv / "bin" / name), "--version"], env=env, cwd=elsewhere,
                                        text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
        probe = ("import os; from claude_multi import entrypoints, layout; entrypoints.prepare_console(); "
                 "print(layout.installation(), os.environ.get('CLAUDE_MULTI_CHANNEL'))")
        result = subprocess.run([str(python), "-c", probe], env=env, cwd=elsewhere, text=True,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [str(checkout), "source"])
        # The suite through the editable install: no src/ on PYTHONPATH.
        suite = subprocess.run([str(python), "-m", "unittest", "tests.test_python_packaging.VersionMappingTests",
                                "tests.test_resource_layout.AuthorityTests"],
                               env={**env, "PYTHONPATH": str(checkout / "tests")}, cwd=checkout,
                               text=True, capture_output=True, timeout=300)
        self.assertEqual(suite.returncode, 0, suite.stderr[-3000:])
        self.assertEqual(sorted(path.name for path in home.iterdir()), [])

    def test_the_installed_wheel_runs_outside_the_checkout(self) -> None:
        venv = self.scratch / "venv"
        created = subprocess.run([sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True,
                                 timeout=300, env={"PATH": "/usr/bin:/bin", "HOME": str(self.scratch)})
        if created.returncode != 0:
            self.skipTest(f"BOUNDARY: no virtual environment with pip on this interpreter: {created.stderr[-300:]}")
        python = venv / "bin" / "python"
        installed = subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps",
                                    "--disable-pip-version-check", "--quiet", str(self.wheel)],
                                   capture_output=True, text=True, timeout=300,
                                   env={"PATH": "/usr/bin:/bin", "HOME": str(self.scratch), "PIP_NO_INPUT": "1"})
        self.assertEqual(installed.returncode, 0, installed.stderr)
        home, elsewhere = self.scratch / "home", self.scratch / "elsewhere"
        home.mkdir()
        elsewhere.mkdir()
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
               "CLAUDE_MULTI_CHANNEL": "nix", "CLAUDE_MULTI_ASSETS": str(self.scratch / "absent")}
        InstalledPrefixTests.assert_installed(self, venv, str(python), env, elsewhere, home)
        site = next((venv / "lib").glob("python*/site-packages"))
        self.assertEqual(_package_files(site / "claude_multi"), _package_files(PACKAGE_DIR))


if __name__ == "__main__":
    unittest.main()
