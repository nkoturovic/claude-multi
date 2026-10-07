"""Profiles v2: parse, evaluate and resolve a lineup.

A profile binds the lead and each agent id of roles v2 to a model line and
an effort — directly (``{"model", "effort"}``) or through a named binding
(``{"use": <name>}``, the operator's ``bindings.json``). :func:`evaluate`
turns a document into either the list of exact validation errors or a
:class:`ResolvedLineup`: retired keys resolved through the retired map
(with notices), lead class, provider spread, outsiders, warnings and the
review-routing table.

Everything in this module except the loader helper :func:`load_profile_file`,
the import-time schema reads and the two stores is pure: no filesystem
access, no clock, no environment. The profile and named-bindings schemas are
read and checked **once at import** from the package
copies (``schemas/{profile,bindings}.schema.json`` next to ``src/``; the
fixture mirrors are byte-equal and guarded by ``test_fixture_assets``), so
:func:`evaluate`/:func:`resolve` never touch the filesystem.

The stores are the only I/O: :class:`BindingStore`
(``<config root>/bindings.json``) and :class:`ProfileStore`
(``<config root>/profiles/<name>.json``). Construction and reads have no
side effects; every mutator runs, in this order, the state-marker check, the
private-directory creation, **one** acquisition of the store's leaf
FileLock, the load/check/mutate step and an unlocked private write — a store
method never re-acquires its own lock.

This module never imports cli, tui, compiler, scope, launch, transition or
composition.
"""

from __future__ import annotations

from . import assets

import copy
import tempfile
import os
import re
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import catalog, errors, paths, sessions, settings, state, strict_json
from . import validate as schema_validate
from .platform import posix_fs
from .catalog import (
    AGENT_ROLE_IDS,
    DEFAULT_SEED,
    LEAD_ROLE,
    PROFILE_DATA_VERSION,
    ROLE_IDS,
    SEED_PROFILE_NAMES,
)

__all__ = [
    "AGENT_ROLE_IDS",
    "AUTHOR_ORDER",
    "BINDINGS_DATA_VERSION",
    "BINDING_NAME",
    "BindingError",
    "BindingStore",
    "DEFAULT_SEED",
    "EFFORT_ORDER",
    "Evaluation",
    "Finding",
    "LEAD_ROLE",
    "LineupCatalog",
    "OVERRIDABLE_SETTINGS",
    "PROFILE_DATA_VERSION",
    "ProfileError",
    "ProfileStore",
    "ProfileValidationError",
    "REVIEWER_IDS",
    "ROLE_IDS",
    "ResolvedAgent",
    "ResolvedBinding",
    "ResolvedLead",
    "ResolvedLineup",
    "RoleSpec",
    "RouteCell",
    "RouteRow",
    "Routing",
    "SEED_PROFILE_NAMES",
    "STRICT_TOOL_SCHEMA_PROVIDERS",
    "ULTRACODE",
    "WRITER_IDS",
    "ad_hoc_direct",
    "binding_selector",
    "bindings_path",
    "bindings_schema_path",
    "declared_efforts",
    "available_efforts",
    "effort_warnings",
    "binding_warnings",
    "qualification_warnings",
    "MODEL_WARNING_CODES",
    "TRANSIENT_WARNING_CODES",
    "derive_routing",
    "recognized_family",
    "AgentEligibility",
    "AgentFacts",
    "AgentGate",
    "RoleWindow",
    "WindowPolicy",
    "agent_class_window",
    "agent_compaction_trigger",
    "agent_selectors",
    "default_policy",
    "keep_recorded_agent_class",
    "lead_set_member",
    "line_agent_window_problems",
    "lineup_windows",
    "role_window",
    "session_policy",
    "window_ceiling",
    "window_policy",
    "agent_eligibility",
    "agent_window_problem",
    "independent_families",
    "effort_rank",
    "evaluate",
    "format_tokens",
    "is_metered",
    "label",
    "load_profile_file",
    "parse",
    "profile_schema_path",
    "profiles_dir",
    "quota_label",
    "referenced_bindings",
    "resolve",
]

# ------------------------------------------------------------------ constants

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
ULTRACODE = "ultracode"
WRITER_IDS = ("cm-implementer-light", "cm-implementer", "cm-implementer-strong")
REVIEWER_IDS = ("cm-reviewer", "cm-reviewer-strong")
AUTHOR_ORDER = (*WRITER_IDS, LEAD_ROLE)  # TUI §2.2 row order
STRONG_PAIRS = (
    ("cm-analyst-strong", "cm-analyst"),
    ("cm-implementer-strong", "cm-implementer"),
    ("cm-reviewer-strong", "cm-reviewer"),
)
BINDING_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# Settings that are otherwise global and a profile may override: the one
# shared mapping in settings.py (the global Settings field has the same key
# and bounds).
OVERRIDABLE_SETTINGS: Mapping[str, tuple[int, int]] = settings.OVERRIDABLE
# Routes that enforce strict tool schemas
# (``required`` must list every property; the hook/goal evaluator's tools
# 400). Route-scoped, never model-named; a warning, never an error.
STRICT_TOOL_SCHEMA_PROVIDERS = frozenset({"meta"})

_BINDING_SHAPES = (frozenset({"model", "effort"}), frozenset({"use"}))
_DEFAULT_WORKFLOWS = "native"  # as in legacy compositions (composition.py)


class ProfileError(errors.ClaudeMultiError, ValueError):
    """A profile could not be loaded, stored or found."""


class ProfileValidationError(ProfileError):
    """A profile document failed validation; ``errors`` holds every message."""

    def __init__(self, errors: Sequence[str]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


_JSON_LINE = re.compile(r"\bat line (\d+)\b")
_ERROR_WHERE = re.compile(r"^(\$?[A-Za-z0-9_.$\[\]-]+): ")


class ProfileLoadError(ProfileError):
    """A profile file exists but cannot be loaded.

    ``path`` is the file, ``line`` the JSON line of a syntax error (None
    otherwise), ``where`` the field of the first validation problem (None
    otherwise) and ``detail`` the reason.
    """

    def __init__(self, name: str, path: Path, detail: str, *, line: int | None = None,
                 where: str | None = None, environ: Mapping[str, str] | None = None):
        self.name = name
        self.path = Path(path)
        self.line = line
        self.where = where
        self.detail = detail
        shown = paths.display(self.path, environ if environ is not None else os.environ)
        at = f" line {line}" if line else ""
        super().__init__(f"cannot load profile {name!r}: {shown}{at}: {detail}")

    @classmethod
    def from_exception(cls, name: str, path: Path, exc: BaseException, *,
                       environ: Mapping[str, str] | None = None) -> "ProfileLoadError":
        if isinstance(exc, ProfileValidationError):
            detail = exc.errors[0] if exc.errors else str(exc)
            match = _ERROR_WHERE.match(detail)
            return cls(name, path, detail, where=match.group(1) if match else None, environ=environ)
        detail = _error_detail(exc)
        match = _JSON_LINE.search(detail)
        return cls(name, path, detail, line=int(match.group(1)) if match else None, environ=environ)


class ProfileChangedError(ProfileError):
    """The profile changed on disk after the caller read it (a save or
    removal with an ``expected`` digest refuses instead of overwriting)."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"profile {name!r} changed since it was read; nothing written")


class ProfileBatchError(ProfileError):
    """A batch write stopped part-way: ``done`` names the profiles already
    written (each one complete), ``failed`` the one it stopped at and
    ``cause`` why. ``failed_changed`` says whether the file of ``failed``
    was replaced before the failure (a later step of it failed, such as its
    install record); ``changed`` names every profile whose file changed."""

    def __init__(self, done: Sequence[str], failed: str, cause: BaseException, *,
                 failed_changed: bool = False):
        self.done = tuple(done)
        self.failed = failed
        self.cause = cause
        self.failed_changed = failed_changed
        written = ", ".join(self.changed)
        tail = f"{failed} not finished" if failed_changed else f"{failed} not"
        super().__init__(f"{written} written; {tail}: {cause}")

    @property
    def changed(self) -> tuple[str, ...]:
        return (*self.done, self.failed) if self.failed_changed else self.done


# ------------------------------------------------------------ small helpers


def label(slot: str) -> str:
    """``"lead"`` for cm-lead, else the id without ``cm-`` (``"analyst-strong"``)."""

    return "lead" if slot == LEAD_ROLE else slot[3:]


def effort_rank(effort: str) -> int:
    """Position in :data:`EFFORT_ORDER`; ValueError for ultracode or an unknown effort."""

    return EFFORT_ORDER.index(effort)


def _rank_key(effort: str) -> tuple[int, str]:
    if effort in EFFORT_ORDER:
        return (EFFORT_ORDER.index(effort), effort)
    return (len(EFFORT_ORDER), effort)


def declared_efforts(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """A line's efforts (client list or gateway map keys), in :data:`EFFORT_ORDER`."""

    efforts = entry.get("efforts")
    if isinstance(efforts, (list, tuple)):
        values = [value for value in efforts if isinstance(value, str)]
    elif isinstance(efforts, Mapping):
        values = [value for value in efforts if isinstance(value, str)]
    else:
        values = []
    return tuple(sorted(set(values), key=_rank_key))


def available_efforts(entry: Mapping[str, Any], native_efforts: Sequence[str], *,
                      lead: bool = False, workflow: bool = False) -> tuple[str, ...]:
    """Representable efforts, independent of a line's recommendations.

    A single-selector client line carries native effort separately. Workflow
    defaults have no separate effort channel, so only its default is usable.
    Gateway efforts always require their exact selector/contract mapping.
    """

    declared = declared_efforts(entry)
    native = set(native_efforts)
    client = isinstance(entry.get("efforts"), (list, tuple))
    if client:
        allowed = native if isinstance(entry.get("selector"), str) and entry["selector"] else set()
        if workflow:
            allowed = allowed & {entry.get("default_effort")}
    else:
        efforts = entry.get("efforts")
        allowed = {level for level in declared if level in native and isinstance(efforts[level], Mapping)
                   and efforts[level].get("selector") and "proxy_contract" in efforts[level]}
    if lead and ULTRACODE in native and isinstance(entry.get("lead"), Mapping):
        allowed.add(ULTRACODE)
    else:
        allowed.discard(ULTRACODE)
    return tuple(sorted(allowed, key=_rank_key))


def effort_warnings(entry: Mapping[str, Any], effort: str, *, slot: str | None = None) -> tuple[Finding, ...]:
    """A supported single-selector effort not verified by this declaration."""

    if (isinstance(entry.get("efforts"), (list, tuple)) and effort != ULTRACODE
            and effort not in declared_efforts(entry)):
        message = f"effort {effort!r} unverified for this line (not declared)"
        return (Finding("effort-unverified", slot, message, message),)
    return ()


def format_tokens(n: int) -> str:
    """``1_000_000`` → ``"1M"``, ``258_400`` → ``"258K"``, ``500_000`` → ``"500K"``."""

    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000}M"
    return f"{(n + 500) // 1000}K"


def is_metered(provider: Mapping[str, Any]) -> bool:
    """A provider whose calls draw on a quota: an OAuth pool, or any keyed route.

    Only a keyless direct provider (``auth.kind: none``, e.g. llm-local) is
    unmetered (derived from the transport, no catalog field).
    """

    transport = provider.get("transport")
    if not isinstance(transport, Mapping):
        return True
    if transport.get("kind") == "oauth-pool":
        return True
    auth = transport.get("auth")
    return not (isinstance(auth, Mapping) and auth.get("kind") == "none")


def quota_label(provider: Mapping[str, Any]) -> str:
    """``"codex quota shared"`` for an OAuth pool, else ``"API key quota shared"``."""

    transport = provider.get("transport")
    if isinstance(transport, Mapping) and transport.get("kind") == "oauth-pool":
        return f"{transport.get('pool')} quota shared"
    return "API key quota shared"


def profile_schema_path() -> Path:
    return assets.root() / "schemas" / "profile.schema.json"


def bindings_schema_path() -> Path:
    return assets.root() / "schemas" / "bindings.schema.json"


def _load_schema(path: Path) -> dict[str, Any]:
    schema = strict_json.load(path)
    schema_validate.check_schema(schema)
    return schema


# Read once at import: evaluate/resolve stay pure.
_PROFILE_SCHEMA: dict[str, Any] = _load_schema(profile_schema_path())
_BINDINGS_SCHEMA: dict[str, Any] = _load_schema(bindings_schema_path())


def binding_selector(
    entry: Mapping[str, Any],
    provider: Mapping[str, Any],
    effort: str,
    *,
    lead: bool,
) -> tuple[str, str | None]:
    """``(selector, proxy_contract)`` a binding of ``entry`` at ``effort`` uses.

    Client-effort lines have one selector and no contract (the effort
    travels in frontmatter / ``--effort``). Gateway-effort lines use
    ``efforts[effort]``; an ``ultracode`` lead uses ``efforts[default_effort]``.
    The single rule for profile evaluation, the lead set and agent files,
    and the live lead switch. Raises
    KeyError/TypeError on a malformed entry; :func:`evaluate` checks the
    shape first.

    ``lead`` is the purpose: ``True`` for the lead set and a lead binding,
    ``False`` for an agent binding, the agent set and the workflow default.
    This is the line's selector; an agent's context class (``[1m]`` or the
    200K class) follows from the session window (:func:`role_window`).
    """

    if _line_mode(entry, provider) == "client":
        return entry["selector"], None
    level = entry["default_effort"] if lead and effort == ULTRACODE else effort
    spec = entry["efforts"][level]
    return spec["selector"], spec["proxy_contract"]


# ------------------------------------------------------------ context windows
#
# One process-wide compaction policy per session: the window
# (``CLAUDE_CODE_AUTO_COMPACT_WINDOW``) is min(ceiling, the lead set's
# smallest provider bound) and the percent is
# ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE``. The pinned client applies that window
# to the lead and to every subagent in the 1M class (a ``[1m]`` selector);
# a selector without ``[1m]`` runs in the client's 200K class (or the
# exported ``CLAUDE_CODE_MAX_CONTEXT_TOKENS``). :func:`role_window` is the
# one rule that decides each role's class and effective window from that
# policy: an agent on a 1M-class line whose provider bound is at least the
# session window keeps ``[1m]`` and runs at the window; one whose bound is
# below it uses the same alias without ``[1m]``, so it compacts in the 200K
# class. Very small provider bounds can still overflow; evaluation warns.
SUFFIX_1M = "[1m]"


@dataclass(frozen=True)
class WindowPolicy:
    """The compiled window/percent policy of one session."""

    window: int  # the session window: lead and every 1M-class agent
    percent: int = settings.COMPACTION_PERCENT_DEFAULT
    scalar: int | None = None  # the exported CLAUDE_CODE_MAX_CONTEXT_TOKENS, if any


