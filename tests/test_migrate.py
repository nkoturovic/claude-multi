"""``claude-multi migrate``.

Synthetic 2.x records of every variant are written as raw JSON matching the
session schema (no 2.x builder). Retired-key cases that the
frozen fixture cannot express (renamed, successor, same-key generation
moves) use test-local catalog copies (``_local_cat``), never shipped ids.
Temp HOME/XDG roots only; no test reaches the live gateway or state.
"""

from __future__ import annotations

import copy
import dataclasses
import io
import os
import shlex
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import _tripwire
from claude_multi import (
    catalog,
    cli,
    migrate,
    migration_map,
    profile,
    scope,
    sessions,
    state,
    strict_json,
    validate,
)
from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT, GOLDENS_ROOT, SHIPPED_ROOT, uses_shipped_catalog
from _golden import assertGolden
import _v3
import claude_multi.launch
import claude_multi.sessions

_tripwire.install()

NOW = "2026-09-25T00:00:00Z"
CREATED = "2026-09-01T00:00:00Z"
HASH0 = "sha256:" + "0" * 64
TOKEN = "f0000000-0000-4000-8000-000000000000"
M1 = "a1111111-1111-4111-8111-111111111111"
M2 = "a2222222-2222-4222-8222-222222222222"
M3 = "a3333333-3333-4333-8333-333333333333"
M4 = "a4444444-4444-4444-8444-444444444444"
M5 = "a5555555-5555-4555-8555-555555555555"
M6 = "a6666666-6666-4666-8666-666666666666"
M7 = "a7777777-7777-4777-8777-777777777777"
M8 = "a8888888-8888-4888-8888-888888888888"
RUNTIME = "b1111111-1111-4111-8111-111111111111"
ALIAS = "b2222222-2222-4222-8222-222222222222"
FORK = "b3333333-3333-4333-8333-333333333333"

BUNDLE = catalog.load_catalog(FIXTURE_ROOT)
LCAT = profile.LineupCatalog.from_docs(BUNDLE.docs)
SCHEMA = strict_json.load(FIXTURE_ROOT / "schemas" / "session.schema.json")
SNAP = _v3.snapshot_for(BUNDLE.docs)  # The 2.x resolver lives in tests/_v3.py
GOLDENS = GOLDENS_ROOT / "v4" / "migrate"
LEAD_SELECTOR = SNAP["lead"]["client_selector"]

SHIPPED_TARGETS = migration_map.PRESET_TARGETS
SHIPPED_RULES = migration_map.CONVERT_RULES
# Test-local reviewed rows of the legacy table (none ship): the names the
# synthetic records carry, one per outcome kind.
LEGACY_TARGETS = {
    "opus-sol": (migration_map.SEED, "balanced"),
    "fable": (migration_map.SEED, "max"),
    "kimi-sol": (migration_map.DROPPED, None),
    "glm-sol": (migration_map.DROPPED, None),
    "muse": (migration_map.CONVERT, "muse"),
    "astra-muse": (migration_map.CONVERT, "astra-muse"),
}
LEGACY_RULES = {
    "muse": migration_map.generic_rule("muse"),
    "astra-muse": migration_map.generic_rule("astra-muse"),
}


def legacy_rows():
    """Patch the test-local rows over the shipped (empty) table."""

    from types import MappingProxyType
    import contextlib

    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(migration_map, "PRESET_TARGETS", MappingProxyType(LEGACY_TARGETS)))
    stack.enter_context(mock.patch.object(migration_map, "CONVERT_RULES", MappingProxyType(LEGACY_RULES)))
    return stack


_MODULE_ROWS = None


def setUpModule() -> None:
    global _MODULE_ROWS
    _MODULE_ROWS = legacy_rows()


def tearDownModule() -> None:
    _MODULE_ROWS.close()


def _local_cat(*, lines: dict | None = None, retired: dict | None = None) -> profile.LineupCatalog:
    """A test-local lineup catalog: never validated."""

    return dataclasses.replace(
        LCAT,
        lines={**LCAT.lines, **(lines or {})},
        retired={**LCAT.retired, **(retired or {})},
    )


def _retired_entry(successor: str | None, *, since: int, wire: str, selectors: dict,
                   provider: str = "anthropic", generation: str | None = None) -> dict:
    entry = {
        "successor": successor, "reason": "test-local", "since_catalog": since,
        "provider": provider, "last_wire": wire, "display": wire,
        "context_tokens": 1_000_000, "capabilities": ["lead", "agents"], "roles": "all",
        "selectors": selectors,
    }
    if generation is not None:
        entry["generation"] = generation
    return entry


def v3_managed(
    managed_id: str = M1,
    *,
    composition_name: str = "opus-sol",
    lead: dict | None = None,
    variants: list | None = None,
    native: dict | None = None,
    availability: dict | None = None,
    catalog_version: int = 31,
    last_event_source: str | None = "end",
    last_seen_at: str = NOW,
    cwd: str = "/project/a",
    snapshot_workflows: str | None = None,
    workflows: str = "native",
    **extra,
) -> dict:
    snap = copy.deepcopy(SNAP)
    if lead is not None:
        snap["lead"].update(lead)
    if variants is not None:
        snap["variants"] = variants
    if native is not None:
        snap["native_agents"] = native
    if availability is not None:
        snap["availability"]["providers"] = availability
    if snapshot_workflows is not None:
        snap["workflows"] = snapshot_workflows
    record = {
        "version": 3,
        "managed_id": managed_id,
        "runtime_session_id": managed_id,
        "runtime_aliases": [],
        "mutation_token": TOKEN,
        "session_type": sessions.SESSION_TYPE_MANAGED,
        "identity_state": sessions.IDENTITY_AUTHORITATIVE,
        "last_event_source": last_event_source,
        "last_seen_at": last_seen_at,
        "pending_forks": [],
        "launch_epoch": 1,
        "cwd": cwd,
        "composition_name": composition_name,
        "composition_hash": strict_json.bundle_digest(snap),
        "snapshot": snap,
        "mode": "durable",
        "scope_generation": 1,
        "workflows": workflows,
        "catalog_version": catalog_version,
        "catalog_hash": HASH0,
        "launcher_version": "2.26.0",
        "created_at": CREATED,
        "forked_from": None,
        "migrated_from_version": None,
    }
    record.update(extra)
    return record


def v3_ordinary(
    managed_id: str = M2,
    *,
    model: str = "qwen38",
    context_profile: str | None = "large",
    last_event_source: str | None = "end",
    last_seen_at: str = NOW,
    cwd: str = "/project/b",
    **extra,
) -> dict:
    record = {
        "version": 3,
        "managed_id": managed_id,
        "runtime_session_id": managed_id,
        "runtime_aliases": [],
        "mutation_token": TOKEN,
        "session_type": sessions.SESSION_TYPE_ORDINARY,
        "identity_state": sessions.IDENTITY_AUTHORITATIVE,
        "last_event_source": last_event_source,
        "last_seen_at": last_seen_at,
        "pending_forks": [],
        "launch_epoch": 2,
        "cwd": cwd,
        "ordinary_model": model,
        "context_profile": context_profile,
        "mode": "durable",
        "scope_generation": 1,
        "catalog_version": 31,
        "catalog_hash": HASH0,
        "launcher_version": "2.26.0",
        "created_at": CREATED,
        "forked_from": None,
        "migrated_from_version": None,
    }
    record.update(extra)
    return record


