"""Session events."""

from __future__ import annotations

from claude_multi import errors as cli_errors

from typing import Any
from typing import Callable
from typing import Mapping
from pathlib import Path
from typing import TextIO
import argparse
from claude_multi import catalog
from claude_multi import client_check
from claude_multi import errors
from claude_multi import hooks
from claude_multi import lineup_files
import os
from claude_multi import sessions
from claude_multi import strict_json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import claude_multi.cli.runtime as runtime_mod


_SESSION_START_SOURCES = frozenset({"startup", "resume", "clear", "compact", "fork"})


def _direct_model_for_selector(
    runtime: runtime_mod.Runtime, selector: str
) -> tuple[str, str | None] | None:
    """Resolve an ordinary selector to its (line key, ordinary profile) on v2 lines.

    The same result as the legacy
    ``compiler.direct_model_for_selector`` for every live line, read from the
    merged v2 lines instead of the v1 view.  Pass 1 walks the lead lines with
    an ordinary profile in line order (wire, every selector, and ``wire[1m]``
    for a 1M line); pass 2 the profile-less lines in sorted key order (wire and
    selectors only).  New lines stay invisible, as the v1 view omitted them.
    """

    lcat = runtime.lineup_catalog()
    live = {
        key: entry
        for key, entry in lcat.lines.items()
        if entry.get("status", "active") != "new"
    }

    def forms(entry: Mapping[str, Any], one_m: bool) -> set[str]:
        out = {entry["wire_model"]}
        out.update(chosen for _effort, chosen, _contract in catalog.line_selectors(entry))
        if one_m and entry["context"]["client_tokens"] >= 1_000_000:
            # Claude Code may report the canonical full model selector even
            # when the picker entered through a gateway alias.
            out.add(entry["wire_model"] + "[1m]")
        return out

    def profile_of(entry: Mapping[str, Any]) -> str | None:
        return entry["context"].get("ordinary_profile")

    for key, entry in live.items():
        prof = profile_of(entry)
        if "lead" in entry["capabilities"] and prof is not None and selector in forms(entry, True):
            return key, prof
    for key in sorted(live):
        entry = live[key]
        lead_ok = "lead" in entry["capabilities"] and profile_of(entry) is not None
        if not lead_ok and selector in forms(entry, False):
            return key, None
    return None


def _lead_equivalents(cat: catalog.Catalog, key: str, selector: str) -> frozenset[str]:
    """Model reports that mean the recorded managed lead.

    The recorded selector itself; the live line's wire (and ``wire[1m]`` for
    a 1M line) when the record's selector is still one of that line's
    selectors; and the retired entry's ``last_wire`` when the selector is a
    retired one (renamed keys such as ``opus55``, same-key generation moves
    such as ``opus@4.8``; ``resolve_selector``). A catalog miss never
    raises: the forms are then just the selector (as in the earlier launcher).
    """

    forms = {selector}

    def add(wire: Any, tokens: Any) -> None:
        if not isinstance(wire, str) or not wire:
            return
        forms.add(wire)
        if isinstance(tokens, int) and tokens >= 1_000_000:
            forms.add(wire + "[1m]")

    try:
        line = cat.lines.get(key)
        if line is not None and selector in {
            chosen for _effort, chosen, _contract in catalog.line_selectors(line)
        }:
            add(line["wire_model"], line["context"]["client_tokens"])
        hit = cat.resolve_selector(selector)
        if hit is not None:
            add(hit[1].get("last_wire"), hit[1].get("context_tokens"))
    except (KeyError, TypeError, AttributeError):
        pass
    return frozenset(forms)


def _ordinary_equivalent(
    runtime: runtime_mod.Runtime, record: dict[str, Any], model: str
) -> tuple[str, str | None] | None:
    """The ordinary (model, profile) a SessionStart model report maps to.

    ``_direct_model_for_selector`` first (the legacy result, on v2 lines); otherwise a
    retired selector or ``last_wire`` of the record's own (renamed or moved)
    key keeps the recorded model and profile; anything
    else is None (the observation goes repair-needed, as in the earlier launcher).
    """

    resolved = _direct_model_for_selector(runtime, model)
    if resolved is not None:
        return resolved
    recorded = record.get("ordinary_model")
    if not isinstance(recorded, str) or not recorded:
        return None
    keep = (recorded, record.get("context_profile"))
    cat = runtime.catalog
    try:
        hit = cat.resolve_selector(model)
        if hit is not None and (hit[0] == recorded or hit[0].split("@", 1)[0] == recorded):
            return keep
        for key, entry in cat.retired.items():
            if key != recorded and key.split("@", 1)[0] != recorded:
                continue
            wire = entry.get("last_wire")
            if isinstance(wire, str) and wire and model in (wire, wire + "[1m]"):
                return keep
    except (KeyError, TypeError, AttributeError):
        return None
    return None


