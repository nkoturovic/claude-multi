"""Named bindings and the profile store.

Every test runs on a temp HOME / XDG_STATE_HOME (and, where it says so, a
temp XDG_CONFIG_HOME) with the frozen fixture catalog; catalog mutations
(a test-local retired entry, a ``status: new`` line) go through a deep copy
of the fixture's raw docs. Nothing reads the operator's real config.
"""

from __future__ import annotations

import copy
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from claude_multi import catalog, paths, profile, sessions, settings, state, strict_json

from _catalog import FIXTURE_ROOT


_BUNDLE = catalog.load_catalog(FIXTURE_ROOT)
_RAW = catalog.load_raw(FIXTURE_ROOT)
_JOIN_TIMEOUT = 20.0  # generous: a deadlock never finishes, a healthy call takes ms


def b(model: str, effort: str) -> dict:
    return {"effort": effort, "model": model}


def use(name: str) -> dict:
    return {"use": name}


_NATIVE = {"explore": "native", "general_purpose": "on", "plan": "native"}


def pdoc(name: str, lead: dict, agents: dict, **over) -> dict:
    document = {
        "version": 2,
        "name": name,
        "lead": lead,
        "agents": agents,
        "native_agents": dict(_NATIVE),
    }
    document.update(over)
    return document


def p1() -> dict:
    return pdoc(
        "p1",
        use("anthropic-lead"),
        {"cm-analyst": b("sol", "high"), "cm-analyst-strong": use("frontier")},
    )


def p2() -> dict:
    return pdoc(
        "p2",
        use("anthropic-lead"),
        {"cm-reviewer": b("sol", "xhigh"), "cm-reviewer-strong": use("frontier")},
    )


def p3() -> dict:
    return pdoc("p3", b("opus55", "ultracode"), {"cm-analyst": b("sol", "high")})


def _lcat_with(mutate) -> profile.LineupCatalog:
    docs = copy.deepcopy(_RAW["docs"])
    mutate(docs)
    return profile.LineupCatalog.from_docs(docs)


def _retired_entry(successor: str | None) -> dict:
    entry = copy.deepcopy(_BUNDLE.retired["muse-spark"])
    entry["successor"] = successor
    return entry


def _run_in_thread(target) -> tuple[threading.Thread, list]:
    outcome: list = []

    def runner() -> None:
        try:
            outcome.append(("ok", target()))
        except BaseException as exc:  # reported by the test
            outcome.append(("error", exc))

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread, outcome


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-profile-store-"))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.environ = {
            "HOME": str(self.tmp / "home"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
        }
        self.config_root = self.tmp / "home" / ".config" / "claude-multi"
        self.bstore = profile.BindingStore(self.environ)
        self.pstore = profile.ProfileStore.for_catalog(_BUNDLE, self.environ)

    def write_marker(self, value: bytes = b"99\n") -> None:
        root = state.ensure_private_dir(sessions.state_root(self.environ))
        state.atomic_write(root / sessions.STATE_MARKER, value)

    def write_bindings_raw(self, document) -> Path:
        state.ensure_private_dir(self.config_root)
        path = self.config_root / "bindings.json"
        state.atomic_write(path, strict_json.pretty_file_bytes(document))
        return path

    def write_profile_raw(self, name: str, document) -> Path:
        state.ensure_private_dir(self.config_root)
        root = state.ensure_private_dir(self.config_root / "profiles")
        path = root / f"{name}.json"
        state.atomic_write(path, strict_json.pretty_file_bytes(document))
        return path

    def tree(self) -> dict[str, bytes | None]:
        """Every path under the temp root (dirs as None) with file bytes."""

        found: dict[str, bytes | None] = {}
        for base, dirs, files in os.walk(self.tmp):
            for name in dirs:
                found[os.path.relpath(Path(base) / name, self.tmp)] = None
            for name in files:
                path = Path(base) / name
                found[os.path.relpath(path, self.tmp)] = path.read_bytes()
        return found

    def base_bindings(self) -> None:
        self.bstore.set("anthropic-lead", "opus55", "ultracode", cat=_BUNDLE)
        self.bstore.set("frontier", "opus55", "xhigh", cat=_BUNDLE)

    def binding_error(self, call) -> str:
        with self.assertRaises(profile.BindingError) as caught:
            call()
        return str(caught.exception)

    def profile_error(self, call) -> str:
        with self.assertRaises(profile.ProfileError) as caught:
            call()
        return str(caught.exception)


# ------------------------------------------------------------ B acceptance


class NamedBindingEditTests(StoreCase):
    """B acceptance: editing a named binding reaches every referencing profile."""

    def test_binding_edit_propagates_without_touching_profile_files(self) -> None:
        self.base_bindings()
        for document in (p1(), p2(), p3()):
            self.pstore.new(document)

        def resolve_all() -> dict[str, profile.ResolvedLineup]:
            bindings = self.bstore.bindings()
            return {
                name: profile.resolve(self.pstore.load(name), _BUNDLE, bindings=bindings)
                for name in ("p1", "p2", "p3")
            }

        before = resolve_all()
        self.assertEqual(before["p1"].lead.binding.key, "opus55")
        self.assertEqual(before["p1"].agents["cm-analyst-strong"].binding.key, "opus55")
        files = {
            name: (self.config_root / "profiles" / f"{name}.json").read_bytes()
            for name in ("p1", "p2", "p3")
        }

        self.bstore.set("anthropic-lead", "fable", "ultracode", cat=_BUNDLE, profiles=self.pstore)
        self.bstore.set("frontier", "fable", "max", cat=_BUNDLE, profiles=self.pstore)

        after = resolve_all()
        for name, slot in (("p1", "cm-analyst-strong"), ("p2", "cm-reviewer-strong")):
            with self.subTest(profile=name):
                lineup = after[name]
                self.assertEqual(lineup.lead.binding.key, "fable")
                self.assertEqual(lineup.lead.binding.effort, "ultracode")
                self.assertEqual(lineup.lead.binding.named, "anthropic-lead")
                self.assertEqual(lineup.agents[slot].binding.key, "fable")
                self.assertEqual(lineup.agents[slot].binding.effort, "max")
                self.assertEqual(lineup.agents[slot].binding.named, "frontier")
                self.assertEqual(lineup.named_bindings, ("anthropic-lead", "frontier"))
        self.assertEqual(after["p3"].applied_bindings(), before["p3"].applied_bindings())
        self.assertEqual(after["p3"].named_bindings, ())
        for name, data in files.items():
            with self.subTest(file=name):
                self.assertEqual(
                    (self.config_root / "profiles" / f"{name}.json").read_bytes(), data
                )


