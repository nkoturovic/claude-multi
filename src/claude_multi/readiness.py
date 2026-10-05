"""Shared per-slot readiness, the seed order and starter planning.

Pure: no filesystem, network or store access. The Runtime gathers the local
observations once (``Runtime.readiness_observations``) and every consumer
(the card, doctor, seed selection, ``profile starter``) reads the same
verdicts from here.

Two kinds of facts, kept apart:

* **Authority** (fail closed): evaluation errors (admission, route,
  capability/role/effort, the agent gate) and the selected-transport
  direct-provider credential. They are reported here with ``authority=True``
  but they are enforced where they always were (``profile.evaluate``,
  ``Runtime.lineup_secret_problems``); readiness never adds a block.
* **Readiness** (never a launch block): the local gateway's served set, OAuth
  credential records, LAN reachability, passive quota. An unavailable
  observation (gateway down, management disabled, no journal, no quota
  data, an unreadable or absent auth directory) is ``unknown``, never a
  failure. The one exception is a slot bound to a known-unreachable LAN
  line refuses a launch fast (``lan_refusals``), with the network-scoped text.

Ready means locally configured and served, never upstream verification.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import catalog, lineup_files, profile

READY = "ready"
BLOCKED = "blocked"
UNKNOWN = "unknown"
UNBOUND = "unbound"
STATES = (READY, BLOCKED, UNKNOWN, UNBOUND)

LEGEND = (
    "Ready means locally configured and served; upstream authentication, quota, "
    "and reachability may still fail."
)
# The order in which the default-profile rule tries the shipped profiles:
# the first whose every bound slot is ready wins (``setup.defaults``).
SEED_ORDER = ("balanced", "quality", "max", "economy", "claude", "openrouter", "openai", "direct")
LAN_UNREACHABLE_REMEDY = "connect to the host's network, or bind another model"
SERVED_REMEDY = "apply your changes (G → P, or claude-multi providers apply), then claude-multi doctor"
OBSERVE_REMEDY = "start the gateway, then claude-multi doctor"


@dataclass(frozen=True)
class Reason:
    """One structured readiness reason (never parsed from presentation)."""

    code: str  # evaluation | credential | oauth-records | lan | served
    text: str
    remedy: str
    authority: bool = False
    blocking: bool = True  # False: an unknown observation


@dataclass(frozen=True)
class Observations:
    """Local observations of one refresh (every field optional).

    ``served``: the gateway's served ids (None: not observed). ``oauth_records``:
    pool -> credential-record count (None: the auth directory was not
    observable). ``lan``: provider -> (state, network-scoped text) of a
    keyless LAN provider (absent: not probed). ``credential_problems``:
    provider -> the selected-transport problem (authority). ``quota``: provider -> a
    passive warning (never a readiness verdict). ``login``: pool -> the
    sign-in command text."""

    served: frozenset[str] | None = None
    oauth_records: Mapping[str, int] | None = None
    lan: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    credential_problems: Mapping[str, str] = field(default_factory=dict)
    quota: Mapping[str, str] = field(default_factory=dict)
    login: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SlotReadiness:
    slot: str  # "cm-lead" or an agent id
    state: str
    key: str | None = None
    reasons: tuple[Reason, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state == READY

    @property
    def first(self) -> Reason | None:
        return self.reasons[0] if self.reasons else None

    def text(self) -> str:
        """``<slot label>: <first reason> — <remedy>`` (card, doctor, preview)."""

        reason = self.first
        where = profile.label(self.slot)
        if reason is None:
            return f"{where}: {self.state}"
        return f"{where}: {reason.text} — {reason.remedy}"


def is_lan(provider: Mapping[str, Any]) -> bool:
    """A keyless LAN route (``direct-openai`` with ``auth.kind`` none)."""

    transport = provider.get("transport") if isinstance(provider, Mapping) else None
    if not isinstance(transport, Mapping) or transport.get("kind") != "direct-openai":
        return False
    auth = transport.get("auth")
    return isinstance(auth, Mapping) and auth.get("kind") == "none"


def slot_readiness(
    slot: str,
    binding: profile.ResolvedBinding | None,
    providers: Mapping[str, Mapping[str, Any]],
    observations: Observations,
) -> SlotReadiness:
    """One slot's readiness from its resolved binding and the observations."""

    if binding is None:
        return SlotReadiness(slot, UNBOUND)
    reasons: list[Reason] = []
    provider = providers.get(binding.provider) or {}
    problem = observations.credential_problems.get(binding.provider)
    if problem is not None:
        reasons.append(Reason("credential", problem, "set the provider's key (G providers → K)", authority=True))
    transport = provider.get("transport") if isinstance(provider, Mapping) else None
    transport = transport if isinstance(transport, Mapping) else {}
    if transport.get("kind") == "oauth-pool":
        pool = str(transport.get("pool"))
        login = observations.login.get(pool, "claude-multi providers sign-in anthropic|openai")
        if observations.oauth_records is None:
            reasons.append(Reason("oauth-records", f"{pool} credential records not observed", login, blocking=False))
        elif observations.oauth_records.get(pool, 0) <= 0:
            reasons.append(Reason("oauth-records", f"no {pool} OAuth credential record", login))
    if is_lan(provider):
        state, text = observations.lan.get(binding.provider, ("unknown", ""))
        if state == "unreachable":
            reasons.append(Reason("lan", text, LAN_UNREACHABLE_REMEDY))
        elif state != "reachable":
            reasons.append(Reason("lan", f"{binding.provider} reachability not checked", "claude-multi doctor",
                                  blocking=False))
    alias = lineup_files.normalize_model(binding.selector)
    if observations.served is None:
        reasons.append(Reason("served", "gateway observation unavailable", OBSERVE_REMEDY, blocking=False))
    elif alias not in {lineup_files.normalize_model(item) for item in observations.served}:
        reasons.append(Reason("served", f"{alias} is not served by the local gateway", SERVED_REMEDY))
    warnings = tuple(w for w in (observations.quota.get(binding.provider),) if w)
    reasons.sort(key=lambda r: (not r.authority, not r.blocking))
    if any(r.blocking for r in reasons):
        state = BLOCKED
    elif reasons:
        state = UNKNOWN
    else:
        state = READY
    return SlotReadiness(slot, state, binding.key, tuple(reasons), warnings)


