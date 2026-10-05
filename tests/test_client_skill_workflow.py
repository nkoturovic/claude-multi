"""Client checks S4, S10 and S13 against the pinned Claude Code.

S4 — in-session ``/cm`` as a scope skill: a ``--add-dir`` scope
skill with ``disable-model-invocation: true`` and ``allowed-tools:
Bash(claude-multi lineup:*)`` invokes a recording fake ``claude-multi`` on
PATH. The probe pins that the skill is invocable, that
``${CLAUDE_SESSION_ID}`` becomes the runtime session id reported by the
SessionStart hook, which argument bytes reach the launcher for the SPEC's
unquoted ``$ARGUMENTS`` body versus a single-quoted ``'$ARGUMENTS'`` body,
and where a permission prompt does (model-relayed Bash with ``;``) and does
not (dynamic-context ``!`cmd``` bodies) appear in a trusted project.

S10 — Workflow ``agent()``: the fake
lead emits a ``Workflow`` tool_use whose script spawns ``agent()`` without
``agentType``, with ``agentType: 'cm-spike-w'`` (frontmatter model), with
``isolation: 'worktree'`` in a git-initialised fixture project, and with a
frontmatter-only ``isolation: worktree`` agent. The wire model, the
gateway hint agent type and a cwd *label* (project vs worktree, derived in
memory from the agent request's environment block) are recorded per agent;
``CLAUDE_CODE_SUBAGENT_MODEL`` is exercised through ``settings.env`` inside
and outside the ``availableModels`` fence. A gate test pins which settings
and env keys switch the Workflow tool off.

S13 — agent-spawning skills: agents compiled
by the production agent-file compiler from the shipped read-only roles
(explorer, reviewer, reviewer-strong) get no ``Skill`` tool and no skill
listing, so a forced call to a ``context: fork`` fixture skill is refused
and nothing forks; the analyst keeps ``Skill`` and an inline skill works
(positive control); the earlier read-only policy lets every read-only
role fork (negative control); and a frontmatter ``Skill(<name>)`` entry
removes the whole ``Skill`` tool at the pinned client, so a per-skill rule
cannot be expressed in agent frontmatter.

Real-binary runs only: the pinned binary comes from
``catalog/native-contract.json`` (path + sha256), fixture HOME and
CLAUDE_CONFIG_DIR, the loopback fake provider, and the live-domain
tripwire. Off the operator host every test skips with a BOUNDARY message;
a "live daemon domain touched" outcome is reported INCONCLUSIVE and
skipped, never passed. Every real run is hermetic: a loopback-only network
namespace (fake provider bridged over a fixture unix socket) and a
fixture-private ``CLAUDE_CODE_TMPDIR``; without isolation the classes skip
with a BOUNDARY message. The tests pass a SYNTHETIC ambient ``environ`` (fake
HOME under the scratch root, fixed PATH), which disables the harness
credential scan and live-roots check against the real environment (the
``test_scope_probe.py`` precedent); the child never inherits it. Evidence is metadata only: request bodies are
inspected in memory by test-local responders that keep labels, models and
counters; no prompt, response or transcript text is persisted or printed.
Each probe prints exactly one ``client check S<n>: <verdict> <detail>`` line.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from typing import Any

from claude_multi import catalog, probe, profile, scope, state, strict_json
from claude_multi.probe import ProbeError

from _tier import real_binary_gate
from _layout import REPO_ROOT, RESOURCES_ROOT  # the one root source


_CONTRACT_PATH = (
    RESOURCES_ROOT / "catalog" / "native-contract.json"
)
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_LIVE_DOMAIN_TOUCHED = "live daemon domain touched"


def _messages_payload(
    content: list[dict[str, Any]], document: dict[str, Any], message_id: str
) -> Any:
    stop = "tool_use" if content[0].get("type") == "tool_use" else "end_turn"
    payload = probe._message_payload(
        content,
        model=document.get("model"),
        stop_reason=stop,
        message_id=message_id,
    )
    return probe._sse_message(payload) if document.get("stream") else payload


def _route(path: str) -> str:
    return urllib.parse.urlsplit(path).path.rstrip("/")


def _not_found() -> tuple[int, dict[str, Any]]:
    return 404, {
        "type": "error",
        "error": {"type": "not_found_error", "message": "probe: unknown path"},
    }


def _tool_names(document: dict[str, Any]) -> set[str]:
    return {
        tool.get("name")
        for tool in document.get("tools") or []
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }


class _SpikeTestCase(unittest.TestCase):
    """Operator-host boundary (same checks as RealPinnedBinaryTests) plus a
    disposable fixture per test."""

    trusted: probe.TrustedExecutable
    boundary_skip: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="client check probes")
        cls.boundary_skip = gate.boundary
        if gate.trusted is not None:
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
        self.fixture = probe.build_fixture(self.root / "fixture", environ=self.environ)
        self.scope = state.ensure_private_dir(self.fixture.root / "scope")

    @property
    def binary_label(self) -> str:
        return f"bin={self.trusted.resolved_path.name}/{self.trusted.sha256[:12]}"

    def _guard_live_domain(self, probe_id: str, run: Any) -> Any:
        """Run one native step; a touched live domain is INCONCLUSIVE."""

        try:
            return run()
        except ProbeError as exc:
            if _LIVE_DOMAIN_TOUCHED in str(exc):
                print(
                    f"client check {probe_id}: INCONCLUSIVE live daemon domain "
                    f"touched during the fixture run {self.binary_label}"
                )
                self.skipTest(
                    f"BOUNDARY: {_LIVE_DOMAIN_TOUCHED}; {probe_id} is "
                    f"inconclusive, never a pass ({exc})"
                )
            raise


# ------------------------------------------------------------------- S4


# The specified body verbatim (unquoted $ARGUMENTS), a single-quoted
# variant, and a model-relayed Bash variant. All three are dynamic-context
# or relay forms of the same launcher command.
_SKILL_FRONTMATTER = (
    "---\n"
    "name: {name}\n"
    "description: {marker} claude-multi lineup control (client check fixture)\n"
    "disable-model-invocation: true\n"
    "allowed-tools: Bash(claude-multi lineup:*)\n"
    "---\n"
)
_SKILL_BODIES = {
    "cm": "!`claude-multi lineup --session ${CLAUDE_SESSION_ID} $ARGUMENTS`\n",
    "cmq": "!`claude-multi lineup --session ${CLAUDE_SESSION_ID} '$ARGUMENTS'`\n",
    "cmr": (
        "Run exactly this command with the Bash tool and relay its output.\n"
        "BEGIN-CHECK-CMD\n"
        "claude-multi lineup --session ${CLAUDE_SESSION_ID} $ARGUMENTS\n"
        "END-CHECK-CMD\n"
    ),
}
_SKILL_DESC_MARKER = "CHECK-SKILL-DESC-0001"
_LINEUP_OUT_RE = re.compile(r"CHECK-LINEUP-OUT-(\d{4})")
_RELAY_RE = re.compile(r"BEGIN-CHECK-CMD\\n(.*?)\\nEND-CHECK-CMD")


def _ack(ordinal: int) -> bytes:
    return f"CHECK-ACK-{ordinal:04d}".encode("ascii")


class _SkillResponder:
    """In-memory S4 script: ACK each launcher output, relay ``cmr`` bodies.

    A request whose last message carries the ``cmr`` command block (not yet
    relayed) gets a Bash tool_use with exactly that command; a request whose
    last message carries launcher output ``CHECK-LINEUP-OUT-<n>`` is answered
    ``CHECK-ACK-<n>`` (a unique PTY barrier per launcher call); anything else
    gets ``PROBE-OK``. Only counters and ordinals are retained.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._relayed: set[str] = set()
        self._requests = 0
        self.acks: list[int] = []
        self.relays = 0

    def respond(
        self, document: dict[str, Any], path: str, context: probe.RequestContext
    ) -> tuple[int, Any]:
        route = _route(path)
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return _not_found()
        messages = document.get("messages") or []
        # The current user turn: every message after the last assistant one.
        start = 0
        for index, message in enumerate(messages):
            if isinstance(message, dict) and message.get("role") == "assistant":
                start = index + 1
        last = json.dumps(messages[start:])
        with self._lock:
            self._requests += 1
            ordinal = self._requests
            pending = [
                block for block in _RELAY_RE.findall(last) if block not in self._relayed
            ]
            if pending and "Bash" in _tool_names(document):
                self._relayed.add(pending[-1])
                self.relays += 1
                command = json.loads('"' + pending[-1] + '"')
                content = [
                    {
                        "type": "tool_use",
                        "id": f"toolu_check_relay_{ordinal:04d}",
                        "name": "Bash",
                        "input": {"command": command, "description": "claude-multi lineup"},
                    }
                ]
            else:
                outputs = [int(value) for value in _LINEUP_OUT_RE.findall(last)]
                if outputs:
                    self.acks.append(max(outputs))
                    text = _ack(max(outputs)).decode("ascii")
                else:
                    text = "PROBE-OK"
                content = [{"type": "text", "text": text}]
        return 200, _messages_payload(content, document, f"msg_check_s4_{ordinal:04d}")


