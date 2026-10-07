"""The Profiles screen: the list with its states, the keys of each row kind,
and every operation — new (from a shipped profile, from the card's profile,
or built from the connected providers), edit, copy, rename, delete, restore
a shipped version, make one the default, the fallback filter and a profile
file that cannot be loaded. A hermetic fixture runtime; no provider request
is sent and every write is checked on disk."""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, choices, profile, state, tui, views
from claude_multi.setup import defaults, model, profiles as setup_profiles

import _tui_fixture as fx
from _tui_render import golden_runtime, render
from test_tui import BACKSPACE, DOWN, END, HOME, RIGHT, UP, FakeWindow
import claude_multi.cli.screens.profiles as screens_profiles
import claude_multi.cli.text as cli_text

ESC = "\x1b"
ENTER = "\n"
CTRL_C = "\x03"
CTRL_S = "\x13"


def _flat(frame: str) -> str:
    return " ".join(line.strip(" |") for line in frame.splitlines())


class ProfilesCase(unittest.TestCase):
    runtime_kwargs: dict = {}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-screens-profiles-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runtime = fx.fixture_runtime(self.tmp / "root", **self.runtime_kwargs)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(fx.hermetic(self.runtime))
        stack.enter_context(mock.patch("claude_multi.cli.gateway_facts._doctor_now", lambda: fx.FIXED_NOW))
        self.runtime.profiles.install_seeds()

    # -- builders ------------------------------------------------------------

    def screen(self, **kwargs) -> screens_profiles._ProfilesScreen:
        kwargs.setdefault("now", fx.FIXED_NOW)
        return screens_profiles._ProfilesScreen(self.runtime, tui.MONO_PALETTE, **kwargs)

    @staticmethod
    def run_screen(screen, keys, *, height=24, width=80):
        win = FakeWindow(list(keys), height=height, width=width)
        return screen.run(win), win

    def rows(self, **kwargs):
        return setup_profiles.rows(self.runtime, **kwargs)

    def names(self, **kwargs) -> list[str]:
        return [row.name for row in self.rows(**kwargs)]

    def to(self, name: str, **kwargs) -> list:
        return [HOME] + [DOWN] * self.names(**kwargs).index(name)

    def yours(self, name: str = "mine", **changes) -> str:
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = name
        document.update(changes)
        self.runtime.profiles.save(document)
        return name

    def broken(self, name: str = "broken", text: bytes = b'{\n  "name": "broken",\n  oops\n}\n') -> Path:
        path = self.runtime.profiles.root / f"{name}.json"
        state.atomic_write(path, text)
        return path

    def fallback_seed(self) -> str:
        store = self.runtime.profiles
        return next(name for name in sorted(store.names())
                    if store.is_seed(name) and store.load(name).get("primary_provider"))

    def default_profile(self) -> str | None:
        return choices.read(self.runtime.environ).get("default_profile")

    def set_default(self, name: str | None) -> None:
        plan = defaults.set_default_plan(self.runtime, name)
        defaults.apply_default(self.runtime, plan, model.Confirmation.given(plan))


# ================================================================ the list


class ListTests(ProfilesCase):
    def test_rows_columns_order_and_details_at_80x24(self) -> None:
        self.yours()
        self.set_default("mine")
        rows = self.rows()
        screen = self.screen(selected="mine")
        win = FakeWindow([], height=24, width=80)
        screen._draw(win)
        frame = win.text()
        widths = screens_profiles.column_widths(rows, fx.FIXED_NOW)
        self.assertIn(screens_profiles.header_text(widths), frame)
        listed = [line for line in frame.splitlines()[3:3 + len(rows) + 1]]
        for row, line in zip(rows, listed[1:]):
            self.assertIn(screens_profiles.row_text(row, widths, fx.FIXED_NOW), line)
        mine = next(row for row in rows if row.name == "mine")
        self.assertEqual(screens_profiles.origin_text(mine), "yours")
        self.assertIn("default", mine.notes)
        self.assertIn(f"› {screens_profiles.row_text(mine, widths, fx.FIXED_NOW)}", frame)
        self.assertIn(views.clip(mine.description, 77), frame)
        self.assertIn(views.clip(mine.reason_line, 77), frame)
        # The order is the ready-first order of the default resolution.
        self.assertEqual([row.name for row in rows],
                         [choice.name for choice in defaults.profile_choices(self.runtime)])

    def test_used_shows_the_age_of_the_last_session(self) -> None:
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        row = next(row for row in self.rows() if row.name == catalog.DEFAULT_SEED)
        self.assertEqual(screens_profiles.used_text(row, fx.FIXED_NOW), "2 min")

    def test_an_edited_seed_reads_seed_edited(self) -> None:
        self.runtime.profiles.update(catalog.DEFAULT_SEED, lambda doc: doc.update(description="changed here"))
        row = next(row for row in self.rows() if row.name == catalog.DEFAULT_SEED)
        self.assertEqual(screens_profiles.origin_text(row), "seed·edited")

    def test_arrows_move_letters_do_not(self) -> None:
        names = self.names()
        screen = self.screen()
        self.run_screen(screen, [DOWN, "j", "k", ESC])
        self.assertEqual(screen.selected.name, names[1])
        self.run_screen(screen, [END, ESC])
        self.assertEqual(screen.selected.name, names[-1])
        self.run_screen(screen, [UP, HOME, ESC])
        self.assertEqual(screen.selected.name, names[0])

    def test_esc_returns_back_and_ctrl_c_interrupts(self) -> None:
        result, _win = self.run_screen(self.screen(), [ESC])
        self.assertEqual(result, screens_profiles.ProfilesResult("back"))
        with self.assertRaises(KeyboardInterrupt):
            self.run_screen(self.screen(), [CTRL_C])

    def test_the_floor(self) -> None:
        _result, win = self.run_screen(self.screen(), [ESC], height=screens_profiles.MIN_ROWS - 1)
        self.assertNotIn(cli_text.PROFILES_TITLE + "\n", win.frames[0] + "\n")
        self.assertIn(tui.TOO_SMALL.format(what="Profiles"), win.frames[0])
        _result, win = self.run_screen(self.screen(), [ESC], height=screens_profiles.MIN_ROWS,
                                       width=screens_profiles.MIN_COLS)
        self.assertIn(cli_text.PROFILES_TITLE, win.frames[0])

    def test_help(self) -> None:
        _result, win = self.run_screen(self.screen(), ["?", ESC, ESC])
        self.assertTrue(any(cli_text.PROFILES_HELP_TITLE in f for f in win.frames))
        self.assertTrue(any("Profiles decide which model leads" in f for f in win.frames))

    def test_moving_and_help_write_nothing(self) -> None:
        before = sorted((p.name, p.read_bytes()) for p in self.runtime.profiles.root.iterdir() if p.is_file())
        self.run_screen(self.screen(), [DOWN, UP, END, HOME, "?", ESC, "f", "F", ESC])
        after = sorted((p.name, p.read_bytes()) for p in self.runtime.profiles.root.iterdir() if p.is_file())
        self.assertEqual(before, after)
        self.assertIsNone(self.default_profile())


