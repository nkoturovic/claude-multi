"""The qualification battery (``claude_multi.qualify``) and
``claude-multi models qualify`` — plan and consent, variants, caps, verdicts,
context honesty, the evidence merge, contract identities and the
exact-client check.

Every provider-bound request goes through an injected seam (the
``Runtime.qualify_http`` fake or local fake servers on port 0; never
8317/8316). No test pins a shipped model id: lines are fixture-local.
"""

from __future__ import annotations

import copy
import io
import json
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import test_cli  # module import: no test classes re-exported
import test_operator as operator_fixtures  # module import: no test classes re-exported
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT
from claude_multi import catalog, management, operator as operator_mod, pin, qualify, service, state, strict_json

CONTRACTS = qualify.ContractIdentity("c" * 64, "d" * 64)
OTHER_CONTRACTS = qualify.ContractIdentity("c" * 64, "e" * 64)
AT = "2026-09-30T00:00:00Z"


def _entry(efforts=None, *, selector=None, declared=200000):
    entry = {
        "efforts": efforts or {"high": {"selector": "custom-x-high", "proxy_contract": "output-config-high"},
                               "max": {"selector": "custom-x-max", "proxy_contract": "output-config-max"}},
        "default_effort": "high",
        "context": {"declared_tokens": declared, "provider_tokens": declared, "client_tokens": 200000,
                    "validated_tokens": min(declared, 200000)},
    }
    if selector is not None:
        entry["selector"] = selector
    return entry


PROVIDER = {"transport": {"kind": "direct", "base_url": "https://api.example.test/anthropic",
                          "auth": {"kind": "bearer", "secret_ref": "env:EXAMPLE_API_KEY"}},
            "adapter": "cliproxy-claude-compatible-v1"}
POOL = {"transport": {"kind": "oauth-pool", "pool": "claude"}, "adapter": "cliproxy-oauth-claude-v1"}


def _plan(checks=qualify.AGENT_CHECKS, **kw):
    kw.setdefault("provider", PROVIDER)
    entry = kw.pop("entry", _entry())
    return qualify.build_plan("custom-x", entry, digest="a" * 64, provider_id="example", checks=checks, **kw)


class _Gateway:
    """A fake well-behaved gateway; ``override`` maps a call label to a reply."""

    def __init__(self, override=None):
        self.override = dict(override or {})
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, call, body):
        document = json.loads(body)
        self.calls.append((call.label(), document))
        if call.label() in self.override:
            return self.override[call.label()]
        return test_cli.fake_gateway_reply(body)


# ------------------------------------------------------------ plan and consent
class ConsentTests(unittest.TestCase):
    def test_plan_names_every_possible_call(self) -> None:
        plan = _plan(checks=(*qualify.AGENT_CHECKS, qualify.CHECK_CONTEXT), context_tokens=150000)
        text = qualify.consent_text(plan)
        self.assertEqual(text, "\n".join([
            "Qualification plan for custom-x:",
            f"  definition: {'a' * 64}",
            "  route: https://api.example.test; auth bearer from EXAMPLE_API_KEY (value not shown)",
            "  1. smoke — alias custom-x-high, approximately 40 input tokens",
            "  2. effort high — alias custom-x-high, approximately 40 input tokens",
            "  3. effort max — alias custom-x-max, approximately 40 input tokens",
            "  4. tools (forced) step 1 — alias custom-x-high, approximately 150 input tokens",
            "  5. tools (forced) step 2 — alias custom-x-high, approximately 250 input tokens",
            "  6. stream — alias custom-x-high, approximately 40 input tokens",
            "  7. context ≈150000 — alias custom-x-high, approximately 150000 input tokens",
            "  tools variant: forced named tool (tool_choice {type: tool}, strict object schema); "
            "no other variant is tried.",
            "  tools step 2 runs only if step 1 returns the expected tool call.",
            "Caps: 120 s and 256 KiB per request; context 300 s and 256 KiB.",
            "No retries. max_tokens is sent but is not a reliable cost bound.",
            "Qualification records evidence only; it does not admit or enable agents.",
            "Proceed with these listed requests? [y/N] ",
        ]))
        # Every request the battery can send is a listed call: a full run
        # sends exactly the listed calls, never more.
        gateway = _Gateway()
        qualify.run_battery(plan, gateway)
        self.assertEqual([label for label, _ in gateway.calls], [call.label() for call in plan.calls])

    def test_agents_is_smoke_efforts_tools_stream_never_context_and_flags_compose(self) -> None:
        self.assertEqual(qualify.requested_checks(smoke=False, efforts=False, tools=False, stream=False,
                                                  context=None, agents=False), ("smoke",))
        self.assertEqual(qualify.requested_checks(smoke=True, efforts=False, tools=False, stream=False,
                                                  context=None, agents=True), qualify.AGENT_CHECKS)
        self.assertEqual(qualify.requested_checks(smoke=False, efforts=False, tools=True, stream=False,
                                                  context=9000, agents=False), ("tools", "context"))
        with self.assertRaisesRegex(qualify.QualifyError, "outside 8192..200000"):
            _plan(checks=("context",), context_tokens=300000)
        with self.assertRaisesRegex(qualify.QualifyError, "outside"):
            _plan(checks=("context",), context_tokens=100)

    def test_list_shaped_efforts_encode_the_client_effort(self) -> None:
        entry = _entry(["high", "max"], selector="claude-x-9[1m]", declared=1000000)
        plan = _plan(entry=entry, provider=POOL)
        calls = plan.calls_for("efforts")
        self.assertEqual([(c.alias, c.effort) for c in calls], [("claude-x-9", "high"), ("claude-x-9", "max")])
        body = json.loads(qualify.smoke_body(calls[1]))
        self.assertEqual(body["output_config"], {"effort": "max"})
        self.assertTrue(plan.exact_client)
        self.assertIn("exact-client check: the pinned Claude Code client runs offline", qualify.consent_text(plan))
        self.assertIn("route: the claude OAuth pool; auth an OAuth pool credential held by the gateway",
                      qualify.consent_text(plan))

    def test_selected_transport_is_the_consented_route(self) -> None:
        alternative = operator_mod.TRANSPORT_ALTERNATIVES[("anthropic", "api-key")]
        selection = operator_mod.TransportSelection("anthropic", "api-key", alternative, True)
        plan = _plan(provider=POOL, transport=selection)
        self.assertEqual(plan.route_text, "https://api.anthropic.com; auth header from PLATFORM_ANTHROPIC_API_KEY "
                                          "(value not shown)")


# ------------------------------------------------------------ variants
class VariantTests(unittest.TestCase):
    def test_forced_and_auto_variants_are_disclosed_and_never_retried(self) -> None:
        for variant, choice in ((qualify.TOOLS_FORCED, {"type": "tool", "name": qualify.TOOL_NAME}),
                                (qualify.TOOLS_AUTO, {"type": "auto"})):
            with self.subTest(variant=variant):
                plan = _plan(checks=("tools",), tools_variant=variant)
                self.assertIn(qualify.TOOL_VARIANT_TEXT[variant], qualify.consent_text(plan))
                gateway = _Gateway()
                (result,) = qualify.run_battery(plan, gateway)
                self.assertEqual((result.item, result.result), (variant, "pass"))
                first, second = (doc for _label, doc in gateway.calls)
                self.assertEqual(first["tool_choice"], choice)
                self.assertEqual(first["tools"][0]["input_schema"]["additionalProperties"], False)
                self.assertEqual(first["tools"][0]["input_schema"]["required"], ["nonce"])
                self.assertEqual(first["tools"][0].get("strict"), True if variant == "auto" else None)
                self.assertNotIn("tool_choice", second)
                # A rejection of the variant is a fact: one request, no
                # second turn and never the other variant.
                rejected = _Gateway({f"tools ({variant}) step 1": qualify.HttpResult(400, b"{}")})
                (result,) = qualify.run_battery(plan, rejected)
                self.assertEqual((result.result, result.reason, result.http), ("failed", "http-status", 400))
                self.assertEqual(len(rejected.calls), 1)

    def test_wrong_or_missing_tool_call_fails_without_a_second_turn(self) -> None:
        plan = _plan(checks=("tools",))
        text = json.dumps({"type": "message", "content": [{"type": "text", "text": "no"}]}).encode()
        gateway = _Gateway({"tools (forced) step 1": qualify.HttpResult(200, text)})
        (result,) = qualify.run_battery(plan, gateway)
        self.assertEqual((result.result, result.reason), ("failed", "no-tool-call"))
        wrong = json.dumps({"type": "message", "content": [
            {"type": "tool_use", "id": "t", "name": qualify.TOOL_NAME, "input": {"nonce": "guess"}}]}).encode()
        gateway = _Gateway({"tools (forced) step 1": qualify.HttpResult(200, wrong)})
        (result,) = qualify.run_battery(plan, gateway)
        self.assertEqual((result.result, result.reason, len(gateway.calls)), ("failed", "wrong-tool-call", 1))


