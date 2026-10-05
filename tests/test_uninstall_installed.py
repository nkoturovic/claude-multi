"""``claude-multi uninstall`` against a real installation.

A bundle built from this checkout (its package, real launchers) is
installed by the real ``packaging/install.sh`` into a temporary HOME, as the
installer tests do; then the installed ``claude-multi uninstall`` (and
``install.sh --uninstall``, which runs it) removes what the installer's
receipt proves: the launchers whose sha256 still matches, the PATH line
exactly as written, the installed release, while the release it runs from
is being removed. Uninstall needs a terminal, so it runs on a
pseudo-terminal whose answers the test scripts. Credentials go only after
the typed phrase; ``--keep-setup`` keeps the state and setup; and a
credential save that failed (ENOSPC) and whose log was pruned since still
refuses the uninstall.
"""

from __future__ import annotations

import errno
import os
import pty
import re
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from claude_multi import install_receipt, state
from _fake_release import FakeRelease, make_key, needs_ssh_keygen, signers_line
from _release import INSTALL_SH, requires_release_tree

UNAME = """#!/bin/sh
case $1 in
-m) echo x86_64 ;;
-r) echo 6.1.0-generic ;;
*) echo Linux ;;
esac
"""
LAUNCHERS = ("claude-multi", "claude-multi-proxy")
USER_PROFILE = "# my shell\nalias ll='ls -l'\n"
KEY_NAME = "DEEPSEEK_CLAUDE_API_KEY"
KEY_VALUE = "fixture-key-value-never-shown"
PHRASE_PROMPT = re.compile(rb"press Enter to keep them:\s*$")
QUESTION = re.compile(rb"\[y/N\]\s*$")


def run_on_terminal(argv: list[str], *, env: dict[str, str], cwd: Path, phrase: bytes = b"",
                    timeout: float = 120.0) -> tuple[int, str]:
    """Run ``argv`` with a pseudo-terminal as stdin, stdout and stderr:
    ``y`` to a y/N question, ``phrase`` (then Enter) at the credential
    question; returns the exit status and everything it printed."""

    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd,
                                   start_new_session=True, close_fds=True)
    finally:
        os.close(slave)
    output = b""
    tail = b""
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() > deadline:
                process.kill()
                raise AssertionError(f"{argv[1:]} did not finish:\n{output.decode(errors='replace')}")
            ready, _, _ = select.select([master], [], [], 0.2)
            if not ready:
                if process.poll() is not None:
                    break
                continue
            try:
                data = os.read(master, 4096)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                data = b""
            if not data:
                break
            output += data
            tail = (tail + data)[-256:]
            if QUESTION.search(tail):
                os.write(master, b"y\n")
                tail = b""
            elif PHRASE_PROMPT.search(tail):
                os.write(master, phrase + b"\n")
                tail = b""
    finally:
        os.close(master)
    code = process.wait(timeout=30)
    return code, output.decode(errors="replace").replace("\r\n", "\n")