class KeybarTests(ProfilesCase):
    def bar(self, row, **kwargs) -> tuple:
        return screens_profiles.keybar(row, fallback_only=kwargs.get("fallback_only", False))

    def test_each_row_kind_has_its_bar_within_two_rows(self) -> None:
        self.yours()
        self.broken()
        self.broken(catalog.DEFAULT_SEED, b"broken")
        rows = {row.name: row for row in self.rows()}
        yours = self.bar(rows["mine"])
        self.assertEqual(yours, cli_text.PROFILES_KEYBAR_YOURS)
        seed = next(row for row in rows.values() if row.origin == "seed" and row.loadable)
        self.assertEqual(seed.seed_state, "current")
        self.assertNotIn(("U", "reseed"), self.bar(seed), "U only when the seed is not current")
        self.assertEqual(self.bar(rows["broken"]), cli_text.PROFILES_KEYBAR_UNLOADABLE)
        unloadable_seed = self.bar(rows[catalog.DEFAULT_SEED])
        self.assertIn(cli_text.PROFILES_RESTORE_BINDING, unloadable_seed)
        self.assertNotIn(("X", "delete"), unloadable_seed)
        filtered = self.bar(rows["mine"], fallback_only=True)
        self.assertIn(("F", cli_text.PROFILES_FILTER_OFF_LABEL), filtered)
        for bar in (yours, self.bar(seed), unloadable_seed, filtered, cli_text.PROFILES_KEYBAR_SEED,
                    self.bar(None)):
            with self.subTest(bar=bar):
                self.assertLessEqual(tui.KeyBar(bar)._rows_needed(80), 2)
                self.assertEqual([key for key, _label in bar[-2:]], ["?", "Esc"])

    def test_an_edited_seed_offers_u(self) -> None:
        self.runtime.profiles.update(catalog.DEFAULT_SEED, lambda doc: doc.update(description="changed here"))
        row = next(row for row in self.rows() if row.name == catalog.DEFAULT_SEED)
        self.assertIn(("U", "reseed"), self.bar(row))


# ================================================================ Enter, W, F


class NamedBindingsTests(ProfilesCase):
    def test_b_opens_named_bindings_with_the_editor_store_access(self) -> None:
        opened = []
        real = tui.NamedBindingsScreen

        class Recording(real):
            def run(self, win):
                opened.append(self)
                return None

        with mock.patch.object(tui, "NamedBindingsScreen", Recording):
            self.run_screen(self.screen(), ["b", ESC])
        self.assertEqual(len(opened), 1)
        callbacks = opened[0].callbacks
        self.assertIsNotNone(callbacks.set_binding)
        self.assertIsNotNone(callbacks.delete_binding)
        bindings, _conflicts = callbacks.load_bindings()
        self.assertEqual(dict(bindings), dict(self.runtime.bindings.bindings()))
        self.assertIn(("B", "bindings"), cli_text.PROFILES_KEYBAR_YOURS)
        self.assertIn(("B", "bindings"), screens_profiles.keybar(None, fallback_only=False))
        self.assertIn("B edits named bindings", cli_text.PROFILES_HELP)