def _write_session_start_context(output_stream: TextIO, message: str) -> None:
    response = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": message,
        }
    }
    output_stream.write(strict_json.canonical_file_bytes(response).decode("utf-8"))


def _read_session_payload(input_stream: TextIO) -> dict[str, Any]:
    """The bounded hook payload read (errors are CLIError)."""

    # Bound original bytes before parsing (security lane): a hostile or
    # broken hook pipe must not allocate unbounded memory before the
    # strict-JSON limit ever runs.
    limit = strict_json.DEFAULT_LIMITS.max_bytes
    raw_stream = getattr(input_stream, "buffer", None)
    if raw_stream is not None:
        raw = raw_stream.read(limit + 1)
    else:  # injected text streams (tests) have no .buffer
        raw = input_stream.read(limit + 1).encode("utf-8")
    if len(raw) > limit:
        raise cli_errors.CLIError(f"session hook payload exceeds the {limit}-byte limit")
    try:
        payload = strict_json.loads(raw)
    except strict_json.StrictJSONError as exc:
        raise cli_errors.CLIError(f"invalid session hook JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise cli_errors.CLIError("session hook payload is not an object")
    return payload


def _session_event_ids(
    args: argparse.Namespace, payload: dict[str, Any]
) -> tuple[str, int | None, str]:
    """``(managed id, launch epoch, observed runtime id)``; CLIError when invalid."""

    stable_id = args.managed_id
    if not sessions.UUID4.fullmatch(stable_id):
        raise cli_errors.CLIError(f"managed id {stable_id!r} is not a UUIDv4")
    launch_epoch = args.launch_epoch
    if launch_epoch is not None and launch_epoch < 0:
        raise cli_errors.CLIError("launch epoch must be non-negative")
    observed = payload.get("session_id")
    if not isinstance(observed, str) or not sessions.UUID4.fullmatch(observed):
        raise cli_errors.CLIError(f"hook session_id {observed!r} is not a UUIDv4")
    return stable_id, launch_epoch, observed


def _session_start_messages(
    record: dict[str, Any], observed: str, source: str, stable_id: str
) -> list[str]:
    """The legacy SessionStart context after a reconcile: at most one message.

    The fork adopt/discard text for a pending fork, else the repair text for
    a repair-needed model observation, else nothing (bytes unchanged). The
    adopt command is ``sessions.fork_adopt_command`` and the
    repair text ``sessions.relink_message`` for every record version, legacy
    record formats included.
    """

    if source == "fork" and any(
        item.get("session_id") == observed
        for item in record.get("pending_forks", [])
    ):
        adopt_hint = sessions.fork_adopt_command(record, observed)
        return [
            "This native fork does not yet have an independent durable "
            "claude-multi scope, and the parent is fork-blocked until you "
            f"decide. Exit this fork, then either adopt it: `{adopt_hint}`, "
            f"or discard the marker: `claude-multi sessions resolve-fork "
            f"{stable_id} {observed}` (the fork transcript is kept either "
            "way). Note: adopting or discarding advances the parent's "
            "launch epoch, so this fork's later hooks can no longer claim "
            "the parent."
        ]
    if (
        record["identity_state"] == sessions.IDENTITY_REPAIR_NEEDED
        and "observed_model" in record
    ):
        return [sessions.relink_message(record)]
    return []


def _line_forms(lcat: Any, key: str) -> set[str]:
    """Every model report that names ``key``'s line in the MERGED catalog (§10.1).

    A live line (custom entries included): each selector and the wire (+
    ``[1m]`` for a 1M client line); a retired entry: its selectors and
    ``last_wire`` (+ ``[1m]`` for a 1M entry). An unknown key gives the
    empty set; never raises.
    """

    forms: set[str] = set()

    def add_wire(wire: Any, tokens: Any) -> None:
        if isinstance(wire, str) and wire:
            forms.add(wire)
            if isinstance(tokens, int) and tokens >= 1_000_000:
                forms.add(wire + "[1m]")

    try:
        entry = lcat.lines.get(key)
        if entry is not None:
            forms.update(selector for _e, selector, _c in catalog.line_selectors(entry))
            add_wire(entry.get("wire_model"), entry.get("context", {}).get("client_tokens"))
        retired = lcat.retired.get(key)
        if retired is not None:
            forms.update(retired.get("selectors", {}))
            add_wire(retired.get("last_wire"), retired.get("context_tokens"))
    except (KeyError, TypeError, AttributeError):
        return set()
    return forms


def _v3_normalize(
    runtime: runtime_mod.Runtime, view: dict[str, Any], model: str
) -> tuple[str | None, str | None]:
    """The managed/ordinary model normalisation of a v3 view (unchanged)."""

    if view["session_type"] == sessions.SESSION_TYPE_ORDINARY:
        resolved = _ordinary_equivalent(runtime, view, model)
        return (None, None) if resolved is None else resolved
    lead = view["snapshot"]["lead"]
    # Catalog drift (recorded lead removed/renamed) must stay informational:
    # an unknown key only narrows the equivalent forms, never
    # crashes the hook. Renamed keys and same-key generation moves
    # resolve through the retired map (resolve_selector/last_wire).
    if model in _lead_equivalents(runtime.catalog, lead["model"], lead["client_selector"]):
        return lead["client_selector"], None
    return model, None


def _normalize_model_evidence(
    runtime: runtime_mod.Runtime, *, source: str, agent_context: bool
) -> Callable[[dict[str, Any], str | None, str | None], sessions.Evidence]:
    """The ``reconcile_runtime`` callback.

    Runs under the record locks on the version actually loaded: the
    compact/agent-context admissibility rule (v4 and v3 managed drop model
    and cwd on a compact or in an agent context; v3 ordinary only in an
    agent context), then the model: v4 -> ``MODEL_EQUIVALENT`` when the
    report is a form of the recorded lead, else the raw report (no model ->
    ``None``: earlier repair evidence survives); v3 -> the earlier rules.
    """

    cache: dict[str, Any] = {}

    def lineup_cat() -> Any:
        if "cat" not in cache:
            cache["cat"] = runtime.lineup_catalog()  # merged, once per hook
        return cache["cat"]

    def normalize(view: dict[str, Any], observed: str | None, cwd: str | None) -> sessions.Evidence:
        v4 = view.get("version") == sessions.RECORD_VERSION
        managed_rule = v4 or view.get("session_type") == sessions.SESSION_TYPE_MANAGED
        if agent_context or (source == "compact" and managed_rule):
            return sessions.Evidence(None, None, None, None)
        if not observed:
            return sessions.Evidence(None, None, None, cwd)
        if v4:
            key, selector = sessions.lead_identity(view)
            forms: set[str] = (
                set(_lead_equivalents(runtime.catalog, key, selector)) if selector else set()
            )
            forms |= _line_forms(lineup_cat(), key)
            return sessions.Evidence(
                sessions.MODEL_EQUIVALENT if observed in forms else observed,
                None,
                observed,
                cwd,
            )
        model, model_profile = _v3_normalize(runtime, view, observed)
        return sessions.Evidence(model, model_profile, observed, cwd)

    return normalize


def _reconcile_session_start(
    runtime: runtime_mod.Runtime,
    *,
    stable_id: str,
    launch_epoch: int | None,
    observed: str,
    payload: dict[str, Any],
) -> list[str]:
    """Validate one SessionStart payload, reconcile the record, return its messages.

    No lock-free record read decides anything. The
    admissibility rule (model/cwd evidence only from the main session; a
    managed compact is inadmissible — the 2.1.220 compact payload carries
    no agent-context marker) and the model normalisation run in the
    ``normalize`` callback, under the locks, on the version loaded there.
    """

    event_name = payload.get("hook_event_name")
    if event_name not in (None, "SessionStart"):
        raise cli_errors.CLIError(f"expected SessionStart payload, got {event_name!r}")
    source = payload.get("source")
    if source not in _SESSION_START_SOURCES:
        raise cli_errors.CLIError(f"unknown SessionStart source {source!r}")
    cwd = payload.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.startswith("/")):
        raise cli_errors.CLIError(f"hook cwd {cwd!r} is not an absolute path")
    model = payload.get("model")
    if model is not None and not isinstance(model, str):
        raise cli_errors.CLIError("hook model must be a string when present")
    transcript_path = payload.get("transcript_path")
    agent_context = bool(
        payload.get("agent_id")
        or payload.get("agent_transcript_path")
    ) or (isinstance(transcript_path, str) and "/subagents/" in transcript_path)
    record = runtime.session_store.reconcile_runtime(
        stable_id,
        observed_runtime_id=observed,
        source=source,
        cwd=cwd,
        model=model,
        observed_model=model,
        launch_epoch=launch_epoch,
        normalize=_normalize_model_evidence(
            runtime, source=source, agent_context=agent_context
        ),
    )
    return _session_start_messages(record, observed, source, stable_id)


