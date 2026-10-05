"""Synthetic per-request hint-header branches; policy stays OFF.

H6 is only a version precondition. None of these synthetic request shapes
substitutes for the hint-client check's completed native-client capture by
request class (tests/test_gateway_hint_client.py). Forwarded
session/agent/parent identities remain privacy evidence, not approval to
enable CLAUDE_CODE_GATEWAY_HINT_HEADERS.
"""
import copy
import json
import unittest
import uuid
from unittest import mock

from claude_multi import catalog
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog
import _gateway_harness as harness

MODULE = "tests.test_gateway_hints"
PREFIX = "x-claude-code-"
IDENTITY_NAMES = {PREFIX + name for name in ("session-id", "agent-id", "parent-agent-id")}
DROPPED_NAMES = {PREFIX + name for name in ("request-class", "agent-type", "prev-tool-durations")}
PRECONDITION = (
    "H1 precondition: the fixture pinned version must be in the gateway baseline's release line "
    "(same major.minor, patch >= baseline: P claude_device_profile.go:23,218-222); "
    "a gateway or fixture re-pin broke it; see HV1/HV2 and H6"
)


def tearDownModule():
    harness.EVIDENCE.finalize_module(MODULE)


def next_minor(version):
    major, minor, _ = map(int, version.split("."))
    return f"{major}.{minor + 1}.0"


def signals(version):
    return ({"x-app": "cli", "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14",
             "User-Agent": f"claude-cli/{version} (external, cli)"},
            {"metadata": {"user_id": json.dumps({"device_id": "a" * 64, "account_uuid": "",
                                                 "session_id": str(uuid.uuid4())})}})


def hints():
    nonce = uuid.uuid4().hex
    return {PREFIX + "session-id": str(uuid.uuid4()), **{
        PREFIX + name: f"gwtesthint-{tag}-{nonce}" for name, tag in (
            ("agent-id", "agent"), ("parent-agent-id", "parent"), ("request-class", "rc"),
            ("agent-type", "at"), ("prev-tool-durations", "ptd"))}}


class HintShapeTests(unittest.TestCase):
    def test_synthetic_signals_and_hint_markers(self):
        headers, body = signals("8.9.10")
        values = hints()
        self.assertEqual(set(values), IDENTITY_NAMES | DROPPED_NAMES)
        self.assertEqual(next_minor("8.9.10"), "8.10.0")
        self.assertEqual(headers["x-app"], "cli")
        self.assertNotIn("Anthropic-Dangerous-Direct-Browser-Access", headers)
        identity = json.loads(body["metadata"]["user_id"])
        self.assertEqual(identity["device_id"], "a" * 64)
        # A value moved to an arbitrary, normally uncaptured header still leaks.
        for value in values.values():
            for captured in (harness.hint_markers(["prefix " + value], b"{}"),
                             harness.hint_markers([], json.dumps({"nested": value}).encode())):
                self.assertTrue(any(value in marker for marker in captured))
        self.assertFalse(harness.hint_markers(["Bearer dummy-gwtest-test"], b"{}"))

    def test_first_duplicate_header_value_is_not_hidden(self):
        from email.message import Message
        headers = Message()
        headers["X-Repeated"] = "gwtesthint-first"
        headers["X-Repeated"] = "last"
        self.assertIn("gwtesthint-first", harness.hint_markers((v for _, v in headers.items()), b"{}"))

    def test_failed_request_is_recorded_before_asserting(self):
        case = HintTests()
        case.gateway = mock.Mock()
        case.gateway.request.return_value = harness.Reply(502, {}, b"{}")
        with mock.patch.object(harness, "EVIDENCE", harness.Evidence()):
            with self.assertRaises(AssertionError):
                case.check("H1-json", "8.9.10", "identity")
            row = harness.EVIDENCE.tables[MODULE]["H"][0]
            self.assertEqual((row["status"], row["hits"], row["branch"]), (502, 0, "unresolved"))
            self.assertEqual(row["hint_names"], [])

    def test_all_hint_cells_are_required_for_publication(self):
        required = harness.REQUIRED_EVIDENCE[MODULE]["H"]
        self.assertEqual(len(required), 13)
        document = {"schema": "gwtest-evidence-v1", "module": MODULE,
                    "binary": {"realpath": "/nix/store/fixture/bin/cli-proxy-api", "version": "7.3.15"},
                    "tables": {"H": [{"row": row} for row in required]}}
        harness.validate_evidence_document(document, MODULE)
        for row in required:
            changed = copy.deepcopy(document)
            changed["tables"]["H"] = [item for item in changed["tables"]["H"] if item["row"] != row]
            with self.subTest(row=row), self.assertRaisesRegex(AssertionError, "missing required evidence rows"):
                harness.validate_evidence_document(changed, MODULE)


class HintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = harness.main_harness()
        cls.version = catalog.load_catalog(FIXTURE_ROOT).docs["native-contract"]["verified"][0]["version"]

    def check(self, row, version, expected, *, stream=False, family="claude", omit_app=False, helper=False):
        alias = {"claude": "gwtest-cc-output-config-high", "codex": "gwtest-codex-reasoning-effort-high",
                 "compat": "gwtest-compat-plain"}[family]
        headers, body = signals(version)
        if omit_app:
            del headers["x-app"]
        if helper:
            headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
        sent = hints()
        reply = self.gateway.request(alias, stream=stream, headers={**headers, **sent}, body=body)
        hit = reply.hits[0] if len(reply.hits) == 1 else None
        names = {name for name in hit.header_names if name.startswith(PREFIX)} if hit else set()
        branch = (("identity" if "anthropic-dangerous-direct-browser-access" in hit.header_names
                   else "preserve") if family == "claude" else "allowlist") if hit else "unresolved"
        markers = hit.hint_markers if hit else frozenset()
        dropped_leak = any(sent[name] in marker for name in DROPPED_NAMES for marker in markers)
        any_leak = any(value in marker for value in sent.values() for marker in markers)
        harness.EVIDENCE.record(MODULE, "H", {
            "row": row, "family": family, "request_class": "helper-shaped" if helper else "main-shaped",
            "classification": "synthetic branch observation", "hint_header_verdict": "off",
            "version_precondition_only": row == "H6", "client_version": version,
            "gateway_variant": "ua" if row.startswith("HV") else "main", "stream": stream,
            "status": reply.status, "hits": len(reply.hits), "branch": branch, "hint_names": sorted(names),
            "value_leak": any_leak, "dropped_value_leak": dropped_leak,
        })
        self.assertEqual(reply.status, 200)
        self.assertEqual(len(reply.hits), 1)
        self.assertEqual(hit.route, self.gateway.aliases[alias].route)
        self.assertEqual(branch, expected, PRECONDITION if row.startswith("H1-") else row)
        self.assertEqual(names, IDENTITY_NAMES if expected == "identity" else
                         IDENTITY_NAMES | DROPPED_NAMES if expected == "preserve" else set())
        if expected == "identity":
            self.assertFalse(dropped_leak, "dropped hint value leaked into upstream headers/body")
        elif expected == "allowlist":
            self.assertFalse(any_leak, "hint value leaked into upstream headers/body")
        else:
            self.assertTrue(dropped_leak)


class HintBranchTests(HintTests):
    def test_h1_h2_h3_h8_per_request_branches_stream_and_json(self):
        for stream in (False, True):
            for row, version, expected, options in (
                ("H1", self.version, "identity", {}),
                ("H2", next_minor(self.version), "preserve", {}),
                ("H3", self.version, "preserve", {"omit_app": True}),
                ("H8", self.version, "preserve", {"helper": True}),
            ):
                with self.subTest(row=row, stream=stream):
                    self.check(row + ("-stream" if stream else "-json"), version, expected, stream=stream, **options)

    def test_h4_h5_non_claude_routes_forward_no_hint_names_or_values(self):
        for row, family in (("H4", "codex"), ("H5", "compat")):
            with self.subTest(row=row):
                self.check(row, self.version, "allowlist", family=family)


@uses_shipped_catalog
class HintShippedVersionTests(HintTests):
    def test_h6_shipped_version_precondition_not_policy_verdict(self):
        version = catalog.load_catalog(SHIPPED_ROOT).docs["native-contract"]["verified"][0]["version"]
        self.check("H6", version, "identity")


class HintVariantBaselineTests(HintTests):
    @classmethod
    def setUpClass(cls):
        cls.gateway = harness.GatewayHarness("ua")
        cls.addClassCleanup(cls.gateway.close)
        cls.version = catalog.load_catalog(FIXTURE_ROOT).docs["native-contract"]["verified"][0]["version"]

    def test_hv1_hv2_user_agent_default_changes_baseline(self):
        self.check("HV1", next_minor(self.version), "identity")
        self.check("HV2", self.version, "preserve")


if __name__ == "__main__":
    unittest.main()
