"""Converge and the record-authoritative expected plan.

There is one launch path (``launch.perform_launch``): a relaunch is a resume
with a target lineup, done in one locked launch, so no separate transition
engine exists. What lives here is repair:

- :func:`expected_plan` compiles the scope a v4 record (``lineup_generation
  >= 1``) implies against the installed catalog, without ever changing the
  record's intent: the non-launch-time files (agent files,
  ``lineup.md``, ``lineup.gen``) are compiled with the record's
  ``scope_lead``, and the launch-time items (``settings.json``
  ``availableModels``/``modelPicker``/``model``/``env`` and the
  ``lead-set.json`` bytes) are restaged from the live scope byte-for-byte
  whenever their ``launch_digest`` equals the record's ``launch_fence``
  the running process pinned exactly those, so a catalog that gained a
  line never widens a live session's fence.
- :func:`converge` (``doctor --repair``/``--repair-all``) loads the record
  under its lifecycle lock (inside the shared migration hold), fences a
  generation a crashed live apply published, and replaces a drifted
  or missing live scope through the ``.prev``/``.new`` swap
  (``scope.swap_scope``), never rmtree+rename.

Authority model (unchanged): the record is intent, the installed catalog is
the trusted source, the scope is re-derivable from both.
"""

from __future__ import annotations

import copy
import os
import stat
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import compiler, errors, managed, profile, scope, sessions, settings as settings_mod, state, strict_json
from .launch import disk_generation


class TransitionError(errors.ClaudeMultiError, RuntimeError):
    """Raised when converge cannot proceed (fail closed, nothing mutated)."""


# Environment sentinel compiled into every managed launch; the session's own
# agents and nested CLI invocations self-identify against it (a `lineup`
# request from inside the session is recorded as pending).
SESSION_ENV_VAR = "CLAUDE_MULTI_MANAGED_ID"
LEGACY_SESSION_ENV_VAR = "CLAUDE_MULTI_SESSION_ID"

# The exact relaunch command printed for a session.
COMMAND_TEMPLATE = (
    "claude-multi lineup --session {session_id} --relaunch profile {name}"
)

# The env value that differs between an epoch-bumped record (sessions link,
# resolve-fork) and the scope its launch committed.
_EPOCH_ENV = "CLAUDE_MULTI_LAUNCH_EPOCH"
_EPOCH_SEARCH_LIMIT = 64


