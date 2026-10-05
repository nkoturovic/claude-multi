"""domain-error compatibility and concise entry-point refusals."""

from __future__ import annotations

import ast
import errno
import inspect
import io
import pickle
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import claude_multi
from claude_multi import (
    catalog, cli, compiler, composition, continuity, custom, dev, errors,
    hooks, launch, lineup, lineup_log, management, migrate, migrate_profiles, probe,
    profile, proxy, quota, release_manifest, render, scope, service, sessions, settings, state, strict_json,
    transition, upgrade, validate,
)
from _catalog import FIXTURE_ROOT
from _layout import RESOURCES_ROOT
from tests.test_cli import CLITestCase, FIXED_ID


# The historical builtin catches must keep catching all domain classes.
HISTORICAL_BASES = {
    OSError: (state.StateError, state.CommittedStateError),
    RuntimeError: (
        sessions.SessionError, sessions.MigrationBusyError,
        sessions.LegacyRecordError, sessions.PendingForkError,
        sessions.StateMarkerError, launch.LaunchError, launch.GatewayKeyError,
        service.ServiceError, proxy.ProxyError, management.ManagementKeyError,
        release_manifest.ReleaseManifestError, release_manifest.ReleaseManifestUnavailable,
        release_manifest.ReleaseManifestMismatch,
        transition.TransitionError, upgrade.UpgradeError, dev.DevError,
        probe.ProbeError, cli.CLIError, cli.LaunchPlanError, cli.NeedsChoiceError,
    ),
    ValueError: (
        catalog.CatalogError, compiler.CompilerError, composition.CompositionError,
        custom.CustomModelsError, scope.ScopeError, profile.ProfileError,
        profile.ProfileValidationError, profile.BindingError,
        settings.SettingsError, render.RenderError, continuity.ContinuityError,
        strict_json.StrictJSONError, quota.QuotaParseError, validate.SchemaError, hooks.HookInputError,
        lineup_log.LineupLogError, migrate.MigrationError, migrate.RestoreRefused,
        migrate.RestoreStopped, migrate_profiles.ProfileMigrationError,
        migrate_profiles.ProfileMigrationStopped,
    ),
    Exception: (lineup.LineupRefusal,),
}


