"""The 3.0 launch card.

Every test runs on a hermetic ``_tui_fixture.fixture_runtime`` inside
``hermetic(runtime)`` with doctor's clock at ``FIXED_NOW``: nothing reads the
host journal, ``/proc`` or the daemon socket directory.  Profiles, keys,
agents and providers derive from the loaded fixture catalog: no fixture
id is a literal here.  The card is driven on ``test_tui.FakeWindow`` in
``MONO_PALETTE``; every script ends with Esc or a returning key.  The
screens a card key opens are patched where the test is about the dispatch.
"""

from __future__ import annotations

import contextlib
import dataclasses
import copy
import io
import json
import shutil
import tempfile
import textwrap
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from claude_multi import catalog, cli, profile, sessions, settings, state, tui, views

import _tui_fixture as fx
from test_tui import BTAB, DOWN, END, HOME, LEFT, RIGHT, FakeWindow
import claude_multi.cli.text as cli_text
import curses as _curses
import claude_multi.compiler
import claude_multi.custom
import claude_multi.launch
import claude_multi.scope
import claude_multi.pin
import claude_multi.tui
import claude_multi.views

SPEC = "the card contract"
ESC = "\x1b"
ENTER = "\n"
TAB = "\t"


def _flat(frame: str) -> str:
    """A frame's text with the Modal borders stripped and lines joined (wrapped text)."""

    return " ".join(line.strip(" |") for line in frame.splitlines())



PGDN = _curses.KEY_NPAGE

