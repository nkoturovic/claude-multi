#!/usr/bin/env python3
"""Scan a repository's whole history for credentials and personal identifiers.

Every blob reachable from any ref (``git rev-list --all``: branches, tags,
notes, stash) is read once, plus every reachable commit and annotated-tag
object (message and identity headers). The scan reports names, counts and locations (blob, the first
commit and path that introduced it, line number) only. A matched value is
never printed or written: a credential hit carries its shape name, its
length, its character entropy and a verdict, nothing else.

Credential shapes: PEM private keys (armor headers and JSON ``\\n`` escapes
included), JWTs, GitHub, Slack and AWS tokens, Google API keys, ``sk-`` keys,
other vendor key prefixes, bearer values, passwords in URLs, and values
assigned to credential-named keys (env files, the gateway config, auth JSON).
Each hit is classified as a synthetic sentinel (a placeholder marker such as
``dummy``/``fake``/``example`` in or right around the value, fill
characters, a repeating pattern, a value made of words, or an entropy well
below a random value of its own alphabet and length, judged on the value's
letters and digits) or as a candidate. Candidates need a human look; the
exit status is 1 while any remain.

Personal identifiers: :data:`GENERIC_IDENTIFIERS` (the home directory of a
real account), plus the exact list of a private input when one is given
(``--identifiers FILE``, or the file :data:`PRIVATE_INPUT_ENV` names; the
format is described there). The repository's identifier gate
(``tests/test_hygiene.py``) uses the same table. Informational classes:
``.lan`` host names and e-mail domains are listed by name with their
counts; private IPv4 addresses are counted only, never listed.

Usage::

    python3 tools/history_scan.py [--repo PATH] [--range BASE..HEAD] [--identifiers FILE] [--out DIR]
                                  [--max-locations N]

``--range BASE..HEAD`` scans what a pull request or a push adds: the commits
reachable from HEAD but not from BASE, and the blobs and annotated tags they
introduce (``git rev-list --objects BASE..HEAD``); refs are not listed. Both
ends must name commits present in the repository (a shallow clone without
the base is an error, never an empty scan). Without ``--range`` everything
reachable from every ref is scanned.

``--out`` writes ``summary.json``, ``credentials.json`` and
``identifiers.json`` (directory 0700, files 0600). Exit 0: no candidate
credential; 1: candidates found; 2: usage or git error (a missing revision
included). Stdlib only.
"""


from __future__ import annotations

import argparse
import collections
import functools
import json
import math
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

LIMITS = """\
Limits: the scan is a heuristic. A clean result means no candidate was
found, not that no secret is present. Known blind spots:
  - a real value written within a few characters of a placeholder word
    (dummy, example, fixture, ...) on the same line is reported synthetic;
  - compressed, archived and binary content is not decoded; a blob with
    a recognised archive or compression signature or a NUL byte in its
    first 8 KiB is counted as uninspected;
  - encoded text (base64, hex dumps) is not decoded and not counted: it
    is scanned as the text it is, so a credential inside it is missed;
  - a passphrase made only of words looks like an identifier and is
    reported synthetic;
  - only the shapes the module documentation lists are recognised.
"""

# --------------------------------------------------------------- identifiers

# The identities test data and examples use (``/home/user``, ``/Users/me``,
# a CI journey account): an absolute home directory under any other name is
# a personal identifier.
NEUTRAL_USERS = (
    "a", "example-user", "fixture", "journey", "me", "onboarding", "sentinel", "service-journey",
    "someone", "test", "u", "user",
)
_NEUTRAL = "|".join(re.escape(name) for name in sorted(NEUTRAL_USERS, key=len, reverse=True))
# Personal identifiers no public tree carries, defined without naming
# anyone: the home directory of a real account (Linux, macOS). A path that
# continues a word (``right/home/end``, ``/fixture/home/a``) is not one.
GENERIC_IDENTIFIERS: tuple[tuple[str, str], ...] = (
    ("home-path", rf"(?<![\w.~-])/home/(?!(?:{_NEUTRAL})(?![\w.-]))[A-Za-z_][\w.-]*"),
    ("macos-home-path", rf"/Users/(?!(?:{_NEUTRAL})(?![\w.-]))[A-Za-z_][\w.-]*"),
)

# The exact identifiers of the people and computers behind a repository (a
# user name, host and network names, a private repository's name) are not
# published with it: they are a private input, a file named by
# ``--identifiers`` or by this environment variable. One entry per line,
# fields separated by one TAB; blank lines and lines starting with ``#`` are
# skipped:
#
#   <label> TAB <regex>                                   an identifier
#   origin TAB <label> TAB <regex>                        an origin-narration pattern
#   identifier-site TAB <path> TAB <label> TAB <count> TAB <owner> TAB <why>
#   origin-site TAB <path> TAB <count> TAB <owner> TAB <why>
#
# The site lines are the reviewed occurrences the repository's hygiene gate
# (``tests/test_hygiene.py``) accepts at their exact counts. An identifier
# may reuse a generic label (``home-path``): its pattern adds to that label,
# and a place both patterns match counts once. Labels are printed; patterns
# and matched values never are.
PRIVATE_INPUT_ENV = "CLAUDE_MULTI_PRIVATE_IDENTIFIERS"
_LABEL = re.compile(r"[a-z][a-z0-9-]*\Z")
_SITE_KINDS = {"origin": 3, "identifier-site": 6, "origin-site": 5}


