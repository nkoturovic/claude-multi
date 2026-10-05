"""Profiles: the list with readiness, and copy, starter, rename, reseed and delete.

Every write keeps what it replaces or removes: a reseed keeps the previous
file as ``.<name>.pre-reseed-<UTC>.json`` and a delete as
``.<name>.removed-<UTC>.json`` (private, never overwritten, hidden from the
profile list). Every apply re-checks the digest of each file its plan read,
so a profile changed meanwhile (another window, another command) is never
overwritten or removed unseen. Sessions that follow a renamed or removed
profile keep their lineup and stop following it; the default profile moves
with a rename and goes back to automatic after a delete.
"""

from __future__ import annotations

import copy
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from claude_multi import lineup as lineup_mod
from claude_multi import paths, profile as profile_mod, readiness, state
from claude_multi.setup import defaults, model

# One reason and one fix per profile (detail line 2 of the Profiles screen).
PROFILE_REASON = {
    "connected": "connected: every model this profile uses is served here",
    "needs-signin": "needs {kind}: G → L signs in",
    "needs-key": "needs the {display} API key: G → K sets it",
    "needs-apply": "a model is not served yet: G → P applies your changes",
    "needs-on": "{display} is turned off: G → Space turns it on",
    "needs-approval": "{id}'s route is not approved: G → Enter approves it",
    "needs-reachable": "{id} is not reachable from here: connect to its network, or E binds another model",
    "unchecked": "not checked here ({text}): {remedy}",
    "invalid": "{first_error} — E fixes it",
    "unloadable": "cannot be loaded — E edits the file, X deletes it (a copy is kept)",
    "seed-stale-unedited": "a newer shipped version exists and you have not changed it — U updates it",
    "seed-stale-edited": "a newer shipped version exists — U shows the changes first; your version is kept as a backup",
}
ORIGIN_SEED = "seed"
ORIGIN_YOURS = "yours"

# Names.
NAME_RULE = "a–z, 0–9, . _ - (up to 64), starting with a letter or digit"
NAME_TAKEN = "{name} already exists — choose another name."
NAME_BAD = "Not a profile name: " + NAME_RULE + "."

# Refusals.
SEED_NO_RENAME = "{name} is a shipped profile and keeps its name — C copies it under a new name."
SEED_NO_DELETE = "{name} is a shipped profile and cannot be deleted — U restores its shipped version."
NOT_A_SEED = "{name} is your own profile — there is no shipped version to restore."
SEED_NO_RENAME_LINE = ("{name} is a shipped profile and keeps its name — copy it under a new name: "
                       "claude-multi profile duplicate {name} NEW")
SEED_NO_DELETE_LINE = ("{name} is a shipped profile and cannot be deleted — restore its shipped version: "
                       "claude-multi profile reseed {name}")
NOT_FOUND = "profile {name!r} does not exist — claude-multi profile list"
UNLOADABLE_SOURCE = "{error} — fix it first: claude-multi profile edit {name} (or rm; a copy is kept)"

# Plan and result lines.
COPY_LINE = "Copy {source} to {target}."
COPY_KEEPS_FALLBACK = "{target} stays the fallback for {provider}; {source} keeps it too."
COPY_DROPS_FALLBACK = "{target} is not a fallback profile ({source} stays the fallback for {provider})."
COPIED = "Copied {source} to {target} — E edits it."
FOLLOWERS_LINE = ("{n} session(s) follow {name} ({m} running). After this they keep their current "
                  "lineup and stop following it.\n")
DEFAULT_MOVES = "It is your default profile; the default moves to the new name.\n"
DEFAULT_CLEARS = "It is your default profile; the default goes back to automatic.\n"
RENAME_LINE = "Rename {old} to {new}."
RENAMED = "Renamed {old} to {new}{followers_note}."
RENAMED_FOLLOWERS = "; {n} session(s) keep their lineup and no longer follow it"
DELETE_LINE = "A copy is kept as {backup}."
DELETED = "Deleted {name}; a copy is kept as {backup}."
RESEED_NOTHING = "{name} already matches its shipped version."
RESEED_UNLOADABLE = "Your file cannot be loaded; the shipped version replaces it. Your file is kept as {backup}."
RESEED_KEEPS = "Your version is kept as {backup}."
RESEEDED = "Restored {name} to shipped version {v}; your version is kept as {backup}."
REFRESH_LINE = "{name} {old} → {new}"
REFRESH_PARTIAL = ("Updated {done} to the shipped version (your versions are kept); {unchanged} {verb} not "
                   "changed: {reason}")
