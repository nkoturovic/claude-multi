"""Runtime."""

from __future__ import annotations

from claude_multi import __version__ as _pkg_version
from claude_multi import endpoint
from claude_multi import gateway_inhibition

from claude_multi import errors as cli_errors

from typing import Any
from typing import Callable
from pathlib import Path
from typing import Iterable
from typing import TextIO
from claude_multi import catalog
from claude_multi import choices as choices_mod
import claude_multi.cli.types as cli_types
from claude_multi import compiler
import copy
from contextlib import contextmanager
from claude_multi import custom
import dataclasses
from dataclasses import dataclass
from claude_multi import errors
import functools
from claude_multi import launch
from claude_multi import lineup as lineup_mod
from claude_multi import managed
from claude_multi import management
from claude_multi import migrate as migrate_mod
from claude_multi import operator as operator_mod
import os
from claude_multi import paths
from claude_multi import pin
from claude_multi import profile as profile_mod
from claude_multi import proxy as proxy_mod
from claude_multi import quota
from claude_multi import readiness as readiness_mod
import claude_multi.cli.resume_checks as resume_checks
from claude_multi import validate as schema_validate
from claude_multi import scope as scope_mod
from claude_multi import service
import claude_multi.cli.session_facts as session_facts
from claude_multi import sessions
from claude_multi import settings as settings_mod
from claude_multi import state
from claude_multi import strict_json
import sys
import time
from claude_multi import views
from claude_multi.platform import posix_fs


# The legacy implicit composition name: every legacy install had a ``default``
# composition (seeded, never a user file), so the alias hint keeps
# naming it after the seedless store.
IMPLICIT_2X_NAMES = frozenset({"default"})


