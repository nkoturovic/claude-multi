"""claude-multi: a durable multi-model launcher for Claude Code (Python
standard library only).

The version comes from ``data/version.json`` inside this package — the same
file the Nix package and the Python package metadata read — so every
``--version`` surface agrees by construction.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# The launcher version grammar of version.json and the session records.
_LAUNCHER_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-dev)?")


def resources_root() -> Path:
    """The packaged default resources: ``version.json``, ``catalog/``,
    ``schemas/`` and the other runtime data, inside this package (``data/``).

    The running release's identity always comes from here; a resource
    override (:mod:`claude_multi.assets`) selects other inputs but never
    redefines the release. New code resolves packaged resources through this
    function, never through its own ``__file__`` arithmetic.
    """

    return Path(__file__).resolve().parent / "data"


def _load_version() -> str:
    try:
        document = json.loads(
            (resources_root() / "version.json").read_text(encoding="utf-8")
        )
        return str(document["launcher_version"])
    except (OSError, ValueError, KeyError):
        return "0.0.0-unknown"


def python_version(launcher_version: str) -> str:
    """The Python package version (PEP 440) of a launcher version:
    ``X.Y.Z`` stays as it is and a development build ``X.Y.Z-dev`` becomes
    ``X.Y.Z.dev0``. Anything else raises ``ValueError``."""

    match = _LAUNCHER_VERSION.fullmatch(launcher_version)
    if match is None:
        raise ValueError(f"not a launcher version (X.Y.Z or X.Y.Z-dev): {launcher_version!r}")
    major, minor, patch, dev = match.groups()
    return f"{major}.{minor}.{patch}" + (".dev0" if dev else "")


__version__ = _load_version()
try:
    # The package metadata version (pyproject.toml reads it).
    PACKAGE_VERSION = python_version(__version__)
except ValueError:
    PACKAGE_VERSION = "0.0.0"
