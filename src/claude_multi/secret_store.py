"""The secret store seam and the shared credential policy.

Operator documents and the catalog name credentials only by logical
``env:NAME`` references. This module is the one place that turns a NAME into
a value, and the one owner of the credential-name policy both the launcher
and the gateway share.

- :class:`SecretStore` is the interface: ``get``, ``is_set``, ``set``,
  ``delete`` (deletion stays behind the store boundary, never a caller
  ``unlink``), a separate storage description with an *optional* path, and
  an internal literal-scan accessor (doctor and ``providers validate``
  compare author text against stored values in memory, never printing
  them). Presence is defined without a mandatory pathname, so a later
  keychain backend needs no operator-document change.
- :class:`FileSecretStore` is the only backend: the strict
  assignment parser and the parse-preserving, locked, atomic 0600 writer
  that ``proxy`` has always used (moved here unchanged; ``proxy`` keeps
  compatibility wrappers with the same messages and ``ProxyError`` type).
  There is **no process-environment fallback**, no argv secret value and no
  automatic copy or relocation. Its location is resolved here, for every
  reader alike (the launcher, the gateway render at start, doctor): the
  ``CLAUDE_MULTI_SECRET_ENV`` override, else the file a validated pointer
  (``~/.config/claude-multi/secret-file.json``) selects, else the default
  ``~/.config/claude-multi/secrets/provider-keys.env``.
- The credential-name policy constants live here and are re-exported **by
  reference** (the same objects) from ``proxy`` (gateway deny lists) and
  ``compiler`` (the managed-environment keep-list), so the operator layer
  never needs an ``operator -> proxy -> operator`` import.

Values are never logged, returned in errors, or interpolated into messages;
errors carry paths and line numbers only.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from . import errors, paths, state, strict_json
from .platform import posix_fs


class SecretStoreError(errors.ClaudeMultiError, RuntimeError):
    """A secret store could not be read or written (no secret bytes inside)."""


SECRET_ENV_OVERRIDE = "CLAUDE_MULTI_SECRET_ENV"

# The strict env-file grammar (shared by read and write, never shell-sourced).
ASSIGNMENT = re.compile(r"^(?:export[ \t]+)?([A-Z0-9_]+)[ \t]*=[ \t]*(.*?)[ \t]*$")
VALUE_SHAPE = re.compile(r"^[A-Za-z0-9._~+/=@:-]+$")
_FILE_NAME = re.compile(r"[A-Z0-9_]+")

# Operator-declared credential names (providers.d ``secret_ref``, legacy
# ``secret_env`` at migration): uppercase, 3..64 characters.
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
RESERVED_SECRET_PREFIXES = ("ANTHROPIC_", "CLAUDE_")
RESERVED_SECRET_SUFFIXES = ("_FILE_DESCRIPTOR",)

# The gateway's inherited-environment deny list:
# MANAGEMENT_PASSWORD enables every management route (internal/api/server.go);
# HOME_JWT and the PGSTORE_/GITSTORE_/OBJECTSTORE_ families move the
# config/credential store off the rendered file (cmd/server/main.go, which
# also reads the lowercase spellings); DEPLOY=cloud switches the startup
# mode; WRITABLE_PATH moves the writable base; MANAGEMENT_STATIC_PATH serves
# a control panel; GITHUB_TOKEN and META_MINT_URL are credential/endpoint
# overrides. Matched case-insensitively.
GATEWAY_ENV_DENY_NAMES = frozenset({
    "MANAGEMENT_PASSWORD",
    "HOME_JWT",
    "DEPLOY",
    "WRITABLE_PATH",
    "MANAGEMENT_STATIC_PATH",
    "GITHUB_TOKEN",
    "META_MINT_URL",
})
GATEWAY_ENV_DENY_PREFIXES = ("PGSTORE_", "GITSTORE_", "OBJECTSTORE_")

# Never unset these, whatever a provider secret_ref or the launch env's
# ``*_API_KEY`` scan names. CLAUDE_CODE_MESSAGING_TOKEN is the client's own
# token; CLAUDE_MULTI_SECRET_ENV is the secret-file path the gateway render
# reads, never a secret itself. Both carry a reserved prefix, so no provider
# credential can take one of these names (it would survive into managed
# sessions). Product names only: a user's own tool key (an MCP server's, say)
# is kept through the ``session_env_keep`` choice, never here.
ENV_UNSET_KEEP = frozenset({"CLAUDE_CODE_MESSAGING_TOKEN", SECRET_ENV_OVERRIDE})

# The generic rule of a managed launch: every inherited ``*_API_KEY`` is
# unset, unless the user keeps it by name (``session_env_keep``).
API_KEY_SUFFIX = "_API_KEY"
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
# Text that looks like a key or token value rather than a variable name:
# the shapes of common key formats, or 20+ characters mixing letters and
# digits that are not an UPPER_SNAKE name. Names are never judged by entropy.
_KEY_SHAPES = (
    re.compile(r"^(?:sk|pk|rk)[-_][A-Za-z0-9]"),
    re.compile(r"^gh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"^xox[baprs]-"),
    re.compile(r"^(?:AKIA|ASIA)[0-9A-Z]{12,}$"),
    re.compile(r"^AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"^glpat-"),
    re.compile(r"^eyJ[A-Za-z0-9_-]{10,}"),
)


def gateway_env_denied(name: str) -> bool:
    """Whether an inherited variable is withheld from the gateway."""

    upper = name.upper()
    return upper in GATEWAY_ENV_DENY_NAMES or upper.startswith(GATEWAY_ENV_DENY_PREFIXES)


def looks_like_key(text: str) -> bool:
    """Whether ``text`` looks like an API key or token rather than the name
    of the environment variable that holds one (never echoed by a caller)."""

    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if any(shape.match(stripped) for shape in _KEY_SHAPES):
        return True
    if len(stripped) < 20 or not any(ch.isdigit() for ch in stripped) or not any(ch.isalpha() for ch in stripped):
        return False
    # An UPPER_SNAKE name may carry digits; anything else this long is a value.
    return not ("_" in stripped and stripped.upper() == stripped and _FILE_NAME.fullmatch(stripped))


def _shown(name: str) -> str:
    """The name as a message may show it: only a plain variable name, never
    text that could be a pasted key."""

    if isinstance(name, str) and _FILE_NAME.fullmatch(name) and not looks_like_key(name):
        return name
    return "this name"


NAME_RULE = "A-Z, 0-9 and _, 3 to 64 characters, starting with a letter"
KEY_NOT_NAME = ("that looks like an API key, not the name of its environment variable; "
                "it was not saved and is not shown")
# What a message shows in place of a kept-environment entry that looks like
# a key value (a pasted key, a hand edit): never the entry itself.
KEY_SHAPED_ENTRY = "(an entry that looks like an API key, not shown)"


def shown_env_name(name: Any) -> str:
    """A kept-environment entry as any display may show it: a plain variable
    name as itself, anything that could be a key value as
    :data:`KEY_SHAPED_ENTRY`."""

    if isinstance(name, str) and ENV_NAME.fullmatch(name) and not looks_like_key(name):
        return name
    return KEY_SHAPED_ENTRY


# What a key-name field may draw as typed: a variable name's characters
# (after an optional ``env:``), nothing else.
_NAME_FIELD_TEXT = re.compile(r"(?:env:)?[A-Z0-9_]*")


def name_field_shown(text: str) -> bool:
    """Whether a field for the name of a key's variable may draw ``text`` as
    typed: only a variable name's characters (A-Z, 0-9, _, after an
    optional ``env:``) that do not look like a key. Anything else (a pasted
    key, even its first characters) is never drawn."""

    return (isinstance(text, str) and bool(_NAME_FIELD_TEXT.fullmatch(text))
            and not looks_like_key(text.removeprefix("env:")))


def secret_name_problem(name: str, *, grammar: bool = True) -> str | None:
    """Why ``name`` cannot name a credential the user declares, or None.

    The shared-reference policy: the grammar, the reserved
    ``ANTHROPIC_*``/``CLAUDE_*`` prefixes and ``*_FILE_DESCRIPTOR`` suffix
    and the gateway deny names/prefixes (the actual shared sets). The
    product keep-list (:data:`ENV_UNSET_KEEP`) needs no rule of its own:
    each of its names carries a reserved prefix. ``grammar=False`` applies
    only the reserved rules (legacy ``custom.json`` names). Text that looks
    like a key is refused without being echoed, and only a plain variable
    name is ever repeated in a message.
    """

    if not isinstance(name, str) or looks_like_key(name):
        return KEY_NOT_NAME if isinstance(name, str) else f"the API-key environment-variable name must be text ({NAME_RULE})"
    shown = _shown(name)
    if grammar and not SECRET_NAME.fullmatch(name):
        return f"{shown} is not a valid API-key environment-variable name ({NAME_RULE})"
    if name.startswith(RESERVED_SECRET_PREFIXES):
        return f"secret name {shown} uses a reserved prefix ({', '.join(p + '*' for p in RESERVED_SECRET_PREFIXES)})"
    if name.endswith(RESERVED_SECRET_SUFFIXES):
        return f"secret name {shown} uses the reserved *_FILE_DESCRIPTOR suffix"
    if gateway_env_denied(name):
        return f"secret name {shown} is withheld from the gateway (gateway deny list)"
    return None


def env_keep_problem(name: str, *, credential_names: Iterable[str] = ()) -> str | None:
    """Why a managed session cannot keep the variable ``name`` (the
    ``session_env_keep`` choice), or None. Names only, never a value.

    A kept name only exempts one variable from the generic ``*_API_KEY``
    rule. It can never keep a name claude-multi owns (the reserved
    ``ANTHROPIC_*``/``CLAUDE_*`` prefixes, the ``*_FILE_DESCRIPTOR``
    suffix), a gateway secret, or the credential name of a provider
    (``credential_names``: every catalog and user provider's API-key name,
    checked again at every launch and resume).
    """

    if not isinstance(name, str) or looks_like_key(name):
        return KEY_NOT_NAME
    if not ENV_NAME.fullmatch(name):
        return f"{_shown(name)} is not an environment variable name (A-Z, 0-9 and _)"
    if not name.endswith(API_KEY_SUFFIX):
        return (f"{name} is not removed from managed sessions (only *{API_KEY_SUFFIX} variables are), "
                "so it needs no exception")
    if name.startswith(RESERVED_SECRET_PREFIXES):
        return f"{name} uses a reserved prefix ({', '.join(p + '*' for p in RESERVED_SECRET_PREFIXES)}); claude-multi sets those itself"
    if name.endswith(RESERVED_SECRET_SUFFIXES) or gateway_env_denied(name):
        return f"{name} is a gateway secret; it never enters a managed session"
    if name in set(credential_names):
        return f"{name} is a provider's API-key name; provider keys never enter a managed session"
    return None


# ------------------------------------------------------------ log redaction
# What the public gateway-log surfaces print instead of a secret or an
# account identifier (``claude-multi gateway logs``, the TUI log view, the
# log tail a failed start shows).
REDACTED = "<redacted>"
_HEADER_NAMES = (r"(?:proxy-)?authorization|x-api-key|api-key|x-goog-api-key|x-management-key|"
                 r"management-key|cookie|set-cookie")
_SECRET_FIELDS = (r"api[_-]?keys?|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
                  r"api[_-]?token|session[_-]?token|bearer[_-]?token|token|client[_-]?secret|secret[_-]?key|"
                  r"secret|password|passwd|management[_-]?key|private[_-]?key")
# Fields whose value names an account: the gateway's auth= (a sign-in's
# record name, which carries its e-mail address) and the like.
_ACCOUNT_FIELDS = r"auth|account|account[_-]?id|email|user|user[_-]?id|username|login"
_QUERY_FIELDS = (r"key|api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token|token|code|"
                 r"client[_-]?secret|password|signature|sig|auth")

# Terminal control sequences, removed before anything is matched: OSC, DCS,
# SOS, PM and APC strings (to BEL or ST), CSI sequences and the other escapes,
# in their 7-bit and 8-bit forms.
_TERMINAL_SEQUENCE = re.compile(
    r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f])[^\x07\x1b\x9c]*(?:\x07|\x9c|\x1b\\)?"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?"
    r"|\x1b[ -/]*[0-~]?"
)
# The other control characters and the invisible format characters: removed
# too, so none of them can split a value a rule must see whole.
_INVISIBLE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u00ad\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff]")

# A sensitive name's separator: the closing quote of a quoted (or escaped)
# name, then ``:`` or ``=``. The value after it is redacted whole.
_SEPARATOR = r"(?:\\*[\"'])?\s*[:=]\s*"
_FIELD_RULES: tuple[tuple[re.Pattern[str], bool], ...] = (
    # A header's whole value (a quoted cookie included): ``(pattern, header)``.
    (re.compile(rf"(?i)(?<![A-Za-z0-9_-])(?:{_HEADER_NAMES}){_SEPARATOR}"), True),
    (re.compile(rf"(?i)(?<![A-Za-z0-9_-])(?:{_SECRET_FIELDS}){_SEPARATOR}"), False),
    (re.compile(rf"(?i)(?<![A-Za-z0-9_.-])(?:{_ACCOUNT_FIELDS})="), False),
    # A JSON (or map literal) account field: its name is quoted.
    (re.compile(rf"(?i)(?<=[\"'])(?:{_ACCOUNT_FIELDS})\\*[\"']\s*:\s*"), False),
)
# A bare value ends at whitespace or a delimiter.
_BARE_VALUE = re.compile(r"[^\s\"',;&)\]}]+")
_OPEN_QUOTE = re.compile(r"(\\*)([\"'])")
# The rest of a cookie header after a quoted value: ``; name=value`` pairs.
_COOKIE_REST = re.compile(r"\s*;.*")
# What may close the structure around a header's bare value (kept).
_CLOSERS = " \"'])}"
_PRIVATE_KEY = r"-----(?:BEGIN|END) [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
_KEY_MARK = re.compile(_PRIVATE_KEY)
_KEY_BEGIN = re.compile(_PRIVATE_KEY.replace("(?:BEGIN|END)", "BEGIN"))
_KEY_END = re.compile(_PRIVATE_KEY.replace("(?:BEGIN|END)", "END"))
# A private key within one line (its line breaks escaped, say), or from its
# BEGIN line to the line's end: redacted before any field rule, so a field
# name in front of it (``private_key: -----BEGIN …``) cannot hide its marker.
_KEY_IN_LINE = re.compile(_KEY_BEGIN.pattern + r".*?(?:" + _KEY_END.pattern + r"|$)")
_LOG_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)(\s+)[A-Za-z0-9._~+/=-]{6,}"), rf"\1\2{REDACTED}"),
    (re.compile(rf"(?i)([?&](?:{_QUERY_FIELDS})=)[^&\s\"'#]+"), rf"\1{REDACTED}"),
    # A URL's user name and password.
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]{0,31}://)[^\s/:@\"'<>]+(?::[^\s/@\"'<>]*)?@"), rf"\1{REDACTED}@"),
    # Credential shapes wherever they stand.
    (re.compile(r"(?<![A-Za-z0-9_-])(?:sk|pk|rk)[-_][A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9_-])(?:gsk|csk|xai|vck|hf|pplx|nvapi)[-_](?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*\d)"
                r"[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|"
                r"glpat-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|ya29\.[A-Za-z0-9_-]{16,}|(?:AKIA|ASIA)[0-9A-Z]{16})"),
     REDACTED),
    (re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){0,2}"), REDACTED),
    # The gateway's own keys (64 hex characters) and other long hex keys,
    # with a dotted suffix where a vendor adds one.
    (re.compile(r"(?<![A-Za-z0-9])[0-9A-Fa-f]{32,}(?:\.[A-Za-z0-9]{8,})?(?![A-Za-z0-9])"), REDACTED),
    # E-mail addresses (account identifiers).
    (re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"), REDACTED),
)
# A run of letters and digits this long that mixes upper case, lower case
# and digits is a generated key (prefix-less vendor keys); words never are.
_MIXED_RUN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{24,}(?![A-Za-z0-9])")
# A line that is nothing but a long base64 run: key material (the body of a
# private key whose first line a log tail cut off).
_BASE64_LINE = re.compile(r"\s*[A-Za-z0-9+/]{40,}={0,2}\s*")


def _mixed(run: str) -> bool:
    return any(c.isupper() for c in run) and any(c.islower() for c in run) and any(c.isdigit() for c in run)


def _mixed_key(match: re.Match[str]) -> str:
    run = match.group(0)
    return REDACTED if _mixed(run) else run


def _sanitized(line: str) -> str:
    """``line`` without terminal escape sequences and control characters
    (a tab reads as a space)."""

    return _INVISIBLE.sub("", _TERMINAL_SEQUENCE.sub("", line.replace("\t", " ")))


class _Open:
    """A value that runs past the end of its line, and how it ends on a later
    one: :meth:`end` is the offset just past its end in ``line``, or None
    while it goes on (a bracketed value keeps its depth across lines)."""

    def __init__(self, *, closer: str | None = None, depth: int = 0, quote: str | None = None,
                 key: bool = False) -> None:
        self.closer, self.depth, self.quote, self.key = closer, depth, quote, key

    def end(self, line: str) -> int | None:
        if self.key:
            match = _KEY_END.search(line)
            return match.end() if match else None
        if self.closer is not None:
            match = re.search(r"(?<!\\)" + re.escape(self.closer), line)
            return match.end() if match else None
        end, self.depth, self.quote = _bracketed(line, 0, self.depth, self.quote)
        return end


def _bracketed(text: str, index: int, depth: int, quote: str | None) -> tuple[int | None, int, str | None]:
    """Scan a bracketed value (a list or an object, nested, with quoted
    strings at any escape level) from ``index``: ``(end, depth, quote)``,
    ``end`` None when the line ends inside it."""

    position = index
    while position < len(text):
        char = text[position]
        if quote is not None:
            # The closing quote at the opening one's escape level, never one
            # escaped once more.
            if text.startswith(quote, position) and (position == 0 or text[position - 1] != "\\"):
                position += len(quote)
                quote = None
            else:
                position += 1
            continue
        opened = _OPEN_QUOTE.match(text, position)
        if opened is not None:
            quote = opened.group(1) + opened.group(2)
            position = opened.end()
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth <= 0:
                return position + 1, 0, None
        position += 1
    return None, depth, quote


def _value_end(text: str, index: int, *, header: bool) -> tuple[int, str, _Open | None]:
    """Where the value that starts at ``index`` ends, whole, and what shows
    in its place: a quoted string (at any escape level) or a list or an
    object (nested) keeps its delimiters around :data:`REDACTED`, so
    redacting the result again changes nothing; a header's bare value runs
    to the line's end, any other bare value is one token. A value still
    open at the line's end runs to it and says how it ends later."""

    opened = _OPEN_QUOTE.match(text, index)
    if opened is not None:
        closer = opened.group(1) + opened.group(2)
        match = re.compile(r"(?<!\\)" + re.escape(closer)).search(text, opened.end())
        if match is None:
            return len(text), REDACTED, _Open(closer=closer)
        end, shown = match.end(), closer + REDACTED + closer
    elif index < len(text) and text[index] in "[{":
        found, depth, quote = _bracketed(text, index, 0, None)
        if found is None:
            return len(text), REDACTED, _Open(depth=depth, quote=quote)
        end, shown = found, text[index] + REDACTED + text[found - 1]
    elif header:
        # The rest of the line, but for the closers of the structure around it.
        return index + len(text[index:].rstrip(_CLOSERS)), REDACTED, None
    else:
        bare = _BARE_VALUE.match(text, index)
        return (bare.end() if bare else index), REDACTED, None
    if header:
        rest = _COOKIE_REST.match(text, end)
        if rest is not None and rest.end() > end:
            end, shown = rest.end(), REDACTED
    return end, shown, None


