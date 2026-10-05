#!/usr/bin/env python3
"""Build the patched gateway (CLIProxyAPI) and the release bundles from hash-pinned inputs.

    python3 tools/build.py gateway [STEP ...] [OPTIONS]
    python3 tools/build.py bundle|release [OPTIONS]   (see `build.py release --help`)

``gateway/UPSTREAM.json`` is the recipe: the upstream source at its commit,
the vendored Go modules (a normalised tree hash), the official Go toolchain
archive for each build host, the ordered patch series, the build flags, the
targets and the typed Go test gates. The Nix flake runs this same tool
offline over the same inputs (fixed-output derivations), so a Nix build and a
plain build produce identical gateway bytes.

Steps, run in this order when several are named (default: fetch vendor apply
build inspect record):

  fetch    take the upstream source archive and the Go toolchain archive for
           this host from the cache (downloading what is missing) or from
           --inputs, verify their sha256 and unpack both into the work dir
  vendor   vendor the Go modules with the pinned toolchain (`go mod vendor`;
           offline: copy a pre-fetched tree) and verify the normalised
           vendor-tree sha256
  apply    apply the admitted patch series in order: `git apply --check`, then
           apply; any reject, offset or reduced context fails (a diagnostic
           omission build may pass --omission-offsets, see its help)
  build    one `go build` per target into DIST/<target>/cli-proxy-api[.exe]
  gates    run the typed Go test gates that apply to this host
  inspect  check the built binaries: no concrete Nix store references,
           static Linux binaries, system-only dynamic libraries on macOS, the
           linker's ad-hoc signature on darwin/arm64, patch markers present
  record   write BUILD.json and gateway-contract.json into DIST; for the
           admitted series also check that gateway/licenses and gateway/sbom
           still describe the shipped targets built here, then copy the
           notices to DIST/licenses (plus THIRD_PARTY_NOTICES.txt) and each
           target's SBOM to DIST/<target>/cli-proxy-api.cdx.json

build, gates, record and notices first re-hash the source and vendor trees
and refuse a work dir edited since the vendor and apply steps verified them.

Commands run alone:

  repro    build one target twice in separate directories and compare
  compare  compare DIST/<target> with a binary built elsewhere (--other)
  env      print the toolchain environment of a prepared work dir
  notices  regenerate gateway/licenses (the licence and notice texts of
           everything the shipped targets link, the patch series' modified
           files, modules.json) and gateway/sbom (a CycloneDX SBOM per
           shipped target) from a work dir that built every shipped target
  contract write src/claude_multi/data/gateway-contract.json (or --out) from
           the recipe: the upstream version and the ordered basename@sha256
           identity of every admitted patch, byte-equal to the Nix package's
           contract
  registry copy the pinned upstream model registry (the files the launcher
           reads) from a work dir prepared by the fetch step alone into
           src/claude_multi/data/registry (or --out DIR); the Nix package
           compares the checked-in copy with the pinned source

Pre-fetched inputs (--inputs DIR): DIR holds the toolchain archive under its
official file name, the upstream source as ``source/`` (an unpacked tree) or
as its archive file, and optionally the vendored modules as ``vendor/``;
nothing in DIR is downloaded. With --offline nothing is downloaded at all
(`--offline --inputs DIR` builds from DIR alone). Fetched inputs are cached in
~/.cache/claude-multi-build (or $CLAUDE_MULTI_BUILD_CACHE, or --cache).

Standard library only; Python 3.11 or newer.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UPSTREAM = REPO_ROOT / "gateway" / "UPSTREAM.json"
DEFAULT_PATCH_DIR = REPO_ROOT / "gateway" / "patches"
DEFAULT_DIST = REPO_ROOT / "dist" / "gateway"
CACHE_ENV = "CLAUDE_MULTI_BUILD_CACHE"

STEPS = ("fetch", "vendor", "apply", "build", "gates", "inspect", "record")
DEFAULT_STEPS = ("fetch", "vendor", "apply", "build", "inspect", "record")
COMMANDS = ("repro", "compare", "env", "notices", "contract", "registry")
GATE_SELECTIONS = ("all", "portable", "race")

UPSTREAM_FORMAT = 1
STATE_FORMAT = 1
BUILD_FORMAT = 1
TREE_FORMAT = "tree-sha256-v1"

# A concrete store path reference (the 32-character Nix base32 hash and its
# dash), never the bare prefix that error texts may legitimately contain.
NIX_STORE_REFERENCE = re.compile(rb"/nix/store/[0-9abcdfghijklmnpqrsvwxyz]{32}-")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
OFFSET_REPORT = re.compile(r"^Hunk #\d+ succeeded at \d+|Context reduced", re.MULTILINE)
CONTEXT_REDUCED = re.compile(r"Context reduced")
TEST_NAME = re.compile(r"^(Test|Example|Fuzz)[A-Za-z0-9_]*$")
PATCH_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
LIST_STATUS = re.compile(r"^(?:\?\s|(?:ok|FAIL|PASS|---|panic:|exit status)\b)")

# Machine-code facts the inspect step checks.
ELF_MACHINES = {"amd64": 62, "arm64": 183}
MACHO_CPUS = {"amd64": 0x01000007, "arm64": 0x0100000C}
PE_MACHINES = {"amd64": 0x8664, "arm64": 0xAA64}
MACHO_SYSTEM_LIBRARIES = ("/usr/lib/", "/System/Library/")
CS_ADHOC = 0x00000002
CS_LINKER_SIGNED = 0x00020000


class BuildError(Exception):
    """A failed step; the message says what to fix."""


def log(message: str) -> None:
    print(f"gateway: {message}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ recipe


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate key {key!r}")
        document[key] = value
    return document


def load_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BuildError(f"{path}: {exc}") from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildError(f"UPSTREAM.json: {message}")


def check_upstream(doc: Any) -> None:
    """The structure this tool relies on (the closed schema lives in schemas/)."""

    _require(isinstance(doc, dict) and doc.get("format") == UPSTREAM_FORMAT, "unsupported format")
    for key in ("upstream", "source", "vendor", "toolchain", "series", "candidates", "build",
                "targets", "gates", "markers", "source_pins"):
        _require(key in doc, f"missing {key!r}")
    upstream = doc["upstream"]
    _require(re.fullmatch(r"[0-9a-f]{40}", str(upstream.get("commit", ""))) is not None, "commit is not a full sha")
    for section, key in (("source", "archive_sha256"), ("source", "tree_sha256"), ("vendor", "tree_sha256")):
        _require(SHA256_HEX.match(str(doc[section].get(key, ""))) is not None, f"{section}.{key} is not a sha256")
    archives = doc["toolchain"].get("archives")
    _require(isinstance(archives, dict) and archives, "toolchain.archives is empty")
    for name, archive in archives.items():
        _require(SHA256_HEX.match(str(archive.get("sha256", ""))) is not None, f"toolchain {name} sha256")
    names = [entry["basename"] for entry in doc["series"] + doc["candidates"]]
    _require(len(names) == len(set(names)), "a patch basename is listed twice")
    for entry in doc["series"] + doc["candidates"]:
        _require(SHA256_HEX.match(str(entry.get("sha256", ""))) is not None, f"{entry.get('basename')} sha256")
        _require(PurePosixPath(entry["basename"]).name == entry["basename"], f"{entry['basename']} is not a basename")
    _require(all(entry.get("admitted") is False for entry in doc["candidates"]), "a candidate is admitted")
    targets = [target["name"] for target in doc["targets"]]
    _require(len(targets) == len(set(targets)), "a target is listed twice")
    gates = [gate["name"] for gate in doc["gates"]]
    _require(len(gates) == len(set(gates)), "a gate is listed twice")
    for gate in doc["gates"]:
        for key in ("run", "skip"):
            _require("/" not in gate.get(key, ""), f"gate {gate['name']}: {key} selects subtests")
        _require(gate["platforms"] in ("portable", "linux"), f"gate {gate['name']}: platforms")
        _require(not gate["race"] or gate["platforms"] == "linux", f"gate {gate['name']}: race gates are Linux gates")
    known = set(names)
    for marker in doc["markers"]:
        _require(marker["patch"] in known, f"marker for unknown patch {marker['patch']}")


def admitted_series(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in doc["series"] if entry["admitted"]]


def target_records(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {target["name"]: target for target in doc["targets"]}


def binary_name(doc: dict[str, Any], target: dict[str, Any]) -> str:
    return doc["upstream"]["binary"] + (".exe" if target["goos"] == "windows" else "")


def contract_document(doc: dict[str, Any], series: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "upstream_version": doc["upstream"]["version"],
        "patches": [f"{entry['basename']}@{entry['sha256']}" for entry in series],
    }


def contract_bytes(contract: dict[str, Any]) -> bytes:
    """The byte form Nix's builtins.toJSON gives the same document."""

    return json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


# ------------------------------------------------------------------ files


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path, *, exclude_top: Iterable[str] = ()) -> str:
    """A normalised content hash of a directory tree.

    Regular files contribute their relative path, sha256 and whether any
    execute bit is set; symbolic links their target. Ownership, other mode
    bits, timestamps and empty directories are ignored, so a read-only store
    copy, a fresh unpack and a writable work copy hash alike.
    """

    root = Path(root)
    excluded = set(exclude_top)
    records: list[tuple[bytes, bytes]] = []
    for directory, dirs, files in os.walk(root):
        here = Path(directory)
        if here == root:
            dirs[:] = [name for name in dirs if name not in excluded]
            files = [name for name in files if name not in excluded]
        kept = []
        for name in dirs:
            if (here / name).is_symlink():
                files.append(name)
            else:
                kept.append(name)
        dirs[:] = kept
        for name in files:
            path = here / name
            relative = path.relative_to(root).as_posix()
            if "\n" in relative or "\0" in relative:
                raise BuildError(f"unsupported file name in {root}: {relative!r}")
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                kind, digest = b"L-", hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
            elif stat.S_ISREG(info.st_mode):
                kind = b"Fx" if info.st_mode & 0o111 else b"F-"
                digest = sha256_file(path)
            else:
                raise BuildError(f"unsupported file type in {root}: {relative}")
            encoded = relative.encode()
            records.append((encoded, kind + b" " + digest.encode() + b" " + encoded + b"\n"))
    total = hashlib.sha256(TREE_FORMAT.encode() + b"\n")
    for _, line in sorted(records):
        total.update(line)
    return total.hexdigest()


def make_writable_copy(source: Path, destination: Path) -> None:
    """Copy a tree (links kept) with normal modes: 0755 dirs, 0644/0755 files."""

    if destination.exists() or destination.is_symlink():
        remove_tree(destination)
    shutil.copytree(Path(source).resolve(), destination, symlinks=True, copy_function=shutil.copyfile)
    normalise_modes(destination)


def normalise_modes(root: Path) -> None:
    for directory, _dirs, files in os.walk(root):
        os.chmod(directory, 0o755)
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            original = os.stat(path).st_mode
            os.chmod(path, 0o755 if original & 0o111 else 0o644)


def remove_tree(path: Path) -> None:
    def retry(function, target, _info):
        os.chmod(os.path.dirname(target), 0o755)
        if os.path.exists(target) and not os.path.islink(target):
            os.chmod(target, 0o755)
        function(target)

    if Path(path).is_symlink() or Path(path).is_file():
        Path(path).unlink()
    elif Path(path).exists():
        shutil.rmtree(path, onexc=retry) if sys.version_info >= (3, 12) else shutil.rmtree(path, onerror=retry)


def safe_extract(archive: Path, destination: Path, *, strip: int = 1) -> dict[str, str]:
    """Unpack a tar archive below ``destination``, dropping ``strip`` leading
    components; absolute names, ``..``, links leaving the tree and special
    files are refused. Returns the archive's pax header fields."""

    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        headers = dict(tar.pax_headers)
        members = []
        for member in tar.getmembers():
            parts = PurePosixPath(member.name).parts
            if member.name.startswith("/") or ".." in parts:
                raise BuildError(f"{archive.name}: unsafe member {member.name!r}")
            if len(parts) <= strip:
                if member.isdir():
                    continue
                raise BuildError(f"{archive.name}: unexpected top-level member {member.name!r}")
            if not (member.isfile() or member.isdir() or member.issym()):
                raise BuildError(f"{archive.name}: unsupported member type {member.name!r}")
            relative = PurePosixPath(*parts[strip:])
            if member.issym():
                target = PurePosixPath(member.linkname)
                resolved = PurePosixPath(*relative.parts[:-1], *target.parts)
                if target.is_absolute() or ".." in PurePosixPath(os.path.normpath(str(resolved))).parts:
                    raise BuildError(f"{archive.name}: link {member.name!r} leaves the tree")
            member.name = str(relative)
            members.append(member)
        if hasattr(tarfile, "data_filter"):
            tar.extractall(destination, members=members, filter="data")
        else:  # pragma: no cover - Python 3.11 before 3.11.4
            tar.extractall(destination, members=members)
    return headers


def download(url: str, destination: Path, sha256: str, *, attempts: int = 3, timeout: float = 60.0) -> None:
    """Fetch ``url`` to ``destination``; a hash mismatch fails at once."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        digest = hashlib.sha256()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "claude-multi-build"})
            with urllib.request.urlopen(request, timeout=timeout) as response, open(partial, "wb") as out:
                for chunk in iter(lambda: response.read(1 << 20), b""):
                    out.write(chunk)
                    digest.update(chunk)
        except (OSError, urllib.error.URLError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            last = exc
            partial.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(attempt)
            continue
        if digest.hexdigest() != sha256:
            partial.unlink(missing_ok=True)
            raise BuildError(f"{url}: sha256 {digest.hexdigest()} differs from the pinned {sha256}")
        os.replace(partial, destination)
        return
    raise BuildError(f"download failed after {attempts} attempts: {url}: {last}")


def verify_file(path: Path, sha256: str, label: str) -> None:
    actual = sha256_file(path)
    if actual != sha256:
        raise BuildError(f"{label}: {path} has sha256 {actual}, pinned {sha256}")


# ------------------------------------------------------------------ host and environment


def host_target() -> str:
    system = {"linux": "linux", "darwin": "darwin"}.get(sys.platform)
    machine = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower())
    if system is None or machine is None:
        raise BuildError(f"unsupported build host {sys.platform}/{platform.machine()}")
    return f"{system}-{machine}"


def default_cache() -> Path:
    configured = os.environ.get(CACHE_ENV)
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "claude-multi-build"


def go_environment(work: Path, *, cgo: bool = False, goos: str | None = None, goarch: str | None = None,
                   base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment every Go invocation runs with.

    Inherited Go and cgo settings are dropped so the user's shell cannot
    change the output (GOFLAGS, GOAMD64, GOEXPERIMENT, CGO_CFLAGS, …); the
    toolchain is the unpacked archive, modules come only from vendor/, and
    HOME points into the work dir so tests never touch the real home.
    """

    source = dict(os.environ if base is None else base)
    environment = {
        key: value for key, value in source.items()
        if not key.startswith(("GO", "CGO_", "XDG_")) and key not in ("HOME", "LC_ALL", "LANG")
    }
    goroot = work / "go"
    environment.update({
        "GOROOT": str(goroot),
        "GOTOOLCHAIN": "local",
        "GOFLAGS": "-mod=vendor",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOENV": "off",
        "GOWORK": "off",
        "GOTELEMETRY": "off",
        "GOCACHE": str(work / "cache" / "go-build"),
        "GOPATH": str(work / "cache" / "gopath"),
        "GOMODCACHE": str(work / "cache" / "gopath" / "pkg" / "mod"),
        "CGO_ENABLED": "1" if cgo else "0",
        "HOME": str(work / "home"),
        "LC_ALL": "C",
        "PATH": str(goroot / "bin") + os.pathsep + source.get("PATH", os.defpath),
    })
    if goos:
        environment["GOOS"] = goos
    if goarch:
        environment["GOARCH"] = goarch
    return environment


def run(argv: list[str], *, cwd: Path, env: dict[str, str], capture: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, cwd=cwd, env=env, text=True, check=False,
                              stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.STDOUT if capture else None)
    except OSError as exc:
        raise BuildError(f"cannot run {argv[0]}: {exc}") from exc


# ------------------------------------------------------------------ context and state


def absolute(path: str | Path) -> Path:
    """``path`` from the current directory, symbolic links not resolved."""

    return Path(os.path.abspath(Path(path).expanduser()))


