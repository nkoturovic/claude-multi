"""The command-line contract every ordinary command keeps.

Exit statuses: 0 done, 1 a failure, refusal or unavailable result, 2 an
invalid command line, 3 the person said no, 130 interrupted. Results go to
stdout, errors and questions to stderr; a JSON report prints one document.
Lists and shows read and never write. All observations are fixture seams.
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _tripwire  # noqa: F401  (no live-home access from these tests)
from _layout import REPO_ROOT
from _catalog import FIXTURE_ROOT
from claude_multi import cli, identity
from claude_multi.cli import parser as parser_mod, streams
from claude_multi.cli.commands import lineup as lineup_cmd
import test_cli
from test_cli import FIXED_ID, OTHER_ID, CLITestCase

TWIN_ID = "11111111-2222-4222-8222-222222222222"  # shares FIXED_ID's first 8 characters


class ExitStatusTests(CLITestCase):
    def test_done_is_0_with_the_result_on_stdout(self) -> None:
        code, out, err = self.run_cli_both(["profile", "list"], interactive=False)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("balanced", out)

    def test_a_refusal_is_1_on_stderr_with_nothing_on_stdout(self) -> None:
        code, out, err = self.run_cli_both(["sessions", "show", OTHER_ID], interactive=False)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("claude-multi: ", err)
        self.assertIn("fix: list the sessions: claude-multi sessions list", err)

    def test_an_invalid_command_line_is_2(self) -> None:
        for argv in (["profile", "list", "--bogus"], ["doctor", "--preview"], ["doctor", "--include-live"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    code, out = self.run_cli(argv, interactive=False)
                except SystemExit as exc:  # argparse's own usage error
                    code, out = exc.code, ""
            self.assertEqual((code, out), (2, ""), err.getvalue())

    def test_direct_without_a_model_off_a_terminal_is_an_invalid_command_line(self) -> None:
        code, out, err = self.run_cli_both(["direct"], interactive=False)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--model", err)
        self.assertEqual(self.launches, [])

    def test_a_no_is_3_and_changes_nothing(self) -> None:
        self.save_v4_session()
        code, out, err = self.run_cli_both(["sessions", "forget", FIXED_ID], "n\n", interactive=True)
        self.assertEqual(code, 3, out + err)
        self.assertIn("[y/N]", err)
        self.assertIn("Nothing was forgotten.", out)
        self.assertTrue((self.runtime.session_store.sessions_dir / f"{FIXED_ID}.json").exists())

    def test_forget_off_a_terminal_needs_yes(self) -> None:
        self.save_v4_session()
        code, out, err = self.run_cli_both(["sessions", "forget", FIXED_ID], interactive=False)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--yes", err)
        self.assertTrue((self.runtime.session_store.sessions_dir / f"{FIXED_ID}.json").exists())
        code, out, err = self.run_cli_both(["sessions", "forget", FIXED_ID, "--yes"], interactive=False)
        self.assertEqual(code, 0, err)
        self.assertIn("manage it again with Sessions → L, or: claude-multi sessions link", out)
        self.assertFalse((self.runtime.session_store.sessions_dir / f"{FIXED_ID}.json").exists())

    def test_an_interrupt_is_130_and_says_so(self) -> None:
        with mock.patch("claude_multi.cli.commands.profile._profile_list", side_effect=KeyboardInterrupt):
            code, out, err = self.run_cli_both(["profile", "list"], interactive=False)
        self.assertEqual((code, out), (130, ""))
        self.assertIn("claude-multi: cancelled", err)


class FilesystemFailureTests(CLITestCase):
    def test_no_space_is_one_line_and_a_fix_never_a_traceback(self) -> None:
        where = Path(self.runtime.environ["HOME"]) / ".config" / "claude-multi" / "profiles" / "x.json"
        failure = OSError(errno.ENOSPC, "No space left on device", str(where))
        with mock.patch.dict(os.environ, {"HOME": self.runtime.environ["HOME"]}), \
                mock.patch("claude_multi.cli.commands.profile._profile_list", side_effect=failure):
            code, out, err = self.run_cli_both(["profile", "list"], interactive=False)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("at ~/.config/claude-multi/profiles/x.json", err)
        self.assertIn("\n  fix: ", err)
        self.assertNotIn("Traceback", err)

    def _lineup(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["lineup", *argv], runtime=self.runtime, output_stream=out, interactive=False)
        return code, out.getvalue(), err.getvalue()

    def test_lineup_storage_and_permission_failures_name_the_fix(self) -> None:
        from claude_multi import state

        where = Path(self.runtime.environ["HOME"]) / ".local" / "state" / "claude-multi" / "sessions" / "x.json"
        self.runtime.environ["CLAUDE_CODE_SESSION_ID"] = OTHER_ID  # a terminal run's session comes from here
        for number in (errno.ENOSPC, errno.EACCES, errno.EROFS, errno.EPERM):
            what, remedy = state.FILESYSTEM_FAILURES[number]
            expected = f"claude-multi: {what} at ~/.local/state/claude-multi/sessions/x.json\n  fix: {remedy}\n"
            failure = OSError(number, os.strerror(number), str(where))
            with self.subTest(errno=errno.errorcode[number]), \
                    mock.patch("claude_multi.lineup.apply", side_effect=failure):
                # A terminal run: stderr, exit 1, the remedy, never the errno.
                code, out, err = self._lineup(["show"])
                self.assertEqual((code, out, err), (1, "", expected))
                # /cm (skill mode) keeps its transport: the same text on stdout, status 0.
                code, out, err = self._lineup(["--session", OTHER_ID, "show"])
                self.assertEqual((code, out, err), (0, expected, ""))

    def test_a_lineup_state_error_keeps_its_message_and_gets_the_fix(self) -> None:
        from claude_multi import state

        failure = state.StateError(errno.EPERM, "the state directory is not owner-controlled")
        self.runtime.environ["CLAUDE_CODE_SESSION_ID"] = OTHER_ID
        with mock.patch("claude_multi.lineup.apply", side_effect=failure):
            code, out, err = self._lineup(["show"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("the state directory is not owner-controlled", err)
        self.assertTrue(err.endswith(f"\n  fix: {state.FILESYSTEM_FAILURES[errno.EPERM][1]}\n"), err)

    def test_doctor_reports_a_failed_preparation_from_a_read_only_runtime(self) -> None:
        home = Path(self.runtime.environ["HOME"])
        environ = {"HOME": str(home), "XDG_STATE_HOME": str(home / ".local" / "state"),
                   "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local" / "share")}
        failure = OSError(errno.EACCES, "Permission denied", str(home / ".local" / "state" / "claude-multi"))
        calls = []

        def build(*_args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise failure
            return self.runtime

        out = io.StringIO()
        with mock.patch.dict(os.environ, environ), mock.patch("claude_multi.cli.runtime.Runtime", side_effect=build), \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["doctor"], output_stream=out, interactive=False)
        self.assertEqual(code, 1, out.getvalue())
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[1]["allow_state_writes"])
        self.assertEqual(out.getvalue().splitlines()[0], "BLOCKED")
        self.assertIn("claude-multi could not prepare its state:", out.getvalue())
        self.assertIn("~/.local/state/claude-multi", out.getvalue())


class SessionResolverTests(CLITestCase):
    def test_an_id_prefix_of_eight_or_more_characters_names_a_session(self) -> None:
        self.save_v4_session()
        code, out, err = self.run_cli_both(["sessions", "show", FIXED_ID[:8]], interactive=False)
        self.assertEqual(code, 0, err)
        self.assertIn(FIXED_ID, out)
        code, _out, err = self.run_cli_both(["sessions", "show", FIXED_ID[:7]], interactive=False)
        self.assertEqual(code, 1)

    def test_an_ambiguous_prefix_lists_the_candidates(self) -> None:
        self.save_v4_session()
        self.save_v4_session(session_id=TWIN_ID)
        code, out, err = self.run_cli_both(["sessions", "show", FIXED_ID[:8]], interactive=False)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(FIXED_ID, err)
        self.assertIn(TWIN_ID, err)


class ReadOnlyListTests(CLITestCase):
    def test_sessions_list_json_is_one_stable_document(self) -> None:
        self.save_v4_session()
        code, out, err = self.run_cli_both(["sessions", "list", "--json"], interactive=True)
        self.assertEqual(code, 0, err)
        document = json.loads(out)
        self.assertEqual(document["schema_version"], 1)
        (row,) = document["sessions"]
        self.assertEqual(row["managed_id"], FIXED_ID)
        self.assertLessEqual({"managed_id", "runtime_session_id", "profile", "lead", "live", "last_used", "cwd"},
                             set(row))

    def test_lists_read_only(self) -> None:
        for argv in (["sessions", "list"], ["profile", "list"], ["models", "list"], ["providers", "list"]):
            with self.subTest(argv=argv):
                args = cli.build_parser().parse_args(argv)
                self.assertTrue(parser_mod._read_only_listing(args))

    def test_models_list_matches_the_bare_listing(self) -> None:
        code, listed, err = self.run_cli_both(["models", "list"], interactive=False)
        self.assertEqual(code, 0, err)
        code, bare, _err = self.run_cli_both(["models"], interactive=False)
        self.assertEqual(listed, bare)
        code, out, err = self.run_cli_both(["models", "list", "--json"], interactive=False)
        self.assertEqual(code, 0, err)
        document = json.loads(out)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual([line["key"] for line in document["lines"]], sorted(line["key"] for line in document["lines"]))
        for key in (line["key"] for line in document["lines"]):
            self.assertIn(key, listed)

    def test_providers_list_names_shipped_providers_without_key_values(self) -> None:
        self.runtime.environ["KIMI_CLAUDE_API_KEY"] = "provider-value-never-shown"
        code, out, err = self.run_cli_both(["providers", "list", "--json"], interactive=False)
        self.assertEqual(code, 0, err)
        document = json.loads(out)
        self.assertEqual(document["schema_version"], 1)
        sources = {entry["source"] for entry in document["providers"]}
        self.assertIn("shipped", sources)
        self.assertNotIn("provider-value-never-shown", out)
        code, human, _err = self.run_cli_both(["providers", "list"], interactive=False)
        self.assertNotIn("(no providers.d declarations)", human)
        for entry in document["providers"]:
            self.assertIn(f"{entry['id']}\t{entry['source']} provider", human)

    def test_profile_list_json_follows_the_listing_order(self) -> None:
        code, out, err = self.run_cli_both(["profile", "list", "--json"], interactive=False)
        self.assertEqual(code, 0, err)
        document = json.loads(out)
        self.assertEqual(document["schema_version"], 1)
        code, human, _err = self.run_cli_both(["profile", "list"], interactive=False)
        names = [line.split("\t", 1)[0] for line in human.splitlines()[1:]]
        self.assertEqual([profile["name"] for profile in document["profiles"]], names)


class CommandLineOptionBoundaryTests(CLITestCase):
    """Command-line option boundaries, each through the real
    entry point."""

    def _construction(self, argv: list[str], *, interactive: bool = False) -> tuple[dict, list]:
        """The Runtime keyword arguments and the channel ``record`` flags an
        entry run of ``argv`` asks for (the Runtime is never built)."""

        import claude_multi.cli.runtime as runtime_mod
        from claude_multi import installs

        records: list = []
        real_check = installs.check

        def check(state_root, environ, *, record):
            records.append(record)
            return real_check(state_root, environ, record=False)

        with mock.patch.dict(os.environ, self.runtime.environ, clear=True), \
                mock.patch.object(installs, "check", side_effect=check), \
                mock.patch.object(runtime_mod, "Runtime", side_effect=RuntimeError("intercept")) as factory, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "intercept"):
                cli.main(argv, output_stream=io.StringIO(), interactive=interactive)
        return factory.call_args.kwargs, records

    def test_inspections_build_a_read_only_runtime(self) -> None:
        for argv in (["doctor"], ["doctor", "-v"], ["custom", "list"], ["compose", "list"],
                     ["compose", "show", "balanced"], ["profile", "default"], ["providers", "validate"],
                     ["window-ceiling"], ["setup", "--status"]):
            with self.subTest(argv=argv):
                kwargs, records = self._construction(argv)
                self.assertEqual(records, [False])
                self.assertIs(kwargs["allow_state_writes"], False)
                self.assertIs(kwargs["refresh_shims"], False)
                self.assertIs(kwargs["initialize_session_store"], False)
        # A write keeps the writable Runtime.
        kwargs, records = self._construction(["doctor", "--repair-all"])
        self.assertEqual(records, [True])
        self.assertIs(kwargs["refresh_shims"], True)

    def test_the_root_show_spelling_builds_the_profile_show_runtime(self) -> None:
        flags = ("allow_state_writes", "refresh_shims", "initialize_session_store")
        for name in ([], ["balanced"]):
            with self.subTest(name=name):
                show, show_records = self._construction(["show", *name])
                profile_show, profile_records = self._construction(["profile", "show", *name])
                self.assertEqual(show_records, profile_records)
                self.assertEqual(show_records, [False])
                self.assertEqual({flag: show[flag] for flag in flags}, {flag: profile_show[flag] for flag in flags})
                self.assertEqual({flag: show[flag] for flag in flags}, dict.fromkeys(flags, False))

    def test_update_check_builds_a_read_only_runtime(self) -> None:
        # A move checks the installation's identity with update --check, so
        # it never records the channel, refreshes a shim or creates state.
        release = str(Path(self.runtime.environ["HOME"]) / "release")
        for argv in (["update", "--check"], ["update", "--check", "--from-dir", release],
                     ["update", "--from-dir", release, "--check"]):
            with self.subTest(argv=argv):
                kwargs, records = self._construction(argv)
                self.assertEqual(records, [False])
                self.assertIs(kwargs["allow_state_writes"], False)
                self.assertIs(kwargs["refresh_shims"], False)
                self.assertIs(kwargs["initialize_session_store"], False)
        # Installing and rolling back keep the writable Runtime.
        for argv in (["update"], ["update", "--from-dir", release], ["update", "--from-dir", release, "--yes"],
                     ["update", "--rollback"]):
            with self.subTest(argv=argv):
                kwargs, records = self._construction(argv)
                self.assertEqual(records, [True])
                self.assertIs(kwargs["allow_state_writes"], True)
                self.assertIs(kwargs["refresh_shims"], True)
                self.assertIs(kwargs["initialize_session_store"], True)

    def test_print_launch_builds_a_read_only_runtime(self) -> None:
        for argv in (["--profile", "balanced"], ["--profile-file", "profile.json"],
                     ["-r", FIXED_ID], ["-c"], ["direct", "--model", "qwen38"],
                     ["direct", "-r", FIXED_ID], ["direct", "-c"]):
            with self.subTest(argv=argv):
                preview = [*argv, "--print-launch"]
                args = cli.build_parser().parse_args(preview)
                self.assertTrue(parser_mod._read_only_listing(args))
                self.assertFalse(parser_mod._writes_versioned_state(args))
                kwargs, records = self._construction(preview)
                self.assertEqual(records, [False])
                for flag in ("allow_state_writes", "refresh_shims", "initialize_session_store"):
                    self.assertIs(kwargs[flag], False, flag)
                # The corresponding launch still prepares writable state.
                args = cli.build_parser().parse_args(argv)
                self.assertTrue(parser_mod._writes_versioned_state(args))
                kwargs, records = self._construction(argv)
                self.assertEqual(records, [True])
                for flag in ("allow_state_writes", "refresh_shims", "initialize_session_store"):
                    self.assertIs(kwargs[flag], True, flag)

    def test_print_launch_prepares_without_provisioning_an_endpoint(self) -> None:
        for argv in (["--profile", "balanced", "--print-launch"],
                     ["direct", "--model", "qwen38", "--print-launch"]):
            with self.subTest(argv=argv), \
                    mock.patch.object(self.runtime, "provision_endpoint", side_effect=AssertionError("write")), \
                    mock.patch.object(self.runtime, "prepare", wraps=self.runtime.prepare) as prepare:
                code, out, err = self.run_cli_both(argv, interactive=False)
                self.assertEqual(code, 0, out + err)
                self.assertIs(prepare.call_args.kwargs["read_only"], True)

    def test_a_read_only_lineup_request_builds_a_read_only_runtime(self) -> None:
        import claude_multi.cli.runtime as runtime_mod

        for words, writes in ((["profiles"], False), (["show"], False), (["review"], False),
                              (["fallback", "openai", "--preview"], False), (["pin"], True)):
            with self.subTest(words=words):
                captured: list[dict] = []

                def factory(**kwargs):
                    captured.append(kwargs)
                    raise RuntimeError("intercept")

                with mock.patch.dict(os.environ, self.runtime.environ, clear=True), \
                        mock.patch.object(runtime_mod, "Runtime", side_effect=factory):
                    cli.main(["lineup", "--session", FIXED_ID, *words], runtime=None,
                             input_stream=io.StringIO(), output_stream=io.StringIO(), interactive=False)
                self.assertEqual(len(captured), 1)
                self.assertIs(captured[0]["allow_state_writes"], writes)
                self.assertIs(captured[0]["refresh_shims"], writes)
                self.assertIs(captured[0]["initialize_session_store"], writes)

    def test_bare_direct_off_a_terminal_refuses_before_any_state(self) -> None:
        import claude_multi.cli.runtime as runtime_mod
        from claude_multi import installs

        for argv, interactive in ((["direct"], False), (["direct", "--print-launch"], True),
                                  (["direct", "-r"], True)):
            with self.subTest(argv=argv), mock.patch.dict(os.environ, self.runtime.environ, clear=True), \
                    mock.patch.object(installs, "check", side_effect=AssertionError("channel recorded")), \
                    mock.patch.object(runtime_mod, "Runtime", side_effect=AssertionError("Runtime built")), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                out = io.StringIO()
                code = cli.main(argv, input_stream=io.StringIO() if interactive else None, output_stream=out,
                                interactive=interactive)
            self.assertEqual((code, out.getvalue()), (2, ""), err.getvalue())
            self.assertIn("claude-multi direct", err.getvalue())

    def test_json_lists_print_a_failure_document(self) -> None:
        import claude_multi.cli.runtime as runtime_mod

        failure = OSError(errno.EACCES, "Permission denied", str(Path(self.runtime.environ["HOME"]) / "x"))
        for argv, name in ((["sessions", "list", "--json"], "sessions"), (["profile", "list", "--json"], "profiles"),
                           (["models", "list", "--json"], "models"), (["providers", "list", "--json"], "providers")):
            with self.subTest(argv=argv), mock.patch.dict(os.environ, self.runtime.environ, clear=True), \
                    mock.patch.object(runtime_mod, "Runtime", side_effect=failure), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                out = io.StringIO()
                code = cli.main(argv, output_stream=out, interactive=False)
            self.assertEqual(code, 1)
            document = json.loads(out.getvalue())
            self.assertEqual(document, {"schema_version": 1, "list": name, "status": "unavailable",
                                        "error": cli.entry.LIST_UNAVAILABLE})
            self.assertIn("permission was denied at ~/x", err.getvalue())
            self.assertNotIn("~/x", out.getvalue())
        # A failure inside the list prints the same document.
        with mock.patch("claude_multi.cli.commands.sessions.sessions_list_document",
                        side_effect=cli.CLIError("fixture failure")):
            code, out, err = self.run_cli_both(["sessions", "list", "--json"], interactive=False)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "unavailable")

    def test_unknown_background_liveness_is_null(self) -> None:
        from claude_multi import sessions

        self.save_v4_session()
        self.runtime.background_liveness = lambda: sessions.BackgroundLiveness(False, frozenset(), "fixture unknown")
        code, out, err = self.run_cli_both(["sessions", "list", "--json"], interactive=False)
        self.assertEqual(code, 0, err)
        (row,) = json.loads(out)["sessions"]
        self.assertIsNone(row["live"])
        self.runtime.background_liveness = lambda: sessions.BackgroundLiveness(True, frozenset())
        code, out, err = self.run_cli_both(["sessions", "list", "--json"], interactive=False)
        (row,) = json.loads(out)["sessions"]
        self.assertIs(row["live"], False)

    def test_presentation_flags_route_lineup_in_either_position(self) -> None:
        self.save_v4_session()
        self.runtime.environ["CLAUDE_CODE_SESSION_ID"] = FIXED_ID
        code, plain, err = self.run_cli_both(["lineup", "show"], interactive=False)
        self.assertEqual(code, 0, err)
        for argv in (["--no-color", "lineup", "show"], ["lineup", "--no-color", "show"],
                     ["--line", "lineup", "show"], ["lineup", "--line", "show"],
                     ["--no-color", "--line", "lineup", "--line", "show"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli_both(argv, interactive=False)
                self.assertEqual((code, out, err), (0, plain, ""))

    def test_providers_show_resolves_a_shipped_provider(self) -> None:
        code, out, err = self.run_cli_both(["providers", "show", "anthropic"], interactive=False)
        self.assertEqual(code, 0, err)
        self.assertIn("provider anthropic: shipped provider", out)
        catalog_keys = sorted(key for key, entry in self.runtime.catalog.docs["models-v2"]["models"].items()
                              if entry["provider"] == "anthropic")
        self.assertTrue(catalog_keys)
        for key in catalog_keys:
            self.assertIn(f"  {key}\t", out)
        code, out, err = self.run_cli_both(["providers", "show", "anthropic", "--resolved"], interactive=False)
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(shown["provider_origin"], "catalog")
        self.assertEqual(sorted(shown["catalog_lines"]), catalog_keys)
        # An unknown id still refuses.
        code, out, err = self.run_cli_both(["providers", "show", "nowhere"], interactive=False)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no shipped provider and no providers.d/nowhere.json", err)


class ProviderEditDeclineTests(test_cli.OperatorCommandCase):
    def test_declining_a_provider_edit_is_3_and_writes_nothing(self) -> None:
        from claude_multi import operator as operator_mod

        code, _out, err = self.op(test_cli.ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        path = operator_mod.providers_dir(self.runtime.gateway_environ()) / "acme.json"
        before = path.read_bytes()
        editor = self.root / "edit.sh"
        editor.write_text("#!/bin/sh\nsed -i 's/\"acme\"/\"acme-labs\"/' \"$1\"\n")
        os.chmod(editor, 0o700)
        with mock.patch.dict(self.runtime.environ, {"EDITOR": str(editor)}):
            code, out, err = self.op(["providers", "edit", "acme"], "n\n")
        self.assertEqual(code, 3, out + err)
        self.assertIn("nothing written", err)
        self.assertEqual(path.read_bytes(), before)


class StreamTests(unittest.TestCase):
    """Results follow a redirect; only full-screen and editor commands keep
    the terminal."""

    def stream(self, argv: list[str]) -> str:
        tty, std = io.StringIO(), io.StringIO()
        args = cli.build_parser().parse_args(argv)
        return "stdout" if streams._report_output_stream(args, tty, std) is std else "terminal"

    def test_results_and_reports_go_to_stdout(self) -> None:
        for argv in (["sessions", "stop", FIXED_ID], ["sessions", "resolve-fork", FIXED_ID, OTHER_ID],
                     ["profile", "default", "balanced"], ["providers", "sign-out", "openai"],
                     ["providers", "transport", "anthropic"], ["providers", "enable", "kimi"],
                     ["models", "list"], ["uninstall", "--dry-run"], ["doctor"], ["sessions", "list"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.stream(argv), "stdout")

    def test_full_screen_and_editor_commands_keep_the_terminal(self) -> None:
        for argv in (["direct"], ["profile", "edit", "balanced"], ["providers", "edit", "kimi"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.stream(argv), "terminal")


class HelpBeforeStateTests(unittest.TestCase):
    def test_lineup_help_needs_no_runtime(self) -> None:
        out = io.StringIO()
        with mock.patch("claude_multi.cli.runtime.Runtime", side_effect=AssertionError("no runtime for help")):
            code = cli.main(["lineup", "--help"], output_stream=out, interactive=False)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), lineup_cmd.lineup_help())

    def test_dev_lists_and_prints_the_release_identity(self) -> None:
        from claude_multi import dev

        self.assertIn("claude-multi-dev --version", dev.DEV_HELP)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(dev.main(["--version"]), 0)
        self.assertEqual(out.getvalue(), identity.version_line("claude-multi-dev") + "\n")


class ProcessContractTests(unittest.TestCase):
    """The real entry point in a fresh process with an empty home."""

    def run_module(self, *argv: str) -> tuple[subprocess.CompletedProcess, Path]:
        home = Path(tempfile.mkdtemp(prefix="cm-contract-home-"))
        self.addCleanup(__import__("shutil").rmtree, home, True)
        cwd = Path(tempfile.mkdtemp(prefix="cm-contract-cwd-"))
        self.addCleanup(__import__("shutil").rmtree, cwd, True)
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "PYTHONPATH": str(REPO_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8"}
        result = subprocess.run([sys.executable, "-B", "-m", "claude_multi.cli", *argv], env=env, cwd=cwd,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        return result, home

    def test_an_unknown_command_is_2_on_stderr(self) -> None:
        result, _home = self.run_module("no-such-command")
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("invalid choice", result.stderr)

    def test_lineup_help_writes_nothing(self) -> None:
        result, home = self.run_module("lineup", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, lineup_cmd.lineup_help())
        self.assertEqual(os.listdir(home), [])


class ReadOnlyEntryProcessTests(unittest.TestCase):
    """Real entry points, a foreign cwd and a home with no session store."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cm-print-launch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.cwd = self.root / "project"
        self.home.mkdir()
        self.cwd.mkdir()

    def snapshot(self) -> dict:
        return {str(path.relative_to(self.home)): (
                    path.stat().st_mode, path.stat().st_mtime_ns,
                    path.read_bytes() if path.is_file() else None)
                for path in (self.home, *self.home.rglob("*"))}

    def run_read_only(self, *argv: str) -> subprocess.CompletedProcess:
        # No gateway key, provider credentials or inherited user state. Keep
        # the runner's child-process guard on PYTHONPATH when it is installed.
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home),
               "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "PYTHONPATH": os.pathsep.join((str(REPO_ROOT / "src"), os.environ.get("PYTHONPATH", ""))),
               "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8",
               "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT), "CLAUDE_MULTI_CHANNEL": "source"}
        if "CM_ISOLATION_GUARD_LOG" in os.environ:
            env["CM_ISOLATION_GUARD_LOG"] = os.environ["CM_ISOLATION_GUARD_LOG"]
        before = self.snapshot()
        result = subprocess.run([sys.executable, "-B", "-m", "claude_multi.cli", *argv],
                                env=env, cwd=self.cwd, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(self.snapshot(), before, result.stdout + result.stderr)
        return result

    def run_preview(self, *argv: str) -> subprocess.CompletedProcess:
        return self.run_read_only(*argv, "--print-launch")

    def test_inhibited_gateway_start_refuses_before_any_entry_point_write(self) -> None:
        from claude_multi import gateway_inhibition as gi, state

        root = self.home / ".local" / "state" / "claude-multi"
        gi.begin(root, owner="installer", purpose="fixture install", phase="replace",
                 remedy="finish the fixture install", expiry=gi.deadline(600))
        record = gi.read(root)
        message, remedy = gi.refusal(record, "nothing was started")
        self.assertFalse((root / "sessions").exists())
        self.assertFalse((root / "last-session-by-cwd").exists())
        self.assertFalse((root / "bin").exists())
        self.assertFalse((root / "channel").exists())
        # The owner may keep the start lock throughout its transaction.
        with state.FileLock(gi.record_path(root).parent / "gateway-start"):
            result = self.run_read_only("gateway", "start")
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", f"claude-multi: {message}\n  fix: {remedy}\n"))

    def assert_legacy_preview(self, argv: tuple[str, ...], roles: tuple[str, ...]) -> None:
        config = self.home / ".config" / "claude-multi"
        config.mkdir(parents=True, mode=0o700)
        override = config / "native-contract.json"
        override.write_text('{"version":1,"claude":{"validated_version":"0.0.0"},"effort_vocabulary":[]}\n')
        override.chmod(0o600)
        os.utime(override, ns=(1_600_000_000_000_000_000,) * 2)
        self.assertFalse((self.home / ".local").exists())
        result = self.run_preview(*argv)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("claude argv (after the verified executable):", result.stdout)
        self.assertIn("context windows (ceiling", result.stdout)
        for role in roles:
            self.assertRegex(result.stdout, rf"(?m)^  {role}\s+\S+ · .+ class · window \d+ · compacts at \d+$")
        self.assertRegex(result.stdout, r"(?m)^compaction env \(process and scope settings\): "
                                       r"CLAUDE_CODE_AUTO_COMPACT_WINDOW=\d+ "
                                       r"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=\d+"
                                       r"(?: CLAUDE_CODE_MAX_CONTEXT_TOKENS=\d+)?$")
        self.assertIn("would launch", result.stdout)
        self.assertFalse((self.home / ".local").exists())

    def test_profile_preview_preserves_the_legacy_override_and_prints_the_plan(self) -> None:
        self.assert_legacy_preview(("--profile", "balanced"), ("lead", "explorer", "implementer"))

    def test_direct_preview_preserves_the_legacy_override_and_prints_the_plan(self) -> None:
        self.assert_legacy_preview(("direct", "--model", "qwen38"), ("lead",))

    def test_a_missing_session_refuses_without_initializing_the_empty_home(self) -> None:
        for argv in (("-r", FIXED_ID), ("direct", "-r", FIXED_ID)):
            with self.subTest(argv=argv):
                result = self.run_preview(*argv)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("fix: list the sessions: claude-multi sessions list", result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_a_missing_provider_key_reports_the_normal_remedy_without_writing(self) -> None:
        from claude_multi import profile

        path = self.cwd / "profile.json"
        path.write_text(json.dumps(profile.ad_hoc_direct("qwen38")))
        result = self.run_preview("--profile-file", str(path))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("would fail:", result.stdout)
        self.assertIn("fix: set the provider's API key: claude-multi providers set-key PROVIDER", result.stdout)
        self.assertEqual(list(self.home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
