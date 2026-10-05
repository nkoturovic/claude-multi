"""``choices.json``: user choices kept apart from every document an older release reads.

``settings.json`` and ``preferences.json`` are shared with older releases,
which read them under closed schemas: a key added there would make an older
release refuse the whole file (its Settings screen could no longer save, its
doctor would report the file). Choices that not every release knows live
here instead, in a closed document of their own (``<config root>/choices.json``,
next to ``settings.json``; the config root follows ``XDG_CONFIG_HOME``). A
release that does not know this file never opens it, so moving between
releases breaks neither document.

The document is closed: version 1 and only the keys of :data:`FIELDS`, each
optional. An absent key reads as its default; writing a key's default value
removes the key. Writes are atomic, 0600, in a 0700 directory, under a lock.
"""

from __future__ import annotations

import errno
import os
import re
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from claude_multi import account_pools, errors, paths, secret_store, sessions, settings, state, strict_json

FILE = "choices.json"
VERSION = 1
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
ENV_KEEP_MAX = 64
# Account sign-in acknowledgements, one per gateway account pool: which text
# was acknowledged (its id and SHA-256), when, and the word typed. An entry
# for a pool this build does not know (one a later release added) is kept as
# it is and never counts here.
ACK_POOLS = account_pools.names()
ACK_WORD = "personal"
ACK_FIELDS = ("text_id", "text_sha256", "acknowledged_at", "typed")
ACK_TEXT_ID_MAX = 64
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_UTC_SECOND = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
# The context window ceiling shared by the lead and every agent: a session's
# window is the smaller of it and the lead set's smallest provider bound.
# Lowering it trades context for cost; it never goes above the operating
# ceiling, which is also its default.
WINDOW_CEILING_KEY = "window_ceiling"
WINDOW_CEILING_DEFAULT = settings.WINDOW_CEILING_DEFAULT
WINDOW_CEILING_MIN = settings.WINDOW_CEILING_MIN
WINDOW_CEILING_MAX = settings.WINDOW_CEILING_MAX
_TOKENS_TEXT = re.compile(r"^(\d{1,7})(?:\s*([kK]))?$")


class ChoicesError(errors.ClaudeMultiError, ValueError):
    """The choices document is unreadable, invalid or a value is refused."""


def _profile_name(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str) or not state.SAFE_NAME.fullmatch(value):
        raise ChoicesError(f"must be a profile name matching {state.SAFE_NAME.pattern} or null")
    return value


def _env_names(value: Any) -> Any:
    if not isinstance(value, list) or len(value) > ENV_KEEP_MAX:
        raise ChoicesError(f"must be a list of at most {ENV_KEEP_MAX} environment variable names")
    if any(not isinstance(item, str) or not _ENV_NAME.fullmatch(item) for item in value):
        raise ChoicesError("must list environment variable names (A-Z, 0-9, _)")
    if len(set(value)) != len(value):
        raise ChoicesError("must not list a name twice")
    return list(value)


def _env_keep_names(value: Any) -> Any:
    """The write check of ``session_env_keep``: the document grammar, then
    the fixed keep policy (``secret_store.env_keep_problem``). A stored name
    the policy refuses later (a provider declared since) still reads: the
    launch checks every name again and strips it."""

    names = _env_names(value)
    problems = [problem for name in names
                if (problem := secret_store.env_keep_problem(name)) is not None]
    if problems:
        raise ChoicesError("refuses " + "; ".join(problems))
    return names


