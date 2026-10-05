"""The ``migrate`` and ``restore-2x`` commands."""

from __future__ import annotations

from claude_multi import migrate as migrate_mod
from claude_multi import paths as product_paths
from claude_multi import profile as profile_mod
from claude_multi import scope as scope_mod
from claude_multi import sessions
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Mapping
from typing import TextIO
import argparse
import json
import os
import sys
import claude_multi.cli.resume_checks as resume_checks
import claude_multi.cli.session_facts as session_facts
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


def _restore_2x(
    state_path: Path,
    output_stream: TextIO,
    args: argparse.Namespace | None = None,
    *,
    input_stream: TextIO | None = None,
    interactive: bool | None = None,
    error_stream: TextIO | None = None,
    proc_root: Path | str = "/proc",
    environ: Mapping[str, str] | None = None,
) -> int:
    """``claude-multi restore-2x`` (dispatched before ``main``'s marker gate).

    Marker > 4, malformed or unsafe -> the marker error, exit 2, nothing
    touched (sc[15]); missing (3) -> nothing to restore, exit 0 — both before
    any store directory is created. Marker 4 -> ``migrate.restore_2x`` with a
    bare ``SessionStore`` (no Runtime, no shim refresh, no catalog). A
    refusal is exit 2 with nothing changed; on a terminal the live/unknown
    records are listed with a y/N (yes records their end).

    A narrow exception: when the caller's own
    ``CLAUDE_MULTI_MANAGED_ID`` is listed in ``--assume-dead``, that record
    alone is checked against the ``/proc`` argvs of processes that are not
    the caller or its ancestors (its own client names it). The daemon ●
    check and ``--not-running`` are never overridden. ``proc_root`` and
    ``environ`` (default ``os.environ``) are the test seams.
    """

    errors = error_stream or sys.stderr
    if getattr(args, "check", False):
        if getattr(args, "not_running", None) or getattr(args, "assume_dead", None):
            errors.write("claude-multi: --check cannot record liveness overrides\n")
            return 2
        env = dict(os.environ if environ is None else environ)
        report = migrate_mod.restore_check(state_path, product_paths.config_root(env))
        if getattr(args, "json_output", False):
            output_stream.write(json.dumps(report, sort_keys=True) + "\n")
        else:
            output_stream.write(migrate_mod.RESTORE_CHECK_CAVEAT + "\n")
            output_stream.write(f"State root: {state_path}\nMetadata: {report['status']}\n")
            for item in report["checks"]:
                output_stream.write(f"  {item['path']}: {item['status']}\n")
        return 1 if report["status"] == "refused" else 0
    if getattr(args, "json_output", False):
        errors.write("claude-multi: --json requires --check\n")
        return 2
    try:
        version = sessions.check_state_marker(state_path)
    except sessions.StateMarkerError as exc:
        errors.write(f"claude-multi: {exc}\n")
        return 1
    if version <= sessions.UNMARKED_STATE_VERSION:
        output_stream.write(
            "No current-format state exists here; nothing to restore.\n"
        )
        return 0
    not_running = list(getattr(args, "not_running", None) or [])
    assume_dead = list(getattr(args, "assume_dead", None) or [])
    for value in not_running + assume_dead:
        if not sessions.UUID4.fullmatch(value):
            errors.write(f"claude-multi: restore-2x: {value!r} is not a managed session UUID\n")
            return 2
    store = sessions.SessionStore(state_path, sessions.default_schema())
    self_id = (os.environ if environ is None else environ).get("CLAUDE_MULTI_MANAGED_ID")
    override = self_id if self_id in assume_dead else None

    def liveness() -> Callable[[dict[str, Any]], bool]:
        """A fresh daemon + ``/proc`` inventory; RestoreRefused when unknown.
        Read for the preview and again inside the commit phase."""

        daemon = sessions.background_liveness()
        if not daemon.known:
            raise migrate_mod.RestoreRefused([
                "claude-multi: restore-2x: cannot tell which sessions are live in the background "
                f"({daemon.reason}); nothing restored"
            ])
        prefixes = daemon.prefixes
        scan = sessions.proc_session_scan(proc_root)
        foreign_scan = scan
        if override is not None and scan.known:
            foreign_scan = sessions.proc_session_scan(
                proc_root, exclude_pids=sessions.ancestor_pids(proc_root)
            )
        if not (scan.known and foreign_scan.known):
            reason = scan.reason if not scan.known else foreign_scan.reason
            raise migrate_mod.RestoreRefused([
                f"claude-multi: restore-2x: cannot tell which sessions are running (the process "
                f"table is unreadable: {reason}); nothing restored"
            ])
        proc_ids, foreign = scan.ids, foreign_scan.ids

        def is_live(view: dict[str, Any]) -> bool:
            own = override is not None and (
                view.get("managed_id", view.get("session_id")) == override
            )
            return session_facts._record_view_is_live(
                view, prefixes=prefixes, proc_ids=foreign if own else proc_ids
            )

        return is_live

    try:
        is_live = liveness()
    except migrate_mod.RestoreRefused as exc:
        for line in exc.lines:
            errors.write(f"{line}\n")
        return 1

    confirm: Callable[[Any], bool] | None = None
    declined: list[bool] = []
    if interactive:
        reader = input_stream or sys.stdin

        def ask_restore(pairs: Any) -> bool:
            output_stream.write(
                f"{len(pairs)} session(s) have no recorded end (live or unknown):\n"
            )
            for managed, source in pairs:
                output_stream.write(f"  {managed}  last event {source or 'none'}\n")
            output_stream.write(
                "Treat them as exited and record their end, then restore? [y/N] "
            )
            output_stream.flush()
            answer = reader.readline()
            output_stream.write("\n" if not answer.endswith("\n") else "")
            agreed = answer.strip().lower() in ("y", "yes")
            if not agreed:
                declined.append(True)
            return agreed

        confirm = ask_restore

    try:
        report = migrate_mod.restore_2x(
            store,
            # The commit phase takes the gateway served-change
            # barrier, which is HOME-relative like the api-key leaf.
            home=product_paths.home(os.environ if environ is None else environ),
            not_running=not_running,
            assume_dead=assume_dead,
            is_live=is_live,
            confirm=confirm,
            refresh_live=liveness,
        )
    except migrate_mod.RestoreRefused as exc:
        for line in exc.lines:
            errors.write(line if line.startswith("claude-multi: ") or line.startswith("  ")
                         else f"claude-multi: {line}")
            errors.write("\n")
        return 3 if declined else 1
    except migrate_mod.RestoreStopped as exc:
        output_stream.write(migrate_mod.render_restore(exc.done))
        errors.write(f"claude-multi: {exc}\n")
        return 1
    output_stream.write(migrate_mod.render_restore(report))
    return 0


