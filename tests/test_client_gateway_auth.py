"""Client checks S11 and S12 against the pinned Claude Code.

S11 — helper-only gateway auth: with no
credential in the child environment, does the ``apiKeyHelper`` token rotate
hitlessly after a gateway 401 (same process, no relaunch), and what does an
env token do when the helper is also configured?

S12 — ``[1m]`` GPT-6 selectors: what window the pinned client assumes for a
``gpt-multi-<key>-<effort>[1m]`` selector and what it puts on the wire, and
what the currently packaged CLIProxyAPI forwards for that alias to a
loopback fake Codex upstream.

Real-binary runs go only through the probe harness (trusted path+sha256
from ``catalog/native-contract.json``, fixture HOME/CLAUDE_CONFIG_DIR,
loopback fake provider, live-daemon-domain tripwire). The gateway half runs
the installed ``cli-proxy-api`` (realpath under ``/nix/store``; its version
is read offline and named, not required to equal the catalog baseline)
against a rendered config whose only upstream is a loopback fake. Both
halves are hermetic: the client runs in the harness's loopback-only network
namespace with a fixture ``CLAUDE_CODE_TMPDIR``; the gateway runs in its own
loopback-only namespace (``probe.start_isolated_process``) with an egress
bridge to the fake upstream's unix socket and an ingress bridge the test
talks through, killed as a process group in ``finally`` and by bwrap's
parent-death signal. Without isolation the module skips with a BOUNDARY
message. The tests pass a SYNTHETIC ambient ``environ`` (fake HOME under
the scratch root, fixed PATH), which disables the harness credential scan
and live-roots check against the real environment (the
``test_scope_probe.py`` precedent); the child never inherits it. Evidence
is metadata only: auth labels, route
classes, model ids, key names, sizes, booleans. Request bodies are inspected
in memory only; no prompt, response, or transcript text is persisted or
printed. The module skips off the operator host with a BOUNDARY message.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from claude_multi import catalog, composition, probe, render, state, strict_json
from claude_multi.probe import ProbeError

from _tier import real_binary_gate
from _layout import RESOURCES_ROOT  # the one root source


_COMPONENT_ROOT = RESOURCES_ROOT
_CONTRACT_PATH = _COMPONENT_ROOT / "catalog" / "native-contract.json"
_LIVE_TOUCHED = "live daemon domain touched"
_MESSAGES = "/v1/messages"
_COUNT_TOKENS = "/v1/messages/count_tokens"
# Fixed non-secret test tokens (labels only ever leave the fake provider).
_S11_TOKENS = {"tok-a": "A", "tok-b": "B", "tok-c": "C"}
_LONG_CONTEXT_BETA = "context-1m-2025-08-07"
_REPLAY_HEADERS = ("anthropic-beta", "anthropic-version", "content-type", "user-agent", "x-app")
_CONTEXT_KEYS = frozenset(
    {"max_output_tokens", "max_tokens", "truncation", "context_window", "context_management"}
)
_LIVE_GATEWAY_PORT = 8317


def _route(path: str) -> str:
    route = urllib.parse.urlsplit(path).path.rstrip("/")
    return route if route in (_MESSAGES, _COUNT_TOKENS) else "other"


def _plain_reply(document: dict, message_id: str) -> tuple[int, object]:
    payload = probe._message_payload(
        [{"type": "text", "text": "PROBE-OK"}],
        model=document.get("model"),
        stop_reason="end_turn",
        message_id=message_id,
    )
    return 200, probe._sse_message(payload) if document.get("stream") else payload


class _AuthRecordingProvider(probe.FakeAnthropicProvider):
    """Fake provider that also labels each auth header separately.

    The base record labels ``x-api-key`` first and falls back to the bearer;
    S11 needs both (the pinned client can send an env bearer and a helper
    ``x-api-key`` at once). Only labels, the route class, the auth-policy
    status and an arrival time are kept.
    """

    def __init__(self, *, known_tokens: dict[str, str], **kwargs):
        super().__init__(known_tokens=known_tokens, **kwargs)
        self._labels = dict(known_tokens)
        self._observed: list[dict] = []
        self._observed_lock = threading.Lock()

    def _label(self, value: str) -> str:
        if not value:
            return "absent"
        return self._labels.get(value, "other")

    @property
    def observed(self) -> list[dict]:
        with self._observed_lock:
            return [dict(entry) for entry in self._observed]

    def _record(self, method, path, headers, body, document):
        bearer = headers.get("Authorization") or ""
        bearer = bearer[7:] if bearer.startswith("Bearer ") else bearer
        entry = {
            "route": _route(path),
            "bearer": self._label(bearer),
            "x_api_key": self._label(headers.get("x-api-key") or ""),
            "at": time.monotonic(),
        }
        context = super()._record(method, path, headers, body, document)
        entry["rejected"] = context.record.auth_rejected_status
        with self._observed_lock:
            self._observed.append(entry)
        return context


class _S11Responder:
    """Answers the first main turn with one read-only tool call.

    Writing the flip file while answering that turn makes the helper return
    the second token from then on, so the tool-result follow-up is the
    "next request in the same process" after the rotation.
    """

    def __init__(self, flip: Path):
        self._flip = flip
        self._main = 0
        self._lock = threading.Lock()

    def respond(self, document, path, context):
        route = _route(path)
        if route == _COUNT_TOKENS:
            return 200, {"input_tokens": 1}
        tools = [
            tool.get("name")
            for tool in document.get("tools") or []
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        ]
        with self._lock:
            if tools:
                self._main += 1
            ordinal = self._main
        if not tools or ordinal != 1:
            return _plain_reply(document, f"msg_check_s11_{ordinal:04d}")
        self._flip.write_text("flip\n", encoding="utf-8")
        name = "Glob" if "Glob" in tools else tools[0]
        payload = probe._message_payload(
            [
                {
                    "type": "tool_use",
                    "id": "toolu_check_s11_0001",
                    "name": name,
                    "input": {"pattern": "*.client-check-none"},
                }
            ],
            model=document.get("model"),
            stop_reason="tool_use",
            message_id="msg_check_s11_tool",
        )
        return 200, probe._sse_message(payload) if document.get("stream") else payload


class _SpikeCase(unittest.TestCase):
    """Pinned-binary boundary checks exactly like RealPinnedBinaryTests."""

    trusted: probe.TrustedExecutable
    contract: dict
    boundary_skip: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="client check probes")
        cls.boundary_skip = gate.boundary
        if gate.trusted is None or gate.contract is None:
            return
        cls.contract = gate.contract
        cls.trusted = gate.trusted

    def setUp(self) -> None:
        super().setUp()
        if self.boundary_skip is not None:
            self.skipTest(self.boundary_skip)
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-client-check-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        # Simulated live roots: the fixture stays disjoint and the ambient
        # credential scan sees no provider credentials.
        self.environ = {"HOME": str(self.root / "live" / "home"), "PATH": "/usr/bin:/bin"}

    def _fixture(self, name: str) -> probe.ProbeFixture:
        fixture = probe.build_fixture(self.root / name, environ=self.environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        return fixture

    def _pin(self) -> str:
        version = (self.contract.get("verified") or [{}])[0].get("version")
        return f"claude={version} sha256={self.trusted.sha256[:12]}"

    def _run(self, probe_id: str, args, *, fixture, provider, timeout=150, **kwargs):
        try:
            return probe.run_native(
                tuple(args),
                trusted=self.trusted,
                fixture=fixture,
                provider=provider,
                timeout=timeout,
                allow_real=True,
                environ=self.environ,
                **kwargs,
            )
        except ProbeError as exc:
            if _LIVE_TOUCHED in str(exc):
                print(
                    f"client check {probe_id}: INCONCLUSIVE live daemon domain "
                    "touched (tripwire failed closed; no verdict)"
                )
                self.skipTest(f"BOUNDARY: INCONCLUSIVE {probe_id}: {exc}")
            raise


class S11HelperOnlyAuthTests(_SpikeCase):
    """S11: helper-only auth rotates hitlessly; an env token outranks it.

    Static anchors in the pinned binary (2.1.280, informational): ``oW``
    builds ``Authorization: Bearer`` from ``ANTHROPIC_AUTH_TOKEN`` or, only
    when that is absent, the awaited helper (``e0e``); the SDK adds
    ``X-Api-Key`` from the cached helper value; the retry predicate clears
    the helper cache on every 401 (``if(e.status===401)return Vze(),!0``);
    a helper-sourced 401 (``ORe``: firstParty, no env token, source
    apiKeyHelper) retries after a fixed ``afr=1000`` ms, other 401s use the
    exponential backoff (500 ms base).

    Test-double artefact: in the env+helper case the single 401 comes from
    the fake labelling ``x-api-key`` before ``Authorization`` (``probe``
    ``_auth_label``), so the stale helper key rejected by the policy is what
    triggers it. The real gateway checks ``Authorization`` first and would
    accept the env bearer, so that 401 is not a claim about the gateway.
    """

    def _helper_fixture(self, name: str) -> tuple[probe.ProbeFixture, Path, Path]:
        fixture = self._fixture(name)
        bin_dir = state.ensure_private_dir(fixture.root / "helper-bin")
        flip = fixture.root / "helper-flip"
        runs = fixture.root / "helper-runs"
        helper = probe.write_fixture_executable(
            fixture,
            bin_dir,
            "check-api-key-helper",
            "#!/bin/sh\n"
            "present=false; matches=false\n"
            "[ \"${ANTHROPIC_BASE_URL+x}\" = x ] && present=true\n"
            f"IFS= read -r expected < {shlex.quote(str(fixture.root / 'helper-base-url'))}\n"
            "[ \"${ANTHROPIC_BASE_URL-}\" = \"$expected\" ] && matches=true\n"
            "printf '{\"present\":%s,\"matches\":%s}\\n' \"$present\" \"$matches\" "
            f">> {shlex.quote(str(runs))}\n"
            f"if [ -e {shlex.quote(str(flip))} ]; then echo tok-b; "
            "else echo tok-a; fi\n",
        )
        state.atomic_write(
            fixture.claude_config_dir / "settings.json",
            strict_json.canonical_file_bytes({"apiKeyHelper": str(helper)}),
        )
        return fixture, flip, runs

    def _case(self, name: str, *, token: str | None, ttl_ms: str | None) -> dict:
        fixture, flip, runs = self._helper_fixture(name)

        def policy(context: probe.RequestContext) -> int | None:
            # After the flip the gateway no longer accepts tok-a.
            return 401 if flip.exists() and context.auth == "A" else None

        provider = _AuthRecordingProvider(
            known_tokens=_S11_TOKENS,
            responder=_S11Responder(flip),
            auth_policy=policy,
        )
        child_env = (
            {"CLAUDE_CODE_API_KEY_HELPER_TTL_MS": ttl_ms} if ttl_ms is not None else None
        )
        with provider:
            # The harness puts this loopback-only value in the client's
            # ANTHROPIC_BASE_URL. The helper records booleans, never its env.
            state.atomic_write(fixture.root / "helper-base-url", (provider.base_url + "\n").encode())
            result = self._run(
                "S11",
                ("-p", "client check s11 turn", "--output-format", "json"),
                fixture=fixture,
                provider=provider,
                token=token,
                child_env=child_env,
            )
        self.assertFalse(result.timed_out, "S11 run timed out")
        self.assertEqual(result.returncode, 0, f"S11 run exit={result.returncode}")
        document = json.loads(result.stdout)
        messages = [entry for entry in provider.observed if entry["route"] == _MESSAGES]
        rejected = [index for index, entry in enumerate(messages) if entry["rejected"]]
        delay = None
        after = None
        if rejected and rejected[0] + 1 < len(messages):
            after = messages[rejected[0] + 1]
            delay = after["at"] - messages[rejected[0]]["at"]
        helper_env = ([json.loads(line) for line in runs.read_text(encoding="utf-8").splitlines()]
                      if runs.exists() else [])
        helper_runs = len(helper_env)
        return {
            "is_error": document.get("is_error"),
            "subtype": document.get("subtype"),
            "messages": messages,
            "rejected": rejected,
            "after": after,
            "delay": delay,
            "helper_runs": helper_runs,
            "helper_env": helper_env,
        }

    @staticmethod
    def _delay_class(delay: float | None) -> str:
        if delay is None:
            return "none"
        if 0.95 <= delay < 2.5:
            return "fixed-1s"
        if 0.45 <= delay < 0.95:
            return "backoff-0.5s"
        return f"{delay:.1f}s"

    def test_s11_helper_only_auth_rotates_without_relaunch(self) -> None:
        helper = self._case("s11-helper", token=None, ttl_ms=None)
        # Helper-only: every request authenticates with the helper token, on
        # both Authorization (bearer) and X-Api-Key.
        messages = helper["messages"]
        self.assertGreaterEqual(len(messages), 3, [m["x_api_key"] for m in messages])
        for entry in messages:
            self.assertEqual(entry["bearer"], entry["x_api_key"], entry)
        self.assertEqual((messages[0]["bearer"], messages[0]["rejected"]), ("A", None))
        # Exactly one 401 (tok-a after the flip), then the SAME process
        # re-runs the helper and succeeds with tok-b: no relaunch.
        self.assertEqual(len(helper["rejected"]), 1, [m["rejected"] for m in messages])
        self.assertEqual(messages[helper["rejected"][0]]["bearer"], "A")
        self.assertIsNotNone(helper["after"])
        self.assertEqual(
            (helper["after"]["bearer"], helper["after"]["rejected"]), ("B", None)
        )
        self.assertTrue(
            all(m["bearer"] == "B" for m in messages[helper["rejected"][0] + 1 :])
        )
        self.assertIs(helper["is_error"], False)
        self.assertEqual(helper["subtype"], "success")
        # Helper-sourced 401: fixed 1000 ms retry (afr), not the backoff.
        self.assertGreaterEqual(helper["delay"], 0.95)
        self.assertLess(helper["delay"], 30.0)
        self.assertGreaterEqual(helper["helper_runs"], 2)

        # TTL 0: the helper is re-run more often; rotation is unchanged.
        ttl0 = self._case("s11-ttl0", token=None, ttl_ms="0")
        self.assertEqual(len(ttl0["rejected"]), 1)
        self.assertEqual(ttl0["after"]["bearer"], "B")
        self.assertIs(ttl0["is_error"], False)
        self.assertGreaterEqual(ttl0["helper_runs"], helper["helper_runs"])

        # Env token AND helper: the env token owns Authorization on every
        # request; the helper key may ride along as X-Api-Key from the
        # startup prefetch, is dropped after a 401 and never re-fetched.
        env = self._case("s11-env", token="tok-c", ttl_ms=None)
        env_messages = env["messages"]
        self.assertTrue(env_messages)
        self.assertTrue(all(m["bearer"] == "C" for m in env_messages), env_messages)
        self.assertNotIn("B", {m["x_api_key"] for m in env_messages})
        self.assertLessEqual(env["helper_runs"], 1)
        self.assertIs(env["is_error"], False)
        if env["rejected"]:
            self.assertIsNotNone(env["after"])
            self.assertEqual(env["after"]["x_api_key"], "absent")
        rode_along = sorted({m["x_api_key"] for m in env_messages})
        print(
            "client check S11: PASS helper-only: auth A, one 401 after rotation, "
            f"same-process retry with B ({self._delay_class(helper['delay'])} "
            f"delay={helper['delay']:.2f}s), requests={len(messages)} "
            f"helper_runs={helper['helper_runs']} (TTL_MS=0 -> "
            f"{ttl0['helper_runs']}); env+helper: Authorization=C on all "
            f"{len(env_messages)} requests, helper x-api-key={rode_along}, "
            f"401s={len(env['rejected'])} retry={self._delay_class(env['delay'])}, "
            f"helper_runs={env['helper_runs']} -> env outranks, helper never "
            f"re-consulted ({self._pin()})"
        )
        observations = [entry for case in (helper, ttl0, env) for entry in case["helper_env"]]
        self.assertTrue(observations, "the helper environment was not observed")
        present = all(entry["present"] is True for entry in observations)
        matches = all(entry["matches"] is True for entry in observations)
        observed = "compiled" if present and matches else "absent" if not present else "different"
        print(f"client check S11-env: {'PASS' if present and matches else 'FAIL'} "
              f"helper_base_url={observed} observations={len(observations)} ({self._pin()})")
        self.assertTrue(present, "ANTHROPIC_BASE_URL absent from the helper environment")
        self.assertTrue(matches, "the helper did not receive the compiled loopback base URL")


class _WireCaptureProvider(probe.FakeAnthropicProvider):
    """Records the anthropic-beta ids per request and keeps the first main
    (tool-bearing) request body plus a few protocol headers IN MEMORY ONLY,
    so the gateway half can replay exactly what the pinned client sends.
    Nothing captured here is printed or persisted."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.betas: list[tuple[str, ...]] = []
        # The pinned client posts to /v1/messages?beta=true, which the
        # harness records as a hashed path; keep the route class here.
        self.routes: list[str] = []
        self.first_main: tuple[bytes, dict[str, str]] | None = None
        self._capture_lock = threading.Lock()

    def _record(self, method, path, headers, body, document):
        context = super()._record(method, path, headers, body, document)
        betas = tuple(
            sorted(
                item.strip()
                for item in (headers.get("anthropic-beta") or "").split(",")
                if item.strip()
            )
        )
        with self._capture_lock:
            self.betas.append(betas)
            self.routes.append(_route(path))
            if (
                self.first_main is None
                and _route(path) == _MESSAGES
                and isinstance(document, dict)
                and document.get("tools")
            ):
                kept = {
                    name: headers.get(name)
                    for name in _REPLAY_HEADERS
                    if headers.get(name) is not None
                }
                self.first_main = (bytes(body), kept)
        return context


