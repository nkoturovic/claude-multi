"""Launch flows: the line confirm, resume and direct flows, relaunch."""

from __future__ import annotations

from claude_multi import catalog
from claude_multi import compiler
from claude_multi import errors as cli_errors
from claude_multi import launch
from claude_multi import lineup as lineup_mod
from claude_multi import profile as profile_mod
from claude_multi import sessions
from claude_multi import termtext
from claude_multi import tui
from claude_multi import views
from typing import Any
from typing import TextIO
import argparse
import dataclasses
import sys
import claude_multi.cli.gateway_facts as gateway_facts
import claude_multi.cli.screens.common as screens_common
import claude_multi.cli.screens.launch_sessions as launch_sessions
import claude_multi.cli.screens.transition as screens_transition
import claude_multi.cli.selection as selection
import claude_multi.cli.session_facts as session_facts
import claude_multi.cli.streams as streams
import claude_multi.cli.text as cli_text
import claude_multi.cli.types as cli_types
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def _skill_policy_notices(prepared: cli_types.PreparedLaunch, stream: TextIO | None = None) -> None:
    """One stderr line per passthrough flag the skill-deny probe proved to
    defeat the managed skill deny (``compiler.SKILL_POLICY_BYPASS_FLAGS``;
    none at the pinned client). The flag is never blocked."""

    out = stream or sys.stderr
    flags = compiler.skill_policy_bypass_flags(prepared.result.argv)
    for flag in flags:
        out.write(cli_text.SKILL_POLICY_BYPASS_NOTICE.format(flag=flag) + "\n")
    if flags:
        out.flush()


def _perform_card_result(runtime: runtime_mod.Runtime, result: tuple, *, force: bool) -> Any:
    """Perform a card/screen ``("perform", prepared[, decision])`` after teardown (§4.13)."""

    decision = result[2] if len(result) > 2 else None
    _skill_policy_notices(result[1])
    return runtime.perform(result[1], resume_decision=decision or ("force" if force else None))


def _transition_preflight_problems(runtime: runtime_mod.Runtime) -> list[str]:
    """Binary verification + gateway readiness, via Runtime accessors.

    The same accessors Doctor uses, so the transition can never proceed when
    Doctor would report BLOCKED: ``doctor_binary_callback`` wraps
    ``launch.resolve_claude`` (full-hash binary verification) and the
    readiness branch runs ``launch.check_readiness`` (loopback-only) unless a
    doctor override is injected.
    """

    problems, _info = runtime.doctor_binary_callback(
        runtime.catalog.docs["native-contract"]
    )
    problems = list(problems)
    if runtime.doctor_callback is not None:
        problems.extend(runtime.doctor_callback(runtime))
    else:
        try:
            runtime.ensure_gateway()  # a relaunch is a resume: start an on-demand gateway first
            runtime.check_readiness()
        except launch.LaunchError as exc:
            problems.append(f"local gateway: {launch.gateway_problem_text(exc, home=runtime.home)}")
    return problems


# The command-line fix for a missing key (never a TUI key).
PRINT_LAUNCH_KEY_FIX = ("set the provider's API key: claude-multi providers set-key PROVIDER, or sign in: "
                        "claude-multi providers sign-in anthropic|openai")


def _print_launch_verdict(prepared: cli_types.PreparedLaunch, output_stream: TextIO, *, warn_only: bool = False) -> int:
    """The last line of ``--print-launch``: ``would launch`` (exit 0) or
    ``would fail`` with the reason and its fix (exit 1). A plan that prints is
    not an admission: the launch still checks the Claude Code copy and the
    gateway when it starts. ``warn_only`` (an ad-hoc direct session) launches
    despite a missing key."""

    if prepared.secret_problems and not warn_only:
        output_stream.write(f"would fail: {termtext.visible_text(prepared.secret_problems[0])}\n"
                            f"  fix: {PRINT_LAUNCH_KEY_FIX}\n")
        return 1
    note = " (a key is missing: its requests may fail)" if prepared.secret_problems else ""
    output_stream.write(f"would launch{note}; the launch itself still checks the Claude Code copy and the "
                        "gateway\n")
    return 0


