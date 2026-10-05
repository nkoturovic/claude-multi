"""The Direct screen.

Every test runs on a hermetic ``_tui_fixture.fixture_runtime`` inside
``hermetic(runtime)``; nothing reads the host journal, ``/proc`` or the daemon
socket directory.  Rows, keys, efforts and providers derive from the loaded
fixture catalog: no fixture id is a literal here.  Screens are driven
on ``test_tui.FakeWindow`` in ``MONO_PALETTE``; every script ends with Esc
or a returning key.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from claude_multi import catalog, cli, custom, profile, settings, state, tui, views

import _tui_fixture as fx
import claude_multi.cli.text as cli_text
from _catalog import FIXTURE_ROOT
from test_screens_catalog import _fixture_copy
from test_tui import DOWN, END, HOME, LEFT, RIGHT, UP, FakeWindow


def _flat(frame: str) -> str:
    """A frame's text with borders stripped and wrapped lines joined."""

    return " ".join(" ".join(line.strip(" |").split()) for line in frame.splitlines())

SPEC = "the Direct screen contract"
ESC = "\x1b"
ENTER = "\n"
TAB = "\t"


class _DirectCase(unittest.TestCase):
    runtime_kwargs: dict = {}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-screens-direct-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runtime = self.make_runtime(self.tmp / "root", **self.runtime_kwargs)

    def make_runtime(self, root: Path, **kwargs) -> fx.ScreenRuntime:
        runtime = fx.fixture_runtime(root, **kwargs)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(fx.hermetic(runtime))
        stack.enter_context(mock.patch('claude_multi.cli.gateway_facts._doctor_now', lambda: fx.FIXED_NOW))
        return runtime

    def signed_in(self):
        """Every OAuth pool has a credential record (no ``not signed in`` marks)."""

        pools = {
            p["transport"]["pool"]
            for p in self.runtime.lineup_catalog().providers.values()
            if p["transport"]["kind"] == "oauth-pool"
        }
        return mock.patch('claude_multi.cli.gateway_facts._oauth_credential_records', lambda _rt: {p: 1 for p in pools})

    def screen(self, runtime=None, **kwargs) -> cli._DirectScreen:
        return cli._DirectScreen(runtime or self.runtime, palette=tui.MONO_PALETTE, **kwargs)

    @staticmethod
    def run_screen(screen, keys, *, height=30, width=100):
        win = FakeWindow(list(keys), height=height, width=width)
        return screen.run(win), win

    def expected_rows(self, runtime=None) -> list[views.LineRow]:
        runtime = runtime or self.runtime
        lcat = runtime.lineup_catalog()
        rows = views.line_rows(
            lcat, runtime.current_effective(),
            custom_ids=frozenset(custom.load_registry(runtime.environ)["models"]),
        )
        first = sorted((r for r in rows if r.lead_capable and r.offered and r.source == "catalog"),
                       key=lambda r: (r.provider_display.lower(), r.display.lower(), r.key))
        return first + [r for r in rows if r.lead_capable and r.offered and r.source == "custom"]

    def steps_to(self, screen: cli._DirectScreen, key: str) -> list:
        keys = [row.key for row in screen.rows]
        return [HOME] + [DOWN] * keys.index(key)

    def unmarked(self, screen: cli._DirectScreen, *, mode: str | None = None) -> views.DirectRow:
        row = next(
            (r for r in screen.rows if r.mark is None and r.source == "catalog" and (mode is None or r.mode == mode)),
            None,
        )
        self.assertIsNotNone(row, f"{SPEC}: the fixture has an unmarked {mode or 'catalog'} lead row")
        return row


class _DirectSecretMixin:
    """Secret helpers derived from the fixture providers."""

    runtime: fx.ScreenRuntime

    @staticmethod
    def secret_name(runtime, key: str) -> str:
        lcat = runtime.lineup_catalog()
        auth = lcat.providers[lcat.lines[key]["provider"]]["transport"]["auth"]
        return auth["secret_ref"].removeprefix("env:")

    @classmethod
    def grant(cls, runtime, key: str) -> None:
        path = Path(runtime.environ["CLAUDE_MULTI_SECRET_ENV"])
        state.atomic_write(path, path.read_bytes() + f"{cls.secret_name(runtime, key)}=tui-fixture-dummy\n".encode())


# ================================================================ rows


