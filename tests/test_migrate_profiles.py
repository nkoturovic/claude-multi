"""``claude-multi profile migrate [--dry-run | --apply]``.

Fixture-based: the frozen asset root (``FIXTURE_ROOT``), synthetic v1
compositions over fixture keys (``opus55``, ``opus``, ``sol``, ``gpt55``,
``qwen38``, ``kimi-k3``, ``fable``, ``qwen-flash-next``, retired
``muse-spark``) and test-local tables. The shipped MIGRATION.md table names
3.0 keys the fixture does not carry, so CLI-level classes patch
``migration_map.PRESET_TARGETS``/``CONVERT_RULES`` (and
``migrate_profiles.V1_DEFAULT_LANES`` where a case needs it): the module reads
them at call time (cf-7). Only ``ShippedTableTests`` reads the shipped catalog.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import dataclasses
import inspect
import io
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from claude_multi import (
    catalog,
    cli,
    migrate,
    migrate_profiles as mp,
    migration_map as mm,
    profile,
    scope,
    sessions,
    state,
    strict_json,
)
from _catalog import FIXTURE_ROOT, GOLDENS_ROOT, SHIPPED_ROOT, uses_shipped_catalog
from _layout import REPO_ROOT
from _golden import assertGolden
from test_migrate import NOW, _cli_environ, _local_cat, _retired_entry, v3_managed
import claude_multi.sessions

BUNDLE = catalog.load_catalog(FIXTURE_ROOT)
LCAT = profile.LineupCatalog.from_docs(BUNDLE.docs)
COMP_SCHEMA = strict_json.load(FIXTURE_ROOT / "schemas" / "composition.schema.json")
VERSION_DOC = BUNDLE.docs["version"]
CATALOG32 = REPO_ROOT / "tests" / "fixtures" / "catalog32-models.json"
GOLDENS = GOLDENS_ROOT / "v4" / "profile-migrate"
SOURCE = REPO_ROOT / "src" / "claude_multi" / "migrate_profiles.py"
CONFIG_PLACEHOLDER = "/config"


# ------------------------------------------------------------------ builders


def slot(role: str, model: str, lane: str | None = None, preferred: bool | None = None) -> dict:
    entry = {"role": role, "model": model}
    if lane is not None:
        entry["lane"] = lane
    if preferred is not None:
        entry["preferred"] = preferred
    return entry


def comp(
    name: str,
    lead: str,
    *agents: dict,
    lead_lane: str | None = None,
    explore: str = "native",
    plan_: str = "native",
    general_purpose: str = "on",
    providers: dict | None = None,
    models: dict | None = None,
    description: str | None = None,
    workflows: str | None = None,
    seed: dict | None = None,
) -> dict:
    """A v1 composition (valid against the fixture schema mirror)."""

    document = {
        "version": 1,
        "name": name,
        "availability": {
            "providers": {"anthropic": "lead+agents"} if providers is None else providers,
            "models": {} if models is None else models,
        },
        "slots": [slot("cm-lead", lead, lead_lane), *agents],
        "native_agents": {"explore": explore, "plan": plan_, "general_purpose": general_purpose},
    }
    if description is not None:
        document["description"] = description
    if workflows is not None:
        document["workflows"] = workflows
    if seed is not None:
        document["seed"] = seed
    return document


def rule(
    source: str,
    members: tuple[str, ...] | None = None,
    kind: str = mm.RULE_MECHANICAL,
    *,
    lead: tuple[str, str] | None = None,
    overrides: dict | None = None,
    native: dict | None = None,
    lead_providers: str = mm.LEAD_PROVIDERS_AVAILABILITY,
    expected: tuple[str, ...] = (),
    primary: str | None = None,
) -> mm.ConvertRule:
    return mm.ConvertRule(
        source,
        tuple(sorted(members if members is not None else (source,))),
        kind,
        lead,
        MappingProxyType(dict(overrides or {})),
        MappingProxyType(dict(native or {})),
        lead_providers,
        expected,
        primary_provider=primary,
    )


def presets(*documents: dict) -> dict[str, mp.Preset]:
    return {doc["name"]: mp.Preset(doc["name"], doc, None) for doc in documents}


def convert(target: str, the_rule: mm.ConvertRule, *documents: dict, lcat=LCAT, lanes=None):
    return mp.convert_target(target, the_rule, presets(*documents), lcat=lcat, default_lanes=lanes)


# The synthetic golden directory: one preset per outcome, one merged group
# (fx-convert + fx-merge -> fx-team), a lead-provider row, and one failed
# source (fx-lost-member present, its named source fx-lost-src absent).
def golden_documents() -> dict[str, dict]:
    return {
        "default": comp("default", "opus55", slot("cm-analyst", "sol")),
        "fx-convert": comp(
            "fx-convert", "opus55",
            slot("cm-analyst", "qwen38"),
            slot("cm-analyst", "sol", "xhigh", preferred=True),
            slot("cm-implementer", "sol", "xhigh"),
            slot("cm-reviewer", "gpt55", "high"),
            slot("cm-reviewer", "kimi-k3"),
            slot("cm-reviewer", "muse-spark", "xhigh"),
            explore="replace", general_purpose="off",
            providers={"anthropic": "lead+agents", "openai": "agents", "qwen": "agents",
                       "kimi": "agents"},
            models={"sol": "agents", "gpt55": "agents"},
            description="Fixture team",
        ),
        "fx-merge": comp(
            "fx-merge", "sol",
            slot("cm-analyst", "sol"),
            slot("cm-reviewer", "gpt55", "high"),
            providers={"openai": "lead+agents"},
        ),
        "fx-direct": comp("fx-direct", "qwen38", providers={"qwen": "lead"}),
        "fx-drop": comp("fx-drop", "muse-spark", slot("cm-analyst", "sol")),
        "fx-judgment": comp("fx-judgment", "kimi-k3", slot("cm-analyst", "sol")),
        "fx-seed": comp("fx-seed", "sol", slot("cm-analyst", "sol")),
        "fx-own": comp("fx-own", "fable"),
        "fx-lost-member": comp("fx-lost-member", "sol"),
    }


# Present for the dry run and the refused run only (removed before the
# apply runs): a composition without a reviewed row, and a member of a
# target whose source is gone.
BLOCKING_DOCUMENTS = ("fx-own", "fx-lost-member")


def fixture_tables() -> tuple[dict, dict]:
    """The test-local PRESET_TARGETS / CONVERT_RULES over fixture keys."""

    targets = {
        "fx-seed": (mm.SEED, "balanced"),
        "fx-convert": (mm.CONVERT, "fx-team"),
        "fx-merge": (mm.CONVERT, "fx-team"),
        "fx-direct": (mm.CONVERT, "fx-direct"),
        "fx-drop": (mm.DROPPED, None),
        "fx-judgment": (mm.DROPPED, None),
        "fx-lost-member": (mm.CONVERT, "fx-lost"),
        "fx-lost-src": (mm.CONVERT, "fx-lost"),
    }
    rules = {
        "fx-team": rule(
            "fx-convert", ("fx-convert", "fx-merge"), mm.RULE_OVERRIDES,
            overrides={"cm-implementer-strong": ("opus55", "xhigh")},
            expected=("fan-out-quota",),
        ),
        "fx-direct": rule(
            "fx-direct", lead_providers=mm.LEAD_PROVIDERS_LEAD_PROVIDER,
        ),
        "fx-lost": rule("fx-lost-src", ("fx-lost-member", "fx-lost-src")),
    }
    return targets, rules


def write_composition(directory: Path, document: dict | None, name: str | None = None,
                      raw: bytes | None = None) -> Path:
    state.ensure_private_dir(directory)
    path = directory / f"{name or document['name']}.json"
    path.write_bytes(raw if raw is not None else strict_json.pretty_file_bytes(document))
    return path


def tree_snapshot(root: Path) -> dict[str, tuple]:
    """Paths, modes, bytes (or link targets) of every entry under ``root``."""

    found = {}
    for path in sorted(root.rglob("*")):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            content = ("link", os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            content = ("file", path.read_bytes())
        else:
            content = ("dir", None)
        found[str(path.relative_to(root))] = (stat.S_IMODE(info.st_mode), content)
    return found


class HomeCase(unittest.TestCase):
    """A temp HOME with XDG config/state under it; tables patched through the module."""

    maxDiff = None

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-profile-migrate-"))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.environ = _cli_environ(self.tmp)
        self.config_root = sessions.config_root(self.environ)
        self.compositions = self.config_root / "compositions"
        self.state_root = sessions.state_root(self.environ)
        self.targets, self.rules = fixture_tables()

    def store(self) -> profile.ProfileStore:
        return profile.ProfileStore.for_catalog(BUNDLE, self.environ)

    def write(self, *documents: dict) -> None:
        for document in documents:
            write_composition(self.compositions, document)

    def write_golden_set(self, *, blocking: bool = True) -> None:
        for name, document in golden_documents().items():
            if blocking or name not in BLOCKING_DOCUMENTS:
                self.write(document)

    def plan(self, *, dry_run: bool = True, lcat=LCAT, **kw) -> mp.ProfileMigrationReport:
        return mp.plan(
            compositions_dir=self.compositions,
            schema=COMP_SCHEMA,
            store=self.store(),
            lcat=lcat,
            version_doc=VERSION_DOC,
            state_root=self.state_root,
            dry_run=dry_run,
            preset_targets=kw.get("preset_targets", self.targets),
            convert_rules=kw.get("convert_rules", self.rules),
            default_lanes=kw.get("default_lanes"),
        )

    def main(self, *argv: str, captured: list | None = None) -> tuple[int, str]:
        """``cli.main`` over this home with the tables patched on migration_map."""

        environ, tmp = self.environ, self.tmp
        real_runtime = cli.Runtime

        def runtime_factory(**kwargs):
            if captured is not None:
                captured.append(kwargs)
            return real_runtime(asset_root=FIXTURE_ROOT, environ=environ, cwd=tmp,
                                allow_state_writes=kwargs["allow_state_writes"],
                                refresh_shims=kwargs["refresh_shims"])

        out = io.StringIO()
        with mock.patch.dict(os.environ, environ), \
                mock.patch('claude_multi.cli.runtime.Runtime', side_effect=runtime_factory), \
                mock.patch.object(claude_multi.sessions, "state_root", return_value=self.state_root), \
                mock.patch.object(mm, "PRESET_TARGETS", MappingProxyType(self.targets)), \
                mock.patch.object(mm, "CONVERT_RULES", MappingProxyType(self.rules)), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = cli.main(["profile", "migrate", *argv], output_stream=out, interactive=False)
        # The report, then a refusal (stderr).
        return code, out.getvalue() + errors.getvalue()

    def target(self, report: mp.ProfileMigrationReport, name: str) -> mp.TargetOutcome:
        return next(item for item in report.targets if item.target == name)

    def preset(self, report: mp.ProfileMigrationReport, name: str) -> mp.PresetOutcome:
        return next(item for item in report.presets if item.name == name)


# --------------------------------------------------------------- goldens


def profile_migrate_golden_files() -> dict[str, bytes]:
    """dry-run / refused over the golden directory; apply / rerun without its blocking files."""

    tmp = Path(tempfile.mkdtemp(prefix="claude-multi-profile-migrate-golden-"))
    try:
        os.chmod(tmp, 0o700)
        environ = _cli_environ(tmp)
        config_root = sessions.config_root(environ)
        compositions = config_root / "compositions"
        targets, rules = fixture_tables()
        documents = golden_documents()
        for document in documents.values():
            write_composition(compositions, document)

        def run(*, dry_run: bool) -> mp.ProfileMigrationReport:
            store = profile.ProfileStore.for_catalog(BUNDLE, environ)
            report = mp.plan(
                compositions_dir=compositions, schema=COMP_SCHEMA, store=store, lcat=LCAT,
                version_doc=VERSION_DOC, state_root=sessions.state_root(environ),
                dry_run=dry_run, preset_targets=targets, convert_rules=rules,
            )
            if not dry_run:
                report = mp.apply_plan(report, store)
            return dataclasses.replace(report, config_root=CONFIG_PLACEHOLDER)

        files = {"dry-run.txt": run(dry_run=True), "refused.txt": run(dry_run=False)}
        for name in BLOCKING_DOCUMENTS:
            (compositions / f"{name}.json").unlink()
        files["apply.txt"] = run(dry_run=False)
        files["rerun.txt"] = run(dry_run=False)
        return {name: mp.render_text(report).encode("utf-8") for name, report in files.items()}
    finally:
        shutil.rmtree(tmp, True)


class ReportGoldenTests(HomeCase):
    def test_render_text_goldens(self) -> None:
        files = profile_migrate_golden_files()
        self.assertEqual(sorted(files), ["apply.txt", "dry-run.txt", "refused.txt", "rerun.txt"])
        for name, data in files.items():
            with self.subTest(name):
                assertGolden(self, GOLDENS / name, data)

    def test_cli_stdout_equals_the_dry_run_golden(self) -> None:
        self.write_golden_set()
        code, out = self.main()
        self.assertEqual(code, 1)  # a failed source
        text = out.replace(str(self.config_root), CONFIG_PLACEHOLDER)
        assertGolden(self, GOLDENS / "dry-run.txt", text.encode("utf-8"))

    def test_target_header_lines_are_unpadded(self) -> None:
        self.write_golden_set()
        text = mp.render_text(self.plan())
        body = text.split("\ntargets:\n", 1)[1].split("\n\nsummary:\n", 1)[0]
        headers = [line for line in body.splitlines() if not line.startswith("    ")]
        self.assertEqual(headers, [
            "  fx-direct  write  (source fx-direct)",
            "  fx-lost  failed  (source fx-lost-src)",
            "  fx-own  write  (source fx-own)",
            "  fx-team  write  (source fx-convert; members fx-convert, fx-merge)",
        ])

    def test_preset_rows_are_padded_columns(self) -> None:
        self.write_golden_set()
        text = mp.render_text(self.plan())
        rows = text.split("\npresets:\n", 1)[1].split("\n\n", 1)[0].splitlines()
        width = max(len(name) for name in golden_documents())
        for row in rows:
            name, kind = row[2:2 + width].rstrip(), row[4 + width:4 + width + 10].rstrip()
            self.assertIn(kind, mp.OUTCOME_KINDS, row)
            self.assertIn(name, golden_documents(), row)
            self.assertEqual(row[4 + width + 10:4 + width + 12], "  ", row)


# ------------------------------------------------------------ reading presets


class ReadPresetsTests(HomeCase):
    def mixed_directory(self) -> None:
        good = comp("alpha", "sol")
        self.write(comp("beta", "opus55"), good)
        state.ensure_private_dir(self.compositions / "dir.json")
        os.symlink(self.compositions / "alpha.json", self.compositions / "link.json")
        write_composition(self.compositions, None, "notes", raw=b"x")
        (self.compositions / "notes.txt").write_text("not a preset")
        write_composition(self.compositions, good, "Bad-Stem")
        write_composition(self.compositions, None, "invalid", raw=b"{nope")
        write_composition(self.compositions, None, "dupe",
                          raw=b'{"version": 1, "version": 1}')
        schema_bad = comp("schema-bad", "sol")
        schema_bad["extra"] = True
        write_composition(self.compositions, schema_bad)
        v2 = comp("v2", "sol")
        v2["version"] = 2
        write_composition(self.compositions, v2)
        write_composition(self.compositions, comp("other", "sol"), "renamed")

    def test_every_entry_is_reported_sorted(self) -> None:
        self.mixed_directory()
        found = mp.read_presets(self.compositions, COMP_SCHEMA)
        self.assertEqual([p.name for p in found], sorted(p.name for p in found))
        by_name = {p.name: p for p in found}
        self.assertEqual({n for n, p in by_name.items() if p.error is None}, {"alpha", "beta"})
        self.assertEqual(by_name["Bad-Stem"].error, "unsafe file name")
        self.assertEqual(by_name["dir"].error, "not a regular file")
        self.assertEqual(by_name["link"].error, "not a regular file")
        self.assertNotIn("notes.txt", by_name)
        self.assertIn("duplicate", by_name["dupe"].error.lower())
        self.assertIn("extra", by_name["schema-bad"].error)
        self.assertIn("version", by_name["v2"].error)
        self.assertEqual(by_name["renamed"].error, "name 'other' differs from the file stem")
        for name in ("invalid", "notes"):
            self.assertIsNotNone(by_name[name].error, name)
        report = self.plan(preset_targets={}, convert_rules={})
        self.assertEqual(
            self.preset(report, "renamed").reason,
            "→ renamed (unreadable: name 'other' differs from the file stem; outcome by its name)",
        )
        text = mp.render_text(report)
        self.assertIn(f"compositions: {len(found)} files in {self.compositions} "
                      f"({len(found) - 2} unreadable)\n", text)

    def test_absent_directory_is_none_and_reported(self) -> None:
        self.assertIsNone(mp.read_presets(self.compositions, COMP_SCHEMA))
        code, out = self.main()
        self.assertEqual(code, 0)
        self.assertIn(f"compositions: 0 files in {self.compositions} (0 unreadable) "
                      "(directory absent)\n", out)
        self.assertFalse(self.compositions.exists())

    def test_unsafe_directory_is_pm4(self) -> None:
        cases = {}
        real = self.tmp / "real"
        state.ensure_private_dir(real)
        state.ensure_private_dir(self.config_root)
        cases["symlink"] = lambda: os.symlink(real, self.compositions)
        cases["group"] = lambda: (state.ensure_private_dir(self.compositions),
                                  os.chmod(self.compositions, 0o750))
        cases["file"] = lambda: (state.ensure_private_dir(self.config_root),
                                 self.compositions.write_text("x"))
        for label, make in cases.items():
            with self.subTest(label):
                make()
                with self.assertRaises(mp.ProfileMigrationError) as ctx:
                    mp.read_presets(self.compositions, COMP_SCHEMA)
                self.assertTrue(str(ctx.exception).startswith("cannot read compositions: "))
                code, out = self.main()
                self.assertEqual(code, 1)
                self.assertTrue(out.startswith("claude-multi: cannot read compositions: "), out)
                if self.compositions.is_symlink() or self.compositions.is_file():
                    self.compositions.unlink()
                else:
                    shutil.rmtree(self.compositions)

    def test_parity_with_the_composition_store(self) -> None:
        self.mixed_directory()
        found = mp.read_presets(self.compositions, COMP_SCHEMA)
        readable = {p.name for p in found if p.error is None}
        content = {"invalid", "notes", "dupe", "schema-bad", "v2", "renamed"}
        self.assertTrue(content <= {p.name for p in found if p.error is not None})
        store = cli.CompositionStore(self.config_root, schema=COMP_SCHEMA)
        self.assertEqual(set(store.names()), readable | content)
        for name in ("Bad-Stem", "dir", "link"):
            self.assertNotIn(name, store.names())

    def test_parity_over_an_all_valid_directory(self) -> None:
        self.write(comp("alpha", "sol"), comp("beta", "opus55"), comp("gamma", "fable"))
        found = mp.read_presets(self.compositions, COMP_SCHEMA)
        store = cli.CompositionStore(self.config_root, schema=COMP_SCHEMA)
        self.assertEqual([p.name for p in found if p.error is None], store.names())


# ------------------------------------------------------------- outcomes


class SeedNameTests(HomeCase):
    """A composition's name is never evidence of the shipped
    profile's lineup."""

    def test_a_composition_named_like_a_seed_converts_under_its_own_name(self) -> None:
        self.write(comp("balanced", "sol", slot("cm-analyst", "qwen38"), description="mine"),
                   comp("quality", "fable"), comp("quality-legacy", "sol"))
        report = self.plan()
        outcome = self.preset(report, "balanced")
        self.assertEqual((outcome.kind, outcome.target), ("convert", "balanced-legacy"))
        self.assertIn("named like the shipped profile balanced", outcome.reason)
        # A taken preserved name moves on deterministically.
        self.assertEqual(self.preset(report, "quality").target, "quality-legacy-2")
        target = self.target(report, "balanced-legacy")
        self.assertEqual((target.status, target.source), ("write", "balanced"))
        self.assertEqual(target.document["lead"]["model"], "sol")
        self.assertIn("cm-analyst", target.document["agents"])
        self.assertEqual(report.blocking, ())
        store = self.store()
        shipped = store.seed_document("balanced")
        applied = mp.apply_plan(dataclasses.replace(report, dry_run=False), store)
        self.assertEqual(self.target(applied, "balanced-legacy").status, "written")
        self.assertEqual(store.load("balanced-legacy")["lead"]["model"], "sol")
        # The shipped profile keeps its own lineup (installed as shipped).
        self.assertEqual(store.load("balanced"), shipped)

    def test_a_seed_is_reused_only_on_an_established_equivalence(self) -> None:
        self.write(comp("balanced", "sol", slot("cm-analyst", "qwen38")))
        with mock.patch.object(mp, "_same_lineup", return_value=True):
            report = self.plan()
        outcome = self.preset(report, "balanced")
        self.assertEqual((outcome.kind, outcome.target), ("seed-map", "balanced"))
        self.assertNotIn("balanced-legacy", [item.target for item in report.targets])
        # The equivalence itself: the shipped lineup under another name is
        # the same lineup; one changed binding is not.
        store = self.store()
        same = store.seed_document("balanced")
        same.pop("seed", None)
        same.update(name="balanced-legacy", description="converted")
        self.assertTrue(mp._same_lineup(same, store, "balanced"))
        other = copy.deepcopy(same)
        other["lead"] = {**other["lead"], "model": "sol" if other["lead"]["model"] != "sol" else "fable"}
        self.assertFalse(mp._same_lineup(other, store, "balanced"))

    def test_an_equivalent_seed_is_not_reused_over_a_different_user_profile(self) -> None:
        # The legacy direct converts exactly to the shipped seed direct.
        self.write(comp("direct", "opus55", workflows="native"))
        outcome = self.preset(self.plan(), "direct")
        self.assertEqual((outcome.kind, outcome.target), ("seed-map", "direct"))
        # The shipped profile installed as its user copy is still the same lineup.
        store = self.store()
        store.install_seeds()
        outcome = self.preset(self.plan(), "direct")
        self.assertEqual((outcome.kind, outcome.target), ("seed-map", "direct"))
        # But the profile direct selects here is the user's own lineup.
        override = store.seed_document("direct")
        override.pop("seed", None)
        override.update(lead={"model": "sol", "effort": "high"}, description="my own direct")
        path = store.save(override)
        before = (path.read_bytes(), os.stat(path).st_mtime_ns)
        report = self.plan()
        outcome = self.preset(report, "direct")
        self.assertEqual((outcome.kind, outcome.target), ("convert", "direct-legacy"))
        target = self.target(report, "direct-legacy")
        self.assertEqual((target.status, target.source), ("write", "direct"))
        self.assertEqual(target.document["lead"]["model"], "opus55")
        self.assertEqual(report.blocking, ())
        applied = mp.apply_plan(dataclasses.replace(report, dry_run=False), self.store())
        self.assertEqual(self.target(applied, "direct-legacy").status, "written")
        self.assertEqual(self.store().load("direct-legacy")["lead"]["model"], "opus55")
        # The override is never altered.
        self.assertEqual((path.read_bytes(), os.stat(path).st_mtime_ns), before)
        self.assertEqual(self.store().load("direct")["lead"], {"model": "sol", "effort": "high"})

    def test_an_unreadable_user_profile_under_the_seed_name_keeps_the_conversion(self) -> None:
        self.write(comp("direct", "opus55", workflows="native"))
        path = self.config_root / "profiles" / "direct.json"
        state.ensure_private_dir(path.parent)
        path.write_bytes(b"{ not json")
        os.chmod(path, 0o600)
        report = self.plan()
        outcome = self.preset(report, "direct")
        self.assertEqual((outcome.kind, outcome.target), ("convert", "direct-legacy"))
        self.assertEqual(self.target(report, "direct-legacy").status, "write")
        mp.apply_plan(dataclasses.replace(report, dry_run=False), self.store())
        self.assertEqual(path.read_bytes(), b"{ not json")


