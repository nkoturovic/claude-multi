"""The profile editor, binding picker, routing preview, named bindings and
the propagation prompt.

Every test runs on a hermetic ``_tui_fixture.fixture_runtime`` inside
``hermetic(runtime)``; nothing reads the host journal, ``/proc`` or the daemon
socket directory.  Expected rows, ids and counts derive from the loaded
fixture catalog: no fixture id is a literal here.  Screens are driven on
``test_tui.FakeWindow`` in ``MONO_PALETTE``; every script ends with an exit key
("exit and restore").  The editor stack writes nothing itself: the store
writes are the cli glue's callbacks (``cli._profile_editor_callbacks``).
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import errno
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, choices, cli, custom, lineup, profile, sessions, settings, state, strict_json, tui, views

import _tui_fixture as fx
from test_tui import DOWN, END, HOME, LEFT, RIGHT, UP, FakeWindow
import claude_multi.tui
import claude_multi.cli.text as cli_text

SPEC = "the editor contract"
ESC = "\x1b"
ENTER = "\n"
CTRL_S = "\x13"
CTRL_O = "\x0f"
CTRL_G = "\x07"
CTRL_C = "\x03"
BACKSPACE = "\x7f"
M = tui.MONO_PALETTE


def _replace_lines(lcat: profile.LineupCatalog, **entries) -> profile.LineupCatalog:
    lines = {key: copy.deepcopy(entry) for key, entry in lcat.lines.items()}
    lines.update(entries)
    return dataclasses.replace(lcat, lines=lines)


class _EditorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-screens-editor-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runtime = self.make_runtime(self.tmp / "root")

    def make_runtime(self, root: Path, **kwargs) -> fx.ScreenRuntime:
        runtime = fx.fixture_runtime(root, **kwargs)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(fx.hermetic(runtime))
        stack.enter_context(mock.patch('claude_multi.cli.gateway_facts._doctor_now', lambda: fx.FIXED_NOW))
        return runtime

    # -- derived fixture facts ------------------------------------------------

    @property
    def lcat(self) -> profile.LineupCatalog:
        return self.runtime.lineup_catalog()

    @property
    def eff(self) -> settings.Effective:
        return self.runtime.current_effective()

    def default(self) -> dict:
        return self.runtime.profiles.load(catalog.DEFAULT_SEED)

    def short(self, key: str) -> str:
        return views.short_label(self.lcat.lines[key]["display"])

    def other_effort(self, key: str, effort: str) -> str | None:
        """Another declared effort of ``key`` (None when the line declares one)."""

        return next((e for e in profile.declared_efforts(self.lcat.lines[key]) if e != effort), None)

    def state(self, document=None, *, name=None, origin=None, bindings=None,
              effective=None, cat=None, is_seed=None, seed_update=None, new=False) -> tui.ProfileEditorState:
        name = name or catalog.DEFAULT_SEED
        if document is None:
            document = self.runtime.profiles.load(name)
        return tui.ProfileEditorState(
            document,
            cat=cat or self.lcat,
            bindings=self.runtime.bindings.bindings() if bindings is None else bindings,
            effective=effective or self.eff,
            origin=None if new else (origin or name),
            is_seed=self.runtime.profiles.is_seed(name) if is_seed is None else is_seed,
            seed_update=seed_update,
        )

    def free_agent(self, document) -> str:
        """A bound agent whose removal keeps the profile valid (none requires it; not Explore's)."""

        roles = self.lcat.roles
        agents = document["agents"]
        return next(
            r for r in catalog.AGENT_ROLE_IDS
            if r in agents and r != "cm-explorer"
            and not any(r in (roles.get(o, {}).get("requires") or ()) for o in agents)
        )

    def callbacks(self) -> tui.ProfileEditorCallbacks:
        return cli._profile_editor_callbacks(self.runtime, M)

    def editor(self, state=None, *, callbacks=None, **kwargs) -> tui.ProfileEditorScreen:
        return tui.ProfileEditorScreen(
            state or self.state(), palette=M, callbacks=callbacks, environ=self.runtime.environ, **kwargs
        )

    @staticmethod
    def run_screen(screen, keys, *, height=24, width=80):
        win = FakeWindow(list(keys), height=height, width=width)
        result = screen.run(win)
        return win, result

    @staticmethod
    def frame(screen, *, height=24, width=80) -> str:
        win = FakeWindow([], height=height, width=width)
        screen._draw(win)
        return win.text()

    def assertFloor(self, factory) -> None:
        """The floor from ``min_size``; one row or column less is too small."""

        screen = factory()
        rows, cols = screen.min_size(80)
        too_small = tui.TOO_SMALL.format(what=screen.what)
        self.assertIn(too_small, self.frame(factory(), height=rows - 1))
        self.assertIn(tui.TOO_SMALL_HINT.format(cols=cols, rows=rows), self.frame(factory(), height=rows - 1))
        fits = self.frame(factory(), height=rows)
        self.assertNotIn(too_small, fits)
        self.assertIn("Esc", fits.splitlines()[-1])
        self.assertTrue(fits.splitlines()[1].strip(), "the title is visible at the floor")
        narrow_rows, _cols = screen.min_size(cols)
        self.assertNotIn(too_small, self.frame(factory(), height=narrow_rows, width=cols))
        self.assertIn(too_small, self.frame(factory(), height=narrow_rows, width=cols - 1))

    # -- records and followers ---------------------------------------------------

    def settings_write(self, document: dict) -> None:
        path = settings.settings_path(self.runtime.environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, json.dumps({"version": settings.SETTINGS_DATA_VERSION, **document}).encode())


def _agent_key(lcat: profile.LineupCatalog, rid: str, *, exclude: frozenset[str] = frozenset()) -> str:
    """A line that admits ``rid`` (all-roles first), not in ``exclude`` (derived)."""

    candidates = [
        k for k, e in lcat.lines.items()
        if "agents" in e["capabilities"] and (e["roles"] == "all" or rid in e["roles"]) and k not in exclude
    ]
    if not candidates:
        raise AssertionError(f"{SPEC} §7.1: the fixture has a line admitting {rid}")
    return candidates[0]


# ================================================================ rows


class EditorRowTests(_EditorCase):
    def test_every_row_of_the_default_seed(self) -> None:
        state = self.state()
        lineup = state.evaluation.lineup
        self.assertIsNotNone(lineup, f"{SPEC} §7.1: the default seed evaluates")
        text = self.frame(self.editor(state))
        lines = text.splitlines()
        self.assertEqual(lines[1].strip(), f"edit profile — {catalog.DEFAULT_SEED}  (seed)")
        self.assertIn(f"General   name {catalog.DEFAULT_SEED} · description", text)
        lead = lineup.lead.binding
        lead_line = next(line for line in lines if line.startswith("  Lead"))
        self.assertIn(f"{views.short_label(lead.display)} · {lead.effort}", lead_line)
        self.assertTrue(lead_line.endswith("[Enter]"))
        document = state.document
        for rid in catalog.AGENT_ROLE_IDS:
            row = next(line for line in lines if line[4:].startswith(f"{profile.label(rid):<22} "))
            if rid in lineup.agents:
                binding = lineup.agents[rid].binding
                self.assertIn(f"{views.short_label(binding.display)} · {binding.effort}", row)
                self.assertIn(binding.family, row)
            else:
                self.assertNotIn(rid, document["agents"])
                self.assertIn("— unbound —", row)
        native = document["native_agents"]
        self.assertIn(f"Native    {views.native_summary(native).split(' · GP')[0]}", text)
        self.assertIn(
            f"Review    routing table ▸ ({lineup.routing.authors} authors, "
            f"{len(lineup.routing.same_family_authors)} same-family)",
            text,
        )
        self.assertIn("Checks    ✓ valid", text)
        self.assertEqual(lines[-2].strip() + " · " + lines[-1].strip(),
                         tui.KeyBar(tui.PROFILE_EDITOR_KEYBAR).text())

    def test_dirty_header_and_unbound_row(self) -> None:
        state = self.state()
        rid = next(r for r in catalog.AGENT_ROLE_IDS if r in state.document["agents"])
        self.assertIsNone(state.unbind(rid))
        self.assertTrue(state.dirty)
        header = self.frame(self.editor(state)).splitlines()[1]
        self.assertTrue(header.endswith("unsaved changes ●"), header)
        row = next(r for r in views.editor_rows(state) if r.key == rid)
        self.assertIn("— unbound —", row.text)
        self.assertEqual(row.role, "dim")

    def test_the_n2_removed_row(self) -> None:
        null_key = fx.needs_choice_key(self.runtime)
        document = self.default()
        rid = next(r for r in catalog.AGENT_ROLE_IDS if r not in ("cm-explorer",) and r in document["agents"]
                   and not any(r in (self.lcat.roles.get(o, {}).get("requires") or ()) for o in document["agents"]))
        document["agents"][rid] = {"model": null_key, "effort": "high"}
        state = self.state(document)
        self.assertEqual(state.evaluation.errors, (), state.evaluation.errors)
        row = next(r for r in views.editor_rows(state) if r.key == rid)
        self.assertIn(f"{null_key} was removed — unbound", row.text)
        self.assertEqual(row.role, "warn")

    def test_the_named_rows(self) -> None:
        lead_key = self.default()["lead"]["model"]
        rid = next(r for r in catalog.AGENT_ROLE_IDS if r in self.default()["agents"])
        agent = self.default()["agents"][rid]
        bindings = {
            "nb-agent": {"model": agent["model"], "effort": agent["effort"]},
            "nb-lead": {"model": lead_key, "effort": profile.ULTRACODE},
        }
        state = self.state(bindings=bindings)
        state.set_named(rid, "nb-agent")
        state.set_named(catalog.LEAD_ROLE, "nb-lead")
        self.assertEqual(state.evaluation.errors, ())
        rows = {r.key: r for r in views.editor_rows(state)}
        self.assertIn(f"nb-agent → {self.short(agent['model'])} · {agent['effort']}", rows[rid].text)
        self.assertEqual(rows[rid].suffix, "(named)")
        self.assertIn(f"{self.short(lead_key)} · {profile.ULTRACODE}  (named nb-lead)", rows["lead"].text)
        drawn = self.frame(self.editor(state))
        self.assertIn("(named)", drawn)

    def test_the_n1_notice_suffix(self) -> None:
        document = self.default()
        rid = next(r for r in catalog.AGENT_ROLE_IDS if r in document["agents"])
        successor = document["agents"][rid]["model"]
        retired = dict(self.lcat.retired)
        retired["editor-old"] = {**next(iter(self.lcat.retired.values())), "successor": successor}
        cat = dataclasses.replace(self.lcat, retired=retired)
        document["agents"][rid] = {"model": "editor-old", "effort": document["agents"][rid]["effort"]}
        state = self.state(document, cat=cat)
        self.assertEqual(state.evaluation.errors, (), state.evaluation.errors)
        row = next(r for r in views.editor_rows(state) if r.key == rid)
        self.assertIn("(was editor-old)", row.suffix)
        self.assertEqual(row.suffix_role, "dim")

    def test_diamond_marks_a_differing_compaction_override(self) -> None:
        state = self.state()
        percent = self.eff.compaction_percent - 5
        self.assertIsNone(state.set_general(compaction_override=percent))
        self.assertTrue(state.diamond())
        general = next(r for r in views.editor_rows(state) if r.key == "general")
        self.assertIn(f"◆ compaction {percent} %", general.text)
        self.assertIsNone(state.set_general(compaction_override=self.eff.compaction_percent))
        self.assertFalse(state.diamond())
        self.assertNotIn("◆", next(r for r in views.editor_rows(state) if r.key == "general").text)
        self.assertIsNone(state.set_general(compaction_override=None))
        self.assertNotIn("settings_overrides", state.document)

    def test_width_tiers_drop_the_provider_column(self) -> None:
        state = self.state()
        lineup = state.evaluation.lineup
        provider = next(iter(lineup.agents.values())).binding.provider
        wide = self.frame(self.editor(state), width=80)
        narrow = self.frame(self.editor(state), width=70)
        self.assertIn(" provider", wide)
        self.assertNotIn(" provider", narrow)
        agent_rows = [l for l in narrow.splitlines() if l.startswith("    ") and "·" in l]
        self.assertTrue(agent_rows)
        self.assertFalse(any(l.rstrip().endswith(provider) for l in agent_rows))
        for line in wide.splitlines() + narrow.splitlines():
            self.assertLessEqual(len(line), 80)

    def test_the_seed_update_message_row(self) -> None:
        state = self.state(seed_update=(1, 2))
        self.assertIn(views.EDITOR_SEED_NAG, self.frame(self.editor(state), width=120))

    def test_agents_scroll_to_the_focus_at_the_floor(self) -> None:
        screen = self.editor(initial_focus=catalog.AGENT_ROLE_IDS[-1])
        rows, _cols = screen.min_size(80)
        text = self.frame(screen, height=rows)
        self.assertIn(profile.label(catalog.AGENT_ROLE_IDS[-1]), text)
        self.assertNotIn(f"    {profile.label(catalog.AGENT_ROLE_IDS[0]):<22}", text)
        self.assertIn("Checks", text)


# ================================================================ checks


def _warning_documents(case: _EditorCase) -> dict[str, tuple[dict, contextlib.AbstractContextManager]]:
    """One test-local (or seed) document per warning code, derived from the fixture."""

    lcat = case.lcat
    out: dict[str, tuple[dict, contextlib.AbstractContextManager]] = {}
    for name in case.runtime.profiles.names():
        document = case.runtime.profiles.load(name)
        ev = profile.evaluate(document, lcat, bindings={}, effective=case.eff)
        for finding in ev.lineup.warnings if ev.lineup else ():
            out.setdefault(finding.code, (document, contextlib.nullcontext()))
    base = case.default()
    lead_key = base["lead"]["model"]
    lead_provider = lcat.lines[lead_key]["provider"]
    # W2: every -strong slot on the lead's provider.
    doc = copy.deepcopy(base)
    for rid in [r for r in catalog.AGENT_ROLE_IDS if r.endswith("-strong") and r in doc["agents"]]:
        key = next(k for k, e in lcat.lines.items() if e["provider"] == lead_provider
                   and "agents" in e["capabilities"] and (e["roles"] == "all" or rid in e["roles"]))
        doc["agents"][rid] = {"model": key, "effort": lcat.lines[key]["default_effort"]}
    out.setdefault("lead-strong-concentration", (doc, contextlib.nullcontext()))
    # W3: a -strong slot on its plain slot's line and effort.
    doc = copy.deepcopy(base)
    strong, plain = next((s, p) for s, p in profile.STRONG_PAIRS if s in doc["agents"] and p in doc["agents"])
    doc["agents"][strong] = copy.deepcopy(doc["agents"][plain])
    out.setdefault("strong-not-stronger", (doc, contextlib.nullcontext()))
    # W5: an agent on a line whose client window is below the lead's.
    lead_tokens = lcat.lines[lead_key]["context"]["client_tokens"]
    small = next(k for k, e in lcat.lines.items() if "agents" in e["capabilities"] and e["roles"] == "all"
                 and e["context"]["client_tokens"] < lead_tokens)
    doc = copy.deepcopy(base)
    doc["agents"]["cm-analyst"] = {"model": small, "effort": lcat.lines[small]["default_effort"]}
    out.setdefault("context-below-lead-class", (doc, contextlib.nullcontext()))
    # W6: make the lead's provider a strict-schema route for the test.
    out.setdefault(
        "strict-tool-schema",
        (copy.deepcopy(base), mock.patch.object(profile, "STRICT_TOOL_SCHEMA_PROVIDERS", frozenset({lead_provider}))),
    )
    return out


class EditorChecksTests(_EditorCase):
    WARNING_CODES = (
        "same-family-review", "lead-strong-concentration", "strong-not-stronger", "fan-out-quota",
        "context-below-lead-class", "strict-tool-schema",
    )

    def test_each_warning_compact_is_on_the_checks_row_and_in_help(self) -> None:
        documents = _warning_documents(self)
        self.assertEqual(set(self.WARNING_CODES) - set(documents), set(), f"{SPEC} §7.3: W1-W5 + strict")
        for code in self.WARNING_CODES:
            document, context = documents[code]
            with self.subTest(code=code), context:
                state = self.state(document, name=document["name"])
                self.assertEqual(state.evaluation.errors, ())
                finding = next(f for f in state.evaluation.lineup.warnings if f.code == code)
                wide = next(r for r in views.editor_rows(state, width=400) if r.key == "checks")
                self.assertTrue(wide.text.startswith("Checks    ✓ valid"), wide.text)
                self.assertIn(f"! {finding.compact}", wide.text)
                self.assertEqual(wide.role, "warn")
                self.assertFalse(wide.selectable, "a valid Checks row is not a focus target")
                win, _ = self.run_screen(self.editor(state), ["?", ENTER, ESC], height=60, width=120)
                help_frame = next(f for f in win.frames if "edit profile — help" in f)
                self.assertIn(f"! {finding.message}"[:60], help_frame)

    def test_the_checks_row_clips_with_a_count(self) -> None:
        state = self.state()
        many = [profile.Finding(f"c{i}", None, f"message {i}", f"compact finding number {i}") for i in range(8)]
        lineup = dataclasses.replace(state.evaluation.lineup, warnings=tuple(many))
        state.evaluation = profile.Evaluation((), lineup)
        row = next(r for r in views.editor_rows(state, width=80) if r.key == "checks")
        self.assertRegex(row.text, r"\(\+\d+ more — \? lists all\)$")

    def error_documents(self) -> list[tuple[str, dict, str, dict]]:
        """(E-code, document, expected field_row, state kwargs) for E3-E22, derived."""

        lcat = self.lcat
        base = self.default()
        lead_key = base["lead"]["model"]
        rid = next(r for r in catalog.AGENT_ROLE_IDS if r in base["agents"])
        null_key = fx.needs_choice_key(self.runtime)
        out: list[tuple[str, dict, str, dict]] = []

        def doc(mutate) -> dict:
            d = copy.deepcopy(base)
            mutate(d)
            return d

        out.append(("E3", doc(lambda d: d["agents"].__setitem__(rid, {"model": "no-such-line", "effort": "high"})), rid, {}))
        out.append(("E4", doc(lambda d: d.__setitem__("lead", {"model": null_key, "effort": profile.ULTRACODE})), "lead", {}))
        bad_effort = next(e for e in profile.EFFORT_ORDER if e not in profile.declared_efforts(lcat.lines[base["agents"][rid]["model"]]))
        out.append(("E5", doc(lambda d: d["agents"][rid].__setitem__("effort", bad_effort)), rid, {}))
        out.append(("E6", doc(lambda d: d["agents"][rid].__setitem__("effort", profile.ULTRACODE)), rid, {}))
        # E7: a custom model (merged from a temp custom registry) cannot be bound.
        direct = next(pid for pid, p in self.runtime.catalog.providers.items() if p["transport"]["kind"] == "direct")
        custom.add_model(
            self.runtime.environ, "editor-custom", wire_model="editor-custom-wire", provider=direct,
            context_tokens=200_000, display="Editor Custom · test", created_via="manual",
            catalog_providers=self.runtime.catalog.providers, catalog_models=self.runtime.catalog.lines,
            retired_models=self.runtime.catalog.retired,
        )
        merged = self.make_runtime(self.tmp / "root").lineup_catalog()  # a fresh Runtime merges custom.json
        out.append(("E7", doc(lambda d: d.__setitem__("lead", {"model": "editor-custom", "effort": "high"})), "lead",
                    {"cat": merged}))
        new_key = _agent_key(lcat, rid)
        new_cat = _replace_lines(lcat, **{new_key: {**lcat.lines[new_key], "status": "new"}})
        out.append(("E8", doc(lambda d: d["agents"].__setitem__(rid, {"model": new_key, "effort": lcat.lines[new_key]["default_effort"]})), rid, {"cat": new_cat}))
        off_provider = lcat.lines[base["agents"][rid]["model"]]["provider"]
        off_eff = dataclasses.replace(self.eff, providers_enabled={**self.eff.providers_enabled, off_provider: False})
        out.append(("E9", copy.deepcopy(base), rid, {"effective": off_eff}))
        no_lead = next(k for k, e in lcat.lines.items() if "lead" not in e["capabilities"])
        out.append(("E10", doc(lambda d: d.__setitem__("lead", {"model": no_lead, "effort": lcat.lines[no_lead]["default_effort"]})), "lead", {}))
        no_agents = next(k for k, e in lcat.lines.items() if "agents" not in e["capabilities"])
        out.append(("E11", doc(lambda d: d["agents"].__setitem__(rid, {"model": no_agents, "effort": lcat.lines[no_agents]["default_effort"]})), rid, {}))
        restricted = next(k for k, e in lcat.lines.items() if "agents" in e["capabilities"] and e["roles"] != "all")
        refused = next(r for r in catalog.AGENT_ROLE_IDS if r not in lcat.lines[restricted]["roles"] and r in base["agents"])
        out.append(("E12", doc(lambda d: d["agents"].__setitem__(refused, {"model": restricted, "effort": lcat.lines[restricted]["default_effort"]})), refused, {}))
        strong = next(r for r in catalog.AGENT_ROLE_IDS if r in base["agents"] and (lcat.roles[r].get("requires") or ()))
        required = lcat.roles[strong]["requires"][0]
        out.append(("E13", doc(lambda d: d["agents"].pop(required)), strong, {}))
        out.append(("E14", doc(lambda d: d["agents"].__setitem__(rid, {"use": "nosuch"})), rid, {}))
        out.append(("E15", doc(lambda d: d["agents"].__setitem__(rid, {"use": "uc"})), rid,
                    {"bindings": {"uc": {"model": base["agents"][rid]["model"], "effort": profile.ULTRACODE}}}))
        out.append(("E16", doc(lambda d: (d["native_agents"].__setitem__("explore", "replace"), d["agents"].pop("cm-explorer", None))), "native", {}))
        out.append(("E17", doc(lambda d: d.__setitem__("lead_providers", ["nosuch"])), "general", {}))
        other = next(p for p in sorted(lcat.providers) if p != lcat.lines[lead_key]["provider"])
        out.append(("E18", doc(lambda d: d.__setitem__("lead_providers", [other])), "general", {}))
        out.append(("E19", doc(lambda d: d.__setitem__("primary_provider", "nosuch")), "general", {}))
        out.append(("E20", doc(lambda d: d.__setitem__("settings_overrides", {"window": 1})), "general", {}))
        out.append(("E21", doc(lambda d: d.__setitem__("settings_overrides", {settings.COMPACTION_PERCENT_KEY: True})), "general", {}))
        out.append(("E22", doc(lambda d: d.__setitem__("settings_overrides", {settings.COMPACTION_PERCENT_KEY: settings.COMPACTION_PERCENT_MAX + 1})), "general", {}))
        return out

    def test_each_reachable_error_maps_to_its_field_row(self) -> None:
        prefixes = {
            "E3": "agents.", "E4": "lead.model", "E5": "agents.", "E6": "agents.", "E7": "lead.model",
            "E8": "agents.", "E9": "agents.", "E10": "lead.model", "E11": "agents.", "E12": "agents.",
            "E13": "agents.", "E14": "agents.", "E15": "agents.", "E16": "native_agents.explore",
            "E17": "lead_providers", "E18": "lead_providers", "E19": "primary_provider",
            "E20": "settings_overrides.", "E21": "settings_overrides.", "E22": "settings_overrides.",
        }
        cases = self.error_documents()
        self.assertEqual([code for code, *_ in cases], [f"E{n}" for n in range(3, 23)])
        for code, document, expected, kwargs in cases:
            with self.subTest(code=code):
                state = self.state(document, **kwargs)
                errors = state.evaluation.errors
                self.assertTrue(errors, f"{code}: the test-local document is invalid")
                self.assertTrue(errors[0].startswith(prefixes[code]), (code, errors))
                self.assertEqual(state.field_row(errors[0]), expected)
                checks = next(r for r in views.editor_rows(state) if r.key == "checks")
                self.assertEqual(checks.text, f"Checks    ✗ {len(errors)} error(s): {errors[0]}")
                self.assertEqual(checks.role, "error")
                screen = self.editor(state, initial_focus="checks")
                self.run_screen(screen, [ENTER, ESC], height=30)
                self.assertEqual(screen.focus, expected)

    def test_schema_problems_map_to_general(self) -> None:
        state = self.state()
        self.assertEqual(state.field_row("$.name: does not match"), "general")
        self.assertEqual(state.field_row("lead: not resolved"), "lead")
        self.assertEqual(state.field_row("workflows: bad"), "native")


class EditorFieldFocusTests(_EditorCase):
    def test_error_row_focus_for_a_retired_null_lead(self) -> None:
        # The reachable BLOCKED case of a lead-less 2.x
        # composition is a retired-null lead (E4); Enter on ✗ jumps to Lead,
        # ^S refuses and focuses it too, with no traceback.
        null_key = fx.needs_choice_key(self.runtime)
        document = self.default()
        document["lead"] = {"model": null_key, "effort": profile.ULTRACODE}
        state = self.state(document)
        self.assertTrue(state.evaluation.errors[0].startswith("lead.model: "))
        screen = self.editor(state, initial_focus="checks")
        win, result = self.run_screen(screen, [ENTER, ESC], height=30)
        self.assertEqual(screen.focus, "lead")
        self.assertIsNone(result)
        self.assertIn(f"✗ 1 error(s): {state.evaluation.errors[0]}"[:60], win.frames[0])
        self.assertIn("routing table ▸ (fix the errors first)", win.frames[0])
        screen = self.editor(state)
        win, _ = self.run_screen(screen, [CTRL_S, ESC], height=30)
        self.assertEqual(screen.focus, "lead")
        self.assertTrue(any(tui.PROFILE_FORM_FIX_ERRORS.format(n=1) in f for f in win.frames))


# ================================================================ keys


class EditorKeyTests(_EditorCase):
    def test_the_selected_slot_shows_its_role_description(self) -> None:
        state = self.state()
        screen = self.editor(state)
        # General is focused first: Down to the lead, Down to the first agent.
        win, _ = self.run_screen(screen, [DOWN, DOWN, ESC], height=24, width=80)
        first = catalog.AGENT_ROLE_IDS[0]
        for slot in (catalog.LEAD_ROLE, first):
            role = self.lcat.roles[slot]
            described = f"{profile.label(slot)} ({role['grade']}): {role['description']}"
            self.assertTrue(any(described[:76] + "…" in frame for frame in win.frames), slot)
        self.assertNotIn(self.lcat.roles[first]["description"][:40], win.frames[0])

    def test_u_unbinds_an_agent_and_refuses_the_lead(self) -> None:
        state = self.state()
        rid = next(r for r in catalog.AGENT_ROLE_IDS if r in state.document["agents"])
        screen = self.editor(state, initial_focus=rid)
        self.run_screen(screen, ["U", ESC, ENTER])
        self.assertNotIn(rid, state.document["agents"])
        screen = self.editor(self.state(), initial_focus="lead")
        win, _ = self.run_screen(screen, ["u", ESC])
        self.assertIn(tui.PROFILE_FORM_LEAD_UNBIND, win.frames[-1])

    def test_enter_on_an_agent_opens_the_picker_and_binds_the_effort(self) -> None:
        state = self.state()
        rid = next(r for r in catalog.AGENT_ROLE_IDS if r in state.document["agents"]
                   and self.other_effort(state.document["agents"][r]["model"], state.document["agents"][r]["effort"]))
        before = state.document["agents"][rid]
        efforts = profile.declared_efforts(self.lcat.lines[before["model"]])
        step = RIGHT if efforts.index(before["effort"]) < len(efforts) - 1 else LEFT
        screen = self.editor(state, initial_focus=rid)
        win, _ = self.run_screen(screen, [ENTER, step, ENTER, ESC, ENTER])
        picker_frame = next(f for f in win.frames if f"{profile.label(rid)} — choose model" in f)
        self.assertIn("← → effort:", picker_frame)
        after = state.document["agents"][rid]
        self.assertEqual(after["model"], before["model"])
        self.assertNotEqual(after["effort"], before["effort"])
        self.assertIn(after["effort"], efforts)

    def test_enter_on_lead_picks_with_ultracode_first(self) -> None:
        state = self.state()
        screen = self.editor(state, initial_focus="lead")
        win, _ = self.run_screen(screen, [ENTER, ENTER, ESC])
        self.assertEqual(state.document["lead"]["effort"], profile.ULTRACODE)
        self.assertFalse(state.dirty)

    def test_r_and_enter_on_review_open_the_routing_preview(self) -> None:
        for keys, focus in (([ "R", ESC, ESC], "general"), ([ENTER, ESC, ESC], "review")):
            with self.subTest(focus=focus):
                win, _ = self.run_screen(self.editor(initial_focus=focus), keys)
                self.assertTrue(any(f"review routing — {catalog.DEFAULT_SEED}" in f for f in win.frames))

    def test_help_modal_shows_the_help_text(self) -> None:
        win, _ = self.run_screen(self.editor(), ["?", ENTER, ESC], height=40, width=100)
        frame = next(f for f in win.frames if "edit profile — help" in f)
        self.assertIn("↑/↓ move. Enter changes the row", frame)
        self.assertIn("◆ marks a per-profile override", frame)
        self.assertEqual(tui.PROFILE_EDITOR_HELP.splitlines()[0],
                         "↑/↓ move. Enter changes the row: General (name, description, primary provider,")

    def test_ctrl_s_and_ctrl_o_both_save(self) -> None:
        for key in (CTRL_S, CTRL_O):
            with self.subTest(key=repr(key)):
                seen = []
                callbacks = tui.ProfileEditorCallbacks(save=lambda win, outcome: (seen.append(outcome), (True, "ok"))[1])
                state = self.state()
                state.unbind(self.free_agent(state.document))
                screen = self.editor(state, callbacks=callbacks)
                _win, result = self.run_screen(screen, [key, ESC])
                self.assertEqual([o.action for o in seen], ["save"])
                self.assertEqual(result, seen[0])
                self.assertFalse(state.dirty)

    def test_general_form_edits_every_field(self) -> None:
        state = self.state()
        providers = sorted(self.lcat.providers)
        lead_provider = self.lcat.lines[state.document["lead"]["model"]]["provider"]
        keys = [
            ENTER,                      # General → the sub-form (focus: name)
            DOWN, "x",                  # description gets an x appended
            DOWN, ENTER, DOWN, ENTER,   # primary provider = the first provider id
            DOWN, ENTER, *([DOWN] * providers.index(lead_provider)), " ", ENTER,  # lead providers = the lead's
            DOWN, "7", "0",             # compaction override 70
            CTRL_S, ESC, ENTER,
        ]
        win, _ = self.run_screen(self.editor(state), keys)
        self.assertTrue(any(tui.PROFILE_FORM_GENERAL_TITLE in f for f in win.frames))
        self.assertTrue(state.document["description"].endswith("x"))
        self.assertEqual(state.document["primary_provider"], providers[0])
        self.assertEqual(state.document["lead_providers"], [lead_provider])
        self.assertEqual(state.document["settings_overrides"], {settings.COMPACTION_PERCENT_KEY: 70})

    def test_general_form_refuses_a_non_integer_and_esc_discards(self) -> None:
        state = self.state()
        before = copy.deepcopy(state.document)
        keys = [ENTER, DOWN, DOWN, DOWN, DOWN, "x", CTRL_S, ESC, ESC]
        win, _ = self.run_screen(self.editor(state), keys)
        self.assertTrue(any("compaction override: 'x' is not a whole number" in f for f in win.frames))
        self.assertEqual(state.document, before)

    def test_general_form_schema_problem_keeps_the_form_open(self) -> None:
        state = self.state()
        keys = [ENTER, *([BACKSPACE] * 64), "B", "A", "D", "!", CTRL_S, ESC, ESC]
        win, _ = self.run_screen(self.editor(state), keys)
        self.assertTrue(any("$.name" in f for f in win.frames if tui.PROFILE_FORM_GENERAL_TITLE in f))
        self.assertEqual(state.document["name"], catalog.DEFAULT_SEED)


class EditorNativeRowTests(_EditorCase):
    def test_native_form_sets_each_group(self) -> None:
        state = self.state()
        native = state.document["native_agents"]
        keys = [ENTER]
        expected = {}
        for index, (key, _label, options) in enumerate(tui._NATIVE_FIELDS):
            current = options.index(native[key])
            keys += [RIGHT, DOWN]
            expected[key] = options[(current + 1) % len(options)]
        wf_current = tui.WORKFLOWS_VALUES.index(state.document.get("workflows", "native"))
        keys += [RIGHT, ENTER, ESC, ENTER]
        screen = self.editor(state, initial_focus="native")
        win, _ = self.run_screen(screen, keys)
        self.assertTrue(any(tui.PROFILE_FORM_NATIVE_TITLE in f for f in win.frames))
        for key, value in expected.items():
            self.assertEqual(state.document["native_agents"][key], value, key)
        wf = tui.WORKFLOWS_VALUES[(wf_current + 1) % len(tui.WORKFLOWS_VALUES)]
        self.assertEqual(state.document["workflows"], wf)
        native_row = next(r for r in views.editor_rows(state) if r.key == "native")
        self.assertIn(f"workflows {wf}", native_row.text)

    def test_workflows_values_are_the_profile_schema_enum(self) -> None:
        schema = json.loads(profile.profile_schema_path().read_text())
        self.assertEqual(tuple(schema["properties"]["workflows"]["enum"]), tui.WORKFLOWS_VALUES)

    def test_setters_refuse_unknown_values(self) -> None:
        state = self.state()
        with self.assertRaises(ValueError):
            state.set_native("explore", "sometimes")
        with self.assertRaises(ValueError):
            state.set_workflows("maybe")

    def test_native_form_help_shows_the_guarantee_panel(self) -> None:
        win, _ = self.run_screen(self.editor(initial_focus="native"), [ENTER, "?", ENTER, ESC, ESC], height=40, width=100)
        frame = next(f for f in win.frames if "edit profile — native — help" in f)
        self.assertIn("Native workflows (ultracode):", frame)


class EditorDirtyGuardTests(_EditorCase):
    def test_clean_esc_and_ctrl_c_return_without_a_modal(self) -> None:
        for key in (ESC, CTRL_C):
            win, result = self.run_screen(self.editor(), [key])
            self.assertIsNone(result)
            self.assertFalse(any(tui.PROFILE_FORM_DISCARD_TITLE in f for f in win.frames))

    def test_dirty_esc_asks_and_keep_editing_keeps(self) -> None:
        for key in (ESC, CTRL_C):
            with self.subTest(key=repr(key)):
                state = self.state()
                state.unbind(self.free_agent(state.document))
                win, result = self.run_screen(self.editor(state), [key, RIGHT, ENTER, key, ENTER])
                modal = next(f for f in win.frames if tui.PROFILE_FORM_DISCARD_TITLE in f)
                self.assertIn(tui.PROFILE_FORM_DISCARD_BODY, modal)
                self.assertIn("[ Discard ]", modal)
                self.assertIn("[ Keep editing ]", modal)
                self.assertIsNone(result)
                self.assertTrue(state.dirty, "discarding never writes; the state keeps the edit")


# ================================================================ save


class EditorSaveTests(_EditorCase):
    def edited(self, name=catalog.DEFAULT_SEED, **kwargs) -> tui.ProfileEditorState:
        state = self.state(name=name, **kwargs)
        rid = self.free_agent(state.document)
        state.unbind(rid)
        return state

    def rename_to(self, state, new: str) -> list:
        return [ENTER, *([BACKSPACE] * 64), *new, CTRL_S]

    def test_plain_save_writes_the_profile(self) -> None:
        state = self.edited()
        win, result = self.run_screen(self.editor(state, callbacks=self.callbacks()), [CTRL_S, ESC])
        self.assertEqual((result.action, result.name), ("save", catalog.DEFAULT_SEED))
        saved = self.runtime.profiles.load(catalog.DEFAULT_SEED)
        self.assertEqual(saved, state.document)
        self.assertIn("seed", saved, "a plain save of a seed keeps its marker")
        self.assertIn(cli.PROPAGATION_NO_FOLLOWERS.format(name=catalog.DEFAULT_SEED), win.frames[-1])

    def test_save_as_new_drops_seed(self) -> None:
        # Save as new never carries the seed marker into the user profile.
        state = self.edited()
        keys = [*self.rename_to(state, "mine"), CTRL_S, ENTER, ESC]
        win, result = self.run_screen(self.editor(state, callbacks=self.callbacks()), keys)
        self.assertTrue(any(tui.PROFILE_FORM_NEW_NAME_TITLE in f for f in win.frames))
        self.assertEqual((result.action, result.name, result.old_name), ("new", "mine", catalog.DEFAULT_SEED))
        mine = self.runtime.profiles.load("mine")
        self.assertNotIn("seed", mine)
        self.assertFalse(self.runtime.profiles.has_user(catalog.DEFAULT_SEED))
        self.assertFalse(state.is_seed)
        self.assertNotIn("seed", state.document)

    def test_rename_moves_a_user_profile(self) -> None:
        self.runtime.profiles.duplicate(catalog.DEFAULT_SEED, "mine")
        state = self.edited(name="mine")
        keys = [*self.rename_to(state, "yours"), CTRL_S, RIGHT, ENTER, ESC]
        _win, result = self.run_screen(self.editor(state, callbacks=self.callbacks()), keys)
        self.assertEqual((result.action, result.name, result.old_name), ("rename", "yours", "mine"))
        self.assertFalse(self.runtime.profiles.contains("mine"))
        self.assertEqual(self.runtime.profiles.load("yours"), state.document)

    def test_a_seed_offers_only_save_as_new_and_the_edits_stay(self) -> None:
        # A shipped profile keeps its name: the dialog never offers Rename.
        state = self.edited()
        keys = [*self.rename_to(state, "renamed"), CTRL_S, RIGHT, ENTER, ESC, ENTER]
        win, result = self.run_screen(self.editor(state, callbacks=self.callbacks()), keys)
        self.assertIsNone(result)
        dialog = next(f for f in win.frames if tui.PROFILE_FORM_NEW_NAME_TITLE in f)
        self.assertIn("[ Save as new ]", dialog)
        self.assertIn("[ Cancel ]", dialog)
        self.assertNotIn("[ Rename ]", dialog)
        self.assertEqual(tui.PROFILE_FORM_SEED_SAVE_BUTTONS, (("Save as new", "new"), ("Cancel", None)))
        self.assertFalse(self.runtime.profiles.contains("renamed"))
        self.assertEqual(state.name, "renamed")
        self.assertTrue(state.dirty)

    def test_save_as_new_refuses_p1(self) -> None:
        state = self.edited()
        taken = next(n for n in self.runtime.profiles.names() if n != catalog.DEFAULT_SEED)
        keys = [*self.rename_to(state, taken), CTRL_S, ENTER, ESC, ENTER]
        win, result = self.run_screen(self.editor(state, callbacks=self.callbacks()), keys)
        self.assertIsNone(result)
        self.assertTrue(any(f"profile target {taken!r} already exists; choose another name" in f for f in win.frames))

    def test_cancel_in_the_name_modal_writes_nothing(self) -> None:
        state = self.edited()
        keys = [*self.rename_to(state, "mine"), CTRL_S, RIGHT, ENTER, ESC, ENTER]
        _win, result = self.run_screen(self.editor(state, callbacks=self.callbacks()), keys)
        self.assertIsNone(result)
        self.assertFalse(self.runtime.profiles.contains("mine"))

    def test_a_new_profile_saves_with_new(self) -> None:
        document = self.default()
        document.pop("seed")
        document["name"] = "fresh"
        state = self.state(document, name="fresh", is_seed=False, new=True)
        _win, result = self.run_screen(self.editor(state, callbacks=self.callbacks()), [CTRL_S, ESC])
        self.assertEqual(result.action, "new")
        self.assertEqual(self.runtime.profiles.load("fresh"), state.document)

    def test_committed_state_error_is_labelled(self) -> None:
        state = self.edited()
        error = state_mod_committed("durability unconfirmed for the test")
        with mock.patch.object(profile.ProfileStore, "commit", side_effect=error):
            win, result = self.run_screen(self.editor(state, callbacks=self.callbacks()), [CTRL_S, ESC, ENTER])
        self.assertIsNone(result)
        self.assertTrue(any("Committed, durability unconfirmed: durability unconfirmed for the test" in f
                            for f in win.frames))
        self.assertTrue(state.dirty)

    def test_a_state_marker_error_is_shown_verbatim(self) -> None:
        state = self.edited()
        with mock.patch.object(profile.ProfileStore, "commit", side_effect=sessions.StateMarkerError("marker says no")):
            win, _ = self.run_screen(self.editor(state, callbacks=self.callbacks()), [CTRL_S, ESC, ENTER])
        self.assertTrue(any("marker says no" in f for f in win.frames))


def state_mod_committed(message: str) -> state.CommittedStateError:
    return state.CommittedStateError(errno.EIO, message)


class EditorSaveFailureTests(_EditorCase):
    """The 3.0 form of 2.x ``EditorFailurePreservationTests``."""

    def test_collision_then_retry_under_another_name(self) -> None:
        state = self.state()
        state.unbind(self.free_agent(state.document))
        taken = next(n for n in self.runtime.profiles.names() if n != catalog.DEFAULT_SEED)
        keys = [ENTER, *([BACKSPACE] * 64), *taken, CTRL_S, CTRL_S, ENTER,      # P1 refused
                ENTER, *([BACKSPACE] * 64), *"retry", CTRL_S, CTRL_S, ENTER,  # retried
                ESC]
        _win, result = self.run_screen(self.editor(state, callbacks=self.callbacks()), keys)
        self.assertEqual((result.action, result.name), ("new", "retry"))
        self.assertEqual(self.runtime.profiles.load("retry"), state.document)

    def test_invalid_then_repair_saves(self) -> None:
        null_key = fx.needs_choice_key(self.runtime)
        document = self.default()
        good_lead = document["lead"]
        document["lead"] = {"model": null_key, "effort": profile.ULTRACODE}
        state = self.state(document)
        screen = self.editor(state, callbacks=self.callbacks(), initial_focus="lead")
        win, _ = self.run_screen(screen, [CTRL_S, ESC, ENTER])
        self.assertFalse(self.runtime.profiles.has_user(catalog.DEFAULT_SEED))
        state.set_binding(catalog.LEAD_ROLE, good_lead["model"], good_lead["effort"])
        _win, result = self.run_screen(screen, [CTRL_S, ESC])
        self.assertEqual(result.action, "save")
        self.assertEqual(self.runtime.profiles.load(catalog.DEFAULT_SEED)["lead"], good_lead)

    def test_io_failure_keeps_the_edits(self) -> None:
        state = self.state()
        rid = self.free_agent(state.document)
        state.unbind(rid)
        edited = copy.deepcopy(state.document)
        with mock.patch.object(state_module(), "atomic_write", side_effect=OSError(errno.ENOSPC, "disk full")):
            win, result = self.run_screen(self.editor(state, callbacks=self.callbacks()), [CTRL_S, ESC, ENTER])
        self.assertIsNone(result)
        self.assertTrue(any("disk full" in f for f in win.frames))
        self.assertEqual(state.document, edited)
        self.assertTrue(state.dirty)

    def test_a_failed_rename_changes_nothing_and_a_retry_renames_with_the_edits(self) -> None:
        # The rename writes the edited document under the new name in one
        # step: a failed write leaves the old name, its follower and the
        # edits as they were, and a retry is the same rename.
        self.runtime.profiles.duplicate(catalog.DEFAULT_SEED, "mine")
        fx.v4_record(self.runtime, "mine", managed_id=fx.FIXED_ID)
        state = self.state(name="mine")
        state.unbind(self.free_agent(state.document))
        callbacks = cli._profile_editor_callbacks(self.runtime, M, editor_state=state)
        screen = self.editor(state, callbacks=callbacks)
        keys = [ENTER, *([BACKSPACE] * 64), *"mine2", CTRL_S, CTRL_S, RIGHT, ENTER,  # Rename
                RIGHT, ENTER,  # the follower is shown first: Rename
                ESC, ENTER]  # dirty: discard and leave
        error = state_module().StateError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(profile.ProfileStore, "_write", side_effect=error):
            win, result = self.run_screen(screen, keys)
        self.assertIsNone(result)
        self.assertTrue(any("1 session(s) follow mine" in f for f in win.frames), "the follower is shown first")
        self.assertTrue(any("No space left on device" in f for f in win.frames))
        self.assertTrue(self.runtime.profiles.contains("mine"))
        self.assertFalse(self.runtime.profiles.contains("mine2"))
        record = self.runtime.session_store.load(fx.FIXED_ID)
        self.assertTrue(record["follow"], "nothing moved: the follower still follows")
        self.assertEqual((state.origin, state.saved, state.renamed), ("mine", None, []))
        self.assertTrue(state.dirty)
        win, result = self.run_screen(screen, [CTRL_S, RIGHT, ENTER, RIGHT, ENTER, ESC, ESC])
        self.assertEqual((result.action, result.old_name, result.name), ("rename", "mine", "mine2"))
        self.assertTrue(any("was renamed to 'mine2'" in f for f in win.frames), "the on_removed report is shown")
        self.assertFalse(self.runtime.profiles.contains("mine"))
        self.assertEqual(self.runtime.profiles.load("mine2"), state.document)
        self.assertFalse(self.runtime.session_store.load(fx.FIXED_ID)["follow"],
                         "the follower of the vanished name is pinned")
        self.assertEqual(state.renamed, [("mine", "mine2")])


def state_module():
    return state


class EditorJsonTests(_EditorCase):
    def opener(self, transform):
        def open(path: str) -> int:
            p = Path(path)
            p.write_bytes(transform(p.read_bytes()))
            return 0

        return open

    def test_ctrl_g_applies_the_edited_json(self) -> None:
        state = self.state()
        old = f'"description": "{state.document["description"]}"'.encode()
        new = b'"description": "from the json editor"'
        screen = self.editor(state, open_in_editor=self.opener(lambda raw: raw.replace(old, new)),
                             suspender=lambda _win: contextlib.nullcontext())
        win, _ = self.run_screen(screen, [CTRL_G, ESC, ENTER])
        self.assertEqual(state.document["description"], "from the json editor")
        self.assertTrue(any("JSON applied from $EDITOR." in f for f in win.frames))

    def test_invalid_json_is_not_applied_and_the_edits_stay(self) -> None:
        state = self.state()
        state.unbind(self.free_agent(state.document))
        edited = copy.deepcopy(state.document)
        for transform, needle in (
            (lambda raw: b"{ not json", "JSON not applied: "),
            (lambda raw: raw.replace(b'"version": 2', b'"version": 1'), "JSON not applied: version: unsupported"),
        ):
            with self.subTest(needle=needle):
                screen = self.editor(state, open_in_editor=self.opener(transform),
                                     suspender=lambda _win: contextlib.nullcontext())
                win, _ = self.run_screen(screen, [CTRL_G, ESC, ENTER])
                self.assertTrue(any(needle in f for f in win.frames), needle)
                self.assertEqual(state.document, edited)

    def test_unchanged_and_failed_editor_runs(self) -> None:
        state = self.state()
        screen = self.editor(state, open_in_editor=lambda path: 0, suspender=lambda _w: contextlib.nullcontext())
        win, _ = self.run_screen(screen, [CTRL_G, ESC])
        self.assertTrue(any("JSON unchanged." in f for f in win.frames))
        screen = self.editor(state, open_in_editor=lambda path: 3, suspender=lambda _w: contextlib.nullcontext())
        win, _ = self.run_screen(screen, [CTRL_G, ESC])
        self.assertTrue(any("$EDITOR exited with status 3; JSON not applied" in f for f in win.frames))

    def test_the_default_opener_runs_visual_then_editor(self) -> None:
        screen = self.editor(self.state())
        screen.environ = {"VISUAL": "vis --flag", "EDITOR": "ed"}
        with mock.patch.object(tui.subprocess, "call", return_value=0) as call:
            self.assertEqual(screen._default_opener()("/x.json"), 0)
        call.assert_called_once()
        self.assertEqual(call.call_args.args, (["vis", "--flag", "/x.json"],))
        # _edit_profile: the runtime environment, with a PATH default.
        env = call.call_args.kwargs["env"]
        self.assertEqual((env["VISUAL"], env["EDITOR"]), ("vis --flag", "ed"))
        self.assertIn("PATH", env)


# ================================================================ binding picker


class PickerTests(_EditorCase):
    def picker(self, slot, *, lcat=None, eff=None, bindings=None, current=None) -> tui.BindingPicker:
        lcat = lcat or self.lcat
        eff = eff or self.eff
        rows = views.line_rows(lcat, eff, custom_ids=frozenset())
        model = views.picker_rows(rows, slot=slot, bindings=bindings or {}, lcat=lcat, eff=eff, current=current)
        return tui.BindingPicker(model, palette=M)

    def keys_of(self, picker) -> set[str]:
        return {item.key for item in picker.model.items if item.kind == "line"}

    def test_admission_per_slot(self) -> None:
        rows = {r.key: r for r in views.line_rows(self.lcat, self.eff, custom_ids=frozenset())}
        self.assertEqual(self.keys_of(self.picker(catalog.LEAD_ROLE)), {k for k, r in rows.items() if r.lead_capable})
        restricted = [k for k, r in rows.items() if r.agents_capable and r.roles != "all"]
        self.assertTrue(restricted, f"{SPEC} §7.1: the fixture has a roles-restricted line")
        for rid in catalog.AGENT_ROLE_IDS:
            with self.subTest(slot=rid):
                keys = self.keys_of(self.picker(rid))
                self.assertEqual(keys, {k for k, r in rows.items() if r.admits(rid)})
                for key in restricted:
                    self.assertEqual(key in keys, rid in rows[key].roles)
        self.assertEqual(self.keys_of(self.picker("binding")),
                         {k for k, r in rows.items() if r.lead_capable or r.agents_capable})
        workflow = self.picker("workflow")
        self.assertEqual(workflow.model.items[0].kind, "off")
        self.assertEqual(self.keys_of(workflow), {k for k, r in rows.items() if r.agents_capable})
        for item in workflow.model.items:
            self.assertNotIn(profile.ULTRACODE, item.text)

    def test_new_is_excluded_then_admitted(self) -> None:
        key = _agent_key(self.lcat, "cm-explorer")
        lcat = _replace_lines(self.lcat, **{key: {**self.lcat.lines[key], "status": "new"}})
        self.assertNotIn(key, self.keys_of(self.picker("cm-explorer", lcat=lcat)))
        eff = dataclasses.replace(self.eff, admitted_lines=frozenset({key}))
        self.assertIn(key, self.keys_of(self.picker("cm-explorer", lcat=lcat, eff=eff)))

    def test_a_disabled_provider_is_dim_and_never_chosen(self) -> None:
        provider = next(iter(sorted({e["provider"] for e in self.lcat.lines.values()})))
        eff = dataclasses.replace(self.eff, providers_enabled={**self.eff.providers_enabled, provider: False})
        picker = self.picker(catalog.LEAD_ROLE, eff=eff)
        off = [i for i, item in enumerate(picker.model.items)
               if item.kind == "line" and self.lcat.lines[item.key]["provider"] == provider]
        self.assertTrue(off)
        frame = self.frame(picker, width=100)
        for index in off:
            item = picker.model.items[index]
            self.assertFalse(item.selectable)
            self.assertEqual(item.role, "dim")
            self.assertIn("(provider off — G)", frame)
        picker.index = picker.model.items.index(next(i for i in picker.model.items if i.selectable))
        _win, result = self.run_screen(picker, [ENTER])
        self.assertNotEqual(self.lcat.lines[result[1]]["provider"], provider)

    def test_custom_models_are_absent(self) -> None:
        direct = next(pid for pid, p in self.runtime.catalog.providers.items() if p["transport"]["kind"] == "direct")
        custom.add_model(
            self.runtime.environ, "picker-custom", wire_model="picker-custom-wire", provider=direct,
            context_tokens=200_000, display="Picker Custom · test", created_via="manual",
            catalog_providers=self.runtime.catalog.providers, catalog_models=self.runtime.catalog.lines,
            retired_models=self.runtime.catalog.retired,
        )
        runtime = self.make_runtime(self.tmp / "root")  # a fresh Runtime re-reads custom.json
        lcat = runtime.lineup_catalog()
        self.assertIn("picker-custom", lcat.lines)
        for slot in (catalog.LEAD_ROLE, "binding"):
            self.assertNotIn("picker-custom", self.keys_of(self.picker(slot, lcat=lcat, eff=runtime.current_effective())))

    def test_v_shows_the_models_full_details(self) -> None:
        rid = "cm-analyst"
        key = _agent_key(self.lcat, rid)
        picker = self.picker(rid, current={"model": key, "effort": self.lcat.lines[key]["default_effort"]})
        item = picker.model.items[picker.index]
        self.assertEqual(item.key, key)
        win, result = self.run_screen(picker, ["v", ESC, ESC], height=40, width=120)
        self.assertIsNone(result)
        details = next(frame for frame in win.frames if "model — details" in frame)
        flat = " ".join(details.split())
        for line in [item.text, *item.details][:3]:
            if line:
                self.assertIn(" ".join(line.split())[:40], flat)

    def test_the_slots_role_description_is_the_subtitle_and_in_v(self) -> None:
        rid = "cm-analyst"
        role = self.lcat.roles[rid]
        described = f"{profile.label(rid)} ({role['grade']}): {role['description']}"
        picker = self.picker(rid)
        lines = self.frame(picker, height=40, width=120).splitlines()
        self.assertEqual(lines[1].strip(), f"{profile.label(rid)} — choose model")
        self.assertEqual(lines[2].strip(), described[:116] + "…")
        self.assertEqual(views.role_description(self.lcat, rid), described)
        self.assertEqual(picker.model.subtitle, described)
        # V shows the description in full, wrapped.
        win, result = self.run_screen(picker, ["v", ESC, ESC], height=40, width=120)
        self.assertIsNone(result)
        details = next(frame for frame in win.frames if "model — details" in frame)
        self.assertIn(" ".join(described.split()), " ".join(details.split()))
        # A named binding or the workflow default has no role: no subtitle.
        self.assertEqual(self.picker("binding").model.subtitle, "")
        self.assertEqual(self.picker("workflow").model.subtitle, "")

    def test_the_zero_model_row_and_named_rows(self) -> None:
        zero = views.zero_model_providers(self.lcat)
        self.assertTrue(zero, f"{SPEC} §7.1: the fixture has a provider without lines")
        rid = "cm-analyst"
        key = _agent_key(self.lcat, rid)
        effort = self.lcat.lines[key]["default_effort"]
        picker = self.picker(rid, bindings={"nb": {"model": key, "effort": effort}}, current={"use": "nb"})
        frame = self.frame(picker, height=40)
        self.assertIn(f"{' · '.join(zero)}     configured · no models", frame)
        self.assertIn(f"named       nb → {self.short(key)} · {effort}", frame)
        self.assertEqual(picker.model.items[picker.index].kind, "named")
        self.assertIn("(named binding)", frame)
        _win, result = self.run_screen(picker, [RIGHT, ENTER], height=40)
        self.assertEqual(result, ("use", "nb"))

    def test_effort_cycling_stops_at_the_ends(self) -> None:
        rid = next(r for r in catalog.AGENT_ROLE_IDS
                   if any(len(profile.declared_efforts(e)) > 1 and "agents" in e["capabilities"]
                          and (e["roles"] == "all" or r in e["roles"]) for e in self.lcat.lines.values()))
        key = next(k for k, e in self.lcat.lines.items()
                   if len(profile.declared_efforts(e)) > 1 and "agents" in e["capabilities"]
                   and (e["roles"] == "all" or rid in e["roles"]))
        efforts = profile.declared_efforts(self.lcat.lines[key])
        picker = self.picker(rid, current={"model": key, "effort": efforts[0]})
        self.assertEqual(picker.effort(), efforts[0])
        _win, result = self.run_screen(picker, [LEFT, *([RIGHT] * (len(efforts) + 2)), ENTER])
        self.assertEqual(result, ("bind", key, efforts[-1]))
        picker = self.picker(rid, current={"model": key, "effort": efforts[-1]})
        _win, result = self.run_screen(picker, [*([LEFT] * (len(efforts) + 2)), ENTER])
        self.assertEqual(result, ("bind", key, efforts[0]))

    def test_workflow_off_and_esc(self) -> None:
        self.assertEqual(self.run_screen(self.picker("workflow"), [HOME, ENTER])[1], ("off",))
        self.assertIsNone(self.run_screen(self.picker("workflow"), [ESC])[1])

    def test_help_and_the_effort_line(self) -> None:
        picker = self.picker(catalog.LEAD_ROLE)
        frame = self.frame(picker)
        self.assertIn(f"effort: [{profile.ULTRACODE}]", frame)
        self.assertIn("Enter choose · V details · ? help · Esc back", frame)
        win, _ = self.run_screen(picker, ["?", ENTER, ESC], height=40, width=100)
        self.assertTrue(any("catalog lines whose roles admit this slot and operator lines (◇)"
                            in " ".join(" ".join(line.strip(" |") for line in f.splitlines()).split())
                            for f in win.frames))


# ================================================================ routing


class RoutingTests(_EditorCase):
    def lineup_for(self, document) -> profile.ResolvedLineup:
        ev = profile.evaluate(document, self.lcat, bindings={}, effective=self.eff)
        self.assertEqual(ev.errors, ())
        return ev.lineup

    def test_the_default_seed_table(self) -> None:
        lineup = self.lineup_for(self.default())
        screen = tui.RoutingPreviewScreen(lineup, name=catalog.DEFAULT_SEED, palette=M)
        frame = self.frame(screen, width=120)
        self.assertIn(f"review routing — {catalog.DEFAULT_SEED}", frame)
        self.assertIn("author", frame)
        self.assertIn("high-stakes change", frame)
        for author, normal, high in views.routing_rows(lineup):
            line = next(l for l in frame.splitlines() if l.startswith(f"  {author} "))
            self.assertIn(normal, line)
            self.assertIn(high, line)
        self.assertIn("✓ cross-family", frame)
        self.assertIn("° preferred reviewer shares", frame)
        self.assertTrue(frame.splitlines()[-1].strip().endswith("? help · Esc back"))

    def test_cells_cover_every_mark_and_reason(self) -> None:
        texts: set[str] = set()
        for name in self.runtime.profiles.names():
            document = self.runtime.profiles.load(name)
            ev = profile.evaluate(document, self.lcat, bindings={}, effective=self.eff)
            for row in views.routing_rows(ev.lineup):
                texts |= {row[1], row[2]}
        document = self.default()
        for rid in profile.REVIEWER_IDS:
            document["agents"].pop(rid, None)
        for row in views.routing_rows(self.lineup_for(document)):
            texts |= {row[1], row[2]}
        joined = "\n".join(texts)
        for mark in ("✓", "≈", "°", "— (its check)", "— (no reviewer bound)"):
            self.assertIn(mark, joined, mark)

    def test_a_direct_lineup_has_one_line(self) -> None:
        direct = next(n for n in self.runtime.profiles.names() if self.runtime.profiles.load(n)["agents"] == {})
        screen = tui.RoutingPreviewScreen(self.lineup_for(self.runtime.profiles.load(direct)), name=direct, palette=M)
        frame = self.frame(screen)
        self.assertIn("no agents: nothing to route", frame)
        win, _ = self.run_screen(screen, ["?", ENTER, ESC])
        self.assertTrue(any("review routing — help" in f for f in win.frames))


# ================================================================ named bindings


class NamedBindingsTests(_EditorCase):
    def screen(self) -> tui.NamedBindingsScreen:
        return tui.NamedBindingsScreen(self.lcat, self.eff, callbacks=self.callbacks(), palette=M)

    def binding_key(self) -> str:
        return _agent_key(self.lcat, "cm-analyst")

    def seed_binding(self, name="nb") -> tuple[str, str]:
        key = self.binding_key()
        effort = self.lcat.lines[key]["default_effort"]
        self.runtime.bindings.set(name, key, effort, cat=self.lcat)
        return key, effort

    def test_rows_and_used_by(self) -> None:
        key, effort = self.seed_binding()
        self.runtime.profiles.duplicate(catalog.DEFAULT_SEED, "user-a")
        self.assertTrue(_admits(self.lcat, key, "cm-reviewer"))

        def use_twice(document: dict) -> None:
            document["agents"]["cm-analyst"] = {"use": "nb"}
            document["agents"]["cm-reviewer"] = {"use": "nb"}

        self.runtime.profiles.update("user-a", use_twice)
        frame = self.frame(self.screen())
        self.assertIn(tui.NAMED_BINDINGS_TITLE, frame)
        row = next(l for l in frame.splitlines() if l.startswith("  nb "))
        self.assertIn(f"{self.short(key)} · {effort}", row)
        self.assertTrue(row.rstrip().endswith("used by 1"), "a profile using the name in two slots counts once")

    def test_add_through_the_picker(self) -> None:
        key = self.binding_key()
        screen = self.screen()
        picker_rows = views.picker_rows(
            views.line_rows(self.lcat, self.eff, custom_ids=frozenset()), slot="binding", bindings={},
            lcat=self.lcat, eff=self.eff, current=None,
        )
        position = next(i for i, item in enumerate(picker_rows.items) if item.key == key)
        first = picker_rows.selected
        moves = [DOWN] * sum(1 for i in range(first + 1, position + 1) if picker_rows.items[i].selectable)
        self.run_screen(screen, ["A", *"added", ENTER, ENTER, *moves, ENTER, ESC])
        stored = self.runtime.bindings.bindings()
        self.assertEqual(stored["added"]["model"], key)

    def test_b1_to_b9_messages_are_shown_verbatim(self) -> None:
        callbacks = self.callbacks()
        key, effort = self.seed_binding()
        lcat = self.lcat
        null_key = fx.needs_choice_key(self.runtime)
        messages = {
            "B1": callbacks.set_binding(None, "Bad_Name", key, effort),
            "B2": callbacks.set_binding(None, key, key, effort),
            "B3": callbacks.set_binding(None, null_key, key, effort),
            "B4": callbacks.set_binding(None, "b-four", "no-such-line", effort),
            "B5": callbacks.set_binding(None, "b-five", null_key, effort),
            "B7": callbacks.set_binding(None, "b-seven", key, "not-an-effort"),
        }
        new_lcat = _replace_lines(lcat, **{key: {**lcat.lines[key], "status": "new"}})
        with mock.patch.object(self.runtime, "lineup_catalog", lambda: new_lcat):
            messages["B6"] = callbacks.set_binding(None, "b-six", key, effort)
        self.runtime.profiles.duplicate(catalog.DEFAULT_SEED, "user-b")
        self.runtime.profiles.update("user-b", lambda d: d["agents"].__setitem__("cm-analyst", {"use": "nb"}))
        messages["B8"] = callbacks.delete_binding("nb")
        refusing = next(k for k, e in lcat.lines.items() if "agents" in e["capabilities"]
                        and e["roles"] != "all" and "cm-analyst" not in e["roles"])
        messages["B9"] = callbacks.set_binding(None, "nb", refusing, lcat.lines[refusing]["default_effort"])
        for code, message in messages.items():
            with self.subTest(code=code):
                self.assertIsInstance(message, str, code)
                self.assertTrue(message, code)
        self.assertIn("bindings.Bad_Name", messages["B1"])
        self.assertIn(f"collides with catalog model key {key!r}", messages["B2"])
        self.assertIn(f"collides with retired catalog key {null_key!r}", messages["B3"])
        self.assertIn("unknown model 'no-such-line'", messages["B4"])
        self.assertIn("bind the live line", messages["B5"])
        self.assertIn("is New · Off (status new) until admitted", messages["B6"])
        self.assertIn("'not-an-effort' is not declared by", messages["B7"])
        self.assertEqual(messages["B8"], "named binding 'nb' is used by profiles: user-b (agents.cm-analyst)")
        self.assertIn("user-b", messages["B9"])
        self.assertEqual(self.runtime.bindings.bindings()["nb"]["model"], key, "a refused change writes nothing")
        # And on the screen: the X refusal shows the store's text verbatim.
        win, _ = self.run_screen(self.screen(), ["X", ENTER, ESC], width=120)
        self.assertTrue(any(messages["B8"] in f for f in win.frames))

    def test_enter_edits_a_binding_through_the_picker(self) -> None:
        key, _effort = self.seed_binding()
        other = next(k for k, e in sorted(self.lcat.lines.items())
                     if k != key and "agents" in e["capabilities"] and e.get("status") != "new"
                     and _admits(self.lcat, k, "cm-analyst"))
        effort = self.lcat.lines[other]["default_effort"]
        with mock.patch.object(tui.BindingPicker, "run", return_value=("bind", other, effort)) as picker:
            self.run_screen(self.screen(), [ENTER, ESC, ESC])
        picker.assert_called_once()
        self.assertEqual(self.runtime.bindings.bindings()["nb"], {"model": other, "effort": effort})

    def test_delete_removes_an_unused_binding(self) -> None:
        self.seed_binding("gone")
        self.run_screen(self.screen(), ["X", ENTER, ESC])
        self.assertNotIn("gone", self.runtime.bindings.bindings())

    def test_a_set_runs_the_propagation_prompt(self) -> None:
        key, effort = self.seed_binding()
        self.runtime.profiles.duplicate(catalog.DEFAULT_SEED, "user-c")
        self.runtime.profiles.update("user-c", lambda d: d["agents"].__setitem__("cm-analyst", {"use": "nb"}))
        fx.v4_record(self.runtime, "user-c", managed_id=fx.FIXED_ID, ended=True)
        other = self.other_effort(key, effort)
        callbacks = self.callbacks()
        win = FakeWindow([ESC], height=24, width=80)
        self.assertIsNone(callbacks.set_binding(win, "nb", key, other or effort))
        frame = win.frames[0]
        self.assertIn("saved binding nb (used by user-c)", frame)
        self.assertIn("1 session follows this profile:", frame)

    def test_the_editor_n_key_reloads_bindings(self) -> None:
        key, effort = self.seed_binding()
        state = self.state()
        self.assertEqual(state.bindings["nb"]["model"], key)
        self.runtime.bindings.set("later", key, effort, cat=self.lcat)
        self.run_screen(self.editor(state, callbacks=self.callbacks()), ["N", ESC, ESC])
        self.assertIn("later", state.bindings)

    def test_read_only_without_callbacks(self) -> None:
        screen = tui.NamedBindingsScreen(self.lcat, self.eff, palette=M, bindings={})
        win, _ = self.run_screen(screen, ["A", ESC])
        self.assertTrue(any(tui.NAMED_BINDINGS_READ_ONLY in f for f in win.frames))


def _admits(lcat, key, rid) -> bool:
    roles = lcat.lines[key]["roles"]
    return roles == "all" or rid in roles


# ================================================================ propagation


class PropagationTests(_EditorCase):
    def followers(self) -> None:
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title="unknown one")
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID, ended=True, title="by proc")
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.THIRD_ID, ended=True)
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FOURTH_ID, pending=True)
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIFTH_ID, generation=0, scope=False)
        self.runtime.proc_ids = frozenset({fx.OTHER_ID})

    def screen(self, names=None) -> cli._PropagationScreen:
        return cli._PropagationScreen(self.runtime, M, names or [catalog.DEFAULT_SEED])

    def test_rows_per_state(self) -> None:
        self.followers()
        screen = self.screen()
        self.assertEqual(screen.states[fx.FIXED_ID], "unknown")
        self.assertEqual(screen.states[fx.OTHER_ID], "live", "cf[13]: a follower live by /proc")
        self.assertEqual(screen.states[fx.THIRD_ID], "ended")
        frame = self.frame(screen, width=100)
        self.assertIn(f"saved {catalog.DEFAULT_SEED}", frame)
        self.assertIn("5 sessions follow this profile:", frame)
        self.assertIn('◐ unknown   project        "unknown one"   apply now (then /reload-plugins there)', frame)
        self.assertIn('● running   project        "by proc"   apply now (then /reload-plugins there)', frame)
        self.assertIn("○ ended     project        —   applies at next resume", frame)
        self.assertIn("has a pending relaunch change; not applied", frame)
        self.assertIn("no lineup-enabled scope yet; applies at next resume", frame)
        self.assertIn("A apply to running · L later (next resume) · ? help · Esc keep sessions as they are", frame)
        narrow = self.frame(screen, width=70)
        self.assertNotIn('"unknown one"', narrow)
        for line in narrow.splitlines():
            self.assertLessEqual(len(line), 70 - 3 + 2)

    def test_a_is_hidden_without_a_running_follower(self) -> None:
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.THIRD_ID, ended=True)
        screen = self.screen()
        self.assertFalse(screen.offers_apply)
        frame = self.frame(screen)
        self.assertNotIn("A apply to running", frame)
        with mock.patch.object(lineup, "on_saved") as on_saved:
            self.run_screen(screen, ["A", ESC])
        on_saved.assert_not_called()

    def test_a_and_l_call_on_saved_and_show_its_lines(self) -> None:
        self.followers()
        for key, live in (("A", True), ("L", False)):
            with self.subTest(key=key):
                calls = []

                def fake(runtime, names, *, apply_live, out, now=None):
                    calls.append((list(names), apply_live))
                    out.write(f"report line apply_live={apply_live}\n")
                    return 0

                with mock.patch.object(lineup, "on_saved", fake):
                    win, result = self.run_screen(self.screen(), [key, ESC])
                self.assertEqual(calls, [([catalog.DEFAULT_SEED], live)])
                self.assertEqual(result, "applied" if live else "later")
                report = next(f for f in win.frames if f"propagation — {catalog.DEFAULT_SEED}" in f)
                self.assertIn(f"report line apply_live={live}", report)

    def test_esc_calls_nothing(self) -> None:
        self.followers()
        with mock.patch.object(lineup, "on_saved") as on_saved:
            _win, result = self.run_screen(self.screen(), [ESC])
        self.assertIsNone(result)
        on_saved.assert_not_called()

    def test_a_applies_live_through_on_saved(self) -> None:
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID, ended=True)
        document = self.default()
        rid = next(r for r in catalog.AGENT_ROLE_IDS if r in document["agents"]
                   and self.other_effort(document["agents"][r]["model"], document["agents"][r]["effort"]))
        document["agents"][rid]["effort"] = self.other_effort(document["agents"][rid]["model"], document["agents"][rid]["effort"])
        self.runtime.profiles.save(document)
        before = self.runtime.session_store.load(fx.FIXED_ID)["lineup_generation"]
        win, _ = self.run_screen(self.screen(), ["A", ESC])
        report = next(f for f in win.frames if "propagation — " in f)
        self.assertIn("applied live (lineup gen", report)
        self.assertIn("applies at the next resume", report)
        self.assertEqual(self.runtime.session_store.load(fx.FIXED_ID)["lineup_generation"], before + 1)

    def test_unreadable_records_and_no_followers(self) -> None:
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        (self.runtime.session_store.root / "sessions" / f"{fx.OTHER_ID}.json").write_text("{broken")
        frame = self.frame(self.screen())
        self.assertIn("? 1 session record(s) unreadable; skipped", frame)
        other = next(n for n in self.runtime.profiles.names() if n != catalog.DEFAULT_SEED)
        win = FakeWindow([], height=24, width=80)
        self.assertEqual(cli.run_propagation_screen(self.runtime, win, M, [other]), "none")
        self.assertEqual(win.frames, [])

    def test_several_profiles_and_the_help(self) -> None:
        self.followers()
        other = next(n for n in self.runtime.profiles.names() if n != catalog.DEFAULT_SEED)
        screen = self.screen([catalog.DEFAULT_SEED, other])
        self.assertIn("5 sessions follow these profiles:", self.frame(screen))
        win, _ = self.run_screen(screen, ["?", ENTER, ESC], height=40, width=100)
        self.assertTrue(any("propagation — help" in f for f in win.frames))

    def test_more_followers_than_rows_scroll(self) -> None:
        # Every follower is reachable; the last row says which are shown.
        count = 25
        for index in range(count):
            fx.v4_record(self.runtime, catalog.DEFAULT_SEED, ended=True, title=f"follower {index:02d}",
                         managed_id=f"{index + 1:08x}-0000-4000-8000-{index + 1:012x}")
        screen = self.screen()
        first = self.frame(screen)
        self.assertIn(f"{count} sessions follow this profile:", first)
        self.assertRegex(first, rf"1-\d+ of {count} \(↑/↓ scroll\)")
        win, _ = self.run_screen(screen, [DOWN] * (count + 5) + ["k", ESC])
        seen = {f"follower {index:02d}" for index in range(count) if any(f"follower {index:02d}" in f for f in win.frames)}
        self.assertEqual(len(seen), count)
        self.assertTrue(any(f"of {count} (↑/↓ scroll)" in f and f"-{count} of" in f for f in win.frames))