class PermissiveEditorTests(ProfilesCase):
    def test_enter_picker_binds_new_line_despite_role_recommendations(self) -> None:
        lcat = self.runtime.lineup_catalog()
        key = next(key for key, entry in lcat.lines.items()
                   if entry.get("status") == "active" and isinstance(entry.get("lead"), dict))
        lines = copy.deepcopy(lcat.lines)
        lines[key].update(status="new", capabilities=[], roles=[])
        lcat = dataclasses.replace(lcat, lines=lines)
        eff = self.runtime.current_effective()
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document["name"] = "permissive-editor"
        document.pop("seed", None)
        for slot, focus in ((catalog.LEAD_ROLE, "lead"), ("cm-implementer-strong", "cm-implementer-strong")):
            with self.subTest(slot=slot):
                editor_state = tui.ProfileEditorState(document, cat=lcat, bindings={}, effective=eff,
                                                      origin=None, is_seed=False)
                editor = tui.ProfileEditorScreen(editor_state, palette=tui.MONO_PALETTE)
                editor.focus = focus
                rows = views.line_rows(lcat, eff, custom_ids=frozenset())
                picker = views.picker_rows(rows, slot=slot, bindings={}, lcat=lcat, eff=eff, current=None)
                target = next(item for item in picker.items if item.kind == "line" and item.key == key)
                self.assertTrue(target.selectable)
                steps = sum(item.selectable for item in picker.items[:picker.items.index(target)])
                with mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("no automatic smoke")), \
                        mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("no automatic diagnostics")):
                    self.run_screen(editor, [ENTER, HOME, *([DOWN] * steps), ENTER, CTRL_S, ESC], height=40, width=100)
                bound = editor_state.document["lead"] if slot == catalog.LEAD_ROLE else editor_state.document["agents"][slot]
                self.assertEqual(bound["model"], key)
                self.assertFalse(editor_state.evaluation.errors)
                self.assertTrue(any("admi" in warning.code for warning in editor_state.evaluation.lineup.warnings))
                self.assertNotIn(key, eff.admitted_lines)


class UseTests(ProfilesCase):
    def test_enter_uses_the_selected_profile(self) -> None:
        name = self.names()[1]
        result, _win = self.run_screen(self.screen(), [*self.to(name), ENTER])
        self.assertEqual(result, screens_profiles.ProfilesResult("use", name))

    def test_a_profile_that_cannot_be_loaded_is_not_used(self) -> None:
        self.broken()
        screen = self.screen(selected="broken")
        result, win = self.run_screen(screen, [ENTER, ESC])
        self.assertEqual(result.kind, "back")
        self.assertTrue(any(setup_profiles.PROFILE_REASON["unloadable"] in f for f in win.frames))

    def test_w_opens_get_started(self) -> None:
        result, _win = self.run_screen(self.screen(), ["w"])
        self.assertEqual(result.kind, "get-started")

    def test_f_lists_the_fallback_profiles(self) -> None:
        fallback = self.names(fallback_only=True)
        self.assertTrue(fallback)
        screen = self.screen()
        _result, win = self.run_screen(screen, ["F", ESC])
        frame = win.frames[-1]
        self.assertIn(cli_text.PROFILES_TITLE_FALLBACK, frame)
        self.assertEqual([row.name for row in screen.rows], fallback)
        self.assertIn(f"F {cli_text.PROFILES_FILTER_OFF_LABEL}", frame)
        self.run_screen(screen, ["f", ESC])
        self.assertEqual([row.name for row in screen.rows], self.names())

    def test_an_empty_fallback_list_says_how_to_make_one(self) -> None:
        screen = self.screen()
        with mock.patch.object(setup_profiles, "rows", return_value=()):
            _result, win = self.run_screen(screen, ["f", ESC])
        self.assertIn(views.clip(cli_text.PROFILES_NO_FALLBACK, 75), win.frames[-1])


# ================================================================ N