def _acknowledgements(value: Any) -> Any:
    if not isinstance(value, dict):
        raise ChoicesError(f"must be an object keyed by account pool ({', '.join(ACK_POOLS)})")
    malformed = sorted(name for name in value if not account_pools.NAME.fullmatch(name))
    if malformed:
        raise ChoicesError(f"names no account pool: {', '.join(repr(name) for name in malformed)}")
    checked: dict[str, Any] = {}
    for pool in sorted(value):
        record = value[pool]
        if not isinstance(record, dict) or set(record) != set(ACK_FIELDS):
            raise ChoicesError(f"{pool} must hold exactly {', '.join(ACK_FIELDS)}")
        text_id = record["text_id"]
        if not isinstance(text_id, str) or not 0 < len(text_id) <= ACK_TEXT_ID_MAX:
            raise ChoicesError(f"{pool}.text_id must be a text of 1 to {ACK_TEXT_ID_MAX} characters")
        if not isinstance(record["text_sha256"], str) or not _SHA256_HEX.fullmatch(record["text_sha256"]):
            raise ChoicesError(f"{pool}.text_sha256 must be 64 lowercase hex digits")
        if not isinstance(record["acknowledged_at"], str) or not _UTC_SECOND.fullmatch(record["acknowledged_at"]):
            raise ChoicesError(f"{pool}.acknowledged_at must be a UTC time like 2026-01-31T12:00:00Z")
        if record["typed"] != ACK_WORD:
            raise ChoicesError(f"{pool}.typed must be {ACK_WORD!r}")
        checked[pool] = {key: record[key] for key in ACK_FIELDS}
    return checked


def window_ceiling_range() -> str:
    """The allowed range as people read it: ``200K–800K (200000–800000 tokens)``."""

    return (f"{WINDOW_CEILING_MIN // 1000}K–{WINDOW_CEILING_MAX // 1000}K "
            f"({WINDOW_CEILING_MIN}–{WINDOW_CEILING_MAX} tokens)")


def _window_ceiling(value: Any) -> Any:
    if (not isinstance(value, int) or isinstance(value, bool)
            or not WINDOW_CEILING_MIN <= value <= WINDOW_CEILING_MAX):
        raise ChoicesError(f"must be a whole number of tokens from {window_ceiling_range()}")
    return value


def parse_window_ceiling(text: str) -> int:
    """A typed ceiling: a token count (``400000``) or thousands (``400K``);
    refused outside the range, with the range in the message."""

    match = _TOKENS_TEXT.fullmatch(text.strip().replace("_", "").replace(",", ""))
    if match is None:
        raise ChoicesError(
            f"window ceiling {text.strip()!r} is not a token count; give one from {window_ceiling_range()}, "
            "e.g. 400000 or 400K"
        )
    value = int(match.group(1)) * (1000 if match.group(2) else 1)
    if not WINDOW_CEILING_MIN <= value <= WINDOW_CEILING_MAX:
        raise ChoicesError(f"window ceiling {value} is outside the allowed range {window_ceiling_range()}")
    return value


@dataclass(frozen=True)
class Field:
    default: Any
    check: Callable[[Any], Any]
    help: str
    # The stricter check a write applies (None: ``check``); reading keeps
    # ``check`` so a later policy change never makes the document unreadable.
    write: Callable[[Any], Any] | None = None

    def check_write(self, value: Any) -> Any:
        return (self.write or self.check)(value)


FIELDS: dict[str, Field] = {
    "default_profile": Field(None, _profile_name,
                             "the profile a launch uses when nothing else chooses one"),
    "session_env_keep": Field((), _env_names,
                              "API-key variables (names only) a managed session keeps although every "
                              "other *_API_KEY is removed", write=_env_keep_names),
    "acknowledgements": Field(types.MappingProxyType({}), _acknowledgements,
                              "the account sign-in texts acknowledged, per account pool"),
    WINDOW_CEILING_KEY: Field(WINDOW_CEILING_DEFAULT, _window_ceiling,
                              "the context window ceiling of the lead and every agent, in tokens"),
}


def _default(key: str) -> Any:
    default = FIELDS[key].default
    if isinstance(default, tuple):
        return list(default)
    if isinstance(default, types.MappingProxyType):
        return dict(default)
    return default


@dataclass(frozen=True)
class Choices:
    """The validated document; absent keys read as their defaults."""

    values: Mapping[str, Any] = field(default_factory=dict)

    def get(self, key: str) -> Any:
        if key not in FIELDS:
            raise KeyError(key)
        if key in self.values:
            return self.values[key]
        return _default(key)

    def document(self) -> dict[str, Any]:
        return {"version": VERSION, **{key: self.values[key] for key in sorted(self.values)}}