class DirectRowTests(_DirectCase):
    def test_rows_offered_lead_capable_any_class_and_custom(self) -> None:
        provider = next(
            pid for pid, p in sorted(self.runtime.catalog.providers.items())
            if p["transport"]["kind"] == "direct"
        )
        custom.add_model(
            self.runtime.environ, "direct-custom-line", wire_model="direct-custom-wire",
            provider=provider, context_tokens=262144, created_via="manual",
            catalog_providers=self.runtime.catalog.providers,
        )
        screen = self.screen()
        expected = self.expected_rows()
        self.assertEqual([row.key for row in screen.rows], [row.key for row in expected])
        classes = {row.lead_class for row in expected if row.source == "catalog"}
        self.assertGreater(len(classes), 1, f"{SPEC}: rows of every class")
        # The custom line is listed once, after the catalog rows.
        keys = [row.key for row in screen.rows]
        self.assertEqual(keys.count("direct-custom-line"), 1)
        self.assertEqual(keys[-1], "direct-custom-line")
        _result, win = self.run_screen(screen, [END, ESC])
        text = win.text()
        self.assertIn(cli.DIRECT_TITLE, text)
        self.assertIn(cli.DIRECT_CLASS_NOTE, text)
        custom_line = next(line for line in text.splitlines() if "direct-custom-line" in line)
        self.assertIn(" custom ", custom_line)
        self.assertTrue(custom_line.rstrip().endswith("(custom)"))
        self.assertIn("Enter launch · Tab save as profile · G providers · ? help · Esc back", text)

    def test_not_offered_lines_are_absent(self) -> None:
        lcat = self.runtime.lineup_catalog()
        rows = views.line_rows(lcat, self.runtime.current_effective(), custom_ids=frozenset())
        lead_provider = next(r.provider for r in rows if r.lead_capable and r.offered)
        self.runtime.settings_store.set_provider_enabled(lead_provider, False, catalog=self.runtime.catalog)
        screen = self.screen()
        self.assertTrue(screen.rows)
        self.assertNotIn(lead_provider, {row.provider for row in screen.rows})

    def test_gateway_rows_cycle_their_effort_and_client_rows_do_not(self) -> None:
        with self.signed_in():
            screen = self.screen()
            gateway = next((r for r in screen.rows if r.mark is None and r.source == "catalog"
                            and r.mode == "gateway" and len(r.efforts) > 1), None)
            self.assertIsNotNone(gateway, f"{SPEC}: a gateway row declares several efforts")
            start = gateway.efforts.index(gateway.default_effort)
            direction = RIGHT if start < len(gateway.efforts) - 1 else LEFT
            step = 1 if direction == RIGHT else -1
            chosen = gateway.efforts[start + step]
            result, win = self.run_screen(screen, [*self.steps_to(screen, gateway.key), direction, ENTER])
        self.assertIn(f"effort [{gateway.default_effort}]", win.frames[0])
        self.assertTrue(any(f"effort [{chosen}]" in frame for frame in win.frames))
        action, prepared = result
        self.assertEqual(action, "perform")
        self.assertEqual(prepared.record["applied"]["lead"]["key"], gateway.key)
        self.assertEqual(prepared.record["applied"]["lead"]["effort"], chosen)
        # A client-effort row launches at ultracode and ignores ← →.
        client = next((r for r in screen.rows if r.mode == "client" and r.source == "catalog"), None)
        self.assertIsNotNone(client)
        self.assertEqual(screen.effort_of(client), profile.ULTRACODE)
        self.assertNotIn("effort [", client.text())

    def test_preselect_derivation(self) -> None:
        with self.signed_in():
            screen = self.screen()
            default_lead = self.runtime.lineup_catalog().resolve_key(
                self.runtime.profiles.load(catalog.DEFAULT_SEED)["lead"]["model"]
            ).key
            keys = [row.key for row in screen.rows]
            self.assertIn(default_lead, keys)
            # 2. the default seed's lead when no direct record exists here
            self.assertEqual(keys[screen.selected], default_lead)
            # 1. the newest direct v4 record of this cwd wins
            other = next(k for k in keys if k != default_lead and screen.rows[keys.index(k)].source == "catalog")
            fx.v4_record(
                self.runtime, None, managed_id=fx.FIXED_ID,
                document=profile.ad_hoc_direct(other), last_seen=timedelta(minutes=1),
            )
            self.assertEqual([r.key for r in self.screen().rows][self.screen().selected], other)
        # 3. row 0 when neither applies (the default lead's provider is off)
        runtime = self.make_runtime(self.tmp / "off")
        provider = runtime.lineup_catalog().lines[default_lead]["provider"]
        runtime.settings_store.set_provider_enabled(provider, False, catalog=runtime.catalog)
        self.assertEqual(self.screen(runtime).selected, 0)

    def test_an_admitted_new_lead_line_is_listed_and_marked(self) -> None:
        # Marks and the detail reserve read lcat.lines /
        # catalog.line_selectors / _line_selectors, never the v1 view (which
        # omits New lines: a KeyError before this port).
        cat = catalog.load_catalog(FIXTURE_ROOT)
        bound: set[str] = set()
        for document in cat.seed_profiles.values():
            slots = [document.get("lead"), *document.get("agents", {}).values()]
            bound |= {s["model"] for s in slots if isinstance(s, dict) and "model" in s}
        key = next(
            (k for k, e in cat.lines.items()
             if "lead" in e["capabilities"] and isinstance(e.get("lead"), dict) and k not in bound),
            None,
        )
        self.assertIsNotNone(key, f"{SPEC}: the fixture has a lead-capable line no seed binds")
        runtime = self.make_runtime(self.tmp / "new", asset_root=_fixture_copy(self, new=(key,)))
        self.assertNotIn(key, [row.key for row in self.screen(runtime).rows], "New · Off is not offered")
        runtime.settings_store.admit_line(key, catalog=runtime.catalog)
        screen = self.screen(runtime)
        self.assertIn(key, [row.key for row in screen.rows])
        mark = cli._direct_marks(
            runtime, key, unavailable={}, oauth_records={}, served=frozenset()
        )
        self.assertIn(mark, (None, "not signed in", "not served"))
        self.assertGreaterEqual(cli._direct_detail_reserve(runtime, 80), 2)
        _result, win = self.run_screen(screen, [*self.steps_to(screen, key), ESC])
        self.assertIn("in-session: /model", win.text())


    def test_no_rows_never_index_or_launch(self) -> None:
        # Restores OrdinaryScreenTuiTests.test_empty_catalog_never_indexes_or_launches:
        # every lead provider off leaves no row; keys and Enter are inert.
        lcat = self.runtime.lineup_catalog()
        for provider in sorted({e["provider"] for e in lcat.lines.values() if "lead" in e["capabilities"]}):
            self.runtime.settings_store.set_provider_enabled(provider, False, catalog=self.runtime.catalog)
        screen = self.screen()
        self.assertEqual(screen.rows, ())
        result, win = self.run_screen(screen, ["j", "k", DOWN, RIGHT, ENTER, TAB, ESC])
        self.assertIsNone(result)
        self.assertIn(cli_text.DIRECT_EMPTY, win.text())
        self.assertEqual(self.runtime.launches, [])
        self.assertEqual(list(self.runtime.session_store.sessions_dir.glob("*.json")), [])


