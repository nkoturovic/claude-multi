#!/usr/bin/env python3
"""Run the claude-multi test suite from a checkout, without Nix.

  python3 tools/test.py                      the whole suite
  python3 tools/test.py tests.test_trust     named modules (also test_trust, tests/test_trust.py)
  python3 tools/test.py --tier fast -k fork  quicker iteration, name filter
  python3 tools/test.py --list               what would run on this platform

What it guarantees on every platform (Python 3.11 or newer):

- The live-gateway tripwire is armed in this process (importing the
  ``tests`` package does that) and in every Python child process (a
  ``sitecustomize`` guard on PYTHONPATH that logs and refuses connects to
  the live gateway ports). Any hit fails the run.
- The suite sees a private temporary HOME and TMPDIR, and no XDG or
  ``CLAUDE_CONFIG_DIR`` overrides, so it never reads or writes your own
  claude-multi or Claude Code state (``--home inherit`` opts out).
- Platform markers: a test module may declare ``PLATFORMS = ("linux",)``
  (any of linux, darwin, windows). It is read without importing the module,
  and on other platforms the module is listed as not run, never imported.
- An empty selection fails; ``--require PATTERN`` makes matching tests
  mandatory (a skip fails the run) and ``--max-skip-ratio`` bounds skips.
- The real-client lanes need the pinned Claude Code, which the private HOME
  does not have: ``--claude PATH`` verifies a file against the packaged pin
  and puts a copy in the private HOME's claude-multi location (the
  product's own acquisition). A lane that needs it (the keyed client lane
  once ``CLAUDE_MULTI_TEST_CLI_PROXY_API`` names a gateway) without it is
  reported as not runnable, with the reason, and the run fails.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLATFORM_NAMES = ("linux", "darwin", "windows")
MIN_PYTHON = (3, 11)
CLEARED_ENV = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
               "CLAUDE_CONFIG_DIR", "CLAUDE_MULTI_ASSETS", "CLAUDE_MULTI_MANAGED_ID", "CLAUDECODE",
               "PYTHONHOME", "PYTHONSTARTUP")
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
# Test modules that need the pinned Claude Code once the named variable is
# set (it selects their real-client lane).
PINNED_CLIENT_LANES = {"tests.test_keyed_compat_client": "CLAUDE_MULTI_TEST_CLI_PROXY_API"}


class UsageError(Exception):
    pass


def current_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    return sys.platform


def module_platforms(path: Path) -> tuple[str, ...] | None:
    """The module's ``PLATFORMS`` marker, read with ``ast`` (never imported)."""

    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PLATFORMS" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError as exc:
                raise UsageError(f"{path.name}: PLATFORMS must be a literal tuple of names") from exc
            if (not isinstance(value, (tuple, list)) or not value
                    or any(name not in PLATFORM_NAMES for name in value)):
                raise UsageError(f"{path.name}: PLATFORMS must name some of {', '.join(PLATFORM_NAMES)}")
            return tuple(value)
    return None


def _module_name(raw: str) -> str:
    text = raw.replace("\\", "/")
    if text.endswith(".py"):
        text = text[:-3]
    text = text.replace("/", ".")
    if not text.startswith("tests."):
        text = "tests." + text
    if not re.fullmatch(r"tests\.test_\w+", text):
        raise UsageError(f"not a test module: {raw}")
    return text


def select_modules(tests_dir: Path, names: list[str], platform_name: str) -> tuple[list[str], list[tuple[str, tuple[str, ...]]]]:
    if names:
        modules = sorted(dict.fromkeys(_module_name(name) for name in names))
    else:
        modules = [f"tests.{path.stem}" for path in sorted(tests_dir.glob("test_*.py"))]
    selected: list[str] = []
    excluded: list[tuple[str, tuple[str, ...]]] = []
    for module in modules:
        path = tests_dir / f"{module.split('.', 1)[1]}.py"
        if not path.is_file():
            raise UsageError(f"no such test module: {module}")
        platforms = module_platforms(path)
        if platforms is not None and platform_name not in platforms:
            excluded.append((module, platforms))
        else:
            selected.append(module)
    return selected, excluded


