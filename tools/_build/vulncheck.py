#!/usr/bin/env python3
"""Check shipped gateway binaries against individually reviewed dispositions.

Binary-mode govulncheck findings are not proof of reachable affected code:
stripped binaries can fall back to module matching. Preserve the raw scanner
output, and distinguish reviewed dispositions from unreviewed findings.
Dispositions bind exact gateway hashes, module versions and reported traces.
Advisory-only revision changes warn; scanner/process/database errors and incomplete
output fail closed. Exit 0 only after every target passes, 1 for a failed check,
2 for usage errors. Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Sequence

GATEWAY = "libexec/claude-multi/cli-proxy-api"
TARGETS = {"linux-x86_64", "linux-aarch64", "darwin-x86_64", "darwin-arm64"}
CLASSES = {"component-absent", "function-not-called", "feature-excluded"}
TIMEOUT = 900
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


class CheckError(Exception):
    pass


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"{argv[0]} could not run: {exc}") from exc


def tool_identity(tool: str) -> list[str]:
    result = _run([tool, "-version"])
    if result.returncode != 0:
        raise CheckError(f"govulncheck -version failed: {(result.stderr or result.stdout).strip()[:300]}")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not any(line.startswith("DB: ") for line in lines):
        raise CheckError("govulncheck -version names no vulnerability database")
    return lines


def require(condition: Any, reason: str) -> None:
    if not condition:
        raise CheckError(reason)


def text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "*" not in value


def digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def revision(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def trace_identity(traces: list[list[dict[str, Any]]]) -> dict[str, Any]:
    # Order and repeated frames in separate findings are not exposure changes.
    # fixed_version is advisory metadata, not part of a finding's trace.
    unique = sorted({json.dumps(trace, sort_keys=True, separators=(",", ":")) for trace in traces})
    return {"trace_count": len(unique), "trace_sha256": canonical_sha256([json.loads(trace) for trace in unique])}


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def dispositions(path: Path | None) -> dict[str, Any]:
    require(path is not None, "a disposition file is required")
    policy = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    require(isinstance(policy, dict) and set(policy) == {"format", "gateway", "dispositions"}
            and policy["format"] == 1, "malformed disposition document")
    gateway = policy["gateway"]
    require(isinstance(gateway, dict) and set(gateway) == {"version", "targets"}
            and text(gateway["version"]), "malformed gateway identity")
    targets = gateway["targets"]
    require(isinstance(targets, dict) and set(targets) == TARGETS
            and all(digest(value) for value in targets.values()), "missing or malformed gateway target hashes")
    entries = policy["dispositions"]
    require(isinstance(entries, list) and entries, "missing dispositions")
    ids = set()
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {
            "id", "module", "version", "advisory_modified", "advisory_sha256", "class", "reason",
            "evidence", "re_review", "trace_sha256", "trace_count"}, "malformed disposition entry")
        identifier = entry["id"]
        require(isinstance(identifier, str) and re.fullmatch(r"GO-\d{4}-\d{4,}", identifier)
                and identifier not in ids, "invalid or duplicate disposition id")
        ids.add(identifier)
        require(text(entry["module"]) and text(entry["version"])
                and revision(entry["advisory_modified"]) and digest(entry["advisory_sha256"])
                and entry["class"] in CLASSES and text(entry["reason"])
                and digest(entry["trace_sha256"]) and type(entry["trace_count"]) is int
                and entry["trace_count"] > 0, f"malformed disposition: {identifier}")
        for key in ("evidence", "re_review"):
            require(isinstance(entry[key], list) and entry[key] and all(text(item) for item in entry[key]),
                    f"missing {key}: {identifier}")
    return policy


def findings(stream: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete successful scan and apply exact per-id bindings.

    The protocol has no end marker. In addition to successful process exit,
    require config, SBOM, progress and every previously reviewed finding. A
    disappeared finding is a re-review event, not silently accepted truncation.
    """

    decoder = json.JSONDecoder(object_pairs_hook=unique_object)
    remaining = stream.strip()
    config, sbom, progress = None, None, False
    advisories: dict[str, Any] = {}
    raw = []
    while remaining:
        message, position = decoder.raw_decode(remaining)
        remaining = remaining[position:].lstrip()
        require(isinstance(message, dict) and len(message) == 1, "malformed scanner message")
        kind, value = next(iter(message.items()))
        require(isinstance(value, dict), f"malformed {kind} message")
        if kind == "config":
            require(config is None and value.get("protocol_version") == "v1.0.0"
                    and value.get("scanner_name") == "govulncheck" and value.get("scanner_version") == "v1.8.0"
                    and value.get("scan_mode") == "binary" and value.get("scan_level") == "symbol"
                    and text(value.get("db")) and revision(value.get("db_last_modified")), "invalid scanner config")
            config = value
        elif kind == "SBOM":
            require(sbom is None and text(value.get("go_version")) and isinstance(value.get("modules"), list)
                    and value["modules"], "invalid scanner SBOM")
            sbom = {}
            for module in value["modules"]:
                require(isinstance(module, dict) and text(module.get("path")) and text(module.get("version"))
                        and module["path"] not in sbom, "invalid scanner module")
                sbom[module["path"]] = module["version"]
        elif kind == "progress":
            require(text(value.get("message")), "invalid scanner progress")
            progress = True
        elif kind == "osv":
            identifier = value.get("id")
            require(text(identifier) and revision(value.get("modified")), "invalid scanner advisory")
            require(identifier not in advisories or advisories[identifier] == value, "conflicting scanner advisories")
            advisories[identifier] = value
        elif kind == "finding":
            trace = value.get("trace")
            require(text(value.get("osv")) and isinstance(trace, list) and trace
                    and all(isinstance(frame, dict) for frame in trace)
                    and text(trace[0].get("module")) and text(trace[0].get("version")), "invalid scanner finding")
            raw.append(value)
        else:
            raise CheckError(f"unknown scanner message: {kind}")
    require(config is not None and sbom is not None and progress, "incomplete scanner output")
    reviewed, unreviewed = {}, set()
    entries = {entry["id"]: entry for entry in policy["dispositions"]}
    ids = {finding["osv"] for finding in raw}
    problems, warnings, revisions = [], [], []
    observed_traces = {identifier: trace_identity([finding["trace"] for finding in raw if finding["osv"] == identifier])
                       for identifier in ids}
    for identifier, entry in entries.items():
        if identifier not in ids:
            problems.append(f"missing reviewed finding {identifier}: incomplete scan or re-review required")
        if sbom.get(entry["module"]) != entry["version"]:
            problems.append(f"module version mismatch: {identifier}")
        advisory = advisories.get(identifier)
        if advisory is not None:
            current = canonical_sha256(advisory)
            revisions.append({"id": identifier, "reviewed_modified": entry["advisory_modified"],
                              "current_modified": advisory["modified"], "reviewed_sha256": entry["advisory_sha256"],
                              "current_sha256": current})
            if advisory["modified"] != entry["advisory_modified"] or current != entry["advisory_sha256"]:
                warnings.append(f"{identifier}: advisory revision changed; reviewed {entry['advisory_modified']} "
                                f"({entry['advisory_sha256']}), current {advisory['modified']} ({current}); "
                                "gateway, module and reported trace bindings remain required")
    for finding in raw:
        identifier, frame = finding["osv"], finding["trace"][0]
        entry, advisory = entries.get(identifier), advisories.get(identifier)
        if not entry:
            unreviewed.add(identifier)
            continue
        if (advisory is None
                or observed_traces[identifier] != {key: entry[key] for key in ("trace_sha256", "trace_count")}
                or sbom.get(entry["module"]) != entry["version"]
                or (frame["module"], frame["version"]) != (entry["module"], entry["version"])):
            unreviewed.add(identifier)
            problems.append(f"missing advisory or finding binding mismatch: {identifier}")
        else:
            reviewed[identifier] = entry
    for identifier in unreviewed:
        reviewed.pop(identifier, None)
    return {"findings": sorted(ids), "raw_findings": raw, "reviewed_dispositions": list(reviewed.values()),
            "unreviewed_findings": sorted(unreviewed), "problems": sorted(set(problems)),
            "advisory_revisions": revisions, "warnings": warnings, "observed_traces": observed_traces}


