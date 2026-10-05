"""The default profile: each step of the rule, its notices in both forms, the
unloadable default, the ready-first order of profile choices, the default
choice itself and the spend notes.

Readiness is injected per profile name (the rule is a pure function of it);
fixture catalog, temporary homes, no gateway, no provider.
"""

from __future__ import annotations

import argparse
import io
import threading
from unittest import mock

import test_cli  # module import: no test classes re-exported
from claude_multi import catalog, choices, profile, readiness, sessions, state
from claude_multi.cli import selection, session_facts
from claude_multi.cli.commands import profile as profile_cmd
from claude_multi.setup import defaults, model

NOT_READY = readiness.SlotReadiness(
    "cm-lead", readiness.BLOCKED, None,
    (readiness.Reason("served", "an alias is not served by the local gateway", readiness.SERVED_REMEDY),))
READY = readiness.SlotReadiness("cm-lead", readiness.READY)


def _record(name: str, cwd: str, stamp: str) -> dict:
    return {"version": sessions.RECORD_VERSION, "cwd": cwd, "profile": name,
            "created_at": stamp, "last_seen_at": stamp}


class DefaultCase(test_cli.CLITestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ready: set[str] = set()
        self.records: list[dict] = []
        self.any_line_ready = False
        runtime_cls = type(self.runtime)

        def profile_readiness(runtime, names, observations=None):
            found = {}
            for name in names:
                try:
                    runtime.profiles.load(name)
                except profile.ProfileError:
                    continue
                found[name] = (READY,) if name in self.ready else (NOT_READY,)
            return found

        def line_readiness(runtime, observations=None):
            return {"a-line": READY if self.any_line_ready else NOT_READY}

        def connected(runtime):
            # Something is connected once a line is ready (a key, a sign-in).
            return ("a-connection",) if self.any_line_ready else ()

        for patcher in (
            mock.patch.object(runtime_cls, "profile_readiness", profile_readiness),
            mock.patch.object(runtime_cls, "line_readiness", line_readiness),
            mock.patch.object(session_facts, "_session_records", lambda _runtime: list(self.records)),
            mock.patch("claude_multi.setup.status.connected", connected),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def mine(self, name: str = "mine") -> None:
        document = self.runtime.profiles.load("balanced")
        document.pop("seed")
        document["name"] = name
        self.runtime.profiles.save(document)

    def resolve(self) -> defaults.DefaultChoice:
        return defaults.resolve_default(self.runtime)


class ResolutionTests(DefaultCase):
    def test_each_step_wins_in_turn(self) -> None:
        here, elsewhere = str(self.runtime.cwd), "/elsewhere"
        self.mine("mine")
        self.mine("zeta")
        # 6. Nothing connected: the shipped default with the notice.
        choice = self.resolve()
        self.assertEqual((choice.name, choice.reason, choice.ready), (catalog.DEFAULT_SEED, "fallback", False))
        self.assertEqual(choice.notice, defaults.NONE_CONNECTED)
        # 5. The first connected profile of yours.
        self.ready = {"zeta", "mine"}
        self.assertEqual((self.resolve().name, self.resolve().reason), ("mine", "yours"))
        # 4. The first connected shipped profile in the seed order.
        self.ready |= {"openai", "claude"}
        self.assertEqual((self.resolve().name, self.resolve().reason), ("claude", "seed"))
        # 3. The most recently used connected profile anywhere.
        self.records = [_record("zeta", elsewhere, "2026-10-01T00:00:00Z"),
                        _record("openai", elsewhere, "2026-09-01T00:00:00Z")]
        self.assertEqual((self.resolve().name, self.resolve().reason), ("zeta", "recent"))
        # 2. The chosen default, when connected.
        choices.update(self.runtime.environ, default_profile="openai")
        self.assertEqual((self.resolve().name, self.resolve().reason, self.resolve().notice),
                         ("openai", "default", None))
        # 1. This directory's most recent profile, never replaced, ready or not.
        self.records.append(_record("quality", here, "2026-08-01T00:00:00Z"))
        choice = self.resolve()
        self.assertEqual((choice.name, choice.reason, choice.ready, choice.notice), ("quality", "here", False, None))
        self.assertEqual(choice.reason_text, "this directory's most recent")

    def test_the_skipped_default_is_named_once_a_later_step_wins(self) -> None:
        self.mine()
        self.ready = {"claude"}
        choices.update(self.runtime.environ, default_profile="mine")
        choice = self.resolve()
        self.assertEqual((choice.name, choice.reason), ("claude", "seed"))
        self.assertEqual(choice.notice, "! your default profile mine is not connected here (needs apply); "
                                        "using claude — P picks another")
        self.assertEqual(choice.notice_line, "! your default profile mine is not connected here (needs apply); "
                                             "using claude — claude-multi --profile NAME")
        choices.update(self.runtime.environ, default_profile="gone")
        choice = self.resolve()
        self.assertEqual(choice.notice, "! your default profile gone no longer exists; using claude — P → D sets another")
        self.assertEqual(choice.notice_line, "! your default profile gone no longer exists; using claude — "
                                             "claude-multi profile default NAME")
        state.atomic_write(self.runtime.profiles.root / "mine.json", b"{ broken")
        choices.update(self.runtime.environ, default_profile="mine")
        choice = self.resolve()
        self.assertEqual(choice.notice, "! your default profile mine cannot be loaded; using claude — P shows why")
        self.assertEqual(choice.notice_line, "! your default profile mine cannot be loaded; using claude — "
                                             "claude-multi profile list")
        # Nothing connected at all: the skipped default is still what is named.
        self.ready = set()
        self.assertEqual(self.resolve().notice,
                         f"! your default profile mine cannot be loaded; using {catalog.DEFAULT_SEED} — P shows why")

    def test_a_provider_name_with_braces_is_named_as_it_is(self) -> None:
        self.mine()
        choices.update(self.runtime.environ, default_profile="mine")
        needs_key = readiness.SlotReadiness("cm-lead", readiness.BLOCKED, None, (readiness.Reason(
            "credential", "the API key of provider (acme): not set", "set it"),))
        runtime_cls = type(self.runtime)

        def profile_readiness(runtime, names, observations=None):
            return {name: (READY,) if name in self.ready else (needs_key,) for name in names}

        for display in ("Acme {US}", "Acme {", "Acme }"):
            for ready, used in (({"claude"}, "claude"), (set(), catalog.DEFAULT_SEED)):
                # A connected alternative wins, or nothing is connected and the shipped default is used.
                self.ready = ready
                with self.subTest(display=display, used=used), \
                        mock.patch.object(runtime_cls, "profile_readiness", profile_readiness), \
                        mock.patch.object(runtime_cls, "readiness_providers",
                                          lambda runtime, display=display: {"acme": {"display": display}}):
                    choice = self.resolve()
                    self.assertEqual(choice.name, used)
                    self.assertEqual(choice.notice, f"! your default profile mine is not connected here "
                                                    f"(needs {display} key); using {used} — P picks another")

    def test_the_fallback_notices(self) -> None:
        self.assertEqual(defaults.NONE_CONNECTED, "! nothing is connected yet — W opens Get started")
        self.assertEqual(defaults.line_form(defaults.NONE_CONNECTED), "! nothing is connected yet — claude-multi setup")
        self.any_line_ready = True
        choice = self.resolve()
        self.assertEqual(choice.notice, f"! no profile is connected here (needs apply); using {catalog.DEFAULT_SEED} "
                                        "— P picks another, or P → N builds one from your connected providers")
        self.assertEqual(choice.notice_line,
                         f"! no profile is connected here (needs apply); using {catalog.DEFAULT_SEED} — "
                         "claude-multi --profile NAME, or claude-multi profile starter --apply")
        self.assertEqual(defaults.REASON_TEXT["fallback"], "nothing is connected")

    def test_the_selection_wrapper_keeps_the_decision(self) -> None:
        name, warning = selection.default_profile(self.runtime)
        self.assertEqual(name, catalog.DEFAULT_SEED)
        self.assertEqual(warning, defaults.NONE_CONNECTED.removeprefix("! "))
        self.assertEqual(self.runtime.selection_default.reason, "fallback")
        self.assertEqual(selection.remembered_profile(self.runtime), catalog.DEFAULT_SEED)


class UnloadableTargetTests(DefaultCase):
    def args(self, profile_name=None) -> argparse.Namespace:
        return argparse.Namespace(profile=profile_name, profile_file=None)

    def test_a_default_chosen_unloadable_profile_becomes_an_unloadable_target(self) -> None:
        self.runtime.profiles.install_seeds()
        state.atomic_write(self.runtime.profiles.root / "mine.json", b"{\n\n oops")
        self.records = [_record("mine", str(self.runtime.cwd), "2026-10-01T00:00:00Z")]
        target = selection._fresh_target(self.runtime, self.args(), io.StringIO(), read_only=False)
        self.assertEqual((target.kind, target.document, target.profile), ("unloadable", None, "mine"))
        error = self.runtime.selection_load_error
        self.assertIsInstance(error, profile.ProfileLoadError)
        self.assertEqual(error.line, 3)
        self.assertEqual(error.remedy, "claude-multi profile edit mine (or rm; a copy is kept)")
        with self.assertRaises(Exception) as caught:
            self.runtime.prepare(target, action="fresh", passthrough=[])
        self.assertIn("cannot load profile 'mine'", str(caught.exception))
        # Named explicitly, it keeps refusing, now with the path, line and fix.
        with self.assertRaises(profile.ProfileLoadError) as caught:
            selection._fresh_target(self.runtime, self.args("mine"), io.StringIO(), read_only=False)
        self.assertEqual(caught.exception.remedy, "claude-multi profile edit mine (or rm; a copy is kept)")


class ChoiceOrderTests(DefaultCase):
    def test_connected_then_not_connected_then_unloadable(self) -> None:
        self.runtime.profiles.install_seeds()
        self.mine("mine")
        state.atomic_write(self.runtime.profiles.root / "broken.json", b"{")
        self.ready = {"openai", "mine"}
        self.records = [_record("openai", "/elsewhere", "2026-10-01T00:00:00Z"),
                        _record("quality", str(self.runtime.cwd), "2026-09-01T00:00:00Z")]
        choices.update(self.runtime.environ, default_profile="mine")
        order = defaults.profile_choices(self.runtime)
        names = [c.name for c in order]
        self.assertEqual(names[:2], ["openai", "mine"])
        self.assertEqual(names[2], "quality")  # this directory's most recent, not connected
        self.assertEqual(names[-1], "broken")
        broken = order[-1]
        self.assertEqual((broken.loadable, broken.ready), (False, None))
        self.assertTrue(next(c for c in order if c.name == "mine").is_default)
        self.assertEqual(next(c for c in order if c.name == "openai").last_used, "2026-10-01T00:00:00Z")
        current = defaults.profile_choices(self.runtime, current="quality")
        self.assertEqual(current[0].name, "quality")
        self.assertEqual(selection.profile_pick_order(self.runtime, current="quality"), [c.name for c in current])

    def test_with_nothing_connected_the_pick_order_is_the_recency_order(self) -> None:
        self.mine("mine")
        self.records = [_record("mine", "/elsewhere", "2026-10-01T00:00:00Z"),
                        _record("quality", str(self.runtime.cwd), "2026-09-01T00:00:00Z")]
        here, elsewhere = selection._profile_recency(self.runtime)
        expected = selection._pick_order_from(self.runtime.profiles.names(), here, elsewhere)
        self.assertEqual(selection.profile_pick_order(self.runtime), expected)
        self.assertEqual(expected[:2], ["quality", "mine"])


class DefaultChoiceTests(DefaultCase):
    def test_set_clear_and_refusals(self) -> None:
        self.mine()
        plan = defaults.set_default_plan(self.runtime, "mine")
        self.assertEqual(plan.lines, ("Default profile: mine.",
                                      "note: mine is not connected here (needs apply); it is used wherever "
                                      "it is connected."))
        with self.assertRaises(model.Refused):
            defaults.apply_default(self.runtime, plan, model.Confirmation("sha256:x", None, "now"))
        defaults.apply_default(self.runtime, plan, model.Confirmation.given(plan))
        self.assertEqual(choices.read(self.runtime.environ).get("default_profile"), "mine")
        # A choice made elsewhere meanwhile: nothing written.
        plan = defaults.set_default_plan(self.runtime, None)
        choices.update(self.runtime.environ, default_profile="quality")
        with self.assertRaises(model.Stale):
            defaults.apply_default(self.runtime, plan, model.Confirmation.given(plan))
        plan = defaults.set_default_plan(self.runtime, None)
        self.assertEqual(plan.lines, ("The default profile is automatic again.",))
        defaults.apply_default(self.runtime, plan, model.Confirmation.given(plan))
        self.assertIsNone(choices.read(self.runtime.environ).get("default_profile"))
        with self.assertRaises(model.Refused) as caught:
            defaults.set_default_plan(self.runtime, "nobody")
        self.assertEqual(str(caught.exception), "profile 'nobody' does not exist — claude-multi profile list")
        state.atomic_write(self.runtime.profiles.root / "mine.json", b"{")
        with self.assertRaises(model.Refused):
            defaults.set_default_plan(self.runtime, "mine")

    def contended(self, operation) -> tuple[list, list]:
        """Run ``operation`` in a thread that waits for the choices lock
        this test holds; while it waits, another writer makes ``openai`` the
        default. Returns the operation's results and errors."""

        target = choices.path(self.runtime.environ)
        state.ensure_private_dir(target.parent)
        waiting = threading.Event()
        real_lock = state.FileLock

        class Signalling(real_lock):
            def acquire(self, blocking: bool = True) -> bool:
                if self.lock_path.name == target.name + ".lock":
                    waiting.set()
                return super().acquire(blocking)

        holder = real_lock(target)
        holder.acquire(blocking=True)
        results: list = []
        failures: list = []

        def run() -> None:
            try:
                results.append(operation())
            except Exception as exc:  # reported to the test
                failures.append(exc)

        with mock.patch.object(state, "FileLock", Signalling):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(waiting.wait(10), "the operation reached the choices lock")
                # The other writer holds the lock: it commits its choice, then releases.
                state.atomic_write(target, b'{\n  "default_profile": "openai",\n  "version": 1\n}\n')
            finally:
                holder.release()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        return results, failures

    def test_a_default_chosen_while_another_choice_waits_is_kept(self) -> None:
        self.mine()
        choices.update(self.runtime.environ, default_profile="mine")
        plan = defaults.set_default_plan(self.runtime, None)
        results, failures = self.contended(
            lambda: defaults.apply_default(self.runtime, plan, model.Confirmation.given(plan)))
        self.assertEqual(results, [])
        self.assertEqual([type(exc) for exc in failures], [model.Stale])
        self.assertEqual(choices.read(self.runtime.environ).get("default_profile"), "openai")

    def test_a_rename_never_moves_a_default_chosen_meanwhile(self) -> None:
        self.mine()
        choices.update(self.runtime.environ, default_profile="mine")
        results, failures = self.contended(lambda: defaults.move_default(self.runtime, "mine", "ours"))
        self.assertEqual((results, failures), ([False], []))
        self.assertEqual(choices.read(self.runtime.environ).get("default_profile"), "openai")

    def test_the_command(self) -> None:
        self.mine()
        self.ready = {"mine"}

        def run(**fields) -> tuple[int, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("sys.stderr", err):
                code = profile_cmd._profile_command(self.runtime, argparse.Namespace(**fields), "default",
                                                    input_stream=io.StringIO(), output_stream=out,
                                                    interactive=False)
            return code, out.getvalue(), err.getvalue()

        self.assertEqual(run(name=None, clear=False),
                         (0, "default profile: automatic\nhere: mine (first connected profile of yours)\n", ""))
        self.assertEqual(run(name="mine", clear=False), (0, "Default profile: mine.\n", ""))
        self.assertEqual(run(name=None, clear=False),
                         (0, "default profile: mine (set by you)\nhere: mine (your default)\n", ""))
        self.assertEqual(run(name=None, clear=True), (0, "The default profile is automatic again.\n", ""))
        self.assertEqual(run(name="nobody", clear=False),
                         (1, "", "claude-multi: profile 'nobody' does not exist — claude-multi profile list\n"))


class SpendNoteTests(test_cli.CLITestCase):
    def lineup(self, name: str) -> profile.ResolvedLineup:
        return profile.resolve(self.runtime.profiles.load(name), self.runtime.lineup_catalog())

    def test_one_note_per_pay_per_token_provider_backing_two_slots(self) -> None:
        notes = defaults.spend_notes(self.runtime, self.lineup("openrouter"))
        self.assertEqual(notes, ("lead + 8 agents bill per token to OpenRouter — set a spend limit in your "
                                 "OpenRouter account",))
        self.assertEqual(defaults.spend_notes(self.runtime, self.lineup("claude")), ())
        self.assertEqual(defaults.spend_notes(self.runtime, self.lineup("balanced")), ())

    def test_pay_per_token(self) -> None:
        providers = self.runtime.lineup_catalog().providers
        self.assertTrue(defaults.pay_per_token(providers["openrouter"]))
        self.assertFalse(defaults.pay_per_token(providers["anthropic"]))
        self.assertFalse(defaults.pay_per_token(providers["llm-local"]))
        self.assertEqual(defaults.SPEND_NOTE.format(who="2 agents", display="X"),
                         "2 agents bill per token to X — set a spend limit in your X account")


if __name__ == "__main__":
    import unittest

    unittest.main()
