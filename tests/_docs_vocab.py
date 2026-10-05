"""Documentation gate rules, one source for every user.

``tests/check_docs_vocabulary.py`` (the command-line checker, never
discovered), ``test_operator_docs`` and ``test_docs_vocabulary`` import this
module; none of them carries a second copy of a rule. Stdlib only;
``claude_multi`` is imported lazily (only the ``--help`` walk, the
command-span parser and the fragment check need it).

The public pages are discovered from the tree (:func:`public_pages`): the
five root pages and every Markdown page under ``docs/``, nested
directories included. A page's label is its name at the root, or its path
below ``docs/`` (``USAGE.md``, ``install/linux.md``).

Rules:

- R1–R6 ``TERMS`` (stale vocabulary). A hit is **intentional** iff one of:
  an ``EXPLICIT`` entry covers it; it is a *code identifier* (inside a
  backtick span, a package path that exists or a code-shaped identifier the
  package source defines: AGENTS.md's module map names ``composition.py``
  and ``CompositionStore`` and must keep them verbatim); it is a *wiki
  slug* (``[[…]]`` or a frontmatter ``related:`` item); it sits under a
  heading matching ``HEADING_MARKERS``; or ``LINE_MARKERS`` matches its
  **context**: the paragraph / list item / fenced block it belongs to (so a
  hard-wrapped continuation line keeps its marker) — for a table row only
  the cell it sits in — clipped to ``MARKER_WINDOW`` characters on each side
  of the hit (so one marker cannot whitelist a 7,000-character row). Every
  other hit fails.
- R7 ``secret-read`` (no allowlist, every file): the ``R7_FLOOR``
  regexes (the secret-file reads plus the management-secret and
  process-environment rows) on every physical line of every unit, plain
  prose included; on top of them, a command context (a backtick span, a
  fenced-code line with ``\\`` continuations joined, a plain-text line) that
  reads a secret (any word of a shell segment that is a ``READ_COMMANDS``
  entry followed later in the segment by one of ``SECRET_NAMES``, so
  ``sudo -u X cat``, ``timeout 5 cat``, ``watch cat``, ``ssh host cat``
  count; quoted ``sh -c``/``bash -c``/``ssh`` bodies are parsed as command
  lines; a ``<`` redirection from one; ``$(< …)``/``$(cat …)``), runs the
  gateway token helper (command position after ``;``, ``&&``, ``||``,
  ``|``, ``$(``, a wrapper or a quoted ``-c`` body; alone on a fenced
  line; after a run verb), or sends the token in a header; a physical line
  saying never/Never is exempt. ``stat -c %s`` (no open) is the sanctioned
  size check.
- R8 ``backend-command`` (the public pages): a paragraph, list item,
  fenced block (with the paragraph before it) or table row that runs a
  service-manager command (``systemctl --user``, ``journalctl --user``,
  ``launchctl``) names the backend it applies to (systemd or launchd), in
  the unit or a heading above it, and never targets the earlier unit name
  ``cli-proxy-api``; ordinary remedies use the public gateway commands
  (``claude-multi gateway …``).
- R9 ``machine-path``: a personal identifier in every in-tree file: the
  home directory of a real account (``tools/history_scan.py``
  ``GENERIC_IDENTIFIERS``; neutral identities such as ``/home/user`` pass)
  and, when the environment names the private identifier input
  (``history_scan.PRIVATE_INPUT_ENV``), its exact list; the public
  repository's address is masked first. The rendered ``--help`` is
  scanned with the installation root replaced by ``<installation>``. The
  user pages also refuse ``/nix/store/`` paths and machine generation
  numbers. No page has an exception.
- R10 ``profile-migrate-flags``: no ``profile migrate --…`` (the migration
  help owns them).
- R11 ``journal-auth``: a command context running ``journalctl`` pipes it
  so that no ``auth=`` value reaches the output: it ends counted
  (``grep -c``, ``wc``) or with auth dropped (``grep -v`` on auth, a
  ``sed`` substitution whose pattern consumes the auth value —
  ``s/ auth=[^ ]+//``, not ``s/auth=//p`` —, a ``grep -o`` whose patterns
  each stay inside one non-auth field such as ``'model=[^ ]+'``, never
  ``'.*'`` or a generic key class); a stage that selects auth (``grep -o
  'auth=…'``, ``sed -n '/auth=/p'``, a plain ``grep auth``) undoes it. A
  bare ``journalctl`` mention is a command too.
- R12 ``lineup-session-id``: ``lineup --session`` takes the runtime id, so
  Markdown never writes ``lineup --session <id>``/``ID``; only
  ``<runtime-id>`` or ``$CLAUDE_CODE_SESSION_ID``.
- R13 ``this-machine``: no "this machine" remedy on a public page; the
  phrase may appear only inside a “…” quotation of launcher text (which the
  fragment check ties to the source).
- R14 ``operator-skill``: no public page names an operator skill (the
  private input's ``operator-skill`` entries, :func:`operator_skills`).
- R15 ``release-lineage`` (the user pages): no earlier release line of
  this product (``RELEASE_LINE``), never a version of another program.

Also here: the ``--help`` walk (``help_pages``), the command-span checker
(``command_spans``/``check_span``: every form of a span, each optional
part left out and put in and each alternative, parsed by the parser that
owns it — the launcher's, ``claude-multi lineup``'s and ``/cm``'s request
parser, the gateway tool's command table, ``claude-multi-dev``'s rules —
with the finite typed ``PLACEHOLDERS`` registry, typed grammar
``FRAGMENTS``, registered ``NEGATIVE_EXAMPLES`` and ``span_coverage``
counts), the fragment check
(``fragment_problems``), the link check (every relative link resolves
inside the repository, heading anchors included; a page under ``docs/``
reaches the root pages only by their public URL; ``private_references``
refuses mentions of material that is not part of the repository), the
tracked drafting markers (``todo_markers``; ``placeholder_problems``
refuses an untracked placeholder), the in-tree target set, budgets and
``self_test()`` (the rule unit cases, run by the checker's
``--self-test`` and by ``test_docs_vocabulary``).
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import fnmatch
import functools
import io
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from _layout import DOCS_DIR, REPO_ROOT, RESOURCES_ROOT, doc_path  # the suite's single root source

SRC_ROOT = REPO_ROOT / "src" / "claude_multi"

# ---------------------------------------------------------------- terms

TERMS: dict[str, re.Pattern[str]] = {
    "composition": re.compile(r"composition", re.I),
    "variant": re.compile(r"\bvariants?\b", re.I),  # not "invariant"
    # The model segment may contain dashes (kimi-k3), the
    # placeholders and wildcards match, and the end is (?![\w-]) because
    # `>` and `*` are not word characters.
    "model-in-name": re.compile(
        r"\bcm-(?:\*|<[a-z]+>|[a-z]+)-(?:\*|<[a-z]+>|[a-z0-9.*<>-]+?)"
        r"-(?:\*|<[a-z]+>|low|medium|high|xhigh|max)(?![\w-])",
        re.I,
    ),
    "--legacy": re.compile(r"--legacy\b"),
    "no-hot-reload": re.compile(
        r"\b(?:does|do)\s*(?:\*\*)?\s*not\s*(?:\*\*)?\s+hot[- ]?reload"
        r"|\bdoesn[’']t\s+hot[- ]?reload",
        re.I,
    ),
    "isolation-contracts": re.compile(r"isolation\s+contracts?", re.I),
}
# "retired" and "alias(es)" are also live 3.0 words
# (`retired.json`, "retired keys", "continuity aliases"); those spellings are
# not history and never whitelist a hit.
LINE_MARKERS = re.compile(
    r"2\.x|\blegacy\b|(?<!continuity )\baliases?\b|\bretired\b(?![._\w-]|\s+keys?\b)"
    r"|\bhistor(?:y|ical)\b|\bv1[–-]3\b|\.v3\.json"
)
HEADING_MARKERS = re.compile(r"histor|2\.x|earlier|archive", re.I)
MARKER_WINDOW = 200  # characters on each side of a hit
SOURCE_PHRASE_TERMS = ("no-hot-reload", "isolation-contracts")

# Argparse renders the kept 2.x alias flags, metavars and dests; they
# are blanked (same length) before the terms run over a --help page.
HELP_ALIAS_TOKENS = re.compile(
    r"--composition-file|--composition|\b[A-Z_]*COMPOSITION\b|\bshow_composition\b"
)

# (file glob, exact substring, reason): an in-tree hit whose position lies in
# an occurrence of the substring is intentional. At most MAX_EXPLICIT
# entries; every entry must still match something (no stale allowlist).
# An entry is added only where no marker, heading or code-identifier rule
# can express the intent.
EXPLICIT: tuple[tuple[str, str, str], ...] = (
    # The AGENTS.md §3 cli.py row stays verbatim; it records
    # the 2.26 retirement of the argv mode, and no marker sits within reach
    # in that 4,500-character cell.
    (
        "AGENTS.md",
        "`--legacy` refused before Runtime (exit 2)",
        "the cli.py row, kept verbatim: the retired argv mode",
    ),
)
MAX_EXPLICIT = 10

# Intentional-hit budgets per in-tree target label: the observed count
# plus at most BUDGET_HEADROOM (``test_docs_vocabulary`` fails on a budget
# further above the observed count, so a removed mention tightens the
# budget); a label absent from it has budget 0. The rendered --help pages
# share ``<help>``, which is exact: the one intentional hit is
# ``profile migrate`` "convert legacy compositions"; the generated command
# reference quotes the same summary.
BUDGETS: dict[str, int] = {
    "AGENTS.md": 25,  # the module map rows (code identifiers) and their legacy-format context
    "CHANGELOG.md": 3,  # retained CLI aliases paired with their public spellings
    "<help>": 1,  # profile migrate: "convert legacy compositions to profiles"
    "reference/cli.md": 1,  # the same summary, generated from the parser
}
BUDGET_HEADROOM = 2

# ------------------------------------------------------------ R7-R15

# The OAuth records (under ~/.local/share/claude-multi; `pinned-clients/`
# there is not one) and the
# auth-file shapes (`claude-<account>.json`, `codex-<account>.json`).
SECRET_NAMES = re.compile(
    r"api-key|previous-key|management-key|MANAGEMENT_PASSWORD"
    r"|config\.yaml|claude\.env|\.credentials\.json|secrets/"
    r"|share/claude-multi/(?!pinned-clients\b)|\b(?:claude|codex)-[^\s/'\"]*\.json", re.I
)
TOKEN_HELPER = "claude-multi-gateway-token"
# The helper in command position: at the start, after a shell separator
# (`;`, `&&`, `||`, `|`, `&`), inside `$( )` or backticks, or behind a
# wrapper (`sh -c '…'`, `sudo -u X`, `timeout 5`, `watch`, `env A=1`, …);
# and followed by the end, a separator, a redirection, a closing
# paren/quote, or a flag. `start`/`end` tell the bare form (the helper
# alone) apart: in a fenced line that runs it, in an inline span it is a
# name unless a run verb precedes it. A file listing
# (`bin/claude-multi-gateway-token (apiKeyHelper shim)`) is a name.
_HELPER_RUN = re.compile(
    r"(?:(?P<start>^\s*(?:\$\s+)?['\"]?)"
    r"|(?:[;|&]|\$\(|`)\s*['\"]?"
    r"|\b(?:sh|bash|zsh|dash|ksh|exec|env|sudo|doas|command|builtin|eval|xargs|time|nice|nohup|"
    r"setsid|stdbuf|timeout|watch|ssh|su|runuser|flock|script)\s+(?:[^\s;&|]+\s+)*?['\"]?)"
    r"[^\s|;&'\"()`]*" + TOKEN_HELPER +
    r"(?=(?P<end>['\"]?\s*$)|\s*(?:[;&|<>)'\"`])|\s+-)"
)
# The gateway process environment holds MANAGEMENT_PASSWORD, so
# reading any /proc/<pid>/environ or `ps e` output is a secret read. Only
# the exact names-only pipeline stays allowed: the
# NUL→newline tr straight into `cut -d= -f1` (optionally `| sort`), with no
# other stage that could copy values (no tee, no second command).
_ENV_PATH = r"/proc/[^\s/'\"]+/environ\b"
_PROC_ENVIRON = re.compile(_ENV_PATH)
_NUL_TO_NL = r"tr\s+['\"]?\\0['\"]?\s+['\"]?\\n['\"]?"
_ENV_NAMES_ONLY = re.compile(
    rf"(?:{_NUL_TO_NL}\s*<\s*{_ENV_PATH}|cat\s+{_ENV_PATH}\s*\|\s*{_NUL_TO_NL})"
    r"\s*\|\s*cut\s+-d\s*['\"]?=['\"]?\s+-f\s*1(?![\d,-])(?:\s*\|\s*sort(?:\s+-u)?)?"
)


class _EnvironRead:
    """R7 floor row: a process-environment read outside the names-only pipeline."""

    _READ = re.compile(
        r"(?:\b(?:cat|head|tail|less|more|bat|xxd|od|strings|tr|xargs|tee|dd)\b[^\n|;&]*?|<\s*)" + _ENV_PATH
    )
    pattern = _READ.pattern

    def search(self, text: str):
        return self._READ.search(_ENV_NAMES_ONLY.sub(" ", text))


# R7 plus the management secrets: the floor on every physical line of every
# unit (prose included); the segment analysis below only widens it.
R7_FLOOR = (
    re.compile(r"\$\(\s*(?:cat|<)\s*[^)]*(?:api-key|previous-key|management-key|claude\.env|config\.yaml|\.credentials\.json)"),
    re.compile(
        r"\b(?:cat|head|tail|less|more|bat|xxd|od|strings)\s+[^\s|;&]*"
        r"(?:api-key|previous-key|management-key|secrets/\S+|\.credentials\.json|config\.yaml)\b"
    ),
    re.compile(r"(?:x-api-key|X-Management-Key):\s*\"?\$", re.I),
    re.compile(r"\b(?:echo|printf|printenv)\b[^\n]*\bMANAGEMENT_PASSWORD\b", re.I),
    re.compile(r"Authorization:\s*Bearer\s+\$", re.I),
    _EnvironRead(),
    # BSD-style `ps e…` (eww, auxe, axeww) prints each process environment,
    # also after other arguments (`ps -p "$PID" eww`); `ps -e` is harmless.
    re.compile(
        r"\bps\b(?:\s+[^\s|;&]+)*?\s+[aAcfhjlmnrsSTuvwxX]*e[aAcefhjlmnrsSTuvwxX]*(?=[\s;|&)'\"`]|$)"
    ),
)
READ_COMMANDS = frozenset(
    """
    cat tac head tail less more bat most xxd od hexdump strings wc grep egrep
    fgrep rg ag awk gawk sed tr cut sort uniq nl fold base64 base32 sha1sum
    sha256sum sha512sum md5sum b2sum cksum diff cmp python python3 perl ruby
    node jq yq source . cp scp rsync tee vi vim nvim nano emacs code openssl
    gpg curl wget nc dd install xargs read mapfile
    """.split()
)
_WRAPPERS = frozenset({"sudo", "command", "exec", "env", "time", "nice", "nohup", "builtin"})
_SHELL_FILTERS = frozenset(
    {"grep", "egrep", "fgrep", "rg", "sed", "awk", "sort", "uniq", "head", "tail", "wc",
     "jq", "cut", "tr", "less", "column", "tee", "xargs", "python3", "cat", "sha256sum"}
)
_SUBSHELL_READ = re.compile(
    r"\$\(\s*(?:cat|<)\s*[^)]*(?:api-key|previous-key|management-key|claude\.env|config\.yaml|\.credentials\.json)"
)
_HEADER_SECRET = re.compile(r"(?:x-api-key|X-Management-Key):\s*\"?\$|Authorization:\s*Bearer\s+\$", re.I)
_RUN_VERB = re.compile(r"\b(?:run|execute|invoke|call)\b[^`\n]{0,24}$", re.I)
_NEVER = re.compile(r"\bnever\b", re.I)

# R8: a service-manager command, the backend names that label it, and the
# earlier unit name a public page never targets.
UNIT_NAME = re.compile(r"\bcli-proxy-api\b(?![-.\w/])")
SERVICE_COMMAND = re.compile(r"systemctl\s+--user|journalctl\s+--user|\blaunchctl\b")
BACKEND_NAME = re.compile(r"\bsystemd\b|\blaunchd\b", re.I)
THIS_MACHINE = re.compile(r"this machine", re.I)
# R14: the operator skills, which are not part of the product. Their names
# are private: the private input's entries under this label
# (tools/history_scan.py); without that input the rule has nothing to find.
OPERATOR_SKILL_LABEL = "operator-skill"
# R15: an earlier release line of this product (2.x, 2.2N, 3.0, 3.0.x,
# 3.1.0, pre-3.0); never a version of another program (Claude Code 2.1.286,
# CLIProxyAPI 7.3.15, Python 3.11), a schema, a path or a number.
# test_docs_vocabulary checks it against test_hygiene's lineage pattern.
RELEASE_LINE = re.compile(r"(?<![\w./+\\-])(?:pre-)?(?:2\.(?:x|2\d)|3\.[0-3](?:\.(?:x|\d+))?)(?![\w%]|\.\d)")

# R9 beyond the identifiers (the user pages): store paths and a machine's
# generation numbers.
MACHINE_PATH_EXTENDED = (
    re.compile(r"/nix/store/"),
    re.compile(r"\bgen(?:eration)? 1\d\d\b", re.I),
)
# The public repository's address is the product's own, never a personal
# identifier (as in the identifier gate).
_PUBLIC_REPOSITORY_ADDRESS = re.compile(r"(?:https://github\.com/)?nkoturovic/claude-multi(?![\w-])")


@functools.lru_cache(maxsize=1)
def operator_skills() -> re.Pattern[str] | None:
    """The operator skills' names (the private input's
    :data:`OPERATOR_SKILL_LABEL` entries) as one pattern, or None."""

    from _layout import load_tool

    tool = load_tool("history_scan")
    private = tool.private_input_from_env()
    patterns = [pattern for label, pattern in (private.identifiers if private else ()) if label == OPERATOR_SKILL_LABEL]
    return re.compile("|".join(f"(?:{pattern})" for pattern in patterns)) if patterns else None


@functools.lru_cache(maxsize=1)
def _identifier_patterns() -> tuple[re.Pattern[str], ...]:
    """The identifier table (generic, plus the private input the
    environment names), as text patterns."""

    from _layout import load_tool

    tool = load_tool("history_scan")
    return tuple(re.compile(pattern) for _name, pattern in tool.identifier_table(tool.private_input_from_env()))
PROFILE_MIGRATE_FLAGS = re.compile(r"profile\s+migrate\s+--")
_GREPS = frozenset({"grep", "egrep", "fgrep", "rg", "zgrep"})
# A sed `s` command: delimiter, regex, replacement (escapes kept).
_SED_SUBST = re.compile(r"(?:^|[;{\s'\"])s([^\w\s\\])((?:\\.|(?!\1).)*)\1((?:\\.|(?!\1).)*)\1")
LINEUP_SESSION_ID = re.compile(
    r"lineup\s+--session(?:=|\s+)(?!<runtime-id>|\"?\$\{?CLAUDE_CODE_SESSION_ID)(\S+)"
)

RULES_TERMS = frozenset(TERMS)
# Every public page: the shared rules.
RULES_MD = RULES_TERMS | {
    "secret-read", "machine-path", "profile-migrate-flags", "journal-auth", "lineup-session-id",
    "backend-command", "this-machine", "operator-skill",
}
# The user pages (README, SECURITY, CHANGELOG and everything under docs/)
# also refuse the extended machine facts and release lineage; the
# contributor pages (AGENTS, CONTRIBUTING) describe store paths and section
# numbers that would trip them.
RULES_USER_DOC = RULES_MD | {"machine-path-extended", "release-lineage"}
RULES_TEXT = RULES_TERMS | {"secret-read", "machine-path", "profile-migrate-flags", "journal-auth"}

# The five root pages (source-only: never installed).
ROOT_PAGES = ("README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md")
CONTRIBUTOR_PAGES = ("AGENTS.md", "CONTRIBUTING.md")
# The manuals the help pointers name (installed at the top of the documents).
MANUALS = ("USAGE.md", "CHEATSHEET.md", "STANDALONE.md")
# The documents' installed directory and the public repository, through
# whose URL an installed page reaches a root page.
PUBLIC_REPOSITORY = "https://github.com/nkoturovic/claude-multi"


def docs_pages(docs: Path = DOCS_DIR) -> tuple[str, ...]:
    """Every Markdown page under ``docs/`` (nested ones included), as its
    path below ``docs/``, sorted."""

    if not docs.is_dir():
        return ()
    return tuple(sorted(path.relative_to(docs).as_posix() for path in docs.rglob("*.md") if path.is_file()))


def public_pages() -> tuple[str, ...]:
    """Every public page's label: the root pages, then every page under docs/."""

    return (*ROOT_PAGES, *docs_pages())


