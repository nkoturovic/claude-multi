"""Pure allowlisting, passive window semantics and redacted text."""

import dataclasses
import json
import unittest
from datetime import datetime, timedelta, timezone

from claude_multi import quota
from _layout import REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "management"
NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
SENTINELS = ("pii-email@example.invalid", "sk-ant-api03-SENTINEL", "/home/sentinel/",
             "label-SENTINEL", "name-SENTINEL", "id-SENTINEL", "acct-SENTINEL",
             "plan-SENTINEL", "private upstream body SENTINEL", "probe-SENTINEL",
             "note-SENTINEL", "project-SENTINEL", "claim-SENTINEL", "model-SENTINEL",
             "cooldown-SENTINEL", "future-SENTINEL", "dup-SENTINEL")


def parse(*entries):
    return quota.parse_auth_files(json.dumps({"files": entries}).encode())


def mixed():
    return quota.parse_auth_files((FIXTURES / "auth-files-mixed.json").read_bytes())


def command(pool):
    return quota.command_lines(pool, provider_by_pool={"claude": "anthropic", "codex": "openai"},
                               login_commands={"claude": "claude-multi providers sign-in anthropic", "codex": "claude-multi providers sign-in openai"},
                               restart_hint="RESTART", now=NOW, tz=timezone.utc)


def doctor(pool):
    return quota.doctor_report(pool, provider_by_pool={"claude": "anthropic", "codex": "openai"},
                              login_commands={"claude": "claude-multi providers sign-in anthropic", "codex": "claude-multi providers sign-in openai"},
                              restart_hint="RESTART", now=NOW, tz=timezone.utc)


class ParseAllowlistTests(unittest.TestCase):
    def test_exact_retained_fields_and_mixed_values(self):
        self.assertEqual([f.name for f in dataclasses.fields(quota.Credential)], [
            "handle", "provider", "status", "reason", "disabled", "unavailable",
            "next_retry_after", "success", "failed", "observed_at", "windows", "metadata", "model_quotas", "plan_type", "plan_source"])
        rows = mixed()
        self.assertEqual([c.handle for c in rows], ["claude#1", "claude#2", "codex#1", "codex#2", "gemini-cli#1"])
        self.assertEqual([quota.classify(c, NOW).level for c in rows], ["ok", "login", "high", "no-data", "no-data"])
        self.assertEqual(rows[0].observed_at.microsecond, 123456)
        self.assertEqual((rows[0].success, rows[0].failed), (41, 2))
        self.assertEqual([(w.label, w.used_percent) for w in rows[0].windows], [("5h", 42), ("7d", 18)])
        self.assertEqual(rows[4].reason, "other")

    def test_every_text_builder_and_repr_drop_sentinels(self):
        rows = mixed()
        texts = [repr(rows), repr(command(quota.PoolStatus("ok", read_at=NOW, credentials=rows))),
                 repr(doctor(quota.PoolStatus("ok", read_at=NOW, credentials=rows)))]
        for c in rows:
            texts += [repr(quota.classify(c, NOW)), quota.credential_text(c, login_command=None, now=NOW)]
            texts += [quota.window_text(w, c, NOW) for w in c.windows]
        for text in texts:
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, text)

    def test_normalization_strict_types_and_numbering(self):
        rows = parse({"provider": "CLAUDE", "status": "active"}, {"provider": "codex"},
                     {"provider": "claude"}, {"provider": "evil\n"},
                     {"provider": ["claude"], "status": [], "quota": [],
                      "disabled": "yes", "unavailable": 1, "success": True, "failed": 2**53})
        self.assertEqual([c.handle for c in rows], ["claude#1", "codex#1", "claude#2", "other#1", "other#2"])
        self.assertEqual(rows[-1].status, "other")
        self.assertFalse(rows[-1].disabled or rows[-1].unavailable)
        self.assertEqual((rows[-1].success, rows[-1].failed), (None, None))
        for value in (-1, 2**53, 1.2, "4", False, None):
            c, = parse({"success": value, "failed": value})
            self.assertEqual((c.success, c.failed), (None, None))
        c, = parse({"success": 2**53-1})
        self.assertEqual(c.success, 2**53-1)

    def test_closed_reason_and_status_enums(self):
        for raw, expected in quota.REASONS.items():
            c, = parse({"status_message": " " + raw.upper() + " "})
            self.assertEqual(c.reason, expected)
        for value in (None, [], 0, ""):
            self.assertIsNone(parse({"status_message": value})[0].reason)
        for status in ("active", "pending", "refreshing", "error", "disabled", "unknown"):
            self.assertEqual(parse({"status": status})[0].status, status)


