"""Pure deterministic CLIProxyAPI config renderer for claude-multi v2.

Consumes only trusted ``gateway.json``/``providers.json``/``models.json``
facts and emits the complete integration YAML with a small schema-specific
emitter — no YAML library, no anchors/tags, stable ordering, terminal
newline. Secret references stay opaque: ``env:NAME`` values are resolved only
through the injected ``resolve_secret`` callable (dev/check passes dummy
values; the runtime render passes the real resolver). When a required
provider secret is unavailable, that provider's routes/aliases and payload
contracts are omitted atomically and reported, never half-rendered.

Adapter protocol contracts below are the pinned payload semantics of the
trusted adapters. Changing them is a reviewed adapter change, not data
drift; they are not hidden route tables.
"""

from __future__ import annotations

from claude_multi import endpoint

import copy
import hashlib
import json
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import errors, secret_store
from .catalog import (
    KEYED_COMPAT_LEVELS,
    is_keyed_compat,
    key_route,
    key_route_levels,
    keyed_compat_levels,
    keyed_compat_problem,
    line_selectors,
)


class RenderError(errors.ClaudeMultiError, ValueError):
    """Raised when trusted facts cannot be rendered deterministically."""


# Pinned adapter payload contracts (trusted in-process protocol semantics).
ADAPTER_PAYLOAD_CONTRACTS: dict[str, dict[str, dict[str, Any]]] = {
    # Keyless OpenAI-compatible upstreams (LAN trust boundary): no payload
    # params — the compat executor translates shape, never effort tiers.
    "cliproxy-openai-compat-v1": {},
    "cliproxy-oauth-codex-v1": {
        "reasoning-effort-low": {
            "kind": "override",
            "protocol": "codex",
            "params": {"reasoning.effort": "low"},
        },
        "reasoning-effort-medium": {
            "kind": "override",
            "protocol": "codex",
            "params": {"reasoning.effort": "medium"},
        },
        "reasoning-effort-high": {
            "kind": "override",
            "protocol": "codex",
            "params": {"reasoning.effort": "high"},
        },
        "reasoning-effort-xhigh": {
            "kind": "override",
            "protocol": "codex",
            "params": {"reasoning.effort": "xhigh"},
        },
        # The pinned 7.3.15 codex client allows "max"
        # (internal/client/codex/models/models.go codexClientAllowedReasoningLevels)
        # and the registry lists it for every GPT-6 line.
        "reasoning-effort-max": {
            "kind": "override",
            "protocol": "codex",
            "params": {"reasoning.effort": "max"},
        },
    },
    "cliproxy-claude-compatible-v1": {
        "output-config-low": {
            "kind": "override",
            "protocol": "claude",
            "params": {"output_config.effort": "low"},
        },
        "output-config-medium": {
            "kind": "override",
            "protocol": "claude",
            "params": {"output_config.effort": "medium"},
        },
        "output-config-high": {
            "kind": "override",
            "protocol": "claude",
            "params": {"output_config.effort": "high"},
        },
        "output-config-xhigh": {
            "kind": "override",
            "protocol": "claude",
            "params": {"output_config.effort": "xhigh"},
        },
        "output-config-max": {
            "kind": "override",
            "protocol": "claude",
            "params": {"output_config.effort": "max"},
        },
        "reasoning-effort-xhigh": {
            "kind": "override",
            "protocol": "claude",
            "params": {"reasoning_effort": "xhigh"},
        },
        "reasoning-effort-max": {
            "kind": "override",
            "protocol": "claude",
            "params": {"reasoning_effort": "max"},
        },
        "filter-thinking": {
            "kind": "filter",
            "protocol": "claude",
            "params": ["thinking"],
        },
    },
}

SENTINEL_PREFIX = "claude-multi-render-"
SENTINEL_BASE_URL = "http://127.0.0.1:9/v1"
SENTINEL_NAME = "claude-multi-render"


def is_reserved_name(name: str) -> bool:
    """Provider/item names reserved for the reload sentinel."""

    return name == SENTINEL_NAME or name.startswith(SENTINEL_PREFIX)


_SELECTOR_SUFFIX = "[1m]"


def _selector_base(selector: str) -> str:
    if selector.endswith(_SELECTOR_SUFFIX):
        return selector[: -len(_SELECTOR_SUFFIX)]
    return selector


def _emit_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, bool):
        raise RenderError(f"unsupported scalar {value!r}")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # JSON double-quoted form is valid, safe YAML for this subset.
        return json.dumps(value, ensure_ascii=False)
    raise RenderError(f"unsupported scalar type {type(value).__name__}")


