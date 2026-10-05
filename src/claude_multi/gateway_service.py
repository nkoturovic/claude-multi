"""``claude-multi gateway service install|uninstall|status``: the opt-in supervised gateway.

The gateway runs on demand by default and needs no service manager. On Linux
with a reachable user service manager the user may hand it to a hardened
systemd user unit instead, which restarts it on failure and confines it
(``ProtectHome=tmpfs`` with explicit binds plus Tier B). The unit is rendered
from the one service spec (``gateway-unit.json`` in the packaged resources)
by ``platform.systemd_unit`` and written as ``<name>.service`` (default name
``claude-multi-gateway``; ``--name`` chooses another) in the user's unit
directory; with ``notify-send`` on PATH a ``<name>-failure.service`` notice
unit joins it. The port stays the one ``endpoint.json`` records.

Install hands the gateway over under the start lock: no inhibition another
owner holds may be recorded (a stale one of an interrupted hand-off is taken
over and finished), a new install then
records its port (from the new-install range; an existing install keeps its
own), whatever listens on that port must be proven ours, the persistence hold must be clear, the
hand-off's inhibition (owner ``service``) stops every other change, the on-demand instance is stopped and
its exit proven, the private directories are created (0700), the installation
the unit executes is selected (bundles: the installer's ``current`` link;
Nix: the link is pointed at this launcher's store path and registered as a
garbage-collector root), the unit files are written atomically, the manager
reloads them (and must load them from that file), ``endpoint.json`` records
``backend: systemd`` and the unit, the unit is enabled and started, and its
gateway is proven ours and answering. Any failure restores the on-demand
backend and, when it ran before, the on-demand gateway. The restoration
stops the new unit only behind the same checks as any stop (proven ours,
persistence hold); when that stop is vetoed or its exit cannot be proven,
the service stays installed and selected, the hand-off record keeps
automatic starts paused, and the outcome says what remains to finish.

Run again on an installed service, install refreshes the unit files and the
selected installation, then says what the running gateway needs: nothing, a
reload (only the launcher changed) or a restart (the unit or the gateway
binary changed; ``claude-multi gateway restart`` is guarded by the hold).
The refresh records the hand-off's inhibition (phase ``refresh``) before its
first change and removes it once complete (or completely restored: the
files back and reloaded by the manager); an interrupted refresh or an
unfinished restoration keeps it, and the next install finishes it.

The unit carries the certificates the installing launcher trusts: each of
``SSL_CERT_FILE`` and ``SSL_CERT_DIR`` its environment sets
(``tls.configured``) goes into the unit's environment, as an absolute or
``%h``-relative path, and is bound read-only where the unit's view hides it
(the empty home, a private ``/tmp``). Install and refresh refuse, changing
nothing, when a set location is not a readable file or folder, has a path a
unit cannot carry, or would show the unit HOME (HOME, a folder above it, a
hidden root or a folder above one, as set or through a link); status judges
an installed unit by the certificate locations it carries, with the binds
the hardening policy requires for them, so its staleness never depends on
the shell asking or on the unit's own binds.

Uninstall reverses the hand-off the same way (hold, record, stop and prove
the exit, remove only files this product wrote, reload the manager, record
the on-demand backend, start the on-demand gateway when the service ran) and
restores the service when the on-demand start fails. Run again after an
uninstall interrupted once it had recorded the on-demand backend, uninstall
finishes it (unit stopped, files gone, the manager reloaded) and removes its
record. Status reports whether
the service is installed, its name and backend, and a stale unit (text that
differs from what this release renders).
"""

from __future__ import annotations

import dataclasses
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import claude_multi
from claude_multi import (endpoint, errors, gateway_hold, gateway_inhibition, gateway_lifecycle, installs, paths,
                          service, state, strict_json, tls)
from claude_multi.platform import linux_service, systemd_unit

SPEC_FILE = Path("gateway-unit.json")
NOTIFIER = "notify-send"
ACTIVE_STATES = frozenset({"active", "activating", "reloading", "deactivating"})
_UNSAFE_HOME = frozenset(" \t\n\r%$\\'\"")
RESTART = "claude-multi gateway restart"
INSTALL = "claude-multi gateway service install"
UNINSTALL = "claude-multi gateway service uninstall"
# The purpose a hand-off records (Gateway.begin_handoff) names its unit.
_HANDOFF_PURPOSE = re.compile(r"gateway service (?:install|uninstall|refresh) \(([a-z][a-z0-9-]{0,63})\)")
# What the unit's view hides besides its home (ProtectHome=tmpfs empties
# every home and /run/user; PrivateTmp gives it a /tmp of its own): a
# certificate location under one of them is bound read-only into the unit.
HIDDEN_ROOTS = ("/home", "/root", "/run/user", "/tmp", "/var/tmp")
TRUST_REMEDY = (f"point SSL_CERT_FILE at a readable certificate bundle, or SSL_CERT_DIR at a folder that holds "
                f"only certificates (never your home folder or a folder above it), on a path with no spaces, "
                f"quotes, %, $, : or backslashes, or unset them to use the system certificates; then run "
                f"{INSTALL} again")


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def _homes(home: Path) -> tuple[str, ...]:
    return tuple(dict.fromkeys((os.path.normpath(os.path.abspath(home)), os.path.realpath(home))))


def _bind_required(path: str, homes: tuple[str, ...]) -> bool:
    """Whether the unit's view hides a certificate location as the unit
    writes it (``%h/…`` or absolute), so the unit must bind it read-only:
    the hardening policy's rule, never what a unit's text says."""

    return path.startswith("%h/") or _under(path, (*homes, *HIDDEN_ROOTS))


def _exposes(path: str, homes: tuple[str, ...]) -> bool:
    """Whether binding ``path`` would show the unit more than certificates:
    HOME, a folder above it, a folder the unit's view hides as a whole
    (:data:`HIDDEN_ROOTS`) or a folder above one, as written or through a
    link."""

    roots = (*homes, *HIDDEN_ROOTS)
    return any(_under(root, (candidate,)) for candidate in (path, os.path.realpath(path)) for root in roots)


def _unit_path(path: str, home: Path) -> str:
    """A certificate location as the unit writes it, as a path here."""

    return os.path.join(os.path.abspath(home), path[3:]) if path.startswith("%h/") else path


