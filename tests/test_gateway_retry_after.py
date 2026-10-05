"""Bounded pinned-client Retry-After observations, host/full only.

W1–W6 use keyed OpenAI-compat (non-fast). U is a credential-scoped Claude
unified-limit-shaped refusal. F is W1 on the Claude fast error path: an
explicitly synthetic speed override and a local, non-forwarding CONNECT/TLS
fake, NOT an assertion that the native client chose fast mode. Direct-fake
controls are run once per case and shared by all series comparisons.

Run2 and run2b are optional paired diagnostic inputs, never shipped packages.
Set GWTEST_RETRY_CANDIDATE and GWTEST_RETRY_CONTROL to their bin/cli-proxy-api
paths and GWTEST_RETRY_EVIDENCE to the desired private JSON artifact. Missing
one of the pair is an error. Without either, full discovery observes only the
current series plus direct controls. No diagnostic build runs during tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from claude_multi import probe, render, state
from _catalog import SHIPPED_ROOT, uses_shipped_catalog
from _tier import real_binary_gate
import _gateway_harness as harness

CANDIDATE_ENV = "GWTEST_RETRY_CANDIDATE"
CONTROL_ENV = "GWTEST_RETRY_CONTROL"
EVIDENCE_ENV = "GWTEST_RETRY_EVIDENCE"
PINNED_MODULES = harness.PINNED_GO_MODULES
PATCHES = tuple("cli-proxy-api-" + name + ".patch" for name in (
    "credential-save-report", "credentialed-redirects", "openai-compat-keyed-safety", "retry-after"))
LEAD, CHILD, AGENT = "gwtest-retry-lead", "gwtest-retry-child", "gwtest-retry-agent"
HOOKS = ("Notification", "StopFailure", "SubagentStop")
PROMPT = "offline probe"  # Must not itself match the W6 retry-marker detector.
RETRY_KEYS = ("attempt", "max_retries", "retry_delay_ms", "error_status", "status_code")


@dataclass(frozen=True)
class Case:
    name: str
    seconds: int
    timeout: int
    route: str = "compat"
    watchdog: bool = True
    agent: bool = False
    pty: bool = False
    unified: bool = False


CASES = (
    Case("W1", 3600, 20), Case("W2", 3600, 20, watchdog=False),
    Case("W3", 5, 30), Case("W4", 30000, 20),
    Case("W5", 3600, 30, agent=True), Case("W6", 3600, 30, pty=True),
    Case("U", 3600, 20, route="claude", unified=True),
    Case("F", 3600, 20, route="claude-fast"),
)


def retry_events(stdout):
    events = []
    for line in stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("type") == "system" and row.get("subtype") == "api_retry":
            events.append({key: row[key] for key in RETRY_KEYS if type(row.get(key)) is int})
    return events


def visible_retry_marker(stdout):
    screen = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", stdout)
    return bool(re.search(r"retry|rate.?limit|minutes", screen, re.I))


def safe_headers(headers):
    """Only protocol timing/limit metadata, never arbitrary upstream headers."""
    return {key.lower(): value for key, value in headers.items()
            if key.lower() in ("retry-after", "retry-after-ms", "x-should-retry")
            or key.lower().startswith("anthropic-ratelimit-unified-")}


def reply_bytes(status, payload, headers=()):
    if isinstance(payload, probe.SseResponse):
        raw = b"".join(harness.sse_frame(kind, data) for kind, data in payload.events)
        content_type = "text/event-stream"
    else:
        raw, content_type = json.dumps(payload).encode(), "application/json"
    return harness.Reply(status, {"content-type": content_type, **dict(headers)}, raw)


def send_reply(handler, reply):
    handler.send_response(reply.status)
    # Transfer framing is reconstructed; every end-to-end header survives.
    for name, value in reply.headers.items():
        if name.lower() not in ("content-length", "connection", "transfer-encoding", "server", "date"):
            handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(reply.raw)))
    handler.end_headers()
    handler.wfile.write(reply.raw)


def request_roles(body):
    tools = {tool.get("name", tool.get("function", {}).get("name")) for tool in body.get("tools", [])}
    child = body.get("model") == CHILD
    return "Agent" in tools and not child, child


class Scenario:
    """First target request fails, all later targets succeed; state per run."""

    def __init__(self, case):
        self.case = case
        self.rows = []
        self.rejected = self.spawned = False
        self.lock = threading.Lock()

    def reply(self, path, body):
        with self.lock:
            if urlsplit(path).path.endswith("/count_tokens"):
                return reply_bytes(200, {"input_tokens": 1})
            lead, child = request_roles(body)
            target = child if self.case.agent else lead
            fail = target and not self.rejected
            spawn = self.case.agent and lead and not self.spawned
            if fail:
                self.rejected = True
                headers = {"retry-after": str(self.case.seconds)}
                if self.case.unified:
                    reset = str(int(time.time()) + self.case.seconds)
                    headers.update({"anthropic-ratelimit-unified-status": "rejected",
                        "anthropic-ratelimit-unified-5h-status": "rejected",
                        "anthropic-ratelimit-unified-5h-reset": reset,
                        "anthropic-ratelimit-unified-reset": reset,
                        "anthropic-ratelimit-unified-representative-claim": "five_hour"})
                payload = {"type": "error", "error": {"type": "rate_limit_error",
                    "message": "5-hour limit exceeded." if self.case.unified else "fixture rate limit"}}
                reply = reply_bytes(429, payload, headers.items())
            else:
                if spawn:
                    self.spawned = True
                reply = self.success(body, path, spawn)
            self.rows.append({"status": reply.status, "headers": safe_headers(reply.headers),
                "target": target, "lead": lead, "child": child, "spawn": spawn,
                "after_rejection": self.rejected and not fail, "at": time.monotonic()})
            return reply

    @staticmethod
    def success(body, path, spawn):
        arguments = {"description": "offline retry observation", "subagent_type": AGENT,
                     "prompt": "Reply PROBE-OK", "run_in_background": False}
        if urlsplit(path).path.endswith("/chat/completions"):
            content = {"role": "assistant", "content": "PROBE-OK"}
            if spawn:
                content = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_gwtest_retry", "type": "function", "function": {
                        "name": "Agent", "arguments": json.dumps(arguments)}}]}
            base = {"id": "chatcmpl_gwtest_retry", "created": 1, "model": body.get("model")}
            finish = "tool_calls" if spawn else "stop"
            if body.get("stream"):
                if spawn:
                    content["tool_calls"][0]["index"] = 0
                chunks = [{**base, "object": "chat.completion.chunk", "choices": [
                    {"index": 0, "delta": content, "finish_reason": None}]},
                    {**base, "object": "chat.completion.chunk", "choices": [
                    {"index": 0, "delta": {}, "finish_reason": finish}]}]
                raw = b"".join(("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks) + b"data: [DONE]\n\n"
                return harness.Reply(200, {"content-type": "text/event-stream"}, raw)
            return reply_bytes(200, {**base, "object": "chat.completion", "choices": [
                {"index": 0, "message": content, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        content = ([{"type": "tool_use", "id": "toolu_gwtest_retry", "name": "Agent", "input": arguments}]
                   if spawn else [{"type": "text", "text": "PROBE-OK"}])
        payload = probe._message_payload(content, model=body.get("model"),
            stop_reason="tool_use" if spawn else "end_turn", message_id="msg_gwtest_retry")
        return reply_bytes(200, probe._sse_message(payload) if body.get("stream") else payload)


class Upstream:
    """Unix fake, optionally a TLS endpoint behind a NON-forwarding CONNECT.

    CONNECT accepts one exact fixture hostname, then serves canned replies on
    the same socket. It never resolves names, opens a TCP socket or proxies.
    """

    def __init__(self, path, scenario, cert=None, key=None):
        self.scenario, self.errors, self.connects = scenario, [], 0
        context = None
        if cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_CONNECT(self):
                if self.path != "api.anthropic.com:443" or context is None:
                    owner.errors.append("unexpected CONNECT target")
                    self.send_error(403)
                    return
                owner.connects += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                with context.wrap_socket(self.connection, server_side=True) as connection:
                    Handler(connection, self.client_address, self.server)
                self.close_connection = True

            def do_POST(self):
                if not urlsplit(self.path).path.endswith(("/v1/messages", "/chat/completions", "/count_tokens")):
                    owner.errors.append("unexpected fake path")
                    self.send_error(404)
                    return
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                send_reply(self, scenario.reply(self.path, json.loads(raw)))

        self.server = probe.UnixHTTPServer(str(path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Relay(probe.FakeAnthropicProvider):
    """Probe-approved endpoint with raw HTTP replies, not the hint capture's headerless relay.

    Only its unix listener uses this handler; native clients reach it through
    probe's loopback-only namespace bridge. Captures remain scalar metadata.
    """

    def __init__(self, scenario, gateway=None):
        super().__init__()
        self.scenario, self.gateway = scenario, gateway
        self.rows, self.errors = [], []
        self.refusal = threading.Event()
        self.lock = threading.Lock()

    def unix_socket(self, directory):
        path = super().unix_socket(directory)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw)
                owner._record("POST", self.path, self.headers, raw, body)
                with owner.lock:
                    at = time.monotonic()
                    before = len(owner.scenario.rows)
                    already_rejected = owner.scenario.rejected
                    lead, child = request_roles(body)
                    target = child if owner.scenario.case.agent else lead
                    if owner.gateway:
                        headers = {key.lower(): value for key, value in self.headers.items()}
                        headers.update(host=f"127.0.0.1:{owner.gateway.port}",
                                       authorization="Bearer " + harness.FIXTURE_GATEWAY_TOKEN)
                        headers.pop("x-api-key", None)
                        reply = harness.unix_request(owner.gateway.socket, "POST", self.path,
                            headers=headers, body=raw, timeout=10)
                    else:
                        reply = owner.scenario.reply(self.path, body)
                    hits = owner.scenario.rows[before:]
                    owner.rows.append({"status": reply.status, "headers": safe_headers(reply.headers),
                        "request_class": self.headers.get("x-claude-code-request-class"),
                        "target": target, "lead": lead, "child": child,
                        "after_rejection": already_rejected, "upstream_hits": len(hits),
                        "at": at, "body_sha256": hashlib.sha256(reply.raw).hexdigest() if reply.status != 200 else None})
                    send_reply(self, reply)
                    if reply.status == 429:
                        owner.refusal.set()

        self._unix_servers[path][0].RequestHandlerClass = Handler
        return path


def document_factory(case, tls_paths):
    def build(bundle, egress, port, home):
        document, _, info = harness.build_document(bundle, egress, port, home)
        document.update({"claude-api-key": [], "codex-api-key": [], "oauth-model-alias": {},
                         "openai-compatibility": [], "payload": {}, "disable-cooling": not case.unified})
        models = [{"name": name, "alias": name} for name in (LEAD, CHILD)]
        base = f"http://127.0.0.1:{egress}/gwtest-retry"
        if case.route == "compat":
            document["openai-compatibility"] = [{"name": "gwtest-retry", "base-url": base,
                "api-key-entries": [{"api-key": "dummy-gwtest-retry"}], "models": models}]
        else:
            section = {"api-key": "dummy-gwtest-retry", "base-url": base, "models": models}
            if case.route == "claude-fast":
                # Speed is intentionally synthetic, not a claim about native /fast.
                section.update({"base-url": "https://api.anthropic.com",
                                "proxy-url": f"http://127.0.0.1:{egress}"})
                document["payload"] = {"override": [{"models": [{"name": "*", "protocol": "claude"}],
                                                     "params": {"speed": "fast"}}]}
                cert, key = home / ".config/claude-multi/fake-ca.pem", home / ".config/claude-multi/fake-key.pem"
                subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=api.anthropic.com", "-addext", "subjectAltName=DNS:api.anthropic.com",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=20)
                key.chmod(0o600)
                tls_paths.update(cert=cert, key=key)
            document["claude-api-key"] = [section]
        return render.finalize_document(document), {}, info
    return build


def start_gateway(case, scenario, binary):
    tls_paths = {}
    original = probe.start_isolated_process

    def isolated(argv, **kwargs):
        if tls_paths:
            # Only this disposable gateway trusts the freshly generated test CA.
            kwargs["env"] = {**kwargs["env"], "SSL_CERT_FILE": str(tls_paths["cert"])}
        return original(argv, **kwargs)

    # The ordinary harness caches one binary per runner. A multi-series
    # diagnostic must not silently reuse run1 for the two candidate runs.
    harness.find_gateway_binary.cache_clear()
    try:
        with mock.patch.dict(os.environ, {harness.BINARY_ENV: binary}), \
                mock.patch.object(probe, "start_isolated_process", side_effect=isolated):
            gateway = harness.GatewayHarness(document_factory=document_factory(case, tls_paths),
                upstream_factory=lambda path: Upstream(path, scenario, **tls_paths))
        if gateway.binary != str(Path(binary).resolve()):
            gateway.close()
            raise AssertionError("Retry-After launched the wrong gateway series")
        return gateway
    finally:
        harness.find_gateway_binary.cache_clear()


def make_hooks(fixture):
    log, recorder = fixture.root / "hook-events.jsonl", fixture.root / "retry-hook.py"
    source = f'''#!{sys.executable}
import json, os, sys
p = json.load(sys.stdin)
e = p.get('hook_event_name')
if e in {HOOKS!r}:
    fd = os.open({str(log)!r}, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'w') as out: out.write(json.dumps(e) + '\\n')
print('{{}}')
'''
    state.atomic_write(recorder, source.encode())
    recorder.chmod(0o700)
    return {event: [{"hooks": [{"type": "command", "command": shlex.quote(str(recorder)), "timeout": 5}]}]
            for event in HOOKS}, log


def run_pty_window(args, steps, common, seconds):
    """Drain PTY continuously for W6's window, then kill only this fixture.

    run_native_pty raises on deadline (unlike headless run_native). Capture
    its one process-group handle in a test-local Popen wrapper and terminate
    it after 30s from the client-visible 429, before the outer safety timeout.
    No shared daemon, /proc search, sleep-blocked PTY reader or transcript.
    """
    original = subprocess.Popen
    done = threading.Event()
    threads = []
    observation = {"stopped": False, "alive": False, "window_started": False}

    def launch(*argv, **kwargs):
        process = original(*argv, **kwargs)
        if kwargs.get("process_group") == 0:
            if threads:
                raise AssertionError("more than one W6 native PTY process")

            def stop_after_window():
                deadline = time.monotonic() + 10
                while not common["provider"].refusal.wait(0.05):
                    if done.is_set() or time.monotonic() >= deadline:
                        return
                observation["window_started"] = True
                if not done.wait(seconds):
                    observation["alive"] = process.poll() is None
                    observation["stopped"] = True
                    probe._kill_process_group(process)

            thread = threading.Thread(target=stop_after_window, daemon=True)
            threads.append(thread)
            thread.start()
        return process

    try:
        with mock.patch.object(subprocess, "Popen", side_effect=launch):
            result = probe.run_native_pty(args, steps, **{**common, "timeout": seconds + 15})
        return result, observation
    finally:
        done.set()
        for thread in threads:
            thread.join(timeout=1)


def run_case(case, trusted, binary=None):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="gwtest-retry-client-") as temp:
        root = Path(temp)
        environ = {"HOME": str(root / "ambient"), "PATH": "/usr/bin:/bin"}
        fixture = probe.build_fixture(root / "client", environ=environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        agents = state.ensure_private_dir(fixture.project_dir / ".claude/agents")
        state.atomic_write(agents / (AGENT + ".md"), (
            f"---\nname: {AGENT}\ndescription: offline retry probe\nmodel: {CHILD}\n---\nReply PROBE-OK.\n").encode())
        hooks, log = make_hooks(fixture)
        settings = probe.write_fixture_settings(fixture, "scope/settings.json", {
            "env": {"CLAUDE_CODE_RETRY_WATCHDOG": "1" if case.watchdog else "0",
                    "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"},
            "hooks": hooks, "availableModels": [LEAD, CHILD]})
        scenario = Scenario(case)
        gateway = start_gateway(case, scenario, binary) if binary else None
        try:
            with Relay(scenario, gateway) as relay:
                args = ("--model", LEAD, "--settings", str(settings))
                common = dict(trusted=trusted, fixture=fixture, provider=relay, allow_real=True,
                              environ=environ, timeout=case.timeout)
                pty_window = None
                if case.pty:
                    steps = [probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
                             probe.PTYInteraction("❯".encode(), (PROMPT + "\r").encode())]
                    result, pty_window = run_pty_window(args, steps, common, case.timeout)
                else:
                    result = probe.run_native(("-p", PROMPT, "--dangerously-skip-permissions",
                        "--output-format", "stream-json", "--verbose", *args), **common)
            events = retry_events(result.stdout)
            incoming = relay.rows
            upstream = scenario.rows
            target_times = [row["at"] for row in incoming if row["target"]]
            observed = {
                "case": case.name, "route": case.route, "watchdog": case.watchdog,
                "gateway_binary": gateway.binary if gateway else None,
                "retry_after_seconds": case.seconds, "window_seconds": case.timeout,
                "seconds": round(time.monotonic() - started, 3),
                "returncode": result.returncode, "timed_out": result.timed_out,
                "alive_at_deadline": pty_window["alive"] if pty_window else result.timed_out,
                "pty_window": pty_window,
                "daemon_unchanged": result.daemon is not None and result.daemon.unchanged,
                "retry_events": events, "retry_delay_ms": [event["retry_delay_ms"] for event in events if "retry_delay_ms" in event],
                "client_request_count": len(incoming), "upstream_request_count": len(upstream),
                "target_request_count": len(target_times),
                "target_interval_ms": round(1000 * (target_times[1] - target_times[0])) if len(target_times) > 1 else None,
                "lead_requests_after_rejection": sum(row["lead"] and row["after_rejection"] for row in incoming),
                "spawned": scenario.spawned, "rejected": scenario.rejected,
                "hook_events": [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else [],
                "visible_retry_marker": visible_retry_marker(result.stdout) if case.pty else None,
                "responses": [{key: value for key, value in row.items() if key != "at"} for row in incoming],
                "fake_responses": [{key: value for key, value in row.items() if key != "at"} for row in upstream],
                "tls_fake_connects": gateway.upstream.connects if gateway else 0,
                "errors": list(relay.errors) + (gateway.upstream.errors if gateway else []),
            }
            return observed
        finally:
            if gateway:
                gateway.close()


def comparison(left, right):
    """left minus right, with raw operands for non-numeric protocol changes."""
    a, b = left["retry_delay_ms"], right["retry_delay_ms"]
    return {"retry_delay_ms": a[0] - b[0] if a and b else None,
            "retry_delay_ms_left": a, "retry_delay_ms_right": b,
            "client_request_count": left["client_request_count"] - right["client_request_count"],
            "upstream_request_count": left["upstream_request_count"] - right["upstream_request_count"],
            "error_body_sha256_left": [row["body_sha256"] for row in left["responses"] if row["status"] >= 400],
            "error_body_sha256_right": [row["body_sha256"] for row in right["responses"] if row["status"] >= 400],
            "status_headers_left": [{"status": row["status"], "headers": row["headers"]} for row in left["responses"]],
            "status_headers_right": [{"status": row["status"], "headers": row["headers"]} for row in right["responses"]]}


def completion_errors(rows):
    errors = []
    for row in rows:
        name = row["case"]
        if not row["rejected"] or not any(reply["status"] == 429 for reply in row["responses"]):
            errors.append(name + ": no target refusal observed by the client")
        if not row["daemon_unchanged"] or row["errors"]:
            errors.append(name + ": isolation/fake invariant failed")
        if name == "W5" and not row["spawned"]:
            errors.append(name + ": subagent dispatch missing")
        if name == "W3" and (row["timed_out"] or row["returncode"] != 0 or row["target_request_count"] < 2):
            errors.append(name + ": bounded positive recovery control did not complete")
        pty = row.get("pty_window")
        if name == "W6" and (not pty or not all(pty.values())):
            errors.append(name + ": interactive observation window did not complete")
        if not row["timed_out"] and row["returncode"] not in ((-9,) if pty else (0, 1)):
            errors.append(name + ": unexpected native exit")
    return errors


def diagnostic_identity(binary, count):
    path = Path(binary).resolve()
    if not str(path).startswith("/nix/store/") or not path.is_file():
        raise AssertionError("Retry-After diagnostic must be a /nix/store binary")
    share = path.parent.parent / "share/gwtest"
    patches = json.loads((share / "extra-patches.json").read_text())
    if [patch["name"] for patch in patches] != list(PATCHES[:count]):
        raise AssertionError("Retry-After diagnostic patch order differs")
    modules = (share / "go-modules").read_text().strip()
    # The share file echoes an attribute; check what the derivation consumed.
    consumed = harness.consumed_vendor(path.parent.parent)
    if modules != PINNED_MODULES or not harness.vendor_pinned(consumed, PINNED_MODULES):
        raise AssertionError("Retry-After diagnostic must reuse the pinned modules")
    return {"binary": str(path), "patches": patches, "go_modules": modules, "go_modules_consumed": consumed,
            "baseline": (share / "baseline").read_text().strip()}


class RetryAfterSelfTests(unittest.TestCase):
    def test_case_inventory_and_windows(self):
        self.assertEqual([(case.name, case.timeout) for case in CASES],
                         [("W1", 20), ("W2", 20), ("W3", 30), ("W4", 20), ("W5", 30), ("W6", 30), ("U", 20), ("F", 20)])
        self.assertFalse(CASES[1].watchdog)
        self.assertEqual(CASES[3].seconds, 30000)

    def test_metadata_parser_never_keeps_error_text_or_non_integer_fields(self):
        row = {"type": "system", "subtype": "api_retry", "retry_delay_ms": 5000,
               "attempt": True, "error_status": 429, "error": "must not retain"}
        self.assertEqual(retry_events("bad json\n" + json.dumps(row)),
                         [{"retry_delay_ms": 5000, "error_status": 429}])
        self.assertEqual(safe_headers({"Authorization": "fixture", "Retry-After": "5"}), {"retry-after": "5"})

    def test_scenario_rejects_only_first_target_and_forces_child(self):
        lead = {"model": LEAD, "tools": [{"name": "Agent"}]}
        scenario = Scenario(CASES[0])
        self.assertEqual(scenario.reply("/v1/messages", {"model": LEAD}).status, 200)
        self.assertEqual(scenario.reply("/v1/messages", lead).status, 429)
        self.assertEqual(scenario.reply("/v1/messages", lead).status, 200)
        scenario = Scenario(CASES[4])
        payload = scenario.reply("/v1/messages", lead).json()
        self.assertEqual(payload["stop_reason"], "tool_use")
        self.assertIs(payload["content"][0]["input"]["run_in_background"], False)
        self.assertEqual(scenario.reply("/v1/messages", {"model": CHILD}).status, 429)
        self.assertTrue(scenario.spawned)

    def test_unified_headers_have_credential_window(self):
        reply = Scenario(CASES[6]).reply("/v1/messages", {"model": LEAD, "tools": [{"name": "Agent"}]})
        self.assertEqual(reply.headers["anthropic-ratelimit-unified-5h-status"], "rejected")
        self.assertGreater(int(reply.headers["anthropic-ratelimit-unified-reset"]), time.time())

    def test_missing_or_broken_observations_fail_closed(self):
        row = {"case": "W3", "rejected": True, "responses": [{"status": 429}], "errors": [],
               "daemon_unchanged": True, "timed_out": False, "returncode": 0, "target_request_count": 2}
        self.assertEqual(completion_errors([row]), [])
        for change in ({"rejected": False}, {"responses": []}, {"daemon_unchanged": False},
                       {"timed_out": True}, {"returncode": 1}, {"target_request_count": 1}):
            self.assertTrue(completion_errors([{**row, **change}]))

    def test_pty_prompt_cannot_be_mistaken_for_retry_feedback(self):
        self.assertFalse(visible_retry_marker(PROMPT))
        self.assertTrue(visible_retry_marker("Rate limit; retry in 60 minutes"))
        self.assertTrue(visible_retry_marker("\x1b[31mRetrying\x1b[0m"))

    def test_gateway_binary_cache_cannot_collapse_three_runs_to_one(self):
        paths = ["/nix/store/gwtest-run1/bin/cli-proxy-api", "/nix/store/gwtest-run2/bin/cli-proxy-api"]
        actual = []

        def factory(**_kwargs):
            binary, _reason = harness.find_gateway_binary()
            actual.append(binary)
            return SimpleNamespace(binary=binary)

        harness.find_gateway_binary.cache_clear()
        self.addCleanup(harness.find_gateway_binary.cache_clear)
        with mock.patch.object(harness, "_gateway_binary", side_effect=lambda: (os.environ[harness.BINARY_ENV], "7.3.15")), \
                mock.patch.object(harness, "GatewayHarness", side_effect=factory):
            for binary in paths:
                self.assertEqual(start_gateway(CASES[0], Scenario(CASES[0]), binary).binary, binary)
        self.assertEqual(actual, paths)

    def test_relay_preserves_retry_headers_status_and_original_request(self):
        scenario = Scenario(CASES[0])
        gateway = SimpleNamespace(socket=Path("/unused"), port=55122)
        raw = b'{ "model": "gwtest-retry-lead", "tools": [] }'
        headers = {"Content-Type": "application/json", "Anthropic-Dangerous-Direct-Browser-Access": "true",
                   "Accept-Encoding": "gzip, deflate, br, zstd"}
        reply = harness.Reply(429, {"retry-after": "3600", "content-type": "application/json",
                                   "anthropic-ratelimit-unified-status": "rejected"}, b'{"error":{}}')
        with tempfile.TemporaryDirectory() as root, Relay(scenario, gateway) as relay:
            path = relay.unix_socket(root)
            # Relay uses the same HTTP helper as this request, so retain the
            # real function for the client side of this isolated unix test.
            request = harness.unix_request
            with mock.patch.object(harness, "unix_request", return_value=reply) as forwarded:
                got = request(path, "POST", "/v1/messages?beta=true", headers=headers, body=raw)
            self.assertEqual(got.status, 429)
            self.assertEqual(got.raw, reply.raw)
            self.assertEqual(safe_headers(got.headers), safe_headers(reply.headers))
            self.assertEqual(forwarded.call_args.kwargs["body"], raw)
            for name, value in headers.items():
                self.assertEqual(forwarded.call_args.kwargs["headers"][name.lower()], value)
            self.assertEqual(relay.rows[0]["status"], 429)
            self.assertEqual(relay.rows[0]["headers"]["retry-after"], "3600")

    def test_offline_derivation_preserves_vendor_fod_and_never_ships(self):
        text = (Path(__file__).with_name("gateway-retry-after.nix")).read_text()
        self.assertIn("goModules = cliProxyApi.goModules;", text)
        # Vendor pinning requires an evaluation assert on the referenced vendor
        # derivation, and diagnostic_identity's consumed-input check.
        self.assertIn("assert import ./vendor-inputs.nix diagnostic == [ cliProxyApi.goModules.drvPath ];", text)
        self.assertIn("patches = (old.patches or [ ]) ++ extraPatches;", text)
        self.assertNotIn("fetch", text)

    def test_run2b_delta_is_separate_from_baseline_shift(self):
        row = {"retry_delay_ms": [5], "client_request_count": 1, "upstream_request_count": 1, "responses": []}
        self.assertEqual(comparison({**row, "retry_delay_ms": [3600000]}, row)["retry_delay_ms"], 3599995)
        self.assertIsNone(comparison({**row, "retry_delay_ms": []}, row)["retry_delay_ms"])


@uses_shipped_catalog
class RealClientRetryAfterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate = real_binary_gate(SHIPPED_ROOT / "catalog/native-contract.json", what="the Retry-After observations")
        if gate.boundary:
            raise unittest.SkipTest(gate.boundary)
        cls.trusted, cls.version = gate.trusted, gate.version
        cls.binary, reason = harness.find_gateway_binary()
        if cls.binary is None:
            harness.boundary("BOUNDARY: " + reason)
        if not shutil.which("openssl"):
            harness.boundary("BOUNDARY: Retry-After fast control needs openssl for a disposable TLS fake")

    def test_complete_path_with_direct_controls(self):
        started = time.monotonic()
        candidate, control = os.environ.get(CANDIDATE_ENV), os.environ.get(CONTROL_ENV)
        self.assertEqual(bool(candidate), bool(control), "supply both run2 and run2b diagnostics")
        series = [("run1", self.binary)]
        builds = {"run1": {"binary": self.binary}}
        if candidate:
            series += [("run2", candidate), ("run2b", control)]
            builds.update(run2=diagnostic_identity(candidate, 4), run2b=diagnostic_identity(control, 3))
            self.assertEqual(builds["run2"]["patches"][:3], builds["run2b"]["patches"],
                             "run2 minus run2b must isolate patch 11, not different prerequisite bytes")
            for label in ("run2", "run2b"):
                self.assertEqual(builds[label]["baseline"], str(Path(self.binary).parent.parent),
                                 "diagnostics must extend exactly the run1 gateway")
        evidence = {"schema": "gwtest-retry-after-v1", "client_version": self.version,
            "client_sha256": self.trusted.sha256, "builds": builds, "cases": {}, "complete": False,
            "candidate_scope": "diagnostic only; disposition belongs to lead (default defer)",
            "fast_control": "synthetic speed override; official-name TLS terminated by local non-forwarding fake",
            "direct_controls": "one per case, reused unchanged across series comparisons",
            "three_series_supplied": bool(candidate),
            "retry_event_capture": "native stream-json stdout tail (probe.run_native, 4000 chars); no transcript reads",
            "request_counts": "only fake protocol requests; never inferred from retry events",
            "isolation": "client and gateway in bwrap --unshare-net; unix fake bridges; no provider calls"}
        errors = []
        try:
            for case in CASES:
                direct = run_case(case, self.trusted)
                row = {"direct": direct, "runs": {}, "direct_deltas": {}}
                evidence["cases"][case.name] = row
                errors.extend(completion_errors([direct]))
                for label, binary in series:
                    observed = run_case(case, self.trusted, binary)
                    row["runs"][label] = observed
                    row["direct_deltas"][label] = comparison(observed, direct)
                    errors.extend(label + ": " + error for error in completion_errors([observed]))
                    if case.name == "F" and not observed["tls_fake_connects"]:
                        errors.append(label + ": fast TLS fake was not reached")
                    print(f"Retry-After {case.name} {label}: {observed['seconds']}s; "
                          f"delays={observed['retry_delay_ms']}; requests={observed['client_request_count']}", flush=True)
                if candidate:
                    row["patch11_delta"] = comparison(row["runs"]["run2"], row["runs"]["run2b"])
                    row["baseline_shift"] = comparison(row["runs"]["run2b"], row["runs"]["run1"])
            evidence["complete"] = not errors
        except Exception as exc:
            evidence["aborted"] = type(exc).__name__  # No exception text or PTY output in evidence.
            raise
        finally:
            evidence.update(seconds=round(time.monotonic() - started, 3), errors=errors)
            target = os.environ.get(EVIDENCE_ENV)
            if target:
                target = Path(target)
                state.ensure_private_dir(target.parent)
                state.atomic_write(target, (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode())
        self.assertEqual(set(evidence["cases"]), {case.name for case in CASES})
        self.assertEqual(errors, [], "; ".join(errors))
        print(f"Retry-After case budget: {evidence['seconds']}s (outside ordinary harness)", flush=True)
