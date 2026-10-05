"""Tests for launch: executable contract, readiness, ordering, execve boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from claude_multi import catalog, compiler, endpoint, launch, pin, scope, service, sessions, state, strict_json
from claude_multi.launch import LaunchError
from _catalog import FIXTURE_ROOT
import _v3


CATALOG_ROOT = FIXTURE_ROOT
FIXED_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"


class _HealthHandler(BaseHTTPRequestHandler):
    status = 200

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/healthz":
            self.send_error(404)
            return
        self.send_response(type(self).status)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _serve(status: int = 200):
    handler = type("Handler", (_HealthHandler,), {"status": status})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    return server


class LaunchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-launch-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(self._cleanup)
        self.project = self.root / "project"
        self.project.mkdir()
        self.bundle = catalog.load_catalog(CATALOG_ROOT)
        # The 2.x snapshot input comes from tests/_v3.py.
        self.resolved = _v3.resolve_v1(self.bundle.docs, _v3.default_composition())
        self.snapshot = _v3.snapshot_v1(self.resolved)
        self.digest = strict_json.bundle_digest(self.snapshot)
        self.store = sessions.SessionStore(
            self.root / "state",
            strict_json.load(CATALOG_ROOT / "schemas" / "session.schema.json"),
        )
        # The owned copy of the pin under a temp data root.
        self.env = {"HOME": str(self.root / "home")}
        self.platform = pin.host_platform()
        install = pin.owned_path(self.env, "2.1.216", self.platform)
        state.ensure_private_dir(install.parent)
        install.write_bytes(b"#!/bin/fake-claude\n")
        install.chmod(0o755)
        self.install = install
        self.native_contract = {
            "verified": [{
                "version": "2.1.216",
                "platforms": {self.platform: {
                    "sha256": hashlib.sha256(install.read_bytes()).hexdigest(),
                    "size": install.stat().st_size,
                }},
                "manifest_sha256": "1" * 64,
                "signature_sha256": None,
                "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE",
                "verified_at": "2026-07-21",
                "evidence": {self.platform: "battery", "receipt_sha256": "2" * 64},
            }],
            "generic_agent_aliases": {
                "values": ["claude"],
                "status": "provisionally-trusted",
                "evidence": "test fixture",
            },
        }
        # Docs copy that consumes the fixture native contract.
        self.docs = dict(self.bundle.docs)
        self.docs["native-contract"] = self.native_contract
        self.home = self.root / "home"
        self.home.mkdir(exist_ok=True)
        os.chmod(self.home, 0o700)
        token_dir = state.ensure_private_dir(self.home / ".config" / "claude-multi")
        self.token_file = token_dir / "api-key"
        state.atomic_write(self.token_file, b"a" * 64 + b"\n")

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)


    def _record(self):
        return _v3.make_record(
            session_id=FIXED_ID,
            cwd=str(self.project),
            composition_name="default",
            snapshot=self.snapshot,
            catalog_version=1,
            catalog_hash="sha256:" + "0" * 64,
            launcher_version="2.0.0",
            now="2026-07-21T00:00:00Z",
        )


class ResolveClaudeTests(LaunchTestCase):
    def _record(self, **changes):
        contract = copy.deepcopy(self.native_contract)
        contract["verified"][0]["platforms"][self.platform].update(changes)
        return contract

    def resolve(self, contract=None, **kwargs):
        return launch.resolve_claude(contract or self.native_contract, environ=self.env, **kwargs)

    def test_verifies_the_owned_copy_and_reports_status(self) -> None:
        status = self.resolve()
        self.assertEqual(status.inspected_path, self.install)
        self.assertEqual(status.inspected_path.parent.name, "2.1.216")
        self.assertEqual(status.validated_version, "2.1.216")
        self.assertEqual(status.platform, self.platform)
        self.assertEqual(status.sha256, self.native_contract["verified"][0]["platforms"][self.platform]["sha256"])
        self.assertFalse(status.migrated)

    def test_owned_path_follows_the_data_root(self) -> None:
        self.assertEqual(self.install, self.root / "home/.local/share/claude-multi/claude/2.1.216" /
                         pin.binary_name(self.platform))
        self.assertEqual(pin.binary_name("win32-x64"), "claude.exe")
        self.assertEqual(pin.binary_name("darwin-arm64"), "claude")

    def test_missing_copy_names_the_setup_step(self) -> None:
        self.install.unlink()
        with self.assertRaises(LaunchError) as raised:
            self.resolve()
        self.assertEqual(str(raised.exception), "Claude Code 2.1.216 is not set up for claude-multi — "
                                                 "run `claude-multi setup --step claude`")

    def test_missing_platform_build_fails_closed(self) -> None:
        contract = copy.deepcopy(self.native_contract)
        contract["verified"][0]["platforms"] = {"win32-arm64" if self.platform != "win32-arm64" else "linux-x64":
                                                contract["verified"][0]["platforms"][self.platform]}
        with self.assertRaisesRegex(LaunchError, "no verified Claude Code 2.1.216 build for"):
            self.resolve(contract)

    def test_symlinked_copy_or_directory_fails_closed(self) -> None:
        real = self.root / "elsewhere"
        self.install.rename(real)
        self.install.symlink_to(real)
        with self.assertRaisesRegex(LaunchError, "is a symlink"):
            self.resolve()
        self.install.unlink()
        real.rename(self.install)
        version_dir = self.install.parent
        moved = self.root / "moved-version"
        version_dir.rename(moved)
        version_dir.symlink_to(moved)
        with self.assertRaisesRegex(LaunchError, "is a symlink"):
            self.resolve()

    def test_non_regular_copy_fails_closed(self) -> None:
        self.install.unlink()
        self.install.mkdir()
        with self.assertRaisesRegex(LaunchError, "regular file"):
            self.resolve()

    def test_size_and_hash_mismatch_fail_closed(self) -> None:
        with self.assertRaisesRegex(LaunchError, "bytes, not the verified"):
            self.resolve(self._record(size=999))
        with self.assertRaisesRegex(LaunchError, "does not match the verified sha256"):
            self.resolve(self._record(sha256="0" * 64))

    def test_no_basename_rule_and_version_from_the_contract(self) -> None:
        # The executable is always named claude; the directory carries the version.
        self.assertEqual(self.resolve().inspected_path.name, pin.binary_name(self.platform))

    def test_non_executable_copy_fails_closed(self) -> None:
        self.install.chmod(0o644)
        with self.assertRaisesRegex(LaunchError, "not executable"):
            self.resolve()

    def test_full_hash_on_each_resolution(self) -> None:
        with mock.patch.object(pin, "file_sha256", wraps=pin.file_sha256) as spy:
            self.resolve()
            self.resolve()
        self.assertEqual(spy.call_count, 2)

    def test_migration_copies_a_hash_identical_retained_copy_only(self) -> None:
        retained = state.ensure_private_dir(self.root / "home/.local/share/claude-multi/pinned-clients")
        source = retained / "2.1.216"
        source.write_bytes(self.install.read_bytes())
        source.chmod(0o755)
        self.install.unlink()
        before = (source.stat().st_ino, source.read_bytes())
        status = self.resolve(retained_root=retained)
        self.assertTrue(status.migrated)
        self.assertEqual(self.install.read_bytes(), b"#!/bin/fake-claude\n")
        self.assertNotEqual(self.install.stat().st_ino, source.stat().st_ino)
        self.assertEqual((source.stat().st_ino, source.read_bytes()), before)  # copied, never moved
        self.assertEqual(stat.S_IMODE(self.install.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.install.parent.stat().st_mode), 0o700)
        # A different retained file is never copied.
        self.install.unlink()
        source.write_bytes(b"#!/bin/other\n")
        with self.assertRaisesRegex(LaunchError, "not set up"):
            self.resolve(retained_root=retained)
        self.assertFalse(self.install.exists())

    def test_read_only_resolution_never_migrates(self) -> None:
        retained = state.ensure_private_dir(self.root / "home/.local/share/claude-multi/pinned-clients")
        (retained / "2.1.216").write_bytes(self.install.read_bytes())
        (retained / "2.1.216").chmod(0o755)
        self.install.unlink()
        with self.assertRaisesRegex(LaunchError, "not set up"):
            self.resolve()
        self.assertFalse(self.install.exists())


class DoctorBinaryReportTests(LaunchTestCase):
    def test_clean_fixture_reports_version_and_no_problems(self) -> None:
        problems, info = launch.doctor_binary_report(self.native_contract, environ=self.env)
        self.assertEqual(problems, [])
        self.assertEqual(len(info), 1)
        self.assertIn("Managed Claude 2.1.216 verified", info[0])
        self.assertIn("~/.local/share/claude-multi/claude/2.1.216/", info[0])

    def test_failure_matches_launch_failure_exactly(self) -> None:
        tampered = copy.deepcopy(self.native_contract)
        tampered["verified"][0]["platforms"][self.platform]["sha256"] = "0" * 64
        with self.assertRaises(LaunchError) as raised:
            launch.resolve_claude(tampered, environ=self.env)
        problems, info = launch.doctor_binary_report(tampered, environ=self.env)
        self.assertEqual(info, [])
        self.assertEqual(len(problems), 1)
        self.assertIn(str(raised.exception), problems[0])

    def test_pending_migration_is_info_not_a_problem(self) -> None:
        retained = state.ensure_private_dir(self.root / "home/.local/share/claude-multi/pinned-clients")
        (retained / "2.1.216").write_bytes(self.install.read_bytes())
        (retained / "2.1.216").chmod(0o755)
        self.install.unlink()
        problems, info = launch.doctor_binary_report(self.native_contract, environ=self.env,
                                                     retained_root=retained)
        self.assertEqual(problems, [])
        self.assertIn("the next launch copies the verified retained copy", info[0])
        self.assertFalse(self.install.exists())  # doctor never writes

    def test_malformed_contract_is_a_problem_not_a_crash(self) -> None:
        for bad in ({}, {"verified": []}, {"verified": [{}]}, {"verified": [{"version": 1}]}):
            with self.subTest(bad=bad):
                problems, info = launch.doctor_binary_report(bad, environ=self.env)
                self.assertEqual(info, [])
                self.assertEqual(len(problems), 1)
                self.assertIn("malformed native-contract record", problems[0])


class SharedDaemonTests(LaunchTestCase):
    def _domain(self) -> Path:
        domain = self.root / f"cc-daemon-{os.geteuid()}"
        domain.mkdir()
        return domain

    def test_absent_domain_is_informational(self) -> None:
        status = launch.inspect_shared_daemon(
            domain_dir=self.root / "no-such-daemon", uid=os.geteuid()
        )
        self.assertEqual(status.state, "absent")
        self.assertIsNone(status.pid)
        self.assertIsNone(status.version)

    def test_domain_without_metadata_is_present_without_pid(self) -> None:
        domain = self._domain()
        status = launch.inspect_shared_daemon(domain_dir=domain, uid=os.geteuid())
        self.assertEqual(status.state, "present")
        self.assertIsNone(status.pid)
        self.assertIn("pid not exposed", status.summary)

    def test_metadata_exposes_pid(self) -> None:
        domain = self._domain()
        (domain / "metadata.json").write_bytes(b'{"pid": 4321, "version": "2.1.217"}')
        status = launch.inspect_shared_daemon(domain_dir=domain, uid=os.geteuid())
        self.assertEqual(status.state, "present")
        self.assertEqual(status.pid, 4321)
        # The simplified reader never renders version strings.
        self.assertIsNone(status.version)
        self.assertNotIn("2.1.217", status.summary)

    def test_unparseable_metadata_degrades_to_no_pid(self) -> None:
        domain = self._domain()
        (domain / "metadata.json").write_bytes(b"not json at all")
        status = launch.inspect_shared_daemon(domain_dir=domain, uid=os.geteuid())
        self.assertEqual(status.state, "present")
        self.assertIsNone(status.pid)

    def test_wrong_owner_domain_is_absent(self) -> None:
        domain = self._domain()
        nobody = 65534 if os.geteuid() != 65534 else 65533
        status = launch.inspect_shared_daemon(domain_dir=domain, uid=nobody)
        self.assertEqual(status.state, "absent")
        self.assertIsNone(status.pid)

    def test_symlinked_domain_is_absent(self) -> None:
        real = self._domain()
        link = self.root / "cc-daemon-link"
        link.symlink_to(real)
        status = launch.inspect_shared_daemon(domain_dir=link, uid=os.geteuid())
        self.assertEqual(status.state, "absent")

    def test_ansi_escape_in_metadata_never_rendered(self) -> None:
        domain = self._domain()
        (domain / "metadata.json").write_bytes(
            b'{"pid": 7, "version": "2.1.217\\u001b[2J evil"}'
        )
        status = launch.inspect_shared_daemon(domain_dir=domain, uid=os.geteuid())
        self.assertEqual(status.state, "present")
        self.assertEqual(status.pid, 7)
        self.assertNotIn("\x1b", status.summary)


class ReadinessTests(LaunchTestCase):
    def test_readiness_success_returns_token(self) -> None:
        server = _serve(200)
        try:
            gateway = {
                "gateway": {
                    **self.bundle.docs["gateway"]["gateway"],
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                }
            }
            token = launch.check_readiness(gateway, home=self.home)
            self.assertEqual(token, "a" * 64)
        finally:
            server.shutdown()
            server.server_close()

    def test_readiness_health_failure(self) -> None:
        server = _serve(500)
        try:
            gateway = {
                "gateway": {
                    **self.bundle.docs["gateway"]["gateway"],
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                }
            }
            with self.assertRaisesRegex(LaunchError, "status 500"):
                launch.check_readiness(gateway, home=self.home)
        finally:
            server.shutdown()
            server.server_close()

    def test_readiness_bad_token_shape(self) -> None:
        state.atomic_write(self.token_file, b"short\n")
        with self.assertRaisesRegex(LaunchError, "token shape"):
            launch.check_readiness(self.bundle.docs["gateway"], home=self.home)

    def test_readiness_missing_token_file(self) -> None:
        self.token_file.unlink()
        with self.assertRaisesRegex(LaunchError, "key file"):
            launch.check_readiness(self.bundle.docs["gateway"], home=self.home)

    def test_readiness_connection_refused(self) -> None:
        gateway = {
            "gateway": {
                **self.bundle.docs["gateway"]["gateway"],
                "base_url": "http://127.0.0.1:1",  # nothing listens here
            }
        }
        with self.assertRaisesRegex(LaunchError, "health check failed"):
            launch.check_readiness(gateway, home=self.home)

    def test_non_loopback_gateway_rejected(self) -> None:
        gateway = {
            "gateway": {
                **self.bundle.docs["gateway"]["gateway"],
                "base_url": "http://10.0.0.9:8317",
            }
        }
        with self.assertRaisesRegex(LaunchError, "loopback"):
            launch.check_readiness(gateway, home=self.home)


if __name__ == "__main__":
    unittest.main()


class RemedyTests(LaunchTestCase):
    def test_every_binary_refusal_names_the_setup_step(self) -> None:
        original = self.install.read_bytes()
        cases = (
            ("missing", "Claude Code 2.1.216 is not set up for claude-multi"),
            ("symlink", "is a symlink"),
            ("directory", "not a regular file"),
            ("mode", "not executable"),
            ("hash", "does not match the verified sha256"),
        )
        for kind, detail in cases:
            with self.subTest(kind=kind):
                contract = copy.deepcopy(self.native_contract)
                if kind == "missing":
                    self.install.unlink()
                elif kind == "symlink":
                    self.install.unlink()
                    self.install.symlink_to(self.token_file)
                elif kind == "directory":
                    self.install.unlink()
                    self.install.mkdir()
                elif kind == "mode":
                    self.install.chmod(0o600)
                elif kind == "hash":
                    contract["verified"][0]["platforms"][self.platform]["sha256"] = "0" * 64
                with self.assertRaises(launch.LaunchError) as raised:
                    launch.resolve_claude(contract, environ=self.env)
                text = str(raised.exception)
                self.assertIn(detail, text)
                self.assertIn("claude-multi setup --step claude", text)
                self.assertNotIn("claude-multi update", text)
                if self.install.is_dir() and not self.install.is_symlink():
                    self.install.rmdir()
                elif self.install.is_symlink():
                    self.install.unlink()
                self.install.write_bytes(original)
                self.install.chmod(0o755)

    def test_key_remedies_are_distinct_and_do_not_contact_health(self) -> None:
        health = mock.Mock(side_effect=AssertionError("must not request health"))
        cases = (("missing", "run claude-multi-proxy init"),
                 ("mode", "mode 0600 (chmod 600 ~/.config/claude-multi/api-key)"),
                 ("shape", "mv ~/.config/claude-multi/api-key ~/.config/claude-multi/api-key.invalid"),
                 ("encoding", "create a fresh key"))
        for kind, remedy in cases:
            with self.subTest(kind=kind):
                state.atomic_write(self.token_file, b"a" * 64 + b"\n")
                if kind == "missing":
                    self.token_file.unlink()
                elif kind == "mode":
                    self.token_file.chmod(0o644)
                elif kind == "shape":
                    state.atomic_write(self.token_file, b"dummy-invalid-secret")
                else:
                    state.atomic_write(self.token_file, b"\xff\xfe")
                with self.assertRaises(launch.GatewayKeyError) as raised:
                    launch.check_readiness(self.bundle.docs["gateway"], home=self.home, health_get=health)
                self.assertIn(remedy, raised.exception.remedy)
                self.assertNotIn("dummy-invalid-secret", str(raised.exception))
                self.assertNotIn("systemctl", raised.exception.remedy)
                self.assertEqual(launch.gateway_problem_text(raised.exception),
                                 f"{raised.exception} — {raised.exception.remedy}")
                health.assert_not_called()
                if self.token_file.exists():
                    self.token_file.chmod(0o600)

    def test_readiness_and_models_errors_carry_unit_remedy(self) -> None:
        for getter in (mock.Mock(side_effect=ConnectionRefusedError("fixture")), mock.Mock(return_value=503)):
            with self.assertRaises(launch.LaunchError) as raised:
                launch.check_readiness(self.bundle.docs["gateway"], home=self.home, health_get=getter)
            self.assertEqual(raised.exception.remedy, launch.gateway_unit_remedy())
        with self.assertRaises(launch.LaunchError) as raised:
            launch.served_models(self.bundle.docs["gateway"], "dummy",
                                 models_get=mock.Mock(side_effect=ConnectionRefusedError("fixture")),
                                 owner_check=lambda _: service.OwnerVerdict("ours", "fixture"))
        self.assertEqual(raised.exception.remedy, launch.gateway_unit_remedy())
        # The on-demand backend names only the product's verbs; the supervised
        # unit adds its recovery and diagnosis commands.
        self.assertIn("claude-multi gateway start", launch.gateway_problem_text(raised.exception))
        self.assertNotIn("systemctl", launch.gateway_unit_remedy())
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=18317, backend=endpoint.SYSTEMD))
        with mock.patch.object(endpoint.sys, "platform", "linux"):
            supervised = launch.gateway_unit_remedy(self.home)
        self.assertIn("start limit hit", supervised)
        self.assertIn(f"systemctl --user reset-failed {endpoint.DEFAULT_UNIT}", supervised)


class GatewayAuthorizationTests(unittest.TestCase):
    def test_absent_listener_needs_no_ownership_attention(self):
        attention = mock.Mock()
        verdict = service.OwnerVerdict("none", "no listener")
        self.assertEqual(launch.check_listener("http://127.0.0.1:8317",
                         owner_check=lambda _: verdict, attention=attention), verdict)
        attention.assert_not_called()

    def test_foreign_listener_never_attaches_or_sends_authorization(self):
        from claude_multi import service
        foreign = lambda _base: service.OwnerVerdict("foreign", "another user (uid 9000)", 9000)
        with mock.patch.object(launch.http.client, "HTTPConnection") as connection:
            with self.assertRaisesRegex(launch.GatewayOwnerError, "token was not sent"):
                launch._default_models_get("http://127.0.0.1:8333", "secret-fixture", owner_check=foreign)
            connection.assert_not_called()

    def test_injected_transport_is_also_gated_and_needs_a_proven_listener(self):
        from claude_multi import service
        gateway = {"gateway": {"base_url": "http://127.0.0.1:8333"}}
        for kind in ("ours", "unknown", "none", "foreign"):
            calls = mock.Mock(return_value=(200, {"fixture-selector"}))
            attention = []
            with self.subTest(kind=kind):
                if kind in ("foreign", "unknown"):
                    # A served read sends the token only to a proven gateway:
                    # another process of yours on the port gets nothing.
                    with self.assertRaisesRegex(launch.GatewayOwnerError, "token was not sent"):
                        launch.served_models(gateway, "secret-fixture", models_get=calls,
                                             owner_check=lambda _: service.OwnerVerdict(kind, "fixture"),
                                             attention=attention.append)
                    calls.assert_not_called()
                else:
                    self.assertEqual(launch.served_models(
                        gateway, "secret-fixture", models_get=calls,
                        owner_check=lambda _: service.OwnerVerdict(kind, "fixture"), attention=attention.append),
                        ({"fixture-selector"}, 200))
                    calls.assert_called_once()
                self.assertEqual(attention, [])

    def test_an_unconfirmed_listener_is_attention_where_no_proof_is_required(self):
        from claude_multi import service
        attention = []
        verdict = service.OwnerVerdict("unknown", "another process of yours")
        self.assertEqual(launch.check_listener("http://127.0.0.1:8333", owner_check=lambda _: verdict,
                                               attention=attention.append), verdict)
        self.assertEqual(len(attention), 1)
        with self.assertRaisesRegex(launch.GatewayOwnerError, "not proven to be the claude-multi gateway"):
            launch.check_listener("http://127.0.0.1:8333", owner_check=lambda _: verdict, require_proof=True)

    def test_a_foreign_same_user_listener_never_sees_the_token(self):
        """A same-user program listening on the gateway port: the served read
        of a launch or doctor refuses before the request, so the later
        "no token was sent" refusal is true."""

        from claude_multi import service

        hits: list = []
        handler = type("Handler", (_ModelsHandler,), {"body": b'{"data": []}', "hits": hits})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        gateway = {"gateway": {"base_url": f"http://127.0.0.1:{server.server_port}", "health_path": "/healthz"}}
        same_user = lambda _base: service.OwnerVerdict("unknown", "another process of yours (unit ownership unconfirmed)")
        for read in (launch.served_snapshot, launch.served_models):
            with self.subTest(read=read.__name__):
                with self.assertRaisesRegex(launch.GatewayOwnerError, "gateway token was not sent"):
                    read(gateway, "secret-fixture", owner_check=same_user)
        self.assertEqual(hits, [])


class _ModelsHandler(BaseHTTPRequestHandler):
    body = b"{}"
    hits: list = []

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:
        type(self).hits.append((self.path, self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)


class ServedSnapshotTests(unittest.TestCase):
    """One /v1/models read keeps bounded metadata;
    ``served_models`` stays its id projection; old id-set callbacks work."""

    OURS = staticmethod(lambda _base: service.OwnerVerdict("ours", "fixture"))

    def serve(self, payload) -> tuple[dict, list]:
        hits: list = []
        handler = type("Handler", (_ModelsHandler,), {"body": json.dumps(payload).encode(), "hits": hits})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return {"gateway": {"base_url": f"http://127.0.0.1:{server.server_port}", "health_path": "/healthz"}}, hits

    def test_one_read_keeps_validated_id_owner_and_created(self) -> None:
        gateway, hits = self.serve({"data": [
            {"id": "wire-b", "owned_by": "anthropic", "created": 1790000000},
            {"id": "wire-a", "owned_by": "x" * 80, "created": True},
            {"id": "wire-a", "owned_by": "dup"},
            {"id": "bad id", "owned_by": "openai"},
            {"id": 7}, "junk",
            {"id": "wire-c", "owned_by": "evil\u001b[31m", "created": -5},
        ]})
        models, status = launch.served_snapshot(gateway, "t" * 64, owner_check=self.OURS)
        self.assertEqual(status, 200)
        self.assertEqual(models, (
            launch.ServedModel("wire-a", None, None),
            launch.ServedModel("wire-b", "anthropic", 1790000000),
            launch.ServedModel("wire-c", None, None),
        ))
        self.assertEqual(len(hits), 1)
        ids, status = launch.served_models(gateway, "t" * 64, owner_check=self.OURS)
        self.assertEqual((ids, status), ({"wire-a", "wire-b", "wire-c"}, 200))

    def test_old_id_set_callbacks_are_supported_without_metadata(self) -> None:
        gateway = {"gateway": {"base_url": "http://127.0.0.1:8333"}}
        old = mock.Mock(return_value=(200, {"sel-b", "sel-a"}))
        models, status = launch.served_snapshot(gateway, "t" * 64, models_get=old, owner_check=self.OURS)
        self.assertEqual(models, (launch.ServedModel("sel-a"), launch.ServedModel("sel-b")))
        new = mock.Mock(return_value=(200, (launch.ServedModel("sel-c", "openai", 5),)))
        self.assertEqual(launch.served_snapshot(gateway, "t" * 64, models_get=new, owner_check=self.OURS),
                         ((launch.ServedModel("sel-c", "openai", 5),), 200))
        self.assertEqual(launch.served_models(gateway, "t" * 64, models_get=new, owner_check=self.OURS),
                         ({"sel-c"}, 200))
        down = mock.Mock(return_value=(401, set()))
        self.assertEqual(launch.served_snapshot(gateway, "t" * 64, models_get=down, owner_check=self.OURS),
                         (None, 401))

    def test_runtime_reads_one_snapshot_through_the_seam(self) -> None:
        from claude_multi import cli

        root = Path(tempfile.mkdtemp(prefix="claude-multi-served-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        calls = []

        def served(gateway, token):
            calls.append(token)
            return {"sel-a"}, 200

        runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ={"HOME": str(root / "home"),
                                                                "XDG_CONFIG_HOME": str(root / "c"),
                                                                "XDG_STATE_HOME": str(root / "s")},
                              cwd=root, served_models_callback=served, health_get=lambda *_: 200,
                              listener_owner=self.OURS)
        self.assertEqual(runtime.served_snapshot("t" * 64), ((launch.ServedModel("sel-a"),), 200))
        self.assertEqual(calls, ["t" * 64])


class ServedReadOwnershipTests(unittest.TestCase):
    """The Runtime's served read (launch planning, readiness, doctor) needs
    the gateway proven before the token is sent."""

    def test_runtime_served_read_refuses_an_unproven_listener(self) -> None:
        from claude_multi import cli

        root = Path(tempfile.mkdtemp(prefix="claude-multi-served-owner-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        callback = mock.Mock(return_value=({"sel-a"}, 200))
        runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ={"HOME": str(root / "home"),
                                                                "XDG_CONFIG_HOME": str(root / "c"),
                                                                "XDG_STATE_HOME": str(root / "s")},
                              cwd=root, served_models_callback=callback, health_get=lambda *_: 200,
                              listener_owner=lambda _base: service.OwnerVerdict(
                                  "unknown", "another process of yours (unit ownership unconfirmed)"))
        with self.assertRaises(launch.GatewayOwnerError):
            runtime.served_snapshot("t" * 64)
        callback.assert_not_called()
        self.assertTrue(any("not proven to be the claude-multi gateway" in line
                            for line in runtime.gateway_attention), runtime.gateway_attention)
        observed = runtime.readiness_observations([], served=True)
        self.assertIsNone(observed.served)
        callback.assert_not_called()

    def test_runtime_reload_check_uses_the_runtimes_own_verdict(self) -> None:
        from claude_multi import cli, proxy

        root = Path(tempfile.mkdtemp(prefix="claude-multi-reload-owner-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        verdict = lambda _base: service.OwnerVerdict("ours", "fixture gateway")
        runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ={"HOME": str(root / "home"),
                                                                "XDG_CONFIG_HOME": str(root / "c"),
                                                                "XDG_STATE_HOME": str(root / "s")},
                              cwd=root, listener_owner=verdict)
        endpoint.write_config(root / "home", endpoint.EndpointConfig(port=18317))
        with mock.patch.object(proxy, "gateway_api_keys", return_value=["t" * 64]), \
                mock.patch.object(proxy, "await_sentinel", return_value="reloaded") as waited:
            self.assertEqual(runtime.verify_reload("claude-multi-render-0123abcd"), "reloaded")
        self.assertIs(waited.call_args.kwargs["owner_check"], runtime.listener_owner)


class SharedOwnerPolicyTests(unittest.TestCase):
    def test_check_listener_delegates_to_service_policy(self):
        verdict = service.OwnerVerdict("ours", "fixture")
        with mock.patch.object(service, "owner_policy", return_value="refuse") as policy:
            with self.assertRaises(launch.GatewayOwnerError):
                launch.check_listener("http://127.0.0.1:1234", owner_check=lambda _: verdict)
        policy.assert_called_once_with(verdict)
