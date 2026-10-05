#!/usr/bin/env python3
"""Release a claude-multi version: check, build, reproduce, sign, verify, publish.

    python3 tools/release.py preflight --receipt FILE [--previous TAG|none]
    python3 tools/release.py build --out DIR
    python3 tools/release.py reproduce --out DIR
    python3 tools/release.py sign --out DIR [--key FILE]
    python3 tools/release.py verify-draft --out DIR [--allowed-signers FILE] [--client FILE]
    python3 tools/release.py publish --out DIR --tag vX.Y.Z

preflight    the release conditions, each reported: a clean tree; a release
             version (no -dev) newer than the previous release tag, with the
             catalog number moved when the catalog changed; a dated CHANGELOG
             section; no drafting markers in public documentation;
             the Claude Code contract (format 2, every bundle
             platform pinned, settings keys recorded); the gateway series
             identical in gateway/UPSTREAM.json, the catalog and the packaged
             contract; the notices re-rendered (tests.test_third_party_notices);
             the hygiene tests; the history scan; the production release key
             in the packaged trust; the release locations in
             packaging/product.json (both https, {version} in the download
             location only); and the release-level gate receipt for this
             commit (--receipt: verdict PASS, level release, head = HEAD,
             clean tree)
build        `tools/build.py release --out DIR` with the commit time as the
             source date; SHA256SUMS must list exactly the release (the
             manifest, its archives and both installers), each file matching
reproduce    clone this commit into a scratch directory, build it there and
             compare with DIR (`build.py release compare`): DIR's checksums
             must match its files, and the bundle contents, the manifest and
             both installers must be identical
sign         print the command that signs DIR/SHA256SUMS with the release key
             (`ssh-keygen -Y sign`, namespace claude-multi-release); this
             tool never reads or holds the key. A release whose MANIFEST.json
             names no release key or no release location (a test build) is
             refused here, by verify-draft without --allowed-signers and by
             publish
verify-draft check DIR/SHA256SUMS.sshsig against the packaged release trust
             (or --allowed-signers, for a test key), that SHA256SUMS lists
             exactly the release (MANIFEST.json, its archives and both
             installers), every listed file's checksum and the manifest,
             then install DIR with its install.sh
             into a temporary HOME, as a new user would, and run the
             installed release: both launchers' --version must name it, and
             the fixture journey (.github/scripts/journey.sh installed) must
             pass: the first-run checks of the new installation (doctor
             --first-run: ready, or not ready only for what a fresh install
             has not set up yet — Claude Code, the gateway, providers, so
             models and the profile wait, and the session hooks —, each
             matched exactly; any other nonzero exit fails), the on-demand
             gateway lifecycle and its loopback confinement, a first managed
             turn against a loopback fixture provider and its resume (with
             --client, the pinned Claude Code), another program on the
             gateway port refused, and an uninstall that keeps the state;
             no provider is ever called
publish      check that the draft release TAG holds exactly DIR's files
             (every file SHA256SUMS lists — the archives, MANIFEST.json and
             both installers — and SHA256SUMS, byte for byte; a stale
             signature may be there), then, after a
             typed confirmation on the terminal, attach SHA256SUMS.sshsig,
             download and compare every file of the draft again (sha256
             and size), signature included, and publish it (gh); a draft
             holding anything else, then or right before the promotion, is
             refused and stays a draft

Exit status: 0 done, 1 refused or failed (the message says why), 2 usage,
3 declined at the confirmation, 130 cancelled. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "claude-multi-release"
PRINCIPAL = "release@claude-multi"
SUMS = "SHA256SUMS"
SIGNATURE = "SHA256SUMS.sshsig"
MANIFEST = "MANIFEST.json"
JOURNEY = Path(".github") / "scripts" / "journey.sh"
JOURNEY_TIMEOUT = 20 * 60
FIRST_RUN_OK = "first run: ok"  # the journey's line for a successful first-run diagnostic
RELEASE_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
ANY_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-dev)?")
RESOURCES = Path("src") / "claude_multi" / "data"
EXIT_REFUSED, EXIT_USAGE, EXIT_DECLINED, EXIT_CANCELLED = 1, 2, 3, 130

Runner = Callable[..., subprocess.CompletedProcess]


class ReleaseError(Exception):
    """A release step cannot proceed; the message says why."""


class Declined(ReleaseError):
    """The maintainer declined at a confirmation."""


def say(message: str) -> None:
    print(f"release: {message}", file=sys.stderr, flush=True)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ReleaseError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_BUILD_TOOLS: dict[Path, object] = {}


def build_tool(repo: Path):
    """The repository's tools/build.py, as a module (loaded once per tree)."""

    if repo not in _BUILD_TOOLS:
        _BUILD_TOOLS[repo] = _load(repo / "tools" / "build.py", f"_claude_multi_release_build_{len(_BUILD_TOOLS)}")
    return _BUILD_TOOLS[repo]


