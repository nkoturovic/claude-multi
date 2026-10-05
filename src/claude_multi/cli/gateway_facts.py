"""Gateway observations for doctor and the screens.

An observation module, not a pure view: explicitly invoked filesystem,
gateway and journal reads. Moved verbatim from the CLI module.
"""

from __future__ import annotations

from claude_multi import account_pools
from claude_multi import continuity as continuity_mod
from claude_multi import custom
from claude_multi import operator as operator_mod
from claude_multi import discovery
from claude_multi import paths
from claude_multi import errors as cli_errors
from claude_multi import launch
from claude_multi import gateway_events
from claude_multi import proxy as proxy_mod
from claude_multi import quota
from claude_multi import render
from claude_multi import secret_store
from claude_multi import service
from claude_multi import settings as settings_mod
from claude_multi import state
from claude_multi.platform import file_log
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Mapping
import claude_multi.cli.text as cli_text
import json
import os
import re
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any
    from typing import Callable
    import claude_multi.cli.runtime as runtime_mod


def _ordinary_unavailable(runtime: runtime_mod.Runtime) -> dict[str, str]:
    """Provider -> unavailable reason for ordinary launch marking.

    Same rule the renderer applies when omitting providers from the gateway
    config (render.unavailable_providers), resolved against the live secret
    env file — the picker's dimmed rows and the CLI warning can never drift
    from what the gateway actually serves. A malformed, unreadable, or
    unsafe secret env file resolves nothing: every direct provider is then
    marked with one static file-level reason (never the parser's message)
    rather than crashing an advisory preflight.
    """

    providers = runtime.effective_transport_docs()["providers"]["providers"]
    try:
        entries = render.unavailable_providers(
            providers,
            resolve_secret=lambda name: proxy_mod.resolve_secret(
                name, environ=runtime.environ
            ),
        )
    except (proxy_mod.ProxyError, UnicodeDecodeError):
        return {
            provider_id: cli_text.DIRECT_SECRET_FILE_ERROR
            for provider_id in sorted(providers)
            if providers[provider_id]["transport"]["kind"] == "direct"
            or render.is_keyed_compat(providers[provider_id])
        }
    return {entry["provider"]: entry["reason"] for entry in entries}


def _line_selectors(line: Mapping[str, Any]) -> list[str]:
    """Every ``/model`` selector of one v2 catalog line (top-level or per effort)."""

    efforts = line.get("efforts")
    selectors = [
        spec["selector"]
        for spec in (efforts.values() if isinstance(efforts, Mapping) else ())
        if isinstance(spec, Mapping) and spec.get("selector")
    ]
    if line.get("selector"):
        selectors.append(line["selector"])
    return sorted(set(selectors))


def _provider_kind_label(kind: str) -> str:
    """Short transport label for provider rows."""

    if kind == "oauth-pool":
        return "OAuth pool"
    if kind == "direct-openai":
        return "keyless LAN"
    return "direct key"


def _connect_hint(runtime: runtime_mod.Runtime, provider_id: str) -> str:
    """Exact per-provider connect instruction — names/paths, no values."""

    provider = runtime.effective_transport_docs()["providers"]["providers"][provider_id]
    transport = provider["transport"]
    if transport["kind"] == "oauth-pool":
        pool = transport["pool"]
        return f"run `{sign_in_command(pool)}` (account sign-in)"
    if transport["kind"] == "direct-openai" and not render.is_keyed_compat(provider):
        return (
            f"keyless LAN route at {transport['base_url']} (no credential; "
            "the server must be up — no key to set here)"
        )
    name = transport["auth"]["secret_ref"].removeprefix("env:")
    path = proxy_mod.secret_env_path(runtime.environ)
    return (
        f"set {name} in {path} (G providers → K offers masked entry), then "
        + cli_text.APPLY_GATEWAY_COMMAND
    )


@dataclass(frozen=True)
class ProviderFacts:
    """Provider rows plus the two pane-level gateway facts."""

    facts: list[dict[str, Any]]
    config_drift: bool | None
    gateway_down: bool


