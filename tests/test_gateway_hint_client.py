"""Native-client hint evidence, through the disposable gateway.

Host-only, full tier. No request replay, model substitution, detector-header
removal, or inference that a missing class passed. The native client and the
real gateway each run in a loopback-only namespace. A byte-preserving ingress
relay joins their unix bridges; bodies/headers stay in memory only. This tests
the Claude-compatible direct route, not the credentialed OAuth transport.
The sampled helper is /rename (wire class "auxiliary"); it is not a census of
all helper producers or a claim about the gateway's narrow Haiku exception.
The runtime hint-header policy stays OFF.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from claude_multi import catalog, compiler, operator, probe, render, state
from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog
from _tier import real_binary_gate
import _gateway_harness as harness

MODULE = "tests.test_gateway_hint_client"
REQUIRED_CLASSES = frozenset(("main", "subagent", "helper", "count-tokens"))
PREFIX = "x-claude-code-"
IDENTITY_NAMES = {PREFIX + name for name in ("session-id", "agent-id", "parent-agent-id")}
DROPPED_NAMES = {PREFIX + name for name in ("request-class", "agent-type", "prev-tool-durations")}


def native_selector(bundle):
    providers = bundle.docs["providers"]["providers"]
    for _key, line in sorted(bundle.lines.items()):
        if providers[line["provider"]]["adapter"] == "cliproxy-oauth-claude-v1":
            return next(selector for effort, selector, _contract in catalog.line_selectors(line)
                        if effort == line["default_effort"])
    raise AssertionError("the hint capture needs a fixture native-Claude selector")


def native_document(bundle, egress, gateway, home):
    document, _aliases, info = harness.build_document(bundle, egress, gateway, home)
    # Deliberately serve the gateway's embedded native-Claude registry, including
    # its helper wires. No model ids pinned in tests and no rewrites at ingress.
    # This is test-local direct-key substitution, NOT an OAuth-path claim.
    section = copy.deepcopy(document["claude-api-key"][0])
    section.update({"api-key": "dummy-gwtest-d7-upstream", "models": [],
                    "base-url": f"http://127.0.0.1:{egress}/gwtest-d7"})
    section.pop("headers", None)
    document.update({"claude-api-key": [section], "codex-api-key": [],
                     "oauth-model-alias": {}, "openai-compatibility": [],
                     "payload": {}})
    return render.finalize_document(document), {}, info


def request_class(path, headers):
    if urlsplit(path).path.endswith("/count_tokens"):
        return "count-tokens"
    value = headers.get(PREFIX + "request-class", "")
    if value in ("main", "subagent"):
        return value
    # The pinned client's query-source classifier calls helpers "auxiliary";
    # count_tokens has the same label, but is separated above by its route.
    if value == "auxiliary" and urlsplit(path).path == "/v1/messages":
        return "helper"
    return "unclassified"


def observation(path, headers, hits, status, raw=b"{}"):
    """Classify by the forwarded hint set, never by deleting browser headers."""
    kind = request_class(path, headers)
    model = json.loads(raw).get("model")
    incoming = sorted(name for name in headers if name.startswith(PREFIX))
    forwarded = sorted({name for hit in hits for name in hit[0] if name.startswith(PREFIX)})
    dropped = {name: headers[name] for name in DROPPED_NAMES & headers.keys()}
    # Native hint values such as "main" also occur in tool descriptions.
    # Preserve the required conservative raw-substring check, but distinguish
    # an existing body occurrence from an attributable header-value leak.
    leak = any(value and (any(value in item for item in outgoing.values()) or value.encode() in upstream_raw)
               for value in dropped.values() for outgoing, upstream_raw in hits)
    input_occurrence = any(value and value.encode() in raw for value in dropped.values())
    header_value = any(value and value == item for value in dropped.values()
                       for outgoing, _ in hits for name, item in outgoing.items() if name not in DROPPED_NAMES)
    body_delta = any(value and upstream_raw.count(value.encode()) > raw.count(value.encode())
                     for value in dropped.values() for _, upstream_raw in hits)
    if kind == "count-tokens" and not hits:
        branch = "local-count"
    elif len(hits) != 1:
        branch = "unresolved"
    elif dropped and not (DROPPED_NAMES & set(forwarded)) and set(forwarded) <= IDENTITY_NAMES:
        branch = "identity"
    elif dropped and all(name in forwarded for name in dropped):
        branch = "preserve"
    else:
        branch = "unresolved"
    return {"request_class": kind, "client_request_class": headers.get(PREFIX + "request-class"),
            "route": urlsplit(path).path, "routing_class": "claude-compatible-direct",
            "branch": branch, "status": status, "hits": len(hits),
            "incoming_hint_names": incoming, "upstream_hint_names": forwarded,
            "client_sent_browser_header": "anthropic-dangerous-direct-browser-access" in headers,
            "client_sent_accept_encoding": "accept-encoding" in headers,
            "client_sent_claude_code_beta": "claude-code-20250219" in headers.get("anthropic-beta", "").split(","),
            "client_model": model,
            "upstream_model_unchanged": all(json.loads(body).get("model") == model for _, body in hits) if hits else None,
            "dropped_value_in_upstream_headers": header_value, "dropped_value_body_delta": body_delta,
            "dropped_value_conservative_substring_match": leak, "dropped_value_in_original_body": input_occurrence,
            "value_scan": "exact header equality excluding dropped names; body occurrence increase; conservative scan is not proof"}


def completion_errors(runs, rows):
    errors = []
    if not runs or any(run["timed_out"] or run["returncode"] != 0 for run in runs):
        errors.append("hint capture requires rc 0 and no timeout for every client run")
    if any(not run["daemon_unchanged"] for run in runs):
        errors.append("hint capture daemon tripwire: unchanged domain required")
    observed = {row["request_class"] for row in rows}
    missing = sorted(REQUIRED_CLASSES - observed)
    if missing:
        errors.append("hint capture unresolved gate: missing request classes " + ", ".join(missing)
                      + "; needs a documented amendment or alternative capture")
    for row in rows:
        count = row["request_class"] == "count-tokens"
        if row["status"] != 200 or row["hits"] != (0 if count else 1):
            errors.append("hint capture request did not complete on the expected gateway path")
        if not count and not row["upstream_model_unchanged"]:
            errors.append("hint capture native selector was retargeted")
        if row["branch"] == "unresolved" or row["request_class"] == "unclassified":
            errors.append("hint capture request class or forwarded branch unresolved")
    return errors


class _ScriptedUpstream:
    """Canned native responses; captures full headers/body in memory only."""

    def __init__(self, path):
        self.hits = []
        self.forced = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw)
                owner.hits.append(({k.lower(): v for k, v in self.headers.items()}, raw))
                if urlsplit(self.path).path != "/gwtest-d7/v1/messages":
                    status, data, content_type = 404, b"{}", "application/json"
                else:
                    force = not owner.forced and any(tool.get("name") == "Agent" for tool in body.get("tools", []))
                    if force:
                        owner.forced = True
                        content = [{"type": "tool_use", "id": "toolu_gwtest_d7", "name": "Agent",
                                    "input": {"description": "hint capture offline probe", "subagent_type": "general-purpose",
                                              "prompt": "Reply with PROBE-OK"}}]
                    else:
                        content = [{"type": "text", "text": "PROBE-OK"}]
                    payload = probe._message_payload(content, model=body.get("model"),
                        stop_reason="tool_use" if force else "end_turn", message_id="msg_gwtest_d7")
                    status = 200
                    if body.get("stream"):
                        data = b"".join(harness.sse_frame(kind, value) for kind, value in probe._sse_message(payload).events)
                        content_type = "text/event-stream"
                    else:
                        data, content_type = json.dumps(payload).encode(), "application/json"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = probe.UnixHTTPServer(str(path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _RelayHandler(probe._ProviderHandler):
    def parse_request(self):
        parsed = super().parse_request()
        if parsed:
            with self.server.provider.lock:
                self.server.provider.ingress_count += 1
        return parsed

    def relay_transport(self):
        provider = self.server.provider
        with provider.lock:
            before = len(provider.gateway.upstream.hits)
            headers = {k.lower(): v for k, v in self.headers.items()}
            reply = harness.unix_request(provider.gateway.socket, self.command, self.path,
                headers={**headers, "host": f"127.0.0.1:{provider.gateway.port}",
                         "authorization": "Bearer " + harness.FIXTURE_GATEWAY_TOKEN},
                body=self.rfile.read(int(self.headers.get("Content-Length", 0))), timeout=45)
            provider.transport_rows.append({"run": provider.run_name, "method": self.command,
                "route": urlsplit(self.path).path, "status": reply.status,
                "upstream_hits": len(provider.gateway.upstream.hits) - before})
        self.send_response(reply.status)
        for name, value in reply.headers.items():
            if name not in {"connection", "transfer-encoding", "server", "date"}:
                self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(reply.raw)

    do_GET = relay_transport

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.relay_transport
        raise AttributeError(name)


class _GatewayRelay(probe.FakeAnthropicProvider):
    """The probe's allowed fake endpoint, forwarding original bytes to the gateway.

    Serialize requests so each upstream slice belongs to exactly one ingress
    request, even when the client's helpers run concurrently. Captures never
    leave memory; only observation()'s scalar metadata is published.
    """

    def __init__(self, gateway):
        super().__init__()
        self.gateway = gateway
        self.captured = {}
        self.rows = []
        self.run_name = None
        self.lock = threading.Lock()
        self.ingress_count = 0
        self.transport_rows = []

    def start(self):
        super().start()
        self._server.RequestHandlerClass = _RelayHandler
        return self

    def unix_socket(self, directory):
        path = super().unix_socket(directory)
        self._unix_servers[path][0].RequestHandlerClass = _RelayHandler
        return path

    def completion_errors(self):
        errors = []
        if (self.captured or len(self.requests) != len(self.rows)
                or self.ingress_count != len(self.rows) + len(self.transport_rows)):
            errors.append("hint capture relay locally answered or failed to relay a client request")
        if self.dropped_requests:
            errors.append("hint capture relay request accounting was truncated")
        return errors

    def _record(self, method, path, headers, body, document):
        context = super()._record(method, path, headers, body, document)
        with self.lock:
            normalized = {k.lower(): v for k, v in headers.items()}
            if len(normalized) != len(headers):
                raise AssertionError("hint capture refuses to collapse duplicate client headers")
            self.captured[context.ordinal] = (body, normalized)
        return context

    def _respond(self, document, path, context=None):
        with self.lock:
            raw, headers = self.captured.pop(context.ordinal)
            before = len(self.gateway.upstream.hits)
            # Transport addressing/auth only. Accept-Encoding, browser header,
            # metadata, model, and the entire original body are untouched.
            forwarded = {**headers, "host": f"127.0.0.1:{self.gateway.port}",
                         "authorization": "Bearer " + harness.FIXTURE_GATEWAY_TOKEN}
            forwarded.pop("x-api-key", None)
            reply = harness.unix_request(self.gateway.socket, "POST", path,
                                         headers=forwarded, body=raw, timeout=45)
            hits = self.gateway.upstream.hits[before:]
            # Still under the serialization lock: this slice belongs to this
            # one ingress request only (OAuth recognition relies on the same seam).
            self._observe(path, headers, hits, reply.status, raw, document)
            if "text/event-stream" in reply.headers.get("content-type", ""):
                events = []
                for frame in reply.raw.decode().split("\n\n"):
                    kind = next((line[7:] for line in frame.splitlines() if line.startswith("event: ")), None)
                    data = next((line[6:] for line in frame.splitlines() if line.startswith("data: ")), None)
                    if kind and data:
                        events.append((kind, json.loads(data)))
                return reply.status, probe.SseResponse(tuple(events))
            return reply.status, reply.json()

    def _observe(self, path, headers, hits, status, raw, _document):
        """Called with the lock held and the exact upstream slice."""
        self.rows.append({"run": self.run_name, **observation(path, headers, hits, status, raw)})


class HintClientSelfTests(unittest.TestCase):
    def test_missing_class_and_unsuccessful_run_never_pass(self):
        rows = [{"request_class": kind, "status": 200, "hits": 0 if kind == "count-tokens" else 1,
                 "branch": "local-count" if kind == "count-tokens" else "preserve", "upstream_model_unchanged": True}
                for kind in sorted(REQUIRED_CLASSES)]
        runs = [{"returncode": 0, "timed_out": False, "daemon_unchanged": True}]
        self.assertEqual(completion_errors(runs, rows), [])
        for kind in REQUIRED_CLASSES:
            with self.subTest(kind=kind):
                self.assertTrue(completion_errors(runs, [row for row in rows if row["request_class"] != kind]))
        for change in ({"returncode": 1}, {"timed_out": True}, {"daemon_unchanged": False}):
            self.assertTrue(completion_errors([{**runs[0], **change}], rows))
        self.assertTrue(completion_errors([], rows))
        for change in ({"status": 500}, {"hits": 2}, {"branch": "unresolved"}, {"request_class": "unclassified"}):
            self.assertTrue(completion_errors(runs, [{**rows[0], **change}, *rows[1:]]))

    def test_branch_uses_hints_even_when_browser_header_was_sent(self):
        headers = {PREFIX + "request-class": "helper", "anthropic-dangerous-direct-browser-access": "true",
                   "accept-encoding": "gzip, deflate, br, zstd"}
        self.assertEqual(observation("/v1/messages", headers, [(headers, b"{}")], 200)["branch"], "preserve")
        row = observation("/v1/messages", headers, [({PREFIX + "session-id": "fixture"}, b"{}")], 200)
        self.assertEqual(row["branch"], "identity")
        self.assertTrue(row["client_sent_browser_header"])
        self.assertFalse(row["dropped_value_in_upstream_headers"])
        self.assertFalse(row["dropped_value_body_delta"])
        row = observation("/v1/messages", headers, [({"other": "helper"}, b"{}")], 200)
        self.assertTrue(row["dropped_value_in_upstream_headers"])

    def test_value_evidence_distinguishes_existing_text_substrings_and_deltas(self):
        headers = {PREFIX + "request-class": "main"}
        raw = b'{"text":"main"}'
        row = observation("/v1/messages", headers, [({"other": "domain"}, raw)], 200, raw)
        self.assertTrue(row["dropped_value_conservative_substring_match"])
        self.assertTrue(row["dropped_value_in_original_body"])
        self.assertFalse(row["dropped_value_in_upstream_headers"])
        self.assertFalse(row["dropped_value_body_delta"])
        row = observation("/v1/messages", headers, [(headers, raw)], 200, raw)
        self.assertFalse(row["dropped_value_in_upstream_headers"], "preserved dropped-name header is not re-encoding")
        row = observation("/v1/messages", headers, [({"other": "main"}, b'{"text":"main main"}')], 200, raw)
        self.assertTrue(row["dropped_value_in_upstream_headers"])
        self.assertTrue(row["dropped_value_body_delta"])

    def test_transport_requests_relay_and_locally_answered_posts_fail_completion(self):
        gateway = SimpleNamespace(socket=Path("/unused"), port=55112, upstream=SimpleNamespace(hits=[]))
        with tempfile.TemporaryDirectory(prefix="gwtest-d7-relay-") as temporary, _GatewayRelay(gateway) as relay:
            self.assertEqual(relay.completion_errors(), [])
            socket = relay.unix_socket(Path(temporary))
            request = harness.unix_request
            for method, path in (("GET", "/v1/models"), ("HEAD", "/api/hello")):
                reply = harness.Reply(404, {"content-length": "0"}, b"")
                with mock.patch.object(harness, "unix_request", return_value=reply) as forward:
                    self.assertEqual(request(socket, method, path).status, 404)
                    self.assertEqual(forward.call_args.args, (gateway.socket, method, path))
            self.assertEqual(relay.ingress_count, 2)
            self.assertEqual(len(relay.transport_rows), 2)
            self.assertEqual(relay.completion_errors(), [])
            self.assertEqual(harness.unix_request(socket, "POST", "/v1/messages", body=b"[]").status, 400)
            self.assertEqual(relay.ingress_count, 3)
            self.assertEqual(len(relay.captured), 1)
            self.assertEqual(len(relay.requests), 1)
            self.assertFalse(relay.rows)
            self.assertTrue(relay.completion_errors())

    def test_auxiliary_messages_are_helpers_but_token_counting_is_separate(self):
        headers = {PREFIX + "request-class": "auxiliary"}
        self.assertEqual(request_class("/v1/messages?beta=true", headers), "helper")
        self.assertEqual(request_class("/v1/messages/count_tokens?beta=true", headers), "count-tokens")
        self.assertEqual(request_class("/v1/messages", {}), "unclassified")

    def test_relay_keeps_original_body_model_and_every_detector_header(self):
        gateway = SimpleNamespace(socket=Path("/unused"), port=55112, upstream=SimpleNamespace(hits=[]))
        relay = _GatewayRelay(gateway)
        headers = {"Accept-Encoding": "gzip, deflate, br, zstd", "Anthropic-Dangerous-Direct-Browser-Access": "true",
                   "Anthropic-Beta": "native-beta", "X-App": "cli", "User-Agent": "native-client",
                   "X-Claude-Code-Request-Class": "auxiliary", "Host": "127.0.0.1:55113"}
        # Deliberately non-canonical JSON: equality must be byte-for-byte, not reserialization.
        raw = b'{ "model" : "gwtest-native", "messages": [], "metadata": {"user_id":"original"} }'
        context = relay._record("POST", "/v1/messages?beta=true", headers, raw, json.loads(raw))
        reply = harness.Reply(200, {"content-type": "application/json"}, b'{"type":"message"}')
        with mock.patch.object(harness, "unix_request", return_value=reply) as request:
            status, _payload = relay._respond(json.loads(raw), "/v1/messages?beta=true", context)
        self.assertEqual(status, 200)
        self.assertEqual(request.call_args.args, (gateway.socket, "POST", "/v1/messages?beta=true"))
        self.assertEqual(request.call_args.kwargs["body"], raw)
        actual = request.call_args.kwargs["headers"]
        for name, value in headers.items():
            if name != "Host":
                self.assertEqual(actual[name.lower()], value)
        self.assertEqual(relay.captured, {})

    def test_native_routing_keeps_detector_model_and_no_external_upstream(self):
        bundle = catalog.load_catalog(FIXTURE_ROOT)
        with tempfile.TemporaryDirectory() as root:
            document, _aliases, _info = native_document(bundle, 55111, 55112, Path(root))
        self.assertTrue(native_selector(bundle))
        self.assertEqual(document["claude-api-key"][0]["models"], [])
        self.assertEqual(document["claude-api-key"][0]["base-url"], "http://127.0.0.1:55111/gwtest-d7")
        self.assertEqual(document["oauth-model-alias"], {})
        self.assertEqual(document["codex-api-key"], [])
        self.assertEqual(document["payload"], {})
        self.assertEqual(len(document["openai-compatibility"]), 1)
        self.assertEqual(document["openai-compatibility"][0]["name"], render.SENTINEL_NAME)


@uses_shipped_catalog
class RealClientHintBranchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate = real_binary_gate(SHIPPED_ROOT / "catalog/native-contract.json", what="the real-client hint capture")
        if gate.boundary:
            raise unittest.SkipTest(gate.boundary)
        cls.trusted, cls.version = gate.trusted, gate.version

    def test_d7_completed_client_covers_every_request_class(self):
        started = time.monotonic()
        root = Path(tempfile.mkdtemp(prefix="gwtest-d7-"))
        self.addCleanup(shutil.rmtree, root)
        environ = {"HOME": str(root / "ambient/home"), "PATH": "/usr/bin:/bin"}
        fixture = probe.build_fixture(root / "client", environ=environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        selector = native_selector(catalog.load_catalog(FIXTURE_ROOT))
        gateway = harness.GatewayHarness(document_factory=native_document, upstream_factory=_ScriptedUpstream)
        self.addCleanup(gateway.close)
        runs = []
        session_id = str(uuid.uuid4())
        with _GatewayRelay(gateway) as relay:
            # /context produces native count_tokens; bare /rename on the
            # resumed synthetic conversation invokes the client's name helper.
            # These are real client operations, not injected helper requests.
            for name, prompt in (("delegation", "hint capture offline turn"), ("context", "/context"), ("rename", "/rename")):
                relay.run_name = name
                result = probe.run_native(
                    ("-p", prompt, "--model", selector, "--output-format", "json",
                     "--dangerously-skip-permissions", "--session-id" if name == "delegation" else "--resume", session_id),
                    trusted=self.trusted, fixture=fixture, provider=relay, allow_real=True,
                    environ=environ, timeout=120, child_env={"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"})
                runs.append({"row": name, "returncode": result.returncode, "timed_out": result.timed_out,
                             "daemon_unchanged": result.daemon is not None and result.daemon.unchanged})
        errors = completion_errors(runs, relay.rows) + relay.completion_errors()
        evidence = harness.Evidence()
        evidence.binary = {"realpath": gateway.binary, "version": gateway.version}
        for row in runs:
            evidence.record(MODULE, "runs", row)
        for index, row in enumerate(relay.rows):
            evidence.record(MODULE, "requests", {"row": str(index), **row})
        for index, row in enumerate(relay.transport_rows):
            evidence.record(MODULE, "transport", {"row": str(index), **row})
        observed = {row["request_class"] for row in relay.rows}
        for kind in sorted(REQUIRED_CLASSES):
            evidence.record(MODULE, "coverage", {"row": kind, "observed": kind in observed})
        helpers = [row for row in relay.rows if row["request_class"] == "helper"]
        evidence.record(MODULE, "coverage", {"row": "helper-profile",
            "no_beta_helper_observed": any(not row["client_sent_claude_code_beta"] for row in helpers),
            "measured_haiku_profile": "not established by auxiliary class coverage",
            "scope": "observed auxiliary helpers on the claude-compatible direct route only"})
        evidence.record(MODULE, "completion", {"row": "native-capture", "complete": not errors,
            "ingress_requests": relay.ingress_count,
            "relayed_requests": len(relay.rows) + len(relay.transport_rows),
            "unrelayed_captures": len(relay.captured),
            "errors": errors, "client_version": self.version, "client_sha256": self.trusted.sha256,
            "seconds": round(time.monotonic() - started, 3), "hint_policy": "off"})
        # Normal execution, before assertions, so an unresolved gate leaves an
        # honest diagnostic artifact and a write error can never pass silently.
        evidence.finalize_module(MODULE)
        print("hint capture: classes=" + ",".join(sorted(observed)) + "; complete=" + str(not errors))
        self.assertTrue(gateway.upstream.forced, "hint capture subagent dispatch was not forced")
        self.assertTrue(all(run["daemon_unchanged"] for run in runs), "hint capture daemon tripwire")
        self.assertEqual(errors, [], "; ".join(errors))


# ------------------------------------------------------- OAuth recognition

D11_MODULE = "tests.test_gateway_hint_client.d11"
D11_PREVIOUS = ("2.1.281", "56fe3da88458465fb27d7e9299dddb3fead55750fb9c2de795f233b5eea6dce1")
D11_OAUTH_TOKEN = "sk-ant-oat01-probe-d11-fake"
# A fixture project agent. Only its definition body, which becomes the
# child's system prompt, carries the marker: the parent's Agent tool lists
# the agent's name and description, never its body, so a request carrying
# the marker in ``system`` is the child's own (no hint headers needed).
D11_CHILD_AGENT = "probe-d11-child"
D11_CHILD_MARKER = "PROBE-D11-CHILD-SYSTEM-MARKER"
D11_CHILD_DEFINITION = (f"---\nname: {D11_CHILD_AGENT}\ndescription: probe recognition disposable child\n"
                        f"model: inherit\n---\n{D11_CHILD_MARKER}. Reply with PROBE-OK.\n")
# Negative control: the forced Agent names an agent that does not exist, so
# no child runs; the parent's continuation must never count as a subagent.
D11_MISSING_AGENT = "probe-d11-no-such-agent"
D11_UNRESOLVED = "unresolved-capture"
# The only body normalizations a confirmed request may show on the Claude
# OAuth pool (ApplyClaudeCredentialMetadata): the selected credential's
# identity inside metadata.user_id (device_id/account_uuid), a subagent's
# per-agent conversation session_id derived exactly from the client's own
# session_id and X-Claude-Code-Agent-Id (every other member preserved), and
# the billing block's cch= attestation.
D11_AGENT_SESSION = "metadata.user_id.session_id:agent-conversation"
D11_PERMITTED = frozenset({"metadata.user_id.device_id", "metadata.user_id.account_uuid", D11_AGENT_SESSION,
                           "system:cch"})
# Silently answered by another model, on another pool or another endpoint.
D11_RETARGET = frozenset({"model", "pool", "route"})
# Cosmetic or advisory output (title/rename, token counting).
D11_COSMETIC = frozenset({"title", "count-tokens"})
D11_HELPERS = frozenset({"title", "helper", "probe"})
D11_REQUIRED = ("main", "subagent", "count-tokens", "rename-helper", "haiku-helper")
D11_HAIKU_RUN = "interactive-haiku"
_BODY_KEYS_IGNORED = frozenset({"messages"})
_PTY_PROMPT = "❯".encode()


def native_reply(body, content, stop):
    """Canned native reply bytes for a parsed request body (stream-aware)."""
    payload = probe._message_payload(content, model=body.get("model"), stop_reason=stop,
                                     message_id="msg_probe_d11")
    if body.get("stream"):
        return 200, b"".join(harness.sse_frame(kind, value) for kind, value in
                             probe._sse_message(payload).events), "text/event-stream"
    return 200, json.dumps(payload).encode(), "application/json"


class _OAuthTLSUpstream:
    """The Claude OAuth pool's upstream, offline: a CONNECT endpoint for
    exactly ``api.anthropic.com:443`` that terminates TLS with a fixture
    certificate (the gateway trusts it through ``SSL_CERT_FILE``) and serves
    canned native replies. Headers and bodies stay in memory only; each hit
    is ``(headers, raw, route)``.

    ``force_agent`` (one-shot) answers the next request offering the Agent
    tool with an Agent call for that agent type; ``respond`` replaces the
    canned replies with ``respond(route, body) -> (status, data, type)``."""

    def __init__(self, path, tls, respond=None):
        self.hits = []
        self.connects = []
        self.force_agent = None
        self.forced = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(tls[0]), str(tls[1]))
        owner = self

        class Inner(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                self._send(404, b"{}", "application/json")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.loads(raw) if raw else {}
                route = urlsplit(self.path).path
                owner.hits.append(({k.lower(): v for k, v in self.headers.items()}, raw, route))
                if respond is not None:
                    self._send(*respond(route, body))
                    return
                if route.endswith("/count_tokens"):
                    self._send(200, b'{"input_tokens": 1}', "application/json")
                    return
                agent = owner.force_agent
                if agent and any(tool.get("name") == "Agent" for tool in body.get("tools") or ()):
                    owner.force_agent = None
                    owner.forced.append(agent)
                    self._send(*native_reply(body, [{"type": "tool_use", "id": "toolu_probe_d11", "name": "Agent",
                                                     "input": {"description": "recognition offline probe",
                                                               "subagent_type": agent,
                                                               "prompt": "Reply with PROBE-OK"}}], "tool_use"))
                    return
                self._send(*native_reply(body, [{"type": "text", "text": "PROBE-OK"}], "end_turn"))

            def _send(self, status, data, content_type):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Outer(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_CONNECT(self):
                owner.connects.append(self.path)
                if self.path != "api.anthropic.com:443":
                    self.send_error(403)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                try:
                    with context.wrap_socket(self.connection, server_side=True) as connection:
                        Inner(connection, self.client_address, self.server)
                except (OSError, ssl.SSLError):
                    pass
                self.close_connection = True

        self.server = probe.UnixHTTPServer(str(path), Outer)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def shipped_claude_pool():
    """The shipped catalog's real render, Claude OAuth pool sections only:
    its served alias table and the T1 registry overlay (the final Sonnet
    overlay among them). No other provider is rendered into the fixture."""
    bundle = catalog.load_catalog(SHIPPED_ROOT)
    docs = copy.deepcopy(bundle.docs)
    plan = operator.render_plan(docs, operator.empty_layer(), None)
    gateway_doc = copy.deepcopy(docs["gateway"])
    gateway_doc["gateway"]["base_url"] = "http://127.0.0.1:1"
    document, _available, _unavailable, _info = render.build_config_document(
        gateway_doc, plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"],
        home=Path("/nonexistent/probe-d11"), gateway_token=harness.FIXTURE_GATEWAY_TOKEN,
        resolve_secret=lambda _name: "dummy-probe-d11", continuity={}, oauth_overlay=plan.overlay,
        overlay_static=None)
    pool = {"oauth-model-alias": {"claude": copy.deepcopy(document["oauth-model-alias"].get("claude", []))}}
    overlay = (document.get(render.OVERLAY_KEY) or {}).get("claude")
    if overlay:
        pool[render.OVERLAY_KEY] = {"claude": copy.deepcopy(overlay)}
    return pool


def start_oauth_gateway(root, respond=None):
    """The candidate gateway on the Claude OAuth pool path: a file OAuth
    credential, the shipped Claude-pool render, and the gateway's own HTTPS
    client to api.anthropic.com reaching a fixture TLS endpoint through the
    reviewed ``proxy-url`` render seam. Hint headers stay OFF."""
    tls = state.ensure_private_dir(root / "tls")
    cert, key = tls / "upstream.pem", tls / "upstream-key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj",
                    "/CN=api.anthropic.com", "-addext", "subjectAltName=DNS:api.anthropic.com",
                    "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True, timeout=60, env={"PATH": "/usr/bin:/bin"})
    holder = {}
    pool = shipped_claude_pool()

    def document(bundle, egress, gateway, home):
        document, _aliases, info = harness.build_document(bundle, egress, gateway, home)
        document.pop(render.OVERLAY_KEY, None)
        document.update({"claude-api-key": [], "codex-api-key": [], "oauth-model-alias": {},
                         "openai-compatibility": [], "payload": {},
                         "proxy-url": f"http://127.0.0.1:{egress}", **copy.deepcopy(pool)})
        auth = Path(document["auth-dir"])
        state.ensure_private_dir(auth)
        state.atomic_write(auth / "claude-probe-d11.json", json.dumps(
            {"type": "claude", "email": "probe-d11@example.invalid",
             "access_token": D11_OAUTH_TOKEN, "scope": "user:inference",
             "expired": "2099-01-01T00:00:00Z"}).encode())
        trust = home / ".config/claude-multi/probe-d11-ca.pem"
        state.atomic_write(trust, cert.read_bytes())
        holder["trust"] = str(trust)
        return render.finalize_document(document), {}, info

    original = probe.start_isolated_process

    def start(argv, **kwargs):
        if "trust" in holder:  # the gateway itself, not the --version banner probe
            kwargs["env"] = {**kwargs["env"], "SSL_CERT_FILE": holder["trust"]}
        return original(argv, **kwargs)

    with mock.patch.object(probe, "start_isolated_process", start):
        return harness.GatewayHarness(document_factory=document,
                                      upstream_factory=lambda path: _OAuthTLSUpstream(path, (cert, key), respond))


def gateway_identity(gateway):
    return {"realpath": gateway.binary, "version": gateway.version,
            "sha256": probe._sha256_file(Path(gateway.binary))}


def d11_body_delta(client_raw: bytes, upstream_raw: bytes, agent_id: str | None = None) -> tuple[str, ...]:
    """The exact body delta classes between the client's and the gateway's
    outgoing body: changed top-level keys (``metadata`` by sub-key, a JSON
    ``metadata.user_id`` by member), the messages array, and tool names
    (MCP/tool aliasing). ``agent_id`` is the client's X-Claude-Code-Agent-Id.
    Pure."""

    client, upstream = json.loads(client_raw), json.loads(upstream_raw)
    delta = []
    for key in sorted(set(client) | set(upstream)):
        if key in _BODY_KEYS_IGNORED or client.get(key) == upstream.get(key):
            continue
        if key == "system":
            delta.append(_system_delta(client.get(key), upstream.get(key)))
            continue
        if key == "metadata" and isinstance(client.get(key), dict) and isinstance(upstream.get(key), dict):
            changed = sorted(name for name in set(client[key]) | set(upstream[key])
                             if client[key].get(name) != upstream[key].get(name))
            for name in changed:
                if name == "user_id":
                    delta.extend(_user_id_delta(client[key].get(name), upstream[key].get(name), agent_id))
                else:
                    delta.append(f"metadata.{name}")
        else:
            delta.append(key)
    if client.get("messages") != upstream.get("messages"):
        delta.append("messages")
    names = lambda body: [tool.get("name") for tool in body.get("tools") or () if isinstance(tool, dict)]
    if names(client) != names(upstream):
        delta.append("tool-names")
    return tuple(delta)


def _user_id_delta(client, upstream, agent_id=None) -> list[str]:
    """``metadata.user_id.<member>`` per changed member when both sides are
    JSON-object strings (the native shape), else ``metadata.user_id``. A
    session_id equal to the gateway's agent-conversation id of exactly the
    client's session and agent (claudeAgentSessionUUID over
    ``claude:<session>:agent:<agent>``) is named as that derivation."""

    def members(value):
        try:
            parsed = json.loads(value) if isinstance(value, str) else None
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    before, after = members(client), members(upstream)
    if before is None or after is None:
        return ["metadata.user_id"]
    delta = []
    for name in sorted(set(before) | set(after)):
        if before.get(name) == after.get(name):
            continue
        if name == "session_id" and agent_id and isinstance(before.get(name), str) \
                and after.get(name) == d11_agent_session(before[name], agent_id):
            delta.append(D11_AGENT_SESSION)
        else:
            delta.append(f"metadata.user_id.{name}")
    return delta


def d11_agent_session(session_id: str, agent_id: str) -> str:
    """The gateway's stable per-agent conversation id (SHA-1 name UUID)."""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, "cli-proxy-api\x00claude\x00agent-conversation\x00"
                          f"claude:{session_id}:agent:{agent_id}"))


