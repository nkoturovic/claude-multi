"""The CI workflows, their scripts and tools, and the no-bundle guard.

The workflows are written to run on GitHub; here they are checked as text:
actions pinned by commit, no default permissions, no pull_request_target,
credentials dropped after checkout, ``umask 077`` first in every shell
step, no expression interpolation inside scripts, the guard before every
upload, every repository path they call present, every option they pass to
a repository tool one that tool has, the flake attributes they build
exported, and the release promotion behind its protected environment. The
journey script runs here on fake releases; the vulnerability check and the
pin watch run on stubs.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
from unittest import mock
import shutil
import signal
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

import os
import socket
import sys
import time
import urllib.error
import urllib.request

from _fake_release import FakeRelease, make_key, needs_ssh_keygen, signers_line
from _layout import REPO_ROOT, RESOURCES_ROOT
from _release import (NO_BUNDLE_GUARD, TOOLS_DIR, WORKFLOWS_DIR, load_tool, process_running,
                      requires_checkout, requires_release_tree)

WORKFLOWS = ("ci.yml", "release.yml", "pin-watch.yml")
JOURNEY = REPO_ROOT / ".github" / "scripts" / "journey.sh"
JOURNEY_FIXTURE = REPO_ROOT / ".github" / "scripts" / "journey_fixture.py"
SCRIPTS = REPO_ROOT / ".github" / "scripts"
WSL_PREPARE = SCRIPTS / "wsl_prepare.sh"
VULNCHECK = TOOLS_DIR / "_build" / "vulncheck.py"
PIN_WATCH = TOOLS_DIR / "_build" / "pin_watch.py"
# Paths the workflows call that later work adds; each entry must still be
# missing (a ratchet: delete it here once the file exists).
PENDING_PATHS: set[str] = set()
_USES = re.compile(r"^\s*(?:- )?uses: ([\w.-]+/[\w./-]+)@(\S+)(.*)$")
PINS = REPO_ROOT / ".github" / "PINS.md"


def _pin_tables(text: str) -> dict[str, list[list[str]]]:
    """The tables of .github/PINS.md by section title: their body rows."""

    tables: dict[str, list[list[str]]] = {}
    title = None
    for line in text.splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
        elif line.startswith("| ") and title is not None:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if set("".join(cells)) <= {"-", " "}:
                continue
            tables.setdefault(title, []).append(cells)
    return {name: rows[1:] for name, rows in tables.items()}  # drop each header row


def _steps(text: str) -> list[str]:
    return re.split(r"\n(?=\s+- (?:name|uses|id|if):)", text)


def _jobs(text: str) -> dict[str, str]:
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"\n(?=  [\w-]+:\n)", "\n" + body)
    return {part.strip().split(":", 1)[0]: part for part in parts if part.strip()}


def _run_blocks(step: str) -> list[list[str]]:
    lines = step.splitlines()
    blocks = []
    for index, line in enumerate(lines):
        if re.match(r"^\s+run: \|$", line):
            indent = len(line) - len(line.lstrip()) + 2
            block = []
            for follower in lines[index + 1:]:
                if follower.strip() and len(follower) - len(follower.lstrip()) < indent:
                    break
                block.append(follower)
            blocks.append(block)
    return blocks


def _upload_guard_problems(job: str) -> list[str]:
    """Why an artifact upload of ``job`` could publish what the no-bundle
    guard refused: no guard before it, a guard allowed to fail, or an upload
    that runs after a failure (always(), failure(), cancelled()) without
    depending on the guard's own success."""

    problems = []
    steps = _steps(job)
    uploads = 0
    for index, step in enumerate(steps):
        if "actions/upload-artifact@" not in step:
            continue
        uploads += 1
        guards = [earlier for earlier in steps[:index] if "no_bundle_guard.py" in earlier]
        if not guards:
            problems.append(f"upload {uploads} has no guard before it")
            continue
        guard = guards[-1]
        if "continue-on-error" in guard:
            problems.append(f"upload {uploads} follows a guard that may fail")
        condition = re.search(r"(?m)^\s+if: (.+)$", step)
        if condition is None or not re.search(r"always\(\)|failure\(\)|cancelled\(\)", condition.group(1)):
            continue  # success(): the guard, like every earlier step, passed
        guard_id = re.search(r"(?m)^\s+id: (\S+)$", guard)
        if guard_id is None:
            problems.append(f"upload {uploads} depends on a guard step without an id")
        elif f"steps.{guard_id.group(1)}.outcome == 'success'" not in condition.group(1):
            problems.append(f"upload {uploads} runs after a failed guard (if: {condition.group(1)})")
    return problems


def _commands(text: str) -> list[str]:
    """Every shell line of the workflows' run blocks, continuations joined."""

    lines: list[str] = []
    for step in _steps(text):
        for block in _run_blocks(step):
            pending = ""
            for line in block:
                pending += line.strip()
                if pending.endswith("\\"):
                    pending = pending[:-1] + " "
                    continue
                lines.append(pending)
                pending = ""
    return lines


