"""The release identity ``claude-multi --version`` prints.

    claude-multi 1.0.0 (catalog 37; CLIProxyAPI 7.3.15 + 19 patches; Claude Code 2.1.286, manifest 0123abcd)

Everything comes from the packaged resources (the running release's own
identity, never a resource override): ``version.json``, the generated
``gateway-contract.json`` and the native contract's pin with the digest of
the signed manifest it was verified against. A resource that cannot be read
is named ``unavailable`` rather than left out or failing ``--version``; nothing
is downloaded, started or contacted. Standard library only and cheap: it
reads three small files.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

UNAVAILABLE = "unavailable"
_HEX = re.compile(r"^[0-9a-f]{64}$")


def _load(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Identity:
    """The typed parts of the release identity; None is unavailable."""

    launcher: str
    catalog: int | None
    gateway_upstream: str | None
    gateway_patches: int | None
    claude_code: str | None
    claude_manifest: str | None  # the first 8 hex digits of the signed manifest's sha256

    def document(self) -> dict[str, object]:
        """The allowlisted fields, for a structured report."""

        return {
            "launcher": self.launcher,
            "catalog": self.catalog,
            "gateway": None if self.gateway_upstream is None else {
                "upstream": self.gateway_upstream, "patches": self.gateway_patches},
            "claude_code": None if self.claude_code is None else {
                "version": self.claude_code, "manifest": self.claude_manifest},
        }


def collect() -> Identity:
    """Read the packaged identity (never raises; unreadable parts are None)."""

    from claude_multi import __version__, resources_root

    data = resources_root()
    version = _load(data / "version.json")
    catalog = version.get("catalog_version") if isinstance(version, dict) else None
    catalog = catalog if isinstance(catalog, int) and not isinstance(catalog, bool) else None
    contract = _load(data / "gateway-contract.json")
    upstream = patches = None
    if isinstance(contract, dict) and isinstance(contract.get("upstream_version"), str):
        upstream = contract["upstream_version"]
        listed = contract.get("patches")
        patches = len(listed) if isinstance(listed, list) else None
    native = _load(data / "catalog" / "native-contract.json")
    verified = native.get("verified") if isinstance(native, dict) else None
    client = manifest = None
    if isinstance(verified, list) and verified and isinstance(verified[0], dict) \
            and isinstance(verified[0].get("version"), str):
        client = verified[0]["version"]
        digest = verified[0].get("manifest_sha256")
        manifest = digest[:8] if isinstance(digest, str) and _HEX.fullmatch(digest) else None
    return Identity(__version__, catalog, upstream, patches, client, manifest)


def version_line(name: str = "claude-multi") -> str:
    found = collect()
    parts = [f"catalog {found.catalog}" if found.catalog is not None else f"catalog {UNAVAILABLE}"]
    if found.gateway_upstream is None:
        parts.append(f"CLIProxyAPI {UNAVAILABLE}")
    elif found.gateway_patches is None:
        parts.append(f"CLIProxyAPI {found.gateway_upstream} + patches {UNAVAILABLE}")
    else:
        count = found.gateway_patches
        parts.append(f"CLIProxyAPI {found.gateway_upstream} + {count} patch{'es' if count != 1 else ''}")
    if found.claude_code is None:
        parts.append(f"Claude Code {UNAVAILABLE}")
    else:
        manifest = found.claude_manifest or UNAVAILABLE
        parts.append(f"Claude Code {found.claude_code}, manifest {manifest}")
    return f"{name} {found.launcher} ({'; '.join(parts)})"