REFRESH_UNFINISHED = ("Updated {done} to the shipped version (your versions are kept), but {failed} was not "
                      "finished: {reason}")
REFRESH_UNFINISHED_REST = REFRESH_UNFINISHED + "; {unchanged} {verb} not changed"


def _display(runtime: Any, path: Path) -> str:
    return paths.display(path, runtime.environ)


def _check_name(store: profile_mod.ProfileStore, name: str) -> None:
    if not isinstance(name, str) or not state.SAFE_NAME.fullmatch(name):
        raise model.Refused(NAME_BAD)
    if store.contains(name):
        raise model.Refused(NAME_TAKEN.format(name=name))


def _require(store: profile_mod.ProfileStore, name: str) -> None:
    try:
        exists = store.contains(name)
    except profile_mod.ProfileError as exc:
        raise model.Refused(str(exc)) from exc
    if not exists:
        raise model.Refused(NOT_FOUND.format(name=name), remedy="claude-multi profile list")


def _load_source(store: profile_mod.ProfileStore, name: str) -> dict[str, Any]:
    _require(store, name)
    try:
        return store.load(name)
    except profile_mod.ProfileError as exc:
        raise model.Refused(UNLOADABLE_SOURCE.format(error=exc, name=name),
                            remedy=f"claude-multi profile edit {name}") from exc


def _changed(name: str) -> model.Stale:
    return model.Stale(f"profile {name}")


# ------------------------------------------------------------------ the list


@dataclass(frozen=True)
class ProfileRow:
    name: str
    origin: str  # "seed" | "yours"
    seed_state: str | None  # current | unedited-stale | edited | edited-stale | unknown | None (yours)
    loadable: bool
    load_error: profile_mod.ProfileError | None
    ready: bool | None  # None when unloadable
    state_text: str  # "connected" | "needs …" | "invalid" | "not checked" | "cannot load"
    reason_line: str  # one reason + one fix
    last_used: str | None  # ISO stamp from session records
    is_default: bool
    fallback_for: str | None  # the profile's primary provider
    description: str
    seed_stale: bool = False  # a newer shipped version exists (whatever the file's provenance)

    @property
    def stale(self) -> bool:
        return self.seed_stale

    @property
    def notes(self) -> tuple[str, ...]:
        found: list[str] = []
        if self.is_default:
            found.append("default")
        if self.fallback_for:
            found.append("fallback")
        if self.stale:
            found.append("update available")
        line = getattr(self.load_error, "line", None)
        if line:
            found.append(f"line {line}")
        return tuple(found)


def _unloadable_text(runtime: Any, error: profile_mod.ProfileError) -> str:
    if isinstance(error, profile_mod.ProfileLoadError):
        at = f" line {error.line}" if error.line else ""
        return f"{_display(runtime, error.path)}{at}: {error.detail}"
    return str(error)


def rows(runtime: Any, *, fallback_only: bool = False) -> tuple[ProfileRow, ...]:
    """Every profile with its readiness (one observation refresh), in the
    ready-first order; ``fallback_only`` keeps the fallback profiles."""

    store = runtime.profiles
    loadable, _failed = defaults.split_loadable(store, store.names())
    verdicts = defaults.profile_verdicts(runtime, loadable)
    order = defaults.profile_choices(runtime, verdicts=verdicts)
    lines = runtime.lineup_catalog().lines
    providers = runtime.readiness_providers()
    found: list[ProfileRow] = []
    for choice in order:
        name = choice.name
        seed = store.is_seed(name)
        origin = ORIGIN_SEED if seed else ORIGIN_YOURS
        judged = store.seed_state(name) if seed else None
        seed_state = judged.label if judged is not None else None
        # A file that cannot be loaded says so instead (its fix restores it).
        seed_stale = bool(judged is not None and judged.stale and choice.loadable)
        if not choice.loadable:
            if fallback_only:
                continue
            try:
                store.load(name)
                error: profile_mod.ProfileError | None = None
            except profile_mod.ProfileError as exc:
                error = exc
            found.append(ProfileRow(
                name, origin, seed_state, False, error, None, defaults.STATE_UNLOADABLE,
                PROFILE_REASON["unloadable"], choice.last_used, choice.is_default, None,
                _unloadable_text(runtime, error) if error is not None else "",
            ))
            continue
        document = store.load(name)
        primary = document.get("primary_provider")
        if fallback_only and not primary:
            continue
        summary = defaults.summarize(verdicts.get(name, ()), lines=lines, providers=providers)
        if summary.ready and seed_state == "unedited-stale":
            reason = PROFILE_REASON["seed-stale-unedited"]
        elif summary.ready and seed_stale:
            # Edited, or of unknown provenance (treated as edited): U shows the changes first.
            reason = PROFILE_REASON["seed-stale-edited"]
        else:
            reason = PROFILE_REASON[summary.reason].format(**summary.fields)
        found.append(ProfileRow(
            name, origin, seed_state, True, None, summary.ready, summary.state_text, reason,
            choice.last_used, choice.is_default, primary if isinstance(primary, str) else None,
            str(document.get("description") or ""), seed_stale,
        ))
    return tuple(found)