def v2(managed_id: str = M3) -> dict:
    return {
        "version": 2, "session_id": managed_id, "cwd": "/project/c",
        "composition_name": "fable", "composition_hash": strict_json.bundle_digest(SNAP),
        "snapshot": copy.deepcopy(SNAP), "mode": "durable", "scope_generation": 1,
        "workflows": "native", "catalog_version": 30, "catalog_hash": HASH0,
        "launcher_version": "2.2.0", "created_at": CREATED, "forked_from": None,
    }


def v1(managed_id: str = M4) -> dict:
    record = v2(managed_id)
    record["version"] = 1
    record["composition_name"] = "default"
    for key in ("mode", "scope_generation", "workflows"):
        record.pop(key)
    return record


def _variant(role: str, model: str, lane: str, selector: str, preferred: bool) -> dict:
    return {"id": f"{role}-{model}-{lane}", "role": role, "model": model, "lane": lane,
            "client_selector": selector, "preferred": preferred}


SOL_HIGH = "gpt-multi-sol-high[1m]"
KIMI = "claude-multi-kimi-k3[1m]"
MUSE_HIGH = "claude-multi-muse-spark-high[1m]"
OPUS5 = "claude-multi-opus-5[1m]"


def golden_inputs() -> dict[str, dict]:
    """The synthetic v1-3 inputs of the record goldens (fixture catalog only)."""

    return {
        "managed-unchanged": v3_managed(M1),
        "managed-needs-choice": v3_managed(
            M5,
            composition_name="kimi-sol",
            lead={"model": "muse-spark", "client_selector": MUSE_HIGH, "effort": "xhigh"},
        ),
        "managed-fallback": v3_managed(
            M6,
            composition_name="astra-muse",
            variants=[
                _variant("cm-analyst", "muse-spark", "high", MUSE_HIGH, True),
                _variant("cm-analyst", "kimi-k3", "max", KIMI, False),
                _variant("cm-implementer", "sol", "high", SOL_HIGH, True),
                _variant("cm-reviewer", "sol", "weird", SOL_HIGH, True),
                _variant("cm-reviewer", "opus5", "xhigh", OPUS5, False),
            ],
        ),
        "managed-explore-downgrade": v3_managed(
            M7,
            composition_name="sol-qwen-glm-deepseek-flash",
            variants=[
                _variant("cm-analyst", "muse-spark", "high", MUSE_HIGH, True),
                _variant("cm-implementer", "sol", "high", SOL_HIGH, True),
                _variant("cm-reviewer", "sol", "xhigh", "gpt-multi-sol-xhigh[1m]", True),
            ],
            availability={"openai": "lead+agents"},
        ),
        "ordinary-single": v3_ordinary(M2, model="qwen38", context_profile=None,
                                       no_subagents=True),
        "ordinary-large": v3_ordinary(M8, model="sol", context_profile="large"),
        "legacy-v1": v1(M4),
        "legacy-v2": v2(M3),
    }


def migrate_golden_files() -> dict[str, bytes]:
    """``migrate.convert_record(...).record`` per golden input: file name -> bytes."""

    with legacy_rows():
        return {
            f"{name}.json": strict_json.pretty_file_bytes(
                migrate.convert_record(raw, cat=LCAT, now=NOW).record
            )
            for name, raw in golden_inputs().items()
        }


def _write_raw(store: sessions.SessionStore, record: dict) -> None:
    stable = record.get("managed_id") or record["session_id"]
    state.atomic_write(
        store.sessions_dir / f"{stable}.json", strict_json.canonical_file_bytes(record)
    )


def _write_pointer(store: sessions.SessionStore, cwd: str, session_id: str,
                   session_type: str | None = sessions.SESSION_TYPE_MANAGED) -> None:
    payload = {"cwd": cwd, "session_id": session_id}
    if session_type is not None:
        payload["session_type"] = session_type
    digest = strict_json.sha256_hex(cwd.encode("utf-8"))[:32]
    suffix = ".ordinary" if session_type == sessions.SESSION_TYPE_ORDINARY else ""
    state.atomic_write(
        store.pointers_dir / f"{digest}{suffix}.json", strict_json.canonical_file_bytes(payload)
    )


def populate_dry_run_state(store: sessions.SessionStore) -> None:
    """The golden dry-run state: the golden records + the three pointer shapes."""

    inputs = golden_inputs()
    # The managed-unchanged session was resumed and is still running.
    inputs["managed-unchanged"]["last_event_source"] = "resume"
    for raw in inputs.values():
        _write_raw(store, raw)
    # merge: /project/a has a managed and an ordinary pointer (the newer wins).
    _write_pointer(store, "/project/a", M1)
    _write_pointer(store, "/project/a", M8, sessions.SESSION_TYPE_ORDINARY)
    # rename-ordinary: only an ordinary pointer.
    _write_pointer(store, "/project/b", M2, sessions.SESSION_TYPE_ORDINARY)
    # keep: a 2.x managed pointer (rewritten without session_type).
    _write_pointer(store, "/project/c", M3)


def _transcript_stub(view: dict) -> tuple[str, float | None]:
    if view["managed_id"] == M5:
        return ("missing", None)
    if view["managed_id"] == M6:
        return ("present", migrate._parse_time(NOW) - 45 * 86400)
    return ("present", migrate._parse_time(NOW) - 86400)


def _dry_run_golden_files() -> dict[str, bytes]:
    """``render_text``/``render_json`` of ``plan`` over the golden dry-run state."""

    root = Path(tempfile.mkdtemp(prefix="claude-multi-migrate-golden-"))
    try:
        os.chmod(root, 0o700)
        store = sessions.SessionStore(root / "state", SCHEMA)
        populate_dry_run_state(store)
        report = migrate.plan(
            store, LCAT, now=NOW, profile_exists=lambda name: True,
            transcript_check=_transcript_stub, cleanup_days=30,
        )
        report = dataclasses.replace(report, state_root="/state")
        return {
            "dry-run.txt": migrate.render_text(report).encode("utf-8"),
            "dry-run.json": migrate.render_json(report),
        }
    finally:
        shutil.rmtree(root, True)


def dry_run_golden_files() -> dict[str, bytes]:
    with legacy_rows():
        return _dry_run_golden_files()


RESTORE_TOKEN = "f1111111-1111-4111-8111-111111111111"
LATER = "2026-09-26T00:00:00Z"


def _restore_golden_files() -> dict[str, bytes]:
    """``restore_overlay`` after a scripted migrate -> lifecycle sequence."""

    cases = {"managed": v3_managed(M1), "ordinary": v3_ordinary(M2), "legacy-v1": v1(M4)}
    files = {}
    for name, backup in cases.items():
        current = migrate.convert_record(backup, cat=LCAT, now=NOW).record
        # A resume that retargets the runtime id (alias appended) ...
        current = sessions.reconcile_runtime_record(
            current, observed_runtime_id=RUNTIME, source="resume",
            launch_epoch=current["launch_epoch"], now=LATER,
        )
        # ... a 3.0 resolve-fork epoch bump, and a relink --cwd.
        current["launch_epoch"] += 1
        current["cwd"] = "/project/moved"
        restored = sessions.restore_overlay(backup, current)
        restored["mutation_token"] = RESTORE_TOKEN
        files[f"{name}.json"] = strict_json.pretty_file_bytes(restored)
    return files