def trust_module(repo: Path):
    """The repository's own signature verifier (src/claude_multi/trust.py)."""

    src = str(repo / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from claude_multi import trust

    return trust


def run(argv: Sequence[str], *, cwd: Path, env: dict[str, str] | None = None,
        timeout: float = 3600, process_group: bool = False) -> subprocess.CompletedProcess:
    if not process_group:
        return subprocess.run(list(argv), cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False)
    # Wait for the shell, not EOF on a pipe held by one of its descendants.
    # Regular files also bound collection if a descendant escapes the group.
    with tempfile.TemporaryDirectory(prefix="journey-helpers-") as helpers, \
            tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        child_env = {**(os.environ if env is None else env), "JOURNEY_HELPERS_DIR": helpers}
        child = subprocess.Popen(list(argv), cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                                 stdout=stdout, stderr=stderr, start_new_session=True)
        timed_out = False
        try:
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
        finally:
            # On timeout/cancellation, let the shell's EXIT trap stop its
            # helper groups. Signal the group so a blocking child also exits.
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
            # Helpers register before leaving the shell's group, so killing
            # that group above closes the registration race. Normal shell
            # cleanup removes entries after wait; forced cleanup stops only
            # the registered groups, without a system-wide process listing.
            for entry in Path(helpers).iterdir():
                try:
                    os.killpg(int(entry.name), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        captured = []
        for stream in (stdout, stderr):
            size = os.fstat(stream.fileno()).st_size
            stream.seek(0)
            captured.append(stream.read(size).decode("utf-8", errors="replace"))
        if timed_out:
            captured[1] += f"\njourney timed out after {timeout:g} seconds\n"
        return subprocess.CompletedProcess(list(argv), 124 if timed_out else child.returncode, *captured)


def git(repo: Path, *args: str, runner: Runner = run) -> str:
    result = runner(["git", "-C", str(repo), *args], cwd=repo)
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip()[-400:]}")
    return result.stdout


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from exc


def version_key(version: str) -> tuple[int, int, int, int]:
    match = ANY_VERSION.fullmatch(version)
    if match is None:
        raise ReleaseError(f"not a claude-multi version: {version!r}")
    major, minor, patch, dev = match.groups()
    return int(major), int(minor), int(patch), 0 if dev else 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ preflight


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def check_clean_tree(repo: Path, runner: Runner = run) -> Check:
    status = git(repo, "status", "--porcelain", "--untracked-files=normal", runner=runner)
    if status.strip():
        lines = status.strip().splitlines()
        return Check("clean-tree", False, f"{len(lines)} uncommitted path(s), e.g. {lines[0].strip()}")
    return Check("clean-tree", True, "nothing uncommitted")


def release_version(repo: Path) -> tuple[str, int]:
    document = read_json(repo / RESOURCES / "version.json")
    if not isinstance(document, dict):
        raise ReleaseError("version.json is not an object")
    version, catalog = document.get("launcher_version"), document.get("catalog_version")
    if not isinstance(version, str) or not isinstance(catalog, int) or isinstance(catalog, bool):
        raise ReleaseError("version.json lacks launcher_version or catalog_version")
    return version, catalog


def check_version(repo: Path) -> Check:
    version, catalog = release_version(repo)
    if RELEASE_VERSION.fullmatch(version) is None:
        return Check("version", False, f"{version} is not a release version (set X.Y.Z in version.json)")
    if catalog < 1:
        return Check("version", False, f"catalog_version {catalog} is not positive")
    return Check("version", True, f"{version} (catalog {catalog})")


def previous_release(repo: Path, version: str, runner: Runner = run) -> str | None:
    """The newest release tag (vX.Y.Z) that is an ancestor of HEAD, other
    than this version's own tag."""

    tags = [tag for tag in git(repo, "tag", "--merged", "HEAD", "--list", "v*", runner=runner).split()
            if RELEASE_VERSION.fullmatch(tag[1:]) and tag != f"v{version}"]
    return max(tags, key=lambda tag: version_key(tag[1:]), default=None)


def check_version_bump(repo: Path, previous: str | None, runner: Runner = run) -> Check:
    version, catalog = release_version(repo)
    own = git(repo, "tag", "--list", f"v{version}", runner=runner).strip()
    if own:
        tagged = git(repo, "rev-list", "-n", "1", own, runner=runner).strip()
        head = git(repo, "rev-parse", "HEAD", runner=runner).strip()
        if tagged != head:
            return Check("version-bump", False, f"the tag {own} exists and names another commit")
    if previous is None:
        return Check("version-bump", True, "the first release (no previous release tag)")
    if version_key(previous[1:]) >= version_key(version):
        return Check("version-bump", False, f"{version} is not newer than the previous release {previous} "
                                            "(name another with --previous, or none for a first release)")
    old = json.loads(git(repo, "show", f"{previous}:{(RESOURCES / 'version.json').as_posix()}", runner=runner))
    changed = runner(["git", "-C", str(repo), "diff", "--quiet", previous, "HEAD", "--",
                      (RESOURCES / "catalog").as_posix()], cwd=repo).returncode != 0
    if changed and old.get("catalog_version") == catalog:
        return Check("version-bump", False, f"the catalog changed since {previous}, but catalog_version is still "
                                            f"{catalog}")
    return Check("version-bump", True, f"after {previous}" + (f"; catalog {old.get('catalog_version')} -> {catalog}"
                                                                if changed else "; catalog unchanged"))


def check_changelog(repo: Path) -> Check:
    version, _ = release_version(repo)
    try:
        text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError as exc:
        return Check("changelog", False, f"CHANGELOG.md: {exc}")
    heading = re.search(rf"(?m)^## \[{re.escape(version)}\](.*)$", text)
    if heading is None:
        return Check("changelog", False, f"CHANGELOG.md has no section for {version}")
    if "unreleased" in heading.group(1).lower() or not re.search(r"\d{4}-\d{2}-\d{2}", heading.group(1)):
        return Check("changelog", False, f"the {version} section is not dated (## [{version}] — YYYY-MM-DD)")
    return Check("changelog", True, heading.group(0).strip())


def check_drafting_markers(repo: Path) -> Check:
    """Public documentation must contain no drafting markers at release."""

    old_path = sys.path[:]
    try:
        sys.path.insert(0, str(REPO_ROOT / "tests"))
        vocab = _load(REPO_ROOT / "tests" / "_docs_vocab.py", "_claude_multi_release_docs")
    finally:
        sys.path[:] = old_path
    problems = []
    for name in (*vocab.ROOT_PAGES, *vocab.docs_pages(repo / "docs")):
        path = repo / name if name in vocab.ROOT_PAGES else repo / "docs" / name
        text = path.read_text(encoding="utf-8")
        problems.extend(f"{path.relative_to(repo).as_posix()}:{line}"
                        for line, _owner, _body in vocab.todo_markers(text))
    if problems:
        return Check("drafting-markers", False, "drafting markers remain: " + "; ".join(problems))
    return Check("drafting-markers", True, "no drafting markers in public documentation")


def check_contract(repo: Path) -> Check:
    contract = read_json(repo / RESOURCES / "catalog" / "native-contract.json")
    product = read_json(repo / "packaging" / "product.json")
    if not isinstance(contract, dict) or contract.get("version") != 2:
        return Check("contract", False, "native-contract.json is not format 2")
    tool = build_tool(repo)
    try:
        pin = tool.claude_pin(contract)
    except tool.BuildError as exc:
        return Check("contract", False, str(exc))
    platforms = pin.get("platforms") if isinstance(pin.get("platforms"), dict) else {}
    wanted = sorted(entry["claude_code"] for entry in product["targets"].values())
    missing = [name for name in wanted if not isinstance(platforms.get(name), dict)
               or not re.fullmatch(r"[0-9a-f]{64}", str(platforms[name].get("sha256", "")))
               or not isinstance(platforms[name].get("size"), int)]
    if missing:
        return Check("contract", False, f"Claude Code {pin.get('version')} has no verified build for {missing}")
    if not pin.get("settings_keys") or not pin.get("evidence"):
        return Check("contract", False, f"Claude Code {pin.get('version')} lacks its settings keys or evidence")
    return Check("contract", True, f"Claude Code {pin['version']}, {len(wanted)} bundle platforms")


def check_gateway_parity(repo: Path) -> Check:
    tool = build_tool(repo)
    doc = read_json(repo / "gateway" / "UPSTREAM.json")
    catalog = read_json(repo / RESOURCES / "catalog" / "gateway.json")
    try:
        tool.check_upstream(doc)
    except tool.BuildError as exc:
        return Check("gateway-parity", False, str(exc))
    admitted = tool.admitted_series(doc)
    names = [entry["basename"] for entry in admitted]
    if catalog["gateway"]["patches"] != names:
        return Check("gateway-parity", False, "catalog/gateway.json lists another patch series than UPSTREAM.json")
    if catalog["gateway"]["cliproxyapi_baseline"] != doc["upstream"]["version"]:
        return Check("gateway-parity", False, "catalog/gateway.json names another upstream version")
    contract = tool.contract_bytes(tool.contract_document(doc, admitted))
    if (repo / RESOURCES / "gateway-contract.json").read_bytes() != contract:
        return Check("gateway-parity", False, "the packaged gateway-contract.json is stale "
                                              "(python3 tools/build.py gateway contract)")
    for entry in admitted:
        path = repo / "gateway" / "patches" / entry["basename"]
        if not path.is_file() or sha256_file(path) != entry["sha256"]:
            return Check("gateway-parity", False, f"{entry['basename']} does not match UPSTREAM.json")
    return Check("gateway-parity", True, f"{len(names)} patches on {doc['upstream']['name']} "
                                         f"{doc['upstream']['version']}")


def _test_check(name: str, module: str, repo: Path, runner: Runner) -> Check:
    environment = {**os.environ, "PYTHONPATH": f"{repo / 'src'}{os.pathsep}{repo / 'tests'}",
                   "PYTHONDONTWRITEBYTECODE": "1"}
    result = runner([sys.executable, "-m", "unittest", module], cwd=repo, env=environment)
    tail = (result.stdout + result.stderr).strip().splitlines()[-1:] or ["no output"]
    return Check(name, result.returncode == 0, tail[0])


def check_notices(repo: Path, runner: Runner = run) -> Check:
    return _test_check("notices", "tests.test_third_party_notices", repo, runner)


def check_hygiene(repo: Path, runner: Runner = run) -> Check:
    return _test_check("hygiene", "tests.test_hygiene", repo, runner)


# The private identifier list (tools/history_scan.py PRIVATE_INPUT_ENV): the
# history scan looks for it when the environment names it, otherwise for
# the generic identifiers only. The hygiene check inherits the variable.
PRIVATE_IDENTIFIERS_ENV = "CLAUDE_MULTI_PRIVATE_IDENTIFIERS"


def check_history(repo: Path, runner: Runner = run, environ: dict[str, str] | None = None) -> Check:
    private = (os.environ if environ is None else environ).get(PRIVATE_IDENTIFIERS_ENV, "")
    with tempfile.TemporaryDirectory(prefix="release-history-") as out:
        result = runner([sys.executable, str(repo / "tools" / "history_scan.py"), "--repo", str(repo),
                         "--out", out, *(["--identifiers", private] if private else [])], cwd=repo)
    tail = (result.stdout + result.stderr).strip().splitlines()[-1:] or ["no output"]
    return Check("history", result.returncode == 0, tail[0])


def check_trust(repo: Path) -> Check:
    trust = trust_module(repo)
    path = repo / RESOURCES / "release-trust" / "allowed_signers"
    try:
        signers = trust.release_signers(path)
    except trust.TrustError as exc:
        return Check("trust", False, f"{exc}; add the production public key to {path.relative_to(repo)}")
    keys = sorted({signer.fingerprint for signer in signers if signer.key_type == "ssh-ed25519"})
    return Check("trust", True, f"{len(keys)} release key(s): {', '.join(keys)}")


def check_release_urls(repo: Path) -> Check:
    """packaging/product.json's release locations: the download location
    of a version (``{version}`` substituted) and the latest release's, both
    https, ``{version}`` only in the first."""

    product = read_json(repo / "packaging" / "product.json")
    if not isinstance(product, dict):
        return Check("release-urls", False, "packaging/product.json is not an object")
    base, latest = product.get("release_base_url"), product.get("release_latest_url")
    problems = []
    for key, value in (("release_base_url", base), ("release_latest_url", latest)):
        if not isinstance(value, str) or not value:
            problems.append(f"{key} is not set")
        elif not value.startswith("https://") or len(value) <= len("https://"):
            problems.append(f"{key} is not an https URL")
    if isinstance(base, str) and "{version}" not in base:
        problems.append("release_base_url does not contain {version}")
    if isinstance(latest, str) and "{version}" in latest:
        problems.append("release_latest_url contains {version}")
    if problems:
        return Check("release-urls", False, "; ".join(problems) + " (set them in packaging/product.json)")
    return Check("release-urls", True, f"{base} and {latest}")


def check_receipt(repo: Path, receipt: Path | None, runner: Runner = run) -> Check:
    if receipt is None:
        return Check("receipt", False, "no gate receipt given (--receipt FILE from the release-level gate)")
    document = read_json(receipt)
    head = git(repo, "rev-parse", "HEAD", runner=runner).strip()
    if not isinstance(document, dict):
        return Check("receipt", False, f"{receipt} is not a receipt")
    problems = []
    if document.get("verdict") != "PASS":
        problems.append(f"verdict {document.get('verdict')!r}")
    if document.get("level") != "release":
        problems.append(f"level {document.get('level')!r}, not release")
    if document.get("head") != head:
        problems.append(f"head {str(document.get('head'))[:12]} is not HEAD {head[:12]}")
    if document.get("tree_clean") is not True:
        problems.append("the gated tree was not clean")
    if problems:
        return Check("receipt", False, "; ".join(problems))
    return Check("receipt", True, f"PASS at {head[:12]}")


def preflight(repo: Path, *, receipt: Path | None, previous: str | None | object = ...,
              runner: Runner = run) -> list[Check]:
    """Every release condition, in order (none stops the others)."""

    checks: list[Check] = []

    def guarded(name: str, function: Callable[[], Check]) -> None:
        try:
            checks.append(function())
        except (ReleaseError, OSError, KeyError, ValueError, TypeError) as exc:
            checks.append(Check(name, False, str(exc)))

    guarded("clean-tree", lambda: check_clean_tree(repo, runner))
    guarded("version", lambda: check_version(repo))

    def bump() -> Check:
        version, _ = release_version(repo)
        chosen = previous_release(repo, version, runner) if previous is ... else previous
        return check_version_bump(repo, chosen, runner)  # type: ignore[arg-type]

    guarded("version-bump", bump)
    guarded("changelog", lambda: check_changelog(repo))
    guarded("drafting-markers", lambda: check_drafting_markers(repo))
    guarded("contract", lambda: check_contract(repo))
    guarded("gateway-parity", lambda: check_gateway_parity(repo))
    guarded("notices", lambda: check_notices(repo, runner))
    guarded("hygiene", lambda: check_hygiene(repo, runner))
    guarded("history", lambda: check_history(repo, runner))
    guarded("trust", lambda: check_trust(repo))
    guarded("release-urls", lambda: check_release_urls(repo))
    guarded("receipt", lambda: check_receipt(repo, receipt, runner))
    return checks


# ------------------------------------------------------------------ build and reproduce


def commit_epoch(repo: Path, runner: Runner = run) -> int:
    return int(git(repo, "log", "-1", "--format=%ct", "HEAD", runner=runner).strip())


INSTALLERS = ("install.sh", "install.ps1")


def read_sums(out: Path) -> dict[str, str]:
    """``{name: sha256}`` of ``out``'s SHA256SUMS (not yet checked against
    a signature or the files)."""

    sums = out / SUMS
    if not sums.is_file():
        raise ReleaseError(f"{out} has no {SUMS}")
    names: dict[str, str] = {}
    for line in sums.read_text(encoding="ascii", errors="replace").splitlines():
        digest, _, name = line.partition("  ")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not name or name in names:
            raise ReleaseError(f"{sums} is not a release checksum list")
        names[name] = digest
    return names


def check_listing(out: Path, sums: Mapping[str, str]) -> None:
    """SHA256SUMS lists exactly the release: MANIFEST.json, the archives
    the manifest names and both installers (each a release member a person
    may run or check), and every listed file has its listed sha256."""

    manifest = read_json(out / MANIFEST)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("assets"), dict):
        raise ReleaseError(f"{MANIFEST} lists no assets")
    members = {MANIFEST, *manifest["assets"], *INSTALLERS}
    missing, extra = sorted(members - set(sums)), sorted(set(sums) - members)
    if missing or extra:
        parts = ([f"missing {', '.join(missing)}"] if missing else []) + (
            [f"not part of the release: {', '.join(extra)}"] if extra else [])
        raise ReleaseError(f"{SUMS} does not list exactly the release: {'; '.join(parts)}; build the release again")
    for name, digest in sorted(sums.items()):
        path = out / name
        if not path.is_file():
            raise ReleaseError(f"{out} has no {name}; build the release again")
        if sha256_file(path) != digest:
            raise ReleaseError(f"{name} does not match {SUMS}")


