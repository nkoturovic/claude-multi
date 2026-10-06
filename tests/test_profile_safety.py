"""Profile safety: kept copies, the seed install record, seed states, the
per-slot diff, digest-checked saves, load errors and the profile plans.

Fixture catalog only; temporary homes; no gateway, no provider.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_cli  # module import: no test classes re-exported
from claude_multi import catalog, lineup, paths, profile, readiness, sessions, settings, state, strict_json
from claude_multi.setup import model, profiles as setup_profiles

from _catalog import FIXTURE_ROOT

_BUNDLE = catalog.load_catalog(FIXTURE_ROOT)
FIXED = datetime.datetime(2026, 10, 2, 9, 30, 15, tzinfo=datetime.timezone.utc)


def _seeds(**changes) -> dict:
    """The fixture seeds; ``changes`` replaces whole seed documents."""

    seeds = copy.deepcopy(_BUNDLE.seed_profiles)
    seeds.update(changes)
    return seeds


def _newer(name: str = "balanced") -> dict:
    """``name``'s fixture seed at the next seed version, with another description."""

    document = copy.deepcopy(_BUNDLE.seed_profiles[name])
    document["seed"]["version"] += 1
    document["description"] = "a newer shipped version"
    return document


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-profile-safety-"))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.environ = {"HOME": str(self.tmp / "home"), "XDG_STATE_HOME": str(self.tmp / "state")}
        self.store = self.make_store()

    def make_store(self, seeds: dict | None = None) -> profile.ProfileStore:
        return profile.ProfileStore(seeds=seeds or _seeds(), environ=self.environ, clock=lambda: FIXED)

    def write_raw(self, name: str, data: bytes) -> Path:
        state.ensure_private_dir(self.store.root)
        path = self.store.root / f"{name}.json"
        state.atomic_write(path, data)
        return path

    def mine(self, **extra) -> dict:
        document = copy.deepcopy(_BUNDLE.seed_profiles["balanced"])
        document.pop("seed")
        document.update(name="mine", **extra)
        return document


class BackupTests(StoreCase):
    def test_names_mode_clash_and_never_overwritten(self) -> None:
        self.store.save(self.mine())
        original = (self.store.root / "mine.json").read_bytes()
        first = self.store.backup("mine", "removed")
        self.assertEqual(first.name, ".mine.removed-20261002T093015Z.json")
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(first.read_bytes(), original)
        second = self.store.backup("mine", "removed")
        self.assertEqual(second.name, ".mine.removed-20261002T093015Z.1.json")
        self.assertEqual(first.read_bytes(), original)  # never overwritten
        third = self.store.backup("mine", "pre-reseed")
        self.assertEqual(third.name, ".mine.pre-reseed-20261002T093015Z.json")
        with self.assertRaises(ValueError):
            self.store.backup("mine", "other")
        # Kept copies and the install record are not profiles.
        self.assertNotIn(".mine.removed-20261002T093015Z", self.store.names())
        self.assertEqual([n for n in self.store.names() if n.startswith(".")], [])

    def test_an_unloadable_file_is_kept_byte_for_byte(self) -> None:
        self.write_raw("broken", b"{\n  \"version\": 2,\n  oops\n")
        kept = self.store.remove("broken")
        self.assertEqual(kept.read_bytes(), b"{\n  \"version\": 2,\n  oops\n")
        self.assertFalse(self.store.has_user("broken"))

    def test_remove_refuses_seeds_and_missing_files(self) -> None:
        with self.assertRaises(profile.ProfileError):
            self.store.remove("balanced")
        with self.assertRaises(profile.ProfileError):
            self.store.remove("nobody")


class SeedRecordTests(StoreCase):
    def sidecar(self) -> dict:
        return json.loads((self.store.root / profile.SEED_DIGESTS_FILE).read_text())

    def test_install_seeds_records_every_file_it_writes(self) -> None:
        written = self.store.install_seeds()
        self.assertEqual(sorted(written), sorted(catalog.SEED_PROFILE_NAMES))
        record = self.sidecar()
        self.assertEqual(record["version"], 1)
        for name in written:
            with self.subTest(seed=name):
                self.assertEqual(record["seeds"][name], {
                    "version": _BUNDLE.seed_profiles[name]["seed"]["version"],
                    "digest": profile.seed_digest(name, _BUNDLE.seed_profiles[name])})
        path = self.store.root / profile.SEED_DIGESTS_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.store.seed_digests(), record["seeds"])
        # A second run writes nothing and keeps the record.
        before = path.read_bytes()
        self.assertEqual(self.store.install_seeds(), [])
        self.assertEqual(path.read_bytes(), before)

    def test_reseed_records_the_shipped_version(self) -> None:
        self.store.install_seeds()
        newer = self.make_store(_seeds(balanced=_newer()))
        newer.reseed("balanced")
        self.assertEqual(self.sidecar()["seeds"]["balanced"]["version"], _newer()["seed"]["version"])
        self.assertEqual(newer.seed_state("balanced").kind, "current")

    def test_an_invalid_record_reads_as_empty(self) -> None:
        self.store.install_seeds()
        state.atomic_write(self.store.root / profile.SEED_DIGESTS_FILE, b'{"version": 2, "seeds": {}}')
        self.assertEqual(self.store.seed_digests(), {})