def gateways(dist: Path, work: Path, policy: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = json.loads((dist / "MANIFEST.json").read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    assets = manifest.get("assets") if isinstance(manifest, dict) else None
    require(isinstance(assets, dict) and assets, f"{dist / 'MANIFEST.json'} lists no bundles")
    require(manifest.get("gateway", {}).get("version") == policy["gateway"]["version"],
            "gateway version mismatch")
    require(all(isinstance(asset, dict) and asset.get("target") in TARGETS for asset in assets.values())
            and len(assets) == len(TARGETS) and {asset["target"] for asset in assets.values()} == TARGETS,
            "missing, duplicate or unexpected gateway target")
    found = []
    for name, asset in sorted(assets.items()):
        require(Path(name).name == name and name.endswith(".tar.gz"), "invalid bundle name")
        top = name.removesuffix(".tar.gz")
        member = f"{top}/{GATEWAY}"
        destination = work / asset["target"] / "cli-proxy-api"
        destination.parent.mkdir(parents=True)
        try:
            with tarfile.open(dist / name, "r:gz") as archive:
                members = [item for item in archive.getmembers() if item.name == member]
                require(len(members) == 1 and members[0].isfile(), f"{name} has no unique regular {GATEWAY}")
                source = archive.extractfile(members[0])
                with source, open(destination, "wb") as out:
                    shutil.copyfileobj(source, out)
        except (OSError, KeyError, tarfile.TarError) as exc:
            raise CheckError(f"{name}: {GATEWAY} cannot be read ({exc})") from exc
        actual = hashlib.sha256(destination.read_bytes()).hexdigest()
        require(actual == asset.get("gateway_sha256"), f"{name}: the gateway's sha256 is not the manifest's")
        require(actual == policy["gateway"]["targets"][asset["target"]], f"{name}: disposition gateway hash mismatch")
        found.append({"target": asset["target"], "archive": name, "path": destination, "sha256": actual})
    return found


def check(dist: Path, tool: str | None, disposition_file: Path | None = None) -> tuple[dict[str, Any], bool]:
    report: dict[str, Any] = {"format": 2, "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"), "tool": None, "targets": [], "problems": []}
    try:
        policy = dispositions(disposition_file)
        report["dispositions"] = policy
        executable = shutil.which(tool or "govulncheck")
        require(executable is not None, "govulncheck is not installed; no binary was checked")
        report["tool"] = tool_identity(executable)
        ok = True
        with tempfile.TemporaryDirectory(prefix="vulncheck-") as root:
            binaries = gateways(dist, Path(root), policy)
            for binary in binaries:
                entry = {key: binary[key] for key in ("target", "archive", "sha256")}
                try:
                    result = _run([executable, "-mode=binary", "-format=json", str(binary["path"])])
                    entry.update(scanner_stdout=result.stdout, scanner_stderr=result.stderr, scanner_exit=result.returncode)
                    require(result.returncode == 0, f"govulncheck exited {result.returncode}: "
                            f"{(result.stderr or result.stdout).strip()[:300]}")
                    entry.update(findings(result.stdout, policy))
                    passed = not entry["unreviewed_findings"] and not entry["problems"]
                    entry["result"] = "reviewed" if passed else "unreviewed"
                    ok = ok and passed
                except (CheckError, ValueError, TypeError, KeyError) as exc:
                    entry.update(result="error", error=str(exc))
                    ok = False
                report["targets"].append(entry)
        return report, ok
    except (CheckError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        report["problems"].append(str(exc))
        return report, False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vulncheck.py", description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--dist", required=True, type=Path, help="the release directory (MANIFEST.json, bundles)")
    parser.add_argument("--out", required=True, type=Path, help="the evidence file to write")
    parser.add_argument("--dispositions", type=Path, help="reviewed disposition file from the resolved source commit")
    parser.add_argument("--govulncheck", help="the govulncheck executable (default: from PATH)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    report, ok = check(args.dist, args.govulncheck, args.dispositions)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    for entry in report["targets"]:
        print(f"{entry['target']}: {entry['result']} ({entry['sha256'][:16]}…) "
              f"reviewed dispositions: {len(entry.get('reviewed_dispositions', []))}; "
              f"unreviewed findings: {len(entry.get('unreviewed_findings', []))}")
        for warning in entry.get("warnings", []):
            print(f"vulncheck: WARNING {entry['target']}: {warning}", file=sys.stderr)
    for problem in report["problems"]:
        print(f"vulncheck: {problem}", file=sys.stderr)
    if not ok:
        print("vulncheck: FAILED (an unreviewed finding, or a check that could not run)", file=sys.stderr)
    return EXIT_OK if ok else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
