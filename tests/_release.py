"""Paths and helpers for the release-engineering tests (installers, runtime
manifest, build and test tools, CI workflows).

These files live outside the package tree (``packaging/``, ``tools/``,
``.github/``). A checkout always has them. The Nix sandbox tree
(``tests/source-tree.nix``) stages ``packaging/``, ``tools/`` and
``.github/`` but no ``flake.nix``: tests guarded by
:func:`requires_release_tree` need only staged files and run there too;
tests guarded by :func:`requires_checkout` skip there with a ``BOUNDARY:``
reason. In a checkout a missing file is a failure, never a skip.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType

from _layout import FLAKE_NIX, REPO_ROOT, RESOURCES_ROOT

CHECKOUT = FLAKE_NIX.is_file()
PACKAGING_DIR = REPO_ROOT / "packaging"
TOOLS_DIR = REPO_ROOT / "tools"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
INSTALL_SH = PACKAGING_DIR / "install.sh"
INSTALL_PS1 = PACKAGING_DIR / "install.ps1"
RUNTIMES_JSON = PACKAGING_DIR / "python-runtimes.json"
PYTHON_RUNTIME = TOOLS_DIR / "_build" / "python_runtime.py"
TEST_RUNNER = TOOLS_DIR / "test.py"
NO_BUNDLE_GUARD = TOOLS_DIR / "_build" / "no_bundle_guard.py"
BUNDLE_TOOL = TOOLS_DIR / "_build" / "bundle.py"
RELEASE_TOOL = TOOLS_DIR / "release.py"
PRODUCT_JSON = PACKAGING_DIR / "product.json"
RUNTIME_LICENSES = PACKAGING_DIR / "licenses" / "python"
RUNTIME_LICENSES_TOOL = TOOLS_DIR / "_build" / "runtime_licenses.py"
PESTER_TESTS = REPO_ROOT / "tests" / "pwsh" / "install.Tests.ps1"
# What the release-build tests read: the staged tree carries all of it.
RELEASE_TREE = (PRODUCT_JSON, RUNTIMES_JSON, INSTALL_SH, INSTALL_PS1, PYTHON_RUNTIME, BUNDLE_TOOL, RELEASE_TOOL,
                RUNTIME_LICENSES / "inventory.json", RUNTIME_LICENSES_TOOL)

requires_checkout = unittest.skipUnless(
    CHECKOUT, "BOUNDARY: the sandbox tree stages no flake.nix (checkout-only test)"
)
# A staged tree always carries the release files; a checkout must too (a
# missing one fails the test instead of skipping it).
requires_release_tree = unittest.skipUnless(
    CHECKOUT or PACKAGING_DIR.is_dir(), "BOUNDARY: this tree stages no packaging/"
)


TRUST_RELATIVE = Path("src") / "claude_multi" / "data" / "release-trust" / "allowed_signers"


def keyless_trust_text() -> str:
    """The packaged release trust without its key lines (its comments
    only): the trust of a build that names no release key."""

    text = (RESOURCES_ROOT / "release-trust" / "allowed_signers").read_text(encoding="utf-8")
    return "".join(f"{line}\n" for line in text.splitlines() if not line.strip() or line.lstrip().startswith("#"))


def keyless_trust(directory: Path) -> Path:
    """A copy of the packaged release trust with no key, in ``directory``."""

    path = directory / "allowed_signers"
    path.write_text(keyless_trust_text(), encoding="utf-8")
    return path


def keyless_repo(directory: Path) -> Path:
    """A repository tree at ``directory`` whose packaged release trust
    names no key (the trust is its only file)."""

    trust = directory / TRUST_RELATIVE
    trust.parent.mkdir(parents=True, exist_ok=True)
    trust.write_text(keyless_trust_text(), encoding="utf-8")
    return directory


def process_running(pid: int) -> bool:
    """A test-owned process, excluding a Linux orphan awaiting reaping."""

    try:
        if sys.platform == "linux":
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            return state != "Z"
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ProcessLookupError):
        return False


def load_tool(path: Path, name: str) -> ModuleType:
    """Import a tool script by path under a private module name."""

    module_name = f"_cm_tool_{name}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