class SeedStateTests(StoreCase):
    def test_the_five_kinds(self) -> None:
        name = "balanced"
        shipped = _BUNDLE.seed_profiles[name]["seed"]["version"]
        # 1. No user file: served from the catalog.
        self.assertEqual(self.store.seed_state(name), profile.SeedState("current", None, shipped, False))
        # 2. A file equal to the shipped seed.
        self.store.install_seeds()
        self.assertEqual(self.store.seed_state(name), profile.SeedState("current", shipped, shipped, False))
        # 3. Unedited: the file is what was installed; a newer seed ships.
        newer = self.make_store(_seeds(balanced=_newer()))
        self.assertEqual(newer.seed_state(name), profile.SeedState("unedited", shipped, shipped + 1, True))
        self.assertEqual(newer.seed_state(name).label, "unedited-stale")
        # 4. Edited: changed after the install.
        newer.update(name, lambda doc: doc.update(description="mine now"))
        self.assertEqual(newer.seed_state(name), profile.SeedState("edited", shipped, shipped + 1, True))
        self.assertEqual(newer.seed_state(name).label, "edited-stale")
        self.assertEqual(self.store.seed_state(name).label, "edited")
        # 5. Unknown: no install record (treated as edited).
        (self.store.root / profile.SEED_DIGESTS_FILE).unlink()
        self.assertEqual(newer.seed_state(name), profile.SeedState("unknown", shipped, shipped + 1, True))
        with self.assertRaises(profile.ProfileError):
            self.store.seed_state("mine")

    def test_an_unloadable_seed_file_needs_a_restore(self) -> None:
        self.write_raw("balanced", b"not json")
        state_ = self.store.seed_state("balanced")
        self.assertEqual((state_.kind, state_.stale), ("edited", True))


class SlotDiffTests(unittest.TestCase):
    def test_one_line_per_changed_slot_then_the_fields(self) -> None:
        old = copy.deepcopy(_BUNDLE.seed_profiles["balanced"])
        new = copy.deepcopy(old)
        new["lead"] = {"model": "sol", "effort": "ultracode"}
        new["agents"]["cm-reviewer"] = {"model": "opus55", "effort": "xhigh"}
        del new["agents"]["cm-explorer"]
        new["description"] = "changed"
        new["native_agents"]["explore"] = "native"
        new["primary_provider"] = "openai"
        old_lead = f"{old['lead']['model']} · {old['lead']['effort']}"
        old_explorer = f"{old['agents']['cm-explorer']['model']} · {old['agents']['cm-explorer']['effort']}"
        old_reviewer = f"{old['agents']['cm-reviewer']['model']} · {old['agents']['cm-reviewer']['effort']}"
        self.assertEqual(profile.slot_diff(old, new), [
            f"{'lead':<14}{old_lead:<20} →  sol · ultracode",
            f"{'explorer':<14}{old_explorer:<20} →  (unbound)",
            f"{'reviewer':<14}{old_reviewer:<20} →  opus55 · xhigh",
            f"{'description':<14}{old['description']:<20} →  changed",
            f"{'native_agents.explore':<14}{'replace':<20} →  native",
            f"{'primary_provider':<14}{'(none)':<20} →  openai",
        ])
        self.assertEqual(profile.slot_diff(old, copy.deepcopy(old)), [])