# ------------------------------------------------------------ BindingStore


class BindingStoreTests(StoreCase):
    def test_absent_file_is_empty_and_reads_create_nothing(self) -> None:
        store = profile.BindingStore(self.environ)
        self.assertEqual(store.path, self.config_root / "bindings.json")
        self.assertEqual(store.load(), {"version": 1, "bindings": {}})
        self.assertEqual(store.bindings(), {})
        self.assertEqual(store.conflicts(_BUNDLE), [])
        self.assertFalse((self.tmp / "home").exists())

    def test_set_on_a_fresh_config_root_writes_0600_in_0700(self) -> None:
        written = self.bstore.set("frontier", "opus55", "xhigh", cat=_BUNDLE)
        self.assertEqual(written, {"version": 1, "bindings": {"frontier": b("opus55", "xhigh")}})
        path = self.config_root / "bindings.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config_root.stat().st_mode), 0o700)
        self.assertEqual(path.read_bytes(), strict_json.pretty_file_bytes(written))
        self.assertEqual(self.bstore.load(), written)

    def test_b1_name_pattern(self) -> None:
        self.assertEqual(
            self.binding_error(lambda: self.bstore.set("Bad", "sol", "high", cat=_BUNDLE)),
            "bindings.Bad: name must match ^[a-z][a-z0-9-]{0,31}$",
        )
        self.assertEqual(
            self.binding_error(lambda: self.bstore.set("x" * 33, "sol", "high", cat=_BUNDLE)),
            f"bindings.{'x' * 33}: name must match ^[a-z][a-z0-9-]{{0,31}}$",
        )
        # passing counterpart: the longest legal name
        self.bstore.set("a" + "b" * 31, "sol", "high", cat=_BUNDLE)
        self.assertIn("a" + "b" * 31, self.bstore.bindings())

    def test_b2_live_key_collision(self) -> None:
        self.assertEqual(
            self.binding_error(lambda: self.bstore.set("opus55", "sol", "high", cat=_BUNDLE)),
            "bindings.opus55: collides with catalog model key 'opus55'",
        )
        self.bstore.set("opus55-x", "sol", "high", cat=_BUNDLE)  # passing counterpart

    def test_b2_counts_status_new_lines(self) -> None:
        lcat = _lcat_with(lambda docs: docs["models"]["models"]["grok46"].update(status="new"))
        self.assertEqual(
            self.binding_error(lambda: self.bstore.set("grok46", "sol", "high", cat=lcat)),
            "bindings.grok46: collides with catalog model key 'grok46'",
        )

    def test_b3_retired_key_and_generation_base_collisions(self) -> None:
        self.assertEqual(
            self.binding_error(lambda: self.bstore.set("muse-spark", "sol", "high", cat=_BUNDLE)),
            "bindings.muse-spark: collides with retired catalog key 'muse-spark'",
        )
        lcat = _lcat_with(
            lambda docs: docs["retired"]["retired"].__setitem__("legacy@1", _retired_entry("sol"))
        )
        self.assertEqual(
            self.binding_error(lambda: self.bstore.set("legacy", "sol", "high", cat=lcat)),
            "bindings.legacy: collides with retired catalog key 'legacy'",
        )
        self.bstore.set("legacy-2", "sol", "high", cat=lcat)  # passing counterpart

    def test_b4_unknown_model(self) -> None:
        self.assertEqual(
            self.binding_error(lambda: self.bstore.set("x", "nope", "high", cat=_BUNDLE)),
            "bindings.x.model: unknown model 'nope'",
        )
        self.bstore.set("x", "sol", "high", cat=_BUNDLE)  # passing counterpart

    def test_b5_retired_models_must_bind_the_live_line(self) -> None:
        message = self.binding_error(
            lambda: self.bstore.set("x", "muse-spark", "high", cat=_BUNDLE)
        )
        notice = _BUNDLE.resolve_key("muse-spark").notice
        self.assertEqual(message, f"bindings.x.model: {notice}; bind the live line")
        self.assertTrue(message.endswith("(no successor): needs a model choice; bind the live line"))
        lcat = _lcat_with(
            lambda docs: docs["retired"]["retired"].__setitem__("x-old", _retired_entry("sol"))
        )
        message = self.binding_error(lambda: self.bstore.set("x", "x-old", "high", cat=lcat))
        self.assertEqual(
            message,
            f"bindings.x.model: {lcat.resolve_key('x-old').notice}; bind the live line",
        )
        self.assertTrue(message.endswith("-> sol; bind the live line"))
        self.bstore.set("x", "sol", "high", cat=lcat)  # passing counterpart

    def test_b6_status_new_until_admitted(self) -> None:
        lcat = _lcat_with(lambda docs: docs["models"]["models"]["grok46"].update(status="new"))
        self.assertEqual(
            self.binding_error(lambda: self.bstore.set("x", "grok46", "high", cat=lcat)),
            "bindings.x.model: model 'grok46' is New · Off (status new) until admitted",
        )
        admitted = settings.Effective(
            providers_enabled={}, admitted_lines=frozenset({"grok46"}), unknown=()
        )
        self.bstore.set("x", "grok46", "high", cat=lcat, effective=admitted)
        self.assertEqual(self.bstore.bindings()["x"], b("grok46", "high"))

    def test_b7_effort_must_be_declared(self) -> None:
        self.assertEqual(
            self.binding_error(lambda: self.bstore.set("x", "sol", "medium", cat=_BUNDLE)),
            "bindings.x.effort: 'medium' is not declared by 'sol' (declared: high, xhigh, ultracode)",
        )
        self.assertEqual(
            self.binding_error(lambda: self.bstore.set("x", "gpt55", "ultracode", cat=_BUNDLE)),
            "bindings.x.effort: 'ultracode' is not declared by 'gpt55' (declared: high)",
        )
        # passing counterparts: ultracode on a lead-capable line, a declared agent effort
        self.bstore.set("x", "sol", "ultracode", cat=_BUNDLE)
        self.bstore.set("y", "gpt55", "high", cat=_BUNDLE)

    def test_every_name_is_reported_sorted(self) -> None:
        document = {
            "version": 1,
            "bindings": {"zz": b("nope", "high"), "aa": b("sol", "medium")},
        }
        self.assertEqual(
            self.binding_error(lambda: self.bstore.save(document, cat=_BUNDLE)),
            "bindings.aa.effort: 'medium' is not declared by 'sol' (declared: high, xhigh, ultracode); "
            "bindings.zz.model: unknown model 'nope'",
        )
        self.assertFalse((self.tmp / "home").exists())  # a refused save creates nothing

    def test_b8_delete_refuses_while_referenced(self) -> None:
        self.base_bindings()
        for document in (p1(), p2(), p3()):
            self.pstore.new(document)
        before = self.bstore.path.read_bytes()
        self.assertEqual(
            self.binding_error(lambda: self.bstore.delete("anthropic-lead", profiles=self.pstore)),
            "named binding 'anthropic-lead' is used by profiles: p1 (lead), p2 (lead)",
        )
        self.assertEqual(
            self.binding_error(lambda: self.bstore.delete("frontier", profiles=self.pstore)),
            "named binding 'frontier' is used by profiles: "
            "p1 (agents.cm-analyst-strong), p2 (agents.cm-reviewer-strong)",
        )
        self.assertEqual(self.bstore.path.read_bytes(), before)
        # passing counterparts: an unreferenced binding deletes; a missing one refuses
        self.bstore.set("spare", "sol", "high", cat=_BUNDLE)
        written = self.bstore.delete("spare", profiles=self.pstore)
        self.assertNotIn("spare", written["bindings"])
        self.assertNotIn("spare", self.bstore.bindings())
        self.assertEqual(
            self.binding_error(lambda: self.bstore.delete("spare", profiles=self.pstore)),
            "named binding 'spare' does not exist",
        )

    def test_b9_an_edit_that_invalidates_a_referencing_profile_refuses(self) -> None:
        self.base_bindings()
        for document in (p1(), p2(), p3()):
            self.pstore.new(document)
        before = self.bstore.path.read_bytes()
        message = self.binding_error(
            lambda: self.bstore.set(
                "anthropic-lead", "gpt55", "high", cat=_BUNDLE, profiles=self.pstore
            )
        )
        self.assertEqual(
            message.split("; "),
            [
                "named binding 'anthropic-lead' would invalidate profile 'p1': "
                "lead.use: named binding 'anthropic-lead': 'gpt55' lacks the lead capability",
                "named binding 'anthropic-lead' would invalidate profile 'p2': "
                "lead.use: named binding 'anthropic-lead': 'gpt55' lacks the lead capability",
            ],
        )
        self.assertEqual(self.bstore.path.read_bytes(), before)
        # Without profiles= the store checks the binding alone (B1-B7).
        self.bstore.set("anthropic-lead", "gpt55", "high", cat=_BUNDLE)
        self.assertEqual(self.bstore.bindings()["anthropic-lead"], b("gpt55", "high"))

    def test_b9_removing_a_referenced_name_through_update_refuses(self) -> None:
        self.base_bindings()
        self.pstore.new(p1())
        message = self.binding_error(
            lambda: self.bstore.update(
                lambda document: document["bindings"].pop("frontier"),
                cat=_BUNDLE,
                profiles=self.pstore,
            )
        )
        self.assertEqual(
            message,
            "named binding 'frontier' would invalidate profile 'p1': "
            "agents.cm-analyst-strong.use: unknown named binding 'frontier'",
        )

    def test_b9_ignores_errors_the_old_bindings_already_had(self) -> None:
        self.base_bindings()
        # cm-analyst-strong requires cm-analyst: invalid before and after the edit.
        broken = pdoc("broken", b("opus55", "ultracode"), {"cm-analyst-strong": use("frontier")})
        self.pstore.new(broken)
        self.assertTrue(
            profile.evaluate(broken, _BUNDLE, bindings=self.bstore.bindings()).errors
        )
        self.bstore.set("frontier", "fable", "max", cat=_BUNDLE, profiles=self.pstore)
        self.assertEqual(self.bstore.bindings()["frontier"], b("fable", "max"))

    def test_symlinked_file_refuses_to_load(self) -> None:
        real = self.tmp / "elsewhere.json"
        real.write_bytes(strict_json.pretty_file_bytes({"version": 1, "bindings": {}}))
        os.chmod(real, 0o600)
        state.ensure_private_dir(self.config_root)
        (self.config_root / "bindings.json").symlink_to(real)
        message = self.binding_error(self.bstore.load)
        self.assertTrue(message.startswith("cannot load named bindings: "), message)
        self.assertIn("symlink", message)

    def test_version_2_wrapper_and_bare_map_refuse(self) -> None:
        self.write_bindings_raw({"version": 2, "bindings": {}})
        self.assertEqual(
            self.binding_error(self.bstore.load),
            "cannot load named bindings: version: unsupported named-bindings version 2 "
            "(this launcher reads version 1)",
        )
        self.write_bindings_raw({"frontier": b("opus55", "xhigh")})
        self.assertEqual(
            self.binding_error(self.bstore.load),
            "cannot load named bindings: version: unsupported named-bindings version None "
            "(this launcher reads version 1)",
        )

    def test_nested_use_and_bad_stored_names_refuse_on_load(self) -> None:
        self.write_bindings_raw({"version": 1, "bindings": {"x": {"use": "y"}}})
        message = self.binding_error(self.bstore.load)
        self.assertTrue(message.startswith("cannot load named bindings: $.bindings.x"), message)
        self.write_bindings_raw({"version": 1, "bindings": {"Bad": b("sol", "high")}})
        self.assertEqual(
            self.binding_error(self.bstore.load),
            "cannot load named bindings: bindings.Bad: name must match ^[a-z][a-z0-9-]{0,31}$",
        )

    def test_group_readable_file_refuses(self) -> None:
        path = self.write_bindings_raw({"version": 1, "bindings": {}})
        os.chmod(path, 0o640)
        self.assertIn("group/other", self.binding_error(self.bstore.load))

    def test_newer_state_marker_refuses_every_write(self) -> None:
        self.base_bindings()
        self.pstore.new(p1())
        before = self.tree()
        self.write_marker()
        marked = self.tree()
        calls = [
            lambda: self.bstore.set("frontier", "fable", "max", cat=_BUNDLE),
            lambda: self.bstore.delete("anthropic-lead", profiles=self.pstore),
            lambda: self.bstore.save({"version": 1, "bindings": {}}, cat=_BUNDLE),
            lambda: self.bstore.update(lambda document: None, cat=_BUNDLE),
        ]
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(sessions.StateMarkerError):
                    call()
        self.assertEqual(self.tree(), marked)
        self.assertEqual(
            {k: v for k, v in marked.items() if k in before}, before
        )
        self.assertEqual(self.bstore.bindings()["frontier"], b("opus55", "xhigh"))

    def test_marker_on_a_fresh_root_creates_nothing(self) -> None:
        self.write_marker()
        with self.assertRaises(sessions.StateMarkerError):
            self.bstore.set("frontier", "opus55", "xhigh", cat=_BUNDLE)
        self.assertFalse((self.tmp / "home").exists())

    def test_update_waits_for_a_held_lock(self) -> None:
        state.ensure_private_dir(self.config_root)
        held = state.FileLock(self.bstore.path)
        self.assertTrue(held.acquire(blocking=False))
        try:
            thread, outcome = _run_in_thread(
                lambda: self.bstore.set("frontier", "opus55", "xhigh", cat=_BUNDLE)
            )
            time.sleep(0.2)
            self.assertTrue(thread.is_alive(), "update did not wait for the lock")
            self.assertFalse(self.bstore.path.exists())
        finally:
            held.release()
        thread.join(_JOIN_TIMEOUT)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome[0][0], "ok", outcome)
        self.assertEqual(self.bstore.bindings()["frontier"], b("opus55", "xhigh"))

    def test_update_holds_the_lock_once_and_completes(self) -> None:
        contention: list[bool] = []

        def mutate(document: dict) -> None:
            probe = state.FileLock(self.bstore.path)
            got = probe.acquire(blocking=False)
            contention.append(got)
            if got:
                probe.release()
            document["bindings"]["frontier"] = b("opus55", "xhigh")

        thread, outcome = _run_in_thread(
            lambda: self.bstore.update(mutate, cat=_BUNDLE, profiles=self.pstore)
        )
        thread.join(_JOIN_TIMEOUT)
        self.assertFalse(thread.is_alive(), "BindingStore.update deadlocked")
        self.assertEqual(outcome[0][0], "ok", outcome)
        self.assertEqual(contention, [False])
        self.assertEqual(
            (self.config_root / "bindings.json.lock").exists(), True
        )

    def test_conflicts_reports_names_a_later_catalog_took(self) -> None:
        self.write_bindings_raw(
            {
                "version": 1,
                "bindings": {
                    "sol": b("opus55", "xhigh"),
                    "muse-spark": b("sol", "high"),
                    "fine": b("sol", "high"),
                },
            }
        )
        self.assertEqual(
            self.bstore.conflicts(_BUNDLE),
            [
                "bindings.muse-spark: collides with retired catalog key 'muse-spark'",
                "bindings.sol: collides with catalog model key 'sol'",
            ],
        )
        self.assertIn("sol", self.bstore.bindings())  # tolerated on load
        # a stored retired model is tolerated on load too (resolution reports it)
        self.write_bindings_raw({"version": 1, "bindings": {"old": b("muse-spark", "high")}})
        self.assertEqual(self.bstore.bindings(), {"old": b("muse-spark", "high")})
        self.assertEqual(self.bstore.conflicts(_BUNDLE), [])

    def test_a_stale_collision_never_blocks_an_unrelated_edit(self) -> None:
        # A later catalog took "sol": tolerated on unchanged names by every writer.
        self.write_bindings_raw(
            {"version": 1, "bindings": {"sol": b("opus55", "xhigh"), "fine": b("sol", "high")}}
        )
        self.bstore.set("other", "sol", "high", cat=_BUNDLE)
        self.bstore.set("fine", "sol", "xhigh", cat=_BUNDLE)
        self.bstore.save(
            {"version": 1, "bindings": dict(self.bstore.bindings(), more=b("gpt55", "high"))},
            cat=_BUNDLE,
        )
        self.assertEqual(
            self.bstore.bindings(),
            {
                "fine": b("sol", "xhigh"),
                "more": b("gpt55", "high"),
                "other": b("sol", "high"),
                "sol": b("opus55", "xhigh"),
            },
        )
        # changing the stale name itself is a change: B2 applies to it
        before = self.bstore.path.read_bytes()
        self.assertEqual(
            self.binding_error(lambda: self.bstore.set("sol", "fable", "max", cat=_BUNDLE)),
            "bindings.sol: collides with catalog model key 'sol'",
        )
        self.assertEqual(self.bstore.path.read_bytes(), before)
        # the stale name can still be dropped while nothing references it
        self.bstore.delete("sol", profiles=self.pstore)
        self.assertNotIn("sol", self.bstore.bindings())

    def test_stale_retired_bindings_repair_one_at_a_time(self) -> None:
        self.write_bindings_raw(
            {"version": 1, "bindings": {"a": b("muse-spark", "high"), "c": b("muse-spark", "high")}}
        )
        notice = _BUNDLE.resolve_key("muse-spark").notice
        # a newly added retired binding still refuses, reporting only the new name
        before = self.bstore.path.read_bytes()
        self.assertEqual(
            self.binding_error(lambda: self.bstore.set("d", "muse-spark", "high", cat=_BUNDLE)),
            f"bindings.d.model: {notice}; bind the live line",
        )
        self.assertEqual(self.bstore.path.read_bytes(), before)
        self.bstore.set("a", "sol", "high", cat=_BUNDLE, profiles=self.pstore)
        self.assertEqual(self.bstore.bindings()["c"], b("muse-spark", "high"))
        self.bstore.set("c", "sol", "high", cat=_BUNDLE, profiles=self.pstore)
        self.assertEqual(
            self.bstore.bindings(), {"a": b("sol", "high"), "c": b("sol", "high")}
        )

    def test_save_refuses_an_unreadable_existing_file(self) -> None:
        path = self.write_bindings_raw({"version": 1, "bindings": {}})
        path.write_bytes(b"{garbage")
        document = {"version": 1, "bindings": {"x": b("opus55", "xhigh")}}
        message = self.binding_error(lambda: self.bstore.save(document, cat=_BUNDLE))
        self.assertTrue(message.startswith("cannot load named bindings: "), message)
        self.assertEqual(path.read_bytes(), b"{garbage")
        path.write_bytes(strict_json.pretty_file_bytes({"version": 1, "bindings": {}}))
        os.chmod(path, 0o644)
        message = self.binding_error(lambda: self.bstore.save(document, cat=_BUNDLE))
        self.assertIn("group/other", message)
        self.assertNotIn("[Errno", message)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        # passing counterpart: a readable file is replaced
        os.chmod(path, 0o600)
        self.bstore.save(document, cat=_BUNDLE)
        self.assertEqual(self.bstore.bindings(), document["bindings"])

    def test_xdg_config_home_moves_the_file(self) -> None:
        environ = dict(self.environ, XDG_CONFIG_HOME=str(self.tmp / "xdg"))
        store = profile.BindingStore(environ)
        self.assertEqual(store.path, self.tmp / "xdg" / "claude-multi" / "bindings.json")
        self.assertEqual(profile.bindings_path(environ), store.path)