def guard_source(tests_dir: Path) -> str:
    """The child-process tripwire (``GUARD_SOURCE`` of the isolation check)."""

    path = tests_dir / "check_fixture_isolation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "GUARD_SOURCE" for t in node.targets):
            return ast.literal_eval(node.value)
    raise UsageError(f"{path} defines no GUARD_SOURCE")


def prepare_environment(work: Path, *, repo: Path, home: str, tier: str) -> Path:
    """Set this process's environment (inherited by every child); returns the guard log."""

    guard_dir = work / "guard"
    guard_dir.mkdir()
    (guard_dir / "sitecustomize.py").write_text(guard_source(repo / "tests"), encoding="utf-8")
    guard_log = work / "guard.log"
    for name in CLEARED_ENV:
        os.environ.pop(name, None)
    tmp = work / "tmp"
    tmp.mkdir()
    os.environ["TMPDIR"] = str(tmp)
    tempfile.tempdir = None  # re-read TMPDIR
    if home == "temp":
        private_home = work / "home"
        private_home.mkdir()
        os.environ["HOME"] = str(private_home)
        if current_platform() == "windows":
            os.environ["USERPROFILE"] = str(private_home)
    os.environ["CM_ISOLATION_GUARD_LOG"] = str(guard_log)
    os.environ["CM_TEST_TIER"] = tier
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONPATH"] = os.pathsep.join([str(guard_dir), str(repo / "src"), str(repo / "tests")])
    for entry in (str(repo / "tests"), str(repo / "src"), str(repo)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return guard_log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools/test.py", description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("modules", nargs="*", help="test modules (default: every tests/test_*.py)")
    parser.add_argument("--tier", choices=("full", "fast"), default="full",
                        help="fast skips the real pinned-client classes (iteration only)")
    parser.add_argument("--home", choices=("temp", "inherit"), default="temp",
                        help="temp (default) gives the suite a private HOME")
    parser.add_argument("--platform", default=None, help=argparse.SUPPRESS)
    parser.add_argument("-k", dest="patterns", action="append", default=[], help="only tests whose name contains this")
    parser.add_argument("--require", action="append", default=[], metavar="PATTERN",
                        help="tests whose id matches this regex must run (a skip fails)")
    parser.add_argument("--max-skip-ratio", type=float, default=None, help="fail when skipped/ran exceeds this")
    parser.add_argument("--failfast", action="store_true")
    parser.add_argument("--list", action="store_true", help="print the selection and exit")
    parser.add_argument("--keep", action="store_true", help="keep the private HOME/TMPDIR for inspection")
    parser.add_argument("--claude", type=Path, metavar="PATH",
                        help="the pinned Claude Code (verified, then copied into the private HOME) "
                             "for the real-client lanes")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if sys.version_info < MIN_PYTHON:
        print(f"tools/test.py needs Python {'.'.join(map(str, MIN_PYTHON))} or newer", file=sys.stderr)
        return EXIT_USAGE
    platform_name = args.platform or current_platform()
    try:
        selected, excluded = select_modules(REPO / "tests", args.modules, platform_name)
    except UsageError as exc:
        print(f"tools/test.py: {exc}", file=sys.stderr)
        return EXIT_USAGE
    for module, platforms in excluded:
        print(f"not run on {platform_name}: {module} (PLATFORMS = {', '.join(platforms)})")
    if args.list:
        print("\n".join(selected))
        return EXIT_OK
    if not selected:
        print("tools/test.py: nothing selected on this platform", file=sys.stderr)
        return EXIT_USAGE
    not_runnable = []
    if args.claude is None and args.home == "temp":
        for module, variable in PINNED_CLIENT_LANES.items():
            if module in selected and os.environ.get(variable):
                selected.remove(module)
                not_runnable.append(f"{module} needs the pinned Claude Code ({variable} selects its real-client "
                                    "lane) and the private HOME has none; pass --claude PATH (a file matching the "
                                    "packaged pin)")
    for reason in not_runnable:
        print(f"not runnable: {reason}")
    args.not_runnable = not_runnable
    if not selected:
        print("tools/test.py: nothing runnable is selected", file=sys.stderr)
        return EXIT_FAILED
    work = Path(tempfile.mkdtemp(prefix="claude-multi-tests-"))
    os.chmod(work, 0o700)
    try:
        return _run(args, selected, work)
    except UsageError as exc:
        print(f"tools/test.py: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def place_pinned_client(repo: Path, source: Path) -> str:
    """Verify ``source`` against the packaged pin and copy it into the
    (private) HOME's owned location, as ``setup --step claude --claude-from``
    does; returns what was placed."""

    from claude_multi import acquire, errors, strict_json

    contract = strict_json.load(repo / "src" / "claude_multi" / "data" / "catalog" / "native-contract.json")
    try:
        outcome = acquire.acquire(contract, {"HOME": os.environ["HOME"], "PATH": ""}, claude_from=source.resolve())
    except (OSError, errors.ClaudeMultiError) as exc:
        raise UsageError(f"--claude {source}: {exc}") from exc
    return f"Claude Code {outcome.version} ({outcome.platform}) placed in the private HOME"


def _run(args: argparse.Namespace, selected: list[str], work: Path) -> int:
    guard_log = prepare_environment(work, repo=REPO, home=args.home, tier=args.tier)
    if args.claude is not None:
        print(place_pinned_client(REPO, args.claude))
    os.chdir(REPO)
    import tests  # noqa: F401  (arms the in-process tripwire)

    tripwire = sys.modules.get("_tripwire")
    if tripwire is None:
        raise UsageError("importing the tests package did not arm the tripwire")
    loader = unittest.TestLoader()
    if args.patterns:
        loader.testNamePatterns = [f"*{pattern}*" for pattern in args.patterns]
    suite = loader.loadTestsFromNames(selected)
    if suite.countTestCases() == 0:
        print("tools/test.py: the selection holds no tests", file=sys.stderr)
        return EXIT_USAGE
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1, failfast=args.failfast).run(suite)
    elapsed = time.monotonic() - started
    problems: list[str] = [f"not runnable: {reason}" for reason in getattr(args, "not_runnable", [])]
    if not result.wasSuccessful():
        problems.append(f"{len(result.failures)} failures, {len(result.errors)} errors")
    hits = list(getattr(tripwire, "HITS", []))
    if hits:
        problems.append(f"the in-process tripwire refused {len(hits)} live-gateway connects")
    if guard_log.exists() and guard_log.read_text(encoding="utf-8", errors="replace").strip():
        problems.append(f"a child process tried the live gateway (see {guard_log}; rerun with --keep)")
    skipped = [(test.id(), reason) for test, reason in result.skipped]
    for pattern in args.require:
        missed = [test_id for test_id, _ in skipped if re.search(pattern, test_id)]
        if missed:
            problems.append(f"required tests were skipped ({pattern}): {', '.join(missed[:5])}")
    if args.max_skip_ratio is not None and result.testsRun and len(skipped) > args.max_skip_ratio * result.testsRun:
        problems.append(f"{len(skipped)} of {result.testsRun} tests skipped (more than {args.max_skip_ratio:.0%})")
    reasons: dict[str, int] = {}
    for _test_id, reason in skipped:
        key = reason.split(":", 1)[0] if reason.startswith("BOUNDARY") else reason[:60]
        reasons[key] = reasons.get(key, 0) + 1
    print(f"\nran {result.testsRun} tests in {elapsed:.1f}s on {current_platform()} "
          f"(tier {args.tier}, HOME {args.home}); skipped {len(skipped)}")
    for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
        print(f"  skipped {count}: {reason}")
    for problem in problems:
        print(f"FAILED: {problem}", file=sys.stderr)
    return EXIT_FAILED if problems else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
