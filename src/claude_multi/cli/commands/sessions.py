"""The ``sessions`` command."""

from __future__ import annotations

from claude_multi import errors as cli_errors
from claude_multi import lineup as lineup_mod
from claude_multi import sessions
from claude_multi import settings as settings_mod
from claude_multi import strict_json
from claude_multi import termtext
from pathlib import Path
from typing import Any
from typing import TextIO
import argparse
import io
import re
import sys
import claude_multi.cli.doctor_actions as doctor_actions
import claude_multi.cli.launch_flow as launch_flow
import claude_multi.cli.runtime as runtime_mod
import claude_multi.cli.session_actions as session_actions
import claude_multi.cli.session_facts as session_facts
import claude_multi.cli.text as cli_text
import claude_multi.cli.types as cli_types


# The JSON sessions list: one document, its fields stable across releases.
SESSIONS_LIST_SCHEMA = 1


def sessions_list_document(runtime: runtime_mod.Runtime) -> dict[str, Any]:
    """``sessions list --json``: every managed session, most recently used
    first. ``live`` is true, false, or null when the background liveness
    cannot be read; ``follow`` is null for a legacy record; no transcript
    content and no native-session metadata."""

    records = sorted(
        session_facts._session_records(runtime),
        key=session_facts._record_sort_key_last_used,
        reverse=True,
    )
    # Unknown daemon liveness (unreadable metadata, a network filesystem) is
    # null, never false.
    try:
        verdict = runtime.background_liveness()
        live, live_known = frozenset(verdict.prefixes), bool(verdict.known)
    except OSError:
        live, live_known = frozenset(), False
    lcat = runtime.lineup_catalog()
    rows = []
    for record in records:
        summary = sessions.record_summary(
            record, lcat if record.get("version") == sessions.RECORD_VERSION else None
        )
        marker = launch_flow._session_row_marker(record, summary, live).strip()
        rows.append({
            "managed_id": summary.managed_id,
            "runtime_session_id": sessions.runtime_session_id(record),
            "format": "current" if record.get("version") == sessions.RECORD_VERSION else "legacy",
            "profile": record.get("profile") if record.get("version") == sessions.RECORD_VERSION else None,
            "lead": summary.lead_key,
            "agents": summary.agent_count,
            "follow": summary.follow,
            "live": (marker == "●") if live_known else None,
            "pending_fork": bool(record.get("pending_forks")),
            "needs_attention": marker == "!",
            "pending_change": bool(summary.pending),
            "last_used": session_facts._record_last_seen(record),
            "cwd": summary.cwd,
        })
    return {"schema_version": SESSIONS_LIST_SCHEMA, "list": "sessions", "sessions": rows}


def _print_sessions_listing(runtime: runtime_mod.Runtime, output_stream: TextIO) -> None:
    """The text sessions listing (``sessions list``, bare ``-r`` without a terminal)."""

    records = sorted(
        session_facts._session_records(runtime),
        key=session_facts._record_sort_key_last_used,
        reverse=True,
    )
    output_stream.write("sessions\n")
    output_stream.write("----------------------------------------\n")
    live = session_facts._live_background_prefixes()
    lcat = runtime.lineup_catalog()
    for record in records:
        summary = sessions.record_summary(
            record, lcat if record.get("version") == sessions.RECORD_VERSION else None
        )
        follow = "-" if summary.follow is None else ("follow" if summary.follow else "pinned")
        # Record fields (cwd, profile) are external text; the whole row is
        # sanitized single-line output.
        output_stream.write(
            termtext.visible_text(
                f"{launch_flow._session_row_marker(record, summary, live)} {summary.managed_id[:8]}  "
                f"{summary.profile_label:<16} {summary.lead_key:<18} agents "
                f"{summary.agent_count:<2} {follow:<7} {session_facts._record_last_used_age(record)}  "
                f"{summary.cwd}"
            )
            + "\n"
        )
    if not records:
        output_stream.write("(no recorded sessions)\n")
    output_stream.write(
        "[r] resume: claude-multi -r <id> · "
        "[t] lineup: claude-multi lineup --session <runtime-id> "
        "show|set|unset|profile|pin|follow|direct · "
        "[e] end a live session: claude-multi sessions stop <id> · "
        "[f] forget: claude-multi sessions forget <id> "
        "(deletes the record + generated scope; transcripts are never touched)\n"
    )
    output_stream.write(
        "markers: ● live · ⚠ pending fork · ! repair or lead choice needed · "
        "↻ pending relaunch change\n"
    )
    output_stream.write(
        "not seeing a session? only managed (launcher-started or adopted) sessions "
        "are listed — adopt native ones with `claude-multi sessions link <fork> "
        "--profile NAME | --direct MODEL` (run it bare for the discovery guide)\n"
    )
    native = session_facts._discover_native_sessions(runtime)
    if native:
        live = session_facts._live_background_prefixes()
        output_stream.write("native (unmanaged, discovered names+times only)\n")
        output_stream.write("----------------------------------------\n")
        for item in native:
            marker = "● " if session_facts._native_is_live(item, live) else ""
            kind = (
                f"(fork of {item['fork_of'][:8]}…)"
                if item.get("fork_of")
                else "(native)"
            )
            output_stream.write(
                termtext.visible_text(
                    f"{marker}{item['session_id']}  {kind}  {item['slug']}  "
                    f"{session_facts._mtime_age(item['mtime'])}"
                )
                + "\n"
            )