class OutcomeTypeTests(HomeCase):
    def test_one_preset_per_outcome(self) -> None:
        self.write_golden_set()
        report = self.plan()
        kinds = {item.name: item.kind for item in report.presets}
        self.assertEqual(kinds, {
            "default": "drop", "fx-convert": "convert", "fx-direct": "convert",
            "fx-drop": "drop", "fx-judgment": "drop", "fx-lost-member": "merge-into",
            "fx-merge": "merge-into", "fx-seed": "seed-map", "fx-own": "convert",
        })
        self.assertEqual([i.name for i in report.presets if i.blocking], [])
        self.assertEqual(self.preset(report, "fx-seed").reason, "→ balanced (lead sol → opus55)")
        self.assertEqual(self.preset(report, "fx-merge").reason, "→ fx-team (source fx-convert)")
        # No reviewed row: a profile of its own name, by the mechanical rule.
        self.assertEqual(self.preset(report, "fx-own").reason, "→ fx-own")
        self.assertEqual(self.target(report, "fx-own").rule, mm.generic_rule("fx-own"))
        self.assertEqual(report.blocking, ("target fx-lost: failed",))

    def test_a_row_without_its_rule_is_unmapped(self) -> None:
        self.write_golden_set()
        targets, rules = fixture_tables()
        targets["fx-own"] = (mm.CONVERT, "nowhere")
        report = self.plan(preset_targets=targets, convert_rules=rules)
        self.assertEqual(self.preset(report, "fx-own").kind, "unmapped")
        self.assertEqual(self.preset(report, "fx-own").reason,
                         "convert target nowhere has no convert rule; --apply refuses")
        self.assertIn("preset fx-own: unmapped", report.blocking)

    def test_drop_reasons(self) -> None:
        self.write_golden_set()
        report = self.plan()
        self.assertEqual(self.preset(report, "fx-drop").reason,
                         "lead muse-spark removed (no successor)")
        self.assertEqual(
            self.preset(report, "fx-judgment").reason,
            "judgment row (lead kimi-k3 → kimi-k3 survives); records → profile null",
        )
        self.assertEqual(
            self.preset(report, "default").reason,
            f"legacy default composition; the default is the {catalog.DEFAULT_SEED} seed",
        )

    def test_seed_map_with_a_removed_lead(self) -> None:
        self.write(comp("fx-seed", "muse-spark"))
        report = self.plan()
        self.assertEqual(self.preset(report, "fx-seed").reason,
                         "→ balanced (lead muse-spark removed)")

    def test_unreadable_seed_and_drop_rows_keep_their_outcome(self) -> None:
        write_composition(self.compositions, None, "fx-seed", raw=b"{")
        write_composition(self.compositions, None, "fx-drop", raw=b"{")
        report = self.plan()
        seed, drop = self.preset(report, "fx-seed"), self.preset(report, "fx-drop")
        self.assertEqual(seed.kind, "seed-map")
        self.assertTrue(seed.reason.startswith("→ balanced (unreadable: "), seed.reason)
        self.assertTrue(seed.reason.endswith("; outcome by its name)"), seed.reason)
        self.assertEqual(drop.kind, "drop")
        self.assertTrue(drop.reason.startswith("unreadable: "))
        self.assertEqual(report.blocking, ())

    def test_an_unreadable_source_fails_its_target(self) -> None:
        write_composition(self.compositions, None, "fx-convert", raw=b"{")
        self.write(golden_documents()["fx-merge"])
        report = self.plan()
        team = self.target(report, "fx-team")
        self.assertEqual(team.status, "failed")
        self.assertEqual(len(team.errors), 1)
        self.assertTrue(team.errors[0].startswith("source fx-convert is unreadable: "))
        self.assertEqual(self.preset(report, "fx-convert").kind, "convert")


