"""The supervised gateway service: unit rendering, reference parity, the hand-offs.

The renderer is checked against the reference unit (every hardening and
lifecycle directive identical). Install, refresh and uninstall run against a
stub user service manager and the lifecycle's fake gateway world in a
temporary HOME: nothing here reaches the developer's service manager, units,
garbage-collector roots or gateway.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import _log_secrets
from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT, RESOURCES_ROOT
from _layout import SERVICE_SPEC
from claude_multi import (cli, endpoint, gateway_inhibition, gateway_lifecycle as gl, gateway_service as gs, installs,
                          secret_store, service, state)
from claude_multi.platform import linux_service, observation, systemd_unit
from test_gateway_lifecycle import World, _Lifecycle, save_line

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "service"
STORE_NOTIFIER = "/nix/store/00000000000000000000000000000000-libnotify/bin/notify-send"
# The reference unit's settings: its name, its exec link and its notifier.
REFERENCE_PLAN = dict(name="cli-proxy-api", exec_root=".local/share/claude-multi-release/current",
                      notify_send=STORE_NOTIFIER)
# Triggers of the packaging the reference came from; the product classifies
# reload versus restart itself (service install), so they have no successor.
REFERENCE_ONLY = {"X-Reload-Triggers", "X-Restart-Triggers"}


def spec() -> dict:
    return json.loads(SERVICE_SPEC.read_text())


def directives(text: str, *, drop: frozenset[str] | set[str] = frozenset()) -> dict[str, list[tuple[str, str]]]:
    return {section: sorted(item for item in items if item[0] not in drop)
            for section, items in systemd_unit.parse(text).items()}


class UnitDirectoryTests(unittest.TestCase):
    def test_new_unit_directories_are_private_and_existing_modes_stay_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / ".config"
            shared.mkdir(mode=0o755)
            shared.chmod(0o755)
            unit = shared / "systemd/user/gateway.service"
            previous = os.umask(0o002)
            try:
                gs.write_unit_file(unit, "[Service]\nType=simple\n")
            finally:
                os.umask(previous)
            self.assertEqual(shared.stat().st_mode & 0o777, 0o755)
            for path in (shared / "systemd", unit.parent):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700, str(path))
            # Do not change a user's already existing shared unit directory.
            unit.parent.chmod(0o755)
            gs.write_unit_file(unit, "[Service]\nType=oneshot\n")
            self.assertEqual(unit.parent.stat().st_mode & 0o777, 0o755)


class RenderTests(unittest.TestCase):
    def reference_plan(self) -> systemd_unit.UnitPlan:
        return systemd_unit.UnitPlan(**REFERENCE_PLAN, notice=service.failure_notice("cli-proxy-api"))

    def test_every_hardening_and_lifecycle_directive_matches_the_reference(self) -> None:
        reference = (FIXTURES / "reference.service").read_text()
        rendered = systemd_unit.render(spec(), self.reference_plan())
        self.assertEqual(set(rendered), {"cli-proxy-api.service", "cli-proxy-api-failure.service"})
        self.assertEqual({key for key, _ in systemd_unit.parse(reference)["Unit"]} & REFERENCE_ONLY, REFERENCE_ONLY)
        self.assertEqual(directives(rendered["cli-proxy-api.service"]),
                         directives(reference, drop=REFERENCE_ONLY))
        # Not merely the same set: the repeated directives keep their counts.
        ours = systemd_unit.parse(rendered["cli-proxy-api.service"])["Service"]
        for key in ("BindPaths", "BindReadOnlyPaths", "RestrictAddressFamilies", "UnsetEnvironment"):
            self.assertEqual(sum(1 for k, _ in ours if k == key),
                             sum(1 for k, _ in systemd_unit.parse(reference)["Service"] if k == key), key)

    def test_the_failure_notice_unit_matches_except_its_text(self) -> None:
        reference = systemd_unit.parse((FIXTURES / "reference-failure.service").read_text())
        rendered = systemd_unit.parse(
            systemd_unit.render(spec(), self.reference_plan())["cli-proxy-api-failure.service"])
        self.assertEqual(rendered["Unit"], reference["Unit"])
        self.assertEqual(dict(rendered["Service"])["Type"], dict(reference["Service"])["Type"])
        prefix = f"-{STORE_NOTIFIER} --urgency=critical --app-name=claude-multi 'claude-multi gateway failed' "
        ours, theirs = dict(rendered["Service"])["ExecStart"], dict(reference["Service"])["ExecStart"]
        self.assertTrue(ours.startswith(prefix) and theirs.startswith(prefix))
        # The remedy for a missing directory changed: the install creates them now.
        self.assertIn("claude-multi gateway service install recreates them", ours)
        self.assertNotIn("tmpfiles", ours)

    def test_a_new_install_renders_through_its_channel_link(self) -> None:
        s = spec()
        for channel, link in s["exec_links"].items():
            with self.subTest(channel=channel):
                files = systemd_unit.render(s, systemd_unit.UnitPlan("claude-multi-gateway", link))
                self.assertEqual(list(files), ["claude-multi-gateway.service"])
                text = files["claude-multi-gateway.service"]
                self.assertTrue(systemd_unit.written_here(text))
                service_section = dict(systemd_unit.parse(text)["Service"])
                self.assertEqual(service_section["ExecStart"],
                                 f"%h/{link}/bin/claude-multi-proxy run --prepared --state-root "
                                 "%h/.local/state/claude-multi")
                self.assertEqual(service_section["ExecStartPre"][:1], "+")
                self.assertEqual(service_section["ExecReload"][:1], "+")
                self.assertNotIn("/nix/store", text)
                self.assertNotIn("OnFailure", text)  # no notifier: no notice unit
                binds = [v for k, v in systemd_unit.parse(text)["Service"] if k == "BindReadOnlyPaths"]
                self.assertEqual(binds, [f"%h/{path}" for path in sorted(s["bind_ro"])])

    def test_other_home_levels_and_escaping(self) -> None:
        s = spec()
        s["hardening"] = {**s["hardening"], "home": "read-only"}
        text = systemd_unit.render(s, systemd_unit.UnitPlan("gw", s["exec_links"]["nix"]))["gw.service"]
        section = systemd_unit.parse(text)["Service"]
        self.assertIn(("ProtectHome", "read-only"), section)
        self.assertEqual(sorted(v for k, v in section if k == "ReadWritePaths"),
                         [f"%h/{p}" for p in sorted(s["bind_rw"])])
        self.assertFalse(any(k.startswith("Bind") for k, _ in section))
        s["hardening"] = {**s["hardening"], "home": "none"}
        text = systemd_unit.render(s, systemd_unit.UnitPlan("gw", s["exec_links"]["nix"]))["gw.service"]
        self.assertNotIn("ProtectHome", text)
        notice = systemd_unit.render(spec(), systemd_unit.UnitPlan(
            "gw", s["exec_links"]["nix"], "/usr/bin/notify-send", ("50% $HOME", "body")))["gw-failure.service"]
        self.assertIn("'50%% $$HOME'", notice)
        with self.assertRaises(systemd_unit.UnitSpecError):
            systemd_unit.render(spec(), systemd_unit.UnitPlan("gw", s["exec_links"]["nix"], "/usr/bin/notify-send",
                                                              ("it's", "body")))

    def test_the_certificate_trust_is_carried_and_bound(self) -> None:
        s = spec()
        trust = (systemd_unit.TrustPath("SSL_CERT_FILE", "%h/certs/corp.pem", True),
                 systemd_unit.TrustPath("SSL_CERT_DIR", "/etc/pki/corp", False))
        plan = systemd_unit.UnitPlan("gw", s["exec_links"]["bundle"], trust=trust)
        text = systemd_unit.render(s, plan)["gw.service"]
        section = systemd_unit.parse(text)["Service"]
        self.assertIn(("Environment", "SSL_CERT_FILE=%h/certs/corp.pem"), section)
        self.assertIn(("Environment", "SSL_CERT_DIR=/etc/pki/corp"), section)
        binds = [value for key, value in section if key == "BindReadOnlyPaths"]
        self.assertIn("%h/certs/corp.pem", binds)
        self.assertNotIn("/etc/pki/corp", binds)
        self.assertEqual(systemd_unit.trust_of(text), trust)
        # The text without trust is the reference's: nothing is added.
        self.assertEqual(systemd_unit.trust_of(systemd_unit.render(s, systemd_unit.UnitPlan(
            "gw", s["exec_links"]["bundle"]))["gw.service"]), ())
        for bad in (systemd_unit.TrustPath("SSL_CERT_FILE", "/etc/my certs/ca.pem", False),
                    systemd_unit.TrustPath("SSL_CERT_FILE", "%h/../ca.pem", True),
                    # A colon makes a bind a source and a destination; a
                    # control character ends the line: in HOME too.
                    systemd_unit.TrustPath("SSL_CERT_FILE", "%h/certs:bad/ca.pem", True),
                    systemd_unit.TrustPath("SSL_CERT_DIR", "%h/certs\x01x", True),
                    systemd_unit.TrustPath("SSL_CERT_DIR", "%h/certs\x7fx", True),
                    systemd_unit.TrustPath("SSL_CERT_DIR", "/etc/certs\x7fx", False),
                    systemd_unit.TrustPath("SSL_CERT_FILE", "relative/ca.pem", False),
                    systemd_unit.TrustPath("SSL_CERT_FILE", "/etc/ca$1.pem", False),
                    systemd_unit.TrustPath("NODE_OPTIONS", "/etc/ca.pem", False)):
            with self.subTest(bad=bad), self.assertRaises(systemd_unit.UnitSpecError):
                systemd_unit.render(s, systemd_unit.UnitPlan("gw", s["exec_links"]["bundle"], trust=(bad,)))

    def test_the_spec_is_closed_and_checked(self) -> None:
        cases = {
            "unknown key": lambda s: s.update(extra=1),
            "absolute path": lambda s: s["bind_rw"].append("/etc"),
            "bind outside the private dirs": lambda s: s["bind_ro"].append(".ssh"),
            "child before parent": lambda s: s["private_dirs"].insert(0, ".local/share/claude-multi/auth/x"),
            "unknown tier b": lambda s: s["hardening"]["tier_b"].append("PrivateNetwork"),
            "workdir outside the state root": lambda s: s.update(working_directory=".cache/gw"),
            "exec links": lambda s: s["exec_links"].pop("nix"),
            "unit name": lambda s: s.update(unit="Gateway.service"),
        }
        for label, change in cases.items():
            document = spec()
            change(document)
            with self.subTest(label), self.assertRaises(systemd_unit.UnitSpecError):
                systemd_unit.validate_spec(document)
        with self.assertRaises(systemd_unit.UnitSpecError):
            systemd_unit.render(spec(), systemd_unit.UnitPlan("Bad Name", ".local/share/claude-multi/nix/current"))

    def test_the_packaged_spec_is_what_the_service_reads(self) -> None:
        self.assertEqual(gs.load_spec(), spec())
        self.assertEqual(gs.load_spec(RESOURCES_ROOT), spec())
        with self.assertRaises(gs.ServiceSetupError):
            gs.load_spec(FIXTURE_ROOT / "catalog")


STALE_AFTER = datetime.timedelta(seconds=gateway_inhibition.DEFAULT_STALE_AFTER)


class Manager:
    """A stub user service manager (and nix-store) over the fake gateway world."""

    def __init__(self, case: "_ServiceCase") -> None:
        self.case = case
        self.calls: list[list[str]] = []
        self.available = True
        self.reload_ok = True
        self.reload_outcomes: list[bool] = []  # the next daemon-reloads' results, before reload_ok applies
        self.enable_ok = True
        self.disable_ok = True
        self.unit_starts = True
        self.after_enable = None  # what the started unit's gateway looks like (default: ready)
        self.fragment: str | None = None
        self.loaded: set[str] = set()
        self.enabled: set[str] = set()
        self.active: set[str] = set()
        self.reloaded: list[str] = []
        self.gc_roots: list[tuple[str, str]] = []
        self.journal_output = b""  # what journalctl prints (every record, whatever -n asks)

    def __call__(self, argv, **_kwargs):
        argv = [str(arg) for arg in argv]
        self.calls.append(argv)
        self.case.events.append(" ".join(argv[:4]))
        if argv[0] == "journalctl":
            return subprocess.CompletedProcess(argv, 0, self.journal_output, b"")
        if os.path.basename(argv[0]) == "nix-store":
            self.gc_roots.append((argv[argv.index("--add-root") + 1], argv[argv.index("--realise") + 1]))
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.case.assertEqual(argv[:2], ["systemctl", "--user"])
        words = [word for word in argv[2:] if word != "--no-ask-password"]
        verb, unit = words[0], words[-1].removesuffix(".service")
        if verb == "show" and "--property=Version" in words:
            return subprocess.CompletedProcess(argv, 0 if self.available else 1, "256\n" if self.available else "", "")
        if verb == "show":
            fragment = self.fragment or str(self.case.unit_dir / f"{unit}.service")
            values = {"LoadState": "loaded" if unit in self.loaded else "not-found",
                      "FragmentPath": fragment if unit in self.loaded else "",
                      "UnitFileState": "enabled" if unit in self.enabled else "disabled",
                      "ActiveState": "active" if unit in self.active else "inactive"}
            if "--value" in words:  # one property's bare value
                (name,) = [word.split("=", 1)[1] for word in words if word.startswith("--property=")]
                return subprocess.CompletedProcess(argv, 0, values.get(name, "") + "\n", "")
            return subprocess.CompletedProcess(argv, 0, "".join(f"{k}={v}\n" for k, v in values.items()), "")
        if verb == "daemon-reload":
            ok = self.reload_outcomes.pop(0) if self.reload_outcomes else self.reload_ok
            if ok:  # a reload that failed leaves the manager's loaded units as they were
                directory = self.case.unit_dir
                self.loaded = {path.stem for path in directory.glob("*.service")} if directory.is_dir() else set()
            return subprocess.CompletedProcess(argv, 0 if ok else 1, "", "")
        if verb == "enable":
            if not self.enable_ok:
                return subprocess.CompletedProcess(argv, 1, "", "Failed")
            self.enabled.add(unit)
            self.active.add(unit)
            if self.after_enable is not None:
                self.after_enable()
            elif self.unit_starts:
                self.case.world.become_ready("bbbbbbbbbbbbbbbb")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "disable":
            self.enabled.discard(unit)
            self.active.discard(unit)
            world = self.case.world
            world.lock, world.alive, world.listener = False, False, "none"
            return subprocess.CompletedProcess(argv, 0 if self.disable_ok else 1, "", "")
        if verb == "reload":
            self.reloaded.append(unit)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "is-active":  # the lifecycle's own state read
            return subprocess.CompletedProcess(argv, 0, "active\n" if unit in self.active else "inactive\n", "")
        raise AssertionError(f"unexpected service manager call {argv}")

    def verbs(self) -> list[str]:
        return [" ".join(w for w in call[2:] if w != "--no-ask-password").split(" ")[0]
                for call in self.calls if call[0] == "systemctl"]


class _ServiceCase(_Lifecycle):
    CHANNEL = "bundle"

    def setUp(self) -> None:
        super().setUp()
        # The service runs with the HOME-relative state root.
        self.state_root = self.home / ".local" / "state" / "claude-multi"
        self.workdir = service.ensure_gateway_workdir(self.state_root)
        self.logs = self.workdir / service.GATEWAY_LOGS
        self.unit_dir = self.home / ".config" / "systemd" / "user"
        self.events: list[str] = []
        self.manager = Manager(self)
        self.notifier: str | None = None
        self.store = self.root / "store"
        self.store.mkdir()
        patcher = mock.patch.object(installs, "STORE_PREFIX", str(self.store) + "/")
        patcher.start()
        self.addCleanup(patcher.stop)
        terminate = self.world.terminate

        def recorded_terminate(pid: int) -> bool:
            self.events.append(f"terminate {pid}")
            return terminate(pid)

        self.world.terminate = recorded_terminate
        self.install_bundle("1.0.0")

    # ----------------------------------------------------------- fixtures
    def install_bundle(self, version: str) -> Path:
        root = state.ensure_private_dir(self.home / ".local/share/claude-multi") / "install"
        target = root / "versions" / version
        (target / "bin").mkdir(parents=True, exist_ok=True)
        for entry in ("claude-multi", "claude-multi-proxy"):
            (target / "bin" / entry).write_text("#!/bin/sh\nexit 1\n")
            (target / "bin" / entry).chmod(0o755)
        link = root / "current"
        if os.path.lexists(link):
            link.unlink()
        link.symlink_to(f"versions/{version}")
        return target

    def store_package(self, name: str, gateway: str) -> Path:
        package = self.store / name
        (package / "bin").mkdir(parents=True, exist_ok=True)
        for entry in ("claude-multi", "claude-multi-proxy"):
            (package / "bin" / entry).write_text("#!/bin/sh\n")
        binary = self.store / gateway / "bin" / "cli-proxy-api"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("gateway")
        (package / "libexec/claude-multi").mkdir(parents=True, exist_ok=True)
        link = package / "libexec/claude-multi/cli-proxy-api"
        if not os.path.lexists(link):
            link.symlink_to(binary)
        return package

    def environ(self, **extra: str) -> dict[str, str]:
        return {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "CLAUDE_MULTI_CHANNEL": self.CHANNEL, **extra}

    def seams(self, **overrides) -> gl.Seams:
        values = dict(runner=self.manager, journal=self.journal())
        values.update(overrides)
        return self.world.seams(**values)

    def gateway(self, *, environ=None, seams=None, platform="linux", **kwargs) -> gl.Gateway:
        return gl.Gateway(home=self.home, state_root=self.state_root, environ=environ or self.environ(),
                          gateway_document=self.document, installation=REPO_ROOT,
                          providers=("claude", "codex"), seams=seams or self.seams(),
                          platform=platform, **kwargs)

    def service(self, *, environ=None, platform="linux", seams=None) -> gs.GatewayService:
        which = lambda name, path=None: self.notifier if name == gs.NOTIFIER else None  # noqa: E731
        return gs.GatewayService(self.gateway(environ=environ, platform=platform, seams=seams),
                                 seams=gs.ServiceSeams(runner=self.manager, which=which))

    def recorded(self) -> endpoint.EndpointConfig | None:
        return endpoint.read_config(self.home)

    def unit_text(self, name: str = "claude-multi-gateway") -> str:
        return (self.unit_dir / f"{name}.service").read_text()


class InstallTests(_ServiceCase):
    def test_install_hands_a_running_gateway_to_the_unit(self) -> None:
        self.running()
        self.notifier = "/usr/bin/notify-send"
        outcome = self.service().install()
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertEqual(outcome.status, "ready")
        self.assertIn("gateway service claude-multi-gateway installed and running", outcome.message)
        # The on-demand instance stopped (and its exit was proven) before any unit file existed.
        self.assertEqual(self.events.index("terminate 4242") < self.events.index("systemctl --user --no-ask-password daemon-reload"), True)
        self.assertEqual(self.manager.verbs(), ["show", "daemon-reload", "show", "enable"])
        self.assertTrue(systemd_unit.written_here(self.unit_text()))
        self.assertIn("ExecStart=%h/.local/share/claude-multi/install/current/bin/claude-multi-proxy run --prepared",
                      self.unit_text())
        self.assertIn("OnFailure=claude-multi-gateway-failure.service", self.unit_text())
        self.assertTrue((self.unit_dir / "claude-multi-gateway-failure.service").is_file())
        config = self.recorded()
        self.assertEqual((config.port, config.backend, config.unit), (self.port, endpoint.SYSTEMD, "claude-multi-gateway"))
        for rel in spec()["private_dirs"]:
            mode = (self.home / rel).stat().st_mode & 0o777
            self.assertEqual(mode, 0o700, rel)
        self.assertIsNone(gateway_inhibition.read(self.state_root))
        self.assertEqual(self.gateway().backend, endpoint.SYSTEMD)

    def test_install_on_a_stopped_gateway_and_without_a_notifier(self) -> None:
        outcome = self.service().install(name="my-gateway")
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertEqual(self.world.terminated, [])
        self.assertNotIn("OnFailure", self.unit_text("my-gateway"))
        self.assertIn("no failure notice", "\n".join(outcome.lines()))
        self.assertEqual(self.recorded().unit, "my-gateway")

    def test_refusals_change_nothing(self) -> None:
        def nothing_changed() -> None:
            self.assertFalse(self.unit_dir.exists())
            self.assertEqual(self.recorded().backend, endpoint.ON_DEMAND)
            self.assertIsNone(gateway_inhibition.read(self.state_root))
            self.assertEqual(self.world.terminated, [])

        cases = {
            "foreign listener": (lambda: setattr(self.world, "listener", "foreign"), "refused"),
            "unknown listener": (lambda: setattr(self.world, "listener", "unknown"), "refused"),
            "no service manager": (lambda: setattr(self.manager, "available", False), "refused"),
        }
        for label, (change, status) in cases.items():
            self.world.listener, self.manager.available = "none", True
            change()
            with self.subTest(label):
                self.assertEqual(self.service().install().status, status)
                nothing_changed()
        self.world.listener = "none"
        with self.subTest("macOS"):
            self.assertEqual(self.service(platform="darwin").install().status, "refused")
            nothing_changed()
        for label, env in {"source checkout": self.environ(CLAUDE_MULTI_CHANNEL="source"),
                           "unknown channel": {"HOME": str(self.home)},
                           "another state root": self.environ(XDG_STATE_HOME=str(self.root / "elsewhere"))}.items():
            with self.subTest(label):
                manager = self.service(environ=env)
                if label == "another state root":
                    manager.gateway.state_root = self.root / "elsewhere" / "claude-multi"
                outcome = manager.install()
                self.assertEqual(outcome.status, "refused", outcome.lines())
                nothing_changed()

    def ca_environ(self, **variables: str) -> dict[str, str]:
        return self.environ(**variables)

    def test_a_custom_ca_reaches_the_service_unit_bound_read_only(self) -> None:
        bundle = self.home / "certs" / "corp.pem"
        bundle.parent.mkdir(mode=0o700)
        bundle.write_text("-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n")
        folder = self.root / "ca-folder"
        folder.mkdir()
        (folder / "fixture.pem").write_text("fixture\n")
        with mock.patch.object(gs, "HIDDEN_ROOTS", (), create=True):
            outcome = self.service(environ=self.ca_environ(SSL_CERT_FILE=str(bundle),
                                                           SSL_CERT_DIR=str(folder))).install()
        self.assertTrue(outcome.ok, outcome.lines())
        section = systemd_unit.parse(self.unit_text())["Service"]
        self.assertIn(("Environment", "SSL_CERT_FILE=%h/certs/corp.pem"), section)
        self.assertIn(("Environment", f"SSL_CERT_DIR={folder}"), section)
        binds = [value for key, value in section if key == "BindReadOnlyPaths"]
        self.assertIn("%h/certs/corp.pem", binds)  # under the unit's empty home: bound
        self.assertNotIn(str(folder), binds)  # visible read-only anyway: not bound
        shown = "\n".join(outcome.lines())
        self.assertIn("certificates: SSL_CERT_FILE=~/certs/corp.pem (bound read-only)", shown)
        # A folder the unit's view hides (a private /tmp, another home) is bound too.
        with mock.patch.object(gs, "HIDDEN_ROOTS", (str(self.root),), create=True):
            outcome = self.service(environ=self.ca_environ(SSL_CERT_DIR=str(folder))).install()
        self.assertTrue(outcome.ok, outcome.lines())
        section = systemd_unit.parse(self.unit_text())["Service"]
        self.assertIn(("BindReadOnlyPaths", str(folder)), section)
        self.assertNotIn(("Environment", "SSL_CERT_FILE=%h/certs/corp.pem"), section)
        self.assertIn("restart pending: the unit changed", "\n".join(outcome.lines()))
        # Status judges the unit by what it carries, from any shell, under the
        # same hardening policy.
        with mock.patch.object(gs, "HIDDEN_ROOTS", (str(self.root),), create=True):
            self.assertIs(self.service().status().stale, False)

    def test_a_custom_ca_that_cannot_be_bound_refuses_with_a_remedy(self) -> None:
        def nothing_changed() -> None:
            self.assertFalse(self.unit_dir.exists())
            self.assertEqual(self.recorded().backend, endpoint.ON_DEMAND)
            self.assertIsNone(gateway_inhibition.read(self.state_root))

        spaced = self.home / "my certs" / "ca.pem"
        spaced.parent.mkdir()
        spaced.write_text("fixture\n")
        cases = {
            "missing file": {"SSL_CERT_FILE": str(self.home / "certs" / "missing.pem")},
            "a folder given as the file": {"SSL_CERT_FILE": str(self.home)},
            "missing folder": {"SSL_CERT_DIR": str(self.root / "no-such-folder")},
            "a path a unit cannot carry": {"SSL_CERT_FILE": str(spaced)},
        }
        for label, variables in cases.items():
            with self.subTest(label):
                outcome = self.service(environ=self.ca_environ(**variables)).install()
                self.assertEqual(outcome.status, "refused", outcome.lines())
                self.assertIn("the gateway service was not installed", outcome.message)
                self.assertIn(next(iter(variables)), outcome.message)
                self.assertIn("unset", outcome.remedy or "")
                self.assertIn(gs.INSTALL, outcome.remedy or "")
                nothing_changed()
        # A refresh refuses the same way and leaves the installed unit as it is.
        self.assertTrue(self.service().install().ok)
        before = self.unit_text()
        outcome = self.service(environ=self.ca_environ(SSL_CERT_FILE=str(self.root / "gone.pem"))).install()
        self.assertEqual(outcome.status, "refused", outcome.lines())
        self.assertIn("the gateway service was not refreshed", outcome.message)
        self.assertEqual(self.unit_text(), before)

    def test_a_home_relative_ca_path_a_unit_cannot_carry_refuses_before_any_change(self) -> None:
        # ~/certs:bad/ca.pem would render BindReadOnlyPaths=%h/certs:bad/ca.pem,
        # which systemd reads as a source and a destination.
        coloned = self.home / "certs:bad" / "ca.pem"
        coloned.parent.mkdir()
        coloned.write_text("fixture\n")
        controlled = self.home / "certs\x01x"
        controlled.mkdir()
        for variables in ({"SSL_CERT_FILE": str(coloned)}, {"SSL_CERT_DIR": str(controlled)}):
            with self.subTest(variables=repr(variables)):
                outcome = self.service(environ=self.ca_environ(**variables)).install()
                self.assertEqual(outcome.status, "refused", outcome.lines())
                self.assertIn("has a path the unit file cannot carry", outcome.message)
                self.assertIn(":", outcome.remedy or "")
                self.assertFalse(self.unit_dir.exists())
                self.assertEqual(self.recorded().backend, endpoint.ON_DEMAND)
                self.assertIsNone(gateway_inhibition.read(self.state_root))
                self.assertEqual(set(self.manager.verbs()) - {"show"}, set())

    def test_a_ca_folder_that_would_show_the_service_home_refuses(self) -> None:
        # HOME, a folder above it, a folder the unit's view hides as a whole,
        # or a link to one of them: bound read-only it would undo the empty
        # home the unit runs with.
        link = self.root / "certs-link"
        link.symlink_to(self.home)
        hidden = next((root for root in gs.HIDDEN_ROOTS if os.path.isdir(root) and os.access(root, os.R_OK | os.X_OK)),
                      None)
        folders = {"home": self.home, "a link to home": link, "above home": self.home.parent, "the root": "/"}
        cases = [(label, folder, gs.HIDDEN_ROOTS) for label, folder in folders.items()]
        # HOME and the folders above it whatever the unit's other hidden roots are.
        cases += [(label, folder, ()) for label, folder in folders.items()]
        if hidden is not None:
            other = self.root / "hidden-link"
            other.symlink_to(hidden)
            cases += [("a hidden root", hidden, gs.HIDDEN_ROOTS), ("a link to a hidden root", other, gs.HIDDEN_ROOTS)]
        for label, folder, hidden_roots in cases:
            with self.subTest(label, hidden_roots=hidden_roots), mock.patch.object(gs, "HIDDEN_ROOTS", hidden_roots):
                outcome = self.service(environ=self.ca_environ(SSL_CERT_DIR=str(folder))).install()
                self.assertEqual(outcome.status, "refused", outcome.lines())
                self.assertIn("SSL_CERT_DIR", outcome.message)
                self.assertIn("home folder", outcome.message)
                self.assertIn("a folder that holds only certificates", outcome.remedy or "")
                self.assertIn("certificate bundle", outcome.remedy or "")
                self.assertFalse(self.unit_dir.exists())
                self.assertIsNone(gateway_inhibition.read(self.state_root))
        # A dedicated folder in HOME is carried and bound.
        dedicated = self.home / "certs"
        dedicated.mkdir()
        outcome = self.service(environ=self.ca_environ(SSL_CERT_DIR=str(dedicated))).install()
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertIn(("BindReadOnlyPaths", "%h/certs"), systemd_unit.parse(self.unit_text())["Service"])

    def test_a_unit_without_a_required_ca_bind_is_stale(self) -> None:
        bundle = self.home / "certs" / "corp.pem"
        bundle.parent.mkdir(mode=0o700)
        bundle.write_text("fixture\n")
        self.assertTrue(self.service(environ=self.ca_environ(SSL_CERT_FILE=str(bundle))).install().ok)
        path = self.unit_dir / "claude-multi-gateway.service"
        self.assertIs(self.service().status().stale, False)
        text = path.read_text()
        self.assertIn("BindReadOnlyPaths=%h/certs/corp.pem\n", text)
        path.write_text(text.replace("BindReadOnlyPaths=%h/certs/corp.pem\n", ""))
        self.assertIs(self.service().status().stale, True)
        self.assertIn("unit: stale", "\n".join(self.service().status().lines(self.environ())))
        # A unit that binds HOME itself (written by an earlier release) is stale too.
        path.write_text(text.replace("%h/certs/corp.pem", str(self.home)).replace(
            "SSL_CERT_FILE=", "SSL_CERT_DIR="))
        self.assertIs(self.service().status().stale, True)

    def test_install_waits_for_the_persistence_hold(self) -> None:
        self.running()
        self.log_file("1111111111111111", [save_line("failed")])
        outcome = self.service().install()
        self.assertEqual(outcome.status, "held")
        self.assertEqual(self.world.terminated, [])
        self.assertFalse(self.unit_dir.exists())

    def test_a_foreign_unit_file_is_never_overwritten(self) -> None:
        self.unit_dir.mkdir(parents=True)
        (self.unit_dir / "claude-multi-gateway.service").write_text("[Service]\nExecStart=/bin/true\n")
        outcome = self.service().install()
        self.assertEqual(outcome.status, "refused")
        self.assertIn("was not written by claude-multi", outcome.message)
        self.assertEqual(self.unit_text(), "[Service]\nExecStart=/bin/true\n")

    def test_a_failed_start_restores_the_on_demand_gateway(self) -> None:
        cases = {
            "enable refused": lambda: setattr(self.manager, "enable_ok", False),
            "never ready": lambda: setattr(self.manager, "unit_starts", False),
            "loaded from elsewhere": lambda: setattr(self.manager, "fragment", "/etc/systemd/user/x.service"),
            # The install's reload is refused, the restoration's succeeds.
            "reload refused": lambda: setattr(self.manager, "reload_outcomes", [False, True]),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.manager = Manager(self)
                self.world.spawned.clear()
                endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.port))
                self.running()
                change()
                outcome = self.service().install(max_wait=1)
                self.assertFalse(outcome.ok, outcome.lines())
                text = "\n".join(outcome.lines())
                self.assertIn("restored: the on-demand gateway runs again", text)
                self.assertEqual(self.recorded(), endpoint.EndpointConfig(port=self.port))
                self.assertEqual(sorted(self.unit_dir.glob("*.service")) if self.unit_dir.exists() else [], [])
                self.assertEqual(len(self.world.spawned), 1)  # the on-demand instance, started again
                self.assertIsNone(gateway_inhibition.read(self.state_root))
                if label in ("never ready", "enable refused"):
                    self.assertIn("disable", self.manager.verbs())

    def test_a_failed_start_shows_the_units_journal_tail_redacted(self) -> None:
        # The hand-off's failure carries the unit journal's tail: a line
        # separator inside a record never splits a header from its value, and
        # a private key after a field name, or one whose BEGIN line is above
        # the lines shown, shows nothing of its body.
        separators = _log_secrets.line_break_cases()
        cases = {
            "line separators": ([line for _secrets, line in separators],
                                sorted({secret for secrets, _line in separators for secret in secrets})),
            "field-named key": (_log_secrets.field_key_cases()[0], list(_log_secrets.SHORT_KEY_PIECES)),
            "unfinished key": (_log_secrets.unfinished_key_lines(gl.LOG_TAIL_LINES + 5, field=True),
                               list(_log_secrets.SHORT_KEY_PIECES)),
        }
        for label, (lines, secrets) in cases.items():
            with self.subTest(label):
                self.manager = Manager(self)
                self.manager.unit_starts = False
                self.manager.journal_output = ("\n".join(lines) + "\n").encode()
                self.world.spawned.clear()
                endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.port))
                self.running()
                outcome = self.service().install(max_wait=1)
                self.assertFalse(outcome.ok, outcome.lines())
                self.assertIn("did not become ready", outcome.message)
                self.assertTrue(outcome.log_tail)
                self.assertIn(secret_store.REDACTED, "\n".join(outcome.log_tail))
                text = "\n".join(outcome.lines())
                for secret in secrets:
                    self.assertNotIn(secret, text)

    def test_a_restoration_the_manager_did_not_reload_keeps_the_record(self) -> None:
        """A failed install whose restoring daemon-reload is refused too is
        not completely restored: the hand-off's record stays with its remedy
        (every change refused, a gateway proven ours still delivered), and a
        later install finishes it."""

        endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.port))
        self.running()
        self.manager.reload_ok = False
        outcome = self.service().install(max_wait=1)
        text = "\n".join(outcome.lines())
        self.assertFalse(outcome.ok, text)
        self.assertIn("restored: the on-demand gateway runs again", text)
        self.assertIn("did not reload the restored unit files, so the hand-off is not finished", text)
        self.assertIn(f"finish it: {gs.INSTALL}", text)
        self.assertEqual(self.recorded(), endpoint.EndpointConfig(port=self.port))
        record = gateway_inhibition.read(self.state_root)
        self.assertIsNotNone(record)
        self.assertEqual((record.owner, record.phase), ("service", "install"))
        self.assertEqual(self.gateway().ensure(max_wait=0).status, "ready")  # proven ours: delivered
        self.assertEqual(self.gateway().stop().status, "inhibited")
        # Once the manager reloads again, an install finishes it.
        self.manager.reload_ok = True
        outcome = self.service().install(max_wait=1)
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertIsNone(gateway_inhibition.read(self.state_root))

    def test_the_restoration_never_stops_a_held_or_unproven_unit(self) -> None:
        failed = observation.LogRecord(save_line("failed"), self.world.now, "boot:invocation", "c1")
        cases = {
            "held": (dict(journal=self.journal(failed)), "the persistence hold applies"),
            "unproven": (dict(), "is not proven yours"),
        }
        for label, (overrides, reason) in cases.items():
            with self.subTest(label):
                self.manager = Manager(self)
                for path in self.unit_dir.glob("*.service") if self.unit_dir.exists() else ():
                    path.unlink()
                endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.port))
                gateway_inhibition.record_path(self.state_root).unlink(missing_ok=True)
                # The previous case's journal failure was recorded as a hold; start clean.
                (self.workdir / "persistence-hold.json").unlink(missing_ok=True)
                self.running()
                self.world.health = 503  # the unit's gateway never becomes ready
                if label == "unproven":  # its lock is held by a process that cannot be identified
                    self.manager.after_enable = lambda: (
                        setattr(self.world, "lock", True), setattr(self.world, "alive", None))
                outcome = self.service(seams=self.seams(**overrides)).install(max_wait=1)
                self.world.health = 200
                text = "\n".join(outcome.lines())
                self.assertFalse(outcome.ok, text)
                self.assertIn("restore stopped", text)
                self.assertIn(reason, text)
                self.assertNotIn("disable", self.manager.verbs())
                # The service stays installed and selected; automatic starts stay paused.
                self.assertTrue(systemd_unit.written_here(self.unit_text()))
                self.assertEqual(self.recorded().backend, endpoint.SYSTEMD)
                self.assertIsNotNone(gateway_inhibition.read(self.state_root))
                # Every change stays refused; a gateway proven ours is still
                # delivered (read-only), anything unproven is not.
                spawned = len(self.world.spawned)
                self.assertEqual(self.gateway().ensure(max_wait=0).status,
                                 "ready" if label == "held" else "inhibited")
                self.assertEqual(self.gateway().stop().status, "inhibited")
                self.assertEqual(len(self.world.spawned), spawned)

    def test_a_new_install_records_its_own_port(self) -> None:
        endpoint.endpoint_path(self.home).unlink()
        (self.home / ".config/claude-multi/api-key").unlink()  # no rendered gateway yet: a new install
        packaged = endpoint.port_of(self.document["gateway"]["base_url"])
        observed = []

        def listener(base, pid):
            observed.append(endpoint.port_of(base))
            if base.endswith(f":{packaged}"):  # something unrelated holds the packaged port
                return service.OwnerVerdict("foreign", "another program", uid=1234)
            return self.world.verdict(base, pid)

        with mock.patch.object(endpoint, "select_port", return_value=18321) as select:
            outcome = self.service(seams=self.seams(port_probe=lambda _p: True, listener=listener)).install()
        self.assertTrue(outcome.ok, outcome.lines())
        select.assert_called()
        self.assertNotIn(packaged, observed)
        self.assertEqual((self.recorded().port, self.recorded().backend), (18321, endpoint.SYSTEMD))


class NixSelectionTests(_ServiceCase):
    CHANNEL = "nix"

    def environ(self, **extra: str) -> dict[str, str]:
        package = self.store_package("aaaa-claude-multi", "gw-1")
        return super().environ(CLAUDE_MULTI_HOOK_COMMAND=str(package / "bin/claude-multi"),
                               PATH=str(self.root / "tools"), **extra)

    def setUp(self) -> None:
        super().setUp()
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "nix-store").write_text("#!/bin/sh\nexit 0\n")
        (tools / "nix-store").chmod(0o755)

    def test_install_links_this_store_path_and_registers_its_root(self) -> None:
        outcome = self.service().install()
        self.assertTrue(outcome.ok, outcome.lines())
        link = self.home / ".local/share/claude-multi/nix/current"
        self.assertEqual(os.readlink(link), str(self.store / "aaaa-claude-multi"))
        self.assertEqual(self.manager.gc_roots, [(str(link), str(self.store / "aaaa-claude-multi"))])
        self.assertIn("%h/.local/share/claude-multi/nix/current/bin/claude-multi-proxy", self.unit_text())
        self.assertFalse(os.path.lexists(self.home / ".local/share/claude-multi/install/current")
                         and os.readlink(self.home / ".local/share/claude-multi/install/current") != "versions/1.0.0")

    def test_without_nix_store_the_link_is_kept_with_a_note(self) -> None:
        (self.root / "tools" / "nix-store").unlink()
        outcome = self.service().install()
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertIn("not a garbage-collector root", "\n".join(outcome.lines()))

    def test_refresh_classifies_reload_and_restart(self) -> None:
        self.assertTrue(self.service().install().ok)
        status = self.service().status()
        self.assertFalse(status.stale)
        self.assertFalse(status.switch_pending)
        self.assertEqual(self.service().install().message, "gateway service claude-multi-gateway is installed and current")
        # The running unit gateway runs the first package's gateway binary.
        self.world.write_stamp("bbbbbbbbbbbbbbbb")
        stamp = service.read_exec_stamp(self.workdir)
        service.write_exec_stamp(self.workdir, service.ExecStamp(
            stamp.launcher_version, stamp.signature, stamp.pid,
            str(self.store / "gw-1/bin/cli-proxy-api"), stamp.started_at, stamp.pid_namespace, stamp.instance))
        # A launcher-only change: another store path, the same gateway.
        same = self.store_package("bbbb-claude-multi", "gw-1")
        env = super().environ(CLAUDE_MULTI_HOOK_COMMAND=str(same / "bin/claude-multi"), PATH=str(self.root / "tools"))
        self.assertTrue(self.service(environ=env).status().switch_pending)
        outcome = self.service(environ=env).install()
        self.assertIn("reloaded: only the launcher changed", "\n".join(outcome.lines()))
        self.assertEqual(self.manager.reloaded, ["claude-multi-gateway"])
        # A new gateway binary: restart pending, never a silent restart.
        newer = self.store_package("cccc-claude-multi", "gw-2")
        env = super().environ(CLAUDE_MULTI_HOOK_COMMAND=str(newer / "bin/claude-multi"), PATH=str(self.root / "tools"))
        outcome = self.service(environ=env).install()
        self.assertIn("restart pending: the gateway binary changed", "\n".join(outcome.lines()))
        self.assertEqual(self.manager.reloaded, ["claude-multi-gateway"])
        self.assertEqual(os.readlink(self.home / ".local/share/claude-multi/nix/current"), str(newer))
        # A changed unit text: restart pending.
        (self.unit_dir / "claude-multi-gateway.service").write_text(self.unit_text().replace("RestartSec=5s", "RestartSec=9s"))
        self.assertTrue(self.service(environ=env).status().stale)
        outcome = self.service(environ=env).install()
        self.assertIn("restart pending: the unit changed", "\n".join(outcome.lines()))
        self.assertFalse(self.service(environ=env).status().stale)

    def test_uninstall_reverses_the_hand_off(self) -> None:
        self.running()
        self.assertTrue(self.service().install().ok)
        self.world.spawned.clear()
        outcome = self.service().uninstall()
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertIn("gateway service claude-multi-gateway removed", outcome.message)
        self.assertIn("disable", self.manager.verbs())
        self.assertEqual(list(self.unit_dir.glob("*.service")), [])
        self.assertEqual(self.recorded(), endpoint.EndpointConfig(port=self.port))
        self.assertFalse(os.path.lexists(self.home / ".local/share/claude-multi/nix/current"))
        self.assertEqual(len(self.world.spawned), 1)  # the on-demand gateway runs again
        self.assertIsNone(gateway_inhibition.read(self.state_root))
        self.assertEqual(self.service().uninstall().message,
                         "the gateway service is not installed; nothing was changed")

    def test_uninstall_waits_for_the_hold_and_restores_on_failure(self) -> None:
        self.running()
        self.assertTrue(self.service().install().ok)
        held = observation.LogRecord(save_line("failed").split("] ", 4)[-1], None, None, None)
        outcome = self.service(seams=self.seams(journal=self.journal(held))).uninstall()
        self.assertEqual(outcome.status, "held", outcome.lines())
        self.assertNotIn("disable", self.manager.verbs())
        # The on-demand start fails: the service comes back.
        self.world.spawn_code = 1
        outcome = self.service().uninstall()
        self.assertFalse(outcome.ok)
        self.assertIn("restored: the gateway service runs again", "\n".join(outcome.lines()))
        self.assertEqual(self.recorded().backend, endpoint.SYSTEMD)
        self.assertTrue(systemd_unit.written_here(self.unit_text()))
        self.assertTrue(os.path.islink(self.home / ".local/share/claude-multi/nix/current"))
        self.assertEqual(self.manager.verbs()[-2:], ["daemon-reload", "enable"])
        self.assertIsNone(gateway_inhibition.read(self.state_root))  # completely restored, reload included


    def test_an_uninstall_restoration_the_manager_did_not_reload_keeps_the_record(self) -> None:
        self.running()
        self.assertTrue(self.service().install().ok)
        self.world.spawn_code = 1  # the on-demand start fails: the service is restored
        self.manager.reload_outcomes = [True, False]  # the uninstall's reload, then the restoration's
        outcome = self.service().uninstall()
        text = "\n".join(outcome.lines())
        self.assertFalse(outcome.ok, text)
        self.assertIn("did not reload the restored unit files, so the hand-off is not finished", text)
        self.assertEqual(self.recorded().backend, endpoint.SYSTEMD)
        record = gateway_inhibition.read(self.state_root)
        self.assertIsNotNone(record)
        self.assertEqual((record.owner, record.phase), ("service", "uninstall"))
        # A completely restored uninstall removes it; here an install finishes it.
        self.world.spawn_code = 0
        outcome = self.service().install(max_wait=1)
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertIn("an interrupted hand-off was finished", outcome.message)
        self.assertIsNone(gateway_inhibition.read(self.state_root))


class StatusAndInhibitionTests(_ServiceCase):
    def test_status_reports_installed_stale_and_missing(self) -> None:
        status = self.service().status()
        self.assertFalse(status.installed)
        self.assertEqual(status.attention({"HOME": str(self.home)}), [])
        self.assertIn("gateway service: not installed — claude-multi-gateway", status.lines({"HOME": str(self.home)})[0])
        self.assertTrue(self.service().install().ok)
        status = self.service().status()
        self.assertTrue(status.installed and status.recorded)
        self.assertIs(status.stale, False)
        self.assertEqual(status.properties["UnitFileState"], "enabled")
        text = self.unit_text().replace("UMask=0077", "UMask=0022")
        (self.unit_dir / "claude-multi-gateway.service").write_text(text)
        attention = self.service().status().attention({"HOME": str(self.home)})
        self.assertTrue(any("differs from what this release renders" in line for line in attention))
        (self.unit_dir / "claude-multi-gateway.service").unlink()
        self.manager.loaded.discard("claude-multi-gateway")
        attention = self.service().status().attention({"HOME": str(self.home)})
        self.assertTrue(any("recorded in endpoint.json but the service manager has no such unit" in line
                            for line in attention))
        self.assertEqual(self.service().status(live=False).attention({"HOME": str(self.home)}), [])

    def handoff_record(self, *, pid: int, started: "datetime.datetime") -> str:
        """The inhibition a service hand-off of process ``pid`` recorded at ``started``."""

        return gateway_inhibition.begin(
            self.state_root, owner=gl.SERVICE_OWNER, purpose="gateway service install (claude-multi-gateway)",
            phase="install", remedy=gl.SERVICE_REMEDY, expiry=gateway_inhibition.owner_process(pid),
            now=started)

    def test_a_hand_off_record_stops_automatic_starts(self) -> None:
        self.handoff_record(pid=os.getppid(), started=self.world.now)
        outcome = self.gateway().ensure(max_wait=0)
        self.assertEqual(outcome.status, "inhibited")
        self.assertIn("inhibited by service (gateway service install (claude-multi-gateway); phase install",
                      outcome.message)
        self.assertNotIn("stale", outcome.message)
        self.assertIn(gl.SERVICE_REMEDY, outcome.remedy)
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(self.world.spawned, [])
        self.world.now = self.world.now + STALE_AFTER * 2  # the hand-off died long ago
        outcome = self.gateway().ensure(max_wait=0)
        self.assertEqual(outcome.status, "inhibited")
        self.assertIn("its owner did not finish (stale)", outcome.message)
        self.assertEqual(self.world.spawned, [])
        # A record this process wrote is no pass for another caller here either.
        gateway_inhibition.record_path(self.state_root).unlink()
        self.handoff_record(pid=os.getpid(), started=self.world.now)
        self.assertEqual(self.gateway().ensure(max_wait=1).status, "inhibited")
        # Only the hand-off's own continuation (the object holding its token) passes.
        gateway_inhibition.record_path(self.state_root).unlink()
        owner = self.gateway()
        owner.begin_handoff("install", "claude-multi-gateway")
        self.assertEqual(gateway_inhibition.read(self.state_root).owner, gl.SERVICE_OWNER)
        self.assertEqual(self.gateway().ensure(max_wait=1).status, "inhibited")
        self.assertTrue(owner.ensure(max_wait=1).ok)
        self.assertEqual(len(self.world.spawned), 1)
        self.assertEqual(self.world.spawned[0]["env"][gateway_inhibition.TOKEN_ENV], owner.inhibition_token)
        owner.end_handoff()
        self.assertIsNone(gateway_inhibition.read(self.state_root))

    def test_a_dead_hand_off_with_a_reused_pid_is_refused(self) -> None:
        self.handoff_record(pid=os.getpid(), started=self.world.now - datetime.timedelta(days=1))
        outcome = self.gateway().ensure(max_wait=1)
        self.assertEqual(outcome.status, "inhibited", outcome.lines())
        self.assertIn("its owner did not finish (stale)", outcome.message)
        self.assertEqual(self.world.spawned, [])
        # A record without its token is unreadable, which refuses too.
        document = json.loads(gateway_inhibition.record_path(self.state_root).read_text())
        del document["token"]
        state.atomic_write(gateway_inhibition.record_path(self.state_root), json.dumps(document).encode())
        outcome = self.gateway().ensure(max_wait=1)
        self.assertEqual(outcome.status, "inhibited")
        self.assertIn("unreadable record", outcome.message)
        self.assertEqual(self.world.spawned, [])
        # ... and no service verb takes an unreadable record over.
        self.assertEqual(self.service().install().status, "inhibited")
        self.assertFalse(self.unit_dir.exists())

    def test_install_finishes_an_interrupted_hand_off(self) -> None:
        self.assertTrue(self.service().install().ok)
        self.handoff_record(pid=os.getppid(), started=self.world.now - STALE_AFTER * 2)
        self.assertEqual(self.gateway().ensure(max_wait=0).status, "ready")  # proven ours: read-only delivery
        self.assertEqual(self.gateway().restart().status, "inhibited")
        # The retry cannot finish it while the unit's gateway never comes up: the record stays.
        self.manager.active.clear()
        self.world.lock, self.world.alive, self.world.listener = False, False, "none"
        self.manager.unit_starts = False
        outcome = self.service().install(max_wait=1)
        self.assertFalse(outcome.ok, outcome.lines())
        self.assertIn("the interrupted hand-off is not finished", outcome.message)
        self.assertIsNotNone(gateway_inhibition.read(self.state_root))
        self.assertEqual(self.gateway().ensure(max_wait=0).status, "inhibited")
        # Once the service starts and is proven, the hand-off is finished and starts resume.
        self.manager.unit_starts = True
        outcome = self.service().install()
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertIn("an interrupted hand-off was finished", outcome.message)
        self.assertIsNone(gateway_inhibition.read(self.state_root))
        self.assertEqual(self.gateway().ensure(max_wait=1).status, "ready")
        self.assertEqual(self.service().install().message,
                         "gateway service claude-multi-gateway is installed and current")

    def test_a_refresh_planned_before_an_uninstall_changes_nothing(self) -> None:
        self.assertTrue(self.service().install().ok)
        manager = self.service()
        take_lock = manager._lock

        def uninstall_first():
            self.assertTrue(self.service().uninstall().ok)
            return take_lock()

        manager._lock = uninstall_first  # type: ignore[method-assign]
        outcome = manager.install()
        self.assertEqual(outcome.status, "refused", outcome.lines())
        self.assertIn("changed meanwhile", outcome.message)
        self.assertEqual(list(self.unit_dir.glob("*.service")), [])
        self.assertEqual(self.recorded().backend, endpoint.ON_DEMAND)

    def test_a_refresh_recreates_missing_service_directories(self) -> None:
        self.assertTrue(self.service().install().ok)
        traces = self.home / ".local/share/claude-multi/traces"
        traces.rmdir()
        outcome = self.service().install()
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertIn("recreated the missing service directories (.local/share/claude-multi/traces)",
                      "\n".join(outcome.lines()))
        self.assertEqual(traces.stat().st_mode & 0o777, 0o700)

    def test_the_start_lock_comes_before_the_backend(self) -> None:
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.port, backend=endpoint.SYSTEMD,
                                                                 unit="claude-multi-gateway"))
        lock = state.FileLock(self.workdir / service.START_LOCK.removesuffix(".lock"))
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertEqual(self.gateway().ensure(max_wait=0).status, "busy")
            self.assertEqual(self.gateway().stop().status, "busy")
        finally:
            lock.release()
        self.assertEqual(self.manager.calls, [])

    def test_uninstall_clears_a_record_whose_unit_is_gone(self) -> None:
        self.assertTrue(self.service().install().ok)
        for path in self.unit_dir.glob("*.service"):
            path.unlink()
        self.manager.loaded.clear()
        self.manager.active.clear()
        self.world.lock, self.world.alive, self.world.listener = False, False, "none"
        outcome = self.service().uninstall()
        self.assertTrue(outcome.ok, outcome.lines())
        self.assertEqual(self.manager.verbs().count("disable"), 0)
        self.assertEqual(self.recorded(), endpoint.EndpointConfig(port=self.port))

    def test_the_tui_offers_install_and_keeps_uninstall_in_the_terminal(self) -> None:
        from claude_multi.cli.screens import gateway_actions

        self.assertIn("gateway service uninstall", gateway_actions.CLI_ONLY)
        darwin = self.service(platform="darwin")
        view = gateway_actions.GatewayView(darwin.gateway, darwin.gateway.observe(), service=darwin,
                                           service_status=darwin.status())
        self.assertFalse(view.can_install_service)
        self.assertEqual(view.service_line(), "service: not available here (on-demand only)")
        manager = self.service()
        view = gateway_actions.GatewayView(manager.gateway, manager.gateway.observe(), service=manager,
                                           service_status=manager.status())
        self.assertTrue(view.can_install_service)
        self.assertEqual(view.service_line(), "service: not installed (the gateway starts on demand)")
        self.assertTrue(gateway_actions.act(view, "service").ok)
        manager = self.service()
        view = gateway_actions.GatewayView(manager.gateway, manager.gateway.observe(), service=manager,
                                           service_status=manager.status())
        self.assertFalse(view.can_install_service)  # installed and current
        self.assertEqual(view.service_line(), "service: installed (claude-multi-gateway)")
        self.assertEqual(gateway_actions.act(view, "service").status, "refused")
        # A fixture view (nothing observed) never offers it.
        self.assertFalse(gateway_actions.GatewayView(None, None).can_install_service)

    def test_the_cli_verbs(self) -> None:
        launcher = self.home / ".local/share/claude-multi/install/current/bin/claude-multi"
        runtime = cli.Runtime(asset_root=FIXTURE_ROOT, environ=self.environ(CLAUDE_MULTI_HOOK_COMMAND=str(launcher)),
                              cwd=self.root,
                              managed_root=self.root / "managed", gateway_seams=self.seams(),
                              gateway_service_seams=gs.ServiceSeams(runner=self.manager, which=lambda *_a, **_k: None))

        def run(*argv: str) -> tuple[int, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stderr(err):
                code = cli.main(["gateway", "service", *argv], runtime=runtime, output_stream=out, interactive=False)
            return code, out.getvalue(), err.getvalue()

        code, out, _ = run("status")
        self.assertEqual(code, 0)
        self.assertIn("gateway service: not installed", out)
        code, out, err = run("install")
        self.assertEqual(code, 0, err)
        self.assertIn("installed and running", out)
        code, out, _ = run("status")
        self.assertIn("gateway service: installed — claude-multi-gateway", out)
        code, out, err = run("uninstall")
        self.assertEqual(code, 0, err)
        self.assertIn("removed", out)


if __name__ == "__main__":
    unittest.main()