def other_fallbacks(runtime: Any, provider: str, *, name: str | None = None) -> tuple[str, ...]:
    """The other loadable profiles that are also the fallback for ``provider``."""

    store = runtime.profiles
    found: list[str] = []
    for other in store.names():
        if other == name:
            continue
        try:
            document = store.load(other)
        except profile_mod.ProfileError:
            continue
        if document.get("primary_provider") == provider:
            found.append(other)
    return tuple(found)


# ------------------------------------------------------------------ copy


@dataclass(frozen=True)
class CopyPlan(model.Plan):
    source: str
    target: str
    keep_fallback: bool
    source_digest: str | None


def plan_copy(runtime: Any, source: str, target: str, *, keep_fallback: bool) -> CopyPlan:
    store = runtime.profiles
    document = _load_source(store, source)
    _check_name(store, target)
    primary = document.get("primary_provider")
    lines = [COPY_LINE.format(source=source, target=target)]
    if isinstance(primary, str):
        template = COPY_KEEPS_FALLBACK if keep_fallback else COPY_DROPS_FALLBACK
        lines.append(template.format(source=source, target=target, provider=primary))
    source_digest = store.digest(source)
    digest = model.digest_of("copy", source, target, bool(keep_fallback), source_digest)
    return CopyPlan(op="copy", subject=target, lines=tuple(lines), consent=None, guarded=False, digest=digest,
                    writes=(_display(runtime, store.root / f"{target}.json"),),
                    source=source, target=target, keep_fallback=bool(keep_fallback), source_digest=source_digest)


def apply_copy(runtime: Any, plan: CopyPlan, confirmation: model.Confirmation) -> Path:
    """``ProfileStore.duplicate``; the copy keeps ``primary_provider`` only when chosen."""

    model.check_apply(runtime, plan, confirmation, verb="profile copy")
    store = runtime.profiles
    try:
        store.duplicate(plan.source, plan.target, keep_primary=plan.keep_fallback, expected=plan.source_digest)
    except profile_mod.ProfileChangedError as exc:
        raise _changed(plan.source) from exc
    except profile_mod.ProfileValidationError:
        raise
    except profile_mod.ProfileError as exc:  # the target appeared meanwhile
        raise _changed(plan.target) from exc
    return store.root / f"{plan.target}.json"


# ------------------------------------------------------------------ starter


@dataclass(frozen=True)
class NewStarterPlan(model.Plan):
    name: str
    document: dict[str, Any]
    spend_notes: tuple[str, ...]


def plan_new_starter(runtime: Any, name: str, *, assume_keys: frozenset[str] = frozenset()) -> NewStarterPlan:
    """A profile built from the connected providers (see ``profile starter``);
    its lines are the legend, the preview and the spend notes. With
    ``assume_keys``, the providers a planned change gives a key first count
    as connected (a preview made before that change)."""

    store = runtime.profiles
    _check_name(store, name)
    plan, warnings = (runtime.starter_plan(name, assume_keys=assume_keys) if assume_keys
                      else runtime.starter_plan(name))
    if plan.document is None:
        raise model.Refused(str(plan.refusal))
    notes: tuple[str, ...] = ()
    evaluation = profile_mod.evaluate(plan.document, runtime.lineup_catalog(), bindings=runtime.bindings.bindings(),
                                      effective=runtime.current_effective(), ad_hoc=False)
    if evaluation.lineup is not None:
        notes = defaults.spend_notes(runtime, evaluation.lineup)
    preview = readiness.starter_preview(plan, name=name, warnings=warnings).splitlines()
    lines = (readiness.LEGEND, *preview[:-1], *notes, *preview[-1:])
    digest = model.digest_of("starter", name, plan.document)
    return NewStarterPlan(op="starter", subject=name, lines=tuple(lines), consent="confirm", guarded=False,
                          digest=digest, writes=(_display(runtime, store.root / f"{name}.json"),),
                          name=name, document=plan.document, spend_notes=notes)


