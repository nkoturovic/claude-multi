"""Configuration portability: the closed export document, the pure import
planner and the export receipt.

The export is a semantic projection, never an archive of files: user
profiles (and seed *references*), named bindings, the portable Settings
policy, the operator's ``providers.d`` declarations and **inert** trust
requests (route approvals, admissions, transport choices the source host
held). It never carries a secret value, a key, auth files, records, scopes,
transcripts, logs, qualification evidence, continuity/ledger state,
``native-contract.json`` or host preferences.

Imported trust never becomes a grant: :func:`plan_import` classifies every
item against the TARGET host (its catalog, stores, ledger and validators)
and lists the existing commands that re-establish authority there. Nothing
in this module writes, locks or calls a provider except
:func:`write_output` and :func:`write_receipt` (the explicit ``--out``
path). The command service that applies a confirmed plan through the
ordinary stores lives in ``cli/commands/portability.py``; another surface
calls :func:`load_export`, :func:`plan_import` and that service, never CLI
text.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import assets, errors, state, strict_json
from . import operator as operator_mod
from . import profile as profile_mod
from . import settings as settings_mod
from . import validate as schema_validate

FORMAT = "claude-multi-operator-export"
VERSION = 1
SCHEMA_NAME = "operator-export.schema.json"
RECEIPT_SCHEMA_NAME = "export-receipt.schema.json"
RECEIPT_NAME = "export-receipt.json"
RECEIPT_VERSION = 1
MAX_BYTES = 4 * 1024 * 1024
MAX_PROFILES = 256
MAX_BINDINGS = 256
MAX_PROVIDERS = operator_mod.MAX_PROVIDER_FILES
MAX_TRUST = 1024
REMINDER_DAYS = 30
PORTABLE_SETTINGS = (*settings_mod.POLICY_KEYS, "providers")

EXPORT_SUMMARY = (
    "Export contains operator configuration and re-approval requests.",
    "Excluded: secrets, auth, keys, sessions, transcripts, logs and evidence.",
    "Importing this file will not approve routes or admit models.",
)
PREVIEW_HEADER = "Import preview — no files written"
TRUST_INACTIVE = "Imported trust is not active."
APPLIED_HEADER = "Applied the confirmed configuration changes."
APPLIED_TRUST = "No imported route approval, admission or qualification evidence was trusted."
APPLIED_RERUN = "Rerun import after completing the listed authorization steps."
STALE_PLAN = ("the configuration changed since the preview — nothing written; rerun "
              "claude-multi import FILE to see the new plan")
KEYLESS_TEXT = ("No approval step exists on this host for {provider} {origin} "
                "(interpreted on this machine's network)")
KEYLESS_QUESTION = KEYLESS_TEXT + "; declare it (its aliases are served at once)? [y/N] "
TRANSPORT_TEXT = ("Not transferred: transport choice {provider} {choice} — "
                  "claude-multi providers transport {provider} {choice}")
_STAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_BINDING_NAME = profile_mod.BINDING_NAME
_PENDING_WORDS = ("New · Off", "until admitted", "unknown model", "not agent-eligible")


class PortabilityError(errors.ClaudeMultiError, ValueError):
    """An export or import input was refused (nothing written)."""


# ============================================================ export
@dataclass(frozen=True)
class ProfileSource:
    """One profile name as the source host's store holds it."""

    name: str
    document: Mapping[str, Any] | None  # the user file (parsed); None: no user file
    seed: Mapping[str, Any] | None  # the shipped seed; None: not a seed


def _seed_version(seed: Mapping[str, Any]) -> int | None:
    marker = seed.get("seed")
    version = marker.get("version") if isinstance(marker, Mapping) else None
    return version if isinstance(version, int) and not isinstance(version, bool) else None


def _parsed_seed(name: str, seed: Mapping[str, Any]) -> dict[str, Any]:
    return profile_mod.parse({**copy.deepcopy(dict(seed)), "name": name})


def profile_entry(source: ProfileSource, *, selected_seed: str) -> dict[str, Any] | None:
    """The export entry for one profile (seed rule).

    A user file is exported as its document, except an unmodified installed
    seed, which (like a virtual seed) travels as a ``{id, version}``
    reference that import never writes, so the target keeps its own seed.
    A virtual seed is exported only when it is the selected default."""

    if source.document is not None:
        if source.seed is not None and dict(source.document) == _parsed_seed(source.name, source.seed):
            return {"seed": {"id": source.name, "version": _seed_version(source.seed)}}
        return {"document": copy.deepcopy(dict(source.document))}
    if source.seed is not None and source.name == selected_seed:
        return {"seed": {"id": source.name, "version": _seed_version(source.seed)}}
    return None