# ------------------------------------------------------------ ProfileStore


class ProfileStoreTests(StoreCase):
    def test_construction_and_reads_on_an_empty_root_create_nothing(self) -> None:
        store = profile.ProfileStore.for_catalog(_BUNDLE, self.environ)
        self.assertEqual(store.root, self.config_root / "profiles")
        self.assertEqual(store.names(), sorted(catalog.SEED_PROFILE_NAMES))
        self.assertEqual(store.load("balanced"), _BUNDLE.seed_profiles["balanced"])
        self.assertTrue(store.contains("balanced"))
        self.assertFalse(store.has_user("balanced"))
        self.assertEqual(store.seed_updates(), [])
        self.assertEqual(store.referencing("frontier"), [])
        self.assertFalse((self.tmp / "home").exists())

    def test_for_catalog_uses_the_catalog_seeds(self) -> None:
        for name in catalog.SEED_PROFILE_NAMES:
            with self.subTest(seed=name):
                self.assertTrue(self.pstore.is_seed(name))
        self.assertFalse(self.pstore.is_seed("mine"))

    def test_names_are_seeds_plus_valid_user_stems(self) -> None:
        self.pstore.new(p3())
        root = self.config_root / "profiles"
        (root / "notes.txt").write_bytes(b"x")
        (root / "link.json").symlink_to(root / "p3.json")
        (root / "Bad.json").write_bytes(b"{}")
        (root / "dir.json").mkdir()
        self.assertEqual(self.pstore.names(), sorted([*catalog.SEED_PROFILE_NAMES, "p3"]))

    def test_load_of_a_seed_is_a_deep_copy(self) -> None:
        loaded = self.pstore.load("balanced")
        loaded["agents"].clear()
        loaded["lead"]["model"] = "sol"
        self.assertEqual(self.pstore.load("balanced"), _BUNDLE.seed_profiles["balanced"])
        # the store holds its own copy of the seeds
        seeds = {"balanced": copy.deepcopy(_BUNDLE.seed_profiles["balanced"])}
        store = profile.ProfileStore(seeds=seeds, environ=self.environ)
        seeds["balanced"]["lead"]["model"] = "sol"
        self.assertEqual(store.load("balanced")["lead"]["model"], "opus55")

    def test_load_non_searchable_seed_override_raises(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("BOUNDARY: root searches a mode-400 directory")
        name = catalog.DEFAULT_SEED
        override = {**self.pstore.load(name), "description": "user override, not the seed"}
        path = self.write_profile_raw(name, override)
        before = path.read_bytes()
        os.chmod(self.pstore.root, 0o400)
        try:
            with self.assertRaisesRegex(profile.ProfileError,
                                        f"cannot load profile {name!r}: .*Permission denied"):
                self.pstore.load(name)
            self.assertEqual(stat.S_IMODE(self.pstore.root.stat().st_mode), 0o400)
        finally:
            os.chmod(self.pstore.root, 0o700)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.pstore.load(name), override)

    def test_save_writes_pretty_private_bytes_and_forces_the_name(self) -> None:
        document = p3()
        path = self.pstore.save(document, target="mine")
        self.assertEqual(path, self.config_root / "profiles" / "mine.json")
        expected = dict(document, name="mine")
        self.assertEqual(path.read_bytes(), strict_json.pretty_file_bytes(expected))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.config_root.stat().st_mode), 0o700)
        self.assertEqual(self.pstore.load("mine"), expected)
        self.assertEqual(document["name"], "p3")  # the caller's document is untouched
        # save is an overwrite
        self.pstore.save(dict(expected, description="changed"))
        self.assertEqual(self.pstore.load("mine")["description"], "changed")

    def test_save_is_schema_only_and_refuses_invalid_documents(self) -> None:
        # semantically invalid (unknown model) but schema-valid: saved
        self.pstore.save(pdoc("odd", b("nope", "ultracode"), {}))
        self.assertEqual(self.pstore.load("odd")["lead"]["model"], "nope")
        with self.assertRaises(profile.ProfileValidationError) as caught:
            self.pstore.save(dict(p3(), workflows="semi"), target="bad")
        self.assertEqual(
            caught.exception.errors, ("$.workflows: value 'semi' is not in the allowed set",)
        )
        self.assertFalse(self.pstore.has_user("bad"))

    def test_create_writes_only_an_absent_user_file(self) -> None:
        # Import: a virtual seed may be shadowed, a user file never.
        shadow = dict(self.pstore.load("balanced"), description="imported")
        path = self.pstore.create(shadow)
        self.assertEqual(path, self.config_root / "profiles" / "balanced.json")
        self.assertEqual(self.pstore.load("balanced")["description"], "imported")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        before = path.read_bytes()
        message = self.profile_error(lambda: self.pstore.create(dict(shadow, description="again")))
        self.assertIn("appeared meanwhile; it is not overwritten", message)
        self.assertEqual(path.read_bytes(), before)
        self.pstore.create(p3())
        self.assertEqual(self.pstore.load("p3"), p3())
        with self.assertRaises(profile.ProfileValidationError):
            self.pstore.create(dict(p3(), name="bad", workflows="semi"))
        self.assertFalse(self.pstore.has_user("bad"))

    def test_refused_save_on_a_fresh_root_creates_nothing(self) -> None:
        with self.assertRaises(profile.ProfileValidationError):
            self.pstore.save({"version": 1, "name": "x"})
        with self.assertRaises(profile.ProfileError):
            self.pstore.save(p3(), target="Bad Name")
        self.assertFalse((self.tmp / "home").exists())

    def test_p2_file_name_must_equal_the_stem(self) -> None:
        self.write_profile_raw("mine", dict(p3(), name="other"))
        shown = paths.display(self.pstore.root / "mine.json", self.environ)
        self.assertEqual(
            self.profile_error(lambda: self.pstore.load("mine")),
            f"cannot load profile 'mine': {shown}: the file names 'other'; "
            "the name must equal the file stem",
        )
        # passing counterpart
        self.write_profile_raw("mine", dict(p3(), name="mine"))
        self.assertEqual(self.pstore.load("mine")["name"], "mine")

    def test_a_2x_composition_file_is_refused_with_the_migrate_hint(self) -> None:
        self.write_profile_raw("x", {"version": 1, "name": "x"})
        shown = paths.display(self.pstore.root / "x.json", self.environ)
        self.assertEqual(
            self.profile_error(lambda: self.pstore.load("x")),
            f"cannot load profile 'x': {shown}: version: unsupported profile version 1 "
            "(profiles are v2; legacy compositions migrate with 'claude-multi profile migrate')",
        )

    def test_unreadable_files_are_load_errors(self) -> None:
        path = self.write_profile_raw("x", p3())
        path.write_bytes(b"{not json")
        self.assertTrue(
            self.profile_error(lambda: self.pstore.load("x")).startswith(
                "cannot load profile 'x': "
            )
        )
        path.unlink()
        (path.parent / "x.json").symlink_to(self.tmp / "nowhere.json")
        message = self.profile_error(lambda: self.pstore.load("x"))
        self.assertIn("symlink", message)
        self.assertNotIn("[Errno", message)  # a StateError's strerror, not its str()

    def test_p3_missing_profile(self) -> None:
        self.assertEqual(
            self.profile_error(lambda: self.pstore.load("nope")),
            "profile 'nope' does not exist",
        )

    def test_unsafe_names_are_profile_errors(self) -> None:
        for call in (
            lambda: self.pstore.load("../x"),
            lambda: self.pstore.has_user("A"),
            lambda: self.pstore.delete("a/b"),
        ):
            with self.subTest(call=call):
                message = self.profile_error(call)
                self.assertTrue(message.startswith("unsafe state name "), message)

    def test_p1_new_and_duplicate_refuse_existing_targets(self) -> None:
        self.pstore.new(p3())
        for target in ("balanced", "p3"):
            with self.subTest(target=target):
                self.assertEqual(
                    self.profile_error(lambda: self.pstore.new(dict(p3(), name=target))),
                    f"profile target {target!r} already exists; choose another name",
                )
                self.assertEqual(
                    self.profile_error(lambda: self.pstore.duplicate("balanced", target)),
                    f"profile target {target!r} already exists; choose another name",
                )
        self.assertEqual(
            (self.config_root / "profiles" / "p3.json").read_bytes(),
            strict_json.pretty_file_bytes(p3()),
        )
        self.assertFalse(self.pstore.has_user("balanced"))

    def test_duplicate_drops_the_seed_marker(self) -> None:
        written = self.pstore.duplicate("balanced", "mine")
        expected = copy.deepcopy(_BUNDLE.seed_profiles["balanced"])
        expected.pop("seed")
        expected["name"] = "mine"
        self.assertEqual(written, expected)
        self.assertEqual(self.pstore.load("mine"), expected)
        self.assertFalse(self.pstore.has_user("balanced"))

    def test_rename_moves_user_profiles_and_refuses_seeds(self) -> None:
        self.pstore.new(p3())
        written = self.pstore.rename("p3", "p4")
        self.assertEqual(written, dict(p3(), name="p4"))
        self.assertFalse(self.pstore.has_user("p3"))
        self.assertEqual(self.pstore.load("p4"), dict(p3(), name="p4"))
        self.assertEqual(
            self.profile_error(lambda: self.pstore.rename("balanced", "mine")),
            "profile 'balanced' is a seed and cannot be renamed; duplicate it instead",
        )
        self.assertEqual(
            self.profile_error(lambda: self.pstore.rename("p4", "quality")),
            "profile target 'quality' already exists; choose another name",
        )
        self.assertEqual(
            self.profile_error(lambda: self.pstore.rename("nope", "other")),
            "profile 'nope' does not exist",
        )
        self.assertTrue(self.pstore.has_user("p4"))
        self.assertFalse(self.pstore.has_user("other"))

    def test_delete_refuses_seeds_and_removes_user_files(self) -> None:
        self.assertEqual(
            self.profile_error(lambda: self.pstore.delete("balanced")),
            "profile 'balanced' is a seed; edit it, or restore it with "
            "'claude-multi profile reseed balanced'",
        )
        self.pstore.new(p3())
        self.assertTrue(self.pstore.delete("p3"))
        self.assertFalse(self.pstore.delete("p3"))
        self.assertNotIn("p3", self.pstore.names())

    def test_reseed_restores_the_seed_bytes(self) -> None:
        self.pstore.update("balanced", lambda document: document["agents"].pop("cm-analyst-strong"))
        path = self.config_root / "profiles" / "balanced.json"
        seed_bytes = strict_json.pretty_file_bytes(_BUNDLE.seed_profiles["balanced"])
        self.assertNotEqual(path.read_bytes(), seed_bytes)
        restored = self.pstore.reseed("balanced")
        self.assertEqual(restored, _BUNDLE.seed_profiles["balanced"])
        self.assertEqual(path.read_bytes(), seed_bytes)
        self.assertEqual(
            self.profile_error(lambda: self.pstore.reseed("mine")),
            "profile 'mine' is not a seed profile",
        )

    def test_install_seeds_writes_only_absent_seeds(self) -> None:
        edited = copy.deepcopy(_BUNDLE.seed_profiles["balanced"])
        edited["description"] = "mine now"
        path = self.write_profile_raw("balanced", edited)
        edited_bytes = path.read_bytes()

        thread, outcome = _run_in_thread(self.pstore.install_seeds)
        thread.join(_JOIN_TIMEOUT)
        self.assertFalse(thread.is_alive(), "install_seeds deadlocked")
        self.assertEqual(outcome[0][0], "ok", outcome)
        self.assertEqual(
            outcome[0][1], [n for n in catalog.SEED_PROFILE_NAMES if n != "balanced"]
        )
        self.assertEqual(path.read_bytes(), edited_bytes)
        for name in catalog.SEED_PROFILE_NAMES:
            if name == "balanced":
                continue
            with self.subTest(seed=name):
                self.assertEqual(
                    (path.parent / f"{name}.json").read_bytes(),
                    strict_json.pretty_file_bytes(_BUNDLE.seed_profiles[name]),
                )
        self.assertEqual(self.pstore.install_seeds(), [])

    def test_install_seeds_on_an_empty_root_writes_all_seven(self) -> None:
        self.assertEqual(self.pstore.install_seeds(), list(catalog.SEED_PROFILE_NAMES))
        self.assertEqual(self.pstore.install_seeds(), [])
        self.assertEqual(stat.S_IMODE(self.pstore.root.stat().st_mode), 0o700)

    def test_seed_updates_reports_older_and_unmarked_seed_files(self) -> None:
        # Test-local shipped seed version 2 (the schema's minimum is 1, so an
        # installed version 1 is the "older" case).
        seed = copy.deepcopy(_BUNDLE.seed_profiles["balanced"])
        seed["seed"]["version"] = 2
        store = profile.ProfileStore(seeds={"balanced": seed}, environ=self.environ)
        self.assertEqual(store.seed_updates(), [])  # virtual seed: nothing installed
        installed = copy.deepcopy(_BUNDLE.seed_profiles["balanced"])
        installed["seed"]["version"] = 1  # explicit: the fixture seed's own marker moves
        self.write_profile_raw("balanced", installed)
        self.assertEqual(store.seed_updates(), [("balanced", 1, 2)])
        installed.pop("seed")
        self.write_profile_raw("balanced", installed)
        self.assertEqual(store.seed_updates(), [("balanced", None, 2)])
        self.write_profile_raw("balanced", seed)
        self.assertEqual(store.seed_updates(), [])  # current
        (store.root / "balanced.json").write_bytes(b"{broken")
        self.assertEqual(store.seed_updates(), [])  # unreadable files are skipped

    def test_referencing_lists_profile_and_field(self) -> None:
        self.pstore.new(p1())
        self.pstore.new(p2())
        self.pstore.new(p3())
        self.write_profile_raw("zbroken", {"version": 1})
        self.assertEqual(
            self.pstore.referencing("frontier"),
            [("p1", "agents.cm-analyst-strong"), ("p2", "agents.cm-reviewer-strong")],
        )
        self.assertEqual(self.pstore.referencing("anthropic-lead"), [("p1", "lead"), ("p2", "lead")])
        self.assertEqual(self.pstore.referencing("unused"), [])

    def test_update_mutates_under_one_lock_and_completes(self) -> None:
        contention: list[bool] = []

        def mutate(document: dict) -> None:
            probe = state.FileLock(self.config_root / "profiles")
            got = probe.acquire(blocking=False)
            contention.append(got)
            if got:
                probe.release()
            document["description"] = "edited"
            document["name"] = "renamed-by-mutate"  # the name is forced back

        thread, outcome = _run_in_thread(lambda: self.pstore.update("quality", mutate))
        thread.join(_JOIN_TIMEOUT)
        self.assertFalse(thread.is_alive(), "ProfileStore.update deadlocked")
        self.assertEqual(outcome[0][0], "ok", outcome)
        self.assertEqual(contention, [False])
        self.assertEqual(self.pstore.load("quality")["description"], "edited")
        self.assertEqual(self.pstore.load("quality")["name"], "quality")
        self.assertFalse(self.pstore.has_user("renamed-by-mutate"))

    def test_the_store_lock_is_profiles_lock_and_blocks_save(self) -> None:
        state.ensure_private_dir(self.config_root)
        held = state.FileLock(self.config_root / "profiles")
        self.assertEqual(held.lock_path, self.config_root / "profiles.lock")
        self.assertTrue(held.acquire(blocking=False))
        try:
            thread, outcome = _run_in_thread(lambda: self.pstore.save(p3()))
            time.sleep(0.2)
            self.assertTrue(thread.is_alive(), "save did not wait for the store lock")
            self.assertFalse(self.pstore.has_user("p3"))
        finally:
            held.release()
        thread.join(_JOIN_TIMEOUT)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome[0][0], "ok", outcome)
        self.assertTrue(self.pstore.has_user("p3"))

    def test_newer_state_marker_refuses_every_write(self) -> None:
        self.pstore.new(p3())
        self.write_marker()
        before = self.tree()
        calls = [
            lambda: self.pstore.save(p3()),
            lambda: self.pstore.new(dict(p3(), name="fresh")),
            lambda: self.pstore.update("p3", lambda document: None),
            lambda: self.pstore.duplicate("p3", "copy"),
            lambda: self.pstore.rename("p3", "moved"),
            lambda: self.pstore.delete("p3"),
            lambda: self.pstore.delete("balanced"),
            lambda: self.pstore.reseed("balanced"),
            lambda: self.pstore.install_seeds(),
        ]
        for index, call in enumerate(calls):
            with self.subTest(call=index):
                with self.assertRaises(sessions.StateMarkerError):
                    call()
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.pstore.load("p3"), p3())  # reads stay available

    def test_marker_on_a_fresh_root_creates_nothing(self) -> None:
        self.write_marker()
        with self.assertRaises(sessions.StateMarkerError):
            self.pstore.install_seeds()
        self.assertFalse((self.tmp / "home").exists())

    def test_xdg_config_home_moves_the_root(self) -> None:
        environ = dict(self.environ, XDG_CONFIG_HOME=str(self.tmp / "xdg"))
        store = profile.ProfileStore.for_catalog(_BUNDLE, environ)
        self.assertEqual(store.root, self.tmp / "xdg" / "claude-multi" / "profiles")
        self.assertEqual(profile.profiles_dir(environ), store.root)
        store.new(p3())
        self.assertTrue((self.tmp / "xdg" / "claude-multi" / "profiles" / "p3.json").is_file())
        self.assertFalse((self.tmp / "home").exists())


class SchemaTests(unittest.TestCase):
    def test_bindings_schema_is_package_data_not_a_catalog_document(self) -> None:
        self.assertNotIn("bindings", catalog.SCHEMA_NAMES)
        self.assertEqual(profile.bindings_schema_path().name, "bindings.schema.json")
        self.assertEqual(
            strict_json.load(profile.bindings_schema_path()),
            strict_json.load(FIXTURE_ROOT / "schemas" / "bindings.schema.json"),
        )
        self.assertNotIn("bindings", _BUNDLE.bundle)  # operator state: never bundled


if __name__ == "__main__":
    unittest.main()
