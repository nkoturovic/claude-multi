"""Trusted catalog loading and reference validation for claude-multi v2.

Loads versioned JSON assets, validates them against the closed-vocabulary
schemas, and enforces semantic invariants: reference integrity, availability
narrowing, provider fork policy, selector/route conflicts, role compatibility,
prompt identity, secret prohibition, and the deterministic bundle hash.
"""

from __future__ import annotations

from . import assets

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import errors, strict_json, validate as schema_validate


# Data version of every catalog document except ``models`` and ``roles``
# (and of legacy compositions: composition.py and cli.py compare against it).
SUPPORTED_DATA_VERSION = 1
# ``catalog/models.json`` data version: model lines.
MODELS_DATA_VERSION = 2
# ``catalog/roles.json`` data version: roles v2 (function, grade).
ROLES_DATA_VERSION = 2
# Seed profile data version (``catalog/profiles/*.json``).
PROFILE_DATA_VERSION = 2
# Native contract data version: the path-free contract whose ``verified``
# list pins one Claude Code version per platform.
NATIVE_CONTRACT_DATA_VERSION = 2

# Conservative evidence ceiling for operator-declared or unqualified models.
CUSTOM_VALIDATED_CAP = 200_000

# Client-effort adapters: effort travels in agent frontmatter / --effort, so
# a line carries one ``selector`` and an ``efforts`` list. Every other adapter
# is gateway-effort: one selector per effort, each with its payload contract.
CLIENT_EFFORT_ADAPTERS = frozenset({"cliproxy-oauth-claude-v1", "cliproxy-openai-compat-v1"})
_ANTHROPIC_POOL_ADAPTER = "cliproxy-oauth-claude-v1"

# The pinned client's categorized-refusal fallback switches only to these
# hard-coded Anthropic ids (observed on the pinned client): they must stay passthrough
# routes of the Anthropic pool. A client fact, not provider data (the
# fallback-only fence set imports it). Re-derived on 2.1.286 through a
# gateway (client lane; no ``fallbacks`` opt-in is sent, and an API-lane
# server-named target is not followed): Opus/Fable cyber ->
# claude-opus-4-8, bio/frontier_llm -> claude-opus-5; Sonnet 5.5 cyber and
# frontier_llm -> claude-sonnet-5, bio terminal with zero fallback
# requests (tests/test_client_models.py S7).
REFUSAL_FALLBACK_WIRES = ("claude-opus-4-8", "claude-opus-5", "claude-sonnet-5")

LINE_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*$")
RETIRED_KEY = re.compile(r"^([a-z0-9][a-z0-9-]*)(?:@([0-9A-Za-z][0-9A-Za-z.-]{0,15}))?$")
SELECTOR_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*(\[1m\])?$")

# The operator layer's line-origin vocabulary of the shared validator.
# ``legacy-custom`` lines (custom.json synthesis) are never validated here;
# ``catalog`` is the trusted catalog.
LINE_ORIGINS = ("catalog", "legacy-custom", "operator", "operator-migrated")
_VALIDATED_ORIGINS = frozenset({"catalog", "operator", "operator-migrated"})
# The ``custom-`` key and selector namespace belongs to operator lines (and
# the legacy custom.json aliases); the trusted catalog never uses it.
CUSTOM_NAMESPACE = "custom-"
OPERATOR_KEY = re.compile(r"^custom-[a-z][a-z0-9-]{0,40}$")
# Migrated legacy ids keep a wider, rollback-safe grammar.
MIGRATED_KEY = re.compile(r"^custom-[a-z0-9][a-z0-9-]{0,56}$")
# The OAuth model overlay (gateway patch 16): the loader's name grammar
# and thinking-level vocabulary. A catalog/retired ``registry_overlay`` is
# only the closed ``{"channel"}`` marker; capabilities come from the entry.
OVERLAY_CHANNELS = ("claude", "codex")
OVERLAY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
OVERLAY_THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max", "none", "auto")
_RETIRED_CHAIN_LIMIT = 8

ROLE_ID = re.compile(r"^cm-[a-z][a-z0-9-]*$")
LEAD_ROLE = "cm-lead"

# Roles v2. profile.py imports these and never
# redefines them.
AGENT_ROLE_IDS = (
    "cm-explorer",
    "cm-analyst",
    "cm-analyst-strong",
    "cm-implementer-light",
    "cm-implementer",
    "cm-implementer-strong",
    "cm-reviewer",
    "cm-reviewer-strong",
    "cm-designer",
)
ROLE_IDS = (LEAD_ROLE, *AGENT_ROLE_IDS)
ROLE_FUNCTIONS = ("lead", "explorer", "analyst", "implementer", "reviewer", "designer")
# `Skill` is denied too: a skill can fork an agent outside
# the lineup, and a per-skill `Skill(<name>)` frontmatter entry removes the
# whole tool at the pinned client, so the tool is denied.
READ_ONLY_TOOLS = ("Edit", "Write", "NotebookEdit", "Agent", "Skill")
READ_ONLY_FUNCTIONS = frozenset({"explorer", "reviewer"})
SEED_PROFILE_NAMES = ("balanced", "quality", "max", "economy", "claude", "openai", "openrouter", "direct")
DEFAULT_SEED = "balanced"
# Generic words that are provider ids or families but never identify a model
# in role text.
IDENTITY_TOKEN_EXCLUSIONS = frozenset({"meta", "local", "custom", "unknown"})

# Compiler-owned environment keys: never settable from a model lead.env block.
# Context capacity and the proactive compaction percentage are derived from the
# catalog's explicit context policy; model-specific env cannot contradict them.
# The config-dir, updater, and nested-spawn keys are also reserved
# against lead.env so a model cannot override launcher isolation.
# CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS is compiled from native-agent
# policy; a lead.env value could contradict the appended policy truth.
CREDENTIAL_ENV_KEYS = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_USE_GATEWAY",
)

RESERVED_LEAD_ENV_KEYS = frozenset(
    {
        *CREDENTIAL_ENV_KEYS,
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
        "CLAUDE_CONFIG_DIR",
        "DISABLE_AUTOUPDATER",
        "DISABLE_UPDATES",
        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
        "CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS",
        "CLAUDE_CODE_DISABLE_WORKFLOWS",
        # Compiled into the session's flag settings env (family
        # defaults, the Explore inherit cap) and unset in the process env.
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP",
        # Policy-defeating keys the
        # v2 compile unsets; a lead env must never set them either.
        "CLAUDE_CODE_EFFORT_LEVEL",
        "DISABLE_AUTO_COMPACT",
        "DISABLE_COMPACT",
        "MAX_THINKING_TOKENS",
        "CLAUDE_CODE_DISABLE_THINKING",
        "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
        "CLAUDE_CODE_COORDINATOR_FORCE_WORKER_INHERIT_MODEL",
        # Compiled into every managed launch env so
        # the client never prefetches fast-mode availability with the
        # gateway key; a lead env must never switch it back on.
        "CLAUDE_CODE_DISABLE_FAST_MODE",
    }
)

_SELECTOR_SUFFIX = "[1m]"
_RENDER_SENTINEL_PREFIX = "claude-multi-render-"

_HASH_FIELDS = frozenset(
    {"sha256", "composition_hash", "catalog_hash", "pre_image_hash", "post_image_hash"}
)
_ENV_REF = re.compile(r"^env:[A-Z0-9_]+$")
_SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret|password|bearer)\b\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=@:-]{8,}"
    ),
)

SCHEMA_NAMES = (
    "gateway",
    "providers",
    "models",
    "retired",
    "roles",
    "profile",
    "native-contract",
    "composition",
    "session",
    "draft",
    "review",
)

SETTINGS_ALLOWED_KEYS = frozenset(
    {"disableWorkflows", "workflowSizeGuideline", "workflowKeywordTriggerEnabled"}
)


class CatalogError(errors.ClaudeMultiError, ValueError):
    """Raised when trusted catalog data fails loading or validation."""


@dataclass(frozen=True)
class KeyResolution:
    """Result of :meth:`Catalog.resolve_key`."""

    requested: str
    key: str | None  # live line key, or None (the chain ends in null)
    chain: tuple[str, ...]  # retired keys traversed, in order
    notice: str | None  # None when ``requested`` is live


# ------------------------------------------------------------ line shape
# Pure helpers over v2 lines.


def effort_mode(provider: dict[str, Any]) -> str:
    """``"client"`` (one selector, effort in frontmatter) or ``"gateway"``."""

    return "client" if provider["adapter"] in CLIENT_EFFORT_ADAPTERS else "gateway"