def _check_real_scope_dir(path: Path, what: str) -> None:
    """Fail closed when an existing scope path is a symlink or not a directory."""

    if not os.path.lexists(path):
        return
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise TransitionError(f"{what} scope path {path} is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise TransitionError(f"{what} scope path {path} is not a directory")


# The drift walk lives in ``scope.live_drift``.
_live_drift = scope.live_drift


@dataclass(frozen=True)
class RuntimeParts:
    """What converge needs from ``cli.Runtime`` (transition never imports cli)."""

    docs: Mapping[str, Any]  # merged catalog ∪ custom
    prompt_bodies: Mapping[str, bytes]
    hook_command: str  # the shim-3 path v2 scopes embed
    token_helper_command: str
    environ: Mapping[str, str] | None = None
    managed_root: Path = managed.MANAGED_ROOT
    resolved_hook_command: str | None = None  # refreshes shim-3 when given
    worktree_available: Callable[[str], bool] | None = None  # None: git_work_tree
    feedback_drafts: str = settings_mod.FEEDBACK_DRAFTS_DEFAULT  # feedback-draft policy input


@dataclass(frozen=True)
class ExpectedPlan:
    plan: scope.ScopePlan | None  # None -> ``reason`` says why nothing may be rewritten
    reason: str | None
    catalog_plan: scope.ScopePlan | None  # the installed catalog with the scope lead
    launch_files_kept: bool  # live launch-time items proven by launch_fence and restaged
    rebuilt_fence_matches: bool | None  # not kept: the rebuilt digest equals launch_fence
    catalog_launch_differs: bool  # kept: the installed catalog compiles other launch items
    lead_switched: bool  # applied.lead (key, effort) != scope_lead (key, effort): info only
    rebuild_reason: str | None = None  # not kept: why the launch-time items were rebuilt
    # kept: the ONLY launch-item difference is the feedback-draft preference:
    # doctor Attention "managed-session preference changed", never BLOCK.
    preference_launch_differs: bool = False
    # Unchanged agent bindings whose recorded selector the installed catalog
    # and the session window move only by their context class (200K <-> 1M)
    # keep the recorded selector until resume: ``(slot label, recorded,
    # installed)`` rows (doctor's pending-move notice; Attention, never drift).
    agent_class_kept: tuple[tuple[str, str, str], ...] = ()


def agent_class_rows(
    before: profile.ResolvedLineup, after: profile.ResolvedLineup
) -> tuple[tuple[str, str, str], ...]:
    """``(label, kept selector, installed selector)`` per slot
    :func:`profile.keep_recorded_agent_class` kept."""

    rows = []
    for rid, agent in after.agents.items():
        installed = before.agents.get(rid)
        if installed is not None and installed.binding.selector != agent.binding.selector:
            rows.append((profile.label(rid), agent.binding.selector, installed.binding.selector))
    return tuple(rows)


def _m8(record: Mapping[str, Any]) -> str:
    return sessions.managed_id(dict(record))[:8]


def lead_display(lcat: profile.LineupCatalog, ref: Mapping[str, Any]) -> str:
    """``{display} · {effort}`` of a lead reference (unknown key -> the key)."""

    entry = lcat.lines.get(ref["key"]) or lcat.retired.get(ref["key"]) or {}
    display = entry.get("display") if isinstance(entry, Mapping) else None
    return f"{display or ref['key']} · {ref['effort']}"


def _restated(record: dict[str, Any], lead: Mapping[str, Any]) -> dict[str, Any]:
    document = sessions.applied_document(record)
    document["lead"] = {"model": lead["key"], "effort": lead["effort"]}
    return document


def _evaluate(
    record: dict[str, Any],
    document: dict[str, Any],
    lcat: profile.LineupCatalog,
    eff: settings_mod.Effective,
) -> profile.Evaluation:
    return profile.evaluate(
        document, lcat, bindings=None, effective=eff, ad_hoc=record["profile"] is None
    )


def _compile(
    record: dict[str, Any],
    lineup: profile.ResolvedLineup,
    lcat: profile.LineupCatalog,
    eff: settings_mod.Effective,
    *,
    docs: Mapping[str, Any],
    prompt_bodies: Mapping[str, bytes],
    hook_command: str,
    token_helper_command: str,
    worktree_available: bool,
    secret_denies: tuple[str, ...] = scope.SECRET_PATH_DENIES,
    proxy_env: Mapping[str, str] | None = None,
    feedback_drafts: str = settings_mod.FEEDBACK_DRAFTS_DEFAULT,
    default_mode: str | None = None,
) -> scope.ScopePlan:
    return scope.compile_lineup_scope(
        lineup,
        lcat,
        eff,
        prompt_bodies,
        scope.catalog_meta_v2(docs),
        lineup_generation=record["lineup_generation"],
        managed_id=sessions.managed_id(record),
        hook_command=hook_command,
        launch_epoch=record.get("launch_epoch", 0),
        token_helper_command=token_helper_command,
        no_subagents=bool(record["applied"]["no_subagents"]),
        worktree_available=worktree_available,
        secret_denies=secret_denies,
        proxy_env=proxy_env,
        feedback_drafts=feedback_drafts,
        default_mode=default_mode,
    )


def _without_preference(items: Mapping[str, Any]) -> dict[str, Any]:
    """Launch items minus the feedback-draft preference env key."""

    stripped = copy.deepcopy(dict(items))
    env = stripped.get("env")
    if isinstance(env, dict):
        env.pop(scope.FEEDBACK_ENV, None)
    return stripped


@dataclass(frozen=True)
class _ProvenLeadSet:
    """The launch-time lead set as ``scope.lineup_md_bytes`` reads a ``Fence``."""

    lead_set: tuple[Any, ...]
    lead_selectors: frozenset[str]


def _launch_lineup_files(
    record: dict[str, Any],
    lineup: profile.ResolvedLineup,
    eff: settings_mod.Effective,
    live_settings: Mapping[str, Any],
    live_lead_set: bytes,
    worktree_available: bool,
) -> dict[str, bytes]:
    """``lineup.md``/``lineup.gen`` stated against the proven launch-time items.

    ``lineup.md`` names the lead-set size, whether ``/model``'s built-in
    Default row is in it and the workflow default: all launch-time facts
    (``lead-set.json``, settings ``env``). When the launch files are proven
    (``launch_fence``) they, not the installed catalog, are what the running
    process pinned, so a catalog that gained a line in the lead class never
    rewrites these lines under it (and never reads as drift). ``{}`` (keep
    the catalog compile) when the proven files do not parse.
    """

    try:
        rows = strict_json.loads(live_lead_set)["rows"]
        fence = _ProvenLeadSet(
            lead_set=tuple(rows),
            lead_selectors=frozenset(str(row["selector"]) for row in rows),
        )
        env = live_settings.get("env") or {}
        lineup_md = scope.lineup_md_bytes(
            lineup,
            fence,  # type: ignore[arg-type]
            eff,
            generation=record["lineup_generation"],
            no_subagents=bool(record["applied"]["no_subagents"]),
            default_row_model=env.get(scope._OPUS_DEFAULT_ENV),
            workflow_default=env.get("CLAUDE_CODE_SUBAGENT_MODEL"),
            worktree_available=worktree_available,
        )
    except (KeyError, TypeError, AttributeError, strict_json.StrictJSONError, scope.ScopeError):
        return {}
    return {
        scope.LINEUP_MD: lineup_md,
        scope.LINEUP_GEN: scope.lineup_gen_line(record["lineup_generation"], lineup_md).encode("utf-8"),
    }


def _launch_items(settings: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(settings[key])
        for key in sessions.LAUNCH_TIME_SETTINGS_KEYS
        if key in settings
    }


def _with_epoch(settings: Mapping[str, Any], epoch: Any) -> dict[str, Any]:
    """A copy whose ``env`` states ``epoch`` (the proven launch epoch)."""

    updated = copy.deepcopy(dict(settings))
    env = updated.get("env")
    if isinstance(env, dict) and _EPOCH_ENV in env and epoch is not None:
        env[_EPOCH_ENV] = str(epoch)
    return updated


def _read_live_launch_files(live: Path | None) -> tuple[dict[str, Any] | None, bytes | None, str]:
    """``(settings, lead-set bytes, why)`` of a live scope; None parts are unreadable."""

    if live is None or not os.path.lexists(live):
        return None, None, "the live scope is missing"
    try:
        settings = strict_json.loads(state.read_private(Path(live) / scope.SETTINGS_RELPATH))
    except (OSError, state.StateError, strict_json.StrictJSONError):
        settings = None
    if not isinstance(settings, dict):
        return None, None, "settings.json is missing or unreadable"
    try:
        lead_set = state.read_private(Path(live) / scope.LEAD_SET_JSON)
    except (OSError, state.StateError):
        return settings, None, "lead-set.json is missing or unreadable"
    return settings, lead_set, ""


def recorded_operator_admissions(
    record: Mapping[str, Any], lcat: profile.LineupCatalog, eff: settings_mod.Effective,
) -> settings_mod.Effective:
    """Record authority for operator lines.

    Record-authority compiles (expected_plan: doctor, converge, live lineup)
    use the snapshot's ``admitted_lines`` and never the current ledger. A
    migrated line (``operator-migrated``) whose ``legacy_key`` or selector
    matches a key or selector the record applied counts as admitted: the
    legacy custom line it replaced was active, so no snapshot names it.
    """

    applied = record.get("applied") or {}
    bindings = [applied.get("lead") or {}, *((applied.get("agents") or {}).values())]
    for reference in (record.get("scope_lead"), record.get("lead_target")):
        if isinstance(reference, Mapping):
            bindings.append(reference)
    keys = {b.get("key") for b in bindings if isinstance(b, Mapping)}
    selectors = {b.get("selector") for b in bindings if isinstance(b, Mapping)}
    extra = set()
    for key, meta in lcat.operator.items():
        if meta.get("origin") != "operator-migrated" or key in eff.admitted_lines:
            continue
        entry = lcat.lines.get(key)
        selector = entry.get("selector") if isinstance(entry, Mapping) else None
        if key in keys or meta.get("legacy_key") in keys or (selector is not None and selector in selectors):
            extra.add(key)
    if not extra:
        return eff
    return dataclasses.replace(eff, admitted_lines=frozenset(eff.admitted_lines | extra))


def record_agent_gate(
    record: Mapping[str, Any], eff: settings_mod.Effective, facts: Mapping[str, Any] | None = None,
) -> profile.AgentGate:
    """Record authority for operator agents (``record`` mode):
    the recorded (key, effort) bindings and the snapshot's workflow default
    keep their grant; no mutable evidence or ledger fact revokes them."""

    applied = record.get("applied") or {}
    recorded = {
        rid: (binding.get("key"), binding.get("effort"))
        for rid, binding in (applied.get("agents") or {}).items()
        if isinstance(binding, Mapping)
    }
    workflow = eff.workflow_default_binding
    return profile.AgentGate(
        profile.AGENT_MODE_RECORD, dict(facts or {}), recorded,
        (workflow["model"], workflow["effort"]) if isinstance(workflow, Mapping) else None,
    )


def expected_plan(
    record: dict[str, Any],
    *,
    docs: Mapping[str, Any],
    prompt_bodies: Mapping[str, bytes],
    state_root: Path | str,
    hook_command: str,
    token_helper_command: str,
    live: Path | None,
    worktree_available: bool | None = None,
    environ: Mapping[str, str] | None = None,
    managed_root: Path = managed.MANAGED_ROOT,
    feedback_drafts: str = settings_mod.FEEDBACK_DRAFTS_DEFAULT,
) -> ExpectedPlan:
    """The record-authoritative scope of a v4 gen >= 1 record (pure reads).

    ``feedback_drafts`` is the current feedback-draft preference (an
    explicit input, never a record field): it shapes the catalog compile
    only; proven launch files keep what the launch pinned.

    Never changes intent: the scope lead and the recorded agents are
    evaluated against the installed catalog with the record's own Settings
    snapshot; any evaluation error or moved selector refuses (``plan`` None
    with the reason). Launch-time items proven by ``launch_fence`` are
    restaged from ``live``; otherwise they are rebuilt from the catalog.
    """

    del state_root  # the scope directory is ``live``; kept for the documented signature
    if record.get("version") != sessions.RECORD_VERSION or record.get("lineup_generation", 0) < 1:
        raise TransitionError(
            f"session {_m8(record)}: expected_plan needs a current-format record with a compiled scope"
        )
    mid = sessions.managed_id(record)
    applied = record["applied"]
    scope_lead = record["scope_lead"]
    lead_switched = (applied["lead"]["key"], applied["lead"]["effort"]) != (
        scope_lead["key"],
        scope_lead["effort"],
    )
    lcat = profile.LineupCatalog.from_docs(docs)
    try:
        # The window the session launched with decides each agent's class.
        eff = settings_mod.effective_from_snapshot(
            applied["settings"], window_ceiling=sessions.recorded_window(record))
    except settings_mod.SettingsError as exc:
        return ExpectedPlan(
            None,
            f"the record's Settings snapshot is unreadable ({exc}); it applies at the next "
            f"resume (claude-multi -r {mid})",
            None, False, None, False, lead_switched,
        )
    eff = recorded_operator_admissions(record, lcat, eff)
    lcat = lcat.with_gate(record_agent_gate(record, eff))
    evaluation = _evaluate(record, _restated(record, scope_lead), lcat, eff)
    if evaluation.errors:
        return ExpectedPlan(
            None,
            "the installed catalog no longer evaluates the recorded lineup ("
            + "; ".join(evaluation.errors)
            + f"); it applies at the next resume (claude-multi -r {mid})",
            None, False, None, False, lead_switched,
        )
    lineup = profile.keep_recorded_agent_class(evaluation.lineup, applied["agents"], lcat)
    class_kept = agent_class_rows(evaluation.lineup, lineup)
    moved: list[tuple[str, str, str]] = []
    if lineup.lead.binding.selector != scope_lead["selector"]:
        moved.append(("lead", lineup.lead.binding.selector, str(scope_lead["selector"])))
    for rid in catalog_role_order(set(applied["agents"]) | set(lineup.agents)):
        old = applied["agents"].get(rid, {}).get("selector") or "(unbound)"
        agent = lineup.agents.get(rid)
        new = agent.binding.selector if agent is not None else "(unbound)"
        if old != new:
            moved.append((profile.label(rid), new, old))
    if moved:
        label, new, old = moved[0]
        return ExpectedPlan(
            None,
            f"the installed catalog resolves {label} to {new} (recorded {old}); it applies "
            f"at the next resume (claude-multi -r {mid})",
            None, False, None, False, lead_switched,
        )
    if worktree_available is None:
        worktree_available = compiler.git_work_tree(record["cwd"])
    live_settings, live_lead_set, why = _read_live_launch_files(live)
    layers = managed.env_layers(environ, record["cwd"], managed_root) if environ and environ.get("HOME") else []
    # A permission-mode defect invalidates the launch files, but must not
    # erase a still-readable bypass while repairing those files.
    previous = managed.settings_env(live / scope.SETTINGS_RELPATH) if live is not None else {}
    bypass = compiler.loopback_no_proxy(environ or {}, *(env for _, env in layers),
                                       previous=previous if isinstance(previous, dict) else {})
    compile_kw = dict(
        docs=docs,
        prompt_bodies=prompt_bodies,
        hook_command=hook_command,
        token_helper_command=token_helper_command,
        worktree_available=worktree_available,
        secret_denies=scope.secret_path_denies(environ or {}),
        proxy_env=bypass,
        feedback_drafts=feedback_drafts,
        # A live scope keeps its own on-disk presence/absence of
        # permissions.defaultMode; only launch and resume decide from layers.
        default_mode=scope.live_default_mode(live),
    )
    catalog_plan = _compile(record, lineup, lcat, eff, **compile_kw)
    fence = record.get("launch_fence")
    proven = (
        live_settings is not None
        and live_lead_set is not None
        and sessions.launch_digest(live_settings, live_lead_set) == fence
    )
    if proven:
        settings_out = {
            key: value
            for key, value in catalog_plan.settings.items()
            if key not in sessions.LAUNCH_TIME_SETTINGS_KEYS
        }
        settings_out.update(_launch_items(live_settings))
        if bypass:
            settings_out["env"] = {**settings_out.get("env", {}), **bypass}
        plan = scope.ScopePlan(
            agent_files=dict(catalog_plan.agent_files),
            settings=settings_out,
            other_files={
                **catalog_plan.other_files,
                scope.LEAD_SET_JSON: bytes(live_lead_set),
                **_launch_lineup_files(
                    record, lineup, eff, live_settings, live_lead_set, worktree_available
                ),
            },
        )
        # What the installed catalog compiles for the LAUNCH lead (proven by
        # the digest), with the env epoch the launch stated.
        differs = True
        preference_only = False
        try:
            launch_lead = strict_json.loads(live_lead_set)["lead"]
            if (launch_lead["key"], launch_lead["effort"]) == (
                scope_lead["key"],
                scope_lead["effort"],
            ):
                reference = catalog_plan
            else:
                launch_eval = _evaluate(
                    record, _restated(record, launch_lead), lcat, eff
                )
                reference = (
                    None
                    if launch_eval.errors
                    else _compile(record, launch_eval.lineup, lcat, eff, **compile_kw)
                )
            if reference is not None:
                epoch = (live_settings.get("env") or {}).get(_EPOCH_ENV)
                ref_settings = _with_epoch(reference.settings, epoch)
                same_lead_set = reference.other_files.get(scope.LEAD_SET_JSON) == live_lead_set
                differs = not (
                    _launch_items(ref_settings) == _launch_items(live_settings) and same_lead_set
                )
                preference_only = differs and same_lead_set and (
                    _without_preference(_launch_items(ref_settings))
                    == _without_preference(_launch_items(live_settings))
                )
        except (
            KeyError,
            TypeError,
            strict_json.StrictJSONError,
            profile.ProfileError,
            compiler.CompilerError,
            scope.ScopeError,
        ):
            differs = True
        return ExpectedPlan(plan, None, catalog_plan, True, None, differs, lead_switched,
                            preference_launch_differs=preference_only, agent_class_kept=class_kept)

    if not why:
        why = "launch-time items were edited after launch"
    plan = catalog_plan
    matches = False
    lead_set = catalog_plan.other_files.get(scope.LEAD_SET_JSON, b"")
    for epoch in range(
        record.get("launch_epoch", 0),
        max(-1, record.get("launch_epoch", 0) - _EPOCH_SEARCH_LIMIT),
        -1,
    ):
        candidate = _with_epoch(catalog_plan.settings, epoch)
        if sessions.launch_digest(candidate, lead_set) == fence:
            # The launch bytes, restated with the epoch that launch committed
            # (a link/resolve-fork bump moves only the hooks).
            plan = scope.ScopePlan(
                agent_files=dict(catalog_plan.agent_files),
                settings=candidate,
                other_files=dict(catalog_plan.other_files),
            )
            matches = True
            break
    return ExpectedPlan(plan, None, catalog_plan, False, matches, False, lead_switched, why,
                        agent_class_kept=class_kept)


def catalog_role_order(ids: set[str]) -> list[str]:
    """Agent ids in ``AGENT_ROLE_IDS`` order (unknown ids last, sorted)."""

    from . import catalog as catalog_mod

    order = {rid: index for index, rid in enumerate(catalog_mod.AGENT_ROLE_IDS)}
    return sorted(ids, key=lambda rid: (order.get(rid, len(order)), rid))


def converge(
    state_root: Path | str,
    store: sessions.SessionStore,
    session_id: str,
    *,
    runtime_parts: RuntimeParts,
    dir_fsync: Callable[[Path], None] | None = None,
    barrier: state.BarrierToken | None = None,
) -> list[str]:
    """Converge a session's scope to record authority (``doctor --repair``).

    ``doctor --repair``/``--repair-all`` pass their held
    served-change token; converge asserts it and takes no migration hold of
    its own (it never acquires the barrier).

    Runs inside the shared migration hold (``MigrationBusyError`` propagates:
    the caller prints ``skipped … migration or restore in progress``) and the
    lifecycle lock, lock-then-load. v1-3 records and gen-0 v4 records are
    reported, never rewritten. Returns human report lines; raises
    :class:`TransitionError`/``SessionError``/``ScopeError`` fail-closed.
    """

    if Path(state_root) != Path(store.root):
        raise TransitionError("state_root does not match the session store root")
    if not sessions.UUID4.fullmatch(session_id):
        raise TransitionError(f"session id {session_id!r} is not a managed-session UUID")
    sync = dir_fsync if dir_fsync is not None else scope._fsync_directory
    m8 = session_id[:8]
    parts = runtime_parts
    report: list[str] = []
    prev_retained = False
    with sessions.phase_or_guard(store.root, barrier):
        lock = store.lifecycle_lock(session_id)
        lock.acquire(blocking=True)
        try:
            record = store.load(session_id)
            if record.get("version") != sessions.RECORD_VERSION:
                return [
                    f"session {m8} is a legacy record; run claude-multi migrate (or resume it) first"
                ]
            if record["lineup_generation"] == 0:
                return [f"session {m8} runs a legacy scope; it converges at its next launch with this launcher"]
            scopes_root = state.ensure_private_dir(Path(store.root) / "scopes")
            live = scopes_root / session_id
            prev = scopes_root / f".{session_id}.prev"
            staging = scopes_root / f".{session_id}.new"
            _check_real_scope_dir(live, "live")
            _check_real_scope_dir(prev, "previous-generation")
            if parts.resolved_hook_command:
                sessions.check_state_marker(Path(store.root))
                scope.ensure_hook_shim_v3(store.root, parts.resolved_hook_command)
            generation = record["lineup_generation"]
            report.append(f"record lineup generation {generation} is authoritative")
            disk_n = disk_generation(live) if os.path.lexists(live) else 0
            if disk_n > generation:
                record = {
                    **record,
                    "lineup_generation": disk_n + 1,
                    "mutation_token": sessions.new_mutation_token(),
                }
                store.save(record)
                report.append(
                    f"lineup generation fenced {generation} → {disk_n + 1} (a crashed live "
                    f"apply published {disk_n})"
                )
            worktree = (parts.worktree_available or compiler.git_work_tree)(record["cwd"])

            def plan_for(target: Path | None) -> ExpectedPlan:
                return expected_plan(
                    record,
                    docs=parts.docs,
                    prompt_bodies=parts.prompt_bodies,
                    state_root=store.root,
                    hook_command=parts.hook_command,
                    token_helper_command=parts.token_helper_command,
                    live=target,
                    worktree_available=worktree,
                    environ=parts.environ,
                    managed_root=parts.managed_root,
                    feedback_drafts=parts.feedback_drafts,
                )

            ep = plan_for(live if os.path.lexists(live) else None)
            if ep.plan is None:
                report.append(str(ep.reason))
                return report
            if not os.path.lexists(live) and os.path.lexists(prev):
                # Crash between the two swap renames: the record still names
                # the prior generation, so the .prev content is truth.
                os.rename(prev, live)
                sync(scopes_root)
                report.append(
                    "live scope was missing; restored .prev as the live scope "
                    "(crash between swap renames)"
                )
                ep = plan_for(live)
                if ep.plan is None:
                    report.append(str(ep.reason))
                    return report
            rebuilt = False
            if not os.path.lexists(live):
                scope.swap_scope(store.root, session_id, ep.plan, dir_fsync=dir_fsync)
                rebuilt = True
                report.append(
                    "live scope was missing; recompiled from the record and the installed catalog"
                )
            else:
                drift = _live_drift(live, ep.plan)
                if drift:
                    scope.swap_scope(store.root, session_id, ep.plan, dir_fsync=dir_fsync)
                    rebuilt = True
                    report.append(
                        "live scope drifted from record authority "
                        f"({'; '.join(drift)}); recompiled from the record and the "
                        "installed catalog"
                    )
                else:
                    report.append("live scope matches the record-authoritative compile")
            if rebuilt and not ep.launch_files_kept and not ep.rebuilt_fence_matches:
                report.append(
                    f"launch-time files rebuilt from the installed catalog ({ep.rebuild_reason}); "
                    f"live agent changes for {m8} need a relaunch until its next launch"
                )
            if ep.lead_switched:
                lcat = profile.LineupCatalog.from_docs(parts.docs)
                report.append(
                    "lead switched with /model since the last compile (scope states "
                    f"{lead_display(lcat, record['scope_lead'])}, the session runs "
                    f"{lead_display(lcat, record['applied']['lead'])}); restated at the next resume"
                )
            if os.path.lexists(staging):
                scope._remove_tree(staging)
                sync(scopes_root)
                report.append("removed stale staged scope .new from an interrupted launch")
            prev_retained = os.path.lexists(prev)
        finally:
            lock.release()
    if prev_retained:
        report.append("prior generation retained at .prev; doctor --prune removes it")
    return report
