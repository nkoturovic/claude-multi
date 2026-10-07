"""80-column TUI frames of the screens over the fixture.

``render(screen_factory, width, height)`` draws one frame of a screen on a
``test_tui.FakeWindow`` (the widget tests' in-memory window, reused
unchanged) and returns every row, right-stripped and ``\\n``-terminated.

``GOLDENS`` maps a golden name to a builder over a temp root: each builds a
hermetic ``_tui_fixture.fixture_runtime`` there and renders one screen in
``MONO_PALETTE``.  ``tests/test_tui_goldens.py`` compares the builders with
``tests/goldens/tui/<name>-80x24.txt``; ``tests/bless.py`` writes them.  The
names are roles, never fixture ids.

Determinism inputs: the fixture catalog, ``FIXED_NOW`` for doctor's
clock (``cli._doctor_now``), the injected liveness/served/health seams and
``hermetic(runtime)``, the journal text through the providers ``journal=``
seam, no pinned registry (``CLAUDE_MULTI_REGISTRY_DIR`` names a missing
directory, so the retirement radar is silent), the gateway token's mtime set
to ``FIXED_NOW``, the temp-root ``.git`` marker, and no temp path drawn.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest import mock

from claude_multi import catalog, cli, profile, proxy, tui, views

import _tui_fixture as fx
from test_tui import FakeWindow

WIDTH = 80
HEIGHT = 24


# The size a builder renders at when it names none (``at_size`` sets it).
_SIZES: list[tuple[int, int]] = []


def current_size() -> tuple[int, int]:
    """``(width, height)`` a builder draws at: the size ``at_size`` set, else 80×24."""

    return _SIZES[-1] if _SIZES else (WIDTH, HEIGHT)


def render(screen_factory: Callable[[], Any], width: int | None = None, height: int | None = None) -> str:
    """One frame of ``screen_factory()`` at ``width`` × ``height`` (default
    the size ``at_size`` set, else 80×24)."""

    default_width, default_height = _SIZES[-1] if _SIZES else (WIDTH, HEIGHT)
    width = default_width if width is None else width
    height = default_height if height is None else height
    win = FakeWindow([], height=height, width=width)
    screen = screen_factory()
    screen._draw(win)
    return "".join(win.line(y) + "\n" for y in range(height))


@contextlib.contextmanager
def golden_runtime(root: Path, **kwargs: Any) -> Iterator[fx.ScreenRuntime]:
    """A hermetic fixture runtime at ``root`` with the golden determinism inputs."""

    fx.managed_agents_precondition()
    extra = {catalog.REGISTRY_DIR_ENV: str(Path(root) / "no-pinned-registry")}
    extra.update(kwargs.pop("environ_extra", None) or {})
    runtime = fx.fixture_runtime(root, environ_extra=extra, **kwargs)
    token = proxy.config_dir(runtime.home) / "api-key"
    stamp = fx.FIXED_NOW.timestamp()
    os.utime(token, (stamp, stamp))
    with fx.hermetic(runtime), mock.patch('claude_multi.cli.gateway_facts._doctor_now', lambda: fx.FIXED_NOW):
        yield runtime


def first_oauth_pool(runtime: cli.Runtime) -> str:
    """The pool of the first OAuth-pool provider (sorted ids; derived)."""

    providers = runtime.lineup_catalog().providers
    return next(
        providers[pid]["transport"]["pool"]
        for pid in sorted(providers)
        if providers[pid]["transport"]["kind"] == "oauth-pool"
    )


def default_lineup(runtime: cli.Runtime) -> profile.ResolvedLineup:
    """``catalog.DEFAULT_SEED`` evaluated against the current Settings (the card's lineup)."""

    evaluation = profile.evaluate(
        runtime.profiles.load(catalog.DEFAULT_SEED),
        runtime.lineup_catalog(),
        bindings=runtime.bindings.bindings(),
        effective=runtime.current_effective(),
    )
    if evaluation.lineup is None:
        raise AssertionError(f"{fx.SPEC}: the default seed evaluates on the fixture")
    return evaluation.lineup


def models_golden(root: Path) -> str:
    with golden_runtime(root) as runtime:
        return render(lambda: cli._ModelsScreen(runtime, palette=tui.MONO_PALETTE))


def providers_golden(root: Path) -> str:
    with golden_runtime(root) as runtime:
        journal = fx.journal_text(dead_pool=first_oauth_pool(runtime))
        return render(
            lambda: cli._ProvidersScreen(runtime, palette=tui.MONO_PALETTE, journal=lambda: journal)
        )


def settings_golden(root: Path, selected: str | None = None, *, writes: bool = True) -> str:
    """``selected`` names the row key whose footer the frame shows
    (None: the screen's initial row); ``writes=False`` is a read-only Runtime."""

    with golden_runtime(root) as runtime:
        lineup = default_lineup(runtime)
        if not writes:
            runtime.allow_state_writes = False

        def screen() -> cli._SettingsScreen:
            built = cli._SettingsScreen(
                runtime, tui.MONO_PALETTE, card_lineup=lineup,
                tty_in=io.StringIO(), tty_out=io.StringIO(),
            )
            if selected is not None:
                built.index = next(
                    index for index, (kind, value) in enumerate(built.entries)
                    if kind == "row" and value.key == selected
                )
            return built

        return render(screen)


def editor_state(runtime: cli.Runtime, name: str = catalog.DEFAULT_SEED) -> tui.ProfileEditorState:
    """The editor state of ``name`` as ``profile edit`` builds it."""

    return cli._profile_editor_state(runtime, name, "edit")


def editor_golden(root: Path) -> str:
    with golden_runtime(root) as runtime:
        return render(lambda: tui.ProfileEditorScreen(editor_state(runtime), palette=tui.MONO_PALETTE))


def picker_golden(root: Path) -> str:
    """The binding picker of the implementer slot of the default seed (derived)."""

    with golden_runtime(root) as runtime:
        state = editor_state(runtime)
        slot = "cm-implementer"
        rows = views.line_rows(state.cat, state.effective, custom_ids=frozenset())
        model = views.picker_rows(
            rows, slot=slot, bindings=state.bindings, lcat=state.cat, eff=state.effective,
            current=state.slot_binding(slot),
        )
        return render(lambda: tui.BindingPicker(model, palette=tui.MONO_PALETTE))


def routing_golden(root: Path) -> str:
    with golden_runtime(root) as runtime:
        lineup = default_lineup(runtime)
        return render(
            lambda: tui.RoutingPreviewScreen(lineup, name=catalog.DEFAULT_SEED, palette=tui.MONO_PALETTE)
        )


def propagate_golden(root: Path) -> str:
    """Two followers of the default seed: one unknown (titled), one ended."""

    with golden_runtime(root) as runtime:
        fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title="api design")
        fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID, ended=True, title="probe fixes")
        return render(lambda: cli._PropagationScreen(runtime, tui.MONO_PALETTE, [catalog.DEFAULT_SEED]))