class _CardCase(unittest.TestCase):
    runtime_kwargs: dict = {}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-screens-card-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runtime = self.make_runtime(self.tmp / "root", **self.runtime_kwargs)

    def make_runtime(self, root: Path, **kwargs) -> fx.ScreenRuntime:
        runtime = fx.fixture_runtime(root, **kwargs)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(fx.hermetic(runtime))
        stack.enter_context(mock.patch('claude_multi.cli.gateway_facts._doctor_now', lambda: fx.FIXED_NOW))
        runtime.profiles.install_seeds()
        return runtime

    # -- builders ------------------------------------------------------------

    @staticmethod
    def target(runtime, name: str = catalog.DEFAULT_SEED, document=None) -> cli.LaunchTarget:
        document = document if document is not None else runtime.profiles.load(name)
        return cli.LaunchTarget("profile", document, name, True, f"Profile {name}")

    def save_profile(self, name: str, mutate, runtime=None) -> dict:
        runtime = runtime or self.runtime
        document = copy.deepcopy(runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = name
        mutate(document)
        runtime.profiles.save(document)
        return runtime.profiles.load(name)

    def card(self, target=None, *, runtime=None, **kwargs) -> cli._LaunchCardScreen:
        runtime = runtime or self.runtime
        kwargs.setdefault("gateway_checked", True)
        kwargs.setdefault("hint_detector", lambda _contract: None)
        kwargs.setdefault("tty_in", io.StringIO())
        kwargs.setdefault("tty_out", io.StringIO())
        kwargs.setdefault("passthrough", [])
        return cli._LaunchCardScreen(
            runtime, target or self.target(runtime), palette=tui.MONO_PALETTE, **kwargs
        )

    @staticmethod
    def frame(screen, *, height=24, width=80) -> str:
        win = FakeWindow([], height=height, width=width)
        screen._draw(win)
        return win.text()

    @staticmethod
    def run_card(screen, keys, *, height=24, width=80):
        win = FakeWindow(list(keys), height=height, width=width)
        return screen.run(win), win

    def blocked_document(self, runtime=None) -> dict:
        """A test-local profile whose lead is the derived retired-null key (E4)."""

        runtime = runtime or self.runtime
        return self.save_profile(
            "card-blocked",
            lambda d: d.update(lead={"model": fx.needs_choice_key(runtime), "effort": profile.ULTRACODE}),
            runtime,
        )

    def resume_prepared(self, *, follow: bool = True, name: str = catalog.DEFAULT_SEED, change=None):
        record = fx.v4_record(self.runtime, name, managed_id=fx.FIXED_ID, follow=follow)
        if change is not None:
            change()
        prepared = self.runtime.prepare(
            cli._record_resume_target(record), action="resume", passthrough=[], session_id=fx.FIXED_ID
        )
        return record, prepared

    def change_a_writer_effort(self, name: str = catalog.DEFAULT_SEED) -> str:
        writer = profile.WRITER_IDS[0]

        def mutate(document: dict) -> None:
            binding = document["agents"][writer]
            efforts = profile.declared_efforts(self.runtime.lineup_catalog().lines[binding["model"]])
            binding["effort"] = next(e for e in efforts if e != binding["effort"])

        self.runtime.profiles.update(name, mutate)
        return writer

    def resume_card(self, prepared, **kwargs) -> cli._LaunchCardScreen:
        return self.card(prepared.target, action="resume", prepared=prepared, **kwargs)



class CardNoticeTests(_CardCase):
    """The card's own notices, its Ready / Attention / BLOCKED status and V's session sections."""

    def test_an_ignored_claude_config_dir_is_named_and_makes_the_card_attention(self) -> None:
        with mock.patch.dict(self.runtime.environ, {"CLAUDE_CONFIG_DIR": "/srv/claude-elsewhere"}):
            screen = self.card()
            text = self.frame(screen, height=40)
        row = next(row for row in screen.card.rows if row.kind == "config-dir")
        self.assertEqual(row.role, "warn")
        self.assertIn("CLAUDE_CONFIG_DIR is set (/srv/claude-elsewhere)", row.text)
        self.assertIn("! CLAUDE_CONFIG_DIR", text)
        self.assertIn("Status  Attention", text)
        # A terminal wide enough draws the full sentence, the path included.
        self.assertIn(row.text, self.frame(screen, height=40, width=160))
        self.assertEqual(views.card_status(screen.card), "attention")
        self.assertTrue(screen.card.ready)

    def test_implementers_without_a_git_repository_are_a_dim_fact(self) -> None:
        screen = self.card()
        row = next(row for row in screen.card.rows if row.kind == "worktree")
        self.assertEqual((row.role, row.text), ("dim", views.CARD_WORKTREE))
        self.assertIn("Status  Ready", self.frame(screen, height=40))
        with mock.patch.object(claude_multi.compiler, "git_work_tree", lambda _cwd: True):
            inside = self.card()
        self.assertFalse(any(row.kind == "worktree" for row in inside.card.rows))

    def test_v_names_the_lead_set_the_kept_environment_and_managed_differences(self) -> None:
        shown: list[list[str]] = []
        real_view = tui.TextView

        class RecordingView(real_view):
            def __init__(self, title, lines, **kwargs):
                shown.append(list(lines))
                super().__init__(title, lines, **kwargs)

        screen = self.card()
        with mock.patch.dict(self.runtime.environ, {"CLAUDE_CONFIG_DIR": "/srv/claude-elsewhere"}), \
                mock.patch.object(claude_multi.tui, "TextView", RecordingView):
            self.run_card(screen, ["v", ESC, ESC])
        lines = shown[0]
        self.assertIn(cli_text.CARD_DETAILS_MODEL, lines)
        for row in screen.prepared.result.fence.lead_set:
            self.assertIn(f"  {row.label} — {row.selector}", lines)
        self.assertIn(cli_text.CARD_DETAILS_ENV_TITLE, lines)
        self.assertIn(cli_text.CARD_DETAILS_MANAGED_TITLE, lines)
        self.assertTrue(any("CLAUDE_CONFIG_DIR is set (/srv/claude-elsewhere)" in line for line in lines))
        self.assertTrue(any(line.startswith("lead: ") for line in lines))
        self.assertFalse(any(len(line) > 400 for line in lines))

    def test_v_shows_the_terms_and_the_cm_guidance_of_the_help(self) -> None:
        shown: list[list[str]] = []
        real_view = tui.TextView

        class RecordingView(real_view):
            def __init__(self, title, lines, **kwargs):
                shown.append(list(lines))
                super().__init__(title, lines, **kwargs)

        screen = self.card()
        with mock.patch.object(claude_multi.tui, "TextView", RecordingView):
            _result, win = self.run_card(screen, ["v", END, ESC, ESC], height=24, width=80)
        lines = shown[0]
        # Every term and /cm line the help shows is in V too.
        help_terms = cli_text.QUICK_HELP.split("\nTerms\n", 1)[1].split("\n\n", 1)[0].splitlines()
        session = cli_text.QUICK_HELP.split("\nIn a claude-multi session\n", 1)[1].splitlines()
        help_cm = [line for line in session if line.startswith(("  /cm", "  A change applies"))]
        self.assertTrue(help_terms and help_cm)
        for line in (*help_terms, *help_cm):
            self.assertIn(line, lines)
        terms = lines.index(cli_text.CARD_TERMS_TITLE)
        self.assertEqual(lines[terms + 1:terms + 1 + len(cli_text.CARD_TERMS)], list(cli_text.CARD_TERMS))
        guidance = lines.index(cli_text.CARD_CM_TITLE)
        self.assertEqual(lines[guidance + 1:guidance + 1 + len(cli_text.CARD_CM_GUIDANCE)],
                         list(cli_text.CARD_CM_GUIDANCE))
        # The same lines the help shows, and they reach the screen at 80x24.
        for line in (*cli_text.CARD_TERMS, *cli_text.CARD_CM_GUIDANCE):
            self.assertIn(line + "\n", cli_text.QUICK_HELP)
        self.assertIn("/cm fallback <provider>", _flat("\n".join(win.frames)))

    def runtime_with_a_new_line(self) -> fx.ScreenRuntime:
        """A runtime over a copy of the fixture assets with one more line,
        New and off: a copy of the first line on a keyed direct provider."""

        from _catalog import FIXTURE_ROOT

        root = self.tmp / "assets-new"
        shutil.copytree(FIXTURE_ROOT, root)
        models_path = root / "catalog" / "models.json"
        document = json.loads(models_path.read_text())
        providers = self.runtime.lineup_catalog().providers
        source = next(key for key in sorted(document["models"])
                      if providers[document["models"][key]["provider"]]["transport"]["kind"] == "direct")
        line = copy.deepcopy(document["models"][source])
        line.update(status="new", display="Fixture New Line", wire_model="fixture-new-wire")
        if isinstance(line.get("efforts"), dict):
            for spec in line["efforts"].values():
                if "selector" in spec:
                    spec["selector"] = "fixture-new-" + spec["selector"]
        if "selector" in line:
            line["selector"] = "fixture-new-" + line["selector"]
        document["models"]["fixture-new"] = line
        models_path.write_text(json.dumps(document, indent=2) + "\n")
        return self.make_runtime(self.tmp / "root-new", asset_root=root)

    def test_every_notice_keeps_its_remedy_at_80x24_80x40_and_120x40(self) -> None:
        runtime = self.runtime_with_a_new_line()
        with mock.patch.dict(runtime.environ, {"CLAUDE_CONFIG_DIR": "/srv/claude-elsewhere"}):
            screen = self.card(runtime=runtime)
        kinds = {row.kind for row in screen.card.rows}
        self.assertLessEqual({"config-dir", "new-lines", "worktree"}, kinds)
        for width, height in ((80, 24), (80, 40), (120, 40)):
            with self.subTest(size=f"{width}x{height}"):
                lines = self.frame(screen, width=width, height=height).splitlines()
                config = next(line for line in lines if "CLAUDE_CONFIG_DIR" in line)
                for part in ("~/.claude", "settings", "MCP", "memory", "resume", "— V"):
                    self.assertIn(part, config)
                new = next(line for line in lines if "new model line(s)" in line)
                self.assertIn("! 1 new model line(s)", new)
                self.assertTrue(new.rstrip().endswith("— M → Enter admits"), new)
                worktree = next(line for line in lines if "Git repository" in line)
                self.assertTrue(worktree.rstrip().endswith("(git init enables them)"), worktree)
                self.assertIn("Status  Attention", "\n".join(lines))
                for line in (config, new, worktree):
                    self.assertLessEqual(len(line), width - 1)
                    self.assertNotIn("…", line)

    def test_a_long_notice_loses_its_middle_never_its_remedy(self) -> None:
        text = "! lead: " + "x" * 90 + " — G → L signs in"
        self.assertEqual(views.fit_text((text,), 60), "! lead: " + "x" * 34 + "… — G → L signs in")
        self.assertEqual(len(views.fit_text((text,), 60)), 60)
        self.assertEqual(views.fit_text(("a" * 70, "short — X"), 60), "short — X")
        self.assertEqual(views.fit_text(("no remedy " * 9,), 20), views.clip("no remedy " * 9, 20))

    def test_status_follows_the_rows(self) -> None:
        screen = self.card()
        model = screen.card
        self.assertEqual(views.card_status(model), "ready")
        noticed = views.card_with_notices(model, (("new-lines", "! 2 new model line(s)"),))
        self.assertEqual(views.card_status(noticed), "ready")
        self.assertLess(noticed.rows.index(next(r for r in noticed.rows if r.kind == "new-lines")), len(noticed.rows))
        warned = views.card_with_notices(model, (("config-dir", "! set"),))
        self.assertEqual(views.card_status(warned), "attention")
        blocked = dataclasses.replace(model, ready=False)
        self.assertEqual(views.card_status(blocked), "blocked")
        self.assertIn("Status  BLOCKED", views.card_text(blocked, width=80))

class CardSignInRemedyTests(_CardCase):
    """A sign-in remedy on the card is its key route (G → L); a resume card,
    which has no G, names the way out and back. The command line keeps the
    sign-in commands."""

    def without_sign_ins(self, runtime=None) -> None:
        runtime = runtime or self.runtime
        auth = Path(runtime.environ["HOME"]) / runtime.catalog.docs["gateway"]["gateway"]["auth_dir"]
        auth.mkdir(parents=True, exist_ok=True)

    def sign_in_rows(self, screen) -> list[str]:
        return [row.text for row in screen.card.rows if "OAuth credential record" in row.text]

    def test_the_fresh_card_names_g_l_and_the_command_line_keeps_the_command(self) -> None:
        from claude_multi.cli import gateway_facts

        self.without_sign_ins()
        screen = self.card()
        rows = self.sign_in_rows(screen)
        self.assertTrue(rows, "the default lineup runs on an account pool")
        self.assertFalse([text for text in rows if "providers sign-in" in text], "the card names its keys")
        for text in rows:
            self.assertTrue(text.endswith(" — sign in: " + cli_text.CARD_SIGN_IN_FRESH), text)
            self.assertNotIn("claude-multi providers sign-in", text)
        frame = self.frame(screen, height=40, width=80)
        self.assertIn("— sign in: " + cli_text.CARD_SIGN_IN_FRESH, frame)
        self.assertNotIn("providers sign-in", frame)
        # The plan's own notices (the line and print surfaces) keep the command.
        commands = set(gateway_facts._OAUTH_LOGIN_COMMANDS.values())
        self.assertTrue(any(command in line for line in screen.prepared.notices for command in commands))
        # V's per-slot readiness says it the card's way too.
        shown: list[list[str]] = []
        real_view = tui.TextView

        class RecordingView(real_view):
            def __init__(self, title, lines, **kwargs):
                shown.append(list(lines))
                super().__init__(title, lines, **kwargs)

        with mock.patch.object(claude_multi.tui, "TextView", RecordingView):
            self.run_card(screen, ["v", ESC, ESC])
        readiness = [line for line in shown[0] if "OAuth credential record" in line]
        self.assertTrue(readiness)
        self.assertFalse(any("providers sign-in" in line for line in shown[0]))
        # The card gives the slots that miss the same sign-in one row; V names each slot.
        self.assertEqual(len(rows), len({line.split(": ", 1)[1] for line in readiness}))
        self.assertGreater(len(readiness), len(rows))

    def test_slots_with_the_same_remedy_share_a_row(self) -> None:
        from claude_multi.cli.screens import launch_sessions

        lines = ["! lead: no x — sign in: G → L", "! explorer: no y — sign in: G → L", "! other notice",
                 "! analyst: no y — sign in: G → L", "! reviewer: no y — sign in: G → L",
                 "! designer: no x — sign in: G → L", "! reviewer-strong: 500K (allowed)"]
        self.assertEqual(launch_sessions._card_slot_groups(lines), [
            "! lead and designer: no x — sign in: G → L", "! 3 agents: no y — sign in: G → L",
            "! other notice", "! reviewer-strong: 500K (allowed)"])
        self.assertEqual(launch_sessions._card_slot_groups(["! lead: a — b", "! explorer: a — b", "! analyst: a — b"]),
                         ["! lead + 2 agents: a — b"])

    def test_a_resume_card_names_the_way_out_and_back(self) -> None:
        self.without_sign_ins()
        _record, prepared = self.resume_prepared()
        screen = self.resume_card(prepared)
        rows = self.sign_in_rows(screen)
        self.assertTrue(rows)
        self.assertFalse([text for text in rows if "providers sign-in" in text], "the card names its keys")
        route = "Esc, run claude-multi, G → L, then S to resume"
        for text in rows:
            self.assertTrue(text.endswith(" — sign in: " + route), text)
        frame = self.frame(screen, height=40, width=120)
        self.assertIn("— sign in: " + route, frame)
        self.assertNotIn("providers sign-in", frame)
        # The way out works: Esc leaves the resume card without resuming.
        result, _win = self.run_card(screen, [ESC])
        self.assertIsNone(result)

    # The whole key sequence a resume card's sign-in remedy displays, pressed
    # as shown: Esc as often as it says, claude-multi run again when it says
    # so, then G → L reaches the sign-in on the launch card, and S resumes.

    WIDE = {"height": 40, "width": 120}

    def recorded_session(self) -> dict:
        """A session on the default seed whose transcript exists (the resume
        gate passes), with no account signed in."""

        self.without_sign_ins()
        record = fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        transcript = (Path(self.runtime.environ["HOME"]) / ".claude" / "projects"
                      / cli._native_project_slug(record["cwd"]) / f"{sessions.runtime_session_id(record)}.jsonl")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.touch()
        return record

    def displayed_route(self, frames: list[str]) -> str:
        """The sign-in route the first resume card drawn shows."""

        marker = " — sign in: "
        for frame in frames:
            if "resume" in frame and marker in frame:
                line = next(line for line in frame.splitlines() if marker in line)
                return line.split(marker, 1)[1].strip()
        raise AssertionError("no resume card with a sign-in remedy was drawn")

    def account_row(self) -> int:
        import claude_multi.cli.screens.providers as providers_screen

        screen = providers_screen._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
        return next(index for index, fact in enumerate(screen.facts) if fact.get("kind") == "oauth-pool")

    def sign_in_then_resume(self) -> list:
        """G → L on the launch card (the account row), then S to resume."""

        return ["g", *([DOWN] * self.account_row()), "l", ESC, ESC, "s", ENTER]

    def assert_in_order(self, frames: list[str], *needles: str) -> None:
        position = 0
        for needle in needles:
            found = next((index for index in range(position, len(frames)) if needle in frames[index]), None)
            self.assertIsNotNone(found, f"{needle!r} was not drawn after frame {position}")
            position = found + 1

    def steps(self, route: str) -> tuple[int, bool]:
        """``(Esc presses, run claude-multi again)`` the route says before G → L."""

        words = route.split(", ")
        self.assertIn("G → L", words, route)
        before = words[: words.index("G → L")]
        reopen = bool(before) and before[-1] == "run claude-multi"
        escapes = before[:-1] if reopen else before
        self.assertEqual(set(escapes), {"Esc"}, route)
        return len(escapes), reopen

    def test_a_resume_from_sessions_names_the_extra_exit_before_g(self) -> None:
        self.recorded_session()
        card = self.card(self.target(self.runtime))
        opened = ["s", ENTER]  # Sessions, then the resume card of this directory's session
        _result, win = self.run_card(card, [*opened, ESC, ESC, ESC], **self.WIDE)
        route = self.displayed_route(win.frames)
        escapes, reopen = self.steps(route)
        self.assertFalse(reopen)
        keys = [*opened, *([ESC] * escapes), *self.sign_in_then_resume(), ESC, ESC, ESC]
        result, win = self.run_card(self.card(self.target(self.runtime)), keys, **self.WIDE)
        self.assertIsNone(result)
        self.assert_in_order(win.frames, route, "Resume cancelled.", "Enter launch", "providers — local status",
                             cli_text.L_BODY_NOT_SIGNED_IN, "sessions — ", route)
        self.assertEqual(self.runtime.launches, [])
        self.assertEqual(route, "Esc, Esc, G → L, then S to resume")

    def test_a_direct_resume_names_the_launcher_again(self) -> None:
        record = self.recorded_session()
        prepared = self.runtime.prepare(cli._record_resume_target(record), action="resume", passthrough=[],
                                        session_id=fx.FIXED_ID)
        _result, win = self.run_card(self.resume_card(prepared), [ESC], **self.WIDE)
        route = self.displayed_route(win.frames)
        escapes, reopen = self.steps(route)
        self.assertTrue(reopen, f"Esc ends claude-multi -r: the route must run it again ({route})")
        result, _win = self.run_card(self.resume_card(prepared), [ESC] * escapes, **self.WIDE)
        self.assertIsNone(result, "Esc ends claude-multi -r")
        # claude-multi run again: its launch card.
        result, win = self.run_card(self.card(self.target(self.runtime)), [*self.sign_in_then_resume(), ESC, ESC, ESC],
                                    **self.WIDE)
        self.assertIsNone(result)
        self.assert_in_order(win.frames, "providers — local status", cli_text.L_BODY_NOT_SIGNED_IN, "sessions — ",
                             "— sign in: Esc, Esc, G → L")

    def test_a_resume_from_the_first_sessions_screen_names_the_launcher_again(self) -> None:
        self.recorded_session()
        screen = cli._SessionsScreen(self.runtime, palette=tui.MONO_PALETTE, root=True, tty_in=io.StringIO(),
                                     tty_out=io.StringIO())
        with mock.patch('claude_multi.cli.screens.launch_sessions._LaunchCardScreen._gateway_status', lambda _s: None):
            result, win = self.run_card(screen, [ENTER, ESC, ESC], **self.WIDE)
        self.assertIsNone(result)
        route = self.displayed_route(win.frames)
        self.assertEqual(self.steps(route), (2, True), route)
        self.assertEqual(route, "Esc, Esc, run claude-multi, G → L, then S to resume")

    def test_a_rejected_sign_in_in_the_quota_row_names_g_l(self) -> None:
        from datetime import timezone

        runtime = self.make_runtime(self.tmp / "root-quota", management=fx.quota_body(reason="unauthorized"))
        screen = self.card(self.target(runtime), runtime=runtime, now=fx.FIXED_NOW, tz=timezone.utc)
        row = next(row for row in screen.card.rows if row.kind == "quota")
        self.assertNotIn("providers sign-in", row.text)
        self.assertIn("sign in: " + cli_text.CARD_SIGN_IN_FRESH, row.text)
        self.assertIn("sign in: " + cli_text.CARD_SIGN_IN_FRESH, self.frame(screen, height=40, width=80))
        self.assertNotIn("providers sign-in", row.text)


class CardHealthKeyRemedyTests(_CardCase):
    """H reports a key doctor says to save the card's way: K on the provider
    in Providers (G), and on a resume card, which has no G, its way back
    first. The command line's doctor keeps the command."""

    def block(self) -> str:
        from claude_multi import operator as operator_mod

        name = operator_mod.transport_alternative("anthropic", operator_mod.TRANSPORT_API_KEY).secret_name
        return (f"provider anthropic transport api-key: {name} is not set — claude-multi providers set-key "
                "anthropic; its selectors are not served (back to the pool: claude-multi providers transport "
                "anthropic oauth-pool)")

    def h_report(self, screen) -> str:
        from claude_multi.cli import doctor as doctor_mod

        with mock.patch.object(doctor_mod, "_collect_doctor_reports", return_value=([self.block()], [], [])):
            result, _win = self.run_card(screen, ["h", ESC])
        self.assertIsNone(result)
        return screen.tty_out.getvalue()

    def assert_route(self, report: str, route: str) -> None:
        self.assertNotIn("providers set-key", report)
        expected = self.block().replace("claude-multi providers set-key anthropic",
                                        cli_text.CARD_SET_KEY.format(route=route))
        self.assertIn(f"  - {expected}\n", report)

    def test_the_fresh_card_names_k_on_the_provider(self) -> None:
        report = self.h_report(self.card(tty_in=io.StringIO("\n\n\n"), tty_out=io.StringIO()))
        self.assert_route(report, "G (providers) → K on Anthropic")

    def test_a_resume_card_names_its_way_back_first(self) -> None:
        _record, prepared = self.resume_prepared()
        report = self.h_report(self.resume_card(prepared, tty_in=io.StringIO("\n\n\n"), tty_out=io.StringIO()))
        self.assert_route(report, "Esc, run claude-multi, G (providers) → K on Anthropic, then S to resume")

    def test_only_a_findings_remedy_becomes_the_route(self) -> None:
        from claude_multi.cli.screens import launch_sessions

        # A command quoted inside other advice (a file to correct) stays as it is.
        advice = ('providers.d/mine.json: a secret value is not allowed — store it with claude-multi providers '
                  'set-key mine; write "env:NAME"')
        self.assertEqual(launch_sessions._card_doctor_line(self.runtime, advice, None), advice)
        # A provider the merged view does not know is named by its id.
        self.assertEqual(launch_sessions._card_doctor_line(self.runtime, "X is not set — claude-multi providers "
                                                                          "set-key nobody", None),
                         "X is not set — " + cli_text.CARD_SET_KEY.format(
                             route=cli_text.CARD_SET_KEY_FRESH.format(display="nobody")))


# ================================================================ rows


class CardRowTests(_CardCase):
    def in_a_work_tree(self, **kwargs) -> cli._LaunchCardScreen:
        """The card planned inside a Git work tree: no implementer notice
        takes a row (the canonical rows at 80x24)."""

        with mock.patch.object(claude_multi.compiler, "git_work_tree", lambda _cwd: True):
            return self.card(**kwargs)

    def test_fresh_card_rows_and_badges(self) -> None:
        screen = self.in_a_work_tree()
        prepared = screen.prepared
        lineup = prepared.lineup
        lead = lineup.lead.binding
        text = self.frame(screen)
        lines = text.splitlines()
        self.assertEqual(lines[1], f"  claude-multi — profile: {catalog.DEFAULT_SEED} · follows profile · Fresh")
        self.assertEqual(lines[2], "  " + "─" * 77)
        self.assertEqual(
            lines[3],
            f"  lead      {views.short_label(lead.display)} · effort {lead.effort} · "
            f"{profile.format_tokens(lead.client_context_tokens)} class   workflows: {lineup.workflows}",
        )
        ctx = prepared.record["applied"]["lead"]["compaction"]
        # Every agent shares the lead's window here, so the context row names them.
        self.assertIn(
            f"  context   window {profile.format_tokens(ctx['window'])} (lead and agents) · compacts at "
            f"~{profile.format_tokens(ctx['trigger'])} ({ctx['percent']} %)",
            lines,
        )
        lineup_line = next(line for line in lines if line.startswith("  lineup    "))
        self.assertTrue(lineup_line.startswith(f"  lineup    {len(lineup.agents)} agents · {cli.DURABLE_BADGE} · "))
        header = next(line for line in lines if line.strip().startswith("agent "))
        for column in ("agent", "model", "effort", "provider"):
            self.assertIn(column, header)
        first_agent = next(iter(lineup.agents))
        self.assertTrue(any(line.strip().startswith(profile.label(first_agent)) for line in lines))
        wide = self.frame(screen, height=40, width=200).splitlines()
        self.assertIn(f"  review    {views.review_sentence(lineup)}", wide)
        self.assertIn(f"  spread    {lineup.spread_text()}", lines)
        pin = claude_multi.pin.version(self.runtime.catalog.docs["native-contract"])
        self.assertIn(f"  health    gateway ok · Claude {pin} · catalog {self.runtime.catalog_version}", lines)
        for finding in lineup.warnings:
            self.assertIn(f"  ! {finding.message}", wide)
        self.assertIn("  Status  Ready", lines)
        self.assertIn("Enter launch · E edit · Tab next · P profiles · D direct · V details", text)
        self.assertTrue(lines[-1].rstrip().endswith("? help · Esc"))
        self.assertNotIn("note      context meter", text, "the default seed leads on the Anthropic OAuth pool")

    def test_read_only_and_worktree_annotations(self) -> None:
        screen = self.card()
        text = self.frame(screen, height=40)
        for rid, agent in screen.lineup.agents.items():
            line = next(l for l in text.splitlines() if l.strip().startswith(profile.label(rid) + " "))
            if agent.role.isolation == "worktree":
                self.assertTrue(line.rstrip().endswith("(worktree)"), line)
            elif agent.role.disallowed_tools:
                self.assertTrue(line.rstrip().endswith("read-only"), line)
            else:
                self.assertFalse(line.rstrip().endswith(("read-only", "(worktree)")), line)

    def test_workflows_off_badge_and_help_panel_off_note(self) -> None:
        document = self.save_profile("card-wf-off", lambda d: d.update(workflows="off"))
        screen = self.card(self.target(self.runtime, "card-wf-off", document))
        self.assertIn("workflows: off", self.frame(screen))
        result, win = self.run_card(screen, ["?", END, ESC, ESC], height=50, width=120)
        self.assertIsNone(result)
        modal = [f for f in win.frames if cli.CARD_HELP_TITLE in f][1]
        self.assertIn("Native workflows (ultracode): OFF", modal)
        self.assertIn(tui.WORKFLOW_OFF_NOTE.splitlines()[0], modal)

    def test_project_row_counts_project_agents_without_collision(self) -> None:
        agents = Path(self.runtime.cwd) / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "helper.md").write_text("---\nname: helper\ndescription: x\n---\nbody\n")
        text = self.frame(self.card(), height=40)
        self.assertIn(
            "  project   1 project agent discovered (native precedence; none colliding)", text.splitlines()
        )
        # At 80x24 the project row (P 7) yields to the agent rows (P 5): the fit.
        self.assertNotIn("  project ", self.frame(self.card()))
        self.assertIn("Status  Ready", text)

    def test_context_override_diamond_and_the_meter_note(self) -> None:
        document = self.save_profile(
            "card-diamond",
            lambda d: d.update(settings_overrides={settings.COMPACTION_PERCENT_KEY: settings.COMPACTION_PERCENT_MIN}),
        )
        context = next(
            l for l in self.frame(self.card(self.target(self.runtime, "card-diamond", document))).splitlines()
            if l.startswith("  context ")
        )
        self.assertTrue(context.endswith(" ◆ override"), context)
        self.assertIn(f"({settings.COMPACTION_PERCENT_MIN} %)", context)
        providers = self.runtime.lineup_catalog().providers
        other = next(
            (name for name in sorted(self.runtime.catalog.seed_profiles)
             if providers[self.card(self.target(self.runtime, name)).lineup.lead.binding.provider]["adapter"]
             not in views.COUNT_TOKENS_UPSTREAM_ADAPTERS),
            None,
        )
        self.assertIsNotNone(other, f"{SPEC}: a fixture seed leads on a non-Anthropic-OAuth provider")
        screen = self.card(self.target(self.runtime, other))
        provider = screen.lineup.lead.binding.provider
        self.assertIn(
            f"  note      context meter approximate: {provider} token counts are local gateway estimates",
            self.frame(screen, height=40, width=200).splitlines(),
        )

    def test_card_health_formats_the_error_remedy_once(self) -> None:
        screen = self.card()
        for error, remedy in ((claude_multi.launch.LaunchError("fixture down"), claude_multi.launch.gateway_unit_remedy()),
                              (claude_multi.launch.GatewayKeyError("bad key", remedy="fix key"), "fix key")):
            with mock.patch.object(self.runtime, "check_readiness", side_effect=error):
                self.assertEqual(screen._gateway_status(), f"{error} — {remedy}")
        with mock.patch.object(self.runtime, "check_readiness", side_effect=ValueError("fixture")):
            self.assertEqual(screen._gateway_status(), f"gateway check failed: fixture — {claude_multi.launch.gateway_unit_remedy()}")

    def test_health_down_preserves_problem_and_update_row_places_u(self) -> None:
        screen = self.in_a_work_tree(gateway_problem="boom", update_hint=("1.0.0", "1.0.1"))
        text = self.frame(screen, width=200)
        self.assertIn(
            "  health    gateway unreachable — launches will fail: boom",
            text.splitlines(),
        )
        self.assertIn("  update    claude-multi 1.0.0 · 1.0.1 · press U to update", text.splitlines())
        self.assertIn("Enter launch · U update · E edit", text)

    def test_notices_outsiders_radar_and_seed_lines(self) -> None:
        lead_provider = self.card().lineup.lead.binding.provider
        document = self.save_profile("card-outsiders", lambda d: d.update(primary_provider=lead_provider))
        radar = ["line some-line (wire x) retires upstream in 3 days — …"]
        with mock.patch('claude_multi.cli.doctor._doctor_retirement_radar', return_value=radar), mock.patch.object(
            profile.ProfileStore, "seed_updates", return_value=[(catalog.DEFAULT_SEED, 1, 2)]
        ):
            screen = self.card(self.target(self.runtime, "card-outsiders", document))
            text = self.frame(screen, height=60, width=200)
        outsiders = screen.lineup.outsiders
        self.assertTrue(outsiders, f"{SPEC}: a primary_provider profile has outsiders")
        for finding in outsiders:
            self.assertIn(f"    {finding.message}", text.splitlines())
        self.assertIn(
            "  ! 1 model line(s) near an upstream retirement date — M (models) or H (doctor)", text.splitlines()
        )
        self.assertIn(
            f"  ! 1 shipped profile(s) have updates: {catalog.DEFAULT_SEED} — "
            "P → U (your edits are kept as a backup)",
            text.splitlines(),
        )


    # -- card behaviours kept from the line-mode card --------------------------

    def test_card_text_is_identical_across_palettes_and_only_attrs_differ(self) -> None:
        # Restores QuickConfirmAccessibilityTests (every colour has a text form).
        texts, status_attrs = [], []
        for palette in (tui.MONO_PALETTE, tui.DARK_PALETTE, tui.LIGHT_PALETTE):
            screen = cli._LaunchCardScreen(
                self.runtime, self.target(self.runtime), palette=palette, passthrough=[],
                gateway_checked=True, hint_detector=lambda _contract: None,
                tty_in=io.StringIO(), tty_out=io.StringIO(),
            )
            win = FakeWindow([ESC])
            self.assertIsNone(screen.run(win))
            texts.append(win.text())
            row, col = win.find("Status  Ready")[0]
            status_attrs.append(win.attr_at(row, col + len("Status  ")))
        self.assertEqual(texts[0], texts[1])
        self.assertEqual(texts[1], texts[2])
        self.assertEqual(status_attrs[0], 0, "mono: no colour attribute")
        self.assertNotEqual(status_attrs[1], 0)
        self.assertNotEqual(status_attrs[2], 0)

    def test_no_health_or_update_row_before_the_first_check(self) -> None:
        # Restores QuickConfirmHealthUpdateTests.test_no_health_lines_when_unchecked:
        # the rows appear only once run() made its one check per open.
        screen = self.card(gateway_checked=False)
        text = self.frame(screen, height=40, width=200)
        for absent in ("  health ", "gateway ok", "gateway unreachable", "  update ", "U update"):
            self.assertNotIn(absent, text)
        self.assertIn("Status  Ready", text)

    def test_the_resume_card_restates_the_durable_badge_and_the_generation(self) -> None:
        # Restores DurableDefaultWiringTests.test_badge_resume_durable_preserves_generation.
        record, prepared = self.resume_prepared()
        text = self.frame(self.resume_card(prepared), height=40, width=200)
        lineup_line = next(line for line in text.splitlines() if line.startswith("  lineup    "))
        self.assertIn(f" · {cli.DURABLE_BADGE} · ", lineup_line)
        self.assertIn(f"Resume {fx.FIXED_ID[:8]} · lineup gen {record['lineup_generation']}", text)


