"""real-binary probes for the v2 scope compiler.

Three classes drive the pinned Claude client (path + full sha256 from
``catalog/native-contract.json``; ``_tier.real_binary_gate`` = the fast-tier
aware ``probe.pinned_binary_gate``) against the loopback fake provider,
every run hermetic (``bwrap --unshare-net``, fixture-private HOME,
CLAUDE_CONFIG_DIR and TMPDIR, the live daemon-domain tripwire armed). No
provider or gateway is contacted; evidence is metadata only (model ids,
effort ids, tool names, marker names, hook event names, booleans).

- ``SeedDelegationProbe`` (SHIPPED catalog): every shipped seed is
  compiled lifecycle-free through ``scope.compile_lineup_scope`` and run
  with the compiled flag settings; one forced parallel ``Agent`` call per
  bound id plus an unfenced canary proves each bound id dispatches on its
  binding, the flag settings and fence are live in the process, the read-only
  roles are denied their tools and SubagentStart carries ``agent_type`` and
  ``agent_id``. Its completion line is ``ESSENTIAL_EVIDENCE_PREFIXES[4]``.
- ``SettingsEnvPlacementProbe`` (FIXTURE): the family default and Explore
  inherit cap are honoured from FLAG settings env, and what
  ``workflow_default_binding`` (``CLAUDE_CODE_SUBAGENT_MODEL`` in flag
  settings env) does to workflow and native agents.
- ``HookShim3RealBinaryTests`` (FIXTURE): a failing launcher never
  blocks a prompt through shim-3 and the protocol-3 notice on startup,
  resume and fork through the real shim and launcher, with the prompt fast
  path observed on the real client payload.

Expectations are derived at runtime from the loaded catalog and the compiled
plan; the request model of a binding is its selector without ``[1m]``
(never the catalog ``wire_model``).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from typing import Any, Callable

from claude_multi import (
    catalog,
    cli,
    hooks,
    probe,
    profile,
    scope,
    sessions,
    settings,
    state,
    strict_json,
    upgrade,
)
from claude_multi.probe import ProbeError

from _catalog import FIXTURE_ROOT, SHIPPED_ROOT, uses_shipped_catalog
from _layout import REPO_ROOT, RESOURCES_ROOT
from _tier import real_binary_gate
import _v3


_CONTRACT_PATH = RESOURCES_ROOT / "catalog" / "native-contract.json"
_RUN_TIMEOUT = 150.0
_LIVE_TOUCH = "live daemon domain touched"
_HINT_ENV = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}
_CANARY = "cm-probe-canary"
_CANARY_MODEL = "cm-probe-unfenced-model"
_ONE_M = "[1m]"


def _request_model(selector: str) -> str:
    """What the client puts on the wire for a selector."""

    return selector.removesuffix(_ONE_M)


def _route(path: str) -> str:
    return path.split("?", 1)[0].rstrip("/")


def _reply(document: dict[str, Any], content: list[dict[str, Any]], message_id: str) -> Any:
    stop = "tool_use" if content and content[0].get("type") == "tool_use" else "end_turn"
    payload = probe._message_payload(
        content, model=document.get("model"), stop_reason=stop, message_id=message_id
    )
    return probe._sse_message(payload) if document.get("stream") else payload


def _agent_use(index: int, subagent_type: str, marker: str) -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": f"toolu_seed_probe_{index:02d}",
        "name": "Agent",
        "input": {
            "description": f"seed probe {index}",
            "subagent_type": subagent_type,
            "prompt": f"Reply with exactly: PROBE-OK {marker}",
        },
    }


def _workflow_use(markers: dict[str, str]) -> dict[str, Any]:
    lines = ["export const meta = { name: 'seed-probe', description: 'seed probe workflow' }"]
    names = []
    for index, (label, marker) in enumerate(markers.items()):
        lines.append(f"const r{index} = await agent('Reply {marker}', {{label: '{label}'}})")
        names.append(f"r{index}")
    lines.append(f"return [{', '.join(names)}]")
    return {
        "type": "tool_use",
        "id": "toolu_seed_probe_workflow",
        "name": "Workflow",
        "input": {"script": "\n".join(lines) + "\n"},
    }


class _ScriptedResponder:
    """Forces one tool-use step per main-loop request; keeps metadata rows.

    ``steps`` is a list of ``(tool name, make)`` pairs, ``make()`` returning
    the tool_use blocks of one assistant turn; the first main request gets
    step 0 (when it offers that tool; otherwise the tool is recorded in
    ``step_tools_missing``), the next main request step 1, and so on; every
    other request gets a plain PROBE-OK. A main request is one that offers ``Agent`` and
    whose gateway hint request class is neither ``subagent`` nor
    ``workflow`` (hint headers are on in every run). Rows keep only model
    and effort ids, tool names, marker names and hint safe ids.
    """

    def __init__(
        self,
        steps: list[tuple[str, Callable[[], list[dict[str, Any]]]]] | None = None,
        *,
        substrings: dict[str, str] | None = None,
    ):
        self._steps = list(steps or [])
        self._substrings = dict(substrings or {})
        self._lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []
        self.step_tools_missing: list[str] = []

    @staticmethod
    def is_main(row: dict[str, Any]) -> bool:
        return (
            row["max_tokens"] is not None
            and "Agent" in row["tool_names"]
            and row["request_class"] not in ("subagent", "workflow")
        )

    def respond(self, document: dict[str, Any], path: str, context: probe.RequestContext) -> Any:
        route = _route(path)
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return 404, {
                "type": "error",
                "error": {"type": "not_found_error", "message": "probe: unknown path"},
            }
        record = context.record
        hints = dict(record.hint_header_values)
        text = json.dumps(document) if self._substrings else ""
        row = {
            "ordinal": context.ordinal,
            "model": record.model,
            "effort": record.effort,
            "max_tokens": record.max_tokens,
            "tool_names": record.tool_names,
            "tool_names_truncated": record.tool_names_truncated,
            "markers": record.markers_found,
            "request_class": hints.get("x-claude-code-request-class"),
            "agent_type": hints.get("x-claude-code-agent-type"),
            "found": tuple(sorted(n for n, s in self._substrings.items() if s in text)),
        }
        content: list[dict[str, Any]] | None = None
        with self._lock:
            self.rows.append(row)
            if self._steps and self.is_main(row):
                tool, make = self._steps[0]
                if tool in record.tool_names:
                    self._steps.pop(0)
                    content = make()
                else:
                    self.step_tools_missing.append(tool)
        if content is None:
            content = [{"type": "text", "text": "PROBE-OK"}]
        return 200, _reply(document, content, f"msg_seed_probe_{context.ordinal:04d}")

    def main_rows(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if self.is_main(row)]

    def attributed(self, name: str) -> list[dict[str, Any]]:
        """Non-main requests carrying exactly this one marker."""

        return [
            row
            for row in self.rows
            if not self.is_main(row) and row["markers"] == (name,)
        ]


class _ProbeCase(unittest.TestCase):
    """Pinned-binary gate plus disposable fixtures and the gated runner."""

    trusted: probe.TrustedExecutable
    version: str = "unknown"
    boundary_skip: str | None = None
    probe_label = "seed probe"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="the scope-compiler probes")
        cls.boundary_skip = gate.boundary
        if gate.trusted is not None:
            cls.trusted = gate.trusted
            cls.version = gate.version

    def setUp(self) -> None:
        super().setUp()
        if self.boundary_skip is not None:
            self.skipTest(self.boundary_skip)
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-seed-probe-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        # Simulated live roots: the fixture stays disjoint from them and the
        # ambient credential scan sees none (the child never inherits it).
        self.environ = {"HOME": str(self.root / "live" / "home"), "PATH": "/usr/bin:/bin"}
        self._count = 0

    def _fixture(self) -> probe.ProbeFixture:
        self._count += 1
        fixture = probe.build_fixture(self.root / f"run-{self._count:02d}", environ=self.environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        return fixture

    @staticmethod
    def _git_init(fixture: probe.ProbeFixture) -> None:
        """A git work tree with one commit (worktree-isolated agents need it)."""

        env = {
            "HOME": str(fixture.home),
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "LANG": "C.UTF-8",
        }
        for command in (
            ["git", "init", "-q", "-b", "main"],
            [
                "git", "-c", "user.name=seed-probe", "-c", "user.email=probe@invalid",
                "commit", "-q", "--allow-empty", "-m", "fixture",
            ],
        ):
            subprocess.run(
                command, cwd=fixture.project_dir, env=env, check=True,
                capture_output=True, timeout=30,
            )

    def _boundary(self, exc: ProbeError) -> None:
        """A touched live domain is INCONCLUSIVE: a BOUNDARY skip, never a pass."""

        if _LIVE_TOUCH in str(exc):
            self.skipTest(f"BOUNDARY: {self.probe_label} INCONCLUSIVE: {exc}")

    def _run(
        self,
        argv: tuple[str, ...],
        *,
        fixture: probe.ProbeFixture,
        responder: Any,
        markers: dict[str, str] | None = None,
        child_env: dict[str, str] | None = None,
    ) -> tuple[probe.NativeRunResult, probe.FakeAnthropicProvider]:
        provider = probe.FakeAnthropicProvider(responder=responder, content_markers=markers)
        with provider:
            try:
                result = probe.run_native(
                    argv,
                    trusted=self.trusted,
                    fixture=fixture,
                    provider=provider,
                    timeout=_RUN_TIMEOUT,
                    allow_real=True,
                    environ=self.environ,
                    child_env=child_env,
                )
            except ProbeError as exc:
                self._boundary(exc)
                raise
        self.assertFalse(result.timed_out, "the pinned client timed out")
        self.assertIsNotNone(result.daemon)
        assert result.daemon is not None
        self.assertTrue(result.daemon.unchanged)
        return result, provider

    @staticmethod
    def _write_scope(fixture: probe.ProbeFixture, plan: scope.ScopePlan) -> Path:
        state_root = state.ensure_private_dir(fixture.root / "state")
        live = scope.write_scope(state_root, str(uuid.uuid4()), plan)
        probe.seed_fixture_project_trust(fixture, live)
        return live


def _lineup_catalog(root: Path) -> tuple[catalog.Catalog, profile.LineupCatalog]:
    cat = catalog.load_catalog(root)
    return cat, profile.LineupCatalog.from_catalog(cat)


def _effective(cat: catalog.Catalog, doc: dict[str, Any] | None = None) -> settings.Effective:
    return settings.effective(
        {"version": 1, **(doc or {})}, provider_ids=cat.providers, line_keys=cat.lines
    )


# ---------------------------------------------------------------- per seed

_CANARY_AGENT = f"""---
name: {_CANARY}
description: Synthetic probe canary agent (unfenced model).
model: {_CANARY_MODEL}
---
You are the {_CANARY} fixture agent.
"""

_SUBAGENT_RECORDER = """\
import json, os, sys
event = json.load(sys.stdin)
row = {
    "hook_event_name": event.get("hook_event_name"),
    "agent_type": event.get("agent_type"),
    "has_agent_id": isinstance(event.get("agent_id"), str) and bool(event.get("agent_id")),
}
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
try:
    os.write(fd, (json.dumps(row, sort_keys=True) + "\\n").encode("utf-8"))