def _sessions_transition(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
    no_color: bool = False,
) -> int:
    """The legacy alias ``sessions transition UUID --composition N [--its-exited]``.

    = ``lineup --relaunch profile N`` on ``store.resolve(UUID)`` (managed ids
    accepted): phase A evaluates the target under the locks and
    either samples the record for :func:`_relaunch_exec` (outside the session,
    confirmed or ``--its-exited``) or, from inside the session or
    non-interactively without ``--its-exited``, records a ``pending`` change
    (v4) / refuses (v1-3).
    """

    if not sessions.UUID4.fullmatch(args.uuid):
        raise cli_errors.CLIError(f"{args.uuid!r} is not a UUIDv4")
    name = args.transition_composition
    sys.stderr.write(
        cli_text.ALIAS_NOTE.format(
            old="sessions transition UUID --composition N",
            new="lineup --session <runtime-id> --relaunch profile N",
        )
        + "\n"
    )
    if not runtime.profiles.contains(name):
        raise cli_errors.CLIError(cli_text.PROFILE_IS_COMPOSITION.format(name=name))
    record = runtime.session_store.resolve(args.uuid)
    mid = sessions.managed_id(record)
    m8 = mid[:8]
    runtime_mod._transition_module()  # fails closed when the converge engine is absent
    inside = lineup_mod.inside_session(runtime.environ, mid)
    exec_ok = not inside and (bool(args.its_exited) or interactive)
    request = lineup_mod.Request("profile", name=name, text=f"profile {name}")
    decision = lineup_mod.locked_decision(
        runtime,
        mid,
        lambda current: lineup_mod.decide_locked(
            runtime, current, request, forced=True, exec_ok=exec_ok
        ),
    )
    if decision.kind != "exec":
        output_stream.write(decision.text)
        output_stream.flush()
        return 0
    assert decision.sample is not None
    target = cli_types.LaunchTarget("relaunch", None, name, True, f"Relaunch {m8}")
    return launch_flow._relaunch_exec(
        runtime,
        mid,
        target,
        its_exited=bool(args.its_exited),
        interactive=interactive,
        sample=decision.sample,
        input_stream=input_stream,
        output_stream=output_stream,
        no_color=no_color,
    )


def _session_show_lines(runtime: runtime_mod.Runtime, record: dict[str, Any]) -> list[str]:
    """``sessions show``: needs-a-choice, pending and drift lines (current records only)."""

    if record.get("version") != sessions.RECORD_VERSION:
        return []
    mid = sessions.managed_id(record)
    rid = sessions.runtime_session_id(record)
    lines: list[str] = []
    lcat = runtime.lineup_catalog()
    if sessions.lead_needs_choice(record, lcat):
        lines.append(
            f"needs a lead choice: {sessions.lead_choice_notice(record, lcat)} — "
            f"claude-multi -r {mid} (interactive) or claude-multi lineup --session {rid} "
            "--relaunch --its-exited profile <name>"
        )
    pending = record.get("pending")
    if pending:
        lines.append(
            f"pending relaunch change: {'; '.join(pending.get('reasons') or ())} — applies "
            f"at the next resume (claude-multi -r {mid})"
        )
    target = record.get("lead_target")
    if target:
        lines.append(
            f"requested lead: {lineup_mod.binding_label(lcat.lines.get(target['key']), target['effort'], key=target['key'])} "
            f"({target['selector']}) — switch with /model, or it applies at the next resume"
        )
    try:
        drift = settings_mod.drift(record["applied"]["settings"], runtime.current_effective())
    except (cli_errors.CLIError, settings_mod.SettingsError):
        drift = []
    lines.extend(f"settings drift: {line}" for line in drift)
    return lines