class NewTests(ProfilesCase):
    def name_keys(self, name: str) -> list:
        return [*([BACKSPACE] * 70), *name, ENTER, ENTER]

    def test_from_a_shipped_profile_edits_an_unsaved_draft(self) -> None:
        seed = self.fallback_seed()
        seeds = [name for name in readiness_order(self.runtime)]
        keys = [ "n", ENTER, *([DOWN] * seeds.index(seed)), ENTER,  # the shipped profile
                *self.name_keys("newone"), ENTER,  # name, then the fallback question: No (default)
                CTRL_S, ESC, ESC]
        screen = self.screen()
        _result, win = self.run_screen(screen, keys)
        saved = self.runtime.profiles.load("newone")
        self.assertNotIn("seed", saved)
        self.assertNotIn("primary_provider", saved, "a new profile is not a second fallback unless kept")
        source = self.runtime.profiles.load(seed)
        self.assertEqual(saved["lead"], source["lead"])
        self.assertTrue(any(cli_text.KEEP_FALLBACK_TITLE.format(provider=source["primary_provider"]) in f
                            for f in win.frames))

    def test_the_name_defaults_to_source_mine(self) -> None:
        seed = readiness_order(self.runtime)[0]
        _result, win = self.run_screen(self.screen(), ["n", ENTER, ENTER, ESC, ESC])
        modal = next(f for f in win.frames if cli_text.NAME_TITLE in f)
        self.assertIn(f"{seed}-mine", modal)

    def test_esc_in_the_editor_saves_nothing(self) -> None:
        seed = readiness_order(self.runtime)[0]
        before = set(self.runtime.profiles.names())
        keys = ["n", ENTER, ENTER, *self.name_keys("draft"), *([ENTER] if self.runtime.profiles.load(seed).get(
            "primary_provider") else []), ESC, ESC]
        self.run_screen(self.screen(), keys)
        self.assertEqual(set(self.runtime.profiles.names()), before)

    def test_from_the_card_profile_and_its_absence(self) -> None:
        self.yours("cardone")
        keys = ["n", DOWN, ENTER, *self.name_keys("fromcard"), CTRL_S, ESC, ESC]
        self.run_screen(self.screen(card_profile="cardone"), keys)
        self.assertEqual(self.runtime.profiles.load("fromcard")["lead"], self.runtime.profiles.load("cardone")["lead"])
        _result, win = self.run_screen(self.screen(card_profile=None), ["n", ESC, ESC])
        chooser = next(f for f in win.frames if cli_text.NEW_TITLE in f)
        self.assertIn(cli_text.NEW_ITEMS[1].format(card="—"), chooser)

    def test_name_rules(self) -> None:
        taken = self.names()[0]
        keys = ["n", ENTER, ENTER, *([BACKSPACE] * 70), *"Bad Name", ENTER, ENTER,
                *([BACKSPACE] * 70), *taken, ENTER, ENTER, ESC, ESC]
        _result, win = self.run_screen(self.screen(), keys)
        text = " ".join(_flat(f) for f in win.frames)
        self.assertIn(setup_profiles.NAME_BAD, text)
        self.assertIn(setup_profiles.NAME_TAKEN.format(name=taken), text)
        self.assertIn(setup_profiles.NAME_RULE, text)

    def test_starter_previews_saves_and_offers_the_default(self) -> None:
        keys = ["n", END, ENTER, ENTER, ENTER, "y", ENTER, ESC]
        screen = self.screen()
        _result, win = self.run_screen(screen, keys)
        self.assertTrue(any(cli_text.STARTER_PREVIEW_TITLE in f for f in win.frames))
        self.assertTrue(self.runtime.profiles.contains("starter"))
        self.assertTrue(any(cli_text.MAKE_DEFAULT_TITLE.format(name="starter") in f for f in win.frames))
        # Make default has the focus: Enter makes it the default.
        self.assertEqual(self.default_profile(), "starter")
        self.assertIn(cli_text.DEFAULT_SET.format(name="starter"), win.frames[-1])

    def test_starter_takes_the_next_free_name_and_not_now_keeps_the_default(self) -> None:
        self.yours("starter")
        keys = ["n", END, ENTER, ENTER, ENTER, "y", RIGHT, ENTER, ESC]
        self.run_screen(self.screen(), keys)
        self.assertTrue(self.runtime.profiles.contains("starter-2"))
        self.assertIsNone(self.default_profile())

    def test_starter_preview_n_writes_nothing(self) -> None:
        before = set(self.runtime.profiles.names())
        self.run_screen(self.screen(), ["n", END, ENTER, ENTER, ENTER, "n", ESC])
        self.assertEqual(set(self.runtime.profiles.names()), before)

    def test_starter_refusal(self) -> None:
        with mock.patch.object(setup_profiles, "plan_new_starter", side_effect=model.Refused("no lead")):
            _result, win = self.run_screen(self.screen(), ["n", END, ENTER, ENTER, ENTER, ESC])
        self.assertTrue(any(cli_text.STARTER_NO_LEAD_TUI[:60] in f for f in win.frames))


def readiness_order(runtime) -> list[str]:
    from claude_multi import readiness

    store = runtime.profiles
    seeds = [name for name in readiness.SEED_ORDER if store.is_seed(name)]
    return seeds + sorted(name for name in store.names() if store.is_seed(name) and name not in seeds)


# ================================================================ C, R, X


class CopyTests(ProfilesCase):
    def test_copy_drops_the_fallback_unless_kept(self) -> None:
        seed = self.fallback_seed()
        provider = self.runtime.profiles.load(seed)["primary_provider"]
        keys = [*self.to(seed), "c", ENTER, ENTER, ENTER, ESC]
        _result, win = self.run_screen(self.screen(), keys)
        copied = self.runtime.profiles.load(f"{seed}-copy")
        self.assertNotIn("primary_provider", copied)
        self.assertIn(setup_profiles.COPIED.format(source=seed, target=f"{seed}-copy"), win.frames[-1])
        keys = [*self.to(seed), "c", *([BACKSPACE] * 70), *"kept", ENTER, ENTER, RIGHT, ENTER, ESC]
        self.run_screen(self.screen(), keys)
        self.assertEqual(self.runtime.profiles.load("kept")["primary_provider"], provider)

    def test_the_fallback_question_esc_cancels(self) -> None:
        seed = self.fallback_seed()
        before = set(self.runtime.profiles.names())
        self.run_screen(self.screen(), [*self.to(seed), "c", ENTER, ENTER, ESC, ESC])
        self.assertEqual(set(self.runtime.profiles.names()), before)

    def test_an_unloadable_profile_is_not_copied(self) -> None:
        self.broken()
        _result, win = self.run_screen(self.screen(selected="broken"), ["c", ESC])
        self.assertIn(setup_profiles.PROFILE_REASON["unloadable"], win.frames[-1])