def _s12_responder(document: dict, path: str) -> tuple[int, object]:
    if _route(path) == _COUNT_TOKENS:
        return 200, {"input_tokens": 1}
    return _plain_reply(document, "msg_check_s12")


_TOKEN_LABEL_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([km]?)$")


def _token_label(label: str) -> int:
    match = _TOKEN_LABEL_RE.match(label.strip().lower())
    if match is None:
        raise AssertionError(f"unparseable token label {label!r}")
    scale = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2)]
    return int(round(float(match.group(1)) * scale))


def _context_numbers(text: str) -> dict:
    """Window and autocompact-buffer numbers from `/context` (UI numbers only)."""

    window = re.search(r"\*\*Tokens:\*\*\s*\S+\s*/\s*([0-9.]+[km]?)", text)
    buffer = re.search(r"\|\s*Autocompact buffer\s*\|\s*([0-9.]+[km]?)\s*\|", text)
    return {
        "window": _token_label(window.group(1)) if window else None,
        "buffer": _token_label(buffer.group(1)) if buffer else None,
    }


def _gpt6_line(lines: dict) -> tuple[str, str, int, str | None] | None:
    """(1M selector, wire, provider_tokens, proxy contract) of the first
    GPT-6 codex line.

    Scans the v2 lines (``Catalog.lines``) through
    ``catalog.line_selectors``, default effort first, for the first ``[1m]``
    selector. New lines are skipped, as the deleted v1 view omitted them, so
    a None is a real boundary (no GPT-6 ``[1m]`` line), never a shape
    mismatch.
    """

    for key in sorted(lines):
        line = lines[key]
        wire = line.get("wire_model")
        if line.get("provider") != "openai" or not str(wire).startswith("gpt-6"):
            continue
        if line.get("status", "active") == "new":
            continue
        triples = catalog.line_selectors(line)
        default = line.get("default_effort")
        ordered = [t for t in triples if t[0] == default] + [t for t in triples if t[0] != default]
        for _effort, selector, contract in ordered:
            if isinstance(selector, str) and selector.endswith("[1m]"):
                return selector, wire, line["context"]["provider_tokens"], contract
    return None


