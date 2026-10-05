#!/usr/bin/env python3
"""The pieces of a release bundle: layout, launchers, SBOMs, manifest, archive.

Standard library only (Python 3.11+); ``tools/build.py bundle|release``
drives it. A bundle is one directory, archived as
``claude-multi-<version>-<target>.tar.gz`` with that directory as its only
top-level entry::

  MANIFEST.json                     what the bundle is (versions, identities)
  bin/<launcher>                    sh launchers (claude-multi, claude-multi-proxy)
                                    that run the bundled Python
  lib/python3.X/site-packages/claude_multi/
                                    the launcher package with its resources in
                                    data/ and bytecode next to every module
  libexec/claude-multi/cli-proxy-api  the gateway
  runtime/python/                   the pruned CPython runtime (stdlib bytecode
                                    compiled at build time)
  share/claude-multi/               the user documents
  share/licenses/                   claude-multi, cli-proxy-api (with the Go
                                    toolchain and module notices), python
  share/sbom/                       CycloneDX: claude-multi, cli-proxy-api, python

The bundle root is an installation prefix like any installed Python package
(``lib/python3.X/site-packages`` plus ``bin`` and ``share/claude-multi``), so
the launcher finds its entry points and documents the same way everywhere.

Archives are deterministic: entries sorted, directories included, owner 0
with empty names, modes 0755 for directories and executables and 0644 for
other files, every mtime the source date, regular files always stored in
full (never as hard links); gzip with mtime 0 and no file name. A bundle's
identity is the sha256 of its uncompressed tar (``tar_sha256``), which does
not depend on the zlib that compressed it.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import stat
import tarfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Iterable

FORMAT = 1
NAME = "claude-multi"
MANIFEST_NAME = "MANIFEST.json"
RUNTIME_DIR = "runtime/python"
GATEWAY_PATH = "libexec/claude-multi/cli-proxy-api"
DOCS_DIR = "share/claude-multi"
LICENSES_DIR = "share/licenses"
SBOM_DIR = "share/sbom"
CONTENT_FORMAT = "bundle-content-v1"
_CHUNK = 1 << 20
_IGNORED_SOURCES = ("__pycache__",)
_IGNORED_SUFFIXES = (".pyc", ".pyo")


class BundleError(Exception):
    """A bundle input or step did not meet its contract."""


# --------------------------------------------------------------- names and paths


def bundle_name(version: str, target: str) -> str:
    return f"{NAME}-{version}-{target}"


def archive_name(version: str, target: str) -> str:
    return bundle_name(version, target) + ".tar.gz"


def python_minor(version: str) -> str:
    """``3.14`` for ``3.14.8``."""

    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise BundleError(f"not an exact CPython version: {version!r}")
    return f"{parts[0]}.{parts[1]}"


def site_dir(python_version: str) -> str:
    """Where the launcher package sits in a bundle (an installation prefix)."""

    return f"lib/python{python_minor(python_version)}/site-packages"


def stdlib_dir(python_version: str) -> str:
    """The bundled runtime's standard library, relative to the bundle root."""

    return f"{RUNTIME_DIR}/lib/python{python_minor(python_version)}"


# --------------------------------------------------------------- files


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ignored(name: str) -> bool:
    return name in _IGNORED_SOURCES or name.endswith(_IGNORED_SUFFIXES)


def copy_tree(source: Path, destination: Path) -> list[str]:
    """Copy a source tree of plain files (bytecode and caches left behind);
    links and special files are refused. Returns the copied files, sorted."""

    source = Path(source)
    if not source.is_dir() or source.is_symlink():
        raise BundleError(f"{source} is not a directory")
    copied: list[str] = []
    for directory, dirs, files in os.walk(source):
        here = Path(directory)
        dirs[:] = sorted(name for name in dirs if not _ignored(name))
        target_dir = destination / here.relative_to(source)
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in dirs:
            if (here / name).is_symlink():
                raise BundleError(f"{here / name} is a link; the bundle copies plain files only")
        for name in sorted(files):
            if _ignored(name):
                continue
            path = here / name
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raise BundleError(f"{path} is not a regular file")
            shutil.copyfile(path, target_dir / name)
            os.chmod(target_dir / name, 0o755 if info.st_mode & 0o111 else 0o644)
            copied.append((target_dir / name).relative_to(destination).as_posix())
    return sorted(copied)


def install_file(source: Path, destination: Path, *, executable: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o755 if executable else 0o644)


