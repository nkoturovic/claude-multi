"""Gateway continuity aliases for retired selectors.

A retired catalog line leaves selectors behind in live sessions, their
records and compiled scopes. The gateway keeps serving them as continuity
aliases (never offered for new launches): ``~/.config/claude-multi/
continuity.json`` persists the set, the renderer merges it after the
catalog (the catalog wins every collision), and entries leave only through
``claude-multi doctor --prune-aliases``.

Lifecycle: a seed watermark plus a tombstone map. ``merge_seed``
adds each retired selector once per catalog version (never a tombstoned
one); ``extend_from_records`` adds retired selectors that session records
name, reviving a tombstone only for a live record; ``apply_prune`` removes
and tombstones. The file is HOME-relative like the gateway token
(``proxy.config_dir``), never ``XDG_CONFIG_HOME``-relative, and is written
only while the caller holds the api-key ``FileLock`` (a leaf lock).

This module never imports ``proxy`` or ``sessions``: record scanning is
read-only and lock-free (no ``SessionStore``, no state-marker check), and a
bad record never raises.
"""

from __future__ import annotations

from . import assets

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import catalog as catalog_mod
from . import errors
from . import render as render_mod
from . import state, strict_json
from . import validate as schema_validate

CONTINUITY_VERSION = 1
FILE_NAME = "continuity.json"

# Copied from sessions.UUID4 (this module must not import sessions).
_RECORD_STEM = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SELECTOR = re.compile(r"^[a-z0-9][a-z0-9._-]*(\[1m\])?$")
_ALIAS_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SUFFIX = "[1m]"


class ContinuityError(errors.ClaudeMultiError, ValueError):
    """The persisted continuity set is unreadable or invalid."""


def path(home: Path) -> Path:
    """``<home>/.config/claude-multi/continuity.json`` (HOME-relative)."""

    return Path(home) / ".config" / "claude-multi" / FILE_NAME


def empty() -> dict[str, Any]:
    return {
        "version": CONTINUITY_VERSION,
        "seeded_through_catalog": 0,
        "state_root": None,
        "aliases": {},
        "pruned": {},
    }


def _schema() -> dict[str, Any]:
    return strict_json.load(
        assets.root() / "schemas" / "continuity.schema.json"
    )