def _redact_fields(text: str, pattern: re.Pattern[str], *, header: bool) -> tuple[str, _Open | None]:
    """``text`` with the whole value after each sensitive name redacted."""

    pieces: list[str] = []
    position, still = 0, None
    for match in pattern.finditer(text):
        if match.start() < position:
            continue  # inside a value already redacted
        end, shown, opened = _value_end(text, match.end(), header=header)
        if end == match.end():
            continue
        pieces += [text[position:match.end()], shown]
        position, still = end, opened or still
    pieces.append(text[position:])
    return "".join(pieces), still


def _redact_line(line: str) -> tuple[str, list[_Open]]:
    """One sanitized line redacted, and the values it leaves open."""

    if _BASE64_LINE.fullmatch(line) and _mixed(line):
        return REDACTED, []
    still: list[_Open] = []
    # Whether a private key stays open past this line is read from the line
    # itself, before any replacement can remove its BEGIN marker.
    marks = list(_KEY_MARK.finditer(line))
    if marks and marks[-1].group().startswith("-----BEGIN"):
        still.append(_Open(key=True))
    text = _KEY_IN_LINE.sub(REDACTED, line)
    for pattern, header in _FIELD_RULES:
        text, opened = _redact_fields(text, pattern, header=header)
        if opened is not None:
            still.append(opened)
    for pattern, replacement in _LOG_RULES:
        text = pattern.sub(replacement, text)
    return _MIXED_RUN.sub(_mixed_key, text), still