def lineup_readiness(
    lineup: profile.ResolvedLineup,
    providers: Mapping[str, Mapping[str, Any]],
    observations: Observations,
) -> tuple[SlotReadiness, ...]:
    """The lead, then every agent id in ``AGENT_ROLE_IDS`` order (unbound
    slots included: intentionally unbound is not a failure)."""

    rows = [slot_readiness(catalog.LEAD_ROLE, lineup.lead.binding, providers, observations)]
    for rid in catalog.AGENT_ROLE_IDS:
        agent = lineup.agents.get(rid)
        rows.append(slot_readiness(rid, agent.binding if agent is not None else None, providers, observations))
    return tuple(rows)


def evaluation_blocked(errors: Sequence[str]) -> tuple[SlotReadiness, ...]:
    """A lineup that does not evaluate: one authority-blocked lead row."""

    return (SlotReadiness(catalog.LEAD_ROLE, BLOCKED, None, tuple(
        Reason("evaluation", str(error), "fix the profile (claude-multi profile edit)", authority=True)
        for error in errors)),)


def satisfiable(rows: Sequence[SlotReadiness]) -> bool:
    """Every configured bound slot is ready (unbound slots never count)."""

    bound = [row for row in rows if row.state != UNBOUND]
    return bool(bound) and all(row.ready for row in bound)


