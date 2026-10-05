"""Operator settings.

``<config root>/settings.json`` (``sessions.config_root``: XDG-aware, the
same root as compositions and ``custom.json``) holds operator *policy*, never
an active model: which providers are enabled (``providers.<id>.enabled``,
fence-only — render and proxy never read this file, so a disabled provider
stays rendered) and which ``status: new`` catalog lines the operator has
admitted (``admitted_lines``).

This file is **not** a catalog document. It never enters ``catalog.load_raw``,
``catalog.SCHEMA_NAMES`` or the bundle hash; the package-root
``settings.json`` (Claude Code settings shipped with the catalog) is
unrelated.

The store and the shared resolver live here. Their callers: the profile
evaluation (lead set / agent set via :func:`line_offered`, plus
``explore_inherit_cap_disabled`` and ``workflow_default_binding``), session
records (snapshot :func:`snapshot`, drift via :func:`drift`,
record-authority compiles via :func:`effective_from_snapshot`) and the
Settings/Models/providers screens (:class:`SettingsStore`). Corruption
policy for those callers: :meth:`SettingsStore.load` raises
:class:`SettingsError`; launch refuses, read-only commands and doctor
degrade to the defaults and report.

The policy fields the compile reads are each optional with their default
in code: ``compaction_percent`` (60-95, default 90),
``explore_inherit_cap_disabled`` (default true), ``review_round_cap`` (1-3,
default 2) and ``workflow_default_binding`` (``{model, effort}`` or null,
default null). One constant set here is the single source of keys and
bounds: profile.py's ``OVERRIDABLE_SETTINGS`` is :data:`OVERRIDABLE`.
:class:`Effective` stays **Settings-level**: a profile's
``settings_overrides`` is never folded into it; the compile takes the
override through :func:`compaction_percent_for`, and a record snapshot
stores the Settings-level value.
"""

from __future__ import annotations

from . import assets

import copy
import re
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import catalog as catalog_mod
from . import errors, sessions, state, strict_json
from . import validate as schema_validate
from .composition import OPERATING_WINDOW_CEILING

SETTINGS_DATA_VERSION = 1

# The window ceiling a session's compaction window never exceeds: the
# session window is min(ceiling, the lead set's smallest provider bound),
# and it governs the lead and every 1M-class agent alike. It is never a
# settings.json key or part of a record's Settings snapshot (an earlier
# release reads both under closed schemas); :class:`Effective` carries it
# in memory only, and a record-authority compile states the window the
# record launched with. People set it in ``choices.json`` (``claude_multi.
# choices``, ``window_ceiling``) within these bounds: lowering it trades
# context for cost, and it never goes above the operating ceiling.
WINDOW_CEILING_DEFAULT = OPERATING_WINDOW_CEILING
WINDOW_CEILING_MIN = 200_000
WINDOW_CEILING_MAX = OPERATING_WINDOW_CEILING

# The policy fields. Every field is optional in settings.json and
# defaults here; the schema's bounds and enum mirror these constants
# (test_settings pins the agreement).
COMPACTION_PERCENT_KEY = "compaction_percent"
COMPACTION_PERCENT_DEFAULT = 90
COMPACTION_PERCENT_MIN = 60
COMPACTION_PERCENT_MAX = 95
EXPLORE_INHERIT_CAP_DISABLED_KEY = "explore_inherit_cap_disabled"
EXPLORE_INHERIT_CAP_DISABLED_DEFAULT = True
REVIEW_ROUND_CAP_KEY = "review_round_cap"
REVIEW_ROUND_CAP_DEFAULT = 2
REVIEW_ROUND_CAP_MIN = 1
REVIEW_ROUND_CAP_MAX = 3
WORKFLOW_DEFAULT_BINDING_KEY = "workflow_default_binding"
# Agent efforts a workflow default binding may name (never ``ultracode``,
# a lead-only effort).
WORKFLOW_BINDING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_BINDING_MODEL = re.compile(r"^[a-z0-9][a-z0-9@.-]*$")

