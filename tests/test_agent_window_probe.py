"""Exact-client probe: the lead and every agent compact at the session window.

Network-hermetic like every real-binary probe (``bwrap --unshare-net``,
loopback fake provider, fixture homes, fixture lines chosen by shape).
Responders keep metadata only: request models, counts, the scripted usage
they reported and the hint-header ids; never prompt, answer or transcript
text.

Four runs of the production compile on the pinned client, under one session
policy (the window ceiling over a 1M-class lead set, at the default percent):

* ``lead``: the lead compacts at the session window's trigger;
* ``agent``: a ``[1m]`` agent compacts at that same trigger;
* ``no-window``: the same agent without the window policy (neither the
  process environment nor the compiled settings) does not compact there;
* ``bounded``: an agent whose line's provider bound is below the session
  window runs on the alias without ``[1m]`` and compacts at the 200K class's
  trigger, below its bound.

Every run must reach its intended model: the requests of its target name the
role's selector as the client sends it (without ``[1m]``), so an agent that
fell back to the lead's model, or ran on any other model, is never evidence.

The output line is a stable interface that release evidence reads:
``agent window probe: <STATUS> class=<class> <detail>``. ``PASS`` (with
:data:`PASS_CLASS`) is printed only on positive evidence from all four runs;
a missing pinned client or isolation boundary prints ``BOUNDARY``.
"""

from __future__ import annotations

import copy
import json
import unittest
import uuid

import test_scope_probe_client as client_probe
from _catalog import FIXTURE_ROOT
from _tier import real_binary_gate
from claude_multi import catalog, compiler, probe, profile, scope, settings
from test_agent_context import BELOW, bounded, codex_line, seed_lead

LINE_PREFIX = "agent window probe:"
PASS_CLASS = "window-reaches-agents"
_HINTS = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}
_AGENT = "cm-analyst"
_WINDOW_ENV = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_MAX_CONTEXT_TOKENS")
_RUNS = ("lead", "agent", "no-window", "bounded")
_AGENT_RUNS = ("agent", "no-window", "bounded")


def line(status: str, observed: str, detail: str) -> str:
    """The probe's one output line."""

    return f"{LINE_PREFIX} {status} class={observed} {detail}".rstrip()


def wire(selector: str) -> str:
    """The model a request names for ``selector``: the pinned client sends
    the alias without ``[1m]``."""

    return selector.removesuffix(profile.SUFFIX_1M)


# ------------------------------------------------------------ the compile


def production_compile(*, provider_tokens: int | None = None) -> dict:
    """The production compile of a fixture lineup: the direct seed's 1M-class
    lead (its lead set kept to the lead's provider) and ``cm-analyst`` on a
    1M-class gateway-effort line. ``provider_tokens`` lowers that line's
    provider bound (``BELOW`` the session window: the agent keeps the 200K
    class). Returns the lineup, the scope plan, the session policy and each
    role's window."""

    bundle = catalog.load_catalog(FIXTURE_ROOT)
    docs = copy.deepcopy(bundle.docs)
    key = codex_line(docs)
    if provider_tokens is not None:
        docs = bounded(docs, key, provider_tokens)
    lcat = profile.LineupCatalog.from_docs(docs)
    eff = settings.effective({"version": 1}, provider_ids=lcat.providers, line_keys=lcat.lines)
    document = copy.deepcopy(bundle.seed_profiles["direct"])
    document.pop("seed", None)
    lead_key, lead_provider = seed_lead(bundle)
    document.update(name="agent-window", lead_providers=[lead_provider])
    document["lead"]["model"] = lead_key
    document["agents"] = {_AGENT: {"model": key, "effort": docs["models"]["models"][key]["default_effort"]}}
    lineup = profile.resolve(document, lcat, effective=eff)
    plan = scope.compile_lineup_scope(lineup, lcat, eff, bundle.prompt_bodies, scope.catalog_meta_v2(docs),
                                      lineup_generation=1)
    fence = scope.compile_fence(lineup, lcat, eff)
    context = compiler.lead_set_context(fence, settings.compaction_percent_for(eff, lineup.settings_overrides),
                                        ceiling=profile.window_ceiling(eff))
    windows = dict(profile.lineup_windows(lineup))
    return {"lineup": lineup, "plan": plan, "context": context, "lead": windows[profile.LEAD_ROLE],
            "agent": windows[_AGENT], "bound": docs["models"]["models"][key]["context"]["provider_tokens"]}