class ParseErrorTests(unittest.TestCase):
    def test_structure_limits_and_no_error_value_leaks(self):
        cases = [p.read_bytes() for p in FIXTURES.glob("malformed-*.json")]
        cases += [b'{"files":[], "dup-SENTINEL":' + b"9"*5000 + b"}", b"\xff", b"{", b"{}", b"x" * (quota.MAX_BODY_BYTES + 1),
                  b'{"files":[], "dup-SENTINEL": NaN}',
                  json.dumps({"files": [], "dup-SENTINEL": "x"*65537}).encode(),
                  b'{"files":[], "dup-SENTINEL":' + b"["*20 + b"0" + b"]"*20 + b"}",
                  json.dumps({"files": [], "dup-SENTINEL": [0]*4097}).encode()]
        for body in cases:
            with self.subTest(size=len(body)):
                with self.assertRaises(quota.QuotaParseError) as caught:
                    quota.parse_auth_files(body)
                for sentinel in SENTINELS:
                    self.assertNotIn(sentinel, str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__ or caught.exception.__context__ is None)


class TimeTests(unittest.TestCase):
    def test_fractions_offsets_and_invalid(self):
        self.assertEqual(quota.parse_time("2026-10-01T12:00:00.123456789Z"), NOW.replace(microsecond=123456))
        self.assertEqual(quota.parse_time("2026-10-01T14:00:00+02:00"), NOW)
        for raw in (None, 0, {}, "garbage", "2026-10-01T12:00:00", "9999-12-31T23:59:59-23:59"):
            self.assertIsNone(quota.parse_time(raw))


class SignalTests(unittest.TestCase):
    def credential(self, signals, provider="codex", observed=NOW.isoformat()):
        return parse({"provider": provider, "quota": {"observed_at": observed, "signals": signals}})[0]

    def test_codex_labels_and_fallback_reset(self):
        for minutes, label in (("300", "5h"), ("10080", "7d"), ("2880", "2d"),
                               ("120", "2h"), ("17", "17m"), (None, "primary"), ("0", "primary")):
            c = self.credential({"X-Codex-Primary-Used-Percent": " 93.123 ",
                                 "x-codex-primary-window-minutes": minutes,
                                 "x-codex-primary-reset-after-seconds": "60"})
            self.assertEqual(c.windows[0].label, label)
            self.assertEqual(c.windows[0].used_percent, 93.123)
            self.assertEqual(c.windows[0].resets_at, NOW + timedelta(seconds=60))
        c = self.credential({"x-codex-primary-used-percent": "93", "x-codex-primary-reset-at": "1790870400",
                             "x-codex-primary-reset-after-seconds": "1"})
        self.assertEqual(c.windows[0].resets_at.hour, 16)
        c = self.credential({"x-codex-primary-used-percent": "93", "x-codex-primary-reset-after-seconds": "1"}, observed=None)
        self.assertIsNone(c.windows[0].resets_at)

    def test_claude_fraction_and_invalid_field_isolation(self):
        for raw, percent in (("0.899", 89.9), ("0.90", 90), ("10.0", 1000)):
            c = self.credential({"ANTHROPIC-RATELIMIT-UNIFIED-5H-UTILIZATION": raw,
                                 "anthropic-ratelimit-unified-5h-reset": "bad"}, "claude")
            self.assertAlmostEqual(c.windows[0].used_percent, percent)
            self.assertIsNone(c.windows[0].resets_at)
        # Exact decimal scaling: binary float 0.29 * 100 floors to 28.
        for raw, shown in (("0.29", 29), ("0.57", 57), ("0.58", 58), ("0.000001", 0), ("1", 100)):
            c = self.credential({"anthropic-ratelimit-unified-5h-utilization": raw}, "claude")
            self.assertEqual(c.windows[0].used_percent, shown if raw != "0.000001" else 0.0001)
            self.assertTrue(quota.window_text(c.windows[0], c, NOW).startswith(f"5h {shown}%"))
        for fraction in range(1001):
            raw = f"{fraction / 1000:.3f}"
            c = self.credential({"anthropic-ratelimit-unified-7d-utilization": raw}, "claude")
            self.assertTrue(quota.window_text(c.windows[0], c, NOW).startswith(f"7d {fraction // 10}%"), raw)
        for raw in (None, 0.9, True, "NaN", "inf", "10.1", "-1", "0.1234567", "1e0", "٠.٩"):
            c = self.credential({"anthropic-ratelimit-unified-5h-utilization": raw}, "claude")
            self.assertEqual(c.windows, ())
        c = self.credential({"x-codex-primary-used-percent": "bad", "x-codex-secondary-used-percent": "1000"})
        self.assertEqual([w.label for w in c.windows], ["secondary"])
        self.assertEqual(self.credential({"x-codex-primary-used-percent": "1001"}).windows, ())
        self.assertEqual(self.credential({"retry-after": "2", "x-codex-credits-balance": "12"}).windows, ())