def _key_problems(document: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for section in ("aliases", "pruned"):
        for key in document[section]:
            if not _ALIAS_KEY.fullmatch(key):
                problems.append(f"{section}: invalid alias {key!r} (a selector base, no [1m])")
            elif key.startswith(render_mod.SENTINEL_PREFIX):
                problems.append(f"{section}: alias {key!r} uses the reserved render-sentinel prefix")
    return problems


def validate_document(document: Any) -> None:
    """Schema + key checks; raises ContinuityError naming the problems."""

    problems = schema_validate.validate(document, _schema(), "$")
    if not problems:
        problems = _key_problems(document)
    if problems:
        raise ContinuityError("; ".join(problems[:3]))


def read(home: Path) -> dict[str, Any] | None:
    """The persisted set, or None when absent; ContinuityError if unusable.

    Symlinks, group/other-readable modes, malformed JSON (duplicate keys
    included), schema or key violations are all refused.
    """

    target = path(home)
    try:
        target.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ContinuityError(f"{FILE_NAME}: {exc.strerror or exc}") from None
    try:
        document = strict_json.loads(state.read_private(target))
    except state.StateError as exc:
        raise ContinuityError(f"{FILE_NAME}: {exc.strerror or exc}") from None
    except (OSError, ValueError) as exc:
        raise ContinuityError(f"{FILE_NAME}: {exc}") from None
    except RecursionError:
        # json's C scanner recurses per nesting level: a deeply nested file
        # fails before strict_json's depth limit is checked.
        raise ContinuityError(f"{FILE_NAME}: nesting too deep") from None
    except Exception as exc:  # any other parse failure: never a traceback
        raise ContinuityError(f"{FILE_NAME}: unparsable ({type(exc).__name__})") from None
    try:
        validate_document(document)
    except ContinuityError as exc:
        raise ContinuityError(f"{FILE_NAME}: {exc}") from None
    except Exception as exc:
        raise ContinuityError(f"{FILE_NAME}: invalid ({type(exc).__name__})") from None
    return document


def write(home: Path, document: Mapping[str, Any]) -> None:
    """Atomic 0600 pretty write. The caller holds the api-key FileLock."""

    validate_document(document)
    state.atomic_write(path(home), strict_json.pretty_file_bytes(document))


def _catalog_version(catalog: catalog_mod.Catalog) -> int:
    return int(catalog.docs["version"]["catalog_version"])


def _seed_entry(key: str, entry: Mapping[str, Any], contract: str | None, source: str) -> dict[str, Any]:
    return {
        "provider": entry["provider"],
        "wire": entry["last_wire"],
        "proxy_contract": contract,
        "display": entry["display"],
        "context_tokens": entry["context_tokens"],
        "source": source,
        "since_catalog": entry["since_catalog"],
    }


def seed_entries(catalog: catalog_mod.Catalog) -> dict[str, dict[str, Any]]:
    """Every retired selector base of the installed catalog, seed-shaped."""

    version = _catalog_version(catalog)
    seeds: dict[str, dict[str, Any]] = {}
    for base, (key, entry, contract) in sorted(catalog.retired_selector_index().items()):
        if entry["since_catalog"] > version:
            continue
        seeds[base] = _seed_entry(key, entry, contract, f"seed:{key}")
    return seeds


def seed_only(catalog: catalog_mod.Catalog) -> dict[str, Any]:
    """Pure: the empty set plus every seed, watermark = catalog_version.

    Doctor's expectation while ``continuity.json`` is absent, and the single
    fallback both render and doctor use while it is corrupt.
    """

    document = empty()
    document["aliases"] = seed_entries(catalog)
    document["seeded_through_catalog"] = _catalog_version(catalog)
    return document


def merge_seed(document: Mapping[str, Any], catalog: catalog_mod.Catalog) -> tuple[dict[str, Any], bool]:
    """Seed a newer catalog's retired selectors once; tombstones stay out.

    Never overwrites an existing alias entry. Returns ``(new_doc, changed)``.
    """

    result = copy.deepcopy(dict(document))
    version = _catalog_version(catalog)
    if result["seeded_through_catalog"] >= version:
        return result, False
    for base, entry in seed_entries(catalog).items():
        if base in result["aliases"] or base in result["pruned"]:
            continue
        result["aliases"][base] = entry
    result["seeded_through_catalog"] = version
    return result, True


@dataclass(frozen=True)
class RecordScan:
    """What session records name (read-only snapshot, never raises).

    ``refs`` maps a selector base to the record ids naming it; ``live`` is
    every readable record whose last event is not ``end`` (a missing event
    counts as live: pre-v3 and never-hooked records stay live until the
    operator runs ``claude-multi sessions forget`` or the session ends);
    ``unreadable`` lists record ids that could not be read at all.
    """

    refs: dict[str, frozenset[str]]
    live: frozenset[str]
    unreadable: tuple[str, ...]
    notices: tuple[str, ...]
    # Set when ``<state_root>/sessions`` exists but cannot be listed (EACCES,
    # not a directory, ...): no record is known, so liveness is unknown.
    directory_error: str | None = None
    # Record ids whose compiled scope fence exists but cannot be
    # read. A notice for renders; destructive plans treat them as unknown.
    fence_unreadable: tuple[str, ...] = ()
    # Record id -> selector base -> the slots naming it (lead, an
    # agent role, observed, fence), for the served-change live-impact list.
    slots: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)


def _selector_bases(values: Iterable[Any]) -> set[str]:
    bases: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not _SELECTOR.fullmatch(value):
            continue
        base = value[: -len(_SUFFIX)] if value.endswith(_SUFFIX) else value
        if base.startswith(render_mod.SENTINEL_PREFIX):
            continue
        bases.add(base)
    return bases


def _record_selectors(record: Mapping[str, Any]) -> list[Any]:
    """Selector fields of any record shape; every lookup is optional.

    Managed records carry ``snapshot.lead.client_selector`` and
    ``snapshot.variants[].client_selector``; ordinary records have no
    snapshot (their ``ordinary_model`` is a catalog key, not a selector) and
    are covered by ``observed_model`` plus their compiled scope settings.
    Version-4 records carry ``applied.lead.selector`` (null for a
    migrated ordinary lead), ``applied.agents[].selector`` and the optional
    ``lead_target``/``scope_lead`` selectors.
    """

    values: list[Any] = []
    applied = record.get("applied")
    if isinstance(applied, Mapping):
        lead = applied.get("lead")
        if isinstance(lead, Mapping):
            values.append(lead.get("selector"))
        agents = applied.get("agents")
        if isinstance(agents, Mapping):
            for binding in agents.values():
                if isinstance(binding, Mapping):
                    values.append(binding.get("selector"))
    for key in ("lead_target", "scope_lead"):
        reference = record.get(key)
        if isinstance(reference, Mapping):
            values.append(reference.get("selector"))
    snapshot = record.get("snapshot")
    if isinstance(snapshot, Mapping):
        lead = snapshot.get("lead")
        if isinstance(lead, Mapping):
            values.append(lead.get("client_selector"))
        variants = snapshot.get("variants")
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, Mapping):
                    values.append(variant.get("client_selector"))
    values.append(record.get("observed_model"))
    return values