def build_export(
    *,
    launcher_version: str,
    catalog_version: int,
    profiles: Iterable[ProfileSource],
    selected_seed: str,
    bindings: Mapping[str, Mapping[str, Any]],
    settings_document: Mapping[str, Any],
    provider_files: Mapping[str, bytes],
    ledger: operator_mod.OperatorLedger | None,
    asset_root: Path | str | None = None,
    reconnect: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """The closed portable document (pure; the caller read every input
    through the ordinary safe stores and refused invalid ones).

    ``reconnect`` names what to connect again on the other computer: the
    API-key names set here and the providers signed in here (names only,
    never a value)."""

    exported_profiles: dict[str, Any] = {}
    for source in sorted(profiles, key=lambda item: item.name):
        entry = profile_entry(source, selected_seed=selected_seed)
        if entry is not None:
            exported_profiles[source.name] = entry
    providers = {file_id: strict_json.loads(raw) for file_id, raw in sorted(provider_files.items())}
    portable = {key: copy.deepcopy(settings_document[key]) for key in PORTABLE_SETTINGS
                if key in settings_document}
    routes = sorted(pid for pid in (ledger.routes if ledger is not None else {}) if pid in providers)
    admissions = sorted(set(ledger.admissions if ledger is not None else {})
                        | set(settings_document.get("admitted_lines", [])))
    transports = dict(sorted((ledger.transport_choices if ledger is not None else {}).items()))
    document = {
        "format": FORMAT,
        "version": VERSION,
        "exported_by": {"launcher_version": launcher_version, "catalog_version": catalog_version},
        "profiles": exported_profiles,
        "bindings": {name: dict(value) for name, value in sorted(bindings.items())},
        "settings": portable,
        "providers": providers,
        "trust_requests": {"routes": routes, "admissions": admissions, "transport_choices": transports},
    }
    if reconnect is not None:
        api_keys = sorted(set(reconnect.get("api_keys", ())))
        sign_ins = sorted(set(reconnect.get("sign_ins", ())))
        if api_keys or sign_ins:
            document["reconnect"] = {"api_keys": api_keys, "sign_ins": sign_ins}
    _check_document(document, asset_root)
    return document


def export_bytes(document: Mapping[str, Any]) -> bytes:
    """The canonical file form (pretty, sorted keys, newline-terminated)."""

    data = strict_json.pretty_file_bytes(document)
    if len(data) > MAX_BYTES:
        raise PortabilityError(f"export refused: the document exceeds {MAX_BYTES} bytes")
    return data


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ============================================================ the closed schema
def schema_path(asset_root: Path | str | None = None) -> Path:
    return (Path(asset_root) if asset_root is not None else assets.root()) / "schemas" / SCHEMA_NAME


def _schema(asset_root: Path | str | None = None) -> dict[str, Any]:
    return strict_json.load(schema_path(asset_root))


def _secret_paths(document: Any) -> list[str]:
    """Token-shaped literals anywhere (shape only: no credential store read)."""

    return operator_mod._secret_hits(document, "$", ())


def _check_document(document: Any, asset_root: Path | str | None = None) -> dict[str, Any]:
    """Closed schema, bounded counts, safe names and each member's own
    store validation (profiles parse, bindings shape, Settings policy)."""

    # Schema and member diagnostics can quote keys and values: screen first.
    hits = _secret_paths(document)
    if hits:
        raise PortabilityError("operator export refused: secret-like literal at " + ", ".join(hits[:6])
                               + " (reference credentials as env:NAME)")
    if not isinstance(document, dict):
        raise PortabilityError("not an operator export: the top level must be an object")
    if document.get("format") != FORMAT:
        raise PortabilityError(f"not an operator export: format must be {FORMAT!r}")
    if document.get("version") != VERSION or isinstance(document.get("version"), bool):
        raise PortabilityError(f"unsupported operator export version {document.get('version')!r} "
                               f"(this launcher reads version {VERSION})")
    problems = schema_validate.validate(document, _schema(asset_root), "$")
    if problems:
        raise PortabilityError("operator export invalid: " + "; ".join(problems[:6]))
    limits = (("profiles", MAX_PROFILES), ("bindings", MAX_BINDINGS), ("providers", MAX_PROVIDERS))
    for member, limit in limits:
        if len(document[member]) > limit:
            raise PortabilityError(f"operator export: more than {limit} {member}")
    trust = document["trust_requests"]
    if sum(len(trust[key]) for key in ("routes", "admissions", "transport_choices")) > MAX_TRUST:
        raise PortabilityError(f"operator export: more than {MAX_TRUST} trust requests")
    for name, entry in document["profiles"].items():
        try:
            state.check_name(name)
        except state.StateError as exc:
            raise PortabilityError(f"operator export: profile name {name!r} is unsafe") from exc
        if "document" in entry:
            try:
                parsed = profile_mod.parse(copy.deepcopy(entry["document"]))
            except profile_mod.ProfileValidationError as exc:
                raise PortabilityError(f"operator export: profile {name}: " + "; ".join(exc.errors[:4])) from exc
            if parsed["name"] != name:
                raise PortabilityError(f"operator export: profile {name} names {parsed['name']!r}")
        elif entry["seed"]["id"] != name:
            raise PortabilityError(f"operator export: seed reference {name} names {entry['seed']['id']!r}")
    bindings_problems = profile_mod._bindings_problems(
        {"version": profile_mod.BINDINGS_DATA_VERSION, "bindings": document["bindings"]})
    if bindings_problems:
        raise PortabilityError("operator export: bindings: " + "; ".join(bindings_problems[:4]))
    try:
        settings_mod._check_shape({"version": settings_mod.SETTINGS_DATA_VERSION, **document["settings"]})
        settings_mod._policy_values(document["settings"], "operator export settings")
    except settings_mod.SettingsError as exc:
        raise PortabilityError(f"operator export: {exc}") from exc
    for file_id in document["providers"]:
        if not operator_mod.PROVIDER_ID.fullmatch(file_id):
            raise PortabilityError(f"operator export: providers.d id {file_id!r} is unsafe")
    for pid in [*trust["routes"], *trust["transport_choices"]]:
        if not operator_mod.PROVIDER_ID.fullmatch(pid):
            raise PortabilityError(f"operator export: provider id {pid!r} is unsafe")
    return document


# ============================================================ import: reading
@dataclass(frozen=True)
class Bundle:
    """A validated portable document and the digest of its bytes."""

    document: dict[str, Any]
    sha256: str

    @property
    def trust(self) -> Mapping[str, Any]:
        return self.document["trust_requests"]


def read_bundle(path: Path | str) -> bytes:
    """Bounded read of the import file: a regular file (or a symlink to one,
    e.g. a read-only store path) owned by the caller or root and not
    group/other writable; never a FIFO or device."""

    try:
        return state.read_owned_readable(Path(path), max_bytes=MAX_BYTES)
    except FileNotFoundError as exc:
        raise PortabilityError(f"import: {path}: no such file") from exc
    except (state.StateError, OSError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        raise PortabilityError(f"import: {path}: refused ({reason})") from exc


def load_export(raw: bytes, *, asset_root: Path | str | None = None) -> Bundle:
    """Strict JSON, the closed schema and every member's own validation
    (pure: no store, no network). Raises :class:`PortabilityError`."""

    if len(raw) > MAX_BYTES:
        raise PortabilityError(f"operator export exceeds {MAX_BYTES} bytes")
    try:
        document = strict_json.loads(raw, strict_json.JSONLimits(max_bytes=MAX_BYTES))
    except strict_json.StrictJSONError as exc:
        raise PortabilityError("operator export is not strict JSON: " + operator_mod._json_failure(exc)) from None
    except (ValueError, RecursionError):
        raise PortabilityError("operator export is not strict JSON: invalid value or excessive nesting") from None
    return Bundle(_check_document(document, asset_root), digest(raw))


# ============================================================ import: planning
APPLY, UNCHANGED, CONFLICT, BLOCKED, REFERENCE = "apply", "unchanged", "conflict", "blocked", "reference"


@dataclass(frozen=True)
class Item:
    kind: str  # provider | setting | binding | profile
    name: str
    status: str  # apply | unchanged | conflict | blocked | reference
    reason: str = ""

    def brief(self) -> str:
        return f"{self.kind} {self.name} ({self.status}" + (f": {self.reason})" if self.reason else ")")

    def line(self) -> str:
        label = {APPLY: "ready", UNCHANGED: "unchanged", CONFLICT: "conflict", BLOCKED: "blocked",
                 REFERENCE: "reference"}[self.status]
        return f"  {label:<9} {self.kind} {self.name}" + (f": {self.reason}" if self.reason else "")


@dataclass(frozen=True)
class ImportTarget:
    """The target host as its ordinary stores read it, plus its validators
    (the same checks the ordinary commands run; injected so the planner
    stays pure)."""

    profiles: Mapping[str, Mapping[str, Any] | None]  # user files (None: unreadable)
    unreadable_profiles: Mapping[str, str]
    seeds: Mapping[str, Mapping[str, Any]]
    bindings: Mapping[str, Mapping[str, Any]]
    settings: Mapping[str, Any]
    provider_files: Mapping[str, bytes]
    provider_refusal: Callable[[str, Mapping[str, Any]], str | None]
    propose: Callable[[Mapping[str, bytes]], operator_mod.OperatorLayer]
    approved_routes: frozenset[str]  # providers whose current route grant is valid here
    admitted: frozenset[str]  # lines admitted here now (current grants)
    transport_choices: Mapping[str, str]
    settings_error: Callable[[Mapping[str, Any]], str | None]
    binding_errors: Callable[[str, Mapping[str, Any], Mapping[str, Mapping[str, Any]], Mapping[str, Any]], list[str]]
    profile_errors: Callable[[Mapping[str, Any], Mapping[str, Mapping[str, Any]], Mapping[str, Any]], list[str]]
    fingerprint: str  # every input above that a commit must find unchanged
    # API-key name -> (provider id, display name) on this host, for the
    # reconnect checklist (names only).
    provider_keys: Mapping[str, tuple[str, str]] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class ImportPlan:
    bundle_sha256: str
    items: tuple[Item, ...]
    providers: dict[str, bytes]  # file id -> bytes to write (new files only)
    keyless: tuple[tuple[str, str], ...]  # (provider id, origin) of eligible keyless declarations
    settings: dict[str, Any]  # the complete Settings document to save (None: unchanged)
    settings_changes: tuple[str, ...]
    provider_enabled: dict[str, bool]
    bindings: dict[str, dict[str, Any]]
    profiles: dict[str, dict[str, Any]]
    routes: tuple[str, ...]  # route approvals still needed here
    admissions: tuple[str, ...]  # admissions still needed here
    transports: tuple[tuple[str, str], ...]  # transport choices not transferred
    target_fingerprint: str
    digest: str = ""
    reconnect: tuple[str, ...] = ()  # what to connect again here (names only)

    @property
    def has_changes(self) -> bool:
        return bool(self.providers or self.settings_changes or self.bindings or self.profiles)

    def count(self, kind: str, status: str) -> int:
        return sum(1 for item in self.items if item.kind == kind and item.status == status)

    def summary_lines(self) -> list[str]:
        def counts(kind: str, labels: Sequence[tuple[str, str]]) -> str:
            return ", ".join(f"{self.count(kind, status)} {label}" for status, label in labels)

        pending = sum(1 for item in self.items if item.kind == "profile" and item.status == BLOCKED
                      and item.reason.startswith("pending"))
        return [
            "  providers: " + counts("provider", ((APPLY, "new"), (UNCHANGED, "unchanged"),
                                                  (CONFLICT, "conflicting"), (BLOCKED, "blocked"))),
            f"  profiles: {self.count('profile', APPLY)} ready, {pending} blocked pending admission, "
            + counts("profile", ((UNCHANGED, "unchanged"), (CONFLICT, "conflicting")))
            + f", {self.count('profile', BLOCKED) - pending} invalid here, "
            + f"{self.count('profile', REFERENCE)} seed references",
            "  bindings: " + counts("binding", ((APPLY, "ready"), (UNCHANGED, "unchanged"),
                                                (CONFLICT, "conflicting"), (BLOCKED, "blocked"))),
            f"  settings: {len(self.settings_changes)} changes, {self.count('setting', BLOCKED)} blocked",
            f"  route re-approvals required: {len(self.routes)}",
            f"  model admissions required: {len(self.admissions)}",
        ]

    def trust_lines(self) -> list[str]:
        lines = [KEYLESS_TEXT.format(provider=pid, origin=origin) for pid, origin in self.keyless]
        lines += [TRANSPORT_TEXT.format(provider=pid, choice=choice) for pid, choice in self.transports]
        lines.append(TRUST_INACTIVE)
        lines += [f"  claude-multi providers approve {pid}" for pid in self.routes]
        lines += [f"  claude-multi models admit {key}" for key in self.admissions]
        return lines

    def preview_lines(self) -> list[str]:
        from claude_multi.setup import texts as setup_texts

        lines = [PREVIEW_HEADER, *self.summary_lines()]
        shown = [item.line() for item in self.items if item.status != UNCHANGED]
        if shown:
            lines += ["", *shown]
        lines = [*lines, "", *self.trust_lines()]
        if self.reconnect:
            lines.append(setup_texts.IMPORT_RECONNECT.format(items="; ".join(self.reconnect)))
        return lines


def reconnect_items(reconnect: Any, provider_keys: Mapping[str, tuple[str, str]]) -> tuple[str, ...]:
    """The reconnect checklist of an export, in this host's words."""

    from claude_multi import account_pools
    from claude_multi.setup import texts as setup_texts

    if not isinstance(reconnect, Mapping):
        return ()
    items: list[str] = []
    for name in reconnect.get("api_keys", ()):
        known = provider_keys.get(name)
        if known is None:
            items.append(setup_texts.RECONNECT_UNKNOWN_KEY.format(name=name))
        else:
            items.append(setup_texts.RECONNECT_KEY.format(display=known[1], id=known[0]))
    pools = {pool.provider: name for name, pool in account_pools.pools().items()}
    for provider_id in reconnect.get("sign_ins", ()):
        kind = setup_texts.ACCOUNT_KINDS.get(pools.get(provider_id, ""), f"{provider_id} account")
        items.append(setup_texts.RECONNECT_SIGNIN.format(kind=kind, id=provider_id))
    return tuple(items)


def _canonical(value: Any) -> str:
    return hashlib.sha256(strict_json.canonical_bytes(value)).hexdigest()


def _is_pending(errors_: Iterable[str]) -> bool:
    return any(word in error for error in errors_ for word in _PENDING_WORDS)


def _settings_default(key: str) -> Any:
    return {
        settings_mod.COMPACTION_PERCENT_KEY: settings_mod.COMPACTION_PERCENT_DEFAULT,
        settings_mod.EXPLORE_INHERIT_CAP_DISABLED_KEY: settings_mod.EXPLORE_INHERIT_CAP_DISABLED_DEFAULT,
        settings_mod.REVIEW_ROUND_CAP_KEY: settings_mod.REVIEW_ROUND_CAP_DEFAULT,
        settings_mod.WORKFLOW_DEFAULT_BINDING_KEY: None,
    }[key]


def plan_import(bundle: Bundle, target: ImportTarget, *, excluded: Iterable[str] = ()) -> ImportPlan:
    """Classify every portable item against the target (pure).

    ``unchanged`` and ``conflict`` items are never written (no force);
    ``blocked`` items wait for target-host authority or are invalid here;
    ``apply`` items form the exact write set an ``--apply`` confirmation
    covers. ``excluded`` names providers the operator declined (keyless
    confirmations). Trust requests only produce the target's own commands."""

    doc = bundle.document
    excluded = frozenset(excluded)
    items: list[Item] = []

    # -- providers.d declarations: new files only, validated as a whole layer.
    candidates: dict[str, bytes] = {}
    for file_id, declaration in doc["providers"].items():
        raw = operator_mod.document_bytes(declaration)
        current = target.provider_files.get(file_id)
        if current is not None:
            try:
                same = strict_json.loads(current) == declaration
            except (strict_json.StrictJSONError, ValueError, RecursionError):
                same = False
            items.append(Item("provider", file_id, UNCHANGED) if same else Item(
                "provider", file_id, CONFLICT,
                f"{operator_mod.file_label(file_id)} differs on this host — not overwritten; compare and "
                f"edit it: claude-multi providers edit {file_id}"))
            continue
        if file_id in excluded:
            items.append(Item("provider", file_id, BLOCKED, "declined at the keyless confirmation"))
            continue
        refusal = target.provider_refusal(file_id, declaration)
        if refusal is not None:
            items.append(Item("provider", file_id, BLOCKED, refusal))
            continue
        candidates[file_id] = raw
    layer = target.propose(candidates)
    for _round in range(len(candidates) + 1):
        failing = {fid for fid in candidates if operator_mod.file_problems(layer, fid)}
        if not failing:
            break
        for fid in sorted(failing):
            problems = operator_mod.file_problems(layer, fid)
            items.append(Item("provider", fid, BLOCKED, "invalid on this host: "
                              + "; ".join(problem.text() for problem in problems[:3])))
            candidates.pop(fid)
        layer = target.propose(candidates)
    if operator_mod.blocking_problems(layer) and candidates:
        # A problem outside the imported files (the target's own layer) blocks
        # every declaration: no silently partial render.
        reason = "the operator layer on this host has problems: " + "; ".join(
            problem.text() for problem in operator_mod.blocking_problems(layer)[:3])
        items += [Item("provider", fid, BLOCKED, reason) for fid in sorted(candidates)]
        candidates = {}
        layer = target.propose({})
    keyless: list[tuple[str, str]] = []
    for fid in sorted(candidates):
        items.append(Item("provider", fid, APPLY))
        provider = layer.providers.get(fid)
        if provider is not None and provider.auth_kind == "none":
            keyless.append((fid, provider.origin))
    declared = set(layer.lines)

    # -- trust: the target's own commands, never a grant.
    routes = tuple(sorted(
        pid for pid, provider in layer.providers.items()
        if provider.auth_kind != "none" and pid not in target.approved_routes
        and (pid in candidates or pid in doc["providers"] or pid in bundle.trust["routes"])))
    admissions = tuple(sorted(
        key for key in bundle.trust["admissions"] if key not in target.admitted))
    transports = tuple(sorted(
        (pid, choice) for pid, choice in bundle.trust["transport_choices"].items()
        if target.transport_choices.get(pid) != choice))

    # -- Settings policy and provider enablement (an authority change).
    merged = copy.deepcopy(dict(target.settings))
    changes: list[str] = []
    enabled: dict[str, bool] = {}
    for key in settings_mod.POLICY_KEYS:
        if key not in doc["settings"]:
            continue
        wanted, have = doc["settings"][key], target.settings.get(key, _settings_default(key))
        if wanted == have:
            items.append(Item("setting", key, UNCHANGED))
            continue
        trial = {**merged, key: copy.deepcopy(wanted)}
        problem = target.settings_error(trial)
        if problem is not None:
            items.append(Item("setting", key, BLOCKED, ("pending admission — " if _is_pending([problem]) else "")
                              + problem))
            continue
        merged = trial
        changes.append(key)
        items.append(Item("setting", key, APPLY, f"{json.dumps(have)} → {json.dumps(wanted)}"))
    for pid, value in sorted(doc["settings"].get("providers", {}).items()):
        wanted = bool(value.get("enabled", True))
        have = bool(target.settings.get("providers", {}).get(pid, {}).get("enabled", True))
        name = f"provider {pid} enabled"
        if wanted == have:
            items.append(Item("setting", name, UNCHANGED))
            continue
        trial = copy.deepcopy(merged)
        trial.setdefault("providers", {})[pid] = {"enabled": wanted}
        problem = target.settings_error(trial)
        if problem is not None:
            items.append(Item("setting", name, BLOCKED, problem))
            continue
        merged = trial
        changes.append(name)
        enabled[pid] = wanted
        items.append(Item("setting", name, APPLY, f"{'on' if have else 'off'} → {'on' if wanted else 'off'}"))

    # -- named bindings: new names only; the ordinary binding checks.
    bindings = {name: dict(value) for name, value in target.bindings.items()}
    added: dict[str, dict[str, Any]] = {}
    for name, value in sorted(doc["bindings"].items()):
        have = target.bindings.get(name)
        if have is not None:
            items.append(Item("binding", name, UNCHANGED) if dict(have) == dict(value) else Item(
                "binding", name, CONFLICT, "differs on this host — not overwritten "
                "(change it in the profile editor or Settings)"))
            continue
        problems = target.binding_errors(name, value, {**bindings, **added, name: dict(value)}, merged)
        if problems:
            pending = _is_pending(problems) or value.get("model") in declared
            items.append(Item("binding", name, BLOCKED, ("pending admission — " if pending else "invalid here — ")
                              + problems[0]))
            continue
        added[name] = dict(value)
        items.append(Item("binding", name, APPLY))
    bindings.update(added)

    # -- profiles: documents absent here (a virtual seed may be shadowed by
    # the source's user file); seed references are never written.
    profiles: dict[str, dict[str, Any]] = {}
    for name, entry in sorted(doc["profiles"].items()):
        if "seed" in entry:
            here = target.seeds.get(name)
            source_version = entry["seed"]["version"]
            if here is None:
                items.append(Item("profile", name, BLOCKED, f"seed {name} (version {source_version}) "
                                  "is not shipped by this host's catalog"))
            else:
                items.append(Item("profile", name, REFERENCE, f"seed reference (source version "
                                  f"{source_version}; this host ships version {_seed_version(here)}) — "
                                  "never written; this host's seed is used"))
            continue
        document = profile_mod.parse(copy.deepcopy(entry["document"]))
        if name in target.unreadable_profiles:
            items.append(Item("profile", name, CONFLICT, f"unreadable on this host "
                              f"({target.unreadable_profiles[name]}) — not overwritten"))
            continue
        have = target.profiles.get(name)
        if have is None and name in target.seeds and _parsed_seed(name, target.seeds[name]) == document:
            items.append(Item("profile", name, UNCHANGED))  # equals this host's own seed
            continue
        if have is not None:
            items.append(Item("profile", name, UNCHANGED) if dict(have) == document else Item(
                "profile", name, CONFLICT, "differs on this host — not overwritten; edit it: "
                f"claude-multi profile edit {name}"))
            continue
        problems = target.profile_errors(document, bindings, merged)
        if problems:
            pending = _is_pending(problems) or any(key in error for error in problems for key in declared)
            items.append(Item("profile", name, BLOCKED, ("pending admission — " if pending else "invalid here — ")
                              + problems[0]))
            continue
        profiles[name] = document
        items.append(Item("profile", name, APPLY))

    order = {"provider": 0, "setting": 1, "binding": 2, "profile": 3}
    plan = ImportPlan(
        bundle_sha256=bundle.sha256,
        items=tuple(sorted(items, key=lambda item: (order[item.kind], item.name))),
        providers={fid: candidates[fid] for fid in sorted(candidates)},
        keyless=tuple(keyless),
        settings=merged if changes else {},
        settings_changes=tuple(changes),
        provider_enabled=enabled,
        bindings=added,
        profiles=profiles,
        routes=routes,
        admissions=admissions,
        transports=transports,
        target_fingerprint=target.fingerprint,
        reconnect=reconnect_items(bundle.document.get("reconnect"), target.provider_keys),
    )
    body = {
        "bundle": plan.bundle_sha256, "target": plan.target_fingerprint,
        "providers": {fid: digest(raw) for fid, raw in plan.providers.items()},
        "settings": plan.settings, "bindings": plan.bindings, "profiles": plan.profiles,
        "items": [[item.kind, item.name, item.status] for item in plan.items],
    }
    return _with_digest(plan, _canonical(body))


def _with_digest(plan: ImportPlan, value: str) -> ImportPlan:
    return dataclasses.replace(plan, digest=value)


def target_fingerprint(*parts: Any) -> str:
    """A digest over the target inputs a confirmed plan depends on."""

    return _canonical(list(parts))


# ============================================================ --out and the receipt
def forbidden_roots(environ: Mapping[str, str], home: Path) -> tuple[Path, ...]:
    """Where an export file never goes: the launcher's state, config and
    gateway roots, the native client's ``~/.claude`` and every credential
    location (``secret_store.credential_locations``: the key-file folders,
    the account sign-ins, a key file the environment selects elsewhere)."""

    from . import paths, secret_store

    env = dict(environ)
    roots = [paths.state_root(env), paths.config_root(env), paths.gateway_config_dir({**env, "HOME": str(home)}),
             home / ".claude"]
    configured = env.get("CLAUDE_CONFIG_DIR")
    if configured:
        roots.append(Path(configured))
    roots.extend(secret_store.credential_locations({**env, "HOME": str(home)}))
    return tuple(dict.fromkeys(roots))


def _within(path: str, root: Path) -> bool:
    base = os.path.realpath(root)
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def check_output_path(path: Path | str, roots: Iterable[Path]) -> Path:
    """The explicit ``--out`` target: an absent or regular file (never a
    symlink, FIFO or directory) in an existing directory outside ``roots``."""

    target = Path(os.path.abspath(path))
    if target.name in ("", ".", ".."):
        raise PortabilityError(f"export --out {path}: not a file path")
    if os.path.islink(target):
        raise PortabilityError(f"export --out {path}: refusing a symlink")
    if os.path.lexists(target) and not os.path.isfile(target):
        raise PortabilityError(f"export --out {path}: not a regular file")
    parent = os.path.realpath(target.parent)
    if not os.path.isdir(parent):
        raise PortabilityError(f"export --out {path}: the directory does not exist")
    resolved = os.path.join(parent, target.name)
    for root in roots:
        if _within(resolved, root) or _within(str(target), root):
            raise PortabilityError(f"export --out {path}: refusing a path under {root} "
                                   "(claude-multi state/config/gateway, a credential location or the native "
                                   "~/.claude)")
    return Path(resolved)


def write_output(target: Path, data: bytes) -> None:
    """Same-directory atomic write (0600, file and directory fsync); a failure
    before the rename leaves any previous file untouched and raises."""

    descriptor, temporary = None, target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.islink(target):
            raise PortabilityError(f"export --out {target}: became a symlink")
        os.replace(temporary, target)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@dataclass(frozen=True)
class Receipt:
    exported_at: str
    sha256: str


def receipt_path(state_root: Path | str) -> Path:
    return Path(state_root) / RECEIPT_NAME


def receipt_document(exported_at: str, sha: str) -> dict[str, Any]:
    return {"version": RECEIPT_VERSION, "exported_at": exported_at, "sha256": sha}


def _receipt_schema(asset_root: Path | str | None = None) -> dict[str, Any]:
    return strict_json.load((Path(asset_root) if asset_root is not None else assets.root())
                            / "schemas" / RECEIPT_SCHEMA_NAME)


def write_receipt(state_root: Path | str, sha: str, *, now: datetime.datetime | None = None,
                  asset_root: Path | str | None = None) -> Receipt:
    """The confirmed-export receipt (time + digest only; closed schema, 0600).
    Called only after the export file was written and fsynced."""

    stamp = operator_mod.utc_stamp(now)
    document = receipt_document(stamp, sha)
    problems = schema_validate.validate(document, _receipt_schema(asset_root), "$")
    if problems:
        raise PortabilityError("export receipt invalid: " + "; ".join(problems))
    root = state.ensure_private_dir(state_root)
    state.atomic_write(receipt_path(root), strict_json.canonical_file_bytes(document))
    return Receipt(stamp, sha)


def read_receipt(state_root: Path | str, *, asset_root: Path | str | None = None) -> Receipt | None:
    """The receipt, None when absent; :class:`PortabilityError` when unusable
    (never repaired here)."""

    path = receipt_path(state_root)
    if not os.path.lexists(path):
        return None
    try:
        document = strict_json.loads(state.read_private(path))
    except (state.StateError, OSError, strict_json.StrictJSONError, ValueError, RecursionError) as exc:
        raise PortabilityError(f"export receipt {path} is unreadable") from exc
    if schema_validate.validate(document, _receipt_schema(asset_root), "$") or not _STAMP.match(
            document.get("exported_at", "")):
        raise PortabilityError(f"export receipt {path} is invalid")
    return Receipt(document["exported_at"], document["sha256"])


def receipt_info_line(receipt: Receipt | None, *, now: datetime.datetime) -> str:
    """Doctor Info (the reminder starts after :data:`REMINDER_DAYS`)."""

    if receipt is None:
        return "Operator state: no confirmed file export recorded."
    moment = datetime.datetime.strptime(receipt.exported_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)
    days = max(0, (now - moment).days)
    text = f"Operator state: last confirmed export {days} days ago"
    return text + (" — claude-multi export --out FILE" if days > REMINDER_DAYS else ".")


def receipt_error_line(error: PortabilityError) -> str:
    return f"Operator state: {error}; no confirmed file export recorded — claude-multi export --out FILE"


__all__ = [
    "APPLIED_HEADER", "APPLIED_RERUN", "APPLIED_TRUST", "Bundle", "EXPORT_SUMMARY", "FORMAT", "ImportPlan",
    "ImportTarget", "Item", "KEYLESS_QUESTION", "MAX_BYTES", "PortabilityError", "ProfileSource", "Receipt",
    "STALE_PLAN", "VERSION", "build_export", "check_output_path", "digest", "export_bytes", "forbidden_roots",
    "load_export", "plan_import", "read_bundle", "read_receipt",
    "receipt_info_line", "target_fingerprint", "write_output", "write_receipt",
]