# ================================================================ floors


class EditorFloorTests(_EditorCase):
    def test_every_editor_screen_floor(self) -> None:
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        state = self.state()
        rows = views.line_rows(self.lcat, self.eff, custom_ids=frozenset())
        factories = {
            "editor": lambda: self.editor(self.state()),
            "picker": lambda: tui.BindingPicker(
                views.picker_rows(rows, slot="cm-implementer", bindings={}, lcat=self.lcat, eff=self.eff, current=None),
                palette=M),
            "routing": lambda: tui.RoutingPreviewScreen(state.evaluation.lineup, name="x", palette=M),
            "named": lambda: tui.NamedBindingsScreen(self.lcat, self.eff, callbacks=self.callbacks(), palette=M),
            "propagation": lambda: cli._PropagationScreen(self.runtime, M, [catalog.DEFAULT_SEED]),
        }
        for name, factory in factories.items():
            with self.subTest(screen=name):
                self.assertFloor(factory)

    def test_below_the_floor_only_esc_acts(self) -> None:
        screen = self.editor()
        rows, _cols = screen.min_size(80)
        win, result = self.run_screen(screen, [ENTER, "U", CTRL_S, ESC], height=rows - 1)
        self.assertIsNone(result)
        self.assertFalse(screen.state.dirty)


