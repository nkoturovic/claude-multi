"""The protocol-3 hook shim ``<state>/bin/claude-multi-hook-3``.

Hermetic ``/bin/sh`` subprocesses (plus ``dash`` and ``busybox sh`` when
present) over the exact text ``scope.hook_shim_v3_text`` returns. The shim
runs with an empty environment except ``PATH``, which holds only logging
wrappers: ``cat`` (logs its name, execs the real one) and stubs for
``python``, ``python3`` and ``claude-multi`` that log and exit 97, so a test
can prove exactly which external commands ran. The launcher is a sh stub
that records its argv, its stdin and its invocation count, prints
``$STUB_OUT`` and exits ``$STUB_EXIT`` (or is killed, or emulates a 2.26
argparse rejection).

No network, no real launcher, no live state: every path is under a private
temp directory.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from claude_multi import hooks, scope
from claude_multi.scope import ScopeError
from _catalog import GOLDENS_ROOT
from _golden import assertGolden

SHIM_GOLDENS = GOLDENS_ROOT / "shim"
GOLDEN_COMMAND = "/nix/store/fixture/bin/claude-multi"
GEN = "4242 0123456789ab"

_REAL_CAT = shutil.which("cat")

_STUB_LAUNCHER = """\
#!/bin/sh
n=0
if [ -f "$STUB_DIR/count" ]; then
    IFS= read -r n < "$STUB_DIR/count" || :
fi
n=$((n + 1))
printf '%s\\n' "$n" > "$STUB_DIR/count"
printf '%s\\n' "$@" > "$STUB_DIR/argv"
cat > "$STUB_DIR/stdin"
case "${STUB_MODE-exit}" in
    kill)
        printf '%s\\n' "${STUB_OUT-}"
        kill -9 $$
        ;;
    argparse)
        echo 'usage: claude-multi [-h] {session-event,...} ...' >&2
        echo 'claude-multi: error: unrecognized arguments: --hook-protocol 3' >&2
        exit 2
        ;;
esac
if [ -n "${STUB_OUT-}" ]; then
    printf '%s\\n' "$STUB_OUT"
