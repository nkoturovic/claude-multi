"""Byte-exact 80x24 goldens of the 3.0 screens over the fixture.

Each ``tests/goldens/tui/<name>-80x24.txt`` is ``tests/_tui_render.GOLDENS
[name]`` rendered in a fresh temp root; ``tests/bless.py`` writes them.  A
mismatch prints a unified diff and the bless command (``_golden``).
"""

from __future__ import annotations

import os
import shutil
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _tui_render
from claude_multi import views
from _catalog import GOLDENS_ROOT
from _golden import assertGolden

TUI_GOLDENS = GOLDENS_ROOT / "tui"


class TuiGoldenTests(unittest.TestCase):
    def test_every_golden_matches(self) -> None:
        for name in _tui_render.GOLDENS:
            with self.subTest(name=name):
                assertGolden(
                    self,
                    TUI_GOLDENS / _tui_render.golden_name(name),
                    _tui_render.build(name).encode("utf-8"),
                )

    def test_the_golden_set_is_exactly_the_builders(self) -> None:
        expected = {_tui_render.golden_name(name) for name in _tui_render.GOLDENS}
        present = {path.name for path in TUI_GOLDENS.glob("*.txt")}
        self.assertEqual(present, expected)


class WideFrameTests(unittest.TestCase):
    def test_every_main_screen_is_dumped_wide_and_uses_the_width(self) -> None:
        width, height = _tui_render.WIDE
        for name in _tui_render.WIDE_SCREENS:
            with self.subTest(name=name):
                lines = _tui_render.build(name).splitlines()
                self.assertEqual(len(lines), height)
                self.assertTrue(all(len(line) <= width for line in lines))
                # Laid out for the whole terminal: the key bar sits on its last row.
                self.assertTrue(lines[-1].strip(), name)
                self.assertNotIn("too small", "\n".join(lines))


class TuiGoldenDeterminismTests(unittest.TestCase):
    def test_every_golden_renders_identically_in_two_roots(self) -> None:
        for name, builder in _tui_render.GOLDENS.items():
            frames = []
            for _ in range(2):
                root = Path(tempfile.mkdtemp(prefix=f"claude-multi-tui-det-{name}-"))
                try:
                    frames.append(builder(root))
                finally:
                    shutil.rmtree(root, ignore_errors=True)
            with self.subTest(name=name):
                self.assertEqual(frames[0], frames[1])
                self.assertNotIn(tempfile.gettempdir(), frames[0])


class QuotaGoldenTests(unittest.TestCase):
    def test_quota_frames_are_timezone_independent(self):
        # The new builders fix both their observation clock and reset timezone.
        try:
            for name in (n for n in _tui_render.GOLDENS if "quota" in n):
                frames = []
                for zone in ("UTC0", "EST5EDT"):
                    with mock.patch.dict(os.environ, {"TZ": zone}):
                        time.tzset()
                        frames.append(_tui_render.build(name))
                self.assertEqual(frames[0], frames[1], name)
        finally:
            time.tzset()

    def test_80_column_age_and_action_survive_fitting(self):
        providers = _tui_render.build("providers-quota")
        self.assertIn("5h 42% · 7d 18% · 12m", providers)
        self.assertIn(views.SIGNIN_REMEDY, providers)
        self.assertIn("+1 more (claude-multi quota)", providers)
        for name in (n for n in _tui_render.GOLDENS if n.startswith("card-quota")):
            text = _tui_render.build(name)
            if "exhausted" in name:
                self.assertFalse(any(line.startswith("  quota ") for line in text.splitlines()),
                                 "unknown-age exhaustion is not a current quota hint")
                self.assertIn("Status  Ready", text)
                continue
            row = next(line for line in text.splitlines() if line.startswith("  quota "))
            self.assertNotIn("…", row, name)
            self.assertIn("age unknown" if "exhausted" in name else "3d old", row)
            self.assertIn("S Sessions → T" if "resume" in name else "P provider fallback", row)
            # A quota warning never blocks: the card can launch and asks for a look.
            self.assertIn("Status  Attention", text)
            if "resume" in name:
                self.assertNotIn("P ", row)


if __name__ == "__main__":
    unittest.main()


class OperatorGoldens(unittest.TestCase):
    def check_frame(self, name, *needles):
        text = _tui_render.build(name)
        for needle in needles:
            self.assertIn(needle, text)
        assertGolden(self, TUI_GOLDENS / _tui_render.golden_name(name), text.encode())

    def test_registry_candidates_hidden_older_unknown_date_golden(self):
        self.check_frame("candidates-registry-detail", "visibility: hide", "older candidate(s) hidden", "created unknown")

    def test_gateway_down_candidates_golden(self):
        self.check_frame("candidates-gateway-down", "Gateway observation unavailable")

    def test_served_extras_unattributed_ids_golden(self):
        self.check_frame("candidates-served-extras", "fixture-unattributed", "unattributed")

    def test_provider_route_approval_consent_golden(self):
        self.check_frame("provider-route-approval", "Approve this route?", "value not shown")

    def test_changed_route_refusal_golden(self):
        self.check_frame("changed-route-refusal", "route changed", "providers approve acme")

    def test_qualification_failure_stale_evidence_golden(self):
        self.check_frame("qualification-failure-stale", "failed", "stale or none")
