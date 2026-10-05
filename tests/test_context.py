"""Tests for the context-policy constants the compiler uses (composition.py).

The context math itself is ``compiler.lead_set_context``
(test_compiler_v2).
"""

from __future__ import annotations

import unittest

from claude_multi import catalog, composition
from _catalog import FIXTURE_ROOT


CATALOG_ROOT = FIXTURE_ROOT


def _scalar_tokens(model_id: str) -> int | None:
    bundle = catalog.load_catalog(CATALOG_ROOT)
    return bundle.lines[model_id]["context"]["scalar_tokens"]


class AutoCompactThresholdTests(unittest.TestCase):
    def test_pinned_client_exact_reactive_thresholds(self) -> None:
        self.assertEqual(composition.auto_compact_trigger(1_000_000), 882_000)
        self.assertEqual(composition.auto_compact_trigger(983_616), 867_254)
        self.assertEqual(composition.auto_compact_trigger(372_000), 316_800)
        self.assertEqual(composition.auto_compact_trigger(272_000), 226_800)

    def test_capacity_too_small_for_fixed_reservations_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            composition.CompositionError, "too small"
        ):
            composition.auto_compact_trigger(33_000)


class OperatingWindowCeilingTests(unittest.TestCase):
    def test_ceiling_boundary_and_idempotence(self) -> None:
        self.assertEqual(composition.operating_window(800_000), 800_000)
        self.assertEqual(composition.operating_window(1_000_000), 800_000)
        self.assertEqual(composition.operating_window(983_616), 800_000)
        self.assertEqual(composition.operating_window(500_000), 500_000)
        self.assertEqual(composition.operating_window(258_400), 258_400)

    def test_capped_window_trigger_math(self) -> None:
        # An 800K operating window compacts reactively at 702K through
        # the unchanged client formula; the raw 1M arithmetic is untouched.
        self.assertEqual(
            composition.auto_compact_trigger(composition.operating_window(1_000_000)),
            702_000,
        )
        self.assertEqual(composition.auto_compact_trigger(1_000_000), 882_000)

    # The two explicit-scalar cases assert operating_window
    # on the fixture line's explicit scalar (compute_scalar is deleted).
    def test_explicit_scalar_above_ceiling_is_capped(self) -> None:
        scalar = _scalar_tokens("qwen38")
        self.assertGreater(scalar, composition.OPERATING_WINDOW_CEILING)
        self.assertEqual(composition.operating_window(scalar), 800_000)

    def test_explicit_scalar_below_ceiling_is_untouched(self) -> None:
        scalar = _scalar_tokens("gpt55")
        self.assertLess(scalar, composition.OPERATING_WINDOW_CEILING)
        self.assertEqual(composition.operating_window(scalar), 258400)


if __name__ == "__main__":
    unittest.main()
