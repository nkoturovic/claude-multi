"""``claude-multi profile migrate [--dry-run | --apply]``.

Converts legacy compositions (``<config root>/compositions/*.json``, v1
documents over catalog-32 keys) into profiles by the migration table:
:data:`migration_map.PRESET_TARGETS` gives each preset's outcome and
:data:`migration_map.CONVERT_RULES` the facts of each convert target (one
copy only). A bare run and ``--dry-run`` report only; ``--apply`` writes
through :class:`profile.ProfileStore` (``new`` per target, ``install_seeds``
for the absent seeds) and nothing else, and refuses while any outcome is
blocking. Compositions are never opened for writing.

Import rule: ``catalog``, ``composition``, ``migration_map``,
``migrate`` (``translate_key``, ``_lead_providers``,
``default_settings_snapshot``, ``MigrationError`` only), ``profile``,
``sessions``, ``state``, ``strict_json``; never ``cli``, ``tui``, ``views``,
``launch``, ``scope``, ``compiler``, ``hooks``, ``transition`` or ``lineup``.
Pure except :func:`read_presets`/:func:`plan` (reads) and :func:`apply_plan`
(writes). The tables are read at call time (cf-7), so a test that patches
``migration_map.PRESET_TARGETS``/``CONVERT_RULES`` or
:data:`V1_DEFAULT_LANES` reaches the CLI path.
"""

from __future__ import annotations

import dataclasses
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence, TextIO

from . import catalog, composition, migrate, migration_map, profile, sessions, state, strict_json

# Every legacy composition is a catalog-32 document: the keys it names
# are translated as of catalog 32 (``migrate.translate_key``).
COMPOSITION_CATALOG_VERSION = 32

# An omitted legacy lane is the frozen catalog-32 ``default_lane`` of the
# key, never the live line's default (data-equal to
# tests/fixtures/catalog32-models.json).
V1_DEFAULT_LANES: Mapping[str, str] = MappingProxyType(
    {
        "astra": "high",
        "deepseek-flash": "high",
        "deepseek-pro": "high",
        "fable": "max",
        "fable51": "max",
        "glm52": "max",
        "gpt55": "high",
        "grok46": "xhigh",
        "kimi-k3": "max",
        "muse-spark": "xhigh",
        "muse-spark-contributor": "high",
        "opus": "xhigh",
        "opus5": "xhigh",
        "opus55": "xhigh",
        "qwen-flash-next": "high",
        "qwen38": "max",
        "sol": "high",
    }
)

LEGACY_AGENT_ROLES = ("cm-analyst", "cm-implementer", "cm-reviewer")  # = migrate.MECHANICAL_ROLES
# The legacy role whose slots back a current agent id (override efforts, not-carried
# matching). cm-designer has no legacy role: an override for it takes the line's
# default_effort.
FUNCTION_ROLE: Mapping[str, str] = MappingProxyType(
    {
        "cm-explorer": "cm-analyst",
        "cm-analyst": "cm-analyst",
        "cm-analyst-strong": "cm-analyst",
        "cm-implementer-light": "cm-implementer",
        "cm-implementer": "cm-implementer",
        "cm-implementer-strong": "cm-implementer",
        "cm-reviewer": "cm-reviewer",
        "cm-reviewer-strong": "cm-reviewer",
    }
)

OUTCOME_KINDS = ("seed-map", "convert", "merge-into", "drop", "unmapped")
TARGET_STATUSES = ("write", "written", "already-migrated", "conflict", "failed", "skipped")
BLOCKING_STATUSES = ("conflict", "failed")
NATIVE_FIELDS = ("explore", "plan", "general_purpose")
OUTCOME_WIDTH = 10

DRY_RUN_FOOTER = "dry run: nothing was changed; rerun with --apply to write"
NOTHING_TO_DO_FOOTER = "nothing to do: every target is already migrated"


class ProfileMigrationError(migrate.MigrationError):
    """``profile migrate`` refused or failed (``main`` prints it; exit 2)."""


class ProfileMigrationStopped(ProfileMigrationError):
    """``--apply`` stopped part-way on an unexpected error; ``report`` is the partial report.

    ``run_command`` prints the partial report, then lets this propagate
    (§4.7: a rerun converges, since every written target compares equal).
    """

    def __init__(self, report: "ProfileMigrationReport", cause: BaseException):
        self.report = report
        self.cause = cause
        written = sum(1 for item in report.targets if item.status == "written")
        super().__init__(
            f"profile migrate stopped after writing {written} profile(s) and "
            f"{len(report.seeds_installed)} seed(s): {cause}; nothing else was changed; "
            "rerun it to converge"
        )


# ------------------------------------------------------------------ types


@dataclass(frozen=True)
class Preset:
    """One ``compositions/*.json`` entry: its document, or why it is unreadable."""

    name: str
    document: dict | None
    error: str | None


@dataclass(frozen=True)
class PresetOutcome:
    name: str
    kind: str  # OUTCOME_KINDS
    target: str | None
    source: str | None  # the convert rule's source (convert / merge-into)
    reason: str  # the report text after the outcome column (drop/unmapped: inside the parentheses)
    blocking: bool  # unmapped only
    error: str | None = None  # the unreadable text, when the file could not be read