# ================================================================ marks


class DirectMarkTests(_DirectCase):
    def test_malformed_secret_file_marks_rows_and_never_raises(self) -> None:
        secret = Path(self.runtime.environ["CLAUDE_MULTI_SECRET_ENV"])
        state.atomic_write(secret, b"\xff\xfe not an env file\n")
        with self.signed_in():
            screen = self.screen()
            direct_rows = [
                r for r in screen.rows
                if self.runtime.lineup_catalog().providers[r.provider]["transport"]["kind"] == "direct"
            ]
            self.assertTrue(direct_rows, f"{SPEC}: a direct-key lead row")
            self.assertTrue(all(r.mark == "key missing" for r in direct_rows))
            _result, win = self.run_screen(screen, [*self.steps_to(screen, direct_rows[0].key), ESC])
        self.assertIn("(key missing)", win.text())
        display = self.runtime.lineup_catalog().providers[direct_rows[0].provider]["display"]
        self.assertIn(cli_text.DIRECT_REASON_KEY_INVALID.format(display=display), _flat(win.text()))

    def test_missing_secret_marks_and_the_enter_recheck_asks(self) -> None:
        runtime = self.make_runtime(self.tmp / "nosecret", secrets=())
        with self.signed_in():
            screen = self.screen(runtime)
            row = next(r for r in screen.rows if r.mark == "key missing")
            result, win = self.run_screen(screen, [*self.steps_to(screen, row.key), ENTER, ENTER, ESC])
        self.assertIsNone(result)  # Close (the only button), then Esc
        modal = next(f for f in win.frames if "has no API key" in f)
        # The box's interior only (list rows show beside it).
        flat = " ".join(line.split("|")[1].strip() for line in modal.splitlines() if line.count("|") >= 2)
        display = runtime.lineup_catalog().providers[row.provider]["display"]
        self.assertIn(cli_text.DIRECT_KEY_MISSING_BODY.format(display=display), flat)
        self.assertNotIn("Launch anyway", modal)
        self.assertEqual(runtime.launches, [])

    def test_a_missing_key_offers_no_launch_anyway(self) -> None:
        # Perform refuses a plan with secret problems, so the key-missing
        # modal closes only; the screen stays and nothing launches.
        runtime = self.make_runtime(self.tmp / "nosecret-anyway", secrets=())
        with self.signed_in():
            screen = self.screen(runtime)
            row = next(r for r in screen.rows if r.mark == "key missing")
            result, win = self.run_screen(
                screen, [*self.steps_to(screen, row.key), ENTER, RIGHT, ENTER, ESC]
            )
        self.assertIsNone(result)
        self.assertEqual(cli_text.DIRECT_KEY_MISSING_BUTTONS, (("Close", None),))
        self.assertTrue(any("has no API key" in f for f in win.frames))
        self.assertEqual(runtime.launches, [])

    def test_sign_in_mark_and_modal(self) -> None:
        screen = self.screen()  # the fixture has no OAuth credential record
        row = next((r for r in screen.rows if r.mark == "not signed in"), None)
        self.assertIsNotNone(row, f"{SPEC}: an OAuth-pool lead row")
        result, win = self.run_screen(screen, [*self.steps_to(screen, row.key), ENTER, RIGHT, ENTER])
        self.assertTrue(any(cli.DIRECT_SIGNIN_TITLE in f for f in win.frames))
        self.assertIn("Enter offers the sign-in", _flat(win.frames[1] + win.frames[0]))
        modal = next(f for f in win.frames if cli.DIRECT_SIGNIN_TITLE in f)
        self.assertIn("[ Sign in now ]", modal)
        action, prepared = result  # "Launch anyway"
        self.assertEqual((action, prepared.record["applied"]["lead"]["key"]), ("perform", row.key))

    def test_sign_in_now_runs_the_shared_sign_in_and_stays(self) -> None:
        import claude_multi.cli.screens.signin as screens_signin

        screen = self.screen()
        row = next(r for r in screen.rows if r.mark == "not signed in")
        provider = self.runtime.lineup_catalog().lines[row.key]["provider"]
        with mock.patch.object(screens_signin, "run_sign_in_flow",
                               side_effect=lambda *a, say=None, **k: say("signed in (fixture)", "accent")) as flow:
            result, _win = self.run_screen(screen, [*self.steps_to(screen, row.key), ENTER, RIGHT, RIGHT, ENTER, ESC])
        self.assertIsNone(result)
        self.assertEqual(flow.call_args.args[3], provider)
        self.assertEqual(screen.rows[screen.selected].key, row.key)
        self.assertEqual(screen.message, "signed in (fixture)")
        self.assertEqual(self.runtime.launches, [])

    def test_not_served_marks_only_with_a_known_served_set(self) -> None:
        lcat = self.runtime.lineup_catalog()
        with self.signed_in():
            victim = self.unmarked(self.screen())
        selectors = {s.removesuffix("[1m]") for _e, s, _c in catalog.line_selectors(lcat.lines[victim.key])}
        served = set(fx.served_selectors(FIXTURE_ROOT)) - selectors
        runtime = self.make_runtime(self.tmp / "unserved", served=served)
        with self.signed_in():
            row = next(r for r in self.screen(runtime).rows if r.key == victim.key)
        self.assertEqual(row.mark, "not served")
        down = self.make_runtime(self.tmp / "down", gateway_down=True)
        with self.signed_in():
            self.assertFalse([r for r in self.screen(down).rows if r.mark == "not served"])


    def test_an_unserved_row_asks_before_launching_and_cancel_stays(self) -> None:
        # Restores OrdinaryScreenTuiTests.test_unserved_row_opens_not_served_modal and
        # UnservedMarkingTests (marking + detail render): only the victim is marked.
        lcat = self.runtime.lineup_catalog()
        with self.signed_in():
            before = self.screen()
            victim = self.unmarked(before)
        selectors = {s.removesuffix("[1m]") for _e, s, _c in catalog.line_selectors(lcat.lines[victim.key])}
        runtime = self.make_runtime(self.tmp / "unserved-modal", served=set(fx.served_selectors(FIXTURE_ROOT)) - selectors)
        with self.signed_in():
            screen = self.screen(runtime)
            marks = {row.key: row.mark for row in screen.rows}
            result, win = self.run_screen(screen, [*self.steps_to(screen, victim.key), ENTER, ENTER, ESC])
        self.assertIsNone(result)
        self.assertEqual(marks[victim.key], "not served")
        for row in before.rows:
            if row.key != victim.key and row.mark is None:
                self.assertIsNone(marks[row.key], row.key)
        self.assertIn("(not served)", win.frames[1])
        self.assertIn("not served by the running gateway", _flat(win.frames[1]))
        self.assertIn("G → P applies your changes", _flat(win.frames[1]))
        self.assertTrue(any(cli.DIRECT_UNSERVED_TITLE in f for f in win.frames))
        self.assertEqual(runtime.launches, [])


