#!/usr/bin/env python3
"""The bundled Python runtime: pinned python-build-standalone archives.

Standard library only (Python 3.11+). ``packaging/python-runtimes.json`` pins
one CPython release per bundle target by file name, size and sha256. This
module

- fetches an archive and accepts it only when size and sha256 match (a cache
  hit is re-hashed; an offline run reads only the cache),
- extracts it with every member checked (inside ``python/``, no absolute or
  escaping links, no device files) and never rewrites a byte,
- prunes the runtime by deleting the entries the manifest's fixed list names
  (delete-only: nothing is added, patched or stripped), and
- compiles a source tree to ``.pyc`` with the pinned interpreter native to the
  build host: ``--invalidation-mode unchecked-hash``, source file names given
  by the in-bundle path (``-d``), ``SOURCE_DATE_EPOCH`` set, so every target
  can ship the same bytecode set; ``digest`` prints the identity CI compares
  across builders. In a bundle the launcher package sits in
  ``lib/python3.X/site-packages`` and the runtime's standard library in
  ``runtime/python/lib/python3.X``; those are the ``--ddir`` values, and
- refuses a runtime or bytecode tree that names a concrete ``/nix/store``
  path (bundles must not depend on the build host).

Command line (each prints one JSON document)::

  python_runtime.py fetch --target linux-x86_64 [--cache DIR] [--offline]
  python_runtime.py prepare --target T --dest DIR [--cache DIR] [--offline]
  python_runtime.py host-interpreter --work DIR [--cache DIR] [--offline]
  python_runtime.py compile --python PY --tree DIR --ddir lib/python3.14/site-packages --epoch N
  python_runtime.py digest --tree DIR
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, BinaryIO

MANIFEST = Path(__file__).resolve().parents[2] / "packaging" / "python-runtimes.json"
TRIPLES = {
    "linux-x86_64": "x86_64-unknown-linux-gnu",
    "linux-aarch64": "aarch64-unknown-linux-gnu",
    "darwin-arm64": "aarch64-apple-darwin",
    "darwin-x86_64": "x86_64-apple-darwin",
}
FLAVOR = "install_only_stripped"
ROOT_DIR = "python"
INTERPRETER = "bin/python3"
_CHUNK = 1 << 20
_HEX64 = re.compile(r"[0-9a-f]{64}")
# A concrete store path: the prefix, 32 characters of Nix's base-32 alphabet, a dash.
_STORE_PATH = re.compile(rb"/nix/store/[0-9a-df-np-sv-z]{32}-")

Opener = Callable[[str], BinaryIO]


class BuildError(Exception):
    """A pinned input or a build step did not meet its contract."""


@dataclass(frozen=True)
class Runtime:
    target: str
    triple: str
    filename: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True)
class Runtimes:
    release: str
    python: str
    targets: dict[str, Runtime]
    prune: tuple[str, ...]

    def runtime(self, target: str) -> Runtime:
        try:
            return self.targets[target]
        except KeyError:
            raise BuildError(f"no pinned runtime for target {target!r}") from None


# --------------------------------------------------------------- manifest


def _check_pattern(pattern: object) -> str:
    if not isinstance(pattern, str) or not pattern:
        raise BuildError(f"prune entry {pattern!r} is not a path pattern")
    segments = pattern.split("/")
    if (pattern.startswith("/") or "\\" in pattern or any(s in ("", ".", "..") for s in segments)):
        raise BuildError(f"prune entry {pattern!r} must be a relative path without '..'")
    return pattern


def load(path: Path | str = MANIFEST) -> Runtimes:
    """Read and validate the runtime manifest (closed shape, consistent names)."""

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"cannot read {path}: {exc}") from exc
    expected_keys = {"format", "source", "release", "python", "flavor", "url", "targets", "prune"}
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise BuildError(f"{path}: expected exactly the keys {sorted(expected_keys)}")
    if document["format"] != 1 or document["source"] != "python-build-standalone":
        raise BuildError(f"{path}: unsupported format or source")
    release, version, template = document["release"], document["python"], document["url"]
    if not (isinstance(release, str) and re.fullmatch(r"[0-9]{8}", release)):
        raise BuildError(f"{path}: release must be the YYYYMMDD release tag")
    if not (isinstance(version, str) and re.fullmatch(r"3\.[0-9]+\.[0-9]+", version)):
        raise BuildError(f"{path}: python must be an exact CPython version")
    if document["flavor"] != FLAVOR:
        raise BuildError(f"{path}: flavor must be {FLAVOR}")
    if not (isinstance(template, str) and template.startswith("https://")
            and "{release}" in template and "{filename}" in template):
        raise BuildError(f"{path}: url must be an https template with {{release}} and {{filename}}")
    targets = document["targets"]
    if not isinstance(targets, dict) or set(targets) != set(TRIPLES):
        raise BuildError(f"{path}: targets must be exactly {sorted(TRIPLES)}")
    runtimes: dict[str, Runtime] = {}
    for target, entry in sorted(targets.items()):
        if not isinstance(entry, dict) or set(entry) != {"triple", "filename", "size", "sha256"}:
            raise BuildError(f"{path}: target {target} needs triple, filename, size and sha256")
        triple = TRIPLES[target]
        filename = f"cpython-{version}+{release}-{triple}-{FLAVOR}.tar.gz"
        if entry["triple"] != triple or entry["filename"] != filename:
            raise BuildError(f"{path}: target {target} must name {triple} and {filename}")
        size, digest = entry["size"], entry["sha256"]
        if not (isinstance(size, int) and not isinstance(size, bool) and size > 0):
            raise BuildError(f"{path}: target {target} size must be a positive integer")
        if not (isinstance(digest, str) and _HEX64.fullmatch(digest)):
            raise BuildError(f"{path}: target {target} sha256 must be 64 lower-case hex digits")
        url = template.format(release=release, filename=urllib.parse.quote(filename, safe=""))
        runtimes[target] = Runtime(target, triple, filename, size, digest, url)
    prune = document["prune"]
    if not isinstance(prune, list):
        raise BuildError(f"{path}: prune must be a list")
    patterns = tuple(_check_pattern(p) for p in prune)
    if list(patterns) != sorted(set(patterns)):
        raise BuildError(f"{path}: prune entries must be sorted and unique")
    return Runtimes(release=release, python=version, targets=runtimes, prune=patterns)


def host_target(system: str | None = None, machine: str | None = None) -> str:
    """The bundle target of the build host (the PBS interpreter it can run)."""

    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine)
    if system == "linux" and arch:
        return f"linux-{arch}"
    if system == "darwin" and arch:
        return "darwin-arm64" if arch == "aarch64" else "darwin-x86_64"
    raise BuildError(f"no pinned runtime runs on {system}/{machine}")


def default_cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "claude-multi-build" / "python-runtimes"


# --------------------------------------------------------------- fetch


def file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def verify_archive(path: Path, runtime: Runtime) -> None:
    digest, size = file_digest(path)
    if size != runtime.size or digest != runtime.sha256:
        raise BuildError(
            f"{runtime.filename}: got {size} bytes sha256 {digest}, "
            f"pinned {runtime.size} bytes sha256 {runtime.sha256}"
        )


def _https_opener(url: str) -> BinaryIO:
    if not url.startswith("https://"):
        raise BuildError(f"refusing a non-https download: {url}")
    return urllib.request.urlopen(url, timeout=60)  # noqa: S310 - https only


def fetch(runtime: Runtime, cache: Path | None = None, *, offline: bool = False,
          opener: Opener | None = None) -> Path:
    """The verified archive in ``cache/<sha256>/<filename>``, downloaded if needed."""

    cache = Path(cache) if cache is not None else default_cache()
    target = cache / runtime.sha256 / runtime.filename
    if target.is_file() and not target.is_symlink():
        verify_archive(target, runtime)
        return target
    if offline:
        raise BuildError(f"{runtime.filename} is not in the cache {cache} (offline)")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, partial = tempfile.mkstemp(dir=target.parent, prefix=f".{runtime.filename}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out, (opener or _https_opener)(runtime.url) as source:
            received = 0
            for chunk in iter(lambda: source.read(_CHUNK), b""):
                received += len(chunk)
                if received > runtime.size:
                    raise BuildError(f"{runtime.filename}: download exceeds the pinned {runtime.size} bytes")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        verify_archive(Path(partial), runtime)
        os.replace(partial, target)
    except BaseException:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise
    return target


# --------------------------------------------------------------- extract


def _inside(name: str) -> bool:
    return name == ROOT_DIR or name.startswith(ROOT_DIR + "/")


def _check_member(member: tarfile.TarInfo, seen: set[str]) -> None:
    name = member.name
    normal = posixpath.normpath(name)
    if (name.startswith("/") or normal != name.rstrip("/") or normal.startswith("..")
            or not _inside(normal)):
        raise BuildError(f"archive member {name!r} is outside {ROOT_DIR}/")
    if member.issym():
        link = member.linkname
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(normal), link))
        if link.startswith("/") or not _inside(resolved):
            raise BuildError(f"archive symlink {name!r} -> {link!r} escapes {ROOT_DIR}/")
    elif member.islnk():
        if posixpath.normpath(member.linkname) not in seen:
            raise BuildError(f"archive hard link {name!r} names {member.linkname!r}, not an earlier member")
    elif not (member.isfile() or member.isdir()):
        raise BuildError(f"archive member {name!r} is not a file, directory or link")
    seen.add(normal)


def extract(archive: Path, dest: Path) -> Path:
    """Extract a verified archive into the empty ``dest``; returns ``dest/python``."""

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise BuildError(f"{dest} is not empty")
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        seen: set[str] = set()
        for member in members:
            _check_member(member, seen)
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, members=members, filter="data")
        else:  # pragma: no cover - Python 3.11.0-3.11.3 without extraction filters
            tar.extractall(dest, members=members)
    root = dest / ROOT_DIR
    if not (root / INTERPRETER).exists():
        raise BuildError(f"{archive.name} has no {ROOT_DIR}/{INTERPRETER}")
    return root


# --------------------------------------------------------------- prune


def _matches(root: Path, pattern: str) -> list[Path]:
    current = [root]
    segments = pattern.split("/")
    for depth, segment in enumerate(segments):
        last = depth == len(segments) - 1
        found: list[Path] = []
        for directory in current:
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name)
            except (FileNotFoundError, NotADirectoryError):
                continue
            for entry in entries:
                if not fnmatch.fnmatchcase(entry.name, segment):
                    continue
                if last or entry.is_dir(follow_symlinks=False):
                    found.append(Path(entry.path))
        current = found
    return current


def prune(root: Path, patterns: tuple[str, ...] | list[str]) -> list[str]:
    """Delete the entries the patterns name (delete-only); returns them, sorted.

    Patterns are ``/``-separated per-segment globs relative to ``root``;
    intermediate segments never follow symlinks. A pattern matching nothing
    is fine (an optional component that this build lacks).
    """

    root = Path(root)
    removed: list[str] = []
    for pattern in patterns:
        for path in _matches(root, _check_pattern(pattern)):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                shutil.rmtree(path)
            removed.append(relative)
    return sorted(removed)


def dangling_links(root: Path) -> list[str]:
    """Symlinks under ``root`` whose target no longer exists (a prune gap)."""

    found = []
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() and not path.exists():
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


def store_references(root: Path) -> list[str]:
    """Files under ``root`` whose bytes name a concrete ``/nix/store`` path."""

    found = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        tail = b""
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                if _STORE_PATH.search(tail + chunk):
                    found.append(path.relative_to(root).as_posix())
                    break
                tail = chunk[-64:]
    return found


def prepare(runtimes: Runtimes, target: str, dest: Path, *, cache: Path | None = None,
            offline: bool = False, opener: Opener | None = None) -> dict[str, object]:
    """Fetch, extract and prune one target's runtime into ``dest/python``."""

    runtime = runtimes.runtime(target)
    archive = fetch(runtime, cache, offline=offline, opener=opener)
    root = extract(archive, dest)
    removed = prune(root, runtimes.prune)
    dangling = dangling_links(root)
    if dangling:
        raise BuildError(f"pruning left dangling links: {', '.join(dangling)}")
    stored = store_references(root)
    if stored:
        raise BuildError(f"the runtime names /nix/store paths: {', '.join(stored[:5])}")
    return {"target": target, "python": runtimes.python, "release": runtimes.release,
            "archive": str(archive), "sha256": runtime.sha256, "root": str(root), "removed": removed}


