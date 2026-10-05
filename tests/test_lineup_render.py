"""The pure rendering helpers of ``lineup.py``.

Fixture catalog only; no state. ``tests/test_lineup.py`` covers the
request parser, live apply and propagation that extend the module.
"""

from __future__ import annotations

import unittest

from claude_multi import catalog, lineup, profile, settings
from _catalog import FIXTURE_ROOT


class LineupRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = catalog.load_catalog(FIXTURE_ROOT)
        self.lcat = profile.LineupCatalog.from_docs(self.bundle.docs)
        docs = self.bundle.docs
        self.eff = settings.effective(
            settings._default(),
            provider_ids=docs["providers"]["providers"],
            line_keys=(docs["models-v2"] if "models-v2" in docs else docs["models"])["models"],
        )

    def _lineup(self, document, *, ad_hoc=False):
        evaluation = profile.evaluate(
            document, self.lcat, bindings={}, effective=self.eff, ad_hoc=ad_hoc
        )
        self.assertEqual(evaluation.errors, ())
        return evaluation.lineup

    def test_binding_label_uses_display_or_the_key(self) -> None:
        self.assertEqual(
            lineup.binding_label({"display": "GPT-5.6 Sol"}, "high", key="sol"),
            "GPT-5.6 Sol · high",
        )
        self.assertEqual(lineup.binding_label(None, "max", key="gone"), "gone · max")

    def test_render_diff_aligns_labels(self) -> None:
        self.assertEqual(
            lineup.render_diff([("lead", "A · high", "B · xhigh")]),
            ["  lead                 A · high → B · xhigh"],
        )
        self.assertEqual(lineup.render_diff([]), [])

    def test_diff_rows_lead_change_and_unbound_agent(self) -> None:
        old = self._lineup(profile.ad_hoc_direct("sol", "high"), ad_hoc=True)
        new = self._lineup(profile.ad_hoc_direct("qwen38", "max"), ad_hoc=True)
        applied = {
            "lead": {"key": old.lead.binding.key, "effort": old.lead.binding.effort},
            "agents": {"cm-reviewer": {"key": "sol", "effort": "xhigh"}},
            "native_agents": new.relaunch_fields()["native_agents"],
            "workflows": new.relaunch_fields()["workflows"],
            "lead_providers": new.relaunch_fields()["lead_providers"],
            "settings_overrides": new.relaunch_fields()["settings_overrides"],
        }
        rows = lineup.diff_rows(applied, new, self.lcat)
        self.assertEqual(rows[0][0], "lead")
        self.assertIn("· high", rows[0][1])
        self.assertIn("· max", rows[0][2])
        reviewer = [row for row in rows if row[0] == profile.label("cm-reviewer")]
        self.assertEqual(len(reviewer), 1)
        self.assertEqual(reviewer[0][2], lineup.UNBOUND)

    def test_diff_rows_lead_class_row_only_when_given(self) -> None:
        new = self._lineup(profile.ad_hoc_direct("qwen38", "max"), ad_hoc=True)
        fields = new.relaunch_fields()
        applied = {
            "lead": {"key": new.lead.binding.key, "effort": new.lead.binding.effort},
            "agents": {},
            "native_agents": fields["native_agents"],
            "workflows": fields["workflows"],
            "lead_providers": fields["lead_providers"],
            "settings_overrides": fields["settings_overrides"],
        }
        self.assertEqual(lineup.diff_rows(applied, new, self.lcat), [])
        self.assertEqual(
            lineup.diff_rows(applied, new, self.lcat, old_lead_class="grok"),
            [("lead class", "grok", str(fields["lead_class"]))],
        )

    def test_render_text_direct_lineup_has_no_agents_and_the_help_line(self) -> None:
        direct = self._lineup(profile.ad_hoc_direct("qwen38", "max"), ad_hoc=True)
        text = lineup.render_text(direct, header="profile (ad-hoc direct)", generation=3)
        first = text.splitlines()[0]
        self.assertTrue(first.startswith("claude-multi · profile (ad-hoc direct) · lineup gen 3 · lead "))
        self.assertIn("none: this session has no cm-* agents", text)
        self.assertTrue(text.endswith("\n".join((lineup.HELP_LINE, *lineup.MODEL_HELP_LINES)) + "\n"))

    def test_render_text_profile_view_and_state_lines(self) -> None:
        direct = self._lineup(profile.ad_hoc_direct("qwen38", "max"), ad_hoc=True)
        view = lineup.render_text(direct, header="profile x", profile_view=True)
        self.assertNotIn("lineup gen", view)
        self.assertNotIn(lineup.HELP_LINE, view)
        text = lineup.render_text(
            direct,
            header="profile x (pinned)",
            pending={"reasons": ["lead class changes"]},
            lead_target={"key": "sol", "effort": "high", "selector": "gpt-multi-sol-high[1m]"},
            needs_choice="lead gone: retired",
            drift=["compaction 80 → 90"],
            managed_id="11111111-1111-4111-8111-111111111111",
            cat=self.lcat,
        )
        self.assertIn("lineup gen -", text)
        self.assertIn(
            "pending relaunch change: lead class changes — applies at the next resume "
            "(claude-multi -r 11111111-1111-4111-8111-111111111111)",
            text,
        )
        self.assertIn("requested lead: ", text)
        self.assertIn("(gpt-multi-sol-high[1m]) — switch with /model", text)
        self.assertIn("needs a lead choice: lead gone: retired — claude-multi -r", text)
        self.assertIn(
            "settings changed since launch: compaction 80 → 90 — applies at the next resume",
            text,
        )


if __name__ == "__main__":
    unittest.main()
