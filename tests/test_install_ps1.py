"""``packaging/install.ps1`` (Windows, WSL 2 bootstrap).

The Pester suite (``tests/pwsh/install.Tests.ps1``, wsl.exe stubbed) runs
when ``pwsh`` with Pester 5 or newer is available; CI runs it with the
pinned Pester 6. Without pwsh this module still checks the script
statically and runs its Linux side — the base64-carried bootstrap, exactly
as install.ps1 passes it to ``wsl.exe --exec`` — against a fake release
directory with ``sh``.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import unittest

from _fake_release import needs_ssh_keygen
from _release import INSTALL_PS1, INSTALL_SH, PESTER_TESTS, requires_release_tree
from test_installer import InstallerTestCase

FAKE_CURL = """#!/bin/sh
out=''
while [ $# -gt 0 ]; do
  case $1 in -o) out=$2; shift 2 ;; *) url=$1; shift ;; esac
done
printf '%s\\n' "$url" >>"$HOME/curl.log"
cp "$FAKE_CURL_SOURCE" "$out"
"""


def _balanced(text: str) -> list[str]:
    """Bracket balance outside comments, strings and here-strings."""

    problems: list[str] = []
    stack: list[tuple[str, int]] = []
    pairs = {")": "(", "}": "{", "]": "["}
    i, line = 0, 1
    while i < len(text):
        char = text[i]
        if char == "\n":
            line += 1
        if text.startswith("<#", i):
            end = text.index("#>", i)
            line += text.count("\n", i, end)
            i = end + 2
            continue
        if char == "#":
            i = text.index("\n", i)
            continue
        if text.startswith("@'\n", i) or text.startswith('@"\n', i):
            closing = "\n'@" if text[i + 1] == "'" else '\n"@'
            end = text.index(closing, i + 2)
            line += text.count("\n", i, end)
            i = end + 3
            continue
        if char in "'\"":
            j = i + 1
            while j < len(text):
                if char == '"' and text[j] == "`":
                    j += 2
                    continue
                if text[j] == char:
                    if j + 1 < len(text) and text[j + 1] == char:
                        j += 2
                        continue
                    break
                j += 1
            line += text.count("\n", i, j)
            i = j + 1
            continue
        if char in "({[":
            stack.append((char, line))
        elif char in ")}]":
            if not stack or stack[-1][0] != pairs[char]:
                problems.append(f"unbalanced {char!r} on line {line}")
            else:
                stack.pop()
        i += 1
    problems.extend(f"unclosed {opener!r} from line {where}" for opener, where in stack)
    return problems


@requires_release_tree
class StaticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = INSTALL_PS1.read_text()

    def test_structure(self) -> None:
        self.assertEqual(_balanced(self.text), [])
        self.assertIn("Set-StrictMode -Version 3.0", self.text)
        self.assertIn("$ErrorActionPreference = 'Stop'", self.text)
        self.assertNotRegex(self.text, r"(?i)invoke-expression|\biex\b")
        # wsl.exe runs only inside the two mockable wrappers.
        self.assertEqual(self.text.count("& wsl.exe @Arguments"), 2)
        self.assertEqual(self.text.count("wsl.exe @"), 2)
        self.assertIn("if ($MyInvocation.InvocationName -ne '.')", self.text)
        for name in ("Version", "InstallerUrl", "InstallerSha256"):
            self.assertRegex(self.text, rf"(?m)^\s+{name}\s+= ''$")

    def test_interactive_native_output_is_not_the_return_code(self) -> None:
        wrapper = self.text.split("function Invoke-WslInteractive {", 1)[1].split("\n}", 1)[0]
        self.assertTrue(wrapper.rstrip().endswith("& wsl.exe @Arguments"))
        self.assertNotIn("|", wrapper)
        self.assertNotIn("$LASTEXITCODE", wrapper)
        calls = [line.strip() for line in self.text.splitlines()
                 if "Invoke-WslInteractive -Arguments" in line]
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertTrue(call.startswith("Invoke-WslInteractive -Arguments"))
            self.assertNotIn("|", call)
            self.assertIn(call + "\n    $code = $LASTEXITCODE", self.text)
        workflow = (INSTALL_PS1.parent.parent / ".github/workflows/release.yml").read_text()
        for text in (self.text, workflow):
            calls = [line.strip() for line in text.splitlines() if "Invoke-Main -Distribution" in line]
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].startswith("Invoke-Main -Distribution"))
            self.assertNotIn("|", calls[0])
        self.assertIn("exit $script:MainExitCode", self.text)
        self.assertIn("$code = $script:MainExitCode", workflow)

    def test_main_sets_status_on_every_return_path(self) -> None:
        main = self.text.split("function Invoke-Main {", 1)[1].split("\n}", 1)[0]
        self.assertIn("$script:MainExitCode = 1", main)
        lines = [line.strip() for line in main.splitlines()]
        returns = [index for index, line in enumerate(lines) if line.startswith("return")]
        self.assertEqual(len(returns), 3)
        for index in returns:
            self.assertEqual(lines[index], "return")
            self.assertRegex(lines[index - 1], r"^\$script:MainExitCode = [03]$")
        self.assertEqual(lines[-1], "$script:MainExitCode = 0")

    def test_the_balance_check_detects_damage(self) -> None:
        self.assertNotEqual(_balanced("function x {\n  if ($a) {\n}\n"), [])
        self.assertEqual(_balanced("$a = '{'\n# }\n<# ( #>\n@'\n{\n'@\n"), [])

    def test_pester_suite_covers_the_wsl_paths(self) -> None:
        tests = PESTER_TESTS.read_text()
        self.assertEqual(_balanced(tests), [])
        for needle in ("WSL 1", "build 19041", "--no-distribution", "Ubuntu", "setup", "base64"):
            self.assertIn(needle, tests)


def _bootstrap() -> str:
    text = INSTALL_PS1.read_text()
    start = text.index("function Get-BootstrapScript")
    body = text[text.index("@'\n", start) + 3:text.index("\n'@", start)]
    return body + "\n"


@needs_ssh_keygen()
class LinuxSideTests(InstallerTestCase):
    """The bootstrap install.ps1 sends into the distribution, run by sh."""

    def run_bootstrap(self, *installer_args: str, sha256: str | None = None) -> subprocess.CompletedProcess[str]:
        if sha256 is None:
            sha256 = hashlib.sha256(INSTALL_SH.read_bytes()).hexdigest()
        fakebin = self.tmp / "curlbin"
        fakebin.mkdir(exist_ok=True)
        (fakebin / "curl").write_text(FAKE_CURL)
        os.chmod(fakebin / "curl", 0o755)
        payload = base64.b64encode(_bootstrap().encode("ascii")).decode()
        argv = ["sh", "-c", "echo $0 | base64 -d | sh -s -- $@", payload,
                "https://example.invalid/releases/v1.0.0/install.sh", sha256, *installer_args]
        env = {"HOME": str(self.home), "PATH": f"{fakebin}:{self.fakebin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
               "LC_ALL": "C", "TMPDIR": str(self.tmp), "SHELL": "/bin/sh", "FAKE_CURL_SOURCE": str(INSTALL_SH)}
        return subprocess.run(argv, env=env, cwd=self.tmp, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                              start_new_session=True)

    def test_bootstrap_downloads_and_runs_install_sh(self) -> None:
        release = self.release("1.0.0")
        result = self.run_bootstrap("--version", "1.0.0", "--from-dir", str(release.dir),
                                    "--allowed-signers", str(self.signers), "--no-modify-path")
        self.assertInstalled(result, "1.0.0")
        self.assertEqual((self.home / "curl.log").read_text(), "https://example.invalid/releases/v1.0.0/install.sh\n")
        self.assertNotIn("next: run", result.stdout)  # install.ps1 starts setup itself
        self.assertFalse(list(self.tmp.glob("claude-multi-install.*")))  # the downloaded copy is removed

    def test_bootstrap_refuses_missing_or_invalid_checksum_before_download(self) -> None:
        release = self.release("1.0.0")
        for checksum in ("none", "", "0" * 63, "g" * 64):
            with self.subTest(checksum=checksum):
                result = self.run_bootstrap("--from-dir", str(release.dir), "--allowed-signers", str(self.signers),
                                            "--no-modify-path", sha256=checksum)
                self.assertEqual(result.returncode, 1)
                self.assertIn("a trusted install.sh sha256 checksum is required", result.stderr)
                self.assertFalse((self.home / "curl.log").exists())
                self.assertFalse((self.root / "current").exists())

    def test_bootstrap_checks_the_installer_checksum(self) -> None:
        good = hashlib.sha256(INSTALL_SH.read_bytes()).hexdigest()
        release = self.release("1.0.0")
        args = ("--from-dir", str(release.dir), "--allowed-signers", str(self.signers), "--no-modify-path")
        refused = self.run_bootstrap(*args, sha256="0" * 64)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("does not match its published checksum", refused.stderr)
        self.assertFalse((self.root / "current").exists())
        self.assertInstalled(self.run_bootstrap(*args, sha256=good), "1.0.0")


PWSH = shutil.which("pwsh")


@requires_release_tree
@unittest.skipUnless(PWSH, "BOUNDARY: pwsh is not installed (CI runs the Pester suite)")
class PesterTests(unittest.TestCase):
    def test_pester_suite(self) -> None:
        probe = subprocess.run(stdin=subprocess.DEVNULL, args=[PWSH, "-NoProfile", "-NonInteractive", "-Command",
                                "if (Get-Module -ListAvailable Pester | Where-Object { $_.Version.Major -ge 5 }) { 'yes' }"],
                               capture_output=True, text=True, timeout=120)
        if probe.stdout.strip() != "yes":
            self.skipTest("BOUNDARY: Pester 5 or newer is not installed")
        command = ("$c = New-PesterConfiguration; $c.Run.Path = '%s'; $c.Run.Exit = $true; "
                   "$c.Output.Verbosity = 'Detailed'; Invoke-Pester -Configuration $c") % PESTER_TESTS
        result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", command], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=600, cwd=PESTER_TESTS.parent)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
