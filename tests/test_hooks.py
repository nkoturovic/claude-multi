"""The protocol-3 hook handlers (``hooks.py``) and their dispatch.

Every test runs on a temp state root holding the managed golden v2 scope
(``bless.v2_scope_plan("managed")``, fixture seed ``balanced``); ids and
selectors come from that compiled scope, never from the shipped catalog.
Hooks reach ``cli.main`` with ``Runtime`` patched (it must never be built for
the four scope-only events), a socket-connect tripwire and an ``open`` guard
on ``transcript_path``.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from claude_multi import (
    cli,
    hooks,
    lineup_files,
    lineup_log,
    scope,
    sessions,
    state,
    strict_json,
)
from _layout import REPO_ROOT
from _golden import assertGolden

import bless

MID = bless.FIXED_SESSION
RID = "22222222-2222-4222-8222-222222222222"
RID2 = "33333333-3333-4333-8333-333333333333"
OTHER_MID = "44444444-4444-4444-8444-444444444444"

_PLAN: scope.ScopePlan | None = None


def _plan() -> scope.ScopePlan:
    global _PLAN
    if _PLAN is None:
        _PLAN = bless.v2_scope_plan("managed")
    return _PLAN


def _lead_set() -> dict[str, Any]:
    return strict_json.loads(_plan().other_files[scope.LEAD_SET_JSON])


def _argv(event: str, managed_id: str = MID, *extra: str) -> list[str]:
    return [
        "session-event", event, "--managed-id", managed_id,
        "--launch-epoch", "0", "--hook-protocol", "3", *extra,
    ]


class _HookCase(unittest.TestCase):
    """Temp state root with the managed golden scope; hermetic cli.main."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cm-hooks-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = self.root / "state" / "claude-multi"
        state.ensure_private_dir(self.state)
        self.home = self.root / "home"
        self.home.mkdir()
        self.scope = scope.write_scope(self.state, MID, _plan())
        self.transcript = self.root / "transcript.jsonl"
        self.transcript.write_text("never read\n")
        self.environ = {
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }
        # No network, ever (the live gateway ports least of all).
        connect = mock.patch.object(
            socket.socket, "connect", side_effect=AssertionError("hook opened a socket")
        )
        connect.start()
        self.addCleanup(connect.stop)

    # -- helpers ----------------------------------------------------------

    def payload(self, **fields: Any) -> str:
        document = {"session_id": RID, "transcript_path": str(self.transcript), **fields}
        return json.dumps(document, separators=(",", ":"))

    def main(self, argv: list[str], text: str = "") -> tuple[int, str, str, mock.Mock]:
        """``cli.main`` without an injected Runtime (the production hook path)."""

        output, error = io.StringIO(), io.StringIO()
        transcript = self.transcript
        real_open = open
        real_os_open = os.open

        def guarded_open(file, *args, **kwargs):
            if Path(str(file)) == transcript:
                raise AssertionError("transcript_path was opened")
            return real_open(file, *args, **kwargs)

        def guarded_os_open(path, *args, **kwargs):
            if Path(str(path)) == transcript:
                raise AssertionError("transcript_path was opened")
            return real_os_open(path, *args, **kwargs)

        with mock.patch.dict(os.environ, self.environ, clear=True), mock.patch('claude_multi.cli.runtime.Runtime'
        ) as runtime, mock.patch.object(sys, "stderr", error), mock.patch(
            "builtins.open", side_effect=guarded_open
        ), mock.patch("os.open", side_effect=guarded_os_open):
            code = cli.main(argv, input_stream=io.StringIO(text), output_stream=output)
        return code, output.getvalue(), error.getvalue(), runtime

    def run_hook(self, event: str, text: str, managed_id: str = MID):
        code, out, err, runtime = self.main(_argv(event, managed_id), text)
        self.assertEqual(code, 0, err)
        runtime.assert_not_called()
        return out, err

    def seen(self, rid: str = RID) -> Path:
        return hooks.seen_path(self.state, rid)

    def rewrite_scope(self, *, generation: int = 1, lineup_md: bytes | None = None) -> None:
        plan = _plan()
        other = dict(plan.other_files)
        md = lineup_md if lineup_md is not None else other[scope.LINEUP_MD]
        other[scope.LINEUP_MD] = md
        other[scope.LINEUP_GEN] = scope.lineup_gen_line(generation, md).encode("ascii")
        scope.write_scope(
            self.state, MID, scope.ScopePlan(plan.agent_files, plan.settings, other_files=other)
        )

    def error_log(self) -> list[dict[str, Any]]:
        path = self.state / hooks.HOOK_ERRORS_LOG
        if not path.exists():
            return []
        return [strict_json.loads(line) for line in path.read_bytes().splitlines()]