# ================================================================ entry


class ProfileEditEntryTests(_EditorCase):
    """The 3.0 form of 2.x ``RunEditorChooserTests``: curses when capable, ``$EDITOR`` otherwise."""

    def run_main(self, argv, *, capable=True):
        inp, out = io.StringIO(), io.StringIO()
        with mock.patch.object(claude_multi.tui, "streams_curses_capable", return_value=capable), \
                mock.patch('claude_multi.cli.screens.profile_editor._run_profile_editor', return_value=0) as editor, \
                mock.patch('claude_multi.cli.commands.profile._edit_profile', return_value=True) as fallback:
            code = cli.main(argv, runtime=self.runtime, input_stream=inp, output_stream=out, interactive=True)
        return code, editor, fallback

    def test_capable_terminal_opens_the_editor(self) -> None:
        for argv, verb, name in ((["profile", "edit", catalog.DEFAULT_SEED], "edit", catalog.DEFAULT_SEED),
                                 (["profile", "new", "brand-new"], "new", "brand-new")):
            with self.subTest(argv=argv):
                code, editor, fallback = self.run_main(argv)
                self.assertEqual(code, 0)
                self.assertEqual(editor.call_args.args[1:3], (name, verb))
                fallback.assert_not_called()

    def test_not_capable_and_line_flag_use_editor_env(self) -> None:
        for argv, capable in ((["profile", "edit", catalog.DEFAULT_SEED], False),
                              (["--line", "profile", "edit", catalog.DEFAULT_SEED], True),
                              (["--line", "compose", "edit", catalog.DEFAULT_SEED], True)):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    _code, editor, fallback = self.run_main(argv, capable=capable)
                editor.assert_not_called()
                fallback.assert_called_once()

    def test_a_curses_failure_falls_back_to_editor_env(self) -> None:
        inp, out = io.StringIO(), io.StringIO()
        with mock.patch.object(claude_multi.tui, "streams_curses_capable", return_value=True), \
                mock.patch.object(claude_multi.tui, "run_profile_editor", side_effect=tui.CursesError("boom")), \
                mock.patch('claude_multi.cli.commands.profile._edit_profile', return_value=True) as fallback:
            cli.main(["profile", "edit", catalog.DEFAULT_SEED], runtime=self.runtime, input_stream=inp,
                     output_stream=out, interactive=True)
        fallback.assert_called_once()

    def on_fake_curses(self, keys):
        """``run_curses_on_streams`` driving the real screens on a FakeWindow."""

        def run(app, _input, _output, *, palette=None):
            return app(FakeWindow(list(keys), height=24, width=80))

        return mock.patch.object(claude_multi.tui, "run_curses_on_streams", run)

    def test_an_error_after_the_editor_started_is_reported_not_fallen_back(self) -> None:
        # Fail closed (AGENTS.md §4): only a curses start-up failure
        # takes the $EDITOR path; an error once the session is up (here
        # the propagation after a committed save) surfaces, the save reported.
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, ended=True)
        errors = (
            (state.StateError(errno.ENOSPC, "No space left on device"), "[Errno 28] No space left on device"),
            (OSError(errno.EIO, "I/O error"), "the profile editor stopped: [Errno 5] I/O error"),
        )
        for error, text in errors:
            with self.subTest(error=type(error).__name__):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(claude_multi.tui, "streams_curses_capable", return_value=True), \
                        self.on_fake_curses([CTRL_S, "L", ESC, ESC]), \
                        mock.patch.object(lineup, "on_saved", side_effect=error), \
                        mock.patch('claude_multi.cli.commands.profile._edit_profile', return_value=True) as fallback, \
                        contextlib.redirect_stderr(err):
                    code = cli.main(["profile", "edit", catalog.DEFAULT_SEED], runtime=self.runtime,
                                    input_stream=io.StringIO(), output_stream=out, interactive=True)
                fallback.assert_not_called()
                self.assertEqual(code, 1)
                self.assertIn(f"Saved profile {catalog.DEFAULT_SEED!r}.\n", out.getvalue())
                self.assertIn(f"claude-multi: {text}", err.getvalue())

    def test_ctrl_s_saves_and_offers_the_change_to_the_followers(self) -> None:
        out = io.StringIO()
        with self.on_fake_curses([CTRL_S, ESC]), \
                mock.patch('claude_multi.cli.screens.propagation.run_propagation_screen',
                           return_value=None) as propagation:
            code = cli._run_profile_editor(self.runtime, catalog.DEFAULT_SEED, "edit",
                                           input_stream=io.StringIO(), output_stream=out)
        self.assertEqual(code, 0)
        propagation.assert_called_once()
        self.assertEqual(propagation.call_args.args[3], [catalog.DEFAULT_SEED])
        self.assertEqual(out.getvalue(), f"Saved profile {catalog.DEFAULT_SEED!r}.\n")

    def test_a_sigint_during_the_propagation_prompt_still_reports_the_save(self) -> None:
        # The save is marked before the prompt runs.
        out = io.StringIO()
        with self.on_fake_curses([CTRL_S, ESC]), \
                mock.patch('claude_multi.cli.screens.propagation.run_propagation_screen', side_effect=KeyboardInterrupt):
            code = cli._run_profile_editor(self.runtime, catalog.DEFAULT_SEED, "edit",
                                           input_stream=io.StringIO(), output_stream=out)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), f"Saved profile {catalog.DEFAULT_SEED!r}.\n")

    def test_the_glue_builds_the_state_and_reports(self) -> None:
        captured = {}

        def fake_run(state, **kwargs):
            captured["state"] = state
            captured.update(kwargs)
            return None

        out = io.StringIO()
        with mock.patch.object(claude_multi.tui, "run_profile_editor", fake_run):
            code = cli._run_profile_editor(self.runtime, "brand-new", "new", input_stream=io.StringIO(),
                                           output_stream=out)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "Nothing was saved.\n")
        state = captured["state"]
        self.assertIsNone(state.origin)
        self.assertEqual(state.name, "brand-new")
        self.assertNotIn("seed", state.document)
        self.assertEqual(captured["initial_focus"], "general")
        saved = tui.ProfileEditorOutcome("save", catalog.DEFAULT_SEED, {})
        out = io.StringIO()
        with mock.patch.object(claude_multi.tui, "run_profile_editor", return_value=saved):
            cli._run_profile_editor(self.runtime, catalog.DEFAULT_SEED, "edit", input_stream=io.StringIO(),
                                    output_stream=out)
        self.assertEqual(out.getvalue(), f"Saved profile {catalog.DEFAULT_SEED!r}.\n")

    def test_the_seed_update_reaches_the_state(self) -> None:
        self.runtime.profiles.install_seeds()
        self.runtime.profiles.update(catalog.DEFAULT_SEED, lambda d: d.pop("seed"))
        state = cli._profile_editor_state(self.runtime, catalog.DEFAULT_SEED, "edit")
        shipped = self.runtime.catalog.seed_profiles[catalog.DEFAULT_SEED]["seed"]["version"]
        self.assertEqual(state.seed_update, (None, shipped))

    def test_the_refusal_keeps_its_line_prefix(self) -> None:
        text = cli.PROFILE_EDITOR_REFUSAL.format(verb="edit")
        self.assertTrue(text.startswith("profile edit needs an editor: set $VISUAL or $EDITOR"))
        self.assertIn("full-screen editor", text)