def _refused(message: str) -> int:
    """A refusal of an ordinary sessions command: stderr, exit 1."""

    sys.stderr.write(f"claude-multi: {termtext.visible_text(message)}\n")
    sys.stderr.flush()
    return 1


def _sessions_mark_ended(
    runtime: runtime_mod.Runtime, args: argparse.Namespace, output_stream: TextIO
) -> int:
    """``sessions mark-ended SESSION | --all-dead``: exit 0 when every named
    session was marked, 1 when one was refused or liveness is unknown."""

    store = runtime.session_store
    if args.mark_all_dead:
        daemon = runtime.background_liveness()
        if not daemon.known:
            return _refused("cannot tell which sessions are live in the background "
                            f"({daemon.reason}); nothing marked")
        prefixes = daemon.prefixes
        scan = sessions.proc_session_scan()
        if not scan.known:
            return _refused("cannot tell which sessions are running (the process table is unreadable: "
                            f"{scan.reason}); nothing marked")
        proc_ids = scan.ids
        ids = []
        for path in store.scan_uuid_records():
            try:
                record = store.load(path.stem)
            except sessions.SessionError:
                continue
            if record.get("last_event_source") == "end":
                continue
            if session_facts._record_view_is_live(record, prefixes=prefixes, proc_ids=proc_ids):
                continue
            ids.append(path.stem)
    else:
        ids = [sessions.managed_id(session_facts.resolve_session(runtime, args.uuid))]
    results = session_actions._mark_ended_ids(runtime, ids)
    if not results:
        output_stream.write("no session without a recorded end is dead by every check\n")
    for managed, status in results:
        output_stream.write(f"{managed}  {status}\n")
    output_stream.flush()
    return 1 if any(status.startswith("refused") for _id, status in results) else 0


def _forget_confirmed(
    record: dict[str, Any] | None, stable_id: str, *, yes: bool, interactive: bool,
    input_stream: TextIO,
) -> bool:
    """The forget confirmation: the target and its consequences, then y/N
    (default No) on a terminal; ``--yes`` without one. ``--force`` is a
    separate decision (liveness) and never answers this question."""

    if yes:
        return True
    if not interactive:
        raise cli_errors.CLIError("sessions forget needs a confirmation: there is no terminal to ask in",
                                  remedy=f"claude-multi sessions forget {stable_id} --yes")
    runtime_id = sessions.runtime_session_id(record) if record is not None else None
    prompt = sys.stderr
    prompt.write(
        f"Forget session {stable_id}"
        + (f" (runtime {runtime_id})" if runtime_id and runtime_id != stable_id else "")
        + "? Its record and generated scope are deleted; the conversation transcript is kept, and "
        "Sessions → L (or claude-multi sessions link RUNTIME-ID) can manage it again. [y/N] "
    )
    prompt.flush()
    answer = (input_stream.readline() or "").strip().lower()
    return answer in ("y", "yes")


def _resolve_fork_confirmed(
    record: dict[str, Any], stable_id: str, fork_id: str, *, yes: bool, interactive: bool,
    input_stream: TextIO,
) -> bool:
    """The resolve-fork confirmation: what is discarded and what is kept,
    then y/N (default No) on a terminal; ``--yes`` without one."""

    if yes:
        return True
    if not interactive:
        raise cli_errors.CLIError("sessions resolve-fork needs a confirmation: there is no terminal to ask in",
                                  remedy=f"claude-multi sessions resolve-fork {stable_id} {fork_id} --yes")
    prompt = sys.stderr
    prompt.write(
        f"Discard the pending fork {fork_id} of session {stable_id}? Its marker is removed and the fork's "
        "later hooks can no longer claim this session; its conversation stays a native session "
        f"(adopt it later: {sessions.fork_adopt_command(record, fork_id)}). [y/N] "
    )
    prompt.flush()
    answer = (input_stream.readline() or "").strip().lower()
    return answer in ("y", "yes")