def _print_launch(
    runtime: runtime_mod.Runtime, target: cli_types.LaunchTarget, output_stream: TextIO, *,
    passthrough: list[str],
) -> int:
    """A fresh ``--print-launch``: the plan and its verdict, or ``would
    fail`` with the reason a prepare refuses (exit 1). Writes nothing."""

    try:
        prepared = runtime.prepare(target, action="fresh", passthrough=passthrough, read_only=True)
    except cli_types.NeedsChoiceError:
        raise
    except cli_errors.ClaudeMultiError as exc:
        output_stream.write(f"would fail: {termtext.visible_message(exc)}\n")
        if exc.remedy:
            output_stream.write(f"  fix: {termtext.visible_text(exc.remedy)}\n")
        output_stream.flush()
        return 1
    _print_launch_plan(prepared, output_stream)
    return _print_launch_verdict(prepared, output_stream)


def _print_launch_plan(prepared: cli_types.PreparedLaunch, output_stream: TextIO) -> None:
    """--print-launch: exact argv + env summary; the token is never shown."""

    result = prepared.result
    output_stream.write("claude argv (after the verified executable):\n")
    for token in result.argv:
        output_stream.write(f"  {termtext.visible_text(token)}\n")
    output_stream.write("environment (effective at exec):\n")
    for key in sorted(result.env_set):
        output_stream.write(f"  set {key}\n")
    for key in result.env_unset:
        output_stream.write(f"  unset {key}\n")
    output_stream.write(
        "gateway auth: scope apiKeyHelper (no credential in process env)\n"
    )
    record = prepared.record
    output_stream.write(
        f"record: profile {termtext.visible_text(record['profile'] or '(ad-hoc direct)')} · follow "
        f"{'yes' if record['follow'] else 'no'} · lineup generation "
        f"{record['lineup_generation']} · session {session_facts._record_identity_label(record)}\n"
    )
    _print_launch_windows(prepared, output_stream)


# The compaction policy a launch exports (numbers, never secrets).
_COMPACTION_ENV = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_MAX_CONTEXT_TOKENS")


def _print_launch_windows(prepared: cli_types.PreparedLaunch, output_stream: TextIO) -> None:
    """--print-launch: each role's selector, class and effective window, and
    the compaction values the process env and the scope settings carry."""

    lineup = getattr(prepared, "lineup", None)
    if lineup is None:
        return
    workflow = getattr(prepared, "workflow_window", None)
    rows = views.role_window_rows(lineup, workflow[1] if workflow is not None else None)
    ceiling = getattr(prepared, "window_ceiling", None)
    head = "context windows"
    if ceiling is not None:
        head += f" (ceiling {ceiling.value}, {ceiling.source})"
    output_stream.write(head + ":\n")
    width = max(len(row.role) for row in rows)
    for row in rows:
        output_stream.write(
            f"  {row.role:<{width}}  {termtext.visible_text(row.selector)} · {row.class_label} class · "
            f"window {row.window} · compacts at {row.trigger}\n"
        )
    env = prepared.result.env_set
    shown = [f"{key}={env[key]}" for key in _COMPACTION_ENV if key in env]
    if shown:
        output_stream.write(f"compaction env (process and scope settings): {' '.join(shown)}\n")


