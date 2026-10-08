"""Second-launcher Python code locations only; no stack values or raw traceback.

The selected public sources are verified against the unchanged release archive.
The daemon disappears on exec; missing captures have no inferred explanation.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tarfile
import threading
import time

MODE = "hosted-disposable-wsl-fixture-only"
SITE = "lib/python3.14/site-packages"
RUNTIME = "runtime/python/lib/python3.14"
PACKAGE_FILES = ("entrypoints.py", "cli/entry.py", "cli/streams.py", "cli/launch_flow.py", "cli/runtime.py",
                 "cli/selection.py", "cli/resume_checks.py", "launch.py", "sessions.py")
RUNTIME_FILES = ("threading.py", "contextlib.py", "selectors.py", "subprocess.py", "socket.py", "queue.py", "io.py", "os.py")
SOURCES = {**{f"{SITE}/claude_multi/{name}": f"package/claude_multi/{name}" for name in PACKAGE_FILES},
           **{f"{RUNTIME}/{name}": f"runtime/{name}" for name in RUNTIME_FILES}}
DIAG_SOURCE = "diagnostic/resume_locations.py"
SOURCE_IDS = frozenset((*SOURCES.values(), DIAG_SOURCE, "other"))
SPECIAL_NAMES = frozenset(("<module>", "<lambda>", "<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>"))
FUNCTION = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z", re.ASCII)
MAX_THREADS, MAX_DEPTH, MAX_CAPTURE_BYTES, MAX_TOTAL_BYTES = 8, 32, 32768, 65536
ALLOWLIST = "resume-location-allowlist.json"
EVIDENCE = "resume-locations.raw.jsonl"
WRAPPER = "claude-multi-resume-diag"
REQUEST = "location-request.json"
ROOT_ANCHOR = '''root=$(CDPATH='' cd "${self%/*}/.." && pwd) || { echo "claude-multi: cannot find the installation of $self" >&2; exit 127; }'''
BOOTSTRAP = 'import sys; sys.path.insert(0, sys.argv.pop(1)); from claude_multi.entrypoints import main; raise SystemExit(main())'


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("code-location evidence rejected")


def pairs(items: list) -> dict:
    result = {}
    for key, value in items:
        require(key not in result)
        result[key] = value
    return result


def parse(raw: bytes) -> dict:
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: require(False))


def encoded(row: dict) -> bytes:
    return (json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")


def read_regular(path: Path, limit: int, *, private: bool = False) -> bytes:
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_size <= limit)
    if private:
        require(stat.S_IMODE(before.st_mode) == 0o600 and before.st_uid == os.geteuid() and before.st_nlink == 1)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        after = os.fstat(handle.fileno())
        require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino) and stat.S_ISREG(after.st_mode))
        raw = handle.read(limit + 1)
    require(len(raw) <= limit)
    return raw


def open_evidence(path: Path) -> int:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def source_info(raw: bytes, source: str) -> dict:
    tree = ast.parse(raw)
    functions = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and FUNCTION.fullmatch(node.name)} | set(SPECIAL_NAMES)
    return {"source": source, "functions": functions, "lines": min(len(raw.splitlines()), 100000)}


def load_registry(install: Path, allowlist: Path) -> dict:
    document = parse(read_regular(allowlist, 32768, private=True))
    require(set(document) == {"schema", "root", "files", "diagnostic_sha256"} and document["schema"] == 1)
    require(document["root"] == str(install) and set(document["files"]) == set(SOURCES))
    registry = {}
    for relative, source in SOURCES.items():
        raw = read_regular(install / relative, 2 * 1024 * 1024)
        require(hashlib.sha256(raw).hexdigest() == document["files"][relative])
        info = source_info(raw, source)
        for alias in (relative, str(install / relative), str((install / relative).resolve())):
            registry[alias] = info
    diagnostic = Path(__file__)
    raw = read_regular(diagnostic, 128 * 1024)
    require(hashlib.sha256(raw).hexdigest() == document["diagnostic_sha256"])
    registry[str(diagnostic)] = source_info(raw, DIAG_SOURCE)
    return registry


def safe_location(frame, registry: dict) -> dict:
    code = frame.f_code
    info = registry.get(code.co_filename)
    if info is None:
        return {"source": "other", "function": "other", "line": 0}
    name = code.co_name
    if name == "other" or name not in info["functions"]:
        return {"source": info["source"], "function": "other", "line": 0}
    number = frame.f_lineno
    return {"source": info["source"], "function": name,
            "line": number if type(number) is int and 1 <= number <= info["lines"] else 0}


def capture(capture_id: int, registry: dict, frames: dict, main_ident: int | None) -> dict:
    identities = ([main_ident] if main_ident in frames else []) + sorted(key for key in frames if key != main_ident)
    row = {"schema": 1, "mode": MODE, "capture": capture_id, "threads": [],
           "threads_capped": len(identities) > MAX_THREADS, "bytes_capped": False}
    for ordinal, identity in enumerate(identities[:MAX_THREADS]):
        thread = {"main": identity == main_ident, "ordinal": ordinal, "frames": [], "depth_capped": False}
        row["threads"].append(thread)
        if len(encoded(row)) > MAX_CAPTURE_BYTES:
            row["threads"].pop()
            row["bytes_capped"] = True
            return row
        frame = frames[identity]
        for _ in range(MAX_DEPTH):
            if frame is None:
                break
            thread["frames"].append(safe_location(frame, registry))
            if len(encoded(row)) > MAX_CAPTURE_BYTES:
                thread["frames"].pop()
                row["bytes_capped"] = True
                return row
            frame = frame.f_back
        thread["depth_capped"] = frame is not None
    require(len(encoded(row)) <= MAX_CAPTURE_BYTES)
    return row


def validate_report(row: dict) -> None:
    require(type(row) is dict and set(row) == {"schema", "mode", "capture", "threads", "threads_capped", "bytes_capped"})
    require(type(row["schema"]) is int and row["schema"] == 1 and row["mode"] == MODE)
    require(type(row["capture"]) is int and row["capture"] in (1, 2))
    require(type(row["threads_capped"]) is bool and type(row["bytes_capped"]) is bool)
    require(type(row["threads"]) is list and len(row["threads"]) <= MAX_THREADS)
    for ordinal, thread in enumerate(row["threads"]):
        require(type(thread) is dict and set(thread) == {"main", "ordinal", "frames", "depth_capped"})
        require(type(thread["main"]) is bool and type(thread["depth_capped"]) is bool)
        require(type(thread["ordinal"]) is int and thread["ordinal"] == ordinal)
        require(type(thread["frames"]) is list and len(thread["frames"]) <= MAX_DEPTH)
        for location in thread["frames"]:
            require(type(location) is dict and set(location) == {"source", "function", "line"})
            require(location["source"] in SOURCE_IDS)
            name = location["function"]
            require(type(name) is str and (FUNCTION.fullmatch(name) is not None or name in SPECIAL_NAMES))
            require(type(location["line"]) is int and 0 <= location["line"] <= 100000)
            if location["source"] == "other":
                require(name == "other" and location["line"] == 0)
            if name == "other":
                require(location["line"] == 0)
    require(sum(thread["main"] for thread in row["threads"]) <= 1 and len(encoded(row)) <= MAX_CAPTURE_BYTES)


def poll(fd: int, registry: dict, request: Path) -> None:
    seen = 0
    total = 0
    deadline = time.monotonic() + 180
    try:
        while seen < 2 and time.monotonic() < deadline:
            if request.exists():
                item = parse(read_regular(request, 2048))
                require(set(item) == {"schema", "mode", "capture"} and type(item["schema"]) is int and item["schema"] == 1)
                require(item["mode"] == MODE and type(item["capture"]) is int and item["capture"] in (1, 2))
                if item["capture"] > seen:
                    seen = item["capture"]
                    frames = sys._current_frames()
                    row = capture(seen, registry, frames, threading.main_thread().ident)
                    del frames
                    validate_report(row)
                    raw = encoded(row)
                    require(total + len(raw) <= MAX_TOTAL_BYTES and os.write(fd, raw) == len(raw))
                    total += len(raw)
                    os.fsync(fd)
            time.sleep(0.2)
    except Exception:
        # Unavailable/incomplete evidence has no inferred cause or exception text.
        return
    finally:
        os.close(fd)


def start(install: str, allowlist: str, evidence: str, metadata: str) -> bool:
    fd = None
    try:
        registry = load_registry(Path(install), Path(allowlist))
        fd = open_evidence(Path(evidence)) # Private, exclusive and close-on-exec BEFORE the daemon.
        worker = threading.Thread(target=poll, args=(fd, registry, Path(metadata) / REQUEST), daemon=True)
        worker.start()
        return True
    except Exception:
        if fd is not None:
            os.close(fd)
        return False # Do not replace the original launch with an instrumentation failure.


def instrument_wrapper(original: bytes, install: Path, scratch: Path, metadata: Path) -> bytes:
    text = original.decode("utf-8")
    require(text.count(ROOT_ANCHOR) == 1 and text.count(BOOTSTRAP) == 1)
    for path in (install, scratch, metadata):
        require(re.fullmatch(r"/[A-Za-z0-9/_. -]+", str(path)) is not None)
    injected = ('import sys; sys.path.insert(0, sys.argv.pop(1));\ntry:\n'
        ' import importlib.util\n'
        f' _s=importlib.util.spec_from_file_location("cm_resume_locations", {json.dumps(str(scratch / "resume_locations.py"))})\n'
        ' _m=importlib.util.module_from_spec(_s); _s.loader.exec_module(_m)\n'
        f' _m.start({json.dumps(str(install))}, {json.dumps(str(scratch / ALLOWLIST))}, '
        f'{json.dumps(str(scratch / EVIDENCE))}, {json.dumps(str(metadata))})\n'
        'except Exception:\n pass\n'
        'from claude_multi.entrypoints import main; raise SystemExit(main())')
    return text.replace(ROOT_ANCHOR, "root=" + shlex.quote(str(install)), 1).replace(BOOTSTRAP, injected, 1).encode()


def prepare(install: Path, dist: Path, scratch: Path, metadata: Path) -> None:
    archive_name = "claude-multi-1.1.0-linux-x86_64.tar.gz"
    release = parse(read_regular(dist / "MANIFEST.json", 1024 * 1024))
    require(release["version"] == "1.1.0")
    asset = release["assets"][archive_name]
    archive = dist / archive_name
    require(archive.is_file() and not archive.is_symlink() and archive.stat().st_size == asset["size"])
    digest = hashlib.sha256()
    with open(archive, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    require(digest.hexdigest() == asset["sha256"])
    files = {}
    wrapper = None
    top = archive_name.removesuffix(".tar.gz") + "/"
    with tarfile.open(archive, "r:gz") as members:
        for member in members:
            if not member.name.startswith(top):
                continue
            relative = member.name[len(top):]
            if relative not in SOURCES and relative != "bin/claude-multi":
                continue
            require(member.isfile() and 0 <= member.size <= 2 * 1024 * 1024)
            raw = members.extractfile(member).read(2 * 1024 * 1024 + 1)
            require(len(raw) == member.size)
            installed = read_regular(install / relative, 2 * 1024 * 1024)
            require(installed == raw)
            if relative == "bin/claude-multi":
                wrapper = raw
            else:
                source_info(raw, SOURCES[relative]) # Parse with the bundled Python's own grammar.
                files[relative] = hashlib.sha256(raw).hexdigest()
    require(set(files) == set(SOURCES) and wrapper is not None)
    document = {"schema": 1, "root": str(install), "files": files,
                "diagnostic_sha256": hashlib.sha256(read_regular(Path(__file__), 128 * 1024)).hexdigest()}
    raw = encoded(document)
    require(len(raw) <= 32768)
    fd = open_evidence(scratch / ALLOWLIST)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    changed = instrument_wrapper(wrapper, install, scratch, metadata)
    fd = open_evidence(scratch / WRAPPER)
    with os.fdopen(fd, "wb") as handle:
        handle.write(changed)
    os.chmod(scratch / WRAPPER, 0o755)


def export_reports(install: Path, scratch: Path, destination: Path) -> None:
    private = scratch / EVIDENCE
    if not os.path.lexists(private):
        return
    raw = read_regular(private, MAX_TOTAL_BYTES, private=True)
    if not raw:
        return
    registry = load_registry(install, scratch / ALLOWLIST)
    by_source = {info["source"]: info for info in registry.values()}
    rows = raw.splitlines()
    require(len(rows) <= 2)
    clean = []
    previous = 0
    for line in rows:
        require(len(line) <= MAX_CAPTURE_BYTES)
        row = parse(line)
        validate_report(row)
        require(row["capture"] > previous)
        previous = row["capture"]
        for thread in row["threads"]:
            for location in thread["frames"]:
                info = by_source.get(location["source"])
                if info is None or location["function"] not in info["functions"]:
                    location.update(function="other", line=0)
                elif location["line"] > info["lines"]:
                    location["line"] = 0
        clean.append(encoded(row))
    pending = destination.with_name(destination.name + ".pending")
    fd = open_evidence(pending)
    with os.fdopen(fd, "wb") as handle:
        handle.write(b"".join(clean))
    os.replace(pending, destination) # Only sanitized metadata crosses to Windows.


if __name__ == "__main__":
    try:
        if len(sys.argv) == 6 and sys.argv[1] == "prepare":
            prepare(*(Path(value) for value in sys.argv[2:]))
        elif len(sys.argv) == 5 and sys.argv[1] == "export":
            export_reports(*(Path(value) for value in sys.argv[2:]))
        else:
            require(False)
    except Exception:
        sys.exit("Code-location evidence preparation/export failed")