@dataclass(frozen=True)
class TargetOutcome:
    target: str
    rule: migration_map.ConvertRule
    source: str
    members: tuple[str, ...]  # rule members present on disk
    status: str  # TARGET_STATUSES
    document: dict | None  # the would-be profile (None when never built)
    warnings: tuple[profile.Finding, ...] = ()
    notices: tuple[profile.Finding, ...] = ()
    expected: tuple[str, ...] = ()  # ConvertRule.expected_warnings (profile finding codes)
    notes: tuple[str, ...] = ()
    not_carried: tuple[str, ...] = ()
    differs: tuple[str, ...] = ()  # one "differs (member): …" text per readable non-source member
    errors: tuple[str, ...] = ()  # failed / conflict


@dataclass(frozen=True)
class ProfileMigrationReport:
    config_root: str
    dry_run: bool
    catalog_version: int
    launcher_version: str
    marker: str  # "absent (state 3)" | "4" | "newer (…): --apply refuses"
    compositions_present: bool
    presets: tuple[PresetOutcome, ...]
    targets: tuple[TargetOutcome, ...]
    seeds: tuple[str, ...]  # every seed, SEED_PROFILE_NAMES order
    seeds_absent: tuple[str, ...]
    seeds_installed: tuple[str, ...]
    user_profiles_before: int
    blocking: tuple[str, ...]
    applied: bool = False  # --apply ran its writes (False: dry run or refused)
    stopped: bool = False  # --apply was interrupted (the partial report of ProfileMigrationStopped)


# ------------------------------------------------------------ reading presets


def read_presets(compositions_dir: Path | str, schema: Mapping[str, Any]) -> list[Preset] | None:
    """Every ``*.json`` entry of ``compositions_dir``, sorted; ``None`` when it is absent.

    The directory must pass the stores' read checks (``state._check_directory``:
    no symlink, owner-controlled, no group/other bits), else PM-4. Each entry
    is reported, never skipped: an unsafe stem, a symlink or non-regular
    file, and every load error (strict JSON, schema, version, name ≠ stem)
    become an unreadable :class:`Preset` (§4.3). Reads only.
    """

    directory = Path(compositions_dir)
    if not os.path.lexists(directory):
        return None
    try:
        state._check_directory(directory)
        entries = sorted(directory.glob("*.json"))
    except OSError as exc:
        raise ProfileMigrationError(f"cannot read compositions: {exc}") from exc
    presets: list[Preset] = []
    for path in entries:
        name = path.stem
        try:
            state.check_name(name)
        except state.StateError:
            presets.append(Preset(name, None, "unsafe file name"))
            continue
        try:
            regular = not path.is_symlink() and path.is_file()
        except OSError:
            regular = False
        if not regular:
            presets.append(Preset(name, None, "not a regular file"))
            continue
        try:
            document = composition.load_composition_file(path, dict(schema))
        except (
            strict_json.StrictJSONError,
            composition.CompositionError,
            OSError,
            ValueError,
            RecursionError,
        ) as exc:
            presets.append(Preset(name, None, str(exc) or type(exc).__name__))
            continue
        if document.get("name") != name:
            presets.append(
                Preset(name, None, f"name {document.get('name')!r} differs from the file stem")
            )
            continue
        presets.append(Preset(name, document, None))
    return presets


# ------------------------------------------------------------- conversion


@dataclass(frozen=True)
class _SlotFact:
    index: int  # position in the file's slots
    role: str
    key: str  # the legacy key as written
    lane: str | None
    preferred: bool
    live: str | None  # the live line key, None = removed / unknown
    family: str | None


def _preference_order(facts: Sequence[_SlotFact]) -> list[_SlotFact]:
    return [f for f in facts if f.preferred] + [f for f in facts if not f.preferred]


def _binding_text(binding: Mapping[str, Any] | None) -> str:
    if binding is None:
        return "unbound"
    return f"{binding['model']}·{binding['effort']}"


def _providers_text(value: Sequence[str] | None) -> str:
    return "omitted" if value is None else f"[{', '.join(value)}]"


