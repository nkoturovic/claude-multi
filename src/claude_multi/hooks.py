"""Protocol-3 hook handlers: the lineup notice, the model-switch fence, spawns.

A compiled lineup-enabled (v2) scope runs every hook through
``<state>/bin/claude-multi-hook-3 session-event <event> --managed-id <id>
--launch-epoch <n> --hook-protocol 3``. ``cli.main`` hands the four
scope-only events (``prompt``, ``premodel``, ``postmodel``, ``subagent``) to
:func:`dispatch` **before** it constructs ``Runtime``: no catalog load, no shim
write, no store directory, no record lock. They read only scope files
(``lineup.gen``, ``lineup.md``, ``lead-set.json``, agent frontmatter), the
notice markers under ``<state>/notice/`` and, for ``postmodel``, the ``model``
key of the user's Claude settings (never written). ``postmodel`` makes one
record write: a user-source switch onto a lead-set row
records ``applied.lead`` on the session's v4 record (lifecycle lock only,
after a bounded migration-window wait); an absent record is never created.

Contract (every event):

- the handler never raises and :func:`dispatch` always returns 0 (exit 2 on
  ``UserPromptSubmit`` would block the user's prompt);
- stdout carries at most one JSON document, written once;
- an internal error is visible: stdout gets
  ``{"systemMessage": "claude-multi <event> failed: <ExceptionClass> — run
  claude-multi doctor"}`` (``premodel`` instead answers an explicit **deny**
  carrying the same system message: blocking a switch is the safe failure;
  the same fixed deny is answered by ``cli.main`` when argparse rejects a
  ``premodel`` argv and by the protocol-3 shim when the launcher is missing
  or exits non-zero, see :func:`premodel_fail_closed_response`),
  stderr gets one sanitised line, and one metadata line (time, event,
  managed id, exception class — never payload content) is appended to the
  bounded ``<state>/hook-errors.log`` (0600, one rotation);
- ``transcript_path`` and ``CLAUDE_CODE_SESSION_ID`` are never read.

The SessionStart notice (``session-event start --hook-protocol 3``) is built
here too (:func:`read_notice`, :func:`notice_text`), but its reconcile needs
the session store, so ``cli`` drives it (the notice is
built from scope files before ``Runtime`` exists and survives any Runtime or
reconcile failure).

The SessionStart and prompt hooks also check which Claude Code runs the
session (``client_check``): a session the Claude daemon moved onto a
different version than the pin gets a user-facing message to exit and resume
through claude-multi, on every prompt until it does.

Layering: stdlib plus ``client_check``, ``lineup_files``,
``lineup_log``, ``sessions`` (``UUID4``, ``check_state_marker``), ``state``,
``strict_json`` and ``platform.posix_fs`` (the flock primitive: no module
outside ``platform/`` imports ``fcntl``); never ``cli``, ``compiler``,
``catalog``, ``profile``, ``scope``, ``launch`` or ``transition``
(``tests/test_hooks.py`` pins the imported module set).
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from . import client_check, errors, lineup_files, lineup_log, sessions, state, strict_json
from .platform import posix_fs
from .lineup_files import GEN_LINE, LEAD_SET_JSON, LINEUP_GEN, LINEUP_MD, normalize_model

SCOPE_ONLY_EVENTS = frozenset({"prompt", "premodel", "postmodel", "subagent"})
USER_SWITCH_SOURCES = frozenset({"command", "picker", "sdk"})
PAYLOAD_LIMITS = strict_json.JSONLimits(
    max_bytes=4 * 1024 * 1024,
    max_depth=64,
    max_collection=4096,
    max_string=4 * 1024 * 1024,
)
LOG_LINE_MAX = lineup_log.LOG_LINE_MAX
NOTICE_DIR = "notice"
SEEN_SUFFIX = ".seen"
SCOPES_DIR = "scopes"
# The global migration lock is ``state.FileLock(<state>/migration)``; the
# hooks only probe it (shared, non-blocking) and never create it.
MIGRATION_LOCK_TARGET = "migration"
# Failures are counted by doctor from this file.
HOOK_ERRORS_LOG = "hook-errors.log"
HOOK_ERRORS_ROTATED = "hook-errors.log.1"
HOOK_ERRORS_MAX_BYTES = 256 * 1024

LEAD_SET_MAX_BYTES = 256 * 1024
AGENT_FILE_MAX_BYTES = 65536
USER_SETTINGS_MAX_BYTES = 1024 * 1024
# lineup.md is at most LINEUP_MD_MAX_BYTES at compile; a little slack so an
# over-limit file is reported as damage, not silently truncated.
_LINEUP_MD_READ_MAX = lineup_files.LINEUP_MD_MAX_BYTES + 4096
_GEN_READ_MAX = 64
_MESSAGE_MAX = 512
_FIELD_MAX = 128
_AGENT_TYPE = re.compile(r"^cm-[a-z0-9-]{1,60}$")


class HookInputError(errors.ClaudeMultiError, ValueError):
    """A hook input (payload, scope file, marker) is missing or malformed."""


# ---------------------------------------------------------------- argv


def is_nonblocking_hook_argv(argv: list[str] | tuple[str, ...]) -> bool:
    """Hook argv whose rejection must never block the client (spec §5.2).

    Protocol-3 argv, or a ``session-event`` whose event is not ``start`` /
    ``end`` (a legacy ``start``/``end`` keeps argparse's exit 2).
    """

    argv = list(argv)
    return argv[:1] == ["session-event"] and (
        "--hook-protocol" in argv or (len(argv) > 1 and argv[1] not in ("start", "end"))
    )


# ---------------------------------------------------------------- reads


def _read_bounded_private(path: Path, limit: int) -> bytes:
    """Read an owner-only regular file without following a symlink.

    ``FileNotFoundError`` when absent; ``HookInputError`` for an unsafe or
    over-limit file. ``O_NONBLOCK`` keeps a planted FIFO from hanging a hook.
    """

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise HookInputError(f"{path.name} is not a readable regular file ({_errno_name(exc)})") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HookInputError(f"{path.name} is not a regular file")
        if info.st_uid != os.geteuid():
            raise HookInputError(f"{path.name} is not owner-controlled")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise HookInputError(f"{path.name} must not be accessible by group/other")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > limit:
        raise HookInputError(f"{path.name} exceeds {limit} bytes")
    return data


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, "error")


def read_payload(input_stream: TextIO) -> dict[str, Any]:
    """The hook payload: a bounded raw read, then a strict parse (``PAYLOAD_LIMITS``)."""

    limit = PAYLOAD_LIMITS.max_bytes
    raw_stream = getattr(input_stream, "buffer", None)
    if raw_stream is not None:
        raw = raw_stream.read(limit + 1)
    else:  # injected text streams (tests) have no .buffer
        raw = input_stream.read(limit + 1).encode("utf-8")
    if len(raw) > limit:
        raise HookInputError(f"hook payload exceeds the {limit}-byte limit")
    try:
        payload = strict_json.loads(raw, PAYLOAD_LIMITS)
    except strict_json.StrictJSONError as exc:
        raise HookInputError(f"invalid hook JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HookInputError("hook payload is not a JSON object")
    return payload


def scope_path(state_root: Path | str, managed_id: str) -> Path:
    """``<state>/scopes/<managed_id>`` (the id must be a canonical UUIDv4)."""

    _check_uuid(managed_id, "managed id")
    return Path(state_root) / SCOPES_DIR / managed_id


def _check_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str) or not sessions.UUID4.fullmatch(value):
        raise HookInputError(f"{label} {value!r} is not a UUIDv4")
    return value


def read_gen(scope_dir: Path | str) -> str | None:
    """The scope's ``lineup.gen`` line (no newline), or None if absent/invalid."""

    try:
        raw = _read_bounded_private(Path(scope_dir) / LINEUP_GEN, _GEN_READ_MAX)
    except (FileNotFoundError, HookInputError):
        return None
    return _gen_from_bytes(raw)


def _gen_from_bytes(raw: bytes) -> str | None:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    line = text[:-1] if text.endswith("\n") else text
    return line if GEN_LINE.fullmatch(line) else None


def seen_path(state_root: Path | str, runtime_id: str) -> Path:
    """``<state>/notice/<runtime_id>.seen`` (the id must be a canonical UUIDv4)."""

    _check_uuid(runtime_id, "runtime session id")
    return Path(state_root) / NOTICE_DIR / f"{runtime_id}{SEEN_SUFFIX}"


def read_seen(state_root: Path | str, runtime_id: str) -> str | None:
    """The generation line this runtime session last saw, or None."""

    try:
        raw = _read_bounded_private(seen_path(state_root, runtime_id), _GEN_READ_MAX)
    except (FileNotFoundError, HookInputError):
        return None
    return _gen_from_bytes(raw)


def write_seen(state_root: Path | str, runtime_id: str, gen_line: str) -> None:
    """Record that ``runtime_id`` saw ``gen_line`` (0600 in a 0700 ``notice/``)."""

    line = gen_line.rstrip("\n")
    if not GEN_LINE.fullmatch(line):
        raise HookInputError(f"not a lineup generation line: {line!r}")
    path = seen_path(state_root, runtime_id)
    state.ensure_private_dir(path.parent)
    state.atomic_write(path, (line + "\n").encode("ascii"))


def clear_seen(state_root: Path | str, runtime_id: str) -> bool:
    """Drop a runtime session's marker so its next prompt re-notifies.

    The current SessionStart clears the marker before anything
    else, so a start hook that fails, is killed at its timeout or never
    writes the notice still leaves the next ``UserPromptSubmit`` a miss.
    Never creates ``notice/``; refuses (``StateError``) an unsafe marker.
    """

    path = seen_path(state_root, runtime_id)
    if not os.path.lexists(path.parent):
        return False
    return state.remove_private(path)


def load_lead_set(scope_dir: Path | str) -> dict[str, Any]:
    """``<scope>/lead-set.json`` after a strict parse and a shape check."""

    path = Path(scope_dir) / LEAD_SET_JSON
    try:
        raw = _read_bounded_private(path, LEAD_SET_MAX_BYTES)
    except FileNotFoundError as exc:
        raise HookInputError(f"{LEAD_SET_JSON} is missing") from exc
    try:
        document = strict_json.loads(raw)
    except strict_json.StrictJSONError as exc:
        raise HookInputError(f"{LEAD_SET_JSON} is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("version") != 1:
        raise HookInputError(f"{LEAD_SET_JSON} has an unsupported shape")
    lead = document.get("lead")
    rows = document.get("rows")
    if (
        not _is_row(lead)
        or not isinstance(document.get("lead_class"), str)
        or not isinstance(rows, list)
        or not rows
        or not all(_is_row(row) for row in rows)
    ):
        raise HookInputError(f"{LEAD_SET_JSON} has an unsupported shape")
    return document


def _is_row(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(key), str) and value.get(key)
        for key in ("display", "family", "selector")
    )


def migration_lock_held(state_root: Path | str) -> bool:
    """True while the global migration lock (``<state>/migration.lock``) is held.

    A shared non-blocking ``flock`` probe on an ``O_RDONLY|O_NOFOLLOW`` fd;
    the lock file is never created. An unsafe or unreadable lock file counts
    as held (never migrate-race a record).
    """

    path = Path(state_root) / f"{MIGRATION_LOCK_TARGET}.lock"
    if not os.path.lexists(path):
        return False
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        posix_fs.lock_descriptor(descriptor, shared=True, blocking=False)
    except BlockingIOError:
        return True
    except OSError:
        return True
    finally:
        os.close(descriptor)
    return False


# ---------------------------------------------------------------- notice

_NOTICE_HEAD = "[claude-multi lineup notice · lineup_generation {generation}]"
_PREFACES = {
    "prompt": "The lineup below is current; it supersedes every earlier lineup notice in this conversation.",
    "startup": "Session started. The lineup below is current.",
    "resume": (
        "Session resumed. Your recorded lead is {lead_display} · {lead_effort} "
        "(`{lead_selector}`); the lineup below is current and supersedes earlier notices."
    ),
    "resume-no-lead": "Session resumed. The lineup below is current and supersedes earlier notices.",
    "fork": (
        "This is a native fork that shares the parent session's scope; the lineup below "
        "applies to it and supersedes earlier notices."
    ),
    "compact": "The conversation was compacted; this notice replaces every earlier lineup notice.",
    "clear": "The conversation was cleared. The lineup below is current.",
    "other": "The lineup below is current; it supersedes every earlier lineup notice.",
}
# No hook observes /reload-plugins, so a notice
# delivered inside a running process calls the generation *staged* — once
# after the preface and once more after the lineup. A startup or resume is a
# fresh process that loaded the agent files at start, so it says nothing.
_STAGED = "Lineup gen {generation} staged — active after /reload-plugins in this session."
_PROCESS_START_KEYS = frozenset({"startup", "resume", "resume-no-lead"})


def notice_key(event: str, source: str | None) -> str:
    """The preface key for a hook event and SessionStart source."""

    if event == "UserPromptSubmit":
        return "prompt"
    if isinstance(source, str) and source in _PREFACES and source not in ("prompt", "resume-no-lead", "other"):
        return source
    return "other"


def notice_text(
    lineup_md: str,
    gen_line: str,
    *,
    event: str,
    source: str | None,
    lead: Mapping[str, Any] | None,
) -> str:
    """The lineup notice; pure.

    ``lead`` is the recorded lead: the SessionStart hook passes None and
    :func:`read_notice` falls back to ``lead-set.json["lead"]``; the launcher
    passes the v4 record's ``applied.lead``.
    """

    generation = gen_line.split(" ", 1)[0].strip()
    key = notice_key(event, source)
    if key == "resume" and not lead:
        key = "resume-no-lead"
    if key == "resume":
        preface = _PREFACES[key].format(
            lead_display=lead.get("display", "?"),
            lead_effort=lead.get("effort") or "?",
            lead_selector=lead.get("selector", "?"),
        )
    else:
        preface = _PREFACES[key]
    parts = [_NOTICE_HEAD.format(generation=generation), preface]
    staged = None if key in _PROCESS_START_KEYS else _STAGED.format(generation=generation)
    if staged is not None:
        parts.append(staged)
    text = "\n".join(parts) + "\n\n" + lineup_md.rstrip("\n")
    if staged is not None:
        text += "\n\n" + staged
    return text


def notice_response(hook_event_name: str, text: str, system_message: str | None = None) -> str:
    """The hook stdout carrying ``text`` as additionalContext (canonical JSON + newline).

    ``system_message`` (shown to the user, not the model) is added only
    when given, so the bytes of a plain notice never change."""

    document: dict[str, Any] = {
        "hookSpecificOutput": {"additionalContext": text, "hookEventName": hook_event_name}
    }
    if system_message is not None:
        document["systemMessage"] = system_message
    return strict_json.canonical_file_bytes(document).decode("utf-8")


def read_notice(
    state_root: Path | str,
    managed_id: str,
    *,
    event: str,
    source: str | None,
    lead: Mapping[str, Any] | None = None,
) -> tuple[str, str | None]:
    """``(notice text, gen line or None)`` from the scope files only.

    ``lineup.md`` and ``lineup.gen`` must agree (``sha256(md)[:12]`` is the
    gen hash); a mismatch re-reads the gen once (a live apply writes md, then
    gen), and if they still disagree the notice is built from the files as
    read with a None gen line, so the caller writes no ``.seen`` and the next
    event retries. A resume without an explicit ``lead`` restates
    ``lead-set.json["lead"]`` (unreadable → the no-lead preface).
    """

    scope_dir = scope_path(state_root, managed_id)
    gen = read_gen(scope_dir)
    if gen is None:
        raise HookInputError("scope has no valid lineup.gen (not a lineup-enabled scope?)")
    try:
        md = _read_bounded_private(scope_dir / LINEUP_MD, _LINEUP_MD_READ_MAX)
    except FileNotFoundError as exc:
        raise HookInputError(f"scope has no {LINEUP_MD}") from exc
    digest = strict_json.sha256_hex(md)[:12]
    consistent: str | None = gen
    if gen.split(" ", 1)[1] != digest:
        again = read_gen(scope_dir)
        if again is not None and again.split(" ", 1)[1] == digest:
            gen = consistent = again
        else:
            consistent = None
    if lead is None and notice_key(event, source) == "resume":
        try:
            lead = load_lead_set(scope_dir)["lead"]
        except (HookInputError, OSError):
            lead = None
    text = notice_text(
        md.decode("utf-8", errors="replace"), gen, event=event, source=source, lead=lead
    )
    return text, consistent


def notice_lead(record: Mapping[str, Any], scope_dir: Path | str) -> dict[str, Any] | None:
    """The v4 record's ``applied.lead`` for the resume notice.

    Decorated from the ``lead-set.json`` row whose normalised selector
    matches (``display``/``family``; no row or an unreadable lead set -> the
    key and ``"?"``). A legacy (v1-3) or gen-0 record, or any malformed field ->
    None (``read_notice`` then restates ``lead-set.json["lead"]``). Reads only
    the scope's ``lead-set.json``; never the catalog.
    """

    try:
        if record.get("version") != sessions.RECORD_VERSION:
            return None
        generation = record.get("lineup_generation")
        if not isinstance(generation, int) or generation < 1:
            return None
        lead = record["applied"]["lead"]
        key, effort, selector = lead["key"], lead["effort"], lead["selector"]
        if not all(isinstance(value, str) and value for value in (key, effort, selector)):
            return None
        try:
            rows = load_lead_set(scope_dir)["rows"]
        except (HookInputError, OSError):
            rows = []
        wanted = normalize_model(selector)
        row = next((r for r in rows if normalize_model(r["selector"]) == wanted), None)
        return {
            "display": row["display"] if row is not None else key,
            "effort": effort,
            "family": row["family"] if row is not None else "?",
            "selector": selector,
        }
    except Exception:
        return None


# ---------------------------------------------------------------- model switch


def classify_switch(
    lead_set: Mapping[str, Any], to_model: Any, from_model: Any
) -> tuple[str, str]:
    """``(decision, reason)`` for a PreModelSwitch.

    Exact match of ``normalize_model(to_model)`` against the lead-set
    selectors (never a wire id, never a prefix); an unmatched or absent
    ``from_model`` is the compiled lead. ``requested_model`` and the source
    are never consulted.
    """

    if not isinstance(to_model, str) or not to_model:
        return (
            "deny",
            "claude-multi cannot classify this model switch (no target model); switch refused.",
        )
    rows = {normalize_model(row["selector"]): row for row in lead_set["rows"]}
    target = rows.get(normalize_model(to_model))
    if target is None:
        return (
            "deny",
            f"{to_model} is not in this session's lead set (lead class "
            f"{lead_set['lead_class']}); /model can switch only within the lead set. "
            "Relaunch with a profile whose lead is that model.",
        )
    current = rows.get(normalize_model(from_model)) if isinstance(from_model, str) else None
    current = current or lead_set["lead"]
    if target["family"] != current["family"]:
        return (
            "ask",
            f"Switching the lead from {current['display']} ({current['family']}) to "
            f"{target['display']} ({target['family']}) re-reads the whole conversation "
            f"uncached; {current['family']} thinking is dropped. In /model press s (this "
            "session only) so the choice is not saved into your global settings.",
        )
    return "allow", f"within the lead set ({target['family']})"


PREMODEL_FAIL_CLOSED_REASON = (
    "claude-multi could not check this model switch; switch refused "
    "(fail closed). Run claude-multi doctor."
)


def premodel_fail_closed_response(system_message: str) -> str:
    """The fixed PreModelSwitch deny for a failure (fail closed).

    One JSON line (newline-terminated). Used by :func:`dispatch` on a handler
    exception, by ``cli.main`` when argparse rejects a ``premodel`` hook argv,
    and (rendered once, at shim-write time) by the protocol-3 shim when the
    launcher is missing or exits non-zero.
    """

    return _premodel_response("deny", PREMODEL_FAIL_CLOSED_REASON, system_message)


def _premodel_response(decision: str, reason: str, system_message: str | None = None) -> str:
    document: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreModelSwitch",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    if system_message is not None:
        document["systemMessage"] = system_message
    return strict_json.canonical_file_bytes(document).decode("utf-8")


def user_settings_path(environ: Mapping[str, str]) -> Path:
    """The user's Claude settings file (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``)."""

    config_dir = environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "settings.json"
    return Path(environ.get("HOME", str(Path.home()))) / ".claude" / "settings.json"


def saved_user_model(environ: Mapping[str, str]) -> tuple[Path, str] | None:
    """``(path, model)`` when the user settings carry a string ``model``; tolerant.

    Only the ``model`` key is read and nothing is written (the user's settings stay theirs). Any
    read/parse problem is "no saved model".
    """

    path = user_settings_path(environ)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        raw = os.read(descriptor, USER_SETTINGS_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(raw) > USER_SETTINGS_MAX_BYTES:
        return None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    model = document.get("model") if isinstance(document, dict) else None
    if not isinstance(model, str) or not model:
        return None
    return path, model


# ---------------------------------------------------------------- spawns


def _agent_model(scope_dir: Path, agent_type: str) -> str | None:
    """The first frontmatter ``model:`` value of a scope ``cm-*`` agent file."""

    if not _AGENT_TYPE.fullmatch(agent_type):
        return None
    path = scope_dir / ".claude" / "agents" / f"{agent_type}.md"
    try:
        raw = _read_bounded_private(path, AGENT_FILE_MAX_BYTES)
    except (FileNotFoundError, HookInputError):
        return None
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            return None
        if stripped.startswith("model:"):
            value = stripped.split(":", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    return None


def _log_field(value: Any) -> str:
    if isinstance(value, str) and 0 < len(value) <= _FIELD_MAX and value.isprintable():
        return value
    return "unknown"


def append_log_line(state_root: Path | str, managed_id: str, record: Mapping[str, Any]) -> Path:
    """Append one line to the session's out-of-scope lineup log."""

    return lineup_log.append(state_root, managed_id, record)


# ---------------------------------------------------------------- dispatch


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def visible(text: object) -> str:
    """Printable-only, bounded text for stderr (never a control byte)."""

    cleaned = "".join(ch if ch.isprintable() else "?" for ch in str(text))
    return cleaned if len(cleaned) <= _MESSAGE_MAX else cleaned[: _MESSAGE_MAX - 1] + "…"


def failure_message(event: str, exc: BaseException) -> str:
    """The failure systemMessage (the exception class only, no content)."""

    return f"claude-multi {event} failed: {type(exc).__name__} — run claude-multi doctor"


def record_hook_error(
    state_root: Path | str,
    *,
    event: str,
    managed_id: str | None,
    exc: BaseException,
    clock: Callable[[], str] | None = None,
) -> Path:
    """Append one metadata line to ``<state>/hook-errors.log``.

    Keys: ``class``, ``event``, ``managed_id`` (null unless a UUIDv4),
    ``time``. Never the payload, the message or a path.
    """

    root = Path(state_root)
    record = {
        "class": type(exc).__name__[:_FIELD_MAX],
        "event": event if event in SCOPE_ONLY_EVENTS | {"start", "end"} else "unknown",
        "managed_id": managed_id
        if isinstance(managed_id, str) and sessions.UUID4.fullmatch(managed_id)
        else None,
        "time": (clock or _utc_now)(),
    }
    state.ensure_private_dir(root)
    return lineup_log.append_bounded(
        root / HOOK_ERRORS_LOG,
        root / HOOK_ERRORS_ROTATED,
        lineup_log.encode_line(record),
        max_bytes=HOOK_ERRORS_MAX_BYTES,
    )


@dataclass
class _Hook:
    event: str
    state_root: Path
    managed_id: str
    scope_dir: Path
    payload: dict[str, Any]
    environ: Mapping[str, str]
    error_stream: TextIO
    clock: Callable[[], str]
    launch_epoch: int | None = None

    def note(self, message: str) -> None:
        self.error_stream.write(f"claude-multi: {self.event} hook: {visible(message)}\n")


# A handler returns (stdout text or None, an after-output action or None).
_Result = tuple[str | None, Callable[[], None] | None]


def _prompt(hook: _Hook) -> _Result:
    runtime_id = hook.payload.get("session_id")
    if not isinstance(runtime_id, str) or not sessions.UUID4.fullmatch(runtime_id):
        hook.note("payload has no UUIDv4 session_id; no lineup notice")
        return None, None
    gen = read_gen(hook.scope_dir)
    if gen is None:
        raise HookInputError("scope has no valid lineup.gen (not a lineup-enabled scope?)")
    if read_seen(hook.state_root, runtime_id) == gen:
        return None, None
    text, gen_line = read_notice(
        hook.state_root, hook.managed_id, event="UserPromptSubmit", source=None
    )
    mismatch = client_check.check((runtime_id, hook.managed_id), hook.environ)
    if mismatch is not None:
        # Off the pin: warn on every prompt (no marker, so the shim's fast
        # path keeps handing the prompt to this check).
        warning = client_check.message(mismatch, hook.managed_id, hook.environ)
        return notice_response("UserPromptSubmit", text, system_message=warning), None
    after = None
    if gen_line is not None:
        def after() -> None:
            write_seen(hook.state_root, runtime_id, gen_line)
    else:
        hook.note("lineup.md and lineup.gen disagree; notice sent, marker not written")
    return notice_response("UserPromptSubmit", text), after


def _premodel(hook: _Hook) -> _Result:
    try:
        lead_set = load_lead_set(hook.scope_dir)
    except HookInputError as exc:
        return (
            _premodel_response(
                "deny",
                f"claude-multi cannot read this session's lead set ({exc}); switch refused. "
                f"Run `claude-multi doctor --repair {hook.managed_id}` or relaunch the session.",
            ),
            None,
        )
    decision, reason = classify_switch(
        lead_set, hook.payload.get("to_model"), hook.payload.get("from_model")
    )
    return _premodel_response(decision, reason), None


PIN_WARNING = (
    "claude-multi: session {m8} now keeps this lead and no longer follows its profile; "
    "/cm follow re-applies the profile (its lead applies at the next resume)"
)
_RECORDS_DIR = "sessions"


def _record_lead_switch(hook: _Hook, row: Mapping[str, Any]) -> str:
    """Record a user-source switch onto lead-set ``row`` on the v4 record.

    ``"none"`` without a UUIDv4 payload runtime id, without a record (never
    created here), or while a migration still holds the global lock after
    the bounded wait (≤ 3 s); otherwise
    ``SessionStore.record_lead_switch`` decides (epoch, authority runtime,
    gen ≥ 1, v4). Its exceptions reach :func:`dispatch` (exit 0).
    """

    runtime_id = hook.payload.get("session_id")
    if not isinstance(runtime_id, str) or not sessions.UUID4.fullmatch(runtime_id):
        hook.note("payload has no UUIDv4 session_id; the lead switch was not recorded")
        return "none"
    if not os.path.lexists(hook.state_root / _RECORDS_DIR / f"{hook.managed_id}.json"):
        return "none"
    if not sessions.migration_window_wait(hook.state_root):
        hook.note("migration or restore in progress; the lead switch was not recorded")
        return "none"
    store = sessions.SessionStore(hook.state_root, sessions.default_schema())
    return store.record_lead_switch(
        hook.managed_id,
        observed_runtime_id=runtime_id,
        launch_epoch=hook.launch_epoch,
        row=row,
    )


def _postmodel(hook: _Hook) -> _Result:
    if hook.payload.get("source") not in USER_SWITCH_SOURCES:
        return None, None
    to_model = hook.payload.get("to_model")
    to_norm = normalize_model(to_model) if isinstance(to_model, str) and to_model else None
    members: set[str] = set()
    target_row: Mapping[str, Any] | None = None
    try:
        lead_set = load_lead_set(hook.scope_dir)
    except HookInputError as exc:
        hook.note(f"cannot read the lead set ({exc}); no lead-set warning")
        lead_set = None
    else:
        members = {normalize_model(row["selector"]) for row in lead_set["rows"]}
        if to_norm is not None:
            target_row = next(
                (row for row in lead_set["rows"] if normalize_model(row["selector"]) == to_norm),
                None,
            )
    warnings: list[str] = []
    if lead_set is not None and to_norm is not None and to_norm not in members:
        warnings.append(
            f"claude-multi: this session switched to {to_model}, which is outside its lead "
            "set; its compaction settings do not fit that model. Switch back with /model or "
            f"relaunch: claude-multi -r {hook.managed_id}"
        )
    saved = saved_user_model(hook.environ)
    if saved is not None:
        path, value = saved
        candidates = set(members)
        if to_norm is not None:
            candidates.add(to_norm)
        if normalize_model(value) in candidates:
            warnings.append(
                f"claude-multi: your /model choice was saved as the default model in {path} "
                f"(plain claude now starts on {value}). Next time press s (this session "
                "only); claude-multi doctor shows the exact revert."
            )
    # Only an in-set target is recorded, after
    # the warnings and before stdout is written.
    if target_row is not None and isinstance(target_row.get("key"), str):
        if _record_lead_switch(hook, target_row) == "pinned":
            warnings.append(PIN_WARNING.format(m8=hook.managed_id[:8]))
    if not warnings:
        return None, None
    return (
        strict_json.canonical_file_bytes(
            {
                "hookSpecificOutput": {
                    "additionalContext": "\n".join(warnings),
                    "hookEventName": "PostModelSwitch",
                }
            }
        ).decode("utf-8"),
        None,
    )


def _subagent(hook: _Hook) -> _Result:
    agent_type = _log_field(hook.payload.get("agent_type"))
    agent_id = _log_field(hook.payload.get("agent_id"))
    selector = _agent_model(hook.scope_dir, agent_type) if agent_type != "unknown" else None
    gen = read_gen(hook.scope_dir)
    record: dict[str, Any] = {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "event": "subagent-start",
        "lineup_gen": gen or "unknown",
        "scope_selector": selector,
        "time": hook.clock(),
    }
    if gen is not None:
        # The scope binding, not proof of the served model.
        record["label"] = lineup_log.binding_label(int(gen.split(" ", 1)[0]))
    append_log_line(hook.state_root, hook.managed_id, record)
    return None, None


_HANDLERS: dict[str, Callable[[_Hook], _Result]] = {
    "prompt": _prompt,
    "premodel": _premodel,
    "postmodel": _postmodel,
    "subagent": _subagent,
}


def dispatch(
    args: Any,
    *,
    state_root: Path | str,
    environ: Mapping[str, str],
    input_stream: TextIO,
    output_stream: TextIO,
    error_stream: TextIO,
    clock: Callable[[], str] | None = None,
) -> int:
    """Run one scope-only hook event; always returns 0."""

    event = getattr(args, "event", "unknown")
    managed_id = getattr(args, "managed_id", None)
    root = Path(state_root)
    written = False
    try:
        _check_uuid(managed_id, "managed id")
        try:
            sessions.check_state_marker(root)
        except sessions.StateMarkerError as exc:
            # A newer state layout: never read stdin, never touch its state.
            # A model switch is the one event that decides something: older
            # code cannot read the lead set the newer release wrote, so it
            # refuses (fail closed) instead of silently allowing.
            error_stream.write(f"claude-multi: {visible(exc)}\n")
            if event == "premodel":
                output_stream.write(premodel_fail_closed_response(
                    f"claude-multi premodel refused: {visible(exc)}"))
                output_stream.flush()
            return 0
        handler = _HANDLERS[event]
        hook = _Hook(
            event=event,
            state_root=root,
            managed_id=managed_id,
            scope_dir=scope_path(root, managed_id),
            payload=read_payload(input_stream),
            environ=environ,
            error_stream=error_stream,
            clock=clock or _utc_now,
            launch_epoch=getattr(args, "launch_epoch", None),
        )
        text, after = handler(hook)
        if text:
            output_stream.write(text)
            output_stream.flush()
        written = True
        if after is not None:
            after()
        return 0
    except (Exception, SystemExit) as exc:  # the hook must never fail the client
        try:
            error_stream.write(f"claude-multi: {event} hook ignored: {visible(exc)}\n")
        except Exception:
            pass
        try:
            record_hook_error(root, event=str(event), managed_id=managed_id, exc=exc, clock=clock)
        except Exception as log_exc:
            try:
                error_stream.write(
                    f"claude-multi: {HOOK_ERRORS_LOG} not written: {visible(log_exc)}\n"
                )
            except Exception:
                pass
        if not written:
            try:
                message = failure_message(str(event), exc)
                if event == "premodel":
                    text = premodel_fail_closed_response(message)
                else:
                    text = strict_json.canonical_file_bytes({"systemMessage": message}).decode(
                        "utf-8"
                    )
                output_stream.write(text)
                output_stream.flush()
            except Exception:
                pass
        return 0


__all__ = [
    "HOOK_ERRORS_LOG",
    "HookInputError",
    "LOG_LINE_MAX",
    "MIGRATION_LOCK_TARGET",
    "NOTICE_DIR",
    "PAYLOAD_LIMITS",
    "PIN_WARNING",
    "PREMODEL_FAIL_CLOSED_REASON",
    "SCOPE_ONLY_EVENTS",
    "USER_SWITCH_SOURCES",
    "append_log_line",
    "classify_switch",
    "clear_seen",
    "dispatch",
    "failure_message",
    "is_nonblocking_hook_argv",
    "load_lead_set",
    "migration_lock_held",
    "notice_lead",
    "notice_response",
    "notice_text",
    "premodel_fail_closed_response",
    "read_gen",
    "read_notice",
    "read_payload",
    "read_seen",
    "record_hook_error",
    "saved_user_model",
    "scope_path",
    "seen_path",
    "user_settings_path",
    "visible",
    "write_seen",
]
