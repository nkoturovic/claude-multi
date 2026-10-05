"""Hermetic screen/PTY fixture.

``fixture_runtime`` builds a ``cli.Runtime`` over the frozen fixture catalog
in a temp root with the ``CLITestCase`` loopback seams (served set, health,
doctor stubs), so no screen test or PTY child reaches the running gateway or
reads host liveness:

- ``ScreenRuntime`` overrides the two liveness method seams
  (``_live_prefixes`` → ``live_prefixes``, ``_proc_ids`` → ``proc_ids``;
  the ``tests/_v4.py`` precedent), so nothing reads ``/tmp/cc-daemon-<uid>``
  or ``/proc``.
- Injecting both loopback seams makes ``_read_gateway_journal`` return None:
  no test reads the host journal.
- An empty ``<tmp>/.git`` marker bounds the ``_project_agent_files`` and
  ``find_cm_collisions`` ancestor walks at the temp root.
- ``hermetic(runtime)`` patches ``cli._live_background_prefixes`` for the
  four kept flows that call it directly; every screen test that
  can reach them, and every PTY child, runs inside it.
- The managed-settings agents directory (``scope.DEFAULT_MANAGED_AGENTS_DIR``)
  is host state the launch gates read too; there is no seam for it.
  :func:`managed_agents_precondition` states the
  precondition (no ``cm-*`` agent file there) and fails loudly, never skips.

Nothing here pins a fixture id: profile names and keys come from the loaded
fixture catalog.  PTY children import this module with
``PTYProcess(..., extra_pythonpath=(tests,))``.
"""

from __future__ import annotations

import contextlib
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from unittest import mock

from claude_multi import catalog, cli, lineup, management as management_mod, scope, sessions, state, strict_json
from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT, served_selectors
import claude_multi.launch
import claude_multi.service

__all__ = [
    "FIFTH_ID",
    "FIXED_ID",
    "FIXED_NOW",
    "FOURTH_ID",
    "MUTATION_TOKEN",
    "OTHER_ID",
    "ScreenRuntime",
    "THIRD_ID",
    "fixture_runtime",
    "hermetic",
    "iso",
    "journal_text",
    "managed_agents_precondition",
    "needs_choice_key",
    "v4_record",
]

FIXED_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
FIXED_ID = "11111111-1111-4111-8111-111111111111"  # tc:34
OTHER_ID = "22222222-2222-4222-8222-222222222222"  # tc:35
THIRD_ID = "33333333-3333-4333-8333-333333333333"
FOURTH_ID = "44444444-4444-4444-8444-444444444444"
FIFTH_ID = "55555555-5555-4555-8555-555555555555"
MUTATION_TOKEN = "66666666-6666-4666-8666-666666666666"

SPEC = "the screen fixture contract"


def iso(moment: datetime) -> str:
    """The record timestamp form (``YYYY-MM-DDTHH:MM:SSZ``, UTC)."""

    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ScreenRuntime(cli.Runtime):
    """No machine liveness scan: tests set ``live_prefixes``/``proc_ids`` explicitly."""

    live_prefixes: frozenset[str] = frozenset()
    proc_ids: frozenset[str] = frozenset()

    def _live_prefixes(self) -> frozenset[str]:
        return self.live_prefixes

    def _proc_ids(self) -> frozenset[str]:
        return self.proc_ids


def _never_exec(path: str, argv: list[str], env: dict[str, str]) -> Any:
    raise AssertionError(f"{SPEC}: a fixture runtime never execs ({path})")


def _secret_names(asset_root: Path) -> list[str]:
    """Every ``env:`` secret name the catalog's direct providers declare (derived)."""

    providers = catalog.load_catalog(asset_root).providers
    names = []
    for provider in providers.values():
        auth = provider.get("transport", {}).get("auth")
        ref = auth.get("secret_ref") if isinstance(auth, dict) else None
        if isinstance(ref, str) and ref.startswith("env:"):
            names.append(ref[4:])
    return sorted(set(names))