def _provider_facts(runtime: runtime_mod.Runtime) -> ProviderFacts:
    """Per-provider local status facts: names and counts only.

    Shared by the providers screen and the line-mode listing so both tell
    the same story. No provider calls; secret values never read into facts.
    ``config_drift``/``gateway_down`` ride along so the pane can explain
    counts that the running daemon can't.

    Per row also: ``enabled`` (Settings;
    ``settings.provider_enabled``, every provider on when settings.json is
    unreadable), ``models`` (how many merged v2 lines name the provider, New
    and custom included), ``source`` (``custom``/``catalog``), ``pool`` and
    ``records`` for OAuth pools, and ``selectors`` (the rendered aliases,
    which attribute the journal quota fact). It never reads the journal.
    """

    merged = runtime.ordinary_docs
    snapshot = runtime.operator_snapshot()
    selections = operator_mod.transport_selections(merged, snapshot.ledger)
    merged = operator_mod.apply_transports(merged, selections)
    providers = merged["providers"]["providers"]
    # The served-set authority reads the v2 lines (New included, as the
    # gateway serves them) plus the same continuity set doctor expects.
    models = merged["models-v2"]["models"]
    custom_registry = custom.load_registry(runtime.environ)
    unavailable = _ordinary_unavailable(runtime)
    try:
        token = runtime.gateway_token()
    except launch.LaunchError:
        token = None
    if token is not None:
        snap = _gateway_snapshot(runtime, token)
    else:
        snap = GatewaySnapshot(
            served=None,
            gateway_down=True,
            models_status=None,
            expected=frozenset(),
            oauth_alias_pools={},
            render_error=None,
            config_drift=None,
            config_note=None,
            oauth_records=_oauth_credential_records(runtime),
            continuity_map=dict(_doctor_continuity(runtime)[0]),
        )
    continuity_map = snap.continuity_map
    try:
        eff: settings_mod.Effective | None = runtime.current_effective()
    except cli_errors.CLIError:
        eff = None
    # Route status and operator line counts from the same snapshot
    # the render uses (a lines-only file keeps the catalog provider origin).
    operator_layer = runtime.operator_snapshot().layer
    # The render plan's retained operator captures, so a
    # captured-only provider's expected set matches the render and doctor.
    try:
        captures: Mapping[str, Mapping[str, Any]] = runtime.operator_render_plan().captures
    except (render.RenderError, proxy_mod.ProxyError, operator_mod.OperatorError, OSError, ValueError):
        captures = {}
    facts: list[dict[str, Any]] = []
    for provider_id in sorted(providers):
        provider = providers[provider_id]
        transport = provider["transport"]
        kind = transport["kind"]
        pool = None
        records = None
        if kind == "oauth-pool":
            pool = transport["pool"]
            records = snap.oauth_records.get(pool, 0)
            credential = (
                f"{records} credential record" + ("s" if records != 1 else "")
            )
            guidance = f"sign in: `{sign_in_command(pool)}`"
            expected = render.provider_selectors(
                provider_id, provider, models,
                continuity=continuity_map, providers=providers, captures=captures,
            )
        elif kind == "direct-openai" and not render.is_keyed_compat(provider):
            # Keyless LAN route: no secret exists, so the keyed branch
            # below cannot apply (a keyed compat route takes it).
            credential = "keyless LAN"
            guidance = f"server must be up at {transport['base_url']}"
            expected = render.provider_selectors(
                provider_id, provider, models,
                continuity=continuity_map, providers=providers, captures=captures,
            )
        else:
            secret_ref = transport["auth"]["secret_ref"]
            name = secret_ref.removeprefix("env:")
            reason = unavailable.get(provider_id)
            if reason is None:
                credential = f"{name} present"
            elif reason == f"missing required secret {secret_ref}":
                credential = f"{name} missing"
            elif reason.startswith(f"unusable required secret {secret_ref}"):
                credential = f"{name} invalid"  # blank or invalid; value never shown
            else:
                credential = reason
            guidance = f"set {name} (masked) with K"
            # One auth.kind-aware availability rule (a keyed compat
            # route needs a usable value, exactly as the render does).
            expected = render.provider_selectors(
                provider_id, provider, models, available=reason is None,
                continuity=continuity_map, providers=providers, captures=captures,
            )
        if snap.served is None:
            served_note = "unknown" if snap.gateway_down else "unknown (non-200)"
        elif not expected:
            served_note = "—"  # never a meaningless 0/0
        else:
            served_note = f"{len(expected & snap.served)}/{len(expected)} served"
        # A provider with no catalog/custom lines stays configured
        # (credential status is independent of the render).
        if any(line["provider"] == provider_id for line in models.values()):
            models_note = None
        else:
            continuity_count = len(
                render.provider_selectors(
                    provider_id, provider, {}, continuity=continuity_map,
                    providers=providers,
                )
                - render.provider_selectors(
                    provider_id, provider, {}, continuity={}, providers=providers,
                )
            )
            # Retained operator captures keep a provider with no
            # current lines rendered (with a usable key); say so, counted
            # like the continuity aliases (render precedence, not availability).
            captured_count = len(
                render.provider_selectors(
                    provider_id, provider, models, continuity=continuity_map,
                    providers=providers, captures=captures,
                )
                - render.provider_selectors(
                    provider_id, provider, models, continuity=continuity_map,
                    providers=providers,
                )
            ) if captures else 0
            models_note = "configured · no models" + (
                f" · {continuity_count} continuity aliases" if continuity_count else ""
            ) + (
                f" · {captured_count} captured aliases" if captured_count else ""
            )
        is_custom = provider_id in custom_registry["providers"]
        # The provider's explicit origin, never
        # custom-registry membership alone; lines-only files keep "catalog".
        origin = "custom" if is_custom else ("operator" if provider.get("origin") == "operator" else "catalog")
        fact: dict[str, Any] = {
            "id": provider_id,
            "display": provider["display"],
            "kind": kind,
            # The auth mode, so views never infer "keyless" from a kind.
            "auth": (transport.get("auth") or {}).get("kind", "none") if kind != "oauth-pool" else "oauth",
            "custom": is_custom,
            "credential": credential,
            "expected": len(expected),
            "served_note": served_note,
            "models_note": models_note,
            "guidance": guidance,
            "enabled": True if eff is None else settings_mod.provider_enabled(eff, provider_id),
            "models": sum(1 for line in models.values() if line["provider"] == provider_id),
            "source": origin,
            "selectors": sorted(expected),
        }
        if pool is not None:
            fact["pool"] = pool
            fact["records"] = records
        operator_lines = sum(1 for line in operator_layer.lines.values() if line.provider_id == provider_id)
        selection = selections.get(provider_id)
        fact["transport_label"] = (f"transport: {selection.choice}" if selection else f"transport: {kind}")
        if selection is not None and selection.problem:
            fact["transport_label"] += f" — {selection.problem}"
        if origin == "operator":
            fact["route"] = operator_layer.route_status.get(provider_id, "unapproved")
        if origin == "operator" or operator_lines:
            fact["operator_lines"] = operator_lines
        facts.append(fact)
    return ProviderFacts(
        facts,
        config_drift=snap.config_drift,
        gateway_down=snap.gateway_down,
    )


@dataclass(frozen=True)
class GatewaySnapshot:
    """One shared read of local gateway state (doctor and the providers screen).

    Loopback probe + names-only filesystem facts; no provider call, and no
    secret or auth-file contents. ``served`` is None when the daemon is down
    or answered non-200; ``config_drift`` is None when the on-disk config is
    unreadable; ``oauth_records`` maps pool -> credential-record count.
    ``sentinel`` is the fresh render's reload-sentinel alias,
    ``key_problem`` names an unusable gateway key slot (never its value),
    and ``rotation_phase`` is set when the on-disk config is a legal
    intermediate token-rotation render (Attention, never drift).
    Continuity: ``continuity_aliases`` are the aliases the
    fresh render emitted from continuity, ``continuity_status`` is
    ``persisted``, ``absent`` or ``corrupt: <reason>``,
    ``continuity_state_root`` the root the persisted set was extended from,
    and ``continuity_map`` the alias map the render used (the persisted set;
    the seed set when absent or corrupt — the renderer's own fallback).
    """

    served: frozenset[str] | None
    gateway_down: bool
    models_status: int | None
    expected: frozenset[str]
    oauth_alias_pools: dict[str, str]
    render_error: str | None
    config_drift: bool | None
    config_note: str | None
    oauth_records: dict[str, int]
    sentinel: str | None = None
    key_problem: str | None = None
    rotation_phase: str | None = None
    continuity_aliases: frozenset[str] = frozenset()
    continuity_status: str = "absent"
    continuity_state_root: str | None = None
    continuity_map: dict[str, Any] = field(default_factory=dict)
    # Overlay precedence conflicts — doctor Attention input.
    overlay_conflicts: tuple[Any, ...] = ()
    # The same /v1/models read with its bounded metadata (id,
    # owned_by, created); empty when the gateway is down or non-200.
    served_entries: tuple[Any, ...] = ()
    routes: dict[str, tuple] = field(default_factory=dict)


def _doctor_continuity(runtime: runtime_mod.Runtime) -> tuple[dict[str, Any], str, str | None]:
    """(alias map, status, state_root) — doctor's single continuity read.

    Doctor never extends from records and never writes: the expected render
    uses the persisted set, so adding or deleting records never moves its
    bytes. Absent (lazy state) and corrupt both fall back to the seed set,
    which is exactly what the renderer serves in those cases.
    """

    try:
        persisted = continuity_mod.read(runtime.home)
    except continuity_mod.ContinuityError as exc:
        return continuity_mod.seed_only(runtime.catalog)["aliases"], f"corrupt: {exc}", None
    if persisted is None:
        return continuity_mod.seed_only(runtime.catalog)["aliases"], "absent", None
    return persisted["aliases"], "persisted", persisted["state_root"]


# The sign-in command a remedy names for each OAuth pool: the public verb,
# which takes the personal-use confirmation (the gateway tool's own login
# commands refuse without a current one).
_OAUTH_LOGIN_COMMANDS = {name: f"claude-multi providers sign-in {pool.provider}"
                         for name, pool in account_pools.pools().items()}
SIGN_IN_ANY = "claude-multi providers sign-in " + "|".join(
    pool.provider for pool in account_pools.pools().values())


def sign_in_command(pool: str) -> str:
    """The public sign-in command for ``pool`` (both, for a pool it does not know)."""

    return _OAUTH_LOGIN_COMMANDS.get(pool, SIGN_IN_ANY)


