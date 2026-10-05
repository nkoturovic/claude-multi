"""``/cm`` help documents the ``/model`` rules on every record kind.

``lineup.MODEL_HELP_LINES`` follows every ``HELP_LINE`` append in
``lineup.py``: ``render_text``'s session view, ``show_text``'s 2.x-record
branch and its evaluation-failure branch ("documented in /cm help").
Fixture catalog and temp homes only.
"""

from __future__ import annotations

import copy
import unittest

from claude_multi import cli, lineup, profile

from test_lineup import FIXED, LineupCase


HELP_BLOCK = "\n".join((lineup.HELP_LINE, *lineup.MODEL_HELP_LINES)) + "\n"


class ModelHelpLinesTextTests(unittest.TestCase):
    def test_two_lines_the_s_rule_then_the_lead_set_rule(self) -> None:
        first, second = lineup.MODEL_HELP_LINES
        self.assertTrue(first.startswith("/model: press s to switch for this session only"))
        self.assertIn("~/.claude/settings.json", first)
        self.assertIn("Alt+P and /config block instead", second)
        for line in lineup.MODEL_HELP_LINES:
            self.assertNotIn("\n", line)

    def test_the_lead_set_line_is_byte_identical_to_the_card_and_direct_help(self) -> None:
        second = lineup.MODEL_HELP_LINES[1]
        self.assertIn(second + "\n", cli.QUICK_HELP)
        self.assertIn(second + "\n", cli.DIRECT_HELP)


class ModelHelpOnEveryRecordKindTests(LineupCase):
    def test_render_text_session_view_ends_with_the_help_block(self) -> None:
        lcat = self.runtime.lineup_catalog()
        lead = self.record["applied"]["lead"]
        document = profile.ad_hoc_direct(lead["key"], lead["effort"])
        evaluation = profile.evaluate(
            document, lcat, bindings={}, effective=self.runtime.current_effective(), ad_hoc=True
        )
        lineup_ = evaluation.lineup
        self.assertIsNotNone(lineup_, evaluation.errors)
        text = lineup.render_text(lineup_, header="profile (ad-hoc direct)", generation=1)
        self.assertTrue(text.endswith(HELP_BLOCK), text)
        view = lineup.render_text(lineup_, header="profile x", profile_view=True)
        for line in (lineup.HELP_LINE, *lineup.MODEL_HELP_LINES):
            self.assertNotIn(line, view)

    def test_show_on_a_v4_record_ends_with_the_help_block(self) -> None:
        code, out, err = self.request("")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith(HELP_BLOCK), out)

    def test_show_on_an_evaluation_failure_ends_with_the_help_block(self) -> None:
        record = self.store.load(self.mid)
        applied = copy.deepcopy(record["applied"])
        applied["lead"]["key"] = "gone-lead"
        self.mutate(self.mid, applied=applied)
        code, out, _ = self.request("")
        self.assertEqual(code, 0)
        self.assertIn("needs a lead choice:", out)
        self.assertTrue(out.endswith(HELP_BLOCK), out)


class ModelHelpOnA2xRecordTests(LineupCase):
    launch_profile = None

    def setUp(self) -> None:
        super().setUp()
        from test_launch_v4 import Gen0AndMigrationTests

        Gen0AndMigrationTests._v3_record(self)
        self.mid = self.rid = FIXED

    def test_show_on_a_2x_record_ends_with_the_help_block(self) -> None:
        code, out, _ = self.request("")
        self.assertIn("legacy record (version 3)", out)
        # The profiles block sits between the 2.x line and the help.
        self.assertIn(lineup.SHOW_2X_LINE.format(mid=FIXED) + "\n" + lineup.PROFILES_HEAD + "\n", out)
        self.assertIn(lineup.PROFILES_SWITCH + "\n" + HELP_BLOCK, out)
        self.assertTrue(out.endswith(HELP_BLOCK), out)


if __name__ == "__main__":
    unittest.main()