def _starts_inside_a_key(lines: list[str]) -> tuple[int, int] | None:
    """``(line, offset)`` just past the end of a private key whose start the
    lines do not show (a tail that begins inside its body), or None."""

    for index, line in enumerate(lines):
        mark = _KEY_MARK.search(line)
        if mark is not None:
            return (index, mark.end()) if mark.group().startswith("-----END") else None
    return None


def redact_lines(lines: Iterable[str]) -> list[str]:
    """Each of ``lines`` (consecutive log lines) sanitized and redacted:
    terminal escape sequences and control characters removed first, then
    every credential-shaped value, header secret (``Authorization``, API-key
    and management-key headers, cookies), secret-named field, URL credential
    and account identifier (``auth=``, e-mail addresses, JSON account
    fields) replaced by :data:`REDACTED`, each value whole (a quoted string,
    a list, an object).

    A value that runs past its line (a private key, a quoted string, a list
    or an object) is redacted on every line up to its end; lines that start
    inside a private key whose first line is not among them are redacted up
    to its end too. The one redactor of the public log surfaces. It works on
    shapes and names only: no stored value is read to find it."""

    clean = [_sanitized(str(line)) for line in lines]
    shown: list[str] = []
    head = _starts_inside_a_key(clean)
    still: list[_Open] = []
    for index, line in enumerate(clean):
        prefix = ""
        if head is not None and index <= head[0]:
            if index < head[0]:
                shown.append(REDACTED)
                continue
            prefix, line = REDACTED, line[head[1]:]
        elif still:
            ends = [(opened, opened.end(line)) for opened in still]
            still = [opened for opened, end in ends if end is None]
            if still:
                shown.append(REDACTED)
                continue
            prefix, line = REDACTED, line[max(end for _opened, end in ends if end is not None):]
        text, still = _redact_line(line)
        shown.append(prefix + text)
    return shown