class _Build:
    """Steps 1-9 of §4.5 over one preset document (the source, or a member for step 12)."""

    def __init__(
        self,
        document: Mapping[str, Any],
        rule: migration_map.ConvertRule,
        lcat: profile.LineupCatalog,
        lanes: Mapping[str, str],
        snapshot: Mapping[str, Any],
    ):
        self.doc = document
        self.rule = rule
        self.lcat = lcat
        self.lanes = lanes
        self.snapshot = snapshot
        self.notes: list[str] = []
        self.errors: list[str] = []
        self._efforts: dict[int, str | None] = {}
        self.lead_fact: _SlotFact | None = None
        self.agent_facts: list[_SlotFact] = []
        self.lead: dict[str, str] | None = None
        self.agents: dict[str, dict[str, str]] = {}
        self.native: dict[str, str] = {}
        self.lead_providers: list[str] | None = None
        self.fields: dict[str, Any] = {}
        self._slots()
        self._build()

    # step 1
    def _slots(self) -> None:
        for index, slot in enumerate(self.doc["slots"]):
            role, key = slot["role"], slot["model"]
            if role != composition.LEAD_ID and role not in LEGACY_AGENT_ROLES:
                self.notes.append(f"slot {role} {key}: not a legacy agent role; ignored")
                continue
            if role == composition.LEAD_ID and self.lead_fact is not None:
                self.notes.append(f"slot {role} {key}: a second cm-lead slot; ignored")
                continue
            label = profile.label(role)
            live, notice = _live_key(key, self.lcat, self.errors, label)
            fact = _SlotFact(
                index=index,
                role=role,
                key=key,
                lane=slot.get("lane"),
                preferred=bool(slot.get("preferred", False)),
                live=live,
                family=(
                    catalog.line_family(self.lcat.lines[live], self.lcat.providers)
                    if live is not None
                    else None
                ),
            )
            if live is not None and notice is not None:
                self.notes.append(f"{label}: {key} → {live} ({notice})")
            if role == composition.LEAD_ID:
                self.lead_fact = fact
            else:
                self.agent_facts.append(fact)

    def effort_value(self, fact: _SlotFact) -> str | None:
        """The slot's converted effort without recording anything (None: no lane at all)."""

        lane = fact.lane or self.lanes.get(fact.key)
        if lane is None or fact.live is None:
            return None
        entry = self.lcat.lines[fact.live]
        return lane if lane in profile.declared_efforts(entry) else entry["default_effort"]

    # step 2
    def effort(self, fact: _SlotFact) -> str | None:
        """The effort of a selected slot, recording its note or error once."""

        if fact.index in self._efforts:
            return self._efforts[fact.index]
        label = profile.label(fact.role)
        lane = fact.lane or self.lanes.get(fact.key)
        value: str | None
        if lane is None:
            self.errors.append(f"{label} {fact.key}: no lane and no frozen legacy default lane")
            value = None
        else:
            entry = self.lcat.lines[fact.live]
            declared = profile.declared_efforts(entry)
            if lane in declared:
                value = lane
            else:
                value = entry["default_effort"]
                self.notes.append(
                    f"{label}: lane {lane} is not offered by {fact.live} "
                    f"(declared: {', '.join(declared)}) → {value}"
                )
        self._efforts[fact.index] = value
        return value

    def _build(self) -> None:
        rule = self.rule
        # step 3: the lead
        if rule.lead is not None:
            self.lead = {"model": rule.lead[0], "effort": rule.lead[1]}
        elif self.lead_fact is None:
            self.errors.append("no cm-lead slot; a convert row needs a live lead")
        elif self.lead_fact.live is None:
            self.errors.append(
                f"lead {self.lead_fact.key} was removed; a convert row needs a live lead"
            )
        else:
            block = self.lcat.lines[self.lead_fact.live].get("lead") or {}
            self.lead = {
                "model": self.lead_fact.live,
                "effort": block.get("effort", profile.ULTRACODE),
            }
        lead_line = None if self.lead is None else self.lcat.lines.get(self.lead["model"])
        lead_family = (
            catalog.line_family(lead_line, self.lcat.providers) if lead_line is not None else None
        )
        # step 4: the mechanical agents (the record rule, migrate._mechanical_agents)
        agents: dict[str, dict[str, str]] = {}
        if rule.rule != migration_map.RULE_EXPLICIT:
            picked: dict[str, _SlotFact] = {}
            for role in LEGACY_AGENT_ROLES:
                ordered = _preference_order([f for f in self.agent_facts if f.role == role])
                if not ordered:
                    continue
                label = profile.label(role)
                first = ordered[0]
                choice = next((f for f in ordered if f.live is not None), None)
                if choice is None:
                    self.notes.append(f"{label}: {first.key} was removed — unbound")
                    continue
                value = self.effort(choice)
                if value is None:
                    continue
                agents[role] = {"model": choice.live, "effort": value}
                picked[role] = choice
                if choice is not first:
                    self.notes.append(
                        f"{label}: preferred {first.key} removed → {choice.live}·{value}"
                    )
            reviewer_family = (
                picked["cm-reviewer"].family if "cm-reviewer" in picked else lead_family
            )
            for fact in self.agent_facts:
                if fact.role != "cm-reviewer" or fact is picked.get("cm-reviewer"):
                    continue
                if fact.live is None or fact.family == reviewer_family:
                    continue
                value = self.effort(fact)
                if value is not None:
                    agents["cm-reviewer-strong"] = {"model": fact.live, "effort": value}
                break
        # step 5: the row's overrides
        for rid, (key, fixed) in rule.overrides.items():
            label = profile.label(rid)
            if key not in self.lcat.lines:
                self.errors.append(
                    f"{label}: row override {key} is not a line in catalog "
                    f"{self.lcat.catalog_version}"
                )
                continue
            value = fixed
            if value is None:
                function = FUNCTION_ROLE.get(rid)
                candidates = [
                    f
                    for f in _preference_order(self.agent_facts)
                    if function is not None and f.role == function and f.live == key
                ]
                if candidates:
                    value = self.effort(candidates[0])
                else:
                    value = self.lcat.lines[key]["default_effort"]
                    where = profile.label(function) if function is not None else "matching"
                    self.notes.append(
                        f"{label}: no legacy {where} slot on {key} → {value} (the line's default)"
                    )
            if value is None:
                continue
            agents[rid] = {"model": key, "effort": value}
            self.notes.append(f"{label}: row override {key}·{value}")
        # step 6: native agents
        native = dict(self.doc["native_agents"])
        native.update(rule.native)
        # step 7: the explorer
        if native.get("explore") == "replace" and "cm-explorer" not in rule.overrides:
            if "cm-analyst" in agents:
                agents["cm-explorer"] = dict(agents["cm-analyst"])
            else:
                native["explore"] = "native"
                self.notes.append("Explore: replace → native (no analyst left to back cm-explorer)")
        self.agents = {rid: agents[rid] for rid in profile.AGENT_ROLE_IDS if rid in agents}
        self.agents.update({rid: agents[rid] for rid in agents if rid not in self.agents})
        self.native = native
        # step 8: lead_providers
        lead_provider = None if lead_line is None else lead_line.get("provider")
        if rule.lead_providers == migration_map.LEAD_PROVIDERS_LEAD_PROVIDER:
            self.lead_providers = None if lead_provider is None else [lead_provider]
        else:
            self.lead_providers = migrate._lead_providers(
                self.lcat,
                {"availability": self.doc.get("availability", {})},
                lead_provider,
                self.snapshot,
                self.notes,
            )
        # step 9: the other fields
        fields: dict[str, Any] = {}
        if self.doc.get("description"):
            fields["description"] = self.doc["description"]
        if "workflows" in self.doc:
            fields["workflows"] = self.doc["workflows"]
        if rule.primary_provider is not None:
            fields["primary_provider"] = rule.primary_provider
        seed = self.doc.get("seed")
        if isinstance(seed, Mapping):
            self.notes.append(
                f"seed {seed.get('id')} v{seed.get('version')}: a legacy seed marker; not carried"
            )
        self.fields = fields

    def document(self, target: str) -> dict[str, Any] | None:
        if self.lead is None:
            return None
        document: dict[str, Any] = {
            "version": profile.PROFILE_DATA_VERSION,
            "name": target,
            "lead": dict(self.lead),
            "agents": {rid: dict(binding) for rid, binding in self.agents.items()},
            "native_agents": dict(self.native),
        }
        if self.lead_providers is not None:
            document["lead_providers"] = list(self.lead_providers)
        document.update(self.fields)
        return document