@requires_release_tree
class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.texts = {name: (WORKFLOWS_DIR / name).read_text() for name in WORKFLOWS}

    def test_the_three_workflows_exist(self) -> None:
        self.assertEqual(sorted(p.name for p in WORKFLOWS_DIR.glob("*.yml")), sorted(WORKFLOWS))

    def test_permissions_and_triggers(self) -> None:
        for name, text in self.texts.items():
            with self.subTest(name):
                self.assertRegex(text, r"(?m)^permissions: \{\}$")
                self.assertNotIn("pull_request_target", text)
                # One secret only: the private identifier list, read by the
                # fast lane's identifier checks through the environment.
                allowed = "PRIVATE_IDENTIFIERS: ${{ secrets.CLAUDE_MULTI_PRIVATE_IDENTIFIERS }}"
                self.assertEqual(text.count(allowed), 1 if name == "ci.yml" else 0)
                self.assertNotIn("secrets.", text.replace(allowed, ""))
                for job, body in _jobs(text).items():
                    self.assertRegex(body, r"\n    permissions:\n", f"{name}: job {job} grants nothing explicitly")
                    self.assertRegex(body, r"\n    timeout-minutes: \d+", f"{name}: job {job} has no timeout")

    def test_actions_are_pinned_by_commit(self) -> None:
        seen = {}
        for name, text in self.texts.items():
            for line in text.splitlines():
                match = _USES.match(line)
                if not match:
                    self.assertNotRegex(line, r"^\s*(- )?uses:", f"{name}: unparsed uses line {line!r}")
                    continue
                action, ref, comment = match.groups()
                with self.subTest(f"{name}: {action}"):
                    self.assertRegex(ref, r"^[0-9a-f]{40}$")
                    self.assertRegex(comment, r"^ # v\d+(\.\d+)*$")
                    self.assertEqual(seen.setdefault(action, ref), ref, f"{action} pinned to two commits")

    def test_every_pin_is_recorded_as_verified(self) -> None:
        """.github/PINS.md records each action, tool and image pin the
        workflows use, with the date it was checked upstream, and nothing
        else; images are pinned by digest and Pester exactly."""

        tables = _pin_tables(PINS.read_text())
        actions = {(row[0], row[1], row[2]) for row in tables["Actions (pinned by commit)"]}
        used = set()
        for text in self.texts.values():
            for line in text.splitlines():
                match = _USES.match(line)
                if match:
                    used.add((match.group(1), match.group(3).removeprefix(" # "), match.group(2)))
        self.assertEqual(used, actions)
        workflows = "\n".join(self.texts.values())
        tools = {row[0]: row[1] for row in tables["Tools (pinned by version)"]}
        found = {
            "actionlint": re.findall(r"github\.com/rhysd/actionlint/cmd/actionlint@(\S+)", workflows),
            "zizmor": re.findall(r"'zizmor==([^' ]+)[ ']", workflows),
            "govulncheck": re.findall(r"golang\.org/x/vuln/cmd/govulncheck@(\S+)", workflows),
            "Pester": re.findall(r"Pester -RequiredVersion (\S+)", workflows),
            "setuptools": re.findall(r"'setuptools==([^' ]+)[ ']", workflows),
        }
        self.assertEqual(set(tools), set(found))
        for tool, versions in found.items():
            with self.subTest(tool=tool):
                self.assertTrue(versions)
                self.assertEqual(set(versions), {tools[tool]})
        self.assertNotIn("-MinimumVersion", workflows)
        images = {f"{row[0]}@{row[1]}" for row in tables["Container images (pinned by index digest)"]}
        pinned = set(re.findall(r'"([a-z0-9.-]+:[\w.-]+@sha256:[0-9a-f]{64})"', workflows))
        self.assertEqual(pinned, images)
        self.assertNotRegex(workflows, r"(?m)^\s+image: (?!\$\{\{)\S")  # no image outside the pinned matrix
        for title, rows in tables.items():
            for row in rows:
                with self.subTest(table=title, pin=row[0]):
                    self.assertRegex(row[-1], r"^20\d\d-\d\d-\d\d$")
        for _action, _tag, commit in actions:
            self.assertRegex(commit, r"^[0-9a-f]{40}$")

    def test_zizmor_is_installed_by_the_recorded_hash(self) -> None:
        """The fast lane installs the zizmor wheel PINS.md records, by its
        sha256 with pip's --require-hashes (the file the local lint ran)."""

        tables = _pin_tables(PINS.read_text())
        version = {row[0]: row[1] for row in tables["Tools (pinned by version)"]}["zizmor"]
        artifacts = {row[0]: row for row in tables["Tool artifacts (pinned by sha256)"]}
        self.assertEqual(set(artifacts), {"zizmor", "setuptools"})
        _tool, wheel, digest, _verified = artifacts["zizmor"]
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertRegex(wheel, rf"^zizmor-{re.escape(version)}-py3-none-manylinux_[0-9_]+_x86_64\.whl$")
        self.assertIn(f"`{wheel}`", PINS.read_text())  # the local lint's record names the same file
        lint = next(step for step in _steps(_jobs(self.texts["ci.yml"])["fast"]) if "zizmor" in step)
        commands = [line for block in _run_blocks(lint) for line in block]
        requirement = [line for line in commands if "zizmor==" in line]
        self.assertEqual(len(requirement), 1, requirement)
        self.assertIn(f"'zizmor=={version} --hash=sha256:{digest}' >\"$RUNNER_TEMP/zizmor.txt\"", requirement[0])
        install = [line for line in commands if "pip install" in line]
        self.assertEqual(len(install), 1, install)
        for option in ("--require-hashes", "--no-deps", "--only-binary=:all:", '-r "$RUNNER_TEMP/zizmor.txt"'):
            self.assertIn(option, install[0])
        self.assertEqual(re.findall(r"zizmor==\S+", "\n".join(self.texts.values())), [f"zizmor=={version}"])

    def test_checkout_drops_credentials(self) -> None:
        for name, text in self.texts.items():
            for step in _steps(text):
                if "uses: actions/checkout@" in step:
                    with self.subTest(name):
                        self.assertIn("persist-credentials: false", step)

    def test_shell_steps_start_with_umask_and_never_interpolate(self) -> None:
        for name, text in self.texts.items():
            for step in _steps(text):
                self.assertNotRegex(step, r"(?m)^\s+run: [^|\s]", f"{name}: use block scripts")
                for block in _run_blocks(step):
                    with self.subTest(f"{name}: {block[:1]}"):
                        self.assertFalse(any("${{" in line for line in block), "use env: for expressions")
                        if "shell: pwsh" not in step:
                            self.assertEqual(block[0].strip(), "umask 077")

    def test_guard_runs_before_every_upload(self) -> None:
        for name, text in self.texts.items():
            for job, body in _jobs(text).items():
                if "actions/upload-artifact@" in body:
                    with self.subTest(f"{name}: {job}"):
                        self.assertLess(body.index("no_bundle_guard.py"), body.index("actions/upload-artifact@"))
                        self.assertEqual(_upload_guard_problems(body), [])

    def test_a_failed_guard_stops_the_upload(self) -> None:
        """An upload that also runs after a failure (always()) must depend
        on the guard's own success, or a guard that found Claude Code bytes
        would not stop it."""

        body = _jobs(self.texts["release.yml"])["vulnerabilities"]
        self.assertIn("if: always() && steps.guard.outcome == 'success'", body)
        loose = body.replace(" && steps.guard.outcome == 'success'", "")
        self.assertEqual(_upload_guard_problems(loose), ["upload 1 runs after a failed guard (if: always())"])
        unnamed = body.replace("        id: guard\n", "")
        self.assertEqual(_upload_guard_problems(unnamed), ["upload 1 depends on a guard step without an id"])
        tolerant = body.replace("        id: guard\n", "        id: guard\n        continue-on-error: true\n")
        self.assertEqual(_upload_guard_problems(tolerant), ["upload 1 follows a guard that may fail"])

    def test_lanes(self) -> None:
        ci, release, watch = (self.texts[n] for n in WORKFLOWS)
        for needle in ("tests/bless.py --check", "git diff --check", 'tools/history_scan.py --repo . --range "$RANGE"',
                       "tests.test_hygiene", "tools/docs_gen.py --check", "import claude_multi",
                       "shellcheck -s sh packaging/install.sh",
                       ".github/scripts/journey.sh", "actionlint", "zizmor", "schedule:", "tools/test.py --tier full",
                       "max-parallel: 1", "tests.test_tui_pty", "apparmor_restrict_unprivileged_userns=0",
                       "fetch-depth: 0"):
            self.assertIn(needle, ci)
        for needle in ("ubuntu-24.04-arm", "macos-15", "windows-2025", "ubuntu:22.04", "ubuntu:24.04", "debian:12",
                       "fedora:", "apparmor_restrict_unprivileged_userns=0", "--draft", "release compare",
                       "if: ${{ !github.event.repository.private }}", "tests/pwsh", "journey.sh install dist",
                       "journey.sh update", "tools/_build/vulncheck.py", "govulncheck", "tools/test.py --verbose --claude",
                       "--gateway-dist gateway-dist", "environment: release", "pyc_sha256"):
            self.assertIn(needle, release)
        self.assertNotRegex(release, r"ssh-keygen -Y sign")  # the release key never signs in CI
        for forbidden in ("release create", "git push", "contents: write"):
            self.assertNotIn(forbidden, watch)
        self.assertIn("issues: write", watch)
        self.assertIn("tools/_build/pin_watch.py compare", watch)

    def test_the_pull_request_scan_policy(self) -> None:
        fast = _jobs(self.texts["ci.yml"])["fast"]
        policy = next(step for step in _steps(fast) if "id: range" in step)
        # The pull request's own base and head, required to be in the clone.
        for needle in ("github.event.pull_request.base.sha", "github.event.pull_request.head.sha",
                       'git cat-file -e "$rev^{commit}"', "exit 1", 'echo "range=$PR_BASE..$PR_HEAD"',
                       "0000000000000000000000000000000000000000", 'echo "range=$PUSH_BEFORE..$PUSH_AFTER"'):
            self.assertIn(needle, policy)
        scan = next(step for step in _steps(fast) if "history_scan.py" in step)
        self.assertIn("steps.range.outputs.range", scan)
        self.assertNotIn("|| true", scan)  # a candidate or an error fails the job

    def test_the_private_identifier_list_comes_from_a_secret_when_present(self) -> None:
        # The exact identifier list is never in the repository: a secret,
        # when the repository has one, becomes a 0600 file the scan and the
        # hygiene gates read through the environment; otherwise the generic
        # checks run alone.
        steps = _steps(_jobs(self.texts["ci.yml"])["fast"])
        index = next(i for i, step in enumerate(steps) if "secrets.CLAUDE_MULTI_PRIVATE_IDENTIFIERS" in step)
        step = steps[index]
        for needle in ("PRIVATE_IDENTIFIERS: ${{ secrets.CLAUDE_MULTI_PRIVATE_IDENTIFIERS }}", "umask 077",
                       'if [ -n "$PRIVATE_IDENTIFIERS" ]; then', 'chmod 600 "$list"',
                       'echo "CLAUDE_MULTI_PRIVATE_IDENTIFIERS=$list" >>"$GITHUB_ENV"',
                       "the generic identifier checks run"):
            self.assertIn(needle, step)
        run = step.split("run: |", 1)[1]
        self.assertNotIn("${{", run)  # the secret reaches the shell only through the environment
        later = steps[index + 1:]
        self.assertTrue(any("history_scan.py" in s for s in later))
        self.assertTrue(any("tests.test_hygiene" in s for s in later))
        import _layout

        self.assertEqual(_layout.load_tool("history_scan").PRIVATE_INPUT_ENV, "CLAUDE_MULTI_PRIVATE_IDENTIFIERS")

    def test_tool_options_exist(self) -> None:
        """Every option a workflow or the journey passes to a repository
        tool (or to the installed claude-multi) is one that command has."""

        help_cache: dict[tuple[str, ...], str] = {}

        def help_text(argv: tuple[str, ...]) -> str:
            if argv not in help_cache:
                result = subprocess.run([sys.executable, *argv, "--help"], cwd=REPO_ROOT, capture_output=True, text=True,
                                        timeout=120, stdin=subprocess.DEVNULL,
                                        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")})
                self.assertEqual(result.returncode, 0, (argv, result.stderr))
                help_cache[argv] = result.stdout
            return help_cache[argv]

        subcommands = {"tools/build.py": ("gateway", "release", "bundle"),
                       "tools/_build/python_runtime.py": ("fetch", "prepare", "host-interpreter", "compile", "digest"),
                       "tools/_build/pin_watch.py": (),
                       ".github/scripts/gateway_probes.py": ("compile", "provision")}
        checked = 0
        commands = [line for text in self.texts.values() for line in _commands(text)]
        commands += [line.strip() for line in JOURNEY.read_text().splitlines() if '"$cm" update' in line]
        for line in commands:
            match = re.search(r"python3 ((?:tools|\.github/scripts)/[\w/]+\.py)\b(.*)", line)
            claude = re.search(r'"\$cm" (update)\b(.*)', line)
            if match:
                script, rest = match.group(1), match.group(2)
                argv: tuple[str, ...] = (script,)
                first = rest.split()[0] if rest.split() else ""
                if first in subcommands.get(script, ()):
                    argv = (script, first)
            elif claude:
                argv, rest = ("bin/claude-multi", "update"), claude.group(2)
            else:
                continue
            for option in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", rest):
                with self.subTest(command=" ".join(argv), option=option):
                    self.assertIn(option, help_text(argv))
                    checked += 1
        self.assertGreater(checked, 20)

    def test_no_unsupported_acquisition_flags(self) -> None:
        for name, text in self.texts.items():
            with self.subTest(name):
                self.assertNotRegex(text, r"setup --step claude[^\n]*--(download|yes)")
                self.assertNotIn("--out \"$RUNNER_TEMP/native\"", text)

    @requires_checkout
    def test_the_flake_attributes_the_workflows_build(self) -> None:
        flake = (REPO_ROOT / "flake.nix").read_text()
        built = set()
        for text in self.texts.values():
            built |= set(re.findall(r"nix build [^\n]*?\.#([\w.-]+)", text))
        self.assertTrue(built)
        for attribute in built:
            with self.subTest(attribute):
                leaf = attribute.split(".")[-1]
                self.assertRegex(flake, rf"(?m)^\s*(x86_64-linux\.)?{re.escape(leaf)}\s*=")

    def test_release_promotion_is_protected(self) -> None:
        jobs = _jobs(self.texts["release.yml"])
        writers = {name for name, body in jobs.items() if re.search(r"contents: write|id-token: write", body)}
        self.assertEqual(writers, {"draft"})
        draft = jobs["draft"]
        self.assertIn("\n    environment: release\n", draft)
        needed = re.search(r"needs: \[([^\]]+)\]", draft).group(1)
        self.assertEqual({name.strip() for name in needed.split(",")}, set(jobs) - {"draft"})
        for name, body in jobs.items():
            if name != "draft":
                with self.subTest(name):
                    self.assertIn("\n      contents: read\n", body)
                    self.assertNotIn("environment:", body)
        self.assertNotIn("pull_request", self.texts["release.yml"].split("\njobs:\n")[0])

    def test_every_job_declares_a_timing_budget_and_the_pty_lane_is_serial(self) -> None:
        ci = _jobs(self.texts["ci.yml"])
        self.assertIn("max-parallel: 1", ci["pty"])
        self.assertIn("grep -v '^tests.test_tui_pty$'", ci["battery"])  # the terminal journeys run only in their lane
        for name, text in self.texts.items():
            for job, body in _jobs(text).items():
                with self.subTest(f"{name}: {job}"):
                    self.assertRegex(body, r"timeout-minutes: \d+")

    def test_referenced_repository_paths_exist(self) -> None:
        referenced = set()
        for text in self.texts.values():
            referenced |= set(re.findall(r"(?<![\w./-])((?:tools|tests|packaging|\.github)/[\w./-]+\.(?:py|sh|ps1))", text))
        self.assertTrue(referenced)
        missing = sorted(p for p in referenced if not (REPO_ROOT / p).exists())
        self.assertEqual(sorted(set(missing) - PENDING_PATHS), [])
        self.assertEqual(sorted(p for p in PENDING_PATHS if (REPO_ROOT / p).exists()), [],
                         "a pending path now exists: remove it from PENDING_PATHS")

    @unittest.skipUnless(shutil.which("shellcheck"), "BOUNDARY: shellcheck is not installed")
    def test_journey_script_lints(self) -> None:
        result = subprocess.run(["shellcheck", "-s", "sh", str(JOURNEY), str(WSL_PREPARE)], capture_output=True,
                                text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout)

    @unittest.skipUnless(shutil.which("actionlint"), "BOUNDARY: actionlint is not installed (CI runs it)")
    def test_actionlint(self) -> None:
        result = subprocess.run(["actionlint", *(str(WORKFLOWS_DIR / n) for n in WORKFLOWS)], capture_output=True,
                                text=True, timeout=120, stdin=subprocess.DEVNULL, cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@requires_release_tree
class JourneyStepsTests(unittest.TestCase):
    """The install journey's order on a real bundle: the confinement check
    while the gateway runs, the managed fixture turn and its resume, the
    stop, then another program on the gateway port; every install journey
    in CI has the pinned client for the turn."""

    def test_the_install_journey_order(self) -> None:
        text = JOURNEY.read_text()
        install = text.split("journey_install() {", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(install.index('fresh_install "$dist"'), install.index('journey_installed "$version"'))
        fresh = text.split("fresh_install() {", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(fresh.index('install_from "$work/release"'), fresh.index('say "fresh install: ok"'))
        body = text.split("journey_installed() {", 1)[1].split("\n}\n", 1)[0]
        steps = ['expect_version "$version"', "first_run", 'bundle_channel "installed $version"',
                 '"$cm" setup --step gateway', 'confinement "$port"', "managed_turn", '"$cm" gateway stop',
                 'foreign_listener "$port"', "session_ends", '"$cm" uninstall --keep-setup --yes']
        positions = [body.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        commands = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        self.assertFalse([line for line in commands if "--force" in line])  # never a forced uninstall
        # The channel after each step of the update journey.
        update = text.split("journey_update() {", 1)[1].split("\n}\n", 1)[0]
        steps = ['expect_version "$old"', 'bundle_channel "installed $old"', 'say "update check: ok"',
                 'bundle_channel "update check"', 'expect_version "$new"', 'bundle_channel "updated to $new"',
                 'say "update and up to date: ok"', 'bundle_channel "up to date"', 'say "rollback: ok"',
                 'bundle_channel "rolled back to $old"']
        positions = [update.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        # doctor's first run is judged by its result, never by one line it prints whatever its exit.
        first = text.split("first_run() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('fx first-run --launcher "$cm" --install "$install_dir") || fail', first)
        self.assertNotRegex(text, r'"\$cm" doctor[^\n]*\|\| true')
        turn = text.split("managed_turn() {", 1)[1].split("\n}\n", 1)[0]
        for needle in ('background "$work/fixture.pid" serve', "providers add --preset lan-openai-compatible", "--base-url \"$base\"",
                       "models add journey-fixture", "fx answer -- \"$cm\" models admit custom-journey-fixture",
                       '"$cm" direct --model custom-journey-fixture -- -p', '"$cm" -c -- -p',
                       'fx carried --log "$work/fixture.log" --reply "$second" --earlier "$first"'):
            self.assertIn(needle, turn)
        self.assertIn('base="http://127.0.0.1:', turn)  # the provider is the loopback fixture only
        confinement = text.split("confinement() {", 1)[1].split("\n}\n", 1)[0]
        for needle in ("--host 0.0.0.0", "control: a listener on every interface does not answer",
                       "the gateway answers on", '[ "$(uname -s)" != Darwin ]'):
            self.assertIn(needle, confinement)
        foreign = text.split("foreign_listener() {", 1)[1].split("\n}\n", 1)[0]
        for needle in ('"$cm" gateway start', "no token was sent", '"$cm" -c -- -p'):
            self.assertIn(needle, foreign)
        # The listener's credential log is read after each attempt.
        self.assertLess(foreign.index('"$cm" gateway start'), foreign.index('no_credential_sent "gateway start"'))
        self.assertLess(foreign.index('"$cm" -c -- -p'), foreign.index('no_credential_sent "the managed launch"'))

    def test_fixture_provider_survives_until_the_foreign_listener_launch(self) -> None:
        text = JOURNEY.read_text()
        managed = text.split("managed_turn() {", 1)[1].split("\n}\n", 1)[0]
        installed = text.split("journey_installed() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn('stop_helper "$work/fixture.pid"', managed,
                         "the resumed launch still needs its provider for profile preflight")
        self.assertIn('stop_helper "$work/fixture.pid"', installed)
        self.assertLess(installed.index('foreign_listener "$port"'),
                        installed.index('stop_helper "$work/fixture.pid"'))
        self.assertLess(installed.index('stop_helper "$work/fixture.pid"'), installed.index("session_ends"))
        self.assertRegex(installed, r'if \[ -n "\$\{JOURNEY_CLIENT:-\}" \]; then\s+'
                                   r'stop_helper "\$work/fixture.pid"\s+fi')
        foreign = text.split("foreign_listener() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("the gateway was not started", foreign)
        self.assertIn('no_credential_sent "the managed launch" "$1"', foreign)

    STUB_CM = """#!/bin/sh
# claude-multi as the foreign-listener step meets it: both attempts refuse;
# LEAK names the one that first sends the listener a credential.
case $1 in
gateway) [ "${LEAK:-}" != start ] || printf '{"credentials": true}\\n' >>"$WORK/foreign.log"
  echo "refused: another program listens on the gateway port; no token was sent"; exit 1 ;;
-c) [ "${LEAK:-}" != resume ] || printf '{"credentials": true}\\n' >>"$WORK/foreign.log"
  echo "refused: the gateway was not started"; exit 1 ;;
esac
exit 2
"""
    HARNESS = """set -eu
say() { printf 'journey: %s\\n' "$*"; }
fail() { printf 'journey: FAILED: %s\\n' "$*" >&2; exit 1; }
work=$WORK
cm=$STUB
helpers=''
install_dir=$INSTALL
fixture=$FIXTURE
JOURNEY_CLIENT=client
trap cleanup EXIT
fx() {
	case $1 in
	listen) exec sleep 60 ;;
	reach) return 0 ;;
	esac
}
"""

    def run_foreign_listener(self, tmp: Path, leak: str) -> subprocess.CompletedProcess[str]:
        """The journey's own foreign-listener step (and the helpers it
        calls), with a listener and a claude-multi stub."""

        text = JOURNEY.read_text()
        functions = []
        for name in ("kill_helper", "cleanup", "background", "stop_helper", "wait_for", "show_tail",
                     "no_credential_sent", "foreign_listener"):
            start = text.find(f"\n{name}() {{\n")
            if start >= 0:
                start += 1
                functions.append(text[start:text.index("\n}\n", start) + 3])
        stub = tmp / "claude-multi"
        stub.write_text(self.STUB_CM)
        stub.chmod(0o755)
        (tmp / "work" / "project").mkdir(parents=True, exist_ok=True)
        python = tmp / "runtime" / "python" / "bin" / "python3"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        fixture = tmp / "fixture.py"
        fixture.write_text("import time\ntime.sleep(60)\n")
        script = self.HARNESS + "".join(functions) + "foreign_listener 18399\n"
        environ = {**os.environ, "WORK": str(tmp / "work"), "STUB": str(stub), "LEAK": leak,
                   "INSTALL": str(tmp), "FIXTURE": str(fixture)}
        return subprocess.run(["sh", "-c", script], env=environ, cwd=tmp, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, start_new_session=True)

    def test_a_credential_sent_to_the_foreign_listener_fails_the_step(self) -> None:
        for leak, message in (("", None), ("start", "gateway start sent a credential to the listener on port 18399"),
                              ("resume", "the managed launch sent a credential to the listener on port 18399")):
            with self.subTest(leak=leak), tempfile.TemporaryDirectory() as tmp:
                result = self.run_foreign_listener(Path(tmp), leak)
                if message is None:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("another program on the gateway port refused: ok", result.stdout)
                else:
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(f"journey: FAILED: {message}", result.stderr)
                    self.assertNotIn("refused: ok", result.stdout)

    def test_background_helpers_are_reaped_on_stop_and_failed_exit(self) -> None:
        text = JOURNEY.read_text()
        functions = []
        for name in ("kill_helper", "cleanup", "fx", "background", "stop_helper"):
            start = text.find(f"\n{name}() {{\n")
            if start >= 0:
                functions.append(text[start + 1:text.index("\n}\n", start) + 3])
        # Use the script's real call shape, including the old fx subshell on
        # an unfixed tree. The fixture owns an ephemeral listener and a child.
        launch = re.search(r'background "\$work/open.pid" (?:fx )?listen', text).group()
        shells = dict.fromkeys(filter(None, (shutil.which(name) for name in ("sh", "dash", "bash"))))
        cases = ((shell, outcome) for shell in shells
                 for outcome in ("stop", "failed exit", "timeout", "forced timeout"))
        for shell, outcome in cases:
            with self.subTest(shell=shell, outcome=outcome), tempfile.TemporaryDirectory() as root:
                tmp = Path(root)
                python = tmp / "runtime/python/bin/python3"
                python.parent.mkdir(parents=True)
                python.symlink_to(sys.executable)
                fixture = tmp / "fixture.py"
                fixture.write_text(
                    "import json, os, socket, subprocess, sys, time\n"
                    "from pathlib import Path\n"
                    "sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen()\n"
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                    "Path(os.environ['REPORT']).write_text(json.dumps([os.getpid(), child.pid, sock.getsockname()[1]]))\n"
                    "time.sleep(60)\n")
                work = tmp / "work"
                work.mkdir()
                report = tmp / "report.json"
                recorded = tmp / "recorded"
                script = ("set -eu\nwork=$WORK\ninstall_dir=$INSTALL\nfixture=$FIXTURE\nhelpers=''\n"
                          "trap cleanup EXIT\ntrap 'exit 143' TERM\n" + "".join(functions) + launch + "\n"
                          "for attempt in $(seq 1 100); do [ ! -s \"$REPORT\" ] || break; sleep 0.02; done\n"
                          'cp "$work/open.pid" "$RECORDED"\n' +
                          {"stop": 'stop_helper "$work/open.pid"\n', "failed exit": "exit 17\n",
                           "timeout": "sleep 60\n", "forced timeout": "trap '' TERM\nsleep 60\n"}[outcome])
                env = {**os.environ, "WORK": str(work), "INSTALL": str(tmp), "FIXTURE": str(fixture),
                       "REPORT": str(report), "RECORDED": str(recorded)}
                argv = [shell, "-c", script]
                if outcome.endswith("timeout"):
                    wrapper = (
                        f"import sys; sys.path.insert(0, {str(REPO_ROOT / 'tests')!r})\n"
                        "from _release import load_tool\nfrom pathlib import Path\n"
                        f"release = load_tool(Path({str(TOOLS_DIR / 'release.py')!r}), 'release')\n"
                        f"sys.exit(release.run({argv!r}, cwd=Path.cwd(), timeout=0.5, process_group=True).returncode)\n")
                    argv = [sys.executable, "-c", wrapper]
                pids = []
                with tempfile.TemporaryFile() as output:
                    child = subprocess.Popen(argv, cwd=tmp, env=env, stdout=output, stderr=output,
                                             stdin=subprocess.DEVNULL, start_new_session=True)
                    try:
                        code = child.wait(timeout=10)
                        self.assertTrue(report.exists(), "the background fixture did not start")
                        parent, descendant, port = json.loads(report.read_text())
                        pids = [parent, descendant]
                        self.assertEqual(code, {"stop": 0, "failed exit": 17, "timeout": 124,
                                                "forced timeout": 124}[outcome])
                        self.assertEqual(int(recorded.read_text()), parent, "recorded a shell instead of the listener")
                        for role, pid in zip(("listener", "descendant"), pids):
                            for _ in range(100):
                                if not process_running(pid):
                                    break
                                time.sleep(0.02)
                            self.assertFalse(process_running(pid), f"helper {role} process {pid} survived")
                        with socket.socket() as probe:
                            probe.settimeout(1)
                            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
                    finally:
                        # The regression must not leak even on the unfixed tree.
                        if report.exists():
                            pids = json.loads(report.read_text())[:2]
                        for pid in [child.pid, *pids]:
                            for kill in (os.killpg, os.kill):
                                with contextlib.suppress(ProcessLookupError):
                                    kill(pid, signal.SIGKILL)
                        child.wait(timeout=5)

    def test_journeys_check_private_state_under_permissive_umask(self) -> None:
        text = JOURNEY.read_text()
        self.assertIn("\numask 0002\n", text)
        self.assertNotIn("\numask 077\n", text)
        installed = text.split("journey_installed() {", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(installed.index('"$cm" gateway stop'), installed.index("private_directories"))
        fresh = text.split("fresh_install() {", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(fresh.index("install_from"), fresh.index("private_directories"))

    def test_linux_journeys_record_modes_and_optional_default_acls(self) -> None:
        text = JOURNEY.read_text()
        self.assertIn("permission_diagnostics() {", text)
        diagnostics = text.split("permission_diagnostics() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("stat -c '%a %U:%G %n'", diagnostics)
        self.assertIn('command -v getfacl >/dev/null 2>&1', diagnostics)
        self.assertIn('getfacl -p "$HOME" || true', diagnostics)
        fresh = text.split("fresh_install() {", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(fresh.index("permission_diagnostics"), fresh.index("install_from"))
        self.assertGreater(fresh.rindex("permission_diagnostics"), fresh.index("install_from"))
        jobs = _jobs((WORKFLOWS_DIR / "release.yml").read_text())
        for job in ("service-linux", "journeys-linux"):
            self.assertIn("JOURNEY_PERMISSIONS=1", jobs[job])

    def test_every_ci_install_journey_has_the_pinned_client(self) -> None:
        jobs = _jobs((WORKFLOWS_DIR / "release.yml").read_text())
        self.assertIn("JOURNEY_CLIENT=fetch sh .github/scripts/journey.sh install dist", jobs["journeys-linux"])
        for job, target in (("journeys-linux-arm64", "linux-aarch64"), ("journeys-macos-intel", "darwin-x86_64")):
            with self.subTest(job):
                self.assertIn(f"JOURNEY_CLIENT=fetch JOURNEY_TARGET={target} sh .github/scripts/journey.sh install dist",
                              jobs[job])
        self.assertIn("JOURNEY_CLIENT=fetch sh .github/scripts/journey.sh installed", jobs["journeys-windows"])
        self.assertIn("JOURNEY_CLIENT: ${{ runner.temp }}/claude", jobs["journeys-macos"])
        self.assertIn("curl", jobs["journeys-linux"].split("- name: Base tools", 1)[1].split("- uses:", 1)[0])


@requires_release_tree
class JourneyFixtureTests(unittest.TestCase):
    """journey_fixture.py, the journey's loopback fixtures (ephemeral ports
    only), run the way the journey runs it: a separate isolated Python."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-journey-fixture-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def fixture(self, *args: str, **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", str(JOURNEY_FIXTURE), *args], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL, cwd=self.tmp, **kwargs)

    def background(self, *args: str) -> subprocess.Popen:
        process = subprocess.Popen([sys.executable, "-I", str(JOURNEY_FIXTURE), *args], cwd=self.tmp,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop() -> None:
            process.kill()
            process.wait(timeout=30)
        self.addCleanup(stop)
        return process

    def port(self, path: Path) -> int:
        deadline = time.monotonic() + 30
        while not (path.is_file() and path.read_text().strip()):
            self.assertLess(time.monotonic(), deadline, f"{path} was not written")
            time.sleep(0.05)
        return int(path.read_text())

    def post(self, port: int, document: dict, headers: dict | None = None) -> bytes:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                         data=json.dumps(document).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - a loopback fixture
            return response.read()

    def test_directory_check_rejects_shared_state_but_not_signed_payload_modes(self) -> None:
        data = self.tmp / ".local/share/claude-multi"
        versions = data / "install/versions"
        versions.mkdir(parents=True)
        for path in (data, data / "install", versions):
            path.chmod(0o700)
        payload = versions / "1.0.0"
        payload.mkdir(mode=0o755)
        payload.chmod(0o755)
        auth = data / "auth"
        auth.mkdir(mode=0o700)
        env = {"HOME": str(self.tmp), "PATH": os.defpath}
        good = self.fixture("private-dirs", env=env)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        for mode in (0o755, 0o770):
            auth.chmod(mode)
            bad = self.fixture("private-dirs", env=env)
            self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
            self.assertIn(str(auth), bad.stderr)
            self.assertIn("group/other access", bad.stderr)
        auth.chmod(0o700)
        self.assertEqual(self.fixture("private-dirs", env=env).returncode, 0)

    def test_the_chat_fixture_numbers_replies_and_records_what_came_back(self) -> None:
        self.background("serve", "--port-file", str(self.tmp / "port"), "--log", str(self.tmp / "log"))
        port = self.port(self.tmp / "port")
        first = json.loads(self.post(port, {"model": "m", "messages": [{"role": "user", "content": "one"}]}))
        self.assertEqual(first["choices"][0]["message"]["content"], "fixture reply 1")
        stream = self.post(port, {"model": "m", "stream": True, "messages": [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": [{"type": "text", "text": "fixture reply 1"}]},
            {"role": "user", "content": "two"}]})
        self.assertIn(b'"content": "fixture reply 2"', stream)
        self.assertTrue(stream.endswith(b"data: [DONE]\n\n"))
        records = [json.loads(line) for line in (self.tmp / "log").read_text().splitlines()]
        self.assertEqual([(r["reply"], r["carried"], r["stream"]) for r in records], [(1, [], False), (2, [1], True)])
        self.assertNotIn("one", (self.tmp / "log").read_text())  # metadata only, never message text
        self.assertEqual(self.fixture("carried", "--log", str(self.tmp / "log"), "--reply", "2", "--earlier", "1")
                         .returncode, 0)
        for reply, earlier in (("1", "2"), ("9", "1")):
            with self.subTest(reply=reply):
                refused = self.fixture("carried", "--log", str(self.tmp / "log"), "--reply", reply, "--earlier", earlier)
                self.assertEqual(refused.returncode, 1)
                self.assertIn("journey_fixture:", refused.stderr)

    def test_the_listener_logs_credentials_and_reach_tells_open_from_closed(self) -> None:
        self.background("listen", "--port", "0", "--port-file", str(self.tmp / "port"), "--log", str(self.tmp / "log"))
        port = self.port(self.tmp / "port")
        self.assertEqual(self.fixture("reach", "--host", "127.0.0.1", "--port", str(port)).returncode, 0)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=30) as response:  # noqa: S310
            self.assertEqual(response.read(), b"ok")
        self.post(port, {}, headers={"Authorization": "Bearer fixture-only"})
        records = [json.loads(line) for line in (self.tmp / "log").read_text().splitlines()]
        self.assertEqual([r["credentials"] for r in records if r["method"] != "HEAD"], [False, True])
        self.assertNotIn("fixture-only", (self.tmp / "log").read_text())
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        self.assertEqual(self.fixture("reach", "--host", "127.0.0.1", "--port", str(closed), "--timeout", "2")
                         .returncode, 1)

    def test_addresses_are_never_loopback(self) -> None:
        result = self.fixture("addresses")
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in result.stdout.splitlines():
            with self.subTest(line):
                self.assertRegex(line, r"^\d+\.\d+\.\d+\.\d+$")
                self.assertFalse(line.startswith("127."))

    def test_answer_confirms_each_question_and_keeps_the_exit_status(self) -> None:
        try:
            os.close(os.openpty()[0])
        except OSError as exc:
            self.skipTest(f"BOUNDARY: no pseudo-terminal here ({exc})")
        asks = ('printf "Proceed? [y/N] " >&2; read a; [ "$a" = y ] || exit 5; '
                'printf "Again? [y/N] "; read b; [ "$b" = y ] || exit 6; echo confirmed; exit 3')
        result = self.fixture("answer", "--", "sh", "-c", asks)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("confirmed", result.stdout)

    def test_client_names_the_installed_release_pin(self) -> None:
        from claude_multi import acquire, pin, strict_json

        site = self.tmp / "install" / "lib" / "python3.14" / "site-packages"
        site.mkdir(parents=True)
        (site / "claude_multi").symlink_to(REPO_ROOT / "src" / "claude_multi")
        result = self.fixture("client", "--install", str(self.tmp / "install"), env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        contract = strict_json.load(RESOURCES_ROOT / "catalog" / "native-contract.json")
        record = pin.platform_record(contract, pin.host_platform())
        self.assertEqual(result.stdout.split(), [acquire.download_url(pin.version(contract), pin.host_platform()),
                                                 record["sha256"], str(record["size"])])
        missing = self.fixture("client", "--install", str(self.tmp / "nothing"))
        self.assertEqual(missing.returncode, 1)

    # claude-multi as the first run meets it: doctor --first-run --json and
    # doctor --first-run print what the test wrote and exit with its codes.
    STUB_DOCTOR = """#!/bin/sh
[ "$1" = doctor ] && [ "${2:-}" = --first-run ] || exit 2
if [ "${3:-}" = --json ]; then cat "$STUB/json"; exit "$(cat "$STUB/json.exit")"; fi
cat "$STUB/text"; exit "$(cat "$STUB/text.exit")"
"""

    @staticmethod
    def check(check_id: str, state: str, detail: str, fix: str | None = None):
        from claude_multi.setup import firstrun, model

        title = firstrun.TITLES[check_id].format(v="2.1.0")
        return firstrun.Check(check_id, title, state, detail,
                              None if fix is None else model.Fix(None, fix, tuple(fix.split())))

    def fresh(self, **changes) -> list:
        """The nine checks of a fresh install (``changes`` replaces some)."""

        found = {
            "computer": self.check("computer", "ok", "Linux x86_64 · 12.0 GB free · local disk"),
            "files": self.check("files", "ok", "private folders for your settings"),
            "policy": self.check("policy", "ok", "no administrator setting blocks claude-multi"),
            "claude": self.check("claude", "fail", "not set up for claude-multi", "claude-multi setup --step claude"),
            "gateway": self.check("gateway", "fail", "not set up yet", "claude-multi setup --step gateway"),
            "providers": self.check("providers", "fail", "nothing connected yet",
                                    "claude-multi setup --step providers"),
            "models": self.check("models", "waiting", "waits for a provider"),
            "profile": self.check("profile", "waiting", "waits for a provider"),
            "hooks": self.check("hooks", "fail", "~/.local/state/claude-multi/bin/claude-multi-hook is missing",
                                "claude-multi doctor --repair-all"),
        }
        found.update(changes)
        return list(found.values())

    def first_run(self, items: list, *, json_exit: int | None = None, text_exit: int | None = None,
                  json_text: str | None = None, text: str | None = None, document: dict | None = None,
                  home: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        """``journey_fixture.py first-run`` over a doctor stub whose two
        forms show ``items`` (the product's own rendering), unless replaced."""

        from claude_multi.setup import firstrun, texts

        stub = self.tmp / "stub"
        stub.mkdir(exist_ok=True)
        site = self.tmp / "install" / "lib" / "python3.14" / "site-packages"
        if not site.exists():
            site.mkdir(parents=True)
            (site / "claude_multi").symlink_to(REPO_ROOT / "src" / "claude_multi")
        (stub / "claude-multi").write_text(self.STUB_DOCTOR)
        (stub / "claude-multi").chmod(0o755)
        report = document if document is not None else firstrun.to_json(items)
        code = 0 if report.get("ready") else 1
        (stub / "json").write_text(json_text if json_text is not None else json.dumps(report))
        (stub / "json.exit").write_text(str(code if json_exit is None else json_exit))
        lines = [texts.FIRSTRUN_HEADER, *firstrun.render_lines(items, surface="cli"), texts.FIRSTRUN_INFO,
                 firstrun.summary(items)]
        (stub / "text").write_text(text if text is not None else "\n".join(lines) + "\n")
        (stub / "text.exit").write_text(str(code if text_exit is None else text_exit))
        # The caller's PATH: the stub's cat may live anywhere (the Nix sandbox has no /usr/bin).
        environ = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(home or self.home), "STUB": str(stub),
                   **(env or {})}
        return self.fixture("first-run", "--launcher", str(stub / "claude-multi"), "--install",
                            str(self.tmp / "install"), env=environ)

    FRESH = "not ready only for what a fresh install has not set up yet"

    def test_first_run_succeeds_ready_or_not_ready_only_for_the_unconfigured_install(self) -> None:
        self.home = self.tmp / "home"
        self.home.mkdir()
        ok = [self.check(item.id, "ok", "fine") for item in self.fresh()]
        result = self.first_run(ok)
        self.assertEqual((result.returncode, result.stdout), (0, "ready\n"), result.stderr)
        verified = self.check("claude", "ok", "verified copy")
        attention = self.check("policy", "attention", "an administrator setting affects managed sessions",
                               "ask your administrator")
        for items, names in ((self.fresh(), "claude, gateway, providers, models, profile, hooks"),
                             (self.fresh(claude=verified, policy=attention),
                              "gateway, providers, models, profile, hooks")):
            with self.subTest(names):
                result = self.first_run(items)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"{self.FRESH} ({names})\n")
        # The helpers named under the home as given, as resolved, or under XDG_STATE_HOME.
        linked = self.tmp / "linked-home"
        linked.symlink_to(self.home)
        absolute = self.check("hooks", "fail", f"{self.home}/.local/state/claude-multi/bin/claude-multi-hook is missing",
                              "claude-multi doctor --repair-all")
        result = self.first_run(self.fresh(hooks=absolute), home=linked)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.tmp / "state"
        elsewhere = self.check("hooks", "fail", f"{state}/claude-multi/bin/claude-multi-hook is missing",
                               "claude-multi doctor --repair-all")
        result = self.first_run(self.fresh(hooks=elsewhere), env={"XDG_STATE_HOME": str(state)})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_first_run_fails_on_anything_else(self) -> None:
        from claude_multi.setup import firstrun

        self.home = self.tmp / "home"
        self.home.mkdir()
        unexpected = "the first run {state}s a check a fresh install passes: "
        fresh = self.fresh()
        reordered = firstrun.to_json(fresh)
        reordered["items"] = reordered["items"][1:] + reordered["items"][:1]
        wrong_next = {**firstrun.to_json(fresh), "next": "providers"}
        plain = {"schema_version": 1, "report": "doctor", "status": "blocked", "facts": [], "diagnostics": []}
        for name, items, kwargs, problem in (
                # A nonzero exit for another reason.
                ("exit 71", self.fresh(), dict(json_exit=71, text_exit=71),
                 "doctor --first-run --json exited 71 with ready false"),
                ("ready but exit 1", [self.check(item.id, "ok", "fine") for item in fresh], dict(json_exit=1),
                 "doctor --first-run --json exited 1 with ready true"),
                ("text exit 71", self.fresh(), dict(text_exit=71),
                 "doctor --first-run exited 71, doctor --first-run --json 1"),
                ("no report", self.fresh(), dict(json_text="Traceback (most recent call last):\n"),
                 "doctor --first-run --json printed no first-run report (exit 1)"),
                ("the ordinary doctor report", self.fresh(), dict(document=plain),
                 "doctor --first-run --json printed no first-run report"),
                ("checks reordered", self.fresh(), dict(document=reordered),
                 "doctor --first-run --json does not list the nine checks in order"),
                ("next disagrees", self.fresh(), dict(document=wrong_next),
                 "doctor --first-run --json disagrees with itself"),
                ("text says ready", self.fresh(), dict(text="claude-multi doctor --first-run\nReady.\n"),
                 "doctor --first-run and doctor --first-run --json disagree"),
                ("a check the fresh install passes", self.fresh(computer=self.check(
                    "computer", "fail", "less than 600 MB free under ~/.local/share/claude-multi",
                    "free at least 600 MB under ~/.local/share/claude-multi")), {},
                 unexpected.format(state="fail") + "computer (less than 600 MB free"),
                ("another gateway problem", self.fresh(gateway=self.check(
                    "gateway", "fail", "the gateway program (cli-proxy-api) is not installed",
                    "reinstall claude-multi")), {},
                 unexpected.format(state="fail") + "gateway (the gateway program"),
                ("near miss", self.fresh(providers=self.check(
                    "providers", "fail", "nothing connected yet (or not)", "claude-multi setup --step providers")), {},
                 unexpected.format(state="fail") + "providers (nothing connected yet (or not)"),
                ("another fix", self.fresh(claude=self.check(
                    "claude", "fail", "not set up for claude-multi", "claude-multi doctor")), {},
                 unexpected.format(state="fail") + "claude (not set up for claude-multi; fix: claude-multi doctor)"),
                ("waiting instead of failing", self.fresh(providers=self.check(
                    "providers", "waiting", "nothing connected yet")), {},
                 unexpected.format(state="waiting") + "providers"),
                ("another home", self.fresh(hooks=self.check(
                    "hooks", "fail", f"{self.tmp}/elsewhere/.local/state/claude-multi/bin/claude-multi-hook is missing",
                    "claude-multi doctor --repair-all")), {},
                 unexpected.format(state="fail") + "hooks"),
                ("text without the check", self.fresh(), dict(
                    text="claude-multi doctor --first-run\nNot ready: 6 item(s) need a fix (first: Claude Code 2.1.0).\n"),
                 "doctor --first-run does not show the claude check")):
            with self.subTest(name):
                result = self.first_run(items, **kwargs)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(f"journey_fixture: the first run failed: {problem}", result.stderr)
        missing = self.fixture("first-run", "--launcher", str(self.tmp / "absent"), "--install", str(self.tmp))
        self.assertEqual(missing.returncode, 1)
        self.assertIn("is not an installed release", missing.stderr)

    def installed(self) -> Path:
        site = self.tmp / "install" / "lib" / "python3.14" / "site-packages"
        if not site.exists():
            site.mkdir(parents=True)
            (site / "claude_multi").symlink_to(REPO_ROOT / "src" / "claude_multi")
        return self.tmp / "install"

    def test_ended_waits_for_every_session_record_to_end(self) -> None:
        import threading

        from claude_multi import sessions

        home = self.tmp / "home"
        records = home / ".local" / "state" / "claude-multi" / "sessions"
        environ = {"HOME": str(home), "PATH": os.environ.get("PATH", os.defpath)}
        nothing = self.fixture("ended", "--install", str(self.installed()), "--timeout", "1", env=environ)
        self.assertEqual((nothing.returncode, nothing.stdout), (0, "0 managed session record(s), every one ended\n"))
        records.mkdir(parents=True)
        ended, open_ = "12345678-1234-4123-8123-123456789abc", "abcdef01-2345-4678-9abc-def012345678"

        def write(name: str, event: str) -> None:
            (records / f"{name}.json").write_text(json.dumps({"version": sessions.RECORD_VERSION,
                                                              "last_event_source": event}))

        write(ended, "end")
        write(open_, "launch")
        (records / "notes.json").write_text("{}")  # not a session record
        (records / "fedcba98-7654-4321-8fed-cba987654321.json").write_text("not json")
        result = self.fixture("ended", "--install", str(self.installed()), "--timeout", "1", env=environ)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(sorted(result.stderr.splitlines()), [
            "journey_fixture: session abcdef01 has not recorded its end after 1 s (last event: launch)",
            "journey_fixture: session fedcba98 has not recorded its end after 1 s (last event: unreadable record)"])
        self.assertNotIn(open_, result.stderr)  # metadata only: the id's first eight characters
        (records / "fedcba98-7654-4321-8fed-cba987654321.json").unlink()
        timer = threading.Timer(1.0, write, (open_, "end"))
        timer.start()
        self.addCleanup(timer.cancel)
        result = self.fixture("ended", "--install", str(self.installed()), "--timeout", "30", env=environ)
        self.assertEqual((result.returncode, result.stdout), (0, "2 managed session record(s), every one ended\n"),
                         result.stderr)

    def get(self, url: str, context=None) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(url, timeout=30, context=context) as response:  # noqa: S310 - loopback
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code, b""

    def test_serve_files_serves_the_directory_only(self) -> None:
        served = self.tmp / "served"
        (served / "sub").mkdir(parents=True)
        (served / "install.sh").write_text("#!/bin/sh\necho installer\n")
        (served / ".hidden").write_text("no")
        (served / "sub" / "deep").write_text("no")
        (served / "link").symlink_to(served / "install.sh")
        self.background("serve-files", "--dir", str(served), "--port-file", str(self.tmp / "port"))
        base = f"http://127.0.0.1:{self.port(self.tmp / 'port')}"
        self.assertEqual(self.get(base + "/install.sh"), (200, b"#!/bin/sh\necho installer\n"))
        for path in ("/", "/sub/deep", "/.hidden", "/link", "/missing", "/../served/install.sh"):
            with self.subTest(path):
                self.assertEqual(self.get(base + path)[0], 404)
        usage = self.fixture("serve-files", "--dir", str(served), "--port-file", str(self.tmp / "p2"),
                             "--cert", str(served / "install.sh"))
        self.assertEqual(usage.returncode, 2)

    def test_serve_files_over_https_for_a_trusted_authority(self) -> None:
        import ssl

        if shutil.which("openssl") is None:
            self.skipTest("BOUNDARY: openssl is not installed")
        tls = self.tmp / "tls"
        tls.mkdir()

        def openssl(*args: str) -> None:
            subprocess.run(["openssl", *args], cwd=tls, check=True, capture_output=True, timeout=60,
                           stdin=subprocess.DEVNULL)

        # The same authority and certificate .github/scripts/wsl_prepare.sh makes.
        openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
                "-subj", "/CN=claude-multi journey loopback CA", "-keyout", "ca.key", "-out", "ca.pem",
                "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign")
        openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-subj", "/CN=localhost",
                "-keyout", "server.key", "-out", "server.csr")
        (tls / "ext").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")
        openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAserial", "ca.srl",
                "-CAcreateserial", "-days", "2", "-extfile", "ext", "-out", "server.pem")
        served = self.tmp / "served"
        served.mkdir()
        (served / "install.sh").write_text("installer\n")
        self.background("serve-files", "--dir", str(served), "--port-file", str(self.tmp / "port"),
                        "--cert", str(tls / "server.pem"), "--key", str(tls / "server.key"))
        port = self.port(self.tmp / "port")
        trusted = ssl.create_default_context(cafile=str(tls / "ca.pem"))
        self.assertEqual(self.get(f"https://localhost:{port}/install.sh", trusted), (200, b"installer\n"))
        with self.assertRaises(urllib.error.URLError) as refused:
            self.get(f"https://localhost:{port}/install.sh", ssl.create_default_context())  # without the authority
        self.assertIsInstance(refused.exception.reason, ssl.SSLCertVerificationError)
        import http.client

        with self.assertRaises((OSError, http.client.HTTPException)):
            self.get(f"http://127.0.0.1:{port}/install.sh")  # https only

    def test_answer_keeps_the_credentials_at_the_uninstall_question(self) -> None:
        try:
            os.close(os.openpty()[0])
        except OSError as exc:
            self.skipTest(f"BOUNDARY: no pseudo-terminal here ({exc})")
        from claude_multi.setup import texts

        asks = (f'printf "{texts.UNINSTALL_CREDENTIALS_QUESTION}" >&2; read a; [ -z "$a" ] || exit 5; '
                'echo kept; exit 0')
        result = self.fixture("answer", "--", "sh", "-c", asks)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("kept", result.stdout)


@requires_release_tree
@needs_ssh_keygen("BOUNDARY: ssh-keygen is not installed (the journey signs with it)")
class JourneyScriptTests(unittest.TestCase):
    """The CI journey on fake releases (no gateway in them, so its lifecycle
    is left out here; CI runs it on real bundles). The fake bundles carry
    this checkout's package and real launchers: the first run and the
    uninstall run for real."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-journey-script-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir(mode=0o700)
        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        (self.fakebin / "uname").write_text('#!/bin/sh\ncase $1 in -m) echo x86_64;; -r) echo 6.1.0;; *) echo Linux;; esac\n')
        os.chmod(self.fakebin / "uname", 0o755)

    def run_journey(self, *args: str, uninstall: bool = False, target: str | None = None
                    ) -> subprocess.CompletedProcess[str]:
        # The caller's PATH stays (the signing tools may live anywhere, as in
        # the Nix sandbox); only uname is a stand-in.
        path = f"{self.fakebin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"
        env = {"HOME": str(self.home), "PATH": path, "LC_ALL": "C", "TMPDIR": str(self.tmp),
               "JOURNEY_GATEWAY": "skip", "SHELL": "/bin/sh"}
        if not uninstall:
            env["JOURNEY_UNINSTALL"] = "skip"
        if target is not None:
            env["JOURNEY_TARGET"] = target
        return subprocess.run(["sh", str(JOURNEY), *args], env=env, cwd=self.tmp, capture_output=True, text=True,
                              timeout=300, stdin=subprocess.DEVNULL, start_new_session=True)

    FIRST_RUN_OK = ("first run: ok, not ready only for what a fresh install has not set up yet "
                    "(claude, gateway, providers, models, profile, hooks)")

    def test_the_install_journey(self) -> None:
        try:
            os.close(os.openpty()[0])
        except OSError as exc:
            self.skipTest(f"BOUNDARY: no pseudo-terminal here, which uninstall needs ({exc})")
        release = FakeRelease(self.tmp / "dist", "1.0.0", real_launchers=True)
        result = self.run_journey("install", str(release.dir), uninstall=True, target="linux-x86_64")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for step in ("bundle target: linux-x86_64", "fresh install: ok", "version: claude-multi 1.0.0 (catalog",
                     self.FIRST_RUN_OK, "installation channel (installed 1.0.0): bundle",
                     "managed sessions ended: ok (0 managed session record(s), every one ended)",
                     "uninstall keeping the state: ok", "done"):
            self.assertIn(step, result.stdout)
        self.assertFalse((self.home / ".local/bin/claude-multi").exists())
        self.assertFalse((self.home / ".local/share/claude-multi/install/current").exists())
        self.assertTrue((self.home / ".local/state/claude-multi").is_dir())

    # Launchers whose first-run checks fail for a reason no fresh
    # installation has (exit 71 with a report that says ready).
    FAILED_FIRST_RUN = """#!/bin/sh
case $1 in
--version) echo "{name} {version} (catalog 1; a launcher whose first run fails)"; exit 0 ;;
doctor)
  if [ "${{3:-}}" = --json ]; then
    printf '{{"version": 1, "ready": true, "next": null, "info": [], "items": ['
    sep=''
    for id in computer files policy claude gateway providers models profile hooks; do
      printf '%s{{"id": "%s", "state": "ok", "detail": "fine", "fix": null}}' "$sep" "$id"; sep=', '
    done
    printf ']}}\\n'
  else printf 'claude-multi doctor --first-run\\nReady.\\n'; fi
  exit 71 ;;
esac
exit 71
"""

    def test_the_journey_requires_a_successful_first_run(self) -> None:
        release = FakeRelease(self.tmp / "dist", "1.0.0", launcher=self.FAILED_FIRST_RUN)
        result = self.run_journey("install", str(release.dir))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("journey: FAILED: doctor's first run", result.stderr)
        self.assertIn("the first run failed: doctor --first-run --json exited 71 with ready true", result.stderr)
        self.assertNotIn("journey: done", result.stdout)

    def test_the_update_journey(self) -> None:
        keys = self.tmp / "keys"
        keys.mkdir()
        key = make_key(keys, "key")
        (keys / "allowed_signers").write_text(signers_line(key))
        trust = signers_line(key)
        first = FakeRelease(self.tmp / "a", "1.0.0", real_launchers=True, trust=trust)
        second = FakeRelease(self.tmp / "b", "1.0.1", real_launchers=True, trust=trust)
        result = self.run_journey("update", str(keys), str(first.dir), str(second.dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for step in ("update check: ok", "version: claude-multi 1.0.1 (", "update and up to date: ok", "rollback: ok"):
            self.assertIn(step, result.stdout)
        channels = re.findall(r"(?m)^journey: installation channel \((.*)\): bundle$", result.stdout)
        self.assertEqual(channels, ["installed 1.0.0", "update check", "updated to 1.0.1", "up to date",
                                    "rolled back to 1.0.0"])

    def test_another_bundle_target_fails_the_install(self) -> None:
        release = FakeRelease(self.tmp / "dist", "1.0.0", real_launchers=True)
        result = self.run_journey("install", str(release.dir), target="linux-aarch64")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("journey: FAILED: install.sh did not install the linux-aarch64 bundle", result.stderr)

    def test_prepare_signs_a_copy_with_its_installer(self) -> None:
        release = FakeRelease(self.tmp / "dist", "1.0.0", real_launchers=True)
        (release.dir / "install.sh").write_text((REPO_ROOT / "packaging" / "install.sh").read_text())
        out = self.tmp / "prepared"
        result = self.run_journey("prepare", str(release.dir), str(out))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sorted(p.name for p in (out / "key").iterdir()), ["allowed_signers", "key", "key.pub"])
        names = sorted(p.name for p in (out / "release").iterdir())
        self.assertIn("SHA256SUMS.sshsig", names)
        self.assertIn("install.sh", names)
        verified = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", str(out / "key" / "allowed_signers"), "-I", "release@claude-multi",
             "-n", "claude-multi-release", "-s", str(out / "release" / "SHA256SUMS.sshsig")],
            stdin=open(out / "release" / "SHA256SUMS", "rb"), capture_output=True, timeout=60)
        self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_a_failure_stops_the_journey(self) -> None:
        result = self.run_journey("bogus")
        self.assertEqual(result.returncode, 1)
        self.assertIn("journey: FAILED: usage", result.stderr)


class RequiredReleaseTestsTests(unittest.TestCase):
    """``needs_ssh_keygen``: a release integration test skips without
    ssh-keygen, unless CLAUDE_MULTI_TEST_REQUIRE_RELEASE=1 requires it (the
    CI battery sets it), and then it fails naming the missing tool."""

    @staticmethod
    def outcome(case: type) -> unittest.TestResult:
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
        return result

    def test_a_required_release_test_fails_without_ssh_keygen(self) -> None:
        import _fake_release

        def cases() -> tuple[type, type]:
            @_fake_release.needs_ssh_keygen()
            class Whole(unittest.TestCase):
                @classmethod
                def setUpClass(cls) -> None:
                    raise RuntimeError("never reached: the class is decided first")

                def test_one(self) -> None:
                    pass

            class Single(unittest.TestCase):
                @_fake_release.needs_ssh_keygen("BOUNDARY: ssh-keygen is not installed (the journey signs with it)")
                def test_one(self) -> None:
                    pass

            return Whole, Single

        with mock.patch.object(_fake_release, "SSH_KEYGEN", None):
            with mock.patch.dict(os.environ, {_fake_release.REQUIRE_RELEASE_ENV: ""}):
                optional = cases()
            with mock.patch.dict(os.environ, {_fake_release.REQUIRE_RELEASE_ENV: "1"}):
                required = cases()
        for case in optional:
            with self.subTest(case.__name__, required=False):
                result = self.outcome(case)
                self.assertEqual((len(result.skipped), len(result.errors), len(result.failures)), (1, 0, 0))
                self.assertTrue(result.skipped[0][1].startswith("BOUNDARY: ssh-keygen is not installed"))
        for case in required:
            with self.subTest(case.__name__, required=True):
                result = self.outcome(case)
                self.assertFalse(result.skipped)
                problems = result.errors + result.failures
                self.assertEqual(len(problems), 1)
                self.assertIn("CLAUDE_MULTI_TEST_REQUIRE_RELEASE=1 requires this release test, but ssh-keygen is not "
                              "installed", problems[0][1])
        with mock.patch.object(_fake_release, "SSH_KEYGEN", "/usr/bin/ssh-keygen"):
            with mock.patch.dict(os.environ, {_fake_release.REQUIRE_RELEASE_ENV: "1"}):
                present = cases()
        self.assertEqual([len(self.outcome(case).skipped) for case in present[1:]], [0])

    def test_the_ci_battery_requires_them(self) -> None:
        ci = (WORKFLOWS_DIR / "ci.yml").read_text().split("\njobs:\n", 1)[0]
        self.assertIn('\n  CLAUDE_MULTI_TEST_REQUIRE_RELEASE: "1"\n', ci)

    def test_the_sandbox_check_requires_them_and_supplies_ssh_keygen(self) -> None:
        check = (REPO_ROOT / "tests" / "default.nix").read_text()
        self.assertIn('\n    env.CLAUDE_MULTI_TEST_REQUIRE_RELEASE = "1";\n', check)
        inputs = re.search(r"(?m)^\s*nativeBuildInputs = \[(.*?)\];$", check, re.S)
        self.assertIsNotNone(inputs)
        # openssh's and openssl's binaries only: as plain inputs the build
        # would take their dev outputs, which the check does not need.
        self.assertIn("(pkgs.lib.getBin pkgs.openssh)", inputs.group(1))
        self.assertIn("(pkgs.lib.getBin pkgs.openssl)", inputs.group(1))
        self.assertNotRegex(inputs.group(1), r"(?<!getBin )pkgs\.open(ssh|ssl)\b")


@requires_release_tree
class VulncheckTests(unittest.TestCase):
    """Exact dispositions over a fake release and a hermetic scanner stub."""

    IDS = {"GO-2026-5841", "GO-2026-5932", "GO-2026-6213", "GO-2026-6214",
           "GO-2026-6303", "GO-2026-6354", "GO-2026-6355"}
    TARGETS = ("linux-x86_64", "linux-aarch64", "darwin-x86_64", "darwin-arm64")

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = load_tool(VULNCHECK, "vulncheck")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-vulncheck-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        binary = b"a fake gateway binary\n"
        self.release = FakeRelease(self.tmp / "dist", "1.0.0", targets=self.TARGETS,
                                   gateway=hashlib.sha256(binary).hexdigest(), gateway_binary=binary)
        self.policy = json.loads((REPO_ROOT / "gateway/vulnerability-dispositions.json").read_text())
        self.policy["gateway"]["targets"] = {target: hashlib.sha256(binary).hexdigest() for target in self.TARGETS}
        manifest_path = self.release.dir / "MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["gateway"] = {"version": self.policy["gateway"]["version"]}
        manifest_path.write_text(json.dumps(manifest))
        self.messages = [
            {"config": {"protocol_version": "v1.0.0", "scanner_name": "govulncheck", "scanner_version": "v1.8.0",
                        "db": "file:///synthetic-db", "db_last_modified": "2026-10-01T20:24:15Z",
                        "scan_level": "symbol", "scan_mode": "binary"}},
            {"progress": {"message": "Checking the binary against the vulnerabilities..."}},
            {"SBOM": {"go_version": "go1.26.8", "modules": [
                {"path": module, "version": version} for module, version in sorted({
                    (entry["module"], entry["version"]) for entry in self.policy["dispositions"]})]}},
        ]
        for entry in self.policy["dispositions"]:
            advisory = {"id": entry["id"], "modified": entry["advisory_modified"], "summary": "synthetic advisory"}
            entry["advisory_sha256"] = hashlib.sha256(
                json.dumps(advisory, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            trace = [{"module": entry["module"], "version": entry["version"]}]
            entry["trace_count"] = 1
            entry["trace_sha256"] = hashlib.sha256(
                json.dumps([trace], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self.messages.extend([{"osv": advisory}, {"finding": {"osv": entry["id"], "trace": trace}}])
        self.policy_file = self.tmp / "dispositions.json"
        self.stream_file = self.tmp / "scanner.json"
        self.stub = self.tmp / "govulncheck"
        self.stub.write_text(f"#!{sys.executable}\n" + '''import os, sys
from pathlib import Path
mode = os.environ.get("STUB_MODE", "reviewed")
if "-version" in sys.argv:
    if mode == "version-fail":
        sys.exit(1)
    print("Scanner: govulncheck@v1.8.0")
    if mode != "no-db":
        print("DB: file:///synthetic-db")
    sys.exit(0)
print(Path(os.environ["STUB_STREAM"]).read_text())
if mode == "fail":
    print("database unreachable", file=sys.stderr)
    sys.exit(1)
''')
        os.chmod(self.stub, 0o755)

    def run_check(self, mode: str = "reviewed", *, tool: Path | None = None,
                  stream: str | None = None, policy: str | None = None,
                  omit_policy: bool = False) -> tuple[int, dict, str]:
        self.policy_file.write_text(json.dumps(self.policy) if policy is None else policy)
        self.stream_file.write_text("\n".join(json.dumps(m) for m in self.messages) if stream is None else stream)
        out = self.tmp / "evidence" / "vulncheck.json"
        output = io.StringIO()
        argv = ["--dist", str(self.release.dir), "--out", str(out), "--govulncheck", str(tool or self.stub)]
        if not omit_policy:
            argv.extend(["--dispositions", str(self.policy_file)])
        with mock.patch.dict(os.environ, {"STUB_MODE": mode, "STUB_STREAM": str(self.stream_file)}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = self.tool.main(argv)
        self.assertTrue(out.is_file(), "failed checks must still produce evidence")
        return code, json.loads(out.read_text()), output.getvalue()

    def test_exact_seven_reviewed_findings_pass_without_hiding_them(self) -> None:
        code, report, output = self.run_check()
        self.assertEqual(code, 0, report)
        self.assertEqual({entry["target"] for entry in report["targets"]}, set(self.TARGETS))
        for entry in report["targets"]:
            self.assertEqual(entry["result"], "reviewed")
            self.assertEqual(set(entry["findings"]), self.IDS)
            self.assertEqual(len(entry["raw_findings"]), 7)
            self.assertEqual({item["id"] for item in entry["reviewed_dispositions"]}, self.IDS)
            self.assertEqual(entry["unreviewed_findings"], [])
            self.assertEqual(entry["scanner_stdout"].strip(), self.stream_file.read_text())
        self.assertIn("reviewed dispositions: 7", output)
        self.assertNotIn("no vulnerabilities", output.lower())

    def test_unknown_findings_remain_blocking_and_visible(self) -> None:
        self.messages.append({"finding": {"osv": "GO-2026-9999", "trace": [{"module": "unknown", "version": "v1"}]}})
        code, report, _ = self.run_check()
        self.assertEqual(code, 1)
        for entry in report["targets"]:
            self.assertEqual(entry["unreviewed_findings"], ["GO-2026-9999"])
            self.assertEqual(len(entry["reviewed_dispositions"]), 7)

    def test_module_and_reported_trace_changes_require_review(self) -> None:
        original = json.dumps(self.messages)
        for change in (lambda: self.messages[4]["finding"]["trace"][0].update(version="v9.0.0"),
                       lambda: self.messages[4]["finding"]["trace"][0].update(module="another/module"),
                       lambda: self.messages[4]["finding"]["trace"][0].update(package="new/package"),
                       lambda: self.messages[4]["finding"]["trace"][0].update(function="NewFunction"),
                       lambda: self.messages[4]["finding"]["trace"].append({"module": "caller", "function": "Call"}),
                       lambda: self.messages[2]["SBOM"]["modules"][0].update(version="v9.0.0")):
            self.messages = json.loads(original)
            change()
            with self.subTest(messages=self.messages[3:5]):
                code, report, _ = self.run_check()
                self.assertEqual(code, 1, report)
                self.assertTrue(all(entry["problems"] for entry in report["targets"]))

    def test_advisory_only_edits_warn_and_pass(self) -> None:
        original = json.dumps(self.messages)
        for change in (lambda: self.messages[3]["osv"].update(modified="2026-10-05T00:00:00Z"),
                       lambda: self.messages[3]["osv"].update(aliases=["CVE-2026-00001"]),
                       lambda: self.messages[3]["osv"].update(summary="changed at same timestamp")):
            self.messages = json.loads(original)
            change()
            self.messages[4]["finding"]["fixed_version"] = "v9.0.0"
            code, report, output = self.run_check()
            self.assertEqual(code, 0, report)
            self.assertIn("WARNING", output)
            self.assertIn("GO-2026-5841", output)
            self.assertIn(self.policy["dispositions"][0]["advisory_modified"], output)
            self.assertIn(self.messages[3]["osv"]["modified"], output)
            for entry in report["targets"]:
                self.assertEqual(len(entry["reviewed_dispositions"]), 7)
                self.assertEqual(len(entry["warnings"]), 1)
                self.assertNotEqual(entry["advisory_revisions"][0]["reviewed_sha256"],
                                    entry["advisory_revisions"][0]["current_sha256"])

    def test_reported_symbols_are_bound_but_trace_order_is_not(self) -> None:
        identifier = self.policy["dispositions"][0]["id"]
        trace = [{"module": "github.com/klauspost/compress", "version": "v1.17.4",
                  "package": "github.com/klauspost/compress/s2", "function": "NewDict"}]
        self.messages.append({"finding": {"osv": identifier, "trace": trace}})
        traces = [self.messages[4]["finding"]["trace"], trace]
        canonical = sorted(json.dumps(value, sort_keys=True, separators=(",", ":")) for value in traces)
        self.policy["dispositions"][0].update(trace_count=2, trace_sha256=hashlib.sha256(
            json.dumps([json.loads(value) for value in canonical], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest())
        self.assertEqual(self.run_check()[0], 0)
        self.messages[4], self.messages[-1] = self.messages[-1], self.messages[4]
        self.assertEqual(self.run_check()[0], 0)
        self.messages[4]["finding"]["trace"][0]["function"] = "OtherFunction"
        code, report, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertTrue(all(identifier in entry["unreviewed_findings"] for entry in report["targets"]))

    def test_malformed_or_incomplete_scans_never_pass(self) -> None:
        streams = ["", "not json", "[]", '{"config":{}}', '{"finding":null}',
                   '{"finding":{"osv":"GO-2026-5841","trace":[]}}', '{"error":{"message":"failed"}}']
        streams.extend("\n".join(json.dumps(m) for m in self.messages[:end]) for end in (1, 2, 3, 4, 5, 16))
        for index in (0, 1, 2, 3):
            streams.append("\n".join(json.dumps(m) for i, m in enumerate(self.messages) if i != index))
        streams.append("\n".join(json.dumps(m) for m in self.messages) + '\n{"finding":')
        for stream in streams:
            with self.subTest(stream=stream[:100]):
                code, report, _ = self.run_check(stream=stream)
                self.assertEqual(code, 1, report)
                self.assertTrue(all(entry["scanner_stdout"] is not None for entry in report["targets"]))

    def test_malformed_missing_and_wildcard_dispositions_refuse(self) -> None:
        original = json.dumps(self.policy)
        for change in (lambda: self.policy.update(format=99), lambda: self.policy.update(dispositions=[]),
                       lambda: self.policy["dispositions"].append(self.policy["dispositions"][0]),
                       lambda: self.policy["dispositions"][0].update(id="GO-*"),
                       lambda: self.policy["dispositions"][0].update(module="*"),
                       lambda: self.policy["dispositions"][0].update(version="*"),
                       lambda: self.policy["dispositions"][0].update(evidence=[]),
                       lambda: self.policy["dispositions"][0].update(re_review=[]),
                       lambda: self.policy["dispositions"][0].pop("advisory_modified"),
                       lambda: self.policy["dispositions"][0].update(advisory_sha256="x"),
                       lambda: self.policy["dispositions"][0].update(**{"class": "ignored"}),
                       lambda: self.policy["gateway"]["targets"].pop("darwin-arm64")):
            self.policy = json.loads(original)
            change()
            with self.subTest(policy=self.policy):
                code, report, _ = self.run_check()
                self.assertEqual(code, 1, report)
                self.assertTrue(report["problems"])
        self.policy = json.loads(original)
        for policy in ("[]", "{}", "not json", '{"format":1,"format":1}'):
            self.assertEqual(self.run_check(policy=policy)[0], 1)
        self.assertEqual(self.run_check(omit_policy=True)[0], 1)
        with mock.patch.object(Path, "read_text", side_effect=FileNotFoundError("missing disposition file")):
            report, ok = self.tool.check(self.release.dir, str(self.stub), self.policy_file)
        self.assertFalse(ok)
        self.assertTrue(report["problems"])

    def test_scanner_database_and_process_failures_keep_evidence(self) -> None:
        for mode in ("fail", "version-fail", "no-db"):
            with self.subTest(mode=mode):
                code, report, _ = self.run_check(mode)
                self.assertEqual(code, 1, report)
        self.assertEqual(self.run_check(tool=self.tmp / "missing")[0], 1)
        real_run = self.tool._run
        for error in (OSError("cannot exec"), subprocess.TimeoutExpired("govulncheck", 900)):
            def run(argv):
                if "-version" in argv:
                    return real_run(argv)
                with mock.patch.object(subprocess, "run", side_effect=error):
                    return real_run(argv)
            with mock.patch.object(self.tool, "_run", side_effect=run):
                code, report, _ = self.run_check()
            self.assertEqual(code, 1, report)
            self.assertEqual({entry["result"] for entry in report["targets"]}, {"error"})

    def test_missing_target_gateway_version_and_hash_mismatch_refuse(self) -> None:
        path = self.release.dir / "MANIFEST.json"
        original = path.read_text()
        for change in (lambda d: d["assets"].pop(next(iter(d["assets"]))),
                       lambda d: d["gateway"].update(version="9.0.0"),
                       lambda d: next(iter(d["assets"].values())).update(gateway_sha256="f" * 64),
                       lambda d: next(iter(d["assets"].values())).update(target="../outside")):
            manifest = json.loads(original)
            change(manifest)
            path.write_text(json.dumps(manifest))
            code, report, _ = self.run_check()
            self.assertEqual(code, 1, report)
            self.assertTrue(report["problems"])
        path.write_text(original)
        self.policy["gateway"]["targets"]["darwin-arm64"] = "f" * 64
        self.assertEqual(self.run_check()[0], 1)
        self.policy["gateway"]["targets"]["darwin-arm64"] = hashlib.sha256(b"a fake gateway binary\n").hexdigest()
        archive = next(iter(json.loads(original)["assets"]))
        (self.release.dir / archive).unlink()
        self.assertEqual(self.run_check()[0], 1)

    def test_public_policy_and_blocking_workflow_are_bound(self) -> None:
        policy = json.loads((REPO_ROOT / "gateway/vulnerability-dispositions.json").read_text())
        self.assertEqual({entry["id"] for entry in policy["dispositions"]}, self.IDS)
        self.assertEqual(set(policy["gateway"]["targets"]), set(self.TARGETS))
        upstream = json.loads((REPO_ROOT / "gateway/UPSTREAM.json").read_text())
        self.assertEqual(policy["gateway"]["version"], upstream["upstream"]["version"])
        job = _jobs((WORKFLOWS_DIR / "release.yml").read_text())["vulnerabilities"]
        self.assertIn("--dispositions gateway/vulnerability-dispositions.json", job)
        self.assertIn("ref: ${{ needs.resolve.outputs.sha }}", job)
        self.assertNotIn("continue-on-error", job)
        self.assertIn("actions/upload-artifact@", job)
        self.assertIn("if: always() && steps.guard.outcome == 'success'", job)


@requires_release_tree
class PinWatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = load_tool(PIN_WATCH, "pin_watch")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-pinwatch-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        for relative in ("src/claude_multi/data/catalog/native-contract.json", "gateway/UPSTREAM.json",
                         "packaging/python-runtimes.json"):
            (self.repo / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / relative, self.repo / relative)

    def test_the_pins_come_from_the_recipe_fields(self) -> None:
        pins = self.tool.pins(self.repo)
        upstream = json.loads((REPO_ROOT / "gateway/UPSTREAM.json").read_text())
        self.assertEqual(pins["go"], upstream["toolchain"]["version"])
        self.assertEqual(pins["gateway"], upstream["upstream"]["version"])
        self.assertEqual(set(pins), set(self.tool.NAMES))

    def test_absent_empty_or_malformed_pins_refuse(self) -> None:
        path = self.repo / "gateway/UPSTREAM.json"
        original = path.read_text()
        for change in (lambda d: d["toolchain"].pop("version"), lambda d: d["toolchain"].update(version=""),
                       lambda d: d["toolchain"].update(version="go1.26"), lambda d: d.pop("toolchain"),
                       lambda d: d["upstream"].update(version=None)):
            document = json.loads(original)
            change(document)
            path.write_text(json.dumps(document))
            with self.subTest(document=document.get("toolchain")), self.assertRaises(self.tool.PinError):
                self.tool.pins(self.repo)
        path.write_text(original)

    def test_compare(self) -> None:
        pins = self.tool.pins(self.repo)
        same = "\n".join(f"{k}={'v' if k == 'gateway' else ''}{v}" for k, v in pins.items() if k != "python")
        self.assertEqual(self.tool.compare(self.repo, same), [])
        major, minor, patch = pins["go"].split(".")
        newer = same.replace(f"go={pins['go']}", f"go=go{major}.{int(minor) + 1}.0")
        self.assertEqual(self.tool.compare(self.repo, newer), [("go", pins["go"], f"{major}.{int(minor) + 1}.0")])
        for broken in (same.replace(f"go={pins['go']}", "go="), "\n".join(same.splitlines()[1:]),
                       same.replace(f"go={pins['go']}", "go=banana")):
            with self.subTest(broken=broken.splitlines()[:1]), self.assertRaises(self.tool.PinError):
                self.tool.compare(self.repo, broken)


@requires_release_tree
class NoBundleGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.guard = load_tool(NO_BUNDLE_GUARD, "no_bundle_guard")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-guard-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pinned = b"pretend this is the pinned client"
        self.contract = self.tmp / "contract.json"
        self.contract.write_text(json.dumps({"claude": {"verified": [
            {"platform": "linux-x64", "sha256": hashlib.sha256(self.pinned).hexdigest(), "size": 1}]}}))
        self.dist = self.tmp / "dist"
        self.dist.mkdir()
        (self.dist / "SHA256SUMS").write_text("x\n")

    def run_guard(self, *paths: Path, max_size: int | None = None) -> tuple[int, str]:
        argv = ["--contract", str(self.contract), *(str(p) for p in paths)]
        if max_size is not None:
            argv += ["--max-size", str(max_size)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.guard.main(argv)
        return code, out.getvalue() + err.getvalue()

    def _tar(self, name: str, members: dict[str, bytes]) -> Path:
        path = self.dist / name
        with tarfile.open(path, "w:gz") as tar:
            for member, data in members.items():
                info = tarfile.TarInfo(member)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def test_clean_artifacts_pass(self) -> None:
        self._tar("claude-multi-1.0.0-linux-x86_64.tar.gz", {"claude-multi-1.0.0-linux-x86_64/bin/claude-multi": b"#!/bin/sh\n"})
        code, output = self.run_guard(self.dist)
        self.assertEqual(code, 0, output)
        self.assertIn("1 pinned hashes checked", output)

    def test_findings(self) -> None:
        self._tar("bundle.tar.gz", {"top/libexec/claude": b"x", "top/copy": self.pinned})
        with zipfile.ZipFile(self.dist / "cache.zip", "w") as archive:
            archive.writestr("deep/claude.exe", b"y")
        (self.dist / "renamed-client").write_bytes(self.pinned)
        (self.dist / "broken.tar.gz").write_bytes(b"not an archive")
        code, output = self.run_guard(self.dist)
        self.assertEqual(code, 1)
        for needle in ("bundle.tar.gz!top/libexec/claude: named like a Claude Code executable",
                       "bundle.tar.gz!top/copy: matches a pinned Claude Code sha256",
                       "cache.zip!deep/claude.exe: named like a Claude Code executable",
                       "renamed-client: matches a pinned Claude Code sha256",
                       "broken.tar.gz: unreadable archive"):
            self.assertIn(needle, output)

    def test_size_cap_and_usage(self) -> None:
        (self.dist / "big").write_bytes(b"0" * 2048)
        code, output = self.run_guard(self.dist, max_size=1024)
        self.assertEqual(code, 1)
        self.assertIn("2048 bytes is larger than 1024", output)
        self.assertEqual(self.run_guard(self.tmp / "missing")[0], 2)

    def test_the_shipped_contract_yields_hashes(self) -> None:
        self.assertTrue(self.guard.pinned_hashes(RESOURCES_ROOT / "catalog" / "native-contract.json"))
        self.assertEqual(self.guard.DEFAULT_CONTRACT, RESOURCES_ROOT / "catalog" / "native-contract.json")

    def test_a_contract_without_pinned_hashes_fails_closed(self) -> None:
        (self.dist / "file").write_bytes(b"x")
        self.contract.write_text(json.dumps({"claude": {"verified": []}}))
        code, output = self.run_guard(self.dist)
        self.assertEqual(code, 2)
        self.assertIn("pins no Claude Code sha256", output)
        self.contract.unlink()
        code, output = self.run_guard(self.dist)
        self.assertEqual(code, 2)
        self.assertIn("cannot read", output)



def _gateway_probes():
    return load_tool(SCRIPTS / "gateway_probes.py", "gateway_probes")


# The diagnostic probes of a gateway build, by file name, as
# tests/gateway-startup-probe.nix builds them: the startup probe (which the
# test variable selects) first, then the omission probes beside it.
PROBE_NAMES = tuple(probe["name"] for probe in _gateway_probes().recipe()[1])


class ContentBoundGatewayTests(unittest.TestCase):
    """tests/_gateway.py: a gateway outside the Nix store is accepted only
    when it is the content-bound build its record names (and only when it
    is selected explicitly); the diagnostic probes built against it are
    bound the same way. Store paths keep their rule."""

    BINARY = "#!/bin/sh\necho 'CLIProxyAPI Version: 7.3.15, Commit: fixture'\n"

    def setUp(self) -> None:
        import _gateway

        self.gw = _gateway
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-bound-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        if _gateway.unsafe_location(self.tmp):
            self.skipTest(f"BOUNDARY: the temporary directory is not private here ({_gateway.unsafe_location(self.tmp)})")
        self.target = _gateway.host_target()
        if self.target is None:
            self.skipTest("BOUNDARY: no gateway target for this host")
        self.dist = self.tmp / "dist"
        self.binary = self.dist / self.target / "cli-proxy-api"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text(self.BINARY)
        self.binary.chmod(0o755)
        self.record = self.dist / "BUILD.json"
        self.write_record()

    def write_record(self, **changes) -> None:
        document = {"format": 1, "admitted_series": True, "series": self.gw.admitted_series(),
                    "targets": {self.target: {"sha256": self.gw.sha256_file(self.binary)}}}
        document.update(changes)
        self.record.write_text(json.dumps(document))

    def bound(self, candidate: Path | None = None, record: Path | None | str = "default") -> tuple[str | None, str]:
        record_path = self.record if record == "default" else record
        return self.gw.bound_gateway_problem(str(candidate or self.binary),
                                             None if record_path is None else str(record_path))

    def test_the_bound_build_is_accepted(self) -> None:
        self.assertEqual(self.bound(), (str(self.binary), ""))
        link = self.tmp / "link"
        link.symlink_to(self.binary)
        self.assertEqual(self.bound(link), (str(self.binary), ""))  # resolved, then checked

    def test_refusals(self) -> None:
        other = self.tmp / "other" / "cli-proxy-api"
        other.parent.mkdir()
        other.write_text(self.BINARY + "# another build\n")
        other.chmod(0o755)
        unbound_link = self.tmp / "unbound-link"
        unbound_link.symlink_to(other)
        self.assertIn("sha256 differs", self.bound(other)[1])
        self.assertIn("sha256 differs", self.bound(unbound_link)[1])
        self.assertIn(f"set {self.gw.BUILD_RECORD_ENV}", self.bound(record=None)[1])
        self.assertIn("cannot be read", self.bound(record=self.tmp / "absent.json")[1])
        record_link = self.tmp / "record-link.json"
        record_link.symlink_to(self.record)
        self.assertIn("is not a regular file", self.bound(record=record_link)[1])
        cases = {
            "names another patch series": dict(series=self.gw.admitted_series()[1:]),
            "names another patch series than": dict(admitted_series=False),
            "is not a gateway build record": dict(format=2),
            f"names no {self.target} gateway": dict(targets={}),
        }
        for message, changes in cases.items():
            with self.subTest(message):
                self.write_record(**changes)
                self.assertEqual(self.bound()[0], None)
                self.assertIn(message, self.bound()[1])
        self.write_record()
        reordered = list(reversed(self.gw.admitted_series()))
        self.write_record(series=reordered)
        self.assertIn("another patch series", self.bound()[1])
        self.write_record()
        self.binary.chmod(0o775)
        self.assertIn("is group- or other-writable", self.bound()[1])
        self.binary.chmod(0o755)
        self.binary.parent.chmod(0o777)
        try:
            self.assertIn(f"{self.binary.parent} is group- or other-writable", self.bound()[1])
        finally:
            self.binary.parent.chmod(0o700)
        self.record.chmod(0o666)
        self.assertIn("the build record:", self.bound()[1])
        self.record.chmod(0o600)
        self.binary.chmod(0o644)
        self.assertIn("is not a regular executable file", self.bound()[1])
        self.binary.chmod(0o755)
        self.assertEqual(self.bound()[1], "")

    def test_selection(self) -> None:
        """Only an explicit selection may name a build outside the store;
        an ambient one never satisfies a test."""

        version = mock.patch.object(self.gw, "_version", side_effect=lambda real: (str(real), "7.3.15"))
        with version, mock.patch.dict(os.environ, {self.gw.BINARY_ENV: str(self.binary),
                                                   self.gw.BUILD_RECORD_ENV: str(self.record)}):
            self.assertEqual(self.gw._gateway_binary(), (str(self.binary), "7.3.15"))
        unbound = self.tmp / "unbound"
        unbound.write_text(self.BINARY + "# unbound\n")
        unbound.chmod(0o755)
        with version, mock.patch.dict(os.environ, {self.gw.BINARY_ENV: str(unbound),
                                                   self.gw.BUILD_RECORD_ENV: str(self.record)}):
            path, reason = self.gw._gateway_binary()
            self.assertIsNone(path)
            self.assertIn("sha256 differs", reason)
        with version, mock.patch.dict(os.environ, {self.gw.BINARY_ENV: str(self.binary)}):
            os.environ.pop(self.gw.BUILD_RECORD_ENV, None)
            self.assertEqual(self.gw._gateway_binary()[0], None)
        ambient = (mock.patch.object(self.gw, "_wrapper_gateway", return_value=None),
                   mock.patch.object(self.gw.shutil, "which", return_value=str(self.binary)))
        with version, ambient[0], ambient[1], mock.patch.dict(os.environ, {self.gw.BUILD_RECORD_ENV: str(self.record)}):
            os.environ.pop(self.gw.BINARY_ENV, None)
            path, reason = self.gw._gateway_binary()
            self.assertIsNone(path)
            self.assertIn("is not a /nix/store path", reason)

    def test_the_store_rule_is_unchanged(self) -> None:
        """A store path needs no build record; a writable one is refused."""

        store = self.tmp / "store"
        binary = store / "fixture-cli-proxy-api" / "bin" / "cli-proxy-api"
        binary.parent.mkdir(parents=True)
        binary.write_text(self.BINARY)
        binary.chmod(0o755)
        version = mock.patch.object(self.gw, "_version", side_effect=lambda real: (str(real), "7.3.15"))
        with version, mock.patch.object(self.gw, "NIX_STORE", str(store) + "/"), \
                mock.patch.dict(os.environ, {self.gw.BINARY_ENV: str(binary)}):
            os.environ.pop(self.gw.BUILD_RECORD_ENV, None)
            self.assertEqual(self.gw._gateway_binary(), (str(binary), "7.3.15"))
            binary.chmod(0o775)
            self.assertIn("group/other-writable", self.gw._gateway_binary()[1])

    def probes(self, *, gateway_sha256: str | None = None, series=None, names=None) -> Path:
        out = self.tmp / "probes"
        shutil.rmtree(out, ignore_errors=True)
        (out / "bin").mkdir(parents=True)
        (out / "share").mkdir()
        recorded = {}
        for name in PROBE_NAMES:
            path = out / "bin" / name
            path.write_text(f"#!/bin/sh\necho {name}\n")
            path.chmod(0o755)
            recorded[name] = self.gw.sha256_file(path)
        (out / "share" / "probe-build.json").write_text(json.dumps({
            "format": 1, "gateway_sha256": gateway_sha256 or self.gw.sha256_file(self.binary),
            "gateway_target": self.target, "series": self.gw.admitted_series() if series is None else series,
            "probes": names if names is not None else recorded}))
        return out

    def test_probes_are_bound_to_their_gateway(self) -> None:
        import _gateway_harness as harness

        startup_name, executor_name, handlers_name = PROBE_NAMES
        out = self.probes()
        startup = out / "bin" / startup_name
        for name in PROBE_NAMES:
            with self.subTest(name):
                self.assertIsNone(self.gw.diagnostic_problem(out / "bin" / name, str(self.binary)))
        with mock.patch.dict(os.environ, {harness.STARTUP_PROBE_ENV: str(startup)}):
            self.assertEqual(harness.selected_diagnostic(str(self.binary)), startup)
            self.assertEqual(harness.selected_diagnostic(str(self.binary), handlers_name), out / "bin" / handlers_name)
        other = self.probes(gateway_sha256="0" * 64)
        self.assertEqual(self.gw.diagnostic_problem(other / "bin" / startup_name, str(self.binary)),
                         "the diagnostic belongs to another gateway build")
        with mock.patch.dict(os.environ, {harness.STARTUP_PROBE_ENV: str(other / "bin" / startup_name)}):
            with self.assertRaises(AssertionError) as raised:
                harness.selected_diagnostic(str(self.binary))
            self.assertNotIsInstance(raised.exception, harness.DiagnosticMissing)  # a failure, never a skip
        series = self.probes(series=self.gw.admitted_series()[:-1])
        self.assertEqual(self.gw.diagnostic_problem(series / "bin" / startup_name, str(self.binary)),
                         "the diagnostic was built from another patch series")
        out = self.probes()
        (out / "bin" / executor_name).write_text("#!/bin/sh\necho replaced\n")
        self.assertIn("is not the probe its record names",
                      self.gw.diagnostic_problem(out / "bin" / executor_name, str(self.binary)))
        (out / "bin" / startup_name).chmod(0o775)
        self.assertIn("is group- or other-writable",
                      self.gw.diagnostic_problem(out / "bin" / startup_name, str(self.binary)))
        (out / "share" / "probe-build.json").unlink()
        self.assertIn("has no readable probe record",
                      self.gw.diagnostic_problem(out / "bin" / handlers_name, str(self.binary)))
        with mock.patch.dict(os.environ, {harness.STARTUP_PROBE_ENV: ""}):
            with self.assertRaises(harness.DiagnosticMissing):
                harness.selected_diagnostic(str(self.binary))

    def test_a_store_probe_names_its_gateway_output(self) -> None:
        store = self.tmp / "store"
        gateway = store / "gateway-out" / "bin" / "cli-proxy-api"
        probe = store / "probe-out" / "bin" / PROBE_NAMES[0]
        for path in (gateway, probe):
            path.parent.mkdir(parents=True)
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
        (store / "probe-out" / "share").mkdir()
        (store / "probe-out" / "share" / "gateway-outpath").write_text(f"{store / 'gateway-out'}\n")
        with mock.patch.object(self.gw, "NIX_STORE", str(store) + "/"):
            self.assertIsNone(self.gw.diagnostic_problem(probe, str(gateway)))
            self.assertEqual(self.gw.diagnostic_problem(probe, str(store / "elsewhere" / "bin" / "cli-proxy-api")),
                             "the diagnostic belongs to another gateway build")

    def test_evidence_names_a_bound_gateway_by_content(self) -> None:
        import _gateway_harness as harness

        evidence = harness.Evidence()
        evidence.binary = {"realpath": str(self.binary), "version": "7.3.15"}
        with self.assertRaisesRegex(AssertionError, "missing launched binary identity"):
            evidence.finalize_module("tests.fixture", self.tmp / "evidence")
        evidence.binary = dict(harness.binary_identity(str(self.binary), "7.3.15"))
        self.assertEqual(evidence.binary["sha256"], self.gw.sha256_file(self.binary))
        evidence.finalize_module("tests.fixture", self.tmp / "evidence")  # validated as it is written
        document = json.loads((self.tmp / "evidence" / "tests.fixture.json").read_text())
        with self.assertRaisesRegex(AssertionError, "missing launched binary identity"):
            harness.validate_evidence_document(document, "tests.fixture", store_only=True)  # published evidence
        stored = {"realpath": "/nix/store/fixture-cli-proxy-api/bin/cli-proxy-api", "version": "7.3.15"}
        self.assertEqual(harness.binary_identity(stored["realpath"], "7.3.15"), stored)
        harness.validate_evidence_document({**document, "binary": stored}, "tests.fixture", store_only=True)


def _job_condition(body: str) -> str | None:
    found = re.search(r"(?m)^    if: (.+)$", body)
    return found.group(1).strip() if found else None


def _evaluate(condition: str, event: str, variables: dict[str, str]) -> bool:
    """A job condition of the forms the workflows use (event names, vars,
    ==, !=, &&, ||, !, parentheses), evaluated for one event."""

    expression = condition.removeprefix("${{").removesuffix("}}").strip()
    expression = re.sub(r"\bvars\.(\w+)", lambda m: repr(variables.get(m.group(1), "")), expression)
    expression = expression.replace("github.event_name", repr(event))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    if not re.fullmatch(r"[\s()'\w.=!-]*", expression):
        raise ValueError(f"a condition this test cannot evaluate: {condition}")
    return bool(eval(expression, {"__builtins__": {}}, {}))  # noqa: S307 - checked to the grammar above


@requires_release_tree
class ScheduleTests(unittest.TestCase):
    """The nightly and weekly schedules run only while the repository
    variable CM_SCHEDULES is "on"; a manual run is unaffected."""

    def test_every_job_a_schedule_reaches_checks_the_variable(self) -> None:
        scheduled = []
        for name in WORKFLOWS:
            text = (WORKFLOWS_DIR / name).read_text()
            if "\n  schedule:\n" not in text.split("\njobs:\n", 1)[0]:
                continue
            for job, body in _jobs(text).items():
                with self.subTest(workflow=name, job=job):
                    condition = _job_condition(body)
                    self.assertIsNotNone(condition, "a job without a condition runs on every schedule")
                    self.assertFalse(_evaluate(condition, "schedule", {}))
                    self.assertFalse(_evaluate(condition, "schedule", {"CM_SCHEDULES": "off"}))
                    dispatch = _evaluate(condition, "workflow_dispatch", {})
                    self.assertEqual(dispatch, _evaluate(condition, "workflow_dispatch", {"CM_SCHEDULES": "on"}))
                    if _evaluate(condition, "schedule", {"CM_SCHEDULES": "on"}):
                        scheduled.append(f"{name}:{job}")
                        self.assertTrue(dispatch, "a scheduled job also runs on a manual run")
        self.assertEqual(sorted(scheduled), ["ci.yml:battery", "ci.yml:gateway", "ci.yml:nix", "ci.yml:pty",
                                             "pin-watch.yml:watch"])

    def test_the_evaluator_reads_the_guard(self) -> None:
        guard = "(github.event_name == 'schedule' && vars.CM_SCHEDULES == 'on') || github.event_name == 'workflow_dispatch'"
        self.assertTrue(_evaluate(guard, "schedule", {"CM_SCHEDULES": "on"}))
        self.assertFalse(_evaluate(guard, "schedule", {}))
        self.assertFalse(_evaluate(guard, "push", {"CM_SCHEDULES": "on"}))
        self.assertTrue(_evaluate("${{ !(github.event_name == 'schedule') }}", "push", {}))
        with self.assertRaises(ValueError):
            _evaluate("contains(github.event.head_commit.message, 'x')", "push", {})


def _module_produces(source: str, prefix: str, index: int | None) -> bool:
    """Whether a test module's source prints the essential completion line
    ``prefix``: literally, through ``ESSENTIAL_EVIDENCE_PREFIXES[index]``,
    or through a probe class's line template (``probe_id`` and, for a class
    line, the class name as a literal)."""

    if prefix in source or (index is not None and f"ESSENTIAL_EVIDENCE_PREFIXES[{index}]" in source):
        return True
    parsed = re.fullmatch(r"(.+?) ([\w.-]+): PASS(?: class=([\w-]+))? ", prefix)
    if parsed is None:
        return False
    family, probe_id, observed = parsed.groups()
    if not re.search(re.escape(family) + r" \{(?:cls|self)\.probe_id\}: ", source):
        return False
    if not re.search(rf'(?m)^\s+probe_id = "{re.escape(probe_id)}"$', source):
        return False
    return observed is None or f'"{observed}"' in source


def _unproduced(modules: list[str], prefixes=None, alternatives=None) -> list[str]:
    from claude_multi import upgrade

    prefixes = upgrade.ESSENTIAL_EVIDENCE_PREFIXES if prefixes is None else prefixes
    alternatives = upgrade.ESSENTIAL_EVIDENCE_ALTERNATIVES if alternatives is None else alternatives
    sources = [(REPO_ROOT / "tests" / f"{module.removeprefix('tests.')}.py").read_text() for module in modules]
    lines = [(prefix, index) for index, prefix in enumerate(prefixes)]
    lines += [(alternative, None) for prefix in prefixes for alternative in alternatives.get(prefix, ())]
    return [line for line, index in lines if not any(_module_produces(source, line, index) for source in sources)]


@requires_release_tree
class EvidenceJobTests(unittest.TestCase):
    """The release evidence job: every producer of the essential completion
    lines (derived from claude_multi.upgrade, never a hand-kept list), the
    candidate gateway and its probes, the pinned client, the product's own
    completion check and the evidence artifact behind the guard."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.jobs = _jobs((WORKFLOWS_DIR / "release.yml").read_text())
        cls.evidence = cls.jobs["evidence"]
        cls.modules = re.findall(r"\btests\.test_\w+", cls.evidence)

    def test_the_job_runs_every_producer(self) -> None:
        self.assertEqual(len(self.modules), len(set(self.modules)))
        for module in self.modules:
            self.assertTrue((REPO_ROOT / "tests" / f"{module.removeprefix('tests.')}.py").is_file(), module)
        self.assertEqual(_unproduced(self.modules), [])
        for needed in ("tests.test_scope_probe_client", "tests.test_scope_probe_seeds", "tests.test_managed_skill_policy",
                       "tests.test_agent_window_probe"):
            self.assertIn(needed, self.modules)

    def test_a_line_without_a_producer_fails_the_derivation(self) -> None:
        from claude_multi import upgrade

        prefixes = (*upgrade.ESSENTIAL_EVIDENCE_PREFIXES, "client probe NEW: PASS class=NEW-proof ")
        self.assertEqual(_unproduced(self.modules, prefixes), ["client probe NEW: PASS class=NEW-proof "])
        alternatives = {**upgrade.ESSENTIAL_EVIDENCE_ALTERNATIVES,
                        upgrade.ESSENTIAL_EVIDENCE_PREFIXES[0]: ("an unprinted alternative ",)}
        self.assertEqual(_unproduced(self.modules, alternatives=alternatives), ["an unprinted alternative "])
        for dropped in ("tests.test_scope_probe_seeds", "tests.test_scope_probe_client", "tests.test_managed_skill_policy",
                        "tests.test_client_hooks"):
            with self.subTest(dropped=dropped):
                self.assertNotEqual(_unproduced([m for m in self.modules if m != dropped]), [])

    def test_the_candidate_gateway_client_and_evidence(self) -> None:
        steps = _steps(self.evidence)
        order = ["name: gateway-dist", "name: gateway-probes", "gateway_probes.py provision", '>>"$GITHUB_ENV"',
                 'curl --proto', 'tools/test.py --verbose --claude "$RUNNER_TEMP/claude"', "status=${PIPESTATUS[0]}",
                 ".github/scripts/evidence.py", 'rm -f "$RUNNER_TEMP/claude"', "name: evidence"]
        positions = [self.evidence.index(needle) for needle in order]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("--home inherit", self.evidence)  # the private HOME and the tripwire stay on
        self.assertIn("kernel.apparmor_restrict_unprivileged_userns=0", self.evidence)
        upload = [step for step in steps if "actions/upload-artifact@" in step]
        self.assertEqual(len(upload), 1)
        self.assertIn("if: always() && steps.guard.outcome == 'success'", upload[0])
        self.assertIn("path: evidence", upload[0])
        self.assertEqual(_upload_guard_problems(self.evidence), [])
        build = self.jobs["build"]
        positions = [build.index(needle) for needle in (
            "tools/build.py gateway fetch vendor apply build inspect record --target shipped",
            "gateway_probes.py compile", "no_bundle_guard.py dist gateway-dist gateway-probes", "name: gateway-probes")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('--work "$RUNNER_TEMP/gateway-work"', build)

    def battery_step(self) -> str:
        (step,) = [step for step in _steps(self.evidence) if "tools/test.py" in step]
        return step

    def required_patterns(self) -> list[str]:
        """The battery's --require patterns as the shell passes them."""

        step = self.battery_step()
        assigned = dict(re.findall(r"(?m)^\s+(\w+)='([^']*)'$", step))
        (command,) = [line for line in _commands(step) if "tools/test.py" in line]
        return [quoted if quoted else assigned[name]
                for quoted, name in re.findall(r"--require (?:'([^']*)'|\"\$(\w+)\")", command)]

    def test_a_clean_runner_skips_only_the_retained_client_diagnostics(self) -> None:
        """--claude places only the pin, in the private HOME's claude-multi
        location: the stop-gate probe and AD (they need retained and newer
        clients) skip there, by their own bodies, and those skips must not
        fail the battery; every other real-client test stays required. The
        verbose log keeps AD's skip with its reason, a verbosity-1 log keeps
        nothing of it."""

        from claude_multi import strict_json
        from tests import test_scope_probe

        real = test_scope_probe.RealPinnedBinaryTests
        diagnostics = ("test_real_takeover_probe_stop_gate", "test_real_adoption_onto_newer_plain_claude")
        clean_runner = type("CleanRunner", (unittest.TestCase,), {
            "contract": strict_json.load(RESOURCES_ROOT / "catalog" / "native-contract.json"),
            **{name: getattr(real, name) for name in diagnostics}})
        logs = {}
        for verbosity in (1, 2):
            log = io.StringIO()
            with tempfile.TemporaryDirectory() as home, \
                    mock.patch.object(test_scope_probe, "_RETAINED_VERSIONS_DIR",
                                      Path(home) / ".local" / "share" / "claude" / "versions"), \
                    contextlib.redirect_stdout(log):  # the job's 2>&1
                result = unittest.TextTestRunner(stream=log, verbosity=verbosity).run(
                    unittest.defaultTestLoader.loadTestsFromTestCase(clean_runner))
            self.assertEqual(sorted(case.id().rsplit(".", 1)[1] for case, _ in result.skipped), sorted(diagnostics))
            self.assertTrue(all(reason.startswith("BOUNDARY: ") for _, reason in result.skipped))
            logs[verbosity] = log.getvalue()
        patterns = self.required_patterns()
        self.assertTrue(patterns)

        def required(test_id: str) -> bool:  # tools/test.py: a matching skip fails the run
            return any(re.search(pattern, test_id) for pattern in patterns)

        for name in diagnostics:
            self.assertFalse(required(f"tests.test_scope_probe.RealPinnedBinaryTests.{name}"), name)
        others = [name for name in unittest.defaultTestLoader.getTestCaseNames(real) if name not in diagnostics]
        self.assertGreaterEqual(len(others), 8)
        for name in others:
            self.assertTrue(required(f"tests.test_scope_probe.RealPinnedBinaryTests.{name}"), name)
        self.assertTrue(required("setUpClass (tests.test_scope_probe.RealPinnedBinaryTests)"))
        self.assertTrue(required("tests.test_client_hooks.SpikeHooks.test_s5"))
        self.assertTrue(required("setUpClass (tests.test_client_models.SelectorRecognitionProbe)"))
        self.assertFalse(required("tests.test_client_check.ClientCheckTests.test_x"))
        evidence = load_tool(SCRIPTS / "evidence.py", "evidence")
        adoption = evidence.skew_dispositions(logs[2])["AD"]
        self.assertEqual(adoption["disposition"], "skipped")
        self.assertEqual(len(adoption["skipped"]), 1)
        self.assertIn("BOUNDARY: the adoption probe needs the pin", next(iter(adoption["skipped"].values())))
        self.assertEqual(evidence.skew_dispositions(logs[1])["AD"]["disposition"], "missing")
        step = self.battery_step()
        self.assertIn("tools/test.py --verbose ", step)
        self.assertRegex(step, r'(?m)^\s+PYTHONUNBUFFERED: "1"$')

    def test_the_battery_runs_with_the_gateway_probes_client_and_distribution_tests(self) -> None:
        jobs = _jobs((WORKFLOWS_DIR / "ci.yml").read_text())
        gateway, battery = jobs["gateway"], jobs["battery"]
        self.assertIn("--target host", gateway)
        self.assertIn("gateway_probes.py compile", gateway)
        self.assertIn("needs: gateway", battery)
        order = ["setuptools==82.0.1", "name: gateway-dist", "name: gateway-probes", "gateway_probes.py provision",
                 "curl --proto", "tools/test.py --tier full --claude \"$RUNNER_TEMP/claude\" --require 'DistributionTests'"]
        positions = [battery.index(needle) for needle in order]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("--home inherit", battery)

    def test_the_distribution_tests_get_the_pinned_backend_by_hash(self) -> None:
        tables = _pin_tables(PINS.read_text())
        _tool, wheel, digest, _verified = {row[0]: row for row in tables["Tool artifacts (pinned by sha256)"]}["setuptools"]
        version = {row[0]: row[1] for row in tables["Tools (pinned by version)"]}["setuptools"]
        import tomllib

        backend = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["build-system"]["requires"]
        self.assertEqual(backend, [f"setuptools=={version}"])
        self.assertEqual(wheel, f"setuptools-{version}-py3-none-any.whl")
        battery = _jobs((WORKFLOWS_DIR / "ci.yml").read_text())["battery"]
        commands = [line for step in _steps(battery) for block in _run_blocks(step) for line in block]
        requirement = [line for line in commands if "setuptools==" in line]
        self.assertEqual(len(requirement), 1, requirement)
        self.assertIn(f"'setuptools=={version} --hash=sha256:{digest}' >\"$RUNNER_TEMP/setuptools.txt\"",
                      requirement[0])
        install = [line for line in commands if "pip install" in line]
        self.assertEqual(len(install), 1, install)
        for option in ("--require-hashes", "--no-deps", "--only-binary=:all:", '-r "$RUNNER_TEMP/setuptools.txt"'):
            self.assertIn(option, install[0])
        self.assertIn('python: ["3.11", "3.14"]', battery)  # both Pythons install it


@requires_release_tree
class DispatchContractTests(unittest.TestCase):
    """release.yml: one resolved commit for every job; a manual run builds
    only unless it asks for a draft on an existing v* tag."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (WORKFLOWS_DIR / "release.yml").read_text()
        cls.jobs = _jobs(cls.text)

    def test_the_dispatch_inputs(self) -> None:
        trigger = self.text.split("\njobs:\n", 1)[0]
        dispatch = trigger.split("workflow_dispatch:\n", 1)[1]
        self.assertRegex(dispatch, r"\n      ref:\n(?:        .*\n)*?        required: true\n(?:        .*\n)*?        type: string\n")
        self.assertRegex(dispatch, r"\n      draft:\n(?:        .*\n)*?        type: boolean\n(?:        .*\n)*?        default: false\n")
        self.assertIn('    tags: ["v*"]', trigger)

    def test_every_job_checks_out_the_resolved_commit(self) -> None:
        resolve = self.jobs["resolve"]
        self.assertIn("fetch-depth: 0", resolve)
        self.assertIn(".github/scripts/release_ref.py", resolve)
        for output in ("sha", "tag", "draft"):
            self.assertIn(f"{output}: ${{{{ steps.resolve.outputs.{output} }}}}", resolve)
        for name, body in self.jobs.items():
            if name == "resolve":
                continue
            with self.subTest(name):
                needs = re.search(r"(?m)^    needs: (.+)$", body).group(1)
                self.assertIn("resolve", re.findall(r"[\w-]+", needs))
                for step in _steps(body):
                    if "uses: actions/checkout@" in step:
                        self.assertIn("ref: ${{ needs.resolve.outputs.sha }}", step)
        self.assertNotIn("BUILD_REF", self.text)
        self.assertEqual(len(re.findall(r"uses: actions/checkout@", self.text)), len(self.jobs) - 1)  # all but draft

    def test_the_draft_condition(self) -> None:
        draft = self.jobs["draft"]
        condition = _job_condition(draft)
        self.assertIn("needs.resolve.outputs.draft == 'true'", condition)
        self.assertIn("!contains(needs.*.result, 'failure')", condition)
        self.assertIn("!contains(needs.*.result, 'cancelled')", condition)
        self.assertIn("TAG: ${{ needs.resolve.outputs.tag }}", draft)
        self.assertIn("--verify-tag", draft)
        for forbidden in ("git tag", "git push"):
            self.assertNotIn(forbidden, self.text)
        self.assertNotIn("--target", draft)  # gh release create never makes the tag
        self.assertIn('--input-ref="$INPUT_REF"', self.jobs["resolve"])  # a value, even one that starts with --
        needed = re.search(r"needs: \[([^\]]+)\]", draft).group(1)
        conditional = sorted(name for name in re.findall(r"[\w-]+", needed) if _job_condition(self.jobs[name]))
        self.assertEqual(conditional, ["journeys-macos-intel"])  # the only job that may be skipped


class ReleaseRefTests(unittest.TestCase):
    """.github/scripts/release_ref.py in a scratch repository."""

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("git") is None:
            raise unittest.SkipTest("BOUNDARY: git is not installed")
        cls.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-release-ref-"))
        cls.repo = cls.tmp / "repo"
        cls.env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(cls.tmp), "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@example.invalid", "LC_ALL": "C"}

        def git(*args: str) -> str:
            return subprocess.run(["git", *args], cwd=cls.repo, env=cls.env, check=True, capture_output=True,
                                  text=True, timeout=60, stdin=subprocess.DEVNULL).stdout.strip()

        cls.repo.mkdir()
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "one")
        cls.first = git("rev-parse", "HEAD")
        git("tag", "v1.0.0")
        git("tag", "x1")
        git("commit", "-q", "--allow-empty", "-m", "two")
        cls.second = git("rev-parse", "HEAD")
        git("tag", "-a", "-m", "annotated", "v1.1.0")
        git("branch", "feature")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def resolve(self, *args: str) -> tuple[int, dict[str, str], str]:
        result = subprocess.run([sys.executable, "-I", str(SCRIPTS / "release_ref.py"), *args], cwd=self.repo,
                                env=self.env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        outputs = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        return result.returncode, outputs, result.stderr

    def test_a_tag_push_drafts_at_the_tag(self) -> None:
        code, outputs, err = self.resolve("--event", "push", "--ref", "refs/tags/v1.0.0", "--sha", self.first)
        self.assertEqual((code, outputs), (0, {"sha": self.first, "tag": "v1.0.0", "draft": "true"}), err)
        code, outputs, _ = self.resolve("--event", "push", "--ref", "refs/tags/v1.1.0", "--sha", self.second)
        self.assertEqual(outputs, {"sha": self.second, "tag": "v1.1.0", "draft": "true"})  # an annotated tag
        for ref, sha, message in (("refs/tags/v1.0.0", self.second, "the push named"),
                                  ("refs/heads/main", self.second, "must be a v<major>"),
                                  ("refs/tags/x1", self.first, "must be a v<major>"),
                                  ("refs/tags/v9.9.9", self.first, "is not in this clone")):
            with self.subTest(ref=ref):
                code, outputs, err = self.resolve("--event", "push", "--ref", ref, "--sha", sha)
                self.assertEqual((code, outputs), (1, {}))
                self.assertIn(message, err)

    def test_a_manual_run_builds_only_by_default(self) -> None:
        for ref, sha in (("feature", self.second), (self.first, self.first), ("v1.0.0", self.first),
                         (self.first[:12], self.first)):
            with self.subTest(ref=ref):
                code, outputs, err = self.resolve("--event", "workflow_dispatch", "--input-ref", ref)
                self.assertEqual((code, outputs), (0, {"sha": sha, "tag": "", "draft": "false"}), err)

    def test_a_draft_needs_an_existing_v_tag(self) -> None:
        code, outputs, err = self.resolve("--event", "workflow_dispatch", "--input-ref", "v1.1.0", "--draft", "true")
        self.assertEqual((code, outputs), (0, {"sha": self.second, "tag": "v1.1.0", "draft": "true"}), err)
        for ref in ("feature", "main", self.first, "x1", "v9.9.9"):
            with self.subTest(ref=ref):
                code, outputs, err = self.resolve("--event", "workflow_dispatch", "--input-ref", ref, "--draft", "true")
                self.assertEqual((code, outputs), (1, {}))
                self.assertIn("release_ref:", err)
        code, _, err = self.resolve("--event", "workflow_dispatch", "--input-ref", "feature", "--draft", "true")
        self.assertIn("a draft needs ref to name an existing v<major>.<minor>.<patch> tag", err)

    def test_other_inputs_are_refused(self) -> None:
        for args, message in ((("--event", "workflow_dispatch", "--input-ref", "nothing"), "names no tag"),
                              (("--event", "workflow_dispatch", "--input-ref=--upload-pack=x"), "names no tag"),
                              (("--event", "workflow_dispatch", "--input-ref", "main", "--draft", "yes"),
                               "draft must be true or false"),
                              (("--event", "pull_request", "--ref", "refs/heads/main"), "not on 'pull_request'")):
            with self.subTest(args=args):
                code, outputs, err = self.resolve(*args)
                self.assertEqual((code, outputs), (1, {}))
                self.assertIn(message, err)


@requires_release_tree
class NativeLaneTests(unittest.TestCase):
    """The native lanes: arm64 Linux, the systemd user manager, Nix's
    indirect roots, WSL 2 through install.ps1 and Intel macOS behind its
    repository variable."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.release = _jobs((WORKFLOWS_DIR / "release.yml").read_text())
        cls.ci = _jobs((WORKFLOWS_DIR / "ci.yml").read_text())

    def test_arm64_linux_installs_its_bundle(self) -> None:
        job = self.release["journeys-linux-arm64"]
        self.assertIn("runs-on: ubuntu-24.04-arm", job)
        self.assertIn("JOURNEY_TARGET=linux-aarch64 sh .github/scripts/journey.sh install dist", job)

    def test_the_service_runs_under_a_real_user_manager(self) -> None:
        job = self.release["service-linux"]
        for needle in ("sudo useradd --create-home service-journey", "sudo loginctl enable-linger service-journey",
                       '/run/user/$uid/systemd/private', 'XDG_RUNTIME_DIR="/run/user/$uid"', "sudo -H -u service-journey",
                       "sh .github/scripts/journey.sh service dist"):
            self.assertIn(needle, job)
        body = JOURNEY.read_text().split("journey_service() {", 1)[1].split("\n}\n", 1)[0]
        steps = ['fresh_install "$dist"', 'background "$work/busy.pid" listen --port "$busy"',
                 '"$cm" setup --step gateway',
                 'if [ -z "$port" ] || [ "$port" = "$busy" ]; then', '"$cm" gateway service install ||',
                 '"$cm" gateway service install || fail "gateway service install again (the refresh)"',
                 'unit_property "$unit" FragmentPath', '[ "$fragment" = "$rendered" ]',
                 'unit_property "$unit" ActiveState', 'readlink "/proc/$pid/exe"',
                 'fx reach --host 127.0.0.1 --port "$port"', '"$cm" gateway service status',
                 '"$cm" gateway service uninstall', '[ ! -e "$rendered" ]', '[ "$loaded" = not-found ]']
        positions = [body.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("busy=18317", body)  # the first port a new install takes

    def test_service_manager_socket_is_checked_as_its_owner(self) -> None:
        job = self.release["service-linux"]
        # The runner cannot traverse another user's mode-0700 runtime dir.
        # Starting the manager is not enough if readiness is tested as runner.
        check = 'sudo -H -u service-journey test -S "/run/user/$uid/systemd/private"'
        self.assertEqual(job.count(check), 2, "both the wait and final readiness check need the service user")
        order = ["sudo loginctl enable-linger service-journey", "uid=$(id -u service-journey)",
                 'sudo systemctl start "user@$uid.service"', "for _ in $(seq 60); do",
                 check + " && break", "sleep 1", "done", check + " ||",
                 'env XDG_RUNTIME_DIR="/run/user/$uid"', "sh .github/scripts/journey.sh service dist"]
        offset = 0
        for needle in order:
            offset = job.index(needle, offset) + len(needle)

    def test_service_user_and_manager_do_not_inherit_foreign_xdg_paths(self) -> None:
        job = self.release["service-linux"]
        order = ["grep -n '^XDG_' /etc/environment || true",
                 "sudo -H -u service-journey env | grep '^XDG_' || true",
                 "sudo python3 - <<'PY'", "environment = Path('/etc/environment')",
                 "name.startswith('XDG_')", "environment.write_text(''.join(kept))",
                 "sudo loginctl enable-linger service-journey",
                 'sudo systemctl start "user@$uid.service"',
                 'manager_environment=$(sudo -H -u service-journey env XDG_RUNTIME_DIR=',
                 "systemctl --user show-environment", "::error::could not read the service user manager environment",
                 "sed -n 's/^XDG_CONFIG_HOME=//p'",
                 'case "$manager_config" in', "''|/home/service-journey|/home/service-journey/*)",
                 '::error::service user manager has foreign XDG_CONFIG_HOME=$manager_config',
                 "sudo -H -u service-journey env -u XDG_CONFIG_HOME", "sh .github/scripts/journey.sh service dist"]
        offset = 0
        for needle in order:
            offset = job.index(needle, offset) + len(needle)
        cleanup = job.split("sudo python3 - <<'PY'", 1)[1].split("\n          PY", 1)[0]
        self.assertIn("home = Path('/home/service-journey')", cleanup)
        self.assertIn("path != home and home not in path.parents", cleanup)
        self.assertIn("value.split(':')", cleanup)
        journey = job.split("sudo -H -u service-journey env -u XDG_CONFIG_HOME", 1)[1]
        for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_DIRS", "XDG_DATA_DIRS"):
            self.assertIn(f"-u {name}", journey)

    def test_service_namespace_probes_and_failure_diagnostics_use_its_manager(self) -> None:
        job = self.release["service-linux"]
        probe = "systemd-run --user --wait --pipe -p PrivateUsers=yes -p ProtectHome=tmpfs /bin/true"
        manager = ('sudo -H -u service-journey env XDG_RUNTIME_DIR="/run/user/$uid" \\\n'
                   '            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \\\n')
        order = ["sysctl kernel.apparmor_restrict_unprivileged_userns\n",
                 "if " + manager, probe + "; then", "probe_status=0", "else", "probe_status=$?", "fi",
                 'echo "User namespace probe before lifting AppArmor restriction: exit $probe_status"',
                 "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0",
                 "if ! " + manager, probe + "; then",
                 "::error::service user manager cannot create the hardened namespace", "exit 1", "fi",
                 "if ! sudo -H -u service-journey env -u XDG_CONFIG_HOME",
                 "sh .github/scripts/journey.sh service dist'; then",
                 manager.replace("            DBUS", "              DBUS"),
                 "systemctl --user status 'claude-multi-gateway*' --no-pager || true",
                 manager.replace("            DBUS", "              DBUS"),
                 "journalctl --user -u claude-multi-gateway -n 200 --no-pager || true",
                 "::error::supervised gateway service journey failed", "exit 1", "fi"]
        offset = 0
        for needle in order:
            offset = job.index(needle, offset) + len(needle)
        self.assertEqual(job.count(probe), 2)

    def test_windows_registers_missing_default_gallery_before_pinned_pester(self) -> None:
        job = self.release["journeys-windows"]
        step = next(step for step in _steps(job) if "- name: install.ps1 Pester suite" in step)
        self.assertEqual(_commands(step)[:5], [
            "if (-not (Get-PSRepository -Name PSGallery -ErrorAction SilentlyContinue)) {",
            "Register-PSRepository -Default",
            "}",
            "Install-Module Pester -RequiredVersion 6.2.0 -Repository PSGallery -Scope CurrentUser -Force -SkipPublisherCheck",
            "Import-Module Pester -RequiredVersion 6.2.0"])
        self.assertEqual(job.count("Register-PSRepository"), 1)

    def test_windows_installer_is_sourced_before_step_variable_assignments(self) -> None:
        job = self.release["journeys-windows"]
        step = job.split("- name: The release's install.ps1 installs into WSL 2", 1)[1]
        # Dot-sourcing executes the script's param defaults in the caller's
        # scope; PowerShell variable names are case-insensitive.
        self.assertEqual(step.count(". ./dist/install.ps1"), 1)
        source = step.index(". ./dist/install.ps1")
        self.assertLess(source, step.index("$distribution = 'Ubuntu-24.04'"))
        self.assertLess(source, step.index("$version = (Get-Content dist/MANIFEST.json"))
        self.assertIn("wsl --distribution $distribution --exec python3 -I -c", step)
        self.assertIn("could not verify Linux loopback server cleanup", step)

    def test_the_nix_lane_proves_the_indirect_root(self) -> None:
        job = self.ci["nix"]
        self.assertIn("package=$(nix build --no-link --print-out-paths .#claude-multi)", job)
        self.assertIn('python3 .github/scripts/nix_roots.py "$package"', job)

    def test_windows_installs_through_install_ps1(self) -> None:
        job = self.release["journeys-windows"]
        order = ["Invoke-Pester", ". ./dist/install.ps1", "Test-WslReady", "Install-Wsl -AssumeYes",
                 "wsl --install --distribution Ubuntu-24.04 --no-launch", "sh .github/scripts/wsl_prepare.sh",
                 "journey.sh prepare dist", "serve-files", "--cert", "Invoke-Main -Distribution $distribution",
                 '-InstallerUrl "https://localhost:$port/install.sh"', "-InstallerSha256 $sum",
                 "-InstallerArguments @('--from-dir'", "'--allowed-signers'",
                 "JOURNEY_CLIENT=fetch sh .github/scripts/journey.sh installed"]
        positions = [job.index(needle) for needle in order]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("journey.sh install dist", job)  # install.ps1 installs, not the journey
        self.assertIn("the public-URL install after publication covers them", job)  # the leg left out, labelled
        prepare = WSL_PREPARE.read_text()
        for needle in ("useradd --create-home", "[user]\\ndefault=%s", "update-ca-certificates",
                       "subjectAltName=DNS:localhost,IP:127.0.0.1", 'trap \'rm -rf "$work"\' EXIT'):
            self.assertIn(needle, prepare)

    def test_windows_installed_journey_detaches_and_keeps_its_argv_and_exit_guard(self) -> None:
        job = self.release["journeys-windows"]
        commands = _commands(job)
        journeys = [line for line in commands if "journey.sh installed" in line]
        self.assertEqual(journeys, [
            'wsl --distribution $distribution --cd "$workspace" --exec setsid --wait sh -c '
            '\'umask 077; JOURNEY_CLIENT=fetch sh .github/scripts/journey.sh installed "$0"\' $version'])
        self.assertEqual(commands[commands.index(journeys[0]) + 1],
                         "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }")
        self.assertEqual(job.count("setsid"), 1, "detach only the installed journey, not installer or readiness")

    @unittest.skipUnless(shutil.which("setsid"), "BOUNDARY: util-linux setsid (--wait) is required for the PTY test")
    def test_windows_installed_journey_detachment_is_headless_and_preserves_exit_status(self) -> None:
        """A controlling PTY survives stdio redirection, but not setsid.
        Make the setsid process a group leader so it must fork: --wait must
        still propagate the shell's nonzero result, not just the fork's 0.
        """

        probe = """import json, os, sys
try:
    fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
except OSError:
    tty = False
else:
    os.close(fd)
    tty = True
print(json.dumps({'tty': tty, 'stdio': [os.isatty(fd) for fd in range(3)], 'version': sys.argv[1]}))
sys.exit(int(sys.argv[2]))
"""
        harness = """import fcntl, json, os, signal, subprocess, sys, termios
signal.signal(signal.SIGHUP, signal.SIG_IGN)
master, slave = os.openpty()
try:
    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    rows = []
    for detached in (False, True):
        for code in (0, 23):
            argv = ['sh', '-c', 'umask 077; "$1" -I -c "$2" "$0" "$3"',
                    '1.2.3', sys.executable, sys.argv[2], str(code)]
            if detached:
                argv = [sys.argv[1], '--wait', *argv]
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=5, process_group=0)
            rows.append({'detached': detached, 'code': code, 'status': result.returncode,
                         'probe': json.loads(result.stdout), 'stderr': result.stderr})
    print(json.dumps(rows))
finally:
    os.close(slave)
    os.close(master)
"""
        with tempfile.TemporaryDirectory(prefix="claude-multi-journey-tty-") as root:
            tmp = Path(root)
            home = tmp / "home"
            home.mkdir(mode=0o700)
            env = {"HOME": str(home), "PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"}
            result = subprocess.run([sys.executable, "-I", "-c", harness, shutil.which("setsid"), probe],
                                    cwd=tmp, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=30, start_new_session=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual([(row["detached"], row["code"]) for row in rows],
                         [(False, 0), (False, 23), (True, 0), (True, 23)])
        for row in rows:
            with self.subTest(detached=row["detached"], code=row["code"]):
                self.assertEqual(row["status"], row["code"], row)
                self.assertEqual(row["probe"], {"tty": not row["detached"], "stdio": [False] * 3,
                                                "version": "1.2.3"})
                self.assertEqual(row["stderr"], "")

    def test_windows_server_cleanup_is_guaranteed_and_checks_the_linux_pid(self) -> None:
        job = self.release["journeys-windows"]
        self.assertIn("$server = $null", job, "the server needs an owner and guaranteed cleanup")
        step = job.split("$server = $null", 1)[1]
        order = ["try {", "$server = Start-Process", "if (-not $port) { throw", "Invoke-Main",
                 "} finally {", "wsl --distribution $distribution --exec python3 -I -c",
                 "if ($LASTEXITCODE -ne 0) { throw 'could not verify Linux loopback server cleanup' }",
                 "} finally {", "Stop-Process -Id $server.Id", "journey.sh installed"]
        offset = 0
        for needle in order:
            offset = step.index(needle, offset) + len(needle)
        # Both embedded programs remain readable and valid Python; WSL and
        # PowerShell execution itself belongs to the native Windows CI lane.
        programs = {}
        for name in ("startSource", "stopSource"):
            source = job.split(f"${name} = @'\n", 1)[1].split("          '@", 1)[0]
            source = "\n".join(line[10:] for line in source.splitlines())
            compile(source, name, "exec")
            programs[name] = source
        start, stop = programs["startSource"], programs["stopSource"]
        for needle in ("str(os.getpid())", "pending.replace(pidfile)", "server.stop", "os.execv"):
            self.assertIn(needle, start)
        self.assertLess(start.index("pending.replace(pidfile)"), start.index("server.stop"))
        for needle in ("server.stop').touch()", "pid = int(pidfile.read_text())", "os.kill(pid, signal.SIGKILL)",
                       "range(100)", "'/proc/{pid}/stat'", "the Linux loopback server did not exit"):
            self.assertIn(needle, stop)

    def test_intel_macos_runs_only_on_a_named_runner(self) -> None:
        job = self.release["journeys-macos-intel"]
        self.assertEqual(_job_condition(job), "vars.CM_MACOS_INTEL_RUNNER != ''")
        self.assertIn("runs-on: ${{ vars.CM_MACOS_INTEL_RUNNER }}", job)
        self.assertIn("JOURNEY_TARGET=darwin-x86_64 sh .github/scripts/journey.sh install dist", job)
        self.assertFalse(_evaluate(_job_condition(job), "push", {}))
        self.assertTrue(_evaluate(_job_condition(job), "push", {"CM_MACOS_INTEL_RUNNER": "macos-13"}))


@requires_release_tree
class GatewayProbesScriptTests(unittest.TestCase):
    """.github/scripts/gateway_probes.py: the recipe of the Nix probe
    expression, the provisioning of a downloaded build and its refusals
    (the compile itself runs in CI: it needs the pinned toolchain)."""

    def setUp(self) -> None:
        import _gateway

        self.gw = _gateway
        self.tool = _gateway_probes()
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-probes-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        if _gateway.unsafe_location(self.tmp) or _gateway.host_target() is None:
            self.skipTest("BOUNDARY: no private temporary directory or no gateway target here")
        self.target = _gateway.host_target()

    def test_the_recipe_follows_the_store_expression(self) -> None:
        copies, probes = self.tool.recipe()
        self.assertEqual(len(copies), 5)
        for source, destination in copies:
            self.assertTrue((REPO_ROOT / "tests" / source).is_file(), source)
            self.assertTrue(destination.endswith("_test.go"), destination)
        self.assertEqual([(p["race"], p["cgo"]) for p in probes], [(False, False), (True, True), (True, True)])
        self.assertEqual(tuple(p["name"] for p in probes), PROBE_NAMES)
        self.assertEqual(probes[0]["package"], "./sdk/cliproxy")
        text = self.tool.EXPRESSION.read_text()
        for bad in ("go test -mod=vendor -tags extra -c -o x ./y", "go test -c -o x ./y"):
            with self.subTest(bad), self.assertRaises(self.tool.ProbeError):
                self.tool.recipe(text + "\n    " + bad + "\n")

    def fixture(self, *, probe_gateway: str | None = None, target: str | None = None) -> tuple[Path, Path]:
        dist = self.tmp / "dist"
        binary = dist / self.target / "cli-proxy-api"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\necho gateway\n")
        binary.chmod(0o644)  # as an artifact download leaves it
        (dist / "BUILD.json").write_text(json.dumps({
            "format": 1, "admitted_series": True, "series": self.gw.admitted_series(),
            "targets": {self.target: {"file": f"{self.target}/cli-proxy-api",
                                      "sha256": self.gw.sha256_file(binary)}}}))
        probes = self.tmp / "probes"
        (probes / "bin").mkdir(parents=True)
        (probes / "share").mkdir()
        recorded = {}
        for name in PROBE_NAMES:
            (probes / "bin" / name).write_text(f"#!/bin/sh\necho {name}\n")
            (probes / "bin" / name).chmod(0o644)
            recorded[name] = self.gw.sha256_file(probes / "bin" / name)
        (probes / "share" / "probe-build.json").write_text(json.dumps({
            "format": 1, "gateway_sha256": probe_gateway or self.gw.sha256_file(binary),
            "gateway_target": target or self.target, "series": self.gw.admitted_series(), "probes": recorded}))
        return dist, probes

    def run_tool(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.tool.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_provision_binds_a_downloaded_build(self) -> None:
        dist, probes = self.fixture()
        code, out, err = self.run_tool("provision", "--dist", str(dist), "--probes", str(probes))
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), [
            f"{self.gw.BINARY_ENV}={dist / self.target / 'cli-proxy-api'}",
            f"{self.gw.BUILD_RECORD_ENV}={dist / 'BUILD.json'}",
            f"CLAUDE_MULTI_TEST_GATEWAY_STARTUP_PROBE={probes / 'bin' / PROBE_NAMES[0]}"])
        for path in (dist / self.target / "cli-proxy-api", *(probes / "bin" / name for name in PROBE_NAMES)):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_provision_refusals(self) -> None:
        for name, kwargs, message in (("another gateway", dict(probe_gateway="0" * 64), "another gateway build"),
                                      ("another target", dict(target="windows-amd64"), "were built for windows-amd64")):
            with self.subTest(name):
                shutil.rmtree(self.tmp / "dist", ignore_errors=True)
                shutil.rmtree(self.tmp / "probes", ignore_errors=True)
                dist, probes = self.fixture(**kwargs)
                code, out, err = self.run_tool("provision", "--dist", str(dist), "--probes", str(probes))
                self.assertEqual((code, out), (1, ""))
                self.assertIn(message, err)
        record = json.loads((self.tmp / "dist" / "BUILD.json").read_text())
        record["series"] = record["series"][1:]
        (self.tmp / "dist" / "BUILD.json").write_text(json.dumps(record))
        code, _out, err = self.run_tool("provision", "--dist", str(self.tmp / "dist"), "--probes", str(self.tmp / "probes"))
        self.assertEqual(code, 1)
        self.assertIn("another patch series", err)

    def test_compile_refuses_an_unprepared_tree(self) -> None:
        dist, _probes = self.fixture()
        (self.tmp / "work").mkdir()
        code, out, err = self.run_tool("compile", "--work", str(self.tmp / "work"), "--dist", str(dist),
                                       "--out", str(self.tmp / "out"))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("is not prepared", err)
        self.assertFalse((self.tmp / "out").exists())


@requires_release_tree
class EvidenceScriptTests(unittest.TestCase):
    """.github/scripts/evidence.py: the product's completion check on a
    battery log, the manifest, and a log that carries a fixture secret."""

    def setUp(self) -> None:
        self.tool = load_tool(SCRIPTS / "evidence.py", "evidence")
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-evidence-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.client = self.tmp / "claude"
        self.client.write_bytes(b"not the pinned client")
        (self.tmp / "modules").write_text("tests.test_scope_probe\ntests.test_scope_probe_client\n")

    SKEW = ("client probe AD: PASS class=AD-accepted pin=2.1.286 newer=2.1.290\n"
            "client probe SL: BOUNDARY class=unavailable BOUNDARY: no Claude Code newer than the pin\n")

    def log(self, *, drop: str | None = None, extra: str = "", skew: str = SKEW) -> Path:
        from claude_multi import upgrade

        lines = [f"{prefix}detail of the run" for prefix in upgrade.ESSENTIAL_EVIDENCE_PREFIXES if prefix != drop]
        path = self.tmp / "battery.log"
        path.write_text("Ran 120 tests\n" + "\n".join(lines) + "\n" + skew + extra)
        return path

    @staticmethod
    def merged(verbosity: int, battery: type) -> str:
        """A battery's log as the job keeps it: unittest's stream (stderr)
        and the tests' output (stdout) in one (2>&1)."""

        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            unittest.TextTestRunner(stream=log, verbosity=verbosity).run(
                unittest.defaultTestLoader.loadTestsFromTestCase(battery))
        return log.getvalue()

    def run_tool(self, log: Path, status: int = 0, *, pinned: bool = True,
                 environ: dict | None = None) -> tuple[int, dict, str]:
        out = self.tmp / "evidence"
        shutil.rmtree(out, ignore_errors=True)
        identity = {**self.tool.client_identity(self.client), "matches_contract": pinned}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(self.tool, "client_identity", return_value=identity), \
                mock.patch.dict(os.environ, environ or {}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.tool.main(["--log", str(log), "--status", str(status), "--claude", str(self.client),
                                   "--modules", str(self.tmp / "modules"), "--out", str(out)])
        return code, json.loads((out / "manifest.json").read_text()), stdout.getvalue() + stderr.getvalue()

    def test_a_complete_run_passes_and_is_described(self) -> None:
        gateway = self.tmp / "cli-proxy-api"
        gateway.write_bytes(b"gateway")
        record = self.tmp / "BUILD.json"
        record.write_text(json.dumps({"series": [{"basename": "a.patch", "sha256": "0" * 64}],
                                      "post_apply_tree_sha256": "1" * 64}))
        environ = {"CLAUDE_MULTI_TEST_CLI_PROXY_API": str(gateway), "CLAUDE_MULTI_TEST_GATEWAY_BUILD": str(record),
                   "GITHUB_RUN_ID": "42", "GITHUB_WORKFLOW": "release"}
        code, manifest, output = self.run_tool(self.log(), environ=environ)
        self.assertEqual(code, 0, output)
        self.assertEqual(manifest["verdict"], "PASS")
        self.assertEqual(manifest["missing_completion_lines"], [])
        self.assertEqual(manifest["modules"], ["tests.test_scope_probe", "tests.test_scope_probe_client"])
        self.assertEqual(manifest["workflow_run"]["run_id"], "42")
        self.assertEqual(manifest["client"]["sha256"], hashlib.sha256(b"not the pinned client").hexdigest())
        self.assertEqual(manifest["gateway"]["sha256"], hashlib.sha256(b"gateway").hexdigest())
        self.assertEqual(manifest["build_record"]["series"], ["a.patch@" + "0" * 64])
        self.assertEqual(manifest["log"]["sha256"], hashlib.sha256((self.tmp / "battery.log").read_bytes()).hexdigest())
        self.assertEqual((self.tmp / "evidence" / "battery.log").read_text(), (self.tmp / "battery.log").read_text())
        self.assertEqual(sorted(p.name for p in (self.tmp / "evidence").iterdir()), ["battery.log", "manifest.json"])
        self.assertNotIn(b"not the pinned client", (self.tmp / "evidence" / "manifest.json").read_bytes())

    def test_failures(self) -> None:
        from claude_multi import upgrade

        prefix = upgrade.ESSENTIAL_EVIDENCE_PREFIXES[5]
        code, manifest, output = self.run_tool(self.log(drop=prefix))
        self.assertEqual((code, manifest["verdict"], manifest["missing_completion_lines"]), (1, "FAIL", [prefix]))
        self.assertIn(f"missing: {prefix.rstrip()}", output)
        code, manifest, _ = self.run_tool(self.log(), status=1)
        self.assertEqual((code, manifest["problems"]), (1, ["the battery exited 1"]))
        code, manifest, _ = self.run_tool(self.log(), pinned=False)
        self.assertEqual(code, 1)
        self.assertIn("the shipped contract's pinned Claude Code", manifest["problems"][0])
        # A named alternative completes its line.
        alternative = upgrade.ESSENTIAL_EVIDENCE_ALTERNATIVES
        main = next(iter(alternative))
        code, manifest, _ = self.run_tool(self.log(drop=main, extra=alternative[main][0] + "detail\n"))
        self.assertEqual((code, manifest["verdict"]), (0, "PASS"))

    def test_completion_lines_after_unittest_progress_and_headers(self) -> None:
        """Merged verbosity-1 and verbosity-2 output: a completion line after
        a progress run or a test header (with or without a docstring line)
        completes; the same line after any other text does not."""

        from claude_multi import upgrade

        def producer(line: str, documented: bool):
            def test(case: unittest.TestCase) -> None:
                print(line + "detail of the run")
            test.__doc__ = "A producer with a docstring." if documented else None
            return test

        def adoption(case: unittest.TestCase) -> None:
            """Pin skew with an auto-updating plain claude."""
            case.skipTest("BOUNDARY: the adoption probe needs the pin and a newer Claude Code")

        body = {f"test_{index:02d}": producer(prefix, bool(index % 2))
                for index, prefix in enumerate(upgrade.ESSENTIAL_EVIDENCE_PREFIXES)}
        body["test_real_adoption_onto_newer_plain_claude"] = adoption
        body["test_sl"] = producer("client probe SL: BOUNDARY class=unavailable ", False)
        battery = type("Battery", (unittest.TestCase,), body)
        client = {"matches_contract": True}
        for verbosity in (1, 2):
            with self.subTest(verbosity=verbosity):
                text = self.merged(verbosity, battery)
                self.assertNotEqual(upgrade._missing_evidence_prefixes(text), ())  # glued to unittest's output
                missing, problems = self.tool.evaluate(text, 0, client)
                self.assertEqual(missing, [])
                first = upgrade.ESSENTIAL_EVIDENCE_PREFIXES[1]
                elsewhere = text.replace(first, "note: " + first)
                self.assertEqual(self.tool.evaluate(elsewhere, 0, client)[0], [first])
                (self.tmp / "battery.log").write_text(text)
                code, manifest, _ = self.run_tool(self.tmp / "battery.log")
                self.assertEqual(manifest["missing_completion_lines"], [])
                self.assertEqual(manifest["client_skew"]["SL"]["disposition"], "recorded")
                if verbosity == 2:
                    self.assertEqual((code, manifest["verdict"], problems), (0, "PASS", []))
                    self.assertEqual(manifest["client_skew"]["AD"]["disposition"], "skipped")
                    self.assertEqual(list(manifest["client_skew"]["AD"]["skipped"].values()),
                                     ["BOUNDARY: the adoption probe needs the pin and a newer Claude Code"])
                else:  # a bare 's': the AD outcome is lost
                    self.assertEqual((code, manifest["verdict"]), (1, "FAIL"))
                    self.assertEqual(manifest["client_skew"]["AD"]["disposition"], "missing")
                    self.assertIn("the log shows no AD outcome", problems[0])
        # The uploaded log is the battery's own, unchanged.
        self.assertEqual((self.tmp / "evidence" / "battery.log").read_text(), text)

    def test_a_skew_probe_without_an_outcome_fails(self) -> None:
        code, manifest, output = self.run_tool(self.log(skew="client probe SL: PASS class=SL-resumed writer=x\n"))
        self.assertEqual((code, manifest["verdict"]), (1, "FAIL"))
        self.assertEqual(manifest["client_skew"]["AD"]["disposition"], "missing")
        self.assertEqual(manifest["client_skew"]["SL"]["records"], ["PASS class=SL-resumed writer=x"])
        self.assertIn("the log shows no AD outcome", output)

    def test_a_log_with_a_fixture_secret_is_withheld(self) -> None:
        from _catalog import FIXTURE_GATEWAY_TOKEN

        code, manifest, output = self.run_tool(self.log(extra=f"header Authorization: Bearer {FIXTURE_GATEWAY_TOKEN}\n"))
        self.assertEqual((code, manifest["verdict"], manifest["log"]["file"]), (1, "FAIL", None))
        self.assertFalse((self.tmp / "evidence" / "battery.log").exists())
        self.assertNotIn(FIXTURE_GATEWAY_TOKEN, (self.tmp / "evidence" / "manifest.json").read_text())
        self.assertNotIn(FIXTURE_GATEWAY_TOKEN, output)


class NixRootsTests(unittest.TestCase):
    """.github/scripts/nix_roots.py against a stand-in nix-store that keeps
    indirect roots the way the real one answers for them."""

    FAKE = """#!/bin/sh
state=$FAKE_NIX_STATE
case $1 in
--add-root)
	[ "${FAKE_NIX_ADD:-ok}" = ok ] || exit 1
	[ "$3" = --indirect ] && [ "$4" = --realise ] || exit 64
	printf '%s\\n' "$2" >>"$state/roots"
	echo "$2" ;;
--query)
	[ -f "$state/roots" ] || exit 0
	while read -r link; do
		if [ -L "$link" ] && [ "$(readlink "$link")" = "$3" ]; then printf '%s -> %s\\n' "$link" "$3"; fi
	done <"$state/roots"
	exit 0 ;;
--delete)
	if [ "${FAKE_NIX_DELETE:-honest}" = honest ] && [ -n "$(sh "$0" --query --roots "$2")" ]; then
		echo "error: cannot delete path '$2' since it is still alive" >&2
		exit 1
	fi
	echo "deleted $2" ;;
esac
"""

    def setUp(self) -> None:
        self.tool = load_tool(SCRIPTS / "nix_roots.py", "nix_roots")
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-nix-roots-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "bin").mkdir()
        self.fake = self.tmp / "fake-nix-store"
        self.fake.write_text(self.FAKE)
        self.fake.chmod(0o755)
        (self.tmp / "bin" / "nix-store").symlink_to(self.fake)
        (self.tmp / "state").mkdir()
        self.store = self.tmp / "store"
        self.package = self.store / "abc-claude-multi"
        (self.package / "bin").mkdir(parents=True)
        (self.package / "bin" / "claude-multi-proxy").write_text("#!/bin/sh\n")

    def prove(self, **environ: str) -> tuple[int, str]:
        """main() on the stand-in store. The product registers its root
        through the runner it is given; here that runner runs the stand-in
        by its own name, never a program called nix-store with --add-root
        (the test tripwire refuses those)."""

        from claude_multi import installs

        def runner(argv, **kwargs):
            return subprocess.run([str(self.fake), *argv[1:]], **kwargs)

        out, err = io.StringIO(), io.StringIO()
        path = f"{self.tmp / 'bin'}{os.pathsep}{os.environ.get('PATH', os.defpath)}"
        prove = self.tool.prove
        with mock.patch.object(installs, "STORE_PREFIX", str(self.store) + "/"), \
                mock.patch.object(self.tool, "prove", lambda package: prove(package, runner)), \
                mock.patch.dict(os.environ, {"PATH": path, "FAKE_NIX_STATE": str(self.tmp / "state"), **environ}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.tool.main([str(self.package)])
        return code, out.getvalue() + err.getvalue()

    def test_the_product_roots_and_unroots_the_package(self) -> None:
        code, output = self.prove()
        self.assertEqual(code, 0, output)
        for step in ("unrooted before the selection: ok", "rooted by the product's link", "deletion refused: still alive: ok",
                     "unrooted after the removal: ok"):
            self.assertIn(step, output)
        roots = (self.tmp / "state" / "roots").read_text().split()
        self.assertEqual(len(roots), 1)
        self.assertTrue(roots[0].endswith("/.local/share/claude-multi/nix/current"))

    def test_failures(self) -> None:
        code, output = self.prove(FAKE_NIX_ADD="fail")
        self.assertEqual(code, 1)
        self.assertIn("did not register the root", output)
        code, output = self.prove(FAKE_NIX_DELETE="ignores-roots")
        self.assertEqual(code, 1)
        self.assertIn("although the link roots it", output)
        other = self.tmp / "other-root"
        other.symlink_to(self.package)
        (self.tmp / "state" / "roots").write_text(f"{other}\n")
        code, output = self.prove()
        self.assertEqual(code, 1)
        self.assertIn("already has roots", output)
        self.assertEqual(self.tool.main([]), 2)


if __name__ == "__main__":
    unittest.main()
