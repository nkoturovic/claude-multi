"""Session actions."""

from __future__ import annotations

from claude_multi import errors as cli_errors

from typing import Any
from typing import Callable
from pathlib import Path
from typing import TextIO
import argparse
import claude_multi.cli.text as cli_text
from dataclasses import dataclass
from claude_multi import errors
from claude_multi import launch
from claude_multi import managed
from claude_multi import paths
from claude_multi import profile as profile_mod
import claude_multi.cli.selection as selection
import claude_multi.cli.types as cli_types
import claude_multi.cli.session_facts as session_facts
from claude_multi import sessions
from claude_multi import settings as settings_mod
from claude_multi import state
import subprocess
import sys
from claude_multi import termtext
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def _forget_liveness_guard(
    runtime: runtime_mod.Runtime, stable_id: str, *, force: bool = False
) -> Callable[[dict[str, Any] | None], str | None]:
    """The shared forget refusal, run under the lifecycle lock (review).

    A liveness verdict taken before blocking on the lock is stale by the
    time deletion happens, so both the CLI handler and the picker pass this
    to ``forget_session`` as its under-lock pre-delete check. A corrupt
    record has no runtime id; the stable-id prefix + self checks still
    apply.
    """

    def check(current: dict[str, Any] | None) -> str | None:
        if runtime.environ.get("CLAUDE_MULTI_MANAGED_ID") == stable_id:
            return (
                "refusing to forget the session you are running "
                "inside — exit it first"
            )
        verdict = runtime.background_liveness()
        if not verdict.known:
            if force:
                return None
            return (f"cannot tell whether session {stable_id} is live in the background ({verdict.reason}); "
                    "refusing to forget it — if you are sure it is not running: "
                    f"claude-multi sessions forget {stable_id} --force")
        prefixes = verdict.prefixes
        if current is not None:
            live = session_facts._record_is_live(current, prefixes)
        else:
            live = any(stable_id.startswith(prefix) for prefix in prefixes)
        if live:
            return (
                f"session {stable_id} is live in the background — "
                "stop it first (`claude-multi sessions stop "
                f"{stable_id}`)"
            )
        return None

    return check


# Observed on the pinned client: "Compacted" renders ~0.5 s before the
# compact_boundary entry is on disk. The SessionStart ``compact`` hook marks
# the record; a launcher-driven stop inside this window after it waits.
COMPACT_BOUNDARY_WAIT_SECONDS = 3.0


