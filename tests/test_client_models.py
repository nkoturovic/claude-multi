"""Client checks S7-S9 against the pinned Claude Code: the model probes.

Real-binary probes against the pinned Claude client (resolved from
``catalog/native-contract.json``: exact path plus full sha256, never a
hard-coded version). Every run goes through the probe harness with
``allow_real=True``: a fresh disposable fixture HOME/CLAUDE_CONFIG_DIR per
run, the loopback fake provider as the only endpoint, and the live
daemon-domain tripwire pointed at the real uid-shared domain. A tripped
tripwire is an explicit INCONCLUSIVE (boundary skip), never a pass. Every
real run is hermetic: a loopback-only network namespace (fake provider
bridged over a fixture unix socket) and a fixture-private
``CLAUDE_CODE_TMPDIR``; without isolation the classes skip with a BOUNDARY
message. The tests pass a SYNTHETIC ambient ``environ`` (fake HOME under the
scratch root, fixed PATH), which disables the harness credential scan and
live-roots check against the real environment (the ``test_scope_probe.py``
precedent); the child never inherits it.

- S7 which wire the client requests for bare ``opus``/``fable`` aliases,
  canonical ``claude-<wire>[1m]`` vs ``claude-multi-<wire>[1m]`` selectors
  (output cap, effort, thinking, prompt-bundle size class, recognition betas,
  model-not-found chain) and the refusal fallback (``stop_reason: refusal`` +
  ``stop_details.category``) with and without its target in the fence.
- S8 agent frontmatter ``effort`` values on the subagent's wire.
- S9 native helper wires (Explore, statusline-setup, claude-code-guide)
  under non-Opus leads and production-shaped fences.

Evidence is metadata only: model ids, integers, beta token names, hint
header safe ids, marker names and booleans. Request bodies are inspected in
memory by the test-local responder; no prompt, response, transcript or
stdio text is persisted or printed. Each probe prints exactly one line
``client check S<n>: <verdict> <detail>``; PASS/FAIL is the design verdict,
while the assertions pin the observed client behaviour (a client flip fails
the test).
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from claude_multi import catalog, compiler, probe, scope, state, strict_json
from claude_multi.probe import ProbeError

from _tier import real_binary_gate
from _layout import RESOURCES_ROOT  # the one root source


_CONTRACT_PATH = (
    RESOURCES_ROOT / "catalog" / "native-contract.json"
)
_TURN = "client check probe turn"
_RUN_TIMEOUT = 120.0
_TRIPWIRE = "live daemon domain touched"
_UNRECOGNIZED_MARKER = "[claude-code:unrecognized_model]"
_BETA_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
_BETA_CAP = 32
_HINT_ENV = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}
_EXPLORE_CAP_OFF = "CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP"

# The compiled family defaults (ANTHROPIC_DEFAULT_*_MODEL)
# derived from the shipped catalog, never a native-generation literal; the
# real-binary spikes read the shipped catalog by rule (AGENTS §4).
_SHIPPED = catalog.load_catalog(RESOURCES_ROOT)
_FAMILY = compiler.family_default_env(
    {key: dict(entry) for key, entry in _SHIPPED.lines.items()}, dict(_SHIPPED.providers)
)
_OPUS = _FAMILY["ANTHROPIC_DEFAULT_OPUS_MODEL"]
_FABLE = _FAMILY["ANTHROPIC_DEFAULT_FABLE_MODEL"]
_SONNET = _FAMILY["ANTHROPIC_DEFAULT_SONNET_MODEL"]
# The client's categorized-refusal route table (a client fact, U23): the
# targets are exactly catalog.REFUSAL_FALLBACK_WIRES (fenced fallback-only).
_OPUS48 = "claude-opus-4-8[1m]"
_OPUS5 = "claude-opus-5[1m]"
_SONNET5 = "claude-sonnet-5[1m]"
_MULTI_OPUS = "claude-multi-" + _OPUS.removeprefix("claude-")
_MULTI_FABLE = "claude-multi-" + _FABLE.removeprefix("claude-")
# A managed scope configured nowhere compiles this default mode;
# without it 2.1.284+ starts in auto mode, which (through a gateway) cannot
# classify Agent spawns and refuses them.
_MANAGED_PERMISSIONS = {"defaultMode": scope.PERMISSION_DEFAULT_MODE}
_LEAD_GPT = "gpt-multi-sol-high"
_OPUS_MARKER = "client-check-marker-opus"
_FABLE_MARKER = "client-check-marker-fable"
_SONNET_MARKER = "client-check-marker-sonnet"


def _retry_s9_pty(runner):
    """Retry just the S9 helper, once, for the known exit-timeout flake."""

    try:
        return runner(timeout=240), False
    except probe.PTYTimeoutError as exc:
        if exc.phase != "client-exit":
            raise
    return runner(timeout=240), True


class S9RetryTests(unittest.TestCase):
    def test_one_timeout_then_success_is_marked(self):
        from unittest.mock import Mock, call

        runner = Mock(side_effect=[
            probe.PTYTimeoutError("client-exit", 1, 1, 240), "ok"
        ])
        self.assertEqual(_retry_s9_pty(runner), ("ok", True))
        self.assertEqual(runner.call_args_list, [call(timeout=240), call(timeout=240)])

    def test_second_timeout_propagates(self):
        from unittest.mock import Mock, call

        first = probe.PTYTimeoutError("client-exit", 1, 1, 240)
        second = probe.PTYTimeoutError("client-exit", 1, 1, 241)
        runner = Mock(side_effect=[first, second])
        with self.assertRaises(probe.PTYTimeoutError) as caught:
            _retry_s9_pty(runner)
        self.assertIs(caught.exception, second)
        self.assertEqual(runner.call_args_list, [call(timeout=240), call(timeout=240)])

    def test_marker_timeout_propagates_without_retry(self):
        from unittest.mock import Mock

        error = probe.PTYTimeoutError("marker", 1, 2, 240)
        runner = Mock(side_effect=error)
        with self.assertRaises(probe.PTYTimeoutError) as caught:
            _retry_s9_pty(runner)
        self.assertIs(caught.exception, error)
        runner.assert_called_once_with(timeout=240)

    def test_plain_probe_errors_propagate_without_retry(self):
        from unittest.mock import Mock

        for message in (
            "some other probe error",
            "probe PTY timed out waiting for client exit",
        ):
            with self.subTest(message=message):
                error = ProbeError(message)
                runner = Mock(side_effect=error)
                with self.assertRaises(ProbeError) as caught:
                    _retry_s9_pty(runner)
                self.assertIs(caught.exception, error)
                runner.assert_called_once_with(timeout=240)


def _wire(selector: str) -> str:
    return selector[: -len("[1m]")] if selector.endswith("[1m]") else selector


def _has_beta(row: dict[str, Any], prefix: str) -> bool:
    return any(name.startswith(prefix) for name in row["betas"])


class _BetaRecordingProvider(probe.FakeAnthropicProvider):
    """Loopback fake that additionally keeps ``anthropic-beta`` token names.

    Only tokens matching a short safe-id shape are kept, keyed by the
    request ordinal; no other header value is read.
    """

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.beta_names: dict[int, tuple[str, ...]] = {}

    def _record(self, method, path, headers, body, document):  # type: ignore[override]
        context = super()._record(method, path, headers, body, document)
        raw = headers.get("anthropic-beta") or ""
        names = sorted(
            {token.strip() for token in raw.split(",") if _BETA_RE.fullmatch(token.strip())}
        )
        self.beta_names[context.ordinal] = tuple(names[:_BETA_CAP])
        return context


def _refusal_reply(model: Any, category: str | None, stream: bool,
                   server_fallback: str | None = None) -> Any:
    """Canned refusal: ``stop_reason: refusal`` plus optional category (and,
    for the refusal probe, an API-lane served-fallback record naming another model)."""

    payload = probe._message_payload(
        [], model=model, stop_reason="refusal", message_id="msg_check_refusal"
    )
    if server_fallback is not None:
        payload["usage"] = {**payload.get("usage", {}), "iterations": [
            {"type": "message", "model": model, "input_tokens": 1, "output_tokens": 1},
            {"type": "fallback_message", "model": server_fallback, "input_tokens": 1, "output_tokens": 1}]}
    details = (
        None
        if category is None
        else {"type": "refusal", "category": category, "explanation": None}
    )
    if details is not None:
        payload["stop_details"] = details
    if not stream:
        return payload
    events = []
    for name, data in probe._sse_message(payload).events:
        if name == "message_delta" and details is not None:
            data = {**data, "delta": {**data["delta"], "stop_details": details}}
        events.append((name, data))
    return probe.SseResponse(tuple(events))


class _SpikeResponder:
    """Context-aware scripted provider; keeps per-request metadata rows.

    ``agents`` forces one parallel Agent call per listed subagent type on the
    first main-thread request offering the Agent tool; ``refuse`` maps wire
    models to a refusal category (None = uncategorized); ``not_found`` wire
    models get a 404 ``not_found_error``. Everything else gets PROBE-OK.
    """

    def __init__(
        self,
        *,
        agents: tuple[str, ...] = (),
        refuse: dict[str, str | None] | None = None,
        not_found: tuple[str, ...] = (),
        server_fallback: str | None = None,
    ):
        self._agents = tuple(agents)
        self._refuse = dict(refuse or {})
        self._not_found = frozenset(not_found)
        # A refusal may also carry the API lane's served-fallback
        # record (usage.iterations[].type == "fallback_message").
        self._server_fallback = server_fallback
        # Requests that carried a ``fallbacks`` field (implicit server-side
        # fallback opt-in, which a managed session never sends): their ordinals.
        self.fallbacks_fields: list[int] = []
        self._forced = False
        self._lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []

    def respond(self, document: dict[str, Any], path: str, context: Any) -> Any:
        route = path.split("?", 1)[0].rstrip("/")
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return 404, {
                "type": "error",
                "error": {"type": "not_found_error", "message": "probe: unknown path"},
            }
        record = context.record
        hints = dict(record.hint_header_values)
        model = document.get("model")
        stream = bool(document.get("stream"))
        with self._lock:
            if "fallbacks" in document:
                self.fallbacks_fields.append(context.ordinal)
            self.rows.append(
                {
                    "ordinal": context.ordinal,
                    "model": record.model,
                    "max_tokens": record.max_tokens,
                    "effort": record.effort,
                    "thinking_type": record.thinking_type,
                    "system_bytes": record.system_json_bytes,
                    "messages_bytes": record.messages_json_bytes,
                    "tool_count": record.tool_count,
                    "stream": stream,
                    "agent_type": hints.get("x-claude-code-agent-type"),
                    "request_class": hints.get("x-claude-code-request-class"),
                    "markers": record.markers_found,
                }
            )
            force = (
                bool(self._agents)
                and not self._forced
                and "Agent" in record.tool_names
                and hints.get("x-claude-code-request-class", "main") != "subagent"
            )
            if force:
                self._forced = True
        if isinstance(model, str) and model in self._not_found:
            return 404, {
                "type": "error",
                "error": {"type": "not_found_error", "message": "probe: model not found"},
            }
        if isinstance(model, str) and model in self._refuse:
            return 200, _refusal_reply(model, self._refuse[model], stream, self._server_fallback)
        if force:
            content = [
                {
                    "type": "tool_use",
                    "id": f"toolu_client_check_{index:02d}",
                    "name": "Agent",
                    "input": {
                        "description": f"client check {index}",
                        "subagent_type": agent,
                        "prompt": "Reply with exactly: PROBE-OK",
                    },
                }
                for index, agent in enumerate(self._agents)
            ]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": "PROBE-OK"}]
            stop = "end_turn"
        payload = probe._message_payload(
            content, model=model, stop_reason=stop, message_id="msg_client_check"
        )
        return 200, probe._sse_message(payload) if stream else payload


class _SpikeTestCase(unittest.TestCase):
    """Operator-host boundary, per-run fixtures and the gated runner."""

    trusted: probe.TrustedExecutable
    version: str = "unknown"
    boundary_skip: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="client check probes")
        cls.boundary_skip = gate.boundary
        if gate.trusted is None:
            return
        cls.trusted = gate.trusted
        cls.version = gate.version

    def setUp(self) -> None:
        super().setUp()
        if self.boundary_skip is not None:
            self.skipTest(self.boundary_skip)
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-client-check-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        # Simulated live roots: the fixture stays disjoint from them and the
        # ambient credential scan sees none (the child never inherits it).
        self.environ = {"HOME": str(self.root / "live" / "home"), "PATH": "/usr/bin:/bin"}
        self._run_count = 0

    def _identity(self) -> str:
        return f"client={self.version} sha256={self.trusted.sha256[:12]}"

    def _fixture(self) -> probe.ProbeFixture:
        self._run_count += 1
        fixture = probe.build_fixture(
            self.root / f"run-{self._run_count:02d}", environ=self.environ
        )
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        return fixture

    @staticmethod
    def _write_agent(
        fixture: probe.ProbeFixture,
        name: str,
        *,
        model: str,
        effort: str | None,
        marker: str,
    ) -> None:
        claude_dir = state.ensure_private_dir(fixture.project_dir / ".claude")
        agents = state.ensure_private_dir(claude_dir / "agents")
        effort_line = "" if effort is None else f"effort: {effort}\n"
        body = (
            f"---\nname: {name}\ndescription: client check effort probe agent\n"
            f"model: {model}\n{effort_line}---\nProbe agent body {marker}.\n"
        )
        state.atomic_write(agents / f"{name}.md", body.encode("utf-8"))

    @staticmethod
    def _settings_argv(
        fixture: probe.ProbeFixture, lead: str, fence: tuple[str, ...] | None,
        settings_env: dict[str, str] | None = None,
    ) -> tuple[str, ...]:
        """Production-shaped flag settings: fence = lead + extra selectors
        (none: no fence), always with the managed permission default;
        ``settings_env`` is the scope ``env`` (where managed sessions also
        put CLAUDE_CODE_DISABLE_FAST_MODE)."""

        directory = state.ensure_private_dir(fixture.root / "scope")
        path = directory / "settings.json"
        document: dict[str, Any] = {"permissions": dict(_MANAGED_PERMISSIONS)}
        if settings_env:
            document["env"] = dict(settings_env)
        if fence is not None:
            document.update(availableModels=sorted({lead, *fence}), model=lead)
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        return ("--settings", str(path))

    def _run(
        self,
        probe_id: str,
        model: str,
        *,
        responder: _SpikeResponder,
        fence: tuple[str, ...] | None = None,
        child_env: dict[str, str] | None = None,
        extra: tuple[str, ...] = (),
        markers: dict[str, str] | None = None,
        fixture: probe.ProbeFixture | None = None,
        pty: bool = False,
        timeout: float = _RUN_TIMEOUT,
        settings_env: dict[str, str] | None = None,
    ) -> tuple[probe.NativeRunResult, list[dict[str, Any]]]:
        fixture = fixture if fixture is not None else self._fixture()
        argv = (
            *(() if pty else ("-p", _TURN)),
            "--model",
            model,
            *self._settings_argv(fixture, model, fence, settings_env),
            *extra,
        )
        provider = _BetaRecordingProvider(responder=responder, content_markers=markers)
        common = {
            "trusted": self.trusted,
            "fixture": fixture,
            "provider": provider,
            "timeout": timeout,
            "allow_real": True,
            "environ": self.environ,
            "child_env": child_env,
        }
        with provider:
            try:
                if pty:
                    prompt = "❯".encode("utf-8")
                    steps = (
                        probe.PTYInteraction(b"Choose", b"2\r"),
                        probe.PTYInteraction(b"Press", b"\r"),
                        probe.PTYInteraction(prompt, _TURN.encode("utf-8") + b"\r"),
                        probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                        probe.PTYInteraction(prompt, b"/exit\r"),
                    )
                    result = probe.run_native_pty(argv, steps, **common)
                else:
                    result = probe.run_native(argv, **common)
            except ProbeError as exc:
                if _TRIPWIRE in str(exc):
                    print(
                        f"client check {probe_id}: INCONCLUSIVE live daemon domain "
                        "touched (observe-only tripwire; cannot be decided offline)"
                    )
                    self.skipTest(f"BOUNDARY: {probe_id} INCONCLUSIVE: {exc}")
                raise
        self.assertFalse(result.timed_out, f"{probe_id} run timed out")
        self.assertIsNotNone(result.daemon)
        assert result.daemon is not None
        self.assertTrue(result.daemon.unchanged)
        rows = [
            {**row, "betas": provider.beta_names.get(row["ordinal"], ())}
            for row in responder.rows
        ]
        self.assertTrue(rows, f"{probe_id}: the client sent no message request")
        return result, rows


class SelectorRecognitionProbe(_SpikeTestCase):
    """S7: anthropic selectors, bare aliases and refusal fallback."""

    def _bare_aliases(self) -> str:
        observed: dict[str, dict[str, Any]] = {}
        markers = {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": _OPUS_MARKER,
            "ANTHROPIC_DEFAULT_FABLE_MODEL": _FABLE_MARKER,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": _SONNET_MARKER,
        }
        for alias in ("opus", "fable", "sonnet"):
            for label, env in (("plain", None), ("env", markers)):
                _result, rows = self._run(
                    "S7", alias, responder=_SpikeResponder(), child_env=env
                )
                observed[f"{alias}/{label}"] = rows[0]
        # Plain ANTHROPIC_BASE_URL is firstParty: bare aliases request the
        # CURRENT generation, which is the compiled family default's wire
        # (asserted against the catalog-derived default, no literal).
        self.assertEqual(observed["opus/plain"]["model"], _wire(_OPUS))
        self.assertEqual(observed["opus/plain"]["max_tokens"], 128000)
        self.assertFalse(_has_beta(observed["opus/plain"], "context-1m-"))
        self.assertEqual(observed["fable/plain"]["model"], _wire(_FABLE))
        self.assertEqual(observed["sonnet/plain"]["model"], _wire(_SONNET))
        # Each family override retargets only its own alias.
        self.assertEqual(observed["opus/env"]["model"], _OPUS_MARKER)
        self.assertEqual(observed["fable/env"]["model"], _FABLE_MARKER)
        self.assertEqual(observed["sonnet/env"]["model"], _SONNET_MARKER)
        return (
            f"bare opus->{observed['opus/plain']['model']} "
            f"fable->{observed['fable/plain']['model']} "
            f"sonnet->{observed['sonnet/plain']['model']} "
            "(= the compiled family defaults; DEFAULT_OPUS/FABLE/SONNET_MODEL retarget each alias)"
        )

    def _selector_classes(self) -> str:
        observed: dict[str, dict[str, Any]] = {}
        unrecognized: dict[str, bool] = {}
        for selector in (_OPUS, _MULTI_OPUS, _FABLE, _MULTI_FABLE, _SONNET):
            result, rows = self._run("S7", selector, responder=_SpikeResponder())
            observed[selector] = rows[0]
            unrecognized[selector] = _UNRECOGNIZED_MARKER in result.stderr
        for selector, row in observed.items():
            self.assertEqual(row["model"], _wire(selector))
            self.assertEqual(row["thinking_type"], "adaptive")
            self.assertTrue(_has_beta(row, "context-1m-"), selector)
        canonical_opus, multi_opus = observed[_OPUS], observed[_MULTI_OPUS]
        canonical_fable, multi_fable = observed[_FABLE], observed[_MULTI_FABLE]
        canonical_sonnet = observed[_SONNET]
        # Canonical ids are recognised: catalog output cap, the client's
        # default effort for the model, no warning. (2.1.284+ no longer sends
        # the fallback-credit beta on the request; the refusal fallback is
        # proven by the matrix below.)
        self.assertEqual(canonical_opus["max_tokens"], 128000)
        self.assertEqual(canonical_opus["effort"], "medium")
        self.assertEqual(canonical_fable["max_tokens"], 64000)
        self.assertEqual(canonical_fable["effort"], "high")
        self.assertEqual(canonical_sonnet["max_tokens"], 128000)
        self.assertEqual(canonical_sonnet["effort"], "medium")
        self.assertFalse(unrecognized[_OPUS] or unrecognized[_FABLE] or unrecognized[_SONNET])
        # claude-multi-* ids are unknown: generic 32000 cap, generic effort,
        # the unrecognized_model stderr marker.
        for selector in (_MULTI_OPUS, _MULTI_FABLE):
            self.assertEqual(observed[selector]["max_tokens"], 32000)
            self.assertEqual(observed[selector]["effort"], "high")
            self.assertTrue(unrecognized[selector])
        # The Fable prompt bundle is only sent for the canonical id.
        self.assertGreater(
            canonical_fable["system_bytes"], multi_fable["system_bytes"] * 3 // 2
        )
        return (
            f"canonical opus max_tokens={canonical_opus['max_tokens']} "
            f"effort={canonical_opus['effort']} fable max_tokens="
            f"{canonical_fable['max_tokens']} effort={canonical_fable['effort']} "
            f"sonnet max_tokens={canonical_sonnet['max_tokens']} effort={canonical_sonnet['effort']}; "
            f"claude-multi max_tokens={multi_opus['max_tokens']} effort={multi_opus['effort']} "
            "unrecognized_model; system bytes opus "
            f"{canonical_opus['system_bytes']}/{multi_opus['system_bytes']} fable "
            f"{canonical_fable['system_bytes']}/{multi_fable['system_bytes']}"
        )

    def _not_found_chain(self) -> str:
        chains: dict[str, list[int | None]] = {}
        for selector in (_OPUS, _MULTI_OPUS, _FABLE, _MULTI_FABLE):
            wire = _wire(selector)
            result, rows = self._run(
                "S7", selector, responder=_SpikeResponder(not_found=(wire,))
            )
            # No automatic model switch on 404: only same-wire retries, then
            # a visible "issue with the selected model" exit.
            self.assertEqual({row["model"] for row in rows}, {wire}, selector)
            self.assertNotEqual(result.returncode, 0, selector)
            chains[selector] = [row["max_tokens"] for row in rows]
        return (
            "not-found: same-wire retry only (max_tokens "
            f"opus {chains[_OPUS]} multi {chains[_MULTI_OPUS]} fable "
            f"{chains[_FABLE]} multi {chains[_MULTI_FABLE]}), no model switch"
        )

    def _refusal_fallback(self) -> str:
        """The refusal probe on the exact client: the category matrix with the
        compiled family defaults in the env (every managed scope pins
        ANTHROPIC_DEFAULT_OPUS_MODEL) and the production fallback-only fence.
        Through a non-first-party base URL the client lane decides; the
        refused turn is retried once on the same wire, then switches only to
        a fenced route-table target, never implicitly opting into the API's
        server-side fallback (no ``fallbacks`` field on any request)."""

        production = tuple(sorted({wire + "[1m]" for wire in catalog.REFUSAL_FALLBACK_WIRES}))
        self.assertEqual(set(production), {_OPUS48, _OPUS5, _SONNET5})
        cases = (
            # (label, lead, category, fence extras or None, expected target)
            ("opus-cyber-nofence", _OPUS, "cyber", None, "claude-opus-4-8"),
            ("opus-cyber-prod", _OPUS, "cyber", production, "claude-opus-4-8"),
            ("opus-cyber-leadonly", _OPUS, "cyber", (), None),
            ("opus-cyber-plain48", _OPUS, "cyber", ("claude-opus-4-8",), "claude-opus-4-8"),
            ("opus-bio-prod", _OPUS, "bio", production, "claude-opus-5"),
            ("opus-bio-with48", _OPUS, "bio", (_OPUS48,), None),
            ("opus-frontier-prod", _OPUS, "frontier_llm", production, "claude-opus-5"),
            ("opus-extraction-prod", _OPUS, "reasoning_extraction", production, None),
            ("opus-uncategorized", _OPUS, None, production, None),
            ("opus-unknown-category", _OPUS, "check_unknown", production, None),
            ("sonnet-cyber-prod", _SONNET, "cyber", production, "claude-sonnet-5"),
            ("sonnet-cyber-leadonly", _SONNET, "cyber", (), None),
            ("sonnet-frontier-prod", _SONNET, "frontier_llm", production, "claude-sonnet-5"),
            # Biology on Sonnet 5.5: terminal, ZERO fallback requests.
            ("sonnet-bio-prod", _SONNET, "bio", production, None),
            ("sonnet-extraction-prod", _SONNET, "reasoning_extraction", production, None),
            ("fable-cyber-prod", _FABLE, "cyber", production, "claude-opus-4-8"),
            ("fable-cyber-leadonly", _FABLE, "cyber", (), None),
            ("fable-bio-prod", _FABLE, "bio", production, "claude-opus-5"),
            ("fable-frontier-prod", _FABLE, "frontier_llm", production, "claude-opus-5"),
            ("multi-opus-cyber", _MULTI_OPUS, "cyber", None, None),
        )
        observed: dict[str, str] = {}
        for label, lead, category, fence, expected in cases:
            wire = _wire(lead)
            responder = _SpikeResponder(refuse={wire: category})
            result, rows = self._run("S7", lead, responder=responder, fence=fence, child_env=dict(_FAMILY))
            self.assertEqual(rows[0]["model"], wire, label)
            lead_rows = [row for row in rows if row["model"] == wire]
            switched = sorted({row["model"] for row in rows} - {wire})
            self.assertEqual(switched, [] if expected is None else [expected], label)
            self.assertEqual(result.returncode == 0, expected is not None, label)
            if expected is not None:
                self.assertIn(expected, catalog.REFUSAL_FALLBACK_WIRES, label)
                # One same-wire retry of the refused turn precedes the switch.
                self.assertEqual(len(lead_rows), 2, label)
            self.assertEqual(responder.fallbacks_fields, [], label)
            observed[label] = switched[0] if switched else "none"
        # A server-named target (the API lane's usage.iterations
        # fallback_message) outside the route table is never followed through
        # the gateway; the client lane keeps its fenced route-table target.
        responder = _SpikeResponder(refuse={_wire(_OPUS): "cyber"}, server_fallback="claude-check-server-chosen")
        result, rows = self._run("S7", _OPUS, responder=responder, fence=production, child_env=dict(_FAMILY))
        self.assertEqual(sorted({row["model"] for row in rows} - {_wire(_OPUS)}), ["claude-opus-4-8"])
        self.assertEqual(responder.fallbacks_fields, [])
        observed["opus-cyber-server-named"] = "claude-opus-4-8"
        # The user setting switchModelsOnFlag:false disables the switch.
        fixture = self._fixture()
        state.atomic_write(
            fixture.claude_config_dir / "settings.json",
            strict_json.canonical_file_bytes({"switchModelsOnFlag": False}),
        )
        result, rows = self._run(
            "S7",
            _OPUS,
            responder=_SpikeResponder(refuse={_wire(_OPUS): "cyber"}),
            fixture=fixture,
        )
        self.assertEqual({row["model"] for row in rows}, {_wire(_OPUS)})
        self.assertNotEqual(result.returncode, 0)
        observed["switchModelsOnFlag=false"] = "none"
        return (
            "refusal (stop_reason=refusal + stop_details.category; compiled family defaults; "
            "one same-wire retry, no fallbacks opt-in) switch target per case: "
            + " ".join(f"{label}->{target}" for label, target in observed.items())
            + " (a fence entry permits the switch with or without [1m]; a "
            "lead-only fence never switches; server-named targets are not followed)"
        )

    def test_s7_selector_recognition(self) -> None:
        details = [
            self._bare_aliases(),
            self._selector_classes(),
            self._not_found_chain(),
            self._refusal_fallback(),
        ]
        print(
            f"client check S7: PASS {self._identity()} canonical anthropic "
            "selectors recognised, claude-multi-* unknown; " + "; ".join(details)
        )


class SafetySwitchTests(_SpikeTestCase):
    """The refusal probe and the 2.1.282-2.1.286 release-note refusal shapes on the
    exact client, offline, with the compiled family defaults (every managed
    scope pins ANTHROPIC_DEFAULT_OPUS/SONNET_MODEL) and the production
    fallback-only fence. Records (a) any implicit server-side fallback
    opt-in (a ``fallbacks`` body field; the post-refusal
    ``fallback-credit-*`` beta is recorded separately), (b) the next target
    and (c) the fence outcome: a fenced fallback-only switch or a visible
    refusal (non-zero exit), never a silent run on the lead."""

    _SERVER_OUT = "claude-check-server-chosen"

    def _case(self, label, lead, category, expected, *, fence=None, server=None,
              extra=(), settings_env=None, user_settings=None):
        production = tuple(sorted({wire + "[1m]" for wire in catalog.REFUSAL_FALLBACK_WIRES}))
        fence = production if fence is None else fence
        wire = _wire(lead)
        fixture = None
        if user_settings is not None:
            fixture = self._fixture()
            state.atomic_write(fixture.claude_config_dir / "settings.json",
                               strict_json.canonical_file_bytes(user_settings))
        responder = _SpikeResponder(refuse={wire: category}, server_fallback=server)
        result, rows = self._run("refusal", lead, responder=responder, fence=fence, child_env=dict(_FAMILY),
                                 extra=extra, fixture=fixture, settings_env=settings_env)
        lead_rows = [row for row in rows if row["model"] == wire]
        switched = [row for row in rows if row["model"] != wire]
        self.assertEqual(rows[0]["model"], wire, label)
        # (a) no implicit server-side fallback opt-in, no credit beta up front.
        self.assertEqual(responder.fallbacks_fields, [], label)
        self.assertFalse(any("fallback" in name for name in rows[0]["betas"]), label)
        # One same-wire retry of the refused turn, never more lead requests.
        self.assertEqual(len(lead_rows), 2, label)
        # (b) next target; (c) fence outcome.
        self.assertEqual(sorted({row["model"] for row in switched}), [] if expected is None else [expected], label)
        if expected is None:
            self.assertNotEqual(result.returncode, 0, f"{label}: refusal must stay visible")
            return label, "visible-refusal", rows
        self.assertEqual(result.returncode, 0, label)
        self.assertIn(expected, catalog.REFUSAL_FALLBACK_WIRES, label)
        self.assertTrue({expected, expected + "[1m]"} & set(fence), f"{label}: target outside the fence")
        return label, f"{expected}(fallback-only)", rows

    def _credit_beta(self, rows) -> bool:
        return all(any(name.startswith("fallback-credit-") for name in row["betas"]) for row in rows[1:])

    def test_api_chosen_target_with_compiled_opus_pin(self) -> None:
        out = self._SERVER_OUT
        cases = (
            ("opus-cyber-server-out", _OPUS, "cyber", "claude-opus-4-8", {"server": out}),
            ("opus-cyber-server-in-set", _OPUS, "cyber", "claude-opus-4-8", {"server": _wire(_SONNET5)}),
            ("opus-bio-server-out", _OPUS, "bio", "claude-opus-5", {"server": out}),
            ("opus-cyber-leadonly-server", _OPUS, "cyber", None, {"server": out, "fence": ()}),
            ("sonnet-cyber-server-out", _SONNET, "cyber", "claude-sonnet-5", {"server": out}),
            ("sonnet-bio-server-out", _SONNET, "bio", None, {"server": out}),
        )
        observed, credit = [], []
        for label, lead, category, expected, kwargs in cases:
            label, outcome, rows = self._case(label, lead, category, expected, **kwargs)
            observed.append(f"{label}->{outcome}")
            credit.append(self._credit_beta(rows))
        print(f"client check refusal: PASS {self._identity()} API-named targets never followed (in or out of the "
              f"route table); client route table + fence decide: " + " ".join(observed)
              + f"; no fallbacks field; fallback-credit beta on every post-refusal request={all(credit)}, "
              "never on the first")

    def test_refusal_category_and_nonfast_fallback_matrix(self) -> None:
        nonfast = {scope.FAST_MODE_ENV: "1"}
        cases = (
            ("nonfast-cyber", _OPUS, "cyber", "claude-opus-4-8", {}),
            ("nonfast-bio", _OPUS, "bio", "claude-opus-5", {}),
            ("nonfast-frontier", _OPUS, "frontier_llm", "claude-opus-5", {}),
            ("nonfast-extraction", _OPUS, "reasoning_extraction", None, {}),
            ("nonfast-uncategorized", _OPUS, None, None, {}),
            ("nonfast-cyber-fallback-model", _OPUS, "cyber", "claude-opus-4-8", {"extra": ("--fallback-model", _SONNET)}),
            ("nonfast-uncategorized-fallback-model", _OPUS, None, None, {"extra": ("--fallback-model", _SONNET)}),
            ("nonfast-sonnet-frontier", _SONNET, "frontier_llm", "claude-sonnet-5", {}),
        )
        observed = []
        for label, lead, category, expected, kwargs in cases:
            label, outcome, rows = self._case(label, lead, category, expected, settings_env=nonfast, **kwargs)
            observed.append(f"{label}->{outcome}")
        # 2.1.282: the safety switch at xhigh keeps the effort; with thinking
        # off (settings or MAX_THINKING_TOKENS=0) the switched turn sends
        # thinking disabled.
        label, outcome, rows = self._case("xhigh-cyber", _OPUS, "cyber", "claude-opus-4-8", extra=("--effort", "xhigh"))
        self.assertEqual({row["effort"] for row in rows}, {"xhigh"})
        observed.append(f"{label}->{outcome}(effort xhigh kept)")
        for label, kwargs in (("thinking-off-settings", {"user_settings": {"alwaysThinkingEnabled": False}}),
                              ("thinking-off-env", {"settings_env": {"MAX_THINKING_TOKENS": "0"}})):
            label, outcome, rows = self._case(label, _OPUS, "cyber", "claude-opus-4-8", **kwargs)
            self.assertNotIn(rows[-1]["thinking_type"], ("adaptive", "enabled"), label)
            observed.append(f"{label}->{outcome}(thinking {rows[-1]['thinking_type']})")
        print(f"client check refusal-switches: PASS {self._identity()} non-fast (scope env {scope.FAST_MODE_ENV}=1) "
              "categories, --fallback-model ignored for refusals, xhigh and thinking-off switches: "
              + " ".join(observed))

    def test_sonnet_subagent_under_non_anthropic_lead_set(self) -> None:
        """Refusal case: a non-Anthropic lead set compiles no fallback-only
        selectors (``scope.fallback_only``), so a refused Sonnet subagent
        turn stays a visible refusal; it never re-runs on the lead model.
        With the fallback-only set fenced it switches to its route target."""

        production = tuple(sorted({wire + "[1m]" for wire in catalog.REFUSAL_FALLBACK_WIRES}))
        observed = []
        for label, category, fence, expected in (
            ("gpt-lead-cyber-no-fallback-set", "cyber", (_SONNET,), None),
            ("gpt-lead-bio-no-fallback-set", "bio", (_SONNET,), None),
            ("gpt-lead-cyber-fallback-set", "cyber", (_SONNET, *production), "claude-sonnet-5"),
        ):
            fixture = self._fixture()
            self._write_agent(fixture, "cm-safety", model=_SONNET, effort="high", marker="check-safety")
            responder = _SpikeResponder(agents=("cm-safety",), refuse={_wire(_SONNET): category})
            result, rows = self._run("refusal", _LEAD_GPT, responder=responder, fence=fence,
                                     child_env={**_FAMILY, **_HINT_ENV}, fixture=fixture)
            child = [row["model"] for row in rows if row["request_class"] == "subagent"]
            self.assertEqual(child[:2], [_wire(_SONNET)] * 2, label)
            self.assertEqual(sorted(set(child) - {_wire(_SONNET)}), [] if expected is None else [expected], label)
            self.assertNotIn(_wire(_LEAD_GPT), child, label)
            self.assertEqual(responder.fallbacks_fields, [], label)
            self.assertEqual(result.returncode, 0, label)
            observed.append(f"{label}->{expected or 'visible-agent-refusal'}")
        print(f"client check refusal-subagent: PASS {self._identity()} " + " ".join(observed)
              + " (a subagent refusal never runs on the lead)")


class AgentEffortProbe(_SpikeTestCase):
    """S8: agent frontmatter effort on the subagent's wire."""

    _EFFORTS = ("none", "ultracode", "low", "medium", "xhigh")

    def test_s8_agent_effort_vocabulary(self) -> None:
        fixture = self._fixture()
        markers: dict[str, str] = {}
        agents: list[str] = []
        for effort in self._EFFORTS:
            name = f"client-check-effort-{effort}"
            marker = f"CLIENT-CHECK-S8-{effort.upper()}-MARK"
            self._write_agent(
                fixture,
                name,
                model=_OPUS,
                effort=None if effort == "none" else effort,
                marker=marker,
            )
            markers[effort] = marker
            agents.append(name)
        # Lead at session effort ultracode (-> xhigh wire) so an agent that
        # inherits is distinguishable from the model default (medium).
        result, rows = self._run(
            "S8",
            _OPUS,
            responder=_SpikeResponder(agents=tuple(agents)),
            extra=("--effort", "ultracode", "--dangerously-skip-permissions"),
            markers=markers,
            fixture=fixture,
        )
        lead = rows[0]
        self.assertEqual(lead["effort"], "xhigh")
        wire: dict[str, dict[str, Any]] = {}
        for effort in self._EFFORTS:
            tagged = [row for row in rows if row["markers"] == (effort,)]
            self.assertEqual(len(tagged), 1, f"agent effort {effort} request")
            wire[effort] = tagged[0]
            self.assertEqual(tagged[0]["model"], "claude-opus-5-5")
            self.assertEqual(tagged[0]["thinking_type"], "adaptive")
        # low/medium/xhigh reach the wire verbatim; ultracode is rejected by
        # the loader (debug-log "has invalid effort", no stderr/UI marker) and
        # the agent silently inherits the session effort like "none".
        self.assertEqual(wire["low"]["effort"], "low")
        self.assertEqual(wire["medium"]["effort"], "medium")
        self.assertEqual(wire["xhigh"]["effort"], "xhigh")
        self.assertEqual(wire["ultracode"]["effort"], lead["effort"])
        self.assertEqual(wire["none"]["effort"], lead["effort"])
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("invalid effort", result.stderr)
        summary = " ".join(f"{effort}->{wire[effort]['effort']}" for effort in self._EFFORTS)
        print(
            f"client check S8: PASS {self._identity()} lead --effort ultracode "
            f"wire={lead['effort']}; agent frontmatter {summary} "
            "(ultracode rejected silently: debug-log only, inherits session effort)"
        )