# ================================================================ BLOCKED


class CardBlockedTests(_CardCase):
    def test_blocked_evaluation_draws_no_lineup_rows(self) -> None:
        document = self.blocked_document()
        screen = self.card(self.target(self.runtime, "card-blocked", document))
        text = self.frame(screen)
        self.assertIsNone(screen.lineup)
        self.assertIn("Status  BLOCKED", text)
        for label in ("  lead ", "  context ", "  lineup ", "  review ", "  spread "):
            self.assertNotIn(label, text)
        self.assertIn("  health    gateway ok", text)
        self.assertIn(f"lead.model: {fx.needs_choice_key(self.runtime)}", _flat(text))

    def test_blocked_evaluation_e_opens_the_editor_on_the_lead_row(self) -> None:
        document = self.blocked_document()
        opened: list = []

        class FakeEditor:
            def __init__(self, state_, **kwargs):
                opened.append((state_, kwargs.get("initial_focus")))

            def run(self, win):
                return None

        with mock.patch.object(claude_multi.tui, "ProfileEditorScreen", FakeEditor):
            result, _win = self.run_card(self.card(self.target(self.runtime, "card-blocked", document)), [ENTER, "e", ESC])
        self.assertIsNone(result)
        self.assertEqual([focus for _state, focus in opened], ["lead", None])
        self.assertEqual(opened[0][0].name, "card-blocked")
        self.assertEqual(self.runtime.launches, [])

    def test_cm_collision_blocks_with_the_exact_error_language(self) -> None:
        rid = next(iter(self.card().lineup.agents))
        agents = Path(self.runtime.cwd) / ".claude" / "agents"
        agents.mkdir(parents=True)
        path = agents / f"{rid}.md"
        path.write_text(f"---\nname: {rid}\ndescription: shadow\n---\nbody\n")
        screen = self.card()
        text = self.frame(screen, height=40, width=200)
        self.assertIn("Status  BLOCKED", text)
        self.assertIn(f"    {cli._collision_error(path, rid, str(self.runtime.cwd))}", text.splitlines())
        self.assertIn("1 project agent discovered (native precedence; collision blocks launch)", text)
        result, _win = self.run_card(screen, [ENTER, ESC])
        self.assertIsNone(result, "Enter never launches a BLOCKED card")

    def _secret_target(self) -> cli.LaunchTarget:
        """An ad-hoc direct target on a derived direct-key lead line."""

        lcat = self.runtime.lineup_catalog()
        key = next(
            (k for k, e in lcat.lines.items()
             if "lead" in e["capabilities"] and isinstance(e.get("lead"), dict)
             and e.get("status", "active") != "new"
             and lcat.providers[e["provider"]]["transport"]["kind"] == "direct"
             and "auth" in lcat.providers[e["provider"]]["transport"]),
            None,
        )
        self.assertIsNotNone(key, f"{SPEC}: a direct-key lead line")
        return cli.LaunchTarget("ad-hoc", profile.ad_hoc_direct(key), None, False, f"Direct {key}")

    def test_secret_problem_blocks_with_the_g_k_remedy_hint(self) -> None:
        runtime = self.make_runtime(self.tmp / "nosecret", secrets=())
        screen = self.card(self._secret_target(), runtime=runtime)
        self.assertTrue(screen.prepared.secret_problems)
        text = self.frame(screen, height=40, width=200)
        self.assertIn("Status  BLOCKED", text)
        for problem in screen.prepared.secret_problems:
            self.assertIn(problem, _flat(text))
        self.assertIn(f"    {views.SECRET_REMEDY}", text.splitlines())
        result, win = self.run_card(screen, [ENTER, ENTER, ESC], height=30)
        self.assertIsNone(result)
        modal = next(f for f in win.frames if cli.CARD_SECRET_TITLE in f)
        self.assertIn(cli.CARD_SECRET_HINT, _flat(modal))
        self.assertEqual(runtime.launches, [])

    def test_resume_secret_problem_names_the_exit_and_retry(self) -> None:
        runtime = self.make_runtime(self.tmp / "resume-nosecret", secrets=())
        target = self._secret_target()
        record = fx.v4_record(runtime, None, managed_id=fx.FIXED_ID, document=target.document)
        prepared = runtime.prepare(
            cli._record_resume_target(record), action="resume", passthrough=[], session_id=fx.FIXED_ID,
        )
        screen = self.resume_card(prepared, runtime=runtime)
        self.assertTrue(prepared.secret_problems)
        way = "Esc, run claude-multi"  # claude-multi -r opened it: Esc ends it
        self.assertIn(f"    {views.SECRET_REMEDY_RESUME.format(way=way)}", self.frame(screen, height=40, width=200))
        self.assertNotIn(views.SECRET_REMEDY + "\n", self.frame(screen, height=40, width=200))
        result, win = self.run_card(screen, [ENTER, ENTER, ESC], height=30)
        self.assertIsNone(result)
        modal = next(f for f in win.frames if cli.CARD_SECRET_TITLE in f)
        self.assertIn(cli.CARD_SECRET_HINT_RESUME.format(way=way), _flat(modal))
        self.assertNotIn(cli.CARD_SECRET_HINT + " ", _flat(modal))
        self.assertEqual(runtime.launches, [])

    def test_ready_when_the_selected_secret_is_present(self) -> None:
        screen = self.card(self._secret_target())
        self.assertEqual(screen.prepared.secret_problems, ())
        text = self.frame(screen)
        self.assertIn("Status  Ready", text)
        self.assertNotIn(views.SECRET_REMEDY, text)

    def test_resume_collision_uses_the_recorded_cwd(self) -> None:
        record, prepared = self.resume_prepared()
        rid = next(iter(prepared.lineup.agents))
        recorded = Path(record["cwd"])
        agents = recorded / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / f"{rid}.md").write_text(f"---\nname: {rid}\ndescription: shadow\n---\nbody\n")
        elsewhere = self.tmp / "root" / "elsewhere"
        elsewhere.mkdir()
        self.runtime.cwd = str(elsewhere)
        self.assertIn("Status  BLOCKED", self.frame(self.resume_card(prepared)))
        # The fresh card of the same runtime reads its own (clean) cwd.
        self.assertIn("Status  Ready", self.frame(self.card()))

    def test_current_effective_error_blocks_with_that_one_error(self) -> None:
        path = settings.settings_path(self.runtime.environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, b"{not json")
        screen = self.card()
        text = self.frame(screen, width=200)
        self.assertIn("Status  BLOCKED", text)
        self.assertIsNone(screen.lineup)
        self.assertNotIn("  lead ", text)
        self.assertEqual(len(screen.card.errors), 1)

    def test_binding_error_blocks(self) -> None:
        path = profile.bindings_path(self.runtime.environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, b"{not json")
        text = self.frame(self.card(), width=200)
        self.assertIn("Status  BLOCKED", text)
        self.assertIn("cannot load named bindings:", text)

    def test_a_resolver_error_omits_the_context_row_only(self) -> None:
        with mock.patch.object(self.runtime, "prepare", side_effect=claude_multi.compiler.CompilerError("compile boom")), \
                mock.patch.object(claude_multi.views, "effective_context", side_effect=claude_multi.scope.ScopeError("fence boom")):
            screen = self.card()
        text = self.frame(screen)
        self.assertIsNotNone(screen.lineup)
        self.assertIn("  lead      ", text)
        self.assertIn("  lineup    ", text)
        self.assertNotIn("  context ", text)
        self.assertIn("Status  BLOCKED", text)
        self.assertIn("compile boom", text)


    def test_a_transient_message_never_joins_the_blocked_errors(self) -> None:
        # Restores ResumeTranscriptBoundaryTests.test_gate_notice_does_not_poison_the_plan
        # on the 3.0 card: a status line (here Tab with one profile) is a
        # message, never one of the BLOCKED card's errors, and the re-plan
        # after the sessions screen (S, then Esc) keeps them as they were.
        # The resume gate modal itself is the sessions screen's
        # (test_screens_sessions.ResumeGateModalTests).
        document = self.blocked_document()
        screen = self.card(self.target(self.runtime, "card-blocked", document))
        screen.pick_order = [screen.target.profile]
        errors_before = tuple(screen.card.errors)
        self.assertTrue(errors_before)
        result, win = self.run_card(screen, [TAB, "s", ESC, ESC])
        self.assertIsNone(result)
        self.assertTrue(any(cli.CARD_ONE_PROFILE in f for f in win.frames))
        self.assertTrue(any("sessions — " in f for f in win.frames), "S opened the sessions screen")
        self.assertEqual(tuple(screen.card.errors), errors_before)
        self.assertNotIn(cli.CARD_ONE_PROFILE, " ".join(screen.card.errors))
        self.assertEqual(self.runtime.launches, [])


