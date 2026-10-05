"""Tests for the Phase-1B disposable no-provider probe harness.

Every safety refusal and the fake loopback endpoint are exercised
deterministically. These tests never execute the installed Claude binary,
never contact a real provider, never read user transcripts, and never
contact, restart, or stop the live daemon. Native execution is tested only
against fake executables created inside each test's private scratch tree.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import http.server
import io
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

from claude_multi import dev, pin, probe, state, strict_json
from claude_multi.probe import ProbeError


_FAKE_CLAUDE_BODY = """import http.client
import json
import os
import urllib.parse

env = os.environ
print("HOME=" + env.get("HOME", ""))
print("CWD=" + os.getcwd())
print("CLAUDE_CONFIG_DIR=" + env.get("CLAUDE_CONFIG_DIR", ""))
print("AMBIENT=" + env.get("PROBE_AMBIENT_MARKER", "absent"))
print("AUTO_WINDOW=" + env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "absent"))
print("AUTO_PERCENT=" + env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "absent"))
print("MAX_CONTEXT=" + env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "absent"))
url = urllib.parse.urlsplit(env["ANTHROPIC_BASE_URL"])
body = json.dumps({
    "model": "probe-model",
    "max_tokens": 8,
    "messages": [{"role": "user", "content": "probe-prompt-content"}],
}).encode("utf-8")
connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
connection.request(
    "POST",
    "/v1/messages",
    body=body,
    headers={"Content-Type": "application/json", "x-api-key": env["ANTHROPIC_AUTH_TOKEN"]},
)
response = connection.getresponse()
print("STATUS=" + str(response.status))
print("BODY=" + response.read().decode("utf-8"))
"""

_PTY_BODY = """import os
import sys

print("READY", flush=True)
first = input()
print("ACK=" + first, flush=True)
second = input()
print("DONE=" + second, flush=True)
"""

_ENV_CLIENT_BODY = """import http.client
import json
import os
import shutil
import subprocess
import urllib.parse

