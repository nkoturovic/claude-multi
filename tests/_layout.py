"""Single source of every repository path the suite reads.

Roots, by ownership (no test computes ``Path(__file__).parents[…]`` or a
repository marker itself; every path comes from these names):

- ``REPO_ROOT``: the repository root (``flake.nix``, ``pyproject.toml``,
  ``src/``, ``bin/``, ``nix/``, ``gateway/``, ``docs/``, ``tests/``). In the
  Nix sandbox it is the staged tree (``tests/source-tree.nix``), which
  carries no ``flake.nix``/``flake.lock``. Source, entry points, child
  ``PYTHONPATH`` and every checkout-relative path come from here
  (``SRC_DIR``, ``PACKAGE_DIR``).
- ``RESOURCES_ROOT``: the packaged runtime resources
  (``src/claude_multi/data``: ``version.json``, ``settings.json``,
  ``catalog/``, ``schemas/``, ``presets/``, ``examples/``,
  ``gateway-unit.json``, ``gateway-contract.json``, ``registry/``,
  ``release-trust/``, ``account-pools.json``).
  Resource-relative names (``catalog/models.json``) join onto it.
- ``FIXTURES_ROOT``: ``tests/fixtures`` (frozen asset roots, recorded
  inputs; flat resource trees such as ``fixtures/assets`` keep the
  resource-relative shape). ``PROBE_BASELINES``: the recorded classes of the
  real-binary probes.
- ``GATEWAY_DIR``: the gateway recipe ``UPSTREAM.json`` (``UPSTREAM_JSON``).
- ``PATCH_DIR``: the admitted gateway patches (basenames are identities).
- ``CANDIDATE_DIR``: gateway patches that are never shipped.
- ``LICENSES_DIR``, ``SBOM_DIR``: the generated licence notices and
  per-target SBOMs under ``gateway/``; ``GATEWAY_CONTRACT`` and
  ``REGISTRY_SNAPSHOT``: the generated, checked-in resources.
- ``UPSTREAM_REVIEW``: the reviewed upstream fixes after the pinned release
  and their dispositions.
- ``TOOLS_DIR``: the build tool ``build.py`` (``BUILD_TOOL``) and the
  repository scripts (``history_scan.py``, ``pin_claude.py``, ``test.py``);
  never installed, imported by tests through :func:`load_tool`.
- ``NIX_DIR``: ``package.nix``, ``gateway.nix``, ``devshell.nix``.
- ``SERVICE_SPEC``: the supervised gateway unit's spec (a packaged resource).
- ``DOCS_DIR``: the user docs (``USAGE.md``, ``CHEATSHEET.md``,
  ``STANDALONE.md``); the contributor docs sit at the repository root
  (:func:`doc_path`).

A test that runs copies of the entry points *as a source tree* builds one
with :func:`checkout_replica`; a developer-command test that needs a
writable source checkout builds one with :func:`fake_checkout`.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
PACKAGE_DIR = SRC_DIR / "claude_multi"
# The packaged resources inside the source tree (checkout-relative path:
# RESOURCES_RELATIVE), and the test fixtures.
RESOURCES_RELATIVE = Path("src") / "claude_multi" / "data"
RESOURCES_ROOT = REPO_ROOT / RESOURCES_RELATIVE
# Where an installed package keeps its resources, relative to its prefix
# (the Nix package and a wheel install the package into site-packages).
INSTALLED_RESOURCES_RELATIVE = Path("lib") / "python3.14" / "site-packages" / "claude_multi" / "data"
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures"
GATEWAY_DIR = REPO_ROOT / "gateway"
UPSTREAM_JSON = GATEWAY_DIR / "UPSTREAM.json"
PATCH_DIR = GATEWAY_DIR / "patches"
CANDIDATE_DIR = GATEWAY_DIR / "candidates"
LICENSES_DIR = GATEWAY_DIR / "licenses"
SBOM_DIR = GATEWAY_DIR / "sbom"
GATEWAY_CONTRACT = RESOURCES_ROOT / "gateway-contract.json"
REGISTRY_SNAPSHOT = RESOURCES_ROOT / "registry"
UPSTREAM_REVIEW = GATEWAY_DIR / "upstream-review.json"
TOOLS_DIR = REPO_ROOT / "tools"
BUILD_TOOL = TOOLS_DIR / "build.py"
NIX_DIR = REPO_ROOT / "nix"
SERVICE_SPEC = RESOURCES_ROOT / "gateway-unit.json"
DOCS_DIR = REPO_ROOT / "docs"

FLAKE_NIX = REPO_ROOT / "flake.nix"
FLAKE_LOCK = REPO_ROOT / "flake.lock"
PROBE_BASELINES = FIXTURES_ROOT / "probe-baselines.txt"

# The package docs by name (labels stay names): the user docs live in docs/,
# the README and the contributor docs at the repository root.
_ROOT_DOC_NAMES = frozenset({"README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"})

# The asset entries of an asset root (catalog loading reads only these); a
# copy of a resource root as an asset root copies exactly these.
ASSET_ENTRIES = ("catalog", "schemas", "settings.json", "version.json")
# The markers of a source checkout (claude_multi.layout.CHECKOUT_MARKERS).
CHECKOUT_MARKERS = ("flake.nix", "src/claude_multi/__init__.py")
# The top-level entries of the packaged resources: a logical label that
# starts with one of them (``schemas/models.schema.json``, ``version.json``)
# is resource-relative; every other label is repository-relative.
RESOURCE_ENTRIES = frozenset({
    "catalog", "schemas", "presets", "examples", "registry", "settings.json", "version.json",
    "gateway-unit.json", "gateway-contract.json", "release-trust", "account-pools.json",
})

# Never part of the scanned or copied source tree: VCS metadata, the
# worktrees and session files under .claude/, build links and bytecode.
_EXCLUDED_TOP = frozenset({".git", ".claude"})
_EXCLUDED_NAMES = frozenset({"__pycache__"})


def checkout_replica(root: Path) -> Path:
    """A minimal source tree at ``root``: the ``nix/package.nix`` marker,
    copies of the ``bin/`` entry points and ``src`` linked to this tree's. The
    entry points detect it as a source tree (never an installed one) in the
    host tree and in the sandbox tree alike; the package resources still
    resolve to this tree. Returns ``root``."""

    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "nix").mkdir(exist_ok=True)
    (root / "nix" / "package.nix").write_text("{}\n")
    for script in sorted((REPO_ROOT / "bin").iterdir()):
        if script.is_file():
            shutil.copy2(script, root / "bin" / script.name)
    (root / "src").symlink_to(SRC_DIR, target_is_directory=True)
    return root


def fake_checkout(root: Path, resources: Path, entries: tuple[str, ...] | None = None) -> Path:
    """A writable source checkout at ``root`` for developer-command tests: the
    checkout markers and copies of ``resources`` (every entry, or only
    ``entries``) at the checkout's resource path. Returns ``root``."""

    for marker in CHECKOUT_MARKERS:
        (root / marker).parent.mkdir(parents=True, exist_ok=True)
        (root / marker).write_text("{}\n" if marker.endswith(".nix") else "")
    target = root / RESOURCES_RELATIVE
    if entries is None:
        shutil.copytree(resources, target, dirs_exist_ok=True)
    else:
        target.mkdir(parents=True, exist_ok=True)
        for name in entries:
            if (resources / name).is_dir():
                shutil.copytree(resources / name, target / name)
            else:
                shutil.copy2(resources / name, target / name)
    # Copies may come from a read-only store path; make them writable.
    for directory, _dirs, files in os.walk(target):
        os.chmod(directory, 0o755)
        for name in files:
            os.chmod(Path(directory) / name, 0o644)
    return root


