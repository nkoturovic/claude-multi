#!/usr/bin/env python3
"""Patch-omission proof; git-tracked files only.

No host Go execution, provider calls, or activation. All test executables run
through the harness's bwrap network namespaces. Builds are comparable scratch
Nix derivations with doCheck=false; the shipped derivation is never changed.
An omission build applies the rest of the admitted series with full context;
only the omitted patches' line shifts may move a later hunk (build.py's
--omission-offsets), and each variant records the hunks that moved.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest

# Run as a script, the tests directory is sys.path[0]; imported, it is on
# PYTHONPATH. Either way _layout is the one root source.
from _layout import REPO_ROOT, RESOURCES_ROOT  # noqa: E402

sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "src"), str(REPO_ROOT / "tests")]
from claude_multi import state
import _gateway_harness as harness


def test_ids():
    return tuple(dict.fromkeys(name for patch, names in harness.PATCH_ROWS.items()
                               for name in (*names, *harness.PATCH_CONTROL_ROWS.get(patch, ()))))


def boundary_error(err):
    """A REQUIRE=1 harness boundary (isolation, startup probe, disk) is no row
    verdict: the variant could not run, so it can never count as red."""
    return str(err[1]).startswith(harness.REQUIRE_ENV + "=1:")


class RowResult(unittest.TextTestResult):
    """Keep structured outcomes, including fixture errors and failing subtests."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {name: "not_run" for name in test_ids()}

    def mark(self, test, value):
        name = test.id()
        if name in self.outcomes:
            self.outcomes[name] = value
        else:
            # unittest attributes setUpClass/tearDownModule errors to holders,
            # not rows. Account for every affected row, never lose an ERROR.
            match = re.fullmatch(r"\w+ \((.+)\)", name)
            if match:
                for row in self.outcomes:
                    if row.startswith(match.group(1) + "."):
                        self.outcomes[row] = value

    def addSuccess(self, test):
        super().addSuccess(test)
        self.mark(test, "ok")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.mark(test, "blocked" if boundary_error(err) else "FAIL")

    def addError(self, test, err):
        super().addError(test, err)
        self.mark(test, "blocked" if boundary_error(err) else "ERROR")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.mark(test, "skipped")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.mark(test, "blocked" if boundary_error(err) else
                      "FAIL" if issubclass(err[0], test.failureException) else "ERROR")


def run_rows(destination):
    # Keep failure traces and captures out of persisted metadata. The individual
    # rows can be rerun with unittest -v for diagnostics.
    stream = io.StringIO()
    try:
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            suite = unittest.defaultTestLoader.loadTestsFromNames(test_ids())
            result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RowResult).run(suite)
        state.atomic_write(destination, (json.dumps({"outcomes": result.outcomes,
            "successful": result.wasSuccessful(), "ran": result.testsRun}) + "\n").encode())
        return 0 if result.wasSuccessful() and all(v == "ok" for v in result.outcomes.values()) else 1
    finally:
        harness.close_main_harness()


def verdict(outcomes, own):
    red = {name for name, value in outcomes.items() if value in ("FAIL", "ERROR")}
    return {"own_red": sorted(red & set(own)),
            "own_green": sorted(name for name in own if outcomes.get(name) == "ok"),
            "cross_red": sorted(red - set(own)),
            "incomplete": sorted(name for name in test_ids() if outcomes.get(name) not in ("ok", "FAIL", "ERROR"))}


def omission_closure(patch, manifest, dependencies):
    """A missing prerequisite also removes all its declared dependents."""
    if set(dependencies) - set(manifest) or any(set(edges) - set(manifest) for edges in dependencies.values()):
        raise ValueError("dependency metadata names a patch outside the manifest")
    # A dependency cycle has no independent difference control; reject it.
    def visit(name, trail):
        if name in trail:
            raise ValueError("cyclic patch dependencies")
        for prerequisite in dependencies.get(name, {}):
            visit(prerequisite, trail | {name})
    for name in manifest:
        visit(name, set())
    missing = {patch}
    while True:
        grown = missing | {name for name, edges in dependencies.items() if set(edges) & missing}
        if grown == missing:
            return tuple(name for name in manifest if name in missing)
        missing = grown


def attribute(variant, patch, dependent_control):
    """Only a row red here AND green without the dependents proves the target."""
    summary = verdict(variant["outcomes"], harness.PATCH_ROWS[patch])
    candidates = summary["own_red"]
    if dependent_control is not None:
        summary["own_red"] = [name for name in candidates if dependent_control["outcomes"].get(name) == "ok"]
    summary["unattributed_own_red"] = sorted(set(candidates) - set(summary["own_red"]))
    expected = {name for omitted in variant["omitted"] if omitted != patch for name in harness.PATCH_ROWS[omitted]}
    summary["expected_dependency_cross_red"] = sorted(set(summary["cross_red"]) & expected)
    summary["cross_red"] = sorted(set(summary["cross_red"]) - expected)
    return summary