# Lines a log tail is read with above the lines it shows: they are redacted
# with it and then dropped, so a value that starts above the tail (a private
# key whose BEGIN line the tail cuts off, with no END yet) still redacts it.
TAIL_CONTEXT_LINES = 200


def redact_tail(lines: Iterable[str], count: int) -> list[str]:
    """The last ``count`` of ``lines`` (consecutive log lines), redacted as
    :func:`redact_lines` does with every line before them as context."""

    return redact_lines(lines)[-count:] if count > 0 else []


def redact(text: str) -> str:
    """``text`` (one log line, or several separated by line breaks)
    sanitized and redacted as :func:`redact_lines` does."""

    return "\n".join(redact_lines(str(text).split("\n")))


# The key-file pointer: ``{"version": 1, "path": "~/…" | "/abs"}``.
POINTER_VERSION = 1
POINTER_PATH_MAX = 4096
# claude-multi's own key-file folder (HOME-relative): every managed session
# is denied reading it (the compiled scope's Read denies), so any file in it
# may be selected. A key file the pointer selects anywhere else must sit in a
# private folder of its own, and the compiled scope denies that file by name.
PROTECTED_DIRS = (".config/claude-multi",)
KEY_SOURCES = ("environment", "pointer", "default")
KEY_FOLDER_RULE = ("the key file must be inside ~/.config/claude-multi/, or in a private folder of its own "
                   "that you own (no group or other access: chmod 700), not your home folder itself")