def logical_path(label: str) -> Path:
    """Resolve a logical label (an evidence or fingerprint key) against its
    owner: resource-relative labels against ``RESOURCES_ROOT``, the rest
    against ``REPO_ROOT``. The labels themselves never change."""

    return (RESOURCES_ROOT if label.split("/", 1)[0] in RESOURCE_ENTRIES else REPO_ROOT) / label


def doc_path(name: str) -> Path:
    """Where the package doc ``name`` lives (``AGENTS.md``, ``USAGE.md``, …)."""

    return REPO_ROOT / name if name in _ROOT_DOC_NAMES else DOCS_DIR / name


def load_tool(name: str):
    """Import ``tools/<name>.py`` (the tools are scripts, not a package)."""

    import importlib.util
    import sys

    module_name = f"_cm_tool_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, TOOLS_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"no tool {name} under {TOOLS_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclasses resolve their module while the class is built
    spec.loader.exec_module(module)
    return module


def pyproject() -> dict:
    """``pyproject.toml``, parsed."""

    import tomllib

    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def installed_paths() -> list[str]:
    """What the package installs, as ``pyproject.toml`` declares it (the Nix
    package installs the same list): the package directory (``src``: the
    ``claude_multi`` package with its resources) and the documents."""

    setuptools = pyproject()["tool"]["setuptools"]
    paths = [setuptools["package-dir"][""]]
    for files in setuptools["data-files"].values():
        paths.extend(files)
    return paths


def staged_tops() -> list[str]:
    """``tests/source-tree.nix`` ``tops``: the tree the Nix sandbox checks stage."""

    import re

    text = (REPO_ROOT / "tests" / "source-tree.nix").read_text()
    declared = re.search(r"\n  tops = \[\n(.*?)\n  \];\n", text, re.DOTALL)
    assert declared is not None, "tests/source-tree.nix tops"
    return re.findall(r'^\s*"([^"]+)"$', declared.group(1), re.MULTILINE)


def tree_files(root: Path = REPO_ROOT):
    """Every file of the source tree under ``root``, sorted.

    Skips VCS metadata, ``.claude/``, ``result*`` build links and bytecode,
    so a scan of the repository root covers every product, test, tool and
    documentation path.
    """

    found = []
    for directory, dirs, files in os.walk(root):
        here = Path(directory)
        kept = []
        for name in dirs:
            child = here / name
            if here == root and (name in _EXCLUDED_TOP or name == "result" or name.startswith("result-")):
                continue
            if name in _EXCLUDED_NAMES or child.is_symlink():
                continue
            kept.append(name)
        dirs[:] = kept
        for name in files:
            path = here / name
            if here == root and (name == "result" or name.startswith("result-")):
                continue
            if name.endswith(".pyc") or (here == root and name in _EXCLUDED_TOP):
                continue
            if path.is_file():
                found.append(path)
    yield from sorted(found)


def repository_files(root: Path = REPO_ROOT) -> list[Path]:
    """The repository's own files under ``root``, sorted: in a git checkout
    the :func:`tree_files` that git tracks or would add (untracked files
    ``.gitignore`` does not exclude); in a tree without git metadata (a
    staged source tree, the Nix sandbox) every one of them.

    Ignored local output (a virtual environment, ``build/``, egg-info) is
    third-party or generated content, not the repository's: the naming
    gates read this list, while the credential gate reads every file
    (:func:`tree_files`). Without a usable ``git`` the list is every file,
    which only makes those gates stricter.
    """

    import subprocess

    files = list(tree_files(root))
    if not (root / ".git").exists() or shutil.which("git") is None:
        return files
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=True, capture_output=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return files
    owned = {os.fsdecode(name) for name in listed.split(b"\0") if name}
    return [path for path in files if path.relative_to(root).as_posix() in owned]