def _schedule(trigger: int, top: int) -> tuple[int, ...]:
    """Scripted usage per work step: below the trigger, one step short of
    it, exactly at it, then up to ``top`` (a run that never compacts ends
    there)."""

    return tuple(sorted({40_000, 100_000, trigger - 100_000, trigger - 1_000, trigger, trigger + 1_000, top}))


# ------------------------------------------------------------ the verdict


def window_class(observed: dict, expected: dict) -> str:
    """The class from four runs, :data:`PASS_CLASS` only on positive
    evidence: every run's target requests name exactly its intended wire
    model (``expected["wires"]``; an agent run on the lead's model is a
    fallback), the lead and the ``[1m]`` agent compact first exactly at the
    session trigger, the bounded agent at the 200K-class trigger, the
    no-window control passes the session trigger without compacting, and
    all three agent runs complete without a failed result. A control that
    never reached its stimulus, or a lead and agent that share a wire model
    (a fallback would be invisible), raises ``Inconclusive``."""

    for name in _RUNS:
        if not observed[name]["work"]:
            raise client_probe._Inconclusive(f"{name}: the target never ran a scripted step")
    for name in _AGENT_RUNS:
        if not observed[name]["spawned"]:
            raise client_probe._Inconclusive(f"{name}: the lead never spawned the agent")
    control = observed["no-window"]
    if control["peak"] <= expected["trigger"]:
        raise client_probe._Inconclusive("no-window: the control never passed the session trigger")
    wires = expected["wires"]
    if any(wires[name] == wires["lead"] for name in _AGENT_RUNS):
        raise client_probe._Inconclusive("the agent and the lead share a wire model: a fallback would be invisible")
    if observed["lead"]["target_models"] != [wires["lead"]]:
        return "lead-model-wrong"
    for name in _AGENT_RUNS:
        models = observed[name]["target_models"]
        if wires["lead"] in models:
            return "agent-fell-back-to-lead"
        if models != [wires[name]]:
            return "agent-model-wrong"
    if control["summaries"]:
        return "window-control-compacted"

    def at(name: str, trigger: int) -> bool:
        row = observed[name]
        return row["summaries"] > 0 and row["first_summary_after"] == trigger

    if not at("lead", expected["trigger"]):
        return "window-not-applied-to-lead"
    if not at("agent", expected["trigger"]):
        return "window-not-applied-to-agents"
    if not at("bounded", expected["bounded_trigger"]):
        return "bounded-agent-class-wrong"
    if not all(observed[name]["completed_result"] and not observed[name]["failed_result"] for name in _AGENT_RUNS):
        return "agent-run-failed"
    return PASS_CLASS


class _WindowResponder(client_probe._Recorder):
    """The target (the lead, or the agent the lead spawns) runs a scripted
    Bash loop whose reported usage follows ``schedule``; after its first
    compaction summary it finishes. Child requests are identified by the
    agent-id hint header (never by model), compaction summaries by the
    client's ``compaction`` request class."""

    def __init__(self, target: str, schedule: tuple[int, ...]):
        super().__init__()
        self.target = target
        self.schedule = schedule
        self.spawned = False
        self.work = 0
        self.summaries = 0
        self.first_summary_after: int | None = None
        self.last_usage: int | None = None
        self.peak = 0
        self.target_models: set[str] = set()
        self.completed_result = False
        self.failed_result = False

    def message(self, document, record, hints):
        child = bool(hints.get("x-claude-code-agent-id"))
        request_class = hints.get("x-claude-code-request-class")
        if request_class == "auxiliary":
            return client_probe._reply(document)
        target = child if self.target == "agent" else not child
        if target and request_class == "compaction":
            self.summaries += 1
            if self.first_summary_after is None:
                self.first_summary_after = self.last_usage
            return client_probe._reply(document)
        if target:
            self.target_models.add(record.model)
            if self.summaries or self.work >= len(self.schedule):
                return client_probe._reply(document)
            self.work += 1
            usage = self.schedule[self.work - 1]
            self.last_usage, self.peak = usage, max(self.peak, usage)
            return client_probe._reply(document, [client_probe._tool("Bash", 100 + self.work, command="printf window-step",
                                                   description="fixture progress")],
                              usage=usage, stop="tool_use")
        if not self.spawned and "Agent" in record.tool_names:
            self.spawned = True
            return client_probe._reply(document, [client_probe._spawn(1, _AGENT)], stop="tool_use")
        for result in client_probe._results(document):
            if result.get("tool_use_id") != "toolu_probe_1":
                continue
            text = json.dumps(result.get("content", "")).lower()
            failed = bool(result.get("is_error")) or "prompt is too long" in text
            self.failed_result |= failed
            self.completed_result |= not failed
        return client_probe._reply(document)

    def observation(self) -> dict:
        return {"spawned": self.spawned, "work": self.work, "summaries": self.summaries,
                "first_summary_after": self.first_summary_after, "peak": self.peak,
                "target_models": sorted(self.target_models), "completed_result": self.completed_result,
                "failed_result": self.failed_result}


