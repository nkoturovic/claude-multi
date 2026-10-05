"""Gateway auth-dir tools — `snapshot-auth` and `discover openai`.

Temp HOME only; no real provider call (every listing uses an injected
fetch), no systemd query (an injected service probe), no real auth dir.
Credential fixtures carry obvious dummy tokens so a leak into output or an
error message is detectable.
"""

from __future__ import annotations

import base64
import datetime
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import urllib.error
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claude_multi import service, catalog, cli, proxy, sessions
from _catalog import FIXTURE_ROOT
from _v4 import V4Case


NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc)
TODAY = datetime.date(2026, 9, 25)
DUMMY_ACCESS = "dummy-access-token-DO-NOT-PRINT"
DUMMY_REFRESH = "dummy-refresh-token-DO-NOT-PRINT"


def _jwt(exp: datetime.datetime) -> str:
    def part(doc: dict) -> str:
        raw = json.dumps(doc).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': int(exp.timestamp())})}.sig-{DUMMY_ACCESS}"


class _AuthHomeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="claude-multi-authtools-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.gateway_info = catalog.load_catalog(FIXTURE_ROOT).docs["gateway"]["gateway"]
        self.auth_dir = self.home / self.gateway_info["auth_dir"]
        self.auth_dir.mkdir(parents=True, mode=0o700)
        for parent in self.auth_dir.relative_to(self.home).parents:
            os.chmod(self.home / parent, 0o700)
        self.environ = {"HOME": str(self.home), "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT)}

    def write_credential(self, name: str, document: dict, mode: int = 0o600) -> Path:
        path = self.auth_dir / name
        path.write_text(json.dumps(document))
        os.chmod(path, mode)
        return path

    def codex_doc(self, **overrides) -> dict:
        doc = {
            "type": "codex",
            "access_token": _jwt(NOW + datetime.timedelta(hours=1)),
            "refresh_token": DUMMY_REFRESH,
            "account_id": "acct-dummy",
            "email": "dummy@example.invalid",
            "expired": (NOW + datetime.timedelta(hours=1)).isoformat(),
        }
        doc.update(overrides)
        return doc


class SnapshotAuthTests(_AuthHomeCase):
    def test_snapshot_copies_private_and_prints_names_only(self) -> None:
        self.write_credential("codex-dummy-pro.json", self.codex_doc())
        self.write_credential("claude-dummy.json", {"type": "claude", "access_token": DUMMY_ACCESS})
        target, names = proxy.snapshot_auth_dir(
            self.gateway_info, environ=self.environ, today=TODAY
        )
        self.assertEqual(target, self.auth_dir.parent / "auth.pre-7.3.20260925")
        self.assertEqual(sorted(names), ["claude-dummy.json", "codex-dummy-pro.json"])
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o700)
        for name in names:
            with self.subTest(name=name):
                copied = target / name
                self.assertEqual(stat.S_IMODE(os.lstat(copied).st_mode), 0o600)
                self.assertEqual(copied.read_bytes(), (self.auth_dir / name).read_bytes())
        # The live dir is untouched and no staging dir is left behind.
        self.assertEqual(sorted(p.name for p in self.auth_dir.iterdir()), sorted(names))
        leftovers = [p.name for p in self.auth_dir.parent.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_patch8_save_temp_is_skipped_before_stat_even_if_it_disappears(self) -> None:
        self.write_credential("claude-dummy.json", {"type": "claude", "access_token": DUMMY_ACCESS})
        temp = self.auth_dir / ".claude-dummy.json.cm-save-0123456789abcdef"
        temp.write_text('{"access_token": "partial')
        real_stat = os.lstat
        def no_temp_stat(path, *args, **kwargs):
            self.assertNotEqual(Path(path), temp, "save temp must not be statted or opened")
            return real_stat(path, *args, **kwargs)
        with mock.patch.object(os, "lstat", side_effect=no_temp_stat):
            target, names = proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)
        self.assertEqual(names, ["claude-dummy.json"])
        self.assertFalse((target / temp.name).exists())
        self.assertTrue(temp.exists(), "snapshot never removes live staging files")

    def test_save_temp_lookalike_ending_in_json_is_still_checked(self) -> None:
        name = ".claude-dummy.json.cm-save-0123456789abcdef.json"
        (self.auth_dir / name).write_text('{"partial":')
        with self.assertRaisesRegex(proxy.ProxyError, "not valid JSON"):
            proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)

    def test_cli_output_is_path_count_and_names_never_contents(self) -> None:
        self.write_credential("codex-dummy-pro.json", self.codex_doc())
        out = io.StringIO()
        with mock.patch.object(proxy.datetime, "date", wraps=datetime.date) as fake_date:
            fake_date.today.return_value = TODAY
            with redirect_stdout(out):
                code = proxy.main(["snapshot-auth"], environ=self.environ)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("auth.pre-7.3.20260925", text)
        self.assertIn("files: 1", text)
        self.assertIn("codex-dummy-pro.json", text)
        self.assertIn("--restore auth.pre-7.3.20260925", text)
        for secret in (DUMMY_ACCESS, DUMMY_REFRESH, "acct-dummy", "dummy@example"):
            self.assertNotIn(secret, text)

    def test_never_overwrites_an_existing_snapshot(self) -> None:
        self.write_credential("codex-a.json", self.codex_doc())
        first, _ = proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)
        marker = first / "codex-a.json"
        before = marker.read_bytes()
        self.write_credential("codex-a.json", self.codex_doc(account_id="changed"))
        second, _ = proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)
        self.assertEqual(second.name, "auth.pre-7.3.20260925.2")
        self.assertEqual(marker.read_bytes(), before, "the first snapshot must stay intact")
        third, _ = proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)
        self.assertEqual(third.name, "auth.pre-7.3.20260925.3")

    def test_symlinked_member_is_refused_and_nothing_is_kept(self) -> None:
        self.write_credential("codex-a.json", self.codex_doc())
        outside = self.root / "outside.json"
        outside.write_text("{}")
        os.symlink(outside, self.auth_dir / "codex-link.json")
        with self.assertRaisesRegex(proxy.ProxyError, "symlink"):
            proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)
        siblings = sorted(p.name for p in self.auth_dir.parent.iterdir())
        self.assertEqual(siblings, ["auth"], "no snapshot or staging dir may remain")

    def test_symlinked_auth_dir_is_refused(self) -> None:
        real = self.root / "real-auth"
        shutil.move(str(self.auth_dir), real)
        os.symlink(real, self.auth_dir)
        with self.assertRaisesRegex(proxy.ProxyError, "not a real directory"):
            proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)

    def test_group_readable_auth_dir_is_refused(self) -> None:
        os.chmod(self.auth_dir, 0o750)
        with self.assertRaisesRegex(proxy.ProxyError, "owner-private"):
            proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)

    def test_torn_json_record_is_refused(self) -> None:
        (self.auth_dir / "codex-torn.json").write_text('{"access_token": "dum')
        os.chmod(self.auth_dir / "codex-torn.json", 0o600)
        with self.assertRaisesRegex(proxy.ProxyError, "not valid JSON") as ctx:
            proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)
        self.assertNotIn("dum", str(ctx.exception).replace("codex-torn", ""))
        self.assertEqual(sorted(p.name for p in self.auth_dir.parent.iterdir()), ["auth"])

    def test_restore_refuses_while_the_gateway_runs(self) -> None:
        self.write_credential("codex-a.json", self.codex_doc())
        snap, _ = proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)
        with self.assertRaisesRegex(proxy.ProxyError, "while cli-proxy-api is running"):
            proxy.restore_auth_dir(
                self.gateway_info, snap.name, environ=self.environ,
                service_active=lambda unit: True, gateway_status=self.status(True),
            )
        self.assertTrue((self.auth_dir / "codex-a.json").is_file())

    def test_unlistable_proc_with_no_stamp_refuses_restore_and_preserves_auth_tree(self) -> None:
        self.write_credential("codex-a.json", self.codex_doc())
        snap, _ = proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)
        self.write_credential("current-only.json", {"type": "claude", "access_token": DUMMY_ACCESS})
        proc_root = self.root / "proc"
        proc_root.mkdir(mode=0o000)
        self.addCleanup(proc_root.chmod, 0o700)
        self.assertTrue(proc_root.is_dir(), "existence alone must not authorize restore")
        zero = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "0", ""))
        health = mock.Mock(side_effect=ConnectionRefusedError())
        state_root = sessions.state_root(self.environ)
        self.assertIsNone(service.read_exec_stamp(service.gateway_workdir(state_root)))

        def observe(**kwargs):
            return service.gateway_status(
                **kwargs, proc_root=proc_root, health_get=health,
                pid_get=lambda **kw: service.gateway_pid(**kw, runner=zero),
            )

        def tree():
            return {str(path.relative_to(self.auth_dir.parent)):
                    (None if path.is_dir() else path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                    for path in self.auth_dir.parent.rglob("*")}

        before = tree()
        listdir = os.listdir

        def unreadable(path):
            if Path(path) == proc_root:
                raise PermissionError("fixture process table is unreadable")
            return listdir(path)

        # Inject the permission failure too, so privileged test runners cannot
        # bypass the 0o000 mode and accidentally exercise the observable first start.
        with mock.patch.object(os, "listdir", side_effect=unreadable):
            self.assertFalse(sessions.proc_session_scan(proc_root).known)
            status = observe(base_url=self.gateway_info["base_url"], health_path="/healthz",
                             state_root=state_root, manager=lambda: "inactive")
            self.assertEqual(status.state, "unknown")
            with self.assertRaisesRegex(proxy.ProxyError, "refusing to restore while .* is unknown"):
                proxy.restore_auth_dir(
                    self.gateway_info, snap.name, environ=self.environ,
                    service_active=lambda _unit: False, gateway_status=observe, today=TODAY,
                )
        self.assertEqual(tree(), before)
        self.assertEqual(health.call_count, 2)
        self.assertEqual(zero.call_count, 2)

    def test_restore_puts_the_snapshot_back_and_keeps_the_replaced_dir(self) -> None:
        self.write_credential("codex-dummy-pro.json", self.codex_doc())
        snap, _ = proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)
        original = (snap / "codex-dummy-pro.json").read_bytes()
        # 7.3 renames the codex record and rewrites its metadata on save.
        (self.auth_dir / "codex-dummy-pro.json").unlink()
        self.write_credential("codex-acct-dummy@example.invalid-pro.json", self.codex_doc(type="codex73"))
        live, moved, names = proxy.restore_auth_dir(
            self.gateway_info, snap.name, environ=self.environ,
            service_active=lambda unit: False, gateway_status=self.status(False), today=TODAY,
        )
        self.assertEqual(live, self.auth_dir)
        self.assertEqual(names, ["codex-dummy-pro.json"])
        self.assertEqual((live / "codex-dummy-pro.json").read_bytes(), original)
        self.assertEqual(stat.S_IMODE(os.lstat(live).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(live / "codex-dummy-pro.json").st_mode), 0o600)
        # Nothing is deleted: the 7.3 dir is moved aside, the snapshot kept.
        self.assertEqual(moved, self.auth_dir.parent / "auth.replaced.20260925")
        self.assertTrue((moved / "codex-acct-dummy@example.invalid-pro.json").is_file())
        self.assertEqual((snap / "codex-dummy-pro.json").read_bytes(), original)

    def test_restore_accepts_the_printed_full_path(self) -> None:
        self.write_credential("codex-a.json", self.codex_doc())
        snap, _ = proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)
        live, _moved, names = proxy.restore_auth_dir(
            self.gateway_info, str(snap), environ=self.environ,
            service_active=lambda unit: False, gateway_status=self.status(False), today=TODAY,
        )
        self.assertEqual(names, ["codex-a.json"])
        self.assertEqual(live, self.auth_dir)

    def test_restore_refuses_names_that_are_not_snapshots(self) -> None:
        other = self.auth_dir.parent / "elsewhere"
        other.mkdir(mode=0o700)
        for bad in ("../elsewhere", "elsewhere", "auth.replaced.20260925",
                    str(self.root / "auth.pre-7.3.20260925"), "auth.pre-7.3.2026"):
            with self.subTest(snapshot=bad):
                with self.assertRaises(proxy.ProxyError):
                    proxy.restore_auth_dir(
                        self.gateway_info, bad, environ=self.environ,
                        service_active=lambda unit: False, gateway_status=self.status(False),
                    )

    def test_restore_cli_refusal_is_one_line_exit_1(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = proxy.main(
                ["snapshot-auth", "--restore", "auth.pre-7.3.20260925"],
                environ=self.environ,
                service_active=lambda unit: True, gateway_status=self.status(True),
            )
        self.assertEqual(code, 1)
        self.assertIn("claude-multi gateway stop", err.getvalue())

    @staticmethod
    def status(running):
        def observe(**kwargs):
            health = mock.Mock(return_value=200) if running else mock.Mock(side_effect=ConnectionRefusedError())
            return service.gateway_status(
                **kwargs, health_get=health, pid_get=lambda **_: (42 if running else None, running),
            )
        return observe

    def test_legacy_injected_restore_path_never_requests_health(self) -> None:
        with mock.patch.object(service, "_health_get", side_effect=AssertionError("network")) as health:
            for active in (False, True):
                with self.subTest(active=active), self.assertRaisesRegex(proxy.ProxyError, "without health and PID"):
                    proxy.restore_auth_dir(
                        self.gateway_info, "auth.pre-7.3.20260925", environ=self.environ,
                        service_active=lambda _: active,
                    )
            health.assert_not_called()

    def test_restore_refuses_unknown_health_or_live_pid_despite_inactive_manager(self) -> None:
        for health, alive in ((503, False), (ConnectionRefusedError(), True)):
            with self.subTest(health=health, alive=alive):
                getter = mock.Mock(side_effect=health) if isinstance(health, Exception) else mock.Mock(return_value=health)
                def observe(**kwargs):
                    return service.gateway_status(**kwargs, health_get=getter, pid_get=lambda **_: (42, alive))
                with self.assertRaisesRegex(proxy.ProxyError, "refusing to restore"):
                    proxy.restore_auth_dir(
                        self.gateway_info, "auth.pre-7.3.20260925", environ=self.environ,
                        service_active=lambda _: False, gateway_status=observe,
                    )

    def test_restore_from_private_pid_and_network_namespaces_refuses_without_user_bus(self) -> None:
        # Reproduce pidns.py through injected /proc/health/manager observations,
        # never a live unshare or a real gateway/auth directory.
        self.write_credential("fixture.json", {"fixture": "snapshot"})
        snap, _ = proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)
        self.write_credential("fixture.json", {"fixture": "current"})
        before = (self.auth_dir / "fixture.json").read_bytes()
        root = proxy.sessions_mod.state_root(self.environ)
        workdir = service.ensure_gateway_workdir(root)
        proc = self.root / "proc"
        proc.mkdir()
        manager = mock.Mock(side_effect=FileNotFoundError("no user bus"))
        for namespace in ("pid:[101]", None):
            with self.subTest(namespace=namespace):
                service.write_exec_stamp(workdir, service.ExecStamp(
                    "v", "s", 42, "/fixture/gateway", NOW.isoformat(), namespace))
                def observe(**kwargs):
                    kwargs["manager"] = lambda: None
                    return service.gateway_status(
                        **kwargs, proc_root=proc, health_get=mock.Mock(side_effect=ConnectionRefusedError()),
                        pid_get=lambda **kw: service.gateway_pid(
                            **kw, runner=manager, readlink=lambda _: "pid:[202]"))
                with mock.patch.object(service, "_health_get", side_effect=AssertionError("network")), \
                     self.assertRaisesRegex(proxy.ProxyError, "refusing to restore while .* is unknown"):
                    proxy.restore_auth_dir(self.gateway_info, snap.name, environ=self.environ,
                                           gateway_status=observe)
                self.assertEqual((self.auth_dir / "fixture.json").read_bytes(), before)
                self.assertEqual(sorted(p.name for p in self.auth_dir.parent.iterdir()),
                                 sorted([self.auth_dir.name, snap.name]))

    def test_restore_cli_passes_complete_injected_status(self) -> None:
        self.write_credential("codex-a.json", self.codex_doc())
        snap, _ = proxy.snapshot_auth_dir(self.gateway_info, environ=self.environ, today=TODAY)
        with mock.patch.object(service, "_health_get", side_effect=AssertionError("network")) as health:
            with redirect_stdout(io.StringIO()):
                code = proxy.main(["snapshot-auth", "--restore", snap.name], environ=self.environ,
                                  service_active=lambda _: False, gateway_status=self.status(False))
            self.assertEqual(code, 0)
            health.assert_not_called()