class ClassifyTests(unittest.TestCase):
    def test_precedence_every_level_and_worst(self):
        base = mixed()[0]
        cases = [
            (dict(disabled=True, reason="unauthorized"), "disabled"),
            (dict(status="disabled"), "disabled"),
            (dict(reason="unauthorized"), "login"), (dict(reason="invalid_grant"), "login"),
            (dict(reason="token_expired"), "unusable"),
            (dict(status="error", unavailable=True, reason=None), "unusable"),
            (dict(status="error", unavailable=True, reason="other"), "unusable"),
            (dict(reason="payment_required"), "payment"), (dict(reason="quota_exhausted", windows=()), "exhausted"),
            (dict(windows=(quota.Window("5h", 90, None, timedelta(hours=5)),)), "high"),
            ({}, "ok"), (dict(observed_at=None), "no-data"), (dict(windows=()), "no-data"),
        ]
        findings = []
        for changes, level in cases:
            c = dataclasses.replace(base, **changes)
            self.assertEqual(quota.classify(c, NOW).level, level)
            findings.append(c)
        self.assertEqual(quota.worst(findings, NOW).level, "login")
        self.assertIsNone(quota.worst([], NOW))
        high = mixed()[2]
        self.assertEqual(quota.worst([base, high], NOW).credential, high)
        self.assertEqual(quota.worst([base, dataclasses.replace(base, handle="claude#2")], NOW).credential, base)

    def test_variant_fixtures_parse_to_one_unusable_codex_credential(self):
        for name, reason in (("token-expired", "token_expired"), ("unknown-error", "other")):
            with self.subTest(name):
                rows = quota.parse_auth_files((FIXTURES / f"{name}.json").read_bytes())
                self.assertEqual([(c.provider, c.reason) for c in rows], [("codex", reason)])
                finding = quota.classify(rows[0], NOW)
                self.assertEqual(finding.level, "unusable")
                text = repr(rows) + repr(finding) + quota.credential_text(rows[0], login_command=None, now=NOW)
                for sentinel in SENTINELS + ("SENTINEL",):
                    self.assertNotIn(sentinel, text)

    def test_boundaries_staleness_and_no_extrapolation(self):
        rows = quota.parse_auth_files((FIXTURES / "boundary-89-90.json").read_bytes())
        self.assertEqual([quota.classify(c, NOW).level for c in rows], ["ok", "high"])
        for c in rows:
            self.assertEqual(quota.classify(c, NOW + timedelta(hours=5)).level, "no-data")
        w = quota.Window("unknown", 93, None, None)
        self.assertTrue(quota.window_current(w, NOW, NOW + timedelta(hours=4)))
        self.assertFalse(quota.window_current(w, NOW, NOW + timedelta(hours=5)))
        self.assertFalse(quota.window_current(dataclasses.replace(w, resets_at=NOW), NOW, NOW))
        self.assertFalse(quota.window_current(dataclasses.replace(w, resets_at=NOW + timedelta(days=2)), None, NOW))
        self.assertFalse(quota.window_current(dataclasses.replace(w, resets_at=NOW + timedelta(days=2)), NOW - timedelta(days=3), NOW))


class FormatTests(unittest.TestCase):
    def test_age_and_reset(self):
        for seconds, long, short in ((-1, "just now", "now"), (59, "just now", "now"),
                                     (60, "1 min ago", "1m"), (3600, "1 h ago", "1h"),
                                     (172799, "47 h ago", "47h"), (172800, "2 d ago", "2d")):
            delta = timedelta(seconds=seconds)
            self.assertEqual(quota.format_age(delta), long)
            self.assertEqual(quota.format_age(delta, compact=True), short)
        for days, expected in ((0, "12:00"), (1, "Fri 12:00"), (6, "Wed 12:00"), (7, "2026-10-08 12:00")):
            self.assertEqual(quota.format_reset(NOW + timedelta(days=days), NOW, timezone.utc), expected)

    def test_missing_reset_exhausted_no_window_and_unknown_age(self):
        base = mixed()[0]
        for c, expected in (
            (dataclasses.replace(base, windows=(quota.Window("5h", 93, None, timedelta(hours=5)),)), "reset unknown"),
            (dataclasses.replace(base, reason="quota_exhausted", windows=(), observed_at=None), "no quota headers yet · observed age unknown"),
        ):
            text = quota.credential_text(c, login_command=None, now=NOW)
            self.assertIn(expected, text)
            self.assertIn("observed", text)
            self.assertIn(expected, "\n".join(command(quota.PoolStatus("ok", credentials=(c,)))[1]))
        self.assertIn("89%", quota.window_text(quota.Window("5h", 89.6, None, timedelta(hours=5)), base, NOW))
        stale = dataclasses.replace(base, observed_at=NOW-timedelta(days=3), windows=(quota.Window("7d", 93, NOW+timedelta(days=1), None),))
        self.assertIn("observed 3 d ago", quota.credential_text(stale, login_command=None, now=NOW))


