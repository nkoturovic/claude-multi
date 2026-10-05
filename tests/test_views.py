"""Pure view builders (``views.py``).

The skeleton rows (``short_label``, ``efforts_text``, ``ScreenFloor``) and the
view rows: ``ages``, ``line_rows``, ``zero_model_providers``, ``used_by``,
``native_summary``, ``review_sentence``, ``effective_context``,
``project_text``, ``propagation_rows`` and ``cli._liveness``, plus
the builders the screens draw from.  Expectations derive from
the frozen fixture catalog (no shipped-id pins, no literal fixture ids).
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import shutil
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT

import _tui_fixture as fx
from _v4 import V4Case
from test_migrate import _retired_entry
from claude_multi import catalog, cli, custom, lineup, profile, scope, sessions, settings, views
import claude_multi.sessions
import subprocess

SPEC = "the views contract"


def _expand(text: str) -> list[str]:
    """Inverse of efforts_text for the oracle: ``a…b`` → the EFFORT_ORDER slice."""

    order = profile.EFFORT_ORDER
    values: list[str] = []
    for token in text.split(" "):
        if "…" in token:
            first, last = token.split("…")
            values.extend(order[order.index(first) : order.index(last) + 1])
        else:
            values.append(token)
    return values


class ShortLabelTests(unittest.TestCase):
    def test_first_segment_of_a_display(self) -> None:
        self.assertEqual(views.short_label("A · B · C"), "A")
        self.assertEqual(views.short_label("Plain"), "Plain")
        self.assertEqual(views.short_label("Dotted·name · tail"), "Dotted·name")

    def test_every_fixture_display(self) -> None:
        lines = catalog.load_catalog(FIXTURE_ROOT).lines
        self.assertTrue(lines, "the fixture catalog has lines")
        for key, entry in lines.items():
            display = entry["display"]
            short = views.short_label(display)
            self.assertTrue(display.startswith(short), key)
            self.assertNotIn(" · ", short, key)
            self.assertEqual(short == display, " · " not in display, key)


class EffortsTextTests(unittest.TestCase):
    def test_synthetic_runs(self) -> None:
        order = profile.EFFORT_ORDER
        cases = {
            order[:4]: f"{order[0]}…{order[3]}",
            order: f"{order[0]}…{order[-1]}",
            order[2:4]: f"{order[2]} {order[3]}",
            (order[2], order[4]): f"{order[2]} {order[4]}",
            (order[0], order[1], order[2], order[4]): f"{order[0]}…{order[2]} {order[4]}",
            (order[1],): order[1],
            (): "",
            (profile.ULTRACODE,): profile.ULTRACODE,
            (*order[:3], profile.ULTRACODE): f"{order[0]}…{order[2]} {profile.ULTRACODE}",
        }
        for efforts, expected in cases.items():
            self.assertEqual(views.efforts_text(efforts), expected, efforts)

    def test_every_fixture_declared_set_round_trips(self) -> None:
        lines = catalog.load_catalog(FIXTURE_ROOT).lines
        for key, entry in lines.items():
            declared = profile.declared_efforts(entry)
            text = views.efforts_text(declared)
            self.assertEqual(_expand(text) if text else [], list(declared), key)
            self.assertLessEqual(len(text), len(" ".join(declared)), key)


class RetirementRowTests(unittest.TestCase):
    def test_a_long_retirement_note_falls_back_to_v_details(self) -> None:
        """A retirement row that does not fit reads
        ``note  retirement change at this resume — V details``."""

        long = views.CardRow("retirement", "note", "x" * 200, priority=0, col=12)
        self.assertEqual(views.card_row_text(long, 80, None).strip(),
                         "note      retirement change at this resume — V details")
        short = views.CardRow("retirement", "note", "lead successor applies at resume", priority=0, col=12)
        self.assertIn("lead successor applies at resume", views.card_row_text(short, 80, None))
        self.assertNotIn("V details", views.card_row_text(short, 80, None))


class ScreenFloorTests(unittest.TestCase):
    def test_compute_caps_the_list_at_three_rows(self) -> None:
        floor = views.ScreenFloor.compute(
            chrome=10, list_rows=0, detail_reserve=0, bar_rows=2, cols=48
        )
        self.assertEqual(floor.as_tuple(), (12, 48))
        capped = views.ScreenFloor.compute(
            chrome=7, list_rows=40, detail_reserve=2, bar_rows=1, cols=44
        )
        self.assertEqual(capped.as_tuple(), (7 + 3 + 2 + 1, 44))

    def test_fits(self) -> None:
        floor = views.ScreenFloor(12, 48)
        self.assertTrue(floor.fits(12, 48))
        self.assertFalse(floor.fits(11, 48))
        self.assertFalse(floor.fits(12, 47))


class ViewsImportTests(unittest.TestCase):
    """Views never imports cli, tui, launch, lineup or curses."""

    ALLOWED = {
        "catalog", "compiler", "profile", "quota", "scope", "sessions", "settings", "strict_json",
    }

    def test_imports_stay_inside_the_allowed_dependency_set(self) -> None:
        tree = ast.parse((REPO_ROOT / "src" / "claude_multi" / "views.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                names = [node.module] if node.module else [a.name for a in node.names]
                for name in names:
                    self.assertIn(name, self.ALLOWED, f"views.py:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                self.assertIn(
                    node.module, ("__future__", "dataclasses", "typing", "datetime", "pathlib")
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        alias.name.split(".")[0],
                        ("curses", "_curses", "termios", "claude_multi"),
                    )



# ------------------------------------------------------------------ fixture helpers


def _lcat() -> profile.LineupCatalog:
    return profile.LineupCatalog.from_catalog(catalog.load_catalog(FIXTURE_ROOT))


def _eff(lcat: profile.LineupCatalog, **changes) -> settings.Effective:
    base = settings.Effective(
        providers_enabled={pid: True for pid in lcat.providers},
        admitted_lines=frozenset(),
        unknown=(),
    )
    return dataclasses.replace(base, **changes)


def _with_lines(lcat: profile.LineupCatalog, **entries) -> profile.LineupCatalog:
    lines = {key: copy.deepcopy(entry) for key, entry in lcat.lines.items()}
    lines.update(entries)
    return dataclasses.replace(lcat, lines=lines)


def _family_lines(lcat: profile.LineupCatalog) -> dict[str, str]:
    """family -> the first agents-capable, all-roles line of that family (derived)."""

    out: dict[str, str] = {}
    for key, entry in lcat.lines.items():
        if "agents" in entry["capabilities"] and entry["roles"] == "all":
            out.setdefault(catalog.line_family(entry, lcat.providers), key)
    return out


class _RuntimeCase(unittest.TestCase):
    """A temp fixture runtime (``_tui_fixture.fixture_runtime``)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-views-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runtime = fx.fixture_runtime(self.tmp)

    def prepared(self, name: str | None = None, document=None):
        name = name or catalog.DEFAULT_SEED
        document = document if document is not None else self.runtime.profiles.load(name)
        target = cli.LaunchTarget("profile", copy.deepcopy(document), name, True, f"Profile {name}")
        return self.runtime.prepare(target, action="fresh", passthrough=[])

    def evaluate(self, document) -> profile.ResolvedLineup:
        ev = profile.evaluate(
            document,
            self.runtime.lineup_catalog(),
            bindings={},
            effective=self.runtime.current_effective(),
            ad_hoc=False,
        )
        self.assertEqual(ev.errors, (), f"{SPEC} §7.3: the test-local profile evaluates")
        return ev.lineup


# ------------------------------------------------------------------ ages


class AgesTests(unittest.TestCase):
    NOW = fx.FIXED_NOW

    def at(self, delta: timedelta) -> str:
        return fx.iso(self.NOW - delta)

    def test_both_forms(self) -> None:
        cases = [
            (timedelta(seconds=30), "now", "now"),
            (timedelta(minutes=2), "2 min ago", "2 min"),
            (timedelta(minutes=59), "59 min ago", "59 min"),
            (timedelta(hours=3), "3 h ago", "3 h"),
            (timedelta(hours=30), "yesterday", "yesterday"),
            (timedelta(days=3), "3 days ago", "3 days"),
            (timedelta(days=13), "13 days ago", "13 days"),
            (timedelta(days=21), "3 weeks ago", "3 wks"),
        ]
        for delta, full, compact in cases:
            iso = self.at(delta)
            self.assertEqual(views.ages(iso, now=self.NOW, compact=False), full, delta)
            self.assertEqual(views.ages(iso, now=self.NOW, compact=True), compact, delta)
            self.assertLessEqual(len(views.ages(iso, now=self.NOW, compact=True)), 10)

    def test_old_dates_none_future_and_garbage(self) -> None:
        old = self.NOW - timedelta(days=100)
        for compact in (False, True):
            self.assertEqual(
                views.ages(fx.iso(old), now=self.NOW, compact=compact), old.strftime("%Y-%m-%d")
            )
            self.assertEqual(views.ages(None, now=self.NOW, compact=compact), "—")
            self.assertEqual(
                views.ages(fx.iso(self.NOW + timedelta(hours=1)), now=self.NOW, compact=compact), "now"
            )
            self.assertEqual(views.ages("not-a-date", now=self.NOW, compact=compact), "not-a-date")


# ------------------------------------------------------------------ line rows


class LineRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lcat = _lcat()
        self.eff = _eff(self.lcat)

    def rows(self, lcat=None, eff=None, custom_ids=frozenset()):
        return {
            row.key: row
            for row in views.line_rows(lcat or self.lcat, eff or self.eff, custom_ids=custom_ids)
        }

    def test_every_line_in_catalog_order_with_line_view_membership(self) -> None:
        rows = views.line_rows(self.lcat, self.eff, custom_ids=frozenset())
        self.assertEqual([row.key for row in rows], list(self.lcat.lines))
        offered = {line.key for line in scope.line_view(self.lcat, self.eff).lines}
        for row in rows:
            entry = self.lcat.lines[row.key]
            self.assertEqual(row.offered, row.key in offered, row.key)
            self.assertEqual(row.short, views.short_label(entry["display"]))
            self.assertEqual(row.efforts, profile.declared_efforts(entry))
            self.assertEqual(row.mode, "client" if isinstance(entry["efforts"], list) else "gateway")
            self.assertEqual(row.class_label, profile.format_tokens(entry["context"]["client_tokens"]))
            self.assertEqual(row.family, catalog.line_family(entry, self.lcat.providers))
            self.assertEqual(row.source, "catalog")
            self.assertEqual(row.provider_display, self.lcat.providers[entry["provider"]]["display"])
        modes = {row.mode for row in rows}
        self.assertEqual(modes, {"client", "gateway"}, f"{SPEC} §7.1: the fixture has both shapes")

    def test_new_line_offered_only_once_admitted(self) -> None:
        key = next(iter(self.lcat.lines))
        entry = {**self.lcat.lines[key], "status": "new"}
        lcat = _with_lines(self.lcat, **{key: entry})
        row = self.rows(lcat)[key]
        self.assertEqual((row.status, row.admitted, row.offered), ("new", False, False))
        admitted = self.rows(lcat, _eff(lcat, admitted_lines=frozenset({key})))[key]
        self.assertEqual((admitted.status, admitted.admitted, admitted.offered), ("new", True, True))

    def test_disabled_provider_is_not_offered(self) -> None:
        key, entry = next(iter(self.lcat.lines.items()))
        enabled = {pid: pid != entry["provider"] for pid in self.lcat.providers}
        row = self.rows(eff=_eff(self.lcat, providers_enabled=enabled))[key]
        self.assertFalse(row.provider_enabled)
        self.assertFalse(row.offered)

    def test_lead_only_and_roles_list(self) -> None:
        rows = self.rows()
        lead_only = [r for r in rows.values() if r.lead_capable and not r.agents_capable]
        listed = [r for r in rows.values() if r.agents_capable and r.roles != "all"]
        self.assertTrue(lead_only, f"{SPEC} §7.1: the fixture has a lead-only line")
        self.assertTrue(listed, f"{SPEC} §7.1: the fixture has a roles-list line")
        for row in lead_only:
            self.assertTrue(row.admits(catalog.LEAD_ROLE))
            self.assertFalse(any(row.admits(rid) for rid in catalog.AGENT_ROLE_IDS))
        for row in listed:
            self.assertIsInstance(row.roles, tuple)
            for rid in catalog.AGENT_ROLE_IDS:
                self.assertEqual(row.admits(rid), rid in row.roles, (row.key, rid))
        agents_only = [r for r in rows.values() if r.agents_capable and not r.lead_capable]
        for row in agents_only:
            self.assertFalse(row.admits(catalog.LEAD_ROLE))

    def test_custom_source_from_a_temp_custom_registry(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="claude-multi-views-custom-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        runtime = fx.fixture_runtime(tmp)
        direct = next(
            pid for pid, p in runtime.catalog.providers.items() if p["transport"]["kind"] == "direct"
        )
        custom.add_model(
            runtime.environ, "views-custom", wire_model="views-custom-wire", provider=direct,
            context_tokens=200_000, display="Views Custom · test", created_via="manual",
            catalog_providers=runtime.catalog.providers,
            catalog_models=runtime.catalog.lines, retired_models=runtime.catalog.retired,
        )
        ids = frozenset(custom.load_registry(runtime.environ)["models"])
        lcat = runtime.lineup_catalog()
        eff = runtime.current_effective()
        rows = self.rows(lcat, eff, ids)
        row = rows["views-custom"]
        self.assertEqual((row.source, row.family, row.short), ("custom", "custom", "Views Custom"))
        self.assertTrue(row.lead_capable and not row.agents_capable)
        self.assertTrue(row.offered)
        seam = {line.key: line.source for line in scope.line_view(lcat, eff).lines}
        for key, value in rows.items():
            if key in seam:
                self.assertEqual(value.source, seam[key], key)
        self.assertEqual({k for k, r in rows.items() if r.source == "custom"}, set(ids))


class ZeroModelProvidersTests(unittest.TestCase):
    def test_providers_without_lines_sorted(self) -> None:
        lcat = _lcat()
        used = {entry["provider"] for entry in lcat.lines.values()}
        expected = tuple(sorted(set(lcat.providers) - used))
        self.assertTrue(expected, f"{SPEC} §7.1: the fixture has a zero-model provider")
        self.assertEqual(views.zero_model_providers(lcat), expected)


# ------------------------------------------------------------------ used_by


class UsedByTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lcat = _lcat()
        self.keys = [k for k, e in self.lcat.lines.items() if "lead" in e["capabilities"]]

    def doc(self, lead, agents=None):
        return {"lead": lead, "agents": agents or {}}

    def test_direct_named_retired_successor_and_null_skipped(self) -> None:
        first, second = self.keys[0], self.keys[1]
        null_key = next(k for k in self.lcat.retired if self.lcat.resolve_key(k).key is None)
        # a test-local retired key whose successor is ``second``
        retired = {**self.lcat.retired, "views-old": {**self.lcat.retired[null_key], "successor": second}}
        lcat = dataclasses.replace(self.lcat, retired=retired)
        self.assertEqual(lcat.resolve_key("views-old").key, second)
        profiles = {
            "p-direct": self.doc({"model": first, "effort": "ultracode"}),
            "p-named": self.doc({"use": "named-one"}),
            "p-retired": self.doc({"model": "views-old", "effort": "ultracode"}),
            "p-null": self.doc({"model": null_key, "effort": "ultracode"}),
            "p-twice": self.doc(
                {"model": first, "effort": "ultracode"},
                {"cm-explorer": {"model": first, "effort": "high"}},
            ),
            "p-unknown": self.doc({"model": "no-such-line", "effort": "high"}, {"cm-analyst": {"use": "missing"}}),
        }
        bindings = {"named-one": {"model": second, "effort": "high"}}
        counts = views.used_by(profiles, bindings, lcat)
        self.assertEqual(set(counts), set(lcat.lines), "every live line key")
        self.assertEqual(counts[first], 2)  # p-direct, p-twice once
        self.assertEqual(counts[second], 2)  # p-named via the binding, p-retired via its successor
        self.assertNotIn(null_key, counts)
        self.assertEqual(sum(counts.values()), 4)


# ------------------------------------------------------------------ native_summary / project_text


class NativeSummaryTests(unittest.TestCase):
    def test_every_value(self) -> None:
        explore = {"replace": "Explore→cm-explorer", "native": "Explore native", "off": "Explore off"}
        for ex, ex_text in explore.items():
            for plan in ("native", "off"):
                for gp in ("on", "off"):
                    text = views.native_summary({"explore": ex, "plan": plan, "general_purpose": gp})
                    expected = [ex_text] + (["Plan off"] if plan == "off" else []) + [f"GP {gp}"]
                    self.assertEqual(text, " · ".join(expected), (ex, plan, gp))

    def test_every_fixture_seed(self) -> None:
        for name, seed in catalog.load_catalog(FIXTURE_ROOT).seed_profiles.items():
            text = views.native_summary(seed["native_agents"])
            self.assertTrue(text.startswith("Explore"), name)
            self.assertTrue(text.endswith(f"GP {seed['native_agents']['general_purpose']}"))


class ProjectTextTests(unittest.TestCase):
    def test_2x_wording(self) -> None:
        self.assertEqual(
            views.project_text(0, []),
            "no project agents discovered (native precedence; none colliding)",
        )
        self.assertEqual(
            views.project_text(1, []), "1 project agent discovered (native precedence; none colliding)"
        )
        self.assertEqual(
            views.project_text(3, ["x"]),
            "3 project agents discovered (native precedence; collision blocks launch)",
        )


# ------------------------------------------------------------------ review sentence


class ReviewSentenceTests(_RuntimeCase):
    def seed(self, name: str) -> profile.ResolvedLineup:
        return self.prepared(name).lineup

    def test_default_seed_is_cross_family_and_derived(self) -> None:
        lineup = self.seed(catalog.DEFAULT_SEED)
        writers = [r for r in lineup.routing.rows if r.author != catalog.LEAD_ROLE]
        reviewers = {r.normal.reviewer for r in writers if r.normal.reviewer}
        families = {r.family for r in writers if r.normal.reviewer}
        self.assertEqual(len(reviewers), 1, f"{SPEC} §4.1.3: one writer reviewer on the default seed")
        self.assertEqual(len(families), 1)
        self.assertFalse(lineup.routing.same_family_authors)
        lead = lineup.routing.rows[-1]
        expected = (
            f"writers({families.pop()})→{profile.label(reviewers.pop())} · "
            f"lead→{profile.label(lead.normal.reviewer)}   all cross-family ✓"
        )
        self.assertEqual(views.review_sentence(lineup), expected)

    def test_single_family_seed(self) -> None:
        single = [
            name for name in catalog.load_catalog(FIXTURE_ROOT).seed_profiles
            if self.prepared(name).lineup.routing.single_family
        ]
        self.assertTrue(single, f"{SPEC} §7.3: a fixture seed routes within one family")
        for name in single:
            text = views.review_sentence(self.seed(name))
            self.assertTrue(text.endswith("   same-family (reduced independence) ≈"), (name, text))

    def test_direct_seed(self) -> None:
        seeds = catalog.load_catalog(FIXTURE_ROOT).seed_profiles
        direct = next(name for name, doc in seeds.items() if doc["agents"] == {})
        self.assertEqual(views.review_sentence(self.seed(direct)), "none (direct session)")

    def _doc(self, agents: dict[str, str]) -> dict:
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = "views-local"
        document["native_agents"] = {**document["native_agents"], "explore": "native"}
        document["agents"] = {
            rid: {"model": key, "effort": self.runtime.lineup_catalog().lines[key]["default_effort"]}
            for rid, key in agents.items()
        }
        return document

    def _two_families(self) -> tuple[str, str]:
        lead_family = self.prepared().lineup.lead.binding.family
        lines = _family_lines(self.runtime.lineup_catalog())
        other = next(key for fam, key in lines.items() if fam != lead_family)
        return lines[lead_family], other

    def test_mixed_writer_reviewers(self) -> None:
        same, other = self._two_families()
        lineup = self.evaluate(self._doc({
            "cm-implementer": same, "cm-implementer-strong": same,
            "cm-reviewer": other, "cm-reviewer-strong": other,
        }))
        text = views.review_sentence(lineup)
        self.assertTrue(
            text.startswith("implementer→reviewer · implementer-strong→reviewer-strong · lead→"), text
        )
        self.assertTrue(text.endswith("   all cross-family ✓"), text)

    def test_same_family_subset(self) -> None:
        same, other = self._two_families()
        lineup = self.evaluate(self._doc({
            "cm-implementer": other, "cm-implementer-strong": same,
            "cm-reviewer": other, "cm-reviewer-strong": other,
        }))
        self.assertTrue(lineup.routing.same_family_authors)
        self.assertFalse(lineup.routing.single_family)
        text = views.review_sentence(lineup)
        expected_tail = "≈ same-family for " + ", ".join(
            profile.label(a) for a in lineup.routing.same_family_authors
        )
        self.assertTrue(text.endswith("   " + expected_tail), text)

    def test_no_reviewer_bound(self) -> None:
        same, _other = self._two_families()
        lineup = self.evaluate(self._doc({"cm-implementer": same}))
        self.assertEqual(views.review_sentence(lineup), "no reviewer bound")


# ------------------------------------------------------------------ effective context


class EffectiveContextTests(_RuntimeCase):
    KEYS = ("window", "trigger", "percent", "scalar")

    def test_equals_the_committed_compaction_for_every_fixture_seed(self) -> None:
        eff = self.runtime.current_effective()
        seeds = catalog.load_catalog(FIXTURE_ROOT).seed_profiles
        self.assertTrue(seeds)
        for name in seeds:
            prepared = self.prepared(name)
            resolved = views.effective_context(self.runtime, prepared.lineup, eff).as_document()
            committed = prepared.record["applied"]["lead"]["compaction"]
            self.assertEqual(
                {k: resolved[k] for k in self.KEYS}, {k: committed[k] for k in self.KEYS}, name
            )

    def test_a_profile_override_changes_the_percent(self) -> None:
        eff = self.runtime.current_effective()
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = "views-override"
        override = settings.COMPACTION_PERCENT_MIN
        document["settings_overrides"] = {settings.COMPACTION_PERCENT_KEY: override}
        prepared = self.prepared("views-override", document)
        resolved = views.effective_context(self.runtime, prepared.lineup, eff)
        self.assertEqual(resolved.percent, override)
        committed = prepared.record["applied"]["lead"]["compaction"]
        self.assertEqual(
            {k: resolved.as_document()[k] for k in self.KEYS}, {k: committed[k] for k in self.KEYS}
        )


# ------------------------------------------------------------------ propagation rows


def _follower(mid: str, *, generation: int = 1, pending: bool = False, cwd: str = "/w/project"):
    return lineup.Follower(
        managed_id=mid, runtime_id=mid, profile="p", live=False, cwd=cwd,
        last_seen_at=fx.iso(fx.FIXED_NOW), generation=generation, has_pending=pending,
    )


class PropagationRowsTests(unittest.TestCase):
    def test_every_state(self) -> None:
        followers = [
            _follower(fx.FIXED_ID),
            _follower(fx.OTHER_ID),
            _follower(fx.THIRD_ID),
            _follower(fx.FOURTH_ID, pending=True),
            _follower(fx.FIFTH_ID, generation=0),
        ]
        states = {
            fx.FIXED_ID: "live", fx.OTHER_ID: "unknown", fx.THIRD_ID: "ended",
            fx.FOURTH_ID: "live", fx.FIFTH_ID: "ended",
        }
        titles = {fx.FIXED_ID: "api design", fx.OTHER_ID: "—"}
        rows = views.propagation_rows(followers, states, titles, width=100)
        self.assertEqual(
            rows,
            (
                '  ● running   project        "api design"   apply now (then /reload-plugins there)',
                "  ◐ unknown   project        —   apply now (then /reload-plugins there)",
                "  ○ ended     project        —   applies at next resume",
                "  ↻ running   project        —   has a pending relaunch change; not applied",
                "  ○ ended     project        —   no lineup-enabled scope yet; applies at next resume",
            ),
        )

    def test_a_t_row_is_never_wider_than_width_minus_3(self) -> None:
        rows = views.propagation_rows(
            [_follower(fx.FIXED_ID)], {fx.FIXED_ID: "live"}, {fx.FIXED_ID: "api design"}
        )
        self.assertEqual(len(rows[0]), 77)
        self.assertTrue(rows[0].endswith("…"))

    def test_narrow_drops_the_title_and_clips(self) -> None:
        rows = views.propagation_rows([_follower(fx.FIXED_ID)], {fx.FIXED_ID: "live"}, {fx.FIXED_ID: "t"}, width=60)
        self.assertNotIn('"t"', rows[0])
        self.assertLessEqual(len(rows[0]), 57)
        self.assertTrue(rows[0].endswith("…"))
        wide = views.propagation_rows(
            [_follower(fx.FIXED_ID)], {fx.FIXED_ID: "live"}, {fx.FIXED_ID: "x" * 90}, width=80
        )
        self.assertLessEqual(len(wide[0]), 77)


# ------------------------------------------------------------------ liveness


class LivenessTests(_RuntimeCase):
    def test_daemon_proc_end_and_none(self) -> None:
        record = fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        ended = fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID, ended=True)
        none = frozenset()
        self.assertEqual(cli._liveness(self.runtime, record, none, none), "unknown")
        self.assertEqual(cli._liveness(self.runtime, ended, none, none), "ended")
        self.assertEqual(
            cli._liveness(self.runtime, record, frozenset({fx.FIXED_ID[:8]}), none), "live"
        )
        self.assertEqual(cli._liveness(self.runtime, ended, none, frozenset({fx.OTHER_ID})), "live")
        # a /proc id of another session is not this one
        self.assertEqual(cli._liveness(self.runtime, ended, none, frozenset({fx.FIXED_ID})), "ended")

    def test_the_runtime_seams(self) -> None:
        with mock.patch.object(claude_multi.sessions, "proc_session_ids", return_value=frozenset({"x"})) as proc:
            plain = cli.Runtime.__new__(cli.Runtime)
            self.assertEqual(cli.Runtime._proc_ids(plain), frozenset({"x"}))
            proc.assert_called_once_with()
        self.runtime.proc_ids = frozenset({fx.FIXED_ID})
        self.runtime.live_prefixes = frozenset({"abc"})
        with mock.patch.object(claude_multi.sessions, "proc_session_ids", side_effect=AssertionError("read /proc")), \
                mock.patch('claude_multi.cli.session_facts._live_background_prefixes', side_effect=AssertionError("read /tmp")):
            self.assertEqual(self.runtime._proc_ids(), frozenset({fx.FIXED_ID}))
            self.assertEqual(self.runtime._live_prefixes(), frozenset({"abc"}))