env = os.environ
print("TOKEN=" + ("present" if "ANTHROPIC_AUTH_TOKEN" in env else "absent"))
print("PATH=" + env.get("PATH", ""))
print("HINTS=" + env.get("CLAUDE_CODE_GATEWAY_HINT_HEADERS", "absent"))
print("SUBAGENT=" + env.get("CLAUDE_CODE_SUBAGENT_MODEL", "absent"))
print("TTL=" + env.get("CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "absent"))
launcher = shutil.which("claude-multi")
print("LAUNCHER=" + (launcher or "absent"))
if launcher:
    subprocess.run([launcher, "lineup", "--probe-arg=x y"], check=True)
url = urllib.parse.urlsplit(env["ANTHROPIC_BASE_URL"])
headers = {"Content-Type": "application/json"}
if "ANTHROPIC_AUTH_TOKEN" in env:
    headers["x-api-key"] = env["ANTHROPIC_AUTH_TOKEN"]
body = json.dumps({"model": "probe-model", "max_tokens": 8, "messages": []})
connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
connection.request("POST", "/v1/messages", body=body.encode("utf-8"), headers=headers)
response = connection.getresponse()
response.read()
print("STATUS=" + str(response.status))
"""

_PTY_FILE_BODY = """import sys

path = sys.argv[1]
print("READY", flush=True)
input()
print("SEEN=" + open(path).read().strip(), flush=True)
input()
print("SEEN2=" + open(path).read().strip(), flush=True)
"""

_SPAWNER_BODY = """import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
with open(sys.argv[1], "w") as handle:
    handle.write(str(child.pid))
time.sleep(30)
"""


class ProbeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-probe-test-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(self._cleanup)
        # Simulated "live" environment roots; the fixture must stay disjoint.
        live = self.root / "live"
        self.environ = {
            "HOME": str(live / "home"),
            "PATH": "/usr/bin:/bin",
        }
        self.fixture_root = self.root / "fixture"

    def _cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _live(self, *parts: str) -> Path:
        return Path(self.environ["HOME"]).joinpath(*parts)

    def _flags(self) -> dict:
        return {
            "allow-local-claude": True,
            "fixture-root": str(self.fixture_root),
        }

    def _write_fake_executable(self, name: str = "fake-claude", body: str = _FAKE_CLAUDE_BODY) -> Path:
        script = self.root / "bin" / name
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(f"#!{sys.executable}\n" + body)
        script.chmod(0o700)
        return script

    def _trusted(
        self, script: Path, *, fake: bool = True, digest: str | None = None
    ) -> probe.TrustedExecutable:
        return probe.TrustedExecutable(
            script,
            digest
            if digest is not None
            else hashlib.sha256(script.read_bytes()).hexdigest(),
            fake=fake,
        )

    def _installed(self, script: Path, version: str = "2.1.217") -> Path:
        """The script as a native install of ``version`` under this HOME."""

        installed = Path(self.environ["HOME"]) / ".local/share/claude/versions" / version
        installed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script, installed)
        installed.chmod(0o700)
        return installed

    def _contract(self, script: Path, *, digest: str | None = None, version: str = "2.1.217") -> dict:
        self._installed(script, version)
        return {"verified": [{
            "version": version,
            "platforms": {pin.host_platform(): {
                "sha256": digest if digest is not None else hashlib.sha256(script.read_bytes()).hexdigest(),
                "size": script.stat().st_size,
            }},
        }]}

    def _contract_file(self, script: Path, *, digest: str | None = None) -> Path:
        path = self.root / "native-contract.json"
        path.write_bytes(strict_json.canonical_file_bytes(self._contract(script, digest=digest)))
        return path

    def _post(
        self,
        port: int,
        body: bytes,
        headers: dict | None = None,
        path: str = "/v1/messages",
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            path,
            body=body,
            headers=headers or {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload

    def _get(self, port: int, path: str) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload

    def _raw_request(self, port: int, request: bytes) -> tuple[int, bytes]:
        """Raw HTTP/1.1 exchange for malformed requests http.client cannot send.

        Only used for fail-closed error paths where the server closes the
        connection after its deterministic error response.
        """

        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(request)
            chunks = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
        raw = b"".join(chunks)
        status = int(raw.split(b" ", 2)[1])
        body = raw.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in raw else b""
        return status, body


class GatingTests(ProbeTestCase):
    def test_missing_allow_local_claude_refused(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = probe.probe_cli(
                ["init"], {"fixture-root": str(self.fixture_root)}, [], environ=self.environ
            )
        self.assertEqual(code, 2)
        self.assertIn("--allow-local-claude", stderr.getvalue())

    def test_missing_fixture_root_refused(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = probe.probe_cli(
                ["init"], {"allow-local-claude": True}, [], environ=self.environ
            )
        self.assertEqual(code, 2)
        self.assertIn("--fixture-root", stderr.getvalue())

    def test_flag_shaped_fixture_root_refused(self) -> None:
        # `--fixture-root --allow-local-claude` parses fixture-root as True.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = probe.probe_cli(
                ["init"],
                {"allow-local-claude": True, "fixture-root": True},
                [],
                environ=self.environ,
            )
        self.assertEqual(code, 2)
        self.assertIn("--fixture-root", stderr.getvalue())

    def test_unknown_action_refused(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = probe.probe_cli(
                ["dance"], self._flags(), [], environ=self.environ
            )
        self.assertEqual(code, 2)
        self.assertIn("unknown probe action", stderr.getvalue())
        self.assertIn("'init' or 'run'", stderr.getvalue())

    def test_dev_main_dispatches_probe(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = dev.main(["probe", "init", "--fixture-root", str(self.fixture_root)])
        self.assertEqual(code, 2)
        self.assertIn("--allow-local-claude", stderr.getvalue())

    def test_dev_main_probe_creates_no_live_draft_state(self) -> None:
        live_state = self.root / "xdg-live"
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(live_state)}):
            with contextlib.redirect_stderr(io.StringIO()):
                code = dev.main(["probe", "init"])
        self.assertEqual(code, 2)
        self.assertFalse((live_state / "claude-multi" / "drafts").exists())


class CredentialRefusalTests(ProbeTestCase):
    def test_refuses_each_provider_credential_shape(self) -> None:
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "OPENAI_API_KEY",
            "KIMI_API_KEY",
            "MOONSHOT_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "AZURE_CLIENT_SECRET",
            "GROQ_API_KEY",
            "XAI_API_KEY",
            "HF_TOKEN",
            "HUGGINGFACE_API_KEY",
            "OPENROUTER_API_KEY",
            "LITELLM_API_KEY",
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ProbeError, name):
                    probe.assert_no_provider_credentials(
                        {"HOME": "/x", name: "dummy-secret-value"}
                    )

    def test_refusal_names_variables_never_values(self) -> None:
        with self.assertRaises(ProbeError) as raised:
            probe.assert_no_provider_credentials(
                {"ANTHROPIC_API_KEY": "dummy-secret-value"}
            )
        self.assertIn("ANTHROPIC_API_KEY", str(raised.exception))
        self.assertNotIn("dummy-secret-value", str(raised.exception))

    def test_benign_environment_passes(self) -> None:
        probe.assert_no_provider_credentials(
            {"HOME": "/x", "PATH": "/usr/bin", "EDITOR": "vi", "ANTHROPIC_MODEL": "m"}
        )

    def test_cli_refuses_ambient_credentials_before_any_effect(self) -> None:
        script = self._write_fake_executable()
        ambient = {**self.environ, "ANTHROPIC_API_KEY": "dummy-secret-value"}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = probe.probe_cli(
                ["run"], self._flags(), [str(script)], environ=ambient
            )
        self.assertEqual(code, 2)
        self.assertIn("ANTHROPIC_API_KEY", stderr.getvalue())
        self.assertNotIn("dummy-secret-value", stderr.getvalue())
        self.assertFalse((self.fixture_root / "evidence").exists())


class FixtureRootRefusalTests(ProbeTestCase):
    def test_relative_root_refused(self) -> None:
        with self.assertRaisesRegex(ProbeError, "absolute"):
            probe.build_fixture("relative/fixture", environ=self.environ)

    def test_dotdot_root_refused(self) -> None:
        with self.assertRaisesRegex(ProbeError, r"\.\."):
            probe.build_fixture(self.root / "a" / ".." / "fixture", environ=self.environ)

    def test_symlink_root_refused(self) -> None:
        real = self.root / "real-fixture"
        real.mkdir(mode=0o700)
        link = self.root / "link-fixture"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ProbeError, "symlink"):
            probe.build_fixture(link, environ=self.environ)

    def test_symlink_ancestor_refused(self) -> None:
        real = self.root / "real-parent"
        real.mkdir(mode=0o700)
        link = self.root / "link-parent"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ProbeError, "symlink"):
            probe.build_fixture(link / "child", environ=self.environ)

    def test_existing_non_private_root_refused(self) -> None:
        public = self.root / "public-fixture"
        public.mkdir(mode=0o755)
        os.chmod(public, 0o755)
        with self.assertRaisesRegex(ProbeError, "private"):
            probe.build_fixture(public, environ=self.environ)

    def test_existing_file_root_refused(self) -> None:
        file_root = self.root / "file-fixture"
        file_root.write_text("x")
        with self.assertRaisesRegex(ProbeError, "private"):
            probe.build_fixture(file_root, environ=self.environ)

    def test_live_home_refused(self) -> None:
        with self.assertRaisesRegex(ProbeError, "live HOME"):
            probe.build_fixture(self._live(), environ=self.environ)

    def test_live_xdg_config_refused(self) -> None:
        with self.assertRaisesRegex(ProbeError, "live XDG_CONFIG_HOME"):
            probe.build_fixture(self._live(".config"), environ=self.environ)

    def test_live_claude_config_refused(self) -> None:
        with self.assertRaisesRegex(ProbeError, "live Claude config root"):
            probe.build_fixture(self._live(".claude"), environ=self.environ)

    def test_inside_live_claude_config_refused(self) -> None:
        with self.assertRaisesRegex(ProbeError, "inside the live Claude config root"):
            probe.build_fixture(self._live(".claude", "probe"), environ=self.environ)

    def test_ancestor_of_live_home_refused(self) -> None:
        with self.assertRaisesRegex(ProbeError, "contains the live"):
            probe.build_fixture(self.root / "live", environ=self.environ)

    def test_live_state_root_refused(self) -> None:
        with self.assertRaisesRegex(ProbeError, "live claude-multi state root"):
            probe.build_fixture(
                self._live(".local", "state", "claude-multi"), environ=self.environ
            )

    def test_inside_live_state_root_refused(self) -> None:
        with self.assertRaisesRegex(
            ProbeError, "inside the live claude-multi state root"
        ):
            probe.build_fixture(
                self._live(".local", "state", "claude-multi", "probe"),
                environ=self.environ,
            )

    def test_live_uid_daemon_socket_refused(self) -> None:
        with self.assertRaisesRegex(ProbeError, "live uid-shared daemon socket"):
            probe.build_fixture(
                Path(f"/tmp/cc-daemon-{os.geteuid()}"), environ=self.environ
            )

    def test_ancestor_of_uid_daemon_socket_refused(self) -> None:
        # Live roots only need path computation; point the simulated HOME
        # outside /tmp so the uid-shared daemon socket is the tightest
        # contained live domain.
        environ = {"HOME": "/nonexistent-probe-home", "PATH": "/usr/bin"}
        with self.assertRaisesRegex(
            ProbeError, "contains the live uid-shared daemon socket"
        ):
            probe.build_fixture(Path("/tmp"), environ=environ)

    def test_live_runtime_dir_refused(self) -> None:
        environ = {**self.environ, "XDG_RUNTIME_DIR": str(self.root / "run")}
        with self.assertRaisesRegex(ProbeError, "live XDG_RUNTIME_DIR"):
            probe.build_fixture(self.root / "run", environ=environ)

    def test_live_claude_config_dir_env_refused(self) -> None:
        environ = {**self.environ, "CLAUDE_CONFIG_DIR": str(self.root / "ccd")}
        with self.assertRaisesRegex(ProbeError, "live CLAUDE_CONFIG_DIR"):
            probe.build_fixture(self.root / "ccd", environ=environ)

    def test_inside_live_claude_config_dir_env_refused(self) -> None:
        environ = {**self.environ, "CLAUDE_CONFIG_DIR": str(self.root / "ccd")}
        with self.assertRaisesRegex(ProbeError, "inside the live CLAUDE_CONFIG_DIR"):
            probe.build_fixture(self.root / "ccd" / "inner", environ=environ)

    def test_containing_live_claude_config_dir_env_refused(self) -> None:
        environ = {
            **self.environ,
            "CLAUDE_CONFIG_DIR": str(self.root / "other" / "ccd"),
        }
        with self.assertRaisesRegex(ProbeError, "contains the live CLAUDE_CONFIG_DIR"):
            probe.build_fixture(self.root / "other", environ=environ)

    def test_symlinked_live_home_root_cannot_hide_overlap(self) -> None:
        real_home = self.root / "real-home"
        (real_home / ".claude").mkdir(parents=True, mode=0o700)
        link_home = self.root / "link-home"
        link_home.symlink_to(real_home, target_is_directory=True)
        environ = {"HOME": str(link_home), "PATH": "/usr/bin"}
        with self.assertRaisesRegex(ProbeError, "live Claude config root"):
            probe.build_fixture(real_home / ".claude", environ=environ)

    def test_symlinked_xdg_state_root_cannot_hide_overlap(self) -> None:
        real_state = self.root / "real-state"
        real_state.mkdir(mode=0o700)
        link_state = self.root / "link-state"
        link_state.symlink_to(real_state, target_is_directory=True)
        environ = {**self.environ, "XDG_STATE_HOME": str(link_state)}
        with self.assertRaisesRegex(ProbeError, "live claude-multi state root"):
            probe.build_fixture(real_state / "claude-multi", environ=environ)


class BuildFixtureTests(ProbeTestCase):
    def test_directories_created_private(self) -> None:
        fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        for path in (
            fixture.root,
            fixture.home,
            fixture.project_dir,
            fixture.xdg_config_home,
            fixture.xdg_state_home,
            fixture.xdg_cache_home,
            fixture.xdg_data_home,
            fixture.xdg_runtime_dir,
            fixture.claude_config_dir,
        ):
            with self.subTest(path=path):
                info = os.lstat(path)
                self.assertTrue(stat.S_ISDIR(info.st_mode))
                self.assertFalse(stat.S_ISLNK(info.st_mode))
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
                self.assertEqual(info.st_uid, os.geteuid())

    def test_environ_without_provider_has_zero_tokens(self) -> None:
        fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        env = fixture.environ()
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_environ_layout_is_disposable(self) -> None:
        fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        env = fixture.environ()
        for key in (
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_RUNTIME_DIR",
            "CLAUDE_CONFIG_DIR",
        ):
            with self.subTest(key=key):
                self.assertTrue(env[key].startswith(str(fixture.root) + os.sep))
        self.assertNotEqual(env["CLAUDE_CONFIG_DIR"], str(self._live(".claude")))
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")

    def test_environ_with_provider_uses_dummy_token(self) -> None:
        fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        env = fixture.environ(base_url="http://127.0.0.1:8317")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8317")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], probe.DUMMY_TOKEN)

    def test_environ_rejects_non_loopback_base_url(self) -> None:
        fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        with self.assertRaisesRegex(ProbeError, "loopback"):
            fixture.environ(base_url="http://10.0.0.9:8317")

    def test_environ_accepts_only_typed_compaction_controls(self) -> None:
        fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        policy = probe.ProbeCompactionPolicy(
            auto_compact_window=983616,
            auto_compact_percent=90,
            max_context_tokens=372000,
        )
        env = fixture.environ(compaction=policy)
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "983616")
        self.assertEqual(env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "90")
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "372000")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_invalid_compaction_controls_fail_closed(self) -> None:
        fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        for policy, needle in (
            (probe.ProbeCompactionPolicy(0, 90), "between 100000 and 1000000"),
            (probe.ProbeCompactionPolicy(100000, 0), "percentage must be 1..100"),
            (probe.ProbeCompactionPolicy(100000, 90, 0), "max context tokens"),
        ):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(ProbeError, needle):
                    fixture.environ(compaction=policy)

    def test_environ_path_is_explicit_without_cwd_entry(self) -> None:
        fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        env = fixture.environ()
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertTrue(all(env["PATH"].split(":")))

    def test_reentry_reuses_existing_fixture(self) -> None:
        first = probe.build_fixture(self.fixture_root, environ=self.environ)
        again = probe.build_fixture(self.fixture_root, environ=self.environ)
        self.assertEqual(again, first)


class FixtureProjectTrustTests(ProbeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        self.config = self.fixture.claude_config_dir / ".claude.json"

    def test_seed_shape_is_exact_and_private(self) -> None:
        path = probe.seed_fixture_project_trust(
            self.fixture, self.fixture.project_dir
        )
        self.assertEqual(path, self.config)
        document = strict_json.loads(state.read_private(path))
        project_key = os.path.realpath(self.fixture.project_dir)
        self.assertEqual(
            document,
            {"projects": {project_key: {"hasTrustDialogAccepted": True}}},
        )
        self.assertNotIn(str(self.fixture.home), document["projects"])
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)

    def test_seed_preserves_other_keys_and_is_idempotent(self) -> None:
        project_key = os.path.realpath(self.fixture.project_dir)
        original = {
            "theme": "dark",
            "projects": {
                project_key: {"allowedTools": ["Read"]},
                "/other/existing/project": {"hasTrustDialogAccepted": False},
            },
        }
        state.atomic_write(self.config, strict_json.canonical_file_bytes(original))
        probe.seed_fixture_project_trust(self.fixture, self.fixture.project_dir)
        first = self.config.read_bytes()
        probe.seed_fixture_project_trust(self.fixture, self.fixture.project_dir)
        self.assertEqual(self.config.read_bytes(), first)
        document = strict_json.loads(first)
        self.assertEqual(document["theme"], "dark")
        self.assertEqual(document["projects"][project_key]["allowedTools"], ["Read"])
        self.assertTrue(document["projects"][project_key]["hasTrustDialogAccepted"])
        self.assertEqual(
            document["projects"]["/other/existing/project"],
            {"hasTrustDialogAccepted": False},
        )

    def test_external_project_is_refused_without_write(self) -> None:
        outside = state.ensure_private_dir(self.root / "outside-project")
        with self.assertRaisesRegex(ProbeError, "not inside fixture root"):
            probe.seed_fixture_project_trust(self.fixture, outside)
        self.assertFalse(self.config.exists())

    def test_external_config_root_is_refused_without_write(self) -> None:
        outside = state.ensure_private_dir(self.root / "outside-config")
        variant = replace(self.fixture, claude_config_dir=outside)
        with self.assertRaisesRegex(ProbeError, "not inside fixture root"):
            probe.seed_fixture_project_trust(variant, variant.project_dir)
        self.assertFalse((outside / ".claude.json").exists())

    def test_symlink_project_is_refused(self) -> None:
        link = self.fixture.home / "linked-project"
        link.symlink_to(self.fixture.project_dir, target_is_directory=True)
        with self.assertRaisesRegex(ProbeError, "symlink"):
            probe.seed_fixture_project_trust(self.fixture, link)
        self.assertFalse(self.config.exists())

    def test_symlink_config_is_refused(self) -> None:
        target = self.fixture.root / "config-target.json"
        state.atomic_write(target, strict_json.canonical_file_bytes({}))
        self.config.symlink_to(target)
        with self.assertRaisesRegex(ProbeError, "symlink"):
            probe.seed_fixture_project_trust(
                self.fixture, self.fixture.project_dir
            )
        self.assertEqual(strict_json.loads(target.read_bytes()), {})

    def test_malformed_existing_config_is_refused_without_overwrite(self) -> None:
        state.atomic_write(self.config, b"{not-json\n")
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ProbeError, "unsafe or malformed"):
            probe.seed_fixture_project_trust(
                self.fixture, self.fixture.project_dir
            )
        self.assertEqual(self.config.read_bytes(), before)

    def test_unexpected_existing_config_shapes_are_refused(self) -> None:
        project_key = os.path.realpath(self.fixture.project_dir)
        for document in (
            [],
            {"projects": None},
            {"projects": []},
            {"projects": {project_key: None}},
            {"projects": {project_key: []}},
        ):
            with self.subTest(document=document):
                state.atomic_write(
                    self.config, strict_json.canonical_file_bytes(document)
                )
                before = self.config.read_bytes()
                with self.assertRaisesRegex(ProbeError, "must be a JSON object"):
                    probe.seed_fixture_project_trust(
                        self.fixture, self.fixture.project_dir
                    )
                self.assertEqual(self.config.read_bytes(), before)


class LoopbackTests(ProbeTestCase):
    def test_accepts_loopback_literal(self) -> None:
        self.assertEqual(
            probe.check_loopback_url("http://127.0.0.1:8317"),
            "http://127.0.0.1:8317",
        )

    def test_refuses_non_loopback(self) -> None:
        for url in (
            "http://localhost:8317",
            "http://[::1]:8317",
            "http://10.0.0.9:8317",
            "http://192.168.1.5:8317",
            "http://api.example.com:8317",
            "https://127.0.0.1:8317",
            "http://127.0.0.1",
            "http://127.0.0.1:not-a-port",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ProbeError):
                    probe.check_loopback_url(url)


class FakeProviderTests(ProbeTestCase):
    def test_constructor_refuses_non_loopback_host(self) -> None:
        for host in ("0.0.0.0", "10.0.0.9", "example.com", "::1"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ProbeError, "loopback"):
                    probe.FakeAnthropicProvider(host=host)

    def test_base_url_requires_start(self) -> None:
        provider = probe.FakeAnthropicProvider()
        with self.assertRaisesRegex(ProbeError, "not started"):
            _ = provider.base_url

    def test_healthz_and_unknown_get(self) -> None:
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, payload = self._get(port, "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(strict_json.loads(payload), {"status": "ok"})
            status, payload = self._get(port, "/v1/models")
            self.assertEqual(status, 200)
            models = strict_json.loads(payload)
            self.assertIn("gpt-multi-sol-high", [item["id"] for item in models["data"]])
            self.assertFalse(models["has_more"])
            status, payload = self._get(port, "/nope")
            self.assertEqual(status, 404)
            self.assertEqual(
                strict_json.loads(payload)["error"]["type"], "not_found_error"
            )
            # GET endpoints are not probe traffic: nothing is recorded.
            self.assertEqual(provider.requests, ())

    def test_messages_text_response_and_record(self) -> None:
        body = (
            b'{"model":"probe-model","max_tokens":8,'
            b'"messages":[{"role":"user","content":"probe-prompt-content"}]}'
        )
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, payload = self._post(port, body)
            self.assertEqual(status, 200)
            reply = strict_json.loads(payload)
            self.assertEqual(reply["content"], [{"type": "text", "text": "PROBE-OK"}])
            self.assertEqual(reply["model"], "probe-model")
            self.assertEqual(reply["stop_reason"], "end_turn")
            self.assertEqual(len(provider.requests), 1)
            record = provider.requests[0]
            self.assertEqual(record.method, "POST")
            self.assertEqual(record.path, "/v1/messages")
            self.assertEqual(record.model, "probe-model")
            self.assertEqual(record.tool_names, ())
            self.assertEqual(record.auth, "absent")
            self.assertEqual(record.body_sha256, hashlib.sha256(body).hexdigest())

    def test_request_query_is_hashed_in_metadata(self) -> None:
        body = b'{"model":"probe-model","messages":[]}'
        target = "/v1/messages?token=query-secret"
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, _payload = self._post(port, body, path=target)
            self.assertEqual(status, 200)
            record = provider.requests[0]
            self.assertRegex(record.path, r"^sha256:[0-9a-f]{64}:bytes=\d+$")
            self.assertNotIn("query-secret", record.path)

    def test_compaction_responder_exposes_one_near_limit_turn(self) -> None:
        responder = probe.CompactionResponder(near_limit_tokens=900)
        status, payload = responder({}, "/v1/messages/count_tokens")
        self.assertEqual((status, payload), (200, {"input_tokens": 1}))
        status, payload = responder(
            {"model": "probe-model", "stream": False}, "/v1/messages"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["usage"]["input_tokens"], 900)
        status, payload = responder({}, "/v1/messages/count_tokens")
        self.assertEqual((status, payload), (200, {"input_tokens": 900}))
        status, payload = responder(
            {"model": "probe-model", "stream": False}, "/v1/messages"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["usage"]["input_tokens"], 1)
        status, payload = responder({}, "/v1/messages/count_tokens")
        self.assertEqual((status, payload), (200, {"input_tokens": 1}))
        self.assertEqual(responder.message_requests, 2)
        self.assertEqual(responder.count_token_requests, 3)

    def test_automatic_compaction_responder_uses_cache_aware_phases(self) -> None:
        responder = probe.AutomaticCompactionResponder()
        main = {
            "model": "probe-model",
            "stream": False,
            "tools": [{"name": "Agent"}],
        }
        for _ordinal in (1, 2):
            status, payload = responder(main, "/v1/messages")
            self.assertEqual(status, 200)
            self.assertEqual(payload["content"][0]["text"], "PROBE-OK")
        status, payload = responder(main, "/v1/messages")
        self.assertEqual(status, 200)
        self.assertEqual(
            sum(
                payload["usage"][key]
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "output_tokens",
                )
            ),
            150000,
        )
        status, auxiliary = responder(
            {"model": "probe-model", "stream": False}, "/v1/messages"
        )
        self.assertEqual(status, 200)
        self.assertEqual(auxiliary["content"][0]["text"], "PROBE-AUX")
        status, payload = responder(main, "/v1/messages")
        self.assertEqual(status, 200)
        self.assertEqual(
            sum(
                payload["usage"][key]
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "output_tokens",
                )
            ),
            165000,
        )
        responder(main, "/v1/messages")
        self.assertEqual(responder.message_requests, 6)
        self.assertEqual(responder.main_requests, 5)
        self.assertEqual(responder.auxiliary_requests, 1)

    def test_record_never_retains_prompt_content(self) -> None:
        body = (
            b'{"model":"probe-model","max_tokens":8,"system":"probe-system-content",'
            b'"messages":[{"role":"user","content":"probe-prompt-content"}]}'
        )
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            self._post(port, body)
            record = provider.requests[0]
            self.assertTrue(record.has_system)
            messages = [{"role": "user", "content": "probe-prompt-content"}]
            self.assertEqual(
                record.system_json_bytes,
                len(strict_json.canonical_bytes("probe-system-content")),
            )
            self.assertEqual(
                record.messages_json_bytes,
                len(strict_json.canonical_bytes(messages)),
            )
            self.assertEqual(record.message_count, 1)
            self.assertEqual(
                record.message_sha256s,
                (
                    strict_json.sha256_hex(
                        strict_json.canonical_bytes(messages[0])
                    ),
                ),
            )
            self.assertFalse(record.message_hashes_truncated)
            rendered = repr(record)
            self.assertNotIn("probe-prompt-content", rendered)
            self.assertNotIn("probe-system-content", rendered)

    def test_record_bounds_message_hash_metadata(self) -> None:
        messages = [
            {"role": "user", "content": f"message-{index}"}
            for index in range(70)
        ]
        body = strict_json.canonical_bytes(
            {"model": "probe-model", "max_tokens": 8, "messages": messages}
        )
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            self._post(port, body)
            record = provider.requests[0]
        self.assertEqual(record.message_count, 70)
        self.assertEqual(len(record.message_sha256s), 64)
        self.assertTrue(record.message_hashes_truncated)
        self.assertNotIn("message-0", repr(record))

    def test_record_bounds_and_hashes_request_controlled_identifiers(self) -> None:
        secret = "prompt-like-secret-" * 32
        tools = [
            {"name": secret if index == 0 else f"tool-{index}"}
            for index in range(70)
        ]
        body = strict_json.canonical_bytes(
            {
                "model": secret,
                "max_tokens": 8,
                "messages": [],
                "tools": tools,
            }
        )
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            self._post(port, body)
            record = provider.requests[0]
        self.assertTrue(record.model.startswith("sha256:"))
        self.assertEqual(record.tool_count, 70)
        self.assertEqual(len(record.tool_names), 64)
        self.assertTrue(record.tool_names_truncated)
        self.assertTrue(record.tool_names[0].startswith("sha256:"))
        self.assertNotIn(secret, repr(record))

    def test_auth_classification(self) -> None:
        body = b'{"model":"probe-model","max_tokens":8,"messages":[]}'
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            self._post(port, body, headers={"x-api-key": probe.DUMMY_TOKEN})
            self._post(
                port,
                body,
                headers={"Authorization": f"Bearer {probe.DUMMY_TOKEN}"},
            )
            self._post(port, body, headers={"x-api-key": "dummy-wrong-token"})
            auths = [record.auth for record in provider.requests]
            self.assertEqual(auths, ["dummy", "dummy", "other"])

    def test_invalid_json_body_deterministic_400_and_recorded(self) -> None:
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, payload = self._post(port, b"{not json")
            self.assertEqual(status, 400)
            self.assertEqual(
                strict_json.loads(payload)["error"]["type"], "invalid_request_error"
            )
            self.assertEqual(len(provider.requests), 1)
            self.assertIsNone(provider.requests[0].model)

    def test_tool_use_echo_for_registry_and_deny_probes(self) -> None:
        body = strict_json.canonical_bytes(
            {
                "model": "probe-model",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "x"}],
                "tools": [
                    {"name": "probe_tool_alpha", "input_schema": {"type": "object"}},
                    {"name": "probe_tool_beta", "input_schema": {"type": "object"}},
                ],
                "tool_choice": {"type": "any"},
            }
        )
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, payload = self._post(port, body)
            self.assertEqual(status, 200)
            reply = strict_json.loads(payload)
            self.assertEqual(reply["stop_reason"], "tool_use")
            self.assertEqual(
                reply["content"],
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_probe_0001",
                        "name": "probe_tool_alpha",
                        "input": {},
                    }
                ],
            )
            self.assertEqual(
                provider.requests[0].tool_names,
                ("probe_tool_alpha", "probe_tool_beta"),
            )

    def test_forced_tool_choice_honored(self) -> None:
        body = strict_json.canonical_bytes(
            {
                "model": "probe-model",
                "max_tokens": 8,
                "messages": [],
                "tools": [
                    {"name": "probe_tool_alpha"},
                    {"name": "probe_tool_beta"},
                ],
                "tool_choice": {"type": "tool", "name": "probe_tool_beta"},
            }
        )
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            _, payload = self._post(port, body)
            reply = strict_json.loads(payload)
            self.assertEqual(reply["content"][0]["name"], "probe_tool_beta")

    def test_responses_are_byte_deterministic(self) -> None:
        body = b'{"model":"probe-model","max_tokens":8,"messages":[]}'
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            first = self._post(port, body)
            second = self._post(port, body)
            self.assertEqual(first, second)

    def test_request_records_bounded_with_truncation_metadata(self) -> None:
        provider = probe.FakeAnthropicProvider()
        document = {"model": "probe-model", "messages": []}
        headers = {"x-api-key": probe.DUMMY_TOKEN}
        for index in range(probe._RECORD_CAP + 10):
            provider._record("POST", "/v1/messages", headers, b"{}", document)
        self.assertEqual(len(provider.requests), probe._RECORD_CAP)
        self.assertEqual(provider.dropped_requests, 10)

    def test_missing_content_length_deterministic_411(self) -> None:
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, payload = self._raw_request(
                port,
                b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Connection: close\r\n\r\n",
            )
            self.assertEqual(status, 411)
            self.assertEqual(
                strict_json.loads(payload)["error"]["type"], "invalid_request_error"
            )
            self.assertEqual(provider.requests, ())

    def test_non_numeric_content_length_deterministic_400(self) -> None:
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, _ = self._raw_request(
                port,
                b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Length: abc\r\nConnection: close\r\n\r\n",
            )
            self.assertEqual(status, 400)
            self.assertEqual(provider.requests, ())

    def test_negative_content_length_deterministic_400(self) -> None:
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, _ = self._raw_request(
                port,
                b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Length: -5\r\nConnection: close\r\n\r\n",
            )
            self.assertEqual(status, 400)

    def test_conflicting_content_length_deterministic_400(self) -> None:
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, _ = self._raw_request(
                port,
                b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Length: 5\r\nContent-Length: 6\r\n"
                b"Connection: close\r\n\r\n",
            )
            self.assertEqual(status, 400)

    def test_oversize_content_length_deterministic_413(self) -> None:
        with probe.FakeAnthropicProvider() as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, _ = self._raw_request(
                port,
                b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                + f"Content-Length: {probe._MAX_BODY_BYTES + 1}\r\n".encode()
                + b"Connection: close\r\n\r\n",
            )
            self.assertEqual(status, 413)
            self.assertEqual(provider.requests, ())


class TrustedContractTests(ProbeTestCase):
    def test_derives_real_spec_from_a_matching_local_file(self) -> None:
        script = self._write_fake_executable()
        contract = self._contract(script)
        trusted = probe.trusted_from_contract(contract, self.environ)
        self.assertEqual(trusted.resolved_path, self._live(".local/share/claude/versions/2.1.217"))
        self.assertEqual(trusted.sha256, hashlib.sha256(script.read_bytes()).hexdigest())
        self.assertFalse(trusted.fake)

    def test_owned_copy_is_found_when_no_native_install_matches(self) -> None:
        script = self._write_fake_executable()
        contract = self._contract(script)
        self._live(".local/share/claude/versions/2.1.217").unlink()
        owned = pin.owned_path(self.environ, "2.1.217", pin.host_platform())
        state.ensure_private_dir(owned.parent)
        shutil.copy2(script, owned)
        self.assertEqual(probe.trusted_from_contract(contract, self.environ).resolved_path, owned)

    def test_path_claude_and_mismatching_files_never_qualify(self) -> None:
        script = self._write_fake_executable()
        contract = self._contract(script, digest="0" * 64)
        bindir = self.root / "pathbin"
        bindir.mkdir()
        (bindir / "claude").symlink_to(script)
        with self.assertRaisesRegex(ProbeError, "no local file matches Claude Code 2.1.217"):
            probe.trusted_from_contract(contract, {**self.environ, "PATH": str(bindir)})
        contract = self._contract(script)
        self._live(".local/share/claude/versions/2.1.217").unlink()
        with self.assertRaisesRegex(ProbeError, "no local file matches"):
            probe.trusted_from_contract(contract, {**self.environ, "PATH": str(bindir)})

    def test_a_repin_candidate_outside_the_standard_locations_is_found_by_hash(self) -> None:
        script = self._write_fake_executable()
        contract = self._contract(script)
        self._live(".local/share/claude/versions/2.1.217").unlink()
        elsewhere = self.root / "custom-releases" / "2.1.217"
        elsewhere.parent.mkdir()
        shutil.copy2(script, elsewhere)
        handed = {**self.environ, probe.REPIN_CANDIDATE_ENV: str(elsewhere)}
        self.assertEqual(probe.trusted_from_contract(contract, handed).resolved_path, elsewhere)
        # Only a file that matches the contract qualifies.
        elsewhere.write_bytes(b"other bytes")
        with self.assertRaisesRegex(ProbeError, "no local file matches"):
            probe.trusted_from_contract(contract, handed)

    def test_rejects_missing_identity(self) -> None:
        with self.assertRaisesRegex(ProbeError, "pinned Claude Code identity"):
            probe.trusted_from_contract({"claude": {}}, self.environ)

    def test_rejects_malformed_digest(self) -> None:
        contract = {"verified": [{"version": "2.1.217", "platforms": {pin.host_platform(): {"sha256": "zz", "size": 1}}}]}
        with self.assertRaisesRegex(ProbeError, "sha256"):
            probe.trusted_from_contract(contract, self.environ)


class RunNativeTests(ProbeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fixture = probe.build_fixture(self.fixture_root, environ=self.environ)

    def test_refuses_relative_executable(self) -> None:
        with self.assertRaisesRegex(ProbeError, "absolute"):
            probe.run_native(
                [],
                trusted=probe.TrustedExecutable(
                    Path("relative/fake"), "0" * 64, fake=True
                ),
                fixture=self.fixture,
            )

    def test_refuses_non_normal_executable_path(self) -> None:
        script = self._write_fake_executable()
        weird = script.parent / ".." / "bin" / script.name
        with self.assertRaisesRegex(ProbeError, "non-normal"):
            probe.run_native(
                [], trusted=self._trusted(weird), fixture=self.fixture
            )

    def test_refuses_missing_executable(self) -> None:
        missing = self.root / "bin" / "nope"
        with self.assertRaisesRegex(ProbeError, "does not exist"):
            probe.run_native(
                [],
                trusted=probe.TrustedExecutable(missing, "0" * 64, fake=True),
                fixture=self.fixture,
            )

    def test_refuses_directory_executable(self) -> None:
        with self.assertRaisesRegex(ProbeError, "regular file"):
            probe.run_native(
                [],
                trusted=probe.TrustedExecutable(
                    self.fixture.root, "0" * 64, fake=True
                ),
                fixture=self.fixture,
            )

    def test_refuses_non_executable_file(self) -> None:
        plain = self.root / "bin" / "plain"
        plain.parent.mkdir(parents=True, exist_ok=True)
        plain.write_text("x")
        plain.chmod(0o600)
        with self.assertRaisesRegex(ProbeError, "not executable"):
            probe.run_native(
                [], trusted=self._trusted(plain), fixture=self.fixture
            )

    def test_refuses_group_other_writable_executable(self) -> None:
        script = self._write_fake_executable()
        script.chmod(0o775)
        with self.assertRaisesRegex(ProbeError, "writable"):
            probe.run_native(
                [], trusted=self._trusted(script), fixture=self.fixture
            )

    def test_refuses_symlink_executable(self) -> None:
        script = self._write_fake_executable()
        link = self.root / "bin" / "linked-claude"
        link.symlink_to(script)
        with self.assertRaisesRegex(ProbeError, "symlink"):
            probe.run_native(
                [], trusted=self._trusted(link), fixture=self.fixture
            )

    def test_refuses_hash_mismatch(self) -> None:
        script = self._write_fake_executable()
        with self.assertRaisesRegex(ProbeError, "hash"):
            probe.run_native(
                [],
                trusted=self._trusted(script, digest="0" * 64),
                fixture=self.fixture,
            )

    def test_refuses_malformed_spec_digest(self) -> None:
        script = self._write_fake_executable()
        with self.assertRaisesRegex(ProbeError, "sha256"):
            probe.run_native(
                [],
                trusted=self._trusted(script, digest="not-hex"),
                fixture=self.fixture,
            )

    def test_refuses_non_positive_timeout(self) -> None:
        script = self._write_fake_executable()
        with self.assertRaisesRegex(ProbeError, "positive"):
            probe.run_native(
                [], trusted=self._trusted(script), fixture=self.fixture, timeout=0
            )

    def test_real_native_spec_fails_closed_without_allow_real(self) -> None:
        script = self._write_fake_executable()
        with self.assertRaisesRegex(ProbeError, "refusing real native execution without allow_real"):
            probe.run_native(
                [],
                trusted=self._trusted(script, fake=False),
                fixture=self.fixture,
            )

    def test_runs_fake_executable_with_disposable_env(self) -> None:
        script = self._write_fake_executable()
        with mock.patch.dict(os.environ, {"PROBE_AMBIENT_MARKER": "present"}):
            result = probe.run_native(
                [], trusted=self._trusted(script), fixture=self.fixture, timeout=15
            )
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)
        self.assertIn(f"HOME={self.fixture.home}", result.stdout)
        self.assertIn(f"CWD={self.fixture.project_dir}", result.stdout)
        self.assertIn(
            f"CLAUDE_CONFIG_DIR={self.fixture.claude_config_dir}", result.stdout
        )
        # The ambient environment never leaks into the probed process.
        self.assertIn("AMBIENT=absent", result.stdout)
        self.assertIn("STATUS=200", result.stdout)
        self.assertIn("PROBE-OK", result.stdout)
        self.assertEqual(len(result.requests), 1)
        record = result.requests[0]
        self.assertEqual(record.method, "POST")
        self.assertEqual(record.path, "/v1/messages")
        self.assertEqual(record.model, "probe-model")
        self.assertEqual(record.auth, "dummy")

    def test_run_native_threads_typed_compaction_policy(self) -> None:
        script = self._write_fake_executable()
        result = probe.run_native(
            [],
            trusted=self._trusted(script),
            fixture=self.fixture,
            timeout=15,
            compaction=probe.ProbeCompactionPolicy(
                auto_compact_window=983616,
                auto_compact_percent=90,
                max_context_tokens=372000,
            ),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("AUTO_WINDOW=983616", result.stdout)
        self.assertIn("AUTO_PERCENT=90", result.stdout)
        self.assertIn("MAX_CONTEXT=372000", result.stdout)

    def test_run_native_pty_drives_bounded_interactions(self) -> None:
        script = self._write_fake_executable("pty-client", _PTY_BODY)
        result = probe.run_native_pty(
            [],
            (
                probe.PTYInteraction(b"READY", b"hello\n"),
                probe.PTYInteraction(b"ACK=hello", b"exit\n"),
            ),
            trusted=self._trusted(script),
            fixture=self.fixture,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("DONE=exit", result.stdout)
        self.assertEqual(result.requests, ())

    def test_run_native_pty_preserves_markers_after_match(self) -> None:
        script = self._write_fake_executable(
            "pty-buffered-markers",
            """import os

os.write(1, b"READY-NEXT\\n")
first = input()
second = input()
print("DONE=" + first + ":" + second, flush=True)
""",
        )
        result = probe.run_native_pty(
            [],
            (
                probe.PTYInteraction(
                    b"READY", b"hello\n", preserve_after_wait=True
                ),
                probe.PTYInteraction(b"NEXT", b"exit\n"),
            ),
            trusted=self._trusted(script),
            fixture=self.fixture,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("DONE=hello:exit", result.stdout)

    def _pty_timeout_after_send(self, *, wait_again=False):
        script = self._write_fake_executable(
            "pty-hung", "import time\nprint('READY', flush=True)\ninput()\ntime.sleep(60)\n"
        )
        # Advance only the harness clock after the child has started and input
        # was sent. No short startup deadline or scheduler-dependent sleep.
        clock, write = time.monotonic, os.write
        offset = 0

        def now():
            return clock() + offset

        def send(fd, data):
            nonlocal offset
            count = write(fd, data)
            if bytes(data) == b"exit\n":
                offset = 30
            return count

        steps = [probe.PTYInteraction(b"READY", b"exit\n")]
        if wait_again:
            steps.append(probe.PTYInteraction(b"PRIVATE-MISSING-MARKER", b""))
        with mock.patch.object(probe.time, "monotonic", side_effect=now), \
                mock.patch.object(probe.os, "write", side_effect=send), \
                mock.patch.object(probe, "_kill_process_group", wraps=probe._kill_process_group) as kill:
            with self.assertRaises(probe.PTYTimeoutError) as caught:
                probe.run_native_pty(
                    [], steps, trusted=self._trusted(script), fixture=self.fixture, timeout=15,
                )
        kill.assert_called_once()
        return caught.exception, kill.call_args.args[0]

    def test_pty_exit_timeout_reports_exit_phase(self) -> None:
        error, _ = self._pty_timeout_after_send()
        self.assertEqual(error.phase, "client-exit")
        self.assertEqual((error.completed_interactions, error.total_interactions), (1, 1))
        self.assertGreaterEqual(error.elapsed_seconds, 30)
        self.assertIn("client-exit phase", str(error))
        self.assertNotIn("READY", str(error))

    def test_pty_exit_timeout_gets_one_marked_s9_retry(self) -> None:
        from tests.test_client_models import _retry_s9_pty

        error, _ = self._pty_timeout_after_send()
        self.assertEqual(error.phase, "client-exit")
        runner = mock.Mock(side_effect=[error, "ok"])
        self.assertEqual(_retry_s9_pty(runner), ("ok", True))
        self.assertEqual(runner.call_args_list, [mock.call(timeout=240), mock.call(timeout=240)])

    def test_pty_marker_timeout_is_not_an_exit_timeout(self) -> None:
        error, _ = self._pty_timeout_after_send(wait_again=True)
        self.assertEqual(error.phase, "marker")
        self.assertEqual((error.completed_interactions, error.total_interactions), (1, 2))
        self.assertNotIn("PRIVATE-MISSING-MARKER", str(error))
        self.assertNotIn("READY", str(error))

    def test_pty_slow_exit_within_bound_completes(self) -> None:
        script = self._write_fake_executable(
            "pty-slow-exit",
            "import time\nprint('READY', flush=True)\ninput()\ntime.sleep(0.1)\nprint('EXITED', flush=True)\n",
        )
        with mock.patch.object(probe, "_kill_process_group", wraps=probe._kill_process_group) as kill:
            result = probe.run_native_pty(
                [], [probe.PTYInteraction(b"READY", b"exit\n")],
                trusted=self._trusted(script), fixture=self.fixture, timeout=15,
            )
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)
        self.assertIn("EXITED", result.stdout)
        kill.assert_not_called()

    def test_pty_hung_client_is_killed_and_never_passes(self) -> None:
        error, child = self._pty_timeout_after_send()
        self.assertIsInstance(error, ProbeError)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        with self.assertRaises(ProcessLookupError):
            os.kill(child.pid, 0)
        with self.assertRaises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)

    def test_caller_owned_provider_keeps_lifecycle(self) -> None:
        script = self._write_fake_executable()
        with probe.FakeAnthropicProvider() as provider:
            result = probe.run_native(
                [],
                trusted=self._trusted(script),
                fixture=self.fixture,
                provider=provider,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0)
            self.assertTrue(provider.base_url.startswith("http://127.0.0.1:"))
            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(result.requests, provider.requests)

    def test_nonzero_exit_propagates(self) -> None:
        script = self._write_fake_executable("exiter", "import sys\nsys.exit(3)\n")
        result = probe.run_native(
            [], trusted=self._trusted(script), fixture=self.fixture, timeout=15
        )
        self.assertEqual(result.returncode, 3)
        self.assertFalse(result.timed_out)

    def test_timeout_kills_process_group_and_reaps(self) -> None:
        pidfile = self.fixture_root / "grandchild.pid"
        spawner = self._write_fake_executable("spawner", _SPAWNER_BODY)
        result = probe.run_native(
            [str(pidfile)],
            trusted=self._trusted(spawner),
            fixture=self.fixture,
            timeout=0.5,
        )
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        grandchild = int(pidfile.read_text())
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("grandchild survived the process-group kill")
        # Cleanup: the timed-out run left no listener or zombie state behind;
        # a fresh run works deterministically.
        script = self._write_fake_executable()
        followup = probe.run_native(
            [], trusted=self._trusted(script), fixture=self.fixture, timeout=15
        )
        self.assertEqual(followup.returncode, 0)


class EvidenceTests(ProbeTestCase):
    def test_write_evidence_private_strict(self) -> None:
        fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        path = probe.write_evidence(fixture, "p1-sample", {"version": 1, "ok": True})
        self.assertEqual(path.parent, fixture.root / "evidence")
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        self.assertEqual(
            strict_json.loads(path.read_bytes()), {"version": 1, "ok": True}
        )

    def test_write_evidence_validates_name(self) -> None:
        fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        with self.assertRaises(state.StateError):
            probe.write_evidence(fixture, "Bad Name!", {})


class ProbeCliTests(ProbeTestCase):
    def test_init_prints_path_only_manifest(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = probe.probe_cli(["init"], self._flags(), [], environ=self.environ)
        self.assertEqual(code, 0)
        manifest = strict_json.loads(stdout.getvalue())
        self.assertEqual(manifest["root"], str(self.fixture_root))
        self.assertEqual(
            manifest["dirs"]["claude_config_dir"],
            str(self.fixture_root / "claude-config"),
        )
        self.assertEqual(
            manifest["dirs"]["project_dir"],
            str(self.fixture_root / "home" / "project"),
        )
        self.assertNotIn(probe.DUMMY_TOKEN, stdout.getvalue())

    def test_init_rejects_argv_tail(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = probe.probe_cli(
                ["init"], self._flags(), ["/bin/x"], environ=self.environ
            )
        self.assertEqual(code, 2)
        self.assertIn("no executable argv", stderr.getvalue())

    def test_run_evidence_is_metadata_only(self) -> None:
        script = self._write_fake_executable()
        flags = {**self._flags(), "native-contract": str(self._contract_file(script))}
        fake_result = probe.NativeRunResult(
            argv=(str(script), "--distinctive-arg-xyz"),
            returncode=0,
            timed_out=False,
            stdout="distinctive-stdout-xyz",
            stderr="",
            requests=(),
        )
        with mock.patch.object(probe, "run_native", return_value=fake_result):
            with contextlib.redirect_stdout(io.StringIO()):
                code = probe.probe_cli(
                    ["run"], flags, ["--distinctive-arg-xyz"], environ=self.environ
                )
        self.assertEqual(code, 0)
        raw = (self.fixture_root / "evidence" / "last-run.json").read_bytes()
        # Raw argv and stdio text never persist, even on the fake-only path.
        self.assertNotIn(b"distinctive-arg-xyz", raw)
        self.assertNotIn(b"distinctive-stdout-xyz", raw)
        evidence = strict_json.loads(raw)
        self.assertNotIn("argv", evidence["run"])
        self.assertEqual(evidence["run"]["argv_count"], 2)
        self.assertEqual(evidence["run"]["returncode"], 0)
        self.assertTrue(evidence["run"]["stdout_sha256"])

    def test_run_requires_native_contract(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = probe.probe_cli(["run"], self._flags(), [], environ=self.environ)
        self.assertEqual(code, 2)
        self.assertIn("--native-contract", stderr.getvalue())

    def test_run_real_contract_fails_closed_without_consent(self) -> None:
        script = self._write_fake_executable()
        flags = {**self._flags(), "native-contract": str(self._contract_file(script))}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = probe.probe_cli(["run"], flags, [], environ=self.environ)
        self.assertEqual(code, 2)
        self.assertIn("refusing real native execution without allow_real", stderr.getvalue())
        self.assertFalse((self.fixture_root / "evidence").exists())

    def test_run_rejects_malformed_contract(self) -> None:
        bad = self.root / "bad-contract.json"
        bad.write_bytes(strict_json.canonical_file_bytes({"claude": {}}))
        flags = {**self._flags(), "native-contract": str(bad)}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = probe.probe_cli(["run"], flags, [], environ=self.environ)
        self.assertEqual(code, 2)
        self.assertIn("pinned Claude Code identity", stderr.getvalue())

    def test_run_rejects_contract_hash_mismatch(self) -> None:
        script = self._write_fake_executable()
        flags = {
            **self._flags(),
            "native-contract": str(self._contract_file(script, digest="0" * 64)),
        }
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = probe.probe_cli(["run"], flags, [], environ=self.environ)
        self.assertEqual(code, 2)
        # No local file matches a mismatching record: refused before any run.
        self.assertIn("no local file matches", stderr.getvalue())
        self.assertFalse((self.fixture_root / "evidence").exists())

    def test_dev_main_run_fails_closed_without_consent(self) -> None:
        script = self._write_fake_executable()
        contract = self._contract_file(script)
        # Scrub the ambient environment exactly as the gated operator
        # invocation requires; probe_cli reads os.environ by default.
        scrubbed = {"HOME": self.environ["HOME"], "PATH": "/usr/bin:/bin"}
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, scrubbed, clear=True):
            with contextlib.redirect_stderr(stderr):
                code = dev.main(
                    [
                        "probe",
                        "run",
                        "--allow-local-claude",
                        "--fixture-root",
                        str(self.fixture_root),
                        "--native-contract",
                        str(contract),
                        "--",
                    ]
                )
        self.assertEqual(code, 2)
        self.assertIn("refusing real native execution without allow_real", stderr.getvalue())
        self.assertFalse((self.fixture_root / "evidence").exists())

    def test_dev_main_probe_split_preserves_dashed_argv(self) -> None:
        captured: dict = {}

        def fake_cli(positionals, flags, run_argv, *, environ=None):
            captured["positionals"] = positionals
            captured["flags"] = flags
            captured["run_argv"] = run_argv
            return 0

        with mock.patch.object(probe, "probe_cli", fake_cli):
            code = dev.main(
                [
                    "probe",
                    "run",
                    "--allow-local-claude",
                    "--fixture-root",
                    "/x",
                    "--",
                    "/bin/fake",
                    "--resume",
                    "1234",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(captured["positionals"], ["run"])
        self.assertEqual(captured["run_argv"], ["/bin/fake", "--resume", "1234"])
        self.assertEqual(captured["flags"]["fixture-root"], "/x")


class DaemonDomainGateTests(ProbeTestCase):
    """Config-root daemon-domain gate: CLAUDE_CONFIG_DIR allowance matrix."""

    def setUp(self) -> None:
        super().setUp()
        self.fixture = probe.build_fixture(self.fixture_root, environ=self.environ)

    def _allow(self, fixture: probe.ProbeFixture, environ: dict | None = None) -> None:
        probe._assert_real_run_allowed(
            fixture, self.environ if environ is None else environ
        )

    def test_valid_fixture_passes_the_allowance(self) -> None:
        _isolation_or_skip(self)
        self._allow(self.fixture)

    def test_config_dir_equal_to_root_refused(self) -> None:
        variant = replace(self.fixture, claude_config_dir=self.fixture.root)
        with self.assertRaisesRegex(ProbeError, "not inside the disposable fixture"):
            self._allow(variant)

    def test_config_dir_outside_root_refused(self) -> None:
        outside = state.ensure_private_dir(self.root / "elsewhere-config")
        variant = replace(self.fixture, claude_config_dir=outside)
        with self.assertRaisesRegex(ProbeError, "not inside the disposable fixture"):
            self._allow(variant)

    def test_config_dir_symlink_escape_refused(self) -> None:
        target = state.ensure_private_dir(self.root / "escaped-config")
        link = self.fixture_root / "linked-config"
        link.symlink_to(target, target_is_directory=True)
        variant = replace(self.fixture, claude_config_dir=link)
        with self.assertRaisesRegex(ProbeError, "not inside the disposable fixture"):
            self._allow(variant)

    def test_missing_config_dir_refused(self) -> None:
        variant = replace(
            self.fixture, claude_config_dir=self.fixture_root / "no-such-config"
        )
        with self.assertRaisesRegex(ProbeError, "does not exist"):
            self._allow(variant)

    def test_non_private_config_dir_refused(self) -> None:
        loose = self.fixture_root / "loose-config"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        variant = replace(self.fixture, claude_config_dir=loose)
        with self.assertRaisesRegex(ProbeError, "owner-private"):
            self._allow(variant)

    def test_ambient_provider_credentials_refused(self) -> None:
        ambient = {**self.environ, "ANTHROPIC_API_KEY": "dummy-secret-value"}
        with self.assertRaises(ProbeError) as raised:
            self._allow(self.fixture, ambient)
        self.assertIn("ANTHROPIC_API_KEY", str(raised.exception))
        self.assertNotIn("dummy-secret-value", str(raised.exception))

    def test_allowance_runs_before_executable_hashing(self) -> None:
        # A bogus spec digest must not be reached when the config-root gate
        # already refuses: the refusal names the config rule, never a hash.
        script = self._write_fake_executable()
        outside = state.ensure_private_dir(self.root / "elsewhere-config")
        variant = replace(self.fixture, claude_config_dir=outside)
        with self.assertRaisesRegex(ProbeError, "not inside the disposable fixture"):
            probe.run_native(
                [],
                trusted=self._trusted(script, fake=False, digest="0" * 64),
                fixture=variant,
                allow_real=True,
                environ=self.environ,
            )


class DaemonDomainSnapshotTests(ProbeTestCase):
    """Live-domain snapshot, tamper diff, and fail-closed enforcement."""

    def setUp(self) -> None:
        super().setUp()
        self.fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        self.domain = state.ensure_private_dir(self.root / "cc-daemon-test")

    def _snapshot(
        self, entries: tuple[probe.DaemonDomainEntry, ...], *, present: bool = True
    ) -> probe.DaemonDomainSnapshot:
        return probe.DaemonDomainSnapshot(
            self.domain, present, 100 if present else None, entries
        )

    def test_expected_subdomain_is_config_root_hash_prefix(self) -> None:
        resolved = os.path.realpath(self.fixture.claude_config_dir)
        expected = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
        self.assertEqual(
            probe.expected_daemon_subdomain(self.fixture.claude_config_dir), expected
        )

    def test_snapshot_absent_domain_tolerated(self) -> None:
        snap = probe.snapshot_daemon_domain(self.root / "absent-domain")
        self.assertFalse(snap.present)
        self.assertEqual(snap.entries, ())
        self.assertIsNone(snap.dir_mtime_ns)

    def test_snapshot_records_entry_kinds_sorted(self) -> None:
        (self.domain / "z-file").write_text("x")
        (self.domain / "a-dir").mkdir()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.domain / "m-sock"))
            snap = probe.snapshot_daemon_domain(self.domain)
        finally:
            listener.close()
        self.assertTrue(snap.present)
        names = [entry.name for entry in snap.entries]
        self.assertEqual(names, ["a-dir", "m-sock", "z-file"])
        kinds = {entry.name: entry.kind for entry in snap.entries}
        self.assertEqual(kinds, {"a-dir": "dir", "m-sock": "sock", "z-file": "file"})

    def test_domain_changes_detects_added_removed_changed(self) -> None:
        before = self._snapshot(
            (
                probe.DaemonDomainEntry("keep", "file", 1, 1),
                probe.DaemonDomainEntry("drop", "file", 1, 1),
                probe.DaemonDomainEntry("mutate", "file", 1, 1),
            )
        )
        after = self._snapshot(
            (
                probe.DaemonDomainEntry("keep", "file", 1, 1),
                probe.DaemonDomainEntry("mutate", "file", 2, 1),
                probe.DaemonDomainEntry("new", "dir", 1, 0),
            )
        )
        self.assertEqual(
            probe._domain_changes(before, after),
            ("added:new", "removed:drop", "changed:mutate"),
        )

    def test_domain_changes_detects_appear_vanish_and_dir_mtime(self) -> None:
        entry = probe.DaemonDomainEntry("keep", "file", 1, 1)
        present = self._snapshot((entry,))
        absent = self._snapshot((), present=False)
        self.assertEqual(
            probe._domain_changes(absent, present),
            ("domain-appeared", "added:keep"),
        )
        self.assertEqual(
            probe._domain_changes(present, absent),
            ("domain-vanished", "removed:keep"),
        )
        churned = probe.DaemonDomainSnapshot(self.domain, True, 200, (entry,))
        self.assertEqual(
            probe._domain_changes(present, churned), ("domain-dir-mtime",)
        )

    def test_domain_changes_refuses_mismatched_domains(self) -> None:
        other = probe.DaemonDomainSnapshot(self.root / "other", True, 1, ())
        with self.assertRaisesRegex(ProbeError, "different domains"):
            probe._domain_changes(self._snapshot(()), other)

    def test_enforce_untouched_returns_metadata_observation(self) -> None:
        before = probe.snapshot_daemon_domain(self.domain)
        after = probe.snapshot_daemon_domain(self.domain)
        observation = probe._enforce_live_domain_untouched(
            before, after, fixture=self.fixture
        )
        self.assertTrue(observation.unchanged)
        self.assertEqual(observation.domain, str(self.domain))
        self.assertTrue(observation.present_before)
        self.assertTrue(observation.present_after)
        self.assertEqual(
            observation.expected_fixture_subdomain,
            probe.expected_daemon_subdomain(self.fixture.claude_config_dir),
        )
        self.assertFalse(observation.fixture_subdomain_observed)

    def test_enforce_tamper_fails_closed_without_remediation(self) -> None:
        before = probe.snapshot_daemon_domain(self.domain)
        rogue = self.domain / "rogue-entry"
        rogue.mkdir()
        after = probe.snapshot_daemon_domain(self.domain)
        with self.assertRaises(ProbeError) as raised:
            probe._enforce_live_domain_untouched(before, after, fixture=self.fixture)
        message = str(raised.exception)
        self.assertIn("live daemon domain touched", message)
        self.assertIn("added:rogue-entry", message)
        self.assertIn("no remediation performed", message)
        # A foreign entry is never modified by the tripwire.
        self.assertTrue(rogue.is_dir())

    def test_enforce_fixture_subdomain_is_never_remediated(self) -> None:
        before = probe.snapshot_daemon_domain(self.domain)
        subdomain = self.domain / probe.expected_daemon_subdomain(
            self.fixture.claude_config_dir
        )
        subdomain.mkdir()
        after = probe.snapshot_daemon_domain(self.domain)
        with self.assertRaises(ProbeError) as raised:
            probe._enforce_live_domain_untouched(before, after, fixture=self.fixture)
        message = str(raised.exception)
        self.assertIn("live daemon domain touched", message)
        self.assertIn("observe-only; no remediation performed", message)
        # Even a fixture-looking entry lives in the live domain and is never
        # modified by the probe.
        self.assertTrue(subdomain.exists())

    def test_fixture_daemon_domains_lists_siblings_only(self) -> None:
        (self.root / "cc-daemon-deadbeef-1000").mkdir()
        (self.root / "cc-daemon-0123456789abcdef").mkdir()
        (self.root / "not-a-domain").mkdir()
        names = probe.fixture_daemon_domains(self.domain)
        self.assertEqual(
            names, ("cc-daemon-0123456789abcdef", "cc-daemon-deadbeef-1000")
        )

    def test_fixture_daemon_domains_tolerates_absent_parent(self) -> None:
        missing = self.root / "no-such-parent" / "cc-daemon-test"
        self.assertEqual(probe.fixture_daemon_domains(missing), ())

    def test_enforce_records_fixture_sibling_observation(self) -> None:
        expected = probe.expected_daemon_subdomain(self.fixture.claude_config_dir)
        sibling = f"cc-daemon-{expected}-4242"
        (self.root / sibling).mkdir()
        before = probe.snapshot_daemon_domain(self.domain)
        after = probe.snapshot_daemon_domain(self.domain)
        observation = probe._enforce_live_domain_untouched(
            before,
            after,
            fixture=self.fixture,
            fixture_domains_before=(),
            fixture_domains_after=(sibling,),
        )
        self.assertTrue(observation.fixture_subdomain_observed)
        self.assertEqual(observation.fixture_domains_after, (sibling,))
        self.assertEqual(observation.fixture_domains_before, ())


class RunNativeAllowRealTests(ProbeTestCase):
    """run_native(allow_real=True): allow/refuse matrix and live tripwire.

    Real-gate runs need the network namespace; without it (for example the
    Nix sandbox suite) the class skips with a BOUNDARY message, and
    IsolationFailClosedTests pins the refusal instead.
    """

    def setUp(self) -> None:
        super().setUp()
        _isolation_or_skip(self)
        self.fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        self.live_domain = state.ensure_private_dir(self.root / "cc-daemon-test")

    def _real_trusted(self, script: Path) -> probe.TrustedExecutable:
        return self._trusted(script, fake=False)

    def _run(
        self,
        script: Path,
        args: list | None = None,
        **overrides,
    ) -> probe.NativeRunResult:
        options = {
            "trusted": self._real_trusted(script),
            "fixture": self.fixture,
            "timeout": 15,
            "allow_real": True,
            "environ": self.environ,
            "live_daemon_domain": self.live_domain,
        }
        options.update(overrides)
        return probe.run_native(args or [], **options)

    def test_allow_real_runs_with_clean_tripwire(self) -> None:
        script = self._write_fake_executable()
        result = self._run(script)
        self.assertEqual(result.returncode, 0)
        self.assertIn("STATUS=200", result.stdout)
        self.assertIsNotNone(result.daemon)
        assert result.daemon is not None
        self.assertTrue(result.daemon.unchanged)
        self.assertEqual(result.daemon.domain, str(self.live_domain))
        self.assertFalse(result.daemon.fixture_subdomain_observed)
        self.assertEqual(result.daemon.fixture_domains_after, ())

    def test_allow_real_refuses_config_dir_outside_fixture(self) -> None:
        script = self._write_fake_executable()
        outside = state.ensure_private_dir(self.root / "elsewhere-config")
        variant = replace(self.fixture, claude_config_dir=outside)
        with self.assertRaisesRegex(ProbeError, "not inside the disposable fixture"):
            self._run(script, fixture=variant)

    def test_allow_real_refuses_ambient_credentials(self) -> None:
        script = self._write_fake_executable()
        ambient = {**self.environ, "OPENAI_API_KEY": "dummy-secret-value"}
        with self.assertRaises(ProbeError) as raised:
            self._run(script, environ=ambient)
        self.assertIn("OPENAI_API_KEY", str(raised.exception))
        self.assertNotIn("dummy-secret-value", str(raised.exception))

    def test_allow_real_requires_the_loopback_fake_provider(self) -> None:
        script = self._write_fake_executable()
        with self.assertRaisesRegex(ProbeError, "must be the loopback fake"):
            self._run(script, provider=object())

    def test_allow_real_tamper_fails_closed(self) -> None:
        body = (
            "import os\n"
            "import sys\n"
            "os.makedirs(sys.argv[1], exist_ok=True)\n"
        )
        script = self._write_fake_executable("tamper-claude", body)
        rogue = self.live_domain / "rogue-entry"
        with self.assertRaisesRegex(ProbeError, "live daemon domain touched"):
            self._run(script, args=[str(rogue)])
        # Foreign entries are evidence, never remediated.
        self.assertTrue(rogue.is_dir())

    def test_allow_real_absent_live_domain_tolerated(self) -> None:
        script = self._write_fake_executable()
        result = self._run(script, live_daemon_domain=self.root / "absent-domain")
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(result.daemon)
        assert result.daemon is not None
        self.assertTrue(result.daemon.unchanged)
        self.assertFalse(result.daemon.present_before)
        self.assertFalse(result.daemon.present_after)

    def test_allow_real_records_fixture_sibling_domain(self) -> None:
        expected = probe.expected_daemon_subdomain(self.fixture.claude_config_dir)
        sibling = f"cc-daemon-{expected}-1000"
        (self.root / sibling).mkdir()
        script = self._write_fake_executable()
        result = self._run(script)
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(result.daemon)
        assert result.daemon is not None
        self.assertTrue(result.daemon.unchanged)
        self.assertTrue(result.daemon.fixture_subdomain_observed)
        self.assertEqual(result.daemon.fixture_domains_before, (sibling,))
        self.assertEqual(result.daemon.fixture_domains_after, (sibling,))


class ProbeEnvAllowlistTests(ProbeTestCase):
    """Child-env overrides: typed allowlist, credential refusal."""

    def setUp(self) -> None:
        super().setUp()
        self.fixture = probe.build_fixture(self.fixture_root, environ=self.environ)

    def test_accepts_each_allowlisted_name_with_typed_values(self) -> None:
        overrides = {
            "CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP": "1",
            "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
            "CLAUDE_CODE_WORKFLOWS": "true",
            "CLAUDE_CODE_DISABLE_WORKFLOWS": "0",
            "CLAUDE_CODE_SUBAGENT_MODEL": "claude-sonnet-4-6",
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "claude-multi-opus-5-5[1m]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5-5[1m]",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-fable-5-1[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5",
            "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "1000",
            "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS": "4",
            "CLAUDE_CODE_RETRY_WATCHDOG": "1",
            "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "22",
            "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "2",
            "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION": "2",
            "ANTHROPIC_SMALL_FAST_MODEL": "probe-sf-marker",
        }
        self.assertEqual(set(overrides), probe.PROBE_ENV_ALLOWLIST)
        self.assertEqual(probe.validate_probe_env(overrides), overrides)
        env = self.fixture.environ(
            base_url="http://127.0.0.1:8317", child_env=overrides
        )
        for name, value in overrides.items():
            with self.subTest(name=name):
                self.assertEqual(env[name], value)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], probe.DUMMY_TOKEN)

    def test_none_and_empty_add_nothing(self) -> None:
        self.assertEqual(probe.validate_probe_env(None), {})
        self.assertEqual(probe.validate_probe_env({}), {})
        self.assertEqual(
            self.fixture.environ(child_env={}), self.fixture.environ()
        )

    def test_refuses_names_off_the_allowlist(self) -> None:
        for name in (
            "PATH",
            "HOME",
            "LD_PRELOAD",
            "CLAUDE_CONFIG_DIR",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_MODEL",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
            "PYTHONPATH",
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ProbeError, "not on the probe allowlist"):
                    probe.validate_probe_env({name: "1"})

    def test_refuses_credential_shaped_names_without_leaking_values(self) -> None:
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "OPENAI_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ProbeError) as raised:
                    probe.validate_probe_env({name: "dummy-secret-value"})
                self.assertIn("credential-shaped", str(raised.exception))
                self.assertIn(name, str(raised.exception))
                self.assertNotIn("dummy-secret-value", str(raised.exception))

    def test_only_credential_shaped_allowlist_member_is_an_integer_rule(self) -> None:
        shaped = sorted(
            name for name in probe.PROBE_ENV_ALLOWLIST if probe._credential_shaped(name)
        )
        self.assertEqual(shaped, ["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"])
        with self.assertRaisesRegex(ProbeError, "outside its int rule"):
            probe.validate_probe_env(
                {"CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "sk-looks-like-a-key"}
            )

    def test_refuses_values_outside_their_rules(self) -> None:
        for name, value, kind in (
            ("CLAUDE_CODE_GATEWAY_HINT_HEADERS", "yes", "flag"),
            ("CLAUDE_CODE_GATEWAY_HINT_HEADERS", "", "flag"),
            ("CLAUDE_CODE_SUBAGENT_MODEL", "model with space", "model"),
            ("CLAUDE_CODE_SUBAGENT_MODEL", "m" * 200, "model"),
            ("CLAUDE_CODE_SUBAGENT_MODEL", "", "model"),
            ("CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "-1", "int"),
            ("CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "86400001", "int"),
            ("CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS", "0", "int"),
            ("CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS", "65", "int"),
            ("CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS", "٣", "int"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(ProbeError, f"outside its {kind} rule"):
                    probe.validate_probe_env({name: value})

    def test_refuses_non_string_values_and_non_mappings(self) -> None:
        with self.assertRaisesRegex(ProbeError, "must be a string"):
            probe.validate_probe_env({"CLAUDE_CODE_GATEWAY_HINT_HEADERS": 1})
        with self.assertRaisesRegex(ProbeError, "mapping"):
            probe.validate_probe_env([("CLAUDE_CODE_GATEWAY_HINT_HEADERS", "1")])

    def test_run_native_refuses_bad_override_before_hashing(self) -> None:
        script = self._write_fake_executable()
        with self.assertRaisesRegex(ProbeError, "not on the probe allowlist"):
            probe.run_native(
                [],
                trusted=self._trusted(script, digest="0" * 64),
                fixture=self.fixture,
                child_env={"LD_PRELOAD": "/x.so"},
            )

    def test_run_native_threads_allowlisted_env(self) -> None:
        script = self._write_fake_executable("env-client", _ENV_CLIENT_BODY)
        result = probe.run_native(
            [],
            trusted=self._trusted(script),
            fixture=self.fixture,
            timeout=15,
            child_env={
                "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
                "CLAUDE_CODE_SUBAGENT_MODEL": "probe-sub",
                "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "1000",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HINTS=1", result.stdout)
        self.assertIn("SUBAGENT=probe-sub", result.stdout)
        self.assertIn("TTL=1000", result.stdout)
        self.assertIn("PATH=/usr/bin:/bin", result.stdout)


class TokenOptOutTests(ProbeTestCase):
    """helper-only auth runs the child without an env token."""

    def setUp(self) -> None:
        super().setUp()
        self.fixture = probe.build_fixture(self.fixture_root, environ=self.environ)

    def test_environ_token_none_keeps_base_url_only(self) -> None:
        env = self.fixture.environ(base_url="http://127.0.0.1:8317", token=None)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8317")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_environ_default_token_is_unchanged(self) -> None:
        self.assertEqual(
            self.fixture.environ(base_url="http://127.0.0.1:8317"),
            self.fixture.environ(
                base_url="http://127.0.0.1:8317", token=probe.DUMMY_TOKEN
            ),
        )

    def test_environ_accepts_fixed_test_token(self) -> None:
        env = self.fixture.environ(base_url="http://127.0.0.1:8317", token="tok-a")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "tok-a")

    def test_environ_refuses_unsafe_tokens(self) -> None:
        for token in ("", "abc", "has space", "x" * 200, "tok\n", 7):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ProbeError, "short fixed non-secret"):
                    self.fixture.environ(
                        base_url="http://127.0.0.1:8317", token=token
                    )

    def test_run_native_without_env_token_presents_no_auth(self) -> None:
        script = self._write_fake_executable("env-client", _ENV_CLIENT_BODY)
        result = probe.run_native(
            [],
            trusted=self._trusted(script),
            fixture=self.fixture,
            timeout=15,
            token=None,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TOKEN=absent", result.stdout)
        self.assertEqual([record.auth for record in result.requests], ["absent"])

    def test_run_native_known_token_is_labelled(self) -> None:
        script = self._write_fake_executable("env-client", _ENV_CLIENT_BODY)
        with probe.FakeAnthropicProvider(
            known_tokens={"tok-a": "A", "tok-b": "B"}
        ) as provider:
            result = probe.run_native(
                [],
                trusted=self._trusted(script),
                fixture=self.fixture,
                provider=provider,
                timeout=15,
                token="tok-b",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([record.auth for record in result.requests], ["B"])
        self.assertNotIn("tok-b", repr(result.requests))


class FixturePathDirTests(ProbeTestCase):
    """Validated fixture bin dirs prepended to the fixed PATH."""

    def setUp(self) -> None:
        super().setUp()
        self.fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        self.bin_dir = state.ensure_private_dir(self.fixture.root / "probe-bin")

    def test_valid_dir_is_prepended(self) -> None:
        env = self.fixture.environ(path_dirs=(self.bin_dir,))
        self.assertEqual(env["PATH"], f"{self.bin_dir}:/usr/bin:/bin")
        self.assertEqual(
            probe.validate_fixture_path_dirs(self.fixture, [str(self.bin_dir)]),
            (self.bin_dir,),
        )

    def test_refusals(self) -> None:
        outside = state.ensure_private_dir(self.root / "outside-bin")
        public = self.fixture.root / "public-bin"
        public.mkdir(mode=0o755)
        os.chmod(public, 0o755)
        os.chmod(public, 0o755)
        link = self.fixture.root / "linked-bin"
        link.symlink_to(self.bin_dir, target_is_directory=True)
        colon = self.fixture.root / "a:b"
        for entry, needle in (
            (outside, "not inside fixture root"),
            (self.fixture.root, "not inside fixture root"),
            (Path("relative/bin"), "absolute"),
            (self.bin_dir / ".." / "probe-bin", r"\.\."),
            (link, "symlink"),
            (public, "not a safe private directory"),
            (self.fixture.root / "missing-bin", "does not exist"),
            (colon, "free of ':'"),
            ("", "nonempty"),
        ):
            with self.subTest(entry=entry):
                with self.assertRaisesRegex(ProbeError, needle):
                    probe.validate_fixture_path_dirs(self.fixture, (entry,))
        self.assertFalse(os.path.lexists(self.fixture.root / "missing-bin"))

    def test_refuses_duplicates_strings_and_symlink_entries(self) -> None:
        with self.assertRaisesRegex(ProbeError, "listed twice"):
            probe.validate_fixture_path_dirs(
                self.fixture, (self.bin_dir, self.bin_dir)
            )
        with self.assertRaisesRegex(ProbeError, "sequence"):
            probe.validate_fixture_path_dirs(self.fixture, str(self.bin_dir))
        (self.bin_dir / "claude").symlink_to("/usr/bin/true")
        with self.assertRaisesRegex(ProbeError, "symlink entry"):
            probe.validate_fixture_path_dirs(self.fixture, (self.bin_dir,))

    def test_write_fixture_executable_is_private_and_validated(self) -> None:
        path = probe.write_fixture_executable(
            self.fixture, self.bin_dir, "claude-multi", "#!/bin/sh\nexit 0\n"
        )
        self.assertEqual(path, self.bin_dir / "claude-multi")
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700)
        for name in ("", "../x", "a/b", ".hidden", "x" * 80):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ProbeError, "plain safe file name"):
                    probe.write_fixture_executable(
                        self.fixture, self.bin_dir, name, "#!/bin/sh\n"
                    )
        outside = state.ensure_private_dir(self.root / "outside-bin")
        with self.assertRaisesRegex(ProbeError, "not inside fixture root"):
            probe.write_fixture_executable(
                self.fixture, outside, "claude-multi", "#!/bin/sh\n"
            )
        self.assertFalse((outside / "claude-multi").exists())
        with self.assertRaisesRegex(ProbeError, "nonempty"):
            probe.write_fixture_executable(self.fixture, self.bin_dir, "empty", b"")

    def test_fake_launcher_on_path_records_argv(self) -> None:
        record = self.fixture.root / "launcher-argv.json"
        probe.write_fixture_executable(
            self.fixture,
            self.bin_dir,
            "claude-multi",
            f"#!{sys.executable}\nimport json, sys\n"
            f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
        )
        script = self._write_fake_executable("env-client", _ENV_CLIENT_BODY)
        result = probe.run_native(
            [],
            trusted=self._trusted(script),
            fixture=self.fixture,
            timeout=15,
            path_dirs=(self.bin_dir,),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"LAUNCHER={self.bin_dir / 'claude-multi'}", result.stdout)
        self.assertEqual(
            strict_json.loads(record.read_bytes()), ["lineup", "--probe-arg=x y"]
        )

    def test_run_native_refuses_bad_path_dir_before_hashing(self) -> None:
        script = self._write_fake_executable()
        outside = state.ensure_private_dir(self.root / "outside-bin")
        with self.assertRaisesRegex(ProbeError, "not inside fixture root"):
            probe.run_native(
                [],
                trusted=self._trusted(script, digest="0" * 64),
                fixture=self.fixture,
                path_dirs=(outside,),
            )


class KnownTokenAuthPolicyTests(ProbeTestCase):
    """Labelled dummy tokens and a pre-responder auth policy."""

    _BODY = b'{"model":"probe-model","max_tokens":8,"messages":[]}'

    def test_labels_known_tokens_and_keeps_dummy(self) -> None:
        with probe.FakeAnthropicProvider(
            known_tokens={"tok-a": "A", "tok-b": "B"}
        ) as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            for headers in (
                {"x-api-key": "tok-a"},
                {"Authorization": "Bearer tok-b"},
                {"x-api-key": probe.DUMMY_TOKEN},
                {"x-api-key": "tok-unknown"},
                {"Content-Type": "application/json"},
            ):
                self._post(port, self._BODY, headers=headers)
            auths = [record.auth for record in provider.requests]
        self.assertEqual(auths, ["A", "B", "dummy", "other", "absent"])
        rendered = repr(provider.requests)
        for token in ("tok-a", "tok-b", "tok-unknown", probe.DUMMY_TOKEN):
            self.assertNotIn(token, rendered)

    def test_known_tokens_validation(self) -> None:
        for tokens, needle in (
            ({"tok-a": "dummy"}, "reserved"),
            ({"tok-a": "other"}, "reserved"),
            ({"tok-a": "absent"}, "reserved"),
            ({probe.DUMMY_TOKEN: "D"}, "must not relabel"),
            ({"tok-a": "A", "tok-b": "A"}, "listed twice"),
            ({"bad token": "A"}, "short fixed non-secret"),
            ({"tok-a": "bad label"}, "short safe ids"),
            ({f"tok-{index:02d}": f"L{index}" for index in range(17)}, "bounded"),
            ([("tok-a", "A")], "map fixed test tokens"),
        ):
            with self.subTest(tokens=tokens):
                with self.assertRaisesRegex(ProbeError, needle):
                    probe.FakeAnthropicProvider(known_tokens=tokens)

    def test_policy_rejects_chosen_label_before_the_responder(self) -> None:
        calls: list[str] = []

        def responder(document, path):
            calls.append(path)
            return 200, {"type": "message", "content": []}

        with probe.FakeAnthropicProvider(
            responder=responder,
            known_tokens={"tok-a": "A", "tok-b": "B"},
            auth_policy=probe.reject_auth_labels("A"),
        ) as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status_a, payload_a = self._post(
                port, self._BODY, headers={"x-api-key": "tok-a"}
            )
            status_b, _ = self._post(port, self._BODY, headers={"x-api-key": "tok-b"})
            records = provider.requests
        self.assertEqual(status_a, 401)
        self.assertEqual(
            strict_json.loads(payload_a)["error"]["type"], "authentication_error"
        )
        self.assertEqual(status_b, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual([record.auth for record in records], ["A", "B"])
        self.assertEqual(
            [record.auth_rejected_status for record in records], [401, None]
        )

    def test_policy_can_answer_403_and_reject_absent(self) -> None:
        with probe.FakeAnthropicProvider(
            auth_policy=probe.reject_auth_labels("absent", status=403)
        ) as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, payload = self._post(port, self._BODY)
        self.assertEqual(status, 403)
        self.assertEqual(
            strict_json.loads(payload)["error"]["type"], "permission_error"
        )

    def test_broken_or_invalid_policy_fails_closed(self) -> None:
        calls: list[str] = []

        def responder(document, path):
            calls.append(path)
            return 200, {"type": "message", "content": []}

        def raising(_context):
            raise RuntimeError("policy bug")

        for policy in (raising, lambda _context: 200, lambda _context: "401"):
            with self.subTest(policy=policy):
                with probe.FakeAnthropicProvider(
                    responder=responder, auth_policy=policy
                ) as provider:
                    port = int(provider.base_url.rsplit(":", 1)[1])
                    status, _ = self._post(port, self._BODY)
                    self.assertEqual(status, 500)
                    self.assertEqual(provider.requests[0].auth_rejected_status, 500)
        self.assertEqual(calls, [])

    def test_reject_auth_labels_validation(self) -> None:
        with self.assertRaisesRegex(ProbeError, "401 or 403"):
            probe.reject_auth_labels("A", status=500)
        with self.assertRaisesRegex(ProbeError, "at least one"):
            probe.reject_auth_labels()
        with self.assertRaisesRegex(ProbeError, "short safe ids"):
            probe.reject_auth_labels("bad label")
        with self.assertRaisesRegex(ProbeError, "callable"):
            probe.FakeAnthropicProvider(auth_policy="A")


class RequestMetadataTests(ProbeTestCase):
    """S5-S8 metadata: effort, max_tokens, hint headers, markers."""

    def _one(self, document: dict, headers: dict | None = None, **options):
        with probe.FakeAnthropicProvider(**options) as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, _ = self._post(
                port,
                strict_json.canonical_bytes(document),
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            self.assertEqual(status, 200)
            return provider.requests[0]

    def test_defaults_keep_existing_records_valid(self) -> None:
        record = self._one({"model": "probe-model", "messages": []})
        self.assertIsNone(record.max_tokens)
        self.assertIsNone(record.effort)
        self.assertIsNone(record.thinking_type)
        self.assertIsNone(record.thinking_budget_tokens)
        self.assertEqual(record.hint_header_names, ())
        self.assertEqual(record.hint_header_values, ())
        self.assertFalse(record.hint_headers_truncated)
        self.assertEqual(record.markers_found, ())
        self.assertIsNone(record.auth_rejected_status)

    def test_records_max_tokens_effort_and_thinking_shape(self) -> None:
        record = self._one(
            {
                "model": "probe-model",
                "max_tokens": 64000,
                "messages": [],
                "output_config": {"effort": "xhigh"},
                "thinking": {"type": "enabled", "budget_tokens": 31999},
            }
        )
        self.assertEqual(record.max_tokens, 64000)
        self.assertEqual(record.effort, "xhigh")
        self.assertEqual(record.thinking_type, "enabled")
        self.assertEqual(record.thinking_budget_tokens, 31999)
        adaptive = self._one(
            {"model": "m", "messages": [], "thinking": {"type": "adaptive"}}
        )
        self.assertEqual(adaptive.thinking_type, "adaptive")
        self.assertIsNone(adaptive.thinking_budget_tokens)

    def test_malformed_effort_metadata_is_bounded(self) -> None:
        secret = "effort-secret-" * 20
        record = self._one(
            {
                "model": "m",
                "max_tokens": True,
                "messages": [],
                "output_config": {"effort": secret},
                "thinking": {"type": 5, "budget_tokens": -1},
            }
        )
        self.assertIsNone(record.max_tokens)
        self.assertTrue(record.effort.startswith("sha256:"))
        self.assertIsNone(record.thinking_type)
        self.assertIsNone(record.thinking_budget_tokens)
        self.assertNotIn(secret, repr(record))

    def test_hint_header_names_and_safe_values_only(self) -> None:
        unsafe = "prompt text with spaces " * 4
        record = self._one(
            {"model": "m", "messages": []},
            headers={
                "X-Claude-Code-Request-Class": "main",
                "x-claude-code-agent-type": "cm-probe-marker",
                "x-claude-code-agent-id": unsafe,
                "x-claude-code-signature": "sig-value-secret",
                "x-api-key": "tok-never-recorded",
            },
        )
        self.assertEqual(
            record.hint_header_names,
            (
                "x-claude-code-agent-id",
                "x-claude-code-agent-type",
                "x-claude-code-request-class",
                "x-claude-code-signature",
            ),
        )
        values = dict(record.hint_header_values)
        self.assertEqual(values["x-claude-code-request-class"], "main")
        self.assertEqual(values["x-claude-code-agent-type"], "cm-probe-marker")
        self.assertTrue(values["x-claude-code-agent-id"].startswith("sha256:"))
        self.assertNotIn("x-claude-code-signature", values)
        rendered = repr(record)
        for secret in ("sig-value-secret", "tok-never-recorded", unsafe, "x-api-key"):
            self.assertNotIn(secret, rendered)

    def test_hint_headers_are_bounded(self) -> None:
        headers = {f"x-claude-code-extra-{index:02d}": "v" for index in range(40)}
        record = self._one({"model": "m", "messages": []}, headers=headers)
        self.assertEqual(len(record.hint_header_names), 32)
        self.assertTrue(record.hint_headers_truncated)

    def test_content_markers_record_names_never_text(self) -> None:
        marker = "CHECK-NOTICE-MARKER-0001"
        record = self._one(
            {
                "model": "m",
                "messages": [
                    {"role": "user", "content": f"context says {marker} here"}
                ],
            },
            content_markers={"notice": marker, "absent": "CHECK-NEVER-SENT-0002"},
        )
        self.assertEqual(record.markers_found, ("notice",))
        self.assertNotIn(marker, repr(record))

    def test_content_marker_validation(self) -> None:
        for markers, needle in (
            ({"n": "short"}, "8-128"),
            ({"n": 'has"quote-xxxxx'}, "8-128"),
            ({"bad name": "CHECK-MARKER-0001"}, "short safe ids"),
            ([("n", "CHECK-MARKER-0001")], "map marker names"),
            ({f"m{index}": f"CHECK-MARKER-{index:04d}" for index in range(17)}, "bounded"),
        ):
            with self.subTest(markers=markers):
                with self.assertRaisesRegex(ProbeError, needle):
                    probe.FakeAnthropicProvider(content_markers=markers)

    def test_records_serialize_as_metadata_evidence(self) -> None:
        fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        record = self._one(
            {"model": "m", "max_tokens": 8, "messages": []},
            headers={"x-claude-code-request-class": "main"},
        )
        path = probe.write_evidence(
            fixture, "metadata-sample", {"requests": [asdict(record)]}
        )
        loaded = strict_json.loads(path.read_bytes())["requests"][0]
        self.assertEqual(
            loaded["hint_header_values"], [["x-claude-code-request-class", "main"]]
        )
        self.assertEqual(loaded["max_tokens"], 8)


class ContextResponderTests(ProbeTestCase):
    """Optional respond(document, path, context) responder convention."""

    def test_context_responder_receives_metadata_context(self) -> None:
        seen: list = []

        class Recorder:
            def respond(self, document, path, context):
                seen.append(context)
                return 200, {"type": "message", "content": [], "ordinal": context.ordinal}

        with probe.FakeAnthropicProvider(
            responder=Recorder(), known_tokens={"tok-a": "A"}
        ) as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            body = b'{"model":"m","messages":[]}'
            self._post(port, body, headers={"x-api-key": "tok-a"})
            _, payload = self._post(
                port,
                body,
                headers={"x-claude-code-agent-type": "cm-x", "x-api-key": "tok-b1"},
            )
        self.assertEqual([context.ordinal for context in seen], [1, 2])
        self.assertEqual([context.auth for context in seen], ["A", "other"])
        self.assertIn("x-api-key", seen[0].header_names)
        self.assertIn("x-claude-code-agent-type", seen[1].header_names)
        self.assertEqual(seen[1].record.hint_header_names, ("x-claude-code-agent-type",))
        self.assertNotIn("tok-a", repr(seen))
        self.assertEqual(strict_json.loads(payload)["ordinal"], 2)

    def test_legacy_callable_responder_still_works(self) -> None:
        with probe.FakeAnthropicProvider(
            responder=lambda document, path: (200, {"legacy": path})
        ) as provider:
            port = int(provider.base_url.rsplit(":", 1)[1])
            status, payload = self._post(port, b'{"model":"m","messages":[]}')
        self.assertEqual(status, 200)
        self.assertEqual(strict_json.loads(payload), {"legacy": "/v1/messages"})

    def test_non_callable_responder_refused(self) -> None:
        with self.assertRaisesRegex(ProbeError, "responder must be callable"):
            probe.FakeAnthropicProvider(responder=object())


class PTYCallbackTests(ProbeTestCase):
    """Bounded callbacks between PTY interactions."""

    def setUp(self) -> None:
        super().setUp()
        self.fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        self.target = self.fixture.root / "agent-model.txt"
        self.target.write_text("model-a\n")
        self.script = self._write_fake_executable("pty-file", _PTY_FILE_BODY)

    def _run(self, interactions, **options):
        return probe.run_native_pty(
            [str(self.target)],
            interactions,
            trusted=self._trusted(self.script),
            fixture=self.fixture,
            timeout=options.pop("timeout", 15),
            **options,
        )

    def test_callback_runs_between_marker_and_send(self) -> None:
        def edit() -> None:
            self.target.write_text("model-b\n")

        result = self._run(
            (
                probe.PTYInteraction(b"READY", b"go\n"),
                probe.PTYInteraction(b"SEEN=model-a", b"again\n", before_send=edit),
            )
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("SEEN2=model-b", result.stdout)

    def test_callback_only_step_with_empty_send(self) -> None:
        calls: list[int] = []

        def edit() -> None:
            calls.append(1)
            self.target.write_text("model-c\n")

        result = self._run(
            (
                probe.PTYInteraction(b"READY", b"go\n"),
                # Callback-only barrier: nothing is typed, and the following
                # marker may arrive in the same PTY read.
                probe.PTYInteraction(
                    b"SEEN=", b"", preserve_after_wait=True, before_send=edit
                ),
                probe.PTYInteraction(b"model-a", b"again\n"),
            )
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, [1])
        self.assertIn("SEEN2=model-c", result.stdout)

    def test_callback_exception_fails_closed_and_reaps(self) -> None:
        def broken() -> None:
            raise RuntimeError("edit failed")

        with self.assertRaisesRegex(ProbeError, "step callback failed: RuntimeError"):
            self._run((probe.PTYInteraction(b"READY", b"go\n", before_send=broken),))

    def test_callback_overrun_fails_closed(self) -> None:
        started = time.monotonic()
        with self.assertRaisesRegex(ProbeError, "exceeded its"):
            self._run(
                (
                    probe.PTYInteraction(
                        b"READY",
                        b"go\n",
                        before_send=lambda: time.sleep(5),
                        callback_timeout=0.3,
                    ),
                )
            )
        self.assertLess(time.monotonic() - started, 4.5)

    def test_callback_validation(self) -> None:
        for interaction, needle in (
            (probe.PTYInteraction(b"READY", b"", before_send="x"), "callable"),
            (
                probe.PTYInteraction(
                    b"READY", b"", before_send=lambda: None, callback_timeout=0
                ),
                "callback timeout",
            ),
            (
                probe.PTYInteraction(
                    b"READY", b"", before_send=lambda: None, callback_timeout=301
                ),
                "callback timeout",
            ),
        ):
            with self.subTest(needle=needle):
                with self.assertRaisesRegex(ProbeError, needle):
                    self._run((interaction,))
        with self.assertRaisesRegex(ProbeError, "PTYInteraction values"):
            self._run(((b"READY", b"go\n"),))
        with self.assertRaisesRegex(ProbeError, "bounded step count"):
            self._run((probe.PTYInteraction(b"READY", b""),) * 257)

    def test_tripwire_still_enforced_when_a_callback_fails(self) -> None:
        _isolation_or_skip(self)
        live = state.ensure_private_dir(self.root / "cc-daemon-test")
        rogue = live / "rogue-entry"

        def touch_live_then_fail() -> None:
            rogue.mkdir()
            raise RuntimeError("after touching the live domain")

        with self.assertRaisesRegex(ProbeError, "live daemon domain touched"):
            probe.run_native_pty(
                [str(self.target)],
                (
                    probe.PTYInteraction(
                        b"READY", b"go\n", before_send=touch_live_then_fail
                    ),
                ),
                trusted=self._trusted(self.script, fake=False),
                fixture=self.fixture,
                timeout=15,
                allow_real=True,
                environ=self.environ,
                live_daemon_domain=live,
            )
        # Foreign live-domain entries are evidence, never remediated.
        self.assertTrue(rogue.is_dir())

    def test_pty_threads_token_opt_out_and_env(self) -> None:
        script = self._write_fake_executable(
            "pty-env",
            'import os\nprint("TOKEN=" + ("present" if "ANTHROPIC_AUTH_TOKEN" '
            'in os.environ else "absent") + " HINTS=" + '
            'os.environ.get("CLAUDE_CODE_GATEWAY_HINT_HEADERS", "absent"), '
            "flush=True)\n",
        )
        result = probe.run_native_pty(
            [],
            (),
            trusted=self._trusted(script),
            fixture=self.fixture,
            timeout=15,
            token=None,
            child_env={"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("TOKEN=absent HINTS=1", result.stdout)


_EGRESS_CLIENT_BODY = """import errno
import http.client
import json
import os
import socket
import sys
import time
import urllib.parse

def attempt(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    started = time.monotonic()
    try:
        sock.connect((host, port))
        result = "connected"
    except OSError as exc:
        result = errno.errorcode.get(exc.errno, str(exc.errno))
    finally:
        sock.close()
    return result, round(time.monotonic() - started, 3)

def attempt_unix(path):
    if not os.path.exists(path):
        return "absent"
    probe_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe_socket.settimeout(2)
    try:
        probe_socket.connect(path)
        return "connected"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
    finally:
        probe_socket.close()

url = urllib.parse.urlsplit(os.environ["ANTHROPIC_BASE_URL"])
connection = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
connection.request(
    "POST", "/v1/messages",
    body=json.dumps({"model": "probe-model", "max_tokens": 8, "messages": []}),
    headers={"Content-Type": "application/json", "x-api-key": os.environ["ANTHROPIC_AUTH_TOKEN"]},
)
provider_status = connection.getresponse().status
host_port = int(sys.argv[1])
try:
    resolved = socket.getaddrinfo("api.anthropic.com", 443)[0][4][0]
except OSError as exc:
    resolved = "unresolved:" + type(exc).__name__
report = {
    "provider": provider_status,
    "host_listener": attempt("127.0.0.1", host_port),
    "external": attempt("1.1.1.1", 443),
    "dns": resolved,
    "uid": os.geteuid(),
    "ifaces": sorted(name for _i, name in socket.if_nameindex()),
    "tmpdir": os.environ.get("CLAUDE_CODE_TMPDIR"),
    # Host control sockets: masked inside the namespace.
    "user_runtime": sorted(os.listdir("/run/user/%d" % os.geteuid()))
    if os.path.isdir("/run/user/%d" % os.geteuid()) else None,
    "docker_sock": attempt_unix("/run/docker.sock"),
}
print("EGRESS=" + json.dumps(report, sort_keys=True))
"""


def _isolation_or_skip(case: unittest.TestCase) -> None:
    reason = probe.network_isolation_available()
    if reason is not None:
        case.skipTest(f"BOUNDARY: {reason}")


class NetworkIsolationTests(ProbeTestCase):
    """Real runs are loopback-only; egress is impossible."""

    def setUp(self) -> None:
        super().setUp()
        _isolation_or_skip(self)
        self.fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        self.live_domain = state.ensure_private_dir(self.root / "cc-daemon-test")

    def test_isolated_real_run_reaches_only_the_bridged_provider(self) -> None:
        # A listener on the HOST loopback: the isolated child must not reach
        # it (its 127.0.0.1 is the namespace's own lo).
        host = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        host.bind(("127.0.0.1", 0))
        host.listen(1)
        self.addCleanup(host.close)
        host_port = host.getsockname()[1]
        script = self._write_fake_executable("egress-client", _EGRESS_CLIENT_BODY)
        with probe.FakeAnthropicProvider() as provider:
            result = probe.run_native(
                [str(host_port)],
                trusted=self._trusted(script, fake=False),
                fixture=self.fixture,
                provider=provider,
                timeout=30,
                allow_real=True,
                environ=self.environ,
                live_daemon_domain=self.live_domain,
            )
            sockets = sorted(
                path.name for path in (self.fixture.root / "run").iterdir()
            )
            provider_port = provider.port
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(
            item for item in result.stdout.splitlines() if item.startswith("EGRESS=")
        )
        report = strict_json.loads(line[len("EGRESS="):].encode("utf-8"))
        # Reaches the fake provider through the unix-socket bridge ...
        self.assertEqual(report["provider"], 200)
        self.assertEqual([record.auth for record in result.requests], ["dummy"])
        self.assertEqual(sockets, [f"provider-{provider_port}.sock"])
        # The socket is removed when the provider stops.
        self.assertEqual(list((self.fixture.root / "run").iterdir()), [])
        # ... but never the host loopback listener ...
        self.assertEqual(report["host_listener"][0], "ECONNREFUSED")
        # ... and an external address fails at once without leaving the
        # namespace (no route: nothing is sent), as does name resolution.
        self.assertEqual(report["external"][0], "ENETUNREACH")
        self.assertLess(report["external"][1], 1.0)
        self.assertTrue(report["dns"].startswith("unresolved:"), report["dns"])
        # Same uid (the uid-keyed tripwire still applies); lo only.
        self.assertEqual(report["uid"], os.geteuid())
        self.assertEqual(report["ifaces"], ["lo"])
        self.assertEqual(report["tmpdir"], str(self.fixture.tmp_dir))
        # Host control sockets are masked: the user runtime dir (session bus,
        # agents) is an empty tmpfs; the docker socket is /dev/null.
        self.assertIn(report["user_runtime"], (None, []))
        self.assertNotEqual(report["docker_sock"], "connected")
        # The host listener saw no connection at all.
        host.setblocking(False)
        with self.assertRaises(BlockingIOError):
            host.accept()
        self.assertEqual(result.argv[0], str(script))

    def test_isolated_pty_run_keeps_the_controlling_terminal(self) -> None:
        script = self._write_fake_executable(
            "pty-tty",
            "import os, sys\n"
            "print('TTY=%s' % os.isatty(0), flush=True)\n"
            "print('SID=%s' % (os.getsid(0) != os.getpid()), flush=True)\n"
            "line = input()\n"
            "print('GOT=' + line, flush=True)\n",
        )
        result = probe.run_native_pty(
            [],
            (probe.PTYInteraction(b"SID=", b"hello\r"),),
            trusted=self._trusted(script, fake=False),
            fixture=self.fixture,
            timeout=30,
            allow_real=True,
            environ=self.environ,
            live_daemon_domain=self.live_domain,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("TTY=True", result.stdout)
        self.assertIn("GOT=hello", result.stdout)

    def test_isolated_timeout_kills_the_whole_group(self) -> None:
        pidfile = self.fixture_root / "grandchild.pid"
        spawner = self._write_fake_executable("spawner", _SPAWNER_BODY)
        result = probe.run_native(
            [str(pidfile)],
            trusted=self._trusted(spawner, fake=False),
            fixture=self.fixture,
            timeout=2.0,
            allow_real=True,
            environ=self.environ,
            live_daemon_domain=self.live_domain,
        )
        self.assertTrue(result.timed_out)
        deadline = time.time() + 10
        while not pidfile.exists() and time.time() < deadline:
            time.sleep(0.05)
        grandchild = int(pidfile.read_text())
        while time.time() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("isolated grandchild survived the process-group kill")

    def test_exec_identity_recheck_refuses_a_swapped_artifact(self) -> None:
        script = self._write_fake_executable("swap-me", "print('ORIGINAL')\n")
        info = os.stat(script)
        argv = probe.isolated_argv(
            [str(script)],
            env_keys=["PATH"],
            identity=(info.st_dev, info.st_ino, info.st_size + 1, info.st_mtime_ns),
        )
        completed = subprocess.run(
            argv, env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(completed.returncode, 125)
        self.assertIn("changed between verification and exec", completed.stderr)
        self.assertNotIn("ORIGINAL", completed.stdout)

    def test_isolated_argv_shape(self) -> None:
        argv = probe.isolated_argv(["/usr/bin/true"], env_keys=["PATH"], cwd="/")
        self.assertTrue(argv[0].startswith("/"))
        self.assertEqual(
            argv[1:6], ["--unshare-net", "--die-with-parent", "--dev-bind", "/", "/"]
        )
        self.assertNotIn("--new-session", argv)
        self.assertNotIn("--unshare-pid", argv)
        self.assertNotIn("--share-net", argv)
        separator = argv.index("--")
        self.assertEqual(argv[separator + 2 : separator + 5], ["-I", "-S", "-c"])
        self.assertEqual(argv[-1], "/usr/bin/true")

    def test_generic_isolated_process_bridges_both_directions(self) -> None:
        run = state.ensure_private_dir(self.root / "bridge-run")
        upstream_socket = run / "upstream.sock"
        ingress_socket = run / "ingress.sock"
        hits: list[str] = []

        class Upstream(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                return

            def do_GET(self) -> None:
                hits.append(self.path)
                body = b"UPSTREAM-OK"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = probe.UnixHTTPServer(str(upstream_socket), Upstream)
        threading.Thread(
            target=upstream.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        self.assertEqual(stat.S_IMODE(os.lstat(upstream_socket).st_mode), 0o600)
        # The isolated "gateway": serves on its namespace-local port 18402 and
        # proxies each request to namespace-local 18401 (the egress bridge).
        gateway = self._write_fake_executable(
            "fake-gateway",
            "import http.client, http.server\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "    protocol_version = 'HTTP/1.1'\n"
            "    def log_message(self, *a): pass\n"
            "    def do_GET(self):\n"
            "        c = http.client.HTTPConnection('127.0.0.1', 18401, timeout=10)\n"
            "        c.request('GET', '/via-egress')\n"
            "        body = b'GW:' + c.getresponse().read()\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Length', str(len(body)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(body)\n"
            "http.server.HTTPServer(('127.0.0.1', 18402), H).serve_forever(poll_interval=0.01)\n",
        )
        process = probe.start_isolated_process(
            [str(gateway)],
            cwd=run,
            env={"PATH": "/usr/bin:/bin"},
            bridges=(
                probe.PortBridge(18401, upstream_socket, "egress"),
                probe.PortBridge(18402, ingress_socket, "ingress"),
            ),
        )
        try:
            deadline = time.monotonic() + 15
            body = b""
            while time.monotonic() < deadline:
                try:
                    connection = probe.UnixHTTPConnection(ingress_socket, timeout=5)
                    connection.request("GET", "/probe")
                    body = connection.getresponse().read()
                    connection.close()
                    break
                except OSError:
                    time.sleep(0.1)
            self.assertEqual(body, b"GW:UPSTREAM-OK")
            self.assertEqual(hits, ["/via-egress"])
            self.assertEqual(stat.S_IMODE(os.lstat(ingress_socket).st_mode), 0o600)
        finally:
            probe.stop_isolated_process(process)
        self.assertIsNotNone(process.poll())

    def test_generic_isolated_process_validation(self) -> None:
        run = state.ensure_private_dir(self.root / "bridge-run")
        loose = self.root / "loose-run"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        os.chmod(loose, 0o755)
        for bridges, needle in (
            ((probe.PortBridge(0, run / "a.sock"),), "1..65535"),
            ((probe.PortBridge(80, run / "a.sock", "sideways"),), "direction"),
            ((probe.PortBridge(80, loose / "a.sock"),), "owner-private 0700"),
            ((probe.PortBridge(80, Path("rel.sock")),), "exact absolute"),
            (
                (probe.PortBridge(80, run / "a.sock"), probe.PortBridge(80, run / "b.sock")),
                "listed twice",
            ),
        ):
            with self.subTest(needle=needle):
                with self.assertRaisesRegex(ProbeError, needle):
                    probe.start_isolated_process(
                        [os.path.realpath(shutil.which("true"))], cwd=run, env={}, bridges=bridges
                    )
        with self.assertRaisesRegex(ProbeError, "exact absolute non-symlink"):
            probe.start_isolated_process(["true"], cwd=run, env={})


class BwrapOwnershipTests(unittest.TestCase):
    """Store owners may be unmapped in the build user namespace, never us."""

    def setUp(self) -> None:
        self.overflow_uid = probe._overflow_uid
        self.path = Path("/nix/store/gwtest-bubblewrap/bin/bwrap")
        self.info = mock.Mock(st_uid=65534, st_mode=stat.S_IFREG | 0o555)
        for target, value in (
            ("os.lstat", self.info),
            ("os.access", True),
            ("os.geteuid", 1000),
            ("_overflow_uid", 65534),
        ):
            patcher = mock.patch(f"claude_multi.probe.{target}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_store_overflow_owner_is_accepted(self) -> None:
        for owner in (0, 65534, 65533):
            with self.subTest(owner=owner):
                self.info.st_uid = owner
                with mock.patch.object(probe, "_overflow_uid", return_value=owner):
                    self.assertIsNone(probe._root_owned_path(self.path))

    def test_store_user_owner_is_refused(self) -> None:
        self.info.st_uid = 1000
        # Even a current uid equal to the kernel overflow uid is not trusted.
        for overflow in (65534, 1000):
            with self.subTest(overflow=overflow):
                with mock.patch.object(probe, "_overflow_uid", return_value=overflow):
                    self.assertIn("not root-owned", probe._root_owned_path(self.path))
        self.info.st_uid = 1001
        self.assertIn("not root-owned", probe._root_owned_path(self.path))

    def test_non_store_overflow_owner_is_refused(self) -> None:
        for path in ("/usr/bin/bwrap", "/nix/store-other/bin/bwrap"):
            with self.subTest(path=path):
                self.assertIn("not root-owned", probe._root_owned_path(Path(path)))

    def test_store_overflow_owner_keeps_file_safety_checks(self) -> None:
        for mode in (0o775, 0o557):
            with self.subTest(mode=mode):
                self.info.st_mode = stat.S_IFREG | mode
                self.assertIn("group/other-writable", probe._root_owned_path(self.path))
        for kind in (stat.S_IFDIR, stat.S_IFLNK, stat.S_IFIFO):
            with self.subTest(kind=kind):
                self.info.st_mode = kind | 0o555
                self.assertIn("not a regular file", probe._root_owned_path(self.path))
        self.info.st_mode = stat.S_IFREG | 0o555
        with mock.patch.object(probe.os, "access", return_value=False):
            self.assertIn("not executable", probe._root_owned_path(self.path))
        with mock.patch.object(probe.os, "lstat", side_effect=OSError("unreadable")):
            self.assertIn("not inspectable", probe._root_owned_path(self.path))

    def test_overflow_uid_read_and_fallback(self) -> None:
        with mock.patch.object(Path, "read_text", autospec=True) as read:
            for value, expected in (("65534\n", 65534), ("65533\n", 65533),
                                    ("", 65534), ("malformed", 65534)):
                with self.subTest(value=value):
                    read.return_value = value
                    self.assertEqual(self.overflow_uid(), expected)
                    read.assert_called_with(Path("/proc/sys/kernel/overflowuid"), encoding="ascii")
            read.side_effect = OSError("unreadable")
            self.assertEqual(self.overflow_uid(), 65534)


class IsolationFailClosedTests(ProbeTestCase):
    """Without a usable namespace a real run refuses; fake specs still run."""

    def setUp(self) -> None:
        super().setUp()
        self.fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        self.live_domain = state.ensure_private_dir(self.root / "cc-daemon-test")
        saved = dict(probe._ISOLATION_CACHE)
        self.addCleanup(lambda: (probe._ISOLATION_CACHE.clear(), probe._ISOLATION_CACHE.update(saved)))

    def test_missing_bwrap_fails_closed(self) -> None:
        script = self._write_fake_executable()
        with mock.patch.object(probe.shutil, "which", return_value=None):
            reason = probe.network_isolation_available(refresh=True)
            self.assertIn("bwrap", reason or "")
            with self.assertRaisesRegex(
                ProbeError, "refusing real native execution: network isolation unavailable"
            ):
                probe.run_native(
                    [],
                    trusted=self._trusted(script, fake=False),
                    fixture=self.fixture,
                    allow_real=True,
                    environ=self.environ,
                    live_daemon_domain=self.live_domain,
                )
            with self.assertRaisesRegex(ProbeError, "network isolation unavailable"):
                probe.run_native_pty(
                    [],
                    (),
                    trusted=self._trusted(script, fake=False),
                    fixture=self.fixture,
                    allow_real=True,
                    environ=self.environ,
                    live_daemon_domain=self.live_domain,
                )
            # Fake unit specs never need the namespace.
            result = probe.run_native(
                [], trusted=self._trusted(script), fixture=self.fixture, timeout=15
            )
            self.assertEqual(result.returncode, 0)

    def test_untrusted_bwrap_is_refused(self) -> None:
        fake_bwrap = self._write_fake_executable("bwrap", "print('fake')\n")
        with mock.patch.object(probe.shutil, "which", return_value=str(fake_bwrap)):
            reason = probe.network_isolation_available(refresh=True)
        self.assertIn("untrusted bwrap", reason or "")
        self.assertIn("not root-owned", reason or "")

    def _stub_bwrap(self):
        # A resolvable stand-in so these tests also run where no bwrap
        # exists (the Nix sandbox); only the namespace outcome is mocked.
        return mock.patch.object(
            probe, "_resolve_bwrap", return_value=Path("/usr/bin/bwrap")
        )

    def test_namespace_creation_failure_fails_closed(self) -> None:
        failing = mock.Mock(returncode=1, stdout="", stderr="setting up uid map: Permission denied")
        with self._stub_bwrap(), mock.patch.object(
            probe.subprocess, "run", return_value=failing
        ):
            reason = probe.network_isolation_available(refresh=True)
        self.assertIn("could not create a network namespace", reason or "")

    def test_non_loopback_namespace_fails_closed(self) -> None:
        leaky = mock.Mock(returncode=0, stdout=f"uid={os.geteuid()} ifaces=eth0,lo\n", stderr="")
        with self._stub_bwrap(), mock.patch.object(
            probe.subprocess, "run", return_value=leaky
        ):
            reason = probe.network_isolation_available(refresh=True)
        self.assertIn("not loopback-only", reason or "")

    def test_pinned_binary_gate_reports_boundaries(self) -> None:
        missing = probe.pinned_binary_gate(self.root / "absent.json", what="probes")
        self.assertTrue(missing.boundary.startswith("BOUNDARY: "))
        malformed = self.root / "malformed.json"
        malformed.write_text("{not json")
        with mock.patch.object(probe, "network_isolation_available", return_value=None):
            gate = probe.pinned_binary_gate(malformed, what="probes")
        self.assertIn("unreadable or malformed", gate.boundary)
        self.assertIsNone(gate.trusted)
        not_object = self.root / "list.json"
        not_object.write_text("[]")
        with mock.patch.object(probe, "network_isolation_available", return_value=None):
            gate = probe.pinned_binary_gate(not_object, what="probes")
        self.assertIn("not a JSON object", gate.boundary)
        contract = self._contract_file(self._write_fake_executable())
        with mock.patch.object(
            probe, "network_isolation_available", return_value="network isolation unavailable: x"
        ):
            gate = probe.pinned_binary_gate(contract, what="probes")
        self.assertIn("loopback-only network namespace", gate.boundary)
        with mock.patch.object(probe, "network_isolation_available", return_value=None):
            gate = probe.pinned_binary_gate(contract, what="probes", environ=self.environ)
        self.assertIsNone(gate.boundary)
        self.assertIsNotNone(gate.trusted)
        self.assertEqual(gate.version, "2.1.217")


class ProbeEnvironmentBoundaryTests(ProbeTestCase):
    """Tokens, PATH dirs, executable overwrite, tmpdir."""

    def setUp(self) -> None:
        super().setUp()
        self.fixture = probe.build_fixture(self.fixture_root, environ=self.environ)

    def test_credential_shaped_tokens_are_refused(self) -> None:
        for token in (
            "sk-ant-api03-" + "A" * 95,
            "sk-ant-short-id",
            "sk-proj-abcdef",
            "SK-upper-case",
            "x" * 41,
        ):
            with self.subTest(token=token[:12]):
                with self.assertRaisesRegex(ProbeError, "never credential-shaped"):
                    self.fixture.environ(base_url="http://127.0.0.1:8317", token=token)
                with self.assertRaises(ProbeError):
                    probe.FakeAnthropicProvider(known_tokens={token: "A"})
        self.assertEqual(
            self.fixture.environ(base_url="http://127.0.0.1:8317", token="x" * 40)[
                "ANTHROPIC_AUTH_TOKEN"
            ],
            "x" * 40,
        )

    def test_path_dirs_require_exact_0700_and_refuse_reserved_dirs(self) -> None:
        readonly = self.fixture.root / "ro-bin"
        readonly.mkdir(mode=0o500)
        os.chmod(readonly, 0o500)
        self.addCleanup(os.chmod, readonly, 0o700)
        with self.assertRaisesRegex(ProbeError, "exactly mode 0700"):
            probe.validate_fixture_path_dirs(self.fixture, (readonly,))
        for reserved, needle in (
            (self.fixture.claude_config_dir, "Claude config root"),
            (self.fixture.project_dir, "project directory"),
        ):
            with self.subTest(reserved=reserved):
                with self.assertRaisesRegex(ProbeError, needle):
                    probe.validate_fixture_path_dirs(self.fixture, (reserved,))
        nested = state.ensure_private_dir(self.fixture.project_dir / ".bin")
        with self.assertRaisesRegex(ProbeError, "project directory"):
            probe.validate_fixture_path_dirs(self.fixture, (nested,))

    def test_write_fixture_executable_never_overwrites_data_files(self) -> None:
        bin_dir = state.ensure_private_dir(self.fixture.root / "probe-bin")
        data = bin_dir / "settings.json"
        data.write_text('{"keep": true}')
        os.chmod(data, 0o600)
        with self.assertRaisesRegex(ProbeError, "non-executable file"):
            probe.write_fixture_executable(self.fixture, bin_dir, "settings.json", "#!/bin/sh\n")
        self.assertEqual(data.read_text(), '{"keep": true}')
        first = probe.write_fixture_executable(self.fixture, bin_dir, "tool", "#!/bin/sh\nexit 0\n")
        again = probe.write_fixture_executable(self.fixture, bin_dir, "tool", "#!/bin/sh\nexit 1\n")
        self.assertEqual(first, again)
        self.assertIn(b"exit 1", again.read_bytes())

    def test_fixture_tmpdir_is_private_and_wired(self) -> None:
        info = os.lstat(self.fixture.tmp_dir)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        self.assertEqual(self.fixture.tmp_dir, self.fixture.root / "tmp")
        env = self.fixture.environ()
        self.assertEqual(env["TMPDIR"], str(self.fixture.tmp_dir))
        self.assertEqual(env["CLAUDE_CODE_TMPDIR"], str(self.fixture.tmp_dir))

    def test_unix_provider_socket_serves_the_same_handler(self) -> None:
        run = state.ensure_private_dir(self.fixture.root / "run")
        with probe.FakeAnthropicProvider() as provider:
            path = provider.unix_socket(run)
            self.assertEqual(provider.unix_socket(run), path)
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
            connection = probe.UnixHTTPConnection(path, timeout=5)
            connection.request(
                "POST",
                "/v1/messages",
                body=b'{"model":"m","max_tokens":1,"messages":[]}',
                headers={"Content-Type": "application/json", "x-api-key": probe.DUMMY_TOKEN},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            self.assertEqual([record.auth for record in provider.requests], ["dummy"])
        self.assertFalse(path.exists())
        loose = self.fixture.root / "loose-run"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        os.chmod(loose, 0o755)
        with probe.FakeAnthropicProvider() as provider:
            with self.assertRaisesRegex(ProbeError, "owner-private 0700"):
                provider.unix_socket(loose)


class HomeViewTests(unittest.TestCase):
    def test_mount_order_and_validation(self):
        home = Path("/fixture/home")
        view = probe.HomeView(home, (home / "data/auth",), (home / "data",), (home / "config/optional",))
        argv = view.argv()
        self.assertEqual(argv[:2], ["--tmpfs", str(home)])
        self.assertEqual(argv[-2:], ["--remount-ro", str(home)])
        self.assertLess(argv.index("--ro-bind"), argv.index("--bind"))
        self.assertIn("--ro-bind-try", argv)
        for path in (Path("relative"), Path("/outside"), home, home / "../outside"):
            with self.subTest(path=path), self.assertRaises(probe.ProbeError):
                probe.HomeView(home, (path,), ()).argv()
        with self.assertRaises(probe.ProbeError):
            probe.HomeView(Path("relative"), (), ()).argv()

    def test_writable_symlink_escape_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            (home / "escape").symlink_to(Path(tmp))
            with self.assertRaises(probe.ProbeError):
                probe.HomeView(home, (home / "escape",), ()).argv()


class FollowupHarnessTests(ProbeTestCase):
    def test_retry_headers_and_legacy_tuple(self):
        import urllib.error
        import urllib.request

        for reply in (
            probe.ProviderReply(429, {"type": "error"}, (("retry-after", "3600"),)),
            (429, {"type": "error"}),
        ):
            with probe.FakeAnthropicProvider(responder=lambda d, p: reply) as provider:
                request = urllib.request.Request(
                    provider.base_url + "/v1/messages", data=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                with caught.exception as response:
                    self.assertEqual(response.code, 429)
                    self.assertEqual(response.headers.get("retry-after"),
                                     "3600" if isinstance(reply, probe.ProviderReply) else None)

    def test_retry_header_validation(self):
        for name in ("retry-after", "retry-after-ms", "x-should-retry"):
            for value in ("0", "999999", "true", "false"):
                probe.ProviderReply(429, {}, ((name, value),))
        for pair in (("set-cookie", "1"), ("retry-after", "1000000"),
                     ("retry-after", "-1"), ("retry-after", "1\r\nX: a"),
                     ("retry-after", "")):
            with self.subTest(pair=pair), self.assertRaises(probe.ProbeError):
                probe.ProviderReply(429, {}, (pair,))

    def test_fixture_settings_private_atomic_and_confined(self):
        fixture = probe.build_fixture(self.fixture_root, environ=self.environ)
        target = probe.write_fixture_settings(fixture, "scope/settings.json", {"env": {}})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        probe.write_fixture_settings(fixture, "scope/settings.json", {"model": "probe-lead"})
        self.assertEqual(strict_json.loads(target.read_bytes()), {"model": "probe-lead"})
        (fixture.root / "escape").symlink_to(self.root, target_is_directory=True)
        for name in ("../bad.json", "/tmp/bad.json", "escape/bad.json", ""):
            with self.subTest(name=name), self.assertRaises(probe.ProbeError):
                probe.write_fixture_settings(fixture, name, {})
        policy = probe.ProbeCompactionPolicy(200000, 90, 150000)
        self.assertEqual(policy.settings_env(), policy.environ())

    def test_pid_namespace_opt_in(self):
        plain = probe._isolation_prefix(Path("/usr/bin/bwrap"), None)
        isolated = probe._isolation_prefix(Path("/usr/bin/bwrap"), None, pid_namespace=True)
        self.assertNotIn("--unshare-pid", plain)
        self.assertEqual(isolated[6:9], ["--unshare-pid", "--proc", "/proc"])

    def test_spawn_environment_bounds(self):
        for name, high in (("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", 64),
                           ("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", 16),
                           ("CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION", 1000)):
            for value in ("1", str(high)):
                self.assertEqual(probe.validate_probe_env({name: value}), {name: value})
            for value in ("0", str(high + 1)):
                with self.assertRaises(probe.ProbeError):
                    probe.validate_probe_env({name: value})


if __name__ == "__main__":
    unittest.main()


class ForcePolicyMatrix(unittest.TestCase):
    def test_force_env_absent_empty_zero_false_enabled_matrix(self):
        from claude_multi import compiler
        name = 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE'
        # This is the managed-policy matrix, not evidence that the native
        # client interprets "0" or "false" as disabled. Every spelling is unset.
        for value in (None, '', '0', 'false', '1', 'true'):
            inherited = {} if value is None else {name: value}
            unset = compiler.v2_env_unset(scalar=None, secret_env_names=(), launch_environ=inherited)
            with self.subTest(value=value):
                self.assertIn(name, unset)
                self.assertNotIn(name, {k:v for k,v in inherited.items() if k not in unset})
