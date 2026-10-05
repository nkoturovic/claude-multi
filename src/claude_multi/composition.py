"""The legacy v1 composition reader (``validate_document``,
``load_composition_file``) for ``profile migrate``, plus the context-policy
constants the compiler uses.

Only the reader remains: the legacy resolver, snapshot and default
projection are gone. Tests that still need legacy session snapshots build
them with ``tests/_v3.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import catalog as catalog_mod
from . import errors
from . import strict_json


class CompositionError(errors.ClaudeMultiError, ValueError):
    """Raised when a composition cannot be loaded or resolved."""


LEAD_ID = "cm-lead"
WORKFLOWS_VOCABULARY = ("native", "off")
AUTO_COMPACT_PERCENT = 90
AUTO_COMPACT_OUTPUT_RESERVE = 20_000
AUTO_COMPACT_REACTIVE_HEADROOM = 13_000
# The operating default: 1M-class routes are capped to an 800K operating
# window (reactive trigger 702K via auto_compact_trigger). This is a local
# operating policy, not a route-capability claim: catalog client/provider/
# declared/validated evidence fields stay untouched, so smaller route bounds
# still narrow the window further and future/custom models are covered.
OPERATING_WINDOW_CEILING = 800_000


def operating_window(window_tokens: int) -> int:
    """Clamp a derived operating window to the operating ceiling (idempotent)."""

    return min(window_tokens, OPERATING_WINDOW_CEILING)


def auto_compact_trigger(window_tokens: int) -> int:
    """Pinned-client reactive compact threshold for one process capacity.

    Claude Code 2.1.218 reserves up to 20K output tokens and applies the
    percentage to the remaining prompt budget. Proactive preparation is not
    returned here because its experiment-controlled fraction can vary at
    runtime; the reactive override is deterministic.
    """

    if window_tokens <= AUTO_COMPACT_OUTPUT_RESERVE + AUTO_COMPACT_REACTIVE_HEADROOM:
        raise CompositionError(
            "auto-compaction capacity is too small for the pinned client policy"
        )
    prompt_budget = window_tokens - AUTO_COMPACT_OUTPUT_RESERVE
    return min(
        prompt_budget * AUTO_COMPACT_PERCENT // 100,
        prompt_budget - AUTO_COMPACT_REACTIVE_HEADROOM,
    )


def validate_document(
    document: dict[str, Any], schema: dict[str, Any]
) -> dict[str, Any]:
    """Schema + version validation shared by file and stdin ingestion."""

    from . import validate as schema_validate

    problems = schema_validate.validate(document, schema, "$")
    if problems:
        raise CompositionError("; ".join(problems))
    if document.get("version") != catalog_mod.SUPPORTED_DATA_VERSION:
        raise CompositionError(
            f"unsupported composition version {document.get('version')!r}"
        )
    return document


def load_composition_file(
    path: Path | str, schema: dict[str, Any]
) -> dict[str, Any]:
    """Strictly load and schema-validate a composition document."""

    return validate_document(strict_json.load(path), schema)