def card_screen(runtime: cli.Runtime, target: cli.LaunchTarget, **kwargs: Any) -> cli._LaunchCardScreen:
    """The 3.0 card with the golden inputs: health checked ok, no update hint, no tty I/O."""

    return cli._LaunchCardScreen(
        runtime,
        target,
        passthrough=[],
        palette=tui.MONO_PALETTE,
        gateway_checked=True,
        hint_detector=lambda _contract: None,
        tty_in=io.StringIO(),
        tty_out=io.StringIO(),
        **kwargs,
    )


def profile_target(runtime: cli.Runtime, name: str) -> cli.LaunchTarget:
    return cli.LaunchTarget("profile", runtime.profiles.load(name), name, True, f"Profile {name}")


def direct_seed(runtime: cli.Runtime) -> str:
    """The seed whose document binds no agent (derived)."""

    name = next(
        (n for n, doc in sorted(runtime.catalog.seed_profiles.items()) if doc.get("agents") == {}),
        None,
    )
    if name is None:
        raise AssertionError(f"{fx.SPEC} §7.1: the fixture has a direct seed")
    return name


def card_fresh_default_golden(root: Path) -> str:
    with golden_runtime(root) as runtime:
        runtime.profiles.install_seeds()
        target = profile_target(runtime, catalog.DEFAULT_SEED)
        return render(lambda: card_screen(runtime, target))


def card_fresh_direct_golden(root: Path) -> str:
    with golden_runtime(root) as runtime:
        runtime.profiles.install_seeds()
        target = profile_target(runtime, direct_seed(runtime))
        return render(lambda: card_screen(runtime, target))


def card_blocked_golden(root: Path) -> str:
    """A test-local profile whose lead is the derived retired-null key."""

    with golden_runtime(root) as runtime:
        document = runtime.profiles.load(catalog.DEFAULT_SEED)
        document.pop("seed", None)
        document["name"] = "blocked"
        document["lead"] = {"model": fx.needs_choice_key(runtime), "effort": profile.ULTRACODE}
        runtime.profiles.save(document)
        target = profile_target(runtime, "blocked")
        return render(lambda: card_screen(runtime, target))