KEY_FILE_RULE = "the key file must be a private regular file you own (no group or other access: chmod 600)"


@dataclass(frozen=True)
class KeyFileLocation:
    """The key file every reader uses, and what selected it."""

    path: Path
    source: str  # environment | pointer | default


def _pointer_failure(shown: str, detail: str) -> SecretStoreError:
    return SecretStoreError(f"key file pointer {shown} is unreadable: {detail}",
                            remedy=f"fix or delete {shown}")


def _private(info: os.stat_result, kind: int) -> bool:
    return stat.S_IFMT(info.st_mode) == kind and posix_fs.owned_by_caller(info) and not posix_fs.shared_mode_bits(info)


def location_problem(path: Path | str, environ: Mapping[str, str]) -> str | None:
    """Why ``path`` cannot be the key file a pointer selects, or None.

    It must be absolute (``~/`` expands to HOME). Resolved (symlinks
    followed), it lies inside claude-multi's own folder
    (:data:`PROTECTED_DIRS`), or in a folder the person chose: a private
    folder they own (no group or other access) other than HOME and its
    parents, the file itself (once it exists) a private regular file they
    own. The compiled scope denies such a file by name
    (:func:`unprotected_key_files`)."""

    text = str(path)
    home = paths.home(environ)
    if text.startswith("~/"):
        candidate = home / text[2:]
    else:
        candidate = Path(text)
    if not candidate.is_absolute():
        return "the path must be absolute or start with ~/"
    real = Path(os.path.realpath(candidate))
    real_home = Path(os.path.realpath(home))
    for relative in PROTECTED_DIRS:
        base = real_home / relative
        if real != base and base in real.parents:
            return None
    folder = real.parent
    if folder == real_home or folder in real_home.parents:
        return KEY_FOLDER_RULE
    try:
        if not _private(os.lstat(folder), stat.S_IFDIR):
            return KEY_FOLDER_RULE
        if os.path.lexists(real) and not _private(os.lstat(real), stat.S_IFREG):
            return KEY_FILE_RULE
    except OSError:
        return KEY_FOLDER_RULE
    return None


