"""The diagnostic probes of a gateway built outside the Nix store (CI).

tests/gateway-startup-probe.nix builds the startup and omission diagnostics
against a store gateway: it copies the probe test files into the prepared
source tree and compiles them with ``go test -c``. This script does the same
for a ``tools/build.py gateway`` build, reading what to do from that
expression (the files it copies and the ``go test`` lines it runs), so both
build the same probes from the same prepared tree.

  python3 .github/scripts/gateway_probes.py compile --work WORK --dist DIST --out OUT
      WORK is the gateway build's work directory and DIST its output (with
      BUILD.json). The prepared tree must still be the one the gateway was
      built from (WORK's verified trees, and BUILD.json's post-apply tree
      hash), and BUILD.json must name this checkout's admitted patch series
      and the host target. The probes are compiled offline with the pinned
      toolchain and the vendored modules, in a copy of the tree, into
      OUT/bin; OUT/share/probe-build.json records the host gateway's sha256
      and target, the series and each probe's sha256, which is what
      tests/_gateway.py binds them by.
  python3 .github/scripts/gateway_probes.py provision --dist DIST --probes OUT
      after an artifact download (which drops file modes): make the host
      gateway and the probes executable and private, check them the way
      the tests will (tests/_gateway.py), and print the test variables that
      select them as NAME=value lines (for $GITHUB_ENV).

Nothing here downloads anything or runs a gateway.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"
EXPRESSION = TESTS / "gateway-startup-probe.nix"
BUILD_TOOL = REPO / "tools" / "build.py"
RECORD = Path("share") / "probe-build.json"
RECORD_FORMAT = 1

_COPY = re.compile(r"^\s*cp \$\{\./([\w.-]+)\} (\S+)\s*$")
_COMPILE = re.compile(r"^\s*(CGO_ENABLED=1 )?go test -mod=vendor (-race )?-c -o ([\w.-]+) (\./[\w./-]+)\s*$")


class ProbeError(Exception):
    pass


def recipe(text: str | None = None) -> tuple[list[tuple[str, str]], list[dict]]:
    """What tests/gateway-startup-probe.nix does: the probe files it copies
    into the tree ``(source under tests/, destination in the tree)`` and
    the probes it compiles ``{name, package, cgo, race}``, in order."""

    copies, probes = [], []
    for line in (EXPRESSION.read_text() if text is None else text).splitlines():
        copied = _COPY.match(line)
        if copied:
            copies.append((copied.group(1), copied.group(2)))
            continue
        compiled = _COMPILE.match(line)
        if compiled:
            probes.append({"name": compiled.group(3), "package": compiled.group(4),
                           "cgo": bool(compiled.group(1)), "race": bool(compiled.group(2))})
        elif re.search(r"\bgo test\b", line):
            raise ProbeError(f"{EXPRESSION.name}: a go test line this script cannot follow: {line.strip()}")
    if not copies or not probes:
        raise ProbeError(f"{EXPRESSION.name} names no probe files or no probes")
    if len({probe["name"] for probe in probes}) != len(probes):
        raise ProbeError(f"{EXPRESSION.name} builds two probes of one name")
    return copies, probes


def _build_tool():
    spec = importlib.util.spec_from_file_location("_cm_gateway_probes_build", BUILD_TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _gateway_module():
    if str(TESTS) not in sys.path:
        sys.path.insert(0, str(TESTS))
    if str(REPO / "src") not in sys.path:
        sys.path.insert(0, str(REPO / "src"))
    import _gateway

    return _gateway


def _load(path: Path, what: str) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProbeError(f"{what} {path} cannot be read ({exc})") from exc
    if not isinstance(document, dict):
        raise ProbeError(f"{what} {path} is not a JSON object")
    return document


def host_gateway(dist: Path, gateway) -> tuple[Path, dict, str]:
    """The host target's gateway in DIST: ``(binary, BUILD.json, target)``,
    checked against the record and this checkout's admitted series."""

    record, problem = gateway.build_record_problem(str(dist / "BUILD.json"))
    if record is None:
        raise ProbeError(problem)
    target = gateway.host_target()
    entry = (record.get("targets") or {}).get(target) if target else None
    if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
        raise ProbeError(f"{dist / 'BUILD.json'} names no {target} gateway")
    binary = dist / entry["file"]
    if not binary.is_file() or gateway.sha256_file(binary) != entry.get("sha256"):
        raise ProbeError(f"{binary} is not the {target} gateway BUILD.json names")
    return binary, record, target


