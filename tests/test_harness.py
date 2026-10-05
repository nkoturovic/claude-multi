"""Test-harness hygiene: tripwire, tiers, goldens, bless --check.

These tests pin the harness itself, not claude-multi behaviour:

- the always-on live-gateway connect tripwire (``tests/_tripwire.py``,
  installed by ``tests/__init__.py``) refuses 8317/8316 on loopback, records
  and logs each hit once, and passes every other address through;
- ``CM_TEST_TIER`` (``tests/_tier.py``): ``fast`` turns the real-binary gate
  into a ``BOUNDARY: fast tier`` skip, unset/``full`` is unchanged;
- ``assertGolden`` (``tests/_golden.py``) fails with a truncated unified diff
  and the exact bless command;
- ``bless.py --check`` writes nothing and exits 1 iff a golden would change
  or be pruned.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import _golden
import _tier
import _tripwire
from _catalog import GOLDENS_ROOT
from _layout import REPO_ROOT


class _NotASocket:
    """A stand-in ``self``: if the guard ever delegated, the real
    ``socket.connect`` would raise TypeError on it instead of reaching the
    network, so a broken guard fails the test without contacting anything."""


class TripwireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-tripwire-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / "guard.log"
        # Route hit logging to a private file (never the gate's log) and
        # drop this test's deliberate hits from the process record.
        env = mock.patch.dict(os.environ, {"CM_ISOLATION_GUARD_LOG": str(self.log)})
        env.start()
        self.addCleanup(env.stop)
        mark = len(_tripwire.HITS)
        self.addCleanup(lambda: _tripwire.HITS.__delitem__(slice(mark, None)))
        self.mark = mark

    def test_guard_is_installed_for_the_test_process(self) -> None:
        self.assertTrue(_tripwire.installed())

    def test_listener_observer_is_blocked_in_process_and_in_children(self) -> None:
        from tests.check_fixture_isolation import GUARD_SOURCE

        guard_dir = self.tmp / "guard"
        guard_dir.mkdir()
        (guard_dir / "sitecustomize.py").write_text(GUARD_SOURCE, encoding="utf-8")
        # A second audit hook is a fail-safe, not the guard under test. If the
        # guard regresses, this still prevents the negative control doing I/O.
        script = """
import os, sys
if sys.argv[1] == "in-process":
    import tests
from claude_multi import launch, service
from pathlib import Path

def backstop(event, args):
    if event == "open" and str(args[0]) in ("/proc/net/tcp", "/proc/net/tcp6"):
        raise RuntimeError("unguarded listener read")
    if event == "subprocess.Popen":
        raise RuntimeError("unguarded MainPID query")
sys.addaudithook(backstop)
try:
    if sys.argv[2] == "served":
        launch.served_models({"gateway": {"base_url": "http://127.0.0.1:8317"}}, "fixture")
    elif sys.argv[2] == "tcp6":
        Path("/proc/net/tcp6").read_text()
    else:
        service.gateway_pid(state_root=Path("/unused"))
except AssertionError as exc:
    assert "live listener observation" in str(exc), str(exc)
else:
    raise AssertionError("uninjected observation did not trip")