def build(repo: Path, out: Path, *, extra: Sequence[str] = (), runner: Runner = run) -> dict:
    """`tools/build.py release --out OUT` at the commit's source date; the
    result must list every release member in its SHA256SUMS."""

    epoch = commit_epoch(repo, runner)
    argv = [sys.executable, str(repo / "tools" / "build.py"), "release", "--out", str(out),
            "--source-date-epoch", str(epoch), *extra]
    say(f"building the release into {out} (source date {epoch})")
    result = subprocess.run(argv, cwd=repo, text=True, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            check=False) if runner is run else runner(argv, cwd=repo)
    if result.returncode != 0:
        raise ReleaseError(f"the release build failed (exit {result.returncode})")
    check_listing(out, read_sums(out))
    return json.loads(result.stdout)


def reproduce(repo: Path, out: Path, *, scratch: Path | None = None, extra: Sequence[str] = (),
              runner: Runner = run) -> dict:
    """Build this commit again from a fresh clone and compare with ``out``."""

    head = git(repo, "rev-parse", "HEAD", runner=runner).strip()
    base = Path(scratch) if scratch is not None else None
    if base is not None:
        base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-reproduce-", dir=base) as root:
        clone = Path(root) / "source"
        result = runner(["git", "clone", "--quiet", "--no-local", "--no-checkout", str(repo), str(clone)], cwd=repo)
        if result.returncode != 0:
            raise ReleaseError(f"cannot clone {repo}: {result.stderr.strip()[-400:]}")
        git(clone, "checkout", "--quiet", "--detach", head, runner=runner)
        rebuilt = Path(root) / "rebuilt"
        build(clone, rebuilt, extra=extra, runner=runner)
        argv = [sys.executable, str(clone / "tools" / "build.py"), "release", "compare", "--out", str(out),
                "--other", str(rebuilt)]
        compared = runner(argv, cwd=clone)
        if compared.returncode != 0:
            raise ReleaseError(f"the rebuild differs from {out}:\n{(compared.stdout + compared.stderr)[-2000:]}")
        return json.loads(compared.stdout)