def is_user_page(label: str) -> bool:
    return label not in CONTRIBUTOR_PAGES


def page_rules(label: str) -> frozenset[str]:
    return frozenset(RULES_USER_DOC if is_user_page(label) else RULES_MD)


USER_DOCS = tuple(label for label in public_pages() if is_user_page(label))
PKG_DOCS = public_pages()
# Every public document: the link and outside-reference gates read all of them.
PUBLIC_DOCS = PKG_DOCS


# ------------------------------------------------------------------ results


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str
    text: str
    intentional: str = ""  # the reason, for an intentional R1–R6 hit
    report_only: str = ""  # the reason, when the target is report-only for this rule

    @property
    def failing(self) -> bool:
        return not self.intentional and not self.report_only

    def format(self) -> str:
        suffix = ""
        if self.intentional:
            suffix = f"  [intentional: {self.intentional}]"
        elif self.report_only:
            suffix = f"  [report-only: {self.report_only}]"
        return f"{self.path}:{self.line}: {self.rule}: {self.text}{suffix}"


@dataclass(frozen=True)
class Unit:
    """One Markdown block (or one plain-text line) with its heading stack."""

    kind: str  # heading | prose | item | row | code | front | line
    start: int  # 1-based number of lines[0]
    lines: tuple[str, ...]
    headings: tuple[str, ...] = ()
    front_key: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def line_of(self, pos: int) -> int:
        return self.start + self.text.count("\n", 0, pos)

    def physical(self, pos: int) -> str:
        return self.lines[self.text.count("\n", 0, pos)]


# ---------------------------------------------------------------- parsing

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ITEM = re.compile(r"^\s{0,12}(?:[-*+]|\d{1,3}[.)])\s+\S")
_ROW = re.compile(r"^\s*\|")
_FRONT_KEY = re.compile(r"^([A-Za-z_][\w-]*):")


def parse_markdown(text: str) -> list[Unit]:
    lines = text.split("\n")
    units: list[Unit] = []
    stack: list[tuple[int, str]] = []
    heads = lambda: tuple(h for _, h in stack)  # noqa: E731
    index = 0
    if lines and lines[0].strip() == "---":
        key = ""
        index = 1
        while index < len(lines) and lines[index].strip() != "---":
            match = _FRONT_KEY.match(lines[index])
            if match:
                key = match[1]
            if lines[index].strip():
                units.append(Unit("front", index + 1, (lines[index],), (), key))
            index += 1
        index += 1
    current: list[str] = []
    kind = ""
    start = 0

    def flush() -> None:
        nonlocal current, kind
        if current:
            units.append(Unit(kind, start, tuple(current), heads()))
        current, kind = [], ""

    fence: str | None = None
    while index < len(lines):
        line = lines[index]
        number = index + 1
        index += 1
        if fence is not None:
            if line.strip().startswith(fence):
                flush()
                fence = None
                continue
            if not current:
                kind, start = "code", number
            current.append(line)
            continue
        fence_match = _FENCE.match(line)
        if fence_match:
            flush()
            fence = fence_match[1][0] * 3
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            level = len(heading[1])
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading[2]))
            units.append(Unit("heading", number, (line,), heads()))
            continue
        if not line.strip():
            flush()
            continue
        if _ROW.match(line):
            flush()
            units.append(Unit("row", number, (line,), heads()))
            continue
        if _ITEM.match(line):
            flush()
            kind, start = "item", number
            current.append(line)
            continue
        if not current:
            kind, start = "prose", number
        current.append(line)
    flush()
    return units


def parse_lines(text: str) -> list[Unit]:
    return [Unit("line", n, (line,)) for n, line in enumerate(text.split("\n"), 1) if line.strip()]


def _cells(row: str) -> list[tuple[int, int]]:
    """Cell spans of a table row: split on unescaped `|` outside code spans."""

    cells: list[tuple[int, int]] = []
    begin, i, tick = 0, 0, 0
    while i < len(row):
        ch = row[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            run = len(row[i:]) - len(row[i:].lstrip("`"))
            tick = 0 if tick == run else (run if tick == 0 else tick)
            i += run
            continue
        if ch == "|" and not tick:
            cells.append((begin, i))
            begin = i + 1
        i += 1
    cells.append((begin, len(row)))
    return cells


def code_spans(text: str) -> list[tuple[int, int, str]]:
    """Inline code spans ``(start, end, content)`` of a (joined) block text."""

    spans: list[tuple[int, int, str]] = []
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] != "`":
            i += 1
            continue
        run = len(text[i:]) - len(text[i:].lstrip("`"))
        close = text.find("`" * run, i + run)
        while close != -1 and close + run < len(text) and text[close + run] == "`":
            close = text.find("`" * run, close + run + 1)
        if close == -1:
            i += run
            continue
        content = text[i + run : close].replace("\n", " ")
        if run > 1:
            content = content.strip()
        spans.append((i, close + run, content))
        i = close + run
    return spans


def command_contexts(unit: Unit) -> list[tuple[int, str]]:
    """``(offset, text)`` pieces that are commands a reader might run.

    A fenced line ending in ``\\`` continues on the next line: the lines are
    joined (one space) into one command at the first line's offset.
    """

    if unit.kind in ("code", "line"):
        out: list[tuple[int, str]] = []
        offset, pending, pending_at = 0, "", -1
        for line in unit.lines:
            if pending_at < 0:
                pending_at = offset
            pending = f"{pending} {line.strip()}" if pending else line
            offset += len(line) + 1
            if unit.kind == "code" and pending.endswith("\\") and not pending.endswith("\\\\"):
                pending = pending[:-1].rstrip()
                continue
            out.append((pending_at, pending))
            pending, pending_at = "", -1
        if pending:
            out.append((pending_at, pending))
        return out
    return [(start, content) for start, _end, content in code_spans(unit.text)]


# ------------------------------------------------------- intentional (R1-R6)