def path(environ: Mapping[str, str] | None = None) -> Path:
    return sessions.config_root(None if environ is None else dict(environ)) / FILE


def _remedy(target: Path, environ: Mapping[str, str] | None) -> str:
    shown = paths.display(target, environ if environ is not None else os.environ)
    return f"fix or remove {shown} (without it every choice has its default)"


def parse(document: Any) -> Choices:
    """Validate the closed document; raises :class:`ChoicesError` naming the rule."""

    if not isinstance(document, dict):
        raise ChoicesError("choices must be a JSON object")
    if document.get("version") != VERSION or isinstance(document.get("version"), bool):
        raise ChoicesError(f"choices version must be {VERSION}")
    unknown = sorted(set(document) - set(FIELDS) - {"version"})
    if unknown:
        raise ChoicesError(f"choices have unknown keys: {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for key, item in document.items():
        if key == "version":
            continue
        try:
            values[key] = FIELDS[key].check(item)
        except ChoicesError as exc:
            raise ChoicesError(f"choice {key} {exc}") from exc
    return Choices(values)


def read(environ: Mapping[str, str] | None = None) -> Choices:
    """The current choices (defaults when the file is absent)."""

    target = path(environ)
    if not os.path.lexists(target):
        return Choices()
    try:
        document = strict_json.loads(state.read_private(target))
    except state.StateError as exc:
        if exc.errno == errno.ENOENT:
            return Choices()
        raise ChoicesError(f"choices unreadable: {exc}", remedy=_remedy(target, environ)) from exc
    except (OSError, ValueError, RecursionError) as exc:
        raise ChoicesError(f"choices are not valid JSON: {exc}", remedy=_remedy(target, environ)) from exc
    try:
        return parse(document)
    except ChoicesError as exc:
        raise ChoicesError(str(exc), remedy=_remedy(target, environ)) from exc


class ChoicesChanged(ChoicesError):
    """The document changed since the caller read it; nothing was written."""


def digest(environ: Mapping[str, str] | None = None) -> str:
    """What a conditional :func:`update` compares: ``absent``, ``unreadable``
    or ``sha256:<hex>`` of the file's bytes."""

    target = path(environ)
    if not os.path.lexists(target):
        return "absent"
    try:
        return "sha256:" + strict_json.sha256_hex(state.read_private(target))
    except OSError:
        return "unreadable"


def written_digest(result: Choices) -> str:
    """What :func:`digest` reads right after a write that produced ``result``
    (the bytes :func:`update` and :func:`replace` write for it)."""

    return "sha256:" + strict_json.sha256_hex(strict_json.pretty_file_bytes(result.document()))


def update(environ: Mapping[str, str] | None = None, *, expected: str | None = None,
           **changes: Any) -> Choices:
    """Set (or, with the default value, remove) choices atomically. With
    ``expected`` (a :func:`digest` read earlier) the update happens only
    when the document is still that one, checked under the same lock as the
    write; otherwise :class:`ChoicesChanged` and nothing is written."""

    unknown = sorted(set(changes) - set(FIELDS))
    if unknown:
        raise ChoicesError(f"unknown choices: {', '.join(unknown)}")
    target = path(environ)
    state.ensure_private_dir(target.parent)
    with state.FileLock(target):
        if expected is not None and digest(environ) != expected:
            raise ChoicesChanged("choices changed since they were read; nothing written")
        current = dict(read(environ).values)
        for key, value in changes.items():
            try:
                checked = FIELDS[key].check_write(value)
            except ChoicesError as exc:
                raise ChoicesError(f"choice {key} {exc}") from exc
            if checked == _default(key):
                current.pop(key, None)
            else:
                current[key] = checked
        result = Choices(current)
        state.atomic_write(target, strict_json.pretty_file_bytes(result.document()))
        return result


def replace(environ: Mapping[str, str] | None, key: str, old: Any, new: Any) -> bool:
    """Set ``key`` to ``new`` only while it is still ``old``, decided under
    the same lock as the write (True: changed; False: nothing written)."""

    if key not in FIELDS:
        raise ChoicesError(f"unknown choices: {key}")
    target = path(environ)
    state.ensure_private_dir(target.parent)
    with state.FileLock(target):
        current = dict(read(environ).values)
        if Choices(current).get(key) != old:
            return False
        try:
            checked = FIELDS[key].check_write(new)
        except ChoicesError as exc:
            raise ChoicesError(f"choice {key} {exc}") from exc
        if checked == _default(key):
            current.pop(key, None)
        else:
            current[key] = checked
        state.atomic_write(target, strict_json.pretty_file_bytes(Choices(current).document()))
        return True


@dataclass(frozen=True)
class WindowCeiling:
    """The window ceiling as configured: its value, whether it was set (an
    absent key reads as the default), and why the document could not be read
    (then the value shown is the default, and launches refuse until fixed)."""

    value: int
    is_set: bool
    problem: str | None = None
    remedy: str | None = None

    @property
    def source(self) -> str:
        return "set" if self.is_set else "default"


def window_ceiling(environ: Mapping[str, str] | None = None) -> WindowCeiling:
    """The configured window ceiling; never raises (see ``problem``)."""

    try:
        current = read(environ)
    except ChoicesError as exc:
        return WindowCeiling(WINDOW_CEILING_DEFAULT, False, str(exc), exc.remedy)
    return WindowCeiling(current.get(WINDOW_CEILING_KEY), WINDOW_CEILING_KEY in current.values)


def set_acknowledgement(environ: Mapping[str, str] | None, pool: str,
                        record: Mapping[str, Any] | None) -> Choices:
    """Store (or, with None, remove) one account pool's acknowledgement,
    keeping the other pools' and every other choice, under the same lock."""

    if pool not in ACK_POOLS:
        raise ChoicesError(f"unknown account pool {pool!r}")
    target = path(environ)
    state.ensure_private_dir(target.parent)
    with state.FileLock(target):
        current = dict(read(environ).values)
        acknowledgements = dict(current.get("acknowledgements", {}))
        if record is None:
            acknowledgements.pop(pool, None)
        else:
            acknowledgements[pool] = dict(record)
        try:
            checked = _acknowledgements(acknowledgements)
        except ChoicesError as exc:
            raise ChoicesError(f"choice acknowledgements {exc}") from exc
        if checked:
            current["acknowledgements"] = checked
        else:
            current.pop("acknowledgements", None)
        result = Choices(current)
        state.atomic_write(target, strict_json.pretty_file_bytes(result.document()))
        return result


# ------------------------------------------------------------------ session_env_keep


def env_keep_problems(names: Iterable[str], *, credential_names: Iterable[str]) -> list[tuple[str, str]]:
    """``(name, reason)`` for every name a managed session cannot keep now
    (``secret_store.env_keep_problem`` against the current providers'
    API-key names). Names only; the same check runs at every launch."""

    credentials = frozenset(credential_names)
    return [(secret_store.shown_env_name(name), problem) for name in names
            if (problem := secret_store.env_keep_problem(name, credential_names=credentials)) is not None]


def set_session_env_keep(environ: Mapping[str, str] | None, names: Iterable[str], *,
                         credential_names: Iterable[str], expected: str | None = None) -> Choices:
    """Save the ``session_env_keep`` names after checking each one against
    the keep policy and the current providers' API-key names (the Settings
    row's save). Refuses the whole list, naming every refused name and why;
    ``expected`` as for :func:`update`."""

    listed = list(names)
    problems = env_keep_problems(listed, credential_names=credential_names)
    if problems:
        raise ChoicesError("choice session_env_keep refuses " + "; ".join(reason for _name, reason in problems),
                           remedy="keep only API-key variables of your own tools (an MCP server's, say)")
    return update(environ, expected=expected, session_env_keep=listed)