def unit_trust(environ: Mapping[str, str], home: Path) -> tuple[tuple[systemd_unit.TrustPath, ...], str | None]:
    """The certificate trust the unit carries: each ``SSL_CERT_FILE`` /
    ``SSL_CERT_DIR`` the environment sets (a relative one from the current
    directory), written ``%h``-relative under HOME and bound read-only
    where the unit's view hides it (the location as set, or the file it
    links to); or why one cannot be carried, with nothing to carry: not a
    readable file or folder, a path a unit cannot carry, or a location
    whose bind would show the unit HOME (HOME, a folder above it, a hidden
    root, or a link to one of them)."""

    homes = _homes(home)
    hidden = (*homes, *HIDDEN_ROOTS)

    def written(path: str) -> str:
        for root in homes:
            if _under(path, (root,)) and path != root:
                return "%h/" + os.path.relpath(path, root)
        return path

    found = []
    for variable, value in tls.configured(environ):
        shown = f"{variable}={paths.display(value, environ) if os.path.isabs(value) else value}"
        absolute = os.path.normpath(os.path.abspath(value))
        folder = variable == "SSL_CERT_DIR"
        present = os.path.isdir(absolute) if folder else os.path.isfile(absolute)
        if not present or not os.access(absolute, os.R_OK | (os.X_OK if folder else 0)):
            return (), f"{shown} is not a readable certificate {'folder' if folder else 'file'}"
        if _exposes(absolute, homes):
            return (), (f"{shown} is your home folder, a folder above it or a private system folder (or links "
                        f"to one); the service may see certificates only")
        real = os.path.realpath(absolute)
        # The location as set, unless only the file it links to is hidden.
        chosen = real if not _under(absolute, hidden) and _under(real, hidden) else absolute
        item = systemd_unit.TrustPath(variable, written(chosen), _bind_required(written(chosen), homes))
        try:
            systemd_unit.check_trust_path(item)
        except systemd_unit.UnitSpecError:
            return (), f"{shown} has a path the unit file cannot carry (spaces, quotes, %, $, : or backslashes)"
        found.append(item)
    return tuple(found), None


def trust_notes(trust: tuple[systemd_unit.TrustPath, ...], environ: Mapping[str, str]) -> list[str]:
    """One line per certificate location the unit carries."""

    def shown(path: str) -> str:
        return "~/" + path[3:] if path.startswith("%h/") else paths.display(path, environ)

    return [f"certificates: {item.variable}={shown(item.path)}" + (" (bound read-only)" if item.bind else "")
            for item in trust]


class ServiceSetupError(errors.ClaudeMultiError, RuntimeError):
    """The service spec cannot be loaded."""


def load_spec(root: Path | str | None = None) -> dict[str, Any]:
    """The running release's service spec (packaged data, never an asset override)."""

    path = Path(root if root is not None else claude_multi.resources_root()) / SPEC_FILE
    try:
        document = strict_json.load(path)
    except (OSError, ValueError) as exc:
        raise ServiceSetupError(f"the gateway service spec cannot be read ({path})",
                                remedy="reinstall claude-multi") from exc
    try:
        return systemd_unit.validate_spec(document)
    except systemd_unit.UnitSpecError as exc:
        raise ServiceSetupError(f"the gateway service spec is invalid: {exc}", remedy="reinstall claude-multi") from exc


@dataclass
class ServiceSeams:
    """The service manager and tool lookups; tests replace them."""

    runner: Callable = subprocess.run
    which: Callable[..., str | None] = shutil.which


@dataclass
class _Undo:
    """What a hand-off changed so far, in order, for its restoration."""

    name: str = ""
    restart_on_demand: bool = False
    selection: installs.Selection | None = None
    files: dict[Path, bytes | None] = field(default_factory=dict)
    written: bool = False
    config_written: bool = False
    config: endpoint.EndpointConfig | None = None
    enabled: bool = False
    removed_link: str | None = None


@dataclass(frozen=True)
class ServiceStatus:
    """What ``gateway service status`` and doctor report."""

    supported: str | None  # None, or why the service cannot run here
    name: str
    recorded: bool  # endpoint.json records the systemd backend for this unit
    backend: str
    unit_path: Path
    installed: bool  # our unit file is present
    foreign: bool  # a file of that name exists that this product did not write
    stale: bool | None  # None: cannot tell
    properties: Mapping[str, str] | None
    exec_root: str | None
    link_target: str | None
    launcher_root: str | None
    channel: str | None

    @property
    def switch_pending(self) -> bool:
        """The service runs another Nix store path than this launcher's."""

        return (self.recorded and self.channel == "nix" and self.launcher_root is not None
                and self.link_target is not None and self.link_target != self.launcher_root)

    def lines(self, environ: Mapping[str, str]) -> list[str]:
        where = paths.display(self.unit_path, environ)
        if self.installed:
            state_text = "installed"
        elif self.foreign:
            state_text = "not installed (a file of that name was not written by claude-multi)"
        else:
            state_text = "not installed"
        lines = [f"gateway service: {state_text} — {self.name} ({where})",
                 f"  backend: {self.backend}" + (" (recorded in endpoint.json)" if self.recorded else "")]
        if self.supported is not None:
            lines.append(f"  supported here: no — {self.supported}")
        if self.properties:
            lines.append("  service manager: " + " · ".join(
                f"{key} {self.properties.get(key) or 'unknown'}" for key in ("UnitFileState", "ActiveState")))
        if self.exec_root is not None:
            target = f" -> {self.link_target}" if self.link_target else ""
            lines.append(f"  runs: ~/{self.exec_root}{target}")
        if self.stale:
            lines.append("  unit: stale (differs from what this release renders)")
        elif self.stale is False:
            lines.append("  unit: current")
        lines.extend(f"  {line}" for line in self.attention(environ))
        return lines

    def attention(self, environ: Mapping[str, str]) -> list[str]:
        where = paths.display(self.unit_path, environ)
        result = []
        # Only the service manager can tell a missing unit from one installed
        # another way under that name (it is then loaded).
        if self.recorded and not self.installed and self.properties is not None and (
                self.properties.get("LoadState") != "loaded"):
            result.append(f"the gateway service {self.name} is recorded in endpoint.json but the service manager "
                          f"has no such unit ({where} is missing): restore it with {INSTALL}, or remove it with "
                          f"{UNINSTALL}")
        if self.installed and self.stale:
            result.append(f"the gateway service unit {where} differs from what this release renders: "
                          f"{INSTALL} refreshes it")
        if self.switch_pending:
            result.append(f"gateway restart pending — the service runs {self.link_target}, this launcher is "
                          f"{self.launcher_root}: run {INSTALL} to switch")
        return result


