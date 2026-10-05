"""Test tiers: ``CM_TEST_TIER=fast`` skips the real-binary probes.

``CM_TEST_TIER`` unset or ``full`` is today's behaviour and the only done
gate. ``CM_TEST_TIER=fast`` is for iteration only (never a gate): every class
that execs the real pinned Claude binary (``test_scope_probe``'s
``RealPinnedBinaryTests`` and the ``test_client_*`` real-client modules) skips with a
``BOUNDARY: fast tier`` reason before it touches the contract or the binary.
Any other value is refused loudly so a typo never silently changes the tier.
"""

from __future__ import annotations

import os
from pathlib import Path

from claude_multi import probe

ENV = "CM_TEST_TIER"
TIERS = ("full", "fast")
FAST_BOUNDARY = (
    "BOUNDARY: fast tier (CM_TEST_TIER=fast skips real pinned-binary probes; "
    "iteration only, never a gate - run the full suite before done)"
)


def tier() -> str:
    value = os.environ.get(ENV, "").strip() or "full"
    if value not in TIERS:
        raise RuntimeError(
            f"{ENV}={value!r} is not a test tier; use 'full' (default) or 'fast'"
        )
    return value


def fast_tier_boundary() -> str | None:
    """The fast-tier BOUNDARY skip message, or None on the full tier."""

    return FAST_BOUNDARY if tier() == "fast" else None


def real_binary_gate(contract_path: Path | str, *, what: str) -> probe.PinnedBinary:
    """``probe.pinned_binary_gate`` behind the fast-tier skip (test side)."""

    boundary = fast_tier_boundary()
    if boundary is not None:
        return probe.PinnedBinary(None, None, boundary)
    return probe.pinned_binary_gate(contract_path, what=what)