class ErrorCompatibilityTests(unittest.TestCase):
    def test_historical_bases_and_common_base(self) -> None:
        for builtin, classes in HISTORICAL_BASES.items():
            for cls in classes:
                with self.subTest(cls=cls):
                    self.assertTrue(issubclass(cls, errors.ClaudeMultiError))
                    self.assertTrue(issubclass(cls, builtin))

    def test_oserror_fields_and_remedy_survive_pickle(self) -> None:
        error = state.StateError(errno.EACCES, "m", "/p", remedy="r")
        for candidate in (error, pickle.loads(pickle.dumps(error))):
            with self.subTest(candidate=candidate):
                self.assertEqual(candidate.errno, errno.EACCES)
                self.assertEqual(candidate.filename, "/p")
                self.assertEqual(candidate.strerror, "m")
                self.assertEqual(candidate.remedy, "r")

    def test_remedy_is_optional_and_survives_pickle(self) -> None:
        error = launch.LaunchError("m", remedy="r")
        restored = pickle.loads(pickle.dumps(error))
        self.assertEqual(restored.args, ("m",))
        self.assertEqual(error.remedy, "r")
        self.assertEqual(restored.remedy, "r")
        self.assertIsNone(launch.LaunchError("m").remedy)
        self.assertEqual(str(error), "m")  # remedies are entry-point presentation

    def test_custom_constructors_keep_their_fields(self) -> None:
        marker = sessions.StateMarkerError(9)
        invalid = profile.ProfileValidationError(["first", "second"])
        plan = cli.LaunchPlanError(["blocked"])
        record = {"managed_id": FIXED_ID, "runtime_session_id": FIXED_ID}
        choice = cli.NeedsChoiceError(record, "missing", "choose")
        for error in (marker, invalid, plan, choice):
            with self.subTest(error=type(error)):
                self.assertIsNone(error.remedy)
        self.assertEqual(marker.version, 9)
        self.assertEqual(invalid.errors, ("first", "second"))
        self.assertEqual(str(invalid), "first; second")
        self.assertEqual(plan.problems, ("blocked",))
        self.assertIs(choice.record, record)
        self.assertEqual(choice.key, "missing")

    def test_errors_is_a_leaf_module(self) -> None:
        tree = ast.parse(inspect.getsource(errors))
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any(node.level or (node.module or "").startswith("claude_multi")
                             for node in imports))
        self.assertFalse(any(isinstance(node, ast.Import) for node in ast.walk(tree)))

    def test_cli_top_level_catch_uses_common_base(self) -> None:
        # Internal action catches keep their spelling.
        # Main's canonical owner is claude_multi.cli.entry.
        from claude_multi.cli import entry

        function = ast.parse(inspect.getsource(entry.main)).body[0]
        handlers = [handler for statement in function.body if isinstance(statement, ast.Try)
                    for handler in statement.handlers]
        domain = [handler for handler in handlers
                  if isinstance(handler.type, ast.Attribute)
                  and ast.unparse(handler.type) == "errors.ClaudeMultiError"]
        self.assertEqual(len(domain), 1)
        self.assertEqual(ast.unparse(handlers[handlers.index(domain[0]) - 1].type),
                         "sessions.StateMarkerError")
        self.assertFalse(any(isinstance(node, ast.Name) and node.id == "CLIError"
                             for handler in handlers if handler.type is not None
                             for node in ast.walk(handler.type)))

    def test_packaged_resources_back_the_default_asset_root_and_the_version(self) -> None:
        self.assertEqual(claude_multi.resources_root(), RESOURCES_ROOT)
        with mock.patch('claude_multi.resources_root', return_value=FIXTURE_ROOT):
            self.assertEqual(cli.default_asset_root(), FIXTURE_ROOT)
        with mock.patch.object(claude_multi, "resources_root", return_value=FIXTURE_ROOT):
            expected = strict_json.load(FIXTURE_ROOT / "version.json")["launcher_version"]
            self.assertEqual(claude_multi._load_version(), expected)