class SourceTests(HomeCase):
    ABSENT = ("source fx-lost-src is not on disk; the table names it as the source; "
              "restore the file or change the table")

    def test_absent_named_source_fails_and_apply_refuses(self) -> None:
        self.write(golden_documents()["fx-lost-member"])
        report = self.plan()
        lost = self.target(report, "fx-lost")
        self.assertEqual((lost.status, lost.errors), ("failed", (self.ABSENT,)))
        self.assertEqual(lost.members, ("fx-lost-member",))
        code, out = self.main("--apply")
        self.assertEqual(code, 1)
        self.assertIn(f"    errors: {self.ABSENT}\n", out)
        self.assertTrue(out.endswith("refused: 1 blocking outcome(s); nothing was written\n"), out)
        self.assertFalse((self.config_root / "profiles").exists())

    def test_named_source_wins(self) -> None:
        documents = golden_documents()
        self.write(documents["fx-convert"], documents["fx-merge"])
        team = self.target(self.plan(), "fx-team")
        self.assertEqual(team.status, "write")
        self.assertEqual(team.document["lead"], {"model": "opus55", "effort": "ultracode"})
        self.assertEqual(team.document["description"], "Fixture team")

    def test_no_member_is_skipped(self) -> None:
        report = self.plan()
        self.assertEqual({t.target: t.status for t in report.targets},
                         {"fx-direct": "skipped", "fx-lost": "skipped", "fx-team": "skipped"})
        self.assertEqual(report.blocking, ())
        self.assertIn("\ntargets:\n  fx-direct  skipped  (source fx-direct)\n"
                      "  fx-lost  skipped  (source fx-lost-src)\n"
                      "  fx-team  skipped  (source fx-convert)\n\nsummary:\n", mp.render_text(report))


