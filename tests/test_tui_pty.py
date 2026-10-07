"""Deterministic PTY smoke tests for quick confirm, curses form, and dumb mode."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT


# Child scripts load the frozen fixture asset root; their PYTHONPATH is the
# package src (never the fixture).
CATALOG_ROOT = FIXTURE_ROOT
SRC_ROOT = REPO_ROOT / "src"
TIMEOUT = 6.0
# Exit waits are load-tolerant: the full suite starves PTY children.
FINISH_TIMEOUT = 30.0


def _set_size(fd: int, rows: int, columns: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


LINE_CONFIRM = "Enter launch · q quit: ".encode()


class PTYProcess:
    def __init__(
        self,
        code: str,
        *,
        rows: int = 24,
        columns: int = 80,
        extra_env=None,
        extra_pythonpath=(),
    ):
        self.master, self.slave = pty.openpty()
        _set_size(self.slave, rows, columns)
        self.before = termios.tcgetattr(self.slave)
        env = dict(os.environ)
        env.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                # Screen children add the tests dir so they
                # can build their Runtime with _tui_fixture.
                "PYTHONPATH": os.pathsep.join([str(SRC_ROOT), *map(str, extra_pythonpath)]),
                "TERM": "xterm-256color",
            }
        )
        if extra_env:
            env.update(extra_env)
        self.process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            close_fds=True,
            env=env,
        )
        self.output = bytearray()

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def resize(self, rows: int, columns: int) -> None:
        _set_size(self.slave, rows, columns)
        self.process.send_signal(signal.SIGWINCH)

    def read_until(self, needle: bytes, timeout: float = TIMEOUT) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.output and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            self.output.extend(chunk)
        if needle not in self.output:
            raise AssertionError(f"did not observe {needle!r}; output={bytes(self.output)!r}")
        return bytes(self.output)

    def finish(self, timeout: float = FINISH_TIMEOUT) -> tuple[int, bytes, list]:
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if chunk:
                    self.output.extend(chunk)
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
            raise AssertionError(f"PTY child timed out; output={bytes(self.output)!r}")
        # Drain bytes available after exit.
        while True:
            ready, _, _ = select.select([self.master], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            self.output.extend(chunk)
        after = termios.tcgetattr(self.slave)
        return self.process.returncode, bytes(self.output), after

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
        os.close(self.master)
        os.close(self.slave)


class DumbTerminalPTYTests(unittest.TestCase):
    """TERM=dumb: no interactive editor; the $VISUAL/$EDITOR guidance
    (``compose edit`` is the ``profile edit`` alias, which needs an editor
    without the TUI editor)."""

    def _edit_code(self, temp: str, secret_file: Path) -> str:
        return f"""
import sys
from pathlib import Path
from claude_multi.cli.runtime import Runtime
from claude_multi.cli.entry import main
root = Path({str(CATALOG_ROOT)!r})
base = Path({temp!r})
env = {{'HOME': str(base/'home'), 'XDG_CONFIG_HOME': str(base/'config'), 'XDG_STATE_HOME': str(base/'state'), 'TERM': 'dumb', 'CLAUDE_MULTI_SECRET_ENV': {str(secret_file)!r}}}
runtime = Runtime(asset_root=root, environ=env, cwd=base/'project', launch_callback=lambda prepared: 0, served_models_callback=lambda gateway, token: (None, None), health_get=lambda base, path: 200)
raise SystemExit(main(['compose', 'edit', 'balanced'], runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout, interactive=True))
"""

    def test_dumb_terminal_prints_plan_and_editor_command(self) -> None:
        temp = tempfile.mkdtemp(prefix="claude-multi-pty-dumb-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = PTYProcess(
            self._edit_code(temp, _secret_file(temp)),
            extra_env={"TERM": "dumb", "NO_COLOR": "1"},
        )
        self.addCleanup(child.close)
        child.read_until(b"$EDITOR")
        code, output, _ = child.finish()
        self.assertEqual(code, 1, output)
        self.assertIn(b"profile edit needs an editor: set $VISUAL or $EDITOR", output)
        self.assertNotIn(b"\x1b", output)


class QuickConfirmPTYTests(unittest.TestCase):
    """A bare launch on a real PTY: the card on a capable TERM, the line
    confirm on a dumb TERM and under ``--line``.
    The card children build their Runtime with ``_tui_fixture``."""

    def _runtime_code(self, temp: str, secret_file: Path) -> str:
        return f"""
import sys
from pathlib import Path
from claude_multi.cli.runtime import Runtime
from claude_multi.cli.entry import main
root = Path({str(CATALOG_ROOT)!r})
base = Path({temp!r})
env = {{'HOME': str(base/'home'), 'XDG_CONFIG_HOME': str(base/'config'), 'XDG_STATE_HOME': str(base/'state'), 'CLAUDE_MULTI_SECRET_ENV': {str(secret_file)!r}}}
def fake(prepared):
    print('FAKE_LAUNCH=' + prepared.result.session_action.kind, flush=True)
    return 0
runtime = Runtime(asset_root=root, environ=env, cwd=base/'project', launch_callback=fake, doctor_binary_callback=lambda contract: ([], ['fixture binary verified.']), doctor_callback=lambda _runtime: [], served_models_callback=lambda gateway, token: (None, None), health_get=lambda base, path: 200)
raise SystemExit(main([], runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout, interactive=True))
"""

    def _temp_runtime(self, prefix: str) -> tuple[str, Path]:
        temp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        return temp, _secret_file(temp)

    def _card(self, prefix: str, argv=()) -> PTYProcess:
        temp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = PTYProcess(_fixture_card_code(temp, list(argv)), extra_pythonpath=(TESTS_ROOT,))
        self.addCleanup(child.close)
        return child

    def test_line_confirm_one_enter_uses_fake_launch(self) -> None:
        temp, secret_file = self._temp_runtime("claude-multi-pty-runtime-")
        child = PTYProcess(self._runtime_code(temp, secret_file), extra_env={"TERM": "dumb"})
        self.addCleanup(child.close)
        child.read_until(LINE_CONFIRM)
        child.send(b"\n")
        returncode, output, _ = child.finish()
        self.assertEqual(returncode, 0, output)
        # The line body is the card's line form.
        self.assertIn("profile: balanced · follows profile".encode(), output)
        self.assertIn(b"FAKE_LAUNCH=fresh", output)

    def test_card_on_a_capable_terminal_launches_and_restores(self) -> None:
        # A capable TERM gets the card.
        child = self._card("claude-multi-pty-card-")
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"\n")
        returncode, output, after = child.finish()
        self.assertEqual(returncode, 0, output)
        self.assertIn(b"FAKE_LAUNCH=fresh", output)
        self.assertNotIn(LINE_CONFIRM, output)
        _restored(self, child, after)

    def test_card_esc_never_launches(self) -> None:
        child = self._card("claude-multi-pty-card-esc-")
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        returncode, output, after = child.finish()
        self.assertEqual(returncode, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_line_flag_keeps_the_line_confirm(self) -> None:
        child = self._card("claude-multi-pty-card-line-", ["--line"])
        child.read_until(LINE_CONFIRM, FIXTURE_TIMEOUT)
        child.send(b"q\n")
        returncode, output, after = child.finish()
        self.assertEqual(returncode, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(CARD_READY, output)
        _restored(self, child, after)

    def test_card_help_modal_shows_the_guarantee_panel(self) -> None:
        # The 3.0 form of the deleted
        # quick-confirm details-and-guarantee-modal PTY test.
        from claude_multi import cli

        child = self._card("claude-multi-pty-card-help-")
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"?")
        child.read_until("launch card — help".encode())
        # The first help line wraps at 80 columns: its opening words.
        child.read_until(" ".join(cli.QUICK_HELP.splitlines()[0].split()[:6]).encode())
        child.read_until(cli.QUICK_HELP.splitlines()[1].encode())
        # The help wraps and scrolls; at 80x24 Space pages down to the panel.
        child.send(b"    ")
        child.read_until(b"Native workflows (ultracode):")
        mark = len(child.output)
        child.send(b"\x1b")  # closes the Modal: the card is back
        _read_after(child, mark, b"Status  Ready")
        child.send(b"\x1b")
        returncode, output, after = child.finish()
        self.assertEqual(returncode, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)


if __name__ == "__main__":
    unittest.main()


def _bare_launch_code(temp: str, secret_file: Path) -> str:
    return f"""