def compile_probes(work: Path, dist: Path, out: Path) -> dict:
    gateway = _gateway_module()
    build = _build_tool()
    copies, probes = recipe()
    binary, record, target = host_gateway(dist, gateway)
    ctx = build.Context(build.parse_args(["gateway", "env", "--work", str(work)]))
    try:
        state = ctx.load_state("apply")
        build.verify_prepared_trees(ctx, state, "probes")
    except build.BuildError as exc:
        raise ProbeError(str(exc)) from exc
    if record.get("post_apply_tree_sha256") != state["post_apply_tree_sha256"]:
        raise ProbeError(f"{work} holds another prepared tree than the one {dist / 'BUILD.json'} was built from")
    if out.exists():
        raise ProbeError(f"{out} exists; give a new directory")
    (out / "bin").mkdir(parents=True)
    (out / RECORD.parent).mkdir(parents=True)
    scratch = Path(tempfile.mkdtemp(prefix="gateway-probes-", dir=work))
    try:
        tree = scratch / "src"
        shutil.copytree(ctx.src, tree, symlinks=True)
        for source, destination in copies:
            target_path = tree / destination
            if target_path.exists():
                raise ProbeError(f"the prepared tree already has {destination}")
            shutil.copyfile(TESTS / source, target_path)
        built = {}
        for probe in probes:
            environment = build.go_environment(ctx.work, cgo=probe["cgo"])
            if not probe["cgo"]:
                environment.update(ctx.doc["build"]["env"])
            argv = [ctx.go, "test", "-mod=vendor", *(["-race"] if probe["race"] else []), "-c",
                    "-o", str(out / "bin" / probe["name"]), probe["package"]]
            print(f"gateway_probes: compiling {probe['name']} ({probe['package']})", file=sys.stderr, flush=True)
            if subprocess.run(argv, cwd=tree, env=environment, check=False).returncode:
                raise ProbeError(f"go test -c failed for {probe['name']}")
            path = out / "bin" / probe["name"]
            os.chmod(path, 0o755)
            built[probe["name"]] = gateway.sha256_file(path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    document = {"format": RECORD_FORMAT, "gateway_sha256": gateway.sha256_file(binary), "gateway_target": target,
                "series": record["series"], "probes": built}
    (out / RECORD).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return document


def _private(path: Path, mode: int) -> None:
    if path.is_symlink() or not (path.is_file() or path.is_dir()):
        raise ProbeError(f"{path} is not a regular file or directory")
    os.chmod(path, mode)


def provision(dist: Path, probes: Path) -> dict[str, str]:
    """The test variables selecting DIST's host gateway and its probes."""

    gateway = _gateway_module()
    _, names = recipe()
    record = _load(probes / RECORD, "the probe record")
    for directory in (dist, probes, probes / "bin", probes / RECORD.parent):
        _private(directory, 0o700)
    _private(dist / "BUILD.json", 0o600)
    _private(probes / RECORD, 0o600)
    binary, _build, target = host_gateway(dist, gateway)
    _private(binary.parent, 0o700)
    _private(binary, 0o700)
    if record.get("gateway_target") != target:
        raise ProbeError(f"the probes were built for {record.get('gateway_target')}, this host is {target}")
    for probe in names:
        _private(probes / "bin" / probe["name"], 0o700)
    bound, problem = gateway.bound_gateway_problem(str(binary), str(dist / "BUILD.json"))
    if bound is None:
        raise ProbeError(problem)
    for probe in names:
        problem = gateway.diagnostic_problem(Path(os.path.realpath(probes / "bin" / probe["name"])), bound)
        if problem is not None:
            raise ProbeError(f"{probe['name']}: {problem}")
    return {gateway.BINARY_ENV: bound, gateway.BUILD_RECORD_ENV: str((dist / "BUILD.json").resolve()),
            "CLAUDE_MULTI_TEST_GATEWAY_STARTUP_PROBE": str((probes / "bin" / names[0]["name"]).resolve())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway_probes.py", description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    compile_cmd = commands.add_parser("compile", help="compile the probes against a tools/build.py gateway build")
    compile_cmd.add_argument("--work", type=Path, required=True, help="the gateway build's work directory")
    compile_cmd.add_argument("--dist", type=Path, required=True, help="the gateway build's output (BUILD.json)")
    compile_cmd.add_argument("--out", type=Path, required=True, help="a new directory for bin/ and share/")
    provision_cmd = commands.add_parser("provision", help="check a downloaded gateway and probes; print the test variables")
    provision_cmd.add_argument("--dist", type=Path, required=True, help="the gateway build's output (BUILD.json)")
    provision_cmd.add_argument("--probes", type=Path, required=True, help="the compiled probes (bin/, share/)")
    args = parser.parse_args(argv)
    try:
        if args.command == "compile":
            document = compile_probes(args.work.resolve(), args.dist.resolve(), args.out.resolve())
            print(json.dumps(document, indent=2, sort_keys=True))
        else:
            for name, value in provision(args.dist.resolve(), args.probes.resolve()).items():
                if "\n" in value:
                    raise ProbeError(f"{name} would span lines")
                print(f"{name}={value}")
    except ProbeError as exc:
        print(f"gateway_probes: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