def line_selectors(entry: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """``(effort, selector, proxy_contract)`` per selector of a v2 line.

    List-shaped ``efforts`` is client-effort: one selector, bound to the
    default effort. Map-shaped is gateway-effort, alphabetical by level
    (the v1 lane order).
    """

    if isinstance(entry["efforts"], list):
        return [(entry["default_effort"], entry["selector"], None)]
    return [
        (level, spec["selector"], spec["proxy_contract"])
        for level, spec in sorted(entry["efforts"].items())
    ]


# ------------------------------------------------------------ the reviewed API-key route of a pool line
# A catalog line on an OAuth pool whose provider also takes an API key
# through a channel that lists its models explicitly (the codex channel:
# an empty list there would expose the gateway's whole default registry)
# declares what is reviewed for that key route in ``key_route``: the efforts
# the provider documents for it, its documented context window and maximum
# input (the line's own provider bound must fit inside it), optionally its
# documented output limit, and the documentation it comes from. A line
# without it is not served on the key route; nothing is ever inherited from
# the account route's evidence.
KEY_ROUTE_POOLS = ("codex",)


def key_route(entry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The reviewed API-key route block of a line, or None."""

    route = entry.get("key_route")
    return route if isinstance(route, Mapping) else None


def key_route_floor(route: Mapping[str, Any]) -> int:
    """The validated floor of a reviewed API-key route: the route is read
    from the vendor's documentation and never measured on it, and nothing
    is inherited from the account route's evidence, so it is the
    conservative cap of an unmeasured line."""

    return min(int(route["input_tokens"]), CUSTOM_VALIDATED_CAP)


def key_route_levels(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """The reviewed key-route efforts of a line in canonical thinking order
    (empty when the line has no reviewed key route)."""

    route = key_route(entry)
    if route is None:
        return ()
    declared = set(route.get("efforts") or ())
    return tuple(level for level in OVERLAY_THINKING_LEVELS if level in declared)


def _check_key_route(
    where: str, entry: Mapping[str, Any], provider: Mapping[str, Any], *, operator: bool,
) -> list[str]:
    route = entry.get("key_route")
    if route is None:
        return []
    if operator:
        return [f"{where}.key_route: only a reviewed catalog line lists an API-key route"]
    transport = provider.get("transport", {})
    if transport.get("kind") != "oauth-pool" or transport.get("pool") not in KEY_ROUTE_POOLS:
        return [f"{where}.key_route: only a line on an OAuth pool of {list(KEY_ROUTE_POOLS)} lists an "
                "API-key route"]
    found: list[str] = []
    efforts = entry.get("efforts")
    declared = set(efforts) if isinstance(efforts, dict) else set()
    for level in route["efforts"]:
        if level not in declared:
            found.append(f"{where}.key_route.efforts: {level!r} is not an effort of the line")
        elif level not in OVERLAY_THINKING_LEVELS:
            found.append(f"{where}.key_route.efforts: {level!r} is outside the thinking-level vocabulary")
    if route["input_tokens"] > route["context_tokens"]:
        found.append(f"{where}.key_route.input_tokens: exceeds the route's context_tokens")
    output = route.get("output_tokens")
    if output is not None and output > route["context_tokens"]:
        found.append(f"{where}.key_route.output_tokens: exceeds the route's context_tokens")
    if entry["context"]["provider_tokens"] > route["input_tokens"]:
        found.append(f"{where}.key_route.input_tokens: the line's provider bound "
                     f"{entry['context']['provider_tokens']} exceeds the route's documented input limit")
    return found


# ------------------------------------------------------------ keyed compat gate
# Keyed generic OpenAI-compatible chat. The trusted
# gateway catalog owns the one route permission,
# ``gateway.json#/audits/openai_compat_keyed`` (closed, optional boolean;
# absent or false = closed). No provider document, ledger entry, discovery
# result, CLI flag or qualification sets it, and a closed gate never
# downgrades a keyed route to the keyless LAN kind. The audited shape is the
# ``cliproxy-openai-compat-v1`` adapter on a ``direct-openai`` transport with
# HTTPS and bearer auth from one ``env:NAME`` secret; its lines are
# client-effort lists. The same pure shape check runs on a trusted (T1)
# catalog provider, a resolved operator (T2) provider and, as a renderer
# precondition, before any secret is resolved.
AUDITS_KEY = "audits"
KEYED_COMPAT_AUDIT = "openai_compat_keyed"
OPENAI_COMPAT_ADAPTER = "cliproxy-openai-compat-v1"
OPENAI_COMPAT_TRANSPORT = "direct-openai"
# The canonical ascending native order of keyed thinking levels:
# a keyed line renders exactly its declared subset in this order.
KEYED_COMPAT_LEVELS = ("low", "medium", "high", "xhigh", "max")
KEYED_AUDIT_CLOSED = ("keyed generic openai-compatible routes are unavailable in this build: "
                      "their compatibility audit gate is closed")
# The canonical OpenAI Platform host is refused for the generic kind
# whatever its path, case or default-port spelling (lexical; no DNS claim).
OPENAI_PLATFORM_HOST = "api.openai.com"
OPENAI_PLATFORM_REFUSAL = (
    "the OpenAI Platform API host is not a generic openai-compatible route "
    "(use the OpenAI Platform API-key route instead)"
)


def keyed_compat_audited(gateway: Any) -> bool:
    """The single trusted accessor: ``gateway.json`` ``audits.openai_compat_keyed`` is true.

    ``gateway`` is the trusted ``gateway.json`` document (``docs["gateway"]``).
    Anything but a literal ``true`` is closed.
    """

    audits = gateway.get(AUDITS_KEY) if isinstance(gateway, Mapping) else None
    return isinstance(audits, Mapping) and audits.get(KEYED_COMPAT_AUDIT) is True


def is_keyed_compat(provider: Any) -> bool:
    """auth.kind-aware: a ``direct-openai`` transport that is not keyless.

    A keyless LAN route (``auth.kind`` none, ``readiness.is_lan``) is not
    keyed; a missing or malformed auth block counts as keyed so it is
    refused by the gate rather than served keyless.
    """

    transport = provider.get("transport") if isinstance(provider, Mapping) else None
    if not isinstance(transport, Mapping) or transport.get("kind") != OPENAI_COMPAT_TRANSPORT:
        return False
    auth = transport.get("auth")
    return not (isinstance(auth, Mapping) and auth.get("kind") == "none")


def keyed_secret_name(provider: Any) -> str | None:
    """The logical secret name of a keyed compat provider, else None."""

    if not is_keyed_compat(provider):
        return None
    auth = provider["transport"].get("auth")
    ref = auth.get("secret_ref") if isinstance(auth, Mapping) else None
    return ref.removeprefix("env:") if isinstance(ref, str) and _ENV_REF.fullmatch(ref) else None


def platform_host_problem(origin: str) -> str | None:
    """The lexical OpenAI Platform exclusion over a normalized origin."""

    host = origin.split("://", 1)[-1].rsplit("@", 1)[-1]
    if host.startswith("["):
        return None
    if host.split(":", 1)[0].rstrip(".").lower() == OPENAI_PLATFORM_HOST:
        return OPENAI_PLATFORM_REFUSAL
    return None


def keyed_compat_problem(provider: Any, gateway: Any) -> str | None:
    """The shared T1/T2 shape gate: None when ``provider`` is not keyed
    compat, or is keyed compat on an open gate with exactly the audited shape.

    Fixed, value-free texts only (never authored text). Pure: no store, no
    network. ``normalize_endpoint`` stays ``operator`` policy and is imported
    at call time (``operator`` imports this module).
    """

    if not is_keyed_compat(provider):
        return None
    if not keyed_compat_audited(gateway):
        return KEYED_AUDIT_CLOSED
    if provider.get("adapter") != OPENAI_COMPAT_ADAPTER:
        return f"a keyed direct-openai route uses the {OPENAI_COMPAT_ADAPTER} adapter"
    transport = provider["transport"]
    auth = transport.get("auth")
    if (not isinstance(auth, Mapping) or set(auth) != {"kind", "secret_ref"} or auth.get("kind") != "bearer"
            or not isinstance(auth.get("secret_ref"), str) or not _ENV_REF.fullmatch(auth["secret_ref"])):
        return "a keyed openai-compatible route takes bearer auth from exactly one env:NAME secret"
    if provider.get("payload_contracts") or provider.get("passthrough_routes"):
        return "a keyed openai-compatible route takes no payload contracts or passthrough routes"
    from . import operator as operator_mod  # call-time: operator imports catalog

    try:
        origin, _base = operator_mod.normalize_endpoint(
            transport.get("base_url"), keyed=True, lan=False,
            gateway_ports=operator_mod.gateway_ports({"gateway": gateway}),
        )
    except ValueError as exc:
        return f"a keyed openai-compatible base URL is refused ({exc})"
    return platform_host_problem(origin)


def keyed_compat_levels(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """The canonical ascending levels a keyed compat line declares."""

    efforts = entry.get("efforts")
    declared = set(efforts) if isinstance(efforts, list) else set()
    return tuple(level for level in KEYED_COMPAT_LEVELS if level in declared)


# Aggregator providers route models of several families: each of their
# catalog lines declares its own ``family`` (the provider's
# ``independence_family`` is ``unknown``), and an operator line on one
# declares a known family or ``unknown``.
AGGREGATOR_PROVIDERS = frozenset({"openrouter"})
# The family a legacy ``custom.json`` synthetic entry carries; no catalog
# line may declare it (nor ``unknown``, which is never a declared family).
LEGACY_CUSTOM_FAMILY = "custom"
UNDECLARED_FAMILIES = frozenset({LEGACY_CUSTOM_FAMILY, "unknown"})


def is_legacy_custom_entry(entry: Mapping[str, Any]) -> bool:
    """A legacy ``custom.json`` synthetic entry (``family: custom``).

    A catalog aggregator line also carries ``family``, so the presence of
    the key never identifies a custom entry; only its value does.
    """

    return entry.get("family") == LEGACY_CUSTOM_FAMILY


def line_family(entry: dict[str, Any], providers: dict[str, Any]) -> str:
    """Independence family of a line: its own declared family (aggregator
    lines, custom synthetic entries), else its provider's."""

    return entry.get("family") or providers[entry["provider"]]["independence_family"]


def resolve_key_in(
    lines: Mapping[str, Any], retired: Mapping[str, Any], key: str
) -> KeyResolution:
    """Map a (possibly retired) model key to its live line.

    A live key resolves to itself with no notice. A retired key follows
    its successor chain; a chain ending in ``null`` resolves to None and
    the notice says a model choice is needed. Unknown keys raise. Pure: the
    shared rule behind :meth:`Catalog.resolve_key` and profile evaluation,
    which runs on raw documents before a Catalog exists.
    """

    if key in lines:
        return KeyResolution(requested=key, key=key, chain=(), notice=None)
    if key not in retired:
        raise CatalogError(f"unknown model key {key!r}")
    chain: list[str] = []
    current: str | None = key
    while current is not None and current in retired and current not in chain:
        chain.append(current)
        if len(chain) > _RETIRED_CHAIN_LIMIT:
            break
        current = retired[current]["successor"]
    entry = retired[key]
    since = entry["since_catalog"]
    if current is None or current not in lines:
        return KeyResolution(
            requested=key,
            key=None,
            chain=tuple(chain),
            notice=f"{key} was retired in catalog {since} (no successor): needs a model choice",
        )
    return KeyResolution(
        requested=key,
        key=current,
        chain=tuple(chain),
        notice=f"{key} was retired in catalog {since} -> {current}",
    )


@dataclass(frozen=True)
class Catalog:
    """Immutable view of the validated trusted asset bundle.

    ``contract_source`` records which native contract is in effect. It is
    always the packaged one; the value says whether an old-format operator
    override file exists beside it: ``"packaged"`` (none),
    ``"override-ignored-stale"`` (it pins a version that is not newer than
    the packaged pin) or ``"override-ignored-newer"`` (it pins a newer
    version, which this release has no verified build of). An override is
    never applied: a release runs only the Claude Code version it pins.
    """

    root: Path
    docs: dict[str, Any]
    prompt_bodies: dict[str, bytes]
    bundle: dict[str, Any]
    bundle_sha256: str
    contract_source: str = "packaged"
    contract_override_detail: str | None = None

    @property
    def providers(self) -> dict[str, Any]:
        return self.docs["providers"]["providers"]

    @property
    def lines(self) -> dict[str, Any]:
        """Every v2 model line, ``status: new`` included (raw catalog data)."""

        return self.docs["models-v2"]["models"]

    @property
    def retired(self) -> dict[str, Any]:
        return self.docs["retired"]["retired"]

    def resolve_key(self, key: str) -> "KeyResolution":
        """Map a (possibly retired) model key to its live line (:func:`resolve_key_in`)."""

        return resolve_key_in(self.lines, self.retired, key)

    def retired_selector_index(self) -> dict[str, tuple[str, dict[str, Any], str | None]]:
        """Each retired selector base -> (retired key, entry, proxy contract)."""

        index: dict[str, tuple[str, dict[str, Any], str | None]] = {}
        for key in sorted(self.retired):
            entry = self.retired[key]
            for selector, contract in sorted(entry["selectors"].items()):
                index.setdefault(_selector_base(selector), (key, entry, contract))
        return index

    def resolve_selector(self, selector: str) -> tuple[str, dict[str, Any]] | None:
        """A retired selector (with or without ``[1m]``) -> (retired key, entry)."""

        found = self.retired_selector_index().get(_selector_base(selector))
        if found is None:
            return None
        return found[0], found[1]

    @property
    def roles(self) -> dict[str, Any]:
        """Alias of :attr:`roles_v2` (the legacy roles view is gone)."""

        return self.docs["roles-v2"]["roles"]

    @property
    def roles_v2(self) -> dict[str, Any]:
        """Every roles v2 entry (all ten ids; raw catalog data)."""

        return self.docs["roles-v2"]["roles"]

    @property
    def seed_profiles(self) -> dict[str, dict[str, Any]]:
        """The catalog seed profiles by name (deep copies are the caller's job)."""

        return {name: self.docs[f"profiles/{name}"] for name in SEED_PROFILE_NAMES}

    @property
    def agent_efforts(self) -> tuple[str, ...]:
        """Efforts an agent definition may carry (never ``ultracode``)."""

        return tuple(self.docs["native-contract"]["agent_efforts"]["values"])

    @property
    def lead_efforts(self) -> tuple[str, ...]:
        """Efforts a lead may carry: the agent efforts plus ``ultracode``."""

        return tuple(self.docs["native-contract"]["lead_efforts"]["values"])


def _check_effort_contract(contract: dict[str, Any], where: str) -> list[str]:
    """The effort split's cross-field rule (the schema checks each list).

    Every agent effort must also be a lead effort, and ``ultracode`` is a
    lead-only effort that must be present: the pinned client drops agent
    ``ultracode`` silently, so the lead list is the only
    place it may live.
    """

    errors: list[str] = []
    agent = list(contract["agent_efforts"]["values"])
    lead = list(contract["lead_efforts"]["values"])
    missing = [value for value in agent if value not in lead]
    if missing:
        errors.append(
            f"{where}.lead_efforts: must contain every agent effort; missing {missing}"
        )
    if "ultracode" not in lead:
        errors.append(f"{where}.lead_efforts: must contain 'ultracode'")
    return errors


def _check_verified_pins(contract: dict[str, Any], where: str) -> list[str]:
    """Each verified pin lists at least one platform build (the schema
    closes the platform names and each record's shape)."""

    errors: list[str] = []
    for index, item in enumerate(contract["verified"]):
        if not item["platforms"]:
            errors.append(f"{where}.verified[{index}].platforms: must list at least one platform")
    return errors


def is_legacy_contract_override(document: Any) -> bool:
    """A legacy operator contract override (the single effort vocabulary).

    The current contract splits ``effort_vocabulary`` into
    ``agent_efforts``/``lead_efforts``; an override written by an earlier
    launcher's ``claude-multi update`` still carries the old key and can
    never load again.
    """

    return (
        isinstance(document, dict)
        and "effort_vocabulary" in document
        and "lead_efforts" not in document
    )


@dataclass(frozen=True)
class LegacyOverrideRemoval:
    """What :func:`remove_legacy_contract_override` removed."""

    path: Path
    pinned_version: str | None
    packaged_version: str


def remove_legacy_contract_override(
    path: Path | str, *, packaged: dict[str, Any]
) -> LegacyOverrideRemoval | None:
    """Remove a legacy operator contract override, unconditionally.

    Returns the removal record, or None when nothing was removed: the file
    is absent, its lock is held (an ``update`` is writing it), it is not
    safely readable (symlink, foreign owner or group/other mode keep the
    BLOCK path), it is not strict JSON, it is not legacy-shaped, or any state
    check or OS call fails. It never raises: this runs inside the Runtime's
    degrade branch, which must never brick the CLI.
    """

    from . import state  # local import: state hardening for the config root

    target = Path(path)
    if not os.path.lexists(target):
        return None
    lock = state.FileLock(target)
    try:
        if not lock.acquire(blocking=False):
            return None
    except (state.StateError, OSError):
        return None
    try:
        try:
            raw = state.read_private(target)
            document = strict_json.loads(raw)
        except (state.StateError, OSError, strict_json.StrictJSONError):
            return None
        if not is_legacy_contract_override(document):
            return None
        claude = document.get("claude")
        pinned = claude.get("validated_version") if isinstance(claude, dict) else None
        if not isinstance(pinned, str):
            pinned = None
        try:
            state.remove_private(target)
        except (state.StateError, OSError):
            return None
        return LegacyOverrideRemoval(
            path=target,
            pinned_version=pinned,
            packaged_version=_pinned_version(packaged),
        )
    finally:
        lock.release()


def _selector_base(selector: str) -> str:
    if selector.endswith(_SELECTOR_SUFFIX):
        return selector[: -len(_SELECTOR_SUFFIX)]
    return selector


def _secret_scan(node: Any, path: str, errors: list[str], key: str | None = None) -> None:
    if isinstance(node, dict):
        for child_key in sorted(node):
            _secret_scan(node[child_key], f"{path}.{child_key}", errors, child_key)
        return
    if isinstance(node, list):
        for index, item in enumerate(node):
            _secret_scan(item, f"{path}[{index}]", errors, key)
        return
    if not isinstance(node, str):
        return
    if key in _HASH_FIELDS or _ENV_REF.fullmatch(node) or node.startswith("sha256:"):
        return
    for shape in _SECRET_SHAPES:
        if shape.search(node):
            errors.append(f"{path}: secret-like value is forbidden in trusted data")
            return


_DATA_VERSION_DETAIL = {
    "models": " (this launcher reads models v2, catalog 33+)",
    "roles": " (this launcher reads roles v2, catalog 33+)",
    "native-contract": " (this launcher reads the path-free native contract v2)",
}
_SEED_VERSION_DETAIL = " (seed profiles are v2)"


def _data_version_detail(key: str) -> str:
    if key.startswith("profiles/"):
        return _SEED_VERSION_DETAIL
    return _DATA_VERSION_DETAIL.get(key, "")


def _expected_data_version(key: str) -> int:
    """The per-document data version ``load_raw`` gates on."""

    if key == "models":
        return MODELS_DATA_VERSION
    if key == "roles":
        return ROLES_DATA_VERSION
    if key == "native-contract":
        return NATIVE_CONTRACT_DATA_VERSION
    if key.startswith("profiles/"):
        return PROFILE_DATA_VERSION
    return SUPPORTED_DATA_VERSION


def load_raw(root: Path | str) -> dict[str, Any]:
    """Load schemas, documents, and prompt bodies without semantic checks.

    Raises CatalogError on any strict-JSON or schema violation. Semantic
    reference validation runs separately in :func:`validate_catalog` so tests
    can exercise mutated copies.
    """

    root = Path(root)
    if not root.is_dir():
        raise CatalogError(f"{root}: catalog root is not a directory")

    errors: list[str] = []
    schemas: dict[str, Any] = {}
    for name in SCHEMA_NAMES:
        relative = f"schemas/{name}.schema.json"
        try:
            schema = strict_json.load(root / relative)
        except strict_json.StrictJSONError as exc:
            raise CatalogError(f"{relative}: {exc}") from exc
        try:
            schema_validate.check_schema(schema)
        except schema_validate.SchemaError as exc:
            raise CatalogError(f"{relative}: {exc}") from exc
        schemas[name] = schema

    docs: dict[str, Any] = {}
    # (relative path, schema name, versioned): settings.json is consumed
    # directly by Claude Code and is not a versioned catalog document.
    documents = {
        "version": ("version.json", None, True),
        "settings": ("settings.json", None, False),
        "gateway": ("catalog/gateway.json", "gateway", True),
        "providers": ("catalog/providers.json", "providers", True),
        "models": ("catalog/models.json", "models", True),
        "retired": ("catalog/retired.json", "retired", True),
        "roles": ("catalog/roles.json", "roles", True),
        "native-contract": ("catalog/native-contract.json", "native-contract", True),
        # The seed profiles are required catalog documents.
        **{
            f"profiles/{name}": (f"catalog/profiles/{name}.json", "profile", True)
            for name in SEED_PROFILE_NAMES
        },
    }
    # The catalog carries no legacy composition, so ``catalog/compositions/``
    # is never read.
    for key, (relative, schema_name, versioned) in documents.items():
        try:
            document = strict_json.load(root / relative)
        except strict_json.StrictJSONError as exc:
            raise CatalogError(f"{relative}: {exc}") from exc
        if not isinstance(document, dict):
            raise CatalogError(f"{relative}: top-level document must be an object")
        # The per-document data-version gate runs before the schema, so an
        # older document gets the friendly version error rather than the
        # schema's const/required noise.
        expected = _expected_data_version(key)
        if versioned and document.get("version") != expected:
            raise CatalogError(
                f"{relative}: unsupported data version {document.get('version')!r}"
                f"{_data_version_detail(key)}"
            )
        if schema_name is not None:
            problems = schema_validate.validate(document, schemas[schema_name], "$")
            if problems:
                raise CatalogError(f"{relative}: " + "; ".join(problems))
        docs[key] = document

    profiles_dir = root / "catalog" / "profiles"
    for path in sorted(profiles_dir.glob("*.json")):
        if path.stem not in SEED_PROFILE_NAMES:
            raise CatalogError(
                f"catalog/profiles/{path.stem}.json: unknown seed profile "
                f"(seeds are {', '.join(SEED_PROFILE_NAMES)})"
            )

    prompt_bodies: dict[str, bytes] = {}
    catalog_dir = root / "catalog"
    for role_id, role in sorted(docs["roles"]["roles"].items()):
        prompt_relative = role["prompt_file"]
        prompt_path = catalog_dir / prompt_relative
        resolved = prompt_path.resolve()
        if not resolved.is_relative_to(catalog_dir.resolve()):
            raise CatalogError(
                f"roles.{role_id}.prompt_file: escapes the trusted catalog root"
            )
        if prompt_path.is_symlink() or not prompt_path.is_file():
            raise CatalogError(
                f"roles.{role_id}.prompt_file: not a regular file in the catalog"
            )
        prompt_bodies[role_id] = prompt_path.read_bytes()

    return {"root": root, "docs": docs, "prompt_bodies": prompt_bodies, "schemas": schemas}


def _check_versions_settings(docs: dict[str, Any], errors: list[str]) -> None:
    version_doc = docs["version"]
    for key in ("version", "launcher_version", "catalog_version"):
        if key not in version_doc:
            errors.append(f"version.json: missing {key!r}")
    settings = docs["settings"]
    if "version" in settings:
        errors.append("settings.json: must not contain a 'version' key")
    for key in sorted(settings):
        if key not in SETTINGS_ALLOWED_KEYS:
            errors.append(f"settings.json: unverified settings key {key!r}")


def _check_provider_routes(providers: dict[str, Any], errors: list[str]) -> str | None:
    """Passthrough routes are data (§3.4; replaces CANONICAL_FORK_ROUTES).

    The schema pins the canonical Anthropic id shape and ``fork: true``;
    here: routes only on the Anthropic OAuth pool, no duplicates, the
    reserved sentinel prefix refused, and every client refusal-fallback
    target present. Returns the pool provider id (None if absent/ambiguous).
    """

    pools = sorted(
        provider_id
        for provider_id, provider in providers.items()
        if provider["adapter"] == _ANTHROPIC_POOL_ADAPTER
    )
    if len(pools) > 1:
        errors.append(
            f"providers: more than one Anthropic OAuth pool provider {pools}"
        )
    for provider_id in sorted(providers):
        provider = providers[provider_id]
        routes = provider["passthrough_routes"]
        if routes and provider["adapter"] != _ANTHROPIC_POOL_ADAPTER:
            errors.append(
                f"providers.{provider_id}: passthrough routes are only allowed on "
                "the Anthropic OAuth pool"
            )
        seen_routes: set[str] = set()
        for route in routes:
            name = route["name"]
            if name.startswith(_RENDER_SENTINEL_PREFIX):
                errors.append(f"providers.{provider_id}: route {name!r} uses reserved render sentinel prefix")
            if name in seen_routes:
                errors.append(f"providers.{provider_id}: duplicate passthrough route {name!r}")
            seen_routes.add(name)
        if provider["adapter"] == _ANTHROPIC_POOL_ADAPTER:
            for wire in REFUSAL_FALLBACK_WIRES:
                if wire not in seen_routes:
                    errors.append(
                        f"providers.{provider_id}: missing refusal-fallback route {wire}"
                    )
    return pools[0] if len(pools) == 1 else None


def _check_account_pools(providers: dict[str, Any], errors: list[str]) -> None:
    """Every OAuth-pool provider names an account pool of this build
    (``account-pools.json``) that names it back as its provider."""

    from claude_multi import account_pools

    try:
        table = account_pools.load()
    except account_pools.AccountPoolsError as exc:
        errors.append(f"providers: {exc}")
        return
    for provider_id in sorted(providers):
        transport = providers[provider_id]["transport"]
        if transport["kind"] != "oauth-pool":
            continue
        pool = table.pools.get(transport["pool"])
        if pool is None:
            errors.append(f"providers.{provider_id}: unknown account pool {transport['pool']!r}")
        elif pool.provider != provider_id:
            errors.append(f"providers.{provider_id}: account pool {pool.name!r} belongs to provider "
                          f"{pool.provider!r}")


def _check_context(where: str, entry: dict[str, Any], errors: list[str]) -> None:
    context = entry["context"]
    client_tokens = context["client_tokens"]
    provider_tokens = context["provider_tokens"]
    scalar_tokens = context["scalar_tokens"]
    if provider_tokens > client_tokens:
        errors.append(
            f"{where}.context.provider_tokens: {provider_tokens} exceeds "
            f"client_tokens {client_tokens}"
        )
    if provider_tokens > context["declared_tokens"]:
        errors.append(
            f"{where}.context.provider_tokens: {provider_tokens} exceeds "
            f"declared_tokens {context['declared_tokens']}"
        )
    if context["validated_tokens"] > context["declared_tokens"]:
        errors.append(
            f"{where}.context.validated_tokens: {context['validated_tokens']} "
            f"exceeds declared_tokens {context['declared_tokens']}"
        )
    if (
        context["validated_tokens"] < provider_tokens
        and "user_reported_tokens" in context
    ):
        if context["user_reported_tokens"] != provider_tokens:
            errors.append(
                f"{where}.context.provider_tokens: a user-attested configured "
                "bound must match user_reported_tokens"
            )
        if "not benchmark-verified" not in context["qualification"]:
            errors.append(
                f"{where}.context.qualification: a user-attested configured "
                "bound must say it is not benchmark-verified"
            )
    if scalar_tokens is not None and scalar_tokens > provider_tokens:
        errors.append(
            f"{where}.context.scalar_tokens: {scalar_tokens} exceeds "
            f"provider_tokens {provider_tokens}"
        )
    ordinary_profile = context["ordinary_profile"]
    if "lead" in entry["capabilities"] and ordinary_profile is None:
        errors.append(
            f"{where}.context.ordinary_profile: lead-capable models must belong "
            "to an ordinary gateway profile"
        )
    if "lead" not in entry["capabilities"] and ordinary_profile is not None:
        errors.append(
            f"{where}.context.ordinary_profile: agents-only models cannot be "
            "ordinary gateway leads"
        )
    if ordinary_profile == "large" and client_tokens < 1_000_000:
        errors.append(
            f"{where}.context.ordinary_profile: large profile requires a 1M "
            "client context"
        )


def _check_effort_shape(
    where: str,
    entry: dict[str, Any],
    provider_id: str,
    provider: dict[str, Any],
    agent: frozenset[str],
    errors: list[str],
) -> bool:
    """Effort shape by the provider's effort mode; False if unusable."""

    efforts = entry["efforts"]
    default = entry["default_effort"]
    ok = True
    if effort_mode(provider) == "client":
        if "selector" not in entry:
            errors.append(f"{where}.selector: a client-effort line requires a selector")
            ok = False
        if not isinstance(efforts, list):
            errors.append(
                f"{where}.efforts: a client-effort line (provider {provider_id!r}) "
                "declares an efforts list, not a map"
            )
            return False
        for effort in efforts:
            if effort not in agent:
                errors.append(
                    f"{where}.efforts: effort {effort!r} is outside the trusted "
                    f"agent efforts {sorted(agent)}"
                )
        if default not in efforts:
            errors.append(
                f"{where}.default_effort: {default!r} is not one of the line's efforts"
            )
            ok = False
        return ok
    if "selector" in entry:
        errors.append(
            f"{where}.selector: a gateway-effort line (provider {provider_id!r}) "
            "carries one selector per effort, never a line selector"
        )
    if not isinstance(efforts, dict) or not efforts:
        errors.append(
            f"{where}.efforts: a gateway-effort line (provider {provider_id!r}) "
            "declares a non-empty {level: {selector, proxy_contract}} map"
        )
        return False
    for level in sorted(efforts):
        if level not in agent:
            errors.append(
                f"{where}.efforts.{level}: effort {level!r} is outside the trusted "
                f"agent efforts {sorted(agent)}"
            )
        contract = efforts[level]["proxy_contract"]
        if contract not in provider["payload_contracts"]:
            errors.append(
                f"{where}.efforts.{level}.proxy_contract: proxy effort contract "
                f"{contract!r} is not declared by provider {provider_id!r}"
            )
    if default not in efforts:
        errors.append(
            f"{where}.default_effort: {default!r} is not one of the line's efforts"
        )
        return False
    return True


def _anthropic_pool_id(providers: Mapping[str, Any]) -> str | None:
    """The single Anthropic OAuth pool provider id (None if absent/ambiguous)."""

    pools = sorted(
        provider_id
        for provider_id, provider in providers.items()
        if provider["adapter"] == _ANTHROPIC_POOL_ADAPTER
    )
    return pools[0] if len(pools) == 1 else None


def _check_operator_effort_shape(
    where: str,
    entry: dict[str, Any],
    provider_id: str,
    provider: dict[str, Any],
    agent: frozenset[str],
    origin: str,
    errors: list[str],
) -> bool:
    """Operator-origin effort shape: the mode comes from the entry's own
    ``efforts`` shape (a list is client-effort), never from ``family``.

    A list is allowed on a client-effort provider and, with no contract, on a
    Claude-compatible direct provider; a map needs a gateway-effort provider
    and selects that provider's reviewed contracts. ``operator-migrated`` is
    the narrow legacy exception: exactly ``["high"]`` with the exact
    legacy selector, even on a provider with reviewed gateway contracts.
    """

    efforts = entry["efforts"]
    default = entry["default_effort"]
    adapter = provider["adapter"]
    if origin == "operator-migrated":
        ok = True
        if efforts != ["high"] or default != "high":
            errors.append(
                f"{where}.efforts: a migrated legacy line keeps exactly [\"high\"] "
                "with default_effort high"
            )
            ok = False
        if provider["transport"]["kind"] == "oauth-pool" or adapter not in (
            "cliproxy-claude-compatible-v1", "cliproxy-openai-compat-v1"
        ):
            errors.append(
                f"{where}.provider: a migrated legacy line needs a direct provider, "
                f"not {provider_id!r}"
            )
            ok = False
        return ok
    if isinstance(efforts, list):
        if effort_mode(provider) != "client" and adapter != "cliproxy-claude-compatible-v1":
            errors.append(
                f"{where}.efforts: provider {provider_id!r} is gateway-effort; declare "
                "a {level: contract} map of its reviewed contracts"
            )
            return False
        if "selector" not in entry:
            errors.append(f"{where}.selector: a client-effort line requires a selector")
            return False
        for effort in efforts:
            if effort not in agent:
                errors.append(
                    f"{where}.efforts: effort {effort!r} is outside the trusted "
                    f"agent efforts {sorted(agent)}"
                )
        if default not in efforts:
            errors.append(
                f"{where}.default_effort: {default!r} is not one of the line's efforts"
            )
            return False
        return True
    return _check_effort_shape(where, entry, provider_id, provider, agent, errors)


def _check_line_family(where: str, entry: Mapping[str, Any], provider_id: str) -> list[str]:
    """A catalog line declares ``family`` exactly when its provider is an
    aggregator, and never ``custom`` or ``unknown``."""

    family = entry.get("family")
    if provider_id not in AGGREGATOR_PROVIDERS:
        if family is not None:
            return [f"{where}.family: only a line of an aggregator provider "
                    f"({', '.join(sorted(AGGREGATOR_PROVIDERS))}) declares a family"]
        return []
    if family is None:
        return [f"{where}.family: a line of aggregator provider {provider_id!r} declares its family"]
    if family in UNDECLARED_FAMILIES:
        return [f"{where}.family: {family!r} is not a declared family"]
    return []


def _check_overlay_marker(
    where: str, overlay: Any, provider_id: str, provider: dict[str, Any] | None,
    wire: str, errors: list[str],
) -> None:
    """A non-null ``registry_overlay`` is the ``{"channel"}`` marker of
    an OAuth pool whose ``transport.pool`` is that channel; its registration
    name is the wire, which must satisfy the overlay loader's name grammar."""

    if overlay is None:
        return
    channel = overlay.get("channel") if isinstance(overlay, dict) else None
    if provider is None:
        return
    transport = provider.get("transport", {})
    if transport.get("kind") != "oauth-pool" or transport.get("pool") != channel:
        errors.append(
            f"{where}.registry_overlay: channel {channel!r} is not the OAuth pool of "
            f"provider {provider_id!r}"
        )
    if not OVERLAY_NAME.fullmatch(wire):
        errors.append(
            f"{where}.registry_overlay: wire {wire!r} is not a valid overlay model name"
        )


def validate_line(
    key: str,
    entry: dict[str, Any],
    *,
    providers: Mapping[str, Any],
    native_contract: Mapping[str, Any],
    taken_selectors: dict[str, str],
    origin: str = "catalog",
    taken_routes: dict[str, str] | None = None,
    role_ids: Sequence[str] | None = None,
) -> list[str]:
    """Per-line semantic checks shared by the catalog and the operator layer.

    ``taken_selectors`` (selector base -> line key) is claimed in place, so a
    caller validating lines in order gets the catalog's duplicate-selector
    verdicts; ``taken_routes`` (``provider:wire`` -> where), when given, does
    the same for the one-entry-per-(provider, wire) rule. Cross-line provider
    and retired graph checks stay in :func:`validate_catalog`. For the
    catalog origin the verdicts and their order are exactly the earlier
    model loop's; the ``custom-`` namespace and the overlay marker checks are
    appended after them. ``role_ids`` defaults to :data:`AGENT_ROLE_IDS`.
    """

    if origin not in _VALIDATED_ORIGINS:
        raise ValueError(f"validate_line: unsupported line origin {origin!r}")
    errors: list[str] = []
    where = f"models.{key}"
    agent = frozenset(native_contract["agent_efforts"]["values"])
    lead_efforts = tuple(native_contract["lead_efforts"]["values"])
    roles_known = list(AGENT_ROLE_IDS if role_ids is None else role_ids)
    operator = origin != "catalog"
    if origin == "catalog":
        if not LINE_KEY.fullmatch(key):
            errors.append(f"{where}: invalid line key {key!r}")
    elif not (OPERATOR_KEY if origin == "operator" else MIGRATED_KEY).fullmatch(key):
        errors.append(f"{where}: invalid {origin} line key {key!r}")
    provider_id = entry["provider"]
    if provider_id not in providers:
        errors.append(f"{where}.provider: unknown provider {provider_id!r}")
        return errors
    provider = providers[provider_id]
    # One entry per (provider, wire): two entries with the same wire on
    # the SAME provider would make listing/radar attribution ambiguous
    # The same wire on DIFFERENT providers is the multi-route
    # convention — each entry is its own route.
    if taken_routes is not None:
        route_key = f"{provider_id}:{entry['wire_model']}"
        if route_key in taken_routes:
            errors.append(
                f"{where}.wire_model: duplicate route wire "
                f"{entry['wire_model']!r} on provider {provider_id!r} "
                f"(already {taken_routes[route_key]})"
            )
        else:
            taken_routes[route_key] = where
    route_owner = {
        route["name"]: pid
        for pid in sorted(providers)
        for route in providers[pid]["passthrough_routes"]
    }
    # An operator line on the Anthropic pool rides the overlay,
    # not a pre-authored passthrough route.
    if not operator and provider_id == _anthropic_pool_id(providers) and entry["wire_model"] not in {
        route["name"] for route in provider["passthrough_routes"]
    }:
        errors.append(
            f"{where}.wire_model: Anthropic line wire {entry['wire_model']} must be "
            "a passthrough route"
        )
    _check_context(where, entry, errors)
    if operator:
        shape_ok = _check_operator_effort_shape(
            where, entry, provider_id, provider, agent, origin, errors
        )
    else:
        shape_ok = _check_effort_shape(where, entry, provider_id, provider, agent, errors)
    if not shape_ok:
        return errors

    client_tokens = entry["context"]["client_tokens"]
    client_pool = provider_id == _anthropic_pool_id(providers) and isinstance(entry["efforts"], list)
    for level, selector, _contract in line_selectors(entry):
        sel_where = (
            f"{where}.selector"
            if isinstance(entry["efforts"], list)
            else f"{where}.efforts.{level}.selector"
        )
        if selector.startswith(_RENDER_SENTINEL_PREFIX):
            errors.append(f"{sel_where}: reserved render sentinel prefix")
        if origin != "operator-migrated" and (
            selector.endswith(_SELECTOR_SUFFIX) != (client_tokens >= 1_000_000)
        ):
            errors.append(
                f"{where}.context.client_tokens: [1m] selector classification of "
                f"{selector!r} and client_tokens={client_tokens} disagree"
            )
        base = _selector_base(selector)
        if base in taken_selectors:
            errors.append(
                f"{sel_where}: client selector {selector!r} duplicates "
                f"models.{taken_selectors[base]}"
            )
        else:
            taken_selectors[base] = key
        owner = route_owner.get(base)
        if owner == provider_id:
            if base != entry["wire_model"]:
                errors.append(
                    f"{sel_where}: selector rides route {base} but names wire "
                    f"{entry['wire_model']}"
                )
        elif owner is not None:
            errors.append(
                f"{sel_where}: selector {selector!r} rides another provider's "
                f"passthrough route {base!r}"
            )

    lead = entry["lead"]
    if lead is not None:
        if lead["effort"] not in lead_efforts:
            errors.append(
                f"{where}.lead.effort: {lead['effort']!r} is outside the trusted "
                f"lead efforts {list(lead_efforts)}"
            )
        for env_key, env_value in lead["env"].items():
            if env_key in RESERVED_LEAD_ENV_KEYS:
                errors.append(
                    f"{where}.lead.env: {env_key!r} is compiler-owned and reserved"
                )
            elif not env_key.startswith("CLAUDE_CODE_"):
                errors.append(f"{where}.lead.env: unexpected variable {env_key!r}")
            if env_value.isdigit() and int(env_value) <= 0:
                errors.append(
                    f"{where}.lead.env: {env_key!r} must be a positive integer"
                )
    if "lead" in entry["capabilities"] and lead is None:
        errors.append(f"{where}: lead capability requires a lead block")
    if "lead" not in entry["capabilities"] and lead is not None:
        errors.append(f"{where}.lead: an agents-only line must have lead: null")

    line_roles = entry["roles"]
    if line_roles != "all":
        for role_id in line_roles:
            if role_id == LEAD_ROLE:
                errors.append(
                    f"{where}.roles: {LEAD_ROLE!r} is implied by the lead "
                    "capability, never listed"
                )
            elif role_id not in roles_known:
                errors.append(f"{where}.roles: unknown role {role_id!r}")
    if ("agents" in entry["capabilities"]) != (line_roles != []):
        errors.append(
            f"{where}.roles: the agents capability requires at least one role "
            "(and roles require the agents capability)"
        )

    # Operator-layer checks, after every earlier verdict (ordering preserved).
    selector_bases = [_selector_base(selector) for _l, selector, _c in line_selectors(entry)]
    if not operator:
        errors.extend(_check_line_family(where, entry, provider_id))
        if key.startswith(CUSTOM_NAMESPACE):
            errors.append(
                f"{where}: the {CUSTOM_NAMESPACE} key namespace is reserved for operator lines"
            )
        for base in selector_bases:
            if base.startswith(CUSTOM_NAMESPACE):
                errors.append(
                    f"{where}: selector {base!r} uses the reserved {CUSTOM_NAMESPACE} namespace"
                )
        _check_overlay_marker(
            where, entry.get("registry_overlay"), provider_id, provider,
            entry["wire_model"], errors,
        )
        errors.extend(_check_key_route(where, entry, provider, operator=False))
    else:
        errors.extend(_check_key_route(where, entry, provider, operator=True))
        if lead is not None:
            if lead["env"]:
                errors.append(f"{where}.lead.env: an operator line has no lead env")
            if lead["effort"] != entry["default_effort"]:
                errors.append(
                    f"{where}.lead.effort: an operator lead runs at the declared "
                    f"default effort {entry['default_effort']!r}"
                )
        if entry.get("registry_overlay") is not None:
            errors.append(
                f"{where}.registry_overlay: an operator line derives its overlay "
                "entry; it carries no registry_overlay"
            )
        for base in selector_bases:
            if origin == "operator-migrated":
                if base != key or entry.get("selector") != key:
                    errors.append(
                        f"{where}.selector: a migrated legacy line keeps the exact "
                        f"selector {key!r}"
                    )
            elif client_pool:
                if base != entry["wire_model"]:
                    errors.append(
                        f"{where}.selector: an Anthropic pool line uses its canonical "
                        f"wire {entry['wire_model']!r} as the selector"
                    )
            elif not base.startswith(key):
                errors.append(
                    f"{where}.selector: {base!r} is outside the line's {key}* namespace"
                )
    return errors


def _check_retired(
    docs: dict[str, Any],
    models: dict[str, Any],
    providers: dict[str, Any],
    live_selectors: dict[str, str],
    route_owner: dict[str, str],
    errors: list[str],
) -> None:
    retired = docs["retired"]["retired"]
    catalog_version = docs["version"].get("catalog_version")
    seen_bases: dict[str, str] = {}
    for key in sorted(retired):
        entry = retired[key]
        where = f"retired.{key}"
        match = RETIRED_KEY.fullmatch(key)
        if match is None:
            errors.append(f"{where}: invalid retired key {key!r}")
            continue
        base, gen = match.group(1), match.group(2)
        if gen is None:
            if key in models:
                errors.append(f"{where}: retired key {key} is also a live line")
        else:
            if base not in models:
                errors.append(f"{where}: {base!r} is not a live line")
            else:
                if entry["successor"] != base:
                    errors.append(f"{where}.successor: must be the live line {base!r}")
                if entry.get("generation") != gen:
                    errors.append(f"{where}.generation: must be present and equal {gen!r}")
                if models[base].get("generation") == gen:
                    errors.append(
                        f"{where}: generation {gen!r} is the live line's current generation"
                    )
        if isinstance(catalog_version, int) and entry["since_catalog"] > catalog_version:
            errors.append(
                f"{where}.since_catalog: {entry['since_catalog']} is newer than "
                f"catalog_version {catalog_version}"
            )
        provider_id = entry["provider"]
        provider = providers.get(provider_id)
        # An externalized-provider retirement names a provider that
        # left the trusted catalog (its reviewed sample ships under
        # ``examples/providers.d/``); the historical aliases stay continuity
        # data, served only while an operator declares that provider.
        externalized = entry.get("externalized")
        if externalized is not None:
            if provider is not None:
                errors.append(
                    f"{where}.externalized: provider {provider_id!r} is still a catalog "
                    "provider"
                )
            if entry["successor"] is not None:
                errors.append(
                    f"{where}.externalized: an externalized retirement has no automatic "
                    "successor (successor must be null)"
                )
            if entry.get("registry_overlay") is not None:
                errors.append(
                    f"{where}.externalized: an externalized provider has no OAuth overlay"
                )
            for selector, contract in sorted(entry["selectors"].items()):
                if contract is not None:
                    errors.append(
                        f"{where}.selectors.{selector}: an externalized provider's "
                        "selectors carry no proxy contract (null)"
                    )
        elif provider is None:
            errors.append(f"{where}.provider: unknown provider {provider_id!r}")
        # Like a line, a retired entry of an aggregator declares its model's
        # family: the owner its retained aliases report.
        errors.extend(_check_line_family(where, entry, provider_id))

        # Successor chain: at most 8 hops, no cycles, ending in null or an
        # active live line.
        chain = [key]
        current = entry["successor"]
        terminal: str | None = None
        broken = False
        while current is not None:
            if current in chain:
                errors.append(
                    f"{where}.successor: cycle {' -> '.join([*chain, current])}"
                )
                broken = True
                break
            if current in models:
                terminal = current
                break
            if current not in retired:
                errors.append(f"{where}.successor: unknown key {current!r}")
                broken = True
                break
            chain.append(current)
            if len(chain) > _RETIRED_CHAIN_LIMIT + 1:
                errors.append(
                    f"{where}.successor: chain longer than {_RETIRED_CHAIN_LIMIT} hops"
                )
                broken = True
                break
            current = retired[current]["successor"]
        if not broken and terminal is not None:
            target = models[terminal]
            if target.get("status") != "active":
                errors.append(
                    f"{where}.successor: chain ends in {terminal!r}, which is not an "
                    "active line (status new)"
                )
            missing: list[str] = sorted(
                set(entry["capabilities"]) - set(target["capabilities"])
            )
            if entry["roles"] == "all":
                if target["roles"] != "all":
                    missing.append("roles all")
            elif target["roles"] != "all":
                missing.extend(sorted(set(entry["roles"]) - set(target["roles"])))
            if missing:
                errors.append(
                    f"{where}: successor {terminal} does not admit roles/capabilities "
                    f"{missing}"
                )

        # Retired roles are history: pattern-checked by the schema only.
        selectors = entry["selectors"]
        if not selectors:
            errors.append(f"{where}.selectors: must name at least one legacy selector")
        for selector in sorted(selectors):
            sel_where = f"{where}.selectors.{selector}"
            if not SELECTOR_PATTERN.fullmatch(selector):
                errors.append(f"{sel_where}: invalid selector")
                continue
            if selector.startswith(_RENDER_SENTINEL_PREFIX):
                errors.append(f"{sel_where}: reserved render sentinel prefix")
            contract = selectors[selector]
            if (
                contract is not None
                and provider is not None
                and contract not in provider["payload_contracts"]
            ):
                errors.append(
                    f"{sel_where}: proxy effort contract {contract!r} is not declared "
                    f"by provider {provider_id!r}"
                )
            sel_base = _selector_base(selector)
            if sel_base in seen_bases:
                errors.append(
                    f"{sel_where}: selector base {sel_base!r} duplicates "
                    f"{seen_bases[sel_base]}"
                )
            else:
                seen_bases[sel_base] = where
            if sel_base in live_selectors:
                line = models[live_selectors[sel_base]]
                if line["provider"] != provider_id or line["wire_model"] != entry["last_wire"]:
                    errors.append(
                        f"{sel_where}: selector base {sel_base!r} is live on "
                        f"models.{live_selectors[sel_base]} with a different "
                        "provider or wire"
                    )
            if sel_base in route_owner and (
                route_owner[sel_base] != provider_id or sel_base != entry["last_wire"]
            ):
                errors.append(
                    f"{sel_where}: selector base {sel_base!r} is a passthrough route "
                    f"of {route_owner[sel_base]!r}; it must be on the same provider and "
                    "equal last_wire"
                )
        # Operator-layer checks (after every earlier verdict): the operator
        # namespace and the overlay marker of an overlay-served retired line.
        if base.startswith(CUSTOM_NAMESPACE) or any(
            _selector_base(selector).startswith(CUSTOM_NAMESPACE) for selector in selectors
        ):
            errors.append(
                f"{where}: the {CUSTOM_NAMESPACE} key and selector namespace is reserved "
                "for operator lines"
            )
        _check_overlay_marker(
            where, entry.get("registry_overlay"), provider_id, provider,
            entry["last_wire"], errors,
        )


EXAMPLES_PROVIDERS_DIR = "examples/providers.d"


def externalized_providers(docs: Mapping[str, Any]) -> dict[str, str]:
    """Provider id -> reviewed sample file name, for every
    externalized-provider retirement whose provider is not a catalog provider.

    Pure over the (possibly merged) docs: a provider an operator declared in
    ``providers.d`` under that id is present in the merged providers and is
    therefore not listed (its historical aliases render again).
    """

    providers = docs["providers"]["providers"]
    found: dict[str, str] = {}
    for key in sorted(docs["retired"]["retired"]):
        entry = docs["retired"]["retired"][key]
        marker = entry.get("externalized")
        if isinstance(marker, Mapping) and entry.get("provider") not in providers:
            found.setdefault(entry["provider"], marker["sample"])
    return found


def identity_tokens(docs: dict[str, Any]) -> frozenset[str]:
    """Lowercased names that identify a model, provider or family.

    Live line keys, retired keys and their ``@`` bases, provider ids,
    ``independence_family`` values and the families aggregator lines
    declare, minus :data:`IDENTITY_TOKEN_EXCLUSIONS`.
    """

    tokens: set[str] = set(docs["models"]["models"])
    for entry in docs["models"]["models"].values():
        family = entry.get("family") if isinstance(entry, dict) else None
        if isinstance(family, str):
            tokens.add(family)
    for key in docs["retired"]["retired"]:
        tokens.add(key)
        tokens.add(key.split("@", 1)[0])
    for provider_id, provider in docs["providers"]["providers"].items():
        tokens.add(provider_id)
        family = provider.get("independence_family") if isinstance(provider, dict) else None
        if isinstance(family, str):
            tokens.add(family)
    return frozenset(t.lower() for t in tokens) - IDENTITY_TOKEN_EXCLUSIONS


def _role_spelling(function: Any, grade: Any) -> str:
    return f"cm-{function}" + ("" if grade == "plain" else f"-{grade}")


def _check_roles_v2(docs: dict[str, Any], errors: list[str]) -> None:
    """Roles v2 validation; every message names the field."""

    roles = docs["roles"]["roles"]
    missing = sorted(set(ROLE_IDS) - set(roles))
    unexpected = sorted(set(roles) - set(ROLE_IDS))
    if missing or unexpected:
        errors.append(
            f"roles: the role set must be exactly {sorted(ROLE_IDS)}; "
            f"missing {missing}, unexpected {unexpected}"
        )
    tokens = sorted(identity_tokens(docs))
    for role_id in sorted(roles):
        if not ROLE_ID.fullmatch(role_id):
            errors.append(f"roles: invalid role ID {role_id!r}")
        if role_id not in ROLE_IDS:
            continue
        role = roles[role_id]
        function, grade = role.get("function"), role.get("grade")
        spelled = _role_spelling(function, grade)
        if role_id != spelled:
            errors.append(
                f"roles.{role_id}: function/grade spell {spelled}, not {role_id!r}"
            )
        expected: Any = [f"cm-{function}"] if grade in ("light", "strong") else []
        if role.get("requires") != expected:
            errors.append(f"roles.{role_id}.requires: must be {expected!r}")
        expected = "worktree" if function == "implementer" else None
        if role.get("isolation") != expected:
            errors.append(f"roles.{role_id}.isolation: must be {expected!r}")
        expected = list(READ_ONLY_TOOLS) if function in READ_ONLY_FUNCTIONS else []
        if role.get("disallowed_tools") != expected:
            errors.append(f"roles.{role_id}.disallowed_tools: must be {expected!r}")
        expected = f"prompts/cm-{function}.md"
        if role.get("prompt_file") != expected:
            errors.append(
                f"roles.{role_id}.prompt_file: must be {expected!r} (one prompt per function)"
            )
        description = role.get("description")
        if isinstance(description, str):
            for token in tokens:
                if re.search(rf"\b{re.escape(token)}\b", description, re.IGNORECASE):
                    errors.append(
                        f"roles.{role_id}.description: names a model, provider or "
                        f"family ({token!r})"
                    )
                    break


def _check_prompts(
    roles: dict[str, Any], prompt_bodies: dict[str, bytes], errors: list[str]
) -> None:
    """Prompt validation: one prompt per function.

    Ids of one function share one body; bodies of different functions are
    distinct (the representative is the plain-grade id).
    """

    for role_id in sorted(roles):
        body = prompt_bodies.get(role_id)
        if body is None:
            errors.append(f"roles.{role_id}: prompt body was not loaded")
        elif not body.strip():
            errors.append(f"roles.{role_id}: prompt body is empty")

    groups: dict[str, list[str]] = {}
    for role_id in sorted(roles):
        role = roles[role_id]
        function = role.get("function") if isinstance(role, dict) else None
        if isinstance(function, str) and role_id in prompt_bodies:
            groups.setdefault(function, []).append(role_id)

    for function in sorted(groups):
        first, *rest = groups[function]
        for role_id in rest:
            if prompt_bodies[role_id] != prompt_bodies[first]:
                errors.append(
                    f"roles.{role_id}: prompt body differs from {first!r} "
                    "(one prompt per function)"
                )

    seen: dict[str, str] = {}
    for function in sorted(groups):
        members = groups[function]
        plain = f"cm-{function}"
        representative = plain if plain in members else members[0]
        digest = strict_json.sha256_hex(prompt_bodies[representative])
        if digest in seen:
            errors.append(
                f"roles.{representative}: prompt body duplicates {seen[digest]!r}"
            )
        else:
            seen[digest] = representative


def validate_catalog(raw: dict[str, Any]) -> list[str]:
    """Semantic reference validation over loaded catalog data (models v2).

    ``raw["docs"]["models"]`` is the raw v2 document.
    """

    errors: list[str] = []
    docs = raw["docs"]
    providers = docs["providers"]["providers"]
    models = docs["models"]["models"]
    roles = docs["roles"]["roles"]
    native_contract = docs["native-contract"]
    role_ids = [role_id for role_id in roles if role_id != LEAD_ROLE]

    _check_versions_settings(docs, errors)
    errors.extend(_check_effort_contract(native_contract, "native-contract"))
    errors.extend(_check_verified_pins(native_contract, "native-contract"))

    _check_roles_v2(docs, errors)
    _check_prompts(roles, raw["prompt_bodies"], errors)

    _check_provider_routes(providers, errors)
    _check_account_pools(providers, errors)
    for provider_id in sorted(providers):
        # The shared keyed compat shape gate on trusted (T1) providers.
        keyed = keyed_compat_problem(providers[provider_id], docs["gateway"])
        if keyed is not None:
            errors.append(f"providers.{provider_id}: {keyed}")
    route_owner = {
        route["name"]: provider_id
        for provider_id in sorted(providers)
        for route in providers[provider_id]["passthrough_routes"]
    }

    live_selectors: dict[str, str] = {}  # selector base -> line key
    route_wires: dict[str, str] = {}
    for model_id in sorted(models):
        errors.extend(
            validate_line(
                model_id,
                models[model_id],
                providers=providers,
                native_contract=native_contract,
                taken_selectors=live_selectors,
                origin="catalog",
                taken_routes=route_wires,
                role_ids=role_ids,
            )
        )

    _check_retired(docs, models, providers, live_selectors, route_owner, errors)

    for document_key in (
        "gateway",
        "providers",
        "models",
        "retired",
        "roles",
        "native-contract",
    ):
        _secret_scan(docs[document_key], f"$.{document_key}", errors)

    _check_seed_profiles(docs, errors)
    return errors


_SEED_RETIRED_NOTICES = frozenset({"retired-successor", "retired-unbound"})


def _seed_bindings(document: dict[str, Any]) -> list[Any]:
    agents = document.get("agents")
    return [document.get("lead"), *(agents.values() if isinstance(agents, dict) else ())]


def _check_seed_profiles(docs: dict[str, Any], errors: list[str]) -> dict[str, int]:
    """Seed-profile validation; returns error counts per seed.

    ``profile.evaluate`` runs with ``effective=None`` (no New line admitted,
    every provider enabled). An evaluation that raises on an otherwise
    invalid catalog is reported, never propagated.
    """

    from . import profile as profile_mod  # local: profile imports catalog

    counts: dict[str, int] = {}
    lineup_catalog: Any = None
    lineup_error: Exception | None = None
    try:
        lineup_catalog = profile_mod.LineupCatalog.from_docs(docs)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        lineup_error = exc
    for name in SEED_PROFILE_NAMES:
        before = len(errors)
        where = f"profiles/{name}"
        document = docs.get(where)
        if not isinstance(document, dict):
            errors.append(f"{where}: missing seed profile")
            counts[name] = 1
            continue
        if document.get("name") != name:
            errors.append(f"{where}: name must equal the file stem")
        seed = document.get("seed")
        if not (
            isinstance(seed, dict)
            and set(seed) == {"id", "version"}
            and seed["id"] == name
            and isinstance(seed["version"], int)
            and not isinstance(seed["version"], bool)
        ):
            errors.append(
                f'{where}.seed: must be {{"id": "{name}", "version": <integer>}}'
            )
        if any(
            isinstance(binding, dict) and "use" in binding
            for binding in _seed_bindings(document)
        ):
            errors.append(f"{where}: seeds must not use named bindings")
        elif lineup_error is not None:
            errors.append(
                f"{where}: not evaluated against an invalid catalog ({lineup_error!r})"
            )
        else:
            try:
                result = profile_mod.evaluate(
                    document, lineup_catalog, bindings=None, effective=None
                )
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                errors.append(
                    f"{where}: not evaluated against an invalid catalog ({exc!r})"
                )
            else:
                errors.extend(f"{where}: {error}" for error in result.errors)
                if result.lineup is not None:
                    errors.extend(
                        f"{where}: {notice.message}; seeds name live lines only"
                        for notice in result.lineup.notices
                        if notice.code in _SEED_RETIRED_NOTICES
                    )
        _secret_scan(document, f"$.{where}", errors)
        counts[name] = len(errors) - before
    return counts


def _version_key(name: Any) -> tuple[int, ...] | None:
    if not isinstance(name, str):
        return None
    parts = name.split(".")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _pinned_version(contract: dict[str, Any]) -> str:
    from . import pin  # local import: pin reads paths only

    return pin.version(contract)


def _load_contract_override(
    path: Path, packaged: dict[str, Any]
) -> tuple[str, str | None]:
    """Classify an operator contract override file; it is never applied.

    A release runs only the Claude Code version its packaged contract pins
    (an override cannot name a build this release verified). Returns
    ``(source, detail)``: an old-format (version 1) override is
    ``override-ignored-stale`` or ``override-ignored-newer`` against the
    packaged pin, with its pinned version as the detail; anything else that
    can be read is ``override-ignored-invalid`` with the reason. A legacy
    override (the single effort vocabulary) raises CatalogError so the
    Runtime's removal path deletes it.
    """

    from . import state  # local import: state hardening for the config root

    try:
        raw = state.read_private(path)
        document = strict_json.loads(raw)
    except (state.StateError, strict_json.StrictJSONError) as exc:
        return "override-ignored-invalid", str(exc)
    if is_legacy_contract_override(document):
        raise CatalogError(
            f"contract override {path}: an older-format contract (effort_vocabulary) can never load"
        )
    claude = document.get("claude") if isinstance(document, dict) else None
    pinned = claude.get("validated_version") if isinstance(claude, dict) else None
    override_key = _version_key(pinned)
    if not isinstance(document, dict) or document.get("version") != 1 or override_key is None:
        return "override-ignored-invalid", "not an old-format contract override"
    packaged_key = _version_key(_pinned_version(packaged))
    if packaged_key is not None and override_key > packaged_key:
        return "override-ignored-newer", pinned
    return "override-ignored-stale", pinned


def load_catalog(root: Path | str, *, contract_override: Path | None = None) -> Catalog:
    """Load, schema-validate, and semantically validate the trusted bundle.

    ``contract_override`` names the operator contract file of earlier
    releases. It is classified (``contract_source``) and never applied: the
    effective native contract is always the packaged one.
    """

    raw = load_raw(root)
    problems = validate_catalog(raw)
    if problems:
        raise CatalogError("; ".join(problems))

    raw_docs = raw["docs"]
    # docs["models"] and docs["roles"] are the raw v2 documents.
    # docs["models-v2"] (Catalog.lines) and docs["roles-v2"]
    # (Catalog.roles_v2) are aliases of the same objects
    # (``docs["models"] is docs["models-v2"]``) for readers that use
    # models-v2-first conditionals.
    docs = {
        **raw_docs,
        "models-v2": raw_docs["models"],
        "roles-v2": raw_docs["roles"],
    }
    prompt_hashes = {
        role_id: "sha256:" + strict_json.sha256_hex(body)
        for role_id, body in sorted(raw["prompt_bodies"].items())
    }
    # The bundle hash covers the raw v2 models, the retired map and the raw
    # roles v2 document. ``prompts`` is keyed by role id (all ten ids; ids
    # of one function share one digest). ``profiles`` holds the seeds.
    bundle = {
        "version": raw_docs["version"],
        "settings": raw_docs["settings"],
        "gateway": raw_docs["gateway"],
        "providers": raw_docs["providers"],
        "models": raw_docs["models"],
        "retired": raw_docs["retired"],
        "roles": raw_docs["roles"],
        "native_contract": docs["native-contract"],
        "prompts": prompt_hashes,
        "profiles": {name: raw_docs[f"profiles/{name}"] for name in SEED_PROFILE_NAMES},
    }
    contract_source, contract_detail = "packaged", None
    if contract_override is not None and os.path.lexists(contract_override):
        contract_source, contract_detail = _load_contract_override(
            Path(contract_override), docs["native-contract"]
        )
    return Catalog(
        root=raw["root"],
        docs=docs,
        prompt_bodies=raw["prompt_bodies"],
        bundle=bundle,
        bundle_sha256=strict_json.bundle_digest(bundle),
        contract_source=contract_source,
        contract_override_detail=contract_detail,
    )


# --------------------------------------------------------- pinned registry
# The pinned gateway's embedded model registry (the upstream source's
# `internal/registry/models` at the pinned commit, the exact served registry
# because the gateway runs with --local-model) is a checked-in, read-only
# resource under `registry/` (the Nix package build compares it with the
# pinned source). It is evidence, never authority: it feeds
# the `claude-multi-dev check` presence rule (the registry overlay's trigger)
# and keeps codex `upgrade`/`retirement_at` reachable for the radar. It
# never sets a fence, a context value or a render input.

REGISTRY_DIR_ENV = "CLAUDE_MULTI_REGISTRY_DIR"
REGISTRY_FILES = ("models.json", "codex_client_models.json")
_REGISTRY_LIMITS = strict_json.JSONLimits(max_string=1024 * 1024)
_REGISTRY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


# Bounded, advisory per-model metadata. It never becomes
# a context validation result, a payload contract, an admission or a fence
# input; malformed fields read as None (the codex-upgrade discipline).
REGISTRY_THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
REGISTRY_DISPLAY_MAX = 96
REGISTRY_TOKENS_MAX = 2**31 - 1
REGISTRY_CREATED_MAX = 253402300799  # 9999-12-31T23:59:59Z, a representable Unix time
_REGISTRY_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")


@dataclass(frozen=True)
class RegistryModel:
    """One registry entry's advisory metadata; every field may be None."""

    display_name: str | None = None
    context_length: int | None = None
    max_completion_tokens: int | None = None
    thinking_levels: tuple[str, ...] | None = None
    created: int | None = None


@dataclass(frozen=True)
class PinnedRegistry:
    """The ids per registry section plus the codex client metadata.

    ``codex`` maps a codex slug to ``{"visibility", "upgrade",
    "retirement_at"}``: ``upgrade`` is the successor slug or None,
    ``retirement_at`` the upstream date (top-level or inside ``upgrade``)
    or None. ``meta`` maps section -> id -> :class:`RegistryModel`;
    ``sections`` is unchanged (the presence rule and the radar).
    """

    root: Path
    sections: dict[str, frozenset[str]]
    codex: dict[str, dict[str, str | None]]
    meta: dict[str, dict[str, RegistryModel]] = field(default_factory=dict)


def _registry_int(value: Any, *, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def registry_model(item: Mapping[str, Any]) -> RegistryModel:
    """Bounded metadata of one registry model entry (malformed -> None)."""

    display = item.get("display_name")
    if not (isinstance(display, str) and display.strip() and len(display) <= REGISTRY_DISPLAY_MAX
            and not _REGISTRY_CONTROL.search(display)):
        display = None
    levels: tuple[str, ...] | None = None
    thinking = item.get("thinking")
    raw_levels = thinking.get("levels") if isinstance(thinking, Mapping) else None
    if (isinstance(raw_levels, list) and raw_levels
            and all(isinstance(level, str) and level in REGISTRY_THINKING_LEVELS for level in raw_levels)
            and len(set(raw_levels)) == len(raw_levels)):
        levels = tuple(raw_levels)
    return RegistryModel(
        display_name=display,
        context_length=_registry_int(item.get("context_length"), low=1, high=REGISTRY_TOKENS_MAX),
        max_completion_tokens=_registry_int(item.get("max_completion_tokens"), low=1, high=REGISTRY_TOKENS_MAX),
        thinking_levels=levels,
        created=_registry_int(item.get("created"), low=0, high=REGISTRY_CREATED_MAX),
    )


def _merge_registry_model(first: RegistryModel, second: RegistryModel) -> RegistryModel:
    """Duplicate ids keep their presence; a field whose values disagree
    becomes unknown (never an arbitrary authoritative pick)."""

    def pick(a: Any, b: Any) -> Any:
        return a if a == b else None

    return RegistryModel(
        display_name=pick(first.display_name, second.display_name),
        context_length=pick(first.context_length, second.context_length),
        max_completion_tokens=pick(first.max_completion_tokens, second.max_completion_tokens),
        thinking_levels=pick(first.thinking_levels, second.thinking_levels),
        created=pick(first.created, second.created),
    )


def registry_dir(environ: dict[str, str] | None = None,
                 asset_root: Path | str | None = None) -> Path | None:
    """The pinned registry directory, or None when the resources carry none.

    ``CLAUDE_MULTI_REGISTRY_DIR`` overrides (tests point it at the fixture
    copy); otherwise ``registry/`` under the selected resources — the
    explicit ``asset_root``, else ``CLAUDE_MULTI_ASSETS``, else the packaged
    resources, which carry the pinned snapshot.
    """

    env = os.environ if environ is None else environ
    override = env.get(REGISTRY_DIR_ENV)
    candidate = Path(override) if override else assets.root(asset_root, environ=env) / "registry"
    return candidate if candidate.is_dir() else None


def load_pinned_registry(root: Path | str) -> PinnedRegistry:
    """Parse the two registry files strictly; malformed data raises."""

    base = Path(root)
    try:
        models = strict_json.load(base / "models.json", _REGISTRY_LIMITS)
        codex_doc = strict_json.load(base / "codex_client_models.json", _REGISTRY_LIMITS)
    except (OSError, ValueError, RecursionError) as exc:
        raise CatalogError(f"pinned registry {base} unreadable: {exc}") from exc
    if not isinstance(models, dict):
        raise CatalogError(f"pinned registry {base}/models.json is not an object")
    sections: dict[str, frozenset[str]] = {}
    meta: dict[str, dict[str, RegistryModel]] = {}
    for name, items in models.items():
        if not isinstance(items, list):
            raise CatalogError(f"pinned registry section {name!r} is not a list")
        sections[name] = frozenset(
            item["id"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and _REGISTRY_ID.fullmatch(item["id"])
        )
        section_meta: dict[str, RegistryModel] = {}
        for item in items:
            if not (isinstance(item, dict) and isinstance(item.get("id"), str)
                    and _REGISTRY_ID.fullmatch(item["id"])):
                continue
            model = registry_model(item)
            previous = section_meta.get(item["id"])
            section_meta[item["id"]] = model if previous is None else _merge_registry_model(previous, model)
        meta[name] = section_meta
    entries = codex_doc.get("models") if isinstance(codex_doc, dict) else None
    if not isinstance(entries, list):
        raise CatalogError(f"pinned registry {base}/codex_client_models.json has no models list")
    codex: dict[str, dict[str, str | None]] = {}
    for item in entries:
        slug = item.get("slug") if isinstance(item, dict) else None
        if not isinstance(slug, str) or not _REGISTRY_ID.fullmatch(slug):
            continue
        codex[slug] = codex_upgrade_fields(item)
    return PinnedRegistry(root=base, sections=sections, codex=codex, meta=meta)


_ISO_INSTANT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9:.]+(Z|[+-][0-9]{2}:[0-9]{2})?)?$")


def codex_upgrade_fields(item: dict[str, Any]) -> dict[str, str | None]:
    """``visibility`` / ``upgrade`` / ``retirement_at`` of one codex model
    entry (the pinned registry or a live plan listing); malformed values
    read as None so provider text never rides along unchecked."""

    visibility = item.get("visibility")
    upgrade = item.get("upgrade")
    successor = upgrade.get("model") if isinstance(upgrade, dict) else None
    retirement = item.get("retirement_at")
    if not isinstance(retirement, str) and isinstance(upgrade, dict):
        retirement = upgrade.get("retirement_at")
    return {
        "visibility": visibility
        if isinstance(visibility, str) and re.fullmatch(r"[a-z_-]{1,32}", visibility)
        else None,
        "upgrade": successor
        if isinstance(successor, str) and _REGISTRY_ID.fullmatch(successor)
        else None,
        "retirement_at": retirement
        if isinstance(retirement, str) and _ISO_INSTANT.fullmatch(retirement)
        else None,
    }


def registry_section(provider: dict[str, Any]) -> str | None:
    """The registry section an OAuth-pool provider's lines are checked
    against (its account pool's ``registry.section``: the codex pool's plan
    tier ``codex-pro``, say), else None.

    Only account pools serve through the embedded registry; direct and
    OpenAI-compatible providers are never registry-backed.
    """

    from claude_multi import account_pools

    transport = provider.get("transport", {})
    if transport.get("kind") != "oauth-pool":
        return None
    pool = account_pools.pool(str(transport.get("pool")))
    return pool.registry_section if pool is not None else None


def registry_absent_wires(
    lines: dict[str, Any], providers: dict[str, Any], registry: PinnedRegistry,
) -> list[tuple[str, str, str]]:
    """``(line key, wire, section)`` for every OAuth-pool line whose wire is
    absent from its pool's registry section (the registry overlay's trigger).

    Every line counts, ``status: new`` included (the gateway serves New
    lines). A section missing from the registry flags every line on it.
    """

    absent: list[tuple[str, str, str]] = []
    for key in sorted(lines):
        line = lines[key]
        provider = providers.get(line["provider"])
        section = registry_section(provider) if provider is not None else None
        if section is None:
            continue
        if line["wire_model"] not in registry.sections.get(section, frozenset()):
            absent.append((key, line["wire_model"], section))
    return absent