# ------------------------------------------------------------------ sign


def check_release_inputs(out: Path) -> dict:
    """The release in ``out`` names its release key and its release
    location in MANIFEST.json (what an installation of it verifies and
    fetches its updates with). An explicit test build is refused even when
    it carries those production inputs."""

    manifest = read_json(out / MANIFEST)
    if not isinstance(manifest, dict):
        raise ReleaseError(f"{out / MANIFEST} is not a release manifest")
    problems = []
    if manifest.get("test_build", False) is not False:
        problems.append("is designated a test build" if manifest.get("test_build") is True
                        else "has an invalid test_build designation")
    trust = manifest.get("release_trust")
    signers = trust.get("signers") if isinstance(trust, dict) else None
    if not isinstance(signers, int) or isinstance(signers, bool) or signers < 1:
        problems.append("names no release key (its packaged release trust is empty)")
    location = manifest.get("release")
    if not isinstance(location, dict) or not all(
            isinstance(location.get(key), str) and location[key].startswith("https://")
            for key in ("base_url", "latest_url")):
        problems.append("names no release location (base_url and latest_url)")
    if problems:
        raise ReleaseError(f"{out / MANIFEST} {' and '.join(problems)}: a test build is never signed, verified "
                           "as a release or published; build the release with `tools/release.py build` from a tree "
                           "with the production key in src/claude_multi/data/release-trust/allowed_signers and the "
                           "release locations in packaging/product.json")
    return manifest