def _live_key(
    key: str, lcat: profile.LineupCatalog, errors: list[str], label: str
) -> tuple[str | None, str | None]:
    """``translate_key(k, None, 32)`` → ``resolve_key``; ``(live, notice)``.

    A key unknown to the catalog, or whose chain ends in null, is removed
    (``live`` None). An ambiguous catalog (two ``K@…`` entries) is an error.
    """

    try:
        translated = migrate.translate_key(key, None, COMPOSITION_CATALOG_VERSION, lcat)
    except migrate.MigrationError as exc:
        errors.append(f"{label} {key}: {exc}")
        return None, None
    try:
        resolution = lcat.resolve_key(translated)
    except catalog.CatalogError:
        return None, None
    if resolution.key is None:
        return None, None
    return resolution.key, resolution.notice


def _not_carried(member: str, build: _Build, agents: Mapping[str, Mapping[str, str]]) -> list[str]:
    """Step 11 for one member: its agent slots no bound id of the same function carries."""

    lines = []
    for fact in build.agent_facts:
        value = build.effort_value(fact)
        carried = fact.live is not None and value is not None and any(
            FUNCTION_ROLE.get(rid) == fact.role
            and binding["model"] == fact.live
            and binding["effort"] == value
            for rid, binding in agents.items()
        )
        if carried:
            continue
        text = f"{member}: {profile.label(fact.role)} {fact.key}@{fact.lane or 'default'}"
        if fact.live is None:
            text += " (removed)"
        lines.append(text)
    models = build.doc.get("availability", {}).get("models", {})
    if models:
        lines.append(f"{member}: availability.models ({len(models)} entries)")
    return lines


def _differs(member: str, build: _Build, source: _Build) -> str:
    """Step 12: a readable non-source member compared field by field with the source."""

    if build.errors:
        return f"differs ({member}): not convertible: {'; '.join(build.errors)}"
    items = []
    if build.lead != source.lead:
        items.append(f"lead {_binding_text(build.lead)} (source {_binding_text(source.lead)})")
    ids = [rid for rid in profile.AGENT_ROLE_IDS if rid in build.agents or rid in source.agents]
    for rid in ids:
        mine, theirs = build.agents.get(rid), source.agents.get(rid)
        if mine != theirs:
            items.append(
                f"{profile.label(rid)} {_binding_text(mine)} (source {_binding_text(theirs)})"
            )
    for name in NATIVE_FIELDS:
        mine, theirs = build.native.get(name), source.native.get(name)
        if mine != theirs:
            items.append(f"native {name} {mine} (source {theirs})")
    mine_w = build.fields.get("workflows", "native")
    theirs_w = source.fields.get("workflows", "native")
    if mine_w != theirs_w:
        items.append(f"workflows {mine_w} (source {theirs_w})")
    if build.lead_providers != source.lead_providers:
        items.append(
            f"lead_providers {_providers_text(build.lead_providers)} "
            f"(source {_providers_text(source.lead_providers)})"
        )
    if build.fields.get("description", "") != source.fields.get("description", ""):
        items.append("description differs")
    return f"differs ({member}): {'; '.join(items) if items else 'none'}"