import sys
from pathlib import Path
from claude_multi.cli.runtime import Runtime
from claude_multi.cli.entry import main
root = Path({str(CATALOG_ROOT)!r})
base = Path({temp!r})
env = {{'HOME': str(base/'home'), 'XDG_CONFIG_HOME': str(base/'config'), 'XDG_STATE_HOME': str(base/'state'), 'TERM': 'dumb', 'CLAUDE_MULTI_SECRET_ENV': {str(secret_file)!r}}}
def fake(prepared):
    print('FAKE_LAUNCH=' + prepared.result.session_action.kind, flush=True)
    return 0
runtime = Runtime(asset_root=root, environ=env, cwd=base/'project', launch_callback=fake, doctor_binary_callback=lambda contract: ([], ['fixture binary verified.']), doctor_callback=lambda _runtime: [], served_models_callback=lambda gateway, token: (None, None), health_get=lambda base, path: 200)
raise SystemExit(main([], runtime=runtime))
"""


def _secret_file(temp: str) -> Path:
    secret_dir = Path(temp) / "secrets"
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    secret_file = secret_dir / "claude.env"
    secret_file.write_text("KIMI_CLAUDE_API_KEY=pty-test-dummy\n")
    secret_file.chmod(0o600)
    return secret_file


class _NoCttyChild:
    """Child in a new session without a controlling terminal, TTY stdio."""

    def __init__(self, code: str, *, extra_env=None):
        self.master, slave = pty.openpty()
        _set_size(slave, 24, 80)
        env = dict(os.environ)
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(SRC_ROOT), "TERM": "dumb"})
        if extra_env:
            env.update(extra_env)
        self.process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=os.setsid,  # new session: no controlling terminal
            close_fds=True,
            env=env,
        )
        os.close(slave)
        self.output = bytearray()

    def read_until(self, needle: bytes, timeout: float = TIMEOUT) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.output and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            self.output.extend(chunk)
        if needle not in self.output:
            raise AssertionError(f"did not observe {needle!r}; output={bytes(self.output)!r}")
        return bytes(self.output)

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def finish(self, timeout: float = FINISH_TIMEOUT) -> tuple[int, bytes]:
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        # Every slave fd is closed, so the child is inside exit, but it
                        # may not be waitable yet (its fds are released before it becomes
                        # a zombie): reap it within the deadline instead of polling once
                        # (a once-flaky reap).
                        try:
                            self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
                        except subprocess.TimeoutExpired:
                            pass
                        break
                    raise
                if chunk:
                    self.output.extend(chunk)
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
            raise AssertionError(f"child timed out; output={bytes(self.output)!r}")
        try:
            while True:
                chunk = os.read(self.master, 65536)
                if not chunk:
                    break
                self.output.extend(chunk)
        except OSError:
            pass
        os.close(self.master)
        return self.process.returncode, bytes(self.output)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)


class _CttyPipesChild:
    """Child with the slave as controlling terminal but piped stdin/stdout.

    Proves /dev/tty is genuinely preferred: interaction must flow over the
    PTY master (the child's controlling terminal), never over the pipes.
    """

    def __init__(self, code: str, *, extra_env=None):
        self.master, slave = pty.openpty()
        _set_size(slave, 24, 80)
        env = dict(os.environ)
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(SRC_ROOT), "TERM": "dumb"})
        if extra_env:
            env.update(extra_env)

        def _attach_ctty() -> None:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        self.process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=_attach_ctty,
            # Preserve a slave through exec: on Darwin closing the last one
            # can hang up the terminal before the child opens /dev/tty.
            pass_fds=(slave,),
            close_fds=True,
            env=env,
        )
        os.close(slave)
        self.output = bytearray()

    def read_until(self, needle: bytes, timeout: float = TIMEOUT) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.output and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    # Transient: slave side fully closed between spawn and the
                    # child opening /dev/tty; data queues on the master later.
                    time.sleep(0.05)
                    continue
                raise
            if not chunk:
                break
            self.output.extend(chunk)
        if needle not in self.output:
            raise AssertionError(f"did not observe {needle!r}; output={bytes(self.output)!r}")
        return bytes(self.output)

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def finish(self, timeout: float = TIMEOUT) -> tuple[int, bytes, bytes]:
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        time.sleep(0.05)
                        continue
                    raise
                if chunk:
                    self.output.extend(chunk)
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
            raise AssertionError(f"child timed out; output={bytes(self.output)!r}")
        while True:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            self.output.extend(chunk)
        os.close(self.master)
        pipe_out = self.process.stdout.read() if self.process.stdout else b""
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()
        return self.process.returncode, bytes(self.output), pipe_out

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
        try:
            os.close(self.master)
        except OSError:
            pass
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()


class SessionsAndTransitionPTYTests(unittest.TestCase):
    """The `sessions transition` alias (the relaunch executor) on a real PTY.

    The tty `sessions list` / bare `-r` tests are in
    :class:`SessionsScreenPTYTests` (the curses sessions screen on a
    capable terminal, with `--line` twins that keep the line flows).
    """

    def _temp(self, prefix: str) -> tuple[str, Path]:
        temp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        return temp, _secret_file(temp)

    def _sessions_code(self, temp: str, secret_file: Path, argv: list[str]) -> str:
        return f"""
import copy, sys
from pathlib import Path
from claude_multi import sessions, scope as scope_mod
from claude_multi.cli.types import LaunchTarget
from claude_multi.cli.runtime import Runtime
from claude_multi.cli.entry import main
from claude_multi.cli.session_facts import _native_project_slug
root = Path({str(CATALOG_ROOT)!r})
base = Path({temp!r})
env = {{'HOME': str(base/'home'), 'XDG_CONFIG_HOME': str(base/'config'), 'XDG_STATE_HOME': str(base/'state'), 'CLAUDE_MULTI_SECRET_ENV': {str(secret_file)!r}}}
def fake(prepared):
    print('FAKE_LAUNCH=' + prepared.result.session_action.kind, flush=True)
    return 0
runtime = Runtime(asset_root=root, environ=env, cwd=base/'project', launch_callback=fake, doctor_binary_callback=lambda contract: ([], ['fixture binary verified.']), doctor_callback=lambda _runtime: [], served_models_callback=lambda gateway, token: (None, None), health_get=lambda base, path: 200)
(base/'project').mkdir(parents=True, exist_ok=True)  # records always have an existing cwd
MID = '11111111-1111-4111-8111-111111111111'
sessions.SessionStore.new_id = lambda _store: MID
runtime.profiles.install_seeds()
balanced = runtime.profiles.load('balanced')
prepared = runtime.prepare(LaunchTarget('profile', balanced, 'balanced', True, 'Profile balanced'), action='fresh', passthrough=[])
record = dict(prepared.record, mutation_token=sessions.new_mutation_token())
sessions.ensure_state_v4(runtime.session_store.root)
scope_mod.swap_scope(runtime.session_store.root, MID, prepared.result.scope_plan)
runtime.session_store.save(record)
transcript = base/'home'/'.claude'/'projects'/_native_project_slug(runtime.cwd)/(MID + '.jsonl')
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.touch()
shifted = copy.deepcopy(balanced)
shifted['name'] = 'shifted'
shifted['lead'] = {{'model': 'sol', 'effort': 'xhigh'}}
runtime.profiles.save(shifted)
raise SystemExit(main({argv!r}, runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout, interactive=True))
"""

    def test_transition_confirm_relaunches(self) -> None:
        temp, secret_file = self._temp("claude-multi-pty-transition-")
        child = PTYProcess(
            self._sessions_code(
                temp,
                secret_file,
                [
                    "sessions",
                    "transition",
                    "11111111-1111-4111-8111-111111111111",
                    "--composition",
                    "shifted",
                ],
            ),
            extra_env={"TERM": "dumb"},
        )
        self.addCleanup(child.close)
        child.read_until(b"Has the target process exited? [y/N] ")
        child.send(b"y\n")
        code, output, _ = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"FAKE_LAUNCH=resume", output)

    def test_transition_curses_modal_shows_the_diff_and_relaunches(self) -> None:
        # _transition_confirm keeps its curses modal (TransitionTuiScreenTests
        # stays); the preview is the lineup diff.
        temp, secret_file = self._temp("claude-multi-pty-transition-curses-")
        child = PTYProcess(
            self._sessions_code(
                temp,
                secret_file,
                [
                    "sessions",
                    "transition",
                    "11111111-1111-4111-8111-111111111111",
                    "--composition",
                    "shifted",
                ],
            )
        )
        self.addCleanup(child.close)
        child.read_until(b"semantic diff")
        child.read_until(b"relaunch session 11111111 with profile shifted")
        child.send(b"\n")
        child.read_until(b"EXITED (not merely idle)")
        child.send(b"\n")
        code, output, _ = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"FAKE_LAUNCH=resume", output)

    def test_transition_no_never_launches(self) -> None:
        temp, secret_file = self._temp("claude-multi-pty-transition-n-")
        child = PTYProcess(
            self._sessions_code(
                temp,
                secret_file,
                [
                    "sessions",
                    "transition",
                    "11111111-1111-4111-8111-111111111111",
                    "--composition",
                    "shifted",
                ],
            ),
            extra_env={"TERM": "dumb"},
        )
        self.addCleanup(child.close)
        child.read_until(b"Has the target process exited? [y/N] ")
        child.send(b"\n")
        code, output, _ = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)


class BareLaunchAutoDetectionPTYTests(unittest.TestCase):
    """Bare launch auto-detection with no injected interactive flag."""

    def test_no_controlling_terminal_still_reaches_the_line_confirm(self) -> None:
        temp = tempfile.mkdtemp(prefix="claude-multi-pty-bare-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = _NoCttyChild(_bare_launch_code(temp, _secret_file(temp)))
        self.addCleanup(child.close)
        # /dev/tty cannot be opened here (no controlling terminal), but both
        # standard streams are TTYs, so the fallback must engage.
        child.read_until(LINE_CONFIRM)
        child.send(b"q\n")  # cancel at the line confirm; never launches
        returncode, output = child.finish()
        self.assertEqual(returncode, 0, output)
        self.assertNotIn(b"FAKE_LAUNCH", output)

    def test_controlling_terminal_slave_stays_open_across_exec(self) -> None:
        # Darwin can hang up the controlling terminal when the last slave fd
        # closes. Keep a slave open before product code first opens /dev/tty.
        child = _CttyPipesChild(
            "import os\n"
            "fds = [int(name) for name in os.listdir('/dev/fd') if name.isdigit()]\n"
            "print(any(fd > 2 and os.isatty(fd) for fd in fds))\n")
        self.addCleanup(child.close)
        output, _ = child.process.communicate(timeout=TIMEOUT)
        self.assertEqual(child.process.returncode, 0, output)
        self.assertEqual(output.strip(), b"True", "the harness closed every slave descriptor before exec")

    def test_controlling_terminal_pipes_stdio_still_uses_dev_tty(self) -> None:
        temp = tempfile.mkdtemp(prefix="claude-multi-pty-ctty-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = _CttyPipesChild(_bare_launch_code(temp, _secret_file(temp)))
        self.addCleanup(child.close)
        # stdin/stdout are pipes, but /dev/tty exists: the line confirm must
        # render on the controlling terminal (PTY master), not the pipes.
        child.read_until(LINE_CONFIRM)
        child.send(b"q\n")  # cancel at the line confirm; never launches
        returncode, tty_output, pipe_output = child.finish()
        self.assertEqual(returncode, 0, tty_output)
        self.assertIn(LINE_CONFIRM, tty_output)
        self.assertNotIn(LINE_CONFIRM, pipe_output)
        self.assertNotIn(b"FAKE_LAUNCH", tty_output + pipe_output)

    def test_capable_terminal_card_routes_to_controlling_terminal(self) -> None:
        # A capable TERM gets the card, on
        # /dev/tty (the PTY master), never on the pipes.
        from claude_multi import catalog

        temp = tempfile.mkdtemp(prefix="claude-multi-pty-ctty-curses-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = _CttyPipesChild(
            _fixture_card_code(temp, [], streams=False),
            extra_env={
                "TERM": "xterm-256color",
                "PYTHONPATH": os.pathsep.join([str(SRC_ROOT), str(TESTS_ROOT)]),
            },
        )
        self.addCleanup(child.close)
        profile_line = f"profile: {_expected_default(self)}".encode()
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        returncode, tty_output, pipe_output = child.finish(FIXTURE_TIMEOUT)
        self.assertEqual(returncode, 0, tty_output)
        self.assertIn(profile_line, tty_output)
        self.assertNotIn(profile_line, pipe_output)
        self.assertNotIn(b"FAKE_LAUNCH", tty_output + pipe_output)

    def test_genuine_pipe_fails_with_explicit_profile_error(self) -> None:
        temp = tempfile.mkdtemp(prefix="claude-multi-pty-pipe-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        env = dict(os.environ)
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(SRC_ROOT), "TERM": "dumb"})
        process = subprocess.Popen(
            [sys.executable, "-c", _bare_launch_code(temp, _secret_file(temp))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,  # hermetic: no controlling terminal at all
            env=env,
        )
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))
        output, _ = process.communicate(timeout=TIMEOUT * 2)
        self.assertEqual(process.returncode, 2, output)
        self.assertIn(b"noninteractive launch requires --profile NAME", output)
        self.assertNotIn(LINE_CONFIRM, output)
        self.assertNotIn(b"FAKE_LAUNCH", output)


# ---------------------------------------------------------------------------
# Escdelay, ^S flow control and the
# curses-less line fallback on a real PTY.  The probe apps need no Runtime;
# CursesAbsentPTYTests builds its Runtime inline as QuickConfirmPTYTests does
# (these children do not use tests/_tui_fixture.py).


def _probe_code(*, exit_on: str) -> str:
    """A curses probe app: draw READY, read one key, report after teardown."""

    return f"""
import curses
import sys
from claude_multi import tui
seen = {{}}
def app(win):
    seen['escdelay'] = curses.get_escdelay()
    tui.safe_add(win, 1, 2, 'PROBE-READY')
    win.refresh()
    while True:
        key = tui.read_key(win)
        if {exit_on!r} == 'any' or (key.kind == 'ctrl' and key.ch == {exit_on!r}):
            return key
result = tui.run_curses_on_streams(app, sys.stdin, sys.stdout)
print('ESCDELAY=' + str(seen['escdelay']), flush=True)
print('KEY=' + result.kind + ':' + result.ch, flush=True)
if result.kind == 'ctrl' and result.ch == 's':
    print('GOT-CTRL-S', flush=True)
"""


class EscDelayPTYTests(unittest.TestCase):
    """A lone Esc is delivered promptly."""

    def test_lone_escape_is_prompt(self) -> None:
        from unittest import mock

        with mock.patch.dict(os.environ):
            os.environ.pop("ESCDELAY", None)  # the child inherits no ESCDELAY
            child = PTYProcess(_probe_code(exit_on="any"))
        self.addCleanup(child.close)
        child.read_until(b"PROBE-READY")
        started = time.monotonic()
        child.send(b"\x1b")
        child.read_until(b"KEY=esc:")
        elapsed = time.monotonic() - started
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"ESCDELAY=25", output)
        # A loose sanity bound; ncurses' default delay is 1000 ms.
        self.assertLess(elapsed, 0.9, output)
        mask = termios.ECHO | termios.ICANON
        self.assertEqual(child.before[3] & mask, after[3] & mask)

    def test_escdelay_env_wins(self) -> None:
        child = PTYProcess(_probe_code(exit_on="any"), extra_env={"ESCDELAY": "600"})
        self.addCleanup(child.close)
        child.read_until(b"PROBE-READY")
        child.send(b"\x1b")
        code, output, _after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"ESCDELAY=600", output)
        self.assertIn(b"KEY=esc:", output)


class IxonRestorePTYTests(unittest.TestCase):
    """^S reaches the screen; IXON restored after."""

    def test_ctrl_s_reaches_the_screen_and_flow_control_is_restored(self) -> None:
        child = PTYProcess(_probe_code(exit_on="s"))
        self.addCleanup(child.close)
        self.assertTrue(
            child.before[0] & termios.IXON,
            "precondition: a fresh PTY has flow control (IXON) on",
        )
        child.read_until(b"PROBE-READY")
        child.send(b"\x13")  # ^S: frozen output if IXON were still on
        child.read_until(b"GOT-CTRL-S")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(child.before[0] & termios.IXON, after[0] & termios.IXON)
        mask = termios.ECHO | termios.ICANON
        self.assertEqual(child.before[3] & mask, after[3] & mask)


class CursesAbsentPTYTests(unittest.TestCase):
    """A Python without curses on a capable terminal still
    reaches the line confirm (the line card)."""

    def _code(self, temp: str, secret_file: Path) -> str:
        return f"""
import sys
sys.modules.update(curses=None, _curses=None)
from pathlib import Path
from claude_multi import tui
assert tui.curses_available() is False
from claude_multi.cli.runtime import Runtime
from claude_multi.cli.entry import main
root = Path({str(CATALOG_ROOT)!r})
base = Path({temp!r})
env = {{'HOME': str(base/'home'), 'XDG_CONFIG_HOME': str(base/'config'), 'XDG_STATE_HOME': str(base/'state'), 'CLAUDE_MULTI_SECRET_ENV': {str(secret_file)!r}}}
def fake(prepared):
    print('FAKE_LAUNCH=' + prepared.result.session_action.kind, flush=True)
    return 0
runtime = Runtime(asset_root=root, environ=env, cwd=base/'project', launch_callback=fake, doctor_binary_callback=lambda contract: ([], ['fixture binary verified.']), doctor_callback=lambda _runtime: [], served_models_callback=lambda gateway, token: (None, None), health_get=lambda base, path: 200)
raise SystemExit(main([], runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout, interactive=True))
"""

    def test_capable_terminal_without_curses_uses_the_line_card(self) -> None:
        temp = tempfile.mkdtemp(prefix="claude-multi-pty-nocurses-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = PTYProcess(self._code(temp, _secret_file(temp)))
        self.addCleanup(child.close)
        child.read_until(LINE_CONFIRM)
        child.send(b"q\n")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(b"Traceback", output)
        # The line card, under the unchanged line footer.
        self.assertIn("claude-multi — profile: balanced · follows profile · Fresh".encode(), output)
        mask = termios.ECHO | termios.ICANON
        self.assertEqual(child.before[3] & mask, after[3] & mask)
        self.assertEqual(child.before[0] & termios.IXON, after[0] & termios.IXON)


# ---------------------------------------------------------------------------
# The 3.0 profile editor on a real PTY.  Children
# build their Runtime with ``_tui_fixture.fixture_runtime`` and run ``main``
# inside ``hermetic(runtime)``; nothing reads the
# journal, ``/proc`` or the daemon socket directory.

TESTS_ROOT = REPO_ROOT / "tests"
# curses turns keypad transmit mode on (smkx), so arrows arrive as SS3 sequences.
DOWN_KEY = b"\x1bOB"
RIGHT_KEY = b"\x1bOC"
EDITOR_READY = "edit profile — ".encode()
FIXTURE_TIMEOUT = 30.0


def _fixture_editor_code(temp: str, name: str, *, setup: str = "", after: str = "", extra: dict | None = None) -> str:
    return f"""
import sys
from pathlib import Path
import _tui_fixture as fx
from claude_multi import catalog, lineup as lineup_mod, profile, scope, sessions
from claude_multi.cli import entry
runtime = fx.fixture_runtime(Path({temp!r}), environ_extra={dict(extra or {})!r})
{setup}
with fx.hermetic(runtime):
    code = entry.main(['profile', 'edit', {name!r}], runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout, interactive=True)
print('EXIT=' + str(code), flush=True)
{after}
raise SystemExit(code)
"""


def fx_ids() -> tuple[str, str]:
    import _tui_fixture as fx

    return fx.FIXED_ID, fx.OTHER_ID


def _restored(case: unittest.TestCase, child: PTYProcess, after: list) -> None:
    mask = termios.ECHO | termios.ICANON
    case.assertEqual(child.before[3] & mask, after[3] & mask)
    case.assertEqual(child.before[0] & termios.IXON, after[0] & termios.IXON)


class ProfileEditorPTYTests(unittest.TestCase):
    """The 3.0 twin of the deleted 2.x ``CursesPTYTests``: ``profile edit <DEFAULT_SEED>``."""

    def child(self, *, setup: str = "", name: str | None = None, extra: dict | None = None, **size) -> PTYProcess:
        from claude_multi import catalog

        temp = tempfile.mkdtemp(prefix="claude-multi-pty-editor-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = PTYProcess(
            _fixture_editor_code(temp, name or catalog.DEFAULT_SEED, setup=setup, extra=extra),
            extra_pythonpath=(TESTS_ROOT,),
            **size,
        )
        self.addCleanup(child.close)
        return child

    def assertCancelled(self, child: PTYProcess) -> bytes:
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"Nothing was saved.", output)
        self.assertNotIn(b"Traceback", output)
        _restored(self, child, after)
        return output

    def test_cancel_restores_the_terminal(self) -> None:
        child = self.child()
        child.read_until(EDITOR_READY, FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        self.assertCancelled(child)

    def test_resize_to_small_is_safe(self) -> None:
        child = self.child()
        child.read_until(EDITOR_READY, FIXTURE_TIMEOUT)
        child.resize(8, 35)
        child.send(b"j")  # wake get_wch; the next draw sees the resized PTY
        child.read_until(b"Terminal too small")
        child.send(b"\x1b")
        self.assertCancelled(child)

    def test_too_small_start_cancels(self) -> None:
        child = self.child(rows=8, columns=35)
        child.read_until(b"Terminal too small", FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        self.assertCancelled(child)

    def test_sigint_cancels_and_restores(self) -> None:
        child = self.child()
        child.read_until(EDITOR_READY, FIXTURE_TIMEOUT)
        child.process.send_signal(signal.SIGINT)
        self.assertCancelled(child)

    def test_blocked_check_row(self) -> None:
        setup = (
            "doc = runtime.profiles.load(catalog.DEFAULT_SEED)\n"
            "doc.pop('seed')\n"
            "doc['name'] = 'blocked'\n"
            "doc['lead'] = {'model': fx.needs_choice_key(runtime), 'effort': profile.ULTRACODE}\n"
            "runtime.profiles.save(doc)\n"
        )
        child = self.child(setup=setup, name="blocked")
        child.read_until(b"error(s)", FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        self.assertCancelled(child)

    def test_ctrl_g_json_not_applied_then_discard(self) -> None:
        editor = f"{sys.executable} -c \"import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('{{ not json')\""
        child = self.child(extra={"VISUAL": editor})
        child.read_until(EDITOR_READY, FIXTURE_TIMEOUT)
        child.send(DOWN_KEY + DOWN_KEY + b"U")  # unbind the first agent: the editor is dirty
        child.read_until("unsaved changes ●".encode())
        child.send(b"\x07")  # ^G: the raw JSON in $VISUAL
        child.read_until(b"JSON not applied")
        child.send(b"\x1b")
        child.read_until(b"Discard")
        child.send(b"\n")
        self.assertCancelled(child)


class EditSavePropagatePTYTests(unittest.TestCase):
    """Edit a writer's effort, ^S, then A applies it live to the unknown follower."""

    def test_save_propagates_live_to_the_running_follower(self) -> None:
        from claude_multi import catalog, profile

        temp = tempfile.mkdtemp(prefix="claude-multi-pty-propagate-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        writer = profile.WRITER_IDS[0]
        setup = (
            f"writer = {writer!r}\n"
            "fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title='running one')\n"
            "fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID, ended=True)\n"
            "store = runtime.session_store\n"
            "agent = scope.scope_dir(store.root, fx.FIXED_ID) / '.claude' / 'agents' / (writer + '.md')\n"
            "before_bytes = agent.read_bytes()\n"
            "before_gen = store.load(fx.FIXED_ID)['lineup_generation']\n"
            "before_effort = runtime.profiles.load(catalog.DEFAULT_SEED)['agents'][writer]['effort']\n"
            "efforts = profile.declared_efforts(runtime.lineup_catalog().lines[runtime.profiles.load(catalog.DEFAULT_SEED)['agents'][writer]['model']])\n"
            "assert efforts.index(before_effort) < len(efforts) - 1, 'precondition: -> reaches another effort'\n"
            "import io\n"
            "real_on_saved = lineup_mod.on_saved\n"
            "reports = []\n"
            "def tee(runtime_, names, *, apply_live, out, now=None):\n"
            "    buffer = io.StringIO()\n"
            "    written = real_on_saved(runtime_, names, apply_live=apply_live, out=buffer)\n"
            "    out.write(buffer.getvalue())\n"
            "    reports.append(buffer.getvalue())\n"
            "    return written\n"
            "lineup_mod.on_saved = tee\n"
        )
        after = (
            "print('REPORT=' + repr(reports), flush=True)\n"
            "print('GEN=%d->%d' % (before_gen, store.load(fx.FIXED_ID)['lineup_generation']), flush=True)\n"
            "print('AGENT_CHANGED=' + str(agent.read_bytes() != before_bytes), flush=True)\n"
            "print('EFFORT=' + before_effort + '->' + runtime.profiles.load(catalog.DEFAULT_SEED)['agents'][writer]['effort'], flush=True)\n"
        )
        child = PTYProcess(
            _fixture_editor_code(temp, catalog.DEFAULT_SEED, setup=setup, after=after),
            extra_pythonpath=(TESTS_ROOT,),
        )
        self.addCleanup(child.close)
        child.read_until(EDITOR_READY, FIXTURE_TIMEOUT)
        downs = 2 + catalog.AGENT_ROLE_IDS.index(writer)  # General → Lead → the agents
        child.send(DOWN_KEY * downs + b"\n")
        child.read_until(b"choose model")
        child.send(RIGHT_KEY + b"\n")
        child.read_until("unsaved changes ●".encode())
        child.send(b"\x13")  # ^S (flow control is off inside the TUI)
        child.read_until(b"2 sessions follow this profile")
        child.send(b"A")
        child.read_until(b"applied live (lineup gen")  # the report screen shows on_saved's lines
        child.send(b"\x1b")  # the report
        child.read_until(f"saved {catalog.DEFAULT_SEED}".encode())
        child.send(b"\x1b")  # the (clean) editor
        code, output, after_attrs = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(f"Saved profile {catalog.DEFAULT_SEED!r}.".encode(), output)
        self.assertNotIn(b"Traceback", output)
        report = output.split(b"REPORT=", 1)[1].split(b"\r", 1)[0].decode()
        self.assertIn(f"{fx_ids()[0][:8]} project: applied live (lineup gen", report)
        self.assertIn(f"{fx_ids()[1][:8]} project: applies at the next resume", report)
        gen = output.split(b"GEN=", 1)[1].split(b"\r", 1)[0].split(b"\n", 1)[0].decode()
        before, after_gen = (int(v) for v in gen.split("->"))
        self.assertEqual(after_gen, before + 1, output)
        self.assertIn(b"AGENT_CHANGED=True", output)
        effort = output.split(b"EFFORT=", 1)[1].split(b"\r", 1)[0].split(b"\n", 1)[0].decode()
        self.assertNotEqual(*effort.split("->"))
        _restored(self, child, after_attrs)


# ---------------------------------------------------------------------------
# The launch card and the screens it opens on a real PTY.  Children build their Runtime with ``_tui_fixture.fixture_runtime``
# and run ``main`` inside ``hermetic(runtime)``; the launch callback prints
# ``FAKE_LAUNCH=<kind>`` (and the prepared target) after curses teardown.

CARD_READY = b"Status  Ready"
DIRECT_READY = "direct session — lead only, no profile".encode()
END_KEY = b"\x1bOF"


def _fixture_card_code(
    temp: str,
    argv: list,
    *,
    setup: str = "",
    after: str = "",
    asset_root: str | None = None,
    streams: bool = True,
) -> str:
    """A bare-launch child over the fixture runtime (``streams=False``: main opens /dev/tty)."""

    extra = f", asset_root=Path({asset_root!r})" if asset_root else ""
    io_args = "input_stream=sys.stdin, output_stream=sys.stdout, interactive=True" if streams else ""
    return f"""
import json
import sys
from pathlib import Path
import _tui_fixture as fx
from claude_multi import catalog, profile, settings
from claude_multi.cli import entry, selection, session_facts
def fake(prepared):
    print('FAKE_LAUNCH=' + prepared.result.session_action.kind, flush=True)
    print('TARGET=' + str(prepared.target.profile), flush=True)
    print('RECORD=' + json.dumps({{'profile': prepared.record['profile'], 'agents': prepared.record['applied']['agents']}}), flush=True)
    return 0
runtime = fx.fixture_runtime(Path({temp!r}), launch_callback=fake{extra})
{setup}
with fx.hermetic(runtime):
    code = entry.main({argv!r}, runtime=runtime{', ' + io_args if io_args else ''})
print('EXIT=' + str(code), flush=True)
{after}
raise SystemExit(code)
"""


def _expected_default(case: unittest.TestCase) -> str:
    """The default profile a bare launch in a fresh fixture child resolves
    (derived on a parent-side fixture runtime of the same shape)."""

    import _tui_fixture as fx
    from claude_multi.setup import defaults

    root = tempfile.mkdtemp(prefix="claude-multi-pty-default-")
    case.addCleanup(__import__("shutil").rmtree, root, True)
    runtime = fx.fixture_runtime(Path(root))
    with fx.hermetic(runtime):
        return defaults.resolve_default(runtime).name


def _read_after(child: PTYProcess, offset: int, needle: bytes, timeout: float = FIXTURE_TIMEOUT) -> None:
    """Read until ``needle`` appears in the output past ``offset`` (a redraw after a key)."""

    deadline = time.monotonic() + timeout
    while needle not in child.output[offset:] and time.monotonic() < deadline:
        ready, _, _ = select.select([child.master], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        try:
            chunk = os.read(child.master, 65536)
        except OSError:
            break
        if not chunk:
            break
        child.output.extend(chunk)
    if needle not in child.output[offset:]:
        raise AssertionError(f"did not observe {needle!r} after the key; output={bytes(child.output)!r}")


def _line_of(output: bytes, prefix: bytes) -> str:
    return output.split(prefix, 1)[1].split(b"\r", 1)[0].split(b"\n", 1)[0].decode()


class _CardChildCase(unittest.TestCase):
    def child(self, prefix: str, argv=(), **kwargs) -> PTYProcess:
        temp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        self.temp = temp
        child = PTYProcess(_fixture_card_code(temp, list(argv), **kwargs), extra_pythonpath=(TESTS_ROOT,))
        self.addCleanup(child.close)
        return child

    def fixture_runtime(self, **kwargs):
        """A parent-side fixture runtime, only to derive keys and positions."""

        import _tui_fixture as fx

        root = tempfile.mkdtemp(prefix="claude-multi-pty-derive-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        return fx.fixture_runtime(Path(root), **kwargs)


class CardLaunchPTYTests(_CardChildCase):
    def test_enter_launches_the_default_seed(self) -> None:
        from claude_multi import catalog

        child = self.child("claude-multi-pty-card-launch-")
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        self.assertIn(b"claude-multi", child.output)
        expected = _expected_default(self)
        self.assertIn(f"profile: {expected}".encode(), child.output)
        child.send(b"\n")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"FAKE_LAUNCH=fresh", output)
        self.assertEqual(_line_of(output, b"TARGET="), expected)
        self.assertNotIn(b"Traceback", output)
        _restored(self, child, after)


class DirectLaunchPTYTests(_CardChildCase):
    def test_d_then_enter_launches_an_ad_hoc_direct_session(self) -> None:
        # Every OAuth pool counts as signed in, so the preselected row is unmarked.
        setup = (
            "pools = {p['transport']['pool'] for p in runtime.lineup_catalog().providers.values() "
            "if p['transport']['kind'] == 'oauth-pool'}\n"
            "import claude_multi.cli.gateway_facts\n"
            "claude_multi.cli.gateway_facts._oauth_credential_records = lambda _rt: {p: 1 for p in pools}\n"
        )
        child = self.child("claude-multi-pty-direct-", setup=setup)
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"d")
        child.read_until(DIRECT_READY)
        child.send(b"\n")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"FAKE_LAUNCH=fresh", output)
        self.assertEqual(json.loads(_line_of(output, b"RECORD=")), {"profile": None, "agents": {}})
        _restored(self, child, after)


class ModelsAdmitPTYTests(_CardChildCase):
    def test_m_then_enter_admits_the_new_line(self) -> None:
        from claude_multi import catalog as catalog_mod
        from test_screens_catalog import _fixture_copy, new_line_key

        key = new_line_key(catalog_mod.load_catalog(CATALOG_ROOT))
        asset = _fixture_copy(self, new=(key,))
        after = "print('ADMITTED=' + json.dumps(runtime.settings_store.load().get('admitted_lines')), flush=True)\n"
        child = self.child("claude-multi-pty-models-", asset_root=str(asset), after=after)
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"M")
        child.send(DOWN_KEY * 40)  # the New rows are last; the cursor stops there
        child.read_until(b"admit badge")
        child.send(b"\n")
        child.read_until(f"Admit {key}?".encode())
        # Metadata admission defaults to Cancel; choose Admit explicitly.
        child.send(b"\x1bOD\n")
        child.read_until(f"admitted {key}".encode())
        child.send(b"\x1b")  # Models → the card
        mark = len(child.output)
        _read_after(child, mark, CARD_READY)
        child.send(b"\x1b")
        code, output, after_attrs = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(_line_of(output, b"ADMITTED=")), [key])
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after_attrs)