_CCH = re.compile(r"\s*cch=[0-9a-zA-Z]+;?")


def _system_delta(client, upstream) -> str:
    """``system:cch`` when the only change is the billing block's ``cch=``
    attestation (the gateway signing a native body on an OAuth credential;
    identical on the previous pin), else ``system:<client blocks>-><upstream
    blocks>`` (prompt injection or rewrite: cloaking)."""

    def blocks(value):
        if isinstance(value, str):
            return [(value, None)]
        return [((block.get("text") if isinstance(block, dict) else block),
                 json.dumps({k: v for k, v in block.items() if k != "text"}, sort_keys=True)
                 if isinstance(block, dict) else None) for block in value or ()]

    before, after = blocks(client), blocks(upstream)
    strip = lambda items: [(_CCH.sub("", str(text)), rest) for text, rest in items]
    if len(before) == len(after) and strip(before) == strip(after):
        return "system:cch"
    return f"system:{len(before)}->{len(after)}"


# A cloaked (unconfirmed) Claude OAuth request: the gateway injects a system
# prompt, rewrites cache_control/TTL or aliases tool names. Used only
# to prove the detector discriminates; confirmation is positive (D11_PERMITTED).
CLOAK_DELTAS = frozenset({"tool-names", "messages", "tools"})


def _cloaked(delta) -> bool:
    return bool(CLOAK_DELTAS & set(delta)) or any(
        item.startswith("system:") and item != "system:cch" for item in delta)


