"""Spike S1/S2 re-run end to end through ``claude-multi lineup``.

Real-binary probes against the pinned Claude Code binary (gated by
``probe.pinned_binary_gate`` through ``tests/_tier.real_binary_gate``; a
missing trusted binary or bubblewrap is a ``BOUNDARY`` skip). Every client
run goes through the probe harness: disposable fixture HOME/XDG/
CLAUDE_CONFIG_DIR, the loopback fake provider bridged into a loopback-only
network namespace (``bwrap --unshare-net``), the live-daemon-domain
tripwire. No provider or gateway is ever contacted.

The session is launched the production way: a probe ``Runtime`` on
the FIXTURE catalog with the fixture's HOME/XDG roots prepares a fresh launch
of the fixture ``balanced`` profile and performs it through
``launch.perform_launch`` with the injected ``execve`` seam, so the state
marker, the scope swap, the record (with ``launch_fence``) and the pointer
are committed before the argv is captured. The probe then
rewrites the scope's ``settings.json`` ``env.ANTHROPIC_BASE_URL`` to the
probe bridge (the fixture gateway's ``base_url`` is the live gateway port,
which the probe must never contact) and replaces ``apiKeyHelper`` with a
fixture helper, keeping ``availableModels`` and the hooks; because
``launch_fence`` covers ``env`` it re-pins the record's fence to the
rewritten bytes (a test-only save; ``launch_fence`` is outside
``applied_hash``), modelling a launch whose gateway was the bridge. The
compiled hooks run the source launcher through a fixture wrapper
(``CLAUDE_MULTI_HOOK_COMMAND``), exactly like the notice probe; the hook's
own Runtime loads the package catalog, as in production.

Every ``cli.main`` call passes ``runtime=rt``; a stat/open spy
asserts the operator's live state root is never touched while ``lineup``
runs. The fake provider attributes requests by the client's gateway hint
headers (``x-claude-code-request-class``); only metadata (models, booleans,
generation numbers) leaves the responder.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

import _tripwire
from claude_multi import cli, pin, probe, scope, sessions, state, strict_json
from claude_multi.probe import ProbeError

from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT
from _layout import REPO_ROOT, RESOURCES_ROOT
from _tier import real_binary_gate
import claude_multi.service

_tripwire.install()

_CONTRACT_PATH = RESOURCES_ROOT / "catalog" / "native-contract.json"
_HINT_ENV = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}
_PROMPT_GLYPH = "❯".encode("utf-8")
_DIRECTIVE_RE = re.compile(r"AGENTPROBE-SPAWN-(\d{1,2})")
_NOTICE_RE = re.compile(r"\[claude-multi lineup notice · lineup_generation (\d+)\]")
_TOOL_ID_PREFIX = "toolu_agentprobe_"
_SUMMARY = "AGENTPROBE-COMPACT-SUMMARY"
_AGENT = "cm-analyst"
_REQUEST = "set analyst=opus5"  # a fixture line on another wire than the lead's
_LIVE_TOUCH = "live daemon domain touched"
_ONE_M = "[1m]"

_WRAPPER = """#!/bin/sh
CLAUDE_MULTI_HOOK_COMMAND={wrapper}
export CLAUDE_MULTI_HOOK_COMMAND
PYTHONPATH={src}
export PYTHONPATH
exec {python} {launcher} "$@"
"""
_HELPER = """#!/bin/sh
printf '%s\\n' {token}
"""


def _wire(selector: str) -> str:
    """What the client puts on the wire for a selector."""

    return selector.removesuffix(_ONE_M)


def _screen_text(raw: str) -> str:
    text = re.sub(r"\x1b\][^\x07]*\x07", "", raw)
    text = re.sub(r"\x1b\[\d*G", " ", text)
    text = re.sub(r"\x1b\[[0-9;?<>=]*[A-Za-z~]", "", text)
    return " ".join(text.split())


class _LineupResponder:
    """Directive-driven fake provider for the lineup S1/S2 probes.

    A main-loop (lead) request whose newest user turn carries a new
    ``AGENTPROBE-SPAWN-<n>`` directive gets a forced ``Agent`` tool_use for
    ``cm-analyst``; its tool_result gets ``AGENTPROBE-DONE-<tag>`` and the
    background completion notification ``AGENTPROBE-NOTIFIED-<tag>``, so the
    PTY script can wait for each spawn. A request the client marks with the
    ``subagent`` request class is recorded as ``(tag, wire model)``. Lead
    requests record their wire model and every lineup-notice generation their
    context carries. While ``compacting`` is set every lead request gets a
    fixed summary.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._issued: list[str] = []
        self._done: set[str] = set()
        self.current_tag: str | None = None
        self.subagent: list[tuple[str | None, str | None, str | None]] = []
        self.lead: list[tuple[str | None, tuple[int, ...]]] = []
        self.compacting = False
        self.compaction_requests = 0

    def set_compacting(self, value: bool) -> None:
        with self._lock:
            self.compacting = value

    def calls_for(self, tag: str) -> list[str | None]:
        with self._lock:
            return [model for call_tag, model, _type in self.subagent if call_tag == tag]

    def respond(self, document: dict[str, Any], path: str, context: Any) -> tuple[int, Any]:
        route = urllib.parse.urlsplit(path).path.rstrip("/")
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return 404, {"type": "error", "error": {"type": "not_found_error", "message": "probe"}}
        record = context.record
        hints = dict(record.hint_header_values)
        with self._lock:
            if hints.get("x-claude-code-request-class") == "subagent":
                self.subagent.append((self.current_tag, record.model, hints.get("x-claude-code-agent-type")))
                return self._text(document, "AGENTPROBE-SUBAGENT-OK", context.ordinal)
            if self.compacting:
                self.compaction_requests += 1
                return self._text(document, f"{_SUMMARY} fixture summary", context.ordinal)
            return self._lead(document, record, context.ordinal)

    @staticmethod
    def _reply(document: dict[str, Any], blocks: list[dict[str, Any]], stop: str, ordinal: int):
        payload = probe._message_payload(
            blocks, model=document.get("model"), stop_reason=stop,
            message_id=f"msg_agent_probe_{ordinal:04d}",
        )
        return 200, (probe._sse_message(payload) if document.get("stream") else payload)

    def _text(self, document: dict[str, Any], text: str, ordinal: int):
        return self._reply(document, [{"type": "text", "text": text}], "end_turn", ordinal)

    def _lead(self, document: dict[str, Any], record: Any, ordinal: int):
        body = json.dumps(document, ensure_ascii=False)  # in memory only
        generations = tuple(sorted({int(n) for n in _NOTICE_RE.findall(body)}))
        names = [tool.get("name") for tool in document.get("tools") or [] if isinstance(tool, dict)]
        if record.max_tokens is not None and "Agent" in names:
            self.lead.append((record.model, generations))
        directives: list[str] = []
        results: set[str] = set()
        last_text = ""
        for message in document.get("messages") or []:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            texts = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                    directives.extend(_DIRECTIVE_RE.findall(block["text"]))
                elif block.get("type") == "tool_result":
                    tool_id = str(block.get("tool_use_id", ""))
                    if tool_id.startswith(_TOOL_ID_PREFIX):
                        results.add(tool_id[len(_TOOL_ID_PREFIX):])
            last_text = "\n".join(texts)
        if "Agent" not in names or not directives:
            return self._text(document, "PROBE-OK", ordinal)
        tag = f"spawn{directives[-1]}"
        if tag not in self._issued:
            self._issued.append(tag)
            self.current_tag = tag
            block = {
                "type": "tool_use",
                "id": f"{_TOOL_ID_PREFIX}{tag}",
                "name": "Agent",
                "input": {
                    "description": "agentprobe lineup probe delegation",
                    "subagent_type": _AGENT,
                    "prompt": "Reply with exactly one short line.",
                },
            }
            return self._reply(document, [block], "tool_use", ordinal)
        if tag in results and tag not in self._done:
            self._done.add(tag)
            return self._text(document, f"AGENTPROBE-DONE-{tag}", ordinal)
        if "<task-notification>" in last_text:
            return self._text(document, f"AGENTPROBE-NOTIFIED-{tag}", ordinal)
        return self._text(document, "PROBE-OK", ordinal)