class PrivateInputError(ValueError):
    """A private input that cannot be used (the message names a line, never its content)."""


@dataclass(frozen=True)
class PrivateInput:
    identifiers: tuple[tuple[str, str], ...] = ()
    origin: tuple[tuple[str, str], ...] = ()
    # (path, label) -> (count, owner, why)
    identifier_sites: Mapping[tuple[str, str], tuple[int, str, str]] = field(default_factory=dict)
    # path -> (count, owner, why)
    origin_sites: Mapping[str, tuple[int, str, str]] = field(default_factory=dict)


def _count_field(value: str, number: int) -> int:
    if not value.isdigit() or int(value) < 1:
        raise PrivateInputError(f"line {number}: the count is not a positive number")
    return int(value)


def parse_private_input(text: str) -> PrivateInput:
    """The private input's entries (see :data:`PRIVATE_INPUT_ENV`)."""

    identifiers: list[tuple[str, str]] = []
    origin: list[tuple[str, str]] = []
    identifier_sites: dict[tuple[str, str], tuple[int, str, str]] = {}
    origin_sites: dict[str, tuple[int, str, str]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        kind = fields[0]
        if kind in _SITE_KINDS:
            if len(fields) != _SITE_KINDS[kind] or not all(fields):
                raise PrivateInputError(f"line {number}: a {kind} entry has {_SITE_KINDS[kind]} fields")
            if kind == "origin":
                origin.append((fields[1], fields[2]))
                entry = origin[-1]
            elif kind == "identifier-site":
                key = (fields[1], fields[2])
                if key in identifier_sites:
                    raise PrivateInputError(f"line {number}: a repeated site")
                identifier_sites[key] = (_count_field(fields[3], number), fields[4], fields[5])
                continue
            else:
                if fields[1] in origin_sites:
                    raise PrivateInputError(f"line {number}: a repeated site")
                origin_sites[fields[1]] = (_count_field(fields[2], number), fields[3], fields[4])
                continue
        elif len(fields) == 2 and all(fields):
            identifiers.append((fields[0], fields[1]))
            entry = identifiers[-1]
        else:
            raise PrivateInputError(f"line {number}: not an entry (label TAB regex, or a typed entry)")
        label, pattern = entry
        if not _LABEL.match(label) or label in _SITE_KINDS:
            raise PrivateInputError(f"line {number}: a label is lower-case words joined by dashes")
        try:
            re.compile(pattern.encode())
        except re.error:
            raise PrivateInputError(f"line {number}: the pattern does not compile") from None
    for table in (identifiers, origin):
        labels = [label for label, _pattern in table]
        if len(labels) != len(set(labels)):
            raise PrivateInputError("a label is defined twice")
    return PrivateInput(tuple(identifiers), tuple(origin), identifier_sites, origin_sites)


def load_private_input(path: Path) -> PrivateInput:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PrivateInputError(f"cannot read the private input: {type(exc).__name__}") from None
    return parse_private_input(text)


def private_input_from_env(environ: Mapping[str, str] | None = None) -> PrivateInput | None:
    """The private input the environment names, or ``None`` when it names none."""

    value = (os.environ if environ is None else environ).get(PRIVATE_INPUT_ENV, "")
    return load_private_input(Path(value)) if value else None


@functools.lru_cache(maxsize=16)
def _compiled(table: tuple[tuple[str, str], ...]) -> tuple[re.Pattern, tuple[str, ...]]:
    # One combined pattern (a group per entry), so each occurrence counts
    # once; private entries come first, so an exact identifier claims an
    # occurrence before a generic shape does.
    pattern = re.compile("|".join(f"(?P<g{index}>{entry})" for index, (_name, entry) in enumerate(table)).encode())
    return pattern, tuple(name for name, _entry in table)


def identifier_table(private: PrivateInput | None = None) -> tuple[tuple[str, str], ...]:
    """The identifiers to look for: the private input's, then the generic ones."""

    return (*(private.identifiers if private is not None else ()), *GENERIC_IDENTIFIERS)

# Informational classes (history scan only; the tree gate uses the identifier table).
_LAN_HOST_RE = re.compile(rb"\b([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*)\.lan\b", re.IGNORECASE)
_EMAIL_RE = re.compile(rb"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
_PRIVATE_IPV4_RE = re.compile(
    rb"(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?![\d.])"
)


def identifier_counts(data: bytes, private: PrivateInput | None = None) -> collections.Counter:
    """Occurrences of each identifier (:func:`identifier_table`) in ``data``, by label."""

    pattern, names = _compiled(identifier_table(private))
    counts: collections.Counter = collections.Counter()
    for match in pattern.finditer(data):
        counts[names[int(match.lastgroup[1:])]] += 1
    return counts


# --------------------------------------------------------------- credentials

@dataclass(frozen=True)
class Shape:
    name: str
    pattern: re.Pattern
    group: int = 0  # the value group the verdict looks at (a fixed vendor prefix stays outside it)
    generic: bool = False  # reported only where no specific shape already matched the value


_B64URL = rb"[A-Za-z0-9_-]"
# A PEM body may sit in a JSON or YAML string with literal "\n" escapes (a
# service-account key file) and may follow RFC 1421/4880 armor headers
# (Proc-Type, DEK-Info, Version, Comment); both are skipped to reach the key
# body, which is read for at most 256 raw characters.
_PEM_BREAK = rb"(?:\s|\\[nr])"
_PEM_HEADER = rb"[A-Za-z][A-Za-z0-9-]*:[^\r\n\\]*(?:\r?\n|(?:\\r)?\\n)"
# Key names whose value is a credential (env files, the gateway config, auth
# JSON, Nix and code assignments); the value may follow on the next line as
# the first item of a YAML list (`api-keys:` / `- <key>`).
_SECRET_NAME = (rb"(?i:api[_-]?keys?|secret[_-]?keys?|client[_-]?secret|access[_-]?token|refresh[_-]?token|"
                rb"id[_-]?token|auth[_-]?token|bearer[_-]?token|management[_-]?key|private[_-]?key|"
                rb"password|passwd|secrets?|token)")
_SECRET_VALUE = rb"([A-Za-z0-9_\-+/=.~|:]{20,})"
SHAPES: tuple[Shape, ...] = (
    Shape("pem-private-key", re.compile(
        rb"-----BEGIN ((?:[A-Z0-9]+ )*)PRIVATE KEY(?: BLOCK)?-----" + _PEM_BREAK + rb"*"
        rb"(?:" + _PEM_HEADER + _PEM_BREAK + rb"*)*"
        rb"((?:[A-Za-z0-9+/=]|\s|\\[nr]){0,256})"), 2),
    Shape("jwt", re.compile(rb"\beyJ" + _B64URL + rb"{10,}\.eyJ" + _B64URL + rb"{10,}\." + _B64URL + rb"{10,}")),
    Shape("github-token", re.compile(rb"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"), 1),
    Shape("slack-token", re.compile(rb"\b(xox[abposr]-[A-Za-z0-9-]{10,})"), 1),
    Shape("slack-webhook", re.compile(rb"hooks\.slack\.com/services/(T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]{16,})"),
          1),
    Shape("aws-access-key-id", re.compile(rb"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b"), 1),
    Shape("aws-secret-access-key", re.compile(
        rb"(?i:aws_secret_access_key|secret_access_key)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})"), 1),
    Shape("google-api-key", re.compile(rb"\b(AIza[0-9A-Za-z_-]{35})"), 1),
    # sk- keys: Anthropic (API and OAuth), OpenAI, OpenRouter (sk-or-v1- + 64
    # hex), DeepSeek and Qwen (sk- + 32 hex), Moonshot/Kimi. The verdict
    # reads the body after the fixed prefix.
    Shape("sk-key", re.compile(rb"(?<![A-Za-z0-9_-])sk-(?:ant-|proj-|or-v1-|svcacct-)?([A-Za-z0-9_-]{20,})"), 1),
    # Other vendor prefixes (xAI, Groq, Hugging Face, Perplexity, NVIDIA,
    # Google OAuth access tokens).
    Shape("vendor-key", re.compile(rb"(?<![A-Za-z0-9_-])(?:xai-|gsk_|hf_|pplx-|nvapi-|ya29\.)([A-Za-z0-9_-]{30,})"),
          1),
    Shape("bearer-value", re.compile(rb"(?i:\bbearer)\s+([A-Za-z0-9._~+/=_-]{20,})"), 1),
    # A scheme starts where no scheme character precedes it and is at most
    # 32 characters long, so a long dotted or dashed run is scanned once
    # (linear time), never once per position. The user name and password
    # have no length limit: neither class admits "/", so the text one match
    # attempt reads after its "://" ends before the next attempt's "://" and
    # the work stays linear in the input.
    Shape("credential-url", re.compile(
        rb"(?<![a-zA-Z0-9+.-])[a-zA-Z][a-zA-Z0-9+.-]{0,31}://[^\s/:@\"'<>]+:([^\s/@\"'<>]{8,})@[A-Za-z0-9.-]"),
          1),
    # Prefix-less keys (Meta's LLM|…, Mistral, GLM, the gateway's own hex
    # tokens) assigned to a credential-named key: a bare key (env file, YAML,
    # Nix, code) with a bare or quoted value, or a quoted key (JSON, a map
    # literal) with a quoted value; a quoted key's bare value is code (an
    # identifier), never a literal.
    Shape("secret-assignment", re.compile(
        _SECRET_NAME + rb"[ \t]*[:=][ \t]*(?:\r?\n[ \t]*-[ \t]*)?[\"']?" + _SECRET_VALUE), 1, generic=True),
    Shape("secret-assignment", re.compile(
        _SECRET_NAME + rb"[\"'][ \t]*[:=][ \t]*(?:\r?\n[ \t]*-[ \t]*)?[\"']" + _SECRET_VALUE), 1, generic=True),
)

# A placeholder word in the value or right beside it (same line, a few
# characters either side) makes a hit a synthetic sentinel; the value-only
# markers (fill characters, template syntax, "test") never clear a hit from
# its surroundings alone. Conservative: anything else stays a candidate.
SENTINEL_WORDS = (
    b"dummy", b"fake", b"example", b"sample", b"sentinel", b"fixture", b"placeholder",
    b"redacted", b"synthetic", b"not-a-real", b"notreal", b"not_real",
)
VALUE_MARKERS = SENTINEL_WORDS + (
    b"test", b"xxxx", b"000000", b"your", b"password", b"changeme", b"<", b"${", b"$(", b"%s", b"{", b"...",
    b"\xe2\x80\xa6",
)
_CONTEXT = 48


def entropy(value: bytes) -> float:
    """Shannon entropy in bits per byte."""

    if not value:
        return 0.0
    counts = collections.Counter(value)
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


# The smallest common alphabet a value is drawn from. Generated secrets come
# in all of them (DeepSeek, Qwen and OpenRouter keys and the gateway's own
# tokens are hex; AWS key ids base36), so a value's entropy is judged against
# a random value of its own alphabet and length, never an absolute bits/char
# bar a hex key can never reach (4 bits at most).
_ALPHABETS: tuple[tuple[frozenset, int], ...] = (
    (frozenset(b"0123456789"), 10),
    (frozenset(b"0123456789abcdef"), 16),
    (frozenset(b"0123456789ABCDEF"), 16),
    (frozenset(b"0123456789abcdefghijklmnopqrstuvwxyz"), 36),
    (frozenset(b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"), 36),
    (frozenset(b"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"), 62),
)
# Below this fraction of the expected entropy of a random value of the same
# alphabet and length, a value is not generated. Random values stay above it
# (measured: 0.76 at the lowest over 20,000 draws of 20 hex characters, the
# worst case of the shapes here).
RELATIVE_ENTROPY_FLOOR = 0.72
# One word: lower, UPPER or Capitalised letters with optional trailing
# digits, or digits alone. A value made only of such words joined by
# separators is an identifier, an environment variable name or a prose
# placeholder (`DEEPSEEK_CLAUDE_API_KEY`, `gwtest-fixture-gateway-token`),
# never a generated secret: random characters do not split into words.
_WORD = re.compile(rb"[a-z]+[0-9]*|[A-Z]+[0-9]*|[A-Z][a-z]+[0-9]*|[0-9]+")


def alphabet_size(value: bytes) -> int:
    chars = set(value)
    for alphabet, size in _ALPHABETS:
        if chars <= alphabet:
            return size
    return 64  # base64, base64url and token punctuation


@functools.lru_cache(maxsize=4096)
def random_entropy(length: int, size: int) -> float:
    """Expected Shannon entropy (bits/char) of a uniformly random string."""

    if length <= 1:
        return 0.0
    p = 1.0 / size
    log_p, log_q = math.log(p), math.log1p(-p)
    total = 0.0
    for count in range(1, length + 1):
        log_weight = (math.lgamma(length + 1) - math.lgamma(count + 1) - math.lgamma(length - count + 1)
                      + count * log_p + (length - count) * log_q)
        if log_weight < -60:
            continue
        share = count / length
        total += math.exp(log_weight) * share * math.log2(share)
    return -size * total


def word_shaped(value: bytes) -> bool:
    parts = [part for part in re.split(rb"[^A-Za-z0-9]+", value) if part]
    return (len(parts) >= 2 and all(_WORD.fullmatch(part) for part in parts)
            and any(part[:1].isalpha() for part in parts))


def _periodic(value: bytes) -> bool:
    """The value repeats one block (``abcabcabc…``, ``0123456789abcdef`` twice)."""

    for period in range(1, min(len(value) // 2, 64) + 1):
        if value == (value[:period] * (len(value) // period + 1))[:len(value)]:
            return True
    return False


def alphanumeric_core(value: bytes) -> bytes:
    """The letters and digits of ``value``: separators (``-``, ``_``, ``.``,
    ``:``…) and base64 punctuation never enlarge the alphabet a value is
    judged against."""

    return bytes(byte for byte in value if chr(byte).isascii() and chr(byte).isalnum())


# A separated segment at least this long is also judged on its own.
SEGMENT_MIN = 12


def _relative(value: bytes) -> float:
    expected = random_entropy(len(value), alphabet_size(value))
    return entropy(value) / expected if expected else 0.0


def relative_entropy(value: bytes) -> float:
    """How random the value looks: its alphanumeric core judged against a
    random value of the core's own alphabet and length, and each long
    segment between separators judged on its own (the higher wins). A
    dashed hex key (a UUID) is judged as hex, a token of digit groups and a
    hex tail (a Slack token) by those groups; a separator never makes a
    value look less random by enlarging its alphabet."""

    core = alphanumeric_core(value) or value
    best = _relative(core)
    for segment in re.split(rb"[^A-Za-z0-9]+", value):
        if len(segment) >= SEGMENT_MIN:
            best = max(best, _relative(segment))
    return best


def classify(value: bytes, context: bytes) -> tuple[str, str]:
    """``("synthetic", reason)`` or ``("candidate", reason)`` for one hit.

    Conservative: a hit is a synthetic sentinel only for a placeholder
    marker, fill characters, a repeating pattern, a value made of words, or
    an entropy well below a random value of its own alphabet and length.
    """

    lowered_value = value.lower()
    for marker in VALUE_MARKERS:
        if marker in lowered_value:
            return "synthetic", "placeholder marker in the value"
    lowered = context.lower()
    for marker in SENTINEL_WORDS:
        if marker in lowered:
            return "synthetic", "placeholder word beside the value"
    # A run is fill only when it is most of the value: structured key
    # material (the OpenSSH body's zero length fields) has short runs.
    longest = max((len(match.group(0)) for match in re.finditer(rb"(.)\1*", value)), default=0)
    if value and longest * 2 >= len(value):
        return "synthetic", "fill characters"
    if _periodic(value):
        return "synthetic", "repeating pattern"
    if word_shaped(value):
        return "synthetic", "words or an identifier, not a generated value"
    if relative_entropy(value) < RELATIVE_ENTROPY_FLOOR:
        return "synthetic", f"entropy below {RELATIVE_ENTROPY_FLOOR} of a random value of its alphabet"
    return "candidate", "random-looking value, no placeholder marker"


@dataclass
class CredentialHit:
    shape: str
    verdict: str
    reason: str
    blob: str
    line: int
    length: int
    entropy: float
    kind: str = "blob"  # blob | commit-message | tag-message
    commit: str | None = None
    path: str | None = None
    occurrences: int = 1  # commits whose tree introduced this blob at some path


def credential_hits(data: bytes) -> Iterator[tuple[str, int, int, float, str, str]]:
    """``(shape, line, length, entropy, verdict, reason)`` per hit; never the value."""

    claimed: list[tuple[int, int]] = []  # value spans of the specific shapes
    for shape in SHAPES:
        for match in shape.pattern.finditer(data):
            value = match.group(shape.group) or b""
            if shape.name == "pem-private-key":
                value = re.sub(rb"\s+|\\[nr]", b"", value)[:200]
            start, end = match.span(shape.group)
            if shape.generic:
                if any(start < other_end and other_start < end for other_start, other_end in claimed):
                    continue
            else:
                claimed.append((start, end))
            line_start = data.rfind(b"\n", 0, start) + 1
            line_end = data.find(b"\n", end)
            line_end = len(data) if line_end < 0 else line_end
            context = data[max(line_start, start - _CONTEXT):start] + data[end:min(line_end, end + _CONTEXT)]
            if shape.name == "pem-private-key":
                header_line = data.rfind(b"\n", 0, match.start()) + 1
                context = data[max(header_line, match.start() - _CONTEXT):match.start()]
                if not value:
                    yield (shape.name, data.count(b"\n", 0, match.start()) + 1, 0, 0.0,
                           "synthetic", "header only, no key body")
                    continue
            verdict, reason = classify(value, context)
            yield (shape.name, data.count(b"\n", 0, start) + 1, len(value), round(entropy(value), 2),
                   verdict, reason)


# --------------------------------------------------------------- git access

class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}
    proc = subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(repo), *args], input=stdin,
                          capture_output=True, env=env)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed ({proc.returncode}): "
                       f"{proc.stderr.decode(errors='replace').strip()[:300]}")
    return proc.stdout


_RANGE = re.compile(r"([0-9A-Za-z][0-9A-Za-z._/@^~-]*)\.\.([0-9A-Za-z][0-9A-Za-z._/@^~-]*)")


def resolve_range(repo: Path, spec: str) -> tuple[str, str]:
    """``BASE..HEAD`` as two commit ids; :class:`GitError` when either end is
    not a commit in ``repo`` (the scan never silently scans less)."""

    match = _RANGE.fullmatch(spec)
    if match is None or ".." in match.group(1) or ".." in match.group(2):
        raise GitError(f"--range must be BASE..HEAD (two revisions), not {spec!r}")
    ends = []
    for revision in match.groups():
        try:
            ends.append(_git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                             f"{revision}^{{commit}}").decode().strip())
        except GitError as exc:
            raise GitError(f"the revision {revision!r} of --range is not a commit in this repository "
                           "(fetch the full history)") from exc
    return ends[0], ends[1]


def _selection(revisions: tuple[str, str] | None) -> list[str]:
    return ["--all"] if revisions is None else [revisions[1], f"^{revisions[0]}"]


def reachable_objects(repo: Path, paths: set[str] | None = None,
                      revisions: tuple[str, str] | None = None) -> dict[str, str]:
    """Every object reachable from every ref (or, with ``revisions``
    ``(base, head)``, from head but not from base): id -> type.

    ``paths`` (when given) collects every tree and blob path the listing
    names: a personal identifier in a file or directory name is part of
    what the history reveals too.
    """

    listed = _git(repo, "rev-list", "--objects", *_selection(revisions), "--")
    ids = set()
    for line in listed.splitlines():
        if not line.strip():
            continue
        object_id, _sep, path = line.partition(b" ")
        ids.add(object_id.decode())
        if paths is not None and path:
            paths.add(path.decode(errors="replace"))
    ids = sorted(ids)
    if not ids:
        return {}
    checked = _git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                   stdin=("\n".join(ids) + "\n").encode())
    found: dict[str, str] = {}
    for line in checked.decode().splitlines():
        parts = line.split()
        if len(parts) == 2:
            found[parts[0]] = parts[1]
    return found


def blob_locations(repo: Path, revisions: tuple[str, str] | None = None) -> dict[str, tuple[str, str, int, set[str]]]:
    """blob -> (first commit, first path, introductions, every path).

    Walks every reachable commit (or those of ``revisions``) oldest first
    with its raw diff against each parent (``-m``; root commits against the
    empty tree), so a blob first added in a merge resolution is attributed
    too.
    """

    raw = _git(repo, "log", *_selection(revisions), "--reverse", "--raw", "--no-abbrev", "--no-renames", "-m",
               "--root", "--format=C %H", "--")
    locations: dict[str, tuple[str, str, int, set[str]]] = {}
    commit = ""
    for line in raw.split(b"\n"):
        if line.startswith(b"C "):
            commit = line[2:].decode()
            continue
        if not line.startswith(b":"):
            continue
        meta, _tab, path_bytes = line.partition(b"\t")
        fields = meta.split()
        if len(fields) < 5:
            continue
        new_mode, new_id, status = fields[1], fields[3].decode(), fields[4][:1]
        if status == b"D" or new_mode == b"160000" or set(new_id) == {"0"}:
            continue
        path = path_bytes.decode(errors="replace")
        if new_id in locations:
            first_commit, first_path, count, paths = locations[new_id]
            paths.add(path)
            locations[new_id] = (first_commit, first_path, count + 1, paths)
        else:
            locations[new_id] = (commit, path, 1, {path})
    return locations


def read_objects(repo: Path, ids: Sequence[str]) -> Iterator[tuple[str, str, bytes]]:
    """``(id, type, content)`` for each id through one ``cat-file --batch``."""

    if not ids:
        return
    proc = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
    assert proc.stdin is not None and proc.stdout is not None

    def feed() -> None:
        try:
            for object_id in ids:
                proc.stdin.write((object_id + "\n").encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        for _ in ids:
            header = proc.stdout.readline().decode().split()
            if len(header) != 3:
                raise GitError(f"cat-file --batch: unexpected header {header!r}")
            object_id, object_type, size = header[0], header[1], int(header[2])
            content = proc.stdout.read(size)
            proc.stdout.read(1)  # the trailing newline
            yield object_id, object_type, content
    finally:
        writer.join(timeout=5)
        proc.stdout.close()
        proc.wait(timeout=60)


# --------------------------------------------------------------- the scan

@dataclass
class ScanResult:
    refs: int = 0
    commits: int = 0
    tags: int = 0
    blobs: int = 0
    blob_bytes: int = 0
    unattributed_blobs: int = 0
    uninspected_blobs: int = 0
    credentials: list[CredentialHit] = field(default_factory=list)
    # identifier -> path -> [occurrences, blobs, first commit]
    identifiers: dict[str, dict[str, list]] = field(default_factory=dict)
    identity_headers: collections.Counter = field(default_factory=collections.Counter)
    message_identifiers: collections.Counter = field(default_factory=collections.Counter)
    # identifier -> distinct tree/blob paths (and ref names) that carry it
    path_identifiers: collections.Counter = field(default_factory=collections.Counter)
    ref_identifiers: collections.Counter = field(default_factory=collections.Counter)
    lan_hosts: collections.Counter = field(default_factory=collections.Counter)
    email_domains: collections.Counter = field(default_factory=collections.Counter)
    private_ipv4: int = 0

    def candidates(self) -> list[CredentialHit]:
        return [hit for hit in self.credentials if hit.verdict == "candidate"]


# Content the scan does not decode: binary data and compressed or archived
# formats, recognised by their leading bytes.
_UNINSPECTED_MAGIC = (b"\x1f\x8b", b"PK\x03\x04", b"\xfd7zXZ\x00", b"BZh", b"\x28\xb5\x2f\xfd",
                      b"7z\xbc\xaf\x27\x1c")


def uninspected(data: bytes) -> bool:
    """True for content the credential shapes cannot see into (binary,
    compressed or archived data)."""

    return data.startswith(_UNINSPECTED_MAGIC) or b"\x00" in data[:8192]


def _note_identifiers(result: ScanResult, data: bytes, path: str, commit: str | None,
                      private: PrivateInput | None = None) -> None:
    for name, count in identifier_counts(data, private).items():
        row = result.identifiers.setdefault(name, {}).setdefault(path, [0, 0, commit])
        row[0] += count
        row[1] += 1
    for match in _LAN_HOST_RE.finditer(data):
        result.lan_hosts[match.group(1).decode(errors="replace").lower() + ".lan"] += 1
    for match in _EMAIL_RE.finditer(data):
        result.email_domains[match.group(1).decode(errors="replace").lower()] += 1
    result.private_ipv4 += len(_PRIVATE_IPV4_RE.findall(data))


def scan(repo: Path, revision_range: str | None = None, private: PrivateInput | None = None) -> ScanResult:
    result = ScanResult()
    revisions = resolve_range(repo, revision_range) if revision_range is not None else None
    refs = [] if revisions is not None else [
        line for line in _git(repo, "for-each-ref", "--format=%(refname)").splitlines() if line]
    result.refs = len(refs)
    for ref in refs:
        for name in identifier_counts(ref, private):
            result.ref_identifiers[name] += 1
    paths: set[str] = set()
    objects = reachable_objects(repo, paths, revisions)
    locations = blob_locations(repo, revisions)
    for _commit, _path, _count, blob_paths in locations.values():
        paths.update(blob_paths)
    for path in paths:
        for name in identifier_counts(path.encode(), private):
            result.path_identifiers[name] += 1
    blobs = sorted(object_id for object_id, kind in objects.items() if kind == "blob")
    meta = sorted(object_id for object_id, kind in objects.items() if kind in ("commit", "tag"))
    result.blobs = len(blobs)
    result.commits = sum(1 for kind in objects.values() if kind == "commit")
    result.tags = sum(1 for kind in objects.values() if kind == "tag")
    for object_id, _kind, data in read_objects(repo, blobs):
        result.blob_bytes += len(data)
        commit, path, introductions, _paths = locations.get(object_id, (None, None, 0, set()))
        if commit is None:
            result.unattributed_blobs += 1
        if uninspected(data):
            result.uninspected_blobs += 1
        for shape, line, length, bits, verdict, reason in credential_hits(data):
            result.credentials.append(CredentialHit(shape, verdict, reason, object_id, line, length, bits,
                                                     "blob", commit, path, introductions))
        _note_identifiers(result, data, path or f"<unattributed blob {object_id[:12]}>", commit, private)
    for object_id, kind, data in read_objects(repo, meta):
        header, _sep, message = data.partition(b"\n\n")
        for line in header.split(b"\n"):
            if line.startswith((b"author ", b"committer ", b"tagger ")):
                role = line.split(b" ", 1)[0].decode()
                for name in identifier_counts(line, private):
                    result.identity_headers[f"{role}:{name}"] += 1
        for name, count in identifier_counts(message, private).items():
            result.message_identifiers[f"{kind}-message:{name}"] += count
        for shape, line, length, bits, verdict, reason in credential_hits(message):
            result.credentials.append(CredentialHit(shape, verdict, reason, object_id, line, length, bits,
                                                    f"{kind}-message", object_id if kind == "commit" else None))
    return result


# --------------------------------------------------------------- reporting

def summary(result: ScanResult) -> dict:
    by_shape: dict[str, dict[str, int]] = {}
    for hit in result.credentials:
        row = by_shape.setdefault(hit.shape, {"synthetic": 0, "candidate": 0})
        row[hit.verdict] += 1
    identifiers = {
        name: {"occurrences": sum(row[0] for row in paths.values()),
               "blobs": sum(row[1] for row in paths.values()), "paths": len(paths)}
        for name, paths in sorted(result.identifiers.items())
    }
    return {
        "schema": "cm-history-scan-v1",
        "refs": result.refs, "commits": result.commits, "annotated_tags": result.tags,
        "blobs": result.blobs, "blob_bytes": result.blob_bytes,
        "unattributed_blobs": result.unattributed_blobs,
        "uninspected_blobs": result.uninspected_blobs,
        "credentials": {"total": len(result.credentials),
                        "synthetic": sum(1 for hit in result.credentials if hit.verdict == "synthetic"),
                        "candidate": len(result.candidates()), "by_shape": dict(sorted(by_shape.items()))},
        "identifiers": identifiers,
        "identity_headers": dict(sorted(result.identity_headers.items())),
        "message_identifiers": dict(sorted(result.message_identifiers.items())),
        "path_identifiers": dict(sorted(result.path_identifiers.items())),
        "ref_identifiers": dict(sorted(result.ref_identifiers.items())),
        "lan_hosts": dict(sorted(result.lan_hosts.items())),
        "email_domains": dict(sorted(result.email_domains.items())),
        "private_ipv4_occurrences": result.private_ipv4,
    }


def _hit_row(hit: CredentialHit) -> dict:
    return {"shape": hit.shape, "verdict": hit.verdict, "reason": hit.reason, "kind": hit.kind,
            "object": hit.blob, "commit": hit.commit, "path": hit.path, "line": hit.line,
            "length": hit.length, "entropy": hit.entropy, "introductions": hit.occurrences}


def render_text(result: ScanResult, *, max_locations: int) -> str:
    doc = summary(result)
    out = [
        f"history scan: {doc['refs']} refs, {doc['commits']} commits, {doc['annotated_tags']} annotated tags, "
        f"{doc['blobs']} blobs ({doc['blob_bytes']} bytes), {doc['unattributed_blobs']} unattributed, "
        f"{doc['uninspected_blobs']} uninspected (binary, compressed or archived)",
        f"credential shapes: {doc['credentials']['total']} hits, {doc['credentials']['synthetic']} synthetic "
        f"sentinels, {doc['credentials']['candidate']} candidates",
    ]
    for shape, row in doc["credentials"]["by_shape"].items():
        out.append(f"  {shape}: synthetic {row['synthetic']}, candidate {row['candidate']}")
    for hit in result.candidates()[:max_locations]:
        out.append(f"  CANDIDATE {hit.shape} {hit.kind} object {hit.blob[:12]} commit "
                   f"{(hit.commit or '-')[:12]} path {hit.path or '-'} line {hit.line} "
                   f"length {hit.length} entropy {hit.entropy}")
    out.append("personal identifiers:")
    for name, row in doc["identifiers"].items():
        out.append(f"  {name}: {row['occurrences']} occurrences in {row['blobs']} blobs, {row['paths']} paths")
        paths = sorted(result.identifiers[name].items(), key=lambda item: (-item[1][0], item[0]))
        for path, (count, blob_count, commit) in paths[:max_locations]:
            out.append(f"    {path}: {count} in {blob_count} blobs (first {(commit or '-')[:12]})")
    for label in ("identity_headers", "message_identifiers", "path_identifiers", "ref_identifiers", "lan_hosts",
                  "email_domains"):
        if doc[label]:
            out.append(f"{label.replace('_', ' ')}: " + ", ".join(f"{k} {v}" for k, v in doc[label].items()))
    out.append(f"private IPv4 occurrences: {doc['private_ipv4_occurrences']}")
    out.append("a clean result is not proof that no secret is present: see the limits in --help")
    return "\n".join(out) + "\n"


def _write_private(directory: Path, name: str, doc: object) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    target = directory / name
    tmp = directory / f".{name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, target)


def write_outputs(result: ScanResult, out: Path) -> None:
    _write_private(out, "summary.json", summary(result))
    _write_private(out, "credentials.json", [_hit_row(hit) for hit in result.credentials])
    _write_private(out, "identifiers.json", {
        name: {path: {"occurrences": row[0], "blobs": row[1], "first_commit": row[2]}
               for path, row in sorted(paths.items())}
        for name, paths in sorted(result.identifiers.items())
    })


def main(argv: Sequence[str] | None = None, stdout=None) -> int:
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0], epilog=LIMITS,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--range", dest="revision_range", metavar="BASE..HEAD",
                        help="scan only what HEAD adds to BASE (a pull request or a push)")
    parser.add_argument("--identifiers", type=Path, metavar="FILE",
                        help=f"the private identifier list (default: the file ${PRIVATE_INPUT_ENV} names; "
                        "without one, only the generic identifiers are looked for)")
    parser.add_argument("--out", type=Path, help="write summary.json, credentials.json, identifiers.json here")
    parser.add_argument("--max-locations", type=int, default=20, help="locations printed per name")
    args = parser.parse_args(argv)
    try:
        private = (load_private_input(args.identifiers) if args.identifiers is not None
                   else private_input_from_env())
    except PrivateInputError as exc:
        print(f"history scan: private identifier input: {exc}", file=sys.stderr)
        return 2
    try:
        result = scan(args.repo, args.revision_range, private)
    except (GitError, OSError) as exc:
        print(f"history scan: {exc}", file=sys.stderr)
        return 2
    stdout.write("personal identifiers: the generic ones and a private list\n" if private is not None
                 else "personal identifiers: the generic ones only (no private list given)\n")
    if args.revision_range is not None:
        stdout.write(f"history scan of the range {args.revision_range}\n")
    stdout.write(render_text(result, max_locations=args.max_locations))
    if args.out is not None:
        write_outputs(result, args.out)
    return 1 if result.candidates() else 0


if __name__ == "__main__":
    raise SystemExit(main())