# ------------------------------------------------------------ verdicts
class VerdictTests(unittest.TestCase):
    def test_outcomes_are_pass_failed_or_inconclusive(self) -> None:
        message = json.dumps({"type": "message", "content": []}).encode()
        cases = [
            (qualify.HttpResult(200, message), ("pass", "ok")),
            (qualify.HttpResult(200, b"{}"), ("failed", "not-a-message")),
            (qualify.HttpResult(400, b""), ("failed", "http-status")),
            (qualify.HttpResult(401, b""), ("failed", "http-status")),
            (qualify.HttpResult(302, b""), ("failed", "http-status")),
            (qualify.HttpResult(429, b""), ("inconclusive", "rate-limited")),
            (qualify.HttpResult(503, b""), ("inconclusive", "upstream-error")),
            (qualify.HttpResult(None, b"", "connection"), ("inconclusive", "connection")),
            (qualify.HttpResult(200, b"", "timeout"), ("inconclusive", "timeout")),
            (qualify.HttpResult(None, b"", "protocol"), ("inconclusive", "protocol")),
            (qualify.HttpResult(200, b"", "oversize"), ("failed", "oversize")),
        ]
        for response, expected in cases:
            with self.subTest(response=response):
                self.assertEqual(qualify.classify_message(response), expected)

    def test_stream_order_and_error_events(self) -> None:
        good = test_cli.fake_gateway_reply(json.dumps({"model": "m", "stream": True, "messages": [
            {"role": "user", "content": "x"}]}).encode())
        self.assertEqual(qualify.classify_stream(good), ("pass", "ok"))
        text = good.body.decode()
        self.assertEqual(qualify.classify_stream(qualify.HttpResult(200, text.rsplit("event: message_stop", 1)[0]
                                                                    .encode())), ("failed", "stream-order"))
        error = text + 'event: error\ndata: {"type": "error", "error": {"type": "overloaded_error"}}\n\n'
        self.assertEqual(qualify.classify_stream(qualify.HttpResult(200, error.encode())), ("failed", "stream-error"))
        orphan = ('data: {"type": "message_start", "message": {}}\n\n'
                  'data: {"type": "content_block_delta", "index": 0}\n\ndata: {"type": "message_stop"}\n\n')
        self.assertEqual(qualify.classify_stream(qualify.HttpResult(200, orphan.encode())), ("failed", "stream-order"))

    def test_the_terminal_message_delta_phase_and_block_indices(self) -> None:
        """B check (MAJOR): content after the terminal message_delta, a
        message_delta while a block is open, and malformed indices are
        stream-order failures (never a pass, never an exception)."""

        def stream(*events):
            raw = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
            return qualify.classify_stream(qualify.HttpResult(200, raw.encode()))

        start, stop = {"type": "message_start", "message": {}}, {"type": "message_stop"}
        delta = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}

        def block(index=0):
            return ({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": "ok"}},
                    {"type": "content_block_stop", "index": index})

        ping = {"type": "ping"}
        self.assertEqual(stream(start, *block(0), *block(1), delta, ping, stop), ("pass", "ok"))
        self.assertEqual(stream(start, *block(0), delta, dict(delta), stop), ("pass", "ok"))
        opened, moved, closed = block(0)
        for events in ((start, delta, *block(0), stop),  # content after the terminal delta
                       (start, *block(0), delta, opened, stop),
                       (start, opened, moved, delta, closed, stop),  # delta while a block is open
                       (start, *block(0), delta, start, stop)):
            with self.subTest(events=[event["type"] for event in events]):
                self.assertEqual(stream(*events), ("failed", "stream-order"))
        for index in ([0], {"i": 0}, None, -1, True, "0", 1.5):
            with self.subTest(index=index):
                self.assertEqual(stream(start, *block(index), stop), ("failed", "stream-order"))


# ------------------------------------------------------------ context honesty
class ContextTests(unittest.TestCase):
    def test_accepted_wrong_and_unknown_usage_preserve_floor(self) -> None:
        plan = _plan(checks=("context",), context_tokens=150000)
        (call,) = plan.calls
        corpus, label = qualify.context_corpus(150000, "n0nce")
        self.assertIn(f"Entry {label}: the verification code is n0nce.", corpus)
        self.assertAlmostEqual(len(corpus) / qualify.APPROX_CHARS_PER_TOKEN, 150000, delta=200)

        def reply(text, usage):
            body = {"type": "message", "content": [{"type": "text", "text": text}]}
            if usage is not None:
                body["usage"] = {"input_tokens": usage}
            return qualify.HttpResult(200, json.dumps(body).encode())

        correct = qualify.classify_context(reply("n0nce", 149321), "n0nce", 150000)
        self.assertEqual((correct.result, correct.retrieval, correct.measured), ("pass", "correct", 149321))
        self.assertEqual(qualify.context_floor(200000, 131072, correct), 149321)
        self.assertEqual(qualify.context_text("custom-x", correct, 131072, 149321),
                         "context custom-x: accepted 149321 input tokens; retrieval correct\nvalidated floor: 149321")
        wrong = qualify.classify_context(reply("other", 149321), "n0nce", 150000)
        self.assertEqual((wrong.result, wrong.reason), ("failed", "retrieval-wrong"))
        self.assertEqual(qualify.context_floor(200000, 131072, wrong), 131072)
        self.assertEqual(qualify.context_text("custom-x", wrong, 131072, 131072),
                         "context custom-x: accepted 149321 input tokens; retrieval failed\n"
                         "validated floor unchanged: 131072")
        unknown = qualify.classify_context(reply("n0nce", None), "n0nce", 150000)
        self.assertEqual((unknown.result, unknown.measured), ("pass", None))
        self.assertEqual(qualify.context_floor(200000, 131072, unknown), 131072)
        self.assertEqual(qualify.context_text("custom-x", unknown, 131072, 131072),
                         "context custom-x: accepted; retrieval correct; token count unavailable\n"
                         "validated floor unchanged: 131072")
        # Requested N is never stored as measured, and the floor is capped.
        self.assertIsNone(unknown.record(AT, CONTRACTS)["measured"])
        self.assertEqual(unknown.record(AT, CONTRACTS)["requested"], 150000)
        self.assertEqual(qualify.context_floor(140000, 100000, correct), 140000)
        # Never below the current floor.
        self.assertEqual(qualify.context_floor(200000, 180000, correct), 180000)

    def test_floor_reaches_the_resolved_line_only_for_the_current_definition(self) -> None:
        files = {"acme": operator_fixtures._fixture_files()["acme"]}
        layer = operator_fixtures._layer(files)
        line = layer.lines["custom-acme-large"]
        schemas = operator_fixtures._schemas()
        result = qualify.CheckResult("context", None, "pass", 200, "ok", requested=250000, measured=250000,
                                     measured_source="usage.input_tokens", retrieval="correct")
        document = operator_mod.merge_checks(None, digest=line.definition_digest, host="api.acme.example",
                                             versions={"launcher": "3.1.0", "client": "2.1.281", "gateway": "7.3.15"},
                                             results=[(("context",), result.record(AT, CONTRACTS))])
        evidence = operator_mod.parse_evidence(strict_json.canonical_file_bytes(
            {"version": 1, "lines": {"custom-acme-large": document}}), schemas.evidence)
        raised = operator_fixtures._layer(files, evidence=evidence).lines["custom-acme-large"]
        self.assertEqual(raised.core_entry["context"]["validated_tokens"], 250000)
        self.assertIn("validated 250000 by qualify 2026-09-30", raised.core_entry["context"]["qualification"])
        self.assertEqual(raised.definition_digest, line.definition_digest)
        stale = copy.deepcopy(document)
        stale["digest"] = "0" * 64
        evidence = operator_mod.parse_evidence(strict_json.canonical_file_bytes(
            {"version": 1, "lines": {"custom-acme-large": stale}}), schemas.evidence)
        kept = operator_fixtures._layer(files, evidence=evidence).lines["custom-acme-large"]
        self.assertEqual(kept.core_entry["context"]["validated_tokens"], 200000)


# ------------------------------------------------------------ evidence store
class EvidenceTests(unittest.TestCase):
    VERSIONS = {"launcher": "3.1.0", "client": "2.1.281", "gateway": "7.3.15"}

    def _merge(self, existing, *results, digest="a" * 64):
        return operator_mod.merge_checks(existing, digest=digest, host="api.example.test", versions=self.VERSIONS,
                                         results=[(path, record) for path, record in results])

    def test_inconclusive_never_replaces_a_pass_and_failed_replaces_only_its_check(self) -> None:
        passed = qualify.CheckResult("tools", "forced", "pass", 200, "ok").record(AT, OTHER_CONTRACTS)
        smoke = qualify.CheckResult("smoke", None, "pass", 200, "ok").record(AT, CONTRACTS)
        record = self._merge(None, (("tools", "forced"), passed), (("smoke",), smoke))
        transient = qualify.CheckResult("tools", "forced", "inconclusive", 429, "rate-limited").record(AT, CONTRACTS)
        after = self._merge(record, (("tools", "forced"), transient))
        self.assertEqual(after["checks"]["tools"]["forced"], passed)  # kept, contract-stale evidence
        failed = qualify.CheckResult("tools", "forced", "failed", 400, "http-status").record(AT, CONTRACTS)
        after = self._merge(after, (("tools", "forced"), failed))
        self.assertEqual(after["checks"]["tools"]["forced"]["result"], "failed")
        self.assertEqual(after["checks"]["smoke"], smoke)  # an unrelated check is untouched
        other = self._merge(after, (("stream",), smoke), digest="b" * 64)
        self.assertEqual(set(other["checks"]), {"stream"})  # another definition counts for nothing

    def test_evidence_view_states(self) -> None:
        levels = ("high",)
        passes = [(("smoke",), "smoke"), (("efforts", "high"), "efforts"), (("tools", "forced"), "tools"),
                  (("stream",), "stream")]

        def view(record, contracts=CONTRACTS, pool=False):
            evidence = operator_mod.OperatorEvidence(lines={"custom-x": record} if record else {})
            return operator_mod.evidence_view(evidence, "custom-x", digest="a" * 64, levels=levels,
                                              contracts=contracts.as_document(), pool_line=pool)

        record = self._merge(None, *[(path, qualify.CheckResult(check, None, "pass", 200, "ok").record(AT, CONTRACTS))
                                     for path, check in passes])
        self.assertEqual(view(record).state, "current")
        self.assertEqual(view(record, OTHER_CONTRACTS).state, "contract-stale")
        self.assertEqual(view(record, qualify.ContractIdentity("c" * 64, None)).state, "contract-stale")
        self.assertEqual(view(None).state, "missing")
        stale = dict(record, digest="b" * 64)
        self.assertEqual(view(stale).state, "definition-stale")
        partial = copy.deepcopy(record)
        del partial["checks"]["stream"]
        self.assertEqual((view(partial).state, view(partial).gaps), ("missing", ("stream",)))
        failed = self._merge(record, (("efforts", "high"),
                                      qualify.CheckResult("efforts", "high", "failed", 400, "http-status").record(
                                          AT, CONTRACTS)))
        self.assertEqual((view(failed).state, view(failed).gaps), ("failed", ("effort high",)))
        self.assertEqual(view(record, pool=True).exact_client, "missing")
        unavailable = self._merge(record, (("exact_client",), qualify.exact_client_result(
            qualify.ExactClientOutcome("inconclusive", "unavailable")).record(AT, CONTRACTS)))
        self.assertEqual(view(unavailable, pool=True).exact_client, "unavailable")
        exact = self._merge(record, (("exact_client",), qualify.exact_client_result(
            qualify.ExactClientOutcome("pass", "ok")).record(AT, CONTRACTS)))
        self.assertEqual(view(exact, pool=True).exact_client, "current")
        self.assertEqual(view(exact, OTHER_CONTRACTS, pool=True).exact_client, "contract-stale")

    def test_schema_mode_and_no_payload_material(self) -> None:
        schemas = operator_fixtures._schemas()
        env = {"XDG_STATE_HOME": str(Path(self.enterContext(_tempdir())) / "state")}
        results = [(("smoke",), qualify.CheckResult("smoke", None, "pass", 200, "ok").record(AT, CONTRACTS)),
                   (("context",), qualify.CheckResult("context", None, "failed", 200, "retrieval-wrong",
                                                      requested=9000, measured=8999,
                                                      measured_source="usage.input_tokens",
                                                      retrieval="wrong").record(AT, CONTRACTS))]
        operator_mod.record_checks(env, schemas, "custom-x", digest="a" * 64, host="api.example.test",
                                   versions=self.VERSIONS, results=results)
        path = operator_mod.evidence_path(env)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        raw = path.read_bytes()
        operator_mod.parse_evidence(raw, schemas.evidence)
        for forbidden in (b"verification code", b"Reply with", b"nonce", b"Authorization", b"messages",
                          b"n0nce", qualify.TOOL_NAME.encode()):
            self.assertNotIn(forbidden, raw)
        # The legacy smoke record still reads (amended in place).
        legacy = json.loads((Path(operator_fixtures.OPERATOR_FIXTURES) / "evidence-sample.json").read_text())
        operator_mod.parse_evidence(json.dumps(legacy).encode(), schemas.evidence)
        # Bounded enums: an unknown reason or result refuses.
        broken = json.loads(raw)
        broken["lines"]["custom-x"]["checks"]["smoke"]["reason"] = "some free text"
        with self.assertRaises(operator_mod.OperatorError):
            operator_mod.parse_evidence(json.dumps(broken).encode(), schemas.evidence)
        self.assertEqual(set(qualify.REASONS),
                         set(json.loads((SHIPPED_ROOT / "schemas" / "operator-evidence.schema.json").read_text())
                             ["properties"]["lines"]["additionalProperties"]["properties"]["checks"]["properties"]
                             ["smoke"]["properties"]["reason"]["enum"]))


def _tempdir():
    import contextlib
    import shutil
    import tempfile

    @contextlib.contextmanager
    def manager():
        root = tempfile.mkdtemp(prefix="claude-multi-qualify-")
        os.chmod(root, 0o700)
        try:
            yield root
        finally:
            shutil.rmtree(root, ignore_errors=True)

    return manager()


# ------------------------------------------------------------ contract identity
class ContractIdentityTests(unittest.TestCase):
    def test_client_digest_reads_only_the_three_fields(self) -> None:
        contract = copy.deepcopy(catalog.load_catalog(FIXTURE_ROOT).docs["native-contract"])
        digest = qualify.client_contract_digest(contract)
        contract["recorded_at"] = "2099-01-01"
        contract["agent_efforts"]["evidence"] = "rewritten prose"
        self.assertEqual(qualify.client_contract_digest(contract), digest)
        platform = pin.host_platform()
        for mutate in (lambda c: c["verified"][0].__setitem__("version", "9.9.9"),
                       lambda c: c["verified"][0]["platforms"][platform].__setitem__("sha256", "f" * 64),
                       lambda c: c["agent_efforts"].__setitem__("values", ["high"])):
            changed = copy.deepcopy(contract)
            mutate(changed)
            self.assertNotEqual(qualify.client_contract_digest(changed), digest)

    def test_content_only_patch_change_moves_the_gateway_digest_not_the_attestation(self) -> None:
        base = {"version": 1, "upstream_version": "7.3.15",
                "patches": ["cli-proxy-api-a.patch@" + "1" * 64, "cli-proxy-api-management-readonly-allowlist"
                            ".patch@" + "2" * 64]}
        digest = qualify.gateway_contract_digest(qualify.parse_gateway_contract(json.dumps(base).encode()))
        changed = copy.deepcopy(base)
        changed["patches"][0] = "cli-proxy-api-a.patch@" + "3" * 64  # same basename, one changed byte
        self.assertNotEqual(qualify.gateway_contract_digest(changed), digest)
        reordered = copy.deepcopy(base)
        reordered["patches"].reverse()
        self.assertNotEqual(qualify.gateway_contract_digest(reordered), digest)
        names = ",".join(item.split("@", 1)[0] for item in base["patches"])
        environ = {management.PINNED_BIN_ENV: "/nix/store/x/bin/cli-proxy-api", management.PATCHES_ENV: names,
                   management.CHANNEL_ENV: management.MANAGEMENT_CHANNEL}
        self.assertTrue(management.allowlist_build(environ))  # the basename attestation is unchanged
        for bad in ({**base, "patches": ["x.patch"]}, {**base, "extra": 1}, {**base, "version": 2},
                    {**base, "patches": [base["patches"][0], base["patches"][0]]}):
            with self.assertRaises(qualify.QualifyError):
                qualify.parse_gateway_contract(json.dumps(bad).encode())

    def test_manifest_under_the_asset_root_or_none(self) -> None:
        with _tempdir() as root:
            contract = catalog.load_catalog(FIXTURE_ROOT).docs["native-contract"]
            self.assertIsNone(qualify.contract_identity(contract, root).gateway)
            manifest = {"version": 1, "upstream_version": "7.3.15", "patches": ["a.patch@" + "1" * 64]}
            Path(root, qualify.GATEWAY_CONTRACT_NAME).write_text(json.dumps(manifest))
            identity = qualify.contract_identity(contract, root)
            self.assertEqual(identity.gateway, qualify.gateway_contract_digest(manifest))
            Path(root, qualify.GATEWAY_CONTRACT_NAME).write_text("{not json")
            self.assertIsNone(qualify.contract_identity(contract, root).gateway)


# ------------------------------------------------------------ transport and caps
class _Server:
    def __init__(self, handler):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.hits = handler.hits

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _handler(mode):
    class Handler(BaseHTTPRequestHandler):
        hits: list[str] = []

        def log_message(self, *_args):
            return

        def do_POST(self):  # noqa: N802
            type(self).hits.append(self.path)
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            if mode == "stall":
                time.sleep(2)
                return
            if mode == "slow-headers":
                time.sleep(2)
            body = json.dumps({"type": "message", "content": [{"type": "text", "text": "ok"}]}).encode()
            if mode == "oversize":
                body = b"x" * (qualify.RESPONSE_MAX_BYTES + 10)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if mode == "close-stall":
                # A will-close response (no length): http.client detaches the
                # socket from the connection; the deadline must still cut it.
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(3)
                return
            if mode != "dribble":
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if mode == "dribble":
                try:
                    for _ in range(40):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.1)
                except OSError:
                    return
                return
            self.wfile.write(body)

    return Handler