# ================================================================ fit


class CardFitTests(_CardCase):
    def assertStatusAndExit(self, text: str, height: int) -> None:
        lines = text.splitlines()
        self.assertEqual(len(lines), height)
        self.assertTrue(any(line.startswith("  Status  ") for line in lines), text)
        self.assertTrue(lines[-1].rstrip().endswith(("Esc", "Esc cancel")), text)

    def test_the_fit_keeps_status_and_the_exit_at_every_height(self) -> None:
        screen = self.card(gateway_problem="boom")
        agents = len(screen.lineup.agents)
        rows, _cols = screen.min_size(80)
        for height in (24, 16, 14, rows):
            with self.subTest(height=height):
                text = self.frame(screen, height=height)
                self.assertStatusAndExit(text, height)
                shown = sum(
                    1 for rid in screen.lineup.agents
                    if any(l.strip().startswith(profile.label(rid) + " ") for l in text.splitlines())
                )
                if shown < agents and height > rows:
                    self.assertIn(f"… {agents - shown} more agents (E shows all)", text)

    def test_resume_rows_are_bounded_by_the_fit_algorithm(self) -> None:
        _record, prepared = self.resume_prepared(change=self.change_a_writer_effort)
        self.assertTrue(prepared.diff)
        screen = self.resume_card(prepared)
        tall = self.frame(screen, height=60)
        self.assertIn(f"  diff      {prepared.diff[0]}"[:79], tall)
        rows, _cols = screen.min_size(80)
        for height in (16, rows):
            with self.subTest(height=height):
                text = self.frame(screen, height=height)
                if height == 16:
                    self.assertIn("(resize to show all)", text)
                    self.assertNotIn("(E shows all)", text)
                self.assertStatusAndExit(text, height)
                bar = tui.KeyBar(screen.card.keys).rows(80)
                drawn = views.card_fit(screen.card, height=height, bar_rows=bar)
                self.assertLessEqual(len(drawn), height - bar - 1 - 3)

    def test_floor(self) -> None:
        screen = self.card()
        rows, cols = screen.min_size(80)
        self.assertEqual(cols, cli.CARD_MIN_COLS)
        self.assertEqual(rows, cli.CARD_CHROME + tui.KeyBar(
            views.card_model(target=screen.target, update_hint=("0", "0")).keys
        ).rows(80))
        too_small = tui.TOO_SMALL.format(what=screen.what)
        self.assertIn(too_small, self.frame(screen, height=rows - 1))
        self.assertNotIn(too_small, self.frame(screen, height=rows))
        narrow_rows, _ = screen.min_size(cols)
        self.assertNotIn(too_small, self.frame(screen, height=narrow_rows, width=cols))
        self.assertIn("Terminal too small", self.frame(screen, height=narrow_rows, width=cols - 1))
        # Below the floor only Esc acts.
        result, _win = self.run_card(screen, ["e", ENTER, "?", ESC], height=rows - 1)
        self.assertIsNone(result)


    def test_a_blocked_card_keeps_status_and_its_first_error_at_80x14(self) -> None:
        # Restores CardBottomReservationTests / FinalGateLayoutTests: the badge
        # and the first error line are never overdrawn at 80x14.
        document = self.blocked_document()
        screen = self.card(self.target(self.runtime, "card-blocked", document))
        rows, _cols = screen.min_size(80)
        self.assertLessEqual(rows, 14)
        text = self.frame(screen, height=14)
        lines = text.splitlines()
        status = next(i for i, line in enumerate(lines) if line.startswith("  Status  "))
        self.assertEqual(lines[status], "  Status  BLOCKED")
        self.assertTrue(lines[status + 1].strip(), "the first error line follows the badge")
        self.assertIn(fx.needs_choice_key(self.runtime), _flat("\n".join(lines[status + 1:])))
        self.assertTrue(lines[-1].rstrip().endswith("Esc"))

    def test_narrow_widths_clip_every_row_and_never_overdraw(self) -> None:
        # Restores QuickConfirmTests.test_quick_summary_wraps_for_narrow_terminals
        # on the curses card (FakeWindow raises on an out-of-bounds write): every
        # drawn line fits the width, and card_text's rule and agent table
        # follow it.  (Line mode prints card_text rows unwrapped.)
        screen = self.card()
        for width in (cli.CARD_MIN_COLS, 56, 71, 80):
            with self.subTest(width=width):
                rows, _cols = screen.min_size(width)
                text = self.frame(screen, height=max(rows, 24), width=width)
                self.assertTrue(all(len(line) <= width for line in text.splitlines()))
                self.assertIn("Status  Ready", text)
                card_lines = views.card_text(screen.card, width).splitlines()
                rule = next(line for line in card_lines if line.startswith("  ─"))
                self.assertLessEqual(len(rule), width)
                header = next(line for line in card_lines if line.strip().startswith("agent "))
                self.assertLessEqual(len(header), width)


# ================================================================ keys