def _record_slots(record: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """(slot label, selector) pairs of :func:`_record_selectors`' fields."""

    pairs: list[tuple[str, Any]] = []
    applied = record.get("applied")
    if isinstance(applied, Mapping):
        lead = applied.get("lead")
        if isinstance(lead, Mapping):
            pairs.append(("lead", lead.get("selector")))
        agents = applied.get("agents")
        if isinstance(agents, Mapping):
            for role, binding in sorted(agents.items()):
                if isinstance(binding, Mapping):
                    pairs.append((str(role), binding.get("selector")))
    for key in ("lead_target", "scope_lead"):
        reference = record.get(key)
        if isinstance(reference, Mapping):
            pairs.append(("lead", reference.get("selector")))
    snapshot = record.get("snapshot")
    if isinstance(snapshot, Mapping):
        lead = snapshot.get("lead")
        if isinstance(lead, Mapping):
            pairs.append(("lead", lead.get("client_selector")))
        variants = snapshot.get("variants")
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, Mapping):
                    pairs.append(("agent", variant.get("client_selector")))
    pairs.append(("observed", record.get("observed_model")))
    return pairs


def _scope_selectors(settings: Any) -> list[Any] | None:
    """The compiled fence's selectors, or None when its structure is not a
    compiled shape: every scope (legacy or lineup-enabled) is an object whose
    ``availableModels``, when present, is a list of strings and whose
    ``model``, when present, is a string. Anything else is an unknown fence,
    never an empty one."""

    if not isinstance(settings, Mapping):
        return None
    values: list[Any] = []
    available = settings.get("availableModels", [])
    if not isinstance(available, list) or not all(isinstance(item, str) for item in available):
        return None
    values.extend(available)
    model = settings.get("model")
    if "model" in settings and not isinstance(model, str):
        return None
    values.append(model)
    return values


def scan_records(state_root: Path) -> RecordScan:
    """Read-only, lock-free scan of ``<state_root>/sessions/*.json``.

    Only a read, parse or non-object failure makes a record unreadable
    (notice names the 8-character id prefix only). Selector fields are
    optional; the compiled scope ``settings.json`` (``availableModels`` and
    ``model``) is read when present; an unreadable or structurally malformed
    one is a notice plus ``fence_unreadable`` (unknown coverage).
    A missing sessions directory is an empty scan; one that exists but
    cannot be listed sets ``directory_error`` (glob would silently yield
    nothing on EACCES, which would read as "no live session").
    """

    root = Path(state_root)
    refs: dict[str, set[str]] = {}
    live: set[str] = set()
    unreadable: list[str] = []
    notices: list[str] = []
    fences: list[str] = []
    slots: dict[str, dict[str, set[str]]] = {}
    sessions_dir = root / "sessions"
    try:
        names = os.listdir(sessions_dir)
    except FileNotFoundError:
        names = []
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        return RecordScan(
            refs={}, live=frozenset(), unreadable=(),
            notices=(f"sessions directory unreadable ({reason}): records not scanned",),
            directory_error=reason,
        )
    candidates = sorted(sessions_dir / name for name in names if name.endswith(".json"))
    for record_path in candidates:
        stem = record_path.stem
        if not _RECORD_STEM.fullmatch(stem):
            continue
        try:
            record = strict_json.loads(state.read_private(record_path))
        except Exception:
            record = None
        if not isinstance(record, dict):
            unreadable.append(stem)
            notices.append(f"record {stem[:8]}: unreadable, skipped")
            continue
        values = _record_selectors(record)
        record_slots = slots.setdefault(stem, {})
        for label, value in _record_slots(record):
            for base in _selector_bases([value]):
                record_slots.setdefault(base, set()).add(label)
        settings_path = root / "scopes" / stem / "settings.json"
        try:
            settings_path.lstat()
            present = True
        except (FileNotFoundError, NotADirectoryError):
            present = False
        except OSError:
            present = True  # stat refused (EACCES): report it, never skip silently
        if present:
            try:
                settings = strict_json.loads(state.read_private(settings_path))
            except Exception:
                notices.append(f"record {stem[:8]}: scope settings unreadable, skipped")
                fences.append(stem)
            else:
                fenced = _scope_selectors(settings)
                if fenced is None:
                    notices.append(f"record {stem[:8]}: scope settings malformed (fence unknown), skipped")
                    fences.append(stem)
                else:
                    values.extend(fenced)
                    for base in _selector_bases(fenced):
                        record_slots.setdefault(base, set()).add("fence")
        for base in _selector_bases(values):
            refs.setdefault(base, set()).add(stem)
        if record.get("last_event_source") != "end":
            live.add(stem)
    return RecordScan(
        refs={base: frozenset(ids) for base, ids in sorted(refs.items())},
        live=frozenset(live),
        unreadable=tuple(unreadable),
        notices=tuple(notices),
        fence_unreadable=tuple(fences),
        slots={stem: {base: tuple(sorted(labels)) for base, labels in sorted(found.items())}
               for stem, found in sorted(slots.items())},
    )


