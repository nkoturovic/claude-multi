"""``tools/release.py``: preflight, build, reproduce, sign, verify-draft, publish.

Signing uses throwaway Ed25519 keys made by ``ssh-keygen`` per test (none is
stored in the tree, none is ever a release key); the draft is a tiny fake
release (``tests/_fake_release.py``) installed with the real
``packaging/install.sh`` into a temporary HOME. ``gh`` is a recording
stand-in on PATH. Nothing is pushed, published or downloaded.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import time
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _fake_release import FakeRelease, make_key, needs_ssh_keygen, signers_line
from _layout import REPO_ROOT
from _release import INSTALL_PS1, INSTALL_SH, RELEASE_TOOL, keyless_repo, load_tool, process_running, requires_release_tree

# POSIX installer and sh (tools/test.py reads this marker).
PLATFORMS = ("linux", "darwin")
needs_git = unittest.skipUnless(shutil.which("git"), "BOUNDARY: git is not installed")


def tool():
    return load_tool(RELEASE_TOOL, "release")


def completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


class FakeRunner:
    """Answers subprocess calls: the first entry whose every text occurs in
    some argument wins; anything else succeeds with no output."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, **_kwargs):
        argv = list(argv)
        self.calls.append(argv)
        for texts, result in self.answers:
            if all(any(text in argument for argument in argv) for text in texts):
                return completed(argv, *result) if isinstance(result, tuple) else result(argv)
        return completed(argv)


def git_repo(root: Path, version: str = "1.2.3", catalog: int = 7, changelog: str | None = None) -> Path:
    repo = root / "repo"
    (repo / "src" / "claude_multi" / "data" / "catalog").mkdir(parents=True)
    (repo / "src" / "claude_multi" / "data" / "version.json").write_text(json.dumps(
        {"catalog_version": catalog, "launcher_version": version, "version": 1}))
    (repo / "src" / "claude_multi" / "data" / "catalog" / "models.json").write_text("{}\n")
    (repo / "CHANGELOG.md").write_text(changelog if changelog is not None else
                                       f"# Changelog\n\n## [{version}] — 2026-10-15\n\n- first\n")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for argv in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                 ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "one"]):
        subprocess.run(argv, cwd=repo, env=env, check=True, capture_output=True, timeout=60)
    return repo


def commit(repo: Path, message: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m",
                    message], cwd=repo, env=env, check=True, capture_output=True, timeout=60)


def tag(repo: Path, name: str) -> None:
    subprocess.run(["git", "tag", name], cwd=repo, check=True, capture_output=True, timeout=60)