def _system_text(body) -> str:
    system = body.get("system")
    if isinstance(system, str):
        return system
    return "\n".join(str(block.get("text", "")) for block in system or () if isinstance(block, dict))


def _title_helper(body) -> bool:
    """The gateway's title-helper fingerprint (isClaudeTitleHelperRequest)."""
    props = (((body.get("output_config") or {}).get("format") or {}).get("schema") or {}).get("properties")
    if isinstance(props, dict) and set(props) == {"title"}:
        return True
    text = _system_text(body)
    return any(marker in text for marker in ("naming a coding session", "Return a short title",
                                             "Write the title in the predominant language"))


def d11_class(route: str, body: dict) -> str:
    """Request class from evidence the request itself carries: the
    count_tokens route; the child agent's system-only marker (never a prompt
    the parent also carries); the gateway's probe (max_tokens 1, no tools)
    and title-helper fingerprints; any other tool-less request is a helper;
    the rest is the main session."""
    if route.endswith("/count_tokens"):
        return "count-tokens"
    if D11_CHILD_MARKER in _system_text(body):
        return "subagent"
    if body.get("tools"):
        return "main"
    if body.get("max_tokens") == 1:
        return "probe"
    return "title" if _title_helper(body) else "helper"


def d11_row(run, route, raw, body, status, hits, headers=None):
    """One ingress request with its exact serialized upstream slice. A
    missing, repeated or otherwise unattributable upstream request is
    ``unresolved`` (never confirmed); a different model, credential pool or
    endpoint is a retarget."""
    kind, client_headers = d11_class(route, body), headers
    if len(hits) != 1:
        delta = (D11_UNRESOLVED,)
    else:
        headers, upstream_raw, upstream_route = hits[0]
        delta = d11_body_delta(raw, upstream_raw, (client_headers or {}).get("x-claude-code-agent-id"))
        if headers.get("authorization") != "Bearer " + D11_OAUTH_TOKEN:
            delta += ("pool",)
        if upstream_route != route:
            delta += ("route",)
    return {"run": run, "class": kind, "status": status, "upstream_hits": len(hits), "delta": delta,
            "model": body.get("model"), "max_tokens": body.get("max_tokens"),
            "tools": len(body.get("tools") or ()), "stream": bool(body.get("stream"))}