class ProvidersTogglePTYTests(_CardChildCase):
    def test_g_then_space_space_toggles_and_never_runs_journalctl(self) -> None:
        from claude_multi import cli, tui

        runtime = self.fixture_runtime()
        screen = cli._ProvidersScreen(runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
        index = next(
            (i for i, row in enumerate(screen.rows) if row.id in {e["provider"] for e in runtime.lineup_catalog().lines.values()}),
            None,
        )
        self.assertIsNotNone(index, "a provider with at least one line")
        provider = screen.rows[index].id
        steps = index - screen.selected
        stub_dir = Path(tempfile.mkdtemp(prefix="claude-multi-pty-journal-stub-"))
        self.addCleanup(__import__("shutil").rmtree, stub_dir, True)
        marker = stub_dir / "marker"
        stub = stub_dir / "journalctl"
        stub.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
        stub.chmod(0o755)
        after = (
            "doc = json.loads(settings.settings_path(runtime.environ).read_text())\n"
            f"print('ENABLED=' + json.dumps(doc['providers'][{provider!r}]['enabled']), flush=True)\n"
        )
        temp = tempfile.mkdtemp(prefix="claude-multi-pty-providers-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = PTYProcess(
            _fixture_card_code(temp, [], after=after),
            extra_pythonpath=(TESTS_ROOT,),
            extra_env={"PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        )
        self.addCleanup(child.close)
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"G")
        child.read_until(b"local status")
        child.send((DOWN_KEY if steps >= 0 else b"\x1bOA") * abs(steps))
        child.send(b" ")
        child.read_until("disabled — applies at the next launch".encode())
        child.send(b" ")
        child.read_until("enabled — applies at the next launch".encode())
        child.send(b"\x1b")
        mark = len(child.output)
        _read_after(child, mark, CARD_READY)
        child.send(b"\x1b")
        code, output, after_attrs = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(_line_of(output, b"ENABLED="), "true")
        self.assertFalse(marker.exists(), "cf[4]: the journal is never read in a fixture child")
        _restored(self, child, after_attrs)


class SettingsNextLaunchPTYTests(_CardChildCase):
    def test_o_edits_compaction_and_the_card_shows_it(self) -> None:
        import io as io_mod

        from claude_multi import cli, settings, tui

        runtime = self.fixture_runtime()
        screen = cli._SettingsScreen(runtime, tui.MONO_PALETTE, tty_in=io_mod.StringIO(), tty_out=io_mod.StringIO())
        selectable = [entry[1].key for entry in screen.entries if screen._selectable(entry)]
        steps = selectable.index(settings.COMPACTION_PERCENT_KEY)
        value = settings.COMPACTION_PERCENT_DEFAULT - 5
        after = (
            "doc = json.loads(settings.settings_path(runtime.environ).read_text())\n"
            "print('PERCENT=' + str(doc[settings.COMPACTION_PERCENT_KEY]), flush=True)\n"
        )
        child = self.child("claude-multi-pty-settings-", after=after)
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"O")
        child.read_until(b"auto-compact at")
        child.send(b"j" * steps + b"\n")
        child.read_until(b"current ")
        child.send(str(value).encode() + b"\n")
        child.send(b"\n")
        child.read_until("applies at the next launch or resume".encode())
        mark = len(child.output)
        child.send(b"\x1b")  # Settings → the card
        _read_after(child, mark, f"({value} %)".encode())
        child.send(b"\x1b")
        code, output, after_attrs = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(_line_of(output, b"PERCENT="), str(value))
        _restored(self, child, after_attrs)


# The sessions screen and the lineup dialog on a real PTY.  Children build their Runtime with
# ``_tui_fixture.fixture_runtime``, seed one v4 record following the default
# seed (with its metadata-only transcript) and run ``main`` inside
# ``hermetic(runtime)``.

_SESSIONS_SETUP = """
record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
transcript = (Path(runtime.environ['HOME']) / '.claude' / 'projects'
              / session_facts._native_project_slug(record['cwd']) / (record['runtime_session_id'] + '.jsonl'))
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.touch()
before = fx.canonical(runtime.session_store.load(fx.FIXED_ID))
print('GEN0=' + str(record['lineup_generation']), flush=True)
"""
_SESSIONS_AFTER = """
after = runtime.session_store.load(fx.FIXED_ID)
print('GEN=' + str(after['lineup_generation']), flush=True)
print('PENDING=' + str('pending' in after), flush=True)
print('UNCHANGED=' + str(fx.canonical(after) == before), flush=True)
"""
SESSIONS_READY = "sessions — project (cwd) · 1 total".encode()
RIGHT_KEY = b"\x1bOC"  # keypad (application) mode, as END_KEY


class _SessionsChildCase(_CardChildCase):
    def sessions_child(self, prefix: str, argv, *, setup: str = "") -> PTYProcess:
        return self.child(prefix, argv, setup=_SESSIONS_SETUP + setup, after=_SESSIONS_AFTER)


class SessionsScreenPTYTests(_SessionsChildCase):
    """A capable TERM gets the curses sessions screen;
    ``--line`` keeps the line flows."""

    def test_sessions_screen_esc_cancels_and_restores_terminal(self) -> None:
        child = self.sessions_child("claude-multi-pty-sessions-", ["-r"])
        child.read_until(SESSIONS_READY, FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"EXIT=0", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(b"Traceback", output)
        _restored(self, child, after)

    def test_sessions_list_on_a_terminal_prints_the_listing(self) -> None:
        child = self.sessions_child("claude-multi-pty-sessions-list-", ["sessions", "list"])
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"11111111", output)
        self.assertNotIn(SESSIONS_READY, output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertIn(b"UNCHANGED=True", output)
        _restored(self, child, after)

    def test_bare_resume_screen_launches(self) -> None:
        child = self.sessions_child("claude-multi-pty-bare-r-", ["-r"])
        child.read_until(SESSIONS_READY, FIXTURE_TIMEOUT)
        child.send(b"\r")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"FAKE_LAUNCH=resume", output)
        _restored(self, child, after)

    def test_line_sessions_list_prints_the_listing_and_restores_terminal(self) -> None:
        child = self.sessions_child("claude-multi-pty-line-sessions-", ["--line", "sessions", "list"])
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"11111111", output)
        self.assertNotIn(b"resume which session", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_line_sessions_list_never_prompts_or_launches(self) -> None:
        child = self.sessions_child("claude-multi-pty-line-sessions-r-", ["--line", "sessions", "list"])
        child.send(b"1\n")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"EXIT=0", output)
        self.assertNotIn(b"resume which session", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_line_bare_resume_keeps_the_line_chooser_and_launches(self) -> None:
        child = self.sessions_child("claude-multi-pty-line-bare-r-", ["--line", "-r"])
        child.read_until(b"resume which session (number, Enter cancels): ", FIXTURE_TIMEOUT)
        child.send(b"1\n")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"FAKE_LAUNCH=resume", output)
        _restored(self, child, after)