def fixture_runtime(
    tmp: Path | str,
    *,
    asset_root: Path = FIXTURE_ROOT,
    live: Iterable[str] = frozenset(),
    proc: Iterable[str] = frozenset(),
    served: Iterable[str] | None = None,
    gateway_down: bool = False,
    health: int | BaseException = 200,
    launch_callback: Callable[[cli.PreparedLaunch], Any] | None = None,
    environ_extra: dict[str, str] | None = None,
    secrets: Iterable[str] | None = None,
    management: bytes | int | None = None,
) -> ScreenRuntime:
    """A hermetic ``ScreenRuntime`` rooted at ``tmp``.

    - ``tmp/.git`` (empty marker), ``tmp/project`` (the cwd; its name is
      ``project`` in every golden), ``tmp/home`` (HOME) with the fixture
      gateway token, ``tmp/config`` / ``tmp/state`` (XDG roots) and
      ``tmp/secrets/claude.env`` holding a dummy value for every direct
      provider secret the catalog declares (``secrets`` narrows the set).
    - ``served`` = the served selector set (None = every selector the
      fixture can serve, ``_catalog.served_selectors``); ``gateway_down``
      makes ``/v1/models`` refuse.  ``health`` is the ``/healthz`` status,
      or an exception ``health_get`` raises.
    - ``launch_callback`` receives each performed plan (default: appended to
      ``runtime.launches``, returning 0).  The ``execve`` seam refuses.
    - ``management`` bytes inject a 200 response; an int injects that status.
      Both install a temp-home key; None creates no key and no callback.
      ``management_calls`` counts requests; ``pool_clock`` starts at zero.
    - ``live``/``proc`` seed ``runtime.live_prefixes``/``runtime.proc_ids``.
    """

    root = Path(tmp)
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / "project").mkdir(parents=True, exist_ok=True)
    secret_dir = state.ensure_private_dir(root / "secrets")
    names = _secret_names(asset_root) if secrets is None else sorted(set(secrets))
    state.atomic_write(
        secret_dir / "claude.env",
        "".join(f"{name}=tui-fixture-dummy\n" for name in names).encode("ascii"),
    )
    environ = {
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_STATE_HOME": str(root / "state"),
        "TERM": "dumb",
        "CLAUDE_MULTI_SECRET_ENV": str(secret_dir / "claude.env"),
    }
    environ.update(environ_extra or {})
    token_dir = state.ensure_private_dir(Path(environ["HOME"]) / ".config" / "claude-multi")
    state.atomic_write(token_dir / "api-key", (FIXTURE_GATEWAY_TOKEN + "\n").encode("ascii"))
    served_set = frozenset(served_selectors(asset_root) if served is None else served)

    def served_models(gateway: dict[str, Any], token: str) -> tuple[set[str] | None, int | None]:
        def models_get(_base_url: str, _token: str) -> tuple[int, set[str]]:
            if gateway_down:
                raise ConnectionRefusedError("fixture gateway down")
            return 200, set(served_set)

        return claude_multi.launch.served_models(gateway, token, models_get=models_get,
                                       owner_check=lambda _base: claude_multi.service.OwnerVerdict("ours", "fixture"))

    def health_get(_base_url: str, _path: str) -> int:
        if isinstance(health, BaseException):
            raise health
        return health

    management_calls = []
    if management is not None:
        management_mod.prepare_start(Path(environ["HOME"]), stopped=True)
        # Quota reads exist only on the management channel (the Nix wrapper's).
        environ.setdefault(management_mod.CHANNEL_ENV, management_mod.MANAGEMENT_CHANNEL)

    def management_get(_base_url: str, _key: str) -> tuple[int, bytes]:
        management_calls.append(True)  # count only, never retain credentials
        return (200, management) if isinstance(management, bytes) else (management, b"")

    launches: list[cli.PreparedLaunch] = []

    def record_launch(prepared: cli.PreparedLaunch) -> int:
        launches.append(prepared)
        return 0

    runtime = ScreenRuntime(
        listener_owner=lambda _base: claude_multi.service.OwnerVerdict("ours", "fixture gateway"),
        background_liveness=lambda: sessions.BackgroundLiveness(True, cli._live_background_prefixes(root / "daemon")),
        managed_root=root / "managed",
        asset_root=asset_root,
        environ=environ,
        cwd=root / "project",
        launch_callback=launch_callback or record_launch,
        doctor_callback=lambda _runtime: [],
        doctor_binary_callback=lambda _contract: ([], ["Managed Claude verified (fixture)."]),
        doctor_daemon_callback=lambda: claude_multi.launch.DaemonStatus(
            state="absent", summary="fixture daemon absent"
        ),
        served_models_callback=served_models,
        health_get=health_get,
        management_callback=management_get if management is not None else None,
        execve=_never_exec,
    )
    runtime.live_prefixes = frozenset(live)
    runtime.proc_ids = frozenset(proc)
    runtime.pool_clock = lambda: 0.0
    runtime.management_calls = management_calls
    runtime.launches = launches  # type: ignore[attr-defined]
    return runtime