def card_resume_diff_golden(root: Path) -> str:
    """A session following the default seed, whose first writer's effort then changed."""

    with golden_runtime(root) as runtime:
        runtime.profiles.install_seeds()
        record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        writer = profile.WRITER_IDS[0]

        def change(document: dict) -> None:
            binding = document["agents"][writer]
            efforts = profile.declared_efforts(runtime.lineup_catalog().lines[binding["model"]])
            binding["effort"] = next(e for e in efforts if e != binding["effort"])

        runtime.profiles.update(catalog.DEFAULT_SEED, change)
        prepared = runtime.prepare(
            cli._record_resume_target(record), action="resume", passthrough=[], session_id=fx.FIXED_ID
        )
        return render(
            lambda: card_screen(runtime, prepared.target, action="resume", prepared=prepared)
        )


def direct_golden(root: Path) -> str:
    with golden_runtime(root) as runtime:
        return render(lambda: cli._DirectScreen(runtime, palette=tui.MONO_PALETTE))


def _evaluated(runtime: cli.Runtime, name: str) -> profile.ResolvedLineup | None:
    return profile.evaluate(
        runtime.profiles.load(name), runtime.lineup_catalog(),
        bindings=runtime.bindings.bindings(), effective=runtime.current_effective(),
    ).lineup


def live_seed(runtime: cli.Runtime) -> str:
    """The first other seed whose relaunch fields and lead equal the default seed's."""

    default = _evaluated(runtime, catalog.DEFAULT_SEED)
    for name in sorted(runtime.catalog.seed_profiles):
        other = _evaluated(runtime, name) if name != catalog.DEFAULT_SEED else None
        if other is not None and other.relaunch_fields() == default.relaunch_fields() and (
            other.lead.binding.key == default.lead.binding.key
        ):
            return name
    raise AssertionError(f"{fx.SPEC} §7.1: the fixture has a LIVE seed pair")


def relaunch_seed(runtime: cli.Runtime) -> str:
    """The first seed whose native agents differ from the default seed's."""

    default = runtime.profiles.load(catalog.DEFAULT_SEED)["native_agents"]
    for name in sorted(runtime.catalog.seed_profiles):
        if runtime.profiles.load(name).get("native_agents") != default:
            return name
    raise AssertionError(f"{fx.SPEC} §7.1: the fixture has a RELAUNCH seed")


NATIVE_ID = "77777777-7777-4777-8777-777777777777"


def sessions_golden(root: Path) -> str:
    """Four records (running, pinned + pending, ended direct, needs a choice) and one native session."""

    with golden_runtime(root) as runtime:
        runtime.profiles.install_seeds()
        fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title="api design")
        fx.v4_record(
            runtime, live_seed(runtime), managed_id=fx.OTHER_ID, follow=False, pending=False,
            title="gateway patch", last_seen=timedelta(days=1),
        )
        other = runtime.session_store.load(fx.OTHER_ID)
        other["pending"] = {
            "requested_at": fx.iso(fx.FIXED_NOW - timedelta(hours=20)),
            "kind": "profile", "profile": relaunch_seed(runtime), "follow": True, "document": None,
            "reasons": ["native agents: explore replace → native"],
        }
        runtime.session_store.save(other)
        fx.v4_record(
            runtime, relaunch_seed(runtime), managed_id=fx.THIRD_ID, ended=True, title="notes",
            last_seen=timedelta(days=3),
        )
        fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FOURTH_ID, needs_choice=True,
                     ended=True, last_seen=timedelta(days=5))
        runtime.live_prefixes = frozenset({fx.FIXED_ID[:8]})
        slug = cli._native_project_slug(runtime.cwd)
        native = Path(runtime.environ["HOME"]) / ".claude" / "projects" / slug
        native.mkdir(parents=True, exist_ok=True)
        (native / f"{NATIVE_ID}.jsonl").touch()
        return render(lambda: cli._SessionsScreen(runtime, palette=tui.MONO_PALETTE, now=fx.FIXED_NOW))


def _lineup_golden(root: Path, target: Callable[[cli.Runtime], str]) -> str:
    with golden_runtime(root) as runtime:
        runtime.profiles.install_seeds()
        record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title="api design")

        def dialog() -> cli._LineupDialog:
            screen = cli._LineupDialog(runtime, record, palette=tui.MONO_PALETTE, liveness="unknown")
            screen.profile_name = target(runtime)
            screen._preview()
            return screen

        return render(dialog)


def lineup_live_golden(root: Path) -> str:
    return _lineup_golden(root, live_seed)


def lineup_relaunch_golden(root: Path) -> str:
    return _lineup_golden(root, relaunch_seed)



def providers_quota_golden(root: Path) -> str:
    with golden_runtime(root, management=fx.quota_body(percent=18, short_percent=42, count=2, age=timedelta(minutes=12))) as runtime:
        journal = fx.journal_text(dead_pool=first_oauth_pool(runtime))
        return render(lambda: cli._ProvidersScreen(
            runtime, palette=tui.MONO_PALETTE, journal=lambda: journal,
            now=fx.FIXED_NOW, tz=timezone.utc,
        ))