def _steps_to(target_expr: str) -> str:
    """Child setup: print how many → reach ``target_expr`` from the dialog's initial profile
    (the session's own).

    Connected profiles lead the dialog's order; these journeys step by
    recency, so nothing counts as connected in the child."""

    return f"""
type(runtime).profile_readiness = lambda self, names, observations=None: {{}}
order = selection.profile_pick_order(runtime)
initial = record['profile'] if record['profile'] in order else order[0]
target = {target_expr}
print('STEPS=' + str((order.index(target) - order.index(initial)) % len(order)), flush=True)
"""


_LIVE_SEED = """(lambda default: next(
    n for n in sorted(runtime.catalog.seed_profiles)
    if n != catalog.DEFAULT_SEED
    and (lambda other: other is not None and other.relaunch_fields() == default.relaunch_fields()
         and other.lead.binding.key == default.lead.binding.key)(
        profile.evaluate(runtime.profiles.load(n), runtime.lineup_catalog(), bindings=runtime.bindings.bindings(),
                         effective=runtime.current_effective()).lineup)))(
    profile.evaluate(runtime.profiles.load(catalog.DEFAULT_SEED), runtime.lineup_catalog(),
                     bindings=runtime.bindings.bindings(), effective=runtime.current_effective()).lineup)"""