class RenameTests(ProfilesCase):
    def test_a_seed_keeps_its_name(self) -> None:
        seed = self.names()[0]
        _result, win = self.run_screen(self.screen(selected=seed), ["r", ESC])
        self.assertIn(setup_profiles.SEED_NO_RENAME.format(name=seed)[:70], _flat(win.frames[-1]))

    def test_rename_without_followers_goes_straight_to_the_name(self) -> None:
        self.yours()
        screen = self.screen(selected="mine")
        result, win = self.run_screen(screen, ["R", *([BACKSPACE] * 70), *"ours", ENTER, ENTER, ESC])
        self.assertFalse(any(cli_text.RENAME_TITLE.format(name="mine") in f for f in win.frames))
        self.assertTrue(self.runtime.profiles.contains("ours"))
        self.assertFalse(self.runtime.profiles.contains("mine"))
        self.assertEqual(result.renamed, (("mine", "ours"),))
        self.assertIn(setup_profiles.RENAMED.format(old="mine", new="ours", followers_note=""), _flat(win.frames[-1]))

    def test_followers_and_the_default_are_named_first_and_the_default_moves(self) -> None:
        self.yours()
        fx.v4_record(self.runtime, "mine", managed_id=fx.FIXED_ID)
        self.set_default("mine")
        screen = self.screen(selected="mine")
        _result, win = self.run_screen(screen, ["r", RIGHT, ENTER, *([BACKSPACE] * 70), *"ours", ENTER, ENTER,
                                                ESC, ESC])
        modal = _flat(next(f for f in win.frames if cli_text.RENAME_TITLE.format(name="mine") in f))
        self.assertIn("1 session(s) follow mine", modal)
        self.assertIn(setup_profiles.DEFAULT_MOVES.strip(), modal)
        self.assertEqual(self.default_profile(), "ours")
        self.assertTrue(self.runtime.profiles.contains("ours"))

    def test_cancel_renames_nothing(self) -> None:
        self.yours()
        self.set_default("mine")
        self.run_screen(self.screen(selected="mine"), ["r", ENTER, ESC])
        self.assertTrue(self.runtime.profiles.contains("mine"))
        self.assertEqual(self.default_profile(), "mine")


class DeleteTests(ProfilesCase):
    def test_a_seed_cannot_be_deleted(self) -> None:
        seed = self.names()[0]
        _result, win = self.run_screen(self.screen(selected=seed), ["x", ESC])
        self.assertIn(setup_profiles.SEED_NO_DELETE.format(name=seed)[:70], _flat(win.frames[-1]))

    def test_delete_keeps_a_copy_and_clears_the_default(self) -> None:
        self.yours()
        self.set_default("mine")
        before = self.runtime.profiles.raw_bytes("mine")
        screen = self.screen(selected="mine")
        result, win = self.run_screen(screen, ["x", RIGHT, ENTER, ESC])
        modal = _flat(next(f for f in win.frames if cli_text.DELETE_TITLE.format(name="mine") in f))
        self.assertIn("A copy is kept as", modal)
        self.assertIn(setup_profiles.DEFAULT_CLEARS.strip()[:40], modal)
        self.assertFalse(self.runtime.profiles.contains("mine"))
        backups = list(self.runtime.profiles.root.glob(".mine.removed-*.json"))
        self.assertEqual([path.read_bytes() for path in backups], [before])
        self.assertIsNone(self.default_profile())
        self.assertEqual(result.removed, ("mine",))
        self.assertIn("Deleted mine; a copy is kept as", _flat(win.frames[-1]))

    def test_cancel_is_the_default(self) -> None:
        self.yours()
        self.run_screen(self.screen(selected="mine"), ["x", ENTER, ESC])
        self.assertTrue(self.runtime.profiles.contains("mine"))

    def test_an_unloadable_file_is_deleted_byte_for_byte(self) -> None:
        path = self.broken()
        raw = path.read_bytes()
        self.run_screen(self.screen(selected="broken"), ["x", RIGHT, ENTER, ESC])
        self.assertFalse(path.exists())
        backups = list(self.runtime.profiles.root.glob(".broken.removed-*.json"))
        self.assertEqual([p.read_bytes() for p in backups], [raw])


# ================================================================ U


def _newer(runtime, name: str) -> dict:
    document = copy.deepcopy(runtime.catalog.seed_profiles[name])
    document["seed"]["version"] += 1
    document["description"] = "a newer shipped version"
    return document