def _card_quota_golden(root: Path, *, resume: bool = False, **observation) -> str:
    with golden_runtime(root, management=fx.quota_body(**observation)) as runtime:
        target = profile_target(runtime, catalog.DEFAULT_SEED)
        kwargs = {"now": fx.FIXED_NOW, "tz": timezone.utc}
        if resume:
            record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
            prepared = runtime.prepare(cli._record_resume_target(record), action="resume",
                                       passthrough=[], session_id=fx.FIXED_ID)
            target = prepared.target
            kwargs.update(action="resume", prepared=prepared)
        return render(lambda: card_screen(runtime, target, **kwargs))



def provider_kind_golden(root: Path) -> str:
    def screen():
        form = tui.OnboardingForm("new provider", views.provider_form_fields())
        form.index = 1
        return form
    return render(screen)


def model_form_golden(root: Path, *, pool=False) -> str:
    import test_onboarding_isolation as journeys
    line = journeys.JourneyFixture().line(pool=pool)
    def screen():
        form = tui.OnboardingForm("declare model", views.model_form_fields(line, key="custom-fixture-reviewer"))
        form.index = 9  # requested capabilities (draft, no grant)
        return form
    return render(screen)


ESC = "\x1b"


def frame_text(frame: str) -> str:
    """A recorded ``FakeWindow`` frame in the golden format (rows ``\n``-terminated)."""

    return "".join(line + "\n" for line in frame.split("\n"))


def last_frame(win: FakeWindow, needle: str) -> str:
    return frame_text(next(frame for frame in reversed(win.frames) if needle in frame))


@contextlib.contextmanager
def onboarding_actions(keys: list[str], *, listing: Callable[..., bytes] | None = None) -> Iterator[Any]:
    """Production ``OnboardingActions`` over the offline journey fixture: a
    scripted 80x24 window, consents answered yes, stdio a terminal (the
    human guard itself is not under test here)."""

    import test_onboarding_isolation as journeys
    import claude_multi.cli.consent as consent
    import claude_multi.cli.screens.common as common

    case = journeys.JourneyFixture()
    case.setUp()
    try:
        if listing is not None:
            case.runtime.listing_transport = listing
        win = FakeWindow(keys, height=HEIGHT, width=WIDTH)
        action = common.OnboardingActions(case.runtime, win, tui.MONO_PALETTE)
        action.confirm = lambda text: True
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            yield case, action, win
    finally:
        case.doCleanups()


def _two_models(url, headers, **caps) -> bytes:
    import json
    return json.dumps({"data": [{"id": "vendor/fixture-reviewer", "context_length": 200000},
                                {"id": "vendor/fixture-assistant", "context_length": 200000}]}).encode()


def checkbox_golden(root: Path) -> str:
    """The production listing picker after Space on the first model ([x])."""

    with onboarding_actions(["\n", " ", ESC], listing=_two_models) as (_case, action, win):
        action.add_models("openrouter")
        return last_frame(win, "advertised models")


def openrouter_listing_golden(root: Path) -> str:
    from test_openrouter_discovery import ACCOUNT, body

    def listing(url, headers, **caps):
        return body("account" if url == ACCOUNT else "public")

    with onboarding_actions(["\n", ESC], listing=listing) as (_case, action, win):
        action.add_models("openrouter")
        return last_frame(win, "advertised models")


def listing_unavailable_golden(root: Path) -> str:
    def refuse(url, headers, **caps) -> bytes:
        raise OSError("fixture listing refused")

    with onboarding_actions(["\n", ESC, ESC], listing=refuse) as (_case, action, win):
        action.add_models("openrouter")
        return last_frame(win, "Listing unavailable")


def qualification_consent_golden(root: Path, *, pool=False) -> str:
    import test_qualify as q
    from claude_multi import qualify
    plan = q._plan(provider=q.POOL if pool else q.PROVIDER,
                   entry=q._entry(["high"], selector="claude-fixture-9") if pool else q._entry())
    return render(lambda: tui.TextView("Confirm explicit action", qualify.consent_text(plan).splitlines(), confirm=True))


