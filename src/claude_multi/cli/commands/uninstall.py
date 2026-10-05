"""``claude-multi uninstall [--dry-run] [--keep-setup] [--keep-credentials] [--yes] [--force]``.

The plan comes first; nothing is removed before the y/N (``--yes`` skips it
on a terminal), and credentials only after the typed phrase, which no flag
supplies. Sessions that may still run (a record without a recorded end, a
background or running Claude Code process naming one, or anything that
cannot be read) refuse unless ``--force``. After the confirmation the
session state is locked against every other writer and the sessions are
checked again; a service that cannot be checked refuses; the running
gateway is stopped through its own lifecycle (refused while a sign-in save
is unconfirmed, also for a gateway that is not running, or while a service
hand-off is in progress) and the recorded service goes through its own
uninstall, before any program file or helper goes; then the gateway's start
lock and the lock of every store whose files go are held until the files
are gone (a store still in use refuses before any of its files goes). While
a transaction (an installer, an update, a machine move, a service hand-off)
holds the gateway inhibition, uninstall refuses with its owner and remedy,
before the question and again under the session state's lock and the start
lock. Exit statuses: 0 done; 1 refused or partial (what remains is listed);
2 usage; 3 declined; 130 cancelled.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, TextIO

from claude_multi import errors, termtext
import claude_multi.cli.consent as consent

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod

EXIT_DONE, EXIT_FAILED, EXIT_USAGE, EXIT_DECLINED, EXIT_CANCELLED = 0, 1, 2, 3, 130


def _write(stream: TextIO, lines: list[str]) -> None:
    stream.write("".join(termtext.visible_text(line) + "\n" for line in lines))
    stream.flush()


def command(runtime: "runtime_mod.Runtime", args: argparse.Namespace, *, input_stream: TextIO,
            output_stream: TextIO, interactive: bool) -> int:
    from claude_multi.setup import uninstall
    from claude_multi.setup import texts

    result = uninstall.Result()
    try:
        return _run(runtime, args, input_stream=input_stream, output_stream=output_stream, result=result)
    except KeyboardInterrupt:
        _write(output_stream, [*(f"  removed {item}" for item in result.removed), texts.UNINSTALL_CANCELLED])
        return EXIT_CANCELLED
    except errors.ClaudeMultiError as exc:
        lines = ["claude-multi uninstall: " + termtext.visible_message(exc)]
        if exc.remedy:
            lines.append(f"  fix: {exc.remedy}")
        _write(sys.stderr, lines)
        return EXIT_FAILED


def _refuse(text: str) -> int:
    _write(sys.stderr, ("claude-multi uninstall: " + text).split("\n"))  # a refusal may carry its fix line
    return EXIT_FAILED


def _guard_inhibition(runtime: "runtime_mod.Runtime") -> None:
    """Refuse (:class:`gateway_inhibition.Inhibited`, with the owner and its
    remedy) while a transaction — an installer, an update, a machine move, a
    service hand-off — holds the gateway inhibition of a state root that
    may own this home's gateway; an unreadable ``continuity.json`` (which
    names that root) refuses too. Uninstall never acts for an owner."""

    from claude_multi import gateway_inhibition

    what = "nothing was removed"
    for root in gateway_inhibition.home_roots(runtime.home, runtime.session_store.root, what=what):
        gateway_inhibition.guard(root, what=what)


def _run(runtime: "runtime_mod.Runtime", args: argparse.Namespace, *, input_stream: TextIO, output_stream: TextIO,
         result: object) -> int:
    # Everything the removal needs is loaded before the first file goes (the
    # release being removed may be the one this command runs from).
    from claude_multi import gateway_inhibition  # noqa: F401
    from claude_multi.setup import external, texts, uninstall  # noqa: F401

    environ = runtime.environ
    if consent.session_marker(environ) is not None:
        consent.require_human("claude-multi uninstall", environ)
    plan = uninstall.build_plan(runtime, keep_setup=bool(args.keep_setup))
    _write(output_stream, uninstall.plan_lines(runtime, plan))
    if args.uninstall_dry_run:
        return EXIT_DONE
    consent.require_human("claude-multi uninstall", environ)
    shown = uninstall.session_check(runtime).names()
    if shown and not args.force:
        return _refuse(texts.UNINSTALL_LIVE.format(ids=", ".join(shown)))
    if shown:
        _write(output_stream, [texts.UNINSTALL_FORCED.format(ids=", ".join(shown))])
    _guard_inhibition(runtime)
    if not args.yes and not consent.confirm(texts.UNINSTALL_QUESTION, input_stream=input_stream):
        _write(output_stream, ["Nothing was removed."])
        return EXIT_DECLINED
    assert isinstance(result, uninstall.Result)
    fence = uninstall.Fence(plan)
    try:
        return _remove(runtime, args, plan, fence, shown, input_stream=input_stream, output_stream=output_stream,
                       result=result)
    finally:
        fence.release()


def _remove(runtime: "runtime_mod.Runtime", args: argparse.Namespace, plan: object, fence: object,
            shown: list[str], *, input_stream: TextIO, output_stream: TextIO, result: object) -> int:
    from claude_multi.setup import external, texts, uninstall

    assert isinstance(plan, uninstall.UninstallPlan) and isinstance(fence, uninstall.Fence)
    assert isinstance(result, uninstall.Result)
    environ = runtime.environ
    # Nothing else writes session state from here on; what the confirmation
    # covered is checked again.
    refusal = fence.sessions()
    if refusal is not None:
        return _refuse(refusal)
    _guard_inhibition(runtime)
    now = uninstall.session_check(runtime).names()
    if now and not args.force:
        return _refuse(texts.UNINSTALL_LIVE.format(ids=", ".join(now)))
    started = [name for name in now if name not in shown]
    if started:
        return _refuse(texts.UNINSTALL_STARTED.format(ids=", ".join(started)))
    service, detail = external.service_state(runtime)
    if service == "unknown":
        return _refuse(texts.UNINSTALL_SERVICE_UNKNOWN.format(detail=detail))
    if service == "stray":
        return _refuse(texts.UNINSTALL_SERVICE_STRAY.format(path=detail))
    stopped, detail = external.stop_gateway_for_uninstall(runtime)
    refused = {"hold": texts.UNINSTALL_HOLD, "inhibited": detail,
               "not-ours": texts.UNINSTALL_NOT_OURS.format(port=detail)}.get(stopped)
    if refused is None and stopped == "failed":
        refused = f"the gateway could not be stopped ({detail}); nothing was removed"
    if refused is not None and stopped == "hold":
        _write(sys.stderr, ["claude-multi uninstall: " + refused, f"  why: {detail}"])
        return EXIT_FAILED
    if refused is not None:
        return _refuse(refused)
    if service == "recorded":
        outcome = external.uninstall_service(runtime)
        if not outcome.ok:
            return _refuse("the gateway service could not be removed: " + outcome.message
                           + "; nothing else was removed")
        result.removed.append("the gateway service")
    rest = texts.UNINSTALL_NOTHING_ELSE if result.removed else "nothing was removed"
    refusal = fence.gateway(runtime, what=rest)
    if isinstance(refusal, external.InhibitedText):
        return _refuse(refusal)
    if refusal is not None:
        return _refuse(texts.UNINSTALL_NOT_STOPPED.format(detail=refusal) + "; " + rest)
    # Every store whose files go is held from here until removal ends: one
    # still in use refuses before any of its files goes.
    refusal = fence.stores(runtime, credentials=bool(plan.credentials) and not args.keep_credentials, what=rest)
    if refusal is not None:
        return _refuse(refusal)
    held = fence.held()
    for edit in plan.path_edits:
        uninstall.remove_path_edit(edit, result, environ)
    uninstall.remove_entries(plan.program, result, environ, skip=held)
    uninstall.prune_empty(plan.install_root)
    uninstall.remove_entries(plan.claude, result, environ, skip=held)
    uninstall.prune_empty(plan.claude_root)
    uninstall.remove_entries(plan.sessions, result, environ, skip=held)
    uninstall.remove_entries(plan.setup, result, environ, skip=held)
    credentials_removed = False
    if plan.credentials and not args.keep_credentials:
        prompt = consent.prompt_stream()
        prompt.write(texts.UNINSTALL_CREDENTIALS_QUESTION)
        prompt.flush()
        typed = (consent._answer_stream(input_stream).readline() or "").strip()
        if typed == texts.UNINSTALL_CREDENTIALS_WORDS:
            uninstall.remove_entries(plan.credentials, result, environ, skip=held)
            credentials_removed = True
            if plan.key_names:
                _write(output_stream, [texts.UNINSTALL_REVOKE.format(names=", ".join(plan.key_names))])
    for root in plan.roots():
        uninstall.prune_empty(root)
    # The locks this run holds go last, while still held, as the plan classified them.
    fence.remove_own(result, environ, credentials_removed=credentials_removed)
    for root in plan.roots():
        uninstall.prune_empty(root)
    remains = sorted({*uninstall.remaining_lines(runtime, plan, credentials_removed=credentials_removed),
                      *(f"  {item}" for item in result.kept)})
    lines = [f"  removed {len(result.removed)} file(s)"]
    if result.failed:
        lines += [texts.UNINSTALL_PARTIAL, *(f"  {item}" for item in result.failed)]
    lines.append(texts.UNINSTALL_DONE if not result.failed else texts.UNINSTALL_PARTIAL.rstrip(":") + ".")
    if remains:
        lines += [texts.UNINSTALL_REMAINS, *remains]
    _write(output_stream, lines)
    return EXIT_FAILED if result.failed else EXIT_DONE
