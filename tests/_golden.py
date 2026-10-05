"""Shared byte-exact golden assertion.

``assertGolden(testcase, path, actual_bytes)`` compares ``actual_bytes`` with
the checked-in golden at ``path``. On a mismatch (or a missing golden) it
fails with a truncated unified diff (golden -> actual) and the exact bless
command, so an intentional change is one copy-paste away and an unintentional
one is readable in the failure itself.
"""

from __future__ import annotations

import difflib
import unittest
from pathlib import Path

from _layout import REPO_ROOT

# From the repository root (the package root is the repository root).
BLESS_COMMAND = "PYTHONPATH=src:tests python3 tests/bless.py"
CHECK_COMMAND = "PYTHONPATH=src:tests python3 tests/bless.py --check"
MAX_DIFF_LINES = 60


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def golden_diff(expected: bytes, actual: bytes, name: str, *, limit: int = MAX_DIFF_LINES) -> str:
    """A unified diff golden -> actual, truncated to ``limit`` lines."""

    lines = list(
        difflib.unified_diff(
            expected.decode("utf-8", "replace").splitlines(keepends=True),
            actual.decode("utf-8", "replace").splitlines(keepends=True),
            fromfile=f"golden/{name}",
            tofile=f"actual/{name}",
        )
    )
    if not lines:
        # Same text after decoding: the difference is in bytes the decode
        # replaced or normalised; say so instead of printing an empty diff.
        return f"(no textual diff; {len(expected)} golden bytes vs {len(actual)} actual bytes)\n"
    shown = [line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
             for line in lines[:limit]]
    if len(lines) > limit:
        shown.append(f"... diff truncated: {len(lines) - limit} more line(s)\n")
    return "".join(shown)


def assertGolden(testcase: unittest.TestCase, path: Path | str, actual_bytes: bytes) -> None:
    """Fail ``testcase`` unless ``actual_bytes`` equals the golden at ``path``."""

    if not isinstance(actual_bytes, (bytes, bytearray)):
        raise TypeError(f"assertGolden needs bytes, got {type(actual_bytes).__name__}")
    golden = Path(path)
    name = _display(golden)
    try:
        expected = golden.read_bytes()
    except FileNotFoundError:
        testcase.fail(
            f"golden {name} is missing.\n"
            f"If this output is intended, bless and review the diff:\n  {BLESS_COMMAND}"
        )
    if bytes(actual_bytes) == expected:
        return
    testcase.fail(
        f"golden mismatch: {name}\n"
        f"{golden_diff(expected, bytes(actual_bytes), name)}"
        f"If the change is intended, bless and review the diff:\n  {BLESS_COMMAND}\n"
        f"(list what would change without writing: {CHECK_COMMAND})"
    )