class DigestSaveTests(StoreCase):
    def test_save_with_the_loaded_digest(self) -> None:
        self.store.save(self.mine())
        loaded = self.store.digest("mine")
        self.store.update("mine", lambda doc: doc.update(description="another window"))
        with self.assertRaises(profile.ProfileChangedError):
            self.store.save(self.mine(description="mine"), expected=loaded)
        self.assertEqual(self.store.load("mine")["description"], "another window")
        self.store.save(self.mine(description="mine"), expected=self.store.digest("mine"))
        self.store.save(self.mine(description="overwrite"), expected=None)
        self.assertEqual(self.store.load("mine")["description"], "overwrite")
        self.assertTrue(self.store.digest("balanced").startswith("seed:sha256:"))
        self.assertIsNone(self.store.digest("nobody"))

    def test_duplicate_drops_the_primary_provider_unless_kept(self) -> None:
        self.assertEqual(_BUNDLE.seed_profiles["claude"]["primary_provider"], "anthropic")
        self.assertNotIn("primary_provider", self.store.duplicate("claude", "copy"))
        self.assertNotIn("seed", self.store.load("copy"))
        kept = self.store.duplicate("claude", "fallback", keep_primary=True)
        self.assertEqual(kept["primary_provider"], "anthropic")


class PermissiveProfileSafetyTests(StoreCase):
    def test_warning_only_profile_stays_byte_exact_across_admission_changes(self) -> None:
        docs = copy.deepcopy(_BUNDLE.docs)
        entry = docs["models"]["models"]["opus55"]
        entry.update(status="new", capabilities=["agents"], roles=["cm-reviewer"])
        cat = profile.LineupCatalog.from_docs(docs)
        document = self.mine(
            lead={"model": "opus55", "effort": "high"},
            agents={"cm-reviewer-strong": {"model": "opus55", "effort": "high"}},
        )
        path = self.store.save(document)
        before = path.read_bytes()
        digest = self.store.digest("mine")
        lineups = []
        for admitted in (frozenset(), frozenset({"opus55"}), frozenset()):
            with self.subTest(admitted=bool(admitted)):
                effective = settings.Effective(providers_enabled={}, admitted_lines=admitted, unknown=())
                resolved = profile.resolve(self.store.load("mine"), cat, effective=effective)
                codes = {f.code for f in resolved.warnings}
                self.assertTrue({"capability-recommendation", "role-recommendation", "effort-unverified",
                                 "companion-grade", "explore-replacement-unbound"} <= codes)
                self.assertEqual("admission" in codes, not admitted)
                self.assertEqual(resolved.native_agents["explore"], "replace")
                self.assertEqual(set(resolved.agents), {"cm-reviewer-strong"})
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.store.digest("mine"), digest)
                lineups.append(resolved.applied_bindings())
        self.assertEqual(lineups[0], lineups[1])
        self.assertEqual(lineups[0], lineups[2])
        self.assertEqual(self.store.load("mine"), document)
        self.assertFalse((self.store.root.parent / "settings.json").exists())
        self.assertFalse((self.store.root / profile.SEED_DIGESTS_FILE).exists())

    def test_unusable_saved_operator_binding_refuses_without_rewriting_the_profile(self) -> None:
        docs = copy.deepcopy(_BUNDLE.docs)
        docs["models"]["models"]["opus55"]["status"] = "new"
        docs[profile.OPERATOR_LINES_KEY] = {"opus55": {"origin": "operator"}}
        cat = profile.LineupCatalog.from_docs(docs)
        document = self.mine(agents={})
        path = self.store.save(document)
        before = path.read_bytes()
        for effective, error in (
            (settings.Effective(providers_enabled={"anthropic": False}, admitted_lines=frozenset(), unknown=()),
             "provider 'anthropic' is disabled in Settings"),
            (settings.Effective(providers_enabled={}, admitted_lines=frozenset({"opus55"}), unknown=(),
                                unavailable_lines={"opus55": "selected transport is unusable"}),
             "selected transport is unusable"),
        ):
            with self.subTest(error=error):
                result = profile.evaluate(self.store.load("mine"), cat, effective=effective)
                self.assertEqual(result.errors, (f"lead.model: {error}",))
                self.assertIsNone(result.lineup)
                self.assertEqual(path.read_bytes(), before)
        # The route can be restored without editing or migrating the profile.
        resolved = profile.resolve(self.store.load("mine"), cat)
        self.assertIn("admission", {f.code for f in resolved.warnings})
        self.assertEqual(resolved.lead.binding.key, "opus55")
        self.assertEqual(path.read_bytes(), before)