def d11_disposition(kind: str, delta: tuple[str, ...], previous=None) -> str:
    """The recognition classes, positive: confirmed only when the upstream request
    is attributable and differs from the client's by nothing but the
    permitted normalizations. Unattributable evidence is ``unresolved``; a
    model/pool/endpoint change blocks for every class; a modified main or
    subagent request blocks; another modified class is carried when the
    previous pin showed the identical delta (``previous``: its deltas for
    that class), a named exception for cosmetic output, else it blocks."""

    if D11_UNRESOLVED in delta:
        return "unresolved"
    if D11_RETARGET & set(delta):
        return "blocks"
    if set(delta) <= D11_PERMITTED:
        return "confirmed"
    if kind in ("main", "subagent"):
        return "blocks"
    if previous and set(delta) in [set(item) for item in previous if D11_UNRESOLVED not in item]:
        return "carried"
    return "named-exception" if kind in D11_COSMETIC else "blocks"


def d11_coverage(rows) -> dict[str, bool]:
    """Positive evidence per required recognition class: the subagent class needs
    the child's own request; the rename helper is the tool-less helper
    request of the /rename run; the Haiku helper is a helper-class request
    of the Haiku run on the client's Haiku family selector (recorded apart
    from title/rename)."""
    classes = {row["class"] for row in rows}
    return {
        "main": "main" in classes,
        "subagent": "subagent" in classes,
        "count-tokens": "count-tokens" in classes,
        "rename-helper": any(row["run"] == "rename" and row["class"] in D11_HELPERS for row in rows),
        "haiku-helper": any(row["run"] == D11_HAIKU_RUN and row["class"] in D11_HELPERS
                            and "haiku" in str(row["model"]) for row in rows),
    }