@dataclass(frozen=True)
class RoleWindow:
    """What one role (the lead, an agent, the workflow default) runs with."""

    selector: str  # the selector the role uses
    client_class: int  # the client's context class for that selector
    window: int  # the effective compaction window: min(class, session window)
    trigger: int  # the reactive compaction trigger at the policy percent
    narrowed: bool = False  # a 1M-class line kept in the 200K class (bound below the window)


def default_policy(effective: "settings.Effective | None" = None, *,
                   percent: int | None = None) -> WindowPolicy:
    """The policy before a lineup decides it: the window ceiling itself.
    ``percent`` None is the largest allowed percent (the conservative
    trigger an eligibility check outside a lineup uses)."""

    return WindowPolicy(window=window_ceiling(effective),
                        percent=settings.COMPACTION_PERCENT_MAX if percent is None else percent)


def window_ceiling(effective: "settings.Effective | None") -> int:
    """The window ceiling of ``effective`` (the default without one)."""

    value = getattr(effective, "window_ceiling", None) if effective is not None else None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return settings.WINDOW_CEILING_DEFAULT


def window_policy(
    rows: Iterable[tuple[int, int, int | None]], percent: int = settings.COMPACTION_PERCENT_DEFAULT,
    *, ceiling: int = settings.WINDOW_CEILING_DEFAULT,
) -> WindowPolicy:
    """The session policy over the lead set's ``(provider_tokens,
    client_tokens, scalar_tokens)`` rows: window = min(ceiling, the smallest
    provider bound); the scalar (the smallest declared scalar, capped by the
    ceiling) only when every lead-set client class is below 1M. No rows:
    the ceiling itself."""

    rows = tuple(rows)
    if not rows:
        return WindowPolicy(window=ceiling, percent=percent)
    scalars = [scalar for _provider, _client, scalar in rows if scalar is not None]
    scalar = (min(min(scalars), ceiling)
              if scalars and max(client for _provider, client, _scalar in rows) < CLASS_1M else None)
    return WindowPolicy(window=min(min(provider for provider, _client, _scalar in rows), ceiling),
                        percent=percent, scalar=scalar)


def role_window(
    selector: str, *, client_tokens: int, provider_tokens: int, policy: WindowPolicy, decide: bool,
) -> RoleWindow:
    """The one rule for a role's context class and effective window.

    ``decide`` True decides an agent's class from its line selector (an
    agent binding, the agent set, the workflow default, the agent gate): a
    ``[1m]`` selector whose ``provider_tokens`` is below the session window
    is narrowed to the same alias without ``[1m]``. ``decide`` False reads a
    selector as bound (the lead, a resolved or recorded binding). The class
    is 1M for ``[1m]``; otherwise the exported scalar when the policy has
    one, else 200K for a newly decided agent. Reading an already bound
    selector retains its ``client_tokens``."""

    narrowed = decide and selector.endswith(SUFFIX_1M) and provider_tokens < policy.window
    if narrowed:
        selector = selector[: -len(SUFFIX_1M)]
    if selector.endswith(SUFFIX_1M):
        client = CLASS_1M
    elif policy.scalar is not None:
        client = policy.scalar
    else:
        client = CLASS_STANDARD if decide else client_tokens
    window = min(client, policy.window)
    return RoleWindow(selector, client, window, agent_compaction_trigger(window, policy.percent), narrowed)


def lead_set_member(entry: Mapping[str, Any], provider_id: str, lead_class: str | None,
                    lead_providers: Iterable[str] | None) -> bool:
    """Whether an offered line has the lead fields and ``lead_class`` the
    process needs and, when ``lead_providers`` narrows the set, is on one
    of them. Capability recommendations do not change membership."""

    if not isinstance(entry.get("lead"), Mapping):
        return False
    context = entry.get("context") if isinstance(entry.get("context"), Mapping) else {}
    if lead_class is None or context.get("ordinary_profile") != lead_class:
        return False
    return lead_providers is None or provider_id in frozenset(lead_providers)


def session_policy(
    lcat: "LineupCatalog", effective: "settings.Effective | None", *, lead_class: str | None,
    lead_providers: Iterable[str] | None, percent: int,
    lead_bounds: tuple[int, int, int | None] | None = None,
) -> WindowPolicy:
    """The policy a lineup with this lead class and ``lead_providers`` runs
    under: :func:`window_policy` over the offered lead-set lines of
    ``lcat`` (``effective`` None offers every active line), with the
    effective window ceiling. ``lead_bounds`` (the lead's own row) stands
    in when no offered line is in the set (the compile then refuses)."""

    rows: list[tuple[int, int, int | None]] = []
    for key, entry in lcat.lines.items():
        if not isinstance(entry, Mapping):
            continue
        provider_id = entry.get("provider")
        if not isinstance(provider_id, str) or provider_id not in lcat.providers:
            continue
        if effective is not None and not settings.line_offered(key, entry, effective):
            continue
        if not lead_set_member(entry, provider_id, lead_class, lead_providers):
            continue
        context = entry.get("context") or {}
        provider_tokens, client_tokens = context.get("provider_tokens"), context.get("client_tokens")
        scalar = context.get("scalar_tokens")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (provider_tokens, client_tokens)):
            continue
        rows.append((provider_tokens, client_tokens,
                     scalar if isinstance(scalar, int) and not isinstance(scalar, bool) else None))
    if not rows and lead_bounds is not None:
        rows.append(lead_bounds)
    return window_policy(rows, percent, ceiling=window_ceiling(effective))


def agent_selectors(entry: Mapping[str, Any], provider: Mapping[str, Any]) -> tuple[str, ...]:
    """Every agent-purpose line selector of a line (one per declared effort
    on a gateway-effort line, one on a client-effort line), in effort order."""

    if _line_mode(entry, provider) == "client":
        return (binding_selector(entry, provider, str(entry.get("default_effort")), lead=False)[0],)
    return tuple(dict.fromkeys(
        binding_selector(entry, provider, effort, lead=False)[0] for effort in declared_efforts(entry)))


def line_agent_window_problems(entry: Mapping[str, Any], provider: Mapping[str, Any], *,
                               policy: WindowPolicy | None = None) -> tuple[str, ...]:
    """Every agent selector whose decided class compacts above the provider
    bound under ``policy``, with the shared :func:`agent_window_problem`
    warning text (including a line not recommended for agents)."""

    context = entry.get("context") or {}
    bound = int(context.get("provider_tokens", 0))
    problems = []
    for selector in agent_selectors(entry, provider):
        problem = agent_window_problem(selector, bound, policy=policy,
                                       client_tokens=context.get("client_tokens"))
        if problem is not None:
            problems.append(f"{selector}: {problem}")
    return tuple(problems)


def keep_recorded_agent_class(
    lineup: "ResolvedLineup", recorded: Mapping[str, Any], lcat: "LineupCatalog | None" = None,
) -> "ResolvedLineup":
    """Record authority for an agent's context class.

    An agent slot whose (key, effort) equals the record's binding and whose
    recorded selector differs from the installed one only by the ``[1m]``
    suffix (the class moved: 200K -> 1M or 1M -> 200K) keeps the recorded
    selector and class until resume: no "moved" refusal, no live rewrite of
    its agent file, no fence gap. A new or changed slot takes the installed
    class. ``recorded`` is a record's ``applied.agents``. With ``lcat`` the
    warnings are recomputed for the kept classes."""

    import dataclasses

    agents = dict(lineup.agents)
    kept = False
    for rid, agent in lineup.agents.items():
        old = recorded.get(rid) if isinstance(recorded, Mapping) else None
        if not isinstance(old, Mapping):
            continue
        binding = agent.binding
        selector = old.get("selector")
        if (old.get("key"), old.get("effort")) != (binding.key, binding.effort):
            continue
        if not isinstance(selector, str) or selector == binding.selector:
            continue
        if selector.removesuffix(SUFFIX_1M) != binding.selector.removesuffix(SUFFIX_1M):
            continue
        agents[rid] = dataclasses.replace(agent, binding=dataclasses.replace(
            binding, selector=selector, client_context_tokens=agent_class_window(selector)))
        kept = True
    if not kept:
        return lineup
    changed = dataclasses.replace(lineup, agents=types.MappingProxyType(agents))
    if lcat is None:
        return changed
    old_derived = _warnings(lineup.lead, lineup.agents, lineup.routing, lcat, lineup.primary_provider, lineup.policy)
    other = tuple(f for f in lineup.warnings if f not in old_derived)
    return dataclasses.replace(changed, warnings=_warnings(
        changed.lead, changed.agents, changed.routing, lcat, changed.primary_provider, changed.policy) + other)


def lineup_windows(lineup: "ResolvedLineup") -> tuple[tuple[str, RoleWindow], ...]:
    """``(slot, window)`` of the lead and every bound agent, in lineup
    order, under the lineup's policy (each binding read as bound). The
    display data of the per-role windows."""

    policy = lineup.policy or default_policy(percent=settings.COMPACTION_PERCENT_DEFAULT)
    rows = [(LEAD_ROLE, lineup.lead.binding)] + [(rid, agent.binding) for rid, agent in lineup.agents.items()]
    return tuple(
        (slot, role_window(binding.selector, client_tokens=binding.client_context_tokens,
                           provider_tokens=binding.provider_context_tokens, policy=policy, decide=False))
        for slot, binding in rows
    )


def _line_mode(entry: Mapping[str, Any], provider: Mapping[str, Any]) -> str:
    # Custom synthetic entries (custom.synthetic_entries, ``family: custom``)
    # are client-effort by construction whatever their provider's adapter.
    # An aggregator's catalog line also declares a family and keeps the
    # effort mode of its shape.
    if catalog.is_legacy_custom_entry(entry):
        return "client"
    # The entry's own effort shape decides, as
    # ``catalog.line_selectors`` does: a list is client-effort (operator
    # no-contract and migrated lines ride claude-compatible adapters). A
    # map on a client-effort adapter stays the malformed case it always was.
    if isinstance(entry.get("efforts"), (list, tuple)):
        return "client"
    return catalog.effort_mode(provider)


# Merged-docs metadata keys written by ``operator.merge_docs`` (literal here:
# profile never imports the operator layer; a test pins the equality).
OPERATOR_LINES_KEY = "operator-lines"
OPERATOR_SECRET_NAMES_KEY = "operator-secret-names"
OPERATOR_KNOWN_FAMILIES_KEY = "_operator_known_families"
# Mutable diagnostics never enter durable scope identity or launch fences.
TRANSIENT_WARNING_CODES = frozenset({
    "admission", "qualification", "exact-client", "tool-evidence", "operator-agent-attention", "context-risk",
})
# Current provider bounds can change while a running selector keeps its old
# class. Capacity predictions are displayed, never hashed into that scope.
MODEL_WARNING_CODES = TRANSIENT_WARNING_CODES | frozenset({
    "capability-recommendation", "role-recommendation", "lead-env-ignored", "family-unknown",
    "effort-unverified", "independence-unknown", "companion-grade", "explore-replacement-unbound",
})
LINE_ORIGINS = ("catalog", "legacy-custom", "operator", "operator-migrated")
OPERATOR_ORIGINS = frozenset({"operator", "operator-migrated"})


# ------------------------------------------------------------ agent gate
#
# One pure gate separates operator-agent routing errors from recommendations
# and optional diagnostics. Evaluation, workflow defaults, pickers, doctor and
# the fence builder share it through LineupCatalog.agent_gate. Inputs are
# explicit facts, never filesystem reads or evidence minted during selection.
AGENT_MODE_CURRENT = "current"  # fresh launch, resume, new or changed in-session slots
AGENT_MODE_RECORD = "record"  # doctor, converge, live apply of unchanged slots
AGENT_MODES = (AGENT_MODE_CURRENT, AGENT_MODE_RECORD)
AGENT_USE = "agent"
WORKFLOW_USE = "workflow"
UNKNOWN_FAMILY = "unknown"
CLASS_1M = 1_000_000
CLASS_STANDARD = 200_000
# The pinned client's compaction constants (``composition``, which profile
# never imports; a test pins the equality with ``compiler.reactive_trigger``).
_AUTO_COMPACT_OUTPUT_RESERVE = 20_000
_AUTO_COMPACT_REACTIVE_HEADROOM = 13_000
AGENT_ATTENTION_CONTRACT = (
    "operator agent {key}: qualification predates the current pins — "
    "re-qualify with claude-multi models qualify {key} --agents"
)
AGENT_ATTENTION_STALE = (
    "operator agent {key}: qualification is stale for the current pins; running binding retained — "
    "re-qualify before the next launch or resume"
)
AGENT_REFUSAL = "{prefix}agents.{slot}: {key} is not agent-eligible ({reason}) — {remedy}"


@dataclass(frozen=True)
class AgentFacts:
    """Current operator-state facts of one operator line (from the layer,
    ledger, Settings and evidence; ``operator.agent_fact_fields``)."""

    key: str
    provider: str
    admitted: bool  # the current admission predicate (digest-bound)
    route: str  # approved | keyless | catalog | unapproved | changed
    d60: bool  # retained compatibility fact; no separate agent-only route allowlist
    route_kind: str
    family: str
    t1_families: frozenset[str]
    evidence: str  # current | contract-stale | missing | failed | definition-stale
    evidence_gaps: tuple[str, ...] = ()
    tools_variants: frozenset[str] = frozenset()
    pool: bool = False  # a first-party pool line: optional exact-client diagnostics
    exact_client: str = "not-required"  # not-required | current | contract-stale | missing | failed | unavailable


@dataclass(frozen=True)
class AgentGate:
    """The mode and facts an evaluation applies to operator agent slots.

    ``recorded`` maps an agent slot to the (key, effort) a record proves
    was granted; in ``record`` mode an unchanged recorded slot keeps its
    authority (never revoked by mutable evidence) while a new or changed
    slot is evaluated as ``current``. ``workflow_default``
    is the recorded Settings workflow default (record mode)."""

    mode: str = AGENT_MODE_CURRENT
    facts: Mapping[str, AgentFacts] = field(default_factory=dict)
    recorded: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    workflow_default: tuple[str, str] | None = None

    def with_record(self, recorded: Mapping[str, tuple[str, str]],
                    workflow_default: tuple[str, str] | None = None) -> "AgentGate":
        return AgentGate(AGENT_MODE_RECORD, self.facts, dict(recorded), workflow_default)


@dataclass(frozen=True)
class AgentEligibility:
    eligible: bool
    reasons: tuple[str, ...]
    remedy: str
    attention: str | None
    evidence: str
    warnings: tuple[Finding, ...] = ()

    def refusal(self, *, key: str, slot: str, profile_name: str | None) -> str:
        """The refusal text for a slot whose line cannot be bound."""

        prefix = f"profile {profile_name}: " if profile_name else ""
        return AGENT_REFUSAL.format(prefix=prefix, slot=slot, key=key,
                                    reason=self.reasons[0] if self.reasons else "not eligible", remedy=self.remedy)


