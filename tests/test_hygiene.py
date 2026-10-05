"""Hygiene tests: import boundaries, credentials, personal identifiers,
origin narration and the POSIX seam.

- The credential gate (:class:`SecretScanTests`): every file of the working
  tree, ignored files included, carries no credential-looking value.
  Credentials belong outside the checkout.
- The identifier gate (:class:`IdentifierGateTests`): no personal
  identifier in the repository's files. The generic identifiers
  (``tools/history_scan.py`` ``GENERIC_IDENTIFIERS``: the home directory of
  a real account) always apply; the exact list of the people and computers
  behind the repository is a private input (``history_scan.
  PRIVATE_INPUT_ENV`` names the file) that adds its identifiers and their
  reviewed sites. Each remaining site is on :data:`IDENTIFIER_RATCHET` (or
  the private input's site list) with its owner, at its exact count.
- The origin gate (:class:`OriginNarrationTests`): the repository describes
  the product as it is. Origin narration is named by a project's own
  history (its tracking-id schemes, plan words and earlier tooling), so the
  patterns and the files that still carry some, with their ceilings, come
  from the same private input; without it the gate's mechanism runs on
  synthetic vocabulary (:data:`SYNTHETIC_PRIVATE`) and the tree scan is
  skipped.
- The naming gates read the repository's own files (``_layout.
  repository_files``: what git tracks or would add); ignored local output
  such as a virtual environment or a build directory is third-party or
  generated content (:class:`LocalEnvironmentTests`). The credential gate
  reads every file, ignored ones included.
- Verbatim third-party notices (the licence files ``gateway/licenses/
  modules.json`` lists with their sha256) are exempt from the identifier and
  origin gates, never from the credential gate (:class:`ThirdPartyNoticeTests`).
- The lineage gate (:class:`LineageWordingTests`): user-visible product
  text (messages, help, data and the goldens of product output) names the
  thing, never the release line it came from (:data:`LINEAGE_PATTERNS`:
  release labels, planning documents and unit ids); test file names carry
  no release or unit lineage either, except
  the files on :data:`LINEAGE_PATHS` with the pass that renames them.
- The operator-defaults review (:data:`OPERATOR_DEFAULTS`) and the ``fcntl``
  seam (:class:`PosixSeamTests`).

A ratchet owner names the pass that removes a site: ``onboarding`` (setup
and provider onboarding), ``service`` (the gateway service and endpoint),
``wording`` (product text: help, UI, messages, code comments and their
goldens), ``docs`` (the documentation and the non-shipped tree: tests,
tools, Nix, CI, the gateway recipe), ``keyed-audit`` (the keyed-route audit
evidence, regenerated for the release candidate), ``publication`` (the
gateway patch content, whose bytes are pinned identities); ``keep`` marks a
site that is not a defect.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import io
import json
import os
import re
import secrets
import tempfile
import tokenize
import unittest
from pathlib import Path

from _layout import LICENSES_DIR, REPO_ROOT, doc_path, installed_paths, load_tool, repository_files, tree_files

_HISTORY_SCAN = load_tool("history_scan")
# The private input (identifiers, origin vocabulary and their reviewed
# sites), when the environment names one; ``None`` runs the generic layer.
PRIVATE = _HISTORY_SCAN.private_input_from_env()
_NO_PRIVATE = ("BOUNDARY: no private identifier input "
               f"(${_HISTORY_SCAN.PRIVATE_INPUT_ENV}); the generic layer runs alone")


_SECRET_SHAPES = (
    # A key starts at a boundary (the history scanner's sk-key shape), never
    # inside a word such as an SPDX id "Asterisk-linking-protocols-exception".
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
# Explicitly allowed placeholders used by tests and dev-render contracts.
_ALLOWED_MARKERS = (
    "dummy",
    "supersecret-value",
    "test-dummy-value",
    "fake",
    "a\" * 64",
    "a' * 64",
)


class ImportBoundaryTests(unittest.TestCase):
    def test_runtime_modules_never_import_dev(self) -> None:
        src = REPO_ROOT / "src" / "claude_multi"
        offenders: list[str] = []
        modules = sorted(src.rglob("*.py"))
        self.assertTrue(any("cli" in p.relative_to(src).parts[:-1] for p in modules))
        for module in modules:
            module_name = "claude_multi." + ".".join(module.relative_to(src).with_suffix("").parts).removesuffix(".__init__")
            relative = module.relative_to(src).as_posix()
            if relative == "dev.py":
                continue
            tree = ast.parse(module.read_text())
            # The entry points load dev.py inside the claude-multi-dev
            # command only, so their module-level imports are what counts.
            nodes = tree.body if relative == "entrypoints.py" else ast.walk(tree)
            for node in nodes:
                if isinstance(node, ast.ImportFrom) and node.module:
                    if "dev" in node.module.split("."):
                        offenders.append(f"{module_name}: from {node.module}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "dev" in alias.name.split("."):
                            offenders.append(f"{module_name}: import {alias.name}")
        self.assertEqual(offenders, [])

    def test_comments_explain_behavior_without_review_references(self) -> None:
        pattern = re.compile(r"\b(?:LU:\d+(?:-\d+)?|R-\d+)\b")
        found = []
        for path in repository_files(REPO_ROOT):
            if path.suffix != ".py":
                continue
            source = path.read_text()
            texts = [(token.start[0], token.string) for token in tokenize.generate_tokens(io.StringIO(source).readline)
                     if token.type == tokenize.COMMENT]
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    docstring = ast.get_docstring(node)
                    if docstring:
                        texts.append((node.body[0].lineno, docstring))
            found.extend(f"{path.relative_to(REPO_ROOT)}:{line}" for line, text in texts if pattern.search(text))
        self.assertEqual(found, [])

    def test_bins_run_their_command_through_the_entry_points(self) -> None:
        for name in ("claude-multi", "claude-multi-dev", "claude-multi-proxy"):
            text = (REPO_ROOT / "bin" / name).read_text()
            with self.subTest(bin=name):
                self.assertIn("from claude_multi.entrypoints import prepare_source, run", text)
                self.assertIn(f'raise SystemExit(run("{name}"))', text)


class SecretScanTests(unittest.TestCase):
    def test_no_secret_shapes_in_source(self) -> None:
        hits: list[str] = []
        # The repository root, minus VCS metadata, worktrees, build links and
        # bytecode (tests/_layout.py tree_files).
        for child in tree_files(REPO_ROOT):
            if child.suffix not in (".py", ".json", ".md", ".nix", ""):
                continue
            text = child.read_text(errors="replace")
            for shape in _SECRET_SHAPES:
                # Every match: one allowed placeholder early in a file never
                # hides a later value.
                for match in shape.finditer(text):
                    if not any(marker in text[max(0, match.start() - 120): match.end() + 120]
                               for marker in _ALLOWED_MARKERS):
                        hits.append(f"{child.relative_to(REPO_ROOT)}: {match.group(0)[:12]}…")
        self.assertEqual(hits, [])

    def test_tree_carries_no_candidate_credential(self) -> None:
        # The history scanner's shapes and verdicts (one definition) over
        # every file of the tree, whatever its suffix (patches, goldens,
        # YAML, shell, Go, third-party notices included). Names and lines only.
        history_scan = load_tool("history_scan")
        candidates = []
        for child in tree_files(REPO_ROOT):
            for shape, line, length, _bits, verdict, _reason in history_scan.credential_hits(child.read_bytes()):
                if verdict == "candidate":
                    candidates.append(f"{child.relative_to(REPO_ROOT)}:{line}: {shape} ({length} chars)")
        self.assertEqual(candidates, [], "a random-looking credential value: replace it with a run-time "
                                         "generated value or a placeholder (dummy, fixture, example, …)")

    def test_ignored_local_secret_files_are_scanned(self) -> None:
        # One policy: credentials live outside the checkout. The gate covers
        # every file of the working tree, ignored ones included, in a
        # checkout and in a staged source tree alike; .gitignore is only a
        # safety net against committing one.
        history_scan = load_tool("history_scan")
        ignore = (REPO_ROOT / ".gitignore").read_text() if (REPO_ROOT / ".gitignore").is_file() else ""
        if ignore:
            self.assertIn("secrets/", ignore.splitlines())
            self.assertIn("*.env", ignore.splitlines())
            self.assertIn("keep keys outside the checkout", ignore)
        while True:
            key = secrets.token_hex(16)
            if not any(marker.decode() in key for marker in history_scan.VALUE_MARKERS):
                break
        with tempfile.TemporaryDirectory(prefix="cm-ignored-secrets-") as tmp:
            for kind in ("checkout", "staged"):
                root = Path(tmp) / kind
                (root / "secrets").mkdir(parents=True)
                (root / "secrets" / "provider-keys.env").write_text("DEEPSEEK_CLAUDE_API_KEY=sk" + "-" + key + "\n")
                (root / "local.env").write_text("QWEN_CLAUDE_API_KEY=sk" + "-" + key[::-1] + "\n")
                if kind == "checkout":
                    (root / ".git").mkdir()
                    (root / ".git" / "config").write_text("[core]\n")
                    (root / ".gitignore").write_text("secrets/\n*.env\n")
                scanned = {path.relative_to(root).as_posix(): path for path in tree_files(root)}
                with self.subTest(kind=kind):
                    self.assertIn("secrets/provider-keys.env", scanned)
                    self.assertIn("local.env", scanned)
                    self.assertFalse(any(name.startswith(".git/") for name in scanned))
                    verdicts = [hit[4] for name in ("secrets/provider-keys.env", "local.env")
                                for hit in history_scan.credential_hits(scanned[name].read_bytes())]
                    self.assertEqual(verdicts, ["candidate", "candidate"])

    def test_no_hardcoded_home_paths_in_src(self) -> None:
        # A real account's home path is a generic identifier; src/ carries none at all.
        history_scan = load_tool("history_scan")
        src = REPO_ROOT / "src"
        modules = sorted(src.rglob("*.py"))
        self.assertTrue(any("cli" in p.relative_to(src).parts[:-1] for p in modules))
        offenders = [str(module.relative_to(REPO_ROOT)) for module in modules
                     if history_scan.identifier_counts(module.read_bytes()).get("home-path")]
        self.assertEqual(offenders, [])


# ---------------------------------------------------------------- tree gates

# Every remaining generic identifier site in the repository tree (outside
# VCS metadata, worktrees and the third-party notices), at its exact count:
# (path, identifier) -> (occurrences, owner, what it is). A ratchet: a new
# site fails, and so does a listed site whose count moved or that no longer
# exists (then shrink this table). Test data uses neutral identities
# (``history_scan.NEUTRAL_USERS``: ``/home/user``, ``/Users/me``, …); a test
# that asserts recorded content keeps the real string and is listed with the
# owner of that content. The private input's ``identifier-site`` entries
# extend this table for its own identifiers.
IDENTIFIER_RATCHET: dict[tuple[str, str], tuple[int, str, str]] = {}
RATCHET_OWNERS = frozenset({"onboarding", "service", "wording", "docs", "keyed-audit", "publication", "keep"})


def identifier_ratchet(private=PRIVATE) -> dict[tuple[str, str], tuple[int, str, str]]:
    """The public sites and, with a private input, its sites."""

    return {**IDENTIFIER_RATCHET, **(dict(private.identifier_sites) if private is not None else {})}


# Operator preferences shipped as product defaults: no identifier names
# them, so the identifier gate cannot see them. Each needle occurs exactly
# once in its file until its owner fixes it; then the entry goes.
# (path, needle, owner, why.)
OPERATOR_DEFAULTS: tuple[tuple[str, str, str, str], ...] = (
    ("src/claude_multi/service.py", 'UNIT = "cli-proxy-api"', "service",
     "an earlier unit name; new installs use the product unit"),
    ("src/claude_multi/data/catalog/gateway.json", '"base_url": "http://127.0.0.1:8317"', "service",
     "an earlier gateway port; new installs record their own port"),
)

# Origin narration: how the code came to be, never what it is. The patterns
# are a project's own vocabulary, so they come from the private input
# (``origin`` entries, named groups in one combined pattern so each
# occurrence counts once), with the files that still carry some
# (``origin-site`` entries: ceiling, owner, why).
#
# Synthetic vocabulary of the same shapes, for the gates' own controls: a
# repository and a host name, a work-item number with a stage letter, a
# plan word, decision, ticket and section ids, a config tool and its
# abbreviation. It names no real project.
SYNTHETIC_PRIVATE = _HISTORY_SCAN.parse_private_input(
    "repository-name\texample-dotrepo\n"
    "lab-host\tlab-01\\.invalid\n"
    "origin\trepository\t(?i:example-dotrepo)\n"
    "origin\tconfig-tool\t(?i:\\bconfig[- ]tool\\b)|\\bCT\\b\n"
    "origin\twork-item\t(?<![\\w.,:/+#\"\\\\-])9[0-6]\\d[A-Za-z]?(?![\\w/-]|\\.\\d)\n"
    "origin\twork-tag\t(?<![a-zA-Z])(?i:xy)9[0-6]\\d\n"
    "origin\tplan-word\t(?i:\\broadmaps?\\b)\n"
    "origin\tdecision-id\t(?<![\\w-])Z-?\\d{1,3}(?:\\.\\d+)?\\b\n"
    "origin\ttracking-id\t\\b(?:TICKET|QX)-[A-Z]?\\d+[a-z]?\\b|\\bR\\d\\.\\d+[a-z]?\\b|(?<![\\w:])K9\\d\\d\\b\n"
    "identifier-site\ttests/example.json\trepository-name\t2\tkeep\ta recorded example\n"
    "origin-site\ttests/example.json\t3\tdocs\tthe narration a later pass removes\n"
)
# Text the synthetic origin patterns must find (the mechanism's positive
# controls), one per shape.
NARRATED_SAMPLES = (
    "# 945 Q8b: the gate", "(roadmap 938)", "see Z148", "per Z-17", "TICKET-19 fix", "QX-7",
    "R3.16 layer", "the K939 boundary", "961C stage", "xy953SchedulerAuth", "the example-dotrepo repository",
    "a config tool switch", "CT generation 147",
)
# Product facts no origin pattern may find (synthetic or private).
PRODUCT_FACTS = (
    "mode 0o700", "umask 077", "1,048,576 tokens", "version 7.3.15", "2026-07-24", "0.035 s", "port 8317",
    "catalog 37", "sha256 0a1b2c", "ABCD1", "0x0451", "\\033[0m", "-rw-r--r-- 0644", "addr 00000000:C002",
    "/v1/models", "U11 note", "x86_64", '"version": "001"',
)


@functools.lru_cache(maxsize=8)
def _origin_regex(patterns: tuple[tuple[str, str], ...]) -> re.Pattern[bytes] | None:
    if not patterns:
        return None
    return re.compile("|".join(f"(?P<o{index}>{pattern})" for index, (_name, pattern) in enumerate(patterns)).encode())


def origin_counts(data: bytes, private=PRIVATE) -> int:
    """Occurrences of origin narration in ``data`` (the private input's
    patterns; none without it)."""

    regex = _origin_regex(tuple(private.origin) if private is not None else ())
    return 0 if regex is None else sum(1 for _match in regex.finditer(data))


# The first-party files under gateway/licenses/ (generated by the build tool;
# never exempt).
FIRST_PARTY_LICENSE_FILES = frozenset({"modules.json", "CLIProxyAPI/MODIFICATIONS.txt"})


def third_party_notices() -> dict[str, str]:
    """``gateway/licenses/<path>`` -> recorded sha256 for every verbatim
    third-party notice ``modules.json`` lists (modules, toolchain, upstream)."""

    inventory = json.loads((LICENSES_DIR / "modules.json").read_text())
    groups = [*inventory["modules"], inventory["toolchain"], inventory["upstream"]]
    notices = {}
    for group in groups:
        for item in group["files"]:
            notices[(LICENSES_DIR / item["path"]).relative_to(REPO_ROOT).as_posix()] = item["sha256"]
    return notices


_NOTICES = third_party_notices()


def exempt_from_naming_gates(relative: str) -> bool:
    """A verbatim third-party notice: exempt from the identifier and origin
    gates (its legal text is never edited), never from the credential gate."""

    return relative in _NOTICES


_installed_paths = installed_paths

# The public repository's identity (owner/name, and its URL) is the
# product's own public address, not a personal identifier: the identifier
# gate reads every file with it masked. Nothing else of the owner's name is.
PUBLIC_REPOSITORY = re.compile(rb"(?:https://github\.com/)?nkoturovic/claude-multi(?![\w-])")


def identifier_bytes(data: bytes) -> bytes:
    """``data`` as the identifier gate reads it (the public repository masked)."""

    return PUBLIC_REPOSITORY.sub(b"<public-repository>", data)


def identifier_sites(private=PRIVATE) -> dict[tuple[str, str], int]:
    """(path, identifier) -> occurrences over the repository's files."""

    found = {}
    for path in repository_files(REPO_ROOT):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if exempt_from_naming_gates(relative):
            continue
        for name, count in _HISTORY_SCAN.identifier_counts(identifier_bytes(path.read_bytes()), private).items():
            found[(relative, name)] = count
    return found


