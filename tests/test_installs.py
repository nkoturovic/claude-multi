"""Release channels: the state root's channel marker, the guard and the install roots.

Temporary HOMEs and state roots only; the launcher subprocess runs from this
checkout (channel ``source``) against an empty temporary HOME.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT
from _layout import checkout_replica
from claude_multi import cli, endpoint, installs, state


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-installs-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.state_root = self.home / ".local" / "state" / "claude-multi"

    def env(self, channel: str | None = None, **extra: str) -> dict[str, str]:
        result = {"HOME": str(self.home), **extra}
        if channel is not None:
            result[endpoint.CHANNEL_ENV] = channel
        return result


class MarkerTests(_Home):
    def test_the_first_writing_run_records_its_channel(self) -> None:
        check = installs.check(self.state_root, self.env("nix"), record=False)
        self.assertTrue(check.ok)
        self.assertFalse(os.path.lexists(installs.marker_path(self.state_root)))  # read-only: nothing written
        check = installs.check(self.state_root, self.env("nix"), record=True)
        self.assertTrue(check.ok and check.recorded)
        path = installs.marker_path(self.state_root)
        self.assertEqual(path.read_bytes(), b"nix\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(installs.read_marker(self.state_root), "nix")
        again = installs.check(self.state_root, self.env("nix"), record=True)
        self.assertTrue(again.ok and not again.recorded)

    def test_another_channel_refuses_with_a_hint(self) -> None:
        installs.write_marker(self.state_root, "bundle")
        check = installs.check(self.state_root, self.env("nix"), record=True)
        self.assertFalse(check.ok)
        self.assertIn("belongs to the bundle installation", check.problem)
        self.assertIn("doctor", check.remedy)
        self.assertEqual(installs.read_marker(self.state_root), "bundle")  # never rewritten
        installs.write_marker(self.state_root, "nix")
        check = installs.check(self.state_root, self.env("bundle"), record=True)
        self.assertIn("--migrate-from-nix", check.remedy)

    def test_an_unknown_launcher_channel_neither_records_nor_refuses(self) -> None:
        self.assertTrue(installs.check(self.state_root, self.env(), record=True).ok)
        self.assertFalse(os.path.lexists(installs.marker_path(self.state_root)))
        installs.write_marker(self.state_root, "source")
        self.assertTrue(installs.check(self.state_root, self.env("homebrew"), record=True).ok)

    def test_two_first_runs_of_different_channels_never_both_own_the_state(self) -> None:
        import threading

        barrier = threading.Barrier(2, timeout=10)
        local = threading.local()
        real = installs.read_marker

        def racing_read(*args, **kwargs):
            result = real(*args, **kwargs)
            if not getattr(local, "raced", False):  # both read "absent" before either claims
                local.raced = True
                barrier.wait()
            return result

        results: dict[str, installs.ChannelCheck] = {}

        def run(channel: str) -> None:
            results[channel] = installs.check(self.state_root, self.env(channel), record=True)

        with mock.patch.object(installs, "read_marker", side_effect=racing_read):
            threads = [threading.Thread(target=run, args=(channel,)) for channel in ("bundle", "nix")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        owner = installs.read_marker(self.state_root)
        self.assertIn(owner, ("bundle", "nix"))
        self.assertTrue(results[owner].ok and results[owner].recorded)
        other = "nix" if owner == "bundle" else "bundle"
        self.assertFalse(results[other].ok)
        self.assertEqual(results[other].owner, owner)
        self.assertIn(f"belongs to the {owner} installation", results[other].problem)

    def test_a_claim_that_cannot_be_written_refuses_the_writing_run(self) -> None:
        with mock.patch.object(installs, "write_marker", side_effect=OSError(28, "No space left on device")):
            check = installs.check(self.state_root, self.env("bundle"), record=True)
        self.assertFalse(check.ok)
        self.assertIn("could not record itself as the owner", check.problem)
        self.assertIsNone(installs.read_marker(self.state_root))

    def test_unknown_marker_content_fails_closed(self) -> None:
        state.ensure_private_dir(self.state_root)
        for raw in (b"bundle", b"Bundle\n", b"bundle\nnix\n", b"pip\n", b"", b" nix\n"):
            state.atomic_write(installs.marker_path(self.state_root), raw)
            with self.subTest(raw=raw):
                check = installs.check(self.state_root, self.env("bundle"), record=True)
                self.assertFalse(check.ok)
                self.assertIn("channel", check.remedy)
                self.assertEqual(installs.marker_path(self.state_root).read_bytes(), raw)
        installs.marker_path(self.state_root).unlink()
        installs.marker_path(self.state_root).symlink_to("/etc/hostname")
        self.assertFalse(installs.check(self.state_root, self.env("bundle"), record=True).ok)


class GuardTests(_Home):
    def runtime(self, channel: str) -> cli.Runtime:
        return cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.env(channel), cwd=self.root,
                           managed_root=self.root / "managed", health_get=lambda *_a: 200)

    def run_cli(self, argv: list[str], channel: str) -> tuple[int, str]:
        """``(exit, stdout then stderr)``."""

        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(argv, runtime=self.runtime(channel), output_stream=out, interactive=False)
        return code, out.getvalue() + err.getvalue()

    def test_a_launcher_of_another_channel_refuses_and_doctor_reports_it(self) -> None:
        installs.write_marker(self.state_root, "bundle")
        code, out = self.run_cli(["profile", "list"], "nix")
        self.assertEqual(code, 1)
        self.assertIn("belongs to the bundle installation, and this is the nix one", out)
        self.assertIn("fix:", out)
        with mock.patch("claude_multi.cli.doctor._doctor_install_report",
                        wraps=__import__("claude_multi.cli.doctor", fromlist=["x"])._doctor_install_report) as report:
            code, out = self.run_cli(["doctor"], "nix")
        report.assert_called_once()
        self.assertIn("installation: this account's claude-multi state belongs to the bundle installation", out)
        self.assertEqual(installs.read_marker(self.state_root), "bundle")

    def test_lineup_requests_pass_the_same_guard(self) -> None:
        from claude_multi import lineup as lineup_mod

        installs.write_marker(self.state_root, "bundle")
        session = "11111111-2222-4333-8444-555555555555"
        with mock.patch.object(lineup_mod, "apply", side_effect=AssertionError("lineup ran")):
            # A skill request (the /cm path): refused on stdout, exit 0, nothing applied.
            code, out = self.run_cli(["lineup", "--session", session, "pin"], "source")
            self.assertEqual(code, 0)
            self.assertIn("belongs to the bundle installation, and this is the source one", out)
            self.assertIn("fix:", out)
            # A terminal request: refused with exit 1.
            runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.env("source", CLAUDE_CODE_SESSION_ID=session),
                                  cwd=self.root, managed_root=self.root / "managed", health_get=lambda *_a: 200)
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli.main(["lineup", "pin"], runtime=runtime, output_stream=io.StringIO(), interactive=False)
            self.assertEqual(code, 1)
            self.assertIn("belongs to the bundle installation", err.getvalue())
        self.assertEqual(installs.read_marker(self.state_root), "bundle")

    def test_the_same_channel_runs_and_a_new_state_root_is_recorded(self) -> None:
        # A listing reads and records nothing; the first writing command claims the root.
        code, out = self.run_cli(["profile", "list"], "bundle")
        self.assertEqual(code, 0, out)
        self.assertIsNone(installs.read_marker(self.state_root))
        code, out = self.run_cli(["profile", "duplicate", "balanced", "copy"], "bundle")
        self.assertEqual(code, 0, out)
        self.assertEqual(installs.read_marker(self.state_root), "bundle")

    def test_the_checkout_launcher_records_source(self) -> None:
        empty = self.root / "empty-home"
        empty.mkdir(mode=0o700)
        cwd = self.root / "cwd"
        cwd.mkdir()
        launcher = checkout_replica(self.root / "checkout") / "bin" / "claude-multi"
        env = {"HOME": str(empty), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        result = subprocess.run([sys.executable, str(launcher), "profile", "duplicate", "balanced", "copy"],
                                cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((empty / ".local/state/claude-multi/channel").read_bytes(), b"source\n")
        installs.write_marker(empty / ".local/state/claude-multi", "nix")
        result = subprocess.run([sys.executable, str(launcher), "profile", "list"],
                                cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("belongs to the nix installation", result.stderr)


class InstallRootTests(_Home):
    def test_roots_links_and_discovery(self) -> None:
        env = self.env()
        data = state.ensure_private_dir(self.home / ".local/share/claude-multi")
        self.assertEqual(installs.bundle_link(env), data / "install/current")
        self.assertEqual(installs.nix_link(env), data / "nix/current")
        self.assertEqual(installs.discover(env), [])
        version = data / "install/versions/1.0.0/bin"
        version.mkdir(parents=True)
        (version / "claude-multi-proxy").write_text("#!/bin/sh\n")
        (data / "install/current").symlink_to("versions/1.0.0")
        release = self.home / ".local/share/claude-multi-release"
        release.mkdir()
        (release / "current").symlink_to("/nix/store/0000-claude-multi")
        found = installs.discover(env)
        self.assertEqual([item.channel for item in found], ["bundle", "nix"])
        self.assertTrue(found[0].line(env).startswith("bundle: ~/.local/share/claude-multi/install/current -> "))
        self.assertIn("(dangling)", found[1].line(env))
        selection = installs.plan_selection("bundle", env, None)
        self.assertEqual(selection.root, str((data / "install/versions/1.0.0").resolve()))
        self.assertIs(installs.apply_selection(selection, env), selection)  # bundles: nothing is written
        for channel in ("source", None):
            with self.subTest(channel=channel), self.assertRaises(installs.SelectionError):
                installs.plan_selection(channel, env, REPO_ROOT)

    def test_the_nix_selection_links_a_store_path_and_restores(self) -> None:
        store = self.root / "store"
        package = store / "aaaa-claude-multi"
        (package / "bin").mkdir(parents=True)
        for name in ("claude-multi", "claude-multi-proxy"):
            (package / "bin" / name).write_text("#!/bin/sh\n")
        state.ensure_private_dir(self.home / ".local/share/claude-multi")
        env = self.env(CLAUDE_MULTI_HOOK_COMMAND=str(package / "bin/claude-multi"), PATH=str(self.root / "no-tools"))
        with mock.patch.object(installs, "STORE_PREFIX", str(store) + "/"):
            self.assertEqual(installs.install_root(env), package)
            selection = installs.plan_selection("nix", env, None)
            self.assertTrue(selection.changed)
            applied = installs.apply_selection(selection, env)
            self.assertFalse(applied.gc_root)  # no nix-store on PATH
            self.assertIn("not a garbage-collector root", applied.note)
            link = installs.nix_link(env)
            self.assertEqual(os.readlink(link), str(package))
            self.assertEqual(stat_mode(link.parent), 0o700)
            installs.restore_selection(applied)
            self.assertFalse(os.path.lexists(link))
            installs.apply_selection(selection, env)
            self.assertEqual(installs.remove_nix_link(env), str(package))
            self.assertFalse(os.path.lexists(link))
            outside = self.env(CLAUDE_MULTI_HOOK_COMMAND=str(REPO_ROOT / "bin/claude-multi"))
            with self.assertRaises(installs.SelectionError):
                installs.plan_selection("nix", outside, None)

    def test_gc_root_registration_is_bounded_and_checked(self) -> None:
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "nix-store").write_text("#!/bin/sh\n")
        (tools / "nix-store").chmod(0o755)
        link = self.root / "link"
        target = self.root / "target"
        target.mkdir()
        link.symlink_to(target)
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs["timeout"]))
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.assertTrue(installs.register_gc_root(link, str(target), {"PATH": str(tools)}, runner=runner))
        self.assertEqual(calls[0][0], [str(tools / "nix-store"), "--add-root", str(link), "--indirect",
                                       "--realise", str(target)])
        self.assertEqual(calls[0][1], installs.GC_ROOT_TIMEOUT)
        failing = lambda argv, **_k: subprocess.CompletedProcess(argv, 1, "", "")  # noqa: E731
        self.assertFalse(installs.register_gc_root(link, str(target), {"PATH": str(tools)}, runner=failing))


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