class NoticeGoldenTests(_HookCase):
    def test_notice_goldens_byte_exact(self) -> None:
        for name, data in bless.notice_files().items():
            with self.subTest(name=name):
                assertGolden(self, bless.NOTICE_GOLDENS / name, data)

    def test_read_notice_from_disk_equals_golden(self) -> None:
        golden = bless.notice_files()
        for name, (event, source) in bless.NOTICE_CASES.items():
            with self.subTest(name=name):
                text, gen_line = hooks.read_notice(self.state, MID, event=event, source=source)
                self.assertEqual(
                    hooks.notice_response(event, text).encode("utf-8"), golden[f"{name}.json"]
                )
                self.assertEqual(
                    gen_line + "\n", _plan().other_files[scope.LINEUP_GEN].decode("ascii")
                )

    def test_notice_shape_and_staged_wording(self) -> None:
        md = _plan().other_files[scope.LINEUP_MD].decode("utf-8")
        gen = _plan().other_files[scope.LINEUP_GEN].decode("ascii").strip()
        staged = "Lineup gen 1 staged — active after /reload-plugins in this session."
        for event, source, expect_staged in (
            ("UserPromptSubmit", None, True),
            ("SessionStart", "compact", True),
            ("SessionStart", "clear", True),
            ("SessionStart", "fork", True),
            ("SessionStart", "mystery", True),
            ("SessionStart", None, True),
            ("SessionStart", "startup", False),
            ("SessionStart", "resume", False),
        ):
            with self.subTest(event=event, source=source):
                text = hooks.notice_text(md, gen, event=event, source=source, lead=None)
                self.assertTrue(
                    text.startswith("[claude-multi lineup notice · lineup_generation 1]\n")
                )
                self.assertIn(md.rstrip("\n"), text)
                # Stated once and repeated once, in-process only.
                self.assertEqual(text.count(staged), 2 if expect_staged else 0)
                self.assertLess(len(text.encode("utf-8")), lineup_files.LINEUP_MD_MAX_BYTES + 600)

    def test_resume_restates_the_recorded_lead(self) -> None:
        lead = _lead_set()["lead"]
        text, _gen = hooks.read_notice(self.state, MID, event="SessionStart", source="resume")
        self.assertIn(f"Your recorded lead is {lead['display']} · {lead['effort']}", text)
        self.assertIn(f"(`{lead['selector']}`)", text)
        # An explicit lead (the record's applied.lead) wins.
        other = {"display": "Other Lead", "effort": "high", "selector": "other-sel"}
        text, _gen = hooks.read_notice(
            self.state, MID, event="SessionStart", source="resume", lead=other
        )
        self.assertIn("Your recorded lead is Other Lead · high (`other-sel`)", text)
        # An unreadable lead-set.json falls back to the no-lead preface.
        (self.scope / scope.LEAD_SET_JSON).unlink()
        text, _gen = hooks.read_notice(self.state, MID, event="SessionStart", source="resume")
        self.assertIn("Session resumed. The lineup below is current", text)


