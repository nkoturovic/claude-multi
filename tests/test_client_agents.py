"""Real-client probes S1-S3: live agent reload, durability, SendMessage.

Real-binary probes against the pinned Claude Code binary (resolved from
``catalog/native-contract.json`` exactly like ``RealPinnedBinaryTests``:
path + full SHA-256, never a hard-coded version). Each run goes through the
probe harness narrow allowance: disposable fixture HOME/CLAUDE_CONFIG_DIR,
the loopback fake provider, and the live-daemon-domain tripwire. A tripped
tripwire is INCONCLUSIVE (a BOUNDARY skip), never a pass.

The scope directory is synthetic: one agent file ``cm-spike-x.md`` passed via
``--add-dir`` whose frontmatter model is rewritten mid-session by a PTY
``before_send`` callback. The fixture settings carry the production-shaped
fence (lead + both agent selectors, lead pinned).

Design questions:

- S1: does ``/reload-plugins`` make the next spawn use the rewritten
  frontmatter model? (PASS => live reload is primary; FAIL => assisted relaunch)
- S2: after a live change, ``/compact``, exit, and a relaunch with the same
  ``--resume`` argv, does a spawn keep the new model? (PASS => durability
  holds; FAIL => live changes are blocked). A real supervisor takeover is not probed
  offline; it stays user acceptance, like U1.
- S3: does a SendMessage-resumed agent keep its original model after a
  reload? (shapes the lead-prompt wording)

Hermetic: every real run executes inside a loopback-only network namespace
(the harness's bwrap isolation, fake provider bridged over a fixture unix
socket) with a fixture-private ``CLAUDE_CODE_TMPDIR``; without isolation the
class skips with a BOUNDARY message. The tests pass a SYNTHETIC ambient
``environ`` (fake HOME under the scratch root, fixed PATH): that disables the
harness credential scan and live-roots check against the real environment
(the ``test_scope_probe.py`` precedent); the child never inherits it.

Every test asserts the observed client behaviour, so a future client flip
fails the suite. Request bodies are inspected in memory inside the
test-local responder only; what is kept and printed is metadata (models,
definition labels, hash prefixes, counters, booleans) — never prompt,
response, or transcript text.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_multi import probe, scope, state, strict_json
from claude_multi.probe import ProbeError

from _tier import real_binary_gate
from _layout import RESOURCES_ROOT  # the one root source


_CONTRACT_PATH = (
    RESOURCES_ROOT / "catalog" / "native-contract.json"
)

_LEAD = "spike-lead"
_MODEL_A = "spike-model-a"
_MODEL_B = "spike-model-b"
_AGENT_TYPE = "cm-spike-x"
_BODY_MARKER = "CLIENT-CHECK-X-BODY"
_DEFINITION_MARKERS = {"A": "CLIENT-CHECK-DEF-A", "B": "CLIENT-CHECK-DEF-B"}
_SUMMARY_MARKER = "CLIENTCHECK-COMPACT-SUMMARY"
_AGENT_TEMPLATE = """---
name: cm-spike-x
description: Synthetic probe agent for the live-reload probes.
model: {model}
---
You are the cm-spike-x fixture agent. {body} {definition}.
"""
_DIRECTIVE_RE = re.compile(r"CLIENTCHECK-(SPAWN|SEND)-(\d{1,2})")
_AGENT_ID_RE = re.compile(r"agentId: ([A-Za-z0-9_-]{4,64})")
_TOOL_ID_PREFIX = "toolu_check_"
_PROMPT_GLYPH = "❯".encode("utf-8")
_RELOAD_RE = re.compile(
    r"Reloaded:\s*(\d+)\s*plugins?\s*·\s*(\d+)\s*skills?\s*·\s*"
    r"(\d+)\s*agents?\s*·\s*(\d+)\s*hooks?\s*·\s*"
    r"(\d+)\s*plugin\s*MCP\s*servers?\s*·\s*(\d+)\s*plugin\s*LSP\s*servers?"
)
_RELOAD_COUNTERS = (
    "plugin",
    "skill",
    "agent",
    "hook",
    "plugin MCP server",
    "plugin LSP server",
)
_OSC_RE = re.compile(r"\x1b\][^\x07]*\x07")
_CURSOR_COLUMN_RE = re.compile(r"\x1b\[\d*G")
_CSI_RE = re.compile(r"\x1b\[[0-9;?<>=]*[A-Za-z~]")


def _screen_text(raw: str) -> str:
    """Flatten a PTY capture: cursor-column jumps become spaces."""

    text = _OSC_RE.sub("", raw)
    text = _CURSOR_COLUMN_RE.sub(" ", text)
    text = _CSI_RE.sub("", text)
    return " ".join(text.split())


def _reload_counters(raw: str) -> dict[str, int] | None:
    match = _RELOAD_RE.search(_screen_text(raw))
    if match is None:
        return None
    return {
        label: int(value) for label, value in zip(_RELOAD_COUNTERS, match.groups())
    }


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
    return "\n".join(parts)


@dataclass(frozen=True)
class _SubagentCall:
    """Metadata of one request made by the cm-spike-x subagent."""

    tag: str | None  # the directive that was active when it arrived
    model: str | None
    definition: str  # "A" | "B" | "?" (which definition body it carried)
    system_sha12: str
    message_count: int


class _SpikeResponder:
    """Directive-driven fake provider script for S1-S3.

    User turns carry directives (``CLIENTCHECK-SPAWN-<n>`` / ``CLIENTCHECK-SEND-<n>``).
    The first lead request for a new directive gets a forced ``Agent`` (or
    ``SendMessage`` to the agent spawned first) tool_use; the tool_result gets
    a ``CLIENTCHECK-DONE-<tag>`` reply and the background-completion notification
    a ``CLIENTCHECK-NOTIFIED-<tag>`` reply, so a PTY script can wait for the
    subagent to finish before its next step. Requests whose system prompt
    carries the agent body marker are the subagent's own; their model,
    definition label, and system-prompt hash prefix are recorded. While
    ``compacting`` is set every lead request gets a fixed summary text.
    Only metadata leaves the responder.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._issued: list[str] = []
        self._done: set[str] = set()
        self.current_tag: str | None = None
        self.subagent_calls: list[_SubagentCall] = []
        self.agent_ids: dict[str, str] = {}  # in memory only, never printed
        self.send_outcomes: dict[str, dict[str, bool]] = {}
        self.tool_surface: dict[str, Any] | None = None
        self.compacting = False
        self.compaction_requests = 0
        self.notified: list[str] = []

    def set_compacting(self, value: bool) -> None:
        with self._lock:
            self.compacting = value

    def calls_for(self, tag: str) -> list[_SubagentCall]:
        with self._lock:
            return [call for call in self.subagent_calls if call.tag == tag]

    def respond(
        self, document: dict[str, Any], path: str, context: Any
    ) -> tuple[int, Any]:
        route = urllib.parse.urlsplit(path).path.rstrip("/")
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return 404, {
                "type": "error",
                "error": {"type": "not_found_error", "message": "probe: unknown path"},
            }
        with self._lock:
            return self._respond_locked(document, context.ordinal)

    @staticmethod
    def _reply(
        document: dict[str, Any], blocks: list[dict[str, Any]], stop: str, ordinal: int
    ) -> tuple[int, Any]:
        payload = probe._message_payload(
            blocks,
            model=document.get("model"),
            stop_reason=stop,
            message_id=f"msg_client_check_{ordinal:04d}",
        )
        if document.get("stream"):
            return 200, probe._sse_message(payload)
        return 200, payload

    def _text(self, document: dict[str, Any], text: str, ordinal: int):
        return self._reply(document, [{"type": "text", "text": text}], "end_turn", ordinal)

    def _respond_locked(self, document: dict[str, Any], ordinal: int):
        model = document.get("model")
        system = strict_json.canonical_bytes(document.get("system", "")).decode(
            "utf-8"
        )
        messages = document.get("messages")
        messages = messages if isinstance(messages, list) else []
        if _BODY_MARKER in system:
            has_a = _DEFINITION_MARKERS["A"] in system
            has_b = _DEFINITION_MARKERS["B"] in system
            definition = "A" if has_a and not has_b else "B" if has_b and not has_a else "?"
            self.subagent_calls.append(
                _SubagentCall(
                    tag=self.current_tag,
                    model=model if isinstance(model, str) else None,
                    definition=definition,
                    system_sha12=hashlib.sha256(system.encode("utf-8")).hexdigest()[:12],
                    message_count=len(messages),
                )
            )
            return self._text(document, "CLIENTCHECK-SUBAGENT-OK", ordinal)
        if self.compacting:
            self.compaction_requests += 1
            return self._text(document, f"{_SUMMARY_MARKER} fixture summary", ordinal)
        tools = [
            tool for tool in document.get("tools") or [] if isinstance(tool, dict)
        ]
        names = [tool.get("name") for tool in tools]
        directives: list[tuple[str, str]] = []
        results: dict[str, dict[str, Any]] = {}
        last_user_text = ""
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            texts: list[str] = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                    directives.extend(_DIRECTIVE_RE.findall(block["text"]))
                elif block.get("type") == "tool_result":
                    tool_id = str(block.get("tool_use_id", ""))
                    if tool_id.startswith(_TOOL_ID_PREFIX):
                        results[tool_id[len(_TOOL_ID_PREFIX):]] = block
            last_user_text = "\n".join(texts)
        if "Agent" not in names or not directives:
            return self._text(document, "PROBE-OK", ordinal)
        kind, number = directives[-1]
        tag = f"{kind.lower()}{number}"
        if tag not in self._issued:
            self._issued.append(tag)
            self.current_tag = tag
            if self.tool_surface is None:
                self.tool_surface = self._surface(tools)
            return self._reply(document, [self._tool_use(kind, tag)], "tool_use", ordinal)
        if tag in results and tag not in self._done:
            self._done.add(tag)
            self._absorb_result(kind, tag, results[tag])
            return self._text(document, f"CLIENTCHECK-DONE-{tag}", ordinal)
        if "<task-notification>" in last_user_text:
            self.notified.append(tag)
            return self._text(document, f"CLIENTCHECK-NOTIFIED-{tag}", ordinal)
        return self._text(document, "PROBE-OK", ordinal)

    @staticmethod
    def _surface(tools: list[dict[str, Any]]) -> dict[str, Any]:
        def props(name: str) -> tuple[str, ...] | None:
            tool = next((item for item in tools if item.get("name") == name), None)
            if tool is None:
                return None
            schema = tool.get("input_schema") or {}
            return tuple(sorted((schema.get("properties") or {}).keys()))

        return {
            "names": tuple(sorted(str(tool.get("name")) for tool in tools)),
            "agent_props": props("Agent"),
            "send_message_props": props("SendMessage"),
        }

    def _tool_use(self, kind: str, tag: str) -> dict[str, Any]:
        if kind == "SPAWN":
            return {
                "type": "tool_use",
                "id": f"{_TOOL_ID_PREFIX}{tag}",
                "name": "Agent",
                "input": {
                    "description": "client check delegation",
                    "subagent_type": _AGENT_TYPE,
                    "prompt": "Reply with exactly one short line.",
                },
            }
        target = next(iter(self.agent_ids.values()), "client-check-missing-agent")
        return {
            "type": "tool_use",
            "id": f"{_TOOL_ID_PREFIX}{tag}",
            "name": "SendMessage",
            "input": {
                "to": target,
                "summary": "client check follow-up",
                "message": "Reply again with one short line.",
            },
        }

    def _absorb_result(self, kind: str, tag: str, block: dict[str, Any]) -> None:
        text = _text_of(block.get("content"))
        if kind == "SPAWN":
            match = _AGENT_ID_RE.search(text)
            if match:
                self.agent_ids[tag] = match.group(1)
            return
        target = next(iter(self.agent_ids.values()), None)
        try:
            outcome = strict_json.loads(text.encode("utf-8"))
        except strict_json.StrictJSONError:
            outcome = None
        success = isinstance(outcome, dict) and outcome.get("success") is True
        resumed = (
            isinstance(outcome, dict)
            and target is not None
            and outcome.get("resumedAgentId") == target
        )
        self.send_outcomes[tag] = {
            "is_error": bool(block.get("is_error")),
            "success": success,
            "resumed_target": resumed,
        }


