"""Host-only tests for the temporary diagnostic harness. No real clients/WSL."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from _layout import REPO_ROOT

PLATFORMS = ("linux",)
SCRIPTS = REPO_ROOT / ".github/scripts"
SPEC = importlib.util.spec_from_file_location("wsl_diag", SCRIPTS / "wsl_diag.py")
diag = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diag)
WORKFLOW = REPO_ROOT / ".github/workflows/release.yml"


def artifact() -> dict:
    return {"schema": 1, "repository": "nkoturovic/claude-multi", "run_id": diag.RUN,
            "artifact_id": diag.ARTIFACT_ID, "name": "dist", "size_bytes": 204873528,
            "digest": diag.ARTIFACT_DIGEST, "head_sha": diag.SHA}


def watchdog() -> dict:
    return {"schema": 1, "mode": diag.MODE, "status": "resume-timeout", "elapsed_ms": 130000, "armed_at_ms": 0,
            "arm_basis": "first-fixture-request", "launcher_exit_code": None,
            "capture_confirmed": False, "cleanup_confirmed": False, "terminate_state": "timeout"}


def hosted_ci() -> dict:
    return {"schema": 1, "mode": diag.MODE, **{name: True for name in
        "github_hosted_windows windows_worker distribution_was_absent native_home_clean credential_environment_clear".split()}}


def preflight() -> dict:
    return {"schema": 1, "mode": diag.MODE, **{name: True for name in
        "ordinary_user fresh_home linux_filesystem provider_auth_empty credential_environment_clear verified_release verified_client candidate_windows_cwd".split()}}


def process() -> dict:
    return {"pid": 42, "ppid": 1, "pgid": 42, "start_ticks": 100,
            "state": "S", "exe": "claude", "wchan": "locks_lock_inode_wait"}


class ScratchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.meta = self.root / "metadata"
        self.meta.mkdir()
        self.output = self.root / "validated"
        diag.atomic_json(self.meta / "artifact.json", artifact())
        diag.atomic_json(self.meta / "watchdog.json", watchdog())


class SourcePatchTests(ScratchTests):
    def test_all_exact_anchors_are_unique_and_required(self) -> None:
        for name, patches in (("journey.sh", diag.JOURNEY_PATCHES), ("journey_fixture.py", diag.FIXTURE_PATCHES)):
            text = (SCRIPTS / name).read_text()
            for old, _new in patches:
                with self.subTest(name=name, anchor=old.splitlines()[0]):
                    self.assertEqual(text.count(old), 1)
                    for changed in (text.replace(old, "", 1), text + "\n" + old):
                        with self.assertRaises(ValueError):
                            diag.patch_text(changed, patches)

    def test_scratch_only_preserves_reply_checks_and_stops_before_controls(self) -> None:
        originals = {name: (SCRIPTS / name).read_bytes() for name in ("journey.sh", "journey_fixture.py")}
        diag.patch(SCRIPTS, self.root)
        journey = (self.root / "journey.sh").read_text()
        fixture = (self.root / "journey_fixture.py").read_text()
        self.assertNotIn("tail -n 20", journey)
        self.assertNotIn("trap cleanup EXIT", journey)
        self.assertIn("trap ':' EXIT", journey)
        self.assertIn('show_tail() { return 0; }', journey)
        expected_resume = diag.RESUME_ANCHOR.replace('"$cm" -c', '"$HOME/diag/claude-multi-resume-diag" -c', 1)
        self.assertIn(expected_resume, journey)
        self.assertNotIn('wsl_diag.py" resume', journey)
        self.assertIn('first=$(reply_number "$work/turn1.txt")', journey)
        self.assertIn('second=$(reply_number "$work/turn2.txt")', journey)
        self.assertIn('fx carried --log "$work/fixture.log" --reply "$second" --earlier "$first" ||', journey)
        self.assertLess(journey.index('wsl_diag.py" outcome'), journey.index('say "resumed managed session: ok'))
        self.assertIn('managed_turn\n\t\t\texit 0', journey)
        self.assertLess(journey.index('exit 0 # Namespace exit'), journey.index('"$cm" gateway stop ||'))
        self.assertIn('wsl_diag.fixture_record(record)', fixture)
        self.assertLess(fixture.index('_log(self.log_path, {"method": "POST", "path": self.path, "model"'),
                        fixture.index('reply = REPLY.format(n=number)'))
        compile(fixture, "scratch fixture", "exec")
        parsed = subprocess.run(["sh", "-n", str(self.root / "journey.sh")], capture_output=True, timeout=5)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        for name, raw in originals.items():
            self.assertEqual((SCRIPTS / name).read_bytes(), raw)

    def test_only_resume_executable_changes_other_inter_turn_bytes_are_exact(self) -> None:
        original = (SCRIPTS / "journey.sh").read_bytes()
        diag.patch(SCRIPTS, self.root)
        patched = (self.root / "journey.sh").read_bytes()
        first = b'\tfirst=$(reply_number "$work/turn1.txt")'
        second = b'\tsecond=$(reply_number "$work/turn2.txt")'
        block = original[original.index(first):original.index(second) + len(second)]
        expected = block.replace(b'"$cm" -c', b'"$HOME/diag/claude-multi-resume-diag" -c', 1)
        self.assertEqual(patched.count(expected), 1)
        self.assertEqual(block.count(b'"$cm" -c'), 1)
        self.assertNotIn(b'wsl_diag.py" resume', patched)
        self.assertTrue(any(old == diag.RESUME_ANCHOR for old, _ in diag.JOURNEY_PATCHES))
        first_turn = b'(cd "$work/project" && "$cm" direct --model custom-journey-fixture -- -p "journey turn one")'
        self.assertEqual(original.count(first_turn), 1)
        self.assertEqual(patched.count(first_turn), 1)
        # This byte span includes first-turn extraction/completion, the original
        # foreground argv/CWD/redirections/null stdin, and resume failure handling.

    def test_pristine_product_goldens_remain_unchanged(self) -> None:
        # tools/test.py supplies this subprocess a private HOME and tripwire.
        checked = subprocess.run([sys.executable, str(REPO_ROOT / "tests/bless.py"), "--check"],
                                 cwd=REPO_ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_wrong_source_hash_refuses_before_any_patch_write(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "journey.sh").write_bytes((SCRIPTS / "journey.sh").read_bytes() + b"\n")
        (source / "journey_fixture.py").write_bytes((SCRIPTS / "journey_fixture.py").read_bytes())
        with self.assertRaises(ValueError):
            diag.patch(source, self.output)
        self.assertFalse(self.output.exists())


class MetadataTests(ScratchTests):
    def test_fixture_records_are_allowlisted_and_pre_response_only(self) -> None:
        row = diag.fixture_record({"method": "POST", "path": "/v1/chat/completions", "model": "fixture-model-1",
            "stream": True, "reply": 2, "carried": [1], "prompt": "never-export", "Authorization": "never-export"})
        self.assertEqual(row, {"event": "request-parsed", "method": "POST", "path_category": "chat-completions",
                              "model": "fixture-model-1", "stream": True, "reply": 2, "carried": [1]})
        self.assertNotIn("never-export", json.dumps(row))
        other = diag.fixture_record({"method": "GET", "path": "/private?token=never-export"})
        self.assertEqual(other["path_category"], "other")
        self.assertNotIn("never-export", json.dumps(other))
        with self.assertRaises(ValueError):
            diag.fixture_record({"method": "POST", "path": "/v1/chat/completions", "model": "never-export"})

    def test_fixture_records_parsed_request_even_when_response_write_fails(self) -> None:
        diag.patch(SCRIPTS, self.root)
        spec = importlib.util.spec_from_file_location("scratch_fixture", self.root / "journey_fixture.py")
        fixture = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"wsl_diag": diag}), mock.patch.object(sys, "path", list(sys.path)):
            spec.loader.exec_module(fixture)
        handler = object.__new__(fixture._Chat)
        request = {"model": "fixture-model-1", "stream": True, "messages": [
            {"role": "user", "content": "raw-prompt-never-export"},
            {"role": "assistant", "content": "fixture reply 1"}]}
        payload = json.dumps(request).encode()
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.path = "/v1/chat/completions"
        handler.log_path = self.root / "fixture.log"
        handler.counter = [1]
        handler.lock = threading.Lock()
        handler._send = mock.Mock(side_effect=BrokenPipeError("synthetic response failure"))
        with mock.patch.dict(os.environ, {"CM_DIAG_METADATA": str(self.meta)}), self.assertRaises(BrokenPipeError):
            handler.do_POST()
        row = json.loads((self.meta / "fixture.jsonl").read_text())
        self.assertEqual(row["event"], "request-parsed")
        self.assertEqual(row["reply"], 2)
        self.assertEqual(row["carried"], [1])
        self.assertNotIn("raw-prompt", json.dumps(row))
        self.assertEqual(fixture.carried(handler.log_path, 2, 1), 0)
        # Carried metadata is only the request; the preserved journey also
        # requires reply extraction from the client's local output to pass.
        diag.validate_directory(self.meta, self.output)

    def test_concurrent_fixture_appends_respect_the_bound(self) -> None:
        row = diag.fixture_record({"method": "GET", "path": "/v1/models"})
        path = self.meta / "fixture.jsonl"
        barrier = threading.Barrier(10)
        succeeded = []
        def append():
            barrier.wait(timeout=5)
            try:
                diag.append_json(path, row, 5 * len(diag.encoded(row)))
                succeeded.append(True)
            except ValueError:
                pass
        threads = [threading.Thread(target=append) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(succeeded), 5)
        self.assertEqual(path.read_bytes(), diag.encoded(row) * 5)

    def test_only_explicit_metadata_files_are_read_and_rewritten(self) -> None:
        (self.meta / "raw-turn.txt").write_text("never-export")
        (self.meta / "home").mkdir()
        (self.meta / "home/credential").write_text("never-export")
        row = diag.fixture_record({"method": "GET", "path": "/v1/models"})
        diag.append_json(self.meta / "fixture.jsonl", row, diag.LIMITS["fixture.jsonl"])
        with mock.patch.object(diag, "safe_read", wraps=diag.safe_read) as read:
            diag.validate_directory(self.meta, self.output)
        self.assertEqual({call.args[0].name for call in read.call_args_list}, set(diag.LIMITS))
        self.assertEqual({p.name for p in self.output.iterdir()},
                         {"artifact.json", "watchdog.json", "fixture.jsonl", "validation.json"})
        self.assertNotIn("never-export", "".join(p.read_text() for p in self.output.iterdir()))
        self.assertEqual(json.loads((self.output / "artifact.json").read_text()), artifact())
        diag.check_export(self.output)

    def test_unknown_keys_raw_paths_wrong_types_and_model_are_rejected(self) -> None:
        good = {"schema": 1, "mode": diag.MODE, "end_captured": True}
        bad_rows = [dict(good, argv="private"), dict(good, pid=True), dict(good, end_captured="true"),
                    dict(good, mode="private-namespace"), dict(good, schema=True)]
        for row in bad_rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                diag.validate_record("observer.json", row)
        snapshot = {"event": "periodic", "elapsed_ms": 3, "processes": [process()], "locks": [],
                    "processes_truncated": False, "locks_available": True, "locks_truncated": False}
        for field, value in (("exe", "/home/private/client"), ("wchan", "raw-unknown-symbol"),
                             ("state", "not-a-kernel-state")):
            changed = {**snapshot, "processes": [{**process(), field: value}]}
            with self.subTest(field=field), self.assertRaises(ValueError):
                diag.validate_record("snapshots.jsonl", changed)
        row = diag.fixture_record({"method": "GET", "path": "/v1/models"})
        for field, value in (("carried", [True]), ("carried", list(range(1, 130))), ("model", "other"),
                             ("stream", "false"), ("reply", -1), ("event", "delivered")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                diag.validate_record("fixture.jsonl", {**row, field: value})

    def test_bad_mandatory_json_and_oversize_reject_without_sanitized_output(self) -> None:
        for raw in (b'{"schema":1,"schema":1}', b'{"schema":NaN}', b"{not json", b"x" * 2049):
            with self.subTest(raw=raw[:30]):
                (self.meta / "watchdog.json").write_bytes(raw)
                with self.assertRaises(ValueError):
                    diag.validate_directory(self.meta, self.output)
                self.assertFalse(self.output.exists())

    def test_mandatory_symlinks_directories_and_fifos_are_not_opened(self) -> None:
        path = self.meta / "watchdog.json"
        path.unlink()
        path.symlink_to(self.meta / "artifact.json")
        with self.assertRaises(ValueError):
            diag.validate_directory(self.meta, self.output)
        path.unlink()
        path.mkdir()
        with self.assertRaises(ValueError):
            diag.validate_directory(self.meta, self.output)
        path.rmdir()
        if hasattr(os, "mkfifo"):
            os.mkfifo(path)
            with self.assertRaises(ValueError):
                diag.validate_directory(self.meta, self.output)

    def test_bounded_jsonl_and_stale_upload_destinations_refuse(self) -> None:
        row = diag.fixture_record({"method": "GET", "path": "/v1/models"})
        path = self.meta / "fixture.jsonl"
        diag.append_json(path, row, len(diag.encoded(row)))
        with self.assertRaises(ValueError):
            diag.append_json(path, row, len(diag.encoded(row)))
        path.write_bytes(diag.encoded(row) * 256)
        with self.assertRaises(ValueError):
            diag.append_json(path, row, diag.LIMITS["fixture.jsonl"])
        self.assertEqual(len(path.read_bytes().splitlines()), 256)
        path.write_bytes(b'{"event":')  # The producer must not append onto a torn record.
        with self.assertRaises(ValueError):
            diag.append_json(path, row, diag.LIMITS["fixture.jsonl"])
        path.write_bytes(diag.encoded(row))
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            diag.validate_directory(self.meta, self.output)

    def test_lock_metadata_maps_only_known_stat_identities(self) -> None:
        raw = """1: FLOCK ADVISORY WRITE 42 00:29:123 0 EOF