def sign_commands(out: Path, key: str) -> list[str]:
    """The commands the maintainer runs; this tool never touches the key."""

    def quote(text: str) -> str:
        return "'" + text.replace("'", "'\\''") + "'"

    sums = out / SUMS
    return [
        f"ssh-keygen -Y sign -f {quote(key)} -n {NAMESPACE} {quote(str(sums))}",
        f"mv {quote(str(sums) + '.sig')} {quote(str(out / SIGNATURE))}",
        f"python3 tools/release.py verify-draft --out {quote(str(out))}",
    ]


# ------------------------------------------------------------------ verify-draft


def verify_files(out: Path, allowed_signers: str, *, repo: Path) -> dict:
    """The signature over SHA256SUMS, that it lists exactly the release
    (:func:`check_listing`: the installers too), every listed file and the
    manifest."""

    trust = trust_module(repo)
    for name in (SUMS, SIGNATURE, MANIFEST):
        if not (out / name).is_file():
            raise ReleaseError(f"{out} has no {name}")
    sums_raw = (out / SUMS).read_bytes()
    try:
        verified = trust.verify(sums_raw, (out / SIGNATURE).read_bytes(), allowed_signers)
        sums = trust.parse_sums(sums_raw)
    except trust.TrustError as exc:
        raise ReleaseError(f"{SIGNATURE} does not verify: {exc}") from exc
    check_listing(out, sums)
    manifest = read_json(out / MANIFEST)
    for name, asset in manifest["assets"].items():
        if sums.get(name) != asset.get("sha256"):
            raise ReleaseError(f"{MANIFEST} and {SUMS} disagree about {name}")
    return {"key": verified.fingerprint, "files": len(sums), "version": manifest.get("version")}