# ------------------------------------------------------------- conversion


class MechanicalRuleTests(unittest.TestCase):
    def build(self, *agents: dict, lead: str = "sol", lanes=None, **kw) -> mp.TargetOutcome:
        return convert("t", rule("src"), comp("src", lead, *agents, **kw), lanes=lanes)

    def test_preferred_picks_and_lane_is_the_effort(self) -> None:
        out = self.build(slot("cm-analyst", "sol", "xhigh"),
                         slot("cm-analyst", "qwen38", "max", preferred=True))
        self.assertEqual(out.document["agents"]["cm-analyst"], {"model": "qwen38", "effort": "max"})
        out = self.build(slot("cm-analyst", "sol", "xhigh"))
        self.assertEqual(out.document["agents"]["cm-analyst"], {"model": "sol", "effort": "xhigh"})

    def test_omitted_lane_uses_the_injected_table(self) -> None:
        out = self.build(slot("cm-analyst", "sol"), lanes={"sol": "xhigh"})
        self.assertEqual(out.document["agents"]["cm-analyst"]["effort"], "xhigh")
        with mock.patch.object(mp, "V1_DEFAULT_LANES", {"sol": "xhigh"}):
            out = self.build(slot("cm-analyst", "sol"))
        self.assertEqual(out.document["agents"]["cm-analyst"]["effort"], "xhigh")
        out = self.build(slot("cm-analyst", "sol"))
        self.assertEqual(out.document["agents"]["cm-analyst"]["effort"],
                         mp.V1_DEFAULT_LANES["sol"])

    def test_omitted_lane_without_a_table_entry_fails(self) -> None:
        out = self.build(slot("cm-analyst", "sol"), lanes={})
        self.assertEqual(out.status, "failed")
        self.assertIn("analyst sol: no lane and no frozen legacy default lane", out.errors)

    def test_undeclared_lane_takes_the_default_effort_with_a_note(self) -> None:
        out = self.build(slot("cm-analyst", "opus55", "high"))
        self.assertEqual(out.document["agents"]["cm-analyst"], {"model": "opus55", "effort": "xhigh"})
        self.assertIn("analyst: lane high is not offered by opus55 (declared: xhigh) → xhigh",
                      out.notes)

    def test_preferred_removed_falls_to_the_next(self) -> None:
        out = self.build(slot("cm-analyst", "muse-spark", "xhigh", preferred=True),
                         slot("cm-analyst", "sol"))
        self.assertEqual(out.document["agents"]["cm-analyst"], {"model": "sol", "effort": "high"})
        self.assertIn("analyst: preferred muse-spark removed → sol·high", out.notes)

    def test_all_removed_is_unbound(self) -> None:
        out = self.build(slot("cm-analyst", "muse-spark", "xhigh"))
        self.assertNotIn("cm-analyst", out.document["agents"])
        self.assertIn("analyst: muse-spark was removed — unbound", out.notes)

    def test_reviewer_strong_is_the_first_other_family_reviewer(self) -> None:
        out = self.build(slot("cm-reviewer", "sol", "xhigh"), slot("cm-reviewer", "gpt55", "high"),
                         slot("cm-reviewer", "opus55", "xhigh"), slot("cm-reviewer", "kimi-k3"))
        agents = out.document["agents"]
        self.assertEqual(agents["cm-reviewer"], {"model": "sol", "effort": "xhigh"})
        self.assertEqual(agents["cm-reviewer-strong"], {"model": "opus55", "effort": "xhigh"})

    def test_reviewer_strong_same_family_only_is_none(self) -> None:
        out = self.build(slot("cm-reviewer", "sol"), slot("cm-reviewer", "gpt55", "high"))
        self.assertNotIn("cm-reviewer-strong", out.document["agents"])

    def test_reviewer_strong_compares_with_the_lead_when_no_plain_reviewer(self) -> None:
        # The plain reviewer fails to bind (no lane), so the lead's family decides.
        out = self.build(slot("cm-reviewer", "sol"), slot("cm-reviewer", "gpt55", "high"),
                         slot("cm-reviewer", "opus55", "xhigh"), lanes={})
        self.assertEqual(out.status, "failed")
        self.assertNotIn("cm-reviewer", out.document["agents"])
        self.assertEqual(out.document["agents"]["cm-reviewer-strong"]["model"], "opus55")

    def test_no_other_grade_is_invented(self) -> None:
        out = self.build(slot("cm-analyst", "sol"), slot("cm-analyst", "opus55", "xhigh"),
                         slot("cm-implementer", "sol"), slot("cm-implementer", "qwen38"),
                         slot("cm-reviewer", "sol"), slot("cm-reviewer", "opus55", "xhigh"))
        self.assertEqual(set(out.document["agents"]),
                         {"cm-analyst", "cm-implementer", "cm-reviewer", "cm-reviewer-strong"})

    def test_lead_effort_comes_from_the_line_lead_block(self) -> None:
        out = self.build(lead="fable", lead_lane="max")
        self.assertEqual(out.document["lead"],
                         {"model": "fable", "effort": LCAT.lines["fable"]["lead"]["effort"]})

    def test_a_removed_lead_fails_a_convert_row(self) -> None:
        out = self.build(lead="muse-spark")
        self.assertEqual(out.status, "failed")
        self.assertEqual(out.errors, ("lead muse-spark was removed; a convert row needs a live lead",))

    def test_a_non_2x_role_is_ignored_with_a_note(self) -> None:
        out = self.build(slot("cm-designer", "sol"))
        self.assertIn("slot cm-designer sol: not a legacy agent role; ignored", out.notes)
        self.assertEqual(out.document["agents"], {})