def proof_passed(proof, row_returncode):
    """Discriminating: own rows red, every other row green except the declared
    dependency rows. A globally red variant (broken build, harness
    trouble) is never coverage."""
    return (bool(proof["own_red"]) and not proof["incomplete"] and not proof["cross_red"]
            and row_returncode == 1)


# The gateway's install step a diagnostic omission runs instead of the
# shipped one: inspect refuses a shipped target built from a partial series
# by design, while record still names that partial series in BUILD.json.
# Both omission tools (this one and check_gateway_races) use it.
SHIPPED_INSTALL_STEP = "gatewayBuild inspect record"
DIAGNOSTIC_INSTALL_PHASE = ('installPhase = builtins.replaceStrings [ "%s" ] [ "gatewayBuild record" ] '
                            'old.installPhase;' % SHIPPED_INSTALL_STEP)
# The patch step of an omission build: the rest of the admitted series still
# applies with its full context, but a later hunk may sit at a line offset
# left by the omitted patches (build.py's --omission-offsets, which refuses
# the admitted series, so the control and every shipped build stay
# offset-free). BUILD.json names each moved hunk. Asserted to have replaced.
SHIPPED_APPLY_STEP = "gatewayBuild apply "
DIAGNOSTIC_PATCH_PHASE = ('patchPhase = let phase = builtins.replaceStrings [ "%s" ] [ "%s--omission-offsets " ] '
                          'old.patchPhase; in assert phase != old.patchPhase; phase;'
                          % (SHIPPED_APPLY_STEP, SHIPPED_APPLY_STEP))


def build_expression(worktree, omitted):
    nix_names = "[ " + " ".join(json.dumps(name) for name in omitted) + " ]"
    return '''let
      f = builtins.getFlake %s;
      pkgs = f.inputs.nixpkgs.legacyPackages.x86_64-linux;
      original = import "${f}/nix/gateway.nix" { inherit pkgs; };
      gateway = original.overrideAttrs (old: {
        patches = builtins.filter (p: !(builtins.elem (builtins.baseNameOf (toString p)) %s)) old.patches;
        doCheck = false;
        %s
        %s
      });
    in import "${f}/tests/gateway-startup-probe.nix" { cliProxyApi = gateway; }
    ''' % (json.dumps(str(worktree)), nix_names, DIAGNOSTIC_INSTALL_PHASE,
           DIAGNOSTIC_PATCH_PHASE if omitted else "")


def moved_hunks(gateway):
    """The hunks an omission moved, from the diagnostic gateway's BUILD.json."""
    record = json.loads((Path(gateway) / "share/cli-proxy-api/BUILD.json").read_text())
    return record.get("moved_hunks", {})