def ratchet_problems(found: dict, ratchet: dict) -> dict[str, list[str]]:
    """New sites, moved counts and listed sites that are gone, for an exact ratchet."""

    return {
        "new sites": sorted(f"{key}: x{count}" for key, count in found.items() if key not in ratchet),
        "counts moved": sorted(f"{key}: x{found[key]}, listed x{listed[0]}" for key, listed in ratchet.items()
                               if key in found and found[key] != listed[0]),
        "listed sites gone": sorted(f"{key}" for key in ratchet if key not in found),
    }


_CLEAN_RATCHET = {"new sites": [], "counts moved": [], "listed sites gone": []}


class IdentifierGateTests(unittest.TestCase):
    """Personal identifiers, shipped paths first."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.history_scan = _HISTORY_SCAN
        cls.files = {path.relative_to(REPO_ROOT).as_posix(): path for path in repository_files(REPO_ROOT)}
        cls.found = identifier_sites()

    def test_only_the_public_repository_is_masked(self) -> None:
        owner = b"nko" + b"turovic"  # split: the owner's name stays a private identifier here
        for text in (b"https://github.com/" + owner + b"/claude-multi/releases/latest/download",
                     b"[" + owner + b"/claude-multi](https://github.com/" + owner + b"/claude-multi)",
                     b"git clone https://github.com/" + owner + b"/claude-multi"):
            with self.subTest(text=text):
                self.assertNotIn(owner, identifier_bytes(text))
        for text in (b"https://github.com/" + owner + b"/other-project", owner + b"/claude-multi-private", owner):
            with self.subTest(text=text):
                self.assertIn(owner, identifier_bytes(text))

    def test_the_generic_identifiers(self) -> None:
        self.assertEqual([name for name, _pattern in self.history_scan.GENERIC_IDENTIFIERS],
                         ["home-path", "macos-home-path"])
        counts = self.history_scan.identifier_counts
        # Split, so this file carries no site of its own.
        home, mac = b"/home/" + b"somebody", b"/Users/" + b"jdoe"
        for text, name in ((home + b"/.config", "home-path"), (b"HOME=" + home, "home-path"),
                           (b"`" + mac + b"/Library`", "macos-home-path"), (b"/mnt/c" + mac + b"/x", "macos-home-path")):
            with self.subTest(text=text):
                self.assertEqual(dict(counts(text)), {name: 1})
        for text in (b"/home/user/.config", b"/home/me", b"/Users/me/x", b"/fixture/home/a", b"left/right/home/end",
                     b"//mnt/c/Users/*/.claude/**", b"/home/.config", b"~/projects"):
            with self.subTest(control=text):
                self.assertEqual(dict(counts(text)), {})

    def test_a_private_input_adds_its_identifiers_and_sites(self) -> None:
        private = SYNTHETIC_PRIVATE
        self.assertEqual(dict(self.history_scan.identifier_counts(b"see lab-01.invalid and example-dotrepo", private)),
                         {"lab-host": 1, "repository-name": 1})
        self.assertEqual(dict(self.history_scan.identifier_counts(b"see lab-01.invalid")), {})
        ratchet = identifier_ratchet(private)
        self.assertEqual(ratchet[("tests/example.json", "repository-name")], (2, "keep", "a recorded example"))
        problems = ratchet_problems({("tests/example.json", "repository-name"): 3,
                                     ("tests/other.md", "lab-host"): 1}, ratchet)
        self.assertEqual(problems["new sites"], ["('tests/other.md', 'lab-host'): x1"])
        self.assertEqual(problems["counts moved"], ["('tests/example.json', 'repository-name'): x3, listed x2"])

    def test_no_identifier_in_a_tree_path(self) -> None:
        # File and directory names are scanned too.
        named = sorted(f"{relative}: {', '.join(sorted(counts))}" for relative in self.files
                       if (counts := self.history_scan.identifier_counts(relative.encode(), PRIVATE)))
        self.assertEqual(named, [])

    def test_every_installed_path_is_scanned(self) -> None:
        for installed in _installed_paths():
            with self.subTest(path=installed):
                self.assertTrue(any(relative == installed or relative.startswith(installed + "/")
                                    for relative in self.files), installed)

    def test_sites_match_the_ratchet_exactly(self) -> None:
        ratchet = identifier_ratchet()
        found = self.found
        if PRIVATE is None:
            # The generic layer judges only the generic identifiers' sites.
            generic = {name for name, _pattern in self.history_scan.GENERIC_IDENTIFIERS}
            ratchet = {key: value for key, value in ratchet.items() if key[1] in generic}
        self.assertEqual(
            ratchet_problems(found, ratchet), _CLEAN_RATCHET,
            "personal identifier gate: test data uses neutral identities (/home/user, example.lan, …); "
            "shipped content a test asserts keeps its string and is listed with its owner; a fixed or "
            "reduced site shrinks IDENTIFIER_RATCHET (tests/test_hygiene.py) or the private input's site list",
        )

    def test_ratchet_names_an_owner_for_every_site(self) -> None:
        for key, (count, owner, why) in identifier_ratchet().items():
            with self.subTest(site=key):
                self.assertGreater(count, 0)
                self.assertIn(owner, RATCHET_OWNERS)
                self.assertTrue(why)

    def test_shipped_sites_have_a_product_owner(self) -> None:
        # A shipped site belongs to onboarding, or docs prose to the
        # documentation pass.
        shipped = _installed_paths()
        for (path, _name), (_count, owner, _why) in identifier_ratchet().items():
            if any(path == item or path.startswith(item + "/") for item in shipped):
                with self.subTest(path=path):
                    self.assertIn(owner, {"onboarding", "docs"})

    def test_operator_defaults_review(self) -> None:
        for path, needle, owner, why in OPERATOR_DEFAULTS:
            with self.subTest(path=path, owner=owner):
                self.assertIn(owner, RATCHET_OWNERS - {"keep"})
                self.assertTrue(why)
                self.assertEqual((REPO_ROOT / path).read_text().count(needle), 1,
                                 f"{path}: the reviewed operator default changed; drop or update its "
                                 "OPERATOR_DEFAULTS entry")


def origin_sites(private=PRIVATE) -> dict[str, int]:
    """path -> origin narration occurrences over the repository's files."""

    found = {}
    for path in repository_files(REPO_ROOT):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if exempt_from_naming_gates(relative):
            continue
        count = origin_counts(path.read_bytes(), private)
        if count:
            found[relative] = count
    return found