class _SpikeRealBinaryTestCase(unittest.TestCase):
    """Operator-host-only base: pinned binary, fixture, synthetic scope."""

    trusted: probe.TrustedExecutable
    evidence_label: str = ""
    boundary_skip: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="the real-client probes")
        cls.boundary_skip = gate.boundary
        if gate.trusted is None:
            return
        cls.trusted = gate.trusted
        cls.evidence_label = f"client={gate.version} sha256={gate.trusted.sha256[:12]}"

    def setUp(self) -> None:
        super().setUp()
        if self.boundary_skip is not None:
            self.skipTest(self.boundary_skip)
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-client-check-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        # Simulated live roots, as in the F-series probes: the fixture stays
        # disjoint and the ambient scan sees no provider credentials.
        self.environ = {
            "HOME": str(self.root / "live" / "home"),
            "PATH": "/usr/bin:/bin",
        }
        self._prepare_fixture("fixture")

    def _prepare_fixture(self, name: str) -> None:
        """Fresh fixture + synthetic scope (model A) as the active fixture."""

        self.fixture = probe.build_fixture(self.root / name, environ=self.environ)
        self.scope = state.ensure_private_dir(self.fixture.root / "scope")
        self.agents_dir = state.ensure_private_dir(self.scope / ".claude" / "agents")
        self._write_agent(_MODEL_A, "A")
        # Production-shaped fence: lead + every bound selector, lead
        # pinned, so neither agent model silently falls back to the lead.
        # Plus the managed permission default (2.1.284+ otherwise
        # starts in auto mode, which cannot classify an Agent spawn through a
        # gateway and blocks it).
        state.atomic_write(
            self.fixture.claude_config_dir / "settings.json",
            strict_json.canonical_file_bytes(
                {"availableModels": [_LEAD, _MODEL_A, _MODEL_B], "model": _LEAD,
                 "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE}}
            ),
        )
        probe.seed_fixture_project_trust(self.fixture, self.fixture.project_dir)
        probe.seed_fixture_project_trust(self.fixture, self.scope)

    def _write_agent(self, model: str, definition: str) -> None:
        body = _AGENT_TEMPLATE.format(
            model=model,
            body=_BODY_MARKER,
            definition=_DEFINITION_MARKERS[definition],
        )
        state.atomic_write(self.agents_dir / f"{_AGENT_TYPE}.md", body.encode("utf-8"))

    def _rewrite_to_b(self) -> None:
        self._write_agent(_MODEL_B, "B")

    def _boundary_entries(self, session_id: str) -> int:
        """Count ``system/compact_boundary`` entries in the FIXTURE transcript.

        Metadata only: each line of the synthetic fixture session file is
        parsed and only its ``type``/``subtype`` keys are compared; nothing
        is kept. Never touches a live transcript root.
        """

        projects = self.fixture.claude_config_dir / "projects"
        count = 0
        for path in projects.glob(f"*/{session_id}.jsonl"):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = strict_json.loads(line.encode("utf-8"))
                    except strict_json.StrictJSONError:
                        continue
                    if (
                        isinstance(entry, dict)
                        and entry.get("type") == "system"
                        and entry.get("subtype") == "compact_boundary"
                    ):
                        count += 1
        return count

    def _await_boundary(self, session_id: str, timeout: float = 20.0) -> Any:
        """PTY callback: block ``/exit`` until the compact boundary is on disk.

        The client renders "Compacted" before it appends the boundary entry;
        exiting inside that window (about 0.5 s here) persists no boundary,
        so the relaunch replays the pre-compaction history. This barrier
        makes the resume observation deterministic and network-independent.
        """

        def callback() -> None:
            deadline = time.monotonic() + timeout
            while self._boundary_entries(session_id) < 1:
                if time.monotonic() > deadline:
                    raise RuntimeError("compact boundary was never persisted")
                time.sleep(0.1)

        return callback

    def _onboarding(self) -> list[probe.PTYInteraction]:
        return [
            probe.PTYInteraction(b"Choose", b"2\r"),
            probe.PTYInteraction(b"Press", b"\r"),
        ]

    def _run_pty(
        self,
        probe_id: str,
        session_id: str,
        interactions: list[probe.PTYInteraction],
        provider: probe.FakeAnthropicProvider,
    ) -> probe.NativeRunResult:
        try:
            result = probe.run_native_pty(
                (
                    "--session-id",
                    session_id,
                    "--model",
                    _LEAD,
                    "--add-dir",
                    str(self.scope),
                ),
                interactions,
                trusted=self.trusted,
                fixture=self.fixture,
                provider=provider,
                timeout=120,
                allow_real=True,
                environ=self.environ,
            )
        except ProbeError as exc:
            self._boundary_if_live_domain(probe_id, exc)
            raise
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(result.daemon)
        assert result.daemon is not None
        self.assertTrue(result.daemon.unchanged)
        return result

    def _boundary_if_live_domain(self, probe_id: str, exc: ProbeError) -> None:
        if "live daemon domain touched" in str(exc):
            print(
                f"client check {probe_id}: INCONCLUSIVE live daemon domain touched "
                f"({self.evidence_label})"
            )
            self.skipTest(
                "BOUNDARY: live daemon-domain churn prevented a clean "
                f"{probe_id} observation ({exc})"
            )

    def _single_call(self, responder: _SpikeResponder, tag: str) -> _SubagentCall:
        calls = responder.calls_for(tag)
        self.assertEqual(
            len(calls),
            1,
            {"tag": tag, "calls": [(c.tag, c.model, c.definition) for c in responder.subagent_calls]},
        )
        return calls[0]


class LiveAgentReloadSpikeTests(_SpikeRealBinaryTestCase):
    """S1: scope rewrite + ``/reload-plugins`` changes the next spawn's model."""

    def test_s1_reload_plugins_rebinds_next_spawn(self) -> None:
        responder = _SpikeResponder()
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT_GLYPH, b"CLIENTCHECK-SPAWN-1 go\r"),
            # Rewrite the scope file, then spawn again WITHOUT a reload: the
            # control that add-dir agents are not hot-watched.
            probe.PTYInteraction(
                b"CLIENTCHECK-NOTIFIED-spawn1",
                b"CLIENTCHECK-SPAWN-2 go\r",
                before_send=self._rewrite_to_b,
            ),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-spawn2", b"/reload-plugins\r"),
            probe.PTYInteraction(b"Reloaded", b"CLIENTCHECK-SPAWN-3 go\r"),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-spawn3", b"/exit\r"),
        ]
        with probe.FakeAnthropicProvider(responder=responder) as provider:
            result = self._run_pty(
                "S1", "34343434-0001-4001-8001-000000000001", steps, provider
            )
        before = self._single_call(responder, "spawn1")
        stale = self._single_call(responder, "spawn2")
        after = self._single_call(responder, "spawn3")
        # Initial spawn: frontmatter model A through the fence.
        self.assertEqual((before.model, before.definition), (_MODEL_A, "A"))
        # Rewritten on disk but not reloaded: the old definition still wins.
        self.assertEqual((stale.model, stale.definition), (_MODEL_A, "A"))
        # After /reload-plugins the next spawn carries the new model and body.
        self.assertEqual((after.model, after.definition), (_MODEL_B, "B"))
        counters = _reload_counters(result.stdout)
        self.assertIsNotNone(counters, "the Reloaded counter line was not rendered")
        assert counters is not None
        self.assertGreaterEqual(counters["agent"], 1)
        screen = _screen_text(result.stdout)
        self.assertNotIn("installed but not applied", screen)
        self.assertNotIn("--force", screen)
        lead_models = {
            record.model
            for record in result.requests
            if record.model is not None and record.model not in (_MODEL_A, _MODEL_B)
        }
        self.assertEqual(lead_models, {_LEAD})
        print(
            "client check S1: PASS "
            f"spawn1={before.model} no-reload-spawn2={stale.model} "
            f"post-reload-spawn3={after.model}/def-{after.definition} "
            "reload-line=Reloaded[" + ",".join(
                f"{label}={value}" for label, value in counters.items()
            ) + "] staged-plugin-update=INCONCLUSIVE(marketplace install needed; "
            f"not stageable offline) {self.evidence_label}"
        )