def unready_texts(rows: Sequence[SlotReadiness]) -> list[str]:
    """Card/doctor lines for bound slots that are not ready and not only
    unobserved: ``<slot>: <reason> — <remedy>`` (authority rows excluded:
    their own refusal already names them)."""

    return [row.text() for row in rows
            if row.state == BLOCKED and row.first is not None and not row.first.authority]


def lan_refusals(rows: Sequence[SlotReadiness]) -> list[str]:
    """The fast pre-launch refusal of a bound slot on a known-unreachable
    LAN line (the one readiness fact that refuses a launch)."""

    return [f"{profile.label(row.slot)}: {row.key}: {reason.text} — {reason.remedy}"
            for row in rows for reason in row.reasons if reason.code == "lan" and reason.blocking]


# ------------------------------------------------------------ starter (§4.9)

STARTER_NAME = "starter"
STARTER_TEMPLATE = "balanced"
STARTER_HEADER = "starter preview — no profile written"
STARTER_FOOTER = "No model was admitted or qualified."
STARTER_CONFIRM = "Save as {name}? [y/N] "


@dataclass(frozen=True)
class StarterRow:
    slot: str
    template: str  # "<key> · <effort>" of the template binding, or "unbound"
    proposed: str  # "<key> · <effort>" or "unbound"


@dataclass(frozen=True)
class StarterPlan:
    document: dict[str, Any] | None  # None: refused (no ready lead)
    rows: tuple[StarterRow, ...]
    warnings: tuple[str, ...]
    unresolved: tuple[str, ...]  # agent slots left unbound
    refusal: str | None = None


def _binding_text(binding: Mapping[str, Any] | None) -> str:
    if not isinstance(binding, Mapping):
        return "unbound"
    return f"{binding.get('model')} · {binding.get('effort')}"


def _effort_distance(effort: str, target: str) -> int:
    order = profile.EFFORT_ORDER
    if effort not in order or target not in order:
        return len(order)
    return abs(order.index(effort) - order.index(target))