# ------------------------------------------------------------------ the screen builders


class CardModelTests(_RuntimeCase):
    def card(self, name=None, **facts):
        prepared = self.prepared(name)
        eff = self.runtime.current_effective()
        ctx = {k: prepared.record["applied"]["lead"]["compaction"][k] for k in EffectiveContextTests.KEYS}
        base = dict(
            target=prepared.target, record=prepared.record, lineup=prepared.lineup, context=ctx,
            eff=eff, providers=self.runtime.lineup_catalog().providers,
        )
        base.update(facts)
        return prepared, views.card_model(**base)

    def test_rows_and_keys_of_a_fresh_card(self) -> None:
        prepared, card = self.card()
        text = views.card_text(card, 80)
        lines = text.splitlines()
        self.assertEqual(
            lines[0],
            f"  claude-multi — profile: {catalog.DEFAULT_SEED} · follows profile · Fresh",
        )
        self.assertEqual(lines[1], "  " + "─" * 77)
        lead = prepared.lineup.lead.binding
        self.assertIn(
            f"  lead      {views.short_label(lead.display)} · effort {lead.effort} · "
            f"{profile.format_tokens(lead.client_context_tokens)} class   workflows: "
            f"{prepared.lineup.workflows}",
            lines,
        )
        self.assertIn(
            f"  lineup    {len(prepared.lineup.agents)} agents · durable scope · "
            f"{views.native_summary(prepared.lineup.native_agents)}",
            lines,
        )
        self.assertIn(f"  review    {views.review_sentence(prepared.lineup)}", lines)
        self.assertIn(f"  spread    {prepared.lineup.spread_text()}", lines)
        self.assertEqual(lines[-1], "  Status  Ready")
        self.assertTrue(card.ready)
        self.assertEqual(card.keys[0], ("Enter", "launch"))
        self.assertEqual(card.keys[-2:], (("?", "help"), ("Esc", "")))
        self.assertNotIn(("U", "update"), card.keys)

    def test_update_badge_key_placement_and_resume_keys(self) -> None:
        _prepared, card = self.card(update_hint=("1.0.0", "released 2026-10-15"))
        self.assertEqual(card.keys[:2], (("Enter", "launch"), ("U", "update")))
        self.assertIn(
            "  update    claude-multi 1.0.0 · released 2026-10-15 · press U to update",
            views.card_text(card, 80).splitlines(),
        )
        row = next(row for row in card.rows if row.kind == "update")
        self.assertEqual((row.role, row.priority), ("dim", 6))
        _prepared, old = self.card(update_hint=("1.0.0", "this release is 61 days old — claude-multi update --check"))
        row = next(row for row in old.rows if row.kind == "update")
        self.assertEqual((row.role, row.priority, row.label_role), ("warn", 1, "warn"))
        _prepared, resume = self.card(action="resume")
        self.assertEqual(
            resume.keys, (("Enter", "resume"), ("V", "details"), ("S", "sessions"), ("H", "doctor"), ("?", "help"), ("Esc", "cancel"))
        )

    def test_health_rows(self) -> None:
        _p, ok = self.card(health=(None, True, "9.9.9", "override", 7))
        self.assertIn(
            "  health    gateway ok · Claude 9.9.9 (operator override) · catalog 7",
            views.card_text(ok, 80).splitlines(),
        )
        hint = cli.gateway_service_hint("start")
        _p, down = self.card(health=("boom", True, "9.9.9", "packaged", 7), start_hint=hint)
        self.assertIn(
            "  health    gateway unreachable — launches will fail: boom",
            views.card_text(down, 80).splitlines(),
        )

    def test_meter_note_follows_the_lead_adapter(self) -> None:
        prepared, card = self.card()
        adapter = self.runtime.lineup_catalog().providers[prepared.lineup.lead.binding.provider]["adapter"]
        has_note = any(row.kind == "note" for row in card.rows)
        self.assertEqual(has_note, adapter not in views.COUNT_TOKENS_UPSTREAM_ADAPTERS)
        seeds = catalog.load_catalog(FIXTURE_ROOT).seed_profiles
        other = [
            n for n in seeds
            if self.runtime.lineup_catalog().providers[self.prepared(n).lineup.lead.binding.provider]["adapter"]
            not in views.COUNT_TOKENS_UPSTREAM_ADAPTERS
        ]
        self.assertTrue(other, f"{SPEC}: a fixture seed leads on a non-Anthropic-OAuth provider")
        prepared, card = self.card(other[0])
        notes = [row for row in card.rows if row.kind == "note"]
        self.assertEqual(
            [row.text for row in notes],
            [f"context meter approximate: {prepared.lineup.lead.binding.provider} token counts are "
             "local gateway estimates"],
        )

    def test_blocked_without_a_lineup_draws_no_lineup_rows(self) -> None:
        prepared = self.prepared()
        card = views.card_model(
            target=prepared.target, lineup=None, context=None, errors=("lead.model: gone",),
            health=(None, True, "1", "packaged", 1),
        )
        kinds = [row.kind for row in card.rows]
        self.assertEqual(kinds, ["title", "rule", "health"])
        self.assertFalse(card.ready)
        text = views.card_text(card, 80)
        self.assertTrue(text.endswith("  Status  BLOCKED\n    lead.model: gone\n"), text)

    def test_secret_problem_remedy_and_diamond(self) -> None:
        eff = self.runtime.current_effective()
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = "views-diamond"
        document["settings_overrides"] = {settings.COMPACTION_PERCENT_KEY: settings.COMPACTION_PERCENT_MIN}
        prepared = self.prepared("views-diamond", document)
        ctx = views.effective_context(self.runtime, prepared.lineup, eff).as_document()
        card = views.card_model(
            target=prepared.target, lineup=prepared.lineup, context=ctx, eff=eff,
            secret_problems=("missing secret",),
        )
        context_row = next(row for row in card.rows if row.kind == "context")
        self.assertTrue(context_row.text.endswith(" ◆ override"))
        self.assertEqual(card.remedy, views.SECRET_REMEDY)
        resume = views.card_model(
            target=prepared.target, action="resume", lineup=prepared.lineup, context=ctx, eff=eff,
            secret_problems=("missing secret",),
        )
        # A resume card claude-multi -r opened: Esc ends it, so the way back runs it again.
        self.assertEqual(resume.remedy, "set a key: Esc, run claude-multi, G → K, then S to resume")
        nested = views.card_model(
            target=prepared.target, action="resume", lineup=prepared.lineup, context=ctx, eff=eff,
            secret_problems=("missing secret",), resume_way="Esc, Esc",
        )
        self.assertEqual(nested.remedy, "set a key: Esc, Esc, G → K, then S to resume")
        self.assertFalse(card.ready)

    def test_line_mode_rows_and_resume_notes(self) -> None:
        prepared, card = self.card(
            health=(None, True, "1", "packaged", 1), update_hint=("1", "2"),
            action="resume", diff=("  lead   a → b",), notices=("settings changed",),
        )
        text = views.card_text(card, 80, line_mode=True)
        for word in ("health", "update", "Status", "diff", "note      settings"):
            self.assertNotIn(word, text)
        full = views.card_text(card, 80)
        self.assertIn("  diff        lead   a → b", full)
        self.assertIn("  note      settings changed", full)

    def test_fit_keeps_status_and_collapses_agents(self) -> None:
        prepared, card = self.card(health=(None, True, "1", "packaged", 1))
        agents = len(prepared.lineup.agents)
        self.assertGreater(agents, 2)
        for height in (24, 16, 14, 12):
            fitted = views.card_fit(card, height=height, bar_rows=2)
            self.assertLessEqual(len(fitted), height - 2 - 1 - 3, height)
            kinds = [row.kind for row in fitted]
            for must in ("title", "rule", "lead", "lineup", "header"):
                self.assertIn(must, kinds, height)
            shown = kinds.count("agent")
            overflow = [row for row in fitted if row.kind == "overflow"]
            floor_only = all(row.priority == 0 for row in fitted)
            if shown < agents and not floor_only:
                self.assertEqual(len(overflow), 1, height)
                self.assertEqual(
                    overflow[0].text, f"  … {agents - shown} more agents (E shows all)"
                )
            elif shown == agents:
                self.assertFalse(overflow)
        _prepared, resume_card = self.card(health=(None, True, "1", "packaged", 1), action="resume")
        resume_rows = views.card_fit(resume_card, height=16, bar_rows=2)
        resume_overflow = [row for row in resume_rows if row.kind == "overflow"]
        self.assertEqual(len(resume_overflow), 1)
        self.assertIn("(resize to show all)", resume_overflow[0].text)
        self.assertNotIn("(E shows all)", resume_overflow[0].text)
        tall = views.card_fit(card, height=60, bar_rows=2)
        self.assertEqual([r.kind for r in tall], [r.kind for r in card.rows])