def convert_target(
    target: str,
    rule: migration_map.ConvertRule,
    presets: Mapping[str, Preset],
    *,
    lcat: profile.LineupCatalog,
    default_lanes: Mapping[str, str] | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> TargetOutcome:
    """§4.5: the would-be profile of one convert target (pure; status write/failed/skipped)."""

    lanes = V1_DEFAULT_LANES if default_lanes is None else default_lanes
    snap = migrate.default_settings_snapshot(lcat) if snapshot is None else snapshot
    members = tuple(name for name in rule.members if name in presets)
    base = TargetOutcome(
        target=target,
        rule=rule,
        source=rule.source,
        members=members,
        status="skipped",
        document=None,
        expected=tuple(rule.expected_warnings),
    )
    # step 0: the named source: no fallback.
    if not members:
        return base
    source = presets.get(rule.source)
    if source is None:
        return dataclasses.replace(
            base,
            status="failed",
            errors=(
                f"source {rule.source} is not on disk; the table names it as the "
                "source; restore the file or change the table",
            ),
        )
    if source.document is None:
        return dataclasses.replace(
            base,
            status="failed",
            errors=(f"source {rule.source} is unreadable: {source.error}",),
        )
    build = _Build(source.document, rule, lcat, lanes, snap)
    document = build.document(target)
    notes = list(build.notes)
    errors = list(build.errors)
    # step 11/12: the members (source first), without evaluating them.
    agents = build.agents
    not_carried = _not_carried(rule.source, build, agents)
    differs = []
    for member in members:
        if member == rule.source:
            continue
        preset = presets[member]
        if preset.document is None:
            continue
        other = _Build(preset.document, rule, lcat, lanes, snap)
        not_carried += _not_carried(member, other, agents)
        differs.append(_differs(member, other, build))
    result = dataclasses.replace(
        base,
        document=document,
        notes=tuple(notes),
        not_carried=tuple(not_carried),
        differs=tuple(differs),
    )
    if errors or document is None:
        return dataclasses.replace(result, status="failed", errors=tuple(errors))
    # Evaluate with the merged catalog, no bindings, no Settings.
    evaluation = profile.evaluate(document, lcat, bindings=None, effective=None)
    if evaluation.errors or evaluation.lineup is None:
        return dataclasses.replace(result, status="failed", errors=tuple(evaluation.errors))
    lineup = evaluation.lineup
    produced = {f.code for f in lineup.warnings} | {f.code for f in lineup.notices}
    for code in rule.expected_warnings:
        if code not in produced:
            notes.append(f"expected {code} not produced")
    return dataclasses.replace(
        result,
        status="write",
        warnings=tuple(lineup.warnings),
        notices=tuple(lineup.notices),
        notes=tuple(notes),
    )


# ------------------------------------------------------------------- plan


def _marker_text(state_root: Path | str) -> str:
    try:
        version = sessions.check_state_marker(Path(state_root))
    except sessions.StateMarkerError as exc:
        label = str(exc.version) if exc.version is not None else "unreadable"
        return f"newer (state version {label}): --apply refuses"
    if version == sessions.UNMARKED_STATE_VERSION:
        return f"absent (state {version})"
    return str(version)


def _preset_outcome(
    preset: Preset,
    *,
    preset_targets: Mapping[str, tuple[str, str | None]],
    convert_rules: Mapping[str, migration_map.ConvertRule],
    store: profile.ProfileStore,
    lcat: profile.LineupCatalog,
) -> PresetOutcome:
    name = preset.name
    unreadable = (
        None if preset.error is None else f"unreadable: {preset.error}; outcome by its name"
    )
    suffix = "" if unreadable is None else f" ({unreadable})"
    lead_key = lead_live = None
    if preset.document is not None:
        lead_slot = next(
            (s for s in preset.document["slots"] if s["role"] == composition.LEAD_ID), None
        )
        if lead_slot is not None:
            lead_key = lead_slot["model"]
            lead_live, _notice = _live_key(lead_key, lcat, [], "lead")
    if name == migration_map.SHIPPED_2X_DEFAULT:
        reason = unreadable or (
            f"legacy default composition; the default is the {catalog.DEFAULT_SEED} seed"
        )
        return PresetOutcome(name, "drop", None, None, reason, False, preset.error)
    row = preset_targets.get(name)
    if row is None:
        row = migration_map.generic_row(name, catalog.SEED_PROFILE_NAMES)
    kind, target = row
    if kind == migration_map.SEED:
        reason = f"→ {target}"
        if lead_key is not None:
            if lead_live is None:
                reason += f" (lead {lead_key} removed)"
            else:
                seed_lead = _seed_lead(store, target)
                if seed_lead is not None and seed_lead != lead_live:
                    reason += f" (lead {lead_live} → {seed_lead})"
        return PresetOutcome(name, "seed-map", target, None, reason + suffix, False, preset.error)
    if kind == migration_map.DROPPED:
        if unreadable is not None:
            reason = unreadable
        elif lead_key is None:
            reason = "judgment row; records → profile null"
        elif lead_live is None:
            reason = f"lead {lead_key} removed (no successor)"
        else:
            reason = (
                f"judgment row (lead {lead_key} → {lead_live} survives); "
                "records → profile null"
            )
        return PresetOutcome(name, "drop", None, None, reason, False, preset.error)
    rule = convert_rules.get(target) if target is not None else None
    if rule is None:
        return PresetOutcome(
            name, "unmapped", target, None,
            f"convert target {target} has no convert rule; --apply refuses" + suffix,
            True, preset.error,
        )
    if name == rule.source:
        kept = (f" (named like the shipped profile {name}, which keeps its own lineup)"
                if target != name and name in catalog.SEED_PROFILE_NAMES else "")
        return PresetOutcome(
            name, "convert", target, rule.source, f"→ {target}{kept}" + suffix, False, preset.error
        )
    return PresetOutcome(
        name, "merge-into", target, rule.source,
        f"→ {target} (source {rule.source})" + suffix, False, preset.error,
    )


_LINEUP_FIELDS_IGNORED = ("name", "description", "seed", "version")


def _same_lineup(document: Mapping[str, Any] | None, store: profile.ProfileStore, seed: str) -> bool:
    """Whether a converted profile is the lineup (every field but its name,
    description and seed marker) of both the shipped profile ``seed`` and
    the profile that name selects here: the only equivalence that lets a
    composition named like it map to it. A user's own profile under that
    name that differs, or cannot be read, keeps the conversion, so mapping
    never swaps the lineup a person chose for the converted one."""

    if document is None:
        return False
    try:
        # The shipped document itself, never a user copy that overrides it,
        shipped = profile.parse(store.seed_document(seed))
        # and the effective target: the user's override when there is one.
        effective = store.load(seed)
        converted = profile.parse(dict(document))
    except (profile.ProfileError, KeyError, TypeError, ValueError, OSError):
        return False

    def lineup(item: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in item.items() if key not in _LINEUP_FIELDS_IGNORED}

    return lineup(converted) == lineup(shipped) == lineup(effective)


def _seed_lead(store: profile.ProfileStore, seed: str | None) -> str | None:
    if seed is None:
        return None
    try:
        document = store.load(seed)
    except profile.ProfileError:
        return None
    lead = document.get("lead")
    return lead.get("model") if isinstance(lead, Mapping) else None


def _compare_existing(outcome: TargetOutcome, store: profile.ProfileStore) -> TargetOutcome:
    """§4.7 step 3 (read-only): a seed name, an equal file, a different or unreadable one."""

    target = outcome.target
    if store.is_seed(target):
        return dataclasses.replace(
            outcome, status="conflict", errors=(f"{target} is a seed name",)
        )
    if not store.has_user(target):
        return outcome
    try:
        existing = store.load(target)
    except profile.ProfileError as exc:
        return dataclasses.replace(
            outcome,
            status="conflict",
            errors=(f"existing profile {target} is unreadable: {exc}",),
        )
    if existing == profile.parse(outcome.document):
        return dataclasses.replace(outcome, status="already-migrated", errors=())
    return dataclasses.replace(
        outcome,
        status="conflict",
        errors=(
            f"profile {target} exists and differs from the migration result; compare with "
            f"claude-multi profile show {target}; to re-migrate, rename it "
            f"(claude-multi profile rename {target} {target}-2x-old) and rerun",
        ),
    )


def _blocking(
    presets: Sequence[PresetOutcome], targets: Sequence[TargetOutcome], marker: str, dry_run: bool
) -> tuple[str, ...]:
    lines = [f"preset {item.name}: unmapped" for item in presets if item.blocking]
    lines += [
        f"target {item.target}: {item.status}"
        for item in targets
        if item.status in BLOCKING_STATUSES
    ]
    if not dry_run and marker.startswith("newer"):
        lines.append(f"state marker {marker}")
    return tuple(lines)


def plan(
    *,
    compositions_dir: Path | str,
    schema: Mapping[str, Any],
    store: profile.ProfileStore,
    lcat: profile.LineupCatalog,
    version_doc: Mapping[str, Any],
    state_root: Path | str,
    dry_run: bool,
    preset_targets: Mapping[str, tuple[str, str | None]] | None = None,
    convert_rules: Mapping[str, migration_map.ConvertRule] | None = None,
    default_lanes: Mapping[str, str] | None = None,
) -> ProfileMigrationReport:
    """§4.7: the whole report; reads only (no directory, lock or file is created)."""

    targets_table = migration_map.PRESET_TARGETS if preset_targets is None else preset_targets
    rules = dict(migration_map.CONVERT_RULES if convert_rules is None else convert_rules)
    lanes = V1_DEFAULT_LANES if default_lanes is None else default_lanes
    compositions_dir = Path(compositions_dir)
    marker = _marker_text(state_root)
    read = read_presets(compositions_dir, schema)
    presets = read or []
    by_name = {preset.name: preset for preset in presets}
    snapshot = migrate.default_settings_snapshot(lcat)
    # A composition without a reviewed row converts into a profile of its
    # own name (migration_map.generic_row); one a shipped profile's name
    # takes converts under a name of its own and maps to that profile only
    # when its converted lineup is the shipped one (never by name alone).
    rows = dict(targets_table)
    taken = frozenset(by_name) | frozenset(rows)
    for preset in presets:
        if preset.name == migration_map.SHIPPED_2X_DEFAULT or preset.name in targets_table:
            continue
        kind, target = migration_map.generic_row(preset.name, catalog.SEED_PROFILE_NAMES, taken)
        if kind != migration_map.CONVERT or target is None:
            rows[preset.name] = (kind, target)
            continue
        rule = migration_map.generic_rule(target, source=preset.name)
        if target != preset.name:
            trial = convert_target(target, rule, by_name, lcat=lcat, default_lanes=lanes, snapshot=snapshot)
            if trial.status == "write" and _same_lineup(trial.document, store, preset.name):
                rows[preset.name] = (migration_map.SEED, preset.name)
                continue
        rows[preset.name] = (kind, target)
        rules.setdefault(target, rule)
    outcomes = tuple(
        _preset_outcome(
            preset, preset_targets=rows, convert_rules=rules, store=store, lcat=lcat
        )
        for preset in presets
    )
    targets = []
    for target in sorted(rules):
        outcome = convert_target(
            target, rules[target], by_name, lcat=lcat, default_lanes=lanes, snapshot=snapshot
        )
        if outcome.status == "write":
            outcome = _compare_existing(outcome, store)
        targets.append(outcome)
    seeds = tuple(name for name in catalog.SEED_PROFILE_NAMES if store.is_seed(name))
    seeds_absent = tuple(name for name in seeds if not store.has_user(name))
    user_before = sum(1 for name in store.names() if store.has_user(name))
    return ProfileMigrationReport(
        config_root=str(compositions_dir.parent),
        dry_run=dry_run,
        catalog_version=version_doc["catalog_version"],
        launcher_version=version_doc["launcher_version"],
        marker=marker,
        compositions_present=read is not None,
        presets=outcomes,
        targets=tuple(targets),
        seeds=seeds,
        seeds_absent=seeds_absent,
        seeds_installed=(),
        user_profiles_before=user_before,
        blocking=_blocking(outcomes, targets, marker, dry_run),
    )


def apply_plan(
    report: ProfileMigrationReport, store: profile.ProfileStore
) -> ProfileMigrationReport:
    """§4.7 ``--apply``: seeds, then each ``write`` target through ``ProfileStore.new``.

    Blocking → nothing is written (the caller prints PM-3). A P1 refusal
    from ``new`` (a concurrent writer) re-runs the read-only comparison
    (``already-migrated`` or ``conflict``); any other error raises
    :class:`ProfileMigrationStopped` carrying the partial report.
    """

    if report.blocking:
        return dataclasses.replace(report, dry_run=False, applied=False)
    installed: tuple[str, ...] = ()
    targets = list(report.targets)

    def partial(stopped: bool = True) -> ProfileMigrationReport:
        return dataclasses.replace(
            report, dry_run=False, applied=True, stopped=stopped, seeds_installed=installed,
            targets=tuple(targets),
        )

    try:
        if report.seeds_absent:
            installed = tuple(store.install_seeds())
    except Exception as exc:
        raise ProfileMigrationStopped(partial(), exc) from exc
    for index, outcome in enumerate(targets):
        if outcome.status != "write":
            continue
        try:
            store.new(outcome.document)
        except profile.ProfileValidationError as exc:
            raise ProfileMigrationStopped(partial(), exc) from exc
        except profile.ProfileError as exc:
            try:
                present = store.contains(outcome.target)
            except profile.ProfileError:
                present = False
            if not present:
                raise ProfileMigrationStopped(partial(), exc) from exc
            targets[index] = _compare_existing(outcome, store)
            continue
        except Exception as exc:
            raise ProfileMigrationStopped(partial(), exc) from exc
        targets[index] = dataclasses.replace(outcome, status="written")
    done = partial(stopped=False)
    return dataclasses.replace(
        done, blocking=_blocking(done.presets, done.targets, done.marker, False)
    )


# ------------------------------------------------------------------ report


def _findings(items: Sequence[profile.Finding]) -> str:
    return "; ".join(f"{item.code}: {item.message}" for item in items) or "none"


def _preset_line(item: PresetOutcome, width: int) -> str:
    rest = f"({item.reason})" if item.kind in ("drop", "unmapped") else item.reason
    return f"  {item.name:<{width}}  {item.kind:<{OUTCOME_WIDTH}}  {rest}"


def _target_lines(item: TargetOutcome) -> list[str]:
    members = f"; members {', '.join(item.members)}" if len(item.members) > 1 else ""
    lines = [f"  {item.target}  {item.status}  (source {item.source}{members})"]
    if item.status == "skipped":
        return lines
    doc = item.document
    if doc is not None:
        lead = f"    lead {_binding_text(doc['lead'])}  lead_providers {_providers_text(doc.get('lead_providers'))}"
        if doc.get("primary_provider"):
            lead += f"  primary_provider {doc['primary_provider']}"
        lines.append(lead)
        agents = ", ".join(
            f"{profile.label(rid)} {_binding_text(binding)}" for rid, binding in doc["agents"].items()
        )
        lines.append(f"    agents: {agents or 'none'}")
        native = doc["native_agents"]
        text = (
            f"    native: explore {native.get('explore')}, plan {native.get('plan')}, "
            f"general_purpose {native.get('general_purpose')}"
        )
        if "workflows" in doc:
            text += f"; workflows {doc['workflows']}"
        lines.append(text)
    lines.append(f"    notes: {'; '.join(item.notes) or 'none'}")
    lines.append(f"    not carried: {'; '.join(item.not_carried) or 'none'}")
    lines.extend(f"    {text}" for text in item.differs)
    lines.append(f"    warnings: {_findings(item.warnings)}")
    lines.append(f"    notices: {_findings(item.notices)}")
    lines.append(f"    expected warnings: {', '.join(item.expected) or 'none'}")
    if item.status in BLOCKING_STATUSES:
        lines.append(f"    errors: {'; '.join(item.errors) or 'none'}")
    return lines


def _footer(report: ProfileMigrationReport, written: int) -> str:
    if report.dry_run:
        return DRY_RUN_FOOTER
    if not report.applied:
        return (
            f"refused: {len(report.blocking)} blocking outcome(s); nothing was written"
        )
    installed = len(report.seeds_installed)
    if report.stopped:
        return (
            f"stopped: wrote {written} profiles, installed {installed} seeds before an error; "
            "compositions untouched; rerun to converge"
        )
    if report.blocking:
        return (
            f"conflict: {len(report.blocking)} target(s) changed while applying; wrote "
            f"{written} profiles, installed {installed} seeds; compositions untouched"
        )
    if not written and not installed:
        return NOTHING_TO_DO_FOOTER
    return f"applied: wrote {written} profiles, installed {installed} seeds; compositions untouched"


def render_text(report: ProfileMigrationReport) -> str:
    """§4.9, exact."""

    mode = " --dry-run" if report.dry_run else " --apply"
    config = report.config_root
    presets = report.presets
    unreadable = sum(1 for item in presets if item.error is not None)
    absent = "" if report.compositions_present else " (directory absent)"
    lines = [
        f"claude-multi profile migrate{mode}: config {config}",
        f"catalog {report.catalog_version} (launcher {report.launcher_version}); "
        f"state marker {report.marker}",
        f"compositions: {len(presets)} files in {config}/compositions "
        f"({unreadable} unreadable){absent}",
        f"profiles: {report.user_profiles_before} user files; seeds absent "
        f"{len(report.seeds_absent)} ({', '.join(report.seeds_absent) or 'none'})",
        "",
        "presets:",
    ]
    width = max((len(item.name) for item in presets), default=0)
    lines.extend(_preset_line(item, width) for item in presets)
    if not presets:
        lines.append("  (none)")
    lines += ["", "targets:"]
    for item in report.targets:
        lines.extend(_target_lines(item))
    if not report.targets:
        lines.append("  (none)")

    kinds = Counter(item.kind for item in presets)
    seed_counts = Counter(item.target for item in presets if item.kind == "seed-map")
    seed_order = [s for s in catalog.SEED_PROFILE_NAMES if s in seed_counts] + sorted(
        s for s in seed_counts if s not in catalog.SEED_PROFILE_NAMES
    )
    seed_detail = ", ".join(f"{seed} {seed_counts[seed]}" for seed in seed_order)
    statuses = Counter(item.status for item in report.targets)
    warnings = Counter(f.code for item in report.targets for f in item.warnings)
    notices = Counter(f.code for item in report.targets for f in item.notices)
    total_seeds = len(report.seeds)
    present = total_seeds - len(report.seeds_absent)
    if report.dry_run:
        seed_verb, seed_count, added = "install", len(report.seeds_absent), statuses["write"]
    else:
        seed_verb, seed_count, added = "installed", len(report.seeds_installed), statuses["written"]
    after = report.user_profiles_before + seed_count + added

    def counted(counter: Counter) -> str:
        return ", ".join(f"{code} {counter[code]}" for code in sorted(counter)) or "none"

    lines += [
        "",
        "summary:",
        f"  presets {len(presets)}: seed-map {kinds['seed-map']}"
        + (f" ({seed_detail})" if seed_detail else "")
        + f", convert {kinds['convert']}, merge-into {kinds['merge-into']}, "
        f"drop {kinds['drop']}, unmapped {kinds['unmapped']}; unreadable {unreadable}",
        f"  targets {len(report.targets)}: "
        + ", ".join(f"{status} {statuses[status]}" for status in TARGET_STATUSES),
        f"  seeds {total_seeds}: {seed_verb} {seed_count}, present {present}; "
        f"profiles after: {after}",
        f"  warnings: {counted(warnings)}; notices: {counted(notices)}",
        f"  compositions: untouched ({len(presets)} files read, none written)",
        _footer(report, statuses["written"]),
    ]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- command


def run_command(runtime: Any, *, apply: bool, output: TextIO) -> int:
    """``claude-multi profile migrate``: the report on stdout; exit 2 while blocking.

    ``runtime`` is ``cli.Runtime`` (read-only for a dry run: main builds it
    with ``allow_state_writes=False, refresh_shims=False``). Only ``--apply``
    reaches :func:`apply_plan`.
    """

    compositions = runtime.compositions
    store = runtime.profiles
    report = plan(
        compositions_dir=compositions.compositions_dir,
        schema=compositions.schema,
        store=store,
        lcat=runtime.lineup_catalog(),
        version_doc=runtime.catalog.docs["version"],
        state_root=runtime.session_store.root,
        dry_run=not apply,
    )
    if apply:
        try:
            report = apply_plan(report, store)
        except ProfileMigrationStopped as stop:
            output.write(render_text(stop.report))
            output.flush()
            raise
    output.write(render_text(report))
    output.flush()
    return 2 if report.blocking else 0
