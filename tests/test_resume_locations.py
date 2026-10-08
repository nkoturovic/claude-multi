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
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from _layout import REPO_ROOT
from tests.test_wsl_diag import artifact, diag, watchdog

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

    def fake_release(self, overrides=None):
        original = bundle.launcher_text("claude-multi", "3.14.8")
        files = {relative: b"def main():\n    return None\n" for relative in reporter.SOURCES}
        files.update(overrides or {})
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

    def upload_inputs(self, *rows):
        diag.atomic_json(self.meta / "artifact.json", artifact())
        diag.atomic_json(self.meta / "watchdog.json", watchdog())
        (self.meta / "locations.jsonl").write_bytes(b"".join(reporter.encoded(row) for row in rows))

    def assert_locations_omitted(self, destination):
        self.assertEqual(json.loads((destination / "artifact.json").read_text()), artifact())
        self.assertEqual(json.loads((destination / "watchdog.json").read_text()), watchdog())
        self.assertFalse((destination / "locations.jsonl").exists())
        self.assertEqual(json.loads((destination / "validation.json").read_text()), {
            "schema": 1, "optional_metadata_valid": False,
            "omitted": [{"file": "locations.jsonl", "reason": "invalid-metadata"}]})
        with self.assertRaises(ValueError):
            diag.check_export(destination)

    def test_final_export_rejects_private_function_and_ignores_writable_allowlist(self):
        self.fake_release()
        relative = next(iter(reporter.SOURCES))
        good = reporter.capture(1, {relative: self.registry["verified.py"]}, {1: Frame(relative)}, 1)
        bad = json.loads(reporter.encoded(good))
        bad["capture"] = 2
        sentinel = "PRIVATE_SENTINEL_NotAPublicFunction"
        bad["threads"][0]["frames"][0]["function"] = sentinel
        reporter.validate_report(bad) # Structurally valid; the final trust check must still reject it.
        self.upload_inputs(good, bad)
        (self.meta / reporter.ALLOWLIST).write_text(json.dumps({self.source: {"functions": [sentinel]}}))
        destination = self.root / "validated"
        with mock.patch.object(diag.locations, "read_regular", wraps=diag.locations.read_regular) as read:
            diag.validate_directory(self.meta, destination, self.dist)
        self.assert_locations_omitted(destination)
        self.assertTrue(read.called)
        self.assertTrue(all(not call.args[0].resolve().is_relative_to(self.meta) for call in read.call_args_list))
        self.assertNotIn(sentinel, "".join(path.read_text() for path in destination.iterdir()))
        # Exercise the actual final CLI, including its independent original-dist argument.
        output = self.root / "cli-validated"
        exported = subprocess.run([sys.executable, str(SCRIPTS / "wsl_diag.py"), "validate",
                                   str(self.meta), str(output), str(self.dist)],
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assert_locations_omitted(output)
        checked = subprocess.run([sys.executable, str(SCRIPTS / "wsl_diag.py"), "check-export", str(output)],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertEqual(checked.returncode, 1)
        self.assertNotIn(sentinel, exported.stdout + exported.stderr + checked.stdout + checked.stderr
                         + "".join(path.read_text() for path in output.iterdir()))

    def test_final_export_accepts_exact_linux_bundle_functions_and_fixed_generalizations(self):
        runtime = f"{reporter.RUNTIME}/threading.py"
        self.fake_release({runtime: b"def linux_bundle_only():\n    return None\n"})
        reporter.prepare(self.install, self.dist, self.scratch, self.meta)
        registry = reporter.load_registry(self.install, self.scratch / reporter.ALLOWLIST)
        relative = next(iter(reporter.SOURCES))
        frame = Frame(relative, parent=Frame(runtime, "linux_bundle_only", parent=
            Frame("/private/NEVER-EXPORT.py", "PRIVATE_SENTINEL", parent=
                  Frame(relative, "PRIVATE_SENTINEL", parent=Frame(str(SCRIPTS / "resume_locations.py"), "capture")))))
        row = reporter.capture(1, registry, {1: frame}, 1)
        self.upload_inputs(row)
        destination = self.root / "validated"
        diag.validate_directory(self.meta, destination, self.dist)
        self.assertEqual(json.loads((destination / "locations.jsonl").read_text()), row)
        self.assertEqual([item["function"] for item in row["threads"][0]["frames"]],
                         ["main", "linux_bundle_only", "other", "other", "capture"])
        self.assertNotIn("PRIVATE_SENTINEL", (destination / "locations.jsonl").read_text())
        diag.check_export(destination)

    def test_final_export_rejects_function_allowed_only_under_another_source(self):
        runtime = f"{reporter.RUNTIME}/threading.py"
        self.fake_release({runtime: b"def linux_bundle_only():\n    return None\n"})
        row = reporter.capture(1, self.registry, {1: Frame("verified.py")}, 1)
        row["threads"][0]["frames"][0]["function"] = "linux_bundle_only"
        self.upload_inputs(row)
        destination = self.root / "validated"
        diag.validate_directory(self.meta, destination, self.dist)
        self.assert_locations_omitted(destination)

    def test_missing_corrupt_or_metadata_local_trust_preserves_mandatory_evidence(self):
        self.fake_release()
        row = reporter.capture(1, self.registry, {1: Frame("verified.py")}, 1)
        self.upload_inputs(row)
        for index, trust in enumerate((None, self.root / "missing", self.meta)):
            with self.subTest(trust=index):
                destination = self.root / f"validated-{index}"
                diag.validate_directory(self.meta, destination, trust)
                self.assert_locations_omitted(destination)
        archive = next(self.dist.glob("*.tar.gz"))
        archive.write_bytes(archive.read_bytes() + b"changed")
        destination = self.root / "validated-corrupt"
        diag.validate_directory(self.meta, destination, self.dist)
        self.assert_locations_omitted(destination)

    def test_upload_trust_reader_uses_bundle_sources_without_linux_only_open_flags(self):
        self.fake_release()
        reporter.prepare(self.install, self.dist, self.scratch, self.meta)
        registry = reporter.load_registry(self.install, self.scratch / reporter.ALLOWLIST)
        with mock.patch.object(reporter, "os", SimpleNamespace(O_RDONLY=os.O_RDONLY, open=os.open,
                                                              fdopen=os.fdopen, fstat=os.fstat)):
            sources = reporter.trusted_sources(self.dist)
        for relative, source in reporter.SOURCES.items():
            self.assertEqual(sources[source], registry[relative])

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