@requires_release_tree
@needs_ssh_keygen()
class InstalledUninstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(tempfile.mkdtemp(prefix="claude-multi-uninstall-installed-"))
        os.chmod(cls.base, 0o700)
        cls.fakebin = cls.base / "fakebin"
        cls.fakebin.mkdir()
        (cls.fakebin / "uname").write_text(UNAME)
        os.chmod(cls.fakebin / "uname", 0o755)
        keys = cls.base / "keys"
        keys.mkdir()
        cls.key = make_key(keys)
        cls.signers = keys / "allowed_signers"
        cls.signers.write_text(signers_line(cls.key))
        cls.release = FakeRelease(cls.base / "release", "1.0.0", real_launchers=True)
        cls.release.sign(cls.key)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="case-", dir=self.base))
        self.home = self.tmp / "home dir"  # a space in HOME on purpose
        self.home.mkdir(mode=0o700)
        self.bin = self.home / ".local/bin"
        self.install_root = self.home / ".local/share/claude-multi/install"
        self.state = self.home / ".local/state/claude-multi"
        self.config = self.home / ".config/claude-multi"
        self.bashrc = self.home / ".bashrc"
        self.bashrc.write_text(USER_PROFILE)
        # The caller's PATH (the installer's tools may live anywhere, as in
        # the Nix sandbox); only uname is a stand-in.
        self.env = {"HOME": str(self.home), "PATH": f"{self.fakebin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                    "LC_ALL": "C", "TMPDIR": str(self.tmp), "SHELL": "/bin/bash", "TERM": "dumb"}
        result = subprocess.run(
            ["sh", str(INSTALL_SH), "--from-dir", str(self.release.dir), "--allowed-signers", str(self.signers),
             "--no-setup", "--modify-path"],
            env=self.env, cwd=self.tmp, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL,
            start_new_session=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.release_dir = self.install_root / os.readlink(self.install_root / "current")
        # What the installer wrote: its launchers with their sha256 and one PATH line.
        receipt = install_receipt.read(self.install_root)
        self.assertEqual(sorted(Path(item.path).name for item in receipt.launchers), sorted(LAUNCHERS))
        self.assertTrue(all(item.owned() for item in receipt.launchers))
        self.assertEqual([item.file for item in receipt.path_lines], [str(self.bashrc)])
        self.assertIn(receipt.path_lines[0].line, self.bashrc.read_text().splitlines())
        # The gateway was set up here, on a port of its own (a free one: no
        # observation ever looks at another program's gateway).
        import socket

        from claude_multi import endpoint

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        endpoint.write_config(self.home, endpoint.parse_config({"version": 1, "host": "127.0.0.1", "port": port}))
        # Some setup, state and credentials of the user's.
        self.profile = self.private(self.config / "profiles" / "mine.json", b"{}\n")
        self.key_file = self.private(self.config / "secrets" / "provider-keys.env",
                                     f"{KEY_NAME}={KEY_VALUE}\n".encode())
        self.gateway_key = self.private(self.config / "api-key", b"0123456789abcdef0123456789abcdef\n")
        self.notice = self.private(self.state / "notice" / "fixture.seen", b"1\n")

    def private(self, path: Path, data: bytes) -> Path:
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, data)
        return path

    def uninstall(self, *args: str, phrase: bytes = b"", via_installer: bool = False) -> tuple[int, str]:
        env = dict(self.env)
        if via_installer:
            argv = ["sh", str(INSTALL_SH), "--uninstall", "--", *args]
        else:
            argv = [str(self.bin / "claude-multi"), "uninstall", *args]
        return run_on_terminal(argv, env=env, cwd=self.tmp, phrase=phrase)

    def assertProgramRemoved(self, code: int, out: str, *, kept_launchers: tuple[str, ...] = ()) -> None:
        self.assertEqual(code, 0, out)
        # The command finished, and said so, after the release it runs from was removed.
        self.assertIn("claude-multi is uninstalled.", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("ModuleNotFoundError", out)
        self.assertFalse(self.release_dir.exists())
        self.assertFalse((self.install_root / "current").is_symlink())
        self.assertFalse((self.install_root / "installer.json").exists())
        for name in LAUNCHERS:
            self.assertEqual((self.bin / name).exists(), name in kept_launchers, name)
        # Exactly the installer's line went; the user's lines are byte for byte as they were.
        self.assertEqual(self.bashrc.read_text(), USER_PROFILE)

    def test_keep_setup_removes_the_program_and_keeps_state_setup_and_credentials(self) -> None:
        code, out = self.uninstall("--keep-setup", "--yes")
        self.assertProgramRemoved(code, out)
        for kept in (self.notice, self.profile, self.key_file, self.gateway_key):
            self.assertTrue(kept.exists(), kept)
        # The plan names the key, never its value; Enter at the question kept the credentials.
        self.assertIn(f"API keys: {KEY_NAME}", out)
        self.assertNotIn(KEY_VALUE, out)
        self.assertIn("Delete your credentials too?", out)
        self.assertNotIn("also revoke these keys", out)

    def test_only_the_typed_phrase_deletes_credentials_and_a_changed_launcher_stays(self) -> None:
        changed = self.bin / "claude-multi-proxy"
        changed.write_text(changed.read_text() + "# my own change\n")
        code, out = self.uninstall("--yes", phrase=b"yes")  # anything but the phrase keeps them
        self.assertProgramRemoved(code, out, kept_launchers=("claude-multi-proxy",))
        self.assertIn("~/.local/bin/claude-multi-proxy: changed since it was installed — kept", out)
        self.assertTrue(changed.read_text().endswith("# my own change\n"))
        self.assertTrue(self.key_file.exists() and self.gateway_key.exists())
        self.assertFalse(self.profile.exists())  # the setup went (no --keep-setup)
        self.assertFalse(self.notice.exists())  # and so did the state

    def test_the_typed_phrase_deletes_the_credentials(self) -> None:
        code, out = self.uninstall("--yes", phrase=b"delete credentials")
        self.assertProgramRemoved(code, out)
        self.assertFalse(self.key_file.exists())
        self.assertFalse(self.gateway_key.exists())
        self.assertIn(f"also revoke these keys at their providers: {KEY_NAME}.", out)
        self.assertNotIn(KEY_VALUE, out)

    def test_install_sh_uninstall_runs_the_same_command(self) -> None:
        code, out = self.uninstall("--keep-setup", "--yes", via_installer=True)
        self.assertProgramRemoved(code, out)
        self.assertIn("claude-multi uninstall — plan (nothing has been removed yet)", out)
        self.assertTrue(self.notice.exists() and self.profile.exists() and self.key_file.exists())

    def test_a_failed_credential_save_whose_log_was_pruned_still_refuses(self) -> None:
        """ENOSPC at a credential save, the log pruned at a later start (the
        failure persists as a hold): uninstall refuses and removes nothing,
        although the gateway is not running."""

        import datetime

        from claude_multi import gateway_hold, service
        from claude_multi.platform import file_log
        from test_gateway_lifecycle import save_line

        workdir = service.ensure_gateway_workdir(self.state)
        started = datetime.datetime(2026, 10, 2, 11, 0, tzinfo=datetime.timezone.utc)
        descriptor, log = file_log.create_instance_log(workdir / service.GATEWAY_LOGS, started, "1111111111111111")
        os.write(descriptor, (save_line("failed") + "\n").encode())  # errno 28: no space left
        os.close(descriptor)
        pruned = gateway_hold.prune_at_start(self.state, ("claude",), budget=0)
        self.assertEqual((pruned.removed, pruned.persisted), (("1111111111111111",), ("1111111111111111",)))
        self.assertFalse(log.exists())
        code, out = self.uninstall("--yes", "--keep-credentials")
        self.assertEqual(code, 1, out)
        self.assertIn("a sign-in save is not confirmed yet", out)
        self.assertIn("nothing was removed", out)
        self.assertTrue(self.release_dir.is_dir())
        for name in LAUNCHERS:
            self.assertTrue((self.bin / name).exists(), name)
        self.assertNotEqual(self.bashrc.read_text(), USER_PROFILE)  # the PATH line is still there
        self.assertTrue(self.notice.exists() and self.profile.exists())


if __name__ == "__main__":
    unittest.main()