_MIGRATION_SKIP_NOTE = "claude-multi: migration in progress; session event not recorded\n"


def _notice_record_lead(state_path: Path, stable_id: str) -> dict[str, Any] | None:
    """The resume notice's lead, read before ``Runtime`` exists.

    A read-only, lock-free load of the session record (never created: an
    absent file is None without touching the store); a v4 gen >= 1 record
    gives its ``applied.lead`` decorated from ``lead-set.json``
    (``hooks.notice_lead``). Any failure, a legacy or a gen-0 record -> None, and
    ``hooks.read_notice`` falls back to ``lead-set.json["lead"]``.
    """

    try:
        if not os.path.lexists(Path(state_path) / "sessions" / f"{stable_id}.json"):
            return None
        record = sessions.SessionStore(state_path, sessions.default_schema()).load(stable_id)
        return hooks.notice_lead(record, hooks.scope_path(state_path, stable_id))
    except Exception:
        return None


def _session_start_v3(
    args: argparse.Namespace,
    *,
    state_path: Path,
    runtime_factory: Callable[[], "Runtime"],
    input_stream: TextIO,
    output_stream: TextIO,
    error_stream: TextIO,
    environ: Mapping[str, str] | None = None,
) -> int:
    """``session-event start --hook-protocol 3``: the lineup notice + the legacy reconcile.

    The payload is
    read once, the runtime session's ``.seen`` marker is dropped first, the
    notice is built from scope files only, and only then is ``Runtime``
    built (``runtime_factory``) and the record reconciled — any failure
    there becomes one context line plus a stderr note while the notice is
    still written (exit 0). ``.seen`` is written after the output. Without a
    notice (a damaged scope) the legacy error behaviour applies (CLIError, exit
    1 on stderr via ``main``).
    """

    def note(message: str) -> None:
        error_stream.write(f"claude-multi: start hook: {hooks.visible(message)}\n")

    stable_id = args.managed_id
    if not sessions.UUID4.fullmatch(stable_id):
        raise cli_errors.CLIError(f"managed id {stable_id!r} is not a UUIDv4")
    problem: str | None = None
    try:
        payload = hooks.read_payload(input_stream)
    except (hooks.HookInputError, OSError, UnicodeDecodeError) as exc:
        payload = {}
        problem = f"unreadable SessionStart payload: {exc}"
        note(problem)
    observed = payload.get("session_id")
    if not isinstance(observed, str) or not sessions.UUID4.fullmatch(observed):
        observed = None
    source = payload.get("source")
    if not isinstance(source, str):
        source = None
    if observed is not None:
        try:
            hooks.clear_seen(state_path, observed)
        except (OSError, hooks.HookInputError) as exc:
            note(f"notice marker not cleared: {exc}")
    notice: str | None = None
    gen: str | None = None
    try:
        lead = (
            _notice_record_lead(state_path, stable_id)
            if hooks.notice_key("SessionStart", source) == "resume"
            else None
        )
        notice, gen = hooks.read_notice(
            state_path, stable_id, event="SessionStart", source=source, lead=lead
        )
    except Exception as exc:  # the notice is best effort; the reconcile still runs
        note(f"no lineup notice: {exc}")
    messages: list[str] = []
    failed = False
    try:
        if problem is not None:
            raise cli_errors.CLIError(problem)
        if observed is None:
            raise cli_errors.CLIError(f"hook session_id {payload.get('session_id')!r} is not a UUIDv4")
        if source not in _SESSION_START_SOURCES:
            if notice is None:
                raise cli_errors.CLIError(f"unknown SessionStart source {source!r}")
            note(f"SessionStart source {source!r} is not recorded")
        elif not sessions.migration_window_wait(state_path):
            # Wait up to 3 s for migrate/restore-2x,
            # then skip the record write (migrate lists it as live).
            error_stream.write(_MIGRATION_SKIP_NOTE)
        else:
            launch_epoch = args.launch_epoch
            if launch_epoch is not None and launch_epoch < 0:
                raise cli_errors.CLIError("launch epoch must be non-negative")
            messages = _reconcile_session_start(
                runtime_factory(),
                stable_id=stable_id,
                launch_epoch=launch_epoch,
                observed=observed,
                payload=payload,
            )
    except Exception as exc:
        if notice is None:
            raise
        failed = True
        messages.append(
            "claude-multi could not record this session start: "
            f"{hooks.visible(exc)}. Run claude-multi doctor."
        )
        note(f"session start not recorded: {exc}")
    parts = ([notice] if notice is not None else []) + messages
    env = os.environ if environ is None else environ
    mismatch = client_check.check((observed, stable_id), env) if observed is not None else None
    warning = client_check.message(mismatch, stable_id, env) if mismatch is not None else None
    if parts:
        output_stream.write(hooks.notice_response("SessionStart", "\n\n".join(parts), system_message=warning))
        output_stream.flush()
    elif warning is not None:
        output_stream.write(strict_json.canonical_file_bytes({"systemMessage": warning}).decode("utf-8"))
        output_stream.flush()
    if observed is not None and gen is not None and not failed and mismatch is None:
        # A failed start leaves no marker, so the next prompt re-notifies.
        try:
            hooks.write_seen(state_path, observed, gen)
        except (OSError, hooks.HookInputError) as exc:
            note(f"notice marker not written: {exc}")
    return 0


