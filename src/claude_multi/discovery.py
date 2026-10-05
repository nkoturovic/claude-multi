"""Discovery: candidates, listing plans, marks, drift and declaration planning.

Pure: no filesystem, network, secret or lock access. Callers pass the
observations (pinned registry, one served snapshot, a listing result) and
the effective catalog/operator view; this module only classifies them.

Observation never declares or admits:

- **Candidates** are computed on display from the pinned registry and
  the served snapshot, minus every wire, alias and route the effective view
  already knows, provider/channel scoped. Advisory only.
- **Listing plans** are immutable: every call names its URL, auth kind
  and secret *name* (never a value), the route verdict and a fingerprint
  the command re-derives before it reads a secret and again before it
  sends; a changed fingerprint is a stale plan.
- **Marks and drift** compare a listing with the effective view; absence is
  reported only for a complete, successful listing, and never as retirement.
- **Declaration planning** turns one listed model into a New·Off operator
  line document for the declaration transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import account_pools
from . import catalog as catalog_mod
from . import operator as operator_mod

# ------------------------------------------------------------ channels
# channel = registry section; a section maps to the provider of the one
# account pool whose sign-ins serve from it (``claude`` -> anthropic, every
# ``codex-<tier>`` -> openai; the packaged pool table, read once); only each
# pool's catalog section shows by default (``claude``, ``codex-pro``), every
# other one only with --all.
DEFAULT_SECTIONS = tuple(pool.registry_section for pool in account_pools.pools().values())
SENTINEL_PREFIX = "claude-multi-render-"
# Launcher-owned alias namespaces: a served id in them is an alias of ours
# (possibly stale — doctor's served cross-check reports those), never an
# uncataloged upstream model.
OWNED_ALIAS_PREFIXES = ("claude-multi-", "gpt-multi-", "custom-")


def section_provider(section: str) -> str | None:
    """The catalog provider a registry section serves, or None (no account
    pool serves from it, or pools of different providers share it)."""

    table = account_pools.load()
    providers = {table.pools[name].provider for name in table.section_pools(section)}
    return providers.pop() if len(providers) == 1 else None


def _date(created: int | None) -> str | None:
    if created is None:
        return None
    try:
        return datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


# ------------------------------------------------------------ known index
@dataclass(frozen=True)
class KnownIndex:
    """What the effective view already knows.

    ``wires`` is provider-scoped (a foreign provider's spelling never
    suppresses a candidate): line and retired wires, their raw passthrough
    selectors, passthrough routes, refusal fallbacks and the wires behind
    continuity and captured aliases. ``selectors`` is the gateway's global
    alias namespace (line and retired aliases, continuity and captured
    aliases, rendered selectors that are no provider's raw wire).
    """

    wires: Mapping[str, frozenset[str]]
    selectors: frozenset[str]
    # Current lines only (catalog, legacy, operator; New included): the
    # per-channel ``created`` threshold reads these, never retired or
    # captured wires.
    onboarded: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def knows(self, provider: str | None, wire: str) -> bool:
        """``provider`` None is an unattributed served id: any provider's
        wire knows it. A registry channel always passes its own scope."""

        if wire in self.selectors or wire.startswith(SENTINEL_PREFIX):
            return True
        if provider is None:
            return any(wire in wires for wires in self.wires.values())
        return wire in self.wires.get(provider, frozenset())


def _selector_bases(selectors: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for selector in selectors:
        found.add(selector)
        found.add(selector.removesuffix("[1m]"))
    return found


def known_index(
    docs: Mapping[str, Any], *,
    continuity: Mapping[str, Any] | None = None,
    captures: Mapping[str, Any] | None = None,
    expected: Iterable[str] = (),
) -> KnownIndex:
    """Build the index from the merged effective docs (catalog + legacy +
    operator lines, New included; retired entries), the continuity alias
    map, the ledger captures and the rendered selectors."""

    wires: dict[str, set[str]] = {}
    aliases: set[str] = set()

    def add(provider: Any, wire: Any) -> None:
        if isinstance(provider, str) and isinstance(wire, str):
            wires.setdefault(provider, set()).add(wire)

    def add_selectors(provider: Any, wire: Any, selectors: Iterable[Any]) -> None:
        # A selector spelling the line's own wire (raw passthrough, with or
        # without [1m]) stays scoped to its provider; the rest are aliases.
        for selector in selectors:
            if not isinstance(selector, str):
                continue
            if isinstance(wire, str) and selector.removesuffix("[1m]") == wire:
                add(provider, selector)
            else:
                aliases.update(_selector_bases([selector]))

    onboarded: dict[str, set[str]] = {}
    lines = (docs.get("models-v2") or docs.get("models") or {}).get("models", {})
    for entry in lines.values():
        add(entry.get("provider"), entry.get("wire_model"))
        if isinstance(entry.get("provider"), str) and isinstance(entry.get("wire_model"), str):
            onboarded.setdefault(entry["provider"], set()).add(entry["wire_model"])
        add_selectors(entry.get("provider"), entry.get("wire_model"),
                      (selector for _level, selector, _c in catalog_mod.line_selectors(dict(entry))))
    retired = (docs.get("retired") or {}).get("retired", {})
    for entry in retired.values():
        add(entry.get("provider"), entry.get("last_wire"))
        add_selectors(entry.get("provider"), entry.get("last_wire"), entry.get("selectors") or ())
    providers = (docs.get("providers") or {}).get("providers", {})
    for provider_id, provider in providers.items():
        for route in provider.get("passthrough_routes") or ():
            add(provider_id, route.get("name") if isinstance(route, Mapping) else None)
        if (provider.get("transport") or {}).get("pool") == "claude":
            for wire in catalog_mod.REFUSAL_FALLBACK_WIRES:
                add(provider_id, wire)
    for wire in catalog_mod.REFUSAL_FALLBACK_WIRES:
        add("anthropic", wire)
    for alias, entry in (continuity or {}).items():
        aliases.update(_selector_bases([alias]))
        if isinstance(entry, Mapping):
            add(entry.get("provider"), entry.get("wire"))
    for alias, entry in (captures or {}).items():
        aliases.update(_selector_bases([alias]))
        if isinstance(entry, Mapping):
            add(entry.get("provider"), entry.get("wire"))
    # Rendered selectors that spell some provider's raw wire stay scoped.
    raw = set().union(*wires.values()) if wires else set()
    aliases |= {selector for selector in _selector_bases(expected)
                if selector not in raw and selector.removesuffix("[1m]") not in raw}
    return KnownIndex({key: frozenset(value) for key, value in wires.items()}, frozenset(aliases),
                      {key: frozenset(value) for key, value in onboarded.items()})


# ------------------------------------------------------------ candidates
@dataclass(frozen=True)
class Candidate:
    """One advisory row. ``channel`` is None for an unattributed served id."""

    channel: str | None
    provider_hint: str | None
    wire: str
    sources: frozenset[str]  # {"registry", "served"}
    metadata: catalog_mod.RegistryModel | None
    visibility: str | None
    created: int | None
    owned_by: str | None = None

    @property
    def mark(self) -> str:
        return "served candidate" if "served" in self.sources else "candidate"

    @property
    def created_text(self) -> str:
        return f"created {_date(self.created)}" if _date(self.created) else "created unknown"


@dataclass(frozen=True)
class CandidateReport:
    rows: tuple[Candidate, ...]
    unattributed: tuple[Candidate, ...]
    hidden_older: Mapping[str, int]
    hidden_channels: tuple[str, ...]
    registry_available: bool
    served_available: bool


def _served_index(served: Sequence[Any] | None) -> dict[str, Any]:
    return {model.id: model for model in served or ()}


def _registry_sections(registry: catalog_mod.PinnedRegistry | None, wire: str) -> list[str]:
    if registry is None:
        return []
    return sorted(section for section, ids in registry.sections.items() if wire in ids)


def _served_attribution(registry: catalog_mod.PinnedRegistry | None, wire: str) -> tuple[str | None, list[str]]:
    """(provider, sections) a served id is attributed to, or (None, []).

    The gateway observation is id-only: it names a provider only when every
    registry section holding the id maps to that one provider. An id in no
    section, in an unmapped section or in sections of different providers is
    ambiguous and stays unattributed (one rule for candidates and doctor).
    """

    sections = _registry_sections(registry, wire)
    providers = {section_provider(section) for section in sections}
    if sections and len(providers) == 1 and None not in providers:
        return providers.pop(), sections
    return None, []


def _channel_scope(section: str) -> str:
    """The known-wire scope of a registry channel: its catalog provider, else
    the section name itself (a provider of that id, when one exists)."""

    return section_provider(section) or section


def candidates(
    registry: catalog_mod.PinnedRegistry | None,
    served: Sequence[Any] | None,
    known: KnownIndex,
    *,
    include_all: bool = False,
) -> CandidateReport:
    """Registry ∪ served ids the effective view does not know.

    Per channel, ids created before the newest ``created`` among the
    channel's onboarded wires are hidden unless ``include_all``; unknown
    dates stay visible. A served id is attributed only by the
    :func:`_served_attribution` rule; any other served id is
    ``unattributed`` (never attributed by name prefix), and the registry
    rows of an ambiguous id stay registry-only candidates.
    """

    served_by_id = _served_index(served)
    attributed = {wire: set(_served_attribution(registry, wire)[1]) for wire in served_by_id}
    rows: list[Candidate] = []
    hidden: dict[str, int] = {}
    hidden_channels: list[str] = []
    if registry is not None:
        for section in sorted(registry.sections):
            if not include_all and section not in DEFAULT_SECTIONS:
                hidden_channels.append(section)
                continue
            provider = section_provider(section)
            meta = registry.meta.get(section, {})
            current = known.onboarded.get(provider, frozenset()) if provider is not None else frozenset()
            onboarded = [meta[wire].created for wire in registry.sections[section]
                         if wire in meta and meta[wire].created is not None and wire in current]
            threshold = max(onboarded) if onboarded else None
            scope = _channel_scope(section)
            for wire in sorted(registry.sections[section]):
                if known.knows(scope, wire):
                    continue
                model = meta.get(wire)
                created = model.created if model is not None else None
                if not include_all and threshold is not None and created is not None and created < threshold:
                    hidden[section] = hidden.get(section, 0) + 1
                    continue
                codex = registry.codex.get(wire) if section.startswith("codex-") else None
                served_here = section in attributed.get(wire, ())
                rows.append(Candidate(
                    channel=section, provider_hint=provider, wire=wire,
                    sources=frozenset({"registry", *(("served",) if served_here else ())}),
                    metadata=model, visibility=(codex or {}).get("visibility"), created=created,
                    owned_by=getattr(served_by_id.get(wire), "owned_by", None) if served_here else None,
                ))
    unattributed: list[Candidate] = []
    for wire, model in sorted(served_by_id.items()):
        if attributed[wire] or wire.startswith(OWNED_ALIAS_PREFIXES):
            continue
        if known.knows(None, wire):
            continue
        unattributed.append(Candidate(
            channel=None, provider_hint=None, wire=wire, sources=frozenset({"served"}), metadata=None,
            visibility=None, created=getattr(model, "created", None), owned_by=getattr(model, "owned_by", None),
        ))
    return CandidateReport(tuple(rows), tuple(unattributed), hidden, tuple(hidden_channels),
                           registry is not None, served is not None)


def served_extras(
    registry: catalog_mod.PinnedRegistry | None, served: Sequence[Any], known: KnownIndex,
) -> tuple[dict[str, list[str]], list[str]]:
    """(channel -> routable uncataloged ids, unattributed ids) of one served
    snapshot. Unique ids: an id in several tiers of one provider counts once
    under its first section; an id whose sections map to different providers
    is ambiguous and stays unattributed."""

    by_channel: dict[str, list[str]] = {}
    unattributed: list[str] = []
    for model in sorted(served, key=lambda item: item.id):
        wire = model.id
        if wire.startswith(OWNED_ALIAS_PREFIXES) or wire.startswith(SENTINEL_PREFIX):
            continue
        provider, sections = _served_attribution(registry, wire)
        if provider is not None:
            if known.knows(provider, wire):
                continue
            preferred = [s for s in sections if s in DEFAULT_SECTIONS] or sections
            by_channel.setdefault(preferred[0], []).append(wire)
            continue
        if known.knows(None, wire):
            continue
        unattributed.append(wire)
    return by_channel, unattributed


def doctor_info_lines(by_channel: Mapping[str, Sequence[str]], unattributed: Sequence[str]) -> list[str]:
    """Doctor Info (never Attention: served is local registration)."""

    total = sum(len(ids) for ids in by_channel.values()) + len(unattributed)
    if not total:
        return []
    lines = [f"discovery: {total} routable, uncataloged model IDs — claude-multi models --candidates"]
    for channel in sorted(by_channel):
        lines.append(f"discovery {channel}: {len(by_channel[channel])} routable, uncataloged; "
                     "locally registered only, upstream access unverified")
    if unattributed:
        lines.append(f"discovery unattributed: {len(unattributed)} routable, uncataloged; "
                     "locally registered only, upstream access unverified")
    return lines


# ------------------------------------------------------------ listing plans
LISTING_DEADLINE_SECONDS = 20.0
LISTING_MAX_BYTES = 4 * 1024 * 1024
FEED_URL = "https://raw.githubusercontent.com/router-for-me/models/refs/heads/main/models.json"
LISTING_WHY = "list models to declare; nothing is admitted"
LISTING_CAPS = "20 s wall-clock; 4 MiB response; no redirects or retries"
CODEX_EXCLUDED = ("openai: not included; its plan listing requires separate consent:\n"
                  "  claude-multi discover openai")
STALE_TEXT = "discover {provider}: configuration changed while awaiting confirmation — nothing sent; retry"
ROUTE_TEXT = "discover {provider}: route unapproved or changed — claude-multi providers approve {provider}"
# Attempt descriptors of T2 providers without a ``listing`` block.
ATTEMPT_DESCRIPTORS = {
    "anthropic-compatible": ("/v1/models", "anthropic", "provider"),
    operator_mod.LAN_KIND: ("/models", "openai", "none"),
    operator_mod.KEYED_KIND: ("/models", "openai", "provider"),
}


class PlanRefusal(ValueError):
    """No request can be planned for this provider (nothing sent)."""

    def __init__(self, provider_id: str, reason: str, *, route: bool = False):
        super().__init__(reason)
        self.provider_id = provider_id
        self.reason = reason
        self.route = route


@dataclass(frozen=True)
class ListingCall:
    """One immutable planned listing request (never a secret value)."""

    provider_id: str
    tier: str  # "T1" (catalog or legacy) | "T2" (providers.d provider block)
    kind: str
    url: str
    shape: str  # anthropic | openai | codex
    auth: str  # none | bearer | header | pool-credential
    header: str | None
    secret_name: str | None
    verified: bool
    route: str  # catalog | approved | keyless
    fingerprint: str

    @property
    def keyed(self) -> bool:
        return self.auth not in ("none",)


@dataclass(frozen=True)
class Skipped:
    provider_id: str
    reason: str


@dataclass(frozen=True)
class DiscoveryPlan:
    calls: tuple[ListingCall, ...]
    skipped: tuple[Skipped, ...]
    codex_excluded: bool = False


def _fingerprint(document: Any) -> str:
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return "sha256:" + hashlib.sha256(raw.encode("ascii")).hexdigest()


def _secret_name(transport: Mapping[str, Any]) -> str | None:
    auth = transport.get("auth")
    ref = auth.get("secret_ref") if isinstance(auth, Mapping) else None
    return ref.removeprefix("env:") if isinstance(ref, str) and ref.startswith("env:") else None


def plan_provider(
    provider_id: str, *,
    docs: Mapping[str, Any],
    layer: operator_mod.OperatorLayer,
    descriptors: Mapping[str, Mapping[str, str]],
    transports: Mapping[str, str] | None = None,
) -> ListingCall:
    """The one listing request for ``provider_id`` or :class:`PlanRefusal`.

    T1 (catalog and legacy providers, and lines-only files on them) use the
    reviewed descriptor table; T2 providers use their approved ``listing``
    block, else the kind's attempt descriptor on the approved origin.
    """

    providers = docs["providers"]["providers"]
    resolved = layer.providers.get(provider_id)
    if resolved is not None:
        return _plan_t2(resolved, layer, docs)
    provider = providers.get(provider_id)
    if provider is None:
        raise PlanRefusal(provider_id, f"unknown provider {provider_id!r} (have: {', '.join(sorted(providers))})")
    if provider.get("origin") in operator_mod.OPERATOR_ORIGINS:
        # A T2 provider whose declaration does not resolve now.
        raise PlanRefusal(provider_id, f"discover {provider_id}: the declaration does not resolve — "
                                       "claude-multi providers validate")
    descriptor = descriptors.get(provider_id, {})
    transport = provider["transport"]
    if descriptor.get("status") == "unsupported":
        note = descriptor.get("note") or "no listing endpoint is known"
        raise PlanRefusal(provider_id, f"discover {provider_id}: model listing unsupported ({note}) — "
                                       f"declare manually: claude-multi models add {provider_id} WIRE --context N "
                                       "--source docs --source-ref URL")
    if transport["kind"] == "oauth-pool":
        choice = (transports or {}).get(provider_id)
        if choice is not None and choice != operator_mod.TRANSPORT_POOL:
            # The selected reviewed transport has no reviewed
            # listing descriptor: never guessed, never the pool's.
            raise PlanRefusal(provider_id, f"discover {provider_id}: its selected {choice} transport has no "
                                           f"reviewed model listing — declare manually: claude-multi models add "
                                           f"{provider_id} WIRE --context N --source docs --source-ref URL")
        if descriptor.get("auth") != "pool-credential":
            raise PlanRefusal(provider_id, f"discover {provider_id}: an OAuth pool has no model listing — "
                                           f"declare manually: claude-multi models add {provider_id} WIRE "
                                           "--context N --source docs --source-ref URL")
        call = {"provider_id": provider_id, "tier": "T1", "kind": "oauth-pool", "url": descriptor["url"],
                "shape": "codex", "auth": "pool-credential", "header": None, "secret_name": None,
                "verified": descriptor.get("status") == "verified", "route": "catalog"}
        return ListingCall(**call, fingerprint=_fingerprint({**call, "pool": transport.get("pool")}))
    base = str(transport.get("base_url", "")).rstrip("/")
    url = descriptor.get("url", "{base}/v1/models").replace("{base}", base)
    shape = descriptor.get("shape", "anthropic")
    mode = descriptor.get("auth", "provider")
    auth_block = transport.get("auth") if isinstance(transport.get("auth"), Mapping) else {}
    if mode == "none":
        auth, header, secret = "none", None, None
    else:
        secret = _secret_name(transport)
        if secret is None:
            raise PlanRefusal(provider_id, f"discover {provider_id}: no secret_ref to list models with "
                                           f"(transport auth {auth_block.get('kind') or 'missing'}); "
                                           "no keyless listing endpoint is known")
        if mode == "bearer" or auth_block.get("kind") == "bearer":
            auth, header = "bearer", None
        else:
            auth, header = "header", auth_block.get("header")
    call = {"provider_id": provider_id, "tier": "T1", "kind": transport["kind"], "url": url, "shape": shape,
            "auth": auth, "header": header, "secret_name": secret,
            "verified": descriptor.get("status", "attempt") == "verified", "route": "catalog"}
    return ListingCall(**call, fingerprint=_fingerprint({**call, "provider": provider}))


def _plan_t2(resolved: operator_mod.ResolvedProvider, layer: operator_mod.OperatorLayer,
             docs: Mapping[str, Any]) -> ListingCall:
    pid = resolved.provider_id
    problem = catalog_mod.keyed_compat_problem(resolved.entry, docs["gateway"])
    if problem:
        raise PlanRefusal(pid, problem)
    route = layer.route_status.get(pid, "unapproved")
    keyed = resolved.auth_kind != "none"
    if keyed and route != "approved":
        # A keyed T2 provider needs its current approved route even when the
        # selected listing is public (never an unapproved send).
        raise PlanRefusal(pid, ROUTE_TEXT.format(provider=pid), route=True)
    lan = resolved.kind == operator_mod.LAN_KIND
    if resolved.listing is not None:
        url, shape, mode = resolved.listing["url"], resolved.listing["shape"], resolved.listing["auth"]
        verified = True
    else:
        attempt = ATTEMPT_DESCRIPTORS.get(resolved.kind)
        if attempt is None:
            raise PlanRefusal(pid, f"discover {pid}: no listing endpoint is known for kind {resolved.kind} — "
                                   f"declare manually: claude-multi models add {pid} WIRE --context N --source docs")
        suffix, shape, mode = attempt
        url, verified = resolved.base_url.rstrip("/") + suffix, False
    if mode == "none":
        auth, header, secret = "none", None, None
    else:
        if not keyed:
            raise PlanRefusal(pid, f"discover {pid}: a credential-bearing listing on a keyless provider")
        secret = resolved.secret_name
        if mode == "bearer" or resolved.auth_kind == "bearer":
            auth, header = "bearer", None
        else:
            auth, header = "header", resolved.header
    try:
        origin, _normalized = operator_mod.normalize_endpoint(
            url, keyed=auth != "none", lan=lan, gateway_ports=operator_mod.gateway_ports(docs))
    except ValueError as exc:
        raise PlanRefusal(pid, f"discover {pid}: listing url refused ({exc})") from exc
    if auth != "none" and origin != resolved.origin:
        # Credential-bearing listings stay on the approved secret origin.
        raise PlanRefusal(pid, f"discover {pid}: a credential-bearing listing must use the approved origin "
                               f"{resolved.origin}")
    call = {"provider_id": pid, "tier": "T2", "kind": resolved.kind, "url": url, "shape": shape, "auth": auth,
            "header": header, "secret_name": secret, "verified": verified,
            "route": "approved" if keyed else "keyless"}
    return ListingCall(**call, fingerprint=_fingerprint({
        **call, "rd": resolved.route_digest, "route_status": route, "entry": resolved.entry,
        "base_url": resolved.base_url}))


def plan_all(
    *, docs: Mapping[str, Any], layer: operator_mod.OperatorLayer,
    descriptors: Mapping[str, Mapping[str, str]],
    enabled: Callable[[str], bool], secret_present: Callable[[str], bool],
    transports: Mapping[str, str] | None = None,
) -> DiscoveryPlan:
    """``discover --all``: deterministic, provider-id sorted, observation only.

    Disabled providers, OAuth pools (codex excluded by name), unsupported
    descriptors, unapproved routes and missing credentials are skipped with
    a reason; nothing is fanned out into declarations.
    """

    calls: list[ListingCall] = []
    skipped: list[Skipped] = []
    codex = False
    providers = docs["providers"]["providers"]
    for pid in sorted(set(providers) | set(layer.providers)):
        provider = providers.get(pid) or layer.providers[pid].entry
        if (provider.get("transport") or {}).get("kind") == "oauth-pool" and \
                descriptors.get(pid, {}).get("auth") == "pool-credential" and \
                (transports or {}).get(pid, operator_mod.TRANSPORT_POOL) == operator_mod.TRANSPORT_POOL:
            codex = True
            continue
        if not enabled(pid):
            skipped.append(Skipped(pid, "disabled in Settings"))
            continue
        try:
            call = plan_provider(pid, docs=docs, layer=layer, descriptors=descriptors, transports=transports)
        except PlanRefusal as exc:
            reason = exc.reason.split(": ", 1)[1] if exc.reason.startswith(f"discover {pid}: ") else exc.reason
            skipped.append(Skipped(pid, reason))
            continue
        if call.secret_name is not None and not secret_present(call.secret_name):
            skipped.append(Skipped(pid, f"credential {call.secret_name} is not set — "
                                        f"claude-multi providers set-key {pid}"))
            continue
        calls.append(call)
    return DiscoveryPlan(tuple(calls), tuple(skipped), codex)


def auth_text(call: ListingCall) -> str:
    if call.auth == "none":
        return "auth: none; no credential is sent"
    if call.auth == "pool-credential":
        return ("auth: one codex pool access credential from the gateway auth dir "
                "(value not shown; nothing is refreshed or written)")
    kind = f"header {call.header}" if call.auth == "header" else call.auth
    return f"auth: {kind} from {call.secret_name} (value not shown)"


def consent_text(calls: Sequence[ListingCall], skipped: Sequence[Skipped] = (), *, codex_excluded: bool = False) -> str:
    """The consent: every call, its auth by name, why and the caps."""

    lines: list[str] = []
    for item in skipped:
        lines.append(f"skipped {item.provider_id}: {item.reason}")
    if codex_excluded:
        lines.append(CODEX_EXCLUDED)
    count = len(calls)
    lines.append("claude-multi will make ONE request to a provider:" if count == 1 else
                 f"claude-multi will make {count} requests to providers:")
    for index, call in enumerate(calls, 1):
        lines.append(f"  {index}. GET {call.url}")
        lines.append(f"     {auth_text(call)}")
        if not call.verified:
            lines.append("     endpoint: unverified (an attempt; a failure here is not a refusal by the provider)")
        lines.append(f"     why: {LISTING_WHY}")
        lines.append(f"     caps: {LISTING_CAPS}")
    lines.append("Proceed with these listed requests? [y/N] ")
    return "\n".join(lines)


def feed_consent_text(url: str = FEED_URL) -> str:
    return ("claude-multi will make ONE credential-free third-party request:\n"
            f"  GET {url}\n"
            "  why: compare the public feed with the pinned registry\n"
            f"  caps: {LISTING_CAPS}\n"
            "Nothing is downloaded into the gateway or stored.\n"
            "Proceed? [y/N] ")


# ------------------------------------------------------------ marks and drift
@dataclass(frozen=True)
class MarkedEntry:
    entry: Mapping[str, Any]
    mark: str


@dataclass(frozen=True)
class OperatorState:
    """The effective state of one operator line for marks."""

    key: str
    provider_id: str
    wire: str
    status: str  # off | admitted | changed — re-admit | route unapproved
    declared_tokens: int
    efforts: tuple[str, ...]


def operator_states(
    layer: operator_mod.OperatorLayer, ledger: operator_mod.OperatorLedger | None, admitted: Iterable[str],
) -> dict[str, OperatorState]:
    """key -> OperatorState over the valid T2 lines."""

    granted = set(admitted)
    states: dict[str, OperatorState] = {}
    for key, line in sorted(layer.lines.items()):
        route = layer.route_status.get(line.provider_id, "catalog")
        grant = ledger.admissions.get(key) if ledger is not None else None
        if route not in operator_mod.ROUTE_USABLE:
            status = "route unapproved"
        elif key in granted and grant is not None and grant.get("digest") == line.definition_digest:
            status = "admitted"
        elif key in granted or grant is not None:
            status = "changed — re-admit"
        else:
            status = "off"
        entry = line.core_entry
        efforts = entry["efforts"]
        levels = tuple(efforts) if isinstance(efforts, list) else tuple(sorted(efforts))
        states[key] = OperatorState(key, line.provider_id, entry["wire_model"], status,
                                    int(entry["context"]["declared_tokens"]), levels)
    return states


def _created_value(entry: Mapping[str, Any]) -> float | None:
    created = entry.get("created")
    if isinstance(created, int) and not isinstance(created, bool):
        return float(created)
    if isinstance(created, str):
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def mark_listing(
    provider_id: str, entries: Sequence[Mapping[str, Any]], *,
    operator: Mapping[str, OperatorState],
    catalog_lines: Mapping[str, Mapping[str, Any]],
    legacy_models: Mapping[str, Mapping[str, Any]],
    retired: Mapping[str, Mapping[str, Any]],
) -> list[MarkedEntry]:
    """Marks against the effective view, provider-scoped.

    ``new``: an unknown id whose listing ``created`` is newer than every
    already-known id of this listing; ``candidate``: any other unknown id
    (unknown dates included).
    """

    by_operator = {state.wire: state for state in operator.values() if state.provider_id == provider_id}
    by_catalog: dict[str, str] = {}
    for key in sorted(catalog_lines):
        line = catalog_lines[key]
        if line.get("provider") == provider_id:
            by_catalog.setdefault(line.get("wire_model"), key)
    by_legacy: dict[str, str] = {}
    for key in sorted(legacy_models):
        spec = legacy_models[key]
        if spec.get("provider") == provider_id:
            by_legacy.setdefault(spec.get("wire_model"), key)
    by_retired: dict[str, str] = {}
    for key in sorted(retired):
        entry = retired[key]
        if entry.get("provider") == provider_id:
            by_retired.setdefault(entry.get("last_wire"), key)
    marks: list[MarkedEntry] = []
    known_created = [value for entry in entries
                     if entry["id"] in by_operator or entry["id"] in by_catalog or entry["id"] in by_legacy
                     or entry["id"] in by_retired
                     for value in [_created_value(entry)] if value is not None]
    newest = max(known_created) if known_created else None
    for entry in entries:
        wire = entry["id"]
        if wire in by_operator:
            state = by_operator[wire]
            mark = f"operator {state.key} ({state.status})"
        elif wire in by_catalog:
            mark = f"cataloged as {by_catalog[wire]}"
        elif wire in by_legacy:
            mark = f"legacy custom {by_legacy[wire]}"
        elif wire in by_retired:
            mark = f"retired {by_retired[wire]} (continuity retained)"
        else:
            created = _created_value(entry)
            mark = "new" if (newest is not None and created is not None and created > newest) else "candidate"
        marks.append(MarkedEntry(entry, mark))
    return marks


def drift_lines(
    provider_id: str, entries: Sequence[Mapping[str, Any]], *, complete: bool,
    operator: Mapping[str, OperatorState], today: str,
) -> list[str]:
    """Context/effort drift of declared operator lines, and absence Info
    from a complete listing only (never a retirement)."""

    listed = {entry["id"]: entry for entry in entries}
    lines: list[str] = []
    for key in sorted(operator):
        state = operator[key]
        if state.provider_id != provider_id:
            continue
        entry = listed.get(state.wire)
        if entry is None:
            if complete:
                lines.append(f"{key}: not advertised on {today} (Info only; not a retirement)")
            continue
        context = entry.get("context_length")
        if isinstance(context, int) and not isinstance(context, bool) and context != state.declared_tokens:
            lines.extend([
                f"{key}: listed context {context} != declared {state.declared_tokens}",
                f"  edit: claude-multi models edit {key}",
                f"  change: context.declared_tokens = {context}",
                "  consequence: new class; admission and evidence lapse; running sessions keep their fence "
                "until relaunch",
            ])
        listed_efforts = [level for level in entry.get("think_efforts") or ()
                          if isinstance(level, str) and level not in state.efforts]
        if listed_efforts:
            lines.extend([
                f"{key}: listing efforts now include {', '.join(sorted(set(listed_efforts)))}",
                f"  edit: claude-multi models edit {key}",
                "  consequence: only reviewed contracts are eligible; re-admission and qualification are "
                "required after a definition change",
            ])
    return lines


def entry_text(marked: MarkedEntry, *, codex: Mapping[str, Any] | None = None) -> str:
    """One listing report row: ``wire<TAB>mark`` plus the stated facts."""

    entry = marked.entry
    parts = [f"{entry['id']}\t{marked.mark}"]
    context = entry.get("context_length")
    if isinstance(context, int) and not isinstance(context, bool):
        parts.append(f" context={context}")
    output = entry.get("max_completion_tokens")
    if isinstance(output, int) and not isinstance(output, bool):
        parts.append(f" max_output={output}")
    if entry.get("think_efforts"):
        parts.append(f" efforts={','.join(str(e) for e in entry['think_efforts'])}")
    created = entry.get("created")
    if isinstance(created, int) and not isinstance(created, bool):
        stamp = _date(created)
        parts.append(f" created={stamp or created}")
    elif isinstance(created, str):
        parts.append(f" created={created}")
    for label, name in (("visibility", "visibility"), ("upgrade", "upgrade"), ("retires", "retirement_at")):
        value = entry.get(name)
        if value:
            parts.append(f" {label}={value}")
    return "".join(parts)


# ------------------------------------------------------------ declaration planning
KEY_MAX = 48
OVER_LISTED_MAX = 200


class DeclarationRefusal(ValueError):
    """A listed model cannot be declared from the listing as asked."""


def derived_key(wire: str, taken: Iterable[str]) -> str | None:
    """``custom-`` + the lower-cased wire (``.``, ``_``, ``/`` -> ``-``); a
    collision suffixes ``-2``, ``-3`` ...."""

    slug = re.sub(r"[^a-z0-9]+", "-", wire.lower()).strip("-")
    base = f"custom-{slug}"[:KEY_MAX].rstrip("-")
    if not operator_mod.NEW_KEY.fullmatch(base):
        return None
    used = set(taken)
    if base not in used:
        return base
    for number in range(2, 100):
        candidate = f"{base[:KEY_MAX - len(str(number)) - 1].rstrip('-')}-{number}"
        if candidate not in used and operator_mod.NEW_KEY.fullmatch(candidate):
            return candidate
    return None


def _contract_for(level: str, contracts: Iterable[str]) -> str | None:
    return next((contract for contract in sorted(contracts) if contract.endswith(f"-{level}")), None)


def declaration_efforts(
    provider: Mapping[str, Any], listed: Sequence[str] | None, registry_levels: Sequence[str] | None,
    agent_efforts: Iterable[str],
) -> tuple[Any, str]:
    """(efforts, default_effort) for a declared line.

    A provider with reviewed contracts: (listing efforts, else registry
    thinking levels, else {high}) ∩ contract levels ∩ native agent efforts,
    as a map. Without contracts (list-shaped lines): ``["high"]``. When
    nothing advertises a level and the provider has no reviewed high
    contract, the line is the plain ``["high"]`` one ``models add`` declares
    by default; advertised levels no contract matches are refused.
    """

    contracts = [c for c in provider.get("payload_contracts", []) if isinstance(c, str)]
    native = [level for level in agent_efforts if level != "ultracode"]
    mapped = {level: _contract_for(level, contracts) for level in native}
    mapped = {level: contract for level, contract in mapped.items() if contract is not None}
    if not mapped:
        return ["high"], "high"
    advertised = list(listed or registry_levels or ())
    wanted = advertised or ["high"]
    efforts = {level: mapped[level] for level in native if level in wanted and level in mapped}
    if not efforts and not advertised:
        return ["high"], "high"
    if not efforts:
        raise DeclarationRefusal(
            "no reviewed effort contract matches the advertised levels — declare the line manually: "
            "claude-multi models add PROVIDER WIRE --effort LEVEL=CONTRACT --context N --source listing")
    order = [level for level in native if level in efforts]
    default = "high" if "high" in efforts else order[-1]
    return efforts, default


def declaration_line(
    entry: Mapping[str, Any], *, provider: Mapping[str, Any], agent_efforts: Iterable[str],
    source_ref: str, context_override: int | None = None, over_listed: str | None = None,
    registry_model: catalog_mod.RegistryModel | None = None, registry_ref: str | None = None,
) -> dict[str, Any]:
    """One New·Off operator line document from a listed model.

    Context: the listing value, else the registry value, else the explicit
    ``--context``; raising above a listing-stated value needs
    ``over_listed``. Capabilities and roles stay the lead-only defaults;
    listing context is provenance, never validation.
    """

    listed = entry.get("context_length")
    listed = listed if isinstance(listed, int) and not isinstance(listed, bool) else None
    if context_override is not None:
        if listed is not None and context_override > listed and not over_listed:
            raise DeclarationRefusal(
                f"--context {context_override} exceeds the listed context {listed}; "
                "give --over-listed REASON to declare above the listing")
        context = {"declared_tokens": context_override, "source": "operator", "source_ref": source_ref}
    elif listed is not None:
        context = {"declared_tokens": listed, "source": "listing", "source_ref": source_ref}
    elif registry_model is not None and registry_model.context_length is not None:
        context = {"declared_tokens": registry_model.context_length, "source": "registry",
                   "source_ref": registry_ref or source_ref}
    else:
        raise DeclarationRefusal("the listing states no context — give --context N")
    efforts, default = declaration_efforts(
        provider, entry.get("think_efforts"), registry_model.thinking_levels if registry_model else None,
        agent_efforts)
    display = entry.get("display_name") or entry["id"]
    display = display[:96] if isinstance(display, str) and display.strip() else entry["id"]
    line: dict[str, Any] = {"wire_model": entry["id"], "display": display, "efforts": efforts,
                            "default_effort": default, "context": context}
    if over_listed:
        line["notes"] = f"over-listed: {over_listed}"[:512]
    return line


# ------------------------------------------------------------ feed peek (§4.4)
def feed_difference(feed: Any, registry: catalog_mod.PinnedRegistry | None) -> dict[str, list[str]]:
    """section -> ids the public feed lists that the pinned registry lacks.

    Advisory: "a future pin bump may add" — never a pin update.
    """

    if not isinstance(feed, Mapping):
        raise ValueError("the feed is not an object of sections")
    added: dict[str, list[str]] = {}
    for section in sorted(feed):
        items = feed[section]
        if not isinstance(section, str) or not isinstance(items, list):
            continue
        pinned = registry.sections.get(section, frozenset()) if registry is not None else frozenset()
        ids = sorted({item["id"] for item in items
                      if isinstance(item, Mapping) and isinstance(item.get("id"), str)
                      and catalog_mod._REGISTRY_ID.fullmatch(item["id"]) and item["id"] not in pinned})
        if ids:
            added[section] = ids
    return added