def plan_starter(
    template: Mapping[str, Any],
    lcat: profile.LineupCatalog,
    *,
    name: str,
    ready_keys: frozenset[str],
    slot_ok: Any,
) -> StarterPlan:
    """The starter profile from ``template`` (the balanced seed's role and
    grade structure) over ready lines only.

    ``ready_keys`` are the offered lines whose readiness (credential,
    served, reachability) is ``ready``; ``slot_ok(slot, key, effort)`` is
    the authority check for one binding (``profile.evaluate`` of a one-slot
    document: capability, role, effort, the agent gate). Per slot: keep the
    exact template binding when it is ready and allowed; otherwise choose
    among ready lines that admit that exact slot, preferring the template
    binding's family, then the effort nearest the template's, then a
    stable key order. Model quality is never inferred from names. An
    agent slot with no candidate stays unbound and is named; no ready lead
    refuses. Nothing is admitted, qualified or written."""

    document = copy.deepcopy(dict(template))
    document.pop("seed", None)
    document["name"] = name
    document["description"] = f"Generated by claude-multi profile starter from the {STARTER_TEMPLATE} template."
    families: dict[str, str] = {}
    for key, entry in lcat.lines.items():
        try:
            families[key] = lcat.line_family(key, entry)
        except (KeyError, TypeError):
            continue

    lead_family: list[str] = []

    def independent(key: str) -> int:
        """0 for a known family other than the lead's (reviewers only)."""

        if slot_is_reviewer[0] and lead_family:
            family = families.get(key)
            return 0 if family not in (None, profile.UNKNOWN_FAMILY, lead_family[0]) else 1
        return 0

    slot_is_reviewer = [False]

    def choose(slot: str, wanted: Mapping[str, Any] | None) -> dict[str, str] | None:
        want_key = wanted.get("model") if isinstance(wanted, Mapping) else None
        want_effort = str(wanted.get("effort")) if isinstance(wanted, Mapping) else "high"
        if isinstance(want_key, str) and want_key in ready_keys and slot_ok(slot, want_key, want_effort):
            return {"model": want_key, "effort": want_effort}
        family = families.get(want_key) if isinstance(want_key, str) else None
        candidates: list[tuple[int, int, str, str]] = []
        for key in sorted(ready_keys):
            entry = lcat.lines.get(key)
            if not isinstance(entry, Mapping):
                continue
            efforts = [level for level in profile.declared_efforts(entry)]
            if slot == catalog.LEAD_ROLE and want_effort == profile.ULTRACODE:
                efforts = [profile.ULTRACODE] if isinstance(entry.get("lead"), Mapping) else []
            ranked = sorted(efforts, key=lambda level: (_effort_distance(level, want_effort), level))
            for effort in ranked:
                if slot_ok(slot, key, effort):
                    candidates.append((0 if families.get(key) == family else 1, independent(key),
                                       _effort_distance(effort, want_effort), key, effort))
                    break
        if not candidates:
            return None
        *_rank, key, effort = min(candidates)
        return {"model": key, "effort": effort}

    rows: list[StarterRow] = []
    lead = choose(catalog.LEAD_ROLE, template.get("lead"))
    rows.append(StarterRow(catalog.LEAD_ROLE, _binding_text(template.get("lead")), _binding_text(lead)))
    if lead is None:
        return StarterPlan(None, tuple(rows), (), (),
                           refusal="starter: no ready lead line on this machine — nothing to propose "
                                   "(sign in or set a key: G providers; then claude-multi doctor)")
    document["lead"] = lead
    lead_family.append(families.get(lead["model"], profile.UNKNOWN_FAMILY))
    agents: dict[str, Any] = {}
    unresolved: list[str] = []
    template_agents = template.get("agents") if isinstance(template.get("agents"), Mapping) else {}
    for rid in catalog.AGENT_ROLE_IDS:
        wanted = template_agents.get(rid)
        if wanted is None:
            continue
        slot_is_reviewer[0] = rid in profile.REVIEWER_IDS
        chosen = choose(rid, wanted)
        rows.append(StarterRow(rid, _binding_text(wanted), _binding_text(chosen)))
        if chosen is None:
            unresolved.append(rid)
        else:
            agents[rid] = chosen
    # A grade whose ``requires`` is unbound cannot stand alone (R-R rules):
    # unbind it too and name it.
    changed = True
    while changed:
        changed = False
        for rid in list(agents):
            role = lcat.roles.get(rid) if isinstance(lcat.roles, Mapping) else None
            requires = role.get("requires", ()) if isinstance(role, Mapping) else ()
            if any(required not in agents for required in requires or ()):
                del agents[rid]
                unresolved.append(rid)
                rows = [StarterRow(row.slot, row.template, "unbound") if row.slot == rid else row for row in rows]
                changed = True
    document["agents"] = agents
    native = document.get("native_agents")
    if isinstance(native, dict) and native.get("explore") == "replace" and "cm-explorer" not in agents:
        native["explore"] = "native"  # 'replace' requires cm-explorer
    order = {rid: index for index, rid in enumerate(catalog.AGENT_ROLE_IDS)}
    return StarterPlan(document, tuple(rows), (), tuple(sorted(set(unresolved), key=order.get)))


def starter_preview(plan: StarterPlan, *, name: str, warnings: Sequence[str] = ()) -> str:
    """The exact preview: one row per slot, the warnings, and
    the footer; the confirm prompt is separate (:data:`STARTER_CONFIRM`)."""

    lines = [STARTER_HEADER]
    lines += [f"{profile.label(row.slot)}: {row.template} -> {row.proposed}" for row in plan.rows]
    notes = list(warnings) + [f"{profile.label(slot)} unbound (no ready line admits it)" for slot in plan.unresolved]
    lines.append("Warnings: " + ("; ".join(notes) if notes else "none"))
    lines.append(STARTER_FOOTER)
    return "\n".join(lines) + "\n"