def _handle_session_event(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
) -> int:
    """Consume one official hook event without opening transcript_path."""

    root = runtime.session_store.root
    try:
        sessions.check_state_marker(root)
    except sessions.StateMarkerError as exc:
        # A legacy hook must not block a running session or mutate its state.
        sys.stderr.write(f"claude-multi: {exc}\n")
        return 0
    # Hand-built Namespaces (legacy callers and tests) carry no
    # hook_protocol attribute.
    protocol = getattr(args, "hook_protocol", None)
    if args.event in hooks.SCOPE_ONLY_EVENTS:
        return hooks.dispatch(
            args,
            state_root=root,
            environ=runtime.environ,
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=sys.stderr,
        )
    if args.event == "start" and protocol == lineup_files.HOOK_PROTOCOL:
        return _session_start_v3(
            args,
            state_path=root,
            runtime_factory=lambda: runtime,
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=sys.stderr,
            environ=runtime.environ,
        )

    payload = _read_session_payload(input_stream)
    stable_id, launch_epoch, observed = _session_event_ids(args, payload)
    if not sessions.migration_window_wait(root):
        # The global migration owns every record while it runs:
        # a bounded wait (≤ 3 s), then the record write is skipped.
        sys.stderr.write(_MIGRATION_SKIP_NOTE)
        return 0

    if args.event == "start":
        messages = _reconcile_session_start(
            runtime,
            stable_id=stable_id,
            launch_epoch=launch_epoch,
            observed=observed,
            payload=payload,
        )
        if messages:
            _write_session_start_context(output_stream, messages[0])
        return 0

    event_name = payload.get("hook_event_name")
    if event_name not in (None, "SessionEnd"):
        raise cli_errors.CLIError(f"expected SessionEnd payload, got {event_name!r}")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason:
        raise cli_errors.CLIError("SessionEnd reason is missing")
    runtime.session_store.record_session_end(
        stable_id,
        observed_runtime_id=observed,
        reason=reason,
        launch_epoch=launch_epoch,
    )
    return 0