def expand(path: str, environ: Mapping[str, str]) -> Path:
    return paths.home(environ) / path[2:] if path.startswith("~/") else Path(path)


def pointer_document(path: Path | str, environ: Mapping[str, str]) -> dict[str, Any]:
    """The pointer for ``path`` (HOME-relative paths are written as ``~/…``)."""

    absolute = Path(path)
    try:
        relative = absolute.relative_to(paths.home(environ))
        text = f"~/{relative.as_posix()}"
    except ValueError:
        text = str(absolute)
    return {"version": POINTER_VERSION, "path": text}


def parse_pointer(document: Any, environ: Mapping[str, str]) -> Path:
    """The key file a pointer document selects; raises ``ValueError``
    naming the rule it breaks."""

    if not isinstance(document, dict) or set(document) != {"version", "path"}:
        raise ValueError('it must be {"version": 1, "path": "~/…"} and nothing else')
    if document["version"] != POINTER_VERSION or isinstance(document["version"], bool):
        raise ValueError(f"version must be {POINTER_VERSION}")
    path = document["path"]
    if not isinstance(path, str) or not path or len(path) > POINTER_PATH_MAX or "\0" in path:
        raise ValueError("path must be a non-empty text")
    problem = location_problem(path, environ)
    if problem is not None:
        raise ValueError(problem)
    return expand(path, environ)


def read_pointer(environ: Mapping[str, str]) -> Path | None:
    """The key file the pointer selects, or None when there is no pointer.

    An unreadable or invalid pointer raises: a pointer that was written means
    the person chose a file, so falling back to another one would hide it."""

    target = paths.secret_pointer_path(environ)
    if not os.path.lexists(target):
        return None
    shown = paths.display(target, environ)
    try:
        raw = state.read_private(target)
    except state.StateError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise _pointer_failure(shown, "it must be a private file you own (0600)") from exc
    except OSError as exc:
        raise _pointer_failure(shown, exc.strerror or type(exc).__name__) from exc
    try:
        document = strict_json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise _pointer_failure(shown, "it is not valid JSON") from exc
    try:
        return parse_pointer(document, environ)
    except ValueError as exc:
        raise _pointer_failure(shown, str(exc)) from exc


def key_file_location(environ: Mapping[str, str] | None = None) -> KeyFileLocation:
    """The one resolution every reader shares: the override, the pointer,
    then the default (raises :class:`SecretStoreError` for a bad pointer)."""

    env = os.environ if environ is None else environ
    override = env.get(SECRET_ENV_OVERRIDE)
    if override:
        return KeyFileLocation(Path(override), "environment")
    pointed = read_pointer(env)
    if pointed is not None:
        return KeyFileLocation(pointed, "pointer")
    return KeyFileLocation(paths.secret_env_default(env), "default")


def _inside_protected(path: Path, homes: tuple[Path, ...]) -> bool:
    return any((home / relative) in path.parents for home in homes for relative in PROTECTED_DIRS)


def _pointer_named(environ: Mapping[str, str]) -> Path | None:
    """The absolute path the key-file pointer names, read leniently (a
    pointer that is not valid still names the file a person chose), or
    None without a pointer that names one. Only the pointer is opened."""

    try:
        document = strict_json.loads(state.read_private(paths.secret_pointer_path(environ)))
    except (OSError, ValueError, RecursionError):
        return None
    named = document.get("path") if isinstance(document, dict) else None
    if not isinstance(named, str) or not named or len(named) > POINTER_PATH_MAX or "\0" in named:
        return None
    candidate = expand(named, environ)
    return candidate if candidate.is_absolute() else None


def _outside_protected(path: str | Path, environ: Mapping[str, str]) -> tuple[Path, ...]:
    """``path`` as given and as it resolves, those of the two the protected
    folder does not cover."""

    home = paths.home(environ)
    homes = (Path(os.path.normpath(os.path.abspath(home))), Path(os.path.realpath(home)))
    found: list[Path] = []
    for candidate in (Path(os.path.normpath(os.path.abspath(path))), Path(os.path.realpath(path))):
        if not _inside_protected(candidate, homes) and candidate not in found:
            found.append(candidate)
    return tuple(found)