class DirectSecretRecheckTests(_DirectCase, _DirectSecretMixin):
    """The Enter recheck of the 2.x picker (OrdinaryScreenTuiTests) on Direct."""

    def test_the_reason_is_rechecked_at_enter_never_cached(self) -> None:
        # Restores test_reason_rechecked_at_enter_not_cached: the secret appears
        # while the screen is up; Enter launches with no modal.
        runtime = self.make_runtime(self.tmp / "recheck", secrets=())
        with self.signed_in():
            screen = self.screen(runtime)
            row = next(r for r in screen.rows if r.mark == "key missing")
            self.grant(runtime, row.key)
            result, win = self.run_screen(screen, [*self.steps_to(screen, row.key), ENTER])
        self.assertFalse(any("has no API key" in f for f in win.frames))
        action, prepared = result
        self.assertEqual((action, prepared.record["applied"]["lead"]["key"]), ("perform", row.key))

    def test_a_row_unblocks_once_its_secret_appears(self) -> None:
        # Restores test_row_unblocks_once_secret_appears.
        runtime = self.make_runtime(self.tmp / "unblock", secrets=())
        with self.signed_in():
            row = next(r for r in self.screen(runtime).rows if r.mark == "key missing")
            self.grant(runtime, row.key)
            screen = self.screen(runtime)
            self.assertIsNone(next(r for r in screen.rows if r.key == row.key).mark)
            result, _win = self.run_screen(screen, [*self.steps_to(screen, row.key), ENTER])
        self.assertEqual(result[0], "perform")

    def test_the_full_reason_and_the_modal_survive_the_minimum_width(self) -> None:
        # Restores test_full_reason_survives_minimum_width and
        # test_confirm_modal_intact_at_minimum_width: at DIRECT_MIN_COLS and the
        # floor height the whole secret name wraps, never truncates, in the
        # detail and inside the Modal's bordered rows.
        runtime = self.make_runtime(self.tmp / "narrow", secrets=())
        with self.signed_in():
            screen = self.screen(runtime)
            row = next(r for r in screen.rows if r.mark == "key missing")
            name = runtime.lineup_catalog().providers[row.provider]["display"]
            rows, cols = screen.min_size(cli.DIRECT_MIN_COLS)
            steps = self.steps_to(screen, row.key)
            _result, detail = self.run_screen(screen, [*steps, ESC], height=rows, width=cols)
            result, win = self.run_screen(self.screen(runtime), [*steps, ENTER, ENTER, ESC], height=rows, width=cols)
        self.assertIsNone(result)
        self.assertNotIn("Terminal too small", detail.text())
        self.assertIn(name, detail.text())
        modal = next(f for f in win.frames if "has no API key" in f)
        self.assertTrue(any(name in line and line.lstrip().startswith("|") for line in modal.splitlines()))
        self.assertEqual(runtime.launches, [])