class KeyTranslationTests(unittest.TestCase):
    def test_a_renamed_key_is_written_as_its_successor(self) -> None:
        lcat = _local_cat(retired={"x-old": _retired_entry(
            "sol", since=32, wire="x-old-1", selectors={"claude-multi-x-old": None},
            provider="openai")})
        out = convert("t", rule("src"), comp("src", "sol", slot("cm-analyst", "x-old", "high")),
                      lcat=lcat)
        self.assertEqual(out.document["agents"]["cm-analyst"], {"model": "sol", "effort": "high"})
        self.assertIn("analyst: x-old → sol (x-old was retired in catalog 32 -> sol)", out.notes)

    def test_bare_opus_is_translated_through_a_later_generation_move(self) -> None:
        lcat = _local_cat(retired={"opus@4.8": _retired_entry(
            "opus", since=33, wire="claude-opus-4-8", selectors={"claude-opus-4-8": None},
            generation="4.8")})
        out = convert("t", rule("src"),
                      comp("src", "sol", slot("cm-reviewer", "opus", "xhigh")), lcat=lcat)
        self.assertEqual(out.document["agents"]["cm-reviewer"], {"model": "opus", "effort": "xhigh"})
        self.assertIn("reviewer: opus → opus (opus@4.8 was retired in catalog 33 -> opus)", out.notes)

    def test_an_unknown_key_is_removed(self) -> None:
        out = convert("t", rule("src"), comp("src", "sol", slot("cm-analyst", "nosuch", "high")))
        self.assertNotIn("cm-analyst", out.document["agents"])
        self.assertIn("analyst: nosuch was removed — unbound", out.notes)


class OverrideTests(unittest.TestCase):
    SOURCE = comp("src", "sol", slot("cm-analyst", "qwen38", preferred=True),
                  slot("cm-analyst", "sol", "xhigh"), slot("cm-reviewer", "gpt55", "high"),
                  general_purpose="off")

    def test_an_override_replaces_the_mechanical_binding(self) -> None:
        out = convert("t", rule("src", kind=mm.RULE_OVERRIDES,
                                overrides={"cm-analyst": ("kimi-k3", "max")}), self.SOURCE)
        self.assertEqual(out.document["agents"]["cm-analyst"], {"model": "kimi-k3", "effort": "max"})
        self.assertIn("analyst: row override kimi-k3·max", out.notes)

    def test_a_none_effort_comes_from_the_first_same_function_slot(self) -> None:
        out = convert("t", rule("src", kind=mm.RULE_OVERRIDES,
                                overrides={"cm-analyst-strong": ("sol", None)}), self.SOURCE)
        self.assertEqual(out.document["agents"]["cm-analyst-strong"],
                         {"model": "sol", "effort": "xhigh"})

    def test_a_none_effort_without_a_slot_takes_the_line_default(self) -> None:
        out = convert("t", rule("src", kind=mm.RULE_OVERRIDES,
                                overrides={"cm-implementer": ("kimi-k3", None)}), self.SOURCE)
        default = LCAT.lines["kimi-k3"]["default_effort"]
        self.assertEqual(out.document["agents"]["cm-implementer"],
                         {"model": "kimi-k3", "effort": default})
        self.assertIn(f"implementer: no legacy implementer slot on kimi-k3 → {default} "
                      "(the line's default)", out.notes)

    def test_explicit_binds_only_the_lead_and_the_overrides(self) -> None:
        out = convert("t", rule("src", kind=mm.RULE_EXPLICIT, lead=("opus55", "ultracode"),
                                overrides={"cm-reviewer": ("gpt55", "high")}), self.SOURCE)
        self.assertEqual(out.document["lead"], {"model": "opus55", "effort": "ultracode"})
        self.assertEqual(out.document["agents"], {"cm-reviewer": {"model": "gpt55", "effort": "high"}})

    def test_rule_native_merges_and_primary_provider_is_carried(self) -> None:
        out = convert("t", rule("src", native={"general_purpose": "on"}, primary="openai"),
                      self.SOURCE)
        self.assertEqual(out.document["native_agents"],
                         {"explore": "native", "plan": "native", "general_purpose": "on"})
        self.assertEqual(out.document["primary_provider"], "openai")
        self.assertIn("  primary_provider openai\n", mp.render_text(dataclasses.replace(
            _empty_report(), targets=(out,))))


def _empty_report() -> mp.ProfileMigrationReport:
    return mp.ProfileMigrationReport(
        config_root=CONFIG_PLACEHOLDER, dry_run=True, catalog_version=31, launcher_version="x",
        marker="absent (state 3)", compositions_present=True, presets=(), targets=(),
        seeds=(), seeds_absent=(), seeds_installed=(), user_profiles_before=0, blocking=(),
    )