@functools.lru_cache(maxsize=None)
def _module_tree(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (OSError, SyntaxError, ValueError):
        return None


# Test modules whose definitions count as package code for the *code
# identifier* class: ``_v3.py`` holds the 2.x validator moved out of
# ``src`` (AGENTS §3 names ``validate_composition`` as gone from the catalog).
CODE_IDENTIFIER_TEST_HELPERS = ("tests/_v3.py",)


@functools.lru_cache(maxsize=1)
def _package_words() -> frozenset[str]:
    """Identifiers the package source *defines or uses as code* (``src/``
    plus ``CODE_IDENTIFIER_TEST_HELPERS``; comments and prose docstrings do
    not count): names, attributes, def/class names, parameters, keyword
    names, and identifier-shaped string constants (record/JSON field names
    such as ``composition_name``)."""

    words: set[str] = set()
    helpers = [REPO_ROOT / rel for rel in CODE_IDENTIFIER_TEST_HELPERS]
    for path in [*sorted(SRC_ROOT.rglob("*.py")), *helpers]:
        tree = _module_tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                words.add(node.id)
            elif isinstance(node, ast.Attribute):
                words.add(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                words.add(node.name)
            elif isinstance(node, ast.arg):
                words.add(node.arg)
            elif isinstance(node, ast.keyword) and node.arg:
                words.add(node.arg)
            elif isinstance(node, ast.alias):
                words.add((node.asname or node.name).split(".")[0])
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.fullmatch(r"[A-Za-z_]\w*", node.value):
                    words.add(node.value)
    return frozenset(words)


def _top_level_names(body: Iterable[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.If, ast.Try)):
            names |= _top_level_names(node.body)
            names |= _top_level_names(getattr(node, "orelse", []))
            for handler in getattr(node, "handlers", []):
                names |= _top_level_names(handler.body)
    return names


def _module_attribute_exists(module: str, attrs: Sequence[str]) -> bool:
    """``module.attrs[0][.attrs[1]]`` is defined in ``SRC/<module>.py``: the
    first attribute at module top level, a second one in that class's body
    (deeper parts are not checked)."""

    tree = _module_tree(SRC_ROOT / f"{module}.py")
    if tree is None:
        return False
    if not attrs:
        return True
    if attrs[0] not in _top_level_names(tree.body):
        return False
    if len(attrs) == 1:
        return True
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == attrs[0]:
            return attrs[1] in _top_level_names(node.body)
    return True  # a function/constant: the rest is a call result or attribute, not checked


def _code_shaped(token: str) -> bool:
    return "_" in token.strip("_") or re.search(r"[a-z][A-Z]", token) is not None or (
        token.isupper() and len(token) > 3
    )


def _dotted_reference(token: str) -> tuple[str, list[str]] | None:
    """``(module, attributes)`` when ``token`` is ``[claude_multi.]<module>.<attr>…``
    naming an existing ``SRC/<module>.py``; ``None`` otherwise."""

    token = token.strip("./").rstrip(",;:")
    if not re.fullmatch(r"[a-z_]+(?:\.[A-Za-z_]\w*)+", token):
        return None
    parts = token.split(".")
    if parts[0] == "claude_multi":
        parts = parts[1:]
    if not parts or not (SRC_ROOT / f"{parts[0]}.py").is_file():
        return None
    return parts[0], parts[1:]


def _package_path_exists(token: str) -> bool:
    token = token.strip("./").rstrip(",;:")
    if not token or token.startswith("~") or "/" not in token and "." not in token:
        return False
    # Inline code paths are repository-relative, package-relative, or —
    # by the docs' convention for ``catalog/…``, ``schemas/…``,
    # ``version.json`` — relative to the packaged resources. Markdown links
    # keep their document-relative resolution (link_problems).
    for base in (REPO_ROOT, SRC_ROOT, REPO_ROOT / "src", RESOURCES_ROOT):
        candidate = base / token
        with contextlib.suppress(OSError, ValueError):
            if candidate.exists():
                return True
    reference = _dotted_reference(token)
    return reference is not None and _module_attribute_exists(*reference)


def _code_identifier(unit: Unit, pos: int, end: int) -> bool:
    for start, stop, content in code_spans(unit.text):
        if not start < pos < stop:
            continue
        run = len(unit.text[start:]) - len(unit.text[start:].lstrip("`"))
        inner = max(0, min(pos - start - run, len(content)))
        left = inner
        while left > 0 and re.match(r"[A-Za-z0-9_./-]", content[left - 1]):
            left -= 1
        right = inner
        while right < len(content) and re.match(r"[A-Za-z0-9_./-]", content[right]):
            right += 1
        token = content[left:right]
        if _package_path_exists(token):
            return True
        if _dotted_reference(token) is not None:
            return False  # `<module>.<attr>` whose attribute the module does not define
        ident_left = inner
        while ident_left > 0 and re.match(r"\w", content[ident_left - 1]):
            ident_left -= 1
        ident_right = inner
        while ident_right < len(content) and re.match(r"\w", content[ident_right]):
            ident_right += 1
        ident = content[ident_left:ident_right]
        return bool(ident) and _code_shaped(ident) and ident in _package_words()
    return False


def _wiki_slug(unit: Unit, pos: int) -> bool:
    if unit.kind == "front" and unit.front_key == "related":
        return True
    for match in re.finditer(r"\[\[[^\]\n]*\]\]", unit.text):
        if match.start() <= pos < match.end():
            return True
    return False


def _marker_context(unit: Unit, pos: int, end: int) -> str:
    text = unit.text
    low, high = 0, len(text)
    if unit.kind == "row":
        for start, stop in _cells(text):
            if start <= pos < stop:
                low, high = start, stop
                break
    return text[max(low, pos - MARKER_WINDOW) : min(high, end + MARKER_WINDOW)]


def _explicit_reason(
    unit: Unit, pos: int, label: str, explicit: Sequence[tuple[str, str, str]], used: set[int]
) -> str:
    for index, (glob, substring, reason) in enumerate(explicit):
        if not fnmatch.fnmatch(label, glob):
            continue
        for match in re.finditer(re.escape(substring), unit.text):
            if match.start() <= pos < match.end():
                used.add(index)
                return f"explicit: {reason}"
    return ""


def intentional_reason(
    unit: Unit,
    pos: int,
    end: int,
    *,
    label: str = "",
    explicit: Sequence[tuple[str, str, str]] = (),
    used: set[int] | None = None,
    markers: bool = True,
) -> str:
    """Why an R1–R6 hit is intentional, or ``""`` when it fails."""

    if explicit:
        reason = _explicit_reason(unit, pos, label, explicit, set() if used is None else used)
        if reason:
            return reason
    if not markers:
        return ""
    if _code_identifier(unit, pos, end):
        return "code identifier"
    if _wiki_slug(unit, pos):
        return "wiki slug"
    for heading in unit.headings:
        if HEADING_MARKERS.search(heading):
            return f"heading: {heading}"
    match = LINE_MARKERS.search(_marker_context(unit, pos, end))
    if match:
        return f"marker: {match[0]}"
    return ""


# ------------------------------------------------------------------- rules


def _excerpt(unit: Unit, pos: int) -> str:
    line = unit.physical(pos).strip()
    return line if len(line) <= 200 else line[:197] + "..."


def _blank(text: str, pattern: re.Pattern[str]) -> str:
    return pattern.sub(lambda m: " " * len(m[0]), text)


def term_hits(
    unit: Unit,
    *,
    label: str,
    terms: Iterable[str] = TERMS,
    explicit: Sequence[tuple[str, str, str]] = (),
    used: set[int] | None = None,
    markers: bool = True,
    blank: re.Pattern[str] | None = None,
) -> list[Hit]:
    text = unit.text if blank is None else _blank(unit.text, blank)
    hits: list[Hit] = []
    for name in terms:
        for match in TERMS[name].finditer(text):
            reason = intentional_reason(
                unit, match.start(), match.end(), label=label, explicit=explicit,
                used=used, markers=markers,
            )
            hits.append(Hit(label, unit.line_of(match.start()), name, _excerpt(unit, match.start()), reason))
    return hits


_PUNCT = frozenset("|&;()<>")


def shell_segments(command: str) -> list[list[str]]:
    """The simple commands of a shell line, as word lists (quotes kept whole).

    ``<x>`` placeholders become ``PH_x`` first (so ``<state>/bin/…`` is one
    word); a ``<`` redirection stays in its segment as the word ``"<"``.
    """

    text = _PLACEHOLDER.sub(lambda m: "PH_" + m[1].replace("-", "_"), command.replace("\\|", "|"))
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars="".join(sorted(_PUNCT)))
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = text.split()
    segments: list[list[str]] = []
    words: list[str] = []
    for token in tokens:
        if token and set(token) <= _PUNCT:
            if set(token) & (_PUNCT - {"<", ">"}):
                if words:
                    segments.append(words)
                words = []
            if "<" in token:
                words.append("<")
            continue
        words.append(token[:-1] if token.endswith("$") and len(token) > 1 else token)
    if words:
        segments.append(words)
    return segments


def _command_word(words: Sequence[str]) -> str:
    rest = [w for w in words if w != "<"]
    while rest and (rest[0] in _WRAPPERS or re.fullmatch(r"[A-Za-z_]\w*=.*", rest[0])):
        rest = rest[1:]
    return os.path.basename(rest[0]) if rest else ""


def _first_word(segment: str) -> str:
    segments = shell_segments(segment)
    return _command_word(segments[0]) if segments else ""


def _nested_segments(command: str, depth: int = 0) -> list[list[str]]:
    """``shell_segments`` plus, recursively, the segments of every quoted word
    that is itself a command line (``sh -c '…'``, ``ssh host '…'``, ``"$(…)"``)."""

    segments = shell_segments(command)
    if depth >= 3:
        return segments
    out = list(segments)
    for words in segments:
        for word in words:
            if word != command and re.search(r"\s|[|;&<>()]", word):
                out += _nested_segments(word, depth + 1)
    return out


def _helper_run(command: str, *, bare_is_run: bool) -> bool:
    """The token helper in command position (see ``_HELPER_RUN``); the bare
    form (the helper alone) counts only when ``bare_is_run``."""

    for match in _HELPER_RUN.finditer(command.replace("\\|", "|")):
        if bare_is_run or match["start"] is None or match["end"] is None:
            return True
    return False


def secret_read_problem(command: str, *, bare_helper_is_run: bool = False) -> str:
    """Why a command context reads a secret or runs the token helper, or ``""``.

    Beyond the ``R7_FLOOR`` regexes: a ``<`` redirection from a
    secret; any word of a shell segment (not only its command word, so
    wrappers with flags, ``timeout``, ``watch``, ``ssh`` count) that is a
    ``READ_COMMANDS`` entry followed later in that segment by a
    ``SECRET_NAMES`` word; quoted ``sh -c``/``bash -c``/``ssh`` bodies are
    parsed as command lines of their own; the token helper in command
    position (``_HELPER_RUN``).
    """

    if _HEADER_SECRET.search(command):
        return "sends a gateway or management secret in a header"
    if _SUBSHELL_READ.search(command) or any(p.search(command) for p in R7_FLOOR):
        return "reads a secret file"
    if _helper_run(command, bare_is_run=bare_helper_is_run):
        return f"runs {TOKEN_HELPER} (it prints the gateway token)"
    segments = shell_segments(command)
    is_command = sum(len(s) for s in segments) > 1 or len(segments) > 1
    if not is_command:
        return ""
    environ_names_only = bool(_ENV_NAMES_ONLY.fullmatch(command.strip()))
    for words in _nested_segments(command):
        for index, word in enumerate(words[:-1]):
            if word == "<" and SECRET_NAMES.search(words[index + 1]):
                return "redirects a secret file into a command"
        if not environ_names_only and any(_PROC_ENVIRON.search(w) for w in words) and (
            "<" in words or any(os.path.basename(w) in READ_COMMANDS for w in words)
        ):
            return "reads a process environment (the gateway's holds MANAGEMENT_PASSWORD)"
        first = _command_word(words)
        for index, word in enumerate(words[:-1]):
            name = os.path.basename(word) if "/" in word.rstrip("/") else word
            if name == "." and name != first:
                continue  # `find . -name api-key` lists a name; only `. file` sources
            if name in READ_COMMANDS and any(SECRET_NAMES.search(w) for w in words[index + 1 :]):
                return f"`{name}` on a secret file"
    return ""


def secret_hits(unit: Unit, *, label: str) -> list[Hit]:
    """R7 over one unit: the command contexts, then the floor on every line."""

    hits: list[Hit] = []
    for offset, command in command_contexts(unit):
        if _NEVER.search(unit.physical(offset)):
            continue
        fenced = unit.kind in ("code", "line")
        problem = secret_read_problem(command, bare_helper_is_run=fenced)
        if not problem and not fenced and _first_word(command) == TOKEN_HELPER:
            before = unit.text[:offset].rsplit("\n", 1)[-1]
            if _RUN_VERB.search(before):
                problem = f"tells the reader to run {TOKEN_HELPER}"
        if problem:
            hits.append(Hit(label, unit.line_of(offset), "secret-read", f"{problem}: {command.strip()[:160]}"))
    # The spec's R7 regexes are the floor on every physical line (plain
    # prose included); a line a command context already hit is not repeated.
    seen = {h.line for h in hits}
    for index, line in enumerate(unit.lines):
        number = unit.start + index
        if number in seen or _NEVER.search(line):
            continue
        if any(p.search(line) for p in R7_FLOOR):
            hits.append(Hit(label, number, "secret-read", f"reads a secret (spec R7): {line.strip()[:160]}"))
    return hits


def backend_command_hits(unit: Unit, *, label: str, before: Unit | None = None) -> list[Hit]:
    """R8: a service-manager command names its backend and never the earlier unit."""

    if unit.kind in ("heading", "front"):
        return []
    text = unit.text
    command = SERVICE_COMMAND.search(text)
    if not command:
        return []
    hits = []
    for match in UNIT_NAME.finditer(text):
        hits.append(Hit(label, unit.line_of(match.start()), "backend-command",
                        f"the earlier unit name; use the public gateway commands: {_excerpt(unit, match.start())}"))
    context = text + ("\n" + before.text if unit.kind == "code" and before is not None else "")
    if not BACKEND_NAME.search(context) and not any(BACKEND_NAME.search(h) for h in unit.headings):
        hits.append(Hit(label, unit.line_of(command.start()), "backend-command",
                        f"a service-manager command without its backend: {_excerpt(unit, command.start())}"))
    return hits


def this_machine_hits(unit: Unit, *, label: str) -> list[Hit]:
    """R13: "this machine" only inside a quotation of launcher text."""

    if unit.kind == "front":
        return []
    quoted = [(match.start(), match.end()) for match in FRAGMENT.finditer(unit.text)]
    return [
        Hit(label, unit.line_of(match.start()), "this-machine", _excerpt(unit, match.start()))
        for match in THIS_MACHINE.finditer(unit.text)
        if not any(start <= match.start() < end for start, end in quoted)
    ]


def operator_skill_hits(unit: Unit, *, label: str) -> list[Hit]:
    pattern = operator_skills()
    if pattern is None:
        return []
    return [Hit(label, unit.line_of(m.start()), "operator-skill", _excerpt(unit, m.start()))
            for m in pattern.finditer(unit.text)]


def release_lineage_hits(unit: Unit, *, label: str) -> list[Hit]:
    return [Hit(label, unit.line_of(m.start()), "release-lineage", _excerpt(unit, m.start()))
            for m in RELEASE_LINE.finditer(unit.text)]


def machine_path_hits(unit: Unit, *, label: str, extended: bool = False) -> list[Hit]:
    hits = []
    # Same length, so positions (and line numbers) stay.
    text = _PUBLIC_REPOSITORY_ADDRESS.sub(lambda match: "_" * len(match[0]), unit.text)
    for pattern in (*_identifier_patterns(), *(MACHINE_PATH_EXTENDED if extended else ())):
        for match in pattern.finditer(text):
            hits.append(Hit(label, unit.line_of(match.start()), "machine-path", _excerpt(unit, match.start())))
    return hits


def migrate_flag_hits(unit: Unit, *, label: str) -> list[Hit]:
    return [
        Hit(label, unit.line_of(m.start()), "profile-migrate-flags", _excerpt(unit, m.start()))
        for m in PROFILE_MIGRATE_FLAGS.finditer(unit.text)
    ]


def journal_problem(command: str) -> str:
    """Why a command context prints raw journal lines (``auth=`` ids), or ``""``."""

    segments = shell_segments(command)
    for index, words in enumerate(segments):
        if _command_word(words) != "journalctl":
            continue
        safe, selected = False, False
        for stage in segments[index + 1 :]:
            verdict = _journal_stage(stage)
            if verdict == "selects-auth":
                safe, selected = False, True
            elif verdict in ("safe", "count"):
                safe = True
        if safe:
            return ""
        if selected:
            return "selects auth= values (grep -o / sed -n …p / grep on auth): count them, or drop them"
        return "prints raw journal lines (auth= ids): count, or filter auth out"
    return ""


def _journal_stage(words: Sequence[str]) -> str:
    """One pipeline stage after ``journalctl``, for R11.

    ``count``: the output is numbers only (``grep -c``, ``wc``);
    ``safe``: the lines no longer carry ``auth=`` (``grep -v`` on auth, a
    ``sed`` substitution whose pattern consumes the auth *value*
    (``auth=[^ ]+``, ``auth_index=[^)]*``) without back-referencing it, a
    ``grep -o`` whose every pattern is confined to one non-auth field —
    ``_grep_o_confined``); ``selects-auth``: the stage keeps or extracts
    auth (``grep -o``/``sed -n …p``/a plain ``grep`` or a non-masking
    ``sed`` naming auth); ``""``: no change (``sort``, ``uniq -c``,
    ``grep -F 'x'``, ``head``, a ``grep -o`` that can span the auth field
    such as ``'session-affinity.*'`` or ``'[a-z_]+=[^ ]+'`` …).
    """

    rest = [w for w in words if w != "<"]
    while rest and (rest[0] in _WRAPPERS or re.fullmatch(r"[A-Za-z_]\w*=.*", rest[0])):
        rest = rest[1:]
    command, args = (os.path.basename(rest[0]), rest[1:]) if rest else ("", [])
    names_auth = any("auth" in w for w in args)
    if command == "wc":
        return "count"
    if command in _GREPS:
        short = "".join(w[1:] for w in args if re.fullmatch(r"-[A-Za-z]+", w))
        long = {w.split("=", 1)[0] for w in args if w.startswith("--")}
        if "c" in short or "--count" in long:
            return "count"
        if "v" in short or "--invert-match" in long:
            return "safe" if names_auth else ""
        if "o" in short or "--only-matching" in long:
            if names_auth:
                return "selects-auth"
            return "safe" if _grep_o_confined(args) else ""
        return "selects-auth" if names_auth else ""
    if command == "sed":
        substitutions = [m for w in args for m in _SED_SUBST.finditer(w)]
        masks = [
            m for m in substitutions
            if _AUTH_VALUE.search(m[2]) and not re.search(r"(?<!\\)&|\\[1-9]", m[3])
        ]
        if masks:
            return "safe"
        return "selects-auth" if names_auth else ""
    return ""


# A sed mask must consume the auth value, not just the key
# (`s/auth=//p` prints the value): `auth…=` then a repeated class, `\S` or `.`.
_AUTH_VALUE = re.compile(r"auth\w*=(?:\[[^\]]+\]|\\S|\.)[+*]")
# A grep -o pattern confined to one field: a literal key (no class, no
# wildcard, never an auth-field suffix) then `=` and a repeated class that excludes a space
# (`[^ ]`, `[^ )]`, `[a-z0-9.-]`) or `\S`; or a plain literal.
_GREP_O_FIELD = re.compile(
    r"[A-Za-z0-9_-]+=(?:\[\^[^\]]* [^\]]*\]|\[[^\]^\s\\]+\]|\\S)[+*]"
)
_GREP_O_LITERAL = re.compile(r"[\w :,-]+")
_GREP_VALUE_OPTIONS = frozenset({"-m", "-A", "-B", "-C", "-f", "--max-count", "--file"})


def _grep_o_confined(args: Sequence[str]) -> bool:
    """True iff every ``grep -o`` pattern matches within one non-auth field."""

    patterns: list[str] = []
    positional: list[str] = []
    it = iter(args)
    for word in it:
        if word in ("-e", "--regexp"):
            patterns.append(next(it, ""))
        elif word.startswith("--regexp="):
            patterns.append(word.split("=", 1)[1])
        elif word in _GREP_VALUE_OPTIONS:
            next(it, None)
        elif word.startswith("-") and word != "-":
            continue
        else:
            positional.append(word)
    if not patterns and positional:
        patterns = positional[:1]
    return bool(patterns) and all(
        "auth" not in p.lower() and (
            (_GREP_O_FIELD.fullmatch(p)
             and not any(field.endswith(p.split("=", 1)[0].lower()) for field in ("auth", "auth_index", "auth_file", "auth_id")))
            or _GREP_O_LITERAL.fullmatch(p)
        )
        for p in patterns
    )


def journal_hits(unit: Unit, *, label: str) -> list[Hit]:
    hits = []
    for offset, command in command_contexts(unit):
        if _NEVER.search(unit.physical(offset)):
            continue
        problem = journal_problem(command.replace("\\|", "|"))
        if problem:
            hits.append(Hit(label, unit.line_of(offset), "journal-auth", f"{problem}: {command.strip()[:160]}"))
    return hits


def lineup_session_hits(unit: Unit, *, label: str) -> list[Hit]:
    return [
        Hit(label, unit.line_of(m.start()), "lineup-session-id",
            f"lineup --session takes <runtime-id>, not {m[1]!r}: {_excerpt(unit, m.start())}")
        for m in LINEUP_SESSION_ID.finditer(unit.text.replace("\n", " "))
    ]


# ---------------------------------------------------------------- targets


@dataclass(frozen=True)
class Target:
    """One scanned text: a file, a rendered --help page or a docstring."""

    label: str
    kind: str  # markdown | text
    rules: frozenset[str]
    path: Path | None = None
    text: str | None = None
    line_filter: str | None = None
    report_only: Mapping[str, str] | None = None  # rule -> reason
    blank: re.Pattern[str] | None = None
    markers: bool = True
    explicit: tuple[tuple[str, str, str], ...] = ()

    def read(self) -> str:
        text = self.text if self.text is not None else self.path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        if self.line_filter:
            keep = re.compile(self.line_filter)
            text = "\n".join(line if keep.search(line) else "" for line in text.split("\n"))
        return text


def scan_target(target: Target, *, used: set[int] | None = None) -> list[Hit]:
    text = target.read()
    units = parse_markdown(text) if target.kind == "markdown" else parse_lines(text)
    rules = target.rules
    hits: list[Hit] = []
    previous: Unit | None = None
    terms = [name for name in TERMS if name in rules]
    for unit in units:
        label = target.label
        if terms:
            hits += term_hits(
                unit, label=label, terms=terms, explicit=target.explicit, used=used,
                markers=target.markers, blank=target.blank,
            )
        if "secret-read" in rules:
            hits += secret_hits(unit, label=label)
        if "backend-command" in rules:
            hits += backend_command_hits(unit, label=label, before=previous)
        if "this-machine" in rules:
            hits += this_machine_hits(unit, label=label)
        if "operator-skill" in rules:
            hits += operator_skill_hits(unit, label=label)
        if "release-lineage" in rules:
            hits += release_lineage_hits(unit, label=label)
        if "machine-path" in rules or "machine-path-extended" in rules:
            hits += machine_path_hits(unit, label=label, extended="machine-path-extended" in rules)
        if "profile-migrate-flags" in rules:
            hits += migrate_flag_hits(unit, label=label)
        if "journal-auth" in rules:
            hits += journal_hits(unit, label=label)
        if "lineup-session-id" in rules and target.kind == "markdown":
            hits += lineup_session_hits(unit, label=label)
        if unit.kind != "code":
            previous = unit
    if target.report_only:
        hits = [
            Hit(h.path, h.line, h.rule, h.text, h.intentional, target.report_only.get(h.rule, ""))
            for h in hits
        ]
    return hits


def in_tree_targets(*, include_help: bool = True) -> list[Target]:
    """The in-tree set: every public page, the rendered --help pages, the
    gateway and maintainer tools' help texts, and the source phrases."""

    targets: list[Target] = []
    for name in public_pages():
        path = doc_path(name)
        if path.is_file():
            targets.append(Target(name, "markdown", page_rules(name), path, explicit=EXPLICIT))
    if include_help:
        for prog, text in help_pages():
            targets.append(Target(f"<help: {prog}>", "text", RULES_TEXT, text=text, blank=HELP_ALIAS_TOKENS, explicit=EXPLICIT))
        for label, text in module_help_texts():
            targets.append(Target(label, "text", RULES_TEXT, text=text, explicit=EXPLICIT))
    for path in sorted(SRC_ROOT.rglob("*.py")):
        targets.append(
            Target(
                path.relative_to(REPO_ROOT).as_posix(), "text", frozenset(SOURCE_PHRASE_TERMS),
                path, markers=False,
            )
        )
    return targets


def budget_label(label: str) -> str:
    """Help pages share one budget (``<help>``); every other label is its own."""

    return "<help>" if label.startswith("<help: ") else label


def intentional_counts(hits: Iterable[Hit]) -> dict[str, int]:
    """Intentional R1–R6 hits per budget label (report-only hits never count)."""

    counts: dict[str, int] = {}
    for hit in hits:
        if hit.intentional and hit.rule in TERMS and not hit.report_only:
            key = budget_label(hit.path)
            counts[key] = counts.get(key, 0) + 1
    return counts


def budget_failures(hits: Iterable[Hit], budgets: Mapping[str, int] = BUDGETS) -> list[str]:
    counts = intentional_counts(hits)
    return [
        f"{key}: {count} intentional hit(s), budget {budgets.get(key, 0)}"
        for key, count in sorted(counts.items())
        if count > budgets.get(key, 0)
    ]


def explicit_problems(explicit: Sequence[tuple[str, str, str]], used: set[int]) -> list[str]:
    problems = []
    if len(explicit) > MAX_EXPLICIT:
        problems.append(f"EXPLICIT has {len(explicit)} entries (at most {MAX_EXPLICIT})")
    for index, (glob, substring, _reason) in enumerate(explicit):
        if index not in used:
            problems.append(f"stale EXPLICIT entry: {glob} {substring!r}")
    return problems


# ---------------------------------------------------------- path guard

# A path the checker is asked to scan is refused, before any read, when it
# names transcripts or a secret.


def _home(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("HOME") or str(Path.home()))


def claude_config_dir(environ: Mapping[str, str]) -> Path:
    value = environ.get("CLAUDE_CONFIG_DIR")
    return Path(value) if value else _home(environ) / ".claude"


def _guarded_resolve(path: Path, stop: set[Path], *, max_links: int = 40, islink=os.path.islink) -> Path:
    """``realpath`` without ever lstat'ing at or below a ``stop`` root.

    ``path`` is absolute and normalised. Components are resolved one at a
    time; a prefix that is (or lies under) a ``stop`` root is returned at
    once with the unresolved rest appended, before it is touched. A symlink
    target's parts are pushed back and resolved the same way.
    """

    pending = list(path.parts[1:])
    resolved = Path(path.anchor or "/")
    links = 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            resolved = resolved.parent
            continue
        candidate = resolved / part
        if any(candidate == root or root in candidate.parents for root in stop):
            return candidate.joinpath(*pending) if pending else candidate
        try:
            is_link = islink(candidate)
        except OSError:
            is_link = False
        if not is_link:
            resolved = candidate
            continue
        links += 1
        if links > max_links:
            return candidate.joinpath(*pending) if pending else candidate
        target = Path(os.readlink(candidate))
        if target.is_absolute():
            resolved = Path(target.anchor)
        pending[:0] = [p for p in target.parts if p != target.anchor]
    return resolved


def forbidden_reason(path: Path, environ: Mapping[str, str] = os.environ) -> str:
    """Why the checker must not read ``path`` (exit 2), or ``""``.

    Decided on the name alone, before any open: transcripts
    (``~/.claude/projects``, ``$CLAUDE_CONFIG_DIR/projects``, ``*.jsonl``)
    and secrets (the gateway token files, ``config.yaml``, the secret dir,
    the OAuth store, ``.credentials.json``, the token helper).
    """

    home = _home(environ)
    lexical = Path(os.path.abspath(os.path.expanduser(str(path))))
    # Lexical first. Then symlinks are followed one component at a time
    # (``_guarded_resolve``), and resolution stops at the first prefix that
    # is a projects or secret root: nothing below one is ever lstat'ed, and
    # nothing at or below a projects root (only its parent goes through
    # realpath; a secret root itself may, as before).
    projects = {
        Path(os.path.abspath(claude_config_dir(environ) / "projects")),
        Path(os.path.abspath(home / ".claude" / "projects")),
    }
    secret_roots = {
        Path(os.path.abspath(home / ".config" / "secrets")),
        Path(os.path.abspath(home / ".local" / "share" / "claude-multi")),
    }
    candidates = [lexical]
    if not any(lexical == root or root in lexical.parents for root in projects):
        projects |= {Path(os.path.realpath(root.parent)) / "projects" for root in list(projects)}
        secret_roots |= {Path(os.path.realpath(root)) for root in list(secret_roots)}
        candidates.append(_guarded_resolve(lexical, projects | secret_roots))
    for candidate in candidates:
        for root in projects:
            if candidate == root or root in candidate.parents:
                return "under ~/.claude/projects (transcripts are never read)"
        if candidate.suffix == ".jsonl":
            return "a .jsonl file (transcripts are never read)"
        for root in secret_roots:
            if candidate == root or root in candidate.parents:
                return "a secret store (names only)"
        # state.atomic_write stages a slot as ``.<name>.<rand>.tmp`` next to
        # it; after a crash that temp file can hold the whole secret.
        staged = candidate.name[1:] if candidate.name.startswith(".") and candidate.name.endswith(".tmp") else ""
        if candidate.name.startswith("management-key") or staged.startswith(
            ("management-key.", "api-key.", "previous-key.")
        ) or candidate.name in {
            "api-key", "previous-key", ".credentials.json", TOKEN_HELPER,
        } or (
            candidate.name == "config.yaml" and candidate.parent.name == "claude-multi"
        ):
            return "a secret file (names only)"
    return ""


# -------------------------------------------------------- the --help walk


def _walk(parser: argparse.ArgumentParser) -> Iterable[argparse.ArgumentParser]:
    seen: set[int] = set()
    todo = [parser]
    while todo:
        current = todo.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for action in current._actions:  # noqa: SLF001 - argparse has no public walk
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                todo.extend(action.choices.values())


@contextlib.contextmanager
def _columns(columns: int):
    saved = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = str(columns)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = saved


def help_pages(columns: int = 200) -> list[tuple[str, str]]:
    """``(prog, format_help())`` for every claude-multi parser and subparser.

    The installation root (where the help's doc pointers live) is replaced
    by ``<installation>``: the dev checkout path must not fail
    R9, the store path is not a machine fact.
    ANSI colour is stripped: Python 3.14's argparse colours help when the
    environment forces colour (``FORCE_COLOR``, as in the Nix sandbox), and
    the escapes would break the ``HELP_ALIAS_TOKENS`` word boundaries.
    """

    from claude_multi import cli, layout

    installation = layout.installation()
    root = str(installation) if installation is not None else None
    with _columns(columns):
        pages = [(p.prog, _ANSI.sub("", p.format_help())) for p in _walk(cli.build_parser())]
    return [(prog, text.replace(root, "<installation>") if root else text) for prog, text in pages]


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def help_epilog() -> str:
    from claude_multi import cli

    return cli.build_parser().epilog or ""


def module_help_texts() -> list[tuple[str, str]]:
    """What ``claude-multi-proxy --help`` and ``claude-multi-dev`` print."""

    from claude_multi import dev, proxy

    return [
        ("<doc: claude-multi-proxy --help>", proxy.__doc__ or ""),
        ("<doc: claude-multi-dev (bare)>", dev.__doc__ or ""),
        ("<doc: claude-multi-dev --help>", dev.DEV_HELP),
    ]


# ----------------------------------------------------- command spans

# The commands a span may name. ``claude-gateway`` (a launcher that no
# longer exists; ``claude-multi direct`` launches one model) is extracted
# only to be refused.
COMMAND_PREFIXES = ("claude-multi-proxy ", "claude-multi-dev ", "claude-multi ", "claude-gateway ", "/cm")
REMOVED_LAUNCHER = re.compile(r"(?<![\w-])claude-gateway(?![\w-])")
UUIDS = {
    "id": "11111111-1111-4111-8111-111111111111",
    "runtime-id": "22222222-2222-4222-8222-222222222222",
    "fork-id": "33333333-3333-4333-8333-333333333333",
}


@dataclass(frozen=True)
class Placeholder:
    """A typed placeholder of a complete example: what it stands for and
    the sample value the example is checked with."""

    kind: str
    sample: str


# The finite placeholder registry: ``<name>`` in a span must be one of
# these (an unknown placeholder fails), or a grammar fragment.
PLACEHOLDERS: dict[str, Placeholder] = {
    "id": Placeholder("a managed session id", UUIDS["id"]),
    "managed-id": Placeholder("a managed session id", UUIDS["id"]),
    "uuid": Placeholder("a managed session id", UUIDS["id"]),
    "runtime-id": Placeholder("the runtime session id Claude Code runs a session under", UUIDS["runtime-id"]),
    "fork-id": Placeholder("a native fork's runtime id", UUIDS["fork-id"]),
    "name": Placeholder("a profile name", "balanced"),
    "profile": Placeholder("a profile name", "balanced"),
    "new": Placeholder("a new profile name", "balanced-copy"),
    "model": Placeholder("a model line", "opus"),
    "line": Placeholder("a model line", "opus"),
    "agent": Placeholder("an agent id", "implementer"),
    "effort": Placeholder("an effort", "high"),
    "alias": Placeholder("a gateway continuity alias", "claude-multi-example-alias"),
    "provider": Placeholder("a provider id", "kimi"),
    "draft": Placeholder("a catalog draft name", "draft"),
    "repo": Placeholder("a source checkout", "/tmp/cm-repo"),
    "path": Placeholder("a file path", "/tmp/cm-profile.json"),
    "file": Placeholder("a file", "/tmp/cm-file.json"),
    "dir": Placeholder("a directory", "/tmp/cm-dir"),
    "root": Placeholder("an absolute state directory", "/tmp/cm-state-root"),
    "wire": Placeholder("an upstream model id", "example-wire-1"),
    "new-id": Placeholder("a new line key", "example-line"),
    "n": Placeholder("a number", "1"),
    "url": Placeholder("a URL", "http://proxy.example:3128"),
    "range": Placeholder("a git revision range", "HEAD~1..HEAD"),
    "nonce": Placeholder("a gateway instance id", "0123456789abcdef"),
    "ceiling": Placeholder("a context window ceiling", "400K"),
    "when": Placeholder("an instant or a duration", "24h"),
    "text": Placeholder("free text", "example text"),
    "step": Placeholder("a setup step", "claude"),
    "preset": Placeholder("a provider preset", "zai"),
    "secret-ref": Placeholder("a key reference by name", "env:EXAMPLE_API_KEY"),
    "key-name": Placeholder("the name a key is saved under", "EXAMPLE_API_KEY"),
    "header": Placeholder("a key header", "x-api-key"),
    "family": Placeholder("a model family", "example"),
    "contracts": Placeholder("payload contracts", "output-config-high"),
    "unit": Placeholder("a service unit name", "claude-multi-gateway"),
}
# Grammar fragments: typed placeholders only a grammar span carries (it
# ends with one), checked up to where it stands.
FRAGMENTS: dict[str, str] = {
    "command": "any command of the command before it",
    "options": "any options of the command before it",
    "request": "a lineup request (the /cm grammar)",
    "claude-args": "arguments Claude Code receives unchanged (after --)",
}
ENV_PLACEHOLDERS = {
    "CLAUDE_MULTI_MANAGED_ID": UUIDS["id"],
    "CLAUDE_MULTI_SESSION_ID": UUIDS["id"],
    "CLAUDE_CODE_SESSION_ID": UUIDS["runtime-id"],
}
# Negative examples: a span a page shows as refused, with the refusal it
# must meet: (page, span) -> a fragment of the expected problem. Every
# other span must parse.
NEGATIVE_EXAMPLES: dict[tuple[str, str], str] = {}
# Pages whose command spans name commands rather than give complete
# examples: each span's command chain must exist.
NAME_ONLY_PAGES = {"CHANGELOG.md": "release notes name the commands; the guides give their complete forms"}

_PLACEHOLDER = re.compile(r"<([a-z][a-z0-9-]*)>", re.I)
_ELLIPSIS = re.compile(r"…|\.\.\.")
_TYPED_REPEAT = re.compile(r"(<[a-z][a-z0-9-]*>)(?:…|\.\.\.)", re.I)
_ALT_SEPARATOR = re.compile(r"\s+·\s+")
_METAVAR = re.compile(r"[A-Z][A-Z0-9_]+")
_SELECTOR_SUFFIX = "[1m]"
MAX_FORMS = 256


def command_spans(text: str) -> list[tuple[int, str, str]]:
    """``(line, span, physical line)`` for every command span in a Markdown text.

    Spans are backtick spans and fenced-code lines that start with a
    ``COMMAND_PREFIXES`` entry; a span holding several ``·``-separated
    commands (``lineup.HELP_LINE``) yields each one. Fenced lines ending in
    ``\\`` are joined with the next (``command_contexts``), so a wrapped
    command is one span, reported at its first line.
    """

    out: list[tuple[int, str, str]] = []
    for unit in parse_markdown(text):
        for offset, context in command_contexts(unit):
            piece = context.strip()
            if unit.kind == "code":
                piece = re.sub(r"\s+#\s.*$", "", piece)
                piece = re.sub(r"^\$\s+", "", piece)
            for part in _ALT_SEPARATOR.split(piece):
                part = part.strip()
                if part == "/cm" or part.startswith(COMMAND_PREFIXES):
                    out.append((unit.line_of(offset), part, unit.physical(offset)))
    return out


@dataclass(frozen=True)
class _Group:
    """``[a | b]`` (optional) or ``(a | b)`` (one of them required): each
    alternative a sequence of text and nested groups."""

    required: bool
    alternatives: tuple[tuple["str | _Group", ...], ...]


_CLOSE = {"[": "]", "(": ")"}
_SPACED_PIPE = re.compile(r"\s+\|\s+")


def _closing(text: str, start: int) -> int | None:
    """The index of the bracket closing ``text[start]`` (``[1m]`` inside is
    part of a selector), or None."""

    depth = 0
    index = start
    while index < len(text):
        if text.startswith(_SELECTOR_SUFFIX, index):
            index += len(_SELECTOR_SUFFIX)
            continue
        char = text[index]
        if char in _CLOSE:
            depth += 1
        elif char in _CLOSE.values():
            depth -= 1
            if depth == 0:
                return index if char == _CLOSE[text[start]] else None
        index += 1
    return None


def _top_level_split(text: str) -> list[str]:
    """``text`` split at its spaced pipes outside any bracket."""

    parts, depth, last, index = [], 0, 0, 0
    while index < len(text):
        if text.startswith(_SELECTOR_SUFFIX, index):
            index += len(_SELECTOR_SUFFIX)
            continue
        char = text[index]
        if char in _CLOSE:
            depth += 1
        elif char in _CLOSE.values():
            depth -= 1
        elif depth == 0 and (match := _SPACED_PIPE.match(text, index)) and text[index].isspace():
            parts.append(text[last:index])
            last = index = match.end()
            continue
        index += 1
    return [*parts, text[last:]]


def _parse_groups(text: str) -> tuple["str | _Group", ...]:
    """``text`` as literal text and groups: ``[…]`` is optional (``[1m]``
    is part of a selector), ``(… | …)`` is a required choice; a ``(…)``
    without a spaced pipe is literal text."""

    items: list[str | _Group] = []
    literal, index = "", 0
    while index < len(text):
        if text.startswith(_SELECTOR_SUFFIX, index):
            literal += _SELECTOR_SUFFIX
            index += len(_SELECTOR_SUFFIX)
            continue
        char = text[index]
        end = _closing(text, index) if char in _CLOSE else None
        if end is not None:
            inner = text[index + 1:end]
            alternatives = _top_level_split(inner)
            if char == "[" or len(alternatives) > 1:
                if literal:
                    items.append(literal)
                    literal = ""
                items.append(_Group(char == "(", tuple(_parse_groups(alt) for alt in alternatives)))
                index = end + 1
                continue
        literal += char
        index += 1
    if literal:
        items.append(literal)
    return tuple(items)


def _render(items: Sequence["str | _Group"], chosen: Mapping[int, str]) -> str:
    """The text of ``items`` with group ``i`` written as ``chosen[i]`` and
    every other group as its default (optional: left out; required: its
    first alternative's default)."""

    out = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            out.append(item)
        elif index in chosen:
            out.append(chosen[index])
        elif item.required:
            out.append(_render(item.alternatives[0], {}))
    return "".join(out)


def _forms(items: Sequence["str | _Group"]) -> list[str]:
    forms = [_render(items, {})]
    for index, item in enumerate(items):
        if isinstance(item, str):
            continue
        for alternative in item.alternatives:
            forms += [_render(items, {index: form}) for form in _forms(alternative)]
    return forms


def optional_forms(text: str) -> list[str]:
    """Every form of a span, linearly: every group at its default (optional
    groups left out, required choices at their first alternative), then each
    group in turn with each of its alternatives (nested groups the same way)
    while every other group keeps its default. ``[a | b]`` is an optional
    choice, ``(a | b)`` a required one. At most :data:`MAX_FORMS`."""

    forms = sorted({" ".join(form.split()) for form in _forms(_parse_groups(text))})
    if len(forms) > MAX_FORMS:
        raise ValueError(f"more than {MAX_FORMS} forms: write fewer forms per span")
    return forms


def _variants(words: list[str]) -> list[list[str]]:
    """Each ``a|b`` word's alternatives, linearly: every word at its first
    alternative, then each word in turn at each of its other ones."""

    choices = [word.split("|") if "|" in word and not word.startswith("|") else [word] for word in words]
    base = [options[0] for options in choices]
    variants = [base]
    for index, options in enumerate(choices):
        variants += [[*base[:index], option, *base[index + 1:]] for option in options[1:]]
    return variants


def _quiet_parse(parser: argparse.ArgumentParser, argv: list[str]) -> str:
    err = io.StringIO()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            lines = [line for line in err.getvalue().splitlines() if line.strip()]
            return lines[-1] if lines else f"argparse exit {exc.code}"
    return ""


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:  # noqa: SLF001
    return next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)  # noqa: SLF001


