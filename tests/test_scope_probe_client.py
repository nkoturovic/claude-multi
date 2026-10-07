"""Bounded, network-hermetic observations of the real pinned client.

Only synthetic selectors and fixture homes. No request/response or transcript
text is retained: responders reduce it to counts, flags and safe identifiers.
Diagnostics are observations, not assertions of a previously seen class;
only the correctness-essential classes are pinned. Remote feature flags are
blocked by netns.
"""
from __future__ import annotations

import copy
import io
import itertools
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from claude_multi import catalog, launch, probe, profile, scope, settings, state, strict_json, upgrade
from _catalog import FIXTURE_ROOT
from _layout import PROBE_BASELINES, RESOURCES_ROOT
from _tier import real_binary_gate

_CONTRACT = RESOURCES_ROOT / "catalog/native-contract.json"
# The recorded probe classes (a neutral fixture).
_README = PROBE_BASELINES
_PROMPT = "❯".encode()
_LEAD = "probe-lead"
_AGENT = "probe-agent"
_DIAGNOSTICS = frozenset({"U1", "X5", "RL", "R19", "RET", "SC", "SX", "SF", "SK", "OC", "FL", "SL"})
# FD (feedback drafts) accepts the full lead-and-subagent proof or the explicit client
# boundary (off suppresses both; notify restores the lead; the client never
# offers the tool to a subagent); see _feedback_class.
_ESSENTIAL_CLASSES = {"CE": ("CE-A",), "DAH": ("DAH-override",),
                      "FD": ("FD-env-off", "FD-env-off-subagent-withheld"), "CC": ("CC-bash-child",),
                      "XC": ("XC-exact",)}


def _baseline(probe_id, text):
    match = re.search(rf"client probe {re.escape(probe_id)}: \w+ class=([\w-]+)", text)
    return match.group(1) if match else None


def _observation_detail(probe_id, observed, detail, text):
    expected = _baseline(probe_id, text)
    if probe_id in _DIAGNOSTICS:
        if expected is None:
            detail += " baseline=unrecorded"
        elif expected != observed:
            detail += f" changed recorded={expected}"
    return detail


# Every reply carries its own message id: the pinned client merges assistant
# messages that share one, so a reused id collapses the conversation (the
# history a compaction probe needs never grows).
_MESSAGE_IDS = itertools.count(1)


def _reply(document, content=None, *, usage=1, stop="end_turn"):
    payload = probe._message_payload(
        content or [{"type": "text", "text": "PROBE-OK"}],
        model=document.get("model"), stop_reason=stop,
        message_id=f"msg_probe_{next(_MESSAGE_IDS):06d}", input_tokens=usage,
    )
    return 200, probe._sse_message(payload) if document.get("stream") else payload


def _tool(name, number, **arguments):
    return {"type": "tool_use", "id": f"toolu_probe_{number}", "name": name,
            "input": arguments}


def _spawn(number, agent=_AGENT, **extra):
    return _tool("Agent", number, description="client probe", subagent_type=agent,
                 prompt="Reply PROBE-OK", run_in_background=False, **extra)