class EditorConflictTests(_EditorCase):
    """A profile saved elsewhere while it is edited: Overwrite, Reload or Cancel."""

    def setUp(self) -> None:
        super().setUp()
        document = copy.deepcopy(self.default())
        document.pop("seed", None)
        document["name"] = "mine"
        self.runtime.profiles.save(document)

    def editing(self) -> tuple[tui.ProfileEditorState, tui.ProfileEditorScreen]:
        state = cli._profile_editor_state(self.runtime, "mine", "edit")
        self.assertEqual(state.loaded_digest, self.runtime.profiles.digest("mine"))
        state.set_general(description="edited here")
        callbacks = cli._profile_editor_callbacks(self.runtime, M, editor_state=state)
        return state, tui.ProfileEditorScreen(state, palette=M, callbacks=callbacks, environ=self.runtime.environ)

    def elsewhere(self) -> None:
        self.runtime.profiles.update("mine", lambda doc: doc.update(description="saved elsewhere"))

    def run_screen(self, screen, keys):
        win = FakeWindow(list(keys), height=24, width=80)
        return win, screen.run(win)

    def dialog(self, win) -> str:
        title = tui.PROFILE_FORM_CHANGED_TITLE.format(name="mine")
        return next(f for f in win.frames if title in f)

    def test_cancel_keeps_the_edits_and_the_stored_profile(self) -> None:
        state, screen = self.editing()
        self.elsewhere()
        win, result = self.run_screen(screen, [CTRL_S, ENTER, ESC, ENTER])
        dialog = self.dialog(win)
        for label, _value in tui.PROFILE_FORM_CHANGED_BUTTONS:
            self.assertIn(f"[ {label} ]", dialog)
        self.assertIsNone(result)
        self.assertEqual(self.runtime.profiles.load("mine")["description"], "saved elsewhere")

    def test_overwrite_saves_the_edits(self) -> None:
        state, screen = self.editing()
        self.elsewhere()
        win, result = self.run_screen(screen, [CTRL_S, RIGHT, RIGHT, ENTER, ESC])
        self.assertEqual(result.name, "mine")
        self.assertEqual(self.runtime.profiles.load("mine")["description"], "edited here")
        self.assertEqual(state.loaded_digest, self.runtime.profiles.digest("mine"))
        # The next save checks against the new digest and needs no dialog.
        self.assertFalse(state.dirty)

    def test_reload_shows_the_stored_profile(self) -> None:
        state, screen = self.editing()
        self.elsewhere()
        win, result = self.run_screen(screen, [CTRL_S, RIGHT, ENTER, ESC])
        self.assertIsNone(result)
        self.assertEqual(state.document["description"], "saved elsewhere")
        self.assertFalse(state.dirty)
        self.assertEqual(state.loaded_digest, self.runtime.profiles.digest("mine"))
        self.assertIn(tui.PROFILE_FORM_RELOADED.format(name="mine"), win.frames[-1])

    def test_a_write_right_after_a_save_is_still_asked_about(self) -> None:
        # Another window saves just after this editor's save released the
        # store lock: the editor's next save must not overwrite it unseen.
        state = cli._profile_editor_state(self.runtime, "mine", "edit")
        callbacks = cli._profile_editor_callbacks(self.runtime, M, editor_state=state)
        store_path = self.runtime.profiles.root / "mine.json"
        other = copy.deepcopy(self.runtime.profiles.load("mine"))
        other["description"] = "other window important changes"
        real_begin = profile.ProfileStore._begin
        armed: list[bool] = [True]

        class Lock:
            def __init__(self, lock):
                self.lock = lock

            def release(self):
                self.lock.release()
                if armed:
                    armed.clear()
                    state_module().atomic_write(store_path, strict_json.pretty_file_bytes(other))

        first = copy.deepcopy(state.document)
        first["description"] = "first editor version"
        with mock.patch.object(profile.ProfileStore, "_begin", lambda store: Lock(real_begin(store))):
            ok, _note = callbacks.save(FakeWindow([ESC] * 4), tui.ProfileEditorOutcome(
                "save", "mine", first, "mine", state.loaded_digest))
        self.assertTrue(ok)
        self.assertEqual(armed, [])
        self.assertEqual(self.runtime.profiles.load("mine")["description"], "other window important changes")
        self.assertNotEqual(state.loaded_digest, self.runtime.profiles.digest("mine"))
        second = copy.deepcopy(first)
        second["description"] = "second editor version"
        with self.assertRaises(tui.ProfileChanged):
            callbacks.save(FakeWindow([ESC] * 4), tui.ProfileEditorOutcome(
                "save", "mine", second, "mine", state.loaded_digest))
        self.assertEqual(self.runtime.profiles.load("mine")["description"], "other window important changes")

    def test_the_digest_after_a_save_is_the_digest_of_what_it_wrote(self) -> None:
        state, screen = self.editing()
        self.run_screen(screen, [CTRL_S, ESC])
        self.assertEqual(state.loaded_digest, self.runtime.profiles.digest("mine"))
        self.assertEqual(state.loaded_digest, "sha256:" + strict_json.sha256_hex(
            (self.runtime.profiles.root / "mine.json").read_bytes()))

    def test_an_unchanged_profile_saves_without_a_dialog(self) -> None:
        state, screen = self.editing()
        win, result = self.run_screen(screen, [CTRL_S, ESC])
        self.assertEqual(result.action, "save")
        self.assertFalse(any(tui.PROFILE_FORM_CHANGED_TITLE.format(name="mine") in f for f in win.frames))
        self.assertEqual(self.runtime.profiles.load("mine")["description"], "edited here")