"""
        for mode in ("in-process", "external"):
            for operation in ("served", "tcp6", "MainPID"):
                with self.subTest(mode=mode, operation=operation):
                    log = self.tmp / f"{mode}-{operation}.log"
                    env = {**os.environ, "CM_ISOLATION_GUARD_LOG": str(log),
                           "PYTHONPATH": os.pathsep.join(map(str, (
                               guard_dir, REPO_ROOT / "src", REPO_ROOT)))}
                    child = subprocess.run([sys.executable, "-c", script, mode, operation],
                                           env=env, capture_output=True, text=True, timeout=20)
                    self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
                    text = log.read_text(encoding="utf-8")
                    self.assertEqual(text.count("BLOCKED "), 1, text)
                    self.assertIn("listener-owner", text)

    def test_guard_blocks_8317(self) -> None:
        with self.assertRaises(ConnectionRefusedError) as caught:
            socket.socket.connect(_NotASocket(), ("127.0.0.1", 8317))  # type: ignore[arg-type]
        self.assertEqual(str(caught.exception), "test tripwire: live gateway port")
        self.assertEqual(_tripwire.HITS[self.mark:], [("127.0.0.1", 8317)])
        log = self.log.read_text(encoding="utf-8")
        self.assertEqual(log.count("BLOCKED "), 1)
        self.assertIn("BLOCKED ('127.0.0.1', 8317)", log)
        self.assertIn("source=tests-guard", log)

    def test_guard_blocks_every_live_spelling_on_both_entry_points(self) -> None:
        addresses = [
            ("127.0.0.1", 8317),
            ("127.0.0.1", 8316),
            ("localhost", 8317),
            ("LOCALHOST", 8316),
            ("::1", 8317, 0, 0),
            ("::1", 8316),
            ("0.0.0.0", 8317),
            ("::ffff:127.0.0.1", 8317),
            ("127.0.0.2", 8316),
        ]
        for method in (socket.socket.connect, socket.socket.connect_ex):
            for address in addresses:
                with self.subTest(method=method.__name__, address=address):
                    with self.assertRaisesRegex(
                        ConnectionRefusedError, "^test tripwire: live gateway port$"
                    ):
                        method(_NotASocket(), address)  # type: ignore[arg-type]
        self.assertEqual(len(_tripwire.HITS) - self.mark, 2 * len(addresses))
        self.assertEqual(
            self.log.read_text(encoding="utf-8").count("BLOCKED "), 2 * len(addresses)
        )

    def test_other_addresses_are_not_live(self) -> None:
        for address in (
            ("127.0.0.1", 8318),
            ("127.0.0.1", 0),
            ("10.0.0.9", 8317),
            ("192.168.1.5", 8316),
            ("api.example.com", 8317),
            ("127.0.0.1", "8317"),
            "/run/user/1000/gateway.sock",
            b"\0abstract",
        ):
            with self.subTest(address=address):
                self.assertFalse(_tripwire.is_live_gateway(address))

    def test_ordinary_loopback_connects_pass_through(self) -> None:
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        accepted: list[socket.socket] = []
        thread = threading.Thread(target=lambda: accepted.append(server.accept()[0]))
        thread.start()
        with socket.socket() as client:
            client.settimeout(5)
            client.connect(server.getsockname())
        thread.join(5)
        for sock in accepted:
            sock.close()
        self.assertEqual(len(accepted), 1)
        with socket.socket() as client:
            client.settimeout(5)
            self.assertEqual(client.connect_ex(server.getsockname()), 0)
        self.assertEqual(_tripwire.HITS[self.mark:], [])
        self.assertFalse(self.log.exists())

    def test_hit_is_logged_once_over_an_inner_tripwire(self) -> None:
        # The external sitecustomize tripwire is installed first, so this guard
        # wraps it; the guard refuses before delegating, so the inner one never
        # sees (or logs) the hit: one BLOCKED line per attempt, not two.
        inner_calls: list[object] = []

        def inner(self: object, address: object) -> None:
            inner_calls.append(address)
            with open(self_log, "a", encoding="utf-8") as handle:
                handle.write(f"BLOCKED {address!r} external\n")
            raise ConnectionRefusedError(111, "isolation guard: live gateway blocked")

        self_log = self.log
        guarded = _tripwire._wrap(inner)
        with self.assertRaisesRegex(ConnectionRefusedError, "test tripwire"):
            guarded(_NotASocket(), ("127.0.0.1", 8317))
        self.assertEqual(inner_calls, [])
        self.assertEqual(self.log.read_text(encoding="utf-8").count("BLOCKED "), 1)
        # Everything else still reaches the wrapped connect.
        with self.assertRaisesRegex(ConnectionRefusedError, "isolation guard"):
            guarded(_NotASocket(), ("127.0.0.1", 18317))
        self.assertEqual(inner_calls, [("127.0.0.1", 18317)])

    def test_sandbox_suite_imports_the_tests_package(self) -> None:
        # The Nix sandbox (flake check) discovers with -t, so tests/__init__.py
        # arms this guard there too, exactly as in the dev gate.
        text = (REPO_ROOT / "tests" / "default.nix").read_text(encoding="utf-8")
        self.assertIn(
            'discover -s "$staged/claude-multi/tests" -t "$staged/claude-multi"', text
        )

    def test_uninjected_served_models_trips_before_proc_read_or_transport(self) -> None:
        from claude_multi import launch, service

        transport = mock.Mock()

        def guarded_open(path, *args, **kwargs):
            # Exercise the actual observer's read without allowing a live read
            # even if the audit guard regresses.
            sys.audit("open", str(path), "r", 0)
            self.fail("uninjected listener observation reached a real file open")

        with mock.patch.object(service.sys, "platform", "linux"), \
                mock.patch("io.open", side_effect=guarded_open):
            with self.assertRaisesRegex(AssertionError, "test tripwire: live listener observation"):
                launch.served_models({"gateway": {"base_url": "http://127.0.0.1:8317"}},
                                     "fixture-token", models_get=transport)
        transport.assert_not_called()
        self.assertEqual(_tripwire.HITS[self.mark:], [("listener-owner", "/proc/net/tcp")])
        self.assertEqual(self.log.read_text().count("BLOCKED "), 1)

    def test_install_is_idempotent(self) -> None:
        before = (socket.socket.connect, socket.socket.connect_ex)
        _tripwire.install()
        self.assertEqual((socket.socket.connect, socket.socket.connect_ex), before)


class IsolationDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests import check_fixture_isolation

        self.isolation = check_fixture_isolation
        self.before = {page: self.region(name, b"old")
                       for page, name in self.isolation.CATALOG_DOC_REGIONS}
        self.before["docs/reference/cli.md"] = self.region("cli-reference", b"commands")
        self.before["README.md"] = b"Overview\n"

    @staticmethod
    def region(name: str, body: bytes) -> bytes:
        return (f"Intro\n<!-- generated: {name} (tools/docs_gen.py) -->\n".encode()
                + body + f"\n<!-- end of generated: {name} -->\nOutro\n".encode())

    def test_only_catalog_bodies_may_change(self) -> None:
        after = dict(self.before)
        for page, name in self.isolation.CATALOG_DOC_REGIONS:
            after[page] = self.region(name, b"new")
        for kind in ("model", "retired"):
            with self.subTest(kind=kind):
                changed, problems = self.isolation._doc_changes(self.before, after, kind=kind)
                self.assertEqual(problems, [])
                self.assertEqual(set(changed), {f"{page}: {name}"
                                               for page, name in self.isolation.CATALOG_DOC_REGIONS})
        self.assertEqual(self.isolation._doc_changes(self.before, self.before, kind="operator"), ([], []))

    def test_prose_and_unrelated_regions_or_documents_are_refused(self) -> None:
        page, name = self.isolation.CATALOG_DOC_REGIONS[0]
        before = dict(self.before)
        before[page] += self.region("unrelated", b"stable")
        for path, data in ((page, before[page].replace(b"Intro", b"Changed intro", 1)),
                           (page, before[page].replace(b"stable", b"changed")),
                           ("docs/reference/cli.md", self.region("cli-reference", b"new commands")),
                           ("README.md", b"Changed overview\n")):
            with self.subTest(path=path, data=data):
                after = {**before, path: data}
                self.assertTrue(self.isolation._doc_changes(before, after, kind="model")[1])
        # An allowed change must not conceal a prose change on the same page.
        after = {**before, page: before[page].replace(b"old", b"new").replace(b"Outro", b"changed")}
        self.assertTrue(self.isolation._doc_changes(before, after, kind="retired")[1])

    def test_added_and_deleted_documents_are_refused(self) -> None:
        for path in ("docs/new.md", "docs/guides/models.md", "NEW.md"):
            before = {**self.before, path: b"document\n"}
            after = {name: data for name, data in before.items() if name != path}
            with self.subTest(path=path):
                self.assertTrue(self.isolation._doc_changes(before, after, kind="model")[1])
                self.assertTrue(self.isolation._doc_changes(after, before, kind="model")[1])

    def test_marker_changes_are_refused(self) -> None:
        page, name = self.isolation.CATALOG_DOC_REGIONS[0]
        for data in (self.before[page].replace(b"<!-- end", b"<!-- removed end"),
                     self.before[page] + self.region(name, b"duplicate"),
                     self.region(name, self.region("unrelated", b"nested"))):
            with self.subTest(data=data):
                self.assertTrue(self.isolation._doc_changes(self.before, {**self.before, page: data},
                                                           kind="model")[1])

    def test_operator_probe_has_no_catalog_allowance(self) -> None:
        page, name = self.isolation.CATALOG_DOC_REGIONS[0]
        after = {**self.before, page: self.region(name, b"new")}
        changed, problems = self.isolation._doc_changes(self.before, after, kind="operator")
        self.assertEqual(changed, [])
        self.assertTrue(problems)

    def test_refresh_checks_generator_exit_and_all_document_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cm-isolation-docs-") as tmp:
            root = Path(tmp)
            for name, data in self.before.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            self.assertEqual(self.isolation._docs_snapshot(root), self.before)
            for code in (0, 1):
                result = subprocess.CompletedProcess([], code, stdout="", stderr="")
                with mock.patch.object(self.isolation.subprocess, "run", return_value=result) as run, \
                        mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    self.assertEqual(self.isolation._refresh_catalog_docs(root, {}, [], kind="model"), code == 0)
                    self.assertEqual(run.call_args.args[0], [sys.executable, "tools/docs_gen.py"])
                    self.assertEqual(run.call_args.kwargs["cwd"], root)
            def unexpected_write(*args, **kwargs):
                (root / "docs" / "added.txt").write_text("unexpected doc\n")
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with mock.patch.object(self.isolation.subprocess, "run", side_effect=unexpected_write), \
                    mock.patch("sys.stdout"):
                self.assertFalse(self.isolation._refresh_catalog_docs(root, {}, [], kind="model"))


class TierTests(unittest.TestCase):
    def _env(self, value: str | None) -> None:
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop(_tier.ENV, None)
        if value is not None:
            os.environ[_tier.ENV] = value

    def test_default_and_full_keep_the_real_gate(self) -> None:
        for value in (None, "", "full"):
            with self.subTest(value=value):
                self._env(value)
                self.assertEqual(_tier.tier(), "full")
                self.assertIsNone(_tier.fast_tier_boundary())
                gate = _tier.real_binary_gate(REPO_ROOT / "absent.json", what="probes")
                self.assertIsNone(gate.trusted)
                self.assertIn("native-contract.json unavailable", gate.boundary or "")

    def test_fast_tier_skips_before_touching_the_contract(self) -> None:
        self._env("fast")
        with mock.patch.object(_tier.probe, "pinned_binary_gate") as real_gate:
            gate = _tier.real_binary_gate(REPO_ROOT / "absent.json", what="probes")
        real_gate.assert_not_called()
        self.assertIsNone(gate.trusted)
        self.assertIsNone(gate.contract)
        self.assertTrue((gate.boundary or "").startswith("BOUNDARY: fast tier"))

    def test_unknown_tier_is_refused(self) -> None:
        self._env("quick")
        with self.assertRaisesRegex(RuntimeError, "CM_TEST_TIER='quick'"):
            _tier.fast_tier_boundary()


class AssertGoldenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-golden-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_equal_bytes_pass(self) -> None:
        golden = self.tmp / "g.txt"
        golden.write_bytes(b"alpha\nbeta\n")
        _golden.assertGolden(self, golden, b"alpha\nbeta\n")

    def test_mismatch_shows_diff_and_bless_command(self) -> None:
        golden = self.tmp / "g.txt"
        golden.write_bytes(b"alpha\nbeta\n")
        with self.assertRaises(self.failureException) as caught:
            _golden.assertGolden(self, golden, b"alpha\ngamma\n")
        message = str(caught.exception)
        self.assertIn("golden mismatch:", message)
        self.assertIn("-beta\n", message)
        self.assertIn("+gamma\n", message)
        self.assertIn(
            "PYTHONPATH=src:tests python3 tests/bless.py\n", message
        )
        self.assertIn(_golden.BLESS_COMMAND, message)
        self.assertIn(_golden.CHECK_COMMAND, message)

    def test_long_diff_is_truncated(self) -> None:
        golden = self.tmp / "g.txt"
        golden.write_bytes(b"".join(b"line %d\n" % n for n in range(500)))
        with self.assertRaises(self.failureException) as caught:
            _golden.assertGolden(self, golden, b"")
        message = str(caught.exception)
        self.assertIn("... diff truncated:", message)
        self.assertLess(message.count("\n-line "), _golden.MAX_DIFF_LINES)

    def test_missing_golden_names_the_bless_command(self) -> None:
        with self.assertRaises(self.failureException) as caught:
            _golden.assertGolden(self, self.tmp / "absent.txt", b"x")
        self.assertIn("is missing", str(caught.exception))
        self.assertIn(_golden.BLESS_COMMAND, str(caught.exception))

    def test_non_bytes_are_refused(self) -> None:
        with self.assertRaises(TypeError):
            _golden.assertGolden(self, self.tmp / "g.txt", "text")  # type: ignore[arg-type]


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class BlessCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import bless

        cls.bless = bless
        cls.planned = bless.plan()

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-bless-check-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.copy = self.tmp / "goldens"
        shutil.copytree(GOLDENS_ROOT, self.copy)

    def test_plan_is_confined_to_the_goldens_tree(self) -> None:
        self.assertTrue(self.planned)
        for path in self.planned:
            self.assertTrue(path.is_relative_to(GOLDENS_ROOT), path)

    def test_clean_tree_checks_clean(self) -> None:
        self.assertEqual(self.bless.check(self.planned, self.copy), [])

    def test_dirty_tree_lists_changes_and_prunes_and_writes_nothing(self) -> None:
        changed = self.copy / "v2" / "managed" / "lead-appendix.md"
        changed.write_bytes(changed.read_bytes() + b"drift\n")
        missing = self.copy / "v2" / "managed" / "env.json"
        missing.unlink()
        (self.copy / "stale.txt").write_bytes(b"left behind\n")
        (self.copy / "old" / "deep").mkdir(parents=True)
        (self.copy / "old" / "deep" / "x.json").write_bytes(b"{}\n")
        before = _snapshot(self.copy)
        lines = self.bless.check(self.planned, self.copy)
        self.assertEqual(_snapshot(self.copy), before)
        self.assertFalse(missing.exists())

        self.assertIn(f"would change {changed}", lines)
        self.assertIn(f"would add {missing}", lines)
        self.assertIn(f"would prune {self.copy / 'stale.txt'}", lines)
        self.assertIn(f"would prune {self.copy / 'old' / 'deep' / 'x.json'}", lines)
        self.assertIn(f"would prune {self.copy / 'old' / 'deep'}/", lines)
        self.assertIn(f"would prune {self.copy / 'old'}/", lines)
        self.assertEqual(len(lines), 6, lines)

    def test_cli_check_on_the_checked_in_tree(self) -> None:
        before = _snapshot(GOLDENS_ROOT)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(REPO_ROOT / "src"), str(REPO_ROOT / "tests")]
        )
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tests" / "bless.py"), "--check"],
            cwd=self.tmp, env=env, capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"bless --check: {len(self.planned)} golden files clean", result.stdout)
        self.assertNotIn("wrote ", result.stdout)
        self.assertEqual(_snapshot(GOLDENS_ROOT), before)

    def test_main_exit_codes(self) -> None:
        with mock.patch.object(self.bless, "check", return_value=["would change x"]), \
                mock.patch("sys.stdout"):
            self.assertEqual(self.bless.main(["--check"]), 1)
        with mock.patch.object(self.bless, "check", return_value=[]), \
                mock.patch("sys.stdout"):
            self.assertEqual(self.bless.main(["--check"]), 0)
        with mock.patch.object(self.bless, "apply") as apply, mock.patch("sys.stderr"):
            self.assertEqual(self.bless.main(["--bogus"]), 2)
            self.assertEqual(self.bless.main(["--check", "extra"]), 2)
        apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