def restore_golden_files() -> dict[str, bytes]:
    with legacy_rows():
        return _restore_golden_files()


def _cli_environ(tmp: Path) -> dict[str, str]:
    home = tmp / "home"
    token_dir = state.ensure_private_dir(home / ".config" / "claude-multi")
    state.atomic_write(token_dir / "api-key", (FIXTURE_GATEWAY_TOKEN + "\n").encode("ascii"))
    return {
        "HOME": str(home),
        "XDG_STATE_HOME": str(tmp / "xdg-state"),
        "XDG_CONFIG_HOME": str(tmp / "xdg-config"),
        "XDG_DATA_HOME": str(tmp / "xdg-data"),
        "TERM": "dumb",
    }


class MigrateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-migrate-"))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.environ = _cli_environ(self.tmp)
        self.root = sessions.state_root(self.environ)
        self.store = sessions.SessionStore(self.root, SCHEMA)
        self.hook_command = "/nix/store/fixture/bin/claude-multi"
        self.shim = scope.ensure_hook_shim(self.root, self.hook_command)

    def write(self, record: dict) -> None:
        _write_raw(self.store, record)

    def raw_bytes(self, managed: str) -> bytes:
        return (self.store.sessions_dir / f"{managed}.json").read_bytes()

    def run_migrate(self, cat=LCAT, **kwargs) -> migrate.MigrationReport:
        return migrate.run(
            self.store, cat, hook_command=self.hook_command, hook_shim_path=self.shim,
            now=NOW, **kwargs,
        )

    def tree(self) -> dict[str, tuple]:
        return {
            str(path.relative_to(self.tmp)): (
                path.is_dir(),
                None if path.is_dir() else path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in sorted(self.tmp.rglob("*"))
        }


# ---------------------------------------------------------------- key rules


class TranslateKeyTests(unittest.TestCase):
    def cat(self) -> profile.LineupCatalog:
        return _local_cat(retired={
            "opus@4.8": _retired_entry("opus", since=33, wire="claude-opus-old-a",
                                       selectors={"claude-multi-opus-old-a[1m]": None},
                                       generation="4.8"),
            "opus@5": _retired_entry("opus", since=34, wire="claude-opus-old-b",
                                     selectors={"claude-multi-opus-old-b[1m]": None},
                                     generation="5"),
        })

    def test_first_retirement_after_the_record_catalog_wins(self) -> None:
        cat = self.cat()
        self.assertEqual(migrate.translate_key("opus", None, 32, cat), "opus@4.8")
        self.assertEqual(migrate.translate_key("opus", None, 33, cat), "opus@5")
        self.assertEqual(migrate.translate_key("opus", None, 34, cat), "opus")

    def test_a_retired_selector_wins_over_the_key_rule(self) -> None:
        cat = self.cat()
        self.assertEqual(
            migrate.translate_key("opus", "claude-multi-opus-old-b", 32, cat), "opus@5"
        )
        self.assertEqual(migrate.translate_key("muse-spark", MUSE_HIGH, 20, LCAT), "muse-spark")
        # A live selector is no retired hit: the key rule applies.
        self.assertEqual(migrate.translate_key("sol", SOL_HIGH, 20, LCAT), "sol")

    def test_two_keys_sharing_the_first_since_catalog_are_ambiguous(self) -> None:
        cat = _local_cat(retired={
            "opus@a": _retired_entry("opus", since=33, wire="w-a", selectors={"s-a": None}),
            "opus@b": _retired_entry("opus", since=33, wire="w-b", selectors={"s-b": None}),
        })
        with self.assertRaisesRegex(migrate.MigrationError, "share since_catalog 33"):
            migrate.translate_key("opus", None, 32, cat)
        raw = v3_managed(lead={"model": "opus", "client_selector": "claude-multi-opus-4-8[1m]"},
                         catalog_version=32)
        with self.assertRaises(migrate.MigrationError):
            migrate.convert_record(raw, cat=cat, now=NOW)

    def test_public_without_catalog_obj_and_on_a_catalog(self) -> None:
        # Callable with selector=None and without catalog_obj,
        # on a LineupCatalog or a Catalog.
        self.assertEqual(migrate.translate_key("sol", None, 31, LCAT), "sol")
        self.assertEqual(migrate.translate_key("sol", None, 31, BUNDLE), "sol")
        self.assertEqual(migrate.translate_key("unknown-x", None, 31, LCAT), "unknown-x")


# ---------------------------------------------------------------- convert


class ConvertRecordTests(unittest.TestCase):
    def convert(self, raw: dict, cat=LCAT) -> migrate.RecordOutcome:
        outcome = migrate.convert_record(raw, cat=cat, now=NOW)
        self.assertEqual(validate.validate(outcome.record, SCHEMA, "$"), [])
        sessions._validate_record_invariants(outcome.record, "test")
        return outcome

    def test_managed_live_lead_is_unchanged_with_the_mechanical_agents(self) -> None:
        outcome = self.convert(v3_managed())
        record = outcome.record
        self.assertEqual(outcome.outcome, "unchanged")
        self.assertEqual(record["version"], 4)
        self.assertEqual(record["applied"]["lead"]["key"], "opus55")
        self.assertEqual(record["applied"]["lead"]["selector"], LEAD_SELECTOR)
        self.assertEqual(
            outcome.agents,
            ("cm-explorer", "cm-analyst", "cm-implementer", "cm-reviewer", "cm-reviewer-strong"),
        )
        agents = record["applied"]["agents"]
        self.assertEqual(agents["cm-analyst"]["selector"], SOL_HIGH)
        self.assertEqual(agents["cm-reviewer"]["effort"], "xhigh")
        # the other-family reviewer variant becomes -strong
        self.assertEqual(agents["cm-reviewer-strong"]["key"], "opus55")
        self.assertEqual(agents["cm-explorer"], agents["cm-analyst"])
        self.assertEqual(record["lineup_generation"], 0)
        self.assertIs(record["follow"], False)
        self.assertEqual(record["profile"], "balanced")
        self.assertEqual(outcome.profile_reason, "seed")
        self.assertEqual(record["lead_class"], "large")
        self.assertEqual(
            record["migration"],
            {"from_version": 3, "migrated_at": NOW, "outcome": "unchanged",
             "composition_name": "opus-sol", "session_type": sessions.SESSION_TYPE_MANAGED},
        )
        self.assertEqual(record["migrated_from_version"], 3)
        self.assertEqual(record["applied"]["settings"], migrate.default_settings_snapshot(LCAT))
        self.assertNotIn("launch_fence", record)
        self.assertNotIn("scope_lead", record)

    def test_retired_null_lead_needs_a_choice(self) -> None:
        outcome = self.convert(golden_inputs()["managed-needs-choice"])
        self.assertEqual(outcome.outcome, "needs-choice")
        self.assertIsNone(outcome.record["lead_class"])
        self.assertIsNone(outcome.successor)
        self.assertEqual(outcome.record["applied"]["lead"]["key"], "muse-spark")
        self.assertEqual(outcome.record["profile"], None)  # kimi-sol is a dropped preset
        self.assertEqual(outcome.profile_reason, "dropped")

    def test_renamed_key_same_wire(self) -> None:
        cat = _local_cat(retired={"opusx": _retired_entry(
            "opus", since=32, wire=LCAT.lines["opus"]["wire_model"],
            selectors={"claude-multi-opusx[1m]": None})})
        outcome = self.convert(
            v3_managed(lead={"model": "opusx", "client_selector": "claude-multi-opusx[1m]"}),
            cat,
        )
        self.assertEqual(outcome.outcome, "renamed")
        self.assertEqual(outcome.v4_lead, "opusx")
        self.assertEqual(outcome.successor, "opus")
        self.assertEqual(outcome.record["applied"]["lead"]["key"], "opusx")
        self.assertEqual(outcome.record["lead_class"], "large")

    def _generation_cat(self) -> profile.LineupCatalog:
        return _local_cat(retired={"opus@4.8": _retired_entry(
            "opus", since=33, wire="claude-opus-old", generation="4.8",
            selectors={"claude-multi-opus-old[1m]": None})})

    def test_successor_through_the_selector(self) -> None:
        raw = v3_managed(lead={"model": "opus", "client_selector": "claude-multi-opus-old[1m]"},
                         catalog_version=32)
        outcome = self.convert(raw, self._generation_cat())
        self.assertEqual(outcome.outcome, "successor")
        self.assertEqual(outcome.record["applied"]["lead"]["key"], "opus@4.8")
        self.assertEqual(outcome.record["applied"]["lead"]["generation"], "4.8")
        self.assertEqual(outcome.successor, "opus")

    def test_successor_through_the_at_rule_and_unchanged_after_it(self) -> None:
        raw = v3_managed(lead={"model": "opus", "client_selector": "claude-multi-opus-4-8[1m]"},
                         catalog_version=32)
        self.assertEqual(self.convert(raw, self._generation_cat()).v4_lead, "opus@4.8")
        raw["catalog_version"] = 33
        outcome = self.convert(raw, self._generation_cat())
        self.assertEqual(outcome.v4_lead, "opus")
        self.assertEqual(outcome.outcome, "unchanged")

    def test_preferred_analyst_removed_falls_back_with_a_note(self) -> None:
        outcome = self.convert(golden_inputs()["managed-fallback"])
        agents = outcome.record["applied"]["agents"]
        self.assertEqual(agents["cm-analyst"]["key"], "kimi-k3")
        self.assertIn("analyst: preferred muse-spark removed → kimi-k3·max", outcome.notes)
        # lane "weird" -> effort high + note
        self.assertEqual(agents["cm-reviewer"]["effort"], "high")
        self.assertIn("cm-reviewer: lane 'weird' → effort high", outcome.notes)
        self.assertEqual(agents["cm-reviewer-strong"]["key"], "opus5")

    def test_every_analyst_removed_downgrades_explore(self) -> None:
        outcome = self.convert(golden_inputs()["managed-explore-downgrade"])
        applied = outcome.record["applied"]
        self.assertNotIn("cm-analyst", applied["agents"])
        self.assertNotIn("cm-explorer", applied["agents"])
        self.assertEqual(applied["native_agents"]["explore"], "native")
        self.assertIn("analyst: 'muse-spark' was removed — unbound", outcome.notes)
        self.assertIn(
            "Explore: replace → native (no analyst left to back cm-explorer)", outcome.notes
        )
        # composition name not on disk -> null (missing)
        self.assertIsNone(outcome.profile)
        self.assertEqual(outcome.profile_reason, "missing")

    def test_same_family_reviewers_give_no_strong(self) -> None:
        raw = v3_managed(variants=[
            _variant("cm-reviewer", "sol", "xhigh", "gpt-multi-sol-xhigh[1m]", True),
            _variant("cm-reviewer", "sol", "high", SOL_HIGH, False),
        ], native={"explore": "native", "plan": "native", "general_purpose": "off"})
        outcome = self.convert(raw)
        self.assertEqual(outcome.agents, ("cm-reviewer",))

    def test_lead_providers_availability_e18_and_every_enabled(self) -> None:
        # Availability without the lead's provider: added, with a note (E18).
        outcome = self.convert(v3_managed(availability={"openai": "lead+agents", "kimi": "agents"}))
        self.assertEqual(outcome.record["applied"]["lead_providers"], ["anthropic", "openai"])
        self.assertIn("lead_providers: added anthropic (the lead's provider, E18)", outcome.notes)
        # Every provider enabled in the default Settings snapshot -> null.
        everything = {provider: "lead+agents" for provider in LCAT.providers}
        outcome = self.convert(v3_managed(availability=everything))
        self.assertIsNone(outcome.record["applied"]["lead_providers"])

    def test_snapshot_workflows_win_over_the_record(self) -> None:
        outcome = self.convert(v3_managed(snapshot_workflows="off", workflows="native"))
        self.assertEqual(outcome.record["applied"]["workflows"], "off")
        outcome = self.convert(v3_managed(workflows="off"))
        self.assertEqual(outcome.record["applied"]["workflows"], "off")

    def test_ordinary_records(self) -> None:
        for model, context_profile in (("qwen38", None), ("sol", "large")):
            with self.subTest(model=model):
                outcome = self.convert(v3_ordinary(model=model, context_profile=context_profile))
                record = outcome.record
                self.assertIsNone(record["profile"])
                self.assertEqual(outcome.profile_reason, "ordinary")
                self.assertIsNone(record["applied"]["lead"]["selector"])
                self.assertEqual(record["applied"]["lead"]["effort"], "ultracode")
                self.assertEqual(record["applied"]["agents"], {})
                self.assertEqual(record["applied"]["native_agents"], migrate.ORDINARY_NATIVE)
                self.assertEqual(
                    record["lead_class"], LCAT.lines[model]["context"]["ordinary_profile"]
                )
                self.assertEqual(record["migration"]["session_type"],
                                 sessions.SESSION_TYPE_ORDINARY)

    def test_ordinary_custom_and_unknown_custom(self) -> None:
        custom_line = {
            **copy.deepcopy(LCAT.lines["qwen-flash-next"]),
            "generation": "custom", "family": "custom",
            "context": {**LCAT.lines["qwen-flash-next"]["context"], "ordinary_profile": "custom-1"},
        }
        cat = _local_cat(lines={"my-model": custom_line})
        outcome = self.convert(v3_ordinary(model="my-model", context_profile="custom-1"), cat)
        self.assertEqual(outcome.outcome, "unchanged")
        self.assertEqual(outcome.record["lead_class"], "custom-1")
        outcome = self.convert(v3_ordinary(model="gone-model", context_profile="custom-2"))
        self.assertEqual(outcome.outcome, "needs-choice")
        self.assertIsNone(outcome.record["lead_class"])
        self.assertIn(
            f"lead gone-model: unknown to catalog {LCAT.catalog_version} (not a custom model either)",
            outcome.notes,
        )

    def test_ordinary_no_subagents(self) -> None:
        outcome = self.convert(v3_ordinary(no_subagents=True))
        self.assertIs(outcome.record["applied"]["no_subagents"], True)

    def test_composition_names_to_profiles(self) -> None:
        for name, profile_name, reason in (
            ("default", None, "default"),
            ("not-on-disk", None, "missing"),
            ("glm-sol", None, "dropped"),
            ("fable", "max", "seed"),
            ("muse", "muse", "convert"),
        ):
            with self.subTest(name=name):
                outcome = self.convert(v3_managed(composition_name=name))
                self.assertEqual(outcome.record["profile"], profile_name)
                self.assertEqual(outcome.profile_reason, reason)
                self.assertEqual(outcome.record["migration"]["composition_name"], name)

    def test_legacy_v1_and_v2(self) -> None:
        for raw, version in ((v1(), 1), (v2(), 2)):
            with self.subTest(version=version):
                outcome = self.convert(raw)
                self.assertEqual(outcome.record["migrated_from_version"], version)
                self.assertEqual(outcome.record["migration"]["from_version"], version)
                self.assertEqual(outcome.record["managed_id"], raw["session_id"])

    def test_lifecycle_and_provenance_are_preserved(self) -> None:
        raw = v3_managed(
            identity_state=sessions.IDENTITY_REPAIR_NEEDED,
            runtime_session_id=RUNTIME,
            runtime_aliases=[{"session_id": ALIAS, "source": "clear", "observed_at": NOW}],
            pending_forks=[{"session_id": FORK, "observed_at": NOW}],
            observed_model="gpt-multi-sol-high[1m]",
            observed_cwd="/elsewhere",
            forked_from=M8,
            last_end_reason="logout",
            launcher_version="2.25.1",
        )
        outcome = self.convert(raw)
        record = outcome.record
        for key in ("runtime_session_id", "runtime_aliases", "pending_forks", "observed_model",
                    "observed_cwd", "forked_from", "last_end_reason", "launcher_version",
                    "identity_state", "launch_epoch", "created_at", "catalog_version",
                    "catalog_hash", "last_seen_at", "last_event_source", "mutation_token"):
            self.assertEqual(record[key], raw[key], key)
        self.assertEqual(
            sessions.may_hold_env_token(raw), sessions.may_hold_env_token(record)
        )


# -------------------------------------------------------------- the table


class MigrationMapTests(unittest.TestCase):
    def test_no_reviewed_rows_ship(self) -> None:
        # The shipped table names no composition: every legacy composition
        # follows the generic rule.
        self.assertEqual(dict(SHIPPED_TARGETS), {})
        self.assertEqual(dict(SHIPPED_RULES), {})

    def test_generic_row_and_rule(self) -> None:
        seeds = catalog.SEED_PROFILE_NAMES
        # A shipped profile's name is never a mapping: a profile of its own.
        self.assertEqual(migration_map.generic_row(seeds[0], seeds), ("convert", f"{seeds[0]}-legacy"))
        self.assertEqual(migration_map.generic_row(seeds[0], seeds, {f"{seeds[0]}-legacy"}),
                         ("convert", f"{seeds[0]}-legacy-2"))
        self.assertEqual(migration_map.generic_row("team-of-mine", seeds), ("convert", "team-of-mine"))
        rule = migration_map.generic_rule("team-of-mine")
        self.assertEqual((rule.source, rule.members, rule.rule, rule.lead, dict(rule.overrides),
                          dict(rule.native), rule.lead_providers, rule.expected_warnings),
                         ("team-of-mine", ("team-of-mine",), "mechanical", None, {}, {},
                          "availability", ()))
        self.assertIn(rule.rule, migration_map.RULES)

    def test_test_local_rows_are_consistent(self) -> None:
        rules = migration_map.CONVERT_RULES
        self.assertEqual(set(rules), migration_map.convert_targets())
        seeds = set(catalog.SEED_PROFILE_NAMES)
        for name, (kind, target) in migration_map.PRESET_TARGETS.items():
            self.assertIn(kind, migration_map.KINDS)
            self.assertEqual(target is None, kind == "dropped", name)
            if kind == "convert":
                self.assertNotIn(target, seeds, name)
                self.assertIn(name, rules[target].members)
            elif kind == "seed":
                self.assertIn(target, seeds, name)

    def test_record_profile(self) -> None:
        self.assertEqual(migration_map.record_profile(None), (None, "ordinary"))
        self.assertEqual(migration_map.record_profile("default"), (None, "default"))
        self.assertEqual(migration_map.record_profile("nope"), (None, "missing"))
        self.assertEqual(migration_map.record_profile("kimi-sol"), (None, "dropped"))
        self.assertEqual(migration_map.record_profile("opus-sol"), ("balanced", "seed"))
        self.assertEqual(migration_map.record_profile("muse"), ("muse", "convert"))
        # Without reviewed rows a named composition keeps its own lineup, pinned.
        with mock.patch.object(migration_map, "PRESET_TARGETS", SHIPPED_TARGETS):
            self.assertEqual(migration_map.record_profile("opus-sol"), (None, "missing"))


# ---------------------------------------------------------------- run


class RunTests(MigrateTestCase):
    def test_marker_is_written_before_the_first_v4_save(self) -> None:
        self.write(v3_managed())
        events: list[str] = []
        real_marker, real_save = sessions.write_state_marker, sessions.SessionStore.save

        def marker(root):
            events.append("marker")
            return real_marker(root)

        def save(store, record):
            events.append(f"save v{record['version']}")
            return real_save(store, record)

        with mock.patch.object(sessions, "write_state_marker", side_effect=marker), \
                mock.patch.object(sessions.SessionStore, "save", autospec=True, side_effect=save), \
                mock.patch.object(sessions, "ensure_state_v4",
                                  side_effect=AssertionError("run never calls ensure_state_v4")):
            report = self.run_migrate()
        self.assertEqual(events, ["marker", "save v4"])
        self.assertTrue(report.ok)
        self.assertEqual(sessions.check_state_marker(self.root), 4)

    def test_run_finishes_under_a_real_lock_without_deadlocking(self) -> None:
        for managed in (M1, M5, M6):
            self.write(v3_managed(managed))
        result: dict = {}
        worker = threading.Thread(target=lambda: result.update(report=self.run_migrate()))
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "migrate.run deadlocked on its own lock")
        self.assertEqual({r.status for r in result["report"].records}, {"migrate"})

    def test_shim_that_does_not_point_at_this_launcher_refuses(self) -> None:
        self.write(v3_managed())
        before = self.tree()
        with self.assertRaisesRegex(migrate.MigrationError, "does not point at this launcher"):
            migrate.run(self.store, LCAT, hook_command="/other/launcher",
                        hook_shim_path=self.shim, now=NOW)
        self.assertEqual(self.tree(), before)
        self.assertIn(shlex.quote(self.hook_command), self.shim.read_text())

    def test_backups_are_byte_equal_and_a_second_run_writes_nothing(self) -> None:
        records = [v3_managed(M1), v3_ordinary(M2), v2(M3), v1(M4)]
        originals = {}
        for raw in records:
            self.write(raw)
            stable = raw.get("managed_id") or raw["session_id"]
            originals[stable] = self.raw_bytes(stable)
        report = self.run_migrate()
        self.assertTrue(report.ok)
        self.assertEqual(report.backups_written, 4)
        for stable, data in originals.items():
            self.assertEqual(self.store.backup_path(stable).read_bytes(), data)
            record = self.store.load(stable)
            self.assertEqual(record["version"], 4)
            self.assertNotEqual(record["mutation_token"], TOKEN)  # Rotated
        before = self.tree()
        again = self.run_migrate()
        self.assertEqual({r.status for r in again.records}, {"already-v4"})
        self.assertEqual(again.backups_written, 0)
        self.assertEqual(
            {k: v for k, v in self.tree().items() if not k.endswith(".lock")},
            {k: v for k, v in before.items() if not k.endswith(".lock")},
        )

    def test_crash_between_backup_and_save_is_rerun(self) -> None:
        self.write(v3_managed(M1))
        original = self.raw_bytes(M1)
        with mock.patch.object(sessions.SessionStore, "save", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.run_migrate()
        self.assertEqual(self.raw_bytes(M1), original)
        self.assertEqual(self.store.backup_path(M1).read_bytes(), original)
        # a 2.x hook updated the still-v3 record: the rerun follows the disk
        hooked = v3_managed(M1, last_event_source="resume", last_seen_at="2026-09-25T01:00:00Z")
        self.write(hooked)
        report = self.run_migrate()
        self.assertTrue(report.ok)
        self.assertEqual(self.store.backup_path(M1).read_bytes(), strict_json.canonical_file_bytes(hooked))
        self.assertEqual(self.store.load(M1)["last_event_source"], "resume")

    def test_unreadable_records_are_reported_and_untouched(self) -> None:
        self.write(v3_managed(M1))
        corrupt = self.store.sessions_dir / f"{M5}.json"
        state.atomic_write(corrupt, b"{not json")
        duplicate = self.store.sessions_dir / f"{M6}.json"
        state.atomic_write(duplicate, b'{"version": 3, "version": 3}')
        wrong_mode = self.store.sessions_dir / f"{M7}.json"
        state.atomic_write(wrong_mode, strict_json.canonical_file_bytes(v3_managed(M7)))
        os.chmod(wrong_mode, 0o644)
        before = {p: p.read_bytes() for p in (corrupt, duplicate, wrong_mode)}
        report = self.run_migrate()
        statuses = {r.managed_id: r.status for r in report.records}
        self.assertEqual(statuses[M1], "migrate")
        for managed in (M5, M6, M7):
            self.assertEqual(statuses[managed], "unreadable")
        self.assertFalse(report.ok)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        code, output = self.cli(["migrate"])
        self.assertEqual(code, 1, output)
        self.assertIn(f"{M5[:8]}  -  unreadable:", output)

    def test_live_during_migration_lists_records_without_an_end(self) -> None:
        self.write(v3_managed(M1, last_event_source="resume"))
        self.write(v3_managed(M5, last_event_source="end"))
        text = migrate.render_text(self.run_migrate())
        self.assertIn("live during migration: 1", text)
        self.assertIn(f"  {M1[:8]}  last event resume at {NOW}: verify with claude-multi "
                      f"sessions show {M1}", text)
        self.assertNotIn(f"  {M5[:8]}  last event", text)

    def test_pointer_merge_shapes_and_backups(self) -> None:
        self.write(v3_managed(M1, last_seen_at="2026-09-20T00:00:00Z"))
        self.write(v3_ordinary(M2, cwd="/project/a", last_seen_at="2026-09-21T00:00:00Z"))
        self.write(v3_ordinary(M8, cwd="/project/b"))
        self.write(v2(M3))
        _write_pointer(self.store, "/project/a", M1)
        _write_pointer(self.store, "/project/a", M2, sessions.SESSION_TYPE_ORDINARY)
        _write_pointer(self.store, "/project/b", M8, sessions.SESSION_TYPE_ORDINARY)
        _write_pointer(self.store, "/project/c", M3)
        originals = {p.name: p.read_bytes() for p in self.store.pointers_dir.glob("*.json")}
        report = self.run_migrate()
        actions = {item.cwd: item for item in report.pointers}
        self.assertEqual(actions["/project/a"].action, "merge")
        self.assertEqual(actions["/project/a"].kept, M2)  # newer last_seen_at
        self.assertEqual(actions["/project/a"].dropped, M1)
        self.assertEqual(actions["/project/b"].action, "rename-ordinary")
        self.assertEqual(actions["/project/c"].action, "keep")
        self.assertEqual(report.pointer_backups_written, 4)
        for name, data in originals.items():
            self.assertEqual((self.store.pointers_dir / (name + ".v3")).read_bytes(), data)
        self.assertEqual(list(self.store.pointers_dir.glob("*.ordinary.json")), [])
        for cwd, managed in (("/project/a", M2), ("/project/b", M8), ("/project/c", M3)):
            payload = strict_json.loads(self.store._pointer_path(cwd).read_bytes())
            self.assertEqual(payload, {"cwd": cwd, "session_id": managed})
            self.assertEqual(self.store.last(cwd), managed)
        # a second run: nothing left to merge, no new backups
        again = self.run_migrate()
        self.assertEqual({item.action for item in again.pointers}, {"already-merged"})
        self.assertEqual(again.pointer_backups_written, 0)

    def test_unreadable_pointer_is_left_and_exits_2(self) -> None:
        self.write(v3_managed(M1))
        digest = strict_json.sha256_hex(b"/project/z")[:32]
        broken = self.store.pointers_dir / f"{digest}.json"
        state.atomic_write(broken, b"nope")
        report = self.run_migrate()
        self.assertEqual([item.action for item in report.pointers], ["unreadable"])
        self.assertFalse(report.ok)
        self.assertEqual(broken.read_bytes(), b"nope")

    def test_blocked_convert_target_refuses_before_writing_anything(self) -> None:
        # A convert target whose profile file
        # does not exist yet (`profile migrate --apply` writes it).
        self.write(v3_managed(M1, composition_name="muse"))
        self.write(v3_managed(M5))
        before = self.tree()
        with self.assertRaisesRegex(
            migrate.MigrationError, r"profile migrate --apply first.*Nothing was changed"
        ):
            self.run_migrate(profile_exists=lambda name: name != "muse")
        self.assertEqual(self.tree(), before)
        report = migrate.plan(self.store, LCAT, now=NOW, profile_exists=lambda n: n != "muse")
        statuses = {r.managed_id: r.status for r in report.records}
        self.assertEqual(statuses, {M1: "blocked", M5: "migrate"})
        text = migrate.render_text(report)
        self.assertIn("1 blocked)", text)
        self.assertIn("blocked: profile 'muse' (convert target of legacy composition 'muse') "
                      "does not exist yet; run claude-multi profile migrate --apply first", text)
        report = self.run_migrate(profile_exists=lambda name: True)
        self.assertTrue(report.ok)
        self.assertEqual(self.store.load(M1)["profile"], "muse")

    def cli(self, argv: list[str]) -> tuple[int, str]:
        runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.environ, cwd=self.tmp)
        output = io.StringIO()
        code = cli.main(argv, runtime=runtime, output_stream=output, interactive=False)
        return code, output.getvalue()


