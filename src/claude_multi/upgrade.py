"""Evidence-gated Claude Code re-pin (``claude-multi-dev repin``; maintainer).

A claude-multi release pins exactly one Claude Code version. Moving the pin
is a maintainer step from a source checkout, run as one command:

1. **detect** a candidate artifact newer than the pin: the target of the
   ``claude`` on PATH, then the native versions directories, newest first;
2. **verify** it against Anthropic's signed release manifest (GnuPG with
   the vendored key; a download only after its request plan was accepted,
   or offline with ``--manifest-dir``) and **inspect** it offline
   (regular non-symlink executable, ``--version``, ``--help`` — no prompt,
   no provider, no daemon);
3. **promote** the next contract v2 into the checkout (every platform build
   the signed manifest lists) and bump ``catalog_version``, syncing the
   suite's deliberate version pins;
4. run the checkout's **evidence** suite, whose real-binary probes now run
   the candidate, and require every essential completion record, then
   print the commit command. The new pin reaches an installation with the
   next release built from the checkout; this command never builds or
   installs anything.

Every step fails closed: a failed verification, inspection or red suite
leaves the checkout byte-identical. No operator override is written: a
build runs only the version its own contract pins. No real provider,
transcript or live daemon is touched.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

import claude_multi

from . import errors, layout, paths, pin, release_manifest, strict_json
from .platform import posix_fs


# Positive completion lines the offline evidence suite must print (anchored
# at a line start). Indices 0-3 are the native probes (test_scope_probe.py
# indexes them); 4 is the per-seed delegation probe
# (test_scope_probe_seeds.py); 5-10 are the client checks the lineup design
# stands on: a BOUNDARY skip, an INCONCLUSIVE or a FAIL verdict prints no
# such line, so `repin` refuses. 11-12 are the correctness-essential CE/DAH
# classes.
ESSENTIAL_EVIDENCE_PREFIXES = (
    "real delegation outcome: ran ",
    "fence negative control: subagent silently ran on the lead ",
    "real manual compaction outcome: ran ",
    "real auto compaction outcome: ran ",
    "per-seed delegation outcome: ran ",
    "client check S1: PASS ",
    "client check S4: PASS ",
    "client check S5: PASS ",
    "client check S6: PASS ",
    "client check S7: PASS ",
    "client check S9: PASS ",
    "client probe CE: PASS class=CE-A ",
    "client probe DAH: PASS class=DAH-override ",
    # The feedback-draft policy (scope env suppresses SendFeedback) and the
    # CLAUDECODE marker reaching Bash children (the consent guard's premise).
    "client probe FD: PASS class=FD-env-off ",
    "client probe CC: PASS class=CC-bash-child ",
    # The exact-client gate: an overlay-only Anthropic pool
    # wire keeps its declared efforts, thinking, window and output limit.
    "client probe XC: PASS class=XC-exact ",
    # The skill-deny probe (the compiled skill deny
    # refuses the model's Skill call for the lead and cm-* agents in every
    # permission mode and allow surface, with discriminating pre-policy
    # controls; typed slash commands still run) and the fast-mode tripwire
    # (no penguin-mode prefetch, no fast speed) are re-proved at every re-pin.
    # Every mandatory skill-deny case prints its own completion record, so a skipped
    # (INCONCLUSIVE/BOUNDARY) case never passes on the others' evidence; only
    # supervisor takeover is outside the proof.
    "client probe S14: PASS class=S14-model-deny ",
    "client probe S14-agents: PASS class=S14-subagent-deny ",
    "client probe S14-matrix: PASS class=S14-mode-allow-matrix ",
    "client probe S14-alias: PASS class=S14-alias-resolution ",
    "client probe S14-user-only: PASS class=S14-upstream-classified ",
    "client probe S14-precedence: PASS class=S14-fresh-resume ",
    "client probe S14-controls: PASS class=S14-positive-controls ",
    "client probe S14-typed: PASS class=S14-typed-run ",
    "client probe FM: PASS class=FM-prefetch-off ",
    # The flag-settings disable wins over a competing
    # user/project/local settings env on a fresh start and on --resume.
    "client probe FM-layers: PASS class=FM-flag-settings ",
)

# Named alternative completion lines that satisfy an essential prefix. The
# feedback-draft probe: the full lead-and-subagent proof, or the explicit
# client boundary (off suppresses both, notify restores the lead, and the
# client never offers SendFeedback to a subagent, even one that lists it).
ESSENTIAL_EVIDENCE_ALTERNATIVES = {
    "client probe FD: PASS class=FD-env-off ": (
        "client probe FD: PASS class=FD-env-off-subagent-withheld ",
    ),
}


def _missing_evidence_prefixes(output: str) -> tuple[str, ...]:
    """Positive native-probe completion records absent from suite output."""

    return tuple(
        prefix
        for prefix in ESSENTIAL_EVIDENCE_PREFIXES
        if not any(
            re.search(rf"(?m)^{re.escape(line)}", output)
            for line in (prefix, *ESSENTIAL_EVIDENCE_ALTERNATIVES.get(prefix, ()))
        )
    )


def _diagnostic_notes(output: str, version: str) -> tuple[str, ...]:
    """Diagnostic changes inform, never veto a verified re-pin."""

    notes: dict[str, str] = {}
    for match in re.finditer(
        r"(?m)^client probe (U1|X5|RL|R19|RET|SC|SX|SF|SK|OC|FL|SL): \w+ class=([\w-]+)([^\n]*)$", output
    ):
        probe_id, observed, detail = match.groups()
        recorded = re.search(r"\bchanged recorded=([\w-]+)", detail)
        if recorded is None:
            continue
        baseline = recorded.group(1)
        notes[probe_id] = (
            f"note: probe {probe_id} observed {observed} on Claude {version} "
            f"(recorded: {baseline}); the doctor and lead-prompt text that depends on it "
            "may be inaccurate until a claude-multi release re-records it"
        )
    return tuple(notes.values())


class UpgradeError(errors.ClaudeMultiError, RuntimeError):
    """Raised on any failed re-pin step (fail closed, checkout untouched)."""


_LAUNCHER_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-dev)?$")


def _release_document(root: Path, *, running_version: str | None = None) -> dict[str, Any]:
    try:
        # Preserve a missing checkout/file as FileNotFoundError: the strict loader
        # otherwise reports every absent path as "not a regular file".
        (root / "version.json").stat()
        doc = strict_json.load(root / "version.json")
        if not isinstance(doc, dict):
            raise ValueError("not an object")
        version = doc.get("launcher_version")
        if not isinstance(version, str) or not _LAUNCHER_VERSION.fullmatch(version):
            raise ValueError("invalid launcher_version")
        for key in ("catalog_version", "version"):
            if type(doc.get(key)) is not int:
                raise ValueError(f"invalid {key}")
        return doc
    except (OSError, ValueError, RecursionError) as exc:
        remedy = (f" — pass --repo with a {running_version} checkout"
                  if isinstance(exc, FileNotFoundError) and running_version is not None else "")
        raise UpgradeError(
            f"repin refused: {root}/version.json is unreadable or incomplete ({exc}); "
            "this launcher cannot tell which claude-multi release the checkout is" + remedy
        ) from exc


@dataclass(frozen=True)
class ReleaseIdentity:
    launcher_version: str
    catalog_version: int
    data_version: int
    contract_schema_sha256: str

    @classmethod
    def from_root(cls, root: Path) -> "ReleaseIdentity":
        doc = _release_document(root)
        try:
            digest = hashlib.sha256((root / "schemas/native-contract.schema.json").read_bytes()).hexdigest()
        except OSError as exc:
            raise UpgradeError(f"repin refused: cannot read native-contract schema at {root}: {exc}") from exc
        return cls(doc["launcher_version"], doc["catalog_version"], doc["version"], digest)


def running_release() -> ReleaseIdentity:
    """The installed package's own release (never a resource override)."""

    return ReleaseIdentity.from_root(claude_multi.resources_root())