finally:
    os.close(fd)
print("{}")
"""


@uses_shipped_catalog
class SeedDelegationProbe(_ProbeCase):
    """Per-seed delegation on the SHIPPED catalog."""

    probe_label = "per-seed delegation"

    def _boundary(self, exc: ProbeError) -> None:
        if _LIVE_TOUCH in str(exc):
            print(f"per-seed delegation outcome: fail-closed: {exc}")
            self.skipTest(f"BOUNDARY: per-seed delegation INCONCLUSIVE: {exc}")

    def _install_recorder(self, fixture: probe.ProbeFixture) -> Path:
        events = fixture.root / "subagent-start.jsonl"
        script = fixture.root / "record-subagent-start.py"
        script.write_text(f"#!{sys.executable}\n" + _SUBAGENT_RECORDER, encoding="utf-8")
        script.chmod(0o700)
        command = f"{shlex.quote(str(script))} {shlex.quote(str(events))}"
        document = {
            "hooks": {
                "SubagentStart": [
                    {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
                ]
            }
        }
        state.atomic_write(
            fixture.claude_config_dir / "settings.json",
            strict_json.canonical_file_bytes(document),
        )
        return events

    @staticmethod
    def _write_canary(fixture: probe.ProbeFixture) -> None:
        claude_dir = state.ensure_private_dir(fixture.project_dir / ".claude")
        agents = state.ensure_private_dir(claude_dir / "agents")
        state.atomic_write(agents / f"{_CANARY}.md", _CANARY_AGENT.encode("utf-8"))

    def _one_seed(
        self,
        name: str,
        lineup: profile.ResolvedLineup,
        cat: catalog.Catalog,
        lcat: profile.LineupCatalog,
        eff: settings.Effective,
    ) -> dict[str, Any]:
        plan = scope.compile_lineup_scope(
            lineup, lcat, eff, cat.prompt_bodies, scope.catalog_meta_v2(cat.docs),
            lineup_generation=1,
        )
        # Lifecycle-free: the probe's own provider/token env must stay in
        # charge (no compiled base URL, helper or hooks).
        self.assertNotIn("ANTHROPIC_BASE_URL", plan.settings["env"])
        self.assertNotIn("apiKeyHelper", plan.settings)
        self.assertNotIn("hooks", plan.settings)
        fixture = self._fixture()
        self._git_init(fixture)
        live = self._write_scope(fixture, plan)
        self._write_canary(fixture)
        events_path = self._install_recorder(fixture)

        # (e) each compiled agent file names its binding's selector.
        for rid, agent in lineup.agents.items():
            text = (live / ".claude" / "agents" / f"{rid}.md").read_text(encoding="utf-8")
            self.assertIn(f"\nmodel: {agent.binding.selector}\n", text, rid)

        spawns = [*lineup.agents, _CANARY]
        markers = {sid: f"SEEDPROBE-SEED-MARK-{index:02d}" for index, sid in enumerate(spawns)}
        responder = _ScriptedResponder(
            [
                (
                    "Agent",
                    lambda: [
                        _agent_use(index, sid, markers[sid])
                        for index, sid in enumerate(spawns)
                    ],
                )
            ]
        )
        lead_selector = lineup.lead.binding.selector
        lead_model = _request_model(lead_selector)
        result, _provider = self._run(
            (
                "-p",
                "seed per-seed delegation probe turn",
                "--settings",
                str(live / "settings.json"),
                "--add-dir",
                str(live),
                "--model",
                lead_selector,
                "--effort",
                lineup.lead.session_effort,
                "--dangerously-skip-permissions",
            ),
            fixture=fixture,
            responder=responder,
            markers=markers,
            child_env=dict(_HINT_ENV),
        )
        self.assertEqual(result.returncode, 0, f"{name}: client exit status")
        self.assertEqual(responder.step_tools_missing, [], f"{name}: Agent never offered")
        main = responder.main_rows()
        self.assertTrue(main, f"{name}: no main-loop request")
        # (c) the lead runs on its compiled selector.
        self.assertEqual(main[0]["model"], lead_model, f"{name}: lead request model")

        agents: dict[str, Any] = {}
        for rid, agent in lineup.agents.items():
            binding = agent.binding
            rows = responder.attributed(rid)
            # (a) every bound id dispatches on its binding.
            self.assertTrue(rows, f"{name}/{rid}: no attributed subagent request")
            self.assertEqual(
                {row["model"] for row in rows}, {_request_model(binding.selector)},
                f"{name}/{rid}: subagent request model",
            )
            # (b) client-effort lines carry the frontmatter effort on the wire.
            if binding.mode == "client":
                self.assertEqual(
                    {row["effort"] for row in rows}, {binding.effort},
                    f"{name}/{rid}: client-effort wire effort",
                )
            # (f) read-only roles are denied their tools on the wire.
            for row in rows:
                self.assertFalse(row["tool_names_truncated"], f"{name}/{rid}")
                if agent.role.disallowed_tools:
                    self.assertFalse(
                        set(agent.role.disallowed_tools) & set(row["tool_names"]),
                        f"{name}/{rid}: disallowed tools offered {row['tool_names']}",
                    )
            if not agent.role.disallowed_tools:
                self.assertTrue(
                    any("Edit" in row["tool_names"] for row in rows),
                    f"{name}/{rid}: a writable role was offered no Edit tool",
                )
            agents[rid] = {
                "selector": binding.selector,
                "mode": binding.mode,
                "effort": binding.effort,
                "requests": len(rows),
                "wire_models": sorted({row["model"] for row in rows}),
                "wire_efforts": sorted({str(row["effort"]) for row in rows}),
                "tools_withheld": list(agent.role.disallowed_tools),
            }
        # (d)/(h) the unfenced canary falls back to the lead: the flag
        # settings (fence) were loaded and enforced in this process.
        canary = responder.attributed(_CANARY)
        self.assertTrue(canary, f"{name}: the canary never ran")
        self.assertEqual({row["model"] for row in canary}, {lead_model}, f"{name}: canary")
        if lineup.is_direct:
            self.assertEqual(lineup.agents, {})
            attributed = {
                marker for row in responder.rows if not responder.is_main(row)
                for marker in row["markers"]
            }
            self.assertEqual(attributed, {_CANARY}, f"{name}: direct spawned more")

        # (g) one SubagentStart per spawn, with agent_type and agent_id.
        events = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line
        ] if events_path.exists() else []
        starts = [row for row in events if row["hook_event_name"] == "SubagentStart"]
        self.assertEqual(
            sorted(row["agent_type"] for row in starts), sorted(spawns),
            f"{name}: SubagentStart agent_type set",
        )
        self.assertTrue(all(row["has_agent_id"] for row in starts), f"{name}: agent_id")

        record = {
            "version": 1,
            "seed": name,
            "client": self.version,
            "lead": {
                "selector": lead_selector,
                "session_effort": lineup.lead.session_effort,
                "wire_model": main[0]["model"],
                "wire_effort": main[0]["effort"],
            },
            "agents": agents,
            "canary": sorted({row["model"] for row in canary}),
            "subagent_start": sorted(row["agent_type"] for row in starts),
        }
        probe.write_evidence(fixture, f"seed-{name}", record)
        return record

    def test_every_shipped_seed_dispatches_its_bindings(self) -> None:
        cat, lcat = _lineup_catalog(SHIPPED_ROOT)
        eff = _effective(cat)
        passed: list[dict[str, Any]] = []
        for name in catalog.SEED_PROFILE_NAMES:
            with self.subTest(seed=name):
                lineup = profile.resolve(cat.seed_profiles[name], lcat, effective=eff)
                passed.append(self._one_seed(name, lineup, cat, lcat, eff))
        if len(passed) != len(catalog.SEED_PROFILE_NAMES):
            return  # a failed subTest already failed the test; no evidence line
        wires = {
            model
            for record in passed
            for agent in record["agents"].values()
            for model in agent["wire_models"]
        }
        print(
            upgrade.ESSENTIAL_EVIDENCE_PREFIXES[4]
            + f"seeds={len(passed)} "
            f"agents={sum(len(record['agents']) for record in passed)} "
            f"distinct_wires={len(wires)} canary=lead-x{len(passed)} "
            f"client={self.version}"
        )


# -------------------------------------------------------------- placement


class SettingsEnvPlacementProbe(_ProbeCase):
    """Flag-settings env placement and the workflow default."""

    probe_label = "settings-env placement"

    def setUp(self) -> None:
        super().setUp()
        self.cat, self.lcat = _lineup_catalog(FIXTURE_ROOT)
        # The spec's test-local profile: lead sol at ultracode, no cm-*
        # agents, every native agent on.
        self.document = {
            "version": 2,
            "name": "seed-placement",
            "lead": {"model": "sol", "effort": "ultracode"},
            "agents": {},
            "native_agents": {"explore": "native", "general_purpose": "on", "plan": "native"},
            "workflows": "native",
        }

    def _compile(self, doc: dict[str, Any] | None, lead: str | None = None
                 ) -> tuple[profile.ResolvedLineup, scope.ScopePlan]:
        eff = _effective(self.cat, doc)
        document = self.document if lead is None else {**self.document, "lead": {"model": lead, "effort": "ultracode"}}
        lineup = profile.resolve(document, self.lcat, effective=eff)
        plan = scope.compile_lineup_scope(
            lineup, self.lcat, eff, self.cat.prompt_bodies,
            scope.catalog_meta_v2(self.cat.docs), lineup_generation=1,
        )
        return lineup, plan

    def _spawn(
        self,
        doc: dict[str, Any] | None,
        steps: list[tuple[str, Callable[[], list[dict[str, Any]]]]],
        markers: dict[str, str],
        lead: str | None = None,
    ) -> tuple[profile.ResolvedLineup, scope.ScopePlan, _ScriptedResponder]:
        lineup, plan = self._compile(doc, lead)
        fixture = self._fixture()
        self._git_init(fixture)
        live = self._write_scope(fixture, plan)
        responder = _ScriptedResponder(steps)
        result, _provider = self._run(
            (
                "-p",
                "seed settings-env placement probe turn",
                "--settings",
                str(live / "settings.json"),
                "--model",
                lineup.lead.binding.selector,
                "--effort",
                lineup.lead.session_effort,
                "--dangerously-skip-permissions",
            ),
            fixture=fixture,
            responder=responder,
            markers=markers,
            child_env=dict(_HINT_ENV),
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(responder.step_tools_missing, [])
        main = responder.main_rows()
        self.assertTrue(main)
        self.assertEqual(main[0]["model"], _request_model(lineup.lead.binding.selector))
        return lineup, plan, responder

    def _explore_model(self, doc: dict[str, Any] | None, lead: str | None = None
                       ) -> tuple[profile.ResolvedLineup, scope.ScopePlan, str]:
        marker = {"explore": "SEEDPROBE-PLACE-EXPLORE"}
        lineup, plan, responder = self._spawn(
            doc, [("Agent", lambda: [_agent_use(0, "Explore", marker["explore"])])], marker, lead
        )
        rows = responder.attributed("explore")
        self.assertTrue(rows, "native Explore never ran")
        self.assertTrue(all(row["agent_type"] == "Explore" for row in rows))
        models = {row["model"] for row in rows}
        self.assertEqual(len(models), 1, models)
        return lineup, plan, models.pop()

    def test_explore_cap_flag_in_settings_env_keeps_the_lead(self) -> None:
        # (i) default Settings: the cap flag compiled into FLAG settings env.
        lineup, plan, model = self._explore_model(None)
        self.assertEqual(plan.settings["env"].get("CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP"), "1")
        self.assertEqual(model, _request_model(lineup.lead.binding.selector))

    def _anthropic_non_opus_lead(self) -> str:
        """A lead-capable Anthropic line the client recognises but that is
        not Opus (the cap's subject on 2.1.284+)."""

        for key in sorted(self.cat.lines):
            entry = self.cat.lines[key]
            if (catalog.line_family(entry, self.cat.providers) == "anthropic"
                    and "lead" in entry["capabilities"] and "opus" not in entry["wire_model"]):
                return key
        self.fail("the fixture has no non-Opus Anthropic lead line")

    def test_family_default_in_settings_env_caps_explore(self) -> None:
        # (ii) cap on: under a recognised non-Opus Anthropic lead Explore goes
        # to the Opus family default, which the client reads from FLAG
        # settings env (fenced as fallback-only). On 2.1.284+, under an
        # unrecognised proxy lead (the GPT fixture lead) Explore inherits the
        # lead even with the cap on, so the cap's subject is a Fable lead.
        _lineup, _plan, inherited = self._explore_model({"explore_inherit_cap_disabled": False})
        self.assertEqual(inherited, _request_model(_lineup.lead.binding.selector))
        lineup, plan, model = self._explore_model({"explore_inherit_cap_disabled": False},
                                                  self._anthropic_non_opus_lead())
        env = plan.settings["env"]
        self.assertNotIn("CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP", env)
        opus_default = env["ANTHROPIC_DEFAULT_OPUS_MODEL"]
        self.assertIn(opus_default, plan.settings["availableModels"])
        self.assertNotIn(
            opus_default, [row["model"] for row in plan.settings["modelPicker"]["options"]]
        )
        self.assertEqual(model, _request_model(opus_default))
        self.assertNotEqual(model, _request_model(lineup.lead.binding.selector))

    def _workflow_default_line(self) -> tuple[str, str]:
        """An offered agents-capable non-Anthropic line other than the lead's."""

        lead_key = self.document["lead"]["model"]
        for key in sorted(self.cat.lines):
            entry = self.cat.lines[key]
            provider = self.cat.providers[entry["provider"]]
            if (
                key != lead_key
                and "agents" in entry["capabilities"]
                and entry.get("status", "active") == "active"
                and catalog.line_family(entry, self.cat.providers) != "anthropic"
                and provider.get("adapter") is not None
            ):
                return key, entry["default_effort"]
        self.fail("the fixture has no agents-capable non-Anthropic line")

    def test_workflow_default_binding_reaches_workflow_and_general_purpose_agents(self) -> None:
        # (iii) what CLAUDE_CODE_SUBAGENT_MODEL in flag
        # settings env does to a workflow agent() without an agentType and
        # to the native Explore / general-purpose / Plan agents (default
        # Settings: Explore inherit cap disabled).
        key, effort = self._workflow_default_line()
        doc = {"workflow_default_binding": {"model": key, "effort": effort}}
        markers = {
            "explore": "SEEDPROBE-PLACE-WFD-EXPLORE",
            "general": "SEEDPROBE-PLACE-WFD-GENERAL",
            "plan": "SEEDPROBE-PLACE-WFD-PLAN",
            "workflow": "SEEDPROBE-PLACE-WFD-WORKFLOW",
        }
        types = {"explore": "Explore", "general": "general-purpose", "plan": "Plan"}
        lineup, plan, responder = self._spawn(
            doc,
            [
                (
                    "Agent",
                    lambda: [
                        _agent_use(index, types[label], markers[label])
                        for index, label in enumerate(types)
                    ],
                ),
                ("Workflow", lambda: [_workflow_use({"wa": markers["workflow"]})]),
            ],
            markers,
        )
        selector = plan.settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"]
        self.assertIn(selector, plan.settings["availableModels"])
        observed = {}
        for label in markers:
            rows = responder.attributed(label)
            self.assertTrue(rows, f"{label} never ran")
            expected_type = types.get(label, "workflow-subagent")
            self.assertEqual({row["agent_type"] for row in rows}, {expected_type}, label)
            models = {row["model"] for row in rows}
            self.assertEqual(len(models), 1, (label, models))
            observed[label] = models.pop()
        default_model = _request_model(selector)
        lead_model = _request_model(lineup.lead.binding.selector)
        self.assertNotEqual(default_model, lead_model)
        # Pinned on the client (the compiled lineup.md/appendix line and the
        # doctor radar text follow it): the workflow default reaches workflow
        # agents without an agentType and native
        # general-purpose; native Explore (cap disabled) and Plan stay on
        # the lead.
        self.assertEqual(
            observed,
            {
                "explore": lead_model,
                "general": default_model,
                "plan": lead_model,
                "workflow": default_model,
            },
        )


# -------------------------------------------------------------- hook shim 3

_WRAPPER = """#!/bin/sh
printf '%s\\n' "${{2-}}" >> {log}
CLAUDE_MULTI_HOOK_COMMAND={wrapper}
export CLAUDE_MULTI_HOOK_COMMAND
PYTHONPATH={src}
export PYTHONPATH
exec {python} {launcher} "$@"
"""

_JUNK_STUB = """#!/bin/sh
cat >/dev/null
printf '%s\\n' "${{2-}}" >> {log}
printf '%s\\n' '{marker}'
exit 2
"""

_NOTICE_HEAD = "lineup_generation {generation}]"
# ASCII openings of the SessionStart prefaces (hooks.notice_text).
_PREFACES = {
    "startup": "Session started. The lineup below is current.",
    "resume": "Session resumed. Your recorded lead is",
    "fork": "This is a native fork that shares the parent session",
}


class HookShim3RealBinaryTests(_ProbeCase):
    """The protocol-3 notice through the real shim-3 and launcher."""

    probe_label = "hook shim 3"
    GENERATION = 4242

    @staticmethod
    def _state_root(fixture: probe.ProbeFixture) -> Path:
        # sessions.state_root of the client env the hooks inherit.
        state.ensure_private_dir(fixture.xdg_state_home)
        return state.ensure_private_dir(fixture.xdg_state_home / "claude-multi")

    @staticmethod
    def _hook_entry(command: str) -> list[dict[str, Any]]:
        return [{"hooks": [{"type": "command", "command": command, "timeout": 5}]}]

    @staticmethod
    def _lead_requests(provider: probe.FakeAnthropicProvider) -> list[probe.RequestRecord]:
        return [
            record
            for record in provider.requests
            if record.max_tokens is not None and "Agent" in record.tool_names
        ]

    def test_failing_launcher_never_blocks_the_prompt(self) -> None:
        fixture = self._fixture()
        state_root = self._state_root(fixture)
        junk = "SEEDPROBE-P27-JUNK-MARKER"
        stub = fixture.root / "junk-launcher"
        ran = fixture.root / "junk-launcher.log"
        stub.write_text(
            _JUNK_STUB.format(marker=junk, log=shlex.quote(str(ran))), encoding="utf-8"
        )
        stub.chmod(0o700)
        shim = scope.hook_shim_v3_path(state_root)
        state.ensure_private_dir(shim.parent)
        state.atomic_write(shim, scope.hook_shim_v3_text(state_root, str(stub)))
        shim.chmod(0o700)
        managed = str(uuid.uuid4())
        command = (
            f"{shlex.quote(str(shim))} session-event prompt --managed-id {managed} "
            f"--launch-epoch 0 --hook-protocol {scope.HOOK_PROTOCOL}"
        )
        flag = fixture.root / "flag-settings.json"
        state.atomic_write(
            flag,
            strict_json.canonical_file_bytes(
                {"hooks": {"UserPromptSubmit": self._hook_entry(command)}}
            ),
        )
        result, provider = self._run(
            ("-p", "seed p27 probe turn", "--settings", str(flag)),
            fixture=fixture,
            responder=_ScriptedResponder(),
            markers={"junk": junk},
        )
        self.assertEqual(result.returncode, 0)
        # The fast path missed (no scope), so the failing launcher ran once.
        self.assertEqual(ran.read_text(encoding="utf-8").split(), ["prompt"])
        lead = self._lead_requests(provider)
        self.assertEqual(len(lead), 1, "the prompt did not reach the lead exactly once")
        self.assertNotIn("junk", lead[0].markers_found)
        self.assertNotIn("blocked by hook", result.stdout)
        self.assertNotIn("blocked by hook", result.stderr)

    # ------------------------------------------------------------- notice

    def _notice_fixture(self) -> dict[str, Any]:
        fixture = self._fixture()
        state_root = self._state_root(fixture)
        log = fixture.root / "launcher-events.log"
        wrapper = fixture.root / "claude-multi-wrapper"
        wrapper.write_text(
            _WRAPPER.format(
                log=shlex.quote(str(log)),
                wrapper=shlex.quote(str(wrapper.resolve())),
                src=shlex.quote(str(REPO_ROOT / "src")),
                python=shlex.quote(sys.executable),
                launcher=shlex.quote(str(REPO_ROOT / "bin" / "claude-multi")),
            ),
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        resolved = str(wrapper.resolve())
        shim = scope.hook_shim_v3_path(state_root)
        state.ensure_private_dir(shim.parent)
        state.atomic_write(shim, scope.hook_shim_v3_text(state_root, resolved))
        shim.chmod(0o700)
        shim_bytes = shim.read_bytes()

        # The v3 record the hook's Runtime reconciles: built exactly as the
        # hook will see it (package asset root, the client's fixture env with
        # the wrapper's CLAUDE_MULTI_HOOK_COMMAND), so its shim refresh
        # writes identical bytes.
        env = fixture.environ()
        env["CLAUDE_MULTI_HOOK_COMMAND"] = resolved
        runtime = cli.Runtime(
            asset_root=RESOURCES_ROOT, environ=env, cwd=fixture.project_dir,
            allow_state_writes=False,
        )
        self.assertEqual(runtime.session_store.root, state_root)
        self.assertEqual(shim.read_bytes(), shim_bytes)
        managed = str(uuid.uuid4())
        # The package catalog has no default composition any more;
        # the 2.x input is the balanced seed's v1 projection (what
        # compositions.load("default") returned before), built by tests/_v3.py.
        document = _v3.v1_projection(
            runtime.catalog.seed_profiles["balanced"],
            _v3.v1_models_view(runtime.catalog.docs)["models"],
        )
        record = _v3.make_record(
            managed_id=managed,
            cwd=str(fixture.project_dir),
            composition_name=document["name"],
            snapshot=_v3.snapshot_for(runtime.catalog.docs, document),
            catalog_version=runtime.catalog_version,
            catalog_hash=runtime.catalog.bundle_sha256,
            launcher_version=runtime.launcher_version,
            mode="durable",
            scope_generation=1,
            launch_epoch=1,
        )
        _v3.save_any(runtime.session_store, record)

        # The v2 scope: managed compile of the FIXTURE seed balanced.
        cat, lcat = _lineup_catalog(FIXTURE_ROOT)
        eff = _effective(cat)
        lineup = profile.resolve(cat.seed_profiles["balanced"], lcat, effective=eff)
        plan = scope.compile_lineup_scope(
            lineup, lcat, eff, cat.prompt_bodies, scope.catalog_meta_v2(cat.docs),
            lineup_generation=self.GENERATION, managed_id=managed,
            hook_command=str(shim), launch_epoch=1,
        )
        scope.write_scope(state_root, managed, plan)
        # Only the compiled hooks: the probe's provider URL/token stay.
        flag = fixture.root / "flag-settings.json"
        state.atomic_write(
            flag, strict_json.canonical_file_bytes({"hooks": plan.settings["hooks"]})
        )
        return {
            "fixture": fixture,
            "state": state_root,
            "log": log,
            "shim": shim,
            "shim_bytes": shim_bytes,
            "managed": managed,
            "flag": flag,
            "model": record["snapshot"]["lead"]["client_selector"],
            "gen": plan.other_files[scope.LINEUP_GEN].decode("utf-8").strip(),
        }

    def _notice_run(self, setup: dict[str, Any], name: str, *extra: str) -> tuple[list[dict[str, Any]], list[str]]:
        before = setup["log"].read_text(encoding="utf-8") if setup["log"].exists() else ""
        head = _NOTICE_HEAD.format(generation=self.GENERATION)
        responder = _ScriptedResponder(
            substrings={"notice": head, "failed": "could not record this session start", **_PREFACES}
        )
        result, _provider = self._run(
            (
                "-p", f"seed notice probe {name} turn",
                "--settings", str(setup["flag"]),
                "--model", setup["model"],
                *extra,
            ),
            fixture=setup["fixture"],
            responder=responder,
            child_env=dict(_HINT_ENV),
        )
        self.assertEqual(result.returncode, 0, f"{name}: client exit status")
        self.assertEqual(setup["shim"].read_bytes(), setup["shim_bytes"], name)
        after = setup["log"].read_text(encoding="utf-8") if setup["log"].exists() else ""
        events = after[len(before):].split()
        return responder.main_rows(), events

    @uses_shipped_catalog
    def test_notice_on_startup_resume_and_fork_through_the_real_shim(self) -> None:
        """Startup, resume and fork notices through shim-3 and the launcher.

        The scope is the FIXTURE ``balanced`` compile at generation 4242; the
        hook's own Runtime necessarily loads the package catalog, so the v3
        record it reconciles is built from that catalog's default composition
        (ids derived at runtime) and the lead ``--model`` is that record's.
        """

        setup = self._notice_fixture()
        managed, state_root = setup["managed"], setup["state"]
        observed: dict[str, Any] = {}
        runs = (
            ("startup", ("--session-id", managed)),
            ("resume", ("--resume", managed)),
            ("fork", ("--resume", managed, "--fork-session")),
        )
        for name, extra in runs:
            seen_before = set(os.listdir(state_root / hooks.NOTICE_DIR)) if (
                state_root / hooks.NOTICE_DIR
            ).is_dir() else set()
            main, events = self._notice_run(setup, name, *extra)
            self.assertTrue(main, f"{name}: no lead request")
            # The SessionStart notice reached the lead's first request.
            self.assertIn("notice", main[0]["found"], f"{name}: no lineup notice")
            self.assertIn(name, main[0]["found"], f"{name}: preface missing")
            # The reconcile succeeded (a failure would add this line).
            self.assertNotIn("failed", main[0]["found"], f"{name}: start not recorded")
            # On the real client payload: SessionStart
            # wrote .seen, so the real UserPromptSubmit payload took the sh
            # fast path and never started the launcher.
            self.assertIn("start", events, f"{name}: launcher events {events}")
            self.assertNotIn("prompt", events, f"{name}: launcher events {events}")
            seen_after = set(os.listdir(state_root / hooks.NOTICE_DIR))
            observed[name] = {"events": events, "new_seen": sorted(seen_after - seen_before)}
        self.assertEqual(hooks.read_seen(state_root, managed), setup["gen"])
        forks = observed["fork"]["new_seen"]
        self.assertEqual(len(forks), 1, observed)
        fork_id = forks[0].removesuffix(hooks.SEEN_SUFFIX)
        self.assertNotEqual(fork_id, managed)
        self.assertEqual(hooks.read_seen(state_root, fork_id), setup["gen"])


if __name__ == "__main__":
    unittest.main()
