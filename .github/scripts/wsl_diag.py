"""Temporary, metadata-only WSL resume diagnostic. No product changes.

Only the fixed metadata filenames below may cross the Windows boundary.
Turn outputs stay in the disposable Linux HOME; this module never reads them.
The original journey still extracts replies and asserts carried history locally.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time

SHA = "8b4ec9e1572d41d33396dbc67a3a1951bb2561b0"
RUN = 37633707710
ARTIFACT_ID = 11488260478
ARTIFACT_DIGEST = "sha256:c3d5f116bccab719c962df4502dca9efcbcca5ef0884ced6af7dcd8514fff6d3"
CLIENT_SHA = "a967e7b1d8b4e47ee421d5433027880347952b0c0857abf880e2c942a4ec93b3"
CLIENT_SIZE = 251456696
MODE = "hosted-disposable-wsl-fixture-only"
ENV_REFUSAL = "WSL diagnostic refused unsafe hosted fixture environment"
CREDENTIAL_NAME = re.compile(
    r"(?:^|_)(?:APIKEY|ACCESSKEY|ACCESSTOKEN|AUTH(?:ORIZATION)?|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|COOKIE|KEY|JWT|BEARER|PAT)(?:_|$)", re.I)
OPERATOR_ENV_NAMES = frozenset(("CLAUDECODE", "CLAUDE_CONFIG_DIR", "CLAUDE_MODEL", "CLAUDE_CODE_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "CLAUDE_CODE_DISABLE_FAST_MODE",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "DEPLOY", "WRITABLE_PATH", "MANAGEMENT_STATIC_PATH", "META_MINT_URL"))
LIMITS = {
    "artifact.json": 2048, "dist.json": 2048, "client.json": 2048,
    "hosted-ci.json": 2048, "preflight.json": 2048, "observer.json": 2048, "capture-request.json": 2048, "cleanup.json": 2048,
    "outcome.json": 2048, "watchdog.json": 2048,
    "fixture.jsonl": 1024 * 1024, "snapshots.jsonl": 8 * 1024 * 1024,
}
MANDATORY = ("artifact.json", "watchdog.json")
OPTIONAL = frozenset(LIMITS).difference(MANDATORY)
EXPORT_LIMITS = {**LIMITS, "validation.json": 4096}
RECORD_LIMITS = {"fixture.jsonl": 256, "snapshots.jsonl": 128}
LINE_LIMITS = {"fixture.jsonl": 4096, "snapshots.jsonl": 65536}
EXES = frozenset(("claude", "cli-proxy-api", "python3", "python3.14", "dash", "bash", "sh",
                  "runuser", "unshare", "sleep", "sed", "head", "grep", "cat", "other", "unavailable"))
# Unknown kernel symbols are deliberately generalized, never copied verbatim.
WCHANS = frozenset(("0", "do_wait", "futex_wait", "futex_wait_queue", "wait_woken", "do_epoll_wait",
                    "ep_poll", "pipe_read", "pipe_write", "locks_lock_inode_wait", "fcntl_setlk",
                    "inet_csk_accept", "unix_stream_read_generic", "unix_stream_recvmsg", "tcp_recvmsg",
                    "sk_wait_data", "hrtimer_nanosleep", "schedule_timeout", "pidfd_poll",
                    "poll_schedule_timeout.constprop.0", "other", "unavailable"))
LOCK_NAMES = frozenset(("migration", "gateway-inhibition", "channel", "gateway", "gateway-start",
                        "runtime-index", "lifecycle", "pointer", "api-key", "token-rotation",
                        "served-change", "settings", "operator", "client-use", "client-acquire"))

TAIL_ANCHOR = '''show_tail() {
\t# $1 = file: its last lines on stderr (journey logs carry no secrets)
\t[ -f "$1" ] && tail -n 20 "$1" >&2
\treturn 0
}'''
RESUME_ANCHOR = '''\t(cd "$work/project" && "$cm" -c -- -p "journey turn two") \\
\t\t>"$work/turn2.txt" 2>"$work/turn2.err" </dev/null ||'''
CARRIED_ANCHOR = '''\tfx carried --log "$work/fixture.log" --reply "$second" --earlier "$first" ||
\t\tfail "the resumed turn did not carry the first turn's reply"
\tsay "resumed managed session: ok (fixture reply $second carried reply $first)"'''
JOURNEY_PATCHES = (
    (TAIL_ANCHOR, 'show_tail() { return 0; }'),
    ('trap cleanup EXIT\n', "trap ':' EXIT # PID namespace teardown owns all process cleanup.\n"),
    (CARRIED_ANCHOR, CARRIED_ANCHOR.replace('\tsay ',
        '\t"$install_dir/runtime/python/bin/python3" -I "$here/wsl_diag.py" outcome\n\tsay ')),
    ('\t\t\tmanaged_turn\n', '\t\t\tmanaged_turn\n\t\t\texit 0 # Namespace exit, not later shutdown/uninstall controls.\n'),
)
LOG_ANCHOR = '''def _log(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\\n")'''
FIXTURE_PATCHES = (
    ('from pathlib import Path\n', '''from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import wsl_diag
'''),
    (LOG_ANCHOR, '''def _log(path: Path, record: dict) -> None:
    if path.name != "fixture.log":
        raise ValueError("unexpected fixture log")
    safe = wsl_diag.fixture_record(record)
    wsl_diag.append_json(path, safe, wsl_diag.LIMITS["fixture.jsonl"])
    wsl_diag.append_json(Path(os.environ["CM_DIAG_METADATA"]) / "fixture.jsonl",
                         safe, wsl_diag.LIMITS["fixture.jsonl"])'''),
)


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("diagnostic metadata or input rejected")


class HostedEnvironmentRefused(ValueError):
    """A fixed refusal; no credential value is inspected or variable unset."""


def admit_hosted_environment(environ) -> None:
    # The hosted baseline intentionally retains WSL interop and shared runtime.
    # Only names are inspected for credentials, provider bypasses and state/model selectors.
    for name in environ:
        upper = name.upper()
        if CREDENTIAL_NAME.search(name) or upper in OPERATOR_ENV_NAMES or upper.startswith(
                ("CLAUDE_MULTI_", "ANTHROPIC_", "CLAUDE_CODE_USE_", "PGSTORE_", "GITSTORE_", "OBJECTSTORE_")):
            raise HostedEnvironmentRefused(ENV_REFUSAL)


def keys(record: object, names: str) -> None:
    require(type(record) is dict and set(record) == set(names.split()))


def integer(value: object, low: int = 0, high: int = 2**63 - 1) -> None:
    require(type(value) is int and low <= value <= high)


def boolean(value: object) -> None:
    require(type(value) is bool)


def code(value: object) -> None:
    if value is not None:
        integer(value, -(2**31), 2**31 - 1)


def digest(value: object) -> None:
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def validate_record(name: str, row: dict) -> None:
    if name == "fixture.jsonl":
        keys(row, "event method path_category model stream reply carried")
        require(row["event"] == "request-parsed")  # Before response, not delivery evidence.
        require(row["method"] in ("GET", "POST"))
        require(row["path_category"] in ("models", "chat-completions", "other"))
        require(row["model"] == "fixture-model-1")
        boolean(row["stream"])
        if row["reply"] is not None:
            integer(row["reply"], 1, 1000000)
        require(type(row["carried"]) is list and len(row["carried"]) <= 128)
        for n in row["carried"]:
            integer(n, 1, 1000000)
        require(row["carried"] == sorted(set(row["carried"])))
        return
    if name == "snapshots.jsonl":
        keys(row, "event elapsed_ms processes locks processes_truncated locks_available locks_truncated")
        require(row["event"] in ("start", "periodic", "resume-start", "end"))
        integer(row["elapsed_ms"], 0, 400000)
        for field in ("processes_truncated", "locks_available", "locks_truncated"):
            boolean(row[field])
        for field in ("processes", "locks"):
            require(type(row[field]) is list and len(row[field]) <= 64)
        for process in row["processes"]:
            keys(process, "pid ppid pgid start_ticks state exe wchan")
            for field in ("pid", "ppid", "pgid", "start_ticks"):
                integer(process[field])
            require(process["state"] in tuple("RSDZTWtXxKPI"))
            require(process["exe"] in EXES and process["wchan"] in WCHANS)
        for lock in row["locks"]:
            keys(lock, "name type mode role pid dev_major dev_minor inode")
            require(lock["name"] in LOCK_NAMES)
            require(lock["type"] in ("FLOCK", "POSIX", "OFDLCK"))
            require(lock["mode"] in ("READ", "WRITE"))
            require(lock["role"] in ("holder", "waiter"))
            integer(lock["pid"], -1)
            for field in ("dev_major", "dev_minor", "inode"):
                integer(lock[field])
        return
    fields = {
        "artifact.json": "schema repository run_id artifact_id name size_bytes digest head_sha",
        "dist.json": "schema sha256sums_verified sums_sha256 launcher_version client_version client_sha256",
        "client.json": "schema installed_version client_version sha256 size_bytes verified",
        "hosted-ci.json": "schema mode github_hosted_windows windows_worker distribution_was_absent native_home_clean credential_environment_clear",
        "preflight.json": "schema mode ordinary_user fresh_home linux_filesystem provider_auth_empty credential_environment_clear verified_release verified_client candidate_windows_cwd",
        "observer.json": "schema mode end_captured",
        "capture-request.json": "schema mode stop",
        "cleanup.json": "schema mode capture_confirmed cleanup_confirmed terminate_state",
        "outcome.json": "schema mode managed_turn_passed journey_exit_code",
        "watchdog.json": "schema mode status elapsed_ms armed_at_ms arm_basis launcher_exit_code capture_confirmed cleanup_confirmed terminate_state",
        "validation.json": "schema optional_metadata_valid omitted",
    }
    keys(row, fields[name])
    require(type(row["schema"]) is int and row["schema"] == 1)
    if name in ("hosted-ci.json", "preflight.json", "observer.json", "capture-request.json", "cleanup.json", "outcome.json", "watchdog.json"):
        require(row["mode"] == MODE)
    if name == "artifact.json":
        require(row == {"schema": 1, "repository": "nkoturovic/claude-multi", "run_id": RUN,
                        "artifact_id": ARTIFACT_ID, "name": "dist", "size_bytes": 204873528,
                        "digest": ARTIFACT_DIGEST, "head_sha": SHA})
    elif name == "dist.json":
        require(row["sha256sums_verified"] is True and row["launcher_version"] == "1.1.0"
                and row["client_version"] == "2.1.292" and row["client_sha256"] == CLIENT_SHA)
        digest(row["sums_sha256"])
    elif name == "client.json":
        require(row["installed_version"] == "1.1.0" and row["client_version"] == "2.1.292"
                and row["sha256"] == CLIENT_SHA and row["size_bytes"] == CLIENT_SIZE and row["verified"] is True)
    elif name in ("hosted-ci.json", "preflight.json"):
        require(all(row[field] is True for field in row if field not in ("schema", "mode")))
    elif name == "observer.json":
        require(row["end_captured"] is True)
    elif name == "capture-request.json":
        require(row["stop"] is True)
    elif name == "outcome.json":
        boolean(row["managed_turn_passed"])
        code(row["journey_exit_code"])
    elif name == "cleanup.json":
        boolean(row["capture_confirmed"])
        boolean(row["cleanup_confirmed"])
        require(row["terminate_state"] in ("returned", "failed", "timeout", "launch-failed"))
        require(row["cleanup_confirmed"] == (row["terminate_state"] == "returned"))
    elif name == "watchdog.json":
        require(row["arm_basis"] == "first-fixture-request")
        require(row["status"] in ("completed", "bootstrap-timeout", "resume-timeout", "absolute-timeout",
                                   "launch-failed", "invalid-metadata", "watchdog-error"))
        integer(row["elapsed_ms"], 0, 480000)
        if row["armed_at_ms"] is not None:
            integer(row["armed_at_ms"], 0, 360000)
        code(row["launcher_exit_code"])
        boolean(row["capture_confirmed"])
        boolean(row["cleanup_confirmed"])
        require(row["terminate_state"] in ("pending", "returned", "failed", "timeout", "launch-failed", "not-owned"))
        require(row["cleanup_confirmed"] == (row["terminate_state"] == "returned"))
    elif name == "validation.json":
        boolean(row["optional_metadata_valid"])
        require(type(row["omitted"]) is list and len(row["omitted"]) <= len(OPTIONAL))
        names = []
        for omission in row["omitted"]:
            keys(omission, "file reason")
            require(type(omission["file"]) is str and omission["file"] in OPTIONAL)
            require(omission["reason"] == "invalid-metadata")
            names.append(omission["file"])
        require(names == sorted(set(names)))
        require(row["optional_metadata_valid"] == (not names))


def encoded(row: dict) -> bytes:
    return (json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")


def atomic_json(path: Path, row: dict) -> None:
    validate_record(path.name, row)
    raw = encoded(row)
    require(len(raw) <= LIMITS[path.name])
    pending = path.with_name(path.name + ".pending")
    with open(pending, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)


_APPEND_LOCK = threading.Lock()


def append_json(path: Path, row: dict, limit: int) -> None:
    # The fixture's local file has the same schema as its exported metadata.
    name = "fixture.jsonl" if path.name == "fixture.log" else path.name
    validate_record(name, row)
    raw = encoded(row)
    require(len(raw) <= LINE_LIMITS[name])
    # ThreadingHTTPServer can parse requests concurrently. One writer owns
    # each JSONL file, and this lock serializes its bound check with its write.
    with _APPEND_LOCK:
        previous = safe_read(path, limit) if os.path.lexists(path) else b""
        require(not previous or previous.endswith(b"\n"))
        require(previous.count(b"\n") < RECORD_LIMITS[name])
        require(len(previous) + len(raw) <= limit)
        with open(path, "ab", buffering=0) as handle:
            handle.write(raw)
            os.fsync(handle.fileno())


def no_duplicates(pairs: list) -> dict:
    row = {}
    for key, value in pairs:
        require(key not in row)
        row[key] = value
    return row


def parse(raw: bytes) -> dict:
    return json.loads(raw, object_pairs_hook=no_duplicates,
                      parse_constant=lambda _: require(False))


def safe_read(path: Path, limit: int) -> bytes:
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and not getattr(before, "st_file_attributes", 0) & 0x400)
    require(before.st_size <= limit)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        after = os.fstat(handle.fileno())
        require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino) and stat.S_ISREG(after.st_mode))
        raw = handle.read(limit + 1)
    require(len(raw) <= limit)
    return raw


def validated_bytes(path: Path, name: str) -> bytes:
    raw = safe_read(path, LIMITS[name])
    records = raw.splitlines() if name.endswith(".jsonl") else [raw]
    require(len(records) <= RECORD_LIMITS.get(name, 1))
    clean = []
    for record in records:
        require(len(record) <= LINE_LIMITS.get(name, 4096))
        row = parse(record)
        validate_record(name, row)
        clean.append(encoded(row))
    return b"".join(clean)


def validate_directory(source: Path, destination: Path) -> None:
    info = source.lstat()
    require(stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400)
    require(not source.is_symlink() and source.resolve() != destination.resolve())
    # Missing or invalid mandatory evidence fails closed, before any export.
    sanitized = {name: validated_bytes(source / name, name) for name in MANDATORY}
    omitted = []
    # Never enumerate, open or copy unknown files, nor recurse into a HOME.
    for name in sorted(OPTIONAL):
        try:
            sanitized[name] = validated_bytes(source / name, name)
        except FileNotFoundError:
            continue  # A failed bootstrap may leave only partial metadata.
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            # Keep independently valid evidence, never partial rows or error text.
            omitted.append({"file": name, "reason": "invalid-metadata"})
    report = {"schema": 1, "optional_metadata_valid": not omitted, "omitted": omitted}
    validate_record("validation.json", report)
    sanitized["validation.json"] = encoded(report)
    require(len(sanitized["validation.json"]) <= EXPORT_LIMITS["validation.json"])
    destination.mkdir()  # Refuse stale or reparse-point upload destinations.
    for name, raw in sanitized.items():
        (destination / name).write_bytes(raw)


def check_export(directory: Path) -> None:
    report = parse(safe_read(directory / "validation.json", EXPORT_LIMITS["validation.json"]))
    validate_record("validation.json", report)
    # Export safety and job outcome are separate: omitted bad metadata still fails.
    require(report["optional_metadata_valid"])


def patch_text(text: str, replacements: tuple) -> str:
    for old, new in replacements:
        require(text.count(old) == 1)
        text = text.replace(old, new, 1)
    return text


def patch(source: Path, scratch: Path) -> None:
    originals = {"journey.sh": "f260528ae4f9ec0209f2a3699c9de6317a9c0aa15768d425b6945de70ad49f86",
                 "journey_fixture.py": "26777d13525c99054e7c2de5cfa48c480d214bdfec26080662b265336cd2ca83"}
    changed = {}
    for name, patches in (("journey.sh", JOURNEY_PATCHES), ("journey_fixture.py", FIXTURE_PATCHES)):
        raw = (source / name).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == originals[name])
        changed[name] = patch_text(raw.decode(), patches)
    require(source.resolve() != scratch.resolve())
    for name, text in changed.items():
        (scratch / name).write_text(text, encoding="utf-8")


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def verify_dist(dist: Path, metadata: Path) -> None:
    names = {"MANIFEST.json", "install.sh", "install.ps1"} | {
        f"claude-multi-1.1.0-{target}.tar.gz" for target in
        ("linux-x86_64", "linux-aarch64", "darwin-x86_64", "darwin-arm64")}
    sums = safe_read(dist / "SHA256SUMS", 4096)
    seen = set()
    for line in sums.decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
        require(match is not None)
        expected, name = match.groups()
        require(name in names and name not in seen)
        seen.add(name)
        info = (dist / name).lstat()
        require(stat.S_ISREG(info.st_mode) and not (dist / name).is_symlink())
        require(file_hash(dist / name) == expected)
    require(seen == names)
    manifest = parse(safe_read(dist / "MANIFEST.json", 1024 * 1024))
    require(manifest["version"] == "1.1.0" and manifest["test_build"] is False)
    require(manifest["claude_code"]["version"] == "2.1.292")
    require(manifest["claude_code"]["platforms"]["linux-x64"] == {"sha256": CLIENT_SHA, "size": CLIENT_SIZE})
    atomic_json(metadata / "dist.json", {"schema": 1, "sha256sums_verified": True,
        "sums_sha256": hashlib.sha256(sums).hexdigest(), "launcher_version": "1.1.0",
        "client_version": "2.1.292", "client_sha256": CLIENT_SHA})


def verify_client(client: Path, install: Path, metadata: Path) -> None:
    data = install / "lib/python3.14/site-packages/claude_multi/data"
    require(parse((data / "version.json").read_bytes())["launcher_version"] == "1.1.0")
    contract = parse((data / "catalog/native-contract.json").read_bytes())["verified"][0]
    require(contract["version"] == "2.1.292")
    require(contract["platforms"]["linux-x64"] == {"sha256": CLIENT_SHA, "size": CLIENT_SIZE})
    require(not client.is_symlink() and client.is_file() and client.stat().st_size == CLIENT_SIZE)
    require(file_hash(client) == CLIENT_SHA)
    atomic_json(metadata / "client.json", {"schema": 1, "installed_version": "1.1.0",
        "client_version": "2.1.292", "sha256": CLIENT_SHA, "size_bytes": CLIENT_SIZE, "verified": True})


def fixture_record(record: dict) -> dict:
    require(record.get("method") in ("GET", "POST"))
    require(record.get("model", "fixture-model-1") == "fixture-model-1")
    path = record.get("path")
    category = {"/v1/models": "models", "/v1/chat/completions": "chat-completions"}.get(path, "other")
    row = {"event": "request-parsed", "method": record["method"], "path_category": category,
           "model": "fixture-model-1", "stream": record.get("stream", False),
           "reply": record.get("reply"), "carried": record.get("carried", [])}
    validate_record("fixture.jsonl", row)
    return row


def process_metadata(pid: int, proc: Path = Path("/proc")) -> dict | None:
    try:
        directory = proc / str(pid)
        raw = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        start = int(raw[19])
        try:
            exe = Path(os.readlink(directory / "exe")).name
            exe = exe if exe in EXES else "other"
        except OSError:
            exe = "unavailable"
        try:
            wchan = (directory / "wchan").read_text().strip()
            wchan = wchan if wchan in WCHANS else "other"
        except OSError:
            wchan = "unavailable"
        # Do not combine observations of two processes that reused one PID.
        if int((directory / "stat").read_text().rsplit(")", 1)[1].split()[19]) != start:
            return None
        return {"pid": pid, "ppid": int(raw[1]), "pgid": int(raw[2]), "start_ticks": start,
                "state": raw[0], "exe": exe, "wchan": wchan}
    except (OSError, ValueError, IndexError):
        return None


def known_locks(home: Path) -> dict:
    state = home / ".local/state/claude-multi"
    config = home / ".config/claude-multi"
    client = home / ".local/share/claude-multi/claude"
    paths = [(state / (name + ".lock"), name) for name in ("migration", "gateway-inhibition", "channel")]
    paths += [(state / "gateway" / (name + ".lock"), name) for name in ("gateway", "gateway-start")]
    paths += [(state / "locks/runtime-index.lock", "runtime-index"),
              (client / "2.1.292/.in-use", "client-use"), (client / ".acquire-2.1.292.lock", "client-acquire")]
    paths += [(config / name, label) for name, label in (
        ("api-key.lock", "api-key"), ("token-rotation.lock", "token-rotation"),
        ("served-change.lock", "served-change"), ("settings.json.lock", "settings"), ("operator-ledger.json.lock", "operator"))]
    # Only names in product lock directories, never records, pointers or configs.
    for directory, pattern, label in (
        (state / "locks", r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.lifecycle\.lock", "lifecycle"),
        (state / "last-session-by-cwd", r"[0-9a-f]{32}\.json\.lock", "pointer"),
    ):
        try:
            with os.scandir(directory) as entries:
                for entry in list_entry_names(entries, 64):
                    if re.fullmatch(pattern, entry):
                        paths.append((directory / entry, label))
        except OSError:
            pass
    found = {}
    for path, label in paths:
        try:
            info = path.stat(follow_symlinks=False)  # Never open a lock file.
            if stat.S_ISREG(info.st_mode):
                found[(os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)] = label
        except OSError:
            pass
    return found


def list_entry_names(entries, maximum: int) -> list[str]:
    names = []
    for entry in entries:
        if len(names) == maximum:
            break
        names.append(entry.name)
    return names


def lock_metadata(raw: str, known: dict) -> tuple[list, bool]:
    rows = []
    for line in raw.splitlines():
        fields = line.split()
        waiting = len(fields) > 1 and fields[1] == "->"
        if waiting:
            fields.pop(1)
        if len(fields) < 6 or fields[1] not in ("FLOCK", "POSIX", "OFDLCK") or fields[3] not in ("READ", "WRITE"):
            continue
        try:
            major, minor, inode = fields[5].split(":")
            identity = (int(major, 16), int(minor, 16), int(inode))
            if identity not in known:
                continue
            rows.append({"name": known[identity], "type": fields[1], "mode": fields[3],
                         "role": "waiter" if waiting else "holder", "pid": int(fields[4]),
                         "dev_major": identity[0], "dev_minor": identity[1], "inode": identity[2]})
        except (ValueError, IndexError):
            continue
        if len(rows) > 64:
            return rows[:64], True
    return rows, False


def snapshot(home: Path, event: str, elapsed: float) -> dict:
    processes = []
    with os.scandir("/proc") as entries:
        for entry in entries:
            if entry.name.isdecimal():
                row = process_metadata(int(entry.name))
                if row is not None:
                    processes.append(row)
                if len(processes) > 64:
                    break
    try:
        raw = safe_read(Path("/proc/locks"), 1024 * 1024).decode("ascii")
        locks, truncated = lock_metadata(raw, known_locks(home))
        available = True
    except (OSError, ValueError):
        locks, truncated, available = [], False, False
    return {"event": event, "elapsed_ms": int(elapsed * 1000), "processes": processes[:64], "locks": locks,
            "processes_truncated": len(processes) > 64, "locks_available": available, "locks_truncated": truncated}


def empty_by_names(path: Path) -> None:
    """Fresh-state guard: stat/list names only, never open any record or config."""
    if not os.path.lexists(path):
        return
    require(stat.S_ISDIR(path.lstat().st_mode))
    with os.scandir(path) as entries:
        require(next(entries, None) is None)


def linux_filesystem(path: Path) -> None:
    checked = subprocess.run(["/usr/bin/stat", "-f", "-c", "%T", str(path)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2, check=False)
    require(checked.returncode == 0 and checked.stdout.strip() in
            (b"ext2/ext3", b"ext4", b"xfs", b"btrfs", b"tmpfs", b"overlayfs"))


def fresh_home(home: Path) -> None:
    # No native Claude state, provider auth, declarations or credential files are imported.
    for relative in (".claude", ".config/claude", ".local/share/claude-multi/auth", ".config/claude-multi",
                     ".local/state/claude-multi/sessions", ".local/state/claude-multi/scopes",
                     ".local/state/claude-multi/last-session-by-cwd"):
        empty_by_names(home / relative)
    for relative in (".claude.json", ".config/claude-multi/api-key", ".config/claude-multi/previous-key",
                     ".config/claude-multi/config.yaml", ".config/claude-multi/custom.json"):
        require(not os.path.lexists(home / relative))
    for path in (home, home / ".local/share/claude-multi/install/current", Path(os.environ.get("TMPDIR") or "/tmp")):
        linux_filesystem(path)
    if os.environ.get("CLAUDE_CODE_TMPDIR"):
        linux_filesystem(Path(os.environ["CLAUDE_CODE_TMPDIR"]))


def hosted_preflight(metadata: Path, workspace: Path) -> None:
    """Only the reviewed hosted Windows VM + its fresh WSL distro may run clients."""
    require(sys.platform.startswith("linux") and "microsoft" in platform.release().lower()
            and "wsl" in platform.release().lower())
    import pwd

    for name in ("artifact.json", "hosted-ci.json"):
        row = parse(safe_read(metadata / name, LIMITS[name]))
        validate_record(name, row)
    for name in ("preflight.json", "outcome.json", "observer.json", "capture-request.json"):
        require(not os.path.lexists(metadata / name))
    require(os.geteuid() != 0 and pwd.getpwuid(os.geteuid()).pw_name == "journey")
    home = Path.home()
    require(home == Path("/home/journey"))
    require(re.fullmatch(r"/mnt/[a-z]/[A-Za-z0-9/_. -]+/candidate", str(workspace)) is not None)
    require(Path.cwd().resolve() == workspace.resolve())
    admit_hosted_environment(os.environ)
    fresh_home(home)
    install = home / ".local/share/claude-multi/install/current"
    verify_dist(workspace.parent / "dist", metadata)
    verify_client(home / "diag/claude", install, metadata)
    atomic_json(metadata / "preflight.json", {"schema": 1, "mode": MODE, **{field: True for field in
        "ordinary_user fresh_home linux_filesystem provider_auth_empty credential_environment_clear verified_release verified_client candidate_windows_cwd".split()}})
    atomic_json(metadata / "outcome.json", {"schema": 1, "mode": MODE,
                                          "managed_turn_passed": False, "journey_exit_code": None})


def observe(metadata: Path) -> None:
    """Background sibling only: observe fresh-distro PIDs, never launch/reparent a journey."""
    preflight = parse(safe_read(metadata / "preflight.json", LIMITS["preflight.json"]))
    validate_record("preflight.json", preflight)
    require(os.geteuid() != 0 and Path.home() == Path("/home/journey"))
    stopping = False
    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
    previous = signal.signal(signal.SIGTERM, stop)
    start = time.monotonic()
    next_sample = start + 3
    try:
        append_json(metadata / "snapshots.jsonl", snapshot(Path.home(), "start", 0), LIMITS["snapshots.jsonl"])
        while not stopping and time.monotonic() - start < 345:
            if (metadata / "capture-request.json").exists():
                request = parse(safe_read(metadata / "capture-request.json", LIMITS["capture-request.json"]))
                validate_record("capture-request.json", request)
                break
            now = time.monotonic()
            if now >= next_sample:
                append_json(metadata / "snapshots.jsonl", snapshot(Path.home(), "periodic", now - start),
                            LIMITS["snapshots.jsonl"])
                next_sample = now + 3
            time.sleep(0.1)
        append_json(metadata / "snapshots.jsonl", snapshot(Path.home(), "end", time.monotonic() - start),
                    LIMITS["snapshots.jsonl"])
        atomic_json(metadata / "observer.json", {"schema": 1, "mode": MODE, "end_captured": True})
    finally:
        signal.signal(signal.SIGTERM, previous)


def finish(metadata: Path, status: int) -> None:
    code(status)
    outcome = parse(safe_read(metadata / "outcome.json", LIMITS["outcome.json"]))
    validate_record("outcome.json", outcome)
    outcome["journey_exit_code"] = status
    atomic_json(metadata / "outcome.json", outcome)
    atomic_json(metadata / "capture-request.json", {"schema": 1, "mode": MODE, "stop": True})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("patch", "verify-dist", "verify-client", "validate", "check-export",
                                             "guard-environment", "hosted-preflight", "observe", "finish", "outcome"))
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    paths = [Path(p) for p in args.paths]
    if args.command == "patch":
        patch(*paths)
    elif args.command == "verify-dist":
        verify_dist(*paths)
    elif args.command == "verify-client":
        verify_client(*paths)
    elif args.command == "validate":
        validate_directory(*paths)
    elif args.command == "check-export":
        check_export(paths[0])
    elif args.command == "guard-environment":
        admit_hosted_environment(os.environ)
    elif args.command == "hosted-preflight":
        hosted_preflight(paths[0], paths[1])
    elif args.command == "observe":
        observe(paths[0])
    elif args.command == "finish":
        finish(paths[0], int(str(paths[1])))
    elif args.command == "outcome":
        metadata = Path(os.environ["CM_DIAG_METADATA"])
        atomic_json(metadata / "outcome.json", {"schema": 1, "mode": MODE, "managed_turn_passed": True,
                                                "journey_exit_code": None})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HostedEnvironmentRefused:
        sys.exit(ENV_REFUSAL)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        # No exception repr, raw subprocess output, record or path in public logs.
        sys.exit("WSL diagnostic failed (input, containment or metadata); no raw output exported")