class CommandLinesTests(unittest.TestCase):
    def test_exact_mixed_output(self):
        code, lines = command(quota.PoolStatus("ok", read_at=NOW, credentials=mixed()))
        self.assertEqual(code, 0)
        self.assertEqual("\n".join(lines), """Quota — local gateway, read 12:00 (passive: updated only by traffic; nothing stored)
Quota condition: available — the accounts' quota was read
anthropic (claude pool)
  claude#1  active    5h 42% (resets 16:00) · 7d 18% (resets Fri 09:00) · observed 12 min ago · 41 ok / 2 failed
    retry-after: elapsed · observed 12 min ago
    The provider reported Retry-After 7s.
    The client may wait silently; this is not proof that an agent has stopped.
  claude#2  error     credential rejected (unauthorized) — sign in again: claude-multi providers sign-in anthropic
    retry-after: unknown · observed age unknown
  Pool headroom: unknown
openai (codex pool)
  codex#1   active    7d 93%! (resets Mon 10:12) · observed 3 h ago · 90%+: openai: quota pressure; choose a profile that does not depend on this provider. For an existing session, use /cm or Sessions -> T.
    plan unknown (source unknown)
    credits: unknown provider credits · has-credits unknown · unlimited unknown
    retry-after: unknown · observed 3 h ago
  codex#2   active    5h reset since the observation · observed 7 h ago
    plan unknown (source unknown)
    credits: unknown provider credits · has-credits unknown · unlimited unknown
    retry-after: unknown · observed 7 h ago
  Pool headroom: unknown
other credentials (not a claude-multi provider): 1
Management safety: five failed logins on allowlisted routes can ban loopback for 30 min; refused routes and guard-refused browser/Host requests do not count.
Quota is passive metadata from gateway traffic; no provider request was made.
Management authentication failures on allowed paths can trigger the gateway's five-strike lockout; this command does not retry them.""")

    def test_remedies_follow_the_surface_and_provider(self):
        """The credential remedies are guidance() for
        the surface shown; never "P moves agents" on a resume card."""
        high = mixed()[2]
        exhausted = dataclasses.replace(mixed()[0], reason="quota_exhausted", windows=())
        for surface, remedy in (
            ("resume", "openai: quota pressure; change the lineup with /cm or Sessions -> T."),
            ("fresh", "openai: quota pressure; P chooses another profile for this new launch."),
        ):
            with self.subTest(surface=surface):
                lines = quota.command_lines(
                    quota.PoolStatus("ok", read_at=NOW, credentials=(high,)),
                    provider_by_pool={"codex": "openai"}, login_commands={}, restart_hint="R", now=NOW,
                    tz=timezone.utc, surface=surface)[1]
                self.assertEqual(lines[3], "  codex#1  active    7d 93%! (resets Mon 10:12) · observed 3 h ago · "
                                           f"90%+: {remedy}")
                self.assertNotIn("card's P", "\n".join(lines))
        text = quota.credential_text(exhausted, login_command=None, now=NOW, provider="anthropic", surface="resume")
        self.assertTrue(text.endswith(" — anthropic: quota pressure; change the lineup with /cm or Sessions -> T."),
                        text)

    def test_all_unavailable_states_state_and_condition_exit_one(self):
        for status in quota.STATES - {"ok"}:
            pool = quota.PoolStatus(status)
            code, lines = command(pool)
            self.assertEqual(code, 1)
            self.assertEqual(lines, [quota.state_text(pool, restart_hint="RESTART") or "quota: management read unavailable",
                                     quota.condition_line(quota.unavailable_reason(pool))])
        self.assertEqual(command(quota.PoolStatus("error", 500)),
                         (1, ["quota: the gateway answered HTTP 500", quota.condition_line("unavailable")]))
        self.assertIn("five failed key attempts on allowlisted routes", quota.state_text(quota.PoolStatus("refused"), restart_hint="RESTART"))