class OriginNarrationTests(unittest.TestCase):
    """The tree describes the product as it is: the private input's origin
    patterns find nothing outside its reviewed sites. Without the input
    the mechanism runs on :data:`SYNTHETIC_PRIVATE`."""

    def test_the_mechanism_finds_narration_and_spares_product_facts(self) -> None:
        for text in NARRATED_SAMPLES:
            with self.subTest(text=text):
                self.assertGreaterEqual(origin_counts(text.encode(), SYNTHETIC_PRIVATE), 1)
        for text in PRODUCT_FACTS:
            with self.subTest(text=text):
                self.assertEqual(origin_counts(text.encode(), SYNTHETIC_PRIVATE), 0)
        self.assertEqual(origin_counts(b"# 945 Q8b: the gate", None), 0)

    def test_the_mechanism_is_a_ratchet(self) -> None:
        sites = {path: listed for path, listed in SYNTHETIC_PRIVATE.origin_sites.items()}
        self.assertEqual(sites, {"tests/example.json": (3, "docs", "the narration a later pass removes")})
        found = {"tests/example.json": 4, "src/new.py": 1}
        problems = origin_problems(found, sites, exists=lambda _path: True)
        self.assertEqual(problems["new sites"], ["src/new.py: x1"])
        self.assertEqual(problems["above the ceiling"], ["tests/example.json: x4, ceiling x3"])
        self.assertEqual(origin_problems({}, sites, exists=lambda _path: True)["listed files now clean"],
                         ["tests/example.json"])

    def test_sites_stay_within_the_ratchet(self) -> None:
        if PRIVATE is None:
            self.skipTest(_NO_PRIVATE)
        if not PRIVATE.origin:
            # A list of plain identifiers only: its vocabulary runs in the
            # identifier gate, which has no ceilings.
            self.skipTest("BOUNDARY: the private input names no origin pattern; its entries run in the "
                          "identifier gate")
        self.assertEqual(
            origin_problems(origin_sites(), dict(PRIVATE.origin_sites)),
            {"new sites": [], "above the ceiling": [], "listed files now clean": []},
            "origin gate: describe the code as it is and keep the reason, without plan, stage, decision or "
            "issue ids and without the project's earlier tooling; a cleaned file leaves the private input's "
            "site list, a reduced one may lower its ceiling",
        )

    def test_the_private_patterns_spare_product_facts(self) -> None:
        if PRIVATE is None:
            self.skipTest(_NO_PRIVATE)
        for text in PRODUCT_FACTS:
            with self.subTest(text=text):
                self.assertEqual(origin_counts(text.encode()), 0)

    def test_the_wording_share_is_clear(self) -> None:
        # Product text (help, UI, messages, code comments and their goldens)
        # carries no origin narration at all: no wording row remains.
        if PRIVATE is None:
            self.skipTest(_NO_PRIVATE)
        self.assertEqual(sorted(path for path, (_c, owner, _w) in PRIVATE.origin_sites.items()
                                if owner == "wording"), [])
        wording = sorted(path for path in origin_sites() if path.startswith(("src/", "tests/goldens/")))
        self.assertEqual(wording, [])

    def test_ratchet_names_an_owner_for_every_file(self) -> None:
        for private in (SYNTHETIC_PRIVATE, PRIVATE):
            for path, (ceiling, owner, why) in (private.origin_sites.items() if private is not None else ()):
                with self.subTest(path=path):
                    self.assertGreater(ceiling, 0)
                    self.assertIn(owner, RATCHET_OWNERS)
                    self.assertTrue(why)
                    self.assertFalse(exempt_from_naming_gates(path))