def verify_draft(out: Path, *, repo: Path, allowed_signers: Path | None = None, installer: Path | None = None,
                 client: Path | None = None, journey_env: Mapping[str, str] | None = None,
                 runner: Runner = run) -> dict:
    """Verify the signed draft, install it like a new user would, then run
    the installed release's first run and its fixture journey (``client``:
    the pinned Claude Code for the managed turn; ``journey_env``: the
    journey's step switches, for tests)."""

    trust = trust_module(repo)
    if allowed_signers is not None:
        signers_text = allowed_signers.read_text(encoding="utf-8")
        say(f"verifying with the allowed signers in {allowed_signers} (not the packaged release trust)")
    else:
        check_release_inputs(out)
        path = repo / RESOURCES / "release-trust" / "allowed_signers"
        try:
            trust.release_signers(path)
        except trust.TrustError as exc:
            raise ReleaseError(f"{exc}; pass --allowed-signers FILE to check a draft signed with a test key") from exc
        signers_text = path.read_text(encoding="utf-8")
    report = verify_files(out, signers_text, repo=repo)
    script = installer or out / "install.sh"
    if not script.is_file():
        raise ReleaseError(f"{script} is missing")
    journey = repo / JOURNEY
    if not journey.is_file():
        raise ReleaseError(f"{journey} is missing")
    with tempfile.TemporaryDirectory(prefix="release-verify-") as root:
        home = Path(root) / "home"
        home.mkdir(mode=0o700)
        signers_file = Path(root) / "allowed_signers"
        signers_file.write_text(signers_text, encoding="utf-8")
        environment = {"HOME": str(home), "PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C",
                       "SHELL": "/bin/sh", "TMPDIR": root}
        installed = runner(["sh", str(script), "--from-dir", str(out), "--allowed-signers", str(signers_file),
                            "--no-setup", "--no-modify-path", "--yes"], cwd=Path(root), env=environment)
        if installed.returncode != 0:
            raise ReleaseError(f"install.sh failed:\n{(installed.stdout + installed.stderr)[-2000:]}")
        installed_versions = {}
        for name in ("claude-multi", "claude-multi-proxy"):
            launcher = home / ".local" / "bin" / name
            version = runner([str(launcher), "--version"], cwd=Path(root), env=environment)
            if version.returncode != 0 or not version.stdout.startswith(f"{name} {report['version']}"):
                raise ReleaseError(f"the installed {name} does not report {report['version']}: "
                                   f"{(version.stdout + version.stderr).strip()[-600:]}")
            installed_versions[name] = version.stdout.strip()
        report["installed"] = installed_versions
        # The first run and the user journey on the installed release, beyond
        # --version: a launcher that answers only --version fails here.
        steps = {**environment, **(journey_env or {})}
        if client is not None:
            steps["JOURNEY_CLIENT"] = str(client)
        walked = runner(["sh", str(journey), "installed", str(report["version"])], cwd=Path(root), env=steps,
                        process_group=True, timeout=JOURNEY_TIMEOUT)
        if walked.returncode != 0:
            launcher = home / ".local" / "bin" / "claude-multi"
            if launcher.is_file():  # a gateway the failed journey started dies with this HOME
                runner([str(launcher), "gateway", "stop"], cwd=Path(root), env=environment,
                       process_group=True, timeout=30)
            raise ReleaseError(f"the installed release failed its journey:\n"
                               f"{(walked.stdout + walked.stderr)[-2000:]}")
        report["journey"] = [line.removeprefix("journey: ") for line in walked.stdout.splitlines()
                             if line.startswith("journey: ")]
        # doctor's first run must have succeeded (the journey's own check of
        # its result, not of one line doctor prints whatever its outcome).
        if not any(line.startswith(FIRST_RUN_OK) for line in report["journey"]):
            raise ReleaseError(f"the installed release's journey reported no successful first run "
                               f"('{FIRST_RUN_OK}'):\n{walked.stdout[-2000:]}")
    return report