def d11_errors(runs, rows, previous=None) -> list[str]:
    """Every recognition gate failure for one client capture (empty = qualified)."""
    errors = []
    if not runs or any(run["returncode"] != 0 or run["timed_out"] or not run["daemon_unchanged"] for run in runs):
        errors.append("recognition requires rc 0, no timeout and an unchanged daemon domain for every run")
    missing = [kind for kind, seen in d11_coverage(rows).items() if not seen]
    if missing:
        errors.append("recognition missing positive class evidence: " + ", ".join(missing))
    for row in rows:
        if row["status"] != 200:
            errors.append(f"recognition {row['run']}/{row['class']} did not complete (status {row['status']})")
        disposition = d11_disposition(row["class"], row["delta"], (previous or {}).get(row["class"]))
        if disposition in ("unresolved", "blocks"):
            errors.append(f"recognition {row['run']}/{row['class']} {disposition}: {list(row['delta'])}")
        if row["class"] in ("main", "subagent") and disposition != "confirmed":
            errors.append(f"recognition {row['class']} must be confirmed exactly")
    return errors


class _D11Relay(_GatewayRelay):
    """The serialized relay, recording recognition rows under its lock."""

    def __init__(self, gateway):
        super().__init__(gateway)
        self.d11_rows = self.rows  # one row per relayed request (completion_errors)

    def _observe(self, path, headers, hits, status, raw, document):
        self.d11_rows.append(d11_row(self.run_name, urlsplit(path).path, raw, document, status, hits, headers))