class NativeAgentWireProbe(_SpikeTestCase):
    """S9: native helper wires under non-Opus leads and fences."""

    def _helper_wire(
        self,
        helper: str,
        lead: str,
        *,
        cap_off: bool,
        fence: tuple[str, ...] | None = None,
        opus_marker: bool = False,
        pty: bool = False,
    ) -> str | None:
        env = dict(_HINT_ENV)
        if cap_off:
            env[_EXPLORE_CAP_OFF] = "1"
        if opus_marker:
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = _OPUS_MARKER
        extra = () if pty else ("--dangerously-skip-permissions",)
        def run_helper(*, timeout=_RUN_TIMEOUT):
            # A retry gets a fresh fixture, provider and scripted responder.
            return self._run(
                "S9", lead, responder=_SpikeResponder(agents=(helper,)),
                fence=fence, child_env=env, extra=extra, pty=pty, timeout=timeout,
            )

        if pty:
            (_result, rows), retried = _retry_s9_pty(run_helper)
            self._pty_retried = getattr(self, "_pty_retried", False) or retried
        else:
            _result, rows = run_helper()
        helper_rows = [row for row in rows if row["agent_type"] == helper]
        self.assertTrue(helper_rows, f"{helper} was never spawned (lead {lead})")
        self.assertTrue(all(row["request_class"] == "subagent" for row in helper_rows))
        models = {row["model"] for row in helper_rows}
        self.assertEqual(len(models), 1, f"{helper} used several wires")
        return models.pop()

    def _explore(self) -> dict[str, str | None]:
        lead_wire = _LEAD_GPT
        observed = {
            "gpt": self._helper_wire("Explore", _LEAD_GPT, cap_off=False),
            "gpt+off": self._helper_wire("Explore", _LEAD_GPT, cap_off=True),
            "gpt+marker": self._helper_wire(
                "Explore", _LEAD_GPT, cap_off=False, opus_marker=True
            ),
            "gpt+marker+off": self._helper_wire(
                "Explore", _LEAD_GPT, cap_off=True, opus_marker=True
            ),
            "gpt fence+opus": self._helper_wire(
                "Explore", _LEAD_GPT, cap_off=False, fence=(_OPUS,)
            ),
            "gpt fence lead-only": self._helper_wire(
                "Explore", _LEAD_GPT, cap_off=False, fence=()
            ),
            "fable": self._helper_wire("Explore", _FABLE, cap_off=False),
            "fable+off": self._helper_wire("Explore", _FABLE, cap_off=True),
            "sonnet": self._helper_wire("Explore", _SONNET, cap_off=False),
        }
        # 2.1.284+ (re-measured on the pin): Explore under an
        # unrecognized proxy lead (GPT) inherits the lead whatever the cap
        # flag or the Opus default (C24 no longer holds for it). A recognized
        # non-Opus Anthropic lead (Fable) is still capped to the Opus family
        # default unless the cap flag is set (the production default, so the
        # compiled Explore override stays); Sonnet keeps its own model.
        for label in ("gpt", "gpt+off", "gpt+marker", "gpt+marker+off", "gpt fence+opus",
                      "gpt fence lead-only"):
            self.assertEqual(observed[label], lead_wire, label)
        self.assertEqual(observed["fable"], _wire(_OPUS))
        self.assertEqual(observed["fable+off"], _wire(_FABLE))
        self.assertEqual(observed["sonnet"], _wire(_SONNET))
        return observed

    def _statusline(self) -> dict[str, str | None]:
        observed = {
            "nofence": self._helper_wire("statusline-setup", _LEAD_GPT, cap_off=True),
            "fence+sonnet": self._helper_wire(
                "statusline-setup", _LEAD_GPT, cap_off=True, fence=(_SONNET,)
            ),
            "fence+opus": self._helper_wire(
                "statusline-setup", _LEAD_GPT, cap_off=True, fence=(_OPUS,)
            ),
        }
        # Pinned "sonnet" resolves to the current Sonnet wire, which is the
        # compiled Sonnet family default, when it is fenced (a
        # cross-provider Anthropic run under a GPT lead) and falls back to
        # the lead when no Sonnet selector is in the fence.
        self.assertEqual(observed["nofence"], _wire(_SONNET))
        self.assertEqual(observed["fence+sonnet"], _wire(_SONNET))
        self.assertEqual(observed["fence+opus"], _LEAD_GPT)
        return observed

    def _guide(self) -> dict[str, str | None]:
        # claude-code-guide is not registered for the sdk-cli entrypoint
        # (headless -p), so it is driven through the interactive PTY.
        observed = {
            "nofence": self._helper_wire(
                "claude-code-guide", _LEAD_GPT, cap_off=True, pty=True
            ),
            "fence+sonnet": self._helper_wire(
                "claude-code-guide", _LEAD_GPT, cap_off=True, fence=(_SONNET,), pty=True
            ),
        }
        self.assertTrue(str(observed["nofence"]).startswith("claude-haiku-4-5"))
        self.assertEqual(observed["fence+sonnet"], _LEAD_GPT)
        return observed

    def test_s9_native_agent_wires(self) -> None:
        explore = self._explore()
        statusline = self._statusline()
        guide = self._guide()

        def fmt(values: dict[str, str | None]) -> str:
            return ", ".join(f"{key}->{value}" for key, value in values.items())

        print(
            f"client check S9: PASS {self._identity()} Explore [{fmt(explore)}]; "
            f"statusline-setup [{fmt(statusline)}]; claude-code-guide "
            f"[{fmt(guide)}]; no errors, SPEC 1.3 table holds with "
            f"{_EXPLORE_CAP_OFF}=1"
            + (" (retried after a PTY timeout)"
               if getattr(self, "_pty_retried", False) else "")
        )
