"""``claude-multi uninstall``: the plan first, removal only through the
installer receipt's proofs, credentials only after the typed phrase, kept
backups and Claude Code's own files never touched, and the exit statuses.

Also: roots that are links are left as they are and removal never follows a
link; every key file is a credential; the previous release is kept for
rollback; sessions that may run refuse, checked again under the session
state's lock after the confirmation; a launcher changed after the plan is
kept; the persistence hold refuses for a stopped gateway too; and a recorded
service goes through its own uninstall.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
from pathlib import Path
from unittest import mock

import test_cli
from _layout import REPO_ROOT
from claude_multi import endpoint, gateway_lifecycle as gl, gateway_service as gs, install_receipt, paths, \
    secret_store, service, sessions, state
from claude_multi.platform import file_log, observation, posix_fs, systemd_unit
from claude_multi.setup import external, texts, uninstall
import claude_multi.cli.consent as consent

MARK = "# added by the claude-multi installer"
EXPORT = f'export PATH="$HOME/.local/bin:$PATH" {MARK}'
RECORD_ID = "abcdef12-3456-4789-8abc-def012345678"


class UninstallCase(test_cli.OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = Path(self.runtime.environ["HOME"])
        self.data = paths.data_root(self.runtime.environ)
        self.install = self.data / "install"
        self.state_dir = Path(self.runtime.session_store.root)
        (self.root / "proc").mkdir(exist_ok=True)  # an empty, readable process table

    def file(self, path: Path, data: bytes = b"x\n", mode: int = 0o600) -> Path:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def installed(self, *, receipt_format: int = 2) -> dict[str, Path]:
        """A bundle install: release files, two launchers and one PATH line."""

        self.file(self.install / "versions" / "1.0.0" / "bin" / "claude-multi", b"#!/bin/sh\n", 0o755)
        launchers = [self.file(self.home / ".local/bin" / name, f"# launcher {name}\n".encode(), 0o755)
                     for name in ("claude-multi", "claude-multi-proxy")]
        profile = self.file(self.home / ".bashrc", f"alias ll='ls -l'\n\n{EXPORT}\n# mine\n".encode(), 0o644)
        if receipt_format == 2:  # written by the installer's own receipt writer
            os.chmod(self.install, 0o700)  # as the installer makes it
            install_receipt.record(self.install, launchers, (str(profile), EXPORT))
        else:  # an earlier development format: paths and markers, no checksums
            document = {"format": 1, "launchers": sorted(str(path) for path in launchers),
                        "path_lines": [{"file": str(profile), "marker": MARK}]}
            self.file(paths.installer_receipt(self.runtime.environ), json.dumps(document).encode())
        return {"launcher": launchers[0], "proxy": launchers[1], "profile": profile}

    def everything(self) -> dict[str, Path]:
        made = {
            "claude": self.file(self.data / "claude" / "2.1.0" / "claude", b"binary", 0o755),
            "trace": self.file(self.data / "traces" / "t.json"),
            "record": self.file(self.state_dir / "sessions" / "abc.json"),
            "kept-record": self.file(self.state_dir / "sessions" / "old.v3.json"),
            "profile": self.file(paths.config_root(dict(self.runtime.environ)) / "profiles" / "mine.json", b"{}"),
            "account": self.file(self.data / "auth" / "claude-a@example.com.json", b"{}"),
            "signed-out": self.file(self.data / "auth.signed-out.claude.20261003" / "claude-b@example.com.json"),
            "retained": self.file(self.data / "pinned-clients" / "2.0.0" / "claude", b"old"),
            "native": self.file(self.home / ".claude" / "settings.json", b"{}"),
        }
        return made

    def uninstall(self, *argv: str, text: str = "", tty: bool = True, env=None):
        return self.op(["uninstall", *argv], text, tty=tty, env=env)

    def where(self, plan: uninstall.UninstallPlan) -> dict[Path, str]:
        return {entry.path: entry.cls for group in (plan.program, plan.claude, plan.sessions, plan.setup,
                                                    plan.credentials, plan.never) for entry in group}

    def ended_record(self, managed_id: str = RECORD_ID) -> dict:
        """A valid v4 record whose session recorded its end."""

        import _tui_fixture as fx

        return fx.v4_record(self.runtime, "balanced", managed_id=managed_id, ended=True)

    def record_path(self, managed_id: str = RECORD_ID) -> Path:
        return self.state_dir / "sessions" / f"{managed_id}.json"

    # ---------------------------------------------------------- a real gateway over a fake world
    def live_gateway(self, *, state_root: Path | None = None) -> gl.Gateway:
        """A real lifecycle (and service) over the lifecycle tests' fake world:
        no process, port or service manager is touched."""

        import test_gateway_lifecycle as lifecycle_tests
        import test_gateway_service as service_tests

        root = state_root or self.state_dir
        self.workdir = service.ensure_gateway_workdir(root)
        self.unit_dir = self.home / ".config" / "systemd" / "user"
        self.events: list[str] = []
        self.world = lifecycle_tests.World(self)
        self.manager = service_tests.Manager(self)
        environ = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "CLAUDE_MULTI_CHANNEL": "bundle"}
        seams = self.world.seams(runner=self.manager,
                                 journal=lambda _unit, _since: observation.LogWindow((), "bounded"))

        def gateway(**_overrides) -> gl.Gateway:
            return gl.Gateway(home=self.home, state_root=root, environ=environ,
                              gateway_document=self.runtime.catalog.docs["gateway"], installation=REPO_ROOT,
                              providers=("claude", "codex"), seams=seams, platform="linux")

        def gateway_service() -> gs.GatewayService:
            return gs.GatewayService(gateway(), seams=gs.ServiceSeams(
                runner=self.manager, which=lambda _name, path=None: None))

        for patcher in (mock.patch.object(external, "_fixture_gateway", return_value=False),
                        mock.patch.object(self.runtime, "gateway", side_effect=gateway),
                        mock.patch.object(self.runtime, "gateway_service", side_effect=gateway_service)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return gateway()


class ReceiptTests(UninstallCase):
    def test_format_2_proves_each_launcher_and_line(self) -> None:
        made = self.installed()
        receipt = uninstall.read_receipt(self.runtime.environ)
        self.assertEqual(receipt.format, 2)
        plan = uninstall.build_plan(self.runtime)
        self.assertEqual({entry.path for entry in plan.program if entry.note == "launcher"},
                         {made["launcher"], made["proxy"]})
        self.assertEqual([edit.file for edit in plan.path_edits], [made["profile"]])
        # A launcher changed since it was installed is kept and named.
        made["proxy"].write_text("# edited by hand\n")
        plan = uninstall.build_plan(self.runtime)
        self.assertNotIn(made["proxy"], {entry.path for entry in plan.program})
        self.assertIn(texts.UNINSTALL_CHANGED_WRAPPER.format(path="~/.local/bin/claude-multi-proxy"),
                      plan.kept_program)

    def test_a_launcher_replaced_while_the_question_waits_is_kept(self) -> None:
        made = self.installed()
        replacement = b"#!/bin/sh\n# put here by another installer\n"

        def replace(_text, *, input_stream=None):
            made["proxy"].write_bytes(replacement)
            temporary = made["launcher"].with_name(".swap")
            temporary.write_bytes(made["launcher"].read_bytes())  # the same bytes, another file
            os.replace(temporary, made["launcher"])
            return True

        with mock.patch.object(consent, "confirm", side_effect=replace):
            code, out, err = self.uninstall("--keep-credentials", text="")
        self.assertEqual(code, 1, out + err)
        self.assertEqual(made["proxy"].read_bytes(), replacement)
        self.assertTrue(made["launcher"].exists())
        self.assertIn(texts.UNINSTALL_CHANGED_SINCE_PLAN.format(path="~/.local/bin/claude-multi-proxy"), out)
        self.assertIn(texts.UNINSTALL_CHANGED_SINCE_PLAN.format(path="~/.local/bin/claude-multi"), out)
        self.assertNotIn(EXPORT, made["profile"].read_text())  # the rest of the plan ran

    def test_a_hashless_earlier_format_authorizes_nothing_outside_the_release(self) -> None:
        made = self.installed(receipt_format=1)
        plan = uninstall.build_plan(self.runtime)
        self.assertEqual([entry for entry in plan.program if entry.note == "launcher"], [])
        self.assertEqual(plan.path_edits, [])
        self.assertTrue(any("format 1 is not format 2" in note and "left as they are" in note
                            for note in plan.kept_program), plan.kept_program)
        code, out, err = self.uninstall("--yes", text="\n")
        self.assertEqual(code, 0, err)
        self.assertTrue(made["launcher"].exists() and made["proxy"].exists())
        self.assertIn(EXPORT, made["profile"].read_text())
        self.assertFalse(self.install.exists())

    def test_invalid_receipts_authorize_nothing(self) -> None:
        cases = [
            {"format": 3, "launchers": [], "path_lines": []},
            {"format": 2, "launchers": [{"path": "/etc/passwd", "sha256": "0" * 64}], "path_lines": []},
            {"format": 2, "launchers": [{"path": "~/.local/bin/x"}], "path_lines": []},
            {"format": 2, "launchers": [], "path_lines": [{"file": "~/.bashrc", "line": "a\nb"}]},
            # a line without the installer's marker, or a marker-only entry
            {"format": 2, "launchers": [], "path_lines": [{"file": "/home/user/.bashrc", "marker": MARK,
                                                           "line": "export PATH=/x"}]},
            {"format": 2, "launchers": [], "path_lines": [{"file": "/home/user/.bashrc", "line": EXPORT}]},
            {"format": 1, "launchers": [], "path_lines": []},
            {"format": 2, "launchers": [], "path_lines": [], "extra": 1},
            ["not", "an", "object"],
        ]
        for document in cases:
            with self.subTest(document=document), self.assertRaises(uninstall.ReceiptError):
                uninstall.parse_receipt(document, self.runtime.environ)
        self.installed()
        self.file(paths.installer_receipt(self.runtime.environ), b"{broken")
        plan = uninstall.build_plan(self.runtime)
        self.assertEqual([entry for entry in plan.program if entry.note == "launcher"], [])
        self.assertTrue(any("not valid JSON" in note for note in plan.kept_program), plan.kept_program)

    def test_no_receipt(self) -> None:
        self.file(self.install / "versions" / "1.0.0" / "bin" / "claude-multi", b"#!/bin/sh\n", 0o755)
        plan = uninstall.build_plan(self.runtime)
        self.assertIn(texts.UNINSTALL_NO_RECEIPT, plan.kept_program)

    def test_a_path_line_present_twice_is_kept(self) -> None:
        made = self.installed()
        made["profile"].write_text(f"{EXPORT}\n{EXPORT}\n")
        plan = uninstall.build_plan(self.runtime)
        self.assertEqual(plan.path_edits, [])
        self.assertIn(texts.UNINSTALL_PATH_AMBIGUOUS.format(file="~/.bashrc"), plan.kept_program)


class PlanTests(UninstallCase):
    def test_classification(self) -> None:
        made = self.everything()
        plan = uninstall.build_plan(self.runtime)
        where = self.where(plan)
        self.assertEqual(where[made["claude"]], "claude")
        self.assertEqual(where[made["trace"]], "sessions")
        self.assertEqual(where[made["record"]], "sessions")
        self.assertEqual(where[made["kept-record"]], "never")
        self.assertEqual(where[made["profile"]], "setup")
        self.assertEqual(where[made["account"]], "credentials")
        self.assertEqual(where[made["signed-out"]], "never")
        self.assertEqual(where[made["retained"]], "never")
        self.assertNotIn(made["native"], where)
        self.assertEqual(where[self.secret_file], "never")  # a key file outside the product's folders
        self.assertEqual(plan.key_names, ("ACME_API_KEY", "KIMI_CLAUDE_API_KEY"))
        self.assertEqual(plan.accounts, ("Claude account (a@example.com)",))
        lines = "\n".join(uninstall.plan_lines(self.runtime, plan))
        self.assertIn(texts.UNINSTALL_PLAN_HEAD, lines)
        self.assertIn("API keys: ACME_API_KEY, KIMI_CLAUDE_API_KEY", lines)
        self.assertNotIn("cli-test-dummy", lines)
        self.assertIn("~/.claude, ~/.claude.json, your conversations", lines)

    def test_keep_setup(self) -> None:
        made = self.everything()
        plan = uninstall.build_plan(self.runtime, keep_setup=True)
        kept = {entry.path for entry in [*plan.setup, *plan.sessions] if not entry.path.is_relative_to(self.data)}
        self.assertNotIn(made["profile"], kept)
        self.assertNotIn(made["record"], kept)


class RootTests(UninstallCase):
    def test_a_root_that_is_a_link_keeps_what_it_points_to(self) -> None:
        outside = self.root / "documents"
        notes = self.file(outside / "notes.txt", b"mine\n")
        for root in (self.install, self.data / "claude", self.data):
            with self.subTest(root=root.name):
                if root == self.data:
                    shutil.rmtree(self.data, ignore_errors=True)
                self.everything() if root != self.data else None
                if os.path.lexists(root):
                    shutil.rmtree(root)
                root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                root.symlink_to(outside, target_is_directory=True)
                plan = uninstall.build_plan(self.runtime)
                self.assertFalse(any(entry.path.is_relative_to(root) for entry in plan.removable(credentials=True)))
                self.assertIn(texts.UNINSTALL_ROOT_LINK.format(path=paths.display(root, self.runtime.environ)),
                              plan.kept)
                code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
                self.assertEqual(code, 0, out + err)
                self.assertEqual(notes.read_bytes(), b"mine\n")
                self.assertTrue(root.is_symlink())
                self.assertIn(texts.UNINSTALL_ROOT_LINK.format(path=paths.display(root, self.runtime.environ)),
                              out)
                root.unlink()

    def test_a_folder_replaced_by_a_link_after_the_plan_keeps_what_it_points_to(self) -> None:
        made = self.everything()
        outside = self.root / "elsewhere"
        bait = self.file(outside / "t.json", b"not claude-multi's\n")

        def swap(_text, *, input_stream=None):
            shutil.rmtree(made["trace"].parent)
            made["trace"].parent.symlink_to(outside, target_is_directory=True)
            return True

        with mock.patch.object(consent, "confirm", side_effect=swap):
            code, out, err = self.uninstall("--keep-credentials", text="")
        self.assertEqual(code, 1, out + err)
        self.assertEqual(bait.read_bytes(), b"not claude-multi's\n")
        self.assertIn(texts.UNINSTALL_MOVED.format(path=paths.display(made["trace"], self.runtime.environ)), out)
        self.assertFalse(made["claude"].exists())  # everything else in the plan went

    def test_a_release_outside_the_home_folder_is_left_as_it_is(self) -> None:
        share = self.home / ".local" / "share"
        shutil.rmtree(share, ignore_errors=True)
        share.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        share.symlink_to(self.file(self.root / "other-disk" / "share" / ".keep").parent, target_is_directory=True)
        self.installed()
        release = self.install / "versions" / "1.0.0" / "bin" / "claude-multi"
        trace = self.file(self.data / "traces" / "t.json")
        code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(release.exists())
        self.assertFalse(trace.exists())  # the data root itself is a real folder
        self.assertIn(texts.UNINSTALL_RELEASE_OUTSIDE.format(path="~/.local/share/claude-multi/install"), out)
        with mock.patch.object(external, "is_store_path", return_value=True):
            self.assertIn(texts.UNINSTALL_RELEASE_OUTSIDE.format(path="~/.local/share/claude-multi/install"),
                          uninstall.build_plan(self.runtime).kept)


class CredentialTests(UninstallCase):
    def test_a_key_file_the_environment_selects_inside_the_state_root_is_a_credential(self) -> None:
        self.everything()
        keys = self.file(self.state_dir / "my-keys.env", b"ACME_API_KEY=acme-dummy-value\n")
        other_name = self.state_dir / "scopes" / "copy.env"
        other_name.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.link(keys, other_name)  # the same file under another name
        env = {"CLAUDE_MULTI_SECRET_ENV": str(keys)}
        with mock.patch.dict(self.runtime.environ, env):
            plan = uninstall.build_plan(self.runtime)
        where = self.where(plan)
        self.assertEqual((where[keys], where[other_name]), ("credentials", "credentials"))
        code, out, err = self.uninstall("--yes", "--keep-credentials", text="", env=env)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(keys.read_bytes(), b"ACME_API_KEY=acme-dummy-value\n")
        self.assertTrue(other_name.exists())

    def test_the_key_file_the_service_reads_is_a_credential_while_the_environment_selects_another(self) -> None:
        self.everything()
        environ = {**self.runtime.environ, "HOME": str(self.home)}
        pointed = self.file(paths.gateway_config_dir(environ) / "my-keys.env", b"ACME_API_KEY=acme-dummy-value\n")
        secret_store.write_pointer(environ, pointed)
        self.assertEqual(secret_store.service_key_file(environ).path, pointed)
        self.assertEqual(secret_store.key_file_location(environ).path, self.secret_file)
        plan = uninstall.build_plan(self.runtime)
        self.assertEqual(self.where(plan)[pointed], "credentials")
        code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(pointed.read_bytes(), b"ACME_API_KEY=acme-dummy-value\n")
        self.assertTrue(paths.secret_pointer_path(environ).exists())  # kept with the credentials

    def test_an_unreadable_key_file_pointer_refuses_the_plan(self) -> None:
        made = self.everything()
        environ = {**self.runtime.environ, "HOME": str(self.home)}
        pointed = self.file(paths.gateway_config_dir(environ) / "my-keys.env", b"ACME_API_KEY=acme-dummy-value\n")
        self.file(paths.secret_pointer_path(environ), b"{broken", 0o600)
        code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        self.assertEqual(code, 1, out + err)
        self.assertTrue(pointed.exists())
        self.assertTrue(all(path.exists() for path in made.values()))
        self.assertIn("which file holds your API keys is not known, so nothing was removed", err)
        self.assertIn("fix: fix or delete ~/.config/claude-multi/secret-file.json", err)
        with self.assertRaises(uninstall.UninstallRefused):
            uninstall.build_plan(self.runtime)


class RetainedReleaseTests(UninstallCase):
    def test_the_previous_release_is_kept_for_rollback(self) -> None:
        self.installed()
        previous = self.file(self.install / "versions" / "0.9.0" / "bin" / "claude-multi", b"#!/bin/sh\n", 0o755)
        (self.install / "current").symlink_to("versions/1.0.0")
        (self.install / "previous").symlink_to("versions/0.9.0")
        plan = uninstall.build_plan(self.runtime)
        where = self.where(plan)
        self.assertEqual((where[previous], where[self.install / "previous"]), ("never", "never"))
        self.assertEqual(where[self.install / "current"], "program")
        lines = "\n".join(uninstall.plan_lines(self.runtime, plan))
        self.assertIn("previous release  ~/.local/share/claude-multi/install/versions/0.9.0", lines)
        code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(previous.read_bytes(), b"#!/bin/sh\n")
        self.assertTrue((self.install / "previous").is_symlink())
        self.assertFalse(os.path.lexists(self.install / "current"))
        self.assertFalse((self.install / "versions" / "1.0.0").exists())
        self.assertIn(f"~/.local/share/claude-multi/install/versions/0.9.0 ({texts.UNINSTALL_RETAINED})", out)


class LivenessTests(UninstallCase):
    def test_an_ended_record_still_running_somewhere_refuses_unless_forced(self) -> None:
        made = self.everything()
        record = self.ended_record()
        runtime_id = record["runtime_session_id"]
        proc = self.root / "proc" / "4321"
        cases = {
            "background": lambda: sessions.BackgroundLiveness(True, frozenset({runtime_id[:8]})),
            "unknown": lambda: sessions.BackgroundLiveness(False, frozenset(), "the daemon root is unreadable"),
            "process": lambda: sessions.BackgroundLiveness(True, frozenset()),
        }
        for label, liveness in cases.items():
            with self.subTest(label), mock.patch.object(self.runtime, "background_liveness", side_effect=liveness):
                if label == "process":
                    self.file(proc / "cmdline", b"claude\0--resume\0" + runtime_id.encode() + b"\0")
                code, _out, err = self.uninstall("--yes", "--keep-credentials", text="")
                self.assertEqual(code, 1, err)
                self.assertIn("sessions may still be running: ", err)
                self.assertIn(RECORD_ID[:8] if label != "unknown" else "background sessions cannot be checked", err)
                self.assertTrue(self.record_path().exists())
                self.assertTrue(made["claude"].exists())
        with mock.patch.object(self.runtime, "background_liveness",
                               side_effect=lambda: sessions.BackgroundLiveness(True, frozenset({runtime_id[:8]}))):
            code, out, err = self.uninstall("--yes", "--force", "--keep-credentials", text="")
        self.assertEqual(code, 0, out + err)
        self.assertIn(texts.UNINSTALL_FORCED.format(ids=RECORD_ID[:8]), out)
        self.assertFalse(self.record_path().exists())

    def test_an_ended_record_nothing_runs_is_removed(self) -> None:
        self.ended_record()
        code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(self.record_path().exists())


class FenceTests(UninstallCase):
    def test_a_session_resumed_while_the_question_waits_refuses(self) -> None:
        made = self.everything()
        self.ended_record()

        def resume(_text, *, input_stream=None):
            path = self.record_path()
            document = json.loads(path.read_text())
            document["last_event_source"] = None
            state.atomic_write(path, json.dumps(document).encode())
            return True

        with mock.patch.object(consent, "confirm", side_effect=resume):
            code, out, err = self.uninstall("--keep-credentials", text="")
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"sessions may still be running: {RECORD_ID[:8]}", err)
        self.assertTrue(self.record_path().exists())
        self.assertTrue(all(path.exists() for path in made.values()))

    def test_another_writer_of_the_session_state_refuses_and_keeps_its_lock(self) -> None:
        made = self.everything()
        holder = sessions.migration_lock(self.state_dir)
        self.assertTrue(holder.acquire(blocking=False))
        self.addCleanup(holder.release)
        before = os.stat(holder.lock_path)
        with mock.patch.object(uninstall, "LOCK_WAIT", 0.2, create=True):
            code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        self.assertEqual(code, 1, out + err)
        self.assertIn(texts.UNINSTALL_BUSY, err)
        after = os.stat(holder.lock_path)
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        self.assertTrue(all(path.exists() for path in made.values()))

    def test_a_store_held_before_uninstall_keeps_its_data_and_refuses(self) -> None:
        made = {**self.installed(), **self.everything()}
        profiles = paths.config_root(dict(self.runtime.environ)) / "profiles"
        profiles.parent.chmod(0o700)
        holder = state.FileLock(profiles)
        self.assertTrue(holder.acquire(blocking=False))
        self.addCleanup(holder.release)
        before = os.stat(holder.lock_path)
        with mock.patch.object(uninstall, "LOCK_WAIT", 0.2):
            code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        # The busy store's profile, and everything else, is still there.
        self.assertEqual([name for name, path in made.items() if not path.exists()], [])
        self.assertEqual(code, 1, out + err)
        shown = paths.display(holder.lock_path, self.runtime.environ)
        self.assertIn(texts.UNINSTALL_STORE_BUSY.format(path=shown, rest="nothing was removed"), err)
        after = os.stat(holder.lock_path)
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        self.assertTrue(self.record_path().parent.joinpath("abc.json").exists())

    def test_a_profile_writer_starting_after_the_plan_never_loses_what_it_wrote(self) -> None:
        import threading

        from claude_multi import profile

        self.everything()
        store = self.runtime.profiles
        for folder in (store.config_root, store.root):
            folder.chmod(0o700)  # as the store makes them
        mine = dict(store.load("balanced"), name="mine")
        mine.pop("seed", None)
        store.save(mine)  # a profile the plan removes
        state_dir_lock = posix_fs.lock_descriptor
        events = {"blocking": threading.Event(), "acquired": threading.Event()}
        outcome: dict[str, object] = {}

        def lock_descriptor(descriptor, *, shared, blocking):
            # Only the writer thread blocks on a lock: say so, then whether it got it.
            if threading.current_thread().name == "profile-writer" and blocking:
                events["blocking"].set()
                state_dir_lock(descriptor, shared=shared, blocking=blocking)
                events["acquired"].set()
                return
            state_dir_lock(descriptor, shared=shared, blocking=blocking)

        def writer() -> None:
            try:
                store.save(dict(mine, description="saved while uninstall ran"))
                outcome["saved"] = True
            except (profile.ProfileError, OSError, state.StateError) as exc:
                outcome["refused"] = exc

        remove = uninstall.remove_entries
        started: list[threading.Thread] = []

        def remove_entries(entries, result, environ, **kwargs):
            if not started:
                # The plan was made and confirmed; a profile save starts now.
                thread = threading.Thread(target=writer, name="profile-writer", daemon=True)
                started.append(thread)
                thread.start()
                self.assertTrue(events["blocking"].wait(5))
                # Let it finish if nothing holds the profile store.
                events["acquired"].wait(0.5)
                if events["acquired"].is_set():
                    thread.join(5)
            return remove(entries, result, environ, **kwargs)

        with mock.patch.object(posix_fs, "lock_descriptor", side_effect=lock_descriptor), \
                mock.patch.object(uninstall, "remove_entries", side_effect=remove_entries):
            code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
            started[0].join(5)
        self.assertFalse(started[0].is_alive())
        self.assertIn(code, (0, 1), out + err)
        if outcome.get("saved"):
            # A save that went through is never deleted afterwards.
            self.assertTrue(store.contains("mine"))
            self.assertEqual(store.load("mine")["description"], "saved while uninstall ran")
        else:
            # The save waited for uninstall to finish with the profile store.
            self.assertIn("refused", outcome)
            self.assertTrue(events["blocking"].is_set())

    def test_a_key_file_where_the_state_lock_lives_is_kept_with_the_credentials(self) -> None:
        self.everything()
        lock_path = self.state_dir / "migration.lock"
        keys = self.file(lock_path, b"ACME_API_KEY=acme-dummy-value\n")
        info = keys.stat()
        env = {"CLAUDE_MULTI_SECRET_ENV": str(keys)}
        with mock.patch.dict(self.runtime.environ, env):
            plan = uninstall.build_plan(self.runtime)
        self.assertEqual(self.where(plan)[keys], "credentials")
        code, out, err = self.uninstall("--yes", "--keep-credentials", text="", env=env)
        self.assertTrue(os.path.lexists(keys))
        self.assertEqual(code, 0, out + err)
        now = keys.stat()  # the same file, by its metadata
        self.assertEqual((now.st_ino, now.st_size), (info.st_ino, info.st_size))
        shown = paths.display(keys, self.runtime.environ)
        self.assertIn(texts.UNINSTALL_KEPT_HELD.format(path=shown, what="one of your credentials"), out)
        self.assertIn("your credentials:", out)

    def test_the_session_state_lock_goes_last(self) -> None:
        self.everything()
        code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(os.path.lexists(self.state_dir / "migration.lock"))
        left = sorted(str(path.relative_to(self.state_dir)) for path in self.state_dir.rglob("*"))
        self.assertEqual(left, ["sessions", "sessions/old.v3.json"])  # only the kept backup


class GatewayHoldTests(UninstallCase):
    def test_an_unconfirmed_save_of_a_stopped_gateway_refuses(self) -> None:
        import test_gateway_lifecycle as lifecycle_tests

        made = self.everything()
        gateway = self.live_gateway()
        self.world.now = lifecycle_tests.NOW
        descriptor, log = file_log.create_instance_log(gateway.logs_dir, lifecycle_tests.NOW, "4444444444444444")
        os.write(descriptor, (lifecycle_tests.save_line("failed") + "\n").encode())
        os.close(descriptor)
        self.assertEqual(gateway.observe().state, gl.STOPPED)
        self.assertTrue(gateway.persistence_hold().held)
        outcome, detail = external.stop_gateway_for_uninstall(self.runtime)
        self.assertEqual(outcome, "hold")
        self.assertIn("a credential save failed", detail)
        code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        self.assertEqual(code, 1, out + err)
        self.assertIn(texts.UNINSTALL_HOLD, err)
        self.assertTrue(log.exists())
        self.assertTrue(all(path.exists() for path in made.values()))
        with mock.patch.object(gl.Gateway, "persistence_hold", side_effect=OSError(5, "Input/output error")):
            outcome, detail = external.stop_gateway_for_uninstall(self.runtime)
        self.assertEqual(outcome, "hold")
        self.assertIn("the hold cannot be evaluated", detail)

    def test_a_stopped_gateway_without_a_hold_is_held_stopped_while_files_go(self) -> None:
        made = self.everything()
        self.live_gateway()
        seen: list[bool] = []
        remove = uninstall.remove_entries

        def watch(entries, result, environ, **kwargs):
            # Another open file description cannot take either lock meanwhile.
            seen.append(posix_fs.lock_held(service.start_lock_path(self.state_dir)) is True
                        and posix_fs.lock_held(self.state_dir / "migration.lock") is True)
            return remove(entries, result, environ, **kwargs)

        with mock.patch.object(uninstall, "remove_entries", side_effect=watch):
            code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(seen and all(seen))
        self.assertFalse(made["claude"].exists())
        self.assertFalse(service.gateway_workdir(self.state_dir).exists())  # its locks went last


class InhibitionTests(UninstallCase):
    """A transaction holding the gateway inhibition (an installer, an
    update, a machine move, a service hand-off) refuses uninstall with its
    owner and remedy, before anything is removed."""

    def begin(self, owner: str = "installer") -> str:
        from claude_multi import gateway_inhibition as gi

        return gi.begin(self.state_dir, owner=owner, purpose="update 1.0.0 to 1.0.1", phase="replace",
                        remedy="sh install.sh --repair", expiry=gi.owner_process(os.getpid()))

    def test_a_recorded_inhibition_refuses_before_the_question(self) -> None:
        program = self.installed()
        made = {**program, **self.everything()}
        self.begin()
        asked: list[str] = []
        with mock.patch.object(consent, "confirm", side_effect=lambda text, **_k: asked.append(text) or True):
            code, out, err = self.uninstall("--keep-credentials", text="")
        self.assertEqual(code, 1, out + err)
        self.assertEqual(asked, [])
        self.assertIn("claude-multi uninstall: the gateway is inhibited by installer (update 1.0.0 to 1.0.1", err)
        self.assertIn("nothing was removed", err)
        self.assertIn("  fix: wait until it has finished; if it was interrupted, its owner finishes or releases it: "
                      "sh install.sh --repair", err)
        self.assertTrue(all(path.exists() for path in made.values()))
        self.assertIn(EXPORT, program["profile"].read_text())
        # The plan alone still shows (it removes nothing).
        code, out, err = self.uninstall("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn(texts.UNINSTALL_PLAN_HEAD, out)

    def test_an_inhibition_recorded_while_the_question_waits_refuses_under_the_lock(self) -> None:
        made = {**self.installed(), **self.everything()}
        self.live_gateway()

        def begin_meanwhile(_text, *, input_stream=None):
            self.begin("cutover")
            return True

        with mock.patch.object(consent, "confirm", side_effect=begin_meanwhile):
            code, out, err = self.uninstall("--keep-credentials", text="")
        self.assertEqual(code, 1, out + err)
        self.assertIn("the gateway is inhibited by cutover", err)
        self.assertTrue(all(path.exists() for path in made.values()))

    def test_the_lifecycle_refusals_carry_the_inhibition(self) -> None:
        self.everything()
        self.live_gateway()
        self.begin("updater")
        outcome, detail = external.stop_gateway_for_uninstall(self.runtime)
        self.assertEqual(outcome, "inhibited")
        self.assertIn("the gateway is inhibited by updater", detail)
        self.assertIn("\n  fix: wait until it has finished", detail)
        lock, problem = external.hold_gateway_starts(self.runtime, what="nothing else was removed")
        self.assertIsNone(lock)
        self.assertIsInstance(problem, external.InhibitedText)
        self.assertIn("; nothing else was removed", problem)

    def test_an_unreadable_continuity_file_refuses(self) -> None:
        made = self.everything()
        self.file(self.home / ".config" / "claude-multi" / "continuity.json", b"{broken")
        code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        self.assertEqual(code, 1, out + err)
        self.assertIn("continuity.json", err)
        self.assertTrue(all(path.exists() for path in made.values()))


def _read_denied(path: Path, rules: tuple[str, ...], home: Path) -> bool:
    """A Read rule of the client's path syntax covers ``path`` (the folder
    of a ``/**`` rule itself included)."""

    for rule in rules:
        if not rule.startswith("Read(") or not rule.endswith(")"):
            continue
        spec = rule[len("Read("):-1]
        base = home / spec[2:] if spec.startswith("~/") else Path(spec[1:]) if spec.startswith("//") else None
        if base is None:
            continue
        if str(base).endswith("/**"):
            folder = Path(str(base)[:-3])
            if path == folder or folder in path.parents:
                return True
        elif base == path:
            return True
    return False


class CredentialInventoryTests(UninstallCase):
    """One credential inventory (``secret_store.credential_locations`` and
    ``key_file_inventory``) drives the compiled denies, the places an export
    never writes to and uninstall: for every way the key file is selected,
    the three agree."""

    def key(self, path: Path) -> Path:
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, b"A_KEY=dummy-value\n")
        return path

    def test_denies_export_and_uninstall_agree(self) -> None:
        from claude_multi import portability, proxy, scope

        home = self.home
        environ = self.runtime.environ
        outside = Path(environ[secret_store.SECRET_ENV_OVERRIDE])
        cases = {
            "the environment's key file outside HOME": ({}, None),
            "the default key file": ({secret_store.SECRET_ENV_OVERRIDE: None}, None),
            "a pointer under ~/.config/secrets": ({secret_store.SECRET_ENV_OVERRIDE: None},
                                                  home / ".config" / "secrets" / "keys.env"),
            "a pointer under the gateway folder": ({secret_store.SECRET_ENV_OVERRIDE: None},
                                                   home / ".config" / "claude-multi" / "secrets" / "other.env"),
            "the environment's key file inside HOME": ({secret_store.SECRET_ENV_OVERRIDE: str(home / "keys.env")},
                                                       None),
        }
        account = self.file(home / secret_store.ACCOUNT_RECORDS / "claude-a@example.com.json", b"{}")
        self.assertEqual(proxy.gateway_auth_dir(self.runtime.catalog.docs["gateway"]["gateway"], {"HOME": str(home)}),
                         home / secret_store.ACCOUNT_RECORDS)
        for label, (changes, pointed) in cases.items():
            with self.subTest(case=label):
                saved = dict(environ)
                for name, value in changes.items():
                    if value is None:
                        environ.pop(name, None)
                    else:
                        environ[name] = value
                self.addCleanup(lambda saved=saved: (environ.clear(), environ.update(saved)))
                env = {**environ, "HOME": str(home)}
                pointer = paths.secret_pointer_path(env)
                pointer.unlink(missing_ok=True)
                if pointed is not None:
                    secret_store.write_pointer(env, self.key(pointed))
                key_files = secret_store.key_file_inventory(env)
                for path in key_files:
                    self.key(path)
                locations = secret_store.credential_locations(env)
                for location in locations:
                    if location not in key_files:
                        state.ensure_private_dir(location)
                denies = scope.secret_path_denies(env)
                roots = portability.forbidden_roots(environ, home)
                for path in (*locations, *key_files, account):
                    for candidate in dict.fromkeys((path, Path(os.path.realpath(path)))):
                        self.assertTrue(_read_denied(candidate, denies, home), (candidate, denies))
                    target = path / "export.json" if path.is_dir() else path
                    with self.assertRaisesRegex(portability.PortabilityError, "refusing a path under"):
                        portability.check_output_path(target, roots)
                where = self.where(uninstall.build_plan(self.runtime))
                for path in key_files:
                    shown = Path(os.path.normpath(os.path.abspath(path)))
                    self.assertIn(where.get(shown), ("credentials", "never"), (path, where))
                self.assertEqual(where.get(account), "credentials")
                # Only the environment's key file can lie outside the folders the rules cover.
                self.assertEqual({path for path in locations if path not in (home / relative for relative in
                                  (*secret_store.PROTECTED_DIRS, secret_store.ACCOUNT_RECORDS))},
                                 set(secret_store.unprotected_key_files(env)))
                if label.endswith("outside HOME"):
                    self.assertIn(outside, locations)
                pointer.unlink(missing_ok=True)


class ServiceStateTests(UninstallCase):
    def test_a_recorded_service_with_a_replaced_unit_refuses_before_any_file_goes(self) -> None:
        made = self.everything()
        gateway_state = self.home / ".local" / "state" / "claude-multi"
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=18329, backend=endpoint.SYSTEMD,
                                                                 unit="claude-multi-gateway"))
        self.live_gateway(state_root=gateway_state)
        unit = self.file(self.unit_dir / "claude-multi-gateway.service", b"[Unit]\nDescription=mine\n", 0o644)
        status = self.runtime.gateway_service().status(live=False)
        self.assertEqual((status.recorded, status.foreign, status.installed), (True, True, False))
        code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        self.assertEqual(code, 1, out + err)
        self.assertTrue(unit.exists())
        self.assertTrue(all(path.exists() for path in made.values()))
        self.assertIn("was not written by claude-multi", err)
        self.assertEqual(external.service_state(self.runtime)[0], "recorded")

    def test_a_service_that_cannot_be_checked_or_is_not_recorded_refuses(self) -> None:
        made = self.everything()
        self.live_gateway(state_root=self.home / ".local" / "state" / "claude-multi")
        with mock.patch.object(gs.GatewayService, "status", side_effect=OSError(13, "Permission denied")):
            code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
            self.assertEqual(code, 1, out + err)
            self.assertTrue(all(path.exists() for path in made.values()))
            self.assertIn("the gateway service cannot be checked", err)
            self.assertEqual(external.service_state(self.runtime)[0], "unknown")
        self.file(self.unit_dir / "claude-multi-gateway.service",
                  f"{systemd_unit.HEADER}\n[Unit]\nDescription=claude-multi gateway\n".encode(), 0o644)
        code, out, err = self.uninstall("--yes", "--keep-credentials", text="")
        self.assertEqual(code, 1, out + err)
        self.assertTrue(all(path.exists() for path in made.values()))
        self.assertIn("is present but not recorded", err)
        self.assertEqual(self.world.terminated, [])
        self.assertEqual(external.service_state(self.runtime)[0], "stray")


class CommandTests(UninstallCase):
    def test_dry_run_removes_nothing(self) -> None:
        made = {**self.installed(), **self.everything()}
        code, out, err = self.uninstall("--dry-run", tty=False)
        self.assertEqual(code, 0, err)
        self.assertIn(texts.UNINSTALL_PLAN_HEAD, out)
        self.assertTrue(all(path.exists() for path in made.values()))

    def test_guards(self) -> None:
        made = self.everything()
        code, _out, err = self.uninstall("--yes", tty=False)
        self.assertEqual(code, 1)
        self.assertIn("not a terminal", err)
        code, _out, err = self.uninstall("--dry-run", env={"CLAUDECODE": "1"})
        self.assertEqual(code, 1)
        code, out, _err = self.uninstall(text="n\n")
        self.assertEqual(code, 3)
        self.assertIn("Nothing was removed.", out)
        self.assertTrue(all(path.exists() for path in made.values()))

    def test_live_sessions_refuse_unless_forced(self) -> None:
        made = self.everything()
        record = self.ended_record()
        record["last_event_source"] = None
        state.atomic_write(self.record_path(), json.dumps(record).encode())
        code, _out, err = self.uninstall("--yes", text="\n")
        self.assertEqual(code, 1)
        self.assertIn(f"sessions may still be running: {RECORD_ID[:8]}", err)
        self.assertTrue(made["record"].exists())
        code, out, err = self.uninstall("--yes", "--force", text="\n")
        self.assertEqual(code, 0, err)
        self.assertIn(f"Removing although these sessions may still be running: {RECORD_ID[:8]}.", out)

    def test_a_gateway_that_cannot_be_stopped_safely_refuses(self) -> None:
        made = self.everything()
        inhibited = "the gateway is inhibited by installer (update); nothing was removed\n  fix: sh install.sh --repair"
        for outcome, detail, text in (("hold", "8317", texts.UNINSTALL_HOLD),
                                      ("inhibited", inhibited, inhibited),
                                      ("not-ours", "8317", "used by a program claude-multi did not start")):
            with self.subTest(outcome=outcome), \
                    mock.patch.object(external, "stop_gateway_for_uninstall", return_value=(outcome, detail)):
                code, _out, err = self.uninstall("--yes", text="\n")
                self.assertEqual(code, 1)
                self.assertIn(text, err)
                self.assertTrue(all(path.exists() for path in made.values()))

    def test_full_removal_keeps_credentials_unless_typed(self) -> None:
        made = {**self.installed(), **self.everything()}
        code, out, err = self.uninstall(text="y\nyes\n")
        self.assertEqual(code, 0, err)
        for name in ("launcher", "proxy", "claude", "trace", "record", "profile"):
            self.assertFalse(made[name].exists(), name)
        self.assertFalse(made["profile"].parent.exists())  # an emptied folder goes too
        self.assertEqual(self.home.joinpath(".bashrc").read_text(), "alias ll='ls -l'\n\n# mine\n")
        self.assertFalse(self.install.exists())
        for name in ("kept-record", "signed-out", "retained", "native", "account"):
            self.assertTrue(made[name].exists(), name)
        self.assertTrue(self.secret_file.exists())
        self.assertTrue((self.home / ".config/claude-multi/api-key").exists())
        self.assertIn(texts.UNINSTALL_DONE, out)
        self.assertIn(texts.UNINSTALL_REMAINS, out)
        self.assertIn("your credentials:", out)

    def test_the_typed_phrase_removes_credentials_and_names_keys_to_revoke(self) -> None:
        made = self.everything()
        code, out, err = self.uninstall("--yes", text="delete credentials\n")
        self.assertEqual(code, 0, err)
        self.assertFalse(made["account"].exists())
        self.assertFalse((self.home / ".config/claude-multi/api-key").exists())
        self.assertTrue(made["signed-out"].exists())
        self.assertTrue(self.secret_file.exists())  # outside the product's folders: never removed
        self.assertIn("also revoke these keys at their providers: ACME_API_KEY, KIMI_CLAUDE_API_KEY", out)
        self.assertNotIn("cli-test-dummy", out + err)

    def test_keep_setup_keeps_profiles_and_records(self) -> None:
        made = self.everything()
        code, out, err = self.uninstall("--yes", "--keep-setup", text="\n")
        self.assertEqual(code, 0, err)
        self.assertTrue(made["profile"].exists())
        self.assertTrue(made["record"].exists())
        self.assertFalse(made["claude"].exists())

    def test_a_nix_install_keeps_its_program_and_service_link(self) -> None:
        link = self.file(self.data / "nix" / "gateway-service", b"link")
        with mock.patch.object(external, "channel", return_value="nix"):
            plan = uninstall.build_plan(self.runtime)
            self.assertIn(texts.UNINSTALL_NIX, plan.kept_program)
            self.assertEqual(plan.program, [])
            self.assertIn(link, {entry.path for entry in plan.never})
            code, _out, err = self.uninstall("--yes", text="\n")
        self.assertEqual(code, 0, err)
        self.assertTrue(link.exists())

    def test_keep_setup_keeps_the_helper_shims(self) -> None:
        from claude_multi import scope

        shim = self.file(scope.hook_shim_path(self.state_dir), b"#!/bin/sh\n", 0o700)
        code, _out, err = self.uninstall("--yes", "--keep-setup", text="\n")
        self.assertEqual(code, 0, err)
        self.assertTrue(shim.exists())

    def test_a_cancel_lists_what_was_removed(self) -> None:
        self.everything()
        with mock.patch.object(uninstall, "remove_entries", side_effect=KeyboardInterrupt):
            code, out, _err = self.uninstall("--yes", text="\n")
        self.assertEqual(code, 130)
        self.assertIn(texts.UNINSTALL_CANCELLED, out)

    def test_a_file_that_cannot_be_removed_is_reported(self) -> None:
        made = self.everything()
        locked = made["claude"].parent
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        code, out, err = self.uninstall("--yes", text="\n")
        self.assertEqual(code, 1, out + err)
        self.assertIn(texts.UNINSTALL_PARTIAL, out)
        self.assertTrue(made["claude"].exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