class LayeringTests(unittest.TestCase):
    def test_hooks_import_no_compiler_chain(self) -> None:
        # Never cli, compiler, catalog, profile,
        # scope, launch or transition.
        code = (
            "import sys, claude_multi.hooks\n"
            "print(sorted(m for m in sys.modules if m.startswith('claude_multi.')))\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(
            strict_json.loads(out.strip().replace("'", '"')),
            [
                "claude_multi.assets",
                # The pin check (which Claude Code runs the session): leaves
                # that read one contract file, never the catalog.
                "claude_multi.client_check",
                "claude_multi.errors",
                "claude_multi.hooks",
                "claude_multi.lineup_files",
                "claude_multi.lineup_log",
                "claude_multi.paths",
                "claude_multi.pin",
                # Reviewed leaves: the empty package, dependency-free
                # result types and the POSIX primitives under state/sessions.
                # Never the service or process backends (lazy in sessions).
                "claude_multi.platform",
                "claude_multi.platform.observation",
                "claude_multi.platform.posix_fs",
                "claude_multi.sessions",
                "claude_multi.state",
                "claude_multi.strict_json",
                "claude_multi.validate",
            ],
        )

    def test_nonblocking_argv(self) -> None:
        self.assertTrue(hooks.is_nonblocking_hook_argv(_argv("start")))
        self.assertTrue(hooks.is_nonblocking_hook_argv(["session-event", "prompt"]))
        self.assertTrue(hooks.is_nonblocking_hook_argv(["session-event", "bogus"]))
        self.assertFalse(hooks.is_nonblocking_hook_argv(["session-event", "start"]))
        self.assertFalse(hooks.is_nonblocking_hook_argv(["session-event", "end", "--x"]))
        self.assertFalse(hooks.is_nonblocking_hook_argv(["doctor", "--hook-protocol"]))

    def test_cli_start_sources_are_the_documented_five(self) -> None:
        self.assertEqual(
            cli._SESSION_START_SOURCES, {"startup", "resume", "compact", "clear", "fork"}
        )