def checkout_guard(product_root: Path, running: ReleaseIdentity, *, writes: bool) -> None:
    """W0–W6, before candidate execution or checkout/override mutation."""
    root = product_root
    resources = layout.checkout_resources(product_root)
    doc = _release_document(resources, running_version=running.launcher_version)
    cv, rv = doc["launcher_version"], running.launcher_version
    checkout_core = tuple(map(int, cv.removesuffix("-dev").split(".")))
    running_core = tuple(map(int, rv.removesuffix("-dev").split(".")))
    mm = ".".join(rv.split(".")[:2])
    if doc["version"] != running.data_version:
        raise UpgradeError(
            f"repin refused: the checkout at {root} uses data version {doc['version']}; "
            f"this launcher ({rv}) handles version {running.data_version} — run repin "
            f"from the checkout's release, or pass --repo with a {mm}.x checkout"
        )
    if checkout_core[:2] != running_core[:2] or checkout_core < running_core:
        raise UpgradeError(
            f"repin refused: the checkout at {root} is claude-multi {cv}, this launcher is {rv}; "
            "repin writes only into a checkout of its own release — run repin from the checkout, "
            f"or pass --repo with a {rv} checkout"
        )
    if doc["catalog_version"] < running.catalog_version:
        raise UpgradeError(
            f"repin refused: the checkout at {root} has catalog_version {doc['catalog_version']}, "
            f"older than this launcher's {running.catalog_version}; promoting into it would "
            "downgrade the catalog of the next release — check out the current branch "
            "or pass --repo"
        )
    if writes:
        schema = resources / "schemas/native-contract.schema.json"
        try:
            digest = hashlib.sha256(schema.read_bytes()).hexdigest()
        except OSError as exc:
            raise UpgradeError(
                f"repin refused: {schema} is missing; the checkout is not a claude-multi {mm}.x source tree"
            ) from exc
        if digest != running.contract_schema_sha256:
            raise UpgradeError(
                "repin refused: the checkout's native-contract schema differs from this "
                f"launcher's (sha256 {digest[:12]}… vs {running.contract_schema_sha256[:12]}…); "
                "a contract written by this launcher may not satisfy it — run repin from "
                "the checkout"
            )
    if cv != rv:
        raise UpgradeError(
            f"repin refused: the checkout at {root} is claude-multi {cv} and this launcher is {rv}; "
            "repin promotes only into a checkout of exactly the running release "
            "(a -dev checkout carries unreleased code) — run repin from the checkout, "
            f"or pass --repo with a {rv} checkout"
        )


FetchConsent = Callable[[release_manifest.RequestPlan], bool]


def _release_verifier(manifest_dir: Path | None, environ: dict[str, str],
                      consent: FetchConsent | None = None):
    """The candidate verifier: offline ``--manifest-dir`` never
    touches the network and needs no consent; otherwise each candidate's
    exact request plan is put to ``consent`` first — no consent (or no
    ``consent`` at all) sends nothing. GnuPG is located before the question,
    so a missing gpg never costs a request."""

    def verify(version: str, digest: str, size: int) -> release_manifest.ReleaseProof:
        gpg = release_manifest.gpg_executable(environ)
        if manifest_dir is not None:
            source = lambda version: release_manifest.load_local(manifest_dir, version)
        else:
            plan = release_manifest.request_plan(version)
            if consent is None or not consent(plan):
                raise release_manifest.ReleaseFetchDeclined(f"declined the request plan for {version}")
            source = release_manifest.fetch
        return release_manifest.verify_release(version, digest, size, source=source, gpg=gpg)
    return verify