def origin_problems(found: dict[str, int], sites: dict[str, tuple[int, str, str]],
                    exists=lambda path: (REPO_ROOT / path.split("/", 1)[0]).exists()) -> dict[str, list[str]]:
    """New files, files above their ceiling, and listed files now clean. A
    tree without a listed file's top-level entry (the Nix sandbox stages no
    .github/ or flake.nix) cannot judge that file."""

    return {
        "new sites": sorted(f"{path}: x{count}" for path, count in found.items() if path not in sites),
        "above the ceiling": sorted(f"{path}: x{found[path]}, ceiling x{ceiling}"
                                    for path, (ceiling, _owner, _why) in sites.items()
                                    if found.get(path, 0) > ceiling),
        "listed files now clean": sorted(path for path in sites if path not in found and exists(path)),
    }


# Release lineage: user-visible product text names the thing ("legacy
# record", "lineup-enabled scope", "the earlier launcher"), never the release
# line it came from, a planning document or a unit id. Serialized keys,
# schema numbers, ``restore-2x`` and compatibility aliases stay.
LINEAGE_PATTERNS: tuple[tuple[str, str], ...] = (
    # Release labels, never a dependency version, schema, path or number.
    ("release-line", r"(?<![\w./+\\-])(?:pre-)?(?:2\.(?:x|2\d)|3\.[0-3](?:\.(?:x|\d+))?)(?![\w%]|\.\d)"),
    # Planning documents and their item ids.
    ("planning", r"\bMIGRATION\.md\b|\bPL-\d+\b|\bLD\d+\b|\bAMB-\d+\b|\bSPEC\s*§"),
    # Unit ids, never an octal escape, a mode or part of a number.
    ("unit-id", r"(?<![\w.,:/+#\"\\-])07\d[A-Za-z]?(?![\w/-]|\.\d)"),
    # The test file families named after a release line or a unit
    # (LINEAGE_PATH_RE): their names in visible text carry the lineage too.
    ("lineage-file", r"\b(?:test_cm3x?|test_s14|gateway_0\d\d)_\w+"),
)
_LINEAGE_RE = re.compile("|".join(f"(?P<{name.replace('-', '_')}>{pattern})" for name, pattern in LINEAGE_PATTERNS))
# Synthetic tokens exercising the production patterns' alternatives and
# boundaries, not accounts of a project's releases or work items.
LINEAGE_EXAMPLES = {
    "release-line": ("2." + "x", "2." + "29", "3." + "3", "3.3." + "x", "3.3." + "987",
                     "pre-" + "3.3"),
    "planning": ("MIGRATION" + ".md", "PL-" + "987", "LD" + "987", "AMB-" + "987", "SPEC" + " §987"),
    "unit-id": (str(79).zfill(3), str(79).zfill(3) + "A"),
    "lineage-file": ("test_cm" + "3_fixture", "test_cm" + "3x_fixture", "test_s" + "14_fixture",
                     "gateway_" + "099_fixture"),
}
LINEAGE_SAMPLES = tuple("fixture: " + token for tokens in LINEAGE_EXAMPLES.values() for token in tokens)
LINEAGE_CONTROLS = (
    "schema v3", "v4 records", "state version 3", "Claude Code 2.1.286", "CLIProxyAPI 7.3.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36", "Mac OS X 10_15_7",
    "Copyright (c) 2026 the claude-multi authors", "\\033[0m", "\\070", "0o700", "mode 0700",
    "python3.14", "Python 3.11", "timeout 3.05 s", "restore-2x", "quarantine-3x/", "<id>.v3.json",
    "catalog 37", "HTTP/2.0", "TLS 1.3", "200K..800K", "GPL-3.0-or-later", "port 8070",
    "tests/test_cm_help.py", "test_s1_reload_plugins_rebinds_next_spawn", "gateway_startup_probe_test.go",
    "test_scope_v2.py", "gateway_0x10", "the cm3 note",
)
# Reviewed sites that keep a match, each at its exact count: (path, needle,
# count, why).
LINEAGE_KEEP: tuple[tuple[str, str, int, str], ...] = (
    ("src/claude_multi/scope.py", "embedded in compiled 3.0", 1,
     "the hook shim's bytes: earlier launchers write the same shim, so a new text rewrites every "
     "installed shim back and forth"),
    ("tests/goldens/shim/claude-multi-hook-3", "embedded in compiled 3.0", 1, "the golden of that shim"),
    ("src/claude_multi/migrate.py", "2.x", 1,
     "the target field value of the restore-2x --check --json document (a machine contract)"),
)
# Test files whose names still carry release or unit lineage, with the pass
# that renames them; a new one fails, a renamed one leaves the table.
LINEAGE_PATH_RE = re.compile(r"(?:^|/)(?:test_cm3x?_|test_s14_|gateway_0\d\d_)[^/]*$|(?:^|/)test_\w+_\dx\.py$")
LINEAGE_PATHS: dict[str, str] = {}