class BoundsTests(unittest.TestCase):
    OWNER = staticmethod(lambda _base: service.OwnerVerdict("ours", "fixture gateway"))

    def _post(self, server, deadline=0.5):
        return qualify.loopback_post(f"http://127.0.0.1:{server.port}", "t" * 32, b"{}", deadline=deadline,
                                     owner_check=self.OWNER)

    def test_dribble_stall_oversize_and_cancel(self) -> None:
        for mode, expected in (("ok", (200, None)), ("dribble", (200, "timeout")), ("stall", (None, "timeout")),
                               ("close-stall", (200, "timeout")),
                               ("slow-headers", (None, "timeout")), ("oversize", (200, "oversize"))):
            with self.subTest(mode=mode):
                server = _Server(_handler(mode))
                try:
                    started = time.monotonic()
                    result = self._post(server)
                    elapsed = time.monotonic() - started
                finally:
                    server.close()
                self.assertEqual((result.status, result.failure), expected)
                self.assertLess(elapsed, 1.5)  # the total wall-clock cap, not a per-read one
                self.assertEqual(len(server.hits), 1)  # never retried
        self.assertEqual(qualify.classify_message(qualify.HttpResult(200, b"", "timeout")),
                         ("inconclusive", "timeout"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        refused = qualify.loopback_post(f"http://127.0.0.1:{port}", "t" * 32, b"{}", deadline=0.5,
                                        owner_check=self.OWNER)
        self.assertEqual(refused.failure, "connection")

    def test_environment_proxies_never_see_the_loopback_request(self) -> None:
        gateway = _Server(_handler("ok"))
        proxy = _Server(_handler("ok"))
        try:
            with mock.patch.dict(os.environ, {"http_proxy": f"http://127.0.0.1:{proxy.port}",
                                              "HTTP_PROXY": f"http://127.0.0.1:{proxy.port}", "no_proxy": ""}):
                result = self._post(gateway)
        finally:
            gateway.close()
            proxy.close()
        self.assertEqual(result.status, 200)
        self.assertEqual(proxy.hits, [])
        self.assertEqual(gateway.hits, ["/v1/messages"])

    def test_non_loopback_and_unowned_listeners_refuse_before_sending(self) -> None:
        with self.assertRaises(qualify.QualifyError):
            qualify.loopback_post("https://127.0.0.1:1234", "t", b"{}", deadline=1)
        with self.assertRaises(qualify.QualifyError):
            qualify.loopback_post("http://10.0.0.1:1234", "t", b"{}", deadline=1)

    def test_request_construction_is_bounded(self) -> None:
        call = qualify.PlannedCall("context", None, "custom-x", None, 5_000_000, 300)
        with self.assertRaisesRegex(qualify.QualifyError, "exceeds 16777216 bytes; nothing sent"):
            qualify.context_body(call, "n")


# ------------------------------------------------------------ exact client
class ExactClientTests(unittest.TestCase):
    def test_classes(self) -> None:
        row = {"model": "claude-x-9", "effort": "high", "thinking": "enabled", "max_tokens": 32000}
        base = {"lead": {"high": {"effort": "high", "rows": [row]}},
                "agent": {"high": {"effort": "high", "rows": [row]}},
                "window": {"client": 1000000, "compiled": 800000}}
        window = {"client": 1000000, "compiled": 800000}

        def verdict(observations):
            return qualify.exact_client_class(observations, wire="claude-x-9", efforts=("high",), client_effort=True,
                                              output_limit=64000, window=window)

        self.assertEqual(verdict(base), qualify.ExactClientOutcome("pass", "ok"))
        for change, reason in ((lambda o: o["lead"]["high"]["rows"][0].update(model="claude-other"),
                                "model-rewritten"),
                               (lambda o: o["agent"]["high"]["rows"][0].update(effort="xhigh"), "effort-dropped"),
                               (lambda o: o["lead"]["high"]["rows"][0].update(thinking=None), "thinking-stripped"),
                               (lambda o: o["lead"]["high"]["rows"][0].update(max_tokens=128000), "output-exceeds"),
                               (lambda o: o["window"].update(compiled=1000000), "window-mismatch")):
            observed = copy.deepcopy(base)
            change(observed)
            self.assertEqual(verdict(observed), qualify.ExactClientOutcome("failed", reason))
        empty = copy.deepcopy(base)
        empty["agent"]["high"]["rows"] = []
        self.assertEqual(verdict(empty), qualify.ExactClientOutcome("inconclusive", "no-run"))

    @staticmethod
    def _record(model, *, markers=("lead",), hints=(), effort="high"):
        from claude_multi import probe

        record = probe.RequestRecord(
            method="POST", path="/v1/messages", model=model, tool_names=(), tool_count=0,
            tool_names_truncated=False, has_system=True, system_json_bytes=1, messages_json_bytes=1,
            message_count=1, message_sha256s=(), message_hashes_truncated=False, auth="dummy",
            body_sha256="0" * 64, max_tokens=32000, effort=effort, thinking_type="adaptive",
            hint_header_values=tuple(hints), markers_found=tuple(markers))
        return probe.RequestContext(ordinal=1, header_names=(), record=record)

    def _observe(self, *records):
        """Feed the production recorder (lead run + agent run) and classify."""

        from claude_multi import probe

        lead, agent = probe._ExactClientRecorder("claude-x-9"), probe._ExactClientRecorder("claude-x-9")
        for kind, context in records:
            (agent if kind == "agent" else lead).respond({"model": context.record.model}, "/v1/messages", context)
        observations = {"lead": {"high": {"effort": "high", "rows": lead.lead}},
                        "agent": {"high": {"effort": "high", "rows": agent.child}},
                        "window": {"client": 1000000, "compiled": 800000}}
        return qualify.exact_client_class(observations, wire="claude-x-9", efforts=("high",), client_effort=True,
                                          output_limit=64000, window={"client": 1000000, "compiled": 800000})

    def test_the_recorder_observes_a_substituted_model(self) -> None:
        """B check (MAJOR): probe requests are classed by marker/hint, never
        by model — a substitution is failed/model-rewritten, never no-run,
        and a failure replaces an earlier pass in the evidence."""

        agent_hint = (("x-claude-code-agent-id", "a1"),)
        good_agent = ("agent", self._record("claude-x-9", markers=("agent",), hints=agent_hint))
        self.assertEqual(self._observe(("lead", self._record("claude-x-9")), good_agent),
                         qualify.ExactClientOutcome("pass", "ok"))
        wholly = self._observe(("lead", self._record("claude-other")),
                               ("agent", self._record("claude-other", markers=("agent",), hints=agent_hint)))
        mixed = self._observe(("lead", self._record("claude-x-9")), ("lead", self._record("claude-other")),
                              good_agent)
        agent_only = self._observe(("lead", self._record("claude-x-9")),
                                   ("agent", self._record("claude-x-9", markers=("agent",), hints=agent_hint)),
                                   ("agent", self._record("claude-other", markers=("agent",))))
        for outcome in (wholly, mixed, agent_only):
            self.assertEqual(outcome, qualify.ExactClientOutcome("failed", "model-rewritten"))
        # Auxiliary and unmarked requests are excluded explicitly (not probe requests).
        auxiliary = (("x-claude-code-request-class", "auxiliary"),)
        self.assertEqual(self._observe(("lead", self._record("claude-x-9")), good_agent,
                                       ("lead", self._record("claude-helper", hints=auxiliary)),
                                       ("lead", self._record("claude-helper", markers=()))),
                         qualify.ExactClientOutcome("pass", "ok"))
        # Evidence merge: the failure replaces an earlier pass.
        versions = {"launcher": "3.1.0", "client": "2.1.281", "gateway": "7.3.15"}
        passed = qualify.exact_client_result(qualify.ExactClientOutcome("pass", "ok")).record(AT, CONTRACTS)
        record = operator_mod.merge_checks(None, digest="a" * 64, host="h", versions=versions,
                                           results=[(("exact_client",), passed)])
        failed = qualify.exact_client_result(wholly).record(AT, CONTRACTS)
        after = operator_mod.merge_checks(record, digest="a" * 64, host="h", versions=versions,
                                          results=[(("exact_client",), failed)])
        self.assertEqual((after["checks"]["exact_client"]["result"], after["checks"]["exact_client"]["reason"]),
                         ("failed", "model-rewritten"))

    def test_platform_without_isolation_is_unavailable_never_a_pass(self) -> None:
        from claude_multi import probe

        with mock.patch.object(probe, "network_isolation_available", return_value="no bwrap"):
            outcome = probe.run_exact_client_check(None, native_contract={})
        self.assertEqual(outcome, qualify.ExactClientOutcome("inconclusive", "unavailable"))
        self.assertEqual(outcome.text(), "inconclusive (exact-client proof unavailable on this platform)")


# ------------------------------------------------------------ the command
AGENT_LINE = {"wire_model": "acme-agent-1", "display": "Acme Agent", "efforts": {"high": "output-config-high"},
              "default_effort": "high", "context": {"declared_tokens": 200000, "source": "operator"},
              "capabilities": ["lead", "agents"], "roles": ["cm-reviewer", "cm-analyst"], "family": "unknown"}


class QualifyCommandCase(test_cli.OperatorCommandCase):
    """An approved acme route with an agent-requesting line, served."""

    def setUp(self) -> None:
        super().setUp()
        code, _out, err = self.op(test_cli.ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        document = json.loads((self.pdir / "acme.json").read_text())
        document["lines"] = {"custom-acme-agent": copy.deepcopy(AGENT_LINE)}
        state.atomic_write(self.pdir / "acme.json", strict_json.pretty_file_bytes(document))
        self.apply()
        patcher = mock.patch.object(self.runtime, "contract_identity", return_value=CONTRACTS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def apply(self) -> None:
        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, err)
        self.serve_current()

    def evidence(self):
        return operator_mod.load_evidence(self.runtime.gateway_environ(), operator_mod.load_schemas(FIXTURE_ROOT))


class AdmissionMetadataTests(test_cli.OperatorCommandCase):
    """Badges never send inference, require credentials or change served state."""

    def setUp(self) -> None:
        super().setUp()
        self.key = "custom-acme-small"
        self.document = operator_fixtures._fixture_files()["acme"]
        state.ensure_private_dir(self.pdir)
        self.declaration = self.pdir / "acme.json"
        state.atomic_write(self.declaration, strict_json.pretty_file_bytes(self.document))
        state.atomic_write(self.secret_file, b"")
        self.runtime.settings_store.set_provider_enabled("acme", False, catalog=self.runtime.lineup_catalog())
        self.served.clear()

    def test_admit_and_revoke_only_change_badges_even_with_failed_evidence(self) -> None:
        from claude_multi.cli.commands import models, providers

        schemas = operator_mod.load_schemas(FIXTURE_ROOT)
        snapshot = self.runtime.operator_snapshot()
        record = operator_mod.merge_checks(
            None, digest=snapshot.layer.lines[self.key].definition_digest, host="fixture",
            versions={"launcher": "1.0.0", "client": "1.0.0", "gateway": "1.0.0"},
            results=[(("smoke",), {"result": "failed", "at": AT, "reason": "no-acknowledgement", "http": 200})],
        )
        operator_mod.write_evidence(self.runtime.gateway_environ(), schemas, self.key, record)
        evidence_path = operator_mod.evidence_path(self.runtime.gateway_environ())
        evidence_before = evidence_path.read_bytes()
        before = self.state_bytes()
        with mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("admit inferred")), \
                mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("admit qualified")), \
                mock.patch.object(self.runtime, "render_gateway", side_effect=AssertionError("badge rendered")), \
                mock.patch.object(models, "render_identity", side_effect=AssertionError("badge needs serving")), \
                mock.patch.object(models, "_credential_present", side_effect=AssertionError("badge needs a key")), \
                mock.patch.object(providers, "served_preflight", side_effect=AssertionError("badge preview rendered")):
            code, out, err = self.op(["models", "admit", self.key], "y\n")
            self.assertEqual(code, 0, out + err)
            self.assertIn("optional local attestation", err)
            snapshot = self.runtime.operator_snapshot()
            self.assertTrue(operator_mod.operator_line_admitted(
                self.key, layer=snapshot.layer, ledger=snapshot.ledger,
                admitted_lines=self.runtime.settings_store.load()["admitted_lines"]))
            self.assertEqual(snapshot.layer.route_status["acme"], "unapproved")
            self.assertEqual(snapshot.ledger.routes, {})
            self.assertFalse(self.runtime.settings_store.load()["providers"]["acme"]["enabled"])
            changed = {Path(path).name for path in set(before) | set(self.state_bytes())
                       if before.get(path) != self.state_bytes().get(path)}
            self.assertEqual(changed, {"settings.json", "operator-ledger.json"})
            code, out, err = self.op(["models", "revoke", self.key], "y\n")
            self.assertEqual(code, 0, out + err)
            self.assertIn("line remains usable", err)
            self.assertIn("qualification evidence is unchanged", err)
        self.assertNotIn(self.key, self.runtime.settings_store.load()["admitted_lines"])
        self.assertNotIn(self.key, self.ledger().admissions)
        self.assertEqual(evidence_path.read_bytes(), evidence_before)
        code, out, err = self.op(["models", "show", self.key])
        self.assertEqual(code, 0, err)
        self.assertIn("qualification: failed", out)
        self.assertIn("smoke evidence: failed (no-acknowledgement)", out)
        self.assertIn("not admitted", out)
        self.assertEqual((self.calls, self.http_calls), ([], []))

    def test_catalog_badges_are_metadata_only_too(self) -> None:
        from claude_multi.cli.commands import providers

        key, entry = next(iter(self.runtime.catalog.lines.items()))
        before = self.state_bytes()
        with mock.patch.dict(entry, {"status": "new"}), \
                mock.patch.object(self.runtime, "render_gateway", side_effect=AssertionError("badge rendered")), \
                mock.patch.object(providers, "served_preflight", side_effect=AssertionError("badge preview rendered")):
            code, out, err = self.op(["models", "admit", key], "y\n")
            self.assertEqual(code, 0, out + err)
            self.assertIn(key, self.runtime.settings_store.load()["admitted_lines"])
            code, out, err = self.op(["models", "revoke", key, "--yes"])
            self.assertEqual(code, 0, out + err)
        changed = {Path(path).name for path in set(before) | set(self.state_bytes())
                   if before.get(path) != self.state_bytes().get(path)}
        self.assertEqual(changed, {"settings.json"})
        self.assertFalse(self.ledger_file.exists())
        self.assertEqual((self.calls, self.http_calls), ([], []))

    def test_default_no_and_human_guard_keep_state_unchanged(self) -> None:
        for text, tty, env, code in (("\n", True, {}, 3), ("y\n", False, {}, 1),
                                     ("y\n", True, {"CLAUDECODE": "1"}, 1)):
            with self.subTest(tty=tty, env=env, answer=text):
                before = self.state_bytes()
                actual, _out, _err = self.op(["models", "admit", self.key], text, tty=tty, env=env)
                self.assertEqual(actual, code)
                self.assertEqual(self.state_bytes(), before)
                self.assertEqual((self.calls, self.http_calls), ([], []))

    def test_definition_or_route_change_while_prompt_open_refuses_badge(self) -> None:
        from claude_multi.cli import consent

        for field in ("wire", "route"):
            with self.subTest(field=field):
                def confirm(_text, **_kw):
                    document = json.loads(self.declaration.read_bytes())
                    if field == "wire":
                        document["lines"][self.key]["wire_model"] = "changed-fixture-model"
                    else:
                        document["provider"]["base_url"] = "https://changed.example/anthropic"
                    state.atomic_write(self.declaration, strict_json.pretty_file_bytes(document))
                    return True

                with mock.patch.object(consent, "confirm", side_effect=confirm):
                    code, _out, err = self.op(["models", "admit", self.key], "y\n")
                self.assertEqual(code, 1, err)
                self.assertIn("configuration changed while awaiting confirmation", err)
                self.assertFalse(self.ledger_file.exists())
                self.assertNotIn(self.key, self.runtime.settings_store.load().get("admitted_lines", ()))
                self.assertEqual((self.calls, self.http_calls), ([], []))

    def test_corrupt_ledger_and_invalid_definition_still_refuse(self) -> None:
        state.atomic_write(self.ledger_file, b"not json")
        before = self.state_bytes()
        code, _out, _err = self.op(["models", "admit", self.key], "y\n")
        self.assertEqual(code, 1)
        self.assertEqual(self.state_bytes(), before)
        state.atomic_write(self.ledger_file, strict_json.pretty_file_bytes(operator_mod.ledger_document(None)))
        document = copy.deepcopy(self.document)
        document["lines"][self.key]["efforts"] = ["not-native"]
        state.atomic_write(self.declaration, strict_json.pretty_file_bytes(document))
        before = self.state_bytes()
        code, _out, err = self.op(["models", "admit", self.key], "y\n")
        self.assertEqual(code, 1, err)
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual((self.calls, self.http_calls), ([], []))


class CommitTests(QualifyCommandCase):
    def test_qualification_changes_only_evidence(self) -> None:
        before = self.state_bytes()
        code, out, err = self.op(["models", "qualify", "custom-acme-agent", "--agents"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Qualification plan for custom-acme-agent:", err)
        self.assertEqual([label for label, _ in self.http_calls],
                         ["smoke", "effort high", "tools (forced) step 1", "tools (forced) step 2", "stream"])
        for line in ("smoke custom-acme-agent: pass (HTTP 200)", "effort high custom-acme-agent: pass (HTTP 200)",
                     "tools (forced) custom-acme-agent: pass (HTTP 200)", "stream custom-acme-agent: pass (HTTP 200)"):
            self.assertIn(line, out)
        after = self.state_bytes()
        changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
        self.assertEqual({Path(path).name for path in changed}, {"operator-evidence.json"})
        record = self.evidence().lines["custom-acme-agent"]
        self.assertEqual(record["checks"]["tools"]["forced"]["contracts"], CONTRACTS.as_document())
        self.assertNotIn("custom-acme-agent", self.runtime.current_effective().admitted_lines)
        # Declined: nothing sent, nothing written.
        self.http_calls.clear()
        before = self.state_bytes()
        code, _out, err = self.op(["models", "qualify", "custom-acme-agent", "--stream"], "n\n")
        self.assertEqual((code, self.http_calls, self.state_bytes()), (1, [], before))
        self.assertIn("declined — nothing sent", err)

    def test_digest_route_or_render_change_refuses_commit(self) -> None:
        path = self.pdir / "acme.json"

        def edit() -> None:
            document = json.loads(path.read_text())
            document["lines"]["custom-acme-agent"]["context"]["declared_tokens"] = 190000
            state.atomic_write(path, strict_json.pretty_file_bytes(document))
            self.during_smoke = None

        self.during_smoke = edit
        code, _out, err = self.op(["models", "qualify", "custom-acme-agent", "--smoke"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("qualify custom-acme-agent: configuration changed during qualification — evidence not "
                      "recorded; retry", err)
        self.assertIsNone(self.evidence())
        # A contract change after the plan refuses before any request.
        self.apply()
        self.http_calls.clear()
        with mock.patch.object(self.runtime, "contract_identity", side_effect=[CONTRACTS, OTHER_CONTRACTS]):
            code, _out, err = self.op(["models", "qualify", "custom-acme-agent", "--smoke"], "y\n")
        self.assertEqual((code, self.http_calls), (1, []))
        self.assertIn("configuration changed while awaiting confirmation — nothing sent", err)
        # ... and between the last request and the commit, the locked commit refuses.
        with mock.patch.object(self.runtime, "contract_identity",
                               side_effect=[CONTRACTS, CONTRACTS, OTHER_CONTRACTS]):
            code, _out, err = self.op(["models", "qualify", "custom-acme-agent", "--smoke"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("configuration changed during qualification — evidence not recorded", err)
        self.assertIsNone(self.evidence())
        # An unapproved route refuses before any request.
        self.http_calls.clear()
        document = json.loads(path.read_text())
        document["provider"]["base_url"] = "https://api2.acme.example/anthropic"
        state.atomic_write(path, strict_json.pretty_file_bytes(document))
        code, _out, err = self.op(["models", "qualify", "custom-acme-agent", "--smoke"], "y\n")
        self.assertEqual((code, self.http_calls), (1, []))
        self.assertEqual(err, "claude-multi: qualify custom-acme-agent: provider acme: route changed — "
                              "claude-multi providers approve acme\n")

    def test_a_change_while_the_prompt_is_open_sends_nothing(self) -> None:
        """B check (MAJOR): the consented authorization is revalidated after
        the answer and before every request, not only at the commit."""

        from claude_multi.cli import consent as consent_mod

        path = self.pdir / "acme.json"
        real_confirm = consent_mod.confirm

        def retarget() -> None:
            document = json.loads(path.read_text())
            document["lines"]["custom-acme-agent"]["wire_model"] = "different-unconsented-wire"
            state.atomic_write(path, strict_json.pretty_file_bytes(document))
            self.apply()

        def confirm_then_change(text, **kwargs):
            answer = real_confirm(text, **kwargs)
            with mock.patch.object(consent_mod, "confirm", real_confirm):
                retarget()
            return answer

        before_evidence = self.evidence()
        with mock.patch.object(consent_mod, "confirm", side_effect=confirm_then_change):
            code, _out, err = self.op(["models", "qualify", "custom-acme-agent", "--agents"], "y\n")
        self.assertEqual((code, self.http_calls), (1, []))
        self.assertIn("qualify custom-acme-agent: configuration changed while awaiting confirmation — nothing "
                      "sent; retry", err)
        self.assertEqual(self.evidence(), before_evidence)

    def test_a_change_between_requests_stops_the_remaining_requests(self) -> None:
        path = self.pdir / "acme.json"

        def retarget() -> None:
            self.during_smoke = None
            document = json.loads(path.read_text())
            document["lines"]["custom-acme-agent"]["wire_model"] = "different-unconsented-wire"
            state.atomic_write(path, strict_json.pretty_file_bytes(document))
            self.apply()

        self.during_smoke = retarget
        code, _out, err = self.op(["models", "qualify", "custom-acme-agent", "--agents"], "y\n")
        self.assertEqual(code, 1)
        self.assertEqual([label for label, _ in self.http_calls], ["smoke"])
        self.assertIn("qualify custom-acme-agent: configuration changed during qualification — remaining "
                      "requests not sent; evidence not recorded; retry", err)
        self.assertIsNone(self.evidence())
        # A route change (unapproved) or a render change stops them too.
        for change in ("route", "render"):
            with self.subTest(change=change):
                self.http_calls.clear()
                document = json.loads(path.read_text())

                def edit(document=document, change=change) -> None:
                    self.during_smoke = None
                    if change == "route":
                        document["provider"]["base_url"] = "https://api2.acme.example/anthropic"
                        state.atomic_write(path, strict_json.pretty_file_bytes(document))
                    else:
                        self.served.clear()

                self.serve_current()
                self.during_smoke = edit
                code, _out, err = self.op(["models", "qualify", "custom-acme-agent", "--agents"], "y\n")
                self.assertEqual(code, 1, err)
                self.assertEqual([label for label, _ in self.http_calls], ["smoke"])
                self.assertIn("remaining requests not sent", err)
                if change == "route":
                    document["provider"]["base_url"] = "https://api.acme.example/anthropic"
                    state.atomic_write(path, strict_json.pretty_file_bytes(document))

    def test_session_and_redirected_stdio_refuse_before_anything(self) -> None:
        before = self.state_bytes()
        for tty, env in ((True, {"CLAUDECODE": "1"}), (True, {"CLAUDE_MULTI_MANAGED_ID": ""}), (False, {})):
            with self.subTest(tty=tty, env=env):
                code, _out, _err = self.op(["models", "qualify", "custom-acme-agent", "--agents"], "y\n",
                                           tty=tty, env=env)
                self.assertNotEqual(code, 0)
        self.assertEqual((self.http_calls, self.state_bytes()), ([], before))

    def test_a_newer_state_marker_refuses_before_any_request(self) -> None:
        marker = self.root / "state" / "claude-multi" / "state-version"
        state.ensure_private_dir(marker.parent)
        state.atomic_write(marker, b"9\n")
        code, out, err = self.op(["models", "qualify", "custom-acme-agent", "--smoke"], "y\n")
        self.assertNotEqual(code, 0)
        self.assertIn("state belongs to a newer claude-multi", out + err)
        self.assertNotIn("Qualification plan", out + err)
        self.assertEqual(self.http_calls, [])
        self.assertIsNone(self.evidence())

    def test_catalog_keys_and_bad_context_are_refused(self) -> None:
        catalog_key = sorted(self.runtime.catalog.lines)[0]
        code, _out, err = self.op(["models", "qualify", catalog_key], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("qualify applies to operator lines", err)
        code, _out, err = self.op(["models", "qualify", "custom-acme-agent", "--context", "999999"], "y\n")
        self.assertEqual(code, 2)
        self.assertIn("outside 8192..200000", err)
        self.assertEqual(self.http_calls, [])

    def test_inconclusive_rerun_keeps_the_pass_and_exits_nonzero(self) -> None:
        code, _out, err = self.op(["models", "qualify", "custom-acme-agent", "--stream"], "y\n")
        self.assertEqual(code, 0, err)
        self.runtime.qualify_http = lambda *_a: qualify.HttpResult(503, b"")
        code, out, _err = self.op(["models", "qualify", "custom-acme-agent", "--stream"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("stream custom-acme-agent: inconclusive (HTTP 503: upstream error)", out)
        self.assertEqual(self.evidence().lines["custom-acme-agent"]["checks"]["stream"]["result"], "pass")

    def test_context_check_raises_the_floor_only_when_correct(self) -> None:
        code, out, err = self.op(["models", "qualify", "custom-acme-agent", "--context", "180000"], "y\n")
        self.assertEqual(code, 0, err)
        # The default floor is min(declared, 200000): a 200K declaration is
        # already at its cap, so a correct measurement leaves it unchanged.
        self.assertRegex(out, r"context custom-acme-agent: accepted \d+ input tokens; retrieval correct\n"
                              r"validated floor unchanged: 200000")
        line = self.runtime.operator_snapshot().layer.lines["custom-acme-agent"]
        self.assertEqual(line.core_entry["context"]["validated_tokens"], 200000)
        self.assertEqual(self.evidence().lines["custom-acme-agent"]["checks"]["context"]["retrieval"], "correct")

    def test_a_smaller_or_failed_context_check_never_lowers_the_persisted_floor(self) -> None:
        """The highest successful measured bound is kept apart
        from the latest verdict, and the reported floor is the one a fresh
        reload of the operator layer resolves."""

        import re

        document = json.loads((self.pdir / "acme.json").read_text())
        document["lines"]["custom-acme-agent"]["context"]["declared_tokens"] = 400000
        state.atomic_write(self.pdir / "acme.json", strict_json.pretty_file_bytes(document))
        self.apply()

        def qualify_context(tokens: int) -> tuple[str, int]:
            code, out, err = self.op(["models", "qualify", "custom-acme-agent", "--context", str(tokens)], "y\n")
            self.assertEqual(code, 0, err)
            reported = int(re.search(r"validated floor(?: unchanged)?: (\d+)", out).group(1))
            reloaded = self.runtime.operator_snapshot().layer.lines["custom-acme-agent"]
            self.assertEqual(reloaded.core_entry["context"]["validated_tokens"], reported, out)
            return out, reported

        out, high = qualify_context(300000)
        self.assertGreater(high, 200000, out)
        self.assertRegex(out, rf"validated floor: {high}")
        out, floor = qualify_context(32000)
        self.assertEqual(floor, high)
        self.assertIn(f"validated floor unchanged: {high}", out)
        check = self.evidence().lines["custom-acme-agent"]["checks"]["context"]
        self.assertEqual((check["requested"], check["floor"]["measured"]), (32000, high))
        # A later failed context check keeps the bound; its verdict is recorded.
        self.runtime.qualify_http = lambda *_a: qualify.HttpResult(400, b"{}")
        code, out, _err = self.op(["models", "qualify", "custom-acme-agent", "--context", "64000"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn(f"validated floor unchanged: {high}", out)
        check = self.evidence().lines["custom-acme-agent"]["checks"]["context"]
        self.assertEqual((check["result"], check["floor"]["measured"]), ("failed", high))
        reloaded = self.runtime.operator_snapshot().layer.lines["custom-acme-agent"]
        self.assertEqual(reloaded.core_entry["context"]["validated_tokens"], high)
        # A new definition digest drops it (evidence counts for nothing then).
        merged = operator_mod.merge_checks(self.evidence().lines["custom-acme-agent"], digest="0" * 64, host="h",
                                           versions={"launcher": "1.0.0", "client": "1.0.0", "gateway": "1.0.0"},
                                           results=[])
        self.assertEqual(merged["checks"], {})


class ExactClientCommandTests(test_cli.OperatorCommandCase):
    """A first-party pool line runs the offline exact-client check (zero
    provider requests), recorded as separate evidence."""

    POOL_LINE = {"wire_model": "claude-fixture-agent-9", "display": "Claude fixture agent", "efforts": ["high", "max"],
                 "default_effort": "high", "context": {"declared_tokens": 1000000, "source": "operator"},
                 "output": {"declared_tokens": 64000, "source": "operator"},
                 "capabilities": ["lead", "agents"], "roles": ["cm-reviewer"]}

    def setUp(self) -> None:
        super().setUp()
        state.ensure_private_dir(self.pdir)
        state.atomic_write(self.pdir / "anthropic.json", strict_json.pretty_file_bytes(
            {"version": 1, "lines": {"custom-claude-agent": copy.deepcopy(self.POOL_LINE)}}))
        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, err)
        self.serve_current()
        self.inputs = []
        patcher = mock.patch.object(self.runtime, "contract_identity", return_value=CONTRACTS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _runner(self, outcome):
        def run(request):
            self.inputs.append(request)
            return outcome
        return run

    def test_exact_client_is_disclosed_separate_and_unavailable_is_specific(self) -> None:
        self.runtime.exact_client_runner = self._runner(qualify.ExactClientOutcome("pass", "ok"))
        code, out, err = self.op(["models", "qualify", "custom-claude-agent", "--agents"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("exact-client check: the pinned Claude Code client runs offline", err)
        self.assertIn("exact-client custom-claude-agent: pass", out)
        (request,) = self.inputs
        self.assertEqual((request.selector, request.wire), ("claude-fixture-agent-9[1m]", "claude-fixture-agent-9"))
        self.assertEqual((request.efforts, request.client_effort, request.output_limit), (("high", "max"), True, 64000))
        self.assertEqual(request.expected_window["client"], 1000000)
        self.assertEqual(request.plan.settings["model"], request.selector)
        self.assertNotIn("exact", " ".join(label for label, _ in self.http_calls))
        record = operator_mod.load_evidence(self.runtime.gateway_environ(),
                                            operator_mod.load_schemas(FIXTURE_ROOT)).lines["custom-claude-agent"]
        self.assertEqual(record["checks"]["exact_client"]["result"], "pass")
        self.runtime.exact_client_runner = self._runner(qualify.ExactClientOutcome("inconclusive", "unavailable"))
        code, out, _err = self.op(["models", "qualify", "custom-claude-agent", "--agents"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("exact-client custom-claude-agent: inconclusive (exact-client proof unavailable on this "
                      "platform)", out)
        # An unavailable attempt never replaces the earlier pass.
        record = operator_mod.load_evidence(self.runtime.gateway_environ(),
                                            operator_mod.load_schemas(FIXTURE_ROOT)).lines["custom-claude-agent"]
        self.assertEqual(record["checks"]["exact_client"]["result"], "pass")





class KeyedReplayTests(unittest.TestCase):
    """Keyed history is returned content, not the legacy id-only fixture."""

    def plan(self, checks=("tools",), **kw):
        return _plan(checks=checks, entry=_entry(["low", "high", "max"], selector="custom-chat"),
                     keyed_replay=True, **kw)

    def content(self):
        return [{"type": "thinking", "thinking": "private-reasoning", "signature": "private-signature"},
                {"type": "text", "text": "private-text"},
                {"type": "tool_use", "id": "call_1", "name": qualify.TOOL_NAME, "input": {"nonce": "nonce"}}]

    def response(self, content):
        return qualify.HttpResult(200, json.dumps({"type": "message", "content": content}).encode())

    def run_tools(self, content=None, *, variant="forced", before_send=None):
        gateway = _Gateway({f"tools ({variant}) step 1": self.response(self.content() if content is None else content)})
        result = qualify.run_battery(self.plan(tools_variant=variant), gateway, nonce=lambda: "nonce",
                                     before_send=before_send)
        return result, gateway.calls

    def test_keyed_tools_replay_returned_bounded_assistant_content(self):
        (result,), calls = self.run_tools()
        self.assertEqual(result.result, "pass")
        self.assertEqual(calls[1][1]["messages"][1]["content"], self.content())
        self.assertEqual(calls[1][1]["messages"][2]["content"][0]["tool_use_id"], "call_1")
        unsigned = self.content()
        del unsigned[0]["signature"]
        (_,), calls = self.run_tools(unsigned)
        self.assertEqual(calls[1][1]["messages"][1]["content"], unsigned)

    def test_keyed_replay_validates_tool_ids_names_arguments_and_blocks(self):
        bad = []
        for field, values in {"id": ["", "bad id", "x" * 257, 1], "name": ["other"],
                              "input": [{"nonce": "wrong"}, {"nonce": "nonce", "extra": 1}]}.items():
            for value in values:
                blocks = self.content()
                blocks[-1][field] = value
                bad.append(blocks)
        bad += [self.content() + [self.content()[-1]], self.content() + [None],
                self.content() + [{"type": []}], self.content() + [{"type": "image"}],
                [{"type": "text", "text": "x" * 65537}] + self.content(),
                [{"type": "text", "text": "x", "unknown": True}] + self.content(),
                [{"type": "text", "text": "\ud800"}] + self.content(),
                [{"type": "text", "text": "x"}] * 65 + self.content()]
        for index, blocks in enumerate(bad):
            with self.subTest(index=index):
                (result,), calls = self.run_tools(blocks)
                self.assertEqual(result.result, "failed")
                self.assertEqual(len(calls), 1)
        huge = self.response([{"type": "text", "text": "x" * qualify.RESPONSE_MAX_BYTES}] + self.content())
        self.assertIsNone(qualify.returned_assistant_content(huge, "nonce"))

    def test_keyed_replay_forced_and_strict_auto_no_weaker_retry(self):
        for variant in qualify.TOOL_VARIANTS:
            (result,), calls = self.run_tools(variant=variant)
            self.assertEqual(result.result, "pass")
            self.assertEqual(calls[0][1]["tool_choice"]["type"], "tool" if variant == "forced" else "auto")
            self.assertEqual(calls[0][1]["tools"][0].get("strict"), True if variant == "auto" else None)
            bad = _Gateway({f"tools ({variant}) step 1": qualify.HttpResult(400, b"{}")})
            (result,) = qualify.run_battery(self.plan(tools_variant=variant), bad)
            self.assertEqual((result.result, len(bad.calls)), ("failed", 1))

    def test_keyed_replay_second_request_is_conditional_and_max_two(self):
        (result,), calls = self.run_tools([])
        self.assertEqual((result.result, len(calls)), ("failed", 1))
        (result,), calls = self.run_tools()
        self.assertEqual((result.result, len(calls)), ("pass", 2))
        gateway = _Gateway({"tools (forced) step 2": qualify.HttpResult(400, b"{}")})
        (result,) = qualify.run_battery(self.plan(), gateway)
        self.assertEqual((result.result, len(gateway.calls)), ("failed", 2))

    def test_keyed_replay_revalidates_before_each_send(self):
        seen = []
        self.run_tools(before_send=seen.append)
        self.assertEqual(seen, [0, 1])
        for stop in (0, 1):
            gateway = _Gateway()
            def check(sent):
                self.assertEqual(len(gateway.calls), sent)
                if sent == stop:
                    raise qualify.QualifyError("stale")
            with self.assertRaisesRegex(qualify.QualifyError, "stale"):
                qualify.run_battery(self.plan(), gateway, before_send=check)
            self.assertEqual(len(gateway.calls), stop)

    def test_keyed_replay_evidence_contains_no_content(self):
        (result,), _ = self.run_tools()
        evidence = json.dumps(result.record(AT, CONTRACTS))
        for value in ("private-reasoning", "private-signature", "private-text", "call_1", "nonce"):
            self.assertNotIn(value, evidence)

    def test_legacy_tools_step2_and_acceptance_unchanged(self):
        call = self.plan().calls[1]
        body = json.loads(qualify.tools_step2(call, "forced", "nonce", {"id": "call_1"}, "ack"))
        self.assertEqual(body["messages"][1]["content"], [self.content()[-1]])
        legacy = _plan(entry=_entry(["low", "high", "max"], selector="fixture-pool"), provider=POOL)
        gateway = _Gateway()
        qualify.run_battery(legacy, gateway)
        self.assertFalse(legacy.keyed_replay)
        self.assertEqual(len(gateway.calls), 7)
        self.assertTrue(all("thinking" not in body for _, body in gateway.calls))
        self.assertNotIn("output_config", gateway.calls[0][1])

    def test_keyed_effort_requests_use_client_thinking_form_legacy_unchanged(self):
        gateway = _Gateway()
        qualify.run_battery(self.plan(checks=("smoke", "efforts")), gateway)
        self.assertEqual([body["output_config"]["effort"] for _, body in gateway.calls],
                         ["high", "low", "high", "max"])
        self.assertTrue(all(body["thinking"] == {"type": "adaptive"} for _, body in gateway.calls))
        call = self.plan(checks=("efforts",)).calls[0]
        self.assertNotIn("thinking", json.loads(qualify.smoke_body(call)))


if __name__ == "__main__":
    unittest.main()