fi
exit "${STUB_EXIT-0}"
"""


def _shells() -> list[str]:
    shells = ["/bin/sh"]
    dash = shutil.which("dash")
    if dash:
        shells.append(dash)
    return shells


def _busybox() -> str | None:
    return shutil.which("busybox")


def _write_exec(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, 0o700)
    return path


def _uuid4() -> str:
    return str(uuid.uuid4())


def _payload(rid: str, *, prompt: str = "hello", first: bool = True, extra: dict | None = None) -> str:
    """A client-shaped (compact, session_id first) UserPromptSubmit payload."""

    fields = {
        "transcript_path": "/nonexistent/transcript.jsonl",
        "cwd": "/nonexistent/project",
        "permission_mode": "default",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }
    if extra:
        fields.update(extra)
    items = list(fields.items())
    position = 0 if first else len(items)
    items.insert(position, ("session_id", rid))
    return json.dumps(dict(items), separators=(",", ":"), ensure_ascii=False)


class _ShimHarness(unittest.TestCase):
    """A private state root, a stub launcher and a logging tool PATH."""

    shell = "/bin/sh"
    state_name = "state"

    def setUp(self) -> None:
        if _REAL_CAT is None:  # pragma: no cover - every supported host has cat
            self.skipTest("no cat on PATH")
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-shim3-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = self.root / self.state_name
        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        self.tools = self.root / "tools"
        self.tool_log = self.root / "tools.log"
        self.launcher = _write_exec(self.root / "launcher" / "claude-multi", _STUB_LAUNCHER)
        _write_exec(
            self.tools / "cat",
            f'#!/bin/sh\necho cat >> "$TOOL_LOG"\nexec {_REAL_CAT} "$@"\n',
        )
        for name in ("python", "python3", "claude-multi"):
            _write_exec(
                self.tools / name, f'#!/bin/sh\necho {name} >> "$TOOL_LOG"\nexit 97\n'
            )
        self.mid = _uuid4()
        self.rid = _uuid4()
        self.shim = self.write_shim(str(self.launcher))

    # -- fixture state -------------------------------------------------
    def write_shim(self, command: str) -> Path:
        return scope.ensure_hook_shim_v3(self.state, command)

    def set_gen(self, value: str | None = GEN, mid: str | None = None) -> None:
        path = self.state / "scopes" / (mid or self.mid) / "lineup.gen"
        path.parent.mkdir(parents=True, exist_ok=True)
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value + "\n")

    def set_seen(self, value: str | None = GEN, rid: str | None = None) -> None:
        path = self.state / "notice" / f"{rid or self.rid}.seen"
        path.parent.mkdir(parents=True, exist_ok=True)
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value + "\n")

    def prompt_argv(self, mid: str | None = None) -> list[str]:
        return [
            "session-event", "prompt", "--managed-id", mid or self.mid,
            "--launch-epoch", "0", "--hook-protocol", "3",
        ]

    def event_argv(self, event: str) -> list[str]:
        return [
            "session-event", event, "--managed-id", self.mid,
            "--launch-epoch", "0", "--hook-protocol", "3",
        ]

    # -- running -------------------------------------------------------
    def run_shim(
        self,
        argv: list[str],
        payload: str | bytes = "{}",
        *,
        stub_exit: int = 0,
        stub_out: str = "",
        stub_mode: str = "exit",
        shell: str | None = None,
        path: Path | None = None,
        timeout: float = 20,
    ) -> subprocess.CompletedProcess:
        for name in ("count", "argv", "stdin"):
            (self.stub_dir / name).unlink(missing_ok=True)
        self.tool_log.unlink(missing_ok=True)
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        env = {
            "PATH": str(path or self.tools),
            "TOOL_LOG": str(self.tool_log),
            "STUB_DIR": str(self.stub_dir),
            "STUB_EXIT": str(stub_exit),
            "STUB_OUT": stub_out,
            "STUB_MODE": stub_mode,
        }
        command = (shell or self.shell).split() + [str(self.shim), *argv]
        return subprocess.run(
            command, input=data, env=env, capture_output=True, timeout=timeout, check=False
        )

    def stub_runs(self) -> int:
        path = self.stub_dir / "count"
        return int(path.read_text().strip()) if path.exists() else 0

    def stub_argv(self) -> list[str]:
        return (self.stub_dir / "argv").read_text().splitlines()

    def stub_stdin(self) -> bytes:
        return (self.stub_dir / "stdin").read_bytes()

    def require_cat_path_trace(self) -> None:
        # Standalone BusyBox builds can execute cat internally, bypassing
        # the PATH wrappers. Calibrate the exact command-substitution shape
        # before claiming that this instrumentation counts its processes.
        self.tool_log.unlink(missing_ok=True)
        done = subprocess.run(
            self.shell.split() + ["-c", 'payload=$(cat); printf "%s" "$payload"'],
            input=b"trace-probe", env={"PATH": str(self.tools), "TOOL_LOG": str(self.tool_log)},
            capture_output=True, timeout=20, check=False,
        )
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"trace-probe", b""))
        if not self.tools_used():
            self.skipTest("BOUNDARY: shell cat bypasses PATH tracing; an exec tracer is needed")
        self.assertEqual(self.tools_used(), ["cat"])

    def tools_used(self) -> list[str]:
        if not self.tool_log.exists():
            return []
        return self.tool_log.read_text().split()


class ShimTextTests(unittest.TestCase):
    def test_goldens(self) -> None:
        assertGolden(
            self,
            SHIM_GOLDENS / "claude-multi-hook-3",
            scope.hook_shim_v3_text(Path("/state"), GOLDEN_COMMAND),
        )
        assertGolden(
            self, SHIM_GOLDENS / "claude-multi-hook", scope.hook_shim_text(GOLDEN_COMMAND)
        )

    def test_posix_syntax(self) -> None:
        text = scope.hook_shim_v3_text(Path("/state dir/x'y"), "/a b/claude-multi")
        with tempfile.TemporaryDirectory(prefix="claude-multi-shim3-") as tmp:
            path = Path(tmp) / "shim"
            path.write_bytes(text)
            shells = [[shell] for shell in _shells()]
            if _busybox():
                shells.append([_busybox(), "sh"])
            for shell in shells:
                with self.subTest(shell=shell):
                    done = subprocess.run(
                        [*shell, "-n", str(path)], capture_output=True, check=False
                    )
                    self.assertEqual(done.returncode, 0, done.stderr)

    def test_text_never_reads_the_transcript_or_the_session_env(self) -> None:
        text = scope.hook_shim_v3_text(Path("/state"), GOLDEN_COMMAND).decode()
        self.assertNotIn("transcript", text)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", text)
        self.assertNotIn("exec ", text)
        self.assertNotIn("python", text)
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertTrue(text.endswith("\nexit 0\n"))

    def test_paths_are_shell_quoted(self) -> None:
        text = scope.hook_shim_v3_text(Path("/st ate"), "/a b/claude-multi").decode()
        self.assertIn("state='/st ate'\n", text)
        self.assertIn("launcher='/a b/claude-multi'\n", text)

    def test_placeholders_are_substituted_once(self) -> None:
        text = scope.hook_shim_v3_text(Path("/s/@Q_CMD@"), "/c/@UUID@").decode()
        self.assertIn("state=/s/@Q_CMD@\n", text)
        self.assertIn("launcher=/c/@UUID@\n", text)
        self.assertEqual(text.count("[89ab]"), 2)

    def test_refuses_an_empty_command(self) -> None:
        with self.assertRaises(ScopeError):
            scope.hook_shim_v3_text(Path("/state"), "")


class TraceBoundaryTests(_ShimHarness):
    def test_builtin_cat_reports_the_path_tracing_boundary(self) -> None:
        # A real shell function models a standalone shell's internal applet:
        # it consumes stdin without executing the logging cat on PATH.
        shell = _write_exec(self.root / "builtin-shell", '''#!/bin/sh
cat() {
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\\n' "$line"
    done
}
if [ "$1" = -c ]; then
    eval "$2"
else
    script=$1
    shift
    . "$script"
fi
''')
        self.shell = f"/bin/sh {shell}"
        with self.assertRaisesRegex(unittest.SkipTest, "BOUNDARY:.*cat.*PATH"):
            FastPathTests.test_hit_starts_no_process_but_cat(self)


class FastPathTests(_ShimHarness):
    def test_hit_starts_no_process_but_cat(self) -> None:
        self.require_cat_path_trace()
        self.set_gen()
        self.set_seen()
        done = self.run_shim(self.prompt_argv(), _payload(self.rid))
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, b"")
        self.assertEqual(done.stderr, b"")
        self.assertEqual(self.stub_runs(), 0)
        self.assertEqual(self.tools_used(), ["cat"])

    def test_hit_without_a_trailing_newline_on_the_marker(self) -> None:
        self.set_gen()
        (self.state / "notice").mkdir(parents=True, exist_ok=True)
        (self.state / "notice" / f"{self.rid}.seen").write_text(GEN)
        done = self.run_shim(self.prompt_argv(), _payload(self.rid))
        self.assertEqual((done.returncode, done.stdout, self.stub_runs()), (0, b"", 0))

    def test_hit_with_a_quoted_state_root(self) -> None:
        self.state = self.root / "st ate'$x"
        self.shim = self.write_shim(str(self.launcher))
        self.set_gen()
        self.set_seen()
        done = self.run_shim(self.prompt_argv(), _payload(self.rid))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
        self.assertEqual(self.stub_runs(), 0)

    def _assert_miss(self, done, payload: str, argv: list[str] | None = None) -> None:
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.stub_runs(), 1)
        self.assertEqual(self.stub_stdin().rstrip(b"\n"), payload.encode("utf-8"))
        self.assertEqual(self.stub_argv(), argv or self.prompt_argv())
        self.assertNotIn("python", self.tools_used())
        self.assertNotIn("python3", self.tools_used())
        self.assertNotIn("claude-multi", self.tools_used())

    def test_misses_go_to_the_launcher_with_the_payload(self) -> None:
        self.set_gen()
        self.set_seen()
        other = _uuid4()
        cases = {
            "seen differs": (lambda: self.set_seen("4241 0123456789ab"), _payload(self.rid)),
            "seen missing": (lambda: self.set_seen(None), _payload(self.rid)),
            "gen missing": (lambda: self.set_gen(None), _payload(self.rid)),
            "gen empty": (lambda: self.set_gen(""), _payload(self.rid)),
            "non-UUID rid": (lambda: None, _payload("not-a-uuid")),
            "traversal rid": (lambda: None, _payload("../" + self.rid)),
            "uppercase rid": (lambda: self.set_seen(rid=self.rid.upper()), _payload(self.rid.upper())),
            "duplicate session_id": (
                lambda: None,
                '{"session_id":"%s","session_id":"%s","prompt":"x"}' % (self.rid, self.rid),
            ),
            "spaced JSON": (lambda: None, '{"session_id": "%s", "prompt": "x"}' % self.rid),
            "key not first": (lambda: None, _payload(self.rid, first=False)),
            "leading whitespace": (lambda: None, " " + _payload(self.rid)),
            "other rid marked": (lambda: self.set_seen(rid=other), _payload(_uuid4())),
            "trailing newlines": (lambda: self.set_seen(None), _payload(self.rid) + "\n\n\n"),
        }
        for name, (prepare, payload) in cases.items():
            with self.subTest(case=name):
                self.set_gen()
                self.set_seen()
                prepare()
                done = self.run_shim(self.prompt_argv(), payload)
                self._assert_miss(done, payload.rstrip("\n"))

    def test_argv_shape_misses(self) -> None:
        self.set_gen()
        self.set_seen()
        payload = _payload(self.rid)
        base = self.prompt_argv()
        variants = {
            "order": ["session-event", "prompt", "--launch-epoch", "0",
                      "--managed-id", self.mid, "--hook-protocol", "3"],
            "no protocol": base[:6],
            "extra arg": [*base, "--verbose"],
            "protocol 4": [*base[:7], "4"],
            "non-UUID managed id": self.prompt_argv(mid="../" + self.mid),
            "uppercase managed id": self.prompt_argv(mid=self.mid.upper()),
        }
        for name, argv in variants.items():
            with self.subTest(case=name):
                done = self.run_shim(argv, payload)
                self._assert_miss(done, payload, argv)

    def test_uppercase_managed_id_with_its_own_scope_still_misses(self) -> None:
        upper = self.mid.upper()
        self.set_gen(mid=upper)
        self.set_seen()
        payload = _payload(self.rid)
        done = self.run_shim(self.prompt_argv(mid=upper), payload)
        self._assert_miss(done, payload, self.prompt_argv(mid=upper))

    def test_decoy_session_id_in_prompt_text_never_hits(self) -> None:
        decoy = _uuid4()
        self.set_gen()
        self.set_seen(rid=decoy)
        prompt = '"session_id":"%s" and {"session_id":"%s"}' % (decoy, decoy)
        payload = _payload(self.rid, prompt=prompt)
        done = self.run_shim(self.prompt_argv(), payload)
        self._assert_miss(done, payload)

    def test_large_prompt_before_the_key_is_bounded_and_misses(self) -> None:
        # The unanchored strip was quadratic in bash (45 s on
        # 320 KB). With session_id after a >= 1 MiB prompt the shim must
        # finish well inside the 5 s hook timeout and use the launcher.
        self.set_gen()
        self.set_seen()
        payload = _payload(self.rid, prompt="p" * (1024 * 1024 + 7), first=False)
        started = time.monotonic()
        done = self.run_shim(self.prompt_argv(), payload)
        elapsed = time.monotonic() - started
        self._assert_miss(done, payload)
        self.assertLess(elapsed, 1.0)

    def test_large_prompt_after_the_key_is_a_fast_hit(self) -> None:
        self.set_gen()
        self.set_seen()
        payload = _payload(self.rid, prompt='q"\\' * (1024 * 1024))
        started = time.monotonic()
        done = self.run_shim(self.prompt_argv(), payload)
        elapsed = time.monotonic() - started
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
        self.assertEqual(self.stub_runs(), 0)
        self.assertLess(elapsed, 1.0)

    def test_fuzz_fast_path_only_for_the_real_first_key(self) -> None:
        # Random prompts (quotes, backslashes, unicode, glob and shell
        # metacharacters, literal decoy keys) in random key order. The decoy
        # id is always marked seen; the real id is marked at random. The only
        # hit is: session_id first AND the real id marked.
        fast = self.root / "fast-tools"
        fast.mkdir()
        os.symlink(_REAL_CAT, fast / "cat")
        rng = random.Random(3038)
        alphabet = list("abc XYZ019\"\\'`$*?[]{}:,;!#~\t\n") + ["é", "✓", "日本", "\u2028"]
        self.set_gen()
        hits = misses = 0
        for index in range(500):
            with self.subTest(index=index):
                decoy = _uuid4()
                self.set_seen(rid=decoy)
                marked = rng.random() < 0.5
                self.set_seen(GEN if marked else None)
                text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
                if rng.random() < 0.6:
                    text += '"session_id":"%s"' % decoy
                fields = [
                    ("transcript_path", "/nonexistent/t.jsonl"),
                    ("cwd", "/nonexistent"),
                    ("hook_event_name", "UserPromptSubmit"),
                    ("prompt", text),
                ]
                rng.shuffle(fields)
                fields.insert(rng.randint(0, len(fields)), ("session_id", self.rid))
                payload = json.dumps(dict(fields), separators=(",", ":"), ensure_ascii=False)
                expect_hit = fields[0][0] == "session_id" and marked
                done = self.run_shim(self.prompt_argv(), payload, path=fast)
                self.assertEqual(done.returncode, 0, done.stderr)
                if expect_hit:
                    hits += 1
                    self.assertEqual(self.stub_runs(), 0)
                    self.assertEqual(done.stdout, b"")
                else:
                    misses += 1
                    self.assertEqual(self.stub_runs(), 1)
                    self.assertEqual(
                        self.stub_stdin().rstrip(b"\n"), payload.encode("utf-8").rstrip(b"\n")
                    )
                (self.state / "notice" / f"{decoy}.seen").unlink()
        self.assertGreater(hits, 20)
        self.assertGreater(misses, 200)


class ExitMappingTests(_ShimHarness):
    NON_BLOCKING = ("prompt", "premodel", "postmodel", "subagent", "bogus")

    def _argv(self, event: str) -> list[str]:
        return self.prompt_argv() if event == "prompt" else self.event_argv(event)

    def test_non_start_end_failures_exit_zero_and_drop_output(self) -> None:
        failures = {
            "exit 1": {"stub_exit": 1},
            "exit 2": {"stub_exit": 2},
            "killed": {"stub_mode": "kill"},
            "2.26 argparse": {"stub_mode": "argparse"},
        }
        for event in self.NON_BLOCKING:
            for name, kwargs in failures.items():
                with self.subTest(event=event, failure=name):
                    done = self.run_shim(
                        self._argv(event), _payload(self.rid), stub_out="JUNK-MARKER", **kwargs
                    )
                    self.assertEqual(done.returncode, 0)
                    self.assertEqual(done.stdout, self.expected_failure_stdout(event))
                    self.assertNotIn(b"JUNK-MARKER", done.stdout)
                    self.assertEqual(self.stub_runs(), 1)
                    notes = done.stderr.decode().count("claude-multi hook shim: launcher exited")
                    self.assertEqual(notes, 1, done.stderr)
                    self.assertIn(f"(event {event})", done.stderr.decode())

    def expected_failure_stdout(self, event: str) -> bytes:
        return self.PREMODEL_DENY if event == "premodel" else b""

    PREMODEL_DENY = (scope._SHIM_PREMODEL_DENY + "\n").encode("utf-8")

    def test_premodel_failure_is_a_fixed_deny(self) -> None:
        # A premodel the launcher cannot answer
        # refuses the switch instead of letting the client allow it.
        document = json.loads(self.PREMODEL_DENY)
        self.assertEqual(
            document["hookSpecificOutput"],
            {
                "hookEventName": "PreModelSwitch",
                "permissionDecision": "deny",
                "permissionDecisionReason": hooks.PREMODEL_FAIL_CLOSED_REASON,
            },
        )
        self.assertIn("run claude-multi doctor", document["systemMessage"])
        self.assertEqual(self.PREMODEL_DENY.count(b"\n"), 1)
        payload = '{"hook_event_name":"PreModelSwitch","to_model":"evil"}'
        done = self.run_shim(self.event_argv("premodel"), payload, stub_exit=1)
        self.assertEqual((done.returncode, done.stdout), (0, self.PREMODEL_DENY))

    def test_success_forwards_stdout_once(self) -> None:
        for event in (*self.NON_BLOCKING, "start", "end"):
            with self.subTest(event=event):
                done = self.run_shim(self._argv(event), _payload(self.rid), stub_out='{"a":1}')
                self.assertEqual(done.returncode, 0)
                self.assertEqual(done.stdout, b'{"a":1}\n')
                self.assertEqual(done.stderr, b"")
                done = self.run_shim(self._argv(event), _payload(self.rid))
                self.assertEqual((done.returncode, done.stdout), (0, b""))

    def test_non_prompt_events_pass_stdin_through(self) -> None:
        payload = '{"hook_event_name":"PreModelSwitch","to_model":"x"}\n'
        done = self.run_shim(self.event_argv("premodel"), payload)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.stub_stdin(), payload.encode())
        self.assertEqual(self.stub_argv(), self.event_argv("premodel"))

    def test_start_and_end_pass_the_status_through(self) -> None:
        for event in ("start", "end"):
            for status in (1, 2, 3):
                with self.subTest(event=event, status=status):
                    done = self.run_shim(
                        self.event_argv(event), "{}", stub_exit=status, stub_out="JUNK"
                    )
                    self.assertEqual(done.returncode, status)
                    self.assertEqual(done.stdout, b"")
                    self.assertIn(b"output dropped", done.stderr)
            with self.subTest(event=event, status="killed"):
                done = self.run_shim(self.event_argv(event), "{}", stub_mode="kill")
                self.assertEqual(done.returncode, 128 + 9)
                self.assertEqual(done.stdout, b"")

    def test_launcher_absent_without_path_fallback(self) -> None:
        self.shim = self.write_shim(str(self.root / "gone" / "claude-multi"))
        (self.tools / "claude-multi").unlink()
        for event in (*self.NON_BLOCKING, "start", "end"):
            with self.subTest(event=event):
                done = self.run_shim(self._argv(event), _payload(self.rid))
                self.assertEqual(done.returncode, 0)
                self.assertEqual(done.stdout, self.expected_failure_stdout(event))
                self.assertEqual(
                    done.stderr, b"claude-multi hook shim: no claude-multi launcher found\n"
                )
                self.assertNotIn("python", self.tools_used())

    def test_path_fallback_when_the_resolved_command_is_gone(self) -> None:
        # A garbage-collected store path falls back to claude-multi on PATH
        # (here the logging stub that exits 97: a failure, so exit 0).
        self.shim = self.write_shim(str(self.root / "gone" / "claude-multi"))
        done = self.run_shim(self.event_argv("premodel"), "{}")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, self.PREMODEL_DENY)
        self.assertIn("claude-multi", self.tools_used())
        self.assertIn(b"status 97", done.stderr)
        done = self.run_shim(self.event_argv("start"), "{}")
        self.assertEqual(done.returncode, 97)

    def test_prompt_hit_needs_no_launcher(self) -> None:
        self.shim = self.write_shim(str(self.root / "gone" / "claude-multi"))
        (self.tools / "claude-multi").unlink()
        self.set_gen()
        self.set_seen()
        done = self.run_shim(self.prompt_argv(), _payload(self.rid))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))


@unittest.skipUnless(shutil.which("dash"), "dash not installed")
class DashFastPathTests(FastPathTests):
    shell = shutil.which("dash") or "dash"

    def test_fuzz_fast_path_only_for_the_real_first_key(self) -> None:  # covered on /bin/sh
        pass


@unittest.skipUnless(shutil.which("dash"), "dash not installed")
class DashExitMappingTests(ExitMappingTests):
    shell = shutil.which("dash") or "dash"


@unittest.skipUnless(_busybox(), "busybox not installed")
class BusyboxFastPathTests(FastPathTests):
    shell = f"{_busybox()} sh"

    def test_fuzz_fast_path_only_for_the_real_first_key(self) -> None:  # covered on /bin/sh
        pass


@unittest.skipUnless(_busybox(), "busybox not installed")
class BusyboxExitMappingTests(ExitMappingTests):
    shell = f"{_busybox()} sh"


class EnsureHookShimV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-shim3-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_written_executable_and_idempotent(self) -> None:
        path = scope.ensure_hook_shim_v3(self.root, "/nix/store/a/bin/claude-multi")
        self.assertEqual(path, scope.hook_shim_v3_path(self.root))
        self.assertEqual(path, self.root / "bin" / "claude-multi-hook-3")
        info = os.lstat(path)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(path.parent).st_mode), 0o700)
        self.assertEqual(
            path.read_bytes(),
            scope.hook_shim_v3_text(self.root, "/nix/store/a/bin/claude-multi"),
        )
        inode = info.st_ino
        again = scope.ensure_hook_shim_v3(self.root, "/nix/store/a/bin/claude-multi")
        self.assertEqual(again, path)
        self.assertEqual(os.lstat(path).st_ino, inode)  # unchanged bytes: no rewrite

    def test_refreshes_when_the_command_changes(self) -> None:
        path = scope.ensure_hook_shim_v3(self.root, "/nix/store/a/bin/claude-multi")
        scope.ensure_hook_shim_v3(self.root, "/nix/store/b/bin/claude-multi")
        body = path.read_text()
        self.assertIn("launcher=/nix/store/b/bin/claude-multi\n", body)
        self.assertNotIn("/nix/store/a/", body)

    def test_repairs_a_lost_exec_bit(self) -> None:
        path = scope.ensure_hook_shim_v3(self.root, "/nix/store/a/bin/claude-multi")
        os.chmod(path, 0o600)
        scope.ensure_hook_shim_v3(self.root, "/nix/store/a/bin/claude-multi")
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700)

    def test_refuses_an_empty_command(self) -> None:
        with self.assertRaises(ScopeError):
            scope.ensure_hook_shim_v3(self.root, "")
        self.assertFalse(scope.hook_shim_v3_path(self.root).exists())

    def test_the_two_shims_are_independent(self) -> None:
        shim2 = scope.ensure_hook_shim(self.root, "/nix/store/a/bin/claude-multi")
        before2 = (shim2.read_bytes(), os.lstat(shim2).st_ino)
        self.assertFalse(scope.hook_shim_v3_path(self.root).exists())
        shim3 = scope.ensure_hook_shim_v3(self.root, "/nix/store/b/bin/claude-multi")
        self.assertEqual((shim2.read_bytes(), os.lstat(shim2).st_ino), before2)
        before3 = (shim3.read_bytes(), os.lstat(shim3).st_ino)
        scope.ensure_hook_shim(self.root, "/nix/store/c/bin/claude-multi")
        self.assertEqual((shim3.read_bytes(), os.lstat(shim3).st_ino), before3)
        self.assertNotEqual(shim2, shim3)


if __name__ == "__main__":
    unittest.main()
