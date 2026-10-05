"""Complete public help, stable plain-text goldens, hidden hooks and aliases."""

from __future__ import annotations

import argparse
import io
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import re
from pathlib import Path
from unittest import mock

from claude_multi import cli, dev, proxy
from claude_multi.cli import parser as cli_parser
from _catalog import GOLDENS_ROOT
from _golden import assertGolden


@contextmanager
def help_environment():
    env = dict(os.environ, NO_COLOR="1", PYTHON_COLORS="0", COLUMNS="100")
    env.pop("FORCE_COLOR", None)
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env, clear=True):
        # The doc pointers name the installation's documents: an installation
        # without them keeps the help bytes machine-independent.
        with mock.patch('claude_multi.layout.installation', return_value=Path(tmp)):
            yield


def help_golden_files() -> dict[str, bytes]:
    with help_environment():
        # Older argparse repeats the metavar on each alias. Keep one golden
        # for the same grammar, shared by this test and bless.py; all other
        # help bytes still compare exactly.
        root_help = cli.build_parser().format_help().replace(
            "-r [SESSION], --resume [SESSION]", "-r, --resume [SESSION]")
        result = {"claude-multi.txt": root_help.encode()}
        for name, main in (("claude-multi-proxy", proxy.main), ("claude-multi-dev", dev.main)):
            out = io.StringIO()
            with redirect_stdout(out):
                assert main(["--help"]) == 0
            result[f"{name}.txt"] = out.getvalue().encode()
        return result


class HelpTests(unittest.TestCase):
    def test_goldens_are_plain_and_stable(self) -> None:
        for name, data in help_golden_files().items():
            with self.subTest(name=name):
                self.assertNotIn(b"\x1b", data)
                assertGolden(self, GOLDENS_ROOT / "v4" / "help" / name, data)

    def test_goldens_normalise_only_the_argparse_resume_alias(self) -> None:
        with help_environment():
            parser = cli.build_parser()
            canonical = parser.format_help().replace(
                "-r [SESSION], --resume [SESSION]", "-r, --resume [SESSION]")
        legacy = canonical.replace("-r, --resume [SESSION]", "-r [SESSION], --resume [SESSION]")
        for label, text in (("single metavar", canonical), ("repeated metavar", legacy)):
            with self.subTest(format=label), mock.patch.object(cli_parser, "build_parser", return_value=parser), \
                    mock.patch.object(parser, "format_help", return_value=text):
                self.assertEqual(help_golden_files()["claude-multi.txt"], canonical.encode())
        changed = legacy.replace("resume a managed session", "changed help")
        with mock.patch.object(cli_parser, "build_parser", return_value=parser), \
                mock.patch.object(parser, "format_help", return_value=changed):
            self.assertEqual(help_golden_files()["claude-multi.txt"],
                             canonical.replace("resume a managed session", "changed help").encode())

    def test_hidden_hook_is_still_parseable(self) -> None:
        with help_environment():
            parser = cli.build_parser()
            for text in (parser.format_usage(), parser.format_help()):
                self.assertNotIn("session-event", text)
                self.assertNotIn("==SUPPRESS==", text)
            args = parser.parse_args(["session-event", "start", "--managed-id", "11111111-1111-4111-8111-111111111111"])
            self.assertEqual(args.event, "start")
            self.assertEqual(args.command, "session-event")

    def test_every_visible_action_has_help(self) -> None:
        # Hidden on purpose: the hook command and the earlier spellings.
        hidden = {"session-event", "compose", "show", "transition"}

        def walk(parser):
            for action in parser._actions:
                if action.dest in ("help", "version"):
                    continue
                if action.help == argparse.SUPPRESS:
                    if isinstance(action, argparse._SubParsersAction):
                        for name, child in action.choices.items():
                            if name not in hidden:
                                walk(child)
                    continue
                self.assertTrue(action.help, f"{parser.prog}: {action.dest} has no help")
                self.assertNotIn("composition", (action.help or "").lower(), parser.prog)
                if isinstance(action, argparse._SubParsersAction):
                    visible = {item.dest for item in action._choices_actions}
                    for name, child in action.choices.items():
                        if name not in hidden:
                            self.assertIn(name, visible)
                            walk(child)
        with help_environment():
            walk(cli.build_parser())

    def test_epilog_preserves_doc_pointers_last(self) -> None:
        with help_environment():
            text = cli.build_parser().format_help()
        for needle in ("Inside a session: /cm", "-- CLAUDE_ARGS", "claude-multi-proxy"):
            self.assertIn(needle, text)
        # The companion launchers are not part of the ordinary help: `direct`
        # covers the one-model session; the developer tool is a checkout's.
        for needle in ("claude-gateway", "claude-multi-dev"):
            self.assertNotIn(needle, text)
        lines = text.splitlines()
        self.assertEqual(lines[-2], "Symptom → command: CHEATSHEET.md in the claude-multi docs")
        self.assertEqual(lines[-1], "Documentation map: USAGE.md in the claude-multi docs")

    def test_proxy_help_and_unknown_command(self) -> None:
        for argv in ([], ["-h"], ["--help"], ["help"]):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(proxy.main(argv), 0)
                self.assertEqual(out.getvalue(), proxy.PROXY_USAGE + "\n")
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(proxy.main(["unknown"]), 1)
        self.assertEqual(err.getvalue(), "claude-multi-proxy: unknown command: unknown\n" + proxy.PROXY_USAGE + "\n")

    def test_dev_no_arguments_prints_help_but_is_usage_error(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(dev.main([]), 2)
        self.assertEqual(out.getvalue(), dev.DEV_HELP)


class KeptEnvironmentHelpTests(unittest.TestCase):
    """The help says what the keep policy does."""

    def test_the_help_never_offers_to_keep_a_provider_key(self) -> None:
        import claude_multi.cli.text as cli_text
        from claude_multi import compiler, secret_store

        for name, text in (("QUICK_HELP", cli_text.QUICK_HELP), ("RESUME_QUICK_HELP", cli_text.RESUME_QUICK_HELP)):
            with self.subTest(help=name):
                flat = " ".join(text.split())
                self.assertNotIn("provider API keys are removed from the session unless", flat)
                self.assertIn("provider API keys are always removed from the session", flat)
                self.assertIn("any other *_API_KEY variable stays only when Settings → kept environment names it",
                              flat)
        # The policy the text states: a provider's key name is never kept,
        # another tool's is.
        credentials = compiler.credential_env_names({"KIMI_CLAUDE_API_KEY"})
        self.assertIsNotNone(secret_store.env_keep_problem("KIMI_CLAUDE_API_KEY", credential_names=credentials))
        self.assertIsNone(secret_store.env_keep_problem("DOCS_TOOL_API_KEY", credential_names=credentials))


class GroupedRootHelpTests(unittest.TestCase):
    """The root help groups the commands, names the launcher's screens and
    hides the earlier spellings and the hook."""

    def test_every_public_command_is_in_exactly_one_group(self) -> None:
        from claude_multi.cli import parser as parser_mod

        with help_environment():
            root = cli.build_parser()
            text = root.format_help()
        commands = next(action for action in root._actions if isinstance(action, argparse._SubParsersAction))
        grouped = [name for _title, rows in parser_mod.COMMAND_GROUPS for name, _summary in rows]
        self.assertEqual(len(grouped), len(set(grouped)))
        public = {item.dest for item in commands._choices_actions}
        self.assertEqual(set(grouped), public)
        self.assertEqual(set(commands.choices) - public, {"session-event", "compose", "show"})
        for title, rows in parser_mod.COMMAND_GROUPS:
            self.assertIn(f"\n{title}:\n", text)
            for name, _summary in rows:
                self.assertRegex(text, rf"\n  {re.escape(name)} +\S")
        for name in ("compose", "session-event", "--composition", "2.x", "3.0", "kept for 3"):
            self.assertNotIn(name, text)

    def test_the_launcher_and_its_screens_are_named(self) -> None:
        from claude_multi.cli import parser as parser_mod

        with help_environment():
            text = " ".join(cli.build_parser().format_help().split())
        self.assertIn("claude-multi with no command opens the launcher", text)
        for key, name in parser_mod.TUI_SCREENS:
            self.assertIn(f"{key} {name}", text)
        self.assertIn("Examples:", text)

    def test_hidden_spellings_still_parse(self) -> None:
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["--composition", "x"]).composition, "x")
        self.assertEqual(parser.parse_args(["compose", "list"]).compose_command, "list")
        self.assertEqual(parser.parse_args(["show"]).command, "show")

    def test_presentation_flags_work_after_a_command(self) -> None:
        parser = cli.build_parser()
        for argv in (["sessions", "list", "--line"], ["--line", "sessions", "list"],
                     ["doctor", "--no-color", "--line"], ["direct", "--line", "--model", "x"],
                     ["profile", "list", "--no-color"]):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertTrue(args.line or args.no_color)
        plain = parser.parse_args(["sessions", "list"])
        self.assertEqual((plain.line, plain.no_color), (False, False))
        # The hook parser keeps its strict grammar.
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["session-event", "start", "--managed-id", "x", "--line"])


