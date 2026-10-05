"""Acquisition of the owned Claude Code copy and ``claude-multi setup --step claude``.

Fixtures only: local files and a loopback fake download server reached
through the download opener seam. Nothing is fetched from the network and
Anthropic's installer is never involved.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from claude_multi import __version__, acquire, cli, pin, release_manifest, retention, state
from _v4 import V4Case


PAYLOAD = b"#!/bin/sh\n# fake claude build\n" + bytes(range(256)) * 64


def _contract(platform: str, data: bytes = PAYLOAD, version: str = "2.1.400") -> dict:
    return {"verified": [{
        "version": version,
        "platforms": {platform: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}},
        "manifest_sha256": "1" * 64, "signature_sha256": None,
        "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE", "verified_at": "2026-10-01",
        "evidence": {platform: "battery", "receipt_sha256": "2" * 64},
    }]}


class _Handler(BaseHTTPRequestHandler):
    payload = PAYLOAD
    truncate = 0
    requests: list = []

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:
        cls = type(self)
        cls.requests.append((self.path, self.headers.get("Range"), self.headers.get("Authorization")))
        data = cls.payload
        start = 0
        ranged = self.headers.get("Range")
        if ranged:
            start = int(ranged.split("=", 1)[1].rstrip("-"))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        body = data[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if cls.truncate > 0:
            cls.truncate -= 1
            self.wfile.write(body[: len(body) // 3])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(body)


class _Response:
    """Report the planned URL (the fake server stands in for its host)."""

    def __init__(self, inner, url: str, reported: str | None = None):
        self.inner, self.url, self.reported = inner, url, reported
        self.status = inner.status
        self.headers = inner.headers

    def geturl(self) -> str:
        return self.reported or self.url

    def read(self, size: int = -1) -> bytes:
        return self.inner.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.inner.close()


class FakeServerCase(unittest.TestCase):
    def setUp(self) -> None:
        handler = type("Handler", (_Handler,), {"requests": [], "truncate": 0, "payload": PAYLOAD})
        self.handler = handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory(prefix="cm-acquire-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(mode=0o700)
        self.env = {"HOME": str(self.home), "PATH": str(Path(self.tmp.name) / "empty-path")}
        self.platform = pin.host_platform()
        self.contract = _contract(self.platform)
        self.owned = pin.owned_path(self.env, "2.1.400", self.platform)
        self.reported: str | None = None

    def opener(self, url, *, headers, timeout):
        parsed = urllib.parse.urlsplit(url)
        local = f"http://127.0.0.1:{self.server.server_port}{parsed.path}"
        inner = urllib.request.urlopen(urllib.request.Request(local, headers=dict(headers)), timeout=timeout)
        return _Response(inner, url, self.reported)

    def acquire(self, **kwargs):
        kwargs.setdefault("platform", self.platform)
        kwargs.setdefault("opener", self.opener)
        return acquire.acquire(self.contract, self.env, **kwargs)


class DownloadTests(FakeServerCase):
    def test_no_consent_sends_nothing_and_names_the_remedies(self):
        for consent in (None, lambda _plan: False):
            with self.subTest(consent=consent):
                outcome = self.acquire(consent=consent)
                self.assertEqual(outcome.state, "declined")
                self.assertIsNone(outcome.path)
                self.assertIn("--claude-from PATH", outcome.notes[-1])
                self.assertIn("update claude-multi", outcome.notes[-1])
        self.assertEqual(self.handler.requests, [])
        self.assertFalse(self.owned.exists())

    def test_plan_shows_the_one_request_and_the_size(self):
        seen = []
        outcome = self.acquire(consent=lambda plan: seen.append(plan.text(self.env)) or True)
        self.assertEqual(outcome.state, "downloaded")
        text = seen[0]
        url = f"https://downloads.claude.ai/claude-code-releases/2.1.400/{self.platform}/{pin.binary_name(self.platform)}"
        self.assertIn(f"  GET {url}", text)
        self.assertIn("Authentication: none", text)
        self.assertIn(f"Size: {len(PAYLOAD)} bytes", text)
        self.assertIn("installer is not run", text)
        self.assertTrue(text.endswith("Download it now? [y/N] "))
        self.assertEqual(self.handler.requests, [(urllib.parse.urlsplit(url).path, None, None)])

    def test_download_lands_verified_private_and_executable(self):
        self.acquire(consent=lambda _plan: True)
        self.assertEqual(self.owned.read_bytes(), PAYLOAD)
        self.assertEqual(stat.S_IMODE(self.owned.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.owned.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(pin.owned_root(self.env).stat().st_mode), 0o700)
        self.assertEqual(list(self.owned.parent.glob(".*")), [])
        self.assertEqual(acquire.read_index(self.env), {__version__: "2.1.400"})

    def test_interrupted_download_resumes_with_range(self):
        self.handler.truncate = 1
        notes = []
        outcome = self.acquire(consent=lambda _plan: True, progress=notes.append)
        self.assertEqual(outcome.state, "downloaded")
        self.assertEqual(self.owned.read_bytes(), PAYLOAD)
        ranges = [entry[1] for entry in self.handler.requests]
        self.assertEqual(ranges[0], None)
        self.assertTrue(ranges[1].startswith("bytes=") and ranges[1] != "bytes=0-", ranges)
        self.assertIn("try 1 of 3 failed", notes[0])

    def test_three_failures_keep_the_partial_for_a_later_resume(self):
        self.handler.truncate = 3
        with self.assertRaisesRegex(acquire.AcquireError, "did not complete.*--claude-from PATH"):
            self.acquire(consent=lambda _plan: True)
        self.assertEqual(len(self.handler.requests), 3)
        self.assertFalse(self.owned.exists())
        partial = self.owned.parent / f".{self.owned.name}.part"
        self.assertTrue(0 < partial.stat().st_size < len(PAYLOAD))
        outcome = self.acquire(consent=lambda _plan: True)
        self.assertEqual(outcome.state, "downloaded")
        self.assertTrue(self.handler.requests[-1][1].startswith("bytes="))
        self.assertFalse(partial.exists())

    def test_resume_at_another_offset_starts_over(self):
        self.handler.truncate = 1
        original = self.handler.do_GET

        def shifted(handler):
            if handler.headers.get("Range"):
                handler.headers.replace_header("Range", "bytes=0-")
            original(handler)
        self.handler.do_GET = shifted
        outcome = self.acquire(consent=lambda _plan: True)
        self.assertEqual(outcome.state, "downloaded")
        self.assertEqual(self.owned.read_bytes(), PAYLOAD)
        self.assertEqual(len(self.handler.requests), 3)  # truncated, refused offset, clean start
        self.assertIsNone(self.handler.requests[-1][1])

    def test_a_resume_needs_room_only_for_the_missing_bytes(self):
        partial = acquire.partial_path(self.owned)
        state.ensure_private_dir(partial.parent)
        partial.write_bytes(PAYLOAD[:-100])
        with mock.patch.object(acquire, "_free_bytes", return_value=200):  # < the build, > the rest
            outcome = self.acquire(consent=lambda _plan: True)
        self.assertEqual(outcome.state, "downloaded")
        self.assertEqual(self.owned.read_bytes(), PAYLOAD)
        self.assertEqual([entry[1] for entry in self.handler.requests], [f"bytes={len(PAYLOAD) - 100}-"])
        # A download that starts over still needs room for the whole build.
        self.owned.unlink()
        with mock.patch.object(acquire, "_free_bytes", return_value=200), \
                self.assertRaisesRegex(acquire.AcquireError, "not enough free space"):
            self.acquire(consent=lambda _plan: True)
        partial.write_bytes(PAYLOAD + b"x")  # larger than the build: discarded, so no credit
        with mock.patch.object(acquire, "_free_bytes", return_value=200), \
                self.assertRaisesRegex(acquire.AcquireError, "not enough free space"):
            self.acquire(consent=lambda _plan: True)
        # A complete partial needs no room and sends nothing.
        partial.write_bytes(PAYLOAD)
        sent = len(self.handler.requests)
        with mock.patch.object(acquire, "_free_bytes", return_value=0):
            self.assertEqual(self.acquire(consent=lambda _plan: True).state, "downloaded")
        self.assertEqual(len(self.handler.requests), sent)
        self.assertEqual(self.owned.read_bytes(), PAYLOAD)
        self.assertFalse(partial.exists())

    def test_wrong_bytes_are_discarded_and_never_installed(self):
        self.handler.payload = PAYLOAD[:-1] + b"X"
        with self.assertRaisesRegex(acquire.AcquireError, "does not match the pinned sha256"):
            self.acquire(consent=lambda _plan: True)
        self.assertFalse(self.owned.exists())
        self.assertEqual(list(self.owned.parent.glob(".*")), [])
        self.assertEqual(len(self.handler.requests), 1)  # no retry of a wrong file

    def test_redirect_to_another_host_is_refused(self):
        self.reported = "https://example.invalid/claude"
        with self.assertRaisesRegex(acquire.AcquireError, "did not complete"):
            self.acquire(consent=lambda _plan: True)
        self.assertFalse(self.owned.exists())

    def test_host_lock_allows_one_acquisition_per_version(self):
        lock = acquire._lock(self.env, "2.1.400")
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.release)
        with self.assertRaises(acquire.AcquireBusy):
            self.acquire(consent=lambda _plan: True)
        self.assertEqual(self.handler.requests, [])

    def test_platform_without_a_build_is_refused(self):
        other = "win32-arm64" if self.platform != "win32-arm64" else "linux-x64"
        with self.assertRaisesRegex(acquire.AcquireError, f"no verified Claude Code 2.1.400 build for {other}"):
            acquire.acquire(self.contract, self.env, platform=other, consent=lambda _plan: True,
                            opener=self.opener)

    def test_download_url_shape(self):
        self.assertEqual(acquire.download_url("2.1.400", "win32-x64"),
                         "https://downloads.claude.ai/claude-code-releases/2.1.400/win32-x64/claude.exe")
        with self.assertRaises(acquire.AcquireError):
            acquire.download_url("2.1.400/../x", "linux-x64")


class LocalSourceTests(FakeServerCase):
    def write(self, path: Path, data: bytes = PAYLOAD) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755)
        return path

    def test_order_and_skip_notes(self):
        retained = paths_retained = acquire.paths.retained_root(self.env) / "2.1.400"
        self.write(paths_retained, b"#!/bin/other\n")
        native = self.write(self.home / ".local/share/claude/versions/2.1.400")
        before = (native.stat().st_ino, native.stat().st_mtime_ns, native.read_bytes())
        outcome = self.acquire(consent=lambda _plan: self.fail("no download when a local file matches"))
        self.assertEqual(outcome.state, "copied")
        self.assertIn("your Claude Code versions directory", outcome.source)
        self.assertIn("skipped the retained copy", outcome.notes[0])
        self.assertEqual(self.owned.read_bytes(), PAYLOAD)
        self.assertNotEqual(self.owned.stat().st_ino, native.stat().st_ino)  # never linked
        self.assertEqual((native.stat().st_ino, native.stat().st_mtime_ns, native.read_bytes()), before)
        self.assertEqual(retained.read_bytes(), b"#!/bin/other\n")
        self.assertEqual(self.handler.requests, [])

    def test_xdg_data_home_and_path_claude(self):
        xdg = Path(self.tmp.name) / "xdg"
        self.env["XDG_DATA_HOME"] = str(xdg)
        self.write(xdg / "claude/versions/2.1.400")
        self.assertEqual(self.acquire().source.split(" (")[0], "your Claude Code versions directory")
        self.owned.unlink()
        del self.env["XDG_DATA_HOME"]
        bindir = Path(self.tmp.name) / "bin"
        target = self.write(Path(self.tmp.name) / "install/2.1.400")
        bindir.mkdir()
        (bindir / "claude").symlink_to(target)
        self.env["PATH"] = str(bindir)
        outcome = self.acquire()
        self.assertIn("the claude on your PATH", outcome.source)
        self.assertIn(str(target), outcome.source)

    def test_claude_from_is_first_and_must_match(self):
        given = self.write(Path(self.tmp.name) / "given")
        self.write(self.home / ".local/share/claude/versions/2.1.400")
        outcome = self.acquire(claude_from=given)
        self.assertIn("--claude-from", outcome.source)
        wrong = self.write(Path(self.tmp.name) / "wrong", b"nope")
        with self.assertRaisesRegex(acquire.AcquireError, "is not the Claude Code 2.1.400 build"):
            self.acquire(claude_from=wrong)
        with self.assertRaisesRegex(acquire.AcquireError, "is not a file"):
            self.acquire(claude_from=Path(self.tmp.name) / "absent")

    def test_present_copy_is_kept_and_a_damaged_one_replaced(self):
        self.write(self.home / ".local/share/claude/versions/2.1.400")
        self.acquire()
        inode = self.owned.stat().st_ino
        self.assertEqual(self.acquire().state, "present")
        self.assertEqual(self.owned.stat().st_ino, inode)
        self.owned.write_bytes(b"damaged")
        self.assertEqual(self.acquire().state, "copied")
        self.assertEqual(self.owned.read_bytes(), PAYLOAD)

    def test_a_symlinked_version_directory_is_refused_and_left_alone(self):
        elsewhere = self.write(Path(self.tmp.name) / "elsewhere" / "claude")
        root = pin.owned_root(self.env)
        state.ensure_private_dir(root)
        (root / "2.1.400").symlink_to(elsewhere.parent)
        self.write(self.home / ".local/share/claude/versions/2.1.400")
        with self.assertRaisesRegex(acquire.AcquireError,
                                    "2.1.400 is a symbolic link.*rm .*rerun `claude-multi setup --step claude`"):
            self.acquire()
        self.assertTrue((root / "2.1.400").is_symlink())
        self.assertEqual(elsewhere.read_bytes(), PAYLOAD)
        self.assertEqual(sorted(path.name for path in elsewhere.parent.iterdir()), ["claude"])
        # The launch refuses the same layout, so setup never reports it set up.
        with self.assertRaises(pin.PinError):
            pin.verify_owned(self.contract, self.env, platform=self.platform)
        (root / "2.1.400").unlink()
        (root / "2.1.400").write_bytes(b"")
        with self.assertRaisesRegex(acquire.AcquireError, "is not a directory"):
            self.acquire()

    def test_unsafe_owned_root_is_refused(self):
        root = pin.owned_root(self.env)
        root.parent.mkdir(parents=True)
        root.mkdir(mode=0o755)
        root.chmod(0o755)
        with self.assertRaisesRegex(acquire.AcquireError, "group/other"):
            self.acquire()

    def test_migrate_retained_respects_the_host_lock(self):
        source = self.write(acquire.paths.retained_root(self.env) / "2.1.400")
        lock = acquire._lock(self.env, "2.1.400")
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertIsNone(acquire.migrate_retained(self.contract, self.env, source.parent,
                                                       platform=self.platform))
        finally:
            lock.release()
        self.assertEqual(acquire.migrate_retained(self.contract, self.env, source.parent,
                                                  platform=self.platform), self.owned)


class PlatformKeyTests(unittest.TestCase):
    def test_every_manifest_platform(self):
        table = {
            ("Linux", "x86_64", False): "linux-x64", ("Linux", "aarch64", False): "linux-arm64",
            ("Linux", "x86_64", True): "linux-x64-musl", ("Linux", "arm64", True): "linux-arm64-musl",
            ("Darwin", "arm64", False): "darwin-arm64", ("Darwin", "x86_64", False): "darwin-x64",
            ("Windows", "AMD64", False): "win32-x64", ("Windows", "ARM64", False): "win32-arm64",
        }
        for (system, machine, musl), key in table.items():
            with self.subTest(system=system, machine=machine, musl=musl):
                self.assertEqual(release_manifest.platform_key(system=system, machine=machine, musl=musl), key)
        self.assertEqual(set(table.values()), set(pin.PLATFORMS))
        for system, machine in (("Linux", "riscv64"), ("FreeBSD", "amd64")):
            with self.assertRaisesRegex(release_manifest.ReleaseManifestUnavailable, "unsupported platform"):
                release_manifest.platform_key(system=system, machine=machine, musl=False)


class SetupCommandTests(V4Case):
    def setUp(self) -> None:
        super().setUp()
        self.fake_binary.unlink()

    def run_setup(self, *argv: str, input_stream=None) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(acquire, "_open", side_effect=AssertionError("no network in tests")):
            code = cli.main(["setup", *argv], runtime=self.runtime, input_stream=input_stream,
                            output_stream=output, interactive=False)
        return code, output.getvalue()

    def answering(self, answer):
        """A terminal-like session with no Claude-session marker whose y/N
        prompt gets ``answer`` (an exception is raised instead)."""

        questions: list[str] = []

        def confirm(text):
            questions.append(text)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        stream = mock.Mock(spec=["confirm"], confirm=confirm)
        for marker in ("CLAUDE_MULTI_MANAGED_ID", "CLAUDECODE"):
            self.runtime.environ.pop(marker, None)
        return stream, questions

    def test_claude_step_copies_from_a_given_file(self):
        given = self.root / "given"
        given.write_bytes(b"#!/bin/fake-claude\n")
        given.chmod(0o755)
        code, output = self.run_setup("--step", "claude", "--claude-from", str(given))
        self.assertEqual(code, 0, output)
        self.assertIn("Claude Code 2.1.281 copied from the file given with --claude-from", output)
        self.assertTrue(self.fake_binary.is_file())
        code, output = self.run_setup("--step", "claude")
        self.assertEqual(code, 0, output)
        self.assertIn("Claude Code 2.1.281 is set up for claude-multi", output)

    def test_download_question_needs_a_terminal_and_nothing_is_sent(self):
        code, output = self.run_setup("--step", "claude")
        self.assertEqual(code, 1, output)
        self.assertIn("needs a terminal outside Claude Code sessions", output)
        self.assertIn("nothing was downloaded", output)
        self.assertIn("not set up for claude-multi", output)
        self.assertFalse(self.fake_binary.exists())

    def test_a_declined_download_exits_3_and_sends_nothing(self):
        stream, questions = self.answering(False)
        with mock.patch("claude_multi.cli.consent.stdio_ttys", return_value=True):
            code, output = self.run_setup("--step", "claude", input_stream=stream)
        self.assertEqual(code, 3, output)
        self.assertEqual(len(questions), 1)
        self.assertIn("downloads.claude.ai", questions[0])
        self.assertIn("nothing was downloaded", output)
        self.assertIn("not set up for claude-multi", output)
        self.assertFalse(self.fake_binary.exists())

    def test_a_guard_refusal_exits_1_without_asking(self):
        stream, questions = self.answering(True)
        self.runtime.environ["CLAUDECODE"] = "1"
        with mock.patch("claude_multi.cli.consent.stdio_ttys", return_value=True):
            code, output = self.run_setup("--step", "claude", input_stream=stream)
        self.assertEqual(code, 1, output)
        self.assertEqual(questions, [])
        self.assertIn("needs a terminal outside Claude Code sessions (CLAUDECODE set)", output)
        self.assertFalse(self.fake_binary.exists())

    def test_cancelling_the_question_exits_130(self):
        stream, _questions = self.answering(KeyboardInterrupt())
        with mock.patch("claude_multi.cli.consent.stdio_ttys", return_value=True):
            code, output = self.run_setup("--step", "claude", input_stream=stream)
        self.assertEqual(code, 130, output)
        self.assertIn("setup --step claude: cancelled", output)
        self.assertFalse(self.fake_binary.exists())

    def test_a_failed_step_exits_1(self):
        given = self.root / "given"
        given.write_bytes(b"#!/bin/another-build\n")
        code, output = self.run_setup("--claude-from", str(given))
        self.assertEqual(code, 1, output)
        self.assertIn("is not the Claude Code 2.1.281 build this release pins", output)
        lock = acquire.version_lock(self.env, "2.1.281")
        self.assertTrue(lock.acquire(blocking=False))
        try:
            code, output = self.run_setup("--step", "claude")
        finally:
            lock.release()
        self.assertEqual(code, 1, output)
        self.assertIn("another claude-multi setup is putting Claude Code 2.1.281 in place", output)
        self.assertFalse(self.fake_binary.exists())

    def test_setup_prunes_copies_no_release_needs(self):
        for version in ("2.1.090", "2.1.100", "2.1.200", "2.1.250"):
            old = pin.owned_path(self.env, version, self.platform)
            state.ensure_private_dir(old.parent)
            old.write_bytes(b"old")
        odd = pin.owned_path(self.env, "2.1.090", self.platform).parent / "link"
        odd.symlink_to(self.root)  # not a regular file: the version is left in place
        given = self.root / "given"
        given.write_bytes(b"#!/bin/fake-claude\n")
        given.chmod(0o755)
        in_use = pin.take_use_lock(self.env, "2.1.200", shared=True)  # a session runs it
        try:
            with mock.patch.object(retention, "scan_processes", return_value=retention.ProcessScan()), \
                    mock.patch.object(retention, "_gateway_launcher", return_value=None):
                code, output = self.run_setup("--claude-from", str(given))
        finally:
            in_use.release()
        self.assertEqual(code, 0, output)
        self.assertIn("Removed Claude Code copies no release here needs: 2.1.100.", output)
        self.assertIn("Kept Claude Code copies in use: 2.1.200 — once no session runs them, "
                      "`claude-multi setup --step claude` removes them.", output)
        self.assertIn("Could not remove Claude Code copies: 2.1.090 — check ", output)
        self.assertIn(", then run `claude-multi setup --step claude` again.", output)
        self.assertTrue(pin.owned_path(self.env, "2.1.090", self.platform).exists())
        self.assertTrue(pin.owned_path(self.env, "2.1.200", self.platform).exists())
        self.assertTrue(pin.owned_path(self.env, "2.1.250", self.platform).exists())  # previous pin

    def test_update_outside_an_installed_release_changes_nothing(self):
        before = sorted(str(path) for path in pin.owned_root(self.env).rglob("*")) if pin.owned_root(self.env).exists() else []
        output = io.StringIO()
        code = cli.main(["update"], runtime=self.runtime, output_stream=output, interactive=False)
        self.assertEqual(code, 1)  # refused: not an installed release
        text = output.getvalue()
        self.assertIn("not installed by the claude-multi installer", text)
        self.assertIn("Nothing was changed", text)
        self.assertNotIn("evidence", text)
        after = sorted(str(path) for path in pin.owned_root(self.env).rglob("*")) if pin.owned_root(self.env).exists() else []
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