class CardKeyTests(_CardCase):
    def test_enter_on_ready_returns_the_prepared_perform_intent(self) -> None:
        screen = self.card()
        result, _win = self.run_card(screen, [ENTER])
        self.assertEqual(len(result), 2)
        action, prepared = result
        self.assertEqual(action, "perform")
        self.assertIs(prepared, screen.prepared)
        self.assertEqual(prepared.target.profile, catalog.DEFAULT_SEED)
        self.assertEqual(prepared.result.session_action.kind, "fresh")
        self.assertEqual(self.runtime.launches, [], "the card never performs")

    def test_esc_returns_none(self) -> None:
        result, _win = self.run_card(self.card(), [ESC])
        self.assertIsNone(result)

    def test_every_key_dispatches_and_the_keybar_lists_it(self) -> None:
        screen = self.card()
        with mock.patch('claude_multi.cli.screens.providers.run_providers_screen') as providers, \
                mock.patch('claude_multi.cli.screens.models.run_models_screen') as models, \
                mock.patch('claude_multi.cli.screens.settings.run_settings_screen') as settings_, \
                mock.patch('claude_multi.cli.screens.direct.run_direct_screen', return_value=None) as direct, \
                mock.patch('claude_multi.cli.screens.launch_sessions._SessionsScreen') as sessions_screen, \
                mock.patch.object(claude_multi.tui, "ProfileEditorScreen") as editor, \
                mock.patch.object(screen, "_run_health") as health, \
                mock.patch.object(screen, "_details") as details:
            sessions_screen.return_value.run.return_value = None
            result, win = self.run_card(screen, ["g", "m", "o", "d", "s", "e", "h", "v", ESC])
        self.assertIsNone(result)
        for called in (providers, models, settings_, direct, health, sessions_screen, details):
            called.assert_called_once()
        # S opens the 3.0 sessions screen with the card's passthrough.
        self.assertEqual(sessions_screen.call_args.kwargs["passthrough"], screen.passthrough)
        sessions_screen.return_value.run.assert_called_once()
        editor.return_value.run.assert_called_once()
        self.assertEqual(settings_.call_args.kwargs["card_lineup"], screen.lineup)
        self.assertEqual(models.call_args.kwargs["profile_hint"], catalog.DEFAULT_SEED)
        bar = win.frames[0].splitlines()[-2] + " " + win.frames[0].splitlines()[-1]
        for key, label in (("G", "providers"), ("M", "models"), ("O", "settings"), ("D", "direct"),
                           ("S", "sessions"), ("E", "edit"), ("H", "doctor"), ("P", "profiles"), ("V", "details"),
                           ("Tab", "next"), ("?", "help")):
            self.assertIn(f"{key} {label}", bar)

    def test_d_direct_returns_a_perform_intent_with_an_ad_hoc_record(self) -> None:
        pools = {
            p["transport"]["pool"] for p in self.runtime.lineup_catalog().providers.values()
            if p["transport"]["kind"] == "oauth-pool"
        }
        with mock.patch('claude_multi.cli.gateway_facts._oauth_credential_records', lambda _rt: {p: 1 for p in pools}):
            result, _win = self.run_card(self.card(passthrough=["--verbose"]), ["d", ENTER])
        action, prepared = result
        self.assertEqual(action, "perform")
        self.assertEqual(prepared.target.kind, "ad-hoc")
        self.assertIsNone(prepared.record["profile"])
        self.assertEqual(prepared.record["applied"]["agents"], {})
        self.assertIn("--verbose", prepared.result.argv)

    def test_h_runs_doctor_in_place_and_prints_its_sections(self) -> None:
        out = io.StringIO()
        screen = self.card(tty_in=io.StringIO("\n\n\n"), tty_out=out)
        problems, info, attention = cli._collect_doctor_reports(self.runtime)
        result, _win = self.run_card(screen, ["h", ESC])
        self.assertIsNone(result)
        printed = out.getvalue()
        self.assertIn("claude-multi doctor\n", printed)
        self.assertIn("BLOCKED\n" if problems else "Ready\n", printed)
        if attention:
            self.assertIn("Attention\n", printed)
        from claude_multi.cli import doctor as doctor_mod

        hidden = [line for line in info if doctor_mod.detail_line(line)]
        for line in info:
            if line in hidden:
                self.assertNotIn(tui.visible_text(line) + "\n", printed)
            else:
                self.assertIn(tui.visible_text(line), printed)
        if hidden:
            self.assertIn(f"({len(hidden)} more detail line", printed)
        status = "blocked" if problems else ("attention" if attention else "ready")
        self.assertIn(doctor_mod.DOCTOR_SUMMARY[status], printed)
        self.assertNotIn("--repair-all", printed)
        self.assertIn("Press Enter to return to claude-multi.", printed)
        self.assertEqual(self.runtime.launches, [])

    def test_h_offers_a_bulk_repair_only_for_session_repair_findings(self) -> None:
        from claude_multi.cli import doctor as doctor_mod

        cases = (
            ([], ["gateway: not running — claude-multi gateway start"], [], False),
            (["session 1234abcd: scope drift — claude-multi doctor --repair 1234abcd"], [], [], True),
        )
        for problems, attention, info, offered in cases:
            out = io.StringIO()
            screen = self.card(tty_in=io.StringIO("n\n\n\n"), tty_out=out)
            with self.subTest(offered=offered), \
                    mock.patch.object(doctor_mod, "_collect_doctor_reports",
                                      return_value=(problems, info, attention)), \
                    mock.patch("claude_multi.cli.doctor_actions._doctor_repair_all",
                               side_effect=AssertionError("answered no")):
                self.run_card(screen, ["h", ESC])
                printed = out.getvalue()
                self.assertEqual("Repair every stopped session now? [y/N]" in printed, offered)
                status = "blocked" if problems else "attention"
                self.assertIn(doctor_mod.DOCTOR_SUMMARY[status], printed)

    def test_h_offers_the_gateway_log_after_the_report(self) -> None:
        screen = self.card(tty_in=io.StringIO("l\n\n\n"), tty_out=io.StringIO())
        with mock.patch("claude_multi.cli.screens.gateway_actions.health_prompt", return_value="log") as prompt, \
                mock.patch("claude_multi.cli.screens.gateway_actions.log_view",
                           return_value=("gateway log — fixture", ["fixture log line"])):
            _result, win = self.run_card(screen, ["h", ESC, ESC])
        prompt.assert_called_once_with(self.runtime, screen.tty_in, screen.tty_out)
        self.assertTrue(any("fixture log line" in frame for frame in win.frames))

    def _h_gateway_action(self, answer: str, view) -> tuple[list, str]:
        """H on the card with a clean report, then ``answer`` at the gateway line."""

        from claude_multi.cli import doctor as doctor_mod

        out = io.StringIO()
        screen = self.card(tty_in=io.StringIO(f"{answer}\n\n\n"), tty_out=out)
        with mock.patch.object(doctor_mod, "_collect_doctor_reports", return_value=([], [], [])), \
                fx.gateway_actions_seam(view) as acted:
            result, _win = self.run_card(screen, ["h", ESC])
        self.assertIsNone(result)
        return acted, out.getvalue()

    def test_h_then_r_restarts_a_proven_gateway(self) -> None:
        view = fx.GatewayViewStub(restart=True)
        acted, printed = self._h_gateway_action("r", view)
        self.assertEqual(acted, [(view, "restart")])
        self.assertIn("r  restart it now", printed)
        self.assertIn("gateway restart: fixture done", printed)
        self.assertEqual(self.runtime.launches, [])

    def test_h_then_i_installs_the_gateway_service(self) -> None:
        view = fx.GatewayViewStub(service=True)
        acted, printed = self._h_gateway_action("i", view)
        self.assertEqual(acted, [(view, "service")])
        self.assertIn("i  install the gateway service", printed)
        self.assertIn("gateway service: fixture done", printed)

    def test_help_modal_shows_quick_help_and_the_guarantee_panel_without_launching(self) -> None:
        # At 80x24 the wrapped help is longer than the Modal: End scrolls to the panel.
        result, win = self.run_card(self.card(), ["?", PGDN, END, ESC, ESC])
        self.assertIsNone(result)
        first, middle, scrolled = [f for f in win.frames if cli.CARD_HELP_TITLE in f][:3]
        flat = " ".join(" ".join(line.strip(" |").split()) for line in first.splitlines())
        self.assertIn(cli.QUICK_HELP.splitlines()[0], flat)
        self.assertIn("/model lists only the lead set", middle)
        self.assertIn(cli._ScrollModal.SCROLL_HINT, first)
        self.assertIn("-- workflow guarantees --", scrolled)
        self.assertIn("Native workflows (ultracode): ON", scrolled)
        self.assertEqual(self.runtime.launches, [])

    def test_resume_help_shows_only_the_resume_workflow_keys(self) -> None:
        _record, prepared = self.resume_prepared()
        result, win = self.run_card(self.resume_card(prepared), ["?", END, ESC, ESC])
        self.assertIsNone(result)
        first, scrolled = [f for f in win.frames if cli.CARD_HELP_TITLE in f][:2]
        self.assertIn("Enter — resume this session with the lineup shown", first)
        self.assertIn("session's lineup).", first)
        self.assertNotIn(cli.QUICK_HELP.splitlines()[0], first)
        self.assertIn("S — sessions: change this session's lineup", first)
        # The sign-in line names this card's own way back (claude-multi -r: run it again).
        self.assertNotIn("{sign_in}", "\n".join(win.frames))
        self.assertIn('"Esc, run claude-multi, G → L, then S to resume": Esc leaves claude-multi', _flat(first))
        self.assertIn("-- workflow guarantees --", scrolled)
        # The doctor, update, /model, terms and in-session lines are shared;
        # only the keys of a fresh launch and the Esc wording differ.
        tail = cli.QUICK_HELP.splitlines()[cli.QUICK_HELP.splitlines().index(
            next(line for line in cli.QUICK_HELP.splitlines() if line.startswith("/model:"))):]
        self.assertEqual(cli.RESUME_QUICK_HELP.splitlines()[-len(tail):], tail)
        self.assertIn(next(line for line in cli.QUICK_HELP.splitlines() if line.startswith("H — doctor")),
                      cli.RESUME_QUICK_HELP.splitlines())
        self.assertIn("Esc — cancel: back without resuming", cli.RESUME_QUICK_HELP)
        self.assertIn("Esc — quit without launching", cli.QUICK_HELP)
        self.assertEqual(self.runtime.launches, [])

    def test_help_wraps_every_line_and_nothing_is_clipped(self) -> None:
        # The /model rule and the whole panel are readable at 80x24.
        result, win = self.run_card(self.card(), ["?", PGDN, END, ESC, ESC])
        self.assertIsNone(result)
        first, middle, scrolled = [f for f in win.frames if cli.CARD_HELP_TITLE in f][:3]

        def body(frame: str) -> str:
            return " ".join(" ".join(line.strip(" |").split()) for line in frame.splitlines())

        model_rule = next(line for line in cli.QUICK_HELP.splitlines() if line.startswith("/model"))
        self.assertIn(" ".join(cli.QUICK_HELP.splitlines()[0].split()), body(first))
        self.assertIn(" ".join(model_rule.split()), body(middle))
        panel = claude_multi.tui.workflow_guarantee_panel("native").splitlines()
        self.assertIn(" ".join(panel[-1].split()), body(scrolled))
        self.assertIn(" ".join(panel[0].split()), body(scrolled))

    def test_u_shows_what_update_will_do_only_while_the_badge_shows(self) -> None:
        from claude_multi import upgrade

        shown: list[tuple[str, list[str]]] = []
        real_view = tui.TextView

        class RecordingView(real_view):
            def __init__(self, title, lines, **kwargs):
                shown.append((title, list(lines)))
                super().__init__(title, lines, **kwargs)

        with mock.patch.object(claude_multi.tui, "TextView", RecordingView), \
                mock.patch.object(upgrade, "run_repin", side_effect=AssertionError("never the re-pin flow")):
            self.run_card(self.card(), ["u", ESC])
            self.assertEqual(shown, [])
            badge = self.card(update_hint=("1.0.0", "1.0.1"))
            result, _win = self.run_card(badge, ["u", ESC, ESC])
        self.assertIsNone(result)
        self.assertEqual(shown[0][0], "Update claude-multi")
        text = " ".join(shown[0][1])
        self.assertIn("Nothing was changed", text)
        self.assertIn("claude-multi update --rollback", text)  # rollback stays a command-line action
        self.assertEqual(self.runtime.launches, [])

    def test_u_on_the_nix_package_says_to_update_the_flake(self) -> None:
        shown: list[tuple[str, list[str]]] = []
        real_view = tui.TextView

        class RecordingView(real_view):
            def __init__(self, title, lines, **kwargs):
                shown.append((title, list(lines)))
                super().__init__(title, lines, **kwargs)

        with mock.patch.dict(self.runtime.environ, {"CLAUDE_MULTI_CHANNEL": "nix"}), \
                mock.patch.object(claude_multi.tui, "TextView", RecordingView):
            screen = self.card(hint_detector=None)
            self.assertEqual(screen.update_hint[1] if screen.update_hint else
                             screen.hint_detector(None)[1], "updated through your Nix flake")
            self.run_card(self.card(update_hint=("1.0.0", "updated through your Nix flake")), ["u", ESC, ESC])
        self.assertIn("update it through your flake", " ".join(shown[0][1]))

    def test_u_on_an_installed_release_runs_the_update_journey(self) -> None:
        from claude_multi import release_update

        tty_in, tty_out = io.StringIO("n\n\n"), io.StringIO()
        with mock.patch.dict(self.runtime.environ, {"CLAUDE_MULTI_CHANNEL": "bundle"}), \
                mock.patch.object(release_update, "run", side_effect=AssertionError("not checked")):
            screen = self.card(update_hint=("1.0.0", "released 2026-10-15"), tty_in=tty_in, tty_out=tty_out)
            self.run_card(screen, ["u", ESC])
        self.assertIn("Check the release server for a newer claude-multi now? [y/N]", tty_out.getvalue())
        self.assertIn("Nothing was checked.", tty_out.getvalue())
        calls = []
        tty_in, tty_out = io.StringIO("y\n\n"), io.StringIO()
        with mock.patch.dict(self.runtime.environ, {"CLAUDE_MULTI_CHANNEL": "bundle"}), \
                mock.patch.object(release_update, "run", side_effect=lambda journey, **kw: calls.append(
                    (journey.channel, kw["mode"])) or 0):
            screen = self.card(update_hint=("1.0.0", "released 2026-10-15"), tty_in=tty_in, tty_out=tty_out)
            self.run_card(screen, ["u", ESC])
        self.assertEqual(calls, [("bundle", "update")])

    def test_default_card_never_shows_the_update_badge(self) -> None:
        screen = cli._LaunchCardScreen(
            self.runtime, cli.LaunchTarget("profile", self.runtime.profiles.load("balanced"), "balanced", True,
                                           "Profile balanced"),
            passthrough=[], palette=tui.MONO_PALETTE, gateway_checked=False,
            gateway_check=lambda: None,
        )
        self.assertIsNone(screen.hint_detector(self.runtime.catalog.docs["native-contract"]))

    def test_the_quick_help_model_line(self) -> None:
        self.assertIn(
            "/model lists only the lead set; another provider family asks first (Alt+P and "
            "/config block instead); a model outside the set is refused — relaunch with a "
            "profile whose lead is that model.",
            cli.QUICK_HELP.splitlines(),
        )
        self.assertTrue(cli.QUICK_HELP.splitlines()[0].startswith("New here? W — Get started"))
        self.assertEqual(cli.QUICK_HELP.splitlines()[1], "Enter — launch this profile (a fresh durable session).")

    def test_the_quick_help_names_session_verbs_terms_and_update_by_channel(self) -> None:
        from claude_multi import lineup, lineup_files
        import re as re_

        lines = cli.QUICK_HELP.splitlines()
        self.assertEqual(lines[lines.index(lineup.MODEL_HELP_LINES[0]) + 1], lineup.MODEL_HELP_LINES[1])
        named = set(re_.findall(r"/cm (\w+)", cli.QUICK_HELP))
        self.assertTrue(named)
        self.assertLessEqual(named, set(lineup_files.CM_VERB_NAMES))
        self.assertEqual(named, set(lineup_files.CM_VERB_NAMES))
        for needle in ("Terms", "In a claude-multi session", "U — update claude-multi",
                       "a Nix install updates through its flake", "H — doctor", "Git worktree",
                       "CLAUDE_CONFIG_DIR", "kept environment"):
            self.assertIn(needle, cli.QUICK_HELP)
        for claim in ("private", "never leaves", "no telemetry", "nothing is sent"):
            self.assertNotIn(claim, cli.QUICK_HELP.lower())


    def test_esc_in_direct_returns_to_the_card(self) -> None:
        # Restores OrdinaryCardKeyTests.test_cancel_stays_on_card on the 3.0 D key.
        screen = self.card()
        result, win = self.run_card(screen, ["d", ESC, ESC])
        self.assertIsNone(result)
        self.assertTrue(any(cli.DIRECT_TITLE in f for f in win.frames))
        self.assertIn(f"profile: {catalog.DEFAULT_SEED}", win.frames[-1])
        self.assertEqual(self.runtime.launches, [])