def operator_models_golden(root: Path, *, admitted=False, eligible=False, picker=False) -> str:
    import test_onboarding_isolation as journeys
    case = journeys.JourneyFixture()
    case.setUp()
    try:
        key = case.declare()
        if admitted:
            case.invoke(["models", "admit", key])
        if eligible:
            case.invoke(["models", "qualify", key, "--agents"])
        runtime = case.runtime
        if picker:
            model = views.picker_rows(views.line_rows(runtime.lineup_catalog(), runtime.current_effective(), custom_ids=frozenset()),
                                      slot="cm-reviewer", bindings={}, lcat=runtime.lineup_catalog(),
                                      eff=runtime.current_effective(), current=None)
            def screen():
                widget = tui.BindingPicker(model)
                widget.index = next(i for i,item in enumerate(model.items) if item.key == key)
                return widget
        else:
            def screen():
                widget = cli._ModelsScreen(runtime, palette=tui.MONO_PALETTE)
                widget.index = widget.keys.index(key)
                return widget
        return render(screen)
    finally:
        case.doCleanups()


def sessions_all_golden(root: Path) -> str:
    with golden_runtime(root) as runtime:
        fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        def screen():
            widget = cli._SessionsScreen(runtime, palette=tui.MONO_PALETTE, now=fx.FIXED_NOW)
            widget.cwd_filter = False
            widget._reload()
            return widget
        return render(screen)


def card_details_golden(root: Path, *, end: bool = False) -> str:
    """The production card V details (card rows, routing, readiness, quota);
    ``end`` shows its second page (routing and the readiness legend)."""

    with golden_runtime(root) as runtime:
        screen = card_screen(runtime, profile_target(runtime, catalog.DEFAULT_SEED))
        win = FakeWindow([tui.curses.KEY_NPAGE, ESC] if end else [ESC], height=HEIGHT, width=WIDTH)
        screen._details(win)
        return last_frame(win, "lineup — full details")


def declaration_preview_golden(root: Path) -> str:
    """The production declaration preview of ``model_form`` (form values as drafted)."""

    import test_onboarding_isolation as journeys

    line = journeys.JourneyFixture().line()
    values = {name: str(default) for name, _, default, _ in views.model_form_fields(line, key="custom-fixture-reviewer")}
    with onboarding_actions([ESC]) as (_case, action, win), \
            mock.patch.object(tui.OnboardingForm, "run", return_value=values):
        action.show = lambda *args: None
        action.model_form("openrouter", line, "custom-fixture-reviewer")
        return last_frame(win, "Declaration preview")


def operator_lifecycle_golden(root: Path, *, state: str) -> str:
    """Models with an operator line ``changed — re-admit`` or ``route unapproved``."""

    import test_cli
    import test_onboarding_isolation as journeys

    case = journeys.JourneyFixture()
    case.setUp()
    try:
        if state == "changed":
            key = case.declare()
            case.invoke(["models", "admit", key])
            import claude_multi.cli.onboarding as onboarding
            edited = dict(case.line(), roles="all")
            with mock.patch("claude_multi.cli.consent.stdio_ttys", return_value=True):
                onboarding.edit_line(case.runtime, key, edited, confirm=lambda text: True, output=io.StringIO())
        else:
            case.op([*test_cli.ACME_ADD, "--declare-only"])
            case.op(test_cli.SMALL_ADD)
            key = "custom-acme-small"

        def screen():
            widget = cli._ModelsScreen(case.runtime, palette=tui.MONO_PALETTE)
            widget.index = widget.keys.index(key)
            return widget
        return render(screen)
    finally:
        case.doCleanups()


def admission_checklist_golden(root: Path) -> str:
    import test_onboarding_isolation as journeys
    import claude_multi.cli.screens.common as common
    import claude_multi.cli.consent as consent
    case = journeys.JourneyFixture()
    case.setUp()
    try:
        key = case.declare()
        action = common.OnboardingActions(case.runtime, None, tui.MONO_PALETTE)
        seen = []
        action.confirm = lambda text: seen.append(text) or False
        action.show = lambda *args: None
        import claude_multi.cli.commands.models as models
        identity = models.render_identity(case.runtime)
        # The render hashes an auth-dir under a disposable HOME. Fix this
        # observation input, not the rendered screenshot bytes.
        with mock.patch.object(consent, "stdio_ttys", return_value=True), mock.patch.object(
                models, "render_identity", return_value=("claude-multi-render-05100000", identity[1], identity[2])):
            action.invoke(["models", "admit", key])
        return render(lambda: tui.TextView("Confirm explicit action", seen[0].splitlines(), confirm=True))
    finally:
        case.doCleanups()