def _results(document):
    # In-memory only, never persisted or included in assertion messages.
    return [block for message in document.get("messages", [])
            if isinstance(message, dict) and isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"]


def _retry_class(observations):
    if not all(row["rejections"] for row in observations.values()):
        return "unavailable"
    delays = [event["retry_delay_ms"] for row in observations.values()
              for event in row["retry_events"] if "retry_delay_ms" in event]
    if any(0 <= delay <= 300000 for delay in delays) or any(
            row["rejections"] > 1 for row in observations.values()):
        return "X5-capped"
    lead, child = observations["W1"], observations["W5"]
    # W1's api_retry measures the delay, not visibility to a blocked lead.
    if child["retry_events"] or child["lead_after"] or child["hook_events"]:
        return "X5-visible"
    if lead["timeout"] and any(3500000 <= event.get("retry_delay_ms", -1) <= 3700000
                               for event in lead["retry_events"]):
        return "X5-silent-hours"
    return "unavailable"


def _identity_class(observations):
    hook = all(observations[key] and all(
        row.get("has_execpath") and row.get("execpath_same_inode") is True
        for row in observations[key]) for key in ("a", "b"))
    alternate = observations["c"]
    if isinstance(alternate, list):
        # Missing/unreadable is unknown, not proof of a non-pinned client.
        hook = hook and bool(alternate) and all(
            row.get("has_execpath") and row.get("execpath_same_inode") is False
            for row in alternate)
    proc_signal = any(True in row.get("proc_matches", []) for row in observations["d"])
    return "U1-hook" if hook else "U1-proc" if proc_signal else "U1-none"


def _exhaustion_class(observations):
    recovered = any(
        (observations[case]["summary_requests"] or observations[case]["compact_events"])
        and observations[case]["completed_result"] for case in ("E1", "E2")
    )
    continuation = observations["E3"]
    if continuation["failed_result"]:
        recovered = recovered and (
            continuation["resume_requests"] > 0 and continuation["resume_rejections"] == 0
            and continuation["resume_completed"]
        )
    return "SX-closed" if recovered else "SX-open-continue-dies"


def _exhaustion_window_class(small, large):
    # A completed 1M agent outliving the exhausted 200K agent is evidence
    # of its own window even when neither emits a compact hook.
    if (small["failed_result"] or small["compact_events"] > large["compact_events"]) and (
            not large["failed_result"] and large["child_requests"] > small["child_requests"]):
        return "E4-own-window"
    keys = ("failed_result", "child_requests", "compact_events")
    if all(small[key] == large[key] for key in keys):
        return "E4-no-observed-difference"
    return "E4-inconclusive"


class _Inconclusive(probe.ProbeError):
    """An invalid control prevents selecting an outcome class."""


class _Recorder:
    def __init__(self):
        self.rows = []
        self.lock = threading.Lock()

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        with self.lock:
            record = context.record
            hints = dict(record.hint_header_values)
            self.rows.append({
                "model": record.model, "request_class": hints.get("x-claude-code-request-class"),
                "tool_count": record.tool_count, "max_tokens": record.max_tokens,
                "effort": record.effort, "thinking": record.thinking_type,
                "agent_offered": "Agent" in record.tool_names,
            })
            return self.message(document, record, hints)

    def message(self, document, record, hints):
        return _reply(document)


class ObservationTests(unittest.TestCase):
    def test_diagnostic_changes_are_passing_data(self):
        self.assertEqual(_DIAGNOSTICS, {"U1", "X5", "RL", "R19", "RET", "SC", "SX", "SF", "SK", "OC", "FL", "SL"})
        for name in _DIAGNOSTICS:
            text = f"client probe {name}: PASS class=old previous observation"
            self.assertIn("changed recorded=old",
                          _observation_detail(name, "new", "observed", text))
            self.assertNotIn("changed", _observation_detail(name, "old", "observed", text))
        self.assertIsNone(_baseline("CE", "no G-P decision yet"))

    def test_retry_classes_use_delay_and_child_visibility(self):
        quiet = {"timeout": True, "rejections": 1, "retry_events": [],
                 "lead_after": 0, "hook_events": 0}
        observed = {"W1": copy.deepcopy(quiet), "W5": copy.deepcopy(quiet)}
        observed["W1"]["retry_events"] = [{"retry_delay_ms": 3600000}]
        self.assertEqual(_retry_class(observed), "X5-silent-hours")
        for key, value in (("retry_events", [{"retry_delay_ms": 3600000}]),
                           ("lead_after", 1), ("hook_events", 1)):
            with self.subTest(key=key):
                changed = copy.deepcopy(observed)
                changed["W5"][key] = value
                self.assertEqual(_retry_class(changed), "X5-visible")
        for case in ("W1", "W5"):
            for delay, expected in ((300000, "X5-capped"), (0, "X5-capped")):
                changed = copy.deepcopy(observed)
                changed[case]["retry_events"] = [{"retry_delay_ms": delay}]
                self.assertEqual(_retry_class(changed), expected)
            changed = copy.deepcopy(observed)
            changed[case]["rejections"] = 2
            self.assertEqual(_retry_class(changed), "X5-capped")
        for events in ([], [{"retry_delay_ms": 300001}]):
            changed = copy.deepcopy(observed)
            changed["W1"]["retry_events"] = events
            self.assertEqual(_retry_class(changed), "unavailable")
        observed["W1"]["timeout"] = False
        self.assertEqual(_retry_class(observed), "unavailable")
        observed["W5"]["rejections"] = 0
        self.assertEqual(_retry_class(observed), "unavailable")

    def test_hook_identity_requires_execpath_and_alternate_discrimination(self):
        matching = {"has_execpath": True, "execpath_same_inode": True}
        different = {"has_execpath": True, "execpath_same_inode": False}
        observed = {"a": [matching], "b": [matching], "c": [different], "d": []}
        self.assertEqual(_identity_class(observed), "U1-hook")
        for alternate in ([matching], [], [{"has_execpath": False}],
                          [{"has_execpath": True, "execpath_same_inode": None}]):
            self.assertEqual(_identity_class(dict(observed, c=alternate)), "U1-none")
        self.assertEqual(_identity_class(dict(observed, c={"boundary": "no alternate"})), "U1-hook")
        pid_only = {"has_claude_pid": True, "pid_exe_same_inode": True}
        for key in ("a", "b"):
            changed = dict(observed, **{key: [pid_only]})
            self.assertEqual(_identity_class(changed), "U1-none")
            changed["d"] = [{"proc_matches": [True]}]
            self.assertEqual(_identity_class(changed), "U1-proc")

    def test_compaction_ignored_flag_and_inconclusive_controls(self):
        observed = {"none": (1000000, None), "F": (1000000, None)}
        triggers = {90: False, 95: False, "P+U+Pj95+F90": False}
        self.assertEqual(CompactionEnvironmentProbe._classify(observed, triggers), "CE-C")
        for window in (300000, 400000, 500000, 600000):
            with self.assertRaises(_Inconclusive):
                CompactionEnvironmentProbe._classify(dict(observed, none=(window, None)), triggers)
        with self.assertRaises(_Inconclusive):
            CompactionEnvironmentProbe._classify(observed, triggers | {95: True})
        observed.update({case: (600000, 33000) for case in ("F", "P+F", "U+F", "P+Pj+F")})
        triggers[90] = True
        self.assertEqual(CompactionEnvironmentProbe._classify(observed, triggers), "CE-B")
        triggers[90] = triggers["P+U+Pj95+F90"] = True
        self.assertEqual(CompactionEnvironmentProbe._classify(observed, triggers), "CE-A")

    def test_compaction_contradictions_fail(self):
        matrix = {case: (600000, 33000) for case in ("F", "P+F", "U+F", "P+Pj+F")}
        matrix["none"] = (1000000, None)
        triggers = {90: False, 95: False, "P+U+Pj95+F90": True}
        for observed, trigger in (
            (matrix, triggers),
            ({"none": (1000000, None), "F": (1000000, None)}, triggers | {90: True}),
        ):
            with self.subTest(observed=observed):
                case = CompactionEnvironmentProbe("test_observation")
                case.evidence = {}
                previous = getattr(CompactionEnvironmentProbe, "verdict", None)
                try:
                    with mock.patch.object(case, "_probe", side_effect=lambda: (
                            CompactionEnvironmentProbe._classify(observed, trigger), "unit")), \
                            mock.patch.object(case, "_fixture"), mock.patch.object(probe, "write_evidence"):
                        with self.assertRaises(AssertionError):
                            case.test_observation()
                    self.assertEqual(case.verdict[0], "FAIL")
                finally:
                    CompactionEnvironmentProbe.verdict = previous

    def test_only_correctness_essential_classes_are_pinned(self):
        for cls, changed in ((CompactionEnvironmentProbe, "CE-B"), (DisableAllHooksProbe, "DAH-partial"),
                             (ClientIdentityProbe, "U1-none"), (SpawnCapsProbe, "SC-future"),
                             (SubagentExhaustionProbe, "SX-closed"), (SmallFastProbe, "SF-lead-light"),
                             (ExactClientProbe, "XC-effort-dropped")):
            case = cls("test_observation")
            case.evidence = {}
            previous = getattr(cls, "verdict", None)
            try:
                with mock.patch.object(case, "_probe", return_value=(changed, "unit")), \
                        mock.patch.object(case, "_fixture"), mock.patch.object(probe, "write_evidence"):
                    if cls.probe_id in _ESSENTIAL_CLASSES:
                        with self.assertRaises(AssertionError):
                            case.test_observation()
                        expected = " or ".join(_ESSENTIAL_CLASSES[cls.probe_id])
                        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                            cls.tearDownClass()
                        self.assertEqual(output.getvalue(),
                                         f"client probe {cls.probe_id}: FAIL class={changed} class pin expects {expected}\n")
                    else:
                        case.test_observation()
                        self.assertEqual(case.verdict[:2], ("PASS", changed))
            finally:
                cls.verdict = previous

    def test_inconclusive_control_keeps_inconclusive_verdict(self):
        case = CompactionEnvironmentProbe("test_observation")
        case.evidence = {}
        previous = getattr(CompactionEnvironmentProbe, "verdict", None)
        try:
            with mock.patch.object(case, "_probe", side_effect=_Inconclusive("invalid control")), \
                    mock.patch.object(case, "_fixture"), mock.patch.object(probe, "write_evidence"):
                with self.assertRaises(AssertionError):
                    case.test_observation()
            self.assertEqual(case.verdict, ("INCONCLUSIVE", "unavailable", "invalid control"))
        finally:
            CompactionEnvironmentProbe.verdict = previous

    def test_nesting_cap_uses_child_depth_not_request_count(self):
        responder = _SpawnResponder("SC-4")
        for ident, parent, tools in (("a", None, ("Agent", "Bash")),
                                    ("b", "a", ("Agent", "Bash")),
                                    ("c", "b", ("Bash",)), ("c", "b", ("Bash",))):
            hints = {"x-claude-code-agent-id": ident}
            if parent:
                hints["x-claude-code-parent-agent-id"] = parent
            responder.message({}, SimpleNamespace(model="probe-nest", tool_names=tools), hints)
        self.assertEqual(responder.caps, {"depth"})
        self.assertEqual(responder.agent_not_offered_depths, {3})
        self.assertEqual(max(responder.depths.values()), 3)
        self.assertEqual(responder.nested, 2)

    def test_exhaustion_accepts_work_after_child_summary(self):
        responder = _ExhaustionResponder("E3")
        child = SimpleNamespace(model="probe-heavy", tool_names=("Bash",))
        for _ in range(3):
            self.assertEqual(responder.message({}, child, {})[0], 200)
        self.assertEqual(responder.message({}, child, {})[0], 400)
        responder.continued = True
        summary = SimpleNamespace(model="probe-heavy", tool_names=())
        self.assertEqual(responder.message({}, summary, {})[0], 200)
        for _ in range(3):
            status, payload = responder.message({}, child, {})
            self.assertEqual(status, 200)
        self.assertEqual(payload["stop_reason"], "end_turn")
        self.assertEqual(responder.compactions, 1)
        self.assertEqual(responder.resume_rejections, 0)

    def test_exhaustion_proactive_and_reactive_recoveries_are_closed(self):
        child = SimpleNamespace(model="probe-heavy", tool_names=("Bash",))
        summary = SimpleNamespace(model="probe-heavy", tool_names=())
        lead = SimpleNamespace(model=_LEAD, tool_names=("Agent",))
        for recovered_case in ("E1", "E2"):
            with self.subTest(case=recovered_case):
                observations = {}
                for name in ("E1", "E2", "E3"):
                    responder = _ExhaustionResponder(name)
                    responder.message({}, lead, {})
                    for _ in range(3):
                        responder.message({}, child, {})
                    if name in {recovered_case, "E3"}:
                        if name != "E1":
                            self.assertEqual(responder.message({}, child, {})[0], 400)
                        responder.message({}, summary, {})
                    while responder.child_requests < 8:
                        responder.message({}, child, {})
                    result = {"messages": [{"content": [{"type": "tool_result",
                        "tool_use_id": "toolu_probe_1", "content": "PROBE-OK"}]}]}
                    responder.message(result, lead, {})
                    observations[name] = {
                        "summary_requests": responder.compactions, "compact_events": 0,
                        "completed_result": responder.completed_result, "failed_result": responder.failed_result,
                        "resume_requests": responder.resume_requests, "resume_rejections": responder.resume_rejections,
                        "resume_completed": responder.resume_completed,
                    }
                    self.assertFalse(responder.continued, "a recovered agent needs no E3 continuation")
                self.assertEqual(_exhaustion_class(observations), "SX-closed")
                observations[recovered_case]["completed_result"] = False
                self.assertEqual(_exhaustion_class(observations), "SX-open-continue-dies")
                observations[recovered_case]["completed_result"] = True
                observations["E3"]["failed_result"] = True
                self.assertEqual(_exhaustion_class(observations), "SX-open-continue-dies")
                observations["E3"].update(resume_requests=1, resume_completed=True)
                self.assertEqual(_exhaustion_class(observations), "SX-closed")
                observations["E3"]["resume_rejections"] = 1
                self.assertEqual(_exhaustion_class(observations), "SX-open-continue-dies")

    def test_exhaustion_window_class_comes_from_observations(self):
        small = {"failed_result": True, "child_requests": 4, "compact_events": 0}
        large = {"failed_result": False, "child_requests": 8, "compact_events": 0}
        self.assertEqual(_exhaustion_window_class(small, large), "E4-own-window")
        small.update(failed_result=False, compact_events=1)
        self.assertEqual(_exhaustion_window_class(small, large), "E4-own-window")
        self.assertEqual(_exhaustion_window_class(large, large), "E4-no-observed-difference")
        large["failed_result"] = True
        self.assertEqual(_exhaustion_window_class(small, large), "E4-inconclusive")

    def test_feedback_full_class_needs_lead_and_subagent_notify_controls(self):
        def matrix(off=(False, False), notify=(True, False), explicit=None):
            rows = {}
            for case in _FEEDBACK_CASES:
                lead, child = off if case.startswith("off") else notify
                if case == "notify-explicit" and explicit is not None:
                    lead, child = explicit
                rows[case] = {"lead_offered": lead, "child_offered": child}
            return rows

        # The 2.1.281 recording: notify restores the lead only, for both
        # subagent shapes. Never the full class.
        recorded = matrix()
        self.assertEqual(_feedback_class(recorded), "FD-env-off-subagent-withheld")
        self.assertNotEqual(_feedback_class(recorded), "FD-env-off")
        self.assertEqual(_feedback_class(matrix(notify=(True, True))), "FD-env-off")
        self.assertEqual(_feedback_class(matrix(explicit=(True, True))), "FD-env-off")
        for off in ((True, False), (False, True)):
            self.assertEqual(_feedback_class(matrix(off=off, notify=(True, True))), "FD-env-ignored")
        for broken in (matrix(notify=(False, False)), matrix(explicit=(False, True))):
            with self.assertRaises(_Inconclusive):
                _feedback_class(broken)
        explicit_off = matrix(notify=(True, True))
        explicit_off["off-explicit"]["child_offered"] = True
        self.assertEqual(_feedback_class(explicit_off), "FD-env-ignored")
        self.assertEqual(_ESSENTIAL_CLASSES["FD"], ("FD-env-off", "FD-env-off-subagent-withheld"))

    def test_exact_client_refuses_every_silent_payload_change(self):
        def run(requested, **row):
            return {"effort": requested, "rows": [{"model": "w", "effort": requested, "thinking": "adaptive",
                                                   "max_tokens": 100, **row}]}

        def matrix(lead=None, agent=None, window=None):
            return {"lead": {"high": lead or run("high")}, "agent": {"max": agent or run("max")},
                    "window": window or {"client": 1000, "compiled": 800}}

        expect = dict(wire="w", window={"client": 1000, "compiled": 800}, output_limit=100)
        self.assertEqual(_xc_class(matrix(), **expect), "XC-exact")
        for observed, cls in (
                (matrix(lead=run("high", model="other")), "XC-model-rewritten"),
                (matrix(lead=run("high", effort=None)), "XC-effort-dropped"),
                (matrix(agent=run("max", effort="high")), "XC-effort-dropped"),
                (matrix(agent=run("max", thinking=None)), "XC-thinking-stripped"),
                (matrix(lead=run("high", max_tokens=101)), "XC-output-exceeds"),
                (matrix(window={"client": 200, "compiled": 800}), "XC-window-mismatch")):
            self.assertEqual(_xc_class(observed, **expect), cls)
        with self.assertRaises(_Inconclusive):
            _xc_class(matrix(agent={"effort": "max", "rows": []}), **expect)
        self.assertEqual(_ESSENTIAL_CLASSES["XC"], ("XC-exact",))

    def test_feedback_subagent_offered_under_off_never_satisfies_client_boundary_evidence(self):
        """Every matrix in which any subagent shape is
        offered SendFeedback under off fails closed: neither FD-env-off nor
        the boundary class, and its completion line never satisfies the FD
        essential prefix of `update`."""

        d95 = "client probe FD: PASS class=FD-env-off "
        others = "\n".join(p + "fixture" for p in upgrade.ESSENTIAL_EVIDENCE_PREFIXES if p != d95)
        cells = [(case, key) for case in _FEEDBACK_CASES for key in ("lead_offered", "child_offered")]
        checked = 0
        for bits in range(1 << len(cells)):
            rows = {case: {} for case in _FEEDBACK_CASES}
            for index, (case, key) in enumerate(cells):
                rows[case][key] = bool(bits >> index & 1)
            if not any(rows[case]["child_offered"] for case in _FEEDBACK_CASES if case.startswith("off")):
                continue
            checked += 1
            with self.subTest(matrix=rows):
                try:
                    observed = _feedback_class(rows)
                except _Inconclusive:
                    status, observed = "INCONCLUSIVE", "unavailable"
                else:
                    self.assertNotIn(observed, _ESSENTIAL_CLASSES["FD"])
                    self.assertEqual(observed, "FD-env-ignored")
                    status = "FAIL"
                line = f"client probe FD: {status} class={observed} detail"
                self.assertEqual(upgrade._missing_evidence_prefixes(others + "\n" + line + "\n"), (d95,))
        self.assertEqual(checked, 192)


class _ProbeCase:
    probe_id = ""

    @classmethod
    def setUpClass(cls):
        cls.verdict = ("INCONCLUSIVE", "unavailable", "probe did not complete")
        cls.gate = real_binary_gate(_CONTRACT, what=f"probe {cls.probe_id}")
        if cls.gate.boundary:
            cls.verdict = ("BOUNDARY", "unavailable", cls.gate.boundary)

    @classmethod
    def tearDownClass(cls):
        status, observed, detail = cls.verdict
        text = _README.read_text() if _README.is_file() else ""
        detail = _observation_detail(cls.probe_id, observed, detail, text)
        # Flushed at once, so the line starts a line of a combined -v log.
        print(f"client probe {cls.probe_id}: {status} class={observed} {detail}", flush=True)

    def setUp(self):
        if not self.probe_id:
            self.skipTest("abstract probe case")
        if self.gate.boundary:
            self.skipTest(self.gate.boundary)
        self.trusted = self.gate.trusted
        self.root = Path(tempfile.mkdtemp(prefix="probe-probes-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root)
        self.environ = {"HOME": str(self.root / "ambient"), "PATH": "/usr/bin:/bin"}
        self.count = 0
        self.evidence = {}

    def _fixture(self):
        self.count += 1
        fixture = probe.build_fixture(self.root / f"run-{self.count}", environ=self.environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        return fixture

    def _settings(self, fixture, document, path="scope/settings.json"):
        # A compiled scope configured nowhere carries the managed
        # permission default (2.1.284+ otherwise starts in auto mode, which
        # through a gateway refuses Agent spawns); an explicit one is kept.
        document = dict(document)
        permissions = dict(document.get("permissions") or {})
        permissions.setdefault("defaultMode", scope.PERMISSION_DEFAULT_MODE)
        document["permissions"] = permissions
        return probe.write_fixture_settings(fixture, path, document)

    def _agent(self, fixture, name=_AGENT, model=_AGENT, extra=""):
        directory = state.ensure_private_dir(fixture.project_dir / ".claude/agents")
        state.atomic_write(directory / f"{name}.md", (
            f"---\nname: {name}\ndescription: probe disposable probe\n"
            f"model: {model}\n{extra}---\nReply PROBE-OK.\n"
        ).encode())

    def _hooks(self, fixture, events, *, identity=False, proc=False, deny=False):
        """Recorder persists only explicit metadata; proc runs inside the fixture's PID namespace."""
        log = fixture.root / "events.jsonl"
        recorder = fixture.root / "record.py"
        source = f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
p = json.load(sys.stdin)
r = {{k: p.get(k) for k in ('hook_event_name', 'source', 'session_id', 'agent_id')}}
r['keys'] = sorted(p)
'''
        if identity:
            source += f'''
pin = Path({str(self.trusted.resolved_path)!r})
def same(path):
    if not path: return None
    try:
        a, b = Path(path).stat(), pin.stat()
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except OSError: return None
exe, pid = os.environ.get('CLAUDE_CODE_EXECPATH'), os.environ.get('CLAUDE_CODE_PID')
r.update(has_execpath=bool(exe), execpath_same_inode=same(exe),
         has_claude_pid=bool(pid), pid_exe_same_inode=same('/proc/' + pid + '/exe') if pid and pid.isdigit() else None)
'''
        if proc:
            source += '''
# This hook is launched ONLY with --unshare-pid --proc /proc. No host scan.
r['proc_matches'] = []
rid = p.get('session_id', '')
for d in Path('/proc').iterdir():
    if not d.name.isdigit(): continue
    try:
        argv = (d / 'cmdline').read_bytes().split(b'\\0')
        if rid.encode() in argv and b'--session-id' in argv:
            r['proc_matches'].append(same(d / 'exe'))
    except OSError: pass
'''
        source += f'''
fd = os.open({str(log)!r}, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
with os.fdopen(fd, 'w') as out: out.write(json.dumps(r) + '\\n')
'''
        if deny:
            source += "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreModelSwitch', 'permissionDecision': 'deny', 'permissionDecisionReason': 'probe fence'}}))\n"
        else:
            source += "print('{}')\n"
        state.atomic_write(recorder, source.encode())
        recorder.chmod(0o700)
        hooks = {event: [{"hooks": [{"type": "command", "command": shlex.quote(str(recorder)),
                                     "timeout": 5}]}] for event in events}
        return hooks, log, recorder

    @staticmethod
    def _events(path):
        return [strict_json.loads(line) for line in path.read_bytes().splitlines()] if path.exists() else []

    def _run(self, fixture, *, document=None, responder=None, model=_LEAD,
             prompt="client probe", steps=None, extra=(), timeout=120, **options):
        document = document if document is not None else {}
        settings = self._settings(fixture, document)
        argv = ("--model", model, "--settings", str(settings), *extra)
        if steps is None:
            argv = ("-p", prompt, "--dangerously-skip-permissions", *argv)
        responder = responder or _Recorder()
        with probe.FakeAnthropicProvider(responder=responder) as provider:
            common = dict(trusted=self.trusted, fixture=fixture, provider=provider,
                          allow_real=True, environ=self.environ, timeout=timeout, **options)
            try:
                result = (probe.run_native(argv, **common) if steps is None else
                          probe.run_native_pty(argv, steps, **common))
            except probe.PTYTimeoutError as exc:
                self.evidence[f"run-{self.count}"] = {
                    "returncode": None, "timed_out": True, "phase": exc.phase,
                    "completed_interactions": exc.completed_interactions,
                    "total_interactions": exc.total_interactions,
                    "elapsed_seconds": exc.elapsed_seconds,
                    "requests": [asdict(row) for row in provider.requests],
                }
                raise
        # Only safe request metadata enters evidence; result stdio stays in memory.
        self.evidence[f"run-{self.count}"] = {
            "returncode": result.returncode, "timed_out": result.timed_out,
            "requests": [asdict(row) for row in result.requests],
        }
        return result, responder

    @staticmethod
    def _onboarding():
        return [probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r")]

    def _turns(self, turns=(b"client probe",)):
        steps = self._onboarding()
        for turn in turns:
            steps += [probe.PTYInteraction(_PROMPT, turn + b"\r"),
                      probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True)]
        steps.append(probe.PTYInteraction(_PROMPT, b"/exit\r"))
        return steps

    def _complete(self, result):
        if result.timed_out or result.returncode != 0 or not result.requests:
            raise probe.ProbeError("probe run did not complete a fake-provider turn")

    def _hardlink(self):
        # A disposable link on the pin's filesystem, not disk-backed TMPDIR.
        cache = state.ensure_private_dir(Path.home() / ".cache/cm-3.0x/probe-u1")
        root = Path(tempfile.mkdtemp(prefix="run-", dir=cache))
        self.addCleanup(shutil.rmtree, root)
        if root.stat().st_dev != self.trusted.resolved_path.stat().st_dev:
            raise unittest.SkipTest("BOUNDARY: U1-b cache and pin are on different filesystems")
        link = root / self.gate.version
        os.link(self.trusted.resolved_path, link)
        return probe.TrustedExecutable(link, self.trusted.sha256)

    def test_observation(self):
        try:
            observed, detail = self._probe()
        except AssertionError:
            self.__class__.verdict = ("FAIL", "unavailable", "probe consistency check failed")
            raise
        except unittest.SkipTest as exc:
            self.__class__.verdict = ("BOUNDARY", "unavailable", str(exc))
            raise
        except _Inconclusive as exc:
            self.__class__.verdict = ("INCONCLUSIVE", "unavailable", str(exc))
            self.fail(str(exc))
        except probe.ProbeError as exc:
            # Do not print PTY errors: they may include captured terminal text.
            if "live daemon domain touched" in str(exc):
                self.__class__.verdict = ("BOUNDARY", "unavailable", "live daemon-domain tripwire hit")
                self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
            detail = "probe execution unavailable"
            if isinstance(exc, probe.PTYTimeoutError):
                detail = str(exc) + "; hint=disabled"
            elif "probe PTY timed out" in str(exc):
                detail = "PTY observation timed out; no completed post-action turn; hint=disabled"
            self.__class__.verdict = ("INCONCLUSIVE", "unavailable", detail)
            if self.probe_id not in _DIAGNOSTICS:
                self.fail("probe execution unavailable (stdio deliberately not retained)")
        else:
            expected = _ESSENTIAL_CLASSES.get(self.probe_id)
            if expected is not None and observed not in expected:
                self.__class__.verdict = ("FAIL", observed, f"class pin expects {' or '.join(expected)}")
                self.assertIn(observed, expected)
            self.__class__.verdict = ("PASS", observed, detail)
        finally:
            fixture = self._fixture()
            probe.write_evidence(fixture, self.probe_id.lower(), self.evidence)


class CompactionEnvironmentProbe(_ProbeCase, unittest.TestCase):
    probe_id = "CE"

    @staticmethod
    def _numbers(text):
        def tokens(match):
            if not match:
                return None
            return int(float(match[1]) * {"": 1, "k": 1000, "m": 1000000}[match[2]])
        return (tokens(re.search(r"\*\*Tokens:\*\*\s*\S+\s*/\s*([0-9.]+)([km]?)", text)),
                tokens(re.search(r"\|\s*Autocompact buffer\s*\|\s*([0-9.]+)([km]?)\s*\|", text)))

    @staticmethod
    def _classify(observed, triggers):
        if observed["none"][0] in (300000, 400000, 500000, 600000):
            raise _Inconclusive("CE none control equals a configured window; adjust probe values")
        if triggers[95]:
            raise _Inconclusive("CE negative control compacted; adjust probe values")
        if observed["F"] == observed["none"]:
            if triggers[90]:
                raise AssertionError("CE flag ignored by matrix but flag-only policy compacted")
            return "CE-C"
        if not triggers[90]:
            raise AssertionError("CE flag wins matrix but flag-only 90% policy did not compact")
        if (all(observed[case] == observed["F"] for case in ("P+F", "U+F", "P+Pj+F"))
                and triggers["P+U+Pj95+F90"]):
            return "CE-A"
        return "CE-B"

    def _probe(self):
        policies = {name: probe.ProbeCompactionPolicy(window, pct) for name, window, pct in
                    (("P", 300000, 60), ("U", 400000, 70), ("Pj", 500000, 80), ("F", 600000, 90))}
        observed = {}
        for case in ((), ("P",), ("U",), ("F",), ("P", "U"), ("P", "F"), ("U", "F"), ("P", "Pj", "F")):
            fixture = self._fixture()
            for layer, path in (("U", "home/claude-config/settings.json"),
                                ("Pj", "project/.claude/settings.json")):
                if layer in case:
                    # Derive the config path from the fixture, never assume HOME layout.
                    target = fixture.claude_config_dir / "settings.json" if layer == "U" else fixture.project_dir / ".claude/settings.json"
                    self._settings(fixture, {"env": policies[layer].settings_env()}, str(target.relative_to(fixture.root)))
            result, _ = self._run(fixture, model=_LEAD + "[1m]", prompt="/context",
                                  document={"env": policies["F"].settings_env()} if "F" in case else {},
                                  compaction=policies["P"] if "P" in case else None)
            self.assertEqual(result.returncode, 0)
            observed["+".join(case) or "none"] = self._numbers(result.stdout)
        self.assertTrue(all(window is not None for window, _ in observed.values()))
        scalar = {}
        for label, process, flag in (("P", 150000, None), ("F", None, 120000), ("P+F", 150000, 120000)):
            fixture = self._fixture()
            result, _ = self._run(fixture, prompt="/context",
                document={"env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(flag)}} if flag else {},
                compaction=probe.ProbeCompactionPolicy(200000, 90, process) if process else None)
            scalar[label] = self._numbers(result.stdout)[0]
        triggers = {}
        for label, percent in ((90, 90), (95, 95), ("P+U+Pj95+F90", 90)):
            fixture = self._fixture()
            hooks, log, _ = self._hooks(fixture, ("PreCompact", "SessionStart"))
            policy = probe.ProbeCompactionPolicy(200000, percent, 200000)
            competing = probe.ProbeCompactionPolicy(200000, 95, 200000) if isinstance(label, str) else None
            if competing:
                for target in (fixture.claude_config_dir / "settings.json",
                               fixture.project_dir / ".claude/settings.json"):
                    self._settings(fixture, {"env": competing.settings_env()},
                                   str(target.relative_to(fixture.root)))
            large = " ".join(f"probe-token-{i:05d}" for i in range(4000))[:60000].encode()
            turns = (b"COMPACTION-SEED-A", b"COMPACTION-SEED-B", b"\x1b[200~" + large + b"\x1b[201~",
                     b"COMPACTION-BRIDGE", b"COMPACTION-TRIGGER")
            result, responder = self._run(fixture, document={"env": policy.settings_env(), "hooks": hooks},
                responder=probe.AutomaticCompactionResponder(), steps=self._turns(turns), timeout=180,
                compaction=competing)
            self._complete(result)
            triggers[label] = any(e.get("source") == "compact" for e in self._events(log))
            self.assertGreaterEqual(responder.main_requests, 5)
        outcome = self._classify(observed, triggers)
        if outcome == "CE-A":
            self.assertEqual(scalar, {"P": 150000, "F": 120000, "P+F": 120000})
            self.assertTrue(triggers[90], "flag-only 90% policy did not compact")
        self.evidence.update(context=observed, scalar=scalar,
                             trigger={str(key): value for key, value in triggers.items()})
        return outcome, (f"matrix={observed} scalar={scalar} compact90={triggers[90]} compact95={triggers[95]} "
                         f"PCT-P+U+Pj95+F90-compacted={triggers['P+U+Pj95+F90']}")


class DisableAllHooksProbe(_ProbeCase, unittest.TestCase):
    probe_id = "DAH"

    def _probe(self):
        observed = []
        for case in range(5):
            fixture = self._fixture()
            hooks, log, _ = self._hooks(fixture, ("SessionStart",))
            if case:
                target = (fixture.project_dir / ".claude/settings.local.json" if case == 3 else
                          fixture.claude_config_dir / "settings.json" if case == 4 else
                          fixture.project_dir / ".claude/settings.json")
                self._settings(fixture, {"disableAllHooks": True}, str(target.relative_to(fixture.root)))
            document = {"hooks": hooks}
            if case >= 2:
                document["disableAllHooks"] = False
            result, _ = self._run(fixture, document=document)
            self._complete(result)
            observed.append(bool(self._events(log)))
        self.assertTrue(observed[0], "DAH positive control hook never fired")
        self.assertFalse(observed[1], "DAH disabled-hooks control was not disabled")
        outcome = "DAH-override" if all(observed[2:]) else "DAH-partial" if any(observed[2:]) else "DAH-no-override"
        return outcome, f"DAH-0..4={observed}"


class ClientIdentityProbe(_ProbeCase, unittest.TestCase):
    probe_id = "U1"

    def _probe(self):
        observations = {}
        pin = self.trusted
        link = self._hardlink()
        candidates = sorted(p for p in pin.resolved_path.parent.iterdir()
                            if re.fullmatch(r"\d+\.\d+\.\d+", p.name)
                            and p.name != self.gate.version and not p.is_symlink())
        alternate = None
        if candidates:
            contract = strict_json.loads(_CONTRACT.read_bytes())
            candidate = probe._retained_binary(candidates[-1].name,
                versions_dir=pin.resolved_path.parent, contract=contract)
            alternate = probe.TrustedExecutable(candidate.path, candidate.sha256)
        for name, trusted in (("a", pin), ("b", link), ("c", alternate)):
            if trusted is None:
                observations[name] = {"boundary": "no retained non-pinned version"}
                continue
            fixture = self._fixture()
            hooks, log, _ = self._hooks(fixture, ("SessionStart",), identity=True)
            rid = str(uuid.uuid4())
            self.trusted = trusted
            try:
                for action in ("--session-id", "--resume"):
                    result, _ = self._run(fixture, document={"hooks": hooks}, extra=(action, rid))
                    self._complete(result)
            finally:
                self.trusted = pin
            observations[name] = self._events(log)
        fixture = self._fixture()
        hooks, log, _ = self._hooks(fixture, ("SessionStart",), identity=True, proc=True)
        result, _ = self._run(fixture, document={"hooks": hooks}, steps=self._turns(),
                              pid_namespace=True, extra=("--session-id", str(uuid.uuid4())))
        self._complete(result)
        observations["d"] = self._events(log)
        self.evidence["identity"] = observations
        rows = [row for values in observations.values() if isinstance(values, list) for row in values]
        outcome = _identity_class(observations)
        proc_signal = any(True in row.get("proc_matches", []) for row in observations["d"])
        pid_only = sum(row.get("pid_exe_same_inode") is True and not (
            row.get("has_execpath") and row.get("execpath_same_inode") is True) for row in rows)
        # Missing signals are unknown, never counted as mismatches.
        unknown = sum(not row.get("has_execpath") and not row.get("has_claude_pid") for row in rows)
        alternate_note = "observed" if alternate else "BOUNDARY(no retained non-pinned version)"
        return outcome, f"events={len(rows)} missing_signal={unknown} pid_only_signal={pid_only} own_tree_proc={proc_signal} U1-c={alternate_note}"


class RetainedLaunchProbe(_ProbeCase, unittest.TestCase):
    probe_id = "RET"

    def _probe(self):
        # A retained copy (<root>/<version>, a disposable hard link of the
        # pin) is copied into a fresh owned layout by the launch resolver,
        # and that owned copy runs a turn.
        linked = self._hardlink()
        fixture = self._fixture()
        contract = copy.deepcopy(strict_json.loads(_CONTRACT.read_bytes()))
        environ = {"HOME": str(fixture.root / "owned-home")}
        try:
            status = launch.resolve_claude(contract, environ=environ, retained_root=linked.resolved_path.parent)
        except launch.LaunchError:
            return "RET-fail", "retained copy migration refused"
        self.trusted = probe.TrustedExecutable(status.inspected_path, linked.sha256)
        result, _ = self._run(fixture)
        good = not result.timed_out and result.returncode == 0 and bool(result.requests)
        return "RET-ok" if good else "RET-fail", f"migrated_turn={good} migrated={status.migrated}"


class ReloadHooksProbe(_ProbeCase, unittest.TestCase):
    probe_id = "RL"

    def _probe(self):
        observations = {}
        for reload in (False, True):
            fixture = self._fixture()
            hooks, log, _ = self._hooks(fixture, ("UserPromptSubmit", "SessionStart", "ConfigChange",
                                                  "FileChanged", "InstructionsLoaded"))
            self._settings(fixture, {"hooks": hooks},
                           str((fixture.claude_config_dir / "settings.json").relative_to(fixture.root)))
            before = []
            steps = self._onboarding() + [
                probe.PTYInteraction(_PROMPT, b"client probe\r"),
                probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                probe.PTYInteraction(_PROMPT, b"/reload-plugins\r" if reload else b"",
                                     before_send=lambda: before.extend(self._events(log))),
            ]
            if reload:
                steps.append(probe.PTYInteraction(b"Reloaded", b"", preserve_after_wait=True))
            if reload:
                steps.append(probe.PTYInteraction(_PROMPT, b"/exit\r"))
            else:
                steps[-1] = probe.PTYInteraction(_PROMPT, b"/exit\r", before_send=lambda: before.extend(self._events(log)))
            result, _ = self._run(fixture, steps=steps)
            self._complete(result)
            observations[reload] = [row.get("hook_event_name") for row in self._events(log)[len(before):]]
        events = sorted(set(observations[True]) - set(observations[False]))
        self.evidence["reload_events"] = {str(key): value for key, value in observations.items()}
        return ("RL-hook", "event=" + ",".join(events)) if events else ("RL-none", "no observing hook versus control")


class MissingHookProbe(_ProbeCase, unittest.TestCase):
    probe_id = "R19"

    def _probe(self):
        outcomes = {}
        for remove in (False, True):
            fixture = self._fixture()
            hooks, log, recorder = self._hooks(fixture, ("PreModelSwitch",), deny=True)
            bundle = catalog.load_catalog(FIXTURE_ROOT)
            lcat = profile.LineupCatalog.from_docs(bundle.docs)
            eff = settings.effective({"version": 1}, provider_ids=lcat.providers, line_keys=lcat.lines)
            lineup = profile.resolve(bundle.seed_profiles["direct"], lcat, effective=eff)
            plan = scope.compile_lineup_scope(lineup, lcat, eff, bundle.prompt_bodies,
                scope.catalog_meta_v2(bundle.docs), lineup_generation=1)
            rows = plan.settings["modelPicker"]["options"][:2]
            lead, second = (row["model"] for row in rows)
            # Keep two compiler-generated rows. The control denies deliberately:
            # a missing target must not be confused with a permitted switch.
            document = {**plan.settings, "model": lead, "availableModels": [lead, second],
                        "modelPicker": {"replaceBuiltInOptions": True, "options": rows},
                        "hooks": hooks}
            shim = state.ensure_private_dir(fixture.root / "state/bin") / "claude-multi-hook-3"
            recorder.rename(shim)
            for entry in hooks["PreModelSwitch"]:
                entry["hooks"][0]["command"] = shlex.quote(str(shim))
            recorder = shim
            other_files = dict(plan.other_files)
            lead_set = strict_json.loads(other_files["lead-set.json"])
            lead_set["rows"] = [row for row in lead_set["rows"] if row["selector"] in (lead, second)]
            first = next(row for row in lead_set["rows"] if row["selector"] == lead)
            lead_set["lead"] = {key: first[key] for key in lead_set["lead"]}
            other_files["lead-set.json"] = strict_json.canonical_file_bytes(lead_set)
            scope_path = scope.write_scope(fixture.root / "state", str(uuid.uuid4()),
                scope.ScopePlan(plan.agent_files, document, other_files))
            def change():
                if remove:
                    recorder.unlink()
            # The switch comes before the first turn: with a cached
            # conversation an allowed switch first asks "Switch model?", and
            # the next typed turn would answer that dialog instead.
            steps = self._onboarding() + [
                probe.PTYInteraction(_PROMPT, f"/model {second}\r".encode(), before_send=change),
                probe.PTYInteraction(_PROMPT, b"probe after\r",
                                     before_send=lambda: threading.Event().wait(0.5)),
                probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                probe.PTYInteraction(_PROMPT, b"/exit\r"),
            ]
            result, _ = self._run(fixture, model=lead, document=document, steps=steps,
                                  extra=("--add-dir", str(scope_path)), timeout=60)
            self._complete(result)
            outcomes[remove] = (any(row.model == second.removesuffix("[1m]") for row in result.requests), len(self._events(log)))
        self.evidence["missing_hook"] = {str(k): v for k, v in outcomes.items()}
        if outcomes[False][0] or not outcomes[False][1]:
            return "unavailable", "present-target negative control failed; hint=disabled"
        return "R19-open" if outcomes[True][0] else "R19-closed", f"present={outcomes[False]} removed={outcomes[True]}"


class _ToolsRecorder(_Recorder):
    """Also keeps whether each request offered one named tool (a flag only)."""

    def __init__(self, tool):
        super().__init__()
        self.tool = tool
        self.offered = []

    def message(self, document, record, hints):
        self.offered.append(self.tool in record.tool_names)
        return _reply(document)


_SKEW_KEY = "probeSettingFromANewerClient"


def _newer_clients(trusted, version):
    """Installed Claude Code builds newer than the pin, next to it (oldest first)."""

    pinned = upgrade._version_key(version)
    if pinned is None:
        return []
    found = []
    for path in trusted.resolved_path.parent.iterdir():
        key = upgrade._version_key(path.name)
        if key is not None and key > pinned and path.is_file() and not path.is_symlink():
            found.append((key, path))
    return [path for _key, path in sorted(found)]


class SettingsSkewProbe(_ProbeCase, unittest.TestCase):
    """A user settings file with a key the pin does not know: does the pin
    still apply that file (its deny rules), or skip it? The control (no
    unknown key) must apply the deny, else the probe is inconclusive."""

    probe_id = "SK"

    def _probe(self):
        offered = {}
        for unknown in (False, True):
            fixture = self._fixture()
            document = {"permissions": {"deny": ["WebFetch"]}}
            if unknown:
                document[_SKEW_KEY] = True
            self._settings(fixture, document,
                           str((fixture.claude_config_dir / "settings.json").relative_to(fixture.root)))
            result, responder = self._run(fixture, responder=_ToolsRecorder("WebFetch"))
            self._complete(result)
            offered[unknown] = any(responder.offered)
        self.evidence["settings_skew"] = {str(key): value for key, value in offered.items()}
        if offered[False]:
            raise _Inconclusive("control: a user-settings deny did not remove the tool")
        return ("SK-skipped" if offered[True] else "SK-kept"), f"denied_tool_offered={offered}"


class OwnedCopyIsolationProbe(_ProbeCase, unittest.TestCase):
    """The owned copy (claude-multi's own layout, the managed update switches
    set) runs a turn in a HOME with a native install: the ``~/.local/bin/claude``
    link, the version file and ``installMethod``/``autoUpdates`` in both global
    config locations are compared before and after."""

    probe_id = "OC"

    def _probe(self):
        linked = self._hardlink()
        owned = state.ensure_private_dir(linked.resolved_path.parent / "claude" / self.gate.version) / "claude"
        os.link(linked.resolved_path, owned)
        self.trusted = probe.TrustedExecutable(owned, linked.sha256)
        fixture = self._fixture()
        versions = state.ensure_private_dir(fixture.home / ".local/share/claude/versions")
        native = versions / "9.9.9"
        state.atomic_write(native, b"#!/bin/sh\nexit 0\n")
        native.chmod(0o700)
        link = state.ensure_private_dir(fixture.home / ".local/bin") / "claude"
        link.symlink_to(native)
        configs = (fixture.home / ".claude.json", fixture.claude_config_dir / ".claude.json")
        for path in configs:
            document = strict_json.loads(path.read_bytes()) if path.exists() else {}
            document.update(installMethod="native", autoUpdates=True)
            state.atomic_write(path, strict_json.canonical_file_bytes(document))

        def snapshot():
            facts = {"link": os.readlink(link), "native": probe._sha256_file(native),
                     "versions": sorted(p.name for p in versions.iterdir())}
            for index, path in enumerate(configs):
                try:
                    document = json.loads(path.read_bytes())
                except (OSError, ValueError):
                    document = {}
                facts[f"config{index}"] = [document.get("installMethod"), document.get("autoUpdates")]
            return facts

        before = snapshot()
        result, _ = self._run(fixture)
        self._complete(result)
        after = snapshot()
        changed = sorted(key for key in before if before[key] != after[key])
        self.evidence["owned_copy"] = {"changed": changed}
        return ("OC-touched" if changed else "OC-untouched"), f"changed={changed}"


class FirstLaunchProbe(_ProbeCase, unittest.TestCase):
    """A first launch with an empty Claude config (no global config, no
    trust): the one-time screens (text style, security notes, the folder
    trust check, whose preselected answer is "No, exit") are answered and a
    first turn completes."""

    probe_id = "FL"

    def _probe(self):
        self.count += 1
        fixture = probe.build_fixture(self.root / f"run-{self.count}", environ=self.environ)
        self.assertEqual(list(fixture.claude_config_dir.iterdir()), [])
        pause = lambda: threading.Event().wait(0.3)  # noqa: E731 - let the dialog take input
        # Single-word markers: an incremental redraw may move the cursor
        # between the words of a phrase. After Down, the selection glyph is
        # drawn again on the "Yes, I trust this folder" row.
        steps = [
            probe.PTYInteraction(b"Choose", b"\r"),
            probe.PTYInteraction(b"Press", b"\r"),
            probe.PTYInteraction(b"confirm", b"\x1b[B", before_send=pause),
            probe.PTYInteraction(_PROMPT, b"\r", before_send=pause),
            probe.PTYInteraction(_PROMPT, b"client probe\r"),
            probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
            probe.PTYInteraction(_PROMPT, b"/exit\r"),
        ]
        result, _ = self._run(fixture, steps=steps, timeout=60)
        good = not result.timed_out and result.returncode == 0 and bool(result.requests)
        return ("FL-ok" if good else "FL-fail"), "screens=text-style,security-notes,folder-trust"


class NewerTranscriptResumeProbe(_ProbeCase, unittest.TestCase):
    """A transcript written by a newer plain claude, resumed by the pin (the
    S → L link-and-resume path): the resumed turn must carry the history."""

    probe_id = "SL"

    def _probe(self):
        newer = _newer_clients(self.trusted, self.gate.version)
        if not newer:
            raise unittest.SkipTest("BOUNDARY: no Claude Code newer than the pin installed next to it")
        writer = probe.TrustedExecutable(newer[-1], probe._sha256_file(newer[-1]))
        fixture = self._fixture()
        session = str(uuid.uuid4())
        pin = self.trusted
        counts = {}
        for label, trusted, argv in (("newer", writer, ("--session-id", session)),
                                     ("pin", pin, ("--resume", session))):
            self.trusted = trusted
            try:
                result, _ = self._run(fixture, extra=argv)
            finally:
                self.trusted = pin
            self._complete(result)
            counts[label] = max(row.message_count for row in result.requests)
        self.evidence["newer_transcript"] = {"writer": newer[-1].name, "message_counts": counts}
        resumed = counts["pin"] > counts["newer"]
        return ("SL-resumed" if resumed else "SL-history-lost"), f"writer={newer[-1].name} counts={counts}"


class _RetryResponder(_Recorder):
    def __init__(self, agent=False):
        super().__init__()
        self.agent = agent
        self.spawned = False
        self.rejected = 0
        self.lead_after = 0

    def message(self, document, record, hints):
        subagent = hints.get("x-claude-code-request-class") == "subagent" or record.model == _AGENT
        if (not self.agent and "Agent" in record.tool_names) or (self.agent and subagent):
            self.rejected += 1
            return probe.ProviderReply(429, {"type": "error", "error": {
                "type": "rate_limit_error", "message": "probe rate limit"}}, (("retry-after", "3600"),))
        if self.agent and "Agent" in record.tool_names and not self.spawned:
            self.spawned = True
            return _reply(document, [_spawn(1)], stop="tool_use")
        if self.rejected and not subagent and "Agent" in record.tool_names:
            self.lead_after += 1
        return _reply(document)


class RetryWatchdogProbe(_ProbeCase, unittest.TestCase):
    probe_id = "X5"

    def _probe(self):
        observations = {}
        for agent in (False, True):
            fixture = self._fixture()
            self._agent(fixture)
            hooks, log, _ = self._hooks(fixture, ("Notification", "StopFailure", "SubagentStop"))
            result, responder = self._run(fixture, responder=_RetryResponder(agent), timeout=20,
                document={"env": {"CLAUDE_CODE_RETRY_WATCHDOG": "1", "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"},
                          "hooks": hooks, "availableModels": [_LEAD, _AGENT]},
                extra=("--output-format", "stream-json", "--verbose"))
            retries = []
            for line in result.stdout.splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("type") == "system" and item.get("subtype") == "api_retry":
                    # Only integer retry metadata, never errors or message text.
                    retries.append({k: item[k] for k in ("attempt", "max_retries", "retry_delay_ms", "status_code")
                                    if isinstance(item.get(k), int)})
            observations["W5" if agent else "W1"] = {
                "timeout": result.timed_out, "rejections": responder.rejected, "retry_events": retries,
                "lead_after": responder.lead_after, "hook_events": len(self._events(log)),
            }
        self.evidence["watchdog"] = observations
        outcome = _retry_class(observations)
        note = " insufficient retry/visibility evidence; hint=disabled" if outcome == "unavailable" else ""
        return outcome, f"W1={observations['W1']} W5={observations['W5']}{note}"


class SmallFastProbe(_ProbeCase, unittest.TestCase):
    probe_id = "SF"

    def _probe(self):
        observations = {}
        haiku, small = "probe-haiku-marker", "probe-sf-marker"
        for case in range(5):
            fixture = self._fixture()
            env = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}
            if case in (1, 2, 4):
                env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = haiku
            if case in (3, 4):
                env["ANTHROPIC_SMALL_FAST_MODEL"] = small
            document = {"env": env}
            if case != 2:
                document["availableModels"] = [_LEAD, _AGENT]
            result, responder = self._run(fixture, document=document,
                                          steps=self._turns((b"probe first", b"probe second")))
            self._complete(result)
            observations[f"SF-{case}"] = responder.rows
        self.evidence["small_fast"] = observations
        background = [row for row in observations["SF-0"] if not row["agent_offered"]]
        markers = {row["model"] for rows in observations.values() for row in rows} & {haiku, small}
        if not background:
            return "SF-env-ignored", "no background request observed; no routing code ships"
        lead = [row for row in background if row["model"] == _LEAD]
        heavy = any((row["tool_count"] or (row["max_tokens"] or 0) > 4096 or row["thinking"] == "enabled") for row in lead)
        outcome = "SF-lead-heavy" if heavy else "SF-lead-light" if lead else "SF-env-ignored"
        return outcome, f"background={len(background)} marker_routes={sorted(markers)} SF-2-models={sorted({r['model'] for r in observations['SF-2']})}; no code ships"


class _SpawnResponder(_Recorder):
    def __init__(self, case):
        super().__init__()
        self.case = case
        self.issued = 0
        self.children = 0
        self.nested = 0
        self.caps = set()
        self.depths = {}
        self.nested_agents = set()
        self.agent_not_offered_depths = set()

    def respond(self, document, path, context):
        response = super().respond(document, path, context)
        if self.case == "SC-1" and context.record.model == "probe-nest":
            # Hold child calls long enough that all 22 dispatches contend for
            # the default cap. Never hold the responder's metadata lock.
            threading.Event().wait(2)
        return response

    def message(self, document, record, hints):
        for result in _results(document):
            text = json.dumps(result.get("content", "")).lower()
            for needle, label in (("concurrent subagent limit", "concurrency"),
                                  ("nesting limit", "depth"), ("budget limit", "budget")):
                if needle in text:
                    self.caps.add(label)
        child = record.model == "probe-nest"
        if child:
            self.children += 1
            if self.case == "SC-4":
                agent_id = hints.get("x-claude-code-agent-id")
                parent_id = hints.get("x-claude-code-parent-agent-id")
                parent_depth = self.depths.get(parent_id) if parent_id else 0
                if agent_id and parent_depth is not None:
                    self.depths[agent_id] = parent_depth + 1
                    if record.tool_names and "Agent" not in record.tool_names:
                        self.agent_not_offered_depths.add(parent_depth + 1)
                        self.caps.add("depth")
                if "Agent" not in record.tool_names or agent_id in self.nested_agents:
                    return _reply(document)
            if self.case == "SC-4" and self.nested < 20:
                if agent_id:
                    self.nested_agents.add(agent_id)
                self.nested += 1
                return _reply(document, [_spawn(100 + self.nested, "probe-nest")], stop="tool_use")
            return _reply(document)
        if "Agent" in record.tool_names:
            total = 3 if self.case == "SC-6" else 1
            if self.issued < total:
                self.issued += 1
                calls = [_spawn(i, "probe-nest") for i in range(22)] if self.case == "SC-1" else [_spawn(self.issued, "probe-nest")]
                return _reply(document, calls, stop="tool_use")
        return _reply(document)


class SpawnCapsProbe(_ProbeCase, unittest.TestCase):
    probe_id = "SC"

    def _probe(self):
        observations = {}
        for case in ("SC-1", "SC-4", "SC-6"):
            fixture = self._fixture()
            self._agent(fixture, "probe-nest", "probe-nest")
            env = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}
            if case == "SC-6":
                env["CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION"] = "2"
            result, responder = self._run(fixture, responder=_SpawnResponder(case),
                document={"env": env, "availableModels": [_LEAD, "probe-nest"]})
            self._complete(result)
            self.assertGreater(responder.issued, 0, "spawn stimulus never reached")
            observations[case] = {"issued_batches": responder.issued, "child_requests": responder.children,
                                  "nested_attempts": responder.nested, "caps": sorted(responder.caps)}
            if case == "SC-4":
                observations[case].update(reached_depth=max(responder.depths.values(), default=None),
                    agent_not_offered_depths=sorted(responder.agent_not_offered_depths))
        self.evidence["spawn_caps"] = observations
        return ("SC-caps" if any(row["caps"] for row in observations.values()) else "SC-none"), str(observations)


class _ExhaustionResponder(_Recorder):
    def __init__(self, case, agent_id=lambda: None):
        super().__init__()
        self.case = case
        self.read_agent_id = agent_id
        self.spawned = False
        self.continued = False
        self.child_requests = 0
        self.rejections = 0
        self.compactions = 0
        self.resume_requests = 0
        self.resume_rejections = 0
        self.agent_id = None
        self.failed_result = False
        self.completed_result = False
        self.resume_completed = False
        self.max_turns_result = False

    def message(self, document, record, hints):
        child = record.model == "probe-heavy"
        if child:
            self.child_requests += 1
            if self.continued:
                self.resume_requests += 1
            # Compaction requests have no executable tools. They receive a
            # short synthetic summary rather than the next work-loop step.
            if not record.tool_names:
                self.compactions += 1
                return _reply(document)
            if self.case in ("E2", "E3") and self.child_requests >= 4 and not self.compactions:
                self.rejections += 1
                if self.continued:
                    self.resume_rejections += 1
                return 400, {"type": "error", "error": {
                    "type": "invalid_request_error", "message": "prompt is too long"}}
            if self.child_requests >= 8:
                return _reply(document)
            usage = (1000 if self.case == "E5" else
                     1000 if self.compactions else
                     40000 if self.child_requests == 1 else
                     100000 if self.child_requests == 2 else 180000)
            return _reply(document, [_tool("Bash", self.child_requests + 100,
                          command="printf probe-step", description="fixture progress")],
                          usage=usage, stop="tool_use")
        if not self.spawned and "Agent" in record.tool_names:
            self.spawned = True
            return _reply(document, [_spawn(1, "probe-heavy")], stop="tool_use")
        if "Agent" not in record.tool_names:
            return _reply(document)
        for result in _results(document):
            if result.get("tool_use_id") not in {"toolu_probe_1", "toolu_probe_2"}:
                continue
            text = json.dumps(result.get("content", ""))
            match = re.search(r"agentId[:=]\s*([a-zA-Z0-9_-]+)", text)
            if match:
                self.agent_id = match[1]
            lower = text.lower()
            failed = bool(result.get("is_error")) or "prompt is too long" in lower
            if result["tool_use_id"] == "toolu_probe_1":
                self.failed_result |= failed
                self.completed_result |= not failed
            else:
                self.resume_completed |= not failed
            self.max_turns_result |= "max" in lower and "turn" in lower
        self.agent_id = self.agent_id or self.read_agent_id()
        if self.case == "E3" and self.failed_result and self.agent_id and not self.continued:
            self.continued = True
            return _reply(document, [_spawn(2, "probe-heavy", resume=self.agent_id)], stop="tool_use")
        return _reply(document)


class SubagentExhaustionProbe(_ProbeCase, unittest.TestCase):
    probe_id = "SX"

    def _probe(self):
        observations = {}
        for case in ("E1", "E2", "E3", "E4", "E5"):
            fixture = self._fixture()
            model = "probe-heavy[1m]" if case == "E4" else "probe-heavy"
            self._agent(fixture, "probe-heavy", model, "maxTurns: 3\n" if case == "E5" else "")
            hooks, log, _ = self._hooks(fixture, ("PreCompact", "SessionStart", "SubagentStart", "SubagentStop"))
            result, responder = self._run(fixture, responder=_ExhaustionResponder(case,
                lambda: next((r.get("agent_id") for r in self._events(log)
                              if r.get("hook_event_name") == "SubagentStart"), None)),
                document={"env": {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}, "hooks": hooks,
                          "availableModels": [_LEAD, model]},
                compaction=probe.ProbeCompactionPolicy(200000, 90))
            self._complete(result)
            self.assertTrue(responder.spawned and responder.child_requests, "exhaustion stimulus not reached")
            events = self._events(log)
            compact = [row for row in events if row.get("source") == "compact"]
            parent = next((row.get("session_id") for row in events if row.get("source") == "startup"), None)
            observations[case] = {
                "child_requests": responder.child_requests, "rejections": responder.rejections,
                "summary_requests": responder.compactions, "compact_events": len(compact),
                "continued": responder.continued, "resume_requests": responder.resume_requests,
                "resume_rejections": responder.resume_rejections,
                "failed_result": responder.failed_result, "max_turns_result": responder.max_turns_result,
                "completed_result": responder.completed_result, "resume_completed": responder.resume_completed,
                "parent_compact_without_marker": any(row.get("session_id") == parent and
                    not {"agent_id", "agent_transcript_path"}.intersection(row["keys"]) for row in compact),
            }
        self.evidence["exhaustion"] = observations
        outcome = _exhaustion_class(observations)
        if observations["E3"]["failed_result"]:
            self.assertGreater(observations["E3"]["resume_requests"], 0,
                               "E3 did not exercise continuation after context exhaustion")
        window_class = _exhaustion_window_class(observations["E1"], observations["E4"])
        return outcome, (f"{observations}; E5-usable={observations['E5']['child_requests'] <= 3} "
                         f"E4-class={window_class}")


# ------------------------------------------------------------------ feedback drafts
# FD, feedback drafts: the proof that the scope env alone suppresses the
# pinned client's SendFeedback tool, lead and subagent, and that omitting the
# key (notify) restores it. At this pin the tool is also gated by a remote
# feature flag, which the netns blocks; the fixture's own Claude config seeds
# that flag's cached value so the control can offer the tool at all (fixture
# onboarding only, never the live config). Only booleans are retained.
# Each case runs twice: a default-tools subagent and one whose frontmatter
# lists SendFeedback explicitly, so a client that withholds the tool from
# every subagent is told apart from one that only needs it requested.
_FEEDBACK_TOOL = "SendFeedback"
_FEEDBACK_FLAG = "tengu_juniper_relay"
_FEEDBACK_AGENTS = {"": "", "-explicit": f"tools: {_FEEDBACK_TOOL}, Bash\n"}
_FEEDBACK_CASES = tuple(f"{case}{shape}" for case in ("off", "notify") for shape in _FEEDBACK_AGENTS)


def _seed_feature_cache(fixture, features):
    path = fixture.claude_config_dir / ".claude.json"
    document = strict_json.loads(state.read_private(path)) if path.exists() else {}
    document["cachedGrowthBookFeatures"] = dict(features)
    state.atomic_write(path, strict_json.canonical_file_bytes(document))


class _FeedbackResponder(_Recorder):
    def __init__(self):
        super().__init__()
        self.spawned = False
        self.lead = []
        self.child = []

    def message(self, document, record, hints):
        child = record.model == _AGENT
        (self.child if child else self.lead).append(_FEEDBACK_TOOL in record.tool_names)
        if not child and "Agent" in record.tool_names and not self.spawned:
            self.spawned = True
            return _reply(document, [_spawn(1)], stop="tool_use")
        return _reply(document)


def _feedback_class(observations):
    """FD-env-off only with a positive notify control for the lead AND a
    subagent; FD-env-off-subagent-withheld when the client withholds the
    tool from every subagent shape even under notify (an explicit client
    boundary: the subagent's absence is not caused by the preference)."""

    off = [observations[case] for case in _FEEDBACK_CASES if case.startswith("off")]
    notify = [observations[case] for case in _FEEDBACK_CASES if case.startswith("notify")]
    if not all(row["lead_offered"] for row in notify):
        raise _Inconclusive("control: the notify run never offered SendFeedback to the lead")
    if any(row["lead_offered"] or row["child_offered"] for row in off):
        return "FD-env-ignored"
    if any(row["child_offered"] for row in notify):
        return "FD-env-off"
    return "FD-env-off-subagent-withheld"


class FeedbackDraftsProbe(_ProbeCase, unittest.TestCase):
    """FD: ``claude_feedback_drafts`` off removes SendFeedback from
    the lead's and the subagent's tool lists through the scope env alone."""

    probe_id = "FD"

    def _probe(self):
        observations = {}
        attempts = self.evidence["feedback_attempts"] = []
        retry_used = False
        for case in _FEEDBACK_CASES:
            value, shape = ("off", case[3:]) if case.startswith("off") else ("notify", case[6:])
            env = scope.feedback_drafts_env(value)
            attempt = 0
            while True:
                attempt += 1
                fixture = self._fixture()
                _seed_feature_cache(fixture, {_FEEDBACK_FLAG: True})
                self._agent(fixture, extra=_FEEDBACK_AGENTS[shape])
                row = {"case": case, "attempt": attempt, "run": f"run-{self.count}",
                       "outcome": "incomplete"}
                attempts.append(row)
                try:
                    result, responder = self._run(
                        fixture, responder=_FeedbackResponder(), steps=self._turns(),
                        document={"env": env, "availableModels": [_LEAD, _AGENT]})
                    row.update(returncode=result.returncode, timed_out=result.timed_out)
                    self._complete(result)
                    self.assertTrue(responder.spawned and responder.child, f"{case}: subagent stimulus not reached")
                except probe.PTYTimeoutError as exc:
                    # Both archived failures reached client exit.
                    # Their stdio/timings were not retained, so do not invent a
                    # shutdown cause or inflate the deadline. Allow ONE marked
                    # retry across the entire matrix, in a fresh fixture. The
                    # failed run stays failed in evidence and the gate log.
                    retry = exc.phase == "client-exit" and not retry_used
                    row.update(outcome="timeout", phase=exc.phase, retry=retry,
                               returncode=None, timed_out=True,
                               completed_interactions=exc.completed_interactions,
                               total_interactions=exc.total_interactions,
                               elapsed_seconds=exc.elapsed_seconds)
                    if not retry:
                        raise
                    retry_used = True
                except (probe.ProbeError, AssertionError) as exc:
                    row.update(outcome="failed", retry=False, error=type(exc).__name__)
                    raise
                else:
                    row.update(outcome="completed", retry=False)
                    break
                finally:
                    print("client probe FD attempt: " + json.dumps(row, sort_keys=True), flush=True)
            observations[case] = {
                "env": sorted(env), "lead_requests": len(responder.lead), "child_requests": len(responder.child),
                "lead_offered": any(responder.lead), "child_offered": any(responder.child),
            }
        self.evidence["feedback_drafts"] = observations
        outcome = _feedback_class(observations)
        detail = "; ".join(f"{case} lead={observations[case]['lead_offered']} "
                           f"child={observations[case]['child_offered']}" for case in _FEEDBACK_CASES)
        detail += f"; marked_exit_retry={'used' if retry_used else 'unused'}"
        if outcome == "FD-env-off-subagent-withheld":
            detail += " (subagent notify control unavailable: the client withholds SendFeedback from subagents)"
        return outcome, detail


class FeedbackRetryTests(unittest.TestCase):
    """Retry only the archived exit-phase signature, once, visibly."""

    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="cm-d95-retry-")
        self.addCleanup(root.cleanup)
        self.case = FeedbackDraftsProbe("test_observation")
        self.case.root = Path(root.name)
        self.case.environ = {"HOME": str(self.case.root / "ambient"), "PATH": "/usr/bin:/bin"}
        self.case.trusted = object()  # run_native_pty is replaced; never a real binary
        self.case.count = 0
        self.case.evidence = {}
        self.log = io.StringIO()
        output = mock.patch("sys.stdout", self.log)
        output.start()
        self.addCleanup(output.stop)
        runner = mock.patch.object(probe, "run_native_pty")
        self.runner = runner.start()
        self.addCleanup(runner.stop)
        self.effects = []
        self.runner.side_effect = self._run

    def _run(self, argv, steps, **kwargs):
        effect = self.effects.pop(0) if self.effects else None
        if isinstance(effect, BaseException):
            raise effect
        document = strict_json.loads(Path(argv[argv.index("--settings") + 1]).read_bytes())
        responder = kwargs["provider"]._responder
        responder.spawned = True
        responder.lead = [not document.get("env")]
        responder.child = [False]
        request = probe.RequestRecord(
            "POST", "/v1/messages", _LEAD, (), 0, False, False, 0, 0, 1, (), False,
            "dummy", "0" * 64,
        )
        return probe.NativeRunResult(tuple(argv), effect or 0, False, "PRIVATE-STDIO", "", (request,))

    @staticmethod
    def _timeout(phase="client-exit"):
        return probe.PTYTimeoutError(phase, 5 if phase == "client-exit" else 3, 5, 120.0)

    def test_pty_retry_is_marked_and_retains_both_results_if_retry_selected(self):
        self.effects = [self._timeout()]
        outcome, detail = self.case._probe()
        self.assertEqual(outcome, "FD-env-off-subagent-withheld")
        self.assertIn("marked_exit_retry=used", detail)
        self.assertEqual(self.runner.call_count, len(_FEEDBACK_CASES) + 1)
        evidence = self.case.evidence
        self.assertTrue(evidence["run-1"]["timed_out"])
        self.assertIsNone(evidence["run-1"]["returncode"])
        self.assertEqual(evidence["run-1"]["phase"], "client-exit")
        self.assertFalse(evidence["run-2"]["timed_out"])
        self.assertEqual(evidence["run-2"]["returncode"], 0)
        rows = evidence["feedback_attempts"]
        self.assertEqual([row["outcome"] for row in rows[:2]], ["timeout", "completed"])
        self.assertEqual([row["attempt"] for row in rows[:2]], [1, 2])
        self.assertEqual([row["retry"] for row in rows[:2]], [True, False])
        logged = [json.loads(line.split("attempt: ", 1)[1]) for line in self.log.getvalue().splitlines()]
        self.assertEqual(logged, rows)  # retained in the receipt's hashed suite log
        self.assertNotIn("PRIVATE-STDIO", json.dumps(evidence) + self.log.getvalue())
        fixtures = [call.kwargs["fixture"].root for call in self.runner.call_args_list]
        self.assertEqual(len(set(fixtures)), len(fixtures))

    def test_pty_second_exit_timeout_never_passes(self):
        self.effects = [self._timeout(), self._timeout()]
        with self.assertRaises(probe.PTYTimeoutError):
            self.case._probe()
        self.assertEqual(self.runner.call_count, 2)
        rows = self.case.evidence["feedback_attempts"]
        self.assertEqual([row["outcome"] for row in rows], ["timeout", "timeout"])
        self.assertEqual([row["retry"] for row in rows], [True, False])

    def test_pty_retry_budget_is_shared_by_all_feedback_cases(self):
        self.effects = [self._timeout(), None, self._timeout()]
        with self.assertRaises(probe.PTYTimeoutError):
            self.case._probe()
        self.assertEqual(self.runner.call_count, 3)
        rows = self.case.evidence["feedback_attempts"]
        self.assertNotEqual(rows[0]["case"], rows[-1]["case"])
        self.assertFalse(rows[-1]["retry"])

    def test_pty_marker_timeout_or_other_failure_is_never_retried(self):
        for error in (self._timeout("marker"), probe.ProbeError("fixture failure")):
            with self.subTest(error=type(error).__name__):
                self.runner.reset_mock()
                self.effects = [error]
                with self.assertRaises(probe.ProbeError):
                    self.case._probe()
                self.assertEqual(self.runner.call_count, 1)

    def test_pty_failed_retry_retains_failure_in_gate_log(self):
        self.effects = [self._timeout(), 3]
        with self.assertRaisesRegex(probe.ProbeError, "did not complete"):
            self.case._probe()
        self.assertEqual(self.runner.call_count, 2)
        rows = self.case.evidence["feedback_attempts"]
        self.assertEqual([row["outcome"] for row in rows], ["timeout", "failed"])
        self.assertEqual(rows[1]["returncode"], 3)
        logged = [json.loads(line.split("attempt: ", 1)[1]) for line in self.log.getvalue().splitlines()]
        self.assertEqual(logged, rows)

    def test_pty_nonzero_exit_is_never_retried(self):
        self.effects = [3]
        with self.assertRaisesRegex(probe.ProbeError, "did not complete"):
            self.case._probe()
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(self.case.evidence["run-1"]["returncode"], 3)
        self.assertNotIn('"outcome": "completed"', self.log.getvalue())


class _MarkerResponder(_Recorder):
    """Lead and subagent each run one Bash command printing CLAUDECODE."""

    COMMAND = "printf 'cm-marker:%s\\n' \"${CLAUDECODE:-unset}\""

    def __init__(self):
        super().__init__()
        self.issued = set()
        self.seen = {}

    def message(self, document, record, hints):
        who = "child" if record.model == _AGENT else "lead"
        for result in _results(document):
            match = re.search(r"cm-marker:([A-Za-z0-9-]+)", json.dumps(result.get("content", "")))
            if match:
                self.seen.setdefault(who, match.group(1))
        if who not in self.issued and "Bash" in record.tool_names:
            self.issued.add(who)
            number = 30 if who == "lead" else 40
            return _reply(document, [_tool("Bash", number, command=self.COMMAND, description="probe marker")],
                          stop="tool_use")
        if who == "lead" and "lead" in self.seen and "spawn" not in self.issued and "Agent" in record.tool_names:
            self.issued.add("spawn")
            return _reply(document, [_spawn(2)], stop="tool_use")
        return _reply(document)


class ClaudeCodeMarkerProbe(_ProbeCase, unittest.TestCase):
    """The consent guard's premise: the pinned
    client exports CLAUDECODE to the Bash children of the lead and of a
    subagent, so the consent guard's session marker reaches them."""

    probe_id = "CC"

    def _probe(self):
        fixture = self._fixture()
        self._agent(fixture)
        result, responder = self._run(fixture, responder=_MarkerResponder(),
                                      document={"availableModels": [_LEAD, _AGENT]})
        self._complete(result)
        self.evidence["claudecode"] = {"issued": sorted(responder.issued), "seen": dict(responder.seen)}
        self.assertIn("lead", responder.seen, "lead Bash marker never observed")
        self.assertIn("child", responder.seen, "subagent Bash marker never observed")
        outcome = ("CC-bash-child" if responder.seen == {"lead": "1", "child": "1"}
                   else "CC-missing")
        return outcome, f"lead={responder.seen.get('lead')} subagent={responder.seen.get('child')}"


# ------------------------------------------------------------------ exact client
# The exact-client gate: an operator
# line on the Anthropic OAuth pool carries an overlay-only wire (the pinned
# client has no built-in knowledge of it) and a canonical selector. The scope
# is compiled for that line as an admitted Direct lead (the production
# compile, the same one launch uses), and the pinned client runs against the
# loopback fake as the lead at every declared effort and as a cm-* agent (an
# agent binding is outside this gate: these lines are lead-only, so the agent
# file is a fixture). The fake records only metadata: the request model, the
# output_config.effort id, the thinking type, max_tokens and the /context
# window. Unsupported payload semantics (a dropped or rewritten effort,
# stripped thinking, a wrong window, an output limit above the declaration)
# are a FAIL class, never a silent pass; the class is essential, so a re-pin
# (``claude-multi update``) refuses without it.
_XC_FILE = Path(__file__).resolve().parent / "fixtures/operator/providers.d/anthropic.json"
_XC_KEY = "custom-claude-fixture"
_XC_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_XC_AGENT = "cm-xc-agent"


def _xc_compile():
    """The admitted operator pool line compiled as a Direct lead (pure)."""

    from claude_multi import operator
    document = strict_json.loads(_XC_FILE.read_bytes())
    document["lines"][_XC_KEY]["efforts"] = list(_XC_EFFORTS)
    bundle = catalog.load_catalog(FIXTURE_ROOT)
    docs = copy.deepcopy(bundle.docs)
    layer = operator.validate_layer(docs, {"anthropic": strict_json.pretty_file_bytes(document)},
                                    schemas=operator.load_schemas(FIXTURE_ROOT))
    if _XC_KEY not in layer.lines:
        raise AssertionError(f"fixture pool line refused: {[p.text() for p in layer.problems]}")
    line = layer.lines[_XC_KEY]
    projection = operator.overlay_projection(line, docs["providers"]["providers"]["anthropic"])
    merged = operator.merge_docs(docs, layer)
    lcat = profile.LineupCatalog.from_docs(merged)
    eff = settings.effective({"version": 1, "admitted_lines": [_XC_KEY]},
                             provider_ids=lcat.providers, line_keys=lcat.lines)
    seed = copy.deepcopy(bundle.seed_profiles["direct"])
    seed.pop("seed", None)
    seed.update(name="xc", lead={"model": _XC_KEY, "effort": line.core_entry["default_effort"]})
    lineup = profile.resolve(seed, lcat, effective=eff)
    plan = scope.compile_lineup_scope(lineup, lcat, eff, bundle.prompt_bodies,
                                      scope.catalog_meta_v2(merged), lineup_generation=1)
    return line, projection, lineup, plan


def _xc_class(observations, *, wire, window, output_limit):
    """XC-exact only when every declared effort arrives unchanged with a
    thinking block, on the wire model, inside the declared window and output
    limit, for the lead and for an agent."""

    runs = [*observations["lead"].values(), *observations["agent"].values()]
    if any(not run["rows"] for run in runs):
        raise _Inconclusive("XC: a run never reached the wire model")
    for run in runs:
        for row in run["rows"]:
            if row["model"] != wire:
                return "XC-model-rewritten"
            if row["effort"] != run["effort"]:
                return "XC-effort-dropped"
            if row["thinking"] is None:
                return "XC-thinking-stripped"
            if row["max_tokens"] is None or row["max_tokens"] > output_limit:
                return "XC-output-exceeds"
    if observations["window"] != window:
        return "XC-window-mismatch"
    return "XC-exact"


def _xc_expected_window(plan, client_tokens):
    """The window the compiled scope books: the client window a canonical
    ``[1m]`` selector declares, capped by the compiled operating window."""

    compiled = plan.settings["env"].get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    return {"client": client_tokens, "compiled": int(compiled) if compiled is not None else client_tokens}


class _ExactClientResponder(_Recorder):
    def __init__(self, wire, spawn=False):
        super().__init__()
        self.wire = wire
        self.spawn = spawn
        self.spawned = False
        self.lead = []
        self.child = []

    def message(self, document, record, hints):
        if record.model != self.wire:
            return _reply(document)
        row = {"model": record.model, "effort": record.effort, "thinking": record.thinking_type,
               "max_tokens": record.max_tokens}
        child = bool(hints.get("x-claude-code-agent-id"))
        (self.child if child else self.lead).append(row)
        if self.spawn and not child and not self.spawned and "Agent" in record.tool_names:
            self.spawned = True
            return _reply(document, [_spawn(1, _XC_AGENT)], stop="tool_use")
        return _reply(document)



class ProductionExactClientTests(unittest.TestCase):
    """``probe.run_exact_client_check`` — the production
    runner ``models qualify --agents`` uses for a first-party pool line —
    passes the fixture pool line on the real pinned client (bwrap
    --unshare-net, loopback fake; zero provider requests)."""

    @classmethod
    def setUpClass(cls):
        cls.gate = real_binary_gate(_CONTRACT, what="the exact-client runner")

    def test_runner_passes_the_fixture_pool_line(self):
        if self.gate.boundary:
            self.skipTest(self.gate.boundary)
        from claude_multi import qualify

        line, _projection, lineup, plan = _xc_compile()
        entry = line.core_entry
        selector = lineup.lead.binding.selector
        compiled = plan.settings["env"].get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
        request = qualify.ExactClientInput(
            key=_XC_KEY, selector=selector, wire=selector.removesuffix("[1m]"), efforts=("high", "max"),
            client_effort=True, output_limit=line.output_tokens,
            expected_window={"client": entry["context"]["client_tokens"],
                             "compiled": int(compiled) if compiled else entry["context"]["client_tokens"]},
            settings=plan.settings, plan=plan,
        )
        outcome = probe.run_exact_client_check(request, native_contract=self.gate.contract)
        self.assertEqual(outcome, qualify.ExactClientOutcome("pass", "ok"))


class ExactClientProbe(_ProbeCase, unittest.TestCase):
    """The pinned client sends an overlay-only Anthropic pool
    wire's declared efforts, thinking, window and output limit unchanged."""

    probe_id = "XC"

    def _write(self, fixture, plan):
        live = scope.write_scope(fixture.root / "state", str(uuid.uuid4()), plan)
        return live, live / "settings.json"

    def _probe(self):
        line, projection, lineup, plan = _xc_compile()
        entry = line.core_entry
        selector, wire = lineup.lead.binding.selector, entry["wire_model"]
        # The production compile: canonical selector, fenced, offered by /model.
        self.assertEqual(plan.settings["model"], selector)
        self.assertIn(selector, plan.settings["availableModels"])
        self.assertEqual(selector, f"{wire}[1m]")
        self.assertIsNotNone(projection, "the pool line must render through oauth-extra-models")
        self.assertEqual(projection["channel"], "claude")
        window, output_limit = _xc_expected_window(plan, entry["context"]["client_tokens"]), line.output_tokens
        self.assertEqual(projection["max_completion_tokens"], output_limit)
        observations = {"lead": {}, "agent": {}}
        for effort in entry["efforts"]:
            fixture = self._fixture()
            live, settings_path = self._write(fixture, plan)
            responder = _ExactClientResponder(wire)
            result, responder = self._run(fixture, responder=responder, model=selector,
                                          extra=("--add-dir", str(live), "--effort", effort),
                                          document=strict_json.loads(settings_path.read_bytes()))
            self._complete(result)
            observations["lead"][effort] = {"effort": effort, "rows": responder.lead}
        for effort in ("high", "max"):
            lead_effort = "low"
            fixture = self._fixture()
            live, settings_path = self._write(fixture, plan)
            self._agent(fixture, name=_XC_AGENT, model=selector, extra=f"effort: {effort}\n")
            responder = _ExactClientResponder(wire, spawn=True)
            result, responder = self._run(fixture, responder=responder, model=selector,
                                          extra=("--add-dir", str(live), "--effort", lead_effort),
                                          document=strict_json.loads(settings_path.read_bytes()))
            self._complete(result)
            self.assertTrue(responder.spawned, f"agent {effort}: spawn stimulus not reached")
            observations["agent"][effort] = {"effort": effort, "rows": responder.child}
        # Window booking: the bare selector (the client's own knowledge of
        # the [1m] suffix), then the compiled scope (its operating window).
        observations["window"] = {}
        for kind in ("client", "compiled"):
            fixture = self._fixture()
            live, settings_path = self._write(fixture, plan)
            document = (strict_json.loads(settings_path.read_bytes()) if kind == "compiled"
                        else {"availableModels": [selector]})
            result, _ = self._run(fixture, model=selector, prompt="/context", extra=("--add-dir", str(live)),
                                  document=document)
            self.assertEqual(result.returncode, 0)
            observations["window"][kind] = CompactionEnvironmentProbe._numbers(result.stdout)[0]
        self.evidence["exact_client"] = observations
        outcome = _xc_class(observations, wire=wire, window=window, output_limit=output_limit)
        summary = {kind: {effort: sorted({(row["effort"], row["thinking"], row["max_tokens"]) for row in run["rows"]},
                                         key=str)
                          for effort, run in observations[kind].items()} for kind in ("lead", "agent")}
        return outcome, f"window={observations['window']} declared={window} output<={output_limit} {summary}"


# ------------------------------------------------------------ client downgrade

_DG_PREVIOUS = ("2.1.286", "fe503f65c6289d59c23e5b21ae44f03583f997dd33a2cbfc75ab4f96fb8fc73f")
_DG_MARKER = "PROBE-DG-SUMMARY"


class _DowngradeResponder:
    """One forced Bash ``true`` on the first lead request offering Bash; every
    other request (the compaction summary included) gets a marked text, so a
    later resume shows whether it continued from the compacted history."""

    def __init__(self):
        self.lock = threading.Lock()
        self.forced = False
        self.rows = []

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        names = [tool.get("name") for tool in document.get("tools") or () if isinstance(tool, dict)]
        text = json.dumps(document.get("messages"))
        with self.lock:
            self.rows.append({"model": document.get("model"), "summary_seen": _DG_MARKER in text,
                              "tool_use_seen": "toolu_probe_dg" in text})
            if "Bash" in names and not self.forced:
                self.forced = True
                content = [{"type": "tool_use", "id": "toolu_probe_dg", "name": "Bash",
                            "input": {"command": "true", "description": "probe downgrade probe"}}]
                stop = "tool_use"
            else:
                content, stop = [{"type": "text", "text": f"{_DG_MARKER} PROBE-OK"}], "end_turn"
        payload = probe._message_payload(content, model=document.get("model"), stop_reason=stop,
                                         message_id=f"msg_probe_dg_{context.ordinal:04d}")
        return 200, probe._sse_message(payload) if document.get("stream") else payload


class DowngradeResumeTests(_ProbeCase, unittest.TestCase):
    """A session started on the candidate pin (a tool turn, then a
    manual compaction) resumed on the previous release's pin, 2.1.286.
    Only synthetic fixture history is exercised; operator transcripts and
    supervisor takeover remain outside this rollback proof."""

    probe_id = "DG"
    test_observation = None  # one named test, not the shared observation runner

    def test_candidate_tool_compact_then_previous_client_resume(self):
        path = self.trusted.resolved_path.parent / _DG_PREVIOUS[0]
        if path == self.trusted.resolved_path or not path.is_file() or probe._sha256_file(path) != _DG_PREVIOUS[1]:
            type(self).verdict = ("BOUNDARY", "unavailable", f"previous pin {_DG_PREVIOUS[0]} not retained")
            self.skipTest(f"BOUNDARY: previous pin {_DG_PREVIOUS[0]} not retained next to the pinned client")
        previous = probe.TrustedExecutable(path, _DG_PREVIOUS[1])
        fixture = self._fixture()
        settings = self._settings(fixture, {})
        session = str(uuid.uuid4())
        responder = _DowngradeResponder()
        steps = (
            ("candidate-tool", self.trusted, ("-p", "PROBE-DG-TOOL-TURN", "--session-id", session,
                                              "--dangerously-skip-permissions")),
            ("candidate-compact", self.trusted, ("-p", "/compact", "--resume", session)),
            ("previous-resume", previous, ("-p", "PROBE-DG-AFTER", "--resume", session)),
        )
        outcomes = {}
        for label, trusted, argv in steps:
            before = len(responder.rows)
            with probe.FakeAnthropicProvider(responder=responder) as provider:
                try:
                    result = probe.run_native(("--model", _LEAD, "--settings", str(settings), *argv),
                                              trusted=trusted, fixture=fixture, provider=provider,
                                              allow_real=True, environ=self.environ, timeout=150)
                except probe.ProbeError as exc:
                    if "live daemon domain touched" in str(exc):
                        self.skipTest(f"BOUNDARY: {exc}")
                    raise
            rows = responder.rows[before:]
            outcomes[label] = {"returncode": result.returncode, "timed_out": result.timed_out, "requests": len(rows),
                               "summary_seen": any(row["summary_seen"] for row in rows),
                               "tool_use_seen": any(row["tool_use_seen"] for row in rows)}
            self.assertFalse(result.timed_out, label)
            self.assertEqual(result.returncode, 0, (label, outcomes))
        self.assertTrue(responder.forced)
        self.assertTrue(outcomes["candidate-compact"]["tool_use_seen"], outcomes)
        resumed = outcomes["previous-resume"]
        # The previous client resumes the candidate's transcript from the
        # compacted history: the summary is present, the pre-compaction tool
        # turn is not replayed.
        self.assertEqual(resumed["requests"] >= 1, True, outcomes)
        self.assertTrue(resumed["summary_seen"], outcomes)
        self.assertFalse(resumed["tool_use_seen"], outcomes)
        type(self).verdict = ("PASS", "DG-resumable",
                              f"candidate tool turn + /compact, resumed on {_DG_PREVIOUS[0]}: {outcomes}")