def _oauth_credential_records(runtime: runtime_mod.Runtime) -> dict[str, int]:
    """Pool -> credential-record count (names-only, symlink-refusing)."""

    gateway_info = runtime.catalog.docs["gateway"]["gateway"]
    providers = runtime.catalog.docs["providers"]["providers"]
    pools = sorted(
        {
            provider["transport"]["pool"]
            for provider in providers.values()
            if provider["transport"]["kind"] == "oauth-pool"
        }
    )
    counts = {pool: 0 for pool in pools}
    home = Path(runtime.environ.get("HOME") or Path.home())
    try:
        entries = list((home / gateway_info["auth_dir"]).iterdir())
    except OSError:
        return counts
    table = account_pools.load()
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            continue
        pool = table.classify(entry.name)
        if pool in counts:
            counts[pool] += 1
    return counts


def oauth_record_observation(runtime: runtime_mod.Runtime) -> dict[str, int] | None:
    """Pool -> credential-record count, or None when the auth
    directory is absent or unreadable (an unknown observation, never "no
    records": a no-credential home is a valid readiness input)."""

    gateway_info = runtime.catalog.docs["gateway"]["gateway"]
    home = Path(runtime.environ.get("HOME") or Path.home())
    directory = home / gateway_info["auth_dir"]
    try:
        info = os.lstat(directory)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    try:
        list(directory.iterdir())
    except OSError:
        return None
    return _oauth_credential_records(runtime)


def _gateway_snapshot(runtime: runtime_mod.Runtime, token: str) -> GatewaySnapshot:
    """Collect the shared snapshot; each fact degrades independently."""

    if runtime._report_cache is not None and "gateway-snapshot" in runtime._report_cache:
        return runtime._report_cache["gateway-snapshot"]
    routes = {}
    served_entries: tuple[launch.ServedModel, ...] = ()
    try:
        # One /v1/models read per refresh, metadata included.
        entries, models_status = runtime.served_snapshot(token)
        served = None if entries is None else {entry.id for entry in entries}
        served_entries = entries or ()
        down = False
    except launch.LaunchError:
        served, down, models_status = None, True, None
    home = Path(runtime.environ.get("HOME") or Path.home())
    expected: frozenset[str] = frozenset()
    alias_pools: dict[str, str] = {}
    render_error: str | None = None
    drift: bool | None = None
    config_note: str | None = None
    sentinel: str | None = None
    key_problem: str | None = None
    rotation_phase: str | None = None
    continuity_rendered: frozenset[str] = frozenset()
    continuity_map, continuity_status, continuity_state_root = _doctor_continuity(runtime)
    try:
        # The same key-slot authority as the gateway's own render:
        # a published dual-key window compares equal, never as drift.
        keys: tuple[str, ...] = proxy_mod.gateway_api_keys(home)
    except proxy_mod.ProxyError as exc:
        key_problem = str(exc)
        keys = (token,)
    overlay_conflicts: tuple[render.OverlayConflict, ...] = ()
    try:
        # build_config_document already appends the reload sentinel; the
        # drift compare must use that exact emit (never finalize twice).
        # The operator render plan is the proxy's own (same inputs,
        # same bytes): route-filtered providers, captures, headers, overlay.
        plan = runtime.operator_render_plan()
        docs = plan.docs
        overlay_static = proxy_mod.overlay_static_wires(runtime.environ, runtime.asset_root)
        rendered_holder: list[tuple[str, ...]] = []
        conflicts_holder: list[tuple[render.OverlayConflict, ...]] = []

        def build(tokens: tuple[str, ...]) -> dict[str, Any]:
            document, _available, _unavailable, info = render.build_config_document(
                docs["gateway"],
                docs["providers"]["providers"],
                docs["models-v2"]["models"],
                home=home,
                gateway_tokens=tokens,
                resolve_secret=lambda name: proxy_mod.resolve_secret(
                    name, environ=runtime.environ
                ),
                continuity=continuity_map,
                captures=plan.captures,
                provider_headers=plan.headers,
                oauth_overlay=plan.overlay,
                overlay_static=overlay_static,
                quiet_providers=plan.quiet_providers,
                retired=(docs.get("retired") or {}).get("retired"),
            )
            rendered_holder.append(info["continuity_rendered"])
            conflicts_holder.append(info["overlay_conflicts"])
            return document

        document = build(keys)
        continuity_rendered = frozenset(rendered_holder[0])
        overlay_conflicts = conflicts_holder[0]
        sentinel = render.document_sentinel(document)
        expected = render.rendered_selectors(document)
        # Project the very render whose sentinel was checked, including retained
        # operator captures. No header, URL or credential enters the route map.
        from claude_multi import routing
        aliases = {**continuity_map, **{key: {**value, "route_origin": "operator-capture"}
                                       for key, value in plan.captures.items()}}
        routes = {selector: routing.current_route(selector, docs, aliases) for selector in expected}
        alias_pools = {
            entry["alias"]: pool
            for pool, entries in document.get("oauth-model-alias", {}).items()
            for entry in entries
        }
        try:
            on_disk = state.read_private(proxy_mod.config_dir(home) / "config.yaml")
            # Byte equality only — the deterministic render makes drift
            # detection exact; contents (which carry secrets) are compared,
            # never displayed.
            fresh = render.emit_yaml(document)
            drift = on_disk != fresh.encode("utf-8")
            if drift:
                phase = _rotation_phase(home, on_disk, build)
                if phase is not None:
                    # The gateway should serve the on-disk phase render, so
                    # its sentinel is the one to expect (never a false
                    # "did not reload").
                    rotation_phase, sentinel = phase
                    drift = False
        except (OSError, ValueError) as exc:
            drift = None
            config_path = proxy_mod.config_dir(home) / "config.yaml"
            # Only an existing-but-unreadable file is noteworthy (a wrong
            # mode silently disables the radar); a missing one is the
            # never-initialized state, which readiness already covers.
            if config_path.exists():
                config_note = f"on-disk gateway config unreadable for the drift check: {exc}"
    except (
        render.RenderError,
        proxy_mod.ProxyError,
        UnicodeDecodeError,
        OSError,
        ValueError,
    ) as exc:
        render_error = str(exc)
    snapshot = GatewaySnapshot(
        served=None if down else served,
        models_status=models_status,
        gateway_down=down,
        expected=expected,
        oauth_alias_pools=alias_pools,
        render_error=render_error,
        config_drift=drift,
        oauth_records=_oauth_credential_records(runtime),
        config_note=config_note,
        sentinel=sentinel,
        key_problem=key_problem,
        rotation_phase=rotation_phase,
        continuity_aliases=continuity_rendered,
        continuity_status=continuity_status,
        continuity_state_root=continuity_state_root,
        continuity_map=dict(continuity_map),
        overlay_conflicts=overlay_conflicts,
        served_entries=() if down else tuple(served_entries),
        routes=routes,
    )
    if runtime._report_cache is not None:
        runtime._report_cache["gateway-snapshot"] = snapshot
    return snapshot


