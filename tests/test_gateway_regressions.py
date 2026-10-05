"""Gateway lifecycle observations, deliberately NOT repaired-product gates.

Run separately with _gateway_harness.py --run-reproducers and the explicit
CLAUDE_MULTI_TEST_GATEWAY_LIFECYCLE_PROBES store output of gateway_go/lifecycle.nix.
Full discovery also runs these when that override is supplied. Main sandbox
checks without diagnostics retain a BOUNDARY; ordinary G7/G8 exclude this lane.

Rebuild from the worktree root (after disk preflight), offline and without a
result link. lifecycle.nix asserts vendor-input identity at evaluation time::

    nix build --offline --no-link --impure --expr '
      let f = builtins.getFlake (toString ./.);
          pkgs = f.inputs.nixpkgs.legacyPackages.x86_64-linux;
          cliProxyApi = import "${f}/nix/gateway.nix" { inherit pkgs; };
      in import "${f}/tests/gateway_go/lifecycle.nix" { inherit cliProxyApi; }'
"""
import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT
from _layout import SERVICE_SPEC
from claude_multi import catalog, probe, proxy, render, state
import _gateway_harness as harness

PROBES_ENV = "CLAUDE_MULTI_TEST_GATEWAY_LIFECYCLE_PROBES"
ROWS = {"B017", "B020", "B021", "B023", "B024"}
MODULE = "tests.test_gateway_regressions"
OBSERVATIONS = harness.Evidence()
PROVENANCE = {}


def revised(document, **changes):
    document = copy.deepcopy(document)
    document["openai-compatibility"].pop()  # only the final render sentinel
    document.update(changes)
    return render.finalize_document(document)


def record(item, desired, reproduced, **fields):
    if desired and reproduced:
        raise ValueError("a reproducer cannot claim both outcomes")
    verdict = ("desired behaviour present" if desired else "known behaviour reproduced" if reproduced
               else "scenario executed")
    OBSERVATIONS.record(MODULE, "reproducers", {
        "row": item, "scenario_executed": True, "verdict": verdict,
        "desired_behaviour_present": desired, "known_behaviour_reproduced": reproduced,
        "product_gate": False, **fields,
    })


def validate_report(document):
    if document.get("schema") != "gwtest-reproducers-v1" or document.get("observation_only") is not True:
        raise AssertionError("incorrect reproducer evidence identity")
    rows = document.get("rows", [])
    if len(rows) != len(ROWS) or {row.get("row") for row in rows} != ROWS:
        raise AssertionError("missing or duplicate F3 scenario")
    provenance = document.get("provenance", {})
    for name in ("gateway", "source", "diagnostics"):
        if not str(provenance.get(name, "")).startswith("/nix/store/"):
            raise AssertionError("missing reproducer source/series identity")
    if provenance.get("patches") != list(harness.PATCH_ROWS):
        raise AssertionError("reproducer series does not match the patch inventory")
    checker = harness.Evidence()
    for row in rows:
        checker.record(MODULE, "reproducers", row)
        desired, reproduced = row.get("desired_behaviour_present"), row.get("known_behaviour_reproduced")
        if type(desired) is not bool or type(reproduced) is not bool or (desired and reproduced):
            raise AssertionError("invalid observation classification")
        expected = "desired behaviour present" if desired else "known behaviour reproduced" if reproduced else "scenario executed"
        if (row.get("scenario_executed") is not True or row.get("product_gate") is not False
                or row.get("verdict") != expected):
            raise AssertionError("a defect observation must not masquerade as a product gate")