def _migrate_command(
    runtime: runtime_mod.Runtime, args: argparse.Namespace, output_stream: TextIO
) -> int:
    """``claude-multi migrate [--dry-run] [--json]``."""

    store = runtime.session_store
    lcat = runtime.lineup_catalog()
    profiles = profile_mod.ProfileStore.for_catalog(runtime.catalog, runtime.environ)
    if args.dry_run:
        user_settings = _user_claude_settings(runtime)
        report = migrate_mod.plan(
            store,
            lcat,
            profile_exists=profiles.contains,
            transcript_check=lambda view: _transcript_presence(runtime, view),
            cleanup_days=migrate_mod.cleanup_period_days(user_settings),
        )
    else:
        report = migrate_mod.run(
            store,
            lcat,
            hook_command=runtime.resolved_hook_command,
            hook_shim_path=scope_mod.hook_shim_path(store.root),
            profile_exists=profiles.contains,
            home=runtime.home,
            barrier_timeout=runtime.served_barrier_timeout,
        )
    if args.json_output:
        output_stream.write(migrate_mod.render_json(report).decode("utf-8"))
    else:
        output_stream.write(migrate_mod.render_text(report))
    output_stream.flush()
    return 0 if report.ok else 1


def _user_claude_settings(runtime: runtime_mod.Runtime) -> Any:
    """The managed client's ``~/.claude/settings.json`` (≤ 1 MiB; tolerant).

    Never ``$CLAUDE_CONFIG_DIR``: managed sessions run without it, so their
    client applies HOME's ``cleanupPeriodDays``."""

    base = product_paths.claude_settings_dir(runtime.environ)
    path = base / "settings.json"
    try:
        with open(path, "rb") as handle:
            raw = handle.read((1 << 20) + 1)
        if len(raw) > (1 << 20):
            return None
        return json.loads(raw)
    except (OSError, ValueError):
        return None


def _transcript_presence(runtime: runtime_mod.Runtime, view: dict[str, Any]) -> tuple[str, float | None]:
    """The transcript's existence and mtime by path only."""

    try:
        status, detail, _decoded = resume_checks._resume_transcript_status(runtime, view)
    except (KeyError, OSError, sessions.SessionError):
        return ("elsewhere", None)
    if status != "present":
        return (status, None)
    try:
        return ("present", Path(detail).stat().st_mtime)
    except OSError:
        return ("missing", None)