def _compact_boundary_wait(
    record: dict[str, Any] | None,
    *,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> float:
    """Wait until a just-compacted session's boundary is durable; seconds waited.

    Metadata only: the record's ``last_event_source``/``last_seen_at``, never
    the transcript. A record whose last event is not ``compact``, or whose
    compaction is older than the window, waits for nothing.
    """

    if not record or record.get("last_event_source") != "compact":
        return 0.0
    when = session_facts._parse_registry_instant(str(record.get("last_seen_at", "")))
    if when is None:
        return 0.0
    import time as time_mod

    now = (clock or time_mod.time)()
    remaining = when.timestamp() + COMPACT_BOUNDARY_WAIT_SECONDS - now
    if remaining <= 0 or remaining > COMPACT_BOUNDARY_WAIT_SECONDS:
        return 0.0
    (sleep or time_mod.sleep)(remaining)
    return remaining


def _stop_runtime(
    runtime: runtime_mod.Runtime,
    runtime_id: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    record: dict[str, Any] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> str | None:
    """Upstream `claude stop <id>` via the verified binary; error text or None.

    This is the only process-lifecycle action claude-multi takes, and it goes
    through upstream's own public CLI — never a signal, never daemon
    internals. The conversation is always kept (upstream guarantee). A
    session compacted moments ago is stopped only after that window
    (``_compact_boundary_wait``), so a stop-and-resume never replays the
    pre-compaction history.
    """

    _compact_boundary_wait(record, clock=clock, sleep=sleep)
    contract = runtime.catalog.docs["native-contract"]
    # The use lock keeps pruning away from the verified copy while it runs.
    pinned = launch.PinnedCopy(contract, runtime.environ)
    try:
        status = pinned.verify(retained_root=paths.retained_root(runtime.environ))
        run = subprocess.run if runner is None else runner
        try:
            outcome = run(
                [str(status.inspected_path), "stop", runtime_id],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "HOME": runtime.environ.get("HOME", "/")},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
    finally:
        pinned.release()
    if outcome.returncode != 0:
        tail = ((outcome.stdout or "") + (outcome.stderr or "")).strip()
        return tail or f"exit code {outcome.returncode}"
    return None


def _stop_precheck(runtime: runtime_mod.Runtime, record: dict[str, Any], *, force: bool = False) -> str | None:
    """Shared stop guards; a refusal message, or None when stop is possible.

    Liveness is checked for the exact resume target that `claude stop`
    will be invoked for (the current runtime id). A live historical alias
    is a different native session — out of this command's scope (review:
    precheck and stop must target the same identity).
    """

    stable_id = sessions.managed_id(record)
    if runtime.environ.get("CLAUDE_MULTI_MANAGED_ID") == stable_id:
        return "refusing to stop the session you are running inside"
    verdict = runtime.background_liveness()
    if not verdict.known:
        if force:
            return None
        return (f"cannot tell whether session {stable_id} is live in the background ({verdict.reason}); "
                "refusing to stop it — to send claude stop anyway: "
                f"claude-multi sessions stop {stable_id} --force")
    runtime_id = sessions.runtime_session_id(record)
    if not any(
        runtime_id.startswith(prefix) for prefix in verdict.prefixes
    ):
        return (
            "not live in the background — nothing to stop (if it is attached "
            "in a terminal, exit it there)"
        )
    return None


# ------------------------------------------------------ sessions link


@dataclass(frozen=True)
class _NoCompile:
    """``applied.lead.compaction`` of a record nothing was compiled for (gen 0)."""

    window: None = None
    trigger: None = None
    percent: None = None
    scalar: None = None


def _sessions_link(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    output_stream: TextIO,
    interactive: bool,
) -> int:
    """``sessions link [UUID] --profile N | --direct M`` (§11.2): a v4 record at gen 0."""

    if args.uuid is None:
        output_stream.write(
            "Only sessions launched through claude-multi (or adopted) appear in\n"
            "the sessions list.\n"
            "adopt a native fork: claude-multi sessions link <fork-uuid> --profile NAME "
            "| --direct MODEL [--cwd DIR]\n"
            "Add `--cwd DIR` when native project-slug decoding is ambiguous.\n"
        )
        return 0
    if not sessions.UUID4.fullmatch(args.uuid):
        raise cli_errors.CLIError(f"{args.uuid!r} is not a UUIDv4")
    name = getattr(args, "link_profile", None)
    model = getattr(args, "link_direct", None)
    if getattr(args, "link_composition", None) is not None:
        sys.stderr.write(cli_text.ALIAS_NOTE.format(old="--composition", new="--profile") + "\n")
        name = args.link_composition
    if getattr(args, "link_model", None) is not None:
        sys.stderr.write(cli_text.ALIAS_NOTE.format(old="--model", new="--direct") + "\n")
        model = args.link_model
    if name is None and model is None:
        if not interactive:
            raise cli_types.UsageError("sessions link needs --profile NAME or --direct MODEL when not interactive")
        name = selection.remembered_profile(runtime)
    lcat = runtime.lineup_catalog()
    eff = runtime.current_effective()
    if model is not None:
        document = profile_mod.ad_hoc_direct(model)
        profile_name, follow = None, False
    else:
        selection._profile_name_or_composition(runtime, str(name))
        if runtime.allow_state_writes:
            runtime.profiles.install_seeds()
        document = runtime.profiles.load(str(name))
        profile_name, follow = str(name), True
    lineup = runtime._evaluate(document, profile_name=profile_name, effective=eff, lcat=lcat)
    adopted_cwd = session_facts._original_cwd_for_adopt(runtime, args.uuid, explicit_cwd=args.link_cwd)
    store = runtime.session_store
    stable_id = store.new_id()
    applied = sessions.applied_from_lineup(
        lineup,
        context=_NoCompile(),
        settings_snapshot=settings_mod.snapshot(eff),
        no_subagents=False,
    )
    record = sessions.make_v4_record(
        managed_id=stable_id,
        cwd=adopted_cwd,
        profile=profile_name,
        follow=follow,
        applied=applied,
        lineup_generation=0,
        lead_class=lineup.lead_class,
        catalog_version=runtime.catalog_version,
        catalog_hash=runtime.catalog.bundle_sha256,
        launcher_version=runtime.launcher_version,
        launch_epoch=0,
        mutation_token=sessions.new_mutation_token(),
        identity_state=sessions.IDENTITY_AUTHORITATIVE,
    )
    record["runtime_session_id"] = args.uuid
    sessions.ensure_state_v4(store.root)  # marker first, then the shared hold
    # Adoption creates a managed reference — one
    # served-change phase (migration guard -> barrier), the root authority
    # revalidated inside it.
    try:
        with sessions.served_change_phase(store.root, runtime.home):
            refusal = runtime.root_authority_refusal()
            if refusal is not None:
                raise cli_errors.CLIError(refusal)
            store.link(record)
    except state.BarrierBusyError as exc:
        raise cli_errors.CLIError(f"sessions link: {exc.strerror} — nothing linked; retry") from exc
    what = (
        f"following profile {profile_name!r}"
        if profile_name is not None
        else f"as an ad-hoc direct session on {model!r}"
    )
    output_stream.write(
        f"Linked runtime {args.uuid} as {stable_id} {termtext.visible_text(what)} (lineup "
        f"generation 0: its first `claude-multi -r {stable_id}` compiles the lineup-enabled scope).\n"
    )
    return 0


def _mark_ended_ids(
    runtime: runtime_mod.Runtime,
    ids: list[str],
    *,
    proc_root: Path | str = "/proc",
) -> list[tuple[str, str]]:
    """Synthetic ``end`` for each id; ``[(id, "marked"|"already ended"|error)]``.

    An unreadable process table, or background (daemon) liveness that cannot
    be observed, cannot prove a session stopped: every record without a
    recorded end is refused, and nothing is written.
    """

    daemon = runtime.background_liveness()
    scan = sessions.proc_session_scan(proc_root)

    def is_live(view: dict[str, Any], managed: str) -> bool:
        if not daemon.known:
            raise sessions.SessionError(
                f"cannot tell whether session {managed} is live in the background "
                f"({daemon.reason}); nothing written"
            )
        if not scan.known:
            raise sessions.SessionError(
                f"cannot tell whether session {managed} is running (the process table "
                f"is unreadable: {scan.reason}); nothing written"
            )
        return session_facts._record_view_is_live(view, prefixes=daemon.prefixes, proc_ids=scan.ids)

    results = []
    for managed in ids:
        try:
            changed = runtime.session_store.mark_ended(
                managed, is_live=lambda view, managed=managed: is_live(view, managed),
            )
        except sessions.SessionError as exc:
            results.append((managed, f"refused: {exc}"))
            continue
        results.append((managed, "marked ended" if changed else "already ended"))
    return results
