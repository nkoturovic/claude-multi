"""One quota fixture through every surface: each typed condition
(available, stale, exhausted, management-disabled, unavailable, not-ours)
is named by the same line on the command line, in /cm quota and in the
Providers details, and the exit policy follows the condition: the command
exits 1 exactly when no reading was made, /cm quota always 0. The pool
reads are injected; no gateway, management key or provider is contacted."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import claude_multi
import test_cli
from test_tui import FakeWindow
from claude_multi import cli, quota, tui
import claude_multi.cli.consent as consent
import claude_multi.cli.screens.providers as providers_screen

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
SESSION = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"
ESC = "\x1b"


def _credential(*, used: float, observed: datetime) -> quota.Credential:
    window = quota.Window("5h", used, NOW + timedelta(hours=2), timedelta(hours=5))
    return quota.Credential("claude#1", "claude", "active", None, False, False, None, 3, 0, observed, (window,))


# The shared fixture: one pool per typed condition.
POOLS = {
    "available": quota.PoolStatus("ok", read_at=NOW, credentials=(_credential(used=40, observed=NOW),)),
    "stale": quota.PoolStatus("ok", read_at=NOW,
                              credentials=(_credential(used=40, observed=NOW - timedelta(days=40)),)),
    "exhausted": quota.PoolStatus("ok", read_at=NOW, credentials=(_credential(used=100, observed=NOW),)),
    "management-disabled": quota.PoolStatus("no-key"),
    "unavailable": quota.PoolStatus("down"),
    "not-ours": quota.PoolStatus("not-ours"),
}


class QuotaConditionTests(test_cli.OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        for name in consent.SESSION_MARKERS:
            self.runtime.environ.pop(name, None)
        clock = mock.patch.object(quota, "_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def pool(self, name: str):
        return mock.patch.object(self.runtime, "pool_status", return_value=POOLS[name])

    def test_the_fixture_covers_every_condition(self) -> None:
        self.assertEqual(set(POOLS), set(quota.CONDITION_TEXT))
        self.assertEqual(set(POOLS), set(quota.CONDITION_EXIT))
        for name, pool in POOLS.items():
            with self.subTest(condition=name):
                self.assertEqual(quota.condition(pool, NOW, pools=("claude",)), name)

    def test_the_command_names_each_condition_and_exits_by_it(self) -> None:
        for name in POOLS:
            with self.subTest(condition=name), self.pool(name):
                code, out, _err = self.op(["quota"])
                self.assertEqual(code, quota.CONDITION_EXIT[name])
                self.assertIn(quota.condition_line(name), out.splitlines())
                code, out, _err = self.op(["quota", "--json"])
                self.assertEqual(code, quota.CONDITION_EXIT[name])
                facts = {fact["code"]: fact for fact in json.loads(out)["facts"]}
                self.assertEqual(facts["quota-condition"]["value"], name)

    def test_cm_quota_names_each_condition_with_status_zero(self) -> None:
        for name in POOLS:
            with self.subTest(condition=name), self.pool(name):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = cli.main(["lineup", "--session", SESSION, "quota"], runtime=self.runtime,
                                    output_stream=out)
                self.assertEqual((code, err.getvalue()), (0, ""))
                self.assertIn(quota.condition_line(name), out.getvalue().splitlines())

    def test_the_tui_shows_each_condition(self) -> None:
        shown: list[tuple[str, list[str]]] = []
        real = tui.TextView

        class RecordingView(real):
            def __init__(self, title, lines, **kwargs):
                shown.append((title, list(lines)))
                super().__init__(title, lines, **kwargs)

        for name in POOLS:
            with self.subTest(condition=name), self.pool(name), \
                    mock.patch.object(claude_multi.tui, "TextView", RecordingView):
                shown.clear()
                screen = providers_screen._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE,
                                                           journal=lambda: None, now=NOW, tz=timezone.utc)
                screen.selected = [row.id for row in screen.rows].index("anthropic")
                screen.run(FakeWindow(["q", ESC, ESC], height=24, width=80))
                self.assertIn(quota.condition_line(name), shown[0][1])


if __name__ == "__main__":
    unittest.main()