class CompositionConversionRulesTests(unittest.TestCase):
    def lead_providers(self, providers: dict, lead: str = "sol", **kw):
        out = convert("t", rule("src", **kw), comp("src", lead, providers=providers))
        return out.document.get("lead_providers"), out.notes

    def test_availability_selects_only_lead_enabled_providers(self) -> None:
        value, _notes = self.lead_providers(
            {"openai": "lead+agents", "anthropic": "lead", "qwen": "agents", "kimi": "off"})
        self.assertEqual(value, ["anthropic", "openai"])

    def test_non_catalog_provider_is_dropped_with_the_record_note(self) -> None:
        value, notes = self.lead_providers({"openai": "lead", "glm-x": "lead"})
        self.assertEqual(value, ["openai"])
        self.assertIn(f"lead_providers: glm-x is not a provider in catalog "
                      f"{LCAT.catalog_version} — dropped", notes)

    def test_adds_the_lead_provider_with_the_record_note(self) -> None:
        value, notes = self.lead_providers({"anthropic": "lead"})
        self.assertEqual(value, ["anthropic", "openai"])
        self.assertIn("lead_providers: added openai (the lead's provider, E18)", notes)

    def test_lead_providers_omitted_when_equal_to_every_enabled_provider(self) -> None:
        value, _notes = self.lead_providers({p: "lead+agents" for p in LCAT.providers})
        self.assertIsNone(value)

    def test_lead_providers_follow_the_record_migration_rule(self) -> None:
        providers = {"anthropic": "lead", "glm-x": "lead", "qwen": "agents"}
        notes: list[str] = []
        expected = migrate._lead_providers(
            LCAT, {"availability": {"providers": providers}}, "openai",
            migrate.default_settings_snapshot(LCAT), notes)
        value, mine = self.lead_providers(providers)
        self.assertEqual(value, expected)
        self.assertTrue(set(notes) <= set(mine))

    def test_lead_provider_rows(self) -> None:
        value, _notes = self.lead_providers(
            {"anthropic": "lead"}, lead="qwen38", lead_providers=mm.LEAD_PROVIDERS_LEAD_PROVIDER)
        self.assertEqual(value, ["qwen"])

    def test_explorer_copies_the_analyst_after_its_override(self) -> None:
        source = comp("src", "sol", slot("cm-analyst", "sol"), slot("cm-analyst", "qwen38", "max"),
                      explore="replace")
        out = convert("t", rule("src", kind=mm.RULE_OVERRIDES,
                                overrides={"cm-analyst": ("qwen38", None)}), source)
        agents = out.document["agents"]
        self.assertEqual(agents["cm-explorer"], {"model": "qwen38", "effort": "max"})
        self.assertEqual(agents["cm-explorer"], agents["cm-analyst"])
        self.assertEqual(out.document["native_agents"]["explore"], "replace")

    def test_replace_without_an_analyst_is_native(self) -> None:
        out = convert("t", rule("src"), comp("src", "sol", slot("cm-reviewer", "sol"),
                                             explore="replace"))
        self.assertEqual(out.document["native_agents"]["explore"], "native")
        self.assertNotIn("cm-explorer", out.document["agents"])
        self.assertIn("Explore: replace → native (no analyst left to back cm-explorer)", out.notes)

    def test_merge_differences_report_lead_providers_and_description(self) -> None:
        documents = golden_documents()
        _targets, rules = fixture_tables()
        out = convert("fx-team", rules["fx-team"], documents["fx-convert"], documents["fx-merge"])
        self.assertEqual(len(out.differs), 1)
        line = out.differs[0]
        self.assertTrue(line.startswith("differs (fx-merge): lead sol·ultracode "
                                        "(source opus55·ultracode); "), line)
        self.assertIn("lead_providers [openai] (source [anthropic])", line)
        self.assertIn("description differs", line)
        same = comp("fx-merge", "opus55", *documents["fx-convert"]["slots"][1:],
                    explore="replace", general_purpose="off",
                    providers=documents["fx-convert"]["availability"]["providers"],
                    description="Fixture team")
        out = convert("fx-team", rules["fx-team"], documents["fx-convert"], same)
        self.assertEqual(out.differs, ("differs (fx-merge): none",))

    def test_same_family_review_and_no_invented_reviewer(self) -> None:
        source = comp("src", "sol", slot("cm-analyst", "sol"), slot("cm-implementer", "sol"),
                      slot("cm-reviewer", "sol"), providers={"openai": "lead+agents"})
        out = convert("t", rule("src", expected=("same-family-review",)), source)
        self.assertEqual(out.status, "write")
        self.assertIn("same-family-review", [w.code for w in out.warnings])
        self.assertNotIn("cm-reviewer-strong", out.document["agents"])
        self.assertNotIn("expected same-family-review not produced", out.notes)

    def test_dropped_rows_map_records_to_null(self) -> None:
        targets, _rules = fixture_tables()
        with mock.patch.object(mm, "PRESET_TARGETS", MappingProxyType(targets)):
            self.assertEqual(mm.record_profile("fx-drop"), (None, "dropped"))
            self.assertEqual(mm.record_profile("fx-judgment"), (None, "dropped"))


class ParityWithRecordsTests(unittest.TestCase):
    def test_lead_providers_equal_the_migrated_record(self) -> None:
        availability = {"anthropic": "lead+agents", "openai": "agents", "glm-x": "lead"}
        record = v3_managed(composition_name="src", availability=availability)
        migrated = migrate.convert_record(record, cat=LCAT, now=NOW).record
        lead = record["snapshot"]["lead"]["model"]
        out = convert("t", rule("src"), comp("src", lead, providers=availability))
        self.assertEqual(out.document.get("lead_providers"), migrated["applied"]["lead_providers"])
        self.assertEqual(out.document["lead"]["model"], migrated["applied"]["lead"]["key"])


class WarningsCaptureTests(HomeCase):
    def test_every_finding_in_order_and_counts(self) -> None:
        self.write_golden_set(blocking=False)
        report = self.plan()
        team = self.target(report, "fx-team")
        self.assertTrue(team.warnings)
        text = mp.render_text(report)
        expected_line = "    warnings: " + "; ".join(f"{w.code}: {w.message}" for w in team.warnings)
        self.assertIn(expected_line + "\n", text)
        counts: dict[str, int] = {}
        for item in report.targets:
            for finding in item.warnings:
                counts[finding.code] = counts.get(finding.code, 0) + 1
        summary = ", ".join(f"{code} {counts[code]}" for code in sorted(counts))
        self.assertIn(f"  warnings: {summary}; notices: ", text)
        self.assertIn("    expected warnings: fan-out-quota\n", text)

    def test_a_missing_expected_code_is_an_informational_note(self) -> None:
        self.rules["fx-direct"] = dataclasses.replace(
            self.rules["fx-direct"], expected_warnings=("same-family-review",))
        self.write(golden_documents()["fx-direct"])
        direct = self.target(self.plan(), "fx-direct")
        self.assertEqual(direct.status, "write")
        self.assertIn("expected same-family-review not produced", direct.notes)
        code, out = self.main()
        self.assertEqual(code, 0)
        self.assertIn("expected same-family-review not produced", out)