def _changed_paths_report(repo: Path, paths: list[str], git: Callable) -> list[str]:
    try:
        result = git(
            ["git", "-C", str(repo), "status", "--short", "--", *paths],
            capture_output=True, text=True, timeout=10,
            env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"),
                 "GIT_OPTIONAL_LOCKS": "0"},
        )
        if result.returncode != 0:
            raise OSError(f"exit {result.returncode}")
        return [f"changed in {repo}:", *[f"  {line}" for line in result.stdout.splitlines()]]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"changed in {repo}: git status unavailable ({exc}); expected: {shlex.join(paths)}"]


def _write_checkout_file(checkout: Path, path: Path, data: bytes) -> None:
    """:func:`_write_repo_file` into the verified checkout only: every
    directory from the checkout down to ``path`` must be a real directory
    inside it (a symlinked ``src``, ``data`` or ``catalog`` never redirects
    a write)."""

    try:
        target = layout.checkout_destination(checkout, path)
    except layout.LayoutError as exc:
        raise UpgradeError(f"repin refused: {exc}") from None
    _write_repo_file(target, data)


def _write_repo_file(path: Path, data: bytes) -> None:
    """Crash-atomic write preserving the repo file's existing mode.

    ``state.atomic_write`` is mode-0600-by-design for private state; checkout
    files are normal repo files (0644), so promotion uses the same
    temp+fsync+replace discipline with the mode carried over.
    """

    if path.is_symlink():
        # Never follow a symlink into a mode/target the caller didn't mean
        # repo files are git-managed regulars.
        raise UpgradeError(f"refusing to replace symlinked repo file {path}")
    try:
        mode = stat.S_IMODE(os.lstat(path).st_mode)
    except OSError:
        mode = 0o644
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise

    # Publication is not durable until the containing directory is synced.
    # Propagate failure after replacement: the repin transaction restores its
    # owned pre-image, unlike the hook log's best-effort helper.
    posix_fs.fsync_directory(path.parent)


