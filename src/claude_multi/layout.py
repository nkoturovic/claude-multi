"""Where claude-multi's files are, by authority.

Four separate authorities answer four different questions; none stands in
for another:

- **Packaged resources** (:func:`claude_multi.resources_root`): the
  read-only data inside the installed package (``claude_multi/data``). The
  running release's identity comes only from here.
- **Selected resources** (:func:`claude_multi.assets.root`): what a command
  reads — an explicit root, then ``CLAUDE_MULTI_ASSETS``, then the packaged
  resources. A selection never moves an executable, a document or a write.
- **Source checkout** (:func:`verify_checkout`, :func:`checkout_resources`,
  :func:`checkout_destination`): the only place developer commands write.
  Its resources sit at :data:`RESOURCES_IN_CHECKOUT`, and every write
  destination must stay inside the checkout through real directories.
- **Installation** (:func:`installation`, :func:`entry_point`,
  :func:`document`, :func:`installation_resources`): where the running
  launcher was installed — a source tree, or the prefix of an installed
  package — with its ``bin/`` entry points and its documents, and the
  resources of another installation named by path.

A standard-library leaf: the hook path imports it through the parser.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import claude_multi
from claude_multi import errors

# A source checkout: the flake and this package's source at the top.
CHECKOUT_MARKERS = ("flake.nix", "src/claude_multi/__init__.py")
# Where a checkout keeps the packaged resources (checkout-relative).
RESOURCES_IN_CHECKOUT = Path("src") / "claude_multi" / "data"
# An installed package sits at <prefix>/lib/pythonX.Y/site-packages/claude_multi
# (a virtual environment, a user or system prefix, the Nix package); its
# console scripts are in <prefix>/bin.
SITE_DIRS = ("site-packages", "dist-packages")
LIB_DIRS = ("lib", "lib64")
# Where an installation keeps its resources, relative to the path that names
# it: an installation prefix (the package under lib/pythonX.Y/site-packages),
# a source tree holding src/claude_multi, or a resource directory itself.
INSTALLED_PACKAGE_GLOB = "lib*/python*/*-packages/claude_multi"
INSTALLED_RESOURCES = (
    RESOURCES_IN_CHECKOUT,
    Path("."),
)
# The documents: an installed package puts them in its share path
# (pyproject.toml, [tool.setuptools.data-files]); a source tree keeps them
# under docs/. The names are the same in both.
DOCUMENTS = ("USAGE.md", "CHEATSHEET.md", "STANDALONE.md")
DOCUMENT_DIRS = (Path("share") / "claude-multi", Path("docs"))
NIX_STORE = "/nix/store"


class LayoutError(errors.ClaudeMultiError, ValueError):
    """A path is not a usable source checkout or write destination."""


# ------------------------------------------------------------ installation


def installation() -> Path | None:
    """Where the running package was installed: a source tree (the directory
    holding ``src/claude_multi``, with ``bin/`` and ``docs/``), or the prefix
    of an installed package (``<prefix>/lib/pythonX.Y/site-packages``, with
    ``bin/`` and ``share/claude-multi``). None for any other placement."""

    return installation_of(Path(claude_multi.__file__).resolve().parent)


def installation_of(package: Path) -> Path | None:
    """:func:`installation` for the package directory ``package``."""

    if package.parent.name == "src":
        return package.parent.parent
    site = package.parent
    if (site.name in SITE_DIRS and site.parent.name.startswith("python")
            and site.parent.parent.name in LIB_DIRS):
        return site.parent.parent.parent
    return None


def entry_point(name: str, tree: Path | str | None = None) -> Path | None:
    """``<installation>/bin/<name>`` (existence is the caller's question), or
    None without an installation tree. ``tree`` replaces the running
    installation (tests)."""

    base = installation() if tree is None else Path(tree)
    return None if base is None else base / "bin" / name


def document(name: str, tree: Path | str | None = None) -> Path | None:
    """The installed document ``name`` (``USAGE.md``, ``CHEATSHEET.md``…),
    or None. Never follows a resource override: documents belong to the
    installation, not to the resources a command selected."""

    base = installation() if tree is None else Path(tree)
    if base is None:
        return None
    for directory in DOCUMENT_DIRS:
        candidate = base / directory / name
        if candidate.is_file():
            return candidate
    return None


def installation_resources(path: Path | str) -> Path | None:
    """The resources of the installation ``path`` names: an installation
    prefix (:data:`INSTALLED_PACKAGE_GLOB`), then :data:`INSTALLED_RESOURCES`;
    the first candidate with ``catalog/`` and ``version.json``, or None."""

    root = Path(path)
    candidates = [package / "data" for package in sorted(root.glob(INSTALLED_PACKAGE_GLOB))]
    candidates += [root / relative for relative in INSTALLED_RESOURCES]
    for resources in candidates:
        if (resources / "catalog").is_dir() and (resources / "version.json").is_file():
            return resources
    return None


# ------------------------------------------------------------ source checkout


def checkout_resources(checkout: Path | str) -> Path:
    """The resources inside the source checkout ``checkout``."""

    return Path(checkout) / RESOURCES_IN_CHECKOUT


def verify_checkout(path: Path | str) -> Path:
    """Require an explicit, writable, real source checkout (never the Nix
    store) and return it resolved. Its resource directories must be real
    directories inside it."""

    candidate = Path(path)
    if candidate.is_symlink():
        raise LayoutError(f"repo path {candidate} must not be a symlink")
    resolved = candidate.resolve()
    if str(resolved) == NIX_STORE or str(resolved).startswith(NIX_STORE + "/"):
        raise LayoutError("the Nix store is never a writable source checkout")
    if not resolved.is_dir():
        raise LayoutError(f"repo path {candidate} is not a directory")
    for marker in CHECKOUT_MARKERS:
        if not (resolved / marker).is_file():
            raise LayoutError(f"{resolved} is not a source checkout: missing {marker}")
    catalog_dir = checkout_resources(resolved) / "catalog"
    if not catalog_dir.is_dir():
        raise LayoutError(f"{resolved} lacks the catalog tree ({RESOURCES_IN_CHECKOUT / 'catalog'})")
    _require_real_directories(resolved, RESOURCES_IN_CHECKOUT / "catalog")
    if not os.access(resolved, os.W_OK):
        raise LayoutError(f"repo path {resolved} is not writable")
    return resolved


def _require_real_directories(checkout: Path, relative: Path) -> None:
    current = checkout
    for part in relative.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise LayoutError(f"{current} is missing from the checkout ({exc.strerror or exc})") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise LayoutError(
                f"refusing to write through {current}: not a real directory inside the checkout {checkout}")


def checkout_destination(checkout: Path | str, path: Path | str) -> Path:
    """``path`` (checkout-relative, or absolute under ``checkout``) as a write
    destination, after proving the write stays inside the checkout: a plain
    path below it, every directory on the way a real directory (a symlinked
    ``src``, ``data`` or ``catalog`` cannot redirect it), and the file itself
    not a symlink."""

    base = Path(checkout)
    given = Path(path)
    if given.is_absolute():
        try:
            relative = given.relative_to(base)
        except ValueError:
            raise LayoutError(f"refusing to write {given}: outside the checkout {base}") from None
    else:
        relative = given
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise LayoutError(f"refusing to write {given}: not a plain path inside the checkout {base}")
    _require_real_directories(base, relative.parent)
    target = base / relative
    if target.is_symlink():
        raise LayoutError(f"refusing to replace symlinked repo file {target}")
    return target