def _relaunch_preview(runtime: runtime_mod.Runtime, mid: str, target: cli_types.LaunchTarget) -> list[str]:
    """The confirm view of a relaunch: the §7.8 diff when it evaluates (display only)."""

    try:
        record = runtime.session_store.load(mid)
        lcat = runtime.lineup_catalog()
        document = (
            target.document
            if target.document is not None
            else runtime.profiles.load(str(target.profile))
        )
        evaluation = profile_mod.evaluate(
            document,
            lcat,
            bindings=runtime.bindings.bindings(),
            effective=runtime.current_effective(),
            ad_hoc=target.profile is None,
        )
    except (cli_errors.CLIError, sessions.SessionError, profile_mod.ProfileError, profile_mod.BindingError):
        evaluation = None
        record = None
    label = f"profile {target.profile}" if target.profile else "an ad-hoc lineup"
    lines = [f"relaunch session {mid[:8]} with {label}"]
    if (
        evaluation is not None
        and evaluation.lineup is not None
        and record is not None
        and record.get("version") == sessions.RECORD_VERSION
    ):
        rows = lineup_mod.diff_rows(
            record["applied"], evaluation.lineup, lcat, old_lead_class=record.get("lead_class")
        )
        lines += lineup_mod.render_diff(rows) or ["  (the lineup is unchanged)"]
    elif evaluation is not None and evaluation.errors:
        lines += [f"  - {error}" for error in evaluation.errors]
    return lines


def _launch_notes(prepared: cli_types.PreparedLaunch, stream: TextIO) -> None:
    """§6.3 non-interactive: the one-time diff and the notices, before exec."""

    record = prepared.record
    m8 = sessions.managed_id(record)[:8]
    for notice in prepared.notices:
        stream.write(f"claude-multi: {termtext.visible_text(notice)}\n")
    if prepared.diff:
        if prepared.target.kind == "record" and record.get("follow") and record.get("profile"):
            stream.write(
                f"session {m8} follows profile {termtext.visible_text(record['profile'])}; "
                "applying its changes at this resume:\n"
            )
        else:
            stream.write(f"session {m8}: applying a lineup change at this resume:\n")
        for line in prepared.diff:
            stream.write(termtext.visible_text(line) + "\n")
    stream.flush()


def _relaunch_exec(
    runtime: runtime_mod.Runtime,
    mid: str,
    target: cli_types.LaunchTarget,
    *,
    its_exited: bool,
    interactive: bool,
    sample: tuple[Any, Any, Any, Any],
    input_stream: TextIO,
    output_stream: TextIO,
    no_color: bool = False,
) -> int:
    """Relaunch executor: confirm, preflight, prepare, perform (no lock held).

    ``sample`` is what the caller read under the locks; ``prepare`` compares
    its own load with it (a change meanwhile refuses), and ``perform_launch``
    CASes the prepared values inside its lock. The resume gate applies; a
    relaunch never stops a session.
    """

    m8 = mid[:8]
    if not its_exited:
        if not interactive:
            raise cli_errors.CLIError(
                f"sessions transition {m8}: not interactive; rerun with --its-exited "
                "once the session has exited"
            )
        live_note: str | None = None
        try:
            record = runtime.session_store.load(mid)
            if session_facts._record_is_live(record, session_facts._live_background_prefixes()):
                live_note = cli_text.TRANSITION_LIVE_NOTE.format(mid=mid)
        except (sessions.SessionError, KeyError):
            pass
        preview = _relaunch_preview(runtime, mid, target)
        if not tui.streams_curses_capable(input_stream, output_stream):
            for line in preview:
                output_stream.write(f"{termtext.visible_text(line)}\n")
            if live_note is not None:
                output_stream.write(f"{termtext.visible_text(live_note)}\n")
        confirmed = screens_transition._transition_confirm(
            preview,
            input_stream=input_stream,
            output_stream=output_stream,
            no_color=no_color,
            live_note=live_note,
        )
        if not confirmed:
            output_stream.write("Nothing was launched.\n")
            output_stream.flush()
            return 0
    problems = _transition_preflight_problems(runtime)
    if problems:
        raise cli_errors.CLIError(cli_text.RELAUNCH_PREFLIGHT_REFUSAL + "; ".join(problems))
    prepared = runtime.prepare(
        target, action="resume", passthrough=[], session_id=mid, expected=sample
    )
    _launch_notes(prepared, output_stream)
    try:
        return runtime.perform(prepared)
    except OSError as exc:
        # perform_launch already put the prior record, scope and pointer back
        # when they were still this attempt's (CAS-by-own-write).
        raise cli_errors.CLIError(
            f"relaunch exec failed ({exc}); the prior record and scope were restored "
            f"where this attempt still owned them — check with claude-multi doctor, "
            f"then retry"
        ) from exc