class ReseedTests(ProfilesCase):
    def newer_store(self, *names: str) -> profile.ProfileStore:
        seeds = copy.deepcopy(self.runtime.catalog.seed_profiles)
        for name in names:
            seeds[name] = _newer(self.runtime, name)
        return profile.ProfileStore(seeds=seeds, environ=self.runtime.environ)

    @contextlib.contextmanager
    def store(self, newer):
        with mock.patch.object(type(self.runtime), "profiles", new_callable=mock.PropertyMock, return_value=newer):
            yield newer

    def test_your_own_profile_has_no_shipped_version(self) -> None:
        self.yours()
        _result, win = self.run_screen(self.screen(selected="mine"), ["u", ESC])
        self.assertIn(setup_profiles.NOT_A_SEED.format(name="mine")[:60], _flat(win.frames[-1]))

    def test_a_current_seed_has_nothing_to_restore(self) -> None:
        seed = self.names()[0]
        _result, win = self.run_screen(self.screen(selected=seed), ["U", ESC])
        self.assertIn(setup_profiles.RESEED_NOTHING.format(name=seed), win.frames[-1])

    def test_unedited_stale_seeds_update_together(self) -> None:
        names = [name for name in readiness_order(self.runtime)[:2]]
        with self.store(self.newer_store(*names)) as newer:
            self.assertEqual({newer.seed_state(name).label for name in names}, {"unedited-stale"})
            screen = self.screen(selected=names[0])
            _result, win = self.run_screen(screen, ["u", RIGHT, ENTER, ESC])
            modal = _flat(next(f for f in win.frames if "to the shipped version?" in f))
            self.assertIn(cli_text.REFRESH_TITLE.format(names=", ".join(sorted(names, key=list(
                newer.names()).index))), modal)
            self.assertIn("You have not changed them", modal)
            for name in names:
                self.assertEqual(newer.seed_state(name).kind, "current")
                self.assertTrue(list(newer.root.glob(f".{name}.pre-reseed-*.json")))
        self.assertIn(cli_text.PROFILES_REFRESHED.split("{")[0], " ".join(win.frames))

    def test_an_update_that_stops_part_way_still_offers_the_updated_ones(self) -> None:
        import claude_multi.cli.screens.propagation as screens_propagation

        names = [name for name in readiness_order(self.runtime)[:2]]
        real_write = profile.ProfileStore._write
        offered: list = []

        def write(store, document, name):
            if name == names[1]:
                raise state.StateError(28, "No space left on device")
            return real_write(store, document, name)

        with self.store(self.newer_store(*names)) as newer, \
                mock.patch.object(profile.ProfileStore, "_write", write), \
                mock.patch.object(screens_propagation, "run_propagation_screen",
                                  lambda runtime, win, palette, updated, **kwargs: offered.append(updated)):
            screen = self.screen(selected=names[0])
            self.run_screen(screen, ["u", RIGHT, ENTER, ESC])
            self.assertEqual(newer.seed_state(names[0]).kind, "current")
            self.assertEqual(newer.seed_state(names[1]).label, "unedited-stale")
        self.assertEqual(offered, [[names[0]]])
        self.assertEqual(screen.message_role, "warn")
        self.assertIn(f"Updated {names[0]} to the shipped version", screen.message)

    def refresh_with_failing_install_record(self, position: int):
        """Two shipped profiles updated together whose ``position``-th one's
        install record cannot be saved after its file was replaced: the
        screen and the propagation offers it makes."""

        import claude_multi.cli.screens.propagation as screens_propagation

        names = [name for name in readiness_order(self.runtime)[:2]]
        newer = self.newer_store(*names)
        order = [name for name in newer.names() if name in names]  # the order the update runs in
        real = profile.ProfileStore._record_seed_locked
        offered: list = []

        def record(store, written):
            if order[position] in written:
                raise state.StateError(28, "No space left on device")
            return real(store, written)

        with self.store(newer), mock.patch.object(profile.ProfileStore, "_record_seed_locked", record), \
                mock.patch.object(screens_propagation, "run_propagation_screen",
                                  lambda runtime, win, palette, updated, **kwargs: offered.append(updated)):
            screen = self.screen(selected=order[0])
            self.run_screen(screen, ["u", RIGHT, ENTER, ESC])
            kinds = {name: newer.seed_state(name).label for name in order}
        return order, screen, offered, kinds

    def test_an_install_record_failing_on_the_first_update_still_offers_it(self) -> None:
        order, screen, offered, kinds = self.refresh_with_failing_install_record(0)
        self.assertEqual(offered, [[order[0]]])
        self.assertEqual(kinds, {order[0]: "current", order[1]: "unedited-stale"})
        self.assertEqual(screen.message_role, "warn")
        self.assertIn(f"Updated {order[0]} to the shipped version", screen.message)
        self.assertIn(f"{order[1]} was not changed", screen.message)
        self.assertNotIn(f"{order[0]} was not changed", screen.message)

    def test_an_install_record_failing_on_a_later_update_offers_every_updated_one(self) -> None:
        order, screen, offered, kinds = self.refresh_with_failing_install_record(1)
        self.assertEqual(offered, [order])
        self.assertEqual(set(kinds.values()), {"current"})
        self.assertEqual(screen.message_role, "warn")
        self.assertIn(f"Updated {', '.join(order)} to the shipped version", screen.message)
        self.assertNotIn("not changed", screen.message)

    def test_an_edited_seed_shows_the_changes_and_keeps_your_version(self) -> None:
        name = readiness_order(self.runtime)[0]
        with self.store(self.newer_store(name)) as newer:
            newer.update(name, lambda doc: doc.update(description="mine now"))
            mine = newer.raw_bytes(name)
            _result, win = self.run_screen(self.screen(selected=name), ["u", "y", ESC, ESC])
            title = cli_text.RESEED_VIEW_TITLE.format(name=name, v=_newer(self.runtime, name)["seed"]["version"])
            view = _flat(next(f for f in win.frames if title in f))
            self.assertIn("description", view)
            backups = list(newer.root.glob(f".{name}.pre-reseed-*.json"))
            self.assertEqual([path.read_bytes() for path in backups], [mine])
            self.assertEqual(newer.seed_state(name).kind, "current")

    def test_n_restores_nothing(self) -> None:
        name = readiness_order(self.runtime)[0]
        self.runtime.profiles.update(name, lambda doc: doc.update(description="mine now"))
        before = self.runtime.profiles.raw_bytes(name)
        _result, win = self.run_screen(self.screen(selected=name), ["u", "n", ESC])
        self.assertEqual(self.runtime.profiles.raw_bytes(name), before)
        self.assertIn(model.NOTHING_CHANGED, win.frames[-1])

    def test_an_unloadable_seed_is_restored(self) -> None:
        name = readiness_order(self.runtime)[0]
        path = self.broken(name, b"broken")
        _result, win = self.run_screen(self.screen(selected=name), ["u", "y", ESC, ESC])
        self.assertTrue(any("Your file cannot be loaded; the shipped version replaces it." in f for f in win.frames))
        self.assertEqual(self.runtime.profiles.load(name)["name"], name)
        backups = list(self.runtime.profiles.root.glob(f".{name}.pre-reseed-*.json"))
        self.assertEqual([p.read_bytes() for p in backups], [b"broken"])
        self.assertTrue(path.exists())


