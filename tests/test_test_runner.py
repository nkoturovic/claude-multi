"""``tools/test.py``, the plain test runner, on a synthetic checkout.

The synthetic checkout carries the real runner, the real ``tests``
package bootstrap (``__init__.py``, ``_tripwire.py``) and the real
child-process guard source, plus tiny test modules that pass, fail, skip,
carry a platform marker or touch the live gateway port.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import hashlib
import json

from _layout import PACKAGE_DIR, REPO_ROOT
from _release import TEST_RUNNER, requires_release_tree

MODULES = {
    "test_pass": """
        import unittest
        class T(unittest.TestCase):
            def test_ok(self):
                self.assertTrue(True)
            def test_skipped(self):
                self.skipTest("BOUNDARY: not here")
    """,
    "test_marked": """
        PLATFORMS = ("windows",)
        import unittest
        class T(unittest.TestCase):
            def test_would_fail(self):
                self.fail("a marked module must not run elsewhere")
    """,
    "test_fail": """
        import unittest
        class T(unittest.TestCase):
            def test_fails(self):
                self.assertEqual(1, 2)
    """,
    "test_env": """
        import os, tempfile, unittest
        from pathlib import Path
        class T(unittest.TestCase):
            def test_private_home_and_tmp(self):
                home = Path(os.environ["HOME"])
                self.assertTrue(home.name == "home" and home.parent.name.startswith("claude-multi-tests-"))
                self.assertEqual(Path(tempfile.gettempdir()), home.parent / "tmp")
                for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_MULTI_ASSETS"):
                    self.assertNotIn(name, os.environ)
                self.assertEqual(os.environ["CM_TEST_TIER"], os.environ.get("EXPECT_TIER", "full"))
    """,
    "test_tripwire_hit": """
        import socket, unittest
        class T(unittest.TestCase):
            def test_connect(self):
                with socket.socket() as s:
                    with self.assertRaises(ConnectionRefusedError):
                        s.connect(("127.0.0.1", 8317))
    """,
    "test_child_hit": """
        import subprocess, sys, unittest
        CHILD = "import socket\\ntry:\\n    socket.create_connection(('127.0.0.1', 8316), 1)\\nexcept OSError:\\n    pass\\n"
        class T(unittest.TestCase):
            def test_child(self):
                subprocess.run([sys.executable, "-c", CHILD], check=True, timeout=60, stdin=subprocess.DEVNULL)
    """,
    "test_bad_marker": """
        PLATFORMS = ("plan9",)
    """,
    "test_keyed_compat_client": """
        import os, unittest
        from pathlib import Path
        class T(unittest.TestCase):
            def test_the_pinned_client_is_in_the_private_home(self):
                owned = Path(os.environ["HOME"]) / ".local/share/claude-multi/claude/9.9.9"
                self.assertEqual((owned / "claude").read_bytes(), b"#!/bin/sh\\necho fake pinned client\\n")
    """,
}


@requires_release_tree
class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(tempfile.mkdtemp(prefix="claude-multi-runner-"))
        cls.repo = cls.base / "repo"
        (cls.repo / "tools").mkdir(parents=True)
        (cls.repo / "src").mkdir()
        (cls.repo / "tests").mkdir()
        shutil.copy2(TEST_RUNNER, cls.repo / "tools" / "test.py")
        for name in ("__init__.py", "_tripwire.py", "check_fixture_isolation.py"):
            shutil.copy2(REPO_ROOT / "tests" / name, cls.repo / "tests" / name)
        for name, body in MODULES.items():
            (cls.repo / "tests" / f"{name}.py").write_text(textwrap.dedent(body).lstrip())
        # The package with a contract that pins a fake client (for --claude).
        shutil.copytree(PACKAGE_DIR, cls.repo / "src" / "claude_multi",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        cls.client = cls.base / "claude-9.9.9"
        cls.client.write_bytes(b"#!/bin/sh\necho fake pinned client\n")
        os.chmod(cls.client, 0o755)
        contract_path = cls.repo / "src/claude_multi/data/catalog/native-contract.json"
        contract = json.loads(contract_path.read_text())
        from claude_multi import pin

        contract["verified"][0]["version"] = "9.9.9"
        contract["verified"][0]["platforms"][pin.host_platform()] = {
            "sha256": hashlib.sha256(cls.client.read_bytes()).hexdigest(), "size": cls.client.stat().st_size}
        contract_path.write_text(json.dumps(contract, indent=2) + "\n")
        cls.cwd = cls.base / "elsewhere"
        cls.cwd.mkdir()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.base, ignore_errors=True)

    def run_runner(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        environ = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.base / "outer-home"),
                   "TMPDIR": str(self.base), "LC_ALL": "C", "XDG_STATE_HOME": "/nonexistent/state",
                   "CLAUDE_CONFIG_DIR": "/nonexistent/claude"}
        environ.update(env)
        return subprocess.run([sys.executable, str(self.repo / "tools" / "test.py"), *args], env=environ,
                              cwd=self.cwd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)

    def test_passing_selection_with_a_platform_marker(self) -> None:
        result = self.run_runner("tests.test_pass", "test_marked", "tests/test_env.py", "--platform", "linux")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not run on linux: tests.test_marked (PLATFORMS = windows)", result.stdout)
        self.assertIn("ran 3 tests", result.stdout)
        self.assertIn("skipped 1: BOUNDARY", result.stdout)
        self.assertFalse(list(self.base.glob("claude-multi-tests-*")))  # the private HOME/TMPDIR is removed

    def test_marker_selects_on_the_named_platform(self) -> None:
        result = self.run_runner("tests.test_marked", "--platform", "windows")
        self.assertEqual(result.returncode, 1)
        self.assertIn("a marked module must not run elsewhere", result.stderr)

    def test_failures_fail_the_run(self) -> None:
        result = self.run_runner("tests.test_pass", "tests.test_fail")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED: 1 failures, 0 errors", result.stderr)

    def test_tripwire_hits_fail_the_run_even_when_tests_pass(self) -> None:
        result = self.run_runner("tests.test_tripwire_hit")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("in-process tripwire refused 1", result.stderr)
        child = self.run_runner("tests.test_child_hit")
        self.assertEqual(child.returncode, 1, child.stdout + child.stderr)
        self.assertIn("a child process tried the live gateway", child.stderr)

    def test_skip_policies(self) -> None:
        required = self.run_runner("tests.test_pass", "--require", r"test_pass\.T\.test_skipped")
        self.assertEqual(required.returncode, 1)
        self.assertIn("required tests were skipped", required.stderr)
        ratio = self.run_runner("tests.test_pass", "--max-skip-ratio", "0.1")
        self.assertEqual(ratio.returncode, 1)
        self.assertIn("1 of 2 tests skipped", ratio.stderr)

    def test_usage_errors_and_listing(self) -> None:
        self.assertEqual(self.run_runner("tests.test_marked", "--platform", "linux").returncode, 2)
        self.assertEqual(self.run_runner("tests.test_nope").returncode, 2)
        self.assertEqual(self.run_runner("tests.test_pass", "-k", "nomatch").returncode, 2)
        bad = self.run_runner("tests.test_bad_marker")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("PLATFORMS must name some of", bad.stderr)
        listing = self.run_runner("--list", "--platform", "darwin")
        self.assertEqual(listing.returncode, 2)  # the bad marker refuses every whole-suite selection
        listing = self.run_runner("--list", "tests.test_pass", "tests.test_env", "--platform", "darwin")
        self.assertEqual(listing.stdout.split(), ["tests.test_env", "tests.test_pass"])

    def test_the_real_client_lane_needs_the_pinned_client(self) -> None:
        missing = self.run_runner("tests.test_keyed_compat_client", "tests.test_pass",
                                  CLAUDE_MULTI_TEST_CLI_PROXY_API="/nonexistent/gateway")
        self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
        self.assertIn("not runnable: tests.test_keyed_compat_client needs the pinned Claude Code", missing.stdout)
        self.assertIn("pass --claude PATH", missing.stderr)
        self.assertIn("ran 2 tests", missing.stdout)  # the rest still runs
        placed = self.run_runner("tests.test_keyed_compat_client", "--claude", str(self.client),
                                 CLAUDE_MULTI_TEST_CLI_PROXY_API="/nonexistent/gateway")
        self.assertEqual(placed.returncode, 0, placed.stdout + placed.stderr)
        self.assertIn("Claude Code 9.9.9", placed.stdout)
        wrong = self.base / "not-the-pin"
        wrong.write_bytes(b"something else")
        refused = self.run_runner("tests.test_keyed_compat_client", "--claude", str(wrong),
                                  CLAUDE_MULTI_TEST_CLI_PROXY_API="/nonexistent/gateway")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("--claude", refused.stderr)
        # Without the gateway variable the lane is not selected for the real client.
        self.assertEqual(self.run_runner("tests.test_pass").returncode, 0)

    def test_tier_and_name_filter(self) -> None:
        result = self.run_runner("tests.test_env", "--tier", "fast", "-k", "private", EXPECT_TIER="fast")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ran 1 tests", result.stdout)


if __name__ == "__main__":
    unittest.main()
