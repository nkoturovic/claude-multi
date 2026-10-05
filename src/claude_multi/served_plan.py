"""The shared served-change plan.

Pure: no lock, no write, no network, no secret value. The "before" side is
the **published** render — ``config.yaml`` as the gateway last loaded it,
parsed by :func:`parse_restricted_yaml`, the inverse of ``render.emit_yaml``
(never a re-render: a change in the render code itself must stay visible).
The "after" side is a candidate document from the same render pipeline.

A route identity keeps exactly what decides where a selector goes and how it
is shaped: the section kind (OAuth alias pool, ``claude-api-key``,
``codex-api-key``, ``openai-compatibility``), the route (pool name, or the normalized
``base-url`` including its path), the upstream wire name, the auth-header
name, the static header names, and the effort/payload/overlay contract. An
``openai-compatibility`` route also keeps its auth mode (keyed
``bearer`` versus keyless), its static header NAMES and the model capability
metadata that changes behaviour (``thinking.levels``, ``max-context-length``,
``force-mapping``) in its contract, so a levels-only change is a retarget.
Key values (``api-key``, ``api-key-entries``, header values, gateway
``api-keys``) are dropped. A missing or unparseable side is *unknown*, never
*unchanged*.

Live impact comes from the record/fence scan of the explicit state root.
A destructive plan (a removal or a retarget) needs complete coverage: an
unreadable record, sessions directory or compiled fence makes it unknown
and an unknown plan cannot remove or retarget. The digest binds the
inputs and the diff; a commit phase recomputes the inputs and refuses with
:data:`CHANGED_REFUSAL` when anything moved after confirmation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

SECTION_OAUTH = "oauth-pool"
SECTION_KEY = "claude-api-key"
# The Responses key channel of a reviewed API-key transport: its identity is
# the base URL, bearer auth, its static header names, the section's
# behaviour switches and each model's capability metadata (never the key).
SECTION_CODEX_KEY = "codex-api-key"
SECTION_COMPAT = "openai-compatibility"
SENTINEL_NAME = "claude-multi-render"
SENTINEL_PREFIX = "claude-multi-render-"

HEADER = "Served change preview — no changes written"
NO_SERVED_CHANGE = "Served selectors: unchanged."
CONFIRM_TEXT = "Apply these changes? [y/N] "
CHANGED_REFUSAL = (
    "Configuration or session references changed after confirmation — nothing applied; "
    "review a new plan."
)
UNKNOWN_IMPACT = (
    "Live session impact: unknown — {reason}.\n"
    "This operation cannot safely remove or retarget selectors."
)
ROOT_REFUSAL = (
    "This gateway is managed for state root {managed}; the requested root is {requested}.\n"
    "Cross-root selector safety is not established. No served change was applied."
)
# An existing but unreadable/unsafe/malformed continuity file
# is unknown authority, never "no authority yet" — the boundary refuses.
ROOT_UNREADABLE_REFUSAL = (
    "gateway continuity set unreadable ({reason}): the gateway's state-root authority is unknown; "
    "the requested root is {requested}.\n"
    "Cross-root selector safety is not established. No served change was applied. Repair: move "
    "~/.config/claude-multi/continuity.json aside and run claude-multi-proxy init"
)
ROOT_BLOCK = (
    "gateway managed for root {managed}; this command used {requested} — run with the managed "
    "root, or adopt this one after the root inventory: claude-multi-proxy init --state-root "
    "{requested} --adopt-root"
)
PENDING_FACT = (
    "Pending served change: {added} added · {retargeted} retargeted · {removed} removed "
    "(live impact: {impact}) — a gateway start or reload publishes it without asking; review "
    "with claude-multi plan, publish with claude-multi providers apply"
)


class PlanParseError(ValueError):
    """The published config is not in the restricted emitted YAML form."""


# ------------------------------------------------------------ restricted YAML
def _scalar(text: str) -> Any:
    if text == "true":
        return True
    if text == "false":
        return False
    if text == "null":
        return None
    if text == "{}":
        return {}
    if text == "[]":
        return []
    if text.startswith('"'):
        try:
            value, end = json.JSONDecoder().raw_decode(text)
        except ValueError as exc:
            raise PlanParseError("bad string scalar") from exc
        if end != len(text) or not isinstance(value, str):
            raise PlanParseError("bad string scalar")
        return value
    if text.lstrip("-").isdigit():
        return int(text)
    raise PlanParseError("unsupported scalar")


def _split_key(text: str) -> tuple[str, str] | None:
    """``key: rest`` / ``key:`` -> (key, rest) or None when ``text`` is a scalar."""

    if text.startswith('"'):
        try:
            key, end = json.JSONDecoder().raw_decode(text)
        except ValueError:
            return None
        if not isinstance(key, str) or not text[end:].startswith(":"):
            return None
        rest = text[end + 1:]
    else:
        index = text.find(":")
        if index <= 0:
            return None
        key = text[:index]
        if not all(char.isalnum() or char in "-_" for char in key):
            return None
        rest = text[index + 1:]
    if rest and not rest.startswith(" "):
        return None
    return key, rest.strip()


def parse_restricted_yaml(text: str) -> dict[str, Any]:
    """Parse the deterministic subset ``render.emit_yaml`` writes.

    Raises :class:`PlanParseError` for anything else (a hand edit outside the
    form, tabs, flow collections other than ``{}``/``[]``).
    """

    lines: list[tuple[int, str]] = []
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise PlanParseError("tab indentation")
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, raw[indent:].rstrip()))
    if not lines:
        raise PlanParseError("empty document")

    def node(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines) or lines[index][0] != indent:
            raise PlanParseError("unexpected indentation")
        if lines[index][1] == "{}":
            return {}, index + 1
        if lines[index][1] == "-" or lines[index][1].startswith("- "):
            return sequence(index, indent)
        return mapping(index, indent)

    def mapping(index: int, indent: int, first: str | None = None) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        while index < len(lines):
            level, content = lines[index]
            if first is not None:
                content, first = first, None
            elif level != indent:
                break
            split = _split_key(content)
            if split is None:
                raise PlanParseError("expected a mapping key")
            key, rest = split
            if key in result:
                raise PlanParseError("duplicate key")
            index += 1
            if rest:
                result[key] = _scalar(rest)
            else:
                if index >= len(lines) or lines[index][0] <= indent:
                    raise PlanParseError("missing nested block")
                result[key], index = node(index, lines[index][0])
        return result, index

    def sequence(index: int, indent: int) -> tuple[list[Any], int]:
        result: list[Any] = []
        while index < len(lines) and lines[index][0] == indent:
            content = lines[index][1]
            if content == "-":
                if index + 1 >= len(lines) or lines[index + 1][0] <= indent:
                    raise PlanParseError("missing nested sequence")
                value, index = node(index + 1, lines[index + 1][0])
                result.append(value)
                continue
            if not content.startswith("- "):
                break
            rest = content[2:]
            if rest == "{}":
                result.append({})
                index += 1
                continue
            if _split_key(rest) is None:
                result.append(_scalar(rest))
                index += 1
                continue
            value, index = mapping(index, indent + 2, first=rest)
            result.append(value)
        return result, index

    document, end = node(0, 0)
    if end != len(lines) or not isinstance(document, dict):
        raise PlanParseError("trailing content")
    return document


# ------------------------------------------------------------ route identity
def normalize_base_url(url: Any) -> str:
    """Scheme and host lower-cased, trailing slash dropped, path kept."""

    text = str(url or "").strip()
    scheme, sep, rest = text.partition("://")
    if not sep:
        return text.rstrip("/")
    host, slash, path = rest.partition("/")
    path = (slash + path).rstrip("/")
    return f"{scheme.lower()}://{host.lower()}{path}"


@dataclass(frozen=True)
class RouteIdentity:
    """Where one public selector goes; never a key value."""

    section: str
    route: str
    wire: str
    auth_header: str | None = None
    header_names: tuple[str, ...] = ()
    contract: str = ""

    def as_document(self) -> dict[str, Any]:
        return {"section": self.section, "route": self.route, "wire": self.wire,
                "auth_header": self.auth_header, "header_names": list(self.header_names),
                "contract": self.contract}

    def label(self) -> str:
        if self.section == SECTION_OAUTH:
            route = f"{self.route} OAuth pool"
        elif self.section == SECTION_KEY:
            route = f"key route {self.route}" + (f" ({self.auth_header})" if self.auth_header else "")
        elif self.section == SECTION_CODEX_KEY:
            route = f"key route {self.route} (responses, {self.auth_header or 'bearer'})"
        else:
            route = f"{self.route}" + (f" ({self.auth_header})" if self.auth_header else "")
        text = f"{route} · {self.wire}"
        if self.contract:
            text += f" · {self.contract}"
        return text

    def changed_parts(self, other: "RouteIdentity") -> tuple[str, ...]:
        parts = []
        if (self.section, self.route, self.auth_header, self.header_names) != (
                other.section, other.route, other.auth_header, other.header_names):
            parts.append("route")
        if self.wire != other.wire:
            parts.append("wire")
        if self.contract != other.contract:
            parts.append("contract")
        return tuple(parts)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _contracts(document: Mapping[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    payload = document.get("payload")
    if not isinstance(payload, Mapping):
        return found
    for kind in ("override", "filter"):
        entries = payload.get(kind)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            params = entry.get("params")
            for model in entry.get("models") or ():
                if isinstance(model, Mapping) and isinstance(model.get("name"), str):
                    found.setdefault(model["name"], []).append(
                        f"{kind}:{model.get('protocol')}:{_canonical(params)}")
    return found


def _overlay(document: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    overlay = document.get("oauth-extra-models")
    found: dict[tuple[str, str], str] = {}
    if not isinstance(overlay, Mapping):
        return found
    for channel, entries in overlay.items():
        for entry in entries if isinstance(entries, list) else ():
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
                found[(str(channel), entry["name"])] = "overlay:" + _canonical(dict(entry))
    return found


_COMPAT_CAPABILITY_KEYS = ("thinking", "max-context-length", "force-mapping")
_CODEX_KEY_SWITCHES = ("disable-codex-cloaking", "request-retry", "websockets")


def _compat_capabilities(model: Mapping[str, Any]) -> list[str]:
    """The canonical capability contract of a keyed compat model
    item (levels, context, force mapping); empty for the keyless LAN shape,
    whose identity is unchanged."""

    if "thinking" not in model and "max-context-length" not in model:
        return []
    return ["model:" + _canonical({key: model.get(key) for key in _COMPAT_CAPABILITY_KEYS})]


def routes_from_document(document: Mapping[str, Any]) -> dict[str, RouteIdentity]:
    """Every public selector of a (parsed or in-memory) gateway document.

    The reload sentinel is never a selector. Key values are never read.
    """

    contracts = _contracts(document)
    overlay = _overlay(document)
    excluded = document.get("oauth-excluded-models")
    excluded_pools = set(excluded) if isinstance(excluded, Mapping) else set()
    routes: dict[str, RouteIdentity] = {}

    def contract_of(alias: str, extra: Iterable[str] = ()) -> str:
        items = sorted([*contracts.get(alias, ()), *extra])
        if not items:
            return ""
        return "contract " + hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()[:12]

    pools = document.get("oauth-model-alias")
    if isinstance(pools, Mapping):
        for pool, entries in pools.items():
            for entry in entries if isinstance(entries, list) else ():
                if not isinstance(entry, Mapping) or not isinstance(entry.get("alias"), str):
                    continue
                wire = str(entry.get("name"))
                extra = [overlay[(str(pool), wire)]] if (str(pool), wire) in overlay else []
                if pool in excluded_pools:
                    extra.append("excluded")
                routes[entry["alias"]] = RouteIdentity(
                    SECTION_OAUTH, str(pool), wire, contract=contract_of(entry["alias"], extra))
    for section in document.get("claude-api-key") or ():
        if not isinstance(section, Mapping):
            continue
        base = normalize_base_url(section.get("base-url"))
        header = section.get("auth-header")
        headers = section.get("headers")
        names = tuple(sorted(headers)) if isinstance(headers, Mapping) else ()
        for model in section.get("models") or ():
            if isinstance(model, Mapping) and isinstance(model.get("alias"), str):
                routes[model["alias"]] = RouteIdentity(
                    SECTION_KEY, base, str(model.get("name")),
                    auth_header=str(header) if isinstance(header, str) else None,
                    header_names=names, contract=contract_of(model["alias"]))
    for section in document.get(SECTION_CODEX_KEY) or ():
        if not isinstance(section, Mapping):
            continue
        base = normalize_base_url(section.get("base-url"))
        headers = section.get("headers")
        names = tuple(sorted(headers)) if isinstance(headers, Mapping) else ()
        switches = {key: section.get(key) for key in _CODEX_KEY_SWITCHES if key in section}
        extra = ["section:" + _canonical(switches)] if switches else []
        for model in section.get("models") or ():
            if isinstance(model, Mapping) and isinstance(model.get("alias"), str):
                routes[model["alias"]] = RouteIdentity(
                    SECTION_CODEX_KEY, base, str(model.get("name")), auth_header="bearer", header_names=names,
                    contract=contract_of(model["alias"], [*_compat_capabilities(model), *extra]))
    for section in document.get("openai-compatibility") or ():
        if not isinstance(section, Mapping):
            continue
        name = str(section.get("name"))
        if name == SENTINEL_NAME or name.startswith(SENTINEL_PREFIX):
            continue
        route = f"{name} {normalize_base_url(section.get('base-url'))}"
        # Auth mode and header NAMES only (key and header values
        # never enter an identity, a plan text or a digest).
        keyed = "api-key-entries" in section or "api-key" in section
        headers = section.get("headers")
        names = tuple(sorted(headers)) if isinstance(headers, Mapping) else ()
        for model in section.get("models") or ():
            if isinstance(model, Mapping) and isinstance(model.get("alias"), str):
                if model["alias"].startswith(SENTINEL_PREFIX):
                    continue
                routes[model["alias"]] = RouteIdentity(
                    SECTION_COMPAT, route, str(model.get("name")),
                    auth_header="bearer" if keyed else None, header_names=names,
                    contract=contract_of(model["alias"], _compat_capabilities(model)))
    return dict(sorted(routes.items()))


def published_routes(raw: bytes | str) -> dict[str, RouteIdentity]:
    """The published identity of ``config.yaml`` bytes (PlanParseError if not
    the emitted form). The bytes carry key values; only identities return."""

    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return routes_from_document(parse_restricted_yaml(text))


def gateway_label(document: Mapping[str, Any] | None) -> str:
    if not isinstance(document, Mapping):
        return "unknown"
    return f"{document.get('host', '?')}:{document.get('port', '?')}"


# ------------------------------------------------------------ references
@dataclass(frozen=True)
class References:
    """Selector references of one state root (from ``continuity.scan_records``)."""

    refs: Mapping[str, frozenset[str]]
    live: frozenset[str]
    slots: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    unknown: str | None = None

    def fingerprint(self) -> str:
        return _digest({
            "refs": {base: sorted(ids) for base, ids in sorted(self.refs.items())},
            "live": sorted(self.live),
            "unknown": self.unknown,
        })


def references_from_scan(scan: Any, *, process_live: Iterable[str] | None = (),
                         process_known: bool = True) -> References:
    """Coverage from a ``continuity.RecordScan``: an unreadable sessions
    directory, record or compiled fence is unknown; ended records that a
    process still names count live; an unreadable process table is unknown."""

    reasons = []
    if getattr(scan, "directory_error", None):
        reasons.append(f"sessions directory unreadable ({scan.directory_error})")
    unreadable = tuple(getattr(scan, "unreadable", ()) or ())
    if unreadable:
        reasons.append("unreadable records " + ", ".join(stem[:8] for stem in unreadable))
    fences = tuple(getattr(scan, "fence_unreadable", ()) or ())
    if fences:
        reasons.append("unreadable scope fences " + ", ".join(stem[:8] for stem in fences))
    if not process_known:
        reasons.append("the process table is unreadable")
    live = set(scan.live) | set(process_live or ())
    return References(
        refs=dict(scan.refs), live=frozenset(live),
        slots=dict(getattr(scan, "slots", {}) or {}),
        unknown="; ".join(reasons) or None,
    )


# ------------------------------------------------------------ the plan
def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _base(selector: str) -> str:
    return selector[:-4] if selector.endswith("[1m]") else selector


@dataclass(frozen=True)
class ServedChangePlan:
    gateway: str
    state_root: str
    added: tuple[tuple[str, RouteIdentity], ...]
    removed: tuple[tuple[str, RouteIdentity], ...]
    retargeted: tuple[tuple[str, RouteIdentity, RouteIdentity], ...]
    authority: tuple[tuple[str, str, str], ...]
    impacts: tuple[tuple[str, str, str, str], ...]
    coverage: str
    unknown_reason: str | None
    before_unknown: str | None
    inputs_digest: str
    digest: str
    notes: tuple[str, ...] = ()

    @property
    def served_changed(self) -> bool:
        return bool(self.added or self.removed or self.retargeted)

    @property
    def destructive(self) -> bool:
        return bool(self.removed or self.retargeted) or self.before_unknown is not None

    @property
    def changed(self) -> bool:
        return self.served_changed or bool(self.authority) or self.before_unknown is not None

    def refusal(self) -> str | None:
        """Unknown live impact refuses a removal or a retarget (never a force)."""

        if self.destructive and self.coverage != "complete":
            return UNKNOWN_IMPACT.format(reason=self.unknown_reason or "unknown")
        return None

    def impact_summary(self) -> str:
        if self.coverage != "complete":
            return "unknown"
        holders = sorted({entry[0] for entry in self.impacts})
        return f"{len(holders)} live session(s)" if holders else "none"

    def lines(self, *, confirm: bool = False) -> list[str]:
        """The preview text. With no served change it is compact:
        the header, ``Served selectors: unchanged.``, any authority diff
        (never omitted) and the digest; the scope and the live-impact
        sections appear only when something served moves."""

        if not self.served_changed and self.before_unknown is None:
            out = [HEADER, NO_SERVED_CHANGE]
            if self.authority:
                out += ["Offered/admitted changes",
                        *(f"  {key}: {old} → {new}" for key, old, new in self.authority)]
            out += [*self.notes, f"Plan digest: {self.digest[:16]}"]
            if confirm:
                out.append(CONFIRM_TEXT.rstrip())
            return out
        out = [HEADER, f"Scope: gateway {self.gateway} · state root {self.state_root}"]
        if self.before_unknown is not None:
            # Nothing to compare with: a count, not a list of every selector
            # (``claude-multi plan --json`` carries the full candidate set).
            out += ["", f"Published render: unknown — {self.before_unknown}; {len(self.added)} selector(s) "
                        "would be served (claude-multi plan --json lists them)"]
        if self.added and self.before_unknown is None:
            out += ["", "Added", *(f"  {selector} → {route.label()}" for selector, route in self.added)]
        if self.retargeted:
            out += ["", "Retargeted", *(f"  {selector}: {old.label()} → {new.label()}"
                                         for selector, old, new in self.retargeted)]
        if self.removed:
            out += ["", "Removed", *(f"  {selector}" for selector, _route in self.removed)]
        if self.authority:
            out += ["", "Offered/admitted changes",
                    *(f"  {key}: {old} → {new}" for key, old, new in self.authority)]
        if self.coverage != "complete":
            out += ["", UNKNOWN_IMPACT.format(reason=self.unknown_reason or "unknown")]
        elif self.impacts:
            out += ["", "Live session impact",
                    *(f"  {id8} {slot}: {selector} {effect}" for id8, slot, selector, effect in self.impacts)]
        else:
            out += ["", "Live session impact: none"]
        out.append(f"Coverage: {self.coverage} for the stated root")
        out += [*self.notes]
        out.append(f"Plan digest: {self.digest[:16]}")
        if confirm:
            out.append(CONFIRM_TEXT.rstrip())
        return out

    def text(self, *, confirm: bool = False) -> str:
        return "\n".join(self.lines(confirm=confirm)) + "\n"

    def as_document(self) -> dict[str, Any]:
        return {
            "gateway": self.gateway,
            "state_root": self.state_root,
            "added": [{"selector": s, "route": r.as_document()} for s, r in self.added],
            "removed": [{"selector": s, "route": r.as_document()} for s, r in self.removed],
            "retargeted": [{"selector": s, "before": o.as_document(), "after": n.as_document(),
                            "changed": list(o.changed_parts(n))} for s, o, n in self.retargeted],
            "authority": [{"key": k, "before": o, "after": n} for k, o, n in self.authority],
            "live_impact": [{"session": i, "slot": slot, "selector": s, "effect": e}
                            for i, slot, s, e in self.impacts],
            "coverage": self.coverage,
            "unknown_reason": self.unknown_reason,
            "published_unknown": self.before_unknown,
            "inputs_digest": self.inputs_digest,
            "digest": self.digest,
        }

    def doctor_fact(self) -> str | None:
        """The read-only doctor Attention line (declared vs published render)."""

        if not self.served_changed:
            return None
        return PENDING_FACT.format(added=len(self.added), retargeted=len(self.retargeted),
                                   removed=len(self.removed), impact=self.impact_summary())


# What an authority key without a recorded value means: the ledger records
# only a non-default transport, so no record is the account sign-in.
ABSENT_AUTHORITY = {"transport": "oauth-pool"}


def absent_authority(key: str) -> str:
    """The value an authority key has when nothing records it (``—``: none)."""

    return ABSENT_AUTHORITY.get(key.split(" ", 1)[0], "—")


def build_plan(
    before: Mapping[str, RouteIdentity] | None,
    after: Mapping[str, RouteIdentity],
    *,
    references: References,
    state_root: str,
    gateway: str,
    authority_before: Mapping[str, str] | None = None,
    authority_after: Mapping[str, str] | None = None,
    before_unknown: str | None = None,
    inputs: Mapping[str, Any] | None = None,
    notes: Iterable[str] = (),
) -> ServedChangePlan:
    """Diff the published identities against a candidate's (pure)."""

    if before is None and before_unknown is None:
        before_unknown = "published render unavailable"
    previous = dict(before or {})
    added = tuple((s, after[s]) for s in sorted(set(after) - set(previous)))
    removed = tuple((s, previous[s]) for s in sorted(set(previous) - set(after)))
    retargeted = tuple((s, previous[s], after[s]) for s in sorted(set(previous) & set(after))
                       if previous[s] != after[s])
    old_authority = dict(authority_before or {})
    new_authority = dict(authority_after if authority_after is not None else old_authority)
    authority = tuple((key, old_authority.get(key, absent_authority(key)), new_authority.get(key, absent_authority(key)))
                      for key in sorted(set(old_authority) | set(new_authority))
                      if old_authority.get(key, absent_authority(key)) != new_authority.get(key, absent_authority(key)))
    impacts: list[tuple[str, str, str, str]] = []
    moving = [(s, "retargets") for s, _o, _n in retargeted] + [(s, "vanishes") for s, _r in removed]
    for selector, effect in moving:
        base = _base(selector)
        for record_id in sorted(set(references.refs.get(base, frozenset())) & set(references.live)):
            slot_map = references.slots.get(record_id, {})
            named = tuple(slot for slot in slot_map.get(base, ()) if slot != "fence")
            for slot in named or ("fence",):
                impacts.append((record_id[:8], slot, selector, effect))
    coverage = "complete" if references.unknown is None else "unknown"
    inputs_document = {
        "before": None if before is None else {s: r.as_document() for s, r in sorted(previous.items())},
        "before_unknown": before_unknown,
        "after": {s: r.as_document() for s, r in sorted(after.items())},
        "authority_before": old_authority,
        "authority_after": new_authority,
        "references": references.fingerprint(),
        "state_root": state_root,
        "gateway": gateway,
        "inputs": dict(inputs or {}),
    }
    inputs_digest = _digest(inputs_document)
    diff = {
        "added": [s for s, _r in added], "removed": [s for s, _r in removed],
        "retargeted": [s for s, _o, _n in retargeted], "authority": [list(a) for a in authority],
        "impacts": [list(i) for i in impacts], "coverage": coverage,
    }
    return ServedChangePlan(
        gateway=gateway, state_root=state_root, added=added, removed=removed, retargeted=retargeted,
        authority=authority, impacts=tuple(sorted(set(impacts))), coverage=coverage,
        unknown_reason=references.unknown, before_unknown=before_unknown,
        inputs_digest=inputs_digest, digest=_digest({"inputs": inputs_digest, "diff": diff}),
        notes=tuple(notes),
    )


# ------------------------------------------------------------ root authority
def root_refusal(managed: str | None, requested: str, *, unreadable: str | None = None) -> str | None:
    """The single-root boundary: a served change or a new managed
    reference from another root refuses (no authority yet: allowed). An
    ``unreadable`` authority (its reason) is unknown and always refuses."""

    if unreadable is not None:
        return ROOT_UNREADABLE_REFUSAL.format(reason=unreadable, requested=requested)
    if managed is None or str(managed) == str(requested):
        return None
    return ROOT_REFUSAL.format(managed=managed, requested=requested)