class CompositionStore:
    """The read-only, seedless legacy user-composition store.

    It reads ``<config root>/compositions/<name>.json`` for the alias
    hint and ``profile migrate``. It has no writer and no seed (the catalog's
    default composition projection is gone), and constructing it touches
    nothing.
    """

    def __init__(self, root: Path | str, *, schema: dict[str, Any]):
        # Construction performs no filesystem write, so a
        # read-only Runtime never creates the config root or compositions/.
        self.root = Path(root)
        self.compositions_dir = self.root / "compositions"
        self.schema = schema

    def _path(self, name: str) -> Path:
        state.check_name(name)
        return self.compositions_dir / f"{name}.json"

    def _directory_present(self) -> bool:
        """Whether compositions/ exists and passes the read-only checks.

        Construction does not run ``ensure_private_dir``,
        so every read validates the directory instead, without creating
        it: a symlinked, foreign-owned or group/other-accessible compositions/
        raises ``StateError``; an absent one is ``False`` (nothing to read).
        """

        if not os.path.lexists(self.compositions_dir):
            return False
        state._check_directory(self.compositions_dir)
        return True

    def has_user(self, name: str) -> bool:
        path = self._path(name)
        try:
            if not self._directory_present():
                return False
        except OSError:
            # An unsafe directory is an empty namespace (as names() treats it).
            return False
        return os.path.lexists(path)

    def contains(self, name: str) -> bool:
        """Whether a name is already visible in the composition namespace."""

        state.check_name(name)
        return name in IMPLICIT_2X_NAMES or self.has_user(name)

    def load(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        try:
            present = self._directory_present()
        except OSError as exc:
            raise cli_errors.CLIError(f"cannot load composition {name!r}: {exc}") from exc
        if present and os.path.lexists(path):
            try:
                document = strict_json.loads(state.read_private(path))
                errors = schema_validate.validate(document, self.schema, "$")
                if errors:
                    raise ValueError("; ".join(errors))
                if document.get("version") != catalog.SUPPORTED_DATA_VERSION:
                    raise ValueError(
                        f"unsupported composition version {document.get('version')!r}"
                    )
                return document
            except (OSError, ValueError) as exc:
                raise cli_errors.CLIError(f"cannot load composition {name!r}: {exc}") from exc
        raise cli_errors.CLIError(f"composition {name!r} does not exist")

    def names(self) -> list[str]:
        names: set[str] = set()
        try:
            if not self._directory_present():
                return []
        except OSError:
            return []
        for path in self.compositions_dir.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            name = path.stem
            try:
                state.check_name(name)
            except OSError:
                continue
            names.add(name)
        return sorted(names)


class Runtime:
    """Bound command APIs with injectable compile/launch/doctor effects."""

    def __init__(
        self,
        *,
        asset_root: Path | str,
        environ: dict[str, str] | None = None,
        cwd: Path | str | None = None,
        compile_callback: Callable[..., compiler.CompileResult] = compiler.compile_lineup_launch,
        launch_callback: Callable[[cli_types.PreparedLaunch], Any] | None = None,
        doctor_callback: Callable[["Runtime"], list[str]] | None = None,
        doctor_binary_callback: Callable[
            [dict[str, Any]], tuple[list[str], list[str]]
        ] | None = None,
        doctor_daemon_callback: Callable[
            [], launch.DaemonStatus
        ] = launch.inspect_shared_daemon,
        doctor_served_callback: Callable[
            ["Runtime", str], tuple[list[str], list[str]]
        ] | None = None,
        served_models_callback: Callable[
            [dict[str, Any], str], tuple[set[str] | None, int | None]
        ] | None = None,
        health_get: Callable[[str, str], int] | None = None,
        management_callback: management.ManagementGetter | None = None,
        management_attention: Callable[[str], None] | None = None,
        listener_owner: Callable | None = None,
        background_liveness: Callable | None = None,
        managed_root: Path = managed.MANAGED_ROOT,
        proc_root: Path | str = "/proc",
        allow_state_writes: bool = True,
        refresh_shims: bool = True,
        initialize_session_store: bool = True,
        execve: Callable[[str, list[str], dict[str, str]], Any] | None = None,
        qualify_transport: Callable[[str, str, str], operator_mod.SmokeOutcome] | None = None,
        lan_probe: Callable[[str], Any] | None = None,
        listing_transport: Callable[..., Any] | None = None,
        qualify_http: Callable[..., Any] | None = None,
        exact_client_runner: Callable[..., Any] | None = None,
        gateway_ensure: Callable[["Runtime"], None] | None = None,
        gateway_seams: Any = None,
        gateway_service_seams: Any = None,
        pin_report: bool | None = None,
        release_version: str | None = None,
    ):
        self.asset_root = Path(asset_root)
        # The running release (claude_multi.__version__, read from the
        # packaged resources). Tests that replay records of a frozen fixture
        # pass that fixture's launcher version explicitly.
        self.release_version = _pkg_version if release_version is None else str(release_version)
        self.allow_state_writes = bool(allow_state_writes)
        self._report_cache: dict | None = None
        self._read_only_store = not initialize_session_store
        # None = posix_fs.exec_replace, i.e. os.execve (tests capture argv/env here).
        self.execve = execve
        self.environ = dict(os.environ if environ is None else environ)
        self.cwd = str(Path.cwd() if cwd is None else Path(cwd).resolve()
        )
        config = sessions.config_root(self.environ)
        state_path = sessions.state_root(self.environ)
        self.broken_override_error: str | None = None
        # One-shot notice: a legacy contract override was removed at init.
        self.contract_notice: str | None = None
        self._contract_notice_shown = False
        override_path = config / "native-contract.json"
        try:
            self.catalog = catalog.load_catalog(
                self.asset_root, contract_override=override_path
            )
        except catalog.CatalogError as exc:
            if not os.path.lexists(override_path):
                raise
            # An override is never applied, and an unloadable one must not
            # brick every command either: degrade to the packaged contract
            # and report (doctor names the file to remove).
            packaged_catalog = catalog.load_catalog(self.asset_root)
            removal = None
            if allow_state_writes:
                # A legacy (effort_vocabulary) override can never
                # load again; remove it unconditionally, with a notice. The
                # helper never raises and skips anything unsafe to touch
                # (held lock, symlink, unsafe mode), which then degrades.
                removal = catalog.remove_legacy_contract_override(
                    override_path,
                    packaged=packaged_catalog.docs["native-contract"],
                )
            error = str(exc)
            if removal is not None:
                self.contract_notice = (
                    f"native contract override (claude "
                    f"{removal.pinned_version or 'unknown'}) used an older "
                    "effort vocabulary and was removed; the packaged contract "
                    f"(claude {removal.packaged_version}) is in effect"
                )
                try:
                    self.catalog = catalog.load_catalog(
                        self.asset_root, contract_override=override_path
                    )
                except catalog.CatalogError as reload_exc:
                    # A different override appeared meanwhile and is itself
                    # invalid: the ordinary degrade path reports it.
                    error = str(reload_exc)
                else:
                    error = None
            if error is not None:
                self.broken_override_error = error
                self.catalog = packaged_catalog
        # The user's gateway endpoint (endpoint.json) replaces the catalog's
        # base URL in this process's view, so compiled scopes, readiness and
        # the operator port rules name the configured port.
        self.endpoint_error: endpoint.EndpointError | None = None
        self.catalog = self._with_endpoint(self.catalog)
        # Single hook-command authority: the stable shim under the state root.
        # Compiled scopes embed the shim's constant path (never a package or
        # store path), so scope bytes survive package rebuilds; the shim is
        # refreshed here so hooks always reach the newest resolved launcher.
        self.resolved_hook_command = scope_mod.resolve_hook_command(self.environ)
        try:
            sessions.check_state_marker(state_path)
            marker_ok = True
        except sessions.StateMarkerError:
            marker_ok = False
        # refresh_shims=False (migrate --dry-run, --print-launch)
        # takes the same paths-only branch as a newer marker, for the legacy
        # shim, shim-3 and the token shim: a read-only command never repoints
        # the shims running sessions use. allow_state_writes still gates only
        # the legacy override removal above.
        # The shims stay what another party left (shims_withheld says why):
        # - "channel": the state root belongs to another installation's
        #   channel (or its marker is unreadable) — every entry point that
        #   builds a Runtime, hooks included, is held to the marker here,
        #   at the write itself;
        # - "inhibited": a transaction owner holds the state root's
        #   inhibition (an installer, an updater, a machine move, a service
        #   hand-off) and this run is not its own (no token). The check and
        #   the three writes happen inside the writer fence, so an owner's
        #   begin never lands between them;
        # - "authority": this home's continuity.json cannot be read, so the
        #   root that owns its gateway (and could be inhibited) is unknown.
        self.shims_withheld: str | None = None
        self._shims_refreshed = False
        if marker_ok and refresh_shims:
            self._shims_refreshed = self._refresh_shims(state_path)
        if not self._shims_refreshed:
            # Read-only commands must not repoint the shims the launcher keeps
            # for still-running sessions. Derive their paths without writing.
            self.hook_command = str(scope_mod.hook_shim_path(state_path))
            self.hook3_command = str(scope_mod.hook_shim_v3_path(state_path))
            self.token_helper_command = scope_mod.token_helper_setting(scope_mod.gateway_token_shim_path(state_path))
        # The legacy composition store is lazy and read-only for lineup
        # paths (only the screens and the alias hint touch it), so no
        # command creates its directory as a side effect.
        self._config_root = config
        self._compositions: CompositionStore | None = None
        self._session_store_root = state_path
        self._session_store: sessions.SessionStore | None = None
        # SessionStore creates its directories. Keep existing commands eager,
        # but quota never needs the store, even with an entirely empty HOME.
        if initialize_session_store:
            self._session_store = self.session_store
        self.compile_callback = compile_callback
        self.launch_callback = launch_callback
        self.doctor_callback = doctor_callback
        self.doctor_binary_callback = doctor_binary_callback or functools.partial(
            launch.doctor_binary_report, environ=self.environ,
            retained_root=paths.retained_root(self.environ),
        )
        # The pin facts (the user's own claude by real path, the pin's age,
        # the owned copy's state on the card) belong to the binary report: an
        # injected binary seam turns them off unless asked for explicitly.
        self.pin_report = (doctor_binary_callback is None) if pin_report is None else bool(pin_report)
        self.doctor_daemon_callback = doctor_daemon_callback
        # None = built-in served-selector cross-check; tests inject a
        # stub here (or a doctor_callback, which skips the whole branch).
        self.doctor_served_callback = doctor_served_callback
        # Loopback gateway seams. None = the live loopback calls
        # (launch.served_models / launch._default_health_get); tests inject
        # fixture results so no test ever reaches the running gateway.
        self.served_models_callback = served_models_callback
        self.health_get = health_get
        self.management_callback = management_callback
        self.management_attention = management_attention
        self._pool_cache = None
        self.pool_clock = time.monotonic
        # None = the gateway's own ownership proof (exec stamp, lock, PID and
        # listener on the configured port; no manager hint needed).
        self.listener_owner = listener_owner if listener_owner is not None else self._observed_listener
        self.background_liveness = background_liveness or self._background_liveness
        self._state_filesystem: tuple[str | None] | None = None
        self.managed_root = Path(managed_root)
        self.proc_root = Path(proc_root)
        self.gateway_attention: list[str] = []
        # Why a writing Runtime could not be prepared, when doctor reports
        # from a read-only one instead (the entry point sets it).
        self.init_problem: str | None = None
        # Prepared plan -> operator definition digests it bound.
        self._operator_prepared: dict[int, tuple[Any, dict[str, str]]] = {}
        # The smoke transport seam, (gateway base URL, gateway token,
        # served alias) -> SmokeOutcome. None = one bounded loopback request
        # through the gateway (default_qualify_transport); tests inject a fake.
        self.qualify_transport = qualify_transport
        # The LAN reachability seam (None = one short-timeout
        # probe; with the gateway seams injected, never a real dial).
        self.lan_probe = lan_probe
        # The warning of the default-profile choice, shown by the next fresh
        # prepare.
        self.selection_notices: list[str] = []
        # The default-profile decision of the last fresh launch target
        # (a ``setup.defaults.DefaultChoice``) and, when the profile it chose
        # cannot be loaded, the load error the card shows.
        self.selection_default: Any = None
        self.selection_load_error: profile_mod.ProfileLoadError | None = None
        # The provider listing transport seam, (url, headers, *,
        # deadline, max_bytes) -> bytes. None = proxy.bounded_get (20 s
        # wall-clock, 4 MiB, no redirects, cookies or retries).
        self.listing_transport = listing_transport
        # The qualification battery transport, (base_url, token,
        # PlannedCall, body) -> qualify.HttpResult. None = one bounded
        # loopback POST (qualify.loopback_post; http.client, no env proxy).
        self.qualify_http = qualify_http
        # The offline exact-client check, (ExactClientInput)
        # -> qualify.ExactClientOutcome. None = probe.run_exact_client_check
        # (pinned client in bwrap --unshare-net against a local fake).
        self.exact_client_runner = exact_client_runner
        # The on-demand gateway seams: ``gateway_ensure`` replaces the whole
        # ensure step; ``gateway_seams`` (a gateway_lifecycle.Seams) replaces
        # its live observations and effects.
        self.gateway_ensure = gateway_ensure
        self.gateway_seams = gateway_seams
        # The supervised service's manager and tool lookups (a
        # gateway_service.ServiceSeams); None = the live ones.
        self.gateway_service_seams = gateway_service_seams
        # The bounded wait launch, /cm, the TUI lineup apply and
        # the served operator verbs use for the gateway barrier (test seam).
        self.served_barrier_timeout: float | None = state.SERVED_BARRIER_TIMEOUT
        # The barrier token of the operator phase in progress (render_gateway
        # asserts it); None outside a served-change phase.
        self.held_barrier: state.BarrierToken | None = None

    def _with_endpoint(self, loaded: catalog.Catalog) -> catalog.Catalog:
        """``loaded`` with the configured endpoint, or unchanged (and the error
        recorded: launches and gateway verbs refuse, doctor reports it)."""

        try:
            result = endpoint.apply_to_catalog(loaded, self.home)
        except endpoint.EndpointError as exc:
            self.endpoint_error = exc
            return loaded
        self.endpoint_error = None
        return result

    def state_filesystem(self) -> str | None:
        """The filesystem type under the state root (None when unknown; cached)."""

        if self._state_filesystem is None:
            from claude_multi.platform import mounts

            self._state_filesystem = (mounts.filesystem_type(self._session_store_root, platform=sys.platform),)
        return self._state_filesystem[0]

    def _background_liveness(self) -> sessions.BackgroundLiveness:
        """Daemon liveness; unknown when the state root is on a network
        filesystem (sessions running on another host are invisible here)."""

        from claude_multi.platform import mounts

        verdict = sessions.background_liveness()
        fstype = self.state_filesystem()
        if fstype in mounts.NETWORK_TYPES:
            return sessions.BackgroundLiveness(
                False, verdict.prefixes,
                f"the state root is on a network filesystem ({fstype}); sessions on other hosts are invisible")
        return verdict

    def _observed_listener(self, base_url: str) -> service.OwnerVerdict:
        """The listener verdict of this runtime's gateway proof, for its own port."""

        try:
            gateway = self.gateway()
            if gateway.base_url == base_url:
                return gateway.observe().listener
        except (errors.ClaudeMultiError, OSError):
            pass
        return service.listener_owner(base_url)

    def gateway(self, **overrides: Any) -> Any:
        """This runtime's gateway (endpoint, backend, lifecycle verbs).

        Raises ``endpoint.EndpointError`` for an unreadable endpoint document.
        """

        from claude_multi import gateway_lifecycle

        if self.endpoint_error is not None:
            raise self.endpoint_error
        params: dict[str, Any] = {
            "home": self.home, "state_root": self._session_store_root, "environ": self.environ,
            "gateway_document": self.catalog.docs["gateway"],
            "providers": lambda: {*self.pool_providers(), *self.ordinary_docs["providers"]["providers"]},
            "seams": self.gateway_seams,
        }
        params.update(overrides)
        return gateway_lifecycle.Gateway(**params)

    def gateway_service(self) -> Any:
        """This runtime's supervised gateway service (install, uninstall, status)."""

        from claude_multi import gateway_service

        return gateway_service.GatewayService(self.gateway(), seams=self.gateway_service_seams)

    def _live_gateway(self) -> bool:
        """Whether this runtime's launches start the gateway themselves.

        Runtimes with injected loopback seams (``health_get`` or
        ``served_models_callback``) describe a fixture gateway that is already
        running, and an injected ``gateway_ensure`` replaces the whole step.
        """

        if self.gateway_ensure is not None:
            return False
        return self.gateway_seams is not None or (self.health_get is None and self.served_models_callback is None)

    @property
    def inhibited_shims(self) -> bool:
        """The shims were left alone for an inhibition this run does not own."""

        return self.shims_withheld == "inhibited"

    def _refresh_shims(self, state_path: Path) -> bool:
        """Refresh the hook, protocol-3 hook and token-helper shims (see
        ``shims_withheld``); False when they were left alone."""

        from claude_multi import installs

        if not installs.check(state_path, self.environ, record=False).ok:
            self.shims_withheld = "channel"
            return False
        try:
            with gateway_inhibition.fenced([state_path], token=self.environ.get(gateway_inhibition.TOKEN_ENV) or None,
                                           what="the shims were not refreshed"):
                # The token shim hands out this home's gateway credential:
                # with the managed root unknown it is left as it is.
                gateway_inhibition.managed_root(self.home, what="the shims were not refreshed")
                hook_command = str(scope_mod.ensure_hook_shim(state_path, self.resolved_hook_command))
                # The protocol-3 shim that v2 scopes
                # embed, refreshed next to the legacy one from the same command.
                hook3_command = str(scope_mod.ensure_hook_shim_v3(state_path, self.resolved_hook_command))
                token_helper_command = scope_mod.ensure_token_helper_command(
                    state_path, self.environ, self.resolved_hook_command)
        except gateway_inhibition.AuthorityUnreadable:
            self.shims_withheld = "authority"
            return False
        except (gateway_inhibition.Inhibited, gateway_inhibition.InhibitionError):
            self.shims_withheld = "inhibited"
            return False
        self.hook_command, self.hook3_command, self.token_helper_command = (
            hook_command, hook3_command, token_helper_command)
        return True

    def provision_endpoint(self) -> bool:
        """A new install records its gateway port before its first launch is
        compiled: the first free port of the new-install range, so the scope,
        readiness and the start agree. A recorded endpoint, an existing
        install (it keeps its port), a read-only run and a fixture gateway
        change nothing. True when the endpoint was recorded now (this
        runtime's catalog view then names it)."""

        if self.endpoint_error is not None or not (self.allow_state_writes and self._shims_refreshed):
            return False
        if not self._live_gateway():
            return False
        if endpoint.read_config(self.home) is not None or endpoint.existing_install(self.home):
            return False
        probe = getattr(self.gateway_seams, "port_probe", None) or endpoint.port_free
        root = self._session_store_root
        token = self.environ.get(gateway_inhibition.TOKEN_ENV) or None
        try:
            # The launcher write guard at the write itself: this Runtime may
            # have been built long before (an open TUI), so a migration in
            # progress and state of a newer release are checked again here.
            # The inhibition is checked inside the writer fence, so the port
            # is never recorded once a transaction owner began.
            with sessions.launcher_write_guard(root):
                sessions.check_state_marker(root)
                what = "no gateway port was recorded"
                with gateway_inhibition.fenced(gateway_inhibition.home_roots(self.home, root, what=what),
                                               token=token, what=what, home=self.home):
                    if endpoint.read_config(self.home) is None:
                        endpoint.ensure_config(self.home, probe=probe)
        except gateway_inhibition.Inhibited:
            return False  # the start that follows reports the inhibition; nothing is recorded under it
        except (gateway_inhibition.AuthorityUnreadable, gateway_inhibition.AuthorityChanged) as exc:
            raise launch.LaunchError(f"local gateway: {exc}", remedy=exc.remedy) from exc
        except sessions.StateMarkerError:
            raise
        except endpoint.EndpointError as exc:
            raise launch.LaunchError(f"local gateway: {exc}", remedy=exc.remedy) from exc
        except (OSError, errors.ClaudeMultiError) as exc:
            raise launch.LaunchError(f"local gateway: the gateway port could not be recorded: {exc}",
                                     remedy="check: claude-multi doctor") from exc
        self.catalog = self._with_endpoint(self.catalog)
        return True

    def ensure_gateway(self) -> None:
        """Start the gateway when a launch or resume needs it (raises LaunchError).

        Runtimes with injected loopback seams (``health_get`` or
        ``served_models_callback``) describe a fixture gateway that is already
        running: nothing is started for them unless ``gateway_ensure`` or
        ``gateway_seams`` is injected too. A new install's first start records
        its port first (:meth:`provision_endpoint`), and this runtime's catalog
        view follows a port recorded meanwhile.
        """

        if self.endpoint_error is not None:
            raise launch.LaunchError(str(self.endpoint_error), remedy=self.endpoint_error.remedy)
        if self.gateway_ensure is not None:
            self.gateway_ensure(self)
            return
        if not self._live_gateway():
            return
        self.provision_endpoint()
        try:
            outcome = self.gateway().ensure()
        except errors.ClaudeMultiError as exc:
            raise launch.LaunchError(str(exc), remedy=exc.remedy) from exc
        finally:
            self.catalog = self._with_endpoint(self.catalog)
        if not outcome.ok:
            raise launch.LaunchError("local gateway: " + outcome.message.removeprefix("BLOCKED: "),
                                     remedy=outcome.remedy or "read its log: claude-multi gateway logs")

    @property
    def session_store(self) -> sessions.SessionStore:
        if self._session_store is None:
            self._session_store = sessions.SessionStore(
                self._session_store_root,
                strict_json.load(self.asset_root / "schemas" / "session.schema.json"),
                read_only=self._read_only_store,
            )
        return self._session_store

    @session_store.setter
    def session_store(self, store: sessions.SessionStore) -> None:
        self._session_store = store

    @property
    def home(self) -> Path:
        """The runtime HOME (never the process HOME when environ carries one)."""

        return Path(self.environ.get("HOME") or Path.home())

    def gateway_token(self) -> str:
        """Loopback gateway token from the runtime HOME (raises LaunchError)."""

        return launch.read_gateway_token(self.catalog.docs["gateway"], home=self.home)

    def served_models(self, token: str) -> tuple[set[str] | None, int | None]:
        """(served selector ids, HTTP status) via the injectable seam."""

        rows, status = self.served_snapshot(token)
        return (None if rows is None else {r.id for r in rows}), status

    @contextmanager
    def report_snapshot(self):
        """One ephemeral report; nested consumers share observations, not effects."""
        previous = self._report_cache
        if previous is None:
            self._report_cache = {}
        try:
            yield
        finally:
            self._report_cache = previous

    def report_value(self, key, collect):
        if self._report_cache is None:
            return collect()
        if key not in self._report_cache:
            self._report_cache[key] = collect()
        return self._report_cache[key]

    def served_snapshot(self, token: str):
        return self.report_value("served", lambda: self._read_served_snapshot(token))

    def _read_served_snapshot(self, token: str) -> tuple[tuple[launch.ServedModel, ...] | None, int | None]:
        """The one ``/v1/models`` read of a refresh, with metadata.

        Same seam as :meth:`served_models`: an injected callback may return
        an id set (metadata absent) or ``launch.ServedModel`` entries."""

        callback = self.served_models_callback
        self.gateway_attention = []
        try:
            if callback is not None:
                launch.gateway_authorization(
                    endpoint.gateway_endpoint(self.catalog.docs["gateway"]).base_url, token,
                    owner_check=self.listener_owner, attention=self.gateway_attention.append,
                    require_proof=True,
                )
                raw, status = callback(self.catalog.docs["gateway"], token)
                return (None if raw is None else launch.served_snapshot_from(raw)), status
            return launch.served_snapshot(
                self.catalog.docs["gateway"], token,
                owner_check=self.listener_owner, attention=self.gateway_attention.append,
            )
        except launch.GatewayOwnerError as exc:
            self.gateway_attention.append(str(exc).removeprefix("BLOCK: ") + f" — {exc.remedy}")
            raise

    def pool_status(self) -> quota.PoolStatus:
        """Passive one-read cache, including failures; injected health never leaks live."""
        now = self.pool_clock()
        if self._pool_cache is not None:
            read_at, result = self._pool_cache
            if now - read_at < quota.CACHE_TTL_SECONDS:
                return result
        if self.management_callback is None and (
            self.health_get is not None or self.served_models_callback is not None
        ):
            result = quota.PoolStatus("seam")
        else:
            result = management.pool_status(
                self.home, self.catalog.docs["gateway"], get=self.management_callback,
                owner_check=self.listener_owner, environ=self.environ,
                attention=(self.management_attention if self.management_attention is not None
                           else self.gateway_attention.append),
            )
        self._pool_cache = (now, result)
        return result

    def pool_providers(self) -> dict[str, str]:
        return {provider["transport"]["pool"]: name
                for name, provider in self.ordinary_docs["providers"]["providers"].items()
                if provider["transport"]["kind"] == "oauth-pool"}

    def _launch_readiness(self, gateway, **kwargs) -> str:
        self.ensure_gateway()
        if endpoint.gateway_endpoint(self.catalog.docs["gateway"]).base_url != endpoint.gateway_endpoint(gateway).base_url:
            # The launch was compiled for another endpoint (recorded meanwhile).
            raise launch.LaunchError("the gateway endpoint changed after this launch was prepared; nothing was launched",
                                     remedy="run the command again")
        token = launch.check_readiness(gateway, **kwargs)
        launch.check_listener(endpoint.gateway_endpoint(gateway).base_url, owner_check=self.listener_owner)
        return token

    def proxy_env(self, cwd: str, live: Path) -> dict[str, str]:
        layers = managed.env_layers({**self.environ, "HOME": str(self.home)}, cwd, self.managed_root)
        return compiler.loopback_no_proxy(
            self.environ, *(env for _, env in layers),
            previous=managed.settings_env(live / "settings.json"),
        )

    def permission_default_mode(self, cwd: str) -> str | None:
        """The launch/resume permission-mode decision:
        ``scope.PERMISSION_DEFAULT_MODE`` unless a layer the managed client
        reads (``$HOME/.claude``, never ``CLAUDE_CONFIG_DIR``; the launch
        cwd's project/local files; managed policy) configures one. When a
        user layer has settings keys the pinned client does not know, the
        pin may skip that file (:meth:`settings_skew`): then only a managed
        policy mode counts, and otherwise the default mode is compiled."""

        environ = {**self.environ, "HOME": str(self.home)}
        known = pin.settings_keys(self.catalog.docs["native-contract"])
        if managed.permission_mode_configured(environ, cwd, self.managed_root, known_keys=known):
            return None
        return scope_mod.PERMISSION_DEFAULT_MODE

    def settings_skew(self, cwd: str, passthrough: Iterable[str] = ()) -> list[str]:
        """Attention lines for user settings files with keys the pin does not know.

        A newer Claude Code writes settings keys the pinned one lacks; the pin
        may then skip the file, dropping its deny rules and its permission
        mode. The launch compiles the default permission mode in that case
        (:meth:`permission_default_mode`); the line says which mode applies
        when a managed policy mode or a ``passthrough`` permission flag
        outranks it. Key names only, never values."""

        contract = self.catalog.docs["native-contract"]
        known = pin.settings_keys(contract)
        environ = {**self.environ, "HOME": str(self.home)}
        skewed = managed.skew_layers(environ, cwd, known)
        if not skewed:
            return []
        if managed.policy_configures_default_mode(self.managed_root):
            mode = "this session uses the managed policy's permission mode"
        elif any(arg in ("--permission-mode", "--dangerously-skip-permissions")
                 or arg.startswith("--permission-mode=") for arg in passthrough):
            mode = "this session uses the permission mode given on the command line"
        else:
            mode = "this session starts in the default permission mode"
        lines = []
        for path, keys in skewed:
            shown = ", ".join(keys[:8]) + (f" and {len(keys) - 8} more" if len(keys) > 8 else "")
            lines.append(
                f"{paths.display(path, environ)} has settings Claude Code {pin.version(contract)} "
                f"does not know ({shown}); it may skip that file, so {mode} and that file's "
                "deny rules may not apply"
            )
        return lines

    def managed_findings(self, lineup: profile_mod.ResolvedLineup | None = None) -> list[managed.ManagedLock]:
        """Managed-policy settings that affect a managed session (names only),
        judged against the pin, the gateway endpoint and ``lineup``'s bound
        models."""

        contract = self.catalog.docs["native-contract"]
        try:
            version: str | None = pin.version(contract)
        except (KeyError, TypeError, ValueError):
            version = None
        selectors: tuple[str, ...] = ()
        if lineup is not None:
            selectors = (lineup.lead.binding.selector,
                         *(agent.binding.selector for agent in lineup.agents.values()))
        return managed.policy_findings(
            self.managed_root, pin_version=version,
            base_url=endpoint.gateway_endpoint(self.catalog.docs["gateway"]).base_url,
            selectors=selectors,
        )

    def managed_launch_blocks(self, lineup: profile_mod.ResolvedLineup | None) -> list[str]:
        """The managed-policy settings that refuse a launch of ``lineup``."""

        return [f"{managed.finding_text(lock)} — {managed.POLICY_REMEDY}"
                for lock in self.managed_findings(lineup) if lock.blocks]

    def check_readiness(self) -> str:
        """Loopback readiness (token from the runtime HOME + /healthz)."""

        if self.endpoint_error is not None:
            raise launch.LaunchError(str(self.endpoint_error), remedy=self.endpoint_error.remedy)
        return launch.check_readiness(
            self.catalog.docs["gateway"], home=self.home, health_get=self.health_get
        )

    @property
    def launcher_version(self) -> str:
        """The running release: the packaged resources' version, never the
        selected resources'. Records, exports and evidence name the launcher
        that wrote them; a resource override changes the catalog inputs
        (``catalog_version``, ``catalog.bundle_sha256``), not that identity."""

        return self.release_version

    def custom_conflicts(self) -> list[str]:
        """Custom-registry ids shadowing the catalog (dropped by the merge)."""

        return custom.merge_conflicts(
            self.catalog.docs, custom.load_registry(self.environ)
        )

    def custom_header_violations(self) -> list[tuple[str, str]]:
        """Custom providers whose auth is unsupported (dropped with their models)."""

        return custom.header_rule_violations(custom.load_registry(self.environ))

    def operator_snapshot(self) -> operator_mod.OperatorSnapshot:
        """The operator layer (providers.d + ledger + legacy registry), read
        through the proxy's own seam so doctor, render and launch agree."""

        return proxy_mod.operator_snapshot(
            {**self.environ, "HOME": str(self.home)}, self.catalog.docs,
            custom.load_registry(self.environ), asset_root=self.asset_root,
        )

    def operator_render_plan(self) -> operator_mod.RenderPlan:
        """What the gateway render serves from the operator layer (start
        policy: a corrupt ledger drops every T2 contribution)."""

        snapshot = self.operator_snapshot()
        legacy = custom.load_registry(self.environ)
        plan = operator_mod.render_plan(self.catalog.docs, snapshot.layer, snapshot.ledger, legacy=legacy)
        if snapshot.ledger_error is not None and plan.layer.lines:
            plan = operator_mod.render_plan(
                self.catalog.docs, operator_mod.without_contributions(snapshot.layer), None, legacy=legacy,
            )
        return plan

    def gateway_environ(self) -> dict[str, str]:
        """The environment the proxy render seams use (this Runtime's HOME
        and asset root), as doctor's alias prune passes it."""

        return {**self.environ, "HOME": str(self.home), "CLAUDE_MULTI_ASSETS": str(self.asset_root)}

    def render_gateway(self, *, policy: str = "explicit", barrier: Any = None) -> tuple[Path, Any, Any]:
        """Render the gateway config under the api-key leaf (write
        verbs; ``proxy.render_runtime_config`` with the session state root).
        A served mutation passes its held barrier token (asserted)."""

        # A home whose gateway was never set up gets no config (it would make
        # the home an install of the packaged port) and no reload request;
        # while a transaction owner holds the gateway's inhibition, only that
        # owner publishes a configuration. The check and the write happen
        # inside the writer fence of every root that may own this home's
        # gateway (this one and the managed root), so a begin never lands
        # between them.
        endpoint.require_set_up(self.home, "the gateway configuration was not written")
        what = "the gateway configuration was not written"
        with gateway_inhibition.fenced(gateway_inhibition.home_roots(self.home, self._session_store_root, what=what),
                                       token=self.environ.get(gateway_inhibition.TOKEN_ENV) or None,
                                       what=what, home=self.home):
            return proxy_mod.render_runtime_config(
                self.home, environ=self.gateway_environ(), state_root=self.session_store.root, policy=policy,
                barrier=barrier if barrier is not None else self.held_barrier,
            )

    def root_authority_refusal(self) -> str | None:
        """The exact refusal when this runtime's state root is
        not the gateway's managed root, or the authority is unreadable (None:
        same root or no authority yet)."""

        return proxy_mod.root_authority_refusal(self.home, self.session_store.root)

    def verify_reload(self, sentinel: str) -> Any:
        """Wait (at most 2 s, no lock held) for the render sentinel through
        the loopback seam; a ``proxy.ReloadResult``."""

        endpoint.require_set_up(self.home, "no reload was requested")
        gateway = self.catalog.docs["gateway"]

        def models_get(_base_url: str, token: str) -> tuple[int, set[str]]:
            ids, status = self.served_models(token)
            return (status or 0), set(ids or ())

        return proxy_mod.await_sentinel(
            gateway, proxy_mod.gateway_api_keys(self.home)[-1], sentinel,
            models_get=None if self.served_models_callback is None else models_get,
            owner_check=self.listener_owner,
        )

    def smoke(self, alias: str) -> operator_mod.SmokeOutcome:
        """ONE bounded smoke request to ``alias`` through the loopback gateway
        (the caller has the consent). Never retried."""

        token = self.gateway_token()
        base_url = endpoint.gateway_endpoint(self.catalog.docs["gateway"]).base_url
        transport = self.qualify_transport or functools.partial(
            default_qualify_transport, owner_check=self.listener_owner,
        )
        return transport(base_url, token, alias)

    def qualify_post(self, call: Any, body: bytes) -> Any:
        """ONE bounded battery request through the loopback gateway (the
        caller holds the consent). Never retried."""

        from claude_multi import qualify as qualify_mod

        token = self.gateway_token()
        base_url = endpoint.gateway_endpoint(self.catalog.docs["gateway"]).base_url
        if self.qualify_http is not None:
            return self.qualify_http(base_url, token, call, body)
        return qualify_mod.loopback_post(base_url, token, body, deadline=call.deadline,
                                         owner_check=self.listener_owner)

    def run_exact_client(self, request: Any) -> Any:
        """The offline exact-client check (zero provider requests)."""

        from claude_multi import probe as probe_mod

        if self.exact_client_runner is not None:
            return self.exact_client_runner(request)
        return probe_mod.run_exact_client_check(request, native_contract=self.catalog.docs["native-contract"],
                                                environ=self.environ)

    def contract_identity(self) -> Any:
        """The client and gateway contract digests evidence binds to."""

        from claude_multi import qualify as qualify_mod

        return qualify_mod.contract_identity(self.catalog.docs["native-contract"], self.asset_root)

    def agent_gate(self, snapshot: operator_mod.OperatorSnapshot | None = None,
                   docs: dict[str, Any] | None = None) -> profile_mod.AgentGate:
        """Current diagnostic facts for every valid operator line.

        Capability and role declarations are recommendations: an explicit
        binding may choose any routable line, with its evidence shown as-is.
        """

        snapshot = snapshot if snapshot is not None else self.operator_snapshot()
        wanting = sorted(snapshot.layer.lines)
        if not wanting:
            return profile_mod.AgentGate(profile_mod.AGENT_MODE_CURRENT, {})
        docs = docs if docs is not None else operator_mod.merge_docs(
            self.catalog.docs, snapshot.layer, legacy=custom.load_registry(self.environ))
        try:
            document = self.settings_store.load()
            eff = settings_mod.effective(
                document, provider_ids=docs["providers"]["providers"],
                line_keys=(docs["models-v2"] if "models-v2" in docs else docs["models"])["models"],
            )
        except settings_mod.SettingsError:
            return profile_mod.AgentGate(profile_mod.AGENT_MODE_CURRENT, {})
        schemas = snapshot.schemas or operator_mod.load_schemas(self.asset_root)
        try:
            evidence = operator_mod.load_evidence(self.gateway_environ(), schemas)
        except operator_mod.OperatorError:
            evidence = None
        contracts = self.contract_identity().as_document()
        facts: dict[str, profile_mod.AgentFacts] = {}
        for key in wanting:
            line = snapshot.layer.lines[key]
            fields = operator_mod.agent_fact_fields(
                key, layer=snapshot.layer, ledger=snapshot.ledger, evidence=evidence,
                admitted_lines=eff.admitted_lines,
                provider_enabled=settings_mod.provider_enabled(eff, line.provider_id),
                contracts=contracts, docs=docs, trusted_docs=self.catalog.docs,
            )
            if fields is not None:
                facts[key] = profile_mod.AgentFacts(**fields)
        return profile_mod.AgentGate(profile_mod.AGENT_MODE_CURRENT, facts)

    def operator_agent_findings(
        self, snapshot: operator_mod.OperatorSnapshot, live: frozenset[str],
    ) -> tuple[list[str], int]:
        """Doctor's (Attention, usable agent count), independently of badges
        and diagnostic results. Every valid declaration may be explicitly
        bound; a failed or stale check stays visible, even while unused."""

        try:
            gate = self.agent_gate(snapshot)
        except (errors.ClaudeMultiError, OSError, ValueError):
            return [], 0
        if not gate.facts:
            return [], 0
        agent_efforts = tuple(self.catalog.docs["native-contract"]["agent_efforts"]["values"])
        eligible = 0
        attention: set[str] = set()
        try:
            eff = self.settings_effective()
            providers = self.effective_transport_docs()["providers"]["providers"]
        except cli_errors.CLIError:
            eff, providers = None, {}
        for key, facts in sorted(gate.facts.items()):
            entry = snapshot.layer.lines[key].core_entry
            verdict = profile_mod.agent_eligibility(entry, key=key, slot=None, effort=entry["default_effort"],
                                                    facts=facts, agent_efforts=agent_efforts)
            if verdict.eligible and eff is not None and settings_mod.line_offered(key, entry, eff):
                credentials = proxy_mod.selected_secret_problems(
                    _SecretAdapter(_SecretModel(key), ()), {key: {"provider": entry["provider"]}},
                    providers, environ=self.environ)
                if not credentials:
                    eligible += 1
            attention.update(finding.message for finding in verdict.warnings)
        store = self.session_store
        for path in store.scan_uuid_records():
            if path.stem not in live:
                continue
            try:
                record, _raw = store.load_raw(path.stem)
            except sessions.SessionError:
                continue
            if record.get("version") != sessions.RECORD_VERSION:
                continue
            for binding in ((record.get("applied") or {}).get("agents") or {}).values():
                key = binding.get("key") if isinstance(binding, dict) else None
                facts = gate.facts.get(key) if isinstance(key, str) else None
                if facts is None:
                    continue
                verdict = profile_mod.agent_eligibility(
                    snapshot.layer.lines[key].core_entry, key=key, slot=None, effort=str(binding.get("effort")),
                    facts=facts, agent_efforts=agent_efforts, mode=profile_mod.AGENT_MODE_RECORD, recorded=True,
                )
                attention.update(finding.message for finding in verdict.warnings)
        return sorted(attention), eligible

    def operator_key_references(self) -> dict[str, list[str]]:
        """line key -> the profiles and named bindings that name it
        (metadata only; unreadable profiles are skipped, never guessed)."""

        found: dict[str, list[str]] = {}
        try:
            names = self.profiles.names()
        except errors.ClaudeMultiError:
            names = []
        for name in names:
            try:
                document = self.profiles.load(name)
            except (errors.ClaudeMultiError, OSError, ValueError):
                continue
            lead = document.get("lead")
            if isinstance(lead, dict) and isinstance(lead.get("model"), str):
                found.setdefault(lead["model"], []).append(f"profile {name} lead")
            for slot, spec in sorted((document.get("agents") or {}).items()):
                if isinstance(spec, dict) and isinstance(spec.get("model"), str):
                    found.setdefault(spec["model"], []).append(f"profile {name} {slot}")
        try:
            bindings = self.bindings.bindings()
        except (errors.ClaudeMultiError, OSError, ValueError):
            bindings = {}
        for name, spec in sorted(bindings.items()):
            if isinstance(spec, dict) and isinstance(spec.get("model"), str):
                found.setdefault(spec["model"], []).append(f"binding {name}")
        return found

    def evidence_versions(self) -> dict[str, str]:
        """launcher / pinned client / gateway baseline for evidence records."""

        return {
            "launcher": self.launcher_version,
            "client": pin.version(self.catalog.docs["native-contract"]),
            "gateway": self.catalog.docs["gateway"]["gateway"]["cliproxyapi_baseline"],
        }

    @property
    def ordinary_docs(self) -> dict[str, Any]:
        """Catalog docs + the custom registry + the operator layer merged.

        The ONLY docs view the ordinary-gateway paths may use (picker,
        prepare_direct, listings, profile math, ordinary scope recompile).
        Managed composition resolution keeps using ``catalog.docs`` —
        customs are never composition-eligible. Computed per access: the
        registry, providers.d and the ledger are small files, never cached
        stale. ``operator.merge_docs`` replaces ``custom.merge_docs``
        (legacy only without the migration marker, secret-origin conflict drops applied);
        operator lines carry explicit origin metadata beside the closed
        entries (``operator.LINES_KEY``).
        """

        snapshot = self.operator_snapshot()
        return operator_mod.merge_docs(
            self.catalog.docs, snapshot.layer, legacy=custom.load_registry(self.environ),
        )

    def lineup_catalog(self, *, snapshot: operator_mod.OperatorSnapshot | None = None,
                       docs: dict[str, Any] | None = None) -> profile_mod.LineupCatalog:
        """The merged catalog and current diagnostic facts. A preparation
        supplies its snapshot so the remembered definitions match its compile."""

        snapshot = snapshot if snapshot is not None else self.operator_snapshot()
        docs = docs if docs is not None else operator_mod.merge_docs(
            self.catalog.docs, snapshot.layer, legacy=custom.load_registry(self.environ))
        return profile_mod.LineupCatalog.from_docs(docs, agent_gate=self.agent_gate(snapshot, docs))

    def reload_catalog(self) -> None:
        """Re-load the catalog (and re-classify an operator override file).

        Cheap: the catalog is small and fully re-validated on load.
        """

        override_path = sessions.config_root(self.environ) / "native-contract.json"
        try:
            self.catalog = catalog.load_catalog(
                self.asset_root,
                contract_override=override_path,
            )
        except catalog.CatalogError as exc:
            if not os.path.lexists(override_path):
                raise
            # Same degradation as __init__: never applied, always reported.
            self.broken_override_error = str(exc)
            self.catalog = catalog.load_catalog(self.asset_root)
        else:
            self.broken_override_error = None
        self.catalog = self._with_endpoint(self.catalog)

    @property
    def catalog_version(self) -> int:
        return self.catalog.docs["version"]["catalog_version"]

    def show_contract_notice(self, stream: TextIO) -> None:
        """Print the legacy-override removal notice, once per Runtime."""

        if self.contract_notice is None or self._contract_notice_shown:
            return
        self._contract_notice_shown = True
        stream.write(f"claude-multi: {self.contract_notice}\n")
        stream.flush()

    @property
    def compositions(self) -> "CompositionStore":
        """The legacy composition store (lazy; the screens and the alias hint only)."""

        if self._compositions is None:
            self._compositions = CompositionStore(
                self._config_root,
                schema=strict_json.load(
                    self.asset_root / "schemas" / "composition.schema.json"
                ),
            )
        return self._compositions

    @compositions.setter
    def compositions(self, store: "CompositionStore") -> None:
        self._compositions = store

    @property
    def profiles(self) -> profile_mod.ProfileStore:
        """The profile store over the catalog seeds (reads side-effect free)."""

        return profile_mod.ProfileStore.for_catalog(self.catalog, self.environ)

    @property
    def bindings(self) -> profile_mod.BindingStore:
        return profile_mod.BindingStore(self.environ)

    @property
    def settings_store(self) -> settings_mod.SettingsStore:
        return settings_mod.SettingsStore(self.environ)

    def current_effective(self, ceiling: choices_mod.WindowCeiling | None = None) -> settings_mod.Effective:
        """Operator Settings resolved against the merged providers/lines,
        with the configured window ceiling (``choices.json``): the one input
        of every evaluation and compile on current state. ``ceiling`` is a
        ceiling read earlier (:meth:`window_ceiling`), so a preparation
        compiles with, and states, one read of it."""

        return dataclasses.replace(self.settings_effective(), window_ceiling=self.effective_window_ceiling(ceiling))

    def settings_effective(self) -> settings_mod.Effective:
        """:meth:`current_effective` with the default window ceiling: the
        operator Settings alone (the Settings screen pairs it with the
        ceiling it shows, so an unreadable ``choices.json`` never hides
        ``settings.json``)."""

        store = self.settings_store
        snapshot = self.operator_snapshot()
        docs = operator_mod.merge_docs(
            self.catalog.docs, snapshot.layer, legacy=custom.load_registry(self.environ),
        )
        try:
            document = store.load()
            eff = settings_mod.effective(
                document,
                provider_ids=docs["providers"]["providers"],
                line_keys=(docs["models-v2"] if "models-v2" in docs else docs["models"])["models"],
            )
        except settings_mod.SettingsError as exc:
            raise cli_errors.CLIError(
                f"cannot read operator settings: {exc}; fix or remove {store.path}"
            ) from exc
        return operator_effective(eff, snapshot, docs=docs)

    def window_ceiling(self) -> choices_mod.WindowCeiling:
        """The configured context window ceiling (``choices.json``); never raises."""

        return choices_mod.window_ceiling(self.environ)

    def effective_window_ceiling(self, ceiling: choices_mod.WindowCeiling | None = None) -> int:
        """The ceiling every compile and evaluation on current state uses
        (``ceiling``, or a fresh read). An unreadable ``choices.json``
        refuses (fail closed: a lowered ceiling is never silently replaced
        by the larger default)."""

        ceiling = self.window_ceiling() if ceiling is None else ceiling
        if ceiling.problem is not None:
            raise cli_errors.CLIError(
                f"cannot read the window ceiling: {ceiling.problem}; {ceiling.remedy}", remedy=ceiling.remedy
            )
        return ceiling.value

    # ------------------------------------------------------ readiness

    def readiness_observations(
        self, lineups: Iterable[profile_mod.ResolvedLineup] = (), *, served: bool = True,
        lan_observed: dict[str, Any] | None = None,
    ) -> readiness_mod.Observations:
        """One refresh of the local observations readiness reads: the
        served set (one loopback ``/v1/models``; None when the gateway is not
        observable), OAuth credential-record counts (None when the auth
        directory is not observable) and one short LAN probe per keyless LAN
        provider the ``lineups`` bind (the ``lan_probe`` seam). Never a
        provider call, never a write; every failure is an unknown.
        ``lan_observed`` (base URL -> observation), when given, is shared
        with another report of the same refresh (doctor): a URL already
        observed there is reused, a new one is added, so one refresh probes
        each LAN provider at most once."""

        import claude_multi.cli.gateway_facts as gateway_facts

        served_set: frozenset[str] | None = None
        # The seam rule of every loopback observation: the live read only on
        # a fully live Runtime (no gateway seam injected) or through an
        # injected served_models_callback; a Runtime that injects only
        # health_get observes nothing here (never a real dial in tests).
        if served and self.served_models_callback is None and self.health_get is not None:
            served = False
        if served:
            try:
                ids, _status = self.served_models(self.gateway_token())
            except (errors.ClaudeMultiError, OSError, ValueError, KeyError):
                ids = None
            served_set = frozenset(ids) if ids is not None else None
        try:
            records = gateway_facts.oauth_record_observation(self)
        except (OSError, KeyError, ValueError):
            records = None
        providers = self.readiness_providers()
        lan: dict[str, tuple[str, str]] = {}
        for lineup in lineups:
            bindings = [lineup.lead.binding, *(agent.binding for agent in lineup.agents.values())]
            for binding in bindings:
                provider = providers.get(binding.provider) or {}
                if binding.provider in lan or not readiness_mod.is_lan(provider):
                    continue
                url = str(provider["transport"].get("base_url", ""))
                observed = lan_observed.get(url) if lan_observed is not None else None
                if observed is None:
                    observed = gateway_facts.lan_reachability(self, url)
                    if lan_observed is not None:
                        lan_observed[url] = observed
                lan[binding.provider] = (observed.state, observed.text())
        login = dict(gateway_facts._OAUTH_LOGIN_COMMANDS)
        return readiness_mod.Observations(served=served_set, oauth_records=records, lan=lan, login=login)

    def readiness_providers(self) -> dict[str, Any]:
        """The provider view readiness observes: the effective transports (a
        selected, approved API-key alternative replaces the OAuth pool, as on
        the Providers screen and in the launch's secret problems); catalog intent is unchanged. An
        unreadable operator view falls back to the lineup catalog's."""

        try:
            return self.effective_transport_docs()["providers"]["providers"]
        except (errors.ClaudeMultiError, OSError, ValueError, KeyError):
            return self.lineup_catalog().providers

    def lineup_readiness(
        self, lineup: profile_mod.ResolvedLineup, observations: readiness_mod.Observations | None = None,
    ) -> tuple[readiness_mod.SlotReadiness, ...]:
        """Per-slot readiness of one lineup (authority secret problems included)."""

        if observations is None:
            observations = self.readiness_observations((lineup,))
        problems: dict[str, str] = {}
        providers = self.readiness_providers()
        try:
            texts = self.lineup_secret_problems(lineup)
        except (KeyError, errors.ClaudeMultiError):
            texts = []
        for text in texts:
            for provider_id in providers:
                if f" ({provider_id}): " in text:
                    problems.setdefault(provider_id, text)
        observed = dataclasses.replace(observations, credential_problems=problems)
        return readiness_mod.lineup_readiness(lineup, providers, observed)

    def profile_readiness(
        self, names: Iterable[str], observations: readiness_mod.Observations | None = None,
    ) -> dict[str, tuple[readiness_mod.SlotReadiness, ...]]:
        """Readiness of each named profile over one observation refresh
        (current Settings; an evaluation error is an authority block).
        Names that do not exist or cannot be loaded are left out."""

        lcat = self.lineup_catalog()
        eff = self.current_effective()
        try:
            bindings = self._bindings_map()
        except profile_mod.BindingError:
            bindings = {}
        lineups: dict[str, Any] = {}
        for name in names:
            if name in lineups:
                continue
            try:
                if not self.profiles.contains(name):
                    continue
                document = self.profiles.load(name)
            except (profile_mod.ProfileError, OSError):
                continue
            evaluation = profile_mod.evaluate(document, lcat, bindings=bindings, effective=eff, ad_hoc=False)
            lineups[name] = evaluation
        if observations is None:
            observations = self.readiness_observations(
                ev.lineup for ev in lineups.values() if ev.lineup is not None and not ev.errors)
        verdicts: dict[str, tuple[readiness_mod.SlotReadiness, ...]] = {}
        for name, evaluation in lineups.items():
            verdicts[name] = (readiness_mod.evaluation_blocked(evaluation.errors)
                              if evaluation.errors or evaluation.lineup is None
                              else self.lineup_readiness(evaluation.lineup, observations))
        return verdicts

    def seed_readiness(
        self, observations: readiness_mod.Observations | None = None,
    ) -> dict[str, tuple[readiness_mod.SlotReadiness, ...]]:
        """Readiness of each seed in :data:`readiness.SEED_ORDER`."""

        return self.profile_readiness(readiness_mod.SEED_ORDER, observations)

    def line_readiness(
        self, observations: readiness_mod.Observations | None = None, *,
        assume_keys: frozenset[str] = frozenset(),
    ) -> dict[str, readiness_mod.SlotReadiness]:
        """The readiness of every offered line at its default effort, over
        one observation refresh (credential problems included). Read-only.

        ``assume_keys`` names providers whose key a planned change saves
        before the lines are used: their credential problems are dropped
        and, when the gateway is observed, their lines count as served (the
        change's own render serves them)."""

        from types import SimpleNamespace

        lcat = self.lineup_catalog()
        eff = self.current_effective()
        view = scope_mod.line_view(lcat, eff)
        providers = self.readiness_providers()
        pseudo: dict[str, Any] = {}
        for line in view.lines:
            entry = line.entry
            try:
                selector = profile_mod.binding_selector(entry, line.provider, entry["default_effort"], lead=False)[0]
            except (KeyError, TypeError):
                continue
            pseudo[line.key] = SimpleNamespace(key=line.key, provider=line.provider_id, selector=selector)
        if not pseudo:
            return {}
        first, *rest = list(pseudo.values())
        bundle = SimpleNamespace(lead=SimpleNamespace(binding=first),
                                 agents={index: SimpleNamespace(binding=b) for index, b in enumerate(rest)})
        if observations is None:
            observations = self.readiness_observations((bundle,))
        problems: dict[str, str] = {}
        for text in self.lineup_secret_problems(bundle):
            for provider_id in providers:
                if f" ({provider_id}): " in text:
                    problems.setdefault(provider_id, text)
        served = observations.served
        if assume_keys:
            problems = {pid: text for pid, text in problems.items() if pid not in assume_keys}
            if served is not None:
                served = frozenset(served) | {binding.selector for binding in pseudo.values()
                                              if binding.provider in assume_keys}
        observed = dataclasses.replace(observations, credential_problems=problems, served=served)
        return {key: readiness_mod.slot_readiness("cm-explorer", binding, providers, observed)
                for key, binding in pseudo.items()}

    def starter_plan(self, name: str, *, assume_keys: frozenset[str] = frozenset(),
                     ) -> tuple[readiness_mod.StarterPlan, list[str]]:
        """``profile starter``: the plan over the
        offered lines that are locally ready, from the shipped balanced
        seed's structure, plus the preview warnings of the proposed lineup.
        Read-only: no admission, qualification, provider call or write.
        ``assume_keys``: providers a planned change gives a key first
        (:meth:`line_readiness`)."""

        lcat = self.lineup_catalog()
        eff = self.current_effective()
        verdicts = self.line_readiness(assume_keys=frozenset(assume_keys))
        if not verdicts:
            return readiness_mod.StarterPlan(None, (), (), (), refusal="starter: no offered line"), []
        ready = frozenset(key for key, row in verdicts.items() if row.ready)
        try:
            bindings = self._bindings_map()
        except profile_mod.BindingError:
            bindings = {}

        def evaluate(document: dict[str, Any]) -> profile_mod.Evaluation:
            return profile_mod.evaluate(document, lcat, bindings=bindings, effective=eff, ad_hoc=False)

        def lead_document(key: str, effort: str) -> dict[str, Any]:
            document = profile_mod.ad_hoc_direct(key, effort)
            document["name"] = name
            return document

        leads = [key for key in sorted(ready) if not evaluate(lead_document(key, profile_mod.ULTRACODE)).errors]
        template = copy.deepcopy(self.catalog.seed_profiles[readiness_mod.STARTER_TEMPLATE])
        template_lead = template.get("lead") or {}
        context_lead = (dict(template_lead) if template_lead.get("model") in leads
                        else {"model": leads[0], "effort": profile_mod.ULTRACODE} if leads else None)

        def slot_ok(slot: str, key: str, effort: str) -> bool:
            """Authority for one binding, in the template's context (other
            slots' structural rules, such as a grade's ``requires``, are
            the planner's; only this slot's errors count)."""

            if slot == catalog.LEAD_ROLE:
                return not evaluate(lead_document(key, effort)).errors
            if context_lead is None:
                return False
            document = copy.deepcopy(template)
            document.pop("seed", None)
            document["name"] = name
            document["lead"] = context_lead
            document["agents"] = {**(template.get("agents") or {}), slot: {"model": key, "effort": effort}}
            return not any(f"agents.{slot}:" in error or f"agents.{slot}." in error
                           for error in evaluate(document).errors)

        plan = readiness_mod.plan_starter(template, lcat, name=name, ready_keys=ready, slot_ok=slot_ok)
        if plan.document is None:
            return plan, []
        evaluation = evaluate(plan.document)
        if evaluation.errors or evaluation.lineup is None:
            return dataclasses.replace(plan, document=None, refusal=(
                "starter: the proposed profile does not evaluate: " + "; ".join(evaluation.errors))), []
        warnings = [finding.message for finding in evaluation.lineup.warnings]
        return plan, warnings

    def effective_transport_docs(self) -> dict[str, Any]:
        """Observation view: selected API-key alternatives replace pool transports.

        Record/catalog intent is untouched. UI credentials, login and quota
        guidance must describe the route the operator actually selected.
        """
        snapshot = self.operator_snapshot()
        docs = operator_mod.merge_docs(self.catalog.docs, snapshot.layer, legacy=custom.load_registry(self.environ))
        return operator_mod.apply_transports(docs, operator_mod.transport_selections(docs, snapshot.ledger))

    def lineup_secret_problems(self, lineup: profile_mod.ResolvedLineup) -> list[str]:
        """The lineup-based ``proxy.selected_secret_problems`` (no proxy change)."""

        snapshot = self.operator_snapshot()
        docs = operator_mod.merge_docs(self.catalog.docs, snapshot.layer, legacy=custom.load_registry(self.environ))
        # The secret check uses the effective transport of the lead
        # and every bound agent (a selected reviewed alternative replaces the
        # pool: its key must be present and its route approved).
        selections = operator_mod.transport_selections(docs, snapshot.ledger)
        docs = operator_mod.apply_transports(docs, selections)
        lines = (docs["models-v2"] if "models-v2" in docs else docs["models"])["models"]
        keys = [lineup.lead.binding.key, *(a.binding.key for a in lineup.agents.values())]
        withheld = [
            f"provider {pid}: {selection.problem}"
            for pid, selection in sorted(selections.items())
            if selection.problem is not None and any(lines.get(key, {}).get("provider") == pid for key in keys)
        ]
        models = {
            key: {"provider": lines[key]["provider"]} for key in keys if key in lines
        }
        adapter = _SecretAdapter(
            lead=_SecretModel(lineup.lead.binding.key),
            variants=tuple(_SecretModel(a.binding.key) for a in lineup.agents.values()),
        )
        return withheld + proxy_mod.selected_secret_problems(
            adapter,
            models,
            docs["providers"]["providers"],
            environ=self.environ,
        )

    @property
    def preferences_store(self) -> settings_mod.PreferencesStore:
        return settings_mod.PreferencesStore(self.environ)

    def feedback_drafts(self) -> str:
        """The current feedback-drafts preference (an unreadable file compiles ``off``)."""

        return settings_mod.feedback_drafts_preference(self.environ)[0]

    def session_env_keep(self) -> tuple[str, ...]:
        """The user's ``session_env_keep`` names (``choices.json``); an
        unreadable document keeps nothing, so every ``*_API_KEY`` is removed."""

        from claude_multi import choices

        try:
            return tuple(choices.read(self.environ).get("session_env_keep"))
        except choices.ChoicesError:
            return ()

    def provider_secret_names(self) -> frozenset[str]:
        """Every provider API-key name read now: the merged providers'
        ``env:`` references and every providers.d declaration's, admitted or
        not, valid or not, selected or not (the scrub set a launch compiles
        from, ``scope.line_view``)."""

        docs = self.ordinary_docs
        names = {name for provider in docs["providers"]["providers"].values()
                 if isinstance(provider, dict) and (name := scope_mod._secret_env_name(provider)) is not None}
        names |= {str(name) for name in docs.get(operator_mod.SECRET_NAMES_KEY) or ()}
        return frozenset(names)

    def provider_credential_names(self) -> frozenset[str]:
        """:meth:`provider_secret_names` plus the reviewed transport
        alternatives' key names: every name a kept variable may never take."""

        return compiler.credential_env_names(self.provider_secret_names())

    def converge_parts(self) -> Any:
        """What ``transition.converge``/``expected_plan`` need from this Runtime."""

        transition = _transition_module()
        return transition.RuntimeParts(
            docs=self.ordinary_docs,
            prompt_bodies=self.catalog.prompt_bodies,
            hook_command=self.hook3_command,
            token_helper_command=self.token_helper_command,
            environ={**self.environ, "HOME": str(self.home)},
            managed_root=self.managed_root,
            resolved_hook_command=(
                self.resolved_hook_command if self._shims_refreshed else None
            ),
            feedback_drafts=self.feedback_drafts(),
        )

    def _bindings_map(self) -> dict[str, Any]:
        return self.bindings.bindings()

    def _evaluate(
        self,
        document: dict[str, Any],
        *,
        profile_name: str | None,
        effective: settings_mod.Effective,
        lcat: profile_mod.LineupCatalog,
    ) -> profile_mod.ResolvedLineup:
        evaluation = profile_mod.evaluate(
            document,
            lcat,
            bindings=self._bindings_map(),
            effective=effective,
            ad_hoc=profile_name is None,
        )
        if evaluation.errors:
            raise cli_types.LaunchPlanError(evaluation.errors)
        return evaluation.lineup

    def prepare(
        self,
        target: cli_types.LaunchTarget,
        *,
        action: str,
        passthrough: list[str],
        session_id: str | None = None,
        no_subagents: bool | None = None,
        lead_override: str | None = None,
        expected: tuple[Any, Any, Any, Any] | None = None,
        read_only: bool = False,
    ) -> cli_types.PreparedLaunch:
        """The one launch/resume/relaunch preparation (no state written).

        Fresh: evaluate ``target.document`` against current Settings. Resume:
        the record (launch-time migration of a legacy record first), its
        source document (lead override, relaunch target, pending change,
        followed profile, applied lineup), the needs-a-choice check, current
        Settings, the generation and the compile. The scope is
        compiled from the lineup the committed record restates, so it is
        exactly the record-authoritative compile converge reproduces.
        ``read_only`` (``--print-launch``) never migrates or converges a
        resolved fork marker.
        """

        if not read_only:
            # A new install's port is recorded before the scope is compiled
            # for it (the first launch otherwise compiles the packaged port).
            self.provision_endpoint()
        store = self.session_store
        operator_snapshot = self.operator_snapshot()
        docs = operator_mod.merge_docs(self.catalog.docs, operator_snapshot.layer,
                                       legacy=custom.load_registry(self.environ))
        lcat = self.lineup_catalog(snapshot=operator_snapshot, docs=docs)
        # One read of the window ceiling: the plan is compiled with it and
        # states it, and perform refuses when it changed before the commit.
        ceiling = self.window_ceiling()
        eff = self.current_effective(ceiling)
        notices: list[str] = []
        diff: list[str] = []
        model_relaunch = False
        record: dict[str, Any] | None = None
        disk_n: int | None = None
        if action == "fresh":
            if target.kind == "unloadable":
                problem = self.selection_load_error
                text = str(problem) if problem is not None else f"cannot load profile {target.profile!r}"
                fix = getattr(problem, "remedy", None)
                raise cli_types.LaunchPlanError([text] + ([f"fix: {fix}"] if fix else []))
            if target.document is None:
                raise cli_errors.CLIError("a fresh launch needs a profile document")
            profile_name, follow = target.profile, target.follow
            lineup = self._evaluate(
                target.document, profile_name=profile_name, effective=eff, lcat=lcat
            )
            mid = store.new_id()
            session_action = compiler.build_fresh(mid)
            epoch = 1
            new_gen = 1
            ns = bool(no_subagents)
            cwd = self.cwd
            source_kind = "fresh"
        elif action == "resume":
            if session_id is None:
                raise cli_errors.CLIError("resume requires a managed session ID")
            record = store.resolve(session_id)
            mid = sessions.managed_id(record)
            m8 = mid[:8]
            if record.get("version") != sessions.RECORD_VERSION:
                if read_only:
                    raise cli_errors.CLIError(f"would migrate {m8} first (claude-multi migrate)")
                try:
                    outcome = migrate_mod.migrate_one(
                        store,
                        lcat,
                        mid,
                        hook_command=self.resolved_hook_command,
                        hook_shim_path=scope_mod.hook_shim_path(store.root),
                        profile_exists=self.profiles.contains,
                        home=self.home,
                        barrier_timeout=self.served_barrier_timeout,
                    )
                except state.BarrierBusyError as exc:
                    raise launch.LaunchError(f"{launch.BARRIER_BUSY_LAUNCH} ({exc.strerror})") from exc
                notices.append("migrated legacy record: " + migrate_mod.outcome_line(outcome))
                record = store.load_v4(mid)
                if expected is not None and expected[0] != record.get("launch_epoch", 0):
                    raise launch.LaunchError(
                        f"session {m8} changed while the relaunch was being confirmed; "
                        "run the command again"
                    )
            elif expected is not None:
                sample = (
                    record.get("launch_epoch", 0),
                    record.get("mutation_token"),
                    record.get("applied_hash"),
                    record.get("lineup_generation"),
                )
                if expected[2] is None:
                    same = expected[0] == sample[0]
                else:
                    same = tuple(expected) == sample
                if not same:
                    raise launch.LaunchError(
                        f"session {m8} changed while the relaunch was being confirmed; "
                        "run the command again"
                    )
            if sessions.drop_resolved_pending_forks(record) is not None:
                if read_only:
                    record = sessions.drop_resolved_pending_forks(record)
                else:
                    store.converge_pending_forks(mid)
                    record = store.load_v4(mid)
            if record.get("pending_forks"):
                raise launch.LaunchError(sessions.pending_fork_message(record))
            if record.get("identity_state") == sessions.IDENTITY_REPAIR_NEEDED:
                if "observed_cwd" in record or "observed_model" not in record:
                    raise launch.LaunchError(sessions.relink_message(record))
                model_relaunch = True
            profile_name, follow = record["profile"], bool(record["follow"])
            source_kind = "record"
            document: dict[str, Any] | None = None
            if lead_override is not None:
                document = sessions.applied_document(record)
                document["lead"] = {"model": lead_override, "effort": profile_mod.ULTRACODE}
                profile_name, follow = None, False
                source_kind = "override"
                model_relaunch = True
            elif target.kind == "relaunch":
                document = (
                    copy.deepcopy(target.document)
                    if target.document is not None
                    else self.profiles.load(str(target.profile))
                )
                profile_name, follow = target.profile, target.follow
                source_kind = "relaunch"
            if document is None and record.get("pending"):
                pending = record["pending"]
                reasons = "; ".join(pending.get("reasons") or ())
                try:
                    if pending["kind"] == "profile":
                        document = self.profiles.load(pending["profile"])
                    else:
                        document = copy.deepcopy(pending["document"])
                    candidate = profile_mod.evaluate(
                        document,
                        lcat,
                        bindings=self._bindings_map(),
                        effective=eff,
                        ad_hoc=pending.get("profile") is None,
                    )
                    if candidate.errors:
                        raise profile_mod.ProfileError("; ".join(candidate.errors))
                except (profile_mod.ProfileError, profile_mod.BindingError) as exc:
                    notices.append(f"pending relaunch change dropped: {exc} ({reasons})")
                    document = None
                else:
                    profile_name, follow = pending.get("profile"), bool(pending.get("follow"))
                    source_kind = "pending"
            if document is None:
                if record["follow"] and record["profile"] and self.profiles.contains(
                    record["profile"]
                ):
                    document = self.profiles.load(record["profile"])
                    profile_name, follow = record["profile"], True
                    source_kind = "follow"
                elif record["follow"]:
                    document = sessions.applied_document(record)
                    profile_name, follow = record["profile"], False
                    notices.append(
                        f"profile {record['profile']!r} no longer exists; session {m8} "
                        "is now pinned to its applied lineup"
                    )
                else:
                    document = sessions.applied_document(record)
                    target_lead = record.get("lead_target")
                    if target_lead is not None:
                        document["lead"] = {
                            "model": target_lead["key"],
                            "effort": target_lead["effort"],
                        }
                        notices.append(
                            "applying the requested lead "
                            f"{lineup_mod.binding_label(lcat.lines.get(target_lead['key']), target_lead['effort'], key=target_lead['key'])} "
                            "at this resume"
                        )
            lead_doc = document.get("lead") if isinstance(document, dict) else None
            if isinstance(lead_doc, dict) and isinstance(lead_doc.get("model"), str):
                if sessions.lead_key_needs_choice(lead_doc["model"], lcat):
                    try:
                        notice = lcat.resolve_key(lead_doc["model"]).notice
                    except catalog.CatalogError:
                        notice = (
                            f"lead {lead_doc['model']}: unknown to catalog "
                            f"{lcat.catalog_version} (not a custom model either)"
                        )
                    raise cli_types.NeedsChoiceError(record, lead_doc["model"], notice)
            lineup = self._evaluate(document, profile_name=profile_name, effective=eff, lcat=lcat)
            recorded_ns = bool(record["applied"]["no_subagents"])
            if no_subagents is not None and bool(no_subagents) != recorded_ns:
                raise launch.LaunchError(
                    "explicit --no-subagents does not match the session's subagent "
                    "policy; relaunch fresh to change it"
                )
            ns = recorded_ns
            # Every resume source kind shows its diff rows (a pinned record
            # included), so an agent context-class move is never applied
            # silently.
            if source_kind in ("record", "follow", "pending", "relaunch", "override"):
                diff = lineup_mod.render_diff(
                    lineup_mod.diff_rows(
                        record["applied"], lineup, lcat, old_lead_class=record.get("lead_class")
                    )
                    + lineup_mod.window_diff_rows(record, lineup)
                )
            notices.extend(
                f"settings changed since the last launch: {line}"
                for line in settings_mod.drift(record["applied"]["settings"], eff)
            )
            session_action = compiler.build_resume(mid, sessions.runtime_session_id(record))
            epoch = record.get("launch_epoch", 0) + 1
            prior = int(record.get("lineup_generation", 0))
            semantic_changed = _semantic_lineup(lineup, ns) != _semantic_applied(
                record["applied"]
            )
            new_gen = prior + 1 if (prior == 0 or semantic_changed) else prior
            disk_n = launch.disk_generation(scope_mod.scope_dir(store.root, mid))
            if disk_n > prior:
                new_gen = max(new_gen, disk_n + 1)
            cwd = record["cwd"]
        elif action == "fork":
            raise cli_errors.CLIError(compiler.FORK_UNVERIFIED_GUIDANCE)
        else:
            raise cli_errors.CLIError(f"unknown launch action {action!r}")

        notices.extend(
            views.retired_lead_note(
                lcat, lineup.lead.binding.requested, timing="applies at this resume",
                named=lineup.lead.binding.named,
            )
            if action == "resume" and finding.code == "retired-successor"
            and finding.slot == catalog.LEAD_ROLE else finding.message
            for finding in lineup.notices
        )
        # The scope is compiled from the document the committed record
        # restates (resolved keys, no named bindings): exactly what converge
        # and doctor recompile from the record.
        restated = _restated_document(lineup, profile_name)
        compiled = self._evaluate(restated, profile_name=profile_name, effective=eff, lcat=lcat)
        result = self.compile_callback(
            docs=docs,
            prompt_bodies=self.catalog.prompt_bodies,
            lineup=compiled,
            effective=eff,
            session_action=session_action,
            lineup_generation=new_gen,
            state_root=store.root,
            scope_dir=scope_mod.scope_dir(store.root, mid),
            hook_command=self.hook3_command,
            token_helper_command=self.token_helper_command,
            launch_epoch=epoch,
            passthrough=list(passthrough),
            no_subagents=ns,
            session_cwd=cwd,
            launch_environ=self.environ,
            proxy_env=self.proxy_env(cwd, scope_mod.scope_dir(store.root, mid)),
            feedback_drafts=self.feedback_drafts(),
            agent_gate=lcat.agent_gate,
            default_mode=self.permission_default_mode(cwd),
            env_keep=self.session_env_keep(),
        )
        if result.scope_plan is None or result.fence is None:
            raise cli_errors.CLIError("the compile produced no v2 scope plan")
        if result.env_keep is not None:
            notices.extend(f"! {name} is removed from this session despite session_env_keep: {reason}"
                           for name, reason in result.env_keep.refused)
        context = compiler.lead_set_context(
            result.fence,
            settings_mod.compaction_percent_for(eff, compiled.settings_overrides),
            ceiling=profile_mod.window_ceiling(eff),
        )
        applied = sessions.applied_from_lineup(
            compiled,
            context=context,
            settings_snapshot=settings_mod.snapshot(eff),
            no_subagents=ns,
        )
        fence_digest = sessions.launch_digest(
            result.scope_plan.settings, result.scope_plan.other_files[scope_mod.LEAD_SET_JSON]
        )
        common = dict(
            profile=profile_name,
            follow=follow,
            applied=applied,
            lineup_generation=new_gen,
            lead_class=compiled.lead_class,
            catalog_version=self.catalog_version,
            catalog_hash=self.catalog.bundle_sha256,
            launcher_version=self.launcher_version,
            launch_epoch=epoch,
            launch_fence=fence_digest,
            scope_lead=sessions.lead_ref(applied["lead"]),
        )
        if record is None:
            new_record = sessions.make_v4_record(managed_id=mid, cwd=cwd, **common)
        else:
            new_record = sessions.next_v4_record(record, **common)
        # Readiness is an observation, never route permission. A known
        # unreachable LAN route is a warning too; credentials and actual
        # route/transport usability have their own hard checks.
        if action == "fresh":
            notices.extend(f"! {line}" for line in self.selection_notices)
            self.selection_notices = []
        rows = self.lineup_readiness(compiled)
        notices.extend(f"! {line}" for line in readiness_mod.unready_texts(rows))
        notices.extend(f"! {finding.message}" for finding in lineup.warnings
                       if finding.code in profile_mod.MODEL_WARNING_CODES)
        notices.extend(f"! {finding.message}" for finding in scope_mod.workflow_default_warnings(
            lcat, eff, policy=context.policy))
        notices.extend(f"! {line}" for line in self.settings_skew(cwd, passthrough))
        prepared = cli_types.PreparedLaunch(
            result,
            new_record,
            compiled,
            target,
            diff=tuple(diff),
            notices=tuple(notices),
            model_relaunch=model_relaunch,
            expected_launch_epoch=None if record is None else record.get("launch_epoch", 0),
            expected_mutation_token=None if record is None else record.get("mutation_token"),
            expected_applied_hash=None if record is None else record.get("applied_hash"),
            expected_lineup_generation=(
                None if record is None else record.get("lineup_generation")
            ),
            expected_disk_generation=disk_n,
            secret_problems=tuple(self.lineup_secret_problems(compiled)),
            workflow_window=views.workflow_window(lcat, eff, compiled),
            window_ceiling=ceiling,
        )
        # Remember the operator definitions this plan was
        # compiled from; perform revalidates them before anything commits.
        keys = prepared_line_keys(prepared, lcat)
        self.__dict__.setdefault("_operator_prepared", {})[id(prepared.result)] = (
            prepared.result, operator_launch_digests(operator_snapshot, keys),
            launch_route_digests(operator_snapshot, docs, keys),
        )
        return prepared

    def revalidate_operator(self, prepared: cli_types.PreparedLaunch) -> None:
        """Before either launch commit, definitions and selected routes must
        still match the prepared plan and be usable. Badge/evidence changes
        alone never invalidate it. Workflow defaults can send requests too.
        """

        lineup = prepared.lineup
        if lineup is None:
            return
        snapshot = self.operator_snapshot()
        docs = operator_mod.merge_docs(self.catalog.docs, snapshot.layer, legacy=custom.load_registry(self.environ))
        lcat = profile_mod.LineupCatalog.from_docs(docs)
        keys = prepared_line_keys(prepared, lcat)
        # Card/Direct adapters may replace presentation fields of a plan;
        # the immutable compile result is the identity of its routing proof.
        stored = self.__dict__.get("_operator_prepared", {}).get(id(prepared.result))
        stored = stored if stored is not None and stored[0] is prepared.result else None
        planned = stored[1] if stored is not None else None
        current = operator_launch_digests(snapshot, keys)
        routes = launch_route_digests(snapshot, docs, keys)
        eff = self.settings_effective()
        for key in sorted(set(keys) | set(planned or {}) | set(current)):
            changed = planned is not None and planned.get(key) != current.get(key)
            if stored is not None:
                changed = changed or stored[2].get(key) != routes.get(key)
            entry = lcat.lines.get(key)
            usable = entry is not None and settings_mod.line_offered(key, entry, eff)
            if changed or not usable:
                raise launch.LaunchError(
                    f"operator line {key} changed or its route/provider became unavailable since this launch was "
                    "prepared — nothing launched; run the command again"
                )

    def revalidate_ceiling(self, prepared: cli_types.PreparedLaunch) -> None:
        """Stale-plan refusal: before a prepared launch commits, the window
        ceiling must still read as the one the plan was compiled with. A
        changed ceiling or an unreadable ``choices.json`` refuses, so a plan
        prepared (or shown on the card) before the change never launches."""

        if prepared.lineup is None:
            return
        planned = prepared.window_ceiling
        current = self.window_ceiling()
        if current.problem is not None:
            raise launch.LaunchError(
                f"cannot read the window ceiling: {current.problem} — nothing launched; {current.remedy}"
            )
        if planned is None or planned.problem is not None or planned.value != current.value:
            before = "unknown" if planned is None else profile_mod.format_tokens(planned.value)
            raise launch.LaunchError(
                f"the window ceiling changed since this launch was prepared ({before} → "
                f"{profile_mod.format_tokens(current.value)}) — nothing launched; run the command again"
            )

    def revalidate_env_keep(self, prepared: cli_types.PreparedLaunch) -> None:
        """Stale-plan refusal of the kept environment: before a prepared
        launch commits, every variable its plan lets reach the session is
        checked against the providers and choices read now. No current
        provider or transport API-key name (every declaration counted, admitted
        or not) may reach it, and every ``*_API_KEY`` that does must still be
        named by ``session_env_keep`` and pass the keep policy. Names only;
        an entry that looks like a key value is never shown."""

        from claude_multi import secret_store

        result = prepared.result
        if prepared.lineup is None or result is None:
            return
        try:
            credentials = self.provider_credential_names()
        except (cli_errors.ClaudeMultiError, OSError, ValueError, KeyError, TypeError) as exc:
            raise launch.LaunchError(
                "cannot read the providers' API-key names to check the kept environment "
                f"({type(exc).__name__}) — nothing launched; run claude-multi doctor"
            ) from exc
        if getattr(result, "env_unset", None) is None:
            return
        keep = frozenset(self.session_env_keep())
        unset = frozenset(result.env_unset)
        overridden = frozenset(getattr(result, "env_set", None) or ())
        stale = sorted(
            name for name in self.environ
            if name not in unset and name not in compiler.ENV_UNSET_KEEP and name not in overridden
            and (name in credentials or (name.endswith(secret_store.API_KEY_SUFFIX) and (
                name not in keep
                or secret_store.env_keep_problem(name, credential_names=credentials) is not None)))
        )
        if stale:
            shown = ", ".join(dict.fromkeys(secret_store.shown_env_name(name) for name in stale))
            raise launch.LaunchError(
                f"the kept session environment changed since this launch was prepared ({shown} would reach "
                "the session, which the providers and choices read now forbid) — nothing launched; run the "
                "command again"
            )

    def perform(
        self, prepared: cli_types.PreparedLaunch, *, resume_decision: str | None = None
    ) -> Any:
        """Launch a prepared plan: credentials, resume gate, then ``perform_launch``.

        A plan with secret problems never launches (the line confirm
        shows them first), nor does one whose operator lines, window
        ceiling or kept environment changed since it was prepared (checked
        again inside the commit barrier). A fresh launch makes the state root a version-4 root
        (``ensure_state_v4``) before ``perform_launch`` takes the shared
        migration hold (never nested).
        """

        if prepared.secret_problems:
            raise cli_types.LaunchPlanError(prepared.secret_problems)
        self.revalidate_operator(prepared)
        self.revalidate_ceiling(prepared)
        self.revalidate_env_keep(prepared)
        self._enforce_resume_gate(prepared, resume_decision)
        if self.launch_callback is not None:
            return self.launch_callback(prepared)
        # Managed policy: settings that would break a managed session refuse
        # the launch before anything is written; a model lock is named.
        findings = self.managed_findings(prepared.lineup)
        blocks = [f"{managed.finding_text(lock)} — {managed.POLICY_REMEDY}"
                  for lock in findings if lock.blocks]
        if blocks:
            raise launch.LaunchError("; ".join(blocks))
        for lock in findings:
            if lock.kind == "model":
                print(f"claude-multi: {managed.finding_text(lock)}", file=sys.stderr)
        store = self.session_store
        if prepared.result.session_action.kind == "fresh":
            try:
                sessions.ensure_state_v4(store.root)
            except sessions.MigrationBusyError as exc:
                raise launch.LaunchError(launch.MIGRATION_BUSY_LAUNCH) from exc
        return launch.perform_launch(
            prepared.result,
            record=prepared.record,
            store=store,
            native_contract=self.catalog.docs["native-contract"],
            gateway=self.catalog.docs["gateway"],
            execve=self.execve or posix_fs.exec_replace,
            environ=self.environ,
            home=self.home,
            health_get=self.health_get,
            readiness=self._launch_readiness,
            retain_root=paths.retained_root(self.environ) if self._shims_refreshed else None,
            allow_model_relaunch=prepared.model_relaunch,
            expected_launch_epoch=prepared.expected_launch_epoch,
            expected_mutation_token=prepared.expected_mutation_token,
            expected_applied_hash=prepared.expected_applied_hash,
            expected_lineup_generation=prepared.expected_lineup_generation,
            expected_disk_generation=prepared.expected_disk_generation,
            barrier_timeout=self.served_barrier_timeout,
            revalidate=lambda: self._revalidate_in_barrier(prepared),
        )

    def _revalidate_in_barrier(self, prepared: cli_types.PreparedLaunch) -> None:
        """Inside the launch commit barrier, the root authority, the
        operator eligibility, the window ceiling and the kept environment are
        checked a second time (the first check ran before the card's
        barrier-free preparation)."""

        refusal = self.root_authority_refusal()
        if refusal is not None:
            raise launch.LaunchError(refusal)
        self.revalidate_operator(prepared)
        self.revalidate_ceiling(prepared)
        self.revalidate_env_keep(prepared)

    def _enforce_resume_gate(
        self, prepared: cli_types.PreparedLaunch, resume_decision: str | None
    ) -> None:
        """Mandatory resume-gate backstop for every real launch.

        Interactive surfaces present the gate and thread the operator's
        decision; noninteractive paths get the actionable refusal here.
        `force` bypasses ONLY the daemon-owned branch (the liveness signal is
        heuristic); repair-needed and transcript problems are never
        bypassed. A relaunch is a resume like any other: the gate
        applies in full and a relaunch never stops a session.
        """

        action = prepared.result.session_action
        if action.kind != "resume" or not prepared.record:
            return
        gate = resume_checks._evaluate_resume_gate(
            self, prepared.record, live_prefixes=self._live_prefixes()
        )
        if gate.kind == "ok":
            return
        if gate.kind == "daemon-owned" and resume_decision == "force":
            return
        raise cli_errors.CLIError(resume_checks._resume_gate_refusal(gate))

    def _live_prefixes(self) -> frozenset[str]:
        """Liveness scan seam (tests inject here, never at machine state)."""

        return session_facts._live_background_prefixes()

    def _proc_ids(self) -> frozenset[str]:
        """``/proc`` argv liveness seam (tests inject here, never at /proc)."""

        return sessions.proc_session_ids()


SMOKE_PROMPT = "Reply with the single word: ok"
SMOKE_MAX_TOKENS = 16


def default_qualify_transport(
    base_url: str, token: str, alias: str, *,
    owner_check: Callable | None = None,
    deadline: float = operator_mod.SMOKE_DEADLINE_SECONDS,
    max_bytes: int = operator_mod.SMOKE_MAX_BYTES,
    clock: Callable[[], float] = time.monotonic,
) -> operator_mod.SmokeOutcome:
    """One bounded ``POST /v1/messages`` to the loopback gateway.

    The endpoint comes from ``endpoint.py`` (loopback http only) and the
    token goes out only after the listener-ownership check
    (``launch.gateway_authorization``). ``http.client`` never follows a
    redirect, so a 3xx is a failed smoke. One minimal generation, no retry,
    a total wall-clock deadline (every socket read is bounded by what is
    left of it) and a cumulative byte cap; either cap makes the verdict
    ``degenerate``. ``max_tokens`` is sent but never trusted as a bound.
    Nothing of the prompt, body or headers is kept.
    """

    import http.client
    import json
    import socket
    import threading
    import urllib.parse

    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None:
        raise cli_errors.CLIError("smoke: the gateway base_url is not loopback http")
    headers = launch.gateway_authorization(base_url, token, owner_check=owner_check)
    body = json.dumps({
        "model": alias, "max_tokens": SMOKE_MAX_TOKENS, "stream": False,
        "messages": [{"role": "user", "content": SMOKE_PROMPT}],
    }).encode("utf-8")
    start = clock()

    def left() -> float:
        return deadline - (clock() - start)

    status: int | None = None
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=max(0.001, deadline))
    control = None
    timer = None

    def cut() -> None:
        # The total wall-clock cap: shutting the shared connection down ends
        # any read in progress (a slow drip cannot outlive the deadline).
        try:
            if control is not None:
                control.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    try:
        connection.request("POST", "/v1/messages", body=body, headers={
            **headers, "content-type": "application/json", "anthropic-version": "2023-06-01",
        })
        if connection.sock is not None:
            control = connection.sock.dup()
            connection.sock.settimeout(max(0.001, left()))
        timer = threading.Timer(max(0.0, left()), cut)
        timer.daemon = True
        timer.start()
        response = connection.getresponse()
        status = response.status
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read1(min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                return operator_mod.classify_smoke(status, b"", overflow=True)
            if left() <= 0:
                return operator_mod.classify_smoke(status, b"", timed_out=True)
        if left() <= 0:
            return operator_mod.classify_smoke(status, b"", timed_out=True)
        return operator_mod.classify_smoke(status, b"".join(chunks))
    except (socket.timeout, TimeoutError):
        return operator_mod.classify_smoke(status, b"", timed_out=True)
    except (OSError, http.client.HTTPException) as exc:
        if left() <= 0:
            return operator_mod.classify_smoke(status, b"", timed_out=True)
        return operator_mod.SmokeOutcome("fail", status, f"the request failed ({type(exc).__name__})")
    finally:
        if timer is not None:
            timer.cancel()
        if control is not None:
            control.close()
        connection.close()


def operator_effective(
    eff: settings_mod.Effective, snapshot: operator_mod.OperatorSnapshot, *, docs: dict[str, Any] | None = None,
) -> settings_mod.Effective:
    """Separate optional digest-bound badges from current route usability.

    Every valid operator line is checked, admitted or not. The route map
    is in-memory only; record-authority compiles keep their launch facts.
    """

    layer = snapshot.layer
    lapsed = {
        key for key in eff.admitted_lines
        if key in layer.lines and not operator_mod.operator_line_admitted(
            key, layer=layer, ledger=snapshot.ledger, admitted_lines=eff.admitted_lines)
    }
    unavailable = dict(eff.unavailable_lines)
    for key, line in layer.lines.items():
        if not operator_mod.operator_line_offered(
                key, layer=layer, ledger=snapshot.ledger, provider_enabled=True):
            status = layer.route_status.get(line.provider_id, "unapproved")
            unavailable[key] = (f"provider {line.provider_id}: route {status}; route approval required — "
                                f"claude-multi providers approve {line.provider_id}")
    if docs is not None:
        lines = (docs["models-v2"] if "models-v2" in docs else docs["models"])["models"]
        for pid, selected in operator_mod.transport_selections(docs, snapshot.ledger).items():
            for key, entry in lines.items():
                if entry["provider"] != pid:
                    continue
                problem = selected.problem
                if problem is None and selected.alternative is not None and selected.alternative.explicit_models:
                    if catalog.key_route(entry) is None:
                        problem = (f"line {key} is not served by the selected {pid} {selected.choice} transport — "
                                   f"choose a reviewed line or claude-multi providers transport {pid} oauth-pool")
                if problem is not None:
                    unavailable[key] = problem
    return dataclasses.replace(eff, admitted_lines=frozenset(eff.admitted_lines - lapsed),
                               unavailable_lines=unavailable)


def prepared_workflow_binding(prepared: cli_types.PreparedLaunch) -> dict[str, str] | None:
    """The Settings binding this plan actually compiled, not today's choice."""

    return ((prepared.record or {}).get("applied", {}).get("settings", {}).get("workflow_default_binding"))


def prepared_line_keys(prepared: cli_types.PreparedLaunch, lcat: profile_mod.LineupCatalog) -> tuple[str, ...]:
    """Every explicitly selected line capable of sending a request."""

    if prepared.lineup is None:
        return ()
    keys = [prepared.lineup.lead.binding.key, *(a.binding.key for a in prepared.lineup.agents.values())]
    binding = prepared_workflow_binding(prepared)
    if binding is not None:
        try:
            key = lcat.resolve_key(binding["model"]).key or binding["model"]
        except catalog.CatalogError:
            key = binding["model"]
        keys.append(key)
    return tuple(dict.fromkeys(keys))


def launch_route_digests(snapshot: operator_mod.OperatorSnapshot, docs: dict[str, Any],
                         keys: Iterable[str]) -> dict[str, str]:
    """Non-secret route identities, independent of attestations and evidence."""

    selected = operator_mod.transport_selections(docs, snapshot.ledger)
    lines = (docs["models-v2"] if "models-v2" in docs else docs["models"])["models"]
    providers = docs["providers"]["providers"]
    result = {}
    for key in keys:
        if key not in lines:
            continue
        pid = lines[key]["provider"]
        route = selected.get(pid)
        result[key] = strict_json.bundle_digest({
            "provider": pid, "transport": providers[pid]["transport"],
            "choice": route.choice if route is not None else operator_mod.TRANSPORT_POOL,
            "route": route.alternative.route_digest if route is not None and route.alternative is not None else None,
        })
    return result


def operator_launch_digests(
    snapshot: operator_mod.OperatorSnapshot, keys: Iterable[str],
) -> dict[str, str]:
    """The current definition digests of the operator lines among ``keys``."""

    return {key: snapshot.layer.lines[key].definition_digest for key in keys if key in snapshot.layer.lines}


@dataclass(frozen=True)
class _SecretModel:
    model: str


@dataclass(frozen=True)
class _SecretAdapter:
    """The ``resolved`` shape ``proxy.selected_secret_problems`` reads."""

    lead: _SecretModel
    variants: tuple[_SecretModel, ...]


def _semantic_applied(applied: dict[str, Any]) -> Any:
    """The intent a generation number tracks (§6.4 ``semantic_changed``)."""

    return (
        (applied["lead"]["key"], applied["lead"]["effort"]),
        tuple(sorted((rid, b["key"], b["effort"]) for rid, b in applied["agents"].items())),
        tuple(sorted(applied["native_agents"].items())),
        applied["workflows"],
        tuple(applied["lead_providers"] or ()),
        tuple(sorted((applied.get("settings_overrides") or {}).items())),
        bool(applied["no_subagents"]),
    )


def _semantic_lineup(lineup: profile_mod.ResolvedLineup, no_subagents: bool) -> Any:
    bindings = lineup.applied_bindings()
    return (
        (bindings["lead"]["key"], bindings["lead"]["effort"]),
        tuple(sorted((rid, b["key"], b["effort"]) for rid, b in bindings["agents"].items())),
        tuple(sorted(lineup.native_agents.items())),
        lineup.workflows,
        tuple(lineup.lead_providers or ()),
        tuple(sorted(dict(lineup.settings_overrides).items())),
        bool(no_subagents),
    )


def _restated_document(
    lineup: profile_mod.ResolvedLineup, profile_name: str | None
) -> dict[str, Any]:
    """The profile document a committed record restates (``applied_document`` shape).

    Resolved keys only (retired keys and named bindings already applied), plus
    ``primary_provider`` (which ``applied`` carries).
    """

    bindings = lineup.applied_bindings()
    document: dict[str, Any] = {
        "version": 2,
        "name": profile_name or "ad-hoc",
        "description": "",
        "lead": {"model": bindings["lead"]["key"], "effort": bindings["lead"]["effort"]},
        "agents": {
            rid: {"model": b["key"], "effort": b["effort"]}
            for rid, b in bindings["agents"].items()
        },
        "native_agents": dict(lineup.native_agents),
        "workflows": lineup.workflows,
    }
    if lineup.lead_providers:
        document["lead_providers"] = list(lineup.lead_providers)
    if lineup.primary_provider is not None:
        document["primary_provider"] = lineup.primary_provider
    if lineup.settings_overrides:
        document["settings_overrides"] = dict(lineup.settings_overrides)
    return document


def _transition_module() -> Any:
    """Lane D's transition engine, imported lazily.

    The rest of the CLI must never depend on its presence; when it is absent
    the command fails closed with an actionable message rather than a
    traceback.
    """

    try:
        from claude_multi import transition
    except ImportError as exc:
        raise cli_errors.CLIError(
            "session transitions are unavailable in this build: "
            "claude_multi.transition is not installed"
        ) from exc
    return transition


def _packaged_contract(runtime: Runtime) -> dict[str, Any]:
    """The installed baseline contract (asset root), pre-override."""

    return strict_json.load(runtime.asset_root / "catalog" / "native-contract.json")