# ================================================================ Enter / Tab / G / ? / choose


class DirectLaunchTests(_DirectCase):
    def test_passthrough_threads_into_the_prepared_argv(self) -> None:
        with self.signed_in():
            screen = self.screen(passthrough=["--verbose"])
            row = self.unmarked(screen)
            result, _win = self.run_screen(screen, [*self.steps_to(screen, row.key), ENTER])
        action, prepared = result
        self.assertEqual(action, "perform")
        self.assertIn("--verbose", prepared.result.argv)
        self.assertEqual(prepared.target.kind, "ad-hoc")
        self.assertIsNone(prepared.record["profile"])
        self.assertEqual(prepared.record["applied"]["agents"], {})
        self.assertEqual(self.runtime.launches, [], "the screen never performs")

    def test_prepare_failure_is_a_message_and_the_screen_stays(self) -> None:
        with self.signed_in(), mock.patch.object(
            self.runtime, "prepare", side_effect=cli.LaunchPlanError(["lead.model: boom"])
        ):
            screen = self.screen()
            row = self.unmarked(screen)
            result, win = self.run_screen(screen, [*self.steps_to(screen, row.key), ENTER, ESC])
        self.assertIsNone(result)
        self.assertTrue(any("profile is blocked: - lead.model: boom" in f for f in win.frames))
        self.assertIn(cli.DIRECT_TITLE, win.frames[-1])

    def test_tab_saves_a_lead_only_profile_without_a_seed_marker(self) -> None:
        with self.signed_in():
            screen = self.screen()
            row = self.unmarked(screen)
            keys = [*self.steps_to(screen, row.key), TAB, ENTER, ENTER]
            _result, win = self.run_screen(screen, [*keys, *keys[-3:], ESC])
        name = f"direct-{row.key}"
        document = self.runtime.profiles.load(name)
        self.assertNotIn("seed", document)
        self.assertEqual(document["agents"], {})
        self.assertEqual(document["lead"]["model"], row.key)
        self.assertTrue(any(cli.DIRECT_SAVED.format(name=name) in f for f in win.frames))
        # P1: the second save under the same name is refused with the store text.
        self.assertTrue(any("already exists" in f for f in win.frames))

    def test_tab_on_a_custom_row_is_refused_with_the_e7_text(self) -> None:
        provider = next(
            pid for pid, p in sorted(self.runtime.catalog.providers.items())
            if p["transport"]["kind"] == "direct"
        )
        custom.add_model(
            self.runtime.environ, "direct-custom-e7", wire_model="direct-custom-e7-wire",
            provider=provider, context_tokens=262144, created_via="manual",
            catalog_providers=self.runtime.catalog.providers,
        )
        screen = self.screen()
        _result, win = self.run_screen(screen, [END, TAB, ESC])
        self.assertTrue(any(cli.DIRECT_CUSTOM_REFUSAL.format(key="direct-custom-e7") in f for f in win.frames))
        self.assertFalse(self.runtime.profiles.has_user("direct-direct-custom-e7"))

    def test_g_opens_providers_and_rebuilds_the_rows(self) -> None:
        with mock.patch('claude_multi.cli.screens.providers.run_providers_screen') as providers:
            _result, _win = self.run_screen(self.screen(), ["g", ESC])
        providers.assert_called_once()

    def test_help_is_the_exact_text(self) -> None:
        _result, win = self.run_screen(self.screen(), ["?", ENTER, ESC], width=120)
        modal = next(f for f in win.frames if "direct — help" in f)
        self.assertIn("No profile is saved. The session is recorded and resumable.", modal)
        rule = (
            "/model lists only the lead set; another provider family asks first (Alt+P and "
            "/config block instead); a model outside the set is refused — relaunch with a "
            "profile whose lead is that model."
        )
        self.assertIn(rule, cli.DIRECT_HELP.splitlines())
        self.assertIn(rule, cli.QUICK_HELP.splitlines())