def apply_new_starter(runtime: Any, plan: NewStarterPlan, confirmation: model.Confirmation) -> Path:
    model.check_apply(runtime, plan, confirmation, verb="profile starter")
    try:
        return runtime.profiles.new(plan.document)
    except profile_mod.ProfileValidationError:
        raise
    except profile_mod.ProfileError as exc:
        raise model.Stale(f"profile {plan.name}") from exc


# ------------------------------------------------------------------ rename


@dataclass(frozen=True)
class RenamePlan(model.Plan):
    source: str
    target: str
    followers: int
    running: int
    is_default: bool
    source_digest: str | None
    # An edited profile saved under the new name (None: the stored one moves).
    document: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class RenameOutcome:
    path: Path
    default_moved: bool
    followers: int
    lines: tuple[str, ...]
    digest: str | None = None  # the digest of the file this rename wrote


def _followers(runtime: Any, name: str) -> tuple[int, int]:
    found, _unreadable = lineup_mod.followers(runtime, [name])
    return len(found), sum(1 for item in found if item.live)


_READ_NOW = object()


def plan_rename(runtime: Any, source: str, target: str, *, document: Mapping[str, Any] | None = None,
                expected: Any = _READ_NOW) -> RenamePlan:
    """Rename ``source`` to ``target``. ``document`` (the profile editor's
    Rename) is what the new name holds; ``expected`` is the digest the
    editor loaded (None: write regardless, its Overwrite), read now when
    not given."""

    store = runtime.profiles
    _require(store, source)
    if store.is_seed(source):
        raise model.Refused(SEED_NO_RENAME.format(name=source), line=SEED_NO_RENAME_LINE.format(name=source),
                            fix=model.Fix("C", f"claude-multi profile duplicate {source} NEW"))
    _check_name(store, target)
    followers, running = _followers(runtime, source)
    is_default = defaults.chosen_default(runtime) == source
    lines = [RENAME_LINE.format(old=source, new=target)]
    if followers:
        lines.append(FOLLOWERS_LINE.format(n=followers, name=source, m=running).rstrip("\n"))
    if is_default:
        lines.append(DEFAULT_MOVES.rstrip("\n"))
    source_digest = store.digest(source) if expected is _READ_NOW else expected
    edited = None if document is None else profile_mod.document_digest({**document, "name": target})
    digest = model.digest_of("rename", source, target, followers, is_default, source_digest, edited)
    return RenamePlan(op="rename", subject=source, lines=tuple(lines), consent="confirm", guarded=False,
                      digest=digest, writes=(_display(runtime, store.root / f"{target}.json"),),
                      source=source, target=target, followers=followers, running=running,
                      is_default=is_default, source_digest=source_digest,
                      document=None if document is None else copy.deepcopy(dict(document)))


def apply_rename(runtime: Any, plan: RenamePlan, confirmation: model.Confirmation) -> RenameOutcome:
    """Rename, then let following sessions keep their lineup (pinned), then
    move the default when it named the old profile."""

    model.check_apply(runtime, plan, confirmation, verb="profile rename")
    store = runtime.profiles
    try:
        written = store.rename(plan.source, plan.target, expected=plan.source_digest, document=plan.document)
    except profile_mod.ProfileChangedError as exc:
        raise _changed(plan.source) from exc
    except profile_mod.ProfileValidationError:
        raise
    except profile_mod.ProfileError as exc:  # the target appeared meanwhile
        raise _changed(plan.target) from exc
    sink = io.StringIO()
    lineup_mod.on_removed(runtime, plan.source, renamed_to=plan.target, out=sink)
    moved = defaults.move_default(runtime, plan.source, plan.target)
    note = RENAMED_FOLLOWERS.format(n=plan.followers) if plan.followers else ""
    lines = (RENAMED.format(old=plan.source, new=plan.target, followers_note=note), *sink.getvalue().splitlines())
    return RenameOutcome(store.root / f"{plan.target}.json", moved, plan.followers, lines,
                         profile_mod.written_digest(written))


# ------------------------------------------------------------------ delete