def _chain_problem(parser: argparse.ArgumentParser, words: list[str]) -> str:
    """Name mode: every leading command word names a real subparser."""

    current = parser
    for word in words:
        if word.startswith("-"):
            continue
        sub = _subparsers(current)
        if sub is None:
            return ""
        if word not in sub.choices:
            return f"unknown subcommand {word!r} (prefix {current.prog!r})"
        current = sub.choices[word]
    return ""


def _command_node(parser: argparse.ArgumentParser, words: list[str]) -> tuple[argparse.ArgumentParser | None, str]:
    """The parser the leading command words reach (options skipped)."""

    current = parser
    for word in words:
        if word.startswith("-"):
            continue
        sub = _subparsers(current)
        if sub is None:
            return current, ""
        if word not in sub.choices:
            return None, f"unknown subcommand {word!r} (prefix {current.prog!r})"
        current = sub.choices[word]
    return current, ""


def _dev_problem(words: list[str], *, prefix: bool) -> str:
    from claude_multi import dev

    if not words:
        return "" if prefix else "bare claude-multi-dev prints usage and exits 2"
    command, rest = words[0], words[1:]
    if command in ("-h", "--help", "help"):
        return ""
    known = {"model", "provider", "drafts", "check", "review", "promote", "smoke-test", "probe", "repin"}
    if command not in known:
        return f"unknown claude-multi-dev command {command!r}"
    if prefix:
        return ""
    if command == "probe":
        head = rest[: rest.index("--")] if "--" in rest else rest
        positionals, flags = dev._parse_flags(head)  # noqa: SLF001 - the parser's own rule
        if "allow-local-claude" not in flags or not isinstance(flags.get("fixture-root"), str):
            return "probe requires --allow-local-claude and --fixture-root PATH"
        if positionals[:1] not in (["init"], ["run"]):
            return "probe takes init|run"
        if positionals[0] == "run" and "native-contract" not in flags:
            return "probe run requires --native-contract FILE"
        return ""
    positionals, flags = dev._parse_flags(rest)  # noqa: SLF001
    if command == "repin":
        unknown = sorted(set(flags) - {"repo", "manifest-dir", "settings-keys"})
        if positionals or unknown:
            return "repin takes [--repo PATH] [--manifest-dir DIR] [--settings-keys FILE]"
        return ""
    if command in ("model", "provider"):
        if positionals[:1] != ["add"]:
            return f"{command} takes add"
        if command == "model" and "like" in flags:
            return "" if "id" in flags and "wire-id" in flags else "model add --like requires --id and --wire-id"
        return "" if "from-json" in flags else f"{command} add requires --from-json FILE"
    if command == "drafts":
        return "" if positionals == ["migrate"] else "drafts takes migrate"
    if not positionals:
        return f"{command} requires a DRAFT name" if command != "smoke-test" else "smoke-test requires a MODEL"
    if command == "promote" and ("repo" in flags) == ("patch-output" in flags):
        return "promote requires exactly one of --repo PATH / --patch-output FILE"
    return ""