class EvaluateFailureTests(HomeCase):
    def test_every_evaluate_error_fails_the_target_and_apply_writes_nothing(self) -> None:
        # A lead-only native effort and a missing gateway-effort mapping
        # are routing errors, not capability or role recommendations.
        self.rules = {"fx-team": rule("fx-convert", kind=mm.RULE_EXPLICIT,
                                      lead=("opus55", "ultracode"),
                                      overrides={"cm-analyst": ("opus", "ultracode"),
                                                 "cm-implementer": ("sol", "medium")})}
        self.targets = {"fx-convert": (mm.CONVERT, "fx-team")}
        self.write(golden_documents()["fx-convert"])
        team = self.target(self.plan(), "fx-team")
        self.assertEqual(team.status, "failed")
        evaluation = profile.evaluate(team.document, LCAT)
        self.assertGreaterEqual(len(evaluation.errors), 2)
        self.assertEqual(team.errors, evaluation.errors)
        code, out = self.main("--apply")
        self.assertEqual(code, 1)
        self.assertIn("    errors: " + "; ".join(evaluation.errors) + "\n", out)
        self.assertFalse((self.config_root / "profiles").exists())

    def test_capability_and_role_recommendations_warn_without_blocking_apply(self) -> None:
        self.rules = {"fx-team": rule("fx-convert", kind=mm.RULE_EXPLICIT,
                                      lead=("opus55", "ultracode"),
                                      overrides={"cm-analyst": ("opus", "xhigh"),
                                                 "cm-implementer": ("qwen-flash-next", "high")})}
        self.targets = {"fx-convert": (mm.CONVERT, "fx-team")}
        self.write(golden_documents()["fx-convert"])
        source_before = tree_snapshot(self.compositions)
        team = self.target(self.plan(), "fx-team")
        self.assertEqual(team.status, "write")
        self.assertEqual(team.errors, ())
        self.assertTrue({"capability-recommendation", "role-recommendation"}
                        <= {warning.code for warning in team.warnings})
        code, out = self.main("--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("capability-recommendation", out)
        self.assertIn("role-recommendation", out)
        self.assertEqual(self.store().load("fx-team"), profile.parse(team.document))
        self.assertEqual(tree_snapshot(self.compositions), source_before)


class NotCarriedTests(unittest.TestCase):
    def test_slots_not_carried_per_member(self) -> None:
        source = comp("src", "sol", slot("cm-analyst", "sol", preferred=True),
                      slot("cm-analyst", "qwen38"), slot("cm-reviewer", "muse-spark", "xhigh"),
                      slot("cm-reviewer", "gpt55", "high"),
                      models={"sol": "agents", "gpt55": "agents"})
        member = comp("mem", "sol", slot("cm-analyst", "kimi-k3"), slot("cm-analyst", "sol"))
        out = convert("t", rule("src", ("mem", "src")), source, member)
        self.assertEqual(out.not_carried, (
            "src: analyst qwen38@default",
            "src: reviewer muse-spark@xhigh (removed)",
            "src: availability.models (2 entries)",
            "mem: analyst kimi-k3@default",
        ))


# ----------------------------------------------------------- CLI behaviour


class DryRunNoWritesTests(HomeCase):
    """A dry run writes nothing, with and without ``compositions/``.

    The read-only homes carry the 2.x state root with its ``sessions/`` and
    ``last-session-by-cwd/`` directories, as every 2.x install does. On a
    home without them the landed ``sessions.SessionStore.__init__``
    creates those private directories for every Runtime (``migrate
    --dry-run`` too), pinned by
    ``test_fresh_home_creates_only_the_session_store_dirs`` so it cannot
    widen unnoticed.
    """

    SESSION_STORE_DIRS = ("sessions", "last-session-by-cwd")

    def seed_2x_state_root(self) -> None:
        for name in self.SESSION_STORE_DIRS:
            state.ensure_private_dir(self.state_root / name)

    def run_dry(self, argv: tuple[str, ...]) -> tuple[dict, dict]:
        """Run the dry run under the write tripwires; the tree before and after."""

        before = tree_snapshot(self.tmp)
        captured: list[dict] = []
        failing = mock.Mock(side_effect=AssertionError("a dry run wrote"))
        with mock.patch.object(profile.ProfileStore, "new", failing), \
                mock.patch.object(profile.ProfileStore, "install_seeds", failing), \
                mock.patch.object(state, "atomic_write", failing), \
                mock.patch.object(scope, "ensure_hook_shim", failing), \
                mock.patch.object(scope, "ensure_hook_shim_v3", failing), \
                mock.patch.object(scope, "ensure_token_helper_command", failing):
            code, out = self.main(*argv, captured=captured)
        self.assertIn(code, (0, 1))
        self.assertTrue(out.startswith("claude-multi profile migrate --dry-run: config "), out)
        self.assertTrue(out.endswith(mp.DRY_RUN_FOOTER + "\n"), out)
        self.assertEqual(failing.call_count, 0)
        self.assertEqual(captured[0]["allow_state_writes"], False)
        self.assertEqual(captured[0]["refresh_shims"], False)
        for path in (self.config_root / "profiles", self.config_root / "profiles.lock",
                     self.state_root / "locks", self.state_root / "state-version",
                     self.state_root / "bin", self.state_root / "gateway"):
            self.assertFalse(os.path.lexists(path), path)
        return before, tree_snapshot(self.tmp)

    def assert_read_only(self, argv: tuple[str, ...]) -> None:
        before, after = self.run_dry(argv)
        self.assertEqual(after, before)

    def test_populated_home(self) -> None:
        self.seed_2x_state_root()
        self.write_golden_set()
        for argv in ((), ("--dry-run",)):
            with self.subTest(argv=argv):
                self.assert_read_only(argv)

    def test_home_without_compositions(self) -> None:
        self.seed_2x_state_root()
        for argv in ((), ("--dry-run",)):
            with self.subTest(argv=argv):
                self.assert_read_only(argv)
                self.assertFalse(self.compositions.exists())

    def test_fresh_home_creates_only_the_session_store_dirs(self) -> None:
        # The recorded exception: no state root yet, so the Runtime's SessionStore creates
        # it with sessions/ and last-session-by-cwd/, empty and 0700.
        # Nothing else appears and nothing existing changes. When the
        # SessionStore stops doing this, this test fails: fold the fresh
        # home into assert_read_only then.
        self.assertFalse(os.path.lexists(self.state_root))
        before, after = self.run_dry(())
        # The state root and its missing ancestors inside the temp home.
        created = [self.state_root / name for name in self.SESSION_STORE_DIRS]
        created += [path for path in (self.state_root, *self.state_root.parents)
                    if path.is_relative_to(self.tmp) and path != self.tmp]
        expected_new = {str(path.relative_to(self.tmp)) for path in created} - set(before)
        self.assertEqual(set(after) - set(before), expected_new)
        self.assertEqual({key: after[key] for key in before}, before)
        for name in self.SESSION_STORE_DIRS:
            key = str((self.state_root / name).relative_to(self.tmp))
            self.assertEqual(after[key], (0o700, ("dir", None)), key)
            self.assertEqual(list((self.state_root / name).iterdir()), [])
        again_before, again_after = self.run_dry(("--dry-run",))
        self.assertEqual(again_after, again_before)


class ApplyTests(HomeCase):
    def test_apply_writes_seeds_and_targets(self) -> None:
        self.write_golden_set(blocking=False)
        store = self.store()
        edited = store.load("balanced")
        edited["description"] = "edited by the operator"
        store.save(edited)
        edited_bytes = (self.config_root / "profiles" / "balanced.json").read_bytes()
        compositions_before = tree_snapshot(self.compositions)
        mtimes = {p.name: p.stat().st_mtime_ns for p in self.compositions.iterdir()}
        planned = self.plan(dry_run=True)
        code, out = self.main("--apply")
        self.assertEqual(code, 0, out)
        profiles = self.config_root / "profiles"
        self.assertEqual(stat.S_IMODE(profiles.stat().st_mode), 0o700)
        for seed in catalog.SEED_PROFILE_NAMES:
            self.assertTrue((profiles / f"{seed}.json").is_file(), seed)
        self.assertEqual((profiles / "balanced.json").read_bytes(), edited_bytes)
        for item in planned.targets:
            if item.status != "write":
                continue
            path = profiles / f"{item.target}.json"
            self.assertEqual(path.read_bytes(),
                             strict_json.pretty_file_bytes(profile.parse(item.document)))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(tree_snapshot(self.compositions), compositions_before)
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in self.compositions.iterdir()},
                         mtimes)
        self.assertIn("  fx-team  written  (source fx-convert; members fx-convert, fx-merge)\n", out)
        self.assertIn("  fx-direct  written  (source fx-direct)\n", out)
        seeds = len(catalog.SEED_PROFILE_NAMES)
        self.assertIn(f"  seeds {seeds}: installed {seeds - 1}, present 1; profiles after: {seeds + 2}\n", out)
        self.assertTrue(out.endswith(
            f"applied: wrote 2 profiles, installed {seeds - 1} seeds; compositions untouched\n"), out)