def candidates_detail_golden(root: Path, *, kind: str) -> str:
    """Candidate details through the production Models details action."""
    import test_discovery
    from claude_multi import launch
    import claude_multi.cli.gateway_facts as facts
    fixture = test_discovery.CandidateTests()
    fixture.setUp()
    try:
        registry = fixture.registry() if kind == "registry" else catalog.PinnedRegistry(root, {}, {})
        with golden_runtime(root) as runtime, mock.patch.object(facts, "pinned_registry", return_value=registry):
            if kind == "down":
                runtime.served_snapshot = lambda *a: (None, 503)
            elif kind == "extras":
                runtime.served_snapshot = lambda *a: ((launch.ServedModel("fixture-unattributed", "unknown", None),), 200)
            widget = cli._ModelsScreen(runtime, palette=tui.MONO_PALETTE)
            widget.index = widget.keys.index("__candidates__")
            win = FakeWindow([ESC], height=HEIGHT, width=WIDTH)
            widget._details(win)
            return last_frame(win, "model — full details")
    finally:
        fixture.doCleanups()


def route_consent_golden(root: Path) -> str:
    import test_cli
    import claude_multi.cli.screens.common as common
    with onboarding_actions([ESC, ESC]) as (case, action, win):
        case.op([*test_cli.ACME_ADD, "--declare-only"])
        action.confirm = lambda text: common.OnboardingActions.confirm(action, text)
        from claude_multi import served_plan
        # Stable preview identity input; no golden byte post-processing.
        with mock.patch.object(served_plan, "_digest", return_value="e" * 64):
            action.invoke(["providers", "approve", "acme"])
        return last_frame(win, "Confirm explicit action")


def changed_route_refusal_golden(root: Path) -> str:
    import json
    import test_cli
    from claude_multi import state
    with onboarding_actions([ESC]) as (case, action, win):
        case.op(test_cli.ACME_ADD, "y\n")
        case.op(test_cli.SMALL_ADD)
        path = case.runtime.home / ".config/claude-multi/providers.d/acme.json"
        doc = json.loads(path.read_bytes())
        doc["provider"]["base_url"] = "https://changed.example.test/anthropic"
        state.atomic_write(path, json.dumps(doc).encode())
        action.invoke(["models", "admit", "custom-acme-small"])
        return last_frame(win, "Action result")


def qualification_stale_golden(root: Path) -> str:
    from claude_multi import qualify
    import claude_multi.cli.onboarding as onboarding
    with onboarding_actions([ESC]) as (case, action, win):
        key = case.declare()
        case.invoke(["models", "admit", key])
        case.runtime.qualify_http = lambda *a, **kw: qualify.HttpResult(400, b"{}")
        output = io.StringIO()
        onboarding.invoke(case.runtime, ["models", "qualify", key, "--agents"],
                          confirm=lambda text: True, output=output)
        onboarding.invoke(case.runtime, ["models", "show", key],
                          confirm=lambda text: True, output=output)
        action.show("Action result", output.getvalue().splitlines())
        return last_frame(win, "Action result")


def keyed_provider_credentials_golden(root: Path) -> str:
    """Keyed compat credential cells (tests/test_openai_compat_keyed.py builder)."""

    import test_openai_compat_keyed as keyed

    return keyed.keyed_cells_golden(root)


def get_started_golden(root: Path) -> str:
    import test_screens_get_started as get_started

    return get_started.get_started_golden(root)


def get_started_picker_golden(root: Path) -> str:
    import test_picker_by_provider as picker

    return picker.picker_golden(root)


def signout_modal_golden(root: Path) -> str:
    import test_screens_providers_lifecycle as lifecycle

    return lifecycle.signout_modal_golden(root)


def remove_blocked_golden(root: Path) -> str:
    import test_screens_providers_lifecycle as lifecycle

    return lifecycle.remove_blocked_golden(root)


def profiles_golden(root: Path) -> str:
    import test_screens_profiles as profiles

    return profiles.profiles_golden(root)


def profiles_unloadable_golden(root: Path) -> str:
    import test_screens_profiles as profiles

    return profiles.profiles_unloadable_golden(root)


def card_not_connected_golden(root: Path) -> str:
    import test_screens_card_onboarding as onboarding

    return onboarding.card_not_connected_golden(root)


def _pending_record(runtime: cli.Runtime, *, follow: bool = True) -> dict:
    """The default seed's record with a recorded change waiting (pinned or following)."""

    runtime.profiles.install_seeds()
    record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title="api design",
                          follow=follow, pending=False)
    record["pending"] = {
        "requested_at": fx.iso(fx.FIXED_NOW - timedelta(hours=20)),
        "kind": "profile", "profile": relaunch_seed(runtime), "follow": True, "document": None,
        "reasons": ["native agents: explore replace → native"],
    }
    runtime.session_store.save(record)
    return runtime.session_store.load(fx.FIXED_ID)