class LiveChangeDurabilitySpikeTests(_SpikeRealBinaryTestCase):
    """S2: live change survives /compact + a same-argv ``--resume`` relaunch."""

    def test_s2_compact_and_resume_relaunch_keep_new_model(self) -> None:
        session_id = "34343434-0002-4002-8002-000000000002"
        responder = _SpikeResponder()
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT_GLYPH, b"CLIENTCHECK-SPAWN-1 go\r"),
            probe.PTYInteraction(
                b"CLIENTCHECK-NOTIFIED-spawn1",
                b"/reload-plugins\r",
                before_send=self._rewrite_to_b,
            ),
            probe.PTYInteraction(b"Reloaded", b"CLIENTCHECK-SPAWN-2 go\r"),
            probe.PTYInteraction(
                b"CLIENTCHECK-NOTIFIED-spawn2",
                b"/compact\r",
                before_send=lambda: responder.set_compacting(True),
            ),
            probe.PTYInteraction(
                b"Compacted",
                b"/exit\r",
                before_send=lambda: (
                    responder.set_compacting(False),
                    self._await_boundary(session_id)(),
                ),
            ),
        ]
        with probe.FakeAnthropicProvider(
            responder=responder, content_markers={"summary": _SUMMARY_MARKER}
        ) as provider:
            self._run_pty("S2", session_id, steps, provider)
            first_phase = len(provider.requests)
            try:
                relaunch = probe.run_native(
                    (
                        "-p",
                        "CLIENTCHECK-SPAWN-3 go",
                        "--resume",
                        session_id,
                        "--model",
                        _LEAD,
                        "--add-dir",
                        str(self.scope),
                    ),
                    trusted=self.trusted,
                    fixture=self.fixture,
                    provider=provider,
                    timeout=120,
                    allow_real=True,
                    environ=self.environ,
                )
            except ProbeError as exc:
                self._boundary_if_live_domain("S2", exc)
                raise
        self.assertFalse(relaunch.timed_out)
        self.assertEqual(relaunch.returncode, 0)
        assert relaunch.daemon is not None
        self.assertTrue(relaunch.daemon.unchanged)
        self.assertGreaterEqual(responder.compaction_requests, 1)
        before = self._single_call(responder, "spawn1")
        live = self._single_call(responder, "spawn2")
        resumed = self._single_call(responder, "spawn3")
        self.assertEqual((before.model, before.definition), (_MODEL_A, "A"))
        self.assertEqual((live.model, live.definition), (_MODEL_B, "B"))
        # Binding durability (the design claim): the relaunch's spawn still
        # carries the live-changed binding.
        self.assertEqual((resumed.model, resumed.definition), (_MODEL_B, "B"))
        # Compacted-transcript resume, made deterministic by the boundary
        # barrier above (/exit only after system/compact_boundary is on
        # disk): every relaunch lead request starts from the summary. The
        # review's netns FAIL was /exit landing inside the ~0.5 s window
        # between the "Compacted" render and the boundary append (no
        # network dependency: the same hermetic run passes with the barrier).
        relaunch_lead = [
            record
            for record in relaunch.requests[first_phase:]
            if record.model == _LEAD and record.max_tokens is not None
        ]
        self.assertTrue(relaunch_lead, "the relaunch made no lead request")
        self.assertTrue(
            all("summary" in record.markers_found for record in relaunch_lead),
            [(r.message_count, r.markers_found) for r in relaunch_lead],
        )
        self.assertEqual(self._boundary_entries(session_id), 1)
        print(
            "client check S2: PASS "
            f"post-reload={live.model} compaction_requests="
            f"{responder.compaction_requests} resume-relaunch-spawn="
            f"{resumed.model}/def-{resumed.definition} "
            "compacted-resume=asserted(after-boundary-persisted; relaunch lead "
            f"message_counts={[r.message_count for r in relaunch_lead]}) "
            "compacted-resume-race=/exit-before-boundary-append-replays-"
            "pre-compaction-history "
            "takeover=user-acceptance(U1-like) "
            f"{self.evidence_label}"
        )