def _rotation_phase(
    home: Path,
    on_disk: bytes,
    build: Callable[[tuple[str, ...]], dict[str, Any]],
) -> tuple[str, str] | None:
    """(phase, sentinel) when the on-disk config is a paused rotation render.

    Only while ``previous-key`` exists, a rotation paused
    mid-phase leaves a config rendered from a key list other than the
    slots' steady ``gateway_api_keys`` — ``[previous, candidate]`` before
    publication (the candidate was never published) or ``[current]`` while
    retiring. Each legal phase is re-rendered exactly (same catalog and
    secrets) and byte-compared; anything else is real drift. Keys are
    compared, never displayed.
    """

    directory = proxy_mod.config_dir(home)
    if not os.path.lexists(directory / "previous-key"):
        return None
    try:
        slots = proxy_mod.gateway_api_keys(home)
    except proxy_mod.ProxyError:
        return None
    if len(slots) == 2:
        candidates = [
            ((slots[-1],), "the gateway config holds the rotation's retirement phase"),
        ]
    else:
        match = re.search(
            rb'^api-keys:\n  - "([0-9a-f]{64})"\n  - "([0-9a-f]{64})"\n(?!  - )',
            on_disk,
            re.M,
        )
        if match is None or match.group(1).decode("ascii") != slots[0]:
            return None
        candidates = [
            (
                (slots[0], match.group(2).decode("ascii")),
                "the gateway config holds the rotation's unverified dual-key "
                "phase (the candidate key was never published)",
            ),
        ]
    for keys, label in candidates:
        try:
            document = build(keys)
        except (render.RenderError, proxy_mod.ProxyError, ValueError):
            return None
        if render.emit_yaml(document).encode("utf-8") == on_disk:
            return label, render.document_sentinel(document)
    return None


def gateway_down_banner(runtime: runtime_mod.Runtime | None = None) -> str:
    banner = "gateway is DOWN — served counts unknown; start it with " + gateway_service_hint("start", runtime)
    recover = gateway_service_hint("recover", runtime)
    if recover != gateway_service_hint("start", runtime):
        banner += "; if it failed: " + recover
    return banner


def gateway_service_hint(verb: str, runtime: runtime_mod.Runtime | None = None) -> str:
    """The one source of gateway remedy text for the screens and reports.

    ``service.hint`` chooses by the backend the runtime HOME's endpoint
    document records: the product's gateway verbs, plus service-manager text
    only where the supervised unit is installed.
    """

    return service.hint_code(verb, home=runtime.home if runtime is not None else None)


def _doctor_now() -> datetime:
    """Doctor's clock (a module seam: tests patch it)."""

    return datetime.now(timezone.utc)


def journal_command(runtime: runtime_mod.Runtime) -> tuple[str, ...] | None:
    """The supervised unit's journal command, or None: the on-demand gateway
    logs to its per-instance files (``claude-multi gateway logs``)."""

    backend = service.backend_of(runtime.home)
    if backend.name != service.SYSTEMD or backend.unit is None:
        return None
    return service.journal_argv(unit=backend.unit)


_JOURNAL_MAX_BYTES = 32 * 1024 * 1024


_JOURNAL_LINE = re.compile(
    r"^\[(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] \[(?P<rid>[^\]]*)\] \[[^\]]*\] "
    r"(?:\[[^\]]*\] )?(?P<msg>.*)$"
)


_JOURNAL_STATUS = re.compile(r"^(?P<code>[1-5]\d\d) \|")


_JOURNAL_MODEL = re.compile(r"\bmodel=(?P<model>[A-Za-z0-9._:/@\[\]-]{1,128})")


_JOURNAL_429_PER_HOUR = 20


def _read_gateway_journal(runtime: runtime_mod.Runtime, *, since: str = "-24h") -> gateway_events.LogWindow | None:
    """A bounded log window, or None for injected runtimes (tests patch this seam).

    Runs only for a runtime whose loopback seams are live (no injected
    ``health_get``/``served_models_callback``), so no test ever reads the
    host journal; bounded in time and bytes; stderr discarded.
    """

    if runtime.health_get is not None or runtime.served_models_callback is not None:
        return None
    backend = service.backend_of(runtime.home)
    if backend.name == service.SYSTEMD and backend.unit is not None:
        return service.read_gateway_journal(max_bytes=_JOURNAL_MAX_BYTES, since=since, unit=backend.unit)
    return file_log.read_recent(
        service.gateway_workdir(runtime.session_store.root) / service.GATEWAY_LOGS,
        since=file_log.since_instant(since, now=_doctor_now()), max_bytes=_JOURNAL_MAX_BYTES)


def _report_journal(runtime, *, since="-24h"):
    def read():
        window = (_read_gateway_journal(runtime) if since == "-24h" else
                  _read_gateway_journal(runtime, since=since))
        if isinstance(window, str):
            # Compatibility for test seams that inject a string window only.
            return gateway_events.collect(gateway_events.LogWindow(), providers=()), _journal_facts(runtime, window)
        events = gateway_events.collect(window or gateway_events.LogWindow(), providers={
            *runtime.pool_providers(), *runtime.ordinary_docs["providers"]["providers"],
        })
        return events, _event_journal_facts(runtime, events)
    return runtime.report_value(("journal", since), read)


def report_events(runtime, *, since="-24h") -> gateway_events.GatewayEvents:
    return _report_journal(runtime, since=since)[0]


def report_journal_facts(runtime) -> JournalFacts:
    return _report_journal(runtime)[1]


def _pool_credential_mtimes(runtime: runtime_mod.Runtime) -> dict[str, float]:
    """Pool -> newest credential-record mtime (names/mtimes only; records
    told apart by the pools' collision-free file-name classification)."""

    gateway_info = runtime.catalog.docs["gateway"]["gateway"]
    pools = sorted(
        {
            provider["transport"]["pool"]
            for provider in runtime.catalog.docs["providers"]["providers"].values()
            if provider["transport"]["kind"] == "oauth-pool"
        }
    )
    try:
        entries = list((runtime.home / gateway_info["auth_dir"]).iterdir())
    except OSError:
        return {}
    newest: dict[str, float] = {}
    table = account_pools.load()
    for entry in entries:
        try:
            info = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        pool = table.classify(entry.name)
        if pool in pools:
            newest[pool] = max(newest.get(pool, 0.0), info.st_mtime)
    return newest


SUBSTITUTION_FORMAT = "%s executor: upstream served model %q for requested model %q (auth_index=%s)"


_Q = r'"((?:[^"\\]|\\.)*)"'


_SUBSTITUTION = re.compile(r"^([A-Za-z0-9_-]+) executor: upstream served model " + _Q
                           + r" for requested model " + _Q)


@dataclass(frozen=True)
class Substitution:
    label: str
    served: str
    count: int


@dataclass(frozen=True)
class JournalFacts:
    """The journal facts: shared by doctor and the providers screen.

    ``dead_pools``: pool -> (invalid_grant lines since that pool's credential
    was written, the last one's local time).  ``unattributed``: invalid_grant
    lines naming no pool -> (count, last local time, candidate pools), or
    None.  ``quota``: (model alias, status code) -> count, holding exactly
    the pairs doctor reports (429 only within the last hour and at or above
    the hourly threshold; 402/403 within the journal window).  ``partial``
    is None for a complete bounded read of the 24 h window, else the text that
    qualifies every count (the oldest record read and the coverage).
    """

    dead_pools: Mapping[str, tuple[int, datetime]]
    unattributed: tuple[int, datetime, tuple[str, ...]] | None
    quota: Mapping[tuple[str, str], int]
    substitutions: tuple[Substitution, ...] = ()
    credential_saves: gateway_events.CredentialSaves | None = None
    partial: str | None = None