class OAuthRecognitionSelfTests(unittest.TestCase):
    def test_delta_and_disposition(self):
        base = {"model": "m", "system": [{"type": "text", "text": "s"}], "metadata": {"user_id": "u"},
                "messages": [], "tools": [{"name": "Bash"}]}
        raw = json.dumps(base).encode()
        self.assertEqual(d11_body_delta(raw, raw), ())
        changed = {**base, "system": [{"type": "text", "text": "cloak"}, *base["system"]],
                   "metadata": {"user_id": "other"}, "tools": [{"name": "mcp_Bash"}]}
        self.assertEqual(d11_body_delta(raw, json.dumps(changed).encode()),
                         ("metadata.user_id", "system:1->2", "tools", "tool-names"))
        signed = {**base, "system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=1;"}]}
        resigned = {**base, "system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=1; cch=a1b2c;"}]}
        self.assertEqual(d11_body_delta(json.dumps(signed).encode(), json.dumps(resigned).encode()), ("system:cch",))
        native = {**base, "metadata": {"user_id": json.dumps({"device_id": "d", "account_uuid": "a", "session_id": "s"})}}
        credential = {**base, "metadata": {"user_id": json.dumps({"device_id": "D", "account_uuid": "A", "session_id": "s"})}}
        session = {**base, "metadata": {"user_id": json.dumps({"device_id": "D", "account_uuid": "A", "session_id": "S"})}}
        self.assertEqual(d11_body_delta(json.dumps(native).encode(), json.dumps(credential).encode()),
                         ("metadata.user_id.account_uuid", "metadata.user_id.device_id"))
        delta = d11_body_delta(json.dumps(native).encode(), json.dumps(session).encode())
        self.assertIn("metadata.user_id.session_id", delta)
        self.assertEqual(d11_disposition("main", delta), "blocks", "a session-id rewrite is not permitted")
        self.assertEqual(d11_body_delta(json.dumps(native).encode(), json.dumps(session).encode(), "a1"), delta)
        derived = {**base, "metadata": {"user_id": json.dumps(
            {"device_id": "D", "account_uuid": "A", "session_id": d11_agent_session("s", "a1")})}}
        delta = d11_body_delta(json.dumps(native).encode(), json.dumps(derived).encode(), "a1")
        self.assertEqual(delta, ("metadata.user_id.account_uuid", "metadata.user_id.device_id", D11_AGENT_SESSION))
        self.assertEqual(d11_disposition("subagent", delta), "confirmed")
        # Derived for another agent, or without the client's agent id: a rewrite.
        self.assertIn("metadata.user_id.session_id",
                      d11_body_delta(json.dumps(native).encode(), json.dumps(derived).encode(), "a2"))
        self.assertIn("metadata.user_id.session_id",
                      d11_body_delta(json.dumps(native).encode(), json.dumps(derived).encode()))
        self.assertEqual(d11_disposition("main", ("metadata.user_id.device_id", "system:cch")), "confirmed")
        self.assertEqual(d11_disposition("main", ()), "confirmed")
        self.assertEqual(d11_disposition("main", ("system:1->2",), [("system:1->2",)]), "blocks")
        self.assertEqual(d11_disposition("title", ("system:1->2",), [("system:1->2",)]), "carried")
        self.assertEqual(d11_disposition("title", ("system:1->2",), None), "named-exception")
        self.assertEqual(d11_disposition("helper", ("system:1->2",), None), "blocks")
        self.assertEqual(d11_disposition("classifier", ("system:1->2",), None), "blocks")

    def test_unattributable_and_retargeted_requests_never_confirm(self):
        # Review negative controls: an unresolved capture and a model,
        # pool or endpoint change are never confirmed, for any class.
        for kind in ("main", "subagent", "title", "count-tokens", "probe"):
            with self.subTest(kind=kind):
                self.assertEqual(d11_disposition(kind, (D11_UNRESOLVED,)), "unresolved")
                self.assertEqual(d11_disposition(kind, (D11_UNRESOLVED,), [(D11_UNRESOLVED,)]), "unresolved")
                for item in sorted(D11_RETARGET):
                    self.assertEqual(d11_disposition(kind, (item,)), "blocks")
                    self.assertEqual(d11_disposition(kind, (item,), [(item,)]), "blocks")
        body = {"model": "m", "messages": [], "tools": [{"name": "Bash"}]}
        raw = json.dumps(body).encode()
        upstream = ({"authorization": "Bearer " + D11_OAUTH_TOKEN}, raw, "/v1/messages")
        self.assertEqual(d11_row("r", "/v1/messages", raw, body, 200, [upstream])["delta"], ())
        for hits in ([], [upstream, upstream]):
            self.assertEqual(d11_row("r", "/v1/messages", raw, body, 200, hits)["delta"], (D11_UNRESOLVED,))
        retarget = json.dumps({**body, "model": "other"}).encode()
        self.assertEqual(d11_row("r", "/v1/messages", raw, body, 200,
                                 [(upstream[0], retarget, "/v1/messages")])["delta"], ("model",))
        self.assertEqual(d11_row("r", "/v1/messages", raw, body, 200,
                                 [({"authorization": "Bearer other"}, raw, "/v1/messages")])["delta"], ("pool",))
        self.assertEqual(d11_row("r", "/v1/messages", raw, body, 200,
                                 [(upstream[0], raw, "/v1/messages/count_tokens")])["delta"], ("route",))

    def test_classes_need_evidence_unique_to_the_request(self):
        self.assertEqual(d11_class("/v1/messages/count_tokens", {}), "count-tokens")
        self.assertEqual(d11_class("/v1/messages", {"tools": []}), "helper")
        self.assertEqual(d11_class("/v1/messages", {"max_tokens": 1}), "probe")
        self.assertEqual(d11_class("/v1/messages", {"output_config": {"format": {"schema": {
            "properties": {"title": {"type": "string"}}}}}}), "title")
        self.assertEqual(d11_class("/v1/messages", {"tools": [1], "system": [{"text": D11_CHILD_MARKER}]}), "subagent")
        # The parent's history after a (failed or successful) dispatch holds
        # the agent type and prompt, never the child's system marker.
        parent = {"tools": [1], "system": [{"text": "parent"}], "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "name": "Agent", "input": {
                "subagent_type": D11_CHILD_AGENT, "prompt": "Reply with PROBE-OK"}}]},
            {"role": "user", "content": [{"type": "tool_result", "is_error": True, "content": "not found"}]}]}
        self.assertEqual(d11_class("/v1/messages", parent), "main")

    def test_coverage_requires_each_class_positively(self):
        def row(run, kind, model="claude-probe-fixture"):
            return {"run": run, "class": kind, "model": model, "status": 200, "delta": ()}
        rows = [row("delegation", "main"), row("delegation", "subagent"), row("context", "count-tokens"),
                row("rename", "title"), row(D11_HAIKU_RUN, "title", "claude-haiku-probe-fixture")]
        runs = [{"returncode": 0, "timed_out": False, "daemon_unchanged": True}]
        self.assertEqual(d11_errors(runs, rows), [])
        for index, kind in enumerate(D11_REQUIRED):
            with self.subTest(kind=kind):
                self.assertTrue(d11_errors(runs, rows[:index] + rows[index + 1:]))
        self.assertTrue(d11_errors(runs, [*rows, {**rows[0], "delta": (D11_UNRESOLVED,)}]))
        self.assertTrue(d11_errors(runs, [*rows, {**rows[0], "delta": ("model",)}]))
        self.assertTrue(d11_errors(runs, [*rows, {**rows[0], "status": 500}]))
        self.assertTrue(d11_errors([{**runs[0], "returncode": 1}], rows))
        self.assertTrue(d11_errors([], rows))
        # Rename helper evidence must come from the /rename run itself, and
        # Haiku evidence from a helper request on a Haiku selector.
        self.assertFalse(d11_coverage([{**rows[3], "run": "interactive"}])["rename-helper"])
        self.assertFalse(d11_coverage([{**rows[4], "model": "claude-probe-fixture"}])["haiku-helper"])
        self.assertFalse(d11_coverage([{**rows[4], "class": "main"}])["haiku-helper"])
        self.assertFalse(d11_coverage([{**rows[4], "run": "interactive"}])["haiku-helper"])