def extend_from_records(
    document: Mapping[str, Any],
    catalog: catalog_mod.Catalog,
    scan: RecordScan,
    catalog_aliases: frozenset[str],
) -> tuple[dict[str, Any], bool, tuple[str, ...]]:
    """Add retired selectors that records still name.

    Catalog, custom and route aliases are served by the catalog already;
    known continuity aliases stay as they are; a tombstoned alias is revived
    only for a live referencing record. Anything else is ``unservable``:
    reported, never guessed, never stored. Returns ``(doc, changed,
    unservable_bases)``.
    """

    result = copy.deepcopy(dict(document))
    index = catalog.retired_selector_index()
    changed = False
    unservable: list[str] = []
    for base in sorted(scan.refs):
        if base in catalog_aliases or base in result["aliases"]:
            continue
        found = index.get(base)
        if found is None:
            unservable.append(base)
            continue
        if base in result["pruned"] and not (scan.refs[base] & scan.live):
            continue
        key, entry, contract = found
        result["aliases"][base] = _seed_entry(key, entry, contract, "record")
        result["pruned"].pop(base, None)
        changed = True
    return result, changed, tuple(unservable)


CATALOG_SERVED = "still served as a catalog route/live selector"


@dataclass(frozen=True)
class PrunePlan:
    """``remove``: aliases to prune; ``keep``: (alias, reason); ``refusal``;
    ``holders``: the live records behind a refusal (full ids, so the remedy
    can name them)."""

    remove: tuple[str, ...]
    keep: tuple[tuple[str, str], ...]
    refusal: str | None = None
    holders: tuple[str, ...] = ()


def _ids8(ids: Iterable[str]) -> str:
    return ", ".join(sorted(identifier[:8] for identifier in ids))


def plan_prune(
    document: Mapping[str, Any],
    catalog: catalog_mod.Catalog,
    scan: RecordScan,
    requested: frozenset[str] | None,
    *,
    served_by_catalog: frozenset[str] = frozenset(),
) -> PrunePlan:
    """Which continuity aliases may go (records are never modified).

    Candidates are the ``requested`` bases (``[1m]`` stripped) or every
    alias. Refusal-fallback targets are always kept; an alias a live record
    names is kept in all-mode and refuses the whole operation when named.
    References by ended records do not keep an alias. In all-mode an alias
    in ``served_by_catalog`` (a catalog selector base or passthrough route
    name, ``render.catalog_alias_set``) is kept: the render never emits it
    from continuity, so pruning it would change nothing served and only
    tombstone it. Naming one explicitly still prunes it.
    """

    aliases = document["aliases"]
    if requested:
        candidates = sorted(
            {name[: -len(_SUFFIX)] if name.endswith(_SUFFIX) else name for name in requested}
        )
        unknown = [name for name in candidates if name not in aliases]
        if unknown:
            return PrunePlan((), (), f"unknown continuity alias(es): {', '.join(unknown)}")
    else:
        candidates = sorted(aliases)
    remove: list[str] = []
    keep: list[tuple[str, str]] = []
    blocked: list[str] = []
    blocking: set[str] = set()
    for alias in candidates:
        if alias in catalog_mod.REFUSAL_FALLBACK_WIRES:
            keep.append((alias, "refusal-fallback target"))
            continue
        holders = scan.refs.get(alias, frozenset()) & scan.live
        if holders:
            if requested:
                blocked.append(f"{alias} -> {_ids8(holders)}")
                blocking |= holders
            else:
                keep.append((alias, f"live session {_ids8(holders)}"))
            continue
        if not requested and alias in served_by_catalog:
            keep.append((alias, CATALOG_SERVED))
            continue
        remove.append(alias)
    if blocked:
        return PrunePlan(
            (), (), "referenced by live session records: " + "; ".join(blocked), tuple(sorted(blocking))
        )
    return PrunePlan(tuple(remove), tuple(keep))


def apply_prune(document: Mapping[str, Any], plan: PrunePlan, catalog_version: int) -> dict[str, Any]:
    """Delete the planned aliases and tombstone them at ``catalog_version``."""

    result = copy.deepcopy(dict(document))
    for alias in plan.remove:
        result["aliases"].pop(alias, None)
        result["pruned"][alias] = int(catalog_version)
    return result
