"""claude-multi's indirect garbage-collector root in a real Nix store (CI).

  python3 .github/scripts/nix_roots.py PACKAGE

PACKAGE is a built claude-multi Nix package (a store path with
bin/claude-multi-proxy) that nothing roots yet, in a disposable Nix
installation. Under a temporary HOME, the product's own code selects it as
``claude-multi gateway service install`` does (``claude_multi.installs``:
plan_selection, then apply_selection, which runs ``nix-store --add-root
LINK --indirect --realise PACKAGE``), and then removes the link as the
service uninstall does (remove_nix_link). Checked with the store's own
answers (``nix-store --query --roots``, ``nix-store --delete``):

- before: PACKAGE has no root;
- after the selection: its roots are exactly the product's link, the
  selection says it registered the root, and deleting PACKAGE is refused
  because it is alive;
- after the removal: PACKAGE has no root again.

Exit 0 when all hold, else 1 naming the first that does not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class RootError(Exception):
    pass


def _nix_store(*args: str) -> subprocess.CompletedProcess[str]:
    tool = shutil.which("nix-store")
    if tool is None:
        raise RootError("nix-store is not on PATH")
    return subprocess.run([tool, *args], capture_output=True, text=True, check=False, timeout=300,
                          stdin=subprocess.DEVNULL)


def roots(package: str) -> list[str]:
    """The roots that keep ``package`` alive (their paths, without the
    ``{censored}`` and runtime entries of other users' processes)."""

    result = _nix_store("--query", "--roots", package)
    if result.returncode != 0:
        raise RootError(f"nix-store --query --roots failed: {result.stderr.strip()}")
    found = []
    for line in result.stdout.splitlines():
        root = line.split(" -> ", 1)[0].strip()
        if root and not root.startswith("{"):
            found.append(root)
    return found


def prove(package: str, runner=subprocess.run) -> list[str]:
    if str(REPO / "src") not in sys.path:
        sys.path.insert(0, str(REPO / "src"))
    from claude_multi import installs

    steps = []
    store = installs.store_path(package)
    if store is None or store != package.rstrip("/"):
        raise RootError(f"{package} is not a top-level Nix store path")
    if roots(store):
        raise RootError(f"{store} already has roots ({', '.join(roots(store))}); build it with --no-link")
    steps.append("unrooted before the selection")
    home = Path(tempfile.mkdtemp(prefix="claude-multi-gc-roots-")).resolve()
    try:
        environ = {"HOME": str(home), "PATH": os.environ.get("PATH", os.defpath)}
        selection = installs.plan_selection("nix", environ, installation=store)
        if selection.root != store:
            raise RootError(f"the selection names {selection.root}, not {store}")
        applied = installs.apply_selection(selection, environ, runner=runner)
        if not applied.gc_root:
            raise RootError(f"apply_selection did not register the root ({applied.note})")
        link = installs.nix_link(environ)
        found = roots(store)
        if found != [str(link)]:
            raise RootError(f"after the selection the roots are {found}, expected exactly {link}")
        steps.append(f"rooted by the product's link {link}")
        deleted = _nix_store("--delete", store)
        if deleted.returncode == 0:
            raise RootError(f"nix-store --delete removed {store} although the link roots it")
        if "alive" not in deleted.stderr:
            raise RootError(f"nix-store --delete refused for another reason: {deleted.stderr.strip()}")
        steps.append("deletion refused: still alive")
        if installs.remove_nix_link(environ) != store:
            raise RootError("remove_nix_link did not remove the link to the package")
        found = roots(store)
        if found:
            raise RootError(f"after the removal {store} is still rooted by {found}")
        steps.append("unrooted after the removal")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return steps


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: nix_roots.py PACKAGE", file=sys.stderr)
        return 2
    try:
        steps = prove(args[0])
    except RootError as exc:
        print(f"nix_roots: FAILED: {exc}", file=sys.stderr)
        return 1
    for step in steps:
        print(f"nix_roots: {step}: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