class CompactBoundaryRaceTests(_SpikeRealBinaryTestCase):
    """S2 root-cause evidence: /exit before the boundary append.

    Records (does not require) whether an immediate ``/exit`` after the
    "Compacted" render persisted the ``compact_boundary`` entry, and what a
    ``--resume`` then replays. This is the observed netns FAIL shape; it is
    timing-dependent by nature, so the outcome is printed, and only the
    consistency between "boundary persisted" and "relaunch carries the
    summary" is asserted.
    """

    def test_s2_race_exit_before_boundary_append(self) -> None:
        session_id = "34343434-0002-4002-8002-0000000000a2"
        responder = _SpikeResponder()
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT_GLYPH, b"CLIENTCHECK-SPAWN-1 go\r"),
            probe.PTYInteraction(
                b"CLIENTCHECK-NOTIFIED-spawn1",
                b"/compact\r",
                before_send=lambda: responder.set_compacting(True),
            ),
            probe.PTYInteraction(
                b"Compacted",
                b"/exit\r",
                before_send=lambda: responder.set_compacting(False),
            ),
        ]
        with probe.FakeAnthropicProvider(
            responder=responder, content_markers={"summary": _SUMMARY_MARKER}
        ) as provider:
            self._run_pty("S2-race", session_id, steps, provider)
            persisted = self._boundary_entries(session_id)
            first_phase = len(provider.requests)
            try:
                relaunch = probe.run_native(
                    ("-p", "CLIENTCHECK-RACE go", "--resume", session_id, "--model", _LEAD),
                    trusted=self.trusted,
                    fixture=self.fixture,
                    provider=provider,
                    timeout=120,
                    allow_real=True,
                    environ=self.environ,
                )
            except ProbeError as exc:
                self._boundary_if_live_domain("S2-race", exc)
                raise
        self.assertEqual(relaunch.returncode, 0)
        lead = [
            record
            for record in relaunch.requests[first_phase:]
            if record.model == _LEAD and record.max_tokens is not None
        ]
        self.assertTrue(lead)
        carries = all("summary" in record.markers_found for record in lead)
        # Consistency: the relaunch starts from the summary exactly when the
        # boundary made it to disk before exit.
        self.assertEqual(carries, persisted >= 1)
        print(
            "client check S2-race: immediate /exit after 'Compacted' -> "
            f"boundary_persisted={persisted >= 1} relaunch_from_summary={carries} "
            f"relaunch_lead_message_counts={[r.message_count for r in lead]} "
            f"{self.evidence_label}"
        )