class OnboardingCardRowsTests(_RuntimeCase):
    """The card rows Get started and Profiles rely on: the default mark, the
    description, spend notes, the single not-connected notice, an
    unloadable target and the two fresh keybars."""

    card = CardModelTests.card

    def test_default_mark_description_and_spend_rows(self) -> None:
        _prepared, card = self.card(description="A fixture description.", is_default=True,
                                    spend=("billed per token by a fixture provider",))
        title = next(row for row in card.rows if row.kind == "title")
        self.assertIn(views.CARD_DEFAULT_MARK.strip(), views.card_row_text(title, 80, None))
        kinds = [row.kind for row in card.rows]
        self.assertEqual(kinds[kinds.index("title") + 1], "description")
        description = card.rows[kinds.index("description")]
        self.assertEqual((description.text, description.priority), ("A fixture description.", 9))
        spend = next(row for row in card.rows if row.kind == "spend")
        self.assertEqual((spend.label, spend.text, spend.role), ("spend", "billed per token by a fixture provider",
                                                                 "warn"))
        self.assertIn("A fixture description.", views.card_text(card, 80))

    def test_not_connected_shows_one_notice_and_the_get_started_keys(self) -> None:
        _prepared, card = self.card(not_connected=True, secret_problems=("fixture secret problem",))
        notices = [row for row in card.rows if row.kind == "not-connected"]
        self.assertEqual([row.text for row in notices], [views.CARD_NOT_CONNECTED])
        self.assertEqual(card.keys, views.CARD_KEYS_NOT_CONNECTED)
        _prepared, fresh = self.card()
        self.assertEqual(fresh.keys, views.CARD_KEYS_FRESH)
        self.assertFalse(any(row.kind == "not-connected" for row in fresh.rows))
        _prepared, badge = self.card(not_connected=True, update_hint=("1.0.0", "1.0.1"))
        self.assertEqual(badge.keys[:3], (("Enter", "launch"), ("U", "update"), ("W", "get started")))

    def test_an_unloadable_target_is_blocked_with_its_fix(self) -> None:
        error = SimpleNamespace(name="broken", path="/x/profiles/broken.json", line=3, detail="Expecting ','")
        _prepared, card = self.card(lineup=None, load_error=error, load_path="~/profiles/broken.json")
        self.assertFalse(card.ready)
        self.assertEqual(card.errors[0], views.CARD_UNLOADABLE.format(
            name="broken", path="~/profiles/broken.json", line=3, detail="Expecting ','"))
        self.assertEqual(card.remedy, views.CARD_UNLOADABLE_FIX)
        unknown = SimpleNamespace(name="broken", path="/x/profiles/broken.json", line=None, detail="unreadable")
        _prepared, card = self.card(lineup=None, load_error=unknown)
        self.assertEqual(card.errors[0], views.CARD_UNLOADABLE_NO_LINE.format(
            name="broken", path="/x/profiles/broken.json", detail="unreadable"))

    def test_seed_updates_name_profiles_and_u(self) -> None:
        _prepared, card = self.card(seed_updates=[(catalog.DEFAULT_SEED, 1, 2)])
        self.assertIn(views.CARD_SEED_UPDATES.format(n=1, names=catalog.DEFAULT_SEED).removeprefix("! "),
                      views.card_text(card, 200))