def write_file(destination: Path, data: bytes, *, executable: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    os.chmod(destination, 0o755 if executable else 0o644)


# --------------------------------------------------------------- launchers

# The launcher every bin/ entry carries; only the program name differs.
# It finds the bundle through its own path without resolving the
# directories above it: an install's `current` link stays in every path it
# exports, so the hook command compiled into a session keeps working after
# an update. -I keeps the caller's environment and directory out of the
# interpreter (PYTHONPATH, PYTHONHOME, user site), -B keeps it from writing
# bytecode into the installed tree (the bundle ships bytecode).
LAUNCHER_TEMPLATE = """#!/bin/sh
# {name}: a claude-multi release launcher (generated by tools/build.py).
# It runs the bundled Python with this installation's environment.
case $0 in
*/*) self=$0 ;;
*) self=$(command -v -- "$0") || {{ echo "{name}: cannot locate $0" >&2; exit 127; }} ;;
esac
while [ -L "$self" ]; do
	link=$(readlink "$self") || {{ echo "{name}: cannot read the link $self" >&2; exit 127; }}
	case $link in
	/*) self=$link ;;
	*) self=${{self%/*}}/$link ;;
	esac
done
root=$(CDPATH='' cd "${{self%/*}}/.." && pwd) || {{ echo "{name}: cannot find the installation of $self" >&2; exit 127; }}
site="$root/{site}"
CLAUDE_MULTI_CHANNEL=bundle
CLAUDE_MULTI_ASSETS="$site/claude_multi/data"
CLAUDE_MULTI_REGISTRY_DIR="$site/claude_multi/data/registry"
CLAUDE_MULTI_HOOK_COMMAND="$root/bin/claude-multi"
CLAUDE_MULTI_PROXY_BIN="$root/{gateway}"
export CLAUDE_MULTI_CHANNEL CLAUDE_MULTI_ASSETS CLAUDE_MULTI_REGISTRY_DIR CLAUDE_MULTI_HOOK_COMMAND CLAUDE_MULTI_PROXY_BIN
unset CLAUDE_MULTI_PROXY_PATCHES
if [ -d "$root/{runtime}/share/terminfo" ]; then
	TERMINFO_DIRS="${{TERMINFO_DIRS:-}}:$root/{runtime}/share/terminfo"
	export TERMINFO_DIRS
fi
exec "$root/{runtime}/bin/python3" -I -B -c '{bootstrap}' "$site" {name} "$@"
"""
# The interpreter's whole program: put the bundle's site directory first on
# sys.path (-I keeps every other source of paths away) and run the entry
# point, which reads the program name from the next argument.
BOOTSTRAP = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
             "from claude_multi.entrypoints import main; raise SystemExit(main())")


def launcher_text(name: str, python_version: str) -> bytes:
    if not name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in name):
        raise BundleError(f"not a launcher name: {name!r}")
    return LAUNCHER_TEMPLATE.format(name=name, site=site_dir(python_version), gateway=GATEWAY_PATH,
                                    runtime=RUNTIME_DIR, bootstrap=BOOTSTRAP).encode()


# --------------------------------------------------------------- SBOMs


def _serial(bom: dict) -> str:
    digest = hashlib.sha256(json.dumps(bom, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "claude-multi:bundle-sbom:" + digest))


def _render_bom(component: dict, components: list[dict], description: str) -> bytes:
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "tools": {"components": [{"type": "application", "group": NAME, "name": "build.py",
                                      "description": description}]},
            "component": component,
        },
        "components": components,
        "dependencies": [{"ref": component["bom-ref"], "dependsOn": [item["bom-ref"] for item in components]}],
    }
    bom = {"bomFormat": bom.pop("bomFormat"), "specVersion": bom.pop("specVersion"), "serialNumber": _serial(bom),
           **bom}
    return (json.dumps(bom, indent=2, ensure_ascii=False) + "\n").encode()


def python_sbom(*, version: str, release: str, target: str, triple: str, filename: str, url: str,
                sha256: str, pruned: Iterable[str], license_ids: Iterable[str] = ("Python-2.0", "CNRI-Python"),
                components: Iterable[dict] = (), removed: Iterable[str] = ()) -> bytes:
    """The bundled runtime: CPython from python-build-standalone, the pinned
    archive, the entries pruned from it (and the extension modules that
    removes), and the libraries linked into it (``components``, from the
    runtime licence inventory)."""

    purl = f"pkg:generic/cpython@{version}?build=python-build-standalone-{release}&target={triple}"
    component = {
        "type": "application", "bom-ref": purl, "name": "cpython", "version": version, "purl": purl,
        "description": f"CPython {version} (python-build-standalone {release}, {triple}, install_only_stripped)",
        "licenses": [{"license": {"id": item}} for item in license_ids],
        "externalReferences": [{"type": "distribution", "url": url,
                                "hashes": [{"alg": "SHA-256", "content": sha256}]}],
        "properties": [{"name": "claude-multi:runtime:target", "value": target},
                       {"name": "claude-multi:runtime:archive", "value": filename}]
                      + [{"name": "claude-multi:runtime:pruned", "value": entry} for entry in sorted(pruned)]
                      + [{"name": "claude-multi:runtime:removed-extension", "value": entry}
                         for entry in sorted(set(removed))],
    }
    return _render_bom(component, list(components), "tools/build.py bundle (the bundled Python runtime)")


def launcher_sbom(*, version: str, license_id: str = "MIT") -> bytes:
    """The launcher package: Python standard library only, no dependencies."""

    purl = f"pkg:generic/claude-multi@{version}"
    component = {"type": "application", "bom-ref": purl, "name": NAME, "version": version, "purl": purl,
                 "description": "the claude-multi launcher (Python standard library only)",
                 "licenses": [{"license": {"id": license_id}}]}
    return _render_bom(component, [], "tools/build.py bundle (the launcher package)")