class DirectCommandTests(_DirectCase):
    """``claude-multi direct`` carries ``--no-subagents`` on every
    path, and Ctrl-C in its line picker is an interrupt."""

    def args(self, **changes):
        import argparse

        values = dict(direct_model=None, direct_resume=None, direct_continue=False, print_launch=False,
                      no_subagents=True, force=False, line=False, no_color=True)
        values.update(changes)
        return argparse.Namespace(**values)

    def run_command(self, args, text: str = "") -> int:
        import io
        import claude_multi.cli.launch_flow as launch_flow

        return launch_flow._direct_command(self.runtime, args, input_stream=io.StringIO(text),
                                           output_stream=io.StringIO(), interactive=True, passthrough=[])

    def assert_no_subagents(self) -> None:
        prepared = self.runtime.launches[-1]
        self.assertIs(prepared.record["applied"]["no_subagents"], True)
        self.assertEqual(prepared.result.env_set.get("CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS"), "1")

    def test_the_curses_picker_carries_no_subagents(self) -> None:
        import claude_multi.cli.screens.common as screens_common

        with self.signed_in():
            probe = self.screen()
            row = self.unmarked(probe)
            keys = [*self.steps_to(probe, row.key), ENTER]
            with mock.patch.object(screens_common, "_curses_ok", return_value=True), \
                    mock.patch.object(screens_common, "_run_screen_curses",
                                      side_effect=lambda draw, **_kw: draw(FakeWindow(keys, height=30, width=100))):
                code = self.run_command(self.args())
        self.assertEqual(code, 0)
        self.assertEqual(self.runtime.launches[-1].lineup.lead.binding.key, row.key)
        self.assert_no_subagents()

    def test_the_line_picker_and_an_explicit_model_carry_no_subagents(self) -> None:
        with self.signed_in():
            row = self.unmarked(self.screen())
            self.assertEqual(self.run_command(self.args(line=True), f"{row.key}\n"), 0)
            self.assert_no_subagents()
            self.assertEqual(self.run_command(self.args(direct_model=row.key)), 0)
            self.assert_no_subagents()
        self.assertEqual(len(self.runtime.launches), 2)

    def test_ctrl_c_in_the_line_picker_is_an_interrupt(self) -> None:
        import contextlib
        import io

        class Interrupting(io.StringIO):
            def readline(self, *_args):
                raise KeyboardInterrupt

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["direct", "--line"], runtime=self.runtime, input_stream=Interrupting(),
                            output_stream=io.StringIO(), interactive=True)
        self.assertEqual(code, 130)
        self.assertIn("cancelled — nothing was launched", err.getvalue())
        self.assertEqual(self.runtime.launches, [])