# Integer Settings fields a profile may override (``settings_overrides``)
# -> inclusive bounds. profile.OVERRIDABLE_SETTINGS is this mapping.
OVERRIDABLE: Mapping[str, tuple[int, int]] = types.MappingProxyType(
    {COMPACTION_PERCENT_KEY: (COMPACTION_PERCENT_MIN, COMPACTION_PERCENT_MAX)}
)
_INT_FIELDS: Mapping[str, tuple[int, int, int]] = types.MappingProxyType(
    {
        COMPACTION_PERCENT_KEY: (
            COMPACTION_PERCENT_MIN, COMPACTION_PERCENT_MAX, COMPACTION_PERCENT_DEFAULT
        ),
        REVIEW_ROUND_CAP_KEY: (
            REVIEW_ROUND_CAP_MIN, REVIEW_ROUND_CAP_MAX, REVIEW_ROUND_CAP_DEFAULT
        ),
    }
)
POLICY_KEYS = (
    COMPACTION_PERCENT_KEY,
    EXPLORE_INHERIT_CAP_DISABLED_KEY,
    REVIEW_ROUND_CAP_KEY,
    WORKFLOW_DEFAULT_BINDING_KEY,
)
_SNAPSHOT_KEYS = frozenset({"version", "providers_enabled", "admitted_lines", *POLICY_KEYS})


class SettingsError(errors.ClaudeMultiError, ValueError):
    """Raised on a corrupt, unsafe or refused operator settings document."""


# ------------------------------------------------------------------ preferences
# Host-level operator preferences live in their own closed document next to
# settings.json: never in settings.json, a record's Settings snapshot, the
# operator ledger or LAUNCH_TIME_SETTINGS_KEYS. An earlier release never
# reads this file, so a rollback stays safe.
PREFERENCES_FILE = "preferences.json"
FEEDBACK_DRAFTS_KEY = "claude_feedback_drafts"
FEEDBACK_DRAFTS_VALUES = ("off", "notify")
FEEDBACK_DRAFTS_DEFAULT = "off"  # absent means off


def preferences_path(environ: Mapping[str, str] | None = None) -> Path:
    return sessions.config_root(None if environ is None else dict(environ)) / PREFERENCES_FILE


def preferences_schema_path() -> Path:
    return assets.root() / "schemas" / "preferences.schema.json"