class IdempotencyTests(HomeCase):
    def count_profile_writes(self, argv=("--apply",)):
        real_write = state.atomic_write
        calls: list[Path] = []

        def spy(path, *args, **kwargs):
            calls.append(Path(path))
            return real_write(path, *args, **kwargs)

        with mock.patch.object(state, "atomic_write", side_effect=spy), \
                mock.patch.object(profile.ProfileStore, "new", autospec=True,
                                  side_effect=profile.ProfileStore.new) as new, \
                mock.patch.object(profile.ProfileStore, "install_seeds", autospec=True,
                                  side_effect=profile.ProfileStore.install_seeds) as seeds:
            code, out = self.main(*argv)
        writes = [p for p in calls if self.config_root in p.parents]
        return code, out, new.call_count, seeds.call_count, writes

    def test_a_second_apply_is_nothing_to_do(self) -> None:
        self.write_golden_set(blocking=False)
        code, out = self.main("--apply")
        self.assertEqual(code, 0, out)
        code, out, news, seeds, writes = self.count_profile_writes()
        self.assertEqual(code, 0, out)
        self.assertEqual((news, seeds, writes), (0, 0, []))
        self.assertIn("  fx-team  already-migrated  (source fx-convert;", out)
        self.assertIn("  fx-direct  already-migrated  (source fx-direct)", out)
        self.assertTrue(out.endswith(mp.NOTHING_TO_DO_FOOTER + "\n"), out)

    def test_a_crash_on_the_second_new_converges_on_rerun(self) -> None:
        self.write_golden_set(blocking=False)
        real_new = profile.ProfileStore.new
        seen = []

        def crashing(store, document):
            seen.append(document["name"])
            if len(seen) == 2:
                raise OSError("disk full (injected)")
            return real_new(store, document)

        with mock.patch.object(profile.ProfileStore, "new", autospec=True, side_effect=crashing):
            code, out = self.main("--apply")
        self.assertEqual(code, 1)
        self.assertEqual(seen, ["fx-direct", "fx-team"])
        self.assertIn("  fx-direct  written  (source fx-direct)\n", out)
        self.assertIn("  fx-team  write  (source fx-convert;", out)
        seeds = len(catalog.SEED_PROFILE_NAMES)
        self.assertIn(f"\nstopped: wrote 1 profiles, installed {seeds} seeds before an error; "
                      "compositions untouched; rerun to converge\n"
                      f"claude-multi: profile migrate stopped after writing 1 profile(s) and {seeds} "
                      "seed(s): disk full (injected)", out)
        code, out = self.main("--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("  fx-direct  already-migrated  (source fx-direct)\n", out)
        self.assertIn("  fx-team  written  (source fx-convert;", out)
        self.assertTrue(out.endswith(
            "applied: wrote 1 profiles, installed 0 seeds; compositions untouched\n"), out)


class RefusalTests(HomeCase):
    def test_pm1_dry_run_and_apply_are_exclusive(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            cli.main(["profile", "migrate", "--dry-run", "--apply"], output_stream=io.StringIO(),
                     interactive=False)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("argument --apply: not allowed with argument --dry-run", err.getvalue())

    def write_existing(self, document: dict) -> None:
        store = self.store()
        store.save(document, target="fx-direct")

    def test_a_differing_existing_file_is_a_conflict_with_the_remedy(self) -> None:
        self.write(golden_documents()["fx-direct"])
        planned = self.target(self.plan(), "fx-direct")
        different = dict(planned.document, description="mine")
        self.write_existing(different)
        before = (self.config_root / "profiles" / "fx-direct.json").read_bytes()
        code, out = self.main("--apply")
        self.assertEqual(code, 1)
        self.assertIn(
            "    errors: profile fx-direct exists and differs from the migration result; compare "
            "with claude-multi profile show fx-direct; to re-migrate, rename it (claude-multi "
            "profile rename fx-direct fx-direct-2x-old) and rerun\n", out)
        self.assertTrue(out.endswith("refused: 1 blocking outcome(s); nothing was written\n"))
        self.assertEqual((self.config_root / "profiles" / "fx-direct.json").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (self.config_root / "profiles").iterdir()),
                         ["fx-direct.json"])

    def test_an_unreadable_existing_file_is_a_conflict(self) -> None:
        self.write(golden_documents()["fx-direct"])
        profiles = state.ensure_private_dir(self.config_root / "profiles")
        state.atomic_write(profiles / "fx-direct.json", b"{")
        target = self.target(self.plan(), "fx-direct")
        self.assertEqual(target.status, "conflict")
        self.assertTrue(target.errors[0].startswith("existing profile fx-direct is unreadable: "))

    def test_a_seed_named_target_is_a_conflict(self) -> None:
        self.targets = {"fx-direct": (mm.CONVERT, "balanced")}
        self.rules = {"balanced": rule("fx-direct", lead_providers=mm.LEAD_PROVIDERS_LEAD_PROVIDER)}
        self.write(golden_documents()["fx-direct"])
        target = self.target(self.plan(), "balanced")
        self.assertEqual((target.status, target.errors), ("conflict", ("balanced is a seed name",)))

    def test_pm2_a_newer_marker(self) -> None:
        self.write_golden_set(blocking=False)
        state.ensure_private_dir(self.state_root)
        state.atomic_write(self.state_root / "state-version", b"5\n")
        code, out = self.main("--apply")
        self.assertEqual(code, 1)
        self.assertEqual(out, f"claude-multi: {sessions.StateMarkerError(5)}\n")
        self.assertFalse((self.config_root / "profiles").exists())
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertIn("; state marker newer (state version 5): --apply refuses\n", out)

    def test_a_concurrent_new_is_re_compared(self) -> None:
        self.write(golden_documents()["fx-direct"])
        real_new = profile.ProfileStore.new
        for variant, status in (("equal", "already-migrated"), ("different", "conflict")):
            with self.subTest(variant):
                shutil.rmtree(self.config_root / "profiles", True)

                def racing(store, document):
                    other = dict(document)
                    if variant == "different":
                        other["description"] = "another writer"
                    real_new(store, other)  # the concurrent writer wins the race
                    return real_new(store, document)  # P1: already exists

                with mock.patch.object(profile.ProfileStore, "new", autospec=True,
                                       side_effect=racing):
                    code, out = self.main("--apply")
                self.assertIn(f"  fx-direct  {status}  (source fx-direct)\n", out)
                if status == "conflict":
                    self.assertEqual(code, 1)
                    self.assertIn("\nconflict: 1 target(s) changed while applying; wrote 0 "
                                  f"profiles, installed {len(catalog.SEED_PROFILE_NAMES)} seeds; "
                                  "compositions untouched\n", out)
                else:
                    self.assertEqual(code, 0, out)


# ------------------------------------------------------------ data guards


class DataTests(unittest.TestCase):
    def test_frozen_default_lanes_equal_the_catalog32_fixture(self) -> None:
        models = strict_json.load(CATALOG32)["models"]
        self.assertEqual(dict(mp.V1_DEFAULT_LANES),
                         {key: entry["default_lane"] for key, entry in models.items()})

    def test_legacy_roles_are_the_record_rule(self) -> None:
        self.assertEqual(mp.LEGACY_AGENT_ROLES, migrate.MECHANICAL_ROLES)
        self.assertEqual(set(mp.FUNCTION_ROLE) | {"cm-designer"}, set(catalog.AGENT_ROLE_IDS))

    def test_ast_guard_no_writer_call(self) -> None:
        # cf-8: call targets only; string literals and docstrings are excluded.
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        forbidden = {"atomic_write", "remove_private", "unlink", "rename", "CompositionStore",
                     "validate_composition"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                self.assertNotIn(name, forbidden, ast.unparse(node))
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
                self.assertNotEqual(node.value.attr, "models", ast.unparse(node))

    def test_imports_follow_the_module_rule(self) -> None:
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        allowed = {"catalog", "composition", "migration_map", "migrate", "profile", "sessions",
                   "state", "strict_json"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                self.assertTrue({alias.name for alias in node.names} <= allowed)
        used = {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "migrate"
        }
        self.assertEqual(used, {"translate_key", "_lead_providers", "default_settings_snapshot",
                                "MigrationError"})

    def test_tables_are_read_at_call_time(self) -> None:
        for name in ("plan", "convert_target"):
            signature = inspect.signature(getattr(mp, name))
            for parameter in ("preset_targets", "convert_rules", "default_lanes"):
                if parameter in signature.parameters:
                    self.assertIsNone(signature.parameters[parameter].default, (name, parameter))


@uses_shipped_catalog
class ShippedTableTests(unittest.TestCase):
    """The shipped MIGRATION.md table against the shipped catalog (release data)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cat = catalog.load_catalog(SHIPPED_ROOT)
        cls.lcat = profile.LineupCatalog.from_catalog(cls.cat)

    def admits(self, key: str, rid: str, effort: str) -> None:
        entry = self.lcat.lines[key]
        roles = entry["roles"]
        self.assertTrue(roles == "all" or rid in roles, (key, rid))
        self.assertIn(effort, profile.declared_efforts(entry), (key, rid, effort))

    def test_overrides_and_fixed_leads_resolve_to_live_lines(self) -> None:
        for target, the_rule in mm.CONVERT_RULES.items():
            with self.subTest(target):
                for rid, (key, effort) in the_rule.overrides.items():
                    self.assertIsNone(self.lcat.resolve_key(key).notice, key)
                    if effort is not None:
                        self.admits(key, rid, effort)
                    else:
                        roles = self.lcat.lines[key]["roles"]
                        self.assertTrue(roles == "all" or rid in roles, (key, rid))
                if the_rule.lead is not None:
                    key, effort = the_rule.lead
                    entry = self.lcat.lines[key]
                    self.assertIn("lead", entry["capabilities"])
                    self.assertEqual(entry["lead"]["effort"], effort)

    def test_seed_and_convert_targets(self) -> None:
        seeds = {t for kind, t in mm.PRESET_TARGETS.values() if kind == mm.SEED}
        self.assertTrue(seeds <= set(catalog.SEED_PROFILE_NAMES))
        self.assertFalse(set(mm.CONVERT_RULES) & set(catalog.SEED_PROFILE_NAMES))
        self.assertEqual(set(mm.CONVERT_RULES), mm.convert_targets())

    def test_explicit_rules_evaluate_with_a_minimal_source(self) -> None:
        for target, the_rule in mm.CONVERT_RULES.items():
            if the_rule.rule != mm.RULE_EXPLICIT:
                continue
            with self.subTest(target):
                key = the_rule.lead[0]
                provider = self.lcat.lines[key]["provider"]
                source = comp(the_rule.source, key, providers={provider: "lead+agents"})
                out = mp.convert_target(target, the_rule, presets(source), lcat=self.lcat)
                self.assertEqual((out.status, out.errors), ("write", ()))


if __name__ == "__main__":
    unittest.main()