# ------------------------------------------------------------------ publish


def confirm(question: str, expected: str, *, stream: TextIO | None = None, output: TextIO | None = None) -> bool:
    """A typed confirmation: the exact ``expected`` text, on a terminal."""

    source = stream if stream is not None else sys.stdin
    sink = output if output is not None else sys.stderr
    if stream is None and not source.isatty():
        raise ReleaseError("publishing needs a confirmation typed on a terminal")
    sink.write(f"{question}\nType '{expected}' to continue: ")
    sink.flush()
    answer = source.readline()
    if not answer:
        return False
    return answer.strip() == expected


@dataclass(frozen=True)
class Asset:
    """A release file's identity: its bytes' sha256 and its size."""

    sha256: str
    size: int


def asset(path: Path) -> Asset:
    return Asset(sha256_file(path), path.stat().st_size)


def release_files(out: Path, *, repo: Path) -> dict[str, Asset]:
    """``{name: Asset}`` of what the release publishes besides its
    signature: every file SHA256SUMS lists (each archive, MANIFEST.json and
    both installers) and SHA256SUMS (the caller verified SHA256SUMS and its
    listing)."""

    trust = trust_module(repo)
    try:
        listed = trust.parse_sums((out / SUMS).read_bytes())
    except (OSError, trust.TrustError) as exc:
        raise ReleaseError(f"{out / SUMS}: {exc}") from exc
    missing = [name for name in INSTALLERS if name not in listed]
    if missing:
        raise ReleaseError(f"{SUMS} does not list {', '.join(missing)}; build the release again")
    files = {}
    for name in sorted({*listed, SUMS}):
        if not (out / name).is_file():
            raise ReleaseError(f"{out} has no {name}; build the release again")
        files[name] = asset(out / name)
    return files


def _gh(arguments: Sequence[str], *, repo: Path, repository: str | None, runner: Runner) -> str:
    argv = ["gh", *arguments, *(["--repo", repository] if repository else [])]
    result = runner(argv, cwd=repo)
    if result.returncode != 0:
        raise ReleaseError(f"{' '.join(argv[:3])} failed: {(result.stdout + result.stderr).strip()[-600:]}")
    return result.stdout


def check_draft(tag: str, files: dict[str, Asset], *, repo: Path, repository: str | None, runner: Runner) -> None:
    """The release ``tag`` is still a draft and holds exactly ``files``
    (a signature may be there besides them when ``files`` has none), and
    every one of them, downloaded, has the local bytes: the same sha256
    and size (the size the draft lists, too)."""

    try:
        view = json.loads(_gh(["release", "view", tag, "--json", "isDraft,assets"], repo=repo,
                              repository=repository, runner=runner))
        listed = {item["name"]: item.get("size") for item in view["assets"]}
        draft = view["isDraft"]
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ReleaseError(f"gh release view {tag} gave no release description ({exc})") from exc
    if draft is not True:
        raise ReleaseError(f"the release {tag} is not a draft (published already?); only a draft is published here")
    missing = sorted(set(files) - set(listed))
    extra = sorted(set(listed) - set(files) - {SIGNATURE})
    if missing or extra:
        parts = ([f"missing {', '.join(missing)}"] if missing else []) + (
            [f"not part of this release: {', '.join(extra)}"] if extra else [])
        raise ReleaseError(f"the draft {tag} does not hold this release's files ({'; '.join(parts)}); "
                           "nothing was published")
    with tempfile.TemporaryDirectory(prefix="release-draft-") as scratch:
        arguments = ["release", "download", tag, "--dir", scratch]
        for name in sorted(files):
            arguments += ["--pattern", name]
        _gh(arguments, repo=repo, repository=repository, runner=runner)
        differ = [name for name in sorted(files)
                  if listed[name] not in (None, files[name].size) or not (Path(scratch) / name).is_file()
                  or asset(Path(scratch) / name) != files[name]]
    if differ:
        raise ReleaseError(f"the draft {tag} holds other bytes than this release for {', '.join(differ)} (an "
                           "earlier build, or replaced meanwhile?); nothing was published. Replace the draft's files "
                           f"with this release's (gh release upload {tag} FILE... --clobber), or sign the build the "
                           "draft holds")