@contextlib.contextmanager
def hermetic(runtime: ScreenRuntime) -> Iterator[ScreenRuntime]:
    """Route the kept direct ``_live_background_prefixes()`` callers through the fixture.

    ``_forget_liveness_guard`` (X forget), ``_run_resume_gate_modal``'s
    post-stop rescan, ``_choose_session_line`` and
    ``_pre_helper_session_labels`` call the module function directly;
    this patches it to ``runtime.live_prefixes`` (the tc:5694/6530
    precedent) for the duration, and answers every Git work-tree probe
    "not a repository".
    """

    with mock.patch('claude_multi.cli.session_facts._live_background_prefixes', lambda root=None: runtime.live_prefixes
    ), mock.patch('claude_multi.compiler.git_work_tree', lambda _cwd: False):
        # The fixture directory is never a Git repository, wherever the
        # temporary directory lives.
        yield runtime


def managed_agents_precondition() -> None:
    """Fail (never skip) when the host managed-settings agents dir holds a ``cm-*`` file.

    ``scope.find_cm_collisions`` scans it and the launch gate and classify
    read it too; there is no seam, so the screen goldens state the
    precondition instead.
    """

    directory = Path(scope.DEFAULT_MANAGED_AGENTS_DIR)
    try:
        hits = sorted(p.name for p in directory.iterdir() if p.name.startswith("cm-"))
    except OSError:
        return
    if hits:
        raise AssertionError(
            f"{SPEC}: the host managed-settings agents directory {directory} holds "
            f"cm-* agent files ({', '.join(hits)}); the screen goldens need it free of them"
        )


def needs_choice_key(runtime: cli.Runtime) -> str:
    """The derived retired-null key: its chain ends in no live line."""

    lcat = runtime.lineup_catalog()
    key = next((k for k in lcat.retired if lcat.resolve_key(k).key is None), None)
    if key is None:
        raise AssertionError(f"{SPEC} §7.1: the fixture has a retired key with a null successor")
    return key