def lineage_matches(text: str) -> list[str]:
    """The lineage phrases in ``text``."""

    return [match.group(0) for match in _LINEAGE_RE.finditer(text)]


def _string_constants(source: str) -> list[str]:
    """Every string a module can show: its string constants (f-string parts
    included), never a docstring, comment or number."""

    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]


def product_texts() -> dict[str, list[str]]:
    """path -> the user-visible texts the lineage gate reads: the modules'
    strings, the packaged data (the vendored model registry aside: third-party
    content) and the goldens of product output."""

    texts: dict[str, list[str]] = {}
    for path in repository_files(REPO_ROOT):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative.startswith("src/claude_multi/") and path.suffix == ".py":
            texts[relative] = _string_constants(path.read_text())
        elif relative.startswith("src/claude_multi/data/") and not relative.startswith(
                "src/claude_multi/data/registry/"):
            texts[relative] = [path.read_text(errors="replace")]
        elif relative.startswith("tests/goldens/"):
            texts[relative] = [path.read_text(errors="replace")]
    return texts


class LineageWordingTests(unittest.TestCase):
    """User-visible product text names the thing, never its release line."""

    def test_the_patterns_find_lineage_and_spare_versions(self) -> None:
        for text in LINEAGE_SAMPLES:
            with self.subTest(text=text):
                self.assertTrue(lineage_matches(text), text)
        for text in LINEAGE_CONTROLS:
            with self.subTest(text=text):
                self.assertEqual(lineage_matches(text), [], text)
        # The patterns are their own: none is an origin pattern too.
        for private in (SYNTHETIC_PRIVATE, PRIVATE):
            self.assertTrue(all(origin_counts(text.encode(), private) == 0
                                for text in LINEAGE_EXAMPLES["release-line"] + LINEAGE_EXAMPLES["planning"]))

    def test_controls_are_synthetic_and_cover_every_pattern(self) -> None:
        self.assertTrue(all(text.startswith("fixture: ") for text in LINEAGE_SAMPLES))
        self.assertEqual(set(LINEAGE_EXAMPLES), {name for name, _pattern in LINEAGE_PATTERNS})
        for name, pattern in LINEAGE_PATTERNS:
            with self.subTest(pattern=name):
                for token in LINEAGE_EXAMPLES[name]:
                    self.assertIsNotNone(re.search(pattern, "fixture: " + token))
                for text in LINEAGE_CONTROLS:
                    self.assertIsNone(re.search(pattern, text))

    def test_lineage_file_names_in_visible_text_are_found(self) -> None:
        for token in LINEAGE_EXAMPLES["lineage-file"]:
            text = f"fixture: tests/{token}.py"
            with self.subTest(text=text):
                self.assertEqual(lineage_matches(text), [token])
        for path in LINEAGE_PATHS:
            with self.subTest(path=path):
                self.assertTrue(lineage_matches(path.rsplit("/", 1)[-1]), path)
        for text in ("tests/test_cm_help.py", "test_s1_reload_plugins_rebinds_next_spawn",
                     "gateway_startup_probe_test.go", "test_scope_v2.py"):
            with self.subTest(control=text):
                self.assertEqual(lineage_matches(text), [], text)

    def test_product_text_carries_no_lineage(self) -> None:
        keep = {}
        for path, needle, count, why in LINEAGE_KEEP:
            self.assertTrue(why)
            keep.setdefault(path, []).append((needle, count))
        found = {}
        texts = product_texts()
        self.assertIn("src/claude_multi/cli/text.py", texts)
        for path, strings in texts.items():
            joined = "\n".join(strings)
            for needle, count in keep.get(path, ()):
                with self.subTest(keep=path, needle=needle):
                    self.assertEqual(joined.count(needle), count)
                joined = joined.replace(needle, "")
            hits = lineage_matches(joined)
            if hits:
                found[path] = hits
        self.assertEqual(found, {}, "name the thing, not the release line: legacy record, lineup-enabled "
                                    "scope, the earlier launcher, the earlier spelling (no planning documents "
                                    "or unit ids); a reviewed exception goes on LINEAGE_KEEP with its reason")

    def test_rendered_help_carries_no_lineage(self) -> None:
        import argparse
        from contextlib import redirect_stdout
        import io

        from claude_multi import dev, proxy
        from claude_multi.cli.parser import build_parser

        helps = {"claude-multi-proxy": proxy.PROXY_USAGE, "claude-multi-dev": dev.DEV_HELP}
        helps.update({f"claude-multi-proxy {command.name}": command.help() for command in proxy.PROXY_COMMANDS})
        seen: set[int] = set()

        def walk(parser: argparse.ArgumentParser, name: str) -> None:
            if id(parser) in seen:
                return
            seen.add(id(parser))
            helps[name] = parser.format_help()
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for child_name, child in action.choices.items():
                        walk(child, f"{name} {child_name}")

        with redirect_stdout(io.StringIO()):
            walk(build_parser(), "claude-multi")
        self.assertGreater(len(helps), 40)
        found = {name: lineage_matches(text) for name, text in helps.items() if lineage_matches(text)}
        self.assertEqual(found, {})

    def test_test_file_names_carry_no_lineage(self) -> None:
        paths = [f"tests/{token}.py" for token in LINEAGE_EXAMPLES["lineage-file"]]
        for path in (*paths, "tests/test_fixture_" + "9x.py"):
            with self.subTest(path=path):
                self.assertTrue(LINEAGE_PATH_RE.search(path), path)
        for path in ("tests/test_client_hooks.py", "tests/test_managed_skill_policy.py",
                     "tests/gateway_executor_probe_test.go", "tests/test_scope_probe_client.py"):
            with self.subTest(control=path):
                self.assertIsNone(LINEAGE_PATH_RE.search(path), path)
        names = sorted(path.relative_to(REPO_ROOT).as_posix() for path in repository_files(REPO_ROOT)
                       if LINEAGE_PATH_RE.search(path.relative_to(REPO_ROOT).as_posix()))
        if not (REPO_ROOT / "tests").is_dir():
            self.skipTest("BOUNDARY: no tests/ in this tree")
        self.assertEqual(names, sorted(LINEAGE_PATHS), "a test file name names what it tests; a renamed file "
                                                       "leaves LINEAGE_PATHS")
        for path, owner in LINEAGE_PATHS.items():
            self.assertIn(owner, RATCHET_OWNERS - {"wording", "keep"})