# Ports inside the gateway's private network namespace (never the host's
# loopback, so they cannot collide with, or reach, the live gateway).
_NETNS_UPSTREAM_PORT = 18401
_NETNS_GATEWAY_PORT = 18402
assert _LIVE_GATEWAY_PORT not in (_NETNS_UPSTREAM_PORT, _NETNS_GATEWAY_PORT)


from _gateway import _gateway_binary


class _CodexUpstream(BaseHTTPRequestHandler):
    """Loopback fake Codex `/responses` upstream: metadata-only capture."""

    protocol_version = "HTTP/1.1"
    hits: list[dict] = []

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length)
        try:
            document = json.loads(body)
        except ValueError:
            document = {}
        if not isinstance(document, dict):
            document = {}
        reasoning = document.get("reasoning")
        type(self).hits.append(
            {
                "path": urllib.parse.urlsplit(self.path).path,
                "header_names": sorted({name.lower() for name in self.headers.keys()}),
                "keys": sorted(document),
                "model": document.get("model"),
                "effort": reasoning.get("effort") if isinstance(reasoning, dict) else None,
                "stream": document.get("stream"),
                "body_bytes": len(body),
            }
        )
        model = document.get("model") if isinstance(document.get("model"), str) else "x"
        item = {
            "type": "message",
            "id": "msg_check_up",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "FAKE_OK", "annotations": []}],
        }
        response = {
            "id": "resp_check_up",
            "object": "response",
            "model": model,
            "status": "completed",
            "output": [item],
            "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        }
        events = [
            {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        data = b"".join(
            b"event: " + event["type"].encode() + b"\ndata: " + json.dumps(event).encode() + b"\n\n"
            for event in events
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class S12GptContextTests(_SpikeCase):
    """S12: what a ``[1m]`` GPT-6 selector gets (client window vs forwarded).

    Static anchors in the pinned binary (2.1.280, informational): ``su(e)``
    = ``/\\[1m\\]/i`` -> window ``1e6`` (``Ig``); the ``long_context`` beta
    (``context-1m-2025-08-07``) is added when ``su(model)``; the request
    model is the selector with ``[1m]`` stripped; an unknown 1M model gets
    auto-compact source ``auto`` (no proactive buffer) unless
    ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` is set. Gateway (CLIProxyAPI source,
    tag v7.2.81 clone): the Claude->Codex translator forwards no
    max_tokens/truncation/context field and the executor rewrites
    ``model`` to the resolved wire. The gateway half feeds the aliases
    through a loopback ``codex-api-key`` entry (same alias table, payload
    rules, translator and executor as the OAuth pool; the OAuth credential
    path itself cannot run offline).
    """

    def _line(self) -> dict:
        bundle = catalog.load_catalog(_COMPONENT_ROOT)
        found = _gpt6_line(bundle.lines)
        if found is None:
            self.skipTest("BOUNDARY: no GPT-6 [1m] codex line in the catalog; S12 skipped")
        selector, wire, provider_tokens, proxy_contract = found
        base = selector[: -len("[1m]")]
        providers = bundle.docs["providers"]["providers"]
        adapter = providers["openai"]["adapter"]
        contract = render.ADAPTER_PAYLOAD_CONTRACTS[adapter][proxy_contract]
        return {
            "bundle": bundle,
            "selector": selector,
            "base": base,
            "wire": wire,
            "provider_tokens": provider_tokens,
            "effort": contract["params"].get("reasoning.effort"),
        }

    def _client_run(self, name: str, model: str, *, prompt: str, compaction=None):
        fixture = self._fixture(name)
        provider = _WireCaptureProvider(responder=_s12_responder)
        with provider:
            result = self._run(
                "S12",
                ("-p", prompt, "--model", model, "--output-format",
                 "json" if prompt != "/context" else "text"),
                fixture=fixture,
                provider=provider,
                timeout=120,
                compaction=compaction,
            )
        self.assertFalse(result.timed_out, "S12 client run timed out")
        self.assertEqual(result.returncode, 0, f"S12 client exit={result.returncode}")
        return result, provider

    def _wire(self, result, provider) -> dict:
        main = [
            (record, betas)
            for record, betas, route in zip(
                provider.requests, provider.betas, provider.routes
            )
            if route == _MESSAGES and record.tool_count > 0
        ]
        self.assertTrue(main, "no main-loop request reached the fake provider")
        usage = json.loads(result.stdout).get("modelUsage") or {}
        return {
            "models": sorted({record.model for record, _betas in main}),
            "max_tokens": sorted({record.max_tokens for record, _betas in main}),
            "effort": sorted({str(record.effort) for record, _betas in main}),
            "long_context": [_LONG_CONTEXT_BETA in betas for _record, betas in main],
            "usage": {key: value.get("contextWindow") for key, value in usage.items()},
            "first_main": provider.first_main,
        }

    def _gateway_half(self, binary: str, line: dict, captured) -> dict:
        body, headers = captured
        gw_root = state.ensure_private_dir(self.root / "gateway")
        sockets = state.ensure_private_dir(gw_root / "run")
        upstream_handler = type("_Upstream", (_CodexUpstream,), {"hits": []})
        # The fake upstream listens only on a private unix socket; inside the
        # gateway's own loopback-only namespace 127.0.0.1:<upstream_port> is
        # bridged to it. Ports are namespace-local (never the host's 8317).
        upstream_socket = sockets / "upstream.sock"
        upstream = probe.UnixHTTPServer(str(upstream_socket), upstream_handler)
        threading.Thread(
            target=upstream.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        upstream_port = _NETNS_UPSTREAM_PORT
        gateway_port = _NETNS_GATEWAY_PORT
        gateway_socket = sockets / "gateway.sock"
        gateway = copy.deepcopy(line["bundle"].docs["gateway"])
        gateway["gateway"]["base_url"] = f"http://127.0.0.1:{gateway_port}"
        token = "client-check-gateway-token-0001"
        # The renderer reads the v2 line map; no continuity aliases
        # (the spike proves the live GPT-6 lane, not the retired set).
        document, _available, _unavailable, _info = render.build_config_document(
            gateway,
            line["bundle"].docs["providers"]["providers"],
            line["bundle"].docs["models-v2"]["models"],
            home=gw_root,
            gateway_token=token,
            resolve_secret=lambda _name: None,  # no direct provider renders
            continuity={},
        )
        # Loopback only: no direct/LAN upstream may remain in the config.
        document["claude-api-key"] = []
        document["openai-compatibility"] = []
        aliases = document["oauth-model-alias"].get("codex", [])
        self.assertTrue(
            any(
                item.get("name") == line["wire"] and item.get("alias") == line["base"]
                for item in aliases
            ),
            "rendered codex alias table lacks the GPT-6 lane",
        )
        document["codex-api-key"] = [
            {
                "api-key": "client-check-dummy-codex-key",
                "base-url": f"http://127.0.0.1:{upstream_port}/codex",
                "models": [
                    {
                        "name": item["name"],
                        "alias": item["alias"],
                        "force-mapping": bool(item.get("force-mapping", False)),
                    }
                    for item in aliases
                ],
            }
        ]
        config_path = gw_root / "config.yaml"
        state.atomic_write(config_path, render.emit_yaml(document).encode("utf-8"))

        def call(path: str, data: bytes | None = None, extra: dict | None = None):
            request_headers = {"Authorization": f"Bearer {token}", **(extra or {})}
            connection = probe.UnixHTTPConnection(gateway_socket, timeout=30)
            try:
                connection.putrequest(
                    "POST" if data is not None else "GET",
                    path,
                    skip_host=True,
                    skip_accept_encoding=True,
                )
                connection.putheader("Host", f"127.0.0.1:{gateway_port}")
                for name, value in request_headers.items():
                    connection.putheader(name, value)
                if data is not None:
                    connection.putheader("Content-Length", str(len(data)))
                connection.endheaders(data)
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        # Own loopback-only namespace: egress 127.0.0.1:<upstream> -> the
        # fake upstream socket; ingress gateway.sock -> 127.0.0.1:<gateway>.
        process = probe.start_isolated_process(
            [binary, "--config", str(config_path), "--local-model"],
            cwd=gw_root,
            env={"HOME": str(gw_root), "PATH": "/usr/bin:/bin"},
            bridges=(
                probe.PortBridge(upstream_port, upstream_socket, "egress"),
                probe.PortBridge(gateway_port, gateway_socket, "ingress"),
            ),
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    if call("/healthz")[0] == 200:
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline or process.poll() is not None:
                    self.fail("disposable gateway did not become ready")
                time.sleep(0.1)

            # Neutral UA: for Claude-CLI user agents the gateway disguises
            # ids that do not start with claude- (EVIDENCE G8).
            status, payload = call(
                "/v1/models", extra={"User-Agent": "client-check-probe"}
            )
            self.assertEqual(status, 200)
            advertised = [
                sorted(entry)
                for entry in json.loads(payload).get("data", [])
                if entry.get("id") == line["base"]
            ]
            replay_status, _reply = call(_MESSAGES, body, headers)
            forwarded = list(upstream_handler.hits)
            bare = json.dumps(
                {
                    "model": line["selector"],
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "client check s12"}],
                }
            ).encode("utf-8")
            bare_status, _bare_reply = call(
                _MESSAGES,
                bare,
                {"anthropic-version": "2023-06-01", "content-type": "application/json",
                 "user-agent": headers.get("user-agent", "")},
            )
        finally:
            probe.stop_isolated_process(process)
        return {
            "advertised": advertised,
            "replay_status": replay_status,
            "forwarded": forwarded,
            "unstripped_status": bare_status,
            "unstripped_hits": len(upstream_handler.hits) - len(forwarded),
        }

    def test_s12_gpt6_1m_selector_client_and_gateway(self) -> None:
        line = self._line()
        # Client side, headless: wire, betas, the window the client books.
        result, provider = self._client_run("s12-1m", line["selector"], prompt="client check s12 turn")
        wide = self._wire(result, provider)
        result, provider = self._client_run("s12-bare", line["base"], prompt="client check s12 turn")
        bare = self._wire(result, provider)
        self.assertEqual(wide["models"], [line["base"]], "client must strip [1m]")
        self.assertTrue(all(wide["long_context"]), "context-1m beta missing on a 1M turn")
        self.assertEqual(wide["usage"], {line["selector"]: 1_000_000})
        self.assertEqual(bare["models"], [line["base"]])
        self.assertFalse(any(bare["long_context"]))
        self.assertEqual(bare["usage"], {line["base"]: 200_000})
        self.assertEqual(wide["max_tokens"], bare["max_tokens"])
        self.assertEqual(len(wide["max_tokens"]), 1)
        self.assertLessEqual(wide["max_tokens"][0], 128_000)

        # Auto-compact: bare client vs the production-shaped capacity env.
        result, _provider = self._client_run("s12-ctx", line["selector"], prompt="/context")
        unmanaged = _context_numbers(result.stdout)
        capacity = composition.operating_window(line["provider_tokens"])
        policy = probe.ProbeCompactionPolicy(capacity, composition.AUTO_COMPACT_PERCENT)
        result, _provider = self._client_run(
            "s12-ctx-managed", line["selector"], prompt="/context", compaction=policy
        )
        managed = _context_numbers(result.stdout)
        self.assertEqual(unmanaged, {"window": 1_000_000, "buffer": None})
        self.assertEqual(managed["window"], min(capacity, 1_000_000))
        self.assertIsNotNone(managed["buffer"])
        self.assertGreater(managed["buffer"], 0)

        binary, version = _gateway_binary()
        if binary is None:
            gateway_note = f"gateway half BOUNDARY ({version})"
        else:
            self.assertIsNotNone(wide["first_main"])
            gw = self._gateway_half(binary, line, wide["first_main"])
            self.assertEqual(gw["replay_status"], 200)
            self.assertEqual(len(gw["forwarded"]), 1, gw["forwarded"])
            hit = gw["forwarded"][0]
            self.assertEqual(hit["path"], "/codex/responses")
            self.assertEqual(hit["model"], line["wire"])
            self.assertEqual(hit["effort"], line["effort"])
            self.assertIs(hit["stream"], True)
            self.assertFalse(_CONTEXT_KEYS & set(hit["keys"]), hit["keys"])
            self.assertTrue(gw["advertised"])
            self.assertFalse(
                [key for keys in gw["advertised"] for key in keys
                 if "context" in key or "token" in key],
                gw["advertised"],
            )
            self.assertNotEqual(gw["unstripped_status"], 200)
            self.assertEqual(gw["unstripped_hits"], 0)
            baseline = line["bundle"].docs["gateway"]["gateway"]["cliproxyapi_baseline"]
            gateway_note = (
                f"gateway {version} at {Path(binary).parent.parent.name} (isolated "
                f"netns; catalog baseline {baseline}, not required equal): alias "
                f"{line['base']} forwarded as model={hit['model']} "
                f"effort={hit['effort']} keys={len(hit['keys'])} with no "
                "max_output_tokens/truncation/context field; /v1/models "
                f"advertises no window ({gw['advertised'][0]}); an unstripped "
                f"[1m] id is refused ({gw['unstripped_status']}, upstream hits=0)"
            )
        verdict = (
            "PASS (caveat: provider-side >272k acceptance is not verifiable offline)"
            if binary is not None
            else "INCONCLUSIVE (client half only)"
        )
        conclusion = "1M class kept" if binary is not None else "gateway forwarding unverified"
        print(
            f"client check S12: {verdict} client: {line['selector']} -> wire "
            f"{wide['models'][0]} + {_LONG_CONTEXT_BETA}, contextWindow=1000000 "
            f"(bare selector 200000, no beta), max_tokens={wide['max_tokens'][0]}, "
            f"auto-compact unmanaged window={unmanaged['window']} buffer=none "
            f"(source auto) vs managed CLAUDE_CODE_AUTO_COMPACT_WINDOW={capacity} "
            f"window={managed['window']} buffer={managed['buffer']}; "
            f"{gateway_note} -> {conclusion} ({self._pin()})"
        )
        if binary is None:
            with self.subTest("S12 gateway half"):
                self.skipTest(f"BOUNDARY: {version}; S12 gateway half skipped")