def _emit_key(key: str) -> str:
    if not isinstance(key, str) or not key:
        raise RenderError(f"unsupported mapping key {key!r}")
    if all(char.isalnum() or char in "-_" for char in key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _emit_lines(node: Any, indent: int) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    if isinstance(node, dict):
        if not node:
            return [f"{pad}{{}}"]
        for key, value in node.items():
            key = _emit_key(key)
            if isinstance(value, dict):
                if not value:
                    lines.append(f"{pad}{key}: {{}}")
                else:
                    lines.append(f"{pad}{key}:")
                    lines.extend(_emit_lines(value, indent + 2))
            elif isinstance(value, list):
                if not value:
                    lines.append(f"{pad}{key}: []")
                else:
                    lines.append(f"{pad}{key}:")
                    lines.extend(_emit_lines(value, indent + 2))
            else:
                lines.append(f"{pad}{key}: {_emit_scalar(value)}")
        return lines
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                if not item:
                    lines.append(f"{pad}- {{}}")
                    continue
                keys = list(item)
                first = _emit_key(keys[0])
                first_value = item[keys[0]]
                if isinstance(first_value, (dict, list)):
                    lines.append(f"{pad}- {first}:")
                    lines.extend(_emit_lines(first_value, indent + 4))
                else:
                    lines.append(f"{pad}- {first}: {_emit_scalar(first_value)}")
                for raw_key in keys[1:]:
                    key = _emit_key(raw_key)
                    value = item[raw_key]
                    if isinstance(value, dict):
                        if not value:
                            lines.append(f"{pad}  {key}: {{}}")
                        else:
                            lines.append(f"{pad}  {key}:")
                            lines.extend(_emit_lines(value, indent + 4))
                    elif isinstance(value, list):
                        if not value:
                            lines.append(f"{pad}  {key}: []")
                        else:
                            lines.append(f"{pad}  {key}:")
                            lines.extend(_emit_lines(value, indent + 4))
                    else:
                        lines.append(f"{pad}  {key}: {_emit_scalar(value)}")
            elif isinstance(item, list):
                lines.append(f"{pad}-")
                lines.extend(_emit_lines(item, indent + 2))
            else:
                lines.append(f"{pad}- {_emit_scalar(item)}")
        return lines
    raise RenderError("document root must be a mapping")


def emit_yaml(document: dict[str, Any]) -> str:
    """Emit the restricted YAML form: deterministic, newline-terminated."""

    if not isinstance(document, dict):
        raise RenderError("document root must be a mapping")
    return "\n".join(_emit_lines(document, 0)) + "\n"


def sentinel_alias(document_without_sentinel: dict[str, Any]) -> str:
    """Identify the exact emitted document, including its ordered API keys."""

    digest = hashlib.sha256(emit_yaml(document_without_sentinel).encode("utf-8"))
    return SENTINEL_PREFIX + digest.hexdigest()[:8]


def finalize_document(document: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with exactly one final, keyless reload sentinel.

    Input must be sentinel-less: reserved provider names refuse, never silently
    drop routes. build_config_document already calls this; do not finalize its
    output again. Registration needs no credentials or upstream connections.
    """

    result = copy.deepcopy(document)
    sections = result.setdefault("openai-compatibility", [])
    for item in sections:
        if is_reserved_name(item.get("name", "")):
            raise RenderError("openai-compatibility provider name is reserved for the render sentinel")
    alias = sentinel_alias(result)
    sections.append({
        "name": SENTINEL_NAME,
        "base-url": SENTINEL_BASE_URL,
        "models": [{"name": SENTINEL_NAME, "alias": alias, "force-mapping": True}],
    })
    return result


def document_sentinel(document: dict[str, Any]) -> str:
    """The reload-sentinel alias of a finalized (sentinel-bearing) document."""

    return document["openai-compatibility"][-1]["models"][0]["alias"]


@dataclass(frozen=True)
class RenderResult:
    """Rendered YAML plus the structured availability report.

    ``continuity_rendered`` names the continuity aliases
    actually emitted, sorted; ``notices`` are non-fatal continuity notes
    (an unconfigured provider, an unavailable effort rule). Continuity data
    never raises: it is operator state, not reviewed catalog data.
    """

    yaml: str
    sentinel: str
    available_providers: tuple[str, ...]
    unavailable: tuple[dict[str, str], ...] = field(default_factory=tuple)
    continuity_rendered: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()
    # The rendered OAuth-pool aliases, sorted; start-mode
    # readiness requires one served. From the render output only (an OAuth
    # pool always renders), never from credential files.
    oauth_aliases: tuple[str, ...] = ()
    # Retained operator captures actually emitted (sorted), and the
    # overlay precedence conflicts (doctor Attention input).
    captures_rendered: tuple[str, ...] = ()
    overlay_conflicts: tuple["OverlayConflict", ...] = ()
    # Every public selector the document serves (``rendered_selectors``).
    served: frozenset[str] = frozenset()


# ------------------------------------------------------------ outbound proxy
# The lexical, credential-free gateway→provider proxy URL policy (owned by
# ``endpoint``): the reviewed gateway.json ``proxy-url``, replaced by the
# user's ``endpoint.json`` ``proxy_url`` (``claude-multi setup --step gateway
# --proxy URL``), reaches the config; there is no per-provider proxy.
# Diagnostics never echo a refused URL (it may carry a secret).
OUTBOUND_PROXY_SCHEMES = endpoint.OUTBOUND_PROXY_SCHEMES
outbound_proxy_problem = endpoint.outbound_proxy_problem


# ------------------------------------------------------------ OAuth overlay
# The top-level ``oauth-extra-models`` (gateway patch 16)
# key registers extra claude/codex wires on OAuth pools. One entry per
# (channel, lower-case wire); precedence current T1 > current T2 > retained
# (retired T1 over T2 capture); a wire the pinned static registry already
# serves gets no entry; absent optional fields are omitted, never null.
OVERLAY_KEY = "oauth-extra-models"
# The merged-docs marker of a catalog OAuth-pool provider moved to a
# reviewed keyed transport alternative (``operator.render_plan`` sets it on
# the render copy only; never a catalog field). ``{"id", "pool", "channel",
# "withheld"}``.
TRANSPORT_ALTERNATIVE_KEY = "transport_alternative"
EXCLUDED_MODELS_KEY = "oauth-excluded-models"
OVERLAY_CHANNELS = ("claude", "codex")
OVERLAY_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
OVERLAY_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max", "none", "auto")
GO_INT_MAX = 2**63 - 1
TIER_CURRENT_T1 = 0
TIER_CURRENT_T2 = 1
TIER_RETAINED = 2

# The keyed gateway channels a selected alternative renders into: the
# Claude Messages channel, and the codex (Responses) channel, whose section
# lists only the lines reviewed for the key route (``catalog.key_route``),
# each with its declared context window and thinking levels, never an empty
# model list (the gateway would serve its whole default registry).
CLAUDE_KEY_SECTION = "claude-api-key"
CODEX_KEY_SECTION = "codex-api-key"
# The gateway's codex executor adds its hosted image_generation tool to
# every Responses request unless this (global) setting says otherwise; the
# pinned gateway's "passthrough" mode never adds and never strips it, so
# the client's own tool list reaches the provider unchanged. It is rendered
# exactly when an account provider's key transport renders into the codex
# key channel (the OpenAI API key): then the codex account pool serves
# nothing, so the ChatGPT account route keeps its request shape whenever it
# is in use.
IMAGE_GENERATION_KEY = "disable-image-generation"
IMAGE_GENERATION_PASSTHROUGH = "passthrough"


def transport_channel(provider: Mapping[str, Any]) -> str | None:
    """The keyed channel of a provider moved onto a reviewed transport
    alternative (the render-copy marker), or None."""

    marker = provider.get(TRANSPORT_ALTERNATIVE_KEY)
    if not isinstance(marker, Mapping):
        return None
    return str(marker.get("channel") or CLAUDE_KEY_SECTION)


def is_codex_key_route(provider: Mapping[str, Any]) -> bool:
    """A provider rendered into the codex (Responses) key channel."""

    return provider["transport"]["kind"] == "direct" and transport_channel(provider) == CODEX_KEY_SECTION


@dataclass(frozen=True)
class OverlaySource:
    """One candidate registration (render stays operator-free: callers build these).

    ``tier``: 0 current catalog line, 1 current operator line, 2 retained
    historical registration. ``rank`` orders the retained tier (0 retired
    catalog entry, 1 operator capture). ``alias`` (retained only) is the
    continuity/capture alias that keeps it alive: a retained source counts
    only while that alias is emitted on the channel's pool.
    """

    tier: int
    channel: str
    name: str
    display: str
    max_context_length: int
    max_completion_tokens: int | None = None
    thinking_levels: tuple[str, ...] = ()
    origin: str = ""
    rank: int = 0
    alias: str | None = None

    def capabilities(self) -> tuple[Any, ...]:
        return (self.max_context_length, self.max_completion_tokens, tuple(self.thinking_levels))


@dataclass(frozen=True)
class OverlayConflict:
    """A lower-precedence registration whose capabilities differ from the winner."""

    channel: str
    wire: str
    winner: str
    loser: str

    def text(self) -> str:
        return (f"overlay {self.channel}/{self.wire}: {self.loser} differs from the winning "
                f"{self.winner}; the winning definition is served")


def _display_ok(text: Any) -> bool:
    return (
        isinstance(text, str) and text.strip() != "" and text == text.strip()
        and len(text) <= 256
        and not any(unicodedata.category(char) in ("Cc", "Cf") for char in text)
    )


def overlay_entry_problem(entry: Mapping[str, Any]) -> str | None:
    """Python pre-validation of one emitted entry against the pinned loader
    rules (gateway patch 16): closed fields, name grammar, positive
    64-bit integer limits (never bool/null), thinking levels vocabulary."""

    allowed = {"name", "display-name", "max-context-length", "max-completion-tokens", "thinking"}
    extra = sorted(set(entry) - allowed)
    if extra:
        return f"unknown overlay fields {extra}"
    name = entry.get("name")
    if not isinstance(name, str) or not re.fullmatch(OVERLAY_NAME_PATTERN, name):
        return f"overlay name {name!r} is invalid"
    if "display-name" in entry and not _display_ok(entry["display-name"]):
        return f"overlay {name}: display-name is invalid"
    for key in ("max-context-length", "max-completion-tokens"):
        if key in entry:
            value = entry[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= GO_INT_MAX:
                return f"overlay {name}: {key} must be a positive 64-bit integer"
    if "thinking" in entry:
        thinking = entry["thinking"]
        if not isinstance(thinking, Mapping) or set(thinking) != {"levels"}:
            return f"overlay {name}: thinking carries only derived levels"
        levels = thinking["levels"]
        if (not isinstance(levels, list) or not levels or len(set(levels)) != len(levels)
                or any(level not in OVERLAY_LEVELS for level in levels)):
            return f"overlay {name}: thinking levels are outside the loader vocabulary"
    return None


def plan_oauth_overlay(
    sources: Sequence[OverlaySource],
    *,
    rendered_aliases: Mapping[str, frozenset[str]],
    static_wires: Mapping[str, frozenset[str]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], tuple[OverlayConflict, ...]]:
    """Aggregate registrations into the emitted ``oauth-extra-models`` map.

    ``rendered_aliases`` maps a channel to the aliases its pool emits; a
    retained source outside it is ignored (nothing needs the registration).
    ``static_wires`` (lower-case ids per channel) suppresses wires the
    pinned static registry already serves; None = unknown (no suppression;
    the gateway's static-wins rule still drops a duplicate). Raises
    :class:`RenderError` on an equal-authority current conflict or an entry
    the loader would refuse (never published, never left to the gateway).
    """

    groups: dict[tuple[str, str], list[OverlaySource]] = {}
    for source in sources:
        if source.channel not in OVERLAY_CHANNELS:
            raise RenderError(f"overlay channel {source.channel!r} is not claude or codex")
        if source.tier == TIER_RETAINED and source.alias not in rendered_aliases.get(source.channel, frozenset()):
            continue
        groups.setdefault((source.channel, source.name.lower()), []).append(source)
    out: dict[str, list[dict[str, Any]]] = {}
    conflicts: list[OverlayConflict] = []
    for (channel, wire) in sorted(groups):
        if static_wires is not None and wire in static_wires.get(channel, frozenset()):
            continue
        members = groups[(channel, wire)]
        best_tier = min(member.tier for member in members)
        top = [member for member in members if member.tier == best_tier]
        if best_tier == TIER_RETAINED:
            best_rank = min(member.rank for member in top)
            top = [member for member in top if member.rank == best_rank]
        distinct = {member.capabilities() for member in top}
        names = sorted({member.name for member in top})
        if len(distinct) > 1 or len(names) > 1:
            if best_tier != TIER_RETAINED:
                raise RenderError(
                    f"overlay {channel}/{wire}: current definitions disagree "
                    f"({', '.join(sorted(member.origin for member in top))})"
                )
            # Equal-authority historical metadata: the explicit conservative
            # resolution (never file order): smallest limits, common levels.
            levels = set(top[0].thinking_levels)
            for member in top[1:]:
                levels &= set(member.thinking_levels)
            completions = [member.max_completion_tokens for member in top]
            winner = OverlaySource(
                tier=best_tier, channel=channel, name=names[0],
                display=min(member.display for member in top),
                max_context_length=min(member.max_context_length for member in top),
                max_completion_tokens=(min(completions) if all(c is not None for c in completions) else None),
                thinking_levels=tuple(level for level in OVERLAY_LEVELS if level in levels),
                origin=" + ".join(sorted(member.origin for member in top)),
                rank=top[0].rank,
            )
            for member in sorted(top, key=lambda m: m.origin):
                conflicts.append(OverlayConflict(channel, wire, "conservative merge", member.origin))
        else:
            winner = min(top, key=lambda m: (m.display, m.origin))
        for member in members:
            if member not in top and member.capabilities() != winner.capabilities():
                conflicts.append(OverlayConflict(channel, wire, winner.origin, member.origin))
        entry: dict[str, Any] = {"name": winner.name}
        if _display_ok(winner.display):
            entry["display-name"] = winner.display
        entry["max-context-length"] = winner.max_context_length
        if winner.max_completion_tokens is not None:
            entry["max-completion-tokens"] = winner.max_completion_tokens
        if winner.thinking_levels:
            entry["thinking"] = {"levels": list(winner.thinking_levels)}
        problem = overlay_entry_problem(entry)
        if problem is not None:
            raise RenderError(problem)
        out.setdefault(channel, []).append(entry)
    return {channel: out[channel] for channel in OVERLAY_CHANNELS if channel in out}, tuple(conflicts)


def rendered_oauth_aliases(document: dict[str, Any]) -> tuple[str, ...]:
    """Every OAuth-pool alias a rendered document serves once its pool is
    registered, sorted (the render sentinel is never an OAuth alias)."""

    return tuple(sorted({
        entry["alias"]
        for entries in document.get("oauth-model-alias", {}).values()
        for entry in entries
        if not entry["alias"].startswith(SENTINEL_PREFIX)
    }))


# ------------------------------------------------------------ keyed compat
def usable_secret(value: Any) -> bool:
    """One usable bearer value: a nonempty ``str`` fully matching the
    secret store's value vocabulary (no whitespace or control characters).
    Never trimmed or repaired; a fake or custom store's odd value is simply
    unusable. The value itself is never returned or printed."""

    return isinstance(value, str) and secret_store.VALUE_SHAPE.fullmatch(value) is not None


def keyed_secret_reason(secret_ref: str, value: Any) -> str | None:
    """None when ``value`` is usable, else the value-free unavailable reason."""

    if value is None:
        return f"missing required secret {secret_ref}"
    if not usable_secret(value):
        return f"unusable required secret {secret_ref} (blank or invalid; value not shown)"
    return None


def provider_secret_reason(provider: Mapping[str, Any], resolve_secret: Callable[[str], Any]) -> str | None:
    """The one auth.kind-aware availability rule of a provider.

    A direct provider is unavailable when its secret resolves to None
    (unchanged); a keyed compat provider when its secret is missing, blank,
    non-string or invalid; OAuth pools and keyless LAN routes always render.
    The secret resolves once per call.
    """

    transport = provider["transport"]
    if transport["kind"] == "direct":
        secret_ref = transport["auth"]["secret_ref"]
        value = resolve_secret(secret_ref.removeprefix("env:"))
        if is_codex_key_route(provider):
            # The Responses key channel takes one usable key, never a blank one.
            return keyed_secret_reason(secret_ref, value)
        if value is None:
            return f"missing required secret {secret_ref}"
        return None
    if is_keyed_compat(provider):
        secret_ref = transport["auth"]["secret_ref"]
        return keyed_secret_reason(secret_ref, resolve_secret(secret_ref.removeprefix("env:")))
    return None


def unavailable_providers(
    providers: dict[str, Any],
    *,
    resolve_secret: Callable[[str], str | None],
) -> tuple[dict[str, str], ...]:
    """Providers whose routes are omitted, as {"provider", "reason"} entries.

    The rule mirrors the omission pass in ``build_config_document`` exactly
    (:func:`provider_secret_reason`): a direct provider whose secret
    resolves to None and a keyed compat provider without a usable secret are
    unavailable; OAuth pools and keyless LAN providers always render. UIs
    that mark provider availability (the ordinary gateway picker) share
    it so their marking can never drift from what the renderer actually
    serves; a parity test pins the two together.
    """

    unavailable: list[dict[str, str]] = []
    for provider_id in sorted(providers):
        reason = provider_secret_reason(providers[provider_id], resolve_secret)
        if reason is not None:
            unavailable.append({"provider": provider_id, "reason": reason})
    return tuple(unavailable)


@dataclass(frozen=True)
class AliasSpec:
    """One public alias the render may emit (catalog line or continuity).

    ``origin`` is ``"catalog"`` (a v2 line selector) or ``"continuity"``
    (a persisted continuity alias for a retired selector).
    """

    provider: str
    wire: str
    alias: str
    contract: str | None
    display: str
    context_tokens: int
    origin: str
    # The canonical thinking levels of an alias on a keyed compat route
    # (None elsewhere; never the OAuth overlay's levels).
    levels: tuple[str, ...] | None = None
    # The model's own family (a catalog line's ``family``, or for a retained
    # alias the one its retired catalog entry declares): the owner an
    # aggregator's alias names (None: not declared).
    family: str | None = None


UNKNOWN_FAMILY = "unknown"


def alias_owner(provider: Mapping[str, Any], spec: AliasSpec) -> str:
    """The ``owned-by`` an alias reports: its provider's family, or for an
    aggregator (whose family is unknown: each line declares its own) the
    line's family when it declares one."""

    family = provider["independence_family"]
    if family == UNKNOWN_FAMILY and spec.family:
        return spec.family
    return family


def catalog_alias_plan(
    providers: Mapping[str, Any], models: Mapping[str, Any],
) -> list[AliasSpec]:
    """Catalog aliases of the v2 line map, sorted by model id, then effort.

    ``models`` is the v2 line map (catalog lines, ``status: new`` included,
    plus custom synthetic entries). Lines on providers absent from
    ``providers`` are never rendered.
    """

    plan: list[AliasSpec] = []
    for model_id in sorted(models):
        model = models[model_id]
        if model["provider"] not in providers:
            continue
        provider = providers[model["provider"]]
        levels: tuple[str, ...] | None = None
        context_tokens = model["context"]["provider_tokens"]
        reviewed: frozenset[str] | None = None
        if is_codex_key_route(provider):
            # Only what is reviewed for the key route: its efforts, its
            # documented window and thinking levels (never the account
            # route's evidence). A line without a reviewed route is not
            # served on it.
            route = key_route(model)
            levels = key_route_levels(model)
            if route is None or not levels:
                continue
            reviewed = frozenset(levels)
            context_tokens = route["context_tokens"]
        elif is_keyed_compat(provider):
            levels = keyed_compat_levels(model)
            if not levels:
                raise RenderError(f"keyed compat line {model_id!r} declares no renderable thinking level")
        for level, selector, contract in line_selectors(model):
            if reviewed is not None and level not in reviewed:
                continue
            plan.append(
                AliasSpec(
                    provider=model["provider"],
                    wire=model["wire_model"],
                    alias=_selector_base(selector),
                    contract=contract,
                    display=model["display"],
                    context_tokens=context_tokens,
                    origin="catalog",
                    levels=levels,
                    family=model.get("family") if isinstance(model.get("family"), str) else None,
                )
            )
    return plan


def catalog_alias_set(
    providers: Mapping[str, Any], models: Mapping[str, Any],
) -> frozenset[str]:
    """Every catalog alias plus every provider's passthrough route name.

    A continuity alias in this set is dropped silently: the catalog wins
    every collision (the gateway-effort retarget), whether or not
    the owning provider is available.
    """

    names = {
        _selector_base(selector)
        for model in models.values()
        for _level, selector, _contract in line_selectors(model)
    }
    names.update(
        route["name"]
        for provider in providers.values()
        for route in provider.get("passthrough_routes", [])
    )
    return frozenset(names)


_CONTINUITY_FIELDS = ("provider", "wire", "display", "context_tokens")


def keyed_capture_levels(entry: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The canonical levels of a retained alias on a keyed compat route, or
    None when missing or invalid (unservable, never reconstructed from the
    gateway's default levels)."""

    levels = entry.get("thinking_levels")
    if (not isinstance(levels, list) or not levels or len(set(levels)) != len(levels)
            or any(level not in KEYED_COMPAT_LEVELS for level in levels)):
        return None
    return tuple(level for level in KEYED_COMPAT_LEVELS if level in levels)


def retained_families(retired: Mapping[str, Any] | None) -> dict[tuple[str, str, str], str]:
    """``(alias, provider, wire)`` -> the family a retired catalog entry
    declares, for every selector it served: the trusted owner of an
    aggregator's retained alias. A persisted continuity entry gets it only
    while its provider and wire are still that entry's."""

    families: dict[tuple[str, str, str], str] = {}
    for entry in (retired or {}).values():
        family = entry.get("family") if isinstance(entry, Mapping) else None
        if not isinstance(family, str):
            continue
        for selector in entry.get("selectors") or {}:
            families[(_selector_base(selector), entry["provider"], entry["last_wire"])] = family
    return families


def _continuity_plan(
    providers: Mapping[str, Any],
    continuity: Mapping[str, Mapping[str, Any]],
    reserved: frozenset[str],
    *,
    origin: str = "continuity",
    quiet_providers: frozenset[str] = frozenset(),
    families: Mapping[tuple[str, str, str], str] | None = None,
) -> tuple[list[AliasSpec], list[str]]:
    """Continuity aliases that survive the merge rules, sorted by alias.

    ``origin`` ``"capture"`` plans retained operator captures with the
    same rules and their own notice label. ``quiet_providers``
    (externalized-provider retirements) are unconfigured by design, so
    their aliases are skipped without a notice: the absence of an optional
    sample is never a standing warning. A retained alias on a keyed compat
    route needs its own valid ``thinking_levels``: a
    continuity alias (which carries none) or a capture with missing or
    invalid levels is unservable and skipped with a notice.
    """

    label = "continuity alias" if origin == "continuity" else "operator alias"
    plan: list[AliasSpec] = []
    notices: list[str] = []
    unreviewed: dict[str, int] = {}
    for alias in sorted(continuity):
        entry = continuity[alias]
        if (
            not isinstance(alias, str)
            or not isinstance(entry, Mapping)
            or any(not isinstance(entry.get(name), str) for name in _CONTINUITY_FIELDS[:3])
            or not isinstance(entry.get("context_tokens"), int)
            or not (entry.get("proxy_contract") is None or isinstance(entry.get("proxy_contract"), str))
        ):
            notices.append(f"{label} {alias}: malformed entry, skipped")
            continue
        if alias in reserved:
            continue  # catalog wins every collision (silently)
        if alias.startswith(SENTINEL_PREFIX):
            notices.append(f"{label} {alias}: reserved render-sentinel prefix, skipped")
            continue
        provider_id = entry["provider"]
        if provider_id not in providers:
            if provider_id not in quiet_providers:
                notices.append(
                    f"{label} {alias}: provider {provider_id} is not configured"
                )
            continue
        levels: tuple[str, ...] | None = None
        if is_codex_key_route(providers[provider_id]):
            # A retained alias is never reviewed for a key route that lists
            # its models explicitly: it is not served while that route is
            # selected (one notice per provider).
            unreviewed[provider_id] = unreviewed.get(provider_id, 0) + 1
            continue
        if is_keyed_compat(providers[provider_id]):
            levels = keyed_capture_levels(entry)
            if levels is None:
                notices.append(
                    f"{label} {alias}: unservable on keyed route {provider_id} "
                    "(no valid recorded thinking levels; never reconstructed)"
                )
                continue
        plan.append(
            AliasSpec(
                provider=provider_id,
                wire=entry["wire"],
                alias=alias,
                contract=entry.get("proxy_contract"),
                display=entry["display"],
                context_tokens=entry["context_tokens"],
                origin=origin,
                levels=levels,
                family=(families or {}).get((alias, provider_id, entry["wire"])),
            )
        )
    for provider_id, count in sorted(unreviewed.items()):
        notices.append(f"{count} {label}(es) of {provider_id}: not served on its API-key route "
                       "(only reviewed lines are)")
    return plan, notices


def _provider_alias_plan(
    provider_id: str,
    provider: Mapping[str, Any],
    catalog_plan: Sequence[AliasSpec],
    continuity_plan: Sequence[AliasSpec],
) -> list[dict[str, Any]]:
    """The exact entries one provider's section (or OAuth pool) carries.

    Shared by ``build_config_document`` and ``provider_selectors`` so the
    served-set authority and the render agree structurally. OAuth pools:
    passthrough routes, then catalog aliases (fork semantics from the
    validated route rule, never recomputed), then continuity aliases.
    Direct/compat sections: catalog entries, then continuity entries, both
    shaped like catalog entries. Aliases are deduplicated stably.
    """

    kind = provider["transport"]["kind"]
    entries: list[dict[str, Any]] = []
    own_catalog = [spec for spec in catalog_plan if spec.provider == provider_id]
    own_continuity = [spec for spec in continuity_plan if spec.provider == provider_id]
    if kind == "oauth-pool":
        seen: set[tuple[str, str]] = set()
        fork_by_route: dict[str, bool] = {}

        def add(entry: dict[str, Any]) -> None:
            key = (entry["name"], entry["alias"])
            if key not in seen:
                entries.append(entry)
                seen.add(key)

        for route in provider["passthrough_routes"]:
            fork_by_route[route["name"]] = bool(route["fork"])
            add({
                "name": route["name"],
                "alias": route["name"],
                "force-mapping": True,
                "fork": bool(route["fork"]),
            })
        for spec in own_catalog:
            if spec.alias == spec.wire:
                if spec.wire in fork_by_route:
                    continue  # the passthrough route serves it
                # An operator line on an OAuth pool may use its
                # canonical wire as the selector without a passthrough route.
                add({"name": spec.wire, "alias": spec.alias, "force-mapping": True, "fork": True})
                continue
            add({
                "name": spec.wire,
                "alias": spec.alias,
                "force-mapping": True,
                "fork": fork_by_route.get(spec.wire, False),
            })
        for spec in own_continuity:
            add({
                "name": spec.wire,
                "alias": spec.alias,
                "force-mapping": True,
                "fork": fork_by_route.get(spec.wire, spec.alias == spec.wire),
            })
        return entries
    seen_aliases: set[str] = set()
    # A reviewed transport alternative moves an OAuth pool provider
    # (its passthrough routes included) onto a keyed direct section with the
    # same selectors; catalog direct providers carry no passthrough routes.
    for route in provider.get("passthrough_routes", []):
        if route["name"] in seen_aliases:
            continue
        seen_aliases.add(route["name"])
        entries.append({"name": route["name"], "alias": route["name"], "force-mapping": True})
    codex_key = is_codex_key_route(provider)
    for spec in [*own_catalog, *own_continuity]:
        if spec.alias in seen_aliases:
            continue
        seen_aliases.add(spec.alias)
        if codex_key:
            # The codex key channel: exactly the reviewed model item (the
            # documented window and thinking levels; force mapping keeps
            # the public alias as the client-visible model).
            if not spec.levels:
                raise RenderError(f"API-key route alias {spec.alias!r} has no reviewed thinking levels")
            entries.append({
                "name": spec.wire,
                "alias": spec.alias,
                "display-name": spec.display,
                "max-context-length": spec.context_tokens,
                "thinking": {"levels": list(spec.levels)},
                "force-mapping": True,
            })
        elif kind == "direct":
            entries.append({
                "name": spec.wire,
                "alias": spec.alias,
                "display-name": spec.display,
                "owned-by": alias_owner(provider, spec),
                "context-length": spec.context_tokens,
                "force-mapping": True,
            })
        elif is_keyed_compat(provider):
            # Exactly the reviewed keyed model item; force mapping
            # per model (client-visible identity, not executor selection).
            if not spec.levels:
                raise RenderError(f"keyed compat alias {spec.alias!r} has no thinking levels")
            entries.append({
                "name": spec.wire,
                "alias": spec.alias,
                "display-name": spec.display,
                "max-context-length": spec.context_tokens,
                "thinking": {"levels": list(spec.levels)},
                "force-mapping": True,
            })
        else:
            entries.append({
                "name": spec.wire,
                "alias": spec.alias,
                "display-name": spec.display,
                "force-mapping": True,
            })
    return entries


def provider_selectors(
    provider_id: str,
    provider: dict[str, Any],
    models: dict[str, Any],
    *,
    available: bool = True,
    continuity: Mapping[str, Mapping[str, Any]],
    providers: Mapping[str, Any] | None = None,
    captures: Mapping[str, Mapping[str, Any]] | None = None,
) -> frozenset[str]:
    """The selectors one provider renders (the shared rule).

    Built from the same per-provider plan as ``build_config_document``:
    OAuth pools always render (passthrough names + catalog aliases +
    continuity aliases); direct and keyed compat providers render only when
    ``available`` (a usable secret, :func:`provider_secret_reason`); keyless
    LAN providers always render.
    ``models`` is the v2 line map; ``continuity`` the continuity alias map
    (``{}`` for none). ``providers`` (all configured providers) makes the
    catalog-wins collision set exact across providers' route names.
    ``captures`` are the render plan's retained operator captures,
    ranked after continuity exactly as ``build_config_document`` ranks them
    (a keyed route keeps only captures with valid recorded levels), so a
    captured-only provider's expected set matches the render.
    """

    everyone = providers if providers is not None else {provider_id: provider}
    reserved = catalog_alias_set(everyone, models)
    own = {provider_id: provider}
    catalog_plan = catalog_alias_plan(own, models)
    continuity_plan, _notices = _continuity_plan(own, continuity, reserved)
    if captures:
        everyone_continuity, _notices = _continuity_plan(everyone, continuity, reserved)
        capture_plan, _notices = _continuity_plan(
            own, captures, reserved | {spec.alias for spec in everyone_continuity}, origin="capture",
        )
        continuity_plan = [*continuity_plan, *capture_plan]
    if provider["transport"]["kind"] == "direct-openai" and not is_keyed_compat(provider):
        available = True
    if provider["transport"]["kind"] != "oauth-pool" and not available:
        return frozenset()
    return frozenset(
        entry["alias"]
        for entry in _provider_alias_plan(provider_id, provider, catalog_plan, continuity_plan)
        if not entry["alias"].startswith(SENTINEL_PREFIX)
    )


def rendered_selectors(document: dict[str, Any]) -> frozenset[str]:
    """Every public selector a rendered config document serves.

    OAuth sections serve each entry's alias (passthrough names alias
    themselves); direct sections serve the lane alias only — the gateway
    registers the alias as the public id, NOT the upstream wire name
    (verified live against a disposable loopback proxy: k3/qwen3.8-max/
    glm-5.2 do not appear in /v1/models). OpenAI-compatibility sections
    serve the same lane aliases. The doctor served-models
    cross-check compares this set against the running gateway's /v1/models.
    """

    selectors: set[str] = set()
    for entries in document.get("oauth-model-alias", {}).values():
        for entry in entries:
            selectors.add(entry["alias"])
    for key in (CLAUDE_KEY_SECTION, CODEX_KEY_SECTION):
        for section in document.get(key, []):
            for entry in section.get("models", []):
                selectors.add(entry["alias"])
    for section in document.get("openai-compatibility", []):
        for entry in section.get("models", []):
            selectors.add(entry["alias"])
    return frozenset(item for item in selectors if not item.startswith(SENTINEL_PREFIX))


def build_config_document(
    gateway: dict[str, Any],
    providers: dict[str, Any],
    models: dict[str, Any],
    *,
    home: Path,
    gateway_token: str | None = None,
    gateway_tokens: Sequence[str] | None = None,
    resolve_secret: Callable[[str], str | None],
    continuity: Mapping[str, Mapping[str, Any]],
    captures: Mapping[str, Mapping[str, Any]] | None = None,
    provider_headers: Mapping[str, Mapping[str, str]] | None = None,
    oauth_overlay: Sequence[OverlaySource] | None = None,
    overlay_static: Mapping[str, frozenset[str]] | None = None,
    quiet_providers: frozenset[str] = frozenset(),
    retired: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[dict[str, str], ...], dict[str, Any]]:
    """Build the finalized (sentinel-bearing) document and availability report.

    ``models`` is the **v2** line map (catalog lines incl. ``status: new``,
    plus custom synthetic entries). ``continuity`` is the continuity
    ``aliases`` map (``{}`` for none) — required, so a caller
    that forgot it fails loudly. Catalog aliases come first and win every
    collision; continuity entries on unconfigured providers are dropped with
    a notice, on unavailable providers omitted with them. ``retired`` (the
    catalog's retired map) gives a retained alias on an aggregator the owner
    its retired entry declares (:func:`retained_families`). Returns
    ``(document, available, unavailable, info)`` where ``info`` carries
    ``continuity_rendered`` (sorted) and ``notices``.

    ``gateway_tokens`` preserves its input order, normally (previous, current)
    during rotation or (current,) otherwise. ``gateway_token`` is the legacy
    single-key calling convention; supply exactly one of them.
    """

    for provider_id in providers:
        if is_reserved_name(provider_id):
            raise RenderError(
                f"provider id {provider_id!r} is reserved for the render sentinel"
            )
    gateway_info = gateway["gateway"]
    parts = urllib.parse.urlsplit(endpoint.gateway_endpoint(gateway).base_url)
    static = gateway_info["cliproxy_static"]

    # The keyed compat shape gate is a renderer precondition,
    # asserted for every provider before any secret is resolved.
    for provider_id in sorted(providers):
        keyed_problem = keyed_compat_problem(providers[provider_id], gateway)
        if keyed_problem is not None:
            raise RenderError(f"provider {provider_id!r}: {keyed_problem}")

    # Secret availability: a direct provider without its secret is omitted;
    # a keyed compat provider without a usable secret is omitted whole.
    available: dict[str, dict[str, Any]] = {}
    unavailable: list[dict[str, str]] = []
    resolved_secrets: dict[str, str] = {}
    for provider_id in sorted(providers):
        provider = providers[provider_id]
        transport = provider["transport"]
        alternative = provider.get(TRANSPORT_ALTERNATIVE_KEY)
        if isinstance(alternative, Mapping) and alternative.get("withheld"):
            # A selected but unapproved transport is never keyed and
            # never falls back to the pool.
            unavailable.append(
                {"provider": provider_id, "reason": f"transport {alternative['id']} is not approved"}
            )
            continue
        if transport["kind"] == "direct" and is_codex_key_route(provider):
            # One usable key for the Responses key channel (missing, blank or
            # invalid omits the provider; the account pool stays excluded).
            secret_ref = transport["auth"]["secret_ref"]
            value = resolve_secret(secret_ref.removeprefix("env:"))
            reason = keyed_secret_reason(secret_ref, value)
            if reason is not None:
                unavailable.append({"provider": provider_id, "reason": reason})
                continue
            resolved_secrets[provider_id] = value
        elif transport["kind"] == "direct":
            secret_ref = transport["auth"]["secret_ref"]
            env_name = secret_ref.removeprefix("env:")
            value = resolve_secret(env_name)
            if value is None:
                unavailable.append(
                    {
                        "provider": provider_id,
                        "reason": f"missing required secret {secret_ref}",
                    }
                )
                continue
            resolved_secrets[provider_id] = value
        elif is_keyed_compat(provider):
            # Resolved once per render plan; missing, blank, non-string or
            # invalid omits every current/captured alias and payload
            # contribution (never a keyless section, never env fallback).
            secret_ref = transport["auth"]["secret_ref"]
            value = resolve_secret(secret_ref.removeprefix("env:"))
            reason = keyed_secret_reason(secret_ref, value)
            if reason is not None:
                unavailable.append({"provider": provider_id, "reason": reason})
                continue
            resolved_secrets[provider_id] = value
        available[provider_id] = provider

    catalog_plan = catalog_alias_plan(providers, models)
    reserved = catalog_alias_set(providers, models)
    continuity_plan, notices = _continuity_plan(
        providers, continuity, reserved, quiet_providers=quiet_providers,
        families=retained_families(retired),
    )
    if captures:
        # Retained operator captures rank after continuity (the
        # catalog, current operator lines and continuity win every collision).
        capture_plan, capture_notices = _continuity_plan(
            providers, captures, reserved | {spec.alias for spec in continuity_plan},
            origin="capture", quiet_providers=quiet_providers,
        )
        continuity_plan = [*continuity_plan, *capture_plan]
        notices.extend(capture_notices)

    document: dict[str, Any] = {}
    document["host"] = parts.hostname
    document["port"] = parts.port
    document["tls"] = static["tls"]
    document["remote-management"] = static["remote-management"]
    document["auth-dir"] = str(home / gateway_info["auth_dir"])
    if gateway_tokens is not None and gateway_token is not None:
        raise RenderError("pass gateway_tokens or gateway_token, not both")
    if isinstance(gateway_tokens, (str, bytes)):
        raise RenderError("gateway_tokens must be an ordered sequence of keys, not a string")
    tokens = list(gateway_tokens) if gateway_tokens is not None else [gateway_token]
    if not tokens or any(not isinstance(token, str) or not token.strip() for token in tokens):
        raise RenderError("gateway api-keys must contain at least one non-blank key")
    document["api-keys"] = tokens
    document["debug"] = static["debug"]
    document["pprof"] = static["pprof"]
    document["plugins"] = static["plugins"]
    # mDNS / DNS-SD LAN advertisement pinned off:
    # the gateway defaults it off, but the rendered config states it.
    document["discovery"] = static["discovery"]
    document["commercial-mode"] = static["commercial-mode"]
    document["logging-to-file"] = static["logging-to-file"]
    document["usage-statistics-enabled"] = static["usage-statistics-enabled"]
    problem = outbound_proxy_problem(static["proxy-url"])
    if problem is not None:
        raise RenderError(f"gateway.json proxy-url refused: {problem}")
    document["proxy-url"] = static["proxy-url"]
    document["passthrough-headers"] = static["passthrough-headers"]
    document["request-retry"] = static["request-retry"]
    document["max-retry-credentials"] = static["max-retry-credentials"]
    document["max-retry-interval"] = static["max-retry-interval"]
    document["disable-cooling"] = static["disable-cooling"]
    document["save-cooldown-status"] = static["save-cooldown-status"]
    document["quota-exceeded"] = static["quota-exceeded"]
    document["routing"] = static["routing"]
    document["ws-auth"] = static["ws-auth"]

    rendered_by_provider: dict[str, list[str]] = {}

    def plan_for(provider_id: str) -> list[dict[str, Any]]:
        entries = _provider_alias_plan(
            provider_id, available[provider_id], catalog_plan, continuity_plan
        )
        rendered_by_provider[provider_id] = [entry["alias"] for entry in entries]
        return entries

    # OAuth alias sections keyed by pool, in provider order.
    alias_sections: dict[str, list[dict[str, Any]]] = {}
    for provider_id in sorted(available):
        provider = available[provider_id]
        transport = provider["transport"]
        if transport["kind"] != "oauth-pool":
            continue
        pool = transport["pool"]
        entries = plan_for(provider_id)
        if entries:
            alias_sections.setdefault(pool, []).extend(entries)
    document["oauth-model-alias"] = alias_sections
    # A pool whose provider moved to a reviewed keyed transport
    # registers nothing through its OAuth credentials, so the same selectors
    # are never served by both routes (no silent credential fallback).
    excluded_pools = sorted({
        str(provider[TRANSPORT_ALTERNATIVE_KEY]["pool"])
        for provider in providers.values()
        if isinstance(provider.get(TRANSPORT_ALTERNATIVE_KEY), Mapping)
    })
    if excluded_pools:
        document[EXCLUDED_MODELS_KEY] = {pool: ["*"] for pool in excluded_pools}
    if any(transport_channel(provider) == CODEX_KEY_SECTION for provider in providers.values()):
        # The selected codex key transport (approved or withheld, keyed or
        # not): no hosted tool the client never asked for (see the constant).
        document[IMAGE_GENERATION_KEY] = IMAGE_GENERATION_PASSTHROUGH
    overlay_conflicts: tuple[OverlayConflict, ...] = ()
    if oauth_overlay:
        pool_aliases: dict[str, frozenset[str]] = {
            pool: frozenset(entry["alias"] for entry in entries)
            for pool, entries in alias_sections.items()
        }
        overlay, overlay_conflicts = plan_oauth_overlay(
            oauth_overlay, rendered_aliases=pool_aliases, static_wires=overlay_static,
        )
        if overlay:
            # One literal top-level key, plain YAML.
            document[OVERLAY_KEY] = overlay

    direct_sections: list[dict[str, Any]] = []
    codex_key_sections: list[dict[str, Any]] = []
    for provider_id in sorted(available):
        provider = available[provider_id]
        transport = provider["transport"]
        if transport["kind"] != "direct":
            continue
        section_models = plan_for(provider_id)
        if is_codex_key_route(provider):
            # The Responses channel with the reviewed key: the fixed official
            # base (the executor appends /responses), bearer auth, the
            # account cloaking off, no extra retry rounds and no websocket
            # transport; never an empty model list.
            if section_models:
                codex_key_sections.append({
                    "api-key": resolved_secrets[provider_id],
                    "base-url": transport["base_url"],
                    "disable-codex-cloaking": True,
                    "request-retry": 0,
                    "models": section_models,
                })
            continue
        section: dict[str, Any] = {
            "api-key": resolved_secrets[provider_id],
            "base-url": transport["base_url"],
        }
        # Header-style auth (e.g. Kimi's x-api-key) emits the override in its
        # documented position; bearer auth leaves the adapter's default
        # Authorization behavior.
        if transport["auth"]["kind"] == "header":
            section["auth-header"] = transport["auth"]["header"]
        # Validated static headers of an operator provider arrive as
        # an explicit input (never a catalog-provider field).
        headers = (provider_headers or {}).get(provider_id)
        if headers:
            section["headers"] = {name: headers[name] for name in sorted(headers)}
        section["cloak"] = {"mode": "never"}
        section["models"] = section_models
        # A zero-model provider stays configured (credential
        # status is derived from providers + secrets, not from the render)
        # but emits no section unless it carries at least one alias
        # (catalog or continuity): the pinned gateway serves its whole
        # embedded Claude registry for an empty claude-api-key models list.
        if section_models:
            direct_sections.append(section)
    document[CLAUDE_KEY_SECTION] = direct_sections
    if codex_key_sections:
        document[CODEX_KEY_SECTION] = codex_key_sections

    # Keyless OpenAI-compatible upstreams (direct-openai): only the pinned
    # 7.2.80 fields — name, base-url, models with name/alias/display-name/
    # force-mapping. No api-key-entries/headers/prefix/thinking: the pinned
    # executor omits Authorization without a key (verified in source) and
    # the LAN server is a text-only keyless trust boundary.
    compat_sections: list[dict[str, Any]] = []
    for provider_id in sorted(available):
        provider = available[provider_id]
        transport = provider["transport"]
        if transport["kind"] != "direct-openai":
            continue
        section_models = plan_for(provider_id)
        if not section_models:
            continue  # never emit `models: []`
        if is_keyed_compat(provider):
            # One keyed section: the validated id, normalized base,
            # exactly one api-key entry, the closed X-Client header (only
            # when present) and the nonempty model list.
            keyed: dict[str, Any] = {
                "name": provider_id,
                "base-url": transport["base_url"],
                "api-key-entries": [{"api-key": resolved_secrets[provider_id]}],
            }
            headers = (provider_headers or {}).get(provider_id)
            if headers:
                keyed["headers"] = {name: headers[name] for name in sorted(headers)}
            keyed["models"] = section_models
            compat_sections.append(keyed)
            continue
        compat_sections.append(
            {
                "name": provider_id,
                "base-url": transport["base_url"],
                "models": section_models,
            }
        )
    document["openai-compatibility"] = compat_sections

    retained_rendered = sorted(
        spec.alias
        for spec in continuity_plan
        if spec.alias in rendered_by_provider.get(spec.provider, ())
    )
    capture_aliases = {spec.alias for spec in continuity_plan if spec.origin == "capture"}
    continuity_rendered = [alias for alias in retained_rendered if alias not in capture_aliases]
    captures_rendered = [alias for alias in retained_rendered if alias in capture_aliases]
    overrides: list[dict[str, Any]] = []
    filters: list[dict[str, Any]] = []
    for provider_id in sorted(available):
        provider = available[provider_id]
        contracts = ADAPTER_PAYLOAD_CONTRACTS.get(provider["adapter"], {})
        declared = list(provider["payload_contracts"])
        own_catalog = [spec for spec in catalog_plan if spec.provider == provider_id]
        own_continuity = [
            spec for spec in continuity_plan
            if spec.provider == provider_id and spec.alias in retained_rendered
        ]
        for spec in own_continuity:
            # Operator state never raises: an effort rule the provider no
            # longer declares (or the adapter does not know) keeps the alias,
            # served without an override, and says so.
            if spec.contract is not None and (
                spec.contract not in declared or spec.contract not in contracts
            ):
                label = "continuity alias" if spec.origin == "continuity" else "operator alias"
                notices.append(
                    f"{label} {spec.alias}: effort rule {spec.contract} unavailable"
                )
        for contract_id in declared:
            contract = contracts.get(contract_id)
            if contract is None:
                raise RenderError(
                    f"provider {provider_id!r} declares unknown payload contract "
                    f"{contract_id!r} for adapter {provider['adapter']!r}"
                )
            if contract["kind"] == "filter":
                # Route-bound filters apply to every alias of the provider,
                # catalog and continuity alike.
                aliases = sorted(
                    {spec.alias for spec in own_catalog}
                    | {spec.alias for spec in own_continuity}
                )
            else:
                aliases = sorted(
                    {spec.alias for spec in own_catalog if spec.contract == contract_id}
                    | {spec.alias for spec in own_continuity if spec.contract == contract_id}
                )
            if not aliases:
                continue
            entry = {
                "models": [
                    {"name": alias, "protocol": contract["protocol"]}
                    for alias in aliases
                ],
                "params": contract["params"],
            }
            if contract["kind"] == "override":
                overrides.append(entry)
            else:
                filters.append(entry)
    document["payload"] = {"override": overrides, "filter": filters}

    info = {
        "continuity_rendered": tuple(continuity_rendered),
        "notices": tuple(notices),
        "captures_rendered": tuple(captures_rendered),
        "overlay_conflicts": overlay_conflicts,
    }
    return finalize_document(document), tuple(sorted(available)), tuple(unavailable), info


def render_config(
    gateway: dict[str, Any],
    providers: dict[str, Any],
    models: dict[str, Any],
    *,
    home: Path,
    gateway_token: str | None = None,
    gateway_tokens: Sequence[str] | None = None,
    resolve_secret: Callable[[str], str | None],
    continuity: Mapping[str, Mapping[str, Any]],
    captures: Mapping[str, Mapping[str, Any]] | None = None,
    provider_headers: Mapping[str, Mapping[str, str]] | None = None,
    oauth_overlay: Sequence[OverlaySource] | None = None,
    overlay_static: Mapping[str, frozenset[str]] | None = None,
    quiet_providers: frozenset[str] = frozenset(),
    retired: Mapping[str, Any] | None = None,
) -> RenderResult:
    """Render the complete deterministic CLIProxyAPI YAML configuration.

    ``models`` is the v2 line map; ``continuity`` the continuity alias map
    (required; ``{}`` for none).
    """

    document, available, unavailable, info = build_config_document(
        gateway,
        providers,
        models,
        home=home,
        gateway_token=gateway_token,
        gateway_tokens=gateway_tokens,
        resolve_secret=resolve_secret,
        continuity=continuity,
        captures=captures,
        provider_headers=provider_headers,
        oauth_overlay=oauth_overlay,
        overlay_static=overlay_static,
        quiet_providers=quiet_providers,
        retired=retired,
    )
    return RenderResult(
        yaml=emit_yaml(document),
        sentinel=document_sentinel(document),
        available_providers=available,
        unavailable=unavailable,
        continuity_rendered=info["continuity_rendered"],
        notices=info["notices"],
        oauth_aliases=rendered_oauth_aliases(document),
        captures_rendered=info["captures_rendered"],
        overlay_conflicts=info["overlay_conflicts"],
        served=rendered_selectors(document),
    )
