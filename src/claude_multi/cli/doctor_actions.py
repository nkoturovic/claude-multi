"""Doctor actions: token rotation, prune, alias prune and repair.

Moved verbatim from the CLI module.
"""

from __future__ import annotations

from claude_multi import compiler
from claude_multi import continuity as continuity_mod
from claude_multi import errors as cli_errors
from claude_multi import hooks
from claude_multi import managed
from claude_multi import profile as profile_mod
from claude_multi import proxy as proxy_mod
from claude_multi import render
from claude_multi import scope as scope_mod
from claude_multi import sessions
from claude_multi import state
from claude_multi import termtext
import claude_multi.cli.doctor as doctor
import claude_multi.cli.runtime as runtime_mod
import claude_multi.cli.session_actions as session_actions
import claude_multi.cli.session_facts as session_facts
import errno
import os
import shutil
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Callable, Mapping
    from types import ModuleType
    from claude_multi import transition as transition_mod
    from typing import TextIO


def _pre_helper_session_labels(runtime: runtime_mod.Runtime) -> list[str]:
    """Records of sessions that may still hold an environment credential
    (names only).

    A launch that was not helper-only (``sessions.helper_only_launch``)
    without a SessionEnd counts as live (heuristic — SessionEnd may never
    fire); an unreadable record is unknown, so it is listed too: rotation
    must never treat unknown state as dead.
    """

    records, _problems, record_ids = session_facts._session_record_scan(runtime)
    prefixes = session_facts._live_background_prefixes()
    labels: list[str] = []
    for record in records:
        if not sessions.may_hold_env_token(record):
            continue
        event = record.get("last_event_source") or "no hook yet"
        daemon = ", ● daemon-live" if session_facts._record_is_live(record, prefixes) else ""
        labels.append(
            f"{sessions.managed_id(record)} (launcher "
            f"{record['launcher_version']}, last event {event}{daemon})"
        )
    readable = {sessions.managed_id(record) for record in records}
    for session_id in sorted(record_ids - readable):
        labels.append(f"{session_id} (unreadable record — liveness unknown)")
    return labels


_ROTATION_DEAD_CONFIRM_PREFIX = proxy_mod.ROTATION_ENV_CREDENTIAL_PREFIX