class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.upstream_path = Path(args.upstream).resolve()
        if not self.upstream_path.is_file():
            raise BuildError(f"recipe not found: {self.upstream_path} (pass --upstream)")
        self.upstream_bytes = self.upstream_path.read_bytes()
        self.doc = load_json(self.upstream_path)
        check_upstream(self.doc)
        # Every path is made absolute here, links kept (a patch link's own
        # name is its basename): go and git run inside the source tree, so a
        # relative path would resolve there instead of where it was given.
        self.patch_dir = absolute(args.patches)
        self.explicit_patches = [absolute(path) for path in args.patch or []]
        self.omission_offsets = bool(getattr(args, "omission_offsets", False))
        self.cache = absolute(args.cache) if args.cache else absolute(default_cache())
        self.inputs = Path(args.inputs).resolve() if args.inputs else None
        self.offline = bool(args.offline)
        self.work = absolute(args.work) if args.work else self.cache / "work" / "gateway"
        self.dist = absolute(args.dist) if args.dist else DEFAULT_DIST
        self.host = host_target()
        self.target_args = list(args.target or [])
        self.gate_selection = args.gates
        self.other = absolute(args.other) if getattr(args, "other", None) else None
        self.scratch = absolute(args.scratch) if getattr(args, "scratch", None) else None
        self.licenses = absolute(args.licenses) if getattr(args, "licenses", None) else DEFAULT_LICENSES
        self.sbom_dir = absolute(args.sbom) if getattr(args, "sbom", None) else DEFAULT_SBOM
        self.out = absolute(args.out) if getattr(args, "out", None) else None

    @property
    def upstream_sha256(self) -> str:
        return hashlib.sha256(self.upstream_bytes).hexdigest()

    @property
    def src(self) -> Path:
        return self.work / "src"

    @property
    def go(self) -> str:
        return str(self.work / "go" / "bin" / "go")

    def targets(self, default: list[str]) -> list[dict[str, Any]]:
        records = target_records(self.doc)
        names = self.target_args or default
        chosen: list[str] = []
        for name in names:
            if name == "host":
                expanded = [self.host]
            elif name == "shipped":
                expanded = [target["name"] for target in self.doc["targets"] if target["shipped"]]
            elif name == "all":
                expanded = list(records)
            else:
                expanded = [name]
            for item in expanded:
                if item not in records:
                    raise BuildError(f"unknown target {item!r}; known: {', '.join(records)}")
                if item not in chosen:
                    chosen.append(item)
        return [records[name] for name in chosen]

    # The work dir's state records what each step verified, so a later step
    # never builds from an unverified or differently pinned tree.
    def load_state(self, required: str) -> dict[str, Any]:
        path = self.work / "state.json"
        if not path.is_file():
            raise BuildError(f"{self.work} is not prepared; run the fetch step first")
        state = load_json(path)
        if state.get("format") != STATE_FORMAT or state.get("upstream_sha256") != self.upstream_sha256:
            raise BuildError(f"{self.work} was prepared from a different recipe; run the fetch step again")
        if required not in state.get("done", []):
            raise BuildError(f"the {required} step has not run in {self.work}")
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        path = self.work / "state.json"
        temporary = path.with_name("state.json.tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)


# ------------------------------------------------------------------ steps


def toolchain_record(ctx: Context) -> dict[str, Any]:
    archives = ctx.doc["toolchain"]["archives"]
    if ctx.host not in archives:
        raise BuildError(f"no pinned Go toolchain for build host {ctx.host}")
    return archives[ctx.host]


def locate_toolchain(ctx: Context) -> Path:
    record = toolchain_record(ctx)
    if ctx.inputs is not None:
        path = ctx.inputs / record["file"]
        if not path.is_file():
            raise BuildError(f"--inputs has no {record['file']}")
    else:
        path = ctx.cache / "downloads" / record["file"]
        if not path.is_file():
            if ctx.offline:
                raise BuildError(f"{record['file']} is not in {path.parent} (offline)")
            log(f"downloading {record['file']}")
            download(ctx.doc["toolchain"]["url_prefix"] + record["file"], path, record["sha256"])
    verify_file(path, record["sha256"], "Go toolchain")
    return path


def locate_source(ctx: Context) -> tuple[Path, bool]:
    """The upstream source: (path, is_archive)."""

    source = ctx.doc["source"]
    if ctx.inputs is not None:
        tree = ctx.inputs / "source"
        if tree.is_dir():
            return tree, False
        path = ctx.inputs / source["archive_file"]
        if not path.is_file():
            raise BuildError(f"--inputs has neither source/ nor {source['archive_file']}")
    else:
        path = ctx.cache / "downloads" / source["archive_file"]
        if not path.is_file():
            if ctx.offline:
                raise BuildError(f"{source['archive_file']} is not in {path.parent} (offline)")
            log(f"downloading {source['archive_file']}")
            download(source["archive_url"], path, source["archive_sha256"])
    verify_file(path, source["archive_sha256"], "upstream source archive")
    return path, True


def step_fetch(ctx: Context) -> None:
    toolchain = locate_toolchain(ctx)
    source, is_archive = locate_source(ctx)
    ctx.work.mkdir(parents=True, exist_ok=True)
    for stale in ("go", "src", "home", "tmp", "state.json"):
        remove_tree(ctx.work / stale)
    log(f"unpacking Go {ctx.doc['toolchain']['version']} ({ctx.host})")
    safe_extract(toolchain, ctx.work / "go", strip=1)
    if is_archive:
        headers = safe_extract(source, ctx.src, strip=1)
        recorded = headers.get("comment")
        if recorded is not None and recorded != ctx.doc["upstream"]["commit"]:
            raise BuildError(f"source archive names commit {recorded}, pinned {ctx.doc['upstream']['commit']}")
        normalise_modes(ctx.src)
    else:
        make_writable_copy(source, ctx.src)
    tree = tree_sha256(ctx.src)
    if tree != ctx.doc["source"]["tree_sha256"]:
        raise BuildError(f"upstream source tree {tree} differs from the pinned {ctx.doc['source']['tree_sha256']}")
    (ctx.work / "home").mkdir(mode=0o700)
    ctx.save_state({
        "format": STATE_FORMAT,
        "upstream_sha256": ctx.upstream_sha256,
        "host": ctx.host,
        "toolchain": {"version": ctx.doc["toolchain"]["version"], "host": ctx.host, **toolchain_record(ctx)},
        "source": {"tree_sha256": tree},
        "done": ["fetch"],
    })


def step_vendor(ctx: Context) -> None:
    state = ctx.load_state("fetch")
    if "vendor" in state["done"]:
        raise BuildError(f"{ctx.work} is already vendored; run the fetch step again")
    pinned = ctx.doc["vendor"]["tree_sha256"]
    vendor = ctx.src / "vendor"
    remove_tree(vendor)
    provided = ctx.inputs / "vendor" if ctx.inputs is not None else None
    cached = ctx.cache / "vendor" / pinned
    if provided is not None and provided.is_dir():
        make_writable_copy(provided, vendor)
        mode = "provided"
    else:
        if cached.is_dir():
            make_writable_copy(cached, vendor)
            mode = "cache"
        elif ctx.offline:
            where = "--inputs has no vendor/ and " if provided is not None else ""
            raise BuildError(f"{where}no vendored modules for {pinned} in {cached.parent} (offline)")
        else:
            log("vendoring Go modules (go mod vendor)")
            env = go_environment(ctx.work)
            env.update({"GOFLAGS": "-mod=mod", "GOPROXY": "https://proxy.golang.org", "GOSUMDB": "sum.golang.org"})
            result = run([ctx.go, "mod", "vendor"], cwd=ctx.src, env=env)
            if result.returncode:
                raise BuildError("go mod vendor failed")
            if tree_sha256(ctx.src, exclude_top=("vendor",)) != state["source"]["tree_sha256"]:
                raise BuildError("go mod vendor changed the upstream sources (go.mod or go.sum)")
            mode = "go mod vendor"
    tree = tree_sha256(vendor)
    if tree != pinned:
        raise BuildError(f"vendored module tree {tree} differs from the pinned {pinned}")
    if mode == "go mod vendor":
        make_writable_copy(vendor, ctx.cache / "vendor" / pinned)
    state["vendor"] = {"tree_sha256": tree, "origin": mode}
    state["done"].append("vendor")
    ctx.save_state(state)


def resolve_series(ctx: Context) -> list[dict[str, Any]]:
    """The patches to apply, each with its verified sha256.

    By default the admitted series from the patch directory. An explicit
    --patch list (diagnostic builds) is applied as given; a listed basename
    must carry its pinned bytes.
    """

    listed = {entry["basename"]: entry for entry in ctx.doc["series"] + ctx.doc["candidates"]}
    if not ctx.explicit_patches:
        chosen = [(ctx.patch_dir / entry["basename"], entry) for entry in admitted_series(ctx.doc)]
    else:
        chosen = [(path, listed.get(path.name)) for path in ctx.explicit_patches]
    series = []
    for path, entry in chosen:
        if not path.is_file():
            raise BuildError(f"patch not found: {path}")
        digest = sha256_file(path)
        name = entry["basename"] if entry else path.name
        if entry is not None and digest != entry["sha256"]:
            raise BuildError(f"{name}: sha256 {digest} differs from the pinned {entry['sha256']}")
        series.append({"basename": name, "sha256": digest, "path": path, "listed": entry is not None})
    return series


def patch_files(text: str) -> list[dict[str, str]]:
    """The files one patch touches, in patch order: ``{"change", "path"}``
    with change ``added``, ``deleted`` or ``modified``."""

    files: list[dict[str, str]] = []
    old = None
    remaining_old = remaining_new = 0
    for line in text.splitlines():
        if remaining_old or remaining_new:
            # Hunk body: a removed line may itself start with "-- ".
            if line.startswith(" "):
                remaining_old, remaining_new = remaining_old - 1, remaining_new - 1
            elif line.startswith("-"):
                remaining_old -= 1
            elif line.startswith("+"):
                remaining_new -= 1
            elif not line.startswith("\\"):
                raise BuildError(f"malformed hunk line {line[:60]!r}")
            if remaining_old < 0 or remaining_new < 0:
                raise BuildError("a hunk is longer than its header says")
            continue
        hunk = PATCH_HUNK.match(line)
        if hunk:
            remaining_old = int(hunk.group(2) if hunk.group(2) is not None else 1)
            remaining_new = int(hunk.group(4) if hunk.group(4) is not None else 1)
        elif line.startswith("--- "):
            old = line[4:].split("\t", 1)[0]
        elif line.startswith(("+", "-", " ")) and not line.startswith("+++ "):
            raise BuildError(f"a hunk line outside any hunk: {line[:60]!r}")
        elif line.startswith("+++ ") and old is not None:
            new = line[4:].split("\t", 1)[0]
            if new == "/dev/null":
                change, path = "deleted", old
            else:
                change, path = ("added" if old == "/dev/null" else "modified"), new
            if not path.startswith(("a/", "b/")):
                raise BuildError(f"patch path {path!r} lacks the a/ or b/ prefix")
            files.append({"change": change, "path": path[2:]})
            old = None
    if not files:
        raise BuildError("a patch names no files")
    return files


def git_environment(src: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({
        "LC_ALL": "C",
        # Never discover a repository around the work dir: `git apply` inside
        # a repository resolves paths against its root and skips the rest.
        "GIT_CEILING_DIRECTORIES": str(src.resolve().parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    })
    return environment


def has_contextless_change(text: str) -> bool:
    """Whether a hunk changes an existing file with no context line: such a
    hunk is located by its line number alone, so it can never move."""

    old_count = new_count = 0
    context = new_file = False
    for line in text.splitlines():
        if old_count or new_count:
            if line.startswith(" "):
                old_count, new_count, context = old_count - 1, new_count - 1, True
            elif line.startswith("-"):
                old_count -= 1
            elif line.startswith("+"):
                new_count -= 1
            if not (old_count > 0 or new_count > 0) and not context and not new_file:
                return True
            continue
        hunk = PATCH_HUNK.match(line)
        if hunk:
            old_count = int(hunk.group(2) if hunk.group(2) is not None else 1)
            new_count = int(hunk.group(4) if hunk.group(4) is not None else 1)
            context, new_file = False, hunk.group(1) == "0" and old_count == 0
    return False


def apply_patch(src: Path, patch: Path, name: str, *, allow_offsets: bool = False) -> list[str]:
    """Apply one patch with no fuzz: refuse rejects, offsets and reduced context.

    ``allow_offsets`` (diagnostic omission builds only) lets a hunk apply at a
    line offset when its full context matches; reduced context, rejects and a
    moved hunk without context still fail. Returns git's report of each moved
    hunk (always empty without ``allow_offsets``).
    """

    if (src / ".git").exists():
        raise BuildError(f"{src} is a git checkout; patches apply to a plain tree")
    env = git_environment(src)
    flags = ["--verbose", "--unidiff-zero"]
    check = run(["git", "apply", "--check", *flags, str(patch)], cwd=src, env=env, capture=True)
    if check.returncode:
        raise BuildError(f"{name} does not apply:\n{check.stdout.strip()[-2000:]}")
    moved = [line for line in check.stdout.splitlines() if OFFSET_REPORT.search(line)]
    if moved and (not allow_offsets or any(CONTEXT_REDUCED.search(line) for line in moved)
                  or has_contextless_change(patch.read_text(encoding="utf-8"))):
        raise BuildError(f"{name} applies only with offsets:\n" + "\n".join(moved))
    applied = run(["git", "apply", *flags, str(patch)], cwd=src, env=env, capture=True)
    if applied.returncode or [line for line in applied.stdout.splitlines() if OFFSET_REPORT.search(line)] != moved:
        raise BuildError(f"{name} failed while applying:\n{applied.stdout.strip()[-2000:]}")
    return moved


def omission_series_problem(doc: dict[str, Any], series: list[dict[str, Any]]) -> str | None:
    """Why ``series`` is not the admitted series with patches left out (the
    only series --omission-offsets accepts), or None."""

    admitted = [(entry["basename"], entry["sha256"]) for entry in admitted_series(doc)]
    applied = [(entry["basename"], entry["sha256"]) for entry in series]
    if applied == admitted:
        return "the series is the admitted series, which always applies without offsets"
    if [item for item in admitted if item in applied] != applied:
        return "the series is not the admitted series with patches left out, in its order"
    return None


def step_apply(ctx: Context) -> None:
    state = ctx.load_state("vendor")
    if "apply" in state["done"]:
        raise BuildError(f"{ctx.work} is already patched; run the fetch step again")
    series = resolve_series(ctx)
    if ctx.omission_offsets:
        problem = None if ctx.explicit_patches else "it needs an explicit --patch list"
        problem = problem or omission_series_problem(ctx.doc, series)
        if problem:
            raise BuildError(f"--omission-offsets is for diagnostic omission builds only: {problem}")
    moved_hunks = {}
    for entry in series:
        log(f"applying {entry['basename']}")
        moved = apply_patch(ctx.src, entry["path"], entry["basename"], allow_offsets=ctx.omission_offsets)
        if moved:
            log(f"{entry['basename']} moved by the omitted patches:\n  " + "\n  ".join(moved))
            moved_hunks[entry["basename"]] = moved
        entry["files"] = patch_files(entry["path"].read_text(encoding="utf-8"))
    admitted = [(item["basename"], item["sha256"]) for item in admitted_series(ctx.doc)]
    state["series"] = [{key: entry[key] for key in ("basename", "sha256", "listed", "files")} for entry in series]
    if moved_hunks:
        state["moved_hunks"] = moved_hunks
    state["admitted_series"] = [(entry["basename"], entry["sha256"]) for entry in series] == admitted
    state["post_apply_tree_sha256"] = tree_sha256(ctx.src, exclude_top=("vendor",))
    state["done"].append("apply")
    ctx.save_state(state)


def verify_prepared_trees(ctx: Context, state: dict[str, Any], step: str) -> None:
    """The work dir's trees are still the ones vendor and apply verified.

    A build, gate run, record or notice generation from an edited tree would
    carry the recorded input hashes (and, for the admitted series, its
    identity) without their content, so any change since is refused.
    """

    if tree_sha256(ctx.src / "vendor") != state["vendor"]["tree_sha256"]:
        raise BuildError(f"{step}: the vendored modules in {ctx.src / 'vendor'} changed after the vendor step; "
                         "run the fetch step again")
    if tree_sha256(ctx.src, exclude_top=("vendor",)) != state["post_apply_tree_sha256"]:
        raise BuildError(f"{step}: the source tree {ctx.src} changed after the apply step; run the fetch step again")


def ldflags(doc: dict[str, Any]) -> str:
    return " ".join(doc["build"]["ldflags"])


def build_target(ctx: Context, target: dict[str, Any]) -> dict[str, Any]:
    out_dir = ctx.dist / target["name"]
    out_dir.mkdir(parents=True, exist_ok=True)
    binary = out_dir / binary_name(ctx.doc, target)
    partial = out_dir / (binary.name + ".part")
    partial.unlink(missing_ok=True)
    env = go_environment(ctx.work, goos=target["goos"], goarch=target["goarch"])
    env.update(ctx.doc["build"]["env"])
    argv = [ctx.go, "build", *ctx.doc["build"]["flags"], "-ldflags=" + ldflags(ctx.doc),
            "-o", str(partial.resolve()), ctx.doc["upstream"]["main_package"]]
    log(f"building {target['name']}")
    if run(argv, cwd=ctx.src, env=env).returncode:
        partial.unlink(missing_ok=True)
        raise BuildError(f"go build failed for {target['name']}")
    os.chmod(partial, 0o755)
    os.replace(partial, binary)
    return {"file": f"{target['name']}/{binary.name}", "sha256": sha256_file(binary),
            "size": binary.stat().st_size, "shipped": target["shipped"]}


def step_build(ctx: Context) -> None:
    state = ctx.load_state("apply")
    verify_prepared_trees(ctx, state, "build")
    built = state.setdefault("built", {})
    for target in ctx.targets(["all"]):
        built[target["name"]] = build_target(ctx, target)
    if "build" not in state["done"]:
        state["done"].append("build")
    ctx.save_state(state)


# ---- gates


def gate_applies(gate: dict[str, Any], host: str) -> bool:
    return gate["platforms"] == "portable" or host.startswith("linux-")


def gate_selected(gate: dict[str, Any], selection: str) -> bool:
    return selection == "all" or (selection == "race") == gate["race"]


def parse_test_list(output: str) -> list[str]:
    # Tests, examples and fuzz targets run; benchmarks and anything a test
    # binary prints while listing do not count.
    return [line.strip() for line in output.splitlines()
            if TEST_NAME.match(line.strip()) and not LIST_STATUS.match(line.strip())]


def gate_selection(gate: dict[str, Any], listed: list[str]) -> list[str]:
    """What the gate runs: the listed names (run already applied) minus skip."""

    skip = re.compile(gate["skip"]) if gate.get("skip") else None
    return sorted({name for name in listed if not (skip and skip.search(name))})


def check_selection(gate: dict[str, Any], selected: list[str]) -> list[str]:
    problems = []
    if not selected:
        problems.append("the test selection is empty")
    expected = set(gate["expected_tests"])
    chosen = set(selected)
    if gate.get("run"):
        if chosen != expected:
            missing, extra = sorted(expected - chosen), sorted(chosen - expected)
            problems.append(f"selection differs from expected_tests (missing {missing}, unexpected {extra})")
    elif not expected <= chosen:
        problems.append(f"expected tests not selected: {sorted(expected - chosen)}")
    return problems


def gate_flags(gate: dict[str, Any]) -> list[str]:
    # -count=1 also defeats Go's test result cache, so a gate always executes.
    flags = ["-race"] if gate["race"] else []
    flags.append(f"-count={gate['count'] or 1}")
    return flags


def run_gate(ctx: Context, gate: dict[str, Any]) -> dict[str, Any]:
    env = go_environment(ctx.work, cgo=gate["race"])
    race = ["-race"] if gate["race"] else []
    if race and not any(shutil.which(name, path=env["PATH"]) for name in (env.get("CC") or "gcc", "cc", "clang")):
        raise BuildError(f"gate {gate['name']}: race gates need a C compiler (gcc or clang)")
    started = time.monotonic()
    listing = run([ctx.go, "test", *race, "-list", gate.get("run") or ".", *gate["packages"]],
                  cwd=ctx.src, env=env, capture=True)
    if listing.returncode:
        raise BuildError(f"gate {gate['name']}: go test -list failed:\n{listing.stdout.strip()[-3000:]}")
    selected = gate_selection(gate, parse_test_list(listing.stdout))
    problems = check_selection(gate, selected)
    if problems:
        raise BuildError(f"gate {gate['name']}: " + "; ".join(problems))
    argv = [ctx.go, "test", *gate_flags(gate)]
    if gate.get("run"):
        argv += ["-run", gate["run"]]
    if gate.get("skip"):
        argv += ["-skip", gate["skip"]]
    argv += gate["packages"]
    log(f"gate {gate['name']}: {len(selected)} tests")
    status = run(argv, cwd=ctx.src, env=env).returncode
    return {"name": gate["name"], "status": "passed" if status == 0 else "failed", "tests": len(selected),
            "seconds": round(time.monotonic() - started, 1)}


def check_source_pins(doc: dict[str, Any], src: Path) -> list[str]:
    """Source texts other tools parse (log formats); a moved text fails here."""

    problems = []
    for pin in doc["source_pins"]:
        path = src / pin["path"]
        if not path.is_file() or pin["text"] not in path.read_text(encoding="utf-8", errors="replace"):
            problems.append(f"source pin {pin['name']}: {pin['path']} lacks {pin['text']!r}")
    return problems


def step_gates(ctx: Context) -> None:
    state = ctx.load_state("apply")
    verify_prepared_trees(ctx, state, "gates")
    pins = check_source_pins(ctx.doc, ctx.src)
    if pins:
        raise BuildError("; ".join(pins))
    results = []
    for gate in ctx.doc["gates"]:
        if not gate_selected(gate, ctx.gate_selection):
            continue
        if not gate_applies(gate, ctx.host):
            results.append({"name": gate["name"], "status": "not-applicable", "tests": 0, "seconds": 0})
            continue
        results.append(run_gate(ctx, gate))
    ran = [result for result in results if result["status"] != "not-applicable"]
    report = {"format": 1, "host": ctx.host, "selection": ctx.gate_selection,
              "series": state["series"], "gates": results}
    (ctx.work / "gates.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failed = [result["name"] for result in results if result["status"] == "failed"]
    if failed:
        raise BuildError("gates failed: " + ", ".join(failed))
    if not ran:
        raise BuildError(f"no gate applies to {ctx.host} with selection {ctx.gate_selection}")
    log(f"gates passed: {len(ran)} ({sum(result['tests'] for result in ran)} tests)")


# ---- inspect


def elf_facts(data: bytes) -> dict[str, Any]:
    if data[:4] != b"\x7fELF" or data[4] != 2:
        raise BuildError("not a 64-bit ELF file")
    endian = "<" if data[5] == 1 else ">"
    machine = struct.unpack_from(endian + "H", data, 18)[0]
    phoff = struct.unpack_from(endian + "Q", data, 32)[0]
    phentsize, phnum = struct.unpack_from(endian + "HH", data, 54)
    kinds = {struct.unpack_from(endian + "I", data, phoff + index * phentsize)[0] for index in range(phnum)}
    return {"machine": machine, "interpreter": 3 in kinds, "dynamic": 2 in kinds}


def macho_facts(data: bytes) -> dict[str, Any]:
    if data[:4] != b"\xcf\xfa\xed\xfe":
        raise BuildError("not a 64-bit little-endian Mach-O file")
    cpu, _subtype, _filetype, ncmds, _size = struct.unpack_from("<iiIII", data, 4)
    libraries, rpaths, signature = [], [], None
    offset = 32
    for _ in range(ncmds):
        command, size = struct.unpack_from("<II", data, offset)
        if command in (0xC, 0x80000018, 0x8000001F, 0x20, 0x80000023):
            name_offset = struct.unpack_from("<I", data, offset + 8)[0]
            raw = data[offset + name_offset:offset + size]
            libraries.append(raw.split(b"\0", 1)[0].decode("utf-8", "replace"))
        elif command == 0x8000001C:
            name_offset = struct.unpack_from("<I", data, offset + 8)[0]
            rpaths.append(data[offset + name_offset:offset + size].split(b"\0", 1)[0].decode("utf-8", "replace"))
        elif command == 0x1D:
            signature = struct.unpack_from("<II", data, offset + 8)
        offset += size
    flags = None
    if signature is not None:
        flags = code_directory_flags(data[signature[0]:signature[0] + signature[1]])
    return {"cpu": cpu & 0xFFFFFFFF, "libraries": libraries, "rpaths": rpaths, "signature_flags": flags}


def code_directory_flags(blob: bytes) -> int | None:
    """The CodeDirectory flags of an embedded signature SuperBlob (big-endian)."""

    if len(blob) < 12 or struct.unpack_from(">I", blob, 0)[0] != 0xFADE0CC0:
        return None
    count = struct.unpack_from(">I", blob, 8)[0]
    for index in range(count):
        _slot, offset = struct.unpack_from(">II", blob, 12 + index * 8)
        if struct.unpack_from(">I", blob, offset)[0] == 0xFADE0C02:
            return struct.unpack_from(">I", blob, offset + 12)[0]
    return None


def pe_facts(data: bytes) -> dict[str, Any]:
    if data[:2] != b"MZ":
        raise BuildError("not a PE file")
    header = struct.unpack_from("<I", data, 0x3C)[0]
    if data[header:header + 4] != b"PE\0\0":
        raise BuildError("missing PE signature")
    return {"machine": struct.unpack_from("<H", data, header + 4)[0]}


def inspect_binary(path: Path, target: dict[str, Any], markers: list[dict[str, Any]],
                   forbidden: Iterable[str] = ()) -> list[str]:
    """Every problem with one built binary (empty when it passes)."""

    data = path.read_bytes()
    problems = []
    match = NIX_STORE_REFERENCE.search(data)
    if match:
        problems.append(f"references a Nix store path ({match.group(0).decode()}…)")
    for text in forbidden:
        if text and text.encode() in data:
            problems.append(f"embeds the build path {text}")
    try:
        if target["goos"] == "linux":
            facts = elf_facts(data)
            if facts["machine"] != ELF_MACHINES[target["goarch"]]:
                problems.append(f"ELF machine {facts['machine']} is not {target['goarch']}")
            if facts["interpreter"] or facts["dynamic"]:
                problems.append("is dynamically linked (Linux binaries must be static)")
        elif target["goos"] == "darwin":
            facts = macho_facts(data)
            if facts["cpu"] != MACHO_CPUS[target["goarch"]]:
                problems.append(f"Mach-O cpu {facts['cpu']:#x} is not {target['goarch']}")
            foreign = [name for name in facts["libraries"] if not name.startswith(MACHO_SYSTEM_LIBRARIES)]
            if foreign:
                problems.append(f"links libraries outside the system: {foreign}")
            if facts["rpaths"]:
                problems.append(f"carries run paths: {facts['rpaths']}")
            if target["goarch"] == "arm64":
                flags = facts["signature_flags"]
                if flags is None or not flags & CS_ADHOC or not flags & CS_LINKER_SIGNED:
                    problems.append("lacks the linker's ad-hoc code signature")
        elif target["goos"] == "windows":
            facts = pe_facts(data)
            if facts["machine"] != PE_MACHINES[target["goarch"]]:
                problems.append(f"PE machine {facts['machine']:#x} is not {target['goarch']}")
        else:
            problems.append(f"no inspection rules for {target['goos']}")
    except (BuildError, struct.error, IndexError) as exc:
        problems.append(f"unreadable executable: {exc}")
    for marker in markers:
        if marker["text"].encode() not in data:
            problems.append(f"patch marker of {marker['patch']} missing")
    return problems


def markers_for(doc: dict[str, Any], series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    applied = {entry["basename"] for entry in series}
    return [marker for marker in doc["markers"] if marker["patch"] in applied]


def built_by_work(ctx: Context) -> dict[str, Any] | None:
    """The work dir's state when it built into this dist, else None."""

    path = ctx.work / "state.json"
    if not path.is_file():
        return None
    state = load_json(path)
    if state.get("upstream_sha256") != ctx.upstream_sha256 or "build" not in state.get("done", []):
        return None
    return state


def step_inspect(ctx: Context) -> None:
    """Inspect what this work dir built, or (no work dir) what BUILD.json lists."""

    state = built_by_work(ctx)
    if state is not None:
        series, built = state["series"], list(state["built"])
    elif (ctx.dist / "BUILD.json").is_file():
        record = load_json(ctx.dist / "BUILD.json")
        series, built = record["series"], list(record["targets"])
    else:
        raise BuildError(f"nothing to inspect: no prepared work dir and no {ctx.dist / 'BUILD.json'}")
    admitted = [(entry["basename"], entry["sha256"]) for entry in admitted_series(ctx.doc)]
    applied = [(entry["basename"], entry["sha256"]) for entry in series]
    forbidden = [str(ctx.work.resolve())] if ctx.work.exists() else []
    failures = []
    for target in ctx.targets(built):
        path = ctx.dist / target["name"] / binary_name(ctx.doc, target)
        if not path.is_file():
            failures.append(f"{target['name']}: {path} is missing")
            continue
        problems = inspect_binary(path, target, markers_for(ctx.doc, series), forbidden)
        if target["shipped"] and applied != admitted:
            problems.append("was not built from the admitted series")
        failures += [f"{target['name']}: {problem}" for problem in problems]
        if not problems:
            log(f"inspect {target['name']}: ok")
    if failures:
        raise BuildError("inspect failed:\n  " + "\n  ".join(failures))


# ---- record


def build_record(ctx: Context, state: dict[str, Any], targets: dict[str, Any]) -> dict[str, Any]:
    doc = ctx.doc
    # A diagnostic omission build names the hunks its omissions moved; the
    # admitted series never has any, so its record keeps its exact shape.
    moved = {"moved_hunks": state["moved_hunks"]} if state.get("moved_hunks") else {}
    return moved | {
        "format": BUILD_FORMAT,
        "upstream": {key: doc["upstream"][key] for key in ("name", "repository", "tag", "version", "commit")},
        "toolchain": state["toolchain"],
        "source": {"tree_sha256": state["source"]["tree_sha256"],
                   "archive_sha256": doc["source"]["archive_sha256"]},
        "vendor": {"tree_sha256": state["vendor"]["tree_sha256"]},
        "series": [{"basename": entry["basename"], "sha256": entry["sha256"]} for entry in state["series"]],
        "admitted_series": state["admitted_series"],
        "post_apply_tree_sha256": state["post_apply_tree_sha256"],
        "build": {"env": doc["build"]["env"], "flags": doc["build"]["flags"], "ldflags": doc["build"]["ldflags"],
                  "main_package": doc["upstream"]["main_package"]},
        "targets": targets,
    }


def recorded_targets(ctx: Context, state: dict[str, Any]) -> dict[str, Any]:
    """The selected targets this work dir built, each binary re-verified."""

    targets = {}
    for target in ctx.targets(list(state["built"])):
        targets[target["name"]] = verify_built_binary(ctx, state, target["name"])
    return targets


def verify_built_binary(ctx: Context, state: dict[str, Any], name: str) -> dict[str, Any]:
    built = state.get("built", {}).get(name)
    path = ctx.dist / name / binary_name(ctx.doc, target_records(ctx.doc)[name])
    if built is None or not path.is_file() or sha256_file(path) != built["sha256"]:
        raise BuildError(f"{name}: {path} is not the binary this work dir built")
    return built


def step_record(ctx: Context) -> None:
    state = ctx.load_state("build")
    verify_prepared_trees(ctx, state, "record")
    targets = recorded_targets(ctx, state)
    record = build_record(ctx, state, targets)
    ctx.dist.mkdir(parents=True, exist_ok=True)
    (ctx.dist / "BUILD.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (ctx.dist / "gateway-contract.json").write_bytes(contract_bytes(contract_document(ctx.doc, record["series"])))
    shipped = [name for name in targets if target_records(ctx.doc)[name]["shipped"]]
    if not state["admitted_series"]:
        log("a diagnostic series: no licence notices or SBOM are recorded")
    elif shipped:
        notices = collect_notices(ctx, state, shipped)
        checked_in = read_notice_tree(ctx.licenses)
        problems = compare_notices(notices, checked_in, shipped)
        problems += compare_sboms(ctx.doc, checked_in, ctx.sbom_dir, shipped)
        if problems:
            raise BuildError("the licence notices or SBOMs in the repository are stale:\n  "
                             + "\n  ".join(problems[:40])
                             + f"\nregenerate them from a build of every shipped target: {NOTICES_COMMAND}")
        write_notice_tree(ctx.dist / "licenses", checked_in)
        (ctx.dist / "licenses" / THIRD_PARTY_FILE).write_bytes(render_third_party_notices(checked_in))
        for name in shipped:
            target = target_records(ctx.doc)[name]
            (ctx.dist / name / (binary_name(ctx.doc, target) + SBOM_SUFFIX)).write_bytes(
                render_sbom(ctx.doc, checked_in.inventory, target))
    log(f"recorded {', '.join(targets)} in {ctx.dist / 'BUILD.json'}")


# ------------------------------------------------------------------ notices and SBOM
#
# gateway/licenses/ holds the licence and notice texts of everything linked
# into the shipped targets, copied verbatim (CLIProxyAPI, the Go toolchain's
# standard library and runtime, every Go module with the licences its
# subpackages carry, at their package-relative paths), a generated statement of
# the files the patch series modifies, and modules.json, the inventory that
# binds them to the recipe. gateway/sbom/<target>.cdx.json is the CycloneDX
# SBOM of each shipped target, rendered from that inventory. `notices`
# regenerates both from a build of every shipped target; `record` refuses a
# build they no longer describe.

NOTICES_COMMAND = "python3 tools/build.py gateway fetch vendor apply build --target shipped && python3 tools/build.py gateway notices"
NOTICES_FORMAT = 1
INVENTORY_FILE = "modules.json"
OVERRIDES_FILE = "license-overrides.json"
MODIFICATIONS_FILE = "MODIFICATIONS.txt"
THIRD_PARTY_FILE = "THIRD_PARTY_NOTICES.txt"
SBOM_SUFFIX = ".cdx.json"
DEFAULT_LICENSES = REPO_ROOT / "gateway" / "licenses"
DEFAULT_SBOM = REPO_ROOT / "gateway" / "sbom"
# The package resources the gateway recipe generates (checked in).
RESOURCES_DIR = REPO_ROOT / "src" / "claude_multi" / "data"
DEFAULT_CONTRACT = RESOURCES_DIR / "gateway-contract.json"
DEFAULT_REGISTRY = RESOURCES_DIR / "registry"
REGISTRY_SOURCE = PurePosixPath("internal/registry/models")
REGISTRY_FILES = ("models.json", "codex_client_models.json")

# Licence and notice files `go mod vendor` keeps next to a module's code: at
# the module root and in every directory between it and a vendored package,
# where a subpackage may carry its own licence and copyright.
NOTICE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|UNLICENSE|NOTICE|PATENTS|COPYRIGHT)(?:[._-].*)?$", re.IGNORECASE)
NOTICE_KINDS = {"LICENSE": "license", "LICENCE": "license", "COPYING": "license", "UNLICENSE": "license",
                "NOTICE": "notice", "PATENTS": "patents", "COPYRIGHT": "copyright"}
# Files a Go build compiles: a package's `notice_response.go` is code that
# merely shares a notice file's prefix.
SOURCE_SUFFIXES = frozenset({".go", ".s", ".sx", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
                             ".m", ".f", ".for", ".f90", ".swig", ".swigcxx", ".syso"})

BUILDINFO_MAGIC = b"\xff Go buildinf:"
BUILDINFO_FLAG_INLINE = 0x2


def notice_kind(name: str) -> str | None:
    match = NOTICE_FILE.match(name)
    if match is None or os.path.splitext(name)[1].lower() in SOURCE_SUFFIXES:
        return None
    return NOTICE_KINDS[match.group(1).upper()]


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if offset >= len(data) or shift > 63:
            raise BuildError("truncated Go build information")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def go_buildinfo(data: bytes) -> tuple[str, str]:
    """(Go version, module information) embedded in a Go executable.

    The format `go version -m` reads: a 32-byte header behind the magic,
    then two length-prefixed strings (inline since Go 1.18); the module
    text sits between two 16-byte sentinels.
    """

    start = 0
    while True:
        index = data.find(BUILDINFO_MAGIC, start)
        if index < 0:
            raise BuildError("no Go build information (not a Go 1.18+ executable)")
        start = index + 1
        if len(data) < index + 32 or not data[index + 15] & BUILDINFO_FLAG_INLINE:
            continue
        try:
            length, offset = _varint(data, index + 32)
            version = data[offset:offset + length].decode("utf-8")
            length, offset = _varint(data, offset + length)
            raw = data[offset:offset + length]
            if len(raw) >= 33 and raw[-17:-16] == b"\n":
                raw = raw[16:-16]
            modinfo = raw.decode("utf-8")
        except (BuildError, UnicodeDecodeError):
            continue
        if not version.startswith("go") or not modinfo.startswith("path\t"):
            continue
        return version, modinfo


def linked_modules(modinfo: str) -> list[tuple[str, str]]:
    """The (path, version) of every dependency module the executable links."""

    modules: list[tuple[str, str]] = []
    for line in modinfo.splitlines():
        fields = line.split("\t")
        if fields[0] == "=>":
            raise BuildError("the build replaces a module; notices support only plain module requirements")
        if fields[0] == "dep":
            if len(fields) < 3 or not fields[1] or not fields[2]:
                raise BuildError(f"malformed dependency line {line!r}")
            modules.append((fields[1], fields[2]))
    return modules


def go_sum_hashes(text: str) -> dict[tuple[str, str], str]:
    sums = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 3 and not fields[1].endswith("/go.mod"):
            sums[(fields[0], fields[1])] = fields[2]
    return sums


def _normalised(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9.,/()-]+", " ", text.lower()).split())


# Each family: patterns that must all match the normalised text (the first
# one places the family). A file that combines several licences gets every
# family it contains.
LICENSE_FAMILIES = tuple((spdx, tuple(re.compile(pattern) for pattern in patterns)) for spdx, patterns in (
    ("Apache-2.0", (r"apache license,? version 2\.0",)),
    ("MIT", (r"permission is hereby granted, free of charge, to any person obtaining a copy",
             r"the above copyright notice and this permission notice shall be included")),
    ("ISC", (r"permission to use, copy, modify, (?:and/or |and )?distribute this software for any purpose "
             r"with or without fee is hereby granted",)),
    ("MPL-2.0", (r"mozilla public license,? (?:version|v\.?) ?2\.0",)),
))
BSD_REDISTRIBUTION = "redistribution and use in source and binary forms"
BSD_ENDORSEMENT = re.compile(r"(neither the name|names? of (?:its|the) contributors may (?:not )?be used to endorse"
                             r"|may not be used to endorse or promote)")


def classify_license(text: str) -> str:
    """An SPDX expression for one licence file (families joined by AND)."""

    normalised = _normalised(text)
    found = []
    for spdx, patterns in LICENSE_FAMILIES:
        matches = [pattern.search(normalised) for pattern in patterns]
        if all(matches):
            found.append((matches[0].start(), spdx))
    redistributions = normalised.count(BSD_REDISTRIBUTION)
    if redistributions:
        endorsements = len(BSD_ENDORSEMENT.findall(normalised))
        position = normalised.find(BSD_REDISTRIBUTION)
        if endorsements:
            found.append((position, "BSD-3-Clause"))
        if redistributions > endorsements:
            found.append((position + 1, "BSD-2-Clause"))
    if not found:
        raise BuildError("unrecognised licence text")
    names = []
    for _, spdx in sorted(found):
        if spdx not in names:
            names.append(spdx)
    return " AND ".join(names)


def license_of(files: list[dict[str, Any]], texts: dict[str, bytes]) -> str:
    names: list[str] = []
    for entry in files:
        if entry["kind"] == "license":
            for name in classify_license(texts[entry["path"]].decode("utf-8", "replace")).split(" AND "):
                if name not in names:
                    names.append(name)
    return " AND ".join(names)


class NoticeTree:
    """An inventory and the bytes of every file it lists (keyed by path)."""

    def __init__(self, inventory: dict[str, Any], files: dict[str, bytes]) -> None:
        self.inventory = inventory
        self.files = files


def _notice_files(directory: Path, prefix: str, *, nested: bool = False,
                  skip: Iterable[str] = ()) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """The licence and notice files of ``directory``, listed as
    ``prefix/<relative path>``. With ``nested`` every subdirectory is
    searched too (the directory's own files first, then each subdirectory in
    name order), except the relative paths in ``skip``."""

    entries: list[dict[str, Any]] = []
    texts: dict[str, bytes] = {}
    excluded = set(skip)

    def visit(relative: PurePosixPath) -> None:
        children = sorted(directory.joinpath(*relative.parts).iterdir(), key=lambda path: path.name)
        for child in children:
            kind = notice_kind(child.name)
            if kind is None or not child.is_file():
                continue
            raw = child.read_bytes()
            path = f"{prefix}/{(relative / child.name).as_posix()}"
            entries.append({"kind": kind, "path": path, "sha256": hashlib.sha256(raw).hexdigest()})
            texts[path] = raw
        if nested:
            for child in children:
                below = relative / child.name
                if child.is_dir() and not child.is_symlink() and below.as_posix() not in excluded:
                    visit(below)

    if directory.is_dir():
        visit(PurePosixPath())
    return entries, texts


def vendored_modules(vendor: Path) -> set[str]:
    """Every module path vendor/modules.txt lists."""

    try:
        text = (vendor / "modules.txt").read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"cannot read the vendored module list: {exc}") from exc
    return {line.split()[1] for line in text.splitlines() if line.startswith("# ") and len(line.split()) > 1}


def module_notice_files(vendor: Path, module: str, modules: set[str]) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """A vendored module's licence and notice files, its subpackages'
    included; a module vendored inside its directory keeps its own."""

    inner = {other[len(module) + 1:] for other in modules if other.startswith(module + "/")}
    return _notice_files(vendor / module, f"modules/{module}", nested=True, skip=inner)


def _series_identities(series: list[dict[str, Any]]) -> list[str]:
    return [f"{entry['basename']}@{entry['sha256']}" for entry in series]


def render_modifications(doc: dict[str, Any], series: list[dict[str, Any]]) -> bytes:
    """The modified-files notice for CLIProxyAPI: each patch and its files."""

    upstream = doc["upstream"]
    lines = [
        f"cli-proxy-api is {upstream['name']} {upstream['version']} ({upstream['repository']},",
        f"commit {upstream['commit']}), distributed under the {upstream['license']} licence",
        "in LICENSE next to this file, modified by the patches below. They are applied",
        "in this order; each lists the files it modifies, adds or deletes.",
        "",
    ]
    width = len(str(len(series)))
    for number, entry in enumerate(series, 1):
        lines.append(f"{number:>{width}}. {entry['basename']}")
        lines.append(f"{'':>{width}}  sha256 {entry['sha256']}")
        for item in entry["files"]:
            lines.append(f"{'':>{width}}    {item['change']:<8}  {item['path']}")
        lines.append("")
    return ("\n".join(lines).rstrip("\n") + "\n").encode()


def read_overrides(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    document = load_json(path)
    if not isinstance(document, dict) or document.get("format") != NOTICES_FORMAT or \
            not isinstance(document.get("modules"), dict):
        raise BuildError(f"{path}: expected {{'format': 1, 'modules': {{path: {{license, reason}}}}}}")
    for module, entry in document["modules"].items():
        if not isinstance(entry, dict) or set(entry) != {"license", "reason"} or \
                not all(isinstance(value, str) and value for value in entry.values()):
            raise BuildError(f"{path}: {module} needs exactly a licence (SPDX) and a reason")
    return document["modules"]


def collect_notices(ctx: Context, state: dict[str, Any], targets: list[str]) -> NoticeTree:
    """Licence notices for ``targets`` from this work dir's build: the
    modules each binary links (its embedded build information), their
    vendored licence and notice files (subpackages' included), their go.sum
    hashes, the toolchain's and the upstream licence, and the patch series'
    modified files."""

    doc = ctx.doc
    linked: dict[tuple[str, str], list[str]] = {}
    toolchain = "go" + doc["toolchain"]["version"]
    for name in targets:
        target = target_records(doc)[name]
        path = ctx.dist / name / binary_name(doc, target)
        version, modinfo = go_buildinfo(path.read_bytes())
        if version != toolchain:
            raise BuildError(f"{name}: built by {version}, the recipe pins {toolchain}")
        for module in linked_modules(modinfo):
            linked.setdefault(module, []).append(name)
    sums = go_sum_hashes((ctx.src / "go.sum").read_text(encoding="utf-8"))
    overrides = read_overrides(ctx.licenses / OVERRIDES_FILE)
    vendored = vendored_modules(ctx.src / "vendor")
    files: dict[str, bytes] = {}
    modules = []
    for (module, version), linking in sorted(linked.items()):
        if (module, version) not in sums:
            raise BuildError(f"go.sum has no hash for {module} {version}")
        if module not in vendored:
            raise BuildError(f"{module} is linked but not vendored")
        entries, texts = module_notice_files(ctx.src / "vendor", module, vendored)
        files.update(texts)
        if any(entry["kind"] == "license" for entry in entries):
            if module in overrides:
                raise BuildError(f"{OVERRIDES_FILE}: {module} ships its own licence")
            try:
                spdx = license_of(entries, texts)
            except BuildError as exc:
                raise BuildError(f"{module}: {exc}; extend the licence families in tools/build.py") from None
            record = {"license": spdx}
        elif module in overrides:
            record = {"license": overrides[module]["license"], "license_override": overrides[module]["reason"]}
        else:
            raise BuildError(f"{module} {version} ships no licence; record it in {OVERRIDES_FILE}")
        modules.append({"path": module, "version": version, "sum": sums[(module, version)],
                        "targets": sorted(linking), "files": entries, **record})
    unused = sorted(set(overrides) - {module for module, _ in linked})
    if unused:
        raise BuildError(f"{OVERRIDES_FILE} names modules no shipped target links: {', '.join(unused)}")
    go_files, texts = _notice_files(ctx.work / "go", "go")
    files.update(texts)
    # The standard library's vendored golang.org/x packages carry the
    # toolchain's own texts; a different one there would need listing.
    std_files, std_texts = _notice_files(ctx.work / "go" / "src" / "vendor", "go/src/vendor", nested=True)
    foreign = [entry["path"] for entry in std_files if std_texts[entry["path"]] not in texts.values()]
    if foreign:
        raise BuildError(f"the Go standard library vendors licence texts the toolchain root lacks: {', '.join(foreign)}")
    upstream_files, texts = _notice_files(ctx.src, "CLIProxyAPI", nested=True, skip=("vendor",))
    files.update(texts)
    if not any(entry["kind"] == "license" for entry in go_files) or \
            not any(entry["kind"] == "license" for entry in upstream_files):
        raise BuildError("the Go toolchain or the upstream source ships no licence file")
    if license_of(upstream_files, files) != doc["upstream"]["license"]:
        raise BuildError(f"the upstream licence file is not the recipe's {doc['upstream']['license']}")
    modifications = render_modifications(doc, state["series"])
    files[f"CLIProxyAPI/{MODIFICATIONS_FILE}"] = modifications
    upstream = doc["upstream"]
    inventory = {
        "format": NOTICES_FORMAT,
        "upstream": {"name": upstream["name"], "version": upstream["version"], "commit": upstream["commit"],
                     "repository": upstream["repository"], "license": license_of(upstream_files, files),
                     "files": upstream_files,
                     "modifications": {"path": f"CLIProxyAPI/{MODIFICATIONS_FILE}",
                                       "sha256": hashlib.sha256(modifications).hexdigest()}},
        "toolchain": {"name": "Go", "version": doc["toolchain"]["version"],
                      "license": license_of(go_files, files), "files": go_files},
        "inputs": {"source_tree_sha256": state["source"]["tree_sha256"],
                   "vendor_tree_sha256": state["vendor"]["tree_sha256"],
                   "post_apply_tree_sha256": state["post_apply_tree_sha256"],
                   "series": _series_identities(state["series"])},
        "targets": sorted(targets),
        "modules": modules,
    }
    return NoticeTree(inventory, files)


def inventory_bytes(inventory: dict[str, Any]) -> bytes:
    return (json.dumps(inventory, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def listed_paths(inventory: dict[str, Any]) -> list[str]:
    paths = [entry["path"] for entry in inventory["upstream"]["files"] + inventory["toolchain"]["files"]]
    paths.append(inventory["upstream"]["modifications"]["path"])
    paths += [entry["path"] for module in inventory["modules"] for entry in module["files"]]
    return paths


def read_notice_tree(root: Path) -> NoticeTree:
    """The checked-in notice tree; every listed file must carry its sha256."""

    if not (root / INVENTORY_FILE).is_file():
        raise BuildError(f"no {INVENTORY_FILE} in {root}; generate the notices: {NOTICES_COMMAND}")
    inventory = load_json(root / INVENTORY_FILE)
    if not isinstance(inventory, dict) or inventory.get("format") != NOTICES_FORMAT:
        raise BuildError(f"{root / INVENTORY_FILE}: unsupported format")
    files = {}
    for path in listed_paths(inventory):
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise BuildError(f"{root / INVENTORY_FILE}: unsafe path {path!r}")
        file = root / path
        if not file.is_file():
            raise BuildError(f"{file} is missing")
        files[path] = file.read_bytes()
    return NoticeTree(inventory, files)


def write_notice_tree(root: Path, tree: NoticeTree) -> None:
    """Replace ``root`` with exactly the tree (the overrides file is kept)."""

    overrides = root / OVERRIDES_FILE
    kept = overrides.read_bytes() if overrides.is_file() else None
    remove_tree(root)
    root.mkdir(parents=True)
    for path, raw in sorted(tree.files.items()):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(raw)
    (root / INVENTORY_FILE).write_bytes(inventory_bytes(tree.inventory))
    if kept is not None:
        overrides.write_bytes(kept)


def compare_notices(observed: NoticeTree, checked_in: NoticeTree, targets: list[str]) -> list[str]:
    """Differences between a build's notices and the checked-in ones, for
    the modules ``targets`` link (a partial build checks its part)."""

    problems = []
    mine, theirs = observed.inventory, checked_in.inventory
    for key in ("upstream", "toolchain", "inputs"):
        if mine[key] != theirs[key]:
            problems.append(f"{INVENTORY_FILE}: {key} differs")
    missing = sorted(set(targets) - set(theirs["targets"]))
    if missing:
        problems.append(f"{INVENTORY_FILE} does not cover {', '.join(missing)}")
    recorded = {}
    for module in theirs["modules"]:
        linking = sorted(set(module["targets"]) & set(targets))
        if linking:
            recorded[(module["path"], module["version"])] = {**module, "targets": linking}
    built = {(module["path"], module["version"]): module for module in mine["modules"]}
    for key in sorted(set(recorded) | set(built)):
        if key not in recorded:
            problems.append(f"{key[0]} {key[1]} is linked but not recorded")
        elif key not in built:
            problems.append(f"{key[0]} {key[1]} is recorded but not linked")
        elif recorded[key] != built[key]:
            problems.append(f"{key[0]} {key[1]} differs")
    for path, raw in sorted(observed.files.items()):
        if path not in checked_in.files:
            problems.append(f"{path} is not checked in")
        elif checked_in.files[path] != raw:
            problems.append(f"{path} differs")
    return problems


def _purl(path: str, version: str) -> str:
    return "pkg:golang/" + "/".join(urllib.parse.quote(part, safe="-._~") for part in path.split("/")) + \
        "@" + urllib.parse.quote(version, safe="-._~")


def _licenses(expression: str) -> list[dict[str, Any]]:
    if " " in expression:
        return [{"expression": expression}]
    return [{"license": {"id": expression}}]


def _h1_hex(value: str) -> str:
    if not value.startswith("h1:"):
        raise BuildError(f"unsupported module hash {value!r}")
    try:
        raw = base64.b64decode(value[3:], validate=True)
    except ValueError:
        raise BuildError(f"malformed module hash {value!r}") from None
    if len(raw) != 32:
        raise BuildError(f"malformed module hash {value!r}")
    return raw.hex()


def render_sbom(doc: dict[str, Any], inventory: dict[str, Any], target: dict[str, Any]) -> bytes:
    """The CycloneDX 1.6 SBOM of one shipped target: the linked modules with
    their go.sum hashes and licences, the Go standard library, and the
    pedigree (the upstream at its commit, the ordered patches, the patched
    tree hash), toolchain and build flags. Deterministic: no timestamp, and
    the serial number is derived from the content."""

    upstream, inputs = doc["upstream"], inventory["inputs"]
    if target["name"] not in inventory["targets"]:
        raise BuildError(f"{INVENTORY_FILE} does not cover {target['name']}")
    go_version = "go" + inventory["toolchain"]["version"]
    components = [{
        "type": "library", "bom-ref": _purl(module["path"], module["version"]),
        "name": module["path"], "version": module["version"], "purl": _purl(module["path"], module["version"]),
        "hashes": [{"alg": "SHA-256", "content": _h1_hex(module["sum"])}],
        "licenses": _licenses(module["license"]),
    } for module in inventory["modules"] if target["name"] in module["targets"]]
    components.append({"type": "library", "bom-ref": _purl("std", go_version), "name": "std",
                       "version": go_version, "purl": _purl("std", go_version),
                       "description": "the Go standard library and runtime",
                       "licenses": _licenses(inventory["toolchain"]["license"])})
    properties = [("target", target["name"]), ("goos", target["goos"]), ("goarch", target["goarch"]),
                  ("toolchain", go_version),
                  ("build-env", " ".join(f"{key}={value}" for key, value in sorted(doc["build"]["env"].items()))),
                  ("build-flags", " ".join(doc["build"]["flags"])), ("ldflags", " ".join(doc["build"]["ldflags"])),
                  ("source-tree-sha256", inputs["source_tree_sha256"]),
                  ("vendor-tree-sha256", inputs["vendor_tree_sha256"]),
                  ("post-apply-tree-sha256", inputs["post_apply_tree_sha256"])]
    properties += [("patch", identity) for identity in inputs["series"]]
    ancestor_purl = _purl(upstream["module"], "v" + upstream["version"])
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "tools": {"components": [{"type": "application", "group": "claude-multi", "name": "build.py",
                                      "description": "tools/build.py gateway"}]},
            "component": {
                "type": "application", "bom-ref": upstream["binary"], "name": upstream["binary"],
                "version": upstream["version"],
                "description": f"{upstream['name']} {upstream['version']} with claude-multi's patches ({target['name']})",
                "licenses": _licenses(inventory["upstream"]["license"]),
                "pedigree": {
                    "ancestors": [{
                        "type": "application", "bom-ref": ancestor_purl, "name": upstream["name"],
                        "version": upstream["version"], "purl": ancestor_purl,
                        "licenses": _licenses(inventory["upstream"]["license"]),
                        "externalReferences": [
                            {"type": "vcs", "url": upstream["repository"]},
                            {"type": "distribution", "url": doc["source"]["archive_url"],
                             "hashes": [{"alg": "SHA-256", "content": doc["source"]["archive_sha256"]}]}],
                    }],
                    "commits": [{"uid": upstream["commit"], "url": f"{upstream['repository']}/commit/{upstream['commit']}"}],
                    "patches": [{"type": "unofficial", "diff": {"url": "gateway/patches/" + identity.split("@", 1)[0]}}
                                for identity in inputs["series"]],
                    "notes": ("Built from the upstream source at the commit above with the patches applied in "
                              "order; each patch's sha256 and the patched tree's normalised sha256 (vendor/ "
                              "excluded) are the claude-multi:gateway properties."),
                },
                "properties": [{"name": f"claude-multi:gateway:{name}", "value": value} for name, value in properties],
            },
        },
        "components": components,
        "dependencies": [{"ref": upstream["binary"], "dependsOn": [item["bom-ref"] for item in components]}],
    }
    digest = hashlib.sha256(json.dumps(bom, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    bom = {"bomFormat": bom.pop("bomFormat"), "specVersion": bom.pop("specVersion"),
           "serialNumber": "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "claude-multi:gateway-sbom:" + digest)),
           **bom}
    return (json.dumps(bom, indent=2, ensure_ascii=False) + "\n").encode()


def compare_sboms(doc: dict[str, Any], tree: NoticeTree, root: Path, targets: list[str]) -> list[str]:
    problems = []
    for name in targets:
        path = root / (name + SBOM_SUFFIX)
        expected = render_sbom(doc, tree.inventory, target_records(doc)[name])
        if not path.is_file() or path.read_bytes() != expected:
            problems.append(f"{path.name} differs from its rendering")
    return problems


def render_third_party_notices(tree: NoticeTree) -> bytes:
    """Every listed licence and notice text in one file, for distribution."""

    inventory, rule = tree.inventory, "-" * 72
    upstream, toolchain = inventory["upstream"], inventory["toolchain"]
    parts = [
        f"Third-party notices for cli-proxy-api {upstream['version']}",
        "",
        f"cli-proxy-api is {upstream['name']} {upstream['version']} with claude-multi's patches. This",
        "file reproduces the licence and notice texts of everything its shipped",
        "builds link: the upstream source, the Go standard library and runtime,",
        "and each Go module (path, version and SPDX licence in each heading).",
        "",
    ]

    def section(title: str, prefix: str, files: list[dict[str, Any]], extra: list[str] = ()) -> None:
        # Each text is headed by its path inside the module or tree, so a
        # subpackage's own licence names the package it covers.
        parts.extend([rule, title, rule, ""])
        for path in [entry["path"] for entry in files] + list(extra):
            parts.append(f"== {path.removeprefix(prefix + '/')}")
            parts.append("")
            parts.append(tree.files[path].decode("utf-8", "replace").rstrip("\n"))
            parts.append("")

    section(f"{upstream['name']} {upstream['version']} ({upstream['license']})", "CLIProxyAPI", upstream["files"],
            [upstream["modifications"]["path"]])
    section(f"Go {toolchain['version']} standard library and runtime ({toolchain['license']})", "go",
            toolchain["files"])
    for module in inventory["modules"]:
        title = f"{module['path']} {module['version']} ({module['license']})"
        if module.get("license_override"):
            title += f" — no licence file shipped: {module['license_override']}"
        section(title, f"modules/{module['path']}", module["files"])
    return ("\n".join(parts).rstrip("\n") + "\n").encode()


def command_notices(ctx: Context) -> None:
    """Regenerate gateway/licenses and gateway/sbom from this work dir."""

    state = ctx.load_state("build")
    if not state["admitted_series"]:
        raise BuildError("notices describe the admitted series; this work dir applied a diagnostic series")
    verify_prepared_trees(ctx, state, "notices")
    shipped = [target["name"] for target in ctx.doc["targets"] if target["shipped"]]
    missing = [name for name in shipped if name not in state.get("built", {})]
    if missing:
        raise BuildError(f"notices need every shipped target built; missing: {', '.join(missing)}")
    for name in shipped:
        verify_built_binary(ctx, state, name)
    tree = collect_notices(ctx, state, shipped)
    write_notice_tree(ctx.licenses, tree)
    remove_tree(ctx.sbom_dir)
    ctx.sbom_dir.mkdir(parents=True)
    for name in shipped:
        (ctx.sbom_dir / (name + SBOM_SUFFIX)).write_bytes(
            render_sbom(ctx.doc, tree.inventory, target_records(ctx.doc)[name]))
    log(f"wrote {len(tree.inventory['modules'])} modules' notices to {ctx.licenses} and "
        f"{len(shipped)} SBOMs to {ctx.sbom_dir}")


def command_contract(ctx: Context) -> None:
    """Write the gateway contract of the admitted series (no build needed)."""

    series = resolve_series(ctx)
    if ctx.explicit_patches:
        raise BuildError("the contract describes the admitted series; drop --patch")
    out = ctx.out or DEFAULT_CONTRACT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(contract_bytes(contract_document(ctx.doc, series)))
    log(f"wrote {out}")


def command_registry(ctx: Context) -> None:
    """Copy the pristine pinned source's model registry into the resources."""

    state = ctx.load_state("fetch")
    if state["done"] != ["fetch"]:
        raise BuildError(f"{ctx.work} is past the fetch step; the registry is the pristine pinned source — "
                         "run the fetch step again, then registry")
    if tree_sha256(ctx.src) != state["source"]["tree_sha256"]:
        raise BuildError(f"{ctx.src} was edited after the fetch step; run the fetch step again")
    out = ctx.out or DEFAULT_REGISTRY
    out.mkdir(parents=True, exist_ok=True)
    for name in REGISTRY_FILES:
        source = ctx.src / REGISTRY_SOURCE / name
        if not source.is_file():
            raise BuildError(f"the pinned source carries no {REGISTRY_SOURCE / name}")
        (out / name).write_bytes(source.read_bytes())
    log(f"wrote {', '.join(REGISTRY_FILES)} to {out}")


# ------------------------------------------------------------------ commands


def command_env(ctx: Context) -> None:
    ctx.load_state("fetch")
    environment = go_environment(ctx.work)
    for key in ("GOROOT", "GOTOOLCHAIN", "GOFLAGS", "GOPROXY", "GOSUMDB", "GOENV", "GOWORK", "GOTELEMETRY",
                "GOCACHE", "GOPATH", "GOMODCACHE", "CGO_ENABLED", "HOME", "PATH"):
        value = environment[key].replace("'", "'\"'\"'")
        print(f"export {key}='{value}'")


def command_compare(ctx: Context) -> None:
    if ctx.other is None:
        raise BuildError("compare needs --other PATH")
    targets = ctx.targets(["linux-amd64"])
    if len(targets) != 1:
        raise BuildError("compare takes one --target")
    target = targets[0]
    mine = ctx.dist / target["name"] / binary_name(ctx.doc, target)
    if not mine.is_file() or not ctx.other.is_file():
        raise BuildError(f"both binaries must exist: {mine}, {ctx.other}")
    ours, theirs = sha256_file(mine), sha256_file(ctx.other)
    print(json.dumps({"target": target["name"], "dist": ours, "other": theirs, "identical": ours == theirs},
                     sort_keys=True))
    if ours != theirs:
        raise BuildError(f"{target['name']} differs: {mine} {ours}, {ctx.other} {theirs}")


def command_repro(ctx: Context) -> None:
    targets = ctx.targets(["linux-amd64"])
    scratch = ctx.scratch or ctx.cache / "repro"
    scratch.mkdir(parents=True, exist_ok=True)
    if ctx.inputs is None:
        # Make sure both builds read the same verified, cached inputs.
        locate_toolchain(ctx)
        locate_source(ctx)
    hashes: dict[str, dict[str, str]] = {}
    with tempfile.TemporaryDirectory(prefix="gateway-repro-", dir=scratch) as root:
        for label, relative in (("first", "a/work"), ("second", "second-build/nested/w")):
            work = Path(root) / relative
            dist = Path(root) / (label + "-dist")
            argv = ["gateway", "fetch", "vendor", "apply", "build",
                    "--upstream", str(ctx.upstream_path), "--patches", str(ctx.patch_dir),
                    "--cache", str(ctx.cache), "--work", str(work), "--dist", str(dist)]
            for patch in ctx.explicit_patches:
                argv += ["--patch", str(patch)]
            if ctx.inputs is not None:
                argv += ["--inputs", str(ctx.inputs)]
            argv += ["--offline"]
            for target in targets:
                argv += ["--target", target["name"]]
            log(f"reproducibility build {label} in {work}")
            run_pipeline(parse_args(argv))
            hashes[label] = {target["name"]: sha256_file(dist / target["name"] / binary_name(ctx.doc, target))
                             for target in targets}
            remove_tree(work)
    identical = hashes["first"] == hashes["second"]
    print(json.dumps({"targets": hashes["first"], "second": hashes["second"], "identical": identical},
                     indent=2, sort_keys=True))
    if not identical:
        raise BuildError("the two builds differ")



RELEASE_DOC = """Assemble the release bundles (no Nix needed).

    python3 tools/build.py release [--out DIR] [OPTIONS]
    python3 tools/build.py release compare --out DIR --other DIR
    python3 tools/build.py release repro [OPTIONS]
    python3 tools/build.py bundle [--target T ...] [--out DIR] [OPTIONS]

release builds every target's bundle into --out (default dist/release, which
must be empty or absent) with the release's MANIFEST.json, SHA256SUMS and the
installers (install.sh and install.ps1 with the release's version, the
bundles' checksums and signing keys filled in; SHA256SUMS lists the manifest,
the bundles and both installers); bundle builds the bundles of the targets
named (default: this host's) with MANIFEST.json and SHA256SUMS, no installers.

Each bundle carries the gateway the recipe built (--gateway-dist DIR takes the
dist directory of an earlier `build.py gateway ... record` run, checked
against the recipe and the checked-in notices and SBOMs; without it the
gateway steps run first, into the work dir), the pinned python-build-standalone
runtime for its target (packaging/python-runtimes.json: fetched by hash,
pruned by its delete-only list, the standard library compiled), the launcher
package with its bytecode and resources, the documents, licences and
CycloneDX SBOMs, the launchers in bin/ and MANIFEST.json. Bytecode is compiled
by the pinned interpreter native to this host (or --python, which must report
the pinned version); the build continues under that interpreter (unless
--no-reexec) so builders agree on the compressed bytes too. The bundle for
this host's own target is run before it is archived: every launcher module
and every standard-library module the launcher imports load under the pruned
runtime, and bin/claude-multi --version answers.

Archives are deterministic: the source date (--source-date-epoch, else
$SOURCE_DATE_EPOCH, else the commit time of HEAD) is every entry's mtime and
the release date in the manifests; a bundle's identity is the sha256 of its
uncompressed tar.

A release carries what an installation needs to update itself: the release
signing key (src/claude_multi/data/release-trust/allowed_signers) and the
download locations (packaging/product.json release_base_url and
release_latest_url). release refuses to build without either, and refuses
--base-url. --test-build (for tests and local trials only, never a release)
allows all three and marks the build summary as a test build; a test build's
MANIFEST.json names no release key or no location, and tools/release.py
refuses to sign, verify or publish it.

The documents are the [tool.setuptools.data-files] table of pyproject.toml,
the one layout of the wheel, the bundles and the Nix package: every key is
share/claude-multi or share/claude-multi/<dir>, every file lies under docs/
in the directory the key names (docs/<dir>/), and each is installed at
<key>/<file name> under the bundle root, so relative links between documents
resolve the same in a checkout and in every installation.

Commands (release only):

  compare  compare the build in --out with --other: the contents (tar sha256,
           gateway, launcher bytecode) must be identical; differing gzip bytes
           are reported only
  repro    assemble twice in different directories from the same inputs and
           require identical archives

Inputs are cached in ~/.cache/claude-multi-build (or $CLAUDE_MULTI_BUILD_CACHE,
or --cache); --offline reads only the cache (and --inputs, for the gateway).
"""

# ------------------------------------------------------------------ release bundles
#
# `bundle` and `release` assemble the installable bundles (tools/_build/bundle.py
# describes their layout) from three inputs, each verified here:
#
# - the gateway recipe's outputs: a dist directory the gateway steps wrote
#   (BUILD.json, gateway-contract.json, the notices, each target's binary and
#   SBOM). They are checked against the recipe and the checked-in notices and
#   SBOMs and copied as they are; nothing is built a second way. Without
#   --gateway-dist the gateway steps run first (fetch vendor apply build
#   inspect record) for the bundle targets.
# - the pinned python-build-standalone runtimes (packaging/python-runtimes.json):
#   fetched by hash, pruned by the delete-only list, the standard library
#   compiled to bytecode.
# - the launcher package (src/claude_multi with its data/), compiled once
#   with the pinned interpreter native to this host, the same bytecode in
#   every bundle; packaging/product.json maps each bundle target to its
#   gateway target and Claude Code platform and names the launchers.
#
# The archives are deterministic (bundle.write_archive); MANIFEST.json in
# each bundle and the release's MANIFEST.json record what went in.

PACKAGING = PurePosixPath("packaging")
# The documents' installed directory (pyproject.toml's data-files keys are it
# or a directory under it) and their directory in the source tree.
DOCUMENTS_KEY = PurePosixPath("share/claude-multi")
DOCUMENTS_SOURCE = PurePosixPath("docs")
# Launchers a bundle never ships: the contributor's entry point, and the
# one-model alias `claude-multi direct` replaces.
NON_BUNDLE_LAUNCHERS = ("claude-gateway", "claude-multi-dev")
TEST_BUILD_FLAG = "--test-build"
RELEASE_FILES = ("MANIFEST.json", "SHA256SUMS")
INSTALLERS = ("install.sh", "install.ps1")
RELEASE_COMMANDS = ("compare", "repro")
REEXEC_ENV = "CLAUDE_MULTI_BUILD_REEXEC"
PRODUCT_FORMAT = 1
RELEASE_FORMAT = 1
TRUST_PRINCIPAL = "release@claude-multi"
_VERSION_TEXT = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-dev)?")
# The installer lines the release build fills in (packaging/install.sh and
# packaging/install.ps1 leave them empty).
INSTALL_SH_FIELDS = ("RELEASE_VERSION", "RELEASE_BASE_URL", "RELEASE_SIGNERS", "RELEASE_SUMS")
INSTALL_PS1_FIELDS = ("Version", "InstallerUrl", "InstallerSha256")


def release_log(message: str) -> None:
    print(f"release: {message}", file=sys.stderr, flush=True)


def build_module(name: str) -> Any:
    """A module of tools/_build, loaded once under a private name."""

    import importlib.util

    key = f"_claude_multi_build_{name}"
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).resolve().parent / "_build" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise BuildError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


class ReleaseContext:
    """The inputs of a bundle or release build, every path absolute."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.artifact = args.artifact
        self.repo = absolute(args.repo) if args.repo else REPO_ROOT
        packaging = self.repo / PACKAGING
        self.src = self.repo / "src"
        self.package = self.src / "claude_multi"
        self.resources = self.package / "data"
        self.product_path = absolute(args.product) if args.product else packaging / "product.json"
        self.runtimes_path = absolute(args.runtimes) if args.runtimes else packaging / "python-runtimes.json"
        self.upstream_path = absolute(args.upstream) if args.upstream else self.repo / "gateway" / "UPSTREAM.json"
        self.patch_dir = absolute(args.patches) if args.patches else self.repo / "gateway" / "patches"
        self.licenses = absolute(args.licenses) if args.licenses else self.repo / "gateway" / "licenses"
        self.sbom_dir = absolute(args.sbom) if args.sbom else self.repo / "gateway" / "sbom"
        self.installers = packaging
        self.cache = absolute(args.cache) if args.cache else absolute(default_cache())
        self.work = absolute(args.work) if args.work else self.cache / "work" / "release"
        self.out = absolute(args.out) if args.out else REPO_ROOT / "dist" / "release"
        self.gateway_dist = absolute(args.gateway_dist) if args.gateway_dist else None
        self.inputs = absolute(args.inputs) if args.inputs else None
        self.offline = bool(args.offline)
        self.python = absolute(args.python) if args.python else None
        self.reexec = not args.no_reexec and self.python is None
        self.base_url = args.base_url
        self.test_build = bool(getattr(args, "test_build", False))
        self.epoch_arg = args.source_date_epoch
        self.target_args = list(getattr(args, "target", None) or [])
        self.other = absolute(args.other) if args.other else None
        self.scratch = absolute(args.scratch) if args.scratch else None
        self.argv = list(args.raw_argv)
        self.doc = load_json(self.upstream_path)
        check_upstream(self.doc)
        self.runtime_tool = build_module("python_runtime")
        self.bundle = build_module("bundle")
        self.licence_tool = build_module("runtime_licenses")
        self.runtime_licenses_path = (absolute(args.runtime_licenses) if getattr(args, "runtime_licenses", None)
                                      else packaging / "licenses" / "python")
        try:
            self.runtimes = self.runtime_tool.load(self.runtimes_path)
            self.runtime_licenses = self.licence_tool.load(self.runtime_licenses_path)
        except (self.runtime_tool.BuildError, self.licence_tool.LicenseError) as exc:
            raise BuildError(str(exc)) from None
        self.product = load_product(self.product_path, self.doc, self.runtimes)
        version = load_json(self.resources / "version.json")
        if not isinstance(version, dict) or not _VERSION_TEXT.fullmatch(str(version.get("launcher_version"))):
            raise BuildError(f"{self.resources / 'version.json'}: no launcher_version")
        self.version = version["launcher_version"]
        self.catalog_version = version.get("catalog_version")
        self.contract = load_json(self.resources / "catalog" / "native-contract.json")

    def targets(self) -> list[str]:
        known = list(self.product["targets"])
        if self.artifact == "release":
            if self.target_args:
                raise BuildError("a release always has every target; build a subset with `bundle --target`")
            return known
        names = self.target_args or ["host"]
        chosen: list[str] = []
        for name in names:
            expanded = [self.runtime_tool.host_target()] if name == "host" else known if name == "all" else [name]
            for item in expanded:
                if item not in known:
                    raise BuildError(f"unknown bundle target {item!r}; known: {', '.join(known)}")
                if item not in chosen:
                    chosen.append(item)
        return chosen


def load_product(path: Path, doc: dict[str, Any], runtimes: Any) -> dict[str, Any]:
    """packaging/product.json: the bundle targets and the launchers."""

    product = load_json(path)
    where = str(path)
    if not isinstance(product, dict) or product.get("format") != PRODUCT_FORMAT or product.get("name") != "claude-multi":
        raise BuildError(f"{where}: not a claude-multi product description (format 1)")
    if set(product) != {"format", "name", "launchers", "release_base_url", "release_latest_url", "targets"}:
        raise BuildError(f"{where}: expected exactly format, name, launchers, release_base_url, "
                         "release_latest_url and targets")
    launchers = product["launchers"]
    if (not isinstance(launchers, list) or not launchers or launchers != sorted(set(launchers))
            or "claude-multi" not in launchers or "claude-multi-proxy" not in launchers):
        raise BuildError(f"{where}: launchers must be sorted and unique and name claude-multi and "
                         "claude-multi-proxy")
    refused = [name for name in launchers if name in NON_BUNDLE_LAUNCHERS]
    if refused:
        raise BuildError(f"{where}: a bundle never ships {', '.join(refused)} (claude-multi-dev is the "
                         "contributor's command; claude-multi direct starts a one-model session)")
    base = product["release_base_url"]
    if base is not None and not (isinstance(base, str) and base.startswith("https://")):
        raise BuildError(f"{where}: release_base_url must be null or an https URL")
    latest = product["release_latest_url"]
    if latest is not None and not (isinstance(latest, str) and latest.startswith("https://")
                                   and "{version}" not in latest):
        raise BuildError(f"{where}: release_latest_url must be null or an https URL without {{version}}")
    targets = product["targets"]
    if not isinstance(targets, dict) or set(targets) != set(runtimes.targets):
        raise BuildError(f"{where}: targets must be exactly the pinned runtimes' {sorted(runtimes.targets)}")
    recipe = target_records(doc)
    gateway_targets = []
    for name, entry in sorted(targets.items()):
        if not isinstance(entry, dict) or set(entry) != {"claude_code", "gateway"}:
            raise BuildError(f"{where}: target {name} needs exactly claude_code and gateway")
        gateway = recipe.get(entry["gateway"])
        if gateway is None or not gateway["shipped"]:
            raise BuildError(f"{where}: target {name} names {entry['gateway']!r}, not a shipped gateway target")
        gateway_targets.append(entry["gateway"])
    if len(set(gateway_targets)) != len(gateway_targets):
        raise BuildError(f"{where}: two bundle targets share a gateway target")
    return product


def claude_pin(contract: Any) -> dict[str, Any]:
    """The native contract's pinned entry (the newest verified version)."""

    verified = contract.get("verified") if isinstance(contract, dict) else None
    if not isinstance(verified, list) or not verified:
        raise BuildError("the native contract pins no Claude Code version")

    def key(item: dict[str, Any]) -> tuple[int, ...]:
        version = str(item.get("version", ""))
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise BuildError(f"the native contract names version {version!r}")
        return tuple(int(part) for part in version.split("."))

    return max(verified, key=key)


def state_format(package: Path) -> int:
    """The state format the launcher writes (``SUPPORTED_STATE_VERSION`` in
    sessions.py, read without importing the package)."""

    import ast

    path = package / "sessions.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        raise BuildError(f"cannot read {path}: {exc}") from exc
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "SUPPORTED_STATE_VERSION"
                                                for target in node.targets):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, int) and not isinstance(value.value, bool):
                return value.value
    raise BuildError(f"{path} defines no SUPPORTED_STATE_VERSION")


def release_trust(resources: Path) -> dict[str, Any]:
    """The packaged release trust (data/release-trust/allowed_signers): its
    sha256 and the Ed25519 signer lines for the release principal."""

    path = resources / "release-trust" / "allowed_signers"
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BuildError(f"cannot read the release trust {path}: {exc}") from exc
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 3 or TRUST_PRINCIPAL not in fields[0].split(",") or "ssh-ed25519" not in fields[1:3]:
            raise BuildError(f"{path}: every line must name {TRUST_PRINCIPAL} with an ssh-ed25519 key")
        if "'" in stripped:
            raise BuildError(f"{path}: a signer line must not contain a single quote")
        lines.append(stripped)
    return {"sha256": hashlib.sha256(raw).hexdigest(), "signers": lines}


def source_date_epoch(ctx: ReleaseContext) -> int:
    """--source-date-epoch, else $SOURCE_DATE_EPOCH, else the checkout's
    HEAD commit time."""

    value = ctx.epoch_arg if ctx.epoch_arg is not None else os.environ.get("SOURCE_DATE_EPOCH")
    if value is None or value == "":
        git = git_environment(ctx.repo)
        git.pop("GIT_CEILING_DIRECTORIES", None)
        result = run(["git", "-C", str(ctx.repo), "log", "-1", "--format=%ct", "HEAD"], cwd=ctx.repo, env=git,
                     capture=True)
        value = result.stdout.strip() if result.returncode == 0 else ""
        if not value:
            raise BuildError("no source date: set SOURCE_DATE_EPOCH or --source-date-epoch "
                             "(outside a git checkout there is no commit time to use)")
    try:
        epoch = int(value)
    except (TypeError, ValueError):
        raise BuildError(f"SOURCE_DATE_EPOCH must be an integer, not {value!r}") from None
    if epoch < 0 or epoch > 253402300799:
        raise BuildError(f"SOURCE_DATE_EPOCH {epoch} is out of range")
    return epoch


def release_date(epoch: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


def host_python(ctx: ReleaseContext) -> Path:
    """The interpreter that compiles the bytecode: the pinned runtime native
    to this host (unpacked under the work dir), or --python, which must be
    the same CPython version."""

    tool = ctx.runtime_tool
    try:
        if ctx.python is not None:
            version = tool.interpreter_version(ctx.python)
            if version != ctx.runtimes.python:
                raise BuildError(f"{ctx.python} is CPython {version}; the bundles pin {ctx.runtimes.python}")
            return ctx.python
        (ctx.work / "host-python").mkdir(parents=True, exist_ok=True)
        return tool.host_interpreter(ctx.runtimes, ctx.work / "host-python", cache=ctx.cache / "python-runtimes",
                                     offline=ctx.offline)
    except tool.BuildError as exc:
        raise BuildError(str(exc)) from None


def maybe_reexec(ctx: ReleaseContext, python: Path) -> None:
    """Continue under the pinned host interpreter, so every builder writes
    the gzip bytes of the same zlib (the uncompressed content never depends
    on it)."""

    if not ctx.reexec or os.environ.get(REEXEC_ENV) == "1":
        return
    if os.path.realpath(sys.executable) == os.path.realpath(python):
        return
    release_log(f"continuing under the pinned interpreter {python}")
    environment = dict(os.environ)
    environment[REEXEC_ENV] = "1"
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(str(python), [str(python), "-I", "-B", str(Path(__file__).resolve()), *ctx.argv], environment)


def gateway_outputs(ctx: ReleaseContext, targets: list[str]) -> Path:
    """The gateway dist the bundles take their gateway from."""

    if ctx.gateway_dist is not None:
        return ctx.gateway_dist
    dist = ctx.work / "dist-gateway"
    remove_tree(dist)
    argv = ["gateway", "fetch", "vendor", "apply", "build", "inspect", "record",
            "--upstream", str(ctx.upstream_path), "--patches", str(ctx.patch_dir), "--cache", str(ctx.cache),
            "--work", str(ctx.work / "gateway"), "--dist", str(dist), "--licenses", str(ctx.licenses),
            "--sbom", str(ctx.sbom_dir)]
    if ctx.inputs is not None:
        argv += ["--inputs", str(ctx.inputs)]
    if ctx.offline:
        argv += ["--offline"]
    for name in targets:
        argv += ["--target", ctx.product["targets"][name]["gateway"]]
    release_log("building the gateway for the bundles")
    run_pipeline(parse_args(argv))
    remove_tree(ctx.work / "gateway")
    return dist


def check_gateway_dist(ctx: ReleaseContext, dist: Path, gateway_targets: list[str]) -> dict[str, dict[str, Any]]:
    """Validate a gateway dist against the recipe and the checked-in notices
    and SBOMs; returns each target's binary record."""

    doc = ctx.doc
    where = dist / "BUILD.json"
    if not where.is_file():
        raise BuildError(f"{dist} holds no gateway build (BUILD.json); build it with `build.py gateway`")
    record = load_json(where)
    problems = []

    def expect(key: str, value: Any, wanted: Any) -> None:
        if value != wanted:
            problems.append(f"{key} is {value!r}, the recipe says {wanted!r}")

    if not isinstance(record, dict) or record.get("format") != BUILD_FORMAT:
        raise BuildError(f"{where}: unsupported format")
    admitted = admitted_series(doc)
    expect("upstream", record.get("upstream"),
           {key: doc["upstream"][key] for key in ("name", "repository", "tag", "version", "commit")})
    expect("toolchain.version", (record.get("toolchain") or {}).get("version"), doc["toolchain"]["version"])
    expect("source", record.get("source"), {"tree_sha256": doc["source"]["tree_sha256"],
                                            "archive_sha256": doc["source"]["archive_sha256"]})
    expect("vendor", record.get("vendor"), {"tree_sha256": doc["vendor"]["tree_sha256"]})
    expect("admitted_series", record.get("admitted_series"), True)
    expect("series", record.get("series"), [{"basename": entry["basename"], "sha256": entry["sha256"]}
                                           for entry in admitted])
    expect("build", record.get("build"), {"env": doc["build"]["env"], "flags": doc["build"]["flags"],
                                          "ldflags": doc["build"]["ldflags"],
                                          "main_package": doc["upstream"]["main_package"]})
    contract = contract_bytes(contract_document(doc, admitted))
    contract_path = dist / "gateway-contract.json"
    if not contract_path.is_file() or contract_path.read_bytes() != contract:
        problems.append("gateway-contract.json is not the recipe's contract")
    packaged = ctx.resources / "gateway-contract.json"
    if not packaged.is_file() or packaged.read_bytes() != contract:
        problems.append(f"the packaged {packaged.name} is not the recipe's contract (run build.py gateway contract)")
    checked_in = read_notice_tree(ctx.licenses)
    try:
        shipped_notices = read_notice_tree(dist / "licenses")
    except BuildError as exc:
        problems.append(f"licenses: {exc}")
        shipped_notices = None
    if shipped_notices is not None:
        if shipped_notices.inventory != checked_in.inventory or shipped_notices.files != checked_in.files:
            problems.append(f"licenses differ from the checked-in notices in {ctx.licenses}")
        third_party = dist / "licenses" / THIRD_PARTY_FILE
        if not third_party.is_file() or third_party.read_bytes() != render_third_party_notices(checked_in):
            problems.append(f"licenses/{THIRD_PARTY_FILE} is not the rendering of the notices")
    targets = record.get("targets") if isinstance(record.get("targets"), dict) else {}
    markers = markers_for(doc, admitted)
    binaries: dict[str, dict[str, Any]] = {}
    for name in gateway_targets:
        target = target_records(doc)[name]
        binary = binary_name(doc, target)
        entry = targets.get(name)
        path = dist / name / binary
        if name not in checked_in.inventory["targets"]:
            problems.append(f"the notices do not cover {name}")
        if not isinstance(entry, dict) or entry.get("file") != f"{name}/{binary}" or entry.get("shipped") is not True:
            problems.append(f"BUILD.json has no shipped record of {name}")
            continue
        if not path.is_file():
            problems.append(f"{name}: {path} is missing")
            continue
        if path.stat().st_size != entry.get("size") or sha256_file(path) != entry.get("sha256"):
            problems.append(f"{name}: {path} is not the binary BUILD.json records")
            continue
        problems += [f"{name}: {problem}" for problem in inspect_binary(path, target, markers)]
        sbom = dist / name / (binary + SBOM_SUFFIX)
        expected = ctx.sbom_dir / (name + SBOM_SUFFIX)
        if not sbom.is_file() or not expected.is_file() or sbom.read_bytes() != expected.read_bytes():
            problems.append(f"{name}: the SBOM is not the checked-in {expected.name}")
        binaries[name] = {"path": path, "sha256": entry["sha256"], "size": entry["size"], "sbom": sbom}
    if problems:
        raise BuildError(f"the gateway outputs in {dist} cannot go into a bundle:\n  " + "\n  ".join(problems))
    return binaries


def stage_launcher(ctx: ReleaseContext, python: Path, epoch: int) -> tuple[Path, str]:
    """The launcher package with its bytecode, staged once for every bundle:
    ``(site directory, bytecode sha256)``."""

    tool, bundle = ctx.runtime_tool, ctx.bundle
    site_relative = bundle.site_dir(ctx.runtimes.python)
    root = ctx.work / "launcher"
    remove_tree(root)
    site = root / site_relative
    bundle.copy_tree(ctx.package, site / "claude_multi")
    try:
        tool.compile_tree(python, site, site_relative, epoch, expected_version=ctx.runtimes.python)
    except tool.BuildError as exc:
        raise BuildError(str(exc)) from None
    return site, tool.pyc_digest(site)


def document_layout(table: Any, *, where: str = "pyproject.toml") -> list[tuple[PurePosixPath, PurePosixPath]]:
    """The documents of a [tool.setuptools.data-files] table as
    ``(source, installed path)`` pairs, both relative: every key is
    share/claude-multi or share/claude-multi/<dir>, every file lies under
    docs/ in that <dir> (docs/<dir>/<name>), and no two entries install the
    same path. Anything else is refused."""

    if not isinstance(table, dict) or not table:
        raise BuildError(f"{where}: [tool.setuptools.data-files] lists no documents")
    pairs: list[tuple[PurePosixPath, PurePosixPath]] = []
    installed: dict[PurePosixPath, PurePosixPath] = {}
    for key, files in table.items():
        directory = PurePosixPath(key) if isinstance(key, str) else None
        if (directory is None or key != directory.as_posix() or ".." in directory.parts
                or directory.parts[:len(DOCUMENTS_KEY.parts)] != DOCUMENTS_KEY.parts):
            raise BuildError(f"{where}: data-files key {key!r} is not {DOCUMENTS_KEY} or a directory under it")
        below = PurePosixPath(*directory.parts[len(DOCUMENTS_KEY.parts):])
        if not isinstance(files, list) or not files:
            raise BuildError(f"{where}: data-files key {key!r} lists no files")
        for name in files:
            source = PurePosixPath(name) if isinstance(name, str) else None
            if (source is None or name != source.as_posix() or source.is_absolute() or ".." in source.parts
                    or source.parts[:len(DOCUMENTS_SOURCE.parts)] != DOCUMENTS_SOURCE.parts
                    or len(source.parts) <= len(DOCUMENTS_SOURCE.parts)):
                raise BuildError(f"{where}: data-files entry {name!r} is not a file under {DOCUMENTS_SOURCE}/")
            if PurePosixPath(*source.parent.parts[len(DOCUMENTS_SOURCE.parts):]) != below:
                raise BuildError(f"{where}: data-files entry {name!r} is installed into {key}, but its directory "
                                 f"below {DOCUMENTS_SOURCE}/ is not {'/'.join(below.parts) or 'the top'}: the key "
                                 "and the directory must agree, so relative links resolve the same when installed")
            target = directory / source.name
            if target in installed:
                raise BuildError(f"{where}: {installed[target]} and {source} both install {target}")
            installed[target] = source
            pairs.append((source, target))
    return pairs


def documents(repo: Path) -> list[tuple[Path, PurePosixPath]]:
    """The documents pyproject.toml installs: ``(file, installed path)``,
    the installed path relative to the installation (bundle) root."""

    import tomllib

    path = repo / "pyproject.toml"
    try:
        project = tomllib.loads(path.read_text(encoding="utf-8"))
        table = project["tool"]["setuptools"]["data-files"]
    except (OSError, ValueError, KeyError) as exc:
        raise BuildError(f"cannot read the installed documents from {path}: {exc}") from exc
    pairs = document_layout(table, where=str(path))
    found = [(repo / source, target) for source, target in pairs]
    missing = [str(source) for source, _target in found if not source.is_file()]
    if missing:
        raise BuildError(f"{path}: data-files names files that do not exist: {', '.join(missing)}")
    return found


def import_check(stage: Path, python_version: str, *, home: Path) -> dict[str, Any]:
    """Run the host target's bundle: every launcher module and every
    standard-library module the launcher imports, under the pruned runtime
    with -I, and the launcher's --version through its bin/ entry."""

    bundle = build_module("bundle")
    runtime = stage / bundle.RUNTIME_DIR / "bin" / "python3"
    site = stage / bundle.site_dir(python_version)
    program = r"""
import ast, importlib, json, pkgutil, sys
site = sys.argv[1]
sys.path.insert(0, site)
import claude_multi
failed, launcher, wanted = [], [], set()
for info in pkgutil.walk_packages(claude_multi.__path__, "claude_multi."):
    launcher.append(info.name)
for name in ["claude_multi"] + launcher:
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        failed.append(f"{name}: {type(exc).__name__}: {exc}")
        continue
    try:
        source = open(module.__file__, encoding="utf-8").read()
    except (OSError, TypeError):
        continue
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            wanted.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            wanted.add(node.module.split(".")[0])
stdlib = sorted(name for name in wanted if name in sys.stdlib_module_names)
missing = []
for name in stdlib:
    try:
        importlib.import_module(name)
    except ImportError as exc:
        missing.append(f"{name}: {exc}")
print(json.dumps({"launcher_modules": len(launcher) + 1, "stdlib_modules": stdlib,
                  "failed": failed, "missing": missing, "flags": [sys.flags.isolated, sys.dont_write_bytecode]}))
"""
    environment = {"HOME": str(home), "PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"}
    before = _tree_paths(stage)
    result = subprocess.run([str(runtime), "-I", "-B", "-c", program, str(site)], capture_output=True, text=True,
                            timeout=600, env=environment, stdin=subprocess.DEVNULL, cwd=home)
    if result.returncode != 0:
        raise BuildError(f"the bundled runtime cannot import the launcher:\n{(result.stdout + result.stderr)[-3000:]}")
    report = json.loads(result.stdout.strip().splitlines()[-1])
    problems = report["failed"] + [f"standard library: {item}" for item in report["missing"]]
    if report["flags"] != [1, True]:
        problems.append(f"the runtime did not run isolated without bytecode writes: {report['flags']}")
    version = subprocess.run([str(stage / "bin" / "claude-multi"), "--version"], capture_output=True, text=True,
                             timeout=120, env=environment, stdin=subprocess.DEVNULL, cwd=home)
    if version.returncode != 0 or not version.stdout.startswith("claude-multi "):
        problems.append(f"bin/claude-multi --version failed: {(version.stdout + version.stderr).strip()[-600:]}")
    written = sorted(_tree_paths(stage) - before)
    if written:
        problems.append(f"running the bundle wrote into it: {written[:5]}")
    if problems:
        raise BuildError("the bundle does not run:\n  " + "\n  ".join(problems))
    return {"launcher_modules": report["launcher_modules"], "stdlib_modules": len(report["stdlib_modules"]),
            "version": version.stdout.strip()}


def _tree_paths(root: Path) -> set[str]:
    return {os.path.relpath(os.path.join(directory, name), root)
            for directory, dirs, files in os.walk(root) for name in dirs + files}


def assemble_target(ctx: ReleaseContext, target: str, *, python: Path, epoch: int, site: Path, bytecode: str,
                    gateway: dict[str, Any], trust: dict[str, Any], out: Path) -> dict[str, Any]:
    """Stage one target's bundle and archive it into ``out``."""

    tool, bundle = ctx.runtime_tool, ctx.bundle
    entry = ctx.product["targets"][target]
    runtime = ctx.runtimes.runtime(target)
    top = bundle.bundle_name(ctx.version, target)
    stage_parent = ctx.work / "stage" / target
    remove_tree(stage_parent)
    root = stage_parent / top
    root.mkdir(parents=True)
    release_log(f"assembling {top}")
    try:
        prepared = tool.prepare(ctx.runtimes, target, root / "runtime", cache=ctx.cache / "python-runtimes",
                                offline=ctx.offline)
        stdlib = bundle.stdlib_dir(ctx.runtimes.python)
        tool.compile_tree(python, root / stdlib, stdlib, epoch, expected_version=ctx.runtimes.python)
    except tool.BuildError as exc:
        raise BuildError(f"{target}: {exc}") from None
    licences = ctx.licence_tool
    try:
        licences.check_shipped(ctx.runtime_licenses, target, root / stdlib / "lib-dynload")
    except licences.LicenseError as exc:
        raise BuildError(str(exc)) from None
    site_relative = bundle.site_dir(ctx.runtimes.python)
    shutil.copytree(site, root / site_relative, symlinks=False)
    if PurePosixPath(bundle.DOCS_DIR) != DOCUMENTS_KEY:
        raise BuildError(f"the bundle's documents directory {bundle.DOCS_DIR} is not {DOCUMENTS_KEY}")
    for document, installed in documents(ctx.repo):
        bundle.install_file(document, root / installed)
    bundle.install_file(gateway["path"], root / bundle.GATEWAY_PATH, executable=True)
    for name in ctx.product["launchers"]:
        bundle.write_file(root / "bin" / name, bundle.launcher_text(name, ctx.runtimes.python), executable=True)
    licenses = root / bundle.LICENSES_DIR
    bundle.install_file(ctx.repo / "LICENSE", licenses / "claude-multi" / "LICENSE")
    notices = read_notice_tree(ctx.licenses)
    for path, raw in sorted(notices.files.items()):
        bundle.write_file(licenses / "cli-proxy-api" / path, raw)
    bundle.write_file(licenses / "cli-proxy-api" / INVENTORY_FILE, inventory_bytes(notices.inventory))
    bundle.write_file(licenses / "cli-proxy-api" / THIRD_PARTY_FILE, render_third_party_notices(notices))
    python_license = root / stdlib / "LICENSE.txt"
    if not python_license.is_file():
        raise BuildError(f"{target}: the runtime carries no {stdlib}/LICENSE.txt")
    bundle.install_file(python_license, licenses / "python" / "LICENSE.txt")
    try:
        licences.install(ctx.runtime_licenses, target, licenses / "python")
    except (licences.LicenseError, OSError) as exc:
        raise BuildError(f"{target}: the runtime's licence texts: {exc}") from None
    sboms = root / bundle.SBOM_DIR
    bundle.install_file(gateway["sbom"], sboms / "cli-proxy-api.cdx.json")
    described = ctx.runtime_licenses.target(target)
    bundle.write_file(sboms / "python.cdx.json", bundle.python_sbom(
        version=ctx.runtimes.python, release=ctx.runtimes.release, target=target, triple=runtime.triple,
        filename=runtime.filename, url=runtime.url, sha256=runtime.sha256, pruned=prepared["removed"],
        license_ids=described["runtime"]["spdx"], components=licences.sbom_components(ctx.runtime_licenses, target),
        removed=[item["extension"] for item in described["pruned"]]))
    bundle.write_file(sboms / "claude-multi.cdx.json", bundle.launcher_sbom(version=ctx.version))
    try:
        bundle.check_links(root)
    except bundle.BundleError as exc:
        raise BuildError(f"{target}: {exc}") from None
    stored = tool.store_references(root)
    if stored:
        raise BuildError(f"{target}: the bundle names /nix/store paths: {', '.join(stored[:5])}")
    problems = inspect_binary(root / bundle.GATEWAY_PATH, target_records(ctx.doc)[entry["gateway"]],
                              markers_for(ctx.doc, admitted_series(ctx.doc)))
    if problems:
        raise BuildError(f"{target}: the bundled gateway: " + "; ".join(problems))
    checked = None
    if target == tool.host_target():
        home = ctx.work / "home"
        remove_tree(home)
        home.mkdir(mode=0o700, parents=True)
        checked = import_check(root, ctx.runtimes.python, home=home)
        remove_tree(home)
    content, files = bundle.content_digest(root)
    pin = claude_pin(ctx.contract)
    platform_record = pin.get("platforms", {}).get(entry["claude_code"])
    if not isinstance(platform_record, dict):
        raise BuildError(f"{target}: the native contract has no {entry['claude_code']} build")
    manifest = {
        "format": bundle.FORMAT,
        "name": bundle.NAME,
        "version": ctx.version,
        "target": target,
        "release_date": release_date(epoch),
        "source_date_epoch": epoch,
        "state_format": state_format(ctx.package),
        "catalog_version": ctx.catalog_version,
        "claude_code": {"version": pin["version"], "platform": entry["claude_code"],
                        "sha256": platform_record["sha256"], "size": platform_record["size"]},
        "gateway": {"name": ctx.doc["upstream"]["name"], "version": ctx.doc["upstream"]["version"],
                    "target": entry["gateway"], "path": bundle.GATEWAY_PATH, "sha256": gateway["sha256"],
                    "size": gateway["size"], "toolchain": "go" + ctx.doc["toolchain"]["version"],
                    "patches": contract_document(ctx.doc, admitted_series(ctx.doc))["patches"]},
        "python": {"version": ctx.runtimes.python, "release": ctx.runtimes.release, "path": bundle.RUNTIME_DIR,
                   "archive": runtime.filename, "archive_sha256": runtime.sha256, "pruned": prepared["removed"]},
        "launcher": {"path": f"{site_relative}/claude_multi", "bytecode_sha256": bytecode,
                     "launchers": ctx.product["launchers"]},
        "release_trust": {"sha256": trust["sha256"], "signers": len(trust["signers"])},
        "release": release_location(ctx),
        "content": {"sha256": content, "files": files},
    }
    bundle.write_file(root / bundle.MANIFEST_NAME, bundle.manifest_bytes(manifest))
    archive = out / bundle.archive_name(ctx.version, target)
    try:
        identity = bundle.write_archive(stage_parent, top, archive, epoch)
    except bundle.BundleError as exc:
        raise BuildError(f"{target}: {exc}") from None
    remove_tree(stage_parent)
    release_log(f"{archive.name}: {identity['size']} bytes, tar sha256 {identity['tar_sha256']}")
    return {"name": archive.name, "target": target, **identity, "gateway_sha256": gateway["sha256"],
            "manifest_sha256": hashlib.sha256(bundle.manifest_bytes(manifest)).hexdigest(),
            "content_sha256": content, "import_check": checked}


def release_location(ctx: "ReleaseContext") -> dict[str, str | None]:
    """Where an installed bundle looks for its updates (``claude-multi
    update``): the download location, kept with its ``{version}``
    placeholder (the installers and ``claude-multi update`` fill it in per
    version), and the latest release's location; null until the product
    names them."""

    base = ctx.base_url if ctx.base_url is not None else ctx.product["release_base_url"]
    latest = ctx.product["release_latest_url"] if ctx.base_url is None else None
    return {"base_url": base, "latest_url": latest}


def fill_shell_fields(text: str, values: dict[str, str]) -> str:
    """packaging/install.sh with its release metadata lines filled in."""

    for name in INSTALL_SH_FIELDS:
        line = f"{name}=''"
        if text.count("\n" + line + "\n") != 1:
            raise BuildError(f"install.sh must carry exactly one empty {line} line")
        value = values.get(name, "")
        if "'" in value:
            raise BuildError(f"the installer value of {name} must not contain a single quote")
        text = text.replace("\n" + line + "\n", f"\n{name}='{value}'\n", 1)
    return text


def fill_powershell_fields(text: str, values: dict[str, str]) -> str:
    """packaging/install.ps1 with Get-ReleaseInfo's fields filled in."""

    for name in INSTALL_PS1_FIELDS:
        pattern = re.compile(rf"(?m)^(        {name}\s*= )''$")
        if len(pattern.findall(text)) != 1:
            raise BuildError(f"install.ps1 must carry exactly one empty {name} field")
        value = values.get(name, "")
        if "'" in value:
            raise BuildError(f"the installer value of {name} must not contain a single quote")
        text = pattern.sub(lambda match: f"{match.group(1)}'{value}'", text, count=1)
    return text


def write_release_files(ctx: ReleaseContext, out: Path, assets: list[dict[str, Any]], *, epoch: int,
                        bytecode: str, trust: dict[str, Any], installers: bool) -> dict[str, str]:
    """The release's MANIFEST.json and SHA256SUMS (and the installers):
    the sha256 of each file written."""

    pin = claude_pin(ctx.contract)
    manifest = {
        "format": RELEASE_FORMAT,
        "name": "claude-multi",
        "version": ctx.version,
        "test_build": ctx.test_build,
        "release_date": release_date(epoch),
        "source_date_epoch": epoch,
        "state_format": state_format(ctx.package),
        "catalog_version": ctx.catalog_version,
        "claude_code": {"version": pin["version"],
                        "platforms": {name: {"sha256": record["sha256"], "size": record["size"]}
                                      for name, record in sorted(pin.get("platforms", {}).items())}},
        "gateway": {"name": ctx.doc["upstream"]["name"], "version": ctx.doc["upstream"]["version"],
                    "toolchain": "go" + ctx.doc["toolchain"]["version"],
                    "patches": contract_document(ctx.doc, admitted_series(ctx.doc))["patches"]},
        "python": {"version": ctx.runtimes.python, "release": ctx.runtimes.release},
        "launcher": {"bytecode_sha256": bytecode},
        "release_trust": {"sha256": trust["sha256"], "signers": len(trust["signers"])},
        "release": release_location(ctx),
        "assets": {asset["name"]: {key: asset[key] for key in ("target", "size", "sha256", "tar_sha256",
                                                                "gateway_sha256", "manifest_sha256",
                                                                "content_sha256")}
                   for asset in assets},
    }
    manifest_raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    (out / "MANIFEST.json").write_bytes(manifest_raw)
    listed = {"MANIFEST.json": hashlib.sha256(manifest_raw).hexdigest()}
    listed.update({asset["name"]: asset["sha256"] for asset in assets})
    written = {"MANIFEST.json": listed["MANIFEST.json"]}
    if installers:
        # The installer embeds the bundles' checksums only (for a host
        # without ssh-keygen); SHA256SUMS then lists both installers too.
        bundles = bundle_sums(listed)
        base = ctx.base_url if ctx.base_url is not None else ctx.product["release_base_url"]
        if base is not None and not base.startswith("https://"):
            raise BuildError("--base-url must be an https URL")
        shell = fill_shell_fields((ctx.installers / "install.sh").read_text(encoding="utf-8"), {
            "RELEASE_VERSION": ctx.version, "RELEASE_BASE_URL": base or "",
            "RELEASE_SIGNERS": "\n".join(trust["signers"]), "RELEASE_SUMS": bundles.rstrip("\n")})
        (out / "install.sh").write_text(shell, encoding="utf-8")
        os.chmod(out / "install.sh", 0o755)
        shell_sha256 = hashlib.sha256(shell.encode("utf-8")).hexdigest()
        url = f"{base.rstrip('/').replace('{version}', ctx.version)}/install.sh" if base else ""
        powershell = fill_powershell_fields((ctx.installers / "install.ps1").read_text(encoding="utf-8"), {
            "Version": ctx.version, "InstallerUrl": url, "InstallerSha256": shell_sha256})
        (out / "install.ps1").write_text(powershell, encoding="utf-8")
        written["install.sh"] = listed["install.sh"] = shell_sha256
        written["install.ps1"] = listed["install.ps1"] = hashlib.sha256(powershell.encode("utf-8")).hexdigest()
    sums = "".join(f"{digest}  {name}\n" for name, digest in sorted(listed.items()))
    (out / "SHA256SUMS").write_text(sums, encoding="ascii")
    written["SHA256SUMS"] = hashlib.sha256(sums.encode("ascii")).hexdigest()
    return written


def bundle_sums(listed: dict[str, str]) -> str:
    """The checksum lines of the release's bundle archives, in SHA256SUMS
    order: what install.sh carries (never its own or install.ps1's sum)."""

    return "".join(f"{digest}  {name}\n" for name, digest in sorted(listed.items()) if name.endswith(".tar.gz"))


def release_input_problems(ctx: ReleaseContext, trust: dict[str, Any]) -> list[str]:
    """What a release lacks to be published, each with its remedy: the
    packaged release key, the download locations product.json names, and
    no --base-url override. Empty for a releasable build."""

    problems = []
    if not trust["signers"]:
        problems.append("the packaged release trust (src/claude_multi/data/release-trust/allowed_signers) names "
                        "no signing key: add the production public key line (release@claude-multi "
                        "namespaces=\"claude-multi-release\" ssh-ed25519 ...); an installation verifies its "
                        "updates with it")
    for key in ("release_base_url", "release_latest_url"):
        if ctx.product[key] is None:
            problems.append(f"{ctx.product_path} names no {key}: set the https download location the installer "
                            "and `claude-multi update` use")
    if ctx.base_url is not None:
        problems.append("--base-url replaces the download location of packaging/product.json, which a release "
                        "and its installations use: change release_base_url there instead")
    return problems


def refuse_unpublishable(ctx: ReleaseContext) -> None:
    """Before anything is fetched or built: a release without its key or its
    download location cannot update the installations it makes. A bundle
    build and a --test-build pass."""

    if ctx.artifact != "release" or ctx.test_build:
        return
    problems = release_input_problems(ctx, release_trust(ctx.resources))
    if problems:
        raise BuildError("this release cannot be published:\n  " + "\n  ".join(problems)
                         + f"\n({TEST_BUILD_FLAG} builds it anyway for tests and local trials, never for a "
                           "release)")


def prepare_out(out: Path) -> None:
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise BuildError(f"{out} is not an empty directory; remove it or choose another --out")
    out.mkdir(parents=True, exist_ok=True)


def run_release(ctx: ReleaseContext) -> dict[str, Any]:
    """Build the bundles (and, for `release`, the release files) into --out."""

    targets = ctx.targets()
    refuse_unpublishable(ctx)
    epoch = source_date_epoch(ctx)
    python = host_python(ctx)
    maybe_reexec(ctx, python)
    try:
        ctx.licence_tool.check_runtimes(ctx.runtime_licenses, ctx.runtimes, targets)
    except ctx.licence_tool.LicenseError as exc:
        raise BuildError(str(exc)) from None
    trust = release_trust(ctx.resources)
    prepare_out(ctx.out)
    ctx.work.mkdir(parents=True, exist_ok=True)
    try:
        dist = gateway_outputs(ctx, targets)
        gateway = check_gateway_dist(ctx, dist, [ctx.product["targets"][name]["gateway"] for name in targets])
        site, bytecode = stage_launcher(ctx, python, epoch)
        assets = []
        try:
            for name in targets:
                assets.append(assemble_target(ctx, name, python=python, epoch=epoch, site=site, bytecode=bytecode,
                                              gateway=gateway[ctx.product["targets"][name]["gateway"]],
                                              trust=trust, out=ctx.out))
        finally:
            remove_tree(ctx.work / "launcher")
            remove_tree(ctx.work / "stage")
        written = write_release_files(ctx, ctx.out, assets, epoch=epoch, bytecode=bytecode, trust=trust,
                                      installers=ctx.artifact == "release")
    except BaseException:
        # --out was empty: a failed build leaves nothing half-written there.
        for leftover in ctx.out.iterdir():
            remove_tree(leftover)
        raise
    missing = release_input_problems(ctx, trust)
    if ctx.test_build:
        release_log(f"a test build ({TEST_BUILD_FLAG}): never sign or publish it"
                    + (": " + "; ".join(problem.split(":", 1)[0] for problem in missing) if missing else ""))
    elif not trust["signers"]:
        release_log("the packaged release trust names no signing key: these bundles cannot verify an update "
                    "with their own trust")
    summary = {"version": ctx.version, "source_date_epoch": epoch, "out": str(ctx.out),
               "test_build": ctx.test_build,
               "launcher_bytecode_sha256": bytecode, "release_key": bool(trust["signers"]),
               "assets": {asset["name"]: {key: asset[key] for key in ("target", "sha256", "size", "tar_sha256",
                                                                       "manifest_sha256", "import_check")}
                          for asset in assets},
               "files": written}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


_SUMS_LINE = re.compile(r"([0-9a-f]{64})  ([^\s/][^/\n]*)")
# What a gzip encoding changes: the compressed archives' checksums and sizes,
# and the installer fields that carry them (install.sh's checksums, and
# install.ps1's checksum of install.sh).
_SHELL_SUMS = re.compile(r"\nRELEASE_SUMS='([^']*)'\n")
_POWERSHELL_SHA256 = re.compile(r"(?m)^(        InstallerSha256 = )'([0-9a-f]*)'$")


def parse_release_sums(directory: Path) -> tuple[str, dict[str, str]]:
    """``(text, {name: sha256})`` of the release's SHA256SUMS (its format
    only; :func:`release_sums` checks it against the files)."""

    path = directory / "SHA256SUMS"
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise BuildError(f"{path}: unreadable ({exc})") from exc
    sums: dict[str, str] = {}
    for line in text.splitlines():
        match = _SUMS_LINE.fullmatch(line)
        if match is None or match.group(2) in sums:
            raise BuildError(f"{path}: not a release checksum list (line {line[:80]!r})")
        sums[match.group(2)] = match.group(1)
    return text, sums


def release_sums(directory: Path, listed: Iterable[str]) -> str:
    """The release's SHA256SUMS text, checked against the files: exactly
    the ``listed`` names, each with the sha256 of the file beside it."""

    path = directory / "SHA256SUMS"
    text, sums = parse_release_sums(directory)
    if not text.endswith("\n") or sorted(sums) != sorted(listed):
        raise BuildError(f"{path} does not list exactly MANIFEST.json, the release's archives and its installers")
    for name, digest in sums.items():
        if not (directory / name).is_file() or sha256_file(directory / name) != digest:
            raise BuildError(f"{directory / name} does not match {path}")
    return text


def release_has_installers(directory: Path) -> bool:
    """Whether ``directory`` holds the installers (both, or neither: a
    `bundle` build)."""

    present = [name for name in INSTALLERS if os.path.lexists(directory / name)]
    if present and (present != list(INSTALLERS) or not all((directory / name).is_file() for name in INSTALLERS)):
        raise BuildError(f"{directory} must hold both installers ({', '.join(INSTALLERS)}) or neither")
    return bool(present)


def release_installers(directory: Path, sums: str) -> dict[str, str] | None:
    """The installers' identity with the gzip-dependent fields emptied
    (each must carry this release's values first: install.sh the bundle
    checksums of ``sums``, install.ps1 the sha256 of install.sh), or None for
    a build without installers (`bundle`)."""

    if not release_has_installers(directory):
        return None
    shell_raw = (directory / "install.sh").read_bytes()
    shell = shell_raw.decode("utf-8", errors="replace")
    filled = _SHELL_SUMS.findall(shell)
    bundles = "".join(line + "\n" for line in sums.splitlines() if line.endswith(".tar.gz"))
    if filled != [bundles.rstrip("\n")]:
        raise BuildError(f"{directory / 'install.sh'} does not carry the checksums of this release's bundles")
    powershell = (directory / "install.ps1").read_bytes().decode("utf-8", errors="replace")
    pinned = _POWERSHELL_SHA256.findall(powershell)
    if [digest for _prefix, digest in pinned] != [hashlib.sha256(shell_raw).hexdigest()]:
        raise BuildError(f"{directory / 'install.ps1'} does not carry the sha256 of this release's install.sh")
    return {
        "install.sh": hashlib.sha256(_SHELL_SUMS.sub("\nRELEASE_SUMS=''\n", shell, count=1).encode()).hexdigest(),
        "install.ps1": hashlib.sha256(_POWERSHELL_SHA256.sub(r"\1''", powershell, count=1).encode()).hexdigest(),
    }


def release_identity(directory: Path) -> dict[str, Any]:
    """What two builds of one release must share: each asset's content and
    gateway, the launcher bytecode, the manifest and the installers (their
    compressed-archive checksums aside); every release file is checked
    against SHA256SUMS and the manifest first."""

    manifest = load_json(directory / "MANIFEST.json")
    if not isinstance(manifest, dict) or manifest.get("format") != RELEASE_FORMAT:
        raise BuildError(f"{directory / 'MANIFEST.json'}: not a release manifest")
    for name, asset in manifest["assets"].items():
        path = directory / name
        if not path.is_file() or sha256_file(path) != asset["sha256"]:
            raise BuildError(f"{path} is not the archive MANIFEST.json lists")
        if build_module("bundle").tar_sha256(path) != asset["tar_sha256"]:
            raise BuildError(f"{path}: its content is not the tar sha256 MANIFEST.json lists")
    parse_release_sums(directory)
    sums = release_sums(directory, ["MANIFEST.json", *manifest["assets"],
                                    *(INSTALLERS if release_has_installers(directory) else ())])
    installers = release_installers(directory, sums)
    normal = copy.deepcopy(manifest)
    for asset in normal["assets"].values():
        asset.pop("sha256", None)
        asset.pop("size", None)
    files = {name: sha256_file(directory / name) for name in (*RELEASE_FILES, *(INSTALLERS if installers else ()))}
    return {"version": manifest["version"], "launcher": manifest["launcher"],
            "assets": {name: {key: asset[key] for key in ("tar_sha256", "gateway_sha256", "manifest_sha256")}
                       for name, asset in manifest["assets"].items()},
            "manifest": hashlib.sha256(json.dumps(normal, sort_keys=True).encode()).hexdigest(),
            "installers": installers,
            "archives": {name: asset["sha256"] for name, asset in manifest["assets"].items()},
            "files": files}


def compare_releases(first: Path, second: Path) -> dict[str, Any]:
    """Two builds of one release: the gateway bytes, each bundle's
    uncompressed archive (every file, the bytecode included), the launcher
    bytecode set, the manifest and both installers (each directory's
    SHA256SUMS checked against its files), then the compressed archives and
    the release files that record their checksums."""

    ours, theirs = release_identity(first), release_identity(second)
    archives = ours.pop("archives") == theirs.pop("archives")
    files = ours.pop("files") == theirs.pop("files")

    def per_asset(key: str) -> bool:
        return ({name: asset[key] for name, asset in ours["assets"].items()}
                == {name: asset[key] for name, asset in theirs["assets"].items()})

    content = ours == theirs
    report = {"content_identical": content, "archives_identical": content and archives and files,
              "gateways_identical": per_asset("gateway_sha256"), "tar_identical": per_asset("tar_sha256"),
              "bytecode_identical": ours["launcher"].get("bytecode_sha256") == theirs["launcher"].get("bytecode_sha256"),
              "manifest_identical": ours["manifest"] == theirs["manifest"],
              "installers_identical": ours["installers"] == theirs["installers"],
              "first": str(first), "second": str(second)}
    if not content:
        differences = sorted(name for name in set(ours["assets"]) | set(theirs["assets"])
                             if ours["assets"].get(name) != theirs["assets"].get(name))
        if ours["installers"] is None or theirs["installers"] is None:
            if ours["installers"] != theirs["installers"]:
                differences.append("the installers (one build has none)")
        else:
            differences += [name for name in INSTALLERS if ours["installers"][name] != theirs["installers"][name]]
        if ours["manifest"] != theirs["manifest"]:
            differences.append("MANIFEST.json")
        report["differences"] = differences or ["launcher bytecode or version"]
    return report


def command_release_compare(ctx: ReleaseContext) -> None:
    if ctx.other is None:
        raise BuildError("compare needs --other DIR (another build of the same release)")
    report = compare_releases(ctx.out, ctx.other)
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["content_identical"]:
        raise BuildError(f"the builds differ: {', '.join(report['differences'])}")
    if not report["archives_identical"]:
        release_log("the contents are identical; the compressed archives differ (another zlib)")


def command_release_repro(ctx: ReleaseContext, args: argparse.Namespace) -> None:
    """Assemble twice in different directories from the same inputs and
    compare: the contents and the compressed archives must be identical."""

    refuse_unpublishable(ctx)
    scratch = ctx.scratch or ctx.cache / "repro-release"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-repro-", dir=scratch) as root:
        dist = ctx.gateway_dist
        if dist is None:
            ctx.work = Path(root) / "gateway-work"
            dist = gateway_outputs(ctx, ctx.targets())
        outs = []
        for label, relative in (("first", "a/w"), ("second", "second-build/nested/w")):
            again = copy.copy(args)
            again.command = None
            again.out = str(Path(root) / f"{label}-out")
            again.work = str(Path(root) / relative)
            again.gateway_dist = str(dist)
            again.no_reexec = True
            release_log(f"reproducibility build {label}")
            with contextlib.redirect_stdout(sys.stderr):
                run_release(ReleaseContext(again))
            outs.append(Path(again.out))
        report = compare_releases(outs[0], outs[1])
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["content_identical"] or not report["archives_identical"]:
        raise BuildError("the two builds differ")


def release_main(args: argparse.Namespace) -> None:
    command = args.command
    if command is not None and command not in RELEASE_COMMANDS:
        raise BuildError(f"unknown command {command!r}; commands: {', '.join(RELEASE_COMMANDS)} (or none to build)")
    ctx = ReleaseContext(args)
    if command == "compare":
        command_release_compare(ctx)
    elif command == "repro":
        command_release_repro(ctx, args)
    else:
        run_release(ctx)


# ------------------------------------------------------------------ entry


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="build.py", description=__doc__.split("\n\n", 1)[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="artifact", required=True)
    gateway = commands.add_parser("gateway", help="build the patched gateway",
                                  description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    gateway.add_argument("steps", nargs="*", metavar="STEP", help="steps or one command (see above)")
    gateway.add_argument("--upstream", default=str(DEFAULT_UPSTREAM), help="the recipe (gateway/UPSTREAM.json)")
    gateway.add_argument("--patches", default=str(DEFAULT_PATCH_DIR), help="directory of the admitted patches")
    gateway.add_argument("--patch", action="append", metavar="FILE",
                         help="apply exactly these patches, in order (diagnostic builds; repeatable)")
    gateway.add_argument("--omission-offsets", action="store_true",
                         help="apply: diagnostic omission builds only, with a --patch list that is the admitted "
                              "series with patches left out: a later patch may apply at a line offset when its "
                              "full context matches (rejects, reduced context and a moved hunk without context "
                              "still fail); BUILD.json names the moved hunks")
    gateway.add_argument("--cache", help=f"download and vendor cache (default ~/.cache/claude-multi-build, ${CACHE_ENV})")
    gateway.add_argument("--work", help="work directory (default CACHE/work/gateway)")
    gateway.add_argument("--dist", help="output directory (default dist/gateway in the repository)")
    gateway.add_argument("--inputs", help="pre-fetched inputs (never downloaded)")
    gateway.add_argument("--offline", action="store_true", help="never download (cache or --inputs only)")
    gateway.add_argument("--target", action="append", metavar="NAME",
                         help="a target name, host, shipped or all (repeatable)")
    gateway.add_argument("--gates", choices=GATE_SELECTIONS, default="all", help="which gates to run")
    gateway.add_argument("--other", help="compare: the binary built elsewhere")
    gateway.add_argument("--scratch", help="repro: where the two builds run (default CACHE/repro)")
    gateway.add_argument("--licenses", help="the licence notice tree (default gateway/licenses in the repository)")
    gateway.add_argument("--sbom", help="the per-target SBOM directory (default gateway/sbom in the repository)")
    gateway.add_argument("--out", help="contract: the file to write (default src/claude_multi/data/gateway-contract.json); "
                                       "registry: the directory (default src/claude_multi/data/registry)")
    for name, text in (("bundle", "assemble the release bundles of some targets"),
                       ("release", "build a release: every bundle, MANIFEST.json, SHA256SUMS and the installers")):
        sub = commands.add_parser(name, help=text, description=RELEASE_DOC,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
        if name == "release":
            sub.add_argument("command", nargs="?", metavar="COMMAND", help="compare or repro (default: build)")
        else:
            sub.set_defaults(command=None)
            sub.add_argument("--target", action="append", metavar="NAME",
                             help="a bundle target, host or all (repeatable; default host)")
        sub.add_argument("--out", help="output directory, empty or absent (default dist/release in the repository)")
        sub.add_argument("--gateway-dist", metavar="DIR", help="the dist directory of an earlier gateway build")
        sub.add_argument("--source-date-epoch", type=int, metavar="N", help="the source date (default $SOURCE_DATE_EPOCH, "
                                                                            "else the commit time of HEAD)")
        sub.add_argument("--python", metavar="PATH", help="compile bytecode with this interpreter (the pinned version)")
        sub.add_argument("--no-reexec", action="store_true", help="stay under the interpreter running this tool")
        sub.add_argument("--repo", help="the source tree (default the checkout holding this tool)")
        sub.add_argument("--product", help="the product description (default packaging/product.json)")
        sub.add_argument("--runtimes", help="the runtime pins (default packaging/python-runtimes.json)")
        sub.add_argument("--runtime-licenses", help="the runtimes' licence inventory and texts "
                                                    "(default packaging/licenses/python)")
        sub.add_argument("--upstream", help="the gateway recipe (default gateway/UPSTREAM.json)")
        sub.add_argument("--patches", help="directory of the admitted gateway patches")
        sub.add_argument("--licenses", help="the gateway notice tree (default gateway/licenses)")
        sub.add_argument("--sbom", help="the gateway SBOM directory (default gateway/sbom)")
        sub.add_argument("--cache", help=f"download cache (default ~/.cache/claude-multi-build, ${CACHE_ENV})")
        sub.add_argument("--work", help="work directory (default CACHE/work/release)")
        sub.add_argument("--inputs", help="pre-fetched gateway inputs (never downloaded)")
        sub.add_argument("--offline", action="store_true", help="never download (the cache and --inputs only)")
        if name == "release":
            sub.add_argument("--base-url", help=f"test only (with {TEST_BUILD_FLAG}): https location the "
                                                "installers download from ({version} is substituted) instead of "
                                                "packaging/product.json's")
            sub.add_argument(TEST_BUILD_FLAG, action="store_true",
                             help="test only, never a release: build without the release signing key, without "
                                  "the download locations of packaging/product.json, or with --base-url; the "
                                  "summary and MANIFEST.json record test_build: true")
            sub.add_argument("--other", help="compare: the other build's output directory")
            sub.add_argument("--scratch", help="repro: where the two builds run (default CACHE/repro-release)")
        else:
            sub.set_defaults(base_url=None, other=None, scratch=None, test_build=False)
    args = parser.parse_args(argv)
    args.raw_argv = list(argv)
    return args


def run_pipeline(args: argparse.Namespace) -> None:
    steps = list(args.steps) or list(DEFAULT_STEPS)
    unknown = [step for step in steps if step not in STEPS + COMMANDS]
    if unknown:
        raise BuildError(f"unknown step {unknown[0]!r}; steps: {' '.join(STEPS)}; commands: {' '.join(COMMANDS)}")
    if any(step in COMMANDS for step in steps):
        if len(steps) != 1:
            raise BuildError(f"{', '.join(COMMANDS)} run alone")
    elif len(set(steps)) != len(steps) or [step for step in STEPS if step in steps] != steps:
        raise BuildError(f"steps run once each, in this order: {' '.join(STEPS)}")
    ctx = Context(args)
    handlers = {"fetch": step_fetch, "vendor": step_vendor, "apply": step_apply, "build": step_build,
                "gates": step_gates, "inspect": step_inspect, "record": step_record}
    if steps == ["env"]:
        command_env(ctx)
    elif steps == ["compare"]:
        command_compare(ctx)
    elif steps == ["repro"]:
        command_repro(ctx)
    elif steps == ["notices"]:
        command_notices(ctx)
    elif steps == ["contract"]:
        command_contract(ctx)
    elif steps == ["registry"]:
        command_registry(ctx)
    else:
        for step in steps:
            handlers[step](ctx)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        if args.artifact == "gateway":
            run_pipeline(args)
        else:
            release_main(args)
    except BuildError as exc:
        print(f"build.py: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(f"build.py: the recipe or a state file lacks {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("build.py: cancelled", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