class LocalEnvironmentTests(unittest.TestCase):
    """The documented development setup passes the hygiene gates. Its
    virtual environment lives outside the checkout; one created inside it
    anyway, like build output and package metadata, is ignored local
    content: out of the naming gates, still under the credential gate."""

    def test_the_documented_virtual_environment_is_outside_the_checkout(self) -> None:
        text = doc_path("CONTRIBUTING.md").read_text()
        setup = text.split("## Set up a checkout", 1)[1].split("\n## ", 1)[0]
        created = re.findall(r"python3 -m venv (\S+)", setup)
        self.assertEqual(len(created), 1, setup)
        venv = created[0]
        # Home-relative or absolute: never a directory of the clone.
        self.assertTrue(venv.startswith(("~/", "/", "$HOME/")), venv)
        used = [line.split()[0] for line in setup.splitlines() if line.startswith(venv + "/bin/")]
        self.assertIn(f"{venv}/bin/pip", used)
        self.assertIn(f"{venv}/bin/claude-multi", used)

    def test_ignored_local_content_stays_out_of_the_naming_gates(self) -> None:
        import shutil
        import subprocess
        import venv

        if shutil.which("git") is None:
            self.skipTest("BOUNDARY: git unavailable (repository files are what git tracks or would add)")
        if not (REPO_ROOT / ".gitignore").is_file():
            self.skipTest("BOUNDARY: no .gitignore in this tree (a staged source tree)")
        narration = "\n".join(NARRATED_SAMPLES) + "\n"
        with tempfile.TemporaryDirectory(prefix="cm-local-env-") as tmp:
            root = Path(tmp) / "checkout"
            (root / "src").mkdir(parents=True)
            shutil.copyfile(REPO_ROOT / ".gitignore", root / ".gitignore")
            (root / "src" / "module.py").write_text("VALUE = 1\n")
            (root / "notes.md").write_text(narration)  # untracked, not ignored
            git = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "HOME": tmp,
                   "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            subprocess.run(["git", "init", "-q", str(root)], check=True, env=git)
            subprocess.run(["git", "-C", str(root), "add", ".gitignore", "src/module.py"], check=True, env=git)
            # The setup a contributor may run inside the clone: a stock
            # virtual environment (pip from the bundled wheel, offline when
            # available), the editable install's metadata, a build directory.
            try:
                venv.EnvBuilder(with_pip=True).create(root / ".venv")
            except (OSError, subprocess.SubprocessError):
                venv.EnvBuilder(with_pip=False).create(root / ".venv")
            # Newer venvs ignore themselves; the checkout's own rules decide.
            (root / ".venv" / ".gitignore").unlink(missing_ok=True)
            local = (".venv/lib/vendored/notes.py", "src/claude_multi.egg-info/SOURCES.txt",
                     "build/lib/claude_multi/notes.py")
            for name in local:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(narration)
            owned = {path.relative_to(root).as_posix() for path in repository_files(root)}
            every = {path.relative_to(root).as_posix() for path in tree_files(root)}
            ignored = every - owned
            self.assertTrue({name for name in every if name.startswith(".venv/")} <= ignored)
            self.assertTrue(set(local) <= ignored)
            self.assertTrue(set(local) <= every)  # the credential gate still reads them
            self.assertEqual(owned, {".gitignore", "src/module.py", "notes.md"})
            narrating = sorted(name for name in owned
                               if origin_counts((root / name).read_bytes(), SYNTHETIC_PRIVATE))
            self.assertEqual(narrating, ["notes.md"])


