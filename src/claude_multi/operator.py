"""The operator layer core: ``providers.d`` declarations.

Trust tiers: T0 is code (kinds,
adapters, contracts, validators, credential policy); T1 the reviewed catalog;
T2 the operator's ``providers.d/<id>.json`` declarations; tool-owned state
(route approvals, admissions, aliases, evidence) lives in the private ledger
and evidence files. T2 *selects* T0/T1 vocabulary and never extends it: no
adapters, pools, transports, passthrough routes, payload-contract code or
authored evidence. This protects against implicit routing changes, stale
admission and author-supplied evidence on the guarded paths; it is not a
defence against a process that deliberately forges the operator's private
files.

This module owns parsing and validating provider files, the composed entry
schema, provider/line resolution, definition and route digests, the
collision and one-origin-per-secret indexes, read-only ledger/evidence
loading, the merged docs view, the admission predicate, the legacy
``custom.json`` migration preflight and
:func:`displaced_lines`. It never imports the CLI, screens or
TUI and never performs network calls or prompts: callers inject those.

Import direction (keep it acyclic): ``operator`` may import ``catalog``,
``custom``, ``render``, ``secret_store`` and ``state``; none of those may
import ``operator``.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import ast
import stat
import datetime
import errno
import ipaddress
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from . import assets, catalog, custom, errors, paths, render, secret_store, state, strict_json
from . import sessions as sessions_mod
from . import validate as schema_validate


class OperatorError(errors.ClaudeMultiError, ValueError):
    """An operator input (ledger, evidence, candidate docs) is unusable."""


# ------------------------------------------------------------ constants
SCHEMA_META_VERSION = 1
DATA_VERSION = 1
PROVIDERS_DIRNAME = "providers.d"
LEDGER_NAME = "operator-ledger.json"
EVIDENCE_NAME = "operator-evidence.json"
# The activation marker of ``providers migrate-custom``; excluded from
# enumeration by the file-name rule (a dot file never matches FILE_NAME).
MIGRATION_MARKER = ".migrated-from-custom.json"
FILE_NAME = re.compile(r"^[a-z][a-z0-9-]*\.json$")
PROVIDER_ID = re.compile(r"^[a-z][a-z0-9-]*$")
NEW_KEY = catalog.OPERATOR_KEY
MIGRATED_KEY = catalog.MIGRATED_KEY
READ_LIMIT_BYTES = 65536
MAX_PROVIDER_FILES = 256
LINE_ORIGINS = catalog.LINE_ORIGINS
OPERATOR_ORIGINS = frozenset({"operator", "operator-migrated"})

# The migration predicate. All-or-nothing; the legacy model
# id grammar that maps to a rollback-safe ``custom-<id>`` key.
LEGACY_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
LEGACY_MODEL_ID_MAX = 57
LEGACY_PROVIDER_ID = PROVIDER_ID
MIGRATION_SECRET_NAME = secret_store.SECRET_NAME
MIGRATION_HEADER = "x-api-key"
MIGRATED_FROM = "custom.json"
MIGRATED_FAMILY = "unknown"

# Reviewed T2 kinds -> (adapter, transport kind). ``openai-compatible``
# (keyed generic compat) resolves only while the trusted catalog's
# ``audits.openai_compat_keyed`` gate is open (``catalog.keyed_compat_audited``);
# a closed gate refuses it by metadata alone, before any store access, and
# never downgrades it to the keyless LAN kind.
KINDS: Mapping[str, tuple[str, str]] = {
    "anthropic-compatible": ("cliproxy-claude-compatible-v1", "direct"),
    "openai-compatible-lan": ("cliproxy-openai-compat-v1", "direct-openai"),
    "openai-compatible": (catalog.OPENAI_COMPAT_ADAPTER, catalog.OPENAI_COMPAT_TRANSPORT),
}
LAN_KIND = "openai-compatible-lan"
KEYED_KIND = "openai-compatible"
KEYED_GATE_REMEDY = "use anthropic-compatible, or openai-compatible-lan for a keyless LAN server"
# The only static header the keyed kind accepts (canonical spelling).
KEYED_HEADER = "X-Client"
_VISIBLE_ASCII = re.compile(r"^[\x20-\x7e]{1,256}$")
# The gateway's openai-compatibility provider-key normalization:
# trim + lower-case; ``openai-compatibility`` and ``openai-compatible-*`` keep
# their name, anything else gets the ``openai-compatible-`` prefix.
COMPAT_KEY_PREFIX = "openai-compatible-"
COMPAT_KEY_LEGACY = "openai-compatibility"

DEFAULT_GENERATION = "op"
OPERATOR_MINIMUM_CLAUDE = "2.1.216"  # the custom.json baseline (custom.py)
ONE_MILLION = 1_000_000
DEFAULT_CLIENT_TOKENS = 200_000
SELECTOR_SUFFIX = "[1m]"
# Capabilities and roles are recommendations retained in the definition
# digest. Route safety applies to leads and agents alike.
RETENTION_AUDITED_ADAPTERS = frozenset({
    "cliproxy-claude-compatible-v1",  # the Claude executor boundary (AGENTS §2.11)
    "cliproxy-oauth-claude-v1",
    "cliproxy-oauth-codex-v1",
})
# An aggregator's omitted line family is unknown, not the aggregator's own
# family. Explicit labels are accepted independently of trusted recognition.
AGGREGATOR_PROVIDERS = catalog.AGGREGATOR_PROVIDERS
UNKNOWN_FAMILY = "unknown"

# Endpoint policy. Lexical only: it proves nothing about DNS or
# redirects (the gateway harness and its redirect patch cover those).
GATEWAY_PORTS = frozenset({8316, 8317})
METADATA_HOSTS = frozenset({"metadata", "metadata.google.internal", "metadata.goog"})
METADATA_ADDRESSES = frozenset(
    {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("100.100.100.200"),
     ipaddress.ip_address("fd00:ec2::254")}
)
LAN_SUFFIXES = (".lan", ".local", ".home.arpa", ".internal")
_DNS_NAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_DOTTED_QUAD = re.compile(r"^(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}$")
_URL_CHARS = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$")

# Static header policy: safe static headers only.
MAX_HEADERS = 8
_HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
FORBIDDEN_HEADERS = frozenset({
    "authorization", "proxy-authorization", "x-api-key", "api-key", "cookie", "set-cookie",
    "host", "content-length", "content-type", "transfer-encoding", "connection", "upgrade",
    "te", "trailer", "keep-alive", "anthropic-version", "anthropic-beta",
})
_CREDENTIAL_HEADER_WORDS = ("auth", "token", "secret", "key", "password", "cookie", "session")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# ------------------------------------------------------------ result types
@dataclass(frozen=True)
class OperatorProblem:
    """One diagnostic. Never carries a secret value; author text only after
    the secret-shape scan passed."""

    code: str
    file: str
    json_path: str
    line: int | None
    column: int | None
    subject: str
    remedy: str | None = None

    @property
    def error_id(self) -> str | None:
        """Error-condition mapping; unrelated failures keep their own codes."""
        if self.code in {"auth", "schema"} and self.json_path.endswith(".auth.header") and "only x-api-key" in self.subject:
            return "E15"
        if self.code == "pool":
            return "E02"  # supported-pool line shape, never a pool prohibition
        if self.code == "catalog-provider":
            return "E14" if "pool admission" in self.subject else "E03"
        if self.code == "schema":
            if "evidence is tool-owned" in self.subject:
                return "E04"
            if "pool admission" in self.subject:
                return "E14"
            return "E02"
        if self.code == "agents" and "retention-audited" in self.subject:
            return "E07"
        if self.code == "auth" and "only x-api-key" in self.subject:
            return "E15"
        if self.code == "headers" and ("credential-carrying" in self.subject or "copy client headers" in self.subject):
            return "E17"
        if self.code in {"endpoint", "listing"} and any(word in self.subject for word in
                ("only over https", "forbidden", "gateway port")):
            return "E18"
        if self.code in {"unsafe-file", "unsafe-directory"} and "writable by group/other" in self.subject:
            return "E09"
        if self.code == "collision" and "(provider, wire)" in self.subject:
            return "E06"
        if self.code in {"line", "effort", "provenance", "contract", "auth", "family", "agents"}:
            return "E02"
        return {"json": "E01", "key": "E05", "displaced": "E06", "secret-literal": "E08",
                "listing-origin": "E10", "secret-origin": "E10"}.get(self.code)

    def text(self) -> str:
        where = self.file
        if self.line is not None:
            where += f":{self.line}" + (f":{self.column}" if self.column is not None else "")
        elif self.json_path and self.json_path != "$":
            where += ": " + self.json_path.removeprefix("$.")
        return f"{where}: {self.subject}" + (f" — {self.remedy}" if self.remedy else "")


@dataclass(frozen=True)
class ResolvedProvider:
    """A valid T2 provider block, synthesized into the catalog provider shape."""

    provider_id: str
    file: str
    entry: dict[str, Any]  # catalog-provider-shaped (+ ``origin``)
    kind: str
    origin: str  # normalized scheme://host[:port]
    base_url: str  # normalized full URL
    secret_name: str | None
    auth_kind: str
    header: str | None
    listing: dict[str, Any] | None
    listing_origin: str | None
    headers: dict[str, str]
    route_digest: str


@dataclass(frozen=True)
class ResolvedOperatorLine:
    """A declaration resolved into a closed core entry plus explicit metadata."""

    key: str
    core_entry: dict[str, Any]
    family: str
    source: str  # the declaring file, e.g. providers.d/groq.json
    definition_digest: str
    selector_mode: str
    origin: str  # operator | operator-migrated
    provider_id: str
    legacy_key: str | None
    output_tokens: int | None


@dataclass(frozen=True)
class OperatorLedger:
    """The private tool-owned trust store (validated; read-only copy)."""

    routes: dict[str, Any]
    admissions: dict[str, Any]
    aliases: dict[str, Any]
    pruned: dict[str, int]
    removed: dict[str, Any]
    transport_choices: dict[str, str]
    migration: dict[str, Any] | None
    sha256: str | None = None

    @classmethod
    def empty(cls) -> "OperatorLedger":
        return cls({}, {}, {}, {}, {}, {}, None, None)


@dataclass(frozen=True)
class OperatorEvidence:
    """Tool-owned, verdict-only evidence (the admission smoke writes it)."""

    lines: dict[str, Any]
    sha256: str | None = None


@dataclass(frozen=True)
class OperatorSchemas:
    provider: dict[str, Any]
    ledger: dict[str, Any]
    evidence: dict[str, Any]
    entry: dict[str, Any]  # the composed model-entry schema


@dataclass(frozen=True)
class ProvidersRead:
    """What an owned-readable scan of ``providers.d`` found (no writes)."""

    path: Path
    exists: bool
    symlinked: bool
    files: dict[str, bytes]  # file id -> raw bytes (readable files only)
    marker: bool
    problems: tuple[OperatorProblem, ...]


@dataclass(frozen=True)
class OperatorLayer:
    """One resolved operator snapshot for every consumer."""

    providers: dict[str, ResolvedProvider]
    lines: dict[str, ResolvedOperatorLine]
    metadata: dict[str, dict[str, Any]]
    route_status: dict[str, str]
    problems_by_file: dict[str, tuple[OperatorProblem, ...]]
    layer_digest: str
    secret_names: frozenset[str]
    displaced: dict[str, ResolvedOperatorLine] = field(default_factory=dict)
    pending: tuple[str, ...] = ()
    legacy_dropped: dict[str, str] = field(default_factory=dict)
    route_changes: dict[str, tuple[str, str]] = field(default_factory=dict)
    marker: bool = False

    @property
    def problems(self) -> tuple[OperatorProblem, ...]:
        return tuple(p for name in sorted(self.problems_by_file) for p in self.problems_by_file[name])


@dataclass(frozen=True)
class DisplacedLine:
    """An operator line whose (provider, wire) a candidate catalog also serves."""

    key: str
    provider: str
    wire: str
    catalog_keys: tuple[str, ...]
    admitted: bool


@dataclass(frozen=True)
class MigrationPreflight:
    """Pure all-or-nothing plan input for ``providers migrate-custom``."""

    documents: dict[str, dict[str, Any]]
    problems: tuple[OperatorProblem, ...]
    # Legacy providers no custom.json model references are
    # listed as skipped — never written, approved or rendered; their field
    # problems are reported (doctor A07) but refuse nothing.
    zero_model_providers: tuple[str, ...]
    skipped_problems: tuple[OperatorProblem, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


# ------------------------------------------------------------ paths
def providers_dir(environ: Mapping[str, str]) -> Path:
    """HOME-relative, like the gateway config (never ``XDG_CONFIG_HOME``)."""

    return paths.gateway_config_dir(environ) / PROVIDERS_DIRNAME


def ledger_path(environ: Mapping[str, str]) -> Path:
    return paths.gateway_config_dir(environ) / LEDGER_NAME


def evidence_path(environ: Mapping[str, str]) -> Path:
    return paths.state_root(dict(environ)) / EVIDENCE_NAME


def file_label(file_id: str) -> str:
    return f"{PROVIDERS_DIRNAME}/{file_id}.json"


# ------------------------------------------------------------ schemas
def compose_entry_schema(models_schema: Mapping[str, Any]) -> dict[str, Any]:
    """The installed model-entry schema plus only the ``custom-<tokens>``
    ordinary-profile branch (never a hand-written second entry schema)."""

    entry = copy.deepcopy(models_schema["properties"]["models"]["additionalProperties"])
    profile = entry["properties"]["context"]["properties"]["ordinary_profile"]
    profile["oneOf"].append({"type": "string", "pattern": "^custom-[0-9]{4,7}$"})
    return {"version": models_schema.get("version", SCHEMA_META_VERSION), **entry}


def load_schemas(asset_root: Path | str | None = None) -> OperatorSchemas:
    """Load the operator schemas and compose the entry schema (assets seam)."""

    base = assets.root(asset_root) / "schemas"
    loaded: dict[str, Any] = {}
    for name in ("operator-provider", "operator-ledger", "operator-evidence", "models"):
        document = strict_json.load(base / f"{name}.schema.json")
        schema_validate.check_schema(document)
        loaded[name] = document
    return OperatorSchemas(
        provider=loaded["operator-provider"],
        ledger=loaded["operator-ledger"],
        evidence=loaded["operator-evidence"],
        entry=compose_entry_schema(loaded["models"]),
    )


# ------------------------------------------------------------ helpers
def _digest(kind: str, document: Any) -> str:
    return strict_json.sha256_hex(strict_json.canonical_bytes({"v": 1, "kind": kind, "body": document}))


def _short(value: Any) -> str:
    return strict_json.sha256_hex(strict_json.canonical_bytes(value))[:16]


def _problem(code: str, file: str, json_path: str, subject: str, remedy: str | None = None) -> OperatorProblem:
    return OperatorProblem(code, file, json_path, None, None, subject, remedy)


def _selector_base(selector: str) -> str:
    return selector[: -len(SELECTOR_SUFFIX)] if selector.endswith(SELECTOR_SUFFIX) else selector


def _docs_lines(docs: Mapping[str, Any]) -> dict[str, Any]:
    return (docs["models-v2"] if "models-v2" in docs else docs["models"])["models"]


_REDACTED = "<redacted>"
_REDACTED_SUBJECT = "diagnostic withheld: it would show a secret-like or stored credential literal"


def _store_literals(store_values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(value for value in store_values if isinstance(value, str) and len(value) >= 8))


def _secret_bearing(text: str, literals: Iterable[str] = ()) -> bool:
    """Token-shaped or stored-value text. No trusted-data exemptions: an
    ``env:``/``sha256:`` prefix or a hash-named field never excuses a
    declaration (the catalog scanner's exemptions are for trusted data)."""

    return any(shape.search(text) for shape in catalog._SECRET_SHAPES) or any(value in text for value in literals)


def _secret_hits(document: Any, path: str, store_values: Iterable[str]) -> list[str]:
    """JSON paths of secret-shaped or stored-value literals in keys and
    values (never the values; a secret-bearing key is redacted in every
    path that would name it)."""

    literals = _store_literals(store_values)
    hits: list[str] = []

    def walk(node: Any, where: str) -> None:
        if isinstance(node, dict):
            for key in sorted(node):
                segment = key
                if _secret_bearing(key, literals):
                    segment = _REDACTED
                    hits.append(f"{where}.{_REDACTED} (key)")
                walk(node[key], f"{where}.{segment}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{where}[{index}]")
        elif isinstance(node, str) and _secret_bearing(node, literals):
            hits.append(where)

    walk(document, path)
    return sorted({hit if not _secret_bearing(hit, literals) else f"{path} ({_REDACTED})" for hit in hits})


def _raw_secret_hit(raw: bytes, store_values: Iterable[str] = ()) -> bool:
    return _secret_bearing(raw.decode("utf-8", errors="replace"), _store_literals(store_values))


def _json_failure(exc: strict_json.StrictJSONError) -> str:
    """A fixed, author-text-free category for a strict-JSON failure (parser
    messages can quote authored keys, e.g. a duplicate key)."""

    cause = exc.__cause__
    message = str(exc)
    if isinstance(cause, json.JSONDecodeError):
        return f"invalid JSON at line {cause.lineno} column {cause.colno}: {cause.msg}"
    if isinstance(cause, UnicodeDecodeError):
        return "invalid UTF-8"
    if message.startswith("duplicate key"):
        return "a duplicate object key (not shown)"
    if message.startswith("non-finite number literal"):
        return "a non-finite number literal"
    if "trailing data" in message:
        return "trailing data after the JSON document"
    return "exceeds a size, nesting or string-length limit"


def _scrub(problem: OperatorProblem, literals: Iterable[str] = ()) -> OperatorProblem:
    """Final guard: withhold any diagnostic field that would show a
    secret-like or stored credential literal."""

    literals = tuple(literals)
    changes: dict[str, str] = {}
    if _secret_bearing(problem.file, literals):
        changes["file"] = f"{PROVIDERS_DIRNAME}/{_REDACTED}.json"
    if problem.json_path and _secret_bearing(problem.json_path, literals):
        changes["json_path"] = f"$ ({_REDACTED})"
    if _secret_bearing(problem.subject, literals):
        changes["subject"] = _REDACTED_SUBJECT
    if problem.remedy and _secret_bearing(problem.remedy, literals):
        changes["remedy"] = "remove the literal; reference credentials as env:NAME"
    return dataclasses.replace(problem, **changes) if changes else problem


# ------------------------------------------------------------ endpoint policy
def normalize_endpoint(url: Any, *, keyed: bool, lan: bool, gateway_ports: Iterable[int] = GATEWAY_PORTS) -> tuple[str, str]:
    """``(origin, normalized_url)`` or raise :class:`ValueError` naming the rule.

    HTTPS for keyed routes; keyless HTTP only on the reviewed LAN kind and
    only for local-network host forms; no userinfo, query, fragment,
    malformed or zero port, ambiguous numeric host spelling, non-ASCII host,
    metadata/link-local/unspecified/multicast address, or a gateway port on
    a loopback host. The default port is dropped; the host is lower-cased.
    """

    if not isinstance(url, str) or not url or len(url) > 256:
        raise ValueError("must be an absolute URL of at most 256 characters")
    if _CONTROL.search(url) or " " in url or not _URL_CHARS.fullmatch(url):
        raise ValueError("contains characters that are not allowed in a URL")
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    allowed = {"https"} if keyed or not lan else {"http", "https"}
    if scheme not in allowed:
        raise ValueError("a secret is sent only over https" if keyed else f"scheme {scheme or '(none)'} is not allowed")
    if "@" in parts.netloc:
        raise ValueError("userinfo is not allowed in a provider URL")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ValueError("a provider URL has no query or fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("malformed port") from exc
    host = parts.hostname or ""
    if not host:
        raise ValueError("missing host")
    if port == 0:
        raise ValueError("port 0 is not allowed")
    bracketed = parts.netloc.split("@")[-1].startswith("[")
    address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    if bracketed:
        try:
            address = ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ValueError("malformed IPv6 host") from exc
    elif re.fullmatch(r"[0-9.]+", host) or re.search(r"(^|\.)0x", host):
        if not _DOTTED_QUAD.fullmatch(host):
            raise ValueError("ambiguous numeric host spelling")
        address = ipaddress.IPv4Address(host)
    elif not _DNS_NAME.fullmatch(host):
        raise ValueError("host is not a plain lower-case DNS name")
    elif host.split(".")[-1].isdigit():
        raise ValueError("ambiguous numeric host spelling")
    if address is not None:
        if address in METADATA_ADDRESSES or address.is_link_local:
            raise ValueError("metadata and link-local addresses are forbidden")
        if address.is_unspecified or address.is_multicast or (
            isinstance(address, ipaddress.IPv4Address) and address == ipaddress.IPv4Address("255.255.255.255")
        ):
            raise ValueError("unspecified, broadcast and multicast addresses are forbidden")
    elif host in METADATA_HOSTS:
        raise ValueError("metadata hosts are forbidden")
    loopback = host == "localhost" or (address is not None and address.is_loopback)
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if loopback and effective_port in set(gateway_ports):
        raise ValueError("the claude-multi gateway port is not a provider")
    if lan:
        local = loopback or (address is not None and address.is_private) or (
            address is None and ("." not in host or host.endswith(LAN_SUFFIXES))
        )
        if not local:
            raise ValueError("the keyless LAN kind needs a local-network host")
    host_text = f"[{address.compressed}]" if isinstance(address, ipaddress.IPv6Address) else host
    default = 443 if scheme == "https" else 80
    origin = f"{scheme}://{host_text}" + (f":{port}" if port is not None and port != default else "")
    path = parts.path.rstrip("/")
    return origin, origin + path


def gateway_ports(docs: Mapping[str, Any]) -> frozenset[int]:
    """The fixed gateway ports plus the catalog gateway endpoint's port."""

    ports = set(GATEWAY_PORTS)
    try:
        base = docs["gateway"]["gateway"]["base_url"]
        port = urllib.parse.urlsplit(base).port
        if port:
            ports.add(port)
    except (KeyError, TypeError, ValueError):
        pass
    return frozenset(ports)


def header_problem(name: Any, value: Any) -> str | None:
    """Why a static header is refused (credential/hop-by-hop names, control
    characters, CR/LF, ``$`` interpolation), or None. Never echoes the value."""

    if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
        return f"header name {name!r} is not a plain token"
    lower = name.lower()
    if lower in FORBIDDEN_HEADERS or any(word in lower for word in _CREDENTIAL_HEADER_WORDS):
        return "credential-carrying header names are refused"
    if not isinstance(value, str) or not value or len(value) > 256:
        return f"header {name} needs a value of 1..256 characters"
    if _CONTROL.search(value):
        return f"header {name} contains control characters (CR/LF)"
    if "$" in value:
        return 'values containing "$" copy client headers upstream — refused'
    return None


def keyed_headers(headers: Any) -> tuple[dict[str, str], list[str]]:
    """The closed static-header policy of the keyed kind.

    Only an optional ``X-Client`` (any case; canonical spelling rendered)
    with one nonblank visible-ASCII value of at most 256 characters, no
    leading/trailing whitespace, no ``$`` and no secret-shaped text. Any
    other name and any case-insensitive duplicate is refused, never dropped.
    Returns ``(canonical headers, value-free reasons)``; never echoes an
    authored name or value.
    """

    if not isinstance(headers, Mapping):
        return {}, ["static headers must be an object"]
    reasons: list[str] = []
    found: dict[str, str] = {}
    names = [name for name in headers if isinstance(name, str)]
    if len({name.lower() for name in names}) != len(headers):
        reasons.append("header names repeat case-insensitively")
    for name in sorted(names):
        value = headers[name]
        if name.lower() != KEYED_HEADER.lower():
            reasons.append(f"the keyed openai-compatible kind accepts only the {KEYED_HEADER} static header")
            continue
        if (not isinstance(value, str) or not _VISIBLE_ASCII.fullmatch(value) or value != value.strip()
                or "$" in value or _secret_bearing(value)):
            reasons.append(f"{KEYED_HEADER} needs one nonblank visible-ASCII value of at most 256 characters, "
                           "without surrounding whitespace, \"$\" or secret-like text")
            continue
        found[KEYED_HEADER] = value
    return ({} if reasons else found), sorted(set(reasons))


def compat_provider_key(name: str) -> str:
    """The gateway's internal openai-compatibility provider key."""

    key = name.strip().lower()
    if key == COMPAT_KEY_LEGACY or key.startswith(COMPAT_KEY_PREFIX):
        return key
    return COMPAT_KEY_PREFIX + key


def keyed_declaration(document: Any) -> bool:
    """Metadata only: a declaration whose provider block names the keyed kind."""

    block = document.get("provider") if isinstance(document, Mapping) else None
    return isinstance(block, Mapping) and block.get("kind") == KEYED_KIND


def keyed_preflight(docs: Mapping[str, Any], file_id: str, raw: bytes) -> OperatorProblem | None:
    """Metadata-only preflight: the sanitized refusal of a keyed
    declaration while the trusted audit gate is closed, or None.

    Bounded strict parse and the trusted accessor only: no secret store
    (``get``/``is_set``/``scan_values``), no network, and no authored text
    in the refusal. A file this cannot parse is left to the ordinary
    validation (which may then screen it against stored literals).
    """

    if catalog.keyed_compat_audited(docs.get("gateway")):
        return None
    try:
        document = strict_json.loads(raw, strict_json.JSONLimits(max_bytes=READ_LIMIT_BYTES, max_string=4096))
    except (strict_json.StrictJSONError, ValueError):
        return None
    if not keyed_declaration(document):
        return None
    # The metadata-only secret-shape scrub (no stored literals: the store is
    # never consulted here), as ordinary validation applies to the label.
    return _scrub(_problem("kind-gated", file_label(file_id), "$.provider.kind", catalog.KEYED_AUDIT_CLOSED,
                           KEYED_GATE_REMEDY))


def store_scan_needed(docs: Mapping[str, Any], files: Mapping[str, bytes]) -> bool:
    """Whether a layer over ``files`` has an independently valid candidate
    contribution that keeps the defensive stored-literal scan: False
    only when every file is a solely rejected closed-keyed declaration."""

    return any(keyed_preflight(docs, file_id, raw) is None for file_id, raw in files.items())


def _scan_values(secret_values: "frozenset[str] | Callable[[], Iterable[str] | None] | None",
                 docs: Mapping[str, Any], files: Mapping[str, bytes]) -> frozenset[str]:
    """Stored values for the literal screen: a callable is invoked only when
    the layer has an independently valid contribution."""

    if secret_values is None or not files:
        return frozenset()
    if callable(secret_values):
        if not store_scan_needed(docs, files):
            return frozenset()
        return frozenset(secret_values() or ())
    return frozenset(secret_values)


def route_digest(
    *, secret_ref: str | None, auth_kind: str, header: str | None, origin: str,
    base_url: str, listing_origin: str | None, listing_auth: str | None,
) -> str:
    """The credential-route identity an approval binds to.

    A path-only edit keeps a bearer route approved; a header-auth route binds
    the normalized full base URL (interim, until the redirect-refusal
    gateway patch makes it unnecessary).
    """

    return _digest("operator-route", {
        "secret_ref": secret_ref,
        "auth": auth_kind,
        "header": header,
        "origin": origin,
        "url": base_url if auth_kind == "header" else None,
        "listing_origin": listing_origin,
        "listing_auth": listing_auth,
    })


def catalog_route_digest(provider: Mapping[str, Any]) -> str:
    """Route identity of a catalog (T1) provider, for captures on T1 routes."""

    transport = provider["transport"]
    if transport["kind"] == "oauth-pool":
        return _digest("catalog-route", {"pool": transport["pool"]})
    auth = transport.get("auth", {})
    return _digest("catalog-route", {
        "kind": transport["kind"], "base_url": transport.get("base_url"),
        "auth": auth.get("kind"), "header": auth.get("header"), "secret_ref": auth.get("secret_ref"),
    })


# ------------------------------------------------------------ digests
def definition_fields(key: str, line: Mapping[str, Any], provider_id: str, provider: Mapping[str, Any], *,
                      family: str, selector_mode: str, headers: Mapping[str, str] | None,
                      output_tokens: int | None) -> dict[str, Any]:
    """The routing-relevant definition (cosmetic display/notes/source text,
    generation label and routing note excluded; only used contracts)."""

    efforts = line["efforts"]
    if isinstance(efforts, list):
        effort_map: dict[str, Any] = {level: None for level in efforts}
        selectors = [line["selector"]]
    else:
        effort_map = {level: spec["proxy_contract"] for level, spec in efforts.items()}
        selectors = [efforts[level]["selector"] for level in sorted(efforts)]
    transport = provider["transport"]
    auth = transport.get("auth") or {}
    keyed: dict[str, Any] = {}
    if catalog.is_keyed_compat(provider):
        # The canonical effective level set (authored order never
        # moves the digest; the set and the default do).
        keyed["thinking_levels"] = list(catalog.keyed_compat_levels(line))
    return {
        **keyed,
        "key": key,
        "wire": line["wire_model"],
        "efforts": effort_map,
        "default_effort": line["default_effort"],
        "capabilities": list(line["capabilities"]),
        "roles": line["roles"],
        "context": line["context"]["declared_tokens"],
        "output": output_tokens,
        "family": family,
        "selectors": selectors,
        "selector_mode": selector_mode,
        "migration_waiver": selector_mode == "migrated",
        "provider": {
            "id": provider_id,
            "adapter": provider["adapter"],
            "transport": transport["kind"],
            "pool": transport.get("pool"),
            "base_url": transport.get("base_url"),
            "auth": auth.get("kind"),
            "secret_ref": auth.get("secret_ref"),
            "header": auth.get("header"),
            "headers": dict(sorted((headers or {}).items())),
            "family": provider["independence_family"],
        },
        "contracts": sorted({c for c in effort_map.values() if c is not None}),
    }


def definition_digest(fields: Mapping[str, Any]) -> str:
    return _digest("operator-definition", fields)


def definition_field_hashes(fields: Mapping[str, Any]) -> dict[str, str]:
    """Per-field hashes for the admission diagnostic ("which fields changed")."""

    return {name: _short(value) for name, value in sorted(fields.items())}


# ------------------------------------------------------------ reading
def read_providers_dir(
    directory: Path | str, *, trusted_uids: frozenset[int] | None = None,
    max_bytes: int = READ_LIMIT_BYTES,
) -> ProvidersRead:
    """Owned-readable scan of ``providers.d`` (the only effect of loading).

    Only ``^[a-z][a-z0-9-]*\\.json$`` names are provider files; the dot-file
    migration marker is noticed, never parsed as a provider. A symlinked
    directory is readable; writers treat it as read-only.
    Per-file failures isolate to that file.
    """

    target = Path(directory)
    problems: list[OperatorProblem] = []
    try:
        descriptor, symlinked = state.open_owned_readable_dir(target, trusted_uids=trusted_uids)
    except FileNotFoundError:
        return ProvidersRead(target, False, False, {}, False, ())
    except (state.StateError, OSError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        problems.append(_problem("unsafe-directory", PROVIDERS_DIRNAME, "", f"directory is unsafe or unreadable ({reason})",
                                 "make it owned by you, not group/other writable"))
        return ProvidersRead(target, True, os.path.islink(target), {}, False, tuple(problems))
    files: dict[str, bytes] = {}
    marker = False
    try:
        names = sorted(os.listdir(descriptor))
        if MIGRATION_MARKER in names:
            marker = True
        candidates = [name for name in names if FILE_NAME.fullmatch(name)]
        for name in names:
            if name != MIGRATION_MARKER and name.endswith(".json") and not FILE_NAME.fullmatch(name) \
                    and not name.startswith("."):
                problems.append(_problem("file-name", f"{PROVIDERS_DIRNAME}/{name}", "",
                                         "ignored: provider file names match ^[a-z][a-z0-9-]*\\.json$"))
        if len(candidates) > MAX_PROVIDER_FILES:
            problems.append(_problem("too-many-files", PROVIDERS_DIRNAME, "",
                                     f"more than {MAX_PROVIDER_FILES} provider files; none loaded"))
            candidates = []
        for name in candidates:
            label = f"{PROVIDERS_DIRNAME}/{name}"
            try:
                files[name[:-5]] = state.read_owned_readable(
                    name, max_bytes=max_bytes, dir_fd=descriptor, trusted_uids=trusted_uids
                )
            except FileNotFoundError:
                problems.append(_problem("unreadable", label, "", "dangling link or vanished file"))
            except (state.StateError, OSError) as exc:
                code = "too-large" if getattr(exc, "errno", None) == errno.EFBIG else "unsafe-file"
                reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
                if "is writable by group/other" in str(exc):
                    # Metadata only; never open a refused file to diagnose permissions.
                    try:
                        mode = stat.S_IMODE(os.stat(name, dir_fd=descriptor).st_mode)
                        subject = f"is writable by group/others ({mode:04o})"
                    except OSError:
                        subject = "is writable by group/others"
                    problems.append(_problem(code, label, "", subject, f"chmod 600 {target / name}"))
                else:
                    problems.append(_problem(code, label, "", f"not loaded ({reason})",
                                             "a provider file is a regular file (or a link to one) owned by you or root, "
                                             f"not group/other writable, at most {max_bytes} bytes"))
    finally:
        os.close(descriptor)
    return ProvidersRead(target, True, symlinked, files, marker, tuple(_scrub(p) for p in problems))


def parse_ledger(raw: bytes, schema: Mapping[str, Any]) -> OperatorLedger:
    """Strict, closed, fail-closed ledger parse (never repaired or inferred)."""

    try:
        document = strict_json.loads(raw)
    except strict_json.StrictJSONError as exc:
        raise OperatorError(f"{LEDGER_NAME}: {exc}") from exc
    problems = schema_validate.validate(document, dict(schema), "$")
    for key in document.get("admissions", {}) if isinstance(document, dict) else ():
        if not MIGRATED_KEY.fullmatch(key):
            problems.append(f"$.admissions: invalid line key {key!r}")
    for key in document.get("routes", {}) if isinstance(document, dict) else ():
        if not PROVIDER_ID.fullmatch(key):
            problems.append(f"$.routes: invalid provider id {key!r}")
    for key in document.get("aliases", {}) if isinstance(document, dict) else ():
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", key):
            problems.append(f"$.aliases: invalid selector base {key!r}")
    if problems:
        raise OperatorError(f"{LEDGER_NAME}: " + "; ".join(problems))
    return OperatorLedger(
        routes=copy.deepcopy(document["routes"]),
        admissions=copy.deepcopy(document["admissions"]),
        aliases=copy.deepcopy(document["aliases"]),
        pruned=dict(document["pruned"]),
        removed=copy.deepcopy(document["removed"]),
        transport_choices=dict(document["transport_choices"]),
        migration=copy.deepcopy(document.get("migration")),
        sha256=strict_json.sha256_hex(raw),
    )


def load_ledger(environ: Mapping[str, str], schemas: OperatorSchemas) -> OperatorLedger | None:
    """The private ledger, or None when absent. Corrupt/unsafe raises."""

    path = ledger_path(environ)
    if not os.path.lexists(path):
        return None
    try:
        raw = state.read_private(path)
    except state.StateError as exc:
        raise OperatorError(f"{LEDGER_NAME} unavailable or unsafe: {exc}") from exc
    return parse_ledger(raw, schemas.ledger)


def parse_evidence(raw: bytes, schema: Mapping[str, Any]) -> OperatorEvidence:
    try:
        document = strict_json.loads(raw)
    except strict_json.StrictJSONError as exc:
        raise OperatorError(f"{EVIDENCE_NAME}: {exc}") from exc
    problems = schema_validate.validate(document, dict(schema), "$")
    for key in document.get("lines", {}) if isinstance(document, dict) else ():
        if not MIGRATED_KEY.fullmatch(key):
            problems.append(f"$.lines: invalid line key {key!r}")
    if problems:
        raise OperatorError(f"{EVIDENCE_NAME}: " + "; ".join(problems))
    return OperatorEvidence(lines=copy.deepcopy(document["lines"]), sha256=strict_json.sha256_hex(raw))


def load_evidence(environ: Mapping[str, str], schemas: OperatorSchemas) -> OperatorEvidence | None:
    path = evidence_path(environ)
    if not os.path.lexists(path):
        return None
    try:
        raw = state.read_private(path)
    except state.StateError as exc:
        raise OperatorError(f"{EVIDENCE_NAME} unavailable or unsafe: {exc}") from exc
    return parse_evidence(raw, schemas.evidence)


# ------------------------------------------------------------ resolution
def overlay_channel(provider: Mapping[str, Any]) -> str | None:
    """Declarations address catalog provider ids (``anthropic``,
    ``openai``); the OAuth overlay channel is the provider's
    ``transport.pool`` (``claude``/``codex``), never a provider id."""

    transport = provider.get("transport", {})
    if transport.get("kind") != "oauth-pool":
        return None
    pool = transport.get("pool")
    return pool if pool in catalog.OVERLAY_CHANNELS else None


def _selector_mode(provider: Mapping[str, Any], efforts: Any, migrated: bool) -> str:
    if migrated:
        return "migrated"
    if provider["transport"]["kind"] == "oauth-pool":
        return "client_effort_existing_pool" if catalog.effort_mode(provider) == "client" else "gateway_effort_existing_pool"
    return "ordinary_list" if isinstance(efforts, list) else "ordinary_map"


# The Go overlay loader's int is 64-bit on supported builds.
GO_INT_MAX = 2**63 - 1


def overlay_projection(line: ResolvedOperatorLine, provider: Mapping[str, Any]) -> dict[str, Any] | None:
    """The closed overlay capability projection of an operator pool line.

    Authored lines carry no overlay field; the entry derives from the
    declared wire (the registration name), efforts, context tokens and
    output tokens. ``thinking_levels`` is derived on both pool channels
    (derived from the declared, native-bounded efforts, never guessed); no
    budget or zero/dynamic flag is invented; ``max_completion_tokens`` only
    when the line declares an output limit. None for non-pool lines. The renderer
    aggregates these per (channel, lower-case wire) before emission.
    """

    channel = overlay_channel(provider)
    if channel is None:
        return None
    entry = line.core_entry
    projection: dict[str, Any] = {"channel": channel, "max_context_length": entry["context"]["declared_tokens"]}
    if line.output_tokens is not None:
        projection["max_completion_tokens"] = line.output_tokens
    projection["thinking_levels"] = list(_catalog_line_levels(entry))
    return projection


def overlay_problem(projection: Mapping[str, Any], wire: str, *, display: str | None = None) -> str | None:
    """Python pre-validation of one projection against the loader rules the
    projection can violate (name grammar, display, positive 64-bit limits,
    levels)."""

    if not catalog.OVERLAY_NAME.fullmatch(wire):
        return f"wire {wire!r} is not a valid overlay model name"
    if display is not None and not render._display_ok(display):
        return "display must be nonblank without surrounding whitespace or control characters (overlay display-name)"
    for name in ("max_context_length", "max_completion_tokens"):
        value = projection.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= GO_INT_MAX):
            return f"{name} must be a positive 64-bit integer"
    levels = projection.get("thinking_levels")
    if projection["channel"] == "claude" and not levels:
        return "a claude-channel overlay entry needs thinking levels derived from declared efforts"
    if levels is not None and (len(set(levels)) != len(levels) or any(level not in catalog.OVERLAY_THINKING_LEVELS for level in levels)):
        return "thinking levels are outside the overlay loader vocabulary"
    return None


def derive_core_entry(
    key: str, line: Mapping[str, Any], provider_id: str, provider: Mapping[str, Any], *,
    migrated: bool, gateway_baseline: str, file: str,
) -> dict[str, Any]:
    """The closed catalog-shaped core entry of one declaration.

    Status ``new``; lead at the declared default effort with ``env: {}``;
    lead-only; class ``custom-<declared>``; floor ``min(declared, 200000)``;
    the qualification text says it is not benchmark-verified. A migrated
    line keeps the exact legacy selector and the legacy client window.
    """

    declared = line["context"]["declared_tokens"]
    if migrated:
        client = provider_tokens = declared
    else:
        client = ONE_MILLION if declared > DEFAULT_CLIENT_TOKENS else DEFAULT_CLIENT_TOKENS
        provider_tokens = min(declared, client)
    floor = min(declared, catalog.CUSTOM_VALIDATED_CAP)
    suffix = SELECTOR_SUFFIX if (client >= ONE_MILLION and not migrated) else ""
    efforts = line["efforts"]
    client_pool = provider["transport"]["kind"] == "oauth-pool" and catalog.effort_mode(provider) == "client"
    entry: dict[str, Any] = {
        "provider": provider_id,
        "display": line["display"],
        "generation": line.get("generation", DEFAULT_GENERATION),
        "wire_model": line["wire_model"],
    }
    if migrated:
        entry["selector"] = key
        entry["efforts"] = list(efforts) if isinstance(efforts, list) else efforts
    elif isinstance(efforts, list):
        entry["selector"] = (line["wire_model"] if client_pool else key) + suffix
        entry["efforts"] = list(efforts)
        if catalog.is_keyed_compat(provider):
            # Canonical ascending order (an equivalent authored
            # ordering yields the same entry, render and digest).
            entry["efforts"] = [level for level in catalog.KEYED_COMPAT_LEVELS if level in efforts] + \
                [level for level in efforts if level not in catalog.KEYED_COMPAT_LEVELS]
    else:
        entry["efforts"] = {
            level: {"selector": f"{key}-{level}{suffix}", "proxy_contract": contract}
            for level, contract in sorted(efforts.items())
        }
    source = line["context"]["source"]
    if migrated:
        qualification = (
            "operator-declared (custom.json, migrated); unverified; "
            f"validated floor {floor}; not benchmark-verified"
        )
    else:
        qualification = (
            f"operator-declared ({source}); validated floor {floor} "
            "(not near-limit measured); not benchmark-verified"
        )
    entry.update({
        "default_effort": line["default_effort"],
        "lead": {"effort": line["default_effort"], "env": {}},
        "capabilities": list(line.get("capabilities", ["lead"])),
        "roles": copy.deepcopy(line.get("roles", [])),
        "context": {
            "client_tokens": client,
            "provider_tokens": provider_tokens,
            "scalar_tokens": None,
            "ordinary_profile": f"custom-{declared}",
            "declared_tokens": declared,
            "validated_tokens": floor,
            "user_reported_tokens": provider_tokens,
            "qualification": qualification,
        },
        "routing_note": line.get("routing_note", f"Operator line ({file})."),
        "minimum_tested": {"claude_code": OPERATOR_MINIMUM_CLAUDE, "cliproxyapi": gateway_baseline},
        "status": "new",
        "registry_overlay": None,
    })
    return entry


def _resolve_provider_block(
    file_id: str, block: Mapping[str, Any], file: str, *, ports: frozenset[int], audited: bool = False,
) -> tuple[ResolvedProvider | None, list[OperatorProblem]]:
    problems: list[OperatorProblem] = []
    kind = block["kind"]
    path = "$.provider"
    keyed_kind = kind == KEYED_KIND
    if keyed_kind and not audited:
        # Defence in depth: validate_layer's metadata preflight refuses first.
        return None, [_problem("kind-gated", file, f"{path}.kind", catalog.KEYED_AUDIT_CLOSED, KEYED_GATE_REMEDY)]
    adapter, transport_kind = KINDS[kind]
    lan = kind == LAN_KIND
    auth = block["auth"]
    auth_kind = auth["kind"]
    secret_ref = auth.get("secret_ref")
    header = auth.get("header")
    if lan and auth_kind != "none":
        problems.append(_problem("auth", file, f"{path}.auth.kind", "the keyless LAN kind takes auth none"))
    if not lan and auth_kind == "none":
        problems.append(_problem("auth", file, f"{path}.auth.kind",
                                 "auth none is only for the keyless openai-compatible-lan kind"))
    if auth_kind == "none" and (secret_ref is not None or header is not None):
        problems.append(_problem("auth", file, f"{path}.auth", "auth none carries no secret_ref or header"))
    if auth_kind in ("bearer", "header") and secret_ref is None:
        problems.append(_problem("auth", file, f"{path}.auth.secret_ref", f"{auth_kind} auth needs secret_ref env:NAME"))
    if auth_kind == "header" and header != MIGRATION_HEADER:
        problems.append(_problem("auth", file, f"{path}.auth.header", 'only x-api-key is honoured for header auth', 'use kind "bearer"'))
    if auth_kind == "bearer" and header is not None:
        problems.append(_problem("auth", file, f"{path}.auth.header", "bearer auth takes no header"))
    if keyed_kind and auth_kind != "bearer":
        problems.append(_problem("auth", file, f"{path}.auth.kind",
                                 "the keyed openai-compatible kind takes bearer auth from one env:NAME secret"))
    secret_name = secret_ref.removeprefix("env:") if isinstance(secret_ref, str) else None
    if secret_name is not None:
        reason = secret_store.secret_name_problem(secret_name)
        if reason is None and secret_name in catalog.RESERVED_LEAD_ENV_KEYS:
            reason = f"secret name {secret_name} is compiler-owned"
        if reason:
            problems.append(_problem("secret-name", file, f"{path}.auth.secret_ref", reason,
                                     "store the key under another name: claude-multi providers set-key"))
    keyed = auth_kind != "none"
    try:
        origin, base_url = normalize_endpoint(block["base_url"], keyed=keyed, lan=lan, gateway_ports=ports)
    except ValueError as exc:
        problems.append(_problem("endpoint", file, f"{path}.base_url", f"base_url refused: {exc}"))
        origin = base_url = ""
    if keyed_kind and origin:
        platform = catalog.platform_host_problem(origin)
        if platform is not None:
            problems.append(_problem("endpoint", file, f"{path}.base_url", f"base_url refused: {platform}"))
    allowed_contracts = render.ADAPTER_PAYLOAD_CONTRACTS.get(adapter, {})
    contracts = list(block.get("payload_contracts", []))
    if keyed_kind and contracts:
        # No payload rules of any kind (value-free refusal).
        problems.append(_problem("contract", file, f"{path}.payload_contracts",
                                 "the keyed openai-compatible kind takes no payload contracts"))
        contracts = []
    for contract in contracts:
        if contract not in allowed_contracts:
            problems.append(_problem("contract", file, f"{path}.payload_contracts",
                                     f"contract {contract!r} is not a reviewed {kind} contract"))
    listing = block.get("listing")
    listing_origin: str | None = None
    if listing is not None:
        if kind == KEYED_KIND and listing["shape"] != "openai":
            problems.append(_problem("listing", file, f"{path}.listing.shape",
                                     "a keyed openai-compatible listing uses the openai shape"))
        if lan and listing["auth"] != "none":
            problems.append(_problem("listing", file, f"{path}.listing.auth", "a keyless LAN listing takes auth none"))
        try:
            listing_origin, _url = normalize_endpoint(listing["url"], keyed=listing["auth"] != "none", lan=lan, gateway_ports=ports)
        except ValueError as exc:
            problems.append(_problem("listing", file, f"{path}.listing.url", f"listing url refused: {exc}"))
        if listing["auth"] != "none" and listing_origin is not None and origin and listing_origin != origin:
            problems.append(_problem("listing-origin", file, f"{path}.listing.url",
                                     f'origin {listing_origin} ≠ base_url origin {origin}: the key is sent only to its own origin (auth "none" for a public listing)'))
    headers = dict(block.get("headers", {}))
    if keyed_kind:
        # Only an optional canonical X-Client (value-free reasons).
        headers, reasons = keyed_headers(headers)
        for reason in reasons:
            problems.append(_problem("headers", file, f"{path}.headers", reason))
    else:
        if headers and lan:
            problems.append(_problem("headers", file, f"{path}.headers", "the keyless LAN kind renders no static headers"))
        if len(headers) > MAX_HEADERS:
            problems.append(_problem("headers", file, f"{path}.headers", f"at most {MAX_HEADERS} static headers"))
        for name in sorted(headers):
            reason = header_problem(name, headers[name])
            if reason:
                problems.append(_problem("headers", file, f"{path}.headers.{name}", reason))
    if problems:
        return None, problems
    auth_entry: dict[str, Any] = {"kind": auth_kind}
    if secret_ref is not None:
        auth_entry["secret_ref"] = secret_ref
    if header is not None:
        auth_entry["header"] = header
    entry = {
        "display": block["display"],
        "independence_family": block["independence_family"],
        "support": "operator-declared",
        "support_note": f"Operator-declared provider ({file}); not a reviewed catalog provider.",
        "adapter": adapter,
        # A keyed route renders its normalized API base (scheme,
        # host and default port canonical, path kept, no trailing slash).
        "transport": {"kind": transport_kind, "base_url": base_url if keyed_kind else block["base_url"],
                      "auth": auth_entry},
        "passthrough_routes": [],
        "payload_contracts": contracts,
        "origin": "operator",
    }
    rd = route_digest(secret_ref=secret_ref, auth_kind=auth_kind, header=header, origin=origin,
                      base_url=base_url, listing_origin=listing_origin,
                      listing_auth=listing["auth"] if listing else None)
    return ResolvedProvider(file_id, file, entry, kind, origin, base_url, secret_name, auth_kind, header,
                            dict(listing) if listing else None, listing_origin, headers, rd), []


def t1_families(docs: Mapping[str, Any]) -> frozenset[str]:
    """The independence families the reviewed catalog declares: its
    providers' and the families its aggregator lines declare (``unknown``
    is never a declared family)."""

    found = {p["independence_family"] for p in docs["providers"]["providers"].values()
             if isinstance(p, Mapping) and isinstance(p.get("independence_family"), str)}
    models = docs.get("models")
    lines = models.get("models") if isinstance(models, Mapping) else None
    for entry in (lines or {}).values():
        family = entry.get("family") if isinstance(entry, Mapping) else None
        if isinstance(family, str) and not catalog.is_legacy_custom_entry(entry):
            found.add(family)
    return frozenset(family.strip().casefold() for family in found) - {"", UNKNOWN_FAMILY, "custom"}


def agent_route_kind(provider: Mapping[str, Any], kind: str | None = None, *,
                     gateway: Mapping[str, Any] | None = None) -> str | None:
    """Compatibility fact for supported generation routes, not an agent gate."""

    if provider.get("adapter") in RETENTION_AUDITED_ADAPTERS:
        return None
    if (provider.get("adapter") == KINDS[LAN_KIND][0]
            and provider.get("transport", {}).get("auth", {}).get("kind") == "none"):
        return None
    if catalog.is_keyed_compat(provider) and catalog.keyed_compat_problem(provider, gateway) is None:
        return None
    return kind or ("openai-compatible" if "openai" in str(provider.get("adapter")) else str(provider.get("adapter")))


def _line_semantics(
    key: str, line: Mapping[str, Any], file: str, provider_id: str, provider: Mapping[str, Any],
    *, catalog_provider: bool, agent_efforts: frozenset[str],
    role_ids: frozenset[str] | None = None, families: frozenset[str] = frozenset(),
    kind: str | None = None, gateway: Mapping[str, Any] | None = None,
) -> tuple[str | None, list[OperatorProblem]]:
    """Author-level line rules; returns (origin, problems)."""

    problems: list[OperatorProblem] = []
    path = f"$.lines.{key}"
    migration_fields = [name for name in ("selector", "legacy_key", "migrated_from") if name in line]
    migrated = bool(migration_fields)
    if migrated:
        if len(migration_fields) != 3:
            problems.append(_problem("migration", file, path,
                                     "selector, legacy_key and migrated_from are written together by migrate-custom"))
        else:
            legacy = line["legacy_key"]
            if not LEGACY_MODEL_ID.fullmatch(legacy) or len(legacy) > LEGACY_MODEL_ID_MAX:
                problems.append(_problem("migration", file, f"{path}.legacy_key",
                                         f"legacy id must match {LEGACY_MODEL_ID.pattern} (at most {LEGACY_MODEL_ID_MAX} characters)"))
            if key != f"custom-{legacy}" or line["selector"] != key:
                problems.append(_problem("migration", file, path,
                                         "a migrated line keeps the exact mapping custom-<legacy id> for key and selector"))
        if not MIGRATED_KEY.fullmatch(key):
            problems.append(_problem("key", file, path, f"invalid migrated key (must match {MIGRATED_KEY.pattern})"))
    elif not NEW_KEY.fullmatch(key):
        problems.append(_problem("key", file, path, 'operator keys start with "custom-"', f"rename to custom-{key.removeprefix('custom-')}"))
    roles = line.get("roles", [])
    if isinstance(roles, list) and role_ids is not None:
        unknown = sorted(set(roles) - set(role_ids))
        if unknown:
            problems.append(_problem("agents", file, f"{path}.roles",
                                     f"unknown agent role(s) {', '.join(unknown)}"))
    for block_name in ("context", "output"):
        block = line.get(block_name)
        if block is not None and block["source"] != "operator" and "source_ref" not in block:
            problems.append(_problem("provenance", file, f"{path}.{block_name}.source_ref",
                                     f"source {block['source']} needs a source_ref"))
    efforts = line["efforts"]
    levels = list(efforts) if isinstance(efforts, list) else sorted(efforts)
    if "ultracode" in levels or line["default_effort"] == "ultracode":
        problems.append(_problem("effort", file, f"{path}.efforts", "ultracode is a lead-only session effort, never a line effort"))
    for level in levels:
        if level not in agent_efforts and level != "ultracode":
            problems.append(_problem("effort", file, f"{path}.efforts", f"effort {level!r} is outside the native client efforts"))
    if isinstance(efforts, dict) and not efforts:
        problems.append(_problem("effort", file, f"{path}.efforts", "declare at least one effort"))
    if line["default_effort"] not in levels:
        problems.append(_problem("effort", file, f"{path}.default_effort", "the default effort is one of the declared efforts"))
    transport = provider["transport"]
    if transport["kind"] == "oauth-pool":
        if migrated:
            problems.append(_problem("pool", file, path, "a migrated legacy line never rides an OAuth pool"))
        if catalog.effort_mode(provider) == "client":
            if not isinstance(efforts, list):
                problems.append(_problem("pool", file, f"{path}.efforts",
                                         f"the {transport['pool']} pool is client-effort: declare an efforts list"))
            if not re.fullmatch(r"claude-[a-z0-9][a-z0-9.-]*", line["wire_model"]):
                problems.append(_problem("pool", file, f"{path}.wire_model",
                                         "an Anthropic pool line uses a canonical claude-* model id"))
        elif isinstance(efforts, list):
            problems.append(_problem("pool", file, f"{path}.efforts",
                                     f"the {transport['pool']} pool is gateway-effort: declare a {{level: contract}} map"))
    return ("operator-migrated" if migrated else "operator"), problems


def _schema_problems(document: Any, schema: Mapping[str, Any], file: str) -> tuple[list[OperatorProblem], dict[str, list[OperatorProblem]]]:
    """File-level vs per-line schema problems (smallest valid unit)."""

    file_level: list[OperatorProblem] = []
    per_line: dict[str, list[OperatorProblem]] = {}
    if not isinstance(document, dict):
        return [_problem("schema", file, "$", "top level must be an object")], per_line
    lines = document.get("lines")
    shell = {k: v for k, v in document.items() if k != "lines"}
    if "lines" in document:
        shell["lines"] = {} if isinstance(lines, dict) else lines
    for message in schema_validate.validate(shell, dict(schema), "$"):
        file_level.append(_problem("schema", file, message.split(":", 1)[0], message.split(":", 1)[-1].strip()))
    if isinstance(lines, dict):
        for key in sorted(lines):
            found = schema_validate.validate({"version": 1, "lines": {key: lines[key]}}, dict(schema), "$")
            if found:
                per_line[key] = [_problem("schema", file, m.split(":", 1)[0], m.split(":", 1)[-1].strip()) for m in found]
    for key, problems in per_line.items():
        line = lines[key]
        if not isinstance(line, dict):
            continue
        for i, problem in enumerate(problems):
            match = re.fullmatch(r"unexpected key '([^']+)'", problem.subject)
            if match and match[1] in {"validated_tokens", "provider_tokens", "client_tokens", "checks", "evidence", "agent_eligible"}:
                problems[i] = _problem("schema", file, problem.json_path + "." + match[1],
                                       "evidence is tool-owned", f"run claude-multi models qualify {key} --context N")
            elif problem.json_path.endswith(".context.declared_tokens"):
                value = (line.get("context") or {}).get("declared_tokens")
                shown = format(value, "g") if isinstance(value, (float, int)) and not isinstance(value, bool) else repr(value)
                problems[i] = dataclasses.replace(problem, subject=f"must be an integer 8192..2097152 (got {shown})")
    block = document.get("provider")
    if isinstance(block, dict):
        family = block.get("independence_family")
        if isinstance(family, str) and not family.isprintable():
            file_level.append(_problem("family", file, "$.provider.independence_family",
                                       "family must be nonempty, single-line printable text (at most 64 characters)"))
    if isinstance(lines, dict):
        for key, line in lines.items():
            family = line.get("family") if isinstance(line, dict) else None
            if isinstance(family, str) and not family.isprintable():
                per_line.setdefault(key, []).append(_problem(
                    "family", file, f"$.lines.{key}.family",
                    "family must be nonempty, single-line printable text (at most 64 characters)"))
    if isinstance(block, dict) and isinstance(block.get("auth"), dict) and block["auth"].get("kind") == "header":
        file_level = [dataclasses.replace(problem, subject="only x-api-key is honoured for header auth",
                                         remedy='use kind "bearer"')
                      if problem.json_path == "$.provider.auth.header" and block["auth"].get("header") != "x-api-key"
                      else problem for problem in file_level]
    if isinstance(block, dict) and block.get("kind") == "oauth-pool":
        file_level = [dataclasses.replace(problem, subject=(
            "openai/anthropic lines ship with claude-multi (pool admission is the catalog's)"),
            remedy="claude-multi models --candidates") if problem.json_path == "$.provider.kind" else problem
            for problem in file_level]
    return file_level, per_line


def validate_layer(
    docs: Mapping[str, Any],
    provider_files: Mapping[str, bytes],
    *,
    schemas: OperatorSchemas,
    marker: bool = False,
    ledger: OperatorLedger | None = None,
    legacy: Mapping[str, Any] | None = None,
    secret_values: frozenset[str] = frozenset(),
    read_problems: Iterable[OperatorProblem] = (),
    evidence: "OperatorEvidence | None" = None,
) -> OperatorLayer:
    """Resolve and cross-validate the whole operator layer (pure).

    ``evidence`` raises a line's validated floor from its
    current-definition context evidence only (accepted, retrieval correct,
    measured; capped at the declaration). The floor is not a definition
    input, so the digest never moves with it.

    ``docs`` are the trusted catalog docs (never mutated). ``provider_files``
    maps file id to raw bytes (:func:`read_providers_dir`). ``marker`` is the
    migrate-custom activation marker: without it, migrated (pending) lines
    are skipped by their own metadata and ``legacy`` (the custom.json
    registry) stays in force; with it, legacy is not consulted. Provider
    failures isolate to their file, line failures to their line; conflicting
    T2 declarations are refused together (no order precedence).
    """

    catalog_providers = docs["providers"]["providers"]
    catalog_lines = _docs_lines(docs)
    native = docs["native-contract"]
    agent_efforts = frozenset(native["agent_efforts"]["values"])
    roles_doc = docs.get("roles-v2") if "roles-v2" in docs else docs.get("roles")
    role_ids = frozenset((roles_doc or {}).get("roles", {})) if isinstance(roles_doc, Mapping) else None
    families = t1_families(docs)
    baseline = docs["gateway"]["gateway"]["cliproxyapi_baseline"]
    ports = gateway_ports(docs)
    legacy_doc = None if marker else (legacy or None)
    legacy_providers = dict(legacy_doc.get("providers", {})) if legacy_doc else {}
    legacy_models = dict(legacy_doc.get("models", {})) if legacy_doc else {}

    literals = _store_literals(secret_values)
    problems: dict[str, list[OperatorProblem]] = {}

    def add(problem: OperatorProblem) -> None:
        problem = _scrub(problem, literals)
        problems.setdefault(problem.file, []).append(problem)

    for problem in read_problems:
        add(problem)

    providers: dict[str, ResolvedProvider] = {}
    candidates: dict[str, ResolvedOperatorLine] = {}
    declared_in: dict[str, str] = {}  # line key -> the first file that declared it
    collided: set[str] = set()
    metadata: dict[str, dict[str, Any]] = {}
    secret_names: set[str] = set()
    pending: list[str] = []
    file_digests: dict[str, str] = {}

    for file_id in sorted(provider_files):
        raw = provider_files[file_id]
        file = file_label(file_id)
        file_digests[file_id] = strict_json.sha256_hex(raw)
        if _secret_bearing(file_id, literals):
            add(_problem("secret-literal", file, "", "the provider file name is a secret-like or stored credential literal",
                         "rename the file; reference credentials as env:NAME"))
            continue
        if not PROVIDER_ID.fullmatch(file_id):
            add(_problem("file-name", file, "", "provider file names match ^[a-z][a-z0-9-]*\\.json$"))
            continue
        try:
            document = strict_json.loads(raw, strict_json.JSONLimits(max_bytes=READ_LIMIT_BYTES, max_string=4096))
        except strict_json.StrictJSONError as exc:
            if _raw_secret_hit(raw, literals):
                add(_problem("secret-literal", file, "$", 'a secret value is not allowed (not strict JSON)',
                             f'store it with claude-multi providers set-key {file_id}; write "env:NAME"'))
            else:
                problem = _problem("json", file, "$", f"not strict JSON ({_json_failure(exc)})")
                if str(exc).startswith("duplicate key "):
                    key = ast.literal_eval(str(exc).removeprefix("duplicate key "))
                    problem = _problem("json", file, "$", f"duplicate key {json.dumps(key)}", "each line key appears once")
                elif isinstance(exc.__cause__, json.JSONDecodeError):
                    problem = dataclasses.replace(problem, line=exc.__cause__.lineno, column=exc.__cause__.colno)
                add(problem)
            continue
        # Conservative scrub set: every syntactically declared T2
        # secret name, whether the file is otherwise valid or not.
        auth = document.get("provider", {}).get("auth") if isinstance(document, dict) and isinstance(document.get("provider"), dict) else None
        ref = auth.get("secret_ref") if isinstance(auth, dict) else None
        if isinstance(ref, str) and re.fullmatch(r"env:[A-Z][A-Z0-9_]{0,63}", ref):
            secret_names.add(ref.removeprefix("env:"))
        # Screen the whole declaration (raw text, decoded keys and values)
        # before any author-derived diagnostic is emitted.
        hits = _secret_hits(document, "$", literals)
        if not hits and _raw_secret_hit(raw, literals):
            hits = ["$"]
        if hits:
            for hit in hits:
                add(_problem("secret-literal", file, hit, 'a secret value is not allowed',
                             f'store it with claude-multi providers set-key {file_id}; write "env:NAME"'))
            continue
        # The trusted audit gate, by metadata alone (a closed
        # keyed declaration is refused before schema or semantic work and
        # contributes nothing; its sanitized refusal echoes no authored text).
        gated = keyed_preflight(docs, file_id, raw)
        if gated is not None:
            add(gated)
            continue
        file_level, per_line = _schema_problems(document, schemas.provider, file)
        if file_level:
            for problem in file_level:
                add(problem)
            continue
        block = document.get("provider")
        resolved_provider: ResolvedProvider | None = None
        if file_id in catalog_providers:
            if block is not None:
                add(_problem("catalog-provider", file, "$.provider",
                             ('openai/anthropic lines ship with claude-multi (pool admission is the catalog\'s)'
                              if catalog_providers[file_id]["transport"]["kind"] == "oauth-pool" else
                              f'"{file_id}" is a catalog provider'),
                             ("claude-multi models --candidates" if catalog_providers[file_id]["transport"]["kind"] == "oauth-pool"
                              else 'remove the "provider" block; lines only')))
                continue
            provider = catalog_providers[file_id]
            catalog_provider = True
        else:
            if block is None:
                add(_problem("provider-missing", file, "$",
                             f"provider {file_id} is not in the catalog: declare its provider block"))
                continue
            if render.is_reserved_name(file_id):
                add(_problem("reserved", file, "", f"provider id {file_id} is reserved for the render sentinel"))
                continue
            if file_id in legacy_providers:
                if is_migration_document(document):
                    # A migrate-custom target published before the
                    # activation marker is pending by its own metadata (every
                    # line migrated from custom.json; a zero-model provider
                    # vacuously): never loaded beside its legacy source.
                    pending.extend(sorted(document["lines"]))
                    continue
                add(_problem("legacy-collision", file, "",
                             f"provider id {file_id} is a legacy custom provider (custom.json)",
                             "migrate it: claude-multi providers migrate-custom"))
                continue
            resolved_provider, provider_problems = _resolve_provider_block(
                file_id, block, file, ports=ports, audited=catalog.keyed_compat_audited(docs["gateway"]))
            if resolved_provider is None:
                for problem in provider_problems:
                    add(problem)
                continue
            providers[file_id] = resolved_provider
            provider = resolved_provider.entry
            catalog_provider = False
        merged_providers = {**catalog_providers, **({file_id: provider} if not catalog_provider else {})}
        for key in sorted(document["lines"]):
            if key in per_line:
                for problem in per_line[key]:
                    add(problem)
                continue
            line = document["lines"][key]
            origin, line_problems = _line_semantics(
                key, line, file, file_id, provider, catalog_provider=catalog_provider, agent_efforts=agent_efforts,
                role_ids=role_ids, families=families, gateway=docs["gateway"],
                kind=resolved_provider.kind if resolved_provider is not None else None,
            )
            if line_problems:
                for problem in line_problems:
                    add(problem)
                continue
            migrated = origin == "operator-migrated"
            if migrated and not marker:
                pending.append(key)  # Skipped by the file's own metadata
                continue
            core = derive_core_entry(key, line, file_id, provider, migrated=migrated,
                                     gateway_baseline=baseline, file=file)
            schema_found = schema_validate.validate(core, schemas.entry, f"$.lines.{key}")
            line_errors = [] if schema_found else catalog.validate_line(
                key, core, providers=merged_providers, native_contract=native,
                taken_selectors={}, origin=origin,
            )
            if schema_found or line_errors:
                for message in [*schema_found, *line_errors]:
                    add(_problem("line", file, f"$.lines.{key}", message))
                continue
            family = line.get("family") or (
                UNKNOWN_FAMILY if catalog_provider and file_id in AGGREGATOR_PROVIDERS
                else provider["independence_family"])
            mode = _selector_mode(provider, line["efforts"], migrated)
            output_tokens = line["output"]["declared_tokens"] if "output" in line else None
            fields = definition_fields(key, core, file_id, provider, family=family, selector_mode=mode,
                                       headers=resolved_provider.headers if resolved_provider else None,
                                       output_tokens=output_tokens)
            if key in declared_in:
                # The same key in two or more files: every declaration is
                # refused (a third one must not reinstate the key).
                add(_problem("collision", file, f"$.lines.{key}", f"line key {key} is declared in another file too"))
                first = declared_in[key]
                if key not in collided:
                    add(_problem("collision", first, f"$.lines.{key}", f"line key {key} is declared in {file} too"))
                    collided.add(key)
                candidates.pop(key, None)
                metadata.pop(key, None)
                continue
            declared_in[key] = file
            digest = definition_digest(fields)
            if evidence is not None:
                context = core["context"]
                floor, when = evidence_floor(evidence, key, digest=digest, declared=context["declared_tokens"],
                                             default_floor=context["validated_tokens"])
                if floor != context["validated_tokens"]:
                    context["validated_tokens"] = floor
                    context["qualification"] = (
                        f"operator-declared ({context['qualification'].split('(', 1)[1].split(')', 1)[0]}); "
                        f"validated {floor} by qualify {when} (accepted, retrieval correct); "
                        "not benchmark-verified")[:768]
            resolved_line = ResolvedOperatorLine(
                key=key, core_entry=core, family=family, source=file,
                definition_digest=digest, selector_mode=mode, origin=origin,
                provider_id=file_id, legacy_key=line.get("legacy_key"), output_tokens=output_tokens,
            )
            projection = overlay_projection(resolved_line, provider)
            if projection is not None:
                reason = overlay_problem(projection, core["wire_model"], display=core["display"])
                if reason:
                    add(_problem("overlay", file, f"$.lines.{key}", reason))
                    continue
            candidates[key] = resolved_line
            metadata[key] = {
                "origin": origin, "family": family, "source": file, "provider": file_id,
                "provider_origin": "catalog" if catalog_provider else "operator",
                "legacy_key": line.get("legacy_key"), "migrated_from": line.get("migrated_from"),
                "context_source": line["context"]["source"],
                "field_hashes": definition_field_hashes(fields),
            }

    # ---- One normalized origin per secret name (T1 > approved T2 > legacy)
    legacy_dropped: dict[str, str] = {}
    t1_origin: dict[str, tuple[str, str]] = {}
    for pid in sorted(catalog_providers):
        transport = catalog_providers[pid]["transport"]
        ref = (transport.get("auth") or {}).get("secret_ref")
        # A keyed T1 compat route is in one-secret/one-origin accounting.
        if (transport["kind"] == "direct" or catalog.is_keyed_compat(catalog_providers[pid])) and isinstance(ref, str):
            try:
                origin, _ = normalize_endpoint(transport["base_url"], keyed=True, lan=False, gateway_ports=())
            except ValueError:
                continue
            t1_origin.setdefault(ref.removeprefix("env:"), (origin, pid))
    # A reviewed transport alternative's credential is T1-owned too
    # (its origin is the fixed official endpoint), whether or not selected
    # and whether or not this build offers it: its name stays reserved.
    for alternative in TRANSPORT_ALTERNATIVES.values():
        if alternative.provider in catalog_providers:
            t1_origin.setdefault(alternative.secret_name, (alternative.origin, alternative.provider))
    route_status: dict[str, str] = {}
    route_changes: dict[str, tuple[str, str]] = {}
    for pid in sorted(providers):
        resolved = providers[pid]
        if resolved.auth_kind == "none":
            route_status[pid] = "keyless"
            continue
        granted = ledger.routes.get(pid) if ledger is not None else None
        if granted is None:
            route_status[pid] = "unapproved"
        elif granted["rd"] == resolved.route_digest:
            route_status[pid] = "approved"
        else:
            route_status[pid] = "changed"
            route_changes[pid] = (granted["origin"], resolved.origin)
    refused_providers: dict[str, str] = {}
    by_secret: dict[str, list[str]] = {}
    for pid in sorted(providers):
        name = providers[pid].secret_name
        if name is not None:
            by_secret.setdefault(name, []).append(pid)
    for name in sorted(by_secret):
        users = by_secret[name]
        if name in t1_origin:
            origin, owner = t1_origin[name]
            for pid in users:
                if providers[pid].origin != origin:
                    refused_providers[pid] = (f"secret env:{name} is bound to {origin} by catalog provider {owner}; "
                                              "one secret has one origin")
            users = [pid for pid in users if pid not in refused_providers]
        if len({providers[pid].origin for pid in users}) > 1:
            for pid in users:
                refused_providers[pid] = (f"secret env:{name} is declared for more than one origin "
                                          f"({', '.join(sorted(users))}); both refused")
    for lid in sorted(legacy_providers):
        spec = legacy_providers[lid]
        name = spec.get("secret_env")
        # The reserved-name and keep-list rules (not the stricter
        # new-name grammar, which gates migration only).
        reason = secret_store.secret_name_problem(name, grammar=False) if isinstance(name, str) else "no secret_env"
        if reason:
            legacy_dropped[lid] = f"legacy custom provider {lid}: {reason}"
            continue
        try:
            origin, _ = normalize_endpoint(spec.get("base_url"), keyed=True, lan=False, gateway_ports=ports)
        except ValueError as exc:
            legacy_dropped[lid] = f"legacy custom provider {lid}: base_url refused: {exc}"
            continue
        if name in t1_origin and t1_origin[name][0] != origin:
            legacy_dropped[lid] = (f"legacy custom provider {lid}: secret env:{name} belongs to catalog provider "
                                   f"{t1_origin[name][1]}; dropped")
            continue
        rivals = [pid for pid in by_secret.get(name, []) if pid not in refused_providers and providers[pid].origin != origin]
        if rivals:
            if any(route_status.get(pid) == "approved" for pid in rivals):
                legacy_dropped[lid] = (f"legacy custom provider {lid}: secret env:{name} belongs to the approved "
                                       f"route of {', '.join(rivals)}; dropped")
            else:
                for pid in rivals:
                    refused_providers[pid] = (f"secret env:{name} is used by legacy custom provider {lid} "
                                              "for another origin")
    for pid in sorted(refused_providers):
        resolved = providers.pop(pid)
        route_status.pop(pid, None)
        route_changes.pop(pid, None)
        add(_problem("secret-origin", resolved.file, "$.provider.auth.secret_ref", refused_providers[pid]))
        for key in [k for k, v in candidates.items() if v.provider_id == pid]:
            candidates.pop(key)
            metadata.pop(key, None)

    # ---- openai-compatibility provider keys collide after the
    # gateway's own normalization (the normalized render sentinel included);
    # both conflicting T2 owners are refused, a T1/sentinel owner wins.
    compat_owners: dict[str, list[str]] = {compat_provider_key(render.SENTINEL_NAME): ["render sentinel"]}
    for pid in sorted(catalog_providers):
        if catalog_providers[pid]["transport"]["kind"] == catalog.OPENAI_COMPAT_TRANSPORT:
            compat_owners.setdefault(compat_provider_key(pid), []).append(f"catalog provider {pid}")
    t2_compat: dict[str, list[str]] = {}
    for pid in sorted(providers):
        if providers[pid].entry["transport"]["kind"] == catalog.OPENAI_COMPAT_TRANSPORT:
            t2_compat.setdefault(compat_provider_key(pid), []).append(pid)
    key_refused: dict[str, str] = {}
    for normalized, pids in sorted(t2_compat.items()):
        if normalized in compat_owners:
            for pid in pids:
                key_refused[pid] = (f"provider {pid} normalizes to gateway provider key {normalized}, "
                                    f"owned by the {compat_owners[normalized][0]}")
        elif len(pids) > 1:
            for pid in pids:
                key_refused[pid] = (f"providers {', '.join(pids)} normalize to one gateway provider key "
                                    f"{normalized}; both refused")
    for pid in sorted(key_refused):
        resolved = providers.pop(pid)
        route_status.pop(pid, None)
        route_changes.pop(pid, None)
        add(_problem("collision", resolved.file, "$.provider", key_refused[pid]))
        for key in [k for k, v in candidates.items() if v.provider_id == pid]:
            candidates.pop(key)
            metadata.pop(key, None)

    # ---- selector / route / retired / sentinel collisions
    catalog_bases: dict[str, str] = {}
    for ckey in sorted(catalog_lines):
        for _level, selector, _contract in catalog.line_selectors(catalog_lines[ckey]):
            catalog_bases.setdefault(_selector_base(selector).lower(), f"catalog line {ckey}")
    for rkey, rentry in sorted(docs.get("retired", {"retired": {}})["retired"].items()):
        for selector in rentry["selectors"]:
            catalog_bases.setdefault(_selector_base(selector).lower(), f"retired key {rkey}")
    for pid in sorted(catalog_providers):
        for route in catalog_providers[pid]["passthrough_routes"]:
            catalog_bases.setdefault(route["name"].lower(), f"passthrough route of {pid}")
    legacy_live = {m: s for m, s in legacy_models.items() if s.get("provider") not in legacy_dropped}
    for mid in sorted(legacy_live):
        catalog_bases.setdefault(f"custom-{mid}".lower(), f"legacy custom model {mid}")
    catalog_routes: dict[tuple[str, str], list[str]] = {}
    for ckey in sorted(catalog_lines):
        entry = catalog_lines[ckey]
        catalog_routes.setdefault((entry["provider"], entry["wire_model"]), []).append(ckey)
    legacy_routes = {(s["provider"], s["wire_model"]): m for m, s in legacy_live.items()}
    retired_keys = set(docs.get("retired", {"retired": {}})["retired"])
    displaced: dict[str, ResolvedOperatorLine] = {}
    refused: dict[str, str] = {}
    t2_bases: dict[str, list[str]] = {}
    t2_routes: dict[tuple[str, str], list[str]] = {}
    captured = dict(ledger.aliases) if ledger is not None else {}
    for key in sorted(candidates):
        line = candidates[key]
        entry = line.core_entry
        route = (line.provider_id, entry["wire_model"])
        if route in catalog_routes:
            displaced[key] = line
            continue
        for _level, selector, _contract in catalog.line_selectors(entry):
            capture = captured.get(_selector_base(selector))
            # Another line cannot reuse a captured alias
            # with a different provider or wire (it stays served until pruned).
            if capture is not None and capture["source"] != f"operator:{key}" and (
                capture["provider"], capture["wire"]) != route:
                refused[key] = (f"selector {_selector_base(selector)} is still captured for "
                                f"{capture['source'].removeprefix('operator:')} ({capture['provider']}/"
                                f"{capture['wire']}); choose another key or prune the alias")
        if route in legacy_routes:
            refused[key] = f"(provider, wire) {route[0]}/{route[1]} is served by legacy custom model {legacy_routes[route]}"
            continue
        if key in refused:
            continue
        if key in retired_keys or key in catalog_lines:
            refused[key] = f"line key {key} is a catalog key"
            continue
        for _level, selector, _contract in catalog.line_selectors(entry):
            # Case-insensitive effective bases (the client suffix removed).
            base = _selector_base(selector).lower()
            if render.is_reserved_name(base):
                refused[key] = f"selector {base} uses the reserved render sentinel prefix"
            elif base in catalog_bases:
                refused[key] = f"selector {base} collides with {catalog_bases[base]}"
            t2_bases.setdefault(base, []).append(key)
        t2_routes.setdefault(route, []).append(key)
    for base, keys in sorted(t2_bases.items()):
        if len(set(keys)) > 1:
            for key in keys:
                refused.setdefault(key, f"selector {base} is declared by {', '.join(sorted(set(keys)))}; both refused")
    for route, keys in sorted(t2_routes.items()):
        if len(keys) > 1:
            for key in keys:
                refused.setdefault(key, f"(provider, wire) {route[0]}/{route[1]} is declared by {', '.join(keys)}; both refused")
    for key in sorted(refused):
        line = candidates.pop(key)
        metadata.pop(key, None)
        add(_problem("collision", line.source, f"$.lines.{key}", refused[key]))
    for key in sorted(displaced):
        candidates.pop(key, None)
        line = displaced[key]
        route = (line.provider_id, line.core_entry["wire_model"])
        add(_problem("displaced", line.source, f"$.lines.{key}",
                     f'{route[0]}/{route[1]} is the catalog line "{", ".join(catalog_routes[route])}"',
                     "admit or bind that line instead"))

    layer_digest = _digest("operator-layer", {
        "files": file_digests, "marker": marker,
        "ledger": ledger.sha256 if ledger is not None else None,
    })
    return OperatorLayer(
        providers=providers,
        lines=dict(sorted(candidates.items())),
        metadata=metadata,
        route_status=route_status,
        problems_by_file={name: tuple(found) for name, found in sorted(problems.items())},
        layer_digest=layer_digest,
        secret_names=frozenset(secret_names),
        displaced=displaced,
        pending=tuple(sorted(pending)),
        legacy_dropped=legacy_dropped,
        route_changes=route_changes,
        marker=marker,
    )


def resolve_provider_document(
    docs: Mapping[str, Any], file_id: str, raw: bytes, *, schemas: OperatorSchemas,
    marker: bool = True, ledger: OperatorLedger | None = None,
) -> OperatorLayer:
    """One provider file on its own (``providers validate FILE``; migration)."""

    return validate_layer(docs, {file_id: raw}, schemas=schemas, marker=marker, ledger=ledger)


# ------------------------------------------------------------ merged view
# Merged-docs keys carrying operator metadata (never catalog documents).
LINES_KEY = "operator-lines"
SECRET_NAMES_KEY = "operator-secret-names"


def merge_docs(
    docs: Mapping[str, Any], layer: OperatorLayer, *, legacy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Trusted docs + legacy custom (only without the marker, minus the
    providers dropped for secret-origin conflicts) + resolved operator providers and lines.

    Inputs are never mutated; ``models`` and ``models-v2`` stay one object;
    operator entries are the closed core projection (origin, family and
    migration metadata stay in ``layer.metadata``). No ordering precedence:
    conflicting T2 lines were already refused together by
    :func:`validate_layer`.
    """

    base: Mapping[str, Any] = docs
    if legacy and not layer.marker:
        filtered = {
            "version": legacy.get("version", 1),
            "providers": {k: v for k, v in legacy.get("providers", {}).items() if k not in layer.legacy_dropped},
            "models": {k: v for k, v in legacy.get("models", {}).items()
                       if v.get("provider") not in layer.legacy_dropped},
        }
        base = custom.merge_docs(dict(docs), filtered)
    merged = dict(base)
    # Capture recognition before operator providers or legacy entries are merged.
    # This metadata is not an operator-extensible family registry.
    merged["_operator_known_families"] = sorted(t1_families(docs))
    if layer.secret_names:
        # The conservative scrub set (every declared T2 secret name,
        # rendered or not) travels with the merged view to env_unset.
        merged[SECRET_NAMES_KEY] = sorted(layer.secret_names)
    if layer.lines:
        # Explicit origin metadata outside the closed core entry.
        merged[LINES_KEY] = {
            key: {"origin": line.origin, "legacy_key": line.legacy_key, "provider": line.provider_id,
                  "selector_mode": line.selector_mode, "family": line.family}
            for key, line in sorted(layer.lines.items())
        }
    if not layer.providers and not layer.lines:
        return merged
    if layer.providers:
        providers_doc = dict(base["providers"])
        providers_doc["providers"] = {
            **base["providers"]["providers"],
            **{pid: copy.deepcopy(p.entry) for pid, p in sorted(layer.providers.items())},
        }
        merged["providers"] = providers_doc
    if layer.lines:
        source = base["models-v2"] if "models-v2" in base else base["models"]
        lines_doc = {**source, "models": {
            **source["models"], **{key: copy.deepcopy(line.core_entry) for key, line in layer.lines.items()},
        }}
        merged["models"] = merged["models-v2"] = lines_doc
    return merged


# ------------------------------------------------------------ transport alternatives
# The reviewed transport alternatives for a catalog
# OAuth-pool provider. The ledger stores only the chosen identifier
# (``transport_choices``) and the approved credential route (``routes``);
# model identity, selectors, capabilities and catalog keys never change.
# The render receives the selection separately (a render-copy marker).
TRANSPORT_POOL = "oauth-pool"
TRANSPORT_API_KEY = "api-key"
TRANSPORT_CHOICES = (TRANSPORT_POOL, TRANSPORT_API_KEY)
# The gateway channel a keyed alternative renders into. The Claude channel
# serves the pool's own selectors as they are; the codex channel (the
# Responses executor) lists only the lines reviewed for that key route
# (``catalog.key_route``) and never an empty model list.
CHANNEL_CLAUDE_KEY = render.CLAUDE_KEY_SECTION
CHANNEL_CODEX_KEY = render.CODEX_KEY_SECTION


@dataclass(frozen=True)
class TransportAlternative:
    """One reviewed alternative transport of a catalog OAuth-pool provider."""

    provider: str  # catalog provider id (anthropic, openai)
    choice: str  # the identifier the ledger stores
    pool: str  # the OAuth channel the alternative replaces
    base_url: str  # fixed official endpoint (never authored)
    secret_name: str  # logical env:NAME in the secret store
    header: str | None  # x-api-key header auth, or None for bearer
    channel: str  # the gateway channel it renders into (CHANNEL_CLAUDE_KEY or CHANNEL_CODEX_KEY)
    available: bool
    closed_reason: str | None = None

    @property
    def explicit_models(self) -> bool:
        """Only lines reviewed for this key route are served on it."""

        return self.channel == CHANNEL_CODEX_KEY

    @property
    def secret_ref(self) -> str:
        return f"env:{self.secret_name}"

    @property
    def auth_kind(self) -> str:
        return "header" if self.header else "bearer"

    @property
    def origin(self) -> str:
        parts = urllib.parse.urlsplit(self.base_url)
        return f"{parts.scheme}://{parts.hostname}"

    @property
    def route_digest(self) -> str:
        return route_digest(secret_ref=self.secret_ref, auth_kind=self.auth_kind, header=self.header,
                            origin=self.origin, base_url=self.base_url, listing_origin=None, listing_auth=None)


TRANSPORT_ALTERNATIVES: Mapping[tuple[str, str], TransportAlternative] = {
    ("anthropic", TRANSPORT_API_KEY): TransportAlternative(
        provider="anthropic", choice=TRANSPORT_API_KEY, pool="claude",
        base_url="https://api.anthropic.com",
        # ANTHROPIC_*/CLAUDE_* are reserved secret prefixes (secret_store).
        secret_name="PLATFORM_ANTHROPIC_API_KEY", header="x-api-key", channel=CHANNEL_CLAUDE_KEY,
        available=True,
    ),
    # The OpenAI API (Responses): the codex channel appends /responses to its
    # base, so the official base carries /v1. Bearer auth; never the account
    # backend (an empty base would select it). Only reviewed lines are listed.
    # The gateway sends this key as a plain API client (codex cloaking off: no
    # codex client Version or session header) and turns provider failures into
    # fixed local text, so an error that quotes the key never reaches a client.
    ("openai", TRANSPORT_API_KEY): TransportAlternative(
        provider="openai", choice=TRANSPORT_API_KEY, pool="codex",
        base_url="https://api.openai.com/v1", secret_name="PLATFORM_OPENAI_API_KEY", header=None,
        channel=CHANNEL_CODEX_KEY, available=True,
    ),
}


def transport_alternative(provider_id: str, choice: str) -> TransportAlternative | None:
    return TRANSPORT_ALTERNATIVES.get((provider_id, choice))


def key_route_lines(docs: Mapping[str, Any], provider_id: str) -> tuple[str, ...]:
    """The catalog lines of ``provider_id`` reviewed for its API-key route
    (``catalog.key_route``), sorted. Operator and custom lines never carry
    one, so they are never served on such a route."""

    return tuple(sorted(key for key, entry in _docs_lines(docs).items()
                        if entry.get("provider") == provider_id and catalog.key_route(entry) is not None))


def key_route_selectors(docs: Mapping[str, Any], provider_id: str) -> frozenset[str]:
    """The selector bases a reviewed API-key route of ``provider_id`` serves:
    each reviewed line's selectors at its reviewed efforts."""

    found: set[str] = set()
    lines = _docs_lines(docs)
    for key in key_route_lines(docs, provider_id):
        entry = lines[key]
        reviewed = set(catalog.key_route_levels(entry))
        found.update(_selector_base(selector) for level, selector, _contract in catalog.line_selectors(dict(entry))
                     if level in reviewed)
    return frozenset(found)


def transport_problem(docs: Mapping[str, Any], provider_id: str, choice: str) -> str | None:
    """Why ``choice`` cannot be selected for ``provider_id`` (None: it can)."""

    provider = docs["providers"]["providers"].get(provider_id)
    if provider is None:
        return f"{provider_id} is not a catalog provider"
    if provider["transport"]["kind"] != TRANSPORT_POOL:
        return f"{provider_id} has no reviewed transport alternatives (it is not an OAuth-pool provider)"
    if choice not in TRANSPORT_CHOICES:
        return f"unknown transport {choice!r} (choose {' or '.join(TRANSPORT_CHOICES)})"
    if choice == TRANSPORT_POOL:
        return None
    alternative = transport_alternative(provider_id, choice)
    if alternative is None:
        return f"{provider_id} has no reviewed {choice} transport"
    if not alternative.available:
        return alternative.closed_reason
    if alternative.explicit_models and not key_route_lines(docs, provider_id):
        # Never select a key route that would serve nothing (an empty model
        # list is never rendered, and the pool stays excluded while selected).
        return (f"no {provider.get('display') or provider_id} model is reviewed for its {choice} transport "
                "yet; the account route stays in use")
    return None


def transport_route_record(alternative: TransportAlternative, *, at: str) -> dict[str, Any]:
    """The ledger route approval of a selected transport alternative."""

    return {
        "rd": alternative.route_digest,
        "secret_ref": alternative.secret_ref,
        "auth": alternative.auth_kind,
        "header": alternative.header,
        "origin": alternative.origin,
        "listing_origin": None,
        "approved_at": at,
    }


@dataclass(frozen=True)
class TransportSelection:
    """The transport a catalog pool provider renders with (pure view)."""

    provider: str
    choice: str
    alternative: TransportAlternative | None
    approved: bool  # the ledger route record matches the reviewed descriptor

    @property
    def problem(self) -> str | None:
        if self.alternative is None:
            return f"transport {self.choice} of {self.provider} is not a reviewed alternative"
        if not self.alternative.available:
            return self.alternative.closed_reason
        if not self.approved:
            return (f"the {self.choice} transport of {self.provider} is not approved for the current "
                    f"reviewed descriptor: claude-multi providers transport {self.provider} {self.choice}")
        return None


def transport_selections(docs: Mapping[str, Any], ledger: OperatorLedger | None) -> dict[str, TransportSelection]:
    """Non-default transport choices recorded in the ledger, by provider."""

    if ledger is None:
        return {}
    providers = docs["providers"]["providers"]
    found: dict[str, TransportSelection] = {}
    for pid, choice in sorted(ledger.transport_choices.items()):
        if choice == TRANSPORT_POOL or pid not in providers:
            continue
        alternative = transport_alternative(pid, choice)
        route = ledger.routes.get(pid)
        approved = bool(alternative is not None and alternative.available and isinstance(route, Mapping)
                        and route.get("rd") == alternative.route_digest)
        found[pid] = TransportSelection(pid, choice, alternative, approved)
    return found


def apply_transports(docs: Mapping[str, Any], selections: Mapping[str, TransportSelection]) -> dict[str, Any]:
    """The render copy of ``docs`` with each selected alternative applied.

    A selected alternative always replaces the pool (never an automatic
    fallback to the OAuth credential): an unapproved or closed choice keeps
    the keyed direct shape marked ``withheld``, so the render treats it as
    unavailable (its key is never resolved) until the operator re-approves
    or switches back.
    """

    if not selections:
        return dict(docs)
    merged = dict(docs)
    providers_doc = dict(docs["providers"])
    providers = dict(providers_doc["providers"])
    for pid, selection in sorted(selections.items()):
        alternative = selection.alternative
        if alternative is None or pid not in providers:
            continue
        entry = copy.deepcopy(providers[pid])
        auth: dict[str, Any] = {"kind": alternative.auth_kind, "secret_ref": alternative.secret_ref}
        if alternative.header:
            auth["header"] = alternative.header
        entry["transport"] = {"kind": "direct", "base_url": alternative.base_url, "auth": auth}
        entry[render.TRANSPORT_ALTERNATIVE_KEY] = {"id": selection.choice, "pool": alternative.pool,
                                                   "channel": alternative.channel,
                                                   "withheld": selection.problem is not None}
        providers[pid] = entry
    providers_doc["providers"] = providers
    merged["providers"] = providers_doc
    return merged


# ------------------------------------------------------------ presets and samples
# Reviewed author-format documents shipped through the asset seam. They are
# templates, never grants: installing one writes an inert declaration (route
# approval and admission stay separate), and nothing ever installs a preset
# or sample into providers.d on import or startup. A preset also carries its
# review in a ``preset`` block (``schemas/preset.schema.json``): the support
# label, which is ``preset`` and never more without evidence, and the vendor
# documentation its endpoint, key and model list were read from (or
# ``generic`` for the template of a server on your network). The block never
# reaches a declaration: :func:`preset_document` drops it.
PRESETS_DIRNAME = "presets"
SAMPLES_DIRNAME = catalog.EXAMPLES_PROVIDERS_DIR
PRESET_BLOCK = "preset"
PRESET_SCHEMA = "preset.schema.json"
PRESET_SUPPORT = "preset"
PRESET_GENERIC = "generic"


@dataclass(frozen=True)
class PresetInfo:
    """What a provider picker shows of one reviewed preset (names only)."""

    name: str
    display: str
    kind: str
    base_url: str
    secret_name: str | None  # the key's name; None for a keyless server
    listing_url: str | None
    family: str
    support: str
    source: str  # docs | generic
    source_ref: str | None
    checked: str | None

    @property
    def keyed(self) -> bool:
        return self.secret_name is not None

    @property
    def generic(self) -> bool:
        return self.source == PRESET_GENERIC


def load_preset_schema(asset_root: Path | str | None = None) -> dict[str, Any]:
    """The schema of a preset's review block (assets seam)."""

    document = strict_json.load(assets.root(asset_root) / "schemas" / PRESET_SCHEMA)
    schema_validate.check_schema(document)
    return document


def preset_info(name: str, raw: bytes, *, schema: Mapping[str, Any]) -> PresetInfo:
    """The review and the provider facts of preset ``name``. A preset
    without a valid review block or provider block raises
    :class:`OperatorError` (the declaration itself is validated when it is
    added, like a hand-written file)."""

    try:
        document = strict_json.loads(raw)
    except strict_json.StrictJSONError as exc:
        raise OperatorError(f"preset {name} is not valid JSON ({_json_failure(exc)})") from None
    if not isinstance(document, dict):
        raise OperatorError(f"preset {name}: top-level document must be an object")
    review = document.get(PRESET_BLOCK)
    if review is None:
        raise OperatorError(f"preset {name}: no {PRESET_BLOCK} block (its support label and source)")
    problems = schema_validate.validate(review, dict(schema), f"$.{PRESET_BLOCK}")
    if problems:
        raise OperatorError(f"preset {name}: {problems[0]}")
    block = document.get("provider")
    if not isinstance(block, Mapping):
        raise OperatorError(f"preset {name}: a preset declares a provider block")
    auth = block.get("auth")
    secret_ref = auth.get("secret_ref") if isinstance(auth, Mapping) else None
    listing = block.get("listing")
    listing_url = listing.get("url") if isinstance(listing, Mapping) else None
    return PresetInfo(
        name=name, display=str(block.get("display") or name), kind=str(block.get("kind") or ""),
        base_url=str(block.get("base_url") or ""),
        secret_name=secret_ref.removeprefix("env:") if isinstance(secret_ref, str) else None,
        listing_url=listing_url if isinstance(listing_url, str) else None,
        family=str(block.get("independence_family") or UNKNOWN_FAMILY), support=review["support"],
        source=review["source"], source_ref=review.get("source_ref"), checked=review.get("checked"))


def presets(asset_root: Path | str | None = None) -> dict[str, PresetInfo]:
    """Every reviewed preset that reads, by name. One that does not read is
    left out of the list (the shipped presets are checked by the test
    suite); ``providers add --preset NAME`` still names its problem."""

    try:
        schema = load_preset_schema(asset_root)
    except (OSError, strict_json.StrictJSONError, schema_validate.SchemaError):
        return {}
    found: dict[str, PresetInfo] = {}
    for name in sorted(preset_paths(asset_root)):
        try:
            found[name] = preset_info(name, load_preset(name, asset_root), schema=schema)
        except OperatorError:
            continue
    return found


def _documents(directories: Iterable[Path]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for directory in directories:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if FILE_NAME.fullmatch(name):
                found.setdefault(name[: -len(".json")], directory / name)
    return found


def preset_paths(asset_root: Path | str | None = None) -> dict[str, Path]:
    """Preset name -> file: the reviewed ``presets/<name>.json`` only (what
    the provider picker offers)."""

    root = Path(asset_root) if asset_root is not None else assets.root()
    return _documents([root / PRESETS_DIRNAME])


def sample_paths(asset_root: Path | str | None = None) -> dict[str, Path]:
    """Presets plus the examples in ``examples/providers.d/`` (``providers add
    --preset NAME`` accepts both; examples are never offered as presets)."""

    root = Path(asset_root) if asset_root is not None else assets.root()
    return _documents([root / PRESETS_DIRNAME, root / SAMPLES_DIRNAME])


def load_preset(name: str, asset_root: Path | str | None = None) -> bytes:
    """The reviewed bytes of preset or example ``name`` (never an operator file)."""

    paths_by_name = sample_paths(asset_root)
    path = paths_by_name.get(name)
    if path is None:
        known = ", ".join(sorted(paths_by_name)) or "none"
        raise OperatorError(f"unknown preset {name!r} (reviewed presets: {known})")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OperatorError(f"preset {name}: unreadable ({exc.strerror or type(exc).__name__})") from exc
    if len(raw) > READ_LIMIT_BYTES:
        raise OperatorError(f"preset {name}: larger than {READ_LIMIT_BYTES} bytes")
    return raw


def preset_document(raw: bytes, *, base_url: str | None = None) -> dict[str, Any]:
    """A preset as a new declaration, without its review block; ``base_url``
    retargets its endpoint (and a listing URL under the same base).
    Validation happens after, on the whole proposed layer, exactly as for a
    hand-written file."""

    try:
        document = strict_json.loads(raw)
    except strict_json.StrictJSONError as exc:
        raise OperatorError(f"preset is not valid JSON ({_json_failure(exc)})") from None
    if not isinstance(document, dict):
        raise OperatorError("preset: top-level document must be an object")
    document = copy.deepcopy(document)
    document.pop(PRESET_BLOCK, None)
    if base_url is not None:
        block = document.get("provider")
        if not isinstance(block, dict):
            raise OperatorError("--base-url applies to a preset with a provider block only")
        old = str(block.get("base_url", "")).rstrip("/")
        block["base_url"] = base_url
        listing = block.get("listing")
        if isinstance(listing, dict) and isinstance(listing.get("url"), str) and old \
                and listing["url"].startswith(old):
            listing["url"] = base_url.rstrip("/") + listing["url"][len(old):]
    return document


def read_secret_file(path: Path | str) -> str:
    """``--secret-file``: a private (owner-only, non-symlink) one-line file.

    The value is returned for one store write and never echoed; the file is
    never referenced persistently (no ``file:`` secret references)."""

    try:
        raw = state.read_private(Path(path))
    except (state.StateError, OSError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        raise OperatorError(f"--secret-file refused: {reason} (it must be a private 0600 file you own)") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise OperatorError("--secret-file refused: not UTF-8 text") from None
    value = text.strip()
    if not value or "\n" in value or "\r" in value or _CONTROL.search(value):
        raise OperatorError("--secret-file refused: it must hold exactly one non-empty line")
    return value


# ------------------------------------------------------------ snapshot, render plan, captures
LEDGER_UNUSABLE = "operator-ledger.json is unreadable or invalid (never repaired or overwritten automatically)"
_RENDERABLE = frozenset({"approved", "keyless"})
# Problem codes that never block an explicit render: an ignored file name and
# a line the catalog now serves (it yields by design).
NONBLOCKING_CODES = frozenset({"file-name", "displaced"})


def empty_layer(*, marker: bool = False) -> OperatorLayer:
    return OperatorLayer({}, {}, {}, {}, {}, _digest("operator-layer", {"files": {}, "marker": marker, "ledger": None}),
                         frozenset(), marker=marker)


@dataclass(frozen=True)
class OperatorSnapshot:
    """One read of the operator inputs (providers.d, ledger) for a consumer.

    ``ledger_error`` is set (and ``ledger`` None) when the ledger exists but
    is unusable: fail closed, never inferred, never overwritten.
    """

    layer: OperatorLayer
    ledger: OperatorLedger | None
    ledger_error: str | None
    ledger_present: bool
    read: ProvidersRead | None
    schemas: OperatorSchemas | None


def load_snapshot(
    environ: Mapping[str, str], docs: Mapping[str, Any], *,
    asset_root: Path | str | None = None, legacy: Mapping[str, Any] | None = None,
    secret_values: Callable[[], frozenset[str]] | None = None,
) -> OperatorSnapshot:
    """Read providers.d and the ledger (owned-readable, no writes) and resolve
    the layer. Absent inputs and an empty legacy registry short-circuit to
    the empty layer (no schema load, no file created)."""

    read = read_providers_dir(providers_dir(environ))
    ledger_file = ledger_path(environ)
    ledger_present = os.path.lexists(ledger_file)
    has_legacy = bool(legacy and (legacy.get("providers") or legacy.get("models")))
    if not read.files and not read.problems and not ledger_present and not has_legacy:
        return OperatorSnapshot(empty_layer(marker=read.marker), None, None, False, read, None)
    schemas = load_schemas(asset_root)
    ledger: OperatorLedger | None = None
    ledger_error: str | None = None
    if ledger_present:
        try:
            ledger = load_ledger(environ, schemas)
        except OperatorError:
            ledger_error = LEDGER_UNUSABLE
    # The defensive stored-literal scan runs only for a layer with
    # an independently valid contribution; a solely rejected closed-keyed
    # declaration never reaches get, is_set or scan_values.
    values = _scan_values(secret_values, docs, read.files)
    evidence: OperatorEvidence | None = None
    if read.files:
        try:
            evidence = load_evidence(environ, schemas)
        except OperatorError:
            evidence = None  # doctor reports it; the floor stays the default
    layer = validate_layer(docs, read.files, schemas=schemas, marker=read.marker, ledger=ledger,
                           legacy=legacy, secret_values=values, read_problems=read.problems, evidence=evidence)
    return OperatorSnapshot(layer, ledger, ledger_error, ledger_present, read, schemas)


def without_contributions(layer: OperatorLayer) -> OperatorLayer:
    """The layer minus every T2 provider and line (legacy secret-origin conflict drops kept):
    the start-policy fallback when captures cannot be committed."""

    return dataclasses.replace(layer, providers={}, lines={}, route_status={}, route_changes={})


def blocking_problems(layer: OperatorLayer) -> tuple[OperatorProblem, ...]:
    """Problems that make an explicit render refuse the candidate."""

    return tuple(problem for problem in layer.problems if problem.code not in NONBLOCKING_CODES)


def renderable_layer(layer: OperatorLayer) -> OperatorLayer:
    """The layer the gateway render uses: providers on an
    approved or keyless route and their lines, plus lines-only lines on
    catalog providers. Admission and provider-enabled never filter here
    (New·Off aliases are served before admission)."""

    providers = {pid: p for pid, p in layer.providers.items() if layer.route_status.get(pid) in _RENDERABLE}
    lines = {key: line for key, line in layer.lines.items()
             if line.provider_id in providers or line.provider_id not in layer.providers}
    return dataclasses.replace(layer, providers=providers, lines=lines)


def _catalog_line_levels(entry: Mapping[str, Any]) -> tuple[str, ...]:
    efforts = entry.get("efforts")
    declared = list(efforts) if isinstance(efforts, list) else sorted(efforts or {})
    return tuple(level for level in catalog.OVERLAY_THINKING_LEVELS if level in declared)


@dataclass(frozen=True)
class RenderPlan:
    """Everything the gateway render needs from the operator layer."""

    docs: dict[str, Any]
    captures: dict[str, dict[str, Any]]
    headers: dict[str, dict[str, str]]
    overlay: tuple[render.OverlaySource, ...]
    layer: OperatorLayer  # the rendered (route-filtered) layer
    route_digests: dict[str, str]  # provider -> current route digest (rendered providers)
    not_rendered: dict[str, str]  # provider id -> reason (unapproved/changed route)
    transports: dict[str, TransportSelection] = field(default_factory=dict)  # Transport selections
    quiet_providers: frozenset[str] = frozenset()  # Externalized providers (no notice)


def render_plan(
    docs: Mapping[str, Any], layer: OperatorLayer, ledger: OperatorLedger | None, *,
    legacy: Mapping[str, Any] | None = None,
) -> RenderPlan:
    """Pure: trusted docs + validated layer + ledger -> render inputs.

    Captures whose alias no current definition emits stay served while
    their provider renders on the same route identity (a
    provider or route change never retargets a retained alias). A current
    definition wins only the aliases it actually emits: an edited line
    (new wire, fewer efforts) keeps its older, non-colliding captures.
    Overlay registrations are listed per source;
    :func:`render.plan_oauth_overlay` aggregates them (a current wire
    outranks a retained one).
    """

    shown = renderable_layer(layer)
    transports = transport_selections(docs, ledger)
    merged = apply_transports(merge_docs(docs, shown, legacy=legacy), transports)
    catalog_providers = docs["providers"]["providers"]
    route_digests: dict[str, str] = {pid: catalog_route_digest(p) for pid, p in catalog_providers.items()}
    for pid, selection in transports.items():
        if selection.alternative is not None:
            # A capture bound to the pool route never retargets onto the key.
            route_digests[pid] = selection.alternative.route_digest
    route_digests.update({pid: p.route_digest for pid, p in shown.providers.items()})
    not_rendered = {pid: layer.route_status.get(pid, "unapproved")
                    for pid in sorted(set(layer.providers) - set(shown.providers))}
    current_bases = {base for line in shown.lines.values() for base in line_aliases(line.core_entry)}
    captures: dict[str, dict[str, Any]] = {}
    for alias, capture in sorted((ledger.aliases if ledger is not None else {}).items()):
        if alias in current_bases:
            continue  # a current definition emitting this alias wins over the capture
        pid = capture["provider"]
        if route_digests.get(pid) != capture["rd"]:
            continue  # provider gone or route identity changed: never retarget
        captures[alias] = {"provider": pid, "wire": capture["wire"], "proxy_contract": capture["proxy_contract"],
                           "display": capture["display"], "context_tokens": capture["context_tokens"]}
        if "thinking_levels" in capture:
            # The alias-level keyed levels (never overlay.thinking_levels).
            captures[alias]["thinking_levels"] = list(capture["thinking_levels"])
    headers = {pid: dict(p.headers) for pid, p in shown.providers.items() if p.headers}
    sources: list[render.OverlaySource] = []
    lines = _docs_lines(docs)
    for key in sorted(lines):
        entry = lines[key]
        marker = entry.get("registry_overlay")
        if not isinstance(marker, Mapping):
            continue
        channel = marker["channel"]
        sources.append(render.OverlaySource(
            tier=render.TIER_CURRENT_T1, channel=channel, name=entry["wire_model"], display=entry["display"],
            max_context_length=entry["context"]["provider_tokens"],
            max_completion_tokens=entry.get("output", {}).get("declared_tokens"),
            thinking_levels=_catalog_line_levels(entry),
            origin=f"catalog line {key}",
        ))
    for key in sorted(shown.lines):
        line = shown.lines[key]
        provider = catalog_providers.get(line.provider_id)
        projection = overlay_projection(line, provider) if provider is not None else None
        if projection is None:
            continue
        sources.append(render.OverlaySource(
            tier=render.TIER_CURRENT_T2, channel=projection["channel"], name=line.core_entry["wire_model"],
            display=line.core_entry["display"], max_context_length=projection["max_context_length"],
            max_completion_tokens=projection.get("max_completion_tokens"),
            thinking_levels=tuple(projection.get("thinking_levels", ())), origin=f"operator line {key}",
        ))
    for alias in sorted(captures):
        overlay = ledger.aliases[alias]["overlay"] if ledger is not None else None
        if overlay is None:
            continue
        sources.append(render.OverlaySource(
            tier=render.TIER_RETAINED, rank=1, channel=overlay["channel"], name=captures[alias]["wire"],
            display=captures[alias]["display"], max_context_length=overlay["max_context_length"],
            max_completion_tokens=overlay.get("max_completion_tokens"),
            thinking_levels=tuple(overlay.get("thinking_levels", ())), origin=f"captured alias {alias}", alias=alias,
        ))
    retired = docs.get("retired", {"retired": {}})["retired"]
    for rkey in sorted(retired):
        rentry = retired[rkey]
        marker = rentry.get("registry_overlay")
        if not isinstance(marker, Mapping):
            continue
        # The retired marker carries no efforts: no thinking is reconstructed;
        # limits come from its own fields.
        for selector in sorted(rentry["selectors"]):
            sources.append(render.OverlaySource(
                tier=render.TIER_RETAINED, rank=0, channel=marker["channel"], name=rentry["last_wire"],
                display=rentry["display"], max_context_length=rentry["context_tokens"],
                max_completion_tokens=rentry.get("output", {}).get("declared_tokens"),
                origin=f"retired key {rkey}", alias=_selector_base(selector),
            ))
    quiet = frozenset(catalog.externalized_providers(merged))
    return RenderPlan(merged, captures, headers, tuple(sources), shown, route_digests, not_rendered,
                      transports, quiet)


def line_aliases(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """The selector bases (served alias names) a core line entry emits."""

    return tuple(_selector_base(selector) for _level, selector, _contract in catalog.line_selectors(dict(entry)))


def emitted_operator_aliases(plan: RenderPlan, emitted: Iterable[str]) -> frozenset[str]:
    """The T2 aliases of the rendered operator lines a candidate actually
    serves (after credential filtering): exactly what a render must capture."""

    served = set(emitted)
    catalog_providers = plan.docs["providers"]["providers"]
    return frozenset(
        base for line in plan.layer.lines.values() if line.provider_id in catalog_providers
        for base in line_aliases(line.core_entry) if base in served
    )


def declared_aliases(layer: OperatorLayer, read: ProvidersRead | None) -> dict[str, str]:
    """Every alias name providers.d declares, rendered or not: alias ->
    providers.d file label. Pure, never raises.

    Validated lines contribute their exact selector bases; a line of a file
    with problems contributes the names it could emit (its key, its
    key-level selectors and its wire, the client-pool selector). A file
    whose name or content was withheld as secret-bearing contributes
    nothing (no name derived from it is ever shown). The reload policy
    checks live references against these, so an omitted declaration never
    passes as "never served" when capture history is absent.
    """

    names: dict[str, str] = {}
    for key in sorted(layer.lines):
        line = layer.lines[key]
        for base in line_aliases(line.core_entry):
            names.setdefault(base, line.source)
    for file_id in sorted(read.files if read is not None else {}):
        found = layer.problems_by_file.get(file_label(file_id))
        if not found or any(problem.code == "secret-literal" for problem in found):
            continue
        try:
            document = strict_json.loads(read.files[file_id],
                                         strict_json.JSONLimits(max_bytes=READ_LIMIT_BYTES, max_string=4096))
        except (strict_json.StrictJSONError, ValueError):
            continue
        lines = document.get("lines") if isinstance(document, dict) else None
        if not isinstance(lines, dict):
            continue
        for key, line in sorted(lines.items()):
            if key in layer.lines:
                continue
            candidates = {key}
            if isinstance(line, dict):
                efforts = line.get("efforts")
                if isinstance(efforts, dict):
                    candidates.update(f"{key}-{level}" for level in efforts if isinstance(level, str))
                if isinstance(line.get("wire_model"), str):
                    candidates.add(line["wire_model"])
            for name in sorted(candidates):
                names.setdefault(name, file_label(file_id))
    return names


def static_overlay_wires(registry: "catalog.PinnedRegistry | None") -> dict[str, frozenset[str]] | None:
    """Lower-case ids the pinned static registry serves per overlay channel:
    every registry section the channel's account pool serves from (codex:
    every plan section, the gateway's own static-wins scope)."""

    from claude_multi import account_pools

    if registry is None:
        return None
    found: dict[str, frozenset[str]] = {}
    for channel in catalog.OVERLAY_CHANNELS:
        pool = account_pools.pool(channel)
        sections = pool.registry_sections if pool is not None else ()
        found[channel] = frozenset(wire.lower() for section in sections
                                   for wire in registry.sections.get(section, frozenset()))
    return found


def plan_alias_capture(
    ledger: OperatorLedger | None, plan: RenderPlan, emitted: Iterable[str], *, catalog_version: int,
) -> dict[str, Any] | None:
    """The ledger document with every emitted T2 alias captured, or None
    when nothing changes. Current definitions overwrite their own
    capture; a tombstone of a re-emitted alias is cleared. Pure."""

    served = set(emitted)
    document = ledger_document(ledger)
    aliases = document["aliases"]
    changed = False
    catalog_providers = plan.docs["providers"]["providers"]
    captured_now = emitted_operator_aliases(plan, served)
    for key in sorted(plan.layer.lines):
        line = plan.layer.lines[key]
        provider = catalog_providers.get(line.provider_id)
        if provider is None:
            continue
        projection = overlay_projection(line, provider)
        entry = line.core_entry
        for _level, selector, contract in catalog.line_selectors(entry):
            base = _selector_base(selector)
            if base not in captured_now:
                continue
            previous = aliases.get(base)
            record = {
                "provider": line.provider_id,
                "rd": plan.route_digests[line.provider_id],
                "wire": entry["wire_model"],
                "proxy_contract": contract,
                "display": entry["display"],
                "context_tokens": entry["context"]["provider_tokens"],
                "source": f"operator:{key}",
                "since_catalog": int(catalog_version),
                "overlay": copy.deepcopy(projection),
            }
            if catalog.is_keyed_compat(provider):
                # The exact canonical levels, captured with the
                # alias before config publication (required on a keyed route).
                record["thinking_levels"] = list(catalog.keyed_compat_levels(entry))
            if previous is not None and previous.get("source") == record["source"]:
                record["since_catalog"] = previous["since_catalog"]
            if previous != record:
                aliases[base] = record
                changed = True
            if base in document["pruned"]:
                del document["pruned"][base]
                changed = True
    return document if changed else None


def ledger_document(ledger: OperatorLedger | None) -> dict[str, Any]:
    """The serializable ledger (an empty v1 ledger for None)."""

    if ledger is None:
        return {"version": 1, "routes": {}, "admissions": {}, "aliases": {}, "pruned": {}, "removed": {},
                "transport_choices": {}}
    document: dict[str, Any] = {
        "version": 1,
        "routes": copy.deepcopy(ledger.routes),
        "admissions": copy.deepcopy(ledger.admissions),
        "aliases": copy.deepcopy(ledger.aliases),
        "pruned": dict(ledger.pruned),
        "removed": copy.deepcopy(ledger.removed),
        "transport_choices": dict(ledger.transport_choices),
    }
    if ledger.migration is not None:
        document["migration"] = copy.deepcopy(ledger.migration)
    return document


def write_ledger(environ: Mapping[str, str], document: Mapping[str, Any], schemas: OperatorSchemas) -> OperatorLedger:
    """Validate and atomically write the ledger (0600, canonical bytes).

    The caller holds the api-key leaf (render captures) or its documented
    outer locks; raises :class:`OperatorError` for an invalid document and
    lets ``state.StateError``/``OSError`` (incl. ``CommittedStateError``)
    through so the caller refuses to publish a config after a failed write.
    """

    raw = strict_json.canonical_file_bytes(document)
    ledger = parse_ledger(raw, schemas.ledger)  # never writes what it could not read back
    target = ledger_path(environ)
    state.ensure_private_dir(target.parent)
    state.atomic_write(target, raw)
    return ledger


def lost_live_references(
    ledger: OperatorLedger | None, emitted: Iterable[str], refs: Mapping[str, frozenset[str]],
    live: frozenset[str], *, declared: Mapping[str, str] | None = None,
) -> dict[str, tuple[str, frozenset[str]]]:
    """Operator aliases a candidate would stop serving that a live record
    still names: alias -> (providers.d file label, live record ids).

    Captured aliases (the ledger) and ``declared`` names
    (:func:`declared_aliases`, alias -> file label) both count: absent
    capture history never proves an omitted declaration unreferenced.
    """

    served = set(emitted)
    labels = {alias: file_label(capture["provider"])
              for alias, capture in (ledger.aliases if ledger is not None else {}).items()}
    for alias, label in (declared or {}).items():
        labels.setdefault(alias, label)
    lost: dict[str, tuple[str, frozenset[str]]] = {}
    for alias in sorted(labels):
        if alias in served:
            continue
        holders = refs.get(alias, frozenset()) & live
        if holders:
            lost[alias] = (labels[alias], frozenset(holders))
    return lost


UNKNOWN_DECLARATION = "a providers.d file with problems"


def unproven_live_references(
    refs: Mapping[str, frozenset[str]], live: frozenset[str], emitted: Iterable[str],
    declared: Mapping[str, str], *, secret_values: Callable[[], Iterable[str]] | None = None,
) -> list[tuple[str, str, frozenset[str]]]:
    """With capture history absent or corrupt, every live reference to a
    selector base in the reserved ``custom-`` operator namespace that the
    candidate does not serve is unsafe, whatever :func:`declared_aliases`
    could extract. Returns sorted ``(shown alias, label, live record ids)``.

    ``label`` names the declaring providers.d file only where it is safely
    known (``declared``, which never names a secret-bearing file), else
    :data:`UNKNOWN_DECLARATION`. A secret-like alias or label (token shapes
    or stored values) is withheld. Pure; ``secret_values``
    (a non-raising reader) is called at most once, only when a reference
    is unsafe.
    """

    served = set(emitted)
    found: dict[tuple[str, str], frozenset[str]] = {}
    literals: tuple[str, ...] | None = None
    for alias in sorted(refs):
        if not alias.startswith(catalog.CUSTOM_NAMESPACE) or alias in served:
            continue
        holders = refs[alias] & live
        if not holders:
            continue
        if literals is None:  # read the stored values only when something is shown
            literals = _store_literals(secret_values() if secret_values is not None else ())
        label = declared.get(alias)
        if label is None or _secret_bearing(label, literals):
            label = UNKNOWN_DECLARATION
        shown = _REDACTED if _secret_bearing(alias, literals) else alias
        found[(shown, label)] = found.get((shown, label), frozenset()) | holders
    return [(shown, label, holders) for (shown, label), holders in sorted(found.items())]


def prepared_fingerprint(environ: Mapping[str, str]) -> dict[str, Any]:
    """Raw, non-raising digests of providers.d and the ledger for the
    prepared-start receipt (read-only inside the confined unit)."""

    def digest(path: Path) -> str | None:
        try:
            with open(path, "rb") as handle:
                data = handle.read(READ_LIMIT_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return f"unreadable:{exc.errno}"
        return strict_json.sha256_hex(data)

    directory = providers_dir(environ)
    files: dict[str, str | None] = {}
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        names = None
    except OSError as exc:
        names = []
        files["."] = f"unreadable:{exc.errno}"
    for name in names or ():
        if FILE_NAME.fullmatch(name) or name == MIGRATION_MARKER:
            files[name] = digest(directory / name)
    return {"providers": None if names is None else files, "ledger": digest(ledger_path(environ))}


# ------------------------------------------------------------ admission
ROUTE_USABLE = frozenset({"approved", "keyless", "catalog"})


def operator_line_admitted(
    key: str, *, layer: OperatorLayer, ledger: OperatorLedger | None,
    admitted_lines: Iterable[str],
) -> bool:
    """A current, digest-bound local badge, independent of route usability."""

    line = layer.lines.get(key)
    if line is None or ledger is None or key not in admitted_lines:
        return False
    grant = ledger.admissions.get(key)
    return grant is not None and grant["digest"] == line.definition_digest


def operator_line_offered(
    key: str, *, layer: OperatorLayer, ledger: OperatorLedger | None = None,
    admitted_lines: Iterable[str] = (), provider_enabled: bool,
) -> bool:
    """Valid definition, enabled provider and usable route; no admission veto.

    ``admitted_lines`` is accepted for older callers but conveys no use
    authority. A missing ledger is harmless on catalog/keyless routes, never
    an approval of a keyed operator route. Credential presence and selected
    catalog transport problems are checked by the runtime separately.
    """

    line = layer.lines.get(key)
    if line is None or not provider_enabled:
        return False
    if layer.route_status.get(line.provider_id, "catalog") not in ROUTE_USABLE:
        return False
    provider = layer.providers.get(line.provider_id)
    if provider is not None and provider.auth_kind != "none":
        grant = ledger.routes.get(line.provider_id) if ledger is not None else None
        return grant is not None and grant["rd"] == provider.route_digest
    return True


# ------------------------------------------------------------ displacement
def displaced_lines(
    candidate_docs: Mapping[str, Any], layer: OperatorLayer, ledger: OperatorLedger | None,
) -> tuple[DisplacedLine, ...]:
    """Operator lines a candidate catalog would displace.

    Pure: explicit candidate docs, the resolved layer and the validated
    ledger only (``None`` = no ledger yet, so nothing is admitted). A row
    names the operator key, provider, wire, the candidate catalog key(s)
    serving the same (provider, wire) — merge's identity — and whether the
    current ledger admission matches the operator definition. There is no
    automatic successor. Malformed input raises :class:`OperatorError`.
    """

    if not isinstance(layer, OperatorLayer):
        raise OperatorError("displaced_lines: layer is not a resolved OperatorLayer")
    if ledger is not None and not isinstance(ledger, OperatorLedger):
        raise OperatorError("displaced_lines: ledger is not a validated OperatorLedger")
    try:
        lines = _docs_lines(candidate_docs)
        index: dict[tuple[str, str], list[str]] = {}
        for ckey in sorted(lines):
            entry = lines[ckey]
            index.setdefault((entry["provider"], entry["wire_model"]), []).append(ckey)
    except (KeyError, TypeError, AttributeError) as exc:
        raise OperatorError(f"displaced_lines: candidate docs are malformed ({type(exc).__name__})") from exc
    rows: list[DisplacedLine] = []
    every = {**layer.displaced, **layer.lines}
    for key in sorted(every):
        line = every[key]
        route = (line.provider_id, line.core_entry["wire_model"])
        matches = index.get(route)
        if not matches:
            continue
        grant = ledger.admissions.get(key) if ledger is not None else None
        rows.append(DisplacedLine(
            key=key, provider=route[0], wire=route[1], catalog_keys=tuple(matches),
            admitted=grant is not None and grant["digest"] == line.definition_digest,
        ))
    return tuple(rows)


# ------------------------------------------------------------ migration preflight
def migration_documents(registry: Mapping[str, Any], docs: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The providers.d documents ``migrate-custom`` would write (pure).

    One file per legacy provider (zero-model providers included) and one
    lines-only file per catalog provider that legacy models ride. Models map
    ``m`` -> ``custom-m`` with the exact legacy selector, ``["high"]``,
    default ``high`` and family ``unknown`` for legacy providers.
    """

    catalog_providers = docs["providers"]["providers"]
    documents: dict[str, dict[str, Any]] = {}
    for pid in sorted(registry.get("providers", {})):
        spec = registry["providers"][pid]
        auth: dict[str, Any] = {"kind": spec["auth_kind"], "secret_ref": f"env:{spec['secret_env']}"}
        if spec["auth_kind"] == "header":
            auth["header"] = spec.get("header", MIGRATION_HEADER)
        documents[pid] = {
            "version": DATA_VERSION,
            "provider": {
                "display": spec.get("display", pid),
                "kind": "anthropic-compatible",
                "base_url": spec["base_url"],
                "auth": auth,
                "independence_family": MIGRATED_FAMILY,
            },
            "lines": {},
        }
    for mid in sorted(registry.get("models", {})):
        spec = registry["models"][mid]
        pid = spec["provider"]
        if pid not in documents:
            if pid not in catalog_providers:
                continue
            documents[pid] = {"version": DATA_VERSION, "lines": {}}
        documents[pid]["lines"][f"custom-{mid}"] = {
            "wire_model": spec["wire_model"],
            "display": spec.get("display", mid),
            "efforts": ["high"],
            "default_effort": "high",
            "context": {"declared_tokens": spec["context_tokens"], "source": "operator"},
            "selector": f"custom-{mid}",
            "legacy_key": mid,
            "migrated_from": MIGRATED_FROM,
        }
    return documents


def migration_preflight(
    registry: Mapping[str, Any], docs: Mapping[str, Any], *, schemas: OperatorSchemas,
) -> MigrationPreflight:
    """All-or-nothing, field-specific preflight of every target.

    Checks the named predicates (model id, provider id, secret_env, header,
    base_url) with a remedy per field, then fully validates every
    synthesized document as it would load after activation. Writes nothing.
    """

    problems: list[OperatorProblem] = []
    source = MIGRATED_FROM
    catalog_providers = docs["providers"]["providers"]
    for mid in sorted(registry.get("models", {})):
        if not LEGACY_MODEL_ID.fullmatch(mid) or len(mid) > LEGACY_MODEL_ID_MAX:
            problems.append(_problem(
                "migrate-model-id", source, f"$.models.{mid}",
                f"model id {mid!r} cannot migrate (must match {LEGACY_MODEL_ID.pattern}, at most {LEGACY_MODEL_ID_MAX} characters)",
                f"re-add it under a valid id with claude-multi custom, then remove {mid}",
            ))
        provider = registry["models"][mid].get("provider")
        if provider not in registry.get("providers", {}) and provider not in catalog_providers:
            problems.append(_problem("migrate-model-provider", source, f"$.models.{mid}.provider",
                                     f"model {mid} names unknown provider {provider!r}",
                                     f"correct its provider in custom.json or remove {mid}"))
        elif provider in catalog_providers and catalog_providers[provider]["transport"]["kind"] == "oauth-pool":
            problems.append(_problem("migrate-model-provider", source, f"$.models.{mid}.provider",
                                     f"model {mid} rides OAuth pool {provider}, which legacy customs never could",
                                     f"remove {mid} from custom.json"))
    ports = gateway_ports(docs)
    referenced = {spec.get("provider") for spec in registry.get("models", {}).values()}
    skipped: list[OperatorProblem] = []
    for pid in sorted(registry.get("providers", {})):
        spec = registry["providers"][pid]
        where = f"$.providers.{pid}"
        found: list[OperatorProblem] = []
        _legacy_provider_problems(pid, spec, where, source, catalog_providers, ports, found)
        (problems if pid in referenced else skipped).extend(found)
    documents = migration_documents(_referenced_only(registry), docs) if not problems else {}
    if documents:
        raw = {pid: strict_json.pretty_file_bytes(doc) for pid, doc in documents.items()}
        layer = validate_layer(docs, raw, schemas=schemas, marker=True)
        # A displaced line yields to the catalog by design: the
        # preview lists it through displaced_lines; it never refuses.
        blocking = blocking_problems(layer)
        problems.extend(blocking)
        expected = {f"custom-{mid}" for mid in registry.get("models", {})} - set(layer.displaced)
        missing = sorted(expected - set(layer.lines))
        if missing and not blocking:
            problems.extend(_problem("migrate-parity", source, "$.models", f"{key} would not load after migration")
                            for key in missing)
    zero = tuple(sorted(pid for pid in registry.get("providers", {}) if pid not in referenced))
    return MigrationPreflight(documents=documents if not problems else {}, problems=tuple(_scrub(p) for p in problems),
                              zero_model_providers=zero, skipped_problems=tuple(_scrub(p) for p in skipped))


def _referenced_only(registry: Mapping[str, Any]) -> dict[str, Any]:
    referenced = {spec.get("provider") for spec in registry.get("models", {}).values()}
    return {**registry, "providers": {pid: spec for pid, spec in registry.get("providers", {}).items()
                                      if pid in referenced}}


def _legacy_provider_problems(
    pid: str, spec: Mapping[str, Any], where: str, source: str, catalog_providers: Mapping[str, Any],
    ports: Iterable[int], problems: list[OperatorProblem],
) -> None:
    """The field predicates of one legacy provider (field-specific remedies)."""

    if not LEGACY_PROVIDER_ID.fullmatch(pid):
        problems.append(_problem("migrate-provider-id", source, where,
                                 f"provider id {pid!r} cannot migrate (must match {LEGACY_PROVIDER_ID.pattern})",
                                 "re-add the provider under a valid id with claude-multi custom add-provider"))
    elif pid in catalog_providers or render.is_reserved_name(pid):
        problems.append(_problem("migrate-provider-id", source, where,
                                 f"provider id {pid} is a catalog or reserved id",
                                 "re-add the provider under another id with claude-multi custom add-provider"))
    name = spec.get("secret_env")
    reason = secret_store.secret_name_problem(name) if isinstance(name, str) else "missing secret_env"
    if reason is None and name in catalog.RESERVED_LEAD_ENV_KEYS:
        reason = f"secret name {name} is compiler-owned"
    if reason:
        problems.append(_problem("migrate-secret-env", source, f"{where}.secret_env", reason,
                                 "store the key under a new name and correct secret_env in custom.json"))
    if spec.get("auth_kind") == "header" and spec.get("header", MIGRATION_HEADER) != MIGRATION_HEADER:
        problems.append(_problem("migrate-header", source, f"{where}.header",
                                 f"header {spec.get('header')!r} is not sent by the gateway (x-api-key only)",
                                 "set \"header\": \"x-api-key\" for it in custom.json"))
    try:
        normalize_endpoint(spec.get("base_url"), keyed=True, lan=False, gateway_ports=ports)
    except ValueError as exc:
        problems.append(_problem("migrate-base-url", source, f"{where}.base_url", f"base_url refused: {exc}",
                                 "correct base_url in custom.json (https, public host, no userinfo)"))


# ------------------------------------------------------------ writer guard
ScreenValues = Callable[[], "Iterable[str] | None"]


def store_scan_values(environ: Mapping[str, str]) -> frozenset[str] | None:
    """Every stored secret value for an in-memory literal screen, or None
    when the store cannot be read (the caller then shows nothing)."""

    try:
        return secret_store.default_store(environ).scan_values()
    except (errors.ClaudeMultiError, OSError, ValueError):
        return None


def write_refusal(file_id: str, fragment: Any, secret_values: ScreenValues | None = None) -> str:
    """The exact refusal for a read-only (managed elsewhere) provider file.

    The fragment is shown only after it passed the token-shape and the
    stored-value screen; an unscreened, secret-bearing or absent fragment
    (an edit of an existing, unvalidated file) gets a value-free refusal.
    """

    head = f"{file_label(file_id)} is read-only here (managed elsewhere)"
    if fragment is None:
        return f"{head} — change it at its source"
    values = secret_values() if secret_values is not None else None
    if values is None:
        return f"{head} — make this change at its source (the fragment is not shown: stored credentials could not be screened)"
    body = strict_json.pretty_file_bytes(fragment).decode("utf-8").rstrip("\n")
    if _secret_hits(fragment, "$", values) or _raw_secret_hit(body.encode("utf-8"), values):
        return (f"{head} — make this change at its source (the fragment is not shown: it contains a secret-like "
                "or stored credential literal; reference credentials as env:NAME)")
    return f"{head} — add this to its source:\n{body}"


def check_writable_target(directory: Path | str, file_id: str, fragment: Any,
                          secret_values: ScreenValues | None = None) -> Path:
    """Refuse unless ``providers.d`` is a private owned directory (not a
    symlink) and the target is absent or a safe owned 0600 regular file.

    A symlink (file or directory) is never replaced: the
    refusal prints the fragment to add to its source instead, when it
    passes the literal screen (``secret_values`` yields the stored values;
    ``fragment=None`` asks for a value-free refusal).
    """

    if not PROVIDER_ID.fullmatch(file_id):
        raise OperatorError(f"invalid provider id {file_id!r}")
    base = Path(directory)
    target = base / f"{file_id}.json"
    if os.path.islink(base) or os.path.islink(target):
        raise OperatorError(write_refusal(file_id, fragment, secret_values))
    if os.path.lexists(target):
        try:
            state.read_private(target)
        except state.StateError as exc:
            raise OperatorError(write_refusal(file_id, fragment, secret_values)) from exc
    if os.path.lexists(base):
        try:
            state._check_directory(base)
        except state.StateError as exc:
            raise OperatorError(write_refusal(file_id, fragment, secret_values)) from exc
    return target


# ============================================================ store services
# Store methods do safe reads and locked commits only; callers inject
# consent, network (the smoke transport) and presentation. Lock order
# (freeze lock_order): migration guard -> token-rotation lock (non-blocking
# for served mutations) -> store locks (this operator store lock; profiles,
# bindings, secret file) -> Settings lock -> api-key leaf. The api-key leaf
# never acquires a store lock; ledger read-modify-writes run inside it so a
# concurrent render's capture write cannot interleave.
def utc_stamp(now: datetime.datetime | None = None) -> str:
    moment = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_migration_document(document: Mapping[str, Any]) -> bool:
    """Every line was written by migrate-custom (a zero-model provider
    vacuously): such a file stays pending until the activation marker."""

    lines = document.get("lines")
    return isinstance(lines, Mapping) and all(
        isinstance(line, Mapping) and line.get("migrated_from") == MIGRATED_FROM for line in lines.values()
    )


@contextlib.contextmanager
def _file_lock(target: Path) -> Iterator[state.FileLock]:
    state.ensure_private_dir(target.parent)
    lock = state.FileLock(target)
    lock.acquire(blocking=True)
    try:
        yield lock
    finally:
        lock.release()


def store_lock(environ: Mapping[str, str]) -> contextlib.AbstractContextManager[state.FileLock]:
    """The operator store lock (providers.d writes and non-render ledger
    writes): ``operator-ledger.json.lock`` beside the ledger."""

    return _file_lock(ledger_path(environ))


def api_key_leaf(environ: Mapping[str, str]) -> contextlib.AbstractContextManager[state.FileLock]:
    """The api-key leaf the render holds (never acquire a store lock inside)."""

    return _file_lock(paths.gateway_config_dir(environ) / "api-key")


def update_ledger(
    environ: Mapping[str, str], schemas: OperatorSchemas, mutate: Callable[[dict[str, Any]], None],
) -> OperatorLedger:
    """Read-modify-write the ledger inside the api-key leaf. A corrupt or
    unsafe ledger raises :class:`OperatorError` and is never overwritten."""

    with api_key_leaf(environ):
        document = ledger_document(load_ledger(environ, schemas))
        mutate(document)
        return write_ledger(environ, document, schemas)


def document_bytes(document: Mapping[str, Any]) -> bytes:
    """The bytes a tool-written providers.d file carries (pretty, sorted)."""

    return strict_json.pretty_file_bytes(document)


def write_provider_bytes(environ: Mapping[str, str], file_id: str, raw: bytes) -> Path:
    """Write one providers.d file (absent or a safe owned 0600 regular file;
    a symlink is never replaced)."""

    directory = providers_dir(environ)
    fragment = strict_json.loads(raw) if raw else {}
    target = check_writable_target(directory, file_id, fragment, lambda: store_scan_values(environ))
    state.ensure_private_dir(directory)
    state.atomic_write(target, raw)
    return target


def write_provider_file(environ: Mapping[str, str], file_id: str, document: Mapping[str, Any]) -> Path:
    return write_provider_bytes(environ, file_id, document_bytes(document))


def remove_provider_file(environ: Mapping[str, str], file_id: str) -> bool:
    directory = providers_dir(environ)
    target = directory / f"{file_id}.json"
    if os.path.islink(directory) or os.path.islink(target):
        raise OperatorError(f"{file_label(file_id)} is read-only here (managed elsewhere) — remove it from its source")
    try:
        return state.remove_private(target)
    except state.StateError as exc:
        raise OperatorError(f"{file_label(file_id)} cannot be removed here ({exc.strerror or type(exc).__name__}) "
                            "— remove it from its source") from exc


def route_record(provider: ResolvedProvider, *, at: str) -> dict[str, Any]:
    """The ledger route approval for a resolved T2 provider."""

    return {
        "rd": provider.route_digest,
        "secret_ref": f"env:{provider.secret_name}" if provider.secret_name else None,
        "auth": provider.auth_kind,
        "header": provider.header,
        "origin": provider.origin,
        "listing_origin": provider.listing_origin,
        "approved_at": at,
    }


def admission_record(layer: OperatorLayer, key: str, *, at: str, via: str) -> dict[str, Any]:
    """A ledger grant: the current definition digest plus the diagnostic
    (prior wire and per-field hashes, never a routing document)."""

    line = layer.lines[key]
    return {
        "digest": line.definition_digest,
        "at": at,
        "via": via,
        "diagnostic": {"wire": line.core_entry["wire_model"], "fields": dict(layer.metadata[key]["field_hashes"])},
    }


def changed_fields(grant: Mapping[str, Any], layer: OperatorLayer, key: str) -> tuple[str, ...]:
    """The definition fields that differ from the admitted definition."""

    old = dict(grant.get("diagnostic", {}).get("fields", {}))
    new = dict(layer.metadata.get(key, {}).get("field_hashes", {}))
    return tuple(sorted(name for name in set(old) | set(new) if old.get(name) != new.get(name)))


def line_file_ids(read: ProvidersRead | None) -> dict[str, str]:
    """line key -> declaring file id, best effort over the raw files (pure,
    never raises; a secret-bearing or unparsable file contributes nothing)."""

    found: dict[str, str] = {}
    for file_id in sorted(read.files if read is not None else {}):
        raw = read.files[file_id]
        if _raw_secret_hit(raw):
            continue
        try:
            document = strict_json.loads(raw, strict_json.JSONLimits(max_bytes=READ_LIMIT_BYTES, max_string=4096))
        except (strict_json.StrictJSONError, ValueError):
            continue
        lines = document.get("lines") if isinstance(document, dict) else None
        if isinstance(lines, dict):
            for key in sorted(lines):
                if isinstance(key, str) and not _secret_bearing(key):
                    found.setdefault(key, file_id)
    return found


def proposed_layer(
    docs: Mapping[str, Any], read: ProvidersRead, changes: Mapping[str, bytes | None], *,
    schemas: OperatorSchemas, ledger: OperatorLedger | None, legacy: Mapping[str, Any] | None = None,
    secret_values: "frozenset[str] | Callable[[], Iterable[str] | None]" = frozenset(),
) -> OperatorLayer:
    """The full layer a write command would produce (validate before
    committing; no silently partial render). A callable ``secret_values``
    is invoked only when the candidate layer has an independently
    valid contribution (:func:`store_scan_needed`)."""

    files = dict(read.files)
    for file_id, raw in changes.items():
        if raw is None:
            files.pop(file_id, None)
        else:
            files[file_id] = raw
    return validate_layer(docs, files, schemas=schemas, marker=read.marker, ledger=ledger, legacy=legacy,
                          secret_values=_scan_values(secret_values, docs, files), read_problems=read.problems)


def file_problems(layer: OperatorLayer, file_id: str) -> tuple[OperatorProblem, ...]:
    return tuple(problem for problem in layer.problems_by_file.get(file_label(file_id), ())
                 if problem.code not in NONBLOCKING_CODES)


# ------------------------------------------------------------ evidence and smoke
SMOKE_DEADLINE_SECONDS = 120.0
SMOKE_MAX_BYTES = 256 * 1024
SMOKE_INPUT_TOKENS = 40
SMOKE_RESULTS = ("pass", "fail", "degenerate")


@dataclass(frozen=True)
class SmokeOutcome:
    """One smoke verdict (never a prompt, body, header or credential)."""

    result: str  # pass | fail | degenerate
    http: int | None
    reason: str


def classify_smoke(status: int | None, body: bytes, *, overflow: bool = False, timed_out: bool = False) -> SmokeOutcome:
    """HTTP + structural validation of one bounded smoke response.

    The wall-clock and byte caps make a response ``degenerate``; any other
    non-200 or a body that is not an Anthropic message object fails.
    ``max_tokens`` is sent but never trusted as a bound.
    """

    if timed_out:
        return SmokeOutcome("degenerate", status, "the wall-clock cap was reached")
    if overflow:
        return SmokeOutcome("degenerate", status, "the response exceeded the byte cap")
    if status is None:
        return SmokeOutcome("fail", None, "no HTTP response")
    if status != 200:
        return SmokeOutcome("fail", status, f"HTTP {status}")
    try:
        document = strict_json.loads(body, strict_json.JSONLimits(max_bytes=SMOKE_MAX_BYTES))
    except (strict_json.StrictJSONError, ValueError, UnicodeDecodeError):
        return SmokeOutcome("fail", status, "the response is not strict JSON")
    if not isinstance(document, dict) or document.get("type") != "message" \
            or not isinstance(document.get("content"), list):
        return SmokeOutcome("fail", status, "the response is not a message object")
    return SmokeOutcome("pass", status, "ok")


@dataclass(frozen=True)
class SmokePlan:
    """What one consented smoke would send (disclosed before consent)."""

    key: str
    alias: str
    effort: str
    upstream: str
    auth: str
    host: str
    digest: str


def smoke_plan(docs: Mapping[str, Any], layer: OperatorLayer, key: str) -> SmokePlan:
    """The single request of an admission smoke: the default effort's
    served alias through the loopback gateway (pure)."""

    line = layer.lines[key]
    entry = line.core_entry
    selectors = catalog.line_selectors(dict(entry))
    chosen = next((item for item in selectors if item[0] == entry["default_effort"]), selectors[0])
    provider = layer.providers.get(line.provider_id)
    catalog_provider = docs["providers"]["providers"].get(line.provider_id)
    if provider is not None:
        upstream, host = provider.origin, urllib.parse.urlsplit(provider.origin).hostname or "unknown"
        auth = ("none (keyless)" if provider.auth_kind == "none"
                else f"{provider.auth_kind} from {provider.secret_name} (value not shown)")
    else:
        transport = catalog_provider["transport"]
        if transport["kind"] == "oauth-pool":
            upstream, host = f"the {transport['pool']} OAuth pool", transport["pool"]
            auth = "an OAuth pool credential held by the gateway (not shown)"
        else:
            parts = urllib.parse.urlsplit(transport["base_url"])
            upstream, host = f"{parts.scheme}://{parts.netloc}", (parts.hostname or "unknown")
            spec = transport.get("auth") or {}
            ref = spec.get("secret_ref")
            auth = ("none (keyless)" if spec.get("kind") == "none"
                    else f"{spec.get('kind')} from {str(ref).removeprefix('env:')} (value not shown)")
    return SmokePlan(key=key, alias=_selector_base(chosen[1]), effort=chosen[0], upstream=upstream, auth=auth,
                     host=host.lower(), digest=line.definition_digest)


def smoke_consent_text(plan: SmokePlan) -> str:
    """The exact consent for the one smoke request."""

    return (
        "claude-multi will make ONE request to a provider:\n"
        f"  gateway alias: {plan.alias}\n"
        f"  upstream origin: {plan.upstream}\n"
        f"  auth: {plan.auth}\n"
        f"  why: admission smoke for {plan.key}\n"
        f"  approximately {SMOKE_INPUT_TOKENS} input tokens; {SMOKE_DEADLINE_SECONDS:g} s wall-clock cap; "
        f"{SMOKE_MAX_BYTES // 1024} KiB response cap\n"
        "Proceed? [y/N] "
    )


def evidence_record(plan: SmokePlan, outcome: SmokeOutcome, *, versions: Mapping[str, str], at: str) -> dict[str, Any]:
    return {
        "digest": plan.digest,
        "host": plan.host,
        "versions": {"launcher": versions["launcher"], "client": versions["client"], "gateway": versions["gateway"]},
        "checks": {"smoke": {"result": outcome.result, "http": outcome.http, "at": at}},
    }


def write_evidence(environ: Mapping[str, str], schemas: OperatorSchemas, key: str, record: Mapping[str, Any]) -> OperatorEvidence:
    """Record one line's verdict-only evidence (0600, locked). A corrupt
    evidence file raises and is never overwritten."""

    target = evidence_path(environ)
    with _file_lock(target):
        current = load_evidence(environ, schemas)
        document = {"version": DATA_VERSION, "lines": copy.deepcopy(current.lines) if current is not None else {}}
        document["lines"][key] = copy.deepcopy(dict(record))
        raw = strict_json.canonical_file_bytes(document)
        parsed = parse_evidence(raw, schemas.evidence)
        state.atomic_write(target, raw)
        return parsed


def smoke_current(evidence: OperatorEvidence | None, key: str, digest: str) -> bool:
    """A passing smoke recorded for the current definition digest."""

    found = (evidence.lines.get(key) if evidence is not None else None) or {}
    return found.get("digest") == digest and found.get("checks", {}).get("smoke", {}).get("result") == "pass"


# ------------------------------------------------------------ battery evidence
# Check paths inside ``checks``: ("smoke",), ("efforts", level),
# ("tools", variant), ("stream",), ("context",), ("exact_client",).
EVIDENCE_PASS = "pass"
EVIDENCE_INCONCLUSIVE = "inconclusive"


def smoke_check_record(outcome: SmokeOutcome, *, at: str, contracts: Mapping[str, Any] | None) -> dict[str, Any]:
    """The admission smoke as a check record: a transient failure
    (no response, 429, 5xx, the wall-clock cap) is inconclusive; the byte cap
    and definite rejections fail."""

    reason = "ok"
    if outcome.result == "pass":
        result = "pass"
    elif outcome.result == "degenerate":
        result, reason = ("failed", "oversize") if "byte cap" in outcome.reason else ("inconclusive", "timeout")
    elif outcome.http is None:
        result, reason = "inconclusive", "connection"
    elif outcome.http == 429:
        result, reason = "inconclusive", "rate-limited"
    elif 500 <= outcome.http <= 599:
        result, reason = "inconclusive", "upstream-error"
    elif outcome.http == 200:
        result, reason = "failed", "not-a-message"
    else:
        result, reason = "failed", "http-status"
    record: dict[str, Any] = {"result": result, "http": outcome.http, "at": at, "reason": reason}
    if contracts is not None:
        record["contracts"] = dict(contracts)
    return record


def merge_checks(existing: Mapping[str, Any] | None, *, digest: str, host: str, versions: Mapping[str, str],
                 results: Iterable[tuple[tuple[str, ...], Mapping[str, Any]]]) -> dict[str, Any]:
    """One line's evidence after new check records (pure).

    Evidence for another definition digest is dropped (it counts for
    nothing). A ``pass`` or ``failed`` replaces the same check; an
    ``inconclusive`` attempt never replaces an earlier pass. Unrequested
    checks stay as they were (each carries its own contract digests, so a
    pass under other pins is kept as contract-stale evidence). The context
    check keeps the highest successful measured bound of this definition
    in ``floor`` apart from the latest verdict, so a later smaller pass or
    a failure never lowers the validated floor.
    """

    same = isinstance(existing, Mapping) and existing.get("digest") == digest
    checks = copy.deepcopy(dict(existing.get("checks", {}))) if same else {}
    for path, record in results:
        parent = checks
        for part in path[:-1]:
            parent = parent.setdefault(part, {})
        previous = parent.get(path[-1])
        if (record.get("result") == EVIDENCE_INCONCLUSIVE and isinstance(previous, Mapping)
                and previous.get("result") == EVIDENCE_PASS):
            continue
        updated = dict(record)
        if path == ("context",):
            best = max((bound for bound in (_context_bound(previous), _context_bound(record)) if bound),
                       key=lambda bound: bound["measured"], default=None)
            if best is not None:
                updated["floor"] = best
        parent[path[-1]] = updated
    return {"digest": digest, "host": host, "versions": {name: versions[name] for name in ("launcher", "client", "gateway")},
            "checks": checks}


def _context_bound(check: Any) -> dict[str, Any] | None:
    """The successful measured bound a context record proves: its kept
    ``floor``, else the record itself when accepted with correct retrieval
    and a measured count."""

    if not isinstance(check, Mapping):
        return None
    floor = check.get("floor")
    if isinstance(floor, Mapping):
        return {"measured": floor["measured"], "at": floor["at"]}
    measured = check.get("measured")
    if (check.get("result") == EVIDENCE_PASS and check.get("retrieval") == "correct"
            and isinstance(measured, int) and not isinstance(measured, bool)):
        return {"measured": measured, "at": check["at"]}
    return None


def record_checks(environ: Mapping[str, str], schemas: OperatorSchemas, key: str, *, digest: str, host: str,
                  versions: Mapping[str, str], results: Iterable[tuple[tuple[str, ...], Mapping[str, Any]]]
                  ) -> OperatorEvidence:
    """Merge check records into the private store (0600, locked). A
    corrupt evidence file raises and is never overwritten."""

    target = evidence_path(environ)
    with _file_lock(target):
        current = load_evidence(environ, schemas)
        document = {"version": DATA_VERSION, "lines": copy.deepcopy(current.lines) if current is not None else {}}
        document["lines"][key] = merge_checks(document["lines"].get(key), digest=digest, host=host,
                                              versions=versions, results=list(results))
        raw = strict_json.canonical_file_bytes(document)
        parsed = parse_evidence(raw, schemas.evidence)
        state.atomic_write(target, raw)
        return parsed


# Evidence states for the agent gate.
EVIDENCE_CURRENT = "current"
EVIDENCE_CONTRACT_STALE = "contract-stale"
EVIDENCE_MISSING = "missing"
EVIDENCE_FAILED = "failed"
EVIDENCE_DEFINITION_STALE = "definition-stale"
EVIDENCE_STATES = (EVIDENCE_CURRENT, EVIDENCE_CONTRACT_STALE, EVIDENCE_MISSING, EVIDENCE_FAILED,
                   EVIDENCE_DEFINITION_STALE)


def _check_at(checks: Mapping[str, Any], path: tuple[str, ...]) -> Mapping[str, Any] | None:
    node: Any = checks
    for part in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node if isinstance(node, Mapping) else None


def _pass_contracts_current(check: Mapping[str, Any], contracts: Mapping[str, Any]) -> bool:
    recorded = check.get("contracts")
    if not isinstance(recorded, Mapping) or contracts.get("gateway") is None:
        return False
    return recorded.get("client") == contracts.get("client") and recorded.get("gateway") == contracts.get("gateway")


@dataclass(frozen=True)
class EvidenceView:
    """The agent-relevant reading of one line's evidence (pure)."""

    state: str
    gaps: tuple[str, ...]  # missing or failed required checks (for the reason text)
    tools_variants: frozenset[str]  # variants with a pass under the current definition
    exact_client: str  # not-required | current | contract-stale | missing | failed | unavailable


def evidence_view(evidence: OperatorEvidence | None, key: str, *, digest: str, levels: Iterable[str],
                  contracts: Mapping[str, Any], pool_line: bool) -> EvidenceView:
    """``current`` only when every required check (smoke, every declared
    effort, a tools variant, stream) passed for this definition under the
    current client and gateway contract digests; ``contract-stale`` when
    they all passed but some under other digests (or with no gateway
    manifest); ``failed`` when a required check's latest definite result
    failed; ``missing`` otherwise. Another definition digest is
    ``definition-stale``. The exact-client check (first-party pools) is
    read separately."""

    found = evidence.lines.get(key) if evidence is not None else None
    if not isinstance(found, Mapping):
        return EvidenceView(EVIDENCE_MISSING, ("smoke", "efforts", "tools", "stream"), frozenset(),
                            "missing" if pool_line else "not-required")
    if found.get("digest") != digest:
        return EvidenceView(EVIDENCE_DEFINITION_STALE, (), frozenset(),
                            "missing" if pool_line else "not-required")
    checks = found.get("checks") or {}
    required: list[tuple[str, tuple[str, ...]]] = [("smoke", ("smoke",))]
    required += [(f"effort {level}", ("efforts", level)) for level in levels]
    required.append(("stream", ("stream",)))
    tools = {variant: _check_at(checks, ("tools", variant)) for variant in ("forced", "auto")}
    passed_variants = frozenset(v for v, check in tools.items() if check is not None and check.get("result") == "pass")
    failed: list[str] = []
    missing: list[str] = []
    stale = False
    for label, path in required:
        check = _check_at(checks, path)
        result = check.get("result") if check is not None else None
        if result == "pass":
            stale = stale or not _pass_contracts_current(check, contracts)
        elif result in ("failed", "fail", "degenerate"):
            failed.append(label)
        else:
            missing.append(label)
    if passed_variants:
        stale = stale or not any(_pass_contracts_current(tools[v], contracts) for v in passed_variants)
    elif any(check is not None and check.get("result") == "failed" for check in tools.values()):
        failed.append("tools")
    else:
        missing.append("tools")
    exact = "not-required"
    if pool_line:
        check = _check_at(checks, ("exact_client",))
        result = check.get("result") if check is not None else None
        if result == "pass":
            exact = "current" if _pass_contracts_current(check, contracts) else "contract-stale"
        elif result == "failed":
            exact = "failed"
        elif check is not None and check.get("reason") == "unavailable":
            exact = "unavailable"
        else:
            exact = "missing"
    if failed:
        return EvidenceView(EVIDENCE_FAILED, tuple(failed + missing), passed_variants, exact)
    if missing:
        return EvidenceView(EVIDENCE_MISSING, tuple(missing), passed_variants, exact)
    return EvidenceView(EVIDENCE_CONTRACT_STALE if stale else EVIDENCE_CURRENT, (), passed_variants, exact)


def evidence_floor(evidence: OperatorEvidence | None, key: str, *, digest: str, declared: int,
                   default_floor: int) -> tuple[int, str | None]:
    """(validated floor, date) from current-definition context evidence:
    accepted, retrieval correct and a measured count only (§9.4)."""

    found = evidence.lines.get(key) if evidence is not None else None
    if not isinstance(found, Mapping) or found.get("digest") != digest:
        return default_floor, None
    bound = _context_bound(_check_at(found.get("checks") or {}, ("context",)))
    if bound is None:
        return default_floor, None
    return max(default_floor, min(bound["measured"], declared)), str(bound["at"])[:10] or None


def agent_fact_fields(
    key: str, *, layer: OperatorLayer, ledger: OperatorLedger | None, evidence: OperatorEvidence | None,
    admitted_lines: Iterable[str], provider_enabled: bool, contracts: Mapping[str, Any], docs: Mapping[str, Any],
    trusted_docs: Mapping[str, Any], ledger_error: str | None = None,
) -> dict[str, Any] | None:
    """The explicit facts ``profile.agent_eligibility`` needs for one
    operator line (current state; pure over the given inputs). None when
    the key is not a valid operator line.

    ``docs`` may be the merged view (T2 providers included); the known
    independence families (condition 4) come only from ``trusted_docs``,
    the unmerged reviewed catalog, so a T2 provider never certifies its own
    family."""

    line = layer.lines.get(key)
    if line is None:
        return None
    entry = line.core_entry
    provider = (layer.providers[line.provider_id].entry if line.provider_id in layer.providers
                else docs["providers"]["providers"][line.provider_id])
    pool = provider["transport"]["kind"] == "oauth-pool"
    efforts = entry["efforts"]
    levels = tuple(efforts) if isinstance(efforts, list) else tuple(sorted(efforts))
    view = evidence_view(evidence, key, digest=line.definition_digest, levels=levels, contracts=contracts,
                         pool_line=pool)
    route = layer.route_status.get(line.provider_id, "catalog")
    kind = layer.providers[line.provider_id].kind if line.provider_id in layer.providers else None
    return {
        "key": key,
        "provider": line.provider_id,
        "provider_enabled": provider_enabled,
        "ledger_error": ledger_error,
        "admitted": operator_line_admitted(key, layer=layer, ledger=ledger, admitted_lines=admitted_lines),
        "route": route,
        "d60": agent_route_kind(provider, kind, gateway=trusted_docs["gateway"]) is None,
        "route_kind": agent_route_kind(provider, kind, gateway=trusted_docs["gateway"]) or "",
        "family": line.family,
        "t1_families": t1_families(trusted_docs),
        "evidence": view.state,
        "evidence_gaps": view.gaps,
        "tools_variants": view.tools_variants,
        "pool": pool,
        "exact_client": view.exact_client,
    }


# ------------------------------------------------------------ alias pruning
@dataclass(frozen=True)
class CapturePrunePlan:
    remove: tuple[str, ...]
    keep: tuple[tuple[str, str], ...]
    refusal: str | None = None
    holders: tuple[str, ...] = ()  # the live records behind a refusal (full ids, for the remedy)


def plan_capture_prune(
    ledger: OperatorLedger | None, current: Iterable[str], refs: Mapping[str, frozenset[str]],
    live: frozenset[str], requested: Iterable[str] | None,
) -> CapturePrunePlan:
    """Which captured operator aliases may go (pure; records never modified).

    ``current`` are the selector bases current operator lines emit (their
    definition wins; pruning would only be re-captured). A live reference
    keeps an alias in all-mode and refuses the whole operation when named.
    """

    captured = dict(ledger.aliases) if ledger is not None else {}
    emitted = set(current)
    if requested:
        candidates = sorted({_selector_base(name) for name in requested} & set(captured))
    else:
        candidates = sorted(captured)
    remove: list[str] = []
    keep: list[tuple[str, str]] = []
    blocked: list[str] = []
    blocking: set[str] = set()
    for alias in candidates:
        holders = refs.get(alias, frozenset()) & live
        if holders:
            if requested:
                blocked.append(f"{alias} -> {', '.join(sorted(h[:8] for h in holders))}")
                blocking |= holders
            else:
                keep.append((alias, f"live session {', '.join(sorted(h[:8] for h in holders))}"))
            continue
        if alias in emitted:
            keep.append((alias, f"current operator line {captured[alias]['source'].removeprefix('operator:')}"))
            continue
        remove.append(alias)
    if blocked:
        return CapturePrunePlan((), (), "referenced by live session records: " + "; ".join(blocked),
                                tuple(sorted(blocking)))
    return CapturePrunePlan(tuple(remove), tuple(keep))


def apply_capture_prune(document: Mapping[str, Any], remove: Iterable[str], catalog_version: int) -> dict[str, Any]:
    """Drop the planned captures and tombstone them (monotonic)."""

    result = copy.deepcopy(dict(document))
    for alias in remove:
        result["aliases"].pop(alias, None)
        result["pruned"][alias] = int(catalog_version)
    return result


# ------------------------------------------------------------ custom.json migration
MARKER_KEYS = frozenset({"custom_sha256", "at", "targets"})


def marker_path(environ: Mapping[str, str]) -> Path:
    return providers_dir(environ) / MIGRATION_MARKER


def parse_marker(raw: bytes) -> dict[str, Any]:
    """The activation marker ``{custom_sha256, at, targets{id: sha256}}``."""

    try:
        document = strict_json.loads(raw)
    except strict_json.StrictJSONError as exc:
        raise OperatorError(f"{MIGRATION_MARKER}: not strict JSON") from exc
    if not isinstance(document, dict) or set(document) != MARKER_KEYS \
            or not isinstance(document["custom_sha256"], str) \
            or not re.fullmatch(r"[0-9a-f]{64}", document["custom_sha256"]) \
            or not isinstance(document["targets"], dict) \
            or not all(isinstance(k, str) and PROVIDER_ID.fullmatch(k) and isinstance(v, str)
                       and re.fullmatch(r"[0-9a-f]{64}", v) for k, v in document["targets"].items()):
        raise OperatorError(f"{MIGRATION_MARKER}: not a migrate-custom activation marker")
    return document


def read_marker(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """The marker document, None when absent; unusable raises OperatorError."""

    path = marker_path(environ)
    if not os.path.lexists(path):
        return None
    try:
        raw = state.read_owned_readable(path, max_bytes=READ_LIMIT_BYTES)
    except (state.StateError, OSError) as exc:
        raise OperatorError(f"{MIGRATION_MARKER} is unreadable or unsafe") from exc
    return parse_marker(raw)


@dataclass(frozen=True)
class CustomMigrationPlan:
    """All-or-nothing plan of ``providers migrate-custom`` (pure).

    ``status``: ``nothing`` (no legacy registry), ``ready`` (first
    migration), ``done`` (the marker records this source: idempotent),
    ``changed`` (the source changed since the marker: reconciliation, no
    automatic admission), ``refused`` (preflight or target problems).
    """

    status: str
    source_sha256: str | None
    preflight: MigrationPreflight
    targets: dict[str, bytes]
    writes: tuple[str, ...]
    unchanged: tuple[str, ...]
    conflicts: tuple[OperatorProblem, ...]
    layer: OperatorLayer | None
    routes: tuple[str, ...]
    admissions: tuple[str, ...]
    displaced: tuple[DisplacedLine, ...]
    kept_files: tuple[str, ...]
    pending: bool

    @property
    def problems(self) -> tuple[OperatorProblem, ...]:
        return self.preflight.problems + self.conflicts


def plan_custom_migration(
    docs: Mapping[str, Any], registry: Mapping[str, Any], source_raw: bytes | None, read: ProvidersRead, *,
    schemas: OperatorSchemas, ledger: OperatorLedger | None, marker_doc: Mapping[str, Any] | None,
) -> CustomMigrationPlan:
    """Plan the migration against the current providers.d (no writes).

    A target file is written only when absent or byte-owned by this tool
    (its bytes match the pending manifest or the marker's recorded hash);
    any other existing file refuses the whole migration with the exact
    fragment to merge by hand. Routes and grants (``via: migrate``) are
    planned only for a first migration; changed legacy content never gains
    an admission merely by being in custom.json.
    """

    empty = MigrationPreflight({}, (), ())
    legacy_present = bool(registry.get("providers") or registry.get("models"))
    source_sha = strict_json.sha256_hex(source_raw) if source_raw is not None else None
    pending_manifest = ledger.migration if ledger is not None else None
    if source_raw is None or not legacy_present:
        return CustomMigrationPlan("nothing", source_sha, empty, {}, (), (), (), None, (), (), (), (),
                                   pending_manifest is not None)
    if marker_doc is not None and marker_doc.get("custom_sha256") == source_sha:
        return CustomMigrationPlan("done", source_sha, empty, {}, (), (), (), None, (), (), (), (),
                                   pending_manifest is not None)
    preflight = migration_preflight(registry, docs, schemas=schemas)
    if not preflight.ok:
        return CustomMigrationPlan("refused", source_sha, preflight, {}, (), (), (), None, (), (), (), (),
                                   pending_manifest is not None)
    targets = {pid: document_bytes(doc) for pid, doc in sorted(preflight.documents.items())}
    owned: dict[str, set[str]] = {}
    for manifest in (pending_manifest, marker_doc):
        for pid, digest in ((manifest or {}).get("targets") or {}).items():
            owned.setdefault(pid, set()).add(digest)
    writes: list[str] = []
    unchanged: list[str] = []
    conflicts: list[OperatorProblem] = []
    for pid, raw in targets.items():
        existing = read.files.get(pid)
        if existing is None:
            label = f"{PROVIDERS_DIRNAME}/{pid}.json"
            if any(problem.file == label for problem in read.problems):
                conflicts.append(_problem("migrate-target", label, "", "exists but is unreadable or unsafe; "
                                          "migrate-custom never overwrites it"))
            else:
                writes.append(pid)
        elif existing == raw:
            unchanged.append(pid)
        elif strict_json.sha256_hex(existing) in owned.get(pid, set()):
            writes.append(pid)  # byte-owned by an earlier run of this tool
        else:
            body = strict_json.loads(raw)
            conflicts.append(_problem(
                "migrate-target", file_label(pid), "",
                "already exists and differs from what migrate-custom writes (it never overwrites)",
                "merge this into it by hand, then rerun:\n" + document_bytes(body).decode("utf-8").rstrip("\n"),
            ))
    files = dict(read.files)
    files.update(targets)
    layer = validate_layer(docs, files, schemas=schemas, marker=True, ledger=ledger, read_problems=read.problems)
    for pid in targets:
        for problem in file_problems(layer, pid):
            conflicts.append(problem)
    kept = tuple(sorted(set((marker_doc or {}).get("targets") or {}) - set(targets)))
    status = "changed" if marker_doc is not None else "ready"
    if conflicts:
        status = "refused"
    migrated_keys = {f"custom-{mid}" for mid in registry.get("models", {})}
    routes = tuple(sorted(pid for pid in preflight.documents if pid in layer.providers)) if status == "ready" else ()
    admissions = tuple(sorted(key for key in migrated_keys if key in layer.lines)) if status == "ready" else ()
    try:
        displaced = displaced_lines(docs, layer, ledger)
    except OperatorError:
        displaced = ()
    displaced = tuple(row for row in displaced if row.key in migrated_keys)
    return CustomMigrationPlan(status, source_sha, preflight, targets, tuple(writes), tuple(unchanged),
                               tuple(_scrub(p) for p in conflicts), layer, routes, admissions, displaced, kept,
                               pending_manifest is not None)


def apply_custom_migration(
    environ: Mapping[str, str], plan: CustomMigrationPlan, *, schemas: OperatorSchemas,
    admit: Callable[[tuple[str, ...], OperatorLayer], None], at: str | None = None,
) -> None:
    """Commit a planned migration (the caller holds the migration guard,
    the token-rotation lock and the operator store lock).

    Order (every step idempotent, so a rerun after a crash at any
    boundary resumes): pending manifest in the ledger -> publish the target
    files (byte-owned only) -> routes and ``via: migrate`` grants -> the
    Settings admissions (``admit``) -> the activation marker -> clear the
    manifest. Until the marker exists the loader skips the published files
    by their own metadata, so legacy and migrated lines never both load.
    custom.json and session records are never touched.
    """

    stamp = at or utc_stamp()
    if plan.status == "done":
        if plan.pending:
            update_ledger(environ, schemas, lambda doc: doc.pop("migration", None))
        return
    if plan.status not in ("ready", "changed") or plan.layer is None or plan.source_sha256 is None:
        raise OperatorError(f"migrate-custom: nothing to apply ({plan.status})")
    directory = providers_dir(environ)
    for pid in plan.writes:  # every refusal before the first write
        check_writable_target(directory, pid, strict_json.loads(plan.targets[pid]), lambda: store_scan_values(environ))
    target_hashes = {pid: strict_json.sha256_hex(raw) for pid, raw in plan.targets.items()}

    def pending(document: dict[str, Any]) -> None:
        document["migration"] = {"state": "pending", "source_sha256": plan.source_sha256, "started_at": stamp,
                                 "targets": dict(target_hashes)}

    update_ledger(environ, schemas, pending)
    for pid in plan.writes:
        write_provider_bytes(environ, pid, plan.targets[pid])
    layer = plan.layer
    if plan.routes or plan.admissions:
        def grant(document: dict[str, Any]) -> None:
            for pid in plan.routes:
                document["routes"][pid] = route_record(layer.providers[pid], at=stamp)
            for key in plan.admissions:
                document["admissions"][key] = admission_record(layer, key, at=stamp, via="migrate")

        update_ledger(environ, schemas, grant)
        admit(plan.admissions, layer)
    marker = {"custom_sha256": plan.source_sha256, "at": stamp, "targets": dict(target_hashes)}
    state.atomic_write(marker_path(environ), strict_json.canonical_file_bytes(marker))
    update_ledger(environ, schemas, lambda doc: doc.pop("migration", None))


def migration_preview_lines(plan: CustomMigrationPlan, *, key_refs: Mapping[str, Iterable[str]] | None = None) -> list[str]:
    """The dry-run report: one line per target, grants, displacement."""

    lines: list[str] = []
    if plan.status == "nothing":
        return ["custom.json: nothing to migrate (no legacy providers or models)"]
    if plan.status == "done":
        suffix = " (an interrupted run's manifest is left; --apply clears it)" if plan.pending else ""
        return [f"custom.json already migrated (sha256 {plan.source_sha256[:16]}){suffix}"]
    for problem in plan.problems:
        lines.append(f"refused: {problem.text()}")
    if plan.status == "refused":
        lines.append("nothing was written; fix every listed field, then rerun claude-multi providers migrate-custom")
        return lines
    if plan.status == "changed":
        lines.append("custom.json changed since migration: reconciling tool-owned files only; "
                     "changed or new lines need claude-multi models admit (no automatic admission)")
    for pid in sorted(plan.targets):
        document = strict_json.loads(plan.targets[pid])
        keys = sorted(document.get("lines", {}))
        action = "write" if pid in plan.writes else "unchanged"
        lines.append(f"{action} {file_label(pid)}: {', '.join(keys) or 'no lines'}")
    for pid in plan.preflight.zero_model_providers:
        lines.append(f"skipped legacy provider {pid}: no custom.json model references it "
                     "(not migrated, never rendered)")
    for problem in plan.preflight.skipped_problems:
        lines.append(f"note (skipped provider): {_problem_body(problem)}")
    for pid in plan.routes:
        provider = plan.layer.providers[pid] if plan.layer is not None else None
        if provider is not None:
            lines.append(f"approve route {pid}: {provider.auth_kind} env:{provider.secret_name} → {provider.origin}")
    for key in plan.admissions:
        lines.append(f"admit {key} (via migrate; unverified until claude-multi models qualify {key})")
    refs = key_refs or {}
    for row in plan.displaced:
        users = sorted(refs.get(row.key, ()))
        lines.append(f"displaced {row.key}: the catalog serves {row.provider}/{row.wire} as "
                     f"{', '.join(row.catalog_keys)}; it yields (no automatic successor)"
                     + (f"; used by {', '.join(users)}" if users else ""))
    for pid in plan.kept_files:
        lines.append(f"kept {file_label(pid)}: no longer in custom.json (remove with claude-multi providers rm {pid})")
    return lines


# ------------------------------------------------------------ doctor
@dataclass(frozen=True)
class DoctorFindings:
    """Operator doctor lines by severity (doctrine #6: damage BLOCKs, lazy
    or unreferenced state is Attention with the fix, counts are Info)."""

    blocks: tuple[str, ...]
    attention: tuple[str, ...]
    info: tuple[str, ...]


_SECRET_CODES = frozenset({"secret-literal"})
_UNSAFE_CODES = frozenset({"unsafe-file", "unsafe-directory", "too-large", "unreadable", "too-many-files"})
_CONFLICT_CODES = frozenset({"collision", "secret-origin", "legacy-collision"})


def _ids8(ids: Iterable[str]) -> str:
    return ", ".join(sorted(identifier[:8] for identifier in ids))


def _problem_body(problem: OperatorProblem) -> str:
    where = f"{problem.json_path}: " if problem.json_path else ""
    return f"{where}{problem.subject}" + (f" — {problem.remedy}" if problem.remedy else "")


def doctor_findings(
    docs: Mapping[str, Any], snapshot: OperatorSnapshot, *,
    plan: RenderPlan | None,
    admitted: Iterable[str],
    refs: Mapping[str, frozenset[str]],
    live: frozenset[str],
    unreadable: Iterable[str] = (),
    key_refs: Mapping[str, Iterable[str]] | None = None,
    served: frozenset[str] | None = None,
    evidence: OperatorEvidence | None = None,
    legacy: Mapping[str, Any] | None = None,
    legacy_sha: str | None = None,
    marker_doc: Mapping[str, Any] | None = None,
    marker_error: str | None = None,
    overlay_conflicts: Iterable[str] = (),
    unset_secrets: Iterable[str] = (),
    agent_eligible: int = 0,
    agent_qualified: int = 0,
    agent_usable: int | None = None,
) -> DoctorFindings:
    """B01-B05 / A01-A08 / I01-I02 for the operator layer (pure).

    ``refs``/``live`` come from the lock-free record scan (selector bases
    -> record ids); ``key_refs`` maps a line key to the profiles and named
    bindings that name it. A reference upgrades an otherwise-Attention
    problem to BLOCK (never the reverse).
    """

    layer = snapshot.layer
    ledger = snapshot.ledger
    admitted_set = set(admitted)
    key_users = {key: sorted(set(users)) for key, users in (key_refs or {}).items()}
    blocks: list[str] = []
    attention: list[str] = []
    info: list[str] = []

    def holders(aliases: Iterable[str]) -> frozenset[str]:
        found: set[str] = set()
        for alias in aliases:
            found |= set(refs.get(alias, frozenset())) & set(live)
        return frozenset(found)

    captures = dict(ledger.aliases) if ledger is not None else {}
    if snapshot.ledger_error is not None:
        text = (f"{snapshot.ledger_error}: operator admissions and captured aliases are unknown, so operator lines "
                "are unusable — review ~/.config/claude-multi/operator-ledger.json and restore it from a backup")
        (blocks if (layer.providers or layer.lines) else attention).append(text)
    unread = tuple(unreadable)
    if unread:
        attention.append(f"operator reference checks are incomplete: unreadable session records {_ids8(unread)}")

    # Per-file declared names (best effort) for reference impact.
    file_keys: dict[str, set[str]] = {}
    for key, file_id in line_file_ids(snapshot.read).items():
        file_keys.setdefault(file_label(file_id), set()).add(key)
    file_aliases: dict[str, set[str]] = {}
    for alias, label in declared_aliases(layer, snapshot.read).items():
        file_aliases.setdefault(label, set()).add(alias)
    for alias, capture in captures.items():
        file_aliases.setdefault(file_label(capture["provider"]), set()).add(alias)

    def file_users(label: str) -> list[str]:
        users = [f"session {identifier[:8]}" for identifier in sorted(holders(file_aliases.get(label, ())))]
        for key in sorted(file_keys.get(label, ())):
            users.extend(key_users.get(key, ()))
        return users

    for label in sorted(layer.problems_by_file):
        for problem in layer.problems_by_file[label]:
            if problem.code == "displaced":
                continue
            if problem.code in _SECRET_CODES or problem.code in _UNSAFE_CODES:
                blocks.append(problem.text())
                continue
            if problem.code == "file-name":
                attention.append(f"{problem.file} ignored: {problem.subject}")
                continue
            users = file_users(problem.file)
            if users:
                file_id = problem.file.removeprefix(f"{PROVIDERS_DIRNAME}/").removesuffix(".json")
                blocks.append(f"{problem.text()} — {len(users)} sessions/profiles use its lines "
                              f"({', '.join(users[:5])}); fix: claude-multi providers edit {file_id}")
            elif problem.code in _CONFLICT_CODES:
                attention.append(problem.text())
            else:
                attention.append(f"{problem.file} ignored: {_problem_body(problem)}")

    route_blocked: set[str] = set()
    for pid in sorted(layer.route_status):
        status = layer.route_status[pid]
        if status not in ("unapproved", "changed"):
            continue
        aliases = {base for line in layer.lines.values() if line.provider_id == pid
                   for base in line_aliases(line.core_entry)}
        aliases |= {alias for alias, capture in captures.items() if capture["provider"] == pid}
        if status == "changed":
            old, new = layer.route_changes.get(pid, ("?", "?"))
            text = route_status_text(pid, "changed", old=old, new=new)
        else:
            text = f"provider {pid}: route unapproved; not rendered — claude-multi providers approve {pid}"
        users = holders(aliases)
        if users:
            route_blocked.add(pid)
            blocks.append(f"{text} (used by {_ids8(users)})")
        else:
            attention.append(text)

    current_bases = {base for line in layer.lines.values() for base in line_aliases(line.core_entry)}
    for alias in sorted(captures):
        if alias in current_bases:
            continue
        capture = captures[alias]
        pid = capture["provider"]
        key = capture["source"].removeprefix("operator:")
        users = holders((alias,))
        rendered = plan is not None and alias in plan.captures
        if not rendered:
            if users:
                if pid not in route_blocked:
                    blocks.append(f"alias {alias} (provider {pid} not rendered) is used by {_ids8(users)} — "
                                  f"restore {file_label(pid)} or end the session; "
                                  f"{sessions_mod.mark_ended_remedy(users)}")
            else:
                attention.append(f"alias {alias} (captured for {key}; provider {pid} not rendered) has no line — "
                                 "prune when unused: claude-multi doctor --prune-aliases")
        elif not users:
            attention.append(f"alias {alias} (captured for {key}) has no line — "
                             "prune when unused: claude-multi doctor --prune-aliases")

    for key in sorted(ledger.admissions if ledger is not None else {}):
        grant = ledger.admissions[key]
        line = layer.lines.get(key)
        if line is None:
            if key not in layer.pending and key not in layer.displaced:
                attention.append(f"{key} is admitted but no valid declaration serves it (invalid or removed) — fix its "
                                 f"providers.d file or revoke: claude-multi models revoke {key}")
            continue
        if grant["digest"] != line.definition_digest:
            fields = [name for name in changed_fields(grant, layer, key) if name != "wire"]
            old_wire = grant.get("diagnostic", {}).get("wire")
            parts = ([f"wire {old_wire}→{line.core_entry['wire_model']}"]
                     if old_wire and old_wire != line.core_entry["wire_model"] else []) + fields
            users = holders(line_aliases(line.core_entry))
            retarget = f"; live sessions retarget at the next reload: {_ids8(users)}" if users else ""
            attention.append(f"{key} changed since admission ({', '.join(parts) or 'definition'}) — "
                             f"re-admit: claude-multi models admit {key}{retarget}")
            continue
        if key in admitted_set and served is not None:
            aliases = line_aliases(line.core_entry)
            count = len(set(aliases) & set(served))
            if count < len(aliases):
                attention.append(f"{key}: {count}/{len(aliases)} aliases served — claude-multi providers apply")
        if grant.get("via") == "migrate" and not smoke_current(evidence, key, line.definition_digest):
            info.append(f"{key}: migrated admission is unverified (no smoke yet) — claude-multi models qualify {key}")

    try:
        rows = displaced_lines(docs, layer, ledger)
    except OperatorError:
        rows = ()
    for row in rows:
        line = layer.displaced.get(row.key) or layer.lines.get(row.key)
        users = [f"session {identifier[:8]}" for identifier in
                 sorted(holders(line_aliases(line.core_entry) if line is not None else ()))]
        users.extend(key_users.get(row.key, ()))
        attention.append(
            f"operator line {row.key} ({row.provider}/{row.wire}) is displaced by catalog "
            f"{', '.join(row.catalog_keys)}{' (admitted)' if row.admitted else ''}: it yields, with no automatic "
            "successor" + (f"; choose a model for {', '.join(users[:5])}" if users else "")
        )

    legacy_doc = legacy or {}
    legacy_present = bool(legacy_doc.get("providers") or legacy_doc.get("models"))
    if marker_error is not None:
        attention.append(f"{PROVIDERS_DIRNAME}/{MIGRATION_MARKER}: {marker_error}")
    if legacy_present and not layer.marker:
        attention.append(f"custom.json: {len(legacy_doc.get('providers', {}))} providers, "
                         f"{len(legacy_doc.get('models', {}))} models not migrated — "
                         "preview: claude-multi providers migrate-custom")
        if snapshot.schemas is not None:
            preflight = migration_preflight(legacy_doc, docs, schemas=snapshot.schemas)
            for problem in preflight.problems:
                attention.append(f"custom.json: migrate-custom would refuse — {_problem_body(problem)}")
            for problem in preflight.skipped_problems:
                attention.append(f"custom.json: unused legacy provider (skipped by migrate-custom) — "
                                 f"{_problem_body(problem)}")
    elif layer.marker and marker_doc is not None and legacy_sha is not None \
            and marker_doc.get("custom_sha256") != legacy_sha:
        attention.append("custom.json changed since migration (it is no longer read) — review: "
                         "claude-multi providers migrate-custom")
    if ledger is not None and ledger.migration is not None:
        attention.append("providers migrate-custom was interrupted — resume: claude-multi providers migrate-custom --apply")

    for text in overlay_conflicts:
        attention.append(f"oauth-extra-models: {text}")

    present = bool(layer.providers or layer.lines or layer.problems or layer.displaced or layer.pending
                   or snapshot.ledger_present)
    if present:
        badges = sum(operator_line_admitted(key, layer=layer, ledger=ledger, admitted_lines=admitted_set)
                     for key in layer.lines)
        usable = agent_eligible if agent_usable is None else agent_usable
        info.append(f"operator: {len(layer.providers)} providers · {len(layer.lines)} lines "
                    f"({badges} admitted, {len(layer.lines) - badges} not admitted, "
                    f"{agent_qualified} qualified, {usable} usable for agents)")
    # A selected transport alternative (never a fallback to the pool).
    missing_secrets = set(unset_secrets)
    for pid, selection in sorted((plan.transports if plan is not None else {}).items()):
        problem = selection.problem
        alternative = selection.alternative
        reviewed = key_route_lines(docs, pid) if alternative is not None and alternative.explicit_models else None
        if problem is None and alternative is not None and alternative.secret_name in missing_secrets:
            # The choice is already made and approved: what is missing is the
            # key itself, which set-key saves (switching again changes nothing).
            problem = f"{alternative.secret_name} is not set — claude-multi providers set-key {pid}"
        if problem is None and reviewed is not None and not reviewed:
            problem = f"no model is reviewed for this route, so nothing of {pid} is served"
        if problem is not None:
            blocks.append(f"provider {pid} transport {selection.choice}: {problem}; its selectors are not served "
                          f"(back to the pool: claude-multi providers transport {pid} {TRANSPORT_POOL})")
        elif alternative is not None:
            info.append(f"provider {pid} transport: {selection.choice} ({alternative.auth_kind} "
                        f"env:{alternative.secret_name} → {alternative.origin}); the {alternative.pool} "
                        "OAuth pool serves nothing while it is selected")
            if reviewed is not None:
                total = sorted(key for key, entry in _docs_lines(docs).items() if entry.get("provider") == pid)
                left = [key for key in total if key not in reviewed]
                info.append(f"provider {pid} transport {selection.choice}: {len(reviewed)} of {len(total)} "
                            f"{pid} model lines are reviewed for this route ({', '.join(reviewed)})"
                            + (f"; not served while it is selected: {', '.join(left)}" if left else ""))
    for key in sorted(layer.lines):
        line = layer.lines[key]
        if line.selector_mode == "migrated" and line.core_entry["context"]["declared_tokens"] >= ONE_MILLION:
            info.append(f"{key}: migrated selector has no [1m]; the client books its default window — "
                        "re-declare to change it")
    return DoctorFindings(tuple(dict.fromkeys(blocks)), tuple(dict.fromkeys(attention)), tuple(dict.fromkeys(info)))


def route_status_text(provider: str, status: str, *, old: str | None = None, new: str | None = None) -> str:
    """E16 diagnostic owner; never a route-policy decision."""
    change = f" (origin {old} → {new})" if status == "changed" and old and new else ""
    return f"provider {provider}: route {status}{change} — claude-multi providers approve {provider}"