class GatewayService:
    """The supervised service of one gateway (its home, state root and endpoint)."""

    def __init__(self, gateway: gateway_lifecycle.Gateway, *, installation: Path | str | None = None,
                 seams: ServiceSeams | None = None, spec_root: Path | str | None = None) -> None:
        self.gateway = gateway
        self.home = gateway.home
        self.environ = gateway.environ
        self.installation = Path(installation) if installation is not None else gateway.installation
        self.seams = seams or ServiceSeams(runner=gateway.seams.runner)
        self.spec_root = spec_root
        self._spec: dict[str, Any] | None = None

    # ------------------------------------------------------------ facts
    def spec(self) -> dict[str, Any]:
        if self._spec is None:
            self._spec = load_spec(self.spec_root)
        return self._spec

    def unit_dir(self) -> Path:
        return Path(linux_service.unit_dir({**self.environ, "HOME": str(self.home)}))

    def unit_path(self, name: str) -> Path:
        return self.unit_dir() / f"{name}.service"

    def failure_path(self, name: str) -> Path:
        return self.unit_dir() / f"{name}-failure.service"

    def notifier(self) -> str | None:
        found = self.seams.which(NOTIFIER, path=self.environ.get("PATH") or None)
        return os.path.realpath(found) if found else None

    def channel(self) -> str | None:
        return endpoint.channel(self.environ)

    def recorded_unit(self) -> str | None:
        config = self.gateway.config
        return config.service_unit if config is not None and config.backend == endpoint.SYSTEMD else None

    def plan(self, name: str, exec_root: str,
             trust: tuple[systemd_unit.TrustPath, ...] = ()) -> systemd_unit.UnitPlan:
        notify = self.notifier()
        return systemd_unit.UnitPlan(name, exec_root, notify, service.failure_notice(name) if notify else None,
                                     trust)

    def rendered(self, plan: systemd_unit.UnitPlan) -> dict[Path, str]:
        return {self.unit_dir() / filename: text for filename, text in systemd_unit.render(self.spec(), plan).items()}

    def unsupported(self) -> str | None:
        """Why the service cannot be installed here (None when it can)."""

        if not self.gateway.platform.startswith("linux"):
            return "the supervised service needs Linux with a user service manager"
        expected = self.home / self.spec()["state_root"]
        if Path(self.gateway.state_root) != expected:
            return (f"the state root is {paths.display(self.gateway.state_root, self.environ)}; the service runs "
                    f"with {paths.display(expected, self.environ)} (unset XDG_STATE_HOME to use it)")
        if any(ch in _UNSAFE_HOME for ch in str(self.home)) or any(ord(ch) < 0x20 for ch in str(self.home)):
            return "the home directory's path contains characters a unit file cannot carry safely"
        return None

    def _own_text(self, path: Path) -> tuple[bool, bytes | None]:
        """``(foreign, bytes)``: our file's bytes, or whether a foreign one blocks it."""

        if not os.path.lexists(path):
            return False, None
        if os.path.islink(path) or not path.is_file():
            return True, None
        try:
            raw = path.read_bytes()
        except OSError:
            return True, None
        return (not systemd_unit.written_here(raw.decode("utf-8", "replace"))), raw

    def _exec_root_of(self, text: str) -> str | None:
        """The stable link an installed unit executes through (from ExecStart)."""

        try:
            sections = systemd_unit.parse(text)
        except systemd_unit.UnitSpecError:
            return None
        for key, value in sections.get("Service", []):
            if key == "ExecStart" and value.startswith("%h/"):
                command = value.split(" ", 1)[0][3:]
                suffix = "/" + systemd_unit.PROXY_ENTRY
                if command.endswith(suffix):
                    return command[: -len(suffix)]
        return None

    def status(self, *, live: bool = True) -> ServiceStatus:
        supported = self.unsupported()
        recorded = self.recorded_unit()
        name = recorded or self.spec()["unit"]
        unit_path = self.unit_path(name)
        foreign, raw = self._own_text(unit_path)
        installed = raw is not None and not foreign
        exec_root = self._exec_root_of(raw.decode("utf-8", "replace")) if installed else None
        stale: bool | None = None
        if installed:
            if exec_root is None or exec_root not in self.spec()["exec_links"].values():
                stale = True
            else:
                # Judged with the certificate locations the unit carries,
                # never this shell's; whether each must be bound comes from
                # the hardening policy, never from the unit's own binds, and
                # one this release would refuse to carry makes it stale.
                homes = _homes(self.home)
                carried = tuple(dataclasses.replace(item, bind=_bind_required(item.path, homes))
                                for item in systemd_unit.trust_of(raw.decode("utf-8", "replace")))
                try:
                    stale = any(_exposes(_unit_path(item.path, self.home), homes) for item in carried) or any(
                        self._own_text(path)[1] != text.encode("utf-8")
                        for path, text in self.rendered(self.plan(name, exec_root, carried)).items())
                except systemd_unit.UnitSpecError:
                    stale = True
        properties = None
        if live and supported is None and (installed or recorded):
            properties = linux_service.unit_properties(name, self.seams.runner)
        link = self.home / exec_root if exec_root is not None else None
        link_target = installs.link_target(link) if link is not None else None
        launcher = installs.install_root(self.environ, self.installation)
        launcher_root = installs.store_path(launcher) if launcher is not None else None
        return ServiceStatus(
            supported=supported, name=name, recorded=recorded is not None, backend=self.gateway.backend,
            unit_path=unit_path, installed=installed, foreign=foreign, stale=stale, properties=properties,
            exec_root=exec_root, link_target=link_target, launcher_root=launcher_root, channel=self.channel())

    # ------------------------------------------------------------ install
    def _preflight(self, verb: str) -> gateway_lifecycle.Outcome | None:
        try:
            reason = self.unsupported()
        except ServiceSetupError as exc:
            return gateway_lifecycle.Outcome("failed", str(exc), exc.remedy)
        if reason is not None:
            return gateway_lifecycle.Outcome("refused", f"the gateway service was not {verb}: {reason}",
                                             "the on-demand gateway keeps working: claude-multi gateway start")
        if not linux_service.manager_available(self.seams.runner):
            return gateway_lifecycle.Outcome(
                "refused", f"the gateway service was not {verb}: no user service manager answers",
                "the on-demand gateway keeps working: claude-multi gateway start")
        return None

    def _packaged_port(self) -> int:
        return endpoint.port_of(self.gateway.catalog_document["gateway"]["base_url"])

    def install(self, name: str | None = None, *,
                max_wait: float = gateway_lifecycle.START_READINESS_TIMEOUT) -> gateway_lifecycle.Outcome:
        refusal = self._preflight("installed")
        if refusal is not None:
            return refusal
        recorded = self.recorded_unit()
        chosen = name or recorded or self.spec()["unit"]
        if not systemd_unit.UNIT_NAME.fullmatch(chosen):
            return gateway_lifecycle.Outcome("refused", f"{chosen!r} is not a plain service name",
                                             "choose a name of a-z, 0-9 and -, starting with a letter")
        if recorded is not None and recorded != chosen:
            return gateway_lifecycle.Outcome("refused", f"the gateway service is installed as {recorded}",
                                             f"remove it first: {UNINSTALL}")
        for path in (self.unit_path(chosen), self.failure_path(chosen)):
            if self._own_text(path)[0]:
                return gateway_lifecycle.Outcome(
                    "refused", f"{paths.display(path, self.environ)} exists and was not written by claude-multi",
                    "move it away, or choose another name with --name, then retry")
        channel = self.channel()
        try:
            selection = installs.plan_selection(channel, self.environ, self.installation)
        except installs.SelectionError as exc:
            return gateway_lifecycle.Outcome("refused", f"the gateway service was not installed: {exc}", exc.remedy)
        exec_root = self.spec()["exec_links"][selection.channel]
        if selection.link != self.home / exec_root:
            return gateway_lifecycle.Outcome("failed", "the service spec and the installation layout disagree",
                                             "reinstall claude-multi")
        trust, problem = unit_trust(self.environ, self.home)
        if problem is not None:
            verb = "refreshed" if recorded is not None else "installed"
            return gateway_lifecycle.Outcome("refused", f"the gateway service was not {verb}: {problem}",
                                             TRUST_REMEDY)
        files = self.rendered(self.plan(chosen, exec_root, trust))
        if recorded is not None:
            outcome = self._refresh(chosen, files, selection, max_wait)
        else:
            outcome = self._install(chosen, files, selection, max_wait)
        if outcome.ok and trust:
            outcome = dataclasses.replace(outcome, notes=(*outcome.notes, *trust_notes(trust, self.environ)))
        return outcome

    def _lock(self) -> state.FileLock | None:
        """The start lock; None while it stays taken (or at once, while its
        holder's inhibition is recorded: see :meth:`_not_locked`)."""

        service.ensure_gateway_workdir(self.gateway.state_root)
        return self.gateway._acquire_start_lock(
            self.gateway.seams.clock() + gateway_lifecycle.START_LOCK_WAIT,
            abort=lambda: self.gateway._foreign_inhibition(take_over_stale_service=True))

    def _not_locked(self, what: str) -> gateway_lifecycle.Outcome:
        """Why :meth:`_lock` came back empty: the inhibition another owner
        holds (never a mere "busy" while one is recorded), else busy."""

        return self.gateway.inhibition_refusal(what, take_over_stale_service=True) or gateway_lifecycle.Outcome(
            "busy", f"a start of the gateway is in progress; {what}", "retry in a moment")

    def _install(self, name: str, files: dict[Path, str], selection: installs.Selection,
                 max_wait: float) -> gateway_lifecycle.Outcome:
        gateway = self.gateway
        lock = self._lock()
        if lock is None:
            return self._not_locked("nothing was installed")
        try:
            inhibited = gateway.inhibition_refusal("nothing was installed", take_over_stale_service=True)
            if inhibited is not None:
                return inhibited
            try:
                # A new install's port is chosen first (a fresh account never
                # observes, or binds, the packaged port); an existing install
                # keeps its own.
                endpoint.ensure_config(self.home, probe=gateway.seams.port_probe)
                gateway.reload_endpoint()
            except endpoint.EndpointError as exc:
                return gateway_lifecycle.Outcome("refused", f"{exc}; nothing was installed", exc.remedy)
            except (OSError, errors.ClaudeMultiError) as exc:
                return gateway_lifecycle.Outcome("failed", f"the gateway port could not be recorded: {exc}",
                                                 "check: claude-multi doctor")
            if gateway.backend == endpoint.SYSTEMD:
                return gateway_lifecycle.Outcome("refused", "the gateway service was installed meanwhile",
                                                 f"run {INSTALL} again to refresh it")
            seen = gateway.observe()
            if seen.state in (gateway_lifecycle.FOREIGN, gateway_lifecycle.UNKNOWN):
                return gateway._refused(seen, "the gateway service was not installed")
            running = seen.state in (gateway_lifecycle.OURS, gateway_lifecycle.STARTING)
            if running:
                hold = gateway.persistence_hold(seen)
                if hold.held:
                    return gateway_lifecycle.Outcome(
                        "held", "the gateway service was not installed: " + gateway_hold.describe(hold),
                        gateway_hold.remedy(hold))
            refused = gateway.begin_handoff("install", name)
            if refused is not None:
                return refused
            undo = _Undo(name=name)
            complete = True
            try:
                if running:
                    stopped = gateway.stop_locked()
                    if not stopped.ok:
                        return gateway_lifecycle.Outcome(
                            stopped.status, "the gateway service was not installed: " + stopped.message,
                            stopped.remedy, stopped.log_tail)
                    undo.restart_on_demand = True
                try:
                    outcome = self._commit_install(name, files, selection, undo, max_wait)
                except (OSError, errors.ClaudeMultiError) as exc:
                    outcome = gateway_lifecycle.Outcome("failed", f"the gateway service could not be installed: {exc}",
                                                        getattr(exc, "remedy", None) or "check: claude-multi doctor")
                except BaseException:
                    _notes, complete = self._restore_on_demand(undo)
                    raise
                if outcome.ok:
                    return outcome
                notes, complete = self._restore_on_demand(undo)
                return self._restored(outcome, notes)
            finally:
                # An incomplete restoration keeps the record: automatic
                # starts stay paused until the user finishes the hand-off.
                gateway.end_handoff(clear=complete)
        finally:
            lock.release()

    def _write_files(self, files: dict[Path, str], undo: _Undo) -> None:
        for path, text in files.items():
            if path not in undo.files:
                undo.files[path] = self._own_text(path)[1]
            undo.written = True
            write_unit_file(path, text)

    def _commit_install(self, name: str, files: dict[Path, str], selection: installs.Selection, undo: _Undo,
                        max_wait: float) -> gateway_lifecycle.Outcome:
        gateway = self.gateway
        runner = self.seams.runner
        for rel in self.spec()["private_dirs"]:
            state.ensure_private_dir(self.home / rel)
        undo.selection = installs.apply_selection(selection, self.environ, runner=runner)
        self._write_files(files, undo)
        if not linux_service.daemon_reload(runner):
            return gateway_lifecycle.Outcome("failed", "the service manager did not reload its unit files",
                                             f"check: {service.hint('status', backend=self._backend(name))}")
        properties = linux_service.unit_properties(name, runner)
        fragment = (properties or {}).get("FragmentPath")
        if not fragment or os.path.realpath(fragment) != os.path.realpath(self.unit_path(name)):
            where = paths.display(self.unit_path(name), self.environ)
            return gateway_lifecycle.Outcome(
                "failed", f"the service manager does not load {name} from {where}"
                + (f" (it uses {fragment})" if fragment else ""),
                "make sure the service manager reads the user unit directory (XDG_CONFIG_HOME), then retry")
        undo.config, _ = endpoint.set_backend(self.home, endpoint.SYSTEMD, name, packaged_port=self._packaged_port(),
                                              probe=gateway.seams.port_probe)
        undo.config_written = True
        gateway.reload_endpoint()
        undo.enabled = True
        if not linux_service.enable_now(name, runner):
            return gateway_lifecycle.Outcome("failed", f"the service manager did not enable and start {name}",
                                             f"why: {service.hint('why', backend=self._backend(name))}")
        deadline = gateway.seams.clock() + max(0.0, max_wait)
        ready = gateway.await_ready(deadline, max_wait)
        if not ready.ok:
            tail = service.journal_tail(name, lines=gateway_lifecycle.LOG_TAIL_READ, runner=runner) or ()
            return gateway_lifecycle.Outcome(ready.status, f"the gateway service {name} did not become ready: "
                                             f"{ready.message}", ready.remedy, tuple(tail))
        notes = [f"unit: {paths.display(self.unit_path(name), self.environ)}"
                 + ("" if self.failure_path(name) in files else f" (no failure notice: {NOTIFIER} is not on PATH)")]
        if undo.selection is not None and undo.selection.note:
            notes.append(undo.selection.note)
        return gateway_lifecycle.Outcome("ready", f"gateway service {name} installed and running — {ready.message}",
                                         notes=tuple(notes))

    def _backend(self, name: str) -> service.Backend:
        return service.Backend(endpoint.SYSTEMD, name)

    def _await_unit_stopped(self, wait: float = gateway_lifecycle.STOP_TIMEOUT) -> bool:
        gateway = self.gateway
        deadline = gateway.seams.clock() + wait
        while True:
            if gateway.observe().state == gateway_lifecycle.STOPPED:
                return True
            if gateway.seams.clock() >= deadline:
                return False
            gateway.seams.sleep(gateway_lifecycle.POLL)

    def _restore_files(self, undo: _Undo) -> list[str]:
        problems = []
        for path, previous in undo.files.items():
            try:
                if previous is None:
                    foreign, raw = self._own_text(path)
                    if raw is not None and not foreign:
                        os.unlink(path)
                else:
                    write_unit_file(path, previous.decode("utf-8"))
            except OSError as exc:
                problems.append(f"{paths.display(path, self.environ)}: {exc.strerror or exc}")
        return problems

    def _stop_veto(self, name: str) -> str | None:
        """Why the unit ``name`` must not be stopped now (None: it may be).

        The same checks as any stop: what runs on the port must be proven
        ours, and a running gateway must be clear of the persistence hold.
        """

        gateway = self.gateway
        seen = gateway.observe()
        if seen.state in (gateway_lifecycle.FOREIGN, gateway_lifecycle.UNKNOWN):
            return f"the gateway on the port is not proven yours ({seen.detail})"
        properties = linux_service.unit_properties(name, self.seams.runner) or {}
        running = seen.state in (gateway_lifecycle.OURS, gateway_lifecycle.STARTING) or (
            properties.get("ActiveState") in ACTIVE_STATES)
        if running:
            hold = gateway.persistence_hold(seen)
            if hold.held:
                return "the persistence hold applies: " + gateway_hold.describe(hold)
        return None

    def _unfinished(self, what: str) -> list[str]:
        return [f"restore stopped: {what}",
                "starts, stops and service changes stay refused until the hand-off is finished; once the "
                f"credentials are safe, finish it: {INSTALL} (or {UNINSTALL})"]

    @staticmethod
    def _not_reloaded() -> list[str]:
        """A restoration whose daemon-reload failed is not complete: the
        manager may still hold the units this hand-off changed."""

        return ["restore: the service manager did not reload the restored unit files, so the hand-off is not "
                "finished",
                "starts, stops and service changes stay refused until it is; once the service manager reloads "
                f"(check: claude-multi doctor), finish it: {INSTALL} (or {UNINSTALL})"]

    def _restore_on_demand(self, undo: _Undo) -> tuple[list[str], bool]:
        """Undo a failed install: ``(notes, complete)``.

        The new unit is stopped only behind :meth:`_stop_veto`; a veto, or an
        exit that cannot be proven, leaves the service installed and selected
        (``complete`` False: the caller keeps the hand-off record). Restored
        unit files count only once the manager's daemon-reload of them
        succeeded; a failed reload leaves the restoration incomplete too.
        """

        gateway = self.gateway
        runner = self.seams.runner
        notes: list[str] = []
        if undo.enabled:
            veto = self._stop_veto(undo.name)
            if veto is not None:
                return self._unfinished(f"the gateway service {undo.name} was not stopped: {veto}; it stays "
                                        "installed"), False
            disabled = linux_service.disable_now(undo.name, runner)
            if not self._await_unit_stopped():
                return self._unfinished(
                    f"the {undo.name} gateway did not stop" + ("" if disabled else
                                                               " (the service manager refused to disable it)")
                    + "; the service stays installed (check: claude-multi gateway status)"), False
        reloaded = True
        if undo.written:
            problems = self._restore_files(undo)
            notes.extend(f"restore: could not restore {problem}" for problem in problems)
            reloaded = linux_service.daemon_reload(runner)
        try:
            if undo.config_written:
                endpoint.restore_config(self.home, undo.config)
            if undo.selection is not None:
                installs.restore_selection(undo.selection)
            gateway.reload_endpoint()
        except (OSError, errors.ClaudeMultiError) as exc:
            notes.append(f"restore: {exc}")
        if undo.restart_on_demand:
            deadline = gateway.seams.clock() + gateway_lifecycle.START_READINESS_TIMEOUT
            started = gateway.ensure_locked(deadline, gateway_lifecycle.START_READINESS_TIMEOUT, explicit=True)
            notes.append("restored: the on-demand gateway runs again" if started.ok
                         else f"restore: the on-demand gateway did not start: {started.message}")
        else:
            notes.append("restored: the on-demand backend is in effect")
        if not reloaded:
            notes.extend(self._not_reloaded())
        return notes, reloaded

    @staticmethod
    def _restored(outcome: gateway_lifecycle.Outcome, notes: list[str]) -> gateway_lifecycle.Outcome:
        return gateway_lifecycle.Outcome(outcome.status, outcome.message, outcome.remedy, outcome.log_tail,
                                         (*outcome.notes, *notes))

    def _ensure_private_dirs(self) -> list[str]:
        """Create the unit's private directories (0700); returns those that were missing."""

        created = []
        for rel in self.spec()["private_dirs"]:
            path = self.home / rel
            if not os.path.lexists(path):
                created.append(rel)
            state.ensure_private_dir(path)
        return created

    def _refresh(self, name: str, files: dict[Path, str], selection: installs.Selection,
                 max_wait: float) -> gateway_lifecycle.Outcome:
        """Install over an installed service: refresh the files, the private
        directories and the selected installation, then classify what the
        running gateway needs. A hand-off that died midway is finished here
        (the service started and proven) before its record is removed.

        The refresh is a hand-off of its own: its inhibition (owner
        ``service``, phase ``refresh``) is recorded before the first change
        and removed only after it completed, or after a failure whose
        restoration is complete; an interruption or an incomplete
        restoration keeps it (every change stays refused) until a later
        install finishes it."""

        gateway = self.gateway
        lock = self._lock()
        if lock is None:
            return self._not_locked("nothing was changed")
        try:
            # What was planned before the lock may be gone (an uninstall
            # finished meanwhile): re-read and re-plan under it.
            try:
                gateway.reload_endpoint()
            except endpoint.EndpointError as exc:
                return gateway_lifecycle.Outcome("refused", f"{exc}; nothing was changed", exc.remedy)
            inhibited = gateway.inhibition_refusal("nothing was changed", take_over_stale_service=True)
            if inhibited is not None:
                return inhibited
            if self.recorded_unit() != name:
                return gateway_lifecycle.Outcome("refused", "the gateway service changed meanwhile; nothing was changed",
                                                 f"check it with: claude-multi gateway service status, then run "
                                                 f"{INSTALL} again")
            try:
                selection = installs.plan_selection(selection.channel, self.environ, self.installation)
            except installs.SelectionError as exc:
                return gateway_lifecycle.Outcome("refused", f"the gateway service was not refreshed: {exc}",
                                                 exc.remedy)
            # A stale record of an interrupted hand-off (the check above let
            # it through) is finished here: an interrupted refresh by
            # refreshing again, any other by starting and proving the service.
            record = gateway_inhibition.read(gateway.state_root)
            interrupted = (record is not None and record.readable and record.owner == gateway_lifecycle.SERVICE_OWNER
                           and not gateway.owns_inhibition(record))
            finish_install = interrupted and record is not None and record.phase != "refresh"
            refused = gateway.begin_handoff("install" if finish_install else "refresh", name)
            if refused is not None:
                return refused
            done = [False]
            try:
                return self._refresh_locked(name, files, selection, max_wait, interrupted=interrupted,
                                            finish_install=finish_install, done=done)
            finally:
                # Only a completed refresh (or a complete restoration of a
                # refresh this run began) removes the record; anything else,
                # an interruption included, keeps it.
                gateway.end_handoff(clear=done[0])
        finally:
            lock.release()

    def _refresh_locked(self, name: str, files: dict[Path, str], selection: installs.Selection, max_wait: float, *,
                        interrupted: bool, finish_install: bool, done: list[bool]) -> gateway_lifecycle.Outcome:
        """:meth:`_refresh` under the start lock and its inhibition; sets
        ``done[0]`` when the record may be removed."""

        gateway = self.gateway
        runner = self.seams.runner
        try:
            created = self._ensure_private_dirs()
        except (OSError, errors.ClaudeMultiError) as exc:
            done[0] = not interrupted
            return gateway_lifecycle.Outcome("failed", f"the gateway service was not refreshed: {exc}",
                                             getattr(exc, "remedy", None) or "check: claude-multi doctor")
        recreated = ([f"recreated the missing service directories ({', '.join(created)}); restart it between "
                      f"turns: {RESTART}"] if created else [])
        stale = {path: text for path, text in files.items() if self._own_text(path)[1] != text.encode("utf-8")}
        orphans = [path for path in (self.failure_path(name),)
                   if path not in files and self._own_text(path)[1] is not None
                   and not self._own_text(path)[0]]
        if not stale and not orphans and not selection.changed and not interrupted:
            applied = installs.apply_selection(selection, self.environ, runner=runner)
            notes = ([applied.note] if applied.note else []) + recreated
            done[0] = True
            return gateway_lifecycle.Outcome("ready", f"gateway service {name} is installed and current",
                                             notes=tuple(notes))
        seen = gateway.observe()
        running = seen.state in (gateway_lifecycle.OURS, gateway_lifecycle.STARTING)
        running_binary = seen.stamp.binary if running and seen.stamp is not None else None
        undo = _Undo(name=name)
        try:
            applied = installs.apply_selection(selection, self.environ, runner=runner)
            undo.selection = applied
            self._write_files(stale, undo)
            for path in orphans:
                undo.files[path] = self._own_text(path)[1]
                undo.written = True
                os.unlink(path)
            # An interrupted run may have written the files without the
            # manager reloading them.
            if (stale or orphans or interrupted) and not linux_service.daemon_reload(runner):
                raise OSError("the service manager did not reload its unit files")
        except OSError as exc:
            return self._refresh_restored(undo, selection, exc, interrupted=interrupted, done=done)
        notes = ([applied.note] if applied.note else []) + recreated
        if finish_install:
            outcome = self._finish_handoff(name, notes, max_wait)
            done[0] = outcome.ok
            return outcome
        if not running:
            notes.append("the gateway is not running; its next start uses the refreshed service")
        elif stale or orphans or interrupted:
            notes.append(f"restart pending: the unit changed; restart it between turns: {RESTART}")
        elif self._same_gateway(running_binary, applied.root):
            if linux_service.reload_unit(name, runner):
                notes.append("reloaded: only the launcher changed")
            else:
                notes.append(f"reload failed; restart it between turns: {RESTART}")
        else:
            notes.append(f"restart pending: the gateway binary changed; restart it between turns: {RESTART}")
        done[0] = True
        return gateway_lifecycle.Outcome("ready", f"gateway service {name} refreshed"
                                         + (" (an interrupted refresh was finished)" if interrupted else ""),
                                         notes=tuple(notes))

    def _refresh_restored(self, undo: _Undo, selection: installs.Selection, exc: OSError, *,
                          interrupted: bool, done: list[bool]) -> gateway_lifecycle.Outcome:
        """Restore a failed refresh to what this run found: the files, the
        manager's loaded units (a daemon-reload that must succeed) and the
        selection. Complete (``done[0]``) only when all of it was restored
        for a refresh this run began (an interrupted one's earlier state is
        unknown); otherwise the inhibition stays and the outcome says how to
        finish."""

        problems = [f"restore: could not restore {problem}" for problem in self._restore_files(undo)]
        if undo.written and not linux_service.daemon_reload(self.seams.runner):
            problems.append("restore: the service manager did not reload the restored unit files")
        installs.restore_selection(selection)
        done[0] = not problems and not interrupted
        message = f"the gateway service was not refreshed: {exc.strerror or exc}"
        if problems:
            return gateway_lifecycle.Outcome(
                "failed", f"{message}; its restoration is not finished, so gateway changes stay paused",
                f"run {INSTALL} again", notes=tuple(problems))
        return gateway_lifecycle.Outcome("failed", message, "check: claude-multi doctor")

    def _finish_handoff(self, name: str, notes: list[str], max_wait: float) -> gateway_lifecycle.Outcome:
        """Finish a hand-off that died with the service recorded: the unit
        enabled and started, its gateway proven ours and ready; only then
        may its inhibition be removed (the caller holds the start lock and
        the hand-off's record, and keeps it unless this is ok)."""

        gateway = self.gateway
        runner = self.seams.runner
        seen = gateway.observe()
        properties = linux_service.unit_properties(name, runner) or {}
        if seen.state in (gateway_lifecycle.FOREIGN, gateway_lifecycle.UNKNOWN):
            return gateway._refused(seen, "the interrupted gateway service hand-off was not finished")
        if seen.state != gateway_lifecycle.STOPPED and properties.get("ActiveState") not in ACTIVE_STATES:
            return gateway_lifecycle.Outcome(
                "refused", "a gateway runs outside the recorded service; the interrupted gateway service "
                "hand-off was not finished", f"check it with: claude-multi gateway status, then run {INSTALL} again")
        if not linux_service.enable_now(name, runner):
            return gateway_lifecycle.Outcome(
                "failed", f"the service manager did not enable and start {name}; the interrupted hand-off is "
                "not finished", f"why: {service.hint('why', backend=self._backend(name))}", notes=tuple(notes))
        ready = gateway.await_ready(gateway.seams.clock() + max(0.0, max_wait), max_wait)
        if not ready.ok:
            tail = service.journal_tail(name, lines=gateway_lifecycle.LOG_TAIL_READ, runner=runner) or ()
            return gateway_lifecycle.Outcome(
                ready.status, f"the gateway service {name} did not become ready: {ready.message}; the "
                "interrupted hand-off is not finished", f"check it with: claude-multi gateway status, then run "
                f"{INSTALL} again", tuple(tail), tuple(notes))
        return gateway_lifecycle.Outcome(
            "ready", f"gateway service {name} installed and running (an interrupted hand-off was finished) — "
            f"{ready.message}", notes=tuple(notes))

    @staticmethod
    def _same_gateway(running: str | None, root: str) -> bool:
        packaged = gateway_lifecycle.packaged_gateway(root)
        if running is None or packaged is None:
            return False
        return os.path.realpath(running) == os.path.realpath(packaged)

    # ------------------------------------------------------------ uninstall
    def uninstall(self, *, max_wait: float = gateway_lifecycle.START_READINESS_TIMEOUT) -> gateway_lifecycle.Outcome:
        name = self.recorded_unit()
        if name is None:
            return self._settle_uninstall()
        refusal = self._preflight("removed")
        if refusal is not None:
            return refusal
        for path in (self.unit_path(name), self.failure_path(name)):
            if self._own_text(path)[0]:
                return gateway_lifecycle.Outcome(
                    "refused", f"{paths.display(path, self.environ)} was not written by claude-multi; nothing was "
                    "removed", "remove that unit yourself, then retry")
        gateway = self.gateway
        runner = self.seams.runner
        lock = self._lock()
        if lock is None:
            return self._not_locked("nothing was removed")
        try:
            try:
                gateway.reload_endpoint()
            except endpoint.EndpointError as exc:
                return gateway_lifecycle.Outcome("refused", f"{exc}; nothing was removed", exc.remedy)
            inhibited = gateway.inhibition_refusal("nothing was removed", take_over_stale_service=True)
            if inhibited is not None:
                return inhibited
            seen = gateway.observe()
            if seen.state in (gateway_lifecycle.FOREIGN, gateway_lifecycle.UNKNOWN):
                return gateway._refused(seen, "the gateway service was not removed")
            properties = linux_service.unit_properties(name, runner) or {}
            running = seen.state in (gateway_lifecycle.OURS, gateway_lifecycle.STARTING) or (
                properties.get("ActiveState") in ACTIVE_STATES)
            if running:
                hold = gateway.persistence_hold(seen)
                if hold.held:
                    return gateway_lifecycle.Outcome(
                        "held", "the gateway service was not removed: " + gateway_hold.describe(hold),
                        gateway_hold.remedy(hold))
            refused = gateway.begin_handoff("uninstall", name)
            if refused is not None:
                return refused
            undo = _Undo(name=name)
            complete = True
            try:
                try:
                    outcome = self._commit_uninstall(name, running, undo, max_wait,
                                                     known=properties.get("LoadState") != "not-found")
                except (OSError, errors.ClaudeMultiError) as exc:
                    outcome = gateway_lifecycle.Outcome("failed", f"the gateway service could not be removed: {exc}",
                                                        getattr(exc, "remedy", None) or "check: claude-multi doctor")
                except BaseException:
                    _notes, complete = self._restore_service(undo, max_wait)
                    raise
                if outcome.ok:
                    return outcome
                notes, complete = self._restore_service(undo, max_wait)
                return self._restored(outcome, notes)
            finally:
                gateway.end_handoff(clear=complete)
        finally:
            lock.release()

    def _settle_uninstall(self) -> gateway_lifecycle.Outcome:
        """No service is recorded. An uninstall interrupted after it recorded
        the on-demand backend left its stale inhibition behind: finish it (the
        unit not running, this product's unit files gone, the manager
        reloaded — every time, since an earlier attempt may have removed the
        files and failed to reload —, the Nix link removed), then remove the
        record; a failed reload keeps it. Another owner's record, a live
        hand-off and an unreadable record are never touched."""

        gateway = self.gateway
        not_installed = gateway_lifecycle.Outcome("stopped", "the gateway service is not installed; nothing was changed")
        record = gateway_inhibition.read(gateway.state_root)
        if record is None or not record.readable or record.owner != gateway_lifecycle.SERVICE_OWNER \
                or record.phase != "uninstall":
            return not_installed
        refusal = self._preflight("removed")
        if refusal is not None:
            return refusal
        runner = self.seams.runner
        lock = self._lock()
        if lock is None:
            return self._not_locked("nothing was removed")
        try:
            try:
                gateway.reload_endpoint()
            except endpoint.EndpointError as exc:
                return gateway_lifecycle.Outcome("refused", f"{exc}; nothing was removed", exc.remedy)
            if self.recorded_unit() is not None:
                return gateway_lifecycle.Outcome("refused", "the gateway service changed meanwhile; nothing was removed",
                                                 f"run {UNINSTALL} again")
            inhibited = gateway.inhibition_refusal("nothing was removed", take_over_stale_service=True)
            if inhibited is not None:
                return inhibited
            record = gateway_inhibition.read(gateway.state_root)
            if record is None:
                return not_installed
            match = _HANDOFF_PURPOSE.fullmatch(record.purpose or "")
            if record.phase != "uninstall" or match is None:
                message, remedy = gateway_inhibition.refusal(record, "nothing was removed", now=gateway.seams.now())
                return gateway_lifecycle.Outcome("inhibited", message, remedy)
            name = match.group(1)
            for path in (self.unit_path(name), self.failure_path(name)):
                if self._own_text(path)[0]:
                    return gateway_lifecycle.Outcome(
                        "refused", f"{paths.display(path, self.environ)} was not written by claude-multi; nothing was "
                        "removed", "remove that unit yourself, then retry")
            seen = gateway.observe()
            if seen.state in (gateway_lifecycle.FOREIGN, gateway_lifecycle.UNKNOWN):
                return gateway._refused(seen, "the interrupted gateway service uninstall was not finished")
            if (linux_service.unit_properties(name, runner) or {}).get("ActiveState") in ACTIVE_STATES:
                return gateway_lifecycle.Outcome(
                    "refused", f"the gateway service {name} still runs; the interrupted uninstall was not finished",
                    "check it with: claude-multi gateway service status")
            refused = gateway.begin_handoff("uninstall", name)
            if refused is not None:
                return refused
            finished = False
            try:
                for path in (self.unit_path(name), self.failure_path(name)):
                    if self._own_text(path)[1] is not None:
                        os.unlink(path)
                # Always: an earlier attempt may have removed the files and
                # failed to reload, so the manager may still hold the unit.
                if not linux_service.daemon_reload(runner):
                    return gateway_lifecycle.Outcome(
                        "failed", "the service manager did not reload its unit files; the interrupted uninstall is "
                        "not finished", f"check: claude-multi doctor, then run {UNINSTALL} again")
                installs.remove_nix_link(self.environ)
                finished = True
                return gateway_lifecycle.Outcome(
                    "stopped", f"gateway service {name} removed (an interrupted uninstall was finished); the gateway "
                    "runs on demand again")
            except OSError as exc:
                return gateway_lifecycle.Outcome(
                    "failed", f"the interrupted uninstall is not finished: {exc.strerror or exc}",
                    "check: claude-multi doctor")
            finally:
                gateway.end_handoff(clear=finished)
        finally:
            lock.release()

    def _commit_uninstall(self, name: str, running: bool, undo: _Undo, max_wait: float, *,
                          known: bool = True) -> gateway_lifecycle.Outcome:
        gateway = self.gateway
        runner = self.seams.runner
        for path in (self.unit_path(name), self.failure_path(name)):
            undo.files[path] = self._own_text(path)[1]
        # A unit the manager does not know (its file is gone) has nothing to
        # stop or disable; only the record remains to clear.
        if known or running or any(raw is not None for raw in undo.files.values()):
            undo.enabled = True  # restoring re-enables what this step disables
            if not linux_service.disable_now(name, runner):
                return gateway_lifecycle.Outcome("failed", f"the service manager did not stop and disable {name}",
                                                 f"why: {service.hint('why', backend=self._backend(name))}")
            if not self._await_unit_stopped():
                return gateway_lifecycle.Outcome("timeout", f"the {name} gateway did not exit",
                                                 "check it with: claude-multi gateway status")
        for path, raw in undo.files.items():
            if raw is not None:
                undo.written = True
                os.unlink(path)
        if not linux_service.daemon_reload(runner):
            return gateway_lifecycle.Outcome("failed", "the service manager did not reload its unit files",
                                             "check: claude-multi doctor")
        undo.config, _ = endpoint.set_backend(self.home, endpoint.ON_DEMAND, packaged_port=self._packaged_port(),
                                              probe=gateway.seams.port_probe)
        undo.config_written = True
        undo.removed_link = installs.remove_nix_link(self.environ)
        gateway.reload_endpoint()
        message = f"gateway service {name} removed; the gateway runs on demand again"
        if not running:
            return gateway_lifecycle.Outcome("stopped", message)
        deadline = gateway.seams.clock() + max(0.0, max_wait)
        started = gateway.ensure_locked(deadline, max_wait, explicit=True)
        if not started.ok:
            return gateway_lifecycle.Outcome(started.status, f"the on-demand gateway did not start: {started.message}",
                                             started.remedy, started.log_tail)
        return gateway_lifecycle.Outcome("ready", f"{message} — {started.message}")

    def _restore_service(self, undo: _Undo, max_wait: float) -> tuple[list[str], bool]:
        """Undo a failed uninstall: the unit files, the endpoint record and the
        service; ``(notes, complete)``. An on-demand gateway started meanwhile
        is stopped only through the guarded stop; when it cannot be, the
        on-demand backend stays and ``complete`` is False. The restored unit
        files count only once the manager's daemon-reload of them succeeded;
        a failed reload leaves ``complete`` False too."""

        gateway = self.gateway
        runner = self.seams.runner
        notes: list[str] = []
        if undo.config_written:
            seen = gateway.observe()
            if seen.state in (gateway_lifecycle.OURS, gateway_lifecycle.STARTING):
                stopped = gateway.stop_locked()
                if not stopped.ok:
                    return self._unfinished(f"the on-demand gateway did not stop: {stopped.message}; the "
                                            "on-demand backend stays in effect"), False
            elif seen.state != gateway_lifecycle.STOPPED:
                return self._unfinished(f"{seen.detail}; the on-demand backend stays in effect"), False
        try:
            for path, raw in undo.files.items():
                if raw is not None and not os.path.lexists(path):
                    write_unit_file(path, raw.decode("utf-8"))
            if undo.removed_link is not None:
                link = installs.nix_link(self.environ)
                state.ensure_private_dir(link.parent)
                installs._replace_link(link, undo.removed_link)
            if undo.config_written:
                endpoint.restore_config(self.home, undo.config)
            gateway.reload_endpoint()
        except (OSError, errors.ClaudeMultiError) as exc:
            notes.append(f"restore: {exc}")
        reloaded = linux_service.daemon_reload(runner)
        if undo.enabled and linux_service.enable_now(undo.name, runner):
            ready = gateway.await_ready(gateway.seams.clock() + max(0.0, max_wait), max_wait)
            notes.append("restored: the gateway service runs again" if ready.ok
                         else f"restore: the gateway service did not become ready: {ready.message}")
        elif undo.enabled:
            notes.append(f"restore: the service manager did not start {undo.name} again")
        if not reloaded:
            notes.extend(self._not_reloaded())
        return notes, reloaded


def write_unit_file(path: Path, text: str) -> None:
    """Atomically replace one unit file (0644; never through a symlink)."""

    # Existing systemd directories may be shared; leave their modes alone.
    # Every directory we create ourselves is owner-only, including parents.
    if not path.parent.is_dir():
        state.ensure_private_dir(path.parent)
    if os.path.islink(path):
        raise OSError(f"{path} is a symlink")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    try:
        state._fsync_directory(path.parent)
    except OSError:
        pass