class OnboardingFormsTests(unittest.TestCase):
    def test_the_closed_keyed_kind_is_not_offered(self) -> None:
        self.assertEqual([kind for kind, _label in views.provider_kind_choices()],
                         ["anthropic-compatible", "openai-compatible", "openai-compatible-lan"])
        closed = views.provider_kind_choices(keyed_audited=False)
        self.assertNotIn("openai-compatible", [kind for kind, _label in closed])
        fields = {name: (label, choices) for name, label, _default, choices in
                  views.provider_form_fields(keyed_audited=False)}
        self.assertIn(views.PROVIDER_KIND_CLOSED_LABEL, fields["kind"][0])
        self.assertEqual(fields["kind"][1], closed)
        for _kind, label in views.PROVIDER_KIND_CHOICES:
            self.assertNotRegex(label, r"\b0\d\d\b", "no internal ids in product text")

    def test_manual_provider_auth_choices_match_each_kind(self) -> None:
        for kind, expected in (("anthropic-compatible", {"header", "bearer"}),
                               ("openai-compatible", {"bearer"}), ("openai-compatible-lan", {"none"})):
            with self.subTest(kind=kind):
                fields = {name: (default, choices) for name, _label, default, choices in
                          views.provider_form_fields(kind=kind)}
                default, choices = fields["auth"]
                self.assertEqual({value for value, _label in choices}, expected)
                self.assertIn(default, expected)

    def test_endpoint_and_lan_forms(self) -> None:
        anthropic = {name: (default, choices) for name, _label, default, choices in
                     views.endpoint_form_fields("anthropic-compatible")}
        openai = {name: default for name, _label, default, _choices in views.endpoint_form_fields("openai-compatible")}
        self.assertEqual(list(anthropic), ["id", "url", "auth", "family", "listing"])
        self.assertEqual(anthropic["auth"], ("header", views.ENDPOINT_AUTH_CHOICES))
        self.assertEqual(openai["auth"], "bearer")
        self.assertEqual(anthropic["family"][0], "unknown")
        self.assertEqual([name for name, *_rest in views.lan_form_fields()], ["id", "url"])


class SettingsDefaultRowTests(unittest.TestCase):
    def test_the_default_profile_row_is_opt_in(self) -> None:
        lcat = _lcat()
        eff = _eff(lcat)
        plain = views.settings_rows({}, eff, overrides={}, context=None, token_state=(None, False), lcat=lcat)
        self.assertNotIn(views.SETTINGS_DEFAULT_KEY, [row.key for row in plain])
        for chosen, shown in (("mine", "mine"), (None, views.SETTINGS_DEFAULT_AUTOMATIC)):
            with self.subTest(chosen=chosen):
                rows = views.settings_rows({}, eff, overrides={}, context=None, token_state=(None, False),
                                           lcat=lcat, default_profile=(chosen,))
                keys = [row.key for row in rows]
                row = rows[keys.index(views.SETTINGS_DEFAULT_KEY)]
                self.assertEqual((row.label, row.value, row.when, row.edit, row.editable),
                                 (views.SETTINGS_DEFAULT_LABEL, shown, "next", "default-profile", True))
                self.assertEqual(row.detail, views.SETTINGS_DEFAULT_DETAIL)
                self.assertLessEqual(len(row.text(80)), 77)
        readonly = views.settings_rows(None, eff, overrides={}, context=None, token_state=(None, False),
                                       lcat=lcat, default_profile=("mine",))
        self.assertTrue(next(r for r in readonly if r.key == views.SETTINGS_DEFAULT_KEY).editable)