def _doctor_rotate_token(
    runtime: runtime_mod.Runtime,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
    terminal_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """`doctor --rotate-token`: the dual-key rotation runbook.

    Drives proxy.rotate_token: publish [old, new] and prove the reload
    (sentinel + HTTP 200 with the candidate), switch the helper's key, wait
    one helper TTL, confirm no session that may still hold an environment
    credential remains, then retire the old key and prove old=401 / new=200. Every step
    is crash-reentrant (rerunning resumes); tokens are never printed. The
    loopback probe goes through the Runtime seam, so tests never reach the
    running gateway.

    Streams: the explanation, the questions and the progress of the wait go
    to ``terminal_stream`` (the conversation with the person); the result
    (rotated, or not started) to ``output_stream``; a pause or an interrupt,
    with how to resume, to ``error_stream``. Either one omitted is
    ``output_stream`` (the TUI passes its one terminal stream).
    """

    terminal = terminal_stream or output_stream
    problems = error_stream or output_stream

    if not interactive:
        raise cli_errors.CLIError(
            "doctor --rotate-token is an interactive operator flow (it may "
            "ask to confirm a gateway restart or that sessions that may still hold "
            "an environment credential are gone); run it from a terminal"
        )
    # A credential key in Claude settings outranks the
    # session's apiKeyHelper, so those sessions would keep (and lose) the old
    # token whatever the rotation does. Names only.
    credential_hits = doctor._settings_radar(runtime, session_facts._session_records(runtime)).credential_hits
    if credential_hits:
        raise cli_errors.CLIError(
            "doctor --rotate-token refused: Claude settings set a gateway credential "
            "that outranks the session apiKeyHelper ("
            + "; ".join(credential_hits)
            + "); remove it, then rerun"
        )

    def ask(question: str) -> bool:
        terminal.write(f"{termtext.visible_text(question)} [y/N] ")
        terminal.flush()
        answer = input_stream.readline()
        terminal.write("\n" if not answer.endswith("\n") else "")
        return answer.strip().lower() in ("y", "yes")

    def write(stream: TextIO, line: str) -> None:
        stream.write(f"{termtext.visible_text(line)}\n")
        stream.flush()

    def say(line: str) -> None:
        write(terminal, line)

    def models_get(_base_url: str, token: str) -> tuple[int, set[str]]:
        ids, status = runtime.served_models(token)
        return (status or 0), set(ids or ())

    resuming = os.path.lexists(proxy_mod.config_dir(runtime.home) / "previous-key")
    labels = _pre_helper_session_labels(runtime)
    say(
        "Gateway token rotation "
        + ("(resuming the in-progress rotation)" if resuming else "(hitless, dual-key)")
        + ": the gateway accepts the current and a new key, the token helper "
        "switches to the new key, and after one helper TTL "
        f"(~{proxy_mod.HELPER_TTL_SECONDS / 60:.0f} min) the old key is "
        "retired. Loopback checks only; no token is ever shown."
    )
    if labels:
        say(
            f"{len(labels)} session(s) may still hold an environment credential "
            "(you will be asked again before the old key is retired):"
        )
        for label in labels:
            say(f"  - {label}")
    if not ask("Rotate the local gateway token now?"):
        write(output_stream, "rotation not started; nothing changed")
        return 1
    environ = {**runtime.environ, "CLAUDE_MULTI_ASSETS": str(runtime.asset_root)}

    def confirm(question: str) -> bool:
        # The dead-confirm answer is recorded, so the next
        # rotation (and restore-2x) no longer counts those sessions live.
        answer = ask(question)
        if answer and question.startswith(_ROTATION_DEAD_CONFIRM_PREFIX):
            ids = [
                label.split(" ", 1)[0]
                for label in _pre_helper_session_labels(runtime)
                if "unreadable record" not in label and "daemon-live" not in label
            ]
            for managed, status in session_actions._mark_ended_ids(runtime, ids):
                say(f"  {managed}: {status}")
                if status not in ("marked ended", "already ended"):
                    answer = False  # a refusal cannot authorize retiring the previous key
        return answer

    try:
        outcome = proxy_mod.rotate_token(
            runtime.home,
            live_sessions=lambda: _pre_helper_session_labels(runtime),
            environ=environ,
            models_get=None if runtime.served_models_callback is None else models_get,
            confirm=confirm,
            progress=say,
            state_root=runtime.session_store.root,
            **({} if clock is None else {"clock": clock}),
            **({} if sleep is None else {"sleep": sleep}),
        )
    except KeyboardInterrupt:
        write(problems,
              "interrupted — the rotation state is resumable: rerun "
              "`claude-multi doctor --rotate-token`")
        return 130
    except (proxy_mod.ProxyError, render.RenderError) as exc:
        write(problems, f"rotation paused: {exc}")
        if os.path.lexists(proxy_mod.config_dir(runtime.home) / "previous-key"):
            write(problems, "resume with `claude-multi doctor --rotate-token`")
        return 1
    write(output_stream, outcome.message)
    write(output_stream, "gateway token rotated: the previous key is retired (old=401, new=200)")
    return 0


def _remove_scope_tree(path: Path) -> None:
    """Remove a scope tree; refuse symlinks and non-directories.

    Mirrors scope.py's own removal guards; used only for ``.<uuid>.prev``
    staging dirs, which have no store-level removal API.
    """

    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise state.StateError(errno.ELOOP, f"scope path {path} is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise state.StateError(
            errno.ENOTDIR, f"scope path {path} is not a directory"
        )
    shutil.rmtree(path)


def _doctor_prune_aliases(
    runtime: runtime_mod.Runtime, requested: list[str], output_stream: TextIO,
) -> int:
    """`doctor --prune-aliases [ALIAS ...]`.

    Drives ``proxy.prune_aliases`` with the runtime HOME and session state
    root: removes (and tombstones) continuity aliases no live session record
    references, re-renders the gateway config and verifies the reload
    through the Runtime loopback seam. Records are never modified.

    A shared-preflight mutator. Before any
    lock it plans the complete post-prune render (the proposed continuity
    and capture removals plus every pending declaration change) against the
    published one and shows it. Anything served beyond this prune's own
    removals (an out-of-band retarget, an addition, an unknown published
    render) refuses — publish it first through ``claude-multi providers
    apply``, which previews it. The commit phase revalidates the plan's
    inputs (taken before the preview) and the prune itself under its locks.
    """

    import hashlib

    import claude_multi.cli.commands.providers as providers_cmd
    from claude_multi import served_plan

    sessions.check_state_marker(runtime.session_store.root)

    def models_get(_base_url: str, token: str) -> tuple[int, set[str]]:
        ids, status = runtime.served_models(token)
        return (status or 0), set(ids or ())

    environ = {**runtime.environ, "CLAUDE_MULTI_ASSETS": str(runtime.asset_root)}
    # The process evidence is read before any lock; the prune
    # re-reads each ended holder under its lifecycle lock and applies it.
    daemon = runtime.background_liveness()
    scan = sessions.proc_session_scan(runtime.proc_root)
    process_known = bool(daemon.known and scan.known)

    def is_process_live(view: Mapping[str, Any]) -> bool:
        return session_facts._record_view_is_live(dict(view), prefixes=daemon.prefixes, proc_ids=scan.ids)

    def inputs_sample() -> str:
        try:
            continuity_bytes = state.read_private(continuity_mod.path(runtime.home))
        except state.StateError as exc:
            continuity_bytes = f"<{exc.errno}>".encode()
        return providers_cmd._sample(runtime) + ":" + hashlib.sha256(continuity_bytes).hexdigest()

    sample = inputs_sample()  # before any plan read: the plan can only be newer
    preview = proxy_mod.prune_preview(
        runtime.home, runtime.session_store.root, requested, environ=environ,
        is_process_live=is_process_live, process_known=process_known,
    )
    if preview.outcome is not None:
        for line in preview.outcome.lines:
            output_stream.write(f"{termtext.visible_text(line)}\n")
        return preview.outcome.code
    assert preview.prune is not None and preview.served is not None
    for line in preview.served.lines():
        output_stream.write(f"{termtext.visible_text(line)}\n")
    refusal = preview.served.refusal()
    if refusal is not None:
        output_stream.write(f"prune-aliases refused: {termtext.visible_text(refusal)}; nothing pruned\n")
        return 1
    if preview.unrelated():
        output_stream.write(
            "prune-aliases refused: the render would also publish the served changes above that are "
            "not part of this prune; review them with claude-multi plan and publish them with "
            "claude-multi providers apply first — nothing pruned\n")
        return 1
    expected = (preview.prune.remove_continuity, preview.prune.remove_captures,
                served_plan.references_from_scan(preview.prune.scan).fingerprint())

    def revalidate(planned: proxy_mod.AliasPrunePlan) -> str | None:
        current = (planned.remove_continuity, planned.remove_captures,
                   served_plan.references_from_scan(planned.scan).fingerprint())
        if current != expected or inputs_sample() != sample:
            return served_plan.CHANGED_REFUSAL
        return None

    try:
        # One served-change phase: migration guard -> barrier.
        with sessions.served_change_phase(runtime.session_store.root, runtime.home) as barrier:
            outcome = proxy_mod.prune_aliases(
                runtime.home,
                runtime.session_store.root,
                requested,
                environ=environ,
                models_get=None if runtime.served_models_callback is None else models_get,
                barrier=barrier,
                is_process_live=is_process_live,
                process_known=process_known,
                revalidate=revalidate,
            )
    except sessions.MigrationBusyError:
        output_stream.write("prune-aliases skipped: migration or restore in progress\n")
        return 1
    except state.BarrierBusyError as exc:
        output_stream.write(f"prune-aliases refused: {exc.strerror}; nothing pruned\n")
        return 1
    except (proxy_mod.ProxyError, render.RenderError, continuity_mod.ContinuityError) as exc:
        output_stream.write(f"prune-aliases failed: {termtext.visible_text(str(exc))}\n")
        return 1
    for line in outcome.lines:
        output_stream.write(f"{termtext.visible_text(line)}\n")
    return outcome.code


def _doctor_prune(runtime: runtime_mod.Runtime, output_stream: TextIO) -> int:
    """Remove stale scopes. Generated files only.

    Prunes ``scopes/.<uuid>.new`` / ``scopes/.<uuid>.prev`` staging dirs and
    live scopes whose records are gone. Anything with a living record, and
    any entry that is not a recognized scope dir, is left untouched.
    """

    store = runtime.session_store
    sessions.check_state_marker(store.root)
    scopes_root = store.root / "scopes"
    removed: list[str] = []
    if os.path.lexists(scopes_root):
        state.ensure_private_dir(scopes_root)
        for entry in sorted(scopes_root.iterdir()):
            name = entry.name
            session_id: str | None = None
            staging = False
            staging_suffix: str | None = None
            if name.startswith("."):
                stem = name[1:]
                for suffix in (".new", ".prev"):
                    candidate = stem[: -len(suffix)] if stem.endswith(suffix) else ""
                    if candidate and sessions.UUID4.fullmatch(candidate):
                        session_id = candidate
                        staging = True
                        staging_suffix = suffix
                        break
            elif sessions.UUID4.fullmatch(name):
                session_id = name
            if session_id is None:
                continue
            lock = store.lifecycle_lock(session_id)
            lock.acquire(blocking=True)
            try:
                if not os.path.lexists(entry):
                    continue
                if staging:
                    if store.exists(session_id):
                        # Retain rollback state until the session is forgotten;
                        # there is no separate post-exec success signal.
                        continue
                    _remove_scope_tree(entry)
                    removed.append(f"stale staging dir scopes/{name}")
                elif not store.exists(session_id):
                    scope_mod.remove_scope(store.root, session_id)
                    removed.append(f"scope for forgotten session {session_id}")
            finally:
                lock.release()
    # Generated per-session files outside scopes/ follow the same rule:
    # pruned only when their record is gone (never a living session's).
    for entry in sorted(store.root.glob("lead-prompt-*-*.md")):
        stem = entry.name.removeprefix("lead-prompt-").removesuffix(".md")
        # lead-prompt-<digest16>-<uuid>: the UUID is the LAST 36 chars.
        session_id = stem[-36:]
        if not sessions.UUID4.fullmatch(session_id):
            continue
        lock = store.lifecycle_lock(session_id)
        lock.acquire(blocking=True)
        try:
            if entry.exists() and not store.exists(session_id):
                state.remove_private(entry)
                removed.append(f"lead prompt for forgotten session {session_id}")
        finally:
            lock.release()
    # Pre-2.2 lead prompts were named lead-prompt-<digest>.md with no session
    # suffix; the launcher has not written that form since, so every such
    # file is an orphan by construction.
    for entry in sorted(store.root.glob("lead-prompt-*.md")):
        stem = entry.name.removeprefix("lead-prompt-").removesuffix(".md")
        if "-" in stem:
            continue  # session-suffixed form, handled above
        if entry.exists():
            state.remove_private(entry)
            removed.append(f"orphaned pre-2.2 lead prompt {entry.name}")
    removed.extend(_prune_039_extras(runtime))
    # Lifecycle lock files are permanent synchronization identities. Unlinking
    # one while another process holds its inode would create two independent
    # locks for the same session.
    if not removed:
        output_stream.write(
            "Prune: nothing stale; every scope has a living record.\n"
        )
        return 0
    output_stream.write("Pruned:\n")
    for line in removed:
        output_stream.write(f"  - {line}\n")
    return 0


def _prune_039_extras(runtime: runtime_mod.Runtime) -> list[str]:
    """The prune rules; the removed items, one line each.

    Orphan ``notice/<rid>.seen`` markers (no record's runtime id or alias),
    superseded ``lead-prompt-<digest>-<mid>.md`` files (each record keeps its
    newest, the one its last launch wrote), lineup logs of forgotten records
    and the rotation of ``hook-errors.log``. Never touches ``*.v3.json``,
    ``*.v3`` pointer backups, ``quarantine-3x/``, ``restored-2x/`` or
    ``migration.lock``. Nothing is pruned by the id-based rules while any
    record is unreadable (an unreadable record may still own the file).
    """

    store = runtime.session_store
    root = store.root
    removed: list[str] = []
    records, record_problems, record_ids = session_facts._session_record_scan(runtime)
    if record_problems:
        return [
            "nothing pruned by session id: "
            f"{len(record_problems)} unreadable record(s) may still own notice markers, "
            "lead prompts or lineup logs"
        ]
    runtime_ids: set[str] = set()
    for record in records:
        runtime_ids.update(sessions.record_session_ids(record))
    notice_dir = root / hooks.NOTICE_DIR
    if notice_dir.is_dir() and not notice_dir.is_symlink():
        for entry in sorted(notice_dir.iterdir()):
            name = entry.name
            if not name.endswith(hooks.SEEN_SUFFIX):
                continue
            rid = name[: -len(hooks.SEEN_SUFFIX)]
            if not sessions.UUID4.fullmatch(rid) or rid in runtime_ids:
                continue
            try:
                state.remove_private(entry)
            except (OSError, state.StateError):
                continue
            removed.append(f"notice marker for unknown runtime session {rid}")
    prompts: dict[str, list[Path]] = {}
    for entry in root.glob("lead-prompt-*-*.md"):
        session_id = entry.name.removeprefix("lead-prompt-").removesuffix(".md")[-36:]
        if sessions.UUID4.fullmatch(session_id) and session_id in record_ids:
            prompts.setdefault(session_id, []).append(entry)
    daemon = runtime.background_liveness() if prompts else None
    process_scan = sessions.proc_session_scan(runtime.proc_root) if prompts else None
    for session_id, entries in sorted(prompts.items()):
        if len(entries) < 2:
            continue
        if daemon is None or process_scan is None or not daemon.known or not process_scan.known:
            continue
        index = store.runtime_index_lock()
        index.acquire(blocking=True)
        lock = store.lifecycle_lock(session_id)
        try:
            lock.acquire(blocking=True)
            try:
                # A hook can change the record after the preview. Re-read
                # under runtime-index -> lifecycle; ended alone is insufficient.
                record = store.load(session_id)
                if record.get("last_event_source") != "end" or session_facts._record_view_is_live(
                        record, prefixes=daemon.prefixes, proc_ids=process_scan.ids):
                    continue
                from claude_multi import continuity
                if session_id in continuity.scan_records(root).fence_unreadable:
                    removed.append(f"nothing pruned for {session_id[:8]}: unreadable scope fence")
                    continue
                present = [entry for entry in entries if entry.exists()]
                present.sort(key=lambda entry: (entry.stat().st_mtime, entry.name))
                for entry in present[:-1]:
                    state.remove_private(entry)
                    removed.append(f"superseded lead prompt {entry.name}")
            finally:
                lock.release()
        finally:
            index.release()
    for session_id in lineup_log_ids(root):
        if session_id in record_ids:
            continue
        lock = store.lifecycle_lock(session_id)
        lock.acquire(blocking=True)
        try:
            if not store.exists(session_id) and lineup_log_remove(root, session_id):
                removed.append(f"lineup log of forgotten session {session_id}")
        finally:
            lock.release()
    log = root / hooks.HOOK_ERRORS_LOG
    try:
        info = log.lstat()
    except OSError:
        info = None
    if info is not None and stat.S_ISREG(info.st_mode) and info.st_size > 0:
        os.replace(log, root / hooks.HOOK_ERRORS_ROTATED)
        removed.append(f"rotated {hooks.HOOK_ERRORS_LOG} (read; kept as {hooks.HOOK_ERRORS_ROTATED})")
    return removed


def lineup_log_ids(root: Path) -> list[str]:
    from claude_multi import lineup_log

    try:
        return lineup_log.log_ids(root)
    except (OSError, state.StateError):
        return []


def lineup_log_remove(root: Path, session_id: str) -> bool:
    from claude_multi import lineup_log

    return lineup_log.remove(root, session_id)


def _doctor_repair(runtime: runtime_mod.Runtime, uuid: str, output_stream: TextIO) -> int:
    """Converge one session's scope to its record (regardless of liveness)."""

    if not sessions.UUID4.fullmatch(uuid):
        raise cli_errors.CLIError(f"{uuid!r} is not a UUIDv4")
    record = runtime.session_store.resolve(uuid)
    stable_id = sessions.managed_id(record)
    transition = runtime_mod._transition_module()
    parts = runtime.converge_parts()
    try:
        # One served-change phase (migration guard ->
        # barrier); the fork cleanup and converge consume the held token.
        with sessions.served_change_phase(runtime.session_store.root, runtime.home) as barrier:
            fork_converged = runtime.session_store.converge_pending_forks(stable_id)
            report = transition.converge(
                runtime.session_store.root,
                runtime.session_store,
                stable_id,
                runtime_parts=parts,
                barrier=barrier,
            )
    except sessions.MigrationBusyError:
        output_stream.write(f"skipped {stable_id[:8]}: migration or restore in progress\n")
        return 1
    except state.BarrierBusyError as exc:
        output_stream.write(f"skipped {stable_id[:8]}: {exc.strerror}\n")
        return 1
    except transition.TransitionError as exc:
        raise cli_errors.CLIError(str(exc)) from exc
    if fork_converged:
        output_stream.write(
            "cleared a fork marker the live runtime already resolved\n"
        )
    for line in report:
        output_stream.write(f"{termtext.visible_text(line)}\n")
    return 0


def _doctor_repair_all(
    runtime: runtime_mod.Runtime, output_stream: TextIO, *, include_live: bool = False
) -> int:
    """Bulk converge; live/unknown sessions only with ``--include-live``.

    v1-3 and gen-0 records get their report line (never rewritten); a record
    whose last event is not ``end`` (or that is ● live) is skipped unless
    ``include_live``. Unreadable records and per-session failures never stop
    the pass; each is reported. Returns 1 when anything failed.
    """

    records, record_problems, _ids = session_facts._session_record_scan(runtime)
    transition = runtime_mod._transition_module()
    failures: list[str] = list(record_problems)
    skipped: list[str] = []
    repaired = 0
    # Unobservable daemon liveness is unknown, never "not live": every
    # record then counts as live or unknown (skipped without --include-live).
    daemon = runtime.background_liveness()
    live = daemon.prefixes
    parts = runtime.converge_parts()
    try:
        # One served-change phase for the whole pass.
        phase = sessions.served_change_phase(runtime.session_store.root, runtime.home)
        barrier = phase.__enter__()
    except sessions.MigrationBusyError:
        output_stream.write("Repair-all: skipped — migration or restore in progress.\n")
        return 1
    except state.BarrierBusyError as exc:
        output_stream.write(f"Repair-all: skipped — {exc.strerror}.\n")
        return 1
    try:
        repaired = _repair_all_records(
            runtime, records, transition, parts, daemon, live, barrier, include_live,
            output_stream, failures, skipped,
        )
    finally:
        phase.__exit__(None, None, None)
    output_stream.write(
        f"Repair-all: {repaired} session(s) converged to record authority.\n"
    )
    for line in skipped:
        output_stream.write(f"  - skipped {termtext.visible_text(line)}\n")
    for line in failures:
        output_stream.write(f"  - FAILED {termtext.visible_text(line)}\n")
    if failures:
        return 1
    output_stream.write(
        "Re-run `claude-multi doctor` to confirm; `doctor --prune` collects "
        "retained .prev generations afterwards.\n"
    )
    return 0


def _repair_all_records(
    runtime: runtime_mod.Runtime, records: list, transition: ModuleType, parts: transition_mod.RuntimeParts, daemon: sessions.BackgroundLiveness,
    live: frozenset, barrier: state.BarrierToken, include_live: bool, output_stream: TextIO,
    failures: list[str], skipped: list[str],
) -> int:
    """The per-record body of ``doctor --repair-all`` inside its one phase."""

    repaired = 0
    for record in records:
        stable_id = sessions.managed_id(record)
        m8 = stable_id[:8]
        if record.get("version") != sessions.RECORD_VERSION:
            # A lifecycle writer: the resolved-fork cleanup saves the
            # record in its own version, so doctor's promise holds for legacy records.
            try:
                if runtime.session_store.converge_pending_forks(stable_id):
                    output_stream.write(
                        f"{m8}… cleared a fork marker the live runtime already resolved\n"
                    )
            except sessions.MigrationBusyError:
                skipped.append(f"{m8}: migration or restore in progress")
                continue
            except (sessions.SessionError, state.StateError) as exc:
                failures.append(f"{stable_id}: {exc}")
                continue
            skipped.append(
                f"session {m8} is a legacy record; run claude-multi migrate (or resume it) first"
            )
            continue
        if record["lineup_generation"] == 0:
            skipped.append(f"session {m8} runs a legacy scope; it converges at its next launch with this launcher")
            continue
        source = record.get("last_event_source")
        is_live = not daemon.known
        try:
            is_live = is_live or session_facts._record_is_live(record, live)
        except (KeyError, sessions.SessionError):
            pass
        if (source != "end" or is_live) and not include_live:
            skipped.append(
                f"{m8}: last event {source or 'none'} (live or unknown) — rerun with "
                "--include-live"
            )
            continue
        try:
            fork_converged = runtime.session_store.converge_pending_forks(stable_id)
            report = transition.converge(
                runtime.session_store.root,
                runtime.session_store,
                stable_id,
                runtime_parts=parts,
                barrier=barrier,
            )
        except sessions.MigrationBusyError:
            skipped.append(f"{m8}: migration or restore in progress")
            continue
        except (
            transition.TransitionError,
            sessions.SessionError,
            profile_mod.ProfileError,
            compiler.CompilerError,
            scope_mod.ScopeError,
            state.StateError,
        ) as exc:
            failures.append(f"{stable_id}: {exc}")
            continue
        repaired += 1
        if fork_converged:
            output_stream.write(
                f"{m8}… cleared a fork marker the live runtime already resolved\n"
            )
        for line in report:
            output_stream.write(f"{m8}… {termtext.visible_text(line)}\n")
    return repaired


def _doctor_prune_preview(runtime: runtime_mod.Runtime, output_stream: TextIO) -> int:
    """Non-authoritative inventory: no locks, chmod, directory creation or I/O
    outside generated state. Existing records are protected, not garbage.
    """
    root = runtime.session_store.root
    output_stream.write(
        "Generated-state cleanup preview — no files removed.\n"
        "Only recognized claude-multi-generated files are candidates.\n"
        "Unadmitted declarations, session records, credentials and rollback backups are not garbage.\n"
    )
    try:
        sessions.check_state_marker(root)
        # Resolve no symlinked directory: even a listing must not wander into a
        # transcript-bearing or backup root supplied as a generated directory.
        for directory in (root, root / "sessions", root / "scopes", root / "notice", root / "lineup-log"):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise state.StateError("unsafe generated-state directory")
        records, problems, ids = session_facts._session_record_scan(runtime)
        if problems:
            output_stream.write("Protected: unreadable records; ownership is unknown.\n")
            return 2
        runtime_ids = {rid for record in records for rid in sessions.record_session_ids(record)}
        candidates: list[tuple[Path, str]] = []
        scopes = root / "scopes"
        if scopes.exists():
            for entry in sorted(scopes.iterdir()):
                name = entry.name
                mid = name
                for suffix in (".new", ".prev"):
                    if name.startswith(".") and name.endswith(suffix):
                        mid = name[1:-len(suffix)]
                if sessions.UUID4.fullmatch(mid) and mid not in ids and not entry.is_symlink():
                    candidates.append((entry, "scope/staging for forgotten record; recheck at commit"))
        for entry in sorted(root.glob("lead-prompt-*.md")):
            stem = entry.name.removeprefix("lead-prompt-").removesuffix(".md")
            mid = stem[-36:]
            digest = stem[:-37] if sessions.UUID4.fullmatch(mid) else stem
            if len(digest) == 16 and all(c in "0123456789abcdef" for c in digest):
                if "-" not in stem or mid not in ids:
                    candidates.append((entry, "obsolete generated lead prompt"))
        # Superseded prompts are candidates only for ended, process-absent
        # records with a readable fence. This is a lock-free preview; the
        # action revalidates under runtime-index -> lifecycle before unlink.
        if records:
            from claude_multi import continuity
            daemon = runtime.background_liveness()
            scan = sessions.proc_session_scan(runtime.proc_root)
            unreadable = set(continuity.scan_records(root).fence_unreadable)
            if daemon.known and scan.known:
                for record in records:
                    mid = sessions.managed_id(record)
                    if (record.get("last_event_source") != "end" or mid in unreadable or
                            session_facts._record_view_is_live(record, prefixes=daemon.prefixes, proc_ids=scan.ids)):
                        continue
                    prompts = sorted((p for p in root.glob(f"lead-prompt-*-{mid}.md") if not p.is_symlink()),
                                     key=lambda p: (p.stat().st_mtime, p.name))
                    candidates.extend((p, "superseded generated lead prompt; recheck at commit") for p in prompts[:-1])
        notice = root / "notice"
        if notice.exists():
            for entry in sorted(notice.iterdir()):
                rid = entry.name.removesuffix(".seen")
                if entry.name.endswith(".seen") and sessions.UUID4.fullmatch(rid) and rid not in runtime_ids:
                    candidates.append((entry, "unknown runtime notice; recheck at commit"))
        logs = root / "lineup-log"
        if logs.exists():
            for entry in sorted(logs.iterdir()):
                mid = entry.name.split(".log", 1)[0]
                if entry.name in {mid + ".log", mid + ".log.1"} and sessions.UUID4.fullmatch(mid) and mid not in ids:
                    candidates.append((entry, "forgotten session lineup log"))
        for path, reason in candidates:
            if not path.is_symlink():
                output_stream.write(f"Candidate: {termtext.visible_text(str(path))} — {reason}\n")
        output_stream.write("Protected: existing session scopes/prompts; .v3 backups; quarantine-3x; restored-2x; auth.pre-*; native Claude roots.\n")
        output_stream.write("Advisory snapshot only; a preview is not deletion authorization.\n")
        return 0
    except (OSError, state.StateError, sessions.SessionError):
        output_stream.write("Protected: metadata unavailable or unsafe; no cleanup inferred.\n")
        return 2
