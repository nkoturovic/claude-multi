"""CLI, quick-confirm, persistence, and resume UX tests."""

from __future__ import annotations

import argparse
import contextlib
import copy
import curses
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, cli, composition, scope as scope_mod, sessions, state, strict_json, tui
from claude_multi.tui import workflow_guarantee_panel
from claude_multi.cli import gateway_facts
from _catalog import (
    FIXTURE_GATEWAY_TOKEN,
    FIXTURE_ROOT,
    served_selectors,
    uses_shipped_catalog,
)
from _layout import REPO_ROOT, RESOURCES_RELATIVE
import _v3
import claude_multi.catalog
import claude_multi.compiler
import claude_multi.continuity
import claude_multi.custom
import claude_multi.hooks
import claude_multi.launch
import claude_multi.pin
import claude_multi.management
import claude_multi.profile
import claude_multi.proxy
import claude_multi.render
import claude_multi.service
import claude_multi.sessions
import claude_multi.state
import claude_multi.tui


CATALOG_ROOT = FIXTURE_ROOT
FIXED_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"


def _write_composition(runtime: cli.Runtime, document: dict) -> Path:
    """Write ``<config root>/compositions/<name>.json`` for a test.

    The store is read-only (its writers went with the 2.x
    editor glue) and no longer creates ``compositions/``, and
    ``state.atomic_write`` never creates parents: the directory is made with
    ``state.ensure_private_dir`` first.
    """

    directory = state.ensure_private_dir(runtime.compositions.compositions_dir)
    path = runtime.compositions._path(document["name"])
    assert path.parent == directory
    state.atomic_write(path, strict_json.pretty_file_bytes(document))
    return path


def _write_stand_in_scope(runtime: cli.Runtime, session_id: str) -> None:
    """A staged stand-in scope for a v3 record (no 2.x compile remains)."""

    plan = scope_mod.ScopePlan(
        agent_files={}, settings={"stand-in": True}, other_files={}
    )
    scope_mod.write_scope(runtime.session_store.root, session_id, plan)


class CLITestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-cli-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.launches = []
        secret_dir = state.ensure_private_dir(self.root / "secrets")
        self.secret_file = secret_dir / "claude.env"
        state.atomic_write(self.secret_file, b"KIMI_CLAUDE_API_KEY=cli-test-dummy\n")
        env = {
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "TERM": "dumb",
            "CLAUDE_MULTI_SECRET_ENV": str(self.secret_file),
        }
        # A real record's cwd always exists at save time; the cwd-missing
        # resume gate depends on that. Gate tests delete/relocate explicitly.
        (self.root / "project").mkdir(parents=True)
        # Hermetic loopback gateway: a 0600 fixture token in the
        # runtime HOME, a fixture served set covering every selector the
        # catalog can serve, and a healthy fixture /healthz. No test in this
        # class reaches the running gateway on 127.0.0.1:8317.
        token_dir = state.ensure_private_dir(
            Path(env["HOME"]) / ".config" / "claude-multi"
        )
        state.atomic_write(
            token_dir / "api-key", (FIXTURE_GATEWAY_TOKEN + "\n").encode("ascii")
        )
        self.served = set(served_selectors(CATALOG_ROOT))
        self.runtime = cli.Runtime(
            listener_owner=lambda _base: claude_multi.service.OwnerVerdict("ours", "fixture gateway"),
            background_liveness=lambda: sessions.BackgroundLiveness(True, cli._live_background_prefixes(self.root / "daemon")),
            managed_root=self.root / "managed",
            proc_root=self.root / "proc",
            asset_root=CATALOG_ROOT,
            environ=env,
            cwd=self.root / "project",
            launch_callback=lambda prepared: self.launches.append(prepared) or 0,
            doctor_callback=lambda _runtime: [],
            doctor_binary_callback=lambda _contract: (
                [],
                ["Managed Claude 2.1.217 verified (fixture)."],
            ),
            doctor_daemon_callback=lambda: claude_multi.launch.DaemonStatus(
                state="absent", summary="fixture daemon absent"
            ),
            served_models_callback=self._served_models,
            health_get=self._health_get,
        )

    def _served_models(self, gateway, token):
        """Fixture /v1/models: ``self.served`` (None = gateway down).

        Routed through ``cli.launch.served_models`` with a fixture getter so
        tests that patch that function keep controlling the result, while
        the unpatched path never opens a socket.
        """

        def models_get(_base_url, _token):
            if self.served is None:
                raise ConnectionRefusedError("fixture gateway down")
            return 200, set(self.served)

        return claude_multi.launch.served_models(gateway, token, models_get=models_get,
                                       owner_check=lambda _base: claude_multi.service.OwnerVerdict("ours", "fixture"))

    def _health_get(self, _base_url, _health_path):
        return 200

    def _runtime_seams(self) -> dict:
        """Gateway seams for extra Runtimes built inside a test."""

        return {
            "listener_owner": lambda _base: claude_multi.service.OwnerVerdict("ours", "fixture gateway"),
            "background_liveness": lambda: sessions.BackgroundLiveness(True, cli._live_background_prefixes(self.root / "daemon")),
            "managed_root": self.root / "managed",
            "proc_root": self.root / "proc",
            "served_models_callback": self._served_models,
            "health_get": self._health_get,
        }

    def run_cli(self, argv, text="", *, interactive=True):
        output = io.StringIO()
        code = cli.main(
            argv,
            runtime=self.runtime,
            input_stream=io.StringIO(text) if interactive else None,
            output_stream=output,
            interactive=interactive,
        )
        return code, output.getvalue()

    def run_cli_both(self, argv, text="", *, interactive=True):
        """``(exit, stdout, stderr)``."""

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code, output = self.run_cli(argv, text, interactive=interactive)
        return code, output, errors.getvalue()

    def run_cli_err(self, argv, text="", *, interactive=True):
        """``(exit, stderr)``: a refused or failed command reports there."""

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code, _output = self.run_cli(argv, text, interactive=interactive)
        return code, errors.getvalue()

    def _write_transcript(self, runtime_id: str) -> Path:
        """Metadata-only fixture transcript for the resume gate."""

        transcript = (
            Path(self.runtime.environ["HOME"])
            / ".claude"
            / "projects"
            / cli._native_project_slug(self.runtime.cwd)
            / f"{runtime_id}.jsonl"
        )
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.touch()
        return transcript

    def save_session(
        self,
        document=None,
        *,
        session_id=FIXED_ID,
        forked_from=None,
        mode="legacy",
        scope_generation=0,
    ):
        document = document or _v3.default_composition()
        record = _v3.make_record(
            session_id=session_id,
            cwd=self.runtime.cwd,
            composition_name=document["name"],
            snapshot=_v3.snapshot_for(self.runtime.catalog.docs, document),
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
            forked_from=forked_from,
            mode=mode,
            scope_generation=scope_generation,
            now="2026-07-21T00:00:00Z",
        )
        _v3.save_any(self.runtime.session_store, record)
        self.runtime.session_store.update_last(self.runtime.cwd, session_id)
        # Fixture transcript so the resume gate sees "present" by default
        # (metadata-only existence; gate tests delete/relocate explicitly).
        self._write_transcript(session_id)
        return record

    def prepare_direct_v4(self, model, **kwargs):
        """``direct --model M`` prepared on the one path (no state)."""

        return self.runtime.prepare(
            cli.LaunchTarget(
                "ad-hoc", claude_multi.profile.ad_hoc_direct(model), None, False, f"Direct {model}"
            ),
            action="fresh",
            passthrough=[],
            **kwargs,
        )

    def save_prepared_v4(self, prepared):
        record = {**prepared.record, "mutation_token": sessions.new_mutation_token()}
        sessions.ensure_state_v4(self.runtime.session_store.root)
        self.runtime.session_store.save(record)
        return self.runtime.session_store.load(record["managed_id"])

    def save_v4_session(
        self,
        profile_name="balanced",
        *,
        session_id=FIXED_ID,
        follow=None,
        target=None,
        write_scope=False,
        last_event_source=None,
    ):
        """A committed v4 record as a real fresh launch would write it."""

        if target is None:
            target = cli.LaunchTarget(
                "profile",
                self.runtime.profiles.load(profile_name),
                profile_name,
                True,
                f"Profile {profile_name}",
            )
        with mock.patch.object(sessions.SessionStore, "new_id", lambda _store: session_id):
            prepared = self.runtime.prepare(target, action="fresh", passthrough=[])
        record = {**prepared.record, "mutation_token": sessions.new_mutation_token()}
        if follow is not None:
            record["follow"] = bool(follow)
        if last_event_source is not None:
            record["last_event_source"] = last_event_source
        sessions.ensure_state_v4(self.runtime.session_store.root)
        if write_scope:
            scope_mod.swap_scope(
                self.runtime.session_store.root, session_id, prepared.result.scope_plan
            )
        self.runtime.session_store.save(record)
        self.runtime.session_store.update_last(self.runtime.cwd, session_id)
        self._write_transcript(record["runtime_session_id"])
        return record


class QuickConfirmTests(CLITestCase):
    """The line confirm of a bare interactive launch (the card's line form)."""

    def test_ready_one_enter_launches_once(self) -> None:
        code, output = self.run_cli([], "\n")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.launches), 1)
        # The line body is the card's line form.
        self.assertIn("claude-multi — profile: balanced · follows profile · Fresh", output)
        self.assertIn("Enter launch · q quit: ", output)

    def test_cancel_does_not_launch_or_write_session(self) -> None:
        before = list(self.runtime.session_store.sessions_dir.iterdir())
        code, output = self.run_cli([], "q\n")
        self.assertEqual(code, 0)
        self.assertEqual(self.launches, [])
        self.assertEqual(list(self.runtime.session_store.sessions_dir.iterdir()), before)
        self.assertIn("Nothing was launched.", output)

    def test_eof_does_not_launch(self) -> None:
        code, _ = self.run_cli([], "")
        self.assertEqual(code, 0)
        self.assertEqual(self.launches, [])

    def test_interrupt_does_not_launch(self) -> None:
        class Interrupting(io.StringIO):
            def readline(self, *args, **kwargs):
                raise KeyboardInterrupt

        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = cli.main(
                [], runtime=self.runtime, input_stream=Interrupting(), output_stream=output, interactive=True
            )
        # Ctrl-C: exit 130 and the cancellation on stderr.
        self.assertEqual(code, 130)
        self.assertIn("cancelled — nothing was launched", errors.getvalue())
        self.assertEqual(self.launches, [])

    def test_invalid_profile_is_blocked_before_any_prompt(self) -> None:
        path = self.root / "blocked.json"
        document = self.runtime.profiles.load("balanced")
        document["agents"]["cm-analyst"] = {"model": "sol", "effort": "max"}
        path.write_text(json.dumps(document))
        code, output = self.run_cli_err(["--profile-file", str(path)], "\nq\n")
        self.assertEqual(code, 1)
        self.assertEqual(self.launches, [])
        self.assertIn("profile is blocked:", output)
        self.assertIn("agents.cm-analyst.effort", output)
        self.assertNotIn("Traceback", output)

    def test_line_view_has_the_lineup_hierarchy(self) -> None:
        # The card's line rows; the /cm help
        # line belonged to render_text, which the line body no longer prints.
        _, output = self.run_cli([], "q\n")
        lines = output.splitlines()
        self.assertTrue(lines[0].lstrip().startswith("claude-multi — profile:"), lines[0])
        for label in ("lead", "context", "lineup"):
            self.assertTrue(any(line.startswith(f"  {label:<10}") for line in lines), label)

    def test_profile_without_a_lead_is_refused_without_traceback(self) -> None:
        path = self.root / "no-lead.json"
        document = self.runtime.profiles.load("balanced")
        del document["lead"]
        path.write_text(json.dumps(document))
        code, output = self.run_cli_err(["--profile-file", str(path)], "\nq\n")
        self.assertEqual(code, 1)
        self.assertEqual(self.launches, [])
        self.assertIn("cannot load profile file", output)
        self.assertNotIn("Traceback", output)


class InteractivePassthroughTests(CLITestCase):
    def test_structural_and_contingency_tails_block_before_enter(self) -> None:
        cases = (
            (["--", "--model", "raw"], "--model"),
            (["--", "--model=raw"], "--model=raw"),
            (["--", "-r" + FIXED_ID], "-r" + FIXED_ID),
            (
                ["--", "--append-system-prompt-file", "/tmp/prompt"],
                "--append-system-prompt-file",
            ),
        )
        for argv, token in cases:
            with self.subTest(argv=argv):
                code, output = self.run_cli_err(argv, "q\n")
                self.assertEqual(code, 2)
                self.assertIn(token, output)
                self.assertIn("launcher-owned", output)
                self.assertNotIn("Enter launch", output)
                self.assertEqual(self.launches, [])

    def test_safe_interactive_tail_stays_ready_and_byte_order_preserved(self) -> None:
        tail = ["--verbose", "hello world", "--debug"]
        code, output = self.run_cli(["--", *tail], "\n")
        self.assertEqual(code, 0, output)
        self.assertIn("Enter launch", output)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.launches[0].result.argv[-len(tail):], tail)


class NoninteractiveAndParserTests(CLITestCase):
    def test_noninteractive_requires_explicit_profile(self) -> None:
        code, output = self.run_cli_err([], interactive=False)
        self.assertEqual(code, 2)
        self.assertIn("requires --profile NAME or --profile-file PATH", output)
        self.assertEqual(self.launches, [])

    def test_noninteractive_explicit_profile_launches_without_a_prompt(self) -> None:
        code, output = self.run_cli(["--profile", "balanced"], interactive=False)
        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        self.assertEqual(len(self.launches), 1)

    def test_composition_alias_maps_a_profile_name_and_refuses_a_2x_one(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code, output = self.run_cli(["--composition", "balanced"], interactive=False)
        self.assertEqual(code, 0, output)
        self.assertIn(
            "claude-multi: '--composition' is the earlier spelling of '--profile'; the alias stays accepted",
            stderr.getvalue(),
        )
        code, output = self.run_cli_err(["--composition", "default"], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn(cli.PROFILE_IS_COMPOSITION.format(name="default"), output)

    def test_safe_passthrough_preserves_order_and_bytes(self) -> None:
        code, _ = self.run_cli(
            ["--profile", "balanced", "--", "--verbose", "hello world"],
            interactive=False,
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.launches[0].result.argv[-2:], ["--verbose", "hello world"])

    def test_structural_passthrough_rejected_by_launch_validation(self) -> None:
        code, output = self.run_cli_err(
            ["--profile", "balanced", "--", "--model=raw"],
            interactive=False,
        )
        self.assertEqual(code, 2)
        self.assertIn("launcher-owned", output)
        self.assertEqual(self.launches, [])

    def test_continue_after_separator_is_not_launcher_control(self) -> None:
        code, output = self.run_cli_err(
            ["--profile", "balanced", "--", "-c"], interactive=False
        )
        self.assertEqual(code, 2)
        self.assertIn("launcher-owned", output)

    def test_passthrough_rejected_for_management_command(self) -> None:
        code, output = self.run_cli_err(["models", "--", "hello"])
        self.assertEqual(code, 2)
        self.assertIn("accepted only for a launch or direct", output)

    def test_complete_parser_surface(self) -> None:
        parser = cli.build_parser()
        samples = [
            ["compose", "list"], ["compose", "show", "default"],
            ["compose", "new", "x"], ["compose", "edit", "default"],
            ["compose", "duplicate", "default", "x"],
            ["compose", "rename", "x", "y"], ["compose", "delete", "x"],
            ["compose", "restore-default"],
            ["compose", "use-as-template", "default", "x"],
            # The 3.0 forms.
            ["profile", "list"], ["profile", "show"], ["profile", "show", "balanced"],
            ["profile", "new", "x"], ["profile", "new", "x", "--from", "balanced"],
            ["profile", "edit", "x"], ["profile", "rm", "x"],
            ["profile", "rename", "x", "y"], ["profile", "duplicate", "balanced", "x"],
            ["profile", "reseed", "balanced"], ["profile", "migrate"],
            ["profile", "migrate", "--dry-run"], ["profile", "migrate", "--apply"],
            ["lineup"], ["lineup", "--session", FIXED_ID, "set implementer=sol"],
            ["lineup", "--relaunch", "--its-exited", "profile", "quality"],
            ["migrate"], ["migrate", "--dry-run", "--json"], ["restore-2x"],
            ["restore-2x", "--not-running", FIXED_ID],
            ["sessions", "link", FIXED_ID, "--profile", "balanced"],
            ["sessions", "link", FIXED_ID, "--direct", "sol"],
            ["sessions", "link", FIXED_ID, "--composition", "balanced"],
            ["sessions", "link", FIXED_ID, "--model", "sol"],
            ["sessions", "mark-ended", FIXED_ID], ["sessions", "mark-ended", "--all-dead"],
            ["doctor", "--repair-all", "--include-live"],
            ["--profile", "balanced"], ["--profile-file", "-"], ["--composition-file", "-"],
            ["sessions", "list"], ["sessions", "show", FIXED_ID],
            ["sessions", "forget", FIXED_ID], ["sessions", "link", FIXED_ID],
            ["sessions", "transition", FIXED_ID, "--composition", "default"],
            ["sessions", "transition", FIXED_ID, "--composition", "default", "--its-exited"],
            ["models"], ["show"], ["show", "default"], ["doctor"],
            ["doctor", "--prune"], ["doctor", "--repair", FIXED_ID],
            ["-c"], ["-r", FIXED_ID], ["--composition", "default"],
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertIsNotNone(parser.parse_args(sample))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["profile", "migrate", "--dry-run", "--apply"])


class RememberedAndResumeTests(CLITestCase):
    """The interactive default, resume by record, overrides refused."""

    def test_remembered_profile_of_this_cwd_precedes_the_seed(self) -> None:
        self.runtime.profiles.duplicate("balanced", "project")
        self.save_v4_session("project")
        code, output = self.run_cli([], "q\n")
        self.assertEqual(code, 0)
        self.assertIn("profile: project · follows profile", output)
        self.assertIn("Nothing was launched.", output)
        self.assertEqual(self.launches, [])

    def test_continue_without_pointer_is_actionable(self) -> None:
        code, output = self.run_cli_err(["-c"], "")
        self.assertEqual(code, 1)
        self.assertIn("no managed session", output)

    def test_exact_resume_of_a_2x_record_shows_the_migration_and_quits(self) -> None:
        self.save_session()
        code, output = self.run_cli(["-r", FIXED_ID], "q\n")
        self.assertEqual(code, 0, output)
        self.assertIn(f"Resume {FIXED_ID[:8]} · lineup gen", output)
        self.assertIn("migrated legacy record:", output)
        self.assertIn("Enter launch · q quit: ", output)
        self.assertEqual(self.launches, [])

    def test_resume_enter_uses_the_recorded_lineup(self) -> None:
        self.save_session()
        code, _ = self.run_cli(["-r", FIXED_ID], "\n")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.launches[0].result.session_action.kind, "resume")

    def test_resume_override_refused_with_the_lineup_pointer(self) -> None:
        record = self.save_v4_session()
        rid = record["runtime_session_id"]
        for argv in (["--profile", "quality", "-r", FIXED_ID], ["--profile", "quality", "-c"]):
            with self.subTest(argv=argv):
                code, output = self.run_cli_err(argv, interactive=False)
                self.assertEqual(code, 1)
                self.assertIn(
                    cli.RESUME_PROFILE_OVERRIDE_REFUSAL.format(name="quality", mid=FIXED_ID, rid=rid),
                    output,
                )
        self.assertEqual(self.launches, [])

    def test_resume_override_refusal_is_pinned_verbatim(self) -> None:
        self.assertEqual(
            cli.RESUME_PROFILE_OVERRIDE_REFUSAL,
            "resume always uses the session's lineup; --profile {name} does not apply to "
            "{mid}. To change it: claude-multi lineup --session {rid} profile {name} (live "
            "when compatible; add --relaunch otherwise)",
        )


    def test_resume_same_profile_is_not_an_override(self) -> None:
        self.save_v4_session()
        for argv in (["--profile", "balanced", "-r", FIXED_ID], ["--profile", "balanced", "-c"]):
            with self.subTest(argv=argv):
                code, output = self.run_cli(argv, interactive=False)
                self.assertEqual(code, 0, output)
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(self.launches[0].result.session_action.kind, "resume")

    def test_noninteractive_resume_needs_no_profile(self) -> None:
        # Regression: scripted `-r <uuid>` resumes from the record alone.
        self.save_session()
        code, output = self.run_cli(["-r", FIXED_ID], interactive=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.launches[0].result.session_action.kind, "resume")

    def test_noninteractive_continue_needs_no_profile(self) -> None:
        self.save_session()
        self.runtime.session_store.update_last(self.runtime.cwd, FIXED_ID)
        code, output = self.run_cli(["-c"], interactive=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.launches[0].result.session_action.kind, "resume")

    def test_noninteractive_fresh_still_requires_a_profile(self) -> None:
        code, output = self.run_cli_err([], interactive=False)
        self.assertEqual(code, 2)
        self.assertIn(cli.NONINTERACTIVE_PROFILE_REQUIRED, output)
        self.assertEqual(self.launches, [])

    def test_pinned_session_resumes_its_applied_lineup_not_the_edited_profile(self) -> None:
        self.runtime.profiles.duplicate("balanced", "mine")
        self.save_v4_session("mine", follow=False)
        self.runtime.profiles.update(
            "mine", lambda doc: doc["lead"].update({"model": "opus5", "effort": "ultracode"})
        )
        code, output = self.run_cli(["-r", FIXED_ID], interactive=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.launches[0].lineup.lead.binding.key, "opus55")

    def test_needs_choice_resume_prompts_and_never_launches_on_cancel(self) -> None:
        record = self.save_session()
        record["snapshot"]["lead"]["model"] = "no-such-model"
        _v3.save_any(self.runtime.session_store, record)
        code, output = self.run_cli(["-r", FIXED_ID], "\n")
        self.assertEqual(code, 0, output)
        self.assertIn(f"session {FIXED_ID[:8]} needs a lead choice: its lead no-such-model", output)
        self.assertIn("Nothing was launched.", output)
        self.assertEqual(self.launches, [])

    def test_catalog_moved_selectors_are_healed_at_resume(self) -> None:
        record = self.save_v4_session(follow=False)
        applied = copy.deepcopy(record["applied"])
        applied["agents"]["cm-reviewer"]["selector"] = "old-selector[1m]"
        record = sessions.with_applied(record, applied)
        self.runtime.session_store.save(record)
        code, output = self.run_cli(["-r", FIXED_ID], interactive=False)
        self.assertEqual(code, 0, output)
        prepared = self.launches[0]
        self.assertNotEqual(
            prepared.record["applied"]["agents"]["cm-reviewer"]["selector"], "old-selector[1m]"
        )
        self.assertEqual(prepared.expected_applied_hash, record["applied_hash"])
        self.assertEqual(prepared.expected_lineup_generation, 1)

    def test_deleted_followed_profile_is_informational_and_resumes(self) -> None:
        self.runtime.profiles.duplicate("balanced", "ghost")
        self.save_v4_session("ghost")
        self.runtime.profiles.delete("ghost")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code, output = self.run_cli(["-r", FIXED_ID], interactive=False)
        self.assertEqual(code, 0, output)
        self.assertIn("profile 'ghost' no longer exists", stderr.getvalue())
        self.assertFalse(self.launches[0].record["follow"])

    def test_unresolvable_recorded_lead_fails_closed_with_n1(self) -> None:
        record = self.save_session()
        record["snapshot"]["lead"]["model"] = "no-such-model"
        _v3.save_any(self.runtime.session_store, record)
        code, output = self.run_cli_err(["-r", FIXED_ID], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn(f"session {FIXED_ID} needs a profile or model choice (lead no-such-model", output)
        self.assertEqual(self.launches, [])

    def test_invalid_resume_uuid_rejected(self) -> None:
        code, output = self.run_cli_err(["-r", "not-a-uuid"], "")
        self.assertEqual(code, 1)
        self.assertIn("no managed session matches 'not-a-uuid'", output)
        self.assertIn("claude-multi sessions list", output)

    def test_resume_prepare_preserves_existing_fork_lineage(self) -> None:
        self.save_session(forked_from=OTHER_ID)
        prepared = self.runtime.prepare(
            cli.LaunchTarget("record", None, None, False, "Session"),
            action="resume",
            passthrough=[],
            session_id=FIXED_ID,
        )
        self.assertEqual(prepared.record["managed_id"], FIXED_ID)
        self.assertEqual(prepared.record["forked_from"], OTHER_ID)

    def test_fork_prepare_fails_with_adoption_guidance(self) -> None:
        self.save_session()
        with self.assertRaisesRegex(cli.CLIError, "sessions link") as raised:
            self.runtime.prepare(
                cli.LaunchTarget("record", None, None, False, "Session"),
                action="fork",
                passthrough=[],
                session_id=FIXED_ID,
            )
        self.assertIn("fork natively", str(raised.exception))
        self.assertEqual(self.launches, [])


class CompositionCommandTests(CLITestCase):
    """`compose …` and `show` are aliases of `profile …` (2.x spellings)."""

    def _alias(self, argv, text="", **kwargs):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code, output = self.run_cli(argv, text, **kwargs)
        return code, output, stderr.getvalue()

    def test_duplicate_rename_delete_restore_and_template(self) -> None:
        profiles = self.runtime.profiles
        code, output, note = self._alias(["compose", "duplicate", "balanced", "copy"])
        self.assertEqual(code, 0, output)
        self.assertIn(
            "claude-multi: 'compose duplicate' is the earlier spelling of 'profile duplicate'; "
            "the alias stays accepted",
            note,
        )
        self.assertTrue(profiles.has_user("copy"))
        self.assertEqual(self._alias(["compose", "rename", "copy", "renamed"])[0], 0)
        self.assertFalse(profiles.has_user("copy"))
        self.assertTrue(profiles.has_user("renamed"))
        self.assertEqual(
            self._alias(["compose", "use-as-template", "renamed", "template"])[0], 0
        )
        self.assertEqual(profiles.load("template")["name"], "template")
        code, output, note = self._alias(["compose", "delete", "renamed"])
        self.assertEqual(code, 0, output)
        self.assertIn("'profile rm'", note)
        self.assertFalse(profiles.has_user("renamed"))
        # restore-default = profile reseed balanced: the overwrite note,
        # an interactive confirm, then the shipped seed bytes.
        profiles.update("balanced", lambda doc: doc.update({"description": "edited"}))
        code, output, note = self._alias(["compose", "restore-default"], "n\n")
        self.assertEqual(code, 0, output)
        self.assertIn("Nothing was changed.", output)
        self.assertEqual(profiles.load("balanced")["description"], "edited")
        self.assertIn(cli.COMPOSE_RESTORE_DEFAULT_NOTE, note)
        code, output, _ = self._alias(["compose", "restore-default"], "y\n")
        self.assertEqual(code, 0, output)
        self.assertIn("overwrite the balanced seed with its shipped version? [y/N] ", output)
        self.assertNotEqual(profiles.load("balanced")["description"], "edited")
        # Non-interactive: 2.x parity, proceeds without a prompt.
        profiles.update("balanced", lambda doc: doc.update({"description": "edited"}))
        code, output, _ = self._alias(["compose", "restore-default"], interactive=False)
        self.assertEqual(code, 0, output)
        self.assertNotIn("[y/N]", output)
        self.assertNotEqual(profiles.load("balanced")["description"], "edited")

    def test_names_and_paths_are_validated(self) -> None:
        code, _output, output = self._alias(["compose", "duplicate", "balanced", "../bad"])
        self.assertEqual(code, 1)
        self.assertIn("Not a profile name", output)

    def test_a_2x_composition_name_is_refused_with_r20(self) -> None:
        code, _output, output = self._alias(["compose", "show", "default"])
        self.assertEqual(code, 1)
        self.assertIn(cli.PROFILE_IS_COMPOSITION.format(name="default"), output)

    def test_show_and_models_are_concise(self) -> None:
        code, output = self.run_cli(["models"])
        self.assertEqual(code, 0)
        self.assertIn("fable\tFable", output)
        code, output, note = self._alias(["show", "balanced"])
        self.assertEqual(code, 0, output)
        self.assertIn("'show' is the earlier spelling of 'profile show'", note)
        self.assertTrue(output.startswith("claude-multi · profile balanced · lead "), output)
        self.assertNotIn("lineup gen", output)
        self.assertNotIn("/cm profile", output)
        code, bare, _ = self._alias(["show"])
        self.assertEqual((code, bare), (0, output))

    def test_compose_list_marks_seed_and_user(self) -> None:
        self.runtime.profiles.duplicate("balanced", "copy")
        code, output, _ = self._alias(["compose", "list"])
        self.assertEqual(code, 0)
        self.assertIn("balanced\tseed\t", output)
        self.assertIn("copy\tyours\t", output)

    def test_duplicate_and_rename_reject_every_visible_target(self) -> None:
        self.runtime.profiles.duplicate("balanced", "source")
        self.runtime.profiles.duplicate("balanced", "taken")
        taken_path = self.runtime.profiles._path("taken")
        taken_before = taken_path.read_bytes()
        for command in (
            ["compose", "duplicate", "source", "balanced"],
            ["compose", "duplicate", "source", "taken"],
            ["compose", "rename", "source", "balanced"],
            ["compose", "rename", "source", "taken"],
        ):
            with self.subTest(command=command):
                code, _output, output = self._alias(command)
                self.assertEqual(code, 1)
                self.assertIn("already exists", output)
                self.assertTrue(self.runtime.profiles.has_user("source"))
                self.assertEqual(taken_path.read_bytes(), taken_before)


class CompositionReadSafetyTests(CLITestCase):
    # The files are written with _write_composition (the store's
    # writers are deleted and it no longer creates compositions/). The
    # source document is _v3.default_composition() (the seedless store's
    # load("default") raises).
    def test_ordinary_private_file_loads(self) -> None:
        _write_composition(self.runtime, {**_v3.default_composition(), "name": "ordinary"})
        self.assertEqual(self.runtime.compositions.load("ordinary")["name"], "ordinary")

    def test_external_symlink_is_rejected_without_following(self) -> None:
        document = _v3.default_composition()
        document["name"] = "external"
        external = self.root / "external.json"
        external.write_bytes(strict_json.canonical_file_bytes(document))
        os.chmod(external, 0o600)
        state.ensure_private_dir(self.runtime.compositions.compositions_dir)
        self.runtime.compositions._path("external").symlink_to(external)
        with self.assertRaisesRegex(
            cli.CLIError, "cannot load composition 'external'.*symlink"
        ):
            self.runtime.compositions.load("external")
        self.assertEqual(
            external.read_bytes(), strict_json.canonical_file_bytes(document)
        )

    def test_an_unsafe_compositions_directory_is_refused_on_read(self) -> None:
        # The seedless store no longer runs ensure_private_dir
        # at construction, so reads validate compositions/ instead: a
        # symlinked or group/other-accessible directory yields no names, no
        # legacy-name membership and a refused load, and nothing is created.
        store = self.runtime.compositions
        document = {**_v3.default_composition(), "name": "evil"}
        foreign = self.root / "foreign-compositions"
        foreign.mkdir()
        os.chmod(foreign, 0o777)
        evil = foreign / "evil.json"
        evil.write_bytes(strict_json.canonical_file_bytes(document))
        os.chmod(evil, 0o600)
        state.ensure_private_dir(store.root)
        self.assertFalse(os.path.lexists(store.compositions_dir))
        store.compositions_dir.symlink_to(foreign)
        with self.assertRaisesRegex(OSError, "is a symlink"):
            state.ensure_private_dir(store.compositions_dir)
        self.assertEqual(store.names(), [])
        self.assertFalse(store.has_user("evil"))
        self.assertFalse(store.contains("evil"))
        with self.assertRaisesRegex(
            cli.CLIError, "cannot load composition 'evil'.*is a symlink"
        ):
            store.load("evil")
        store.compositions_dir.unlink()
        # A real but group/other-accessible directory is refused the same way.
        store.compositions_dir.mkdir(mode=0o700)
        (store.compositions_dir / "evil.json").write_bytes(evil.read_bytes())
        os.chmod(store.compositions_dir / "evil.json", 0o600)
        os.chmod(store.compositions_dir, 0o770)
        self.assertEqual(store.names(), [])
        self.assertFalse(store.contains("evil"))
        with self.assertRaisesRegex(
            cli.CLIError, "cannot load composition 'evil'.*group/other"
        ):
            store.load("evil")
        # Tightened to 0700 it reads normally, so the checks are the gate.
        os.chmod(store.compositions_dir, 0o700)
        self.assertEqual(store.names(), ["evil"])
        self.assertTrue(store.contains("evil"))
        self.assertEqual(store.load("evil")["name"], "evil")

    def test_in_tree_symlink_is_rejected_without_following(self) -> None:
        source = _write_composition(self.runtime, {**_v3.default_composition(), "name": "source"})
        before = source.read_bytes()
        self.runtime.compositions._path("alias").symlink_to(source.name)
        with self.assertRaisesRegex(
            cli.CLIError, "cannot load composition 'alias'.*symlink"
        ):
            self.runtime.compositions.load("alias")
        self.assertEqual(source.read_bytes(), before)


class SessionCommandTests(CLITestCase):
    def test_link_list_show_forget_without_private_scrape(self) -> None:
        import builtins

        project_dir = (
            Path(self.runtime.environ["HOME"])
            / ".claude"
            / "projects"
            / cli._native_project_slug(self.runtime.cwd)
        )
        project_dir.mkdir(parents=True)
        transcript = project_dir / f"{FIXED_ID}.jsonl"
        transcript.write_bytes(b'{"poison":"must-not-be-read"}\n')
        real_builtin_open = builtins.open
        real_io_open = io.open

        def guarded_open(original):
            def wrapper(file, mode="r", *args, **kwargs):
                try:
                    target = Path(file)
                except TypeError:
                    target = None
                if target == transcript and any(flag in mode for flag in ("r", "+")):
                    raise AssertionError("transcript body was opened")
                return original(file, mode, *args, **kwargs)

            return wrapper

        with mock.patch("builtins.open", side_effect=guarded_open(real_builtin_open)), mock.patch(
            "io.open", side_effect=guarded_open(real_io_open)
        ):
            code, output = self.run_cli(
                ["sessions", "link", FIXED_ID, "--profile", "balanced"]
            )
            self.assertEqual(code, 0, output)
            self.assertIn("Linked", output)
            record = self.runtime.session_store.resolve(FIXED_ID)
            stable_id = record["managed_id"]
            # A v4 record at lineup generation 0, following.
            self.assertEqual(
                (record["version"], record["lineup_generation"], record["profile"], record["follow"]),
                (sessions.RECORD_VERSION, 0, "balanced", True),
            )
            self.assertNotIn("launch_fence", record)
            self.assertIsNone(record["applied"]["lead"]["compaction"]["window"])
            self.assertEqual(self.run_cli(["sessions", "list"])[0], 0)
            code, output = self.run_cli(["sessions", "show", FIXED_ID])
            self.assertEqual(code, 0)
            self.assertIn('"runtime_session_id":"' + FIXED_ID, output)
            self.assertIn('"managed_id":', output)
            payload = strict_json.canonical_bytes(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": FIXED_ID,
                    "source": "resume",
                    "cwd": self.runtime.cwd,
                    "model": "claude-fable-5[1m]",
                    "transcript_path": str(transcript),
                }
            ).decode("utf-8")
            self.assertEqual(
                self.run_cli(
                    ["session-event", "start", "--managed-id", stable_id], payload
                )[0],
                0,
            )
            code, output = self.run_cli(["sessions", "forget", FIXED_ID, "--yes"])
            self.assertEqual(code, 0)
            self.assertIn("Forgot", output)

    def test_link_updates_pointer_for_adopted_original_cwd(self) -> None:
        other = self.root / "original-project"
        other.mkdir()
        with mock.patch('claude_multi.cli.session_facts._original_cwd_for_adopt', return_value=str(other)
        ), contextlib.redirect_stderr(io.StringIO()) as note:
            code, output = self.run_cli(
                ["sessions", "link", FIXED_ID, "--composition", "balanced"]
            )
        self.assertIn("'--composition' is the earlier spelling of '--profile'", note.getvalue())
        self.assertEqual(code, 0, output)
        record = self.runtime.session_store.resolve(FIXED_ID)
        self.assertEqual(record["cwd"], str(other))
        self.assertEqual(
            self.runtime.session_store.last(str(other)), record["managed_id"]
        )
        self.assertIsNone(self.runtime.session_store.last(self.runtime.cwd))

    def test_link_can_adopt_native_session_as_ordinary_gateway(self) -> None:
        with mock.patch('claude_multi.cli.session_facts._original_cwd_for_adopt', return_value=self.runtime.cwd
        ), contextlib.redirect_stderr(io.StringIO()) as note:
            code, output = self.run_cli(
                ["sessions", "link", FIXED_ID, "--model", "qwen38"]
            )
        self.assertEqual(code, 0, output)
        self.assertIn("'--model' is the earlier spelling of '--direct'", note.getvalue())
        self.assertIn("as an ad-hoc direct session on 'qwen38'", output)
        record = self.runtime.session_store.resolve(FIXED_ID)
        # `--direct M` = ad_hoc_direct(M), profile null, pinned, gen 0.
        self.assertEqual(record["version"], sessions.RECORD_VERSION)
        self.assertEqual((record["profile"], record["follow"]), (None, False))
        self.assertEqual(record["applied"]["lead"]["key"], "qwen38")
        self.assertEqual(record["applied"]["agents"], {})
        self.assertEqual(record["lineup_generation"], 0)
        # One pointer per cwd for every session kind.
        self.assertEqual(
            self.runtime.session_store.last(self.runtime.cwd),
            record["managed_id"],
        )
        # The first launcher resume compiles the 3.0 scope (gen 0 -> 1).
        prepared = self.runtime.prepare(
            cli.LaunchTarget("record", None, None, False, "Session"),
            action="resume",
            passthrough=[],
            session_id=record["managed_id"],
        )
        self.assertEqual(prepared.record["version"], sessions.RECORD_VERSION)
        self.assertEqual(prepared.record["lineup_generation"], 1)
        self.assertEqual(prepared.record["applied"]["lead"]["key"], "qwen38")
        self.assertEqual(prepared.expected_lineup_generation, 0)
        # 3.0 restates the lead at every resume (the compile's --model).
        argv = prepared.result.argv
        self.assertEqual(argv[argv.index("--model") + 1], "claude-multi-qwen38-max[1m]")

    def test_relink_runtime_repairs_existing_record_without_moving_scope_id(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        runtime_id = "22222222-2222-4222-8222-222222222222"
        repaired_cwd = self.root / "repaired-project"
        repaired_cwd.mkdir()
        code, output = self.run_cli(
            [
                "sessions",
                "relink-runtime",
                FIXED_ID,
                runtime_id,
                "--cwd",
                str(repaired_cwd),
            ]
        )
        self.assertEqual(code, 0, output)
        self.assertIn(f"managed {FIXED_ID}", output)
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(record["managed_id"], FIXED_ID)
        self.assertEqual(record["runtime_session_id"], runtime_id)
        self.assertEqual(record["cwd"], str(repaired_cwd))
        self.assertEqual(record["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertEqual(record["launch_epoch"], 1)
        self.assertRegex(record["mutation_token"], sessions.UUID4)
        self.assertIsNone(self.runtime.session_store.last(self.runtime.cwd))
        self.assertEqual(
            self.runtime.session_store.last(str(repaired_cwd)), FIXED_ID
        )

    def test_relink_runtime_ownership_failure_does_not_partially_commit_cwd(self) -> None:
        target = self.save_session(mode="durable", scope_generation=1)
        target["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        target["observed_cwd"] = "/wrong/project"
        _v3.save_any(self.runtime.session_store, target)
        owner_id = "33333333-3333-4333-8333-333333333333"
        owner = _v3.make_record(
            managed_id=owner_id,
            runtime_session_id=OTHER_ID,
            cwd=self.runtime.cwd,
            composition_name=target["composition_name"],
            snapshot=target["snapshot"],
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
            mode="durable",
            scope_generation=1,
            identity_state=sessions.IDENTITY_AUTHORITATIVE,
        )
        _v3.save_any(self.runtime.session_store, owner)
        before = self.runtime.session_store.read_record_bytes(FIXED_ID)
        repaired_cwd = self.root / "other-project"
        repaired_cwd.mkdir()
        code, output = self.run_cli_err(
            [
                "sessions",
                "relink-runtime",
                FIXED_ID,
                OTHER_ID,
                "--cwd",
                str(repaired_cwd),
            ]
        )
        self.assertEqual(code, 1, output)
        self.assertIn("already owned", output)
        self.assertEqual(self.runtime.session_store.read_record_bytes(FIXED_ID), before)

    def test_relink_runtime_refuses_while_migrate_or_restore_holds_the_lock(self) -> None:
        # relink-runtime is a launcher writer; it refuses before writing.
        self.save_session(mode="durable", scope_generation=1)
        before = self.runtime.session_store.read_record_bytes(FIXED_ID)
        exclusive = sessions.migration_lock(self.runtime.session_store.root)
        self.assertTrue(exclusive.acquire(blocking=False))
        try:
            code, output = self.run_cli_err(
                ["sessions", "relink-runtime", FIXED_ID, OTHER_ID]
            )
        finally:
            exclusive.release()
        self.assertEqual(code, 1, output)
        self.assertIn(sessions.MIGRATION_BUSY_TEXT, output)
        self.assertNotIn("Reconciled", output)
        self.assertEqual(self.runtime.session_store.read_record_bytes(FIXED_ID), before)

    def test_noninteractive_link_requires_a_target(self) -> None:
        code, output = self.run_cli_err(["sessions", "link", FIXED_ID], interactive=False)
        self.assertEqual(code, 2)
        self.assertIn(
            "sessions link needs --profile NAME or --direct MODEL when not interactive", output
        )
        code, output = self.run_cli(["sessions", "link"], interactive=False)
        self.assertEqual(code, 0)
        self.assertIn(
            "adopt a native fork: claude-multi sessions link <fork-uuid> --profile NAME "
            "| --direct MODEL [--cwd DIR]",
            output,
        )

    def test_link_strips_the_parents_fork_and_bumps_its_epoch_in_its_version(self) -> None:
        parent = self.save_session(session_id=OTHER_ID)
        parent["pending_forks"] = [{"session_id": FIXED_ID, "observed_at": "2026-07-21T00:00:00Z"}]
        parent["identity_state"] = sessions.IDENTITY_PENDING_FORK
        _v3.save_any(self.runtime.session_store, parent)
        with mock.patch('claude_multi.cli.session_facts._original_cwd_for_adopt', return_value=self.runtime.cwd):
            code, output = self.run_cli(["sessions", "link", FIXED_ID, "--direct", "sol"])
        self.assertEqual(code, 0, output)
        raw, _ = self.runtime.session_store.load_raw(OTHER_ID)
        self.assertEqual(raw["version"], 3)
        self.assertEqual(raw["pending_forks"], [])
        self.assertEqual(raw["launch_epoch"], parent.get("launch_epoch", 0) + 1)

    def test_doctor_uses_injected_local_check(self) -> None:
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0)
        self.assertIn("Ready", output.splitlines())  # the verdict line
        self.assertEqual(self.launches, [])

    def test_doctor_reports_binary_and_daemon_info(self) -> None:
        code, output = self.run_cli(["doctor", "-v"])
        self.assertEqual(code, 0)
        self.assertIn("Managed Claude 2.1.217 verified (fixture).", output)
        self.assertIn("Shared daemon: fixture daemon absent.", output)

    def test_doctor_blocked_when_binary_verification_fails(self) -> None:
        self.runtime.doctor_binary_callback = lambda _contract: (
            ["managed Claude binary: inspected Claude artifact /x is missing; "
             "re-run the native-contract inspection against the installed version"],
            [],
        )
        code, output = self.run_cli(["doctor"], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", output)
        self.assertNotIn("Ready", output.splitlines())  # the verdict line
        self.assertIn("managed Claude binary", output)
        self.assertIn("re-run the native-contract inspection", output)

    def test_doctor_binary_parity_uses_launch_resolver_by_default(self) -> None:
        runtime = cli.Runtime(
            asset_root=CATALOG_ROOT,
            environ=self.runtime.environ,
            cwd=self.root / "project",
            **self._runtime_seams(),
        )
        self.assertIs(runtime.doctor_binary_callback.func, claude_multi.launch.doctor_binary_report)
        self.assertEqual(runtime.doctor_binary_callback.keywords, {
            "environ": runtime.environ,
            "retained_root": runtime.home / ".local/share/claude-multi/pinned-clients",
        })
        self.assertIs(runtime.doctor_daemon_callback, claude_multi.launch.inspect_shared_daemon)


if __name__ == "__main__":
    unittest.main()


class SecretReadinessSurfaceTests(CLITestCase):
    def _without_secret(self):
        self.secret_file.unlink()

    def _kimi_profile(self):
        # The balanced seed binds only OAuth-pool lines; a Kimi lead
        # needs the env-file secret.
        document = copy.deepcopy(self.runtime.profiles.load("balanced"))
        document["name"] = "kimi-lead"
        document["lead"] = {"model": "kimi-k3", "effort": "max"}
        self.runtime.profiles.save(document)
        return ["--profile", "kimi-lead"]

    def test_quick_confirm_blocked_when_selected_secret_missing(self) -> None:
        argv = self._kimi_profile()
        self._without_secret()
        # The line confirm prints `blocked:` lines and never launches.
        code, output = self.run_cli(argv, text="\n")
        self.assertEqual(code, 0)
        self.assertIn("blocked: ", output)
        self.assertIn("Kimi (kimi)", output)
        self.assertIn("env:KIMI_CLAUDE_API_KEY", output)
        self.assertIn("q quit: ", output)
        self.assertEqual(self.launches, [])

    def test_noninteractive_launch_refuses_on_a_missing_secret(self) -> None:
        argv = self._kimi_profile()
        self._without_secret()
        code, output = self.run_cli_err(argv, interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("profile is blocked:", output)
        self.assertIn("env:KIMI_CLAUDE_API_KEY", output)
        self.assertEqual(self.launches, [])

    def test_quick_confirm_ready_when_secret_present(self) -> None:
        code, output = self.run_cli([], text="\n")
        self.assertNotIn("env:KIMI_CLAUDE_API_KEY unavailable", output)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.launches), 1)

    def test_doctor_reports_affected_profiles(self) -> None:
        # per-profile secret readiness is Attention (the launch
        # path blocks); doctor no longer reads 2.x compositions.
        self._kimi_profile()
        self._without_secret()
        code, output = self.run_cli(["doctor"], interactive=False)
        self.assertEqual(code, 0)
        self.assertIn("Attention", output)
        self.assertIn("profile kimi-lead: provider kimi has no credential (set KIMI_CLAUDE_API_KEY", output)
        self.assertNotIn("profile balanced: provider", output)

    def test_doctor_ready_when_secret_present(self) -> None:
        code, output = self.run_cli(["doctor"], interactive=False)
        self.assertEqual(code, 0)
        self.assertIn("Ready", output.splitlines())  # the verdict line

    def test_error_line_redacted(self) -> None:
        argv = self._kimi_profile()
        self.secret_file.write_bytes(b"not-an-assignment\n")
        code, output = self.run_cli(argv, text="q\n")
        self.assertEqual(code, 0)
        # The phrase may wrap around the inserted file path; match parts.
        self.assertIn("unsafe", output)
        self.assertIn("malformed", output)
        self.assertIn(str(self.secret_file), output)
        self.assertNotIn("not-an-assignment", output)
        self.assertEqual(self.launches, [])

class DoctorBinaryParityTests(CLITestCase):
    """Real launch resolver callback behind Doctor, temp fixture artifact."""

    def _fixture_runtime(self, sha256: str) -> cli.Runtime:
        from claude_multi import pin, state as state_mod

        platform = pin.host_platform()
        install = pin.owned_path(self.runtime.environ, "2.1.217", platform)
        state_mod.ensure_private_dir(install.parent)
        install.write_bytes(b"#!/bin/fake-claude\n")
        install.chmod(0o755)
        fixture = {"verified": [{
            "version": "2.1.217",
            "platforms": {platform: {"sha256": sha256, "size": install.stat().st_size}},
            "manifest_sha256": "1" * 64, "signature_sha256": None,
            "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE",
            "verified_at": "2026-07-22", "evidence": {platform: "battery", "receipt_sha256": "2" * 64},
        }]}
        runtime = cli.Runtime(
            asset_root=CATALOG_ROOT,
            environ=self.runtime.environ,
            cwd=self.root / "project",
            doctor_callback=lambda _runtime: [],
            doctor_daemon_callback=lambda: claude_multi.launch.DaemonStatus(
                state="absent", summary="fixture daemon absent"
            ),
            **self._runtime_seams(),
        )
        # Real callback (the Runtime default) against the fixture contract.
        self.assertIs(runtime.doctor_binary_callback.func, claude_multi.launch.doctor_binary_report)
        self.assertEqual(runtime.doctor_binary_callback.keywords, {
            "environ": runtime.environ,
            "retained_root": runtime.home / ".local/share/claude-multi/pinned-clients",
        })
        # 3.0 reads agent efforts from the native contract (the lineup
        # catalog): only the claude entry is swapped for the fixture.
        runtime.catalog.docs["native-contract"] = {
            **runtime.catalog.docs["native-contract"], **fixture
        }
        return runtime

    def test_doctor_ready_with_intact_fixture_artifact(self) -> None:
        import hashlib

        sha256 = hashlib.sha256(b"#!/bin/fake-claude\n").hexdigest()
        runtime = self._fixture_runtime(sha256)
        output = io.StringIO()
        code = cli.main(
            ["doctor"],
            runtime=runtime,
            output_stream=output,
            interactive=False,
        )
        self.assertEqual(code, 0)
        # The verdict line: nothing blocks (the pin's age may be an attention item).
        self.assertIn(output.getvalue().splitlines()[0], ("Ready", "Attention"))
        self.assertNotIn("BLOCKED", output.getvalue())
        self.assertIn("Managed Claude 2.1.217 verified", output.getvalue())
        self.assertIn("~/.local/share/claude-multi/claude/2.1.217/", output.getvalue())

    def test_doctor_blocked_with_tampered_fixture_hash(self) -> None:
        runtime = self._fixture_runtime("0" * 64)
        output = io.StringIO()
        code = cli.main(
            ["doctor"],
            runtime=runtime,
            output_stream=output,
            interactive=False,
        )
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", output.getvalue())
        self.assertNotIn("Ready", output.getvalue().splitlines())  # the verdict line
        # Same failure text launch would raise: parity, not a parallel check.
        self.assertIn("does not match the verified sha256", output.getvalue())
        self.assertIn("run `claude-multi setup --step claude` to replace it", output.getvalue())


class BareLaunchStreamTests(unittest.TestCase):
    """_open_tty_streams resolution: prefer /dev/tty, fall back to real TTYs."""

    def test_prefers_dev_tty_with_read_write_handles(self) -> None:
        opened: list[tuple] = []

        def fake_open(path, mode, **kwargs):
            opened.append((path, mode))
            return object() if mode == "r" else object()

        with mock.patch("builtins.open", side_effect=fake_open):
            inp, out = cli._open_tty_streams()
        self.assertEqual(
            opened,
            [("/dev/tty", "r"), ("/dev/tty", "w")],
        )
        self.assertIsNot(inp, out)

    def test_output_handle_failure_closes_input_and_falls_back(self) -> None:
        closed = []

        class Handle:
            def close(self):
                closed.append(True)

        def fake_open(path, mode, **kwargs):
            if mode == "w":
                raise OSError("cannot open for writing")
            return Handle()

        with mock.patch("builtins.open", side_effect=fake_open):
            with mock.patch('claude_multi.cli.streams._stdio_streams_are_ttys', return_value=True):
                inp, out = cli._open_tty_streams()
        self.assertEqual(closed, [True])
        self.assertIs(inp, sys.stdin)

    def test_falls_back_to_stdio_when_both_are_ttys(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("ENXIO")):
            with mock.patch("sys.stdin") as fake_in, mock.patch("sys.stdout") as fake_out:
                fake_in.isatty.return_value = True
                fake_out.isatty.return_value = True
                inp, out = cli._open_tty_streams()
        self.assertIs(inp, fake_in)
        self.assertIs(out, fake_out)

    def test_fails_closed_when_stdout_not_tty(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("ENXIO")):
            with mock.patch("sys.stdin") as fake_in, mock.patch("sys.stdout") as fake_out:
                fake_in.isatty.return_value = True
                fake_out.isatty.return_value = False
                with self.assertRaisesRegex(cli.CLIError, "no interactive terminal"):
                    cli._open_tty_streams()

    def test_fails_closed_when_stdin_not_tty(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("ENXIO")):
            with mock.patch("sys.stdin") as fake_in, mock.patch("sys.stdout") as fake_out:
                fake_in.isatty.return_value = False
                fake_out.isatty.return_value = True
                with self.assertRaisesRegex(cli.CLIError, "no interactive terminal"):
                    cli._open_tty_streams()


class StdioProbeTests(unittest.TestCase):
    def test_none_streams_fail_closed(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("ENXIO")):
            with mock.patch.object(sys, "stdin", None), mock.patch.object(
                sys, "stdout", None
            ):
                with self.assertRaisesRegex(cli.CLIError, "no interactive terminal"):
                    cli._open_tty_streams()

    def test_closed_stream_probe_fails_closed(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("ENXIO")):
            with mock.patch("sys.stdin") as fake_in, mock.patch("sys.stdout") as fake_out:
                fake_in.isatty.side_effect = ValueError("I/O operation on closed file")
                with self.assertRaisesRegex(cli.CLIError, "no interactive terminal"):
                    cli._open_tty_streams()

    def test_probe_oserror_fails_closed(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("ENXIO")):
            with mock.patch("sys.stdin") as fake_in, mock.patch("sys.stdout") as fake_out:
                fake_in.isatty.side_effect = OSError("inappropriate ioctl")
                with self.assertRaisesRegex(cli.CLIError, "no interactive terminal"):
                    cli._open_tty_streams()

    def test_probe_attribute_error_fails_closed(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("ENXIO")):
            with mock.patch.object(sys, "stdin", object()), mock.patch.object(
                sys, "stdout", object()
            ):
                with self.assertRaisesRegex(cli.CLIError, "no interactive terminal"):
                    cli._open_tty_streams()


def _hide_transition_engine(test_case) -> None:
    """Simulate a build without Lane D's module (lazy import must fail closed).

    ``from . import transition`` resolves the parent package attribute first,
    so simulating absence requires removing both the attribute and the
    sys.modules entry.
    """

    import claude_multi

    if hasattr(claude_multi, "transition"):
        saved = claude_multi.transition
        del claude_multi.transition
        test_case.addCleanup(setattr, claude_multi, "transition", saved)
    patcher = mock.patch.dict(sys.modules, {"claude_multi.transition": None})
    test_case.addCleanup(patcher.stop)
    patcher.start()


class QuickConfirmVisibilityTests(CLITestCase):
    """Quick-confirm badges: durability, workflow, policy, project."""

    def test_legacy_record_resume_shows_the_migration_line(self) -> None:
        # A 2.x record migrates at its launcher resume; the line
        # confirm names the migration outcome (the 2.x upgrade note is gone).
        self.save_session()
        code, output = self.run_cli(["-r", FIXED_ID], "q\n")
        self.assertEqual(code, 0)
        self.assertIn("migrated legacy record: 11111111", output)
        self.assertNotIn("predates durable scopes", output)
        self.assertEqual(self.launches, [])

    def test_v4_record_shows_lineup_generation(self) -> None:
        # An unchanged resume launches without a confirm; the planned
        # record's generation shows in --print-launch.
        self.save_v4_session()
        code, output = self.run_cli(["-r", FIXED_ID, "--print-launch"])
        self.assertEqual(code, 0, output)
        self.assertIn("record: profile balanced · follow yes · lineup generation 1", output)
        code, output = self.run_cli(["-r", FIXED_ID], "")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.launches), 1)
        self.assertNotIn("Enter launch", output)

class SessionsScreenTests(CLITestCase):
    """Sessions screen: mode column, per-row actions, forget honesty."""

    def test_list_shows_the_row_columns_actions_and_help(self) -> None:
        # Marker, m8, profile, lead, agents, follow, age, cwd.
        self.save_v4_session(session_id=FIXED_ID)
        self.save_session(session_id=OTHER_ID)
        code, output = self.run_cli(["sessions", "list"], interactive=False)
        self.assertEqual(code, 0)
        v4_row = next(line for line in output.splitlines() if FIXED_ID[:8] in line)
        v3_row = next(line for line in output.splitlines() if OTHER_ID[:8] in line)
        self.assertRegex(
            v4_row, r"^  11111111  balanced +opus55 +agents 8  follow  .*  " + re.escape(self.runtime.cwd)
        )
        self.assertRegex(v3_row, r"^  22222222  cm:default +opus55 +agents \d +-  ")
        self.assertIn(
            "[t] lineup: claude-multi lineup --session <runtime-id> "
            "show|set|unset|profile|pin|follow|direct",
            output,
        )
        self.assertIn("claude-multi sessions link <fork> --profile NAME | --direct MODEL", output)
        self.assertIn("transcripts are never touched", output)

    def test_row_markers_for_pending_needs_choice_and_ended(self) -> None:
        record = self.save_v4_session(session_id=FIXED_ID, last_event_source="end")
        code, output = self.run_cli(["sessions", "list"], interactive=False)
        self.assertRegex(output, r"(?m)^  11111111  balanced")
        pending = {
            "requested_at": "2026-09-25T00:00:00Z", "kind": "profile", "profile": "quality",
            "follow": True, "document": None, "reasons": ["native agents: explore a → b"],
        }
        self.runtime.session_store.save({**record, "pending": pending})
        code, output = self.run_cli(["sessions", "list"], interactive=False)
        self.assertRegex(output, r"(?m)^↻ 11111111  balanced")
        applied = copy.deepcopy(record["applied"])
        applied["lead"]["key"] = "gone-lead"
        self.runtime.session_store.save(
            {**record, "applied": applied, "applied_hash": strict_json.bundle_digest(applied)}
        )
        code, output = self.run_cli(["sessions", "list"], interactive=False)
        self.assertRegex(output, r"(?m)^! 11111111  balanced +gone-lead")

    def test_empty_list_keeps_header_and_help(self) -> None:
        code, output = self.run_cli(["sessions", "list"])
        self.assertEqual(code, 0)
        self.assertIn("(no recorded sessions)", output)
        self.assertIn("[f] forget", output)

    def _write_scope(self, session_id: str) -> None:
        # The 2.x scope compile is deleted; a v3 record's scope is
        # never read back by 3.0 (doctor is catalog-free for v1-3), so any
        # staged plan stands in for it here.
        _write_stand_in_scope(self.runtime, session_id)

    def test_forget_states_exact_deletion_and_removes_scope(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        self._write_scope(FIXED_ID)
        live = scope_mod.scope_dir(self.runtime.session_store.root, FIXED_ID)
        self.assertTrue(live.is_dir())
        code, output = self.run_cli(["sessions", "forget", FIXED_ID, "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("Forgot", output)
        self.assertIn("session record + generated scope", output)
        self.assertIn("Transcripts are never touched", output)
        self.assertFalse(live.exists())
        self.assertFalse(self.runtime.session_store.exists(FIXED_ID))

    def test_forget_without_scope_says_so(self) -> None:
        self.save_session()
        code, output = self.run_cli(["sessions", "forget", FIXED_ID, "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("Forgot", output)
        self.assertIn("no generated scope existed", output)
        self.assertIn("Transcripts are never touched", output)

    def test_forget_not_found_unchanged(self) -> None:
        code, output = self.run_cli_err(["sessions", "forget", FIXED_ID, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("no managed session matches", output)
        self.assertIn("nothing was forgotten", output)


class TransitionCommandTests(CLITestCase):
    """`sessions transition` is the alias of `lineup --relaunch profile N`
    on the relaunch executor (confirm → preflight → prepare → perform, no lock held)."""

    def _transition(self, *extra, name="quality", session=FIXED_ID, **kwargs):
        """``(exit, stdout then stderr)``: a refusal reports on stderr."""

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code, output = self.run_cli(
                ["sessions", "transition", session, "--composition", name, *extra], **kwargs
            )
        return code, output + errors.getvalue()

    def test_transition_rejects_invalid_uuid(self) -> None:
        code, output = self._transition(session="not-a-uuid", text="")
        self.assertEqual(code, 1)
        self.assertIn("not a UUIDv4", output)

    def test_a_2x_composition_name_is_refused_with_r20(self) -> None:
        self.save_v4_session()
        code, output = self._transition(name="default", text="y\n")
        self.assertEqual(code, 1)
        self.assertIn(cli.PROFILE_IS_COMPOSITION.format(name="default"), output)
        self.assertEqual(self.launches, [])

    def test_declined_confirmation_prints_the_diff_and_launches_nothing(self) -> None:
        before = self.save_v4_session()
        code, output = self._transition(text="n\n")
        self.assertEqual(code, 0)
        self.assertIn(f"relaunch session {FIXED_ID[:8]} with profile quality", output)
        self.assertIn("EXITED (not merely idle)", output)
        self.assertIn("Nothing was launched.", output)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.runtime.session_store.load(FIXED_ID), before)

    def test_confirmed_relaunch_prepares_the_target_and_performs(self) -> None:
        before = self.save_v4_session()
        code, output = self._transition(text="y\n")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.launches), 1)
        launched = self.launches[0]
        self.assertEqual(launched.result.session_action.kind, "resume")
        self.assertEqual((launched.record["profile"], launched.record["follow"]), ("quality", True))
        self.assertEqual(launched.record["launch_epoch"], before["launch_epoch"] + 1)
        self.assertEqual(launched.expected_mutation_token, before["mutation_token"])
        self.assertEqual(launched.expected_applied_hash, before["applied_hash"])

    def test_transition_accepts_authoritative_runtime_uuid(self) -> None:
        record = self.save_v4_session()
        self.runtime.session_store.relink_runtime(FIXED_ID, observed_runtime_id=OTHER_ID)
        self._write_transcript(OTHER_ID)
        code, output = self._transition("--its-exited", session=OTHER_ID, interactive=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.launches[0].record["managed_id"], FIXED_ID)
        del record

    def test_its_exited_flag_skips_the_prompt(self) -> None:
        self.save_v4_session()
        code, output = self._transition("--its-exited", interactive=False)
        self.assertEqual(code, 0, output)
        self.assertNotIn("[y/N]", output)
        self.assertEqual(len(self.launches), 1)

    def test_noninteractive_without_flag_records_a_pending_change(self) -> None:
        # The former interim refusal became the lineup pending branch.
        before = self.save_v4_session(write_scope=True)
        code, output = self._transition(interactive=False)
        self.assertEqual(code, 0, output)
        self.assertIn("relaunch needed: requested (--relaunch)", output)
        self.assertIn(f"Recorded as a pending change for session {FIXED_ID[:8]}", output)
        self.assertIn(f"Exit the session, then run: claude-multi -r {FIXED_ID}", output)
        self.assertEqual(self.launches, [])
        after = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(
            after["pending"],
            {
                "requested_at": after["pending"]["requested_at"],
                "kind": "profile",
                "profile": "quality",
                "follow": True,
                "document": None,
                "reasons": ["requested (--relaunch)"],
            },
        )
        self.assertEqual(after["applied_hash"], before["applied_hash"])
        self.assertNotEqual(after["mutation_token"], before["mutation_token"])

    def test_noninteractive_without_flag_refuses_r25_on_a_2x_record(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        before = self.runtime.session_store.read_record_bytes(FIXED_ID)
        code, output = self._transition(interactive=False)
        self.assertEqual(code, 1)
        self.assertIn(
            f"session {FIXED_ID[:8]} is a legacy record (version 3) and cannot hold a pending "
            "change",
            output,
        )
        self.assertEqual(self.runtime.session_store.read_record_bytes(FIXED_ID), before)
        self.assertEqual(self.launches, [])

    def test_from_inside_the_target_session_it_records_a_pending_change(self) -> None:
        self.save_v4_session()
        self.runtime.environ["CLAUDE_MULTI_SESSION_ID"] = FIXED_ID
        code, output = self._transition("--its-exited", interactive=False)
        self.assertEqual(code, 0, output)
        self.assertIn("Recorded as a pending change", output)
        self.assertEqual(self.launches, [])
        self.assertEqual(
            self.runtime.session_store.load(FIXED_ID)["pending"]["profile"], "quality"
        )

    def test_a_2x_record_is_migrated_by_prepare_then_relaunched(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        code, output = self._transition("--its-exited", interactive=False)
        self.assertEqual(code, 0, output)
        self.assertIn("migrated legacy record:", output)
        self.assertTrue(self.runtime.session_store.backup_path(FIXED_ID).is_file())
        self.assertEqual(sessions.check_state_marker(self.runtime.session_store.root), 4)
        self.assertEqual(self.launches[0].record["profile"], "quality")

    def test_a_record_change_during_the_confirm_refuses(self) -> None:
        self.save_v4_session()
        store = self.runtime.session_store

        def confirm_and_race(*_args, **_kwargs):
            record = store.load(FIXED_ID)
            store.save({**record, "mutation_token": sessions.new_mutation_token()})
            return True

        with mock.patch('claude_multi.cli.screens.transition._transition_confirm', side_effect=confirm_and_race):
            code, output = self._transition(text="y\n")
        self.assertEqual(code, 1)
        self.assertIn(
            f"session {FIXED_ID[:8]} changed while the relaunch was being confirmed; run the "
            "command again",
            output,
        )
        self.assertEqual(self.launches, [])

    def test_exec_failure_is_reported_without_a_traceback(self) -> None:
        self.save_v4_session()

        def failing_launch(_prepared):
            raise OSError("simulated execve failure")

        self.runtime.launch_callback = failing_launch
        code, output = self._transition(text="y\n")
        self.assertEqual(code, 1)
        self.assertIn("relaunch exec failed (simulated execve failure)", output)

    def test_transition_unavailable_without_engine(self) -> None:
        self.save_v4_session()
        _hide_transition_engine(self)
        code, output = self._transition(name="balanced", text="")
        self.assertEqual(code, 1)
        self.assertIn("transitions are unavailable", output)
        self.assertIn("claude_multi.transition", output)


class DoctorVisibilityTests(CLITestCase):
    """Doctor additions: scope integrity, prune, collisions, evidence."""

    def _write_scope(self, session_id: str) -> None:
        # The 2.x scope compile is deleted; a v3 record's scope is
        # never read back by 3.0 (doctor is catalog-free for v1-3), so any
        # staged plan stands in for it here.
        _write_stand_in_scope(self.runtime, session_id)

    def test_sessions_scope_collisions_and_evidence_lines(self) -> None:
        self.save_v4_session(write_scope=True)
        code, output = self.run_cli(["doctor", "-v"])
        self.assertEqual(code, 0, output)
        self.assertIn("Sessions: 1 recorded.", output)
        self.assertIn(f"Scope: {FIXED_ID[:8]} OK (lineup generation 1).", output)
        self.assertIn("Collisions: none (0 project agents).", output)
        self.assertIn(
            "Evidence: add-dir carry: backgrounding documented · "
            "takeover binary-consistent, acceptance-pending(U1)",
            output,
        )

    def test_missing_scope_blocks_with_repair_guidance(self) -> None:
        self.save_v4_session()
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", output)
        self.assertIn(
            f"session {FIXED_ID[:8]}: scope unreadable; claude-multi doctor --repair {FIXED_ID}",
            output,
        )

    def test_drifted_scope_reports_record_scope_mismatch(self) -> None:
        self.save_v4_session(write_scope=True)
        tampered = (
            scope_mod.scope_dir(self.runtime.session_store.root, FIXED_ID)
            / ".claude" / "agents" / "cm-reviewer.md"
        )
        state.atomic_write(tampered, b"tampered body\n")
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn(
            f"session {FIXED_ID[:8]}: scope differs from the record-authoritative compile "
            "(.claude/agents/cm-reviewer.md content differs); run claude-multi doctor "
            f"--repair {FIXED_ID}",
            output,
        )

    def test_legacy_sessions_have_no_scope_check(self) -> None:
        self.save_session()
        code, output = self.run_cli(["doctor", "-v"])
        self.assertEqual(code, 0, output)
        self.assertIn("Sessions: 1 recorded · 1 legacy record (claude-multi migrate).", output)
        self.assertIn("legacy record · lead opus55 (not checked until migrated)", output)
        self.assertNotIn("Scope:", output)

    def test_corrupt_session_record_blocks_doctor_and_is_counted(self) -> None:
        path = self.runtime.session_store.sessions_dir / f"{OTHER_ID}.json"
        state.atomic_write(path, b"{not-json\n")
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("Sessions: 1 recorded · 1 unreadable.", output)
        self.assertIn(f"session record {OTHER_ID} is unreadable", output)
        self.assertIn("corrupt session record", output)

    def test_prune_removes_staging_and_forgotten_scopes_only(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        self._write_scope(FIXED_ID)
        self._write_scope(OTHER_ID)  # no record: forgotten
        scopes_root = self.runtime.session_store.root / "scopes"
        staging_prev = scopes_root / f".{FIXED_ID}.prev"
        state.ensure_private_dir(staging_prev)
        state.atomic_write(staging_prev / "settings.json", b"{}\n")
        staging_new = scopes_root / f".{OTHER_ID}.new"
        state.ensure_private_dir(staging_new)
        staging_forgotten_prev = scopes_root / f".{OTHER_ID}.prev"
        state.ensure_private_dir(staging_forgotten_prev)
        code, output = self.run_cli(["doctor", "--prune"])
        self.assertEqual(code, 0, output)
        self.assertIn("Pruned:", output)
        self.assertNotIn(f"stale staging dir scopes/.{FIXED_ID}.prev", output)
        self.assertIn(f"stale staging dir scopes/.{OTHER_ID}.new", output)
        self.assertIn(f"stale staging dir scopes/.{OTHER_ID}.prev", output)
        self.assertIn(f"scope for forgotten session {OTHER_ID}", output)
        # A scope and transition rollback directory with a living record are
        # never touched; both may be needed between transition commit and exec.
        self.assertTrue(
            scope_mod.scope_dir(self.runtime.session_store.root, FIXED_ID).is_dir()
        )
        self.assertTrue(staging_prev.is_dir())
        self.assertTrue(self.runtime.session_store.exists(FIXED_ID))

    def test_prune_without_stale_scopes_is_a_noop(self) -> None:
        code, output = self.run_cli(["doctor", "--prune"])
        self.assertEqual(code, 0)
        self.assertIn("nothing stale", output)

    def test_prune_collects_orphaned_digest_only_lead_prompts(self) -> None:
        # Pre-2.2 lead prompts (lead-prompt-<digest>.md, no session suffix)
        # are orphans by construction; session-suffixed prompts with a living
        # record are never touched.
        self.save_session(mode="durable", scope_generation=1)
        root = self.runtime.session_store.root
        orphan = root / "lead-prompt-abcdef0123456789.md"
        state.atomic_write(orphan, b"orphaned prompt\n")
        from claude_multi import compiler as compiler_mod

        living = compiler_mod.lead_prompt_path(
            root, "sha256:" + "a" * 64, FIXED_ID
        )
        state.atomic_write(living, b"living prompt\n")
        code, output = self.run_cli(["doctor", "--prune"])
        self.assertEqual(code, 0, output)
        self.assertIn(f"orphaned pre-2.2 lead prompt {orphan.name}", output)
        self.assertFalse(orphan.exists())
        self.assertTrue(living.exists())

    def test_prune_rejects_symlinked_scopes_root_without_following(self) -> None:
        scopes_root = self.runtime.session_store.root / "scopes"
        if scopes_root.exists():
            shutil.rmtree(scopes_root)
        outside = self.root / "outside-scopes"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        scopes_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(state.StateError, "is a symlink"):
            cli._doctor_prune(self.runtime, io.StringIO())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_collision_scan_blocks_with_fail_closed_list(self) -> None:
        agents = Path(self.runtime.cwd) / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "cm-reviewer.md").write_bytes(b"---\nname: cm-reviewer\n---\nbody\n")
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("Collisions: 1 blocking (1 project agent).", output)
        self.assertIn("collides with the managed cm-* namespace", output)

    def test_repair_recompiles_missing_scope_from_record(self) -> None:
        self.save_v4_session()
        live = scope_mod.scope_dir(self.runtime.session_store.root, FIXED_ID)
        self.assertFalse(live.exists())
        code, output = self.run_cli(["doctor", "--repair", FIXED_ID])
        self.assertEqual(code, 0, output)
        self.assertIn("record lineup generation 1 is authoritative", output)
        self.assertIn("live scope was missing; recompiled", output)
        self.assertTrue(live.is_dir())
        code, output = self.run_cli(["doctor", "-v"])
        self.assertEqual(code, 0, output)
        self.assertIn(f"Scope: {FIXED_ID[:8]} OK", output)

    def test_repair_keeps_the_record_epoch_in_hooks_and_the_launch_epoch_in_env(self) -> None:
        # Hooks carry the record's (bumped) epoch — the fork
        # revocation — while the launch-time env keeps the epoch the launch
        # committed, so the rebuilt launch_fence still matches.
        record = self.save_v4_session(
            target=cli.LaunchTarget("ad-hoc", claude_multi.profile.ad_hoc_direct("qwen38"), None, False, "Direct")
        )
        self.runtime.session_store.save({**record, "launch_epoch": 7})
        code, output = self.run_cli(["doctor", "--repair", FIXED_ID])
        self.assertEqual(code, 0, output)
        self.assertNotIn("need a relaunch", output)
        live = scope_mod.scope_dir(self.runtime.session_store.root, FIXED_ID)
        settings = strict_json.load(live / "settings.json")
        self.assertEqual(settings["env"]["CLAUDE_MULTI_LAUNCH_EPOCH"], "1")
        start = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        end = settings["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
        self.assertIn("--launch-epoch 7", start)
        self.assertIn("--launch-epoch 7", end)

    def test_repair_converges_drifted_scope(self) -> None:
        self.save_v4_session(write_scope=True)
        tampered = (
            scope_mod.scope_dir(self.runtime.session_store.root, FIXED_ID)
            / ".claude" / "agents" / "cm-reviewer.md"
        )
        state.atomic_write(tampered, b"tampered body\n")
        code, output = self.run_cli(["doctor", "--repair", FIXED_ID])
        self.assertEqual(code, 0, output)
        self.assertIn("live scope drifted from record authority", output)
        code, output = self.run_cli(["doctor", "-v"])
        self.assertEqual(code, 0, output)
        self.assertIn(f"Scope: {FIXED_ID[:8]} OK", output)

    def test_repair_legacy_record_is_actionable(self) -> None:
        self.save_session()
        code, output = self.run_cli(["doctor", "--repair", FIXED_ID])
        self.assertEqual(code, 0)
        self.assertIn(
            f"session {FIXED_ID[:8]} is a legacy record; run claude-multi migrate (or resume it) first",
            output,
        )

    def test_repair_unavailable_without_engine(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        _hide_transition_engine(self)
        code, output = self.run_cli_err(["doctor", "--repair", FIXED_ID])
        self.assertEqual(code, 1)
        self.assertIn("transitions are unavailable", output)

    def test_repair_all_converges_ended_scopes(self) -> None:
        self.save_v4_session(write_scope=True, last_event_source="end")
        stale = scope_mod.scope_dir(self.runtime.session_store.root, FIXED_ID) / "lineup.md"
        state.atomic_write(stale, b"stale\n")
        code, output = self.run_cli(["doctor", "--repair-all"])
        self.assertEqual(code, 0, output)
        self.assertIn("Repair-all: 1 session(s) converged to record authority.", output)
        self.assertIn("live scope drifted from record authority", output)
        code, output = self.run_cli(["doctor", "-v"])
        self.assertEqual(code, 0, output)
        self.assertNotIn("BLOCKED", output)

    def test_repair_all_skips_legacy_and_reports_unreadable(self) -> None:
        self.save_session()
        path = self.runtime.session_store.sessions_dir / f"{OTHER_ID}.json"
        state.atomic_write(path, b"{not-json\n")
        code, output = self.run_cli(["doctor", "--repair-all"])
        self.assertEqual(code, 1)
        self.assertIn(
            f"skipped session {FIXED_ID[:8]} is a legacy record; run claude-multi migrate", output
        )
        self.assertIn("FAILED", output)
        self.assertIn("unreadable", output)

    def test_repair_all_continues_past_a_broken_record(self) -> None:
        # One record whose scope cannot be touched safely (a symlinked live
        # scope) must not abort the pass: it is a failure (exit 1) while every
        # other session is still repaired.
        self.save_v4_session(write_scope=True, last_event_source="end")
        live = scope_mod.scope_dir(self.runtime.session_store.root, FIXED_ID)
        moved = self.root / "moved-scope"
        os.rename(live, moved)
        os.symlink(moved, live)
        self.save_v4_session(session_id=OTHER_ID, last_event_source="end")
        code, output = self.run_cli(["doctor", "--repair-all"])
        self.assertEqual(code, 1)
        self.assertIn(f"FAILED {FIXED_ID}", output)
        self.assertTrue(scope_mod.scope_dir(self.runtime.session_store.root, OTHER_ID).is_dir())
        self.assertIn("Repair-all: 1 session(s) converged", output)

    def test_repair_all_handles_direct_records(self) -> None:
        record = self.save_v4_session(
            target=cli.LaunchTarget("ad-hoc", claude_multi.profile.ad_hoc_direct("qwen38"), None, False, "Direct"),
            last_event_source="end",
        )
        live = scope_mod.scope_dir(self.runtime.session_store.root, FIXED_ID)
        self.assertFalse(live.exists())
        code, output = self.run_cli(["doctor", "--repair-all"])
        self.assertEqual(code, 0, output)
        settings = strict_json.load(live / "settings.json")
        self.assertEqual(settings["model"], record["applied"]["lead"]["selector"])
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)


class EditorWorkflowsTests(unittest.TestCase):
    """Workflows row in the composition editor with the `?` panel."""

    def test_guarantee_panel_modes(self) -> None:
        native = workflow_guarantee_panel("native")
        self.assertIn("Native workflows (ultracode): ON", native)
        self.assertNotIn("disableWorkflows:true", native)
        off = workflow_guarantee_panel("off")
        self.assertIn("Native workflows (ultracode): OFF", off)
        self.assertIn("disableWorkflows:true compiled", off)
        self.assertIn('presents off as "safer subagents"', off)


class DurableDefaultWiringTests(CLITestCase):
    """Lead-integrator tests: durable-by-default launch selection + --legacy."""

    def _prepared(self, argv, text="\n", interactive=True):
        code, _out = self.run_cli(argv, text, interactive=interactive)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.launches), 1)
        return self.launches[0]

    def test_fresh_launch_is_durable(self) -> None:
        prepared = self._prepared(["--profile", "balanced"])
        self.assertTrue(prepared.result.durable)
        argv = prepared.result.argv
        self.assertIn("--add-dir", argv)
        self.assertNotIn("--agents", argv)
        self.assertNotIn("--disallowedTools", argv)
        settings_path = argv[argv.index("--settings") + 1]
        self.assertIn("settings.json", settings_path)
        self.assertIn("scopes", settings_path)
        self.assertEqual(prepared.record["version"], sessions.RECORD_VERSION)
        self.assertEqual(prepared.record["lineup_generation"], 1)
        self.assertEqual(prepared.record["profile"], "balanced")

    def test_legacy_refuses_every_launch_form_before_runtime(self) -> None:
        expected = (
            "claude-multi: --legacy was retired: every launch is durable, "
            "and gateway auth now comes only from the session's apiKeyHelper "
            "(the env token was argv mode's only credential). Drop the flag. "
            "Legacy records still resume normally and upgrade to a durable "
            "scope on first resume.\n"
        )
        for argv in (
            [], ["--composition", "default"], ["-r", FIXED_ID], ["-c"], ["-r"],
            ["--composition", "default", "--print-launch"],
        ):
            for interactive in (True, False):
                with self.subTest(argv=argv, interactive=interactive):
                    output, errors = io.StringIO(), io.StringIO()
                    with mock.patch('claude_multi.cli.runtime.Runtime') as runtime, \
                            contextlib.redirect_stderr(errors):
                        code = cli.main(
                            [*argv, "--legacy"], output_stream=output,
                            interactive=interactive,
                        )
                    self.assertEqual(code, 2)
                    self.assertEqual((output.getvalue(), errors.getvalue()), ("", expected))
                    runtime.assert_not_called()
        self.assertNotIn("--legacy", cli.build_parser().format_help())

    def test_resume_legacy_record_migrates_and_compiles_generation_one(self) -> None:
        # A 2.x (argv-mode) record migrates at its resume; the
        # migrated gen-0 record compiles lineup generation 1.
        self.save_session(mode="legacy", scope_generation=0)
        prepared = self._prepared(["-r", FIXED_ID])
        self.assertTrue(prepared.result.durable)
        self.assertEqual(prepared.record["version"], sessions.RECORD_VERSION)
        self.assertEqual(prepared.record["lineup_generation"], 1)

    def test_resume_unchanged_v4_record_preserves_generation(self) -> None:
        record = self.save_v4_session()
        self.runtime.session_store.save({**record, "lineup_generation": 3})
        prepared = self._prepared(["-r", FIXED_ID], "")
        self.assertTrue(prepared.result.durable)
        self.assertEqual(prepared.record["lineup_generation"], 3)

    def test_retired_legacy_resume_leaves_lineage_untouched(self) -> None:
        self.save_session(mode="durable", scope_generation=2)
        before = self.runtime.session_store.read_record_bytes(FIXED_ID)
        code, output = self.run_cli_err(["-r", FIXED_ID, "--legacy"])
        self.assertEqual(code, 2)
        self.assertIn(cli.LEGACY_REFUSAL, output)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.runtime.session_store.read_record_bytes(FIXED_ID), before)


class StateMarkerWiringTests(CLITestCase):
    def _marker(self, body=b"5\n"):
        path = self.runtime.session_store.root / sessions.STATE_MARKER
        state.atomic_write(path, body)
        return path

    def _state_bytes(self):
        return {
            str(path.relative_to(self.runtime.session_store.root)): (
                path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns
            )
            for path in self.runtime.session_store.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    def test_write_commands_refuse_before_runtime_and_leave_state_unchanged(self):
        self.save_session()
        self._marker()
        before = self._state_bytes()
        commands = (
            [], ["--composition", "default"], ["-r", FIXED_ID], ["-c"],
            ["direct"], ["direct", "--resume", FIXED_ID],
            ["sessions", "forget", FIXED_ID], ["sessions", "link", OTHER_ID],
            ["sessions", "relink-runtime", FIXED_ID, OTHER_ID],
            ["sessions", "resolve-fork", FIXED_ID, OTHER_ID],
            ["sessions", "transition", FIXED_ID, "--composition", "default"],
            ["doctor", "--repair", FIXED_ID], ["doctor", "--repair-all"],
            ["doctor", "--prune"], ["doctor", "--rotate-token"],
            ["custom", "remove-model", "custom-test"],
            ["custom", "remove-provider", "custom-test"],
            ["custom", "add-provider", "custom-test", "--base-url", "https://example.test",
             "--auth", "bearer", "--secret-env", "TEST_KEY"],
            ["custom", "add-model", "custom-test", "--provider", "kimi",
             "--wire", "fixture-wire", "--context", "200000"],
        )
        with mock.patch.dict(os.environ, self.runtime.environ, clear=True):
            for command in commands:
                with self.subTest(command=command):
                    output, errors = io.StringIO(), io.StringIO()
                    with mock.patch('claude_multi.cli.runtime.Runtime') as runtime, \
                            contextlib.redirect_stderr(errors):
                        code = cli.main(command, output_stream=output, interactive=False)
                    runtime.assert_not_called()
                    # A refusal (exit 1) on stderr; nothing on stdout.
                    self.assertEqual(code, 1)
                    error = sessions.StateMarkerError(5)
                    self.assertEqual(
                        (output.getvalue(), errors.getvalue()),
                        ("", f"claude-multi: {error}\n  fix: {error.remedy}\n" if error.remedy else f"claude-multi: {error}\n"),
                    )
                    self.assertEqual(self._state_bytes(), before)

    def test_marker_is_checked_again_for_an_injected_runtime(self):
        self._marker(b"malformed")
        code, output = self.run_cli_err(["direct"], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("state version unknown", output)
        self.assertEqual(self.launches, [])

    def test_symlink_marker_refuses(self):
        marker = self.runtime.session_store.root / sessions.STATE_MARKER
        marker.symlink_to(self.root / "missing-marker-target")
        code, output = self.run_cli_err(["doctor", "--prune"], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("state version unknown", output)
        self.assertTrue(marker.is_symlink())

    def test_session_events_skip_without_runtime_or_stdin_reads(self):
        self._marker()
        before = self._state_bytes()
        with mock.patch.dict(os.environ, self.runtime.environ, clear=True):
            for event in ("start", "end"):
                with self.subTest(event=event):
                    inp = mock.Mock()
                    output, error = io.StringIO(), io.StringIO()
                    with mock.patch('claude_multi.cli.runtime.Runtime') as runtime, mock.patch.object(
                        sys, "stderr", error
                    ):
                        code = cli.main(
                            ["session-event", event, "--managed-id", FIXED_ID],
                            input_stream=inp, output_stream=output,
                        )
                    self.assertEqual(code, 0)
                    runtime.assert_not_called()
                    inp.read.assert_not_called()
                    self.assertEqual(output.getvalue(), "")
                    self.assertIn("newer claude-multi", error.getvalue())
        self.assertEqual(self._state_bytes(), before)

    def test_direct_session_event_handler_also_skips(self):
        self._marker()
        args = cli.build_parser().parse_args(
            ["session-event", "start", "--managed-id", FIXED_ID]
        )
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(cli._handle_session_event(
                self.runtime, args, input_stream=io.StringIO("not JSON"),
                output_stream=io.StringIO(),
            ), 0)

    def test_read_only_commands_and_shim_paths_work_without_refresh(self):
        self.save_session()
        self._marker()
        before = self._state_bytes()
        with mock.patch.object(scope_mod, "ensure_hook_shim") as hook, mock.patch.object(
            scope_mod, "ensure_hook_shim_v3"
        ) as hook3, mock.patch.object(
            scope_mod, "ensure_token_helper_command"
        ) as helper:
            runtime = cli.Runtime(
                asset_root=CATALOG_ROOT, environ=self.runtime.environ,
                cwd=self.runtime.cwd, **self._runtime_seams(),
            )
        hook.assert_not_called()
        hook3.assert_not_called()
        helper.assert_not_called()
        self.assertEqual(runtime.hook_command, self.runtime.hook_command)
        self.assertEqual(runtime.hook3_command, self.runtime.hook3_command)
        self.assertEqual(runtime.token_helper_command, self.runtime.token_helper_command)
        for command in (
            ["models"], ["show"], ["compose", "list"], ["compose", "show", "balanced"],
            ["profile", "list"], ["profile", "show", "balanced"],
            ["custom", "list"], ["sessions", "list"], ["sessions", "show", FIXED_ID], ["-r"],
            ["--composition", "balanced", "--print-launch"],
            ["direct", "--model", "qwen38", "--print-launch"],
        ):
            with self.subTest(command=command):
                code, output = self.run_cli(command, interactive=False)
                self.assertEqual(code, 0, output)
        self.assertEqual(self._state_bytes(), before)

    def test_runtime_refreshes_both_hook_shims(self):
        # Runtime writes the 2.x shim and the protocol-3
        # shim from the one resolved command; both 0700, exact bytes.
        root = self.runtime.session_store.root
        resolved = self.runtime.resolved_hook_command
        shim2 = scope_mod.hook_shim_path(root)
        shim3 = scope_mod.hook_shim_v3_path(root)
        self.assertEqual(self.runtime.hook_command, str(shim2))
        self.assertEqual(self.runtime.hook3_command, str(shim3))
        self.assertEqual(shim2.read_bytes(), scope_mod.hook_shim_text(resolved))
        self.assertEqual(shim3.read_bytes(), scope_mod.hook_shim_v3_text(root, resolved))
        for path in (shim2, shim3):
            with self.subTest(path=path.name):
                self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700)
        # A launcher move refreshes both (the shim tracks the newest install).
        moved = str(self.root / "moved" / "claude-multi")
        runtime = cli.Runtime(
            asset_root=CATALOG_ROOT,
            environ={**self.runtime.environ, "CLAUDE_MULTI_HOOK_COMMAND": moved},
            cwd=self.runtime.cwd, **self._runtime_seams(),
        )
        self.assertEqual(runtime.hook3_command, str(shim3))
        self.assertNotEqual(runtime.resolved_hook_command, resolved)
        self.assertEqual(
            shim2.read_bytes(), scope_mod.hook_shim_text(runtime.resolved_hook_command)
        )
        self.assertEqual(
            shim3.read_bytes(),
            scope_mod.hook_shim_v3_text(root, runtime.resolved_hook_command),
        )

    def test_marker_forces_interactive_session_lists_to_read_only(self):
        # A curses-capable terminal would reach the 3.0
        # sessions screen; a newer marker still refuses it (the listing).
        self._marker()
        with mock.patch('claude_multi.cli.screens.launch_sessions._sessions_list_tui') as picker, \
                mock.patch.object(claude_multi.tui, "streams_curses_capable", return_value=True):
            for command in (["-r"], ["sessions", "list"]):
                code, output = self.run_cli(command, interactive=True)
                self.assertEqual(code, 0, output)
            picker.assert_not_called()

    def test_doctor_reports_one_marker_problem_not_record_corruption(self):
        self._marker()
        for session_id in (FIXED_ID, OTHER_ID):
            state.atomic_write(
                self.runtime.session_store.sessions_dir / f"{session_id}.json",
                b'{"version":4}\n',
            )
        with mock.patch('claude_multi.cli.session_facts._session_record_scan') as scan, mock.patch.object(
            self.runtime, "custom_conflicts"
        ) as registry, mock.patch.object(self.runtime, "check_readiness") as readiness:
            code, output = self.run_cli(["doctor"], interactive=False)
        scan.assert_not_called()
        registry.assert_not_called()
        readiness.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(output.count("BLOCKED"), 1)
        self.assertEqual(output.count("  - "), 1)
        self.assertIn("newer claude-multi", output)
        self.assertIn("Sessions: 2 recorded; not inspected", output)

    def test_prune_library_guard_preserves_unlocked_legacy_prompt(self):
        prompt = self.runtime.session_store.root / "lead-prompt-legacy.md"
        state.atomic_write(prompt, b"keep\n")
        self._marker()
        before = self._state_bytes()
        with self.assertRaises(sessions.StateMarkerError):
            cli._doctor_prune(self.runtime, io.StringIO())
        self.assertEqual(self._state_bytes(), before)

    def test_custom_library_guards_precede_directory_and_lock_creation(self):
        self._marker()
        registry = {"version": 1, "providers": {}, "models": {}}
        mutate = mock.Mock()
        with mock.patch.object(state, "ensure_private_dir") as mkdir, mock.patch.object(
            state, "FileLock"
        ) as lock:
            with self.assertRaises(sessions.StateMarkerError):
                claude_multi.custom.save_registry(self.runtime.environ, registry)
            with self.assertRaises(sessions.StateMarkerError):
                claude_multi.custom._mutate(self.runtime.environ, mutate, touches=lambda _after: set())
        mkdir.assert_not_called()
        lock.assert_not_called()
        mutate.assert_not_called()
        self.assertEqual(claude_multi.custom.load_registry(self.runtime.environ), registry)

    def test_restore_2x_never_constructs_runtime_and_gates_on_the_marker(self):
        # A compatible marker body (<= 3) is 2.x state: nothing to restore;
        # newer or malformed markers print the marker error
        # and touch nothing. Marker 4 runs the real
        # restore (no records here: it only removes the marker, last).
        for body, expected, text in (
            (None, 0, "nothing to restore"),
            (b"3\n", 0, "nothing to restore"),
            (b"5\n", 1, f"claude-multi: {sessions.StateMarkerError(5)}\n"),
            (b"bad", 1, f"claude-multi: {sessions.StateMarkerError()}\n"),
        ):
            with self.subTest(body=body):
                if body is not None:
                    self._marker(body)
                before = self._state_bytes()
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, self.runtime.environ, clear=True), mock.patch('claude_multi.cli.runtime.Runtime'
                ) as runtime, contextlib.redirect_stderr(errors):
                    code = cli.main(["restore-2x"], output_stream=output, interactive=False)
                runtime.assert_not_called()
                self.assertEqual(code, expected)
                if expected == 0:
                    self.assertIn(text, output.getvalue())
                else:
                    self.assertEqual((output.getvalue(), errors.getvalue()), ("", text))
                self.assertEqual(self._state_bytes(), before)
        self._marker(b"4\n")
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.runtime.environ, clear=True), mock.patch('claude_multi.cli.runtime.Runtime'
        ) as runtime, mock.patch.object(
            sessions, "live_background_prefixes", return_value=frozenset()
        ), mock.patch.object(sessions, "proc_session_ids", return_value=frozenset()), mock.patch.object(
            sessions, "proc_session_scan", return_value=sessions.ProcessScan(True, frozenset())
        ):
            code = cli.main(["restore-2x"], output_stream=output, interactive=False)
        runtime.assert_not_called()
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("marker removed.", output.getvalue())
        self.assertFalse(
            os.path.lexists(self.runtime.session_store.root / sessions.STATE_MARKER)
        )
        self.assertIn("restore-2x", cli.build_parser().format_help())
        args = cli.build_parser().parse_args(["restore-2x"])
        tty, stdout = io.StringIO(), io.StringIO()
        self.assertIs(cli._report_output_stream(args, tty, stdout), stdout)
        args = cli.build_parser().parse_args(
            ["restore-2x", "--not-running", FIXED_ID, "--assume-dead", OTHER_ID]
        )
        self.assertEqual(args.not_running, [FIXED_ID])
        self.assertEqual(args.assume_dead, [OTHER_ID])


class SessionStoreUuidGuardTests(CLITestCase):
    def test_load_rejects_non_uuid(self) -> None:
        with self.assertRaises(sessions.SessionError):
            self.runtime.session_store.load("../escape")

    def test_forget_rejects_non_uuid(self) -> None:
        with self.assertRaises(sessions.SessionError):
            self.runtime.session_store.forget("..%2fbad")


class TransitionTuiScreenTests(unittest.TestCase):
    """Transition diff view + exited-confirmation Modal."""

    DIFF = ["lead model: opus55 -> sol", "workflows: native -> native"]

    def _run(self, keys):
        from test_tui import FakeWindow

        screen = cli._TransitionScreen(list(self.DIFF), palette=tui.MONO_PALETTE)
        win = FakeWindow(keys)
        return screen.run(win), win

    def test_diff_rendered_and_confirm_via_modal(self) -> None:
        confirmed, win = self._run(["\n", "\n"])
        self.assertTrue(confirmed)
        text = win.text()
        self.assertIn("lead model: opus55 -> sol", text)
        self.assertIn("EXITED (not merely idle)", text)
        self.assertIn("Has the target process exited?", text)

    def test_esc_cancels_without_confirmation(self) -> None:
        confirmed, _win = self._run(["\x1b"])
        self.assertFalse(confirmed)

    def test_modal_cancel_declines(self) -> None:
        confirmed, _win = self._run(["\n", curses.KEY_RIGHT, "\n"])
        self.assertFalse(confirmed)

    def test_modal_esc_declines(self) -> None:
        confirmed, _win = self._run(["\n", "\x1b"])
        self.assertFalse(confirmed)


class DoctorBadgePaletteTests(CLITestCase):
    """Doctor keeps its line contract; badges style only when color is active."""

    def test_output_palette_is_mono_for_non_tty(self) -> None:
        palette = cli._output_palette(io.StringIO(), io.StringIO(), False)
        self.assertIs(palette, tui.MONO_PALETTE)

    def test_output_palette_detects_dark_for_tty(self) -> None:
        class Tty(io.StringIO):
            def isatty(self):
                return True

        with mock.patch.dict(os.environ, {"COLORFGBG": "15;0"}, clear=False):
            os.environ.pop("NO_COLOR", None)
            palette = cli._output_palette(Tty(), Tty(), False)
        self.assertIs(palette, tui.DARK_PALETTE)

    def test_output_palette_no_color_flag_wins(self) -> None:
        class Tty(io.StringIO):
            def isatty(self):
                return True

        palette = cli._output_palette(Tty(), Tty(), True)
        self.assertIs(palette, tui.MONO_PALETTE)

    def test_doctor_ready_badge_colored_only_on_tty(self) -> None:
        class Tty(io.StringIO):
            def isatty(self):
                return True

        output = Tty()
        with mock.patch.dict(os.environ, {"COLORFGBG": "15;0"}, clear=False):
            os.environ.pop("NO_COLOR", None)
            code = cli.main(
                ["doctor"],
                runtime=self.runtime,
                output_stream=output,
                interactive=False,
            )
        self.assertEqual(code, 0)
        self.assertIn("\x1b[32mReady\x1b[0m", output.getvalue())

    def test_doctor_ready_badge_plain_with_no_color(self) -> None:
        class Tty(io.StringIO):
            def isatty(self):
                return True

        output = Tty()
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            code = cli.main(
                ["doctor"],
                runtime=self.runtime,
                output_stream=output,
                interactive=False,
            )
        self.assertEqual(code, 0)
        self.assertIn("Ready\n", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())


class SessionsListTuiDriverTests(CLITestCase):
    """The tty entry of the sessions screen: bare ``-r`` on a curses-capable
    terminal gets ``_sessions_list_tui``, whose ``("perform", …)`` result
    ``main`` performs after teardown; ``--line`` keeps the numbered line
    chooser. ``sessions list`` is a listing everywhere, never the screen.
    """

    def setUp(self) -> None:
        super().setUp()
        # The screen samples liveness through the Runtime seams.
        for name in ("_live_prefixes", "_proc_ids"):
            patcher = mock.patch.object(self.runtime, name, return_value=frozenset())
            patcher.start()
            self.addCleanup(patcher.stop)

    def _capable(self):
        return mock.patch.object(claude_multi.tui, "streams_curses_capable", return_value=True)

    def test_quit_returns_zero_without_action(self) -> None:
        self.save_v4_session()
        with self._capable(), mock.patch(
            "claude_multi.tui.run_curses_on_streams", return_value=None
        ) as curses_run:
            code, _output = self.run_cli(["-r"])
        self.assertEqual(code, 0)
        curses_run.assert_called_once()
        self.assertEqual(self.launches, [])

    def test_tty_sessions_list_is_a_listing_not_the_screen(self) -> None:
        self.save_v4_session()
        with self._capable(), mock.patch('claude_multi.cli.screens.launch_sessions._sessions_list_tui', return_value=None) as screen:
            code, output = self.run_cli(["sessions", "list"], "1\n")
        self.assertEqual(code, 0, output)
        screen.assert_not_called()
        self.assertTrue(output.startswith("sessions\n"), output)
        self.assertNotIn("resume which session", output)
        self.assertEqual(self.launches, [])

    def test_tty_bare_resume_opens_the_sessions_screen(self) -> None:
        self.save_v4_session()
        with self._capable(), mock.patch('claude_multi.cli.screens.launch_sessions._sessions_list_tui', return_value=None) as screen:
            code, output = self.run_cli(["-r"], "1\n")
        self.assertEqual(code, 0, output)
        screen.assert_called_once()
        kwargs = screen.call_args.kwargs
        self.assertIs(screen.call_args.args[0], self.runtime)
        self.assertIsInstance(kwargs["input_stream"], io.StringIO)
        self.assertIsInstance(kwargs["output_stream"], io.StringIO)
        self.assertNotIn("resume which session", output)
        self.assertEqual(self.launches, [])

    def test_tty_sessions_list_screen_result_is_performed_after_teardown(self) -> None:
        self.save_v4_session()
        prepared = self.runtime.prepare(
            cli._record_resume_target(self.runtime.session_store.load(FIXED_ID)),
            action="resume", passthrough=[], session_id=FIXED_ID,
        )
        self._touch_transcript()
        with self._capable(), mock.patch(
            'claude_multi.cli.screens.launch_sessions._sessions_list_tui', return_value=("perform", prepared, None)
        ):
            code, output = self.run_cli(["-r"])
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.launches[0].result.session_action.kind, "resume")
        self.assertEqual(self.launches[0].record["managed_id"], FIXED_ID)

    def test_tty_sessions_list_screen_esc_returns_zero(self) -> None:
        self.save_v4_session()
        with self._capable(), mock.patch('claude_multi.cli.screens.launch_sessions._sessions_list_tui', return_value=None):
            code, _output = self.run_cli(["sessions", "list"], "\n")
        self.assertEqual(code, 0)
        self.assertEqual(self.launches, [])

    def test_tty_sessions_list_with_line_flag_prints_the_listing(self) -> None:
        self.save_v4_session()
        with self._capable(), mock.patch('claude_multi.cli.screens.launch_sessions._sessions_list_tui') as screen:
            code, output = self.run_cli(["--line", "sessions", "list"], "1\n")
        self.assertEqual(code, 0, output)
        screen.assert_not_called()
        self.assertIn(FIXED_ID[:8], output)
        self.assertNotIn("resume which session", output)
        self.assertEqual(self.launches, [])

    def test_tty_bare_resume_with_line_flag_uses_the_line_chooser_and_resumes(self) -> None:
        self.save_v4_session()
        self._touch_transcript()
        with self._capable(), mock.patch('claude_multi.cli.screens.launch_sessions._sessions_list_tui') as screen:
            code, output = self.run_cli(["--line", "-r"], "1\n")
        self.assertEqual(code, 0, output)
        screen.assert_not_called()
        self.assertIn("resume which session (number, Enter cancels): ", output)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.launches[0].result.session_action.kind, "resume")
        self.assertEqual(self.launches[0].record["managed_id"], FIXED_ID)

    def test_tty_bare_resume_opens_the_sessions_screen_with_the_passthrough(self) -> None:
        self.save_v4_session()
        with self._capable(), mock.patch('claude_multi.cli.screens.launch_sessions._sessions_list_tui', return_value=None) as screen:
            code, _output = self.run_cli(["-r", "--", "--verbose"])
        self.assertEqual(code, 0)
        screen.assert_called_once()
        self.assertEqual(list(screen.call_args.kwargs["passthrough"]), ["--verbose"])

    def _touch_transcript(self) -> None:
        record = self.runtime.session_store.load(FIXED_ID)
        transcript = (
            Path(self.runtime.environ["HOME"]) / ".claude" / "projects"
            / cli._native_project_slug(record["cwd"]) / f"{sessions.runtime_session_id(record)}.jsonl"
        )
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.touch()


class HelperOnlyWorkflowsTests(CLITestCase):
    def test_off_prepares_durable(self) -> None:
        document = copy.deepcopy(self.runtime.profiles.load("balanced"))
        document["workflows"] = "off"
        prepared = self.runtime.prepare(
            cli.LaunchTarget("profile-file", document, None, False, "file"),
            action="fresh",
            passthrough=[],
        )
        self.assertTrue(prepared.result.durable)
        self.assertEqual(prepared.lineup.workflows, "off")
        self.assertEqual(
            prepared.result.scope_plan.settings["apiKeyHelper"],
            self.runtime.token_helper_command,
        )

class TransitionPreflightTests(CLITestCase):
    """Binary verification + gateway readiness run BEFORE prepare;
    a failure aborts with record and scope untouched."""

    def _transition(self, *extra, **kwargs):
        """``(exit, stdout then stderr)``: a refusal reports on stderr."""

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code, output = self.run_cli(
                ["sessions", "transition", FIXED_ID, "--composition", "quality", *extra],
                **kwargs,
            )
        return code, output + errors.getvalue()

    def test_preflight_refusal_is_pinned_verbatim(self) -> None:
        self.assertEqual(
            cli.RELAUNCH_PREFLIGHT_REFUSAL,
            "relaunch preflight failed; the session record and scope are untouched: ",
        )

    def _refused(self, *extra, **kwargs) -> str:
        before = self.save_v4_session()
        with mock.patch('claude_multi.cli.runtime.Runtime.prepare') as prepare_mock:
            code, output = self._transition(*extra, **kwargs)
        self.assertEqual(code, 1)
        prepare_mock.assert_not_called()
        self.assertIn("relaunch preflight failed", output)
        self.assertIn("the session record and scope are untouched", output)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.runtime.session_store.load(FIXED_ID), before)
        return output

    def test_binary_failure_aborts_before_prepare(self) -> None:
        self.runtime.doctor_binary_callback = lambda _contract: (
            ["managed Claude binary: inspected Claude artifact /x is missing; "
             "re-run the native-contract inspection against the installed version"],
            [],
        )
        output = self._refused(text="y\n")
        self.assertIn("re-run the native-contract inspection", output)

    def test_readiness_failure_aborts_before_prepare(self) -> None:
        self.runtime.doctor_callback = lambda _runtime: ["local gateway: fixture gateway down"]
        output = self._refused(text="y\n")
        self.assertIn("local gateway: fixture gateway down", output)

    def test_its_exited_flag_also_preflights(self) -> None:
        self.runtime.doctor_binary_callback = lambda _contract: (
            ["managed Claude binary: content hash does not match"], []
        )
        self._refused("--its-exited", interactive=False)

    def test_confirmed_relaunch_runs_preflight_then_prepare(self) -> None:
        self.save_v4_session()
        calls: list[str] = []
        real_preflight = cli._transition_preflight_problems
        real_prepare = cli.Runtime.prepare

        def preflight(runtime):
            calls.append("preflight")
            return real_preflight(runtime)

        def prepare(runtime, *args, **kwargs):
            calls.append("prepare")
            return real_prepare(runtime, *args, **kwargs)

        with mock.patch('claude_multi.cli.launch_flow._transition_preflight_problems', preflight), mock.patch(
            'claude_multi.cli.runtime.Runtime.prepare', prepare
        ):
            code, output = self._transition(text="y\n")
        self.assertEqual(code, 0, output)
        self.assertEqual(calls, ["preflight", "prepare"])
        self.assertEqual(len(self.launches), 1)


class TerminalInjectionTests(CLITestCase):
    """External text (cwd, project paths, names) never reaches the
    terminal raw; every render point escapes control bytes visibly."""

    OSC_CWD = "/tmp/evil\x1b]8;;https://bad.example\x07link\x1b]8;;\x07"

    def test_hostile_cwd_sanitized_in_sessions_list(self) -> None:
        record = self.save_session()
        record["cwd"] = self.OSC_CWD
        _v3.save_any(self.runtime.session_store, record)
        code, output = self.run_cli(["sessions", "list"], interactive=False)
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\x07", output)
        self.assertIn("^[]8;;https://bad.example^Glink^[]8;;^G", output)

    def test_hostile_project_agent_path_sanitized_in_doctor(self) -> None:
        # Doctor checks the balanced seed's bound ids.
        variant_id = "cm-reviewer"
        agents = Path(self.runtime.cwd) / ".claude" / "agents"
        agents.mkdir(parents=True)
        hostile = agents / "cm-shadow\x1b[2J.md"
        hostile.write_bytes(
            f"---\nname: {variant_id}\ndescription: shadow\n---\nbody\n".encode()
        )
        code, output = self.run_cli(["doctor"], interactive=False)
        self.assertEqual(code, 1)
        self.assertNotIn("\x1b", output)
        self.assertIn("cm-shadow^[", output)

    def test_hostile_override_name_sanitized_in_main_error(self) -> None:
        self.save_session()
        code, output = self.run_cli_err(
            ["--composition", "evil\x1b]8;;https://bad\x07", "-r", FIXED_ID],
            interactive=False,
        )
        self.assertEqual(code, 1)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\x07", output)
        # --composition is the --profile alias; the hostile name
        # fails the state-name check before the resume-override refusal.
        self.assertIn("unsafe state name", output)

class ResumeNameResolutionTests(CLITestCase):
    """-r accepts exact UUIDs and composition names (native exit-hint form)."""

    OTHER_ID = "97a6194a-1111-4222-8333-444455556666"

    def test_uuid_still_resumes_exact(self) -> None:
        self.save_session()
        self.assertEqual(
            cli._resolve_resume_target(self.runtime, FIXED_ID), FIXED_ID
        )

    def test_unique_composition_name_resolves(self) -> None:
        self.save_session()
        self.assertEqual(
            cli._resolve_resume_target(self.runtime, "default"), FIXED_ID
        )

    def test_cm_prefixed_name_resolves(self) -> None:
        self.save_session()
        self.assertEqual(
            cli._resolve_resume_target(self.runtime, "cm:default"), FIXED_ID
        )

    def test_ambiguous_name_lists_candidates(self) -> None:
        self.save_session(session_id=FIXED_ID)
        self.save_session(session_id=self.OTHER_ID)
        with self.assertRaises(cli.CLIError) as ctx:
            cli._resolve_resume_target(self.runtime, "default")
        text = str(ctx.exception)
        self.assertIn("matches 2 managed sessions", text)
        self.assertIn(FIXED_ID, text)
        self.assertIn(self.OTHER_ID, text)

    def test_unknown_name_points_at_sessions_list(self) -> None:
        with self.assertRaises(cli.CLIError) as ctx:
            cli._resolve_resume_target(self.runtime, "cm:no-such-thing")
        self.assertIn("claude-multi sessions list", ctx.exception.remedy)

    def test_resume_by_name_end_to_end(self) -> None:
        self.save_session()
        code, _out = self.run_cli(["-r", "cm:default"], "\n", interactive=True)
        self.assertEqual(code, 0)
        self.assertEqual(
            self.launches[0].record["managed_id"], FIXED_ID
        )

    def test_qualified_display_name_resolves(self) -> None:
        # The exit hint prints exactly the --name value:
        # cm:<composition>@<project> must resolve like the plain forms.
        self.save_session()
        self.assertEqual(
            cli._resolve_resume_target(self.runtime, "cm:default@project"),
            FIXED_ID,
        )

    def test_qualified_name_uses_recorded_cwd(self) -> None:
        record = self.save_session()
        record["cwd"] = "/somewhere/project-x"
        _v3.save_any(self.runtime.session_store, record)
        self.assertEqual(
            cli._resolve_resume_target(self.runtime, "cm:default@project-x"),
            FIXED_ID,
        )

    def test_qualified_name_ambiguity_lists_candidates(self) -> None:
        self.save_session(session_id=FIXED_ID)
        self.save_session(session_id=self.OTHER_ID)
        with self.assertRaises(cli.CLIError) as ctx:
            cli._resolve_resume_target(self.runtime, "cm:default@project")
        text = str(ctx.exception)
        self.assertIn("matches 2 managed sessions", text)
        self.assertIn(FIXED_ID, text)
        self.assertIn(self.OTHER_ID, text)

    def test_unknown_qualified_name_points_at_sessions_list(self) -> None:
        self.save_session()
        with self.assertRaises(cli.CLIError) as ctx:
            cli._resolve_resume_target(self.runtime, "cm:default@nowhere")
        self.assertIn("claude-multi sessions list", ctx.exception.remedy)

    def test_resume_by_qualified_name_end_to_end(self) -> None:
        self.save_session()
        code, _out = self.run_cli(
            ["-r", "cm:default@project"], "\n", interactive=True
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            self.launches[0].record["managed_id"], FIXED_ID
        )


class VisibleMessageTests(CLITestCase):
    def test_multi_line_error_keeps_structure(self) -> None:
        self.save_session(session_id=FIXED_ID)
        self.save_session(session_id="97a6194a-1111-4222-8333-444455556666")
        code, out = self.run_cli_err(["-r", "default"], interactive=True)
        self.assertEqual(code, 1)
        self.assertIn("matches 2 managed sessions", out)
        self.assertNotIn("^J", out)
        self.assertIn(FIXED_ID, out)
        # Each candidate renders on its own real line.
        self.assertTrue(
            any(line.strip().startswith(FIXED_ID) for line in out.splitlines())
        )

    def test_visible_message_sanitizes_per_line(self) -> None:
        hostile = "line one\x1b]0;pwned\x07\nline two"
        rendered = claude_multi.tui.visible_message(hostile)
        self.assertEqual(rendered, "line one^[]0;pwned^G\nline two")


# -- G picker helpers ----------------------------------------------
# Picker rows, groups, cursor targets and size floors are derived from the
# loaded (fixture) catalog, never pinned: a catalog line added or removed
# moves no test here.


def _picker_floor(screen, width: int) -> int:
    """The G picker's exact size floor at ``width``.

    Title, separator, subtitle and a blank; one header, the rows and a blank
    per group; a trailing blank; the worst-case wrapped detail lines.
    """

    return (
        4 + len(screen.rows) + 2 * len(screen.groups) + 1 + screen._detail_reserve(width)
    )


def _steps(screen, target: str, *, up="k", down="j") -> list:
    """Keys moving the picker cursor from its preselect onto ``target``."""

    delta = screen.rows.index(target) - screen.selected
    return [down] * delta if delta > 0 else [up] * -delta


def _row_ready(screen, model_id: str) -> bool:
    """Enter launches this row without any confirm modal."""

    return (
        screen._row_reason(model_id) is None
        and screen._row_signin_pool(model_id) is None
        and not screen._row_unserved(model_id)
    )


def _nearest(screen, predicate) -> str:
    """The row nearest the preselect (ties: above) matching ``predicate``."""

    here = screen.selected
    candidates = [
        index
        for index, model_id in enumerate(screen.rows)
        if index != here and predicate(model_id)
    ]
    if not candidates:
        raise AssertionError("no picker row in the loaded catalog fits this test")
    return screen.rows[min(candidates, key=lambda i: (abs(i - here), i > here))]


def _provider_secret_ref(runtime, model_id: str) -> str | None:
    """``env:NAME`` secret ref of a model's direct provider (None otherwise)."""

    docs = runtime.ordinary_docs
    provider = docs["models"]["models"][model_id]["provider"]
    transport = docs["providers"]["providers"][provider]["transport"]
    if transport["kind"] != "direct":
        return None
    return transport["auth"]["secret_ref"]


def _grant_secret(secret_file: Path, runtime, model_id: str) -> None:
    """Append a dummy value for the model's direct-provider secret, if any."""

    ref = _provider_secret_ref(runtime, model_id)
    if ref is not None:
        with open(secret_file, "a", encoding="utf-8") as handle:
            handle.write(f"{ref.removeprefix('env:')}=late-addition\n")


def _keyless_ordinary_model(runtime, profile: str | None = None) -> tuple[str, str]:
    """(line key, secret ref) of a profiled lead line whose direct provider
    secret is absent (the CLITestCase secret file holds only
    KIMI_CLAUDE_API_KEY), optionally within one ordinary ``profile``.

    The merged v2 lines in catalog order,
    catalog lines only (no ``family``), not New, lead-capable, with a
    non-null ``context.ordinary_profile``; the 2.x
    ``compiler.ordinary_launch_models`` it read is deleted.
    """

    unavailable = cli._ordinary_unavailable(runtime)
    lcat = runtime.lineup_catalog()
    for key, entry in lcat.lines.items():
        if "family" in entry or entry.get("status", "active") == "new":
            continue
        if "lead" not in entry.get("capabilities", ()):
            continue
        line_profile = (entry.get("context") or {}).get("ordinary_profile")
        if line_profile is None or (profile is not None and line_profile != profile):
            continue
        provider = entry["provider"]
        if provider in unavailable:
            transport = lcat.providers[provider]["transport"]
            return key, transport["auth"]["secret_ref"]
    raise AssertionError("the loaded catalog has no keyless ordinary model")


class DirectCommandSecretTests(CLITestCase):
    """``claude-multi direct --model``'s advisory secret warnings (not a screen).

    Kept with ``_break_secret_file`` apart from the card tests.
    """

    def test_direct_cli_warns_on_missing_secret_before_launch(self) -> None:
        keyless, secret_ref = _keyless_ordinary_model(self.runtime)
        code, out = self.run_cli(["direct", "--model", keyless], interactive=False)
        self.assertEqual(code, 0)
        self.assertIn(f"warning: missing required secret {secret_ref}", out)
        self.assertEqual(len(self.launches), 1)  # advisory, never blocking

    def test_direct_cli_print_launch_stays_clean(self) -> None:
        # Dry-run reports never carry the warning, even with the secret gone.
        keyless, _secret_ref = _keyless_ordinary_model(self.runtime)
        code, out = self.run_cli(
            ["direct", "--model", keyless, "--print-launch"], interactive=False
        )
        self.assertEqual(code, 0)
        self.assertNotIn("warning:", out)
        self.assertEqual(self.launches, [])

    def test_direct_cli_silent_when_secret_present(self) -> None:
        keyless, _secret_ref = _keyless_ordinary_model(self.runtime)
        _grant_secret(self.secret_file, self.runtime, keyless)
        code, out = self.run_cli(["direct", "--model", keyless], interactive=False)
        self.assertEqual(code, 0)
        self.assertNotIn("warning:", out)

    def _break_secret_file(self, style: str = "duplicate") -> None:
        # "duplicate": ProxyError from the parser; "bytes": invalid UTF-8,
        # translated to ProxyError by parse_secret_env's decode wrapper.
        if style == "duplicate":
            with open(self.secret_file, "a", encoding="utf-8") as handle:
                handle.write("KIMI_CLAUDE_API_KEY=duplicate-assignment\n")
        else:
            with open(self.secret_file, "ab") as handle:
                handle.write(b"\xff\xfe invalid bytes")

    def test_direct_oauth_provider_never_probes_secrets(self) -> None:
        # sol's provider is an OAuth pool: the availability probe must not
        # run at all, even with a broken secret env file.
        self._break_secret_file()
        probed = []
        original = gateway_facts._ordinary_unavailable

        def spy(runtime):
            probed.append(True)
            return original(runtime)

        with mock.patch.object(gateway_facts, '_ordinary_unavailable', side_effect=spy):
            code, out = self.run_cli(["direct", "--model", "sol"], interactive=False)
        self.assertEqual(code, 0)
        self.assertNotIn("warning:", out)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(probed, [])

    def test_direct_warns_statically_on_malformed_secret_file(self) -> None:
        for style in ("duplicate", "bytes"):
            self.setUp()
            self._break_secret_file(style)
            code, out = self.run_cli(["direct", "--model", "kimi-k3"], interactive=False)
            self.assertEqual(code, 0)
            self.assertIn(f"warning: {cli.DIRECT_SECRET_FILE_ERROR}", out)
            self.assertEqual(len(self.launches), 1)  # advisory, never blocking

    def test_direct_flushes_warning_before_launch_boundary(self) -> None:
        import io as _io

        class FlushSpy(_io.StringIO):
            def __init__(self) -> None:
                super().__init__()
                self.flushes = 0

            def flush(self) -> None:
                self.flushes += 1
                super().flush()

        spy = FlushSpy()

        def launch_then_assert_flushed(prepared):
            # execve never flushes Python buffers: the warning must be out
            # before control reaches the launch boundary.
            self.assertGreater(spy.flushes, 0)
            self.launches.append(prepared)
            return 0

        self.runtime.launch_callback = launch_then_assert_flushed
        keyless, _secret_ref = _keyless_ordinary_model(self.runtime)
        code = cli.main(
            ["direct", "--model", keyless],
            runtime=self.runtime,
            input_stream=None,
            output_stream=spy,
            interactive=False,
        )
        self.assertEqual(code, 0)
        self.assertIn("warning:", spy.getvalue())
        self.assertEqual(len(self.launches), 1)


class ComposeListOrderTests(CLITestCase):
    """`profile list` (and its `compose list` alias): origin + last use."""

    def test_order_and_last_used_column(self) -> None:
        self.runtime.profiles.duplicate("balanced", "second")
        self.save_v4_session("second")
        with contextlib.redirect_stderr(io.StringIO()):
            code, out = self.run_cli(["compose", "list"], interactive=False)
        self.assertEqual(code, 0)
        header, *lines = out.splitlines()
        self.assertEqual(header.split("\t"), ["profile", "origin", "state", "used", "notes"])
        rows = {line.split("\t")[0]: line.split("\t") for line in lines}
        self.assertEqual(rows["second"][1], "yours")
        self.assertNotEqual(rows["second"][3], "-")
        self.assertEqual((rows["balanced"][1], rows["balanced"][3]), ("seed", "-"))
        self.runtime.profiles.update("balanced", lambda doc: doc.update({"description": "x"}))
        code, out = self.run_cli(["profile", "list"], interactive=False)
        rows = {line.split("\t")[0]: line.split("\t") for line in out.splitlines()[1:]}
        self.assertEqual((rows["balanced"][1], rows["balanced"][3]), ("seed·edited", "-"))


class CompositionFileTests(CLITestCase):
    """--profile-file (and its 2.x --composition-file alias)."""

    def _write_document(self, name="fly-by-night") -> Path:
        document = self.runtime.profiles.load("balanced")
        document["name"] = name
        document.pop("seed", None)
        path = self.root / "fly.json"
        state.atomic_write(path, strict_json.pretty_file_bytes(document))
        return path

    def test_valid_file_launches_noninteractive_pinned_and_nameless(self) -> None:
        path = self._write_document()
        code, out = self.run_cli(["--profile-file", str(path)], interactive=False)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.launches), 1)
        record = self.launches[0].record
        self.assertIsNone(record["profile"])
        self.assertFalse(record["follow"])
        self.assertEqual(len(record["applied"]["agents"]), 8)

    def test_composition_file_alias_reads_a_profile_file(self) -> None:
        path = self._write_document()
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            code, out = self.run_cli(["--composition-file", str(path)], interactive=False)
        self.assertEqual(code, 0, out)
        self.assertIn("'--composition-file' is the earlier spelling of '--profile-file'", stderr.getvalue())

    def test_stdin_dash_forces_noninteractive(self) -> None:
        document = self.runtime.profiles.load("balanced")
        payload = strict_json.pretty_file_bytes(document).decode("utf-8")
        with unittest.mock.patch("sys.stdin", io.StringIO(payload)):
            code, out = self.run_cli(["--profile-file", "-"], interactive=None)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.launches), 1)

    def test_schema_invalid_file_is_exit_2(self) -> None:
        path = self.root / "bad.json"
        state.atomic_write(path, b'{"version": 2, "name": "x"}')
        code, out = self.run_cli_err(["--profile-file", str(path)], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("cannot load profile file", out)

    def test_a_2x_composition_file_names_profile_migrate(self) -> None:
        # The seedless store raises for "default".
        document = _v3.default_composition()
        path = self.root / "v1.json"
        state.atomic_write(path, strict_json.pretty_file_bytes(document))
        code, out = self.run_cli_err(["--profile-file", str(path)], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("unsupported profile version 1", out)
        self.assertIn("claude-multi profile migrate", out)

    def test_mutually_exclusive_with_profile_flag(self) -> None:
        path = self._write_document()
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["--profile", "balanced", "--profile-file", str(path)])

    def test_resume_refuses_a_profile_file(self) -> None:
        record = self.save_v4_session()
        path = self._write_document(name="balanced")
        code, out = self.run_cli_err(["-r", FIXED_ID, "--profile-file", str(path)], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn(
            cli.RESUME_PROFILE_FILE_REFUSAL.format(rid=record["runtime_session_id"]), out
        )
        self.assertIn("never verifiably", out)

    def test_oversized_stdin_rejected_before_unbounded_read(self) -> None:
        limit = strict_json.DEFAULT_LIMITS.max_bytes
        payload = " " * (limit + 2)
        with unittest.mock.patch("sys.stdin", io.StringIO(payload)):
            code, out = self.run_cli_err(["--profile-file", "-"], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("byte limit", out)

    def test_print_launch_accepts_a_profile_file_and_writes_nothing(self) -> None:
        path = self._write_document()
        code, out = self.run_cli(["--profile-file", str(path), "--print-launch"], interactive=False)
        self.assertEqual(code, 0, out)
        self.assertIn("claude argv", out)
        self.assertIn("record: profile (ad-hoc direct) · follow no · lineup generation 1", out)
        self.assertEqual(self.launches, [])
        self.assertEqual(list(self.runtime.session_store.sessions_dir.iterdir()), [])


class DoctorServedCrossCheckTests(CLITestCase):
    """Served-vs-rendered selector cross-check (loopback only)."""

    def _report(self, served):
        with unittest.mock.patch.object(
            claude_multi.launch, "served_models", return_value=(served, 200)
        ):
            return cli._doctor_served_report(self.runtime, "t" * 64)

    def _document(self, tokens=(FIXTURE_GATEWAY_TOKEN,)):
        home = Path(self.runtime.environ["HOME"])
        # v2 lines plus the continuity set doctor expects (the seed
        # set while continuity.json is absent).
        document, _available, _unavailable, _info = claude_multi.render.build_config_document(
            self.runtime.catalog.docs["gateway"],
            self.runtime.catalog.docs["providers"]["providers"],
            self.runtime.catalog.docs["models-v2"]["models"],
            home=home,
            gateway_tokens=tokens,
            resolve_secret=lambda name: claude_multi.proxy.resolve_secret(
                name, environ=self.runtime.environ
            ),
            continuity=claude_multi.continuity.seed_only(self.runtime.catalog)["aliases"],
        )
        return document

    def _expected(self):
        return claude_multi.render.rendered_selectors(self._document())

    def _write_config(self, document) -> None:
        config_dir = claude_multi.proxy.config_dir(Path(self.runtime.environ["HOME"]))
        state.ensure_private_dir(config_dir)
        state.atomic_write(
            config_dir / "config.yaml",
            claude_multi.render.emit_yaml(document).encode("utf-8"),
        )

    def test_full_match_is_silent(self) -> None:
        problems, info = self._report(set(self._expected()))
        self.assertEqual(problems, [])
        self.assertEqual(info, [])

    def test_missing_selector_with_current_render_loaded_names_restart(self) -> None:
        expected = set(self._expected())
        # A DIRECT-provider alias: no OAuth-record disambiguation applies.
        missing_one = "claude-multi-kimi-k3"
        self.assertIn(missing_one, expected)
        problems, _info = self._report(expected - {missing_one})
        self.assertEqual(len(problems), 1)
        self.assertIn(missing_one, problems[0])
        self.assertIn("claude-multi gateway restart", problems[0])

    def test_missing_oauth_alias_without_record_points_to_login(self) -> None:
        expected = set(self._expected())
        self.assertIn("claude-multi-opus-5", expected)
        # Fixture has no OAuth credential records: login guidance, not restart.
        problems, info = self._report(expected - {"claude-multi-opus-5"})
        self.assertEqual(problems, [])
        self.assertEqual(len(info), 1)
        self.assertIn("claude-multi providers sign-in anthropic", info[0])
        self.assertIn("claude-multi-opus-5", info[0])

    def test_missing_oauth_alias_with_record_is_a_restart_problem(self) -> None:
        expected = set(self._expected())
        auth_dir = (
            Path(self.runtime.environ["HOME"])
            / self.runtime.catalog.docs["gateway"]["gateway"]["auth_dir"]
        )
        state.ensure_private_dir(auth_dir)
        state.atomic_write(auth_dir / "claude-fixture.json", b"{}")
        problems, _info = self._report(expected - {"claude-multi-opus-5"})
        self.assertEqual(len(problems), 1)
        self.assertIn("claude-multi-opus-5", problems[0])
        self.assertIn("claude-multi gateway restart", problems[0])

    def test_config_drift_is_a_problem_naming_the_apply(self) -> None:
        config_dir = claude_multi.proxy.config_dir(Path(self.runtime.environ["HOME"]))
        state.ensure_private_dir(config_dir)
        state.atomic_write(config_dir / "config.yaml", b"stale: true\n")
        problems, _info = self._report(set(self._expected()))
        self.assertEqual(len(problems), 1)
        self.assertIn("claude-multi providers apply", problems[0])
        # The gateway hot-reloads; the apply verifies and names a restart
        # only when needed — the drift line never prescribes one.
        self.assertIn("verified reload", problems[0])
        self.assertNotIn("systemctl", problems[0])

    def test_fresh_config_match_produces_no_drift_problem(self) -> None:
        document = self._document()
        self._write_config(document)
        served = set(self._expected()) | {claude_multi.render.document_sentinel(document)}
        problems, info = self._report(served)
        self.assertEqual(problems, [])
        self.assertEqual(info, [])

    def test_sentinel_bearing_render_is_compared_without_refinalizing(self) -> None:
        # build_config_document already carries exactly one sentinel; the
        # drift check must compare that emit byte-for-byte.
        document = self._document()
        sections = document["openai-compatibility"]
        self.assertEqual(
            [s["name"] for s in sections].count("claude-multi-render"), 1
        )
        self._write_config(document)
        snap = cli._gateway_snapshot(self.runtime, FIXTURE_GATEWAY_TOKEN)
        self.assertFalse(snap.config_drift)
        self.assertEqual(snap.sentinel, claude_multi.render.document_sentinel(document))
        self.assertNotIn(snap.sentinel, snap.expected)

    def test_older_served_sentinel_is_a_restart_required_problem(self) -> None:
        document = self._document()
        self._write_config(document)
        stale = claude_multi.render.SENTINEL_PREFIX + "00000000"
        self.assertNotEqual(stale, claude_multi.render.document_sentinel(document))
        problems, info = self._report(set(self._expected()) | {stale})
        self.assertEqual(len(problems), 1)
        self.assertIn("did not reload the current render (sentinel missing)", problems[0])
        self.assertIn("claude-multi gateway restart", problems[0])
        self.assertEqual(info, [])

    def test_old_sentinels_beside_the_current_one_are_not_stale_selectors(self) -> None:
        document = self._document()
        self._write_config(document)
        served = set(self._expected()) | {
            claude_multi.render.document_sentinel(document),
            claude_multi.render.SENTINEL_PREFIX + "00000000",
        }
        problems, info = self._report(served)
        self.assertEqual(problems, [])
        self.assertEqual(info, [])

    def test_no_served_sentinel_is_not_a_reload_problem(self) -> None:
        # A gateway started from a pre-2.26 render serves no sentinel at
        # all; drift (if any) is the actionable line, not "did not reload".
        self._write_config(self._document())
        problems, info = self._report(set(self._expected()))
        self.assertEqual(problems, [])
        self.assertEqual(info, [])

    def test_dual_key_window_is_attention_not_drift(self) -> None:
        home = Path(self.runtime.environ["HOME"])
        config_dir = claude_multi.proxy.config_dir(home)
        previous = "b" * 64
        state.atomic_write(config_dir / "previous-key", (previous + "\n").encode())
        document = self._document((previous, FIXTURE_GATEWAY_TOKEN))
        self._write_config(document)
        served = set(self._expected()) | {claude_multi.render.document_sentinel(document)}
        problems, info = self._report(served)
        self.assertEqual(problems, [])
        self.assertEqual(info, [])
        attention = cli._token_rotation_attention(self.runtime)
        self.assertIn("rotation in progress", attention)
        self.assertIn("claude-multi doctor --rotate-token", attention)
        self.assertNotIn(previous, attention)
        self.assertNotIn(FIXTURE_GATEWAY_TOKEN, attention)
        state.remove_private(config_dir / "previous-key")
        self.assertIsNone(cli._token_rotation_attention(self.runtime))

    def test_unusable_key_slot_is_a_problem_without_its_value(self) -> None:
        config_dir = claude_multi.proxy.config_dir(Path(self.runtime.environ["HOME"]))
        state.atomic_write(config_dir / "previous-key", b"not-a-token\n")
        problems, _info = self._report(set(self._expected()))
        self.assertTrue(problems)
        self.assertIn("gateway key slots unusable", problems[0])
        self.assertIn("previous-key", problems[0])
        self.assertNotIn("not-a-token", " ".join(problems))

    def test_stale_our_shaped_selector_is_info(self) -> None:
        served = set(self._expected()) | {"claude-multi-retired-max"}
        problems, info = self._report(served)
        self.assertEqual(problems, [])
        self.assertEqual(len(info), 1)
        self.assertIn("claude-multi-retired-max", info[0])

    def test_registry_extras_are_never_problems_only_discovery_info(self) -> None:
        # Extras are never drift or damage; doctor counts
        # the unique routable, uncataloged ids as Info (no registry in this
        # fixture HOME, so they are unattributed — never by name prefix).
        served = set(self._expected()) | {"claude-sonnet-4-5", "gpt-5.6-codex-mini"}
        problems, info = self._report(served)
        self.assertEqual(problems, [])
        self.assertEqual(info, [
            "discovery: 2 routable, uncataloged model IDs — claude-multi models --candidates",
            "discovery unattributed: 2 routable, uncataloged; locally registered only, upstream access unverified",
        ])

    def test_non_200_is_one_info_line(self) -> None:
        with unittest.mock.patch.object(
            claude_multi.launch, "served_models", return_value=(None, 500)
        ):
            problems, info = cli._doctor_served_report(self.runtime, "t" * 64)
        self.assertEqual(problems, [])
        self.assertEqual(len(info), 1)
        self.assertIn("non-200", info[0])

    def test_connection_failure_reports_a_transient_skip(self) -> None:
        # Readiness passed but the models probe failed (a restart in
        # flight): the radar must SAY it never ran — silence reads as clean.
        with unittest.mock.patch.object(
            claude_multi.launch,
            "served_models",
            side_effect=claude_multi.launch.LaunchError("connection refused"),
        ):
            problems, info = cli._doctor_served_report(self.runtime, "t" * 64)
        self.assertEqual(problems, [])
        self.assertEqual(len(info), 1)
        self.assertIn("changed state after the readiness check", info[0])

    def test_expected_covers_fixture_secret_omissions(self) -> None:
        # The fixture secret file holds only the Kimi key: qwen selectors
        # are not rendered, so they are not expected either.
        expected = self._expected()
        self.assertIn("claude-multi-kimi-k3", expected)
        self.assertNotIn("claude-multi-qwen38-max", expected)
        self.assertIn("claude-multi-opus-5", expected)
        # Aliases only: upstream wire names are NOT served (verified live
        # against a disposable loopback proxy during review).
        self.assertNotIn("k3", expected)


class ImprovementBatchTests(CLITestCase):
    """2.13.1: pins for the multi-lens improvement batch."""


    # -- line mode: h runs doctor ------------------------------------------
    # -- models command ----------------------------------------------------
    def test_models_lists_wire_and_typed_selectors(self) -> None:
        code, out = self.run_cli(["models"], interactive=False)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        lcat = self.runtime.lineup_catalog()
        # One entry per merged v2 line, sorted, then the retired
        # keys; each line is followed by its typed selectors, joined in
        # sorted order. The expected set comes from the v2
        # line (catalog.line_selectors), the set _line_selectors prints.
        heads = [line.split("\t", 1)[0] for line in lines if not line.startswith("  ")]
        self.assertEqual(heads, sorted(lcat.lines) + sorted(lcat.retired))
        self.assertNotIn("not in default", out)
        for model_id, model in lcat.lines.items():
            head = lines.index(next(l for l in lines if l.startswith(f"{model_id}\t")))
            self.assertIn(f"wire={model['wire_model']}", lines[head])
            self.assertEqual(
                lines[head + 1],
                "  in-session: " + " · ".join(
                    f"/model {s}" for s in sorted(
                        {s for _e, s, _c in catalog.line_selectors(model)}
                    )
                ),
            )
        # A two-lane model lists both lane selectors on one line.
        self.assertIn(
            "in-session: /model gpt-multi-sol-high[1m] · /model gpt-multi-sol-xhigh[1m]",
            out,
        )

    # -- discover command ---------------------------------------------------
    # A listing is one consented request through the injected
    # transport (the guard's terminal probe is patched; no request leaves).
    def _discover(self, provider, entries, *, shape="anthropic", text="y\n"):
        if shape == "anthropic":
            data = [{k: v for k, v in entry.items() if k != "think_efforts"}
                    | ({"think_efforts": {"valid_efforts": entry["think_efforts"]}}
                       if entry.get("think_efforts") else {}) for entry in entries]
        else:
            data = [dict(entry) for entry in entries]
        self.sent = []

        def transport(url, headers, *, deadline, max_bytes):
            self.sent.append(url)
            return json.dumps({"data": data}).encode()

        self.runtime.listing_transport = transport
        with unittest.mock.patch("claude_multi.cli.consent.stdio_ttys", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            return self.run_cli(["discover", provider], text)

    def test_discover_marks_cataloged_and_candidates(self) -> None:
        entries = [
            {"id": "k3", "display_name": "K3", "context_length": 1048576,
             "think_efforts": ["low", "high", "max"]},
            {"id": "k4", "display_name": "K4", "context_length": 524288},
        ]
        code, out = self._discover("kimi", entries)
        self.assertEqual(code, 0)
        self.assertIn("k3\tcataloged as kimi-k3 context=1048576 efforts=low,high,max\n", out)
        self.assertIn("k4\tcandidate context=524288\n", out)
        self.assertEqual(self.sent, ["https://api.kimi.com/coding/v1/models"])
        # The old custom add-model hint is gone (declare with --add).
        self.assertNotIn("custom add-model", out)

    def test_discover_prints_the_created_stamp(self) -> None:
        # `created` (unix seconds or an RFC 3339
        # string) survives into the discover output as a date.
        entries = [
            {"id": "k4", "display_name": "K4", "context_length": None, "created": 1790035200},
            {"id": "k5", "display_name": "K5", "context_length": None, "created_at": "2026-09-01T00:00:00Z"},
            {"id": "k6", "display_name": "K6", "context_length": None},
        ]
        code, out = self._discover("kimi", entries)
        self.assertEqual(code, 0)
        lines = {line.split("\t")[0]: line for line in out.splitlines()}
        self.assertTrue(lines["k4"].endswith(" created=2026-09-22"), lines["k4"])
        self.assertTrue(lines["k5"].endswith(" created=2026-09-01T00:00:00Z"), lines["k5"])
        self.assertNotIn("created=", lines["k6"])

    def test_discover_marks_every_provider_wire_cataloged_redirect_not(self) -> None:
        # Every cataloged wire of the listed provider is marked with its
        # catalog id; a lookalike redirect id is reported, never adopted;
        # a wire cataloged under ANOTHER provider is not this listing's entry.
        models = self.runtime.catalog.lines
        providers = self.runtime.catalog.docs["providers"]["providers"]
        by_provider: dict[str, dict[str, str]] = {}
        for model_id, model in sorted(models.items()):
            by_provider.setdefault(model["provider"], {}).setdefault(model["wire_model"], model_id)
        listable = [p for p in sorted(by_provider) if providers[p]["transport"]["kind"] == "direct"
                    and claude_multi.proxy._LISTING_SUPPORT.get(p, {}).get("status") != "unsupported"]
        provider = max(listable, key=lambda p: len(by_provider[p]))
        wires = by_provider[provider]
        foreign_wire = next(model["wire_model"] for model in models.values()
                            if model["provider"] != provider and model["wire_model"] not in wires)
        redirect = sorted(wires)[0] + "-redirect"
        entries = [{"id": wire, "display_name": "", "context_length": None} for wire in sorted(wires)] + [
            {"id": redirect, "display_name": "", "context_length": None},
            {"id": foreign_wire, "display_name": "", "context_length": None},
        ]
        shape = claude_multi.proxy._LISTING_SUPPORT.get(provider, {}).get("shape", "anthropic")
        code, out = self._discover(provider, entries, shape=shape)
        self.assertEqual(code, 0)
        for wire, model_id in wires.items():
            self.assertIn(f"{wire}\tcataloged as {model_id}\n", out)
        self.assertIn(f"{redirect}\tcandidate\n", out)
        self.assertIn(f"{foreign_wire}\tcandidate\n", out)

    def test_discover_unknown_provider_is_refused(self) -> None:
        with unittest.mock.patch("claude_multi.cli.consent.stdio_ttys", return_value=True):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, _out = self.run_cli(["discover", "nope"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("unknown provider 'nope'", err.getvalue())

    def test_discover_qwen_reports_unsupported(self) -> None:
        with self.assertRaises(claude_multi.proxy.ProxyError):
            claude_multi.proxy.list_provider_models(
                "qwen",
                self.runtime.catalog.docs["providers"]["providers"],
                environ=self.runtime.environ,
            )
        code, _out = self._discover("qwen", [])
        self.assertEqual(code, 1)
        self.assertEqual(self.sent, [])


class CustomModelsTests(CLITestCase):
    """Custom providers & ordinary models registry + integration."""

    def _add_kimi_custom(self, model_id="k3-256k", context=262144):
        claude_multi.custom.add_model(
            self.runtime.environ,
            model_id,
            wire_model=model_id,
            provider="kimi",
            context_tokens=context,
            created_via="discover",
            catalog_providers=self.runtime.catalog.providers,
        )

    def test_registry_roundtrip_and_mode(self) -> None:
        claude_multi.custom.add_provider(
            self.runtime.environ,
            "my-lab",
            base_url="https://lab.example.com/apps/anthropic",
            auth_kind="bearer",
            secret_env="MY_LAB_API_KEY",
        )
        registry = claude_multi.custom.load_registry(self.runtime.environ)
        self.assertIn("my-lab", registry["providers"])
        path = claude_multi.custom.registry_path(self.runtime.environ)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(claude_multi.custom.remove_provider(self.runtime.environ, "my-lab"), (True, ()))

    def test_invalid_registry_fails_closed(self) -> None:
        path = claude_multi.custom.registry_path(self.runtime.environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, b'{"version": 1, "models": {"x": {}}, "providers": {}}')
        with self.assertRaises(claude_multi.custom.CustomModelsError):
            claude_multi.custom.load_registry(self.runtime.environ)

    def test_ordinary_docs_merges_but_catalog_never_sees_customs(self) -> None:
        self._add_kimi_custom()
        self.assertIn("k3-256k", self.runtime.ordinary_docs["models"]["models"])
        self.assertNotIn("k3-256k", self.runtime.catalog.lines)
        # Composition resolution is catalog-only: a custom id must fail there.
        # The 2.x resolver lives in tests/_v3.py.
        document = _v3.default_composition()
        document["slots"].append({"role": "cm-reviewer", "model": "k3-256k"})
        with self.assertRaises(ValueError):
            _v3.resolve_v1(self.runtime.catalog.docs, document)

    def test_doctor_flags_unapplied_custom_as_config_drift(self) -> None:
        self._add_kimi_custom()
        config_dir = claude_multi.proxy.config_dir(Path(self.runtime.environ["HOME"]))
        state.ensure_private_dir(config_dir)
        # On-disk config rendered WITHOUT the custom model → drift problem.
        state.atomic_write(config_dir / "config.yaml", b"old: true\n")
        with unittest.mock.patch.object(
            claude_multi.launch, "served_models", return_value=(set(), 200)
        ):
            problems, _info = cli._doctor_served_report(self.runtime, "t" * 64)
        self.assertTrue(any("claude-multi providers apply" in p for p in problems))

    def test_record_save_accepts_custom_profile(self) -> None:
        # The session schema must accept custom-<n> profiles,
        # else no custom ordinary session can ever be recorded.
        self._add_kimi_custom()
        loaded = self.save_prepared_v4(self.prepare_direct_v4("k3-256k"))
        self.assertEqual(loaded["lead_class"], "custom-262144")

    def test_doctor_census_accepts_custom_sessions(self) -> None:
        # The scope census must not flag a custom session.
        self._add_kimi_custom()
        prepared = self.prepare_direct_v4("k3-256k")
        self.save_prepared_v4(prepared)
        scope_mod.swap_scope(
            self.runtime.session_store.root,
            prepared.record["managed_id"],
            prepared.result.scope_plan,
        )
        problems, info, _attention = cli._collect_doctor_reports(self.runtime)
        joined = "\n".join(problems)
        self.assertNotIn("no longer", joined)
        self.assertNotIn("custom-262144", joined)
        self.assertIn(f"Scope: {prepared.record['managed_id'][:8]} OK (lineup generation 1).", info)

    def test_converge_repairs_custom_sessions(self) -> None:
        # Doctor --repair must converge custom sessions.
        from claude_multi import transition as transition_mod

        self._add_kimi_custom()
        prepared = self.prepare_direct_v4("k3-256k")
        self.save_prepared_v4(prepared)
        report = transition_mod.converge(
            self.runtime.session_store.root,
            self.runtime.session_store,
            prepared.record["managed_id"],
            runtime_parts=self.runtime.converge_parts(),
        )
        self.assertIn(
            "live scope was missing; recompiled from the record and the installed catalog",
            report,
        )

    def test_fetch_mark_rejects_catalog_id_shadowing(self) -> None:
        with self.assertRaises(claude_multi.custom.CustomModelsError):
            claude_multi.custom.add_model(
                self.runtime.environ,
                "sol",  # a catalog id — must never be shadowed
                wire_model="whatever-wire",
                provider="kimi",
                context_tokens=262144,
                created_via="discover",
                catalog_providers=self.runtime.catalog.providers,
                catalog_models=tuple(self.runtime.catalog.lines.keys()),
            )


class LaunchOptionsAndPruningTests(CLITestCase):
    """Launch options: w toggle, bare -r, --print-launch, candidate modes,
    relative age, prune coverage."""


    # B: bare -r ----------------------------------------------------------
    def test_bare_resume_prints_listing_when_piped(self) -> None:
        self.save_session()
        code, out = self.run_cli(["-r"], interactive=False)
        self.assertEqual(code, 0)
        self.assertIn(FIXED_ID[:8], out)
        self.assertIn("sessions", out)

    # C: --print-launch ----------------------------------------------------
    def test_print_launch_shows_argv_without_token(self) -> None:
        code, out = self.run_cli(
            ["--profile", "balanced", "--print-launch"], interactive=False
        )
        self.assertEqual(code, 0, out)
        self.assertIn("--add-dir", out)
        self.assertIn("--settings", out)
        self.assertIn("  unset ANTHROPIC_AUTH_TOKEN\n", out)
        self.assertNotIn("  set ANTHROPIC_AUTH_TOKEN", out)
        self.assertIn("no credential in process env", out)
        self.assertNotIn(FIXTURE_GATEWAY_TOKEN, out)
        self.assertEqual(len(self.launches), 0)

    def test_ambiguous_candidates_show_mode(self) -> None:
        self.save_session(session_id=FIXED_ID, mode="durable", scope_generation=2)
        self.save_session(
            session_id="97a6194a-1111-4222-8333-444455556666", mode="legacy"
        )
        with self.assertRaises(cli.CLIError) as ctx:
            cli._resolve_resume_target(self.runtime, "default")
        text = str(ctx.exception)
        self.assertIn("durable(g2)", text)
        self.assertIn("legacy", text)

    # E: relative age -------------------------------------------------------
    def test_record_age_buckets(self) -> None:
        from datetime import datetime, timezone, timedelta

        now = datetime(2026, 7, 22, 22, 0, 0, tzinfo=timezone.utc)
        def record_age_at(hours, minutes=0):
            stamp = (now - timedelta(hours=hours, minutes=minutes)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            return cli._record_age({"created_at": stamp}, now=now)

        self.assertEqual(record_age_at(0, 0), "just now")
        self.assertEqual(record_age_at(0, 45), "45m ago")
        self.assertEqual(record_age_at(5), "5h ago")
        self.assertEqual(record_age_at(96), "4d ago")
        self.assertEqual(
            cli._record_age({"created_at": "bogus"}, now=now), "bogus"
        )

    # F: prune covers lead prompts and locks --------------------------------
    def test_prune_removes_generated_files_of_forgotten_sessions(self) -> None:
        store = self.runtime.session_store
        gone = "9bc5fd42-d428-4545-97af-3eefcb05b9f2"
        alive = FIXED_ID
        self.save_session(session_id=alive)
        prompt_gone = store.root / f"lead-prompt-abcdef1234567890-{gone}.md"
        prompt_alive = store.root / f"lead-prompt-abcdef1234567890-{alive}.md"
        state.atomic_write(prompt_gone, b"x")
        state.atomic_write(prompt_alive, b"x")
        lock_gone = store.lifecycle_lock(gone)
        lock_gone.acquire(blocking=False)
        lock_gone.release()
        lock_alive = store.lifecycle_lock(alive)
        lock_alive.acquire(blocking=False)
        lock_alive.release()
        import io as _io

        code = cli._doctor_prune(self.runtime, _io.StringIO())
        self.assertEqual(code, 0)
        self.assertFalse(prompt_gone.exists())
        self.assertTrue(prompt_alive.exists())
        self.assertTrue(lock_gone.lock_path.exists())
        self.assertTrue(lock_alive.lock_path.exists())

    def test_prune_waits_for_session_lifecycle_lock(self) -> None:
        store = self.runtime.session_store
        gone = "9bc5fd42-d428-4545-97af-3eefcb05b9f2"
        orphan = scope_mod.scope_dir(store.root, gone)
        state.ensure_private_dir(orphan)
        lock = store.lifecycle_lock(gone)
        self.assertTrue(lock.acquire(blocking=False))
        done: list[int] = []
        thread = threading.Thread(
            target=lambda: done.append(cli._doctor_prune(self.runtime, io.StringIO()))
        )
        thread.start()
        thread.join(timeout=0.2)
        self.assertTrue(thread.is_alive())
        self.assertTrue(orphan.exists())
        lock.release()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(done, [0])
        self.assertFalse(orphan.exists())
        self.assertTrue(lock.lock_path.exists())


class PickerIntentThreadingTests(CLITestCase):
    def test_print_launch_reports_helper_only_auth(self) -> None:
        self.save_v4_session()
        code, out = self.run_cli(
            ["-r", FIXED_ID, "--print-launch"], "\n", interactive=True
        )
        self.assertEqual(code, 0, out)
        self.assertNotIn("launch mode:", out)
        self.assertIn("record: profile balanced · follow yes · lineup generation 1", out)
        self.assertIn("gateway auth: scope apiKeyHelper", out)
        self.assertNotIn("  set ANTHROPIC_AUTH_TOKEN", out)
        for key in claude_multi.catalog.CREDENTIAL_ENV_KEYS:
            self.assertIn(f"  unset {key}\n", out)

    def test_direct_print_launch_reports_credential_unsets(self) -> None:
        model, _secret_ref = _keyless_ordinary_model(self.runtime)
        code, output = self.run_cli(
            ["direct", "--model", model, "--print-launch"], interactive=False
        )
        self.assertEqual(code, 0, output)
        self.assertIn("gateway auth: scope apiKeyHelper", output)
        self.assertNotIn("  set ANTHROPIC_AUTH_TOKEN", output)
        self.assertNotIn(FIXTURE_GATEWAY_TOKEN, output)
        for key in claude_multi.catalog.CREDENTIAL_ENV_KEYS:
            self.assertIn(f"  unset {key}\n", output)
        self.assertEqual(self.launches, [])

    def test_chooser_resume_threads_passthrough(self) -> None:
        # Bare -r's line chooser threads the claude-side tail.
        self.save_v4_session()
        code, out = self.run_cli(["-r", "--", "--verbose"], "1\n", interactive=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.launches), 1)
        prepared = self.launches[0]
        self.assertIn("--verbose", prepared.result.argv)
        self.assertTrue(prepared.result.durable)

class NativeDiscoveryTests(CLITestCase):
    def _fake_projects(self, session_ids):
        home = self.runtime.environ["HOME"]
        import os, pathlib

        projects = pathlib.Path(home) / ".claude" / "projects"
        projects.mkdir(parents=True, exist_ok=True)
        for slug, sid in session_ids:
            target = projects / slug
            target.mkdir(parents=True, exist_ok=True)
            (target / f"{sid}.jsonl").write_bytes(b"{}\n")

    def test_discovers_unmanaged_only(self) -> None:
        self.save_session()
        native_id = "aaaaaaaa-1111-4111-8111-111111111111"
        self._fake_projects([("-proj", FIXED_ID), ("-proj", native_id), ("-proj", "not-a-uuid")])
        found = cli._discover_native_sessions(self.runtime)
        self.assertEqual([item["session_id"] for item in found], [native_id])
        self.assertEqual(found[0]["slug"], "-proj")

    def test_duplicate_uuid_across_project_dirs_is_one_row(self) -> None:
        native_id = "aaaaaaaa-1111-4111-8111-111111111111"
        self._fake_projects([("-one", native_id), ("-two", native_id)])
        found = cli._discover_native_sessions(self.runtime)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["session_id"], native_id)
        self.assertEqual(found[0]["slugs"], ("-one", "-two"))
        self.assertEqual(found[0]["slug"], "(ambiguous)")

    def test_cwd_filter_applies_before_native_row_limit(self) -> None:
        native_id = "aaaaaaaa-1111-4111-8111-111111111111"
        current_slug = cli._native_project_slug(self.runtime.cwd)
        entries = [(current_slug, native_id)]
        for index in range(25):
            other = self.root / f"unrelated-{index}"
            other.mkdir()
            session_id = f"{index + 1:08x}-1111-4111-8111-{index + 1:012x}"
            entries.append((cli._native_project_slug(other), session_id))
        self._fake_projects(entries)
        projects = Path(self.runtime.environ["HOME"]) / ".claude" / "projects"
        os.utime(projects / current_slug / f"{native_id}.jsonl", (1, 1))
        for index, (slug, session_id) in enumerate(entries[1:], start=100):
            os.utime(projects / slug / f"{session_id}.jsonl", (index, index))
        found = cli._discover_native_sessions(
            self.runtime, limit=20, cwd_filter=self.runtime.cwd
        )
        self.assertEqual([item["session_id"] for item in found], [native_id])
        self.assertEqual(found[0]["cwd"], self.runtime.cwd)

    def test_managed_runtime_id_is_not_rediscovered_as_native(self) -> None:
        native_id = "aaaaaaaa-1111-4111-8111-111111111111"
        record = self.save_session()
        updated = self.runtime.session_store.reconcile_runtime(
            record["managed_id"],
            observed_runtime_id=native_id,
            source="resume",
            cwd=self.runtime.cwd,
        )
        self._fake_projects([("-proj", native_id)])
        self.assertEqual(updated["runtime_session_id"], native_id)
        self.assertEqual(cli._discover_native_sessions(self.runtime), [])

    def test_missing_projects_dir_is_empty(self) -> None:
        self.assertEqual(cli._discover_native_sessions(self.runtime), [])

class VersionConsistencyTests(unittest.TestCase):
    def test_package_version_matches_version_json(self) -> None:
        import json
        from claude_multi import __version__

        document = json.loads(
            (cli.default_asset_root() / "version.json").read_text(encoding="utf-8")
        )
        self.assertEqual(__version__, document["launcher_version"])

    def test_cli_version_flag_prints_the_release_identity(self) -> None:
        from claude_multi import identity

        parser = cli.build_parser()
        for argv in (["--version"], ["--line", "--version"]):
            with self.subTest(argv=argv):
                shown = io.StringIO()
                with self.assertRaises(SystemExit) as ctx, mock.patch.object(sys, "stdout", shown):
                    parser.parse_args(argv)
                self.assertEqual(ctx.exception.code, 0)
                self.assertEqual(shown.getvalue(), identity.version_line() + "\n")

    def test_module_entry_prints_the_launchers_identity_line(self) -> None:
        # python -m claude_multi.cli reaches the parser's --version (no
        # launcher shortcut): the same line as the launchers.
        from claude_multi import identity

        with tempfile.TemporaryDirectory() as home:
            for argv in (["--version"], ["--no-color", "--version"]):
                with self.subTest(argv=argv):
                    result = subprocess.run([sys.executable, "-B", "-m", "claude_multi.cli", *argv],
                                            env={"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                                 "PYTHONPATH": str(REPO_ROOT / "src")},
                                            capture_output=True, text=True, timeout=60, cwd=home)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, identity.version_line() + "\n")
                    self.assertIn(" (catalog ", result.stdout)  # the full line, not the bare version


class ProxyVersionConsistencyTests(unittest.TestCase):
    def test_proxy_version_matches_package(self) -> None:
        import io
        from claude_multi import __version__, proxy

        out = io.StringIO()
        with __import__("contextlib").redirect_stdout(out):
            proxy.main(["--version"])
        self.assertIn(__version__, out.getvalue())


class AdoptOriginalCwdTests(CLITestCase):
    def test_decode_project_slug_with_dashed_names(self) -> None:
        home = Path(self.runtime.environ["HOME"])
        project = home / "projects" / "occams-agent-flow"
        project.mkdir(parents=True)
        slug = cli._native_project_slug(project)
        self.assertEqual(cli._decode_project_slug(slug), project)
        self.assertIsNone(cli._decode_project_slug("-no-such-place-anywhere"))

    def test_native_project_slug_matches_pinned_client_vectors(self) -> None:
        self.assertEqual(cli._native_project_slug("/tmp/a-b_c.d"), "-tmp-a-b-c-d")
        self.assertEqual(cli._native_project_slug("/tmp/😀"), "-tmp---")
        self.assertEqual(cli._native_project_slug("a" * 200), "a" * 200)
        self.assertEqual(
            cli._native_project_slug("a" * 201), "a" * 200 + "-rkvsv5"
        )
        self.assertEqual(
            cli._native_project_slug("/" + "a" * 200),
            "-" + "a" * 199 + "-b6ymvl",
        )

    def test_decode_hidden_project_slug_and_explicit_cwd(self) -> None:
        home = Path(self.runtime.environ["HOME"])
        hidden = home / ".claude"
        hidden.mkdir(parents=True, exist_ok=True)
        slug = cli._native_project_slug(hidden)
        self.assertTrue(slug.endswith("--claude"), slug)
        self.assertEqual(cli._decode_project_slug(slug), hidden)

        native_id = "aaaaaaaa-1111-4111-8111-111111111111"
        project_dir = home / ".claude" / "projects" / slug
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / f"{native_id}.jsonl").write_bytes(b"not-read")
        self.assertEqual(
            cli._original_cwd_for_adopt(
                self.runtime, native_id, explicit_cwd=str(hidden)
            ),
            str(hidden),
        )
        wrong = home / "wrong"
        wrong.mkdir()
        with self.assertRaisesRegex(cli.CLIError, "does not contain native session"):
            cli._original_cwd_for_adopt(
                self.runtime, native_id, explicit_cwd=str(wrong)
            )

    def test_explicit_cwd_accepts_long_hashed_native_slug(self) -> None:
        home = Path(self.runtime.environ["HOME"])
        project = home / ("long-" + "a" * 190)
        project.mkdir(parents=True)
        slug = cli._native_project_slug(project)
        self.assertGreater(len(str(project)), 200)
        self.assertLessEqual(len(slug), 207)
        native_id = "cccccccc-3333-4333-8333-333333333333"
        project_dir = home / ".claude" / "projects" / slug
        project_dir.mkdir(parents=True)
        (project_dir / f"{native_id}.jsonl").write_bytes(b"not-read")
        self.assertEqual(
            cli._original_cwd_for_adopt(
                self.runtime, native_id, explicit_cwd=str(project)
            ),
            str(project),
        )

    def test_adopt_records_decoded_original_cwd(self) -> None:
        home = Path(self.runtime.environ["HOME"])
        project = home / "projects" / "real-project"
        project.mkdir(parents=True)
        slug = cli._native_project_slug(project)
        native_id = "aaaaaaaa-1111-4111-8111-111111111111"
        projects = home / ".claude" / "projects" / slug
        projects.mkdir(parents=True)
        (projects / f"{native_id}.jsonl").write_bytes(b"{}\n")
        resolved_cwd = cli._original_cwd_for_adopt(self.runtime, native_id)
        self.assertEqual(resolved_cwd, str(project))

    def test_adopt_without_local_metadata_fails_closed(self) -> None:
        with self.assertRaisesRegex(cli.CLIError, "cannot locate native session"):
            cli._original_cwd_for_adopt(
                self.runtime, "bbbbbbbb-2222-4222-8222-222222222222"
            )


class SessionNameWiringTests(CLITestCase):
    """--name carries the session's project basename end to end."""

    def test_fresh_managed_name_carries_project_basename(self) -> None:
        prepared = self.runtime.prepare(
            cli.LaunchTarget(
                "profile", self.runtime.profiles.load("balanced"), "balanced", True, "Profile"
            ),
            action="fresh",
            passthrough=[],
        )
        index = prepared.result.argv.index("--name")
        # The fixture runtime cwd is <root>/project.
        self.assertEqual(prepared.result.argv[index + 1], "cm:balanced@project")

    def test_resume_name_uses_recorded_cwd_not_current(self) -> None:
        record = self.save_v4_session()
        self.runtime.session_store.save({**record, "cwd": "/somewhere/project-x"})
        prepared = self.runtime.prepare(
            cli.LaunchTarget("record", None, "balanced", True, "Session"),
            action="resume",
            passthrough=[],
            session_id=FIXED_ID,
        )
        index = prepared.result.argv.index("--name")
        self.assertEqual(prepared.result.argv[index + 1], "cm:balanced@project-x")

    def test_direct_name_carries_project_basename(self) -> None:
        # A non-default direct lead (the fresh default is sol).
        prepared = self.prepare_direct_v4("qwen38")
        index = prepared.result.argv.index("--name")
        self.assertEqual(prepared.result.argv[index + 1], "cm:direct:qwen38@project")

class SessionEventAndDirectModeTests(CLITestCase):
    def test_session_start_reconciles_runtime_id_without_persisting_transcript(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        runtime_id = "22222222-2222-4222-8222-222222222222"
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": runtime_id,
                "source": "compact",
                "cwd": self.runtime.cwd,
                "model": "claude-fable-5[1m]",
                "transcript_path": "/must/not/be/read.jsonl",
            }
        ).decode("utf-8")
        code, output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID], payload
        )
        self.assertEqual(code, 0, output)
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(record["runtime_session_id"], runtime_id)
        self.assertEqual(record["runtime_aliases"][0]["session_id"], FIXED_ID)
        self.assertNotIn("transcript", strict_json.canonical_bytes(record).decode())

    def test_session_event_reads_stdin_without_opening_tty(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": FIXED_ID,
                "source": "startup",
                "cwd": self.runtime.cwd,
            }
        ).decode("utf-8")
        output = io.StringIO()
        with mock.patch('claude_multi.cli.streams._open_tty_streams', side_effect=AssertionError("must not open tty")
        ), mock.patch.object(sys, "stdin", io.StringIO(payload)):
            code = cli.main(
                ["session-event", "start", "--managed-id", FIXED_ID],
                runtime=self.runtime,
                output_stream=output,
                interactive=None,
            )
        self.assertEqual(code, 0, output.getvalue())

    def test_managed_fork_event_warns_and_preserves_parent_runtime(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        fork_id = "22222222-2222-4222-8222-222222222222"
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": fork_id,
                "source": "fork",
                "cwd": self.runtime.cwd,
            }
        ).decode("utf-8")
        code, output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID], payload
        )
        self.assertEqual(code, 0, output)
        self.assertIn("does not yet have an independent durable", output)
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(record["runtime_session_id"], FIXED_ID)
        self.assertEqual(record["pending_forks"][0]["session_id"], fork_id)

    def test_managed_model_mismatch_is_recorded_as_repair_needed(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": FIXED_ID,
                "source": "resume",
                "cwd": self.runtime.cwd,
                "model": "gpt-multi-sol-high[1m]",
            }
        ).decode("utf-8")
        code, output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID], payload
        )
        self.assertEqual(code, 0, output)
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(record["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)
        self.assertEqual(record["observed_model"], "gpt-multi-sol-high[1m]")
        response = strict_json.loads(output)
        context = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"claude-multi -r {FIXED_ID}", context)
        # The repair text is sessions.relink_message for every
        # record; the 2.x `sessions transition` remedy is gone.
        self.assertEqual(context, sessions.relink_message(record))
        self.assertNotIn("sessions transition", context)

    def test_managed_different_lane_is_not_treated_as_equivalent(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        record["snapshot"]["lead"]["model"] = "sol"
        record["snapshot"]["lead"]["client_selector"] = "gpt-multi-sol-high[1m]"
        record["composition_hash"] = strict_json.bundle_digest(record["snapshot"])
        _v3.save_any(self.runtime.session_store, record)
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": FIXED_ID,
                "source": "resume",
                "cwd": self.runtime.cwd,
                "model": "gpt-multi-sol-xhigh",
            }
        ).decode("utf-8")
        code, output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID], payload
        )
        self.assertEqual(code, 0, output)
        updated = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)
        self.assertEqual(updated["observed_model"], "gpt-multi-sol-xhigh")
        self.assertIn("differs from the recorded gpt-multi-sol-high[1m]", output)
        self.assertNotIn("sessions transition", output)

    def test_ordinary_hook_maps_selector_back_to_catalog_model(self) -> None:
        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd=self.runtime.cwd,
            model="qwen38",
            context_profile="large",
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
        )
        _v3.save_any(self.runtime.session_store, record)
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": FIXED_ID,
                "source": "compact",
                "cwd": self.runtime.cwd,
                "model": "claude-fable-5[1m]",
            }
        ).decode("utf-8")
        code, output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID], payload
        )
        self.assertEqual(code, 0, output)
        updated = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(updated["ordinary_model"], "fable")
        self.assertEqual(updated["context_profile"], "large")
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertNotIn("observed_model", updated)
        self.assertEqual(output, "")

    def test_ordinary_hook_accepts_wire_model_in_same_profile(self) -> None:
        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd=self.runtime.cwd,
            model="qwen38",
            context_profile="large",
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
        )
        _v3.save_any(self.runtime.session_store, record)
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": FIXED_ID,
                "source": "resume",
                "cwd": self.runtime.cwd,
                "model": "claude-fable-5",
            }
        ).decode("utf-8")
        code, output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID], payload
        )
        self.assertEqual(code, 0, output)
        updated = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(updated["ordinary_model"], "fable")
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertEqual(output, "")

    def test_ordinary_hook_accepts_canonical_opus_1m_selector(self) -> None:
        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd=self.runtime.cwd,
            model="qwen38",
            context_profile="large",
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
        )
        _v3.save_any(self.runtime.session_store, record)
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": FIXED_ID,
                "source": "compact",
                "cwd": self.runtime.cwd,
                "model": "claude-opus-4-8[1m]",
            }
        ).decode("utf-8")
        code, output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID], payload
        )
        self.assertEqual(code, 0, output)
        updated = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(updated["ordinary_model"], "opus")
        self.assertEqual(updated["context_profile"], "large")
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertNotIn("observed_model", updated)
        self.assertEqual(output, "")

    def test_ordinary_cross_profile_hook_fails_closed_with_warning(self) -> None:
        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd=self.runtime.cwd,
            model="qwen38",
            context_profile="large",
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
        )
        _v3.save_any(self.runtime.session_store, record)
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": FIXED_ID,
                "source": "resume",
                "cwd": self.runtime.cwd,
                "model": "claude-multi-grok46-xhigh",
            }
        ).decode("utf-8")
        code, output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID], payload
        )
        self.assertEqual(code, 0, output)
        updated = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(updated["ordinary_model"], "qwen38")
        self.assertEqual(updated["context_profile"], "large")
        self.assertEqual(updated["observed_model"], "claude-multi-grok46-xhigh")
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)
        context = strict_json.loads(output)["hookSpecificOutput"]["additionalContext"]
        # sessions.relink_message for every record version.
        self.assertEqual(context, sessions.relink_message(updated))
        self.assertIn("differs from the recorded qwen38", context)
        self.assertNotIn("claude-gateway -r", context)

    def test_ordinary_unknown_hook_model_fails_closed(self) -> None:
        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd=self.runtime.cwd,
            model="qwen38",
            context_profile="large",
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
        )
        _v3.save_any(self.runtime.session_store, record)
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": FIXED_ID,
                "source": "compact",
                "cwd": self.runtime.cwd,
                "model": "unknown-provider-model",
            }
        ).decode("utf-8")
        code, output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID], payload
        )
        self.assertEqual(code, 0, output)
        updated = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(updated["ordinary_model"], "qwen38")
        self.assertEqual(updated["observed_model"], "unknown-provider-model")
        self.assertEqual(updated["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)
        strict_json.loads(output)

    def test_direct_print_launch_has_no_composition_or_agents(self) -> None:
        code, output = self.run_cli(
            ["direct", "--model", "qwen38", "--print-launch"]
        )
        self.assertEqual(code, 0, output)
        self.assertIn("claude-multi-qwen38-max[1m]", output)
        self.assertIn("record: profile (ad-hoc direct) · follow no", output)
        self.assertNotIn("--agents", output)
        self.assertEqual(self.launches, [])

    def test_direct_resume_targets_recorded_runtime_uuid(self) -> None:
        runtime_id = "22222222-2222-4222-8222-222222222222"
        record = self.save_v4_session(target=cli.LaunchTarget("ad-hoc", claude_multi.profile.ad_hoc_direct("qwen38"), None, False, "Direct qwen38"))
        self.runtime.session_store.save({**record, "runtime_session_id": runtime_id})
        prepared = self.runtime.prepare(
            cli.LaunchTarget("record", None, None, False, "Session"),
            action="resume",
            passthrough=[],
            session_id=FIXED_ID,
        )
        self.assertEqual(prepared.result.argv[:2], ["--resume", runtime_id])
        self.assertEqual(prepared.record["managed_id"], FIXED_ID)
        self.assertIsNone(prepared.record["profile"])
        self.assertEqual(prepared.record["applied"]["lead"]["key"], "qwen38")
        self.assertEqual(prepared.record["launch_epoch"], 2)
        command = prepared.result.scope_plan.settings["hooks"]["SessionStart"][0][
            "hooks"
        ][0]["command"]
        self.assertIn("--launch-epoch 2", command)

    def test_direct_continue_reads_the_legacy_ordinary_pointer(self) -> None:
        # One merged per-cwd pointer. A 2.x `<d>.ordinary.json`
        # still counts: its record is newer than the `<d>.json` one, so
        # `direct -c` resumes it.
        self.save_v4_session()
        other = self.save_v4_session(session_id=OTHER_ID, target=cli.LaunchTarget("ad-hoc", claude_multi.profile.ad_hoc_direct("qwen38"), None, False, "Direct qwen38"))
        self.runtime.session_store.save({**other, "last_seen_at": "2099-01-01T00:00:00Z"})
        self.runtime.session_store.update_last(self.runtime.cwd, FIXED_ID)
        state.atomic_write(
            self.runtime.session_store._legacy_pointer_path(self.runtime.cwd),
            strict_json.canonical_file_bytes({
                "cwd": self.runtime.cwd,
                "session_id": OTHER_ID,
                "session_type": sessions.SESSION_TYPE_ORDINARY,
            }),
        )
        code, output = self.run_cli(["direct", "-c", "--print-launch"])
        # The fixture has no Qwen key: the plan prints and says it would fail.
        self.assertEqual(code, 1, output)
        self.assertIn("would fail: provider Qwen", output)
        self.assertIn("--resume\n", output)
        self.assertIn(OTHER_ID, output)

    def test_direct_implicit_resume_pins_the_recorded_lead_on_model_repair_state(self) -> None:
        # A model mismatch is repaired by the launcher resume itself
        # (the relink text says "resume through the launcher"): the plain
        # resume relaunches pinned to the recorded lead.
        record = self.save_v4_session(target=cli.LaunchTarget("ad-hoc", claude_multi.profile.ad_hoc_direct("qwen38"), None, False, "Direct qwen38"))
        self.runtime.session_store.save({
            **record,
            "identity_state": sessions.IDENTITY_REPAIR_NEEDED,
            "observed_model": "gpt-multi-sol-high[1m]",
        })
        prepared = self.runtime.prepare(
            cli.LaunchTarget("record", None, None, False, "Session"),
            action="resume",
            passthrough=[],
            session_id=FIXED_ID,
        )
        self.assertTrue(prepared.model_relaunch)
        self.assertEqual(
            prepared.result.argv[prepared.result.argv.index("--model") + 1],
            record["applied"]["lead"]["selector"],
        )

    def test_direct_explicit_model_repairs_with_pinned_relaunch(self) -> None:
        record = self.save_v4_session(target=cli.LaunchTarget("ad-hoc", claude_multi.profile.ad_hoc_direct("qwen38"), None, False, "Direct qwen38"))
        self.runtime.session_store.save({
            **record,
            "identity_state": sessions.IDENTITY_REPAIR_NEEDED,
            "observed_model": "gpt-multi-sol-high[1m]",
        })
        prepared = self.runtime.prepare(
            cli.LaunchTarget("record", None, None, False, "Session"),
            action="resume",
            passthrough=[],
            session_id=FIXED_ID,
            lead_override="sol",
        )
        self.assertTrue(prepared.model_relaunch)
        self.assertEqual(prepared.record["applied"]["lead"]["key"], "sol")
        self.assertEqual(prepared.record["lead_class"], "large")
        self.assertEqual(
            prepared.result.argv[prepared.result.argv.index("--model") + 1],
            prepared.record["applied"]["lead"]["selector"],
        )
        self.assertEqual(
            prepared.result.env_set["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "800000"
        )
        self.assertEqual(
            prepared.result.env_set["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "90"
        )

    def test_direct_pending_fork_blocks_even_with_explicit_model_repair(self) -> None:
        record = self.save_v4_session(target=cli.LaunchTarget("ad-hoc", claude_multi.profile.ad_hoc_direct("qwen38"), None, False, "Direct qwen38"))
        self.runtime.session_store.save({
            **record,
            "identity_state": sessions.IDENTITY_REPAIR_NEEDED,
            "observed_model": "gpt-multi-sol-high[1m]",
            "pending_forks": [
                {"session_id": OTHER_ID, "observed_at": "2026-07-22T00:00:00Z"}
            ],
        })
        with self.assertRaisesRegex(claude_multi.launch.LaunchError, "unresolved native fork"):
            self.runtime.prepare(
                cli.LaunchTarget("record", None, None, False, "Session"),
                action="resume",
                passthrough=[],
                session_id=FIXED_ID,
                lead_override="sol",
            )

    def test_managed_resume_refuses_cwd_repair_state(self) -> None:
        record = self.save_v4_session()
        self.runtime.session_store.save({
            **record,
            "identity_state": sessions.IDENTITY_REPAIR_NEEDED,
            "observed_cwd": "/wrong/project",
        })
        with self.assertRaisesRegex(claude_multi.launch.LaunchError, "relink-runtime"):
            self.runtime.prepare(
                cli.LaunchTarget("record", None, "balanced", True, "Session"),
                action="resume",
                passthrough=[],
                session_id=FIXED_ID,
            )

    def test_managed_model_only_repair_prepares_pinned_relaunch(self) -> None:
        record = self.save_v4_session()
        self.runtime.session_store.save({
            **record,
            "identity_state": sessions.IDENTITY_REPAIR_NEEDED,
            "observed_model": "gpt-multi-sol-high[1m]",
        })
        prepared = self.runtime.prepare(
            cli.LaunchTarget("record", None, "balanced", True, "Session"),
            action="resume",
            passthrough=[],
            session_id=FIXED_ID,
        )
        self.assertTrue(prepared.model_relaunch)
        self.assertIn("--model", prepared.result.argv)

class DoctorContractOverrideAttentionTests(CLITestCase):
    """An old-format contract override is ignored and named for removal."""

    def test_doctor_names_an_ignored_override(self) -> None:
        from claude_multi import state as state_mod

        config = sessions.config_root(self.runtime.environ)
        state_mod.ensure_private_dir(config)
        document = {"version": 1, "claude": {"validated_version": "9.9.9", "executable": {}}}
        state_mod.atomic_write(config / "native-contract.json", json.dumps(document).encode())
        self.runtime.reload_catalog()
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)
        self.assertIn("Attention", output)
        self.assertIn("contract override ignored: it pins Claude Code 9.9.9, newer than the", output)
        self.assertIn("rm ", output)
        self.assertNotIn("BLOCKED", output)


class ResolveForkCommandTests(CLITestCase):
    """`sessions resolve-fork`: the discard path for pending native forks."""

    def _forked(self):
        self.save_session(mode="durable", scope_generation=1)
        return self.runtime.session_store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd=self.runtime.cwd,
            now="2026-07-22T00:00:00Z",
        )

    def _stuck(self):
        record = self._forked()
        stuck = {
            **record,
            "runtime_session_id": OTHER_ID,
            "runtime_aliases": [
                {
                    "session_id": FIXED_ID,
                    "source": "fork",
                    "observed_at": "2026-07-22T00:01:00Z",
                }
            ],
        }
        _v3.save_any(self.runtime.session_store, stuck)
        return stuck

    def test_resolve_fork_discards_marker(self) -> None:
        self._forked()
        code, output, errors = self.run_cli_both(["sessions", "resolve-fork", FIXED_ID, OTHER_ID], "y\n")
        self.assertEqual(code, 0, output)
        self.assertIn(f"Discard the pending fork {OTHER_ID} of session {FIXED_ID}?", errors)
        self.assertIn("Resolved fork", output)
        self.assertIn("transcript stays", output)
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(record["pending_forks"], [])
        self.assertEqual(record["identity_state"], "authoritative")

    def _unchanged(self, before: bytes) -> None:
        self.assertEqual(self.runtime.session_store.load_raw(FIXED_ID)[1], before)
        self.assertEqual([item["session_id"] for item in self.runtime.session_store.load(FIXED_ID)["pending_forks"]],
                         [OTHER_ID])

    def test_a_terminal_asks_first_and_no_is_the_default(self) -> None:
        self._forked()
        before = self.runtime.session_store.load_raw(FIXED_ID)[1]
        for answer in ("\n", "n\n", ""):
            with self.subTest(answer=answer):
                code, output, errors = self.run_cli_both(["sessions", "resolve-fork", FIXED_ID, OTHER_ID], answer)
                self.assertEqual(code, 3, errors)
                self.assertIn("[y/N]", errors)
                self.assertIn(f"sessions link {OTHER_ID}", errors)  # how to adopt it later
                self.assertEqual(output, f"Nothing was changed: fork {OTHER_ID} is still pending.\n")
                self._unchanged(before)

    def test_off_a_terminal_it_is_refused_without_yes(self) -> None:
        self._forked()
        before = self.runtime.session_store.load_raw(FIXED_ID)[1]
        code, output, errors = self.run_cli_both(["sessions", "resolve-fork", FIXED_ID, OTHER_ID], interactive=False)
        self.assertEqual((code, output), (1, ""))
        self.assertIn("needs a confirmation: there is no terminal to ask in", errors)
        self.assertIn(f"fix: claude-multi sessions resolve-fork {FIXED_ID} {OTHER_ID} --yes", errors)
        self.assertNotIn("[y/N]", errors)
        self._unchanged(before)

    def test_yes_resolves_off_a_terminal_without_asking(self) -> None:
        epoch = self._forked().get("launch_epoch", 0)
        code, output, errors = self.run_cli_both(["sessions", "resolve-fork", FIXED_ID, OTHER_ID, "--yes"],
                                                 interactive=False)
        self.assertEqual(code, 0, errors)
        self.assertNotIn("[y/N]", errors)
        self.assertIn("Resolved fork", output)
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(record["pending_forks"], [])
        self.assertEqual(record["launch_epoch"], epoch + 1)

    def test_a_marker_it_cannot_discard_is_refused_before_the_question(self) -> None:
        self._forked()
        unknown = "33333333-3333-4333-8333-333333333333"
        code, _output, errors = self.run_cli_both(["sessions", "resolve-fork", FIXED_ID, unknown], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("no pending fork", errors)
        self.assertNotIn("[y/N]", errors)

    def test_resolve_fork_authority_holder_points_at_repair_all(self) -> None:
        self._stuck()
        code, output = self.run_cli_err(["sessions", "resolve-fork", FIXED_ID, OTHER_ID])
        self.assertEqual(code, 1)
        self.assertIn("doctor --repair-all", output)

    def test_resolve_fork_unknown_fork(self) -> None:
        self._forked()
        code, output = self.run_cli_err(
            ["sessions", "resolve-fork", FIXED_ID, "33333333-3333-4333-8333-333333333333"]
        )
        self.assertEqual(code, 1)
        self.assertIn("no pending fork", output)

    def test_blocked_resume_message_is_actionable(self) -> None:
        self._forked()
        code, output = self.run_cli_err(["-r", FIXED_ID], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn(OTHER_ID, output)
        self.assertIn("resolve-fork", output)
        self.assertIn("sessions link", output)


class DoctorPendingForkTests(CLITestCase):
    """A fork marker the live runtime already resolved: attention, not damage."""

    def _stuck(self):
        self.save_session(mode="durable", scope_generation=1)
        record = self.runtime.session_store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd=self.runtime.cwd,
            now="2026-07-22T00:00:00Z",
        )
        stuck = {
            **record,
            "runtime_session_id": OTHER_ID,
            "runtime_aliases": [
                {
                    "session_id": FIXED_ID,
                    "source": "fork",
                    "observed_at": "2026-07-22T00:01:00Z",
                }
            ],
        }
        _v3.save_any(self.runtime.session_store, stuck)
        _write_stand_in_scope(self.runtime, FIXED_ID)

    def test_stuck_marker_is_attention_not_blocked(self) -> None:
        self._stuck()
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)
        self.assertIn("already resolved", output)
        self.assertNotIn("BLOCKED", output)

    def test_repair_all_clears_the_marker(self) -> None:
        self._stuck()
        code, output = self.run_cli(["doctor", "--repair-all"])
        self.assertEqual(code, 0, output)
        self.assertIn("cleared a fork marker", output)
        self.assertEqual(
            self.runtime.session_store.load(FIXED_ID)["pending_forks"], []
        )
        code, output = self.run_cli(["doctor"])
        self.assertNotIn("already resolved", output)


class SessionsScreenForkLiveTests(CLITestCase):
    """Native fork discovery (``_discover_native_sessions``; not a screen test).

    The screen halves (⚠ marker, X resolution, ● marker, fork row) are in
    ``tests/test_screens_sessions.py``.
    """

    def _forked(self):
        self.save_session(mode="durable", scope_generation=1)
        return self.runtime.session_store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd=self.runtime.cwd,
            now="2026-07-22T00:00:00Z",
        )

    def test_native_fork_row_is_annotated(self) -> None:
        self._forked()
        home = Path(self.runtime.environ["HOME"])
        projects = home / ".claude" / "projects"
        target = projects / "-proj"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{OTHER_ID}.jsonl").write_bytes(b"{}\n")
        found = cli._discover_native_sessions(self.runtime)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["fork_of"], FIXED_ID)


class LiveBackgroundPrefixTests(unittest.TestCase):
    def test_socket_names_become_prefixes(self) -> None:
        import shutil as _shutil

        root = Path(tempfile.mkdtemp(prefix="cc-daemon-test-"))
        self.addCleanup(_shutil.rmtree, root, True)
        sock = root / "916a7da9" / "pty"
        sock.mkdir(parents=True)
        (sock / "707e80d4.sock").write_bytes(b"")
        (sock / "not-a-sock.txt").write_bytes(b"")
        (root / "916a7da9" / "spare").mkdir()
        self.assertEqual(cli._live_background_prefixes(root), frozenset({"707e80d4"}))

    def test_missing_root_is_empty(self) -> None:
        self.assertEqual(
            cli._live_background_prefixes(Path("/nonexistent-cc-daemon-root")),
            frozenset(),
        )


class RepinProgressTests(CLITestCase):
    """run_repin reports its phases live (no silent hang)."""

    def test_run_repin_reports_phases(self) -> None:
        import json as _json

        from claude_multi import upgrade as upgrade_mod
        from tests.test_upgrade import FAKE_SETTINGS_KEYS, FIXTURE_SCHEMA, fake_release

        home = Path(self.runtime.environ["HOME"])
        checkout = self.root / "checkout"
        resources = checkout / RESOURCES_RELATIVE  # the checkout keeps its resources in the package
        (checkout / "tests").mkdir(parents=True)
        (resources / "catalog").mkdir(parents=True)
        (resources / "schemas").mkdir()
        (resources / "schemas/native-contract.schema.json").write_bytes(FIXTURE_SCHEMA)
        contract = _json.loads(
            (CATALOG_ROOT / "catalog" / "native-contract.json").read_text()
        )
        contract["verified"][0]["version"] = "0.0.1"
        (resources / "catalog" / "native-contract.json").write_text(_json.dumps(contract))
        (resources / "version.json").write_text(
            _json.dumps({"version": 1, "launcher_version": "2.5.0", "catalog_version": 4})
        )
        versions = home / ".local" / "share" / "claude" / "versions"
        versions.mkdir(parents=True)
        candidate = versions / "9.9.9"
        candidate.write_bytes(
            b'#!/bin/sh\nif [ "$1" = "--help" ]; then echo "Claude Code"; else echo "9.9.9"; fi\n'
        )
        candidate.chmod(0o755)

        class _Done:
            returncode = 0
            stdout = "Ran 1 tests in 0.001s\nOK\n" + "\n".join(
                prefix + "fixture" for prefix in upgrade_mod.ESSENTIAL_EVIDENCE_PREFIXES
            ) + "\n"
            stderr = ""

        notes: list[str] = []
        outcome = upgrade_mod.run_repin(
            checkout_root=checkout,
            native_contract=contract,
            today="2026-07-27",
            runner=lambda *a, **k: _Done(),
            env={**self.runtime.environ, "PATH": str(self.root / "no-path")},
            progress=notes.append,
            running=upgrade_mod.ReleaseIdentity.from_root(resources),
            release_verifier=fake_release,
            settings_keys=FAKE_SETTINGS_KEYS,
        )
        self.assertEqual(outcome.kind, "prepared")
        self.assertTrue(any("evidence suite" in note for note in notes))
        self.assertTrue(any("the checkout pins Claude Code 9.9.9" in note for note in notes))


class SingleRepairConvergenceTests(CLITestCase):
    """A single --repair converges fork markers too."""

    def test_single_repair_clears_authority_holding_marker(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        record = self.runtime.session_store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd=self.runtime.cwd,
            now="2026-07-22T00:00:00Z",
        )
        stuck = {
            **record,
            "runtime_session_id": OTHER_ID,
            "runtime_aliases": [
                {
                    "session_id": FIXED_ID,
                    "source": "fork",
                    "observed_at": "2026-07-22T00:01:00Z",
                }
            ],
        }
        _v3.save_any(self.runtime.session_store, stuck)
        code, output = self.run_cli(["doctor", "--repair", FIXED_ID])
        self.assertEqual(code, 0, output)
        self.assertIn("cleared a fork marker", output)
        self.assertEqual(
            self.runtime.session_store.load(FIXED_ID)["pending_forks"], []
        )


class ForkHardeningBatchTests(CLITestCase):
    """Resume self-heal, ordinary fork context, X remaining."""

    def _stuck(self):
        self.save_session(mode="durable", scope_generation=1)
        record = self.runtime.session_store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd=self.runtime.cwd,
            now="2026-07-22T00:00:00Z",
        )
        stuck = {
            **record,
            "runtime_session_id": OTHER_ID,
            "runtime_aliases": [
                {
                    "session_id": FIXED_ID,
                    "source": "fork",
                    "observed_at": "2026-07-22T00:01:00Z",
                }
            ],
        }
        _v3.save_any(self.runtime.session_store, stuck)

    def test_noninteractive_resume_self_heals_authority_marker(self) -> None:
        self._stuck()
        self._write_transcript(OTHER_ID)
        code, output = self.run_cli(["-r", FIXED_ID], interactive=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(
            self.runtime.session_store.load(FIXED_ID)["pending_forks"], []
        )

    def test_ordinary_fork_context_uses_model_flag(self) -> None:
        import argparse as _argparse
        import io as _io
        import json as _json

        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd=self.runtime.cwd,
            model="sol",
            context_profile="sol",
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
            now="2026-07-21T00:00:00Z",
        )
        _v3.save_any(self.runtime.session_store, record)
        self.runtime.session_store.reconcile_runtime(
            FIXED_ID,
            observed_runtime_id=OTHER_ID,
            source="fork",
            cwd=self.runtime.cwd,
            now="2026-07-22T00:00:00Z",
        )
        out = _io.StringIO()
        args = _argparse.Namespace(
            event="start", managed_id=FIXED_ID, launch_epoch=1
        )
        cli._handle_session_event(
            self.runtime,
            args,
            input_stream=_io.StringIO(
                _json.dumps({"session_id": OTHER_ID, "source": "fork"})
            ),
            output_stream=out,
        )
        text = out.getvalue()
        # sessions.fork_adopt_command (v3 ordinary -> --direct KEY).
        self.assertIn("--direct sol", text)
        self.assertNotIn("--composition", text)


def _row_text(win, y: int) -> str:
    return "".join(
        win.grid.get((y, x), (" ", 0))[0] for x in range(win.width)
    ).rstrip()


class KeyBarExitNeverClippedTests(CLITestCase):
    """H8k: the exit binding survives any overflow."""

    def test_exit_visible_when_three_rows_needed(self) -> None:
        from test_tui import FakeWindow

        bar = tui.KeyBar(
            (
                ("R", "resume"), ("T", "switch comp"), ("X", "resolve fork"),
                ("F", "forget"), ("L", "adopt"), ("C", "cwd filter"),
                ("?", "help"), ("Esc", "quit"),
            )
        )
        win = FakeWindow((), height=10, width=40)
        bar.draw(win, 9, tui.MONO_PALETTE)
        all_text = _row_text(win, 8) + _row_text(win, 9)
        self.assertIn("Esc quit", all_text)


class SelectListFooterReservationTests(CLITestCase):
    """A wrapped footer never overdraws the message row."""

    def test_message_survives_wrapped_footer(self) -> None:
        from test_tui import FakeWindow

        select = tui.SelectList(
            "pick",
            [tui.SelectItem("one"), tui.SelectItem("two")],
            footer=(("Enter", "choose"), ("A", "action-one"),
                    ("B", "action-two"), ("Esc", "back")),
        )
        win = FakeWindow((), height=12, width=42)
        select.message = "something happened"
        select.draw(win, tui.MONO_PALETTE)
        self.assertIn("something happened", _row_text(win, 12 - 1 - select.footer.rows(42)))
        bottom = _row_text(win, 10) + _row_text(win, 11)
        self.assertIn("Esc back", bottom)


class ModalTinyTerminalTests(CLITestCase):
    """Tiny terminals never see wrong (negative-sliced) body lines."""

    def test_tiny_modal_shows_buttons_not_wrong_lines(self) -> None:
        from test_tui import FakeWindow

        modal = tui.Modal(
            "title",
            ["first-body-line", "second-body-line", "third-body-line"],
            buttons=(("OK", True),),
        )
        win = FakeWindow((), height=5, width=40)
        modal.draw(win, tui.MONO_PALETTE)
        text = win.text()
        self.assertIn("OK", text)
        self.assertNotIn("third-body-line", text)


class TransitionScreenLayoutTests(CLITestCase):
    """Diff lines start below the separator; indicator above the bar."""

    def test_first_diff_line_below_separator(self) -> None:
        from test_tui import FakeWindow

        screen = cli._TransitionScreen(
            ["first-diff-line", "second-diff-line"], palette=tui.MONO_PALETTE
        )
        win = FakeWindow((), height=12, width=60)
        screen._draw(win)
        self.assertEqual(_row_text(win, 2).strip(), "─" * 57)
        self.assertIn("first-diff-line", _row_text(win, 3))


class BrokenOverrideDegradationTests(CLITestCase):
    """An unreadable override is never applied — and never bricks the CLI."""

    def _broken_runtime(self):
        config = sessions.config_root(self.runtime.environ)
        state.ensure_private_dir(config)
        state.atomic_write(config / "native-contract.json", b'{"claude": {bad json')
        return cli.Runtime(
            asset_root=CATALOG_ROOT,
            environ=self.runtime.environ,
            cwd=self.runtime.cwd,
            launch_callback=self.runtime.launch_callback,
            doctor_callback=lambda _runtime: [],
            doctor_binary_callback=lambda _contract: ([], ["fixture binary ok"]),
            doctor_daemon_callback=lambda: claude_multi.launch.DaemonStatus(
                state="absent", summary="fixture daemon absent"
            ),
            **self._runtime_seams(),
        )

    def test_runtime_ignores_it_and_doctor_names_it(self) -> None:
        runtime = self._broken_runtime()
        self.assertIsNone(runtime.broken_override_error)
        self.assertEqual(runtime.catalog.contract_source, "override-ignored-invalid")
        saved, self.runtime = self.runtime, runtime
        try:
            code, output = self.run_cli(["doctor"])
        finally:
            self.runtime = saved
        self.assertEqual(code, 0, output)
        self.assertIn("contract override ignored: it cannot be used", output)
        self.assertIn("rm ", output)
        self.assertNotIn("claude-multi update", output)

    def test_update_never_touches_the_override(self) -> None:
        runtime = self._broken_runtime()
        override = sessions.config_root(self.runtime.environ) / "native-contract.json"
        before = override.read_bytes()
        saved, self.runtime = self.runtime, runtime
        try:
            code, output = self.run_cli(["update"], interactive=False)
        finally:
            self.runtime = saved
        self.assertEqual(code, 1, output)  # not an installed release: instructions, no change
        self.assertIn("Nothing was changed", output)
        self.assertEqual(override.read_bytes(), before)


class LegacyOverrideCleanupTests(CLITestCase):
    """A pre-3.0 contract override is removed at Runtime init.

    The 3.0 native contract splits ``effort_vocabulary`` into
    ``agent_efforts``/``lead_efforts``; a 2.x override can never load again,
    so it is removed unconditionally (whatever version it pins) with a
    one-shot notice and a doctor Attention line. Anything unsafe to touch
    (held lock, symlink, unsafe directory) keeps the degrade path, and
    nothing ever raises out of Runtime init.
    """

    def setUp(self) -> None:
        super().setUp()
        self.config = sessions.config_root(self.runtime.environ)
        state.ensure_private_dir(self.config)
        self.override = self.config / "native-contract.json"
        self.packaged_version = claude_multi.pin.version(self.runtime.catalog.docs["native-contract"])

    def _legacy_document(self, version: str) -> dict:
        document = copy.deepcopy(self.runtime.catalog.docs["native-contract"])
        del document["verified"]
        document["version"] = 1
        document["claude"] = {"validated_version": version}
        document["lifecycle_evidence"]["inspected_version"] = version
        del document["agent_efforts"]
        del document["lead_efforts"]
        document["effort_vocabulary"] = {
            "evidence": "2.x single effort vocabulary (fixture).",
            "status": "provisionally-trusted",
            "values": ["high", "xhigh", "max", "ultracode"],
        }
        return document

    def _write(self, document: dict, path: Path | None = None) -> Path:
        target = path or self.override
        state.atomic_write(target, strict_json.pretty_file_bytes(document))
        return target

    def _runtime(self, **kwargs) -> cli.Runtime:
        return cli.Runtime(
            asset_root=CATALOG_ROOT,
            environ=self.runtime.environ,
            cwd=self.runtime.cwd,
            launch_callback=self.runtime.launch_callback,
            doctor_callback=lambda _runtime: [],
            doctor_binary_callback=lambda _contract: ([], ["fixture binary ok"]),
            doctor_daemon_callback=lambda: claude_multi.launch.DaemonStatus(
                state="absent", summary="fixture daemon absent"
            ),
            **self._runtime_seams(),
            **kwargs,
        )

    def _doctor(self, runtime: cli.Runtime) -> tuple[int, str, str]:
        saved, self.runtime = self.runtime, runtime
        stderr = io.StringIO()
        try:
            with mock.patch.object(sys, "stderr", stderr):
                code, output = self.run_cli(["doctor"])
        finally:
            self.runtime = saved
        return code, output, stderr.getvalue()

    def _assert_degraded(self, runtime: cli.Runtime) -> None:
        self.assertIsNone(runtime.contract_notice)
        self.assertIsNotNone(runtime.broken_override_error)
        self.assertEqual(runtime.catalog.contract_source, "packaged")

    def test_legacy_override_removed_whatever_its_version(self) -> None:
        major, minor, patch = (int(part) for part in self.packaged_version.split("."))
        for label, version in (
            ("older", f"{major}.{minor}.{max(patch - 1, 0)}"),
            ("equal", self.packaged_version),
            ("newer", f"{major}.{minor}.{patch + 1}"),
        ):
            with self.subTest(version=label):
                self._write(self._legacy_document(version))
                runtime = self._runtime()
                self.assertFalse(os.path.lexists(self.override))
                self.assertIsNone(runtime.broken_override_error)
                self.assertEqual(runtime.catalog.contract_source, "packaged")
                self.assertEqual(
                    runtime.contract_notice,
                    f"native contract override (claude {version}) used an older "
                    "effort vocabulary and was removed; the packaged contract "
                    f"(claude {self.packaged_version}) is in effect",
                )
                code, output, stderr = self._doctor(runtime)
                self.assertIn("contract override removed: native contract override", output)
                self.assertNotIn("invalid and was IGNORED", output)
                # main prints the notice once per Runtime, to stderr.
                self.assertEqual(stderr.count("older effort vocabulary"), 1)
                _code, _output, stderr_again = self._doctor(runtime)
                self.assertEqual(stderr_again, "")

    def test_non_legacy_override_is_ignored_with_attention(self) -> None:
        document = copy.deepcopy(self.runtime.catalog.docs["native-contract"])
        document["recorded_at"] = "not-a-date"
        self._write(document)
        runtime = self._runtime()
        self.assertTrue(self.override.exists())
        self.assertIsNone(runtime.broken_override_error)
        self.assertEqual(runtime.catalog.contract_source, "override-ignored-invalid")
        code, output, stderr = self._doctor(runtime)
        self.assertEqual(code, 0, output)
        self.assertIn("contract override ignored", output)
        self.assertNotIn("contract override removed", output)
        self.assertEqual(stderr, "")

    def test_held_lock_skips_removal(self) -> None:
        self._write(self._legacy_document("9.9.9"))
        lock = state.FileLock(self.override)
        self.assertTrue(lock.acquire(blocking=False))
        try:
            runtime = self._runtime()
        finally:
            lock.release()
        self.assertTrue(self.override.exists())
        self._assert_degraded(runtime)

    def test_symlinked_override_not_removed(self) -> None:
        real = self._write(self._legacy_document("9.9.9"), self.config / "real-contract.json")
        self.override.symlink_to(real)
        runtime = self._runtime()
        self.assertTrue(self.override.is_symlink())
        self.assertTrue(real.exists())
        # Never read through the link and never applied: ignored with Attention.
        self.assertIsNone(runtime.broken_override_error)
        self.assertEqual(runtime.catalog.contract_source, "override-ignored-invalid")

    def test_state_writes_disallowed_skips_removal(self) -> None:
        self._write(self._legacy_document("9.9.9"))
        runtime = self._runtime(allow_state_writes=False)
        self.assertTrue(self.override.exists())
        self._assert_degraded(runtime)

    def test_group_readable_config_dir_never_raises_from_the_cleanup(self) -> None:
        # The lock's directory check raises StateError on a 0755 config dir;
        # the cleanup maps it to "nothing removed" (the degrade path). The
        # Runtime itself still refuses such a config dir later, in the
        # composition store — so the helper is
        # exercised directly here.
        self._write(self._legacy_document("9.9.9"))
        os.chmod(self.config, 0o755)
        self.addCleanup(os.chmod, self.config, 0o700)
        packaged = self.runtime.catalog.docs["native-contract"]
        self.assertIsNone(
            claude_multi.catalog.remove_legacy_contract_override(self.override, packaged=packaged)
        )
        self.assertTrue(self.override.exists())

    def test_read_only_config_dir_degrades_without_raising(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory permissions")
        self._write(self._legacy_document("9.9.9"))
        os.chmod(self.config, 0o500)
        self.addCleanup(os.chmod, self.config, 0o700)
        runtime = self._runtime()
        self.assertTrue(self.override.exists())
        self._assert_degraded(runtime)

    def test_main_passes_allow_state_writes_per_command_and_marker(self) -> None:
        # cli.main builds the single Runtime for every command: session-event,
        # a newer state marker and every read-only report (plain doctor
        # included) disallow the cleanup.
        state_root = self.root / "main-state"
        state.ensure_private_dir(state_root)

        class _Stop(Exception):
            pass

        captured: list[dict] = []

        def fake_runtime(**kwargs):
            captured.append(kwargs)
            raise _Stop

        cases = (
            (["doctor", "--repair-all"], None, True),
            (["doctor"], None, False),
            (["session-event", "start", "--managed-id", FIXED_ID], None, False),
            (["sessions", "list"], "99\n", False),
        )
        for argv, marker, expected in cases:
            with self.subTest(argv=argv, marker=marker):
                captured.clear()
                marker_path = state_root / "state-version"
                if marker is None:
                    marker_path.unlink(missing_ok=True)
                else:
                    marker_path.write_text(marker, encoding="ascii")
                    marker_path.chmod(0o600)
                with mock.patch.dict(os.environ, {"HOME": str(state_root.parent),
                                                  "XDG_CONFIG_HOME": str(state_root.parent / "config"),
                                                  "XDG_STATE_HOME": str(state_root.parent / "state"),
                                                  "XDG_DATA_HOME": str(state_root.parent / "data")}), \
                        mock.patch.object(claude_multi.sessions, "state_root", return_value=state_root), \
                        mock.patch('claude_multi.cli.runtime.Runtime', side_effect=fake_runtime), \
                        mock.patch('claude_multi.assets.default_asset_root', return_value=CATALOG_ROOT):
                    with self.assertRaises(_Stop):
                        cli.main(
                            argv,
                            input_stream=io.StringIO(""),
                            output_stream=io.StringIO(),
                            interactive=False,
                        )
                self.assertEqual(len(captured), 1)
                self.assertIs(captured[0]["allow_state_writes"], expected)


class SessionsStopTests(CLITestCase):
    """`sessions stop` + the picker's E action (upstream stop primitive)."""

    def _fixture_contract(self):
        import hashlib as _hashlib

        platform = claude_multi.pin.host_platform()
        binary = claude_multi.pin.owned_path(self.runtime.environ, "2.1.999", platform)
        state.ensure_private_dir(binary.parent)
        binary.write_bytes(b"#!/bin/sh\n")
        binary.chmod(0o755)
        self.fixture_binary = binary
        return {**self.runtime.catalog.docs["native-contract"], "verified": [{
            "version": "2.1.999",
            "platforms": {platform: {"sha256": _hashlib.sha256(binary.read_bytes()).hexdigest(),
                                     "size": binary.stat().st_size}},
            "manifest_sha256": "1" * 64, "signature_sha256": None,
            "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE",
            "verified_at": "2026-07-27", "evidence": {platform: "battery", "receipt_sha256": "2" * 64},
        }]}

    def _runtime_with_fixture_contract(self):
        docs = self.runtime.catalog.docs
        original = docs["native-contract"]
        docs["native-contract"] = self._fixture_contract()
        self.addCleanup(docs.__setitem__, "native-contract", original)

    def _runner(self, captured):
        def run(argv, **kwargs):
            captured.append((list(argv), kwargs.get("env")))
            return subprocess.CompletedProcess(argv, 0, "stopped\n", "")
        return run

    def test_stop_happy_path_invokes_verified_binary(self) -> None:
        self._runtime_with_fixture_contract()
        self.save_session(mode="durable", scope_generation=1)
        captured = []
        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset({"11111111"})), \
             mock.patch("subprocess.run", side_effect=self._runner(captured)):
            code, output = self.run_cli(
                ["sessions", "stop", FIXED_ID, "--yes"], interactive=False
            )
        self.assertEqual(code, 0, output)
        argv, env = captured[0]
        self.assertEqual(argv[1:], ["stop", FIXED_ID])
        # The scrubbed env is the only barrier between the gateway
        # token/CLAUDE_MULTI_SECRET_ENV and the subprocess.
        self.assertEqual(set(env), {"PATH", "HOME"})
        self.assertIn("Stopped session", output)
        self.assertIn("conversation is kept", output)

    def test_stop_refuses_when_not_live(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset()):
            code, output = self.run_cli_err(
                ["sessions", "stop", FIXED_ID, "--yes"], interactive=False
            )
        self.assertEqual(code, 1, output)
        self.assertIn("nothing to stop", output)

    def test_stop_refuses_self_stop(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = FIXED_ID
        try:
            with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset({"11111111"})
            ):
                code, output = self.run_cli_err(
                    ["sessions", "stop", FIXED_ID, "--yes"], interactive=False
                )
        finally:
            del self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"]
        self.assertEqual(code, 1, output)
        self.assertIn("running inside", output)

    def test_stop_noninteractive_requires_yes(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset({"11111111"})):
            code, output = self.run_cli_err(["sessions", "stop", FIXED_ID], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("--yes", output)

    def test_stop_confirmation_declined(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset({"11111111"})):
            code, output = self.run_cli_err(["sessions", "stop", FIXED_ID], "n\n")
        self.assertEqual(code, 3, output)
        self.assertIn("cancelled", output)

    def test_stop_upstream_failure_surfaces(self) -> None:
        self._runtime_with_fixture_contract()
        self.save_session(mode="durable", scope_generation=1)

        def failing(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 3, "", "no such session")

        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset({"11111111"})), \
             mock.patch("subprocess.run", side_effect=failing):
            code, output = self.run_cli_err(
                ["sessions", "stop", FIXED_ID, "--yes"], interactive=False
            )
        self.assertEqual(code, 1)
        self.assertIn("no such session", output)

    def test_transition_live_note_shown_when_target_live(self) -> None:
        self.save_v4_session()
        shifted = copy.deepcopy(self.runtime.profiles.load("balanced"))
        shifted["name"] = "shifted"
        shifted["lead"] = {"model": "sol", "effort": "xhigh"}
        self.runtime.profiles.save(shifted)
        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset({"11111111"})):
            code, output = self.run_cli(
                ["sessions", "transition", FIXED_ID, "--composition", "shifted"],
                "n\n",
            )
        self.assertIn("stop it first", output)
        self.assertIn("sessions stop", output)
        self.assertEqual(self.launches, [])


class SubagentModelRadarTests(CLITestCase):
    """Doctor radar: doctor flags settings that can alter roster routing."""

    def _write_user_settings(self, env: dict[str, str]) -> None:
        home = Path(self.runtime.environ["HOME"])
        settings = home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_bytes(strict_json.canonical_file_bytes({"env": env}))

    def test_doctor_attention_on_user_settings_default_override(self) -> None:
        self._write_user_settings(
            {"CLAUDE_CODE_SUBAGENT_MODEL": "gpt-multi-sol-high[1m]"}
        )
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL", output)
        # The stale precedence claim of an older client is gone.
        self.assertNotIn("pinned Claude 2.1.220", output)
        self.assertIn("becomes the model of workflow agents", output)
        # The placement probe: general-purpose follows it, Explore
        # and Plan do not (doctor may wrap the line; compare unwrapped).
        flat = " ".join(output.split())
        self.assertIn("and of native general-purpose agents", flat)
        self.assertIn("native Explore and Plan are not affected", flat)
        self.assertIn("workflow_default_binding", output)

    def test_doctor_attention_on_user_settings_force_override(self) -> None:
        self._write_user_settings({"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"})
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", output)
        self.assertIn("global override", output)
        self.assertIn("ignoring per-spawn and agent-definition models", output)

    def test_doctor_attention_on_project_local_force_override(self) -> None:
        settings = Path(self.runtime.cwd) / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_bytes(
            strict_json.canonical_file_bytes(
                {"env": {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "true"}}
            )
        )
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)
        self.assertIn(str(settings), output)
        self.assertIn("global override", output)

    def test_no_attention_without_override(self) -> None:
        code, output = self.run_cli(["doctor"])
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", output)


class ResumeGateTests(CLITestCase):
    """The resume gate — pure evaluation, perform backstop, modal flow."""

    def _gate(self, record, prefixes=frozenset()):
        return cli._evaluate_resume_gate(
            self.runtime, record, live_prefixes=prefixes
        )

    def _transcript(self, record):
        return (
            Path(self.runtime.environ["HOME"])
            / ".claude"
            / "projects"
            / cli._native_project_slug(record["cwd"])
            / f"{sessions.runtime_session_id(record)}.jsonl"
        )

    def _bare_runtime(self):
        return cli.Runtime(
            asset_root=CATALOG_ROOT,
            environ=self.runtime.environ,
            cwd=self.runtime.cwd,
            launch_callback=None,
            doctor_callback=lambda _runtime: [],
            **self._runtime_seams(),
        )

    def _prepared(self, record, *, kind="resume"):
        import types

        action = (
            claude_multi.compiler.build_resume(
                sessions.managed_id(record), sessions.runtime_session_id(record)
            )
            if kind == "resume"
            else claude_multi.compiler.build_fresh(sessions.managed_id(record))
        )
        result = types.SimpleNamespace(session_action=action)
        return cli.PreparedLaunch(result, record, None, None)

    # -- evaluator branches --------------------------------------------------

    def test_gate_ok_for_healthy_record(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        self.assertEqual(self._gate(record).kind, "ok")

    def test_gate_repair_needed_carries_relink_message(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        record["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        record["observed_cwd"] = "/wrong/project"
        _v3.save_any(self.runtime.session_store, record)
        gate = self._gate(record)
        self.assertEqual(gate.kind, "repair-needed")
        text = " ".join(gate.lines)
        self.assertIn("relink-runtime", text)
        self.assertIn(FIXED_ID, text)
        self.assertIn("--cwd /wrong/project", text)
        self.assertIn(("repair-resume", "Repair & resume"), gate.actions)

    def test_gate_refusal_text_never_splits_commands(self) -> None:
        # The refusal text is built from raw lines, so
        # no path length can hyphen-split `relink-runtime` apart.
        record = self.save_session(mode="durable", scope_generation=1)
        record["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        record["observed_cwd"] = "/wrong/project-with-a-very-long-path-" + "x" * 80
        _v3.save_any(self.runtime.session_store, record)
        gate = self._gate(record)
        refusal = cli._resume_gate_refusal(gate)
        self.assertIn("relink-runtime", refusal)
        self.assertNotIn("relink- runtime", refusal)

    def test_gate_transcript_blocker_outranks_daemon_owned(self) -> None:
        # Live + missing transcript → transcript gate,
        # and force must not bypass it.
        record = self.save_session(mode="durable", scope_generation=1)
        self._transcript(record).unlink()
        gate = self._gate(record, prefixes=frozenset({FIXED_ID}))
        self.assertEqual(gate.kind, "transcript-missing")
        runtime = self._bare_runtime()
        with mock.patch.object(
            runtime, "_live_prefixes", return_value=frozenset({FIXED_ID})
        ):
            with self.assertRaises(cli.CLIError) as raised:
                runtime.perform(self._prepared(record), resume_decision="force")
        self.assertIn("Transcript not found", str(raised.exception))

    def test_gate_daemon_owned_offers_stop_and_force(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        gate = self._gate(record, prefixes=frozenset({FIXED_ID}))
        self.assertEqual(gate.kind, "daemon-owned")
        text = " ".join(gate.lines)
        self.assertIn(f"sessions stop {FIXED_ID}", text)
        self.assertIn("best-effort heuristic", text)
        values = [value for value, _label in gate.actions]
        self.assertEqual(values, ["stop-resume", "force"])

    def test_gate_transcript_missing_guides_restore_or_forget(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        self._transcript(record).unlink()
        gate = self._gate(record)
        self.assertEqual(gate.kind, "transcript-missing")
        text = " ".join(gate.lines)
        self.assertIn("never deletes transcripts", text)
        self.assertIn(f"sessions forget {FIXED_ID}", text)
        self.assertEqual(gate.actions, ())

    def test_gate_transcript_elsewhere_points_at_relink_cwd(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        stray = self._transcript(record)
        other_dir = stray.parent.parent / "-other-project"
        other_dir.mkdir(parents=True)
        stray.rename(other_dir / stray.name)
        gate = self._gate(record)
        self.assertEqual(gate.kind, "transcript-elsewhere")
        text = " ".join(gate.lines)
        self.assertIn("-other-project", text)
        self.assertIn("relink-runtime", text)

    # -- perform backstop ----------------------------------------------------

    def test_perform_refuses_daemon_owned_without_decision(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        runtime = self._bare_runtime()
        with mock.patch.object(
            runtime, "_live_prefixes", return_value=frozenset({FIXED_ID})
        ):
            with self.assertRaises(cli.CLIError) as raised:
                runtime.perform(self._prepared(record))
        self.assertIn(f"sessions stop {FIXED_ID}", str(raised.exception))

    def test_perform_force_bypasses_daemon_owned_only(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        runtime = self._bare_runtime()
        with mock.patch.object(
            claude_multi.launch, "perform_launch", return_value=0
        ) as perform_launch:
            with mock.patch.object(
                runtime, "_live_prefixes", return_value=frozenset({FIXED_ID})
            ):
                code = runtime.perform(
                    self._prepared(record), resume_decision="force"
                )
        self.assertEqual(code, 0)
        self.assertTrue(perform_launch.called)

    def test_perform_never_bypasses_transcript_missing(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        self._transcript(record).unlink()
        runtime = self._bare_runtime()
        with self.assertRaises(cli.CLIError) as raised:
            runtime.perform(self._prepared(record), resume_decision="force")
        self.assertIn("Transcript not found", str(raised.exception))

    def test_perform_ignores_fresh_actions(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        self._transcript(record).unlink()
        runtime = self._bare_runtime()
        with mock.patch.object(claude_multi.launch, "perform_launch", return_value=0):
            code = runtime.perform(self._prepared(record, kind="fresh"))
        self.assertEqual(code, 0)

    def test_sessions_show_sends_relink_message_to_stderr(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        record["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        record["observed_cwd"] = "/wrong/project"
        _v3.save_any(self.runtime.session_store, record)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, output = self.run_cli(["sessions", "show", FIXED_ID])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), self.runtime.session_store.load(FIXED_ID))
        self.assertIn("relink-runtime", err.getvalue())
        self.assertIn("--cwd /wrong/project", err.getvalue())

class ResumeOptionsAndAuthorityTests(CLITestCase):
    """Resume gates preserve direct-launch options and recorded authority."""

    def _ordinary(self, **overrides):
        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd=self.runtime.cwd,
            model="qwen38",
            context_profile="large",
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
        )
        for key, value in overrides.items():
            record[key] = value
        self.runtime.session_store.save(record)
        return record

    def test_direct_force_flag_equivalent_both_placements(self) -> None:
        parser = cli.build_parser()
        before = parser.parse_args(
            ["--force", "direct", "--resume", FIXED_ID]
        )
        after = parser.parse_args(
            ["direct", "--force", "--resume", FIXED_ID]
        )
        self.assertTrue(getattr(before, "force", False))
        self.assertTrue(getattr(after, "force", False))

    def test_transcript_elsewhere_decodes_real_project_path(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        stray = (
            Path(self.runtime.environ["HOME"])
            / ".claude"
            / "projects"
            / cli._native_project_slug(record["cwd"])
            / f"{FIXED_ID}.jsonl"
        )
        real_project = Path(self.runtime.environ["HOME"]) / "real-project"
        real_project.mkdir(parents=True)
        slug_dir = stray.parent.parent / cli._native_project_slug(real_project)
        slug_dir.mkdir(parents=True)
        stray.rename(slug_dir / stray.name)
        gate = cli._evaluate_resume_gate(self.runtime, record)
        self.assertEqual(gate.kind, "transcript-elsewhere")
        text = " ".join(gate.lines)
        self.assertIn(str(real_project), text)
        self.assertIn(f"--cwd {str(real_project)}", text)

    def test_ordinary_combined_repair_prepares_with_recorded_model(self) -> None:
        # Leftover model evidence (observed_cwd already
        # cleared by the gate's repair) makes the plain resume a pinned
        # relaunch; the explicit recorded lead is accepted too.
        record = self.save_v4_session(
            target=cli.LaunchTarget(
                "ad-hoc", claude_multi.profile.ad_hoc_direct("kimi-k3", "max"), None, False, "Direct"
            )
        )
        self.runtime.session_store.save({
            **record,
            "identity_state": sessions.IDENTITY_REPAIR_NEEDED,
            "observed_model": "gpt-multi-sol-high[1m]",
        })
        target = cli.LaunchTarget("record", None, None, False, "Session")
        plain = self.runtime.prepare(
            target, action="resume", passthrough=[], session_id=FIXED_ID
        )
        self.assertTrue(plain.model_relaunch)
        explicit = self.runtime.prepare(
            target, action="resume", passthrough=[], session_id=FIXED_ID,
            lead_override=record["applied"]["lead"]["key"],
        )
        self.assertTrue(explicit.model_relaunch)

class ResumeTranscriptBoundaryTests(CLITestCase):
    """Resume boundaries: ambiguity, non-files, force threading, notices."""

    def _prepared(self, record, *, precommitted=False):
        import types

        action = claude_multi.compiler.build_resume(
            sessions.managed_id(record), sessions.runtime_session_id(record)
        )
        result = types.SimpleNamespace(session_action=action)
        return cli.PreparedLaunch(result, record, None, None)

    def _bare_runtime(self):
        return cli.Runtime(
            asset_root=CATALOG_ROOT,
            environ=self.runtime.environ,
            cwd=self.runtime.cwd,
            launch_callback=None,
            doctor_callback=lambda _runtime: [],
            **self._runtime_seams(),
        )

    def _transcript(self, record):
        return (
            Path(self.runtime.environ["HOME"])
            / ".claude"
            / "projects"
            / cli._native_project_slug(record["cwd"])
            / f"{sessions.runtime_session_id(record)}.jsonl"
        )

    def test_precommitted_still_refuses_transcript_missing(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        self._transcript(record).unlink()
        runtime = self._bare_runtime()
        with self.assertRaises(cli.CLIError) as raised:
            runtime.perform(self._prepared(record, precommitted=True))
        self.assertIn("Transcript not found", str(raised.exception))

    def test_ambiguous_slug_decode_falls_back_to_slug_guidance(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        home = Path(self.runtime.environ["HOME"])
        # Two real paths whose slugs collide: "a-b/c" and "a/b-c".
        for real in (home / "a-b" / "c", home / "a" / "b-c"):
            real.mkdir(parents=True)
        stray = self._transcript(record)
        slug_dir = stray.parent.parent / cli._native_project_slug(home / "a-b" / "c")
        self.assertEqual(
            slug_dir, stray.parent.parent / cli._native_project_slug(home / "a" / "b-c")
        )
        slug_dir.mkdir(parents=True)
        stray.rename(slug_dir / stray.name)
        gate = cli._evaluate_resume_gate(self.runtime, record)
        self.assertEqual(gate.kind, "transcript-elsewhere")
        text = " ".join(gate.lines)
        # No exact --cwd command when the decode is ambiguous.
        self.assertNotIn(str(home / "a-b" / "c"), text)
        self.assertIn("<that project directory>", text)

    def test_directory_named_like_transcript_is_not_a_transcript(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        stray = self._transcript(record)
        stray.unlink()
        (stray.parent / f"{FIXED_ID}.jsonl").mkdir()
        gate = cli._evaluate_resume_gate(self.runtime, record)
        self.assertEqual(gate.kind, "transcript-missing")

class ResumePickerAndLivenessTests(CLITestCase):
    """Ordinary embedded picker, no-force stop,
    target-only precheck."""

    def _bare_runtime(self):
        return cli.Runtime(
            asset_root=CATALOG_ROOT,
            environ=self.runtime.environ,
            cwd=self.runtime.cwd,
            launch_callback=None,
            doctor_callback=lambda _runtime: [],
            **self._runtime_seams(),
        )

    def _prepared(self, record, *, precommitted=False):
        import types

        action = claude_multi.compiler.build_resume(
            sessions.managed_id(record), sessions.runtime_session_id(record)
        )
        result = types.SimpleNamespace(session_action=action)
        return cli.PreparedLaunch(result, record, None, None)

    def _ordinary(self, **overrides):
        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd=self.runtime.cwd,
            model="qwen38",
            context_profile="large",
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
        )
        for key, value in overrides.items():
            record[key] = value
        self.runtime.session_store.save(record)
        self._write_transcript(sessions.runtime_session_id(record))
        return record

    def test_stop_resume_does_not_bypass_final_gate(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        runtime = self._bare_runtime()
        prepared = self._prepared(record)
        with mock.patch.object(claude_multi.launch, "perform_launch", return_value=0):
            # Decision None: a target that is live again at the boundary
            # is refused (no stale force exemption after stop & resume).
            with mock.patch.object(
                runtime, "_live_prefixes", return_value=frozenset({FIXED_ID})
            ):
                with self.assertRaises(cli.CLIError) as raised:
                    runtime.perform(prepared, resume_decision=None)
        self.assertIn("live in the background", str(raised.exception))

    def test_stop_precheck_requires_target_liveness_not_alias(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        record["runtime_session_id"] = OTHER_ID
        record["runtime_aliases"] = [
            {
                "session_id": FIXED_ID,
                "source": "fork",
                "observed_at": "2026-07-22T00:00:00Z",
            }
        ]
        _v3.save_any(self.runtime.session_store, record)
        # Only the historical alias (FIXED_ID) is live; the current runtime
        # (OTHER_ID) is not — stop must refuse rather than target a
        # non-live identity.
        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset({FIXED_ID})
        ):
            refusal = cli._stop_precheck(self.runtime, record)
        self.assertIsNotNone(refusal)
        self.assertIn("not live in the background", refusal)


class SubagentModelBleedTests(CLITestCase):
    """model/cwd evidence from subagent contexts and compact events is
    inadmissible; start/resume events stay authoritative."""

    def _event(self, **fields):
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": FIXED_ID,
            "cwd": self.runtime.cwd,
        }
        payload.update(fields)
        return strict_json.canonical_bytes(payload).decode("utf-8")

    def test_compact_model_is_not_observed(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        code, _output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID],
            self._event(source="compact", model="gpt-multi-sol-xhigh"),
        )
        self.assertEqual(code, 0)
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertNotIn("observed_model", record)
        self.assertNotEqual(
            record["identity_state"], sessions.IDENTITY_REPAIR_NEEDED
        )

    def test_compact_does_not_clear_a_legit_observed_model(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        record = self.runtime.session_store.load(FIXED_ID)
        record["identity_state"] = sessions.IDENTITY_REPAIR_NEEDED
        record["observed_model"] = "gpt-multi-sol-high[1m]"
        _v3.save_any(self.runtime.session_store, record)
        code, _output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID],
            self._event(source="compact", model="gpt-multi-sol-xhigh"),
        )
        self.assertEqual(code, 0)
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(record["observed_model"], "gpt-multi-sol-high[1m]")

    def test_agent_context_markers_suppress_model_and_cwd(self) -> None:
        # Defense-in-depth on SYNTHETIC shapes (the 2.1.220 compact payload
        # carries no markers). source="resume" is used so the
        # managed-compact blanket cannot mask the marker branch itself:
        # each marker alone must suppress BOTH model and cwd evidence.
        self.save_session(mode="durable", scope_generation=1)
        worktree = "/home/user/repo/.claude/worktrees/agent-abc123"
        markers = (
            {"agent_id": "agent-abc123"},
            {"agent_transcript_path": "/proj/x/subagents/agent-abc123.jsonl"},
            {"transcript_path": "/proj/x/subagents/agent-abc123.jsonl"},
        )
        for marker in markers:
            with self.subTest(marker=next(iter(marker))):
                record = self.runtime.session_store.load(FIXED_ID)
                for key in ("observed_model", "observed_cwd"):
                    record.pop(key, None)
                _v3.save_any(self.runtime.session_store, record)
                code, _output = self.run_cli(
                    ["session-event", "start", "--managed-id", FIXED_ID],
                    self._event(
                        source="resume",
                        cwd=worktree,
                        model="gpt-multi-sol-xhigh",
                        **marker,
                    ),
                )
                self.assertEqual(code, 0)
                record = self.runtime.session_store.load(FIXED_ID)
                self.assertNotIn("observed_model", record)
                self.assertNotIn("observed_cwd", record)

    def test_resume_model_still_observed(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        code, _output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID],
            self._event(source="resume", model="gpt-multi-sol-high[1m]"),
        )
        self.assertEqual(code, 0)
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(record["observed_model"], "gpt-multi-sol-high[1m]")
        self.assertEqual(record["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)


class SubagentModelBleedOrdinaryTests(CLITestCase):
    """Ordinary side: compact reconciliation stays, agent context ignored."""

    def _ordinary(self):
        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd=self.runtime.cwd,
            model="qwen38",
            context_profile="large",
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
        )
        _v3.save_any(self.runtime.session_store, record)
        return record

    def test_ordinary_compact_still_reconciles_model(self) -> None:
        self._ordinary()
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": FIXED_ID,
                "source": "compact",
                "cwd": self.runtime.cwd,
                "model": "claude-fable-5[1m]",
            }
        ).decode("utf-8")
        code, _output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID], payload
        )
        self.assertEqual(code, 0)
        updated = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(updated["ordinary_model"], "fable")

    def test_ordinary_agent_context_event_ignored(self) -> None:
        # Same defense-in-depth note as the managed marker test: synthetic
        # shape; the compact payload carries no such field at 2.1.220.
        self._ordinary()
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": FIXED_ID,
                "source": "compact",
                "cwd": self.runtime.cwd,
                "model": "claude-fable-5[1m]",
                "agent_transcript_path": "/proj/x/subagents/agent-abc.jsonl",
            }
        ).decode("utf-8")
        code, _output = self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID], payload
        )
        self.assertEqual(code, 0)
        updated = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(updated["ordinary_model"], "qwen38")


class SessionsLastUsedSortTests(CLITestCase):
    """Feature: sessions sorted by last used, created also visible."""

    def _save_with_times(
        self, session_id, *, created, last_seen=None, scope_generation=1
    ):
        record = self.save_session(
            session_id=session_id, mode="durable", scope_generation=scope_generation
        )
        record["created_at"] = created
        if last_seen is not None:
            record["last_seen_at"] = last_seen
        _v3.save_any(self.runtime.session_store, record)
        return record

    def test_missing_last_seen_falls_back_to_created(self) -> None:
        # The schema always carries last_seen_at; the helper's fallback is
        # exercised directly at the unit level.
        bare = {"created_at": "2026-07-25T00:00:00Z"}
        self.assertEqual(cli._record_last_seen(bare), bare["created_at"])
        self.assertEqual(
            cli._record_sort_key_last_used(bare), bare["created_at"]
        )
        self._save_with_times(FIXED_ID, created="2026-07-25T00:00:00Z")
        self._save_with_times(
            OTHER_ID,
            created="2026-07-28T00:00:00Z",
            last_seen="2026-07-20T00:00:00Z",
        )
        # FIXED's last_seen (make_record default 07-21) precedes its created
        # (07-25) → clamps to created; OTHER (last_seen 07-20 < created
        # 07-28) also clamps: the effective order is by created (07-28 first).
        # (The screen half is in test_screens_sessions.)
        ordered = sorted(
            cli._session_records(self.runtime), key=cli._record_sort_key_last_used, reverse=True
        )
        self.assertEqual([r["managed_id"] for r in ordered], [OTHER_ID, FIXED_ID])

    def test_text_listing_sorted_by_last_used_with_both_fields(self) -> None:
        self._save_with_times(
            FIXED_ID,
            created="2026-07-20T00:00:00Z",
            last_seen="2026-07-29T10:00:00Z",
        )
        self._save_with_times(
            OTHER_ID,
            created="2026-07-28T00:00:00Z",
            last_seen="2026-07-21T00:00:00Z",
        )
        code, output = self.run_cli(["sessions", "list"], interactive=False)
        self.assertEqual(code, 0)
        # One age column (last used), newest first.
        self.assertIn(cli._record_last_used_age(self.runtime.session_store.load(FIXED_ID)), output)
        self.assertLess(output.index(FIXED_ID[:8]), output.index(OTHER_ID[:8]))


class SessionsLastUsedLayoutTests(CLITestCase):
    """The last-used fallback (the width tiers are in test_screens_sessions)."""

    def test_pre_creation_last_seen_falls_back_to_created(self) -> None:
        record = {
            "created_at": "2026-07-28T00:00:00Z",
            "last_seen_at": "2026-07-20T00:00:00Z",
        }
        self.assertEqual(cli._record_last_seen(record), record["created_at"])
        malformed = {
            "created_at": "2026-07-28T00:00:00Z",
            "last_seen_at": "not-a-date",
        }
        self.assertEqual(cli._record_last_seen(malformed), malformed["created_at"])
        missing = {"created_at": "2026-07-28T00:00:00Z"}
        self.assertEqual(cli._record_last_seen(missing), missing["created_at"])


class SessionsLastUsedTierTests(CLITestCase):
    """The last-used display equals the sort value (the tiers are in test_screens_sessions)."""

    def test_last_used_age_matches_sort_value(self) -> None:
        record = {
            "created_at": "2026-99-99T00:00:00Z",
            "last_seen_at": "2026-07-20T00:00:00Z",
        }
        # Calendar-invalid created sorts as the effective value; the display
        # must show the SAME value, never a divergent fallback.
        self.assertEqual(
            cli._record_last_used_age(record), cli._record_last_seen(record)
        )


class CwdMissingGateTests(CLITestCase):
    """Transcript present but recorded project dir gone
    (the classic rename) — the gate names both real exits up front."""

    def _gate(self, record):
        return cli._evaluate_resume_gate(self.runtime, record)

    def _transcript(self, record):
        return (
            Path(self.runtime.environ["HOME"])
            / ".claude"
            / "projects"
            / cli._native_project_slug(record["cwd"])
            / f"{sessions.runtime_session_id(record)}.jsonl"
        )

    def test_present_transcript_with_gone_cwd_is_cwd_missing(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        shutil.rmtree(self.root / "project")
        gate = self._gate(record)
        self.assertEqual(gate.kind, "cwd-missing")
        text = " ".join(gate.lines)
        runtime_id = sessions.runtime_session_id(record)
        self.assertIn("rename it back", text)
        self.assertIn(str(self.root / "project"), text)
        self.assertIn(
            f"relink-runtime {FIXED_ID} {runtime_id} --cwd '<new project directory>'",
            text,
        )
        self.assertIn("the exact spot to move the transcript to", text)
        self.assertEqual([value for value, _label in gate.actions], ["relink-choose"])

    def test_elsewhere_names_the_expected_location(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        stray = self._transcript(record)
        other_dir = stray.parent.parent / "-other-project"
        other_dir.mkdir(parents=True)
        stray.rename(other_dir / stray.name)
        gate = self._gate(record)
        self.assertEqual(gate.kind, "transcript-elsewhere")
        text = " ".join(gate.lines)
        # The follow-up instruction is completable: the exact destination is
        # named, never a <new-slug> the operator must compute by hand.
        self.assertIn(str(stray.parent / stray.name), text)
        self.assertNotIn("<new-slug>", text)

    def test_existing_cwd_stays_ok(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        gate = self._gate(record)
        self.assertEqual(gate.kind, "ok")

    def test_cwd_missing_outranks_liveness(self) -> None:
        # Transcript blockers (cwd-missing among them) outrank the daemon
        # question, exactly like transcript-missing does.
        record = self.save_session(mode="durable", scope_generation=1)
        shutil.rmtree(self.root / "project")
        gate = cli._evaluate_resume_gate(
            self.runtime, record, live_prefixes=frozenset({FIXED_ID[:8]})
        )
        self.assertEqual(gate.kind, "cwd-missing")


class LegacyScopeRemedyAndSessionDisplayTests(CLITestCase):
    def test_scope_exceeds_is_legacy_and_current_cli_names_profile_migrate(self) -> None:
        # The 2.x availability validator is retired. Reproduce
        # its old message using the frozen reader, then exercise today's CLI.
        document = _v3.default_composition()
        key = next(k for k, value in document["availability"]["models"].items() if value != "off")
        models = _v3.v1_models_view(self.runtime.catalog.docs)["models"]
        provider = models[key]["provider"]
        wanted = document["availability"]["models"][key]
        document["availability"]["providers"][provider] = "off"
        with self.assertRaisesRegex(composition.CompositionError, "scope .* exceeds provider") as caught:
            _v3.resolve_v1(self.runtime.catalog.docs, document)
        self.assertIn(
            f"availability.models.{key}: scope {wanted!r} exceeds provider {provider!r} scope 'off'",
            str(caught.exception),
        )
        document["name"] = "legacy-scope-exceeds"
        _write_composition(self.runtime, document)
        code, output = self.run_cli_err(["profile", "show", document["name"]], interactive=False)
        self.assertEqual(code, 1, output)
        self.assertIn("claude-multi profile migrate", output)
        self.assertNotIn("exceeds provider", output)

    def test_sessions_show_notices_leave_stdout_as_one_json_document(self) -> None:
        for notice in ("pending", "choice", "fork"):
            with self.subTest(notice=notice):
                record = self.save_v4_session()
                if notice == "pending":
                    record["pending"] = {
                        "requested_at": "2026-09-26T12:00:00Z", "kind": "profile",
                        "profile": "quality", "follow": True, "document": None,
                        "reasons": ["requested (--relaunch)"],
                    }
                    expected = "pending relaunch change:"
                elif notice == "choice":
                    record["applied"]["lead"]["key"] = "no-such-lead"
                    record["applied_hash"] = strict_json.bundle_digest(record["applied"])
                    expected = "needs a lead choice:"
                else:
                    record["pending_forks"] = [{"session_id": OTHER_ID, "observed_at": "2026-09-26T12:00:00Z"}]
                    expected = OTHER_ID
                self.runtime.session_store.save(record)
                before = self.runtime.session_store.read_record_bytes(FIXED_ID)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code, output = self.run_cli(["sessions", "show", FIXED_ID], interactive=False)
                self.assertEqual(code, 0, output)
                self.assertEqual(json.loads(output), self.runtime.session_store.load(FIXED_ID))
                self.assertIn(expected, err.getvalue())
                self.assertEqual(self.runtime.session_store.read_record_bytes(FIXED_ID), before)


class CorruptForgetTests(CLITestCase):
    """A corrupt record is forgettable load-free."""

    def _corrupt_record(self) -> Path:
        path = self.runtime.session_store.sessions_dir / f"{FIXED_ID}.json"
        state.atomic_write(path, b"{not json")
        return path

    def test_corrupt_record_forgets_load_free_with_note(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        self._corrupt_record()
        pointer = self.runtime.session_store._pointer_path(self.runtime.cwd)
        code, output, errors = self.run_cli_both(["sessions", "forget", FIXED_ID, "--yes"])
        self.assertEqual(code, 0, output)
        self.assertIn("unreadable", errors)
        self.assertIn("load-free", errors)
        self.assertIn(f"Forgot: {FIXED_ID}", output)
        self.assertFalse(self.runtime.session_store.exists(FIXED_ID))
        # The pointer sweep is by id and works without a parseable record.
        self.assertFalse(pointer.exists())

    def test_corrupt_record_removes_scope(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        live_scope = scope_mod.scope_dir(self.runtime.session_store.root, FIXED_ID)
        state.ensure_private_dir(live_scope)
        self._corrupt_record()
        code, output = self.run_cli(["sessions", "forget", FIXED_ID, "--yes"])
        self.assertEqual(code, 0, output)
        self.assertFalse(live_scope.exists())

    def test_corrupt_runtime_lookup_names_the_managed_id_retry_without_mutating(self) -> None:
        for malformed in (True, False):
            with self.subTest(malformed=malformed):
                record = self.save_session(mode="durable", scope_generation=1)
                record["runtime_session_id"] = OTHER_ID
                record["cwd"] = 42  # parseable but invalid, still claims OTHER_ID
                path = self.runtime.session_store.sessions_dir / f"{FIXED_ID}.json"
                raw = b"{not json" if malformed else json.dumps(record).encode()
                state.atomic_write(path, raw)
                pointer = self.runtime.session_store._pointer_path(self.runtime.cwd)
                before_pointer = pointer.read_bytes()
                code, output = self.run_cli_err(["sessions", "forget", OTHER_ID], interactive=False)
                self.assertEqual(code, 1, output)
                head = (f"cannot determine runtime ownership while session record {FIXED_ID} is malformed:"
                        if malformed else f"unreadable session record {FIXED_ID} may claim runtime session {OTHER_ID}:")
                self.assertIn(head, output)
                self.assertIn(f"retry with its managed ID: claude-multi sessions forget {FIXED_ID}", output)
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(pointer.read_bytes(), before_pointer)
                code, output = self.run_cli(["sessions", "forget", FIXED_ID, "--yes"], interactive=False)
                self.assertEqual(code, 0, output)
                self.assertIn(f"Forgot: {FIXED_ID}", output)
                self.assertFalse(path.exists())

    def test_non_uuid_corrupt_argument_still_raises(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        self._corrupt_record()
        # A prefix that may name the unreadable record chooses nothing.
        code, output = self.run_cli_err(["sessions", "forget", FIXED_ID[:-1], "--yes"])
        self.assertEqual(code, 1)
        self.assertIn(f"session record {FIXED_ID} is unreadable", output)
        self.assertIn(f"claude-multi sessions forget {FIXED_ID}", output)
        self.assertTrue(self.runtime.session_store.exists(FIXED_ID))


class ForgetLiveGuardTests(CLITestCase):
    """`sessions forget` refuses live/self sessions."""

    def test_live_session_refuses_with_stop_guidance(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset({"11111111"})
        ):
            code, output = self.run_cli_err(["sessions", "forget", FIXED_ID, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("live in the background", output)
        self.assertIn(f"sessions stop {FIXED_ID}", output)
        self.assertTrue(self.runtime.session_store.exists(FIXED_ID))

    def test_self_forget_refuses(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = FIXED_ID
        self.addCleanup(self.runtime.environ.pop, "CLAUDE_MULTI_MANAGED_ID", None)
        code, output = self.run_cli_err(["sessions", "forget", FIXED_ID, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("running inside", output)
        self.assertTrue(self.runtime.session_store.exists(FIXED_ID))

    def test_corrupt_record_self_forget_still_refuses_by_id(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        state.atomic_write(
            self.runtime.session_store.sessions_dir / f"{FIXED_ID}.json",
            b"{not json",
        )
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = FIXED_ID
        self.addCleanup(self.runtime.environ.pop, "CLAUDE_MULTI_MANAGED_ID", None)
        code, output = self.run_cli_err(["sessions", "forget", FIXED_ID, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("running inside", output)

    def test_not_live_forgets_normally(self) -> None:
        self.save_session()
        code, output = self.run_cli(["sessions", "forget", FIXED_ID, "--yes"])
        self.assertEqual(code, 0, output)
        self.assertIn("Forgot", output)


class ManagedHookModelEquivalenceTests(CLITestCase):
    """Managed 1M-lead wire+'[1m]' canonical form
    reconciles; catalog drift never crashes the SessionStart hook."""

    def _kimi_sol_record(self):
        # kimi-sol is an operator composition, not a catalog one: swap the
        # default lead to a >=1M model instead (kimi-k3, 1M selector).
        # The seedless store raises for "default".
        document = _v3.default_composition()
        document["slots"][0]["model"] = "kimi-k3"
        return self.save_session(document=document, mode="durable", scope_generation=1)

    def _start_event(self, model: str):
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": OTHER_ID,
                "source": "startup",
                "cwd": self.runtime.cwd,
                "model": model,
            }
        ).decode("utf-8")
        return self.run_cli(
            ["session-event", "start", "--managed-id", FIXED_ID], payload
        )

    def test_wire_1m_canonical_form_reconciles_to_selector(self) -> None:
        self._kimi_sol_record()
        code, output = self._start_event("k3[1m]")
        self.assertEqual(code, 0, output)
        updated = self.runtime.session_store.load(FIXED_ID)
        # Canonical form accepted: no drift marker, runtime reconciled.
        self.assertNotIn("observed_model", updated)
        self.assertEqual(updated["runtime_session_id"], OTHER_ID)

    def test_wire_name_still_reconciles(self) -> None:
        self._kimi_sol_record()
        code, output = self._start_event("k3")
        self.assertEqual(code, 0, output)
        updated = self.runtime.session_store.load(FIXED_ID)
        self.assertNotIn("observed_model", updated)

    def test_catalog_drift_records_evidence_without_crashing(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        lead_id = record["snapshot"]["lead"]["model"]
        docs = self.runtime.catalog.docs
        original = docs["models"]["models"].pop(lead_id)
        self.addCleanup(
            docs["models"]["models"].__setitem__, lead_id, original
        )
        code, output = self._start_event("anything-at-all")
        # No KeyError traceback: the hook exits clean, the runtime-id
        # reconciliation still runs, and the unverifiable model report is
        # recorded as informational drift instead of dying the hook.
        self.assertEqual(code, 0, output)
        updated = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(updated["runtime_session_id"], OTHER_ID)
        self.assertEqual(updated["observed_model"], "anything-at-all")


class DoctorRotateTokenTests(CLITestCase):
    """`doctor --rotate-token` drives proxy.rotate_token.

    The loopback probe is the Runtime seam: the fixture gateway below reads
    the on-disk render (sentinel + api-keys), so no test reaches :8317.
    Clock and sleep are injected; the helper-TTL wait is simulated.
    """

    def setUp(self) -> None:
        super().setUp()
        self.home = Path(self.runtime.environ["HOME"])
        self.config_dir = claude_multi.proxy.config_dir(self.home)
        self.old = FIXTURE_GATEWAY_TOKEN
        self.now = 0.0
        self.gateway_calls = 0
        self.runtime.served_models_callback = self._fixture_gateway

    def _clock(self) -> float:
        return self.now

    def _sleep(self, seconds: float) -> None:
        self.now += seconds

    def _fixture_gateway(self, gateway, token):
        self.gateway_calls += 1
        yaml = (self.config_dir / "config.yaml").read_text()
        block = yaml.split("api-keys:\n", 1)[1].split("debug:", 1)[0]
        keys = re.findall(r'"([0-9a-f]{64})"', block)
        if token not in keys:
            return None, 401
        sentinels = set(re.findall(r'alias: "(claude-multi-render-[0-9a-f]+)"', yaml))
        # Serves the loaded render: its sentinel plus every routable selector.
        return set(self.served) | sentinels, 200

    def _rotate(self, answers: str, *, interactive=True):
        output = io.StringIO()
        code = cli._doctor_rotate_token(
            self.runtime,
            input_stream=io.StringIO(answers),
            output_stream=output,
            interactive=interactive,
            clock=self._clock,
            sleep=self._sleep,
        )
        return code, output.getvalue()

    def test_parser_offers_rotate_token_in_the_doctor_action_group(self) -> None:
        args = cli.build_parser().parse_args(["doctor", "--rotate-token"])
        self.assertTrue(args.doctor_rotate_token)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["doctor", "--rotate-token", "--prune"])

    def test_noninteractive_refuses_without_writing(self) -> None:
        code, out = self.run_cli_err(["doctor", "--rotate-token"], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("interactive operator flow", out)
        self.assertFalse((self.config_dir / "previous-key").exists())
        self.assertFalse((self.config_dir / "config.yaml").exists())
        self.assertEqual(self.gateway_calls, 0)

    def test_declined_start_changes_nothing(self) -> None:
        code, out = self._rotate("n\n")
        self.assertEqual(code, 1)
        self.assertIn("nothing changed", out)
        self.assertFalse((self.config_dir / "previous-key").exists())
        self.assertEqual(self.runtime.gateway_token(), self.old)

    def test_full_rotation_reports_each_step_and_never_prints_tokens(self) -> None:
        code, out = self._rotate("y\n")
        self.assertEqual(code, 0, out)
        new = self.runtime.gateway_token()
        self.assertNotEqual(new, self.old)
        for step in ("step 1/4", "step 2/4", "step 3/4", "step 4/4"):
            self.assertIn(step, out)
        self.assertIn("previous key is retired (old=401, new=200)", out)
        self.assertFalse((self.config_dir / "previous-key").exists())
        self.assertNotIn(self.old, out)
        self.assertNotIn(new, out)
        self.assertEqual(self._fixture_gateway(None, self.old)[1], 401)
        self.assertGreaterEqual(self.now, claude_multi.proxy.HELPER_TTL_SECONDS)

    def _rotate_redirected(self, answers: str) -> tuple[int, str, str, str]:
        """``doctor --rotate-token`` through ``main`` with a terminal of its
        own and stdout/stderr redirected: (exit, terminal, stdout, stderr)."""

        class Terminal(io.StringIO):
            def close(self):  # main closes an owned terminal; keep the text
                pass

        real = claude_multi.proxy.rotate_token

        def rotate(home, **options):
            return real(home, clock=self._clock, sleep=self._sleep, **options)

        tty_in, tty_out, stdout, stderr = Terminal(answers), Terminal(), io.StringIO(), io.StringIO()
        with mock.patch('claude_multi.cli.streams._open_tty_streams', return_value=(tty_in, tty_out)), \
                mock.patch.object(claude_multi.proxy, "rotate_token", rotate), \
                mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr):
            code = cli.main(["doctor", "--rotate-token"], runtime=self.runtime)
        return code, tty_out.getvalue(), stdout.getvalue(), stderr.getvalue()

    def test_a_redirected_rotation_asks_on_the_terminal_and_reports_on_stdout(self) -> None:
        code, terminal, stdout, stderr = self._rotate_redirected("y\n")
        self.assertEqual(code, 0, terminal + stderr)
        new = self.runtime.gateway_token()
        self.assertIn("Rotate the local gateway token now? [y/N]", terminal)
        self.assertIn("step 1/4", terminal)
        self.assertNotIn("[y/N]", stdout)
        self.assertNotIn("step 1/4", stdout)
        self.assertIn("gateway token rotated: the previous key is retired (old=401, new=200)", stdout)
        self.assertNotIn("gateway token rotated", terminal)
        self.assertEqual(stderr, "")
        for text in (terminal, stdout, stderr):
            self.assertNotIn(self.old, text)
            self.assertNotIn(new, text)

    def test_a_redirected_rotation_reports_a_pause_on_stderr(self) -> None:
        record = self.save_session()
        record["launcher_version"] = "2.25.1"
        record["last_event_source"] = "startup"
        _v3.save_any(self.runtime.session_store, record)
        code, terminal, stdout, stderr = self._rotate_redirected("y\nn\n")
        self.assertEqual(code, 1, terminal + stdout + stderr)
        self.assertIn("Rotate the local gateway token now? [y/N]", terminal)
        self.assertIn("may still hold an environment credential", terminal)
        self.assertEqual(stdout, "")
        self.assertIn("rotation paused", stderr)
        self.assertIn("resume with `claude-multi doctor --rotate-token`", stderr)
        self.assertNotIn("rotation paused", terminal)

    def test_env_credential_record_is_listed_and_must_be_confirmed(self) -> None:
        record = self.save_session()
        record["launcher_version"] = "2.25.1"
        record["last_event_source"] = "startup"
        _v3.save_any(self.runtime.session_store, record)
        # Start yes, then refuse the "sessions are dead" confirmation.
        code, out = self._rotate("y\nn\n")
        self.assertEqual(code, 1)
        self.assertIn(FIXED_ID, out)
        self.assertIn("launcher 2.25.1", out)
        # The wording names the behaviour, never a release number.
        self.assertIn("1 session(s) may still hold an environment credential", out)
        self.assertIn("sessions that may still hold an environment credential (the previous token): ", out)
        self.assertNotIn("pre-2", out)
        self.assertIn("rotation paused", out)
        self.assertIn("resume with `claude-multi doctor --rotate-token`", out)
        self.assertTrue((self.config_dir / "previous-key").exists())
        # The dual-key window is Attention, never drift/damage.
        self.assertIn("rotation in progress", cli._token_rotation_attention(self.runtime))
        # Rerun resumes the published window; confirming finishes it.
        code, out = self._rotate("y\ny\n")
        self.assertEqual(code, 0, out)
        self.assertIn("resuming the in-progress rotation", out)
        self.assertFalse((self.config_dir / "previous-key").exists())

    def test_ended_and_helper_only_records_are_not_listed(self) -> None:
        record = self.save_session()
        record["launcher_version"] = "2.25.1"
        record["last_event_source"] = "end"
        _v3.save_any(self.runtime.session_store, record)
        self.assertEqual(cli._pre_helper_session_labels(self.runtime), [])
        record["launcher_version"] = "2.26.0"
        record["last_event_source"] = "startup"
        _v3.save_any(self.runtime.session_store, record)
        self.assertEqual(cli._pre_helper_session_labels(self.runtime), [])

    def test_a_release_record_is_listed_only_below_the_helper_only_catalog(self) -> None:
        # A low launcher number on a helper-only catalog is not a credential
        # holder; the same number on an older catalog stays conservative.
        record = self.save_session()
        record["launcher_version"] = "1.0.0"
        record["catalog_version"] = claude_multi.sessions.HELPER_ONLY_CATALOG
        record["last_event_source"] = "startup"
        _v3.save_any(self.runtime.session_store, record)
        self.assertEqual(cli._pre_helper_session_labels(self.runtime), [])
        record["catalog_version"] = claude_multi.sessions.HELPER_ONLY_CATALOG - 1
        _v3.save_any(self.runtime.session_store, record)
        labels = cli._pre_helper_session_labels(self.runtime)
        self.assertEqual(len(labels), 1)
        self.assertIn("launcher 1.0.0", labels[0])

    def test_unreadable_record_is_listed_as_unknown(self) -> None:
        state.ensure_private_dir(self.runtime.session_store.sessions_dir)
        state.atomic_write(
            self.runtime.session_store.sessions_dir / f"{OTHER_ID}.json", b"{bad"
        )
        labels = cli._pre_helper_session_labels(self.runtime)
        self.assertEqual(len(labels), 1)
        self.assertIn(OTHER_ID, labels[0])
        self.assertIn("liveness unknown", labels[0])

    def _served_report(self):
        return cli._doctor_served_report(self.runtime, self.runtime.gateway_token())

    def _pause_in_retirement(self) -> str:
        """Run a rotation that pauses in step 4 (old key still accepted)."""

        real = self._fixture_gateway

        def old_still_accepted(gateway, token):
            ids, status = real(gateway, token)
            return (ids, status) if token != self.old else (set(), 200)

        self.runtime.served_models_callback = old_still_accepted
        code, out = self._rotate("y\n")
        self.assertEqual(code, 1, out)
        self.assertIn("previous token still accepted", out)
        self.runtime.served_models_callback = real
        return out

    def test_paused_retirement_phase_is_attention_not_drift(self) -> None:
        # config.yaml holds [new] while the slots are
        # (old, new). That is a legal rotation phase, never drift.
        self._pause_in_retirement()
        snap = cli._gateway_snapshot(self.runtime, self.runtime.gateway_token())
        self.assertFalse(snap.config_drift)
        self.assertIn("retirement phase", snap.rotation_phase)
        problems, info = self._served_report()
        self.assertEqual(problems, [])
        self.assertTrue(any("--rotate-token" in line for line in info), info)
        self.assertTrue(any("not `claude-multi-proxy init`" in line for line in info))
        self.assertIn("dual-key window open", cli._token_rotation_attention(self.runtime))
        # Rerunning finishes the rotation; no problem, no rotation line.
        code, out = self._rotate("y\n")
        self.assertEqual(code, 0, out)
        problems, info = self._served_report()
        self.assertEqual(problems, [])
        self.assertFalse(any("rotation" in line for line in info), info)
        self.assertIsNone(cli._token_rotation_attention(self.runtime))

    def test_paused_unpublished_dual_phase_is_attention_not_drift(self) -> None:
        # The reload of [old, candidate] was never verified
        # (unpatched gateway / declined restart): slots are (old,) with
        # previous-key == api-key, config.yaml carries [old, candidate].
        real = self._fixture_gateway
        self.runtime.served_models_callback = lambda gateway, token: (set(), 200)
        code, out = self._rotate("y\nn\n")
        self.assertEqual(code, 1, out)
        self.runtime.served_models_callback = real
        self.assertEqual(self.runtime.gateway_token(), self.old)
        snap = cli._gateway_snapshot(self.runtime, self.old)
        self.assertFalse(snap.config_drift)
        self.assertIn("never published", snap.rotation_phase)
        self.assertIn(
            "interrupted before publication", cli._token_rotation_attention(self.runtime)
        )
        # The expected sentinel is the on-disk phase render's, so a gateway
        # serving it is not "did not reload".
        problems, _info = self._served_report()
        self.assertEqual(problems, [])
        yaml = (self.config_dir / "config.yaml").read_text()
        self.assertIn(snap.sentinel, yaml)

    def test_tampered_phase_render_is_still_drift(self) -> None:
        self._pause_in_retirement()
        path = self.config_dir / "config.yaml"
        state.atomic_write(path, path.read_bytes().replace(b"debug: false", b"debug: true"))
        snap = cli._gateway_snapshot(self.runtime, self.runtime.gateway_token())
        self.assertTrue(snap.config_drift)
        self.assertIsNone(snap.rotation_phase)

    def test_unrelated_key_list_with_previous_key_is_drift(self) -> None:
        state.atomic_write(self.config_dir / "previous-key", (self.old + "\n").encode())
        home = self.home
        document, _a, _u, _i = claude_multi.render.build_config_document(
            self.runtime.ordinary_docs["gateway"],
            self.runtime.ordinary_docs["providers"]["providers"],
            self.runtime.ordinary_docs["models-v2"]["models"],
            home=home,
            gateway_tokens=("c" * 64, "d" * 64),
            resolve_secret=lambda name: claude_multi.proxy.resolve_secret(
                name, environ=self.runtime.environ
            ),
            continuity=claude_multi.continuity.seed_only(self.runtime.catalog)["aliases"],
        )
        state.atomic_write(
            self.config_dir / "config.yaml",
            claude_multi.render.emit_yaml(document).encode("utf-8"),
        )
        snap = cli._gateway_snapshot(self.runtime, self.old)
        self.assertTrue(snap.config_drift)
        self.assertIsNone(snap.rotation_phase)

    def test_unreloaded_gateway_pauses_with_restart_command(self) -> None:
        self.runtime.served_models_callback = lambda gateway, token: (set(), 200)
        code, out = self._rotate("y\nn\n")
        self.assertEqual(code, 1)
        self.assertIn("restart required", out)
        self.assertIn("claude-multi gateway restart", out)
        # Unpublished: the helper still serves the old key.
        self.assertEqual(self.runtime.gateway_token(), self.old)


class DoctorServed401Tests(CLITestCase):
    """A 401 from /v1/models is a problem with a
    restart action; other non-200 statuses stay an informational skip."""

    def _report(self, served, status):
        with unittest.mock.patch.object(
            claude_multi.launch, "served_models", return_value=(served, status)
        ):
            return cli._doctor_served_report(self.runtime, "t" * 64)

    def test_401_is_a_problem_naming_restart(self) -> None:
        problems, info = self._report(None, 401)
        self.assertEqual(len(problems), 1)
        self.assertIn("401", problems[0])
        self.assertIn("rejected the local token", problems[0])
        self.assertIn("claude-multi gateway restart", problems[0])
        self.assertEqual(info, [])

    def test_other_non_200_is_an_informational_skip(self) -> None:
        problems, info = self._report(None, 500)
        self.assertEqual(problems, [])
        self.assertEqual(len(info), 1)
        self.assertIn("cross-check was skipped", info[0])

    def test_gateway_down_after_readiness_is_an_informational_skip(self) -> None:
        with unittest.mock.patch.object(
            claude_multi.launch,
            "served_models",
            side_effect=claude_multi.launch.LaunchError("connection refused"),
        ):
            problems, info = cli._doctor_served_report(self.runtime, "t" * 64)
        self.assertEqual(problems, [])
        self.assertEqual(len(info), 1)
        self.assertIn("restart in flight", info[0])


class CustomRegistryGuardTests(CLITestCase):
    """The registry's fail-closed
    guards (OAuth pools, header name, catalog shadowing, FileLock)."""

    def test_add_model_rejects_oauth_pool_providers(self) -> None:
        with self.assertRaisesRegex(claude_multi.custom.CustomModelsError, "OAuth pool"):
            claude_multi.custom.add_model(
                self.runtime.environ,
                "pool-backed",
                wire_model="pool-backed",
                provider="anthropic",
                context_tokens=200000,
                created_via="manual",
                catalog_providers=self.runtime.catalog.providers,
            )

    def test_add_provider_header_auth_is_x_api_key_only(self) -> None:
        with self.assertRaisesRegex(claude_multi.custom.CustomModelsError, "x-api-key"):
            claude_multi.custom.add_provider(
                self.runtime.environ,
                "bad-header",
                base_url="https://lab.example.com/apps/anthropic",
                auth_kind="header",
                header="Authorization",
                secret_env="BAD_HEADER_API_KEY",
            )

    def test_add_provider_never_shadows_the_catalog(self) -> None:
        with self.assertRaisesRegex(claude_multi.custom.CustomModelsError, "trusted catalog"):
            claude_multi.custom.add_provider(
                self.runtime.environ,
                "kimi",
                base_url="https://example.com",
                auth_kind="bearer",
                secret_env="KIMI_CLAUDE_API_KEY",
                catalog_providers=self.runtime.catalog.providers,
            )

    def test_add_model_never_shadows_the_catalog(self) -> None:
        with self.assertRaisesRegex(claude_multi.custom.CustomModelsError, "trusted catalog"):
            claude_multi.custom.add_model(
                self.runtime.environ,
                "sol",
                wire_model="sol-clone",
                provider="kimi",
                context_tokens=200000,
                created_via="manual",
                catalog_providers=self.runtime.catalog.providers,
                catalog_models=tuple(self.runtime.catalog.lines),
            )

    def test_fetch_path_shadow_guard_matches_manual_path(self) -> None:
        # A provider-advertised id colliding with the catalog is refused
        # exactly like a typed one (the guard lives in add_model itself).
        with self.assertRaisesRegex(claude_multi.custom.CustomModelsError, "trusted catalog"):
            claude_multi.custom.add_model(
                self.runtime.environ,
                "sol",
                wire_model="sol",
                provider="kimi",
                context_tokens=372000,
                created_via="discover",
                catalog_providers=self.runtime.catalog.providers,
                catalog_models=tuple(self.runtime.catalog.lines),
            )

    def test_mutation_holds_the_registry_lock(self) -> None:
        observed = {}

        def probe(registry):
            lock = state.FileLock(claude_multi.custom.registry_path(self.runtime.environ))
            observed["second_acquire"] = lock.acquire(blocking=False)
            if not observed["second_acquire"]:
                return
            lock.release()

        claude_multi.custom._mutate(self.runtime.environ, probe, touches=lambda _after: set())
        # The whole load-modify-save transaction runs under the FileLock.
        self.assertFalse(observed["second_acquire"])

    def test_handwritten_shadow_entries_drop_with_conflicts_named(self) -> None:
        # Bypass the add-time guards by writing the registry directly:
        # the merge must drop catalog-shadowing entries loudly.
        path = claude_multi.custom.registry_path(self.runtime.environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(
            path,
            strict_json.pretty_file_bytes(
                {
                    "version": 1,
                    "providers": {
                        "kimi": {
                            "base_url": "https://evil.example.com",
                            "auth_kind": "bearer",
                            "secret_env": "KIMI_CLAUDE_API_KEY",
                        }
                    },
                    "models": {
                        "sol": {
                            "wire_model": "not-really-sol",
                            "provider": "kimi",
                            "context_tokens": 8192,
                            "created_via": "manual",
                        }
                    },
                }
            ),
        )
        registry = claude_multi.custom.load_registry(self.runtime.environ)
        conflicts = claude_multi.custom.merge_conflicts(self.runtime.catalog.docs, registry)
        self.assertEqual(sorted(conflicts), ["model sol", "provider kimi"])
        merged = claude_multi.custom.merge_docs(self.runtime.catalog.docs, registry)
        # The catalog wins: the shadow entries never reached the merged view.
        self.assertEqual(
            merged["providers"]["providers"]["kimi"],
            self.runtime.catalog.docs["providers"]["providers"]["kimi"],
        )
        self.assertEqual(
            merged["models"]["models"]["sol"],
            self.runtime.catalog.docs["models"]["models"]["sol"],
        )


    def test_render_sentinel_provider_ids_are_reserved(self) -> None:
        # A custom provider named like the render sentinel
        # would make every render refuse and crash-loop the gateway unit.
        for reserved in ("claude-multi-render", "claude-multi-render-deadbeef"):
            with self.subTest(provider=reserved), self.assertRaisesRegex(
                claude_multi.custom.CustomModelsError, "reserved for the gateway render sentinel"
            ):
                claude_multi.custom.add_provider(
                    self.runtime.environ,
                    reserved,
                    base_url="https://example.com",
                    auth_kind="bearer",
                    secret_env="RESERVED_API_KEY",
                    catalog_providers=self.runtime.catalog.providers,
                )

    def test_handwritten_reserved_provider_drops_with_its_models(self) -> None:
        path = claude_multi.custom.registry_path(self.runtime.environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(
            path,
            strict_json.pretty_file_bytes(
                {
                    "version": 1,
                    "providers": {
                        "claude-multi-render": {
                            "base_url": "https://example.com",
                            "auth_kind": "bearer",
                            "secret_env": "RESERVED_API_KEY",
                        }
                    },
                    "models": {
                        "rendered": {
                            "wire_model": "rendered",
                            "provider": "claude-multi-render",
                            "context_tokens": 8192,
                            "created_via": "manual",
                        }
                    },
                }
            ),
        )
        registry = claude_multi.custom.load_registry(self.runtime.environ)
        self.assertEqual(
            claude_multi.custom.merge_conflicts(self.runtime.catalog.docs, registry),
            ["provider claude-multi-render"],
        )
        merged = claude_multi.custom.merge_docs(self.runtime.catalog.docs, registry)
        self.assertNotIn("claude-multi-render", merged["providers"]["providers"])
        self.assertNotIn("rendered", merged["models"]["models"])
        # The render (what the gateway unit runs) still succeeds.
        claude_multi.render.build_config_document(
            merged["gateway"],
            merged["providers"]["providers"],
            merged["models-v2"]["models"],
            home=Path(self.runtime.environ["HOME"]),
            gateway_tokens=(FIXTURE_GATEWAY_TOKEN,),
            resolve_secret=lambda _name: None,
            continuity={},
        )


_MUSE_HIGH = "claude-multi-muse-spark-high"
_MUSE_XHIGH = "claude-multi-muse-spark-xhigh"
_LIVE_ID = "44444444-4444-4444-8444-444444444444"
_ENDED_ID = "55555555-5555-4555-8555-555555555555"


class ContinuityDoctorCase(CLITestCase):
    """doctor/prune against a real on-disk render (no doctor stub).

    META is credentialed so the fixture's continuity (retired muse-spark on
    meta) is rendered and served. The fixture gateway serves the on-disk
    render (its sentinel included) minus ``self.hidden``; the reload check
    goes through the Runtime seam. No test reaches :8317.
    """

    def setUp(self) -> None:
        super().setUp()
        state.atomic_write(
            self.secret_file,
            b"KIMI_CLAUDE_API_KEY=cli-test-dummy\nMETA_CLAUDE_API_KEY=cli-test-meta\n",
        )
        self.home = Path(self.runtime.environ["HOME"])
        self.config_dir = claude_multi.proxy.config_dir(self.home)
        self.continuity_file = claude_multi.continuity.path(self.home)
        self.state_root = self.runtime.session_store.root
        self.hidden: set[str] = set()
        self.runtime.doctor_callback = None
        self.runtime.served_models_callback = self._fixture_gateway

    def _fixture_gateway(self, gateway, token):
        yaml = (self.config_dir / "config.yaml").read_text()
        return set(re.findall(r'alias: "([^"]+)"', yaml)) - self.hidden, 200

    def _init(self) -> str:
        environ = {**self.runtime.environ, "CLAUDE_MULTI_ASSETS": str(CATALOG_ROOT)}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = claude_multi.proxy.cmd_init(
                [], environ=environ,
                models_get=lambda _b, _t: (200, self._fixture_gateway(None, None)[0]),
            )
        self.assertEqual(code, 0)
        return out.getvalue()

    def _checks(self):
        return cli._doctor_served_checks(self.runtime, FIXTURE_GATEWAY_TOKEN)

    def _record(self, stem: str, selector: str, *, event=None, raw: bytes | None = None) -> Path:
        directory = state.ensure_private_dir(self.state_root / "sessions")
        path = directory / f"{stem}.json"
        document = {
            "version": 3, "managed_id": stem, "last_event_source": event,
            "snapshot": {"lead": {"client_selector": selector}, "variants": []},
        }
        state.atomic_write(path, raw if raw is not None else strict_json.canonical_file_bytes(document))
        return path


class ContinuityDoctorTests(ContinuityDoctorCase):
    """Doctor reads the persisted set."""

    def test_after_init_doctor_is_clean_and_serves_continuity(self) -> None:
        output = self._init()
        self.assertIn("continuity: 2 aliases (2 rendered)", output)
        problems, info, attention = self._checks()
        self.assertEqual((problems, info, attention), ([], [], []))
        snap = cli._gateway_snapshot(self.runtime, FIXTURE_GATEWAY_TOKEN)
        self.assertEqual(snap.continuity_status, "persisted")
        self.assertEqual(snap.continuity_aliases, frozenset({_MUSE_HIGH, _MUSE_XHIGH}))
        self.assertEqual(snap.continuity_state_root, str(self.state_root))

    def test_records_added_and_deleted_never_move_doctor(self) -> None:
        self._init()
        before = self.continuity_file.read_bytes()
        mtime = self.continuity_file.stat().st_mtime_ns
        record = self._record(_LIVE_ID, "claude-multi-grok45-high[1m]")
        self.assertEqual(self._checks()[:2], ([], []))
        record.unlink()
        self.assertEqual(self._checks()[:2], ([], []))
        self.assertEqual(self.continuity_file.read_bytes(), before)
        self.assertEqual(self.continuity_file.stat().st_mtime_ns, mtime)

    def test_missing_file_says_nothing(self) -> None:
        self._init()
        self.continuity_file.unlink()
        self.assertEqual(self._checks(), ([], [], []))
        self.assertFalse(self.continuity_file.exists())  # doctor never writes

    def test_corrupt_file_is_exactly_one_block_and_no_drift(self) -> None:
        self._init()
        state.atomic_write(self.continuity_file, b'{"version": 1, "aliases": ')
        corrupt = self.continuity_file.read_bytes()
        for rerendered in (False, True):
            if rerendered:
                self._init()  # the renderer's single fallback: the seed set
            with self.subTest(rerendered=rerendered):
                problems, info, attention = self._checks()
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("gateway continuity set unreadable", problems[0])
                self.assertIn("claude-multi-proxy init", problems[0])
                self.assertFalse(any("differs from a fresh render" in line for line in problems + info))
                self.assertEqual((info, attention), ([], []))
                self.assertEqual(self.continuity_file.read_bytes(), corrupt)

    def test_state_root_mismatch_is_block(self) -> None:
        # The persisted root is the gateway's
        # authority; another root is a BLOCK naming both and the adoption.
        self._init()
        document = claude_multi.continuity.read(self.home)
        document["state_root"] = "/elsewhere/claude-multi"
        state.atomic_write(self.continuity_file, strict_json.pretty_file_bytes(document))
        problems, _info, attention = self._checks()
        self.assertEqual(problems, [
            "gateway managed for root /elsewhere/claude-multi; this command used "
            f"{self.state_root} — run with the managed root, or adopt this one after the root "
            f"inventory: claude-multi-proxy init --state-root {self.state_root} --adopt-root"
        ])
        self.assertEqual(attention, [])

    def test_unserved_continuity_alias_is_attention_not_block(self) -> None:
        self._init()
        self.hidden = {_MUSE_HIGH}
        problems, _info, attention = self._checks()
        self.assertEqual(problems, [])
        self.assertEqual(attention, [
            f"continuity alias {_MUSE_HIGH} is not served (its upstream route may "
            "be gone); once no live session uses it: claude-multi doctor "
            f"--prune-aliases {_MUSE_HIGH}"
        ])
        # A catalog alias missing stays a BLOCK.
        self.hidden = {"claude-multi-kimi-k3"}
        self.assertTrue(self._checks()[0])

    def test_doctor_files_continuity_lines_under_attention(self) -> None:
        self._init()
        self.hidden = {_MUSE_XHIGH}
        code, output = self.run_cli(["doctor"], interactive=False)
        self.assertEqual(code, 0, output)
        attention = output.split("Attention\n", 1)[1]
        self.assertIn(f"continuity alias {_MUSE_XHIGH} is not served", attention)


class PruneAliasesTests(ContinuityDoctorCase):
    """`doctor --prune-aliases`."""

    def setUp(self) -> None:
        super().setUp()
        # An unreadable process table is unknown liveness, so
        # the prune fixtures carry a readable (empty) one.
        (self.root / "proc").mkdir(exist_ok=True)

    def _prune(self, *aliases: str) -> tuple[int, str]:
        return self.run_cli(["doctor", "--prune-aliases", *aliases], interactive=False)

    def test_absent_file_has_nothing_to_prune(self) -> None:
        code, output = self._prune()
        self.assertEqual((code, output), (0, "nothing to prune\n"))
        self.assertFalse(self.continuity_file.exists())

    def test_named_alias_with_a_live_record_refuses(self) -> None:
        self._init()
        self._record(_LIVE_ID, _MUSE_HIGH + "[1m]")
        before = self.continuity_file.read_bytes()
        code, output = self._prune(_MUSE_HIGH, _MUSE_XHIGH)
        self.assertEqual(code, 1)
        self.assertIn(f"{_MUSE_HIGH} -> {_LIVE_ID[:8]}", output)
        self.assertEqual(self.continuity_file.read_bytes(), before)

    def test_all_mode_keeps_the_live_reference(self) -> None:
        self._init()
        self._record(_LIVE_ID, _MUSE_HIGH + "[1m]")
        code, output = self._prune()
        self.assertEqual(code, 0, output)
        self.assertIn(f"pruned: {_MUSE_XHIGH} (meta muse-spark-1.3)", output)
        self.assertIn(f"kept: {_MUSE_HIGH} — live session {_LIVE_ID[:8]}", output)
        document = claude_multi.continuity.read(self.home)
        self.assertEqual(set(document["aliases"]), {_MUSE_HIGH})
        self.assertEqual(document["pruned"], {_MUSE_XHIGH: 31})

    def test_unreadable_record_refuses(self) -> None:
        self._init()
        self._record(_LIVE_ID, "", raw=b"{broken")
        before = self.continuity_file.read_bytes()
        code, output = self._prune()
        self.assertEqual(code, 1)
        self.assertIn(f"cannot prove liveness: unreadable records {_LIVE_ID[:8]}", output)
        self.assertEqual(self.continuity_file.read_bytes(), before)

    def test_success_rerenders_awaits_the_sentinel_and_keeps_records(self) -> None:
        self._init()
        ended = self._record(_ENDED_ID, _MUSE_HIGH + "[1m]", event="end")
        record_bytes = ended.read_bytes()
        code, output = self._prune()
        self.assertEqual(code, 0, output)
        self.assertIn(f"pruned: {_MUSE_HIGH} (meta muse-spark-1.3)", output)
        self.assertIn(f"pruned: {_MUSE_XHIGH} (meta muse-spark-1.3)", output)
        self.assertIn("gateway: reloaded (sentinel claude-multi-render-", output)
        document = claude_multi.continuity.read(self.home)
        self.assertEqual(document["aliases"], {})
        self.assertEqual(document["pruned"], {_MUSE_HIGH: 31, _MUSE_XHIGH: 31})
        config = (self.config_dir / "config.yaml").read_text()
        self.assertNotIn(_MUSE_HIGH, config)
        self.assertEqual(ended.read_bytes(), record_bytes)
        # Doctor agrees with the pruned render (no drift), and a re-init
        # never re-seeds the tombstones.
        self.assertEqual(self._checks()[:2], ([], []))
        self._init()
        self.assertEqual(claude_multi.continuity.read(self.home)["aliases"], {})
        code, output = self._prune()
        self.assertEqual((code, output), (0, "nothing to prune\n"))

    def test_unknown_alias_refuses(self) -> None:
        self._init()
        code, output = self._prune("claude-multi-nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown continuity alias(es): claude-multi-nope", output)

    def test_refused_while_a_token_rotation_holds_its_lock(self) -> None:
        self._init()
        before = self.continuity_file.read_bytes()
        lock = state.FileLock(self.config_dir / "token-rotation")
        self.assertTrue(lock.acquire(blocking=False))
        try:
            code, output = self._prune()
        finally:
            lock.release()
        self.assertEqual(code, 1)
        self.assertIn(claude_multi.proxy.ROTATION_IN_PROGRESS, output)
        self.assertEqual(self.continuity_file.read_bytes(), before)

    def test_refused_under_a_newer_state_marker(self) -> None:
        self._init()
        before = self.continuity_file.read_bytes()
        state.atomic_write(self.state_root / "state-version", b"5\n")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code, _output = self._prune()
        output = errors.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("state belongs to a newer claude-multi", output)
        self.assertEqual(self.continuity_file.read_bytes(), before)
        self.assertTrue(cli._writes_versioned_state(
            cli.build_parser().parse_args(["doctor", "--prune-aliases"])
        ))


class ZeroModelProviderPaneTests(CLITestCase):
    """Model-less providers stay configured."""

    def test_listing_and_pane_label_model_less_providers(self) -> None:
        facts = {fact["id"]: fact for fact in cli._provider_facts(self.runtime).facts}
        self.assertEqual(facts["deepseek"]["models_note"], "configured · no models")
        self.assertEqual(facts["deepseek"]["credential"], "DEEPSEEK_CLAUDE_API_KEY missing")
        self.assertEqual(
            facts["meta"]["models_note"], "configured · no models · 2 continuity aliases"
        )
        self.assertIsNone(facts["kimi"]["models_note"])
        self.assertNotIn("0/0", " ".join(str(fact["served_note"]) for fact in facts.values()))
        # The pane half moved to test_screens_catalog
        # (ProvidersScreenTests.test_zero_model_rows); the
        # listing half with _print_ordinary_listing (the line listing of the
        # deleted 2.x line card).

    def test_secret_problems_tolerate_model_less_providers(self) -> None:
        # The 2.x resolved input comes from tests/_v3.py.
        resolved = _v3.resolve_v1(self.runtime.catalog.docs, _v3.default_composition())
        claude_multi.proxy.selected_secret_problems(
            resolved,
            self.runtime.catalog.docs["models"]["models"],
            self.runtime.catalog.docs["providers"]["providers"],
            environ=self.runtime.environ,
        )


class ForgetUnderLockGuardTests(CLITestCase):
    """The forget liveness verdict is taken under the
    lifecycle lock with a fresh prefix scan — and covers corrupt records
    by stable-id prefix."""

    def test_corrupt_record_with_live_stable_prefix_refuses(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        state.atomic_write(
            self.runtime.session_store.sessions_dir / f"{FIXED_ID}.json",
            b"{not json",
        )
        with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', return_value=frozenset({"11111111"})
        ):
            code, output = self.run_cli_err(["sessions", "forget", FIXED_ID, "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("live in the background", output)
        # The corrupt record survived: a refusal never deletes.
        self.assertTrue(
            (self.runtime.session_store.sessions_dir / f"{FIXED_ID}.json").exists()
        )

    def test_check_runs_under_the_lifecycle_lock(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        store = self.runtime.session_store
        held = {}

        def probe(current):
            probe_lock = store.lifecycle_lock(FIXED_ID)
            held["reacquire"] = probe_lock.acquire(blocking=False)
            if held["reacquire"]:
                probe_lock.release()
            return None

        removed, _scope = store.forget_session(FIXED_ID, pre_delete_check=probe)
        self.assertTrue(removed)
        # The lifecycle lock was already held when the check ran.
        self.assertFalse(held["reacquire"])

    def test_refusal_under_lock_deletes_nothing(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        with self.assertRaisesRegex(sessions.SessionError, "held up"):
            self.runtime.session_store.forget_session(
                FIXED_ID, pre_delete_check=lambda _current: "held up"
            )
        self.assertTrue(self.runtime.session_store.exists(FIXED_ID))


class PointerSweepDurabilityTests(CLITestCase):
    """Review should-fix: the by-id pointer sweep deletes through the
    durable primitive (directory fsync), not a bare unlink."""

    def test_sweep_uses_remove_private(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        state.atomic_write(
            self.runtime.session_store.sessions_dir / f"{FIXED_ID}.json",
            b"{not json",
        )
        with mock.patch.object(
            claude_multi.state, "remove_private", wraps=claude_multi.state.remove_private
        ) as spy:
            code, output = self.run_cli(["sessions", "forget", FIXED_ID, "--yes"])
        self.assertEqual(code, 0, output)
        pointer = self.runtime.session_store._pointer_path(self.runtime.cwd)
        self.assertIn(mock.call(pointer), spy.call_args_list)
        self.assertFalse(pointer.exists())


class NewProviderDirectLaunchTests(CLITestCase):
    """Ad-hoc direct launches for direct-provider lines (on the one path).

    On the frozen fixture (the shipped catalog no longer has the deepseek-pro
    and grok46 lines): qwen38 and kimi-k3 are the direct 1M
    claude-compatible class, grok46 the 500K class.
    """

    def test_deepseek_flash_direct_launch(self) -> None:
        prepared = self.prepare_direct_v4("qwen38")
        self.assertEqual(prepared.record["applied"]["lead"]["key"], "qwen38")
        self.assertEqual(prepared.record["lead_class"], "large")
        self.assertEqual(
            prepared.result.env_set["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "800000"
        )

    def test_deepseek_pro_direct_launch_defaults_high_in_large_profile(self) -> None:
        prepared = self.prepare_direct_v4("kimi-k3")
        self.assertEqual(prepared.record["applied"]["lead"]["key"], "kimi-k3")
        self.assertEqual(prepared.record["lead_class"], "large")
        self.assertIn("claude-multi-kimi-k3[1m]", prepared.result.argv)
        self.assertEqual(
            prepared.result.env_set["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "800000"
        )

    def test_grok46_direct_launch_uses_xhigh_500k_window(self) -> None:
        prepared = self.prepare_direct_v4("grok46")
        self.assertEqual(prepared.record["applied"]["lead"]["key"], "grok46")
        self.assertEqual(prepared.record["lead_class"], "grok")
        env_set = prepared.result.env_set
        self.assertEqual(env_set["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "500000")
        # The 500K window must never come from a [1m] selector.
        argv = prepared.result.argv
        self.assertIn("claude-multi-grok46-xhigh", argv)
        self.assertNotIn("claude-multi-grok46-xhigh[1m]", argv)

    def test_new_models_record_saves_with_their_profiles(self) -> None:
        # The session schema accepts every lead class (a custom-profile enum
        # miss would dead-end every custom launch).
        for model_id, lead_class in (
            ("qwen38", "large"),
            ("kimi-k3", "large"),
            ("grok46", "grok"),
        ):
            loaded = self.save_prepared_v4(self.prepare_direct_v4(model_id))
            self.assertEqual(loaded["lead_class"], lead_class)


class SingleModelPrepareTests(CLITestCase):
    """WS4c/4d on the one path: any catalog model launches as an
    ad-hoc direct lineup; the subagent policy is recorded in ``applied``."""

    def _direct(self, model):
        return cli.LaunchTarget(
            "ad-hoc", claude_multi.profile.ad_hoc_direct(model), None, False, f"Direct {model}"
        )

    def _fresh(self, model, **kwargs):
        prepared = self.runtime.prepare(
            self._direct(model), action="fresh", passthrough=[], **kwargs
        )
        record = {**prepared.record, "mutation_token": sessions.new_mutation_token()}
        sessions.ensure_state_v4(self.runtime.session_store.root)
        self.runtime.session_store.save(record)
        return prepared, record

    def _resume(self, mid, **kwargs):
        return self.runtime.prepare(
            cli.LaunchTarget("record", None, None, False, "Session"),
            action="resume",
            passthrough=[],
            session_id=mid,
            **kwargs,
        )

    def test_agent_only_line_is_refused_as_a_direct_lead(self) -> None:
        # 3.0 evaluation: a lead needs the lead capability even ad hoc
        # (gpt55 is agents-only in the fixture catalog); 2.x launched it.
        with self.assertRaisesRegex(cli.LaunchPlanError, "'gpt55' lacks the lead/context fields"):
            self._fresh("gpt55")

    def test_direct_fresh_records_null_profile(self) -> None:
        prepared, record = self._fresh("qwen38")
        self.assertEqual(record["applied"]["lead"]["key"], "qwen38")
        self.assertIsNone(record["profile"])
        self.assertFalse(record["follow"])
        self.assertFalse(record["applied"]["no_subagents"])
        self.assertEqual(record["applied"]["agents"], {})
        loaded = self.runtime.session_store.load(record["managed_id"])
        self.assertIsNone(loaded["profile"])

    def test_unknown_model_still_rejected(self) -> None:
        with self.assertRaises((cli.CLIError, claude_multi.profile.ProfileError)):
            self.runtime.prepare(
                self._direct("no-such-model"), action="fresh", passthrough=[]
            )

    def test_no_subagents_fresh_records_and_compiles(self) -> None:
        prepared, record = self._fresh("sol", no_subagents=True)
        self.assertTrue(record["applied"]["no_subagents"])
        self.assertIn("Agent", prepared.result.scope_plan.settings["permissions"]["deny"])
        self.assertEqual(
            prepared.result.env_set["CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS"],
            "1",
        )

    def test_resume_reapplies_recorded_policy_silently(self) -> None:
        _prepared, record = self._fresh("sol", no_subagents=True)
        resumed = self._resume(record["managed_id"])
        self.assertTrue(resumed.record["applied"]["no_subagents"])
        self.assertEqual(
            resumed.result.env_set["CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS"],
            "1",
        )

    def test_resume_accepts_matching_explicit_flag(self) -> None:
        _prepared, record = self._fresh("sol", no_subagents=True)
        resumed = self._resume(record["managed_id"], no_subagents=True)
        self.assertTrue(resumed.record["applied"]["no_subagents"])

    def test_resume_rejects_mismatching_explicit_flag(self) -> None:
        _prepared, record = self._fresh("sol")
        with self.assertRaisesRegex(
            claude_multi.launch.LaunchError, "does not match the session's subagent policy"
        ):
            self._resume(record["managed_id"], no_subagents=True)

    def test_direct_flag_parses_tri_state(self) -> None:
        args = cli.build_parser().parse_args(["direct", "--no-subagents"])
        self.assertTrue(args.no_subagents)
        plain = cli.build_parser().parse_args(["direct"])
        self.assertFalse(hasattr(plain, "no_subagents"))


class RetiredProfileDoctorHintTests(CLITestCase):
    """A retired ordinary profile ('sol' before its catalog retirement) must get the
    re-pin resume remedy, never a --repair hint that would just error."""

    def _stale_sol_record(self):
        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd=self.runtime.cwd,
            model="sol",
            context_profile="sol",  # retired (sol joined large)
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
        )
        _v3.save_any(self.runtime.session_store, record)
        return record

    def test_retired_profile_names_the_repin_resume(self) -> None:
        # A 2.x record is never compiled by doctor; it
        # gets the catalog-free line and no --repair hint (its resume
        # migrates it).
        self._stale_sol_record()
        info, problems, _attention = cli._doctor_scope_report(self.runtime)
        line = next(item for item in info if FIXED_ID[:8] in item)
        self.assertIn("legacy record · lead sol (not checked until migrated)", line)
        self.assertFalse(any("doctor --repair" in item for item in problems))

    def test_healthy_record_keeps_the_repair_hint_shape(self) -> None:
        record = self.save_v4_session(
            target=cli.LaunchTarget(
                "ad-hoc", claude_multi.profile.ad_hoc_direct("sol"), None, False, "Direct sol"
            )
        )
        # No scope on disk: the plain repair hint applies.
        problems, _attention, _info = cli._check_scope_integrity(self.runtime, record)
        self.assertEqual(
            problems,
            [f"session {FIXED_ID[:8]}: scope unreadable; claude-multi doctor --repair {FIXED_ID}"],
        )

@uses_shipped_catalog(rebind="CATALOG_ROOT")
class DeepSeekProPlannedCompositionFilesTests(CLITestCase):
    """Test the exact staged/rollback files used at activation."""

    @property
    def fixture_root(self) -> Path:
        return REPO_ROOT / "tests" / "fixtures" / "compositions"

    def _load(self, path: Path):
        # Catalog 33 retired deepseek-pro/grok46/glm52, so these
        # activation files are historical: schema-validated only, never
        # resolved against the shipped catalog.
        return composition.load_composition_file(path, self.runtime.compositions.schema)

    def test_all_planned_files_load_and_resolve(self) -> None:
        root = self.fixture_root / "024-planned"
        names = set()
        for path in sorted(root.glob("*.json")):
            document = self._load(path)
            names.add(document["name"])
            self.assertEqual(
                sum(1 for slot in document["slots"] if slot["role"] == "cm-lead"), 1
            )
        self.assertEqual(
            names,
            {"deepseek", "deepseek-flash", "grok-deepseek", "sol-qwen-glm-deepseek-flash"},
        )

    def test_planned_pipeline_roles_are_exact(self) -> None:
        root = self.fixture_root / "024-planned"
        deepseek = self._load(root / "deepseek.json")
        preferred = {
            slot["role"]: slot["model"]
            for slot in deepseek["slots"]
            if slot.get("preferred")
        }
        self.assertEqual(
            preferred,
            {
                "cm-analyst": "deepseek-flash",
                "cm-implementer": "deepseek-pro",
                "cm-reviewer": "deepseek-flash",
            },
        )
        self.assertEqual(deepseek["slots"][0]["model"], "deepseek-pro")

        grok = self._load(root / "grok-deepseek.json")
        preferred = {
            slot["role"]: slot["model"]
            for slot in grok["slots"]
            if slot.get("preferred")
        }
        self.assertEqual(
            preferred,
            {
                "cm-analyst": "deepseek-flash",
                "cm-implementer": "deepseek-pro",
                "cm-reviewer": "grok46",
            },
        )
        grok_slots=[s for s in grok["slots"] if s["model"]=="grok46"]
        self.assertTrue(grok_slots)
        for slot in grok_slots:
            if slot["role"] != "cm-lead":
                self.assertEqual(slot.get("lane"),"xhigh")
        self.assertNotIn("grok45",grok["availability"]["models"])

        pool = self._load(root / "sol-qwen-glm-deepseek-flash.json")
        pro_slots = [s for s in pool["slots"] if s["model"] == "deepseek-pro"]
        self.assertEqual(
            {(s["role"], s.get("lane"), s.get("preferred")) for s in pro_slots},
            {
                ("cm-analyst", "max", False),
                ("cm-implementer", "max", False),
                ("cm-reviewer", "max", False),
            },
        )

    def test_catalog18_rollback_files_stay_historical(self) -> None:
        # Rollback files belong to catalog18 and MUST retain grok45. They are
        # schema-validated here, not resolved against catalog19 (where grok45
        # is intentionally absent).
        root = self.fixture_root / "024-rollback-catalog18"
        for path in sorted(root.glob("*.json")):
            document = composition.load_composition_file(
                path, self.runtime.compositions.schema
            )
            self.assertNotIn("deepseek-pro", document["availability"]["models"])
            if path.name in ("deepseek.json", "grok-deepseek.json"):
                self.assertIn("grok45", document["availability"]["models"])
            self.assertNotIn("grok46", document["availability"]["models"])


class Grok46DiscoveryAndMigrationTests(CLITestCase):
    """No active Grok 4.5 route; 4.6 is cataloged and migratable.

    Runs on the frozen fixture, which still carries the grok46 line
    (catalog 33 retired it from the shipped catalog).
    """

    def test_openrouter_discovery_catalogs_46_not_45(self) -> None:
        entries = [
            {"id":"x-ai/grok-4.6","display_name":"Grok 4.6","context_length":500000},
            {"id":"x-ai/grok-4.5","display_name":"Grok 4.5","context_length":500000},
        ]
        self.runtime.listing_transport = (
            lambda url, headers, **_caps: json.dumps({"data": entries}).encode())
        with mock.patch("claude_multi.cli.consent.stdio_ttys", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            code,out=self.run_cli(["discover","openrouter"],"y\n")
        self.assertEqual(code,0)
        self.assertIn("x-ai/grok-4.6\tcataloged as grok46",out)
        self.assertIn("x-ai/grok-4.5\tcandidate",out)

    def _stale_45_record(self):
        record=_v3.make_ordinary_record(
            managed_id=FIXED_ID,runtime_session_id=FIXED_ID,cwd=self.runtime.cwd,
            model="grok46",context_profile="grok",
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
        )
        record["ordinary_model"]="grok45"
        _v3.save_any(self.runtime.session_store, record)
        return record

    def test_doctor_names_grok46_for_stale_45_record(self) -> None:
        # A 2.x record with a retired key gets the
        # catalog-free line; doctor never compiles it (no crash, no hint).
        self._stale_45_record()
        info, problems, _attention = cli._doctor_scope_report(self.runtime)
        line = next(item for item in info if FIXED_ID[:8] in item)
        self.assertIn("legacy record · lead grok45 (not checked until migrated)", line)
        self.assertFalse(any(FIXED_ID in item for item in problems))

    def test_explicit_resume_migrates_stale_45_record_same_profile(self) -> None:
        # `direct -r ID --model grok46` migrates the 2.x
        # record, then relaunches with the explicit lead.
        self._stale_45_record()
        prepared = self.runtime.prepare(
            cli.LaunchTarget("record", None, None, False, "Session"),
            action="resume",
            passthrough=[],
            session_id=FIXED_ID,
            lead_override="grok46",
        )
        self.assertEqual(prepared.record["applied"]["lead"]["key"], "grok46")
        self.assertEqual(prepared.record["lead_class"], "grok")
        self.assertIn("claude-multi-grok46-xhigh", prepared.result.argv)
        self.assertEqual(
            prepared.result.env_set["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "500000"
        )

class NewLineListingFilterTests(CLITestCase):
    """The provider-listing "already cataloged"
    filters read every v2 line, ``status: new`` included — never the v1
    view, which omits New lines. Test-local New lines on a fixture
    copy (grok46 and qwen38 are not named by the frozen composition)."""

    def setUp(self) -> None:
        super().setUp()
        import json as _json

        copy_root = self.root / "assets"
        shutil.copytree(CATALOG_ROOT, copy_root)
        for root_dir, _dirs, files in os.walk(copy_root):
            os.chmod(root_dir, 0o755)
            for name in files:
                os.chmod(Path(root_dir) / name, 0o644)
        models_path = copy_root / "catalog" / "models.json"
        document = _json.loads(models_path.read_text(encoding="utf-8"))
        for key in ("grok46", "qwen38"):
            document["models"][key]["status"] = "new"
        models_path.write_bytes(strict_json.pretty_file_bytes(document))
        self.runtime = cli.Runtime(
            asset_root=copy_root,
            environ=self.runtime.environ,
            cwd=self.root / "project",
            launch_callback=lambda prepared: self.launches.append(prepared) or 0,
            doctor_callback=lambda _runtime: [],
            doctor_binary_callback=lambda _contract: ([], ["fixture"]),
            doctor_daemon_callback=lambda: claude_multi.launch.DaemonStatus(
                state="absent", summary="fixture daemon absent"
            ),
            **self._runtime_seams(),
        )
        # Precondition: the lines are New (the v1 view that omitted them is
        # gone; the status itself is the precondition).
        for key in ("grok46", "qwen38"):
            self.assertEqual(self.runtime.catalog.lines[key]["status"], "new")

    def test_discover_marks_a_new_line_wire_cataloged(self) -> None:
        # Qwen's listing is unsupported (no request); the New grok46
        # line on openrouter shows the same rule.
        wire = self.runtime.catalog.lines["grok46"]["wire_model"]
        entries = [
            {"id": wire, "display_name": "", "context_length": None},
            {"id": "x-ai/fresh", "display_name": "", "context_length": None},
        ]
        self.runtime.listing_transport = (
            lambda url, headers, **_caps: json.dumps({"data": entries}).encode())
        with mock.patch("claude_multi.cli.consent.stdio_ttys", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            code, out = self.run_cli(["discover", "openrouter"], "y\n")
        self.assertEqual(code, 0)
        self.assertIn(f"{wire}\tcataloged as grok46\n", out)
        self.assertIn("x-ai/fresh\tcandidate", out)


class HookProtocol3SessionEventTests(CLITestCase):
    """The protocol-3 SessionStart notice on top of the 2.x reconcile, and the 2.x
    compatibility changes of the hook-dispatch region."""

    GEN_HEAD = "[claude-multi lineup notice · lineup_generation 1]"

    def setUp(self) -> None:
        super().setUp()
        import bless

        self.bless = bless
        self.state_root = self.runtime.session_store.root
        self.plan = bless.v2_scope_plan("managed")
        scope_mod.write_scope(self.state_root, FIXED_ID, self.plan)
        self.gen_bytes = self.plan.other_files[scope_mod.LINEUP_GEN]

    def _payload(self, source="startup", session_id=OTHER_ID, **fields) -> str:
        document = {
            "hook_event_name": "SessionStart",
            "session_id": session_id,
            "source": source,
            "cwd": self.runtime.cwd,
            **fields,
        }
        return strict_json.canonical_bytes(document).decode("utf-8")

    def _event(self, text, *, event="start", protocol=True, epoch=1):
        argv = ["session-event", event, "--managed-id", FIXED_ID, "--launch-epoch", str(epoch)]
        if protocol:
            argv += ["--hook-protocol", "3"]
        error = io.StringIO()
        with mock.patch.object(sys, "stderr", error):
            code, output = self.run_cli(argv, text)
        return code, output, error.getvalue()

    def _context(self, output: str) -> str:
        self.assertEqual(output.count("\n"), 1, output)
        document = strict_json.loads(output)
        self.assertEqual(document["hookSpecificOutput"]["hookEventName"], "SessionStart")
        return document["hookSpecificOutput"]["additionalContext"]

    def _seen(self, rid=OTHER_ID) -> Path:
        return claude_multi.hooks.seen_path(self.state_root, rid)

    def _record_bytes(self) -> bytes:
        return (self.state_root / "sessions" / f"{FIXED_ID}.json").read_bytes()

    def test_every_source_gets_exactly_one_notice(self) -> None:
        self.assertEqual(
            cli._SESSION_START_SOURCES, {"startup", "resume", "compact", "clear", "fork"}
        )
        for index, source in enumerate([*sorted(cli._SESSION_START_SOURCES), "mystery"]):
            with self.subTest(source=source):
                self.save_session(mode="durable", scope_generation=1)
                rid = f"5555555{index}-5555-4555-8555-555555555555"
                code, output, error = self._event(self._payload(source, session_id=rid))
                self.assertEqual(code, 0, error)
                context = self._context(output)
                self.assertTrue(context.startswith(self.GEN_HEAD + "\n"), context[:200])
                self.assertIn("## Review routing", context)
                self.assertEqual(self._seen(rid).read_bytes(), self.gen_bytes)
        # The fast path then stays silent for that runtime id.
        self.assertEqual(
            claude_multi.hooks.read_seen(self.state_root, rid), self.gen_bytes.decode().strip()
        )

    def test_resume_restates_the_lead(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        lead = strict_json.loads(self.plan.other_files[scope_mod.LEAD_SET_JSON])["lead"]
        code, output, _error = self._event(self._payload("resume"))
        self.assertEqual(code, 0)
        context = self._context(output)
        self.assertIn(f"Your recorded lead is {lead['display']} · {lead['effort']}", context)
        self.assertEqual(
            output.encode("utf-8"), self.bless.notice_files()["resume.json"]
        )

    def test_compact_is_notified_and_model_evidence_still_ignored(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        code, output, _error = self._event(
            self._payload("compact", session_id=FIXED_ID, model="gpt-multi-sol-xhigh")
        )
        self.assertEqual(code, 0)
        self.assertIn("compacted", self._context(output))
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertNotIn("observed_model", record)

    def test_fork_marks_the_fork_id_and_carries_the_adopt_text(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        claude_multi.hooks.write_seen(self.state_root, FIXED_ID, self.gen_bytes.decode())
        parent = self._seen(FIXED_ID).read_bytes()
        code, output, _error = self._event(self._payload("fork"))
        self.assertEqual(code, 0)
        context = self._context(output)
        self.assertTrue(context.startswith(self.GEN_HEAD))
        self.assertIn("native fork that shares the parent session's scope", context)
        self.assertIn("does not yet have an independent durable", context)
        self.assertEqual(self._seen(OTHER_ID).read_bytes(), self.gen_bytes)
        self.assertEqual(self._seen(FIXED_ID).read_bytes(), parent)
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(record["pending_forks"][0]["session_id"], OTHER_ID)

    def test_reconcile_no_op_and_failure_keep_the_notice(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        record["launch_epoch"] = 5
        _v3.save_any(self.runtime.session_store, record)
        before = self._record_bytes()
        code, output, _error = self._event(self._payload("startup"), epoch=1)
        self.assertEqual(code, 0)
        self.assertTrue(self._context(output).startswith(self.GEN_HEAD))
        self.assertEqual(self._record_bytes(), before)  # stale epoch: a no-op
        # A reconcile failure: notice + one failure line, exit 0, no marker.
        self._seen().unlink()
        with mock.patch('claude_multi.cli.session_events._reconcile_session_start',
            side_effect=sessions.SessionError("17 pending forks"),
        ):
            code, output, error = self._event(self._payload("fork"))
        self.assertEqual(code, 0)
        context = self._context(output)
        self.assertTrue(context.startswith(self.GEN_HEAD))
        self.assertIn(
            "claude-multi could not record this session start: 17 pending forks. "
            "Run claude-multi doctor.",
            context,
        )
        self.assertIn("session start not recorded", error)
        self.assertFalse(self._seen().exists())

    def test_invalid_json_still_notifies_without_marker(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        code, output, error = self._event("{not json")
        self.assertEqual(code, 0, error)
        context = self._context(output)
        self.assertTrue(context.startswith(self.GEN_HEAD))
        self.assertIn("could not record this session start", context)
        notice_dir = self.state_root / "notice"
        self.assertEqual(sorted(notice_dir.glob("*")) if notice_dir.exists() else [], [])

    def test_stale_marker_is_cleared_before_anything_else(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        claude_multi.hooks.write_seen(self.state_root, OTHER_ID, self.gen_bytes.decode())
        with mock.patch.object(claude_multi.hooks, "read_notice", side_effect=RuntimeError("x")):
            code, _output, _error = self._event(self._payload("compact"))
        self.assertEqual(code, 0)
        # No notice was delivered, so the next prompt must miss the fast path.
        self.assertFalse(self._seen().exists())

    def test_runtime_failure_still_delivers_the_notice(self) -> None:
        # Runtime(...) raising (catalog load) on a v3
        # compact: notice JSON, exit 0, no marker; the next prompt re-notifies.
        self.save_session(mode="durable", scope_generation=1)
        output, error = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.runtime.environ, clear=True), mock.patch('claude_multi.cli.runtime.Runtime', side_effect=claude_multi.catalog.CatalogError("catalog broken")
        ) as runtime, mock.patch.object(sys, "stderr", error):
            code = cli.main(
                ["session-event", "start", "--managed-id", FIXED_ID, "--launch-epoch", "1",
                 "--hook-protocol", "3"],
                input_stream=io.StringIO(self._payload("compact")),
                output_stream=output,
            )
            runtime.assert_called_once()
            self.assertEqual(code, 0, error.getvalue())
            context = self._context(output.getvalue())
            self.assertTrue(context.startswith(self.GEN_HEAD))
            self.assertIn("catalog broken", context)
            self.assertFalse(self._seen().exists())
            prompt_out = io.StringIO()
            code = cli.main(
                ["session-event", "prompt", "--managed-id", FIXED_ID, "--launch-epoch", "1",
                 "--hook-protocol", "3"],
                input_stream=io.StringIO(self._payload("compact")),
                output_stream=prompt_out,
            )
        self.assertEqual(code, 0)
        self.assertIn("UserPromptSubmit", prompt_out.getvalue())
        self.assertEqual(self._seen().read_bytes(), self.gen_bytes)

    def test_migration_lock_skips_the_record_not_the_notice(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        before = self._record_bytes()
        lock = state.FileLock(self.state_root / claude_multi.hooks.MIGRATION_LOCK_TARGET)
        lock.acquire()
        try:
            code, output, error = self._event(self._payload("startup"))
            self.assertEqual(code, 0)
            self.assertTrue(self._context(output).startswith(self.GEN_HEAD))
            self.assertIn("migration in progress", error)
            for event, text in (
                ("start", self._payload("startup")),
                ("end", strict_json.canonical_bytes(
                    {"hook_event_name": "SessionEnd", "session_id": FIXED_ID, "reason": "exit"}
                ).decode()),
            ):
                with self.subTest(two_x=event):
                    code, output, error = self._event(text, event=event, protocol=False)
                    self.assertEqual((code, output), (0, ""))
                    self.assertIn("migration in progress", error)
        finally:
            lock.release()
        self.assertEqual(self._record_bytes(), before)

    def test_damaged_scope_falls_back_to_two_x_errors(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        (self.state_root / "scopes" / FIXED_ID / scope_mod.LINEUP_GEN).unlink()
        code, output, error = self._event(self._payload("startup"))
        self.assertEqual((code, output), (0, ""))
        self.assertIn("no lineup notice", error)
        self.assertEqual(
            self.runtime.session_store.load(FIXED_ID)["runtime_session_id"], OTHER_ID
        )
        code, output, error = self._event("{not json")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("unreadable SessionStart payload", error)

    def test_two_x_start_on_a_v2_scope_has_no_notice(self) -> None:
        self.save_session(mode="durable", scope_generation=1)
        code, output, _error = self._event(self._payload("startup"), protocol=False)
        self.assertEqual((code, output), (0, ""))
        self.assertFalse((self.state_root / "notice").exists())

    def test_two_x_errors_go_to_stderr_with_exit_one(self) -> None:
        # Hook failures go to stderr with a nonzero exit, never to stdout.
        self.save_session(mode="durable", scope_generation=1)
        for event, text, expected in (
            ("start", "{not json", "invalid session hook JSON"),
            ("start", self._payload("bogus-source"), "unknown SessionStart source"),
            ("end", strict_json.canonical_bytes({"session_id": FIXED_ID}).decode(), "reason"),
        ):
            with self.subTest(event=event, expected=expected):
                code, output, error = self._event(text, event=event, protocol=False)
                self.assertEqual((code, output), (1, ""))
                self.assertIn(expected, error)

    def test_v3_end_records_like_two_x(self) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        code, output, _error = self._event(
            strict_json.canonical_bytes(
                {"hook_event_name": "SessionEnd", "session_id": FIXED_ID, "reason": "exit"}
            ).decode(),
            event="end",
            epoch=record.get("launch_epoch", 0),
        )
        self.assertEqual((code, output), (0, ""))
        self.assertEqual(self.runtime.session_store.load(FIXED_ID)["last_event_source"], "end")


class HookModelEquivalenceV3Tests(CLITestCase):
    """Lead and ordinary equivalence through the
    retired map (resolve_selector/last_wire), on test-local in-memory
    catalog edits derived from the fixture (no id pinned)."""

    def setUp(self) -> None:
        super().setUp()
        docs = self.runtime.catalog.docs
        saved = {name: copy.deepcopy(docs[name]) for name in ("models-v2", "retired")}

        def restore() -> None:
            for name, value in saved.items():
                docs[name] = value

        self.addCleanup(restore)

    def _anthropic_line(self) -> tuple[str, dict]:
        for key, entry in sorted(self.runtime.catalog.lines.items()):
            if (
                entry["provider"] == "anthropic"
                and isinstance(entry["efforts"], list)
                and entry["context"]["client_tokens"] >= 1_000_000
                and "lead" in entry["capabilities"]
            ):
                return key, entry
        self.fail("fixture has no 1M client-effort Anthropic lead line")

    def _managed(self, key: str, selector: str) -> None:
        record = self.save_session(mode="durable", scope_generation=1)
        record["snapshot"]["lead"]["model"] = key
        record["snapshot"]["lead"]["client_selector"] = selector
        record["composition_hash"] = strict_json.bundle_digest(record["snapshot"])
        _v3.save_any(self.runtime.session_store, record)

    def _start(self, model: str):
        payload = strict_json.canonical_bytes(
            {
                "hook_event_name": "SessionStart",
                "session_id": OTHER_ID,
                "source": "resume",
                "cwd": self.runtime.cwd,
                "model": model,
            }
        ).decode("utf-8")
        return self.run_cli(["session-event", "start", "--managed-id", FIXED_ID], payload)

    def _retire(self, key: str, *, last_wire: str, selector: str, successor) -> None:
        self.runtime.catalog.docs["retired"]["retired"][key] = {
            "capabilities": ["lead", "agents"],
            "context_tokens": 1_000_000,
            "display": "Test-local retired line",
            "last_wire": last_wire,
            "provider": "anthropic",
            "reason": "test-local",
            "roles": "all",
            "selectors": {selector: None},
            "since_catalog": 99,
            "successor": successor,
        }

    def test_same_key_generation_move(self) -> None:
        key, entry = self._anthropic_line()
        old_selector, old_wire = entry["selector"], entry["wire_model"]
        entry["wire_model"] = old_wire + "-next"
        entry["selector"] = f"claude-next-{key}[1m]"
        self._retire(f"{key}@old", last_wire=old_wire, selector=old_selector, successor=key)
        self._managed(key, old_selector)
        for model in (old_wire + "[1m]", old_wire, old_selector):
            with self.subTest(model=model):
                code, output = self._start(model)
                self.assertEqual(code, 0, output)
                record = self.runtime.session_store.load(FIXED_ID)
                self.assertNotIn("observed_model", record)
                self.assertNotEqual(record["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)

    def test_renamed_key(self) -> None:
        key, _entry = self._anthropic_line()
        self._retire(
            "renamed-lead", last_wire="claude-renamed-9", selector="claude-renamed-9-sel[1m]",
            successor=key,
        )
        self._managed("renamed-lead", "claude-renamed-9-sel[1m]")
        code, output = self._start("claude-renamed-9[1m]")
        self.assertEqual(code, 0, output)
        self.assertNotIn("observed_model", self.runtime.session_store.load(FIXED_ID))

    def test_fixture_retired_key_and_total_miss_never_crash(self) -> None:
        retired_key, retired = sorted(self.runtime.catalog.retired.items())[0]
        selector = sorted(retired["selectors"])[0]
        self._managed(retired_key, selector)
        code, output = self._start(retired["last_wire"] + "[1m]")
        self.assertEqual(code, 0, output)
        self.assertNotIn("observed_model", self.runtime.session_store.load(FIXED_ID))
        self._managed("no-such-key", "no-such-selector")
        code, output = self._start("something-else")
        self.assertEqual(code, 0, output)
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(record["observed_model"], "something-else")

    def test_genuine_switch_is_observed(self) -> None:
        key, entry = self._anthropic_line()
        self._managed(key, entry["selector"])
        other = next(
            e["wire_model"] for k, e in sorted(self.runtime.catalog.lines.items()) if k != key
        )
        code, output = self._start(other)
        self.assertEqual(code, 0, output)
        record = self.runtime.session_store.load(FIXED_ID)
        self.assertEqual(record["observed_model"], other)
        self.assertEqual(record["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)

    def test_ordinary_record_on_a_retired_key_keeps_its_model(self) -> None:
        retired_key, retired = sorted(self.runtime.catalog.retired.items())[0]
        record = _v3.make_ordinary_record(
            managed_id=FIXED_ID,
            runtime_session_id=FIXED_ID,
            cwd=self.runtime.cwd,
            model=retired_key,
            context_profile="large",
            catalog_version=self.runtime.catalog_version,
            catalog_hash=self.runtime.catalog.bundle_sha256,
            launcher_version=self.runtime.launcher_version,
        )
        _v3.save_any(self.runtime.session_store, record)
        for model in (sorted(retired["selectors"])[0], retired["last_wire"]):
            with self.subTest(model=model):
                code, output = self._start(model)
                self.assertEqual(code, 0, output)
                updated = self.runtime.session_store.load(FIXED_ID)
                self.assertEqual(updated["ordinary_model"], retired_key)
                self.assertEqual(updated["context_profile"], "large")
                self.assertNotIn("observed_model", updated)

    def test_v3_ordinary_start_with_a_model_reconciles_after_the_view_deletion(self) -> None:
        # The v3 ordinary branch resolves the
        # reported model on the merged v2 lines.  The v1 view is blanked here;
        # the reconcile still maps every form of a live lead line.
        # (docs["models"] is the raw v2 alias, so blanking that key proves
        # the reconcile reads models-v2 / the lineup catalog.)
        key, entry = next(
            (k, e) for k, e in self.runtime.lineup_catalog().lines.items()
            if e.get("status", "active") != "new"
            and "lead" in e["capabilities"]
            and e["context"].get("ordinary_profile") is not None
        )
        profile_name = entry["context"]["ordinary_profile"]
        docs = self.runtime.catalog.docs
        view = docs["models"]
        self.addCleanup(docs.__setitem__, "models", view)
        docs["models"] = {**view, "models": {}}
        forms = [entry["wire_model"], *(s for _e, s, _c in catalog.line_selectors(entry))]
        if entry["context"]["client_tokens"] >= 1_000_000:
            forms.append(entry["wire_model"] + "[1m]")
        for model in forms:
            with self.subTest(model=model):
                record = _v3.make_ordinary_record(
                    managed_id=FIXED_ID,
                    runtime_session_id=FIXED_ID,
                    cwd=self.runtime.cwd,
                    model=key,
                    context_profile=profile_name,
                    catalog_version=self.runtime.catalog_version,
                    catalog_hash=self.runtime.catalog.bundle_sha256,
                    launcher_version=self.runtime.launcher_version,
                )
                _v3.save_any(self.runtime.session_store, record)
                code, output = self._start(model)
                self.assertEqual(code, 0, output)
                updated = self.runtime.session_store.load(FIXED_ID)
                self.assertEqual(updated["ordinary_model"], key)
                self.assertEqual(updated["context_profile"], profile_name)
                self.assertNotIn("observed_model", updated)
                self.assertNotEqual(updated["identity_state"], sessions.IDENTITY_REPAIR_NEEDED)


class DirectSelectorResolverTests(CLITestCase):
    """``cli._direct_model_for_selector`` on the merged v2 lines.

    The resolver expectations of ``tests/test_compiler.py`` (the 2.x
    ``compiler.direct_model_for_selector`` cases: wire, client and gateway
    selectors, the ``wire[1m]`` report form of a 1M lead, profile-less lines
    with a null profile, unknown ids), derived from the fixture instead of
    pinned.
    """

    def _live(self) -> dict:
        return {
            key: entry
            for key, entry in self.runtime.lineup_catalog().lines.items()
            if entry.get("status", "active") != "new"
        }

    @staticmethod
    def _selectors(entry) -> list[str]:
        return [selector for _effort, selector, _contract in catalog.line_selectors(entry)]

    @staticmethod
    def _profiled_lead(entry) -> bool:
        return "lead" in entry["capabilities"] and entry["context"].get("ordinary_profile") is not None

    def _forms(self) -> dict[str, set[str]]:
        forms = {}
        for key, entry in self._live().items():
            forms[key] = {entry["wire_model"], *self._selectors(entry)}
            if self._profiled_lead(entry) and entry["context"]["client_tokens"] >= 1_000_000:
                forms[key].add(entry["wire_model"] + "[1m]")
        return forms

    def test_every_form_of_a_profiled_lead_line_resolves_to_its_profile(self) -> None:
        forms = self._forms()
        owners: dict[str, list[str]] = {}
        for key, keyed in forms.items():
            for form in keyed:
                owners.setdefault(form, []).append(key)
        self.assertFalse(
            [form for form, keys in owners.items() if len(keys) > 1],
            "fixture forms are unique across lines",
        )
        live = self._live()
        profiled = [key for key, entry in live.items() if self._profiled_lead(entry)]
        self.assertTrue(profiled)
        kinds = {isinstance(live[key]["efforts"], dict) for key in profiled}
        self.assertEqual(kinds, {True, False}, "client- and gateway-effort lead lines")
        self.assertTrue(any(live[k]["context"]["client_tokens"] >= 1_000_000 for k in profiled))
        self.assertTrue(any(live[k]["context"]["client_tokens"] < 1_000_000 for k in profiled))
        for key in profiled:
            entry = live[key]
            for form in sorted(forms[key]):
                with self.subTest(key=key, form=form):
                    self.assertEqual(
                        cli._direct_model_for_selector(self.runtime, form),
                        (key, entry["context"]["ordinary_profile"]),
                    )
            if entry["context"]["client_tokens"] < 1_000_000:
                # A sub-1M line never self-classifies through a [1m] report.
                self.assertIsNone(cli._direct_model_for_selector(self.runtime, entry["wire_model"] + "[1m]"))

    def test_profile_less_lines_resolve_with_a_null_profile(self) -> None:
        live = self._live()
        single = sorted(key for key, entry in live.items() if not self._profiled_lead(entry))
        self.assertTrue(single, "the fixture has a profile-less line")
        for key in single:
            entry = live[key]
            for form in sorted({entry["wire_model"], *self._selectors(entry)}):
                with self.subTest(key=key, form=form):
                    self.assertEqual(cli._direct_model_for_selector(self.runtime, form), (key, None))

    def test_unknown_and_retired_forms_never_resolve(self) -> None:
        live_forms = set().union(*self._forms().values())
        self.assertIsNone(cli._direct_model_for_selector(self.runtime, "unknown-provider-model"))
        retired = self.runtime.lineup_catalog().retired
        self.assertTrue(retired)
        for key, entry in sorted(retired.items()):
            for form in sorted({entry["last_wire"], *entry["selectors"]} - live_forms):
                with self.subTest(retired=key, form=form):
                    self.assertIsNone(cli._direct_model_for_selector(self.runtime, form))

    def test_a_new_line_is_invisible(self) -> None:
        docs = self.runtime.catalog.docs
        saved = copy.deepcopy(docs["models-v2"])
        self.addCleanup(docs.__setitem__, "models-v2", saved)
        key, entry = next((k, e) for k, e in self._live().items() if self._profiled_lead(e))
        forms = self._forms()[key]
        docs["models-v2"] = copy.deepcopy(saved)
        docs["models-v2"]["models"][key]["status"] = "new"
        for form in sorted(forms):
            with self.subTest(form=form):
                self.assertIsNone(cli._direct_model_for_selector(self.runtime, form))

    def test_a_custom_lead_line_resolves_through_the_merged_lines(self) -> None:
        provider = next(
            pid for pid, p in sorted(self.runtime.catalog.providers.items())
            if p["transport"]["kind"] == "direct"
        )
        claude_multi.custom.add_model(
            self.runtime.environ, "resolver-custom", wire_model="resolver-custom-wire",
            provider=provider, context_tokens=262144, created_via="manual",
            catalog_providers=self.runtime.catalog.providers,
        )
        entry = self.runtime.lineup_catalog().lines["resolver-custom"]
        expected = ("resolver-custom", entry["context"]["ordinary_profile"])
        for form in sorted({entry["wire_model"], *self._selectors(entry)}):
            with self.subTest(form=form):
                self.assertEqual(cli._direct_model_for_selector(self.runtime, form), expected)

class CompositionStoreSideEffectTests(CLITestCase):
    """The read-only 2.x ``CompositionStore`` creates nothing."""

    def _read_only_runtime(self) -> cli.Runtime:
        environ = {**self.runtime.environ, "XDG_CONFIG_HOME": str(self.root / "empty-config")}
        return cli.Runtime(
            asset_root=CATALOG_ROOT,
            environ=environ,
            cwd=self.root / "project",
            allow_state_writes=False,
            launch_callback=lambda prepared: self.launches.append(prepared) or 0,
            **self._runtime_seams(),
        )

    def _cli(self, runtime: cli.Runtime, *argv: str) -> tuple[int, str]:
        """``(exit, stdout then stderr)``."""

        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = cli.main(list(argv), runtime=runtime, input_stream=None, output_stream=output, interactive=False)
        return code, output.getvalue() + errors.getvalue()

    def test_a_read_only_runtime_creates_neither_the_config_root_nor_compositions(self) -> None:
        runtime = self._read_only_runtime()
        config_root = claude_multi.sessions.config_root(runtime.environ)
        compositions = config_root / "compositions"
        self.assertEqual(runtime.compositions.compositions_dir, compositions)
        self.assertFalse(config_root.exists())
        code, output = self._cli(runtime, "profile", "show", "nosuch")
        self.assertEqual(code, 1)
        self.assertIn("profile 'nosuch' does not exist", output)
        self.assertFalse(config_root.exists())
        code, output = self._cli(runtime, "profile", "show", "default")
        self.assertEqual(code, 1)
        self.assertIn(cli.PROFILE_IS_COMPOSITION.format(name="default"), output)
        self.assertFalse(config_root.exists())
        # The store is seedless, so an absent directory lists nothing
        # ("default" stays a legacy name through IMPLICIT_2X_NAMES).
        self.assertEqual(runtime.compositions.names(), [])
        self.assertFalse(config_root.exists())
        self.assertFalse(os.path.lexists(compositions))

    def test_a_file_only_2x_name_is_r20_and_nothing_else_appears(self) -> None:
        runtime = self._read_only_runtime()
        config_root = claude_multi.sessions.config_root(runtime.environ)
        path = _write_composition(runtime, {**_v3.default_composition(), "name": "file-only"})
        before = sorted(p.relative_to(config_root) for p in config_root.rglob("*"))
        self.assertEqual(before, [Path("compositions"), path.relative_to(config_root)])
        code, output = self._cli(runtime, "profile", "show", "file-only")
        self.assertEqual(code, 1)
        self.assertIn(cli.PROFILE_IS_COMPOSITION.format(name="file-only"), output)
        self.assertEqual(runtime.compositions.names(), ["file-only"])
        self.assertEqual(sorted(p.relative_to(config_root) for p in config_root.rglob("*")), before)

    def test_r20_still_prints_after_the_view_deletion(self) -> None:
        # The store is seedless and
        # the catalog has no default composition, yet --composition keeps
        # its refusal text for the implicit 2.x name and for a file-only name.
        self.assertFalse(hasattr(self.runtime.catalog, "default_composition"))
        self.assertEqual(cli.IMPLICIT_2X_NAMES, frozenset({"default"}))
        _write_composition(self.runtime, {**_v3.default_composition(), "name": "file-only"})
        alias_note = (
            "claude-multi: '--composition' is the earlier spelling of '--profile'; "
            "the alias stays accepted"
        )
        for name in ("default", "file-only"):
            with self.subTest(name=name):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code, output = self.run_cli(["--composition", name], interactive=False)
                self.assertEqual(code, 1, output)
                self.assertIn(alias_note, stderr.getvalue())
                self.assertIn(cli.PROFILE_IS_COMPOSITION.format(name=name), stderr.getvalue())
                self.assertEqual(self.launches, [])
        with self.assertRaisesRegex(cli.CLIError, "composition 'default' does not exist"):
            self.runtime.compositions.load("default")

    def test_the_writers_are_gone(self) -> None:
        for name in ("save", "delete", "duplicate", "rename", "require_new_target", "restore_default"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(cli.CompositionStore, name))


class CustomRemovalApplyTests(CLITestCase):
    def test_header_violations_are_reported_and_provider_removal_names_cascaded_models(self) -> None:
        from tests.test_custom_registry import registry_fixture

        registry = registry_fixture()
        claude_multi.custom.save_registry(self.runtime.environ, registry)
        self.assertEqual(self.runtime.custom_header_violations(), [("acme", "Authorization")])
        self.assertNotIn("acme", self.runtime.ordinary_docs["providers"]["providers"])
        self.assertNotIn("acme-one", self.runtime.lineup_catalog().lines)
        code, output = self.run_cli(["custom", "remove-provider", "acme"])
        self.assertEqual(code, 0, output)
        self.assertEqual(output,
            f"Removed: acme — apply with {cli.APPLY_GATEWAY_COMMAND}\n"
            "Removed custom models: acme-one\n")
        self.assertEqual(self.runtime.custom_header_violations(), [])
        self.assertIn("clean-one", claude_multi.custom.load_registry(self.runtime.environ)["models"])
        code, output = self.run_cli_err(["custom", "remove-provider", "clean"])
        self.assertEqual(code, 1, output)
        self.assertIn("remove them first", output)

    def test_removal_names_apply_command_but_not_found_stays_unchanged(self) -> None:
        for kind in ("provider", "model"):
            for removed in (True, False):
                with self.subTest(kind=kind, removed=removed):
                    result = (removed, ()) if kind == "provider" else removed
                    with mock.patch.object(claude_multi.custom, f"remove_{kind}", return_value=result):
                        code, output, errors = self.run_cli_both(["custom", f"remove-{kind}", "fixture-entry"])
                    self.assertEqual(code, 0 if removed else 1)
                    self.assertEqual((output, errors),
                                     (f"Removed: fixture-entry — apply with {cli.APPLY_GATEWAY_COMMAND}\n", "")
                                     if removed else ("", "claude-multi: not found: fixture-entry; nothing was removed\n"))


class UpdateCommandTests(CLITestCase):
    def test_update_outside_an_installed_release_says_how_and_refuses(self) -> None:
        from claude_multi import upgrade
        with mock.patch.object(upgrade, "run_repin", side_effect=AssertionError("never the re-pin flow")):
            for argv, needle in ((["update"], "not installed by the claude-multi installer"),
                                 (["update", "--check"], "not installed by the claude-multi installer"),
                                 (["update", "--rollback", "--yes"], "not installed by the claude-multi installer")):
                with self.subTest(argv=argv):
                    code, output = self.run_cli(argv, interactive=False)
                    self.assertEqual(code, 1, output)
                    self.assertIn(needle, output)
                    self.assertIn("Nothing was changed", output)
            with mock.patch.dict(self.runtime.environ, {"CLAUDE_MULTI_CHANNEL": "nix"}):
                code, output = self.run_cli(["update"], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("update it through your flake", output)

    def test_update_flags(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["update", "--check", "--from-dir", "/r", "--base-url", "https://e.invalid/x"])
        self.assertEqual((args.update_check, args.update_rollback, args.update_yes, args.update_from_dir,
                          args.update_base_url), (True, False, False, "/r", "https://e.invalid/x"))
        self.assertTrue(parser.parse_args(["update", "--rollback", "-y"]).update_yes)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["update", "--check", "--rollback"])

    def test_old_flags_are_gone(self) -> None:
        parser = cli.build_parser()
        for argv in (["update", "--manifest-dir", "x"], ["update", "--activate"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_setup_parser(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["setup", "--step", "claude", "--claude-from", "/x/claude"])
        self.assertEqual((args.command, args.step, str(args.claude_from)), ("setup", "claude", "/x/claude"))
        # Bare setup runs every step that is not done; --step names one.
        self.assertIsNone(parser.parse_args(["setup"]).step)
        args = parser.parse_args(["setup", "--status"])
        self.assertTrue(args.setup_status)
        args = parser.parse_args(["setup", "--step", "gateway", "--proxy", "http://proxy.example.invalid:3128"])
        self.assertEqual((args.step, args.proxy, args.no_proxy), ("gateway", "http://proxy.example.invalid:3128", False))
        for argv in (["setup", "--step", "other"], ["setup", "--step", "gateway", "--proxy", "x", "--no-proxy"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(argv)
        # One registration: the command list names setup once.
        commands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        self.assertEqual([choice.dest for choice in commands._choices_actions].count("setup"), 1)

    def test_setup_refuses_an_option_of_the_other_step(self) -> None:
        for argv, flag in ((["setup", "--step", "claude", "--proxy", "http://proxy.example.invalid:3128"], "--proxy"),
                           (["setup", "--step", "claude", "--no-proxy"], "--no-proxy"),
                           (["setup", "--step", "gateway", "--claude-from", "/x/claude"], "--claude-from")):
            err = io.StringIO()
            with self.subTest(argv=argv), contextlib.redirect_stderr(err), \
                    mock.patch("claude_multi.cli.commands.setup.claude_step",
                               side_effect=AssertionError("claude step")), \
                    mock.patch("claude_multi.cli.commands.gateway.setup_command",
                               side_effect=AssertionError("gateway step")):
                code, _output = self.run_cli(argv, interactive=False)
                self.assertEqual(code, 2)
                self.assertIn(f"{flag} belongs to another step", err.getvalue())

    def test_setup_routes_each_step_to_its_handler(self) -> None:
        seen = []
        with mock.patch("claude_multi.cli.commands.setup.claude_step",
                        side_effect=lambda *_a, **k: seen.append(("claude", k["claude_from"])) or 0), \
                mock.patch("claude_multi.cli.commands.gateway.setup_command",
                           side_effect=lambda _r, args, **_k: seen.append(("gateway", args.proxy)) or 0):
            self.assertEqual(self.run_cli(["setup", "--step", "claude"], interactive=False)[0], 0)
            self.assertEqual(self.run_cli(["setup", "--step", "gateway", "--proxy", "http://p.example.invalid:1"],
                                          interactive=False)[0], 0)
            # A step's own option selects it.
            self.assertEqual(self.run_cli(["setup", "--claude-from", "/x/claude"], interactive=False)[0], 0)
            self.assertEqual(self.run_cli(["setup", "--no-proxy"], interactive=False)[0], 0)
        self.assertEqual(seen, [("claude", None), ("gateway", "http://p.example.invalid:1"),
                                ("claude", Path("/x/claude")), ("gateway", None)])


class CarryInScreenAndLivenessTests(CLITestCase):
    def test_models_and_providers_before_launch_ownership_matrix(self):
        for kind, detail in (("ours", "fixture service"), ("foreign", "another user (uid 9000)"),
                             ("unknown", "another process of yours"), ("unknown", "ownership unreadable")):
            with self.subTest(kind=kind, detail=detail):
                runtime = self.runtime
                runtime.listener_owner = lambda _base, k=kind, d=detail: claude_multi.service.OwnerVerdict(k, d)
                transport = mock.Mock(return_value=(self.served, 200))
                runtime.served_models_callback = transport
                # Candidates observe the local served set behind the same ownership guard;
                # only a proven gateway gets the token.
                cli._ModelsScreen(runtime, palette=claude_multi.tui.MONO_PALETTE)
                if kind == "ours":
                    transport.assert_called_once()
                else:
                    transport.assert_not_called()
                # The model-launch picker and Providers do make authenticated requests.
                direct = cli._DirectScreen(runtime, palette=claude_multi.tui.MONO_PALETTE)
                providers = cli._ProvidersScreen(runtime, palette=claude_multi.tui.MONO_PALETTE,
                                                  journal=lambda: None)
                self.assertEqual(len(self.launches), 0)
                if kind == "ours":
                    self.assertEqual(transport.call_count, 3)
                    self.assertEqual(direct.served, self.served)
                    self.assertFalse(runtime.gateway_attention)
                else:
                    transport.assert_not_called()
                    self.assertIsNone(direct.served)
                    self.assertIn("Attention", direct.message)
                    self.assertIn("token was not sent", providers._banner())

    def test_no_listener_keeps_the_down_remedy_in_both_screens(self):
        proc = self.root / "empty-proc"
        (proc / "net").mkdir(parents=True)
        for table in ("tcp", "tcp6"):
            (proc / "net" / table).write_text("sl local_address rem_address st ...\n")
        self.runtime.listener_owner = lambda base: claude_multi.service.listener_owner(base, proc_root=proc)
        self.served = None
        direct = cli._DirectScreen(self.runtime, palette=claude_multi.tui.MONO_PALETTE)
        providers = cli._ProvidersScreen(self.runtime, palette=claude_multi.tui.MONO_PALETTE,
                                         journal=lambda: None)
        self.assertTrue(providers.gateway_down)
        self.assertEqual(self.runtime.gateway_attention, [])
        for text in (direct.message, providers._banner()):
            self.assertIn("gateway is DOWN — served counts unknown; start it with", text)
            self.assertIn(cli.gateway_service_hint("recover"), text)
            self.assertNotIn("ownership", text)
        self.runtime.gateway_attention = ["fixture ownership unknown"]
        self.assertTrue(providers._banner().startswith("gateway is DOWN"))
        self.assertIn("Attention: fixture ownership unknown", providers._banner())

    def test_force_help_uses_the_specified_liveness_wording(self):
        for command, expected in (
            ("forget", "forget even when background liveness cannot be determined"),
            ("stop", "send claude stop even when background liveness cannot be determined"),
        ):
            with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["sessions", command, "--help"])
            self.assertIn(expected, " ".join(output.getvalue().split()))

    def test_unknown_liveness_refuses_forget_stop_and_force_is_explicit(self):
        self.save_session()
        self.runtime.background_liveness = lambda: sessions.BackgroundLiveness(False, frozenset(), "PermissionError")
        code, output = self.run_cli_err(["sessions", "forget", FIXED_ID, "--yes"], interactive=False)
        self.assertEqual(code, 1)
        self.assertIn(f"sessions forget {FIXED_ID} --force", output)
        self.assertTrue(self.runtime.session_store.exists(FIXED_ID))
        code, output = self.run_cli_err(["sessions", "stop", FIXED_ID, "--yes"], interactive=False)
        self.assertIn(f"sessions stop {FIXED_ID} --force", output)
        with mock.patch('claude_multi.cli.session_actions._stop_runtime', return_value=None) as stop:
            code, output = self.run_cli(["sessions", "stop", FIXED_ID, "--yes", "--force"], interactive=False)
            stop.assert_called_once()
        code, output = self.run_cli(["sessions", "forget", FIXED_ID, "--force", "--yes"], interactive=False)
        self.assertEqual(code, 0, output)
        self.assertFalse(self.runtime.session_store.exists(FIXED_ID))

    def test_force_never_bypasses_self_refusal(self):
        record = self.save_session()
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = FIXED_ID
        self.runtime.background_liveness = lambda: sessions.BackgroundLiveness(False, frozenset(), "unreadable")
        self.assertIn("running inside", cli._stop_precheck(self.runtime, record, force=True))
        self.assertIn("running inside", cli._forget_liveness_guard(self.runtime, FIXED_ID, force=True)(record))


class RuntimePoolStatusTests(CLITestCase):
    def setUp(self):
        super().setUp()
        from claude_multi import management
        from tests.test_quota import FIXTURES
        management.prepare_start(self.runtime.home, stopped=True)
        self.runtime.environ[management.CHANNEL_ENV] = management.MANAGEMENT_CHANNEL
        self.body = (FIXTURES / "auth-files-mixed.json").read_bytes()
        self.getter = mock.Mock(return_value=(200, self.body))

    def test_seam_suppression_with_selected_key_and_no_host_reads(self):
        from tests import _tripwire
        before = len(_tripwire.HITS)
        with mock.patch.object(claude_multi.management, "pool_status", side_effect=AssertionError("live management")):
            self.assertEqual(self.runtime.pool_status().state, "seam")
        self.assertEqual(len(_tripwire.HITS), before)

    def test_ttl_caches_success_and_failures_and_does_not_bypass_on_refresh(self):
        self.runtime.management_callback = self.getter
        ticks = [100.0]
        self.runtime.pool_clock = lambda: ticks[0]
        for code, expected in ((200, "ok"), (401, "mismatch")):
            self.getter.return_value = (code, self.body)
            self.getter.reset_mock()
            self.runtime._pool_cache = None
            first = self.runtime.pool_status()
            ticks[0] += 59
            self.assertIs(self.runtime.pool_status(), first)
            self.assertEqual(first.state, expected)
            self.getter.assert_called_once()
            ticks[0] += 2
            self.assertEqual(self.runtime.pool_status().state, expected)
            self.assertEqual(self.getter.call_count, 2)

    def test_owner_and_attention_injection(self):
        self.runtime.management_callback = self.getter
        self.runtime.listener_owner = mock.Mock(return_value=claude_multi.service.OwnerVerdict("foreign", "fixture"))
        self.assertEqual(self.runtime.pool_status().state, "not-ours")
        self.getter.assert_not_called()
        self.runtime._pool_cache = None
        self.runtime.listener_owner.return_value = claude_multi.service.OwnerVerdict("unknown", "fixture")
        self.runtime.management_attention = mock.Mock()
        self.assertEqual(self.runtime.pool_status().state, "ok")
        self.runtime.management_attention.assert_called_once()
        self.getter.assert_called_once()

    def test_pool_provider_map_is_catalog_driven(self):
        expected = {p["transport"]["pool"]: name
                    for name, p in self.runtime.ordinary_docs["providers"]["providers"].items()
                    if p["transport"]["kind"] == "oauth-pool"}
        self.assertEqual(self.runtime.pool_providers(), expected)


class QuotaCommandTests(CLITestCase):
    def setUp(self):
        super().setUp()
        from claude_multi import management
        from tests.test_quota import FIXTURES
        management.prepare_start(self.runtime.home, stopped=True)
        self.runtime.environ[management.CHANNEL_ENV] = management.MANAGEMENT_CHANNEL
        self.getter = mock.Mock(return_value=(200, (FIXTURES / "auth-files-mixed.json").read_bytes()))
        self.runtime.management_callback = self.getter
        self.runtime.served_models_callback = mock.Mock(side_effect=AssertionError("models read"))
        self.runtime.health_get = mock.Mock(side_effect=AssertionError("health read"))

    def test_stdout_report_in_managed_session_no_other_gateway_reads(self):
        self.runtime.environ["CLAUDE_MULTI_MANAGED_ID"] = FIXED_ID
        out = io.StringIO()
        with mock.patch('claude_multi.cli.streams._open_tty_streams', side_effect=AssertionError("must stay stdout")):
            code = cli.main(["quota"], runtime=self.runtime, input_stream=io.StringIO(), output_stream=out)
        self.assertEqual(code, 0)
        self.assertIn("Quota — local gateway", out.getvalue())
        self.getter.assert_called_once()
        self.runtime.served_models_callback.assert_not_called()
        self.runtime.health_get.assert_not_called()
        self.assertFalse(cli._writes_versioned_state(cli.build_parser().parse_args(["quota"])))

    def test_report_goes_to_stdout_when_a_terminal_is_open(self):
        # Distinct terminal and stdout streams, so a quota report
        # routed to the terminal (not stdout) fails this test.
        class Terminal(io.StringIO):
            def close(self):  # main closes an owned terminal; keep the text
                pass
        tty_in, tty_out, stdout = Terminal(), Terminal(), io.StringIO()
        args = cli.build_parser().parse_args(["quota"])
        self.assertIs(cli._report_output_stream(args, tty_out, stdout), stdout)
        with mock.patch('claude_multi.cli.streams._open_tty_streams', return_value=(tty_in, tty_out)), \
             mock.patch.object(sys, "stdout", stdout):
            code = cli.main(["quota"], runtime=self.runtime)
        self.assertEqual(code, 0)
        self.assertIn("Quota — local gateway", stdout.getvalue())
        self.assertEqual(tty_out.getvalue(), "")
        self.getter.assert_called_once()

    def test_unavailable_exit_codes_and_owner_attention(self):
        for code, text in ((401, "mismatch"), (403, "five failed key attempts"), (404, "management off"), (500, "HTTP 500")):
            self.runtime._pool_cache = None
            self.getter.return_value = (code, b"secret error body")
            result, output = self.run_cli(["quota"], interactive=False)
            self.assertEqual(result, 1)
            self.assertIn(text, output)
            self.assertNotIn("secret error body", output)
        self.runtime._pool_cache = None
        self.runtime.listener_owner = lambda _: claude_multi.service.OwnerVerdict("unknown", "fixture listener")
        result, output = self.run_cli(["quota"], interactive=False)
        self.assertIn("Attention: port", output)

    def test_main_constructs_runtime_without_cleanup_or_shim_writes(self):
        # Exercise main without an injected Runtime, with an override that the
        # ordinary constructor would remove and shims it would overwrite.
        override = Path(self.runtime.environ["XDG_CONFIG_HOME"]) / "claude-multi" / "native-contract.json"
        state.ensure_private_dir(override.parent)
        state.atomic_write(override, b'{"effort_vocabulary": [], "version": 1}\n')
        original_runtime = cli.Runtime
        constructed = []
        def factory(**kwargs):
            self.assertFalse(kwargs["allow_state_writes"])
            self.assertFalse(kwargs["refresh_shims"])
            self.assertFalse(kwargs["initialize_session_store"])
            runtime = original_runtime(**kwargs, management_callback=self.getter,
                listener_owner=lambda _: claude_multi.service.OwnerVerdict("ours", "fixture"),
                health_get=self.runtime.health_get, served_models_callback=self.runtime.served_models_callback,
                cwd=self.root / "project")
            constructed.append(runtime)
            return runtime
        def snapshot():
            return {str(p.relative_to(self.root)): (p.stat().st_mode, p.stat().st_mtime_ns,
                    p.read_bytes() if p.is_file() else None) for p in self.root.rglob("*")}
        before = snapshot()
        with mock.patch.dict(os.environ, self.runtime.environ, clear=True), \
                mock.patch('claude_multi.assets.default_asset_root', return_value=FIXTURE_ROOT), \
                mock.patch('claude_multi.cli.runtime.Runtime', side_effect=factory), \
                mock.patch.object(catalog, "remove_legacy_contract_override", side_effect=AssertionError("cleanup")), \
                mock.patch.object(scope_mod, "ensure_hook_shim", side_effect=AssertionError("shim write")), \
                mock.patch.object(scope_mod, "ensure_hook_shim_v3", side_effect=AssertionError("shim3 write")), \
                mock.patch.object(scope_mod, "ensure_token_helper_command", side_effect=AssertionError("helper write")):
            code = cli.main(["quota"], input_stream=io.StringIO(), output_stream=io.StringIO(), interactive=False)
        self.assertEqual(code, 0)
        self.assertEqual(len(constructed), 1)
        self.assertEqual(snapshot(), before)
        self.runtime.served_models_callback.assert_not_called()
        self.runtime.health_get.assert_not_called()


    def test_quota_with_empty_home_creates_no_state_directories(self):
        # Run the actual entry point, not a Runtime that already created its
        # stores during fixture setup. No key means no observer or HTTP call;
        # off the management channel nothing is even looked up.
        for channel, text in (("nix", "no management key"), ("bundle", "Quota: unavailable in this build"),
                              (None, "Quota: unavailable in this build")):
            with self.subTest(channel=channel):
                home = self.root / f"empty-quota-home-{channel}"
                home.mkdir()
                env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                       "XDG_STATE_HOME": str(home / "state"), "TERM": "dumb"}
                if channel is not None:
                    env["CLAUDE_MULTI_CHANNEL"] = channel
                output = io.StringIO()
                with mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch('claude_multi.assets.default_asset_root', return_value=FIXTURE_ROOT), \
                        mock.patch.object(sessions, "SessionStore", side_effect=AssertionError("unused store creates directories")), \
                        mock.patch.object(claude_multi.service, "listener_owner", side_effect=AssertionError("host observer")):
                    code = cli.main(["quota"], input_stream=io.StringIO(), output_stream=output, interactive=False)
                self.assertEqual(code, 1)
                self.assertIn(text, output.getvalue())
                if channel != "nix":
                    self.assertNotIn("claude-multi-proxy", output.getvalue())
                self.assertEqual(list(home.rglob("*")), [])


# ==================================================================
# Admission and merged consumers: origin matrix, current authority,
# stale-Runtime refusal, the conservative T2 secret scrub and deterministic compilation.
import test_operator as operator_fixtures  # module import: no test classes re-exported
from claude_multi import operator as operator_mod
from claude_multi import profile as profile_mod
from claude_multi import settings as settings_mod
from claude_multi import views as views_mod
from claude_multi.cli import types as cli_types_mod


class OperatorConsumerTests(CLITestCase):
    MIGRATED = {"version": 1, "provider": {
        "display": "Legacy box", "kind": "anthropic-compatible", "base_url": "https://legacy.example/v1",
        "auth": {"kind": "header", "secret_ref": "env:LEGACYBOX_API_KEY", "header": "x-api-key"},
        "independence_family": "unknown"},
        "lines": {"custom-oldbox": {"wire_model": "oldbox-1", "display": "Old box", "efforts": ["high"],
                                   "default_effort": "high",
                                   "context": {"declared_tokens": 131072, "source": "operator"},
                                   "selector": "custom-oldbox", "legacy_key": "oldbox",
                                   "migrated_from": "custom.json"}}}

    def setUp(self) -> None:
        super().setUp()
        self.env = self.runtime.environ
        self.home = self.runtime.home
        self.files = operator_fixtures._fixture_files()
        self.files["legacybox"] = copy.deepcopy(self.MIGRATED)
        self.pdir = operator_mod.providers_dir({"HOME": str(self.home)})
        state.ensure_private_dir(self.pdir)
        for name, doc in self.files.items():
            state.atomic_write(self.pdir / f"{name}.json", strict_json.pretty_file_bytes(doc))
        state.atomic_write(self.pdir / operator_mod.MIGRATION_MARKER, b"{}\n")
        docs = self.runtime.catalog.docs
        self.layer = operator_mod.validate_layer(
            docs, {k: strict_json.pretty_file_bytes(v) for k, v in self.files.items()},
            schemas=operator_mod.load_schemas(CATALOG_ROOT), marker=True)
        self.assertEqual([p.text() for p in self.layer.problems], [])
        self.keys = ("custom-acme-small", "custom-acme-large", "custom-oldbox")
        self.grant(*self.keys)
        state.atomic_write(self.secret_file, b"KIMI_CLAUDE_API_KEY=cli-test-dummy\nACME_API_KEY=acme-dummy-value\n"
                                             b"LEGACYBOX_API_KEY=legacy-dummy-value\n")
        self.served |= {"custom-acme-small", "custom-acme-large-high", "custom-oldbox"}

    def grant(self, *keys, routes=("acme", "legacybox"), digests=None) -> None:
        document = operator_mod.ledger_document(None)
        document["routes"] = {pid: operator_fixtures._route(self.layer.providers[pid]) for pid in routes}
        for key in keys:
            line = self.layer.lines[key]
            document["admissions"][key] = {
                "digest": (digests or {}).get(key, line.definition_digest), "at": "2026-09-30T00:00:00Z",
                "via": "admit", "diagnostic": {"wire": line.core_entry["wire_model"],
                                               "fields": self.layer.metadata[key]["field_hashes"]}}
        path = operator_mod.ledger_path({"HOME": str(self.home)})
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        self.runtime.settings_store.update(
            lambda doc: doc.__setitem__("admitted_lines", sorted(set(doc.get("admitted_lines", [])) | set(keys))),
            catalog=self.runtime.lineup_catalog())

    def prepare(self, document: dict, *, profile: str | None = None):
        target = cli_types_mod.LaunchTarget("ad-hoc" if profile is None else "profile", document, profile, False,
                                            "fixture")
        return self.runtime.prepare(target, action="fresh", passthrough=[])

    def test_origin_metadata_reaches_every_consumer(self) -> None:
        lcat = self.runtime.lineup_catalog()
        self.assertEqual(lcat.origin("custom-acme-small"), "operator")
        self.assertEqual(lcat.origin("custom-oldbox"), "operator-migrated")
        self.assertEqual(lcat.origin("opus"), "catalog")
        self.assertEqual(profile_mod.OPERATOR_LINES_KEY, operator_mod.LINES_KEY)
        self.assertEqual(profile_mod.OPERATOR_SECRET_NAMES_KEY, operator_mod.SECRET_NAMES_KEY)
        eff = self.runtime.current_effective()
        rows = {row.key: row for row in views_mod.line_rows(lcat, eff, custom_ids=frozenset())}
        self.assertEqual(rows["custom-acme-small"].origin, "operator")
        self.assertEqual(rows["custom-acme-small"].source, "catalog")
        self.assertEqual(rows["custom-acme-small"].mode, "client")
        self.assertEqual(rows["custom-acme-large"].mode, "gateway")
        direct = {row.key: row for row in views_mod.direct_rows(tuple(rows.values()), marks={})}
        for key in self.keys:
            self.assertIn(key, direct)
            self.assertNotIn("(custom)", direct[key].text())
        self.assertIn("effort [high]", direct["custom-acme-large"].text())
        for slot in ("cm-lead", "binding"):
            picker = views_mod.picker_rows(tuple(rows.values()), slot=slot, bindings={}, lcat=lcat, eff=eff,
                                           current=None)
            keys = {item.key for item in picker.items if item.kind == "line"}
            self.assertTrue(set(self.keys) <= keys, (slot, set(self.keys) - keys))
        agent = views_mod.picker_rows(tuple(rows.values()), slot="cm-analyst", bindings={}, lcat=lcat, eff=eff,
                                      current=None)
        self.assertTrue(set(self.keys) <= {item.key for item in agent.items if item.selectable})

    def test_list_and_map_operator_lines_are_direct_profile_and_named_leads(self) -> None:
        for key in self.keys:
            with self.subTest(direct=key):
                prepared = self.prepare(profile_mod.ad_hoc_direct(key, "high"))
                self.assertIn(prepared.lineup.lead.binding.selector, prepared.result.fence.available_models)
                self.assertIn(prepared.lineup.lead.binding.selector, prepared.result.fence.lead_selectors)
            with self.subTest(profile=key):
                document = profile_mod.ad_hoc_direct(key, "high")
                document["name"] = "admitted"
                prepared = self.prepare(document, profile="admitted")
                self.assertEqual(prepared.lineup.lead.binding.key, key)
        state.atomic_write(sessions.config_root(self.env) / "bindings.json", strict_json.pretty_file_bytes(
            {"version": 1, "bindings": {"opbind": {"model": "custom-acme-large", "effort": "high"}}}))
        document = profile_mod.ad_hoc_direct("opbind", "high")
        document["name"] = "admitted"
        document["lead"] = {"use": "opbind"}
        prepared = self.prepare(document, profile="admitted")
        self.assertEqual(prepared.lineup.lead.binding.key, "custom-acme-large")
        self.assertEqual(prepared.lineup.lead.binding.selector, "custom-acme-large-high[1m]")
        context = claude_multi.compiler.lead_set_context(prepared.result.fence, 90)
        self.assertFalse(context.custom_bound)  # custom_bound is legacy-custom only

    def test_operator_lead_recommendation_does_not_block_agents(self) -> None:
        document = profile_mod.ad_hoc_direct("opus", "high")
        document["name"] = "admitted"
        document["agents"] = {"cm-analyst": {"model": "custom-acme-large", "effort": "high"}}
        prepared = self.prepare(document, profile="admitted")
        self.assertIn("custom-acme-large", str(prepared.notices))
        self.assertIn("capability-recommendation", {finding.code for finding in prepared.lineup.warnings})
        self.assertIn(prepared.lineup.agents["cm-analyst"].binding.selector,
                      prepared.result.fence.available_models)

    def test_current_badge_needs_digest_and_use_separately_needs_route(self) -> None:
        """A badge never authorizes a route; losing a badge never disables one."""

        self.assertTrue(set(self.keys) <= self.runtime.current_effective().admitted_lines)
        self.grant(*self.keys, digests={"custom-acme-small": "0" * 64})
        self.assertNotIn("custom-acme-small", self.runtime.current_effective().admitted_lines)
        self.assertIn("custom-acme-large", self.runtime.current_effective().admitted_lines)
        self.grant(*self.keys, routes=("legacybox",))
        effective = self.runtime.current_effective()
        self.assertTrue(set(self.keys) <= effective.admitted_lines)
        self.assertTrue({"custom-acme-small", "custom-acme-large"} <= effective.unavailable_lines.keys())
        self.assertNotIn("custom-oldbox", effective.unavailable_lines)
        with self.assertRaises(cli.LaunchPlanError):
            self.prepare(profile_mod.ad_hoc_direct("custom-acme-small", "high"))

    def test_stale_runtime_allows_badge_changes_but_refuses_an_edited_line(self) -> None:
        prepared = self.prepare(profile_mod.ad_hoc_direct("custom-acme-large", "high"))
        self.grant("custom-acme-small", "custom-oldbox")  # only the optional badge is gone
        self.runtime.perform(prepared)
        self.assertEqual(len(self.launches), 1)
        self.launches.clear()
        self.grant(*self.keys)
        prepared = self.prepare(profile_mod.ad_hoc_direct("custom-acme-large", "high"))
        files = copy.deepcopy(self.files["acme"])
        files["lines"]["custom-acme-large"]["context"]["declared_tokens"] = 131072
        state.atomic_write(self.pdir / "acme.json", strict_json.pretty_file_bytes(files))
        with self.assertRaisesRegex(claude_multi.launch.LaunchError, "changed or its route/provider became unavailable"):
            self.runtime.perform(prepared)
        self.assertEqual(self.launches, [])
        state.atomic_write(self.pdir / "acme.json", strict_json.pretty_file_bytes(self.files["acme"]))
        prepared = self.prepare(profile_mod.ad_hoc_direct("custom-acme-large", "high"))
        self.runtime.perform(prepared)
        self.assertEqual(len(self.launches), 1)

    def test_every_declared_t2_secret_name_is_scrubbed(self) -> None:
        ghost = {"version": 1, "provider": {
            "display": "Ghost", "kind": "anthropic-compatible", "base_url": "https://ghost.example/v1",
            "auth": {"kind": "bearer", "secret_ref": "env:GHOST_API_KEY"}, "independence_family": "ghost"},
            "lines": {"custom-ghost": {"wire_model": "ghost-1", "display": "Ghost", "efforts": ["ultracode"],
                                       "default_effort": "ultracode",
                                       "context": {"declared_tokens": 65536, "source": "operator"}}}}
        state.atomic_write(self.pdir / "ghost.json", strict_json.pretty_file_bytes(ghost))
        self.grant(*self.keys, routes=("legacybox",))  # acme unapproved, ghost invalid
        prepared = self.prepare(profile_mod.ad_hoc_direct("opus", "xhigh"))
        for name in ("GHOST_API_KEY", "ACME_API_KEY", "BETA_API_KEY", "LEGACYBOX_API_KEY"):
            self.assertIn(name, prepared.result.env_unset)

    def test_operator_compile_is_pure_and_t3_independent(self) -> None:
        """Identical inputs compile identical bytes; evidence and
        served observations are not inputs; no new record field."""

        def compile_bytes():
            prepared = self.prepare(profile_mod.ad_hoc_direct("custom-acme-large", "high"))
            plan = prepared.result.scope_plan
            mid = sessions.managed_id(prepared.record)
            text = json.dumps([plan.settings, {k: v.decode() if isinstance(v, bytes) else v
                                               for k, v in plan.other_files.items()},
                               list(prepared.result.env_unset)], sort_keys=True).replace(mid, "<mid>")
            return text, sorted(prepared.record["applied"]), prepared.record["applied"]["lead"]

        first = compile_bytes()
        evidence = operator_mod.evidence_path(self.env)
        state.ensure_private_dir(evidence.parent)
        state.atomic_write(evidence, strict_json.canonical_file_bytes({"version": 1, "lines": {}}))
        self.served = set()
        second = compile_bytes()
        self.assertEqual(first, second)
        self.assertNotIn("operator", json.dumps(first[1]))
        self.assertEqual(first[2]["key"], "custom-acme-large")

    def test_legacy_custom_can_be_bound_in_a_profile_without_renaming(self) -> None:
        os.unlink(self.pdir / operator_mod.MIGRATION_MARKER)
        os.unlink(self.pdir / "legacybox.json")
        claude_multi.custom.save_registry(self.env, {"version": 1, "providers": {
            "legacyp": {"base_url": "https://legacy-p.example/v1", "auth_kind": "bearer", "secret_env": "LEGACYP_KEY"}},
            "models": {"lm": {"provider": "legacyp", "wire_model": "lm-1", "context_tokens": 65536,
                              "created_via": "manual"}}})
        lcat = self.runtime.lineup_catalog()
        self.assertEqual(lcat.origin("lm"), "legacy-custom")
        self.prepare(profile_mod.ad_hoc_direct("lm", "high"))  # ad-hoc Direct keeps working
        document = profile_mod.ad_hoc_direct("lm", "high")
        document["name"] = "admitted"
        prepared = self.prepare(document, profile="admitted")
        self.assertEqual(prepared.lineup.lead.binding.key, "lm")
        self.assertEqual(prepared.lineup.lead.binding.selector, lcat.lines["lm"]["selector"])

    def test_models_new_off_operator_enter_is_guarded(self) -> None:
        self.runtime.settings_store.update(lambda doc: doc.__setitem__("admitted_lines", []),
                                           catalog=self.runtime.lineup_catalog())
        lcat = self.runtime.lineup_catalog()
        rows = views_mod.line_rows(lcat, self.runtime.current_effective(), custom_ids=frozenset())
        model = views_mod.models_model(rows, catalog_version=1, used={}, retired={}, continuity_count=0, radar={})
        self.assertIn("custom-acme-small", model.new_keys)
        self.assertTrue(set(self.keys) <= model.guarded)
        self.assertIn("claude-multi models admit custom-acme-small",
                      views_mod.operator_admission_refusal("custom-acme-small", False))
        before = self.runtime.settings_store.load()
        from claude_multi.cli.screens import models as models_screen
        screen = models_screen._ModelsScreen.__new__(models_screen._ModelsScreen)
        screen.runtime = self.runtime
        screen.line_rows = rows
        screen.banner = None
        screen.message = ""
        screen.message_role = "accent"
        screen.palette = claude_multi.tui.MONO_PALETTE
        screen.lifecycle = {"custom-acme-small": "off"}
        screen.index = 0
        screen.model = lambda _width: model
        screen._row_kind = lambda _key, _model: "new"
        with mock.patch.object(type(screen), "selected_key", new_callable=mock.PropertyMock,
                               return_value="custom-acme-small"):
            win = mock.Mock()
            win.getmaxyx.return_value = (24, 80)
            with mock.patch("claude_multi.cli.screens.common.OnboardingActions.confirm", return_value=False):
                screen._toggle_admission(win)
        # Cancelling the shared in-screen admission leaves both authorities unchanged.
        self.assertEqual(self.runtime.settings_store.load(), before)


# ---------------------------------------------------------------------------
# Providers / models commands, consent, doctor wiring.
import claude_multi.cli.consent as consent_mod
import claude_multi.cli.parser as parser_mod
import claude_multi.cli.streams as streams_mod
from claude_multi.cli import doctor as doctor_mod


ACME_ADD = ["providers", "add", "acme", "--kind", "anthropic-compatible", "--base-url",
            "https://api.acme.example/anthropic", "--auth", "bearer", "--secret-ref", "env:ACME_API_KEY",
            "--family", "acme", "--contracts", "output-config-high"]
SMALL_ADD = ["models", "add", "acme", "acme-small-1", "--as", "custom-acme-small", "--context", "131072",
             "--source", "operator", "--effort", "high"]


def fake_gateway_reply(body: bytes):
    """A well-behaved fake gateway answer for one battery request."""

    from claude_multi import qualify as qualify_mod

    document = json.loads(body)
    message = {"id": "msg_fake", "type": "message", "role": "assistant", "model": document["model"],
               "stop_reason": "end_turn", "usage": {"input_tokens": 9, "output_tokens": 1},
               "content": [{"type": "text", "text": "ok"}]}
    if document.get("stream"):
        frames = [{"type": "message_start", "message": {**message, "content": []}},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
                  {"type": "content_block_stop", "index": 0}, {"type": "message_stop"}]
        raw = "".join(f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n" for frame in frames)
        return qualify_mod.HttpResult(200, raw.encode())
    messages = document["messages"]
    last = messages[-1]["content"]
    if document.get("tools") and isinstance(last, str):
        nonce = last.split('nonce "', 1)[1].split('"', 1)[0]
        message["content"] = [{"type": "tool_use", "id": "toolu_fake", "name": qualify_mod.TOOL_NAME,
                               "input": {"nonce": nonce}}]
        message["stop_reason"] = "tool_use"
    elif isinstance(last, list):
        ack = next(block["content"] for block in last if block.get("type") == "tool_result")
        message["content"] = [{"type": "text", "text": ack}]
    elif "verification code" in last:
        code = last.split("the verification code is ", 1)[1].split(".", 1)[0]
        message["content"] = [{"type": "text", "text": code}]
        message["usage"] = {"input_tokens": len(last) // 4}
    return qualify_mod.HttpResult(200, json.dumps(message).encode())


class OperatorCommandCase(CLITestCase):
    """A fixture runtime with a fake smoke transport and an instant reload."""

    def setUp(self) -> None:
        super().setUp()
        self.env = {"HOME": str(self.runtime.home)}
        self.pdir = operator_mod.providers_dir(self.env)
        self.ledger_file = operator_mod.ledger_path(self.env)
        state.atomic_write(self.secret_file, b"KIMI_CLAUDE_API_KEY=cli-test-dummy\nACME_API_KEY=acme-dummy-value\n")
        self.calls: list[str] = []
        self.outcome = operator_mod.SmokeOutcome("pass", 200, "ok")
        self.during_smoke = None
        self.runtime.qualify_transport = self._transport
        # The battery transport seam (a well-behaved fake gateway).
        self.http_calls: list[tuple[str, bytes]] = []
        self.runtime.qualify_http = self._qualify_http
        patcher = mock.patch.object(
            self.runtime, "verify_reload",
            side_effect=lambda sentinel: claude_multi.proxy.ReloadResult("reloaded", f"gateway: reloaded (sentinel {sentinel})"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _transport(self, _base, _token, alias):
        self.calls.append(alias)
        if self.during_smoke is not None:
            self.during_smoke()
        return self.outcome

    def _qualify_http(self, _base, _token, call, body):
        from claude_multi import qualify as qualify_mod

        self.http_calls.append((call.label(), body))
        if self.during_smoke is not None:
            self.during_smoke()
        return fake_gateway_reply(body)

    def op(self, argv, text="", *, tty=True, env=None):
        err = io.StringIO()
        with mock.patch.object(consent_mod, "stdio_ttys", return_value=tty), \
                mock.patch.dict(self.runtime.environ, env or {}), contextlib.redirect_stderr(err):
            code, out = self.run_cli(argv, text)
        return code, out, err.getvalue()

    def ledger(self):
        return operator_mod.load_ledger(self.env, operator_mod.load_schemas(CATALOG_ROOT))

    def serve_current(self) -> None:
        """The fixture gateway serves exactly the current render (sentinel included)."""

        snap = gateway_facts._gateway_snapshot(self.runtime, self.runtime.gateway_token())
        self.assertIsNone(snap.render_error)
        self.assertIs(snap.config_drift, False)
        self.served |= set(snap.expected) | {snap.sentinel}

    def apply_and_serve(self) -> None:
        """Render what is declared now, then serve exactly that render."""

        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, err)
        self.serve_current()

    def declare_small(self) -> None:
        code, out, err = self.op(ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        code, out, err = self.op(SMALL_ADD)
        self.assertEqual(code, 0, err)

    def state_bytes(self) -> dict:
        found = {}
        for base in (self.root / "home", self.root / "config", self.root / "state", self.root / "secrets"):
            for path in sorted(base.rglob("*")) if base.exists() else ():
                if path.is_file() and not path.name.endswith(".lock"):
                    found[str(path)] = path.read_bytes()
        return found


class OperatorParserStreamTests(OperatorCommandCase):
    def test_parser_classification_and_stream_routing(self) -> None:
        parser = cli.build_parser()
        cases = {
            ("providers", "list"): False, ("providers", "show", "x"): False, ("providers", "validate"): False,
            ("providers", "template"): False, ("providers", "apply"): True, ("providers", "approve", "x"): True,
            ("providers", "set-key", "x"): True, ("providers", "rm", "x"): True, ("providers", "edit", "x"): True,
            ("providers", "migrate-custom"): False, ("providers", "migrate-custom", "--apply"): True,
            ("models",): False, ("models", "show", "k"): False, ("models", "admit", "k"): True,
            ("models", "revoke", "k"): True, ("models", "qualify", "k"): True, ("models", "rm", "k"): True,
            ("models", "add", "p", "w", "--context", "9000", "--source", "operator"): True,
            # Stdout export and the import preview write nothing.
            ("export",): False, ("export", "--out", "f.json"): True,
            ("import", "f.json"): False, ("import", "f.json", "--apply"): True,
        }
        tty, std = io.StringIO(), io.StringIO()
        for argv, writes in cases.items():
            with self.subTest(argv=argv):
                args = parser.parse_args(list(argv))
                self.assertIs(parser_mod._writes_versioned_state(args), writes)
                routed = streams_mod._report_output_stream(args, tty, std)
                self.assertIs(routed, tty if argv[1:2] == ("edit",) else std)
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (["providers"], ["providers", "add", "x", "--kind", "bogus", "--base-url", "u", "--auth",
                                         "none", "--family", "f"],
                         ["models", "add", "p", "w", "--context", "x", "--source", "operator"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised:
                    parser.parse_args(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_argument_combinations_are_usage_errors(self) -> None:
        before = self.state_bytes()
        for extra in (["--header", "x-api-key"], ["--listing-url", "https://api.acme.example/v1/models"]):
            with self.subTest(extra=extra):
                code, _out, err = self.op([*ACME_ADD, "--declare-only", *extra])
                self.assertEqual(code, 2, err)
        code, _out, err = self.op(["providers", "add", "lan", "--kind", "openai-compatible-lan", "--base-url",
                                   "http://box.lan:8000/v1", "--auth", "none", "--secret-ref", "env:LAN_KEY",
                                   "--family", "local"])
        self.assertEqual(code, 2)
        self.assertIn("--auth none takes no --secret-ref", err)
        self.assertEqual(self.state_bytes(), before)

    def test_template_is_deterministic_and_validates(self) -> None:
        for kind in ("anthropic-compatible", "openai-compatible-lan"):
            with self.subTest(kind=kind):
                code, out, err = self.op(["providers", "template", "--kind", kind])
                self.assertEqual(code, 0, err)
                self.assertEqual(out, strict_json.pretty_file_bytes(json.loads(out)).decode())
                candidate = self.root / "example.json"
                candidate.write_text(out)
                os.chmod(candidate, 0o600)
                code, report, err = self.op(["providers", "validate", str(candidate)])
                self.assertEqual(code, 0, report + err)
                self.assertIn("providers.d/example.json: valid (1 line(s))", report)


class OperatorClosedKeyedTests(OperatorCommandCase):
    """The closed keyed gate, on a fixture asset root whose
    audit is explicitly false, whatever the shipped catalog says."""

    def setUp(self) -> None:
        assets = Path(tempfile.mkdtemp(prefix="keyedtest-closed-cli-")).resolve()
        self.addCleanup(shutil.rmtree, assets, True)
        root = assets / "assets"
        shutil.copytree(CATALOG_ROOT, root)
        path = root / "catalog" / "gateway.json"
        document = json.loads(path.read_text())
        document[catalog.AUDITS_KEY] = {catalog.KEYED_COMPAT_AUDIT: False}
        path.write_bytes(strict_json.pretty_file_bytes(document))
        with mock.patch(__name__ + ".CATALOG_ROOT", root):
            super().setUp()

    def test_closed_keyed_add_is_refused(self) -> None:
        before = self.state_bytes()
        code, _out, err = self.op(["providers", "add", "gen", "--kind", "openai-compatible", "--base-url",
                                   "https://api.gen.example/v1", "--auth", "bearer", "--secret-ref",
                                   "env:GEN_API_KEY", "--family", "gen"])
        self.assertEqual(code, 1)
        self.assertIn("audit gate is closed", err)
        self.assertEqual(self.state_bytes(), before)

    def test_closed_keyed_template_is_refused(self) -> None:
        code, _out, err = self.op(["providers", "template", "--kind", "openai-compatible"])
        self.assertEqual(code, 1)


class OperatorGuardTests(OperatorCommandCase):
    """The human guard covers every authority verb before any secret
    access or side effect; flags never waive it; the answer defaults to no."""

    def guarded(self) -> list[list[str]]:
        return [ACME_ADD, ["providers", "approve", "acme"], ["providers", "set-key", "acme"],
                ["models", "admit", "custom-acme-small"], ["models", "qualify", "custom-acme-small", "--smoke"],
                ["providers", "migrate-custom", "--apply"]]

    def test_session_markers_and_redirected_stdio_refuse_with_zero_effects(self) -> None:
        self.op([*ACME_ADD, "--declare-only"])
        self.op(SMALL_ADD)
        before = self.state_bytes()
        cases = [({"CLAUDE_MULTI_MANAGED_ID": FIXED_ID}, True, "CLAUDE_MULTI_MANAGED_ID set"),
                 ({"CLAUDECODE": "1"}, True, "CLAUDECODE set"),
                 ({}, False, "stdin/stdout is not a terminal")]
        for env, tty, reason in cases:
            for argv in self.guarded():
                with self.subTest(argv=argv, reason=reason), \
                        mock.patch.object(claude_multi.secret_store.FileSecretStore, "_values",
                                          side_effect=AssertionError("secret read before the guard")):
                    code, out, err = self.op(argv, "y\n", tty=tty, env=env)
                    self.assertEqual(code, 1, out + err)
                    self.assertIn("needs a terminal outside Claude Code sessions", err)
                    self.assertIn(reason, err)
                    self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_declaration_is_allowed_in_session_and_inert(self) -> None:
        code, out, err = self.op([*ACME_ADD, "--declare-only"], env={"CLAUDE_MULTI_MANAGED_ID": FIXED_ID})
        self.assertEqual(code, 0, err)
        self.assertIn("declared provider acme — route unapproved; not rendered\nnext: claude-multi providers approve acme",
                      out)
        self.assertFalse(os.path.lexists(self.ledger_file) and self.ledger().routes)
        code, out, err = self.op(SMALL_ADD, env={"CLAUDECODE": "1"})
        self.assertEqual(code, 0, err)
        self.assertIn("declared custom-acme-small — New · not admitted · selectors custom-acme-small · class custom-131072\n"
                      "lead recommended; explicit agent bindings are allowed · validated floor 131072 (not near-limit measured)", out)
        self.assertIn("optional: claude-multi models admit custom-acme-small", out)
        config = (claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml").read_text()
        self.assertNotIn("custom-acme-small", config)

    def test_default_no_writes_nothing(self) -> None:
        before = self.state_bytes()
        code, _out, err = self.op(ACME_ADD, "\n")
        self.assertEqual(code, 1)
        self.assertIn("route not approved — nothing written", err)
        self.assertEqual(self.state_bytes(), before)

    def test_legacy_custom_add_uses_the_same_guard(self) -> None:
        before = self.state_bytes()
        for argv in (["custom", "add-provider", "zed", "--base-url", "https://api.zed.example", "--auth", "bearer",
                      "--secret-env", "ZED_API_KEY"],
                     ["custom", "add-model", "zed-one", "--provider", "kimi", "--wire", "zed-1", "--context", "65536"]):
            for env, tty in (({"CLAUDE_MULTI_MANAGED_ID": FIXED_ID}, True), ({}, False)):
                with self.subTest(argv=argv, env=env):
                    code, _out, err = self.op(argv, env=env, tty=tty)
                    self.assertEqual(code, 1)
                    self.assertIn("needs a terminal outside Claude Code sessions", err)
        self.assertEqual(self.state_bytes(), before)
        code, out, _err = self.op(["custom", "add-model", "zed-one", "--provider", "kimi", "--wire", "zed-1",
                                   "--context", "65536"])
        self.assertEqual(code, 0, out)
        self.assertIn("zed-one", claude_multi.custom.load_registry(self.runtime.environ)["models"])

    def test_discover_refuses_an_unapproved_t2_route_with_zero_calls(self) -> None:
        # A keyed T2 listing needs the current approved route.
        self.op([*ACME_ADD, "--declare-only"])
        self.runtime.listing_transport = mock.Mock(side_effect=AssertionError("provider call"))
        code, out, err = self.op(["discover", "acme"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("discover acme: route unapproved or changed — claude-multi providers approve acme", err)
        code, out, err = self.op(["discover", "acme"], "y\n", env={"CLAUDE_MULTI_MANAGED_ID": FIXED_ID})
        self.assertEqual(code, 1)
        self.assertIn("discover acme: needs a terminal outside Claude Code sessions", err)
        self.runtime.listing_transport.assert_not_called()


class OperatorLifecycleTests(OperatorCommandCase):
    def test_approve_declare_admit_revoke_without_inference_or_render(self) -> None:
        self.declare_small()
        layer = self.runtime.operator_snapshot().layer
        self.assertEqual(self.ledger().routes["acme"]["rd"], layer.providers["acme"].route_digest)
        config = claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml"
        before = config.read_bytes()
        routes = dict(self.ledger().routes)
        digest = layer.lines["custom-acme-small"].definition_digest
        with mock.patch.object(self.runtime, "render_gateway", side_effect=AssertionError("badge must not render")), \
                mock.patch.object(self.runtime, "smoke", side_effect=AssertionError("badge must not infer")), \
                mock.patch.object(self.runtime, "qualify_post", side_effect=AssertionError("badge must not qualify")):
            code, out, err = self.op(["models", "admit", "custom-acme-small"], "y\n")
            self.assertEqual(code, 0, out + err)
            self.assertIn("no requests are sent", err)
            self.assertIn("Optional badge recorded", out)
            grant = self.ledger().admissions["custom-acme-small"]
            self.assertEqual((grant["digest"], grant["via"]), (digest, "admit"))
            self.assertIn("custom-acme-small", self.runtime.current_effective().admitted_lines)
            self.assertIsNone(operator_mod.load_evidence(self.runtime.gateway_environ(),
                                                       operator_mod.load_schemas(CATALOG_ROOT)))
            code, out, err = self.op(["models", "revoke", "custom-acme-small"])
            self.assertEqual(code, 3, err)
            self.assertIn("line remains usable", err)
            self.assertIn("qualification evidence is unchanged", err)
            self.assertIn("custom-acme-small", self.ledger().admissions)
            code, out, err = self.op(["models", "revoke", "custom-acme-small"], "y\n")
            self.assertEqual(code, 0, err)
            self.assertIn("use availability and qualification unchanged", out)
        self.assertNotIn("custom-acme-small", self.ledger().admissions)
        self.assertNotIn("custom-acme-small", self.runtime.current_effective().admitted_lines)
        self.assertEqual(self.ledger().routes, routes)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.http_calls, [])
        self.assertTrue(settings_mod.line_offered("custom-acme-small", layer.lines["custom-acme-small"].core_entry,
                                                 self.runtime.current_effective()))

    def test_admission_needs_no_served_alias_health_or_credential(self) -> None:
        self.declare_small()
        # Declining the optional attestation still changes nothing.
        code, _out, err = self.op(["models", "admit", "custom-acme-small"], "n\n")
        self.assertEqual(code, 3)
        self.assertIn("not admitted — nothing changed", err)
        self.assertNotIn("custom-acme-small", self.ledger().admissions)
        state.atomic_write(self.secret_file, b"KIMI_CLAUDE_API_KEY=cli-test-dummy\n")
        self.served.clear()
        with mock.patch.object(self.runtime, "check_readiness", side_effect=AssertionError("health observation")), \
                mock.patch.object(self.runtime, "served_models", side_effect=AssertionError("served observation")):
            code, out, err = self.op(["models", "admit", "custom-acme-small"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("custom-acme-small", self.ledger().admissions)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.http_calls, [])

    def test_failed_optional_smoke_stays_failed_after_admission(self) -> None:
        from claude_multi import qualify

        self.declare_small()
        self.serve_current()
        self.runtime.qualify_http = lambda *_args: qualify.HttpResult(400, b'{"error":"fixture"}')
        code, out, err = self.op(["models", "qualify", "custom-acme-small", "--smoke"], "y\n")
        self.assertEqual(code, 1, out + err)
        evidence = operator_mod.evidence_path(self.runtime.gateway_environ())
        before = evidence.read_bytes()
        recorded = json.loads(before)["lines"]["custom-acme-small"]["checks"]["smoke"]["result"]
        self.assertEqual(recorded, "failed")
        self.assertNotIn("custom-acme-small", self.ledger().admissions)
        code, out, err = self.op(["models", "admit", "custom-acme-small"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("custom-acme-small", self.ledger().admissions)
        self.assertEqual(evidence.read_bytes(), before)

    def test_definition_change_during_the_smoke_records_nothing(self) -> None:
        self.declare_small()
        self.serve_current()
        path = self.pdir / "acme.json"

        def edit() -> None:
            document = json.loads(path.read_text())
            document["lines"]["custom-acme-small"]["wire_model"] = "acme-small-2"
            state.atomic_write(path, strict_json.pretty_file_bytes(document))

        self.during_smoke = edit
        code, _out, err = self.op(["models", "qualify", "custom-acme-small", "--smoke"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("configuration changed", err)
        self.assertEqual(len(self.http_calls), 1)
        self.assertIsNone(operator_mod.load_evidence(self.runtime.gateway_environ(), operator_mod.load_schemas(CATALOG_ROOT)))
        self.assertNotIn("custom-acme-small", self.ledger().admissions)

    def test_qualify_smoke_and_show(self) -> None:
        self.declare_small()
        self.serve_current()
        code, out, err = self.op(["models", "qualify", "custom-acme-small", "--smoke"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertEqual([label for label, _body in self.http_calls], ["smoke"])
        self.assertIn("smoke custom-acme-small: pass (HTTP 200)", out)
        self.assertIn("evidence recorded for", out)
        code, out, err = self.op(["models", "show", "custom-acme-small", "--evidence"])
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(shown["evidence"]["checks"]["smoke"]["result"], "pass")
        code, out, err = self.op(["models", "show", "custom-acme-small", "--resolved"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["entry"]["status"], "new")
        self.assertEqual(out, strict_json.pretty_file_bytes(json.loads(out)).decode())
        code, out, err = self.op(["models", "show", "custom-acme-small"])
        self.assertIn("status New · not admitted · lead recommended; explicit agent bindings are allowed · class custom-131072", out)
        self.assertIn("smoke evidence: pass (ok)", out)

    def test_models_rm_refuses_live_and_bound_then_keeps_captures(self) -> None:
        self.declare_small()
        record_id = "33333333-3333-4333-8333-333333333333"
        sessions_dir = state.ensure_private_dir(self.runtime.session_store.root / "sessions")
        record = {"version": 4, "applied": {"lead": {"key": "custom-acme-small", "selector": "custom-acme-small"},
                                            "agents": {}}, "last_event_source": "start"}
        state.atomic_write(sessions_dir / f"{record_id}.json", strict_json.canonical_file_bytes(record))
        code, _out, err = self.op(["models", "rm", "custom-acme-small"])
        self.assertEqual(code, 1)
        self.assertIn("live sessions use it (33333333)", err)
        # A record with no recorded end may be a launch that exited
        # before SessionStart; the refusal names the exact way out.
        self.assertIn("including a launch that exited before SessionStart", err)
        self.assertIn(f"record its end: claude-multi sessions mark-ended {record_id}", err)
        record["last_event_source"] = "end"
        state.atomic_write(sessions_dir / f"{record_id}.json", strict_json.canonical_file_bytes(record))
        code, out, err = self.op(["models", "rm", "custom-acme-small"], "n\n")
        self.assertEqual(code, 3, err)  # the y/N defaults to no: nothing removed
        self.assertIn("custom-acme-small", self.runtime.operator_snapshot().layer.lines)
        code, out, err = self.op(["models", "rm", "custom-acme-small"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("Removed custom-acme-small — the gateway reloaded\n"
                      "Captured aliases remain served until pruned.", out)
        self.assertEqual(self.ledger().removed["custom-acme-small"]["last_wire"], "acme-small-1")
        config = (claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml").read_text()
        self.assertIn('alias: "custom-acme-small"', config)  # The capture keeps serving
        self.assertIn("custom-acme-small", self.ledger().aliases)

    def test_providers_rm_refusal_names_mark_ended_for_live_looking_records(self) -> None:
        """As for models rm: a live-looking record is named with
        its full id and the sessions mark-ended remedy."""

        self.declare_small()
        record_id = "55555555-5555-4555-8555-555555555555"
        sessions_dir = state.ensure_private_dir(self.runtime.session_store.root / "sessions")
        record = {"version": 4, "applied": {"lead": {"key": "custom-acme-small", "selector": "custom-acme-small"},
                                            "agents": {}}, "last_event_source": "launch"}
        state.atomic_write(sessions_dir / f"{record_id}.json", strict_json.canonical_file_bytes(record))
        code, _out, err = self.op(["providers", "rm", "acme"])
        self.assertEqual(code, 1)
        self.assertIn("refused — claude-multi sessions stop 55555555; a record without a recorded end", err)
        self.assertIn(f"claude-multi sessions mark-ended {record_id}", err)
        self.assertTrue((self.pdir / "acme.json").exists())

    def test_providers_rm_refuses_admitted_lines(self) -> None:
        self.declare_small()
        self.serve_current()
        self.assertEqual(self.op(["models", "admit", "custom-acme-small"], "y\n")[0], 0)
        code, _out, err = self.op(["providers", "rm", "acme"])
        self.assertEqual(code, 1)
        self.assertIn("refused — claude-multi models revoke custom-acme-small", err)
        self.assertTrue((self.pdir / "acme.json").exists())

    def test_apply_exit_codes_and_explicit_refusal(self) -> None:
        self.declare_small()
        for status, code in (("reloaded", 0), ("restart_required", 3), ("token_mismatch", 4), ("down", 5)):
            with self.subTest(status=status), mock.patch.object(
                    self.runtime, "verify_reload",
                    return_value=claude_multi.proxy.ReloadResult(status, f"gateway: {status}")):
                self.assertEqual(self.op(["providers", "apply"])[0], code)
        config = claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml"
        before = config.read_bytes()
        state.atomic_write(self.pdir / "broken.json", b"{not json")
        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 1)
        self.assertIn("providers.d/broken.json", err)
        self.assertEqual(config.read_bytes(), before)
        code, out, _err = self.op(["providers", "validate"])
        self.assertEqual(code, 1)
        self.assertIn("providers.d/broken.json:1:2: not strict JSON", out)

    def test_list_and_show(self) -> None:
        self.declare_small()
        code, out, err = self.op(["providers", "list"])
        self.assertEqual(code, 0, err)
        self.assertIn("acme\toperator provider · anthropic-compatible · https://api.acme.example · route approved", out)
        self.assertIn("  custom-acme-small\tNew · not admitted", out)
        code, out, err = self.op(["providers", "show", "acme", "--resolved"])
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual((shown["provider_origin"], shown["route"]), ("operator", "approved"))
        self.assertNotIn("acme-dummy-value", out)

    def test_editor_edit_reports_the_admission_lapse(self) -> None:
        self.declare_small()
        self.serve_current()
        self.assertEqual(self.op(["models", "admit", "custom-acme-small"], "y\n")[0], 0)
        editor = self.root / "edit.sh"
        editor.write_text("#!/bin/sh\nsed -i 's/acme-small-1/acme-small-9/' \"$1\"\n")
        os.chmod(editor, 0o700)
        with mock.patch.dict(self.runtime.environ, {"EDITOR": str(editor)}):
            code, out, err = self.op(["models", "edit", "custom-acme-small"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("custom-acme-small: definition changed (", out)
        self.assertIn("its optional admission badge lapses (not a use restriction); re-admit: claude-multi models admit custom-acme-small", out)
        self.assertNotIn("custom-acme-small", self.runtime.current_effective().admitted_lines)


class OperatorDoctorWiringTests(OperatorCommandCase):
    def test_doctor_reports_operator_lines_only_with_operator_state(self) -> None:
        self.assertEqual(doctor_mod._doctor_operator_report(self.runtime, None), ([], [], []))
        self.op([*ACME_ADD, "--declare-only"])
        self.op(SMALL_ADD)
        blocks, attention, info = doctor_mod._doctor_operator_report(self.runtime, None)
        self.assertEqual(blocks, [])
        self.assertIn("provider acme: route unapproved; not rendered — claude-multi providers approve acme", attention)
        self.assertIn("operator: 1 providers · 1 lines (0 admitted, 1 not admitted, 0 qualified, 0 usable for agents)", info)
        sessions_dir = state.ensure_private_dir(self.runtime.session_store.root / "sessions")
        record = {"version": 4, "applied": {"lead": {"key": "custom-acme-small", "selector": "custom-acme-small"},
                                            "agents": {}}, "last_event_source": "start"}
        state.atomic_write(sessions_dir / "44444444-4444-4444-8444-444444444444.json",
                           strict_json.canonical_file_bytes(record))
        blocks, _attention, _info = doctor_mod._doctor_operator_report(self.runtime, None)
        self.assertIn("provider acme: route unapproved; not rendered — claude-multi providers approve acme "
                      "(used by 44444444)", blocks)
        problems, _info, _attention = doctor_mod._collect_doctor_reports(self.runtime)
        self.assertTrue(any(line.startswith("provider acme: route unapproved") for line in problems))

    def test_provider_facts_report_route_and_origin(self) -> None:
        self.op([*ACME_ADD, "--declare-only"])
        self.op(SMALL_ADD)
        path = self.pdir / "kimi.json"
        state.atomic_write(path, strict_json.pretty_file_bytes(operator_fixtures._fixture_files()["kimi"]))
        facts = {fact["id"]: fact for fact in gateway_facts._provider_facts(self.runtime).facts}
        self.assertEqual((facts["acme"]["source"], facts["acme"]["route"], facts["acme"]["operator_lines"]),
                         ("operator", "unapproved", 1))
        self.assertEqual((facts["kimi"]["source"], facts["kimi"]["operator_lines"]), ("catalog", 1))
        self.assertNotIn("route", facts["kimi"])


class OperatorMigrateCommandTests(OperatorCommandCase):
    """migrate-custom through the CLI: dry run writes nothing, parity refuses, --apply
    grants via migrate and leaves 3.0.2 readers able to read everything."""

    def setUp(self) -> None:
        super().setUp()
        state.atomic_write(self.secret_file, b"KIMI_CLAUDE_API_KEY=cli-test-dummy\nZETA_API_KEY=zeta-dummy-value\n"
                                             b"IDLE_API_KEY=idle-dummy-value\n")
        self.source = (Path(operator_fixtures.OPERATOR_FIXTURES) / "custom.json").read_bytes()
        self.shell = claude_multi.custom.registry_path(self.runtime.environ)
        self.service = claude_multi.proxy.config_dir(self.runtime.home) / "custom.json"
        for path in (self.shell, self.service):
            state.ensure_private_dir(path.parent)
            state.atomic_write(path, self.source)

    def test_dry_run_creates_nothing(self) -> None:
        before = self.state_bytes()
        listing = sorted(str(p) for p in self.root.rglob("*"))
        code, out, err = self.op(["providers", "migrate-custom"])
        self.assertEqual(code, 0, err)
        self.assertIn("write providers.d/zeta.json: custom-zeta-pro", out)
        self.assertIn("approve route zeta: header env:ZETA_API_KEY → https://api.zeta.example", out)
        self.assertIn("dry run: nothing written", out)
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), listing)

    def test_registry_parity_mismatch_refuses(self) -> None:
        os.unlink(self.service)
        before = self.state_bytes()
        code, _out, err = self.op(["providers", "migrate-custom", "--apply"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("custom.json mismatch", err)
        self.assertEqual(self.state_bytes(), before)

    def test_apply_grants_via_migrate_and_old_readers_still_read(self) -> None:
        records_before = {p: p.read_bytes() for p in (self.runtime.session_store.root / "sessions").glob("*.json")}
        code, out, err = self.op(["providers", "migrate-custom", "--apply"], "y\n")
        self.assertEqual(code, 0, out + err)
        self.assertIn("selectors unchanged: custom-kimi-side, custom-zeta-pro", out)
        self.assertEqual(self.shell.read_bytes(), self.source)
        settings_doc = self.runtime.settings_store.load()
        self.assertTrue({"custom-kimi-side", "custom-zeta-pro"} <= set(settings_doc["admitted_lines"]))
        # The 3.0.2 readers (closed schemas, unchanged) accept what 3.1 wrote.
        schema = strict_json.load(CATALOG_ROOT / "schemas" / "settings.schema.json")
        from claude_multi import validate as schema_validate
        self.assertEqual(schema_validate.validate(settings_doc, schema, "$"), [])
        self.assertEqual(claude_multi.custom.load_registry(self.runtime.environ)["models"].keys(),
                         {"zeta-pro", "kimi-side"})
        continuity_doc = claude_multi.continuity.read(self.runtime.home)
        if continuity_doc is not None:
            claude_multi.continuity.validate_document(continuity_doc)
        self.assertEqual({p: p.read_bytes() for p in (self.runtime.session_store.root / "sessions").glob("*.json")},
                         records_before)
        eff = self.runtime.current_effective()
        self.assertTrue({"custom-kimi-side", "custom-zeta-pro"} <= set(eff.admitted_lines))
        code, out, _err = self.op(["providers", "migrate-custom"])
        self.assertIn("custom.json already migrated", out)
        code, _out, err = self.op(["custom", "add-model", "z2", "--provider", "zeta", "--wire", "z2",
                                   "--context", "65536"])
        self.assertEqual(code, 1)
        self.assertIn("custom.json was migrated to providers.d and is no longer read — use claude-multi models add", err)


class OperatorRouteAndKeyTests(OperatorCommandCase):
    def test_approve_after_declare_only_and_set_key(self) -> None:
        self.op([*ACME_ADD, "--declare-only"])
        code, out, err = self.op(["providers", "approve", "acme"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("approved route acme: bearer env:ACME_API_KEY → https://api.acme.example", out)
        self.assertEqual(self.runtime.operator_snapshot().layer.route_status["acme"], "approved")
        code, out, _err = self.op(["providers", "approve", "acme"])
        self.assertIn("route acme is already approved", out)
        # set-key: the replacement needs its own confirmation; the value never prints.
        code, out, err = self.op(["providers", "set-key", "acme"], "n\n")
        self.assertEqual(code, 3)
        self.assertIn("A acme API key is set (16 chars). Replace it? [y/N] ", err)
        self.assertIn("acme API key kept — nothing changed", out)
        code, out, err = self.op(["providers", "set-key", "acme"], "y\nacme-new-value\n")
        self.assertEqual(code, 0, err)
        self.assertIn("acme API key saved (14 chars) — the gateway reloaded", out)
        self.assertNotIn("acme-new-value", out + err)
        self.assertEqual(claude_multi.secret_store.default_store(self.runtime.environ).get("ACME_API_KEY"),
                         "acme-new-value")

    def test_route_edit_lapses_approval_until_reapproved(self) -> None:
        self.declare_small()
        path = self.pdir / "acme.json"
        document = json.loads(path.read_text())
        document["provider"]["base_url"] = "https://api.acme2.example/anthropic"
        state.atomic_write(path, strict_json.pretty_file_bytes(document))
        layer = self.runtime.operator_snapshot().layer
        self.assertEqual(layer.route_status["acme"], "changed")
        code, out, err = self.op(["providers", "approve", "acme"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("→ https://api.acme2.example", out)


class OperatorManagedFileTests(OperatorCommandCase):
    def test_hm_symlink_is_read_but_never_replaced(self) -> None:
        managed = self.root / "managed-kimi.json"
        managed.write_bytes(strict_json.pretty_file_bytes(operator_fixtures._fixture_files()["kimi"]))
        os.chmod(managed, 0o444)
        state.ensure_private_dir(self.pdir)
        os.symlink(managed, self.pdir / "kimi.json")
        self.assertIn("custom-kimi-next", self.runtime.operator_snapshot().layer.lines)
        before = managed.read_bytes()
        code, _out, err = self.op(["models", "add", "kimi", "kimi-fixture-extra", "--as", "custom-kimi-extra",
                                   "--context", "65536", "--source", "operator", "--effort", "max=output-config-max"])
        self.assertEqual(code, 1)
        self.assertIn("providers.d/kimi.json is read-only here (managed elsewhere) — add this to its source:", err)
        self.assertIn('"custom-kimi-extra"', err)
        self.assertTrue(os.path.islink(self.pdir / "kimi.json"))
        self.assertEqual(managed.read_bytes(), before)

    def test_providers_edit_validates_before_writing(self) -> None:
        self.op([*ACME_ADD, "--declare-only"])
        editor = self.root / "break.sh"
        editor.write_text("#!/bin/sh\nsed -i 's/https:\\/\\/api.acme.example/http:\\/\\/api.acme.example/' \"$1\"\n")
        os.chmod(editor, 0o700)
        before = (self.pdir / "acme.json").read_bytes()
        with mock.patch.dict(self.runtime.environ, {"EDITOR": str(editor)}):
            code, _out, err = self.op(["providers", "edit", "acme"], "y\n")
        self.assertEqual(code, 1)
        self.assertIn("was not changed", err)
        self.assertIn("your edit is kept at", err)
        kept = Path(err.rsplit("your edit is kept at ", 1)[1].split()[0])
        self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o600)
        kept.unlink()
        self.assertEqual((self.pdir / "acme.json").read_bytes(), before)

    def test_edit_refusal_of_a_managed_file_never_echoes_a_credential_literal(self) -> None:
        """The read-only refusal of an existing, unvalidated
        managed file is value-free (providers edit and models edit)."""

        literal = "sk-" + "operatorreviewdummy" * 2
        kimi = operator_fixtures._fixture_files()["kimi"]
        kimi["lines"]["custom-kimi-next"]["display"] = literal
        managed = self.root / "managed-kimi.json"
        managed.write_bytes(strict_json.pretty_file_bytes(kimi))
        os.chmod(managed, 0o444)
        state.ensure_private_dir(self.pdir)
        os.symlink(managed, self.pdir / "kimi.json")
        stored = "stored-credential-value-9"
        state.atomic_write(self.secret_file, f"KIMI_CLAUDE_API_KEY={stored}\n".encode())
        problems = self.runtime.operator_snapshot().layer.problems_by_file.get("providers.d/kimi.json", ())
        self.assertTrue(any(p.code == "secret-literal" for p in problems))
        for argv in (["providers", "edit", "kimi"], ["models", "edit", "custom-kimi-next"]):
            with self.subTest(argv=argv), mock.patch.dict(self.runtime.environ, {"EDITOR": "/bin/false"}):
                code, out, err = self.op(argv)
                if argv[0] == "models":
                    # The line is refused (secret literal): no declaration to edit.
                    self.assertEqual(code, 1)
                else:
                    self.assertEqual(code, 1)
                    self.assertIn("providers.d/kimi.json is read-only here (managed elsewhere) — change it at its source",
                                  err)
                self.assertNotIn(literal, out + err)
                self.assertNotIn(stored, out + err)
        # A readable managed file without a literal: models edit refuses value-free too.
        kimi["lines"]["custom-kimi-next"]["display"] = "Kimi next"
        os.chmod(managed, 0o644)
        managed.write_bytes(strict_json.pretty_file_bytes(kimi))
        os.chmod(managed, 0o444)
        with mock.patch.dict(self.runtime.environ, {"EDITOR": "/bin/false"}):
            code, _out, err = self.op(["models", "edit", "custom-kimi-next"])
        self.assertEqual(code, 1)
        self.assertIn("providers.d/kimi.json is read-only here (managed elsewhere) — change it at its source", err)
        self.assertNotIn('"custom-kimi-next"', err)


class OperatorConfigCommitBoundaryTests(OperatorCommandCase):
    """A config.yaml replaced before its directory fsync
    failed is published; the verb keeps its inputs and says so."""

    def test_directory_fsync_failure_after_publication_keeps_the_change(self) -> None:
        code, _out, err = self.op(ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        original = state.atomic_write

        def write(path, data):
            original(path, data)
            if Path(path).name == "config.yaml":
                raise state.CommittedStateError(5, f"state file {path} was replaced but directory fsync failed")

        with mock.patch.object(state, "atomic_write", side_effect=write):
            code, out, err = self.op(SMALL_ADD)
        self.assertEqual(code, 0, err)
        self.assertIn("models add: the new gateway config was published but its durability is unconfirmed", err)
        self.assertIn("the change is kept — rerun claude-multi providers apply to confirm it", err)
        self.assertNotIn("nothing changed", err)
        self.assertIn("custom-acme-small", json.loads((self.pdir / "acme.json").read_text())["lines"])
        config = (claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml").read_text()
        self.assertIn('alias: "custom-acme-small"', config)
        self.assertIn("declared custom-acme-small", out)

    def test_a_refusal_before_publication_still_undoes(self) -> None:
        code, _out, err = self.op(ACME_ADD, "y\n")
        self.assertEqual(code, 0, err)
        config = claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml"
        before = config.read_bytes()
        original = state.atomic_write

        def write(path, data):
            if Path(path).name == "config.yaml":
                raise state.StateError(5, "injected failure before the replace")
            original(path, data)

        with mock.patch.object(state, "atomic_write", side_effect=write):
            code, _out, err = self.op(SMALL_ADD)
        self.assertEqual(code, 1)
        self.assertIn("the gateway render refused — nothing changed", err)
        self.assertNotIn("custom-acme-small", json.loads((self.pdir / "acme.json").read_text()).get("lines", {}))
        self.assertEqual(config.read_bytes(), before)


class OperatorUndoBeforeWriteTests(OperatorCommandCase):
    """The provider and model writers register each write's exact undo
    before the write: a failure after the bytes were replaced (a directory
    fsync that failed, an interrupt) puts back every write that landed."""

    @staticmethod
    def fail_after(target, name: str, *, times: int = 1):
        """Patch ``target.name`` so its first ``times`` calls write, then fail."""

        real = getattr(target, name)
        calls = [0]

        def write(*args, **kwargs):
            result = real(*args, **kwargs)
            calls[0] += 1
            if calls[0] <= times:
                raise state.CommittedStateError(5, "injected: the write landed, its directory fsync failed")
            return result

        # A method of a class is patched with its signature, so ``self`` reaches the real one.
        return mock.patch.object(target, name, side_effect=write, autospec=isinstance(target, type))

    def routes(self) -> dict:
        return dict(self.ledger().routes) if self.ledger_file.exists() else {}

    def test_providers_add_puts_back_the_file_when_the_approval_write_fails(self) -> None:
        with self.fail_after(operator_mod, "update_ledger"):
            code, _out, err = self.op(ACME_ADD, "y\n")
        self.assertNotEqual(code, 0, err)
        self.assertFalse((self.pdir / "acme.json").exists())
        self.assertNotIn("acme", self.routes())

    def test_providers_add_puts_back_a_file_whose_own_write_failed_after_landing(self) -> None:
        with self.fail_after(operator_mod, "write_provider_bytes"):
            code, _out, err = self.op(ACME_ADD, "y\n")
        self.assertNotEqual(code, 0, err)
        self.assertFalse((self.pdir / "acme.json").exists())
        self.assertNotIn("acme", self.routes())

    def test_providers_approve_withdraws_an_approval_that_landed(self) -> None:
        code, _out, err = self.op([*ACME_ADD, "--declare-only"])
        self.assertEqual(code, 0, err)
        with self.fail_after(operator_mod, "update_ledger"):
            code, _out, err = self.op(["providers", "approve", "acme"], "y\n")
        self.assertNotEqual(code, 0, err)
        self.assertNotIn("acme", self.routes())

    def test_providers_and_models_edit_put_back_the_previous_bytes(self) -> None:
        code, _out, err = self.op([*ACME_ADD, "--declare-only"])
        self.assertEqual(code, 0, err)
        path = self.pdir / "acme.json"
        before = path.read_bytes()
        editor = self.root / "rename.sh"
        editor.write_text("#!/bin/sh\nsed -i 's/\"display\": \"acme\"/\"display\": \"Acme Two\"/' \"$1\"\n")
        os.chmod(editor, 0o700)
        with mock.patch.dict(self.runtime.environ, {"EDITOR": str(editor)}), \
                self.fail_after(operator_mod, "write_provider_bytes"):
            code, _out, err = self.op(["providers", "edit", "acme"], "y\n")
        self.assertNotEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), before)
        with self.fail_after(operator_mod, "write_provider_bytes"):
            code, _out, err = self.op(SMALL_ADD)
        self.assertNotEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), before)

    def test_a_preset_key_import_puts_back_the_previous_key(self) -> None:
        document = {"version": 1, "lines": {}, "provider": {
            "display": "acme", "kind": "anthropic-compatible", "base_url": "https://api.acme.example/anthropic",
            "auth": {"kind": "bearer", "secret_ref": "env:ACME_API_KEY"}, "independence_family": "acme",
            "payload_contracts": ["output-config-high"]}}
        key = self.root / "acme.key"
        state.atomic_write(key, b"acme-imported-value\n")
        store = claude_multi.secret_store.default_store(self.runtime.environ)
        self.assertEqual(store.get("ACME_API_KEY"), "acme-dummy-value")
        with mock.patch.object(operator_mod, "load_preset", return_value=strict_json.pretty_file_bytes(document)), \
                self.fail_after(claude_multi.secret_store.FileSecretStore, "set"):
            code, _out, err = self.op(["providers", "add", "--preset", "acme-keyed", "--as", "acme",
                                       "--secret-file", str(key)], "y\n")
        self.assertNotEqual(code, 0, err)
        self.assertNotIn("acme-imported-value", err)
        self.assertEqual(claude_multi.secret_store.default_store(self.runtime.environ).get("ACME_API_KEY"),
                         "acme-dummy-value")
        self.assertFalse((self.pdir / "acme.json").exists())
        self.assertNotIn("acme", self.routes())


class OperatorSuccessorRaceTests(OperatorCommandCase):
    """``models rm --successor`` revalidates the reviewed
    references at the commit boundary and never overwrites a newer choice."""

    def setUp(self) -> None:
        super().setUp()
        self.declare_small()
        self.serve_current()
        self.assertEqual(self.op(["models", "admit", "custom-acme-small"], "y\n")[0], 0)
        self.op(["models", "add", "acme", "acme-large-1", "--as", "custom-acme-large", "--context", "262144",
                 "--source", "operator", "--effort", "high"])
        self.serve_current()
        self.assertEqual(self.op(["models", "admit", "custom-acme-large"], "y\n")[0], 0)
        document = copy.deepcopy(self.runtime.profiles.load("direct"))
        document.pop("seed", None)
        document["name"] = "opline"
        document["lead"] = {"model": "custom-acme-small", "effort": "high"}
        self.runtime.profiles.save(document)

    def lead(self) -> dict:
        return self.runtime.profiles.load("opline")["lead"]

    def test_successor_rewrites_the_reviewed_slot(self) -> None:
        code, out, err = self.op(["models", "rm", "custom-acme-small", "--successor", "custom-acme-large"], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("profile opline · lead: custom-acme-small → custom-acme-large", out)
        self.assertIn("Removed custom-acme-small and moved 1 binding(s) to custom-acme-large", out)
        self.assertEqual(self.lead()["model"], "custom-acme-large")

    def test_a_slot_changed_during_the_confirmation_is_never_overwritten(self) -> None:
        catalog_lead = next(key for key, line in sorted(self.runtime.catalog.lines.items())
                            if line.get("status") == "active" and "lead" in line.get("capabilities", ())
                            and line["lead"] and line["lead"]["effort"] == "ultracode")
        original = consent_mod.confirm

        def confirm(prompt, **kwargs):
            answer = original(prompt, **kwargs)
            if "is removed after these changes" in prompt:
                def change(doc):
                    doc["lead"] = {"model": catalog_lead, "effort": "ultracode"}
                self.runtime.profiles.update("opline", change)
            return answer

        before = (self.pdir / "acme.json").read_bytes()
        with mock.patch.object(consent_mod, "confirm", side_effect=confirm):
            code, _out, err = self.op(["models", "rm", "custom-acme-small", "--successor", "custom-acme-large"],
                                      "y\n")
        self.assertEqual(code, 1)
        self.assertIn("the references to custom-acme-small changed while you were deciding — nothing written; "
                      "try again", err)
        self.assertEqual(self.lead(), {"model": catalog_lead, "effort": "ultracode"})
        self.assertEqual((self.pdir / "acme.json").read_bytes(), before)
        self.assertNotIn("custom-acme-small", self.ledger().removed)

    def test_a_session_started_during_the_confirmation_refuses(self) -> None:
        original = consent_mod.confirm
        sessions_dir = state.ensure_private_dir(self.runtime.session_store.root / "sessions")

        def confirm(prompt, **kwargs):
            answer = original(prompt, **kwargs)
            record = {"version": 4, "applied": {"lead": {"key": "custom-acme-small", "selector": "custom-acme-small"},
                                                "agents": {}}, "last_event_source": "start"}
            state.atomic_write(sessions_dir / "44444444-4444-4444-8444-444444444444.json",
                               strict_json.canonical_file_bytes(record))
            return answer

        with mock.patch.object(consent_mod, "confirm", side_effect=confirm):
            code, _out, err = self.op(["models", "rm", "custom-acme-small", "--successor", "custom-acme-large"],
                                      "y\n")
        self.assertEqual(code, 1)
        self.assertIn("live sessions use it (44444444) — nothing changed", err)
        self.assertIn("claude-multi sessions mark-ended 44444444-4444-4444-8444-444444444444", err)
        self.assertEqual(self.lead()["model"], "custom-acme-small")

    def test_the_profile_update_itself_refuses_a_stale_slot(self) -> None:
        """Belt and braces: the locked profile write checks the reviewed spec."""

        catalog_lead = next(key for key, line in sorted(self.runtime.catalog.lines.items())
                            if line.get("status") == "active" and "lead" in line.get("capabilities", ())
                            and line["lead"] and line["lead"]["effort"] == "ultracode")
        from claude_multi.cli.commands import models as models_cmd
        real = models_cmd._successor_rewrites
        calls = []

        def rewrites(runtime, key, successor):
            found = real(runtime, key, successor)
            calls.append(len(found))
            if len(calls) == 2:  # the commit-boundary recheck: change the slot just after it
                def change(doc):
                    doc["lead"] = {"model": catalog_lead, "effort": "ultracode"}
                self.runtime.profiles.update("opline", change)
            return found

        with mock.patch.object(models_cmd, "_successor_rewrites", side_effect=rewrites):
            code, _out, err = self.op(["models", "rm", "custom-acme-small", "--successor", "custom-acme-large"],
                                      "y\n")
        self.assertEqual(code, 1)
        self.assertIn("profile opline lead changed while you were deciding — nothing written", err)
        self.assertEqual(self.lead(), {"model": catalog_lead, "effort": "ultracode"})
        self.assertIn("custom-acme-small", self.runtime.operator_snapshot().layer.lines)


# ---------------------------------------------------------------------------
# Presets and reviewed transport alternatives.
from _catalog import SHIPPED_ROOT as _SHIPPED_PRESETS_ROOT

_LOAD_PRESET = operator_mod.load_preset


class OperatorPresetAndTransportTests(OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        # The fixture asset root ships no presets: read the shipped reviewed ones.
        patcher = mock.patch.object(operator_mod, "load_preset",
                                    side_effect=lambda name, _root=None: _LOAD_PRESET(name, _SHIPPED_PRESETS_ROOT))
        patcher.start()
        self.addCleanup(patcher.stop)

    def key_file(self, value: bytes = b"platform-dummy-key\n") -> Path:
        path = self.root / "platform.key"
        state.atomic_write(path, value)
        return path

    def test_preset_declares_a_keyless_lan_provider_even_unattended(self) -> None:
        code, out, err = self.op(["providers", "add", "--preset", "lan-openai-compatible", "--as", "lanbox",
                                  "--base-url", "http://box.lan:8000/v1"], tty=False, env={"CLAUDECODE": "1"})
        self.assertEqual(code, 0, err)
        self.assertIn("declared provider lanbox from preset lan-openai-compatible", out)
        self.assertIn("keyless LAN route", out)
        document = json.loads((self.pdir / "lanbox.json").read_text())
        self.assertEqual(document["provider"]["base_url"], "http://box.lan:8000/v1")
        self.assertEqual(document["provider"]["listing"]["url"], "http://box.lan:8000/v1/models")
        ledger = self.ledger() if self.ledger_file.exists() else None
        self.assertEqual(ledger.routes if ledger else {}, {})
        # Exists: never overwritten.
        before = self.state_bytes()
        code, _out, err = self.op(["providers", "add", "--preset", "lan-openai-compatible", "--as", "lanbox"])
        self.assertEqual(code, 1)
        self.assertIn("exists", err)
        self.assertEqual(self.state_bytes(), before)

    def test_preset_never_overrides_a_catalog_provider_and_forms_are_exclusive(self) -> None:
        before = self.state_bytes()
        # The fixture still carries llm-local as a catalog provider.
        code, _out, err = self.op(["providers", "add", "--preset", "llm-local"])
        self.assertEqual(code, 1)
        self.assertIn("is a catalog provider", err)
        code, _out, err = self.op(["providers", "add", "--preset", "no-such-preset"])
        self.assertEqual(code, 1)
        self.assertIn("unknown preset", err)
        for argv, needle in (
            (["providers", "add", "x", "--preset", "lan-openai-compatible"], "--as ID"),
            (["providers", "add", "--preset", "lan-openai-compatible", "--kind", "openai-compatible-lan"],
             "exclusive"),
            (["providers", "add", "--preset", "lan-openai-compatible", "--secret-file", "k", "--declare-only"],
             "--declare-only"),
            (["providers", "add", "x", "--kind", "openai-compatible-lan"], "required"),
            (["providers", "add", "x", "--as", "y", "--kind", "openai-compatible-lan"], "--preset only"),
        ):
            with self.subTest(argv=argv):
                code, _out, err = self.op(argv)
                self.assertEqual(code, 2, err)
                self.assertIn(needle, err)
        self.assertEqual(self.state_bytes(), before)

    def test_preset_key_import_passes_the_human_guard_first(self) -> None:
        key = self.key_file()
        before = self.state_bytes()
        store = mock.Mock(side_effect=AssertionError("secret store read"))
        with mock.patch.object(claude_multi.secret_store, "default_store", store):
            code, _out, err = self.op(["providers", "add", "--preset", "lan-openai-compatible", "--as", "lanbox",
                                       "--secret-file", str(key)], env={"CLAUDE_MULTI_MANAGED_ID": "x"})
        self.assertEqual(code, 1)
        self.assertIn("CLAUDE_MULTI_MANAGED_ID set", err)
        self.assertEqual(self.state_bytes(), before)
        code, _out, err = self.op(["providers", "add", "--preset", "lan-openai-compatible", "--as", "lanbox",
                                   "--secret-file", str(key)])
        self.assertEqual(code, 1)
        self.assertIn("keyless", err)
        self.assertNotIn("platform-dummy-key", err)

    def test_transport_show_and_closed_platform_route(self) -> None:
        code, out, err = self.op(["providers", "transport", "anthropic"], tty=False, env={"CLAUDECODE": "1"})
        self.assertEqual(code, 0, err)
        self.assertIn("anthropic\ttransport\toauth-pool", out)
        self.assertIn("anthropic\tapi-key\theader env:PLATFORM_ANTHROPIC_API_KEY → https://api.anthropic.com", out)
        code, out, _err = self.op(["providers", "transport", "openai"])
        self.assertIn("openai\tapi-key\tclosed: ", out)
        before = self.state_bytes()
        code, _out, err = self.op(["providers", "transport", "openai", "api-key"], "y\n")
        self.assertEqual(code, 1)
        # Offered by this build; the fixture reviews no OpenAI line for it.
        self.assertIn("no OpenAI model is reviewed for its api-key transport", err)
        code, _out, err = self.op(["providers", "transport", "kimi", "api-key"], "y\n")
        self.assertEqual(code, 1)
        code, _out, err = self.op(["providers", "transport", "anthropic", "api-key"], "y\n",
                                  env={"CLAUDECODE": "1"})
        self.assertEqual(code, 1)
        self.assertIn("CLAUDECODE set", err)
        code, _out, err = self.op(["providers", "transport", "anthropic", "api-key"], "n\n")
        self.assertEqual(code, 3)
        self.assertIn("not approved", err)
        self.assertEqual(self.state_bytes(), before)

    def test_transport_switch_keeps_selectors_and_switches_back(self) -> None:
        key = self.key_file()
        code, out, err = self.op(["providers", "transport", "anthropic", "api-key", "--secret-file", str(key)],
                                 "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("Claude models now use your Anthropic API key — the gateway reloaded", out)
        self.assertIn("model selector(s) move to https://api.anthropic.com — same names, never both at once", err)
        self.assertNotIn("platform-dummy-key", out + err)
        ledger = self.ledger()
        alternative = operator_mod.transport_alternative("anthropic", "api-key")
        self.assertEqual(ledger.transport_choices, {"anthropic": "api-key"})
        self.assertEqual(ledger.routes["anthropic"]["rd"], alternative.route_digest)
        self.assertEqual(claude_multi.secret_store.default_store(self.runtime.environ)
                         .get("PLATFORM_ANTHROPIC_API_KEY"), "platform-dummy-key")
        config = (claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml").read_text()
        self.assertIn('oauth-excluded-models:\n  claude:\n    - "*"', config)
        self.assertIn('base-url: "https://api.anthropic.com"', config)
        snap = gateway_facts._gateway_snapshot(self.runtime, self.runtime.gateway_token())
        self.assertIs(snap.config_drift, False)
        self.assertFalse(any(pool == "claude" for pool in snap.oauth_alias_pools.values()))
        code, out, err = self.op(["providers", "transport", "anthropic", "api-key"], "y\n")
        self.assertIn("already uses the api-key transport", out)
        code, out, err = self.op(["providers", "transport", "anthropic", "oauth-pool"], "y\nn\n")
        self.assertEqual(code, 0, err)
        self.assertIn("Claude models use your Claude account sign-in again", out)
        self.assertIn("Also remove the saved Anthropic API key (PLATFORM_ANTHROPIC_API_KEY)? [y/N] ", err)
        self.assertTrue(claude_multi.secret_store.default_store(self.runtime.environ)
                        .is_set("PLATFORM_ANTHROPIC_API_KEY"))  # "no" keeps it
        ledger = self.ledger()
        self.assertEqual((ledger.transport_choices, ledger.routes), ({}, {}))
        config = (claude_multi.proxy.config_dir(self.runtime.home) / "config.yaml").read_text()
        self.assertNotIn("oauth-excluded-models", config)

    def test_readiness_observes_the_selected_api_key_transport(self) -> None:
        """An approved API-key transport with its key present
        is ready with no OAuth credential record (the effective view, as on
        the Providers screen); catalog intent is unchanged."""

        from claude_multi import profile, readiness

        lcat = self.runtime.lineup_catalog()
        key = next(k for k, entry in sorted(lcat.lines.items())
                   if entry["provider"] == "anthropic" and entry.get("lead") is not None)
        evaluation = profile.evaluate(profile.ad_hoc_direct(key), lcat, bindings={},
                                      effective=self.runtime.current_effective(), ad_hoc=True)
        self.assertEqual(evaluation.errors, ())
        no_records = dict(served=None, oauth_records={}, lan={}, login={})
        observations = readiness.Observations(**no_records)
        before = self.runtime.lineup_readiness(evaluation.lineup, observations)
        self.assertEqual(before[0].state, readiness.BLOCKED)
        code, _out, err = self.op(["providers", "transport", "anthropic", "api-key", "--secret-file",
                                   str(self.key_file())], "y\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.runtime.lineup_secret_problems(evaluation.lineup), [])
        after = self.runtime.lineup_readiness(evaluation.lineup, observations)
        self.assertNotEqual(after[0].state, readiness.BLOCKED, after[0].text())
        self.assertEqual(self.runtime.lineup_catalog().providers["anthropic"]["transport"]["kind"], "oauth-pool")


class AgentClassRollbackTests(unittest.TestCase):
    """A record whose agents run in the 1M class stays readable where the
    same line's agents are in the 200K class (a catalog in which its bound
    is below the session window, as an earlier release reads it): the
    closed session schema accepts it, the running session keeps its recorded
    selectors with a doctor Attention (never BLOCK), and its next resume
    moves them with an explicit "agent class 1M → 200K" row. The line is
    chosen by shape (``test_agent_context.codex_line``), outside the lead set."""

    def setUp(self) -> None:
        import _v4
        import test_agent_context as ac

        case = _v4.V4Case("run")
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.case = case
        self.ac = ac
        self.before_root, self.key = ac.write_bounded_assets(case.root / "assets-200k", ac.codex_line, ac.BELOW)

    def test_a_1m_record_reads_back_where_the_line_is_in_the_200k_class(self) -> None:
        from claude_multi import validate as schema_validate
        from claude_multi.cli import doctor

        case = self.case
        case.runtime = case.make_runtime(asset_root=CATALOG_ROOT)
        document = self.ac.narrowed_balanced(case.runtime.catalog)
        case.runtime.profiles.new(document)
        record = case.launch_fresh(cli.LaunchTarget("profile", document, document["name"], False, "Narrow"))
        mid = record["managed_id"]
        slots = sorted(rid for rid, b in record["applied"]["agents"].items() if b["key"] == self.key)
        self.assertTrue(slots)
        self.assertTrue(all(record["applied"]["agents"][rid]["selector"].endswith("[1m]") for rid in slots))
        schema = strict_json.load(CATALOG_ROOT / "schemas" / "session.schema.json")
        self.assertEqual(schema_validate.validate(record, schema, "$"), [])
        # The 200K-class view reads the session: a pending move, never BLOCK.
        case.runtime = case.make_runtime(asset_root=self.before_root)
        problems, attention, _info = doctor._check_scope_integrity(case.runtime, case.store.load(mid))
        self.assertEqual(problems, [])
        notice = next(line for line in attention if "agent context class changes at the next resume" in line)
        self.assertIn(f"{profile_mod.label(slots[0])} 1M → 200K", notice)
        prepared = case.prepare_resume(mid)
        rows = [line for line in prepared.diff if "agent class" in line]
        self.assertEqual(len(rows), len(slots), prepared.diff)
        self.assertTrue(all(line.rstrip().endswith("1M → 200K") for line in rows))
        case.runtime.perform(prepared)
        resumed = case.store.load(mid)
        for rid in slots:
            self.assertFalse(resumed["applied"]["agents"][rid]["selector"].endswith("[1m]"))
        self.assertEqual(schema_validate.validate(resumed, schema, "$"), [])