def _nothing_connected(runtime: runtime_mod.Runtime) -> bool:
    """True when no provider is connected here (one gateway observation;
    an observation that fails counts as connected, so nothing is offered)."""

    from claude_multi.setup import status

    try:
        return not status.connected(runtime)
    except (cli_errors.ClaudeMultiError, OSError, ValueError, KeyError):
        return False


def _offer_setup_line(runtime: runtime_mod.Runtime, input_stream: TextIO, output_stream: TextIO) -> bool:
    """A fresh launch in line mode while nothing is connected: say so and
    offer ``claude-multi setup`` first (Enter or y runs it). True when setup
    ran, so the caller resolves the default profile again."""

    import claude_multi.cli.commands.setup as commands_setup
    import claude_multi.cli.consent as consent
    from claude_multi.setup import texts

    if consent.session_marker(runtime.environ) is not None or not runtime.allow_state_writes:
        return False
    if not _nothing_connected(runtime):
        return False
    output_stream.write(texts.LINE_NOT_CONNECTED + "\n")
    output_stream.write(texts.LINE_SETUP_NOW)
    output_stream.flush()
    try:
        answer = input_stream.readline()
    except KeyboardInterrupt:
        answer = ""
    if not answer or answer.strip().lower() not in ("", "y", "yes"):
        return False
    write = commands_setup._writer(output_stream)
    try:
        commands_setup._bare(runtime, redo=False, input_stream=input_stream, output_stream=output_stream,
                             write=write)
    except KeyboardInterrupt:
        write(texts.SETUP_CANCELLED)
    return True


def _confirm_launch_line(
    plan: cli_types.PreparedLaunch,
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    runtime: runtime_mod.Runtime | None = None,
) -> bool:
    """The line confirm (dumb TERM, no ctty, ``--line``, no curses).

    The line confirm's signature, keys and texts; only the body is the card's
    line form (``views.card_text(..., line_mode=True)``, rows 1–16 of §4.1.1).
    ``runtime`` (optional) adds the context-meter note row.
    """

    record = plan.record
    compaction = record["applied"]["lead"]["compaction"]
    card = views.card_model(
        target=plan.target,
        action=selection._card_action(plan),
        record=record,
        lineup=plan.lineup,
        context={key: compaction[key] for key in launch_sessions._CARD_CONTEXT_KEYS},
        eff=None,
        errors=(),
        secret_problems=(),
        health=None,
        update_hint=None,
        seed_updates=(),
        radar_hits=0,
        diff=(),
        notices=(),
        providers=(runtime.lineup_catalog().providers if runtime is not None else None),
        lead_origin=(launch_sessions._lead_origin(runtime, plan.lineup) if runtime is not None else None),
        workflow_default=getattr(plan, "workflow_window", None),
    )
    text = views.card_text(card, streams._stream_width(output_stream), line_mode=True)
    for line in text.splitlines():
        output_stream.write(termtext.visible_text(line) + "\n")
    if plan.diff:
        output_stream.write("changes at this launch:\n")
        for line in plan.diff:
            output_stream.write(termtext.visible_text(line) + "\n")
    for notice in plan.notices:
        output_stream.write(termtext.visible_text(notice) + "\n")
    if plan.secret_problems:
        for problem in plan.secret_problems:
            output_stream.write(f"blocked: {termtext.visible_text(problem)}\n")
        output_stream.write("q quit: ")
        output_stream.flush()
        input_stream.readline()
        output_stream.write("Nothing was launched.\n")
        output_stream.flush()
        return False
    output_stream.write("Enter launch · q quit: ")
    output_stream.flush()
    answer = input_stream.readline()
    if answer in ("\n", "\r\n") or answer.strip().lower() in ("y", "yes"):
        return True
    if answer and answer.strip() == "" and answer.endswith("\n"):
        return True
    output_stream.write("Nothing was launched.\n")
    output_stream.flush()
    return False