def publish(out: Path, tag: str, *, repo: Path, repository: str | None = None,
            stream: TextIO | None = None, runner: Runner = run) -> dict:
    """Check the draft release ``tag`` against ``out``, attach the
    signature and publish it."""

    manifest = check_release_inputs(out)
    version = manifest.get("version")
    if not isinstance(version, str) or RELEASE_VERSION.fullmatch(version) is None:
        raise ReleaseError(f"{out / MANIFEST} names no release version")
    if tag != f"v{version}":
        raise ReleaseError(f"the tag {tag} does not name the release {version} (v{version})")
    trust = trust_module(repo)
    path = repo / RESOURCES / "release-trust" / "allowed_signers"
    try:
        trust.release_signers(path)
    except trust.TrustError as exc:
        raise ReleaseError(str(exc)) from exc
    report = verify_files(out, path.read_text(encoding="utf-8"), repo=repo)
    files = release_files(out, repo=repo)
    if shutil.which("gh") is None:
        raise ReleaseError("gh (the GitHub command line) is not installed")
    gh = {"repo": repo, "repository": repository, "runner": runner}
    # The draft must hold exactly the release that was verified here: a
    # same-version draft of an earlier build would otherwise go public with
    # a signature that does not match its checksums.
    check_draft(tag, files, **gh)
    expected = f"publish {version}"
    if not confirm(f"Publish claude-multi {version} ({tag}, signed by {report['key']}) for everyone?", expected,
                   stream=stream):
        raise Declined("not published")
    _gh(["release", "upload", tag, str(out / SIGNATURE), "--clobber"], **gh)
    # Again with the signature, every asset downloaded and compared, right
    # before the promotion: a file replaced on the draft while the question
    # waited (under the same name, with SHA256SUMS unchanged) stays private.
    signed = {**files, SIGNATURE: asset(out / SIGNATURE)}
    check_draft(tag, signed, **gh)
    _gh(["release", "edit", tag, "--draft=false"], **gh)
    return {"published": tag, "key": report["key"], "files": len(signed)}


# ------------------------------------------------------------------ command line


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="release.py", description=__doc__.split("\n\n", 1)[0],
                                     epilog=__doc__.split("\n\n", 2)[2],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=str(REPO_ROOT), help="the source checkout (default this one)")
    commands = parser.add_subparsers(dest="command", required=True)
    pre = commands.add_parser("preflight", help="check the release conditions")
    pre.add_argument("--receipt", help="the release-level gate receipt (receipt.json)")
    pre.add_argument("--previous", help="the previous release tag, or none (default the newest v* tag before HEAD)")
    for name, text in (("build", "build the release"), ("reproduce", "rebuild from a fresh clone and compare"),
                       ("sign", "print the signing commands"), ("verify-draft", "verify and install the draft"),
                       ("publish", "publish the signed draft")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--out", required=True, help="the release directory")
        if name in ("build", "reproduce"):
            sub.add_argument("--offline", action="store_true", help="build from cached inputs only")
        if name == "reproduce":
            sub.add_argument("--scratch", help="where the clone and the rebuild run")
        if name == "sign":
            sub.add_argument("--key", default="<release key>", help="the private key file named in the command")
        if name == "verify-draft":
            sub.add_argument("--allowed-signers", help="verify with these signers instead of the packaged trust")
            sub.add_argument("--installer", help="the install.sh to run (default the draft's)")
            sub.add_argument("--client", help="the pinned Claude Code (a verified local copy) for the journey's "
                                              "managed turn")
        if name == "publish":
            sub.add_argument("--tag", required=True, help="the draft release's tag (vX.Y.Z)")
            sub.add_argument("--repository", help="OWNER/NAME for gh (default gh's own choice)")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return int(exc.code or 0) and EXIT_USAGE
    repo = Path(args.repo).resolve()
    try:
        if args.command == "preflight":
            previous: str | None | object = ...
            if args.previous is not None:
                previous = None if args.previous == "none" else args.previous
            checks = preflight(repo, receipt=Path(args.receipt) if args.receipt else None, previous=previous)
            for check in checks:
                print(f"{'ok  ' if check.ok else 'FAIL'} {check.name}: {check.detail}")
            failed = [check.name for check in checks if not check.ok]
            if failed:
                raise ReleaseError(f"not ready to release: {', '.join(failed)}")
            print("release: ready")
        elif args.command == "build":
            print(json.dumps(build(repo, Path(args.out).resolve(), extra=["--offline"] if args.offline else []),
                             indent=2, sort_keys=True))
        elif args.command == "reproduce":
            print(json.dumps(reproduce(repo, Path(args.out).resolve(),
                                       scratch=Path(args.scratch).resolve() if args.scratch else None,
                                       extra=["--offline"] if args.offline else []), indent=2, sort_keys=True))
        elif args.command == "sign":
            out = Path(args.out).resolve()
            if not (out / SUMS).is_file():
                raise ReleaseError(f"{out} has no {SUMS}; build the release first")
            check_release_inputs(out)
            print("Sign the release with the release key (this tool never reads it):")
            for line in sign_commands(out, args.key):
                print(f"  {line}")
        elif args.command == "verify-draft":
            print(json.dumps(verify_draft(Path(args.out).resolve(), repo=repo,
                                          allowed_signers=Path(args.allowed_signers) if args.allowed_signers else None,
                                          installer=Path(args.installer).resolve() if args.installer else None,
                                          client=Path(args.client).resolve() if args.client else None),
                             indent=2, sort_keys=True))
        elif args.command == "publish":
            print(json.dumps(publish(Path(args.out).resolve(), args.tag, repo=repo, repository=args.repository),
                             indent=2, sort_keys=True))
    except Declined as exc:
        print(f"release.py: {exc}", file=sys.stderr)
        return EXIT_DECLINED
    except ReleaseError as exc:
        print(f"release.py: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        print("release.py: cancelled", file=sys.stderr)
        return EXIT_CANCELLED
    return 0


if __name__ == "__main__":
    sys.exit(main())