@requires_release_tree
class PreflightCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-release-tool-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tool = tool()

    @needs_git
    def test_clean_tree_version_and_changelog(self) -> None:
        repo = git_repo(self.tmp)
        self.assertTrue(self.tool.check_clean_tree(repo).ok)
        (repo / "stray.txt").write_text("x")
        check = self.tool.check_clean_tree(repo)
        self.assertFalse(check.ok)
        self.assertIn("stray.txt", check.detail)
        self.assertTrue(self.tool.check_version(repo).ok)
        self.assertTrue(self.tool.check_changelog(repo).ok)
        dev = git_repo(self.tmp / "dev", version="1.2.3-dev",
                       changelog="# Changelog\n\n## [1.2.3-dev] — unreleased\n")
        self.assertFalse(self.tool.check_version(dev).ok)
        self.assertIn("not a release version", self.tool.check_version(dev).detail)
        undated = git_repo(self.tmp / "undated", changelog="# Changelog\n\n## [1.2.3] — unreleased\n")
        self.assertFalse(self.tool.check_changelog(undated).ok)
        missing = git_repo(self.tmp / "missing", changelog="# Changelog\n\n## [1.2.2] — 2026-01-01\n")
        self.assertIn("no section for 1.2.3", self.tool.check_changelog(missing).detail)

    @needs_git
    def test_version_bump_against_the_previous_release(self) -> None:
        repo = git_repo(self.tmp, version="1.0.0")
        self.assertIsNone(self.tool.previous_release(repo, "1.0.0"))
        self.assertTrue(self.tool.check_version_bump(repo, None).ok)
        tag(repo, "v1.0.0")
        self.assertTrue(self.tool.check_version_bump(repo, None).ok)  # its own tag on HEAD
        version = repo / "src" / "claude_multi" / "data" / "version.json"
        version.write_text(json.dumps({"catalog_version": 7, "launcher_version": "1.1.0", "version": 1}))
        commit(repo, "two")
        self.assertEqual(self.tool.previous_release(repo, "1.1.0"), "v1.0.0")
        self.assertTrue(self.tool.check_version_bump(repo, "v1.0.0").ok)
        (repo / "src" / "claude_multi" / "data" / "catalog" / "models.json").write_text('{"changed": true}\n')
        commit(repo, "catalog")
        check = self.tool.check_version_bump(repo, "v1.0.0")
        self.assertFalse(check.ok)
        self.assertIn("catalog changed", check.detail)
        version.write_text(json.dumps({"catalog_version": 8, "launcher_version": "1.1.0", "version": 1}))
        commit(repo, "bump")
        self.assertTrue(self.tool.check_version_bump(repo, "v1.0.0").ok)
        version.write_text(json.dumps({"catalog_version": 8, "launcher_version": "0.9.0", "version": 1}))
        commit(repo, "older")
        self.assertIn("not newer", self.tool.check_version_bump(repo, "v1.0.0").detail)
        tag(repo, "v0.9.0")
        version.write_text(json.dumps({"catalog_version": 8, "launcher_version": "0.9.0", "version": 1}))
        (repo / "x").write_text("x")
        commit(repo, "moved on")
        self.assertIn("names another commit", self.tool.check_version_bump(repo, None).detail)

    def test_repository_checks_on_this_tree(self) -> None:
        self.assertTrue(self.tool.check_contract(REPO_ROOT).ok, self.tool.check_contract(REPO_ROOT).detail)
        parity = self.tool.check_gateway_parity(REPO_ROOT)
        self.assertTrue(parity.ok, parity.detail)
        trust = self.tool.check_trust(REPO_ROOT)
        self.assertTrue(trust.ok, trust.detail)  # the production key
        self.assertRegex(trust.detail, r"^1 release key\(s\): SHA256:[A-Za-z0-9+/]{43}$")
        keyless = self.tool.check_trust(keyless_repo(self.tmp / "keyless"))
        self.assertFalse(keyless.ok)
        self.assertIn("no release signing key", keyless.detail)
        urls = self.tool.check_release_urls(REPO_ROOT)
        self.assertTrue(urls.ok, urls.detail)  # packaging/product.json names the release locations

    def test_release_urls(self) -> None:
        tree = self.tmp / "tree"
        (tree / "packaging").mkdir(parents=True)
        product = json.loads((REPO_ROOT / "packaging" / "product.json").read_text())

        def check(**changes):
            (tree / "packaging" / "product.json").write_text(json.dumps({**product, **changes}))
            return self.tool.check_release_urls(tree)

        good = check()
        self.assertTrue(good.ok, good.detail)
        self.assertEqual(good.name, "release-urls")
        cases = {
            "release_base_url is not set": dict(release_base_url=None),
            "release_latest_url is not set": dict(release_latest_url=None),
            "release_base_url is not an https URL": dict(release_base_url="http://example.invalid/v{version}"),
            "release_latest_url is not an https URL": dict(release_latest_url="ftp://example.invalid/latest"),
            "release_base_url does not contain {version}": dict(release_base_url="https://example.invalid/v1"),
            "release_latest_url contains {version}": dict(release_latest_url="https://example.invalid/v{version}"),
        }
        for needle, changes in cases.items():
            with self.subTest(needle):
                result = check(**changes)
                self.assertFalse(result.ok)
                self.assertIn(needle, result.detail)
                self.assertIn("set them in packaging/product.json", result.detail)

    def test_parity_refuses_a_stale_contract(self) -> None:
        copy = self.tmp / "tree"
        for part in ("gateway", "tools", "packaging", "src/claude_multi/data"):
            shutil.copytree(REPO_ROOT / part, copy / part, ignore=shutil.ignore_patterns("__pycache__"))
        (copy / "src/claude_multi/data/gateway-contract.json").write_text("{}")
        check = self.tool.check_gateway_parity(copy)
        self.assertFalse(check.ok)
        self.assertIn("stale", check.detail)
        catalog = copy / "src/claude_multi/data/catalog/gateway.json"
        document = json.loads(catalog.read_text())
        document["gateway"]["patches"].reverse()
        catalog.write_text(json.dumps(document))
        self.assertIn("another patch series", self.tool.check_gateway_parity(copy).detail)

    @needs_ssh_keygen()
    def test_trust_with_a_key(self) -> None:
        key = make_key(self.tmp)
        trust_file = self.tmp / "tree" / "src" / "claude_multi" / "data" / "release-trust" / "allowed_signers"
        trust_file.parent.mkdir(parents=True)
        trust_file.write_text("# test key\n" + signers_line(key))
        check = self.tool.check_trust(self.tmp / "tree")
        self.assertTrue(check.ok, check.detail)
        self.assertIn("1 release key(s): SHA256:", check.detail)

    def test_receipt(self) -> None:
        head = "a" * 40
        runner = FakeRunner([(["rev-parse", "HEAD"], (0, head + "\n"))])
        receipt = self.tmp / "receipt.json"
        good = {"verdict": "PASS", "level": "release", "head": head, "tree_clean": True}
        receipt.write_text(json.dumps(good))
        self.assertTrue(self.tool.check_receipt(self.tmp, receipt, runner).ok)
        for change, needle in (({"verdict": "FAIL"}, "verdict"), ({"level": "stage"}, "not release"),
                               ({"head": "b" * 40}, "is not HEAD"), ({"tree_clean": False}, "not clean")):
            receipt.write_text(json.dumps({**good, **change}))
            check = self.tool.check_receipt(self.tmp, receipt, runner)
            self.assertFalse(check.ok)
            self.assertIn(needle, check.detail)
        self.assertIn("no gate receipt", self.tool.check_receipt(self.tmp, None, runner).detail)

    def test_test_and_scan_checks_report_their_commands(self) -> None:
        runner = FakeRunner([(["tests.test_hygiene"], (1, "", "FAILED (failures=2)")),
                             (["history_scan.py"], (0, "history: clean\n", ""))])
        hygiene = self.tool.check_hygiene(REPO_ROOT, runner)
        self.assertEqual((hygiene.ok, hygiene.detail), (False, "FAILED (failures=2)"))
        self.assertTrue(self.tool.check_notices(REPO_ROOT, runner).ok)
        history = self.tool.check_history(REPO_ROOT, runner, environ={})
        self.assertTrue(history.ok)
        argv = next(call for call in runner.calls if any("history_scan.py" in item for item in call))
        self.assertEqual(argv[2:4], ["--repo", str(REPO_ROOT)])
        self.assertIn("--out", argv)
        self.assertNotIn("--identifiers", argv)

    def test_the_history_check_passes_the_private_identifier_list_through(self) -> None:
        import _layout

        history_scan = _layout.load_tool("history_scan")
        self.assertEqual(self.tool.PRIVATE_IDENTIFIERS_ENV, history_scan.PRIVATE_INPUT_ENV)
        runner = FakeRunner([(["history_scan.py"], (0, "history: clean\n", ""))])
        self.tool.check_history(REPO_ROOT, runner, environ={self.tool.PRIVATE_IDENTIFIERS_ENV: "/private/list.tsv"})
        argv = next(call for call in runner.calls if any("history_scan.py" in item for item in call))
        self.assertEqual(argv[-2:], ["--identifiers", "/private/list.tsv"])

    def test_preflight_runs_every_check(self) -> None:
        runner = FakeRunner([(["status", "--porcelain"], (0, "")), (["rev-parse", "HEAD"], (0, "c" * 40 + "\n")),
                             (["tag", "--merged"], (0, "")), (["tag", "--list"], (0, ""))])
        checks = self.tool.preflight(REPO_ROOT, receipt=None, runner=runner)
        names = [check.name for check in checks]
        self.assertEqual(names, ["clean-tree", "version", "version-bump", "changelog", "drafting-markers",
                                 "contract", "gateway-parity",
                                 "notices", "hygiene", "history", "trust", "release-urls", "receipt"])
        verdicts = {check.name: check.ok for check in checks}
        # A development line (X.Y.Z-dev, AGENTS.md) is refused by the version
        # and changelog checks; the release commit sets X.Y.Z and its dated
        # entry, and then both pass.
        from claude_multi import __version__

        released = self.tool.RELEASE_VERSION.fullmatch(__version__) is not None
        self.assertEqual(verdicts["version"], released)
        self.assertEqual(verdicts["changelog"], released)
        self.assertTrue(verdicts["clean-tree"] and verdicts["contract"] and verdicts["gateway-parity"]
                        and verdicts["release-urls"])
        self.assertTrue(verdicts["trust"])  # the production key
        self.assertFalse(verdicts["receipt"])

    def test_command_line_reports_and_refuses(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(self.tool.main(["--help"]), 0)
            self.assertEqual(self.tool.main(["unknown"]), 2)
            self.assertEqual(self.tool.main(["sign", "--out", str(self.tmp)]), 1)
        self.assertIn("has no SHA256SUMS", err.getvalue())


@requires_release_tree
class BuildCommandTests(unittest.TestCase):
    def test_build_and_reproduce_call_the_build_tool(self) -> None:
        release = tool()
        tmp = Path(tempfile.mkdtemp(prefix="claude-multi-release-tool-build-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        summary = json.dumps({"assets": {}})
        compared = json.dumps({"content_identical": True})
        listing = {"installers": True}

        def built(argv):
            # A release directory as the build tool writes it.
            out = Path(argv[argv.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "MANIFEST.json").write_text(json.dumps({"assets": {"claude-multi-1.2.3-linux-x86_64.tar.gz": {}}}))
            names = ["MANIFEST.json", "claude-multi-1.2.3-linux-x86_64.tar.gz"]
            (out / names[1]).write_bytes(b"fixture archive")
            for name in ("install.sh", "install.ps1"):
                (out / name).write_text("fixture installer\n")
                if listing["installers"]:
                    names.append(name)
            (out / "SHA256SUMS").write_text("".join(
                f"{hashlib.sha256((out / name).read_bytes()).hexdigest()}  {name}\n" for name in sorted(names)))
            return completed(argv, 0, summary)

        runner = FakeRunner([(["log", "-1"], (0, "1700000000\n")), (["rev-parse", "HEAD"], (0, "d" * 40 + "\n")),
                             (["release", "compare"], (0, compared)), (["build.py", "release"], built)])
        self.assertEqual(release.build(REPO_ROOT, tmp / "out", extra=["--offline"], runner=runner), {"assets": {}})
        # A build whose SHA256SUMS leaves the installers out is no release.
        listing["installers"] = False
        with self.assertRaisesRegex(release.ReleaseError, "does not list exactly the release: missing install.ps1"):
            release.build(REPO_ROOT, tmp / "unlisted", runner=runner)
        listing["installers"] = True
        build = next(call for call in runner.calls if call[1:3] == [str(REPO_ROOT / "tools" / "build.py"), "release"])
        self.assertEqual(build[3:], ["--out", str(tmp / "out"), "--source-date-epoch", "1700000000", "--offline"])
        runner.calls.clear()
        report = release.reproduce(REPO_ROOT, tmp / "out", scratch=tmp / "scratch", runner=runner)
        self.assertEqual(report, {"content_identical": True})
        clone = next(call for call in runner.calls if call[:2] == ["git", "clone"])
        self.assertIn("--no-local", clone)
        checkout = next(call for call in runner.calls if "checkout" in call)
        self.assertEqual(checkout[-1], "d" * 40)
        compare = next(call for call in runner.calls if "compare" in call)
        self.assertEqual(compare[compare.index("--out") + 1], str(tmp / "out"))


@requires_release_tree
class SignTests(unittest.TestCase):
    def test_sign_prints_the_commands_and_never_reads_the_key(self) -> None:
        release = tool()
        out = Path("/nonexistent") / "dist"
        lines = release.sign_commands(out, "/keys/it's private")
        self.assertEqual(lines[0], "ssh-keygen -Y sign -f '/keys/it'\\''s private' -n claude-multi-release "
                                   "'/nonexistent/dist/SHA256SUMS'")
        self.assertIn("SHA256SUMS.sshsig", lines[1])
        self.assertIn("verify-draft", lines[2])


@requires_release_tree
@needs_ssh_keygen()
class DraftTests(unittest.TestCase):
    """verify-draft and publish against a fake signed draft."""

    # The fake bundles carry no gateway: the journey's other steps run (the
    # uninstall included: their launchers run this checkout's package).
    JOURNEY_ENV = {"JOURNEY_GATEWAY": "skip"}
    # Launchers that answer --version like a release and fail every other command.
    VERSION_ONLY = """#!/bin/sh
if [ "$1" = --version ]; then echo "{name} {version} (catalog 1; a launcher that only knows --version)"; exit 0; fi
exit 71
"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-release-draft-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.release = tool()
        self.key = make_key(self.tmp)
        self.host = load_tool(REPO_ROOT / "tools" / "_build" / "python_runtime.py", "python_runtime").host_target()
        self.draft = self.signed_draft("draft", real_launchers=True)
        self.signers = self.tmp / "allowed_signers"
        self.signers.write_text(signers_line(self.key))

    def signed_draft(self, name: str, *, installers_listed: bool = True, **kwargs) -> FakeRelease:
        draft = FakeRelease(self.tmp / name, "1.2.3", targets=(self.host,), **kwargs)
        shutil.copyfile(INSTALL_SH, draft.dir / "install.sh")
        shutil.copyfile(INSTALL_PS1, draft.dir / "install.ps1")
        if installers_listed:
            draft.write_sums()
        draft.sign(self.key)
        return draft

    def test_the_installers_must_be_listed_in_the_signed_checksums(self) -> None:
        """Both installers are release members: verify-draft and publish
        refuse a draft whose signed SHA256SUMS leaves either out."""

        def listed(draft: FakeRelease) -> set[str]:
            return {line.split("  ", 1)[1] for line in (draft.dir / "SHA256SUMS").read_text().splitlines()}

        self.assertTrue({"install.sh", "install.ps1"} <= listed(self.draft))
        unlisted = self.signed_draft("unlisted", installers_listed=False, real_launchers=True)
        self.assertFalse({"install.sh", "install.ps1"} & listed(unlisted))
        refusal = "SHA256SUMS does not list exactly the release: missing install.ps1, install.sh"
        with self.assertRaisesRegex(self.release.ReleaseError, refusal):
            self.release.verify_draft(unlisted.dir, repo=REPO_ROOT, allowed_signers=self.signers,
                                      journey_env=self.JOURNEY_ENV)
        repo = self.publish_repo()
        log = self.fake_gh(unlisted.dir)
        with self.assertRaisesRegex(self.release.ReleaseError, refusal):
            self.release.publish(unlisted.dir, "v1.2.3", repo=repo, stream=io.StringIO("publish 1.2.3\n"))
        self.assertEqual(self.calls(log), [])
        self.assertFalse(self.published())

    def test_verify_draft_installs_and_runs_the_release(self) -> None:
        report = self.release.verify_draft(self.draft.dir, repo=REPO_ROOT, allowed_signers=self.signers,
                                           journey_env=self.JOURNEY_ENV)
        self.assertEqual(report["version"], "1.2.3")
        self.assertEqual(sorted(report["installed"]), ["claude-multi", "claude-multi-proxy"])
        self.assertTrue(report["installed"]["claude-multi"].startswith("claude-multi 1.2.3 (catalog "))
        self.assertEqual(report["installed"]["claude-multi-proxy"], "claude-multi-proxy 1.2.3")
        self.assertTrue(report["key"].startswith("SHA256:"))
        # Beyond --version: the first-run checks of the new installation, not
        # ready only for the setup steps the install left out (--no-setup),
        # by check name (providers: nothing connected yet).
        self.assertIn("first run: ok, not ready only for what a fresh install has not set up yet "
                      "(claude, gateway, providers, models, profile, hooks)", report["journey"])
        self.assertIn("uninstall keeping the state: ok", report["journey"])
        self.assertTrue(any(line.startswith("version: claude-multi 1.2.3 (") for line in report["journey"]))
        self.assertEqual(report["journey"][-1], "done")

    def test_verify_draft_runs_more_than_version(self) -> None:
        """A signed draft whose launchers answer only --version installs and
        reports the right version, but fails verification at its first run."""

        broken = self.signed_draft("version-only", launcher=self.VERSION_ONLY)
        calls = []

        def recording(argv, **kwargs):
            calls.append((list(argv), kwargs.get("env") or {}))
            return self.release.run(argv, **kwargs)

        client = self.tmp / "claude"
        with self.assertRaisesRegex(self.release.ReleaseError, "failed its journey(.|\\n)*the first run failed: "
                                                               "doctor --first-run --json printed no first-run "
                                                               "report"):
            self.release.verify_draft(broken.dir, repo=REPO_ROOT, allowed_signers=self.signers, client=client,
                                      journey_env=self.JOURNEY_ENV, runner=recording)
        journeys = [env for argv, env in calls if argv[-2:] == ["installed", "1.2.3"]]
        self.assertEqual(len(journeys), 1)
        self.assertEqual(journeys[0]["JOURNEY_CLIENT"], str(client))  # the managed turn's pinned client
        self.assertEqual(sum(argv[-1:] == ["--version"] for argv, _env in calls), 2)

    # Launchers whose first-run checks report ready but exit 71, a failure
    # no fresh installation has (text and --json alike).
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

    def test_verify_draft_requires_a_successful_first_run(self) -> None:
        """A signed draft whose first-run checks report ready but exit
        nonzero for an unexpected reason fails verification."""

        broken = self.signed_draft("failed-first-run", launcher=self.FAILED_FIRST_RUN)
        with self.assertRaisesRegex(self.release.ReleaseError, "failed its journey(.|\\n)*the first run failed: "
                                                               "doctor --first-run --json exited 71 with ready "
                                                               "true"):
            self.release.verify_draft(broken.dir, repo=REPO_ROOT, allowed_signers=self.signers,
                                      journey_env=self.JOURNEY_ENV)

    def test_supervised_journey_needs_no_process_listing_command(self) -> None:
        try:
            with mock.patch.dict(os.environ, {"PATH": ""}):
                result = self.release.run([sys.executable, "-c", "print('journey: done')"], cwd=self.tmp,
                                          env=dict(os.environ), timeout=5, process_group=True)
        except OSError as exc:
            self.fail(f"supervision requires an external command: {exc}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("journey: done", result.stdout)

    def test_verify_draft_bounds_descendant_output_and_reports_the_shell_result(self) -> None:
        journey = self.tmp / "tree" / ".github" / "scripts" / "journey.sh"
        journey.parent.mkdir(parents=True)
        pidfile = self.tmp / "descendant.pid"
        for outcome in ("success", "failure", "timeout"):
            with self.subTest(outcome=outcome):
                journey.write_text(
                    f"#!/bin/sh\nsleep 60 &\necho $! >'{pidfile}'\n"
                    "echo 'journey: first run: ok'\necho 'journey: marker before exit'\n" +
                    ("wait\n" if outcome == "timeout" else f"exit {0 if outcome == 'success' else 17}\n"))
                # An outer deadline keeps the unfixed implementation's pipe
                # hang bounded. This exercises verify-draft, not just run().
                script = (
                    f"import sys; sys.path[:0] = {[str(REPO_ROOT / 'tests'), str(REPO_ROOT / 'src')]!r}\n"
                    "from test_release_tool import tool\n"
                    "release = tool(); release.JOURNEY_TIMEOUT = 0.5\n"
                    f"from pathlib import Path\nreport = release.verify_draft(Path({str(self.draft.dir)!r}), "
                    f"repo=Path({str(self.tmp / 'tree')!r}), allowed_signers=Path({str(self.signers)!r}))\n"
                    "print(report['journey'])\n")
                with tempfile.TemporaryFile() as output:
                    child = subprocess.Popen([sys.executable, "-c", script], cwd=self.tmp, stdout=output,
                                             stderr=output, stdin=subprocess.DEVNULL, start_new_session=True)
                    try:
                        try:
                            code = child.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            self.fail("verify-draft hung on a descendant holding journey output")
                        output.seek(0)
                        text = output.read().decode()
                        self.assertEqual(code, 0 if outcome == "success" else 1, text)
                        self.assertIn("marker before exit", text)
                        if outcome == "timeout":
                            self.assertIn("journey timed out after 0.5 seconds", text)
                        elif outcome == "failure":
                            self.assertIn("the installed release failed its journey", text)
                        pid = int(pidfile.read_text())
                        for _ in range(100):
                            if not process_running(pid):
                                break
                            time.sleep(0.02)
                        self.assertFalse(process_running(pid), "journey descendant survived")
                    finally:
                        pids = [child.pid]
                        if pidfile.exists():
                            pids.append(int(pidfile.read_text()))
                        for pid in pids:
                            for kill in (os.killpg, os.kill):
                                with contextlib.suppress(ProcessLookupError):
                                    kill(pid, signal.SIGKILL)
                        child.wait(timeout=5)

    def test_verify_draft_requires_the_journeys_first_run_line(self) -> None:
        """A journey that passes without reporting a successful first run
        (here: one that only says done) does not verify the draft."""

        journey = self.tmp / "tree" / ".github" / "scripts" / "journey.sh"
        journey.parent.mkdir(parents=True)
        journey.write_text("#!/bin/sh\necho 'journey: doctor: ok'\necho 'journey: done'\n")
        with self.assertRaisesRegex(self.release.ReleaseError, "reported no successful first run"):
            self.release.verify_draft(self.draft.dir, repo=self.tmp / "tree", allowed_signers=self.signers,
                                      journey_env=self.JOURNEY_ENV)

    def test_a_test_build_is_never_signed_verified_or_published(self) -> None:
        """A draft whose MANIFEST.json names no release key or no release
        location (a test build) is refused by sign, by verify-draft against
        the packaged trust and by publish, naming what it lacks."""

        cases = {"no release key": self.signed_draft("keyless", real_launchers=True, release_signers=0),
                 "no release location": self.signed_draft("nowhere", real_launchers=True, location=None)}
        repo = self.publish_repo()
        for needle, draft in cases.items():
            with self.subTest(needle):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(self.release.main(["sign", "--out", str(draft.dir)]), 1)
                self.assertIn(f"names {needle}", err.getvalue())
                self.assertIn("a test build is never signed", err.getvalue())
                with self.assertRaisesRegex(self.release.ReleaseError, f"names {needle}.*never signed, verified"):
                    self.release.verify_draft(draft.dir, repo=repo)
                with self.assertRaisesRegex(self.release.ReleaseError, f"names {needle}.*published"):
                    self.release.publish(draft.dir, "v1.2.3", repo=repo, stream=io.StringIO("publish 1.2.3\n"))
        both = self.signed_draft("test-build", real_launchers=True, release_signers=0, location=None)
        with self.assertRaisesRegex(self.release.ReleaseError, "names no release key .* and names no release location"):
            self.release.check_release_inputs(both.dir)
        # A test-key check (--allowed-signers) of a test build still runs: that is its purpose.
        report = self.release.verify_draft(cases["no release key"].dir, repo=REPO_ROOT, allowed_signers=self.signers,
                                           journey_env=self.JOURNEY_ENV)
        self.assertEqual(report["version"], "1.2.3")
        # The release this test signs names both: sign prints its commands.
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.release.main(["sign", "--out", str(self.draft.dir)]), 0)
        self.assertIn("ssh-keygen -Y sign", out.getvalue())

    def test_designated_test_build_with_release_inputs_is_refused(self) -> None:
        manifest_path = self.draft.dir / "MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        self.assertGreater(manifest["release_trust"]["signers"], 0)
        self.assertTrue(all(manifest["release"][key].startswith("https://") for key in ("base_url", "latest_url")))
        manifest["test_build"] = True
        manifest_path.write_text(json.dumps(manifest))
        self.draft.write_sums()
        self.draft.sign(self.key)
        repo = self.publish_repo()
        journey = repo / ".github" / "scripts" / "journey.sh"
        journey.parent.mkdir(parents=True)
        for name in ("journey.sh", "journey_fixture.py"):
            shutil.copyfile(REPO_ROOT / ".github" / "scripts" / name, journey.parent / name)
        log = self.fake_gh()
        with self.subTest("sign"):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(self.release.main(["sign", "--out", str(self.draft.dir)]), 1)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("designated a test build", err.getvalue())
        with self.subTest("production verification"), self.assertRaisesRegex(
                self.release.ReleaseError, "designated a test build"):
            self.release.verify_draft(self.draft.dir, repo=repo, journey_env=self.JOURNEY_ENV)
        with self.subTest("publication"), self.assertRaisesRegex(
                self.release.ReleaseError, "designated a test build"):
            self.release.publish(self.draft.dir, "v1.2.3", repo=repo, stream=io.StringIO("publish 1.2.3\n"))
        self.assertEqual(self.calls(log), [])
        self.assertFalse(self.published())
        # Explicit test-key verification still checks the signed listing and runs the journey.
        report = self.release.verify_draft(self.draft.dir, repo=REPO_ROOT, allowed_signers=self.signers,
                                          journey_env=self.JOURNEY_ENV)
        self.assertEqual(report["version"], "1.2.3")

    def test_verify_draft_refusals(self) -> None:
        with self.assertRaisesRegex(self.release.ReleaseError, "no release signing key.*--allowed-signers"):
            self.release.verify_draft(self.draft.dir, repo=keyless_repo(self.tmp / "keyless"))
        # The production key does not verify this test-key signature.
        with self.assertRaisesRegex(self.release.ReleaseError, "does not verify"):
            self.release.verify_draft(self.draft.dir, repo=REPO_ROOT)
        other = make_key(self.tmp, "other")
        wrong = self.tmp / "wrong_signers"
        wrong.write_text(signers_line(other))
        with self.assertRaisesRegex(self.release.ReleaseError, "does not verify"):
            self.release.verify_draft(self.draft.dir, repo=REPO_ROOT, allowed_signers=wrong)
        archive = next(self.draft.dir.glob("*.tar.gz"))
        archive.write_bytes(archive.read_bytes() + b"\0")
        with self.assertRaisesRegex(self.release.ReleaseError, "does not match SHA256SUMS"):
            self.release.verify_draft(self.draft.dir, repo=REPO_ROOT, allowed_signers=self.signers)

    def publish_repo(self) -> Path:
        repo = self.tmp / "repo"
        trust_file = repo / "src" / "claude_multi" / "data" / "release-trust" / "allowed_signers"
        trust_file.parent.mkdir(parents=True)
        trust_file.write_text(signers_line(self.key))
        return repo

    FAKE_GH = """#!{python}
# gh as tools/release.py uses it, over a draft release kept in a directory.
import json, os, shutil, sys
from pathlib import Path

remote = Path(os.environ["FAKE_GH_REMOTE"])
state = remote.parent / "is-draft"
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if "--repo" in args:
    del args[args.index("--repo"):args.index("--repo") + 2]
if args[:2] == ["release", "view"]:
    print(json.dumps({{"isDraft": state.read_text() == "true",
                      "assets": [{{"name": p.name, "size": p.stat().st_size}} for p in sorted(remote.iterdir())]}}))
elif args[:2] == ["release", "download"]:
    target = Path(args[args.index("--dir") + 1])
    for index, arg in enumerate(args):
        if arg == "--pattern" and (remote / args[index + 1]).is_file():
            shutil.copyfile(remote / args[index + 1], target / args[index + 1])
elif args[:2] == ["release", "upload"]:
    shutil.copyfile(args[3], remote / Path(args[3]).name)
elif args[:2] == ["release", "edit"] and "--draft=false" in args:
    state.write_text("false")
else:
    sys.exit("fake gh: unexpected " + " ".join(args))
"""

    def fake_gh(self, draft: Path | None = None) -> Path:
        """gh on PATH, serving a draft that holds ``draft``'s published
        files (default this test's draft); returns its call log."""

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(self.FAKE_GH.format(python=sys.executable))
        (bin_dir / "gh").chmod(0o755)
        self.remote = self.tmp / "remote" / "assets"
        self.remote.mkdir(parents=True)
        (self.remote.parent / "is-draft").write_text("true")
        source = draft or self.draft.dir
        for path in sorted(source.iterdir()):
            if path.name != "SHA256SUMS.sshsig":  # the draft is uploaded unsigned
                shutil.copyfile(path, self.remote / path.name)
        log = self.tmp / "gh.log"
        patcher = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
                                               "FAKE_GH_REMOTE": str(self.remote), "FAKE_GH_LOG": str(log)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return log

    def calls(self, log: Path) -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    def published(self) -> bool:
        return (self.remote.parent / "is-draft").read_text() == "false"

    def test_publish_needs_the_typed_confirmation(self) -> None:
        repo = self.publish_repo()
        log = self.fake_gh()
        prompts = io.StringIO()
        with contextlib.redirect_stderr(prompts):
            with self.assertRaises(self.release.Declined):
                self.release.publish(self.draft.dir, "v1.2.3", repo=repo, stream=io.StringIO("yes\n"))
        self.assertIn("Type 'publish 1.2.3' to continue", prompts.getvalue())
        # The draft was checked before the question; nothing was uploaded or published.
        self.assertEqual([call.split()[:2] for call in self.calls(log)], [["release", "view"], ["release", "download"]])
        self.assertFalse(self.published())
        with self.assertRaisesRegex(self.release.ReleaseError, "does not name the release"):
            self.release.publish(self.draft.dir, "v1.2.4", repo=repo, stream=io.StringIO("publish 1.2.3\n"))
        log.unlink()
        with contextlib.redirect_stderr(io.StringIO()):
            report = self.release.publish(self.draft.dir, "v1.2.3", repo=repo, repository="example/claude-multi",
                                          stream=io.StringIO("publish 1.2.3\n"))
        self.assertEqual(report["published"], "v1.2.3")
        calls = self.calls(log)
        files = sorted(path.name for path in self.draft.dir.iterdir() if path.name != "SHA256SUMS.sshsig")
        self.assertEqual(calls[0], "release view v1.2.3 --json isDraft,assets --repo example/claude-multi")
        self.assertTrue(calls[1].startswith("release download v1.2.3 --dir "))
        self.assertEqual(calls[1].split(" --pattern ")[1:], [*files[:-1], files[-1] + " --repo example/claude-multi"])
        self.assertEqual(calls[2], f"release upload v1.2.3 {self.draft.dir / 'SHA256SUMS.sshsig'} --clobber "
                                   "--repo example/claude-multi")
        self.assertEqual(calls[3], "release view v1.2.3 --json isDraft,assets --repo example/claude-multi")
        # Right before the promotion every asset is downloaded again, the signature included.
        signed = sorted([*files, "SHA256SUMS.sshsig"])
        self.assertTrue(calls[4].startswith("release download v1.2.3 --dir "))
        self.assertEqual(calls[4].split(" --pattern ")[1:], [*signed[:-1], signed[-1] + " --repo example/claude-multi"])
        self.assertEqual(calls[5], "release edit v1.2.3 --draft=false --repo example/claude-multi")
        self.assertEqual(len(calls), 6)
        self.assertTrue(self.published())
        self.assertEqual((self.remote / "SHA256SUMS.sshsig").read_bytes(),
                         (self.draft.dir / "SHA256SUMS.sshsig").read_bytes())

    def test_publish_refuses_a_stale_draft_of_the_same_version(self) -> None:
        """The draft holds an earlier build of this version: its checksums
        would not match the new signature, so nothing is uploaded or
        published."""

        repo = self.publish_repo()
        earlier = self.signed_draft("earlier", real_launchers=True, release_date="2026-10-14")
        log = self.fake_gh(earlier.dir)
        with self.assertRaisesRegex(self.release.ReleaseError, "holds other bytes than this release for "
                                                               "MANIFEST.json, SHA256SUMS"):
            self.release.publish(self.draft.dir, "v1.2.3", repo=repo, stream=io.StringIO("publish 1.2.3\n"))
        self.assertEqual([call.split()[:2] for call in self.calls(log)], [["release", "view"], ["release", "download"]])
        self.assertFalse(self.published())
        self.assertFalse((self.remote / "SHA256SUMS.sshsig").exists())

    def test_publish_refuses_a_draft_with_other_files_or_none(self) -> None:
        repo = self.publish_repo()
        log = self.fake_gh()
        (self.remote / "install.ps1").unlink()
        (self.remote / "notes.txt").write_text("x")
        with self.assertRaisesRegex(self.release.ReleaseError, r"does not hold this release's files \(missing "
                                                               r"install.ps1; not part of this release: notes.txt\)"):
            self.release.publish(self.draft.dir, "v1.2.3", repo=repo, stream=io.StringIO("publish 1.2.3\n"))
        (self.remote / "notes.txt").unlink()
        shutil.copyfile(self.draft.dir / "install.ps1", self.remote / "install.ps1")
        (self.remote.parent / "is-draft").write_text("false")
        with self.assertRaisesRegex(self.release.ReleaseError, "is not a draft"):
            self.release.publish(self.draft.dir, "v1.2.3", repo=repo, stream=io.StringIO("publish 1.2.3\n"))
        self.assertFalse([call for call in self.calls(log) if call.split()[1] in ("upload", "edit")])
        (self.draft.dir / "install.ps1").unlink()
        with self.assertRaisesRegex(self.release.ReleaseError, "has no install.ps1"):
            self.release.publish(self.draft.dir, "v1.2.3", repo=repo, stream=io.StringIO("publish 1.2.3\n"))

    class AnswerAfter:
        """The typed confirmation, answered only after ``change`` ran: the
        draft changed while the question waited."""

        def __init__(self, change) -> None:
            self.change = change

        def readline(self) -> str:
            self.change()
            return "publish 1.2.3\n"

    def publish_changed_while_waiting(self, change, refusal: str) -> list[str]:
        """Publish this test's draft with ``change`` applied to the remote
        draft during the confirmation: refused with ``refusal``, nothing
        published. Returns the gh calls."""

        repo = self.publish_repo()
        log = self.fake_gh()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(self.release.ReleaseError, refusal):
                self.release.publish(self.draft.dir, "v1.2.3", repo=repo, stream=self.AnswerAfter(change))
        self.assertFalse(self.published())
        calls = self.calls(log)
        self.assertFalse([call for call in calls if call.split()[1] == "edit"])
        return calls

    def test_publish_refuses_an_archive_replaced_while_the_confirmation_waits(self) -> None:
        """An archive replaced on the draft under its own name (same size,
        SHA256SUMS unchanged) while the confirmation waits: the release
        stays a draft."""

        archive = next(path.name for path in self.draft.dir.glob("*.tar.gz"))

        def replace() -> None:
            data = bytearray((self.remote / archive).read_bytes())
            data[len(data) // 2] ^= 0xFF
            (self.remote / archive).write_bytes(bytes(data))

        calls = self.publish_changed_while_waiting(
            replace, f"holds other bytes than this release for {re.escape(archive)} .*nothing was published")
        # The signature was attached to the draft; the check that refused ran after it.
        self.assertEqual([call.split()[1] for call in calls], ["view", "download", "upload", "view", "download"])

    def test_publish_refuses_an_installer_replaced_while_the_confirmation_waits(self) -> None:
        def replace() -> None:
            with open(self.remote / "install.sh", "a") as handle:
                handle.write("echo replaced\n")

        self.publish_changed_while_waiting(
            replace, "holds other bytes than this release for install.sh .*nothing was published")

    def test_publish_refuses_an_asset_removed_or_added_while_the_confirmation_waits(self) -> None:
        self.publish_changed_while_waiting(
            lambda: (self.remote / "MANIFEST.json").unlink(),
            r"does not hold this release's files \(missing MANIFEST.json\); nothing was published")
        for name in ("repo", "bin", "remote"):  # a fresh draft, gh and trust
            shutil.rmtree(self.tmp / name)
        (self.tmp / "gh.log").unlink()
        self.publish_changed_while_waiting(
            lambda: (self.remote / "notes.txt").write_text("x"),
            r"does not hold this release's files \(not part of this release: notes.txt\); nothing was published")

    def test_publish_refuses_without_a_terminal_or_production_key(self) -> None:
        with self.assertRaisesRegex(self.release.ReleaseError, "no release signing key"):
            self.release.publish(self.draft.dir, "v1.2.3", repo=keyless_repo(self.tmp / "keyless"),
                                 stream=io.StringIO("publish 1.2.3\n"))
        log = self.fake_gh()
        stdin = sys.stdin
        sys.stdin = io.StringIO("publish 1.2.3\n")
        self.addCleanup(setattr, sys, "stdin", stdin)
        with self.assertRaisesRegex(self.release.ReleaseError, "typed on a terminal"):
            self.release.publish(self.draft.dir, "v1.2.3", repo=self.publish_repo())
        self.assertFalse([call for call in self.calls(log) if call.split()[1] in ("upload", "edit")])

if __name__ == "__main__":
    unittest.main()
