"""Pure frames, fake release/wrapper fixtures and private-file tests; no clients."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from _layout import REPO_ROOT

PLATFORMS = ("linux",)
SCRIPTS = REPO_ROOT / ".github/scripts"
_spec = importlib.util.spec_from_file_location("resume_locations", SCRIPTS / "resume_locations.py")
reporter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reporter)
_bundle_spec = importlib.util.spec_from_file_location("fixture_bundle", REPO_ROOT / "tools/_build/bundle.py")
bundle = importlib.util.module_from_spec(_bundle_spec)
_bundle_spec.loader.exec_module(bundle)


class Frame:
    def __init__(self, filename, function="main", line=1, parent=None):
        self.f_code = SimpleNamespace(co_filename=filename, co_name=function)
        self.f_lineno = line
        self.f_back = parent

    @property
    def f_locals(self):
        raise AssertionError("secret locals inspected")

    @property
    def f_globals(self):
        raise AssertionError("secret globals inspected")


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.install, self.dist, self.scratch, self.meta = (self.root / name for name in ("install", "dist", "scratch", "metadata"))
        for path in (self.install, self.dist, self.scratch, self.meta):
            path.mkdir()
        self.source = next(iter(reporter.SOURCES.values()))
        self.registry = {"verified.py": {"source": self.source, "functions": {"main", "<module>"}, "lines": 100}}

    def fake_release(self):
        original = bundle.launcher_text("claude-multi", "3.14.8")
        files = {relative: b"def main():\n    return None\n" for relative in reporter.SOURCES}
        files["bin/claude-multi"] = original
        for relative, raw in files.items():
            path = self.install / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        archive_name = "claude-multi-1.1.0-linux-x86_64.tar.gz"
        archive = self.dist / archive_name
        with tarfile.open(archive, "w:gz") as handle:
            for relative, raw in files.items():
                info = tarfile.TarInfo(archive_name.removesuffix(".tar.gz") + "/" + relative)
                info.size = len(raw)
                handle.addfile(info, io.BytesIO(raw))
        (self.dist / "MANIFEST.json").write_text(json.dumps({"version": "1.1.0", "assets": {archive_name: {
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "size": archive.stat().st_size}}}))
        shutil.copyfile(SCRIPTS / "resume_locations.py", self.scratch / "resume_locations.py")
        return original

    def test_secret_locals_ignored_and_unknown_paths_functions_generalized(self):
        frame = Frame("/private/NEVER-EXPORT/token.py", "NEVER_EXPORT_SECRET", 42,
                      Frame("verified.py", "NEVER_EXPORT_SECRET", 12, Frame("verified.py", "main", 18)))
        row = reporter.capture(1, self.registry, {987654321: frame}, 987654321)
        reporter.validate_report(row)
        locations = row["threads"][0]["frames"]
        self.assertEqual(locations[0], {"source": "other", "function": "other", "line": 0})
        self.assertEqual(locations[1], {"source": self.source, "function": "other", "line": 0})
        self.assertEqual(locations[2], {"source": self.source, "function": "main", "line": 18})
        self.assertNotIn("NEVER", json.dumps(row))
        self.assertNotIn("987654321", json.dumps(row))
        self.assertTrue(row["threads"][0]["main"])
        self.assertEqual(row["threads"][0]["ordinal"], 0)

    def test_thread_depth_count_and_byte_caps(self):
        chain = None
        for _ in range(100):
            chain = Frame("verified.py", parent=chain)
        row = reporter.capture(1, self.registry, {index: chain for index in range(20)}, 19)
        reporter.validate_report(row)
        self.assertEqual(len(row["threads"]), reporter.MAX_THREADS)
        self.assertTrue(row["threads_capped"])
        self.assertTrue(row["threads"][0]["main"])
        self.assertTrue(all(thread["depth_capped"] for thread in row["threads"]))
        self.assertTrue(all(len(thread["frames"]) == reporter.MAX_DEPTH for thread in row["threads"]))
        with mock.patch.object(reporter, "MAX_CAPTURE_BYTES", 700):
            capped = reporter.capture(2, self.registry, {index: chain for index in range(20)}, 19)
            self.assertTrue(capped["bytes_capped"])
            self.assertLessEqual(len(reporter.encoded(capped)), 700)
            reporter.validate_report(capped)

    def test_bad_line_numbers_are_not_source_values(self):
        for line in (-1, 100001, "NEVER-EXPORT", True):
            location = reporter.safe_location(Frame("verified.py", line=line), self.registry)
            self.assertEqual(location["line"], 0)

    def test_exclusive_private_file_and_symlink_refusal(self):
        path = self.scratch / reporter.EVIDENCE
        fd = reporter.open_evidence(path)
        try:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse(os.get_inheritable(fd))
            with self.assertRaises(FileExistsError):
                reporter.open_evidence(path)
        finally:
            os.close(fd)
        target = self.scratch / "untouched"
        target.write_bytes(b"NEVER-EXPORT")
        link = self.scratch / "link"
        link.symlink_to(target)
        with self.assertRaises(FileExistsError):
            reporter.open_evidence(link)
        self.assertEqual(target.read_bytes(), b"NEVER-EXPORT")

    def test_archive_and_installed_sources_verified_with_ast_allowlist(self):
        original = self.fake_release()
        reporter.prepare(self.install, self.dist, self.scratch, self.meta)
        registry = reporter.load_registry(self.install, self.scratch / reporter.ALLOWLIST)
        relative = next(iter(reporter.SOURCES))
        info = registry[relative]
        self.assertIn("main", info["functions"])
        self.assertEqual(info["lines"], 2)
        self.assertEqual((self.install / "bin/claude-multi").read_bytes(), original)
        (self.install / relative).write_bytes(b"def private_secret():\n    pass\n")
        with self.assertRaises(ValueError):
            reporter.load_registry(self.install, self.scratch / reporter.ALLOWLIST)

    def test_wrapper_only_root_and_bootstrap_change_exports_and_argv_remain_original(self):
        original = self.fake_release()
        reporter.prepare(self.install, self.dist, self.scratch, self.meta)
        changed = (self.scratch / reporter.WRAPPER).read_bytes()
        restored = changed.decode().replace("root=" + shlex.quote(str(self.install)), reporter.ROOT_ANCHOR, 1)
        before, rest = restored.split("-c '", 1)
        code, after = rest.split("' \"$site\"", 1)
        self.assertEqual((before + "-c '" + reporter.BOOTSTRAP + "' \"$site\"" + after).encode(), original)
        self.assertLess(code.index("_m.start("), code.index("from claude_multi.entrypoints import main"))
        self.assertNotIn("run_console", code)
        self.assertNotIn("PYTHONPATH", code)
        self.assertNotIn("sitecustomize", code)
        output = self.root / "wrapper-proof"
        runtime = self.install / "runtime/python/bin/python3"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        # This is a recording shell stub, NOT a bundled Python/client execution.
        runtime.write_text("#!/bin/sh\n{ printf '%s\\n' \"$CLAUDE_MULTI_CHANNEL\" \"$CLAUDE_MULTI_ASSETS\" \"$CLAUDE_MULTI_HOOK_COMMAND\" \"$CLAUDE_MULTI_PROXY_BIN\"; printf '%s\\n' \"$@\"; } >\"$WRAPPER_TEST_OUTPUT\"\n")
        runtime.chmod(0o755)
        completed = subprocess.run(["sh", str(self.scratch / reporter.WRAPPER), "-c", "--", "-p", "journey turn two"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=5, env={"PATH": "/usr/bin:/bin", "WRAPPER_TEST_OUTPUT": str(output)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        proof = output.read_text()
        self.assertTrue(proof.startswith("bundle\n" + str(self.install / reporter.SITE / "claude_multi/data") + "\n"))
        self.assertIn(str(self.install / "bin/claude-multi") + "\n", proof)
        self.assertIn(str(self.install / "libexec/claude-multi/cli-proxy-api") + "\n", proof)
        self.assertIn("-I\n-B\n-c\n", proof)
        self.assertTrue(proof.endswith(str(self.install / reporter.SITE) + "\nclaude-multi\n-c\n--\n-p\njourney turn two\n"))

    def test_sanitized_copy_only_and_invalid_or_symlink_private_data_never_exported(self):
        self.fake_release()
        reporter.prepare(self.install, self.dist, self.scratch, self.meta)
        registry = reporter.load_registry(self.install, self.scratch / reporter.ALLOWLIST)
        relative = next(iter(reporter.SOURCES))
        row = reporter.capture(1, registry, {1: Frame(relative, "main", 1)}, 1)
        path = self.scratch / reporter.EVIDENCE
        fd = reporter.open_evidence(path)
        os.write(fd, reporter.encoded(row))
        os.close(fd)
        destination = self.meta / "locations.jsonl"
        reporter.export_reports(self.install, self.scratch, destination)
        self.assertEqual(json.loads(destination.read_text()), row)
        destination.unlink()
        path.write_bytes(b'{"private":"NEVER-EXPORT"')
        with self.assertRaises(ValueError):
            reporter.export_reports(self.install, self.scratch, destination)
        self.assertFalse(destination.exists())
        path.unlink()
        path.symlink_to(self.scratch / reporter.ALLOWLIST)
        with self.assertRaises(ValueError):
            reporter.export_reports(self.install, self.scratch, destination)
        self.assertFalse(destination.exists())

    def test_missing_or_empty_capture_has_no_inferred_cause(self):
        destination = self.meta / "locations.jsonl"
        reporter.export_reports(self.install, self.scratch, destination)
        fd = reporter.open_evidence(self.scratch / reporter.EVIDENCE)
        os.close(fd)
        reporter.export_reports(self.install, self.scratch, destination)
        self.assertFalse(destination.exists())

    def test_start_is_best_effort_silent_and_one_daemon_after_private_open(self):
        self.fake_release()
        reporter.prepare(self.install, self.dist, self.scratch, self.meta)
        with mock.patch.object(reporter.threading, "Thread") as thread:
            self.assertTrue(reporter.start(str(self.install), str(self.scratch / reporter.ALLOWLIST),
                                           str(self.scratch / reporter.EVIDENCE), str(self.meta)))
            self.assertTrue(thread.call_args.kwargs["daemon"])
            fd = thread.call_args.kwargs["args"][0]
            self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o600)
            self.assertFalse(os.get_inheritable(fd))
            thread.return_value.start.assert_called_once()
            os.close(fd)
            self.assertFalse(reporter.start(str(self.install), str(self.scratch / reporter.ALLOWLIST),
                                            str(self.scratch / reporter.EVIDENCE), str(self.meta)))
            self.assertEqual(thread.call_count, 1)

    def test_poll_allows_only_two_captures_and_total_bytes_remain_bounded(self):
        request = self.meta / reporter.REQUEST
        request.write_bytes(reporter.encoded({"schema": 1, "mode": reporter.MODE, "capture": 1}))
        def advance(_seconds):
            request.write_bytes(reporter.encoded({"schema": 1, "mode": reporter.MODE, "capture": 2}))
        path = self.scratch / reporter.EVIDENCE
        fd = reporter.open_evidence(path)
        with mock.patch.object(reporter.sys, "_current_frames", return_value={1: Frame("verified.py")}), \
             mock.patch.object(reporter.threading, "main_thread", return_value=SimpleNamespace(ident=1)), \
             mock.patch.object(reporter.time, "sleep", side_effect=advance):
            reporter.poll(fd, self.registry, request)
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        self.assertEqual([row["capture"] for row in rows], [1, 2])
        self.assertLessEqual(path.stat().st_size, reporter.MAX_TOTAL_BYTES)
        for row in rows:
            reporter.validate_report(row)
        request.write_bytes(reporter.encoded({"schema": 1, "mode": reporter.MODE, "capture": 1}))
        limited = self.scratch / "limited.raw.jsonl"
        fd = reporter.open_evidence(limited)
        with mock.patch.object(reporter.sys, "_current_frames", return_value={1: Frame("verified.py")}), \
             mock.patch.object(reporter.threading, "main_thread", return_value=SimpleNamespace(ident=1)), \
             mock.patch.object(reporter.time, "sleep", side_effect=advance), \
             mock.patch.object(reporter, "MAX_TOTAL_BYTES", len(reporter.encoded(rows[0]))):
            reporter.poll(fd, self.registry, request)
        self.assertEqual(len(limited.read_bytes().splitlines()), 1)

    def test_no_signal_tracing_locals_source_line_or_exception_rendering(self):
        source = (SCRIPTS / "resume_locations.py").read_text()
        tree = ast.parse(source)
        forbidden = {"f_locals", "f_globals", "co_varnames", "co_consts", "format_exc", "print_exception"}
        self.assertFalse(any(isinstance(node, ast.Attribute) and node.attr in forbidden for node in ast.walk(tree)))
        for name in ("SIGUSR1", "faulthandler", "ptrace", "linecache", "traceback", "repr(", "print("):
            self.assertNotIn(name, source.replace("raw traceback", "raw report"))
        self.assertIn("sys._current_frames()", source)