class ThirdPartyNoticeTests(unittest.TestCase):
    """Verbatim third-party notices keep their bytes and are exempt from the
    naming gates only; the exemption covers nothing else."""

    def test_every_notice_keeps_its_recorded_bytes(self) -> None:
        self.assertTrue(_NOTICES)
        for relative, digest in _NOTICES.items():
            with self.subTest(notice=relative):
                self.assertTrue(relative.startswith("gateway/licenses/"))
                self.assertEqual(hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest(), digest)

    def test_the_exemption_is_narrow(self) -> None:
        listed = {path.relative_to(LICENSES_DIR).as_posix() for path in LICENSES_DIR.rglob("*") if path.is_file()}
        notices = {relative.removeprefix("gateway/licenses/") for relative in _NOTICES}
        # Every file there is a listed notice or one of the first-party files.
        self.assertEqual(listed - notices, set(FIRST_PARTY_LICENSE_FILES))
        for name in FIRST_PARTY_LICENSE_FILES:
            self.assertFalse(exempt_from_naming_gates(f"gateway/licenses/{name}"), name)
        for relative in ("LICENSE", "README.md", "gateway/UPSTREAM.json", "src/claude_multi/data/version.json"):
            self.assertFalse(exempt_from_naming_gates(relative), relative)

    def test_notices_stay_under_the_credential_gate(self) -> None:
        history_scan = load_tool("history_scan")
        scanned = {path.relative_to(REPO_ROOT).as_posix() for path in tree_files(REPO_ROOT)}
        self.assertTrue(set(_NOTICES) <= scanned)
        notice = next(iter(sorted(_NOTICES)))
        while True:
            key = secrets.token_hex(16)
            if not any(marker.decode() in key for marker in history_scan.VALUE_MARKERS):
                break
        data = (REPO_ROOT / notice).read_bytes() + ("\nQWEN_CLAUDE_API_KEY=sk" + "-" + key + "\n").encode()
        self.assertIn("candidate", [hit[4] for hit in history_scan.credential_hits(data)])