class _LiveStateSpy:
    """Records any stat/open of the operator's live state root (names only)."""

    def __init__(self) -> None:
        self.live_root = os.path.join(os.path.expanduser("~"), ".local", "state", "claude-multi")
        self.hits: list[str] = []
        self._patches: list[Any] = []

    def _wrap(self, real):
        def spy(path, *args, **kwargs):
            try:
                text = os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else ""
            except TypeError:
                text = ""
            if text and os.path.abspath(text).startswith(self.live_root):
                self.hits.append(os.path.abspath(text))
            return real(path, *args, **kwargs)

        return spy

    def __enter__(self) -> "_LiveStateSpy":
        for name in ("stat", "lstat", "open"):
            patcher = mock.patch.object(os, name, self._wrap(getattr(os, name)))
            patcher.start()
            self._patches.append(patcher)
        return self

    def __exit__(self, *exc: Any) -> None:
        for patcher in reversed(self._patches):
            patcher.stop()


class _LineupProbeCase(unittest.TestCase):
    """Pinned-binary gate, a fixture, and a probe Runtime that really launches."""

    trusted: probe.TrustedExecutable
    evidence_label = ""
    boundary_skip: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="the lineup S1/S2 probes")
        cls.boundary_skip = gate.boundary
        if gate.trusted is not None:
            cls.trusted = gate.trusted
            cls.evidence_label = f"client={gate.version} sha256={gate.trusted.sha256[:12]}"

    def setUp(self) -> None:
        super().setUp()
        if self.boundary_skip is not None:
            self.skipTest(self.boundary_skip)
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-agent-probe-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"HOME": str(self.root / "live" / "home"), "PATH": "/usr/bin:/bin"}
        self.fixture = probe.build_fixture(self.root / "fixture", environ=self.environ)
        probe.seed_fixture_project_trust(self.fixture, self.fixture.project_dir)
        self.execs: list[tuple[str, list[str], dict[str, str]]] = []
        self.rt = self._runtime()

    # ---------------------------------------------------------- runtime

    def _runtime(self) -> cli.Runtime:
        wrapper = self.fixture.root / "claude-multi-wrapper"
        wrapper.write_text(
            _WRAPPER.format(
                wrapper=shlex.quote(str(wrapper)),
                src=shlex.quote(str(REPO_ROOT / "src")),
                python=shlex.quote(sys.executable),
                launcher=shlex.quote(str(REPO_ROOT / "bin" / "claude-multi")),
            ),
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        env = self.fixture.environ()
        env["CLAUDE_MULTI_HOOK_COMMAND"] = str(wrapper)
        token_dir = state.ensure_private_dir(self.fixture.home / ".config" / "claude-multi")
        state.atomic_write(token_dir / "api-key", (FIXTURE_GATEWAY_TOKEN + "\n").encode())
        # A fake verified binary for perform_launch's identity check: the
        # execve seam captures argv; the pinned client runs through the probe.
        platform = pin.host_platform()
        install = pin.owned_path(env, "2.1.281", platform)
        state.ensure_private_dir(install.parent)
        install.write_bytes(b"#!/bin/fake-claude\n")
        install.chmod(0o755)
        rt = cli.Runtime(
            asset_root=FIXTURE_ROOT,
            environ=env,
            cwd=self.fixture.project_dir,
            health_get=lambda _base, _path: 200,
            listener_owner=lambda _base: claude_multi.service.OwnerVerdict("ours", "fixture"),
            background_liveness=lambda: sessions.BackgroundLiveness(True, frozenset(), None),
            managed_root=self.fixture.root / "managed-policy",
            served_models_callback=lambda _gateway, _token: (None, None),
            doctor_binary_callback=lambda _contract: ([], ["fixture binary verified."]),
            doctor_callback=lambda _runtime: [],
            execve=self._execve,
        )
        docs = rt.catalog.docs
        docs["native-contract"] = {
            **docs["native-contract"],
            "verified": [{
                "version": "2.1.281",
                "platforms": {platform: {"sha256": hashlib.sha256(install.read_bytes()).hexdigest(),
                                         "size": install.stat().st_size}},
                "manifest_sha256": "1" * 64, "signature_sha256": None,
                "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE",
                "verified_at": "2026-09-26", "evidence": {platform: "battery", "receipt_sha256": "2" * 64},
            }],
        }
        rt._live_prefixes = lambda: frozenset()  # no machine liveness scan
        self.assertTrue(str(rt.session_store.root).startswith(str(self.fixture.root)))
        return rt

    def _execve(self, path: str, argv: list[str], env: dict[str, str]) -> int:
        self.execs.append((path, list(argv), dict(env)))
        return 0

    # ----------------------------------------------------------- launch

    def _launch_fresh(self) -> str:
        document = self.rt.profiles.load("balanced")
        target = cli.LaunchTarget("profile", document, "balanced", True, "Profile balanced")
        prepared = self.rt.prepare(target, action="fresh", passthrough=[])
        self.rt.perform(prepared)
        return sessions.managed_id(prepared.record)

    def _resume(self, mid: str) -> None:
        record = self.rt.session_store.load(mid)
        # The resume gate checks ~/.claude/projects; the fixture client keeps
        # its transcripts under CLAUDE_CONFIG_DIR (a presence stand-in only).
        slug = cli._native_project_slug(record["cwd"])
        marker = self.fixture.home / ".claude" / "projects" / slug / f"{record['runtime_session_id']}.jsonl"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        target = cli.LaunchTarget("record", None, record.get("profile"), bool(record.get("follow")), "resume")
        prepared = self.rt.prepare(target, action="resume", passthrough=[], session_id=mid)
        self.rt.perform(prepared)

    def _bridge_scope(self, mid: str, provider: probe.FakeAnthropicProvider) -> list[str]:
        """Point the committed scope at the probe bridge; re-pin launch_fence; argv."""

        store = self.rt.session_store
        live = scope.scope_dir(store.root, mid)
        path = live / "settings.json"
        settings = strict_json.loads(path.read_bytes())
        settings["env"]["ANTHROPIC_BASE_URL"] = provider.base_url
        helper = self.fixture.root / "token-helper"
        helper.write_text(_HELPER.format(token=shlex.quote(probe.DUMMY_TOKEN)), encoding="utf-8")
        helper.chmod(0o700)
        settings["apiKeyHelper"] = str(helper)
        self.assertIn("availableModels", settings)
        self.assertIn("hooks", settings)
        state.atomic_write(path, strict_json.canonical_file_bytes(settings))
        record = copy.deepcopy(store.load(mid))
        record["launch_fence"] = sessions.launch_digest(
            settings, (live / scope.LEAD_SET_JSON).read_bytes()
        )
        store.save(record)
        probe.seed_fixture_project_trust(self.fixture, live)
        _path, argv, _env = self.execs[-1]
        self.assertEqual(argv[argv.index("--settings") + 1], str(path))
        return argv[1:]

    def _lineup(self, rid: str, request: str) -> str:
        """``/cm <request>`` in skill mode, on the probe Runtime; stdout."""

        out, err = io.StringIO(), io.StringIO()
        with _LiveStateSpy() as spy, redirect_stderr(err):
            code = cli.main(
                ["lineup", "--session", rid, request], runtime=self.rt,
                output_stream=out, interactive=False,
            )
        self.assertEqual(spy.hits, [], "lineup touched the operator's live state root")
        self.assertEqual(code, 0, err.getvalue())
        self.lineup_output = out.getvalue()
        return self.lineup_output

    # -------------------------------------------------------------- runs

    def _onboarding(self) -> list[probe.PTYInteraction]:
        return [probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r")]

    def _boundary(self, probe_id: str, exc: ProbeError) -> None:
        if _LIVE_TOUCH in str(exc):
            print(f"client check {probe_id} via lineup: INCONCLUSIVE live daemon domain touched ({self.evidence_label})")
            self.skipTest(f"BOUNDARY: live daemon-domain churn prevented a clean {probe_id} observation ({exc})")

    def _run_pty(self, probe_id: str, argv: list[str], steps: list[probe.PTYInteraction],
                 provider: probe.FakeAnthropicProvider) -> probe.NativeRunResult:
        try:
            result = probe.run_native_pty(
                argv, steps, trusted=self.trusted, fixture=self.fixture, provider=provider,
                timeout=150, allow_real=True, environ=self.environ, child_env=dict(_HINT_ENV),
            )
        except ProbeError as exc:
            self._boundary(probe_id, exc)
            raise
        self.assertFalse(result.timed_out, _screen_text(result.stdout)[-2000:])
        self.assertEqual(result.returncode, 0)
        assert result.daemon is not None
        self.assertTrue(result.daemon.unchanged)
        return result

    def _boundary_entries(self, session_id: str) -> int:
        """``system/compact_boundary`` entries of the FIXTURE transcript (type keys only)."""

        count = 0
        for path in (self.fixture.claude_config_dir / "projects").glob(f"*/{session_id}.jsonl"):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = strict_json.loads(line.encode("utf-8"))
                    except strict_json.StrictJSONError:
                        continue
                    if isinstance(entry, dict) and entry.get("type") == "system" and entry.get(
                        "subtype"
                    ) == "compact_boundary":
                        count += 1
        return count

    def _await_boundary(self, session_id: str, timeout: float = 20.0) -> None:
        """C43: never stop the session inside the compact-boundary window."""

        deadline = time.monotonic() + timeout
        while self._boundary_entries(session_id) < 1:
            if time.monotonic() > deadline:
                raise RuntimeError("compact boundary was never persisted")
            time.sleep(0.1)

    def _selectors(self, mid: str) -> tuple[str, str]:
        record = self.rt.session_store.load(mid)
        return record["applied"]["lead"]["selector"], record["applied"]["agents"][_AGENT]["selector"]


class LineupS1Probe(_LineupProbeCase):
    """S1 through `claude-multi lineup`: /cm set + /reload-plugins rebinds the next spawn."""

    def test_cm_set_then_reload_rebinds_next_spawn(self) -> None:
        mid = self._launch_fresh()
        record = self.rt.session_store.load(mid)
        rid = record["runtime_session_id"]
        lead_selector, model_a = self._selectors(mid)
        responder = _LineupResponder()
        outputs: list[str] = []
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT_GLYPH, b"AGENTPROBE-SPAWN-1 go\r"),
            # /cm set while the session runs, then spawn WITHOUT a reload.
            probe.PTYInteraction(
                b"AGENTPROBE-NOTIFIED-spawn1",
                b"AGENTPROBE-SPAWN-2 go\r",
                before_send=lambda: outputs.append(self._lineup(rid, _REQUEST)),
            ),
            probe.PTYInteraction(b"AGENTPROBE-NOTIFIED-spawn2", b"/reload-plugins\r"),
            probe.PTYInteraction(b"Reloaded", b"AGENTPROBE-SPAWN-3 go\r"),
            probe.PTYInteraction(b"AGENTPROBE-NOTIFIED-spawn3", b"/exit\r"),
        ]
        with probe.FakeAnthropicProvider(responder=responder) as provider:
            argv = self._bridge_scope(mid, provider)
            self._run_pty("S1", argv, steps, provider)
        self.assertEqual(len(outputs), 1)
        self.assertIn(
            "next: type /reload-plugins (a model cannot run it for you; it also applies any "
            "pending plugin updates)", outputs[0])
        self.assertTrue(outputs[0].startswith("lineup gen 2: analyst "), outputs[0])
        _lead, model_b = self._selectors(mid)
        self.assertNotEqual(model_a, model_b)
        self.assertEqual(self.rt.session_store.load(mid)["lineup_generation"], 2)
        spawn1, spawn2, spawn3 = (responder.calls_for(f"spawn{n}") for n in (1, 2, 3))
        self.assertEqual(set(spawn1), {_wire(model_a)}, responder.subagent)
        # Rewritten on disk but not reloaded: the old definition still wins.
        self.assertEqual(set(spawn2), {_wire(model_a)}, responder.subagent)
        # After /reload-plugins the next spawn carries the new binding.
        self.assertEqual(set(spawn3), {_wire(model_b)}, responder.subagent)
        lead_models = {model for model, _gens in responder.lead}
        self.assertEqual(lead_models, {_wire(lead_selector)})
        # The startup notice (generation 1) and, on the prompt after the
        # change, the generation-2 notice reached the lead: the compiled
        # protocol-3 hooks ran through shim-3 and the source launcher.
        self.assertTrue(any(1 in gens for _model, gens in responder.lead), responder.lead)
        self.assertTrue(any(2 in gens for _model, gens in responder.lead), responder.lead)
        # SessionStart/SessionEnd reconciled the record through the same hooks.
        self.assertEqual(self.rt.session_store.load(mid)["last_event_source"], "end")
        print(
            "client check S1 via lineup: PASS "
            f"spawn1={_wire(model_a)} no-reload-spawn2={_wire(model_a)} "
            f"post-reload-spawn3={_wire(model_b)} lead={_wire(lead_selector)} "
            f"notice-gen2=yes {self.evidence_label}"
        )