def _session_row_marker(record: dict[str, Any], summary: sessions.RecordSummary, live: frozenset[str]) -> str:
    try:
        if session_facts._record_is_live(record, live):
            return "●"
    except (KeyError, sessions.SessionError):
        pass
    if record.get("pending_forks"):
        return "⚠"
    if summary.identity_state == sessions.IDENTITY_REPAIR_NEEDED or summary.needs_choice:
        return "!"
    if summary.pending:
        return "↻"
    return " "


def _choose_session_line(
    runtime: runtime_mod.Runtime, input_stream: TextIO, output_stream: TextIO
) -> str | None:
    """Numbered sessions, then ``resume which session``; the managed id or None."""

    records = sorted(
        session_facts._session_records(runtime), key=session_facts._record_sort_key_last_used, reverse=True
    )
    if not records:
        output_stream.write("(no recorded sessions)\n")
        output_stream.flush()
        return None
    live = session_facts._live_background_prefixes()
    lcat = runtime.lineup_catalog()
    for index, record in enumerate(records, start=1):
        summary = sessions.record_summary(
            record, lcat if record.get("version") == sessions.RECORD_VERSION else None
        )
        follow = "-" if summary.follow is None else ("follow" if summary.follow else "pinned")
        output_stream.write(
            termtext.visible_text(
                f"{index:>3}. {_session_row_marker(record, summary, live)} "
                f"{summary.managed_id[:8]}  {summary.profile_label:<16} "
                f"{summary.lead_key:<18} agents {summary.agent_count:<2} {follow:<7} "
                f"{session_facts._record_last_used_age(record)}  {summary.cwd}"
            )
            + "\n"
        )
    output_stream.write("resume which session (number, Enter cancels): ")
    output_stream.flush()
    try:
        answer = (input_stream.readline() or "").strip()
    except KeyboardInterrupt:
        return None
    if not answer:
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(records):
        raise cli_errors.CLIError(f"no session numbered {answer!r}")
    return sessions.managed_id(records[int(answer) - 1])


def _choose_lead_line(
    runtime: runtime_mod.Runtime, exc: cli_types.NeedsChoiceError, input_stream: TextIO, output_stream: TextIO
) -> cli_types.LaunchTarget | None:
    """The line chooser for a session whose lead was removed."""

    mid = sessions.managed_id(exc.record)
    m8 = mid[:8]
    output_stream.write(
        f"session {m8} needs a lead choice: its lead {exc.key} was removed "
        f"({exc.notice}). Enter a profile name, or direct:<model> (Enter cancels): "
    )
    output_stream.flush()
    answer = (input_stream.readline() or "").strip()
    if not answer:
        return None
    if answer.startswith("direct:"):
        model = answer.removeprefix("direct:").strip()
        return cli_types.LaunchTarget(
            "relaunch", profile_mod.ad_hoc_direct(model), None, False, f"Relaunch {m8}"
        )
    return cli_types.LaunchTarget("relaunch", None, answer, True, f"Relaunch {m8}")