# ---------------------------------------------------------------- dry run


class DryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-migrate-dry-"))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.environ = _cli_environ(self.tmp)
        self.root = sessions.state_root(self.environ)
        self.store = sessions.SessionStore(self.root, SCHEMA)
        populate_dry_run_state(self.store)

    def snapshot(self) -> dict:
        """Every file under the temp tree, plus every directory of the state root.

        (The config root's empty ``compositions/`` directory is created by
        every Runtime; it holds no file.)
        """

        return {
            str(path.relative_to(self.tmp)): (
                path.is_dir(), None if path.is_dir() else path.read_bytes(),
                stat.S_IMODE(path.lstat().st_mode), path.lstat().st_mtime_ns,
            )
            for path in sorted(self.tmp.rglob("*"))
            if not path.is_dir() or self.root in (path, *path.parents)
        }

    def test_plan_writes_nothing_and_takes_no_lock(self) -> None:
        before = self.snapshot()
        with mock.patch.object(state.FileLock, "acquire",
                               side_effect=AssertionError("plan takes no lock")):
            report = migrate.plan(self.store, LCAT, now=NOW)
        self.assertTrue(report.dry_run)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "locks").exists())
        self.assertEqual(report.marker_before, 3)
        self.assertEqual(report.backups_written, 8)
        self.assertIn("4 (would write)", migrate.render_text(report))

    def test_cli_dry_run_builds_a_read_only_runtime_and_writes_nothing(self) -> None:
        before = self.snapshot()
        captured: list[dict] = []
        real = cli.Runtime

        def runtime_factory(**kwargs):
            captured.append(kwargs)
            return real(**{**kwargs, "asset_root": FIXTURE_ROOT, "environ": self.environ,
                           "cwd": self.tmp})

        fail = AssertionError("dry run refreshed a shim")
        output = io.StringIO()
        with mock.patch('claude_multi.cli.runtime.Runtime', side_effect=runtime_factory), \
                mock.patch.object(claude_multi.sessions, "state_root", return_value=self.root), \
                mock.patch.object(scope, "ensure_hook_shim", side_effect=fail), \
                mock.patch.object(scope, "ensure_token_helper_command", side_effect=fail), \
                mock.patch.object(scope, "ensure_hook_shim_v3", side_effect=fail), \
                mock.patch.dict(os.environ, self.environ, clear=False):
            code = cli.main(["migrate", "--dry-run"], output_stream=output, interactive=False)
        # astra-muse (a convert target) has no profile file yet: blocked,
        # so the dry run exits 1 while writing nothing.
        self.assertEqual(code, 1, output.getvalue())
        self.assertIn("1 blocked)", output.getvalue())
        self.assertEqual(len(captured), 1)
        self.assertIs(captured[0]["allow_state_writes"], False)
        self.assertIs(captured[0]["refresh_shims"], False)
        self.assertEqual(self.snapshot(), before)
        text = output.getvalue()
        self.assertTrue(text.startswith(f"claude-multi migrate --dry-run: state {self.root}\n"))
        # The fixture HOME has no transcripts at all
        self.assertIn("transcripts: 8 record(s) whose transcript is missing", text)
        self.assertIn(f"transcript missing: claude-multi sessions forget {M1}", text)

    def test_json_output(self) -> None:
        report = migrate.plan(self.store, LCAT, now=NOW)
        document = strict_json.loads(migrate.render_json(report))
        self.assertEqual(document["marker_before"], 3)
        self.assertTrue(document["dry_run"])
        self.assertEqual(len(document["records"]), 8)
        self.assertNotIn("record", document["records"][0])
        self.assertEqual(sum(v["managed"] + v["ordinary"] for v in document["summary"].values()), 8)

    def test_goldens(self) -> None:
        for name, data in dry_run_golden_files().items():
            with self.subTest(name=name):
                assertGolden(self, GOLDENS / name, data)