@uses_shipped_catalog
class OAuthRecognitionTests(unittest.TestCase):
    """The exact candidate client's request classes through
    the candidate gateway on the Claude OAuth pool path (a file OAuth
    credential, the shipped Claude-pool render, the gateway's own HTTPS
    client to api.anthropic.com reaching a fixture TLS endpoint through the
    reviewed ``proxy-url`` render seam), compared with the previous pin
    through the same gateway: each class's exact body delta and its recognition
    disposition. Requests are serialized so every upstream request is
    attributed to exactly one client request. Hint headers stay OFF."""

    @classmethod
    def setUpClass(cls):
        gate = real_binary_gate(SHIPPED_ROOT / "catalog/native-contract.json", what="the OAuth recognition probe")
        if gate.boundary:
            raise unittest.SkipTest(gate.boundary)
        if shutil.which("openssl", path="/usr/bin:/bin") is None:
            raise unittest.SkipTest("BOUNDARY: openssl is needed for the fixture TLS upstream")
        cls.trusted, cls.version = gate.trusted, gate.version
        previous = gate.trusted.resolved_path.parent / D11_PREVIOUS[0]
        cls.previous = None
        if previous != gate.trusted.resolved_path and previous.is_file() \
                and probe._sha256_file(previous) == D11_PREVIOUS[1]:
            cls.previous = probe.TrustedExecutable(previous, D11_PREVIOUS[1])
        bundle = catalog.load_catalog(SHIPPED_ROOT)
        lines = {key: dict(entry) for key, entry in bundle.lines.items()}
        family = compiler.family_default_env(lines, dict(bundle.providers))
        cls.selector = family["ANTHROPIC_DEFAULT_OPUS_MODEL"]

    def _fixture(self, root, name, environ, *, agent):
        fixture = probe.build_fixture(root / name, environ=environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        if agent:
            directory = state.ensure_private_dir(fixture.project_dir / ".claude/agents")
            state.atomic_write(directory / f"{D11_CHILD_AGENT}.md", D11_CHILD_DEFINITION.encode())
        return fixture

    def _capture(self, trusted, root, *, control):
        environ = {"HOME": str(root / "ambient/home"), "PATH": "/usr/bin:/bin"}
        gateway = start_oauth_gateway(root)
        runs, rows, control_rows = [], [], []
        try:
            identity = gateway_identity(gateway)
            with _D11Relay(gateway) as relay:
                common = dict(trusted=trusted, provider=relay, allow_real=True, environ=environ, timeout=150)

                def record(name, result):
                    runs.append({"row": name, "returncode": result.returncode, "timed_out": result.timed_out,
                                 "daemon_unchanged": result.daemon is not None and result.daemon.unchanged})

                fixture = self._fixture(root, "client", environ, agent=True)
                session_id = str(uuid.uuid4())
                # /context produces native count_tokens; bare /rename on the
                # resumed conversation invokes the client's name helper.
                for name, prompt in (("delegation", "recognition offline turn"), ("context", "/context"), ("rename", "/rename")):
                    relay.run_name = name
                    gateway.upstream.force_agent = D11_CHILD_AGENT if name == "delegation" else None
                    record(name, probe.run_native(
                        ("-p", prompt, "--model", self.selector, "--output-format", "json",
                         "--permission-mode", "default",
                         "--session-id" if name == "delegation" else "--resume", session_id),
                        fixture=fixture, **common))
                # The interactive client's own background helpers (session
                # title and any start-up probe). In the managed shape (no
                # Haiku family variable) they ride the lead selector; the
                # Haiku run sets the Haiku family selector to the gateway's
                # newest listed Haiku wire, so the client's Haiku helper path
                # itself is exercised and recorded apart from title/rename.
                gateway.upstream.force_agent = None
                haiku = _gateway_haiku(gateway)
                for name, child_env in (("interactive", None),
                                        (D11_HAIKU_RUN, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku})):
                    relay.run_name = name
                    record(name, probe.run_native_pty(
                        ("--model", self.selector, "--permission-mode", "default"), _pty_turn(relay),
                        fixture=self._fixture(root, name, environ, agent=False), child_env=child_env, **common))
                if control:
                    # Negative control: a forced Agent naming a missing agent.
                    relay.run_name = "missing-agent"
                    gateway.upstream.force_agent = D11_MISSING_AGENT
                    before = len(relay.d11_rows)
                    record("missing-agent", probe.run_native(
                        ("-p", "recognition offline turn", "--model", self.selector, "--output-format", "json",
                         "--permission-mode", "default", "--session-id", str(uuid.uuid4())),
                        fixture=self._fixture(root, "control", environ, agent=True), **common))
                    control_rows = relay.d11_rows[before:]
                rows = [row for row in relay.d11_rows if row["run"] != "missing-agent"]
                relay_errors = relay.completion_errors()
                # Negative control: an unrecognised caller on the same pool is
                # cloaked, so the delta detector discriminates.
                before = len(gateway.upstream.hits)
                body = {"model": self.selector.removesuffix("[1m]"), "max_tokens": 64,
                        "system": [{"type": "text", "text": "probe control"}],
                        "messages": [{"role": "user", "content": "probe control"}]}
                raw = json.dumps(body).encode()
                reply = harness.unix_request(gateway.socket, "POST", "/v1/messages", headers={
                    "authorization": "Bearer " + harness.FIXTURE_GATEWAY_TOKEN, "content-type": "application/json",
                    "anthropic-version": "2023-06-01", "user-agent": "probe-d11-control/1",
                    "host": f"127.0.0.1:{gateway.port}"}, body=raw, timeout=45)
                cloak = d11_row("control-unrecognised", "/v1/messages", raw, body, reply.status,
                                gateway.upstream.hits[before:])
            attributed = sum(row["upstream_hits"] for row in relay.d11_rows) + cloak["upstream_hits"]
            accounting = {"upstream_hits": len(gateway.upstream.hits), "attributed": attributed,
                          "forced": list(gateway.upstream.forced), "relay_errors": relay_errors}
            connects = sorted(set(gateway.upstream.connects))
        finally:
            gateway.close()
        return {"runs": runs, "rows": rows, "control_rows": control_rows, "cloak": cloak,
                "accounting": accounting, "connects": connects, "gateway": identity, "haiku_selector": haiku}

    def test_exact_candidate_request_classes_confirmed(self):
        observed = {}
        for label, trusted in (("candidate", self.trusted), ("previous", self.previous)):
            if trusted is None:
                continue
            root = Path(tempfile.mkdtemp(prefix="probe-d11-"))
            self.addCleanup(shutil.rmtree, root, True)
            observed[label] = self._capture(trusted, root, control=label == "candidate")
        candidate = observed["candidate"]
        previous = {}
        for row in observed.get("previous", {}).get("rows", ()):
            previous.setdefault(row["class"], []).append(row["delta"])
        dispositions = {}
        for row in candidate["rows"]:
            dispositions.setdefault(row["class"], set()).add(
                (d11_disposition(row["class"], row["delta"], previous.get(row["class"])), row["delta"],
                 row["model"]))
        errors = d11_errors(candidate["runs"], candidate["rows"], previous)
        control_coverage = d11_coverage(candidate["control_rows"])
        evidence = {label: {"runs": value["runs"], "connects": value["connects"], "gateway": value["gateway"],
                            "haiku_selector": value["haiku_selector"],
                            "accounting": value["accounting"], "coverage": d11_coverage(value["rows"]),
                            "classes": sorted({(row["run"], row["class"], row["status"], row["delta"], row["model"])
                                               for row in value["rows"]}, key=str),
                            "errors": d11_errors(value["runs"], value["rows"])}
                    for label, value in observed.items()}
        evidence["candidate"]["missing_agent_control"] = {
            "coverage": control_coverage, "classes": sorted({(row["class"], row["delta"])
                                                             for row in candidate["control_rows"]}, key=str)}
        print("OAuth recognition: " + json.dumps({"client": self.version, "sha256": self.trusted.sha256[:12],
                                        "dispositions": {k: sorted(map(list, v)) for k, v in dispositions.items()},
                                        "evidence": evidence}, sort_keys=True, default=list))
        self.assertTrue(_cloaked(candidate["cloak"]["delta"]), candidate["cloak"])
        self.assertEqual(candidate["connects"], ["api.anthropic.com:443"])
        accounting = candidate["accounting"]
        self.assertEqual(accounting["relay_errors"], [], accounting)
        self.assertEqual(accounting["upstream_hits"], accounting["attributed"], "unattributed upstream requests")
        self.assertEqual(accounting["forced"], [D11_CHILD_AGENT, D11_MISSING_AGENT], accounting)
        # The missing-agent control dispatched nothing, so it must not yield
        # subagent evidence (the parent's continuation stays main).
        self.assertTrue(candidate["control_rows"])
        self.assertFalse(control_coverage["subagent"], candidate["control_rows"])
        self.assertTrue(control_coverage["main"], candidate["control_rows"])
        self.assertEqual(errors, [], "; ".join(errors))