class CodexPlanListingTests(_AuthHomeCase):
    LISTING = json.dumps(
        {
            "models": [
                {"slug": "gpt-6-astra", "display_name": "x", "base_instructions": DUMMY_ACCESS},
                {"slug": "gpt-6-sol"},
                {"slug": "gpt-6-luna"},
                {"slug": "gpt-6-sol"},
                {"slug": "bad slug with spaces"},
                {"no_slug": True},
                "junk",
            ]
        }
    ).encode()

    def test_one_get_with_upstream_headers_and_ids_only(self) -> None:
        self.write_credential("codex-dummy-pro.json", self.codex_doc())
        calls = []

        def fetch(url, headers):
            calls.append((url, dict(headers)))
            return self.LISTING

        listed = proxy.list_codex_plan_models(
            self.gateway_info, environ=self.environ, fetch=fetch, now=NOW
        )
        self.assertEqual(
            [model["id"] for model in listed], ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna"]
        )
        # No instruction text or other listing fields ride along.
        for model in listed:
            self.assertEqual(set(model), {"id", "visibility", "upgrade", "retirement_at"})
        self.assertEqual(len(calls), 1)
        url, headers = calls[0]
        self.assertEqual(
            url, "https://chatgpt.com/backend-api/codex/models?client_version=0.159.1"
        )
        self.assertTrue(headers["Authorization"].startswith("Bearer "))
        self.assertEqual(headers["Chatgpt-Account-Id"], "acct-dummy")
        self.assertEqual(headers["Originator"], "codex_cli_rs")
        self.assertTrue(headers["User-Agent"].startswith("codex_cli_rs/"))

    def test_upgrade_retirement_and_visibility_are_kept(self) -> None:
        # The codex successor pointer, the
        # retirement date (inside `upgrade` or top-level) and the visibility
        # survive the listing; malformed values read as None.
        self.write_credential("codex-dummy-pro.json", self.codex_doc())
        listing = json.dumps({"models": [
            {"slug": "gpt-5.5", "visibility": "list",
             "upgrade": {"model": "gpt-5.6-sol", "migration_markdown": DUMMY_ACCESS,
                         "retirement_at": "2026-10-14T19:00:00Z"}},
            {"slug": "codex-auto-review", "visibility": "hide", "upgrade": None,
             "retirement_at": "2027-01-01T00:00:00Z"},
            {"slug": "gpt-weird", "visibility": "\x1b[31mred",
             "upgrade": {"model": "bad model id"}, "retirement_at": "soon"},
        ]}).encode()
        listed = proxy.list_codex_plan_models(
            self.gateway_info, environ=self.environ, fetch=lambda u, h: listing, now=NOW
        )
        self.assertEqual(listed, [
            {"id": "gpt-5.5", "visibility": "list", "upgrade": "gpt-5.6-sol",
             "retirement_at": "2026-10-14T19:00:00Z"},
            {"id": "codex-auto-review", "visibility": "hide", "upgrade": None,
             "retirement_at": "2027-01-01T00:00:00Z"},
            {"id": "gpt-weird", "visibility": None, "upgrade": None, "retirement_at": None},
        ])
        self.assertNotIn(DUMMY_ACCESS, repr(listed))

    def test_never_writes_the_auth_dir(self) -> None:
        path = self.write_credential("codex-dummy-pro.json", self.codex_doc())
        before = (path.read_bytes(), os.stat(path).st_mtime_ns, sorted(os.listdir(self.auth_dir)))
        proxy.list_codex_plan_models(
            self.gateway_info, environ=self.environ, fetch=lambda u, h: self.LISTING, now=NOW
        )
        after = (path.read_bytes(), os.stat(path).st_mtime_ns, sorted(os.listdir(self.auth_dir)))
        self.assertEqual(before, after)

    def test_expired_token_refuses_without_refresh_or_request(self) -> None:
        past = NOW - datetime.timedelta(minutes=5)
        path = self.write_credential(
            "codex-dummy-pro.json",
            self.codex_doc(access_token=_jwt(past), expired=past.isoformat()),
        )
        before = path.read_bytes()
        fetch = mock.Mock(side_effect=AssertionError("no request on an expired token"))
        with self.assertRaisesRegex(proxy.ProxyError, "let the gateway refresh it") as ctx:
            proxy.list_codex_plan_models(
                self.gateway_info, environ=self.environ, fetch=fetch, now=NOW
            )
        fetch.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(DUMMY_ACCESS, str(ctx.exception))
        self.assertNotIn(DUMMY_REFRESH, str(ctx.exception))

    def test_token_inside_the_refresh_leeway_counts_as_expired(self) -> None:
        soon = NOW + datetime.timedelta(seconds=10)
        self.write_credential(
            "codex-dummy-pro.json", self.codex_doc(access_token=_jwt(soon), expired=soon.isoformat())
        )
        with self.assertRaisesRegex(proxy.ProxyError, "expired"):
            proxy.list_codex_plan_models(
                self.gateway_info, environ=self.environ,
                fetch=lambda u, h: self.LISTING, now=NOW,
            )

    def test_opaque_token_uses_the_file_expiry_and_unknown_expiry_refuses(self) -> None:
        self.write_credential(
            "codex-dummy-pro.json",
            self.codex_doc(access_token=DUMMY_ACCESS),
        )
        listed = proxy.list_codex_plan_models(
            self.gateway_info, environ=self.environ, fetch=lambda u, h: self.LISTING, now=NOW
        )
        self.assertIn("gpt-6-sol", [model["id"] for model in listed])
        doc = self.codex_doc(access_token=DUMMY_ACCESS)
        del doc["expired"]
        self.write_credential("codex-dummy-pro.json", doc)
        with self.assertRaisesRegex(proxy.ProxyError, "expired"):
            proxy.list_codex_plan_models(
                self.gateway_info, environ=self.environ,
                fetch=lambda u, h: self.LISTING, now=NOW,
            )

    def test_group_readable_or_symlinked_credential_is_refused(self) -> None:
        self.write_credential("codex-dummy-pro.json", self.codex_doc(), mode=0o640)
        with self.assertRaisesRegex(proxy.ProxyError, "codex-dummy-pro.json refused"):
            proxy.list_codex_plan_models(
                self.gateway_info, environ=self.environ,
                fetch=lambda u, h: self.LISTING, now=NOW,
            )
        (self.auth_dir / "codex-dummy-pro.json").unlink()
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps(self.codex_doc()))
        os.chmod(target, 0o600)
        os.symlink(target, self.auth_dir / "codex-dummy-pro.json")
        with self.assertRaisesRegex(proxy.ProxyError, "refused"):
            proxy.list_codex_plan_models(
                self.gateway_info, environ=self.environ,
                fetch=lambda u, h: self.LISTING, now=NOW,
            )

    def test_disabled_and_other_pool_records_are_skipped(self) -> None:
        self.write_credential("claude-dummy.json", {"access_token": DUMMY_ACCESS})
        self.write_credential("codex-a-disabled.json", self.codex_doc(disabled=True))
        with self.assertRaisesRegex(proxy.ProxyError, "no enabled codex credential"):
            proxy.list_codex_plan_models(
                self.gateway_info, environ=self.environ,
                fetch=lambda u, h: self.LISTING, now=NOW,
            )

    def test_missing_credential_names_the_login_command(self) -> None:
        with self.assertRaisesRegex(proxy.ProxyError, "claude-multi providers sign-in openai"):
            proxy.list_codex_plan_models(
                self.gateway_info, environ=self.environ,
                fetch=lambda u, h: self.LISTING, now=NOW,
            )

    def test_fetch_failures_never_carry_the_token(self) -> None:
        self.write_credential("codex-dummy-pro.json", self.codex_doc())

        def leaky(url, headers):
            raise RuntimeError(f"boom {headers}")

        for fetch, expected in (
            (leaky, "RuntimeError"),
            (mock.Mock(side_effect=urllib.error.URLError("refused")), "connection error"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaises(proxy.ProxyError) as ctx:
                    proxy.list_codex_plan_models(
                        self.gateway_info, environ=self.environ, fetch=fetch, now=NOW
                    )
                self.assertIn(expected, str(ctx.exception))
                self.assertNotIn(DUMMY_ACCESS, str(ctx.exception))
                self.assertNotIn("acct-dummy", str(ctx.exception))

    def test_unexpected_shape_is_a_clean_error(self) -> None:
        self.write_credential("codex-dummy-pro.json", self.codex_doc())
        for body in (b'{"data": []}', b"[]", b"not json"):
            with self.subTest(body=body):
                with self.assertRaisesRegex(proxy.ProxyError, "unexpected shape"):
                    proxy.list_codex_plan_models(
                        self.gateway_info, environ=self.environ,
                        fetch=lambda u, h, b=body: b, now=NOW,
                    )

    def test_generic_lister_still_refuses_the_pool_and_points_here(self) -> None:
        providers = catalog.load_catalog(FIXTURE_ROOT).docs["providers"]["providers"]
        with self.assertRaisesRegex(proxy.ProxyError, "discover openai"):
            proxy.list_provider_models("openai", providers, fetch=lambda u, h: b"{}")
        self.assertTrue(proxy.listing_uses_pool_credential("openai"))
        self.assertFalse(proxy.listing_uses_pool_credential("kimi"))


class _DiscoverCliCase(_AuthHomeCase):
    """Every discover provider call passes the one human guard
    (consent.require_human) and one y/N naming every request; the approval
    flag is parsed only for its migration error. The terminal probe, the
    codex lister and the listing transport are injected; no request is made."""

    def setUp(self) -> None:
        super().setUp()
        secrets = self.root / "secrets"
        secrets.mkdir(mode=0o700)
        self.secret_file = secrets / "claude.env"
        self.secret_file.write_text("KIMI_CLAUDE_API_KEY=dummy-kimi-DO-NOT-PRINT\n")
        os.chmod(self.secret_file, 0o600)
        self.sent: list[tuple[str, dict]] = []
        self.body = json.dumps({"data": [{"id": "k9", "display_name": "K9", "context_length": 262144}]}).encode()
        self.runtime = self.make_runtime({})
        codex = mock.patch.object(proxy, "list_codex_plan_models", return_value=[
            {"id": "gpt-6-astra", "visibility": None, "upgrade": None, "retirement_at": None},
            {"id": "gpt-6-sol", "visibility": None, "upgrade": None, "retirement_at": None},
        ])
        self.listing = codex.start()
        self.addCleanup(codex.stop)
        self.tty_patch = mock.patch("claude_multi.cli.consent.stdio_ttys", return_value=True)
        self.tty_probe = self.tty_patch.start()
        self.addCleanup(self.tty_patch.stop)

    def transport(self, url, headers, *, deadline, max_bytes):
        self.sent.append((url, dict(headers)))
        return self.body

    def make_runtime(self, extra: dict[str, str]) -> cli.Runtime:
        return cli.Runtime(
            asset_root=FIXTURE_ROOT,
            environ={**self.environ, "XDG_CONFIG_HOME": str(self.root / "config"),
                     "XDG_STATE_HOME": str(self.root / "state"), "TERM": "dumb",
                     "CLAUDE_MULTI_SECRET_ENV": str(self.secret_file), **extra},
            cwd=self.root,
            served_models_callback=lambda gateway, token: (set(), 200),
            health_get=lambda base_url, path: 200,
            listing_transport=self.transport,
        )

    def run_cli(self, argv, text=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(argv, runtime=self.runtime, input_stream=io.StringIO(text or ""),
                            output_stream=out, interactive=True)
        return code, out.getvalue(), err.getvalue()


class DiscoverOpenAICliTests(_DiscoverCliCase):
    """`claude-multi discover openai`: its own single-provider consent."""

    def test_prompt_states_the_exact_url_the_pool_credential_and_the_caps(self) -> None:
        code, out, err = self.run_cli(["discover", "openai"], text="n\n")
        self.assertEqual(code, 0)
        self.assertIn("claude-multi will make ONE request to a provider:\n"
                      f"  1. GET {proxy.CODEX_MODELS_URL}\n"
                      "     auth: one codex pool access credential from the gateway auth dir "
                      "(value not shown; nothing is refreshed or written)\n", err)
        self.assertIn("caps: 20 s wall-clock; 4 MiB response; no redirects or retries", err)
        self.assertIn("Proceed with these listed requests? [y/N]", err)
        self.assertIn("declined — nothing sent", err)
        self.assertEqual(out, "")
        self.listing.assert_not_called()

    def test_default_answer_is_no(self) -> None:
        code, _out, _err = self.run_cli(["discover", "openai"], text="\n")
        self.assertEqual(code, 0)
        self.listing.assert_not_called()

    def test_yes_makes_exactly_one_call_through_the_bounded_transport(self) -> None:
        code, out, _err = self.run_cli(["discover", "openai"], text="y\n")
        self.assertEqual(code, 0)
        self.listing.assert_called_once()
        self.assertEqual([line.split("\t")[0] for line in out.splitlines()], ["gpt-6-astra", "gpt-6-sol"])
        fetch = self.listing.call_args.kwargs["fetch"]
        self.assertEqual(fetch("https://example.invalid/x", {"a": "b"}), self.body)
        self.assertEqual(self.sent, [("https://example.invalid/x", {"a": "b"})])

    def test_marks_cataloged_retired_and_candidate_ids_with_metadata(self) -> None:
        cataloged = {line["wire_model"]: key for key, line in sorted(self.runtime.catalog.lines.items())
                     if line["provider"] == "openai"}
        wire, key = sorted(cataloged.items())[0]
        retired = {"provider": "openai", "last_wire": "gpt-retired-wire"}
        self.listing.return_value = [
            {"id": wire, "visibility": "list", "upgrade": "gpt-next", "retirement_at": "2026-10-14T19:00:00Z"},
            {"id": "gpt-retired-wire", "visibility": None, "upgrade": None, "retirement_at": None},
            {"id": "gpt-brand-new", "visibility": "hide", "upgrade": None, "retirement_at": None},
        ]
        with mock.patch.dict(self.runtime.catalog.docs["retired"]["retired"], {"oldline": retired}):
            code, out, _err = self.run_cli(["discover", "openai"], text="y\n")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"{wire}\tcataloged as {key} visibility=list upgrade=gpt-next "
                                   "retires=2026-10-14T19:00:00Z")
        self.assertEqual(lines[1], "gpt-retired-wire\tretired oldline (continuity retained)")
        self.assertEqual(lines[2], "gpt-brand-new\tcandidate visibility=hide")

    def test_the_approval_flag_is_a_migration_error(self) -> None:
        for argv in (["discover", "openai", "--yes-i-approve-this-provider-call"],
                     ["discover", "kimi", "--yes-i-approve-this-provider-call"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(argv, text="y\n")
                self.assertEqual(code, 2)
                self.assertIn("--yes-i-approve-this-provider-call cannot replace interactive approval;\n"
                              "run discover in a terminal outside Claude Code sessions.", err)
                self.assertEqual(out, "")
        self.listing.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_listing_errors_are_refusals_without_secret_text(self) -> None:
        self.listing.side_effect = proxy.ProxyError("codex access token expired (x)")
        code, out, err = self.run_cli(["discover", "openai"], text="y\n")
        self.assertEqual(code, 1)
        self.assertIn("codex access token expired", err)
        self.assertEqual(out, "")


class DiscoverInClaudeSessionTests(_DiscoverCliCase):
    """In-session y/N cannot authorize a provider call. Inside a Claude session (a
    marker present, even empty) or without terminal stdio every discover
    mode refuses before any plan, secret read or request."""

    def test_markers_and_redirected_stdio_refuse_every_mode(self) -> None:
        cases = [({"CLAUDECODE": "1"}, True, "CLAUDECODE set"),
                 ({"CLAUDE_MULTI_MANAGED_ID": ""}, True, "CLAUDE_MULTI_MANAGED_ID set"),
                 ({}, False, "stdin/stdout is not a terminal")]
        for extra, tty, reason in cases:
            self.runtime = self.make_runtime(extra)
            self.tty_probe.return_value = tty
            for argv in (["discover", "openai"], ["discover", "kimi"], ["discover", "--all"],
                         ["discover", "--feed"]):
                verb = " ".join(argv)
                with self.subTest(reason=reason, argv=argv), \
                        mock.patch("claude_multi.secret_store.FileSecretStore._values",
                                   side_effect=AssertionError("secret read before the guard")):
                    code, out, err = self.run_cli(argv, text="y\n")
                    self.assertEqual(code, 1, err)
                    self.assertEqual(
                        err.strip(),
                        f"claude-multi: {verb}: needs a terminal outside Claude Code sessions ({reason}) "
                        "— run it in a separate shell")
                    self.assertEqual(out, "")
        self.listing.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_outside_a_session_a_direct_listing_sends_one_keyed_request(self) -> None:
        code, out, err = self.run_cli(["discover", "kimi"], text="y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("auth: header x-api-key from KIMI_CLAUDE_API_KEY (value not shown)", err)
        self.assertNotIn("DO-NOT-PRINT", err + out)
        self.assertEqual(len(self.sent), 1)
        url, headers = self.sent[0]
        self.assertEqual(url, "https://api.kimi.com/coding/v1/models")
        self.assertEqual(headers["x-api-key"], "dummy-kimi-DO-NOT-PRINT")
        self.assertEqual(out.splitlines()[0], "k9\tcandidate context=262144")

    def test_unsupported_listing_refuses_with_zero_requests(self) -> None:
        code, _out, err = self.run_cli(["discover", "qwen"], text="y\n")
        self.assertEqual(code, 1)
        self.assertIn("model listing unsupported", err)
        self.assertIn("declare manually: claude-multi models add qwen", err)
        self.assertEqual(self.sent, [])

    def test_tty_probe_is_the_process_stdio(self) -> None:
        from claude_multi.cli.commands import discover as discover_mod

        self.tty_patch.stop()
        try:
            with mock.patch('claude_multi.cli.streams._stdio_streams_are_ttys', return_value=False) as probe:
                self.assertFalse(discover_mod._provider_call_tty())
            probe.assert_called_once_with()
        finally:
            self.tty_patch.start()


class ReloadFailureVisibilityTests(V4Case):
    """Health 200 cannot hide an exit-1 reload, even without stamps."""
    def test_doctor_reports_preexec_failure_despite_older_success_and_healthy_gateway(self):
        from claude_multi import launch
        self.runtime.doctor_callback = None
        self.runtime.doctor_daemon_callback = lambda: launch.DaemonStatus("absent", "fixture")
        workdir = service.ensure_gateway_workdir(self.runtime.session_store.root)
        service.write_reload_attempt(workdir, "2026-09-27T01:00:00+00:00", "v")
        service.write_reload_stamp(workdir, "reloaded", "2026-09-27T01:00:01+00:00", "v")
        runner = mock.Mock(return_value=mock.Mock(returncode=0,
            stdout="{ path=/fixture/broken-wrapper ; start_time=[Sun 2026-09-27 02:00:00 UTC] ; "
                   "stop_time=[Sun 2026-09-27 02:00:01 UTC] ; pid=123 ; code=exited ; status=203 }"))
        def attention():
            return cli._collect_doctor_reports(
                self.runtime, reload_failed=lambda: service.last_reload_result(runner=runner))[2]
        self.assertEqual(self.runtime.health_get("fixture", "/healthz"), 200)
        self.assertTrue(any("gateway restart pending" in line for line in attention()))
        service.write_reload_stamp(workdir, "reloaded", "2026-09-27T03:00:00+00:00", "v")
        self.assertFalse(any("gateway restart pending" in line for line in attention()))
        self.assertEqual(runner.call_count, 2)

    def test_render_error_and_unwritable_stamp_directory_are_visible(self):
        from claude_multi import launch
        self.runtime.doctor_callback = None
        self.runtime.doctor_daemon_callback = lambda: launch.DaemonStatus("absent", "fixture")
        env = {**self.env, "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT)}
        for unwritable in (False, True):
            with self.subTest(unwritable=unwritable):
                # The directory check fails even for root, unlike chmod-only tests.
                stamp_failure = PermissionError("fixture stamp directory")
                attempt_writer = (mock.patch.object(service, "write_reload_attempt", side_effect=stamp_failure)
                                  if unwritable else nullcontext())
                result_writer = (mock.patch.object(service, "write_reload_stamp", side_effect=stamp_failure)
                                 if unwritable else nullcontext())
                with mock.patch.object(proxy, "render_runtime_config", side_effect=OSError("fixture render")), \
                     attempt_writer, result_writer, \
                     redirect_stderr(io.StringIO()):
                    self.assertEqual(proxy.main(["init", "--reload-check"], environ=env), 1)
                runner = mock.Mock(return_value=mock.Mock(returncode=0,
                    stdout="{ path=/fixture/proxy ; start_time=[Sun 2026-09-27 01:00:00 UTC] ; "
                           "stop_time=[Sun 2026-09-27 01:00:01 UTC] ; pid=123 ; code=exited ; status=1 }"))
                self.assertEqual(self.runtime.health_get("fixture", "/healthz"), 200)
                _, _, attention = cli._collect_doctor_reports(
                    self.runtime, reload_failed=lambda: service.last_reload_failed(runner=runner))
                self.assertTrue(any("gateway restart pending" in line for line in attention), attention)
                if unwritable:
                    runner.assert_called_once()
                else:
                    runner.assert_called_once()
                    workdir = service.gateway_workdir(self.runtime.session_store.root)
                    self.assertTrue((workdir / service.RELOAD_ATTEMPT).is_file())
                    self.assertEqual(service.read_reload_stamp(workdir)["status"], "error")

    def test_older_success_cannot_hide_failed_writers_in_writable_directory(self):
        import errno
        from claude_multi import launch
        self.runtime.doctor_callback = None
        self.runtime.doctor_daemon_callback = lambda: launch.DaemonStatus("absent", "fixture")
        env = {**self.env, "CLAUDE_MULTI_ASSETS": str(FIXTURE_ROOT)}
        workdir = service.ensure_gateway_workdir(self.runtime.session_store.root)
        old = "2026-09-27T01:00:00+00:00"
        for number in (errno.ENOSPC, errno.EDQUOT, errno.EIO):
            with self.subTest(errno=number):
                service.write_reload_attempt(workdir, old, "v")
                service.write_reload_stamp(workdir, "reloaded", old, "v")
                self.assertTrue(os.access(workdir, os.W_OK))
                with mock.patch.object(proxy, "render_runtime_config", side_effect=OSError(number, "fixture render")), \
                     mock.patch.object(service, "write_reload_attempt", side_effect=OSError(number, "fixture attempt")), \
                     mock.patch.object(service, "write_reload_stamp", side_effect=OSError(number, "fixture result")), \
                     redirect_stderr(io.StringIO()):
                    self.assertEqual(proxy.main(["init", "--reload-check"], environ=env), 1)
                self.assertFalse((workdir / service.RELOAD_STAMP).exists())
                self.assertFalse((workdir / service.RELOAD_SUCCESS).exists())
                self.assertEqual(self.runtime.health_get("fixture", "/healthz"), 200)
                manager = mock.Mock(return_value=True)
                _, _, attention = cli._collect_doctor_reports(self.runtime, reload_failed=manager)
                self.assertTrue(any("gateway restart pending" in line and "ExecReload failed" in line
                                    for line in attention), attention)
                manager.assert_called_once()


if __name__ == "__main__":
    unittest.main()