def _resume_flow(
    runtime: runtime_mod.Runtime,
    session_id: str,
    *,
    passthrough: list[str],
    interactive: bool,
    confirm: bool,
    input_stream: TextIO,
    output_stream: TextIO,
    report_stream: TextIO,
    force: bool,
    print_launch: bool,
    lead_override: str | None = None,
    no_subagents: bool | None = None,
    line: bool = True,
    no_color: bool = False,
) -> int:
    """Every resume goes through prepare/perform.

    An interactive entry that opts in (``line=False``) on a
    curses-capable terminal gets the resume card for a diff, notices or
    secret problems; the default (``line=True``) keeps the line confirm.
    Resume is always prepare-first (the record exists). A prepare
    that raises ``NeedsChoiceError`` opens ``_NeedsChoiceChooser`` (then the
    resume card) on the same condition, else ``_choose_lead_line``.
    """

    record = runtime.session_store.resolve(session_id)
    mid = sessions.managed_id(record)
    target = cli_types.LaunchTarget(
        "record",
        None,
        record.get("profile") if record.get("version") == sessions.RECORD_VERSION else None,
        bool(record.get("follow", False)),
        f"Session {mid[:8]} lineup",
    )
    if print_launch:
        prepared = runtime.prepare(
            target,
            action="resume",
            passthrough=passthrough,
            session_id=mid,
            lead_override=lead_override,
            no_subagents=no_subagents,
            read_only=True,
        )
        _print_launch_plan(prepared, report_stream)
        return _print_launch_verdict(prepared, report_stream)
    try:
        prepared = runtime.prepare(
            target,
            action="resume",
            passthrough=passthrough,
            session_id=mid,
            lead_override=lead_override,
            no_subagents=no_subagents,
        )
    except cli_types.NeedsChoiceError as exc:
        if not interactive:
            raise
        if screens_common._curses_ok(line, input_stream, output_stream):
            # The chooser, then the
            # resume card; performed after teardown.
            try:
                result = launch_sessions._needs_choice_tui(
                    runtime,
                    exc,
                    passthrough=passthrough,
                    input_stream=input_stream,
                    output_stream=output_stream,
                    no_color=no_color,
                )
            except (tui.CursesError, OSError):
                pass  # curses could not start: the line chooser below
            else:
                if result is None:
                    output_stream.write("Nothing was launched.\n")
                    output_stream.flush()
                    return 0
                return _perform_card_result(runtime, result, force=force)
        choice = _choose_lead_line(runtime, exc, input_stream, output_stream)
        if choice is None:
            output_stream.write("Nothing was launched.\n")
            output_stream.flush()
            return 0
        prepared = runtime.prepare(
            choice, action="resume", passthrough=passthrough, session_id=mid
        )
    if interactive and confirm and (
        prepared.diff or prepared.notices or prepared.secret_problems
    ):
        if screens_common._curses_ok(line, input_stream, output_stream):
            try:
                result = launch_sessions.launch_card(
                    runtime,
                    prepared.target,
                    action=selection._card_action(prepared),
                    session_id=mid,
                    prepared=prepared,
                    resume_decision="force" if force else None,
                    passthrough=passthrough,
                    input_stream=input_stream,
                    output_stream=output_stream,
                    no_color=no_color,
                )
            except (tui.CursesError, OSError):
                pass  # curses could not start: the line confirm below
            else:
                if result is None:
                    output_stream.write("Nothing was launched.\n")
                    output_stream.flush()
                    return 0
                return _perform_card_result(runtime, result, force=force)
        if not _confirm_launch_line(prepared, input_stream, output_stream, runtime=runtime):
            return 0
    else:
        _launch_notes(prepared, sys.stderr)
    _skill_policy_notices(prepared)
    return runtime.perform(prepared, resume_decision="force" if force else None)


DIRECT_NEEDS_MODEL = "claude-multi direct needs a model: there is no terminal to pick one in"
DIRECT_MODEL_REMEDY = "claude-multi direct --model MODEL (the models: claude-multi models list)"
DIRECT_RESUME_NEEDS_SESSION = "claude-multi direct -r needs the session to resume"
DIRECT_RESUME_REMEDY = ("claude-multi direct -r SESSION (its id, an id prefix of 8 or more characters, or its "
                        "name); claude-multi -r with no value opens the sessions picker")


