"""Synthetic log fixtures only, never a host journal or live gateway."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import hashlib
import subprocess
import sys
import unittest
from unittest import mock

from claude_multi import gateway_events as events, launch, service
from claude_multi.cli import doctor, gateway_facts
from claude_multi.platform import linux_service
from _layout import PATCH_DIR
from _v4 import V4Case

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
INSTANCE = "a" * 32 + ":" + "b" * 32
INDEX = "0123456789abcdef"
MESSAGE = (
    "credential_save_v1 operation=refresh result=persisted provider=claude "
    f"auth_index={INDEX} credentials_changed=true stage=none errno=0 category=none "
    "bytes=123 size=123 generation=2 epoch=4"
)


def line(message=MESSAGE, **overrides):
    item = {
        "MESSAGE": "[2026-09-30 12:00:00] [--------] [info ] [credential_save.go:42] " + message,
        "__REALTIME_TIMESTAMP": str(int(NOW.timestamp() * 1_000_000)),
        "_BOOT_ID": "a" * 32, "_SYSTEMD_INVOCATION_ID": "b" * 32,
        "__CURSOR": "s=fixture;i=42",
    }
    item.update(overrides)
    # Cursors are deduplicated. Distinct synthetic journal records need
    # distinct cursors too; the old helper accidentally reused one for all.
    if "__CURSOR" not in overrides and (message != MESSAGE or overrides):
        item["__CURSOR"] = "s=fixture;i=" + hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:12]
    return json.dumps(item).encode() + b"\n"


def window(message=MESSAGE):
    return linux_service.journal_records(line(message))


class CollectorTests(unittest.TestCase):
    def collect(self, source):
        return events.collect_credential_saves(source, providers={"claude", "codex"})

    def test_metadata_and_source_identity_only(self):
        batch = self.collect(window())
        self.assertEqual(batch.coverage, "bounded")
        event, = batch.events
        self.assertEqual((event.timestamp, event.gateway_instance, event.source_cursor),
                         (NOW, INSTANCE, "s=fixture;i=42"))
        self.assertEqual((event.provider, event.auth_index, event.bytes, event.errno,
                          event.generation, event.epoch), ("claude", INDEX, 123, 0, 2, 4))
        self.assertNotIn("message", asdict(event))
        self.assertNotIn("credential_save.go", repr(batch))

    def test_source_independent_and_optional_cursor(self):
        source = events.LogWindow((events.LogRecord(MESSAGE, NOW, "file-generation-7"),), "bounded")
        event, = self.collect(source).events
        self.assertEqual(event.gateway_instance, "file-generation-7")
        self.assertIsNone(event.source_cursor)
        self.assertEqual(self.collect(events.LogWindow()).coverage, "unavailable")
        missing = self.collect(events.LogWindow((events.LogRecord(MESSAGE),), "bounded"))
        self.assertEqual(missing.coverage, "incomplete")
        self.assertIsNone(missing.events[0].timestamp)
        self.assertIsNone(missing.events[0].gateway_instance)

    def test_all_outcomes_and_refresh_only_noops(self):
        for outcome in ("persisted", "failed", "unchanged", "skipped", "unverified"):
            with self.subTest(outcome=outcome):
                result = self.collect(window(MESSAGE.replace("result=persisted", f"result={outcome}")))
                self.assertEqual(result.events[0].result, outcome)
        for outcome in ("unchanged", "skipped"):
            result = self.collect(window(MESSAGE.replace("operation=refresh", "operation=register")
                                        .replace("result=persisted", f"result={outcome}")))
            self.assertEqual(result.events, ())
            self.assertEqual(result.coverage, "incomplete")

    def test_malformed_paths_basenames_and_extra_fields_are_never_facts(self):
        for message in (
            "save failed: /private/account@example.invalid.json",
            MESSAGE + " filename=account@example.invalid.json",
            MESSAGE + " /private/auth.json",
            MESSAGE.replace("provider=claude", "provider=secret.json"),
            MESSAGE.replace(INDEX, "account.json"),
            MESSAGE.replace("stage=none", "stage=/private/auth.json"),
            MESSAGE.replace("category=none", "category=token_secret"),
            MESSAGE.replace("bytes=123", "bytes=-1"),
            MESSAGE.replace("generation=2", "generation=" + "9" * 5000),
            MESSAGE.replace("result=persisted", "result=success"),
            "prefix " + MESSAGE, MESSAGE + "\npath=/private/auth.json",
        ):
            with self.subTest(message=message[:100]):
                self.assertEqual(self.collect(window(message)).events, ())
        raw = line(MESSAGE) + line(MESSAGE + " path=/private/auth.json")
        batch = self.collect(linux_service.journal_records(raw))
        self.assertEqual(len(batch.events), 1)
        self.assertEqual(batch.coverage, "incomplete")
        self.assertNotIn("/private", repr(batch))

    def test_unknown_index_is_explicit_not_qualifying(self):
        batch = self.collect(window(MESSAGE.replace(INDEX, "invalid")))
        self.assertEqual(batch.events[0].auth_index, "invalid")
        self.assertEqual(batch.coverage, "incomplete")

    def test_restarts_preserve_instance_and_generation(self):
        raw = line() + line(_SYSTEMD_INVOCATION_ID="c" * 32)
        batch = self.collect(linux_service.journal_records(raw))
        self.assertNotEqual(batch.events[0].gateway_instance, batch.events[1].gateway_instance)
        self.assertEqual(batch.events[0].generation, batch.events[1].generation)

    def test_event_bound_is_explicit_and_keeps_recent_events(self):
        source = window().records[0]
        records = tuple(replace(source, source_cursor=f"cursor-{n}", message=source.message.replace("generation=2", f"generation={n}"))
                        for n in range(events.MAX_EVENTS + 3))
        batch = self.collect(events.LogWindow(records, "bounded"))
        self.assertEqual(len(batch.events), events.MAX_EVENTS)
        self.assertEqual(batch.events[0].generation, 3)
        self.assertEqual(batch.coverage, "truncated")

    def test_source_normalization_rejects_malformed_or_path_metadata(self):
        for raw in (b"not JSON\n", b"[]\n", b'{"MESSAGE":[1,2]}\n'):
            batch = linux_service.journal_records(raw)
            self.assertEqual(batch.records, ())
            self.assertEqual(batch.coverage, "incomplete")
        # A long record stays a source record (the journal recognizers still count it);
        # only the credential-save collector refuses a long receipt-shaped message.
        long_record = linux_service.journal_records(line("x" * (events.MAX_MESSAGE + 1)))
        self.assertEqual((len(long_record.records), long_record.coverage), (1, "bounded"))
        self.assertEqual(self.collect(long_record), events.CredentialSaves((), "bounded"))
        long_receipt = self.collect(window(MESSAGE + " " * events.MAX_MESSAGE))
        self.assertEqual((long_receipt.events, long_receipt.coverage), ((), "incomplete"))
        record, = linux_service.journal_records(line(__CURSOR="/private/secret", _BOOT_ID="secret.json",
                                                    __REALTIME_TIMESTAMP="invalid")).records
        self.assertIsNone(record.source_cursor)
        self.assertIsNone(record.gateway_instance)
        self.assertIsNone(record.timestamp)

    def test_source_pins_patch8_format(self):
        patch = (PATCH_DIR / "cli-proxy-api-credential-save-report.patch").read_text()
        self.assertIn('credential_save_v1 operation=%s result=%s provider=%s auth_index=%s '
                      'credentials_changed=%t stage=%s errno=%d category=%s bytes=%d size=%d '
                      'generation=%d epoch=%d', patch)


class JournalSourceTests(unittest.TestCase):
    def run_source(self, program, *, max_bytes=65536, timeout=2):
        calls = []
        def spawn(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.Popen([sys.executable, "-c", program], **kwargs)
        result = linux_service.read_gateway_journal(unit="fixture-unit", max_bytes=max_bytes,
                                                   timeout=timeout, popen=spawn)
        self.assertEqual(calls[0][0][0], "journalctl")
        self.assertIn("json", calls[0][0])
        self.assertIn("fixture-unit", calls[0][0])
        self.assertEqual(calls[0][1]["stderr"], subprocess.DEVNULL)
        return result

    def test_success_and_empty_are_bounded_not_complete(self):
        result = self.run_source(f"import os; os.write(1, {line()!r})")
        self.assertEqual(result.coverage, "bounded")
        self.assertEqual(len(result.records), 1)
        self.assertEqual(self.run_source("pass").coverage, "bounded")

    def test_reader_runs_newest_first_over_the_whole_day_and_returns_chronological(self):
        calls = []
        def spawn(argv, **kwargs):
            calls.append(argv)
            newest_first = line(_SYSTEMD_INVOCATION_ID="c" * 32) + line()
            return subprocess.Popen([sys.executable, "-c", f"import os; os.write(1, {newest_first!r})"], **kwargs)
        result = linux_service.read_gateway_journal(unit="fixture-unit", max_bytes=65536, popen=spawn)
        self.assertIn("--reverse", calls[0])
        self.assertIn("-24h", calls[0])
        self.assertNotIn("-n", calls[0], "a record count must not cut the 24 h window")
        self.assertEqual([record.gateway_instance[-1] for record in result.records], ["b", "c"])

    def test_caps_keep_the_newest_records(self):
        newest_first = b"".join(line(__CURSOR=f"s=fixture;i={n}") for n in range(5, 0, -1))
        with mock.patch.object(linux_service.observation, "LOG_MAX_RECORDS", 3):
            window = linux_service.journal_records(newest_first, newest_first=True)
        self.assertEqual(window.coverage, "truncated")
        self.assertEqual([record.source_cursor for record in window.records],
                         ["s=fixture;i=3", "s=fixture;i=4", "s=fixture;i=5"])
        cut = self.run_source(f"import os; os.write(1, {newest_first!r})", max_bytes=len(newest_first) * 2 // 5 + 3)
        self.assertEqual(cut.coverage, "truncated")
        self.assertEqual([record.source_cursor for record in cut.records], ["s=fixture;i=4", "s=fixture;i=5"])

    def test_byte_limit_and_timeout_kill_reader_without_unbounded_buffering(self):
        result = self.run_source("import os; os.write(1, b'x' * 1000000)", max_bytes=512)
        self.assertEqual(result.coverage, "truncated")
        self.assertEqual(result.records, ())
        result = self.run_source("import time; time.sleep(30)", timeout=0.05)
        self.assertEqual(result.coverage, "incomplete")

    def test_source_exit_timeout_retains_partial_and_marks_timed_out(self):
        program = "import os,time; os.write(1, " + repr(line()) + "); os.close(1); time.sleep(30)"
        result = self.run_source(program, timeout=0.1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.coverage, "incomplete")
        self.assertEqual(len(result.records), 1)

    def test_failure_and_missing_reader_are_unavailable(self):
        self.assertEqual(self.run_source("raise SystemExit(1)").coverage, "unavailable")
        with mock.patch.object(linux_service, "read_gateway_journal", return_value=events.LogWindow()) as read:
            self.assertEqual(service.read_gateway_journal(max_bytes=1024).coverage, "unavailable")
            read.assert_called_once_with(unit=service.UNIT, max_bytes=1024, since="-24h")
        self.assertEqual(linux_service.read_gateway_journal(
            unit="fixture", max_bytes=512, popen=mock.Mock(side_effect=FileNotFoundError())).coverage,
            "unavailable")


class DoctorSaveTests(V4Case):
    def report(self, source):
        info = []
        attention = doctor._doctor_journal_report(self.runtime, source, info_lines=info)
        return attention, info

    FAILED = (MESSAGE.replace("result=persisted", "result=failed").replace("stage=none", "stage=dirsync")
              .replace("errno=0", "errno=28").replace("category=none", "category=errno"))

    def test_failed_alone_is_attention_with_the_hold_remedy(self):
        attention, info = self.report(linux_service.journal_records(line(self.FAILED)))
        self.assertEqual(len(attention), 1)
        for value in ("failed provider=claude", f"auth_index={INDEX}", "bytes=123/123", "errno=28",
                      "keep the gateway running", "free space", "wait for a persisted save",
                      "re-authenticate with claude-multi providers sign-in anthropic only if none appears",
                      "before any restart or rollback", "retain auth.pre-*"):
            self.assertIn(value, attention[0])
        self.assertTrue(any("coverage=bounded" in text for text in info))

    def test_later_repair_evidence_changes_the_remedy_but_keeps_the_failure_visible(self):
        unverified = MESSAGE.replace("result=persisted", "result=unverified").replace("category=none", "category=no_sync")
        for label, later in (("persisted", MESSAGE), ("unverified changed", unverified),
                             ("newer generation", MESSAGE.replace("generation=2", "generation=3"))):
            with self.subTest(label):
                attention, info = self.report(linux_service.journal_records(line(self.FAILED) + line(later)))
                self.assertEqual(attention, [], "repair evidence satisfies the hold")
                failure, = [text for text in info if "failed provider=claude" in text]
                self.assertIn("no re-authentication needed", failure)
                self.assertIn("retain auth.pre-*", failure)
                self.assertNotIn("re-authenticate with", failure)
                if "unverified" in label:
                    self.assertIn("credentials_changed=true (repair evidence, not a verified save)", failure)
                    self.assertTrue(any("result=unverified" not in text and "unverified provider=claude" in text
                                        and "credentials_changed=true" in text and "not a verified save" in text
                                        for text in info))

    def test_nonqualifying_later_events_keep_the_hold(self):
        unverified = MESSAGE.replace("result=persisted", "result=unverified")
        cases = {
            "unverified unchanged": [line(unverified.replace("credentials_changed=true", "credentials_changed=false"))],
            "superseded": [line(unverified.replace("category=none", "category=superseded"))],
            "unchanged": [line(MESSAGE.replace("result=persisted", "result=unchanged"))],
            "other instance": [line(_SYSTEMD_INVOCATION_ID="c" * 32)],
            "other credential": [line(MESSAGE.replace(INDEX, "fedcba9876543210"))],
            "older generation": [line(MESSAGE.replace("generation=2", "generation=1"))],
            "failed again": [line(), line(self.FAILED.replace("errno=28", "errno=5"))],
            "unknown instance": [line(_BOOT_ID="unknown")],
        }
        for label, later in cases.items():
            with self.subTest(label):
                attention, _info = self.report(linux_service.journal_records(line(self.FAILED) + b"".join(later)))
                self.assertEqual(len(attention), 1)
                self.assertIn("only if none appears", attention[0])

    def test_partial_windows_qualify_the_journal_counts(self):
        status = '[2026-09-30 11:00:00] [req-1] [info ] [gin.go:1] 403 | 1ms | 127.0.0.1 | POST "/v1/messages"'
        grant = "[2026-09-30 11:00:00] [--------] [warn ] [x.go:1] codex refresh failed: invalid_grant"
        raw = line(MESSAGE=status, __CURSOR="status") + line(MESSAGE=grant, __CURSOR="grant")
        oldest = NOW - timedelta(hours=1)
        records = linux_service.journal_records(raw).records + (events.LogRecord("unrelated", oldest),)
        for coverage, timed_out, qualified in (("bounded", False, False), ("truncated", False, True),
                                               ("incomplete", False, True), ("bounded", True, True)):
            with self.subTest(coverage=coverage, timed_out=timed_out):
                source = events.LogWindow(records, coverage, timed_out=timed_out)
                with mock.patch.object(gateway_facts, "_pool_credential_mtimes", return_value={}):
                    attention, _info = self.report(source)
                quota, = [text for text in attention if "× 403" in text]
                dead, = [text for text in attention if "invalid_grant" in text]
                self.assertIn("in the last 24 h", quota)
                for text in (quota, dead):
                    self.assertEqual("partial journal read" in text, qualified, text)
                    if qualified:
                        self.assertIn(f"since {oldest.astimezone():%Y-%m-%d %H:%M}, coverage={coverage}", text)
                    self.assertEqual("timed out" in text, timed_out)

    def test_log_window_substitutions_keep_live_and_retired_catalog_labels(self):
        docs = self.runtime.ordinary_docs
        for collection, field in ((docs["models"]["models"], "wire_model"),
                                  (docs["retired"]["retired"], "last_wire")):
            key, model = next(iter(collection.items()))
            wire = model[field]
            with self.subTest(key=key):
                message = (f'[2026-09-30 12:00:00] [req-1] [warn ] [fixture.go:1] '
                           f'claude executor: upstream served model "fixture-served" for requested model "{wire}"')
                source = events.LogWindow((events.LogRecord(message, NOW, INSTANCE),), "bounded")
                attention, _info = self.report(source)
                substitution, = [text for text in attention if "gateway substitution:" in text]
                legacy, _info = self.report(message)
                self.assertEqual(substitution, next(text for text in legacy if "gateway substitution:" in text))
                self.assertIn(key, substitution)
                self.assertIn(f"({wire})", substitution)
                self.assertNotIn("partial journal read", substitution)
        unknown = message.replace(wire, "fixture-unknown-wire")
        attention, _info = self.report(events.LogWindow((events.LogRecord(unknown, NOW, INSTANCE),), "bounded"))
        self.assertIn("gateway substitution: wire fixture-unknown-wire", "\n".join(attention))

    def test_nonpersisted_results_are_not_verified_and_raw_paths_never_display(self):
        for outcome in ("unverified", "unchanged", "skipped"):
            attention, info = self.report(window(MESSAGE.replace("result=persisted", f"result={outcome}")))
            self.assertEqual(attention, [])
            self.assertIn("not a verified save", info[-1])
        attention, info = self.report(window(MESSAGE + " auth=account.json path=/private/auth.json invalid_grant"))
        self.assertEqual(attention, [])
        self.assertEqual(len(info), 1)
        self.assertIn("coverage=incomplete", info[0])
        for forbidden in ("account.json", "/private", "invalid_grant", "credential_save.go"):
            self.assertNotIn(forbidden, repr((attention, info)))

    def test_reader_is_suppressed_by_injected_runtime_and_called_once_when_live(self):
        from claude_multi import endpoint
        from claude_multi.platform import file_log

        # The on-demand gateway's log source is its instance files; the
        # supervised unit's is its journal.
        with mock.patch.object(file_log, "read_recent", return_value=window()) as files, \
                mock.patch.object(service, "read_gateway_journal", return_value=window()) as read:
            self.assertIsNone(gateway_facts._read_gateway_journal(self.runtime))
            files.assert_not_called()
            self.runtime.health_get = self.runtime.served_models_callback = None
            info = []
            doctor._doctor_journal_report(self.runtime, info_lines=info)
            files.assert_called_once()
            self.assertEqual(files.call_args.kwargs["max_bytes"], gateway_facts._JOURNAL_MAX_BYTES)
            read.assert_not_called()
            self.assertTrue(any("persisted provider=claude" in text for text in info))
            port = endpoint.port_of(self.runtime.catalog.docs["gateway"]["gateway"]["base_url"])
            endpoint.write_config(self.runtime.home, endpoint.EndpointConfig(port=port, backend=endpoint.SYSTEMD))
            with mock.patch.object(endpoint.sys, "platform", "linux"):
                gateway_facts._read_gateway_journal(self.runtime)
            read.assert_called_once_with(max_bytes=gateway_facts._JOURNAL_MAX_BYTES, since="-24h",
                                         unit=endpoint.DEFAULT_UNIT)

    def test_readiness_failure_does_not_hide_the_persistence_hold(self):
        failed = MESSAGE.replace("result=persisted", "result=failed")
        self.runtime.doctor_callback = None
        self.runtime.health_get = mock.Mock(side_effect=launch.LaunchError("fixture unavailable"))
        with mock.patch.object(gateway_facts, "_read_gateway_journal", return_value=window(failed)) as read:
            problems, info, attention = doctor._collect_doctor_reports(self.runtime)
        self.assertTrue(problems)
        self.assertTrue(any("keep the gateway running" in text for text in attention))
        read.assert_called_once_with(self.runtime)


class RecoveryHoldPolicyTests(unittest.TestCase):
    def batch(self, *messages):
        raw = b''.join(line(message, __REALTIME_TIMESTAMP=str(int(NOW.timestamp() * 1000000) + n),
                            __CURSOR=f's=fixture;i={n}') for n, message in enumerate(messages))
        return events.collect_credential_saves(linux_service.journal_records(raw), providers={'claude'})

    def failure(self, operation='update', stage='write'):
        return MESSAGE.replace('operation=refresh', 'operation=' + operation).replace('result=persisted', 'result=failed').replace('stage=none', 'stage=' + stage)

    def test_recovery_repair_requires_same_instance_identity_epoch(self):
        batch = self.batch(self.failure(), MESSAGE)
        self.assertFalse(events.recovery_hold(batch))
        failure, repaired = batch.events
        for other in (replace(repaired, gateway_instance='other'), replace(repaired, auth_index='fedcba9876543210'),
                      replace(repaired, epoch=3), replace(repaired, generation=1), replace(repaired, timestamp=failure.timestamp)):
            self.assertTrue(events.recovery_hold(events.CredentialSaves((failure, other), 'bounded')))
        self.assertTrue(events.recovery_hold(replace(batch, coverage='incomplete')))

    def test_recovery_unverified_changed_is_a_repair(self):
        self.assertFalse(events.recovery_hold(self.batch(self.failure(), MESSAGE.replace('persisted', 'unverified'))))
        self.assertTrue(events.recovery_hold(self.batch(self.failure(), MESSAGE.replace('persisted', 'unverified').replace('credentials_changed=true', 'credentials_changed=false'))))