# ================================================================ D


class DefaultTests(ProfilesCase):
    def test_d_on_a_connected_profile_makes_it_the_default(self) -> None:
        ready = next(row for row in self.rows() if row.ready)
        _result, win = self.run_screen(self.screen(selected=ready.name), ["d", ESC])
        self.assertEqual(self.default_profile(), ready.name)
        self.assertIn(cli_text.DEFAULT_SET.format(name=ready.name)[:70], win.frames[-1])

    def test_d_on_the_default_clears_it(self) -> None:
        ready = next(row for row in self.rows() if row.ready)
        self.set_default(ready.name)
        _result, win = self.run_screen(self.screen(selected=ready.name), ["D", ESC])
        self.assertIsNone(self.default_profile())
        self.assertIn(defaults.DEFAULT_CLEARED, win.frames[-1])

    def test_a_profile_not_connected_here_asks_first(self) -> None:
        other = next(row for row in self.rows() if row.loadable and not row.ready)
        _result, win = self.run_screen(self.screen(selected=other.name), ["d", ENTER, ESC])
        modal = _flat(next(f for f in win.frames if cli_text.DEFAULT_NOT_READY_TITLE.format(name=other.name) in f))
        self.assertIn("Make it the default anyway?", modal)
        self.assertIsNone(self.default_profile(), "Cancel is the default")
        self.run_screen(self.screen(selected=other.name), ["d", RIGHT, ENTER, ESC])
        self.assertEqual(self.default_profile(), other.name)

    def test_an_unloadable_profile_cannot_be_the_default(self) -> None:
        self.broken()
        self.run_screen(self.screen(selected="broken"), ["d", ESC])
        self.assertIsNone(self.default_profile())


# ================================================================ E and a file that cannot be loaded