class DispatchNeverFailsTests(_HookCase):
    RAISED = (
        RuntimeError("boom"),
        KeyError("k"),
        OSError("io"),
        state.StateError(5, "state"),
        cli.CLIError("cli"),
        strict_json.StrictJSONError("json"),
        SystemExit(2),
    )

    def test_forced_exception_is_a_visible_system_message(self) -> None:
        # systemMessage + exit 0; premodel denies; one stderr
        # line; one metadata line in <state>/hook-errors.log (no payload).
        for event in sorted(hooks.SCOPE_ONLY_EVENTS):
            for exc in self.RAISED:
                with self.subTest(event=event, exc=type(exc).__name__):
                    before = len(self.error_log())
                    with mock.patch.dict(
                        hooks._HANDLERS, {event: mock.Mock(side_effect=exc)}
                    ):
                        out, err = self.run_hook(event, self.payload(prompt="SECRET-PAYLOAD"))
                    document = strict_json.loads(out)
                    message = (
                        f"claude-multi {event} failed: {type(exc).__name__} — run claude-multi doctor"
                    )
                    self.assertEqual(document["systemMessage"], message)
                    if event == "premodel":
                        self.assertEqual(
                            document["hookSpecificOutput"]["permissionDecision"], "deny"
                        )
                        self.assertEqual(
                            document["hookSpecificOutput"]["hookEventName"], "PreModelSwitch"
                        )
                    else:
                        self.assertEqual(set(document), {"systemMessage"})
                    self.assertEqual(out.count("\n"), 1)
                    self.assertEqual(err.count(f"claude-multi: {event} hook ignored:"), 1)
                    lines = self.error_log()
                    self.assertEqual(len(lines), before + 1)
                    self.assertEqual(
                        lines[-1],
                        {
                            "class": type(exc).__name__,
                            "event": event,
                            "managed_id": MID,
                            "time": lines[-1]["time"],
                        },
                    )
        log = self.state / hooks.HOOK_ERRORS_LOG
        self.assertEqual(stat.S_IMODE(os.lstat(log).st_mode), 0o600)
        self.assertNotIn(b"SECRET-PAYLOAD", log.read_bytes())

    def test_hook_error_log_is_bounded(self) -> None:
        with mock.patch.object(hooks, "HOOK_ERRORS_MAX_BYTES", 300):
            for _ in range(10):
                hooks.record_hook_error(
                    self.state, event="prompt", managed_id=MID, exc=RuntimeError()
                )
        self.assertLessEqual((self.state / hooks.HOOK_ERRORS_LOG).stat().st_size, 300)
        self.assertTrue((self.state / "hook-errors.log.1").exists())

    def test_bad_inputs_exit_zero(self) -> None:
        huge = self.payload(prompt="x" * (hooks.PAYLOAD_LIMITS.max_bytes + 10))
        cases = (
            ("malformed JSON", _argv("prompt"), "{not json"),
            ("payload over 4 MiB", _argv("prompt"), huge),
            ("unknown managed id", _argv("prompt", OTHER_MID), self.payload()),
            ("non-UUID managed id", _argv("prompt", "../../etc"), self.payload()),
            ("non-object payload", _argv("subagent"), "[]"),
        )
        for label, argv, text in cases:
            with self.subTest(label):
                code, out, err, runtime = self.main(argv, text)
                self.assertEqual(code, 0)
                runtime.assert_not_called()
                self.assertEqual(set(strict_json.loads(out)), {"systemMessage"})
                self.assertIn("hook ignored", err)
        self.assertFalse(self.seen().exists())

    def test_argparse_rejections_exit_zero(self) -> None:
        for argv in (
            ["session-event", "prompt", "--launch-epoch", "0", "--hook-protocol", "3"],
            _argv("prompt", MID, "--unknown-flag"),
            _argv("start", MID, "--unknown-flag"),
            ["session-event", "start", "--managed-id", MID, "--hook-protocol", "4"],
            ["session-event", "brand-new-event", "--managed-id", MID],
        ):
            with self.subTest(argv=argv):
                code, out, err, runtime = self.main(argv, self.payload())
                self.assertEqual(code, 0)
                self.assertEqual(out, "")
                self.assertIn("hook arguments rejected", err)
                runtime.assert_not_called()
        # A rejected premodel argv fails closed.
        code, out, err, runtime = self.main(
            ["session-event", "premodel", "--managed-id", MID, "--hook-protocol", "4"],
            self.payload(),
        )
        self.assertEqual(code, 0)
        runtime.assert_not_called()
        self.assertIn("hook arguments rejected", err)
        document = strict_json.loads(out)
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(
            document["hookSpecificOutput"],
            {
                "hookEventName": "PreModelSwitch",
                "permissionDecision": "deny",
                "permissionDecisionReason": hooks.PREMODEL_FAIL_CLOSED_REASON,
            },
        )
        self.assertEqual(
            document["systemMessage"],
            "claude-multi premodel failed: hook arguments rejected — run claude-multi doctor",
        )
        # A 2.x start/end keeps argparse's exit 2 (parity).
        with mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.main(["session-event", "start", "--managed-id", MID, "--bogus"])
        self.assertEqual(caught.exception.code, 2)

    def test_newer_marker_reads_nothing(self) -> None:
        marker = self.state / sessions.STATE_MARKER
        marker.write_text("5\n")
        marker.chmod(0o600)
        for event in sorted(hooks.SCOPE_ONLY_EVENTS) + ["start", "end"]:
            with self.subTest(event=event):
                inp = mock.Mock()
                output, error = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, self.environ, clear=True), mock.patch('claude_multi.cli.runtime.Runtime'
                ) as runtime, mock.patch.object(sys, "stderr", error):
                    code = cli.main(_argv(event), input_stream=inp, output_stream=output)
                self.assertEqual(code, 0)
                runtime.assert_not_called()
                inp.read.assert_not_called()
                if event == "premodel":
                    # A model switch is refused (fail closed), never allowed by silence.
                    document = json.loads(output.getvalue())
                    self.assertEqual(output.getvalue(), hooks.premodel_fail_closed_response(document["systemMessage"]))
                    self.assertTrue(document["systemMessage"].startswith(
                        "claude-multi premodel failed: state belongs to a newer claude-multi"), document)
                else:
                    self.assertEqual(output.getvalue(), "")
                self.assertIn("newer claude-multi", error.getvalue())
        self.assertFalse((self.state / hooks.HOOK_ERRORS_LOG).exists())

    def test_the_entry_point_refuses_a_switch_over_newer_state(self) -> None:
        # Through the command line a hook runs (a fresh process, the module
        # entry): the newer state marker refuses the switch with the fixed
        # deny instead of exiting quietly.
        import subprocess
        import sys

        from _layout import REPO_ROOT

        marker = self.state / sessions.STATE_MARKER
        marker.write_text("5\n")
        marker.chmod(0o600)
        env = {**self.environ, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(REPO_ROOT / "src")}
        result = subprocess.run([sys.executable, "-B", "-m", "claude_multi.cli", *_argv("premodel")], env=env,
                                input=self.payload(model="claude-fable-5-1"), capture_output=True, text=True,
                                timeout=60, cwd=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(result.stdout, hooks.premodel_fail_closed_response(document["systemMessage"]))
        self.assertEqual(document["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(document["systemMessage"].startswith(
            "claude-multi premodel failed: state belongs to a newer claude-multi"), document)
        self.assertIn("newer claude-multi", result.stderr)

    def test_premodel_over_newer_state_refuses_the_switch(self) -> None:
        marker = self.state / sessions.STATE_MARKER
        marker.write_text("5\n")
        marker.chmod(0o600)
        inp = mock.Mock()
        output, error = io.StringIO(), io.StringIO()
        args = mock.Mock(event="premodel", managed_id=MID, launch_epoch=None)
        code = hooks.dispatch(args, state_root=self.state, environ=self.environ, input_stream=inp,
                              output_stream=output, error_stream=error)
        self.assertEqual(code, 0)
        inp.read.assert_not_called()
        # Older code over newer state refuses the switch (fail closed), naming the fix.
        document = json.loads(output.getvalue())
        self.assertEqual(document["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("newer claude-multi", document["systemMessage"])
        self.assertIn("claude-multi update", document["systemMessage"])


class PromptTests(_HookCase):
    def test_first_prompt_notifies_then_fast_path_silence(self) -> None:
        out, _err = self.run_hook("prompt", self.payload(prompt="hi"))
        golden = bless.notice_files()["prompt.json"].decode("utf-8")
        self.assertEqual(out, golden)
        seen = self.seen()
        self.assertEqual(stat.S_IMODE(os.lstat(seen).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(seen.parent).st_mode), 0o700)
        self.assertEqual(seen.read_bytes(), _plan().other_files[scope.LINEUP_GEN])
        out, _err = self.run_hook("prompt", self.payload(prompt="again"))
        self.assertEqual(out, "")

    def test_generation_bump_and_same_generation_change_renotify(self) -> None:
        self.run_hook("prompt", self.payload())
        self.rewrite_scope(generation=2)
        out, _err = self.run_hook("prompt", self.payload())
        self.assertIn("lineup_generation 2", strict_json.loads(out)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.run_hook("prompt", self.payload())[0], "")
        # Same generation, other content (repair / catalog drift): the hash moves.
        md = _plan().other_files[scope.LINEUP_MD] + b"- one more line\n"
        self.rewrite_scope(generation=2, lineup_md=md)
        out, _err = self.run_hook("prompt", self.payload())
        self.assertIn("one more line", out)

    def test_two_runtime_ids_are_notified_once_each(self) -> None:
        for rid in (RID, RID2):
            with self.subTest(rid=rid):
                self.assertNotEqual(self.run_hook("prompt", self.payload(session_id=rid))[0], "")
        for rid in (RID, RID2):
            self.assertEqual(self.run_hook("prompt", self.payload(session_id=rid))[0], "")

    def test_md_gen_mismatch_notifies_without_marker(self) -> None:
        md = self.scope / scope.LINEUP_MD
        state.atomic_write(md, md.read_bytes() + b"tampered\n")
        out, err = self.run_hook("prompt", self.payload())
        self.assertIn("tampered", out)
        self.assertFalse(self.seen().exists())
        self.assertIn("disagree", err)
        self.assertNotEqual(self.run_hook("prompt", self.payload())[0], "")

    def test_large_prompt_accepted(self) -> None:
        out, _err = self.run_hook("prompt", self.payload(prompt="p" * (1024 * 1024)))
        self.assertIn("UserPromptSubmit", out)

    def test_missing_session_id_is_quiet(self) -> None:
        out, err = self.run_hook("prompt", json.dumps({"prompt": "x"}))
        self.assertEqual(out, "")
        self.assertIn("no UUIDv4 session_id", err)
        out, _err = self.run_hook("prompt", self.payload(session_id="../../x"))
        self.assertEqual(out, "")

    def test_missing_gen_is_visible_damage(self) -> None:
        (self.scope / scope.LINEUP_GEN).unlink()
        out, _err = self.run_hook("prompt", self.payload())
        self.assertEqual(
            strict_json.loads(out),
            {"systemMessage": "claude-multi prompt failed: HookInputError — run claude-multi doctor"},
        )


class PremodelTests(_HookCase):
    def decide(self, **fields: Any) -> tuple[str, str]:
        out, _err = self.run_hook("premodel", self.payload(**fields))
        document = strict_json.loads(out)
        spec = document["hookSpecificOutput"]
        # Output shape equals the real-client probe's (tests/test_client_hooks.py).
        self.assertEqual(set(document), {"hookSpecificOutput"})
        self.assertEqual(
            set(spec), {"hookEventName", "permissionDecision", "permissionDecisionReason"}
        )
        self.assertEqual(spec["hookEventName"], "PreModelSwitch")
        return spec["permissionDecision"], spec["permissionDecisionReason"]

    def rows(self) -> list[dict[str, Any]]:
        return _lead_set()["rows"]

    def test_matrix(self) -> None:
        lead = _lead_set()["lead"]
        same = [row for row in self.rows() if row["family"] == lead["family"]]
        other = [row for row in self.rows() if row["family"] != lead["family"]]
        self.assertTrue(same and len({row["family"] for row in other}) >= 2)
        for row in same:
            with self.subTest(same=row["selector"]):
                self.assertEqual(self.decide(to_model=row["selector"])[0], "allow")
        for row in other:
            with self.subTest(other=row["selector"]):
                decision, reason = self.decide(to_model=row["selector"])
                self.assertEqual(decision, "ask")
                self.assertIn("re-reads the whole conversation uncached", reason)
                self.assertIn(f"({lead['family']})", reason)
                self.assertIn(f"({row['family']})", reason)
        members = {row["selector"] for row in self.rows()}
        outside = [s for s in _plan().settings["availableModels"] if s not in members]
        # Fallback-only, agent-only and other-class selectors are all outside.
        self.assertTrue(any(s.endswith("[1m]") for s in outside))
        for selector in [*outside, "no-such-model", "claude-multi-unknown[1m]"]:
            with self.subTest(outside=selector):
                decision, reason = self.decide(to_model=selector)
                self.assertEqual(decision, "deny")
                self.assertIn("not in this session's lead set", reason)

    def test_normalisation_and_ignored_fields(self) -> None:
        row = self.rows()[0]
        base = self.decide(to_model=row["selector"])
        stripped = row["selector"].removesuffix("[1m]")
        for fields in (
            {"to_model": stripped},
            {"to_model": stripped + "[1m]"},
            {"to_model": row["selector"].upper()},
            {"to_model": row["selector"], "requested_model": None},
            {"to_model": row["selector"], "requested_model": "opus"},
            {"to_model": row["selector"], "source": "command"},
            {"to_model": row["selector"], "source": "picker"},
            {"to_model": row["selector"], "source": "sdk"},
            {"to_model": row["selector"], "from_model": "unmatched-model"},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(self.decide(**fields), base)

    def test_from_model_within_a_family(self) -> None:
        other = [r for r in self.rows() if r["family"] != _lead_set()["lead"]["family"]]
        by_family: dict[str, list[dict[str, Any]]] = {}
        for row in other:
            by_family.setdefault(row["family"], []).append(row)
        pair = next(rows for rows in by_family.values() if len(rows) >= 2)
        self.assertEqual(
            self.decide(to_model=pair[1]["selector"], from_model=pair[0]["selector"])[0], "allow"
        )

    def test_missing_target_and_damaged_lead_set_deny(self) -> None:
        decision, reason = self.decide()
        self.assertEqual(decision, "deny")
        self.assertIn("no target model", reason)
        target = self.scope / scope.LEAD_SET_JSON
        for label, damage in (
            ("missing", lambda: target.unlink()),
            ("invalid JSON", lambda: state.atomic_write(target, b"{nope")),
            ("wrong shape", lambda: state.atomic_write(target, b'{"version":1}')),
            ("directory", lambda: (target.unlink(), target.mkdir())),
        ):
            with self.subTest(label):
                if target.is_dir():
                    target.rmdir()
                if not target.exists():
                    state.atomic_write(target, _plan().other_files[scope.LEAD_SET_JSON])
                damage()
                decision, reason = self.decide(to_model=self.rows()[0]["selector"])
                self.assertEqual(decision, "deny")
                self.assertIn(f"claude-multi doctor --repair {MID}", reason)


class PostmodelTests(_HookCase):
    def settings_file(self, doc: Any, *, base: Path | None = None) -> Path:
        path = (base or self.home / ".claude") / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc)
        return path

    def test_sources_and_membership(self) -> None:
        member = _lead_set()["rows"][0]["selector"]
        for source in ("auto", "resume", None, "startup"):
            with self.subTest(source=source):
                self.assertEqual(
                    self.run_hook("postmodel", self.payload(source=source, to_model="x"))[0], ""
                )
        for source in sorted(hooks.USER_SWITCH_SOURCES):
            with self.subTest(source=source):
                self.assertEqual(
                    self.run_hook("postmodel", self.payload(source=source, to_model=member))[0],
                    "",
                )
                out, _err = self.run_hook(
                    "postmodel", self.payload(source=source, to_model="outside-model")
                )
                context = strict_json.loads(out)["hookSpecificOutput"]
                self.assertEqual(context["hookEventName"], "PostModelSwitch")
                self.assertIn("outside its lead set", context["additionalContext"])
                self.assertIn(f"claude-multi -r {MID}", context["additionalContext"])

    def test_saved_user_model_warning_never_writes(self) -> None:
        member = _lead_set()["rows"][0]["selector"]
        path = self.settings_file({"model": member, "theme": "dark"})
        before = (path.read_bytes(), os.stat(path).st_mtime_ns)
        out, _err = self.run_hook("postmodel", self.payload(source="picker", to_model=member))
        context = strict_json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"saved as the default model in {path}", context)
        self.assertIn(f"plain claude now starts on {member}", context)
        self.assertEqual((path.read_bytes(), os.stat(path).st_mtime_ns), before)

    def test_no_saved_warning_for_foreign_or_broken_settings(self) -> None:
        member = _lead_set()["rows"][0]["selector"]
        for doc in ({"model": "sonnet"}, {"model": "claude-some-other"}, "{broken", ["x"], {}):
            with self.subTest(doc=doc):
                self.settings_file(doc)
                self.assertEqual(
                    self.run_hook("postmodel", self.payload(source="command", to_model=member))[0],
                    "",
                )

    def test_claude_config_dir_is_honoured(self) -> None:
        member = _lead_set()["rows"][0]["selector"]
        config = self.root / "claude-config"
        path = self.settings_file({"model": member}, base=config)
        self.environ["CLAUDE_CONFIG_DIR"] = str(config)
        out, _err = self.run_hook("postmodel", self.payload(source="sdk", to_model=member))
        self.assertIn(str(path), out)

    def test_unreadable_lead_set_skips_membership_warning(self) -> None:
        (self.scope / scope.LEAD_SET_JSON).unlink()
        out, err = self.run_hook("postmodel", self.payload(source="command", to_model="outside"))
        self.assertEqual(out, "")
        self.assertIn("cannot read the lead set", err)


class SubagentTests(_HookCase):
    def log_lines(self) -> list[dict[str, Any]]:
        path = lineup_files.lineup_log_path(self.state, MID)
        return [strict_json.loads(line) for line in path.read_bytes().splitlines()]

    def dispatch(self, text: str) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(_argv("subagent"))
        output, error = io.StringIO(), io.StringIO()
        code = hooks.dispatch(
            args,
            state_root=self.state,
            environ=self.environ,
            input_stream=io.StringIO(text),
            output_stream=output,
            error_stream=error,
            clock=lambda: "2026-09-26T00:00:00Z",
        )
        return code, output.getvalue(), error.getvalue()

    def test_one_canonical_line(self) -> None:
        agent_id = "agent-abc123"
        agent = "cm-explorer"
        selector = _plan().agent_files[f".claude/agents/{agent}.md"].decode().split("model: ", 1)[1].split("\n", 1)[0]
        code, out, _err = self.dispatch(
            self.payload(agent_type=agent, agent_id=agent_id, prompt="SECRET")
        )
        self.assertEqual((code, out), (0, ""))
        gen = _plan().other_files[scope.LINEUP_GEN].decode("ascii").strip()
        self.assertEqual(
            self.log_lines(),
            [
                {
                    "agent_id": agent_id,
                    "agent_type": agent,
                    "event": "subagent-start",
                    "label": "binding at gen 1 (reload unconfirmed)",
                    "lineup_gen": gen,
                    "scope_selector": selector,
                    "time": "2026-09-26T00:00:00Z",
                }
            ],
        )
        path = lineup_files.lineup_log_path(self.state, MID)
        self.assertNotIn(b"SECRET", path.read_bytes())
        self.assertNotIn(b"transcript", path.read_bytes())
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)

    def test_selector_null_and_unknown_fields(self) -> None:
        for fields in (
            {"agent_type": "general-purpose", "agent_id": "a1"},
            {"agent_type": "cm-../x", "agent_id": "a2"},
            {"agent_type": "cm-designer", "agent_id": "a3"},  # unbound: no file
            {},
            {"agent_type": 7, "agent_id": "x" * 500},
            {"agent_type": "cm-explorer\x1b[31m", "agent_id": "a4"},
        ):
            with self.subTest(fields=fields):
                path = lineup_files.lineup_log_path(self.state, MID)
                before = path.read_bytes() if path.exists() else b""
                self.assertEqual(self.dispatch(self.payload(**fields))[:2], (0, ""))
                after = path.read_bytes()
                self.assertTrue(after.startswith(before))
                line = self.log_lines()[-1]
                self.assertIsNone(line["scope_selector"])
                if not isinstance(fields.get("agent_type"), str) or not fields.get("agent_type", "").isprintable():
                    self.assertEqual(line["agent_type"], "unknown")
        self.assertEqual(self.log_lines()[-2]["agent_id"], "unknown")

    def test_unsafe_log_is_never_written(self) -> None:
        log_dir = state.ensure_private_dir(self.state / lineup_files.LINEUP_LOG_DIR)
        path = lineup_files.lineup_log_path(self.state, MID)
        target = self.root / "elsewhere"
        target.write_bytes(b"")
        for label, make in (
            ("symlink", lambda: path.symlink_to(target)),
            ("directory", lambda: path.mkdir()),
        ):
            with self.subTest(label):
                make()
                code, out, err, _runtime = self.main(_argv("subagent"), self.payload(agent_type="cm-explorer"))
                self.assertEqual(code, 0)
                self.assertIn("subagent failed", out)
                self.assertEqual(target.read_bytes(), b"")
                if path.is_symlink():
                    path.unlink()
                else:
                    path.rmdir()
        self.assertTrue(log_dir.is_dir())

    def test_concurrent_processes_keep_every_line(self) -> None:
        script = (
            "import os, sys\n"
            "from claude_multi import hooks\n"
            "from claude_multi.cli import parser\n"
            "args = parser.build_parser().parse_args(sys.argv[1:])\n"
            "sys.exit(hooks.dispatch(args, state_root=os.environ['CM_STATE'], environ={},"
            " input_stream=sys.stdin, output_stream=sys.stdout, error_stream=sys.stderr))\n"
        )
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "CM_STATE": str(self.state),
        }
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, *_argv("subagent")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            for _ in range(20)
        ]
        for index, process in enumerate(processes):
            out, err = process.communicate(
                self.payload(agent_type="cm-reviewer", agent_id=f"agent-{index}").encode(), timeout=60
            )
            self.assertEqual((process.returncode, out), (0, b""), err)
        lines = self.log_lines()
        self.assertEqual(sorted(line["agent_id"] for line in lines), sorted(f"agent-{i}" for i in range(20)))


class MigrationLockTests(_HookCase):
    def test_probe_never_creates_and_sees_holders(self) -> None:
        lock_path = self.state / f"{hooks.MIGRATION_LOCK_TARGET}.lock"
        self.assertFalse(hooks.migration_lock_held(self.state))
        self.assertFalse(lock_path.exists())
        lock = state.FileLock(self.state / hooks.MIGRATION_LOCK_TARGET)
        lock.acquire()
        try:
            self.assertTrue(hooks.migration_lock_held(self.state))
        finally:
            lock.release()
        self.assertFalse(hooks.migration_lock_held(self.state))


if __name__ == "__main__":
    unittest.main()