class DoctorReportTests(unittest.TestCase):
    def test_all_states_attention_info_or_silent_never_block(self):
        for status in quota.STATES - {"ok"}:
            with self.subTest(status=status):
                pool = quota.PoolStatus(status, key_problem="bad shape", remedy="REPAIR")
                attention, info, refresh_failures = doctor(pool)
                text = quota.state_text(pool, restart_hint="RESTART")
                expected = ([], []) if status in {"seam", "down"} else (
                    ([], [text]) if status in {"no-key", "disabled", "unavailable"} else ([text], []))
                self.assertEqual((attention, info), expected)
                self.assertEqual(refresh_failures, frozenset())
                self.assertNotIn("BLOCK", repr((attention, info)))
        self.assertEqual(doctor(quota.PoolStatus("error", 500))[0],
                         ["quota: the gateway answered HTTP 500"])

    def test_exact_credential_attention_texts_and_equivalent_refresh_only(self):
        base = mixed()[0]
        prefix = "quota: claude#1 (anthropic)"
        login = "claude-multi providers sign-in anthropic"
        fallback = "anthropic: quota pressure; choose a profile that does not depend on this provider. For an existing session, use /cm or Sessions -> T."
        cases = [
            ({"reason": "unauthorized"}, f"{prefix} credential rejected by the provider (unauthorized) — sign in again: {login}"),
            ({"reason": "invalid_grant"}, f"{prefix} credential rejected by the provider (invalid_grant) — sign in again: {login}"),
            ({"reason": "token_expired"}, f"{prefix} is unusable (access token expired and not refreshed) — if it persists, sign in again: {login}"),
            ({"status": "error", "unavailable": True}, f"{prefix} is unusable (access token expired and not refreshed) — if it persists, sign in again: {login}"),
            ({"reason": "payment_required"}, f"{prefix} refused by the provider (payment required or forbidden) — {fallback}"),
            ({"reason": "quota_exhausted", "windows": (), "observed_at": NOW}, f"{prefix} quota exhausted (observed just now) — {fallback}"),
            ({"windows": (quota.Window("5h", 93.9, None, timedelta(hours=5)),)},
             f"{prefix} at 93% of its 5h window (reset unknown; observed 12 min ago; passive) — {fallback}"),
            ({"windows": (quota.Window("7d", 90, NOW+timedelta(days=1), timedelta(days=7)),),
              "observed_at": NOW-timedelta(days=3)},
             f"{prefix} at 90% of its 7d window (resets Fri 12:00; observed 3 d ago; passive) — {fallback}"),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                attention, info, refresh_failures = doctor(quota.PoolStatus(
                    "ok", credentials=(dataclasses.replace(base, **changes),)))
                self.assertEqual(attention, [expected])
                self.assertEqual(len(info), 1)
                self.assertEqual(refresh_failures, frozenset({"claude"}) if changes.get("reason") == "invalid_grant" else frozenset())

    def test_summary_and_non_pool_credentials_never_warn(self):
        rows = mixed()
        other = dataclasses.replace(rows[-1], reason="invalid_grant")
        disabled = dataclasses.replace(rows[1], disabled=True, reason="invalid_grant")
        attention, info, refresh_failures = doctor(quota.PoolStatus("ok", credentials=(rows[0], disabled, rows[3], other)))
        self.assertEqual(attention, [])
        self.assertEqual(refresh_failures, frozenset())
        self.assertEqual(info, ["Quota (local gateway, passive): claude#1 5h 42% (resets 16:00) · "
                               "7d 18% (resets Fri 09:00) (12 min ago); claude#2 disabled; "
                               "codex#2 5h reset since the observation (7 h ago); other credentials: 1"])
        self.assertEqual(doctor(quota.PoolStatus("ok", credentials=parse({"provider": "claude"})))[1],
                         ["Quota (local gateway, passive): claude#1 no quota headers yet"])
        self.assertEqual(doctor(quota.PoolStatus("ok")),
                         ([], ["Quota (local gateway, passive): no credentials"], frozenset()))

    def test_healthy_access_token_does_not_cover_a_refresh_failure(self):
        rows = quota.parse_auth_files((FIXTURES / "auth-files-refresh-failing-active.json").read_bytes())
        attention, info, refresh_failures = doctor(quota.PoolStatus("ok", credentials=rows))
        self.assertEqual(attention, [])
        self.assertIn("claude#1 5h 42%", info[0])
        self.assertEqual(refresh_failures, frozenset())





class QuotaSurfaceTests(unittest.TestCase):
    def setUp(self):
        import _tui_fixture as fx
        self.fx = fx
        self.now = fx.FIXED_NOW
        self.logins = {"claude": "claude-multi providers sign-in anthropic", "codex": "claude-multi providers sign-in openai"}

    def credentials(self, **kwargs):
        return quota.parse_auth_files(self.fx.quota_body(**kwargs))

    def card(self, credentials, **kwargs):
        return quota.card_summary(quota.PoolStatus("ok", credentials=credentials),
                                  pools={"claude", "codex"}, login_commands=self.logins,
                                  now=self.now, tz=timezone.utc, **kwargs)

    def test_provider_cell_compacts_windows_and_handle_before_age(self):
        c = self.credentials(percent=42, age=timedelta(minutes=12))[0]
        c = dataclasses.replace(c, windows=(quota.Window("5h", 42, self.now + timedelta(hours=4),
                                                       timedelta(hours=5)), *c.windows))
        credentials = (c, dataclasses.replace(c, handle="claude#2"))
        full = quota.provider_cell(credentials, self.now)
        self.assertEqual(full, "claude#1 5h 42% · 7d 42% · 12m")
        for width in (36, 28, 22, 18, 12, 6):
            text = quota.provider_cell(credentials, self.now, width=width)
            self.assertLessEqual(len(text), width)
            self.assertTrue(text.endswith("12m"), text)
        self.assertEqual(quota.provider_cell(credentials, self.now, width=22), "5h 42% · 7d 42% · 12m")
        details = quota.provider_details(credentials, self.now, timezone.utc)
        self.assertIn("observed 12 min ago", details[0])
        self.assertIn("resets 16:00", details[0])
        self.assertEqual(details[1], "+1 more (claude-multi quota)")

    def test_provider_states_never_fabricate_percent_or_reset(self):
        for kwargs, label in ((dict(window=False), "no data"),
                              (dict(reason="quota exhausted", window=False, observed=False), "no data"),
                              (dict(reason="invalid_grant"), "sign-in!"),
                              (dict(reason="token expired"), "unusable!"),
                              (dict(reason="payment_required"), "payment!")):
            credentials = self.credentials(**kwargs)
            self.assertIn(label, quota.provider_cell(credentials, self.now))
        credentials = self.credentials(reset=False)
        self.assertIn("reset unknown", quota.provider_details(credentials, self.now)[0])
        old = dataclasses.replace(credentials[0], observed_at=self.now - timedelta(days=8))
        self.assertEqual(quota.provider_cell((old,), self.now), "reset · 8d")
        exhausted = self.credentials(window=False, observed=False, reason="quota exhausted")
        details = quota.provider_details(exhausted, self.now)
        self.assertIn("no quota headers yet", details[0])
        self.assertIn("age unknown", details[0])
        self.assertNotIn("%", details[0])
        self.assertEqual(quota.provider_cell(exhausted, self.now, width=6), "age ?")

    def test_card_high_age_reset_fresh_and_resume_remedies(self):
        for resume in (False, True):
            text, role = self.card(self.credentials(), resume=resume)
            self.assertEqual(role, "warn")
            self.assertIn("93%!", text)
            self.assertIn("3d old", text)
            self.assertIn("resets Fri 09:00", text)
            self.assertLessEqual(len(text), 67)
            self.assertIn("S Sessions → T" if resume else "P provider fallback", text)
            if resume:
                self.assertNotIn("P ", text)

    def test_card_missing_window_and_reset_and_nonwarning_states(self):
        self.assertIsNone(self.card(self.credentials(reason="quota exhausted", window=False, observed=False)))
        for resume in (False, True):
            self.assertIn("reset unknown", self.card(self.credentials(reset=False), resume=resume)[0])
        self.assertEqual(self.card(self.credentials(percent=42))[1], "dim")
        for reason in ("invalid_grant", "token expired"):
            text, role = self.card(self.credentials(reason=reason))
            self.assertIn("claude-multi providers sign-in anthropic", text)
            self.assertEqual(role, "warn")
        for kwargs in (dict(reason="payment_required"), dict(window=False)):
            self.assertIsNone(self.card(self.credentials(**kwargs)))
        c = dataclasses.replace(self.credentials()[0], disabled=True)
        self.assertIsNone(self.card((c,)))

    def test_card_restricts_to_actual_pools_and_ignores_unavailable_reads(self):
        for pools in (set(), {"unrelated"}):
            self.assertIsNone(quota.card_summary(quota.PoolStatus("ok", credentials=self.credentials()),
                              pools=pools, login_commands=self.logins, now=self.now))
        for state in quota.STATES - {"ok"}:
            self.assertIsNone(quota.card_summary(quota.PoolStatus(state), pools={"claude"},
                              login_commands=self.logins, now=self.now))

    def test_all_new_formatters_drop_sentinels(self):
        rows = mixed()
        texts = [quota.provider_cell(rows, NOW), repr(quota.provider_details(rows, NOW)),
                 repr(quota.card_summary(quota.PoolStatus("ok", credentials=rows), pools={"claude", "codex"},
                      login_commands=self.logins, now=NOW))]
        for text in texts:
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, text)

    def test_long_codex_login_keeps_unknown_age_and_complete_command(self):
        c = self.credentials(pool="codex", reason="invalid_grant", observed=False)[0]
        c = dataclasses.replace(c, handle="codex#256")
        text, role = self.card((c,))
        self.assertLessEqual(len(text), 67)
        self.assertIn("age unknown", text)
        self.assertIn("claude-multi providers sign-in openai", text)
        self.assertEqual(role, "warn")