class _Heartbeat:
    """Periodic proof-of-life line while a long subprocess phase runs."""

    def __init__(
        self, progress: Callable[[str], None] | None, label: str, interval: float = 15.0
    ):
        self._progress = progress
        self._label = label
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_Heartbeat":
        if self._progress is None:
            return self
        started = time.monotonic()

        def _beat() -> None:
            while not self._stop.wait(self._interval):
                self._progress(
                    f"{self._label} ({int(time.monotonic() - started)}s elapsed)"
                )

        self._thread = threading.Thread(target=_beat, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        return False


_VERSION_DIR_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


@dataclass(frozen=True)
class CandidateInspection:
    """Offline evidence for one candidate Claude artifact."""

    path: Path
    version: str
    sha256: str
    release: release_manifest.ReleaseProof | None = None


def _version_key(name: str) -> tuple[int, ...] | None:
    parts = name.split(".")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def inspect_candidate(
    path: Path, *, verify: Callable[[str, str, int], release_manifest.ReleaseProof] | None = None,
) -> CandidateInspection:
    """Shape, hash, signed release evidence (when supplied), then execution."""

    if not path.is_absolute():
        raise UpgradeError(f"candidate path {path} is not absolute")
    if not os.path.lexists(path):
        raise UpgradeError(f"candidate artifact {path} is missing")
    if path.is_symlink():
        raise UpgradeError(
            f"candidate artifact {path} is a symlink; inspect the resolved file"
        )
    if not path.is_file():
        raise UpgradeError(f"candidate artifact {path} is not a regular file")
    if not _VERSION_DIR_RE.fullmatch(path.name):
        raise UpgradeError(
            f"candidate basename {path.name!r} is not an X.Y.Z version"
        )
    if not os.access(path, os.X_OK):
        raise UpgradeError(f"candidate artifact {path} is not executable")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as artifact:
        while chunk := artifact.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    digest = digest.hexdigest()
    try:
        proof = verify(path.name, digest, size) if verify is not None else None
    except release_manifest.ReleaseManifestMismatch as exc:
        raise UpgradeError(f"release manifest mismatch ({exc})") from exc
    try:
        version_out = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/")},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpgradeError(f"candidate --version failed: {exc}") from exc
    if version_out.returncode != 0 or path.name not in version_out.stdout:
        raise UpgradeError(
            f"candidate --version did not report {path.name}: "
            f"{version_out.stdout.strip()!r}"
        )
    try:
        help_out = subprocess.run(
            [str(path), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/")},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpgradeError(f"candidate --help failed: {exc}") from exc
    if help_out.returncode != 0 or "Claude Code" not in help_out.stdout:
        raise UpgradeError("candidate --help does not look like Claude Code")
    return CandidateInspection(path=path, version=path.name, sha256=digest, release=proof)


def find_candidates(
    native_contract: Mapping[str, Any], environ: Mapping[str, str] | None = None,
) -> list[Path]:
    """Ordered candidate artifacts newer than the pin: the resolved target of
    the ``claude`` on PATH first, then every native versions directory's
    entries newest-first BY VERSION KEY (never lexically: name order puts
    2.1.99 ahead of 2.1.218). Callers inspect in order and skip invalid
    entries (an invalid highest name must not abort the whole repin)."""

    from . import acquire

    env = os.environ if environ is None else environ
    pinned = _version_key(pin.version(native_contract))
    if pinned is None:
        raise UpgradeError(f"pinned version {pin.version(native_contract)!r} is not X.Y.Z")
    ordered: list[Path] = []
    on_path = shutil.which("claude", path=env.get("PATH", os.defpath))
    if on_path:
        target = Path(os.path.realpath(on_path))
        key = _version_key(target.name)
        if key is not None and key > pinned:
            ordered.append(target)
    keyed: list[tuple[tuple[int, ...], Path]] = []
    for versions_dir in acquire.native_versions_dirs(env):
        try:
            entries = list(versions_dir.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            key = _version_key(entry.name)
            if key is None or key <= pinned:
                continue
            keyed.append((key, entry))
    keyed.sort(key=lambda item: item[0], reverse=True)
    for _key, entry in keyed:
        if entry not in ordered:
            ordered.append(entry)
    return ordered


def find_candidate(native_contract: Mapping[str, Any],
                   environ: Mapping[str, str] | None = None) -> Path | None:
    """Newest acceptable artifact (see :func:`find_candidates`)."""

    ordered = find_candidates(native_contract, environ)
    return ordered[0] if ordered else None


PENDING_RECEIPT = "0" * 64
# ``probe.REPIN_CANDIDATE_ENV`` (the probe module is not imported here).
REPIN_CANDIDATE_ENV = "CLAUDE_MULTI_REPIN_CANDIDATE"


# The bundled client defines its settings schema as one object literal of
# ``key: schema`` (newer builds: ``key: () => schema``) entries; the
# ``apiKeyHelper`` entry with its description appears nowhere else in that
# form. The keys are read without running the binary.
SETTINGS_SCHEMA_ANCHOR = re.compile(
    rb"apiKeyHelper:(?:\(\)=>)?[A-Za-z_$][A-Za-z0-9_$]*\(\)\.optional\(\)"
    rb"\.describe\(\"Path to a script that outputs authentication"
)
SETTINGS_KEYS_REQUIRED = frozenset({"apiKeyHelper", "env", "hooks", "model", "permissions"})
_SETTINGS_KEY = re.compile(rb"\s*(?:([A-Za-z_$][A-Za-z0-9_$]*)|\"([A-Za-z_$][A-Za-z0-9_$]*)\")\s*:")
_SETTINGS_SCAN_BACK = 1 << 16
_SETTINGS_SCAN_FORWARD = 1 << 22


def _skip_literal(data: Any, at: int, end: int) -> int:
    quote = data[at]
    at += 1
    while at < end and data[at] != quote:
        at += 2 if data[at] == 0x5C else 1  # backslash escapes one byte
    return at + 1


def _settings_object_keys(data: Any, start: int, anchor: int, end: int) -> list[str] | None:
    """Keys of the object literal opened at ``start`` when ``anchor`` is one
    of its own keys (depth 1), else None. Keys of a conditional spread
    (``...flag&&{key:...}``) count as well."""

    keys: list[str] = []
    depth = 0
    at = start
    expect_key = False
    spread = False  # inside a depth-1 spread item
    collect: list[int] = []  # depths whose keys are collected
    while at < end:
        byte = data[at]
        if at == anchor:
            if depth != 1:
                return None
            anchor = -1
        if expect_key and byte == 0x22:  # a quoted key
            match = _SETTINGS_KEY.match(data, at)
            if match is not None:
                keys.append((match.group(1) or match.group(2)).decode("ascii"))
                expect_key = False
                at = match.end()
                continue
        if byte in (0x22, 0x27, 0x60):  # " ' `
            at = _skip_literal(data, at, end)
            expect_key = False
            continue
        if byte in (0x7B, 0x28, 0x5B):  # { ( [
            depth += 1
            if byte == 0x7B and (depth == 1 or (depth == 2 and spread)):
                collect.append(depth)
                expect_key = True
            else:
                expect_key = False
            at += 1
            continue
        if byte in (0x7D, 0x29, 0x5D):  # } ) ]
            if collect and collect[-1] == depth:
                collect.pop()
            depth -= 1
            at += 1
            if depth == 0:
                return keys if anchor == -1 else None
            expect_key = False
            continue
        if byte == 0x2C and collect and collect[-1] == depth:  # ,
            expect_key = True
            if depth == 1:
                spread = False
            at += 1
            continue
        if expect_key:
            if data[at:at + 3] == b"...":
                spread = depth == 1
                expect_key = False
                at += 3
                continue
            match = _SETTINGS_KEY.match(data, at)
            if match is not None:
                keys.append((match.group(1) or match.group(2)).decode("ascii"))
                expect_key = False
                at = match.end()
                continue
            if byte not in b" \t\r\n":
                expect_key = False
        at += 1
    return None


def settings_keys_from_binary(path: Path | str) -> list[str]:
    """The top-level settings keys a Claude Code build knows, read statically.

    Finds the settings schema object literal in the bundled JavaScript and
    returns its keys, sorted. Never executes the file. Raises
    :class:`UpgradeError` when the schema cannot be found unambiguously or
    lacks the keys every build has."""

    try:
        with open(path, "rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            found_anchors = [match.start() for match in SETTINGS_SCHEMA_ANCHOR.finditer(data)]
            if len(found_anchors) != 1:
                raise UpgradeError(f"{path}: no single settings schema found; record its settings keys by hand")
            anchor = found_anchors[0]
            end = min(len(data), anchor + _SETTINGS_SCAN_FORWARD)
            start = anchor
            floor = max(0, anchor - _SETTINGS_SCAN_BACK)
            while True:
                start = data.rfind(b"{", floor, start)
                if start < 0:
                    raise UpgradeError(f"{path}: the settings schema object could not be delimited")
                keys = _settings_object_keys(data, start, anchor, end)
                if keys is not None:
                    break
    except OSError as exc:
        raise UpgradeError(f"cannot read {path}: {exc}") from exc
    found = sorted(set(keys))
    missing = SETTINGS_KEYS_REQUIRED - set(found)
    if missing:
        raise UpgradeError(f"{path}: the settings schema found lacks {', '.join(sorted(missing))}")
    return found


_SETTINGS_KEY_NAME = re.compile(r"^[$A-Za-z_][$A-Za-z0-9_]*$")
SETTINGS_KEYS_REMEDY = (
    "a new pin records the settings keys its build knows (the launch's settings-skew check "
    "depends on them): review the build's settings schema and pass the keys with "
    "--settings-keys FILE (a JSON array of key names)"
)


def read_settings_keys_file(path: Path | str) -> list[str]:
    """A reviewed settings-key inventory: a strict JSON array of the
    top-level settings key names a build knows, sorted. It must name the
    keys every build has; duplicates and anything else are refused."""

    try:
        document = strict_json.loads(Path(path).read_bytes())
    except (OSError, ValueError, errors.ClaudeMultiError) as exc:
        raise UpgradeError(f"cannot read the settings keys in {path}: {exc}") from exc
    if (not isinstance(document, list) or not document
            or not all(isinstance(key, str) and _SETTINGS_KEY_NAME.fullmatch(key) for key in document)):
        raise UpgradeError(f"{path} must be a JSON array of settings key names")
    if len(set(document)) != len(document):
        raise UpgradeError(f"{path} names a settings key more than once")
    missing = SETTINGS_KEYS_REQUIRED - set(document)
    if missing:
        raise UpgradeError(f"{path} lacks {', '.join(sorted(missing))}, which every build knows")
    return sorted(document)


def render_contract(
    prior: Mapping[str, Any], inspection: CandidateInspection, *, today: str,
    receipt_sha256: str = PENDING_RECEIPT, settings_keys: list[str] | None = None,
) -> dict[str, Any]:
    """The next contract v2: the candidate's signed builds, behaviour carried.

    Every platform build the signed manifest lists is recorded; the evidence
    class is ``battery`` for the platform the essential battery runs on (this
    host) and ``identity+smoke`` for the others. ``receipt_sha256`` is the
    digest of the battery output (written after the suite passes).

    ``settings_keys`` (the settings keys the candidate knows) is recorded
    when given; otherwise a re-recording of the same version carries the
    prior entry's keys. A new version without keys is refused: the launch's
    settings-skew safeguard needs them.
    """

    proof = inspection.release
    if proof is None or not proof.platforms:
        raise UpgradeError(
            f"candidate {inspection.version} has no verified signed-manifest builds; "
            "the next contract records Anthropic's signed per-platform sizes and hashes"
        )
    contract = json.loads(json.dumps(prior))
    contract["version"] = pin.CONTRACT_VERSION
    contract.pop("claude", None)
    contract["verified"] = [{
        "version": inspection.version,
        "platforms": {name: dict(proof.platforms[name]) for name in sorted(proof.platforms)},
        "manifest_sha256": proof.manifest_sha256,
        "signature_sha256": proof.signature_sha256,
        "key_fingerprint": proof.fingerprint,
        "verified_at": today,
        "evidence": {proof.platform: "battery", "others": "identity+smoke",
                     "receipt_sha256": receipt_sha256},
    }]
    try:
        previous = pin.entry(prior)
    except (KeyError, TypeError, ValueError):
        previous = {}
    same_version = previous.get("version") == inspection.version
    if settings_keys is None and same_version and isinstance(previous.get("settings_keys"), list):
        settings_keys = list(previous["settings_keys"])
    if settings_keys:
        contract["verified"][0]["settings_keys"] = sorted(set(settings_keys))
    elif not same_version:
        raise UpgradeError(f"Claude Code {inspection.version} has no settings keys recorded; "
                           + SETTINGS_KEYS_REMEDY)
    lifecycle = contract.setdefault("lifecycle_evidence", {})
    lifecycle["inspected_version"] = inspection.version
    evidence_id = lifecycle.get("evidence_id")
    if isinstance(evidence_id, str) and evidence_id:
        lifecycle["evidence_id"] = re.sub(
            r"[0-9]+\.[0-9]+\.[0-9]+$", inspection.version, evidence_id
        )
    return contract


# A declined request plan sends nothing and changes nothing.
FETCH_DECLINED = "repin: declined — nothing sent; nothing was changed"
CANDIDATE_CHANGED = ("repin stopped: {what} changed while the candidate was being verified — "
                     "nothing was changed; rerun claude-multi-dev repin")


@dataclass(frozen=True)
class _Selection:
    """The lock-free candidate selection: what was inspected and verified,
    and the fingerprints the repin transaction revalidates."""

    inspection: CandidateInspection | None
    skipped: tuple[str, ...]
    size: int | None
    checkout: str
    declined: str | None = None


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"


def _checkout_fingerprint(product_root: Path) -> str:
    """The checkout state a promotion depends on (no secrets): resource-relative
    labels with the bytes of the checkout's resources."""

    resources = layout.checkout_resources(product_root)
    parts = [f"{name}={_file_digest(resources / name)}" for name in
             ("catalog/native-contract.json", "version.json", "schemas/native-contract.schema.json")]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _artifact_identity(path: Path) -> tuple[str, int] | None:
    """(sha256, size) of the still-regular, non-symlink artifact, else None."""

    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return None
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as artifact:
            while chunk := artifact.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size
    except OSError:
        return None


def _offline_hint(version: str) -> str:
    url = release_manifest.MANIFEST_URL.format(version=version)
    return (f"Offline: fetch {url} and {url}.sig into DIR/{version}/ and run "
            "claude-multi-dev repin --manifest-dir DIR")


def _select_candidate(
    native_contract: Mapping[str, Any], product_root: Path, *,
    running: ReleaseIdentity | None, environ: Mapping[str, str],
    release_verifier: Callable[[str, str, int], release_manifest.ReleaseProof],
) -> _Selection:
    """Inspect, consent, fetch and verify OUTSIDE the repin transaction: no
    lock is held while the maintainer reads a request plan or while the
    network is used. Each candidate gets its own plan; a mismatch moves to
    the next candidate (which asks again), unavailable evidence stops, a
    decline sends nothing."""

    candidates = find_candidates(native_contract, environ)
    if candidates:
        checkout_guard(product_root, running or running_release(), writes=True)
    checkout = _checkout_fingerprint(product_root)
    skipped: list[str] = []
    for entry in candidates:
        try:
            inspection = inspect_candidate(entry, verify=release_verifier)
        except release_manifest.ReleaseFetchDeclined:
            return _Selection(None, tuple(skipped), None, checkout,
                              declined=f"{FETCH_DECLINED}. {_offline_hint(entry.name)}")
        except release_manifest.ReleaseManifestUnavailable as exc:
            raise UpgradeError(
                f"repin stopped: cannot verify {entry.name} against Anthropic's signed "
                f"release manifest ({exc}); nothing was changed. {_offline_hint(entry.name)}"
            ) from exc
        except UpgradeError as exc:
            skipped.append(f"{entry.name}: {exc}")
            continue
        size = inspection.release.size if inspection.release is not None else None
        if size is None:
            identity = _artifact_identity(entry)
            size = identity[1] if identity is not None else None
        return _Selection(inspection, tuple(skipped), size, checkout)
    return _Selection(None, tuple(skipped), None, checkout)


def _revalidate(selection: _Selection, product_root: Path, *, running: ReleaseIdentity | None) -> None:
    """Inside the repin transaction, before any promotion: the verified
    artifact bytes and the checkout state are still exactly what was
    inspected."""

    if _checkout_fingerprint(product_root) != selection.checkout:
        raise UpgradeError(CANDIDATE_CHANGED.format(what="the source checkout"))
    inspection = selection.inspection
    if inspection is None:
        return
    checkout_guard(product_root, running or running_release(), writes=True)
    if _artifact_identity(inspection.path) != (inspection.sha256, selection.size):
        raise UpgradeError(CANDIDATE_CHANGED.format(what=f"the candidate artifact {inspection.path}"))


@dataclass(frozen=True)
class UpgradeOutcome:
    """What happened."""

    kind: str  # "current" | "prepared" | "fetch-declined"
    inspection: CandidateInspection | None
    messages: tuple[str, ...]


_SYNC_TEST_FILES = ("tests/test_catalog.py", "tests/test_native_contract.py")


def _sync_pinned_test_literals(
    product_root: Path,
    *,
    old_version: str,
    new_version: str,
    old_sha256: str | None,
    new_sha256: str,
    old_inspected_at: str | None,
    new_inspected_at: str,
    old_catalog_version: int,
    final_catalog_version: int,
    backups: dict[Path, bytes],
    old_platforms: Mapping[str, Mapping[str, Any]] | None = None,
    new_platforms: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """Move the suite's deliberate version pins to the promoted contract.

    Several tests pin the reviewed baseline on purpose (the re-pin commit's
    diff IS the review trail): the pinned version, this platform's sha256,
    every platform build's sha256 and size (``old_platforms`` →
    ``new_platforms``, the signed records; a size moves only in the
    ``["<platform>"]["size"], <n>)`` assertion form), the verification date
    and the ``catalog_version`` literal. If promotion
    did not move them, the evidence suite would fail against the very
    contract it is meant to validate. Lines that are intentionally decoupled
    from the artifact version (the per-model ``floors = {...}`` minimums)
    are left untouched. Original bytes are recorded in ``backups`` for the
    fail-closed restore.

    The old and final ``catalog_version`` are both explicit (never
    ``final = old + 1``): a checkout already ahead of the running release
    keeps its literal, and only an actual bump moves it.
    """

    synced: list[str] = []
    for relative in _SYNC_TEST_FILES:
        path = product_root / relative
        if not path.is_file():
            continue
        original = path.read_bytes()
        lines = original.decode("utf-8").splitlines(keepends=True)
        changed: list[str] = []
        for line in lines:
            if "floors" in line:
                changed.append(line)  # model minimums are decoupled by design
                continue
            # Boundary-anchored: the old pin must never rewrite inside a
            # longer literal (e.g. old 2.1.2 mangling 2.1.216 into 2.1.2016).
            updated = re.sub(
                r"(?<![0-9.])" + re.escape(old_version) + r"(?![0-9])",
                new_version,
                line,
            )
            if old_sha256 and old_sha256 != new_sha256:
                updated = updated.replace(old_sha256, new_sha256)
            for platform, before in (old_platforms or {}).items():
                after = (new_platforms or {}).get(platform)
                if not isinstance(after, Mapping):
                    continue
                if before.get("sha256") and before.get("sha256") != after.get("sha256"):
                    updated = updated.replace(str(before["sha256"]), str(after["sha256"]))
                if before.get("size") is not None and before.get("size") != after.get("size"):
                    updated = updated.replace(
                        f'["{platform}"]["size"], {before["size"]})',
                        f'["{platform}"]["size"], {after["size"]})',
                    )
            if old_inspected_at and old_inspected_at != new_inspected_at:
                updated = updated.replace(
                    f'"{old_inspected_at}"', f'"{new_inspected_at}"'
                )
            if final_catalog_version != old_catalog_version:
                updated = updated.replace(
                    f'"catalog_version"], {old_catalog_version})',
                    f'"catalog_version"], {final_catalog_version})',
                )
            changed.append(updated)
        new_text = "".join(changed)
        if new_text.encode("utf-8") != original:
            backups.setdefault(path, original)
            _write_checkout_file(product_root, path, new_text.encode("utf-8"))
            synced.append(relative)
    return synced


def promoted_catalog_version(checkout: int, running: int) -> int:
    """The checkout's ``catalog_version`` after promotion.

    Equal to the running release's: bump once (the re-pin is new trusted
    content). Already ahead (a development checkout whose catalog was bumped
    for unreleased content): keep it, so promotion is idempotent and never
    double-bumps. Behind: refuse (``checkout_guard`` already refuses that
    before any write; this is the defensive restatement)."""

    if checkout == running:
        return checkout + 1
    if checkout > running:
        return checkout
    raise UpgradeError(
        f"repin refused: the checkout's catalog_version {checkout} is older than the "
        f"running release's {running}"
    )


def default_checkout() -> Path:
    """The tree this ``claude-multi-dev`` runs from (``--repo`` default); the
    caller verifies it is a writable source checkout. Never the packaged
    resources."""

    tree = layout.installation()
    if tree is None:
        raise UpgradeError("this claude-multi-dev runs from no source checkout — pass --repo PATH")
    return tree


def _lock_path(environ: Mapping[str, str]) -> Path:
    from . import state

    return state.ensure_private_dir(paths.state_root(dict(environ)) / "locks") / "repin"


def run_repin(
    *,
    checkout_root: Path,
    native_contract: Mapping[str, Any],
    today: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    env: dict[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
    running: ReleaseIdentity | None = None,
    manifest_dir: Path | None = None,
    release_verifier: Callable[[str, str, int], release_manifest.ReleaseProof] | None = None,
    git: Callable | None = None,
    fetch_consent: FetchConsent | None = None,
    settings_keys: list[str] | None = None,
) -> UpgradeOutcome:
    """Verify → inspect → promote → evidence.

    Candidate inspection, the per-candidate request-plan consent
    (``fetch_consent``; without it a network verification sends nothing and
    the outcome is ``fetch-declined``), the fetch and the signature check
    run before the repin transaction is taken; inside it the artifact hash
    and the checkout state are revalidated before any write.

    ``settings_keys`` is a reviewed settings-key inventory
    (:func:`read_settings_keys_file`) recorded instead of the keys read
    from the candidate; without it a candidate whose keys cannot be read
    is refused before anything is written.

    ``progress`` receives one line per long phase. Two repins of one user
    queue on an advisory lock under the state root.
    """

    from . import state  # local import: hardened writes for the state root

    def _note(line: str) -> None:
        if progress is not None:
            progress(line)

    environ = dict(os.environ if env is None else env)
    verifier = release_verifier or _release_verifier(manifest_dir, environ, consent=fetch_consent)
    product_root = Path(checkout_root)
    selection = _select_candidate(native_contract, product_root, running=running,
                                  environ=environ, release_verifier=verifier)
    if selection.declined is not None:
        return UpgradeOutcome(kind="fetch-declined", inspection=None, messages=(selection.declined,))
    lock = state.FileLock(_lock_path(environ))
    if not lock.acquire(blocking=False):
        _note("another claude-multi-dev repin is running; waiting for it…")
        lock.acquire(blocking=True)
    try:
        return _run_repin_locked(
            checkout_root=product_root,
            native_contract=native_contract,
            today=today,
            runner=runner,
            env=env,
            note=_note,
            progress=progress,
            running=running,
            selection=selection,
            git=subprocess.run if git is None else git,
            settings_keys=settings_keys,
        )
    finally:
        lock.release()


def _run_repin_locked(
    *,
    checkout_root: Path,
    native_contract: Mapping[str, Any],
    today: str,
    runner: Callable[..., subprocess.CompletedProcess],
    env: dict[str, str] | None,
    note: Callable[[str], None],
    progress: Callable[[str], None] | None,
    running: ReleaseIdentity | None,
    selection: _Selection,
    git: Callable,
    settings_keys: list[str] | None = None,
) -> UpgradeOutcome:
    _note = note

    product_root = Path(checkout_root)
    # Inspection, consent, fetch and verification already ran lock-free;
    # nothing below writes before this revalidation.
    _revalidate(selection, product_root, running=running)
    inspection = selection.inspection
    skipped = list(selection.skipped)
    skip_note = (
        "skipped unusable candidate artifact(s): " + "; ".join(skipped)
        if skipped
        else None
    )
    if inspection is None:
        messages = [
            f"pinned Claude Code {pin.version(native_contract)} is the newest installed "
            "artifact; nothing to re-pin."
        ]
        if skip_note is not None:
            messages.append(skip_note)
        return UpgradeOutcome(kind="current", inspection=None, messages=tuple(messages))
    total = 4
    _note(f"[1/{total}] candidate {inspection.version} verified and inspected offline "
          "(signed manifest, --version, --help, sha256)")
    resources = layout.checkout_resources(product_root)
    contract_path = resources / "catalog" / "native-contract.json"
    version_path = resources / "version.json"
    if not (
        contract_path.is_file()
        and version_path.is_file()
        and (product_root / "tests").is_dir()
    ):
        raise UpgradeError(
            f"{product_root} is not a claude-multi source checkout with tests; "
            "the evidence suite needs the checkout — pass --repo with a source checkout"
        )
    for destination in (contract_path, version_path):
        # Before any write: a symlinked resource directory refuses here.
        try:
            layout.checkout_destination(product_root, destination)
        except layout.LayoutError as exc:
            raise UpgradeError(f"repin refused: {exc}") from None
    contract_backup = contract_path.read_bytes()
    version_backup = version_path.read_bytes()
    sync_backups: dict[Path, bytes] = {}

    # The settings keys the candidate knows: a reviewed list when given,
    # else read from the verified file without running it. A new pin
    # without them is refused before anything is written.
    if settings_keys is not None:
        candidate_keys = sorted(set(settings_keys))
        keys_note = f"settings keys: {len(candidate_keys)} recorded from the reviewed list"
    else:
        try:
            candidate_keys = settings_keys_from_binary(inspection.path)
        except UpgradeError as exc:
            raise UpgradeError(f"{exc}; {SETTINGS_KEYS_REMEDY} — nothing was changed") from exc
        keys_note = f"settings keys: {len(candidate_keys)} recorded (read from the candidate, never run)"
    promoted = render_contract(native_contract, inspection, today=today, settings_keys=candidate_keys)
    version_doc = json.loads(version_backup.decode("utf-8"))
    old_catalog_version = int(version_doc["catalog_version"])
    final_catalog_version = promoted_catalog_version(
        old_catalog_version, (running or running_release()).catalog_version)
    version_doc["catalog_version"] = final_catalog_version
    proof = inspection.release
    assert proof is not None  # render_contract refuses an unverified candidate
    old_version = pin.version(native_contract)
    old_record = pin.platform_record(native_contract, proof.platform)
    old_verified_at = pin.entry(native_contract).get("verified_at")
    messages = [
        f"candidate: {inspection.path}",
        f"inspected offline: --version ok, --help ok, sha256 {inspection.sha256[:12]}...",
        f"release manifest: {proof.platform} sha256 and size match Anthropic's signed "
        f"manifest for {proof.version} (signing key …{proof.fingerprint[-16:]}); "
        f"{len(proof.platforms or {})} platform builds recorded",
        keys_note,
    ]
    if skip_note is not None:
        messages.append(skip_note)

    def _write_contract(document: Mapping[str, Any]) -> None:
        _write_checkout_file(product_root, contract_path,
                             (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"))

    try:
        _write_contract(promoted)
        _write_checkout_file(
            product_root,
            version_path,
            (json.dumps(version_doc, indent=2) + "\n").encode("utf-8"),
        )
        synced = _sync_pinned_test_literals(
            product_root,
            old_version=old_version,
            new_version=inspection.version,
            old_sha256=old_record["sha256"] if old_record is not None else None,
            new_sha256=inspection.sha256,
            old_inspected_at=old_verified_at if isinstance(old_verified_at, str) else None,
            new_inspected_at=today,
            old_catalog_version=old_catalog_version,
            final_catalog_version=final_catalog_version,
            backups=sync_backups,
            old_platforms=pin.entry(native_contract).get("platforms"),
            new_platforms=proof.platforms,
        )
        if synced:
            messages.append(
                "synced version-pinned test literals (" + ", ".join(synced) + ")"
            )
        _note(
            f"[2/{total}] promoted into the checkout; version-pinned test "
            "literals synced"
        )
        _note(
            f"[3/{total}] running the offline evidence suite against the "
            "candidate (several minutes; the suite is silent between updates)…"
        )
        with _Heartbeat(progress, f"  [3/{total}] evidence suite still running"):
            evidence = runner(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                cwd=product_root,
                env={
                    **(os.environ if env is None else env),
                    "PYTHONPATH": "src:tests",
                    # The verified candidate, wherever it was found: the
                    # probes accept it only if it matches the contract.
                    REPIN_CANDIDATE_ENV: str(inspection.path),
                },
                capture_output=True,
                text=True,
                timeout=1800,
            )
        tail = (evidence.stdout or "") + (evidence.stderr or "")
        if evidence.returncode != 0:
            failures = re.findall(r"(?m)^(?:ERROR|FAIL): .+$", tail)
            raise UpgradeError(
                "the offline evidence suite failed against the candidate "
                "contract; the checkout was restored unchanged.\n"
                + ("Failing tests:\n" + "\n".join(failures) + "\n" if failures else "")
                + "Failing tail:\n"
                + "\n".join(tail.strip().splitlines()[-15:])
            )
        missing = _missing_evidence_prefixes(tail)
        if missing:
            raise UpgradeError(
                "the offline evidence suite exited zero but essential native-probe "
                "completion evidence is missing; the checkout was restored unchanged. "
                "Missing anchored prefix(es): "
                + ", ".join(repr(prefix) for prefix in missing)
            )
        receipt = hashlib.sha256(tail.encode("utf-8")).hexdigest()
        _write_contract(render_contract(native_contract, inspection, today=today, receipt_sha256=receipt,
                                        settings_keys=candidate_keys))
        messages.extend(_diagnostic_notes(tail, inspection.version))
        ran = re.search(r"Ran (\d+) tests", tail)
        messages.append(
            f"evidence: offline suite green ({ran.group(1) if ran else '?'} tests, "
            f"all {len(ESSENTIAL_EVIDENCE_PREFIXES)} essential native probes "
            f"completed positively; receipt sha256 {receipt[:12]}…)"
        )
    except BaseException:
        # The restore runs precisely when things already failed — its
        # writes must be just as crash-atomic as the promotion's.
        _write_checkout_file(product_root, contract_path, contract_backup)
        _write_checkout_file(product_root, version_path, version_backup)
        for synced_path, synced_bytes in sync_backups.items():
            _write_checkout_file(product_root, synced_path, synced_bytes)
        raise

    _note(f"[4/{total}] evidence green; the checkout pins Claude Code {inspection.version}")
    messages.append(
        f"promoted into the source checkout (catalog_version {version_doc['catalog_version']}); "
        "a build of this checkout runs the new pin after `claude-multi setup --step claude`"
    )
    repo = product_root
    changed = [str(path.relative_to(repo)) for path in (contract_path, version_path)]
    changed += [str((product_root / relative).relative_to(repo)) for relative in synced]
    messages.extend(_changed_paths_report(repo, changed, git))
    subject = f"build(pin): pin Claude Code {inspection.version}"
    messages.append(f"commit: git -C {shlex.quote(str(repo))} commit -m {shlex.quote(subject)} -- {shlex.join(changed)}")
    messages.append("install: the new pin ships with the next release built from this checkout "
                    "(tools/build.py release); this command builds and installs nothing")
    return UpgradeOutcome(kind="prepared", inspection=inspection, messages=tuple(messages))