def pointer_key_files(environ: Mapping[str, str]) -> tuple[Path, ...]:
    """The key file the pointer names, as written and as it resolves, when
    it lies outside claude-multi's own folder (empty otherwise)."""

    named = _pointer_named(environ)
    return _outside_protected(named, environ) if named is not None else ()


def unprotected_key_files(environ: Mapping[str, str]) -> tuple[Path, ...]:
    """Key files a managed session must also be denied: the ones the
    protected folder (:data:`PROTECTED_DIRS`) does not cover.

    The default file always lies inside it; the environment override can be
    anywhere (another folder, a Windows drive under WSL, a link), and so can
    the file the pointer names (:func:`pointer_key_files`, a private folder
    the person chose). Both the path as given and the file it resolves to
    are listed. Nothing is opened but the pointer; no key file is read."""

    found: list[Path] = []
    override = environ.get(SECRET_ENV_OVERRIDE)
    if override:
        found.extend(_outside_protected(override, environ))
    found.extend(path for path in pointer_key_files(environ) if path not in found)
    return tuple(found)


# Where the gateway keeps the account sign-ins (HOME-relative, as the
# catalog's gateway ``auth_dir``).
ACCOUNT_RECORDS = ".local/share/claude-multi/auth"


def credential_locations(environ: Mapping[str, str]) -> tuple[Path, ...]:
    """The one inventory of where credentials live: the protected folder
    under HOME (:data:`PROTECTED_DIRS`: the default key file, the gateway's
    keys and configuration), the gateway's account sign-ins
    (:data:`ACCOUNT_RECORDS`) and every key file the environment or the
    pointer selects outside that folder (:func:`unprotected_key_files`, as
    given and resolved).

    The compiled session denies cover each location, an export never writes
    into one, and uninstall deletes credentials found there only after the
    typed phrase (:func:`key_file_inventory` names the key files exactly).
    Nothing is opened but the pointer."""

    home = paths.home(environ)
    found = [home / relative for relative in PROTECTED_DIRS]
    found.append(home / ACCOUNT_RECORDS)
    found.extend(unprotected_key_files(environ))
    return tuple(dict.fromkeys(found))


def secret_env_path(environ: Mapping[str, str] | None = None) -> Path:
    """The file backend's location (:func:`key_file_location`)."""

    return key_file_location(environ).path


def service_key_file(environ: Mapping[str, str]) -> KeyFileLocation:
    """The key file the supervised gateway reads: its unit drops the
    override, so only the pointer or the default select it."""

    return key_file_location({name: value for name, value in environ.items() if name != SECRET_ENV_OVERRIDE})


def key_file_inventory(environ: Mapping[str, str]) -> tuple[Path, ...]:
    """Every file that holds, or is selected to hold, the provider keys:
    the environment override (as given), the file the supervised gateway
    reads (the pointer's, or the default) and the default.

    Raises :class:`SecretStoreError` when the pointer cannot be read: the
    file it selects is then unknown, and a caller deciding what may be
    deleted must not guess. Nothing is opened but the pointer."""

    found: list[Path] = []
    override = environ.get(SECRET_ENV_OVERRIDE)
    if override:
        found.append(Path(override))
    found.append(service_key_file(environ).path)
    found.append(paths.secret_env_default(environ))
    return tuple(dict.fromkeys(found))


def write_pointer(environ: Mapping[str, str], path: Path | str) -> Path:
    """Select ``path`` as the key file (atomic, 0600); returns the pointer path."""

    document = pointer_document(path, environ)
    parse_pointer(document, environ)
    target = paths.secret_pointer_path(environ)
    state.ensure_private_dir(target.parent)
    state.atomic_write(target, strict_json.pretty_file_bytes(document))
    return target


def check_key_file(path: Path | str, environ: Mapping[str, str]) -> dict[str, str]:
    """An existing key file a pointer may select: where
    :func:`location_problem` allows it, a private regular file you own and
    strictly ``NAME=value``.
    Returns its assignments (callers use the names only)."""

    candidate = Path(path)
    problem = location_problem(candidate, environ)
    if problem is not None:
        raise SecretStoreError(f"{paths.display(candidate, environ)}: {problem}")
    if not os.path.lexists(candidate):
        raise SecretStoreError(f"{paths.display(candidate, environ)} does not exist")
    return read_env_file(candidate)


def parse_env_bytes(raw: bytes, path: Path) -> dict[str, str]:
    """Strict assignment parsing of env-file BYTES (shared read/write rule).

    Never shell-sourced; error messages carry line numbers, never secret
    bytes.
    """

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # One failure class for consumers; the codec message never carries
        # secret bytes.
        raise SecretStoreError(f"secret env file {path} is not valid UTF-8") from exc
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ASSIGNMENT.match(line)
        if not match:
            raise SecretStoreError(f"{path}:{number}: malformed assignment line")
        name, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not value or not VALUE_SHAPE.fullmatch(value):
            raise SecretStoreError(f"{path}:{number}: unsupported characters in value")
        if name in values:
            raise SecretStoreError(f"{path}:{number}: duplicate assignment for {name}")
        values[name] = value
    return values