# --------------------------------------------------------------- content and manifest


def _entries(root: Path) -> list[tuple[tuple[str, ...], str, Path]]:
    """Every path below ``root``: ``(parts, kind, path)``, kind dir/file/link,
    in tree order (a directory before its contents)."""

    found: list[tuple[tuple[str, ...], str, Path]] = []
    for directory, dirs, files in os.walk(root):
        here = Path(directory)
        links = [name for name in dirs if (here / name).is_symlink()]
        dirs[:] = [name for name in dirs if name not in links]
        for name in dirs:
            path = here / name
            found.append((path.relative_to(root).parts, "dir", path))
        for name in files + links:
            path = here / name
            relative = path.relative_to(root).as_posix()
            if "\n" in relative or "\0" in relative:
                raise BundleError(f"unsupported file name in {root}: {relative!r}")
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                kind = "link"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
            else:
                raise BundleError(f"{path} is neither a file, a directory nor a link")
            found.append((path.relative_to(root).parts, kind, path))
    return sorted(found, key=lambda entry: entry[0])


def _mode(kind: str, path: Path) -> int:
    if kind == "dir":
        return 0o755
    if kind == "link":
        return 0o777
    return 0o755 if os.lstat(path).st_mode & 0o111 else 0o644


def check_links(root: Path) -> None:
    """Every link stays inside ``root`` and names something that exists."""

    for parts, kind, path in _entries(root):
        if kind != "link":
            continue
        target = os.readlink(path)
        resolved = PurePosixPath(*parts[:-1], *PurePosixPath(target).parts)
        if target.startswith("/") or ".." in PurePosixPath(os.path.normpath(str(resolved))).parts:
            raise BundleError(f"the link {'/'.join(parts)} -> {target} leaves the bundle")
        if not path.exists():
            raise BundleError(f"the link {'/'.join(parts)} -> {target} is dangling")


def content_digest(root: Path, *, exclude: Iterable[str] = ()) -> tuple[str, int]:
    """``(sha256, files)`` over the tree as the archive stores it: every path
    with its kind, normalised mode and content hash (or link target)."""

    skipped = set(exclude)
    total = hashlib.sha256(CONTENT_FORMAT.encode() + b"\n")
    files = 0
    for parts, kind, path in _entries(root):
        relative = "/".join(parts)
        if relative in skipped:
            continue
        if kind == "file":
            files += 1
            line = f"F {_mode(kind, path):04o} {sha256_file(path)} {relative}\n"
        elif kind == "link":
            line = f"L {os.readlink(path)} {relative}\n"
        else:
            line = f"D {relative}\n"
        total.update(line.encode())
    return total.hexdigest(), files


def manifest_bytes(document: dict) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


# --------------------------------------------------------------- archive


def write_archive(stage: Path, top: str, destination: Path, epoch: int) -> dict[str, object]:
    """Archive ``stage/top`` as ``destination`` (a .tar.gz); returns its
    ``sha256``, ``size`` and the ``tar_sha256`` of the uncompressed tar."""

    root = Path(stage) / top
    if not root.is_dir():
        raise BundleError(f"nothing staged at {root}")
    if epoch < 0:
        raise BundleError("the source date must not be negative")
    destination.parent.mkdir(parents=True, exist_ok=True)
    raw = destination.with_name(destination.name + ".tar.partial")
    partial = destination.with_name(destination.name + ".partial")
    try:
        with open(raw, "wb") as handle:
            with tarfile.open(fileobj=handle, mode="w", format=tarfile.PAX_FORMAT) as tar:
                tar.addfile(_info(top, "dir", root, epoch))
                for parts, kind, path in _entries(root):
                    info = _info("/".join((top, *parts)), kind, path, epoch)
                    if kind == "file":
                        with open(path, "rb") as data:
                            tar.addfile(info, data)
                    else:
                        tar.addfile(info)
        tar_sha256 = sha256_file(raw)
        with open(raw, "rb") as source, open(partial, "wb") as out:
            with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as compressed:
                for chunk in iter(lambda: source.read(_CHUNK), b""):
                    compressed.write(chunk)
        os.replace(partial, destination)
    finally:
        for leftover in (raw, partial):
            try:
                os.unlink(leftover)
            except FileNotFoundError:
                pass
    return {"sha256": sha256_file(destination), "size": destination.stat().st_size, "tar_sha256": tar_sha256}


def _info(name: str, kind: str, path: Path, epoch: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = epoch
    info.mode = _mode(kind, path)
    if kind == "dir":
        info.type = tarfile.DIRTYPE
    elif kind == "link":
        info.type = tarfile.SYMTYPE
        info.linkname = os.readlink(path)
    else:
        info.type = tarfile.REGTYPE
        info.size = os.lstat(path).st_size
    return info


def tar_sha256(archive: Path) -> str:
    """The sha256 of a .tar.gz's uncompressed tar."""

    digest = hashlib.sha256()
    with gzip.open(archive, "rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
