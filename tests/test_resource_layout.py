"""The packaged resources live inside the package; nothing assumes the
repository root holds them.

The guard is ownership-aware rather than a blanket text match: a resource
name (``catalog``, ``version.json``…) is a defect only where it is joined
onto a *repository-root* owner — a repository root variable in Python, a
``../``-relative path in a Nix file, a checkout-relative path in a workflow.
Flat resource trees (``FIXTURE_ROOT / "catalog"``, the frozen test assets),
resource roots (``RESOURCES_ROOT / "schemas"``) and logical labels
(``"catalog/native-contract.json"`` as an evidence or fingerprint key) are
allowed: they never claim a repository location.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import claude_multi
from claude_multi import assets, layout
from _layout import (
    FIXTURES_ROOT, NIX_DIR, REPO_ROOT, RESOURCE_ENTRIES, RESOURCES_RELATIVE, RESOURCES_ROOT, installed_paths,
    logical_path, staged_tops,
)

# Names that denote a repository root or a source checkout, per owner.
REPOSITORY_ROOT_NAMES = {
    "src": frozenset({"checkout", "repo", "repo_root", "product_root", "verified", "candidate"}),
    "tools": frozenset({"REPO", "REPO_ROOT"}),
    "bin": frozenset({"_ROOT"}),
    "tests": frozenset({"REPO_ROOT", "FLAKE_REPO", "checkout", "repo", "product"}),
}
# The removed repository-level homes of the resources (besides the entries).
FORMER_HOMES = ("service", "gateway/gateway-contract.json")
# Deliberate repository-level joins, each with its reason: (file, join) -> why.
ALLOWED_JOINS = {
    ("tests/test_upgrade.py", "product / 'version.json'"):
        "proves a stray repository-level copy is not the checkout's resource",
}


def _python_files(top: str) -> list[Path]:
    base = REPO_ROOT / top
    if top == "bin":
        return sorted(path for path in base.iterdir() if path.is_file())
    return sorted(path for path in base.rglob("*.py") if "__pycache__" not in path.parts)


def _first_component(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        return node.value.split("/", 1)[0]
    return None


def _owner_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def repository_root_joins(path: Path, owners: frozenset[str]) -> list[str]:
    """``<repository root> / "<resource entry>…"`` joins in one Python file."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    label = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path.name
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            owner = _owner_name(node.left)
            entry = _first_component(node.right)
            if owner in owners and entry in RESOURCE_ENTRIES:
                found.append((node.lineno, f"{label}:{node.lineno}: {owner} / {node.right.value!r}"))
    return [text for _line, text in sorted(found)]