def _proxy_problem(words: list[str], *, prefix: bool) -> str:
    """A ``claude-multi-proxy`` form against the tool's own command table:
    each command's argument check, which has no effect."""

    from claude_multi import proxy

    if not words or (words[0] in ("-h", "--help", "help", "-v", "--version") and len(words) == 1):
        return ""
    commands = {command.name: command for command in proxy.PROXY_COMMANDS}
    descriptor = commands.get(words[0])
    if descriptor is None:
        return f"unknown claude-multi-proxy command {words[0]!r}"
    args = words[1:]
    if prefix or args[:1] and args[0] in proxy.HELP_ARGUMENTS:
        return ""
    try:
        descriptor.check(args, {"HOME": "/nonexistent-cm-docs-home"})
    except proxy.ProxyError as exc:
        return f"claude-multi-proxy refuses: {exc}"
    return ""


def _cm_problem(words: list[str], *, fragment: str | None) -> str:
    """``/cm <request>`` exactly as the skill passes it: one argument to
    ``claude-multi lineup --session <runtime-id>``."""

    from claude_multi import lineup

    if fragment is not None:
        return "" if not words and fragment == "request" else f"/cm takes a <request>, not <{fragment}>"
    argv = ["--session", UUIDS["runtime-id"], *([" ".join(words)] if words else [])]
    try:
        lineup.parse_cli(argv, {})
    except lineup.LineupRefusal as exc:
        return f"/cm refuses: {exc}"
    return ""