def git_provenance(worktree):
    prefix = ["git", "-C", str(worktree)]
    head = subprocess.check_output([*prefix, "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output([*prefix, "status", "--porcelain", "--untracked-files=all"], text=True)
    return {"head": head, "dirty": bool(status), "status_porcelain": status.splitlines()}


def prove(destination, patches=None):
    worktree = REPO_ROOT
    state.ensure_private_dir(destination)
    report = {"schema": "gwtest-patch-revert-v2", "git_tracked_only": True,
              "worktree": str(worktree), "patch_rows": harness.PATCH_ROWS,
              "dependencies": harness.PATCH_DEPENDENCIES, "variants": [],
              "passed": False}
    began = time.monotonic()

    def save():
        report["duration_seconds"] = round(time.monotonic() - began, 3)
        state.atomic_write(destination / "patch-revert.json",
                           (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())

    # Shipped read is intentional in this re-pin tool, never a behavioral ID.
    manifest = json.loads((RESOURCES_ROOT / "catalog/gateway.json").read_text())["gateway"]["patches"]
    if set(manifest) != set(harness.PATCH_ROWS) or len(manifest) != len(set(manifest)):
        raise AssertionError("manifest and PATCH_ROWS differ")
    report.update(git_provenance(worktree))
    if report["dirty"]:
        print("Revert proof uses a dirty tree: HEAD alone does not identify the tested code; "
              "see status_porcelain in patch-revert.json.", file=sys.stderr)
    save()
    selected = tuple(dict.fromkeys(patches)) if patches else tuple(manifest)
    if set(selected) - set(manifest):
        raise ValueError("selected patch is outside the admitted manifest")
    report["selected_patches"] = list(selected)
    report["control_rows"] = harness.PATCH_CONTROL_ROWS
    closures = {patch: omission_closure(patch, manifest, harness.PATCH_DEPENDENCIES) for patch in selected}
    # Include a dependents-only control even when it is not already another
    # patch's omission. Dict insertion order deduplicates those build sets.
    plans = dict.fromkeys([(), *closures.values(),
        *(tuple(name for name in names if name != patch) for patch, names in closures.items())])
    by_omission = {}
    for omitted in plans:
        start = time.monotonic()
        variant = {"omitted": list(omitted), "build": "pending"}
        by_omission[omitted] = variant
        report["variants"].append(variant)
        print("Building " + (", ".join(omitted) or "control"), flush=True)
        try:
            built = subprocess.run(["nix", "build", "--offline", "--no-link", "--print-out-paths",
                "--impure", "--max-jobs", "1", "--expr", build_expression(worktree, omitted)],
                cwd=worktree, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1200)
            variant["build_seconds"] = round(time.monotonic() - start, 3)
            if built.returncode:
                variant.update(build="failed", returncode=built.returncode,
                    failed_derivations=sorted(set(re.findall(r"/nix/store/[^\s'\"]+\.drv", built.stderr))))
                print(built.stderr[-6000:], file=sys.stderr)
                save()
                return 2  # BLOCKED; never invent an undeclared dependency.
            paths = [line for line in built.stdout.splitlines() if line.startswith("/nix/store/")]
            if len(paths) != 1:
                raise RuntimeError("Nix did not return exactly one diagnostic outPath")
            diagnostic = Path(paths[0])
            # Keep every completed variant's closure live across later builds.
            roots = destination / "roots"
            roots.mkdir(exist_ok=True)
            subprocess.run(["nix-store", "--add-root", str(roots / str(len(report["variants"]))),
                            "--indirect", "--realise", str(diagnostic)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            gateway = (diagnostic / "share/gateway-outpath").read_text().strip()
            variant.update(build="ok", gateway=gateway, diagnostic=str(diagnostic),
                           go_modules_consumed=harness.consumed_vendor(diagnostic),
                           moved_hunks=moved_hunks(gateway))
            if variant["moved_hunks"] and not omitted:
                raise RuntimeError("the control moved a hunk: the admitted series never applies with offsets")
            if not harness.vendor_pinned(variant["go_modules_consumed"]):
                # A forked vendor FOD is a different build, never evidence.
                raise RuntimeError("diagnostic does not consume the pinned vendor modules (F17)")
            env = {**os.environ, harness.BINARY_ENV: gateway + "/bin/cli-proxy-api",
                   harness.STARTUP_PROBE_ENV: str(diagnostic / "bin/gwtest-startup-probe"),
                   harness.REQUIRE_ENV: "1", "PYTHONPATH": str(REPO_ROOT / "src") + ":" + str(REPO_ROOT / "tests")}
            # Partial row runs cannot satisfy the full module evidence inventory.
            env.pop(harness.EVIDENCE_ENV, None)
            with tempfile.TemporaryDirectory(prefix="gwtest-revert-") as temporary:
                rows_path = Path(temporary) / "rows.json"
                run_start = time.monotonic()
                run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--run-rows", str(rows_path)],
                    cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
                variant["row_seconds"] = round(time.monotonic() - run_start, 3)
                variant["row_returncode"] = run.returncode
                rows = json.loads(rows_path.read_text())
            variant.update(rows)
            variant["incomplete"] = verdict(rows["outcomes"], ())["incomplete"]
            save()
            if "blocked" in rows["outcomes"].values():
                print("Revert proof blocked: a harness boundary stopped rows of "
                      + (", ".join(omitted) or "control"), file=sys.stderr)
                return 2  # BLOCKED; a variant that could not run proves nothing.
            if not omitted and (run.returncode != 0 or not rows["successful"] or
                                any(value != "ok" for value in rows["outcomes"].values())):
                return 1
            print(f"{', '.join(omitted) or 'control'}: rows finished, rc={run.returncode}", flush=True)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            variant.update(build="failed" if variant["build"] == "pending" else variant["build"],
                           error=type(exc).__name__)
            save()
            print(f"Revert proof blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    proofs = {}
    for patch, omitted in closures.items():
        variant = by_omission[omitted]
        dependents = tuple(name for name in omitted if name != patch)
        proof = attribute(variant, patch, by_omission[dependents] if dependents else None)
        proof.update(omitted=list(omitted), dependent_control=list(dependents),
                     passed=proof_passed(proof, variant["row_returncode"]))
        proofs[patch] = proof
        print(f"{patch}: passed={proof['passed']} own_red={len(proof['own_red'])} "
              f"cross_red={len(proof['cross_red'])} dependency_cross_red={len(proof['expected_dependency_cross_red'])}", flush=True)
    report["proofs"] = proofs
    report["passed"] = all(proof["passed"] for proof in proofs.values())
    save()
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path.home() / ".cache/claude-multi-gateway-revert" /
                        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--patch", action="append", help="prove this patch's closure (repeatable; default: every patch); all rows still run")
    parser.add_argument("--run-rows", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    return run_rows(args.run_rows) if args.run_rows else prove(args.out, args.patch)


if __name__ == "__main__":
    raise SystemExit(main())