class S4ScopeSkillTests(_SpikeTestCase):
    """S4: ``/cm`` as an add-dir scope skill driven through the real TUI."""

    def _write_skills(self) -> None:
        skills = state.ensure_private_dir(self.scope / ".claude" / "skills")
        for name, body in _SKILL_BODIES.items():
            directory = state.ensure_private_dir(skills / name)
            text = _SKILL_FRONTMATTER.format(name=name, marker=_SKILL_DESC_MARKER) + body
            state.atomic_write(directory / "SKILL.md", text.encode("utf-8"))

    def _write_launcher(self) -> tuple[Path, Path]:
        bin_dir = state.ensure_private_dir(self.fixture.root / "bin")
        record = self.fixture.root / "launcher-argv.jsonl"
        body = (
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"path = {str(record)!r}\n"
            "try:\n"
            "    with open(path, encoding='utf-8') as handle:\n"
            "        count = sum(1 for line in handle if line.strip())\n"
            "except FileNotFoundError:\n"
            "    count = 0\n"
            "entry = {'argv': sys.argv[1:], "
            "'env_session': os.environ.get('CLAUDE_CODE_SESSION_ID')}\n"
            "with open(path, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(entry) + '\\n')\n"
            "print('CHECK-LINEUP-OUT-%04d' % (count + 1))\n"
        )
        probe.write_fixture_executable(self.fixture, bin_dir, "claude-multi", body)
        return bin_dir, record

    def _install_session_start_recorder(self) -> Path:
        events = self.fixture.root / "session-start.jsonl"
        recorder = self.fixture.root / "record-session-start.py"
        recorder.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "event = json.load(sys.stdin)\n"
            "kept = {k: event.get(k) for k in ('hook_event_name', 'session_id', 'source')}\n"
            f"with open({str(events)!r}, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(kept) + '\\n')\n"
            "print('{}')\n",
            encoding="utf-8",
        )
        recorder.chmod(0o700)
        hook = {"type": "command", "command": shlex.quote(str(recorder)), "timeout": 5}
        # The managed permission default rides along (2.1.284+
        # otherwise starts in auto mode).
        state.atomic_write(
            self.fixture.claude_config_dir / "settings.json",
            strict_json.canonical_file_bytes(
                {"hooks": {"SessionStart": [{"hooks": [hook]}]},
                 "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE}}
            ),
        )
        return events

    @staticmethod
    def _settle(record: Path, expected: int, seconds: float = 4.0) -> Any:
        """PTY callback: settle, then require exactly ``expected`` launcher calls.

        Used after a step that must NOT reach the launcher (no ACK barrier
        exists for it): a late call would show up as an extra record line.
        """

        def callback() -> None:
            time.sleep(seconds)
            lines = record.read_text().splitlines() if record.exists() else []
            count = sum(1 for line in lines if line.strip())
            if count != expected:
                raise RuntimeError(
                    f"launcher call count {count} != expected {expected}"
                )

        return callback

    def test_s4_cm_scope_skill_invocation(self) -> None:
        self._write_skills()
        bin_dir, record = self._write_launcher()
        events = self._install_session_start_recorder()
        # A file an unquoted `opus[1m]` glob matches (shell cwd = project).
        (self.fixture.project_dir / "cm-explorer=opus1:high").write_text("x")
        probe.seed_fixture_project_trust(self.fixture, self.fixture.project_dir)
        responder = _SkillResponder()
        step = probe.PTYInteraction
        interactions = (
            step(b"Choose", b"2\r"),
            step(b"Press", b"\r"),
            step("❯".encode("utf-8"), b"/cm profile balanced\r"),
            step(_ack(1), b"/cm set cm-x=a;b\r"),
            step(b"failed", b"/cm set cm-explorer=opus[1m]:high\r"),
            step(_ack(2), b"/cmq set cm-x=a;b\r"),
            step(_ack(3), b"/cmq set cm-explorer=opus[1m]:high\r"),
            step(_ack(4), b"/cmq set cm-x=$(printf CHECKSUBST)\r"),
            step(_ack(5), b"/cmq note a !b\r"),
            step(_ack(6), b"/cmq set cm-x=it's\r"),
            step(b"failed", b"/cmr profile balanced\r"),
            step(_ack(7), b"/cmr set cm-x=a;b\r"),
            # 2.1.286 draws the approval dialog's last line ("Esc to cancel
            # · Tab to amend") after "proceed"; an Esc sent before the dialog
            # takes input is lost, so wait for that line and settle first.
            step(b"amend", b"\x1b", before_send=lambda: time.sleep(0.6)),
            # Balanced apostrophes in the quoted body re-open
            # shell parsing (word split), and backticks run command
            # substitution. Barrier = the launcher-output ACK or a settle
            # wait that counts launcher calls (a refused call has no ACK).
            step(b"Interrupted", b"/cmq set a='b c'\r"),
            step(_ack(8), b"/cmq set a=`printf CHECKTICK`\r"),
            step(
                "❯".encode("utf-8"),
                b"/cm set a=`printf CHECKTICK`\r",
                before_send=self._settle(record, 8),
            ),
            step(
                _ack(9),
                b"/exit\r",
            ),
        )

        def run() -> probe.NativeRunResult:
            with probe.FakeAnthropicProvider(
                responder=responder,
                content_markers={"desc": _SKILL_DESC_MARKER},
            ) as provider:
                return probe.run_native_pty(
                    ("--add-dir", str(self.scope)),
                    interactions,
                    trusted=self.trusted,
                    fixture=self.fixture,
                    provider=provider,
                    timeout=120,
                    allow_real=True,
                    environ=self.environ,
                    path_dirs=(bin_dir,),
                )

        result = self._guard_live_domain("S4", run)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("--dangerously-skip-permissions", result.argv)

        starts = [json.loads(line) for line in events.read_text().splitlines() if line]
        self.assertEqual([event.get("source") for event in starts], ["startup"])
        session_id = starts[0].get("session_id")
        self.assertIsInstance(session_id, str)
        self.assertRegex(session_id, _UUID_RE)
        entries = [json.loads(line) for line in record.read_text().splitlines() if line]
        prefix = ["lineup", "--session", session_id]
        expected = [
            # SPEC body, unquoted: word-split AND glob-expanded by bash.
            prefix + ["profile", "balanced"],
            prefix + ["set", "cm-explorer=opus1:high"],
            # Single-quoted body: one argument, exact bytes for ; [1m] $().
            prefix + ["set cm-x=a;b"],
            prefix + ["set cm-explorer=opus[1m]:high"],
            prefix + ["set cm-x=$(printf CHECKSUBST)"],
            # The client escapes a word-leading `!` before substitution.
            prefix + ["note a \\!b"],
            # Model-relayed Bash: allowed by the skill's allowed-tools.
            prefix + ["profile", "balanced"],
            # Quoted body with BALANCED apostrophes: the user's quotes close
            # and re-open the body's quoting, so the argument word-splits
            # (and would glob) into several argv elements.
            prefix + ["set a=b", "c"],
            # Unquoted body with a backtick: command substitution runs in
            # the skill shell (printf is not on the fixture PATH, so it
            # substitutes nothing) and the words split.
            prefix + ["set", "a="],
        ]
        # `;` in the unquoted body is refused by the dynamic-context
        # permission check, a lone `'` breaks the quoted body's shell eval,
        # a backtick inside the quoted body breaks it too (the substitution
        # is unterminated once the quoting re-opens), and the relayed `a;b`
        # stops at a permission prompt (cancelled): none of them reaches the
        # launcher.
        self.assertEqual([entry["argv"] for entry in entries], expected)
        self.assertEqual({entry["env_session"] for entry in entries}, {session_id})
        self.assertEqual(sorted(set(responder.acks)), [1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(responder.relays, 2)
        # disable-model-invocation: the skill is never listed to the model.
        self.assertFalse(
            any("desc" in record_.markers_found for record_ in result.requests)
        )
        # Design consequence: no body quoting makes the launcher argv exact
        # for every input; the launcher must refuse unless exactly ONE
        # argument follows `--session <id>` (the single-quoted body yields
        # one only when the user text has no apostrophe or backtick).
        tails = [entry["argv"][len(prefix):] for entry in entries]
        split = sum(1 for tail in tails if len(tail) != 1)
        print(
            "client check S4: PASS skill invocable; dynamic-context !`cmd` runs the "
            "launcher at expansion with no permission prompt (output then "
            "reaches the lead as one user turn); ${CLAUDE_SESSION_ID} and "
            "CLAUDE_CODE_SESSION_ID == SessionStart session_id; SPEC unquoted "
            "$ARGUMENTS word-splits+globs (opus[1m]->opus1), ';' is refused, "
            "backtick substitutes (a=`printf ..` -> [set, a=]); quoted "
            "'$ARGUMENTS' is one exact arg for ; [1m] $() but NOT safe: "
            "balanced apostrophes word-split (a='b c' -> [set a=b, c]), a lone "
            "' or a backtick fails closed; leading ! arrives as \\!; "
            f"launcher calls={len(tails)} multi-arg={split} -> launcher must "
            "refuse unless exactly one arg follows --session <id>; "
            f"model-relayed Bash prompts on ';' {self.binary_label}"
        )


# ------------------------------------------------------------------ S10


_LEAD = "client-check-lead"
_AGENT_MODEL = "spike-model-w"
_SUB_MODEL = "spike-sub-w"
_SUB_OUTSIDE = "spike-out-w"
_AGENT_W = """---
name: cm-spike-w
description: Synthetic client check workflow agent (frontmatter model only).
model: spike-model-w
---
You are the cm-spike-w fixture agent.
"""
_AGENT_WI = """---
name: cm-spike-wi
description: Synthetic client check workflow agent (frontmatter isolation).
model: spike-model-w
isolation: worktree
---
You are the cm-spike-wi fixture agent.
"""
# Per-agent prompt markers let the responder attribute each request to the
# agent() call that produced it; only labels are retained.
_WF_CALLS = {
    "default": "{label: 'wa'}",
    "typed": "{label: 'wb', agentType: 'cm-spike-w'}",
    "typed-isolated": "{label: 'wc', agentType: 'cm-spike-w', isolation: 'worktree'}",
    "frontmatter-isolation": "{label: 'wd', agentType: 'cm-spike-wi'}",
}
_WF_MARKERS = {
    name: f"CHECK-WF-{index:02d}-{name.upper()}"
    for index, name in enumerate(_WF_CALLS, start=1)
}
_PWD_RE = re.compile(r"Primary working directory: ([^\\\n\"]+)")


def _workflow_script(calls: tuple[str, ...]) -> str:
    lines = ["export const meta = { name: 'client-check-s10', description: 'client check S10' }"]
    names = []
    for index, name in enumerate(calls):
        lines.append(
            f"const r{index} = await agent('Reply {_WF_MARKERS[name]}', {_WF_CALLS[name]})"
        )
        names.append(f"r{index}")
    lines.append(f"return [{', '.join(names)}]")
    return "\n".join(lines) + "\n"


class _WorkflowResponder:
    """Force one Workflow tool_use; label each workflow-agent request.

    The first request offering the ``Workflow`` tool gets a tool_use running
    the given script. A request that does not offer ``Workflow`` and carries
    exactly one agent prompt marker is a workflow-agent request: its wire
    model, gateway agent-type hint and a cwd label (``project`` /
    ``worktree`` / ``other`` / ``absent``, derived in memory from the
    request's environment block) are kept. Everything else gets text.
    """

    def __init__(self, script: str, project: Path) -> None:
        self._script = script
        self._project = str(project)
        self._lock = threading.Lock()
        self._forced = False
        self._requests = 0
        self.workflow_offered = False
        self.agents: dict[str, dict[str, Any]] = {}
        self.completion_seen = False

    def _cwd_label(self, text: str) -> str:
        labels = set()
        for path in _PWD_RE.findall(text):
            if path == self._project:
                labels.add("project")
            elif path.startswith(self._project + "/.claude/worktrees/"):
                labels.add("worktree")
            else:
                labels.add("other")
        if not labels:
            return "absent"
        return "+".join(sorted(labels))

    def respond(
        self, document: dict[str, Any], path: str, context: probe.RequestContext
    ) -> tuple[int, Any]:
        route = _route(path)
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return _not_found()
        tools = _tool_names(document)
        text = json.dumps(document.get("system")) + json.dumps(document.get("messages"))
        found = [name for name, marker in _WF_MARKERS.items() if marker in text]
        with self._lock:
            self._requests += 1
            ordinal = self._requests
            if "Workflow" in tools:
                self.workflow_offered = True
                if not self._forced:
                    self._forced = True
                    content = [
                        {
                            "type": "tool_use",
                            "id": "toolu_check_workflow_0001",
                            "name": "Workflow",
                            "input": {"script": self._script},
                        }
                    ]
                    return 200, _messages_payload(
                        content, document, f"msg_check_s10_{ordinal:04d}"
                    )
                if len(found) > 1:
                    self.completion_seen = True
            elif len(found) == 1 and found[0] not in self.agents:
                hints = dict(context.record.hint_header_values)
                self.agents[found[0]] = {
                    "model": context.record.model,
                    "agent_type": hints.get("x-claude-code-agent-type"),
                    "request_class": hints.get("x-claude-code-request-class"),
                    "cwd": self._cwd_label(text),
                    "worktree_note": "You are running in an isolated git worktree" in text,
                }
        content = [{"type": "text", "text": "PROBE-OK"}]
        return 200, _messages_payload(content, document, f"msg_check_s10_{ordinal:04d}")


class S10WorkflowAgentTests(_SpikeTestCase):
    """S10: Workflow ``agent()`` type, wire model and cwd."""

    def setUp(self) -> None:
        super().setUp()
        agents = state.ensure_private_dir(self.scope / ".claude" / "agents")
        state.atomic_write(agents / "cm-spike-w.md", _AGENT_W.encode("utf-8"))
        state.atomic_write(agents / "cm-spike-wi.md", _AGENT_WI.encode("utf-8"))
        probe.seed_fixture_project_trust(self.fixture, self.fixture.project_dir)
        probe.seed_fixture_project_trust(self.fixture, self.scope)
        self.git_env = {
            "HOME": str(self.fixture.home),
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "LANG": "C.UTF-8",
        }
        for command in (
            ["git", "init", "-q", "-b", "main"],
            [
                "git", "-c", "user.name=client-check", "-c", "user.email=spike@invalid",
                "commit", "-q", "--allow-empty", "-m", "fixture",
            ],
        ):
            subprocess.run(
                command,
                cwd=self.fixture.project_dir,
                env=self.git_env,
                check=True,
                capture_output=True,
                timeout=30,
            )

    def _settings(self, name: str, extra: dict[str, Any] | None = None) -> Path:
        document: dict[str, Any] = {
            "availableModels": sorted([_LEAD, _AGENT_MODEL, _SUB_MODEL]),
            "model": _LEAD,
        }
        document.update(extra or {})
        path = self.scope / f"settings-{name}.json"
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        return path

    def _run_workflow(
        self, settings: Path, calls: tuple[str, ...]
    ) -> tuple[_WorkflowResponder, probe.NativeRunResult]:
        responder = _WorkflowResponder(_workflow_script(calls), self.fixture.project_dir)

        def run() -> probe.NativeRunResult:
            with probe.FakeAnthropicProvider(responder=responder) as provider:
                return probe.run_native(
                    (
                        "-p",
                        "client check S10",
                        "--settings",
                        str(settings),
                        "--add-dir",
                        str(self.scope),
                        "--dangerously-skip-permissions",
                    ),
                    trusted=self.trusted,
                    fixture=self.fixture,
                    provider=provider,
                    timeout=150,
                    allow_real=True,
                    environ=self.environ,
                    child_env={"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"},
                )

        result = self._guard_live_domain("S10", run)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(responder.workflow_offered, "Workflow tool not offered")
        self.assertEqual(
            sorted(responder.agents), sorted(calls), "workflow agents missing"
        )
        self.assertTrue(responder.completion_seen, "workflow completion not relayed")
        return responder, result

    def _worktree_count(self) -> int:
        listing = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=self.fixture.project_dir,
            env=self.git_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        return sum(1 for line in listing.splitlines() if line.startswith("worktree "))

    def test_s10_workflow_agent_type_model_and_cwd(self) -> None:
        # (1) Production-shaped fence, no workflow default binding. The
        # Workflow tool is on by default: no enable key is set anywhere.
        base, _ = self._run_workflow(self._settings("base"), tuple(_WF_CALLS))
        self.assertEqual(
            base.agents["default"],
            {
                "model": _LEAD,
                "agent_type": "workflow-subagent",
                "request_class": "workflow",
                "cwd": "project",
                "worktree_note": False,
            },
        )
        self.assertEqual(
            base.agents["typed"],
            {
                "model": _AGENT_MODEL,
                "agent_type": "custom",
                "request_class": "workflow",
                "cwd": "project",
                "worktree_note": False,
            },
        )
        self.assertEqual(
            base.agents["typed-isolated"],
            {
                "model": _AGENT_MODEL,
                "agent_type": "custom",
                "request_class": "workflow",
                "cwd": "worktree",
                "worktree_note": True,
            },
        )
        # C22: the definition's `isolation: worktree` is never read.
        self.assertEqual(base.agents["frontmatter-isolation"]["cwd"], "project")
        self.assertFalse(base.agents["frontmatter-isolation"]["worktree_note"])
        self.assertEqual(base.agents["frontmatter-isolation"]["model"], _AGENT_MODEL)
        # The unchanged isolated worktree is auto-removed after the run.
        self.assertEqual(self._worktree_count(), 1)

        # (2) Workflow default binding compiled into settings.env: applies
        # only to agents without a frontmatter model.
        sub, _ = self._run_workflow(
            self._settings("sub", {"env": {"CLAUDE_CODE_SUBAGENT_MODEL": _SUB_MODEL}}),
            ("default", "typed"),
        )
        self.assertEqual(sub.agents["default"]["model"], _SUB_MODEL)
        self.assertEqual(sub.agents["default"]["agent_type"], "workflow-subagent")
        self.assertEqual(sub.agents["typed"]["model"], _AGENT_MODEL)

        # (3) A default binding outside availableModels silently falls back
        # to the lead: the compiled selector must be inside the fence.
        out, _ = self._run_workflow(
            self._settings("out", {"env": {"CLAUDE_CODE_SUBAGENT_MODEL": _SUB_OUTSIDE}}),
            ("default", "typed"),
        )
        self.assertEqual(out.agents["default"]["model"], _LEAD)
        self.assertEqual(out.agents["typed"]["model"], _AGENT_MODEL)

        print(
            "client check S10: PASS Workflow on by default; agent() w/o agentType="
            "workflow-subagent on lead (settings.env SUBAGENT_MODEL in fence "
            "-> that model; outside fence -> lead); agentType cm-*=frontmatter "
            "model (SUBAGENT_MODEL ignored); cwd project w/o isolation, "
            "worktree with per-call isolation:'worktree' (auto-removed); "
            "frontmatter isolation ignored (C22) "
            f"{self.binary_label}"
        )

    def test_s10_workflow_gate_keys(self) -> None:
        """Pins the keys that switch the Workflow tool off (static:
        ``disableWorkflows``/``enableWorkflows`` settings,
        ``CLAUDE_CODE_WORKFLOWS``/``CLAUDE_CODE_DISABLE_WORKFLOWS`` env)."""

        cases = (
            ("default", {}, {}, True),
            ("disable-setting", {"disableWorkflows": True}, {}, False),
            ("enable-false", {"enableWorkflows": False}, {}, False),
            ("env-workflows-0", {}, {"CLAUDE_CODE_WORKFLOWS": "0"}, False),
            ("env-disable-1", {}, {"CLAUDE_CODE_DISABLE_WORKFLOWS": "1"}, False),
        )
        observed = {}
        for name, extra, child_env, _expected in cases:
            settings = self._settings(f"gate-{name}", extra)

            def run(
                settings: Path = settings, child_env: dict[str, str] = child_env
            ) -> probe.NativeRunResult:
                with probe.FakeAnthropicProvider() as provider:
                    return probe.run_native(
                        ("-p", "client check S10 gate", "--settings", str(settings)),
                        trusted=self.trusted,
                        fixture=self.fixture,
                        provider=provider,
                        timeout=90,
                        allow_real=True,
                        environ=self.environ,
                        child_env=child_env,
                    )

            result = self._guard_live_domain("S10", run)
            self.assertFalse(result.timed_out)
            self.assertEqual(result.returncode, 0)
            main = [record for record in result.requests if "Agent" in record.tool_names]
            self.assertTrue(main, f"{name}: no main-loop request")
            observed[name] = any("Workflow" in record.tool_names for record in main)
        self.assertEqual(observed, {name: expected for name, _, _, expected in cases})


# ------------------------------------------------------------------ S13


# A read-only agent must not reach an
# agent-spawning skill. Two fixture skills: a `context: fork` skill (the
# agent-spawning vector, like the bundled `code-review`) and an inline one
# (spawns no agent). `$ARGUMENTS` carries the invoker's spawn marker into
# the fork, so every fork is attributed to the agent that invoked it.
_SK_FORK = "check-sk-fork"
_SK_INLINE = "check-sk-inline"
_SK_FORK_BODY = "CHECK-SK-FORK-BODY"
_SK_INLINE_BODY = "CHECK-SK-INLINE-BODY"
_SK_SKILLS = {
    _SK_FORK: ("context: fork\n", _SK_FORK_BODY),
    _SK_INLINE: ("", _SK_INLINE_BODY),
}
_SK_PROMPT = "client check S13"
_SK_LEAD = "check-sk-lead"
_SK_AGENT_MODEL = "check-sk-agent"
_SK_MARKER_RE = re.compile(r"CHECK-SK-AGENT-\d{2}X")
# The earlier read-only policy (roles.json and READ_ONLY_TOOLS):
# the negative control compiles it to show the fork it let through.
_PRE_L10C_READ_ONLY_TOOLS = ("Edit", "Write", "NotebookEdit", "Agent")
_READ_ONLY_ROLE_IDS = ("cm-explorer", "cm-reviewer", "cm-reviewer-strong")
_SHIPPED_ROOT = RESOURCES_ROOT


def _sk_skill_text(name: str, extra: str, body: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: client check S13 fixture skill {name}\n"
        f"{extra}"
        "---\n"
        f"{body} $ARGUMENTS Reply PROBE-OK.\n"
    )


def _sk_binding(slot: str) -> profile.ResolvedBinding:
    return profile.ResolvedBinding(
        slot=slot, requested="check-sk", key="check-sk", chain=("check-sk",), named=None,
        provider="check-sk", family="check-sk", display="client check", generation="1",
        mode="client", effort="high", selector=_SK_AGENT_MODEL, proxy_contract=None,
        default_effort="high", client_context_tokens=200000,
        provider_context_tokens=200000,
    )


class _SkillPolicyResponder:
    """Force a spawn per agent, then one Skill call per agent; attribute forks.

    The lead's first request (its first message carries ``_SK_PROMPT``) gets
    one ``Agent`` tool_use per planned spawn, in parallel. A request whose
    first message carries a spawn marker is that agent's: its first request
    records whether the ``Skill`` tool and the bundled ``code-review`` skill
    are offered and gets a forced ``Skill`` tool_use (``args`` = its marker);
    a later one records whether the call was refused (an ``is_error``
    tool_result for that call) and whether the inline skill body arrived. A
    request whose first message carries the fork skill body is a fork and is
    counted against the marker it carries. Only booleans and counters are
    kept.
    """

    def __init__(self, spawns: dict[str, tuple[str, str]]) -> None:
        self._spawns = spawns  # marker -> (agent type, skill it invokes)
        self._lock = threading.Lock()
        self._spawned = False
        self.observed: dict[str, dict[str, Any]] = {
            agent_type: {
                "skill_offered": None,
                "code_review_listed": None,
                "skill_refused": False,
                "inline_body_seen": False,
                "forks": 0,
            }
            for agent_type, _ in spawns.values()
        }
        self.unattributed_forks = 0

    @staticmethod
    def _call_id(marker: str) -> str:
        return "toolu_check_sk_skill_" + marker[len("CHECK-SK-AGENT-"):-1]

    def respond(
        self, document: dict[str, Any], path: str, context: probe.RequestContext
    ) -> tuple[int, Any]:
        route = _route(path)
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return _not_found()
        messages = document.get("messages") or []
        first = json.dumps(messages[0]) if messages else ""
        whole = json.dumps(document.get("system")) + json.dumps(messages)
        tools = _tool_names(document)
        content: list[dict[str, Any]] = [{"type": "text", "text": "PROBE-OK"}]
        with self._lock:
            if _SK_FORK_BODY in first:
                markers = set(_SK_MARKER_RE.findall(first))
                if len(markers) == 1 and next(iter(markers)) in self._spawns:
                    agent_type = self._spawns[next(iter(markers))][0]
                    self.observed[agent_type]["forks"] += 1
                else:
                    self.unattributed_forks += 1
            elif _SK_PROMPT in first:
                if "Agent" in tools and not self._spawned:
                    self._spawned = True
                    content = [
                        {
                            "type": "tool_use",
                            "id": f"toolu_check_sk_spawn_{index:02d}",
                            "name": "Agent",
                            "input": {
                                "description": "client check S13",
                                "subagent_type": agent_type,
                                "prompt": f"Reply {marker}.",
                            },
                        }
                        for index, (marker, (agent_type, _)) in enumerate(
                            self._spawns.items()
                        )
                    ]
            else:
                marker = next((m for m in self._spawns if m in first), None)
                if marker is not None:
                    agent_type, skill = self._spawns[marker]
                    seen = self.observed[agent_type]
                    if seen["skill_offered"] is None:
                        seen["skill_offered"] = "Skill" in tools
                        seen["code_review_listed"] = "code-review" in whole
                        content = [
                            {
                                "type": "tool_use",
                                "id": self._call_id(marker),
                                "name": "Skill",
                                "input": {"skill": skill, "args": marker},
                            }
                        ]
                    else:
                        call_id = self._call_id(marker)
                        for message in messages:
                            blocks = message.get("content") if isinstance(message, dict) else None
                            for block in blocks if isinstance(blocks, list) else []:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "tool_result"
                                    and block.get("tool_use_id") == call_id
                                    and block.get("is_error") is True
                                ):
                                    seen["skill_refused"] = True
                        if _SK_INLINE_BODY in whole:
                            seen["inline_body_seen"] = True
        return 200, _messages_payload(
            content, document, f"msg_check_s13_{context.ordinal:04d}"
        )


class S13SkillSpawnPolicyTests(_SpikeTestCase):
    """S13: the read-only roles' tool policy blocks the skill vector.

    Agents are compiled by the production agent-file compiler from the
    SHIPPED roles and prompts (synthetic binding, no shipped model id).
    """

    def setUp(self) -> None:
        super().setUp()
        self.catalog = catalog.load_catalog(_SHIPPED_ROOT)
        self.agents_dir = state.ensure_private_dir(self.scope / ".claude" / "agents")
        skills = state.ensure_private_dir(self.scope / ".claude" / "skills")
        for name, (extra, body) in _SK_SKILLS.items():
            directory = state.ensure_private_dir(skills / name)
            state.atomic_write(
                directory / "SKILL.md", _sk_skill_text(name, extra, body).encode("utf-8")
            )
        probe.seed_fixture_project_trust(self.fixture, self.fixture.project_dir)
        probe.seed_fixture_project_trust(self.fixture, self.scope)
        self.settings = self.scope / "settings-s13.json"
        state.atomic_write(
            self.settings, strict_json.canonical_file_bytes({"model": _SK_LEAD})
        )

    def _compile_agent(
        self, role_id: str, *, name: str | None = None,
        disallowed_tools: tuple[str, ...] | None = None,
    ) -> str:
        """Write one agent file through ``scope._agent_file_v2_bytes``."""

        role = self.catalog.roles_v2[role_id]
        spec = profile.RoleSpec(
            id=name or role_id,
            function=role["function"],
            grade=role["grade"],
            prompt_file=role["prompt_file"],
            isolation=role["isolation"],
            disallowed_tools=tuple(role["disallowed_tools"]),
            description=role["description"],
            requires=tuple(role["requires"]),
        )
        if disallowed_tools is not None:
            spec = dataclasses.replace(spec, disallowed_tools=disallowed_tools)
        bodies = dict(self.catalog.prompt_bodies)
        bodies[spec.id] = self.catalog.prompt_bodies[role_id]
        data = scope._agent_file_v2_bytes(
            profile.ResolvedAgent(_sk_binding(spec.id), spec, True), bodies
        )
        state.atomic_write(self.agents_dir / f"{spec.id}.md", data)
        return spec.id

    def _run(self, plan: list[tuple[str, str]]) -> _SkillPolicyResponder:
        spawns = {
            f"CHECK-SK-AGENT-{index:02d}X": entry for index, entry in enumerate(plan, 1)
        }
        responder = _SkillPolicyResponder(spawns)

        def run() -> probe.NativeRunResult:
            with probe.FakeAnthropicProvider(responder=responder) as provider:
                return probe.run_native(
                    (
                        "-p",
                        _SK_PROMPT,
                        "--settings",
                        str(self.settings),
                        "--add-dir",
                        str(self.scope),
                        "--dangerously-skip-permissions",
                    ),
                    trusted=self.trusted,
                    fixture=self.fixture,
                    provider=provider,
                    timeout=180,
                    allow_real=True,
                    environ=self.environ,
                )

        result = self._guard_live_domain("S13", run)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)
        for agent_type, seen in responder.observed.items():
            self.assertIsNotNone(seen["skill_offered"], f"{agent_type} was not spawned")
        self.assertEqual(responder.unattributed_forks, 0)
        return responder

    def test_s13_read_only_roles_cannot_reach_agent_spawning_skills(self) -> None:
        # The chosen policy: `Skill` is a read-only disallowed tool.
        for role_id in _READ_ONLY_ROLE_IDS:
            self.assertIn("Skill", self.catalog.roles_v2[role_id]["disallowed_tools"])
        shipped = [(self._compile_agent(rid), _SK_FORK) for rid in _READ_ONLY_ROLE_IDS]
        # (b) positive control: a non-read-only role keeps Skill, and a skill
        # that spawns no agent works.
        shipped.append((self._compile_agent("cm-analyst"), _SK_INLINE))
        policy = self._run(shipped)

        # (c) negative control: the earlier read-only policy lets the fork
        # happen for every read-only role. The per-skill variant appends a
        # frontmatter `Skill(<fork skill>)` rule to the earlier list and
        # calls the INLINE skill: it shows whether the client honours a
        # per-skill rule in agent frontmatter.
        controls = [
            (
                self._compile_agent(
                    rid, name=f"check-pre-{rid[3:]}",
                    disallowed_tools=_PRE_L10C_READ_ONLY_TOOLS,
                ),
                _SK_FORK,
            )
            for rid in _READ_ONLY_ROLE_IDS
        ]
        per_skill = self._compile_agent(
            "cm-reviewer", name="check-per-skill-reviewer",
            disallowed_tools=_PRE_L10C_READ_ONLY_TOOLS + (f"Skill({_SK_FORK})",),
        )
        controls.append((per_skill, _SK_INLINE))
        control = self._run(controls)

        # (a) every read-only role: no Skill tool, no skill listing (the
        # bundled agent-spawning `code-review` is absent), a forced call to
        # the fork skill is refused, and nothing forks.
        blocked = {
            "skill_offered": False,
            "code_review_listed": False,
            "skill_refused": True,
            "inline_body_seen": False,
            "forks": 0,
        }
        self.assertEqual(
            {rid: policy.observed[rid] for rid in _READ_ONLY_ROLE_IDS},
            {rid: blocked for rid in _READ_ONLY_ROLE_IDS},
        )
        self.assertEqual(
            policy.observed["cm-analyst"],
            {
                "skill_offered": True,
                "code_review_listed": True,
                "skill_refused": False,
                "inline_body_seen": True,
                "forks": 0,
            },
        )
        forked = {
            "skill_offered": True,
            "code_review_listed": True,
            "skill_refused": False,
            "inline_body_seen": False,
            "forks": 1,
        }
        self.assertEqual(
            {rid: control.observed[f"check-pre-{rid[3:]}"] for rid in _READ_ONLY_ROLE_IDS},
            {rid: forked for rid in _READ_ONLY_ROLE_IDS},
        )
        # A frontmatter `Skill(<name>)` entry is not per-skill at the pinned
        # client: it removes the whole Skill tool, so the inline skill is
        # refused as well (the client's agent tool filter keys on the tool
        # name). That is why the policy denies `Skill`, not a skill name.
        self.assertEqual(control.observed[per_skill], blocked)

        print(
            "client check S13: PASS read-only roles (explorer, reviewer, "
            "reviewer-strong) compile disallowedTools with Skill: no Skill tool, "
            "no skill listing (code-review absent), a forced fork-skill call is "
            "refused, 0 forks; analyst keeps Skill and an inline skill works "
            "(positive control); the pre-L10c read-only policy forks 3/3 "
            "(negative control); a frontmatter Skill(<name>) rule drops the whole "
            f"Skill tool (not per-skill) {self.binary_label}"
        )


if __name__ == "__main__":
    unittest.main()
