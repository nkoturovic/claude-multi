"""Resource selection, separate from the running package's version identity.

Precedence: an explicit resource root, then a nonempty
``CLAUDE_MULTI_ASSETS``, then the packaged default
(:func:`claude_multi.resources_root`). A selection changes the inputs a
command reads; the running release (its version, executables and documents)
stays the installed package's own (:mod:`claude_multi.layout`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

import claude_multi


def root(explicit_root: Path | str | None = None, *, environ: Mapping[str, str] | None = None) -> Path:
    """Explicit resources, then a nonempty environment override, then package data."""

    if explicit_root is not None:
        return Path(explicit_root)
    env = os.environ if environ is None else environ
    override = env.get("CLAUDE_MULTI_ASSETS")
    return Path(override) if override else claude_multi.resources_root()


def default_asset_root(explicit_root: Path | str | None = None, *, environ: Mapping[str, str] | None = None) -> Path:
    return root(explicit_root, environ=environ)