class LoadErrorTests(StoreCase):
    def test_path_line_and_where(self) -> None:
        path = self.write_raw("broken", b"{\n  \"version\": 2,\n  oops\n}\n")
        with self.assertRaises(profile.ProfileLoadError) as caught:
            self.store.load("broken")
        error = caught.exception
        shown = paths.display(path, self.environ)
        self.assertEqual((error.path, error.line, error.where), (path, 3, None))
        self.assertTrue(str(error).startswith(f"cannot load profile 'broken': {shown} line 3: "), str(error))
        document = dict(self.mine(), name="invalid", workflows="semi")
        self.write_raw("invalid", strict_json.pretty_file_bytes(document))
        with self.assertRaises(profile.ProfileLoadError) as caught:
            self.store.load("invalid")
        self.assertIsNone(caught.exception.line)
        self.assertEqual(caught.exception.where, "$.workflows")
        self.assertIsInstance(caught.exception, profile.ProfileError)


class BusyTextTests(test_cli.CLITestCase):
    def test_a_removal_that_cannot_update_its_followers_says_what_happens(self) -> None:
        out = io.StringIO()
        with mock.patch.object(lineup, "served_phase", side_effect=sessions.MigrationBusyError("busy")):
            self.assertEqual(lineup.on_removed(self.runtime, "mine", renamed_to=None, out=out), 0)
        self.assertEqual(out.getvalue(), "? migration or restore in progress; not updated now — they keep "
                                         "their lineup and are pinned at their next resume (claude-multi "
                                         "doctor lists them)\n")
        out = io.StringIO()
        with mock.patch.object(lineup, "served_phase", side_effect=state.BarrierBusyError("busy")):
            lineup.on_removed(self.runtime, "mine", renamed_to="ours", out=out)
        self.assertEqual(out.getvalue(), lineup.REMOVED_BUSY.format(state=state.BARRIER_BUSY_TEXT) + "\n")


