"""``claude-multi profile …`` and its 2.x aliases.

``list``/``show`` are read commands; every other subcommand is write-allowed
and installs the absent seeds first. ``new``/``edit`` run
``$VISUAL``/``$EDITOR`` on a 0600 temp copy (an injected editor script here)
without the TUI editor; saves, reseeds, removals and renames propagate to
following sessions. ``profile migrate`` has its own module; here: the parser
(``--dry-run``/``--apply`` exclusive, bare = dry run), the read-only Runtime
formula and the dispatch before ``install_seeds``.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_multi import catalog, cli, paths, profile, sessions, strict_json
from claude_multi.cli.commands import profile as profile_cmd
from _catalog import FIXTURE_ROOT
from test_lineup import LineupCase
from test_migrate import _cli_environ
import claude_multi.sessions


class ProfileCliCase(LineupCase):
    launch_profile = None

    def cli(self, *argv: str, text: str | None = None, interactive: bool = False):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(
                list(argv),
                runtime=self.runtime,
                output_stream=out,
                input_stream=io.StringIO(text or "") if interactive else None,
                interactive=interactive,
            )
        return code, out.getvalue(), err.getvalue()

    def editor(self, body: str) -> None:
        """An ``$EDITOR`` script: ``body`` is Python run with the temp path as argv[1]."""

        script = self.root / "editor.py"
        script.write_text("import sys, json\npath = sys.argv[1]\n" + body)
        self.runtime.environ["EDITOR"] = f"python3 {script}"


class ReadCommandTests(ProfileCliCase):
    def test_list_shows_origin_and_last_use(self) -> None:
        self.runtime.profiles.duplicate("balanced", "mine")
        self.launch_fresh(self.profile_target("mine"))
        code, out, _ = self.cli("profile", "list")
        self.assertEqual(code, 0)
        header, *lines = out.splitlines()
        self.assertEqual(header, "profile\torigin\tstate\tused\tnotes")
        rows = {line.split("\t")[0]: line.split("\t")[1:] for line in lines}
        self.assertEqual(rows["balanced"][0], "seed")
        self.assertEqual(rows["balanced"][2], "-")
        self.assertEqual(rows["mine"][0], "yours")
        self.assertNotEqual(rows["mine"][2], "-")
        self.assertEqual(set(rows), set(self.runtime.profiles.names()))
        # A primary provider marks a fallback profile.
        self.assertIn("fallback", rows["claude"][3].split(" · "))

    def test_show_defaults_to_the_seed_and_refuses_a_2x_name(self) -> None:
        code, out, _ = self.cli("profile", "show")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("claude-multi · profile balanced · lead "), out)
        self.assertNotIn("/cm profile", out)
        code, _out, err = self.cli("profile", "show", "default")
        self.assertEqual(code, 1)
        self.assertIn(cli.PROFILE_IS_COMPOSITION.format(name="default"), err)
        code, _out, err = self.cli("profile", "show", "nosuch")
        self.assertEqual(code, 1)
        self.assertIn("profile 'nosuch' does not exist", err)

    def test_read_commands_install_no_seed(self) -> None:
        self.cli("profile", "list")
        self.cli("profile", "show", "balanced")
        self.assertFalse(self.runtime.profiles.root.exists())


class WriteCommandTests(ProfileCliCase):
    def test_new_from_duplicate_rename_rm_and_seed_refusals(self) -> None:
        code, out, _ = self.cli("profile", "new", "a", "--from", "balanced")
        self.assertEqual((code, out), (0, "Created 'a' from 'balanced'.\n"))
        # write-allowed commands install the absent seeds first
        self.assertTrue(self.runtime.profiles.has_user("quality"))
        self.assertEqual(self.cli("profile", "duplicate", "a", "b")[0], 0)
        self.assertEqual(self.cli("profile", "rename", "b", "c")[1], "Renamed 'b' to 'c'.\n")
        # Off a terminal, rm needs --yes; on one it asks (No keeps the profile).
        code, _out, err = self.cli("profile", "rm", "c")
        self.assertEqual((code, err), (1, "profile rm c: needs a terminal to confirm, or --yes\n"))
        code, out, err = self.cli("profile", "rm", "c", text="n\n", interactive=True)
        self.assertEqual(code, 3)
        self.assertEqual(err, "Delete c? A copy is kept. [y/N] ")  # the question is on stderr
        self.assertTrue(out.endswith("Nothing changed.\n"), out)
        self.assertTrue(self.runtime.profiles.has_user("c"))
        code, out, _ = self.cli("profile", "rm", "c", text="y\n", interactive=True)
        self.assertEqual(code, 0, out)
        (kept,) = self.runtime.profiles.root.glob(".c.removed-*.json")
        self.assertIn(f"Deleted c; a copy is kept as {paths.display(kept, self.runtime.environ)}.", out)
        self.assertFalse(self.runtime.profiles.has_user("c"))
        self.assertEqual(self.cli("profile", "rm", "c")[1:], ("", "claude-multi: profile 'c' does not exist\n"))
        code, _out, err = self.cli("profile", "rm", "balanced")
        self.assertEqual(code, 1)
        self.assertIn("balanced is a shipped profile and cannot be deleted — restore its shipped version: "
                      "claude-multi profile reseed balanced", err)
        code, _out, err = self.cli("profile", "rename", "balanced", "x")
        self.assertEqual(code, 1)
        self.assertIn("balanced is a shipped profile and keeps its name — copy it under a new name: "
                      "claude-multi profile duplicate balanced NEW", err)
        code, _out, err = self.cli("profile", "new", "a", "--from", "balanced")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

    def test_new_and_edit_need_an_editor_and_a_terminal(self) -> None:
        code, _out, err = self.cli("profile", "new", "x", interactive=True)
        self.assertEqual(code, 1)
        self.assertIn(cli.PROFILE_EDITOR_REFUSAL.format(verb="new"), err)
        code, _out, err = self.cli("profile", "edit", "balanced", interactive=True)
        self.assertIn(cli.PROFILE_EDITOR_REFUSAL.format(verb="edit"), err)
        self.editor("pass\n")
        code, _out, err = self.cli("profile", "edit", "balanced")
        self.assertEqual(code, 1)
        self.assertIn("needs a terminal for the editor", err)

    def test_new_with_an_editor_saves_the_edited_profile(self) -> None:
        self.editor(
            "doc = json.load(open(path))\n"
            "doc['description'] = 'from the editor'\n"
            "open(path, 'w').write(json.dumps(doc))\n"
        )
        code, out, _ = self.cli("profile", "new", "fresh", interactive=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(out, "Saved profile 'fresh'.\n")
        saved = self.runtime.profiles.load("fresh")
        self.assertEqual(saved["description"], "from the editor")
        self.assertNotIn("seed", saved)

    def test_an_invalid_edit_is_shown_and_kept(self) -> None:
        self.editor(
            "doc = json.load(open(path))\n"
            "doc['agents']['cm-analyst']['effort'] = 'ultracode'\n"
            "open(path, 'w').write(json.dumps(doc))\n"
        )
        before = self.runtime.profiles.load("balanced")
        code, out, _ = self.cli("profile", "edit", "balanced", interactive=True)
        self.assertEqual(code, 1)
        self.assertIn("profile balanced was not saved:", out)
        self.assertIn("agents.cm-analyst.effort: 'ultracode' is lead-only", out)
        kept = Path(out.rsplit("your edit is kept at ", 1)[1].strip())
        self.addCleanup(kept.unlink, True)
        self.assertEqual(stat.S_IMODE(os.stat(kept).st_mode), 0o600)
        self.assertEqual(strict_json.load(kept)["agents"]["cm-analyst"]["effort"], "ultracode")
        self.assertEqual(self.runtime.profiles.load("balanced"), before)

    def test_missing_explore_replacement_is_saved_with_a_warning(self) -> None:
        self.editor(
            "doc = json.load(open(path))\n"
            "doc['agents'].pop('cm-explorer')\n"
            "open(path, 'w').write(json.dumps(doc))\n"
        )
        code, out, err = self.cli("profile", "edit", "balanced", interactive=True)
        self.assertEqual(code, 0, out + err)
        self.assertIn("Saved profile 'balanced'.", out)
        saved = self.runtime.profiles.load("balanced")
        self.assertNotIn("cm-explorer", saved["agents"])
        self.assertEqual(saved["native_agents"]["explore"], "replace")
        code, out, err = self.cli("profile", "show", "balanced")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Explore remains disabled", out)

    def test_edit_propagates_to_a_running_follower_after_the_prompt(self) -> None:
        follower = self.launch_fresh(self.profile_target("balanced"))
        self.editor(
            "doc = json.load(open(path))\n"
            "doc['agents']['cm-implementer'] = {'model': 'opus55', 'effort': 'xhigh'}\n"
            "open(path, 'w').write(json.dumps(doc))\n"
        )
        code, out, _ = self.cli("profile", "edit", "balanced", text="y\n", interactive=True)
        self.assertEqual(code, 0, out)
        self.assertIn("1 session(s) follow balanced (1 running)", out)
        self.assertIn("apply to 1 running session(s) now? [y/N] ", out)
        self.assertIn("applied live (lineup gen 2)", out)
        self.assertEqual(self.store.load(follower["managed_id"])["applied"]["agents"]
                         ["cm-implementer"]["key"], "opus55")

    def test_non_interactive_reseed_does_not_apply_live(self) -> None:
        follower = self.launch_fresh(self.profile_target("balanced"))
        self.runtime.profiles.update("balanced", lambda d: d["agents"].update(
            {"cm-implementer": {"model": "opus55", "effort": "xhigh"}}))
        before = self.record_bytes(follower["managed_id"])
        code, _out, err = self.cli("profile", "reseed", "balanced")
        self.assertEqual((code, err), (1, "profile reseed balanced: needs a terminal to confirm, or --yes\n"))
        out = io.StringIO()
        code = profile_cmd._profile_command(
            self.runtime, argparse.Namespace(name="balanced", yes=True), "reseed",
            input_stream=io.StringIO(""), output_stream=out, interactive=False)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("implementer   opus55 · xhigh       →  ", out.getvalue())
        self.assertIn("Restored balanced to shipped version ", out.getvalue())
        self.assertIn("not applied — apply with: claude-multi lineup --session", out.getvalue())
        self.assertEqual(self.record_bytes(follower["managed_id"]), before)
        code, out_text, _ = self.cli("profile", "reseed", "balanced")
        self.assertEqual((code, out_text), (0, "balanced already matches its shipped version.\n"))

    def test_rm_and_rename_pin_their_followers(self) -> None:
        self.cli("profile", "new", "mine", "--from", "balanced")
        follower = self.launch_fresh(self.profile_target("mine"))
        code, out, _ = self.cli("profile", "rename", "mine", "ours")
        self.assertIn("profile 'mine' was renamed to 'ours'; the session keeps its applied "
                      "lineup and is now pinned", out)
        self.assertFalse(self.store.load(follower["managed_id"])["follow"])


class ProfileMigrateTests(unittest.TestCase):
    """The dispatch, its parser and the read-only Runtime."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-multi-profile-migrate-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.environ = _cli_environ(self.tmp)

    def run_main(self, *argv: str, spy_seeds: bool = False) -> tuple[list[dict], int, str, list]:
        captured: list[dict] = []
        callers: list[tuple[str, str]] = []
        environ = self.environ
        tmp = self.tmp
        real_runtime = cli.Runtime
        real_install = profile.ProfileStore.install_seeds
        package = str(Path(cli.__file__).resolve().parent.parent)

        def runtime_factory(**kwargs):
            captured.append(kwargs)
            return real_runtime(asset_root=FIXTURE_ROOT, environ=environ, cwd=tmp,
                               allow_state_writes=kwargs["allow_state_writes"],
                               refresh_shims=kwargs["refresh_shims"])

        def recording_install(store):
            # The first claude_multi frame above this spy is the caller.
            frame = next(
                info for info in inspect.stack()[1:]
                if str(Path(info.filename).resolve()).startswith(package)
            )
            callers.append((frame.frame.f_globals["__name__"], frame.function))
            return real_install(store)

        if spy_seeds:
            # --apply: a spy wrapping the real method records its caller.
            seeds_patch = mock.patch.object(profile.ProfileStore, "install_seeds", autospec=True,
                                            side_effect=recording_install)
        else:
            # Dry runs: install_seeds must never run (the dispatch comes
            # before the install_seeds preamble); AssertionError is not caught by main.
            seeds_patch = mock.patch.object(profile.ProfileStore, "install_seeds",
                                            side_effect=AssertionError("install_seeds before H9"))
        out = io.StringIO()
        with mock.patch.dict(os.environ, environ), \
                mock.patch('claude_multi.cli.runtime.Runtime', side_effect=runtime_factory), \
                mock.patch.object(claude_multi.sessions, "state_root",
                                  return_value=sessions.state_root(environ)), \
                seeds_patch, contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv), output_stream=out, interactive=False)
        return captured, code, out.getvalue(), callers

    def test_a_dry_run_is_read_only_and_writes_no_shim(self) -> None:
        for argv in (["profile", "migrate"], ["profile", "migrate", "--dry-run"]):
            with self.subTest(argv=argv):
                captured, code, out, _callers = self.run_main(*argv)
                self.assertEqual(code, 0, out)
                self.assertTrue(out.startswith("claude-multi profile migrate --dry-run: config "), out)
                self.assertIn("(directory absent)", out)
                self.assertIs(captured[0]["refresh_shims"], False)
                self.assertIs(captured[0]["allow_state_writes"], False)
                self.assertFalse((sessions.state_root(self.environ) / "bin").exists())
                self.assertFalse((sessions.config_root(self.environ) / "profiles").exists())

    def test_apply_is_a_writer_and_installs_the_seeds_from_apply_plan(self) -> None:
        captured, code, out, callers = self.run_main("profile", "migrate", "--apply", spy_seeds=True)
        self.assertEqual(code, 0, out)
        self.assertIs(captured[0]["refresh_shims"], True)
        self.assertIs(captured[0]["allow_state_writes"], True)
        self.assertEqual(callers, [("claude_multi.migrate_profiles", "apply_plan")])
        profiles = sessions.config_root(self.environ) / "profiles"
        self.assertEqual(sorted(p.stem for p in profiles.glob("*.json") if not p.name.startswith(".")),
                         sorted(catalog.SEED_PROFILE_NAMES))
        self.assertTrue(out.endswith(
            f"applied: wrote 0 profiles, installed {len(catalog.SEED_PROFILE_NAMES)} seeds; "
            "compositions untouched\n"), out)

    def test_dry_run_and_apply_are_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["profile", "migrate", "--dry-run", "--apply"])


if __name__ == "__main__":
    unittest.main()