class PreferencesStore:
    """``<config root>/preferences.json``: strict load, validated 0600 save.

    Construction has no side effects; an absent file is ``{}`` (every
    preference at its default). Its leaf lock (``preferences.json.lock``)
    sits among the store locks, before Settings (the frozen lock order).
    """

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = None if environ is None else dict(environ)
        self.path = preferences_path(self._environ)

    def load(self) -> dict[str, Any]:
        try:
            present = self.path.is_symlink() or self.path.exists()
        except OSError as exc:
            raise SettingsError(f"cannot read preferences {self.path}: {exc}") from exc
        if not present:
            return {}
        try:
            document = strict_json.loads(state.read_private(self.path))
        except (state.StateError, OSError) as exc:
            raise SettingsError(f"cannot read preferences {self.path}: {exc}") from exc
        except (strict_json.StrictJSONError, ValueError, RecursionError) as exc:
            raise SettingsError(f"preferences {self.path} are corrupt: {exc}") from exc
        return _check_preferences(document)

    def update(self, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Locked load -> mutate -> validated atomic save; returns what was written."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        state.ensure_private_dir(self.path.parent)
        lock = state.FileLock(self.path)
        lock.acquire(blocking=True)
        try:
            document = self.load()
            mutate(document)
            document = _check_preferences(copy.deepcopy(document))
            state.atomic_write(self.path, strict_json.pretty_file_bytes(document))
            return document
        finally:
            lock.release()

    def feedback_drafts(self) -> str:
        return self.load().get(FEEDBACK_DRAFTS_KEY, FEEDBACK_DRAFTS_DEFAULT)


def _check_preferences(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise SettingsError("preferences: top-level document must be an object")
    problems = schema_validate.validate(document, strict_json.load(preferences_schema_path()), "$")
    if problems:
        raise SettingsError("preferences invalid: " + "; ".join(problems))
    return document


def feedback_drafts_preference(environ: Mapping[str, str] | None = None) -> tuple[str, str | None]:
    """``(value, problem)``: the managed-session feedback-draft policy input.

    A missing file is the default ``off``; an unreadable or invalid file
    also compiles the restrictive default and reports why (doctor Attention),
    never blocking a launch over a preference.
    """

    try:
        return PreferencesStore(environ).feedback_drafts(), None
    except SettingsError as exc:
        return FEEDBACK_DRAFTS_DEFAULT, str(exc)


def settings_path(environ: Mapping[str, str] | None = None) -> Path:
    return sessions.config_root(None if environ is None else dict(environ)) / "settings.json"


def schema_path() -> Path:
    return assets.root() / "schemas" / "settings.schema.json"


def _schema() -> dict[str, Any]:
    return strict_json.load(schema_path())


def _default() -> dict[str, Any]:
    return {"version": SETTINGS_DATA_VERSION}


def _check_shape(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise SettingsError("operator settings: top-level document must be an object")
    problems = schema_validate.validate(document, _schema(), "$")
    if problems:
        raise SettingsError("operator settings invalid: " + "; ".join(problems))
    for provider_id in document.get("providers", {}):
        try:
            state.check_name(provider_id)
        except state.StateError as exc:
            raise SettingsError(f"operator settings: provider id {provider_id!r}: {exc}") from exc
    return document


def _unknown_entries(
    document: Mapping[str, Any], provider_ids: Iterable[str], line_keys: Iterable[str]
) -> tuple[str, ...]:
    providers = frozenset(provider_ids)
    lines = frozenset(line_keys)
    unknown = [
        f"provider {provider_id}"
        for provider_id in sorted(document.get("providers", {}))
        if provider_id not in providers
    ]
    unknown += [
        f"line {key}" for key in sorted(document.get("admitted_lines", [])) if key not in lines
    ]
    return tuple(unknown)


def _custom_provider_ids(custom_registry: Mapping[str, Any] | None) -> frozenset[str]:
    if not custom_registry:
        return frozenset()
    return frozenset(custom_registry.get("providers", {}))


def _check_int(value: Any, key: str, low: int, high: int, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SettingsError(f"{where}: {key} must be an integer, not {value!r}")
    if not low <= value <= high:
        raise SettingsError(f"{where}: {key} {value} is outside {low}-{high}")
    return value


def _check_binding(value: Any, where: str) -> dict[str, str] | None:
    """A workflow default binding's shape (``{model, effort}`` or None)."""

    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"model", "effort"}:
        raise SettingsError(
            f"{where}: {WORKFLOW_DEFAULT_BINDING_KEY} must be null or exactly "
            "{model, effort}"
        )
    model, effort = value["model"], value["effort"]
    if not isinstance(model, str) or not _BINDING_MODEL.match(model):
        raise SettingsError(
            f"{where}: {WORKFLOW_DEFAULT_BINDING_KEY}.model {model!r} is not a model key"
        )
    if effort not in WORKFLOW_BINDING_EFFORTS:
        raise SettingsError(
            f"{where}: {WORKFLOW_DEFAULT_BINDING_KEY}.effort {effort!r} is not an agent "
            f"effort {list(WORKFLOW_BINDING_EFFORTS)}"
        )
    return {"model": model, "effort": effort}


def _policy_values(document: Mapping[str, Any], where: str) -> dict[str, Any]:
    """The four policy fields of a settings document or snapshot.

    An absent field is its default; a present one must be valid
    (:class:`SettingsError` naming the key).
    """

    values: dict[str, Any] = {}
    for key, (low, high, default) in _INT_FIELDS.items():
        values[key] = (
            _check_int(document[key], key, low, high, where) if key in document else default
        )
    cap = document.get(EXPLORE_INHERIT_CAP_DISABLED_KEY, EXPLORE_INHERIT_CAP_DISABLED_DEFAULT)
    if not isinstance(cap, bool):
        raise SettingsError(
            f"{where}: {EXPLORE_INHERIT_CAP_DISABLED_KEY} must be a boolean, not {cap!r}"
        )
    values[EXPLORE_INHERIT_CAP_DISABLED_KEY] = cap
    values[WORKFLOW_DEFAULT_BINDING_KEY] = _check_binding(
        document.get(WORKFLOW_DEFAULT_BINDING_KEY), where
    )
    return values


def _line_efforts(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """Declared efforts by the line's own shape (list or map)."""

    efforts = entry.get("efforts")
    if isinstance(efforts, (list, tuple)):
        return tuple(value for value in efforts if isinstance(value, str))
    if isinstance(efforts, Mapping):
        return tuple(value for value in efforts if isinstance(value, str))
    return ()


def check_workflow_default_binding(binding: Mapping[str, Any] | None, *, catalog: Any) -> None:
    """Refuse a workflow default binding the catalog cannot back (save side).

    The model key must resolve (``resolve_key``: live, or retired with a live
    successor); the resolved line must be agents-capable and declare the
    effort. A client-effort line (list-shaped ``efforts``) compiles to its one
    selector, so ``CLAUDE_CODE_SUBAGENT_MODEL`` cannot carry any other effort:
    the effort must be the line's ``default_effort``.
    Offered-ness (status, provider enabled) and fence membership are
    compile-time checks. ``catalog`` needs ``lines`` and ``retired``.
    """

    if binding is None:
        return
    field = f"Settings {WORKFLOW_DEFAULT_BINDING_KEY}"
    model, effort = binding["model"], binding["effort"]
    try:
        resolution = catalog_mod.resolve_key_in(
            catalog.lines, getattr(catalog, "retired", {}) or {}, model
        )
    except catalog_mod.CatalogError as exc:
        raise SettingsError(f"{field}.model: {exc}") from exc
    if resolution.key is None:
        raise SettingsError(f"{field}.model: {resolution.notice}")
    key = resolution.key
    entry = catalog.lines[key]
    if "agents" not in entry.get("capabilities", ()):
        raise SettingsError(
            f"{field}.model: line {key!r} is not agents-capable (a lead-only line "
            "cannot run workflow agents)"
        )
    declared = _line_efforts(entry)
    if effort not in declared:
        raise SettingsError(
            f"{field}.effort: {effort!r} is not declared by line {key!r} "
            f"(declares {list(declared)})"
        )
    if isinstance(entry.get("efforts"), (list, tuple)) and effort != entry.get("default_effort"):
        raise SettingsError(
            f"{field}.effort: line {key!r} is client-effort (one selector), so a "
            f"workflow default runs at its default effort {entry.get('default_effort')!r}, "
            f"never {effort!r}"
        )


_UNSET = object()


def check_save(
    doc: Mapping[str, Any],
    *,
    catalog: Any,
    custom_registry: Mapping[str, Any] | None = None,
    tolerated: Iterable[str] = (),
    tolerated_binding: Any = _UNSET,
) -> dict[str, Any]:
    """Every check :meth:`SettingsStore.save` runs, without writing; returns
    the normalized document (import validates a planned Settings document
    exactly as the ordinary save would)."""

    document = _check_shape(copy.deepcopy(dict(doc)))
    if "admitted_lines" in document:
        document["admitted_lines"] = sorted(document["admitted_lines"])
    accepted = frozenset(tolerated)
    provider_ids = frozenset(catalog.providers) | _custom_provider_ids(custom_registry)
    refused = [
        entry
        for entry in _unknown_entries(document, provider_ids, catalog.lines)
        if entry not in accepted
    ]
    if refused:
        raise SettingsError(
            "refusing to save operator settings naming unknown "
            + ", ".join(refused)
        )
    binding = document.get(WORKFLOW_DEFAULT_BINDING_KEY)
    if binding is not None and binding != tolerated_binding:
        check_workflow_default_binding(binding, catalog=catalog)
    return document


def unknown_entries(document: Mapping[str, Any], *, catalog: Any,
                    custom_registry: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """The stale provider/line entries a later save tolerates."""

    provider_ids = frozenset(catalog.providers) | _custom_provider_ids(custom_registry)
    return _unknown_entries(document, provider_ids, catalog.lines)


class SettingsStore:
    """The operator settings file: strict load, validated 0600 atomic save.

    Construction has no filesystem side effects; only :meth:`save` (and
    therefore :meth:`update`) creates the private config directory.
    """

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = None if environ is None else dict(environ)
        self.path = settings_path(self._environ)

    def load(self) -> dict[str, Any]:
        """The document; an absent file is ``{"version": 1}``.

        Unknown provider ids and line keys are tolerated here (the catalog
        or custom registry may have moved on); :func:`effective` reports
        them. Malformed JSON, duplicate keys, a group/other-accessible file
        or a symlink raise :class:`SettingsError`.
        """

        try:
            present = self.path.is_symlink() or self.path.exists()
        except OSError as exc:
            raise SettingsError(f"cannot read operator settings {self.path}: {exc}") from exc
        if not present:
            return _default()
        try:
            document = strict_json.loads(state.read_private(self.path))
        except (state.StateError, OSError) as exc:
            raise SettingsError(f"cannot read operator settings {self.path}: {exc}") from exc
        except (strict_json.StrictJSONError, ValueError, RecursionError) as exc:
            raise SettingsError(f"operator settings {self.path} are corrupt: {exc}") from exc
        return _check_shape(document)

    def save(
        self,
        doc: Mapping[str, Any],
        *,
        catalog: Any,
        custom_registry: Mapping[str, Any] | None = None,
        _tolerated: Iterable[str] = (),
        _tolerated_binding: Any = _UNSET,
    ) -> dict[str, Any]:
        """Validate and write ``doc`` (0600, pretty bytes); returns what was written.

        Refuses a provider id outside the catalog providers and the custom
        registry's providers, an admitted line outside ``catalog.lines``, and
        a ``workflow_default_binding`` the catalog cannot back
        (:func:`check_workflow_default_binding`). ``_tolerated`` (``update``
        only) names ``"provider <id>"`` / ``"line <key>"`` entries that were
        already on disk, and ``_tolerated_binding`` the binding that was on
        disk: a stale entry left by a catalog or registry change must not
        block every later write — only a newly introduced one is refused.
        """

        sessions.check_state_marker(sessions.state_root(self._environ))
        document = check_save(doc, catalog=catalog, custom_registry=custom_registry,
                              tolerated=_tolerated, tolerated_binding=_tolerated_binding)
        state.ensure_private_dir(self.path.parent)
        state.atomic_write(self.path, strict_json.pretty_file_bytes(document))
        return document

    def update(
        self,
        mutate: Callable[[dict[str, Any]], None],
        *,
        catalog: Any,
        custom_registry: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Locked load -> mutate -> save; returns the saved document."""

        sessions.check_state_marker(sessions.state_root(self._environ))
        state.ensure_private_dir(self.path.parent)
        lock = state.FileLock(self.path)
        lock.acquire(blocking=True)
        try:
            document = self.load()
            provider_ids = frozenset(catalog.providers) | _custom_provider_ids(custom_registry)
            already = _unknown_entries(document, provider_ids, catalog.lines)
            binding_on_disk = copy.deepcopy(document.get(WORKFLOW_DEFAULT_BINDING_KEY))
            mutate(document)
            return self.save(
                document,
                catalog=catalog,
                custom_registry=custom_registry,
                _tolerated=already,
                _tolerated_binding=binding_on_disk,
            )
        finally:
            lock.release()

    def set_provider_enabled(
        self,
        provider_id: str,
        enabled: bool,
        *,
        catalog: Any,
        custom_registry: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        known = frozenset(catalog.providers) | _custom_provider_ids(custom_registry)
        if provider_id not in known:
            raise SettingsError(f"unknown provider {provider_id!r}")

        def _apply(document: dict[str, Any]) -> None:
            document.setdefault("providers", {})[provider_id] = {"enabled": bool(enabled)}

        return self.update(_apply, catalog=catalog, custom_registry=custom_registry)

    def admit_line(self, key: str, *, catalog: Any) -> dict[str, Any]:
        """Admit a ``status: new`` line; active or unknown lines are refused."""

        entry = catalog.lines.get(key)
        if entry is None:
            raise SettingsError(f"unknown catalog line {key!r}")
        if entry.get("status", "active") != "new":
            raise SettingsError(
                f"line {key!r} is {entry.get('status', 'active')!r}, not New · Off; "
                "only status new lines are admitted"
            )

        def _apply(document: dict[str, Any]) -> None:
            admitted = set(document.get("admitted_lines", []))
            admitted.add(key)
            document["admitted_lines"] = sorted(admitted)

        return self.update(_apply, catalog=catalog)

    def revoke_line(self, key: str, *, catalog: Any) -> dict[str, Any]:
        """Remove an admission (an unknown, stale key may be revoked too)."""

        def _apply(document: dict[str, Any]) -> None:
            if "admitted_lines" in document:
                document["admitted_lines"] = sorted(
                    set(document["admitted_lines"]) - {key}
                )

        return self.update(_apply, catalog=catalog)


@dataclass(frozen=True)
class Effective:
    """Settings resolved against the known providers and lines.

    Settings-level only: a profile's ``settings_overrides`` never folds in
    here; see :func:`compaction_percent_for`. The policy fields default so
    call sites that build one directly stay valid.

    ``window_ceiling`` is the session window's ceiling
    (:data:`WINDOW_CEILING_DEFAULT` unless set): an in-memory input of every
    compile and evaluation, never written to ``settings.json`` or a
    snapshot (:func:`snapshot` and :func:`drift` ignore it).
    """

    providers_enabled: Mapping[str, bool]  # every known provider id -> bool (absent = True)
    admitted_lines: frozenset[str]
    unknown: tuple[str, ...]  # "provider <id>" / "line <key>" tolerated on load
    compaction_percent: int = COMPACTION_PERCENT_DEFAULT
    explore_inherit_cap_disabled: bool = EXPLORE_INHERIT_CAP_DISABLED_DEFAULT
    review_round_cap: int = REVIEW_ROUND_CAP_DEFAULT
    workflow_default_binding: Mapping[str, str] | None = None  # {model, effort} or None
    window_ceiling: int = WINDOW_CEILING_DEFAULT


def effective(
    doc: Mapping[str, Any],
    *,
    provider_ids: Iterable[str],
    line_keys: Iterable[str],
) -> Effective:
    """Resolve ``doc`` against the known provider ids and line keys.

    The policy fields default when absent; an invalid present value
    raises :class:`SettingsError` naming the key. The workflow default
    binding is carried as written: resolving its key, offered-ness and
    fence membership are the compile's.
    """

    providers = sorted(set(provider_ids))
    lines = frozenset(line_keys)
    configured = doc.get("providers", {})
    enabled = {
        provider_id: bool(configured.get(provider_id, {}).get("enabled", True))
        for provider_id in providers
    }
    admitted = frozenset(key for key in doc.get("admitted_lines", []) if key in lines)
    return Effective(
        providers_enabled=enabled,
        admitted_lines=admitted,
        unknown=_unknown_entries(doc, providers, lines),
        **_policy_values(doc, "operator settings"),
    )


def compaction_percent_for(
    eff: Effective, settings_overrides: Mapping[str, Any] | None = None
) -> int:
    """The compaction percent a compile uses.

    ``settings_overrides`` is the profile's (``ResolvedLineup.settings_overrides``):
    its ``compaction_percent`` wins over the Settings-level value in ``eff``.
    A key outside :data:`OVERRIDABLE`, a non-integer (bool included) or an
    out-of-bounds value raises :class:`SettingsError` naming the key.
    """

    overrides = settings_overrides or {}
    for key in sorted(overrides):
        if key not in OVERRIDABLE:
            raise SettingsError(f"settings_overrides.{key}: not overridable per profile")
        low, high = OVERRIDABLE[key]
        _check_int(overrides[key], key, low, high, "settings_overrides")
    return int(overrides.get(COMPACTION_PERCENT_KEY, eff.compaction_percent))


def provider_enabled(eff: Effective, provider_id: str) -> bool:
    return bool(eff.providers_enabled.get(provider_id, True))


def line_offered(key: str, entry_v2: Mapping[str, Any], eff: Effective) -> bool:
    """A line is offered when active (or admitted) and its provider is enabled."""

    admitted = entry_v2.get("status", "active") == "active" or key in eff.admitted_lines
    return admitted and provider_enabled(eff, entry_v2["provider"])


def _binding_snapshot(binding: Mapping[str, str] | None) -> dict[str, str] | None:
    if binding is None:
        return None
    return {"effort": binding["effort"], "model": binding["model"]}


def snapshot(eff: Effective) -> dict[str, Any]:
    """Canonical record snapshot (stored in a record's ``applied.settings``).

    Settings-level values only: a profile override travels with the
    profile / applied lineup, never in the snapshot.
    """

    return {
        "version": SETTINGS_DATA_VERSION,
        "providers_enabled": {
            provider_id: bool(eff.providers_enabled[provider_id])
            for provider_id in sorted(eff.providers_enabled)
        },
        "admitted_lines": sorted(eff.admitted_lines),
        COMPACTION_PERCENT_KEY: eff.compaction_percent,
        EXPLORE_INHERIT_CAP_DISABLED_KEY: eff.explore_inherit_cap_disabled,
        REVIEW_ROUND_CAP_KEY: eff.review_round_cap,
        WORKFLOW_DEFAULT_BINDING_KEY: _binding_snapshot(eff.workflow_default_binding),
    }


def effective_from_snapshot(doc: Any, *, window_ceiling: int | None = None) -> Effective:
    """The inverse of :func:`snapshot`, for the record-authority compiles.

    Strict: the version, ``providers_enabled`` (id -> bool) and
    ``admitted_lines`` (strings) are required, an unknown key is refused (a
    newer snapshot shape is never misread), and an absent policy field is
    its default. ``unknown`` is always empty (a snapshot names only what was
    known at launch). ``window_ceiling`` is the window the record launched
    with (``sessions.recorded_window``; None: the default ceiling), so a
    record-authority compile decides each role's class against the window
    the running session has. Raises :class:`SettingsError`.
    """

    where = "settings snapshot"
    if not isinstance(doc, Mapping):
        raise SettingsError(f"{where}: must be an object")
    extra = sorted(str(key) for key in doc if key not in _SNAPSHOT_KEYS)
    if extra:
        raise SettingsError(f"{where}: unknown keys {extra}")
    if doc.get("version") != SETTINGS_DATA_VERSION or isinstance(doc.get("version"), bool):
        raise SettingsError(f"{where}: version must be {SETTINGS_DATA_VERSION}")
    providers = doc.get("providers_enabled")
    if not isinstance(providers, Mapping) or not all(
        isinstance(key, str) and isinstance(value, bool) for key, value in providers.items()
    ):
        raise SettingsError(f"{where}: providers_enabled must map provider ids to booleans")
    admitted = doc.get("admitted_lines")
    if not isinstance(admitted, (list, tuple)) or not all(
        isinstance(key, str) for key in admitted
    ):
        raise SettingsError(f"{where}: admitted_lines must be a list of line keys")
    return Effective(
        providers_enabled={key: providers[key] for key in sorted(providers)},
        admitted_lines=frozenset(admitted),
        unknown=(),
        **_policy_values(doc, where),
        window_ceiling=WINDOW_CEILING_DEFAULT if window_ceiling is None else window_ceiling,
    )


def _lenient_policy(snapshot_doc: Any) -> dict[str, Any]:
    """Policy values of a snapshot for :func:`drift`: junk reads as the default."""

    document = snapshot_doc if isinstance(snapshot_doc, Mapping) else {}
    values = _policy_values({}, "snapshot")
    for key in POLICY_KEYS:
        if key in document:
            try:
                values[key] = _policy_values({key: document[key]}, "snapshot")[key]
            except SettingsError:
                pass
    return values


def _binding_text(binding: Mapping[str, str] | None) -> str:
    return "off" if binding is None else f"{binding['model']}:{binding['effort']}"


def drift(snapshot_doc: Mapping[str, Any], eff: Effective) -> list[str]:
    """Human annotations of what changed since the snapshot; never raises for a difference.

    A provider missing from the snapshot counts as enabled (the default);
    a provider missing from ``eff`` is not a settings change and is skipped.
    A policy field missing from (or invalid in) the snapshot reads as its
    default.
    """

    before = snapshot_doc.get("providers_enabled", {}) if isinstance(snapshot_doc, Mapping) else {}
    if not isinstance(before, Mapping):
        before = {}
    notes: list[str] = []
    for provider_id in sorted(eff.providers_enabled):
        was = bool(before.get(provider_id, True))
        now = bool(eff.providers_enabled[provider_id])
        if was != now:
            notes.append(
                f"provider {provider_id} {'enabled' if now else 'disabled'} since launch"
            )
    old_lines = snapshot_doc.get("admitted_lines", []) if isinstance(snapshot_doc, Mapping) else []
    old = frozenset(
        key for key in (old_lines if isinstance(old_lines, (list, tuple)) else ()) if isinstance(key, str)
    )
    for key in sorted(eff.admitted_lines - old):
        notes.append(f"line {key} admitted since launch")
    for key in sorted(old - eff.admitted_lines):
        notes.append(f"line {key} revoked since launch")
    was_policy = _lenient_policy(snapshot_doc)
    if eff.compaction_percent != was_policy[COMPACTION_PERCENT_KEY]:
        notes.append(
            f"compaction percent {eff.compaction_percent} since launch "
            f"(was {was_policy[COMPACTION_PERCENT_KEY]})"
        )
    if eff.explore_inherit_cap_disabled != was_policy[EXPLORE_INHERIT_CAP_DISABLED_KEY]:
        notes.append(
            "explore inherit cap "
            f"{'disabled' if eff.explore_inherit_cap_disabled else 'enabled'} since launch"
        )
    if eff.review_round_cap != was_policy[REVIEW_ROUND_CAP_KEY]:
        notes.append(
            f"review round cap {eff.review_round_cap} since launch "
            f"(was {was_policy[REVIEW_ROUND_CAP_KEY]})"
        )
    now_binding = _binding_snapshot(eff.workflow_default_binding)
    was_binding = was_policy[WORKFLOW_DEFAULT_BINDING_KEY]
    if now_binding != was_binding:
        notes.append(
            f"workflow default binding {_binding_text(now_binding)} since launch "
            f"(was {_binding_text(was_binding)})"
        )
    return notes