class TranscriptNoteTests(unittest.TestCase):
    def test_missing_and_stale_by_path(self) -> None:
        views = [{"managed_id": M1}, {"managed_id": M5}, {"managed_id": M6}]
        notes = migrate._transcript_notes(views, _transcript_stub, 30, NOW)
        self.assertEqual(
            notes,
            (migrate.TranscriptNote(M5, "missing", None), migrate.TranscriptNote(M6, "stale", 45)),
        )
        self.assertEqual(migrate.cleanup_period_days({"cleanupPeriodDays": 7}), 7)
        self.assertEqual(migrate.cleanup_period_days({}), 30)
        self.assertEqual(migrate.cleanup_period_days(None), 30)

    def test_transcript_presence_checks_the_path_only(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="claude-multi-transcript-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        environ = _cli_environ(tmp)
        runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ=environ, cwd=tmp)
        view = sessions._lifecycle_view(v3_managed(M1, cwd="/project/a"))
        self.assertEqual(cli._transcript_presence(runtime, view), ("missing", None))
        folder = Path(environ["HOME"]) / ".claude" / "projects" / cli._native_project_slug("/project/a")
        folder.mkdir(parents=True)
        transcript = folder / f"{M1}.jsonl"
        transcript.write_bytes(b"")
        os.utime(transcript, (1_000_000, 1_000_000))
        self.assertEqual(cli._transcript_presence(runtime, view), ("present", 1_000_000.0))