def _sessions_command(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
    no_color: bool,
) -> int | None:
    """The ``sessions`` command: ``list`` is a report (the interactive
    picker is bare ``claude-multi -r`` and the card's S); the other verbs act
    on the session a person names (``session_facts.resolve_session``)."""

    command = args.sessions_command
    if command == "list":
        if getattr(args, "json_output", False):
            output_stream.write(strict_json.pretty_file_bytes(sessions_list_document(runtime)).decode("utf-8"))
            return 0
        _print_sessions_listing(runtime, output_stream)
        return 0
    if command == "mark-ended":
        return _sessions_mark_ended(runtime, args, output_stream)
    if command == "show":
        record = session_facts.resolve_session(runtime, args.uuid)
        if record.get("pending_forks"):
            sys.stderr.write(termtext.visible_text(sessions.pending_fork_message(record)) + "\n")
        elif record.get("identity_state") == sessions.IDENTITY_REPAIR_NEEDED:
            sys.stderr.write(termtext.visible_text(sessions.relink_message(record)) + "\n")
        for line in _session_show_lines(runtime, record):
            sys.stderr.write(termtext.visible_text(line) + "\n")
        output_stream.write(strict_json.canonical_file_bytes(record).decode("utf-8"))
        return 0
    if command == "forget":
        record: dict[str, Any] | None = None
        try:
            if sessions.UUID4.fullmatch(args.uuid):
                record = runtime.session_store.resolve(args.uuid)
            else:
                record = runtime.session_store.resolve_human(
                    args.uuid, names=session_facts._record_resume_names)
            stable_id = sessions.managed_id(record)
        except sessions.AmbiguousSessionError:
            session_facts.resolve_session(runtime, args.uuid)  # raises the listing
            raise
        except sessions.SessionError as exc:
            if "no managed session matches" in str(exc):
                return _refused(f"no managed session matches {args.uuid!r}; nothing was forgotten")
            # A corrupt record is exactly what forget exists for: an exact
            # UUID with a present record file forgets load-free (scope
            # removal and pointer sweep by id).
            if not (
                sessions.UUID4.fullmatch(args.uuid)
                and runtime.session_store.exists(args.uuid)
            ):
                # Runtime lookup fails closed on a damaged record. Name
                # the stable-id retry; never guess its runtime ownership.
                damaged = re.match(
                    r"(?:unreadable session record |cannot determine runtime ownership while session record )"
                    r"([0-9a-f-]{36})\b", str(exc)
                )
                if damaged and sessions.UUID4.fullmatch(damaged[1]):
                    raise cli_errors.CLIError(
                        f"{exc}; to remove that record, retry with its managed ID: "
                        f"claude-multi sessions forget {damaged[1]}"
                    ) from exc
                raise
            stable_id = args.uuid
            sys.stderr.write(
                f"note: record {stable_id} is unreadable ({termtext.visible_message(exc)}); "
                "forgetting it load-free.\n"
            )
        if not _forget_confirmed(record, stable_id, yes=bool(getattr(args, "yes", False)),
                                 interactive=interactive, input_stream=input_stream):
            output_stream.write("Nothing was forgotten.\n")
            return 3
        # The live guard runs UNDER the lifecycle lock with a fresh prefix
        # scan: a liveness verdict taken before blocking on the lock would be
        # stale by the time deletion happens.
        removed, scope_removed = runtime.session_store.forget_session(
            stable_id,
            pre_delete_check=session_actions._forget_liveness_guard(runtime, stable_id, force=args.force),
        )
        if not removed:
            return _refused(f"no managed session matches {args.uuid!r}; nothing was forgotten")
        runtime_id = sessions.runtime_session_id(record) if record is not None else None
        output_stream.write(f"Forgot: {stable_id}\n")
        output_stream.write(
            "Deleted: session record + generated scope"
            + ("" if scope_removed else " (no generated scope existed)")
            + ". Transcripts are never touched.\n"
        )
        if runtime_id is not None:
            output_stream.write(
                f"Its conversation stays a native session (runtime {runtime_id}); manage it again with "
                f"Sessions → L, or: claude-multi sessions link {runtime_id} --profile NAME\n"
            )
        return 0
    if command == "relink-runtime":
        if not sessions.UUID4.fullmatch(args.runtime_uuid):
            raise cli_errors.CLIError(f"{args.runtime_uuid!r} is not a UUIDv4")
        record = session_facts.resolve_session(runtime, args.uuid)
        stable_id = sessions.managed_id(record)
        repaired_cwd: str | None = None
        if args.repair_cwd is not None:
            repaired_cwd = str(Path(args.repair_cwd).resolve())
            if not Path(repaired_cwd).is_dir():
                raise cli_errors.CLIError(
                    f"repair CWD {repaired_cwd!r} is not an accessible directory"
                )
        updated = runtime.session_store.relink_runtime(
            stable_id,
            observed_runtime_id=args.runtime_uuid,
            cwd=repaired_cwd,
        )
        if (
            updated.get("version") == sessions.RECORD_VERSION
            and updated.get("lineup_generation", 0) >= 1
        ):
            try:
                doctor_actions._doctor_repair(runtime, stable_id, io.StringIO())
            except cli_errors.CLIError as exc:
                output_stream.write(
                    f"Reconciled managed {stable_id} to runtime "
                    f"{updated['runtime_session_id']}, but scope reconverge "
                    f"failed: {termtext.visible_message(exc)}; run "
                    f"`claude-multi doctor --repair {stable_id}`\n"
                )
                return 1
        output_stream.write(
            f"Reconciled managed {stable_id} to runtime "
            f"{updated['runtime_session_id']} at CWD {updated['cwd']}.\n"
        )
        return 0
    if command == "transition":
        return _sessions_transition(
            runtime,
            args,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
            no_color=no_color,
        )
    if command == "resolve-fork":
        record = session_facts.resolve_session(runtime, args.uuid)
        stable_id = sessions.managed_id(record)
        # A marker this cannot discard is refused before anything is asked;
        # the store checks it again under the lifecycle lock.
        problem = sessions.fork_resolution_problem(record, stable_id, args.fork_uuid)
        if problem is not None:
            raise cli_errors.CLIError(problem)
        if not _resolve_fork_confirmed(record, stable_id, args.fork_uuid, yes=bool(getattr(args, "yes", False)),
                                       interactive=interactive, input_stream=input_stream):
            output_stream.write(f"Nothing was changed: fork {args.fork_uuid} is still pending.\n")
            return 3
        try:
            updated = runtime.session_store.resolve_fork(stable_id, args.fork_uuid)
        except sessions.SessionError as exc:
            raise cli_errors.CLIError(str(exc)) from exc
        output_stream.write(
            f"Resolved fork {args.fork_uuid} on session {stable_id}: the "
            "marker is discarded, the fork transcript stays on disk as a "
            "native session (adopt it later with "
            f"`{sessions.fork_adopt_command(record, args.fork_uuid)}` if you "
            "ever need it). Identity is now "
            f"{updated.get('identity_state', sessions.IDENTITY_UNVERIFIED)}.\n"
        )
        return 0
    if command == "stop":
        record = session_facts.resolve_session(runtime, args.uuid)
        stable_id = sessions.managed_id(record)
        runtime_id = sessions.runtime_session_id(record)
        refusal = session_actions._stop_precheck(runtime, record, force=args.force)
        if refusal is not None:
            return _refused(f"session {stable_id}: {refusal}; nothing was stopped")
        if not args.yes:
            if not interactive:
                raise cli_errors.CLIError(
                    "sessions stop requires --yes when non-interactive"
                )
            sys.stderr.write(
                f"Stop live background session {stable_id} (runtime "
                f"{runtime_id}) with upstream `claude stop`? The "
                "conversation is always kept. [y/N] "
            )
            sys.stderr.flush()
            answer = (input_stream.readline() or "").strip().lower()
            if answer not in ("y", "yes"):
                sys.stderr.write("Stop cancelled.\n")
                return 3
        problem = session_actions._stop_runtime(runtime, runtime_id, record=record)
        if problem is not None:
            raise cli_errors.CLIError(
                f"upstream stop failed for {runtime_id}: {problem}"
            )
        output_stream.write(
            f"Stopped session {stable_id} (runtime {runtime_id}). The "
            "conversation is kept — resume it with `claude-multi -r "
            f"{stable_id}` when ready.\n"
        )
        return 0
    if command == "link":
        return session_actions._sessions_link(
            runtime, args, output_stream=output_stream, interactive=interactive
        )