def lineup_target_golden(root: Path, target: str) -> str:
    """The lineup dialog on the keep target (a pending change dropped,
    Follow off) or on the first fallback provider."""

    with golden_runtime(root) as runtime:
        record = _pending_record(runtime) if target == "keep" else fx.v4_record(
            runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title="api design")

        def dialog() -> cli._LineupDialog:
            screen = cli._LineupDialog(runtime, record, palette=tui.MONO_PALETTE, liveness="unknown")
            screen.target = target
            screen._preview()
            return screen

        return render(dialog)


def follow_preview_golden(root: Path) -> str:
    """Sessions F on a pinned session with a recorded change: the preview
    of following its profile again, before anything is written."""

    with golden_runtime(root) as runtime:
        _pending_record(runtime, follow=False)
        screen = cli._SessionsScreen(runtime, palette=tui.MONO_PALETTE, now=fx.FIXED_NOW)
        win = FakeWindow(["f", ESC, ESC], height=HEIGHT, width=WIDTH)
        screen.run(win)
        return last_frame(win, "follow — preview for")


def forget_typed_golden(root: Path) -> str:
    """Sessions X while background liveness cannot be observed: the typed
    session id dialog (Cancel follows the field)."""

    from claude_multi import sessions

    with golden_runtime(root) as runtime:
        runtime.profiles.install_seeds()
        fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title="api design")
        screen = cli._SessionsScreen(runtime, palette=tui.MONO_PALETTE, now=fx.FIXED_NOW)
        win = FakeWindow(["x", *fx.FIXED_ID[:8], ESC, ESC], height=HEIGHT, width=WIDTH)
        with mock.patch.object(runtime, "background_liveness",
                               return_value=sessions.BackgroundLiveness(False, frozenset(), "fixture: unreadable")):
            screen.run(win)
        return last_frame(win, "without a liveness check")


def card_help_golden(root: Path) -> str:
    """The card's help, first page (it scrolls)."""

    with golden_runtime(root) as runtime:
        screen = card_screen(runtime, profile_target(runtime, catalog.DEFAULT_SEED))
        win = FakeWindow(["?", ESC, ESC], height=HEIGHT, width=WIDTH)
        screen.run(win)
        return last_frame(win, "launch card — help")


LONG_NAME = "research-and-review-with-a-very-long-profile-name"


def card_long_name_golden(root: Path) -> str:
    """A profile with a long name and description: the title and the
    description clip with an ellipsis; nothing wraps."""

    with golden_runtime(root) as runtime:
        document = dict(runtime.profiles.load(catalog.DEFAULT_SEED))
        document.pop("seed", None)
        document["name"] = LONG_NAME
        document["description"] = ("A profile whose description is longer than any card row, so the card "
                                   "clips it at the edge instead of wrapping it into the rows below.")
        runtime.profiles.save(document)
        return render(lambda: card_screen(runtime, profile_target(runtime, LONG_NAME)))


def card_wide_golden(root: Path) -> str:
    """The card at 120x30: every agent row and the low-priority rows fit."""

    with golden_runtime(root) as runtime:
        return render(lambda: card_screen(runtime, profile_target(runtime, catalog.DEFAULT_SEED)),
                      *GOLDEN_SIZES["card-wide"])


def at_size(builder: Callable[[Path], str], size: tuple[int, int]) -> Callable[[Path], str]:
    """``builder`` drawn at ``size`` (every ``render`` call inside it)."""

    def build_at(root: Path) -> str:
        _SIZES.append(size)
        try:
            return builder(root)
        finally:
            _SIZES.pop()

    return build_at


# A wide terminal: every main screen is also dumped at this size.
WIDE = (120, 40)
WIDE_SCREENS: dict[str, Callable[[Path], str]] = {
    "providers-wide": providers_golden,
    "models-wide": models_golden,
    "settings-wide": settings_golden,
    "sessions-wide": sessions_golden,
    "direct-wide": direct_golden,
    "editor-balanced-wide": editor_golden,
    "lineup-live-wide": lineup_live_golden,
    "card-resume-diff-wide": card_resume_diff_golden,
}

# Goldens drawn at another size than 80x24 (their file names say so).
GOLDEN_SIZES: dict[str, tuple[int, int]] = {"card-wide": (120, 30), **{name: WIDE for name in WIDE_SCREENS}}