class DirectGatewayDispatchTests(_DirectCase):
    """W on Direct opens the gateway's actions while the gateway is not served."""

    runtime_kwargs = {"gateway_down": True}

    def test_w_start_now_starts_a_stopped_gateway(self) -> None:
        view = fx.GatewayViewStub(start=True)
        screen = self.screen()
        self.assertIsNone(screen.served)
        with fx.gateway_actions_seam(view) as acted:
            _result, win = self.run_screen(screen, ["w", ENTER, ESC])
        self.assertEqual(acted, [(view, "start")])
        self.assertTrue(any("[ Start now ]" in frame for frame in win.frames))
        self.assertTrue(any("gateway start: fixture done" in frame for frame in win.frames))


class DirectKeyTests(_DirectCase):
    """Navigation and Esc of the 2.x picker (OrdinaryScreenTuiTests) on Direct."""

    def test_arrow_keys_match_j_and_k(self) -> None:
        with self.signed_in():
            arrows, jk = self.screen(), self.screen()
            self.assertGreater(len(arrows.rows), 2)
            self.assertIsNone(self.run_screen(arrows, [HOME, DOWN, DOWN, UP, ESC])[0])
            self.assertIsNone(self.run_screen(jk, [HOME, "j", "j", "k", ESC])[0])
            self.assertEqual((arrows.selected, jk.selected), (1, 1))
            target = self.unmarked(self.screen())
            index = [row.key for row in self.screen().rows].index(target.key)
            by_arrows = self.run_screen(self.screen(), [HOME] + [DOWN] * index + [ENTER])[0]
            by_jk = self.run_screen(self.screen(), [HOME] + ["j"] * index + [ENTER])[0]
        self.assertEqual(by_arrows[1].record["applied"]["lead"]["key"], target.key)
        self.assertEqual(by_jk[1].record["applied"]["lead"]["key"], target.key)

    def test_the_bar_names_effort_on_gateway_rows_and_w_while_the_gateway_is_down(self) -> None:
        screen = self.screen()
        for index, row in enumerate(screen.rows):
            screen.selected = index
            with self.subTest(row=row.key):
                bar = dict(screen._bindings())
                self.assertEqual("← →" in bar, row.mode == "gateway" and bool(row.efforts))
                self.assertNotIn("W", bar)
                self.assertEqual(list(bar)[-2:], ["?", "Esc"])
        down = self.screen(self.make_runtime(self.tmp / "down-bar", gateway_down=True))
        self.assertIn(cli_text.DIRECT_GATEWAY_BINDING, down._bindings())
        with mock.patch("claude_multi.cli.screens.gateway_actions.dialog",
                        return_value="gateway ready: fixture") as dialog:
            _result, win = self.run_screen(down, ["W", ESC])
        dialog.assert_called_once()
        self.assertIn("W gateway", win.frames[0])
        self.assertIn("W — the local gateway", cli_text.DIRECT_HELP)

    def test_help_tells_a_saved_lead_only_profile_from_a_direct_launch(self) -> None:
        self.assertIn("listed in Profiles and launched from the card", cli_text.DIRECT_HELP)
        self.assertIn("stays a direct session with no profile", cli_text.DIRECT_HELP)

    def test_esc_creates_no_record_and_launches_nothing(self) -> None:
        result, _win = self.run_screen(self.screen(), [ESC])
        self.assertIsNone(result)
        self.assertEqual(list(self.runtime.session_store.sessions_dir.glob("*.json")), [])
        self.assertEqual(self.runtime.launches, [])