def agent_class_window(selector: str, compiled_scalar: int | None = None, *, ceiling: int | None = None) -> int:
    """The client context class of a selector as bound: 1M with ``[1m]``,
    else the compiled ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` when the scope
    emits it, else 200K; ``ceiling`` caps it."""

    window = CLASS_1M if selector.endswith(SUFFIX_1M) else (compiled_scalar or CLASS_STANDARD)
    return min(window, ceiling) if ceiling is not None else window


def agent_compaction_trigger(class_window: int, percent: int = settings.COMPACTION_PERCENT_MAX) -> int:
    """The pinned client's reactive trigger for ``class_window`` at
    ``percent`` (``compiler.reactive_trigger``'s rule, without its refusal
    of a window too small to hold the output reserve)."""

    prompt_budget = class_window - _AUTO_COMPACT_OUTPUT_RESERVE
    return min(prompt_budget * percent // 100, prompt_budget - _AUTO_COMPACT_REACTIVE_HEADROOM)


def agent_window_problem(selector: str, provider_tokens: int, *, policy: WindowPolicy | None = None,
                         client_tokens: int | None = None) -> str | None:
    """Why an agent's client window or compaction trigger exceeds its
    provider bound under ``policy`` (the default policy at the largest
    percent without one), or None. The class is decided by
    :func:`role_window`; ``client_tokens`` is the line's client class (by
    default the selector's). The text names the numbers."""

    policy = policy or default_policy()
    if not isinstance(client_tokens, int) or isinstance(client_tokens, bool):
        client_tokens = agent_class_window(selector)
    role = role_window(selector, client_tokens=client_tokens, provider_tokens=provider_tokens,
                       policy=policy, decide=True)
    if role.window <= provider_tokens and role.trigger <= provider_tokens:
        return None
    return _window_risk_text(role, provider_tokens, policy)


def _window_risk_text(role: RoleWindow, provider_tokens: int, policy: WindowPolicy) -> str:
    context = (f"its agent class {format_tokens(role.client_class)} ({role.client_class} tokens), "
               f"shared process window {policy.window}, effective window {role.window}, "
               f"compacts at {role.trigger} tokens ({policy.percent}%)")
    if role.trigger > provider_tokens:
        return f"{context}, above its provider bound {provider_tokens}"
    return f"{context}; the effective client window exceeds its provider bound {provider_tokens}"


def binding_warnings(entry: Mapping[str, Any], *, key: str, slot: str | None, effort: str,
                     family: str, known_families: Iterable[str], admitted: bool = True) -> tuple[Finding, ...]:
    """Recommendations shared by explicit lead, agent and workflow bindings."""

    found: list[Finding] = []

    def warn(code: str, message: str) -> None:
        found.append(Finding(code, slot, f"{key}: {message}", f"{key}: {message}"))

    lead_use = slot == LEAD_ROLE
    capability = "lead" if lead_use else "agents"
    if capability not in (entry.get("capabilities") or ()):
        warn("capability-recommendation", f'explicit binding overrides the missing "{capability}" recommendation')
    roles = entry.get("roles")
    if not lead_use and slot is not None and roles != "all" and slot not in (roles or ()):
        warn("role-recommendation", f"explicit binding overrides the recommendation against {slot}")
    lead = entry.get("lead")
    if not lead_use and isinstance(lead, Mapping) and lead.get("env"):
        warn("lead-env-ignored", "its lead-only environment is not applied to an agent")
    if not recognized_family(family, known_families):
        warn("family-unknown", f"family {family!r}: review independence unknown")
    if not admitted:
        warn("admission", "not admitted with its current definition (optional attestation)")
    found.extend(effort_warnings(entry, effort, slot=slot))
    return tuple(found)


def qualification_warnings(key: str, facts: AgentFacts | None, *, slot: str | None,
                           use: str = AGENT_USE) -> tuple[Finding, ...]:
    """Optional operator attestations; absence never grants or denies a route."""

    found: list[Finding] = []

    def warn(code: str, message: str) -> None:
        found.append(Finding(code, slot, f"{key}: {message}", f"{key}: {message}"))

    if facts is None or not facts.admitted:
        warn("admission", "not admitted with its current definition (optional attestation)")
    evidence = facts.evidence if facts is not None else "missing"
    gaps = ", ".join(facts.evidence_gaps) if facts is not None and facts.evidence_gaps else "agent"
    if evidence == "missing":
        warn("qualification", f"not qualified: no current {gaps} evidence")
    elif evidence == "failed":
        warn("qualification", f"failed {gaps} qualification")
    elif evidence == "definition-stale":
        warn("qualification", "qualification is stale for the current definition")
    elif evidence == "contract-stale":
        warn("qualification", "qualification predates the current pins (stale evidence)")
    elif evidence != "current":
        warn("qualification", f"qualification {evidence}")
    if use == WORKFLOW_USE and (facts is None or "forced" not in facts.tools_variants):
        warn("tool-evidence", "no passing forced named-tool evidence for the workflow default")
    if facts is not None and facts.pool and facts.exact_client != "current":
        warn("exact-client", f"exact-client check {facts.exact_client}")
    return tuple(found)


def agent_eligibility(
    entry: Mapping[str, Any], *, key: str, slot: str | None, effort: str,
    facts: AgentFacts | None, agent_efforts: Sequence[str], mode: str = AGENT_MODE_CURRENT,
    recorded: bool = False, use: str = AGENT_USE, policy: WindowPolicy | None = None,
    family: str | None = None, known_families: Iterable[str] | None = None,
) -> AgentEligibility:
    """Hard routing/effort problems and advisory findings for an operator agent.

    Evidence never authorizes a route or changes a recorded selector. Both
    modes produce the same deterministic recommendations; callers preserve
    recorded classes and launch fences independently of these diagnostics.
    """

    if mode not in AGENT_MODES:
        raise ValueError(f"unknown agent eligibility mode {mode!r}")
    reasons: list[str] = []
    remedies: list[str] = []
    warnings: list[Finding] = []

    def warn(code: str, message: str) -> None:
        warnings.append(Finding(code, slot, f"{key}: {message}", f"{key}: {message}"))

    if "agents" not in (entry.get("capabilities") or ()):
        warn("capability-recommendation", 'explicit binding overrides the missing "agents" recommendation')
    roles = entry.get("roles")
    if use == AGENT_USE and slot is not None and roles != "all" and slot not in (roles or ()):
        warn("role-recommendation", f"explicit binding overrides the recommendation against {slot}")
    allowed = available_efforts(entry, agent_efforts, workflow=use == WORKFLOW_USE)
    if effort not in allowed:
        reasons.append(f"effort {effort!r} is not a representable native agent effort")
        remedies.append("bind a supported effort with an existing selector mapping")
    warnings.extend(effort_warnings(entry, effort, slot=slot) if effort in allowed else ())
    lead = entry.get("lead")
    if isinstance(lead, Mapping) and lead.get("env"):
        warn("lead-env-ignored", "its lead-only environment is not applied to an agent")
    if (facts is not None and facts.route not in ("approved", "keyless", "catalog")
            and not (mode == AGENT_MODE_RECORD and recorded)):
        reasons.append(f"the route of provider {facts.provider} is {facts.route}")
        remedies.append(f"claude-multi providers approve {facts.provider}")
    family = family if family is not None else str(entry.get("family", facts.family if facts else UNKNOWN_FAMILY))
    known = known_families if known_families is not None else (facts.t1_families if facts else ())
    if not recognized_family(family, known):
        warn("family-unknown", f"family {family!r}: review independence unknown")
    context = entry.get("context") or {}
    selector = binding_agent_selector(entry, effort)
    if not selector:
        reasons.append("no selector for the requested effort")
        remedies.append("bind an effort with an existing selector mapping")
    else:
        window = agent_window_problem(selector, int(context.get("provider_tokens", 0)), policy=policy,
                                      client_tokens=context.get("client_tokens"))
        if window is not None:
            warn("context-risk", window)
    warnings.extend(qualification_warnings(key, facts, slot=slot, use=use))
    evidence = facts.evidence if facts is not None else "missing"
    attention = warnings[0].message if warnings else None
    return AgentEligibility(not reasons, tuple(reasons), remedies[0] if remedies else "", attention,
                            evidence, tuple(warnings))


def binding_agent_selector(entry: Mapping[str, Any], effort: str) -> str:
    """The line selector an agent binding of ``entry`` at ``effort`` starts
    from (the agent purpose of :func:`binding_selector`; list-shaped lines
    have one). :func:`role_window` decides its class."""

    efforts = entry.get("efforts")
    if isinstance(efforts, (list, tuple)):
        return str(entry.get("selector", ""))
    spec = efforts.get(effort) if isinstance(efforts, Mapping) else None
    return str(spec.get("selector", "")) if isinstance(spec, Mapping) else ""


# ------------------------------------------------------------ LineupCatalog


@dataclass(frozen=True)
class LineupCatalog:
    """The catalog facts profile evaluation needs."""

    lines: Mapping[str, dict]  # raw v2 models, status new included
    retired: Mapping[str, dict]
    providers: Mapping[str, dict]
    roles: Mapping[str, dict]  # roles v2
    agent_efforts: tuple[str, ...]
    lead_efforts: tuple[str, ...]
    catalog_version: int
    # Explicit operator-line metadata (origin, legacy key)
    # and the conservative T2 secret-name scrub set, from the merged view.
    operator: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    extra_secret_names: frozenset[str] = frozenset()
    # Operator diagnostics and route facts (None: qualification unknown,
    # not a denial; callers carry current route availability in Effective).
    agent_gate: AgentGate | None = None
    known_families: frozenset[str] = frozenset()

    def with_gate(self, gate: AgentGate | None) -> "LineupCatalog":
        import dataclasses

        return dataclasses.replace(self, agent_gate=gate)

    def line_family(self, key: str, entry: Mapping[str, Any]) -> str:
        """The independence family of a line: an operator line's declared
        (or aggregator ``unknown``) family from the merged metadata, else
        the catalog rule."""

        meta = self.operator.get(key)
        if isinstance(meta, Mapping) and isinstance(meta.get("family"), str):
            return str(meta["family"])
        return catalog.line_family(entry, self.providers)

    def origin(self, key: str) -> str:
        """The line's origin: catalog, legacy-custom, operator or operator-migrated."""

        meta = self.operator.get(key)
        if isinstance(meta, Mapping) and meta.get("origin") in LINE_ORIGINS:
            return str(meta["origin"])
        entry = self.lines.get(key)
        return "legacy-custom" if isinstance(entry, Mapping) and catalog.is_legacy_custom_entry(entry) else "catalog"

    @classmethod
    def from_docs(cls, docs: Mapping[str, Any], *, agent_gate: AgentGate | None = None) -> "LineupCatalog":
        """From raw ``load_raw`` docs, ``Catalog.docs`` or a ``custom.merge_docs`` result.

        ``models-v2``/``roles-v2`` are read when present, else ``models``/``roles``.
        The v1 views are gone: a loaded or merged docs map carries the raw v2
        documents under both keys (the ``-v2`` keys stay aliases), and raw
        ``load_raw`` docs carry them under the plain keys.
        """

        # Conditional, not ``docs.get(key, docs[...])``: the default would be
        # evaluated eagerly and raise on a map without the v1 views.
        models = docs["models-v2"] if "models-v2" in docs else docs["models"]
        roles = docs["roles-v2"] if "roles-v2" in docs else docs["roles"]
        contract = docs["native-contract"]
        # Merged operator docs carry this set from the unmerged trusted input.
        # Without metadata this is a raw catalog (or legacy-custom merge).
        known = docs.get(OPERATOR_KNOWN_FAMILIES_KEY)
        if known is None:
            known = [provider.get("independence_family", "")
                     for provider in docs["providers"]["providers"].values()]
            known += [entry.get("family", "") for entry in models["models"].values()
                      if not catalog.is_legacy_custom_entry(entry)]
        return cls(
            lines=models["models"],
            retired=docs.get("retired", {"retired": {}})["retired"],
            providers=docs["providers"]["providers"],
            roles=roles["roles"],
            agent_efforts=tuple(contract["agent_efforts"]["values"]),
            lead_efforts=tuple(contract["lead_efforts"]["values"]),
            catalog_version=docs["version"]["catalog_version"],
            operator=dict(docs.get(OPERATOR_LINES_KEY) or {}),
            extra_secret_names=frozenset(docs.get(OPERATOR_SECRET_NAMES_KEY) or ()),
            agent_gate=agent_gate,
            known_families=frozenset(
                family.strip().casefold() for family in known
                if isinstance(family, str) and family.strip().casefold() not in ("", UNKNOWN_FAMILY, "custom")
            ),
        )

    @classmethod
    def from_catalog(cls, cat: "catalog.Catalog | LineupCatalog") -> "LineupCatalog":
        if isinstance(cat, LineupCatalog):
            return cat
        return cls.from_docs(cat.docs)

    def resolve_key(self, key: str) -> catalog.KeyResolution:
        if key not in self.lines and key not in self.retired:
            # A record or profile keeps its legacy
            # custom.json key; after migrate-custom it resolves through the
            # migrated line's explicit legacy_key, deterministically and with
            # no new state (records are never rewritten).
            for migrated in sorted(self.operator):
                meta = self.operator[migrated]
                if (isinstance(meta, Mapping) and meta.get("origin") == "operator-migrated"
                        and meta.get("legacy_key") == key and migrated in self.lines):
                    return catalog.KeyResolution(
                        requested=key, key=migrated, chain=(),
                        notice=f"{key} was migrated from custom.json -> {migrated}",
                    )
        return catalog.resolve_key_in(self.lines, self.retired, key)


# ------------------------------------------------------------- result types


@dataclass(frozen=True)
class RoleSpec:
    """One roles v2 entry."""

    id: str
    function: str
    grade: str
    prompt_file: str
    isolation: str | None
    disallowed_tools: tuple[str, ...]
    description: str
    requires: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedBinding:
    slot: str  # "cm-lead" or an agent id
    requested: str  # model key as written (profile or named binding)
    key: str  # live line key after the retired map
    chain: tuple[str, ...]  # KeyResolution.chain
    named: str | None  # named binding when the slot said {"use": ...}
    provider: str
    family: str  # catalog.line_family
    display: str
    generation: str
    mode: str  # "client" | "gateway"
    effort: str  # as bound; "ultracode" only on the lead
    selector: str
    proxy_contract: str | None  # gateway lines only
    default_effort: str
    client_context_tokens: int
    provider_context_tokens: int


@dataclass(frozen=True)
class ResolvedLead:
    binding: ResolvedBinding
    session_effort: str  # "xhigh" for ultracode with workflows off, else effort
    comparison_effort: str  # default_effort for ultracode, else effort (S §1.2)
    env: Mapping[str, str]
    lead_class: str  # context.ordinary_profile


@dataclass(frozen=True)
class ResolvedAgent:
    binding: ResolvedBinding
    role: RoleSpec
    # False iff the agent is on the lead's line at the same or lower effort
    # than the lead's comparison effort (the lead-prompt "-strong adds
    # context, not capability" rule, exposed as data; never a warning).
    lead_step: bool


@dataclass(frozen=True)
class Finding:
    code: str
    slot: str | None  # agent id, "cm-lead", or None (profile-wide)
    message: str  # card text, without the UI's "! " prefix
    compact: str  # editor "Checks" text


@dataclass(frozen=True)
class RouteCell:
    reviewer: str | None  # "cm-reviewer" | "cm-reviewer-strong" | None
    same_family: bool
    only_other_family: bool  # the preferred reviewer is bound but excluded by family
    reason: str | None  # "its check" | "no reviewer bound" | None
    independence_unknown: bool = False


@dataclass(frozen=True)
class RouteRow:
    author: str
    family: str
    normal: RouteCell
    high: RouteCell


@dataclass(frozen=True)
class Routing:
    rows: tuple[RouteRow, ...]  # bound writer grades in AUTHOR_ORDER, then the lead
    same_family_authors: tuple[str, ...]
    single_family: bool
    independence_unknown: bool = False
    independence_unknown_authors: tuple[str, ...] = ()

    @property
    def authors(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class ResolvedLineup:
    name: str | None  # None for ad-hoc direct
    description: str
    is_direct: bool  # the document's "agents" == {}
    lead: ResolvedLead
    agents: Mapping[str, ResolvedAgent]  # bound agents, AGENT_ROLE_IDS order
    unbound: tuple[str, ...]  # AGENT_ROLE_IDS not in agents
    native_agents: Mapping[str, str]
    workflows: str
    lead_providers: tuple[str, ...] | None
    primary_provider: str | None
    settings_overrides: Mapping[str, Any]
    routing: Routing
    spread: tuple[tuple[str, int], ...]  # agents per provider, count desc then id
    warnings: tuple[Finding, ...]
    notices: tuple[Finding, ...]
    # The window/percent policy the agents' classes were decided under
    # (:func:`session_policy`); the compile asserts it is the one it emits.
    policy: WindowPolicy | None = None

    @property
    def lead_class(self) -> str:
        return self.lead.lead_class

    @property
    def outsiders(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.notices if finding.code == "outsider")

    @property
    def named_bindings(self) -> tuple[str, ...]:
        names = {self.lead.binding.named} | {
            agent.binding.named for agent in self.agents.values()
        }
        return tuple(sorted(name for name in names if name is not None))

    def spread_text(self) -> str:
        """``"openai 6 · anthropic 2 (+lead)"`` (TUI §1)."""

        lead_provider = self.lead.binding.provider
        parts = []
        seen_lead = False
        for provider, count in self.spread:
            if provider == lead_provider:
                parts.append(f"{provider} {count} (+lead)")
                seen_lead = True
            else:
                parts.append(f"{provider} {count}")
        if not seen_lead:
            parts.append(f"{lead_provider} 0 (+lead)")
        return " · ".join(parts)

    def applied_bindings(self) -> dict[str, Any]:
        """What a record stores as the applied lineup."""

        def one(binding: ResolvedBinding) -> dict[str, Any]:
            return {
                "effort": binding.effort,
                "generation": binding.generation,
                "key": binding.key,
                "selector": binding.selector,
            }

        return {
            "agents": {rid: one(agent.binding) for rid, agent in self.agents.items()},
            "lead": one(self.lead.binding),
        }

    def relaunch_fields(self) -> dict[str, Any]:
        """The relaunch comparison input."""

        return {
            "lead_class": self.lead_class,
            "lead_providers": None if self.lead_providers is None else list(self.lead_providers),
            "native_agents": dict(self.native_agents),
            "settings_overrides": copy.deepcopy(dict(self.settings_overrides)),
            "workflows": self.workflows,
        }


@dataclass(frozen=True)
class Evaluation:
    errors: tuple[str, ...]
    lineup: ResolvedLineup | None  # None iff errors


# ---------------------------------------------------------------- functions


def _e1(version: Any) -> str:
    return (
        f"version: unsupported profile version {version!r} "
        "(profiles are v2; legacy compositions migrate with 'claude-multi profile migrate')"
    )


def _e2(path: str) -> str:
    return f'{path}: a binding is {{"model", "effort"}} or {{"use"}}'


def _slots(document: Mapping[str, Any]) -> list[tuple[str, str, Any]]:
    """``(slot id, field path, binding)``: the lead, then agents in AGENT_ROLE_IDS order."""

    slots: list[tuple[str, str, Any]] = [(LEAD_ROLE, "lead", document.get("lead"))]
    agents = document.get("agents")
    if isinstance(agents, Mapping):
        slots += [(rid, f"agents.{rid}", agents[rid]) for rid in AGENT_ROLE_IDS if rid in agents]
    return slots


def parse(document: Any) -> dict[str, Any]:
    """Deep copy + version + schema + binding shape; raises ProfileValidationError.

    Order: not an object → ``profile: expected an object``; version != 2 →
    E1 (before the schema); the schema problems verbatim; then E2 per
    binding (lead, then agents in AGENT_ROLE_IDS order).
    """

    if not isinstance(document, dict):
        raise ProfileValidationError(["profile: expected an object"])
    doc = copy.deepcopy(document)
    if doc.get("version") != PROFILE_DATA_VERSION or isinstance(doc.get("version"), bool):
        raise ProfileValidationError([_e1(doc.get("version"))])
    problems = schema_validate.validate(doc, _PROFILE_SCHEMA, "$")
    if problems:
        raise ProfileValidationError(problems)
    shape_errors = [
        _e2(path)
        for _slot, path, binding in _slots(doc)
        if frozenset(binding) not in _BINDING_SHAPES
    ]
    if shape_errors:
        raise ProfileValidationError(shape_errors)
    return doc


def load_profile_file(path: Path | str) -> dict[str, Any]:
    """Strict-load and :func:`parse` a profile file (``--profile-file``)."""

    try:
        document = strict_json.load(Path(path))
    except strict_json.StrictJSONError as exc:
        raise ProfileError(f"cannot load profile file {path}: {exc}") from exc
    return parse(document)


def referenced_bindings(document: Mapping[str, Any]) -> dict[str, str]:
    """``{"lead" | "agents.<id>": name}`` for every slot that uses a named binding."""

    return {
        path: binding["use"]
        for _slot, path, binding in _slots(document)
        if isinstance(binding, Mapping) and isinstance(binding.get("use"), str)
    }


def ad_hoc_direct(model: str, effort: str = ULTRACODE) -> dict[str, Any]:
    """The profile document of an ad-hoc direct session (lead only, no agents)."""

    return {
        "version": PROFILE_DATA_VERSION,
        "name": "ad-hoc",
        "description": "",
        "lead": {"effort": effort, "model": model},
        "agents": {},
        "native_agents": {"explore": "native", "general_purpose": "on", "plan": "native"},
        "workflows": "native",
    }


# -------------------------------------------------------------- evaluation


@dataclass
class _Slot:
    slot: str
    path: str
    named: str | None
    requested: str | None = None
    resolution: catalog.KeyResolution | None = None
    binding: ResolvedBinding | None = None
    unbound: bool = False  # an agent unbound by a retired key with no successor
    entry: Mapping[str, Any] | None = None
    provider: str | None = None  # the line's provider once the entry is usable
    warnings: tuple[Finding, ...] = ()


def _entry_problem(entry: Any, provider: Any, mode_hint: str | None) -> str | None:
    """Why a line entry cannot be evaluated (malformed catalog data), or None."""

    if not isinstance(entry, Mapping):
        return "not an object"
    if not isinstance(provider, Mapping) or not isinstance(provider.get("adapter"), str):
        return "its provider has no adapter"
    if "family" not in entry and not isinstance(provider.get("independence_family"), str):
        return "its provider has no independence_family"
    for field in ("display", "generation", "default_effort"):
        if not isinstance(entry.get(field), str):
            return f"missing {field}"
    if not isinstance(entry.get("capabilities"), (list, tuple)):
        return "missing capabilities"
    roles = entry.get("roles")
    if roles != "all" and not isinstance(roles, (list, tuple)):
        return "missing roles"
    context = entry.get("context")
    if not isinstance(context, Mapping):
        return "missing context"
    for field in ("client_tokens", "provider_tokens"):
        value = context.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            return f"missing context.{field}"
    efforts = entry.get("efforts")
    mode = mode_hint
    if mode == "client":
        if not isinstance(efforts, (list, tuple)):
            return "client-effort line without an efforts list"
        if not isinstance(entry.get("selector"), str):
            return "client-effort line without a selector"
    else:
        if not isinstance(efforts, Mapping):
            return "gateway-effort line without an efforts map"
        for level, spec in efforts.items():
            if not (
                isinstance(spec, Mapping)
                and isinstance(spec.get("selector"), str)
                and "proxy_contract" in spec
            ):
                return f"efforts.{level} lacks selector/proxy_contract"
    if entry["default_effort"] not in declared_efforts(entry):
        return "default_effort is not declared"
    return None


def _role_spec(rid: str, role: Any) -> RoleSpec | None:
    if not isinstance(role, Mapping):
        return None
    try:
        return RoleSpec(
            id=rid,
            function=role["function"],
            grade=role["grade"],
            prompt_file=role["prompt_file"],
            isolation=role["isolation"],
            disallowed_tools=tuple(role["disallowed_tools"]),
            description=role["description"],
            requires=tuple(role["requires"]),
        )
    except (KeyError, TypeError):
        return None


def evaluate(
    document: Any,
    cat: "LineupCatalog | catalog.Catalog",
    *,
    bindings: Mapping[str, Mapping[str, Any]] | None = None,
    effective: "settings.Effective | None" = None,
    ad_hoc: bool = False,
) -> Evaluation:
    """Validate ``document`` and resolve it; never raises for content problems.

    ``bindings`` is the inner ``"bindings"`` map of ``bindings.json`` (None:
    no named bindings exist). ``effective`` None enables every provider
    and has no admission badges; missing badges warn rather than deny use.
    Disabled providers and unavailable routes still refuse. Errors are
    collected in slot order, never short-circuited except after
    :func:`parse`. With ``ad_hoc`` the lineup name is None.

    The lead resolves first; its lead class, ``lead_providers`` and the
    effective window ceiling give the session's window/percent policy
    (:func:`session_policy`), and every agent's context class is decided
    under it (:func:`role_window`). ``ResolvedLineup.policy`` carries it.
    """

    try:
        doc = parse(document)
    except ProfileValidationError as exc:
        return Evaluation(exc.errors, None)
    lcat = LineupCatalog.from_catalog(cat)
    errors: list[str] = []
    slots: list[_Slot] = []

    profile_name = None if ad_hoc else doc.get("name")
    policy = default_policy(effective, percent=_document_percent(doc, effective))
    for slot_id, path, binding in _slots(doc):
        slot = _Slot(slot=slot_id, path=path, named=binding.get("use"))
        slots.append(slot)
        _resolve_slot(slot, binding, lcat, bindings, effective, ad_hoc, errors, profile_name=profile_name,
                      policy=policy)
        if slot_id == LEAD_ROLE:
            policy = _lineup_policy(doc, lcat, effective, slot, policy)

    lead = slots[0]
    agent_slots = slots[1:]
    bound_ids = [s.slot for s in agent_slots]
    unbound_by_retirement = {s.slot: s for s in agent_slots if s.unbound}
    effective_ids = [rid for rid in bound_ids if rid not in unbound_by_retirement]

    # Role identities and prompt/isolation data are structural, companions advisory.
    roles = lcat.roles
    for rid in effective_ids:
        if not isinstance(roles.get(rid), Mapping):
            errors.append(f"agents.{rid}: role {rid!r} is not in the catalog roles")

    # Profile fields.
    providers = lcat.providers
    lead_providers = doc.get("lead_providers")
    if lead_providers is not None:
        for provider_id in lead_providers:
            if provider_id not in providers:
                errors.append(f"lead_providers: unknown provider {provider_id!r}")
        if lead.provider is not None and lead.provider not in lead_providers:
            errors.append(
                f"lead_providers: must include the lead's provider {lead.provider!r}"
            )
    primary = doc.get("primary_provider")
    if primary is not None and primary not in providers:
        errors.append(f"primary_provider: unknown provider {primary!r}")
    overrides = doc.get("settings_overrides", {})
    for key in sorted(overrides):
        value = overrides[key]
        if key not in OVERRIDABLE_SETTINGS:
            errors.append(f"settings_overrides.{key}: not overridable per profile")
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"settings_overrides.{key}: must be an integer")
            continue
        low, high = OVERRIDABLE_SETTINGS[key]
        if not low <= value <= high:
            errors.append(f"settings_overrides.{key}: {value} is outside {low}–{high}")

    if errors or lead.binding is None:
        return Evaluation(tuple(errors) or ("lead: not resolved",), None)

    # Build.
    role_specs: dict[str, RoleSpec] = {}
    for rid in effective_ids:
        spec = _role_spec(rid, roles.get(rid))
        if spec is None:
            errors.append(f"agents.{rid}: role {rid!r} is malformed in the catalog roles")
        else:
            role_specs[rid] = spec
    if errors:
        return Evaluation(tuple(errors), None)
    return Evaluation((), _build(doc, lcat, lead, agent_slots, role_specs, ad_hoc, policy))


def _document_percent(doc: Mapping[str, Any], effective: "settings.Effective | None") -> int:
    """The compaction percent of a parsed document: its override, else
    Settings (an invalid override reads as Settings; evaluation reports it)."""

    base = effective if effective is not None else settings.Effective({}, frozenset(), ())
    try:
        return settings.compaction_percent_for(base, doc.get("settings_overrides") or {})
    except settings.SettingsError:
        return base.compaction_percent


def _lineup_policy(doc: Mapping[str, Any], lcat: LineupCatalog, effective: "settings.Effective | None",
                   lead: "_Slot", fallback: WindowPolicy) -> WindowPolicy:
    """The session policy once the lead slot resolved (``fallback`` when it
    did not: the evaluation then fails)."""

    entry = lead.entry
    if lead.binding is None or not isinstance(entry, Mapping):
        return fallback
    context = entry["context"]
    scalar = context.get("scalar_tokens")
    lead_providers = doc.get("lead_providers")
    return session_policy(
        lcat, effective, lead_class=context.get("ordinary_profile"),
        lead_providers=tuple(lead_providers) if lead_providers else None, percent=fallback.percent,
        lead_bounds=(context["provider_tokens"], context["client_tokens"],
                     scalar if isinstance(scalar, int) and not isinstance(scalar, bool) else None),
    )


def _resolve_slot(
    slot: _Slot,
    binding: Mapping[str, Any],
    lcat: LineupCatalog,
    bindings: Mapping[str, Mapping[str, Any]] | None,
    effective: "settings.Effective | None",
    ad_hoc: bool,
    errors: list[str],
    *,
    profile_name: str | None = None,
    policy: WindowPolicy | None = None,
) -> None:
    """Resolve one slot (§6.5 step 2), appending its errors. An agent's
    context class is decided under ``policy`` (the session's window/percent
    policy; the default policy without one)."""

    is_lead = slot.slot == LEAD_ROLE
    path = slot.path

    def err(field: str, reason: str) -> None:
        if slot.named is not None:
            errors.append(f"{path}.use: named binding {slot.named!r}: {reason}")
        else:
            errors.append(f"{path}.{field}: {reason}")

    # 2.1 named binding
    if slot.named is not None:
        name = slot.named
        if bindings is None or name not in bindings:
            errors.append(f"{path}.use: unknown named binding {name!r}")
            return
        named = bindings[name]
        if not (
            isinstance(named, Mapping)
            and isinstance(named.get("model"), str)
            and isinstance(named.get("effort"), str)
        ):
            errors.append(f"{path}.use: named binding {name!r} is malformed")
            return
        if not is_lead and named["effort"] == ULTRACODE:
            errors.append(f"{path}.use: named binding {name!r} carries lead-only effort 'ultracode'")
            return
        model, effort = named["model"], named["effort"]
    else:
        model, effort = binding["model"], binding["effort"]
    slot.requested = model
    start = len(errors)

    # 2.2 key
    try:
        resolution = lcat.resolve_key(model)
    except catalog.CatalogError:
        err("model", f"unknown model {model!r}")
        return
    except (KeyError, TypeError):  # a malformed retired map (invalid catalog)
        err("model", f"retired entry for {model!r} is malformed")
        return
    slot.resolution = resolution

    # 2.3 retired with no successor
    if resolution.key is None:
        if is_lead:
            err("model", str(resolution.notice))
        else:
            slot.unbound = True
        return
    key = resolution.key

    # 2.4 entry
    entry = lcat.lines.get(key)
    provider_id = entry.get("provider") if isinstance(entry, Mapping) else None
    provider = lcat.providers.get(provider_id) if isinstance(provider_id, str) else None
    if isinstance(entry, Mapping) and provider is None:
        err("model", f"line {key!r} names unknown provider {provider_id!r}")
        return
    mode_hint = None
    if isinstance(entry, Mapping) and isinstance(provider, Mapping) and isinstance(
        provider.get("adapter"), str
    ):
        mode_hint = _line_mode(entry, provider)
    problem = _entry_problem(entry, provider, mode_hint)
    if problem is not None:
        err("model", f"line {key!r} is malformed ({problem})")
        return
    assert isinstance(entry, Mapping) and isinstance(provider, Mapping)
    slot.entry = entry
    slot.provider = provider_id
    operator_line = lcat.origin(key) in OPERATOR_ORIGINS
    gate = lcat.agent_gate
    facts = gate.facts.get(key) if gate is not None and operator_line else None
    recorded = bool(operator_line and not is_lead and gate is not None and gate.mode == AGENT_MODE_RECORD
                    and gate.recorded.get(slot.slot) == (key, effort))
    family = lcat.line_family(key, entry)
    if effective is not None and not settings.provider_enabled(effective, provider_id):
        err("model", f"provider {provider_id!r} is disabled in Settings")
    unavailable = getattr(effective, "unavailable_lines", {}).get(key)
    if unavailable:
        err("model", unavailable)
    if facts is not None and facts.route not in ("approved", "keyless", "catalog") and not recorded:
        err("model", f"the route of provider {provider_id} is {facts.route} — "
                     f"claude-multi providers approve {provider_id}")
    if is_lead and (not isinstance(entry.get("lead"), Mapping)
                    or not isinstance(entry["context"].get("ordinary_profile"), str)):
        err("model", f"{key!r} lacks the lead/context fields required for compilation")
        return

    native = lcat.lead_efforts if is_lead else lcat.agent_efforts
    allowed = available_efforts(entry, native, lead=is_lead)
    if not is_lead and effort == ULTRACODE:
        err("effort", "'ultracode' is lead-only")
    elif effort not in allowed:
        err("effort", f"{effort!r} is not supported by {key!r} (available: {', '.join(allowed)})")
    if len(errors) != start:
        return

    if operator_line and not is_lead:
        verdict = agent_eligibility(
            entry, key=key, slot=slot.slot, effort=effort, facts=facts,
            agent_efforts=lcat.agent_efforts,
            mode=gate.mode if gate is not None else AGENT_MODE_CURRENT,
            recorded=recorded, policy=policy, family=family, known_families=lcat.known_families,
        )
        if not verdict.eligible:
            errors.append(verdict.refusal(key=key, slot=slot.slot, profile_name=profile_name))
            return
        # Context warnings are derived below from the actual bound selector,
        # including when record authority keeps its launch-time class.
        slot.warnings = tuple(w for w in verdict.warnings if w.code != "context-risk")
    else:
        admitted = operator_line or entry.get("status", "active") != "new" or (
            effective is not None and key in effective.admitted_lines)
        slot.warnings = binding_warnings(entry, key=key, slot=slot.slot, effort=effort,
                                         family=family, known_families=lcat.known_families, admitted=admitted)
        if operator_line:
            slot.warnings += qualification_warnings(key, facts, slot=slot.slot)

    # 2.8 selector; an agent's context class follows the session window.
    selector, contract = binding_selector(entry, provider, effort, lead=is_lead)
    context = entry["context"]
    client_tokens = context["client_tokens"]
    if not is_lead:
        role = role_window(selector, client_tokens=client_tokens, provider_tokens=context["provider_tokens"],
                           policy=policy or default_policy(effective), decide=True)
        selector, client_tokens = role.selector, role.client_class
    slot.binding = ResolvedBinding(
        slot=slot.slot,
        requested=model,
        key=key,
        chain=resolution.chain,
        named=slot.named,
        provider=provider_id,
        family=lcat.line_family(key, entry),
        display=entry["display"],
        generation=entry["generation"],
        mode=_line_mode(entry, provider),
        effort=effort,
        selector=selector,
        proxy_contract=contract,
        default_effort=entry["default_effort"],
        client_context_tokens=client_tokens,
        provider_context_tokens=context["provider_tokens"],
    )


def _notice_suffix(named: str | None) -> str:
    return "" if named is None else f" (named binding {named!r})"


def _build(
    doc: Mapping[str, Any],
    lcat: LineupCatalog,
    lead_slot: _Slot,
    agent_slots: list[_Slot],
    role_specs: Mapping[str, RoleSpec],
    ad_hoc: bool,
    policy: WindowPolicy | None = None,
) -> ResolvedLineup:
    lead_binding = lead_slot.binding
    assert lead_binding is not None and lead_slot.entry is not None
    workflows = doc.get("workflows", _DEFAULT_WORKFLOWS)
    effort = lead_binding.effort
    comparison = lead_binding.default_effort if effort == ULTRACODE else effort
    lead_entry = lead_slot.entry
    lead = ResolvedLead(
        binding=lead_binding,
        session_effort="xhigh" if effort == ULTRACODE and workflows == "off" else effort,
        comparison_effort=comparison,
        env=types.MappingProxyType(copy.deepcopy(dict(lead_entry["lead"].get("env") or {}))),
        lead_class=lead_entry["context"].get("ordinary_profile"),
    )

    agents: dict[str, ResolvedAgent] = {}
    for slot in agent_slots:
        if slot.unbound or slot.binding is None:
            continue
        binding = slot.binding
        agents[slot.slot] = ResolvedAgent(
            binding=binding,
            role=role_specs[slot.slot],
            lead_step=_lead_step(binding, lead),
        )
    unbound = tuple(rid for rid in AGENT_ROLE_IDS if rid not in agents)

    author_families = {LEAD_ROLE: lead_binding.family}
    author_families.update(
        {rid: agents[rid].binding.family for rid in WRITER_IDS if rid in agents}
    )
    reviewer_families = {
        rid: agents[rid].binding.family for rid in REVIEWER_IDS if rid in agents
    }
    routing = derive_routing(author_families, reviewer_families, lcat.known_families)

    counts: dict[str, int] = {}
    for agent in agents.values():
        counts[agent.binding.provider] = counts.get(agent.binding.provider, 0) + 1
    spread = tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    primary = doc.get("primary_provider")
    lead_providers = doc.get("lead_providers")
    warnings = _warnings(lead, agents, routing, lcat, primary, policy)
    warnings += tuple(w for slot in (lead_slot, *agent_slots) if slot.binding for w in slot.warnings)
    if doc["native_agents"]["explore"] == "replace" and "cm-explorer" not in agents:
        message = "Explore remains disabled: its configured replacement cm-explorer is unbound"
        warnings += (Finding("explore-replacement-unbound", None, message, message),)

    notices: list[Finding] = []
    for slot in [lead_slot, *agent_slots]:
        resolution = slot.resolution
        if resolution is None or resolution.notice is None:
            continue
        if slot.unbound:
            message = (
                f"{label(slot.slot)}: {slot.requested!r} was removed — unbound"
                f"{_notice_suffix(slot.named)}"
            )
            notices.append(Finding("retired-unbound", slot.slot, message, message))
        elif slot.binding is not None:
            message = f"{label(slot.slot)}: {resolution.notice}{_notice_suffix(slot.named)}"
            notices.append(Finding("retired-successor", slot.slot, message, message))
    if primary is not None:
        bound = [(LEAD_ROLE, lead_binding)] + [
            (rid, agent.binding) for rid, agent in agents.items()
        ]
        for rid, binding in bound:
            if binding.provider != primary:
                message = f"outsider: {label(rid)} → {binding.display} ({binding.provider})"
                notices.append(Finding("outsider", rid, message, message))

    return ResolvedLineup(
        name=None if ad_hoc else doc["name"],
        description=doc.get("description", ""),
        is_direct=doc["agents"] == {},
        lead=lead,
        agents=types.MappingProxyType(agents),
        unbound=unbound,
        native_agents=types.MappingProxyType(dict(doc["native_agents"])),
        workflows=workflows,
        lead_providers=tuple(lead_providers) if lead_providers else None,
        primary_provider=primary,
        settings_overrides=types.MappingProxyType(
            copy.deepcopy(dict(doc.get("settings_overrides", {})))
        ),
        routing=routing,
        spread=spread,
        warnings=warnings,
        notices=tuple(notices),
        policy=policy,
    )


def _lead_step(binding: ResolvedBinding, lead: ResolvedLead) -> bool:
    if binding.key != lead.binding.key:
        return True
    if binding.effort not in EFFORT_ORDER or lead.comparison_effort not in EFFORT_ORDER:
        return binding.effort != lead.comparison_effort
    return effort_rank(binding.effort) > effort_rank(lead.comparison_effort)


def _warnings(
    lead: ResolvedLead,
    agents: Mapping[str, ResolvedAgent],
    routing: Routing,
    lcat: LineupCatalog,
    primary: str | None,
    policy: WindowPolicy | None = None,
) -> tuple[Finding, ...]:
    """The lineup warnings (W1..W5) plus the strict-schema route warning (W6)."""

    found: list[Finding] = []
    lead_provider = lead.binding.provider

    # W1 same-family review
    if routing.same_family_authors:
        if routing.single_family:
            found.append(
                Finding(
                    "same-family-review",
                    None,
                    "review same-family (reduced independence)",
                    "review same-family",
                )
            )
        else:
            labels = ", ".join(label(author) for author in routing.same_family_authors)
            found.append(
                Finding(
                    "same-family-review",
                    None,
                    f"review same-family (reduced independence) for {labels}",
                    f"same-family review: {labels}",
                )
            )

    if routing.independence_unknown:
        names = ", ".join(label(author) for author in routing.independence_unknown_authors)
        message = f"review independence unknown for {names}"
        found.append(Finding("independence-unknown", None, message, message))

    for rid, agent in agents.items():
        for required in agent.role.requires:
            if required not in agents:
                message = f"{label(rid)}: recommended companion {required} is unbound"
                found.append(Finding("companion-grade", rid, message, message))
        binding = agent.binding
        active_policy = policy or default_policy(percent=settings.COMPACTION_PERCENT_DEFAULT)
        role = role_window(binding.selector, client_tokens=binding.client_context_tokens,
                           provider_tokens=binding.provider_context_tokens, policy=active_policy, decide=False)
        if role.window > binding.provider_context_tokens or role.trigger > binding.provider_context_tokens:
            message = f"{label(rid)}: {_window_risk_text(role, binding.provider_context_tokens, active_policy)}"
            found.append(Finding("context-risk", rid, message, f"{label(rid)}: context overflow risk"))

    # W2 lead + every -strong slot on one provider
    strong = [agents[rid] for rid in AGENT_ROLE_IDS if rid.endswith("-strong") and rid in agents]
    if (
        strong
        and all(agent.binding.provider == lead_provider for agent in strong)
        and lead_provider != primary
    ):
        found.append(
            Finding(
                "lead-strong-concentration",
                None,
                f"lead and every -strong slot are on {lead_provider}",
                f"lead+strong share {lead_provider}",
            )
        )

    # W3 -strong not stronger (same line, same or lower effort than plain)
    for strong_id, plain_id in STRONG_PAIRS:
        if strong_id not in agents or plain_id not in agents:
            continue
        s, p = agents[strong_id].binding, agents[plain_id].binding
        if s.key != p.key:
            continue
        if s.effort in EFFORT_ORDER and p.effort in EFFORT_ORDER:
            weaker = effort_rank(s.effort) <= effort_rank(p.effort)
        else:
            weaker = s.effort == p.effort
        if weaker:
            found.append(
                Finding(
                    "strong-not-stronger",
                    strong_id,
                    f"{label(strong_id)}: -strong is not stronger "
                    f"(same model as {label(plain_id)} at the same or lower effort)",
                    f"{label(strong_id)}: -strong is not stronger",
                )
            )

    # W4 fan-out quota (explorer and every bound writer on one metered provider)
    writers = [agents[rid] for rid in WRITER_IDS if rid in agents]
    if "cm-explorer" in agents and writers:
        fan = {agents["cm-explorer"].binding.provider} | {w.binding.provider for w in writers}
        if len(fan) == 1:
            (provider_id,) = fan
            provider = lcat.providers.get(provider_id, {})
            if is_metered(provider) and provider_id != primary:
                found.append(
                    Finding(
                        "fan-out-quota",
                        None,
                        f"explorer and every implementer are on {provider_id} "
                        f"({quota_label(provider)})",
                        f"explorer+implementers share {provider_id}",
                    )
                )

    # W5 context below the lead's class. W5 compares the model's own client
    # window (the line's ``client_tokens``); the 200K class of an agent whose
    # provider bound is below the session window is a deliberate compaction
    # class (its effective window is shown with the lineup), not W5.
    lead_tokens = lead.binding.client_context_tokens
    for rid in AGENT_ROLE_IDS:
        agent = agents.get(rid)
        if agent is None:
            continue
        line = lcat.lines.get(agent.binding.key)
        own = (line.get("context") or {}).get("client_tokens") if isinstance(line, Mapping) else None
        own = own if isinstance(own, int) and not isinstance(own, bool) else agent.binding.client_context_tokens
        if own >= lead_tokens:
            continue
        found.append(
            Finding(
                "context-below-lead-class",
                rid,
                f"{label(rid)}: context {format_tokens(own)} "
                f"is below the lead's {format_tokens(lead_tokens)} class (allowed)",
                f"{label(rid)}: context below lead class",
            )
        )

    # W6 strict tool-schema route: never an error.
    bound = [(LEAD_ROLE, lead.binding)] + [
        (rid, agents[rid].binding) for rid in AGENT_ROLE_IDS if rid in agents
    ]
    for provider_id in sorted(STRICT_TOOL_SCHEMA_PROVIDERS):
        slots = [label(rid) for rid, binding in bound if binding.provider == provider_id]
        if not slots:
            continue
        names = ", ".join(slots)
        found.append(
            Finding(
                "strict-tool-schema",
                None,
                f"{names} on {provider_id}: the route rejects tool schemas whose "
                "'required' omits a property (HTTP 400, e.g. hook evaluator calls)",
                f"{provider_id} strict tool schemas: {names}",
            )
        )
    return tuple(found)


def resolve(document: Any, cat: "LineupCatalog | catalog.Catalog", **kw: Any) -> ResolvedLineup:
    """:func:`evaluate`; raises :class:`ProfileValidationError` when it has errors."""

    result = evaluate(document, cat, **kw)
    if result.errors or result.lineup is None:
        raise ProfileValidationError(result.errors)
    return result.lineup


# ----------------------------------------------------------------- routing


def recognized_family(value: str, known_families: Iterable[str]) -> str | None:
    """Normalized trusted family identity, never certification by an operator."""

    normalized = value.strip().casefold()
    known = {family.strip().casefold() for family in known_families}
    return normalized if normalized and normalized != UNKNOWN_FAMILY and normalized in known else None


def independent_families(a: str, b: str, known_families: Iterable[str] = ()) -> bool:
    """Only two recognized, different families establish independence."""

    first, second = recognized_family(a, known_families), recognized_family(b, known_families)
    return first is not None and second is not None and first != second


def derive_routing(
    author_families: Mapping[str, str], reviewer_families: Mapping[str, str],
    known_families: Iterable[str] = (),
) -> Routing:
    """Review routing: a pure function of families.

    ``author_families`` holds ``cm-lead`` plus every bound writer grade;
    ``reviewer_families`` every bound reviewer id. Candidates are the
    reviewers of a recognized different family, or all bound reviewers
    when none is independent. A recognized equal pair is same-family;
    every unrecognized pair has unknown independence. The normal column
    prefers ``cm-reviewer-strong`` for the
    strong writer and the lead and ``cm-reviewer`` otherwise, the
    high-stakes column always prefers ``cm-reviewer-strong``.
    """

    known_families = frozenset(known_families)
    reviewers = {rid: reviewer_families[rid] for rid in REVIEWER_IDS if rid in reviewer_families}
    rows: list[RouteRow] = []
    for author in AUTHOR_ORDER:
        if author not in author_families:
            continue
        family = author_families[author]
        if not reviewers:
            none = RouteCell(None, False, False, "no reviewer bound")
            rows.append(RouteRow(author, family, none, none))
            continue
        # ``unknown`` is never evidence of independence.
        candidates = [rid for rid in reviewers if independent_families(reviewers[rid], family, known_families)]
        cross_family = bool(candidates)
        if not candidates:
            candidates = list(reviewers)

        def pick(preferred: str) -> RouteCell:
            if preferred in candidates:
                chosen: str | None = preferred
            else:
                chosen = next((rid for rid in candidates if rid != preferred), None)
            only_other = (
                cross_family
                and preferred in reviewers
                and preferred not in candidates
                and chosen is not None
            )
            first = recognized_family(family, known_families)
            second = recognized_family(reviewers[chosen], known_families) if chosen is not None else None
            unknown = chosen is not None and (first is None or second is None)
            same = chosen is not None and not unknown and first == second
            return RouteCell(chosen, same, only_other, None, unknown)

        if author == "cm-implementer-light":
            normal = RouteCell(None, False, False, "its check")
        elif author in ("cm-implementer-strong", LEAD_ROLE):
            normal = pick("cm-reviewer-strong")
        else:
            normal = pick("cm-reviewer")
        rows.append(RouteRow(author, family, normal, pick("cm-reviewer-strong")))
    same_family_authors = tuple(
        row.author for row in rows if row.normal.same_family or row.high.same_family
    )
    unknown_authors = tuple(row.author for row in rows
                            if row.normal.independence_unknown or row.high.independence_unknown)
    families = {recognized_family(family, known_families)
                for family in (*author_families.values(), *reviewers.values())}
    return Routing(
        rows=tuple(rows),
        same_family_authors=same_family_authors,
        single_family=bool(reviewers) and None not in families and len(families) == 1,
        independence_unknown=bool(unknown_authors),
        independence_unknown_authors=unknown_authors,
    )


# ================================================================== stores
#
# Shared contracts (both stores):
#   - construction and every read are side-effect-free (no directory is
#     created; unlike cli.CompositionStore, which runs on every Runtime);
#   - reads go through state.read_private (symlink/owner/mode refusing),
#     strict JSON, the version gate, then the schema;
#   - every mutator runs check_state_marker -> ensure_private_dir ->
#     one FileLock acquisition -> load/check/mutate -> the unlocked
#     private write; flock is per open file
#     description, so a method that re-acquired its own store lock would
#     deadlock itself;
#   - both locks are leaf locks: never take a record, lifecycle, pointer or
#     runtime-index lock while holding one, and never hold both. Readers
#     (the live apply) read the atomically replaced files without a lock.

BINDINGS_DATA_VERSION = 1


class BindingError(errors.ClaudeMultiError, ValueError):
    """Named bindings could not be loaded or a change was refused."""


def bindings_path(environ: Mapping[str, str] | None = None) -> Path:
    """``<config root>/bindings.json`` (XDG-aware, like compositions and custom.json)."""

    return sessions.config_root(None if environ is None else dict(environ)) / "bindings.json"


def profiles_dir(environ: Mapping[str, str] | None = None) -> Path:
    """``<config root>/profiles`` (XDG-aware)."""

    return sessions.config_root(None if environ is None else dict(environ)) / "profiles"


def _error_detail(exc: BaseException) -> str:
    """Operator-facing text of a wrapped error.

    A :class:`state.StateError` is an ``OSError`` built with an errno, so
    ``str()`` carries an ``[Errno N]`` prefix; its ``strerror`` is the whole
    message (path included). Anything else keeps ``str()``.
    """

    if isinstance(exc, state.StateError) and exc.strerror:
        return exc.strerror
    return str(exc)


def _changed_names(old: Mapping[str, Any], new: Mapping[str, Any]) -> set[str]:
    """Names added, removed or given a different value between two binding maps."""

    return {name for name in set(old) | set(new) if old.get(name) != new.get(name)}


def _empty_bindings() -> dict[str, Any]:
    return {"version": BINDINGS_DATA_VERSION, "bindings": {}}


def _bindings_problems(document: Any) -> list[str]:
    """Shape problems of a bindings document: object, version (before the schema), schema, names."""

    if not isinstance(document, dict):
        return ["bindings: expected an object"]
    version = document.get("version")
    if version != BINDINGS_DATA_VERSION or isinstance(version, bool):
        return [
            f"version: unsupported named-bindings version {version!r} "
            f"(this launcher reads version {BINDINGS_DATA_VERSION})"
        ]
    problems = schema_validate.validate(document, _BINDINGS_SCHEMA, "$")
    if problems:
        return problems
    return [
        f"bindings.{name}: name must match {BINDING_NAME.pattern}"
        for name in sorted(document["bindings"])
        if not BINDING_NAME.fullmatch(name)
    ]


def _retired_names(lcat: LineupCatalog) -> frozenset[str]:
    """Retired keys and the bases of ``<line>@<generation>`` keys."""

    retired = frozenset(lcat.retired)
    return retired | frozenset(key.split("@", 1)[0] for key in retired if "@" in key)


def _collision(name: str, lcat: LineupCatalog) -> str | None:
    if name in lcat.lines:
        return f"bindings.{name}: collides with catalog model key {name!r}"
    if name in _retired_names(lcat):
        return f"bindings.{name}: collides with retired catalog key {name!r}"
    return None


def _binding_errors(
    name: str,
    value: Mapping[str, Any],
    lcat: LineupCatalog,
    effective: "settings.Effective | None",
) -> list[str]:
    """B1-B7 for one stored name (the document already passed the schema)."""

    errors: list[str] = []
    if not BINDING_NAME.fullmatch(name):
        errors.append(f"bindings.{name}: name must match {BINDING_NAME.pattern}")
    collision = _collision(name, lcat)
    if collision is not None:
        errors.append(collision)
    model, effort = value["model"], value["effort"]
    try:
        resolution = lcat.resolve_key(model)
    except catalog.CatalogError:
        errors.append(f"bindings.{name}.model: unknown model {model!r}")
        return errors
    except (KeyError, TypeError):  # malformed retired map (invalid catalog)
        errors.append(f"bindings.{name}.model: retired entry for {model!r} is malformed")
        return errors
    if resolution.notice is not None:
        errors.append(f"bindings.{name}.model: {resolution.notice}; bind the live line")
        return errors
    entry = lcat.lines.get(model)
    if not isinstance(entry, Mapping):
        errors.append(f"bindings.{name}.model: line {model!r} is malformed")
        return errors
    provider_id = entry.get("provider")
    provider = lcat.providers.get(provider_id)
    if provider is None:
        errors.append(f"bindings.{name}.model: unknown provider {provider_id!r}")
        return errors
    problem = _entry_problem(entry, provider, _line_mode(entry, provider))
    if problem:
        errors.append(f"bindings.{name}.model: line {model!r} is malformed ({problem})")
        return errors
    # Saving metadata does not require enabling a provider. Operator routes
    # still need their existing safety approval; use checks every route again.
    operator_line = lcat.origin(model) in OPERATOR_ORIGINS
    unavailable = getattr(effective, "unavailable_lines", {}).get(model) if operator_line else None
    if unavailable:
        errors.append(f"bindings.{name}.model: {unavailable}")
    facts = lcat.agent_gate.facts.get(model) if lcat.agent_gate and operator_line else None
    if facts is not None and facts.route not in ("approved", "keyless", "catalog"):
        errors.append(f"bindings.{name}.model: the route of provider {provider_id} is {facts.route}")
    lead_capable = isinstance(entry.get("lead"), Mapping)
    native = tuple(dict.fromkeys((*lcat.agent_efforts, *lcat.lead_efforts)))
    allowed = available_efforts(entry, native, lead=lead_capable)
    if effort not in allowed:
        errors.append(f"bindings.{name}.effort: {effort!r} is not supported by {model!r} "
                      f"(available: {', '.join(allowed)})")
    return errors


def binding_errors(
    name: str,
    value: Mapping[str, Any],
    cat: "LineupCatalog | catalog.Catalog",
    effective: "settings.Effective | None",
) -> list[str]:
    """B1-B7 for one proposed named binding, as :meth:`BindingStore.save`
    checks an added name (import plans with the ordinary checks)."""

    return _binding_errors(name, value, LineupCatalog.from_catalog(cat), effective)


class BindingStore:
    """The operator's named bindings, ``<config root>/bindings.json``.

    The file is ``{"version": 1, "bindings": {<name>: {"model", "effort"}}}``
    (a versioned wrapper; operator state, never a catalog document). A
    profile slot ``{"use": <name>}`` resolves through it, so an edit here
    reaches every referencing profile.
    """

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = None if environ is None else dict(environ)
        self.path = bindings_path(self._environ)

    # -------------------------------------------------------------- reads

    def load(self) -> dict[str, Any]:
        """The document; absent → ``{"version": 1, "bindings": {}}``.

        Tolerates retired models and catalog collisions (:meth:`conflicts`
        reports the latter; resolution uses the explicit ``use`` field, so
        it is never ambiguous). Anything unreadable raises
        :class:`BindingError`.
        """

        try:
            present = self.path.is_symlink() or self.path.exists()
        except OSError as exc:
            raise BindingError(f"cannot load named bindings: {_error_detail(exc)}") from exc
        if not present:
            return _empty_bindings()
        try:
            document = strict_json.loads(state.read_private(self.path))
        except (state.StateError, OSError) as exc:
            raise BindingError(f"cannot load named bindings: {_error_detail(exc)}") from exc
        except (strict_json.StrictJSONError, ValueError, RecursionError) as exc:
            raise BindingError(f"cannot load named bindings: {exc}") from exc
        problems = _bindings_problems(document)
        if problems:
            raise BindingError(f"cannot load named bindings: {'; '.join(problems)}")
        return document

    def bindings(self) -> dict[str, dict[str, Any]]:
        """``load()["bindings"]`` — the map :func:`evaluate` takes as ``bindings=``."""

        return self.load()["bindings"]

    def conflicts(self, cat: "LineupCatalog | catalog.Catalog") -> list[str]:
        """B2/B3 for stored names that collide with the catalog (doctor/editor use).

        A later catalog may add a key equal to a stored name; the writers
        refuse collisions on added or changed names, ``load`` and the writers
        tolerate old ones on unchanged names.
        """

        lcat = LineupCatalog.from_catalog(cat)
        found = [_collision(name, lcat) for name in sorted(self.bindings())]
        return [message for message in found if message is not None]

    # ------------------------------------------------------------- writes

    def _begin(self) -> state.FileLock:
        """Marker, private directory, then the store lock (acquired once)."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        state.ensure_private_dir(self.path.parent)
        lock = state.FileLock(self.path)
        lock.acquire(blocking=True)
        return lock

    @staticmethod
    def _check_shape(document: Any) -> dict[str, Any]:
        """Version, schema and B1 over the whole document (what ``load`` enforces); a deep copy."""

        problems = _bindings_problems(document)
        if problems:
            raise BindingError("; ".join(problems))
        return copy.deepcopy(document)

    @staticmethod
    def _check_changed(
        candidate: Mapping[str, Any],
        old: Mapping[str, Any],
        lcat: LineupCatalog,
        effective: "settings.Effective | None",
    ) -> None:
        """B1-B7 for every added or changed name (sorted).

        An unchanged stored name is tolerated exactly as ``load`` tolerates
        it (a collision a later catalog introduced, a model a later catalog
        retired): a stale entry never blocks an unrelated edit, and two
        stale entries can be repaired one at a time.
        """

        bindings = candidate["bindings"]
        errors: list[str] = []
        for name in sorted(_changed_names(old, bindings) & set(bindings)):
            errors += _binding_errors(name, bindings[name], lcat, effective)
        if errors:
            raise BindingError("; ".join(errors))

    @staticmethod
    def _check_profiles(
        candidate: Mapping[str, Any],
        old: Mapping[str, Any],
        lcat: LineupCatalog,
        profiles: "ProfileStore | None",
        effective: "settings.Effective | None",
    ) -> None:
        """B9 (only with ``profiles``)."""

        if profiles is None:
            return
        errors = _invalidated_profiles(profiles, lcat, old, candidate["bindings"], effective)
        if errors:
            raise BindingError("; ".join(errors))

    def _write(self, document: Mapping[str, Any]) -> Path:
        """The private write (caller holds the store lock)."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        state.atomic_write(self.path, strict_json.pretty_file_bytes(document))
        return self.path

    def save(
        self,
        doc: Mapping[str, Any],
        *,
        cat: "LineupCatalog | catalog.Catalog",
        profiles: "ProfileStore | None" = None,
        effective: "settings.Effective | None" = None,
    ) -> Path:
        """Validate and replace the whole document (0600, pretty bytes).

        The shape (version, schema, B1) is checked on the whole document;
        B1-B7 refuse per added or changed name (unchanged stored names are
        tolerated, as on load); with ``profiles`` every profile that uses a
        changed name is re-evaluated with the new bindings and an edit that
        introduces an error refuses (B9). ``effective`` as in
        :func:`evaluate` (None: providers enabled, badges absent). An unreadable
        existing file refuses (``cannot load named bindings: …``) like every
        other writer; it is never silently replaced.
        """

        sessions.check_state_marker(sessions.state_root(self._environ))
        lcat = LineupCatalog.from_catalog(cat)
        candidate = self._check_shape(doc)
        # Unlocked pre-check so a refusal creates nothing; repeated under the lock.
        self._check_changed(candidate, self.load()["bindings"], lcat, effective)
        lock = self._begin()
        try:
            old = self.load()["bindings"]
            self._check_changed(candidate, old, lcat, effective)
            self._check_profiles(candidate, old, lcat, profiles, effective)
            return self._write(candidate)
        finally:
            lock.release()

    def update(
        self,
        mutate: Callable[[dict[str, Any]], None],
        *,
        cat: "LineupCatalog | catalog.Catalog",
        profiles: "ProfileStore | None" = None,
        effective: "settings.Effective | None" = None,
    ) -> dict[str, Any]:
        """Locked load → mutate → check → write; returns the written document."""

        lcat = LineupCatalog.from_catalog(cat)
        lock = self._begin()
        try:
            document = self.load()
            old = copy.deepcopy(document["bindings"])
            mutate(document)
            candidate = self._check_shape(document)
            self._check_changed(candidate, old, lcat, effective)
            self._check_profiles(candidate, old, lcat, profiles, effective)
            self._write(candidate)
            return candidate
        finally:
            lock.release()

    def set(
        self,
        name: str,
        model: str,
        effort: str,
        *,
        cat: "LineupCatalog | catalog.Catalog",
        profiles: "ProfileStore | None" = None,
        effective: "settings.Effective | None" = None,
    ) -> dict[str, Any]:
        """Create or change one named binding."""

        def apply(document: dict[str, Any]) -> None:
            document["bindings"][name] = {"effort": effort, "model": model}

        return self.update(apply, cat=cat, profiles=profiles, effective=effective)

    def delete(self, name: str, *, profiles: "ProfileStore") -> dict[str, Any]:
        """Remove one named binding; refuses (B8) while any profile uses it."""

        lock = self._begin()
        try:
            document = self.load()
            if name not in document["bindings"]:
                raise BindingError(f"named binding {name!r} does not exist")
            users = profiles.referencing(name)
            if users:
                listed = ", ".join(f"{profile_name} ({field})" for profile_name, field in users)
                raise BindingError(f"named binding {name!r} is used by profiles: {listed}")
            del document["bindings"][name]
            self._write(document)
            return document
        finally:
            lock.release()


def _invalidated_profiles(
    profiles: "ProfileStore",
    lcat: LineupCatalog,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    effective: "settings.Effective | None",
) -> list[str]:
    """B9: one entry per profile a binding change would newly invalidate.

    Only profiles that use a changed name are re-evaluated, and only errors
    the old bindings did not already produce count: a profile broken for an
    unrelated reason (a later catalog, a disabled provider) must not block
    every binding edit.
    """

    changed = _changed_names(old, new)
    if not changed:
        return []
    errors: list[str] = []
    for profile_name in profiles.names():
        try:
            document = profiles.load(profile_name)
        except ProfileError:
            continue
        used = [name for name in referenced_bindings(document).values() if name in changed]
        if not used:
            continue
        after = evaluate(document, lcat, bindings=new, effective=effective).errors
        if not after:
            continue
        before = set(evaluate(document, lcat, bindings=old, effective=effective).errors)
        fresh = [message for message in after if message not in before]
        if fresh:
            errors.append(
                f"named binding {used[0]!r} would invalidate profile "
                f"{profile_name!r}: {fresh[0]}"
            )
    return errors


SEED_DIGESTS_FILE = ".seed-digests.json"
SEED_DIGESTS_VERSION = 1
BACKUP_REASONS = ("pre-reseed", "removed")
SEED_KINDS = ("current", "unedited", "edited", "unknown")


def seed_digests_schema_path() -> Path:
    """The sidecar is this release's own state: its schema is always the
    packaged one, whatever resources are selected."""

    from claude_multi import resources_root

    return resources_root() / "schemas" / "seed-digests.schema.json"


_SEED_DIGESTS_SCHEMA: dict[str, Any] = _load_schema(seed_digests_schema_path())


def document_digest(document: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of a profile document's canonical bytes."""

    return "sha256:" + strict_json.sha256_hex(strict_json.canonical_bytes(document))


def seed_digest(name: str, seed: Mapping[str, Any]) -> str:
    """The canonical digest of seed ``name`` as installed under its name."""

    return document_digest(parse({**seed, "name": name}))


def written_digest(document: Mapping[str, Any]) -> str:
    """What :meth:`ProfileStore.digest` reads back after a store write of
    ``document`` (the parsed document a write returns): ``sha256:<hex>`` of
    the exact file bytes that write produced."""

    return "sha256:" + strict_json.sha256_hex(strict_json.pretty_file_bytes(document))


@dataclass(frozen=True)
class SeedState:
    """How a seed's user file relates to the shipped seed.

    ``kind``: ``current`` (no user file, or one equal to the shipped seed),
    ``unedited`` (the file is still what was installed), ``edited`` (it was
    changed, or it cannot be loaded) or ``unknown`` (no install record; it
    is treated as edited). ``installed`` is the installed seed version when
    known; ``stale`` means a newer shipped version exists.
    """

    kind: str
    installed: int | None
    shipped: int
    stale: bool

    @property
    def label(self) -> str:
        """``current``, ``unedited-stale``, ``edited``, ``edited-stale`` or ``unknown``."""

        if self.kind in ("current", "unknown"):
            return self.kind
        if self.kind == "unedited":
            return "unedited-stale" if self.stale else "current"
        return "edited-stale" if self.stale else "edited"


def _binding_text(binding: Any) -> str:
    if not isinstance(binding, Mapping):
        return "(unbound)"
    if isinstance(binding.get("use"), str):
        return f"use {binding['use']}"
    return f"{binding.get('model')} · {binding.get('effort')}"


def _value_text(value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, str):
        return value or "(empty)"
    return strict_json.canonical_bytes(value).decode("utf-8")


def _diff_line(label_text: str, old: str, new: str) -> str:
    return f"{label_text:<14}{old:<20} →  {new}"


def slot_diff(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """One line per changed slot (lead, then agents in role order), then one
    per changed ``description``, ``native_agents.*``, ``workflows``,
    ``primary_provider`` and ``settings_overrides``."""

    lines: list[str] = []
    old_agents = old.get("agents") if isinstance(old.get("agents"), Mapping) else {}
    new_agents = new.get("agents") if isinstance(new.get("agents"), Mapping) else {}
    slots = [(LEAD_ROLE, old.get("lead"), new.get("lead"))]
    slots += [(rid, old_agents.get(rid), new_agents.get(rid)) for rid in AGENT_ROLE_IDS]
    for slot, before, after in slots:
        if before != after:
            lines.append(_diff_line(label(slot), _binding_text(before), _binding_text(after)))
    if old.get("description") != new.get("description"):
        lines.append(_diff_line("description", _value_text(old.get("description")),
                                _value_text(new.get("description"))))
    old_native = old.get("native_agents") if isinstance(old.get("native_agents"), Mapping) else {}
    new_native = new.get("native_agents") if isinstance(new.get("native_agents"), Mapping) else {}
    for key in sorted(set(old_native) | set(new_native)):
        if old_native.get(key) != new_native.get(key):
            lines.append(_diff_line(f"native_agents.{key}", _value_text(old_native.get(key)),
                                    _value_text(new_native.get(key))))
    for field_name in ("workflows", "primary_provider", "settings_overrides"):
        if old.get(field_name) != new.get(field_name):
            lines.append(_diff_line(field_name, _value_text(old.get(field_name)),
                                    _value_text(new.get(field_name))))
    return lines


class ProfileStore:
    """User profiles ``<config root>/profiles/<name>.json`` over the catalog seeds (§9.2).

    A seed without a user file is served virtually from the catalog.
    :meth:`install_seeds` writes only absent seeds and
    is called only by write-allowed entry points; :meth:`reseed`
    is the only overwrite of a seed; seeds are never renamed or deleted.
    Saves are schema-only (version, schema, binding shape): semantic
    validation is the caller's :func:`evaluate`.
    """

    def __init__(
        self,
        *,
        seeds: Mapping[str, Mapping[str, Any]],
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], Any] | None = None,
    ):
        self._environ = None if environ is None else dict(environ)
        self._seeds = {name: copy.deepcopy(dict(doc)) for name, doc in seeds.items()}
        self.config_root = sessions.config_root(self._environ)
        self.root = profiles_dir(self._environ)
        self._clock = clock

    @classmethod
    def for_catalog(
        cls, cat: "catalog.Catalog", environ: Mapping[str, str] | None = None
    ) -> "ProfileStore":
        return cls(seeds=cat.seed_profiles, environ=environ)

    # -------------------------------------------------------------- reads

    def _path(self, name: str) -> Path:
        try:
            state.check_name(name)
        except state.StateError as exc:
            raise ProfileError(_error_detail(exc)) from exc
        return self.root / f"{name}.json"

    def is_seed(self, name: str) -> bool:
        return name in self._seeds

    def seed_document(self, name: str) -> dict[str, Any]:
        """The shipped document of seed ``name`` (never a user copy that
        overrides it); ``KeyError`` for a name no seed has."""

        return copy.deepcopy(self._seeds[name])

    def has_user(self, name: str) -> bool:
        return os.path.lexists(self._path(name))

    def has_user_strict(self, name: str) -> bool:
        """Like has_user, but only a clean 'absent' is False; EACCES on a
        non-searchable directory raises instead of reading as absent."""
        try:
            os.lstat(self._path(name))
        except FileNotFoundError:
            return False
        return True

    def contains(self, name: str) -> bool:
        path = self._path(name)
        return self.is_seed(name) or os.path.lexists(path)

    def require_new_target(self, name: str) -> None:
        if self.contains(name):
            raise ProfileError(f"profile target {name!r} already exists; choose another name")

    def names(self) -> list[str]:
        """Seeds plus the stems of regular ``*.json`` files with a safe name."""

        found = set(self._seeds)
        try:
            entries = list(self.root.glob("*.json"))
        except OSError:
            entries = []
        for path in entries:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                state.check_name(path.stem)
            except OSError:
                continue
            found.add(path.stem)
        return sorted(found)

    def load(self, name: str) -> dict[str, Any]:
        """The user file when present (parsed, stem-checked), else a deep copy of the seed.

        A user file that cannot be loaded raises :class:`ProfileLoadError`
        (its path, the JSON line of a syntax error, the first invalid field).
        """

        path = self._path(name)
        try:
            present = self.has_user_strict(name)
        except OSError as exc:
            raise ProfileLoadError(name, path, exc.strerror or type(exc).__name__,
                                   environ=self._environ) from exc
        if present:
            try:
                document = parse(strict_json.loads(state.read_private(path)))
            except (state.StateError, OSError, ValueError, RecursionError) as exc:
                raise ProfileLoadError.from_exception(name, path, exc, environ=self._environ) from exc
            if document["name"] != name:
                raise ProfileLoadError(
                    name, path,
                    f"the file names {document['name']!r}; the name must equal the file stem",
                    where="name", environ=self._environ,
                )
            return document
        if name in self._seeds:
            return copy.deepcopy(self._seeds[name])
        raise ProfileError(f"profile {name!r} does not exist")

    def digest(self, name: str) -> str | None:
        """What a later write compares with: ``sha256:<hex>`` of the user
        file's bytes (loadable or not), ``seed:sha256:<hex>`` for a seed
        served without a file, None when the profile does not exist."""

        path = self._path(name)
        if os.path.lexists(path):
            try:
                return "sha256:" + strict_json.sha256_hex(state.read_private(path))
            except OSError as exc:
                raise ProfileLoadError(name, path, _error_detail(exc), environ=self._environ) from exc
        if name in self._seeds:
            return "seed:" + seed_digest(name, self._seeds[name])
        return None

    def raw_bytes(self, name: str) -> bytes:
        """The user file's bytes as stored (an unloadable file included)."""

        path = self._path(name)
        try:
            return state.read_private(path)
        except OSError as exc:
            raise ProfileLoadError(name, path, _error_detail(exc), environ=self._environ) from exc

    def seed_digests(self) -> dict[str, dict[str, Any]]:
        """The install records of the sidecar (``{name: {version, digest}}``).

        An absent, unreadable or invalid sidecar reads as empty: every seed
        file is then of ``unknown`` provenance, never ``unedited``."""

        path = self.root / SEED_DIGESTS_FILE
        if not os.path.lexists(path):
            return {}
        try:
            document = strict_json.loads(state.read_private(path))
        except (OSError, ValueError, RecursionError):
            return {}
        if schema_validate.validate(document, _SEED_DIGESTS_SCHEMA, "$"):
            return {}
        return {name: dict(entry) for name, entry in document["seeds"].items()
                if state.SAFE_NAME.fullmatch(name)}

    def seed_state(self, name: str) -> SeedState:
        """The seed's state (see :class:`SeedState`); ``name`` must be a seed."""

        return self.seed_snapshot(name)[0]

    def seed_snapshot(self, name: str) -> tuple[SeedState, str | None]:
        """The seed's state and the :meth:`digest` of the very bytes it was
        judged from, read once (None only for a file that cannot be read,
        which judges as edited): a write that expects that digest refuses
        when the file changed after the judgement."""

        if not self.is_seed(name):
            raise ProfileError(f"profile {name!r} is not a seed profile")
        seed = self._seeds[name]
        marker = seed.get("seed")
        shipped = marker.get("version") if isinstance(marker, Mapping) else None
        shipped = shipped if isinstance(shipped, int) else 0
        path = self._path(name)
        if not os.path.lexists(path):
            return SeedState("current", None, shipped, False), "seed:" + seed_digest(name, seed)
        try:
            raw = state.read_private(path)
        except OSError:
            return SeedState("edited", None, shipped, True), None
        return self._judge_seed(name, raw, shipped), "sha256:" + strict_json.sha256_hex(raw)

    def _judge_seed(self, name: str, raw: bytes, shipped: int) -> SeedState:
        """The state of seed ``name`` whose user file holds ``raw``."""

        seed = self._seeds[name]
        try:
            document = parse(strict_json.loads(raw))
        except (ProfileError, ValueError, RecursionError):
            return SeedState("edited", None, shipped, True)
        if document["name"] != name:
            return SeedState("edited", None, shipped, True)
        own = document.get("seed")
        installed = own.get("version") if isinstance(own, Mapping) and own.get("id") == name else None
        installed = installed if isinstance(installed, int) else None
        digest = document_digest(document)
        if digest == seed_digest(name, seed):
            return SeedState("current", installed, shipped, False)
        recorded = self.seed_digests().get(name)
        if recorded is None:
            return SeedState("unknown", installed, shipped, installed is None or installed < shipped)
        if recorded["digest"] == digest:
            return SeedState("unedited", recorded["version"], shipped, recorded["version"] < shipped)
        return SeedState("edited", installed, shipped, installed is None or installed < shipped)

    def seed_updates(self) -> list[tuple[str, int | None, int]]:
        """``(name, installed seed version or None, shipped version)`` for stale seed files.

        Only seeds with a user file whose ``seed`` marker is older than the
        shipped seed's (or absent, or naming another seed) are listed;
        unreadable files are skipped. Read-only (the Profiles screen shows it as Attention).
        """

        updates: list[tuple[str, int | None, int]] = []
        for name, seed in self._seeds.items():
            shipped = seed.get("seed", {}).get("version")
            if not isinstance(shipped, int) or not self.has_user(name):
                continue
            try:
                document = self.load(name)
            except ProfileError:
                continue
            marker = document.get("seed")
            installed = (
                marker.get("version")
                if isinstance(marker, Mapping) and marker.get("id") == name
                else None
            )
            if installed is None or installed < shipped:
                updates.append((name, installed, shipped))
        return updates

    def referencing(self, binding_name: str) -> list[tuple[str, str]]:
        """``(profile, field)`` for every loadable profile using ``binding_name``."""

        found: list[tuple[str, str]] = []
        for name in self.names():
            try:
                document = self.load(name)
            except ProfileError:
                continue
            for field, used in referenced_bindings(document).items():
                if used == binding_name:
                    found.append((name, field))
        return found

    # ------------------------------------------------------------- writes

    @property
    def lock_target(self) -> Path:
        """The store lock's target: the lock file is ``<config root>/profiles.lock``."""

        return self.config_root / "profiles"

    def _begin(self) -> state.FileLock:
        """Marker, private directories, then the store lock (acquired once).

        The lock file is ``<config root>/profiles.lock``.
        """

        sessions.check_state_marker(sessions.state_root(self._environ))
        state.ensure_private_dir(self.config_root)
        state.ensure_private_dir(self.root)
        lock = state.FileLock(self.lock_target)
        lock.acquire(blocking=True)
        return lock

    def _write(self, document: Mapping[str, Any], name: str) -> dict[str, Any]:
        """Force the name, parse, write privately (caller holds the store lock)."""

        path = self._path(name)
        candidate = copy.deepcopy(dict(document))
        candidate["name"] = name
        parsed = parse(candidate)
        sessions.check_state_marker(sessions.state_root(self._environ))
        state.atomic_write(path, strict_json.pretty_file_bytes(parsed))
        return parsed

    def _check_expected(self, name: str, expected: str | None) -> None:
        """Under the store lock: refuse when ``name`` changed since ``expected``."""

        if expected is not None and self.digest(name) != expected:
            raise ProfileChangedError(name)

    def _now(self) -> Any:
        import datetime

        return self._clock() if self._clock is not None else datetime.datetime.now(datetime.timezone.utc)

    def _backup_locked(self, name: str, reason: str) -> Path:
        """Keep the user file's bytes as ``.<name>.<reason>-<UTC stamp>.json``
        (``.<n>`` added on a clash; an existing file is never overwritten).
        The caller holds the store lock."""

        if reason not in BACKUP_REASONS:
            raise ValueError(f"unknown backup reason {reason!r}")
        data = self.raw_bytes(name)
        stamp = self._now().strftime("%Y%m%dT%H%M%SZ")
        descriptor, temporary = tempfile.mkstemp(dir=self.root, prefix=f".{name}.", suffix=".tmp")
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            attempt = 0
            while True:
                suffix = "" if attempt == 0 else f".{attempt}"
                target = self.root / f".{name}.{reason}-{stamp}{suffix}.json"
                try:
                    os.link(temporary, target)  # never replaces an existing file
                except FileExistsError:
                    attempt += 1
                    continue
                break
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        posix_fs.fsync_directory(self.root)
        return target

    def _record_seed_locked(self, written: Sequence[str]) -> None:
        """Record the install of ``written`` seeds in the sidecar (caller holds the lock)."""

        if not written:
            return
        seeds = self.seed_digests()
        for name in written:
            marker = self._seeds[name].get("seed")
            version = marker.get("version") if isinstance(marker, Mapping) else None
            if not isinstance(version, int) or version < 1:
                seeds.pop(name, None)
                continue
            seeds[name] = {"version": version, "digest": seed_digest(name, self._seeds[name])}
        document = {"version": SEED_DIGESTS_VERSION, "seeds": dict(sorted(seeds.items()))}
        state.atomic_write(self.root / SEED_DIGESTS_FILE, strict_json.pretty_file_bytes(document))

    def backup(self, name: str, reason: str) -> Path:
        """Keep a private copy of ``name``'s user file (see :meth:`_backup_locked`)."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        self._path(name)
        lock = self._begin()
        try:
            return self._backup_locked(name, reason)
        finally:
            lock.release()

    def _prepared(self, document: Any, name: str) -> dict[str, Any]:
        """Name + parse checks before any directory is created (a refused save writes nothing)."""

        self._path(name)
        if not isinstance(document, Mapping):
            raise ProfileValidationError(["profile: expected an object"])
        candidate = copy.deepcopy(dict(document))
        candidate["name"] = name
        return parse(candidate)

    def save(self, document: Mapping[str, Any], *, target: str | None = None,
             expected: str | None = None) -> Path:
        """Write ``document`` as ``target`` (default: its own name); overwrites.

        With ``expected`` (a :meth:`digest` read earlier) the save refuses
        with :class:`ProfileChangedError` when the profile changed since."""

        name = target if target is not None else _document_name(document)
        self.commit(document, target=name, expected=expected)
        return self._path(name)

    def new(self, document: Mapping[str, Any]) -> Path:
        """Save under a name that is neither a seed nor an existing user file."""

        self.commit(document, new=True)
        return self._path(_document_name(document))

    def commit(self, document: Mapping[str, Any], *, target: str | None = None,
               expected: str | None = None, new: bool = False) -> str:
        """:meth:`save` (with ``new``: :meth:`new`), returning the digest of
        the exact bytes this write produced — what :meth:`digest` reads
        until the next write, whoever makes it."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        name = target if target is not None else _document_name(document)
        prepared = self._prepared(document, name)
        lock = self._begin()
        try:
            if new:
                self.require_new_target(name)
            else:
                self._check_expected(name, expected)
            written = self._write(prepared, name)
        finally:
            lock.release()
        return written_digest(written)

    def create(self, document: Mapping[str, Any]) -> Path:
        """Write a profile whose user file is absent (import): a
        virtual seed may be shadowed, an existing user file never is.
        Refuses under the store lock when a file appeared meanwhile."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        name = _document_name(document)
        prepared = self._prepared(document, name)
        lock = self._begin()
        try:
            if os.path.lexists(self._path(name)):
                raise ProfileError(f"profile {name!r} appeared meanwhile; it is not overwritten")
            self._write(prepared, name)
        finally:
            lock.release()
        return self._path(name)

    def update(self, name: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Locked load → mutate → write under the same name; returns the written document."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        self._path(name)
        lock = self._begin()
        try:
            document = self.load(name)
            mutate(document)
            return self._write(document, name)
        finally:
            lock.release()

    def duplicate(self, source: str, target: str, *, keep_primary: bool = False,
                  expected: str | None = None) -> dict[str, Any]:
        """Copy ``source`` (a seed or user profile) to a new ``target``, dropping
        ``seed`` and, unless ``keep_primary``, ``primary_provider`` (a copy is
        not another fallback profile for the same provider unless chosen)."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        self._path(source)
        self._path(target)
        lock = self._begin()
        try:
            self.require_new_target(target)
            self._check_expected(source, expected)
            document = self.load(source)
            document.pop("seed", None)
            if not keep_primary:
                document.pop("primary_provider", None)
            return self._write(document, target)
        finally:
            lock.release()

    def rename(self, source: str, target: str, *, expected: str | None = None,
               document: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Move a user profile; seeds refuse. With ``document`` the
        target holds that document (an edited profile saved under its new
        name) instead of the stored one; returns what was written.

        Two steps under the store lock (write target, then remove source):
        a crash between them leaves both names, and deleting one by hand
        converges (the cli.CompositionStore precedent).
        """

        sessions.check_state_marker(sessions.state_root(self._environ))
        self._path(source)
        self._path(target)
        if self.is_seed(source):
            raise ProfileError(
                f"profile {source!r} is a seed and cannot be renamed; duplicate it instead"
            )
        prepared = self._prepared(document, target) if document is not None else None
        lock = self._begin()
        try:
            self.require_new_target(target)
            self._check_expected(source, expected)
            written = self._write(prepared if prepared is not None else self.load(source), target)
            state.remove_private(self._path(source))
            return written
        finally:
            lock.release()

    def delete(self, name: str) -> bool:
        """Remove a user profile (durable); seeds refuse. False when absent."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        path = self._path(name)
        if self.is_seed(name):
            raise ProfileError(
                f"profile {name!r} is a seed; edit it, or restore it with "
                f"'claude-multi profile reseed {name}'"
            )
        lock = self._begin()
        try:
            return state.remove_private(path)
        finally:
            lock.release()

    def remove(self, name: str, *, expected: str | None = None) -> Path:
        """Delete a user profile after keeping a ``removed`` copy of its bytes
        (an unloadable file included); returns the copy. Seeds refuse."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        path = self._path(name)
        if self.is_seed(name):
            raise ProfileError(
                f"profile {name!r} is a seed; edit it, or restore it with "
                f"'claude-multi profile reseed {name}'"
            )
        lock = self._begin()
        try:
            self._check_expected(name, expected)
            if not os.path.lexists(path):
                raise ProfileError(f"profile {name!r} does not exist")
            kept = self._backup_locked(name, "removed")
            state.remove_private(path)
            return kept
        finally:
            lock.release()

    def reseed(self, name: str) -> dict[str, Any]:
        """Overwrite ``name`` with its catalog seed (the only seed overwrite)."""

        return self.restore_seed(name, keep_copy=False)[0]

    def restore_seed(self, name: str, *, keep_copy: bool = True,
                     expected: str | None = None) -> tuple[dict[str, Any], Path | None]:
        """Overwrite ``name`` with its catalog seed and record the install.

        With ``keep_copy`` an existing user file (loadable or not) is kept
        first as a ``pre-reseed`` copy; returns ``(written, copy or None)``."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        path = self._path(name)
        if not self.is_seed(name):
            raise ProfileError(f"profile {name!r} is not a seed profile")
        lock = self._begin()
        try:
            self._check_expected(name, expected)
            kept = self._backup_locked(name, "pre-reseed") if keep_copy and os.path.lexists(path) else None
            written = self._write(self._seeds[name], name)
            self._record_seed_locked([name])
            return written, kept
        finally:
            lock.release()

    def refresh_unedited(self, expected: Mapping[str, str]) -> list[tuple[str, Path | None]]:
        """Restore the shipped version of each seed in ``expected`` (name →
        the digest its plan read) as one batch under one acquisition of the
        store lock: each is first judged again from its current bytes, and
        only when every one is still that digest and still an unedited seed
        with a newer shipped version is anything written (each a kept copy
        first, as :meth:`restore_seed`). Otherwise
        :class:`ProfileChangedError` names the first that changed and
        nothing is written. A failure once some file was replaced — an
        earlier profile's, or this one's before a later step of it (its
        install record) failed — raises :class:`ProfileBatchError`, which
        names every profile whose file changed. Returns ``(name, copy)``."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        for name in expected:
            self._path(name)
            if not self.is_seed(name):
                raise ProfileError(f"profile {name!r} is not a seed profile")
        lock = self._begin()
        try:
            for name, digest in expected.items():
                judged, current = self.seed_snapshot(name)
                if current is None or current != digest or judged.label != "unedited-stale":
                    raise ProfileChangedError(name)
            done: list[tuple[str, Path | None]] = []
            for name, digest in expected.items():
                replaced = False
                try:
                    path = self._path(name)
                    kept = self._backup_locked(name, "pre-reseed") if os.path.lexists(path) else None
                    try:
                        self._write(self._seeds[name], name)
                    except state.CommittedStateError:
                        replaced = True  # the new bytes are in place; only their durability is unconfirmed
                        raise
                    replaced = True
                    self._record_seed_locked([name])
                except Exception as exc:
                    replaced = replaced or self._replaced_since(name, digest)
                    if done or replaced:
                        raise ProfileBatchError([item for item, _kept in done], name, exc,
                                                failed_changed=replaced) from exc
                    raise
                done.append((name, kept))
            return done
        finally:
            lock.release()

    def _replaced_since(self, name: str, digest: str) -> bool:
        """Whether ``name``'s file no longer holds the bytes of ``digest``
        (one that cannot be read counts as replaced: a changed profile is
        never reported unchanged). The caller holds the store lock."""

        try:
            return self.digest(name) != digest
        except ProfileError:
            return True

    def install_seeds(self) -> list[str]:
        """Write every seed whose user file is absent (never overwrite); returns the names.

        Callers are write-allowed entry points only (never
        session-event, doctor or read-only commands, never with
        ``Runtime(allow_state_writes=False)``).
        """

        sessions.check_state_marker(sessions.state_root(self._environ))
        lock = self._begin()
        try:
            written = []
            for name, seed in self._seeds.items():
                if os.path.lexists(self._path(name)):
                    continue
                self._write(seed, name)
                written.append(name)
            self._record_seed_locked(written)
            return written
        finally:
            lock.release()


def _document_name(document: Any) -> str:
    name = document.get("name") if isinstance(document, Mapping) else None
    if not isinstance(name, str):
        raise ProfileValidationError(["name: a profile needs a name"])
    return name