GOLDENS: dict[str, Callable[[Path], str]] = {
    "keyed-provider-credentials": keyed_provider_credentials_golden,
    "get-started": get_started_golden,
    "get-started-picker": get_started_picker_golden,
    "signout-modal": signout_modal_golden,
    "remove-blocked": remove_blocked_golden,
    "profiles": profiles_golden,
    "profiles-unloadable": profiles_unloadable_golden,
    "card-not-connected": card_not_connected_golden,
    "candidates-registry-detail": lambda root: candidates_detail_golden(root, kind="registry"),
    "candidates-gateway-down": lambda root: candidates_detail_golden(root, kind="down"),
    "candidates-served-extras": lambda root: candidates_detail_golden(root, kind="extras"),
    "provider-route-approval": route_consent_golden,
    "changed-route-refusal": changed_route_refusal_golden,
    "qualification-failure-stale": qualification_stale_golden,
    "declaration-preview": declaration_preview_golden,
    "admission-checklist": admission_checklist_golden,
    "qualification-selection": lambda root: render(lambda: tui.OnboardingForm(
        "qualify — choose checks", views.qualification_form_fields(), footer=views.QUALIFY_FORM_FOOTER)),
    "provider-kind": provider_kind_golden,
    "onboarding-listing": checkbox_golden,
    "openrouter-listing": openrouter_listing_golden,
    "listing-unavailable": listing_unavailable_golden,
    "operator-changed-readmit": lambda root: operator_lifecycle_golden(root, state="changed"),
    "operator-route-unapproved": lambda root: operator_lifecycle_golden(root, state="unapproved"),
    "model-declaration": model_form_golden,
    "pool-declaration": lambda root: model_form_golden(root, pool=True),
    "qualification-consent": qualification_consent_golden,
    "pool-qualification-consent": lambda root: qualification_consent_golden(root, pool=True),
    "operator-off": operator_models_golden,
    "operator-admitted": lambda root: operator_models_golden(root, admitted=True),
    "operator-picker-eligible": lambda root: operator_models_golden(root, admitted=True, eligible=True, picker=True),
    "operator-picker-ineligible": lambda root: operator_models_golden(root, admitted=True, picker=True),
    "sessions-all": sessions_all_golden,
    "card-details": card_details_golden,
    "card-details-routing": lambda root: card_details_golden(root, end=True),
    "models": models_golden,
    "providers": providers_golden,
    "providers-quota": providers_quota_golden,
    "card-quota": _card_quota_golden,
    "card-quota-resume": lambda root: _card_quota_golden(root, resume=True),
    "card-quota-exhausted": lambda root: _card_quota_golden(
        root, reason="quota exhausted", window=False, observed=False),
    "card-quota-exhausted-resume": lambda root: _card_quota_golden(
        root, resume=True, reason="quota exhausted", window=False, observed=False),
    "card-quota-unknown-reset": lambda root: _card_quota_golden(root, reset=False),
    "card-quota-unknown-reset-resume": lambda root: _card_quota_golden(root, reset=False, resume=True),
    "settings": settings_golden,
    # The footer of each selected-row kind (and a read-only Runtime).
    "settings-editable": lambda root: settings_golden(root, "compaction_percent"),
    "settings-preference": lambda root: settings_golden(root, "claude_feedback_drafts"),
    "settings-navigation": lambda root: settings_golden(root, "providers"),
    "settings-token": lambda root: settings_golden(root, "token"),
    "settings-evidence": lambda root: settings_golden(root, "reserve"),
    "settings-readonly": lambda root: settings_golden(root, "compaction_percent", writes=False),
    "editor-balanced": editor_golden,
    "picker-implementer": picker_golden,
    "routing-balanced": routing_golden,
    "propagate": propagate_golden,
    "card-fresh-default": card_fresh_default_golden,
    "card-fresh-direct": card_fresh_direct_golden,
    "card-blocked": card_blocked_golden,
    "card-resume-diff": card_resume_diff_golden,
    "direct": direct_golden,
    "sessions": sessions_golden,
    "lineup-live": lineup_live_golden,
    "lineup-relaunch": lineup_relaunch_golden,
    "lineup-keep": lambda root: lineup_target_golden(root, "keep"),
    "lineup-fallback": lambda root: lineup_target_golden(root, "fallback"),
    "sessions-follow-preview": follow_preview_golden,
    "sessions-forget-typed": forget_typed_golden,
    "card-help": card_help_golden,
    "card-long-name": card_long_name_golden,
    "card-wide": card_wide_golden,
    **{name: at_size(builder, WIDE) for name, builder in WIDE_SCREENS.items()},
}


def golden_name(name: str) -> str:
    width, height = GOLDEN_SIZES.get(name, (WIDTH, HEIGHT))
    return f"{name}-{width}x{height}.txt"


def build(name: str) -> str:
    """Render golden ``name`` in a fresh temp root (removed afterwards)."""

    root = Path(tempfile.mkdtemp(prefix=f"claude-multi-tui-golden-{name}-"))
    try:
        return GOLDENS[name](root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def tui_golden_files() -> dict[str, bytes]:
    """``{<name>-80x24.txt: bytes}`` for ``tests/goldens/tui/`` (bless.py)."""

    return {golden_name(name): build(name).encode("utf-8") for name in GOLDENS}