class ResumeCardRegressionTests(V4Case):
    def card(self, prepared):
        return views.card_model(
            target=prepared.target, action="resume", record=prepared.record,
            lineup=prepared.lineup, notices=prepared.notices, diff=prepared.diff,
        )

    def test_pending_profile_or_direct_header_uses_the_prepared_record(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        old_profile = record["profile"]
        new_profile = next(n for n in self.runtime.profiles.names() if n != old_profile)
        lead = record["applied"]["lead"]["key"]
        cases = (
            ("profile", new_profile, True, None),
            ("lineup", None, False, profile.ad_hoc_direct(lead)),
            ("profile", "missing-profile", True, None),
        )
        for kind, name, follow, document in cases:
            with self.subTest(profile=name):
                self.mutate(mid, pending={
                    "kind": kind, "profile": name, "follow": follow, "document": document,
                    "requested_at": "2026-09-26T12:00:00Z", "reasons": ["test pending change"],
                })
                prepared = self.prepare_resume(mid)
                card = self.card(prepared)
                title = next(row.text for row in card.rows if row.kind == "title")
                expected = old_profile if name == "missing-profile" else name
                self.assertEqual(prepared.record["profile"], expected)
                self.assertIn(f"profile: {expected or '(ad-hoc)'} ·", title)
                self.assertIn("follows profile" if expected else "pinned", title)
                if name == "missing-profile":
                    self.assertTrue(any("pending relaunch change dropped" in line for line in prepared.notices))
                else:
                    self.assertEqual(prepared.lineup.name, expected)
                # The input target deliberately still names the original
                # profile; the body and header must nevertheless agree.
                self.assertEqual(prepared.target.profile, old_profile)

    def test_retired_lead_card_note_distinguishes_rename_from_generation(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        lcat = self.runtime.lineup_catalog()
        key = record["applied"]["lead"]["key"]
        entry = lcat.lines[key]
        applied = copy.deepcopy(record["applied"])
        applied["lead"]["key"] = "former-name"
        self.mutate(mid, applied=applied, follow=False)
        for wire in (entry["wire_model"], "previous-generation-wire"):
            with self.subTest(wire=wire):
                retired = _retired_entry(key, since=33, wire=wire, provider=entry["provider"],
                                         selectors={"former-selector": None})
                local = dataclasses.replace(lcat, retired={**lcat.retired, "former-name": retired})
                with mock.patch.object(type(self.runtime), "lineup_catalog", lambda _self: local):
                    prepared = self.prepare_resume(mid)
                text = views.card_text(self.card(prepared), 180)
                if wire == entry["wire_model"]:
                    expected = f"lead former-name renamed → {key} (same model; applies at this resume)"
                else:
                    expected = (f"lead former-name retired → {key} (applies at this resume; "
                                "thinking continuity is not carried across generations)")
                self.assertIn(expected, prepared.notices)
                self.assertIn(expected.split("; thinking continuity", 1)[0].replace(" (applies at", "; applies at") if "thinking continuity" in expected else expected, text)
            if "thinking continuity" in expected:
                self.assertIn("thinking continuity is not carried across generations", text)

    def test_retired_named_lead_resume_note_has_binding_suffix(self) -> None:
        record = self.launch_fresh()
        mid = record["managed_id"]
        lcat = self.runtime.lineup_catalog()
        key = record["applied"]["lead"]["key"]
        entry = lcat.lines[key]
        retired = _retired_entry(key, since=33, wire=entry["wire_model"],
                                 provider=entry["provider"], selectors={"former-selector": None})
        local = dataclasses.replace(lcat, retired={**lcat.retired, "former-name": retired})
        document = copy.deepcopy(self.runtime.profiles.load(record["profile"]))
        document["lead"] = {"use": "lead-choice"}
        expected = (f"lead former-name renamed → {key} (same model; applies at this resume) "
                    "(named binding 'lead-choice')")
        with mock.patch.object(type(self.runtime), "lineup_catalog", lambda _self: local), \
                mock.patch.object(profile.ProfileStore, "load", return_value=document), \
                mock.patch.object(self.runtime, "_bindings_map", return_value={
                    "lead-choice": {"model": "former-name", "effort": record["applied"]["lead"]["effort"]},
                }):
            prepared = self.prepare_resume(mid)
        self.assertIn(expected, prepared.notices)
        self.assertIn(expected, views.card_text(self.card(prepared), 180))


class RoutingRowsTests(_RuntimeCase):
    def test_cells_and_direct(self) -> None:
        lineup_ = self.prepared().lineup
        rows = views.routing_rows(lineup_)
        self.assertEqual(len(rows), lineup_.routing.authors)
        self.assertTrue(rows[-1][0].startswith("lead ("))
        for (author, normal, high), row in zip(rows, lineup_.routing.rows):
            for text, cell in ((normal, row.normal), (high, row.high)):
                if cell.reviewer is None:
                    self.assertEqual(text, f"— ({cell.reason})")
                else:
                    self.assertTrue(text.startswith(profile.label(cell.reviewer) + " "))
                    self.assertIn("≈" if cell.same_family else "✓", text)
                    self.assertEqual(text.endswith("°"), cell.only_other_family)
        seeds = catalog.load_catalog(FIXTURE_ROOT).seed_profiles
        direct = next(name for name, doc in seeds.items() if doc["agents"] == {})
        self.assertEqual(
            views.routing_rows(self.prepared(direct).lineup), (("no agents: nothing to route", "", ""),)
        )


class PickerRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lcat = _lcat()
        self.eff = _eff(self.lcat)
        self.rows = views.line_rows(self.lcat, self.eff, custom_ids=frozenset())

    def keys(self, model):
        return [item.key for item in model.items if item.kind == "line"]

    def test_slot_admission(self) -> None:
        by_key = {row.key: row for row in self.rows}
        lead = views.picker_rows(self.rows, slot=catalog.LEAD_ROLE, bindings={}, lcat=self.lcat, eff=self.eff, current=None)
        self.assertEqual(set(self.keys(lead)), {k for k, r in by_key.items() if r.lead_capable})
        for rid in catalog.AGENT_ROLE_IDS:
            model = views.picker_rows(self.rows, slot=rid, bindings={}, lcat=self.lcat, eff=self.eff, current=None)
            self.assertEqual(set(self.keys(model)), {k for k, r in by_key.items() if r.admits(rid)}, rid)
        both = views.picker_rows(self.rows, slot="binding", bindings={}, lcat=self.lcat, eff=self.eff, current=None)
        self.assertEqual(
            set(self.keys(both)), {k for k, r in by_key.items() if r.lead_capable or r.agents_capable}
        )
        wf = views.picker_rows(self.rows, slot="workflow", bindings={}, lcat=self.lcat, eff=self.eff, current=None)
        self.assertEqual(wf.items[0].kind, "off")
        for item in wf.items:
            if item.kind == "line":
                row = by_key[item.key]
                self.assertTrue(row.agents_capable)
                self.assertNotIn(profile.ULTRACODE, item.text)
                if row.mode == "client":
                    self.assertEqual(item.efforts, ())
                    self.assertTrue(item.text.endswith(f"efforts {row.default_effort}"))

    def test_lead_ultracode_zero_model_and_disabled(self) -> None:
        provider = self.rows[0].provider
        eff = _eff(self.lcat, providers_enabled={p: p != provider for p in self.lcat.providers})
        rows = views.line_rows(self.lcat, eff, custom_ids=frozenset())
        model = views.picker_rows(rows, slot=catalog.LEAD_ROLE, bindings={}, lcat=self.lcat, eff=eff, current=None)
        lines = [i for i in model.items if i.kind == "line"]
        self.assertTrue(all(i.text.rstrip().split("  (")[0].endswith(profile.ULTRACODE) for i in lines))
        self.assertTrue(all(i.initial_effort == profile.ULTRACODE for i in lines))
        off = [i for i in lines if views.short_label(self.lcat.lines[i.key]["display"]) and self.lcat.lines[i.key]["provider"] == provider]
        self.assertTrue(off)
        for item in off:
            self.assertFalse(item.selectable)
            self.assertIn("(provider off — G)", item.text)
        zero = [i for i in model.items if i.kind == "zero"]
        self.assertEqual(len(zero), 1)
        self.assertEqual(zero[0].text, " · ".join(views.zero_model_providers(self.lcat)) + "     configured · no models")
        self.assertTrue(model.items[model.selected].selectable)

    def test_new_excluded_until_admitted_and_named_rows(self) -> None:
        key = next(k for k, e in self.lcat.lines.items() if "agents" in e["capabilities"] and e["roles"] == "all")
        lcat = _with_lines(self.lcat, **{key: {**self.lcat.lines[key], "status": "new"}})
        rows = views.line_rows(lcat, _eff(lcat), custom_ids=frozenset())
        model = views.picker_rows(rows, slot="cm-explorer", bindings={}, lcat=lcat, eff=_eff(lcat), current=None)
        self.assertNotIn(key, self.keys(model))
        eff = _eff(lcat, admitted_lines=frozenset({key}))
        rows = views.line_rows(lcat, eff, custom_ids=frozenset())
        model = views.picker_rows(
            rows, slot="cm-explorer", bindings={"mine": {"model": key, "effort": "high"}},
            lcat=lcat, eff=eff, current={"use": "mine"},
        )
        self.assertIn(key, self.keys(model))
        named = [i for i in model.items if i.kind == "named"]
        self.assertEqual(len(named), 1)
        self.assertTrue(named[0].text.startswith("named       mine → "))
        self.assertEqual(model.items[model.selected].kind, "named")

    def test_removed_current_is_in_the_title(self) -> None:
        null_key = next(k for k in self.lcat.retired if self.lcat.resolve_key(k).key is None)
        model = views.picker_rows(
            self.rows, slot="cm-reviewer", bindings={}, lcat=self.lcat, eff=self.eff,
            current={"model": null_key, "effort": "high"},
        )
        self.assertEqual(model.title, f"reviewer — choose model   (current: {null_key} was removed)")


class SessionRowsTests(_RuntimeCase):
    def test_columns_tiers_marks_and_banner(self) -> None:
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title="t1")
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID, ended=True, pending=True)
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.THIRD_ID, needs_choice=True)
        lcat = self.runtime.lineup_catalog()
        summaries = [
            sessions.record_summary(self.runtime.session_store.load(mid), lcat)
            for mid in (fx.FIXED_ID, fx.OTHER_ID, fx.THIRD_ID)
        ]
        states = {fx.FIXED_ID: "live", fx.OTHER_ID: "ended"}
        wide = views.session_rows(summaries, lcat=lcat, states=states, now=fx.FIXED_NOW, width=100, cwd=self.runtime.cwd)
        self.assertEqual(wide.title, "sessions — project (cwd) · 3 total")
        self.assertEqual(len(wide.columns), 7)
        cells = [row.cells for row in wide.rows]
        self.assertEqual(cells[0][0], "● running")
        self.assertEqual(cells[1][0], "○ ended")
        self.assertEqual(cells[2][0], "◐ unknown")
        self.assertEqual(cells[0][5], "2 min ago")
        self.assertTrue(cells[1][6].startswith("↻ "))
        key = fx.needs_choice_key(self.runtime)
        self.assertEqual(cells[2][2], f"{key} !")
        self.assertEqual(cells[2][6], "! needs a choice")
        self.assertTrue(wide.rows[2].lead_error)
        self.assertEqual(wide.banner, "! 1 session(s) need a profile choice (lead model removed)")
        mid = views.session_rows(summaries, lcat=lcat, states=states, now=fx.FIXED_NOW, width=80)
        self.assertEqual(mid.title, "sessions — all directories · 3 total")
        self.assertEqual(mid.rows[0].cells[5], "2 min")
        self.assertEqual(
            views.session_rows(summaries, lcat=lcat, states=states, now=fx.FIXED_NOW, width=70).columns,
            ("state", "profile", "lead", "last seen", "title"),
        )
        self.assertEqual(
            views.session_rows(summaries, lcat=lcat, states=states, now=fx.FIXED_NOW, width=50).columns,
            ("state", "profile", "title"),
        )
        forks = views.session_rows(
            summaries, lcat=lcat, states=states, now=fx.FIXED_NOW, width=100, forks=frozenset({fx.FIXED_ID})
        )
        self.assertEqual(forks.rows[0].cells[-1], "⚠ t1")
        self.assertEqual(
            views.session_actions(summaries[1], pending_reasons=[lineup.REASON_FORCED]),
            f"lineup gen 1 · {len(self.runtime.session_store.load(fx.OTHER_ID)['applied']['agents'])} agents · "
            f"follow · pending relaunch: {lineup.REASON_FORCED}",
        )


class LineupDialogModelTests(unittest.TestCase):
    def test_live_relaunch_and_errors(self) -> None:
        live = views.lineup_dialog_model(
            title="t", managed_id=fx.FIXED_ID, state="unknown", profile_label="a", generation=2,
            target="profile", target_profile="b", diff=["  lead   x → y"], mode="LIVE", lead_switch="Y",
        )
        self.assertEqual(live.title, 'change lineup — "t" (unknown)')
        self.assertIn(("next", "run /reload-plugins in that session", "normal"), live.lines)
        self.assertIn(("lead", "switch with /model → Y (press s: this session only)", "normal"), live.lines)
        self.assertTrue(live.can_apply)
        relaunch = views.lineup_dialog_model(
            title=None, managed_id=fx.FIXED_ID, state="ended", profile_label="a", generation=0,
            target="direct", mode="RELAUNCH", reasons=[lineup.REASON_GEN0],
        )
        self.assertEqual(relaunch.title, f"change lineup — {fx.FIXED_ID[:8]} (ended)")
        self.assertIn(("diff", "(no change)", "dim"), relaunch.lines)
        self.assertIn(("why", lineup.REASON_GEN0, "normal"), relaunch.lines)
        errors = views.lineup_dialog_model(
            title=None, managed_id=fx.FIXED_ID, state="live", profile_label="a", generation=1,
            target="per-agent", errors=["bad"],
        )
        self.assertFalse(errors.can_apply)
        self.assertIn(("✗", "bad", "error"), errors.lines)


class ModelsAndDirectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lcat = _lcat()

    def test_models_groups_new_retired_and_detail(self) -> None:
        key = next(iter(self.lcat.lines))
        lcat = _with_lines(self.lcat, **{key: {**self.lcat.lines[key], "status": "new"}})
        rows = views.line_rows(lcat, _eff(lcat), custom_ids=frozenset())
        retired = {k: lcat.resolve_key(k).key for k in lcat.retired}
        model = views.models_model(
            rows, used={}, retired=retired, continuity_count=0, catalog_version=lcat.catalog_version, radar={},
        )
        self.assertEqual(model.title, f"models — catalog {lcat.catalog_version}")
        self.assertNotIn(key, model.keys)
        self.assertEqual(model.new_keys, (key,))
        self.assertEqual(model.new_rows[0][-1], "New · Off")
        self.assertEqual(model.new_heading, "new (off until admitted — Enter admits; stored in Settings):")
        self.assertEqual(
            model.retired, "retired  " + " · ".join(f"{k} → (none)" for k in sorted(retired))
        )
        admitted = views.models_model(
            views.line_rows(lcat, _eff(lcat, admitted_lines=frozenset({key})), custom_ids=frozenset()),
            used={}, retired={}, continuity_count=3, catalog_version=1, radar={key: "radar!"},
        )
        self.assertIn(key, admitted.keys)
        self.assertTrue(admitted.new_heading.endswith(": —"))
        self.assertEqual(admitted.retired, "retired  3 continuity aliases retained")
        self.assertTrue(admitted.details[key][0].endswith(" · admitted (New, stored in Settings)"))
        self.assertEqual(admitted.details[key][1], "radar!")

    def test_custom_rows_listed_once_after_the_catalog_rows(self) -> None:
        provider = next(p for p, v in self.lcat.providers.items() if v["transport"]["kind"] == "direct")
        synthetic = custom.synthetic_entries(
            {"models": {"views-c": {"provider": provider, "wire_model": "w", "context_tokens": 200_000}}},
            cliproxyapi="0",
        )
        lcat = _with_lines(self.lcat, **synthetic)
        rows = views.line_rows(lcat, _eff(lcat), custom_ids=frozenset(synthetic))
        model = views.models_model(rows, used={}, retired={}, continuity_count=0, catalog_version=1, radar={})
        self.assertEqual(model.keys.count("views-c"), 1)
        self.assertEqual(model.keys[-1], "views-c")
        self.assertEqual(model.rows[-1][2], "custom")
        self.assertEqual(model.rows[-1][-1], "direct only")
        direct = views.direct_rows(rows, marks={})
        self.assertEqual([r.key for r in direct].count("views-c"), 1)
        self.assertEqual(direct[-1].key, "views-c")
        self.assertEqual(
            {r.key for r in direct[:-1]},
            {r.key for r in rows if r.lead_capable and r.offered and r.source == "catalog"},
        )
        self.assertTrue(direct[-1].text().endswith("   (custom)"))
        gateway = next(r for r in direct if r.mode == "gateway")
        self.assertIn(f"effort [{gateway.default_effort}]", gateway.text())
        client = next(r for r in direct if r.mode == "client" and r.source == "catalog")
        self.assertNotIn("effort [", client.text())
        marked = views.direct_rows(rows, marks={client.key: "no secret"})
        self.assertTrue(next(r for r in marked if r.key == client.key).text().endswith("  (no secret)"))
        detail = views.direct_detail(client, direct)
        count = sum(1 for r in direct if r.lead_class == client.lead_class)
        self.assertEqual(
            detail, f"lead class {client.lead_class} · {count} model{'s' if count != 1 else ''} in that class"
        )