class EditorRenameTests(_EditorCase):
    """Rename in the editor is the profile rename: the default moves with
    it and who follows it is shown first."""

    def setUp(self) -> None:
        super().setUp()
        document = copy.deepcopy(self.default())
        document.pop("seed", None)
        document["name"] = "mine"
        self.runtime.profiles.save(document)

    def outcome(self, state: tui.ProfileEditorState, name: str) -> tui.ProfileEditorOutcome:
        document = copy.deepcopy(state.document)
        document["name"] = name
        document["description"] = "edited before the rename"
        return tui.ProfileEditorOutcome("rename", name, document, "mine", state.loaded_digest)

    def test_renaming_the_default_moves_the_default(self) -> None:
        choices.update(self.runtime.environ, default_profile="mine")
        state = cli._profile_editor_state(self.runtime, "mine", "edit")
        callbacks = cli._profile_editor_callbacks(self.runtime, M, editor_state=state)
        win = FakeWindow([RIGHT, ENTER, ESC, ESC], height=24, width=80)
        ok, _note = callbacks.save(win, self.outcome(state, "ours"))
        self.assertTrue(ok)
        self.assertEqual(choices.read(self.runtime.environ).get("default_profile"), "ours")
        self.assertFalse(self.runtime.profiles.contains("mine"))
        self.assertEqual(self.runtime.profiles.load("ours")["description"], "edited before the rename")
        shown = next(f for f in win.frames if cli_text.EDITOR_RENAME_TITLE.format(old="mine", new="ours") in f)
        self.assertIn("It is your default profile; the default moves to the new name.", shown)
        self.assertEqual(state.loaded_digest, self.runtime.profiles.digest("ours"))
        self.assertEqual(state.renamed, [("mine", "ours")])

    def test_cancel_on_the_effects_renames_nothing(self) -> None:
        choices.update(self.runtime.environ, default_profile="mine")
        state = cli._profile_editor_state(self.runtime, "mine", "edit")
        callbacks = cli._profile_editor_callbacks(self.runtime, M, editor_state=state)
        ok, note = callbacks.save(FakeWindow([ENTER]), self.outcome(state, "ours"))
        self.assertEqual((ok, note), (False, "Nothing changed."))
        self.assertTrue(self.runtime.profiles.contains("mine"))
        self.assertFalse(self.runtime.profiles.contains("ours"))
        self.assertEqual(choices.read(self.runtime.environ).get("default_profile"), "mine")

    def test_a_rename_without_followers_or_default_asks_nothing_more(self) -> None:
        state = cli._profile_editor_state(self.runtime, "mine", "edit")
        callbacks = cli._profile_editor_callbacks(self.runtime, M, editor_state=state)
        win = FakeWindow([ESC, ESC], height=24, width=80)
        ok, _note = callbacks.save(win, self.outcome(state, "ours"))
        self.assertTrue(ok)
        self.assertFalse(any(cli_text.EDITOR_RENAME_TITLE.format(old="mine", new="ours") in f for f in win.frames))
        self.assertTrue(self.runtime.profiles.contains("ours"))

    def test_a_profile_changed_elsewhere_is_never_renamed_unseen(self) -> None:
        state = cli._profile_editor_state(self.runtime, "mine", "edit")
        callbacks = cli._profile_editor_callbacks(self.runtime, M, editor_state=state)
        self.runtime.profiles.update("mine", lambda doc: doc.update(description="saved elsewhere"))
        with self.assertRaises(tui.ProfileChanged):
            callbacks.save(FakeWindow([ESC]), self.outcome(state, "ours"))
        self.assertEqual(self.runtime.profiles.load("mine")["description"], "saved elsewhere")
        self.assertFalse(self.runtime.profiles.contains("ours"))


