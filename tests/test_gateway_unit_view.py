"""The gateway unit's bind-set proof; real gateway only in bwrap --unshare-net.

This models ProtectHome=tmpfs, not systemd/seccomp.
Python init runs outside the view; run --prepared runs inside it, exactly as
ExecStartPre=+ / ExecStart do. All HOME, assets, auth and config are synthetic.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from claude_multi import management, probe, proxy, service, state
from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT
from _layout import SERVICE_SPEC
from _gateway import _gateway_binary
from _tier import fast_tier_boundary


WRITER = r'''
import errno, json, os
from pathlib import Path
h = Path(os.environ['HOME'])
a = h / '.local/share/claude-multi/auth/probe-write'
results = {}
try:
    a.write_text('first'); a.write_text('second'); a.rename(a.with_suffix('.renamed'))
    results['auth'] = True
except OSError:
    results['auth'] = False
for name, path in [('home', h/'forbidden'), ('data', h/'.local/share/claude-multi/forbidden'),
                   ('policy', h/'.config/claude-multi/native-contract.json')]:
    try:
        path.write_text('forbidden')
        results[name] = False
    except OSError as exc:
        results[name] = exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM)
results['hidden'] = not (h/'.ssh/probe-canary').exists()
print(json.dumps(results))
'''


class GatewayHomeViewTests(unittest.TestCase):
    def setUp(self):
        reason = fast_tier_boundary() or probe.network_isolation_available()
        if reason:
            self.skipTest(reason)
        self.binary, reason = _gateway_binary()
        if self.binary is None:
            self.skipTest('BOUNDARY: ' + reason)
        self.tmp = tempfile.TemporaryDirectory(prefix='probe-home-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.spec = json.loads(SERVICE_SPEC.read_text())
        for rel in self.spec['private_dirs']:
            state.ensure_private_dir(self.home/rel)
        # The stable exec link's installation (a bundle-style root under the
        # read-only data bind, as the rendered unit reaches it).
        release = self.home/self.spec['exec_links']['bundle']
        release.mkdir(mode=0o700, parents=True)
        # Exercise the installed-layout entry point: source-tree bin scripts
        # intentionally discard inherited asset overrides, whereas the
        # copied package script must keep this synthetic release's assets.
        self.entry = release/'bin'/'claude-multi-proxy'
        self.entry.parent.mkdir()
        shutil.copy(REPO_ROOT/'bin/claude-multi-proxy', self.entry)
        self.assets = release/'assets'
        shutil.copytree(FIXTURE_ROOT, self.assets)
        self.gateway = self.assets/'catalog/gateway.json'
        doc = json.loads(self.gateway.read_text())
        doc['gateway']['base_url'] = 'http://127.0.0.1:18403'
        self.gateway.write_text(json.dumps(doc))
        self.env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin',
                    'PYTHONPATH': str(REPO_ROOT/'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'CLAUDE_MULTI_ASSETS': str(self.assets), 'CLAUDE_MULTI_PROXY_BIN': self.binary,
                    management.PATCHES_ENV: management.ALLOWLIST_PATCH,
                    management.CHANNEL_ENV: management.MANAGEMENT_CHANNEL}
        self.state_root = self.home/self.spec['state_root']
        self.workdir = self.home/self.spec['working_directory']
        (self.home/'.ssh').mkdir()
        (self.home/'.ssh/probe-canary').write_text('fixture')
        from claude_multi.platform import systemd_unit
        rw, ro = systemd_unit.bind_sets(self.spec, self.spec['exec_links']['bundle'])
        self.view = probe.HomeView(self.home, tuple(self.home/p for p in rw), tuple(self.home/p for p in ro))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            proxy.cmd_init(["--prepare-start"], environ=self.env,
                           models_get=lambda *_: (_ for _ in ()).throw(ConnectionRefusedError()),
                           listener_observer=lambda _: service.OwnerVerdict("none", "isolated fixture"),
                           pid_get=lambda **_: (None, False))

    def writer(self, view):
        with tempfile.TemporaryFile() as out:
            child = probe.start_isolated_process([str(Path(sys.executable).resolve()), '-c', WRITER],
                                                  cwd=self.root, env=self.env, home_view=view,
                                                  stdout=out, stderr=subprocess.STDOUT)
            try:
                self.assertEqual(child.wait(timeout=20), 0)
            finally:
                probe.stop_isolated_process(child)
            out.seek(0)
            return json.loads(out.read())

    def test_fresh_home_policy_and_auth_rename_with_negative_control(self):
        self.assertFalse((self.home/'.config/claude-multi/native-contract.json').exists())
        self.assertTrue(all(self.writer(self.view).values()))
        negative = probe.HomeView(self.home, tuple(p for p in self.view.bind_rw if p.name != 'auth'), self.view.bind_ro)
        self.assertFalse(self.writer(negative)['auth'])

    def test_prepared_gateway_and_changed_sentinel_reload(self):
        sock = self.root/'gateway.sock'
        token = proxy.gateway_api_keys(self.home)[-1]
        management_key = management.read_active(self.home)
        self.assertIsNotNone(management_key)
        config_before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                         for p in proxy.config_dir(self.home).iterdir()}
        def get(_base=None, _token=token, path='/v1/models'):
            connection = probe.UnixHTTPConnection(sock, timeout=1)
            try:
                connection.request('GET', path, headers={'Authorization': 'Bearer ' + _token})
                response = connection.getresponse()
                body = response.read()
                return response.status, ({row['id'] for row in json.loads(body)['data']} if response.status == 200 and path == '/v1/models' else set())
            finally:
                connection.close()
        with tempfile.TemporaryFile() as log:
            child = probe.start_isolated_process(
                [str(Path(sys.executable).resolve()), str(self.entry), 'run', '--prepared'],
                cwd=self.root, env=self.env, home_view=self.view,
                bridges=[probe.PortBridge(18403, sock, 'ingress')], stdout=log, stderr=subprocess.STDOUT)
            try:
                before = None
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        status, before = get()
                        if status == 200 and any(v.startswith('claude-multi-render-') for v in before):
                            break
                    except OSError:
                        pass
                    time.sleep(.05)
                self.assertIsNotNone(before)
                self.assertEqual(get(path='/healthz')[0], 200)
                self.assertEqual(child.poll(), None)
                old = next(v for v in before if v.startswith('claude-multi-render-'))
                # A harmless, real render input: a new loopback alias. Not merely
                # re-rendering identical bytes (which proves no watcher activity).
                doc = json.loads(self.gateway.read_text())
                self.assertEqual(get(_token=management_key, path='/v0/management/auth-files')[0], 200)
                self.assertEqual(config_before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                                                for p in proxy.config_dir(self.home).iterdir()})
                doc['gateway']['cliproxy_static']['routing']['session-affinity-ttl'] = '23h'
                self.gateway.write_text(json.dumps(doc))
                # Render to an unwatched sibling HOME first: the negative control.
                other = self.root/'unwatched'
                other.mkdir(mode=0o700)
                env = {**self.env, 'HOME': str(other)}
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    proxy.ensure_directories(other)
                    state.atomic_write(proxy.config_dir(other)/'api-key', (token+'\n').encode())
                    # Use the same HOME-derived auth path in the final render;
                    # intercept the write target, leaving watched bytes untouched.
                    original = state.atomic_write
                    def redirect(path, data, *a, **kw):
                        return original((other/'config.yaml') if Path(path) == proxy.config_dir(self.home)/'config.yaml' else path, data, *a, **kw)
                    from unittest import mock
                    with mock.patch.object(state, 'atomic_write', side_effect=redirect):
                        target, result, _ = proxy.render_runtime_config(self.home, environ=self.env, state_root=self.state_root)
                    self.assertNotEqual(result.sentinel, old)
                    self.assertNotIn(result.sentinel, before)
                    outcome = proxy.await_sentinel(doc, token, result.sentinel, models_get=get, timeout=.3)
                    self.assertEqual(outcome.status, 'restart_required')
                    self.assertEqual(proxy.cmd_init(['--reload-check'], environ=self.env, models_get=get), 0)
                self.assertIn(result.sentinel, get()[1])
                self.assertIsNone(child.poll())
            finally:
                probe.stop_isolated_process(child)