def _lineup_problem(words: list[str], *, fragment: str | None) -> str:
    from claude_multi import lineup

    if words in (["--help"], ["-h"]):
        return ""  # the request grammar, before any session is read
    if fragment is not None:
        if fragment != "request":
            return f"claude-multi lineup takes a <request>, not <{fragment}>"
        words = [*words, "show"]
    try:
        # Inside a session the runtime id comes from the environment.
        lineup.parse_cli(words, {"CLAUDE_CODE_SESSION_ID": UUIDS["runtime-id"]})
    except lineup.LineupRefusal as exc:
        return f"lineup refuses: {exc}"
    return ""


def _options_problem(parser: argparse.ArgumentParser, words: list[str]) -> str:
    """``<options>``: the command exists and every option written before
    the fragment is one of its options."""

    node, problem = _command_node(parser, words)
    if problem:
        return problem
    if node is None or (sub := _subparsers(node)) is not None and sub.required:
        return "<options> follows a command, not a group of commands"
    known = {option for action in node._actions for option in action.option_strings}  # noqa: SLF001
    unknown = [word for word in words if word.startswith("-") and word.split("=", 1)[0] not in known
               and word not in ("--line", "--no-color")]
    return f"unknown options before <options>: {' '.join(unknown)}" if unknown else ""


@functools.lru_cache(maxsize=1)
def _launcher_parser() -> argparse.ArgumentParser:
    """The launcher's parser, built once: parsing a form changes nothing in it."""

    from claude_multi import cli

    return cli.build_parser()


def _launcher_problem(words: list[str], *, fragment: str | None) -> str:
    from claude_multi.cli.parser import split_passthrough

    if words[:1] == ["lineup"]:
        return _lineup_problem(words[1:], fragment=fragment)
    parser = _launcher_parser()
    launcher, tail = split_passthrough(words)
    passthrough = "--" in words
    if fragment == "claude-args" and not passthrough:
        return "<claude-args> stands after --"
    if fragment in ("request",):
        return "<request> belongs to claude-multi lineup and /cm"
    if fragment == "command":
        node, problem = _command_node(parser, launcher)
        if problem:
            return problem
        return "" if node is not None and _subparsers(node) is not None else "no command follows here"
    if fragment == "options":
        return _options_problem(parser, launcher)
    problem = _quiet_parse(parser, launcher)
    if problem:
        return problem
    if passthrough or tail:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            command = parser.parse_args(launcher).command
        if command not in (None, "direct"):
            return "arguments after -- reach Claude Code only from a launch or claude-multi direct"
    return ""


@dataclass(frozen=True)
class SpanCheck:
    """One span's verdict: ``problem`` ("" passes), ``kind`` (complete,
    fragment, name or negative) and how many forms were parsed."""

    problem: str
    kind: str
    forms: int


def _substitute(words: list[str]) -> tuple[list[str], str]:
    out: list[str] = []
    for word in words:
        def placeholder(match: re.Match[str]) -> str:
            key = match[1].lower()
            if key in PLACEHOLDERS:
                return PLACEHOLDERS[key].sample
            raise KeyError(match[0])

        if _METAVAR.fullmatch(word):
            return out, f"an untyped metavar {word}: write a typed placeholder (<{word.lower().replace('_', '-')}>)"
        try:
            word = _PLACEHOLDER.sub(placeholder, word)
        except KeyError as exc:
            return out, f"unknown placeholder {exc.args[0]}"
        for name, value in ENV_PLACEHOLDERS.items():
            word = re.sub(r"\"?\$\{?" + name + r"\}?\"?", value, word)
        out.append(word)
    return out, ""


def _form_problem(form: str, *, mode: str) -> tuple[str, str]:
    """``(problem, kind)`` of one form (no optional group left)."""

    if mode == "name":
        form = _ELLIPSIS.split(form, 1)[0]
    elif _ELLIPSIS.search(form):
        return ("an untyped ellipsis: write the complete command, or end it with a typed fragment ("
                + ", ".join(f"<{name}>" for name in FRAGMENTS) + ")"), "complete"
    try:
        words = shlex.split(form)
    except ValueError as exc:
        return f"unbalanced quoting: {exc}", "complete"
    if not words:
        return "an empty command", "complete"
    fragment = None
    fragments = [index for index, word in enumerate(words) if word.lower().strip("<>") in FRAGMENTS
                 and word.startswith("<") and word.endswith(">")]
    if fragments:
        if fragments != [len(words) - 1]:
            return "a grammar fragment ends the span, once", "fragment"
        fragment = words.pop().strip("<>").lower()
    kind = "name" if mode == "name" else "fragment" if fragment else "complete"
    for variant in _variants(words):
        substituted, problem = _substitute(variant)
        if problem and mode != "name":
            return problem, kind
        if problem:
            substituted = [word for word in variant if not word.startswith("<")]
        problem = _head_problem(substituted, mode=mode, fragment=fragment)
        if problem:
            return f"{problem} (in: {' '.join(variant)})", kind
    return "", kind


def _head_problem(words: list[str], *, mode: str, fragment: str | None) -> str:
    head, rest = words[0], words[1:]
    prefix = mode == "name"
    if head == "claude-gateway":
        return "claude-gateway is not a command of this release (claude-multi direct launches one model)"
    if head == "/cm":
        return "" if prefix else _cm_problem(rest, fragment=fragment)
    if head == "claude-multi-proxy":
        return _proxy_problem(rest, prefix=prefix or fragment is not None)
    if head == "claude-multi-dev":
        return _dev_problem(rest, prefix=prefix or fragment is not None)
    if head != "claude-multi":
        return f"not a claude-multi command: {head}"
    if prefix:
        from claude_multi import cli

        return "" if rest[:1] == ["lineup"] else _chain_problem(cli.build_parser(), rest)
    return _launcher_problem(rest, fragment=fragment)


def check_span(span: str, *, mode: str = "example") -> SpanCheck:
    """Check one command span: every form of it (each optional group left
    out and put in, each ``a|b`` alternative) parses with the parser that
    owns it, typed placeholders replaced by their samples; a grammar
    fragment parses up to its typed fragment; ``mode="name"`` checks only
    that the command chain exists.

    Rules: ``\\|`` is a pipe; inside ``[…]`` or ``(…)`` a spaced `` | ``
    separates alternatives; outside them, `` | `` followed by a shell filter
    is a pipe (only the command before it parses) and any other spaced
    `` | `` is refused (one span per form); ``<x>…`` is a typed repetition,
    any other ellipsis is refused; ``&&``/``;``/``||`` separate commands.
    """

    text = span.replace("\\|", "|").strip().rstrip(".,;:")
    if REMOVED_LAUNCHER.search(text):
        return SpanCheck("claude-gateway is not a command of this release (claude-multi direct launches one model)",
                         "complete", 0)
    forms_checked, kinds = 0, set()
    for command in re.split(r"\s*(?:&&|;|\|\|)\s*", text):
        command = command.strip()
        if not (command == "/cm" or command.startswith(COMMAND_PREFIXES)):
            continue
        parts = _top_level_split(command)
        if len(parts) > 1:
            if all(_first_word(part) in _SHELL_FILTERS for part in parts[1:]):
                command = parts[0]
            else:
                return SpanCheck("spaced alternatives (a | b): write one span per form with its operands",
                                 "complete", forms_checked)
        command = _TYPED_REPEAT.sub(r"\1", command)
        try:
            forms = optional_forms(command)
        except ValueError as exc:
            return SpanCheck(str(exc), "complete", forms_checked)
        for form in forms:
            forms_checked += 1
            problem, kind = _form_problem(form, mode=mode)
            kinds.add(kind)
            if problem:
                return SpanCheck(problem, kind, forms_checked)
    kind = "fragment" if "fragment" in kinds else "name" if "name" in kinds else "complete"
    return SpanCheck("", kind, forms_checked)


def span_problem(span: str, line: str = "") -> str:
    """Why a complete command span does not parse, or ``""`` (see :func:`check_span`)."""

    return check_span(span).problem


def page_check(label: str, span: str) -> SpanCheck:
    """:func:`check_span` for a span of the page ``label``: a name-only page
    checks names, a negative example must be refused as recorded."""

    if label in NAME_ONLY_PAGES:
        return check_span(span, mode="name")
    expected = NEGATIVE_EXAMPLES.get((label, span))
    result = check_span(span)
    if expected is None:
        return result
    if not result.problem:
        return SpanCheck(f"a negative example that parses (it must be refused: {expected})", "negative", result.forms)
    if expected not in result.problem:
        return SpanCheck(f"refused for another reason than {expected!r}: {result.problem}", "negative", result.forms)
    return SpanCheck("", "negative", result.forms)


def page_span_problem(label: str, span: str, line: str = "") -> str:
    """:func:`page_check`'s problem, or ``""``."""

    return page_check(label, span).problem


def span_coverage(pages: Mapping[str, str]) -> dict[str, int]:
    """Coverage counts over ``label -> text``: pages with spans, spans by
    kind, parsed forms and problems."""

    counts = {"pages": 0, "spans": 0, "complete": 0, "fragment": 0, "name": 0, "negative": 0, "forms": 0,
              "problems": 0}
    for label, text in pages.items():
        spans = command_spans(text)
        counts["pages"] += bool(spans)
        for _line, span, _physical in spans:
            result = page_check(label, span)
            counts["spans"] += 1
            counts[result.kind] += 1
            counts["forms"] += result.forms
            counts["problems"] += bool(result.problem)
    return counts


# ---------------------------------------------------------- fragments

FRAGMENT = re.compile(r"“([^”]+)”")
FRAGMENT_MIN_CHUNK = 10


@functools.lru_cache(maxsize=1)
def source_literals() -> tuple[str, ...]:
    """Every string literal of ``SRC/*.py`` (``Constant`` and ``JoinedStr`` parts), whitespace-normalised."""

    found: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(" ".join(node.value.split()))
    return tuple(sorted(found))


def fragment_problems(text: str) -> list[tuple[int, str, str]]:
    """``(line, fragment, problem)`` for “…” fragments not found in the source.

    A fragment is split at ``<…>`` placeholders and ``…``; every chunk of at
    least ``FRAGMENT_MIN_CHUNK`` characters must be a substring of one
    string literal (whitespace normalised on both sides; JoinedStr constant
    parts are ``Constant`` nodes, so ``ast.walk`` sees them), and at least
    one chunk must be that long.
    """

    literals = source_literals()
    problems: list[tuple[int, str, str]] = []
    for unit in parse_markdown(text):
        for match in FRAGMENT.finditer(unit.text):
            fragment = " ".join(match[1].split())
            chunks = [c.strip() for c in re.split(r"<[^<>]+>|…", fragment)]
            long = [c for c in chunks if len(c) >= FRAGMENT_MIN_CHUNK]
            line = unit.line_of(match.start())
            if not long:
                problems.append((line, fragment, f"no chunk of {FRAGMENT_MIN_CHUNK}+ characters"))
                continue
            for chunk in long:
                if not any(chunk in literal for literal in literals):
                    problems.append((line, fragment, f"not in any source literal: {chunk!r}"))
    return problems


# --------------------------------------------------------------- links

_LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


# Material that is not part of this repository: a public document never
# points into it, by link or by path.
PRIVATE_REFERENCE = re.compile(r"(?<![\w./-])(?:\.\./)*(?:docs/design/|maintainer/)")


def private_references(text: str) -> list[tuple[int, str]]:
    """``(line, text)`` for every reference to material outside the repository."""

    return [(text.count("\n", 0, match.start()) + 1, match.group(0)) for match in PRIVATE_REFERENCE.finditer(text)]


def heading_slug(heading: str) -> str:
    """The anchor a repository host gives a heading: code marks and link
    targets dropped, lower case, punctuation removed, spaces as dashes."""

    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.replace("`", ""))
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


@functools.lru_cache(maxsize=None)
def heading_anchors(path: Path) -> frozenset[str]:
    """Every heading anchor of a Markdown file (a repeated heading gets -1, -2, …)."""

    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for unit in parse_markdown(path.read_text(encoding="utf-8")):
        if unit.kind != "heading":
            continue
        slug = heading_slug(unit.headings[-1])
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return frozenset(anchors)


_REPOSITORY_FILE = re.compile(re.escape(PUBLIC_REPOSITORY) + r"/blob/main/([^#?\s]+)(?:#(\S+))?$")


def link_problems(path: Path, *, outside_only: bool = False, root: Path = REPO_ROOT, docs: Path | None = None,
                  repository: Path | None = None) -> list[tuple[int, str, str]]:
    """``(line, target, problem)`` for every Markdown link that does not
    resolve: a relative link must reach a file inside ``root`` (a link that
    leaves the repository is a problem even when its target exists), its
    ``#fragment`` must be a heading of that file (an anchor-only link: of
    this file), a page under ``docs`` (default ``root/docs``) reaches files
    outside it only by the public repository URL (installed pages have no
    root pages next to them), a public repository URL must name a file of
    ``repository`` (default ``root``) and its heading, and no link names a
    machine-local path."""

    problems: list[tuple[int, str, str]] = []
    docs = root / "docs" if docs is None else docs
    repository = root if repository is None else repository
    under_docs = docs == path.parent or docs in path.parents
    for unit in parse_markdown(path.read_text(encoding="utf-8")):
        if unit.kind == "code":
            continue
        for match in _LINK.finditer(unit.text):
            target = match[1]
            line = unit.line_of(match.start())
            if target.startswith(("file:", "/")) or re.match(r"[A-Za-z]:\\", target):
                problems.append((line, target, "a machine-local path"))
                continue
            public = _REPOSITORY_FILE.match(target)
            if public:
                if not outside_only and not (repository / public[1]).exists():
                    problems.append((line, target, f"missing in the repository: {public[1]}"))
                elif not outside_only and public[2] and (repository / public[1]).suffix == ".md" \
                        and public[2] not in heading_anchors(repository / public[1]):
                    problems.append((line, target, f"no heading #{public[2]} in {public[1]}"))
                continue
            if re.match(r"[a-z][a-z0-9+.-]*:", target, re.I):
                continue
            file_part, _hash, fragment = target.partition("#")
            if not file_part:
                if not outside_only and fragment and fragment not in heading_anchors(path):
                    problems.append((line, target, f"no heading #{fragment} in this page"))
                continue
            resolved = Path(os.path.normpath(path.parent / file_part))
            inside = resolved == root or root in resolved.parents
            if outside_only and inside:
                continue
            if not inside:
                problems.append((line, target, f"outside the repository: {resolved}"))
            elif not resolved.exists():
                problems.append((line, target, f"missing: {resolved}"))
            elif under_docs and not (resolved == docs or docs in resolved.parents):
                problems.append((line, target, "an installed page reaches a root page only by its public URL "
                                                f"({PUBLIC_REPOSITORY}/blob/main/…)"))
            elif fragment and resolved.suffix == ".md" and fragment not in heading_anchors(resolved):
                problems.append((line, target, f"no heading #{fragment} in {resolved.name}"))
    return problems


