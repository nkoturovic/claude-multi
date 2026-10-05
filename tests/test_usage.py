"""Inference-only final HTTP observations and honest missing coverage."""
from datetime import timedelta
import json
import unittest

from claude_multi import gateway_events, usage
from tests.test_observations import NOW, MID, collect, final, log


def values(report):
    return {f.code: f.value for f in report.facts if f.subject_kind == "gateway"}


class UsageTests(unittest.TestCase):
    def report(self, events, session=None):
        return usage.report(events, NOW - timedelta(hours=1), NOW, session)

    def test_usage_counts_final_client_requests_not_attempts(self):
        batch = collect(log("503 | 15ms | upstream execution failed: provider=claude model=fixture auth=secret err=private"),
                        log("503 | 15ms | upstream execution failed: provider=claude model=fixture auth=secret err=private"), final())
        report = self.report(batch)
        self.assertEqual(values(report)["requests"], 1)
        self.assertIn("not token usage, billing, or a count of upstream retry attempts", usage.text(report))

    def test_usage_excludes_models_count_tokens_and_websocket_upgrades(self):
        batch = collect(final(), final(path="/v1/models", method="GET"),
                        final(path="/v1/messages/count_tokens"), final(101, path="/v1/responses", method="GET"),
                        final(path="/v1/images/generations"), final(path="/healthz", method="GET"))
        result = values(self.report(batch))
        self.assertEqual(result["requests"], 1)
        self.assertEqual([result["excluded-" + k] for k in ("models", "count-tokens", "websocket", "other")], [1, 1, 1, 2])
        for path in gateway_events.INFERENCE_PATHS:
            self.assertEqual(values(self.report(collect(final(path=path))))["requests"], 1)

    def test_usage_includes_unknown_attribution_bucket(self):
        report = self.report(collect(final(429), final(402), final(403), final(502)))
        buckets = {f.code: f.value for f in report.facts if f.subject_kind == "usage-bucket"}
        self.assertEqual((buckets["provider"], buckets["selector"], buckets["requests"]), ("unknown", "unknown", 4))
        self.assertEqual((buckets["http-429"], buckets["http-402"], buckets["http-403"], buckets["http-5xx"]), (1, 1, 1, 1))

    def test_session_usage_unknown_without_exact_join(self):
        report = self.report(collect(log("session-affinity: model=fixture session=11111111"), final()), MID)
        self.assertIsNone(next(f.value for f in report.facts if f.code == "session-requests"))
        self.assertIn("Gateway-wide totals are shown separately", usage.text(report))

    def test_usage_initial_status_stream_caveat(self):
        report = self.report(collect(final(), log("upstream stream failed after content")))
        self.assertEqual(values(report)["requests"], 1)
        self.assertIn(usage.STREAM_CAVEAT, usage.text(report))
        self.assertIn(usage.STREAM_CAVEAT, report.json())
        # The exact caveat.
        self.assertEqual(usage.STREAM_CAVEAT, "Statuses are those sent before the response began; a stream "
                                              "that failed later counts as its initial status.")

    def test_management_disabled_no_logs_are_unknown(self):
        report = self.report(gateway_events.collect(gateway_events.LogWindow(), providers=()))
        self.assertIsNone(values(report)["requests"])
        self.assertEqual(report.coverage["journal"], "unavailable")
        self.assertIn("Requests: unknown", usage.text(report))

    def test_ranges_and_missing_timestamp(self):
        self.assertEqual(usage.parse_since("7d", NOW), NOW - timedelta(days=7))
        self.assertEqual(usage.parse_since("2026-09-30T11:00:00Z", NOW), NOW - timedelta(hours=1))
        for bad in ("0h", "31d", "100000h", "future", "2026-10-01T12:00:00Z", "2026-09-30 11:00:00"):
            with self.assertRaises(usage.errors.CLIError):
                usage.parse_since(bad, NOW)
        report = self.report(collect(final(when=None)))
        self.assertEqual(values(report)["requests"], 0)
        self.assertGreater(values(report)["incomplete"], 0)
        self.assertEqual(json.loads(report.json())["coverage"]["journal"], "partial")