class CardEditFlowTests(_CardCase):
    def test_repair_in_the_editor_saves_and_the_card_becomes_ready_and_launches(self) -> None:
        document = self.blocked_document()
        good_lead = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED)["lead"])

        class RepairingEditor:
            """Stands in for the editor: fix the lead, then save through the real callbacks."""

            def __init__(self, state_, *, callbacks, **kwargs):
                self.state = state_
                self.callbacks = callbacks

            def run(self, win):
                self.state.set_binding(catalog.LEAD_ROLE, good_lead["model"], good_lead["effort"])
                assert not self.state.evaluation.errors
                outcome = tui.ProfileEditorOutcome("save", self.state.name, dict(self.state.document))
                ok, _text = self.callbacks.save(win, outcome)
                assert ok
                return outcome

        screen = self.card(self.target(self.runtime, "card-blocked", document))
        self.assertFalse(screen.card.ready)
        with mock.patch.object(claude_multi.tui, "ProfileEditorScreen", RepairingEditor):
            result, win = self.run_card(screen, [ENTER, ENTER])
        self.assertTrue(any("Status  Ready" in f for f in win.frames))
        action, prepared = result
        self.assertEqual(action, "perform")
        self.assertEqual(prepared.target.profile, "card-blocked")
        self.assertEqual(prepared.record["applied"]["lead"]["key"], good_lead["model"])
        self.assertEqual(self.runtime.profiles.load("card-blocked")["lead"], good_lead)

    def test_an_ad_hoc_target_opens_the_editor_on_the_name_and_a_save_retargets(self) -> None:
        target = cli.LaunchTarget(
            "profile-file", copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED)) | {"name": "card-file"},
            None, False, "Profile file /x/card-file.json",
        )
        focus: list = []

        class SavingEditor:
            def __init__(self, state_, *, callbacks, initial_focus=None, **kwargs):
                focus.append(initial_focus)
                self.state, self.callbacks = state_, callbacks

            def run(self, win):
                outcome = tui.ProfileEditorOutcome("new", self.state.name, dict(self.state.document))
                self.callbacks.save(win, outcome)
                self.state.mark_saved(outcome)
                return outcome

        screen = self.card(target)
        self.assertIn("profile: (file card-file.json) · pinned · Fresh", self.frame(screen))
        with mock.patch.object(claude_multi.tui, "ProfileEditorScreen", SavingEditor):
            self.run_card(screen, ["e", ESC])
        self.assertEqual(focus, ["general"])
        self.assertEqual(screen.target.profile, "card-file")
        self.assertNotIn("seed", self.runtime.profiles.load("card-file"))


class CardProfileCycleTests(_CardCase):
    def test_tab_cycles_profiles_in_mru_order(self) -> None:
        screen = self.card()
        order = screen.pick_order
        self.assertEqual(order[0], catalog.DEFAULT_SEED)
        self.assertEqual(order, cli.profile_pick_order(self.runtime, current=catalog.DEFAULT_SEED))
        seen = []
        with mock.patch.object(screen, "_replan", wraps=screen._replan):
            win = FakeWindow([TAB, TAB, BTAB, RIGHT, LEFT, LEFT, ESC], height=24, width=80)
            original = screen._retarget

            def spy(name):
                seen.append(name)
                original(name)

            screen._retarget = spy
            self.assertIsNone(screen.run(win))
        self.assertEqual(seen, [order[1], order[2], order[1], order[2], order[1], order[0]])
        self.assertEqual(screen.target.profile, order[0])

    def test_one_profile_only_says_so(self) -> None:
        screen = self.card()
        screen.pick_order = [catalog.DEFAULT_SEED]
        _result, win = self.run_card(screen, [TAB, ESC])
        self.assertTrue(any(cli.CARD_ONE_PROFILE in f for f in win.frames))

    def test_p_opens_profiles_with_the_fallback_filter(self) -> None:
        # The provider-fallback list became Profiles' F filter: F lists the
        # profiles that declare a primary provider, Enter uses one.
        from claude_multi.setup import profiles as setup_profiles

        fallback = [row.name for row in setup_profiles.rows(self.runtime, fallback_only=True)]
        self.assertTrue(fallback, f"{SPEC}: fixture seeds declare primary_provider")
        screen = self.card()
        _result, win = self.run_card(screen, ["p", "f", HOME, ENTER, ESC])
        listing = next(f for f in win.frames if cli_text.PROFILES_TITLE_FALLBACK in f)
        for name in fallback:
            self.assertIn(name, listing)
        self.assertEqual(screen.target.profile, fallback[0])
        self.assertIn(cli_text.PROFILES_USED.format(name=fallback[0]), win.frames[-1])


