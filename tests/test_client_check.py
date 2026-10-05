"""Which Claude Code runs a managed session: the hook-side pin check.

Fixture process trees only (a fake ``/proc`` and a fake ``ps``); no real
process is inspected and nothing is executed.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from claude_multi import client_check, hooks, strict_json
from claude_multi.cli import session_events
from _catalog import FIXTURE_ROOT
import test_hooks
from test_hooks import MID, RID

PINNED = strict_json.load(FIXTURE_ROOT / "catalog" / "native-contract.json")["verified"][0]["version"]


class _ProcTree:
    def __init__(self, root: Path) -> None:
        self.root = root

    def add(self, pid: int, ppid: int, argv: list[str], exe: Path | str | None) -> None:
        base = self.root / str(pid)
        base.mkdir(parents=True)
        (base / "cmdline").write_bytes(b"\0".join(item.encode() for item in argv) + b"\0")
        (base / "stat").write_bytes(f"{pid} (x y) S {ppid} 1 1 0".encode())
        if exe is not None:
            (base / "exe").symlink_to(exe)


class SessionClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-client-check-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proc = _ProcTree(self.tmp / "proc")
        versions = self.tmp / "home/.local/share/claude/versions"
        versions.mkdir(parents=True)
        self.newer = versions / "2.1.290"
        self.newer.write_bytes(b"x")
        # The hook chain: client -> shim -> shim subshell (the launcher's parent).
        self.proc.add(100, 1, [str(self.newer), "--resume", RID, "--settings", "s.json"], self.newer)
        shim = ["/bin/sh", "/state/bin/claude-multi-hook-3", "session-event", "prompt", "--managed-id", MID]
        self.proc.add(200, 100, shim, "/usr/bin/bash")
        self.proc.add(300, 200, shim, "/usr/bin/bash")
        self.environ = {"HOME": str(self.tmp / "home"), "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT)}

    def check(self, **kwargs):
        return client_check.check((RID, MID), self.environ, pid=300, proc_root=self.tmp / "proc",
                                  platform="linux", **kwargs)

    def test_the_session_client_is_the_ancestor_naming_the_session(self) -> None:
        exe = client_check.session_client((RID,), pid=300, proc_root=self.tmp / "proc", platform="linux")
        self.assertEqual(exe, self.newer)
        # The shim names the managed id with --managed-id only: never the client.
        self.assertIsNone(client_check.session_client(("unknown",), pid=300, proc_root=self.tmp / "proc",
                                                      platform="linux"))

    def test_a_different_version_is_a_mismatch_with_a_resume_message(self) -> None:
        mismatch = self.check()
        self.assertEqual((mismatch.actual_version, mismatch.pinned), ("2.1.290", PINNED))
        text = client_check.message(mismatch, MID, self.environ)
        self.assertIn("runs Claude Code 2.1.290 (~/.local/share/claude/versions/2.1.290)", text)
        self.assertIn(f"not the {PINNED} claude-multi verified", text)
        self.assertIn(f"claude-multi -r {MID}", text)

    def test_unknown_or_matching_clients_stay_silent(self) -> None:
        self.assertIsNone(client_check.check((RID,), {**self.environ, "CLAUDE_MULTI_ASSETS": str(self.tmp)},
                                             pid=300, proc_root=self.tmp / "proc", platform="linux"))
        shutil.rmtree(self.tmp / "proc/100")
        owned = self.tmp / f"home/.local/share/claude-multi/claude/{PINNED}/claude"
        owned.parent.mkdir(parents=True)
        owned.write_bytes(b"x")
        self.proc.add(100, 1, [str(owned), "--session-id", RID], owned)
        self.assertIsNone(self.check())
        shutil.rmtree(self.tmp / "proc/100")
        self.proc.add(100, 1, ["node", "cli.js", "--resume", RID], "/usr/bin/node")
        self.assertIsNone(self.check())
        self.assertIsNone(client_check.check((RID,), self.environ, pid=300, proc_root=self.tmp / "proc",
                                             platform="win32"))

    def test_a_replaced_executable_keeps_its_version_name(self) -> None:
        shutil.rmtree(self.tmp / "proc/100")
        self.proc.add(100, 1, ["claude", "--resume", RID], f"{self.newer} (deleted)")
        self.assertEqual(self.check().actual, self.newer)

    def test_the_walk_is_bounded_and_never_raises(self) -> None:
        self.proc.add(400, 400, ["loop"], None)  # its own parent
        self.assertIsNone(client_check.session_client((RID,), pid=400, proc_root=self.tmp / "proc",
                                                      platform="linux"))
        with mock.patch.object(client_check, "session_client", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.check())

    def test_macos_uses_ps(self) -> None:
        table = {(300, "args"): "/bin/sh shim session-event prompt --managed-id " + MID, (300, "ppid"): "100",
                 (300, "comm"): "/bin/sh",
                 (100, "args"): f"{self.newer} --resume {RID}", (100, "ppid"): "1", (100, "comm"): str(self.newer)}

        def run(argv, **_kwargs):
            pid, field = int(argv[-1]), argv[3].rstrip("=")
            value = table.get((pid, field))
            return SimpleNamespace(returncode=0 if value else 1, stdout=(value or "") + "\n")

        exe = client_check.session_client((RID,), pid=300, platform="darwin", run=run)
        self.assertEqual(exe, Path(os.path.realpath(self.newer)))


class HookMessageTests(test_hooks._HookCase):
    """The prompt and SessionStart hooks show the mismatch to the user."""

    def mismatch(self):
        return client_check.ClientMismatch(Path(self.home / ".local/share/claude/versions/2.1.290"),
                                           "2.1.290", "2.1.286")

    def test_prompt_warns_and_keeps_checking(self) -> None:
        with mock.patch.object(client_check, "check", return_value=self.mismatch()) as check:
            out, _err = self.run_hook("prompt", self.payload(prompt="hi"))
            again, _err = self.run_hook("prompt", self.payload(prompt="again"))
        document = strict_json.loads(out)
        self.assertIn(f"claude-multi -r {MID}", document["systemMessage"])
        self.assertIn("additionalContext", document["hookSpecificOutput"])
        self.assertFalse(self.seen().exists())
        self.assertIn("systemMessage", again)
        self.assertEqual(check.call_args.args[0], (RID, MID))

    def test_no_mismatch_keeps_the_plain_notice(self) -> None:
        with mock.patch.object(client_check, "check", return_value=None):
            out, _err = self.run_hook("prompt", self.payload(prompt="hi"))
        self.assertNotIn("systemMessage", strict_json.loads(out))
        self.assertTrue(self.seen().exists())

    def start(self, mismatch):
        output = io.StringIO()
        args = SimpleNamespace(managed_id=MID, launch_epoch=0)
        payload = json.dumps({"session_id": RID, "source": "startup"})
        with mock.patch.object(client_check, "check", return_value=mismatch), \
                mock.patch.object(session_events, "_reconcile_session_start", return_value=[]):
            code = session_events._session_start_v3(
                args, state_path=self.state, runtime_factory=mock.Mock(),
                input_stream=io.StringIO(payload), output_stream=output, error_stream=io.StringIO(),
                environ=self.environ)
        self.assertEqual(code, 0)
        return strict_json.loads(output.getvalue())

    def test_session_start_warns_and_writes_no_marker(self) -> None:
        document = self.start(None)
        self.assertNotIn("systemMessage", document)
        self.assertTrue(hooks.seen_path(self.state, RID).exists())
        document = self.start(self.mismatch())
        self.assertIn(f"claude-multi -r {MID}", document["systemMessage"])
        self.assertIn("additionalContext", document["hookSpecificOutput"])
        self.assertFalse(hooks.seen_path(self.state, RID).exists())


if __name__ == "__main__":
    unittest.main()