class RepositoryRootTests(unittest.TestCase):
    def test_the_repository_root_holds_no_resources(self) -> None:
        for entry in sorted(RESOURCE_ENTRIES):
            with self.subTest(entry=entry):
                self.assertFalse((REPO_ROOT / entry).exists(), f"{entry} belongs under {RESOURCES_RELATIVE}")
        for former in FORMER_HOMES:
            with self.subTest(former=former):
                self.assertFalse((REPO_ROOT / former).exists(), former)

    def test_every_resource_entry_lives_in_the_package(self) -> None:
        self.assertEqual(RESOURCES_ROOT, REPO_ROOT / "src" / "claude_multi" / "data")
        self.assertEqual(RESOURCES_ROOT, claude_multi.resources_root())
        self.assertEqual(RESOURCES_RELATIVE, layout.RESOURCES_IN_CHECKOUT)
        present = {path.name for path in RESOURCES_ROOT.iterdir() if path.name != "__pycache__"}
        self.assertEqual(present, set(RESOURCE_ENTRIES))

    def test_python_never_joins_a_resource_onto_a_repository_root(self) -> None:
        offenders, allowed = [], set()
        for top, owners in REPOSITORY_ROOT_NAMES.items():
            for path in _python_files(top):
                for found in repository_root_joins(path, owners):
                    location, join = found.split(": ", 1)
                    key = (location.rsplit(":", 1)[0], join)
                    if key in ALLOWED_JOINS:
                        allowed.add(key)
                    else:
                        offenders.append(found)
        self.assertEqual(offenders, [], "join resource paths onto RESOURCES_ROOT (tests), "
                                        "layout.checkout_resources(checkout) (src) or the resource directory")
        self.assertEqual(allowed, set(ALLOWED_JOINS), "a listed deliberate join is gone: shrink ALLOWED_JOINS")

    def test_the_guard_flags_a_repository_join_and_allows_flat_trees_and_labels(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "sample.py"
            sample.write_text(
                'a = REPO_ROOT / "catalog" / "models.json"\n'      # flagged: repository root
                'b = self.repo / "version.json"\n'                  # flagged: a checkout
                'c = FIXTURE_ROOT / "catalog"\n'                   # flat fixture tree
                'd = RESOURCES_ROOT / "schemas"\n'                 # the resources
                'e = {"catalog/native-contract.json": 1}\n'        # a logical label
                'f = REPO_ROOT / "tests" / "catalog"\n'            # a repository path
            )
            found = repository_root_joins(sample, REPOSITORY_ROOT_NAMES["tests"])
        self.assertEqual([line.split(": ", 1)[1] for line in found],
                         ["REPO_ROOT / 'catalog'", "repo / 'version.json'"])

    def test_logical_labels_resolve_against_their_owner(self) -> None:
        self.assertEqual(logical_path("schemas/models.schema.json"), RESOURCES_ROOT / "schemas/models.schema.json")
        self.assertEqual(logical_path("version.json"), RESOURCES_ROOT / "version.json")
        self.assertEqual(logical_path("src/claude_multi/scope.py"), REPO_ROOT / "src/claude_multi/scope.py")
        self.assertEqual(logical_path("nix/gateway.nix"), REPO_ROOT / "nix/gateway.nix")
        # A flat fixture tree keeps the resource-relative shape.
        self.assertTrue((FIXTURES_ROOT / "assets" / "catalog").is_dir())

    def test_nix_names_no_repository_level_resource(self) -> None:
        pattern = re.compile(r"\.\.?/((?:%s)(?![\w.-]))" % "|".join(re.escape(entry) for entry in RESOURCE_ENTRIES))
        files = sorted(NIX_DIR.glob("*.nix")) + sorted((REPO_ROOT / "tests").glob("*.nix"))
        flake = REPO_ROOT / "flake.nix"
        if flake.is_file():  # the sandbox tree stages no flake.nix
            files.append(flake)
        offenders = [f"{path.relative_to(REPO_ROOT)}: {match.group(0)}"
                     for path in files for match in pattern.finditer(path.read_text())]
        offenders += [f"{path.relative_to(REPO_ROOT)}: {former}" for path in files
                      for former in ("gateway/gateway-contract.json",) if former in path.read_text()]
        self.assertEqual(offenders, [])
        # The package installs src/ (with its resources) and the staged tree
        # carries src/; neither lists a resource entry at the top.
        self.assertFalse(set(installed_paths()) & RESOURCE_ENTRIES)
        self.assertIn("src", installed_paths())
        self.assertFalse(set(staged_tops()) & RESOURCE_ENTRIES)
        self.assertIn("src", staged_tops())

    def test_workflows_read_the_packaged_resources(self) -> None:
        workflows = REPO_ROOT / ".github" / "workflows"
        if not workflows.is_dir():
            self.skipTest("BOUNDARY: the sandbox tree stages no .github/")
        pattern = re.compile(r"(?<![\w/.-])(?:%s)/[\w./-]+" % "|".join(
            re.escape(entry) for entry in sorted(RESOURCE_ENTRIES) if "." not in entry))
        offenders = [f"{path.name}: {match.group(0)}" for path in sorted(workflows.glob("*.yml"))
                     for match in pattern.finditer(path.read_text())
                     if not path.read_text()[max(0, match.start() - len("src/claude_multi/data/")):match.start()]
                     .endswith("src/claude_multi/data/")]
        self.assertEqual(offenders, [])


class AuthorityTests(unittest.TestCase):
    """Four authorities: packaged resources, selected resources, the source
    checkout and the installation never stand in for one another."""

    def test_selected_resources_precedence(self) -> None:
        self.assertEqual(assets.root(environ={}), claude_multi.resources_root())
        self.assertEqual(assets.root(environ={"CLAUDE_MULTI_ASSETS": "/env"}), Path("/env"))
        self.assertEqual(assets.root("/explicit", environ={"CLAUDE_MULTI_ASSETS": "/env"}), Path("/explicit"))

    def test_the_installation_is_the_tree_beside_the_package(self) -> None:
        self.assertEqual(layout.installation(), REPO_ROOT)
        self.assertEqual(layout.entry_point("claude-multi"), REPO_ROOT / "bin" / "claude-multi")
        self.assertEqual(layout.document("USAGE.md"), REPO_ROOT / "docs" / "USAGE.md")
        self.assertIsNone(layout.document("ABSENT.md"))
        self.assertEqual(layout.installation_resources(REPO_ROOT), RESOURCES_ROOT)
        self.assertEqual(layout.installation_resources(RESOURCES_ROOT), RESOURCES_ROOT)
        self.assertIsNone(layout.installation_resources(REPO_ROOT / "docs"))

    def test_the_checkout_keeps_its_resources_under_the_package(self) -> None:
        self.assertEqual(layout.checkout_resources(REPO_ROOT), RESOURCES_ROOT)

    def test_an_installed_package_names_its_prefix(self) -> None:
        # The package in site-packages: the installation is the prefix, with
        # its console scripts in bin/ and its documents in share/claude-multi
        # (tests/test_python_packaging.py runs such an installation).
        for site in ("lib/python3.14/site-packages", "lib64/python3.11/site-packages",
                     "lib/python3/dist-packages"):
            with self.subTest(site=site):
                self.assertEqual(layout.installation_of(Path("/prefix") / site / "claude_multi"), Path("/prefix"))
        for elsewhere in ("/opt/claude_multi", "/prefix/lib/site-packages/claude_multi",
                          "/prefix/share/python3.14/site-packages/claude_multi"):
            with self.subTest(elsewhere=elsewhere):
                self.assertIsNone(layout.installation_of(Path(elsewhere)))
        self.assertEqual(layout.installation_of(REPO_ROOT / "src" / "claude_multi"), REPO_ROOT)
        self.assertEqual(layout.entry_point("claude-multi", "/prefix"), Path("/prefix/bin/claude-multi"))

    def test_installation_resources_of_an_installed_prefix(self) -> None:
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory(prefix="cm-prefix-") as tmp:
            prefix = Path(tmp)
            data = prefix / "lib" / "python3.14" / "site-packages" / "claude_multi" / "data"
            (data / "catalog").mkdir(parents=True)
            shutil.copy2(RESOURCES_ROOT / "version.json", data / "version.json")
            self.assertEqual(layout.installation_resources(prefix), data)
            (prefix / "share" / "claude-multi").mkdir(parents=True)
            (prefix / "share" / "claude-multi" / "USAGE.md").write_text("usage\n")
            self.assertEqual(layout.document("USAGE.md", prefix), prefix / "share" / "claude-multi" / "USAGE.md")


if __name__ == "__main__":
    unittest.main()
