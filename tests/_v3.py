"""Version-3 (2.x) record builders for tests (moved out of ``sessions.py``).

``sessions.make_record``/``make_ordinary_record`` left ``src`` with their
last production callers: no current writer produces a v3 record. Tests
still need v3 inputs (migration, hooks on unmigrated records, the
screen regions' own tests), so the two builders live here verbatim, and
:func:`save_v3` writes a v3 record the way a 2.x launcher did (schema and
invariants checked through the store's version-preserving writer, since
``SessionStore.save`` accepts only version 4).

The 2.x snapshot inputs live here too: the frozen default
composition (:func:`default_composition`), the v1 views, the composition
validator and the resolver (:func:`resolve_v1`, :func:`snapshot_v1`,
:func:`snapshot_for`, and :func:`v1_projection` for the one shipped-catalog
v3 record input), moved verbatim out of ``catalog.py`` and
``composition.py`` when they left ``src``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from _layout import REPO_ROOT
from claude_multi import catalog, sessions, strict_json
from claude_multi.composition import (
    CompositionError,
    LEAD_ID,
    auto_compact_trigger,
    operating_window,
)


def save_v3(store: sessions.SessionStore, record: dict[str, Any]):
    """Write a v1-3 record in its own version (``SessionStore.save`` is v4-only)."""

    return store._save_lifecycle(record)


def make_record(
    *,
    managed_id: str | None = None,
    session_id: str | None = None,
    runtime_session_id: str | None = None,
    cwd: str,
    composition_name: str,
    snapshot: dict[str, Any],
    catalog_version: int,
    catalog_hash: str,
    launcher_version: str,
    forked_from: str | None = None,
    mode: str = "legacy",
    scope_generation: int = 0,
    workflows: str = "native",
    now: str | None = None,
    identity_state: str = sessions.IDENTITY_UNVERIFIED,
    launch_epoch: int = 0,
) -> dict[str, Any]:
    """Build a managed-composition v3 record without accepting secrets.

    ``session_id`` remains a source-compatibility keyword for callers being
    migrated. New code should pass ``managed_id`` explicitly. A fresh record's
    requested native UUID initially equals the stable ID; SessionStart replaces
    it with the authoritative runtime UUID before later resumes.
    """

    stable = managed_id if managed_id is not None else session_id
    if stable is None:
        raise sessions.SessionError("make_record requires managed_id")
    if session_id is not None and managed_id is not None and session_id != managed_id:
        raise sessions.SessionError("managed_id and legacy session_id disagree")
    runtime_id = runtime_session_id or stable
    timestamp = now or sessions._now()
    return {
        "version": sessions.V3_RECORD_VERSION,
        "managed_id": stable,
        "runtime_session_id": runtime_id,
        "runtime_aliases": [],
        "session_type": sessions.SESSION_TYPE_MANAGED,
        "identity_state": identity_state,
        "last_event_source": None,
        "last_seen_at": timestamp,
        "pending_forks": [],
        "launch_epoch": launch_epoch,
        "cwd": cwd,
        "composition_name": composition_name,
        "composition_hash": strict_json.bundle_digest(snapshot),
        "snapshot": snapshot,
        "mode": mode,
        "scope_generation": scope_generation,
        "workflows": workflows,
        "catalog_version": catalog_version,
        "catalog_hash": catalog_hash,
        "launcher_version": launcher_version,
        "created_at": timestamp,
        "forked_from": forked_from,
        "migrated_from_version": None,
    }


def make_ordinary_record(
    *,
    managed_id: str,
    runtime_session_id: str | None,
    cwd: str,
    model: str,
    context_profile: str | None,
    catalog_version: int,
    catalog_hash: str,
    launcher_version: str,
    now: str | None = None,
    identity_state: str = sessions.IDENTITY_UNVERIFIED,
    launch_epoch: int = 0,
    mode: str = "durable",
    scope_generation: int = 1,
    no_subagents: bool = False,
) -> dict[str, Any]:
    """Build an ordinary gateway v3 record with no composition semantics."""

    timestamp = now or sessions._now()
    return {
        "version": sessions.V3_RECORD_VERSION,
        "managed_id": managed_id,
        "runtime_session_id": runtime_session_id or managed_id,
        "runtime_aliases": [],
        "session_type": sessions.SESSION_TYPE_ORDINARY,
        "identity_state": identity_state,
        "last_event_source": None,
        "last_seen_at": timestamp,
        "pending_forks": [],
        "launch_epoch": launch_epoch,
        "cwd": cwd,
        "ordinary_model": model,
        "context_profile": context_profile,
        "no_subagents": bool(no_subagents),
        "mode": mode,
        "scope_generation": scope_generation,
        "catalog_version": catalog_version,
        "catalog_hash": catalog_hash,
        "launcher_version": launcher_version,
        "created_at": timestamp,
        "forked_from": None,
        "migrated_from_version": None,
    }


def save_any(store: sessions.SessionStore, record: dict[str, Any]):
    """``store.save`` for a v4 record, :func:`save_v3` for a v1-3 test input."""

    if record.get("version") == sessions.RECORD_VERSION:
        return store.save(record)
    return save_v3(store, record)


# ---------------------------------------------------------------- 2.x inputs
# The 2.x resolver,
# its composition validator and the v1 views left ``src`` with the view
# deletion. Tests still build 2.x session snapshots (v1-3 record inputs for
# migration, hooks, doctor and the resume paths), so the code moved here
# VERBATIM from catalog.py / composition.py, except that the views
# read ``docs["models-v2"]``/``docs["roles-v2"]`` and ``resolve_v1`` calls
# this module's ``validate_composition``. Never import it from ``src``.

DEFAULT_COMPOSITION_PATH = REPO_ROOT / "tests" / "fixtures" / "v3" / "default-composition.json"

LEGACY_ROLE_IDS = ("cm-lead", "cm-analyst", "cm-reviewer", "cm-implementer")

SCOPES = ("lead+agents", "lead", "agents", "off")
_SCOPE_USES = {
    "lead+agents": frozenset({"lead", "agents"}),
    "lead": frozenset({"lead"}),
    "agents": frozenset({"agents"}),
    "off": frozenset(),
}


def _uses(scope: str) -> frozenset[str]:
    return _SCOPE_USES[scope]


def validate_composition(
    composition: dict[str, Any],
    models: dict[str, Any],
    roles: dict[str, Any],
    providers: dict[str, Any],
    new_lines: frozenset[str] = frozenset(),
) -> list[str]:
    """Semantic composition validation against trusted models/roles/providers.

    ``models`` is the v1 view, which omits ``status: new`` lines; keys in
    ``new_lines`` are reported as New · Off instead of unknown.
    """

    errors: list[str] = []
    availability = composition["availability"]
    provider_scope = availability["providers"]
    model_scope = availability["models"]

    def _unknown(where: str, key: str) -> str:
        if key in new_lines:
            return f"{where}: model {key!r} is New · Off (status new) until admitted"
        return f"{where}: unknown model {key!r}"

    for key in sorted(provider_scope):
        if key not in providers:
            errors.append(f"availability.providers: unknown provider {key!r}")
    for key in sorted(model_scope):
        if key not in models:
            errors.append(_unknown("availability.models", key))
            continue
        scope = model_scope[key]
        model = models[key]
        provider = model["provider"]
        granted = provider_scope.get(provider, "off")
        if not _uses(scope) <= _uses(granted):
            errors.append(
                f"availability.models.{key}: scope {scope!r} exceeds provider "
                f"{provider!r} scope {granted!r}"
            )
        if not _uses(scope) <= set(model["capabilities"]):
            errors.append(
                f"availability.models.{key}: scope {scope!r} exceeds immutable "
                f"catalog capability {model['capabilities']!r}"
            )

    lead_slots = [slot for slot in composition["slots"] if slot["role"] == catalog.LEAD_ROLE]
    if len(lead_slots) != 1:
        errors.append(f"slots: exactly one {catalog.LEAD_ROLE} slot required, found {len(lead_slots)}")

    seen_triples: set[tuple[str, str, str]] = set()
    preferred_per_role: dict[str, int] = {}
    analyst_available = 0

    for index, slot in enumerate(composition["slots"]):
        where = f"slots[{index}]"
        role_id = slot["role"]
        model_id = slot["model"]
        if role_id not in roles:
            errors.append(f"{where}: unknown role {role_id!r}")
            continue
        if model_id not in models:
            errors.append(_unknown(where, model_id))
            continue
        model = models[model_id]
        if role_id not in model["compatible_roles"]:
            errors.append(f"{where}: model {model_id!r} is not compatible with {role_id!r}")
        lane = slot.get("lane", model["default_lane"])
        if lane not in model["lanes"]:
            errors.append(f"{where}: model {model_id!r} has no lane {lane!r}")
            continue
        triple = (role_id, model_id, lane)
        if triple in seen_triples:
            errors.append(f"{where}: duplicate slot (role, model, lane) triple {triple!r}")
        seen_triples.add(triple)

        if role_id == catalog.LEAD_ROLE:
            if slot.get("preferred") is not None:
                errors.append(f"{where}: preferred does not apply to the lead slot")
            if slot.get("lane") is not None:
                errors.append(f"{where}: lane does not apply to the lead slot")
            use = "lead"
        else:
            use = "agents"
            preferred_per_role[role_id] = preferred_per_role.get(role_id, 0) + (
                1 if slot.get("preferred") else 0
            )

        scope = model_scope.get(model_id, "off")
        if use not in _uses(scope):
            errors.append(
                f"{where}: model {model_id!r} scope {scope!r} does not allow use as {use}"
            )
        if use == "lead":
            if "lead" not in model["capabilities"]:
                errors.append(f"{where}: model {model_id!r} lacks the lead capability")
            if model["lead"] is None:
                errors.append(f"{where}: model {model_id!r} has no lead block")
        elif role_id == "cm-analyst":
            analyst_available += 1

    for role_id in sorted(preferred_per_role):
        if preferred_per_role[role_id] != 1:
            errors.append(
                f"slots: role {role_id!r} must have exactly one preferred variant, "
                f"found {preferred_per_role[role_id]}"
            )

    policy = composition["native_agents"]
    if policy["explore"] == "replace" and analyst_available == 0:
        errors.append(
            "native_agents.explore: 'replace' requires at least one available "
            "cm-analyst variant"
        )

    workflows = composition.get("workflows", "native")
    if workflows not in ("native", "off"):
        errors.append(f"workflows: invalid value {workflows!r}")

    return errors


def v1_view_entry(key: str, entry: dict[str, Any], role_ids: Sequence[str]) -> dict[str, Any]:
    """The lane-shaped (catalog v1) view of one v2 line.

    A client-effort line gets exactly one lane, id ``default_effort``; a
    gateway-effort line one lane per effort. ``role_hints`` is always empty
    (hints left the model entry). The extra keys (``generation``,
    ``status``, ``registry_overlay``, ``family``) are harmless: the view is
    never schema-validated or serialized into records.
    """

    entry = copy.deepcopy(entry)
    lanes = {
        level: {
            "client_selector": selector,
            "agent_effort": level,
            "proxy_effort_contract": contract,
        }
        for level, selector, contract in catalog.line_selectors(entry)
    }
    default = entry["default_effort"]
    # The view only ever names legacy role ids, in LEGACY_ROLE_IDS
    # order, whatever ``role_ids`` the caller passes (kept for signature
    # compatibility): the 2.x resolver and validate_composition keep seeing
    # exactly four roles.
    del role_ids
    compatible = ([catalog.LEAD_ROLE] if "lead" in entry["capabilities"] else []) + [
        role
        for role in LEGACY_ROLE_IDS
        if role != catalog.LEAD_ROLE and (entry["roles"] == "all" or role in entry["roles"])
    ]
    view = {
        field: entry[field]
        for field in (
            "provider",
            "display",
            "wire_model",
            "capabilities",
            "context",
            "lead",
            "routing_note",
            "minimum_tested",
        )
    }
    view.update(
        client_selector=lanes[default]["client_selector"],
        lanes=lanes,
        default_lane=default,
        compatible_roles=compatible,
        role_hints={},
        generation=entry.get("generation"),
        status=entry.get("status", "active"),
        registry_overlay=entry.get("registry_overlay"),
    )
    if "family" in entry:  # custom synthetic entries only
        view["family"] = entry["family"]
    return view


def v1_models_view(docs: dict[str, Any]) -> dict[str, Any]:
    """The v1 ``models`` document view of ``docs``' v2 lines (the former
    ``catalog.v1_view``, verbatim over ``docs["models-v2"]``); ``status: new`` lines are
    omitted.
    """

    models_doc = docs["models-v2"] if "models-v2" in docs else docs["models"]
    role_ids = list(LEGACY_ROLE_IDS)
    return {
        "version": models_doc["version"],
        "models": {
            key: v1_view_entry(key, entry, role_ids)
            for key, entry in models_doc["models"].items()
            if entry.get("status", "active") != "new"
        },
    }


def legacy_roles_view(docs: dict[str, Any]) -> dict[str, Any]:
    """The 2.x (v1) roles document for unconverted readers.

    Exactly :data:`LEGACY_ROLE_IDS`, in that order, with the v1 fields:
    ``summary`` is the v2 ``description``. ``disallowed_tools`` is dropped
    on purpose: the frozen 2.x scope path never emitted tool lists, so
    for 2.x scopes the read-only contract of reviewers stays a prompt
    contract; the v2 path (``scope.compile_lineup_scope``) compiles
    ``disallowedTools`` from roles v2. Raises KeyError when a legacy id is
    missing. (The former ``catalog.legacy_roles_view``, verbatim over
    ``docs["roles-v2"]``.)
    """

    roles = (docs["roles-v2"] if "roles-v2" in docs else docs["roles"])["roles"]
    return {
        "version": catalog.SUPPORTED_DATA_VERSION,
        "roles": {
            role_id: {
                "summary": roles[role_id]["description"],
                "prompt_file": roles[role_id]["prompt_file"],
                "isolation": roles[role_id]["isolation"],
                "tools": None,
                "disallowed_tools": [],
            }
            for role_id in LEGACY_ROLE_IDS
        },
    }


@dataclass(frozen=True)
class ResolvedVariant:
    id: str
    role: str
    model: str
    lane: str
    client_selector: str
    agent_effort: str
    proxy_effort_contract: str | None
    preferred: bool
    routing_hint: str | None
    isolation: str | None
    display: str
    family: str


@dataclass(frozen=True)
class ResolvedLead:
    model: str
    client_selector: str
    effort: str
    env: dict[str, str]
    display: str
    family: str
    client_context_tokens: int
    provider_context_tokens: int
    provider_context_validated: bool
    auto_compact_tokens: int


@dataclass(frozen=True)
class ResolvedComposition:
    name: str
    description: str
    lead: ResolvedLead
    variants: tuple[ResolvedVariant, ...]
    native_agents: dict[str, str]
    availability: dict[str, Any]
    scalar_context_tokens: int | None
    auto_compact_window_tokens: int
    workflows: str = "native"


def variant_id(role: str, model: str, lane: str) -> str:
    """Deterministic generated agent ID from the (role, model, lane) triple."""

    return f"{role}-{model}-{lane}"


def compute_scalar(selected_models: list[dict[str, Any]]) -> int | None:
    """Minimum explicit process scalar across selected models (ceiling-capped)."""

    values = [
        model["context"]["scalar_tokens"]
        for model in selected_models
        if model["context"]["scalar_tokens"] is not None
    ]
    return operating_window(min(values)) if values else None


def compute_auto_compact_capacity(
    lead_model: dict[str, Any], selected_models: list[dict[str, Any]]
) -> int:
    """Safe process capacity without needlessly shrinking scalar models.

    The environment value is process-wide. A model whose provider bound equals
    its client classification is already protected by Claude Code's per-model
    cap. Extended selectors such as Qwen can advertise a larger client window
    than the provider accepts, so those stricter provider bounds narrow the
    shared capacity for every thread in the process.
    """

    capacity = lead_model["context"]["provider_tokens"]
    for model in selected_models:
        context = model["context"]
        if context["provider_tokens"] < context["client_tokens"]:
            capacity = min(capacity, context["provider_tokens"])
    return operating_window(capacity)


def _lead_context_policy(model: dict[str, Any]) -> tuple[int, int, int]:
    """Claude-client context, provider bound, and initial reactive trigger.

    The trigger is provisional (resolve() recomputes it from the shared
    ceiling-capped capacity); the returned client/provider values stay raw
    catalog evidence.
    """

    context = model["context"]
    client_tokens = context["client_tokens"]
    provider_tokens = context["provider_tokens"]
    return (
        client_tokens,
        provider_tokens,
        auto_compact_trigger(operating_window(provider_tokens)),
    )


def resolve_v1(docs: dict[str, Any], composition: dict[str, Any]) -> ResolvedComposition:
    """Validate and resolve a composition against trusted catalog documents."""

    models = v1_models_view(docs)["models"]
    roles = legacy_roles_view(docs)["roles"]
    providers = docs["providers"]["providers"]

    problems = validate_composition(composition, models, roles, providers)
    if problems:
        raise CompositionError("; ".join(problems))

    lead: ResolvedLead | None = None
    variants: list[ResolvedVariant] = []
    workflows = composition.get("workflows", "native")
    for slot in composition["slots"]:
        role_id = slot["role"]
        model_id = slot["model"]
        model = models[model_id]
        provider = providers[model["provider"]]
        if role_id == LEAD_ID:
            lead_block = model["lead"]
            lead_effort = lead_block["effort"]
            if workflows == "off" and lead_effort == "ultracode":
                # The compile-time derivation (composition level only): with
                # native workflows off, lead ultracode maps to xhigh. The
                # derived value is what the snapshot records.
                lead_effort = "xhigh"
            client_context, provider_context, compact_trigger = _lead_context_policy(
                model
            )
            lead = ResolvedLead(
                model=model_id,
                client_selector=model["client_selector"],
                effort=lead_effort,
                env=dict(lead_block["env"]),
                display=model["display"],
                family=provider["independence_family"],
                client_context_tokens=client_context,
                provider_context_tokens=provider_context,
                provider_context_validated=(
                    model["context"]["validated_tokens"] >= provider_context
                ),
                auto_compact_tokens=compact_trigger,
            )
            continue
        lane_id = slot.get("lane", model["default_lane"])
        lane = model["lanes"][lane_id]
        variants.append(
            ResolvedVariant(
                id=variant_id(role_id, model_id, lane_id),
                role=role_id,
                model=model_id,
                lane=lane_id,
                client_selector=lane["client_selector"],
                agent_effort=lane["agent_effort"],
                proxy_effort_contract=lane["proxy_effort_contract"],
                preferred=bool(slot.get("preferred", False)),
                routing_hint=model["role_hints"].get(role_id),
                isolation=roles[role_id]["isolation"],
                display=model["display"],
                family=provider["independence_family"],
            )
        )

    if lead is None:  # validate_composition guarantees this is unreachable
        raise CompositionError("composition has no lead slot")

    selected = [models[lead.model]] + [models[v.model] for v in variants]
    auto_compact_window = compute_auto_compact_capacity(
        models[lead.model], selected
    )
    lead = replace(
        lead, auto_compact_tokens=auto_compact_trigger(auto_compact_window)
    )
    return ResolvedComposition(
        name=composition["name"],
        description=composition.get("description", ""),
        lead=lead,
        variants=tuple(variants),
        native_agents=dict(composition["native_agents"]),
        availability={
            "providers": dict(composition["availability"]["providers"]),
            "models": dict(composition["availability"]["models"]),
        },
        scalar_context_tokens=compute_scalar(selected),
        auto_compact_window_tokens=auto_compact_window,
        workflows=workflows,
    )


def snapshot_v1(resolved: ResolvedComposition) -> dict[str, Any]:
    """Resolved composition semantics for session snapshots (no secrets).

    ``workflows`` is recorded only when it differs from the default
    (``"native"``): the composition hash still covers the workflow mode both
    directions, while a native-mode snapshot stays byte-identical to the
    pre-rethink form (legacy argv parity).
    """

    document: dict[str, Any] = {
        "lead": {
            "model": resolved.lead.model,
            "client_selector": resolved.lead.client_selector,
            "effort": resolved.lead.effort,
            "env": dict(resolved.lead.env),
            "client_context_tokens": resolved.lead.client_context_tokens,
            "provider_context_tokens": resolved.lead.provider_context_tokens,
            "auto_compact_tokens": resolved.lead.auto_compact_tokens,
        },
        "variants": [
            {
                "id": variant.id,
                "role": variant.role,
                "model": variant.model,
                "lane": variant.lane,
                "client_selector": variant.client_selector,
                "preferred": variant.preferred,
            }
            for variant in resolved.variants
        ],
        "native_agents": dict(resolved.native_agents),
        "availability": {
            "providers": dict(resolved.availability["providers"]),
            "models": dict(resolved.availability["models"]),
        },
        "scalar_context_tokens": resolved.scalar_context_tokens,
        "auto_compact_window_tokens": resolved.auto_compact_window_tokens,
    }
    if resolved.workflows != "native":
        document["workflows"] = resolved.workflows
    return document


_PROJECTION_GROUPS = (
    ("cm-analyst", ("cm-analyst", "cm-analyst-strong")),
    ("cm-implementer", ("cm-implementer", "cm-implementer-light", "cm-implementer-strong")),
    ("cm-reviewer", ("cm-reviewer", "cm-reviewer-strong")),
)


def v1_projection(seed: Mapping[str, Any], view_models: Mapping[str, Any]) -> dict[str, Any]:
    """The 2.x default composition projected from a v2 seed profile (the former
    ``composition.v1_projection``, verbatim; test input for a shipped-catalog v3 record only).

    Lossless or omitted, never snapped: an agent binding becomes a v1 slot only
    when the v1 view offers a lane whose id equals the binding's effort (and
    the view line admits the 2.x role). ``cm-explorer`` and ``cm-designer``
    are never projected. The seed is expected to be valid; a
    binding without both ``model`` and ``effort`` is skipped, never a
    KeyError.
    """

    lead_key = seed["lead"]["model"]
    slots: list[dict[str, Any]] = [{"model": lead_key, "role": LEAD_ID}]
    agents = seed.get("agents", {})
    emitted: set[tuple[str, str, str]] = set()
    for role, ids in _PROJECTION_GROUPS:
        first = True
        for rid in ids:
            binding = agents.get(rid)
            if not isinstance(binding, Mapping):
                continue
            key, effort = binding.get("model"), binding.get("effort")
            if not isinstance(key, str) or not isinstance(effort, str):
                continue
            model = view_models.get(key)
            if model is None or effort not in model["lanes"]:
                continue  # omitted, not snapped
            if role not in model.get("compatible_roles", ()):
                continue
            if (role, key, effort) in emitted:
                continue
            emitted.add((role, key, effort))
            slot: dict[str, Any] = {"model": key, "preferred": first, "role": role}
            if effort != model["default_lane"]:
                slot["lane"] = effort
            slots.append(slot)
            first = False
    models_availability: dict[str, str] = {}
    providers_availability: dict[str, str] = {}
    for slot in slots:
        key = slot["model"]
        if key in models_availability:
            continue
        model = view_models.get(key)
        capabilities = set(model["capabilities"]) if model is not None else {"lead"}
        if {"lead", "agents"} <= capabilities:
            scope = "lead+agents"
        elif "lead" in capabilities:
            scope = "lead"
        else:
            scope = "agents"
        models_availability[key] = scope
        if model is not None:
            providers_availability[model["provider"]] = "lead+agents"
    projection: dict[str, Any] = {
        "availability": {"models": models_availability, "providers": providers_availability},
        "description": (
            f"Interim 2.x projection of the {seed['name']} profile "
            "(bindings without a v1 lane are omitted)"
        ),
        "name": "default",
        "native_agents": dict(seed["native_agents"]),
        "seed": {"id": "default", "version": 1},
        "slots": slots,
        "version": 1,
    }
    if seed.get("workflows") == "off":
        projection["workflows"] = "off"
    return projection

def default_composition() -> dict[str, Any]:
    """The frozen catalog-32 2.x default composition (a deep copy per call)."""

    return copy.deepcopy(_DEFAULT_COMPOSITION)


_DEFAULT_COMPOSITION = strict_json.load(DEFAULT_COMPOSITION_PATH)


def snapshot_for(docs: dict[str, Any], document: dict[str, Any] | None = None) -> dict[str, Any]:
    """The 2.x session snapshot of ``document`` (default: the frozen default)."""

    return snapshot_v1(resolve_v1(docs, document if document is not None else default_composition()))