def read_env_file(path: Path) -> dict[str, str]:
    """Strict parse of the private secret env file (regular, owned, 0600)."""

    try:
        raw = state.read_private(path)
    except state.StateError as exc:
        raise SecretStoreError(f"secret env file {path} unavailable or unsafe: {exc}") from exc
    return parse_env_bytes(raw, path)


def _rewrite(path: Path, name: str, value: str | None) -> bool:
    """Locked, parse-preserving rewrite: set (value) or delete (None) NAME.

    Every existing assignment of the name collapses into the first one
    (keeping its ``export`` prefix and spacing) or is dropped; unrelated
    lines are untouched; the candidate passes the strict parser before the
    atomic 0600 write. Returns whether the file changed.
    """

    state.ensure_private_dir(path.parent)
    lock = state.FileLock(path)
    lock.acquire(blocking=True)
    try:
        out_lines: list[str] = []
        if os.path.lexists(path):
            try:
                raw = state.read_private(path)
            except state.StateError as exc:
                raise SecretStoreError(f"secret env file unavailable or unsafe: {exc}") from exc
            try:
                out_lines = raw.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise SecretStoreError(f"secret env file {path} is not valid UTF-8") from exc
        elif value is None:
            return False
        replaced = False
        drop: set[int] = set()
        for index, line in enumerate(out_lines):
            match = ASSIGNMENT.match(line)
            if match and match.group(1) == name:
                if replaced or value is None:
                    # The strict parser rejects duplicates, so a second line
                    # would leave the file broken after "saved".
                    drop.add(index)
                    continue
                prefix = line[: match.start(1)]  # export/tab/spacing exactly
                out_lines[index] = f"{prefix}{name}={value}"
                replaced = True
        out_lines = [line for index, line in enumerate(out_lines) if index not in drop]
        if value is None:
            if not drop:
                return False
        elif not replaced:
            out_lines.append(f"{name}={value}")
        candidate = ("\n".join(out_lines) + "\n").encode("utf-8") if out_lines else b""
        # A successful write must yield a consumable file (catches
        # pre-existing malformed or duplicate lines of OTHER keys).
        parse_env_bytes(candidate, path)
        # State errors (unsafe directory, CommittedStateError) propagate
        # unchanged, exactly as the earlier proxy writer let them.
        state.atomic_write(path, candidate)
        return True
    finally:
        lock.release()


def write_env_value(path: Path, name: str, value: str) -> int:
    """Insert or replace ``NAME=value`` (every assignment of the name collapses into
    the first one); returns the length only."""

    if not _FILE_NAME.fullmatch(name):
        raise SecretStoreError("invalid secret variable name (A-Z, 0-9 and _ only; the text given is not shown)")
    if not value or not VALUE_SHAPE.fullmatch(value):
        raise SecretStoreError(
            "secret value has an unsupported shape "
            "(letters, digits, and . _ ~ + / = @ : - only)"
        )
    _rewrite(path, name, value)
    return len(value)


def delete_env_value(path: Path, name: str) -> bool:
    """Remove every assignment of ``NAME``; False when it was not set."""

    if not _FILE_NAME.fullmatch(name):
        raise SecretStoreError("invalid secret variable name (A-Z, 0-9 and _ only; the text given is not shown)")
    return _rewrite(path, name, None)


@runtime_checkable
class SecretStore(Protocol):
    """Logical ``env:NAME`` credentials, independent of where they live."""

    def get(self, name: str) -> str | None: ...

    def is_set(self, name: str) -> bool: ...

    def set(self, name: str, value: str) -> int: ...

    def delete(self, name: str) -> bool: ...

    def description(self) -> str: ...

    @property
    def path(self) -> Path | None: ...

    def scan_values(self) -> frozenset[str]: ...


class FileSecretStore:
    """The strict private env file (the one backend).

    An absent file is an empty store (every name unset); an unsafe or
    malformed file raises :class:`SecretStoreError` rather than guessing.
    """

    def __init__(self, path: Path | str, *, environ: Mapping[str, str] | None = None) -> None:
        self._path = Path(path)
        self._environ = dict(os.environ if environ is None else environ)

    @property
    def path(self) -> Path | None:
        return self._path

    def _values(self) -> dict[str, str]:
        # ``exists`` (not ``lexists``): the earlier resolver's rule, so a
        # dangling link still reads as an empty store.
        if not self._path.exists():
            return {}
        return read_env_file(self._path)

    def get(self, name: str) -> str | None:
        return self._values().get(name)

    def is_set(self, name: str) -> bool:
        return name in self._values()

    def set(self, name: str, value: str) -> int:
        return write_env_value(self._path, name, value)

    def delete(self, name: str) -> bool:
        return delete_env_value(self._path, name)

    def description(self) -> str:
        return f"private env file {paths.display(self._path, self._environ)}"

    def scan_values(self) -> frozenset[str]:
        """Internal: every stored value, for in-memory literal scans only."""

        return frozenset(self._values().values())


def default_store(environ: Mapping[str, str] | None = None) -> FileSecretStore:
    """The machine's store: the file backend at :func:`secret_env_path`."""

    env = os.environ if environ is None else environ
    return FileSecretStore(secret_env_path(env), environ=env)