# --------------------------------------------------------------- bytecode


def interpreter_version(python: Path | str) -> str:
    result = subprocess.run(
        [str(python), "-I", "-c", "import platform; print(platform.python_version())"],
        capture_output=True, text=True, timeout=60, env=_clean_env(), stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise BuildError(f"{python} does not run: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def host_interpreter(runtimes: Runtimes, work: Path, *, cache: Path | None = None,
                     offline: bool = False, opener: Opener | None = None) -> Path:
    """The pinned interpreter for this build host, extracted under ``work``."""

    target = host_target()
    runtime = runtimes.runtime(target)
    dest = Path(work) / f"host-{runtime.sha256[:16]}"
    python = dest / ROOT_DIR / INTERPRETER
    if not python.exists():
        archive = fetch(runtime, cache, offline=offline, opener=opener)
        staging = Path(tempfile.mkdtemp(dir=Path(work), prefix=".host-"))
        try:
            extract(archive, staging)
            if dest.exists():
                shutil.rmtree(dest)
            os.replace(staging, dest)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    version = interpreter_version(python)
    if version != runtimes.python:
        raise BuildError(f"{python} is CPython {version}, the bundles pin {runtimes.python}")
    return python


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", "TZ": "UTC"}
    if sys.platform == "win32" and "SYSTEMROOT" in os.environ:  # pragma: no cover
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    env.update(extra or {})
    return env


def _cache_tag(python: Path | str) -> str:
    result = subprocess.run(
        [str(python), "-I", "-c", "import sys; print(sys.implementation.cache_tag)"],
        capture_output=True, text=True, timeout=60, env=_clean_env(), stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise BuildError(f"{python} does not report a bytecode cache tag")
    return result.stdout.strip()


def compile_tree(python: Path | str, tree: Path, ddir: str, epoch: int, *,
                 expected_version: str | None = None) -> list[str]:
    """Compile every ``.py`` under ``tree``; returns the ``.pyc`` paths (sorted).

    Existing ``__pycache__`` directories are removed first so the set is
    exactly the sources' bytecode. Every ``.pyc`` must be an unchecked-hash
    file of the interpreter's cache tag.
    """

    tree = Path(tree)
    if ddir.startswith("/") or ".." in ddir.split("/"):
        raise BuildError(f"the in-bundle path {ddir!r} must be relative")
    if expected_version is not None:
        version = interpreter_version(python)
        if version != expected_version:
            raise BuildError(f"{python} is CPython {version}, the bundles pin {expected_version}")
    for cache_dir in sorted(tree.rglob("__pycache__"), reverse=True):
        if cache_dir.is_dir() and not cache_dir.is_symlink():
            shutil.rmtree(cache_dir)
    command = [str(python), "-I", "-m", "compileall", "-q", "-f",
               "--invalidation-mode", "unchecked-hash", "-d", ddir, str(tree)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL,
                            env=_clean_env({"SOURCE_DATE_EPOCH": str(int(epoch))}))
    if result.returncode != 0:
        raise BuildError(f"compileall failed: {(result.stdout + result.stderr).strip()[:400]}")
    tag = _cache_tag(python)
    compiled: list[str] = []
    for source in sorted(tree.rglob("*.py")):
        pyc = source.parent / "__pycache__" / f"{source.stem}.{tag}.pyc"
        header = pyc.read_bytes()[:16] if pyc.is_file() else b""
        if len(header) < 16 or int.from_bytes(header[4:8], "little") != 0b01:
            raise BuildError(f"{source.relative_to(tree)} has no unchecked-hash bytecode")
        compiled.append(pyc.relative_to(tree).as_posix())
    stored = [name for name in store_references(tree) if name.endswith(".pyc")]
    if stored:
        raise BuildError(f"bytecode names /nix/store paths: {', '.join(stored[:5])}")
    return compiled


def pyc_digest(tree: Path) -> str:
    """sha256 over every ``.pyc`` under ``tree`` (sorted relative path + bytes)."""

    tree = Path(tree)
    digest = hashlib.sha256()
    for path in sorted(tree.rglob("*.pyc"), key=lambda p: p.relative_to(tree).as_posix()):
        relative = path.relative_to(tree).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big") + relative)
        digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


# --------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python_runtime.py", description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch", "prepare", "host-interpreter"):
        command = sub.add_parser(name)
        command.add_argument("--cache", type=Path, default=None)
        command.add_argument("--offline", action="store_true")
        if name != "host-interpreter":
            command.add_argument("--target", required=True, choices=sorted(TRIPLES))
        if name == "prepare":
            command.add_argument("--dest", type=Path, required=True)
        if name == "host-interpreter":
            command.add_argument("--work", type=Path, required=True)
    compile_cmd = sub.add_parser("compile")
    compile_cmd.add_argument("--python", required=True)
    compile_cmd.add_argument("--tree", type=Path, required=True)
    compile_cmd.add_argument("--ddir", required=True)
    compile_cmd.add_argument("--epoch", type=int, default=None)
    compile_cmd.add_argument("--expect-version", default=None)
    digest_cmd = sub.add_parser("digest")
    digest_cmd.add_argument("--tree", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "digest":
            result: dict[str, object] = {"pyc_sha256": pyc_digest(args.tree)}
        elif args.command == "compile":
            epoch = args.epoch if args.epoch is not None else int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
            files = compile_tree(args.python, args.tree, args.ddir, epoch, expected_version=args.expect_version)
            result = {"compiled": len(files), "pyc_sha256": pyc_digest(args.tree)}
        else:
            runtimes = load(args.manifest)
            if args.command == "fetch":
                path = fetch(runtimes.runtime(args.target), args.cache, offline=args.offline)
                result = {"target": args.target, "archive": str(path)}
            elif args.command == "prepare":
                result = prepare(runtimes, args.target, args.dest, cache=args.cache, offline=args.offline)
            else:
                args.work.mkdir(parents=True, exist_ok=True)
                python = host_interpreter(runtimes, args.work, cache=args.cache, offline=args.offline)
                result = {"python": str(python), "version": runtimes.python}
    except BuildError as exc:
        print(f"python_runtime: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