def _direct_preflight(args: argparse.Namespace, *, interactive: bool) -> None:
    """The refusals of ``direct`` that need nothing but the command line and
    whether a terminal is there: ``-r`` without its session, and a bare
    ``direct`` (no ``--model``, no resume) without a terminal for its picker
    or with ``--print-launch``. ``main`` runs it before the channel is
    recorded or a Runtime is built; ``_direct_command`` runs it again."""

    if args.direct_resume == "":
        raise cli_types.UsageError(DIRECT_RESUME_NEEDS_SESSION, remedy=DIRECT_RESUME_REMEDY)
    bare = args.direct_model is None and args.direct_resume is None and not args.direct_continue
    if bare and (not interactive or args.print_launch):
        raise cli_types.UsageError(DIRECT_NEEDS_MODEL if not interactive else
                                   "--print-launch needs a model to print", remedy=DIRECT_MODEL_REMEDY)


def _direct_line_choice(runtime: runtime_mod.Runtime, input_stream: TextIO, output_stream: TextIO) -> str | None:
    """The line form of the Direct picker: the offered lead-capable models,
    numbered (no gateway read); the chosen key, or None (Enter cancels)."""

    from claude_multi import custom

    try:
        eff = runtime.current_effective()
    except cli_errors.CLIError:
        eff = screens_common._default_effective(runtime)
    lcat = runtime.lineup_catalog()
    rows = views.direct_rows(
        views.line_rows(lcat, eff, custom_ids=frozenset(custom.load_registry(runtime.environ)["models"])),
        marks={},
    )
    if not rows:
        output_stream.write("No model can lead a direct session here; see claude-multi models list.\n")
        output_stream.flush()
        return None
    for index, row in enumerate(rows, start=1):
        output_stream.write(termtext.visible_text(f"{index:>3}. {row.key:<24} {row.text()}") + "\n")
    output_stream.write("direct session with which model (number or key, Enter cancels): ")
    output_stream.flush()
    # Ctrl-C reaches the shared handler (exit 130); Enter alone cancels.
    answer = (input_stream.readline() or "").strip()
    if not answer:
        return None
    keys = [row.key for row in rows]
    if answer.isdigit() and 1 <= int(answer) <= len(rows):
        return keys[int(answer) - 1]
    if answer in keys:
        return answer
    raise cli_errors.CLIError(f"no model {answer!r} in the list", remedy=DIRECT_MODEL_REMEDY)


def _direct_pick(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
    passthrough: list[str],
) -> tuple | str | None:
    """Bare ``direct``: the Direct picker on a terminal (the screen, or its
    line form with ``--line``); refused without one, before anything is
    prepared or written. Returns the screen's ``("perform", prepared)``, a
    model key from the line form, or None when nothing was chosen."""

    _direct_preflight(args, interactive=interactive)
    if screens_common._curses_ok(bool(getattr(args, "line", False)), input_stream, output_stream):
        import claude_multi.cli.screens.direct as screens_direct

        palette = tui.detect_palette(runtime.environ, no_color=bool(getattr(args, "no_color", False)),
                                     tty_in=input_stream, tty_out=output_stream)
        try:
            return screens_common._run_screen_curses(
                lambda win: screens_direct.run_direct_screen(
                    runtime, win, palette, passthrough=passthrough,
                    no_subagents=getattr(args, "no_subagents", None)),
                input_stream=input_stream, output_stream=output_stream, palette=palette,
                what="the direct screen",
            )
        except (tui.CursesError, OSError):
            pass  # curses could not start: the line form below
    return _direct_line_choice(runtime, input_stream, output_stream)