# ------------------------------------------------------------ pure checks


class ProductionCompileTests(unittest.TestCase):
    """What the probe runs: the production compile's selectors and policy."""

    def test_the_session_policy_reaches_both_environments(self) -> None:
        case = production_compile()
        context, plan = case["context"], case["plan"]
        self.assertEqual(context.window, settings.WINDOW_CEILING_DEFAULT)
        self.assertEqual(context.percent, settings.COMPACTION_PERCENT_DEFAULT)
        self.assertEqual(plan.settings["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], str(context.window))
        self.assertEqual(plan.settings["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], str(context.percent))
        self.assertEqual(probe.ProbeCompactionPolicy(context.window, context.percent).environ(),
                         {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(context.window),
                          "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(context.percent)})

    def test_a_1m_agent_runs_at_the_session_window(self) -> None:
        case = production_compile()
        lead, agent = case["lead"], case["agent"]
        self.assertTrue(lead.selector.endswith(profile.SUFFIX_1M))
        self.assertTrue(agent.selector.endswith(profile.SUFFIX_1M))
        self.assertGreaterEqual(case["bound"], case["context"].window)
        self.assertEqual((lead.window, lead.trigger), (case["context"].window, case["context"].trigger))
        self.assertEqual((agent.window, agent.trigger), (lead.window, lead.trigger))
        self.assertIn(agent.selector, case["plan"].settings["availableModels"])

    def test_a_bound_below_the_window_keeps_the_200k_class(self) -> None:
        case = production_compile(provider_tokens=BELOW)
        agent = case["agent"]
        self.assertLess(case["bound"], case["context"].window)
        self.assertEqual(case["context"].window, settings.WINDOW_CEILING_DEFAULT)  # the lead set is unchanged
        self.assertFalse(agent.selector.endswith(profile.SUFFIX_1M))
        self.assertEqual(agent.client_class, profile.CLASS_STANDARD)
        self.assertEqual(agent.trigger, profile.agent_compaction_trigger(profile.CLASS_STANDARD,
                                                                         case["context"].percent))
        self.assertLess(agent.trigger, case["bound"])
        self.assertIn(agent.selector, case["plan"].settings["availableModels"])

    def test_the_schedules_straddle_each_trigger(self) -> None:
        for trigger, top in ((702_000, 800_000), (162_000, 180_000)):
            schedule = _schedule(trigger, top)
            self.assertIn(trigger - 1_000, schedule)
            self.assertEqual(schedule[schedule.index(trigger - 1_000) + 1], trigger)
            self.assertEqual(schedule[-1], top)


class WindowClassTests(unittest.TestCase):
    # The lead's wire and the agent's (the bounded agent runs on the same
    # alias, which the client sends without ``[1m]`` either way).
    LEAD, AGENT = "lead-wire", "agent-wire"
    EXPECTED = {"trigger": 702_000, "bounded_trigger": 162_000,
                "wires": {"lead": LEAD, "agent": AGENT, "no-window": AGENT, "bounded": AGENT}}

    @classmethod
    def run_row(cls, **fields):
        row = {"spawned": True, "work": 5, "summaries": 1, "first_summary_after": 702_000, "peak": 702_000,
               "target_models": [cls.AGENT], "completed_result": True, "failed_result": False}
        row.update(fields)
        return row

    def observed(self, **changes):
        rows = {"lead": self.run_row(spawned=False, completed_result=False, target_models=[self.LEAD]),
                "agent": self.run_row(),
                "no-window": self.run_row(summaries=0, first_summary_after=None, peak=800_000),
                "bounded": self.run_row(first_summary_after=162_000, peak=162_000)}
        for name, fields in changes.items():
            rows[name.replace("_", "-")] = {**rows[name.replace("_", "-")], **fields}
        return rows

    def test_the_wire_is_the_selector_the_client_sends(self) -> None:
        self.assertEqual(wire("gpt-multi-x-high[1m]"), "gpt-multi-x-high")
        self.assertEqual(wire("gpt-multi-x-high"), "gpt-multi-x-high")

    def test_pass_needs_every_positive_observation(self) -> None:
        self.assertEqual(window_class(self.observed(), self.EXPECTED), PASS_CLASS)
        for changes, expected in (
            ({"no_window": {"summaries": 1, "first_summary_after": 702_000}}, "window-control-compacted"),
            ({"lead": {"summaries": 0, "first_summary_after": None}}, "window-not-applied-to-lead"),
            ({"lead": {"first_summary_after": 800_000}}, "window-not-applied-to-lead"),
            ({"agent": {"summaries": 0, "first_summary_after": None}}, "window-not-applied-to-agents"),
            ({"agent": {"first_summary_after": 162_000}}, "window-not-applied-to-agents"),
            ({"bounded": {"first_summary_after": 702_000}}, "bounded-agent-class-wrong"),
            ({"bounded": {"summaries": 0, "first_summary_after": None}}, "bounded-agent-class-wrong"),
            ({"agent": {"completed_result": False}}, "agent-run-failed"),
            ({"bounded": {"failed_result": True}}, "agent-run-failed"),
            # A failed no-window control is no control.
            ({"no_window": {"completed_result": False, "failed_result": True}}, "agent-run-failed"),
            ({"no_window": {"completed_result": False}}, "agent-run-failed"),
        ):
            with self.subTest(changes=changes):
                self.assertEqual(window_class(self.observed(**changes), self.EXPECTED), expected)

    def test_a_run_on_another_model_is_never_evidence(self) -> None:
        # Every other observation passes: the class names the routing fault.
        for changes, expected in (
            ({"agent": {"target_models": [self.LEAD]}}, "agent-fell-back-to-lead"),
            ({"no_window": {"target_models": [self.LEAD]}}, "agent-fell-back-to-lead"),
            ({"bounded": {"target_models": [self.LEAD]}}, "agent-fell-back-to-lead"),
            ({"agent": {"target_models": sorted([self.AGENT, self.LEAD])}}, "agent-fell-back-to-lead"),
            ({"agent": {"target_models": ["unrelated-wire"]}}, "agent-model-wrong"),
            ({"bounded": {"target_models": [self.AGENT, "unrelated-wire"]}}, "agent-model-wrong"),
            ({"no_window": {"target_models": []}}, "agent-model-wrong"),
            ({"lead": {"target_models": [self.AGENT]}}, "lead-model-wrong"),
            ({"lead": {"target_models": ["unrelated-wire"]}}, "lead-model-wrong"),
        ):
            with self.subTest(changes=changes):
                self.assertEqual(window_class(self.observed(**changes), self.EXPECTED), expected)

    def test_controls_that_never_reached_their_stimulus_are_inconclusive(self) -> None:
        for changes in ({"no_window": {"peak": 702_000}}, {"agent": {"spawned": False}},
                        {"lead": {"work": 0}}, {"bounded": {"work": 0}}):
            with self.subTest(changes=changes), self.assertRaises(client_probe._Inconclusive):
                window_class(self.observed(**changes), self.EXPECTED)
        # A lead and an agent on one wire model could hide a fallback.
        shared = {**self.EXPECTED, "wires": {**self.EXPECTED["wires"], "agent": self.LEAD}}
        with self.assertRaises(client_probe._Inconclusive):
            window_class(self.observed(), shared)

    def test_the_line_has_a_fixed_prefix_and_no_plan_ids(self) -> None:
        from test_hygiene import origin_counts

        text = line("PASS", PASS_CLASS, "lead=702000/800000")
        self.assertTrue(text.startswith(f"{LINE_PREFIX} PASS class={PASS_CLASS} "))
        self.assertEqual(origin_counts(text.encode()), 0)
        self.assertEqual(origin_counts(LINE_PREFIX.encode()), 0)


# ------------------------------------------------------------ the probe


class AgentWindowProbe(client_probe._ProbeCase, unittest.TestCase):
    """The four runs on the pinned client (offline; zero provider calls)."""

    probe_id = "agent-window"
    test_observation = None  # run under the name below

    @classmethod
    def setUpClass(cls):
        cls.verdict = ("INCONCLUSIVE", "unavailable", "probe did not complete")
        cls.gate = real_binary_gate(client_probe._CONTRACT, what="the agent window probe")
        if cls.gate.boundary:
            cls.verdict = ("BOUNDARY", "unavailable", cls.gate.boundary)

    @classmethod
    def tearDownClass(cls):
        # Flushed at once, so the line starts a line of a combined -v log.
        print(line(*cls.verdict), flush=True)

    def _case(self, name: str, compiled: dict, *, target: str, schedule: tuple[int, ...], window: bool) -> dict:
        plan, context = compiled["plan"], compiled["context"]
        document = copy.deepcopy(plan.settings)
        env = {key: value for key, value in (document.get("env") or {}).items()
               if window or key not in _WINDOW_ENV}
        document["env"] = dict(env, **_HINTS)
        fixture = self._fixture()
        live = scope.write_scope(fixture.root / "state", str(uuid.uuid4()), plan)
        responder = _WindowResponder(target, schedule)
        policy = probe.ProbeCompactionPolicy(context.window, context.percent) if window else None
        result, responder = self._run(fixture, responder=responder, model=compiled["lead"].selector,
                                      extra=("--add-dir", str(live)), document=document,
                                      compaction=policy, timeout=240)
        self._complete(result)
        row = responder.observation()
        self.evidence[name] = row
        return row

    def _probe(self):
        base = production_compile()
        narrow = production_compile(provider_tokens=BELOW)
        context = base["context"]
        agent_wire = wire(base["agent"].selector)
        expected = {"trigger": context.trigger, "bounded_trigger": narrow["agent"].trigger,
                    "wires": {"lead": wire(base["lead"].selector), "agent": agent_wire, "no-window": agent_wire,
                              "bounded": wire(narrow["agent"].selector)}}
        top = context.window
        observed = {
            "lead": self._case("lead", base, target="lead", schedule=_schedule(context.trigger, top), window=True),
            "agent": self._case("agent", base, target="agent", schedule=_schedule(context.trigger, top),
                                window=True),
            "no-window": self._case("no-window", base, target="agent",
                                    schedule=_schedule(context.trigger, top), window=False),
            "bounded": self._case("bounded", narrow, target="agent",
                                  schedule=_schedule(expected["bounded_trigger"], 180_000), window=True),
        }
        self.evidence["expected"] = expected
        outcome = window_class(observed, expected)
        detail = (
            f"window={context.window} percent={context.percent} "
            f"lead={base['lead'].selector}@{observed['lead']['first_summary_after']} "
            f"agent={base['agent'].selector}@{observed['agent']['first_summary_after']} "
            f"no-window=none-through-{observed['no-window']['peak']} "
            f"bounded={narrow['agent'].selector}@{observed['bounded']['first_summary_after']}"
            f"/{narrow['bound']} expected={expected['trigger']}/{expected['bounded_trigger']} "
            # The models each run's requests named, as observed.
            "wires=" + ",".join(f"{name}:{'+'.join(observed[name]['target_models']) or 'none'}" for name in _RUNS)
        )
        return outcome, detail

    def test_lead_and_agents_compact_at_the_session_window(self):
        try:
            observed, detail = self._probe()
        except unittest.SkipTest as exc:
            self.__class__.verdict = ("BOUNDARY", "unavailable", str(exc))
            raise
        except client_probe._Inconclusive as exc:
            self.__class__.verdict = ("INCONCLUSIVE", "unavailable", str(exc))
            self.fail(str(exc))
        except probe.ProbeError as exc:
            if "live daemon domain touched" in str(exc):
                self.__class__.verdict = ("BOUNDARY", "unavailable", "live daemon-domain tripwire hit")
                self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
            # Never print run errors: they may carry captured terminal text.
            self.__class__.verdict = ("INCONCLUSIVE", "unavailable", "probe execution unavailable")
            self.fail("probe execution unavailable (stdio deliberately not retained)")
        else:
            if observed != PASS_CLASS:
                self.__class__.verdict = ("FAIL", observed, detail)
                self.fail(f"{observed}: {detail}")
            self.__class__.verdict = ("PASS", observed, detail)
        finally:
            fixture = self._fixture()
            probe.write_evidence(fixture, self.probe_id, self.evidence)


if __name__ == "__main__":
    unittest.main()