class ProviderRowsTests(unittest.TestCase):
    def test_credential_cells_and_details(self) -> None:
        lcat = _lcat()
        eff = _eff(lcat, providers_enabled={p: p != "p-off" for p in (*lcat.providers, "p-off")})
        facts = [
            {"id": "p-pool", "kind": "oauth-pool", "pool": "codex", "credential": "2 credential records",
             "served_note": "3/3 served", "models_note": None, "guidance": "sign in: x", "custom": False,
             "selectors": ["m1"]},
            {"id": "p-dead", "kind": "oauth-pool", "pool": "claude", "credential": "1 credential record",
             "served_note": "unknown", "models_note": None, "guidance": "", "custom": False},
            {"id": "p-key", "kind": "direct", "credential": "K_API present", "served_note": "—",
             "models_note": "configured · no models", "guidance": "set K", "custom": True},
            {"id": "p-off", "kind": "direct", "credential": "K missing", "served_note": "0/1 served",
             "models_note": None, "guidance": "", "custom": False},
            {"id": "p-lan", "kind": "direct-openai", "credential": "keyless LAN", "served_note": "1/1 served",
             "models_note": None, "guidance": "", "custom": False},
        ]
        journal = SimpleNamespace(dead_pools={"claude": (1, fx.FIXED_NOW)}, quota={("m1", "429"): 25})
        rows = {row.id: row for row in views.provider_rows(facts, eff=eff, journal=journal)}
        self.assertEqual(rows["p-pool"].cells[2], views.CREDENTIAL_TEXT["signed-in-n"].format(n=2))
        self.assertEqual(rows["p-pool"].cells[5], "3/3")
        self.assertIn("m1: 25× 429 (rate limit / quota) in the last hour", rows["p-pool"].details)
        # Signed in: L's other uses, never advice to sign in again.
        self.assertEqual(rows["p-pool"].details[0], views.SIGNED_IN_GUIDANCE)
        self.assertEqual(rows["p-dead"].cells[2], views.CREDENTIAL_TEXT["failing"])
        self.assertEqual(rows["p-key"].cells[2], "key set")
        self.assertEqual(rows["p-key"].span, views.PROVIDERS_NO_MODELS)
        self.assertIn("custom provider — edit or remove it with claude-multi custom …", rows["p-key"].details)
        self.assertEqual(rows["p-off"].cells[1:3], ("off", "key missing"))
        self.assertTrue(any(d.startswith("off in Settings") for d in rows["p-off"].details))
        self.assertEqual(rows["p-lan"].cells[2], "keyless")
        zero = views.provider_rows(
            [{**facts[0], "credential": "0 credential records"}], eff=eff, journal=None
        )
        self.assertEqual(zero[0].cells[2], "not signed in")
        one = views.provider_rows([{**facts[0], "credential": "1 credential record"}], eff=eff, journal=None)
        self.assertEqual(one[0].cells[2], "signed in")
        keyed = views.provider_rows(
            [{**facts[2], "id": "anthropic", "transport_label": "transport: api-key", "credential": "K missing"},
             {**facts[2], "id": "own", "source": "operator", "route": "unapproved", "models_note": None}],
            eff=eff, journal=None)
        self.assertEqual(keyed[0].cells[2], "API key missing")
        self.assertEqual(keyed[1].cells[7], "not approved")


class SettingsRowsTests(unittest.TestCase):
    def test_rows_values_and_readonly(self) -> None:
        lcat = _lcat()
        eff = _eff(lcat)
        rows = views.settings_rows(
            {}, eff, overrides={"prof": settings.COMPACTION_PERCENT_MIN}, context=None,
            token_state=(None, False), lcat=lcat,
        )
        by = {row.key: row for row in rows}
        self.assertEqual(by["ceiling"].value, profile.format_tokens(views.OPERATING_WINDOW_CEILING))
        self.assertEqual(by[settings.COMPACTION_PERCENT_KEY].value, f"{eff.compaction_percent} % ◆")
        self.assertIn(f"◆ overridden by: prof ({settings.COMPACTION_PERCENT_MIN} %)", by[settings.COMPACTION_PERCENT_KEY].detail)
        self.assertEqual(by["effective"].label, "→ effective: open from the card to see a profile's numbers")
        self.assertEqual(by[settings.WORKFLOW_DEFAULT_BINDING_KEY].value, "off")
        self.assertEqual(by["providers"].value, f"{len(lcat.providers)} of {len(lcat.providers)}")
        self.assertEqual(by["token"].when, "live")
        edited = [row.key for row in rows if row.edit in ("int", "toggle", "picker")]
        self.assertEqual(
            edited,
            [settings.COMPACTION_PERCENT_KEY, settings.EXPLORE_INHERIT_CAP_DISABLED_KEY,
             settings.WORKFLOW_DEFAULT_BINDING_KEY, settings.REVIEW_ROUND_CAP_KEY],
            f"{SPEC}: exactly four edited fields",
        )
        for row in rows:
            text = row.text(80)
            self.assertLessEqual(len(text), 77, row.key)
        # On 2.1.286 the cap applies under a Fable lead only.
        explore = by[settings.EXPLORE_INHERIT_CAP_DISABLED_KEY].detail
        self.assertIn("under a Fable lead only; GPT leads inherit either way", explore)
        self.assertNotIn("under GPT and Fable leads", explore)
        readonly = views.settings_rows(None, eff, overrides={}, context=None, token_state=(0.0, True))
        self.assertFalse(any(r.editable for r in readonly if r.edit in ("int", "toggle", "picker")))
        self.assertEqual({r.key: r for r in readonly}["token"].note, "rotation in progress")
        key = next(k for k, e in lcat.lines.items() if "agents" in e["capabilities"])
        bound = views.settings_rows(
            {}, _eff(lcat, workflow_default_binding={"model": key, "effort": "high"}),
            overrides={}, context=("p", "L", {"window": 800_000, "trigger": 702_000}),
            token_state=(0.0, False), lcat=lcat,
        )
        by = {row.key: row for row in bound}
        self.assertEqual(
            by[settings.WORKFLOW_DEFAULT_BINDING_KEY].value,
            f"{views.short_label(lcat.lines[key]['display'])} · high",
        )
        self.assertEqual(by["effective"].label, "→ effective for p (L): window 800K · compacts at ~702K")
        self.assertEqual(by["token"].note, "last rotated 1970-01-01")


class EditorRowsTests(_RuntimeCase):
    def state(self, document, *, original=None, seed_update=None):
        ev = profile.evaluate(
            document, self.runtime.lineup_catalog(), bindings={},
            effective=self.runtime.current_effective(), ad_hoc=False,
        )
        return SimpleNamespace(
            document=document, original=original if original is not None else copy.deepcopy(document),
            evaluation=ev, cat=self.runtime.lineup_catalog(), bindings={},
            effective=self.runtime.current_effective(), is_seed=True, seed_update=seed_update,
        )

    def test_rows_dirty_unbound_removed_and_checks(self) -> None:
        document = self.runtime.profiles.load(catalog.DEFAULT_SEED)
        rows = views.editor_rows(self.state(document))
        self.assertEqual(rows[0].text, f"edit profile — {catalog.DEFAULT_SEED}  (seed)")
        self.assertEqual(rows[0].suffix, "")
        agent_rows = [r for r in rows if r.kind == "agent"]
        self.assertEqual([r.key for r in agent_rows], list(catalog.AGENT_ROLE_IDS))
        unbound = [r for r in agent_rows if r.key not in document["agents"]]
        for row in unbound:
            self.assertIn("— unbound —", row.text)
        self.assertTrue(next(r for r in rows if r.kind == "checks").text.startswith("Checks    ✓ valid"))
        changed = copy.deepcopy(document)
        null_key = fx.needs_choice_key(self.runtime)
        rid = next(iter(changed["agents"]))
        changed["agents"][rid] = {"model": null_key, "effort": "high"}
        rows = views.editor_rows(self.state(changed, original=document, seed_update=(1, 2)))
        self.assertEqual(rows[0].suffix, "unsaved changes ●")
        removed = next(r for r in rows if r.key == rid)
        self.assertIn(f"{null_key} was removed — unbound", removed.text)
        self.assertEqual(
            rows[-1].text,
            "a newer shipped version exists — P → U shows the changes (your version is kept as a backup)",
        )
        broken = copy.deepcopy(document)
        broken["lead"] = {"model": null_key, "effort": "ultracode"}
        rows = views.editor_rows(self.state(broken))
        checks = next(r for r in rows if r.kind == "checks")
        self.assertTrue(checks.text.startswith("Checks    ✗ "), checks.text)
        self.assertTrue(checks.selectable)
        self.assertEqual(checks.role, "error")