_RELAUNCH_SEED = """next(n for n in sorted(runtime.catalog.seed_profiles)
    if runtime.profiles.load(n).get('native_agents') != runtime.profiles.load(catalog.DEFAULT_SEED)['native_agents'])"""


class SessionsLineupPTYTests(_SessionsChildCase):
    """Bare ``-r``; ``T``; the derived LIVE / RELAUNCH seed; Enter."""

    def _to_target(self, child: PTYProcess) -> None:
        child.read_until(SESSIONS_READY, FIXTURE_TIMEOUT)
        steps = int(_line_of(bytes(child.output), b"STEPS="))
        offset = len(child.output)
        child.send(b"T")
        _read_after(child, offset, b"change lineup")
        _read_after(child, offset, b"mode")
        for _ in range(steps):
            offset = len(child.output)
            child.send(RIGHT_KEY)
            # Wait for the redraw of the next profile; curses updates only
            # what changed (it may scroll the mode row rather than redraw it).
            _read_after(child, offset, b"\x1b[")

    def test_live(self) -> None:
        child = self.sessions_child("claude-multi-pty-lineup-live-", ["-r"], setup=_steps_to(_LIVE_SEED))
        self._to_target(child)
        offset = len(child.output)
        child.send(b"\r")
        _read_after(child, offset, b"/reload-plugins")
        offset = len(child.output)
        child.send(b"\r")
        _read_after(child, offset, SESSIONS_READY)
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        # The "mode" label and its text are drawn with different attributes.
        self.assertIn("LIVE — running agents keep their model".encode(), output)
        self.assertEqual(int(_line_of(output, b"GEN=")), int(_line_of(output, b"GEN0=")) + 1)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_relaunch(self) -> None:
        child = self.sessions_child("claude-multi-pty-lineup-relaunch-", ["-r"], setup=_steps_to(_RELAUNCH_SEED))
        self._to_target(child)
        offset = len(child.output)
        child.send(b"\r")
        _read_after(child, offset, b"Recorded as a pending change")
        offset = len(child.output)
        child.send(b"\r")
        _read_after(child, offset, SESSIONS_READY)
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn("RELAUNCH — exit the session; resume applies it".encode(), output)
        self.assertEqual(_line_of(output, b"PENDING="), "True")
        self.assertEqual(int(_line_of(output, b"GEN=")), int(_line_of(output, b"GEN0=")))
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_esc_in_the_lineup_dialog_changes_nothing(self) -> None:
        # Restores tp SessionsAndTransitionPTYTests.test_transition_esc_prints_command_without_mutating.
        child = self.sessions_child("claude-multi-pty-lineup-esc-", ["-r"])
        child.read_until(SESSIONS_READY, FIXTURE_TIMEOUT)
        offset = len(child.output)
        child.send(b"T")
        _read_after(child, offset, b"change lineup")
        offset = len(child.output)
        child.send(b"\x1b")
        _read_after(child, offset, SESSIONS_READY)
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(_line_of(output, b"UNCHANGED="), "True")
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)