class EntryPointTests(CLITestCase):
    def invoke_cli(self, error: Exception, argv: list[str] | None = None):
        out, err = io.StringIO(), io.StringIO()
        argv = argv or ["sessions", "list"]
        # A session event reaches _handle_session_event before
        # (and without) handle_command; the error is raised at that seam.
        seam = ("claude_multi.cli.session_events._handle_session_event" if argv[0] == "session-event"
                else "claude_multi.cli.dispatch.handle_command")
        with mock.patch(seam, side_effect=error), redirect_stderr(err):
            code = cli.main(
                argv, runtime=self.runtime,
                input_stream=io.StringIO(), output_stream=out, interactive=False,
            )
        return code, out.getvalue(), err.getvalue()

    def test_cli_catches_previously_omitted_domains(self) -> None:
        for cls in (
            render.RenderError, proxy.ProxyError, continuity.ContinuityError,
            strict_json.StrictJSONError, validate.SchemaError, lineup_log.LineupLogError,
            transition.TransitionError, upgrade.UpgradeError, scope.ScopeError,
            profile.ProfileError, lineup.LineupRefusal,
        ):
            with self.subTest(cls=cls):
                # A refused or failed ordinary command: exit 1, the error on stderr.
                self.assertEqual(self.invoke_cli(cls("boom")),
                                 (1, "", "claude-multi: boom\n"))

    def test_cli_session_event_uses_stderr_and_exit_one(self) -> None:
        self.assertEqual(
            self.invoke_cli(render.RenderError("boom"),
                            ["session-event", "end", "--managed-id", FIXED_ID]),
            (1, "", "claude-multi: boom\n"),
        )

    def test_state_marker_keeps_special_hook_status(self) -> None:
        error = sessions.StateMarkerError(9)
        for args, expected in (
            (["sessions", "list"], (1, "", f"claude-multi: {error}\n"
                                    + (f"  fix: {error.remedy}\n" if error.remedy else ""))),
            (["session-event", "end", "--managed-id", FIXED_ID],
             (0, "", f"claude-multi: {error}\n")),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.invoke_cli(error, args), expected)

    def test_cli_remedy_and_control_bytes(self) -> None:
        self.assertEqual(self.invoke_cli(launch.LaunchError("boom", remedy="r")),
                         (1, "", "claude-multi: boom\n  fix: r\n"))
        error = launch.LaunchError("boom\x1b[31m\nmore", remedy="r\n\x1b[0m")
        self.assertEqual(self.invoke_cli(error),
                         (1, "", "claude-multi: boom^[[31m\nmore\n  fix: r^J^[[0m\n"))
        self.assertEqual(
            self.invoke_cli(launch.LaunchError("boom", remedy="r"),
                            ["session-event", "end", "--managed-id", FIXED_ID]),
            (1, "", "claude-multi: boom\n  fix: r\n"),
        )

    def test_cli_does_not_hide_programming_errors(self) -> None:
        with self.assertRaisesRegex(TypeError, "bug"):
            self.invoke_cli(TypeError("bug"))

    def test_proxy_corrupt_custom_registry_is_one_line(self) -> None:
        env = {**self.runtime.environ, "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT)}
        state.ensure_private_dir(custom.registry_path(env).parent)
        state.atomic_write(custom.registry_path(env), b"{broken")
        out, err = io.StringIO(), io.StringIO()
        execve = mock.Mock(side_effect=AssertionError("must not exec"))
        with redirect_stderr(err), redirect_stdout(out):
            code = proxy.main(["run", "--state-root", str(self.root / "proxy-state")],
                              environ=env, execve=execve)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertTrue(err.getvalue().startswith(
            "claude-multi-proxy: cannot load the custom registry"))
        execve.assert_not_called()

    def test_proxy_domain_remedy_and_oserror_fallback(self) -> None:
        for error, expected in (
            (catalog.CatalogError("boom", remedy="r"),
             "claude-multi-proxy: boom\n  fix: r\n"),
            (OSError(errno.EACCES, "denied", "/fixture"),
             "claude-multi-proxy: denied (/fixture)\n"),
        ):
            with self.subTest(error=type(error)):
                err = io.StringIO()
                with mock.patch.object(proxy, "cmd_run", side_effect=error), redirect_stderr(err):
                    code = proxy.main(["run"], environ=self.runtime.environ)
                self.assertEqual(code, 1)
                self.assertEqual(err.getvalue(), expected)

    def test_dev_catches_compiler_error(self) -> None:
        err = io.StringIO()
        with mock.patch.object(dev, "smoke_test", side_effect=compiler.CompilerError("boom")), \
                redirect_stderr(err):
            code = dev.main(["smoke-test", "fixture-model"])
        self.assertEqual(code, 2)
        self.assertEqual(err.getvalue(), "claude-multi-dev: boom\n")

    def test_probe_catches_other_domains_and_oserror(self) -> None:
        for error in (compiler.CompilerError("boom"), OSError("boom")):
            with self.subTest(error=type(error)):
                err = io.StringIO()
                with mock.patch.object(probe, "build_fixture", side_effect=error), redirect_stderr(err):
                    code = probe.probe_cli(
                        ["init"], {"allow-local-claude": True, "fixture-root": str(self.root)},
                        [], environ={"HOME": str(self.root)},
                    )
                self.assertEqual(code, 2)
                self.assertEqual(err.getvalue(), "claude-multi-dev: probe: boom\n")


if __name__ == "__main__":
    unittest.main()