1: -> FLOCK ADVISORY WRITE 43 00:29:123 0 EOF
2: POSIX ADVISORY READ 99 00:29:999 0 EOF
3: OFDLCK ADVISORY READ -1 00:29:124 0 EOF
"""
        rows, truncated = diag.lock_metadata(raw, {(0, 0x29, 123): "lifecycle", (0, 0x29, 124): "client-use"})
        self.assertFalse(truncated)
        self.assertEqual([row["role"] for row in rows], ["holder", "waiter", "holder"])
        self.assertEqual([row["pid"] for row in rows], [42, 43, -1])
        self.assertEqual(rows[0]["inode"], 123)
        self.assertNotIn(999, [row["inode"] for row in rows])
        many, truncated = diag.lock_metadata(raw * 65, {(0, 0x29, 123): "lifecycle"})
        self.assertTrue(truncated)
        self.assertEqual(len(many), 64)

    def test_known_lock_mapping_never_opens_product_files(self) -> None:
        state = self.root / ".local/state/claude-multi"
        (state / "locks").mkdir(parents=True)
        lock = state / "locks/12345678-1234-4234-8234-123456789012.lifecycle.lock"
        lock.write_text("not-to-be-read")
        (state / "locks/unknown.txt").write_text("not-to-be-read")
        with mock.patch("builtins.open", side_effect=AssertionError("opened product file")):
            found = diag.known_locks(self.root)
        info = lock.stat()
        self.assertEqual(found, {(os.major(info.st_dev), os.minor(info.st_dev), info.st_ino): "lifecycle"})

    def test_process_metadata_ignores_comm_and_never_reads_argv_env_or_stacks(self) -> None:
        proc = self.root / "proc"
        directory = proc / "42"
        directory.mkdir(parents=True)
        fields = ["S", "1", "42"] + ["0"] * 16 + ["123"]
        (directory / "stat").write_text("42 (never-export )) " + " ".join(fields))
        (directory / "wchan").write_text("locks_lock_inode_wait")
        (directory / "exe").symlink_to("/usr/bin/python3")
        def read_text(path, *args, **kwargs):
            with io.open(path, *args, **kwargs) as handle:
                return handle.read()
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=read_text) as read:
            row = diag.process_metadata(42, proc)
        self.assertEqual(row["start_ticks"], 123)
        self.assertEqual(row["exe"], "python3")
        self.assertEqual(row["wchan"], "locks_lock_inode_wait")
        self.assertEqual({call.args[0].name for call in read.call_args_list}, {"stat", "wchan"})
        self.assertNotIn("never-export", json.dumps(row))
        (directory / "wchan").write_text("never-export")
        self.assertEqual(diag.process_metadata(42, proc)["wchan"], "other")


class ExportTests(ScratchTests):
    def assert_timeout_evidence_preserved(self) -> None:
        diag.atomic_json(self.meta / "outcome.json",
                         {"schema": 1, "mode": diag.MODE, "managed_turn_passed": False, "journey_exit_code": 1})
        diag.validate_directory(self.meta, self.output)
        self.assertEqual(json.loads((self.output / "artifact.json").read_text()), artifact())
        self.assertEqual(json.loads((self.output / "watchdog.json").read_text()), watchdog())
        self.assertFalse(json.loads((self.output / "outcome.json").read_text())["managed_turn_passed"])
        self.assertFalse((self.output / "fixture.jsonl").exists())
        self.assertEqual(json.loads((self.output / "validation.json").read_text()),
                         {"schema": 1, "optional_metadata_valid": False,
                          "omitted": [{"file": "fixture.jsonl", "reason": "invalid-metadata"}]})
        self.assertNotIn("NEVER-EXPORT", "".join(p.read_text() for p in self.output.iterdir()))
        with self.assertRaises(ValueError):
            diag.check_export(self.output)

    def test_resume_timeout_watchdog_survives_257_valid_fixture_records(self) -> None:
        row = diag.fixture_record({"method": "POST", "path": "/v1/chat/completions", "model": "fixture-model-1",
                                   "stream": True, "reply": 2, "carried": [1]})
        raw = diag.encoded(row) * 257
        self.assertLess(len(raw), diag.LIMITS["fixture.jsonl"])
        (self.meta / "fixture.jsonl").write_bytes(raw)
        self.assert_timeout_evidence_preserved()

    def test_resume_timeout_watchdog_survives_truncated_final_jsonl(self) -> None:
        row = diag.fixture_record({"method": "GET", "path": "/v1/models"})
        (self.meta / "fixture.jsonl").write_bytes(diag.encoded(row) + b'{"event":"NEVER-EXPORT"')
        self.assert_timeout_evidence_preserved()

    def test_bad_optional_locations_do_not_suppress_mandatory_watchdog(self) -> None:
        (self.meta / "locations.jsonl").write_bytes(b'{"private":"NEVER-EXPORT"')
        diag.validate_directory(self.meta, self.output)
        self.assertEqual(json.loads((self.output / "watchdog.json").read_text()), watchdog())
        self.assertEqual(json.loads((self.output / "artifact.json").read_text()), artifact())
        self.assertFalse((self.output / "locations.jsonl").exists())
        self.assertEqual(json.loads((self.output / "validation.json").read_text())["omitted"],
                         [{"file": "locations.jsonl", "reason": "invalid-metadata"}])
        with self.assertRaises(ValueError):
            diag.check_export(self.output)
        self.assertNotIn("NEVER-EXPORT", "".join(p.read_text() for p in self.output.iterdir()))

    def test_mandatory_invalid_or_missing_artifact_and_watchdog_fail_closed(self) -> None:
        (self.meta / "fixture.jsonl").write_bytes(b'{"event":"NEVER-EXPORT"')
        for name in diag.MANDATORY:
            path = self.meta / name
            original = path.read_bytes()
            with self.subTest(name=name, kind="invalid"):
                path.write_bytes(diag.encoded({**json.loads(original), "raw": "NEVER-EXPORT"}))
                with self.assertRaises(ValueError):
                    diag.validate_directory(self.meta, self.output)
                self.assertFalse(self.output.exists())
            with self.subTest(name=name, kind="missing"):
                path.unlink()
                with self.assertRaises(FileNotFoundError):
                    diag.validate_directory(self.meta, self.output)
                self.assertFalse(self.output.exists())
            path.write_bytes(original)

    def test_jsonl_producers_and_validator_share_record_limits(self) -> None:
        fixture = diag.fixture_record({"method": "GET", "path": "/v1/models"})
        snapshot = {"event": "periodic", "elapsed_ms": 0, "processes": [], "locks": [],
                    "processes_truncated": False, "locks_available": True, "locks_truncated": False}
        for filename, name, row in (("fixture.jsonl", "fixture.jsonl", fixture),
                                    ("fixture.log", "fixture.jsonl", fixture),
                                    ("snapshots.jsonl", "snapshots.jsonl", snapshot)):
            with self.subTest(filename=filename):
                path = self.meta / filename
                maximum = diag.RECORD_LIMITS[name]
                raw = diag.encoded(row)
                path.write_bytes(raw * (maximum - 1))
                diag.append_json(path, row, diag.LIMITS[name])
                self.assertEqual(diag.validated_bytes(path, name), raw * maximum)
                with self.assertRaises(ValueError):
                    diag.append_json(path, row, diag.LIMITS[name])
                self.assertEqual(path.read_bytes(), raw * maximum)

    def test_export_and_job_integrity_have_separate_cli_outcomes(self) -> None:
        (self.meta / "fixture.jsonl").write_bytes(b'{"event":"NEVER-EXPORT"')
        exported = subprocess.run([sys.executable, str(SCRIPTS / "wsl_diag.py"), "validate",
                                   str(self.meta), str(self.output)], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=5)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertEqual(exported.stdout + exported.stderr, "")
        checked = subprocess.run([sys.executable, str(SCRIPTS / "wsl_diag.py"), "check-export", str(self.output)],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertEqual(checked.returncode, 1)
        self.assertNotIn("NEVER-EXPORT", checked.stdout + checked.stderr)
        self.assertEqual(json.loads((self.output / "watchdog.json").read_text()), watchdog())

    def test_omission_report_accepts_only_fixed_file_identities_and_reason(self) -> None:
        good = {"schema": 1, "optional_metadata_valid": False,
                "omitted": [{"file": "fixture.jsonl", "reason": "invalid-metadata"}]}
        diag.validate_record("validation.json", good)
        for omission in ({"file": "raw-turn.txt", "reason": "invalid-metadata"},
                         {"file": "fixture.jsonl", "reason": "NEVER-EXPORT"}):
            with self.subTest(omission=omission), self.assertRaises(ValueError):
                diag.validate_record("validation.json", {**good, "omitted": [omission]})
        with self.assertRaises(ValueError):
            diag.validate_record("validation.json", {**good, "optional_metadata_valid": True})


class HostedBaselineTests(ScratchTests):
    def test_honest_mode_and_cleanup_confirmation_only_from_termination(self) -> None:
        self.assertEqual(diag.MODE, "hosted-disposable-wsl-fixture-only")
        diag.validate_record("watchdog.json", watchdog())
        for changed in ({**watchdog(), "namespace_exit_confirmed": True},
                        {**watchdog(), "net_private": True}, {**watchdog(), "cleanup_confirmed": True},
                        {**watchdog(), "mode": "private-namespace"}):
            with self.assertRaises(ValueError):
                diag.validate_record("watchdog.json", changed)
        diag.validate_record("watchdog.json", {**watchdog(), "terminate_state": "returned", "cleanup_confirmed": True})
        self.assertFalse(any(name in diag.LIMITS for name in
                             ("containment.json", "namespace.json", "resume-start.json", "resume-end.json")))
        diag.validate_directory(self.meta, self.output)
        diag.check_export(self.output)

    def test_names_only_guard_rejects_credentials_provider_bypasses_and_state_selectors(self) -> None:
        class NamesOnly(dict):
            def __getitem__(self, name):
                raise AssertionError("environment value read")
        allowed = NamesOnly.fromkeys(("PATH", "TMPDIR", "CLAUDE_CODE_TMPDIR", "WSL_DISTRO_NAME",
                                      "WSL_INTEROP", "WSLENV", "XDG_RUNTIME_DIR"))
        diag.admit_hosted_environment(allowed)
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN", "AWS_SECRET_ACCESS_KEY",
                     "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                     "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                     "CLAUDE_CONFIG_DIR", "CLAUDE_MULTI_ASSETS", "XDG_STATE_HOME", "HTTP_PROXY",
                     "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_DISABLE_FAST_MODE", "PGSTORE_DSN",
                     "GITSTORE_GIT_URL", "OBJECTSTORE_ENDPOINT", "DEPLOY", "WRITABLE_PATH", "META_MINT_URL"):
            environment = NamesOnly({name: "NEVER-EXPORT"})
            with self.subTest(name=name), self.assertRaises(diag.HostedEnvironmentRefused) as caught:
                diag.admit_hosted_environment(environment)
            self.assertEqual(str(caught.exception), diag.ENV_REFUSAL)
            self.assertEqual(set(environment), {name})

    def test_provider_auth_directory_is_checked_by_names_stat_never_contents(self) -> None:
        home = self.root / "home"
        home.mkdir()
        auth = home / ".local/share/claude-multi/auth"
        auth.mkdir(parents=True)
        with mock.patch.object(diag, "linux_filesystem"), mock.patch("builtins.open", side_effect=AssertionError("config read")):
            diag.fresh_home(home)
        credential = auth / "fixture-account.json"
        credential.write_text("NEVER-EXPORT")
        with mock.patch.object(diag, "linux_filesystem"), mock.patch("builtins.open", side_effect=AssertionError("credential read")):
            with self.assertRaises(ValueError):
                diag.fresh_home(home)
        credential.unlink()
        auth.rmdir()
        auth.symlink_to(home, target_is_directory=True)
        with self.assertRaises(ValueError):
            diag.fresh_home(home)

    def test_stale_operator_configuration_or_session_names_are_not_imported(self) -> None:
        for relative in (".config/claude-multi/endpoint.json", ".config/claude-multi/profiles/profile.json",
                         ".local/state/claude-multi/sessions/record.json", ".claude.json"):
            with self.subTest(relative=relative):
                home = self.root / relative.replace("/", "_")
                path = home / relative
                path.parent.mkdir(parents=True)
                path.write_text("NEVER-EXPORT")
                with mock.patch.object(diag, "linux_filesystem"), \
                     mock.patch("builtins.open", side_effect=AssertionError("operator state read")):
                    with self.assertRaises(ValueError):
                        diag.fresh_home(home)

    def test_local_or_unproven_hosted_execution_refuses_before_any_client(self) -> None:
        with mock.patch.object(diag.platform, "release", return_value="ordinary-linux"), \
             mock.patch.object(diag.subprocess, "Popen") as client, mock.patch.object(diag, "verify_client") as pin:
            with self.assertRaises(ValueError):
                diag.hosted_preflight(self.meta, Path("/mnt/d/a/repo/candidate"))
            client.assert_not_called()
            pin.assert_not_called()
        diag.atomic_json(self.meta / "hosted-ci.json", hosted_ci())
        bad = {**hosted_ci(), "distribution_was_absent": False}
        (self.meta / "hosted-ci.json").write_text(json.dumps(bad))
        with mock.patch.object(diag.platform, "release", return_value="microsoft-standard-WSL2"), \
             mock.patch.object(diag.subprocess, "Popen") as client:
            with self.assertRaises(ValueError):
                diag.hosted_preflight(self.meta, Path("/mnt/d/a/repo/candidate"))
            client.assert_not_called()

    def test_all_preflight_guards_and_hashes_precede_foreground_admission(self) -> None:
        workspace = Path("/mnt/d/a/repo/candidate")
        diag.atomic_json(self.meta / "hosted-ci.json", hosted_ci())
        order = []
        with mock.patch.object(diag.platform, "release", return_value="microsoft-standard-WSL2"), \
             mock.patch.object(diag.os, "geteuid", return_value=1000), \
             mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_name="journey")), \
             mock.patch.object(Path, "home", return_value=Path("/home/journey")), \
             mock.patch.object(Path, "cwd", return_value=workspace), \
             mock.patch.dict(os.environ, {"WSL_INTEROP": "/run/WSL/fixture_interop"}, clear=True), \
             mock.patch.object(diag, "fresh_home", side_effect=lambda _: order.append("fresh-home")), \
             mock.patch.object(diag, "verify_dist", side_effect=lambda *a: order.append("bundle-hash")), \
             mock.patch.object(diag, "verify_client", side_effect=lambda *a: order.append("client-hash")), \
             mock.patch.object(diag.subprocess, "Popen", side_effect=AssertionError("client launched")):
            diag.hosted_preflight(self.meta, workspace)
        self.assertEqual(order, ["fresh-home", "bundle-hash", "client-hash"])
        self.assertEqual(json.loads((self.meta / "preflight.json").read_text()), preflight())
        self.assertFalse(json.loads((self.meta / "outcome.json").read_text())["managed_turn_passed"])

    def test_credential_guard_fails_before_hashes_or_client_and_never_unsets(self) -> None:
        workspace = Path("/mnt/d/a/repo/candidate")
        diag.atomic_json(self.meta / "hosted-ci.json", hosted_ci())
        with mock.patch.object(diag.platform, "release", return_value="microsoft-standard-WSL2"), \
             mock.patch.object(diag.os, "geteuid", return_value=1000), \
             mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_name="journey")), \
             mock.patch.object(Path, "home", return_value=Path("/home/journey")), \
             mock.patch.object(Path, "cwd", return_value=workspace), \
             mock.patch.dict(os.environ, {"GH_TOKEN": "NEVER-EXPORT"}, clear=True), \
             mock.patch.object(diag, "verify_client") as pin, mock.patch.object(diag.subprocess, "Popen") as client:
            with self.assertRaises(diag.HostedEnvironmentRefused):
                diag.hosted_preflight(self.meta, workspace)
            self.assertIn("GH_TOKEN", os.environ)
            pin.assert_not_called()
            client.assert_not_called()
        self.assertFalse((self.meta / "preflight.json").exists())
        self.assertNotIn("NEVER-EXPORT", "".join(p.read_text() for p in self.meta.iterdir()))

    def test_observer_is_a_nonlaunching_sibling_and_captures_before_ack(self) -> None:
        diag.atomic_json(self.meta / "preflight.json", preflight())
        diag.atomic_json(self.meta / "capture-request.json", {"schema": 1, "mode": diag.MODE, "stop": True})
        def observed(home, event, elapsed):
            return {"event": event, "elapsed_ms": int(elapsed * 1000), "processes": [], "locks": [],
                    "processes_truncated": False, "locks_available": True, "locks_truncated": False}
        with mock.patch.object(diag.os, "geteuid", return_value=1000), \
             mock.patch.object(Path, "home", return_value=Path("/home/journey")), \
             mock.patch.object(diag, "snapshot", side_effect=observed), \
             mock.patch.object(diag.subprocess, "run", return_value=mock.Mock(returncode=0)) as exported, \
             mock.patch.object(diag.subprocess, "Popen", side_effect=AssertionError("observer launched journey")):
            diag.observe(self.meta)
        self.assertEqual(exported.call_args.args[0][-4:],
                         ["export", "/home/journey/.local/share/claude-multi/install/current", "/home/journey/diag",
                          str(self.meta / "locations.jsonl")])
        self.assertEqual(exported.call_args.kwargs["timeout"], 2)
        self.assertEqual([json.loads(line)["event"] for line in (self.meta / "snapshots.jsonl").read_text().splitlines()],
                         ["start", "end"])
        self.assertEqual(json.loads((self.meta / "observer.json").read_text()),
                         {"schema": 1, "mode": diag.MODE, "end_captured": True})

    def test_finish_preserves_failure_and_requests_capture(self) -> None:
        diag.atomic_json(self.meta / "outcome.json", {"schema": 1, "mode": diag.MODE,
                                                    "managed_turn_passed": False, "journey_exit_code": None})
        diag.finish(self.meta, 1)
        self.assertEqual(json.loads((self.meta / "outcome.json").read_text()),
                         {"schema": 1, "mode": diag.MODE, "managed_turn_passed": False, "journey_exit_code": 1})
        self.assertEqual(json.loads((self.meta / "capture-request.json").read_text()),
                         {"schema": 1, "mode": diag.MODE, "stop": True})

    def test_linux_filesystem_guard_refuses_windows_or_unknown_work_roots(self) -> None:
        with mock.patch.object(diag.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=b"9p\n")) as checked:
            with self.assertRaises(ValueError):
                diag.linux_filesystem(self.root)
        self.assertEqual(checked.call_args.args[0][:4], ["/usr/bin/stat", "-f", "-c", "%T"])
        self.assertEqual(checked.call_args.kwargs["timeout"], 2)


class FrozenInputTests(ScratchTests):
    def fake_dist(self) -> Path:
        dist = self.root / "dist"
        dist.mkdir()
        names = ("install.sh", "install.ps1", *(f"claude-multi-1.1.0-{target}.tar.gz" for target in
                 ("linux-x86_64", "linux-aarch64", "darwin-x86_64", "darwin-arm64")))
        for name in names:
            (dist / name).write_bytes(b"fake release member")
        (dist / "MANIFEST.json").write_text(json.dumps({"version": "1.1.0", "test_build": False,
            "claude_code": {"version": "2.1.292", "platforms": {"linux-x64": {
                "sha256": diag.CLIENT_SHA, "size": diag.CLIENT_SIZE}}}}))
        (dist / "SHA256SUMS").write_text("".join(
            f"{diag.file_hash(dist / name)}  {name}\n" for name in sorted((*names, "MANIFEST.json"))))
        return dist

    def test_every_sums_member_verified_without_modifying_dist(self) -> None:
        dist = self.fake_dist()
        before = {p.name: p.read_bytes() for p in dist.iterdir()}
        diag.verify_dist(dist, self.meta)
        self.assertTrue(json.loads((self.meta / "dist.json").read_text())["sha256sums_verified"])
        self.assertEqual({p.name: p.read_bytes() for p in dist.iterdir()}, before)
        for name in before:
            if name == "SHA256SUMS":
                continue
            with self.subTest(name=name):
                (dist / name).write_bytes(b"changed")
                with self.assertRaises(ValueError):
                    diag.verify_dist(dist, self.meta)
                (dist / name).write_bytes(before[name])

    def test_sums_path_escape_duplicate_and_missing_members_refuse(self) -> None:
        dist = self.fake_dist()
        sums = (dist / "SHA256SUMS").read_text()
        for changed in (sums + sums.splitlines()[0] + "\n", "\n".join(sums.splitlines()[1:]),
                        sums.replace("install.sh", "../install.sh")):
            with self.subTest(changed=changed[:30]), self.assertRaises(ValueError):
                (dist / "SHA256SUMS").write_text(changed)
                diag.verify_dist(dist, self.meta)

    def test_prefetched_client_hash_size_and_installed_version_checked_without_execution(self) -> None:
        install = self.root / "install"
        data = install / "lib/python3.14/site-packages/claude_multi/data"
        (data / "catalog").mkdir(parents=True)
        (data / "version.json").write_text('{"launcher_version":"1.1.0"}')
        client = self.root / "client"
        payload = b"fake client: not executable"
        client.write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        (data / "catalog/native-contract.json").write_text(json.dumps({"verified": [{"version": "2.1.292",
            "platforms": {"linux-x64": {"sha256": sha, "size": len(payload)}}}]}))
        with mock.patch.object(diag, "CLIENT_SHA", sha), mock.patch.object(diag, "CLIENT_SIZE", len(payload)), \
             mock.patch.object(diag.subprocess, "Popen", side_effect=AssertionError("client executed")):
            diag.verify_client(client, install, self.meta)
            client.write_bytes(b"wrong")
            with self.assertRaises(ValueError):
                diag.verify_client(client, install, self.meta)
            client.write_bytes(payload)
            (data / "version.json").write_text('{"launcher_version":"1.0.0"}')
            with self.assertRaises(ValueError):
                diag.verify_client(client, install, self.meta)


class WorkflowHostedTests(unittest.TestCase):
    def test_ps_helper_never_assigns_automatic_home_and_real_guard_is_covered(self) -> None:
        text = (SCRIPTS / "wsl_diag_watchdog.ps1").read_text()
        assignment = r"(?im)\$(?:(?:global|script|local|private):)?home\b\s*(?:[+\-*/%]?=|\+\+|--)"
        for example in ("$home = 'x'", "$HOME = 'x'", "$local:HoMe += 'x'"):
            self.assertRegex(example, assignment)
        self.assertNotRegex(text, assignment)
        guard = text[text.index("function Assert-DiagHostEnvironment"):text.index("function Initialize-DiagHostedWorker")]
        self.assertIn("$windowsHome = [Environment]::GetFolderPath", guard)
        self.assertNotRegex(guard, r"(?i)\$home\b")
        native = (REPO_ROOT / "tests/pwsh/wsl_diag_watchdog.ps1").read_text()
        self.assertLess(native.index("\nAssert-DiagHostEnvironment\n"),
                        native.index("\nfunction Assert-DiagHostEnvironment"))
        self.assertIn("Assert-True ($HOME -ceq $automaticHomeBefore)", native)

    def test_dispatch_only_single_read_only_job_and_original_artifact(self) -> None:
        text = WORKFLOW.read_text()
        jobs = re.findall(r"(?m)^  ([a-z][a-z-]+):$", text.split("jobs:\n")[1])
        self.assertEqual(jobs, ["wsl-resume-diag"])
        self.assertNotRegex(text, r"(?m)^  (push|pull_request|schedule):")
        self.assertIn("group: wsl-resume-diag-", text)
        self.assertIn("actions: read", text)
        self.assertIn("contents: read", text)
        for forbidden in ("environment: release", "contents: write", "id-token:", "continue-on-error:",
                          "gh release", "Invoke-Pester", "tools/build.py", "tools/release.py"):
            self.assertNotIn(forbidden, text)
        self.assertIn("path: harness", text)
        self.assertIn("path: candidate", text)
        self.assertIn(f"ref: {diag.SHA}", text)
        self.assertIn('artifact-ids: "11488260478"', text)
        self.assertIn('run-id: "37633707710"', text)
        self.assertIn("repository: nkoturovic/claude-multi", text)
        self.assertIn("github-token: ${{ github.token }}", text)
        self.assertIn("digest-mismatch: error", text)
        self.assertIn("timeout-minutes: 8", text)
        self.assertIn("steps.validate.outcome == 'success'", text)
        integrity = text[text.index("      - name: Invalid optional metadata still fails the diagnostic"):
                         text.index("      - uses: actions/upload-artifact@")]
        self.assertIn("if: always() && steps.validate.outcome == 'success'", integrity)
        self.assertIn('wsl_diag.py check-export "$env:CM_DIAG_UPLOAD"', integrity)
        self.assertIn('if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }', integrity)
        upload = text[text.index("      - uses: actions/upload-artifact@") :]
        self.assertIn("if: always() && steps.validate.outcome == 'success'", upload)
        uploaded = re.findall(r"\$\{\{ env.CM_DIAG_UPLOAD \}\}/([^\s]+)", text)
        self.assertEqual(set(uploaded), set(diag.EXPORT_LIMITS))
        self.assertTrue(all("*" not in name and "/" not in name for name in uploaded))

    def test_original_hosted_foreground_with_observer_sibling_and_no_wrappers(self) -> None:
        text = (SCRIPTS / "wsl_diag.sh").read_text()
        runtime = text.split("journey)\n", 1)[1]
        for absent in ("unshare", "runuser", "mount ", "env -i", "PATH=", "TMPDIR=", "CLAUDE_CODE_TMPDIR=", "setsid"):
            self.assertNotIn(absent, runtime)
        pre = runtime.index('hosted-preflight "$metadata" "$workspace"')
        observe = runtime.index('observe "$metadata"')
        foreground = runtime.index('sh "$scratch/journey.sh" installed 1.1.0')
        self.assertLess(pre, observe)
        self.assertLess(observe, foreground)
        self.assertIn('>"$scratch/observer.stdout" 2>"$scratch/observer.stderr" &', runtime)
        self.assertIn('CM_DIAG_METADATA="$metadata" JOURNEY_CLIENT="$scratch/claude"', runtime)
        self.assertIn('for attempt in $(seq 50)', runtime)
        self.assertNotIn('wait ', runtime)
        helper = (SCRIPTS / "wsl_diag.py").read_text()
        self.assertNotIn("subprocess.Popen", helper)
        self.assertNotIn("start_new_session", helper)
        workflow = WORKFLOW.read_text()
        self.assertIn('wsl --distribution Ubuntu-24.04 --cd "$candidate" --exec sh', workflow)
        self.assertIn('-LinuxWorkspace "$env:CM_DIAG_LINUX_WORKSPACE"', workflow)
        self.assertIn("Initialize-DiagHostedWorker", workflow)
        self.assertIn("steps.journey.outcome != 'success'", workflow)
        self.assertLess(workflow.index("Bounded disposable-distro termination fallback"), workflow.index("actions/upload-artifact@"))

    def test_frozen_fixture_routing_fences_and_fast_prefetch_controls_remain_owned(self) -> None:
        original = (SCRIPTS / "journey.sh").read_text()
        self.assertIn('base="http://127.0.0.1:$(cat "$work/fixture.port")/v1"', original)
        self.assertIn('--as journey-fixture --base-url "$base"', original)
        self.assertIn('direct --model custom-journey-fixture -- -p "journey turn one"', original)
        compiler = (REPO_ROOT / "src/claude_multi/compiler.py").read_text()
        self.assertIn('"ANTHROPIC_BASE_URL": meta.gateway_base_url', compiler)
        self.assertIn('"CLAUDE_CODE_DISABLE_FAST_MODE": "1"', compiler)
        scope = (REPO_ROOT / "src/claude_multi/scope.py").read_text()
        self.assertIn('"availableModels"', scope)
        self.assertIn('"apiKeyHelper"', scope)

    def test_hosted_windows_guards_and_capture_cleanup_order_are_fail_closed(self) -> None:
        text = (SCRIPTS / "wsl_diag_watchdog.ps1").read_text()
        worker = text[text.index("function Assert-DiagHostedWorker"):text.index("function Get-DiagDeadline")]
        for marker in ("$IsWindows", "26100", "GITHUB_ACTIONS", "RUNNER_OS", "RUNNER_ENVIRONMENT",
                       "github-hosted", "GITHUB_EVENT_NAME", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"):
            self.assertIn(marker, worker)
        self.assertIn("[Environment]::GetEnvironmentVariables().Keys", worker)
        self.assertIn(".EnumerateFileSystemInfos()", worker)
        self.assertIn("'--list', '--quiet'", worker)
        self.assertIn("$read.IsCompleted", worker)
        self.assertIn("Preexisting or unavailable WSL distribution; no reuse", worker)
        self.assertNotIn("ReadAllText", worker)
        self.assertNotIn("Get-ItemProperty", worker)
        self.assertNotIn("Get-NetTCPConnection", text)
        self.assertNotIn("NetFirewall", text)
        capture = text[text.index("function Request-DiagEndCapture"):text.index("function Stop-DiagDistribution")]
        self.assertIn("-lt 2000", capture)
        termination = text[text.index("function Stop-DiagDistribution"):text.index("function Invoke-DiagWatchdog")]
        self.assertIn("Assert-DiagHostedWorker", termination)
        self.assertIn("-lt 15000", termination)
        invocation = text[text.index("function Invoke-DiagWatchdog"):]
        self.assertLess(invocation.index("Request-DiagEndCapture $Directory"),
                        invocation.index("$row.terminate_state = Stop-DiagDistribution"))
        self.assertNotIn("if (-not $passed)", invocation)
        native = (REPO_ROOT / "tests/pwsh/wsl_diag_watchdog.ps1").read_text()
        for case in ("SUCCESS also terminates", "FAILURE also terminates", "Launcher exit alone is not cleanup proof",
                     "launches 0", "not-owned", "Preexisting", "terminationState = 'timeout'"):
            if case == "Preexisting":
                self.assertIn("-Output 'Ubuntu-24.04'", native)
            else:
                self.assertIn(case, native)

    def test_watchdog_first_fixture_reader_is_bounded_strict_and_partial_safe(self) -> None:
        text = (SCRIPTS / "wsl_diag_watchdog.ps1").read_text()
        reader = text[text.index("function Test-DiagFirstFixtureRequest"):
                      text.index("function Test-DiagObserverEnd")]
        self.assertEqual(re.findall(r"Join-Path \$Directory '([^']+)'", reader), ["fixture.jsonl"])
        self.assertIn(f'$limit = {diag.LIMITS["fixture.jsonl"]}', reader)
        self.assertIn(f'$records -ge {diag.RECORD_LIMITS["fixture.jsonl"]}', reader)
        self.assertIn(f'$partial -gt {diag.LINE_LIMITS["fixture.jsonl"]}', reader)
        self.assertIn('if ($partial -gt 0) { return $false }', reader)
        self.assertIn('if (Test-DiagFixtureRecord $line)', reader)
        self.assertNotIn('return $true', reader) # All complete records validate before arming.
        self.assertNotIn('resume-start.json', text)
        self.assertNotIn('Test-DiagResumeStart', text)
        for required in ("EnumerateObject()", "JsonDocumentOptions", "TryGetInt64", "1000000",
                         "carried,event,method,model,path_category,reply,stream", "request-parsed",
                         "fixture-model-1", "$path -cne 'models'", "$path -cne 'chat-completions'",
                         "return $reply -eq 1", "arm_basis = 'first-fixture-request'"):
            self.assertIn(required, text)
        native = (REPO_ROOT / "tests/pwsh/wsl_diag_watchdog.ps1").read_text()
        for case in ("Partial append cannot arm", "Unvalidated tail stays pending", "UTF-8",
                     "other-model", "'delivered'", "'\"reply\":1.0'", "* 257", "* 1048577",
                     "fixtureReads 1", "fixtureReads 2", "armed_at_ms 2000", "arm_basis 'first-fixture-request'"):
            self.assertIn(case, native)

    def test_only_bounded_60_90_second_location_markers_are_added(self) -> None:
        text = (SCRIPTS / "wsl_diag_watchdog.ps1").read_text()
        self.assertIn('$locationRequests = 0', text)
        self.assertIn('$armedAt -ge 0 -and $locationRequests -lt 2', text)
        self.assertIn('$due = if ($locationRequests -eq 0) { 60000 } else { 90000 }', text)
        self.assertIn("'location-request.json' @{ schema = 1; mode = $script:DiagMode; capture = $locationRequests }", text)
        self.assertLess(text.index('if ($deadline) { $row.status = $deadline; break }'), text.index('$due = if'))
        for capture in (1, 2):
            diag.validate_record('location-request.json', {'schema': 1, 'mode': diag.MODE, 'capture': capture})
        with self.assertRaises(ValueError):
            diag.validate_record('location-request.json', {'schema': 1, 'mode': diag.MODE, 'capture': 3})
        native = (REPO_ROOT / 'tests/pwsh/wsl_diag_watchdog.ps1').read_text()
        self.assertIn('Reset-Scenario @(0, 59999, 60000, 89999, 90000, 129999, 130000)', native)
        self.assertIn('Assert-Equal $locations.Count 2', native)
        workflow = WORKFLOW.read_text()
        self.assertNotIn('resume-locations.raw.jsonl', workflow)
        self.assertIn('${{ env.CM_DIAG_UPLOAD }}/locations.jsonl', workflow)

    def test_watchdog_has_monotonic_deadlines_one_arm_and_no_synchronous_wsl(self) -> None:
        text = (SCRIPTS / "wsl_diag_watchdog.ps1").read_text()
        for limit in (180000, 130000, 360000, 15000):
            self.assertIn(str(limit), text)
        self.assertIn("[Diagnostics.Stopwatch]::StartNew()", text)
        self.assertIn("if ($armedAt -lt 0)", text)
        self.assertIn("$info.ArgumentList.Add($argument)", text)
        self.assertNotIn("WaitForExit", text)
        self.assertNotIn("ReadAllText", text)
        self.assertNotRegex(text, r"(?m)^\s*(?:&\s+)?wsl(?:\.exe)?\s")
        self.assertNotIn("\\\\wsl$", text)
        self.assertLess(text.index("Write-DiagResult $Directory $row"), text.index("$row.terminate_state = Stop-DiagDistribution"))
        self.assertNotIn("namespace_exit_confirmed", text)
        self.assertNotIn("not-needed", text)
        self.assertIn("$row.capture_confirmed -and $row.cleanup_confirmed", text)
        self.assertIn("$row.cleanup_confirmed = $row.terminate_state -eq 'returned'", text)
        self.assertIn("} finally {", text)
        invocation = text[text.index("function Invoke-DiagWatchdog"):]
        self.assertNotIn("'--user'", invocation)
        self.assertIn("'--cd', $Workspace, '--exec'", invocation)
        self.assertIn("'journey', $LinuxDirectory, $Workspace", invocation)
        self.assertIn("Assert-DiagHostedWorker", invocation)
        self.assertIn("Assert-DiagHostedMarker $Directory", invocation)
        self.assertIn("Assert-DiagHostEnvironment", invocation)
        self.assertLess(invocation.index("Assert-DiagHostEnvironment"), invocation.index("$process = Start-DiagProcess"))
        self.assertIn("Start-DiagProcess -Arguments @('--terminate', 'Ubuntu-24.04')", text)
        self.assertIn("Start-DiagProcess -Arguments @('--distribution'", text)
        # Executable native mock cases are kept separately for an authorized
        # PowerShell host; this test itself never shells around a host denial.
        native_tests = (REPO_ROOT / "tests/pwsh/wsl_diag_watchdog.ps1").read_text()
        for status in ("bootstrap-timeout", "resume-timeout", "absolute-timeout", "invalid-metadata", "launch-failed"):
            self.assertIn(status, native_tests)
