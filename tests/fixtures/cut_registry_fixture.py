"""Recipe for the frozen pinned-registry fixture ``tests/fixtures/registry/``.

Usage (from the repository root)::

    python3 tests/fixtures/cut_registry_fixture.py \
        /nix/store/<hash>-source/internal/registry/models [OUT_DIR]

The source is the pinned CLIProxyAPI 7.3.15 embedded registry directory
(``${cliProxyApi.src}/internal/registry/models``, the path nix/module.nix
passes to nix/package.nix as ``cliProxyApiRegistry``). OUT_DIR defaults to
``tests/fixtures/registry``. The output is COMMITTED and FROZEN:
tests read it through ``CLAUDE_MULTI_REGISTRY_DIR`` and never the real
registry, and nothing regenerates it at test time. Re-run it only as a
deliberate fixture change.

What it keeps: the ``claude``, ``codex-free`` and ``codex-pro`` sections of
``models.json`` cut to the OAuth-pool wires of the frozen test catalog
(``tests/fixtures/assets``) — the tier difference stays visible
(``codex-free`` lacks ``gpt-5.6-sol``) — with the fields id, created,
display_name, context_length and max_completion_tokens; and the matching
``codex_client_models.json`` entries with slug, visibility, the upgrade
successor and retirement date, context_window, max_context_window and
minimal_client_version (``gpt-5.5`` carries the 2026-10-14 retirement).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tests/, for _layout
from _layout import FIXTURES_ROOT  # noqa: E402

FIXTURE_ASSETS = FIXTURES_ROOT / "assets"
SECTIONS = ("claude", "codex-free", "codex-pro")
MODEL_FIELDS = ("id", "created", "display_name", "context_length", "max_completion_tokens")
CODEX_FIELDS = (
    "slug",
    "visibility",
    "retirement_at",
    "context_window",
    "max_context_window",
    "minimal_client_version",
)


def fixture_oauth_wires() -> set[str]:
    models = json.loads((FIXTURE_ASSETS / "catalog" / "models.json").read_text())["models"]
    providers = json.loads((FIXTURE_ASSETS / "catalog" / "providers.json").read_text())[
        "providers"
    ]
    return {
        line["wire_model"]
        for line in models.values()
        if providers[line["provider"]]["transport"]["kind"] == "oauth-pool"
    }


def pretty(document: object) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def main(argv: list[str]) -> int:
    source = Path(argv[1])
    out = Path(argv[2]) if len(argv) > 2 else FIXTURES_ROOT / "registry"
    wires = fixture_oauth_wires()
    models = json.loads((source / "models.json").read_text())
    cut = {
        section: [
            {field: item[field] for field in MODEL_FIELDS if field in item}
            for item in models[section]
            if item["id"] in wires
        ]
        for section in SECTIONS
    }
    codex = json.loads((source / "codex_client_models.json").read_text())["models"]
    codex_cut = []
    for item in codex:
        if item["slug"] not in wires:
            continue
        entry = {field: item[field] for field in CODEX_FIELDS if field in item}
        upgrade = item.get("upgrade")
        entry["upgrade"] = (
            {"model": upgrade["model"], "retirement_at": upgrade.get("retirement_at")}
            if isinstance(upgrade, dict)
            else None
        )
        codex_cut.append(entry)
    out.mkdir(parents=True, exist_ok=True)
    (out / "models.json").write_bytes(pretty(cut))
    (out / "codex_client_models.json").write_bytes(pretty({"models": codex_cut}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