@dataclass(frozen=True)
class DeletePlan(model.Plan):
    name: str
    followers: int
    running: int
    is_default: bool
    name_digest: str | None


@dataclass(frozen=True)
class DeleteOutcome:
    backup: Path
    default_cleared: bool
    lines: tuple[str, ...]


def _backup_hint(runtime: Any, name: str, reason: str) -> str:
    """Where a copy will be kept (its time is the time of the write)."""

    store = runtime.profiles
    return _display(runtime, store.root / f".{name}.{reason}-<time>.json")


def plan_delete(runtime: Any, name: str) -> DeletePlan:
    store = runtime.profiles
    _require(store, name)
    if store.is_seed(name):
        raise model.Refused(SEED_NO_DELETE.format(name=name), line=SEED_NO_DELETE_LINE.format(name=name),
                            fix=model.Fix("U", f"claude-multi profile reseed {name}"))
    followers, running = _followers(runtime, name)
    is_default = defaults.chosen_default(runtime) == name
    lines = [DELETE_LINE.format(backup=_backup_hint(runtime, name, "removed"))]
    if followers:
        lines.append(FOLLOWERS_LINE.format(n=followers, name=name, m=running).rstrip("\n"))
    if is_default:
        lines.append(DEFAULT_CLEARS.rstrip("\n"))
    name_digest = store.digest(name)
    digest = model.digest_of("delete", name, followers, is_default, name_digest)
    return DeletePlan(op="delete", subject=name, lines=tuple(lines), consent="destructive", guarded=False,
                      digest=digest, writes=(_display(runtime, store.root / f"{name}.json"),),
                      name=name, followers=followers, running=running, is_default=is_default,
                      name_digest=name_digest)


def apply_delete(runtime: Any, plan: DeletePlan, confirmation: model.Confirmation) -> DeleteOutcome:
    """Keep a copy, remove the file, let followers keep their lineup (pinned),
    then put the default back to automatic when it named this profile."""

    model.check_apply(runtime, plan, confirmation, verb="profile rm")
    store = runtime.profiles
    try:
        backup = store.remove(plan.name, expected=plan.name_digest)
    except profile_mod.ProfileChangedError as exc:
        raise _changed(plan.name) from exc
    sink = io.StringIO()
    lineup_mod.on_removed(runtime, plan.name, renamed_to=None, out=sink)
    cleared = defaults.move_default(runtime, plan.name, None)
    lines = (DELETED.format(name=plan.name, backup=_display(runtime, backup)), *sink.getvalue().splitlines())
    return DeleteOutcome(backup, cleared, lines)


# ------------------------------------------------------------------ reseed


@dataclass(frozen=True)
class ReseedPlan(model.Plan):
    name: str
    kind: str  # the seed state's label
    nothing: bool  # already the shipped version: nothing to do
    version: int  # the shipped seed version
    diff: tuple[str, ...]
    followers: int
    name_digest: str | None


@dataclass(frozen=True)
class ReseedOutcome:
    name: str
    version: int
    backup: Path | None
    lines: tuple[str, ...]


def plan_reseed(runtime: Any, name: str) -> ReseedPlan:
    """Replace a shipped profile's file with its shipped version: the per-slot
    changes, and where your version is kept."""

    store = runtime.profiles
    _require(store, name)
    if not store.is_seed(name):
        raise model.Refused(NOT_A_SEED.format(name=name))
    seed_state = store.seed_state(name)
    name_digest = store.digest(name)
    followers, _running = _followers(runtime, name)
    shipped = runtime.catalog.seed_profiles[name]
    if seed_state.kind == "current":
        lines: tuple[str, ...] = (RESEED_NOTHING.format(name=name),)
        diff: tuple[str, ...] = ()
        nothing = True
    else:
        hint = _backup_hint(runtime, name, "pre-reseed")
        try:
            current = store.load(name)
        except profile_mod.ProfileError:
            diff = ()
            lines = (RESEED_UNLOADABLE.format(backup=hint),)
        else:
            diff = tuple(profile_mod.slot_diff(current, {**shipped, "name": name}))
            lines = (*diff, RESEED_KEEPS.format(backup=hint))
        nothing = False
    digest = model.digest_of("reseed", name, seed_state.label, name_digest)
    return ReseedPlan(op="reseed", subject=name, lines=lines, consent=None if nothing else "confirm",
                      guarded=False, digest=digest, writes=() if nothing else (
                          _display(runtime, store.root / f"{name}.json"),),
                      name=name, kind=seed_state.label, nothing=nothing, version=seed_state.shipped,
                      diff=diff, followers=followers, name_digest=name_digest)