class PrintLaunchTests(unittest.TestCase):
    def test_print_launch_builds_a_runtime_that_refreshes_no_shim(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="claude-multi-print-launch-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        environ = _cli_environ(tmp)
        captured: list[dict] = []

        class _Stop(Exception):
            pass

        def runtime_factory(**kwargs):
            captured.append(kwargs)
            raise _Stop

        with mock.patch('claude_multi.cli.runtime.Runtime', side_effect=runtime_factory), \
                mock.patch.object(claude_multi.sessions, "state_root",
                                  return_value=sessions.state_root(environ)), \
                mock.patch.dict(os.environ, environ):
            with self.assertRaises(_Stop):
                cli.main(["--print-launch", "--composition", "default"],
                         output_stream=io.StringIO(), interactive=False)
        self.assertIs(captured[0]["refresh_shims"], False)
        self.assertIs(captured[0]["allow_state_writes"], False)
        self.assertIs(captured[0]["initialize_session_store"], False)
        # Such a Runtime creates neither shims nor the session store.
        root = sessions.state_root(environ)
        cli.Runtime(**{**captured[0], "asset_root": FIXTURE_ROOT}, environ=environ, cwd=tmp)
        self.assertFalse(root.exists())
        runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ=environ, cwd=tmp)
        self.assertTrue(scope.hook_shim_path(root).exists())
        self.assertTrue(Path(runtime.hook3_command).exists())

    def test_lineup_catalog_is_the_merged_view(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="claude-multi-lineup-cat-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ=_cli_environ(tmp), cwd=tmp)
        lcat = runtime.lineup_catalog()
        self.assertIsInstance(lcat, profile.LineupCatalog)
        self.assertEqual(set(lcat.lines), set(profile.LineupCatalog.from_docs(runtime.ordinary_docs).lines))


# ---------------------------------------------------------------- migrate_one


class MigrateOneTests(MigrateTestCase):
    def test_migrates_one_record_and_writes_the_marker(self) -> None:
        self.write(v3_managed(M1))
        self.write(v3_managed(M5))
        outcome = migrate.migrate_one(
            self.store, LCAT, M1, hook_command=self.hook_command, hook_shim_path=self.shim,
            now=NOW,
        )
        self.assertEqual(outcome.status, "migrate")
        self.assertEqual(sessions.check_state_marker(self.root), 4)
        self.assertEqual(self.store.load(M1)["version"], 4)
        self.assertEqual(self.store.load_raw(M5)[0]["version"], 3)
        self.assertTrue(self.store.backup_path(M1).exists())
        again = migrate.migrate_one(
            self.store, LCAT, M1, hook_command=self.hook_command, hook_shim_path=self.shim,
        )
        self.assertEqual(again.status, "already-v4")

    def test_busy_migration_lock_refuses_with_nothing_written(self) -> None:
        self.write(v3_managed(M1))
        holder = sessions.migration_lock(self.root)
        self.assertTrue(holder.acquire(blocking=False))
        self.addCleanup(holder.release)
        clock = [0.0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        before = self.tree()
        with self.assertRaises(sessions.MigrationBusyError):
            migrate.migrate_one(
                self.store, LCAT, M1, hook_command=self.hook_command,
                hook_shim_path=self.shim, clock=lambda: clock[0], sleep=sleep,
            )
        self.assertGreaterEqual(clock[0], 3.0)
        self.assertEqual(self.tree(), before)

    def test_blocked_record_raises(self) -> None:
        self.write(v3_managed(M1, composition_name="muse"))
        with self.assertRaisesRegex(migrate.MigrationError, "profile migrate --apply"):
            migrate.migrate_one(
                self.store, LCAT, M1, hook_command=self.hook_command,
                hook_shim_path=self.shim, profile_exists=lambda name: False,
            )
        self.assertFalse((self.root / sessions.STATE_MARKER).exists())


# ---------------------------------------------------------------- doctor


class DoctorMarkerTests(MigrateTestCase):
    """Marker health and a doctor run over migrated v4 records."""

    def runtime(self) -> cli.Runtime:
        runtime = cli.Runtime(
            asset_root=FIXTURE_ROOT, environ=self.environ, cwd=self.tmp,
            doctor_callback=lambda _runtime: [],
            doctor_binary_callback=lambda _contract: ([], []),
            doctor_daemon_callback=lambda: claude_multi.launch.DaemonStatus(
                state="absent", summary="fixture daemon absent"
            ),
        )
        # the Runtime refreshed the 2.x shim to its own launcher
        self.hook_command = runtime.resolved_hook_command
        return runtime

    def test_marker_lines_before_and_after_migration(self) -> None:
        self.write(v3_managed(M1))
        self.write(v3_ordinary(M2))
        problems, info, attention = cli._collect_doctor_reports(self.runtime())
        self.assertIn(
            "state not migrated yet: 2 legacy record(s); run claude-multi migrate --dry-run, "
            "then claude-multi migrate",
            attention,
        )
        self.run_migrate()
        problems, info, attention = cli._collect_doctor_reports(self.runtime())
        self.assertFalse(any("not migrated" in line or "legacy record" in line for line in attention))
        self.assertIn("2 legacy backup(s) kept for claude-multi restore-2x", info)
        # A v4 record's line is its record_summary.
        self.assertTrue(
            any(line.startswith(f"Session {M1}") and "lineup generation 0" in line for line in info),
            info,
        )
        self.assertIn("Sessions: 2 recorded.", info)
        self.write(v3_managed(M5))
        _problems, _info, attention = cli._collect_doctor_reports(self.runtime())
        self.assertIn("1 legacy record(s) (version ≤ 3) remain: rerun claude-multi migrate",
                      attention)

    def test_possibly_missed_hooks_and_lock_and_rollback_dirs(self) -> None:
        self.write(v3_managed(M1, last_event_source="resume", last_seen_at="2026-09-24T00:00:00Z"))
        self.write(v3_managed(M5, last_event_source="end"))
        self.run_migrate()
        state.ensure_private_dir(self.root / "restored-2x")
        holder = sessions.migration_lock(self.root)
        self.assertTrue(holder.acquire(blocking=False))
        try:
            _problems, info, attention = cli._collect_doctor_reports(self.runtime())
        finally:
            holder.release()
        self.assertIn(
            f"session {M1[:8]} was live while it was migrated and no session event has "
            "reached it since; if it was /clear-ed or resumed then, check its runtime id "
            f"(claude-multi sessions show {M1}; claude-multi sessions relink-runtime {M1} "
            "<runtime-id>)",
            attention,
        )
        self.assertFalse(any(M5[:8] in line and "was live" in line for line in attention))
        self.assertIn("migration or restore in progress (session hooks skip record writes)", info)
        self.assertIn(
            f"{self.root / 'restored-2x'} holds files moved aside by claude-multi restore-2x", info
        )

    def test_newer_marker_blocks_and_counts_uuid_records_only(self) -> None:
        self.write(v3_managed(M1))
        self.run_migrate()
        state.atomic_write(self.root / sessions.STATE_MARKER, b"5\n")
        problems, info, attention = cli._collect_doctor_reports(self.runtime())
        self.assertEqual(problems, [f"state marker: {sessions.StateMarkerError(5)}"])
        self.assertEqual(info, ["Sessions: 1 recorded; not inspected by this launcher."])
        self.assertEqual(attention, [])


# ---------------------------------------------------------------- goldens


class RecordGoldenTests(unittest.TestCase):
    def test_record_goldens(self) -> None:
        for name, data in migrate_golden_files().items():
            with self.subTest(name=name):
                assertGolden(self, GOLDENS / name, data)


# ---------------------------------------------------------------- shipped shape


@uses_shipped_catalog
class ShippedRetiredOutcomeTests(unittest.TestCase):
    """Every shipped retired key migrates to the outcome its retired.json data implies."""

    def test_outcome_class_follows_the_data(self) -> None:
        shipped = catalog.load_catalog(SHIPPED_ROOT)
        lcat = profile.LineupCatalog.from_docs(shipped.docs)
        self.assertTrue(lcat.retired)
        for key, entry in sorted(lcat.retired.items()):
            with self.subTest(key=key):
                selector = sorted(entry["selectors"])[0]
                raw = v3_managed(
                    lead={"model": key.split("@", 1)[0] if "@" in key else key,
                          "client_selector": selector},
                    catalog_version=entry["since_catalog"] - 1,
                    variants=[], native={"explore": "native", "plan": "native",
                                         "general_purpose": "off"},
                )
                outcome = migrate.convert_record(raw, cat=lcat, now=NOW)
                self.assertEqual(outcome.v4_lead, key)
                resolution = lcat.resolve_key(key)
                if resolution.key is None:
                    expected = "needs-choice"
                elif entry["last_wire"] == lcat.lines[resolution.key]["wire_model"]:
                    expected = "renamed"
                else:
                    expected = "successor"
                self.assertEqual(outcome.outcome, expected)
                self.assertEqual(
                    validate.validate(
                        outcome.record,
                        strict_json.load(SHIPPED_ROOT / "schemas" / "session.schema.json"),
                        "$",
                    ),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