class DuplicateFallbackTests(_EditorCase):
    def test_another_fallback_for_the_same_provider_is_a_check(self) -> None:
        store = self.runtime.profiles
        owner = next(name for name in sorted(store.names()) if store.load(name).get("primary_provider"))
        provider = store.load(owner)["primary_provider"]
        document = copy.deepcopy(self.default())
        document.pop("seed", None)
        document["name"] = "second"
        store.save(document)
        state = cli._profile_editor_state(self.runtime, "second", "edit")
        self.assertIsNone(state.duplicate_fallback())
        state.set_general(primary_provider=provider)
        expected = tui.PROFILE_FORM_DUP_FALLBACK.format(
            other=", ".join(name for name in state.fallback_owners[provider]), provider=provider)
        self.assertEqual(state.duplicate_fallback(), expected)
        screen = tui.ProfileEditorScreen(state, palette=M)
        checks = next(row for row in screen.rows(200) if row.kind == "checks")
        self.assertIn(expected, checks.text)
        self.assertEqual(checks.role, "warn")
        # The profile being edited is never its own duplicate.
        own = cli._profile_editor_state(self.runtime, owner, "edit")
        self.assertNotIn(owner, own.fallback_owners.get(provider, ()))


class ModalTabTests(unittest.TestCase):
    def test_tab_reaches_every_button(self) -> None:
        modal = tui.Modal("t", [], buttons=(("Cancel", False), ("Remove", True)))
        self.assertTrue(modal.run(FakeWindow(["\t", ENTER]), M))
        modal = tui.Modal("t", [], buttons=(("A", "a"), ("B", "b"), ("C", "c")))
        self.assertEqual(modal.run(FakeWindow(["\t", "\t", ENTER]), M), "c")
        modal = tui.Modal("t", [], buttons=(("A", "a"), ("B", "b"), ("C", "c")))
        self.assertEqual(modal.run(FakeWindow(["\t", "\t", "\t", ENTER]), M), "a")
        modal = tui.Modal("t", [], buttons=(("A", "a"), ("B", "b"), ("C", "c")))
        self.assertEqual(modal.run(FakeWindow([claude_multi.tui.curses.KEY_BTAB, ENTER]), M), "c")

    def test_with_an_input_tab_still_cycles_through_it(self) -> None:
        modal = tui.Modal("t", [], buttons=(("OK", True), ("Cancel", False)), input=tui.TextInput())
        self.assertFalse(modal.run(FakeWindow(["\t", "\t", ENTER]), M))


if __name__ == "__main__":
    unittest.main()
