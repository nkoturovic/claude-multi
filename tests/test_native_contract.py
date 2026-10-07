"""Tests for native-contract consumption and the skipped real probe."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from claude_multi import catalog, compiler, composition, pin, strict_json
from claude_multi.compiler import CompilerError
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog


# Behavioural consumption tests run on the frozen fixture; the promoted
# contract identity (upgrade-synced literal below) reads the shipped record.
CATALOG_ROOT = FIXTURE_ROOT
FIXED_ID = "11111111-1111-4111-8111-111111111111"
OLD_ID = "33333333-3333-4333-8333-333333333333"


def _docs(root: Path = CATALOG_ROOT):
    return catalog.load_raw(root)["docs"]


class ForkGuidanceTests(unittest.TestCase):
    def test_guidance_points_at_native_fork_and_link(self) -> None:
        self.assertIn("fork natively", compiler.FORK_UNVERIFIED_GUIDANCE)
        self.assertIn("--fork-session", compiler.FORK_UNVERIFIED_GUIDANCE)
        self.assertIn("sessions link", compiler.FORK_UNVERIFIED_GUIDANCE)


class PassthroughContractTests(unittest.TestCase):
    def test_structural_long_split_and_equals_rejected(self) -> None:
        for token in ("--model", "--model=x", "--agents={}", "--session-id=abc",
                      "--disallowedTools=Agent(X)", "--disallowed-tools", "--settings=/tmp/s"):
            with self.subTest(token=token):
                with self.assertRaises(CompilerError):
                    compiler.validate_passthrough([token])

    def test_plugin_and_fallback_rejected(self) -> None:
        for token in ("--plugin-url", "--plugin-url=https://x", "--plugin-dir",
                      "--fallback-model", "--fallback-model=x"):
            with self.subTest(token=token):
                with self.assertRaises(CompilerError):
                    compiler.validate_passthrough([token])

    def test_short_forms_rejected(self) -> None:
        for token in ("-c", "-r", "-rABC", "-r=ABC", "-n", "-nNAME"):
            with self.subTest(token=token):
                with self.assertRaises(CompilerError):
                    compiler.validate_passthrough([token])

    def test_prompt_flags_rejected(self) -> None:
        # Lead delivery is contingency-only, so the prompt flags are always
        # launcher-owned.
        for token in ("--system-prompt", "--append-system-prompt",
                      "--append-system-prompt-file"):
            with self.subTest(token=token):
                with self.assertRaises(CompilerError):
                    compiler.validate_passthrough([token])

    def test_safe_arguments_pass_byte_order_preserving(self) -> None:
        args = ["--verbose", "--debug=x", "positional", "-p", "print me"]
        self.assertEqual(compiler.validate_passthrough(args), args)

    def test_rejection_gives_ownership_guidance(self) -> None:
        with self.assertRaises(CompilerError) as raised:
            compiler.validate_passthrough(["--model"])
        self.assertIn("composition lead/model", str(raised.exception))


@uses_shipped_catalog
class PromotedContractConsumptionTests(unittest.TestCase):
    """The promoted 2.1.292 record changes no fail-closed consumption."""

    def test_promoted_record_identity(self) -> None:
        record = _docs(SHIPPED_ROOT)["native-contract"]
        self.assertEqual(pin.version(record), "2.1.292")
        self.assertEqual(
            record["lifecycle_evidence"]["inspected_version"],
            pin.version(record),
        )

    def test_no_capability_or_acceptance_claimed_verified(self) -> None:
        record = _docs(SHIPPED_ROOT)["native-contract"]
        statuses = {
            entry["status"] for entry in record["capabilities"].values()
        }
        self.assertEqual(statuses, {"pending"})
        acceptance = {
            entry["status"] for entry in record["acceptance"].values()
        }
        # U5 (the availableModels fence) is acceptance-verified by the
        # offline delegation probe on the pinned binary; the contract folds in
        # the real-client probe verdicts (U13 = S3 FAIL, recorded failed with its
        # fallback); the rest stay unverified until their own acceptance runs.
        self.assertEqual(acceptance, {"unverified", "verified", "failed"})
        self.assertEqual(record["nested_subagents"]["decision"], "pending")
        self.assertEqual(
            {
                record["nested_subagents"]["depth_key"]["status"],
                record["nested_subagents"]["concurrency_key"]["status"],
            },
            {"unverified"},
        )


class RealProbeGateTests(unittest.TestCase):
    """The real Claude probe stays skipped in this workflow."""

    def test_probe_requires_explicit_gate_and_fixture(self) -> None:
        gated = os.environ.get("CM_RUN_NATIVE_CONTRACT") == "1"
        fixture = os.environ.get("CM_NATIVE_CONTRACT_FIXTURE")
        if not (gated and fixture):
            self.skipTest(
                "native probe disabled: requires CM_RUN_NATIVE_CONTRACT=1 and a "
                "proven loopback fixture; not authorized in this workflow"
            )
        self.fail("probe gate must not be enabled in this workflow")


if __name__ == "__main__":
    unittest.main()