class UnavailableReasonTests(unittest.TestCase):
    """Typed quota conditions: management off, unavailable, stale and
    exhausted are told apart; none of them advises another sign-in."""

    def test_backend_aware_health_source(self):
        for backend, needle in (("systemd", "service's journal"), ("on-demand", "instance logs"),
                                (None, "the service journal when supervised, the instance logs on demand")):
            with self.subTest(backend=backend):
                for state in ("no-key", "unavailable"):
                    text = quota.state_text(quota.PoolStatus(state), restart_hint="RESTART", backend=backend)
                    self.assertIn(needle, text)
                    self.assertNotIn("sign in", text)
        self.assertEqual(quota.command_lines(quota.PoolStatus("unavailable"), provider_by_pool={}, login_commands={},
                                             restart_hint="R", now=NOW, backend="on-demand")[1],
                         [quota.state_text(quota.PoolStatus("unavailable"), restart_hint="R", backend="on-demand"),
                          quota.condition_line("unavailable")])

    def test_every_state_has_one_typed_reason(self):
        for state in quota.STATES - {"ok"}:
            with self.subTest(state=state):
                reason = quota.unavailable_reason(quota.PoolStatus(state))
                self.assertIn(reason, ("management-disabled", "unavailable", "not-ours"))
                text = quota.state_text(quota.PoolStatus(state), restart_hint="RESTART") or ""
                self.assertNotIn("sign in", text)
        self.assertIsNone(quota.unavailable_reason(quota.PoolStatus("ok")))

    def test_conditions_of_a_readable_pool(self):
        rows = mixed()
        self.assertEqual(quota.condition(quota.PoolStatus("no-key"), NOW), "management-disabled")
        self.assertEqual(quota.condition(quota.PoolStatus("down"), NOW), "unavailable")
        self.assertEqual(quota.condition(quota.PoolStatus("ok", credentials=rows), NOW), "available")
        # Hours later the same passive windows are no longer current: stale, never zero.
        self.assertEqual(quota.condition(quota.PoolStatus("ok", credentials=rows), NOW + timedelta(days=40)), "stale")
        exhausted = [dataclasses.replace(c, reason="quota_exhausted", status="active", disabled=False,
                                         unavailable=False, observed_at=NOW,
                                         windows=(quota.Window("5h", 100, NOW + timedelta(hours=1), timedelta(hours=5)),))
                     for c in rows if c.provider == "claude"]
        self.assertTrue(exhausted)
        self.assertEqual(quota.condition(quota.PoolStatus("ok", credentials=tuple(exhausted)), NOW, pools=("claude",)),
                         "exhausted")