class SendMessageResumeSpikeTests(_SpikeRealBinaryTestCase):
    """S3: which model does a SendMessage-resumed agent use after a reload."""

    def _control_without_reload(self) -> tuple[_SubagentCall, _SubagentCall]:
        """Control: same spawn + SendMessage, no rewrite and no reload.

        Runs in its own fresh fixture (the onboarding prompts appear only on
        a fixture's first launch); the main fixture is restored afterwards.
        """

        saved = (self.fixture, self.scope, self.agents_dir)
        self._prepare_fixture("fixture-control")
        self.addCleanup(self._restore_fixture, saved)
        try:
            return self._control_run()
        finally:
            self._restore_fixture(saved)

    def _restore_fixture(self, saved: tuple[Any, Any, Any]) -> None:
        self.fixture, self.scope, self.agents_dir = saved

    def _control_run(self) -> tuple[_SubagentCall, _SubagentCall]:
        responder = _SpikeResponder()
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT_GLYPH, b"CLIENTCHECK-SPAWN-1 go\r"),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-spawn1", b"CLIENTCHECK-SEND-2 go\r"),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-send2", b"/exit\r"),
        ]
        with probe.FakeAnthropicProvider(responder=responder) as provider:
            self._run_pty(
                "S3", "34343434-0003-4003-8003-0000000000c0", steps, provider
            )
        self.assertEqual(
            responder.send_outcomes.get("send2"),
            {"is_error": False, "success": True, "resumed_target": True},
        )
        return self._single_call(responder, "spawn1"), self._single_call(
            responder, "send2"
        )

    def test_s3_sendmessage_resume_takes_new_model_with_old_prompt(self) -> None:
        control_original, control_resumed = self._control_without_reload()
        # Without a rewrite and reload the resumed agent keeps model A and
        # its original prompt: the model switch below is the reload's doing.
        self.assertEqual(
            (control_original.model, control_original.definition), (_MODEL_A, "A")
        )
        self.assertEqual(
            (control_resumed.model, control_resumed.definition), (_MODEL_A, "A")
        )
        self.assertEqual(control_resumed.system_sha12, control_original.system_sha12)
        responder = _SpikeResponder()
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT_GLYPH, b"CLIENTCHECK-SPAWN-1 go\r"),
            probe.PTYInteraction(
                b"CLIENTCHECK-NOTIFIED-spawn1",
                b"/reload-plugins\r",
                before_send=self._rewrite_to_b,
            ),
            probe.PTYInteraction(b"Reloaded", b"CLIENTCHECK-SEND-2 go\r"),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-send2", b"/exit\r"),
        ]
        with probe.FakeAnthropicProvider(responder=responder) as provider:
            self._run_pty(
                "S3", "34343434-0003-4003-8003-000000000003", steps, provider
            )
        surface = responder.tool_surface
        self.assertIsNotNone(surface)
        assert surface is not None
        # The client offers SendMessage to the lead in this mode.
        self.assertIn("SendMessage", surface["names"])
        self.assertTrue({"to", "message"} <= set(surface["send_message_props"] or ()))
        self.assertIn("spawn1", responder.agent_ids)
        self.assertEqual(
            responder.send_outcomes.get("send2"),
            {"is_error": False, "success": True, "resumed_target": True},
        )
        original = self._single_call(responder, "spawn1")
        resumed = self._single_call(responder, "send2")
        self.assertEqual((original.model, original.definition), (_MODEL_A, "A"))
        # Observed: the resumed agent does NOT keep its original model — it
        # takes the reloaded frontmatter model — while its system prompt is
        # the original spawn's, byte for byte (old definition body).
        self.assertEqual(resumed.model, _MODEL_B)
        self.assertEqual(resumed.definition, "A")
        self.assertEqual(resumed.system_sha12, original.system_sha12)
        self.assertGreater(resumed.message_count, original.message_count)
        props = ",".join(surface["send_message_props"] or ())
        sha_relation = (
            "sha-equal" if resumed.system_sha12 == original.system_sha12 else "sha-differs"
        )
        print(
            "client check S3: FAIL "
            f"tool=SendMessage({props}) original={original.model}/def-"
            f"{original.definition} resumed={resumed.model} resumed-system="
            f"original-def-{resumed.definition}({sha_relation}) "
            f"no-reload-control={control_resumed.model}/def-"
            f"{control_resumed.definition} "
            f"{self.evidence_label}"
        )


if __name__ == "__main__":
    unittest.main()
