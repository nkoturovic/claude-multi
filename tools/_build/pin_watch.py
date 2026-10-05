#!/usr/bin/env python3
"""The pinned build inputs, read strictly, compared with upstream versions.

  python3 tools/_build/pin_watch.py pins [--repo DIR]
  python3 tools/_build/pin_watch.py compare --upstream FILE [--repo DIR]

``pins`` prints the pins as JSON: Claude Code (the packaged native
contract's verified version), the gateway (``gateway/UPSTREAM.json``
``upstream.version``), Go (its ``toolchain.version``), CPython and the
python-build-standalone release (``packaging/python-runtimes.json``). A pin
that is absent, empty or not a version refuses (exit 1): a watch that cannot
read its pins must not look like a quiet week.

``compare`` reads ``name=value`` lines of upstream versions (as the weekly
workflow collects them) and prints a Markdown table of the inputs whose
upstream is newer; an upstream value that is missing or malformed is a
failure too. Exit 0 with no output: nothing newer. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

REPO = Path(__file__).resolve().parents[2]
NAMES = ("claude_code", "gateway", "go", "python", "python_build_standalone")
_VERSION = re.compile(r"[0-9]+(\.[0-9]+){1,2}")
_RELEASE = re.compile(r"[0-9]{8}")


class PinError(Exception):
    pass


def _load(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PinError(f"{path} cannot be read ({exc})") from exc


def _field(document: object, keys: Sequence[str], where: str) -> object:
    node = document
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            raise PinError(f"{where}: {'.'.join(keys)} is missing")
        node = node[key]
    return node


def _version(value: object, what: str, pattern: re.Pattern[str] = _VERSION) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PinError(f"{what} is not a version: {value!r}")
    return value


def pins(repo: Path = REPO) -> dict[str, str]:
    contract_path = repo / "src/claude_multi/data/catalog/native-contract.json"
    contract = _load(contract_path)
    verified = _field(contract, ("verified",), str(contract_path))
    if not isinstance(verified, list) or not verified or not isinstance(verified[0], dict):
        raise PinError(f"{contract_path}: verified names no Claude Code version")
    upstream_path = repo / "gateway/UPSTREAM.json"
    upstream = _load(upstream_path)
    runtimes_path = repo / "packaging/python-runtimes.json"
    runtimes = _load(runtimes_path)
    return {
        "claude_code": _version(verified[0].get("version"), "the Claude Code pin"),
        "gateway": _version(_field(upstream, ("upstream", "version"), str(upstream_path)), "the gateway pin"),
        "go": _version(_field(upstream, ("toolchain", "version"), str(upstream_path)), "the Go pin"),
        "python": _version(_field(runtimes, ("python",), str(runtimes_path)), "the CPython pin"),
        "python_build_standalone": _version(_field(runtimes, ("release",), str(runtimes_path)),
                                            "the python-build-standalone release", _RELEASE),
    }


def _key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def normalize(name: str, raw: str) -> str:
    value = raw.strip()
    if name in ("gateway", "claude_code"):
        value = value.removeprefix("v")
    if name == "go":
        value = value.removeprefix("go")
    pattern = _RELEASE if name == "python_build_standalone" else _VERSION
    return _version(value, f"the upstream {name} version", pattern)


def compare(repo: Path, upstream_text: str) -> list[tuple[str, str, str]]:
    pinned = pins(repo)
    upstream = dict(line.split("=", 1) for line in upstream_text.splitlines() if "=" in line)
    newer = []
    for name in NAMES:
        if name == "python":
            continue  # follows the python-build-standalone release
        if name not in upstream or not upstream[name].strip():
            raise PinError(f"the upstream {name} version is missing (the lookup failed)")
        latest = normalize(name, upstream[name])
        if _key(latest) > _key(pinned[name]):
            newer.append((name, pinned[name], latest))
    return newer


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pin_watch.py", description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("command", choices=("pins", "compare"))
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--upstream", type=Path, help="compare: the name=value lines")
    args = parser.parse_args(argv)
    try:
        if args.command == "pins":
            print(json.dumps(pins(args.repo), indent=2, sort_keys=True))
            return 0
        if args.upstream is None:
            parser.error("compare needs --upstream FILE")
        rows = compare(args.repo, args.upstream.read_text(encoding="utf-8"))
    except (PinError, OSError) as exc:
        print(f"pin-watch: {exc}", file=sys.stderr)
        return 1
    if rows:
        print("Newer upstream versions than the pins (re-pin through the documented release steps):\n")
        print("| input | pinned | upstream |\n|---|---|---|")
        for name, pinned, latest in rows:
            print(f"| {name} | {pinned} | {latest} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
