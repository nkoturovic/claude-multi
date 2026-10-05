"""Reports: minimized sources, severity parity and no observation writes."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import errno
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from claude_multi import gateway_events as events, observations as obs, quota, sessions, state, validate
import claude_multi.cli.doctor as doctor
import claude_multi.cli.entry as entry
import claude_multi.cli.gateway_facts as gateway_facts
import claude_multi.cli.runtime as runtime_mod
from claude_multi.platform.observation import LogRecord, LogWindow
from _catalog import FIXTURE_ROOT
from _v4 import V4Case

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
MID = "11111111-1111-4111-8111-111111111111"


def log(message, rid="a1b2c3d4", *, when=NOW, instance="fixture-1", cursor=None):
    return LogRecord(f"[2026-09-30 12:00:00] [{rid}] [info ] [fixture.go:1] {message}", when, instance, cursor)


def source(*rows, coverage="bounded"):
    return LogWindow(tuple(rows), coverage)


def collect(*rows, **kwargs):
    return events.collect(source(*rows, **kwargs), providers={"claude", "codex"})


def final(code=200, path="/v1/messages", method="POST", **kwargs):
    return log(f'{code} | 25ms | 127.0.0.1 | {method} "{path}"', **kwargs)


def inventory(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode, p.read_bytes() if p.is_file() and not p.is_symlink() else None)
            for p in root.rglob("*")}


class EventTests(unittest.TestCase):
    def test_backend_neutral_events_optional_cursor_instance_coverage(self):
        batch = collect(log("session-affinity: provider=claude model=fixture-alias auth=private@example.invalid session=123"), final())
        self.assertEqual(batch.requests[0].selector, "fixture-alias")
        self.assertEqual(batch.coverage, "partial")
        self.assertIsNone(batch.requests[0].source_cursor)
        self.assertIsNone(batch.requests[0].session_id)
        missing = collect(final(instance=None))
        self.assertGreater(missing.incomplete, 0)

    def test_journal_observation_drops_auth_identity(self):
        batch = collect(log("session-affinity: provider=claude model=fixture-alias auth=secret@example.invalid session=1234"),
                        log('claude executor: upstream served model "fixture-served" for requested model "fixture-wire" (auth_index=SECRET)'),
                        log("claude refresh: invalid_grant /private/credential.json"), final())
        for secret in ("secret@example.invalid", "SECRET", "/private/", "session=1234", "fixture.go"):
            self.assertNotIn(secret, repr(batch))
        self.assertEqual(batch.oauth_failures[0].pool, "claude")

    def test_request_id_reuse_does_not_merge_requests(self):
        batch = collect(log("session-affinity: provider=claude model=first"), final(),
                        log("session-affinity: provider=codex model=second", when=NOW + timedelta(seconds=5)),
                        final(429, when=NOW + timedelta(seconds=6)))
        self.assertEqual(len(batch.requests), 2)
        self.assertTrue(all(r.selector is None and r.provider is None for r in batch.requests))
        separate = collect(log("model=first provider=claude"), final(),
                           log("model=second provider=codex", instance="fixture-2"), final(instance="fixture-2"))
        self.assertEqual([r.selector for r in separate.requests], ["first", "second"])

    def test_journal_truncation_marks_partial(self):
        batch = collect(final(), coverage="truncated")
        self.assertEqual(batch.coverage, "partial")
        self.assertEqual(batch.credential_saves.coverage, "truncated")

    def test_empty_observation_does_not_claim_complete_history(self):
        self.assertEqual(collect().coverage, "partial")
        self.assertEqual(events.collect(LogWindow(), providers=()).coverage, "unavailable")

    def test_cursor_dedup_and_conflicts_are_not_request_identity(self):
        row = final(cursor="same")
        self.assertEqual(len(collect(row, row).requests), 1)
        batch = collect(log("model=one"), log("model=two"), final())
        self.assertIsNone(batch.requests[0].selector)

    def test_json_unknown_is_null_not_zero(self):
        f = obs.Fact("counter", "gateway", None, "unknown")
        report = obs.Report("usage", NOW, {"journal": "unavailable"}, (f,))
        self.assertIsNone(json.loads(report.json())["facts"][0]["value"])
        with self.assertRaises(ValueError):
            replace(f, value=0)
        with self.assertRaises(ValueError):
            replace(f, status="known", value={"raw": "body"})

    def test_block_attention_info_are_preserved(self):
        for levels, expected in ((("info",), "ready"), (("info", "attention"), "attention"),
                                 (("info", "attention", "block"), "blocked")):
            ds = tuple(obs.legacy_diagnostic(level, "raw private body") for level in levels)
            report = obs.Report("doctor", NOW, {}, diagnostics=ds)
            self.assertEqual(report.status, expected)
            self.assertEqual([d.severity for d in report.diagnostics], list(levels))
            self.assertNotIn("raw private body", report.json())

    def test_closed_report_schema_and_fixture_mirror(self):
        schema = json.loads((FIXTURE_ROOT / "schemas/report.schema.json").read_text())
        report = obs.Report("doctor", NOW, {}, (obs.Fact("count", "gateway", None, "known", 2),)).document()
        self.assertEqual(validate.validate(report, schema), [])
        report["raw_body"] = "private"
        self.assertTrue(validate.validate(report, schema))


class ReportRuntimeTests(V4Case):
    def test_readonly_session_store_does_not_create_or_chmod(self):
        root = self.root / "empty-state"
        store = sessions.SessionStore(root, self.runtime.session_store.schema, read_only=True)
        self.assertEqual(store.scan_uuid_records(), [])
        self.assertIsNone(store.last(str(self.root)))
        self.assertFalse(root.exists())
        root.mkdir(mode=0o755)
        os.chmod(root, 0o755)
        (root / "sessions").mkdir(mode=0o750)
        before = inventory(root), root.stat().st_mode
        store = sessions.SessionStore(root, store.schema, read_only=True)
        self.assertEqual(store.scan_uuid_records(), [])
        for effect in (store.runtime_index_lock, lambda: store.lifecycle_lock(MID), lambda: store.update_last("cwd", MID)):
            with self.assertRaises(sessions.SessionError):
                effect()
        self.assertEqual((inventory(root), root.stat().st_mode), before)

    def test_report_does_not_install_seeds_or_shims(self):
        home = self.root / "empty-home"
        home.mkdir()
        runtime = runtime_mod.Runtime(asset_root=FIXTURE_ROOT, environ={"HOME": str(home)}, cwd=home,
                   allow_state_writes=False, refresh_shims=False, initialize_session_store=False,
                   health_get=lambda *_: 200, served_models_callback=lambda *_: (set(), 200),
                   doctor_callback=lambda *_: [], doctor_binary_callback=lambda *_: ([], []),
                   doctor_daemon_callback=self.runtime.doctor_daemon_callback,
                   managed_root=self.root / "empty-managed", proc_root=self.root / "empty-proc")
        for argv in (["doctor", "--json"], ["quota", "--json"], ["usage", "--json"], ["explain", MID, "--json"]):
            out = io.StringIO()
            entry.main(argv, runtime=runtime, output_stream=out, interactive=False)
            self.assertEqual(json.loads(out.getvalue())["report"], argv[0])
        self.assertEqual(runtime.session_store.scan_uuid_records(), [])
        self.assertEqual(runtime.pool_status().state, "seam")
        self.assertEqual(inventory(home), {})

    def test_json_refusals_keep_specific_diagnostic_on_stderr(self):
        self.runtime.environ.pop("CLAUDE_MULTI_MANAGED_ID", None)
        # A refusal exits 1, an invalid option value 2; stdout keeps one document.
        cases = ((["explain"], "no managed session selected — pass a session id", 1),
                 (["usage", "--since", "bogus"], "usage: --since", 2),
                 (["explain", "zzz"], "no managed session matches 'zzz'", 1))
        for argv, message, status in cases:
            with self.subTest(argv=argv), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                out = io.StringIO()
                self.assertEqual(entry.main([*argv, "--json"], runtime=self.runtime,
                                            output_stream=out, interactive=False), status)
                self.assertEqual(json.loads(out.getvalue())["report"], argv[0])
                self.assertIn("claude-multi: " + message, err.getvalue())
                self.assertNotIn(message, out.getvalue())
        record = self.launch_fresh()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            out = io.StringIO()
            self.assertEqual(entry.main(["explain", record["managed_id"], "--agent", "not-a-role", "--json"],
                                        runtime=self.runtime, output_stream=out, interactive=False), 1)
        self.assertIn("agent must be a bound cm-* role", err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["report"], "explain")

    def test_json_collection_errors_keep_sanitized_diagnostic_and_remedy(self):
        from claude_multi import errors
        import claude_multi.cli.dispatch as dispatch
        for error in (sessions.StateMarkerError(99), errors.ClaudeMultiError("fixture \x1b[31m", remedy="fix \x1b[0m")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(dispatch, "handle_command", side_effect=error), \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                out = io.StringIO()
                self.assertEqual(entry.main(["quota", "--json"], runtime=self.runtime,
                                            output_stream=out, interactive=False), 1)
                self.assertEqual(json.loads(out.getvalue())["report"], "quota")
                self.assertNotIn("\x1b", err.getvalue())
                self.assertIn("claude-multi: ", err.getvalue())
                if error.remedy:
                    self.assertIn("fix: fix ", err.getvalue())
                else:
                    self.assertIn("state version 99", err.getvalue())

    def test_reports_take_read_only_runtime_branch(self):
        for argv in (["doctor", "--json"], ["quota", "--json"], ["usage", "--json"], ["explain", MID, "--json"]):
            with self.subTest(argv=argv), mock.patch.dict(os.environ, self.runtime.environ, clear=True), \
                    mock.patch.object(runtime_mod, "Runtime", side_effect=RuntimeError("intercept construction")) as factory:
                with self.assertRaisesRegex(RuntimeError, "intercept construction"):
                    entry.main(argv, output_stream=io.StringIO(), interactive=False)
                kwargs = factory.call_args.kwargs
                self.assertFalse(kwargs["allow_state_writes"])
                self.assertFalse(kwargs["refresh_shims"])
                self.assertFalse(kwargs["initialize_session_store"])

    def test_reports_create_no_files_or_locks(self):
        record = self.launch_fresh()
        before = inventory(self.root)
        for argv in (["doctor", "--json"], ["quota", "--json"], ["usage", "--json"],
                     ["explain", record["managed_id"], "--json"]):
            with mock.patch.object(gateway_facts, "_read_gateway_journal", return_value=source()):
                entry.main(argv, runtime=self.runtime, output_stream=io.StringIO(), interactive=False)
        self.assertEqual(inventory(self.root), before)

    def test_observation_never_changes_authority(self):
        self.test_reports_create_no_files_or_locks()

    def test_report_reuses_management_failure_cache(self):
        # A snapshot must not circumvent the existing negative cache or TTL.
        self.runtime._pool_cache = (self.runtime.pool_clock(), quota.PoolStatus("mismatch", 401))
        with mock.patch("claude_multi.management.pool_status", side_effect=AssertionError("retry")):
            with self.runtime.report_snapshot():
                self.assertEqual(self.runtime.pool_status().state, "mismatch")
                self.assertEqual(self.runtime.pool_status().state, "mismatch")

    def test_text_and_json_share_one_snapshot(self):
        from claude_multi import usage
        with mock.patch.object(gateway_facts, "_read_gateway_journal", return_value=source(final())) as read:
            with self.runtime.report_snapshot():
                first = gateway_facts.report_events(self.runtime)
                second = gateway_facts.report_events(self.runtime)
                self.assertIs(first, second)
                report = usage.report(first, NOW - timedelta(hours=1), NOW)
                self.assertIn("Observed requests: 1", usage.text(report))
                self.assertIn('"value": 1', report.json())
            self.assertEqual(read.call_count, 1)

    def test_doctor_json_status_never_better_than_human(self):
        with self.runtime.report_snapshot():
            report = doctor.doctor_report(self.runtime, (["arbitrary secret block"], ["ok"], ["attention"]))
        self.assertEqual(report.status, "blocked")
        self.assertEqual(report.diagnostics[0].severity, "block")
        self.assertNotIn("arbitrary secret", report.json())

    def test_json_contains_no_raw_error_or_management_body(self):
        self.runtime.broken_override_error = "private upstream body sk-ant-SENTINEL"
        output = io.StringIO()
        entry.main(["doctor", "--json"], runtime=self.runtime, output_stream=output, interactive=False)
        document = json.loads(output.getvalue())
        self.assertEqual(document["status"], "blocked")
        self.assertNotIn("SENTINEL", output.getvalue())
        self.assertNotIn("private upstream body", output.getvalue())


class HookLogTests(V4Case):
    def fact(self):
        return doctor._hook_error_observation(self.runtime, [self.record] if hasattr(self, "record") else [])

    @property
    def path(self):
        return self.runtime.session_store.root / "hook-errors.log"

    def test_hook_log_enoent_is_absent(self):
        f, lines = self.fact()
        self.assertEqual((f.status, f.value, f.reason, lines), ("known", 0, "absent", []))

    def test_hook_log_empty_and_last_launch_boundary(self):
        self.record = self.launch_fresh()
        settings = self.runtime.session_store.root / "scopes" / self.record["managed_id"] / "settings.json"
        os.utime(settings, (NOW.timestamp(), NOW.timestamp()))
        self.path.write_bytes(b"")
        self.assertEqual(self.fact()[0].value, 0)
        rows = [{"time": (NOW + timedelta(seconds=n)).isoformat(), "event": "prompt"} for n in (-1, 0, 1)]
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        f, lines = self.fact()
        self.assertEqual((f.value, f.classification), (2, "attention"))
        self.assertIn("prompt 2", lines[0])

    def test_hook_log_malformed_lines_preserve_valid_counts_and_remedy(self):
        self.record = self.launch_fresh()
        settings = self.runtime.session_store.root / "scopes" / self.record["managed_id"] / "settings.json"
        os.utime(settings, (NOW.timestamp(), NOW.timestamp()))
        valid = [{"time": (NOW + timedelta(seconds=n)).isoformat(), "event": "prompt"} for n in (-1, 0, 1)]
        baseline = "".join(json.dumps(row) + "\n" for row in valid)
        self.path.write_text(baseline)
        expected = self.fact()[1]
        for malformed in ("not json\n", '{"event":"prompt"}\n', '{"time":"bad"}\n', '{"time":'):
            with self.subTest(malformed=malformed):
                self.path.write_text(baseline + malformed)
                fact, lines = self.fact()
                self.assertEqual((fact.status, fact.value, fact.coverage, fact.reason),
                                 ("known", 2, "partial", "malformed-lines"))
                self.assertEqual(lines, expected)
                self.assertNotIn(doctor.HOOK_LOG_UNAVAILABLE, lines)

    def test_hook_log_eacces_is_attention_null_count(self):
        self.path.touch()
        with mock.patch.object(Path, "lstat", side_effect=PermissionError(errno.EACCES, "private error")):
            fact, lines = self.fact()
        self.assertEqual((fact.status, fact.value, fact.classification), ("unavailable", None, "attention"))
        self.assertNotIn("private error", repr(lines))

    def test_hook_log_symlink_dangling_directory_fifo_socket_unavailable(self):
        target = self.root / "target"
        target.write_text("never read")
        for kind in ("symlink", "dangling", "directory", "fifo", "socket"):
            with self.subTest(kind=kind):
                sock = None
                if kind in {"symlink", "dangling"}:
                    self.path.symlink_to(target if kind == "symlink" else self.root / "absent")
                elif kind == "directory":
                    self.path.mkdir()
                elif kind == "fifo":
                    os.mkfifo(self.path)
                else:
                    sock = socket.socket(socket.AF_UNIX)
                    sock.bind(str(self.path))
                try:
                    with mock.patch("claude_multi.lineup_log.os.open", side_effect=AssertionError("opened special file")):
                        fact, lines = self.fact()
                    self.assertEqual((fact.status, fact.value, fact.classification), ("unavailable", None, "attention"))
                    self.assertEqual(lines, [doctor.HOOK_LOG_UNAVAILABLE])
                finally:
                    if sock:
                        sock.close()
                    if kind == "directory":
                        self.path.rmdir()
                    else:
                        self.path.unlink()


class ManagementReportTests(V4Case):
    def enable(self, code, body):
        from claude_multi import management, service
        management.prepare_start(self.runtime.home, stopped=True)
        self.runtime.environ[management.CHANNEL_ENV] = management.MANAGEMENT_CHANNEL
        self.runtime.management_callback = mock.Mock(return_value=(code, body))
        self.runtime.listener_owner = lambda _base: service.OwnerVerdict("ours", "fixture")
        self.runtime._pool_cache = None

    def test_quota_json_never_retries_auth_failure(self):
        self.enable(401, b"private upstream body SENTINEL")
        for _ in range(2):
            out = io.StringIO()
            self.assertEqual(entry.main(["quota", "--json"], runtime=self.runtime, output_stream=out, interactive=False), 1)
            self.assertEqual(json.loads(out.getvalue())["coverage"]["management"], "mismatch")
            self.assertNotIn("SENTINEL", out.getvalue())
        self.runtime.management_callback.assert_called_once()

    def test_quota_json_preserves_listener_ownership_attention(self):
        from claude_multi import service
        self.enable(200, b'{"files":[]}')
        self.runtime.listener_owner = lambda _base: service.OwnerVerdict("unknown", "fixture")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            out = io.StringIO()
            self.assertEqual(entry.main(["quota", "--json"], runtime=self.runtime,
                                        output_stream=out, interactive=False), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["status"], "attention")
        self.assertIn("gateway-attention", [d["code"] for d in report["diagnostics"]])
        self.assertIn("could not be confirmed", err.getvalue())
        self.runtime.management_callback.assert_called_once()

    def test_no_new_management_route_is_used(self):
        self.enable(200, b'{"files":[]}')
        self.runtime.pool_status()
        from claude_multi import management
        connection = mock.Mock()
        connection.getresponse.return_value.status = 401
        management.send_request(connection, "dummy-test-key")
        connection.request.assert_called_once_with("GET", "/v0/management/auth-files", headers={"X-Management-Key": "dummy-test-key"})
        self.runtime.management_callback.assert_called_once()

    def test_oauth_and_credential_save_parity_with_healthy_management(self):
        from tests.test_credential_saves import MESSAGE, line
        from claude_multi.platform.linux_service import journal_records
        self.enable(200, b'{"files":[{"provider":"claude","status":"active"}]}')
        failure = MESSAGE.replace("result=persisted", "result=failed")
        rows = journal_records(line(failure)).records + (log("claude refresh invalid_grant"),)
        with mock.patch.object(gateway_facts, "_read_gateway_journal", return_value=source(*rows)), \
                mock.patch.object(gateway_facts, "_pool_credential_mtimes", return_value={}):
            with self.runtime.report_snapshot():
                _attention, _info, suppressed = doctor._doctor_quota_report(self.runtime)
                attention = doctor._doctor_journal_report(self.runtime, refresh_failures=suppressed)
        self.assertTrue(any("invalid_grant" in text for text in attention))
        self.assertTrue(any("credential save: failed" in text for text in attention))


class DiagnosticContractTests(unittest.TestCase):
    """Typed diagnostics: stable codes, severities, safe subjects, one
    structured remedy, and the one severity reducer."""

    def test_the_reducer_orders_blocked_attention_ready(self) -> None:
        self.assertEqual(obs.status_of([]), "ready")
        self.assertEqual(obs.status_of(["info"]), "ready")
        self.assertEqual(obs.status_of(["info", "attention"]), "attention")
        self.assertEqual(obs.status_of(["attention", "block", "info"]), "blocked")

    def test_fields_are_validated(self) -> None:
        good = obs.Diagnostic("attention", "gateway-not-set-up", "the gateway is not set up yet",
                              subject_id="gateway", remedy="claude-multi setup --step gateway")
        self.assertEqual(good.remedy, "claude-multi setup --step gateway")
        for kwargs in ({"severity": "fatal"}, {"code": "Not A Code"}, {"code": "legacy_line"},
                       {"subject_id": "/home/user/secret path"}, {"subject_id": "user@example.com"},
                       {"remedy": "line one\nline two"}, {"remedy": "x" * 241}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                obs.Diagnostic(**{"severity": "info", "code": "a-code", "text": "t", **kwargs})

    def test_the_document_carries_stable_keys(self) -> None:
        report = obs.Report("doctor", datetime.now(timezone.utc), {}, diagnostics=(
            obs.Diagnostic("block", "scope-damaged", "scope damaged", subject_id="1111aaaa",
                           remedy="claude-multi doctor --repair 1111aaaa"),
            obs.Diagnostic("info", "legacy-line", "x")))
        document = report.document()
        self.assertEqual(document["status"], "blocked")
        self.assertEqual([set(d) for d in document["diagnostics"]],
                         [{"severity", "code", "text", "subject_id", "remedy"}] * 2)
        self.assertIsNone(document["diagnostics"][1]["remedy"])