if __name__ == "__main__":
    unittest.main()


class QuotaEvidenceAndExhaustionTests(unittest.TestCase):
    def credential(self, signals=None, **fields):
        return parse({"provider": "codex", "status": "active", "disabled": False, "unavailable": False,
                      "quota": {"observed_at": NOW.isoformat(), "signals": signals or {}}, **fields})[0]

    def test_credits_and_plan_allowlist_drops_pii(self):
        c = self.credential({"x-codex-credits-has-credits": "false", "x-codex-credits-unlimited": "true",
                            "x-codex-credits-balance": "0.125", "x-codex-plan-type": "business",
                            "x-codex-credits-email": "private@example.invalid"},
                            id_token={"plan_type": "pro", "account_id": "private-account"})
        self.assertEqual((c.metadata.has_credits, c.metadata.unlimited, c.metadata.balance), (False, True, "0.125"))
        self.assertEqual((c.plan_type, c.plan_source), ("business", "reported header"))
        text = quota.report(quota.PoolStatus("ok", credentials=(c,)), NOW).json()
        self.assertNotIn("private", text)
        for plan in quota.PLAN_TYPES:
            self.assertEqual(self.credential(id_token={"plan_type": plan}).plan_type, plan)
        for invalid in ("PRIVATE", "enterprise@example.invalid", [], True):
            self.assertIsNone(self.credential({"x-codex-plan-type": invalid}, id_token={"plan_type": invalid}).plan_type)
        for invalid in (True, "yes", "1", "FALSE", " false "):
            self.assertIsNone(self.credential({"x-codex-credits-has-credits": invalid}).metadata.has_credits)
        for invalid in ("NaN", "Infinity", "-1", "1000000000000001", "1.1234567", 12):
            self.assertIsNone(self.credential({"x-codex-credits-balance": invalid}).metadata.balance)

    def test_plan_claim_and_header_facts_never_overwrite_each_other(self):
        c = self.credential(id_token={"plan_type": "pro"})
        facts = quota.report(quota.PoolStatus("ok", credentials=(c,)), NOW).facts
        chosen, = [f for f in facts if f.code == "plan-type"]
        header, = [f for f in facts if f.code == "quota-plan-type"]
        self.assertEqual(chosen.value, "pro")
        self.assertIsNone(chosen.observed_at, "decoded claims have no quota observation timestamp")
        self.assertIsNone(header.value)
        self.assertEqual(header.observed_at, NOW)

    def test_model_quota_keeps_its_own_timestamp(self):
        before = NOW - timedelta(hours=8)
        c = self.credential({"x-codex-primary-used-percent": "10"}, model_quotas={
            "fixture-wire": {"observed_at": before.isoformat(), "signals": {"x-codex-primary-used-percent": "100"}},
            "private@example.invalid": {"observed_at": NOW.isoformat()}, "/path/identity": {},
        })
        self.assertEqual(len(c.model_quotas), 1)
        self.assertEqual(c.model_quotas[0].observed_at, before)
        self.assertEqual(c.observed_at, NOW)
        self.assertEqual(quota.classify(c, NOW).level, "ok")
        document = quota.report(quota.PoolStatus("ok", credentials=(c,)), NOW).document()
        model = [f for f in document["facts"] if "-model-" in f["subject_id"]]
        self.assertTrue(any(f["freshness"] == "stale" for f in model))
        self.assertNotIn("private", repr(document))

    def test_missing_counter_is_unknown(self):
        for value in (None, True, -1, 2**53, "0", 0.0):
            c = self.credential(success=value, failed=value)
            self.assertIsNone(c.success)
            self.assertIsNone(c.failed)
        self.assertEqual(self.credential(success=0).success, 0)

    def test_retry_after_date_and_delta_validation(self):
        self.assertEqual(quota.retry_after("10", NOW), NOW + timedelta(seconds=10))
        self.assertEqual(quota.retry_after("Thu, 01 Oct 2026 12:00:00 GMT", NOW), NOW)
        for value in ("-1", "1.5", "2147483648", "Thu, 01 Oct 2026 12:00:00", "2026-10-01T12:00:00Z", True, None):
            self.assertIsNone(quota.retry_after(value, NOW))
        self.assertIsNone(quota.retry_after("10", None))
        c = self.credential({"retry-after": "10"}, next_retry_after=(NOW + timedelta(hours=1)).isoformat())
        self.assertNotEqual(c.metadata.retry_after, c.next_retry_after)

    def test_retry_after_text_is_relative_or_explicitly_elapsed(self):
        c = self.credential({"retry-after": "120"})
        for now, expected in ((NOW, "in 2m"), (NOW + timedelta(seconds=90), "in 30s"),
                              (NOW + timedelta(seconds=120), "elapsed"),
                              (NOW + timedelta(minutes=3), "elapsed")):
            with self.subTest(now=now):
                text = "\n".join(quota.metadata_lines(c, now))
                self.assertIn(f"retry-after: {expected} · observed ", text)
        self.assertIn("retry-after: unknown", "\n".join(quota.metadata_lines(self.credential(), NOW)))

    def test_reported_retry_after_carries_the_silent_wait_caveat(self):
        """A reported Retry-After is shown with its
        duration and the two exact lines, in text and as a JSON fact reason."""

        c = self.credential({"retry-after": "120"})
        text = "\n".join(quota.metadata_lines(c, NOW))
        self.assertIn("The provider reported Retry-After 2m.\n"
                      "The client may wait silently; this is not proof that an agent has stopped.", text)
        self.assertEqual(quota.retry_after_duration(self.credential({"retry-after": "45"})), "45s")
        self.assertNotIn("The provider reported", "\n".join(quota.metadata_lines(self.credential(), NOW)))
        report = quota.report(quota.PoolStatus("ok", read_at=NOW, credentials=(c,)), NOW)
        reasons = [f.reason for f in report.facts if f.code == "retry-after-honesty"]
        self.assertEqual(reasons, ["client-may-wait-silently"])
        self.assertNotIn("retry-after-honesty", quota.report(
            quota.PoolStatus("ok", read_at=NOW, credentials=(self.credential(),)), NOW).json())

    def test_stale_quota_never_triggers_exhausted(self):
        c = self.credential({"x-codex-primary-used-percent": "100"}, status_message="quota exhausted")
        self.assertTrue(quota.pool_exhausted((c,), NOW))
        stale = dataclasses.replace(c, observed_at=NOW - timedelta(days=8))
        self.assertFalse(quota.pool_exhausted((stale,), NOW))
        self.assertEqual(quota.classify(stale, NOW).level, "no-data")

    def test_one_exhausted_credential_is_not_exhausted_pool(self):
        exhausted = self.credential({"x-codex-primary-used-percent": "100"})
        healthy = self.credential({"x-codex-primary-used-percent": "10"})
        self.assertFalse(quota.pool_exhausted((exhausted, healthy), NOW))
        self.assertFalse(quota.pool_exhausted((exhausted, self.credential()), NOW))
        self.assertTrue(quota.pool_exhausted((exhausted,), NOW))
        self.assertFalse(quota.pool_exhausted((), NOW))