def installed_link_problems(share: Path, *, repository: Path = REPO_ROOT) -> list[tuple[str, int, str, str]]:
    """``(page, line, target, problem)`` for every page of an installed
    document tree (``share/claude-multi`` of a wheel, a bundle or the Nix
    package): every relative link and ``#fragment`` resolves inside the tree,
    nested directories included, and a root page of the repository is
    reached only by its public URL (checked against ``repository``)."""

    out = []
    for path in sorted(share.rglob("*.md")):
        for line, target, problem in link_problems(path, root=share, docs=share, repository=repository):
            out.append((path.relative_to(share).as_posix(), line, target, problem))
    return out


def link_targets() -> list[Path]:
    """The documents the link and outside-reference gates read: every public page."""

    return [doc_path(name) for name in public_pages() if doc_path(name).is_file()]


# ------------------------------------------------------- drafting markers

# A fact a page cannot state yet (it lands with later work) is drafted as an
# HTML comment the rendered page does not show, naming who supplies it:
#   <!-- docs-todo(OWNER): what the page still needs -->
# The markers are tracked (test_operator_docs) and must be gone at release
# preflight. Any other placeholder text on a public page fails.
TODO_MARKER = re.compile(r"<!-- docs-todo\((?P<owner>[a-z]+)\): (?P<text>[^\n]*?) -->")
TODO_OWNERS = {
    "providers": "the provider routes, presets and model lines of the release",
    "ui": "the interface's final wording, keys and dialogs",
    "generate": "content generated from the parser, the operation matrix and the catalog",
    "release": "facts of the release candidate: evidence, signing, publication",
    "costs": "official provider cost and limit references with a checked date",
}
PLACEHOLDER = re.compile(r"\b(?:TODO|TBD|FIXME|XXX)\b|lorem ipsum|_to be (?:published|decided)_", re.I)


def todo_markers(text: str) -> list[tuple[int, str, str]]:
    """``(line, owner, text)`` for every drafting marker of a page."""

    return [(text.count("\n", 0, match.start()) + 1, match["owner"], match["text"])
            for match in TODO_MARKER.finditer(text)]


def placeholder_problems(text: str) -> list[tuple[int, str]]:
    """``(line, text)`` for placeholder text outside a well-formed drafting
    marker, and for a malformed marker (unknown owner, no text, not on lines
    of its own)."""

    problems: list[tuple[int, str]] = []
    spans = []
    for match in TODO_MARKER.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        before = text[text.rfind("\n", 0, match.start()) + 1: match.start()]
        after_end = text.find("\n", match.end())
        after = text[match.end(): len(text) if after_end == -1 else after_end]
        if match["owner"] not in TODO_OWNERS or not match["text"].strip():
            problems.append((line, f"a malformed marker: {match[0][:80]}"))
        elif before.strip() or after.strip():
            problems.append((line, f"a marker not on a line of its own: {match[0][:80]}"))
        spans.append((match.start(), match.end()))
    for match in PLACEHOLDER.finditer(text):
        if not any(start <= match.start() < end for start, end in spans):
            problems.append((text.count("\n", 0, match.start()) + 1, f"placeholder text: {match[0]}"))
    for match in re.finditer(r"docs-todo", text):
        if not any(start <= match.start() < end for start, end in spans):
            problems.append((text.count("\n", 0, match.start()) + 1, "a malformed marker"))
    return problems


# ----------------------------------------------------------- self test

# The model-in-name forms and real earlier agent names must hit; role ids must not.
MODEL_IN_NAME_POSITIVE = (
    "cm-*-<model>-<lane>",
    "cm-<role>-<model>-<lane>",
    "cm-*-sol-*",
    "cm-*-kimi-*",
    "cm-analyst-kimi-k3-max",
    "cm-implementer-kimi-k3-max",
    "cm-explorer-sol-high",
    "A `cm-*-sol-*` dispatch must produce",
)
MODEL_IN_NAME_NEGATIVE = (
    "cm-implementer-light",
    "cm-analyst-strong",
    "cm-reviewer-strong",
    "cm-implementer[-light|-strong]",
    "cm-lead",
    "cm-explorer",
)
SECRET_POSITIVE = (
    "`cat ~/.config/claude-multi/management-key`",
    "`head -c 64 ~/.config/claude-multi/management-key.next`",
    "`cp ~/.config/claude-multi/management-key* /tmp/backup/`",
    "`TOKEN=$(<~/.config/claude-multi/management-key)`",
    "`bash -c 'cat ~/.config/claude-multi/management-key.next'`",
    '`curl -H "X-Management-Key: $KEY" http://127.0.0.1:8317/v0/management/auth-files`',
    '`curl -H "x-management-key: ${KEY}" http://127.0.0.1:8317/v0/management/auth-files`',
    '`echo "$MANAGEMENT_PASSWORD"`',
    "`printenv MANAGEMENT_PASSWORD`",
    "`python3 -c 'import os; print(os.environ[\"MANAGEMENT_PASSWORD\"])'`",
    # The gateway process environment holds MANAGEMENT_PASSWORD.
    "```\ncat /proc/$PID/environ\n```",
    "```\ntr '\\0' '\\n' < /proc/$PID/environ\n```",
    "```\nsudo strings /proc/1234/environ\n```",
    "```\nxargs -0 -n1 < /proc/$PID/environ\n```",
    "```\nps eww $PID\n```",
    "`ps auxe`",
    "`x=$(< /proc/$PID/environ)`",
    "`python3 -c 'print(open(\"/proc/1/environ\").read())'`",
    "Then run cat /proc/$PID/environ to see it.",
    # The names-only exemption must not cover leaking pipelines.
    "```\ncat /proc/$PID/environ; printf foo=bar | cut -d= -f1\n```",
    "```\ntr '\\0' '\\n' < /proc/$PID/environ | tee /dev/stderr | cut -d= -f1\n```",
    "```\ntee /dev/stderr < /proc/$PID/environ | cut -d= -f1\n```",
    "Then run tr '\\0' '\\n' < /proc/1/environ | tee x | cut -d= -f1 to list them.",
    "`tr '=' ' ' </proc/1/environ | cut -d= -f1`",
    "```\nps -p \"$PID\" eww\n```",
    "`ps -o pid e`",
    "`cat -A ~/.config/claude-multi/api-key`",
    "`head -c 64 ~/.config/claude-multi/api-key`",
    "`tr -d '\\n' < ~/.config/claude-multi/api-key`",
    "`grep -c . ~/.config/claude-multi/previous-key`",
    "`wc -c ~/.config/claude-multi/api-key`",
    "`TOKEN=$(<~/.config/claude-multi/api-key)`",
    "`curl -H \"Authorization: Bearer $TOKEN\" http://127.0.0.1:8317/v1/models`",
    "`~/.local/state/claude-multi/bin/claude-multi-gateway-token | wc -c`",
    "To check the token, run `<state>/bin/claude-multi-gateway-token`.",
    "`python3 -c 'print(open(\"/home/u/.config/secrets/claude.env\").read())'`",
    # The spec floor on plain prose, any read word of a
    # segment, quoted -c bodies.
    "Then run cat ~/.config/claude-multi/api-key to compare.",
    "`bash -c 'cat ~/.config/claude-multi/api-key'`",
    "`sudo -u user cat ~/.config/claude-multi/api-key`",
    "`timeout 5 cat ~/.config/claude-multi/api-key`",
    "`watch cat ~/.config/claude-multi/previous-key`",
    "`ssh host cat ~/.config/claude-multi/api-key`",
    "```\nsh -c \"head -c 8 ~/.config/claude-multi/api-key\"\n```",
    "```\ncat \\\n  ~/.config/claude-multi/api-key\n```",
    # The helper run behind ;, &&, || and in a -c body.
    "```\n<state>/bin/claude-multi-gateway-token; echo\n```",
    "```\n~/.local/state/claude-multi/bin/claude-multi-gateway-token && echo ok\n```",
    "```\n<state>/bin/claude-multi-gateway-token || true\n```",
    "`bash -c '<state>/bin/claude-multi-gateway-token'`",
    "`x=$(<state>/bin/claude-multi-gateway-token)`",
    "`sudo -u user <state>/bin/claude-multi-gateway-token`",
    # The OAuth records and a quoted helper run on a fenced line.
    "`cat ~/.local/share/claude-multi/claude-me@x.json`",
    "`grep -h access_token ~/.local/share/claude-multi/*.json`",
    "`jq .access_token codex-me@x.json`",
    "```\n\"$STATE/bin/claude-multi-gateway-token\"\n```",
    "```\nx=$(\"$STATE/bin/claude-multi-gateway-token\")\n```",
)
SECRET_NEGATIVE = (
    "`stat -c %s ~/.config/claude-multi/management-key*`",
    "`find ~/.config/claude-multi -name 'management-key*'`",
    "`management-key*`, `MANAGEMENT_PASSWORD` and `X-Management-Key` are names only.",
    "Never print `MANAGEMENT_PASSWORD` or pass `X-Management-Key` by hand.",
    "`claude-multi-proxy rotate-management-key`",
    "`claude-multi-proxy disable-management-key`",
    "`readlink /proc/$PID/exe`",
    "```\ntr '\\0' '\\n' </proc/<pid>/environ | cut -d= -f1 | sort\n```",
    "`cat /proc/$PID/environ | tr '\\0' '\\n' | cut -d= -f1`",
    "A running process: `tr '\\0' '\\n' </proc/<pid>/environ | cut -d= -f1 | sort`.",
    "`ps -eo pid,user,etime`",
    "`ps aux | less`",
    "`ps -p \"$PID\" -o comm=`",
    "`/proc/<pid>/environ` by name/count",
    "`ps -eo pid,comm`",
    "`ps -e`",
    "Never read the gateway process environment (`/proc/<pid>/environ`, `ps e`).",
    "`stat -c %s ~/.config/claude-multi/api-key`",
    "Never `cat ~/.config/claude-multi/api-key`; names only.",
    "`api-key` and `previous-key` are secrets — names only.",
    "the scope `apiKeyHelper` is `<state>/bin/claude-multi-gateway-token`",
    "`ls -la ~/.config/claude-multi`",
    "`find ~/.config/claude-multi -name api-key`",
    "`bin/claude-multi-gateway-token` (apiKeyHelper shim)",
    "```\nbin/claude-multi-gateway-token (apiKeyHelper shim)\n```",
    "Sizes only: `stat -c %s ~/.config/claude-multi/api-key ~/.config/claude-multi/previous-key`.",
    "`ls -l ~/.local/share/claude-multi/pinned-clients/`",
    "`cmp ~/.local/share/claude-multi/pinned-clients/7.2.80 \"$(command -v claude)\"`",
    "`stat -c %s ~/.local/share/claude-multi/claude-me@x.json`",
    "the apiKeyHelper is `\"<state>/bin/claude-multi-gateway-token\"`",
)
# A unit named without a journal command is no journal read.
UNIT_MENTION = (
    "The supervised gateway is the systemd user unit `claude-multi-gateway`\n"
    "(`systemctl --user … claude-multi-gateway`); its log through `claude-multi gateway logs`.\n"
)
JOURNAL_POSITIVE = (
    "`journalctl --user -u claude-multi-gateway -n 50 -o cat`",
    "`journalctl --user -u claude-multi-gateway --since \"2 hours ago\" | grep selector.go`",
    # A stage that selects auth values is not a filter.
    "`journalctl --user -u claude-multi-gateway -o cat | grep -oE 'auth=[^ ]+'`",
    "`journalctl --user -u claude-multi-gateway -o cat | sed -n '/auth=/p'`",
    "`journalctl --user -u claude-multi-gateway -o cat | grep -oE 'auth=[^ ]+' | sort | uniq -c`",
    "`journalctl --user -u claude-multi-gateway -o cat | sort | uniq -c`",
    "`journalctl --user -u claude-multi-gateway -o cat | sed -E 's/.*(auth=[^ ]+).*/\\1/'`",
    # A sed that deletes only the key, and grep -o patterns
    # that can span the auth field, are not filters.
    "`journalctl --user -u claude-multi-gateway -o cat | sed -n 's/auth=//p'`",
    "`journalctl --user -u claude-multi-gateway -o cat | grep -o 'session-affinity.*'`",
    "`journalctl --user -u claude-multi-gateway -o cat | grep -oE '[a-z_]+=[^ ]+'`",
    "`journalctl --user -u claude-multi-gateway -o cat | grep -oE 'model=.*'`",
    # A bare journal mention is a command too.
    "systemd user unit `claude-multi-gateway` (`journalctl --user -u claude-multi-gateway`,",
)
JOURNAL_NEGATIVE = (
    "`journalctl --user -u claude-multi-gateway -n 50 -o cat \\| sed -E 's/ auth=[^ ]+//'`",
    "`journalctl --user -u claude-multi-gateway --since -2h -o cat | grep -F 'session-affinity:' | grep -oE 'model=[^ ]+' | sort | uniq -c`",
    "`journalctl --user -u claude-multi-gateway --since -24h -o cat | grep -F 'upstream served model' | sed -E 's/ \\(auth_index=[^)]*\\)//'`",
    "`journalctl --user -u claude-multi-gateway -o cat | sed -E 's/auth=[^ ]+/auth=<id>/'`",
    "`journalctl --user -u claude-multi-gateway -o cat | grep -v 'auth='`",
    "`journalctl --user -u claude-multi-gateway -o cat | grep -c 'auth='`",
    "`journalctl --user -u claude-multi-gateway -o cat | grep -oE 'auth=[^ ]+' | sort -u | wc -l`",
    "`journalctl --user -u claude-multi-gateway -o cat | grep -oE 'model=[a-z0-9.-]+' | sort | uniq -c`",
    "`journalctl --user -u claude-multi-gateway -o cat | grep -o -e 'provider=[^ ]*' | sort | uniq -c`",
    UNIT_MENTION,
)
BACKEND_POSITIVE = (
    "| restart | `systemctl --user restart cli-proxy-api` |",
    "| restart (systemd) | `systemctl --user restart cli-proxy-api` |",
    "Restart it: `systemctl --user restart claude-multi-gateway`.",
    "```\njournalctl --user -u claude-multi-gateway -n 20 | wc -l\n```",
)
BACKEND_NEGATIVE = (
    "With the systemd user service: `systemctl --user status claude-multi-gateway`.",
    "| restart | `claude-multi gateway restart` |",
    "The gateway binary is `libexec/claude-multi/cli-proxy-api`.",
    "Its licences are in `share/licenses/cli-proxy-api/`.",
)
SPAN_OK = (
    "claude-multi lineup --session <runtime-id> show",
    "claude-multi lineup --session <runtime-id> --relaunch --its-exited profile <name>",
    "claude-multi lineup --session <runtime-id> <request>",
    "claude-multi profile list|show",
    "claude-multi profile rename <name> <new>",
    "claude-multi doctor --repair-all [--include-live]",
    "claude-multi restore-2x [--not-running <id>…] [--assume-dead <id>…]",
    "claude-multi sessions mark-ended --all-dead",
    "claude-multi models | grep -F '<alias>'",
    "claude-multi gateway <command>",
    "claude-multi providers add <provider> <options>",
    "claude-multi models add <provider> <wire> <options>",
    "claude-multi lineup <request>",
    "claude-multi --profile <name> -- <claude-args>",
    "claude-multi direct --model <model> -- --verbose",
    "/cm set <agent>=<model>[:<effort>]",
    "/cm direct [<model>[:<effort>]]",
    "/cm fallback <provider> [--preview]",
    "/cm review [high-stakes] [<range>]",
    "/cm pin|follow",
    "/cm profile claude",
    "/cm",
    "claude-multi-proxy status",
    "claude-multi-proxy init --state-root <root>",
    "claude-multi-proxy snapshot-auth [--restore <file>]",
    "claude-multi-proxy --help",
    "claude-multi-dev model add --like <line> --id <new-id> --wire-id <wire> --name <draft>",
    "claude-multi-dev promote <draft> --repo <repo>",
    "claude-multi migrate --dry-run && claude-multi migrate",
    "claude-multi sessions mark-ended (<id> | --all-dead)",
    "claude-multi setup [--step <step>] [--proxy <url> | --no-proxy]",
    "claude-multi update [--check | --rollback] [--yes]",
    "claude-multi [-c | -r [<id>]] [-- <claude-args>]",
    "claude-multi providers remove-key <provider> --name <key-name>",
)
SPAN_BAD = (
    "claude-multi lineup --session <id> set",
    "claude-multi profile new",
    "claude-multi lineup --session <runtime-id> show | set | unset",
    "claude-multi lineup --session <runtime-id> …",
    "claude-multi providers remove-key …",
    "claude-multi profile show NAME",
    "claude-multi doctor --repair-all [--no-such-flag]",
    "claude-multi sessions list -- --verbose",
    "claude-multi gateway status <command>",
    "claude-multi gateway <command> extra",
    "claude-multi <request>",
    "claude-multi providers add <name> --bogus <options>",
    "claude-multi providers <options>",
    "claude-gateway -r <id>",
    "/cm bogus",
    "/cm direct [<model> <model>]",
    "claude-multi-dev review",
    "claude-multi-dev promote <draft>",
    "claude-multi-proxy restart",
    "claude-multi-proxy init --state-root relative/root",
    "claude-multi-proxy status --extra",
    "claude-multi sessions frobnicate <id>",
    "claude-multi sessions show <session>",
    "claude-multi sessions mark-ended (<id> | --bogus)",
    "claude-multi update [--check | --nope]",
    "claude-multi providers remove-key <provider> --name KEY_NAME",
)


