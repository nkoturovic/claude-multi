"""How legacy compositions become profiles.

The single source of the migration map: ``claude-multi migrate`` reads
:func:`record_profile` for each record's ``profile``, and ``profile
migrate`` reads :data:`PRESET_TARGETS` and :data:`CONVERT_RULES` for
reviewed rows, and :func:`generic_row` and :func:`generic_rule` for every
composition without one (no second copy). The conversion algorithm of a
convert row is ``migrate_profiles``'.

No reviewed rows ship: the legacy ``default`` gives way to the default
profile and every other composition converts mechanically into a profile
of its own name. A name is never evidence of the same lineup: a
composition named like a shipped profile converts under a deterministic
name of its own (:func:`preserved_name`), and maps to the shipped profile
only when its converted lineup is that profile's (``migrate_profiles``
establishes it). Stdlib only; imports nothing from claude_multi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

SEED = "seed"
CONVERT = "convert"
DROPPED = "dropped"
KINDS = (SEED, CONVERT, DROPPED)

# profile_reason values of record_profile (the report's profile column).
REASON_ORDINARY = "ordinary"
REASON_DEFAULT = "default"
REASON_MISSING = "missing"
REASON_DROPPED = DROPPED

# The legacy default composition (the default profile replaces it).
SHIPPED_2X_DEFAULT = "default"

# name: (kind, target) — reviewed rows for named legacy compositions. None
# ship: every legacy composition follows the generic rule
# (:func:`generic_row`); a test or a maintainer supplies rows for a
# deliberate mapping.
PRESET_TARGETS: Mapping[str, tuple[str, str | None]] = MappingProxyType({})


PRESERVED_SUFFIX = "-legacy"


def preserved_name(name: str, taken: frozenset[str] | tuple[str, ...] | set[str]) -> str:
    """The deterministic profile name of a legacy composition whose own name
    a shipped profile has: ``<name>-legacy``, then ``<name>-legacy-2``, …,
    the first not in ``taken`` (the shipped names and every composition's)."""

    candidate = f"{name}{PRESERVED_SUFFIX}"
    number = 2
    while candidate in taken:
        candidate = f"{name}{PRESERVED_SUFFIX}-{number}"
        number += 1
    return candidate


def generic_row(name: str, seed_names: frozenset[str] | tuple[str, ...],
                taken: frozenset[str] | tuple[str, ...] | set[str] = ()) -> tuple[str, str | None]:
    """The outcome of a legacy composition with no reviewed row: a mechanical
    conversion into a profile of its own name, or, when a shipped profile
    has that name, under :func:`preserved_name` (never the shipped profile
    by its name alone; ``taken`` adds the other compositions' names). The
    legacy ``default`` gives way to the default profile before this is
    asked."""

    if name in seed_names:
        return CONVERT, preserved_name(name, frozenset(seed_names) | frozenset(taken))
    return CONVERT, name


def record_profile(composition_name: str | None) -> tuple[str | None, str]:
    """A migrated record's ``profile`` and the reason.

    ``None`` (an ordinary record) -> ``(None, "ordinary")``; the legacy
    ``default`` -> ``(None, "default")``; a name without a reviewed row ->
    ``(None, "missing")`` (the record keeps its own lineup, pinned); a
    dropped row -> ``(None, "dropped")``; a seed or convert row ->
    ``(target, kind)``.
    """

    if composition_name is None:
        return None, REASON_ORDINARY
    if composition_name == SHIPPED_2X_DEFAULT:
        return None, REASON_DEFAULT
    row = PRESET_TARGETS.get(composition_name)
    if row is None:
        return None, REASON_MISSING
    kind, target = row
    if kind == DROPPED:
        return None, REASON_DROPPED
    return target, kind


@dataclass(frozen=True)
class ConvertRule:
    """The fixed facts of one convert target (``migrate_profiles`` applies them)."""

    source: str  # the member whose name the row gives ("convert as X")
    members: tuple[str, ...]  # every preset mapped to this target (sorted)
    rule: str  # "mechanical" | "mechanical+overrides" | "explicit"
    lead: tuple[str, str] | None  # (key, effort) fixed by the row, else None (retired map)
    overrides: Mapping[str, tuple[str, str | None]]  # agent id -> (key, effort); None = the lane
    native: Mapping[str, str]  # native_agents fields fixed by the row ({} = from the preset)
    lead_providers: str  # "availability" | "lead-provider" (*-direct rows)
    # Profile finding codes (profile.Finding.code) the dry run must show:
    # never W-numbers or free text.
    expected_warnings: tuple[str, ...]
    primary_provider: str | None = None  # the provider the profile primarily uses
    # A free-text note kept for the report (informational only).
    note: str = field(default="")


RULE_MECHANICAL = "mechanical"
RULE_OVERRIDES = "mechanical+overrides"
RULE_EXPLICIT = "explicit"
RULES = (RULE_MECHANICAL, RULE_OVERRIDES, RULE_EXPLICIT)
LEAD_PROVIDERS_AVAILABILITY = "availability"
LEAD_PROVIDERS_LEAD_PROVIDER = "lead-provider"


def _frozen(mapping: Mapping) -> Mapping:
    return MappingProxyType(dict(mapping))


# target: the reviewed facts of a convert row of :data:`PRESET_TARGETS`
# (none ship; :func:`generic_rule` covers a composition without a row).
CONVERT_RULES: Mapping[str, ConvertRule] = MappingProxyType({})


def generic_rule(name: str, source: str | None = None) -> ConvertRule:
    """The mechanical conversion of the legacy composition ``source``
    (default ``name``) into the profile ``name``: its own lead and agents,
    the providers it was available on, nothing added."""

    member = name if source is None else source
    return ConvertRule(member, (member,), RULE_MECHANICAL, None, _frozen({}), _frozen({}),
                       LEAD_PROVIDERS_AVAILABILITY, ())


def convert_targets() -> frozenset[str]:
    """Every distinct convert target (== ``CONVERT_RULES`` keys)."""

    return frozenset(
        target for kind, target in PRESET_TARGETS.values() if kind == CONVERT and target
    )