class PlanTests(test_cli.CLITestCase):
    """The setup layer's profile plans: commit, the kept copy, the default
    moving, confirmation mismatch, read-only and a profile changed meanwhile."""

    def setUp(self) -> None:
        super().setUp()
        self.runtime.profiles.install_seeds()
        document = self.runtime.profiles.load("claude")
        document.pop("seed")
        document["name"] = "mine"
        self.runtime.profiles.save(document)

    def given(self, plan: model.Plan) -> model.Confirmation:
        return model.Confirmation.given(plan)

    def test_copy(self) -> None:
        plan = setup_profiles.plan_copy(self.runtime, "mine", "copy", keep_fallback=False)
        self.assertEqual(plan.lines, ("Copy mine to copy.",
                                      "copy is not a fallback profile (mine stays the fallback for anthropic)."))
        with self.assertRaises(model.Refused) as caught:
            setup_profiles.apply_copy(self.runtime, plan, model.Confirmation("sha256:other", None, "now"))
        self.assertEqual(str(caught.exception), model.CONFIRMATION_MISMATCH)
        path = setup_profiles.apply_copy(self.runtime, plan, self.given(plan))
        self.assertEqual(path, self.runtime.profiles.root / "copy.json")
        self.assertNotIn("primary_provider", self.runtime.profiles.load("copy"))
        kept = setup_profiles.plan_copy(self.runtime, "mine", "fallback", keep_fallback=True)
        self.assertEqual(kept.lines[1], "fallback stays the fallback for anthropic; mine keeps it too.")
        setup_profiles.apply_copy(self.runtime, kept, self.given(kept))
        self.assertEqual(self.runtime.profiles.load("fallback")["primary_provider"], "anthropic")
        for target, text in (("copy", setup_profiles.NAME_TAKEN.format(name="copy")),
                             ("Bad Name", setup_profiles.NAME_BAD)):
            with self.subTest(target=target), self.assertRaises(model.Refused) as caught:
                setup_profiles.plan_copy(self.runtime, "mine", target, keep_fallback=False)
            self.assertEqual(str(caught.exception), text)
        self.assertEqual(setup_profiles.NAME_BAD,
                         "Not a profile name: a–z, 0–9, . _ - (up to 64), starting with a letter or digit.")

    def test_a_profile_changed_after_the_plan_is_never_overwritten(self) -> None:
        plan = setup_profiles.plan_copy(self.runtime, "mine", "copy", keep_fallback=False)
        self.runtime.profiles.update("mine", lambda doc: doc.update(description="changed"))
        with self.assertRaises(model.Stale) as caught:
            setup_profiles.apply_copy(self.runtime, plan, self.given(plan))
        self.assertEqual(str(caught.exception),
                         "profile mine changed while you were deciding — nothing written; try again")
        self.assertFalse(self.runtime.profiles.has_user("copy"))

    def test_a_read_only_runtime_refuses(self) -> None:
        plan = setup_profiles.plan_delete(self.runtime, "mine")
        self.runtime.allow_state_writes = False
        with self.assertRaises(model.Refused) as caught:
            setup_profiles.apply_delete(self.runtime, plan, self.given(plan))
        self.assertEqual(str(caught.exception), model.READ_ONLY_SETUP)
        self.assertTrue(self.runtime.profiles.has_user("mine"))

    def test_rename_moves_the_default(self) -> None:
        from claude_multi import choices

        choices.update(self.runtime.environ, default_profile="mine")
        plan = setup_profiles.plan_rename(self.runtime, "mine", "ours")
        self.assertEqual(plan.lines, ("Rename mine to ours.", setup_profiles.DEFAULT_MOVES.rstrip("\n")))
        outcome = setup_profiles.apply_rename(self.runtime, plan, self.given(plan))
        self.assertEqual(outcome.lines[0], "Renamed mine to ours.")
        self.assertTrue(outcome.default_moved)
        self.assertEqual(choices.read(self.runtime.environ).get("default_profile"), "ours")
        with self.assertRaises(model.Refused) as caught:
            setup_profiles.plan_rename(self.runtime, "balanced", "x")
        self.assertEqual(str(caught.exception), setup_profiles.SEED_NO_RENAME.format(name="balanced"))
        self.assertEqual(caught.exception.line_text, setup_profiles.SEED_NO_RENAME_LINE.format(name="balanced"))

    def test_delete_keeps_a_copy_and_clears_the_default(self) -> None:
        from claude_multi import choices

        choices.update(self.runtime.environ, default_profile="mine")
        before = (self.runtime.profiles.root / "mine.json").read_bytes()
        plan = setup_profiles.plan_delete(self.runtime, "mine")
        self.assertEqual(plan.consent, "destructive")
        self.assertEqual(plan.lines[-1], setup_profiles.DEFAULT_CLEARS.rstrip("\n"))
        outcome = setup_profiles.apply_delete(self.runtime, plan, self.given(plan))
        self.assertEqual(outcome.backup.read_bytes(), before)
        self.assertTrue(outcome.default_cleared)
        self.assertIsNone(choices.read(self.runtime.environ).get("default_profile"))
        shown = paths.display(outcome.backup, self.runtime.environ)
        self.assertEqual(outcome.lines[0], f"Deleted mine; a copy is kept as {shown}.")
        with self.assertRaises(model.Refused) as caught:
            setup_profiles.plan_delete(self.runtime, "balanced")
        self.assertEqual(str(caught.exception), setup_profiles.SEED_NO_DELETE.format(name="balanced"))

    def test_reseed_shows_the_changes_and_keeps_your_version(self) -> None:
        self.runtime.profiles.update("balanced", lambda doc: doc.update(description="edited"))
        before = (self.runtime.profiles.root / "balanced.json").read_bytes()
        plan = setup_profiles.plan_reseed(self.runtime, "balanced")
        self.assertFalse(plan.nothing)
        self.assertEqual(plan.kind, "edited")
        self.assertEqual(plan.diff[0][:14], f"{'description':<14}")
        self.assertTrue(plan.lines[-1].startswith("Your version is kept as "))
        outcome = setup_profiles.apply_reseed(self.runtime, plan, self.given(plan))
        self.assertEqual(outcome.backup.read_bytes(), before)
        self.assertEqual(self.runtime.profiles.seed_state("balanced").kind, "current")
        shown = paths.display(outcome.backup, self.runtime.environ)
        self.assertEqual(outcome.lines, (setup_profiles.RESEEDED.format(name="balanced", v=plan.version,
                                                                         backup=shown),))
        nothing = setup_profiles.plan_reseed(self.runtime, "balanced")
        self.assertTrue(nothing.nothing)
        self.assertEqual(nothing.lines, ("balanced already matches its shipped version.",))
        with self.assertRaises(model.Refused) as caught:
            setup_profiles.plan_reseed(self.runtime, "mine")
        self.assertEqual(str(caught.exception), setup_profiles.NOT_A_SEED.format(name="mine"))

    def test_an_unloadable_seed_is_restored(self) -> None:
        state.atomic_write(self.runtime.profiles.root / "quality.json", b"broken")
        plan = setup_profiles.plan_reseed(self.runtime, "quality")
        self.assertEqual(plan.diff, ())
        self.assertTrue(plan.lines[0].startswith("Your file cannot be loaded; the shipped version replaces it."))
        outcome = setup_profiles.apply_reseed(self.runtime, plan, self.given(plan))
        self.assertEqual(outcome.backup.read_bytes(), b"broken")
        self.assertEqual(self.runtime.profiles.load("quality")["name"], "quality")

    def test_refresh_every_unedited_stale_seed(self) -> None:
        store = self.runtime.profiles
        seeds = _seeds(claude=_newer("claude"), openai=_newer("openai"))
        newer = profile.ProfileStore(seeds=seeds, environ=self.runtime.environ)
        newer.update("openai", lambda doc: doc.update(description="edited"))
        with mock.patch.object(type(self.runtime), "profiles", new_callable=mock.PropertyMock, return_value=newer):
            plan = setup_profiles.plan_refresh_unedited(self.runtime)
            self.assertEqual(plan.names, ("claude",))
            old = _BUNDLE.seed_profiles["claude"]["seed"]["version"]
            self.assertEqual(plan.versions, f"claude {old} → {old + 1}")
            self.assertEqual(setup_profiles.apply_refresh_unedited(self.runtime, plan, self.given(plan)), ("claude",))
            self.assertEqual(newer.seed_state("claude").kind, "current")
            self.assertEqual(newer.seed_state("openai").kind, "edited")
        self.assertTrue(list(store.root.glob(".claude.pre-reseed-*.json")))

    def newer(self, *names: str) -> profile.ProfileStore:
        return profile.ProfileStore(seeds=_seeds(**{name: _newer(name) for name in names}),
                                    environ=self.runtime.environ, clock=lambda: FIXED)

    def as_store(self, store: profile.ProfileStore):
        return mock.patch.object(type(self.runtime), "profiles", new_callable=mock.PropertyMock, return_value=store)

    def test_an_edit_made_while_a_refresh_is_planned_is_never_overwritten(self) -> None:
        newer = self.newer("claude")
        path = newer.root / "claude.json"
        edited = newer.load("claude")
        edited["description"] = "concurrent user edit"
        real_read = state.read_private
        fired: list[bool] = []

        def read_then_edit(target, *args, **kwargs):
            data = real_read(target, *args, **kwargs)
            if Path(target) == path and not fired:
                # Another window saves its edit right after the plan read the file.
                fired.append(True)
                state.atomic_write(path, strict_json.pretty_file_bytes(edited))
            return data

        with self.as_store(newer):
            with mock.patch.object(state, "read_private", read_then_edit):
                plan = setup_profiles.plan_refresh_unedited(self.runtime)
            self.assertEqual(fired, [True])
            self.assertEqual(plan.names, ("claude",))
            with self.assertRaises(model.Stale) as caught:
                setup_profiles.apply_refresh_unedited(self.runtime, plan, self.given(plan))
        self.assertEqual(str(caught.exception),
                         "profile claude changed while you were deciding — nothing written; try again")
        self.assertEqual(newer.load("claude")["description"], "concurrent user edit")
        self.assertEqual(list(newer.root.glob(".claude.pre-reseed-*.json")), [])

    def test_a_stale_batch_refresh_writes_nothing(self) -> None:
        newer = self.newer("claude", "openai")
        with self.as_store(newer):
            plan = setup_profiles.plan_refresh_unedited(self.runtime)
            self.assertEqual(plan.names, ("claude", "openai"))
            before = newer.digest("claude")
            newer.update("openai", lambda doc: doc.update(description="edited before the confirmation"))
            with self.assertRaises(model.Stale) as caught:
                setup_profiles.apply_refresh_unedited(self.runtime, plan, self.given(plan))
        self.assertEqual(str(caught.exception),
                         "profile openai changed while you were deciding — nothing written; try again")
        # The profile planned first is untouched too: nothing written means nothing.
        self.assertEqual(newer.digest("claude"), before)
        self.assertEqual(newer.seed_state("claude").label, "unedited-stale")
        self.assertEqual(list(newer.root.glob(".*.pre-reseed-*.json")), [])
        self.assertEqual(newer.load("openai")["description"], "edited before the confirmation")

    def test_a_refresh_that_fails_part_way_names_what_it_updated(self) -> None:
        newer = self.newer("claude", "openai")
        real_write = profile.ProfileStore._write

        def write(store, document, name):
            if name == "openai":
                raise state.StateError(28, "No space left on device")
            return real_write(store, document, name)

        with self.as_store(newer):
            plan = setup_profiles.plan_refresh_unedited(self.runtime)
            with mock.patch.object(profile.ProfileStore, "_write", write), \
                    self.assertRaises(setup_profiles.RefreshPartial) as caught:
                setup_profiles.apply_refresh_unedited(self.runtime, plan, self.given(plan))
        self.assertEqual((caught.exception.done, caught.exception.failed), (("claude",), "openai"))
        self.assertIn("Updated claude to the shipped version", str(caught.exception))
        self.assertIn("openai was not changed", str(caught.exception))
        self.assertEqual(newer.seed_state("claude").kind, "current")
        self.assertEqual(newer.seed_state("openai").label, "unedited-stale")

    def install_record_fails(self, failing: str):
        """The install record of ``failing`` cannot be saved (a full disk)
        after its file was replaced."""

        real = profile.ProfileStore._record_seed_locked

        def record(store, written):
            if failing in written:
                raise state.StateError(28, "No space left on device")
            return real(store, written)

        return mock.patch.object(profile.ProfileStore, "_record_seed_locked", record)

    def test_an_install_record_failing_on_the_first_profile_still_reports_it_updated(self) -> None:
        newer = self.newer("claude", "openai")
        with self.as_store(newer):
            plan = setup_profiles.plan_refresh_unedited(self.runtime)
            self.assertEqual(plan.names, ("claude", "openai"))
            with self.install_record_fails("claude"):
                try:
                    setup_profiles.apply_refresh_unedited(self.runtime, plan, self.given(plan))
                except Exception as exc:  # whatever reaches the caller is the subject
                    failure = exc
                else:
                    self.fail("the refresh reported no failure")
        # An explicit partial outcome naming the profile it changed.
        self.assertIsInstance(failure, setup_profiles.RefreshPartial)
        self.assertEqual((failure.done, failure.failed, failure.failed_changed, failure.unchanged),
                         (("claude",), "claude", True, ("openai",)))
        self.assertEqual(str(failure), setup_profiles.REFRESH_UNFINISHED_REST.format(
            done="claude", failed="claude", reason=failure.__cause__.cause, unchanged="openai", verb="was"))
        self.assertNotIn("claude was not changed", str(failure))
        # Its file holds the shipped version; the next one was never touched.
        self.assertEqual(newer.seed_state("claude").kind, "current")
        self.assertTrue(list(newer.root.glob(".claude.pre-reseed-*.json")))
        self.assertEqual(newer.seed_state("openai").label, "unedited-stale")

    def test_an_install_record_failing_on_a_later_profile_reports_every_updated_one(self) -> None:
        newer = self.newer("claude", "openai")
        with self.as_store(newer):
            plan = setup_profiles.plan_refresh_unedited(self.runtime)
            with self.install_record_fails("openai"), self.assertRaises(setup_profiles.RefreshPartial) as caught:
                setup_profiles.apply_refresh_unedited(self.runtime, plan, self.given(plan))
        failure = caught.exception
        self.assertEqual((failure.done, failure.failed), (("claude", "openai"), "openai"))
        self.assertEqual(failure.unchanged, ())
        self.assertTrue(str(failure).startswith("Updated claude, openai to the shipped version (your versions are "
                                                "kept), but openai was not finished: "), str(failure))
        self.assertNotIn("not changed", str(failure))
        self.assertEqual({newer.seed_state(name).kind for name in ("claude", "openai")}, {"current"})

    def test_a_stale_seed_without_an_install_record_offers_its_update(self) -> None:
        # Installed before the install record existed: provenance unknown, a newer version ships.
        newer = self.newer("claude")
        (newer.root / profile.SEED_DIGESTS_FILE).unlink()
        judged = newer.seed_state("claude")
        self.assertEqual((judged.kind, judged.stale), ("unknown", True))
        ready = readiness.SlotReadiness("cm-lead", readiness.READY)
        with self.as_store(newer), \
                mock.patch.object(type(self.runtime), "profile_readiness",
                                  lambda runtime, names, observations=None: {name: (ready,) for name in names}):
            row = {row.name: row for row in setup_profiles.rows(self.runtime)}["claude"]
            plan = setup_profiles.plan_reseed(self.runtime, "claude")
        self.assertEqual(row.seed_state, "unknown")
        self.assertTrue(row.stale)
        self.assertIn("update available", row.notes)
        self.assertEqual(row.reason_line, setup_profiles.PROFILE_REASON["seed-stale-edited"])
        # Its update asks first, with the changes and the kept copy, as for an edited seed.
        self.assertEqual((plan.nothing, plan.consent, plan.kind), (False, "confirm", "unknown"))
        self.assertTrue(plan.lines[-1].startswith("Your version is kept as "))

    def test_rows_name_one_reason_and_one_fix(self) -> None:
        state.atomic_write(self.runtime.profiles.root / "broken.json", b"{\n oops")
        rows = {row.name: row for row in setup_profiles.rows(self.runtime)}
        self.assertEqual(set(rows), set(self.runtime.profiles.names()))
        broken = rows["broken"]
        self.assertEqual((broken.loadable, broken.ready, broken.state_text), (False, None, "cannot load"))
        self.assertEqual(broken.reason_line, setup_profiles.PROFILE_REASON["unloadable"])
        self.assertIn("line 2", broken.notes)
        self.assertEqual(list(rows)[-1], "broken")  # unloadable rows come last
        mine = rows["mine"]
        self.assertEqual((mine.origin, mine.seed_state, mine.fallback_for), ("yours", None, "anthropic"))
        self.assertEqual(rows["balanced"].origin, "seed")
        self.assertEqual(rows["balanced"].seed_state, "current")
        fallback = setup_profiles.rows(self.runtime, fallback_only=True)
        self.assertTrue(fallback)
        self.assertTrue(all(row.fallback_for for row in fallback))
        self.assertEqual(setup_profiles.other_fallbacks(self.runtime, "anthropic", name="mine"), ("claude",))

    def test_every_apply_checks_its_confirmation(self) -> None:
        plans = (
            (setup_profiles.plan_copy(self.runtime, "mine", "copy", keep_fallback=False), setup_profiles.apply_copy),
            (setup_profiles.plan_rename(self.runtime, "mine", "ours"), setup_profiles.apply_rename),
            (setup_profiles.plan_delete(self.runtime, "mine"), setup_profiles.apply_delete),
            (setup_profiles.plan_reseed(self.runtime, "balanced"), setup_profiles.apply_reseed),
            (setup_profiles.plan_refresh_unedited(self.runtime), setup_profiles.apply_refresh_unedited),
        )
        before = sorted(p.name for p in self.runtime.profiles.root.iterdir())
        for plan, apply in plans:
            with self.subTest(op=plan.op):
                self.assertTrue(plan.digest.startswith("sha256:"))
                wrong = model.Confirmation(plan.digest, "typed", "now")
                with self.assertRaises(model.Refused) as caught:
                    apply(self.runtime, plan, wrong)
                self.assertEqual(str(caught.exception), model.CONFIRMATION_MISMATCH)
        self.assertEqual(sorted(p.name for p in self.runtime.profiles.root.iterdir()), before)

    def test_a_file_that_cannot_be_loaded_is_edited_as_stored(self) -> None:
        from claude_multi.cli.commands import profile as profile_cmd

        good = strict_json.pretty_file_bytes(self.runtime.profiles.load("mine"))
        path = self.runtime.profiles.root / "mine.json"
        state.atomic_write(path, b"{ not json")
        script = self.root / "editor.py"
        script.write_text("import sys\nassert open(sys.argv[1], 'rb').read() == b'{ not json'\n"
                          f"open(sys.argv[1], 'wb').write({good!r})\n")
        self.runtime.environ["EDITOR"] = f"python3 {script}"
        out = io.StringIO()
        code = profile_cmd._profile_command(
            self.runtime, argparse.Namespace(name="mine", line=True), "edit",
            input_stream=io.StringIO(""), output_stream=out, interactive=True)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(self.runtime.profiles.load("mine")["name"], "mine")

    def test_the_reason_texts(self) -> None:
        self.assertEqual(setup_profiles.PROFILE_REASON["connected"],
                         "connected: every model this profile uses is served here")
        self.assertEqual(setup_profiles.PROFILE_REASON["needs-key"].format(display="OpenRouter"),
                         "needs the OpenRouter API key: G → K sets it")
        self.assertEqual(setup_profiles.SEED_NO_DELETE.format(name="claude"),
                         "claude is a shipped profile and cannot be deleted — U restores its shipped version.")
        self.assertEqual(setup_profiles.FOLLOWERS_LINE.format(n=2, name="mine", m=1),
                         "2 session(s) follow mine (1 running). After this they keep their current lineup "
                         "and stop following it.\n")


if __name__ == "__main__":
    unittest.main()
