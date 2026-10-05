#!/usr/bin/env python3
"""Refuse Claude Code bytes in release artifacts and CI caches.

claude-multi never redistributes Claude Code: users acquire the pinned
version themselves. This guard scans files and the members of ``.tar``,
``.tar.gz``/``.tgz`` and ``.zip`` archives under the given paths and fails
when it finds

- an entry named ``claude`` or ``claude.exe`` (a Claude Code executable),
- content whose sha256 equals a pinned Claude Code hash (every ``sha256``
  value in ``native-contract.json``), or
- a single file larger than ``--max-size`` (default 150 MiB; claude-multi's
  own files stay far below it, a Claude Code executable does not).

  python3 tools/_build/no_bundle_guard.py [--contract FILE] PATH ...

The default contract is the checkout's packaged native contract. Exit 0:
clean. Exit 1: findings (one per line). Exit 2: usage error, or a contract
that cannot be read or pins no hash (the guard never runs without them).
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import IO, Iterator

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = REPO / "src" / "claude_multi" / "data" / "catalog" / "native-contract.json"
FORBIDDEN_NAMES = frozenset({"claude", "claude.exe"})
DEFAULT_MAX_SIZE = 150 * 1024 * 1024
_CHUNK = 1 << 20
_HEX64 = re.compile(r"[0-9a-f]{64}")


def pinned_hashes(contract: Path) -> frozenset[str]:
    """Every ``"sha256": "<hex>"`` value anywhere in the contract document."""

    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "sha256" and isinstance(value, str) and _HEX64.fullmatch(value):
                    found.add(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(contract.read_text(encoding="utf-8")))
    return frozenset(found)


def _digest(stream: IO[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(_CHUNK), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _is_archive(name: str) -> bool:
    return name.endswith((".tar", ".tar.gz", ".tgz", ".zip"))


def _members(path: Path) -> Iterator[tuple[str, int, IO[bytes] | None]]:
    """``(name, size, stream)`` per archive member; stream None for non-files."""

    if path.name.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    yield info.filename, 0, None
                    continue
                with archive.open(info) as stream:
                    yield info.filename, info.file_size, stream
        return
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            stream = archive.extractfile(member) if member.isfile() else None
            yield member.name, member.size if member.isfile() else 0, stream


def scan(paths: list[Path], hashes: frozenset[str], max_size: int) -> list[str]:
    findings: list[str] = []

    def check(label: str, name: str, size: int, stream: IO[bytes] | None) -> None:
        if os.path.basename(name.rstrip("/")) in FORBIDDEN_NAMES and stream is not None:
            findings.append(f"{label}: named like a Claude Code executable")
        if size > max_size:
            findings.append(f"{label}: {size} bytes is larger than {max_size}")
        if stream is not None and hashes and _digest(stream) in hashes:
            findings.append(f"{label}: matches a pinned Claude Code sha256")

    for root in paths:
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
        for path in files:
            with open(path, "rb") as stream:
                check(str(path), path.name, path.stat().st_size, stream)
            if _is_archive(path.name):
                try:
                    for name, size, member in _members(path):
                        check(f"{path}!{name}", name, size, member)
                except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
                    findings.append(f"{path}: unreadable archive ({exc})")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="no_bundle_guard.py", description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--max-size", type=int, default=DEFAULT_MAX_SIZE)
    args = parser.parse_args(argv)
    missing = [str(p) for p in args.paths if not p.exists()]
    if missing:
        print(f"no_bundle_guard: no such path: {', '.join(missing)}", file=sys.stderr)
        return 2
    try:
        hashes = pinned_hashes(args.contract)
    except (OSError, ValueError) as exc:
        print(f"no_bundle_guard: cannot read {args.contract}: {exc}", file=sys.stderr)
        return 2
    if not hashes:
        print(f"no_bundle_guard: {args.contract} pins no Claude Code sha256", file=sys.stderr)
        return 2
    findings = scan(args.paths, hashes, args.max_size)
    for finding in findings:
        print(finding)
    if findings:
        print(f"no_bundle_guard: {len(findings)} finding(s); Claude Code must never be redistributed", file=sys.stderr)
        return 1
    print(f"no_bundle_guard: clean ({len(hashes)} pinned hashes checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
