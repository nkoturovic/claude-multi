"""The release evidence of the essential Claude Code battery (CI).

  python3 .github/scripts/evidence.py --log LOG --status N --claude FILE \
      --modules FILE --out DIR

LOG is the battery's output and N its exit status; FILE the pinned Claude
Code the battery ran (read for its sha256 and size only, never copied);
--modules the file listing the test modules it ran, one per line. The
product's own positive-completion check (``claude_multi.upgrade``: every
essential completion line, or a named alternative, at the start of a line)
runs on the log. The log merges unittest's progress and test headers
(stderr) with the probes' completion lines (stdout), so a completion line
may also start right after a run of progress characters or a test header;
the check sees each such remainder as a line of its own, and nothing else
in a line counts. DIR receives the log, unchanged, as ``battery.log`` and
``manifest.json``: the commit, the workflow run, the client's version and
platform (from the shipped contract) with its sha256 and size, the
gateway's and the probes' sha256 with the identity of the gateway build
record (from the test variables that selected them), the modules, the
battery's exit status, the missing completion lines, the dispositions of
the client-skew probes (AD, SL) and the verdict.

The client-skew probes need a Claude Code newer than the pin, which a clean
runner does not have; their outcome may be a boundary, but the log must
show it: a probe record, or the skipped test with its reason (the battery
runs verbose). A probe with neither fails the verdict.

A log that carries a fixture secret (the test gateway token, a dummy key or
a hint marker) is not written: the manifest says so and the verdict fails.
Exit 0 only when the battery passed, no completion line or skew disposition
is missing and the log is clean; 1 otherwise; 2 for a usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
TESTS = REPO / "tests"
MANIFEST_FORMAT = 1
GATEWAY_ENV = "CLAUDE_MULTI_TEST_CLI_PROXY_API"
BUILD_RECORD_ENV = "CLAUDE_MULTI_TEST_GATEWAY_BUILD"
STARTUP_PROBE_ENV = "CLAUDE_MULTI_TEST_GATEWAY_STARTUP_PROBE"
RUN_FIELDS = ("GITHUB_REPOSITORY", "GITHUB_WORKFLOW", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB")
# unittest's own output in a merged log: a verbose test header
# ("test_x (tests.m.C.test_x)", then " ... " and the result), or a run of
# verbosity-1 progress characters.
TEST_HEADER = re.compile(r"(\w+) \(([\w.]+)\)")
HEADER_RESULT = re.compile(r"\w+ \([\w.]+\) \.\.\. ")
PROGRESS = re.compile(r"[.sFExu]+")
# The client-skew probes: their record prefix and their test.
SKEW_PROBES = {
    "AD": ("client probe AD: ", re.compile(r"(^|\.)test_real_adoption_onto_newer_plain_claude$")),
    "SL": ("client probe SL: ", re.compile(r"(^|\.)NewerTranscriptResumeProbe(\.test_observation)?$")),
}


def _imports() -> None:
    for path in (SRC, TESTS):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def carries_fixture_secret(text: str) -> bool:
    """Whether ``text`` holds a value the tests use as a credential or a
    hint marker (the gateway harness's own list, which its evidence refuses
    too) or an OAuth token prefix; none may leave the runner."""

    _imports()
    import _gateway_harness

    try:
        _gateway_harness.Evidence.scalar(text)
    except ValueError:
        return True
    return "sk-ant-oat" in text


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(value: str | None) -> dict | None:
    if not value:
        return None
    path = Path(os.path.realpath(value))
    if not path.is_file():
        return {"path": str(path), "sha256": None}
    return {"path": str(path), "sha256": sha256_file(path)}


def client_identity(client: Path) -> dict:
    _imports()
    from claude_multi import pin, strict_json

    contract = strict_json.load(SRC / "claude_multi" / "data" / "catalog" / "native-contract.json")
    platform = pin.host_platform()
    record = pin.platform_record(contract, platform) or {}
    sha256 = sha256_file(client) if client.is_file() else None
    return {"version": pin.version(contract), "platform": platform, "sha256": sha256,
            "size": client.stat().st_size if client.is_file() else None,
            "matches_contract": sha256 is not None and sha256 == record.get("sha256")}


def gateway_identity(environ: dict[str, str]) -> dict:
    gateway = _file_identity(environ.get(GATEWAY_ENV))
    record_path = environ.get(BUILD_RECORD_ENV)
    build = None
    if record_path and Path(record_path).is_file():
        try:
            document = json.loads(Path(record_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            document = {}
        build = {"sha256": sha256_file(Path(record_path)),
                 "series": [f"{entry.get('basename')}@{entry.get('sha256')}" for entry in document.get("series", [])
                            if isinstance(entry, dict)],
                 "post_apply_tree_sha256": document.get("post_apply_tree_sha256"),
                 "toolchain": document.get("toolchain")}
    probes = None
    startup = environ.get(STARTUP_PROBE_ENV)
    if startup:
        directory = Path(os.path.realpath(startup)).parent
        record = directory.parent / "share" / "probe-build.json"
        probes = {"record_sha256": sha256_file(record) if record.is_file() else None,
                  "binaries": {path.name: sha256_file(path) for path in sorted(directory.iterdir()) if path.is_file()}}
    return {"gateway": gateway, "build_record": build, "probes": probes}


def commit() -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                                timeout=30, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def record_lines(log_text: str) -> str:
    """The log with every place a record may start as a line start: each
    line as it is, plus its remainder after a verbose test header
    (``test_x (id) ... ``, or `` ... `` on the docstring line under a bare
    header) or after a run of progress characters. Only these unittest
    prefixes are recognised, so the anchored check stays anchored."""

    lines, bare = [], False
    for line in log_text.split("\n"):
        lines.append(line)
        header = HEADER_RESULT.match(line)
        if header:
            lines.append(line[header.end():])
        elif bare and " ... " in line:
            lines.append(line.split(" ... ", 1)[1])
        else:
            progress = PROGRESS.match(line)
            if progress and progress.end() < len(line):
                lines.append(line[progress.end():])
        bare = TEST_HEADER.fullmatch(line) is not None
    return "\n".join(lines)


def skipped_tests(log_text: str) -> dict[str, str]:
    """``{test id: reason}`` of the skips a verbose log records: the
    header names the test, `` ... skipped 'reason'`` (or a later line
    ``skipped 'reason'``, after the test's own output) gives the reason."""

    current, found = None, {}
    for line in log_text.splitlines():
        header = TEST_HEADER.match(line)
        if header:
            current = header.group(2)
        if current is None:
            continue
        index = line.find(" ... skipped")
        if index >= 0 or line.startswith("skipped "):
            reason = line[index + len(" ... skipped"):] if index >= 0 else line[len("skipped"):]
            found.setdefault(current, reason.strip().strip("'\""))
            current = None
    return found


def skew_dispositions(log_text: str) -> dict[str, dict]:
    """What the log shows of each client-skew probe: its records (status
    first) and its skipped test with the reason. Neither is ``missing``."""

    records = record_lines(log_text).split("\n")
    skips = skipped_tests(log_text)
    found = {}
    for name, (head, test) in SKEW_PROBES.items():
        lines = sorted({line[len(head):].strip()[:200] for line in records if line.startswith(head)})
        skipped = {test_id: reason[:200] for test_id, reason in sorted(skips.items()) if test.search(test_id)}
        found[name] = {"records": lines, "skipped": skipped,
                       "disposition": ("recorded" if lines else "skipped") if lines or skipped else "missing"}
    return found


def evaluate(log_text: str, status: int, client: dict) -> tuple[list[str], list[str]]:
    """``(missing completion lines, problems)`` of one battery run."""

    _imports()
    from claude_multi import upgrade

    missing = list(upgrade._missing_evidence_prefixes(record_lines(log_text)))
    problems = []
    for name, disposition in skew_dispositions(log_text).items():
        if disposition["disposition"] == "missing":
            problems.append(f"the log shows no {name} outcome: neither its record nor its skipped test with the "
                            "reason (the battery runs with --verbose)")
    if status != 0:
        problems.append(f"the battery exited {status}")
    if not client.get("matches_contract"):
        problems.append("the client is not the shipped contract's pinned Claude Code for this platform")
    if missing:
        problems.append(f"{len(missing)} essential completion line(s) missing")
    if carries_fixture_secret(log_text):
        problems.append("the log carries a fixture secret; it is not kept")
    return missing, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evidence.py", description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--status", type=int, required=True)
    parser.add_argument("--claude", type=Path, required=True)
    parser.add_argument("--modules", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        log_text = args.log.read_text(encoding="utf-8", errors="replace")
        modules = [line.strip() for line in args.modules.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as exc:
        print(f"evidence: {exc}", file=sys.stderr)
        return 2
    client = client_identity(args.claude)
    missing, problems = evaluate(log_text, args.status, client)
    clean = not carries_fixture_secret(log_text)
    args.out.mkdir(parents=True, exist_ok=True)
    if clean:
        (args.out / "battery.log").write_text(log_text, encoding="utf-8")
    manifest = {
        "format": MANIFEST_FORMAT,
        "commit": commit(),
        "workflow_run": {name.removeprefix("GITHUB_").lower(): os.environ.get(name) for name in RUN_FIELDS},
        "client": client,
        **gateway_identity(dict(os.environ)),
        "modules": modules,
        "exit_status": args.status,
        "missing_completion_lines": missing,
        "client_skew": skew_dispositions(log_text) if clean else None,
        "log": {"file": "battery.log", "sha256": hashlib.sha256(log_text.encode("utf-8")).hexdigest()}
        if clean else {"file": None, "withheld": "the log carries a fixture secret"},
        "problems": problems,
        "verdict": "FAIL" if problems else "PASS",
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for problem in problems:
        print(f"evidence: {problem}", file=sys.stderr)
    for line in missing:
        print(f"evidence: missing: {line.rstrip()}", file=sys.stderr)
    print(f"evidence: {manifest['verdict']} ({len(modules)} modules, exit {args.status})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