def _fixture_hits(markdown: str, rules: Iterable[str] = RULES_USER_DOC, label: str = "fixture.md") -> list[Hit]:
    return scan_target(Target(label, "markdown", frozenset(rules), text=markdown))


def self_test() -> list[str]:
    """The rule unit cases; ``[]`` = pass."""

    failures: list[str] = []
    pattern = TERMS["model-in-name"]
    failures += [f"model-in-name misses {s!r}" for s in MODEL_IN_NAME_POSITIVE if not pattern.search(s)]
    failures += [f"model-in-name matches {s!r}" for s in MODEL_IN_NAME_NEGATIVE if pattern.search(s)]
    # A wrapped list item keeps its marker; a row is judged per cell
    # and within MARKER_WINDOW; a 2.x heading covers its section.
    wrapped = (
        "- 2.x aliases (intentional): `--composition[-file]`, `compose …`,\n"
        "  `show`, `sessions transition`, `sessions link --composition|--model`,\n"
        "  kept for 3.0.x.\n"
    )
    if any(h.failing for h in _fixture_hits(wrapped, RULES_TERMS)):
        failures.append("a wrapped list item lost its marker")
    far = "Intro 2.x. " + "x" * (MARKER_WINDOW + 20) + " then a composition word.\n"
    if not any(h.failing and h.rule == "composition" for h in _fixture_hits(far)):
        failures.append("a marker beyond MARKER_WINDOW still whitelisted a hit")
    row = "| 2.x alias | ok | the composition editor |\n"
    if not any(h.failing for h in _fixture_hits(row)):
        failures.append("a marker in another table cell whitelisted a hit")
    # Live 3.0 spellings of "retired"/"aliases" are not history.
    for text in (
        "The card shows retired keys for each line. Start a composition with Enter.\n",
        "Only `models.json` / `retired.json` / compositions are edited.\n",
        "Each retired key keeps its route; pick a composition.\n",
        "Doctor lists continuity aliases next to the composition.\n",
    ):
        if not any(h.failing and h.rule == "composition" for h in _fixture_hits(text)):
            failures.append(f"a live 3.0 word whitelisted a hit: {text.strip()!r}")
    for text in (
        "The composition picker was retired in 3.0.\n",
        "2.x aliases kept: `--composition`.\n",
    ):
        if any(h.failing for h in _fixture_hits(text, RULES_TERMS)):
            failures.append(f"a history marker no longer whitelisted a hit: {text.strip()!r}")
    section = "## History\n\nThe composition picker was replaced.\n\n## Now\n\nNo composition here.\n"
    hits = [h for h in _fixture_hits(section) if h.rule == "composition"]
    if [h.failing for h in hits] != [False, True]:
        failures.append(f"heading scope wrong: {[(h.line, h.failing) for h in hits]}")
    if any(h.failing for h in _fixture_hits("The module `composition.py` and `CompositionStore` stay.\n")):
        failures.append("a code identifier was not intentional")
    for text in ("Use `cli.compositions` now.\n", "Use `profile.composition_editor` now.\n"):
        if not any(h.failing for h in _fixture_hits(text)):
            failures.append(f"a dotted name the module does not define passed: {text.strip()!r}")
    if any(h.failing for h in _fixture_hits("Only `composition.CompositionError` stays.\n")):
        failures.append("a defined dotted name failed")
    if not any(h.failing for h in _fixture_hits("Run `claude-multi --composition default`.\n")):
        failures.append("a 2.x flag without a marker passed")
    front = "---\nrelated:\n  - claude-multi-composition-design\n---\n# Page\n"
    if any(h.failing for h in _fixture_hits(front)):
        failures.append("a wiki slug in related: failed")
    for text in SECRET_POSITIVE:
        if not any(h.rule == "secret-read" for h in _fixture_hits(text + "\n")):
            failures.append(f"secret-read misses {text!r}")
    for text in SECRET_NEGATIVE:
        if any(h.rule == "secret-read" for h in _fixture_hits(text + "\n")):
            failures.append(f"secret-read flags {text!r}")
    for text in JOURNAL_POSITIVE:
        if not any(h.rule == "journal-auth" for h in _fixture_hits(text + " (systemd)\n")):
            failures.append(f"journal-auth misses {text!r}")
    for text in JOURNAL_NEGATIVE:
        if any(h.rule == "journal-auth" for h in _fixture_hits(text + " (systemd)\n")):
            failures.append(f"journal-auth flags {text!r}")
    # R8: the earlier unit is refused even when labelled; the product unit
    # needs its backend named; the public gateway commands need nothing.
    for text in BACKEND_POSITIVE:
        if not any(h.rule == "backend-command" for h in _fixture_hits(text + "\n")):
            failures.append(f"backend-command misses {text!r}")
    for text in BACKEND_NEGATIVE:
        if any(h.rule == "backend-command" for h in _fixture_hits(text + "\n")):
            failures.append(f"backend-command flags {text!r}")
    for text in ("Run this on this machine.\n", "| fix | `claude-multi gateway restart` (this machine) |\n"):
        if not any(h.rule == "this-machine" for h in _fixture_hits(text)):
            failures.append(f"this-machine misses {text!r}")
    if any(h.rule == "this-machine" for h in _fixture_hits("doctor: “ask the policy owner, or use plain claude on this machine”\n")):
        failures.append("this-machine flags a quotation of launcher text")
    # A synthetic skill name stands in for the private list's.
    real = operator_skills
    globals()["operator_skills"] = lambda: re.compile(r"\bexample-ops-skill\b")
    try:
        for text in ("see skill example-ops-skill §6\n", "`example-ops-skill`\n"):
            if not any(h.rule == "operator-skill" for h in _fixture_hits(text)):
                failures.append(f"operator-skill misses {text!r}")
        if any(h.rule == "operator-skill" for h in _fixture_hits("the /cm skill\n")):
            failures.append("operator-skill flags the product's own skill")
    finally:
        globals()["operator_skills"] = real
    for text in ("kept for 3.0.x\n", "a 2.x record\n", "since 3.1\n", "the earlier launcher (2.26)\n"):
        if not any(h.rule == "release-lineage" for h in _fixture_hits(text)):
            failures.append(f"release-lineage misses {text!r}")
    for text in ("Claude Code 2.1.286\n", "CLIProxyAPI 7.3.15\n", "Python 3.11\n", "`restore-2x`\n",
                 "release 1.0.0\n", "TLS 1.3\n"):
        if any(h.rule == "release-lineage" for h in _fixture_hits(text)):
            failures.append(f"release-lineage flags {text!r}")
    if not any(h.rule == "lineup-session-id" for h in _fixture_hits("`claude-multi lineup --session <id> show`\n")):
        failures.append("lineup-session-id misses <id>")
    if any(h.rule == "lineup-session-id" for h in _fixture_hits("`claude-multi lineup --session <runtime-id> show`\n")):
        failures.append("lineup-session-id flags <runtime-id>")
    if not any(h.rule == "machine-path" for h in _fixture_hits("generation 143 is active\n")):
        failures.append("machine-path-extended misses a generation number")
    try:
        import claude_multi  # noqa: F401
    except ImportError:
        return failures + ["claude_multi is not importable: spans and fragments not checked"]
    failures += [f"span refused: {s!r}: {span_problem(s)}" for s in SPAN_OK if span_problem(s)]
    failures += [f"span accepted: {s!r}" for s in SPAN_BAD if not span_problem(s)]
    # A line that says "refused" exempts nothing: a negative example is
    # registered with the refusal it must meet.
    if not span_problem("claude-multi profile new", "`claude-multi profile new` is refused"):
        failures.append("a span on a line that says refused was skipped")
    forms = optional_forms("/cm direct [<model>[:<effort>]]")
    if forms != ["/cm direct", "/cm direct <model>", "/cm direct <model>:<effort>"]:
        failures.append(f"optional forms are not all checked: {forms}")
    if check_span("claude-multi doctor --repair-all [--include-live]").forms != 2:
        failures.append("both forms of an optional flag are not parsed")
    # Linear: every group at its default, then each group's alternatives in
    # turn (four forms for three optional flags, not eight).
    if optional_forms("claude-multi doctor [-v] [--first-run] [--preview]") != [
            "claude-multi doctor", "claude-multi doctor --first-run", "claude-multi doctor --preview",
            "claude-multi doctor -v"]:
        failures.append("optional groups are not expanded one at a time")
    if optional_forms("claude-multi x (a | b [c])") != ["claude-multi x a", "claude-multi x b", "claude-multi x b c"]:
        failures.append("a required choice is not expanded into each alternative")
    if check_span("claude-multi gateway <command>").kind != "fragment":
        failures.append("a typed fragment is not reported as a grammar fragment")
    if check_span("claude-multi gateway restart …", mode="name").problem:
        failures.append("a name-only span with a trailing ellipsis failed")
    wrapped_span = command_spans("```bash\nclaude-multi doctor \\\n  --repair-all\n```\n")
    if [(line, span) for line, span, _ in wrapped_span] != [(2, "claude-multi doctor --repair-all")] or any(
        span_problem(span) for _, span, _ in wrapped_span
    ):
        failures.append(f"a \\-continued fenced command was not joined: {wrapped_span}")
    failures += _forbidden_self_test()
    good = "doctor: “gateway did not reload — restart required”\n"
    if fragment_problems(good):
        failures.append(f"a real fragment failed: {fragment_problems(good)}")
    if not fragment_problems("“this sentence is not in the launcher source”\n"):
        failures.append("an invented fragment passed")
    return failures


def _forbidden_self_test() -> list[str]:
    """A symlink into a projects root is refused, and nothing inside the
    projects root is lstat'ed on the way."""

    import tempfile

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="cm-docs-vocab-") as tmp:
        root = Path(os.path.realpath(tmp))
        projects = root / "home" / ".claude" / "projects"
        (projects / "p").mkdir(parents=True)
        (root / "x").mkdir()
        (root / "x" / "p").symlink_to(projects)
        environ = {"HOME": str(root / "home")}
        if not forbidden_reason(root / "x" / "p" / "whatever.md", environ).startswith("under ~/.claude/projects"):
            failures.append("a symlink into ~/.claude/projects was not refused")
        touched: list[Path] = []

        def islink(candidate: Path) -> bool:
            touched.append(Path(candidate))
            return os.path.islink(candidate)

        resolved = _guarded_resolve(root / "x" / "p" / "p" / "whatever.md", {projects}, islink=islink)
        if resolved != projects / "p" / "whatever.md" or any(projects == t or projects in t.parents for t in touched):
            failures.append(f"_guarded_resolve touched the projects root: {resolved}, {touched}")
    return failures


def run_all(targets: Sequence[Target], *, explicit: Sequence[tuple[str, str, str]] = EXPLICIT) -> tuple[list[Hit], list[str]]:
    """Scan targets; returns hits and allowlist-hygiene problems for ``explicit``."""

    used: set[int] = set()
    hits: list[Hit] = []
    for target in targets:
        hits += scan_target(target, used=used)
    return hits, explicit_problems(explicit, used)