class DirectChooseTests(_DirectCase):
    def test_choose_mode_returns_key_and_effort(self) -> None:
        with self.signed_in():
            screen = self.screen(purpose="needs a choice")
            row = self.unmarked(screen, mode="gateway")
            result, win = self.run_screen(screen, [*self.steps_to(screen, row.key), ENTER])
        self.assertEqual(result, (row.key, row.default_effort))
        self.assertIn("choose a direct model — needs a choice", win.frames[0])
        self.assertIn("Enter choose · ? help · Esc back", win.frames[0])
        self.assertNotIn("Tab save", win.frames[0])

    def test_link_choose_mode_shows_no_effort_and_returns_none(self) -> None:
        # _sessions_link --direct takes a model only.
        with self.signed_in():
            screen = self.screen(purpose="link")
            row = self.unmarked(screen, mode="gateway")
            result, win = self.run_screen(screen, [*self.steps_to(screen, row.key), RIGHT, ENTER])
        self.assertEqual(result, (row.key, None))
        self.assertNotIn("effort [", win.frames[0])


class DirectFloorTests(_DirectCase):
    def test_floor(self) -> None:
        factory = lambda: self.screen()  # noqa: E731
        screen = factory()
        rows, cols = screen.min_size(80)
        too_small = tui.TOO_SMALL.format(what=screen.what)

        def frame(height: int, width: int) -> str:
            win = FakeWindow([], height=height, width=width)
            factory()._draw(win)
            return win.text()

        self.assertEqual(cols, cli.DIRECT_MIN_COLS)
        self.assertIn(too_small, frame(rows - 1, 80))
        fits = frame(rows, 80)
        self.assertNotIn(too_small, fits)
        self.assertIn("Esc back", fits.splitlines()[-1])
        self.assertIn(cli.DIRECT_TITLE, fits)
        narrow_rows, _cols = screen.min_size(cols)
        self.assertNotIn(too_small, frame(narrow_rows, cols))
        self.assertIn("Terminal too small", frame(narrow_rows, cols - 1))
        win = FakeWindow(["j", ENTER, "?", ESC], height=rows - 1, width=80)
        self.assertIsNone(factory().run(win))


class DirectNarrowWidthTests(_DirectCase):
    def test_narrow_widths_never_overdraw(self) -> None:
        # Restores OrdinaryScreenTuiTests.test_narrow_widths_do_not_overdraw
        # (FakeWindow raises on an out-of-bounds write).
        for width in (cli.DIRECT_MIN_COLS, 56, 71, 90):
            with self.subTest(width=width):
                screen = self.screen()
                rows, _cols = screen.min_size(width)
                result, win = self.run_screen(screen, [END, ESC], height=max(rows, 30), width=width)
                self.assertIsNone(result)
                self.assertTrue(all(len(line) <= width for line in win.text().splitlines()))


if __name__ == "__main__":
    unittest.main()