class LineupHelpTests(unittest.TestCase):
    """`claude-multi lineup --help` describes the request grammar before any
    session, state or Runtime is read."""

    def test_help_needs_no_session_or_runtime(self) -> None:
        from claude_multi import lineup_files
        from claude_multi.cli import runtime as runtime_mod

        env = {key: value for key, value in os.environ.items()
               if key not in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_MULTI_MANAGED_ID")}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {**env, "HOME": tmp}, clear=True), \
                mock.patch.object(runtime_mod, "Runtime", side_effect=AssertionError("Runtime built")):
            for argv in (["lineup", "--help"], ["lineup", "-h"], ["lineup", "--relaunch", "--help"]):
                with self.subTest(argv=argv), redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.main(argv), 0)
                text = out.getvalue()
                self.assertTrue(text.startswith("usage: claude-multi lineup"), text)
                for verb in lineup_files.CM_VERBS:
                    self.assertIn(verb.spelled(), text)
                self.assertIn("--preview", text)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_help_inside_the_request_or_skill_mode_is_not_help(self) -> None:
        from claude_multi.cli.commands import lineup as commands_lineup

        self.assertFalse(commands_lineup._help_requested(["set", "--help"]))
        self.assertFalse(commands_lineup._help_requested(["--", "--help"]))
        self.assertFalse(commands_lineup._help_requested(["--session", "11111111-1111-4111-8111-111111111111",
                                                          "--help"]))
        self.assertTrue(commands_lineup._help_requested(["--its-exited", "-h"]))