def tearDownModule():
    directory = os.environ.get(harness.EVIDENCE_ENV)
    if not directory:
        return
    document = {"schema": "gwtest-reproducers-v1", "observation_only": True,
                "provenance": PROVENANCE, "sandbox": harness.sandbox_metadata(),
                "rows": OBSERVATIONS.tables.get(MODULE, {}).get("reproducers", [])}
    validate_report(document)  # normal, failure-propagating execution, never atexit
    directory = state.ensure_private_dir(Path(directory))
    state.atomic_write(directory / "reproducers.json", (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())


def diagnostics():
    gateway, reason = harness.find_gateway_binary()
    if gateway is None:
        harness.boundary("BOUNDARY: " + reason)
    reason = probe.network_isolation_available()
    if reason:
        harness.boundary("BOUNDARY: " + reason)
    value = os.environ.get(PROBES_ENV, "")
    root = Path(os.path.realpath(value))
    binaries = [root / "bin" / ("gwtest-lifecycle-" + kind) for kind in ("watcher", "service")]
    if (not value or not str(root).startswith("/nix/store/") or any(
            not binary.is_file() or binary.is_symlink() or not os.access(binary, os.X_OK)
            or binary.stat().st_mode & 0o022 for binary in binaries)):
        harness.boundary("BOUNDARY: set " + PROBES_ENV + " to the matching store diagnostics")
    output = str(Path(gateway).parent.parent)
    if (root / "share/gateway-outpath").read_text().strip() != output:
        raise AssertionError("lifecycle diagnostics belong to another gateway build")
    patches = json.loads((root / "share/gateway-patches.json").read_text())
    if patches != list(harness.PATCH_ROWS):
        raise AssertionError("lifecycle diagnostic patch order differs from harness")
    PROVENANCE.update(gateway=output, diagnostics=str(root),
                      source=(root / "share/gateway-source").read_text().strip(), patches=patches)
    return root


def wait_for(process, condition, label, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"lifecycle diagnostic exited before {label}: rc={process.returncode}")
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("lifecycle deadline exceeded: " + label)


def models_get(socket, port):
    def get(_url, _token):
        reply = harness.unix_request(socket, "GET", "/v1/models", timeout=0.25,
            headers={"Authorization": "Bearer " + FIXTURE_GATEWAY_TOKEN,
                     "Host": f"127.0.0.1:{port}", "User-Agent": "gwtest-reproducer/1"})
        return reply.status, {row["id"] for row in reply.json().get("data", [])}
    return get


def reconcile(socket, port, document):
    # The actual product restart detector, with ONLY transport replaced by the
    # disposable namespace's unix bridge. It polls real running-service replies.
    return proxy.await_sentinel({"gateway": {"base_url": f"http://127.0.0.1:{port}"}},
        FIXTURE_GATEWAY_TOKEN, render.document_sentinel(document), models_get=models_get(socket, port))


def websocket(gateway, authenticated):
    headers = gateway.headers()
    if not authenticated:
        headers.pop("Authorization")
    headers.update({"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                    "Sec-WebSocket-Key": "Y20wNDgtc3ludGhldGljIQ=="})
    reply = harness.unix_request(gateway.socket, "GET", "/v1/ws", headers=headers)
    if reply.status == 101:
        # A real WebSocket handshake, not just a permissive HTTP route.
        import base64
        expected = base64.b64encode(hashlib.sha1((headers["Sec-WebSocket-Key"]
            + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if reply.headers.get("sec-websocket-accept") != expected:
            raise AssertionError("invalid websocket upgrade control")
    return reply.status


class ReproducerEvidenceTests(unittest.TestCase):
    def report(self):
        with mock.patch.object(OBSERVATIONS, "tables", {}):
            for item in sorted(ROWS):
                record(item, False, True)
            return {"schema": "gwtest-reproducers-v1", "observation_only": True,
                    "provenance": {"gateway": "/nix/store/fake-gateway", "source": "/nix/store/fake-source",
                                   "diagnostics": "/nix/store/fake-diagnostics", "patches": list(harness.PATCH_ROWS)},
                    "rows": copy.deepcopy(OBSERVATIONS.tables[MODULE]["reproducers"])}

    def test_complete_observations_are_not_product_passes(self):
        validate_report(self.report())

    def test_missing_duplicate_or_product_pass_evidence_refused(self):
        for mutation in (lambda d: d["rows"].pop(), lambda d: d["rows"].append(d["rows"][0]),
                         lambda d: d["rows"][0].update(product_gate=True),
                         lambda d: d["rows"][0].update(verdict="desired behaviour present"),
                         lambda d: d["provenance"].update(patches=[])):
            document = self.report()
            mutation(document)
            with self.assertRaises(AssertionError):
                validate_report(document)

    def test_publication_is_private_complete_and_failure_propagating(self):
        document = self.report()
        with tempfile.TemporaryDirectory(prefix="gwtest-repro-evidence-") as directory:
            output = Path(directory) / "evidence"
            with mock.patch.dict(os.environ, {harness.EVIDENCE_ENV: str(output)}), \
                    mock.patch.dict(PROVENANCE, document["provenance"], clear=True), \
                    mock.patch.object(OBSERVATIONS, "tables", {MODULE: {"reproducers": document["rows"]}}):
                tearDownModule()
                self.assertEqual(output.stat().st_mode & 0o777, 0o700)
                self.assertEqual((output / "reproducers.json").stat().st_mode & 0o777, 0o600)
                validate_report(json.loads((output / "reproducers.json").read_text()))
                with mock.patch.object(state, "atomic_write", side_effect=OSError("publication failed")):
                    with self.assertRaisesRegex(OSError, "publication failed"):
                        tearDownModule()

    def test_unsettled_ws_reload_is_not_desired_behavior(self):
        gateway = mock.Mock(document={"ws-auth": True}, socket=Path("/unused"), port=55123)
        for reconciliation, status, expected in (("restart_required", 401, "scenario executed"),
                ("reloaded", 401, "desired behaviour present"), ("reloaded", 101, "known behaviour reproduced")):
            case = LifecycleReproducerTests("test_b023_ws_auth_hot_downgrade")
            with self.subTest(reconciliation=reconciliation, status=status), \
                    mock.patch.object(harness, "GatewayHarness", return_value=gateway), \
                    mock.patch(__name__ + ".revised", return_value={}), \
                    mock.patch.object(state, "atomic_write"), \
                    mock.patch(__name__ + ".websocket", side_effect=[401, 101, status]), \
                    mock.patch(__name__ + ".reconcile", return_value=proxy.ReloadResult(reconciliation, "fixture")), \
                    mock.patch.object(OBSERVATIONS, "tables", {}):
                try:
                    case.test_b023_ws_auth_hot_downgrade()
                    row = OBSERVATIONS.tables[MODULE]["reproducers"][0]
                    self.assertEqual(row["verdict"], expected)
                finally:
                    case.doCleanups()

    def test_failed_runner_does_not_validate_or_leave_stale_report(self):
        with tempfile.TemporaryDirectory(prefix="gwtest-reproducer-runner-") as temporary:
            directory = Path(temporary)
            path = directory / "reproducers.json"
            path.write_text(json.dumps(self.report()))
            result = mock.Mock(skipped=[])
            result.wasSuccessful.return_value = False
            with mock.patch.dict(os.environ, {harness.EVIDENCE_ENV: str(directory)}), \
                    mock.patch.object(unittest.defaultTestLoader, "loadTestsFromModule"), \
                    mock.patch.object(unittest, "TextTestRunner") as runner, \
                    mock.patch(__name__ + ".validate_report") as validate:
                runner.return_value.run.return_value = result
                self.assertEqual(harness.main(["--run-reproducers"]), 1)
                validate.assert_not_called()
            self.assertFalse(path.exists())

    def test_reproducer_lane_is_separate_from_ordinary_budget(self):
        self.assertIn("test_gateway_regressions", harness.GATEWAY_CHECK_EXCLUSIONS)
        self.assertNotIn(MODULE, harness.GATEWAY_CHECK_MODULES)


class LifecycleReproducerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.diagnostics = diagnostics()

    def watcher_observation(self, item, test_name):
        with tempfile.TemporaryDirectory(prefix="gwtest-watcher-repro-") as directory:
            root = Path(directory).resolve()
            log_path = root / "probe.log"
            with log_path.open("wb") as log:
                process = probe.start_isolated_process(
                    [str(self.diagnostics / "bin/gwtest-lifecycle-watcher"), "-test.v",
                     "-test.run=^" + test_name + "$", "-test.timeout=10s"], cwd=root,
                    env={"HOME": str(root), "TMPDIR": str(root), "PATH": "/usr/bin:/bin"},
                    stdout=log, stderr=subprocess.STDOUT)
                try:
                    process.wait(timeout=15)
                finally:
                    probe.stop_isolated_process(process)
            output = log_path.read_text()
            self.assertEqual(process.returncode, 0, "watcher scenario failed (not an observed product defect)")
            self.assertIn("--- PASS: " + test_name, output)
            rows = re.findall(r"^GWTEST_REPRO=(.+)$", output, re.M)
            self.assertEqual(len(rows), 1, "watcher scenario omitted or duplicated observation")
            result = json.loads(rows[0])
            self.assertEqual(result.pop("row"), item)
            return result

    def test_b017_config_replaced_before_watcher(self):
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="gwtest-startup-window-") as directory:
            root = Path(directory).resolve()
            home, control = root / "home", root / "run"
            spec = json.loads((SERVICE_SPEC).read_text())
            for path in (home, control, *(home / p for p in spec["private_dirs"]), home / ".local/share/claude-multi-release"):
                state.ensure_private_dir(path)
            egress, port = harness.ports()
            original, _, _ = harness.build_document(catalog.load_catalog(FIXTURE_ROOT), egress, port, home)
            replacement = revised(original, **{"request-retry": 1})
            config = home / ".config/claude-multi/config.yaml"
            state.atomic_write(config, render.emit_yaml(original).encode())
            socket, log_path = control / "gateway.sock", root / "probe.log"
            upstream = harness.FakeUpstream(control / "upstream.sock")
            process = None
            try:
                with log_path.open("wb") as log:
                    process = probe.start_isolated_process(
                        [str(self.diagnostics / "bin/gwtest-lifecycle-service"), "-test.v",
                         "-test.run=^TestGatewayB017StartupWindow$", "-test.timeout=25s"],
                        cwd=home / spec["working_directory"],
                        env={"HOME": str(home), "TMPDIR": str(root), "PATH": "/usr/bin:/bin",
                             "WRITABLE_PATH": str(root), "GWTEST_CONFIG": str(config), "GWTEST_CONTROL": str(control)},
                        home_view=probe.HomeView(home, tuple(home / p for p in spec["bind_rw"]), tuple(home / p for p in spec["bind_ro"])),
                        bridges=[probe.PortBridge(egress, control / "upstream.sock", "egress"),
                                 probe.PortBridge(port, socket, "ingress")], stdout=log, stderr=subprocess.STDOUT)
                    wait_for(process, lambda: (control / "paused").exists(), "pre-watcher startup hook")
                    state.atomic_write(config, render.emit_yaml(replacement).encode())
                    state.atomic_write(control / "release", b"released\n")
                    wait_for(process, lambda: harness.WATCHER_READY in log_path.read_text(), "watcher started")
                    result = reconcile(socket, port, replacement)
                    self.assertIn(result.status, {"reloaded", "restart_required"})
                    status, ids = models_get(socket, port)(None, None)
                    self.assertEqual(status, 200)
                    self.assertTrue({render.document_sentinel(original), render.document_sentinel(replacement)} & ids)
                    # A subsequent replacement proves this was the startup
                    # interval, not a broken watcher/invalid synthetic render.
                    followup = revised(replacement, **{"request-retry": 2})
                    state.atomic_write(config, render.emit_yaml(followup).encode())
                    self.assertEqual(reconcile(socket, port, followup).status, "reloaded")
                    self.assertEqual(len(upstream.hits), 0)
                    state.atomic_write(control / "done", b"done\n")
                    process.wait(timeout=10)
                self.assertEqual(process.returncode, 0, "startup window service did not stop cleanly")
                self.assertIn("--- PASS: TestGatewayB017StartupWindow", log_path.read_text())
                # B017's accepted alternative is explicit restart-required; the
                # missed-window residual remains visible and is NOT called fixed.
                record("B017", True, False, reconciliation=result.status,
                       reconciliation_reason=result.message,
                       window="post-listener, pre-watcher (OnAfterStart)",
                       pre_listener_window="not exercised; launcher reports down/config will apply on start",
                       served_sentinels=sorted(ids & {render.document_sentinel(original), render.document_sentinel(replacement)}),
                       original_sentinel=render.document_sentinel(original),
                       replacement_sentinel=render.document_sentinel(replacement),
                       startup_replacement_missed=render.document_sentinel(replacement) not in ids,
                       followup_reloaded=True, provider_hits=0, seconds=time.monotonic() - started)
            finally:
                if process is not None:
                    probe.stop_isolated_process(process)
                upstream.close()

    def test_b020_mutable_config_guard(self):
        started = time.monotonic()
        observed = self.watcher_observation("B020", "TestGatewayB020MutableGuard")
        self.assertTrue(observed["intact_guard_refused"])
        self.assertTrue(observed["in_place_mutation"])
        gateway = harness.GatewayHarness(management=True)
        self.addCleanup(gateway.close)
        management_headers = {"Host": f"127.0.0.1:{gateway.port}", "X-Management-Key": "dummy-gwtest-mgmt"}
        positive = harness.unix_request(gateway.socket, "GET", "/v0/management/auth-files",
            headers=management_headers)
        self.assertEqual(positive.status, 200, "management allowlist positive control")
        # The current management policy closes these concrete mutation routes. Do not
        # extrapolate this bounded check to all possible in-place mutations.
        statuses = []
        for method, path, body in (("PUT", "/v0/management/api-keys", b'{"keys":[]}'),
                                   ("PATCH", "/v0/management/api-keys", b'{"old":"absent","new":""}'),
                                   ("PUT", "/v0/management/config.yaml", b"api-keys: []\n")):
            reply = harness.unix_request(gateway.socket, method, path,
                headers=management_headers, body=body)
            self.assertEqual((reply.status, len(reply.raw)), (404, 0))
            statuses.append(reply.status)
        self.assertEqual(gateway.get("/v1/models").status, 200)
        self.assertEqual(harness.unix_request(gateway.socket, "GET", "/v1/models").status, 401)
        record("B020", not observed["key_drop_accepted"], observed["key_drop_accepted"], **observed,
               mutation_scope="synthetic in-place pointer; three HTTP mutation controls refused",
               management_positive=positive.status, management_statuses=statuses, seconds=time.monotonic() - started)

    def test_b021_post_start_symlink_replacement(self):
        started = time.monotonic()
        gateway = harness.GatewayHarness()
        self.addCleanup(gateway.close)
        replacement = revised(gateway.document, **{"request-retry": 1})
        target = gateway.config.parent / "gwtest-target.yaml"
        state.atomic_write(target, render.emit_yaml(replacement).encode())
        link = gateway.config.parent / "gwtest-link"
        link.symlink_to(target)
        os.replace(link, gateway.config)
        self.assertTrue(gateway.config.is_symlink())
        result = reconcile(gateway.socket, gateway.port, replacement)
        self.assertIn(result.status, {"reloaded", "restart_required"})
        # A timeout alone is not proof of a fail-closed regular-file policy.
        record("B021", False, result.status == "reloaded", reconciliation=result.status,
               symlink_replaced=True, observation_bound_seconds=2, seconds=time.monotonic() - started)

    def test_b023_ws_auth_hot_downgrade(self):
        started = time.monotonic()
        gateway = harness.GatewayHarness()
        self.addCleanup(gateway.close)
        self.assertIs(gateway.document["ws-auth"], True)
        self.assertEqual(websocket(gateway, False), 401)
        self.assertEqual(websocket(gateway, True), 101)
        replacement = revised(gateway.document, **{"ws-auth": False})
        state.atomic_write(gateway.config, render.emit_yaml(replacement).encode())
        result = reconcile(gateway.socket, gateway.port, replacement)
        self.assertIn(result.status, {"reloaded", "restart_required"})
        status = websocket(gateway, False)
        self.assertIn(status, {401, 101})
        # An unchanged auth result before a proven reload is only a bounded
        # scenario, not evidence that the gateway refused the downgrade.
        record("B023", status == 401 and result.status == "reloaded", status == 101, unauthenticated_before=401,
               authenticated_control=101, unauthenticated_after=status, reconciliation=result.status,
               seconds=time.monotonic() - started)

    def test_b024_reload_timer_after_stop(self):
        started = time.monotonic()
        observed = self.watcher_observation("B024", "TestGatewayB024TimerAfterStop")
        self.assertTrue(observed["timer_entered"])
        self.assertTrue(observed["stop_returned_before_release"])
        # No callback in a bounded window is not a proof that no race exists.
        record("B024", False, observed["callback_after_stop"], **observed,
               negative_meaning="not reproduced within the bound", seconds=time.monotonic() - started)


if __name__ == "__main__":
    unittest.main()