def apply_reseed(runtime: Any, plan: ReseedPlan, confirmation: model.Confirmation) -> ReseedOutcome:
    """Keep the current file, write the shipped version and record the
    install; the caller then offers the change to following sessions."""

    model.check_apply(runtime, plan, confirmation, verb="profile reseed")
    if plan.nothing:
        return ReseedOutcome(plan.name, plan.version, None, plan.lines)
    try:
        _written, backup = runtime.profiles.restore_seed(plan.name, keep_copy=True, expected=plan.name_digest)
    except profile_mod.ProfileChangedError as exc:
        raise _changed(plan.name) from exc
    shown = _display(runtime, backup) if backup is not None else "(nothing to keep)"
    return ReseedOutcome(plan.name, plan.version, backup,
                         (RESEEDED.format(name=plan.name, v=plan.version, backup=shown),))


@dataclass(frozen=True)
class RefreshPlan(model.Plan):
    names: tuple[str, ...]
    versions: str  # "claude 2 → 3, openai 3 → 4"
    digests: Mapping[str, str | None]


def plan_refresh_unedited(runtime: Any) -> RefreshPlan:
    """Every shipped profile you have not changed whose shipped version is newer."""

    store = runtime.profiles
    names: list[str] = []
    versions: list[str] = []
    digests: dict[str, str | None] = {}
    for name in store.names():
        if not store.is_seed(name):
            continue
        # The judgement and the digest the apply re-checks come from the same bytes.
        seed_state, current = store.seed_snapshot(name)
        if seed_state.label != "unedited-stale" or current is None:
            continue
        names.append(name)
        versions.append(REFRESH_LINE.format(name=name, old=seed_state.installed, new=seed_state.shipped))
        digests[name] = current
    text = ", ".join(versions)
    digest = model.digest_of("refresh", sorted(digests.items()))
    return RefreshPlan(op="refresh", subject=", ".join(names), lines=tuple(versions),
                       consent="confirm" if names else None, guarded=False, digest=digest,
                       writes=tuple(_display(runtime, store.root / f"{name}.json") for name in names),
                       names=tuple(names), versions=text, digests=digests)


class RefreshPartial(model.SetupError):
    """A refresh stopped part-way: ``done`` names every profile whose file
    now holds the shipped version (each with a kept copy) — the one it
    stopped at too when only a later step of it failed (``failed_changed``)
    — and ``unchanged`` the planned ones that were not changed. The caller
    still offers the change of every one in ``done`` to following
    sessions."""

    def __init__(self, done: tuple[str, ...], failed: str, reason: BaseException, *,
                 failed_changed: bool = False, unchanged: tuple[str, ...] = ()):
        self.done = done
        self.failed = failed
        self.failed_changed = failed_changed
        self.unchanged = unchanged
        verb = "was" if len(unchanged) == 1 else "were"
        if failed_changed:
            template = REFRESH_UNFINISHED_REST if unchanged else REFRESH_UNFINISHED
        else:
            template = REFRESH_PARTIAL
        super().__init__(template.format(done=", ".join(done), failed=failed, reason=reason,
                                         unchanged=", ".join(unchanged), verb=verb))


def apply_refresh_unedited(runtime: Any, plan: RefreshPlan, confirmation: model.Confirmation) -> tuple[str, ...]:
    """Each one as a reseed (a copy kept), all or none: every planned
    profile is checked again under one store lock before any is written, so
    a profile changed meanwhile refuses the whole run with nothing written.
    A failure once a file was replaced raises :class:`RefreshPartial`
    naming every profile that changed, whichever step failed."""

    model.check_apply(runtime, plan, confirmation, verb="profile reseed")
    expected = {name: plan.digests.get(name) for name in plan.names}
    if any(digest is None for digest in expected.values()):
        raise _changed(next(name for name, digest in expected.items() if digest is None))
    try:
        done = runtime.profiles.refresh_unedited(expected)
    except profile_mod.ProfileChangedError as exc:
        raise _changed(exc.name) from exc
    except profile_mod.ProfileBatchError as exc:
        changed = exc.changed
        raise RefreshPartial(changed, exc.failed, exc.cause, failed_changed=exc.failed_changed,
                             unchanged=tuple(name for name in plan.names if name not in changed)) from exc
    return tuple(name for name, _kept in done)