def _journal_facts(runtime: runtime_mod.Runtime, text: str | gateway_events.LogWindow | None) -> JournalFacts:
    """Parse CLIProxyAPI journal text into :class:`JournalFacts`.

    CLIProxyAPI's log lines are ``[YYYY-mm-dd HH:MM:SS] [<request id>]
    [<level>] [<file:line>] <message>`` (local time); a status line's
    message starts with ``<code> |``, a selector line carries
    ``model=<alias>`` under the same request id. Counts only; no body or
    message text is kept. Empty or None text gives empty facts.
    """

    if isinstance(text, gateway_events.GatewayEvents):
        return _event_journal_facts(runtime, text)
    if isinstance(text, gateway_events.LogWindow):
        return _event_journal_facts(runtime, gateway_events.collect(text, providers={
            *runtime.pool_providers(), *runtime.ordinary_docs["providers"]["providers"],
        }))
    if not text:
        return JournalFacts(dead_pools={}, unattributed=None, quota={})
    window = text if isinstance(text, gateway_events.LogWindow) else gateway_events.LogWindow(
        tuple(gateway_events.LogRecord(line) for line in text.splitlines()[:gateway_events.MAX_RECORDS]),
        "incomplete",
    )
    saves = gateway_events.collect_credential_saves(window, providers={
        provider["transport"]["pool"]
        for provider in runtime.catalog.docs["providers"]["providers"].values()
        if provider["transport"]["kind"] == "oauth-pool"
    })
    now = _doctor_now()
    local_now = now.astimezone().replace(tzinfo=None)
    request_model: dict[str, str] = {}
    statuses: list[tuple[datetime, str, str]] = []
    grants: list[tuple[datetime, str]] = []
    substitutions: dict[tuple[str, str], int] = {}
    for record in window.records:
        raw_line = record.message
        # Receipt-shaped lines (including malformed/path-bearing ones) never
        # fall through to the older permissive journal pattern recognizers.
        if "credential_save_v1" in raw_line:
            continue
        match = _JOURNAL_LINE.match(raw_line)
        if match is None:
            continue
        try:
            when = datetime.strptime(match["ts"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        rid, message = match["rid"], match["msg"]
        substitution = _SUBSTITUTION.match(message)
        if substitution is not None:
            def unquote(value: str) -> str:
                try:
                    return json.loads('"' + value + '"')
                except ValueError:
                    return value
            served, requested = (unquote(value) for value in substitution.groups()[1:])
            pair = (requested, served)
            substitutions[pair] = substitutions.get(pair, 0) + 1
        model = _JOURNAL_MODEL.search(message)
        if model is not None and rid and set(rid) != {"-"}:
            request_model[rid] = model["model"]
        status = _JOURNAL_STATUS.match(message)
        if status is not None and status["code"] in {"402", "403", "429"}:
            statuses.append((when, rid, status["code"]))
        if "invalid_grant" in message:
            lowered = message.lower()
            pool = "codex" if "codex" in lowered else "claude" if (
                "claude" in lowered or "anthropic" in lowered
            ) else ""
            grants.append((when, pool))
    mtimes = _pool_credential_mtimes(runtime)

    def after_write(name: str, when: datetime) -> bool:
        since = mtimes.get(name)
        return since is None or when > datetime.fromtimestamp(since)

    dead_pools: dict[str, tuple[int, datetime]] = {}
    for pool in sorted({pool for _when, pool in grants if pool}):
        fresh = [
            when for when, grant_pool in grants if grant_pool == pool and after_write(pool, when)
        ]
        if fresh:
            dead_pools[pool] = (len(fresh), max(fresh))
    # A grant line naming no pool is ONE failure of an unknown pool: one fact
    # naming every pool whose credential predates it as a candidate, never
    # one "dead" pool per candidate.
    names = sorted(mtimes) or sorted(_OAUTH_LOGIN_COMMANDS)
    unattributed_times = [
        when for when, pool in grants if not pool and any(after_write(n, when) for n in names)
    ]
    unattributed = None
    if unattributed_times:
        possible = tuple(
            n for n in names if any(after_write(n, when) for when in unattributed_times)
        )
        unattributed = (len(unattributed_times), max(unattributed_times), possible)
    per_model: dict[tuple[str, str], int] = {}
    for when, rid, code in statuses:
        age = (local_now - when).total_seconds()
        if code == "429" and age > 3600:
            continue
        model = request_model.get(rid, "unattributed")
        per_model[(model, code)] = per_model.get((model, code), 0) + 1
    quota = {
        (model, code): count
        for (model, code), count in sorted(per_model.items())
        if not (code == "429" and count < _JOURNAL_429_PER_HOUR)
    }
    labels: dict[str, set[str]] = {}
    if substitutions:
        docs = runtime.ordinary_docs
        for collection, field in ((docs["models"]["models"], "wire_model"),
                                  (docs.get("retired", {}).get("retired", {}), "last_wire")):
            for key, entry in collection.items():
                labels.setdefault(entry[field], set()).add(key)
    substitution_facts = tuple(
        Substitution(
            f"{', '.join(sorted(labels[wire]))} ({wire})" if wire in labels else f"wire {wire}",
            served, count,
        )
        for (wire, served), count in sorted(substitutions.items())
    )
    partial = None
    if isinstance(text, gateway_events.LogWindow) and window.coverage in {"truncated", "incomplete"}:
        # A cap or a timeout cut the oldest part of the window (the reader runs
        # newest first); counts are lower bounds over what was actually read.
        stamps = [record.timestamp for record in window.records if record.timestamp is not None]
        partial = (f"partial journal read since {min(stamps).astimezone():%Y-%m-%d %H:%M}, "
                   f"coverage={window.coverage}") if stamps else f"partial journal read, coverage={window.coverage}"
    return JournalFacts(dead_pools=dead_pools, unattributed=unattributed, quota=quota,
                        substitutions=substitution_facts,
                        credential_saves=saves if saves.events or isinstance(text, gateway_events.LogWindow) else None,
                        partial=partial)


def _event_journal_facts(runtime, events: gateway_events.GatewayEvents) -> JournalFacts:
    """Doctor and Providers reuse the same minimized journal facts as reports."""
    now = _doctor_now()
    mtimes = _pool_credential_mtimes(runtime)
    def fresh(pool, when):
        return when is not None and (pool not in mtimes or when.timestamp() > mtimes[pool])
    dead = {}
    for pool in account_pools.names():
        times = [f.timestamp for f in events.oauth_failures if f.pool == pool and fresh(pool, f.timestamp)]
        if times:
            dead[pool] = (len(times), max(times).astimezone())
    names = sorted(mtimes) or sorted(_OAUTH_LOGIN_COMMANDS)
    unknown = [f.timestamp for f in events.oauth_failures if f.pool is None
               and any(fresh(n, f.timestamp) for n in names)]
    unattributed = (len(unknown), max(unknown).astimezone(), tuple(
        n for n in names if any(fresh(n, t) for t in unknown))) if unknown else None
    counts = {}
    for request in events.requests:
        if request.endpoint != "inference" or request.status not in {402, 403, 429}:
            continue
        if request.status == 429 and (request.timestamp is None or
                not 0 <= (now - request.timestamp).total_seconds() <= 3600):
            continue
        key = (request.selector or "unattributed", str(request.status))
        counts[key] = counts.get(key, 0) + 1
    counts = {k: v for k, v in counts.items() if k[1] != "429" or v >= _JOURNAL_429_PER_HOUR}
    substitutions = {}
    for route in events.routes:
        if route.requested and route.served:
            key = (route.requested, route.served)
            substitutions[key] = substitutions.get(key, 0) + 1
    labels: dict[str, set[str]] = {}
    if substitutions:
        docs = runtime.ordinary_docs
        for collection, field in ((docs["models"]["models"], "wire_model"),
                                  (docs.get("retired", {}).get("retired", {}), "last_wire")):
            for key, entry in collection.items():
                labels.setdefault(entry[field], set()).add(key)
    partial = None
    if events.source_coverage in {"truncated", "incomplete"} or events.timed_out:
        bound = f" since {events.oldest_at.astimezone():%Y-%m-%d %H:%M}" if events.oldest_at else ""
        partial = f"partial journal read{bound}, coverage={events.source_coverage}"
        if events.timed_out:
            partial += ", timed out"
    return JournalFacts(dead, unattributed, counts,
                        tuple(Substitution(f"{', '.join(sorted(labels[requested]))} ({requested})"
                                           if requested in labels else "wire " + requested, served, count)
                              for (requested, served), count in sorted(substitutions.items())),
                        events.credential_saves, partial)


# ------------------------------------------------------------------ LAN reachability
# A keyless LAN provider (openai-compatible-lan) is reachable only from some
# networks. Reachability is a network-scoped observation: one explicit
# short-timeout probe (name resolution, then one TCP connect), never a
# model request, never in the background, never BLOCK, never part of an
# evidence digest. Tests inject every result (NXDOMAIN, refused, timeout,
# reachable); no SSID or network detection.

LAN_PROBE_TIMEOUT = 1.5
LAN_UNREACHABLE_REMEDY = "connect to the host's network, or bind another model"
_LAN_REASONS = {
    "nxdomain": "does not resolve",
    "refused": "refused the connection",
    "timeout": "did not answer in time",
    "unreachable": "is unreachable",
}


@dataclass(frozen=True)
class LanReachability:
    """``state``: reachable | unreachable | unknown; ``reason`` names why
    (nxdomain, refused, timeout, unreachable) or why it was not checked."""

    state: str
    host: str
    reason: str | None = None

    def text(self) -> str:
        """The exact network-scoped message (Info, or a fast launch refusal)."""

        if self.state == "reachable":
            return f"reachable from this network: {self.host}"
        if self.state == "unreachable":
            return (f"not reachable from this network: {self.host} "
                    f"{_LAN_REASONS.get(self.reason or '', 'is unreachable')}")
        return f"reachability of {self.host} not checked"


def lan_host(url: str) -> str:
    import urllib.parse

    try:
        parts = urllib.parse.urlsplit(url)
        return parts.hostname or url
    except ValueError:
        return url


def probe_lan(
    url: str, *, timeout: float = LAN_PROBE_TIMEOUT,
    resolve: "Callable[[str, int], Any] | None" = None,
    connect: "Callable[[Any, float], Any] | None" = None,
) -> LanReachability:
    """One bounded reachability probe of ``url``'s host and port.

    Resolution runs on a daemon thread bounded by ``timeout`` (getaddrinfo
    has no timeout of its own); the connects share what is left. Every
    resolved address is tried in resolver order (its own family and socket
    address, so an IPv6 address without a listener before a working IPv4
    one is not a verdict) until one connects or the shared deadline ends;
    each attempt gets an even share of the time left over the untried
    candidates, so a first address that drops packets cannot use up a later
    address's turn. The host is unreachable only when every candidate
    failed. ``connect`` receives one ``getaddrinfo`` entry and its share in
    seconds. Nothing is sent: the socket closes right after the connect.
    """

    import socket
    import threading
    import time
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return LanReachability("unknown", host or url, "malformed port")
    if not host:
        return LanReachability("unknown", url, "no host")
    deadline = time.monotonic() + timeout
    resolver = resolve or (lambda name, number: socket.getaddrinfo(name, number, type=socket.SOCK_STREAM))
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["addresses"] = resolver(host, port)
        except BaseException as exc:  # handed to the caller below
            result["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return LanReachability("unreachable", host, "timeout")
    error = result.get("error")
    if error is not None:
        if isinstance(error, socket.gaierror):
            return LanReachability("unreachable", host, "nxdomain")
        if isinstance(error, (socket.timeout, TimeoutError)):
            return LanReachability("unreachable", host, "timeout")
        return LanReachability("unreachable", host, "unreachable")
    candidates: list[Any] = []
    for entry in result.get("addresses") or []:
        if not (isinstance(entry, tuple) and len(entry) == 5 and isinstance(entry[4], tuple)):
            continue
        if all(entry[4] != seen[4] for seen in candidates):
            candidates.append(entry)
    if not candidates:
        return LanReachability("unreachable", host, "nxdomain")
    def open_address(entry: Any, seconds: float) -> Any:
        """Connect to one ``getaddrinfo`` entry (its family, type, protocol
        and socket address), bounded by ``seconds``; the caller closes it."""

        family, socktype, proto, _canonical, address = entry
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(max(seconds, 0.001))
            sock.connect(address)
        except BaseException:
            sock.close()
            raise
        return sock

    opener = connect or open_address
    reasons: list[str] = []
    for index, entry in enumerate(candidates):
        left = deadline - time.monotonic()
        if left <= 0:
            if index:
                reasons.append("timeout")
                break
            share = 0.05  # the resolver used the budget: one minimal attempt
        else:
            share = left / (len(candidates) - index)
        try:
            sock = opener(entry, share)
        except ConnectionRefusedError:
            reasons.append("refused")
            continue
        except (socket.timeout, TimeoutError):
            reasons.append("timeout")
            continue
        except OSError:
            reasons.append("unreachable")
            continue
        try:
            close = getattr(sock, "close", None)
            if close is not None:
                close()
        except OSError:
            pass
        return LanReachability("reachable", host)
    # Every candidate failed: the most specific reason one of them gave.
    reason = next((r for r in ("refused", "timeout") if r in reasons), "unreachable")
    return LanReachability("unreachable", host, reason)


def lan_reachability(runtime: runtime_mod.Runtime, url: str) -> LanReachability:
    """The Runtime seam: an injected ``lan_probe``; with the gateway test
    seams injected and no probe, ``unknown`` (no test ever dials out)."""

    if runtime.lan_probe is not None:
        return runtime.lan_probe(url)
    if runtime.health_get is not None or runtime.served_models_callback is not None:
        return LanReachability("unknown", lan_host(url), "seam")
    return probe_lan(url)


# ------------------------------------------------------------------ discovery adapters
def discovery_known(
    runtime: runtime_mod.Runtime, *, expected: "Any" = (), continuity_map: "Mapping[str, Any] | None" = None,
) -> discovery.KnownIndex:
    """What the effective view already knows (catalog, legacy and operator
    lines with New ones, retired entries, passthrough routes, continuity
    and captured aliases, the rendered selectors) — the candidate filter."""

    snapshot = runtime.operator_snapshot()
    captures = snapshot.ledger.aliases if snapshot.ledger is not None else {}
    if continuity_map is None:
        continuity_map, _status, _root = _doctor_continuity(runtime)
    return discovery.known_index(runtime.ordinary_docs, continuity=continuity_map, captures=captures,
                                 expected=expected)


def pinned_registry(runtime: runtime_mod.Runtime) -> "Any":
    """The pinned registry of this build, or None (absent or unreadable)."""

    from claude_multi import catalog as catalog_mod

    root = catalog_mod.registry_dir(runtime.environ, runtime.asset_root)
    if root is None:
        return None
    try:
        return catalog_mod.load_pinned_registry(root)
    except catalog_mod.CatalogError:
        return None


def served_extras_info(runtime: runtime_mod.Runtime, snap: GatewaySnapshot) -> list[str]:
    """Doctor Info from the snapshot's own /v1/models read (no second call):
    unique routable, uncataloged ids per channel plus the unattributed ones."""

    if snap.served is None:
        return []
    entries = snap.served_entries or tuple(launch.ServedModel(item) for item in sorted(snap.served))
    known = discovery_known(runtime, expected=snap.expected, continuity_map=snap.continuity_map)
    by_channel, unattributed = discovery.served_extras(pinned_registry(runtime), entries, known)
    return discovery.doctor_info_lines(by_channel, unattributed)


# ----------------------------------------------------------- local files and gateway facts

# The management key files have their own report (with category-safe repairs).
_SECRET_FILES = ("api-key", "previous-key", "config.yaml", "endpoint.json")
_PROXY_VARIABLES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


def _entry_problem(path: Path, *, directory: bool, environ: Mapping[str, str]) -> str | None:
    """Mode, owner and symlink checks for one private entry (None when fine or absent)."""

    shown = paths.display(path, environ)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{shown} cannot be inspected ({exc.strerror or type(exc).__name__})"
    kind = "directory" if directory else "file"
    if stat.S_ISLNK(info.st_mode):
        return (f"{shown} is a symlink — claude-multi refuses symlinks for private state: "
                f"replace the symlink with the {kind} itself")
    if directory != stat.S_ISDIR(info.st_mode) or (not directory and not stat.S_ISREG(info.st_mode)):
        return f"{shown} is not a {'directory' if directory else 'regular file'}: move it aside"
    if info.st_uid != os.geteuid():
        return f"{shown} is owned by uid {info.st_uid}, not by you: chown it to your user (or move it aside)"
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        wanted = "700" if directory else "600"
        return f"{shown} has mode {mode:04o} (group/other access): chmod {wanted} {shown}"
    return None


def local_files_report(runtime: runtime_mod.Runtime) -> tuple[list[str], set[Path]]:
    """The config and state roots and the secret files: mode, owner, symlink
    and parse checks, before any gateway key check (a damaged root otherwise
    shows up as a misleading key error). Names only; no secret is read beyond
    the key's shape. Returns ``(problems, flagged paths)``."""

    environ = {**runtime.environ, "HOME": str(runtime.home)}
    gateway_dir = paths.gateway_config_dir(environ)
    problems: list[str] = []
    flagged: set[Path] = set()
    directories = dict.fromkeys([gateway_dir, paths.config_root(environ), runtime.session_store.root])
    for directory in directories:
        problem = _entry_problem(directory, directory=True, environ=environ)
        if problem is not None:
            problems.append(problem)
            flagged.add(directory)
    files = [gateway_dir / name for name in _SECRET_FILES]
    try:
        files.append(secret_store.secret_env_path(environ))
    except secret_store.SecretStoreError as exc:
        problems.append(f"{exc} — {exc.remedy}")
    for path in files:
        problem = _entry_problem(path, directory=False, environ=environ)
        if problem is not None:
            problems.append(problem)
            flagged.add(path)
    key = gateway_dir / "api-key"
    if key not in flagged and gateway_dir not in flagged and os.path.lexists(key):
        try:
            launch.read_gateway_token(runtime.catalog.docs["gateway"], home=runtime.home)
        except launch.GatewayKeyError as exc:
            problems.append(f"{paths.display(key, environ)}: {exc} — {exc.remedy}")
            flagged.add(key)
    return problems, flagged


class Finding(str):
    """A doctor line that carries its typed fields: as a ``str`` it is the
    line plain doctor prints; ``code`` (stable, kebab-case), ``public`` (the
    text the JSON report may carry: no path, value or exception body), an
    optional safe ``subject_id`` and one ``remedy`` line become the report's
    diagnostic instead of the redacted fallback."""

    code: str
    public: str
    subject_id: str | None
    remedy: str | None

    def __new__(cls, text: str, *, code: str, public: str, subject_id: str | None = None,
                remedy: str | None = None) -> "Finding":
        line = super().__new__(cls, text)
        line.code, line.public, line.subject_id, line.remedy = code, public, subject_id, remedy
        return line


WSL_SEPARATE_CONFIG = ("WSL: the Windows-side and WSL-side Claude configurations are separate — sign-ins, "
                       "settings and sessions of Claude Code on Windows are not seen here, and the reverse")


def platform_report(runtime: runtime_mod.Runtime) -> tuple[list[str], list[str]]:
    """WSL and the state root's filesystem: ``(problems, info)``."""

    from claude_multi.platform import mounts

    problems: list[str] = []
    info: list[str] = []
    environ = runtime.environ
    root = runtime.session_store.root
    fstype = runtime.state_filesystem()
    distro = mounts.wsl(environ, proc_root=runtime.proc_root)
    if distro is not None:
        info.append(f"WSL: running under {distro}; the gateway stops when the WSL VM shuts down (when "
                    "every WSL terminal is closed) and the next launch starts it again")
        info.append(Finding(WSL_SEPARATE_CONFIG, code="wsl-separate-claude-config", public=WSL_SEPARATE_CONFIG))
        if fstype in mounts.WINDOWS_DRIVE_TYPES:
            problems.append(
                f"the state root {paths.display(root, environ)} is on a Windows drive ({fstype}) under WSL: "
                "locks and private file modes do not work there — move it into the Linux file system "
                "(unset XDG_STATE_HOME, or point it under your Linux home)")
        if str(runtime.cwd).startswith("/mnt/"):
            info.append(f"The current directory {runtime.cwd} is on a Windows drive: file access is slow "
                        "there and file modes are not kept; keep projects in the Linux file system")
    if fstype in mounts.NETWORK_TYPES and not (distro is not None and fstype in mounts.WINDOWS_DRIVE_TYPES):
        info.append(f"The state root {paths.display(root, environ)} is on a network filesystem ({fstype}): "
                    "sessions running on another host are invisible here, so forget, stop and mark-ended "
                    "treat their liveness as unknown")
    return problems, info


HOLD_COVERAGE_TEXT = (
    "gateway persistence hold: HELD because the log coverage is incomplete, not because a credential "
    "failed — {reasons}. A verbose, long-running gateway can write more log than one read evaluates, so "
    "the save evidence stays incomplete and stop, restart, update and uninstall are refused. Keep it "
    "running; after checking the credentials (claude-multi providers list), clear it on the command line "
    "(claude-multi gateway clear-hold: it records that you checked them and evaluates the logs again from "
    "there on)"
)


GATEWAY_NOT_SET_UP = ("Gateway: not set up yet in this home — no port is recorded, so nothing was observed on "
                      "the packaged port (claude-multi setup prepares it)")


def gateway_runtime_report(runtime: runtime_mod.Runtime) -> tuple[list[str], list[str]]:
    """Doctor's gateway facts: ``(attention, info)``.

    Backend, supervision, confinement, endpoint, outbound proxy, the instance
    lock, the crash counter, log coverage and size, the persistence hold and
    a restart pending from a changed gateway binary. The live observation runs
    only for a live runtime (or injected gateway seams), never against the
    host from a fixture, and never in a home where the gateway is not set up
    (another program may own the packaged port).
    """

    from claude_multi import endpoint as endpoint_mod
    from claude_multi import gateway_hold, gateway_lifecycle

    if runtime.endpoint_error is not None:
        return [], []  # readiness reports it with the document's own remedy
    attention: list[str] = []
    info: list[str] = []
    try:
        gateway = runtime.gateway()
    except (cli_errors.ClaudeMultiError, OSError) as exc:
        return [f"gateway facts unavailable: {exc}"], []
    live = runtime.gateway_seams is not None or (
        runtime.health_get is None and runtime.served_models_callback is None)
    unit = f" ({gateway.unit})" if gateway.unit else ""
    source = "endpoint.json" if gateway.config is not None else "packaged default"
    channel = endpoint_mod.channel(runtime.environ) or "unknown"
    supervision = "systemd (restarts on failure)" if gateway.backend == endpoint_mod.SYSTEMD else "none"
    info.append(f"Gateway: backend {gateway.backend}{unit} · supervision {supervision} · confinement "
                f"{gateway_lifecycle.CONFINEMENT[gateway.backend]} · endpoint {gateway.base_url} ({source}) · "
                f"channel {channel}")
    proxy_url = gateway.config.proxy_url if gateway.config is not None else None
    if proxy_url:
        info.append(f"Gateway outbound proxy: {proxy_url} (endpoint.json)")
    elif any(runtime.environ.get(name) for name in _PROXY_VARIABLES):
        info.append("A proxy variable is set in this shell; the gateway never inherits it — give the gateway "
                    "its outbound proxy with: claude-multi setup --step gateway --proxy URL")
    if live and not endpoint_mod.set_up(runtime.home):
        info.append(Finding(GATEWAY_NOT_SET_UP, code="gateway-not-set-up", public=GATEWAY_NOT_SET_UP,
                            remedy="claude-multi setup"))
    elif live:
        seen = gateway.observe()
        lock = {True: "held", False: "free", None: "unknown"}[seen.lock]
        instance = ""
        if seen.stamp is not None and seen.state in (gateway_lifecycle.OURS, gateway_lifecycle.STARTING):
            instance = f" — instance {seen.stamp.instance or 'unrecorded'}, pid {seen.stamp.pid}"
        info.append(f"Gateway instance lock: {lock}{instance} ({seen.state}: {seen.detail})")
        drift = gateway.binary_drift(seen)
        if drift is not None:
            attention.append(drift)
    starts = len(gateway.recent_starts())
    window = int(gateway_lifecycle.CRASH_WINDOW.total_seconds() // 60)
    if starts >= gateway_lifecycle.CRASH_LIMIT and gateway.backend == endpoint_mod.ON_DEMAND:
        attention.append(
            f"automatic gateway starts are paused ({starts} starts in the last {window} minutes): read "
            f"{gateway_service_hint('logs', runtime)}, fix the cause, then start it yourself: "
            f"{gateway_service_hint('start', runtime)}")
    else:
        info.append(f"Gateway starts in the last {window} minutes: {starts} of {gateway_lifecycle.CRASH_LIMIT}")
    if gateway.backend == endpoint_mod.ON_DEMAND:
        logs = file_log.instance_logs(gateway.logs_dir)
        info.append(f"Gateway logs: {len(logs)} instance log(s), {file_log.total_size(logs)} bytes in "
                    f"{paths.display(gateway.logs_dir, runtime.environ)}")
    else:
        info.append(f"Gateway logs: the {gateway.unit} journal (24 h window)")
    if live or gateway.backend == endpoint_mod.ON_DEMAND:
        hold = gateway.persistence_hold()
        if hold.held and hold.coverage_only:
            attention.append(HOLD_COVERAGE_TEXT.format(reasons="; ".join(hold.reasons)))
        elif hold.held:
            attention.append("gateway persistence hold: HELD — stop, restart, update and uninstall are refused: "
                             + "; ".join(hold.reasons) + " — " + gateway_hold.REMEDY)
        else:
            info.append(f"Gateway persistence hold: none ({hold.source} evaluated completely)")
    return attention, info


def inhibition_report(runtime: runtime_mod.Runtime) -> tuple[list[str], list[str], list[str]]:
    """Doctor's facts on the state root's gateway inhibition: ``(problems,
    attention, info)``.

    None recorded says nothing. An inhibition in progress is Attention (starts,
    stops, service and install changes wait for it); a stale one (its owner
    was interrupted) or an unreadable record BLOCKs with its remedy: it
    refuses every change until its owner finishes or releases it, and is
    never ignored.
    """

    from claude_multi import gateway_inhibition

    record = gateway_inhibition.read(runtime.session_store.root)
    if record is None:
        return [], [], []
    message, remedy = gateway_inhibition.refusal(
        record, "starts, stops, service changes, installs and updates are refused")
    line = f"{message} — fix: {remedy}"
    if not record.readable or record.stale():
        return [line], [], []
    return [], [line], []


def install_report(runtime: runtime_mod.Runtime) -> tuple[list[str], list[str]]:
    """Doctor's installation facts: ``(problems, info)``.

    This launcher's release channel, the channel the state root records (a
    mismatch or an unreadable marker BLOCKs: the launcher refuses to run) and
    every installation link found on this account.
    """

    from claude_multi import installs

    environ = {**runtime.environ, "HOME": str(runtime.home)}
    check = installs.check(runtime.session_store.root, environ, record=False)
    problems: list[str] = []
    if not check.ok:
        problems.append(f"installation: {check.problem} — {check.remedy}")
    recorded = check.owner or "not recorded yet"
    info = [f"Installation: this launcher is the {check.running or 'unknown'} channel; the state root "
            f"belongs to: {recorded}"]
    found = installs.discover(environ)
    if found:
        info.append("Installations found: " + "; ".join(item.line(environ) for item in found))
    shims = installs.installer_launchers(environ)
    if shims:
        info.append("Installer launchers: " + "; ".join(shims))
    return problems, info


def service_report(runtime: runtime_mod.Runtime) -> tuple[list[str], list[str]]:
    """Doctor's supervised-service facts: ``(attention, info)``.

    Whether the service is installed, its name and backend, and Attention for
    a stale unit, a recorded service whose unit is missing, or a Nix service
    that runs another store path than this launcher. The service manager is
    asked only for a live runtime.
    """

    from claude_multi import gateway_service

    if runtime.endpoint_error is not None:
        return [], []
    try:
        manager = runtime.gateway_service()
        live = runtime.gateway_seams is not None or runtime.gateway_service_seams is not None or (
            runtime.health_get is None and runtime.served_models_callback is None)
        status = manager.status(live=live)
    except (cli_errors.ClaudeMultiError, OSError) as exc:
        return [], [f"Gateway service: unknown ({exc})"]
    where = paths.display(status.unit_path, runtime.environ)
    if status.installed:
        info = [f"Gateway service: installed — {status.name} ({where}) · backend {status.backend}"
                + (" · unit stale" if status.stale else "")]
    elif status.supported is not None:
        info = [f"Gateway service: not available here ({status.supported}); the gateway runs on demand"]
    else:
        info = [f"Gateway service: not installed · backend {status.backend} (claude-multi gateway service "
                "install hands the gateway to a supervised unit)"]
    return status.attention(runtime.environ), info