class PosixSeamTests(unittest.TestCase):
    """``fcntl`` only behind ``platform/`` (the dev harness ``probe.py`` is the
    one exception: its PTY driver sizes terminals with ``ioctl``)."""

    ALLOWED = frozenset({"claude_multi.platform.posix_fs", "claude_multi.probe"})

    def test_fcntl_is_imported_only_by_the_platform_seam(self) -> None:
        src = REPO_ROOT / "src" / "claude_multi"
        importers = set()
        for path in sorted(src.rglob("*.py")):
            dotted = "claude_multi." + ".".join(path.relative_to(src).with_suffix("").parts).removesuffix(".__init__")
            text = path.read_text()
            tree = ast.parse(text)
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names = [node.module]
                if any(name == "fcntl" or name.startswith("fcntl.") for name in names):
                    importers.add(dotted)
            if re.search(r"""(?:import_module|__import__)\(\s*["']fcntl""", text):
                importers.add(dotted)
        outside = sorted(name for name in importers
                         if not name.startswith("claude_multi.platform.") and name not in self.ALLOWED)
        self.assertEqual(outside, [], "route flock/ioctl through claude_multi.platform.posix_fs")
        # Positive control: the seam itself is found by this scan.
        self.assertIn("claude_multi.platform.posix_fs", importers)

    def test_hooks_and_lineup_log_lock_through_posix_fs(self) -> None:
        for name in ("hooks.py", "lineup_log.py"):
            text = (REPO_ROOT / "src" / "claude_multi" / name).read_text()
            with self.subTest(module=name):
                self.assertNotIn("fcntl.", text)
                self.assertIn("posix_fs.lock_descriptor(", text)


# The manager's command names belong to the systemd backend only (no
# templates ship in src/); every other module reaches them through the
# remedy seam (service.hint) or the backend's calls.
SERVICE_MANAGER_OWNERS = ("claude_multi.platform.linux_service",)


class ServiceBoundaryTests(unittest.TestCase):
    def test_service_manager_literals_have_one_owner(self) -> None:
        src = REPO_ROOT / "src" / "claude_multi"
        owners_seen = set()
        for path in sorted(src.rglob("*.py")):
            text = path.read_text()
            dotted = "claude_multi." + ".".join(path.relative_to(src).with_suffix("").parts).removesuffix(".__init__")
            with self.subTest(module=dotted):
                if dotted not in SERVICE_MANAGER_OWNERS:
                    self.assertNotRegex(text, r"systemctl|journalctl")
                else:
                    owners_seen.add(dotted)
                if dotted not in ("claude_multi.service", "claude_multi.proxy"):
                    if path.name == "management.py":
                        # The exact build-attestation artifact, not a service
                        # name or command. All other literal uses stay refused.
                        text = text.replace(
                            'ALLOWLIST_PATCH = "cli-proxy-api-management-readonly-allowlist.patch"',
                            "", 1)
                    self.assertNotIn("cli-proxy-api", text)
                self.assertNotIn("+ restart", text)
        self.assertEqual(owners_seen, set(SERVICE_MANAGER_OWNERS))


if __name__ == "__main__":
    unittest.main()