class EditTests(ProfilesCase):
    def test_e_opens_the_editor_with_the_loaded_digest(self) -> None:
        self.yours()
        seen = []
        real = tui.ProfileEditorScreen.run

        def spy(editor, win):
            seen.append(editor.state.loaded_digest)
            return real(editor, win)

        with mock.patch.object(tui.ProfileEditorScreen, "run", spy):
            self.run_screen(self.screen(selected="mine"), ["e", ESC, ESC])
        self.assertEqual(seen, [self.runtime.profiles.digest("mine")])

    def test_an_unloadable_file_without_an_editor_says_how(self) -> None:
        self.broken()
        self.runtime.environ.pop("VISUAL", None)
        self.runtime.environ.pop("EDITOR", None)
        _result, win = self.run_screen(self.screen(selected="broken"), ["e", ESC])
        self.assertIn(cli_text.RAW_EDIT_NO_EDITOR[:70], _flat(win.frames[-1]))

    def editor_script(self, content: bytes) -> str:
        source = self.tmp / "content.json"
        source.write_bytes(content)
        script = self.tmp / "editor.py"
        script.write_text(f"import shutil, sys\nshutil.copyfile({str(source)!r}, sys.argv[1])\n")
        return f"{sys.executable} {script}"

    def test_raw_edit_saves_a_valid_file(self) -> None:
        self.broken()
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = "broken"
        self.runtime.environ["VISUAL"] = self.editor_script(json.dumps(document).encode())
        with mock.patch.object(tui, "suspended_curses", contextlib.nullcontext):
            _result, win = self.run_screen(self.screen(selected="broken"), ["e", ESC])
        self.assertEqual(self.runtime.profiles.load("broken")["lead"], document["lead"])
        self.assertIn("Saved profile 'broken'.", win.frames[-1])

    def test_raw_edit_keeps_an_invalid_edit_and_shows_why(self) -> None:
        path = self.broken()
        before = path.read_bytes()
        self.runtime.environ["VISUAL"] = self.editor_script(b"{still broken")
        with mock.patch.object(tui, "suspended_curses", contextlib.nullcontext):
            _result, win = self.run_screen(self.screen(selected="broken"), ["e", ESC, ESC])
        self.assertEqual(path.read_bytes(), before)
        view = next(f for f in win.frames if cli_text.RAW_EDIT_TITLE.format(name="broken") in f)
        self.assertIn("your edit is kept at", view)

    def drive_editor(self, *, then_save: bool = False, keys: tuple = ()):
        """The editor as the person drives it: the name becomes ``ours`` and
        is saved with Rename (``keys`` answer what the rename asks), then
        with ``then_save`` the description changes and is saved again."""

        def run(editor, win):
            state = editor.state
            state.set_general(name="ours")
            outcome = tui.ProfileEditorOutcome("rename", "ours", copy.deepcopy(state.document), state.origin,
                                               state.loaded_digest)
            ok, note = editor.callbacks.save(FakeWindow([*keys, ESC, ESC]), outcome)
            self.assertTrue(ok, note)
            state.mark_saved(outcome)
            if then_save:
                state.set_general(description="changed after the rename")
                again = tui.ProfileEditorOutcome("save", "ours", copy.deepcopy(state.document), "ours",
                                                 state.loaded_digest)
                ok, note = editor.callbacks.save(FakeWindow([ESC, ESC]), again)
                self.assertTrue(ok, note)
                state.mark_saved(again)
            return state.saved

        return mock.patch.object(tui.ProfileEditorScreen, "run", run)

    def test_e_renaming_the_default_moves_it(self) -> None:
        self.yours()
        self.set_default("mine")
        with self.drive_editor(keys=(RIGHT, ENTER)):
            result, _win = self.run_screen(self.screen(selected="mine"), ["e", ESC])
        self.assertEqual(self.default_profile(), "ours")
        self.assertFalse(self.runtime.profiles.contains("mine"))
        self.assertEqual(result.renamed, (("mine", "ours"),))

    def test_a_rename_saved_again_is_still_reported(self) -> None:
        self.yours()
        with self.drive_editor(then_save=True):
            result, _win = self.run_screen(self.screen(selected="mine"), ["e", ESC])
        self.assertEqual(self.runtime.profiles.load("ours")["description"], "changed after the rename")
        self.assertEqual(result.renamed, (("mine", "ours"),))

    def test_the_unloadable_row_names_its_file_and_line(self) -> None:
        self.broken()
        row = next(row for row in self.rows() if row.name == "broken")
        self.assertFalse(row.loadable)
        self.assertEqual(row.state_text, defaults.STATE_UNLOADABLE)
        self.assertIn("line 3", row.notes)
        _result, win = self.run_screen(self.screen(selected="broken"), [ESC], width=200)
        frame = win.frames[0]
        self.assertIn("broken.json line 3:", frame)
        self.assertIn(setup_profiles.PROFILE_REASON["unloadable"], frame)
        self.assertEqual(self.names()[-1], "broken", "unloadable profiles come last")


# ================================================================ goldens


def profiles_golden(root: Path) -> str:
    """Profiles at 80x24 with a profile of yours as the default, a session's
    last use and the shipped profiles (the first row selected)."""

    with golden_runtime(root) as runtime:
        runtime.profiles.install_seeds()
        document = copy.deepcopy(runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = "mine"
        document["description"] = "my everyday profile"
        runtime.profiles.save(document)
        plan = defaults.set_default_plan(runtime, "mine")
        defaults.apply_default(runtime, plan, model.Confirmation.given(plan))
        fx.v4_record(runtime, "mine", managed_id=fx.FIXED_ID)
        return render(lambda: screens_profiles._ProfilesScreen(runtime, tui.MONO_PALETTE, now=fx.FIXED_NOW))


def profiles_unloadable_golden(root: Path) -> str:
    """Profiles with a profile file that cannot be loaded, selected."""

    # The profiles live under HOME, so the file is drawn HOME-relative.
    config = Path(root) / "home" / ".config"
    with golden_runtime(root, environ_extra={"XDG_CONFIG_HOME": str(config)}) as runtime:
        runtime.profiles.install_seeds()
        state.atomic_write(runtime.profiles.root / "broken.json", b'{\n  "name": "broken",\n  oops\n}\n')
        return render(lambda: screens_profiles._ProfilesScreen(
            runtime, tui.MONO_PALETTE, selected="broken", now=fx.FIXED_NOW))


if __name__ == "__main__":
    unittest.main()