def _direct_command(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool,
    passthrough: list[str],
) -> int:
    """``claude-multi direct``: an ad-hoc direct lineup on the one launch path.

    ``--model`` names the lead; without it a terminal opens the Direct
    picker and anything else is refused with the ``--model`` remedy. A
    resume (``-r SESSION``, ``-c``) keeps the session's lineup unless
    ``--model`` changes its lead."""

    identifier = args.direct_resume
    force = bool(getattr(args, "force", False))
    no_subagents = getattr(args, "no_subagents", None)
    _direct_preflight(args, interactive=interactive)
    if identifier is not None:
        identifier = selection._resolve_resume_target(runtime, identifier)
    if args.direct_continue:
        identifier = runtime.session_store.last(runtime.cwd)
        if identifier is None:
            raise cli_errors.CLIError("no remembered session in this directory")
    if identifier is not None:
        if args.direct_model is not None and not args.print_launch:
            prior = runtime.session_store.resolve(identifier)
            old_class = (
                prior.get("lead_class")
                if prior.get("version") == sessions.RECORD_VERSION
                else None
            )
            try:
                entry = runtime.lineup_catalog().lines.get(args.direct_model) or {}
                new_class = (entry.get("context") or {}).get("ordinary_profile")
            except (KeyError, AttributeError):
                new_class = None
            if old_class != new_class:
                output_stream.write(
                    f"note: cross-class relaunch ({old_class or 'unknown'} -> "
                    f"{new_class or 'unknown'} lead class) replaces the session scope "
                    "before the new process starts; make sure the previous process "
                    "has exited.\n"
                )
                output_stream.flush()
        return _resume_flow(
            runtime,
            identifier,
            passthrough=list(passthrough),
            interactive=interactive,
            confirm=False,
            input_stream=input_stream,
            output_stream=output_stream,
            report_stream=output_stream,
            force=force,
            print_launch=bool(args.print_launch),
            lead_override=args.direct_model,
            no_subagents=no_subagents,
        )
    model = args.direct_model
    if model is None:
        picked = _direct_pick(runtime, args, input_stream=input_stream, output_stream=output_stream,
                              interactive=interactive, passthrough=list(passthrough))
        if picked is None:
            output_stream.write("Nothing was launched.\n")
            output_stream.flush()
            return 0
        if isinstance(picked, tuple):
            return _perform_card_result(runtime, picked, force=force)
        model = picked
    target = cli_types.LaunchTarget("ad-hoc", profile_mod.ad_hoc_direct(model), None, False, f"Direct {model}")
    prepared = runtime.prepare(
        target, action="fresh", passthrough=list(passthrough), no_subagents=no_subagents,
        read_only=bool(args.print_launch),
    )
    if args.print_launch:
        _print_launch_plan(prepared, output_stream)
        return _print_launch_verdict(prepared, output_stream, warn_only=True)
    provider_id = prepared.lineup.lead.binding.provider
    provider = runtime.ordinary_docs["providers"]["providers"].get(provider_id, {})
    # The auth-aware predicate (a keyed compat lead warns like a
    # direct one; the ad-hoc warn-only exception is unchanged).
    if provider.get("transport", {}).get("kind") == "direct" or catalog.is_keyed_compat(provider):
        unavailable_reason = gateway_facts._ordinary_unavailable(runtime).get(provider_id)
        if unavailable_reason is not None:
            # Non-blocking: the operator is told why the session's
            # requests may fail before the process starts.
            output_stream.write(
                f"warning: {unavailable_reason} — requests may fail "
                "unless the running gateway still serves an earlier "
                "rendered config.\n"
                f"fix: {gateway_facts._connect_hint(runtime, provider_id)}\n"
            )
    if prepared.secret_problems:
        # Kept for ad-hoc direct: a missing credential warns
        # and never blocks this command, as in the earlier launcher.
        for problem in prepared.secret_problems:
            output_stream.write(f"warning: {problem}\n")
        prepared = dataclasses.replace(prepared, secret_problems=())
    # execve never flushes Python buffers.
    output_stream.flush()
    _skill_policy_notices(prepared)
    return runtime.perform(prepared, resume_decision="force" if force else None)