class OnboardingJourneyPTYTests(unittest.TestCase):
    """Real curses, fixture transports, both exact-client outcomes."""

    def child(self, *, pool=False, unavailable=False, keyed=False):
        code = f'''
import sys, json, copy
import tests  # live-port tripwire, including this child
from unittest import mock
from test_onboarding_isolation import JourneyFixture, KeyedJourneyFixture
import test_openai_compat_keyed as keyed_fixture
from claude_multi import catalog, operator, state
from claude_multi import tui, views, profile, qualify
import claude_multi.cli.screens.providers as providers
import claude_multi.cli.screens.models as models
import claude_multi.cli.screens.profile_editor as editor
import claude_multi.cli.types as types
case = KeyedJourneyFixture() if {keyed!r} else JourneyFixture()
case.setUp()
runtime = case.runtime
pool = {pool!r}
key = 'custom-pool-reviewer' if pool else 'custom-vendor-fixture-reviewer'
pid = 'anthropic' if pool else 'openrouter'
if {keyed!r}:
    key, pid = keyed_fixture.KEYED_KEY, keyed_fixture.KEYED_ID
    state.atomic_write(case.secret_file, case.secret_file.read_bytes() +
                       f'{{keyed_fixture.KEYED_SECRET}}={{keyed_fixture.KEYED_VALUE}}\\n'.encode())
if {unavailable!r}:
    runtime.exact_client_runner = lambda request: qualify.ExactClientOutcome('inconclusive', 'unavailable')
try:
    if {keyed!r}:
        def new_provider_app(win):
            providers._ProvidersScreen(runtime, palette=tui.MONO_PALETTE, journal=lambda:None).run(win)
        tui.run_curses_on_streams(new_provider_app, sys.stdin, sys.stdout)
        layer = runtime.operator_snapshot().layer
        assert layer.route_status[pid] == 'approved'
        assert not case.http_calls
        # The TUI kind field, not a default, must have declared the keyed kind.
        declared = layer.providers[pid]
        assert declared.kind == operator.KEYED_KIND, declared.kind
        assert catalog.is_keyed_compat(declared.entry), declared.entry
        print('KEYED_KIND=' + declared.kind, flush=True)
        print('KEYED_ROUTE_APPROVED', flush=True)
    def provider_app(win):
        screen = providers._ProvidersScreen(runtime, palette=tui.MONO_PALETTE, journal=lambda:None)
        screen.selected = next(i for i,r in enumerate(screen.rows) if r.id == pid)
        screen.run(win)
    tui.run_curses_on_streams(provider_app, sys.stdin, sys.stdout)
    case.serve_current()
    if {keyed!r}:
        assert key not in runtime.current_effective().admitted_lines
        assert case.keyed_picker(key).selectable
        assert not case.http_calls
        print('KEYED_NOT_ADMITTED_SELECTABLE', flush=True)
    print('MODELS_STAGE', flush=True)
    real_admit = providers.ConnectActions.admit
    admission_calls = []
    def metadata_admit(actions, selected):
        with mock.patch.object(runtime, 'smoke', side_effect=AssertionError('admission inference')), \\
                mock.patch.object(runtime, 'qualify_post', side_effect=AssertionError('admission inference')):
            code = real_admit(actions, selected)
        admission_calls.append(code)
        return code
    providers.ConnectActions.admit = metadata_admit
    def model_app(win):
        screen = models._ModelsScreen(runtime, palette=tui.MONO_PALETTE)
        screen.index = screen.keys.index(key)
        screen.run(win)
    tui.run_curses_on_streams(model_app, sys.stdin, sys.stdout)
    assert admission_calls == [0], admission_calls
    print('ADMISSION_REQUESTS=0', flush=True)
    doc, ev = case.evaluation(key)
    if {unavailable!r}:
        assert not ev.errors, ev.errors
        assert any(w.code == 'exact-client' and 'unavailable' in w.message for w in ev.lineup.warnings), ev.lineup.warnings
        print('ATTENTION_EXACT_CLIENT', flush=True)
    assert not ev.errors, ev.errors
    doc['agents'] = {{}}
    model = views.picker_rows(views.line_rows(runtime.lineup_catalog(), runtime.current_effective(), custom_ids=frozenset()),
                              slot='cm-reviewer', bindings={{}}, lcat=runtime.lineup_catalog(),
                              eff=runtime.current_effective(), current=None)
    print('BIND_INDEX=' + str(next(i for i,row in enumerate(model.items) if row.key == key)), flush=True)
    def edit_app(win):
        st = tui.ProfileEditorState(doc, cat=runtime.lineup_catalog(), bindings={{}},
                                    effective=runtime.current_effective(), origin=None, is_seed=False)
        callbacks = editor._profile_editor_callbacks(runtime, tui.MONO_PALETTE, editor_state=st)
        tui.ProfileEditorScreen(st, palette=tui.MONO_PALETTE, callbacks=callbacks,
                                environ=runtime.environ, initial_focus='cm-reviewer').run(win)
    tui.run_curses_on_streams(edit_app, sys.stdin, sys.stdout)
    saved = runtime.profiles.load(doc['name'])
    assert saved['agents']['cm-reviewer']['model'] == key, saved
    target = types.LaunchTarget('profile', saved, saved['name'], True, 'Profile fixture-onboarding')
    prepared = runtime.prepare(target, action='fresh', passthrough=[])
    assert runtime.perform(prepared) == 0
    print('JOURNEY_LAUNCHED=' + str(len(case.launches)), flush=True)
    print('LISTINGS=' + str(len(case.listing_calls)), flush=True)
finally:
    case.doCleanups()
'''
        child = PTYProcess(code, extra_pythonpath=(REPO_ROOT, TESTS_ROOT))
        self.addCleanup(child.close)
        return child

    def journey(self, *, pool=False, unavailable=False, keyed=False):
        child = self.child(pool=pool, unavailable=unavailable, keyed=keyed)
        if keyed:
            child.read_until(b"providers", FIXTURE_TIMEOUT)
            child.send(b"N")
            child.read_until(b"add your own provider")
            child.send(b"\x1bOF\n")  # End: every field (the manual form)
            child.read_until(b"Provider id")
            child.send(b"chatco\n" + b"\x1bOC" + b"\nhttps://api.chatco.example/v1\n\nCHATCO_API_KEY\n\n\n\n\n\n")
            child.read_until(b"Declare this provider?", FIXTURE_TIMEOUT)
            child.send(b"\n")
            child.read_until(b"approve the credential route of provider chatco:", FIXTURE_TIMEOUT)
            child.send(b"y")
            child.read_until(b"declared provider chatco", FIXTURE_TIMEOUT)
            mark = len(child.output)
            child.send(b"\x1b")  # the result, then the picker and Providers
            _read_after(child, mark, b"Enter connect", FIXTURE_TIMEOUT)
            child.send(b"\x1b\x1b")
            child.read_until(b"KEYED_ROUTE_APPROVED", FIXTURE_TIMEOUT)
            self.assertEqual(_line_of(bytes(child.output), b"KEYED_KIND="), "openai-compatible")
        child.read_until(b"providers", FIXTURE_TIMEOUT)
        child.send(b"A")
        child.read_until(b"add models")
        if pool or keyed:
            child.send(DOWN_KEY + b"\n")
        else:
            child.send(b"\n")
            child.read_until(b"Confirm explicit action")
            child.send(b"y")
            # The account-filtered listing has its own consent after the public one.
            child.read_until(b"/user")
            child.send(b"y")
            child.read_until(b"advertised models")
            child.send(b" \n")
        child.read_until(b"declare model")
        # Keep the listing prefill, or enter sourced manual pool metadata.
        if keyed:
            child.send(b"chatco-chat-1\n\ncustom-chatco-chat\n200000\n\nfixture docs, date\n\n")
        elif pool:
            child.send(b"claude-fixture-reviewer-9\n\ncustom-pool-reviewer\n1000000\n\nfixture docs, date\n\n")
        else:
            # Keep the display name and other prefills. The family of an
            # aggregator line is "unknown": replace it (one more Backspace
            # would step back to the previous field).
            child.send(b"\n\n\n\n\n\n" + b"\x7f" * len("unknown") + b"openai\n")
        # Efforts and default; output bound; request agents, then the role.
        child.send(b"\n\n64000\n" + b"\x1bOC" + b"\ncm-reviewer\n")
        child.read_until(b"Declaration preview", FIXTURE_TIMEOUT)
        child.send(b"\n")
        child.read_until(b"declared custom-", FIXTURE_TIMEOUT)
        child.send(b"\x1b")  # result -> Providers
        child.send(b"\x1b")  # Providers -> next fixture stage
        child.read_until(b"MODELS_STAGE", FIXTURE_TIMEOUT)
        child.read_until(b"admit badge", FIXTURE_TIMEOUT)
        mark = len(child.output)
        child.send(b"\n")
        _read_after(child, mark, b"Confirm explicit action", FIXTURE_TIMEOUT)
        child.send(b"y")
        child.read_until(b"Action result", FIXTURE_TIMEOUT)
        child.send(b"\x1bq")
        child.read_until("qualify — choose checks".encode(), FIXTURE_TIMEOUT)
        child.send(b"\n\n\n")
        child.read_until(b"Qualification plan", FIXTURE_TIMEOUT)
        if pool:
            child.read_until(b"exact-client", FIXTURE_TIMEOUT)
        child.send(b"y")
        child.read_until(b"evidence recorded for", FIXTURE_TIMEOUT)
        child.send(b"\x1b\x1b")
        if unavailable:
            child.read_until(b"ATTENTION_EXACT_CLIENT", FIXTURE_TIMEOUT)
        child.read_until(b"BIND_INDEX=", FIXTURE_TIMEOUT)
        # The index comes from the fixture's actual picker inventory.
        index = int(_line_of(bytes(child.output), b"BIND_INDEX="))
        child.send(b"\n")
        child.read_until(b"choose model", FIXTURE_TIMEOUT)
        child.send(b"\x1bOH" + DOWN_KEY * index + b"\n\x13")
        child.read_until(b"saved fixture-onboarding", FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        child.read_until(b"JOURNEY_LAUNCHED=1", FIXTURE_TIMEOUT)
        code, output, attrs = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"LISTINGS=" + (b"0" if pool or keyed else b"2"), output)
        if keyed:
            self.assertIn(b"KEYED_NOT_ADMITTED_SELECTABLE", output)
        self.assertIn(b"ADMISSION_REQUESTS=0", output)
        self.assertNotIn(b"Traceback", output)
        _restored(self, child, attrs)

    def test_openrouter_non_xai_journey(self):
        self.journey()

    def test_authenticated_pool_journey(self):
        self.journey(pool=True)

    def test_pool_missing_exact_client_warns_and_launches(self):
        self.journey(pool=True, unavailable=True)

    def test_provider_wizard_recommended_kind_and_cancel(self):
        code = '''
import sys
from claude_multi import tui, views
form = tui.OnboardingForm('new provider', views.provider_form_fields())
result = tui.run_curses_on_streams(form.run, sys.stdin, sys.stdout)
assert result is None
print('CANCELLED_WITHOUT_DECLARATION', flush=True)
'''
        child = PTYProcess(code, extra_pythonpath=(REPO_ROOT, TESTS_ROOT))
        self.addCleanup(child.close)
        child.read_until(b"Provider id")
        child.send(b"fixture-new\n")
        child.read_until(b"anthropic-compatible")
        child.read_until(b"recommended")
        child.send(b"\x1b")
        code, output, attrs = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"CANCELLED_WITHOUT_DECLARATION", output)
        _restored(self, child, attrs)


class KeyedJourneyPTYTests(unittest.TestCase):
    child = OnboardingJourneyPTYTests.child

    def test_keyed_tui_optional_admission_qualification_and_binding(self):
        OnboardingJourneyPTYTests.journey(self, keyed=True)