def v4_record(
    runtime: cli.Runtime,
    profile_name: str | None,
    *,
    managed_id: str,
    follow: bool = True,
    ended: bool = False,
    title: str | None = None,
    pending: bool = False,
    needs_choice: bool = False,
    last_seen: timedelta = timedelta(minutes=2),
    scope: bool = True,
    generation: int | None = None,
    document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A deterministic v4 record saved through the v4 writer.

    1. ``runtime.prepare`` a fresh launch of ``profile_name`` (or of
       ``document``, ad-hoc when ``profile_name`` is None) with
       ``SessionStore.new_id`` patched to ``managed_id``.
    2. ``sessions.ensure_state_v4``; with ``scope`` the compiled scope is
       swapped in (a gen ≥ 1 scope with ``lead-set.json``).
    3. A fixed ``mutation_token``; ``created_at``/``last_seen_at`` relative to
       :data:`FIXED_NOW`; ``last_event_source`` ``end`` when ``ended``;
       ``title``.
    4. ``pending`` adds a schema-valid pending profile change (all six
       keys; ``document`` null for ``kind: "profile"``).
    5. ``needs_choice`` rewrites ``applied.lead.key`` to the derived
       retired-null key through ``sessions.with_applied``.
    6. ``generation`` overrides ``lineup_generation`` (0 drops
       ``launch_fence`` and ``scope_lead``, which only a gen ≥ 1 record
       carries).
    7. ``runtime.session_store.save``.
    """

    if document is None:
        if profile_name is None:
            raise ValueError("v4_record needs a profile name or a document")
        document = runtime.profiles.load(profile_name)
    if profile_name is None:
        target = cli.LaunchTarget("ad-hoc", copy.deepcopy(document), None, False, "Direct")
    else:
        target = cli.LaunchTarget(
            "profile", copy.deepcopy(document), profile_name, follow, f"Profile {profile_name}"
        )
    with mock.patch.object(sessions.SessionStore, "new_id", lambda _store: managed_id):
        prepared = runtime.prepare(target, action="fresh", passthrough=[])
    root = runtime.session_store.root
    sessions.ensure_state_v4(root)
    if scope:
        scope_mod_swap(root, managed_id, prepared.result.scope_plan)
    record = copy.deepcopy(prepared.record)
    record["mutation_token"] = MUTATION_TOKEN
    seen = FIXED_NOW - last_seen
    record["last_seen_at"] = iso(seen)
    record["created_at"] = iso(seen - timedelta(hours=1))
    record["last_event_source"] = "end" if ended else None
    if title is not None:
        record["title"] = title
    if pending:
        if profile_name is None:
            raise ValueError("a pending profile change needs a profile")
        record["pending"] = {
            "requested_at": iso(FIXED_NOW - timedelta(minutes=1)),
            "kind": "profile",
            "profile": profile_name,
            "follow": True,
            "document": None,
            "reasons": [lineup.REASON_FORCED],
        }
    if needs_choice:
        applied = copy.deepcopy(record["applied"])
        applied["lead"]["key"] = needs_choice_key(runtime)
        record = sessions.with_applied(record, applied)
    if generation is not None:
        record["lineup_generation"] = generation
        if generation == 0:
            record.pop("launch_fence", None)
            record.pop("scope_lead", None)
    runtime.session_store.save(record)
    return runtime.session_store.load(managed_id)


def scope_mod_swap(root: Path, managed_id: str, plan: Any) -> None:
    """``scope.swap_scope`` (a named step so tests can see what ``scope=True`` does)."""

    scope.swap_scope(root, managed_id, plan)


def journal_text(
    *,
    dead_pool: str | None = None,
    quota: Iterable[tuple[str, str, int]] = (),
    when: datetime | None = None,
) -> str:
    """CLIProxyAPI-format journal lines for the providers screen's ``journal=`` seam.

    The ``tests/test_doctor_v4.py`` shapes: ``[YYYY-mm-dd HH:MM:SS]
    [<request id>] [<level>] [<file:line>] <message>`` in local time.
    ``dead_pool`` (``claude``/``codex``) adds one ``invalid_grant`` line
    naming it; ``quota`` adds, per ``(model alias, code, count)``, ``count``
    request ids, each with one selector line and one status line.
    ``when`` defaults to :data:`FIXED_NOW` in local time.
    """

    moment = (when or FIXED_NOW).astimezone().replace(tzinfo=None)
    stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = []
    if dead_pool is not None:
        lines.append(
            f"[{stamp}] [--------] [error] [conductor.go:1] {dead_pool} refresh failed: "
            "invalid_grant"
        )
    serial = 0
    for model, code, count in quota:
        for _ in range(count):
            serial += 1
            rid = f"{serial:08x}"
            lines.append(f"[{stamp}] [{rid}] [info ] [selector.go:468] x | model={model}")
            lines.append(
                f'[{stamp}] [{rid}] [warn ] [gin_logger.go:99] {code} |  1.2s | 127.0.0.1 | POST "/v1/messages"'
            )
    return "".join(line + "\n" for line in lines)


def canonical(record: dict[str, Any]) -> bytes:
    """Canonical record bytes (for byte-unchanged assertions)."""

    return strict_json.canonical_file_bytes(record)


__all__ += ["canonical", "scope_mod_swap"]


def quota_body(*, pool: str = "claude", percent: int = 93, age: timedelta = timedelta(days=3),
               reset: bool = True, window: bool = True, observed: bool = True,
               reason: str = "", count: int = 1, short_percent: int | None = None) -> bytes:
    """Synthetic passive weekly observations at FIXED_NOW (no shipped ids)."""
    signals = {}
    if window:
        if pool == "claude":
            signals = {"anthropic-ratelimit-unified-7d-utilization": str(percent / 100)}
            if reset:
                signals["anthropic-ratelimit-unified-7d-reset"] = iso(FIXED_NOW + timedelta(days=3, hours=-3))
        else:
            signals = {"x-codex-secondary-used-percent": str(percent),
                       "x-codex-secondary-window-minutes": "10080"}
            if reset:
                signals["x-codex-secondary-reset-at"] = str(int((FIXED_NOW + timedelta(days=3, hours=-3)).timestamp()))
    if short_percent is not None:
        if pool == "claude":
            signals["anthropic-ratelimit-unified-5h-utilization"] = str(short_percent / 100)
            if reset:
                signals["anthropic-ratelimit-unified-5h-reset"] = iso(FIXED_NOW + timedelta(hours=4))
        else:
            signals["x-codex-primary-used-percent"] = str(short_percent)
            signals["x-codex-primary-window-minutes"] = "300"
            if reset:
                signals["x-codex-primary-reset-at"] = str(int((FIXED_NOW + timedelta(hours=4)).timestamp()))
    observation = {"signals": signals}
    if observed:
        observation["observed_at"] = iso(FIXED_NOW - age)
    return strict_json.pretty_file_bytes({"files": [
        {"provider": pool, "status": "active", "status_message": reason, "quota": observation}
        for _ in range(count)
    ]})


class GatewayViewStub:
    """A gateway view for the screens' gateway keys (W, H): which actions it
    offers and what the dialog draws, without observing any gateway."""

    gateway = None
    inhibition = None

    def __init__(self, *, start: bool = False, restart: bool = False, service: bool = False,
                 service_line: str | None = None) -> None:
        self.can_start, self.can_restart, self.can_install_service = start, restart, service
        self._service_line = service_line

    def headline(self) -> str:
        return "Gateway: fixture view"

    def service_line(self) -> str | None:
        return self._service_line


@contextlib.contextmanager
def gateway_actions_seam(view: GatewayViewStub, *, tty_in: str = "\n") -> Iterator[list]:
    """Route the gateway actions of a screen to ``view``: ``observe`` returns
    it, ``act`` records ``(view, action)`` and reports ready, curses is not
    suspended and the line prompts read ``tty_in``. Yields the recorded
    actions."""

    import io

    from claude_multi import gateway_lifecycle, tui
    import claude_multi.cli.screens.gateway_actions as gateway_actions

    acted: list = []

    def act(seen, action):
        acted.append((seen, action))
        return gateway_lifecycle.Outcome("ready", f"gateway {action}: fixture done")

    with mock.patch.object(gateway_actions, "observe", return_value=view), \
            mock.patch.object(gateway_actions, "act", side_effect=act), \
            mock.patch.object(tui, "suspended_curses", mock.MagicMock()), \
            mock.patch("sys.stdin", io.StringIO(tty_in)), contextlib.redirect_stdout(io.StringIO()):
        yield acted
