"""The battery provisions an interpreter that the isolation boundary admits."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


PLATFORMS = ("linux",)
REPO = Path(__file__).resolve().parents[1]


class BatteryInterpreterTests(unittest.TestCase):
    def test_toolcache_interpreter_is_read_only_before_the_suite(self) -> None:
        workflow = (REPO / ".github/workflows/ci.yml").read_text()
        battery = workflow.split("\n  battery:\n", 1)[1].split("\n  pty:\n", 1)[0]
        step = re.search(
            r"      - name: Read-only interpreter for isolated probes\n"
            r"        run: \|\n((?:          .*\n)+)", battery,
        )
        self.assertIsNotNone(step, "the battery must provision its writable setup-python interpreter")
        assert step is not None
        self.assertLess(step.start(), battery.index("      - name: Full suite"))
        script = "\n".join(line[10:] for line in step[1].splitlines())
        with tempfile.TemporaryDirectory(prefix="cm-battery-python-") as tmp:
            root = Path(tmp)
            interpreter = root / "interpreter"
            shutil.copyfile(Path(sys.executable).resolve(), interpreter)
            interpreter.chmod(0o775)
            (root / "python3").symlink_to(interpreter)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ.get("PATH", ""),
                   "PYTHONHOME": sys.base_prefix}
            done = subprocess.run(["/bin/sh", "-eu", "-c", script], env=env,
                                  capture_output=True, text=True, timeout=20)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(stat.S_IMODE(interpreter.stat().st_mode), 0o555)
            done = subprocess.run([str(root / "python3"), "-c", "print('ready')"], env=env,
                                  capture_output=True, text=True, timeout=20)
            self.assertEqual((done.returncode, done.stdout), (0, "ready\n"), done.stderr)