def _gateway_haiku(gateway, limit=20.0):
    """The newest Claude Haiku wire the candidate gateway lists on the
    OAuth pool (by ``created``), never a test-pinned id."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        reply = gateway.get("/v1/models")
        if reply.status == 200:
            rows = [row for row in reply.json().get("data", ()) if "haiku" in str(row.get("id"))]
            if rows:
                return max(rows, key=lambda row: (row.get("created") or 0, row["id"]))["id"]
        time.sleep(0.25)
    raise AssertionError("recognition: the candidate gateway lists no Claude Haiku wire on the OAuth pool")


def _pty_turn(relay):
    """One interactive turn; /exit only once background requests settled."""
    return [probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
            probe.PTYInteraction(_PTY_PROMPT, b"recognition interactive turn\r"),
            probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
            probe.PTYInteraction(_PTY_PROMPT, b"/exit\r", before_send=_settle(relay))]


def _settle(relay, quiet=1.5, limit=20.0):
    """A PTY before_send barrier: wait until the client has sent no request
    for ``quiet`` seconds, so background helpers land in this run."""

    def wait():
        deadline = time.monotonic() + limit
        seen, since = len(relay.d11_rows), time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.1)
            count = len(relay.d11_rows)
            if count != seen:
                seen, since = count, time.monotonic()
            elif time.monotonic() - since >= quiet:
                return
    return wait