class LineupS2Probe(_LineupProbeCase):
    """S2 through `claude-multi lineup`: the live change survives /compact + `-r`."""

    def test_live_change_survives_compact_and_resume(self) -> None:
        mid = self._launch_fresh()
        rid = self.rt.session_store.load(mid)["runtime_session_id"]
        lead_selector, model_a = self._selectors(mid)
        responder = _LineupResponder()
        outputs: list[str] = []
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT_GLYPH, b"AGENTPROBE-SPAWN-1 go\r"),
            probe.PTYInteraction(
                b"AGENTPROBE-NOTIFIED-spawn1",
                b"/reload-plugins\r",
                before_send=lambda: outputs.append(self._lineup(rid, _REQUEST)),
            ),
            probe.PTYInteraction(b"Reloaded", b"AGENTPROBE-SPAWN-2 go\r"),
            probe.PTYInteraction(
                b"AGENTPROBE-NOTIFIED-spawn2",
                b"/compact\r",
                before_send=lambda: responder.set_compacting(True),
            ),
            probe.PTYInteraction(
                b"Compacted",
                b"/exit\r",
                before_send=lambda: (responder.set_compacting(False), self._await_boundary(rid)),
            ),
        ]
        with probe.FakeAnthropicProvider(
            responder=responder, content_markers={"summary": _SUMMARY}
        ) as provider:
            argv = self._bridge_scope(mid, provider)
            self._run_pty("S2", argv, steps, provider)
            _lead, model_b = self._selectors(mid)
            applied_after_live = copy.deepcopy(self.rt.session_store.load(mid)["applied"])
            # `claude-multi -r <mid>`: prepared and performed through the probe
            # Runtime (the exec seam), then bridged again.
            self._resume(mid)
            resume_argv = self._bridge_scope(mid, provider)
            self.assertIn("--resume", resume_argv)
            first_phase = len(provider.requests)
            try:
                relaunch = probe.run_native(
                    ("-p", "AGENTPROBE-SPAWN-3 go", *resume_argv),
                    trusted=self.trusted, fixture=self.fixture, provider=provider,
                    timeout=150, allow_real=True, environ=self.environ,
                    child_env=dict(_HINT_ENV),
                )
            except ProbeError as exc:
                self._boundary("S2", exc)
                raise
        self.assertFalse(relaunch.timed_out)
        self.assertEqual(relaunch.returncode, 0)
        assert relaunch.daemon is not None
        self.assertTrue(relaunch.daemon.unchanged)
        self.assertEqual(len(outputs), 1)
        self.assertIn(
            "next: type /reload-plugins (a model cannot run it for you; it also applies any "
            "pending plugin updates)", outputs[0])
        self.assertNotEqual(model_a, model_b)
        self.assertGreaterEqual(responder.compaction_requests, 1)
        self.assertEqual(set(responder.calls_for("spawn1")), {_wire(model_a)}, responder.subagent)
        self.assertEqual(set(responder.calls_for("spawn2")), {_wire(model_b)}, responder.subagent)
        # The relaunch's first spawn keeps the live-changed binding.
        self.assertEqual(set(responder.calls_for("spawn3")), {_wire(model_b)}, responder.subagent)
        record = self.rt.session_store.load(mid)
        self.assertEqual(record["applied"]["agents"][_AGENT]["selector"], model_b)
        self.assertEqual(record["applied"]["agents"], applied_after_live["agents"])
        relaunch_lead = [
            row for row in relaunch.requests[first_phase:]
            if row.model == _wire(lead_selector) and row.max_tokens is not None
        ]
        self.assertTrue(relaunch_lead, "the relaunch made no lead request")
        self.assertTrue(all("summary" in row.markers_found for row in relaunch_lead))
        print(
            "client check S2 via lineup: PASS "
            f"pre-change={_wire(model_a)} post-reload={_wire(model_b)} "
            f"compaction_requests={responder.compaction_requests} "
            f"resume-spawn={_wire(model_b)} record-applied={_wire(record['applied']['agents'][_AGENT]['selector'])} "
            f"{self.evidence_label}"
        )


if __name__ == "__main__":
    unittest.main()