# ------------------------------------------------------------------ the fixture harness


class TuiFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-views-fx-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_hermetic_roots_and_seams(self) -> None:
        runtime = fx.fixture_runtime(self.tmp, live={"ab"}, proc={fx.FIXED_ID})
        self.assertTrue((self.tmp / ".git").is_dir(), f"{SPEC} v2-checks code 18: temp-root .git marker")
        self.assertEqual(Path(runtime.cwd).name, "project")
        self.assertEqual(Path(runtime.cwd).parent, self.tmp.resolve())
        self.assertEqual(runtime._live_prefixes(), frozenset({"ab"}))
        self.assertEqual(runtime._proc_ids(), frozenset({fx.FIXED_ID}))
        self.assertTrue(Path(runtime.environ["HOME"]).is_relative_to(self.tmp))
        self.assertEqual(cli._project_agent_files(runtime.cwd), [])
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("journalctl")):
            self.assertIsNone(cli._read_gateway_journal(runtime))
        with fx.hermetic(runtime):
            self.assertEqual(cli._live_background_prefixes(), frozenset({"ab"}))
        self.assertIsNot(cli._live_background_prefixes, None)
        fx.managed_agents_precondition()

    def test_prepare_perform_never_execs(self) -> None:
        runtime = fx.fixture_runtime(self.tmp)
        prepared = runtime.prepare(
            cli.LaunchTarget("profile", runtime.profiles.load(catalog.DEFAULT_SEED), catalog.DEFAULT_SEED, True, "x"),
            action="fresh", passthrough=[],
        )
        self.assertEqual(prepared.secret_problems, ())
        self.assertEqual(runtime.perform(prepared), 0)
        self.assertEqual(runtime.launches, [prepared])

    def test_v4_record_is_deterministic_and_schema_valid(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="claude-multi-views-fx2-"))
        self.addCleanup(shutil.rmtree, other, True)
        records = []
        for root in (self.tmp, other):
            runtime = fx.fixture_runtime(root)
            record = fx.v4_record(
                runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title="t", pending=True,
                last_seen=timedelta(hours=3),
            )
            self.assertTrue(
                (scope.scope_dir(runtime.session_store.root, fx.FIXED_ID) / scope.LEAD_SET_JSON).is_file()
            )
            records.append({**record, "cwd": "<cwd>"})
        self.assertEqual(fx.canonical(records[0]), fx.canonical(records[1]))
        record = records[0]
        self.assertEqual(record["managed_id"], fx.FIXED_ID)
        self.assertEqual(record["mutation_token"], fx.MUTATION_TOKEN)
        self.assertEqual(record["last_seen_at"], fx.iso(fx.FIXED_NOW - timedelta(hours=3)))
        self.assertEqual(record["pending"]["reasons"], [lineup.REASON_FORCED])
        runtime = fx.fixture_runtime(self.tmp / "third")
        gen0 = fx.v4_record(
            runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID, needs_choice=True,
            generation=0, scope=False, ended=True,
        )
        self.assertEqual(gen0["lineup_generation"], 0)
        self.assertEqual(gen0["applied"]["lead"]["key"], fx.needs_choice_key(runtime))
        self.assertEqual(gen0["last_event_source"], "end")
        self.assertFalse(scope.scope_dir(runtime.session_store.root, fx.OTHER_ID).exists())

    def test_journal_text_is_the_doctor_shape(self) -> None:
        runtime = fx.fixture_runtime(self.tmp)
        model = "views-model-alias"
        text = fx.journal_text(dead_pool="codex", quota=[(model, "402", 2)])
        with mock.patch('claude_multi.cli.gateway_facts._doctor_now', lambda: fx.FIXED_NOW):
            report = cli._doctor_journal_report(runtime, text)
        self.assertEqual(len(report), 2, report)
        self.assertIn("invalid_grant line(s) since the codex pool", report[0])
        self.assertIn(f"model {model} got 2× 402 (payment required)", report[1])





class ProviderQuotaCellTests(unittest.TestCase):
    def rows(self, *, pool=None, enabled=True, journal=None, **kwargs):
        from claude_multi import quota
        facts = [dict(id="a-long-fixture-provider", kind="oauth-pool", pool="claude",
                      credential="2 credential records", enabled=enabled, selectors=["fixture-model"],
                      guidance="ordinary guidance", models=2, served_note="2/2 served"),
                 dict(id="fixture-key", kind="direct", credential="KEY present"),
                 dict(id="fixture-keyless", kind="direct-openai", credential="keyless")]
        return views.provider_rows(facts, eff=_eff(_lcat()), journal=journal,
                                   pool=pool or quota.PoolStatus("ok", credentials=quota.parse_auth_files(fx.quota_body(count=2))),
                                   now=fx.FIXED_NOW, tz=timezone.utc,
                                   login_commands={"claude": "claude-login"}, **kwargs)

    def test_six_cells_and_pool_only_values(self):
        rows = self.rows()
        self.assertEqual(len(views.PROVIDERS_COLUMNS), 8)
        self.assertEqual([len(r.cells) for r in rows], [8, 8, 8])
        self.assertIn("93%!", rows[0].cells[3])
        self.assertEqual(rows[0].cells[4:6], ("2", "2/2"))
        self.assertEqual([r.cells[3] for r in rows[1:]], ["", ""])
        self.assertIn("quota pressure; choose a profile", rows[0].details[0])
        self.assertIn("3d", rows[0].details[1])
        self.assertIn("+1 more (claude-multi quota)", rows[0].details[1])
        self.assertEqual(len(rows[0].details), 2)

    def test_healthy_management_cannot_clear_journal_refresh_failure(self):
        from claude_multi import quota
        journal = SimpleNamespace(dead_pools={"claude": (1, fx.FIXED_NOW)},
                                  quota={("fixture-model", "429"): 2})
        pool = quota.PoolStatus("ok", credentials=quota.parse_auth_files(fx.quota_body(percent=42)))
        row = self.rows(pool=pool, enabled=False, journal=journal)[0]
        self.assertEqual(row.cells[2], views.CREDENTIAL_TEXT["failing"])
        self.assertEqual(row.details[0], views.SIGNIN_REMEDY)
        self.assertIn("off in Settings", row.details[1])
        self.assertIn("journal 2× 429", row.details[1])
        self.assertIn("3d", row.details[1])

    def test_all_unavailable_states_and_management_auth_failure(self):
        from claude_multi import quota
        for state in quota.STATES - {"ok"}:
            with self.subTest(state=state):
                row = self.rows(pool=quota.PoolStatus(state))[0]
                self.assertEqual(row.cells[3], "—")
                if state in {"no-key", "disabled", "unavailable", "seam", "down"}:
                    # A management state never asks a signed-in account to sign in.
                    self.assertEqual(row.details[0], views.SIGNED_IN_GUIDANCE)
                else:
                    self.assertTrue(row.details[0].startswith("claude-multi quota — "))
        pool = quota.PoolStatus("ok", credentials=quota.parse_auth_files(fx.quota_body(reason="token expired")))
        self.assertEqual(self.rows(pool=pool)[0].cells[2], views.CREDENTIAL_TEXT["failing"])

    def test_quota_off_preserves_guidance_settings_and_refresh_priority(self):
        from claude_multi import quota
        for state, note in (("no-key", "quota off — no management key"),
                            ("disabled", "quota off — disabled by operator"),
                            ("unavailable", "quota unavailable in this build")):
            for enabled in (True, False):
                with self.subTest(state=state, enabled=enabled):
                    journal = SimpleNamespace(dead_pools={}, quota={("fixture-model", "429"): 25})
                    row = self.rows(pool=quota.PoolStatus(state), enabled=enabled, journal=journal)[0]
                    self.assertEqual(row.details[0], views.SIGNED_IN_GUIDANCE if enabled else
                                     "off in Settings: new sessions exclude it; running sessions keep their routes")
                    self.assertEqual(row.details[1], "journal 25× 429 · " + note)
                    journal.dead_pools["claude"] = (1, fx.FIXED_NOW)
                    row = self.rows(pool=quota.PoolStatus(state), enabled=enabled, journal=journal)[0]
                    self.assertEqual(row.cells[2], views.CREDENTIAL_TEXT["failing"])
                    self.assertEqual(row.details[0], views.SIGNIN_REMEDY)


class CardQuotaRowTests(unittest.TestCase):
    def test_quota_after_health_is_advisory_and_optional(self):
        target = SimpleNamespace(profile="fixture", follow=True)
        args = dict(target=target, health=(None, True, "fixture", "packaged", 1))
        baseline = views.card_model(**args)
        for role in ("warn", "dim"):
            card = views.card_model(**args, quota_row=("observed fixture quota", role))
            kinds = [r.kind for r in card.rows]
            self.assertEqual(kinds.index("quota"), kinds.index("health") + 1)
            row = next(r for r in card.rows if r.kind == "quota")
            self.assertEqual((row.label, row.role, row.priority), ("quota", role, 1))
            self.assertEqual((card.ready, card.errors), (baseline.ready, baseline.errors))
        self.assertNotIn("quota", [r.kind for r in baseline.rows])


if __name__ == "__main__":
    unittest.main()