class ProfilePickOrderTests(_CardCase):
    """The pick-order rules of the earlier composition picker, on profiles.

    Connected profiles come first; within one readiness group these rules
    hold, so the rule tests run with nothing connected."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(type(self.runtime), "profile_readiness",
                                    lambda _runtime, names, observations=None: {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_connected_profile_comes_first(self) -> None:
        a = self.seeds()[-1]
        ready = (claude_multi.readiness.SlotReadiness("cm-lead", claude_multi.readiness.READY),)
        with self.records(), mock.patch.object(type(self.runtime), "profile_readiness",
                                               lambda _runtime, names, observations=None:
                                               {name: ready for name in names if name == a}):
            self.assertEqual(self.order()[0], a)

    def records(self, *rows):
        """Synthetic v4 records: (profile, cwd or None = here, last_seen)."""

        out = []
        for index, (name, cwd, seen) in enumerate(rows):
            out.append({
                "version": sessions.RECORD_VERSION,
                "managed_id": f"77777777-7777-4777-8777-{index:012d}",
                "profile": name,
                "cwd": str(self.runtime.cwd) if cwd is None else cwd,
                "created_at": "2026-07-20T00:00:00Z",
                "last_seen_at": seen,
            })
        return mock.patch('claude_multi.cli.session_facts._session_records', return_value=out)

    def order(self) -> list[str]:
        return cli.profile_pick_order(self.runtime)

    def seeds(self) -> list[str]:
        return sorted(self.runtime.catalog.seed_profiles)

    def test_never_used_fall_to_the_alphabetical_tail(self) -> None:
        with self.records():
            self.assertEqual(self.order(), self.seeds())

    def test_mru_orders_by_last_seen(self) -> None:
        a, b = self.seeds()[-1], self.seeds()[-2]
        with self.records((b, None, "2026-08-01T10:00:00Z"), (a, None, "2026-08-09T10:00:00Z")):
            self.assertEqual(self.order()[:2], [a, b])

    def test_this_cwd_beats_global_recency(self) -> None:
        a, b = self.seeds()[-1], self.seeds()[-2]
        with self.records((a, "/elsewhere", "2026-08-09T10:00:00Z"), (b, None, "2026-08-01T10:00:00Z")):
            self.assertEqual(self.order()[:2], [b, a])

    def test_ties_break_by_name_ascending(self) -> None:
        a, b = self.seeds()[-1], self.seeds()[-2]
        stamp = "2026-08-09T10:00:00Z"
        with self.records((a, "/elsewhere", stamp), (b, "/elsewhere", stamp)):
            self.assertEqual(self.order()[:2], sorted([a, b]))

    def test_used_here_and_elsewhere_appears_once(self) -> None:
        a = self.seeds()[-1]
        with self.records((a, None, "2026-08-01T10:00:00Z"), (a, "/elsewhere", "2026-08-09T10:00:00Z")):
            order = self.order()
        self.assertEqual(order[0], a)
        self.assertEqual(order.count(a), 1)

    def test_a_deleted_profile_is_not_resurrected(self) -> None:
        with self.records(("card-ghost", None, "2026-08-09T10:00:00Z")):
            self.assertEqual(self.order(), self.seeds())

    def test_ad_hoc_and_2x_records_are_ignored(self) -> None:
        a = self.seeds()[-1]
        with self.records((None, None, "2026-08-09T10:00:00Z")) as patched:
            patched.return_value.append({"version": 3, "composition_name": a, "profile": a,
                                         "cwd": str(self.runtime.cwd), "created_at": "2026-07-20T00:00:00Z",
                                         "last_seen_at": "2026-08-09T10:00:00Z"})
            self.assertEqual(self.order(), self.seeds())

    def test_an_unreadable_record_does_not_poison_the_order(self) -> None:
        last = self.seeds()[-1]
        fx.v4_record(self.runtime, last, managed_id=fx.FIXED_ID)
        bad = self.runtime.session_store.sessions_dir / f"{fx.OTHER_ID}.json"
        state.atomic_write(bad, b"{not json")
        self.assertEqual(self.order()[0], last)

    def test_the_current_profile_goes_first(self) -> None:
        a = self.seeds()[-1]
        with self.records((a, None, "2026-08-09T10:00:00Z")):
            self.assertEqual(cli.profile_pick_order(self.runtime, current=self.seeds()[0])[:2], [self.seeds()[0], a])


# ================================================================ sessions key (S)


class CardSessionsKeyTests(_CardCase):
    """S: the 3.0 sessions screen with this card's passthrough.

    Its ``("perform", prepared, decision)`` is the card's result; the card
    never performs.
    """

    def _transcript(self, record) -> None:
        transcript = (
            Path(self.runtime.environ["HOME"]) / ".claude" / "projects"
            / cli._native_project_slug(record["cwd"]) / f"{sessions.runtime_session_id(record)}.jsonl"
        )
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.touch()  # metadata-only fixture transcript for the resume gate

    def _resumable_v4_record(self, name=catalog.DEFAULT_SEED, **kwargs):
        record = fx.v4_record(self.runtime, name, managed_id=fx.FIXED_ID, **kwargs)
        self._transcript(record)
        self.assertEqual(cli._evaluate_resume_gate(self.runtime, record).kind, "ok")
        return record

    def test_s_resume_returns_a_perform_intent(self) -> None:
        self._resumable_v4_record()
        result, win = self.run_card(self.card(), ["s", ENTER])
        self.assertEqual(result[0], "perform")
        prepared = result[1]
        self.assertIsNone(result[2])
        self.assertEqual(prepared.result.session_action.kind, "resume")
        self.assertEqual(prepared.record["managed_id"], fx.FIXED_ID)
        self.assertTrue(any("sessions — " in f for f in win.frames))
        self.assertEqual(self.runtime.launches, [], "the card never performs")

    def test_s_resume_threads_the_card_passthrough_into_prepare(self) -> None:
        self._resumable_v4_record()
        result, _win = self.run_card(self.card(passthrough=["--verbose"]), ["s", ENTER])
        prepared = result[1]
        self.assertIn("--verbose", prepared.result.argv)
        self.assertEqual(prepared.result.session_action.kind, "resume")
        self.assertEqual(self.runtime.launches, [])

    def test_embedded_sessions_resume_of_a_direct_record(self) -> None:
        # A direct (ad-hoc, agent-less) record resumes
        # from the card's S like any other; the lineup stays direct.
        key = self.runtime.lineup_catalog().resolve_key(
            self.runtime.profiles.load(catalog.DEFAULT_SEED)["lead"]["model"]
        ).key
        record = fx.v4_record(
            self.runtime, None, managed_id=fx.FIXED_ID, document=profile.ad_hoc_direct(key)
        )
        self._transcript(record)
        result, _win = self.run_card(self.card(), ["s", ENTER])
        self.assertEqual(result[0], "perform")
        prepared = result[1]
        self.assertIsNone(prepared.record["profile"])
        self.assertEqual(prepared.record["applied"]["agents"], {})
        self.assertEqual(prepared.record["applied"]["lead"]["key"], key)
        self.assertEqual(self.runtime.launches, [])

    def test_s_esc_returns_to_the_card(self) -> None:
        self._resumable_v4_record()
        screen = self.card()
        result, win = self.run_card(screen, ["s", ESC, ESC])
        self.assertIsNone(result)
        self.assertIn("Status  Ready", win.frames[-1])
        self.assertEqual(self.runtime.launches, [])

    def test_s_on_the_resume_card_opens_the_sessions_screen(self) -> None:
        _record, prepared = self.resume_prepared(change=self.change_a_writer_effort)
        with mock.patch('claude_multi.cli.screens.launch_sessions._SessionsScreen', wraps=cli._SessionsScreen) as spy:
            result, win = self.run_card(self.resume_card(prepared), ["s", ESC, ESC])
        self.assertIsNone(result)
        spy.assert_called_once()
        self.assertTrue(any("sessions — " in f for f in win.frames))
        self.assertEqual(self.runtime.launches, [])


# ================================================================ the resume card


class CardResumeKeyTests(_CardCase):
    def test_resume_card_has_no_lineup_change_keys(self) -> None:
        _record, prepared = self.resume_prepared(change=self.change_a_writer_effort)
        screen = self.resume_card(prepared)
        self.assertEqual(
            screen.card.keys,
            (("Enter", "resume"), ("V", "details"), ("S", "sessions"), ("H", "doctor"), ("?", "help"), ("Esc", "cancel")),
        )
        with mock.patch('claude_multi.cli.screens.providers.run_providers_screen') as providers, \
                mock.patch('claude_multi.cli.screens.models.run_models_screen') as models, \
                mock.patch('claude_multi.cli.screens.settings.run_settings_screen') as settings_, \
                mock.patch('claude_multi.cli.screens.direct.run_direct_screen') as direct, \
                mock.patch.object(claude_multi.tui, "ProfileEditorScreen") as editor:
            result, win = self.run_card(screen, ["t", "c", "r", "p", "d", "g", "m", "o", "e", TAB, BTAB, ESC])
        self.assertIsNone(result)
        for called in (providers, models, settings_, direct, editor):
            called.assert_not_called()
        self.assertIn("Enter resume · V details · S sessions · H doctor · ? help · Esc cancel", win.frames[-1])
        self.assertIs(screen.prepared, prepared)

    def test_e_is_inert_on_a_resume_card_and_never_opens_the_editor(self) -> None:
        _record, prepared = self.resume_prepared(change=self.change_a_writer_effort)
        with mock.patch.object(claude_multi.tui, "ProfileEditorScreen") as editor:
            result, _win = self.run_card(self.resume_card(prepared), ["e", "E", ESC])
        self.assertIsNone(result)
        editor.assert_not_called()

    def test_enter_resumes_with_the_callers_decision(self) -> None:
        _record, prepared = self.resume_prepared(change=self.change_a_writer_effort)
        result, _win = self.run_card(self.resume_card(prepared, resume_decision="force"), [ENTER])
        self.assertEqual(result, ("perform", prepared, "force"))


class CardResumeTests(_CardCase):
    def test_resume_card_shows_the_follow_diff_rows_verbatim(self) -> None:
        record, prepared = self.resume_prepared(change=self.change_a_writer_effort)
        self.assertTrue(prepared.diff)
        text = self.frame(self.resume_card(prepared), height=60, width=200)
        lines = text.splitlines()
        self.assertIn(f"  diff      {prepared.diff[0]}", lines)
        for line in prepared.diff[1:]:
            self.assertIn(f"            {line}", lines)
        self.assertIn(
            f"— profile: {catalog.DEFAULT_SEED} · follows profile · Resume {fx.FIXED_ID[:8]} · lineup gen "
            f"{prepared.record['lineup_generation']}",
            text,
        )
        self.assertIn("Status  Ready", text)

    def test_settings_drift_and_notices_are_informational_rows(self) -> None:
        def drift() -> None:
            self.runtime.settings_store.update(
                lambda d: d.update({settings.COMPACTION_PERCENT_KEY: settings.COMPACTION_PERCENT_MIN}),
                catalog=self.runtime.catalog,
            )

        _record, prepared = self.resume_prepared(change=drift)
        drift_notes = [n for n in prepared.notices if n.startswith("settings changed since the last launch:")]
        self.assertTrue(drift_notes)
        text = self.frame(self.resume_card(prepared), height=60, width=200)
        self.assertIn(f"  note      {drift_notes[0]}", text.splitlines())
        self.assertIn("Status  Ready", text)

    def test_row_18_never_repeats_a_lineup_notice_and_line_mode_prints_it_once(self) -> None:
        lead_provider = self.card().lineup.lead.binding.provider
        self.save_profile("card-notices", lambda d: d.update(primary_provider=lead_provider))
        _record, prepared = self.resume_prepared(name="card-notices")
        messages = [f.message for f in prepared.lineup.notices]
        self.assertTrue(messages, f"{SPEC}: outsider notices")
        text = self.frame(self.resume_card(prepared), height=80, width=200)
        for message in messages:
            self.assertEqual(text.count(message), 1, message)
        out = io.StringIO()
        self.assertFalse(cli._confirm_launch_line(prepared, io.StringIO("q\n"), out, runtime=self.runtime))
        for message in messages:
            self.assertEqual(out.getvalue().count(message), 1, message)

    def test_resume_card_workflows_badge_restates_the_applied_workflows(self) -> None:
        record, prepared = self.resume_prepared(follow=False)
        applied = sessions.applied_document(record)
        self.assertIn(
            f"workflows: {applied['workflows']}", self.frame(self.resume_card(prepared), width=200)
        )
        self.assertEqual(prepared.lineup.workflows, applied["workflows"])

    def test_resume_card_workflows_off_badge_from_a_workflows_off_record(self) -> None:
        self.save_profile("card-wf-off", lambda d: d.update(workflows="off"))
        record, prepared = self.resume_prepared(follow=False, name="card-wf-off")
        self.assertEqual(sessions.applied_document(record)["workflows"], "off")
        self.assertIn("workflows: off", self.frame(self.resume_card(prepared), width=200))

    def test_a_resume_card_needs_the_callers_prepared(self) -> None:
        with self.assertRaises(ValueError):
            self.card(action="resume")


# ================================================================ injection


class CardInjectionTests(_CardCase):
    def test_hostile_project_agent_path_is_sanitized_on_the_card(self) -> None:
        rid = next(iter(self.card().lineup.agents))
        agents = Path(self.runtime.cwd) / ".claude" / "agents"
        agents.mkdir(parents=True)
        hostile = agents / "cm-shadow\x1b[2J.md"
        hostile.write_bytes(f"---\nname: {rid}\ndescription: shadow\n---\nbody\n".encode())
        text = self.frame(self.card(), height=40, width=200)
        self.assertNotIn("\x1b", text)
        self.assertIn("cm-shadow^[", text)
        self.assertIn("Status  BLOCKED", text)

    def test_hostile_profile_description_never_reaches_the_terminal(self) -> None:
        document = self.save_profile("card-hostile", lambda d: d.update(description="evil\x1b[2J\x07"))
        text = self.frame(self.card(self.target(self.runtime, "card-hostile", document)))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)


    def test_a_hostile_profile_name_is_sanitized_on_the_card(self) -> None:
        # Restores TerminalInjectionTests.test_hostile_composition_name_sanitized_in_quick_confirm:
        # names are check_name-constrained on every real path; the card still
        # defends in depth for an in-memory target.
        hostile = "evil\x1b[2J\nname"
        document = copy.deepcopy(self.runtime.profiles.load(catalog.DEFAULT_SEED)) | {"name": hostile}
        screen = self.card(cli.LaunchTarget("profile", document, hostile, True, f"Profile {hostile}"))
        text = self.frame(screen, height=40, width=200)
        self.assertNotIn("\x1b", text)
        self.assertIn("evil^[[2J^Jname", text)

    def test_a_hostile_record_display_is_sanitized_on_the_resume_card_and_in_line_mode(self) -> None:
        # Restores TerminalInjectionTests.test_hostile_record_model_sanitized_in_quick_confirm:
        # the record's lead is a custom line whose display carries control bytes.
        provider = next(
            pid for pid, p in sorted(self.runtime.catalog.providers.items())
            if p["transport"]["kind"] == "direct"
        )
        custom_mod = claude_multi.custom
        custom_mod.add_model(
            self.runtime.environ, "card-hostile-line", wire_model="card-hostile-wire", provider=provider,
            context_tokens=262144, display="evil\x1b[2J\x07 display", created_via="manual",
            catalog_providers=self.runtime.catalog.providers,
        )
        record = fx.v4_record(
            self.runtime, None, managed_id=fx.FIXED_ID, document=profile.ad_hoc_direct("card-hostile-line"),
        )
        prepared = self.runtime.prepare(
            cli._record_resume_target(record), action="resume", passthrough=[], session_id=fx.FIXED_ID
        )
        text = self.frame(self.resume_card(prepared), height=40, width=200)
        self.assertIn("evil^[[2J^G display", text)
        out = io.StringIO()
        self.assertFalse(cli._confirm_launch_line(prepared, io.StringIO("q\n"), out, runtime=self.runtime))
        for rendered in (text, out.getvalue()):
            self.assertNotIn("\x1b", rendered)
            self.assertNotIn("\x07", rendered)
        self.assertIn("evil^[[2J^G display", out.getvalue())


# ================================================================ line mode and entries


class CardLineModeTests(_CardCase):
    def test_line_mode_prints_rows_1_to_16_under_the_line_footer(self) -> None:
        prepared = self.runtime.prepare(self.target(self.runtime), action="fresh", passthrough=[])
        out = io.StringIO()
        self.assertFalse(cli._confirm_launch_line(prepared, io.StringIO("q\n"), out, runtime=self.runtime))
        text = out.getvalue()
        lines = text.splitlines()
        self.assertEqual(lines[0], f"  claude-multi — profile: {catalog.DEFAULT_SEED} · follows profile · Fresh")
        for label in ("  lead      ", "  context   ", "  lineup    ", "  review    ", "  spread    "):
            self.assertTrue(any(line.startswith(label) for line in lines), label)
        for absent in ("Status", "health", "Esc", "  update "):
            self.assertNotIn(absent, text)
        self.assertTrue(text.endswith("Enter launch · q quit: Nothing was launched.\n"), text)

    def test_line_mode_without_a_runtime_omits_the_meter_note(self) -> None:
        other = next(
            name for name in sorted(self.runtime.catalog.seed_profiles)
            if self.runtime.lineup_catalog().providers[
                self.card(self.target(self.runtime, name)).lineup.lead.binding.provider
            ]["adapter"] not in views.COUNT_TOKENS_UPSTREAM_ADAPTERS
        )
        prepared = self.runtime.prepare(self.target(self.runtime, other), action="fresh", passthrough=[])
        with_rt, without = io.StringIO(), io.StringIO()
        cli._confirm_launch_line(prepared, io.StringIO("q\n"), with_rt, runtime=self.runtime)
        cli._confirm_launch_line(prepared, io.StringIO("q\n"), without)
        self.assertIn("context meter approximate", with_rt.getvalue())
        self.assertNotIn("context meter approximate", without.getvalue())


class CardEntryTests(_CardCase):
    """Main's bare launch and ``_resume_flow`` route to the card when curses is OK."""

    # The dynamic patch names resolve to their owner modules.
    OWNER_BY_NAME = {"launch_card": "claude_multi.cli.screens.launch_sessions"}

    def main(self, argv, stdin="", **patches):
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch(f"{self.OWNER_BY_NAME[name]}.{name}", value))
            code = cli.main(argv, runtime=self.runtime, input_stream=io.StringIO(stdin),
                            output_stream=out, interactive=True)
        return code, out.getvalue()

    def test_capable_streams_open_the_card_and_perform_after_teardown(self) -> None:
        calls = []

        def fake_card(runtime, target, **kwargs):
            calls.append((target, kwargs))
            prepared = runtime.prepare(target, action="fresh", passthrough=list(kwargs["passthrough"]))
            return ("perform", prepared)

        with mock.patch.object(claude_multi.tui, "streams_curses_capable", return_value=True):
            code, _out = self.main([], launch_card=fake_card)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        target, kwargs = calls[0]
        # The default profile: here the first connected shipped profile.
        self.assertEqual((target.profile, kwargs["action"]),
                         (self.runtime.selection_default.name, "fresh"))
        self.assertEqual(self.runtime.selection_default.reason, "seed")
        self.assertEqual(len(self.runtime.launches), 1)

    def test_esc_prints_nothing_was_launched(self) -> None:
        with mock.patch.object(claude_multi.tui, "streams_curses_capable", return_value=True):
            code, out = self.main([], launch_card=lambda *a, **k: None)
        self.assertEqual(code, 0)
        self.assertIn("Nothing was launched.", out)
        self.assertEqual(self.runtime.launches, [])

    def test_line_flag_and_curses_failure_take_the_line_confirm(self) -> None:
        with mock.patch.object(claude_multi.tui, "streams_curses_capable", return_value=True):
            code, out = self.main(["--line"], "q\n", launch_card=mock.Mock(side_effect=AssertionError("curses")))
            self.assertEqual(code, 0)
            self.assertIn("Enter launch · q quit: ", out)
            code, out = self.main([], "q\n", launch_card=mock.Mock(side_effect=claude_multi.tui.CursesError("boom")))
        self.assertEqual(code, 0)
        self.assertIn("Enter launch · q quit: ", out)
        self.assertEqual(self.runtime.launches, [])

    def test_a_blocked_lineup_on_the_line_branch_exits_before_any_prompt(self) -> None:
        document = self.blocked_document()
        path = self.tmp / "blocked.json"
        path.write_text(json.dumps(document))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code, out = self.main(["--profile-file", str(path)], "\n")
        self.assertEqual(code, 1)  # a refused launch
        self.assertIn("profile is blocked", err.getvalue())
        self.assertNotIn("q quit", out)

    def test_interrupt_exits_130_and_says_nothing_was_launched(self) -> None:
        with mock.patch.object(claude_multi.tui, "streams_curses_capable", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code, _out = self.main([], launch_card=mock.Mock(side_effect=KeyboardInterrupt))
        self.assertEqual(code, 130)
        self.assertIn("cancelled — nothing was launched", err.getvalue())

    def test_launch_card_reraises_a_curses_start_failure(self) -> None:
        with mock.patch.object(claude_multi.tui, "run_curses_on_streams", side_effect=claude_multi.tui.CursesError("no tty")):
            with self.assertRaises(claude_multi.tui.CursesError):
                cli.launch_card(self.runtime, self.target(self.runtime), action="fresh", passthrough=[],
                                input_stream=io.StringIO(), output_stream=io.StringIO(), no_color=True)

        def started_then_failed(app, *_a, **_k):
            with mock.patch('claude_multi.cli.screens.launch_sessions._LaunchCardScreen.run', side_effect=OSError("mid-session")):
                app(FakeWindow([]))

        with mock.patch.object(claude_multi.tui, "run_curses_on_streams", side_effect=started_then_failed):
            with self.assertRaises(cli.CLIError):
                cli.launch_card(self.runtime, self.target(self.runtime), action="fresh", passthrough=[],
                                input_stream=io.StringIO(), output_stream=io.StringIO(), no_color=True)

    def test_resume_flow_opens_the_resume_card_only_when_the_caller_opts_in(self) -> None:
        record = fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        transcript = (
            Path(self.runtime.environ["HOME"]) / ".claude" / "projects"
            / cli._native_project_slug(self.runtime.cwd) / f"{sessions.runtime_session_id(record)}.jsonl"
        )
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.touch()  # metadata-only fixture transcript for the resume gate
        self.change_a_writer_effort()
        seen = []

        def fake_card(runtime, target, **kwargs):
            seen.append(kwargs)
            return ("perform", kwargs["prepared"], kwargs["resume_decision"])

        common = dict(
            passthrough=[], interactive=True, confirm=True, input_stream=io.StringIO("q\n"),
            output_stream=io.StringIO(), report_stream=io.StringIO(), force=True, print_launch=False,
        )
        with mock.patch.object(claude_multi.tui, "streams_curses_capable", return_value=True), \
                mock.patch('claude_multi.cli.screens.launch_sessions.launch_card', fake_card):
            self.assertEqual(cli._resume_flow(self.runtime, fx.FIXED_ID, **common), 0)
            self.assertEqual(seen, [], "the default line=True keeps the line confirm")
            self.assertEqual(self.runtime.launches, [])
            self.assertEqual(cli._resume_flow(self.runtime, fx.FIXED_ID, line=False, **common), 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual((seen[0]["action"], seen[0]["session_id"], seen[0]["resume_decision"]),
                         ("resume", fx.FIXED_ID, "force"))
        self.assertEqual(len(self.runtime.launches), 1)





class CardQuotaScreenTests(_CardCase):
    runtime_kwargs = {"management": fx.quota_body()}

    def quota_rows(self, screen):
        return [r for r in screen.card.rows if r.kind == "quota"]

    def test_fresh_and_resume_warning_remedies_age_and_readiness(self):
        screen = self.card(now=fx.FIXED_NOW)
        text = self.frame(screen)
        self.assertIn("93%!", text)
        self.assertIn("3d old", text)
        self.assertIn("P provider fallback", text)
        self.assertTrue(screen.card.ready)
        self.assertEqual(self.quota_rows(screen)[0].role, "warn")
        _record, prepared = self.resume_prepared()
        resume = self.resume_card(prepared, now=fx.FIXED_NOW)
        text = self.frame(resume)
        self.assertIn("S Sessions → T", text)
        self.assertNotIn("P provider fallback", text)
        self.assertTrue(resume.card.ready)
        self.assertEqual(len(self.runtime.management_calls), 1)

    def details_lines(self, screen) -> list[str]:
        seen: list[list[str]] = []
        with mock.patch.object(tui.TextView, "run", autospec=True,
                               side_effect=lambda view, win: seen.append(list(view.lines))):
            screen._details(None)
        return seen[0]

    def test_resume_details_never_offer_p_and_show_the_readiness_legend(self):
        """V details on a resume card name the
        resume remedy only, and Per-slot readiness carries the exact legend."""

        from claude_multi import readiness

        _record, prepared = self.resume_prepared()
        lines = self.details_lines(self.resume_card(prepared, now=fx.FIXED_NOW))
        text = "\n".join(lines)
        self.assertNotIn("card's P", text)
        self.assertNotIn("P chooses", text)
        self.assertIn("quota pressure; change the lineup with /cm or Sessions -> T.", text)
        self.assertEqual(lines[lines.index("Per-slot readiness") + 1], readiness.LEGEND)
        fresh = "\n".join(self.details_lines(self.card(now=fx.FIXED_NOW)))
        self.assertIn("quota pressure; P chooses another profile for this new launch.", fresh)

    def test_replans_reuse_even_after_cache_expiry_but_health_refresh_fetches(self):
        screen = self.card(now=fx.FIXED_NOW)
        self.runtime.pool_clock = lambda: 61.0
        for _ in range(3):
            screen._replan()
            self.frame(screen)
        self.assertEqual(len(self.runtime.management_calls), 1)
        screen._refresh_health()
        screen._replan()
        self.assertEqual(len(self.runtime.management_calls), 2)
        screen._refresh_health()
        self.assertEqual(len(self.runtime.management_calls), 2)

    def test_unchecked_open_fetches_once_and_down_never_fetches(self):
        screen = self.card(gateway_checked=False, now=fx.FIXED_NOW)
        self.assertEqual(self.runtime.management_calls, [])
        self.assertFalse(self.quota_rows(screen))
        self.run_card(screen, [ESC])
        self.assertEqual(len(self.runtime.management_calls), 1)
        self.assertTrue(self.quota_rows(screen))
        runtime = self.make_runtime(self.tmp / "down", management=fx.quota_body(), health=503)
        down = self.card(runtime=runtime, gateway_checked=False)
        self.run_card(down, [ESC])
        self.assertEqual(runtime.management_calls, [])
        self.assertFalse(self.quota_rows(down))

    def test_missing_window_unknown_age_and_missing_reset_render(self):
        for label, kwargs, words in (
            ("no-window", dict(window=False, observed=False, reason="quota exhausted"),
             ()),
            ("no-reset", dict(reset=False), ("93%!", "reset unknown", "3d old")),
        ):
            runtime = self.make_runtime(self.tmp / label, management=fx.quota_body(**kwargs))
            for resume in (False, True):
                if resume:
                    record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
                    prepared = runtime.prepare(cli._record_resume_target(record), action="resume",
                                               passthrough=[], session_id=fx.FIXED_ID)
                    screen = self.card(runtime=runtime, action="resume", prepared=prepared, now=fx.FIXED_NOW)
                else:
                    screen = self.card(runtime=runtime, now=fx.FIXED_NOW)
                text = self.frame(screen)
                for word in words:
                    self.assertIn(word, text)
                self.assertTrue(screen.card.ready)
                if label == "no-window":
                    self.assertEqual(self.quota_rows(screen), [], "unknown-age exhaustion is not a current quota hint")

    def test_only_lineup_pools_and_no_quota_for_unknown_lineup(self):
        lcat = self.runtime.lineup_catalog()
        key, line = next((k, v) for k, v in lcat.lines.items()
                         if "lead" in v["capabilities"] and
                         lcat.providers[v["provider"]]["transport"]["kind"] != "oauth-pool")
        document = profile.ad_hoc_direct(key, line["default_effort"])
        target = cli.LaunchTarget("ad-hoc", document, None, False, "Direct")
        screen = self.card(target, now=fx.FIXED_NOW)
        self.assertFalse(self.quota_rows(screen))
        document = self.blocked_document()
        blocked = self.card(self.target(self.runtime, "card-blocked", document), now=fx.FIXED_NOW)
        self.assertIsNone(blocked.lineup)
        self.assertFalse(self.quota_rows(blocked))

    def test_failed_quota_is_not_launch_blocking(self):
        for code in (401, 403, 404, 500):
            runtime = self.make_runtime(self.tmp / str(code), management=code)
            screen = self.card(runtime=runtime)
            self.assertTrue(screen.card.ready)
            self.assertFalse(self.quota_rows(screen))

    def test_line_mode_performs_no_quota_read(self):
        out = io.StringIO()
        with mock.patch.object(self.runtime, "pool_status", side_effect=AssertionError("line mode quota read")):
            result = cli.main(["--line"], runtime=self.runtime, input_stream=io.StringIO("q\n"), output_stream=out, interactive=True)
        self.assertEqual(self.runtime.management_calls, [])
        self.assertFalse(any(line.startswith("  quota ") for line in out.getvalue().splitlines()))


    def test_agent_pool_is_included_but_unused_gateway_pool_is_not(self):
        runtime = self.make_runtime(self.tmp / "agent-pool", management=fx.quota_body(pool="codex"))
        # The balanced fixture's lead is not on codex, but some agents are.
        screen = self.card(runtime=runtime, now=fx.FIXED_NOW)
        providers = runtime.lineup_catalog().providers
        self.assertNotEqual(providers[screen.lineup.lead.binding.provider]["transport"].get("pool"), "codex")
        self.assertIn("codex#1", self.quota_rows(screen)[0].text)
        binding = screen.lineup.lead.binding
        document = profile.ad_hoc_direct(binding.key, binding.effort)
        direct = self.card(cli.LaunchTarget("ad-hoc", document, None, False, "Direct"),
                           runtime=runtime, now=fx.FIXED_NOW)
        self.assertFalse(self.quota_rows(direct))


if __name__ == "__main__":
    unittest.main()
